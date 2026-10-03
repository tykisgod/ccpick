import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import { randomBytes } from 'node:crypto';
import { setTimeout as delay } from 'node:timers/promises';
import { RuntimeVault, credentialStore, credentialsFromOAuthResponse } from '../runtime-vault.mjs';
import { ActiveRuntime } from '../active-runtime.mjs';
import { createProfile, bindAccount, atomicJson, readJson, getProfile, globalConfigFile, digest } from '../core.mjs';

const UUID_A = '00000000-0000-4000-8000-000000000001';
const UUID_B = '00000000-0000-4000-8000-000000000002';
const ORG_A = '00000000-0000-4000-8000-000000000003';
const ORG_B = '00000000-0000-4000-8000-000000000004';
const proxy = 'http://127.0.0.1:11811';
const proxyB = 'http://127.0.0.1:11911';
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

test('account authentication, refresh and bearer routing use the same household', async t => {
  const f = await fixture(t), accountA = await f.profile('A'), accountB = await f.profile('B');
  const route = household => Object.freeze({ household, proxy: household === 'A' ? proxy : proxyB, revision: household });
  const egress = { defaultGroup: async () => 'A', group: async household => route(household),
    account: async name => route(name === accountB.name ? 'B' : 'A') };
  const vault = f.vault({ egress });
  await Promise.all([vault.loadAccount(accountA.name), vault.loadAccount(accountB.name)]);
  assert.equal(f.requests.find(item => item.options.token === 'INVENTED-ACCESS-A').selectedProxy, proxy);
  assert.equal(f.requests.find(item => item.options.token === 'INVENTED-ACCESS-B').selectedProxy, proxyB);
  assert.equal((await vault.bearerEgress('INVENTED-ACCESS-B')).proxy, proxyB);
  await assert.rejects(vault.bearerEgress('INVENTED-UNKNOWN'), /auth_unverified/);
  f.values.set(accountA.configDirectory, credentials('A', Date.now() - 1));
  egress.account = async () => route('B');
  const refreshed = await vault.loadAccount(accountA.name);
  assert.equal(refreshed.egress.household, 'B');
  for (const item of f.requests.filter(item => item.kind === 'refresh' || item.options.token?.endsWith('-NEW')))
    assert.equal(item.selectedProxy, proxyB);
  assert.equal((await vault.bearerEgress('INVENTED-ACCESS-A')).proxy, proxyB);
});

test('moving an account invalidates verification cache without changing its IDs', async t => {
  const f = await fixture(t), p = await f.profile(); let household = 'A';
  const egress = { account: async () => ({ household, proxy: household === 'A' ? proxy : proxyB, revision: household }) };
  const vault = f.vault({ egress }), before = await fs.readFile(globalConfigFile(p));
  await vault.loadAccount(p.name); household = 'B'; await vault.loadAccount(p.name);
  assert.deepEqual(f.requests.map(item => item.selectedProxy), [proxy, proxyB]);
  assert.deepEqual(await fs.readFile(globalConfigFile(p)), before);
});

test('verified old bearer survives refresh and vault reconstruction, following the current household', async t => {
  const f = await fixture(t), p = await f.profile(), oldExpiry = Date.now() + 30_000;
  const file = path.join(f.root, 'active-runtime', 'bearer-bindings.json');
  f.values.set(p.configDirectory, credentials('A', oldExpiry));
  let household = 'A';
  const egress = { account: async () => ({ household, proxy: household === 'A' ? proxy : proxyB, revision: household }) };
  const vault = f.vault({ egress, request: async (...args) => {
    if (args[1] === 'refresh') {
      const saved = await readJson(file);
      assert.equal(saved.bindings[digest('INVENTED-ACCESS-A')].profileId, p.name);
      assert.equal(saved.bindings[digest('INVENTED-ACCESS-A')].accountHash, digest(UUID_A));
    }
    return f.request(...args);
  } });
  await vault.loadAccount(p.name);
  const { RuntimeVault: RestartedVault } = await import(`../runtime-vault.mjs?restart=${randomBytes(8).toString('hex')}`);
  const rebuilt = new RestartedVault(f.config, proxy, { store: f.store, request: f.request, egress });
  assert.equal((await rebuilt.bearerEgress('INVENTED-ACCESS-A')).proxy, proxy);
  household = 'B';
  assert.equal((await rebuilt.bearerEgress('INVENTED-ACCESS-A')).proxy, proxyB);
  assert.equal((await rebuilt.bearerEgress('INVENTED-ACCESS-A-NEW')).proxy, proxyB);
  const saved = await readJson(file), serialized = await fs.readFile(file, 'utf8');
  assert.deepEqual(Object.keys(saved.bindings).sort(), [digest('INVENTED-ACCESS-A'), digest('INVENTED-ACCESS-A-NEW')].sort());
  assert.equal(saved.bindings[digest('INVENTED-ACCESS-A')].expiresAt, oldExpiry);
  assert(!serialized.includes('INVENTED')); assert(!serialized.includes(UUID_A));
  assert.deepEqual(f.requests.map(item => item.kind), ['profile', 'refresh', 'profile']);
  t.mock.method(Date, 'now', () => oldExpiry + 1);
  await assert.rejects(rebuilt.bearerEgress('INVENTED-ACCESS-A'), /auth_unverified/);
  assert.equal((await rebuilt.bearerEgress('INVENTED-ACCESS-A-NEW')).proxy, proxyB);
  assert.equal((await readJson(file)).bindings[digest('INVENTED-ACCESS-A')], undefined);
});

test('first seen expired bearer is never inferred from its local profile', async t => {
  const f = await fixture(t), p = await f.profile();
  f.values.set(p.configDirectory, credentials('A', Date.now() - 1));
  await f.vault().loadAccount(p.name);
  const rebuilt = f.vault(), file = path.join(f.root, 'active-runtime', 'bearer-bindings.json');
  await assert.rejects(rebuilt.bearerEgress('INVENTED-ACCESS-A'), /auth_unverified/);
  assert.equal((await rebuilt.bearerEgress('INVENTED-ACCESS-A-NEW')).proxy, proxy);
  assert.equal((await readJson(file)).bindings[digest('INVENTED-ACCESS-A')], undefined);
  assert.deepEqual(f.requests.map(item => item.kind), ['refresh', 'profile']);
});

test('failed verification after refresh saves the rotated RT but no unverified new bearer binding', async t => {
  const f = await fixture(t), p = await f.profile();
  f.values.set(p.configDirectory, credentials('A', Date.now() + 30_000));
  const request = async (...args) => {
    if (args[1] === 'profile' && args[2].token.endsWith('-NEW')) throw new Error('network_unavailable');
    return f.request(...args);
  };
  await assert.rejects(f.vault({ request }).loadAccount(p.name), /network_unavailable/);
  assert.equal(f.values.get(p.configDirectory).claudeAiOauth.refreshToken, 'INVENTED-REFRESH-A-NEW');
  const saved = await readJson(path.join(f.root, 'active-runtime', 'bearer-bindings.json'));
  assert(saved.bindings[digest('INVENTED-ACCESS-A')]);
  assert.equal(saved.bindings[digest('INVENTED-ACCESS-A-NEW')], undefined);
  assert.equal((await f.vault({ request }).bearerEgress('INVENTED-ACCESS-A')).proxy, proxy);
  await assert.rejects(f.vault({ request }).bearerEgress('INVENTED-ACCESS-A-NEW'), /network_unavailable/);
});

test('independent concurrent vaults merge verified account hashes without overwriting each other', async t => {
  const f = await fixture(t), pa = await f.profile('A'), pb = await f.profile('B');
  const request = async (...args) => { await delay(2); return f.request(...args); };
  await Promise.all([f.vault({ request }).loadAccount(pa.name), f.vault({ request }).loadAccount(pb.name)]);
  const file = path.join(f.root, 'active-runtime', 'bearer-bindings.json'), saved = await readJson(file);
  assert.equal(saved.bindings[digest('INVENTED-ACCESS-A')].profileId, pa.name);
  assert.equal(saved.bindings[digest('INVENTED-ACCESS-B')].profileId, pb.name);
  const rebuilt = f.vault();
  await Promise.all([rebuilt.bearerEgress('INVENTED-ACCESS-A'), rebuilt.bearerEgress('INVENTED-ACCESS-B')]);
  await assert.rejects(fs.stat(file + '.lock'), { code: 'ENOENT' });
  assert.equal(f.requests.length, 2);
});

test('corrupt index and third party valid edits fail closed without overwriting the evidence', async t => {
  const corrupt = await fixture(t), pa = await corrupt.profile();
  await corrupt.vault().loadAccount(pa.name);
  const corruptFile = path.join(corrupt.root, 'active-runtime', 'bearer-bindings.json');
  await atomicJson(corruptFile, { version: 1, bindings: [] });
  const damaged = await fs.readFile(corruptFile);
  await assert.rejects(corrupt.vault().bearerEgress('INVENTED-ACCESS-A'), /runtime_bearer_index_invalid/);
  assert.deepEqual(await fs.readFile(corruptFile), damaged);
  const changed = await fixture(t), p = await changed.profile();
  await changed.vault().loadAccount(p.name);
  const changedFile = path.join(changed.root, 'active-runtime', 'bearer-bindings.json');
  const value = await readJson(changedFile); value.bindings[digest('INVENTED-ACCESS-A')].retainUntil--;
  await atomicJson(changedFile, value); const external = await fs.readFile(changedFile);
  await assert.rejects(changed.vault().bearerEgress('INVENTED-ACCESS-A'), /runtime_bearer_index_changed/);
  assert.deepEqual(await fs.readFile(changedFile), external);
});

test('orphaned unrelated lock cannot block reconstruction; hardlinked binding files are refused', async t => {
  const locked = await fixture(t), p = await locked.profile();
  await fs.mkdir(path.join(locked.root, 'active-runtime'), { mode: 0o700 });
  const lockFile = path.join(locked.root, 'active-runtime', 'bearer-bindings.json.lock');
  const foreign = JSON.stringify({ pid: 123, nonce: 'foreign-owner' });
  await fs.writeFile(lockFile, foreign, { mode: 0o600 });
  await locked.vault().loadAccount(p.name);
  assert.equal((await locked.vault().bearerEgress('INVENTED-ACCESS-A')).proxy, proxy);
  assert.equal(await fs.readFile(lockFile, 'utf8'), foreign);
  const linked = await fixture(t), account = await linked.profile();
  await linked.vault().loadAccount(account.name);
  const file = path.join(linked.root, 'active-runtime', 'bearer-bindings.json'), copy = path.join(linked.root, 'hardlink.json');
  await fs.link(file, copy); const before = await fs.readFile(file);
  await assert.rejects(linked.vault().bearerEgress('INVENTED-ACCESS-A'), /runtime_bearer_index_invalid/);
  assert.deepEqual(await fs.readFile(copy), before);
});

test('a reused profile name cannot route the previous account bearer', async t => {
  const f = await fixture(t), p = await f.profile();
  await f.vault().loadAccount(p.name);
  const { root, configDirectory, ...manifest } = p;
  await atomicJson(path.join(root, 'profile.json'), { ...manifest, account: { uuid: UUID_B, email: 'b@example.com' } });
  await assert.rejects(f.vault().bearerEgress('INVENTED-ACCESS-A'), /auth_unverified/);
});

test('a policy change while authentication is in flight rejects publication', async t => {
  const f = await fixture(t), p = await f.profile(); let household = 'A';
  const egress = { account: async () => ({ household, proxy: household === 'A' ? proxy : proxyB, revision: household }) };
  const vault = f.vault({ egress, request: async (...args) => { const result = await f.request(...args); household = 'B'; return result; } });
  await assert.rejects(vault.loadAccount(p.name), /selection_changed/);
  assert.equal(f.requests[0].selectedProxy, proxy);
});

test('load verifies bearer/account/org through injected residential request and keeps stable IDs', async t => {
  const f = await fixture(t), p = await f.profile();
  const before = await fs.readFile(globalConfigFile(p));
  const result = await f.vault().loadAccount(p.name);
  assert.equal(result.accountUuid, UUID_A); assert.equal(result.credentials.claudeAiOauth.accessToken, 'INVENTED-ACCESS-A');
  assert.deepEqual(await fs.readFile(globalConfigFile(p)), before); assert.equal(f.writes.length, 0);
  assert.equal(f.requests[0].selectedProxy, proxy); assert.equal(f.requests[0].kind, 'profile');
});

test('new unbound profile requires login before any credential read or authentication', async t => {
  const f = await fixture(t), p = await f.profile('A', { bound: false });
  t.mock.method(os, 'homedir', () => f.root);
  let reads = 0;
  const vault = f.vault({ store: { ...f.store, read: async () => { reads++; throw new Error('unexpected_credential_read'); } } });
  await assert.rejects(vault.loadAccount(p.name), /^Error: login_required$/);
  assert.equal(reads, 0); assert.equal(f.requests.length, 0); assert.equal(f.writes.length, 0);
  const ids = await readJson(globalConfigFile(p));
  await atomicJson(globalConfigFile(p), { ...ids, userID: randomBytes(32).toString('hex') });
  await assert.rejects(vault.loadAccount(p.name), /^Error: identity_changed$/);
  assert.equal(reads, 0); assert.equal(f.requests.length, 0);
});

test('bound account UUID mismatch still blocks before credential reads or authentication', async t => {
  const f = await fixture(t), p = await f.profile();
  t.mock.method(os, 'homedir', () => f.root);
  const ids = await readJson(globalConfigFile(p));
  let reads = 0;
  const vault = f.vault({ store: { ...f.store, read: async () => { reads++; throw new Error('unexpected_credential_read'); } } });
  for (const oauthAccount of [undefined, { ...ids.oauthAccount, accountUuid: UUID_B }]) {
    await atomicJson(globalConfigFile(p), { ...ids, oauthAccount });
    await assert.rejects(vault.loadAccount(p.name), /^Error: wrong_account$/);
  }
  assert.equal(reads, 0); assert.equal(f.requests.length, 0); assert.equal(f.writes.length, 0);
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

test('refresh journal precedes target write and a pending rotation cannot use the previous token again', async t => {
  const f = await fixture(t), p = await f.profile();
  f.values.set(p.configDirectory, credentials('A', 0));
  let pending = false, preserved;
  const receipt = { id: 'INVENTED-refresh-receipt', capturedAt: new Date().toISOString() };
  const vault = f.vault({
    pendingRefresh: () => pending,
    preserveRefresh: async input => {
      assert.equal(f.writes.length, 0);
      assert.equal(input.name, p.name); assert.equal(input.accountUuid, UUID_A);
      assert.deepEqual(Object.keys(input.credentials), ['claudeAiOauth']);
      preserved = structuredClone(input.credentials); pending = true; return receipt;
    },
    store: { read: f.store.read, write: async () => { throw new Error('fixture_target_too_large'); } },
  });
  await assert.rejects(vault.loadAccount(p.name), /fixture_target_too_large/);
  assert.equal(preserved.claudeAiOauth.refreshToken, 'INVENTED-REFRESH-A-NEW');
  await assert.rejects(vault.loadAccount(p.name), /auth_renewal_failed/);
  assert.equal(f.requests.filter(value => value.kind === 'refresh').length, 1);
  assert.equal(f.values.get(p.configDirectory).claudeAiOauth.refreshToken, 'INVENTED-REFRESH-A');
});

test('refresh receipt commits with its grant and is acknowledged only after identity verification', async t => {
  const f = await fixture(t), p = await f.profile(); f.values.set(p.configDirectory, credentials('A', 0));
  const receipt = { id: 'INVENTED-refresh-receipt', capturedAt: new Date().toISOString() };
  let pending = false, acknowledged = false;
  const vault = f.vault({ pendingRefresh: () => pending,
    preserveRefresh: async () => { pending = true; return receipt; },
    acknowledgeRefresh: async input => {
      assert.equal(f.requests.filter(value => value.kind === 'profile').length, 1);
      assert.deepEqual(f.values.get(p.configDirectory).ccpickRuntimeGrant, receipt);
      assert.equal(input.name, p.name); acknowledged = true; pending = false;
    },
  });
  assert.equal((await vault.loadAccount(p.name)).accountUuid, UUID_A);
  assert(acknowledged); assert.equal((await vault.loadAccount(p.name)).accountUuid, UUID_A);
  assert.equal(f.requests.filter(value => value.kind === 'refresh').length, 1);
});

test('refresh recovery is bound to its original account and organization and honors its wait', async t => {
  const f = await fixture(t), pa = await f.profile('A'), pb = await f.profile('B');
  const receipt = { id: 'INVENTED-rotation', capturedAt: new Date().toISOString(),
    reason: 'oauth_refresh_completed', profileId: pa.name, accountUuid: UUID_A, organizationUuid: ORG_A };
  const vault = f.vault();
  await assert.rejects(vault.recoverGrant({ credentials: credentials('B'), receipt, persist: async () => {} }), /wrong_account/);
  assert.equal(f.writes.length, 0); assert.equal(f.values.get(pb.configDirectory).claudeAiOauth.refreshToken, 'INVENTED-REFRESH-B');
  await atomicJson(path.join(pa.root, 'auth-backoff.json'), { version: 1, retryAt: Date.now() + 3_600_000 });
  const count = f.requests.length;
  await assert.rejects(vault.recoverGrant({ credentials: credentials('A-NEW'), receipt, persist: async () => {} }), /auth_rate_limited/);
  assert.equal(f.requests.length, count); assert.equal(f.writes.length, 0);
});

test('a refresh receipt in the vault is not proof that official identity verification succeeded', async t => {
  const f = await fixture(t), p = await f.profile();
  const receipt = { id: 'INVENTED-rotation', capturedAt: new Date().toISOString(),
    reason: 'oauth_refresh_completed', profileId: p.name, accountUuid: UUID_A, organizationUuid: ORG_A };
  f.values.set(p.configDirectory, { ...credentials('A-NEW'), ccpickRuntimeGrant: { id: receipt.id, capturedAt: receipt.capturedAt } });
  const vault = f.vault({ request: async () => { throw Object.assign(new Error('auth_rate_limited'), { status: 429 }); } });
  await assert.rejects(vault.recoverGrant({ credentials: credentials('A'), receipt, persist: async () => {} }), /auth_rate_limited/);
  assert.equal(f.writes.length, 0);
  assert((await readJson(path.join(p.root, 'auth-backoff.json'))).retryAt > Date.now());
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

test('one profile 429 stops concurrent retries, survives service restart and leaves another account available', async t => {
  const f = await fixture(t), pa = await f.profile('A'), pb = await f.profile('B');
  const until = Date.now() + 7_200_000;
  let calls = 0;
  const request = async (...args) => {
    calls++;
    if (args[2].token.endsWith('-A')) throw Object.assign(new Error('auth_rate_limited'), { status: 429, retryAt: until });
    return f.request(...args);
  };
  const vault = f.vault({ request });
  const attempts = await Promise.allSettled(Array.from({ length: 12 }, () => vault.loadAccount(pa.name)));
  assert(attempts.every(value => value.status === 'rejected' && value.reason.retryAt === until));
  assert.equal(calls, 1);
  assert.deepEqual(await readJson(path.join(pa.root, 'auth-backoff.json')), { version: 1, retryAt: until });
  const restarted = f.vault({ request });
  await assert.rejects(restarted.loadAccount(pa.name), error => error.message === 'auth_rate_limited' && error.retryAt === until);
  assert.equal(calls, 1); assert.equal((await restarted.loadAccount(pb.name)).accountUuid, UUID_B);
  assert.equal(calls, 2); assert.equal(f.writes.length, 0);
});

test('existing usage 429 in seconds blocks profile, refresh, known-grant import and cached readiness without changing files', async t => {
  const f = await fixture(t), p = await f.profile(), vault = f.vault();
  await vault.loadAccount(p.name);
  const file = path.join(p.root, 'usage.json'), retryAt = Date.now() + 7_200_000;
  await atomicJson(file, { error: 'http-429', nextPollAt: retryAt / 1000, preserved: 'fixture' });
  const before = await fs.readFile(file);
  await assert.rejects(vault.loadAccount(p.name), error => error.retryAt === retryAt);
  f.values.set(p.configDirectory, credentials('A', 0));
  const restarted = f.vault();
  await assert.rejects(restarted.loadAccount(p.name), /auth_rate_limited/);
  await assert.rejects(restarted.importGrant({ credentials: credentials('A') }), /auth_rate_limited/);
  assert.equal(f.requests.length, 1); assert.equal(f.writes.length, 0);
  assert.deepEqual(await fs.readFile(file), before);
});

test('new 429 respects a longer monitor deadline written while the request was in flight', async t => {
  const f = await fixture(t), p = await f.profile(), retryAt = Date.now() + 10_800_000;
  const vault = f.vault({ request: async () => {
    await atomicJson(path.join(p.root, 'usage.json'), { error: 'http-429', nextPollAt: retryAt / 1000 });
    throw Object.assign(new Error('auth_rate_limited'), { status: 429, retryAt: Date.now() + 1_800_000 });
  } });
  await assert.rejects(vault.loadAccount(p.name), error => error.retryAt === retryAt);
  assert.equal((await readJson(path.join(p.root, 'auth-backoff.json'))).retryAt, retryAt);
});

test('refresh 429 leaves the old grant intact, enforces the minimum wait, and never changes selection', async t => {
  const f = await fixture(t), p = await f.profile(), current = await f.profile('B');
  f.values.set(p.configDirectory, credentials('A', 0));
  const file = path.join(f.root, 'state.json'), state = { selected: current.name, selectedAt: 'fixture-original' };
  await atomicJson(file, state); const before = await fs.readFile(file);
  let calls = 0; const started = Date.now();
  const request = async (_, kind) => {
    calls++; assert.equal(kind, 'refresh'); throw Object.assign(new Error('auth_rate_limited'), { status: 429 });
  };
  const vault = f.vault({ request });
  const runtime = new ActiveRuntime({ readSelection: () => readJson(file),
    bootstrapAccessToken: async () => 'INVENTED-ACCESS-B', loadAccount: name => vault.loadAccount(name),
    select: async () => assert.fail('failed readiness must not publish a selection') });
  await assert.rejects(runtime.select(p.name), error => error.retryAt >= started + 1_800_000);
  await assert.rejects(f.vault({ request }).loadAccount(p.name), /auth_rate_limited/);
  assert.equal(calls, 1); assert.equal(f.writes.length, 0); assert.deepEqual(await fs.readFile(file), before);
});

test('successful token rotation remains durable across profile 429 and is not repeated before its wait expires', async t => {
  const f = await fixture(t), p = await f.profile();
  f.values.set(p.configDirectory, credentials('A', 0));
  let limited = true, profileCalls = 0;
  const request = async (...args) => {
    if (args[1] === 'profile') {
      profileCalls++; assert.equal(f.writes.length, 1);
      if (limited) throw Object.assign(new Error('auth_rate_limited'), { status: 429 });
    }
    return f.request(...args);
  };
  await assert.rejects(f.vault({ request }).loadAccount(p.name), /auth_rate_limited/);
  assert.equal(f.values.get(p.configDirectory).claudeAiOauth.refreshToken, 'INVENTED-REFRESH-A-NEW');
  const file = path.join(p.root, 'auth-backoff.json'), saved = await fs.readFile(file);
  const until = JSON.parse(saved).retryAt;
  limited = false; const restarted = f.vault({ request });
  await assert.rejects(restarted.loadAccount(p.name), /auth_rate_limited/);
  assert.equal(profileCalls, 1); assert.equal(f.requests.filter(value => value.kind === 'refresh').length, 1);
  t.mock.method(Date, 'now', () => until + 1);
  assert.equal((await restarted.loadAccount(p.name)).accountUuid, UUID_A);
  assert.equal(profileCalls, 2); assert.equal(f.requests.filter(value => value.kind === 'refresh').length, 1);
  assert.deepEqual(await fs.readFile(file), saved); // A success never clears or shortens prior evidence.
});

test('unregistered login/profile/recovery share durable enrollment backoff without blocking a registered account', async t => {
  const f = await fixture(t), p = await f.profile();
  let calls = 0;
  const request = async (...args) => {
    calls++;
    if (args[1] === 'login') throw Object.assign(new Error('auth_rate_limited'), { status: 429 });
    return f.request(...args);
  };
  const vault = f.vault({ request });
  const results = await Promise.allSettled(Array.from({ length: 5 }, () => vault.exchangeLogin({ body: { code: 'INVENTED' } })));
  assert(results.every(result => result.status === 'rejected' && result.reason.message === 'auth_rate_limited'));
  assert.equal(calls, 1);
  const restarted = f.vault({ request });
  await assert.rejects(restarted.exchangeLogin({ body: { code: 'INVENTED-OTHER' } }), /auth_rate_limited/);
  await assert.rejects(restarted.verifyGrant(credentials('B')), /auth_rate_limited/);
  await assert.rejects(restarted.recoverGrant({ credentials: credentials('B', 0),
    receipt: { id: 'fixture-new', capturedAt: new Date().toISOString() }, persist: async () => assert.fail('must not rotate') }), /auth_rate_limited/);
  assert.equal(calls, 1); assert.equal((await restarted.loadAccount(p.name)).accountUuid, UUID_A);
  assert.equal(calls, 2);
});

test('expired recovery preserves its rotated grant before profile 429 and retries reuse the journal token', async t => {
  const f = await fixture(t); let persisted, refreshCalls = 0, profileCalls = 0;
  const request = async (...args) => {
    if (args[1] === 'refresh') { refreshCalls++; return f.request(...args); }
    profileCalls++; assert.equal(persisted.claudeAiOauth.refreshToken, 'INVENTED-REFRESH-A-NEW');
    throw Object.assign(new Error('auth_rate_limited'), { status: 429 });
  };
  const receipt = { id: 'fixture-unbound', capturedAt: new Date().toISOString() };
  const persist = async value => { persisted = structuredClone(value); };
  await assert.rejects(f.vault({ request }).recoverGrant({ credentials: credentials('A', 0), receipt, persist }), /auth_rate_limited/);
  await assert.rejects(f.vault({ request }).recoverGrant({ credentials: persisted, receipt, persist }), /auth_rate_limited/);
  assert.equal(refreshCalls, 1); assert.equal(profileCalls, 1);
});

test('OAuth on a validated B proxy still shares durable enrollment admission', async t => {
  const f = await fixture(t), p = await f.profile(); let calls = 0;
  const egress = { account: async () => ({ household: 'A', proxy, revision: 'A' }),
    defaultGroup: async () => assert.fail('a validated login proxy must not resolve the default group') };
  const request = async (...args) => {
    calls++;
    if (args[1] === 'login') {
      assert.equal(args[0], proxyB);
      throw Object.assign(new Error('auth_rate_limited'), { status: 429 });
    }
    return f.request(...args);
  };
  await assert.rejects(f.vault({ egress, request }).exchangeLogin({ body: { code: 'INVENTED-B-CODE' } },
    { proxy: proxyB }), /auth_rate_limited/);
  const restarted = f.vault({ egress, request });
  await assert.rejects(restarted.exchangeLogin({ body: { code: 'INVENTED-B-OTHER' } },
    { proxy: proxyB }), /auth_rate_limited/);
  assert.equal(calls, 1);
  assert.equal((await restarted.loadAccount(p.name)).accountUuid, UUID_A);
  assert.equal(calls, 2);
});

test('refresh journal captures the frozen household when policy changes during rotation', async t => {
  const f = await fixture(t), p = await f.profile('B'); let household = 'B', captured;
  f.values.set(p.configDirectory, credentials('B', 0));
  const egress = { account: async () => ({ household, proxy: household === 'B' ? proxyB : proxy, revision: household }) };
  const requests = [];
  const vault = f.vault({ egress, request: async (selectedProxy, kind, options) => {
    requests.push({ selectedProxy, kind });
    if (kind === 'refresh') {
      household = 'A';
      return { access_token: 'INVENTED-ACCESS-B-NEW', refresh_token: 'INVENTED-REFRESH-B-NEW', expires_in: 3600 };
    }
    return remote('B');
  }, preserveRefresh: async input => {
    captured = input;
    return { id: 'INVENTED-frozen-B-refresh', capturedAt: new Date().toISOString() };
  } });
  await assert.rejects(vault.loadAccount(p.name), /selection_changed/);
  assert.equal(captured.household, 'B');
  assert.equal(captured.name, p.name);
  assert.deepEqual(requests.map(item => item.selectedProxy), [proxyB, proxyB]);
  assert.equal(f.values.get(p.configDirectory).claudeAiOauth.refreshToken, 'INVENTED-REFRESH-B-NEW');
});

test('B refresh recovery keeps its household and a missing route never retries through A', async t => {
  const f = await fixture(t), p = await f.profile('B'); let unavailable = false, persisted;
  const egress = { account: async () => {
    if (unavailable) throw new Error('household_not_configured');
    return { household: 'B', proxy: proxyB, revision: 'B' };
  }, defaultGroup: async () => assert.fail('a bound refresh must use its account route') };
  const requests = [];
  const request = async (selectedProxy, kind) => {
    requests.push({ selectedProxy, kind });
    if (kind === 'refresh') return { access_token: 'INVENTED-ACCESS-B-NEW', refresh_token: 'INVENTED-REFRESH-B-NEW', expires_in: 3600 };
    assert(persisted); return remote('B');
  };
  const receipt = { id: 'INVENTED-B-refresh-recovery', capturedAt: new Date().toISOString(),
    reason: 'oauth_refresh_completed', profileId: p.name, accountUuid: UUID_B, organizationUuid: ORG_B, household: 'B' };
  assert.equal((await f.vault({ egress, request }).recoverGrant({ credentials: credentials('B', 0), receipt,
    persist: async value => { persisted = value; } })).name, p.name);
  assert.deepEqual(requests.map(item => item.selectedProxy), [proxyB, proxyB]);
  unavailable = true;
  const before = requests.length;
  await assert.rejects(f.vault({ egress, request }).recoverGrant({ credentials: credentials('B', 0),
    receipt: { ...receipt, id: 'INVENTED-B-another-refresh' },
    persist: async () => assert.fail('no route permits no rotation') }), /household_not_configured/);
  assert.equal(requests.length, before);
});

test('profile hints rebuild their auth and monitor backoff paths from the registered name', async t => {
  const f = await fixture(t), p = await f.profile(), retryAt = Date.now() + 3_600_000;
  await atomicJson(path.join(p.root, 'usage.json'), { error: 'http-429', nextPollAt: retryAt / 1000 });
  const hint = { ...p, root: path.join(f.root, 'unrelated-directory') };
  await assert.rejects(f.vault().verifyGrant(credentials('A'), hint), error => error.retryAt === retryAt);
  assert.equal(f.requests.length, 0);
});

test('a durable historical B bearer uses its registered route and backoff after vault reconstruction', async t => {
  const f = await fixture(t), p = await f.profile('B');
  const egress = { account: async () => ({ household: 'B', proxy: proxyB, revision: 'B' }),
    defaultGroup: async () => assert.fail('verified history must retain its registered route') };
  await f.vault({ egress }).loadAccount(p.name);
  f.values.set(p.configDirectory, credentials('B-NEW'));
  await f.vault({ egress }).verifyGrant(credentials('B'));
  assert.equal(f.requests.at(-1).selectedProxy, proxyB);
  const before = f.requests.length, retryAt = Date.now() + 3_600_000;
  await atomicJson(path.join(p.root, 'auth-backoff.json'), { version: 1, retryAt });
  await assert.rejects(f.vault({ egress }).verifyGrant(credentials('B')), error => error.retryAt === retryAt);
  assert.equal(f.requests.length, before);
});

test('fresh B discovery honors the registered account wait before any durable bearer binding', async t => {
  const f = await fixture(t), p = await f.profile('B'), retryAt = Date.now() + 3_600_000;
  await atomicJson(path.join(p.root, 'auth-backoff.json'), { version: 1, retryAt });
  const egress = { defaultGroup: async () => assert.fail('the login receipt already binds its household'),
    group: async household => {
      assert.equal(household, 'B'); return { household, proxy: proxyB, revision: 'B' };
    }, account: async () => ({ household: 'B', proxy: proxyB, revision: 'B' }) };
  const receipt = { id: 'INVENTED-fresh-B-wait', capturedAt: new Date().toISOString(), household: 'B' };
  const vault = f.vault({ egress });
  await assert.rejects(vault.importGrant({ credentials: credentials('B-FRESH'), receipt }), /auth_rate_limited/);
  await assert.rejects(vault.importGrant({ credentials: credentials('B-FRESH'), receipt }), /auth_rate_limited/);
  assert.equal(f.requests.length, 1); assert.equal(f.requests[0].selectedProxy, proxyB);
  assert.equal(f.writes.length, 0);
  await assert.rejects(fs.stat(path.join(f.root, 'active-runtime', 'bearer-bindings.json')), { code: 'ENOENT' });
});

test('fresh tokens discovered as a waiting registered account cannot import or acknowledge recovery', async t => {
  const f = await fixture(t), p = await f.profile(), retryAt = Date.now() + 7_200_000;
  const waitFile = path.join(p.root, 'auth-backoff.json');
  await atomicJson(waitFile, { version: 1, retryAt });
  const waitBefore = await fs.readFile(waitFile), idsBefore = await fs.readFile(globalConfigFile(p));
  const profileBefore = await fs.readFile(path.join(p.root, 'profile.json'));
  const storedBefore = structuredClone(f.values.get(p.configDirectory));
  const grant = credentials('A-NEW-LOGIN');
  const receipt = { id: 'fixture-fresh-waiting-login', capturedAt: new Date().toISOString() };
  const pending = { credentials: structuredClone(grant), receipt: structuredClone(receipt) };
  let acknowledged = false;
  const vault = f.vault();
  const limited = error => error.message === 'auth_rate_limited' && error.retryAt === retryAt;
  await assert.rejects(vault.importGrant({ credentials: grant, receipt }), limited);
  assert.equal(f.requests.length, 1); // One identity discovery for opaque new tokens.
  assert.equal(f.requests[0].kind, 'profile');
  await assert.rejects(vault.verifyGrant(grant), limited);
  await assert.rejects(vault.importGrant({ credentials: grant, receipt }), limited);
  await assert.rejects(vault.recoverGrant({ ...pending,
    persist: async () => assert.fail('an unexpired grant must stay in its pending journal'),
  }).then(() => { acknowledged = true; }), limited);
  assert.equal(acknowledged, false); assert.equal(f.requests.length, 1);
  assert.equal(f.writes.length, 0); assert.deepEqual(f.values.get(p.configDirectory), storedBefore);
  assert.deepEqual(pending, { credentials: grant, receipt });
  assert.deepEqual(await fs.readFile(waitFile), waitBefore);
  assert.deepEqual(await fs.readFile(globalConfigFile(p)), idsBefore);
  assert.deepEqual(await fs.readFile(path.join(p.root, 'profile.json')), profileBefore);
  await assert.rejects(f.vault().recoverGrant({ ...pending,
    persist: async () => assert.fail('must preserve pending grant'),
  }), limited);
  assert.equal(f.requests.length, 2); assert.equal(f.writes.length, 0);
});
