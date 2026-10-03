import path from 'node:path';
import fs from 'node:fs/promises';
import { randomUUID } from 'node:crypto';
import { regular, object, validName, digest, fail, privacyEnvironment, backgroundShellEnvironment } from './core.mjs';

async function snapshot(file) {
  try {
    const before = await regular(file);
    if (before.size > 1024 * 1024) fail('file_too_large');
    const bytes = await fs.readFile(file);
    const after = await regular(file);
    const identity = info => `${info.dev}:${info.ino}:${info.size}:${info.mtimeMs}:${info.ctimeMs}`;
    if (identity(before) !== identity(after)) return { unstable: true };
    return { signature: `${identity(after)}:${digest(bytes)}`,
      settings: JSON.parse(bytes.toString('utf8').replace(/^\uFEFF/, '')) };
  } catch (error) { if (error.code === 'ENOENT') return { signature: null, settings: {} }; throw error; }
}

/** Native daemon dispatch can remove a parent's custom headers. The worker
 * reloads its own private user settings before initializing authentication.
 * Only the stable local channel is stored here; account grants stay in vaults. */
export function runtimeSettingsEnvironment(service, scope = 'default') {
  if (scope !== 'default' && !validName(scope)) fail('runtime_scope_invalid');
  if (!/^[a-f0-9]{64}$/.test(service?.key ?? '')) fail('runtime_key_invalid');
  const port = service.scopeProxyPorts?.[scope] ?? service.proxyPort;
  if (service.egressVersion === 1 && port !== undefined && (!Number.isInteger(port) || port < 1 || port > 65535))
    fail('runtime_service_invalid');
  const proxy = `http://127.0.0.1:${port}`;
  return {
    ...backgroundShellEnvironment,
    ANTHROPIC_BASE_URL: 'https://api.anthropic.com',
    ANTHROPIC_CUSTOM_HEADERS: `Authorization: Bearer ${service.key}\nx-ccpick-account-runtime: ${service.key}\nx-ccpick-account-scope: ${scope}`,
    ...(service.egressVersion === 1 && port !== undefined ? {
      HTTPS_PROXY: proxy, HTTP_PROXY: proxy, ALL_PROXY: proxy,
      WS_PROXY: proxy, WSS_PROXY: proxy,
      NO_PROXY: '127.0.0.1,localhost,::1',
    } : {}),
  };
}

export async function syncRuntimeSettings(config, service, scope = 'default') {
  if (config.seamlessAccounts !== true) fail('seamless_runtime_disabled');
  const managed = { ...privacyEnvironment, ...runtimeSettingsEnvironment(service, scope),
    CCPICK_ACCOUNT_RUNTIME: '1', CCPICK_RUNTIME_SCOPE: scope,
    CCPICK_RUNTIME_INSTALL: path.resolve(config.dataRoot, '..', 'install.json') };
  const replaced = new Set([...Object.keys(managed), 'CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC']);
  const root = path.resolve(config.dataRoot, '..', 'active-runtime');
  const scopeRoot = scope === 'default' ? root : path.join(root, 'scopes', scope);
  const directory = path.join(scopeRoot, 'claude');
  await regular(root, true);
  if (scope !== 'default') { await regular(path.join(root, 'scopes'), true); await regular(scopeRoot, true); }
  await regular(directory, true);
  const file = path.join(directory, 'settings.json');
  for (let attempt = 0; attempt < 5; attempt++) {
    const before = await snapshot(file);
    if (before.unstable) continue;
    const settings = before.settings;
    if (!object(settings) || (settings.env !== undefined && !object(settings.env))) fail('settings_conflict');
    const env = { ...settings.env };
    for (const key of Object.keys(env)) if (replaced.has(key.toUpperCase())) delete env[key];
    Object.assign(env, managed);
    if (JSON.stringify(settings.env) === JSON.stringify(env)) return false;
    const temporary = `${file}.${randomUUID()}.tmp`;
    try {
      await fs.writeFile(temporary, JSON.stringify({ ...settings, env }, null, 2) + '\n', { mode: 0o600, flag: 'wx' });
      const current = await snapshot(file);
      if (current.unstable || current.signature !== before.signature) continue;
      await fs.rename(temporary, file);
      return true;
    } catch (error) {
      if (process.platform !== 'win32' || !['EPERM', 'EACCES', 'EBUSY'].includes(error.code)) throw error;
      await new Promise(resolve => setTimeout(resolve, 25 * (attempt + 1)));
    } finally { await fs.unlink(temporary).catch(error => { if (error.code !== 'ENOENT') throw error; }); }
  }
  fail('runtime_settings_busy');
}
