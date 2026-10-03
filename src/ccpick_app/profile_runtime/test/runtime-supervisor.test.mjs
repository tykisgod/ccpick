import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import { EventEmitter, once } from 'node:events';
import { spawn } from 'node:child_process';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { atomicJson, readJson } from '../core.mjs';
import { superviseRuntime, runtimeProcessAlive, runtimeProcessIdentity, runtimeOwnerAlive, supervisorEnvironment } from '../runtime-supervisor.mjs';

async function fixture(t) {
  const directory = await fs.mkdtemp(path.join(os.tmpdir(), 'runtime-supervisor-test-'));
  const config = { dataRoot: path.join(directory, 'data'), seamlessAccounts: true };
  const root = path.join(directory, 'active-runtime');
  await fs.mkdir(root, { mode: 0o700 });
  const install = path.join(directory, 'install.json'); await atomicJson(install, config);
  t.after(() => fs.rm(directory, { recursive: true, force: true }));
  return { directory, config, root, install };
}

function fakeChild(pid) {
  const child = new EventEmitter(); child.pid = pid; child.connected = true; child.sent = [];
  child.send = (message, callback) => {
    child.sent.push(message); child.connected = false; callback?.(); child.emit('close', 0, null);
  };
  return child;
}

test('child crash restarts with backoff; disabling drains its IPC child and releases ownership', async t => {
  const { config, root, install } = await fixture(t);
  let time = 0; const children = [], starts = [], states = [];
  const result = await superviseRuntime(install, { loadConfig: async () => config,
    now: () => time, pollMs: 50, initialRetryMs: 100, maxRetryMs: 400, handleSignals: false,
    isAlive: pid => pid === process.pid,
    spawnChild: () => { starts.push(time); const child = fakeChild(10000 + children.length); children.push(child); return child; },
    onStatus: state => states.push(state),
    sleep: async ms => {
      time += ms;
      if (children.length === 1 && children[0].connected) {
        children[0].connected = false;
        await atomicJson(path.join(root, 'service.lock'), { pid: children[0].pid, instanceId: 'crashed-child' });
        await atomicJson(path.join(root, 'service.json'), { pid: children[0].pid });
        children[0].emit('close', 42, null);
      } else if (children.length === 2) config.seamlessAccounts = false;
    },
  });
  assert.equal(result.attempts, 2); assert.ok(starts[1] - starts[0] >= 100);
  assert.deepEqual(children[1].sent, [{ type: 'runtime-supervisor-stop' }]);
  assert.equal(children[0].sent.length, 0); assert.equal(await readJson(path.join(root, 'service.lock'), true), null);
  assert.equal(await readJson(path.join(root, 'supervisor.lock'), true), null);
  assert.equal(states.at(-1).state, 'stopped');
});

test('a live or unknown supervisor is never stolen; only a verified dead owner is reclaimed', async t => {
  const { config, root, install } = await fixture(t), lock = path.join(root, 'supervisor.lock');
  for (const alive of [true, null]) {
    await atomicJson(lock, { pid: 1234567, nonce: 'other-owner' });
    await assert.rejects(superviseRuntime(install, { loadConfig: async () => config, isAlive: () => alive,
      handleSignals: false, spawnChild: () => { throw new Error('must not launch'); } }), /runtime_supervisor_busy/);
    assert.equal((await readJson(lock)).nonce, 'other-owner');
  }
  config.seamlessAccounts = false;
  const result = await superviseRuntime(install, { loadConfig: async () => config, isAlive: () => false, handleSignals: false });
  assert.equal(result.attempts, 0); assert.equal(await readJson(lock, true), null);
});

test('concurrent second host cannot start and a live legacy service is only observed', async t => {
  const { config, root, install } = await fixture(t);
  await atomicJson(path.join(root, 'service.lock'), { pid: process.pid, instanceId: 'existing-legacy' });
  let checked = false, spawned = false;
  await superviseRuntime(install, { loadConfig: async () => config, handleSignals: false,
    isAlive: pid => pid === process.pid,
    spawnChild: () => { spawned = true; throw new Error('must not spawn'); },
    sleep: async () => {
      if (checked) return;
      checked = true;
      await assert.rejects(superviseRuntime(install, { loadConfig: async () => config, handleSignals: false }), /runtime_supervisor_busy/);
      config.seamlessAccounts = false;
    },
  });
  assert.equal(spawned, false);
  assert.equal((await readJson(path.join(root, 'service.lock'))).instanceId, 'existing-legacy');
});

test('startup failures retry at a capped rate and never persist exception text or secrets', async t => {
  const { config, root, install } = await fixture(t);
  let time = 0; const starts = [], states = [];
  await superviseRuntime(install, { loadConfig: async () => config, handleSignals: false,
    now: () => time, pollMs: 1000, initialRetryMs: 100, maxRetryMs: 400,
    spawnChild: () => { starts.push(time); throw new Error('INVENTED PRIVATE TOKEN MUST NEVER BE LOGGED'); },
    onStatus: value => states.push(value), sleep: async ms => {
      time += ms; if (starts.length >= 5) config.seamlessAccounts = false;
    },
  });
  assert.deepEqual(starts, [0, 100, 300, 700, 1100]);
  assert.doesNotMatch(JSON.stringify(states), /PRIVATE|TOKEN|INVENTED/);
  assert.doesNotMatch(await fs.readFile(path.join(root, 'supervisor.json'), 'utf8'), /PRIVATE|TOKEN|INVENTED/);
});

test('daemon environment strips inherited routing, credentials and Node preloads', () => {
  assert.deepEqual(supervisorEnvironment({ PATH: 'safe', HOME: 'safe-home', ANTHROPIC_API_KEY: 'secret',
    CLAUDE_CONFIG_DIR: 'wrong', CCPICK_RUNTIME_SCOPE: 'wrong', CCPICK_ACCOUNT_PROFILE: 'wrong',
    NODE_EXTRA_CA_CERTS: 'wrong', NODE_OPTIONS: '--inspect', NODE_TLS_REJECT_UNAUTHORIZED: '0',
    HTTP_PROXY: 'wrong', https_proxy: 'wrong', ALL_PROXY: 'wrong', NO_PROXY: '*', WS_PROXY: 'wrong', WSS_PROXY: 'wrong' }),
  { PATH: 'safe', HOME: 'safe-home' });
  assert.equal(runtimeProcessAlive(process.pid), true); assert.equal(runtimeProcessAlive(-1), null);
});

test('owner generation and executable reject reused live PIDs without exposing command lines', async () => {
  const identity = { pid: 731, createdAt: '2026-10-03T00:00:00.1234567Z', executable: process.execPath,
    script: fileURLToPath(new URL('../runtime-supervisor.mjs', import.meta.url)) };
  const owner = { pid: identity.pid, nonce: 'fixture-owner', processIdentity: identity };
  const check = actual => runtimeOwnerAlive(owner, { isAlive: () => true, inspectProcess: async () => actual });
  assert.equal(await check({ ...identity }), true);
  assert.equal(await check({ ...identity, createdAt: '2026-10-03T00:01:00.1234567Z' }), false);
  assert.equal(await check({ ...identity, executable: path.join(path.dirname(process.execPath), 'unrelated-node.exe') }), false);
  assert.equal(await check({ ...identity, script: path.join(path.dirname(identity.script), 'other-script.mjs') }), false);
  assert.equal(await check({ ...identity, script: null }), true);
  assert.equal(await check(null), null);
  assert.equal(await check(false), false);
  assert.equal(await check({ ...identity, executable: '' }), null);
  assert.equal(await runtimeOwnerAlive(owner, { isAlive: () => null, inspectProcess: async () => identity }), null);
  assert.equal(await runtimeOwnerAlive(owner, { isAlive: () => false, inspectProcess: async () => { throw Error('must not inspect'); } }), false);
});

test('legacy live locks require positive role evidence; missing identity never proves staleness', async () => {
  const script = fileURLToPath(new URL('../runtime-supervisor.mjs', import.meta.url));
  const actual = { pid: 732, createdAt: 'fixture-birth', executable: process.execPath, script };
  const options = { isAlive: () => true, executable: process.execPath, script, inspectProcess: async () => actual };
  assert.equal(await runtimeOwnerAlive({ pid: actual.pid }, options), true);
  assert.equal(await runtimeOwnerAlive({ pid: actual.pid }, { ...options, inspectProcess: async () => ({ ...actual, script: null }) }), null);
  assert.equal(await runtimeOwnerAlive({ pid: actual.pid }, { ...options,
    inspectProcess: async () => ({ ...actual, executable: path.join(path.dirname(process.execPath), 'previous-node.exe') }) }), null);
  assert.equal(await runtimeOwnerAlive({ pid: actual.pid }, { ...options,
    inspectProcess: async () => ({ ...actual, executable: path.join(path.dirname(process.execPath), 'cua-node.exe'),
      script: path.join(path.dirname(script), 'unrelated-app.mjs') }) }), false);
  assert.equal(await runtimeOwnerAlive({ pid: actual.pid, processIdentity: { ...actual, executable: 'relative-path' } }, options), null);
  assert.equal(await runtimeOwnerAlive({ pid: -1 }, options), null);
});

test('Node upgrade preserves a live legacy supervisor when its executable differs from current configuration', async t => {
  const { config, root, install } = await fixture(t), lock = path.join(root, 'supervisor.lock');
  const original = { pid: 735, nonce: 'legacy-node-before-upgrade' }; await atomicJson(lock, original);
  const script = fileURLToPath(new URL('../runtime-supervisor.mjs', import.meta.url));
  const actual = { pid: original.pid, createdAt: 'legacy-process-birth',
    executable: path.join(path.dirname(process.execPath), 'previous-node.exe'), script };
  await assert.rejects(superviseRuntime(install, { loadConfig: async () => config, handleSignals: false,
    isAlive: () => true, inspectProcess: async () => actual }), /runtime_supervisor_busy/);
  assert.deepEqual(await readJson(lock), original);
});

test('reused supervisor PID is reclaimed only with verified process evidence and new owner records its birth', async t => {
  const { config, root, install } = await fixture(t), lock = path.join(root, 'supervisor.lock');
  const script = fileURLToPath(new URL('../runtime-supervisor.mjs', import.meta.url));
  const own = { pid: process.pid, createdAt: 'fixture-own-birth', executable: process.execPath, script };
  const original = { pid: 733, nonce: 'original-owner', processIdentity: { ...own, pid: 733, createdAt: 'old-birth' } };
  await atomicJson(lock, original);
  let inspections = 0;
  config.seamlessAccounts = false;
  const result = await superviseRuntime(install, { loadConfig: async () => config, handleSignals: false,
    isAlive: () => true, getProcessIdentity: async () => own,
    inspectProcess: async () => { inspections++; return { ...original.processIdentity, createdAt: 'reused-birth' }; } });
  assert.equal(inspections, 1); assert.equal(result.attempts, 0);
  assert.equal(await readJson(lock, true), null);
  assert.deepEqual((await readJson(path.join(root, 'supervisor.json'))).processIdentity, own);
  assert.doesNotMatch(await fs.readFile(path.join(root, 'supervisor.json'), 'utf8'), /commandLine/i);
});

test('unknown external process identity preserves the original supervisor lock', async t => {
  const { config, root, install } = await fixture(t), lock = path.join(root, 'supervisor.lock');
  const original = { pid: 734, nonce: 'unknown-owner' }; await atomicJson(lock, original);
  await assert.rejects(superviseRuntime(install, { loadConfig: async () => config, handleSignals: false,
    isAlive: () => true, inspectProcess: async () => null }), /runtime_supervisor_busy/);
  assert.deepEqual(await readJson(lock), original);
});

test('current process inspection records birth and executable but no complete argv', async () => {
  const identity = await runtimeProcessIdentity(process.pid);
  assert.equal(identity.pid, process.pid); assert.ok(identity.createdAt);
  assert.ok(path.isAbsolute(identity.executable)); assert.equal(identity.commandLine, undefined);
  assert.equal(await runtimeOwnerAlive({ pid: process.pid, processIdentity: identity }), true);
});

test('real detached host recovers a crashed child after its launching wrapper exits', { timeout: 15000 }, async t => {
  const { config, root, install, directory } = await fixture({ after() {} });
  const moduleUrl = pathToFileURL(fileURLToPath(new URL('../runtime-supervisor.mjs', import.meta.url))).href;
  const worker = path.join(directory, 'worker.mjs'), host = path.join(directory, 'host.mjs');
  await fs.writeFile(worker, `import fs from 'node:fs/promises';
    const file = process.argv[2]; let count = 0;
    try { count = Number(await fs.readFile(file, 'utf8')); } catch {}
    await fs.writeFile(file, String(count + 1));
    if (!count) process.exit(42);
    process.on('message', message => { if (message.type === 'runtime-supervisor-stop') process.exit(0); });
    process.on('disconnect', () => process.exit(0));
    setInterval(() => {}, 1000);
  `, { mode: 0o600 });
  await fs.writeFile(host, `import fs from 'node:fs/promises'; import { spawn } from 'node:child_process';
    import { superviseRuntime } from ${JSON.stringify(moduleUrl)};
    await superviseRuntime(process.argv[2], { loadConfig: async file => JSON.parse(await fs.readFile(file, 'utf8')),
      pollMs: 25, initialRetryMs: 50, maxRetryMs: 100,
      spawnChild: () => spawn(process.execPath, [${JSON.stringify(worker)}, ${JSON.stringify(path.join(directory, 'starts'))}],
        { windowsHide: true, shell: false, stdio: ['ignore', 'ignore', 'ignore', 'ipc'] }) });
    await fs.writeFile(${JSON.stringify(path.join(directory, 'stopped'))}, 'yes');
  `, { mode: 0o600 });
  const launcher = spawn(process.execPath, ['--input-type=module', '-e',
    `import { spawn } from 'node:child_process';
      const child = spawn(process.execPath, ${JSON.stringify([host, install])},
        { detached: true, windowsHide: true, stdio: 'ignore', shell: false });
      child.on('spawn', () => child.unref());`], { windowsHide: true, stdio: 'ignore', shell: false });
  assert.equal((await once(launcher, 'close'))[0], 0);
  const waitUntil = async predicate => {
    const deadline = Date.now() + 8000;
    while (Date.now() < deadline) { if (await predicate()) return; await new Promise(resolve => setTimeout(resolve, 30)); }
    throw new Error('fixture did not settle');
  };
  t.after(async () => { await atomicJson(install, { ...config, seamlessAccounts: false });
    await waitUntil(async () => (await readJson(path.join(root, 'supervisor.lock'), true)) === null);
    await fs.rm(directory, { recursive: true, force: true }); });
  await waitUntil(async () => Number(await fs.readFile(path.join(directory, 'starts'), 'utf8').catch(() => '0')) >= 2);
  const owner = await readJson(path.join(root, 'supervisor.lock'));
  assert.equal(runtimeProcessAlive(owner.pid), true);
  assert.equal(launcher.exitCode, 0);
  await atomicJson(install, { ...config, seamlessAccounts: false });
  await waitUntil(async () => (await fs.readFile(path.join(directory, 'stopped'), 'utf8').catch(() => '')) === 'yes');
  assert.equal(await readJson(path.join(root, 'supervisor.lock'), true), null);
  assert.equal((await readJson(path.join(root, 'supervisor.json'))).state, 'stopped');
});
