import fs from 'node:fs/promises';
import { createReadStream, constants } from 'node:fs';
import path from 'node:path';
import os from 'node:os';
import { createHash, randomUUID } from 'node:crypto';
import { atomicJson, executeNative, fail, lease, listProfiles, readJson, regular, verifyIdentity, withLeaseLock } from './core.mjs';

export const SHARED_HISTORY_DIRECTORIES = ['projects', 'file-history'];
const defaults = () => path.join(os.homedir(), '.claude');
async function sharedBase(config, options) {
  if (options.baseDirectory) return options.baseDirectory;
  const state = await readJson(path.resolve(config.dataRoot, '..', 'state.json'), true);
  return state?.bootstrapEmail ? path.resolve(config.dataRoot, '..', 'shared-history') : defaults();
}
const samePath = (a, b) => process.platform === 'win32'
  ? path.resolve(a).toLowerCase() === path.resolve(b).toLowerCase()
  : path.resolve(a) === path.resolve(b);
async function stat(file) {
  try { return await fs.lstat(file); }
  catch (e) { if (e.code === 'ENOENT') return null; throw e; }
}
async function directory(file, create = false) {
  if (create) await fs.mkdir(file, { recursive: true, mode: 0o700 });
  const info = await fs.lstat(file);
  if (!info.isDirectory() || info.isSymbolicLink() || !samePath(await fs.realpath(file), file)) fail('unsafe_history_path');
}
async function hash(file) {
  const digest = createHash('sha256');
  for await (const chunk of createReadStream(file)) digest.update(chunk);
  return digest.digest('hex');
}
async function emptyTree(dir) {
  for (const entry of await fs.readdir(dir, { withFileTypes: true })) {
    if (entry.isSymbolicLink() || !entry.isDirectory() || !await emptyTree(path.join(dir, entry.name))) return false;
  }
  return true;
}
async function removeEmptyTree(dir) {
  for (const entry of await fs.readdir(dir, { withFileTypes: true })) {
    if (entry.isSymbolicLink() || !entry.isDirectory()) fail('history_changed');
    await removeEmptyTree(path.join(dir, entry.name));
  }
  await fs.rmdir(dir); // Never recursive: any newly written file stops removal.
}
async function kind(target, shared) {
  if (samePath(target, shared)) {
    const info = await stat(target);
    if (info) await directory(target);
    return 'shared';
  }
  const info = await stat(target);
  if (!info) return 'missing';
  if (info.isSymbolicLink()) {
    if (!samePath(await fs.realpath(target), shared)) fail('history_link_conflict');
    return 'shared';
  }
  await directory(target);
  return await emptyTree(target) ? 'empty' : 'private';
}
export async function historyStatus(config, options = {}) {
  const baseDirectory = await sharedBase(config, options);
  const profiles = [];
  for (const p of await listProfiles(config)) {
    const resources = {};
    for (const name of SHARED_HISTORY_DIRECTORIES)
      resources[name] = await kind(path.join(p.configDirectory, name), path.join(baseDirectory, name));
    profiles.push({ name: p.name, resources });
  }
  const shared = profiles.every(p => Object.values(p.resources).every(v => v === 'shared'));
  return { status: shared ? 'shared' : 'needs-migration', baseDirectory, shared, profiles };
}
async function makeShared(baseDirectory) {
  await directory(baseDirectory, true);
  for (const name of SHARED_HISTORY_DIRECTORIES) await directory(path.join(baseDirectory, name), true);
}
async function checkInterruptedMigration(config) {
  const backups = path.resolve(config.dataRoot, '..', 'history-backups');
  if (!await stat(backups)) return;
  await directory(backups);
  for (const entry of await fs.readdir(backups)) {
    const journal = await readJson(path.join(backups, entry, 'migration.json'), true);
    if (!journal || !['complete', 'rolled-back'].includes(journal.status)) fail('history_recovery_required');
  }
}
async function link(shared, target) {
  await fs.symlink(shared, target, process.platform === 'win32' ? 'junction' : 'dir');
}

export async function prepareHistory(config, p, options = {}) {
  const baseDirectory = await sharedBase(config, options);
  await regular(p.configDirectory, true);
  return withLeaseLock(p, async records => {
    await checkInterruptedMigration(config);
    await makeShared(baseDirectory);
    const pending = [];
    for (const name of SHARED_HISTORY_DIRECTORIES) {
      const target = path.join(p.configDirectory, name), shared = path.join(baseDirectory, name);
      const state = await kind(target, shared);
      if (state === 'private') fail('history_migration_required');
      if (state !== 'shared') pending.push({ target, shared, state });
    }
    if (pending.length && records.length) fail('history_busy');
    for (const { target, shared, state } of pending) {
      if (state === 'empty') await removeEmptyTree(target);
      try { await link(shared, target); }
      catch (e) { if (state === 'empty') await fs.mkdir(target, { mode: 0o700 }); throw e; }
    }
  });
}

export async function checkNoNativeProcesses(config) {
  const windows = process.platform === 'win32';
  const executable = windows
    ? path.join(process.env.WINDIR || 'C:/Windows', 'System32/WindowsPowerShell/v1.0/powershell.exe') : '/bin/ps';
  const args = windows ? ['-NoProfile', '-Command',
    "ConvertTo-Json -InputObject @(Get-Process -Name claude -ErrorAction SilentlyContinue | Select-Object -ExpandProperty Id) -Compress; exit 0"]
    : ['-axo', 'pid=,comm='];
  const result = await executeNative(executable, args, { capture: true, timeoutMs: 20000 });
  if (windows) {
    let pids;
    try { pids = JSON.parse(result.stdout); } catch { fail('process_scan_failed'); }
    if (!Array.isArray(pids) || pids.some(pid => !Number.isSafeInteger(pid))) fail('process_scan_failed');
    if (pids.length) fail('active_native_sessions');
  } else {
    if (result.code !== 0) fail('process_scan_failed');
    if (result.stdout.split('\n').some(row => {
      const match = row.trim().match(/^\d+\s+(.+)$/);
      return match && (path.basename(match[1]) === 'claude' || samePath(match[1], config.native));
    })) fail('active_native_sessions');
  }
}

async function inventory(root, { preserveLinks = false } = {}) {
  const files = new Map(), directories = new Set();
  async function visit(current, relative = '') {
    for (const entry of await fs.readdir(current, { withFileTypes: true })) {
      const file = path.join(current, entry.name), key = path.join(relative, entry.name);
      if (entry.isSymbolicLink()) {
        if (preserveLinks) continue;
        fail('unsafe_history_path');
      }
      if (entry.isDirectory()) { directories.add(key); await visit(file, key); }
      else if (entry.isFile()) files.set(key, { source: file, hash: await hash(file) });
      else fail('unsafe_history_path');
    }
  }
  await directory(root); await visit(root);
  return { files, directories };
}

async function historyDestination(root, key, { directory = false, create = false } = {}) {
  const parts = key.split(path.sep);
  let current = root;
  for (const [index, part] of parts.entries()) {
    current = path.join(current, part);
    let info = await stat(current);
    if (!info && create) {
      try { await fs.mkdir(current, { mode: 0o700 }); }
      catch (e) { if (e.code !== 'EEXIST') throw e; }
      info = await fs.lstat(current);
    }
    if (!info) break;
    const expectsDirectory = directory || index < parts.length - 1;
    if (info.isSymbolicLink() || (expectsDirectory ? !info.isDirectory() : !info.isFile()))
      fail('history_conflict');
  }
  return path.join(root, key);
}

export async function migrateHistory(config, options = {}) {
  const baseDirectory = await sharedBase(config, options);
  const checkProcesses = options.checkProcesses ?? checkNoNativeProcesses;
  const profiles = await listProfiles(config), tokens = [];
  const changes = [], additions = [];
  const root = path.resolve(config.dataRoot, '..');
  let backup, journal;
  try {
    await checkInterruptedMigration(config);
    if ((await historyStatus(config, { baseDirectory })).shared)
      return { ok: true, alreadyShared: true, copiedFiles: 0, linkedDirectories: 0 };
    for (const p of profiles) tokens.push(await lease(p, 'login'));
    await checkInterruptedMigration(config);
    await checkProcesses(config);
    for (const p of profiles) await verifyIdentity(config, p);
    await makeShared(baseDirectory);
    const plans = [];
    for (const name of SHARED_HISTORY_DIRECTORIES) {
      const shared = path.join(baseDirectory, name);
      const merged = await inventory(shared, { preserveLinks: true });
      const original = new Set(merged.files.keys());
      const sources = [];
      for (const p of profiles) {
        const target = path.join(p.configDirectory, name), state = await kind(target, shared);
        if (state === 'shared') continue;
        const relative = path.relative(path.resolve(config.dataRoot), path.resolve(target));
        if (!relative || relative.startsWith('..') || path.isAbsolute(relative)) fail('unsafe_history_path');
        sources.push({ profile: p.name, name, target, shared, exists: state !== 'missing' });
        if (state === 'missing') continue;
        const tree = await inventory(target);
        for (const key of tree.directories) {
          await historyDestination(shared, key, { directory: true });
          if (merged.files.has(key)) fail('history_conflict');
          merged.directories.add(key);
        }
        for (const [key, file] of tree.files) {
          await historyDestination(shared, key);
          if (merged.directories.has(key) || (merged.files.has(key) && merged.files.get(key).hash !== file.hash)) fail('history_conflict');
          if (!merged.files.has(key)) merged.files.set(key, file);
        }
      }
      plans.push({ shared, ...merged, original, sources });
    }
    if (!plans.some(plan => plan.sources.length)) return { ok: true, alreadyShared: true, copiedFiles: 0, linkedDirectories: 0 };
    const backupRoot = path.join(root, 'history-backups');
    await directory(backupRoot, true);
    backup = path.join(backupRoot, new Date().toISOString().replace(/[:.]/g, '-') + '-' + randomUUID());
    await fs.mkdir(backup, { mode: 0o700 });
    journal = { version: 1, status: 'copying', baseDirectory, changes, additions, pendingCopy: null };
    await atomicJson(path.join(backup, 'migration.json'), journal);
    for (const plan of plans) {
      for (const key of [...plan.directories].sort((a, b) => a.length - b.length))
        await historyDestination(plan.shared, key, { directory: true, create: true });
      for (const [key, file] of plan.files) {
        if (plan.original.has(key)) continue;
        const destination = await historyDestination(plan.shared, key);
        const temporary = destination + '.ccpick-history-' + randomUUID() + '.tmp';
        journal.pendingCopy = temporary;
        await atomicJson(path.join(backup, 'migration.json'), journal);
        try {
          await fs.copyFile(file.source, temporary, constants.COPYFILE_EXCL);
          if (await hash(temporary) !== file.hash || await hash(file.source) !== file.hash) fail('history_changed');
          await fs.link(temporary, destination);
          additions.push({ path: destination, hash: file.hash });
        } finally { await fs.unlink(temporary).catch(e => { if (e.code !== 'ENOENT') throw e; }); }
        journal.pendingCopy = null;
      }
    }
    journal.status = 'linking';
    await atomicJson(path.join(backup, 'migration.json'), journal);
    for (const plan of plans) for (const item of plan.sources) {
      const saved = item.exists ? path.join(backup, `${item.profile}-${item.name}`) : null;
      const change = { ...item, backup: saved, moved: false, linked: false };
      changes.push(change);
      await atomicJson(path.join(backup, 'migration.json'), journal);
      if (saved) { await fs.rename(item.target, saved); change.moved = true; }
      await atomicJson(path.join(backup, 'migration.json'), journal);
      await link(item.shared, item.target); change.linked = true;
      await atomicJson(path.join(backup, 'migration.json'), journal);
    }
    for (const p of profiles) await verifyIdentity(config, p);
    if (!(await historyStatus(config, { baseDirectory })).shared) fail('history_verification_failed');
    journal.status = 'complete';
    await atomicJson(path.join(backup, 'migration.json'), journal);
    return { ok: true, copiedFiles: additions.length, linkedDirectories: changes.length, backup };
  } catch (error) {
    let restored = true;
    if (journal?.pendingCopy) {
      try { await fs.unlink(journal.pendingCopy); journal.pendingCopy = null; }
      catch (e) { if (e.code === 'ENOENT') journal.pendingCopy = null; else restored = false; }
    }
    for (const item of [...changes].reverse()) {
      try {
        if (item.linked) {
          if (await kind(item.target, item.shared) !== 'shared') fail('history_link_conflict');
          await fs.unlink(item.target);
        }
        if (item.moved) await fs.rename(item.backup, item.target);
      } catch { restored = false; }
    }
    if (journal) {
      journal.status = restored ? 'rolled-back' : 'recovery-required';
      await atomicJson(path.join(backup, 'migration.json'), journal);
    }
    if (!restored) fail('history_recovery_required');
    throw error;
  } finally { for (const token of tokens.reverse()) await token.release(); }
}
