import fs from 'node:fs/promises';
import path from 'node:path';
import http from 'node:http';
import { spawn } from 'node:child_process';
import { randomUUID } from 'node:crypto';
import { fileURLToPath } from 'node:url';
import { readJson, atomicJson, regular, validName, getProfile, childEnvironment,
  verifyIdentity, checkProfileSettings, nativeArguments, executeNative, fail } from './core.mjs';
import { prepareNetwork } from './network.mjs';
import { supervisorEnvironment } from './runtime-supervisor.mjs';
import { runtimeSettingsEnvironment, syncRuntimeSettings } from './runtime-settings.mjs';

export const runtimeRoot = config => path.resolve(config.dataRoot, '..', 'active-runtime');
export function runtimeScope(value = 'default') {
  if (value !== 'default' && !validName(value)) fail('runtime_scope_invalid');
  return value;
}
export const runtimeDirectory = (config, scope = 'default') => scope === 'default'
  ? path.join(runtimeRoot(config), 'claude') : path.join(runtimeRoot(config), 'scopes', runtimeScope(scope), 'claude');

export function inheritedRuntimeScope(config, environment = process.env) {
  if (environment.CCPICK_ACCOUNT_RUNTIME !== '1') return null;
  if (config.seamlessAccounts !== true || environment.CCPICK_ACCOUNT_PROFILE || !environment.CCPICK_RUNTIME_SCOPE)
    fail('profile_context_conflict');
  const scope = runtimeScope(environment.CCPICK_RUNTIME_SCOPE);
  if (!environment.CLAUDE_CONFIG_DIR || !samePath(config, environment.CLAUDE_CONFIG_DIR, runtimeDirectory(config, scope)))
    fail('profile_context_conflict');
  return scope;
}

function exists(pid) {
  if (!Number.isSafeInteger(pid) || pid < 1) return null;
  try { process.kill(pid, 0); return true; }
  catch (error) { return error.code === 'ESRCH' ? false : error.code === 'EPERM' ? true : null; }
}
const delay = ms => new Promise(resolve => setTimeout(resolve, ms));
const inside = (parent, file) => { const relative = path.relative(parent, file); return relative && !relative.startsWith('..') && !path.isAbsolute(relative); };
const samePath = (config, a, b) => config.platform === 'win32'
  ? path.resolve(a).toLowerCase() === path.resolve(b).toLowerCase() : path.resolve(a) === path.resolve(b);

export async function runtimeServiceInfo(config) {
  const root = runtimeRoot(config);
  const [service, channel] = await Promise.all([readJson(path.join(root, 'service.json'), true), readJson(path.join(root, 'channel-key.json'), true)]);
  if (!service || !channel) return null;
  if (!Number.isSafeInteger(service.pid) || service.pid < 1 ||
      !['controlPort', 'proxyPort'].every(k => Number.isSafeInteger(service[k]) && service[k] > 0 && service[k] < 65536) ||
      !/^[a-f0-9]{64}$/.test(channel.value ?? '') || typeof service.certFile !== 'string' ||
      !path.isAbsolute(service.certFile) || !inside(root, service.certFile)) fail('runtime_service_invalid');
  await regular(service.certFile);
  return { ...service, key: channel.value };
}

export function runtimeRpc(service, action, payload = {}, { timeoutMs = 60000 } = {}) {
  if (!['status', 'register', 'select', 'select-guarded', 'ready', 'login-begin', 'login-finished'].includes(action)) return Promise.reject(new Error('runtime_action_invalid'));
  return new Promise((resolve, reject) => {
    const body = JSON.stringify(payload); let finished = false;
    const end = (error, value) => { if (finished) return; finished = true; clearTimeout(timer); error ? reject(error) : resolve(value); };
    const request = http.request({ host: '127.0.0.1', port: service.controlPort, method: 'POST', path: '/' + action,
      agent: false, headers: { 'content-type': 'application/json', 'content-length': Buffer.byteLength(body), 'x-ccpick-account-runtime': service.key } }, response => {
      const chunks = []; let size = 0;
      response.on('data', chunk => {
        size += chunk.length;
        if (size > 262144) { end(new Error('runtime_response_invalid')); response.destroy(); request.destroy(); }
        else chunks.push(chunk);
      });
      response.once('error', () => end(new Error('runtime_unavailable')));
      response.once('aborted', () => end(new Error('runtime_unavailable')));
      response.once('end', () => {
        let value; try { value = JSON.parse(Buffer.concat(chunks)); } catch { return end(new Error('runtime_response_invalid')); }
        if (action === 'select-guarded' && value?.selectionGuard !== 1) {
          return end(new Error(response.statusCode === 409 && value?.ok === false && value?.reason === 'runtime_unavailable'
            ? 'selection_guard_unsupported' : 'runtime_response_invalid'));
        }
        if (response.statusCode !== 200 || value?.ok !== true) {
          const reason = /^[a-z_]{1,80}$/.test(value?.reason ?? '') ? value.reason : 'runtime_unavailable';
          const allowed = ['login_required', 'wrong_account', 'auth_unverified', 'auth_forbidden', 'auth_rate_limited',
            'network_unavailable', 'selection_changed', 'runtime_unavailable', 'login_in_progress',
            'unclaimed_login_credentials', 'identity_changed', 'credentials_changed'];
          return end(new Error(action === 'select-guarded' && !allowed.includes(reason) ? 'runtime_unavailable' : reason));
        }
        end(null, value);
      });
    });
    const timer = setTimeout(() => { end(new Error('runtime_request_timed_out')); request.destroy(); }, timeoutMs); timer.unref();
    request.once('error', () => end(new Error('runtime_unavailable'))); request.end(body);
  });
}

/** Serialize daemon startup; only a positively dead PID permits stale recovery.
 * Live/unknown owners are never killed, stolen on age, or assumed to be stale. */
export async function ensureRuntime(config, dependencies = {}) {
  if (config.seamlessAccounts !== true) fail('seamless_runtime_disabled');
  const read = dependencies.readService ?? runtimeServiceInfo, rpc = dependencies.rpc ?? runtimeRpc;
  const alive = dependencies.isAlive ?? exists, sleep = dependencies.sleep ?? delay, now = dependencies.now ?? Date.now;
  const timeoutMs = dependencies.timeoutMs ?? 120000, deadline = now() + timeoutMs;
  const root = runtimeRoot(config), file = path.join(root, 'startup.lock'), nonce = randomUUID();
  await fs.mkdir(root, { recursive: true, mode: 0o700 }); await regular(root, true);
  async function ready() {
    const service = await read(config);
    if (!service || alive(service.pid) === false) return null;
    try {
      const result = await rpc(service, 'status', {}, { timeoutMs: 1500 });
      if (result.pid !== undefined && result.pid !== service.pid) return null;
      if (service.instanceId !== undefined && result.instanceId !== service.instanceId) return null;
      return service;
    } catch { return null; }
  }
  const current = await ready();
  const supervisorFile = path.join(root, 'supervisor.lock');
  const supervisorOwner = await readJson(supervisorFile, true).catch(() => null);
  if (current && supervisorOwner && alive(supervisorOwner.pid) === true) return current;
  let owned = false;
  try {
    while (!owned) {
      if (now() >= deadline) fail('runtime_start_timed_out');
      try {
        const lock = await fs.open(file, 'wx', 0o600);
        try { await lock.writeFile(JSON.stringify({ pid: process.pid, nonce })); owned = true; }
        finally { await lock.close(); }
      } catch (error) {
        if (error.code !== 'EEXIST') throw error;
        let owner; try { owner = await readJson(file, true); } catch { /* Writer may not have flushed the lock yet. */ }
        if (owner && alive(owner.pid) === false) {
          const again = await readJson(file, true);
          if (again?.pid === owner.pid && again?.nonce === owner.nonce) await fs.unlink(file).catch(error => { if (error.code !== 'ENOENT') throw error; });
        }
        const service = await ready(); if (service) return service;
        await sleep(100);
      }
    }
    const readyNow = await ready();
    let hostOwner;
    try { hostOwner = await readJson(supervisorFile, true); } catch { fail('runtime_supervisor_busy'); }
    let canSpawn = !hostOwner;
    if (hostOwner && alive(hostOwner.pid) === false) {
      const again = await readJson(supervisorFile, true);
      if (again?.pid === hostOwner.pid && again?.nonce === hostOwner.nonce) {
        await fs.unlink(supervisorFile); canSpawn = true;
      }
    }
    const previous = readyNow ?? await read(config);
    if (!readyNow && previous && alive(previous.pid) !== false) canSpawn = false;
    if (canSpawn) {
      const launch = dependencies.spawnSupervisor ?? dependencies.spawnService ?? (() => {
        const child = spawn(process.execPath, [fileURLToPath(new URL('./runtime-supervisor.mjs', import.meta.url)),
          path.resolve(config.dataRoot, '..', 'install.json')], {
          detached: true, windowsHide: true, stdio: 'ignore', shell: false,
          env: supervisorEnvironment(),
        });
        return new Promise((resolve, reject) => {
          child.once('error', () => reject(new Error('runtime_start_failed')));
          child.once('spawn', () => { child.unref(); resolve(); });
        });
      });
      await launch(config);
    }
    while (now() < deadline) {
      const service = await ready(); if (service) return service;
      await sleep(150);
    }
    fail('runtime_start_timed_out');
  } finally {
    if (owned) {
      const owner = await readJson(file, true).catch(() => null);
      if (owner?.nonce === nonce && owner.pid === process.pid) await fs.unlink(file).catch(() => {});
    }
  }
}

export async function runtimeRequest(config, action, payload = {}, dependencies = {}) {
  if (config.seamlessAccounts !== true) fail('seamless_runtime_disabled');
  const service = dependencies.start === false ? await (dependencies.readService ?? runtimeServiceInfo)(config)
    : await (dependencies.ensureRuntime ?? ensureRuntime)(config, dependencies);
  if (!service) fail('runtime_unavailable');
  return (dependencies.rpc ?? runtimeRpc)(service, action, payload, { timeoutMs: dependencies.timeoutMs ?? 60000 });
}

export function runtimeEnvironment(config, profile, service, scope = 'default', environment = process.env) {
  runtimeScope(scope);
  const proxy = `http://127.0.0.1:${service.proxyPort}`;
  const env = childEnvironment(config, profile, proxy, environment);
  for (const key of Object.keys(env)) if (/^CCPICK_(?:ACCOUNT_|RUNTIME_)/i.test(key)) delete env[key];
  env.CLAUDE_CONFIG_DIR = runtimeDirectory(config, scope);
  env.CCPICK_ACCOUNT_RUNTIME = '1'; env.CCPICK_RUNTIME_SCOPE = scope; env.CCPICK_RUNTIME_CLIENT_ID = randomUUID();
  env.NODE_EXTRA_CA_CERTS = service.certFile;
  env.CCPICK_RUNTIME_INSTALL = path.resolve(config.dataRoot, '..', 'install.json');
  Object.assign(env, runtimeSettingsEnvironment(service, scope));
  return env;
}

export async function launchRuntime(config, name, action, args = [], dependencies = {}) {
  if (config.seamlessAccounts !== true) fail('seamless_runtime_disabled');
  const environment = dependencies.environment ?? process.env;
  const scope = runtimeScope(dependencies.runtimeScope ?? inheritedRuntimeScope(config, environment) ?? name);
  const p = await getProfile(config, name), cwd = dependencies.cwd ?? process.cwd();
  const invoke = dependencies.invoke ?? executeNative, check = dependencies.checkSettings ?? checkProfileSettings;
  await verifyIdentity(config, p);
  const local = action === 'local' || (action === 'run' && args[0] === 'stop');
  const service = local ? await (dependencies.readService ?? runtimeServiceInfo)(config)
    : await (dependencies.ensureRuntime ?? ensureRuntime)(config, dependencies);
  if (!service) fail('runtime_unavailable');
  if (!local) {
    await (dependencies.prepareNetwork ?? prepareNetwork)(config, { checkOnly: true, requireBrowser: action === 'login' });
    const registered = await (dependencies.rpc ?? runtimeRpc)(service, 'register', { scope });
    if (registered.scope !== scope || typeof registered.directory !== 'string' ||
        !samePath(config, registered.directory, runtimeDirectory(config, scope))) fail('runtime_directory_invalid');
  }
  const env = runtimeEnvironment(config, p, service, scope, environment);
  await syncRuntimeSettings(config, service, scope);
  await check(config, { ...p, configDirectory: env.CLAUDE_CONFIG_DIR }, env, { cwd: action === 'login' ? undefined : cwd });
  if (action === 'doctor') {
    const ready = await (dependencies.rpc ?? runtimeRpc)(service, 'ready', { name, scope });
    console.log(JSON.stringify({ ok: true, name, runtimeScope: scope, readyToRun: ready.ready === true, network: '已验证' })); return 0;
  }
  let nativeArgs;
  if (action === 'login') {
    nativeArgs = ['--setting-sources', 'user', 'auth', 'login', '--claudeai',
      ...(dependencies.loginEmail ? ['--email', dependencies.loginEmail] : [])];
  } else if (action === 'local') nativeArgs = ['--setting-sources', 'user', ...args];
  else nativeArgs = nativeArguments(args);
  if (action === 'run' && args[0] === 'stop') {
    let help;
    try { help = await invoke(config.native, nativeArguments(['stop', '--help']), { env, cwd, capture: true, timeoutMs: 10000 }); }
    catch { fail('native_stop_unsupported'); }
    if (help?.code !== 0 || help.timedOut || !/^Usage: claude stop <id>\r?$/m.test(help.stdout ?? '')) fail('native_stop_unsupported');
    if (['--help', '-h'].includes(args[1])) { process.stdout.write(help.stdout); return 0; }
  }
  const directory = path.join(runtimeRoot(config), 'leases'), file = path.join(directory, env.CCPICK_RUNTIME_CLIENT_ID + '.json');
  await fs.mkdir(directory, { recursive: true, mode: 0o700 }).catch(() => {});
  let record, exitCode;
  try {
    const result = await invoke(config.native, nativeArgs, { env, cwd, onSpawn: async childPid => {
      record = { version: 1, clientId: env.CCPICK_RUNTIME_CLIENT_ID, scope, wrapperPid: process.pid, childPid,
        mode: action, startedAt: new Date().toISOString(), status: 'running' };
      await atomicJson(file, record).catch(() => {});
    }, ...(local ? { timeoutMs: args[0] === 'stop' ? 30000 : 10000 } : {}),
    ...(dependencies.quietLogin && action === 'login' ? { capture: true, timeoutMs: 600000 } : {}) });
    exitCode = result.code;
    if (action === 'login' && result.code === 0) {
      await (dependencies.rpc ?? runtimeRpc)(service, 'login-finished', { scope, sessionId: env.CCPICK_RUNTIME_CLIENT_ID }, { timeoutMs: 3000 }).catch(() => {});
    }
    return result.code;
  } finally {
    if (record) await atomicJson(file, { ...record, status: 'exited', exitedAt: new Date().toISOString(), exitCode }).catch(() => {});
  }
}
