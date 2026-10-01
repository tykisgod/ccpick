import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import { randomBytes } from 'node:crypto';
import { setTimeout as delay } from 'node:timers/promises';
import { RuntimeVault, credentialStore, credentialsFromOAuthResponse } from '../runtime-vault.mjs';
import { createProfile, bindAccount, atomicJson, readJson, getProfile, globalConfigFile } from '../core.mjs';

const UUID_A = '00000000-0000-4000-8000-000000000001';
const UUID_B = '00000000-0000-4000-8000-000000000002';
const ORG_A = '00000000-0000-4000-8000-000000000003';
const ORG_B = '00000000-0000-4000-8000-000000000004';
const proxy = 'http://127.0.0.1:11811';
const credentials = (label, expiresAt = Date.now() + 3_600_000) => ({ preserved: 'other-credential-field', claudeAiOauth: {
  accessToken: `INVENTED-ACCESS-${label}`, refreshToken: `INVENTED-REFRESH-${label}`, expiresAt,
  scopes: ['user:inference', 'user:profile'], subscriptionType: 'max', rateLimitTier: 'fixture',
} });
const remote = (label = 'A') => ({ account: { uuid: label === 'A' ? UUID_A : UUID_B,
  email: `${label.toLowerCase()}@example.com`, display_name: `Fixture ${label}` },
  organization: { uuid: label === 'A' ? ORG_A : ORG_B, organization_type: 'fixture' } });

async function fixture(t) {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), 'ccpick-vault-test-'));
  if (process.platform !== 'win32') await fs.chmod(root, 0o700);
  t.after(() => fs.rm(root, { recursive: true, force: true }));
  const config = { dataRoot: path.join(root, 'data'), platform: process.platform, serviceRoot: root,
    native: path.join(root, 'never-run-native'), browser: path.join(root, 'never-run-browser') };
  await fs.mkdir(config.dataRoot, { mode: 0o700 });
  const values = new Map(), writes = [], requests = [];
  const store = { read: async directory => structuredClone(values.get(directory) ?? {}),
    write: async (directory, value) => { values.set(directory, structuredClone(value)); writes.push({ directory, value: structuredClone(value) }); } };
  async function profile(label = 'A', { bound = true } = {}) {
    let p = await createProfile(config, { name: `account-${label.toLowerCase()}`, email: `${label.toLowerCase()}@example.com` }, {
      initialize: async (_, target) => atomicJson(globalConfigFile(target), {
        userID: randomBytes(32).toString('hex'), machineID: randomBytes(32).toString('hex'), preference: 'kept',
      }),
    });
    const source = remote(label);
    if (bound) {
      const ids = await readJson(globalConfigFile(p));
      await atomicJson(globalConfigFile(p), { ...ids, oauthAccount: { accountUuid: source.account.uuid,
        organizationUuid: source.organization.uuid, emailAddress: source.account.email } });
      await bindAccount(config, p, { loggedIn: true, uuid: source.account.uuid, email: source.account.email });
      p = await getProfile(config, p.name);
    }
    values.set(p.configDirectory, credentials(label)); return p;
  }
  const request = async (selectedProxy, kind, options) => {
    requests.push({ selectedProxy, kind, options });
    if (kind === 'profile') return remote(options.token.includes('-B') ? 'B' : 'A');
    return { access_token: 'INVENTED-ACCESS-A-NEW', refresh_token: 'INVENTED-REFRESH-A-NEW', expires_in: 3600, scope: 'user:inference user:profile' };
  };
  return { root, config, values, writes, requests, store, profile, request,
    vault: overrides => new RuntimeVault(config, proxy, { store, request, ...overrides }) };
}

test('load verifies bearer/account/org through injected residential request and keeps stable IDs', async t => {
  const f = await fixture(t), p = await f.profile();
  const before = await fs.readFile(globalConfigFile(p));
  const result = await f.vault().loadAccount(p.name);
  assert.equal(result.accountUuid, UUID_A); assert.equal(result.credentials.claudeAiOauth.accessToken, 'INVENTED-ACCESS-A');
  assert.deepEqual(await fs.readFile(globalConfigFile(p)), before); assert.equal(f.writes.length, 0);
  assert.equal(f.requests[0].selectedProxy, proxy); assert.equal(f.requests[0].kind, 'profile');
});

test('parallel callers of one expired account refresh once and reuse the verified new grant', async t => {
  const f = await fixture(t), p = await f.profile();
  f.values.set(p.configDirectory, credentials('A', Date.now() - 1000));
  const vault = f.vault({ request: async (...args) => { await delay(2); return f.request(...args); } });
  const results = await Promise.all(Array.from({ length: 12 }, () => vault.loadAccount(p.name)));
  assert(results.every(result => result.credentials.claudeAiOauth.accessToken === 'INVENTED-ACCESS-A-NEW'));
  assert.equal(f.requests.filter(value => value.kind === 'refresh').length, 1);
  assert.equal(f.requests.filter(value => value.kind === 'profile').length, 1); assert.equal(f.writes.length, 1);
  const saved = f.values.get(p.configDirectory); assert.equal(saved.preserved, 'other-credential-field');
  assert.equal(saved.claudeAiOauth.subscriptionType, 'max');
});

test('rotated refresh token is durable before profile failure and later retry does not rotate again', async t => {
  const f = await fixture(t), p = await f.profile();
  f.values.set(p.configDirectory, credentials('A', Date.now() - 1000));
  let failProfile = true;
  const vault = f.vault({ request: async (...args) => {
    if (args[1] === 'profile' && failProfile) { assert.equal(f.writes.length, 1); throw new Error('network_unavailable'); }
    return f.request(...args);
  } });
  await assert.rejects(vault.loadAccount(p.name), /network_unavailable/);
  assert.equal(f.values.get(p.configDirectory).claudeAiOauth.refreshToken, 'INVENTED-REFRESH-A-NEW');
  failProfile = false; assert.equal((await vault.loadAccount(p.name)).accountUuid, UUID_A);
  assert.equal(f.requests.filter(value => value.kind === 'refresh').length, 1);
});

test('refresh account or organization mismatch never produces a request snapshot or verification cache', async t => {
  const f = await fixture(t), p = await f.profile();
  f.values.set(p.configDirectory, credentials('A', Date.now() - 1000));
  let profileCalls = 0;
  const vault = f.vault({ request: async (...args) => {
    if (args[1] === 'profile') { profileCalls++; return { ...remote('A'), organization: remote('B').organization }; }
    return f.request(...args);
  } });
  await assert.rejects(vault.loadAccount(p.name), /wrong_account/);
  await assert.rejects(vault.loadAccount(p.name), /wrong_account/);
  assert.equal(profileCalls, 2);
});

test('legacy concurrent credential write causes CAS rejection without overwriting that writer', async t => {
  const f = await fixture(t), p = await f.profile();
  f.values.set(p.configDirectory, credentials('A', Date.now() - 1000));
  const concurrent = credentials('A-EXTERNAL');
  const vault = f.vault({ request: async (...args) => {
    if (args[1] === 'refresh') f.values.set(p.configDirectory, concurrent);
    return f.request(...args);
  } });
  await assert.rejects(vault.loadAccount(p.name), /credentials_changed/);
  assert.deepEqual(f.values.get(p.configDirectory), concurrent); assert.equal(f.writes.length, 0);
});

test('different accounts stay independently serialized and credentials never cross vault directories', async t => {
  const f = await fixture(t), pa = await f.profile('A'), pb = await f.profile('B');
  f.values.set(pa.configDirectory, credentials('A', 0)); f.values.set(pb.configDirectory, credentials('B', 0));
  let refreshCount = 0, release;
  const bothRefreshing = new Promise(resolve => { release = resolve; });
  const vault = f.vault({ request: async (_, kind, options) => {
    if (kind === 'profile') return remote(options.token.includes('-B') ? 'B' : 'A');
    refreshCount++; if (refreshCount === 2) release(); await bothRefreshing;
    const label = options.body.refresh_token.endsWith('-B') ? 'B' : 'A';
    return { access_token: `INVENTED-ACCESS-${label}-NEW`, refresh_token: `INVENTED-REFRESH-${label}-NEW`, expires_in: 3600 };
  } });
  const result = await Promise.all([vault.loadAccount(pa.name), vault.loadAccount(pb.name)]);
  assert.deepEqual(result.map(value => value.accountUuid), [UUID_A, UUID_B]);
  assert.equal(f.values.get(pa.configDirectory).claudeAiOauth.accessToken, 'INVENTED-ACCESS-A-NEW');
  assert.equal(f.values.get(pb.configDirectory).claudeAiOauth.accessToken, 'INVENTED-ACCESS-B-NEW');
});

test('missing RT on expired grant requires login and does not attempt refresh', async t => {
  const f = await fixture(t), p = await f.profile();
  const value = credentials('A', 0); delete value.claudeAiOauth.refreshToken; f.values.set(p.configDirectory, value);
  await assert.rejects(f.vault().loadAccount(p.name), /login_required/); assert.equal(f.requests.length, 0);
});

test('cache is keyed by bearer fingerprint and changed credentials are verified again', async t => {
  const f = await fixture(t), p = await f.profile(), vault = f.vault();
  await vault.loadAccount(p.name); await vault.loadAccount(p.name);
  f.values.set(p.configDirectory, credentials('A-NEW')); await vault.loadAccount(p.name);
  assert.equal(f.requests.filter(value => value.kind === 'profile').length, 2);
});

test('import existing account verifies real bearer and preserves its IDs and other preferences', async t => {
  const f = await fixture(t), p = await f.profile(), vault = f.vault();
  const before = await readJson(globalConfigFile(p));
  const result = await vault.importGrant({ credentials: credentials('A-NEW'), remote: { accountUuid: UUID_A } });
  const after = await readJson(globalConfigFile(p));
  assert.equal(result.name, p.name); assert.equal(after.userID, before.userID); assert.equal(after.machineID, before.machineID);
  assert.equal(after.preference, 'kept'); assert.equal(after.oauthAccount.accountUuid, UUID_A);
  assert.equal(f.values.get(p.configDirectory).claudeAiOauth.accessToken, 'INVENTED-ACCESS-A-NEW');
});

test('enrollment only merges OAuth and keeps destination MCP credentials and optional native metadata', async t => {
  const f = await fixture(t), p = await f.profile(), vault = f.vault();
  f.values.set(p.configDirectory, { ...credentials('A'), mcpOAuth: { server: 'destination-only' } });
  const minimal = { claudeAiOauth: { accessToken: 'INVENTED-ACCESS-A-NEW', refreshToken: 'INVENTED-REFRESH-A-NEW',
    expiresAt: Date.now() + 3_600_000 }, preserved: 'wrong-runtime-value', mcpOAuth: { server: 'another-account' } };
  const receipt = { id: 'fixture-import', capturedAt: new Date().toISOString() };
  await vault.importGrant({ credentials: minimal, receipt, recovery: true });
  let saved = f.values.get(p.configDirectory);
  assert.equal(saved.preserved, 'other-credential-field'); assert.deepEqual(saved.mcpOAuth, { server: 'destination-only' });
  assert.equal(saved.claudeAiOauth.subscriptionType, 'max'); assert.equal(saved.claudeAiOauth.rateLimitTier, 'fixture');
  assert.deepEqual(saved.claudeAiOauth.scopes, ['user:inference', 'user:profile']);
  await vault.importGrant({ credentials: { ...minimal, claudeAiOauth: { ...minimal.claudeAiOauth,
    subscriptionType: 'pro', rateLimitTier: null, scopes: ['user:profile'] } }, receipt });
  saved = f.values.get(p.configDirectory);
  assert.equal(saved.claudeAiOauth.subscriptionType, 'pro'); assert.equal(saved.claudeAiOauth.rateLimitTier, null);
  assert.deepEqual(saved.claudeAiOauth.scopes, ['user:profile']);
});

test('journal receipt replay cannot replace a newer login or its organization binding', async t => {
  const f = await fixture(t), p = await f.profile(), vault = f.vault();
  await vault.importGrant({ credentials: credentials('A-LATER'), receipt: { id: 'later', capturedAt: '2026-10-01T02:00:00.000Z' } });
  const before = structuredClone(f.values.get(p.configDirectory)), beforeIds = await readJson(globalConfigFile(p));
  await vault.importGrant({ credentials: credentials('A-EARLIER'), receipt: { id: 'earlier', capturedAt: '2026-10-01T01:00:00.000Z' }, recovery: true });
  assert.deepEqual(f.values.get(p.configDirectory), before);
  assert.deepEqual(await readJson(globalConfigFile(p)), beforeIds);
});

test('a repeated journal receipt cannot roll back a subsequently refreshed grant', async t => {
  const f = await fixture(t), p = await f.profile(), vault = f.vault();
  const receipt = { id: 'same-login', capturedAt: '2026-10-01T02:00:00.000Z' };
  await vault.importGrant({ credentials: credentials('A', 0), receipt, recovery: true });
  await vault.loadAccount(p.name);
  const before = structuredClone(f.values.get(p.configDirectory));
  await vault.importGrant({ credentials: credentials('A'), receipt, recovery: true });
  await vault.importGrant({ credentials: credentials('A'), receipt });
  assert.deepEqual(f.values.get(p.configDirectory), before);
  assert.equal(before.claudeAiOauth.refreshToken, 'INVENTED-REFRESH-A-NEW');
});

test('native metadata can finish after RT-only rotation without rolling back the refresh token', async t => {
  const f = await fixture(t), p = await f.profile(), vault = f.vault({ request: async (...args) => args[1] === 'refresh'
    ? { access_token: 'INVENTED-ACCESS-A', refresh_token: 'INVENTED-REFRESH-A-ROTATED', expires_in: 3600 }
    : f.request(...args) });
  const receipt = { id: 'same-login', capturedAt: '2026-10-01T02:00:00.000Z' };
  await vault.importGrant({ credentials: credentials('A', 0), receipt, recovery: true });
  await vault.loadAccount(p.name);
  const native = credentials('A'); native.claudeAiOauth.subscriptionType = 'pro';
  await vault.importGrant({ credentials: native, receipt });
  const saved = f.values.get(p.configDirectory).claudeAiOauth;
  assert.equal(saved.accessToken, 'INVENTED-ACCESS-A');
  assert.equal(saved.refreshToken, 'INVENTED-REFRESH-A-ROTATED');
  assert.equal(saved.subscriptionType, 'pro');
});

test('raw OAuth recovery preserves the exchange expiry and never invents missing grant fields', () => {
  const result = credentialsFromOAuthResponse({ access_token: 'INVENTED-ACCESS', refresh_token: 'INVENTED-REFRESH',
    expires_in: 3600 }, '2026-10-01T00:00:00.000Z');
  assert.equal(result.claudeAiOauth.expiresAt, Date.parse('2026-10-01T01:00:00.000Z'));
  assert.equal(result.claudeAiOauth.scopes, undefined); assert.equal(result.claudeAiOauth.subscriptionType, undefined);
  assert.throws(() => credentialsFromOAuthResponse({ access_token: 'INVENTED', expires_in: -1 }, new Date().toISOString()), /auth_unverified/);
});

test('import rejects supplied account claim that disagrees with bearer without changing any vault', async t => {
  const f = await fixture(t), p = await f.profile();
  await assert.rejects(f.vault().importGrant({ credentials: credentials('B'), remote: { accountUuid: UUID_A } }), /wrong_account/);
  assert.equal(f.writes.length, 0); assert.equal((await getProfile(f.config, p.name)).account.uuid, UUID_A);
});

test('an interrupted unbound enrollment is completed using the reserved profile and original IDs', async t => {
  const f = await fixture(t), p = await f.profile('A', { bound: false });
  const before = await readJson(globalConfigFile(p));
  await atomicJson(globalConfigFile(p), { ...before, oauthAccount: { accountUuid: UUID_A,
    organizationUuid: ORG_A, emailAddress: 'a@example.com' } });
  const result = await f.vault().importGrant({ credentials: credentials('A-NEW'), remote: { accountUuid: UUID_A } });
  assert.equal(result.name, p.name); assert.equal((await getProfile(f.config, p.name)).account.uuid, UUID_A);
  assert.equal((await readJson(globalConfigFile(p))).userID, before.userID);
  assert.equal((await fs.readdir(f.config.dataRoot)).length, 1);
});

test('Mac credential adapter failure does not fall back to a plaintext credential file', async t => {
  const f = await fixture(t), p = await f.profile();
  const store = credentialStore({ ...f.config, platform: 'darwin', python: process.execPath });
  await assert.rejects(store.write(p.configDirectory, credentials('A')), /credential_store_unavailable/);
  await assert.rejects(fs.stat(path.join(p.configDirectory, '.credentials.json')), { code: 'ENOENT' });
});
