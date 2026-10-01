import fs from 'node:fs/promises';
import path from 'node:path';
import { spawn } from 'node:child_process';
import { randomUUID } from 'node:crypto';
import { fileURLToPath } from 'node:url';
import { loadInstall, readJson, atomicJson, regular, fail } from './core.mjs';

export function runtimeProcessAlive(pid) {
  if (!Number.isSafeInteger(pid) || pid < 1) return null;
  try { process.kill(pid, 0); return true; }
  catch (error) { return error.code === 'ESRCH' ? false : error.code === 'EPERM' ? true : null; }
}

export const supervisorEnvironment = (environment = process.env) => Object.fromEntries(
  Object.entries(environment).filter(([key]) => !/^(?:ANTHROPIC_|CLAUDE|CCPICK_(?:ACCOUNT_|RUNTIME_)|NODE_(?:EXTRA_CA_CERTS|OPTIONS|TLS_REJECT_UNAUTHORIZED)$|(?:HTTP|HTTPS|ALL|WS|WSS|NO)_PROXY$)/i.test(key)));

const delay = ms => new Promise(resolve => setTimeout(resolve, ms));
const sameOwner = (a, b) => a?.pid === b?.pid && a?.nonce === b?.nonce;

/** A detached host outlives native wrappers, including retained native tasks.
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
  const sleep = dependencies.sleep ?? delay, now = dependencies.now ?? Date.now;
  const pollMs = dependencies.pollMs ?? 1000;
  const initialRetryMs = dependencies.initialRetryMs ?? 250;
  const maxRetryMs = dependencies.maxRetryMs ?? 30000;
  const stableMs = dependencies.stableMs ?? 60000;
  const config = await readConfig(installation);
  const root = path.resolve(config.dataRoot, '..', 'active-runtime');
  await fs.mkdir(root, { recursive: true, mode: 0o700 }); await regular(root, true);
  const lockFile = path.join(root, 'supervisor.lock');
  const owner = { pid: process.pid, nonce: randomUUID() };
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
      if (alive(previous?.pid) !== false) fail('runtime_supervisor_busy');
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
      if (alive(previous.pid) !== false) return true;
      const again = await readJson(serviceLock, true);
      if (again?.pid !== previous.pid || again?.instanceId !== previous.instanceId) return true;
      await fs.unlink(serviceLock).catch(error => { if (error.code !== 'ENOENT') throw error; });
    }
    const service = await readJson(path.join(root, 'service.json'), true);
    return service && alive(service.pid) !== false;
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
