import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import { AccountEgressRouter } from '../account-egress.mjs';
import { atomicJson, createProfile, getProfile } from '../core.mjs';

async function fixture(t) {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), 'account-egress-'));
  t.after(() => fs.rm(root, { recursive: true, force: true }));
  const serviceRoot = path.join(root, 'service-a');
  const serviceB = path.join(root, 'service-b');
  const dataRoot = path.join(root, 'data');
  for (const dir of [serviceRoot, serviceB, dataRoot, path.join(serviceRoot, 'config')])
    await fs.mkdir(dir, { recursive: true, mode: 0o700 });
  const config = { serviceRoot, dataRoot, networkProfile: 'primary', platform: process.platform,
    browser: path.join(serviceRoot, 'scripts/login-browser/login-browser.exe') };
  for (const name of ['account-a', 'account-b']) {
    const dir = path.join(dataRoot, name);
    await fs.mkdir(dir, { mode: 0o700 });
    await atomicJson(path.join(dir, 'profile.json'), { version: 1, name, label: name,
      identity: { userID: 'a'.repeat(64), machineID: 'b'.repeat(64) }, account: null });
  }
  const routes = { version: 1, defaultGroup: 'A', groups: {
    A: { serviceRoot, networkProfile: 'primary' }, B: { serviceRoot: serviceB, networkProfile: 'secondary' } } };
  const file = path.join(serviceRoot, 'config/account-egress.json');
  const calls = [];
  let now = 100;
  const network = async (value, options) => {
    calls.push({ config: value, options });
    return { apiMode: 'official', proxy: value.serviceRoot === serviceB ? 'http://127.0.0.1:12811' : 'http://127.0.0.1:11811' };
  };
  const router = new AccountEgressRouter(config, { proxy: 'http://127.0.0.1:11811', network, now: () => now });
  return { config, routes, file, calls, router, network, tick: n => { now += n; } };
}

test('single-route legacy retains A and its prepared proxy', async t => {
  const f = await fixture(t);
  const route = await f.router.account('account-a');
  assert.equal(route.household, 'A'); assert.equal(route.householdSource, 'default');
  assert.equal(route.proxy, 'http://127.0.0.1:11811'); assert.equal(f.calls.length, 0);
  assert.equal(await f.router.defaultGroup(), 'A');
  await assert.rejects(f.router.group('B'), /household_not_configured/);
  await f.router.setAccount('account-a', 'A');
  assert.equal(f.calls.length, 1);
  assert.deepEqual(f.calls[0].options, { checkOnly: true, requireBrowser: false });
});

test('explicit B route is immutable, checked and isolated from identity and selection', async t => {
  const f = await fixture(t); await atomicJson(f.file, f.routes);
  const profileFile = path.join(f.config.dataRoot, 'account-a/profile.json');
  const before = await fs.readFile(profileFile, 'utf8');
  const route = await f.router.setAccount('account-a', 'B');
  assert.equal(route.household, 'B'); assert.equal(route.networkConfig.serviceRoot, f.routes.groups.B.serviceRoot);
  assert.equal(route.networkConfig.browser, path.join(f.routes.groups.B.serviceRoot, 'scripts/login-browser/login-browser.exe'));
  assert.equal(route.proxy, 'http://127.0.0.1:12811'); assert.ok(Object.isFrozen(route.networkConfig));
  assert.equal(await fs.readFile(profileFile, 'utf8'), before);
  assert.equal((await getProfile(f.config, 'account-a')).household, 'B');
  assert.equal((await f.router.account('account-b')).household, 'A');
  assert.notEqual((await f.router.account('account-a')).revision, (await f.router.account('account-b')).revision);
  assert.equal(await f.router.defaultGroup(), 'A');
});

test('ready cache expires and configuration changes immediately invalidate its key', async t => {
  const f = await fixture(t); await atomicJson(f.file, f.routes);
  const first = await f.router.group('B'); await f.router.group('B'); assert.equal(f.calls.length, 1);
  f.tick(60000); await f.router.group('B'); assert.equal(f.calls.length, 2);
  f.routes.groups.B.networkProfile = 'replacement'; await atomicJson(f.file, f.routes);
  assert.notEqual((await f.router.group('B')).revision, first.revision); assert.equal(f.calls.length, 3);
  await atomicJson(f.file, { ...f.routes, defaultGroup: 'C' });
  await assert.rejects(f.router.group('B'), /account_egress_invalid/);
});

test('failed B readiness never writes policy or falls back to A', async t => {
  const f = await fixture(t); await atomicJson(f.file, f.routes);
  const router = new AccountEgressRouter(f.config, { proxy: 'http://127.0.0.1:11811',
    network: async () => { throw new Error('network_not_ready'); } });
  await assert.rejects(router.setAccount('account-a', 'B'), /network_not_ready/);
  await assert.rejects(fs.access(path.join(f.config.dataRoot, 'account-a/egress-policy.json')), { code: 'ENOENT' });
});

test('concurrent requests share one readiness check and manual binding always refreshes it', async t => {
  const f = await fixture(t); await atomicJson(f.file, f.routes);
  await Promise.all(Array.from({ length: 20 }, () => f.router.group('B')));
  assert.equal(f.calls.length, 1);
  await f.router.setAccount('account-a', 'B'); assert.equal(f.calls.length, 2);
  await f.router.setAccount('account-a', 'B'); assert.equal(f.calls.length, 3);
});

test('routing file changes invalidate B readiness immediately without periodic process launches', async t => {
  const f = await fixture(t); await atomicJson(f.file, f.routes);
  const configDir = path.join(f.routes.groups.B.serviceRoot, 'config');
  await fs.mkdir(configDir, { mode: 0o700 });
  await atomicJson(path.join(configDir, 'selector.json'), { fixtureRevision: 1 });
  const first = await f.router.group('B');
  await atomicJson(path.join(configDir, 'selector.json'), { fixtureRevision: 2 });
  assert.notEqual((await f.router.group('B')).revision, first.revision);
  assert.equal(f.calls.length, 2);
});

test('the prepared primary proxy is rechecked when its local routing document changes', async t => {
  const f = await fixture(t); await atomicJson(f.file, f.routes);
  await f.router.group('A'); assert.equal(f.calls.length, 0);
  await atomicJson(path.join(f.config.serviceRoot, 'config/selector.json'), { fixtureRevision: 2 });
  await f.router.group('A'); assert.equal(f.calls.length, 1);
});

test('alternate household cannot inherit an external primary browser entry', async t => {
  const f = await fixture(t); await atomicJson(f.file, f.routes);
  const router = new AccountEgressRouter({ ...f.config, browser: path.join(f.config.dataRoot, 'outside.exe') }, { network: f.network });
  await assert.rejects(router.group('B'), /account_egress_invalid/);
  assert.equal(f.calls.length, 0);
});

test('sidecar survives account metadata rewrites and malformed policy fails closed', async t => {
  const f = await fixture(t); await atomicJson(f.file, f.routes);
  await f.router.setAccount('account-a', 'B');
  const profileFile = path.join(f.config.dataRoot, 'account-a/profile.json');
  const p = JSON.parse(await fs.readFile(profileFile, 'utf8')); p.label = 'Renamed'; await atomicJson(profileFile, p);
  assert.equal((await f.router.account('account-a')).household, 'B');
  await atomicJson(path.join(f.config.dataRoot, 'account-a/egress-policy.json'), { version: 1, household: 'B', proxy: 'other' });
  await assert.rejects(f.router.account('account-a'), /account_egress_invalid/);
  await atomicJson(path.join(f.config.dataRoot, 'account-a/egress-policy.json'), null);
  await assert.rejects(f.router.account('account-a'), /account_egress_invalid/);
});

test('configuration changes during a readiness check abort the operation', async t => {
  const f = await fixture(t); await atomicJson(f.file, f.routes);
  const router = new AccountEgressRouter(f.config, { network: async (config, options) => {
    await atomicJson(f.file, { ...f.routes, defaultGroup: 'B' });
    return f.network(config, options);
  } });
  await assert.rejects(router.setAccount('account-a', 'B'), /account_egress_changed/);
  await assert.rejects(fs.access(path.join(f.config.dataRoot, 'account-a/egress-policy.json')), { code: 'ENOENT' });
});

test('unsafe configuration and nonlocal network results are rejected', async t => {
  const f = await fixture(t);
  for (const invalid of [null, { ...f.routes, groups: {} }, { ...f.routes, groups: { A: { serviceRoot: 'relative', networkProfile: 'primary' } } },
    { ...f.routes, extra: true }, { ...f.routes, groups: { C: f.routes.groups.A } }]) {
    await atomicJson(f.file, invalid);
    await assert.rejects(f.router.group('A'), /account_egress_invalid/);
  }
  await atomicJson(f.file, f.routes);
  for (const proxy of ['http://example.invalid:8080', 'http://localhost:8080']) {
    const router = new AccountEgressRouter(f.config, { network: async () => ({ apiMode: 'official', proxy }) });
    await assert.rejects(router.group('B'), /network_not_ready/);
  }
  await assert.rejects(f.router.setAccount('account-a', 'C'), /household_not_configured/);
  await assert.rejects(f.router.setAccount('account-a', 'D'), /household_not_configured/);
  await assert.rejects(f.router.setAccount('account-a', 'E'), /invalid_household/);
});

test('new profile stores the chosen household in a sidecar and existing profiles cannot be overwritten', async t => {
  const f = await fixture(t);
  const initialize = async (_config, profile) => atomicJson(path.join(profile.configDirectory, '.claude.json'),
    { userID: 'c'.repeat(64), machineID: 'd'.repeat(64) });
  const profile = await createProfile(f.config, { name: 'account-c', household: 'B' }, { initialize });
  assert.equal(profile.household, 'B'); assert.equal(profile.householdSource, 'explicit');
  const manifest = JSON.parse(await fs.readFile(path.join(profile.root, 'profile.json'), 'utf8'));
  assert.equal(Object.hasOwn(manifest, 'household'), false);
  await assert.rejects(createProfile(f.config, { name: 'account-c', household: 'A' }, { initialize }), { code: 'EEXIST' });
  assert.equal((await getProfile(f.config, 'account-c')).household, 'B');
  await assert.rejects(createProfile(f.config, { name: 'account-e', household: 'E' }, { initialize }), /invalid_household/);
});

for (const household of ['C', 'D']) test(`${household} route keeps A/B bindings and checks its own independent upstream before saving`, async t => {
  const f = await fixture(t), serviceC = path.join(f.config.serviceRoot, '..', 'service-c');
  await fs.mkdir(serviceC, { mode: 0o700 });
  const networkProfile = `household-${household.toLowerCase()}`;
  f.routes.groups[household] = { serviceRoot: serviceC, networkProfile };
  await atomicJson(f.file, f.routes);
  await f.router.setAccount('account-b', 'B');
  const calls = [];
  const router = new AccountEgressRouter(f.config, { network: async (config, options) => {
    calls.push({ config, options });
    if (config.serviceRoot === serviceC) return { apiMode: 'official', proxy: 'http://127.0.0.1:13011' };
    return f.network(config, options);
  } });
  const route = await router.setAccount('account-a', household);
  assert.equal(route.household, household);
  assert.equal(route.proxy, 'http://127.0.0.1:13011');
  assert.equal(route.networkConfig.networkProfile, networkProfile);
  assert.equal(route.networkConfig.browser, path.join(serviceC, 'scripts/login-browser/login-browser.exe'));
  assert.deepEqual(calls[0].options, { checkOnly: true, requireBrowser: false });
  assert.equal(await router.defaultGroup(), 'A');
  assert.deepEqual(await router.configuredGroups(), ['A', 'B', household]);
  assert.equal((await router.account('account-b')).household, 'B');
  assert.equal((await getProfile(f.config, 'account-a')).household, household);

  const failed = new AccountEgressRouter(f.config, { network: async () => { throw new Error('network_not_ready'); } });
  await assert.rejects(failed.setAccount('account-b', household), /network_not_ready/);
  assert.equal((await getProfile(f.config, 'account-b')).household, 'B');
});

for (const household of ['C', 'D']) test(`new ${household} profile persists its route sidecar without changing native identity storage`, async t => {
  const f = await fixture(t);
  const initialize = async (_config, p) => atomicJson(path.join(p.configDirectory, '.claude.json'),
    { userID: 'c'.repeat(64), machineID: 'd'.repeat(64) });
  const profile = await createProfile(f.config, { name: 'account-c', household }, { initialize });
  assert.equal(profile.household, household);
  assert.equal(profile.householdSource, 'explicit');
  assert.equal((await getProfile(f.config, 'account-a')).household, null);
  assert.deepEqual(JSON.parse(await fs.readFile(path.join(profile.configDirectory, '.claude.json'), 'utf8')),
    { userID: 'c'.repeat(64), machineID: 'd'.repeat(64) });
  assert.deepEqual(JSON.parse(await fs.readFile(path.join(profile.root, 'egress-policy.json'), 'utf8')),
    { version: 1, household });
});
