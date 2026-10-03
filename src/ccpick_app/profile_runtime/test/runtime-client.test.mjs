import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import http from 'node:http';
import { once, EventEmitter } from 'node:events';
import { randomBytes, randomUUID } from 'node:crypto';
import { fileURLToPath } from 'node:url';
import { createProfile, atomicJson, readJson, executeNative } from '../core.mjs';
import { runtimeRoot, runtimeDirectory, runtimeEnvironment, inheritedRuntimeScope,
  runtimeServiceInfo, runtimeRpc, ensureRuntime, runtimeRequest, launchRuntime,
  windowsRuntimeTaskSpec, windowsRuntimeTaskScript, startRuntimeSupervisor } from '../runtime-client.mjs';
import { contextProfile } from '../registry.mjs';
import { openAccountBrowser, finishInteractiveLogin } from '../browser.mjs';
import { launch, selectRuntimeProfile } from '../portable.mjs';

const fixtureIdentity = async pid => ({ pid, createdAt: '2000-01-01T00:00:00.000Z', executable: process.execPath, script: null });
const fixtureOwners = { inspectProcess: fixtureIdentity, isOwnerAlive: async owner => owner?.pid === process.pid };

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
    CLAUDE_CODE_DISABLE_BG_SHELL_PRESSURE_REAP: '0', claude_code_disable_bg_shell_pressure_reap: '0',
  });
  assert.equal(env.CLAUDE_CONFIG_DIR, runtimeDirectory(config));
  assert.equal(env.CCPICK_ACCOUNT_PROFILE, undefined); assert.equal(env.CCPICK_ACCOUNT_LEASE, undefined);
  assert.equal(env.CCPICK_ACCOUNT_RUNTIME, '1'); assert.equal(env.CCPICK_RUNTIME_SCOPE, 'default');
  assert.match(env.CCPICK_RUNTIME_CLIENT_ID, /^[a-f0-9-]{36}$/);
  assert.equal(env.ANTHROPIC_BASE_URL, 'https://api.anthropic.com');
  assert.equal(env.anthropic_base_url, undefined); assert.equal(env.ANTHROPIC_API_KEY, undefined);
  assert.equal(env.NODE_OPTIONS, undefined); assert.equal(env.NODE_EXTRA_CA_CERTS, service.certFile);
  assert.equal(env.CLAUDE_CODE_DISABLE_BG_SHELL_PRESSURE_REAP, '1');
  assert.equal(env.claude_code_disable_bg_shell_pressure_reap, undefined);
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

test('persisted scope ports restore each client proxy and reject ambiguous or reserved endpoints', async t => {
  const { config, profiles: [a], service } = await fixture(t), root = runtimeRoot(config);
  const published = { ...service, key: undefined, egressVersion: 1, householdOptions: ['A', 'B'] };
  await atomicJson(path.join(root, 'channel-key.json'), { value: service.key });
  await atomicJson(path.join(root, 'service.json'), published);
  await atomicJson(path.join(root, 'scope-proxy-ports.json'), { version: 1, ports: { 'account-a': 12343 } });
  const restored = await runtimeServiceInfo(config);
  assert.equal(restored.scopeProxyPorts['account-a'], 12343);
  const env = runtimeEnvironment(config, a, restored, 'account-a', {});
  for (const key of ['HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY', 'WS_PROXY', 'WSS_PROXY'])
    assert.equal(env[key], 'http://127.0.0.1:12343');
  assert.equal(runtimeEnvironment(config, a, restored, 'default', {}).HTTPS_PROXY, 'http://127.0.0.1:12342');
  for (const ports of [{ default: 12343 }, { 'account-a': service.proxyPort }, { 'account-a': service.controlPort },
    { 'account-a': 12343, 'account-b': 12343 }, 'bad']) {
    await atomicJson(path.join(root, 'scope-proxy-ports.json'), { version: 1, ports });
    await assert.rejects(runtimeServiceInfo(config), /runtime_service_invalid/);
  }
});

test('concurrent startup launches one daemon and authenticates readiness', async t => {
  const { config, service } = await fixture(t);
  let published, spawns = 0;
  const deps = { ...fixtureOwners, readService: async () => published, rpc: async (_service, action) => {
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
  await ensureRuntime(config, { ...fixtureOwners, readService: async () => published, rpc: async () => ({ ok: true }),
    isAlive: pid => pid === 987654 ? false : true, spawnService: async () => { spawns++; published = service; } });
  assert.equal(spawns, 1); assert.equal((await readJson(path.join(root, 'service.lock'))).pid, 987654);
  for (const state of [true, null]) {
    await atomicJson(path.join(root, 'startup.lock'), { pid: 987654, nonce: 'keep-owner' });
    let now = 0;
    await assert.rejects(ensureRuntime(config, { inspectProcess: fixtureIdentity, isOwnerAlive: async () => state,
      readService: async () => null, isAlive: () => state,
      now: () => now, sleep: async () => { now += 200; }, timeoutMs: 100,
      spawnService: async () => { throw new Error('must not spawn'); } }), /runtime_start_timed_out/);
    assert.equal((await readJson(path.join(root, 'startup.lock'))).nonce, 'keep-owner');
  }
});

test('startup recovers reused startup, supervisor and service PIDs only after exact identity mismatch', async t => {
  const { config, service } = await fixture(t), root = runtimeRoot(config);
  const stale = { pid: 987654, nonce: 'old-generation', processIdentity: {
    pid: 987654, createdAt: 'old-generation', executable: process.execPath,
    script: fileURLToPath(new URL('../runtime-supervisor.mjs', import.meta.url)),
  } };
  await atomicJson(path.join(root, 'startup.lock'), stale);
  await atomicJson(path.join(root, 'supervisor.lock'), stale);
  await atomicJson(path.join(root, 'service.lock'), stale);
  const currentService = { ...service, pid: 876543, instanceId: 'new-instance', processIdentity: {
    pid: 876543, createdAt: 'new-service', executable: process.execPath,
    script: fileURLToPath(new URL('../runtime-service.mjs', import.meta.url)),
  } };
  let published = { ...service, ...stale }, spawns = 0;
  const result = await ensureRuntime(config, { readService: async () => published,
    isAlive: () => true, inspectProcess: async pid => pid === 987654
      ? { ...stale.processIdentity, createdAt: 'reused-by-unrelated-process', script: path.join(root, 'unrelated.js') }
      : pid === currentService.pid ? currentService.processIdentity : fixtureIdentity(pid),
    rpc: async () => ({ ok: true, pid: currentService.pid, instanceId: currentService.instanceId }),
    spawnSupervisor: async () => {
      spawns++;
      assert.equal(await readJson(path.join(root, 'supervisor.lock'), true), null);
      assert.equal((await readJson(path.join(root, 'startup.lock'))).processIdentity.createdAt, '2000-01-01T00:00:00.000Z');
      published = currentService;
    },
  });
  assert.equal(result.pid, currentService.pid); assert.equal(spawns, 1);
  assert.deepEqual(await readJson(path.join(root, 'service.lock')), stale, 'service recovery remains owned by the supervisor');
});

test('legacy supervisor PID with a different known script is reclaimed while unreadable startup identity is preserved', async t => {
  const { config, service } = await fixture(t), root = runtimeRoot(config);
  await atomicJson(path.join(root, 'supervisor.lock'), { pid: 987654, nonce: 'legacy-supervisor' });
  let published, spawns = 0;
  const inspect = async pid => pid === 987654 ? { pid, createdAt: 'now', executable: process.execPath,
    script: path.join(root, 'unrelated-worker.js') } : fixtureIdentity(pid);
  await ensureRuntime(config, { readService: async () => published, isAlive: () => true, inspectProcess: inspect,
    isOwnerAlive: async (owner, kind) => kind === 'service' ? true :
      (await import('../runtime-supervisor.mjs')).runtimeOwnerAlive(owner, { isAlive: () => true,
        inspectProcess: inspect, executable: process.execPath,
        script: kind === 'supervisor' ? fileURLToPath(new URL('../runtime-supervisor.mjs', import.meta.url)) : undefined }),
    rpc: async () => ({ ok: true, pid: service.pid }), spawnSupervisor: async () => { spawns++; published = service; } });
  assert.equal(spawns, 1);
  await atomicJson(path.join(root, 'startup.lock'), { pid: 987654, nonce: 'unknown-generation' });
  let clock = 0;
  await assert.rejects(ensureRuntime(config, { readService: async () => null, isAlive: () => true,
    inspectProcess: async pid => pid === process.pid ? fixtureIdentity(pid) : null,
    now: () => clock, timeoutMs: 100, sleep: async () => { clock += 200; },
    spawnSupervisor: async () => { throw new Error('must not spawn'); } }), /runtime_start_timed_out/);
  assert.equal((await readJson(path.join(root, 'startup.lock'))).nonce, 'unknown-generation');
});

test('Windows host launch uses an independent task with configured install paths and no detached fallback', async t => {
  const { config } = await fixture(t), calls = [];
  const win = { ...config, platform: 'win32' }, spec = windowsRuntimeTaskSpec(win);
  assert.equal(spec.taskName, 'ccpick-account-runtime');
  assert.equal(spec.workingDirectory, path.resolve(config.dataRoot, '..'));
  assert(spec.arguments.includes(path.join(spec.workingDirectory, 'app', 'runtime-supervisor.mjs')));
  assert(spec.arguments.includes(path.join(spec.workingDirectory, 'install.json')));
  await startRuntimeSupervisor(win, { checkOnly: true, invoke: async (executable, args, options) => {
    calls.push({ executable, args, options });
    const script = Buffer.from(args.at(-1), 'base64').toString('utf16le');
    assert(script.includes('$taskCheckOnly = $true'));
    assert(script.includes('runtime_task_identity_mismatch'));
    assert(!script.includes('Stop-ScheduledTask')); assert(!script.includes('-Force'));
    assert.equal(options.env.ANTHROPIC_API_KEY, undefined);
    return { code: 0 };
  } });
  assert.equal(calls.length, 1); assert(calls[0].executable.endsWith('powershell.exe'));
  await assert.rejects(startRuntimeSupervisor(win, { invoke: async () => ({ code: 1 }) }), /runtime_start_failed/);
  let unref = false, options;
  await startRuntimeSupervisor({ ...config, platform: 'darwin' }, { spawn: (_exe, _args, value) => {
    options = value; const child = new EventEmitter(); child.unref = () => { unref = true; };
    queueMicrotask(() => child.emit('spawn')); return child;
  } });
  assert.equal(options.detached, true); assert.equal(unref, true);
});

test('Windows task registration, reuse and check-only validate exact ownership without running a real task',
  { skip: process.platform !== 'win32' }, async t => {
    const { config } = await fixture(t), spec = windowsRuntimeTaskSpec(config);
    const prelude = `
$ErrorActionPreference='Stop'
$counts=@{register=0;start=0}
$fixtureSid=[Security.Principal.WindowsIdentity]::GetCurrent().User.Value
$fixtureTask=[pscustomobject]@{Actions=@([pscustomobject]@{Execute=${JSON.stringify(spec.execute)};Arguments=${JSON.stringify(spec.arguments)};WorkingDirectory=${JSON.stringify(spec.workingDirectory)}});
Principal=[pscustomobject]@{UserId=$fixtureSid;LogonType='Interactive';RunLevel='Limited'};
Triggers=@([pscustomobject]@{CimClass=[pscustomobject]@{CimClassName='MSFT_TaskLogonTrigger'};UserId=$fixtureSid});
Settings=[pscustomobject]@{MultipleInstances='IgnoreNew';ExecutionTimeLimit='PT0S';DisallowStartIfOnBatteries=$false;StopIfGoingOnBatteries=$false;Hidden=$true;StartWhenAvailable=$true;RestartCount=3;RestartInterval='PT1M'};State='Ready'}
function Get-ScheduledTask {[CmdletBinding()]param($TaskName,$TaskPath) if($exists){return $fixtureTask}}
function New-ScheduledTaskAction {param($Execute,$Argument,$WorkingDirectory) return $fixtureTask.Actions[0]}
function New-ScheduledTaskPrincipal {param($UserId,$LogonType,$RunLevel) return $fixtureTask.Principal}
function New-ScheduledTaskTrigger {param([switch]$AtLogOn,$User) return $fixtureTask.Triggers[0]}
function New-ScheduledTaskSettingsSet {param($ExecutionTimeLimit,$MultipleInstances,[switch]$Hidden,[switch]$StartWhenAvailable,$RestartCount,$RestartInterval,[switch]$AllowStartIfOnBatteries,[switch]$DontStopIfGoingOnBatteries) return $fixtureTask.Settings}
function Register-ScheduledTask {param($TaskName,$TaskPath,$Action,$Principal,$Trigger,$Settings) $script:counts.register++;$script:exists=$true}
function Start-ScheduledTask {[CmdletBinding()]param($TaskName,$TaskPath) $script:counts.start++}
`;
    const fixturePrelude = prelude.replace(/"(?:[^"\\]|\\.)*"/g, value => "'" + JSON.parse(value).replaceAll("'", "''") + "'");
    const run = async (setup, checkOnly = false) => {
      const source = fixturePrelude + setup + '\ntry {\n' + windowsRuntimeTaskScript(spec, { checkOnly }) +
        '\n[ordered]@{ok=$true;register=$counts.register;start=$counts.start}|ConvertTo-Json -Compress\n} catch { [ordered]@{ok=$false;reason=$_.Exception.Message;register=$counts.register;start=$counts.start}|ConvertTo-Json -Compress }';
      const result = await executeNative(path.join(process.env.SystemRoot ?? 'C:\\Windows', 'System32/WindowsPowerShell/v1.0/powershell.exe'),
        ['-NoProfile', '-NonInteractive', '-EncodedCommand', Buffer.from(source, 'utf16le').toString('base64')],
        { capture: true, timeoutMs: 10000 });
      assert.equal(result.code, 0); return JSON.parse(result.stdout.trim());
    };
    assert.deepEqual(await run('$exists=$false'), { ok: true, register: 1, start: 1 });
    assert.deepEqual(await run('$exists=$true'), { ok: true, register: 0, start: 1 });
    assert.deepEqual(await run('$exists=$true', true), { ok: true, register: 0, start: 0 });
    assert.deepEqual(await run("$exists=$true;$fixtureTask.Actions[0].WorkingDirectory=$fixtureTask.Actions[0].WorkingDirectory.Replace('\\','/')", true),
      { ok: true, register: 0, start: 0 });
    assert.deepEqual(await run("$exists=$true;$fixtureTask.Actions[0].Arguments='different-install'"),
      { ok: false, reason: 'runtime_task_identity_mismatch', register: 0, start: 0 });
    assert.deepEqual(await run('$exists=$true;$fixtureTask.Settings.RestartCount=0'),
      { ok: false, reason: 'runtime_task_identity_mismatch', register: 0, start: 0 });
    assert.deepEqual(await run('$exists=$false', true), { ok: false, reason: 'runtime_task_missing', register: 0, start: 0 });
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
  assert.deepEqual(calls, ['register', 'network']); assert.equal(invocations.length, 1);
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
        'CLAUDE_CONFIG_DIR', 'CLAUDE_CODE_DISABLE_BG_SHELL_PRESSURE_REAP', 'CCPICK_ACCOUNT_RUNTIME', 'CCPICK_RUNTIME_SCOPE', 'PATH'])
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
      assert.equal(settings.env.CLAUDE_CODE_DISABLE_BG_SHELL_PRESSURE_REAP, '1');
      assert.equal(env.CLAUDE_CODE_DISABLE_BG_SHELL_PRESSURE_REAP, '1');
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
    assert.deepEqual(calls, ['register', 'network']); assert.equal(invocations.length, 1);
    assert.deepEqual(incoming, before, 'the parent process environment is not changed');
  }
});

test('B launches register their scope first and only require the selected household network', async t => {
  const { config, profiles: [, b], service, root } = await fixture(t);
  config.browser = path.join(root, 'bin', 'house-browser'); config.networkProfile = 'primary';
  const houseB = path.join(root, 'house-b'); await fs.mkdir(houseB, { mode: 0o700 });
  await fs.mkdir(path.join(root, 'config'), { mode: 0o700 });
  await atomicJson(path.join(root, 'config', 'account-egress.json'), { version: 1, defaultGroup: 'A', groups: {
    A: { serviceRoot: root, networkProfile: 'primary' }, B: { serviceRoot: houseB, networkProfile: 'primary' },
  } });
  const order = [], calls = [], invocations = [];
  const dependencies = { runtimeScope: b.name, environment: {}, ensureRuntime: async () => service,
    rpc: async (_service, action, payload) => { order.push(action); return { directory: runtimeDirectory(config, payload.scope),
      scope: payload.scope, proxyPort: 12343, egressVersion: 1, household: 'B', householdOptions: ['A', 'B'] }; },
    prepareNetwork: async (target, options) => {
      order.push('network'); calls.push(options); assert.equal(target.serviceRoot, houseB);
      assert.notEqual(options.requireHouseReady, false);
      return { proxy: 'http://127.0.0.1:2', apiMode: 'official' };
    }, checkSettings: async () => {}, invoke: async (_, args, options) => { invocations.push({ args, options }); return { code: 0 }; },
  };
  assert.equal(await launchRuntime(config, b.name, 'run', ['--resume', randomUUID()], dependencies), 0);
  assert.deepEqual(order, ['register', 'network']); assert.equal(calls.length, 1);
  assert.equal(invocations[0].options.env.HTTPS_PROXY, 'http://127.0.0.1:12343');
  order.length = 0;
  assert.equal(await launchRuntime(config, b.name, 'login', [], dependencies), 0);
  assert.deepEqual(order.slice(0, 3), ['register', 'network', 'network']);
  assert.equal(calls.at(-1).requireBrowser, true);
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
  assert.deepEqual(order, ['login-begin', 'network', 'open']);
  let hinted;
  assert.equal(await finishInteractiveLogin(config, env, { request: async (_config, action, _payload, options) => {
    hinted = { action, options }; throw new Error('offline');
  } }), 0);
  assert.deepEqual(hinted, { action: 'login-finished', options: { start: false, timeoutMs: 3000 } });
  await assert.rejects(openAccountBrowser(config, url, { ...dependencies,
    request: async () => { throw new Error('login_in_progress'); }, open: async () => { throw new Error('must not open'); } }), /login_in_progress/);
});

for (const household of ['B', 'C', 'D']) for (const oauth of [true, false]) test(`runtime ${oauth ? 'OAuth intent' : 'ordinary official page'} selects the scoped ${household} browser`, async t => {
  const { config, profiles: [a], service, root } = await fixture(t);
  config.browser = path.join(root, 'bin', 'house-browser'); config.networkProfile = 'primary';
  const houseB = path.join(root, 'house-' + household.toLowerCase()); await fs.mkdir(houseB, { mode: 0o700 });
  await fs.mkdir(path.join(root, 'config'), { mode: 0o700 });
  await atomicJson(path.join(root, 'config', 'account-egress.json'), { version: 1, defaultGroup: 'A', groups: {
    A: { serviceRoot: root, networkProfile: 'primary' }, [household]: { serviceRoot: houseB, networkProfile: 'primary' },
  } });
  const env = runtimeEnvironment(config, a, service, 'account-a', {}), checks = [], opened = [];
  const url = oauth ? 'https://claude.com/oauth/authorize?client_id=fixture&state=fixture-state&code_challenge=fixture-challenge&redirect_uri=http%3A%2F%2Flocalhost%3A12345%2Fcallback' :
    'https://claude.ai/settings/billing';
  const dependencies = { environment: env, network: async (target, options) => {
    checks.push({ target, options }); assert.equal(target.serviceRoot, houseB);
    return { proxy: 'http://127.0.0.1:2', apiMode: 'official' };
  }, request: async (_config, action, payload) => {
    assert.equal(action, oauth ? 'login-begin' : 'browser-route'); assert.equal(payload.scope, 'account-a');
    return { household, scope: 'account-a' };
  }, open: async (executable, args, options) => {
    opened.push({ executable, args, options }); return { code: 0 };
  } };
  assert.equal(await openAccountBrowser(config, url, dependencies), 0);
  assert.equal(opened[0].executable, path.join(houseB, 'bin', 'house-browser'));
  assert.equal(opened[0].options.env.CCPICK_FIXED_EGRESS_HOME, houseB);
  assert.equal(opened[0].options.env.CCPICK_FIXED_EGRESS_PROFILE, 'primary');
  assert(checks.some(value => value.options.requireBrowser === false));
  assert(checks.some(value => value.options.requireBrowser !== false));
  if (!oauth) await assert.rejects(openAccountBrowser(config, url, { ...dependencies,
    request: async () => ({ household, scope: 'account-b' }) }), /account_egress_invalid/);
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
  await assert.rejects(ensureRuntime(config, { ...fixtureOwners, readService: async () => ({ ...service, instanceId: 'expected-instance' }),
    rpc: async () => ({ ok: true, pid: process.pid, instanceId: 'different-instance' }), isAlive: () => true,
    now: () => now, timeoutMs: 100, sleep: async () => { now += 200; }, spawnService: async () => { spawned = true; } }), /runtime_start_timed_out/);
  assert.equal(spawned, false);
});

test('an authenticated legacy service gains supervision without replacing the service', async t => {
  const { config, service } = await fixture(t);
  let launches = 0;
  const deps = { ...fixtureOwners, readService: async () => service, rpc: async () => ({ ok: true, pid: service.pid }),
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
  const result = await ensureRuntime(config, { ...fixtureOwners, readService: async () => ready ? service : null,
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
