import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import { createHash, randomBytes, randomUUID } from 'node:crypto';
import { createProfile, atomicJson } from '../core.mjs';
import { addAccount, portableCommand, registerBrowser } from '../portable.mjs';
import { prepareInputHistory } from '../input-history.mjs';
import { serveRuntime } from '../runtime-service.mjs';
import { runtimeServiceInfo, runtimeRpc } from '../runtime-client.mjs';

async function fixture(t) {
  const root = await fs.realpath(await fs.mkdtemp(path.join(os.tmpdir(), 'ccpick-portable-test-')));
  t.after(() => fs.rm(root, { recursive: true, force: true }));
  const config = { dataRoot: path.join(root, 'data'), serviceRoot: root, platform: process.platform,
    native: '/synthetic/native', browser: '/synthetic/browser', seamlessAccounts: true,
    upstreamProxy: 'http://127.0.0.1:18123' };
  await fs.mkdir(config.dataRoot, { mode: 0o700 });
  const profiles = [];
  for (const name of ['account-a', 'account-b']) profiles.push(await createProfile(config, { name, email: `${name}@example.com` }, {
    initialize: async (_, p) => atomicJson(path.join(p.configDirectory, '.claude.json'), {
      userID: randomBytes(32).toString('hex'), machineID: randomBytes(32).toString('hex') }),
  }));
  await atomicJson(path.join(root, 'state.json'), { version: 2, enabled: true, selected: 'account-a', selectedAt: 'generation-a' });
  return { root, config, profiles };
}

test('manual selection remains available while automatic CAS refuses a manual-only account', async t => {
  const { config, profiles: [, b] } = await fixture(t);
  await portableCommand(config, 'auto-policy', [b.name, 'off']);
  const calls = [];
  const request = async (_config, action, payload) => { calls.push({ action, payload }); return { ok: true }; };
  await portableCommand(config, 'select', [b.name], { request });
  assert.equal(calls[0].action, 'select');
  await assert.rejects(portableCommand(config, 'select-guarded', [b.name, 'account-a', 'generation-a'], { request }), /account_manual_only/);
  assert.equal(calls.length, 1);
});

test('browser registration binds the native scope and PKCE without reopening a browser or leaking URL', async t => {
  const { root, config } = await fixture(t);
  const environment = { CCPICK_ACCOUNT_RUNTIME: '1', CCPICK_RUNTIME_SCOPE: 'default', CCPICK_RUNTIME_CLIENT_ID: randomUUID(),
    CLAUDE_CONFIG_DIR: path.join(root, 'active-runtime', 'claude') };
  const calls = [];
  const request = async (_config, action, payload) => { calls.push({ action, payload }); return { ok: true }; };
  const url = 'https://claude.ai/oauth/authorize?state=synthetic-state&client_id=synthetic-client&code_challenge=synthetic-challenge&redirect_uri=http%3A%2F%2Flocalhost%3A1234%2Fcallback';
  assert.deepEqual(await registerBrowser(config, url, environment, { request }), { ok: true });
  assert.deepEqual(calls, [{ action: 'login-begin', payload: { scope: 'default', sessionId: environment.CCPICK_RUNTIME_CLIENT_ID,
    oauthState: 'synthetic-state', oauthChallenge: 'synthetic-challenge' } }]);
  for (const bad of ['https://untrusted.example.com/oauth/authorize?state=fixture', url + '&state=duplicate',
    'https://platform.claude.com/oauth/authorize?state=x&client_id=c&code_challenge=p&redirect_uri=r'])
    await assert.rejects(registerBrowser(config, bad, environment, { request }), /invalid_login_url|unsupported_login_method/);
  assert.equal(calls.length, 1);
  const workerEnvironment = { ...environment }; delete workerEnvironment.CCPICK_RUNTIME_CLIENT_ID;
  await registerBrowser(config, url, workerEnvironment, { request });
  assert.match(calls[1].payload.sessionId, /^[a-f0-9-]{36}$/);
  assert.equal(calls[1].payload.scope, 'default');
});

test('separate enrollment keeps the current default and its verified new account', async t => {
  const { config, profiles: [a, b] } = await fixture(t);
  const bound = { ...b, account: { uuid: 'synthetic-uuid', email: b.email } };
  const { root, configDirectory, ...manifest } = bound;
  await atomicJson(path.join(root, 'profile.json'), manifest);
  const calls = [];
  const result = await addAccount(config, { email: b.email }, {
    create: async () => bound, launch: async () => 0,
    request: async (_config, action, payload) => { calls.push({ action, payload }); throw new Error('selection_changed'); },
  });
  assert.equal(result.ok, true); assert.equal(result.selected, false);
  assert.deepEqual(calls, []);
  assert.equal((await portableCommand(config, 'status')).selected, a.name);
});

test('first enrollment does not overwrite a manual selection made during login', async t => {
  const { root, config, profiles: [a, b] } = await fixture(t);
  const bound = { ...b, account: { uuid: 'synthetic-uuid', email: b.email } };
  const { root: profileRoot, configDirectory, ...manifest } = bound;
  await atomicJson(path.join(profileRoot, 'profile.json'), manifest);
  await atomicJson(path.join(root, 'state.json'), { version: 2, enabled: true, selected: null, selectedAt: null });
  const calls = [];
  const result = await addAccount(config, { email: b.email }, {
    create: async () => bound,
    launch: async () => {
      await atomicJson(path.join(root, 'state.json'), { version: 2, enabled: true,
        selected: a.name, selectedAt: 'later-manual' });
      return 0;
    },
    request: async (_config, action, payload) => { calls.push({ action, payload }); throw new Error('selection_changed'); },
  });
  assert.equal(result.ok, true); assert.equal(result.selected, false);
  assert.equal(calls[0].action, 'select-guarded');
  assert.equal(calls[0].payload.name, b.name);
  assert.equal(calls[0].payload.expectedState.selected, b.name);
  assert.equal((await portableCommand(config, 'status')).selected, a.name);
});

test('shared old input imports referenced paste payloads without copying identity or authentication', async t => {
  const { root, config, profiles: [target] } = await fixture(t);
  const homeVariable = process.platform === 'win32' ? 'USERPROFILE' : 'HOME';
  const previous = process.env[homeVariable];
  const home = path.join(root, 'synthetic-home'), legacy = path.join(home, '.claude');
  await fs.mkdir(path.join(legacy, 'paste-cache'), { recursive: true, mode: 0o700 });
  process.env[homeVariable] = home;
  try {
    const pasted = 'synthetic clipboard contents', contentHash = createHash('sha256').update(pasted).digest('hex').slice(0, 16);
    const entry = { display: '[Pasted text #1]', timestamp: 100, project: path.join(root, 'synthetic-project'),
      sessionId: randomUUID(), pastedContents: { 1: { id: 1, type: 'text', contentHash } } };
    await fs.writeFile(path.join(legacy, 'history.jsonl'), JSON.stringify(entry) + '\n', { mode: 0o600 });
    await fs.writeFile(path.join(legacy, 'paste-cache', contentHash + '.txt'), pasted, { mode: 0o600 });
    await fs.writeFile(path.join(legacy, '.credentials.json'), 'synthetic credentials: never import', { mode: 0o600 });
    const before = await fs.readFile(path.join(target.configDirectory, '.claude.json'));
    const result = await prepareInputHistory({ ...config, legacyInputHistory: true }, target);
    assert.equal(result.imported, 1); assert.equal(result.copiedPastes, 1);
    assert.equal(await fs.readFile(path.join(target.configDirectory, 'paste-cache', contentHash + '.txt'), 'utf8'), pasted);
    assert.deepEqual(await fs.readFile(path.join(target.configDirectory, '.claude.json')), before);
    await assert.rejects(fs.stat(path.join(target.configDirectory, '.credentials.json')), { code: 'ENOENT' });
    assert.equal((await prepareInputHistory({ ...config, legacyInputHistory: true }, target)).imported, 0);
  } finally {
    if (previous === undefined) delete process.env[homeVariable]; else process.env[homeVariable] = previous;
  }
});

test('a clean unbound account can start the service, register an enrollment scope and begin native login offline', async t => {
  const { root, config, profiles: [a, b] } = await fixture(t);
  await atomicJson(path.join(root, 'state.json'), { version: 2, enabled: true,
    selected: a.name, selectedAt: 'generation-a', bootstrapEmail: 'account-a@example.com' });
  let externalCalls = 0;
  const values = new Map();
  const service = await serveRuntime(config, {
    network: async () => ({ apiMode: 'official', proxy: 'http://127.0.0.1:1' }),
    store: { read: async directory => structuredClone(values.get(directory) ?? {}),
      write: async (directory, value) => values.set(directory, structuredClone(value)) },
    requestAccount: async () => { externalCalls++; throw new Error('fixture_external_request_refused'); },
    brokerTransport: async () => { externalCalls++; throw new Error('fixture_external_request_refused'); },
  });
  t.after(() => service.close());
  const channel = await runtimeServiceInfo(config);
  const registration = await runtimeRpc(channel, 'register', { scope: b.name });
  assert.equal(registration.scope, b.name);
  const intent = await runtimeRpc(channel, 'login-begin', { scope: b.name, sessionId: randomUUID(),
    oauthState: randomBytes(16).toString('hex'), oauthChallenge: randomBytes(32).toString('base64url') });
  assert.equal(intent.ok, true); assert.equal(typeof intent.id, 'string');
  assert.equal((await runtimeRpc(channel, 'status', { scope: b.name })).ready, false);
  assert.equal(externalCalls, 0);
});
