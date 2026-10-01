import fs from 'node:fs/promises';
import path from 'node:path';
import { createHash } from 'node:crypto';
import { performance } from 'node:perf_hooks';

export const MAX_REPLAY_BYTES = 512 * 1024;
const MARKERS = /\[(Pasted text|Image|Audio|\.\.\.Truncated text) #(\d+)(?: \+\d+ lines)?(\.)*\]/g;
const HASH = /^[a-f0-9]{16}$/;
const UUID = /^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$/i;
const utf8 = new TextDecoder('utf-8', { fatal: true });

function unavailable() {
  return Object.assign(new Error('handoff_attachment_unavailable'), { code: 'handoff_attachment_unavailable' });
}
function sameDirectory(a, b) {
  if (typeof a !== 'string' || !path.isAbsolute(a)) return false;
  const values = [a, b].map(value => path.resolve(value));
  return process.platform === 'win32' ? values[0].toLowerCase() === values[1].toLowerCase() : values[0] === values[1];
}
async function regular(file, directory = false) {
  const info = await fs.lstat(file);
  if (info.isSymbolicLink() || !(directory ? info.isDirectory() : info.isFile())) throw unavailable();
  return info;
}
async function historyTail(file) {
  await regular(file);
  const handle = await fs.open(file, 'r');
  try {
    const info = await handle.stat();
    if (!info.isFile()) throw unavailable();
    const start = Math.max(0, info.size - MAX_REPLAY_BYTES);
    const data = Buffer.alloc(Math.min(info.size, MAX_REPLAY_BYTES));
    const { bytesRead } = await handle.read(data, 0, data.length, start);
    let tail = data.subarray(0, bytesRead);
    if (start) {
      const firstNewline = tail.indexOf(10);
      if (firstNewline < 0) return [];
      tail = tail.subarray(firstNewline + 1);
    }
    const records = [];
    for (const line of utf8.decode(tail).split('\n')) {
      try { records.push(JSON.parse(line)); } catch { /* An append may leave its last line incomplete. */ }
    }
    return records;
  } finally { await handle.close(); }
}
async function pastedText(directory, record, cache, budget) {
  if (!record || record.type !== 'text' || record.unavailable === true) throw unavailable();
  const inline = typeof record.content === 'string';
  const hashed = typeof record.contentHash === 'string';
  if (inline === hashed) throw unavailable();
  if (inline) return record.content;
  const hash = record.contentHash;
  if (!HASH.test(hash)) throw unavailable();
  if (cache.has(hash)) return cache.get(hash);
  const folder = path.join(directory, 'paste-cache');
  await regular(folder, true);
  const file = path.join(folder, `${hash}.txt`);
  const info = await regular(file);
  if (info.size > budget.remaining) throw unavailable();
  const handle = await fs.open(file, 'r');
  try {
    const size = (await handle.stat()).size;
    if (size > budget.remaining) throw unavailable();
    const data = Buffer.alloc(size + 1);
    const { bytesRead } = await handle.read(data, 0, data.length, 0);
    if (bytesRead !== size) throw unavailable();
    const contents = data.subarray(0, bytesRead);
    if (createHash('sha256').update(contents).digest('hex').slice(0, 16) !== hash) throw unavailable();
    const text = utf8.decode(contents);
    budget.remaining -= bytesRead;
    cache.set(hash, text);
    return text;
  } finally { await handle.close(); }
}
async function expand(directory, prompt, markers, record) {
  const entries = record.pastedContents;
  if (!entries || typeof entries !== 'object' || Array.isArray(entries)) throw unavailable();
  const cache = new Map(), budget = { remaining: MAX_REPLAY_BYTES }, resolved = new Map();
  for (const match of markers) {
    const id = Number(match[2]);
    if (!Number.isSafeInteger(id) || id < 1 || id >= 4294967296) throw unavailable();
    const value = entries[id];
    if (!value || value.id !== id) throw unavailable();
    if (!resolved.has(id)) resolved.set(id, await pastedText(directory, value, cache, budget));
  }
  let bytes = Buffer.byteLength(prompt, 'utf8');
  for (const match of markers) bytes += Buffer.byteLength(resolved.get(Number(match[2])), 'utf8') - Buffer.byteLength(match[0], 'utf8');
  if (bytes > MAX_REPLAY_BYTES) throw unavailable();
  return prompt.replace(MARKERS, (_, kind, id) => resolved.get(Number(id)));
}

/** Run after the blocking hook exits. Return exact text, or a fixed public error.
 * No account files are written; image/audio placeholders are never replayed.
 * History reads use a bounded tail; cache payloads are hash-verified and bounded.
 */
export async function resolveReplayPrompt(sourceProfile, request, { waitMs = 2000, pollMs = 50 } = {}) {
  try {
    const prompt = request?.prompt;
    if (typeof prompt !== 'string' || Buffer.byteLength(prompt, 'utf8') > MAX_REPLAY_BYTES) throw unavailable();
    const markers = [...prompt.matchAll(MARKERS)];
    if (!markers.length) return prompt;
    if (markers.some(match => match[1] === 'Image' || match[1] === 'Audio')) throw unavailable();
    const directory = sourceProfile?.configDirectory;
    const createdAt = typeof request.createdAt === 'number' ? request.createdAt : Date.parse(request.createdAt);
    if (typeof directory !== 'string' || !path.isAbsolute(directory) || !UUID.test(request.sessionId ?? '') ||
        typeof request.cwd !== 'string' || !path.isAbsolute(request.cwd) || !Number.isFinite(createdAt) || createdAt <= 0)
      throw unavailable();
    if (!Number.isFinite(waitMs) || !Number.isFinite(pollMs)) throw unavailable();
    const deadline = performance.now() + Math.max(0, Math.min(2000, waitMs));
    const delay = Math.max(10, Math.min(100, pollMs));
    for (;;) {
      try {
        const records = await historyTail(path.join(directory, 'history.jsonl'));
        const candidates = records.filter(value => value && value.sessionId === request.sessionId &&
          sameDirectory(value.project, request.cwd) && typeof value.display === 'string' &&
          value.display.trim() === prompt.trim() && typeof value.timestamp === 'number' &&
          Number.isFinite(value.timestamp) && value.timestamp >= createdAt - 10000 && value.timestamp <= createdAt + 5000);
        if (candidates.length > 1) throw unavailable();
        if (candidates.length === 1) return await expand(directory, prompt, markers, candidates[0]);
      } catch (error) {
        if (error.code !== 'ENOENT') throw error;
      }
      const remaining = deadline - performance.now();
      if (remaining <= 0) throw unavailable();
      await new Promise(resolve => setTimeout(resolve, Math.min(delay, remaining)));
    }
  } catch { throw unavailable(); }
}
