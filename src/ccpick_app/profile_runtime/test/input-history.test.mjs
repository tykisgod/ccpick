import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import path from 'node:path';
import os from 'node:os';
import { createHash, randomBytes } from 'node:crypto';
import { atomicJson, createProfile } from '../core.mjs';
import { prepareInputHistory } from '../input-history.mjs';
import { resolveReplayPrompt } from '../prompt-replay.mjs';

const session = '00000000-0000-4000-8000-000000000001';
const project = path.resolve('example-project');
const record = (display, timestamp, extra = {}) => ({ display, pastedContents: {}, timestamp, project, sessionId: session, ...extra });
const jsonl = rows => rows.map(row => JSON.stringify(row)).join('\n') + (rows.length ? '\n' : '');
const history = profile => path.join(profile.configDirectory, 'history.jsonl');
const contents = async profile => (await fs.readFile(history(profile), 'utf8')).trim().split('\n').filter(Boolean).map(JSON.parse);
const sha = text => createHash('sha256').update(text).digest('hex').slice(0, 16);
async function fixture(t) {
  const root = await fs.realpath(await fs.mkdtemp(path.join(os.tmpdir(), 'input-history-test-')));
  t.after(() => fs.rm(root, { recursive: true, force: true }));
  const config = { dataRoot: path.join(root, 'data') };
  await fs.mkdir(config.dataRoot, { mode: 0o700 });
  const create = name => createProfile(config, { name }, { initialize: async (_, profile) =>
    atomicJson(path.join(profile.configDirectory, '.claude.json'), {
      userID: randomBytes(32).toString('hex'), machineID: randomBytes(32).toString('hex') }) });
  const a = await create('account-a'), b = await create('account-b');
  return { root, config, a, b, create,
    write: (profile, rows) => fs.writeFile(history(profile), jsonl(rows), { mode: 0o600 }),
    cache: async (profile, text) => {
      const folder = path.join(profile.configDirectory, 'paste-cache');
      await fs.mkdir(folder, { recursive: true, mode: 0o700 });
      await fs.writeFile(path.join(folder, sha(text) + '.txt'), text, { mode: 0o600 });
      return sha(text);
    } };
}

test('chronological merge restores earlier input without displacing its newer resubmission; originals and credentials survive', async t => {
  const f = await fixture(t);
  const earlier = record('refine and restructure', 100), later = record('refine and restructure', 200);
  await f.write(f.a, [record('previous request', 50), earlier]);
  await f.write(f.b, [later, record('newest request', 300)]);
  const before = await fs.readFile(history(f.b));
  const privateFiles = ['.credentials.json', '.claude.json', 'settings.json'];
  await fs.writeFile(path.join(f.b.configDirectory, '.credentials.json'), 'invented secret');
  const privateBefore = await Promise.all(privateFiles.map(file => fs.readFile(path.join(f.b.configDirectory, file))));
  const result = await prepareInputHistory(f.config, f.b);
  assert.equal(result.imported, 2);
  assert.deepEqual((await contents(f.b)).map(row => row.timestamp), [50, 100, 200, 300]);
  assert.deepEqual(await fs.readFile(result.backup), before);
  assert.deepEqual(await Promise.all(privateFiles.map(file => fs.readFile(path.join(f.b.configDirectory, file)))), privateBefore);
  assert.equal((await fs.lstat(history(f.b))).isSymbolicLink(), false);
  assert.deepEqual(await contents(f.a), [record('previous request', 50), earlier]);
  assert.equal((await prepareInputHistory(f.config, f.b)).imported, 0);
  assert.deepEqual((await contents(f.b)).map(row => row.timestamp), [50, 100, 200, 300]);
});

test('new accounts and simultaneous starts import once; canonical dedup ignores JSON key order', async t => {
  const f = await fixture(t), row = record('history from existing account', 1);
  await f.write(f.a, [row]);
  await f.write(f.b, [{ ...Object.fromEntries(Object.entries(row).reverse()) }]);
  const c = await f.create('account-c');
  const results = await Promise.all([prepareInputHistory(f.config, c), prepareInputHistory(f.config, c)]);
  assert.equal(results.reduce((total, result) => total + result.imported, 0), 1);
  assert.deepEqual(await contents(c), [row]);
  assert.equal((await prepareInputHistory(f.config, f.a)).imported, 0);
});

test('imports retain inline images and hashed pasted text; replay resolves the complete copied attachment', async t => {
  const f = await fixture(t), text = '完整文本\n'.repeat(300);
  const contentHash = await f.cache(f.a, text);
  const pasted = record('Please refine [Pasted text #1]', 100, { pastedContents: {
    1: { id: 1, type: 'text', contentHash, filename: 'notes.txt' },
  } });
  const image = record('Inspect [Image #1]', 200, { pastedContents: {
    1: { id: 1, type: 'image', content: 'invented-inline-image', mediaType: 'image/png' },
  } });
  await f.write(f.a, [pasted, image]);
  const result = await prepareInputHistory(f.config, f.b);
  assert.equal(result.copiedPastes, 1);
  assert.deepEqual(await contents(f.b), [pasted, image]);
  assert.equal(await resolveReplayPrompt(f.b, {
    prompt: pasted.display, createdAt: 100, sessionId: session, cwd: project,
  }), 'Please refine ' + text);
});

test('missing or corrupt paste content is not imported as a broken placeholder and is retried on next launch', async t => {
  const f = await fixture(t), text = 'late paste content', contentHash = sha(text);
  const row = record('[Pasted text #1]', 100, { pastedContents: { 1: { id: 1, type: 'text', contentHash } } });
  await f.write(f.a, [row]);
  let result = await prepareInputHistory(f.config, f.b);
  assert.equal(result.imported, 0); assert.equal(result.skippedAttachments, 1);
  await fs.mkdir(path.join(f.a.configDirectory, 'paste-cache'), { mode: 0o700 });
  await fs.writeFile(path.join(f.a.configDirectory, 'paste-cache', `${contentHash}.txt`), 'corrupt');
  result = await prepareInputHistory(f.config, f.b);
  assert.equal(result.imported, 0); assert.equal(result.skippedAttachments, 1);
  await f.cache(f.a, text);
  assert.equal((await prepareInputHistory(f.config, f.b)).imported, 1);
  assert.deepEqual(await contents(f.b), [row]);
});

test('duplicate input can recover its cache from a later registered source', async t => {
  const f = await fixture(t), text = 'cache available in the second source', contentHash = sha(text);
  const row = record('[Pasted text #1]', 100, { pastedContents: { 1: { id: 1, type: 'text', contentHash } } });
  await f.write(f.a, [row]);
  const c = await f.create('account-c'); await f.write(c, [row]); await f.cache(c, text);
  const result = await prepareInputHistory(f.config, f.b);
  assert.equal(result.imported, 1); assert.equal(result.copiedPastes, 1);
  assert.deepEqual(await contents(f.b), [row]);
});

test('a native append owns the shared lock; merge reads its new target record after the writer releases', async t => {
  const f = await fixture(t);
  await f.write(f.a, [record('source', 1)]); await f.write(f.b, [record('target', 2)]);
  const lock = history(f.b) + '.lock'; await fs.mkdir(lock, { mode: 0o700 });
  const merging = prepareInputHistory(f.config, f.b);
  await new Promise(resolve => setTimeout(resolve, 80));
  await fs.appendFile(history(f.b), jsonl([record('concurrently submitted', 3)]));
  await fs.rmdir(lock);
  assert.equal((await merging).imported, 1);
  assert.deepEqual((await contents(f.b)).map(row => row.display), ['source', 'target', 'concurrently submitted']);
});

test('a held lock times out without rewriting or stealing the writer lock', async t => {
  const f = await fixture(t);
  await f.write(f.a, [record('source', 1)]); await f.write(f.b, [record('target', 2)]);
  const lock = history(f.b) + '.lock'; await fs.mkdir(lock, { mode: 0o700 });
  await assert.rejects(prepareInputHistory(f.config, f.b, { lockTimeoutMs: 10 }), { code: 'input_history_busy' });
  assert.deepEqual(await contents(f.b), [record('target', 2)]);
  assert.equal((await fs.lstat(lock)).isDirectory(), true);
});

test('a writer releasing its lock between EEXIST and inspection allows one merge', async t => {
  const f = await fixture(t);
  await f.write(f.a, [record('source', 1)]); await f.write(f.b, [record('target', 2)]);
  const lock = history(f.b) + '.lock'; await fs.mkdir(lock, { mode: 0o700 });
  const mkdir = fs.mkdir, lstat = fs.lstat;
  let attempts = 0, inspections = 0;
  t.mock.method(fs, 'mkdir', async (file, ...args) => {
    if (file === lock) attempts++;
    return mkdir(file, ...args);
  });
  t.mock.method(fs, 'lstat', async (file, ...args) => {
    if (file === lock && ++inspections === 1) await fs.rmdir(lock);
    return lstat(file, ...args);
  });
  assert.equal((await prepareInputHistory(f.config, f.b)).imported, 1);
  assert.equal(attempts, 2);
  assert.deepEqual(await contents(f.b), [record('source', 1), record('target', 2)]);
  assert.equal((await fs.readdir(path.join(f.b.root, 'input-history-backups'))).length, 1);
});

test('Windows pending-delete errors during history lock acquisition are retried without repeating the merge',
  { skip: process.platform !== 'win32' }, async t => {
    const f = await fixture(t);
    await f.write(f.a, [record('source', 1)]); await f.write(f.b, [record('target', 2)]);
    const lock = history(f.b) + '.lock', mkdir = fs.mkdir;
    let attempts = 0;
    t.mock.method(fs, 'mkdir', async (file, ...args) => {
      if (file === lock && ++attempts <= 3)
        throw Object.assign(new Error('fixture acquisition conflict'), { code: ['EPERM', 'EACCES', 'EBUSY'][attempts - 1] });
      return mkdir(file, ...args);
    });
    assert.equal((await prepareInputHistory(f.config, f.b)).imported, 1);
    assert.equal(attempts, 4);
    assert.deepEqual(await contents(f.b), [record('source', 1), record('target', 2)]);
    assert.equal((await fs.readdir(path.join(f.b.root, 'input-history-backups'))).length, 1);
  });

test('Windows unreadable history lock times out within the acquisition budget without removing its owner',
  { skip: process.platform !== 'win32' }, async t => {
    const f = await fixture(t);
    await f.write(f.a, [record('source', 1)]); await f.write(f.b, [record('target', 2)]);
    const lock = history(f.b) + '.lock'; await fs.mkdir(lock, { mode: 0o700 });
    const lstat = fs.lstat;
    let inspections = 0;
    t.mock.method(fs, 'lstat', async (file, ...args) => {
      if (file === lock) { inspections++; throw Object.assign(new Error('fixture locked'), { code: 'EPERM' }); }
      return lstat(file, ...args);
    });
    const started = performance.now();
    await assert.rejects(prepareInputHistory(f.config, f.b, { lockTimeoutMs: 20 }), { code: 'input_history_busy' });
    assert.ok(inspections >= 2);
    assert.ok(performance.now() - started < 1000);
    assert.equal((await lstat(lock)).isDirectory(), true);
    assert.deepEqual(await contents(f.b), [record('target', 2)]);
  });

test('unknown history lock acquisition failures are not retried or converted to contention', async t => {
  const f = await fixture(t);
  await f.write(f.a, [record('source', 1)]); await f.write(f.b, [record('target', 2)]);
  const lock = history(f.b) + '.lock', mkdir = fs.mkdir;
  let attempts = 0;
  t.mock.method(fs, 'mkdir', async (file, ...args) => {
    if (file === lock) { attempts++; throw Object.assign(new Error('fixture disk failure'), { code: 'EIO' }); }
    return mkdir(file, ...args);
  });
  await assert.rejects(prepareInputHistory(f.config, f.b), { code: 'EIO' });
  assert.equal(attempts, 1);
  assert.deepEqual(await contents(f.b), [record('target', 2)]);
});

test('history merge errors after acquisition never reacquire the lock or replay the merge', async t => {
  const f = await fixture(t);
  await f.write(f.a, [record('source', 1)]); await f.write(f.b, [record('target', 2)]);
  const lock = history(f.b) + '.lock', mkdir = fs.mkdir, open = fs.open;
  let attempts = 0, reads = 0;
  t.mock.method(fs, 'mkdir', async (file, ...args) => {
    if (file === lock) attempts++;
    return mkdir(file, ...args);
  });
  t.mock.method(fs, 'open', async (file, flags, ...args) => {
    if (file === history(f.b) && flags === 'r') {
      reads++; throw Object.assign(new Error('fixture merge failure'), { code: 'EPERM' });
    }
    return open(file, flags, ...args);
  });
  await assert.rejects(prepareInputHistory(f.config, f.b), { code: 'EPERM' });
  assert.equal(attempts, 1); assert.equal(reads, 1);
  await assert.rejects(fs.lstat(lock), { code: 'ENOENT' });
  assert.deepEqual(await contents(f.b), [record('target', 2)]);
});

test('bounded imports keep newest records while preserving every existing target row and duplicate', async t => {
  const f = await fixture(t);
  const original = [record('target old', 0), record('target old', 0), record('target latest', 10)];
  await f.write(f.b, original);
  await f.write(f.a, Array.from({ length: 8 }, (_, i) => record(`source ${i}`, i + 1)));
  const c = await f.create('account-c'); await f.write(c, [record('c latest', 9)]);
  const result = await prepareInputHistory(f.config, f.b, { limits: { importedRecords: 3 } });
  assert.equal(result.imported, 3);
  assert.deepEqual((await contents(f.b)).map(row => row.timestamp), [0, 0, 7, 8, 9, 10]);
});

test('bounded source tails skip partial lines and pick up their completion at a later launch', async t => {
  const f = await fixture(t), complete = record('complete recent entry', 2), partial = record('later entry', 3);
  await fs.writeFile(history(f.a), 'x'.repeat(2048) + '\n' + jsonl([complete]) + JSON.stringify(partial).slice(0, 20));
  assert.equal((await prepareInputHistory(f.config, f.b, { limits: { sourceBytes: 512 } })).imported, 1);
  await fs.appendFile(history(f.a), JSON.stringify(partial).slice(20) + '\n');
  assert.equal((await prepareInputHistory(f.config, f.b, { limits: { sourceBytes: 512 } })).imported, 1);
  assert.deepEqual(await contents(f.b), [complete, partial]);
});

test('malformed target and excessive target size refuse a rewrite without changing original bytes', async t => {
  const f = await fixture(t);
  await f.write(f.a, [record('source', 1)]);
  const broken = jsonl([record('target', 2)]) + '{"display":"unfinished';
  await fs.writeFile(history(f.b), broken);
  await assert.rejects(prepareInputHistory(f.config, f.b), { code: 'input_history_invalid' });
  assert.equal(await fs.readFile(history(f.b), 'utf8'), broken);
  await f.write(f.b, [record('target', 2)]);
  await assert.rejects(prepareInputHistory(f.config, f.b, { limits: { targetBytes: 1 } }), { code: 'input_history_too_large' });
  assert.deepEqual(await contents(f.b), [record('target', 2)]);
});

test('an unregistered legacy history is not imported by a clean personal bootstrap', async t => {
  const f = await fixture(t);
  const legacy = path.join(f.root, 'unregistered-default'); await fs.mkdir(legacy);
  await fs.writeFile(path.join(legacy, 'history.jsonl'), jsonl([record('another person legacy history', 1)]));
  await atomicJson(path.join(f.root, 'state.json'), { bootstrapEmail: 'account-0090@example.com' });
  await f.write(f.a, [record('registered personal history', 2)]);
  await prepareInputHistory(f.config, f.b);
  assert.deepEqual(await contents(f.b), [record('registered personal history', 2)]);
});

test('a paste-cache junction never reads an external directory', async t => {
  const f = await fixture(t), text = 'outside text', contentHash = sha(text);
  const external = path.join(f.root, 'outside'); await fs.mkdir(external);
  await fs.writeFile(path.join(external, `${contentHash}.txt`), text);
  await fs.symlink(external, path.join(f.a.configDirectory, 'paste-cache'), process.platform === 'win32' ? 'junction' : 'dir');
  await f.write(f.a, [record('[Pasted text #1]', 1, { pastedContents: { 1: { id: 1, type: 'text', contentHash } } })]);
  await assert.rejects(prepareInputHistory(f.config, f.b), { code: 'unsafe_input_history_path' });
  assert.equal(await fs.readFile(history(f.b), 'utf8'), '');
});

test('an uncooperative target writer prevents replacement; the new entry is retained', async t => {
  const f = await fixture(t);
  await f.write(f.a, [record('source', 1)]); await f.write(f.b, [record('target', 2)]);
  const write = fs.writeFile; let changed = false;
  t.mock.method(fs, 'writeFile', async (file, ...args) => {
    if (!changed && String(file).startsWith(history(f.b) + '.') && String(file).endsWith('.tmp')) {
      changed = true; await fs.appendFile(history(f.b), jsonl([record('unexpected writer', 3)]));
    }
    return write(file, ...args);
  });
  await assert.rejects(prepareInputHistory(f.config, f.b), { code: 'input_history_changed' });
  assert.deepEqual((await contents(f.b)).map(row => row.display), ['target', 'unexpected writer']);
});

test('a failed replacement retains the original target and recoverable snapshot', async t => {
  const f = await fixture(t);
  await f.write(f.a, [record('source', 1)]); await f.write(f.b, [record('target', 2)]);
  const rename = fs.rename;
  t.mock.method(fs, 'rename', async (source, target) => {
    if (target === history(f.b)) throw Object.assign(new Error('disk full'), { code: 'ENOSPC' });
    return rename(source, target);
  });
  await assert.rejects(prepareInputHistory(f.config, f.b), { code: 'ENOSPC' });
  assert.deepEqual(await contents(f.b), [record('target', 2)]);
  const backups = await fs.readdir(path.join(f.b.root, 'input-history-backups'));
  assert.equal(backups.length, 1);
  assert.equal(await fs.readFile(path.join(f.b.root, 'input-history-backups', backups[0]), 'utf8'), jsonl([record('target', 2)]));
  assert.equal((await fs.readdir(f.b.configDirectory)).some(name => name.endsWith('.tmp') || name.endsWith('.lock')), false);
});
