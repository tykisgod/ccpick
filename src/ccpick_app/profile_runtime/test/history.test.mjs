import { test } from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import { randomBytes } from 'node:crypto';
import { atomicJson, createProfile, lease, readJson, verifyIdentity } from '../core.mjs';
import { historyStatus, migrateHistory, prepareHistory, SHARED_HISTORY_DIRECTORIES } from '../history.mjs';

async function fixture(t) {
  const root = await fs.realpath(await fs.mkdtemp(path.join(os.tmpdir(), 'shared-history-test-')));
  t.after(() => fs.rm(root, { recursive: true, force: true }));
  const config = { dataRoot: path.join(root, 'data') }, baseDirectory = path.join(root, 'default');
  await fs.mkdir(config.dataRoot, { mode: 0o700 });
  const options = { baseDirectory, checkProcesses: async () => {} };
  const initialize = async (_, p) => atomicJson(path.join(p.configDirectory, '.claude.json'), {
    userID: randomBytes(32).toString('hex'), machineID: randomBytes(32).toString('hex') });
  const a = await createProfile(config, { name: 'account-a' }, { initialize });
  const b = await createProfile(config, { name: 'account-b' }, { initialize });
  async function write(relative, content) {
    const file = path.join(root, relative);
    await fs.mkdir(path.dirname(file), { recursive: true, mode: 0o700 });
    await fs.writeFile(file, content, { mode: 0o600 });
  }
  return { root, config, options, a, b, write, initialize };
}

test('migration merges durable history, keeps recoverable originals and preserves credentials and IDs', async t => {
  const { root, config, options, a, b, write } = await fixture(t);
  await write('default/projects/work/old.jsonl', 'old transcript\n');
  await write('data/account-a/claude/projects/work/a.jsonl', 'A transcript\n');
  await write('data/account-b/claude/projects/work/b.jsonl', 'B transcript\n');
  await write('data/account-b/claude/projects/work/memory/MEMORY.md', 'project memory');
  await write('data/account-a/claude/file-history/session/version', 'file before edit');
  await write('data/account-b/claude/.credentials.json', 'invented credential');
  await write('data/account-a/claude/history.jsonl', 'private input history');
  await write('data/account-a/claude/session-env/live', 'private live state');
  const before = await Promise.all([a, b].map(p => readJson(path.join(p.configDirectory, '.claude.json'))));
  const result = await migrateHistory(config, options);
  assert.equal(result.copiedFiles, 4);
  assert.equal(result.linkedDirectories, 4);
  for (const p of [a, b]) {
    await verifyIdentity(config, p);
    for (const name of SHARED_HISTORY_DIRECTORIES)
      assert.equal(await fs.realpath(path.join(p.configDirectory, name)), await fs.realpath(path.join(options.baseDirectory, name)));
    assert.equal(await fs.readFile(path.join(p.configDirectory, 'projects/work/old.jsonl'), 'utf8'), 'old transcript\n');
    assert.equal(await fs.readFile(path.join(p.configDirectory, 'projects/work/b.jsonl'), 'utf8'), 'B transcript\n');
  }
  assert.equal(await fs.readFile(path.join(result.backup, 'account-b-projects/work/b.jsonl'), 'utf8'), 'B transcript\n');
  assert.equal(await fs.readFile(path.join(b.configDirectory, '.credentials.json'), 'utf8'), 'invented credential');
  assert.equal(await fs.readFile(path.join(a.configDirectory, 'session-env/live'), 'utf8'), 'private live state');
  await assert.rejects(fs.access(path.join(b.configDirectory, 'history.jsonl')), { code: 'ENOENT' });
  assert.deepEqual(await Promise.all([a, b].map(p => readJson(path.join(p.configDirectory, '.claude.json')))), before);
  await fs.appendFile(path.join(b.configDirectory, 'projects/work/a.jsonl'), 'B continued\n');
  assert.match(await fs.readFile(path.join(a.configDirectory, 'projects/work/a.jsonl'), 'utf8'), /B continued/);
  assert.equal((await historyStatus(config, options)).shared, true);
  assert.equal((await migrateHistory(config, options)).alreadyShared, true);
  assert.equal((await fs.readdir(path.join(root, 'history-backups'))).length, 1);
});

test('same-content duplicates merge; conflicting content aborts before moving or copying any history', async t => {
  const { config, options, a, b, write } = await fixture(t);
  await write('default/projects/work/same.jsonl', 'same');
  await write('data/account-a/claude/projects/work/same.jsonl', 'same');
  await write('data/account-b/claude/projects/work/same.jsonl', 'different');
  await write('data/account-a/claude/projects/work/unique.jsonl', 'preserve');
  await assert.rejects(migrateHistory(config, options), /history_conflict/);
  assert.equal((await fs.lstat(path.join(a.configDirectory, 'projects'))).isSymbolicLink(), false);
  assert.equal((await fs.lstat(path.join(b.configDirectory, 'projects'))).isSymbolicLink(), false);
  await assert.rejects(fs.access(path.join(options.baseDirectory, 'projects/work/unique.jsonl')), { code: 'ENOENT' });
  await fs.writeFile(path.join(b.configDirectory, 'projects/work/same.jsonl'), 'same');
  assert.equal((await migrateHistory(config, options)).copiedFiles, 1);
});

test('migration refuses live managed or unmanaged clients and releases earlier acquired locks', async t => {
  const { config, options, a, b, write } = await fixture(t);
  await write('data/account-a/claude/projects/work/a.jsonl', 'preserve');
  const run = await lease(b, 'run');
  try { await assert.rejects(migrateHistory(config, options), /profile_busy/); }
  finally { await run.release(); }
  const aRun = await lease(a, 'run'); await aRun.release();
  await assert.rejects(migrateHistory(config, { ...options, checkProcesses: async () => { throw new Error('active_native_sessions'); } }), /active_native_sessions/);
  assert.equal((await fs.lstat(path.join(a.configDirectory, 'projects'))).isSymbolicLink(), false);
  assert.equal((await migrateHistory(config, options)).copiedFiles, 1);
});

test('new profiles attach automatically, concurrent first starts serialize, and existing private data is not silently moved', async t => {
  const { config, options, a, b, write, initialize } = await fixture(t);
  await write('data/account-a/claude/projects/work/a.jsonl', 'A');
  await assert.rejects(prepareHistory(config, a, options), /history_migration_required/);
  await migrateHistory(config, options);
  const c = await createProfile(config, { name: 'account-c' }, { initialize });
  await fs.mkdir(path.join(c.configDirectory, 'projects/initialized/memory'), { recursive: true, mode: 0o700 });
  await Promise.all([prepareHistory(config, c, options), prepareHistory(config, c, options)]);
  assert.equal(await fs.readFile(path.join(c.configDirectory, 'projects/work/a.jsonl'), 'utf8'), 'A');
  const run = await lease(b, 'run');
  try { await prepareHistory(config, b, options); }
  finally { await run.release(); }
});

test('foreign history links and nested symlinks are rejected without copying their targets', async t => {
  const { root, config, options, a, write } = await fixture(t);
  await write('other/private.txt', 'not history');
  await fs.mkdir(path.join(a.configDirectory, 'projects'), { mode: 0o700 });
  await fs.symlink(path.join(root, 'other'), path.join(a.configDirectory, 'projects/external'), process.platform === 'win32' ? 'junction' : 'dir');
  await assert.rejects(migrateHistory(config, options), /unsafe_history_path/);
  await fs.unlink(path.join(a.configDirectory, 'projects/external'));
  await fs.rmdir(path.join(a.configDirectory, 'projects'));
  await fs.symlink(path.join(root, 'other'), path.join(a.configDirectory, 'projects'), process.platform === 'win32' ? 'junction' : 'dir');
  await assert.rejects(prepareHistory(config, a, options), /history_link_conflict/);
});

test('canonical memory links stay opaque while sibling conversations merge', async t => {
  const { root, config, options, a, write } = await fixture(t);
  await write('repository-memory/MEMORY.md', 'repository-owned memory');
  await write('default/projects/work/old.jsonl', 'old conversation');
  await write('data/account-a/claude/projects/work/new.jsonl', 'new conversation');
  const linked = path.join(options.baseDirectory, 'projects/work/memory');
  const external = path.join(root, 'repository-memory');
  await fs.symlink(external, linked, process.platform === 'win32' ? 'junction' : 'dir');
  const original = await fs.readlink(linked), readdir = fs.readdir;
  t.mock.method(fs, 'readdir', async (directory, ...args) => {
    const value = String(directory);
    assert.ok(value !== linked && !value.startsWith(linked + path.sep), 'never enter canonical link');
    assert.ok(value !== external && !value.startsWith(external + path.sep), 'never enter link target');
    return readdir(directory, ...args);
  });
  const result = await migrateHistory(config, options);
  assert.equal(result.copiedFiles, 1);
  assert.equal(await fs.readlink(linked), original);
  assert.equal(await fs.readFile(path.join(external, 'MEMORY.md'), 'utf8'), 'repository-owned memory');
  assert.equal(await fs.readFile(path.join(options.baseDirectory, 'projects/work/new.jsonl'), 'utf8'), 'new conversation');
  assert.equal(await fs.readFile(path.join(result.backup, 'account-a-projects/work/new.jsonl'), 'utf8'), 'new conversation');
  assert.equal(await fs.realpath(path.join(a.configDirectory, 'projects')), path.join(options.baseDirectory, 'projects'));
  assert.equal((await historyStatus(config, options)).shared, true);
});

for (const relative of ['work/memory', 'work/memory/nested/new.jsonl', 'work']) {
  test(`canonical link blocks source overwrite at ${relative} before any copy`, async t => {
    const { root, config, options, a, write } = await fixture(t);
    await write('repository-memory/MEMORY.md', 'preserve external content');
    await write('default/projects/work/old.jsonl', 'old conversation');
    const linked = path.join(options.baseDirectory, 'projects/work/memory');
    await fs.symlink(path.join(root, 'repository-memory'), linked, process.platform === 'win32' ? 'junction' : 'dir');
    const original = await fs.readlink(linked);
    await write('data/account-a/claude/projects/' + relative, 'conflicting source');
    await write('data/account-a/claude/projects/unique.jsonl', 'must not copy');
    await assert.rejects(migrateHistory(config, options), /history_conflict/);
    assert.equal(await fs.readlink(linked), original);
    assert.equal(await fs.readFile(path.join(root, 'repository-memory/MEMORY.md'), 'utf8'), 'preserve external content');
    await assert.rejects(fs.access(path.join(options.baseDirectory, 'projects/unique.jsonl')), { code: 'ENOENT' });
    assert.equal((await fs.lstat(path.join(a.configDirectory, 'projects'))).isSymbolicLink(), false);
  });
}

test('case aliases cannot write through canonical links on case-insensitive filesystems', async t => {
  const { root, config, options, a, write } = await fixture(t);
  await write('repository-memory/MEMORY.md', 'preserve external content');
  await write('default/projects/work/old.jsonl', 'old conversation');
  const linked = path.join(options.baseDirectory, 'projects/work/Memory');
  await fs.symlink(path.join(root, 'repository-memory'), linked, process.platform === 'win32' ? 'junction' : 'dir');
  const alias = path.join(options.baseDirectory, 'projects/work/memory');
  const aliasInfo = await fs.lstat(alias).catch(e => { if (e.code !== 'ENOENT') throw e; return null; });
  if (!aliasInfo?.isSymbolicLink()) { t.skip('fixture filesystem is case-sensitive'); return; }
  await write('data/account-a/claude/projects/work/memory/new.jsonl', 'must not escape');
  await write('data/account-a/claude/projects/unique.jsonl', 'must not copy');
  await assert.rejects(migrateHistory(config, options), /history_conflict/);
  await assert.rejects(fs.access(path.join(root, 'repository-memory/new.jsonl')), { code: 'ENOENT' });
  await assert.rejects(fs.access(path.join(options.baseDirectory, 'projects/unique.jsonl')), { code: 'ENOENT' });
  assert.equal((await fs.lstat(path.join(a.configDirectory, 'projects'))).isSymbolicLink(), false);
  await fs.rm(path.join(a.configDirectory, 'projects/work'), { recursive: true });
  await write('data/account-a/claude/projects/WORK', 'must not replace directory');
  await assert.rejects(migrateHistory(config, options), /history_conflict/);
  await assert.rejects(fs.access(path.join(options.baseDirectory, 'projects/unique.jsonl')), { code: 'ENOENT' });
});

test('a failed copy never publishes partial history and a second migration succeeds', async t => {
  const { config, options, a, write } = await fixture(t);
  await write('data/account-a/claude/projects/work/a.jsonl', 'complete transcript');
  const copy = fs.copyFile;
  let once = true;
  t.mock.method(fs, 'copyFile', async (source, destination, flags) => {
    if (once) {
      once = false;
      await fs.writeFile(destination, 'partial');
      throw Object.assign(new Error('disk full'), { code: 'ENOSPC' });
    }
    return copy(source, destination, flags);
  });
  await assert.rejects(migrateHistory(config, options), { code: 'ENOSPC' });
  await assert.rejects(fs.access(path.join(options.baseDirectory, 'projects/work/a.jsonl')), { code: 'ENOENT' });
  assert.equal(await fs.readFile(path.join(a.configDirectory, 'projects/work/a.jsonl'), 'utf8'), 'complete transcript');
  assert.equal((await migrateHistory(config, options)).copiedFiles, 1);
});

test('an interrupted journal blocks migration and fresh starts until its files have been recovered', async t => {
  const { root, config, options, a } = await fixture(t);
  const directory = path.join(root, 'history-backups/interrupted');
  await fs.mkdir(directory, { recursive: true, mode: 0o700 });
  await atomicJson(path.join(directory, 'migration.json'), { version: 1, status: 'linking' });
  await assert.rejects(migrateHistory(config, options), /history_recovery_required/);
  await assert.rejects(prepareHistory(config, a, options), /history_recovery_required/);
  const run = await lease(a, 'run'); await run.release();
});

test('clean personal bootstrap shares only registered profiles and does not adopt the legacy default history', async t => {
  const { root, config, a, b } = await fixture(t);
  await atomicJson(path.join(root, 'state.json'), { version: 2, enabled: true, bootstrapEmail: 'account-0090@example.com' });
  await prepareHistory(config, a);
  await prepareHistory(config, b);
  const expected = path.join(root, 'shared-history');
  assert.equal((await historyStatus(config)).baseDirectory, expected);
  assert.equal(await fs.realpath(path.join(a.configDirectory, 'projects')), path.join(expected, 'projects'));
  assert.equal((await historyStatus(config)).shared, true);
});

test('a failed directory link restores the original directory and leaves the verified shared copy recoverable', async t => {
  const { config, options, a, write } = await fixture(t);
  await write('data/account-a/claude/projects/work/a.jsonl', 'preserve original');
  const symlink = fs.symlink;
  let once = true;
  t.mock.method(fs, 'symlink', async (...args) => {
    if (once) { once = false; throw Object.assign(new Error('link refused'), { code: 'EPERM' }); }
    return symlink(...args);
  });
  await assert.rejects(migrateHistory(config, options), { code: 'EPERM' });
  assert.equal((await fs.lstat(path.join(a.configDirectory, 'projects'))).isSymbolicLink(), false);
  assert.equal(await fs.readFile(path.join(a.configDirectory, 'projects/work/a.jsonl'), 'utf8'), 'preserve original');
  assert.equal(await fs.readFile(path.join(options.baseDirectory, 'projects/work/a.jsonl'), 'utf8'), 'preserve original');
  assert.equal((await migrateHistory(config, options)).copiedFiles, 0);
  assert.equal((await historyStatus(config, options)).shared, true);
});
