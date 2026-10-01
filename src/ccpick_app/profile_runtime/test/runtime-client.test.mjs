import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import http from 'node:http';
import { once } from 'node:events';
import { randomBytes, randomUUID } from 'node:crypto';
import { createProfile, atomicJson, readJson } from '../core.mjs';
import { runtimeRoot, runtimeDirectory, runtimeEnvironment, inheritedRuntimeScope,
  runtimeServiceInfo, runtimeRpc, ensureRuntime, runtimeRequest, launchRuntime } from '../runtime-client.mjs';
import { contextProfile } from '../registry.mjs';
import { openAccountBrowser, finishInteractiveLogin } from '../browser.mjs';
import { launch, selectRuntimeProfile } from '../portable.mjs';

async function fixture(t) {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), 'runtime-client-test-'));
  t.after(() => fs.rm(root, { recursive: true, force: true }));
  const config = { dataRoot: path.join(root, 'data'), serviceRoot: root, native: '/invented/native',
    browser: '/invented/house-browser', accountBrowser: '/invented/account-browser',
    platform: process.platform, seamlessAccounts: true, integrated: true };
  await fs.mkdir(config.dataRoot, { mode: 0o700 });
  const profiles = [];
  for (const name of ['account-a', 'account-b']) profiles.push(await createProfile(config, { name }, {
    initialize: async (_, p) => atomicJson(path.join(p.configDirectory, '.claude.json'), {
      userID: randomBytes(32).toString('hex'), machineID: randomBytes(32).toString('hex') }),
  }));
  await atomicJson(path.join(root, 'state.json'), { version: 2, enabled: true, selected: 'account-a' });
  await fs.mkdir(runtimeDirectory(config), { recursive: true, mode: 0o700 });
  for (const p of profiles) await fs.mkdir(runtimeDirectory(config, p.name), { recursive: true, mode: 0o700 });
  const service = { pid: process.pid, controlPort: 12341, proxyPort: 12342, key: 'a'.repeat(64), certFile: path.join(runtimeRoot(config), 'api-cert.pem') };
  await fs.writeFile(service.certFile, 'INVENTED TEST CERTIFICATE', { mode: 0o600 });
  return { root, config, profiles, service };
}

test('runtime environment replaces account context and scopes all supported proxies to the guard', async t => {
  const { config, profiles: [a], service } = await fixture(t);
  const env = runtimeEnvironment(config, { ...a, storage: 'native-default' }, service, 'default', {
    PATH: 'fixture-path', ANTHROPIC_BASE_URL: 'https://untrusted.invalid',
    anthropic_base_url: 'https://lowercase-untrusted.invalid', ANTHROPIC_API_KEY: 'old-key',
    ANTHROPIC_CUSTOM_HEADERS: 'fixture-old-headers', CLAUDE_CONFIG_DIR: 'old-dir', NODE_OPTIONS: '--inspect',
    CCPICK_ACCOUNT_PROFILE: a.name, CCPICK_ACCOUNT_LEASE: 'old', CCPICK_RUNTIME_SCOPE: 'old', HTTPS_PROXY: 'old-proxy',
  });
  assert.equal(env.CLAUDE_CONFIG_DIR, runtimeDirectory(config));
  assert.equal(env.CCPICK_ACCOUNT_PROFILE, undefined); assert.equal(env.CCPICK_ACCOUNT_LEASE, undefined);
  assert.equal(env.CCPICK_ACCOUNT_RUNTIME, '1'); assert.equal(env.CCPICK_RUNTIME_SCOPE, 'default');
  assert.match(env.CCPICK_RUNTIME_CLIENT_ID, /^[a-f0-9-]{36}$/);
  assert.equal(env.ANTHROPIC_BASE_URL, 'https://api.anthropic.com');
  assert.equal(env.anthropic_base_url, undefined); assert.equal(env.ANTHROPIC_API_KEY, undefined);
  assert.equal(env.NODE_OPTIONS, undefined); assert.equal(env.NODE_EXTRA_CA_CERTS, service.certFile);
  assert.equal(env.ANTHROPIC_CUSTOM_HEADERS, `Authorization: Bearer ${service.key}\nx-ccpick-account-runtime: ${service.key}\nx-ccpick-account-scope: default`);
  for (const key of ['HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY', 'WS_PROXY', 'WSS_PROXY']) {
    assert.equal(env[key], 'http://127.0.0.1:12342'); assert.equal(env[key.toLowerCase()], env[key]);
  }
  assert.equal(env.NO_PROXY, '127.0.0.1,localhost,::1'); assert.equal(env.PATH, 'fixture-path');
});

test('nested runtime uses current shared or pinned selection and rejects forged directory context', async t => {
  const { config, profiles: [a, b], service } = await fixture(t);
  const shared = runtimeEnvironment(config, a, service, 'default', {});
  assert.equal(inheritedRuntimeScope(config, shared), 'default');
  await atomicJson(path.join(config.dataRoot, '..', 'state.json'), { version: 2, enabled: true, selected: b.name });
  assert.equal((await contextProfile(config, ['-p', 'fixture'], shared)).name, b.name);
  const pinned = runtimeEnvironment(config, a, service, a.name, {});
  assert.equal((await contextProfile(config, [], pinned)).name, a.name);
  assert.throws(() => inheritedRuntimeScope(config, { ...shared, CLAUDE_CONFIG_DIR: a.configDirectory }), /profile_context_conflict/);
  assert.throws(() => inheritedRuntimeScope({ ...config, seamlessAccounts: false }, shared), /profile_context_conflict/);
  assert.throws(() => inheritedRuntimeScope(config, { ...shared, CCPICK_ACCOUNT_PROFILE: a.name }), /profile_context_conflict/);
});

test('concurrent startup launches one daemon and authenticates readiness', async t => {
  const { config, service } = await fixture(t);
  let published, spawns = 0;
  const deps = { readService: async () => published, rpc: async (_service, action) => {
    assert.equal(action, 'status'); return { ok: true, pid: process.pid };
  }, isAlive: pid => pid === process.pid, spawnSupervisor: async () => {
    spawns++; await new Promise(resolve => setTimeout(resolve, 10));
    await atomicJson(path.join(runtimeRoot(config), 'supervisor.lock'), { pid: process.pid, nonce: 'fixture-host' });
    published = service;
  } };
  const result = await Promise.all([ensureRuntime(config, deps), ensureRuntime(config, deps)]);
  assert.equal(spawns, 1); assert.equal(result[0].key, service.key); assert.equal(result[1].pid, process.pid);
  assert.equal(await readJson(path.join(runtimeRoot(config), 'startup.lock'), true), null);
});

test('startup reclaims verified dead owners but never an alive or unknown owner', async t => {
  const { config, service } = await fixture(t), root = runtimeRoot(config);
  await atomicJson(path.join(root, 'startup.lock'), { pid: 987654, nonce: 'dead-owner' });
  await atomicJson(path.join(root, 'service.lock'), { pid: 987654 });
  let published, spawns = 0;
  await ensureRuntime(config, { readService: async () => published, rpc: async () => ({ ok: true }),
    isAlive: pid => pid === 987654 ? false : true, spawnService: async () => { spawns++; published = service; } });
  assert.equal(spawns, 1); assert.equal((await readJson(path.join(root, 'service.lock'))).pid, 987654);
  for (const state of [true, null]) {
    await atomicJson(path.join(root, 'startup.lock'), { pid: 987654, nonce: 'keep-owner' });
    let now = 0;
    await assert.rejects(ensureRuntime(config, { readService: async () => null, isAlive: () => state,
      now: () => now, sleep: async () => { now += 200; }, timeoutMs: 100,
      spawnService: async () => { throw new Error('must not spawn'); } }), /runtime_start_timed_out/);
    assert.equal((await readJson(path.join(root, 'startup.lock'))).nonce, 'keep-owner');
  }
});

test('service metadata and control RPC reject unexpected paths, response bodies and instance IDs', async t => {
  const { config, service } = await fixture(t), root = runtimeRoot(config);
  await atomicJson(path.join(root, 'channel-key.json'), { value: service.key });
  await atomicJson(path.join(root, 'service.json'), { ...service, key: undefined });
  assert.equal((await runtimeServiceInfo(config)).proxyPort, service.proxyPort);
  await atomicJson(path.join(root, 'service.json'), { ...service, certFile: path.join(root, '..', 'wrong.pem') });
  await assert.rejects(runtimeServiceInfo(config), /runtime_service_invalid/);
  let seen;
  const server = http.createServer(async (req, res) => {
    let bytes = ''; for await (const chunk of req) bytes += chunk;
    seen = { path: req.url, key: req.headers['x-ccpick-account-runtime'], input: JSON.parse(bytes) };
    res.writeHead(409); res.end('{"ok":false,"reason":"secret value must not print"}');
  });
  server.listen(0, '127.0.0.1'); await once(server, 'listening'); t.after(() => new Promise(resolve => server.close(resolve)));
  await assert.rejects(runtimeRpc({ ...service, controlPort: server.address().port }, 'select', { name: 'account-b' }), /^Error: runtime_unavailable$/);
  assert.deepEqual(seen, { path: '/select', key: service.key, input: { name: 'account-b' } });
  await assert.rejects(runtimeRpc(service, 'unrecognized'), /runtime_action_invalid/);
});

test('guarded RPC refuses an old service without falling back and returns only known reasons', async t => {
  const { service } = await fixture(t);
  let selected = 'account-a', mode = 'old'; const paths = [];
  const server = http.createServer(async (req, res) => {
    let bytes = ''; for await (const chunk of req) bytes += chunk;
    const input = JSON.parse(bytes); paths.push(req.url);
    if (req.url === '/select') { selected = input.name; res.end('{"ok":true}'); return; }
    if (mode === 'old') { res.writeHead(409); res.end('{"ok":false,"reason":"runtime_unavailable"}'); return; }
    if (mode === 'missing-marker') { res.end('{"ok":true}'); return; }
    res.writeHead(409); res.end(JSON.stringify({ ok: false, selectionGuard: 1,
      reason: mode === 'changed' ? 'selection_changed' : 'private_account_secret' }));
  });
  server.listen(0, '127.0.0.1'); await once(server, 'listening');
  t.after(() => new Promise(resolve => server.close(resolve)));
  const endpoint = { ...service, controlPort: server.address().port };
  const payload = { name: 'account-b', expectedState: { selected: 'account-a', selectedAt: 'generation-a' } };
  await assert.rejects(runtimeRpc(endpoint, 'select-guarded', payload), /^Error: selection_guard_unsupported$/);
  assert.equal(selected, 'account-a'); assert.deepEqual(paths, ['/select-guarded']);
  mode = 'changed';
  await assert.rejects(runtimeRpc(endpoint, 'select-guarded', payload), /^Error: selection_changed$/);
  mode = 'private';
  await assert.rejects(runtimeRpc(endpoint, 'select-guarded', payload), /^Error: runtime_unavailable$/);
  mode = 'missing-marker';
  await assert.rejects(runtimeRpc(endpoint, 'select-guarded', payload), /^Error: runtime_response_invalid$/);
  assert.equal(selected, 'account-a'); assert(paths.every(value => value === '/select-guarded'));
});

test('runtime-select CLI keeps manual semantics and forwards complete expected-selection flags', async t => {
  const { config } = await fixture(t); const calls = [];
  const request = async (_config, action, payload) => { calls.push({ action, payload });
    return { ok: true, selectionReceipt: { profileId: 'account-b', generation: 'committed-b' } }; };
  await selectRuntimeProfile(config, ['account-b'], { request });
  const selected = await selectRuntimeProfile(config, ['account-b', '--expected-selected', 'account-a',
    '--expected-selected-at', 'generation-a'], { request });
  assert.deepEqual(selected.selectionReceipt, { profileId: 'account-b', generation: 'committed-b' });
  assert.ok(Object.isFrozen(selected.selectionReceipt));
  assert.deepEqual(calls, [{ action: 'select', payload: { name: 'account-b' } },
    { action: 'select-guarded', payload: { name: 'account-b',
      expectedState: { selected: 'account-a', selectedAt: 'generation-a' } } }]);
  const bad = [[], ['account-b', '--expected-selected', 'account-a'],
    ['account-b', '--expected-selected-at', 'generation-a'],
    ['account-b', '--expected-selected', 'account-a', '--expected-selected-at', ''],
    ['account-b', '--expected-selected', 'account-a', '--expected-selected-at', 'generation-a', '--ignored'],
    ['account-b', '--expected-selected', 'account-a', '--unexpected', 'generation-a']];
  for (const args of bad) await assert.rejects(selectRuntimeProfile(config, args, {
    request: async () => assert.fail('malformed CLI guard cannot call RPC'),
    resolve: async () => assert.fail('malformed CLI guard cannot resolve target'),
  }), /invalid_arguments|selection_changed/);
  for (const receipt of [undefined, {}, { profileId: 'account-b' },
    { profileId: 'account-b', generation: '' }, { profileId: 'account-a', generation: 'manual-a' },
    { profileId: 'account-b', generation: 'committed-b', token: 'must-not-forward' }]) {
    await assert.rejects(selectRuntimeProfile(config, ['account-b', '--expected-selected', 'account-a',
      '--expected-selected-at', 'generation-a'], { request: async () => ({ ok: true, selectionReceipt: receipt }) }), /selection_changed/);
  }
});

test('seamless launch keeps native PID lifecycle, registers scope and never uses restart handoff', async t => {
  const { config, profiles: [a], service } = await fixture(t);
  const calls = [], invocations = [], settings = [];
  const deps = { runtimeScope: 'default', environment: {}, ensureRuntime: async () => service,
    prepareNetwork: async () => { calls.push('network'); }, checkSettings: async (_config, profile, env) => settings.push({ profile, env }),
    rpc: async (_service, action, value) => { calls.push(action); return { ok: true, scope: value.scope, directory: runtimeDirectory(config, value.scope) }; },
    invoke: async (exe, args, options) => { invocations.push({ exe, args, options }); await options.onSpawn(process.pid); return { code: 0 }; },
  };
  assert.equal(await launch(config, a.name, 'run', ['-p', 'fixture'], deps), 0);
  assert.deepEqual(calls, ['network', 'register']); assert.equal(invocations.length, 1);
  assert.deepEqual(invocations[0].args, ['-p', 'fixture']);
  assert.equal(settings[0].profile.configDirectory, runtimeDirectory(config));
  const files = await fs.readdir(path.join(runtimeRoot(config), 'leases'));
  const record = await readJson(path.join(runtimeRoot(config), 'leases', files[0]));
  assert.equal(record.childPid, process.pid); assert.equal(record.status, 'exited'); assert.equal(record.scope, 'default');
  assert.equal((await fs.readdir(path.join(a.root, 'leases'))).length, 0);
  const nested = runtimeEnvironment(config, a, service, a.name, {});
  assert.equal(await launchRuntime(config, a.name, 'run', ['-p', 'nested'], { ...deps, runtimeScope: undefined, environment: nested }), 0);
  assert.equal(invocations[1].options.env.CCPICK_RUNTIME_SCOPE, a.name);
});

test('ordinary, resumed and forked launches preserve native arguments and the protected runtime environment', async t => {
  const { config, profiles: [a], service } = await fixture(t);
  const id = randomUUID();
  for (const [scope, args] of [['default', []], ['default', ['--resume', id]],
    [a.name, ['--resume', id, '--fork-session']],
    ['default', ['-p', 'fixture task', '--append-system-prompt', 'fixture instructions',
      '--permission-mode', 'default', '--allowedTools', 'Read,Bash(git status)']]]) {
    const incoming = runtimeEnvironment(config, a, service, scope, { PATH: 'fixture-path' });
    const before = { ...incoming }, calls = [], invocations = [];
    const check = async (_config, profile, env) => {
      assert.equal(profile.configDirectory, runtimeDirectory(config, scope));
      for (const key of ['HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY', 'WS_PROXY', 'WSS_PROXY']) {
        assert.equal(env[key], `http://127.0.0.1:${service.proxyPort}`);
        assert.equal(env[key.toLowerCase()], env[key]);
      }
      for (const key of ['NO_PROXY', 'no_proxy', 'NODE_EXTRA_CA_CERTS', 'ANTHROPIC_BASE_URL', 'ANTHROPIC_CUSTOM_HEADERS',
        'CLAUDE_CONFIG_DIR', 'CCPICK_ACCOUNT_RUNTIME', 'CCPICK_RUNTIME_SCOPE', 'PATH'])
        assert.equal(env[key], incoming[key], `${key} must survive nested launch unchanged`);
      assert.equal(env.NODE_EXTRA_CA_CERTS, service.certFile);
      assert.equal(env.ANTHROPIC_BASE_URL, 'https://api.anthropic.com');
      assert.equal(env.ANTHROPIC_CUSTOM_HEADERS,
        `Authorization: Bearer ${service.key}\nx-ccpick-account-runtime: ${service.key}\nx-ccpick-account-scope: ${scope}`);
      assert.equal(env.CCPICK_RUNTIME_SCOPE, scope);
      assert.notEqual(env.CCPICK_RUNTIME_CLIENT_ID, incoming.CCPICK_RUNTIME_CLIENT_ID);
      const settings = await readJson(path.join(profile.configDirectory, 'settings.json'));
      assert.equal(settings.env.ANTHROPIC_BASE_URL, env.ANTHROPIC_BASE_URL);
      assert.equal(settings.env.ANTHROPIC_CUSTOM_HEADERS, env.ANTHROPIC_CUSTOM_HEADERS);
    };
    assert.equal(await launch(config, a.name, 'run', args, {
      environment: incoming, ensureRuntime: async () => service,
      prepareNetwork: async () => { calls.push('network'); }, checkSettings: check,
      rpc: async (_service, action, payload) => {
        calls.push(action); assert.equal(payload.scope, scope);
        return { ok: true, scope, directory: runtimeDirectory(config, scope) };
      },
      invoke: async (executable, nativeArgs, options) => {
        assert.equal(executable, config.native); assert.deepEqual(nativeArgs, args);
        await check(config, { configDirectory: options.env.CLAUDE_CONFIG_DIR }, options.env);
        invocations.push(nativeArgs); await options.onSpawn(process.pid); return { code: 0 };
      },
    }), 0);
    assert.deepEqual(calls, ['network', 'register']); assert.equal(invocations.length, 1);
    assert.deepEqual(incoming, before, 'the parent process environment is not changed');
  }
});

test('native stop uses existing runtime without readiness or login and checks exact local help', async t => {
  const { config, profiles: [a], service } = await fixture(t);
  const calls = [];
  const deps = { runtimeScope: 'default', environment: {}, readService: async () => service,
    ensureRuntime: async () => { throw new Error('no daemon startup'); }, rpc: async () => { throw new Error('no authentication'); },
    prepareNetwork: async () => { throw new Error('no network request'); }, checkSettings: async () => {},
    invoke: async (_exe, args, options) => {
      calls.push(args); assert.equal(options.env.CLAUDE_CONFIG_DIR, runtimeDirectory(config));
      return args.includes('--help') ? { code: 0, stdout: 'Usage: claude stop <id>\n' } : { code: 0 };
    },
  };
  assert.equal(await launchRuntime(config, a.name, 'run', ['stop', 'deadbeef'], deps), 0); assert.equal(calls.length, 2);
  await assert.rejects(launchRuntime(config, a.name, 'run', ['stop', 'deadbeef'], { ...deps,
    invoke: async () => ({ code: 0, stdout: 'Usage: claude [options] [prompt]' }) }), /native_stop_unsupported/);
});

test('runtime browser registers native state/challenge before opening; notification stays advisory', async t => {
  const { config, profiles: [a], service } = await fixture(t);
  const env = runtimeEnvironment(config, a, service, 'default', {}), order = [];
  const url = 'https://claude.com/oauth/authorize?client_id=fixture&state=fixture-state&code_challenge=fixture-challenge&redirect_uri=http%3A%2F%2Flocalhost%3A12345%2Fcallback';
  const dependencies = { environment: env, network: async () => order.push('network'),
    request: async (_config, action, payload) => { order.push(action); assert.equal(payload.sessionId, env.CCPICK_RUNTIME_CLIENT_ID);
      assert.equal(payload.scope, 'default'); assert.equal(payload.oauthState, 'fixture-state'); assert.equal(payload.oauthChallenge, 'fixture-challenge'); },
    open: async (executable, args) => { order.push('open'); assert.equal(executable, config.browser);
      assert.equal(new URL(args[0]).searchParams.has('login_hint'), false); return { code: 0 }; },
  };
  assert.equal(await openAccountBrowser(config, url, dependencies), 0);
  assert.deepEqual(order, ['network', 'login-begin', 'open']);
  let hinted;
  assert.equal(await finishInteractiveLogin(config, env, { request: async (_config, action, _payload, options) => {
    hinted = { action, options }; throw new Error('offline');
  } }), 0);
  assert.deepEqual(hinted, { action: 'login-finished', options: { start: false, timeoutMs: 3000 } });
  await assert.rejects(openAccountBrowser(config, url, { ...dependencies,
    request: async () => { throw new Error('login_in_progress'); }, open: async () => { throw new Error('must not open'); } }), /login_in_progress/);
});

test('advisory runtime RPC cannot spawn a daemon and mode off always refuses new paths', async t => {
  const { config, service } = await fixture(t);
  const result = await runtimeRequest(config, 'login-finished', { scope: 'default', sessionId: randomUUID() }, {
    start: false, timeoutMs: 3000, readService: async () => service,
    ensureRuntime: async () => { throw new Error('must not start'); }, rpc: async (_service, action, _payload, options) => {
      assert.equal(action, 'login-finished'); assert.equal(options.timeoutMs, 3000); return { ok: true };
    },
  });
  assert.equal(result.ok, true);
  await assert.rejects(ensureRuntime({ ...config, seamlessAccounts: false }), /seamless_runtime_disabled/);
  await assert.rejects(runtimeRequest({ ...config, seamlessAccounts: false }, 'status'), /seamless_runtime_disabled/);
});

test('a live daemon with a mismatching instance is never treated as ready or replaced', async t => {
  const { config, service } = await fixture(t);
  let now = 0, spawned = false;
  await assert.rejects(ensureRuntime(config, { readService: async () => ({ ...service, instanceId: 'expected-instance' }),
    rpc: async () => ({ ok: true, pid: process.pid, instanceId: 'different-instance' }), isAlive: () => true,
    now: () => now, timeoutMs: 100, sleep: async () => { now += 200; }, spawnService: async () => { spawned = true; } }), /runtime_start_timed_out/);
  assert.equal(spawned, false);
});

test('an authenticated legacy service gains supervision without replacing the service', async t => {
  const { config, service } = await fixture(t);
  let launches = 0;
  const deps = { readService: async () => service, rpc: async () => ({ ok: true, pid: service.pid }),
    spawnSupervisor: async () => {
      launches++;
      await atomicJson(path.join(runtimeRoot(config), 'supervisor.lock'), { pid: process.pid, nonce: 'attached-host' });
    },
  };
  assert.equal((await ensureRuntime(config, deps)).pid, service.pid);
  assert.equal((await ensureRuntime(config, deps)).pid, service.pid);
  assert.equal(launches, 1);
});

test('client waits for a living supervisor to recover its child and never starts a second host', async t => {
  const { config, service } = await fixture(t);
  await atomicJson(path.join(runtimeRoot(config), 'supervisor.lock'), { pid: process.pid, nonce: 'recovering-host' });
  let time = 0, ready = false;
  const result = await ensureRuntime(config, { readService: async () => ready ? service : null,
    now: () => time, timeoutMs: 500, sleep: async ms => { time += ms; ready = true; },
    rpc: async () => ({ ok: true, pid: service.pid }),
    spawnSupervisor: async () => { throw new Error('must not spawn a second host'); },
  });
  assert.equal(result.pid, service.pid);
});

test('missing selected authorization and an unavailable observational lease do not prevent native login', async t => {
  const { config, service, profiles: [a] } = await fixture(t);
  await fs.writeFile(path.join(runtimeRoot(config), 'leases'), 'fixture file prevents lease-directory creation', { mode: 0o600 });
  const actions = []; let invoked = false;
  const result = await launchRuntime(config, a.name, 'login', [], { runtimeScope: 'default', environment: {},
    ensureRuntime: async () => service, prepareNetwork: async () => {}, checkSettings: async () => {},
    rpc: async (_service, action) => { actions.push(action); return { ok: true, ready: false, scope: 'default', directory: runtimeDirectory(config) }; },
    invoke: async (_exe, args, options) => {
      invoked = true; assert.deepEqual(args, ['--setting-sources', 'user', 'auth', 'login', '--claudeai']);
      await options.onSpawn(process.pid); return { code: 0 };
    },
  });
  assert.equal(result, 0); assert.equal(invoked, true); assert.deepEqual(actions, ['register', 'login-finished']);
});
