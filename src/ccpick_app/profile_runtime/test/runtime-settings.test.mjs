import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import { atomicJson, readJson, validateSettings, childEnvironment, privacyEnvironment } from '../core.mjs';
import { runtimeSettingsEnvironment, syncRuntimeSettings } from '../runtime-settings.mjs';

function portableSettings(f, scope = 'default') {
  return { ...runtimeSettingsEnvironment(f.service, scope), CCPICK_ACCOUNT_RUNTIME: '1',
    CCPICK_RUNTIME_SCOPE: scope, CCPICK_RUNTIME_INSTALL: path.resolve(f.config.dataRoot, '..', 'install.json') };
}

async function fixture(t) {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), 'runtime-settings-test-'));
  t.after(() => fs.rm(root, { recursive: true, force: true }));
  const config = { dataRoot: path.join(root, 'data'), seamlessAccounts: true };
  const runtime = path.join(root, 'active-runtime');
  const directory = scope => scope === 'default' ? path.join(runtime, 'claude')
    : path.join(runtime, 'scopes', scope, 'claude');
  for (const scope of ['default', 'account-a']) await fs.mkdir(directory(scope), { recursive: true, mode: 0o700 });
  return { root, config, runtime, directory, service: { key: 'a'.repeat(64) } };
}

test('private runtime settings preserve native preferences and restore each scope after daemon filtering', async t => {
  const f = await fixture(t);
  const source = { language: '简体中文', hooks: { Stop: [{ hooks: [{ type: 'command', command: 'fixture-stop' }] }] },
    env: { FIXTURE_PREFERENCE: 'keep', HTTPS_PROXY: 'http://127.0.0.1:1510',
      anthropic_base_url: 'https://old.invalid', ANTHROPIC_CUSTOM_HEADERS: 'fixture-old-scope-capability',
      CLAUDE_CODE_DISABLE_BG_SHELL_PRESSURE_REAP: '0', claude_code_disable_bg_shell_pressure_reap: '0' } };
  const vault = path.join(f.root, 'account-vault.json'); await atomicJson(vault, source);
  const sourceBytes = await fs.readFile(vault);
  for (const scope of ['default', 'account-a']) {
    const file = path.join(f.directory(scope), 'settings.json'); await atomicJson(file, source);
    assert.equal(await syncRuntimeSettings(f.config, f.service, scope), true);
    const settings = await readJson(file), expected = runtimeSettingsEnvironment(f.service, scope);
    assert.deepEqual(settings.hooks, source.hooks); assert.equal(settings.language, source.language);
    assert.equal(settings.env.FIXTURE_PREFERENCE, 'keep');
    assert.equal(settings.env.HTTPS_PROXY, source.env.HTTPS_PROXY);
    assert.equal(settings.env.anthropic_base_url, undefined);
    assert.equal(settings.env.CLAUDE_CODE_DISABLE_BG_SHELL_PRESSURE_REAP, '1');
    assert.equal(settings.env.claude_code_disable_bg_shell_pressure_reap, undefined);
    const daemonChild = { HTTPS_PROXY: source.env.HTTPS_PROXY, NODE_EXTRA_CA_CERTS: 'fixture-ca' };
    const worker = { ...daemonChild, ...settings.env };
    assert.equal(worker.CLAUDE_CODE_DISABLE_BG_SHELL_PRESSURE_REAP, '1');
    assert.equal(worker.ANTHROPIC_CUSTOM_HEADERS, expected.ANTHROPIC_CUSTOM_HEADERS);
    assert.equal(worker.ANTHROPIC_BASE_URL, expected.ANTHROPIC_BASE_URL);
    assert.equal(worker.NODE_EXTRA_CA_CERTS, daemonChild.NODE_EXTRA_CA_CERTS);
    assert.equal(worker.CCPICK_RUNTIME_CLIENT_ID, undefined);
    validateSettings(settings, worker, f.config);
    assert.throws(() => validateSettings({ env: { HTTPS_PROXY: 'http://wrong.invalid' } }, worker, f.config), /settings_conflict/);
    assert.throws(() => validateSettings({ env: { ANTHROPIC_CUSTOM_HEADERS: 'fixture-wrong-scope' } }, worker, f.config), /settings_conflict/);
    assert.throws(() => validateSettings({ env: { CLAUDE_CODE_DISABLE_BG_SHELL_PRESSURE_REAP: '0' } }, worker, f.config), /settings_conflict/);
    const stat = await fs.stat(file), bytes = await fs.readFile(file);
    assert.equal(await syncRuntimeSettings(f.config, f.service, scope), false);
    assert.equal((await fs.stat(file)).mtimeMs, stat.mtimeMs);
    assert.deepEqual(await fs.readFile(file), bytes);
    if (process.platform !== 'win32') assert.equal(stat.mode & 0o077, 0);
  }
  assert.deepEqual(await fs.readFile(vault), sourceBytes);
});

test('per-scope proxy settings replace every old transport alias while keeping user preferences', async t => {
  const f = await fixture(t), scope = 'account-a';
  const service = { ...f.service, egressVersion: 1, proxyPort: 12341, scopeProxyPorts: { [scope]: 12342 } };
  const env = { FIXTURE_PREFERENCE: 'kept' };
  for (const key of ['HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY', 'WS_PROXY', 'WSS_PROXY', 'NO_PROXY']) {
    env[key] = 'old'; env[key.toLowerCase()] = 'old lowercase';
  }
  const file = path.join(f.directory(scope), 'settings.json'); await atomicJson(file, { env });
  await syncRuntimeSettings(f.config, service, scope);
  const updated = (await readJson(file)).env;
  for (const key of ['HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY', 'WS_PROXY', 'WSS_PROXY']) {
    assert.equal(updated[key], 'http://127.0.0.1:12342'); assert.equal(updated[key.toLowerCase()], undefined);
  }
  assert.equal(updated.NO_PROXY, '127.0.0.1,localhost,::1'); assert.equal(updated.no_proxy, undefined);
  assert.equal(updated.FIXTURE_PREFERENCE, 'kept');
  assert.equal(runtimeSettingsEnvironment(service).HTTPS_PROXY, 'http://127.0.0.1:12341');
  for (const proxyPort of [0, 65536, '12341']) assert.throws(() => runtimeSettingsEnvironment({ ...service, proxyPort }), /runtime_service_invalid/);
});

test('runtime settings refuse malformed private files and unsafe scope paths without publishing capabilities', async t => {
  const f = await fixture(t), file = path.join(f.directory('default'), 'settings.json');
  for (const invalid of [[], { env: [] }, { env: 'bad' }]) {
    await atomicJson(file, invalid); const before = await fs.readFile(file);
    await assert.rejects(syncRuntimeSettings(f.config, f.service), /settings_conflict/);
    assert.deepEqual(await fs.readFile(file), before);
  }
  for (const scope of ['../account-a', '', 'DEFAULT'])
    await assert.rejects(syncRuntimeSettings(f.config, f.service, scope), /runtime_scope_invalid/);
  await assert.rejects(syncRuntimeSettings(f.config, { key: 'invalid\ncapability' }), /runtime_key_invalid/);
  await assert.rejects(syncRuntimeSettings({ ...f.config, seamlessAccounts: false }, f.service), /seamless_runtime_disabled/);
  await assert.rejects(syncRuntimeSettings(f.config, f.service, 'unregistered'), /ENOENT/);
  await fs.unlink(file); await fs.rmdir(f.directory('default'));
  const outside = path.join(f.root, 'outside'); await fs.mkdir(outside, { mode: 0o700 });
  await fs.symlink(outside, f.directory('default'), process.platform === 'win32' ? 'junction' : 'dir');
  await assert.rejects(syncRuntimeSettings(f.config, f.service), /unsafe_path/);
  assert.deepEqual(await fs.readdir(outside), []);
});

test('existing runtime scopes migrate legacy privacy switches and pass the current launcher settings check', async t => {
  const f = await fixture(t), proxy = 'http://127.0.0.1:1510', certificate = 'fixture-ca';
  const native = { language: '简体中文', model: 'fixture-model', permissions: { allow: ['Read'] },
    hooks: { Stop: [{ hooks: [{ type: 'command', command: 'fixture-stop' }] }] } };
  for (const scope of ['default', 'account-a']) {
    const file = path.join(f.directory(scope), 'settings.json');
    const profile = { name: 'account-a', configDirectory: f.directory(scope) };
    const environment = { ...childEnvironment(f.config, profile, proxy, {}),
      NODE_EXTRA_CA_CERTS: certificate, ...portableSettings(f, scope) };
    for (const value of ['1', '0', '']) {
      const legacy = { ...native, env: { CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC: value,
        claude_code_disable_nonessential_traffic: value, DISABLE_TELEMETRY: '0', disable_telemetry: '0',
        DISABLE_ERROR_REPORTING: '0', disable_error_reporting: '0', DISABLE_AUTOUPDATER: '0', disable_autoupdater: '0',
        DISABLE_FEEDBACK_COMMAND: '0', disable_feedback_command: '0', HTTPS_PROXY: proxy,
        NODE_EXTRA_CA_CERTS: certificate, FIXTURE_PREFERENCE: 'keep' } };
      await atomicJson(file, legacy);
      assert.throws(() => validateSettings(legacy, environment, f.config), /settings_conflict/);
      assert.equal(await syncRuntimeSettings(f.config, f.service, scope), true);
      const settings = await readJson(file);
      assert.deepEqual(settings, { ...native, env: { HTTPS_PROXY: proxy, NODE_EXTRA_CA_CERTS: certificate,
        FIXTURE_PREFERENCE: 'keep', ...privacyEnvironment, ...portableSettings(f, scope) } });
      assert.doesNotThrow(() => validateSettings(settings, environment, f.config));
      for (const key of Object.keys(privacyEnvironment)) assert.equal(settings.env[key], environment[key]);
      const before = await fs.readFile(file), stat = await fs.stat(file);
      assert.equal(await syncRuntimeSettings(f.config, f.service, scope), false);
      assert.deepEqual(await fs.readFile(file), before);
      assert.equal((await fs.stat(file)).mtimeMs, stat.mtimeMs);
    }
  }
});

test('a concurrent native preference save is merged again before the local channel is published', async t => {
  const f = await fixture(t), file = path.join(f.directory('default'), 'settings.json');
  await atomicJson(file, { language: 'before', env: { FIXTURE_PREFERENCE: 'before' } });
  const write = fs.writeFile.bind(fs); let prepared = 0;
  const native = { language: 'native-new', hooks: { Stop: [{ hooks: [{ command: 'native-new-hook' }] }] },
    env: { FIXTURE_PREFERENCE: 'native-new', EXTRA_PREFERENCE: 'preserved' } };
  t.mock.method(fs, 'writeFile', async (target, ...args) => {
    const result = await write(target, ...args);
    if (String(target).startsWith(file + '.') && String(target).endsWith('.tmp') && ++prepared === 1)
      await write(file, JSON.stringify(native), { mode: 0o600 });
    return result;
  });
  assert.equal(await syncRuntimeSettings(f.config, f.service), true);
  assert.equal(prepared, 2);
  const settings = await readJson(file);
  assert.deepEqual(settings.hooks, native.hooks); assert.equal(settings.language, native.language);
  assert.deepEqual(settings.env, { ...native.env, ...privacyEnvironment, ...portableSettings(f) });
  assert.deepEqual(await fs.readdir(f.directory('default')), ['settings.json']);
});

test('repeated concurrent saves stop after bounded retries and retain the latest native settings', async t => {
  const f = await fixture(t), file = path.join(f.directory('default'), 'settings.json');
  await atomicJson(file, { revision: 0 });
  const write = fs.writeFile.bind(fs); let prepared = 0;
  t.mock.method(fs, 'writeFile', async (target, ...args) => {
    const result = await write(target, ...args);
    if (String(target).startsWith(file + '.') && String(target).endsWith('.tmp'))
      await write(file, JSON.stringify({ revision: ++prepared, nativePreference: 'keep latest' }), { mode: 0o600 });
    return result;
  });
  await assert.rejects(syncRuntimeSettings(f.config, f.service), /^Error: runtime_settings_busy$/);
  assert.equal(prepared, 5);
  assert.deepEqual(await readJson(file), { revision: 5, nativePreference: 'keep latest' });
  assert.deepEqual(await fs.readdir(f.directory('default')), ['settings.json']);
});

test('simultaneous launchers converge without replacing unrelated native settings', async t => {
  const f = await fixture(t), file = path.join(f.directory('default'), 'settings.json');
  const original = { permissions: { allow: ['Read'] }, env: { FIXTURE_PREFERENCE: 'keep' } };
  await atomicJson(file, original);
  const results = await Promise.all(Array.from({ length: 4 }, () => syncRuntimeSettings(f.config, f.service)));
  assert(results.some(Boolean));
  assert.deepEqual(await readJson(file), { ...original,
    env: { ...original.env, ...privacyEnvironment, ...portableSettings(f) } });
  assert.deepEqual(await fs.readdir(f.directory('default')), ['settings.json']);
});
