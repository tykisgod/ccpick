import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import http from 'node:http';
import https from 'node:https';
import tls from 'node:tls';
import { once } from 'node:events';
import { PassThrough } from 'node:stream';
import { createHash, randomBytes, X509Certificate } from 'node:crypto';
import { setTimeout as delay } from 'node:timers/promises';
import { serveRuntime, runtimeRoot, runtimeDirectory } from '../runtime-service.mjs';
import { runtimeCertificate } from '../runtime-certificate.mjs';
import { createProfile, bindAccount, getProfile, globalConfigFile, atomicJson, readJson } from '../core.mjs';
import { registryState } from '../registry.mjs';

const uuid = suffix => `00000000-0000-4000-8000-${String(suffix).padStart(12, '0')}`;
const grant = label => ({ claudeAiOauth: { accessToken: `INVENTED-${label}-ACCESS`,
  refreshToken: `INVENTED-${label}-REFRESH`, expiresAt: Date.now() + 3_600_000,
  scopes: ['user:profile', 'user:inference'], subscriptionType: 'max' } });
const profile = label => ({ account: { uuid: uuid(label === 'A' ? 1 : 2), email: `${label.toLowerCase()}@example.com` },
  organization: { uuid: uuid(label === 'A' ? 3 : 4) } });
const streamResponse = (text = 'fixture') => {
  const value = new PassThrough(); value.statusCode = 200; value.headers = { 'content-type': 'text/event-stream' };
  queueMicrotask(() => value.end(text)); return value;
};
async function until(predicate, message = 'fixture condition timed out') {
  const deadline = Date.now() + 2500;
  while (!await predicate()) { assert(Date.now() < deadline, message); await delay(5); }
}
async function fixture(t, overrides = {}) {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), 'ccpick-service-test-'));
  if (process.platform !== 'win32') await fs.chmod(root, 0o700);
  const config = { serviceRoot: root, dataRoot: path.join(root, 'profiles', 'data'), seamlessAccounts: true,
    native: path.join(root, 'never-run-native'), browser: path.join(root, 'never-run-browser'), platform: process.platform };
  await fs.mkdir(config.dataRoot, { recursive: true, mode: 0o700 });
  const values = new Map(), writes = [], calls = [], accountCalls = [], services = [], profiles = {};
  const signalBefore = new Map(['SIGTERM', 'SIGINT'].map(name => [name, process.listeners(name)]));
  const store = { read: async directory => structuredClone(values.get(directory) ?? {}), write: async (directory, value) => {
    if (f.blockMirror && directory === runtimeDirectory(config)) throw new Error('EBUSY: PRIVATE credential detail');
    values.set(directory, structuredClone(value)); writes.push({ directory, value: structuredClone(value) });
  } };
  const f = { root, config, values, writes, calls, accountCalls, profiles, store, services };
  for (const label of ['A', 'B']) {
    let p = await createProfile(config, { name: `account-${label.toLowerCase()}`, email: `${label.toLowerCase()}@example.com` }, {
      initialize: async (_, target) => atomicJson(globalConfigFile(target), { userID: randomBytes(32).toString('hex'),
        machineID: randomBytes(32).toString('hex'), preference: 'kept' }),
    });
    const ids = await readJson(globalConfigFile(p)), remote = profile(label);
    await atomicJson(globalConfigFile(p), { ...ids, oauthAccount: { accountUuid: remote.account.uuid,
      organizationUuid: remote.organization.uuid, emailAddress: remote.account.email } });
    await bindAccount(config, p, { loggedIn: true, uuid: remote.account.uuid, email: remote.account.email });
    p = await getProfile(config, p.name); profiles[label] = p; values.set(p.configDirectory, grant(label));
  }
  await atomicJson(path.join(config.dataRoot, '..', 'state.json'), { version: 2, enabled: true,
    selected: profiles.A.name, selectedAt: 'fixture-generation-A', bootstrapEmail: 'fixture@example.com' });
  const defaults = {
    network: async () => ({ proxy: 'http://127.0.0.1:1' }), store,
    certificateFactory: async directory => (f.certificate = await runtimeCertificate(directory)),
    requestAccount: async (proxy, kind, options) => {
      accountCalls.push({ proxy, kind, options });
      if (kind === 'profile') return profile(options.token.includes('-B-') ? 'B' : 'A');
      if (kind === 'login') return { access_token: 'INVENTED-B-ACCESS', refresh_token: 'INVENTED-B-REFRESH', expires_in: 3600 };
      const label = options.body.refresh_token.includes('-B-') ? 'B' : 'A';
      return { access_token: `INVENTED-${label}-ACCESS-NEW`, refresh_token: `INVENTED-${label}-REFRESH-NEW`, expires_in: 3600 };
    },
    brokerTransport: async input => { calls.push(input); return streamResponse(); },
    apiHandler: async (_, res) => res.end('{"offline":true}'),
  };
  f.start = async (extra = {}) => {
    const service = await serveRuntime(config, { ...defaults, ...overrides, ...extra }); services.push(service);
    f.service = service; f.key = (await readJson(path.join(runtimeRoot(config), 'channel-key.json'))).value; return service;
  };
  f.control = (route, input = {}, headers = {}) => new Promise((resolve, reject) => {
    const request = http.request({ hostname: '127.0.0.1', port: f.service.control.address().port,
      method: 'POST', path: route, agent: false, headers: { 'content-type': 'application/json', 'x-ccpick-account-runtime': f.key, ...headers } }, response => {
      const parts = []; response.on('data', part => parts.push(part)); response.on('error', reject);
      response.on('end', () => resolve({ status: response.statusCode, value: JSON.parse(Buffer.concat(parts)) }));
    }); request.once('error', reject); request.end(JSON.stringify(input));
  });
  f.request = async (route, { host = 'api.anthropic.com', headers = {}, body = '', method = 'POST' } = {}) => {
    const socket = await new Promise((resolve, reject) => {
      const request = http.request({ hostname: '127.0.0.1', port: f.service.guard.address().port,
        method: 'CONNECT', path: `${host}:443`, headers: { host: `${host}:443` }, agent: false });
      request.once('error', reject); request.once('connect', (response, tunnel) => {
        if (response.statusCode !== 200) { tunnel.destroy(); reject(new Error('fixture_connect_refused')); }
        else resolve(tunnel);
      }); request.end();
    });
    const secure = tls.connect({ socket, servername: host, ca: f.certificate.cert, rejectUnauthorized: true });
    await once(secure, 'secureConnect');
    const agent = new https.Agent({ keepAlive: false }); agent.createConnection = (_, callback) => callback(null, secure);
    try {
      return await new Promise((resolve, reject) => {
        const request = https.request({ hostname: host, method, path: route, agent, headers: { host,
          'content-type': 'application/json', 'content-length': Buffer.byteLength(body), ...headers } }, response => {
          const parts = []; response.on('data', part => parts.push(part)); response.on('error', reject);
          response.on('end', () => resolve({ status: response.statusCode, body: Buffer.concat(parts).toString() }));
        }); request.once('error', reject); request.end(body);
      });
    } finally { agent.destroy(); secure.destroy(); }
  };
  f.model = scope => f.request('/v1/messages?beta=true', { headers: { authorization: `Bearer ${f.key}`,
    'x-ccpick-account-runtime': f.key, 'x-ccpick-account-scope': scope ?? 'default' }, body: JSON.stringify({
      model: 'claude-fixture', messages: [{ role: 'user', content: 'same conversation' }],
      metadata: { user_id: JSON.stringify({ device_id: '', account_uuid: '', session_id: 'same-session' }) }, stream: true,
    }) });
  t.after(async () => {
    for (const service of services) await service.close();
    for (const [name, before] of signalBefore) for (const listener of process.listeners(name))
      if (!before.includes(listener)) process.removeListener(name, listener);
    await fs.rm(root, { recursive: true, force: true });
  });
  return f;
}
function assertOwner(call, label) {
  assert(call.headers.authorization.startsWith(`Bearer INVENTED-${label}-ACCESS`));
  assert.equal(JSON.parse(JSON.parse(call.body).metadata.user_id).account_uuid, profile(label).account.uuid);
  assert.equal(call.headers['x-ccpick-account-scope'], undefined);
}
async function exchangeLogin(f) {
  const state = 'fixture-oauth-state', verifier = 'fixture-code-verifier';
  const begun = await f.control('/login-begin', { sessionId: 'fixture-session', oauthState: state,
    oauthChallenge: createHash('sha256').update(verifier).digest('base64url') });
  assert.equal(begun.status, 200);
  return f.request('/v1/oauth/token', { host: 'platform.claude.com', body: JSON.stringify({
    grant_type: 'authorization_code', code: 'INVENTED-CODE', state, code_verifier: verifier,
  }) });
}

test('guarded select RPC preserves a newer manual selection and rejects malformed guards', async t => {
  const f = await fixture(t); await f.start();
  const initial = await registryState(f.config);
  const expectedState = { selected: initial.selected, selectedAt: initial.selectedAt };
  for (const guard of [undefined, null, {}, [], { selected: 'account-a' },
    { selected: 'account-a', selectedAt: null },
    { ...expectedState, ignored: true }]) {
    const result = await f.control('/select-guarded', { name: 'account-b', ...(guard === undefined ? {} : { expectedState: guard }) });
    assert.equal(result.status, 409);
    assert.deepEqual(result.value, { ok: false, reason: 'selection_changed', selectionGuard: 1 });
    assert.deepEqual(await registryState(f.config), initial);
  }
  assert.equal((await f.control('/select', { name: 'account-b', expectedState })).value.reason, 'selection_changed');
  assert.deepEqual(await registryState(f.config), initial);
  assert.equal((await f.control('/select', { name: 'account-a' })).status, 200);
  const later = await registryState(f.config), callsBefore = f.accountCalls.length;
  assert.notEqual(later.selectedAt, initial.selectedAt);
  const stale = await f.control('/select-guarded', { name: 'account-b', expectedState });
  assert.equal(stale.status, 409); assert.equal(stale.value.selectionGuard, 1);
  assert.equal(stale.value.reason, 'selection_changed');
  assert.deepEqual(await registryState(f.config), later);
  assert.equal(f.accountCalls.length, callsBefore, 'stale automatic selection does not authenticate target');
  const accepted = await f.control('/select-guarded', { name: 'account-b',
    expectedState: { selected: later.selected, selectedAt: later.selectedAt } });
  assert.equal(accepted.status, 200); assert.equal(accepted.value.selectionGuard, 1);
  const committed = await registryState(f.config);
  assert.equal(committed.selected, 'account-b');
  assert.deepEqual(accepted.value.selectionReceipt, { profileId: committed.selected, generation: committed.selectedAt });
  assert.deepEqual(Object.keys(accepted.value).sort(), ['ok', 'selectionGuard', 'selectionReceipt']);
  await f.model(); assertOwner(f.calls.at(-1), 'B');
});

test('guarded RPC returns its immutable selection receipt when a human changes before response delivery', async t => {
  const f = await fixture(t); await f.start();
  const before = await registryState(f.config);
  const original = f.service.coordinator.select.bind(f.service.coordinator);
  let human;
  f.service.coordinator.select = async (...args) => {
    const committed = await original(...args);
    human = await original('account-a');
    return committed;
  };
  const reply = await f.control('/select-guarded', { name: 'account-b',
    expectedState: { selected: before.selected, selectedAt: before.selectedAt } });
  assert.equal(reply.status, 200);
  assert.equal(reply.value.selectionReceipt.profileId, 'account-b');
  assert.notEqual(reply.value.selectionReceipt.generation, human.generation);
  assert.equal((await registryState(f.config)).selected, 'account-a');
  f.service.coordinator.select = original;
  const next = await f.control('/select-guarded', { name: 'account-b', expectedState: {
    selected: reply.value.selectionReceipt.profileId, selectedAt: reply.value.selectionReceipt.generation } });
  assert.equal(next.value.reason, 'selection_changed');
  assert.equal((await registryState(f.config)).selectedAt, human.generation);
});

test('service connects guarded model requests to vault snapshots and access-only shared runtime', async t => {
  const f = await fixture(t); await f.start();
  assert.equal((await f.control('/status')).value.ready, true);
  assert.equal((await f.model()).status, 200); assertOwner(f.calls[0], 'A');
  assert.equal(f.values.get(runtimeDirectory(f.config)).claudeAiOauth.refreshToken, undefined);
  assert.equal(f.values.get(f.profiles.A.configDirectory).claudeAiOauth.refreshToken, 'INVENTED-A-REFRESH');
  assert.equal((await readJson(path.join(runtimeDirectory(f.config), '.claude.json'))).preference, 'kept');
  assert.equal(await fs.realpath(path.join(runtimeDirectory(f.config), 'projects')),
    await fs.realpath(path.join(f.config.dataRoot, '..', 'shared-history', 'projects')));
  assert.equal((await f.control('/ready', { name: 'account-b' })).value.ready, true);
  assert.equal((await registryState(f.config)).selected, 'account-a');
});

test('first scope imports both legacy input histories and referenced paste caches only once', async t => {
  const f = await fixture(t), project = path.join(f.root, 'fixture-project');
  const row = (display, timestamp, pastedContents = {}) => ({ display, timestamp, pastedContents, project,
    sessionId: '00000000-0000-4000-8000-000000000099' });
  const jsonl = rows => rows.map(value => JSON.stringify(value)).join('\n') + '\n';
  const expected = [], originals = new Map();
  for (const [index, label] of ['A', 'B'].entries()) {
    const source = f.profiles[label].configDirectory, text = `INVENTED fixture ${label} pasted text\n`.repeat(100);
    const contentHash = createHash('sha256').update(text).digest('hex').slice(0, 16);
    await fs.mkdir(path.join(source, 'paste-cache'), { mode: 0o700 });
    await fs.writeFile(path.join(source, 'paste-cache', `${contentHash}.txt`), text, { mode: 0o600 });
    const record = row(`Earlier ${label} [Pasted text #1]`, index + 1,
      { 1: { id: 1, type: 'text', contentHash } });
    await fs.writeFile(path.join(source, 'history.jsonl'), jsonl([record]), { mode: 0o600 });
    expected.push(record); originals.set(contentHash, text);
  }
  await f.start();
  const directory = runtimeDirectory(f.config), historyFile = path.join(directory, 'history.jsonl');
  const readHistory = async () => (await fs.readFile(historyFile, 'utf8')).trim().split('\n').map(JSON.parse);
  assert.deepEqual(await readHistory(), expected);
  for (const [hash, text] of originals)
    assert.equal(await fs.readFile(path.join(directory, 'paste-cache', `${hash}.txt`), 'utf8'), text);
  const runtimeInput = row('Native runtime input remains newest', 3);
  await fs.appendFile(historyFile, jsonl([runtimeInput]));
  await fs.appendFile(path.join(f.profiles.B.configDirectory, 'history.jsonl'), jsonl([row('Do not reimport after initialization', 4)]));
  await f.control('/select', { name: 'account-b' }); await f.control('/select', { name: 'account-a' });
  assert.deepEqual(await readHistory(), [...expected, runtimeInput]);
  await f.service.close(); await f.start();
  assert.deepEqual(await readHistory(), [...expected, runtimeInput]);
  for (const [hash, text] of originals)
    assert.equal(await fs.readFile(path.join(directory, 'paste-cache', `${hash}.txt`), 'utf8'), text);
});

test('default switching leaves explicitly pinned runtime scope and same session independent', async t => {
  const f = await fixture(t); await f.start();
  const registered = await Promise.all(Array.from({ length: 4 }, () => f.control('/register', { scope: 'account-a' })));
  assert.equal(new Set(registered.map(value => value.value.directory)).size, 1);
  await f.model('account-a'); await f.control('/select', { name: 'account-b' });
  await f.model(); await f.model('account-a');
  assertOwner(f.calls[0], 'A'); assertOwner(f.calls[1], 'B'); assertOwner(f.calls[2], 'A');
  assert(f.calls.every(call => JSON.parse(JSON.parse(call.body).metadata.user_id).session_id === 'same-session'));
  assert.equal((await registryState(f.config)).selected, 'account-b');
});

test('daemon workers restore the private local channel from user settings and retain their own scope', async t => {
  const f = await fixture(t), sourceSettings = new Map();
  for (const p of Object.values(f.profiles))
    sourceSettings.set(p.name, await fs.readFile(path.join(p.configDirectory, 'settings.json')));
  await f.start();
  const pinned = await f.control('/register', { scope: 'account-a' });
  const body = JSON.stringify({ model: 'claude-fixture', messages: [], stream: true,
    metadata: { user_id: JSON.stringify({ device_id: '', account_uuid: '', session_id: 'same-session' }) } });
  const missing = await f.request('/v1/messages', { body });
  assert.equal(missing.status, 403); assert.equal(f.calls.length, 0);
  const requestFromSettings = async directory => {
    const { env } = await readJson(path.join(directory, 'settings.json'));
    assert.equal(env.ANTHROPIC_BASE_URL, 'https://api.anthropic.com');
    const headers = Object.fromEntries(env.ANTHROPIC_CUSTOM_HEADERS.split('\n').map(line => {
      const at = line.indexOf(':'); return [line.slice(0, at).toLowerCase(), line.slice(at + 1).trim()];
    }));
    assert.equal(headers.authorization, `Bearer ${f.key}`);
    return f.request('/v1/messages', { body, headers });
  };
  assert.equal((await requestFromSettings(runtimeDirectory(f.config))).status, 200);
  await f.control('/select', { name: 'account-b' });
  assert.equal((await requestFromSettings(runtimeDirectory(f.config))).status, 200);
  assert.equal((await requestFromSettings(pinned.value.directory)).status, 200);
  assertOwner(f.calls[0], 'A'); assertOwner(f.calls[1], 'B'); assertOwner(f.calls[2], 'A');
  for (const p of Object.values(f.profiles))
    assert.deepEqual(await fs.readFile(path.join(p.configDirectory, 'settings.json')), sourceSettings.get(p.name));
});

test('ordinary MCP OAuth remains with its runtime scope across Claude account changes without changing vaults', async t => {
  const f = await fixture(t);
  for (const label of ['A', 'B']) {
    const value = f.values.get(f.profiles[label].configDirectory);
    value.mcpOAuth = { fixture: { accessToken: `INVENTED-MCP-${label}` } };
    f.values.set(f.profiles[label].configDirectory, value);
  }
  const beforeA = structuredClone(f.values.get(f.profiles.A.configDirectory));
  const beforeB = structuredClone(f.values.get(f.profiles.B.configDirectory));
  await f.start();
  const directory = runtimeDirectory(f.config);
  assert.equal(f.values.get(directory).mcpOAuth.fixture.accessToken, 'INVENTED-MCP-A');
  const native = f.values.get(directory);
  native.mcpOAuth.fixture.accessToken = 'INVENTED-MCP-A-RENEWED';
  f.values.set(directory, native);
  assert.equal((await f.control('/select', { name: 'account-b' })).status, 200);
  assert.equal(f.values.get(directory).claudeAiOauth.accessToken, 'INVENTED-B-ACCESS');
  assert.equal(f.values.get(directory).mcpOAuth.fixture.accessToken, 'INVENTED-MCP-A-RENEWED');
  assert.equal((await f.control('/select', { name: 'account-a' })).status, 200);
  assert.equal(f.values.get(directory).mcpOAuth.fixture.accessToken, 'INVENTED-MCP-A-RENEWED');
  const scoped = await f.control('/register', { scope: 'account-b' });
  assert.equal(f.values.get(scoped.value.directory).mcpOAuth.fixture.accessToken, 'INVENTED-MCP-B');
  assert.notEqual(scoped.value.directory, directory);
  assert.deepEqual(f.values.get(f.profiles.A.configDirectory), beforeA);
  assert.deepEqual(f.values.get(f.profiles.B.configDirectory), beforeB);
  await f.service.close(); await f.start();
  assert.equal(f.values.get(directory).mcpOAuth.fixture.accessToken, 'INVENTED-MCP-A-RENEWED');
});

test('clearing ordinary MCP credentials in an initialized runtime does not resurrect a vault copy', async t => {
  const f = await fixture(t), vault = f.values.get(f.profiles.A.configDirectory);
  vault.mcpOAuth = { fixture: { accessToken: 'INVENTED-MCP-A' } };
  f.values.set(f.profiles.A.configDirectory, vault); await f.start();
  f.values.set(runtimeDirectory(f.config), {});
  await f.control('/select', { name: 'account-b' }); await f.control('/select', { name: 'account-a' });
  assert.equal(f.values.get(runtimeDirectory(f.config)).mcpOAuth, undefined);
  assert.equal(f.values.get(f.profiles.A.configDirectory).mcpOAuth.fixture.accessToken, 'INVENTED-MCP-A');
});

test('control requires the exact local key and Host, rejects even empty Origin and duplicate keys', async t => {
  const f = await fixture(t); await f.start();
  for (const headers of [{ 'x-ccpick-account-runtime': '0'.repeat(64) }, { origin: '' }, { host: 'evil.invalid' },
    { 'x-ccpick-account-runtime': [f.key, f.key] }]) assert.equal((await f.control('/status', {}, headers)).status, 403);
});

test('one startup lock excludes another service and repeat close cannot remove the successor lock', async t => {
  const f = await fixture(t), first = await f.start();
  await assert.rejects(f.start(), { code: 'EEXIST' }); await first.close();
  const second = await f.start(); const lockPath = path.join(runtimeRoot(f.config), 'service.lock');
  const current = await fs.readFile(lockPath); await first.close();
  assert.deepEqual(await fs.readFile(lockPath), current); await second.close();
  await assert.rejects(fs.stat(lockPath), { code: 'ENOENT' });
});

test('restart preserves both native endpoints, channel key and certificate', async t => {
  const f = await fixture(t); await f.start();
  const previous = await readJson(path.join(runtimeRoot(f.config), 'service.json'));
  const key = f.key, cert = f.certificate.cert.toString();
  await f.service.close(); await f.start();
  const current = await readJson(path.join(runtimeRoot(f.config), 'service.json'));
  assert.equal(current.proxyPort, previous.proxyPort); assert.equal(current.controlPort, previous.controlPort);
  assert.equal(current.certificateFingerprint, previous.certificateFingerprint); assert.equal(current.certFile, previous.certFile);
  assert.equal(f.key, key); assert.equal(f.certificate.cert.toString(), cert);
  assert.equal((await f.model()).status, 200); assert.equal((await f.control('/status')).value.ready, true);
});

for (const endpoint of ['proxyPort', 'controlPort']) test(`occupied persisted ${endpoint} fails closed without changing the saved endpoint`, async t => {
  const f = await fixture(t); await f.start();
  const filename = path.join(runtimeRoot(f.config), 'service.json'), before = await fs.readFile(filename);
  const descriptor = JSON.parse(before); await f.service.close();
  const occupant = http.createServer();
  await new Promise(resolve => occupant.listen(descriptor[endpoint], '127.0.0.1', resolve));
  t.after(() => new Promise(resolve => occupant.close(resolve)));
  await assert.rejects(f.start(), endpoint === 'proxyPort' ? /guard_listen_failed/ : { code: 'EADDRINUSE' });
  assert.deepEqual(await fs.readFile(filename), before);
  await assert.rejects(fs.stat(path.join(runtimeRoot(f.config), 'service.lock')), { code: 'ENOENT' });
});

test('changed certificate cannot silently replace the trust anchor retained by live native clients', async t => {
  const f = await fixture(t); await f.start(); await f.service.close();
  await assert.rejects(f.start({ certificateFactory: async () => ({ ...f.certificate, fingerprint: 'different' }) }), /runtime_unavailable/);
  await assert.rejects(fs.stat(path.join(runtimeRoot(f.config), 'service.lock')), { code: 'ENOENT' });
});

for (const failure of ['missing', 'replaced']) test(`a ${failure} channel key cannot silently replace the key retained by live native clients`, async t => {
  const f = await fixture(t); await f.start(); await f.service.close();
  const keyFile = path.join(runtimeRoot(f.config), 'channel-key.json');
  if (failure === 'missing') await fs.unlink(keyFile);
  else await atomicJson(keyFile, { value: '0'.repeat(64) });
  await assert.rejects(f.start(), /runtime_key_invalid/);
  if (failure === 'missing') await assert.rejects(fs.stat(keyFile), { code: 'ENOENT' });
  await assert.rejects(fs.stat(path.join(runtimeRoot(f.config), 'service.lock')), { code: 'ENOENT' });
});

test('close keeps service lock until an already admitted refresh commit finishes', async t => {
  let release, refreshStarted = false;
  const f = await fixture(t, { requestAccount: async (_, kind, options) => {
    if (kind === 'profile') return profile(options.token.includes('-B-') ? 'B' : 'A');
    refreshStarted = true; await new Promise(resolve => { release = resolve; });
    return { access_token: 'INVENTED-B-ACCESS-NEW', refresh_token: 'INVENTED-B-REFRESH-NEW', expires_in: 3600 };
  } });
  await f.start(); const expired = grant('B'); expired.claudeAiOauth.expiresAt = 0; f.values.set(f.profiles.B.configDirectory, expired);
  const pending = f.control('/ready', { name: 'account-b' }); pending.catch(() => {});
  await until(() => refreshStarted);
  let closed = false; const closing = f.service.close().then(() => { closed = true; });
  await delay(20); assert.equal(closed, false);
  assert(await fs.stat(path.join(runtimeRoot(f.config), 'service.lock')));
  release(); await closing; await pending.catch(() => {});
  assert.equal(f.values.get(f.profiles.B.configDirectory).claudeAiOauth.refreshToken, 'INVENTED-B-REFRESH-NEW');
  await assert.rejects(fs.stat(path.join(runtimeRoot(f.config), 'service.lock')), { code: 'ENOENT' });
});

test('OAuth state/PKCE exchange persists the grant privately before replying, then native mirror promotes B', async t => {
  const f = await fixture(t); await f.start();
  const state = 'fixture-oauth-state', verifier = 'fixture-code-verifier';
  const challenge = createHash('sha256').update(verifier).digest('base64url');
  assert.equal((await f.control('/login-begin', { sessionId: 'fixture-session', oauthState: state, oauthChallenge: challenge })).status, 200);
  const exchange = await f.request('/v1/oauth/token', { host: 'platform.claude.com', body: JSON.stringify({
    grant_type: 'authorization_code', code: 'INVENTED-CODE', state, code_verifier: verifier,
  }) });
  assert.equal(exchange.status, 200);
  const preserved = f.writes.find(value => value.directory.includes('pending-logins') && value.value.oauthResponse);
  assert(preserved); assert.equal(preserved.value.oauthResponse.refresh_token, 'INVENTED-B-REFRESH');
  const record = await fs.readFile(path.join(preserved.directory, 'record.json'), 'utf8');
  assert(!record.includes('INVENTED-B-REFRESH'));
  f.values.set(runtimeDirectory(f.config), grant('B'));
  assert.equal((await f.control('/login-finished')).status, 200);
  await f.model(); assertOwner(f.calls.at(-1), 'B');
  assert.equal((await registryState(f.config)).selected, 'account-b');
  assert.equal(f.values.get(runtimeDirectory(f.config)).claudeAiOauth.refreshToken, undefined);
  assert.deepEqual(f.values.get(preserved.directory), {});
  assert((await readJson(path.join(preserved.directory, 'record.json'))).acknowledgedAt);
  assert.equal(f.values.get(runtimeDirectory(f.config)).ccpickRuntimeGrant, undefined);
});

test('startup recovers a consumed-code grant after native exits without changing a later picker selection', async t => {
  const f = await fixture(t, { requestAccount: async (_, kind, options) => kind === 'profile' ?
    profile(options.token.includes('-B-') ? 'B' : 'A') :
    { access_token: 'INVENTED-B-RECOVERED-ACCESS', refresh_token: 'INVENTED-B-RECOVERED-REFRESH', expires_in: 3600 } });
  await f.start(); assert.equal((await exchangeLogin(f)).status, 200);
  const preserved = f.writes.find(value => value.value.oauthResponse);
  const idsBefore = await readJson(globalConfigFile(f.profiles.B));
  await f.control('/select', { name: 'account-b' }); await f.control('/select', { name: 'account-a' });
  const selection = await registryState(f.config); await f.service.close(); await f.start();
  assert.deepEqual(await registryState(f.config), selection);
  const saved = f.values.get(f.profiles.B.configDirectory);
  assert.equal(saved.claudeAiOauth.refreshToken, 'INVENTED-B-RECOVERED-REFRESH');
  assert.equal(saved.claudeAiOauth.subscriptionType, 'max');
  const idsAfter = await readJson(globalConfigFile(f.profiles.B));
  assert.equal(idsBefore.userID, idsAfter.userID); assert.equal(idsBefore.machineID, idsAfter.machineID);
  assert.deepEqual(f.values.get(preserved.directory), {});
  assert.equal((await f.model()).status, 200); assertOwner(f.calls.at(-1), 'A');
});

test('recovery works when the exchange was durable but observeLoginGrant failed before native reply', async t => {
  const f = await fixture(t); await f.start();
  t.mock.method(f.service.coordinator, 'observeLoginGrant', async () => { throw new Error('fixture_interrupted'); });
  assert.equal((await exchangeLogin(f)).status, 409);
  const pending = f.writes.find(value => value.value.oauthResponse);
  await f.service.close(); await f.start();
  assert.deepEqual(f.values.get(pending.directory), {});
  assert.equal((await registryState(f.config)).selected, 'account-a');
  assert.equal((await readJson(path.join(pending.directory, 'record.json'))).profileId, 'account-b');
});

test('failed journal recovery backs off instead of probing the profile on every runtime poll', async t => {
  let attempts = 0;
  const f = await fixture(t, { recoveryDelayMs: 0, recoveryRetryMs: 10_000, requestAccount: async (_, kind, options) => {
    if (kind === 'profile') {
      if (options.token.includes('-B-')) { attempts++; throw new Error('network_unavailable'); }
      return profile('A');
    }
    return { access_token: 'INVENTED-B-ACCESS', refresh_token: 'INVENTED-B-REFRESH', expires_in: 3600 };
  } });
  await f.start(); assert.equal((await exchangeLogin(f)).status, 200);
  await until(() => attempts === 1); await delay(1100);
  assert.equal(attempts, 1);
  assert.equal((await registryState(f.config)).selected, 'account-a');
  assert.equal((await f.model()).status, 200);
});

test('failed staged-grant polling backs off while explicit login completion can retry immediately', async t => {
  let attempts = 0;
  const f = await fixture(t, { requestAccount: async (_, kind, options) => {
    if (kind === 'profile') {
      if (options.token.includes('-B-')) { attempts++; throw new Error('network_unavailable'); }
      return profile('A');
    }
    return { access_token: 'INVENTED-B-ACCESS', refresh_token: 'INVENTED-B-REFRESH', expires_in: 3600 };
  } });
  await f.start(); assert.equal((await exchangeLogin(f)).status, 200);
  f.values.set(runtimeDirectory(f.config), grant('B'));
  await until(() => attempts === 1); await delay(1100);
  assert.equal(attempts, 1);
  assert.equal((await f.control('/login-finished')).status, 409);
  assert.equal(attempts, 2);
});

test('native completion arriving after recovery and refresh cannot restore the original refresh token', async t => {
  const f = await fixture(t); await f.start();
  assert.equal((await exchangeLogin(f)).status, 200);
  await f.service.close(); await f.start();
  const saved = f.values.get(f.profiles.B.configDirectory);
  saved.claudeAiOauth.expiresAt = 0; f.values.set(f.profiles.B.configDirectory, saved);
  assert.equal((await f.control('/ready', { name: 'account-b' })).status, 200);
  assert.equal(f.values.get(f.profiles.B.configDirectory).claudeAiOauth.refreshToken, 'INVENTED-B-REFRESH-NEW');
  f.values.set(runtimeDirectory(f.config), grant('B'));
  assert.equal((await f.control('/login-finished')).status, 200);
  assert.equal(f.values.get(f.profiles.B.configDirectory).claudeAiOauth.refreshToken, 'INVENTED-B-REFRESH-NEW');
  assert.equal(f.values.get(runtimeDirectory(f.config)).claudeAiOauth.accessToken, 'INVENTED-B-ACCESS-NEW');
  assert.equal((await registryState(f.config)).selected, 'account-b');
});

test('an expired offline journal renews privately before verification and retains the rotated grant on outage', async t => {
  let failProfile = false, refreshes = 0;
  const f = await fixture(t, { requestAccount: async (_, kind, options) => {
    if (kind === 'profile') {
      if (failProfile && options.token.includes('-B-')) throw new Error('network_unavailable');
      return profile(options.token.includes('-B-') ? 'B' : 'A');
    }
    if (kind === 'refresh') { refreshes++; return { access_token: 'INVENTED-B-ACCESS-NEW',
      refresh_token: 'INVENTED-B-REFRESH-NEW', expires_in: 3600 }; }
    return { access_token: 'INVENTED-B-ACCESS', refresh_token: 'INVENTED-B-REFRESH', expires_in: 3600 };
  } });
  await f.start(); assert.equal((await exchangeLogin(f)).status, 200);
  const pending = f.writes.find(value => value.value.oauthResponse);
  await f.service.close();
  const recordPath = path.join(pending.directory, 'record.json'), record = await readJson(recordPath);
  await atomicJson(recordPath, { ...record, capturedAt: new Date(Date.now() - 7_200_000).toISOString() });
  failProfile = true; await f.start();
  assert.equal(refreshes, 1);
  assert.equal(f.values.get(pending.directory).recoveredCredentials.claudeAiOauth.refreshToken, 'INVENTED-B-REFRESH-NEW');
  await f.service.close(); failProfile = false; await f.start();
  assert.equal(refreshes, 1);
  assert.equal(f.values.get(f.profiles.B.configDirectory).claudeAiOauth.refreshToken, 'INVENTED-B-REFRESH-NEW');
  assert.deepEqual(f.values.get(pending.directory), {});
  assert.equal((await registryState(f.config)).selected, 'account-a');
});

for (const laterPicker of [false, true]) test(`expired staged login uses its recovered current grant without reversing a later picker (${laterPicker})`, async t => {
  let expiredOldB = false;
  const f = await fixture(t, { requestAccount: async (_, kind, options) => {
    if (kind === 'profile') {
      if (expiredOldB && options.token === 'INVENTED-B-ACCESS') throw new Error('login_required');
      return profile(options.token.includes('-B-') ? 'B' : 'A');
    }
    if (kind === 'refresh') return { access_token: 'INVENTED-B-ACCESS-NEW', refresh_token: 'INVENTED-B-REFRESH-NEW', expires_in: 3600 };
    return { access_token: 'INVENTED-B-ACCESS', refresh_token: 'INVENTED-B-REFRESH', expires_in: 3600 };
  } });
  await f.start(); assert.equal((await exchangeLogin(f)).status, 200);
  const pending = f.writes.find(value => value.value.oauthResponse);
  if (laterPicker) { await f.control('/select', { name: 'account-b' }); await f.control('/select', { name: 'account-a' }); }
  await f.service.close();
  const recordPath = path.join(pending.directory, 'record.json'), record = await readJson(recordPath);
  assert.equal(record.acknowledgedAt, undefined, 'recovery fixture must begin with an uncommitted journal');
  assert.notEqual(f.values.get(f.profiles.B.configDirectory).ccpickRuntimeGrant?.id, record.id);
  const native = grant('B'); native.claudeAiOauth.subscriptionType = 'pro';
  f.values.set(runtimeDirectory(f.config), native);
  await atomicJson(recordPath, { ...record, capturedAt: new Date(Date.now() - 7_200_000).toISOString() });
  expiredOldB = true; await f.start();
  assert.equal(f.values.get(f.profiles.B.configDirectory).claudeAiOauth.accessToken, 'INVENTED-B-ACCESS-NEW');
  assert.equal(f.values.get(f.profiles.B.configDirectory).claudeAiOauth.subscriptionType, 'pro');
  assert.equal((await f.model()).status, 200);
  assertOwner(f.calls.at(-1), laterPicker ? 'A' : 'B');
  assert.equal((await registryState(f.config)).selected, laterPicker ? 'account-a' : 'account-b');
});

test('shutdown drains an in-flight native completion without replaying its committed journal', async t => {
  const f = await fixture(t); await f.start();
  assert.equal((await exchangeLogin(f)).status, 200);
  const pending = f.writes.find(value => value.value.oauthResponse);
  await f.control('/select', { name: 'account-b' }); await f.control('/select', { name: 'account-a' });
  const selected = await registryState(f.config), snapshot = f.service.coordinator.snapshot.bind(f.service.coordinator);
  let entered = false, release;
  const gate = new Promise(resolve => { release = resolve; });
  t.mock.method(f.service.coordinator, 'snapshot', async () => {
    entered = true; await gate; return snapshot();
  });
  try {
    await until(() => entered, 'runtime poll must be in flight before shutdown');
    const native = grant('B'); native.claudeAiOauth.subscriptionType = 'pro';
    f.values.set(runtimeDirectory(f.config), native);
    const closing = f.service.close(); release(); await closing;
  } finally { release(); }
  const recordPath = path.join(pending.directory, 'record.json'), record = await readJson(recordPath);
  assert(record.acknowledgedAt);
  const saved = structuredClone(f.values.get(f.profiles.B.configDirectory));
  assert.equal(saved.ccpickRuntimeGrant.id, record.id);
  assert.equal(saved.claudeAiOauth.subscriptionType, 'pro');
  assert.deepEqual(f.values.get(pending.directory), {});
  await atomicJson(recordPath, { ...record, capturedAt: new Date(Date.now() - 7_200_000).toISOString() });
  await f.start();
  assert.deepEqual(f.values.get(f.profiles.B.configDirectory), saved);
  assert.equal(f.accountCalls.filter(value => value.kind === 'refresh').length, 0);
  assert.deepEqual(await registryState(f.config), selected);
  assert.equal((await f.model()).status, 200); assertOwner(f.calls.at(-1), 'A');
});

test('a crash after vault commit but before journal erasure retries erasure without importing or probing again', async t => {
  const f = await fixture(t); await f.start(); assert.equal((await exchangeLogin(f)).status, 200);
  const pending = f.writes.find(value => value.value.oauthResponse);
  const originalWrite = f.store.write;
  f.store.write = async (directory, value) => {
    if (directory === pending.directory && !Object.keys(value).length) throw new Error('fixture_keychain_busy');
    return originalWrite(directory, value);
  };
  f.values.set(runtimeDirectory(f.config), grant('B'));
  assert.equal((await f.control('/login-finished')).status, 409);
  assert((await readJson(path.join(pending.directory, 'record.json'))).acknowledgedAt);
  await f.service.close(); f.store.write = originalWrite;
  const vaultWrites = f.writes.filter(value => value.directory === f.profiles.B.configDirectory).length;
  await f.start();
  assert.deepEqual(f.values.get(pending.directory), {});
  assert.equal(f.writes.filter(value => value.directory === f.profiles.B.configDirectory).length, vaultWrites,
    'already committed native metadata and recovery do not duplicate the vault write');
});

test('OAuth exchange remains owned through shutdown and preserves the consumed-code grant', async t => {
  let release, started = false;
  const f = await fixture(t, { requestAccount: async (_, kind, options) => {
    if (kind === 'profile') return profile(options.token.includes('-B-') ? 'B' : 'A');
    started = true; await new Promise(resolve => { release = resolve; });
    return { access_token: 'INVENTED-B-ACCESS', refresh_token: 'INVENTED-B-REFRESH', expires_in: 3600 };
  } });
  await f.start(); const verifier = 'fixture-code-verifier', state = 'fixture-state';
  await f.control('/login-begin', { sessionId: 'fixture-session', oauthState: state,
    oauthChallenge: createHash('sha256').update(verifier).digest('base64url') });
  const exchange = f.request('/v1/oauth/token', { host: 'platform.claude.com', body: JSON.stringify({
    grant_type: 'authorization_code', code: 'INVENTED-CODE', state, code_verifier: verifier,
  }) }); exchange.catch(() => {}); await until(() => started);
  let closed = false; const closing = f.service.close().then(() => { closed = true; });
  await delay(20); assert.equal(closed, false); release(); await closing; await exchange.catch(() => {});
  assert(f.writes.some(value => value.value.oauthResponse?.refresh_token === 'INVENTED-B-REFRESH'));
});

for (const failure of ['missing', 'expired']) test(`a ${failure} current grant keeps login available while model requests fail closed`, async t => {
  const f = await fixture(t, { requestAccount: async (_, kind, options) => {
    if (kind === 'refresh') throw new Error('login_required');
    if (kind === 'profile') return profile(options.token.includes('-B-') ? 'B' : 'A');
    return { access_token: 'INVENTED-B-ACCESS', refresh_token: 'INVENTED-B-REFRESH', expires_in: 3600 };
  } });
  const credentials = failure === 'missing' ? {} : grant('A');
  if (failure === 'expired') credentials.claudeAiOauth.expiresAt = 0;
  f.values.set(f.profiles.A.configDirectory, credentials);
  await f.start();
  assert.equal((await f.control('/status')).value.ready, false);
  const registration = await f.control('/register');
  assert.equal(registration.status, 200);
  assert.equal(registration.value.directory, runtimeDirectory(f.config));
  assert.equal((await f.model()).status, 503);
  assert.equal(f.calls.length, 0, 'no upstream model request before a verified replacement');

  const state = `fixture-${failure}-state`, verifier = `fixture-${failure}-verifier`;
  const begun = await f.control('/login-begin', { sessionId: `fixture-${failure}-session`, oauthState: state,
    oauthChallenge: createHash('sha256').update(verifier).digest('base64url') });
  assert.equal(begun.status, 200);
  assert.equal((await f.model()).status, 503, 'an intent alone cannot admit model traffic');
  const exchange = await f.request('/v1/oauth/token', { host: 'platform.claude.com', body: JSON.stringify({
    grant_type: 'authorization_code', code: 'INVENTED-CODE', state, code_verifier: verifier,
  }) });
  assert.equal(exchange.status, 200);
  f.values.set(runtimeDirectory(f.config), grant('B'));
  assert.equal((await f.control('/login-finished')).status, 200);
  assert.equal((await f.control('/status')).value.ready, true);
  assert.equal((await registryState(f.config)).selected, 'account-b');
  assert.equal((await f.model()).status, 200);
  assertOwner(f.calls.at(-1), 'B');
  assert.equal(f.values.get(runtimeDirectory(f.config)).claudeAiOauth.refreshToken, undefined);
  assert.equal(f.values.get(f.profiles.B.configDirectory).claudeAiOauth.refreshToken, 'INVENTED-B-REFRESH');
});

test('missing-current startup cannot import an unobserved grant from the native mirror', async t => {
  const f = await fixture(t); f.values.set(f.profiles.A.configDirectory, {}); await f.start();
  await f.control('/login-begin', { sessionId: 'fixture-session', oauthState: 'fixture-state',
    oauthChallenge: createHash('sha256').update('fixture-verifier').digest('base64url') });
  f.values.set(runtimeDirectory(f.config), grant('B'));
  assert.equal((await f.control('/login-finished')).status, 409);
  assert.equal((await f.model()).status, 503);
  assert.equal(f.calls.length, 0);
  assert.equal((await registryState(f.config)).selected, 'account-a');
  assert.equal((await f.control('/status')).value.ready, false);
});

test('file-busy mirror failure cannot send B or falsely report old A as ready', async t => {
  const f = await fixture(t); await f.start(); f.blockMirror = true;
  const switched = await f.control('/select', { name: 'account-b' });
  assert.equal(switched.status, 409); assert.equal(switched.value.reason, 'runtime_unavailable');
  assert.equal((await f.control('/status')).value.ready, false);
  assert.equal((await f.model()).status, 503); assert.equal(f.calls.length, 0);
  f.blockMirror = false; assert.equal((await f.model()).status, 200); assertOwner(f.calls[0], 'B');
  assert.equal((await f.control('/status')).value.ready, true);
});

test('startup network failure leaves no live lock or readiness record', async t => {
  const f = await fixture(t); await assert.rejects(f.start({ network: async () => { throw new Error('network_unavailable'); } }), /network_unavailable/);
  await assert.rejects(fs.stat(path.join(runtimeRoot(f.config), 'service.lock')), { code: 'ENOENT' });
  await assert.rejects(fs.stat(path.join(runtimeRoot(f.config), 'service.json')), { code: 'ENOENT' });
});

test('failed initial lock write closes its handle and removes only the newly created lock', async t => {
  const f = await fixture(t), open = fs.open;
  let handleClosed = false;
  t.mock.method(fs, 'open', async (file, ...args) => {
    const handle = await open(file, ...args);
    if (file !== path.join(runtimeRoot(f.config), 'service.lock')) return handle;
    return { writeFile: async () => { throw Object.assign(new Error('fixture_write_failed'), { code: 'EIO' }); },
      close: async () => { handleClosed = true; await handle.close(); } };
  });
  await assert.rejects(f.start(), { code: 'EIO' }); assert.equal(handleClosed, true);
  await assert.rejects(fs.stat(path.join(runtimeRoot(f.config), 'service.lock')), { code: 'ENOENT' });
});

test('service close removes its own signal handlers so later starts do not accumulate exit callbacks', async t => {
  const f = await fixture(t);
  const before = new Map(['SIGTERM', 'SIGINT'].map(signal => [signal, process.listeners(signal)]));
  await f.start();
  for (const [signal, listeners] of before) assert.equal(process.listeners(signal).length, listeners.length + 1);
  await f.service.close();
  for (const [signal, listeners] of before) assert.deepEqual(process.listeners(signal), listeners);
});

for (const event of ['message', 'disconnect']) test(`supervisor ${event} awaits cleanup, removes IPC handlers and exits cleanly`, async t => {
  const descriptor = Object.getOwnPropertyDescriptor(process, 'send');
  Object.defineProperty(process, 'send', { value: () => {}, configurable: true, writable: true });
  t.after(() => descriptor ? Object.defineProperty(process, 'send', descriptor) : delete process.send);
  const exits = []; t.mock.method(process, 'exit', code => { exits.push(code); });
  const before = new Map(['message', 'disconnect'].map(name => [name, process.listeners(name)]));
  const f = await fixture(t); await f.start();
  process.emit(event, { type: 'runtime-supervisor-stop' });
  await until(() => exits.length === 1);
  assert.deepEqual(exits, [0]);
  await assert.rejects(fs.stat(path.join(runtimeRoot(f.config), 'service.lock')), { code: 'ENOENT' });
  for (const [name, listeners] of before) assert.deepEqual(process.listeners(name), listeners);
});

test('certificate validation rejects a mismatched private key and a future validFrom', async t => {
  const f = await fixture(t);
  const firstDir = path.join(f.root, 'cert-one'), secondDir = path.join(f.root, 'cert-two');
  await fs.mkdir(firstDir, { mode: 0o700 }); await fs.mkdir(secondDir, { mode: 0o700 });
  const first = await runtimeCertificate(firstDir), second = await runtimeCertificate(secondDir);
  await fs.writeFile(path.join(firstDir, 'api-key.pem'), second.key, { mode: 0o600 });
  await assert.rejects(runtimeCertificate(firstDir), /runtime_certificate_expired/);
  await fs.writeFile(path.join(firstDir, 'api-key.pem'), first.key, { mode: 0o600 });
  const notBefore = new Date(new X509Certificate(first.cert).validFrom).getTime();
  t.mock.method(Date, 'now', () => notBefore - 1000);
  await assert.rejects(runtimeCertificate(firstDir), /runtime_certificate_expired/);
});
