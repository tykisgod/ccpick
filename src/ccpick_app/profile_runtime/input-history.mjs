import os from 'node:os';
import fs from 'node:fs/promises';
import path from 'node:path';
import { createHash, randomUUID } from 'node:crypto';
import { performance } from 'node:perf_hooks';
import { listProfiles } from './core.mjs';

export const INPUT_HISTORY_LIMITS = Object.freeze({
  sourceBytes: 4 * 1024 * 1024, importedRecords: 1000,
  targetBytes: 64 * 1024 * 1024, pasteBytes: 4 * 1024 * 1024, totalPasteBytes: 16 * 1024 * 1024,
});
const HASH = /^[a-f0-9]{16}$/;
const utf8 = new TextDecoder('utf-8', { fatal: true });
const object = value => value && typeof value === 'object' && !Array.isArray(value);
const failure = reason => Object.assign(new Error(reason), { code: reason });
const samePath = (a, b) => process.platform === 'win32'
  ? path.resolve(a).toLowerCase() === path.resolve(b).toLowerCase() : path.resolve(a) === path.resolve(b);
const sameFile = (a, b) => a.ino === b.ino && a.dev === b.dev && a.birthtimeMs === b.birthtimeMs;
const sameVersion = (a, b) => sameFile(a, b) && a.size === b.size && a.mtimeMs === b.mtimeMs && a.ctimeMs === b.ctimeMs;
const delay = ms => new Promise(resolve => setTimeout(resolve, ms));
const hash = bytes => createHash('sha256').update(bytes).digest('hex');

async function regular(file, directory = false) {
  const info = await fs.lstat(file);
  if (info.isSymbolicLink() || !(directory ? info.isDirectory() : info.isFile()) ||
      !samePath(await fs.realpath(file), file)) throw failure('unsafe_input_history_path');
  return info;
}
async function optionalStat(file) {
  try { return await regular(file); }
  catch (error) { if (error.code === 'ENOENT') return null; throw error; }
}
function canonical(value) {
  if (Array.isArray(value)) return '[' + value.map(canonical).join(',') + ']';
  if (object(value)) return '{' + Object.keys(value).sort().map(key => JSON.stringify(key) + ':' + canonical(value[key])).join(',') + '}';
  return JSON.stringify(value);
}
function parseRecord(line) {
  const record = JSON.parse(line);
  if (!object(record) || typeof record.display !== 'string' || typeof record.project !== 'string' ||
      !Number.isFinite(record.timestamp) || record.timestamp < 0 || !object(record.pastedContents) ||
      (record.sessionId !== undefined && typeof record.sessionId !== 'string')) throw failure('input_history_invalid');
  return record;
}

async function withNativeLock(file, action, timeoutMs) {
  const lock = file + '.lock', deadline = performance.now() + timeoutMs;
  const transient = error => process.platform === 'win32' && ['EPERM', 'EACCES', 'EBUSY'].includes(error.code);
  for (;;) {
    try { await fs.mkdir(lock, { mode: 0o700 }); break; }
    catch (error) {
      if (error.code === 'EEXIST') {
        try { await regular(lock, true); }
        catch (inspectionError) {
          if (inspectionError.code !== 'ENOENT' && !transient(inspectionError)) throw inspectionError;
        }
      } else if (!transient(error)) throw error;
      if (performance.now() >= deadline) throw failure('input_history_busy');
      await delay(Math.min(50, Math.max(1, deadline - performance.now())));
    }
  }
  const owner = await regular(lock, true);
  let compromised = false, pending = Promise.resolve();
  const verify = async () => {
    if (compromised || !sameFile(owner, await regular(lock, true))) throw failure('input_history_lock_changed');
  };
  const timer = setInterval(() => {
    pending = pending.then(async () => {
      await verify();
      const now = new Date(); await fs.utimes(lock, now, now);
    }).catch(() => { compromised = true; });
  }, 1000);
  timer.unref();
  try { return await action(verify); }
  finally {
    clearInterval(timer); await pending;
    const current = await fs.lstat(lock).catch(error => { if (error.code !== 'ENOENT') throw error; return null; });
    if (current && sameFile(owner, current)) await fs.rmdir(lock);
  }
}

async function readHistory(directory, { tail = false, limits } = {}) {
  try { await regular(directory, true); }
  catch (error) { if (tail && error.code === 'ENOENT') return { records: [], bytes: Buffer.alloc(0), info: null }; throw error; }
  const file = path.join(directory, 'history.jsonl'), info = await optionalStat(file);
  if (!info) return { records: [], bytes: Buffer.alloc(0), info: null };
  if (!tail && info.size > limits.targetBytes) throw failure('input_history_too_large');
  const handle = await fs.open(file, 'r');
  try {
    const opened = await handle.stat();
    if (!sameFile(info, opened) || !opened.isFile()) throw failure('input_history_changed');
    const start = tail ? Math.max(0, opened.size - limits.sourceBytes) : 0;
    const bytes = Buffer.alloc(opened.size - start);
    const { bytesRead } = await handle.read(bytes, 0, bytes.length, start);
    if (bytesRead !== bytes.length) throw failure('input_history_changed');
    let view = bytes;
    if (start > 0) {
      const boundary = view.indexOf(10);
      view = boundary < 0 ? Buffer.alloc(0) : view.subarray(boundary + 1);
    }
    if (tail && view.length && view.at(-1) !== 10) {
      const end = view.lastIndexOf(10); view = end < 0 ? Buffer.alloc(0) : view.subarray(0, end + 1);
    }
    let text;
    try { text = utf8.decode(view); }
    catch { if (tail) return { records: [], bytes, info: opened }; throw failure('input_history_invalid'); }
    const records = [];
    for (const line of text.split('\n')) {
      if (!line.trim()) continue;
      try { records.push({ record: parseRecord(line), line }); }
      catch { if (!tail) throw failure('input_history_invalid'); }
    }
    return { records, bytes, info: opened };
  } finally { await handle.close(); }
}

async function readPaste(directory, id, limits) {
  const folder = path.join(directory, 'paste-cache');
  await regular(folder, true);
  const file = path.join(folder, `${id}.txt`), info = await regular(file);
  if (info.size > limits.pasteBytes) throw failure('input_history_paste_unavailable');
  const handle = await fs.open(file, 'r');
  try {
    const opened = await handle.stat();
    if (!sameFile(info, opened) || opened.size !== info.size) throw failure('input_history_paste_unavailable');
    const buffer = Buffer.alloc(info.size + 1);
    const { bytesRead } = await handle.read(buffer, 0, buffer.length, 0);
    const bytes = buffer.subarray(0, bytesRead);
    if (bytesRead !== info.size || hash(bytes).slice(0, 16) !== id) throw failure('input_history_paste_unavailable');
    try { utf8.decode(bytes); } catch { throw failure('input_history_paste_unavailable'); }
    return bytes;
  } finally { await handle.close(); }
}
async function publishExclusive(file, bytes) {
  const temporary = file + '.' + randomUUID() + '.tmp';
  try {
    await fs.writeFile(temporary, bytes, { mode: 0o600, flag: 'wx' });
    await fs.link(temporary, file);
  } finally { await fs.unlink(temporary).catch(error => { if (error.code !== 'ENOENT') throw error; }); }
}
async function ensurePastes(item, target, state, limits) {
  for (const value of Object.values(item.record.pastedContents)) {
    if (!object(value) || value.contentHash === undefined) continue;
    const id = value.contentHash;
    if (typeof id !== 'string' || !HASH.test(id)) return false;
    if (state.verified.has(id)) continue;
    try {
      try { await readPaste(target, id, limits); state.verified.add(id); continue; }
      catch (error) { if (error.code !== 'ENOENT') throw error; }
      let bytes;
      for (const directory of item.directories) {
        try { bytes = await readPaste(directory, id, limits); break; }
        catch (error) {
          if (!['ENOENT', 'input_history_paste_unavailable'].includes(error.code)) throw error;
        }
      }
      if (!bytes) return false;
      if (state.bytes + bytes.length > limits.totalPasteBytes) return false;
      const folder = path.join(target, 'paste-cache');
      try { await fs.mkdir(folder, { mode: 0o700 }); } catch (error) { if (error.code !== 'EEXIST') throw error; }
      await regular(folder, true);
      try { await publishExclusive(path.join(folder, `${id}.txt`), bytes); state.copied++; state.bytes += bytes.length; }
      catch (error) { if (error.code !== 'EEXIST') throw error; }
      await readPaste(target, id, limits); state.verified.add(id);
    } catch (error) {
      if (['ENOENT', 'input_history_paste_unavailable'].includes(error.code)) return false;
      throw error;
    }
  }
  return true;
}
async function replace(file, bytes, verify) {
  const temporary = file + '.' + randomUUID() + '.tmp';
  try {
    await fs.writeFile(temporary, bytes, { flag: 'wx', mode: 0o600 });
    for (let attempt = 0; ; attempt++) {
      await verify();
      try { await fs.rename(temporary, file); break; }
      catch (error) {
        if (process.platform !== 'win32' || !['EPERM', 'EACCES', 'EBUSY'].includes(error.code) || attempt >= 7) throw error;
        await delay(25 * (attempt + 1));
      }
    }
  } finally { await fs.unlink(temporary).catch(error => { if (error.code !== 'ENOENT') throw error; }); }
}

export async function prepareInputHistory(config, target, options = {}) {
  const limits = { ...INPUT_HISTORY_LIMITS, ...options.limits };
  const candidates = new Map(), directories = new Set();
  const sources = await listProfiles(config);
  if (config.legacyInputHistory === true) sources.push({ configDirectory: path.join(os.homedir(), '.claude') });
  for (const profile of sources) {
    if (samePath(profile.configDirectory, target.configDirectory)) continue;
    const key = process.platform === 'win32' ? path.resolve(profile.configDirectory).toLowerCase() : path.resolve(profile.configDirectory);
    if (directories.has(key)) continue;
    directories.add(key);
    const source = await readHistory(profile.configDirectory, { tail: true, limits });
    for (const item of source.records) {
      const id = canonical(item.record);
      const previous = candidates.get(id);
      if (previous) previous.directories.push(profile.configDirectory);
      else candidates.set(id, { ...item, id, directories: [profile.configDirectory] });
    }
    if (candidates.size > limits.importedRecords) {
      const keep = new Set([...candidates.values()].sort((a, b) => b.record.timestamp - a.record.timestamp)
        .slice(0, limits.importedRecords).map(item => item.id));
      for (const id of candidates.keys()) if (!keep.has(id)) candidates.delete(id);
    }
  }
  if (!candidates.size) return { imported: 0, copiedPastes: 0, skippedAttachments: 0, backup: null };
  await regular(target.configDirectory, true);
  const file = path.join(target.configDirectory, 'history.jsonl');
  if (!await optionalStat(file)) {
    const handle = await fs.open(file, 'ax', 0o600).catch(error => { if (error.code !== 'EEXIST') throw error; return null; });
    await handle?.close();
  }
  return withNativeLock(file, async verifyLock => {
    const existing = await readHistory(target.configDirectory, { limits });
    const known = new Set(existing.records.map(item => canonical(item.record)));
    const imports = [], state = { verified: new Set(), copied: 0, bytes: 0 };
    let skippedAttachments = 0;
    for (const item of [...candidates.values()].sort((a, b) => a.record.timestamp - b.record.timestamp)) {
      if (!await ensurePastes(item, target.configDirectory, state, limits)) { skippedAttachments++; continue; }
      if (known.has(item.id)) continue;
      known.add(item.id); imports.push(item);
    }
    if (!imports.length) return { imported: 0, copiedPastes: state.copied, skippedAttachments, backup: null };
    const merged = [...existing.records, ...imports].sort((a, b) => a.record.timestamp - b.record.timestamp);
    const bytes = Buffer.from(merged.map(item => item.line).join('\n') + '\n');
    if (bytes.length > limits.targetBytes) throw failure('input_history_too_large');
    const verify = async () => {
      await verifyLock();
      if (!sameVersion(existing.info, await regular(file))) throw failure('input_history_changed');
    };
    await verify();
    await regular(target.root, true);
    const backupDirectory = path.join(target.root, 'input-history-backups');
    try { await fs.mkdir(backupDirectory, { mode: 0o700 }); } catch (error) { if (error.code !== 'EEXIST') throw error; }
    await regular(backupDirectory, true);
    const backup = path.join(backupDirectory, `${hash(existing.bytes)}.jsonl`);
    try { await publishExclusive(backup, existing.bytes); }
    catch (error) {
      if (error.code !== 'EEXIST') throw error;
      await regular(backup);
      if (hash(await fs.readFile(backup)) !== hash(existing.bytes)) throw failure('input_history_backup_conflict');
    }
    await replace(file, bytes, verify);
    return { imported: imports.length, copiedPastes: state.copied, skippedAttachments, backup };
  }, options.lockTimeoutMs ?? 5000);
}
