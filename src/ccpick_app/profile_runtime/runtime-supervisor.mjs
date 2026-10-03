import fs from 'node:fs/promises';
import path from 'node:path';
import { spawn, execFile } from 'node:child_process';
import { randomUUID } from 'node:crypto';
import { fileURLToPath } from 'node:url';
import { promisify } from 'node:util';
import { loadInstall, readJson, atomicJson, regular, fail } from './core.mjs';

export function runtimeProcessAlive(pid) {
  if (!Number.isSafeInteger(pid) || pid < 1) return null;
  try { process.kill(pid, 0); return true; }
  catch (error) { return error.code === 'ESRCH' ? false : error.code === 'EPERM' ? true : null; }
}

const executeFile = promisify(execFile);
let ownIdentity;
async function inspectIdentity(pid) {
  if (runtimeProcessAlive(pid) === false) return false;
  try {
    if (process.platform === 'win32') {
      const powershell = path.join(process.env.WINDIR ?? 'C:/Windows', 'System32/WindowsPowerShell/v1.0/powershell.exe');
      const expression = `^\\s*(?:"[^"]*"|\\S+)\\s+(?:"(?<script>[^"]+\\.(?:mjs|cjs|js))"|(?<script>[^\\s]+\\.(?:mjs|cjs|js)))(?:\\s|$)`;
      const code = `$ErrorActionPreference='Stop'; $processRecord=Get-CimInstance Win32_Process -Filter 'ProcessId=${pid}'; ` +
        `if($null -eq $processRecord){[Console]::Write('null');exit}; $scriptPath=$null; ` +
        `if($processRecord.CommandLine -match '${expression}'){$scriptPath=$Matches['script']}; ` +
        `[ordered]@{pid=[int]$processRecord.ProcessId;createdAt=$processRecord.CreationDate.ToUniversalTime().ToString('O');` +
        `executable=[string]$processRecord.ExecutablePath;script=$scriptPath}|ConvertTo-Json -Compress`;
      const output = await executeFile(powershell, ['-NoProfile', '-NonInteractive', '-Command', code], {
        windowsHide: true, shell: false, timeout: 5000, maxBuffer: 16 * 1024, env: supervisorEnvironment(),
      });
      const result = JSON.parse(output.stdout);
      if (result === null) return false;
      if (result.pid !== pid || !result.createdAt || !path.isAbsolute(result.executable ?? '')) return null;
      return { pid, createdAt: result.createdAt, executable: result.executable,
        script: result.script && path.isAbsolute(result.script) ? result.script : null };
    }
    if (process.platform === 'linux') {
      const [stat, executable, command, boot] = await Promise.all([
        fs.readFile(`/proc/${pid}/stat`, 'utf8'), fs.readlink(`/proc/${pid}/exe`),
        fs.readFile(`/proc/${pid}/cmdline`, 'utf8'), fs.readFile('/proc/sys/kernel/random/boot_id', 'utf8'),
      ]);
      const fields = stat.slice(stat.lastIndexOf(') ') + 2).trim().split(/\s+/);
      if (!/^\d+$/.test(fields[19] ?? '')) return null;
      const argument = command.split('\0')[1];
      return { pid, createdAt: `linux:${boot.trim()}:${fields[19]}`, executable,
        script: argument && path.isAbsolute(argument) ? argument : null };
    }
    if (process.platform === 'darwin') {
      const query = async field => (await executeFile('/bin/ps', ['-p', String(pid), '-o', `${field}=`], {
        timeout: 5000, maxBuffer: 16 * 1024, env: { ...supervisorEnvironment(), TZ: 'UTC', LC_ALL: 'C' },
      })).stdout.trim();
      const [createdAt, executable] = await Promise.all([query('lstart'), query('comm')]);
      if (!createdAt || !path.isAbsolute(executable)) return null;
      return { pid, createdAt: `darwin:${createdAt}`, executable, script: null };
    }
    return null;
  } catch { return runtimeProcessAlive(pid) === false ? false : null; }
}

/** false means positively absent; null means inspection was unavailable. */
export async function runtimeProcessIdentity(pid) {
  if (!Number.isSafeInteger(pid) || pid < 1) return null;
  if (pid !== process.pid) return inspectIdentity(pid);
  if (!ownIdentity) ownIdentity = inspectIdentity(pid);
  const result = await ownIdentity;
  if (result === null) ownIdentity = undefined;
  return result;
}

async function sameExecutable(a, b) {
  const canonical = async value => {
    const resolved = await fs.realpath(value).catch(() => path.resolve(value));
    return process.platform === 'win32' ? resolved.toLowerCase() : resolved;
  };
  return await canonical(a) === await canonical(b);
}

/** Verify generation and role; a living reused PID is not the recorded owner.
 * Legacy records require a known executable+script role. Unknown inspection or
 * incomplete evidence never grants permission to reclaim an owner's lock. */
export async function runtimeOwnerAlive(owner, { isAlive = runtimeProcessAlive,
  inspectProcess = runtimeProcessIdentity, executable, script } = {}) {
  if (!Number.isSafeInteger(owner?.pid) || owner.pid < 1) return null;
  const alive = await isAlive(owner.pid);
  if (alive !== true) return alive === false ? false : null;
  let actual; try { actual = await inspectProcess(owner.pid); } catch { return null; }
  if (actual === false) return false;
  if (!actual || actual.pid !== owner.pid) return null;
  const expected = owner.processIdentity;
  if (expected && (expected.pid !== owner.pid || typeof expected.createdAt !== 'string' || !expected.createdAt ||
      typeof expected.executable !== 'string' || !path.isAbsolute(expected.executable) ||
      (expected.script != null && (typeof expected.script !== 'string' || !path.isAbsolute(expected.script))))) return null;
  if (!expected) {
    if (typeof script !== 'string' || !path.isAbsolute(script) ||
        typeof actual.script !== 'string' || !path.isAbsolute(actual.script)) return null;
    if (!await sameExecutable(actual.script, script)) return false;
    if (typeof executable !== 'string' || !path.isAbsolute(executable) ||
        typeof actual.executable !== 'string' || !path.isAbsolute(actual.executable)) return null;
    return await sameExecutable(actual.executable, executable) ? true : null;
  }
  if (typeof actual.createdAt !== 'string' || !actual.createdAt) return null;
  if (actual.createdAt !== expected.createdAt) return false;
  if (typeof actual.executable !== 'string' || !path.isAbsolute(actual.executable)) return null;
  if (!await sameExecutable(actual.executable, expected.executable)) return false;
  const expectedScript = expected.script;
  if (expectedScript && actual.script) {
    if (typeof actual.script !== 'string' || !path.isAbsolute(actual.script)) return null;
    if (!await sameExecutable(actual.script, expectedScript)) return false;
  }
  return true;
}

export const supervisorEnvironment = (environment = process.env) => Object.fromEntries(
  Object.entries(environment).filter(([key]) => !/^(?:ANTHROPIC_|CLAUDE|CCPICK_(?:ACCOUNT_|RUNTIME_)|NODE_(?:EXTRA_CA_CERTS|OPTIONS|TLS_REJECT_UNAUTHORIZED)$|(?:HTTP|HTTPS|ALL|WS|WSS|NO)_PROXY$)/i.test(key)));

const delay = ms => new Promise(resolve => setTimeout(resolve, ms));
const sameOwner = (a, b) => a?.pid === b?.pid && a?.nonce === b?.nonce;

/** An independently launched host outlives native wrappers and retained tasks.
 * It owns exactly one IPC child. Never signal a PID read from a file: an older
 * unsupervised service is observed until it dies, then safely replaced.
 * Any child exit (even exit 0) restarts while the feature remains enabled.
 * Disable seamlessAccounts to stop intentionally; the IPC shutdown drains the
 * child's admitted mutations before exit. If draining hangs we keep ownership
 * and report stopping, rather than force-killing a credential commit.
 */
export async function superviseRuntime(installation, dependencies = {}) {
  const readConfig = dependencies.loadConfig ?? loadInstall;
  const alive = dependencies.isAlive ?? runtimeProcessAlive;
  const ownerAlive = dependencies.isOwnerAlive ?? (dependencies.isAlive && !dependencies.inspectProcess
    ? owner => alive(owner?.pid)
    : owner => runtimeOwnerAlive(owner, { isAlive: alive, inspectProcess: dependencies.inspectProcess ?? runtimeProcessIdentity,
      executable: process.execPath, script: fileURLToPath(import.meta.url) }));
  const serviceAlive = dependencies.isServiceOwnerAlive ?? (dependencies.isAlive && !dependencies.inspectProcess
    ? owner => alive(owner?.pid)
    : owner => runtimeOwnerAlive(owner, { isAlive: alive, inspectProcess: dependencies.inspectProcess ?? runtimeProcessIdentity,
      executable: process.execPath, script: fileURLToPath(new URL('./runtime-service.mjs', import.meta.url)) }));
  const sleep = dependencies.sleep ?? delay, now = dependencies.now ?? Date.now;
  const pollMs = dependencies.pollMs ?? 1000;
  const initialRetryMs = dependencies.initialRetryMs ?? 250;
  const maxRetryMs = dependencies.maxRetryMs ?? 30000;
  const stableMs = dependencies.stableMs ?? 60000;
  const config = await readConfig(installation);
  const root = path.resolve(config.dataRoot, '..', 'active-runtime');
  await fs.mkdir(root, { recursive: true, mode: 0o700 }); await regular(root, true);
  const lockFile = path.join(root, 'supervisor.lock');
  const captured = await (dependencies.getProcessIdentity ?? runtimeProcessIdentity)(process.pid);
  if (!captured || captured.pid !== process.pid || typeof captured.createdAt !== 'string' || !captured.createdAt ||
      typeof captured.executable !== 'string' || !path.isAbsolute(captured.executable)) fail('runtime_process_identity_unavailable');
  const processIdentity = { pid: captured.pid, createdAt: captured.createdAt, executable: captured.executable,
    script: typeof captured.script === 'string' && path.isAbsolute(captured.script) ? captured.script : null };
  const owner = { pid: process.pid, nonce: randomUUID(), processIdentity };
  for (;;) {
    try {
      const lock = await fs.open(lockFile, 'wx', 0o600);
      try { await lock.writeFile(JSON.stringify(owner)); }
      catch (error) { await lock.close(); await fs.unlink(lockFile).catch(() => {}); throw error; }
      await lock.close(); break;
    } catch (error) {
      if (error.code !== 'EEXIST') throw error;
      let previous;
      try { previous = await readJson(lockFile); } catch { fail('runtime_supervisor_busy'); }
      if (await ownerAlive(previous) !== false) fail('runtime_supervisor_busy');
      if (!sameOwner(await readJson(lockFile, true), previous)) continue;
      await fs.unlink(lockFile).catch(error => { if (error.code !== 'ENOENT') throw error; });
    }
  }

  const defaultSpawn = () => spawn(process.execPath,
    [fileURLToPath(new URL('./runtime-service.mjs', import.meta.url)), path.resolve(installation)],
    { windowsHide: true, shell: false, detached: false, stdio: ['ignore', 'ignore', 'ignore', 'ipc'],
      env: supervisorEnvironment() });
  let stopping = dependencies.signal?.aborted === true, child, childDone, ended, stopSent = false;
  let attempts = 0, startedAt = 0, nextStart = 0, retryMs = initialRetryMs, lastStatus;
  const requestStop = () => { stopping = true; };
  const handlers = [];
  if (dependencies.handleSignals !== false) for (const signal of ['SIGTERM', 'SIGINT']) {
    process.on(signal, requestStop); handlers.push(signal);
  }
  dependencies.signal?.addEventListener('abort', requestStop);
  const status = async (state, reason) => {
    const value = { state, reason, childPid: Number.isSafeInteger(child?.pid) ? child.pid : null,
      attempts, retryMs: state === 'waiting' ? Math.max(0, nextStart - now()) : 0 };
    const serialized = JSON.stringify({ ...value, retryMs: undefined });
    if (serialized === lastStatus) return;
    lastStatus = serialized;
    await atomicJson(path.join(root, 'supervisor.json'), { version: 1, ...owner, ...value,
      updatedAt: new Date().toISOString() }).catch(() => {});
    dependencies.onStatus?.(value);
  };
  async function anotherService() {
    const serviceLock = path.join(root, 'service.lock');
    let previous;
    try { previous = await readJson(serviceLock, true); }
    catch { return true; }
    if (previous) {
      if (await serviceAlive(previous) !== false) return true;
      const again = await readJson(serviceLock, true);
      if (again?.pid !== previous.pid || again?.instanceId !== previous.instanceId) return true;
      await fs.unlink(serviceLock).catch(error => { if (error.code !== 'ENOENT') throw error; });
    }
    const service = await readJson(path.join(root, 'service.json'), true);
    return service && await serviceAlive(service) !== false;
  }
  try {
    for (;;) {
      try { if (!sameOwner(await readJson(lockFile, true), owner)) stopping = true; }
      catch { await status('waiting', 'runtime_supervisor_state_unavailable'); await sleep(pollMs); continue; }
      let usable = false;
      try {
        const latest = await readConfig(installation);
        if (path.resolve(latest.dataRoot, '..', 'active-runtime') !== root) fail('runtime_installation_changed');
        if (latest.seamlessAccounts !== true) stopping = true;
        else usable = true;
      } catch { await status('waiting', 'runtime_supervisor_config_unavailable'); }

      if (child && ended) {
        const wasStable = now() - startedAt >= stableMs;
        child = undefined; childDone = undefined; ended = undefined; stopSent = false;
        if (wasStable) retryMs = initialRetryMs;
        nextStart = now() + retryMs; retryMs = Math.min(maxRetryMs, retryMs * 2);
        await status('waiting', 'runtime_service_exited');
      }
      if (stopping) {
        if (!child) { await status('stopped', 'runtime_supervisor_stopped'); return { stopped: true, attempts }; }
        if (!stopSent) {
          stopSent = true;
          if (child.connected) try { child.send({ type: 'runtime-supervisor-stop' }, () => {}); } catch { /* Wait for close, never force-kill. */ }
        }
        await status('stopping', 'runtime_supervisor_stopping');
      } else if (!child && usable && now() >= nextStart) {
        let occupied = true;
        try { occupied = await anotherService(); }
        catch { await status('waiting', 'runtime_supervisor_state_unavailable'); }
        if (occupied) {
          await status('observing', 'runtime_service_already_running');
        } else {
          attempts++; startedAt = now();
          try {
            child = (dependencies.spawnChild ?? defaultSpawn)();
            child.once('error', () => {});
            childDone = new Promise(resolve => child.once('close', () => {
              ended = { reason: 'runtime_service_exited' }; resolve();
            }));
            await status('running', 'runtime_service_started');
          } catch {
            child = undefined; nextStart = now() + retryMs; retryMs = Math.min(maxRetryMs, retryMs * 2);
            await status('waiting', 'runtime_service_start_failed');
          }
        }
      }
      await sleep(Math.min(pollMs, !child && nextStart > now() ? nextStart - now() : pollMs));
    }
  } finally {
    if (child && !ended) {
      if (child.connected) try { child.send({ type: 'runtime-supervisor-stop' }, () => {}); } catch { /* Preserve ownership while draining. */ }
      await childDone;
    }
    for (const signal of handlers) process.removeListener(signal, requestStop);
    dependencies.signal?.removeEventListener('abort', requestStop);
    if (sameOwner(await readJson(lockFile, true).catch(() => null), owner)) await fs.unlink(lockFile).catch(() => {});
  }
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  try { await superviseRuntime(process.argv[2]); }
  catch { process.stderr.write('runtime_supervisor_unavailable\n'); process.exitCode = 1; }
}
