import fs from 'node:fs/promises';
import path from 'node:path';
import os from 'node:os';
import { randomUUID, createHash } from 'node:crypto';
import { spawn } from 'node:child_process';

export const digest = value => createHash('sha256').update(value).digest('hex');
export const fail = reason => { throw new Error(reason); };
export const object = value => value && typeof value === 'object' && !Array.isArray(value);
export const privacyEnvironment = Object.freeze({ DISABLE_TELEMETRY: '1', DISABLE_ERROR_REPORTING: '1',
  DISABLE_AUTOUPDATER: '1', DISABLE_FEEDBACK_COMMAND: '1' });
export const backgroundShellEnvironment = Object.freeze({ CLAUDE_CODE_DISABLE_BG_SHELL_PRESSURE_REAP: '1' });
export const validName = name => /^[a-z][a-z0-9_-]{0,47}$/.test(name ?? '') &&
  !/^(con|prn|aux|nul|com[1-9]|lpt[1-9])$/i.test(name);
export function textValue(value, max = 120) {
  if (typeof value !== 'string' || !value.trim() || value.length > max || /[\x00-\x1f\x7f]/.test(value)) fail('invalid_text');
  return value.trim();
}
export async function regular(file, directory = false) {
  const info = await fs.lstat(file);
  if (info.isSymbolicLink() || !(directory ? info.isDirectory() : info.isFile())) fail('unsafe_path');
  if (process.platform !== 'win32' && (info.uid !== process.getuid() || (info.mode & 0o077))) fail('private_permissions_required');
  return info;
}
export async function readJson(file, optional = false) {
  try {
    const info = await regular(file);
    if (info.size > 1024 * 1024) fail('file_too_large');
    return JSON.parse((await fs.readFile(file, 'utf8')).replace(/^\uFEFF/, ''));
  } catch (e) { if (optional && e.code === 'ENOENT') return null; throw e; }
}
export async function atomicJson(file, value) {
  const temporary = `${file}.${randomUUID()}.tmp`;
  try {
    await fs.writeFile(temporary, JSON.stringify(value, null, 2) + '\n', { mode: 0o600, flag: 'wx' });
    for (let attempt = 0; ; attempt++) {
      try { await fs.rename(temporary, file); break; }
      catch (error) {
        if (process.platform !== 'win32' || !['EPERM', 'EACCES', 'EBUSY'].includes(error.code) || attempt >= 7) throw error;
        await new Promise(resolve => setTimeout(resolve, 25 * (attempt + 1)));
      }
    }
  } finally { await fs.unlink(temporary).catch(e => { if (e.code !== 'ENOENT') throw e; }); }
}
export async function loadInstall(file) {
  const config = await readJson(file);
  if (config?.version !== 1 || !['win32', 'darwin'].includes(config.platform) || config.platform !== process.platform ||
      !['serviceRoot', 'dataRoot', 'native', 'browser'].every(k => typeof config[k] === 'string' && path.isAbsolute(config[k])) ||
      !validName(config.networkProfile)) fail('installation_invalid');
  if (config.accountBrowser !== undefined && (typeof config.accountBrowser !== 'string' || !path.isAbsolute(config.accountBrowser))) fail('installation_invalid');
  await regular(config.dataRoot, true);
  if (path.resolve(config.dataRoot) !== path.join(path.resolve(config.serviceRoot), 'private', 'account-profiles', 'data'))
    fail('installation_invalid');
  return config;
}
export function profileDirectory(config, name) {
  if (!validName(name)) fail('invalid_name');
  return path.join(config.dataRoot, name);
}
export async function readEgressPolicy(config, name) {
  const root = profileDirectory(config, name);
  await regular(root, true);
  let policy;
  try { policy = await readJson(path.join(root, 'egress-policy.json')); }
  catch (error) { if (error.code === 'ENOENT') return null; throw error; }
  if (!object(policy) || policy.version !== 1 || !['A', 'B', 'C', 'D'].includes(policy.household) ||
      Object.keys(policy).some(key => !['version', 'household'].includes(key))) fail('account_egress_invalid');
  return policy;
}
export async function getProfile(config, name) {
  const root = profileDirectory(config, name);
  await regular(root, true);
  const manifest = await readJson(path.join(root, 'profile.json'));
  if (manifest?.version !== 1 || manifest.name !== name || !object(manifest.identity) ||
      !['userID', 'machineID'].every(k => /^[a-f0-9]{64}$/.test(manifest.identity[k] ?? ''))) fail('profile_invalid');
  textValue(manifest.label);
  if (manifest.storage !== undefined && manifest.storage !== 'native-default') fail('profile_invalid');
  let autoSwitchEnabled = true;
  try {
    const policy = await readJson(path.join(root, 'switch-policy.json'));
    if (!object(policy) || policy.version !== 1 || typeof policy.autoSwitchEnabled !== 'boolean' ||
        Object.keys(policy).some(key => !['version', 'autoSwitchEnabled'].includes(key))) fail('profile_invalid');
    autoSwitchEnabled = policy.autoSwitchEnabled;
  } catch (error) { if (error.code !== 'ENOENT') throw error; }
  const egress = await readEgressPolicy(config, name);
  return { ...manifest, autoSwitchEnabled, household: egress?.household ?? null,
    householdSource: egress ? 'explicit' : 'default', root,
    configDirectory: manifest.storage === 'native-default' ? path.join(os.homedir(), '.claude') : path.join(root, 'claude') };
}
export const globalConfigFile = p => p.storage === 'native-default'
  ? path.join(os.homedir(), '.claude.json') : path.join(p.configDirectory, '.claude.json');
export async function listProfiles(config, query = '') {
  const results = [];
  for (const entry of await fs.readdir(config.dataRoot, { withFileTypes: true })) {
    if (!validName(entry.name)) continue;
    if (!entry.isDirectory() || entry.isSymbolicLink()) fail('unsafe_path');
    let p;
    try { p = await getProfile(config, entry.name); }
    catch (e) { if (e.code === 'ENOENT') continue; throw e; } // An addition may still be initializing.
    if (`${p.name} ${p.label} ${p.email ?? ''} ${p.account?.email ?? ''}`.toLowerCase().includes(query.toLowerCase())) results.push(p);
  }
  return results.sort((a, b) => a.name.localeCompare(b.name, undefined, { numeric: true }));
}
export async function createProfile(config, { name, label, email, autoSwitchEnabled, household } = {}, { initialize = initializeNative } = {}) {
  if (name !== undefined && !validName(name)) fail('invalid_name');
  if (label !== undefined) textValue(label);
  if (autoSwitchEnabled !== undefined && typeof autoSwitchEnabled !== 'boolean') fail('invalid_arguments');
  if (household !== undefined && !['A', 'B', 'C', 'D'].includes(household)) fail('invalid_household');
  if (email !== undefined && (textValue(email, 254) !== email || !/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(email))) fail('invalid_email');
  if (email && (await listProfiles(config)).some(p => (p.account?.email ?? p.email ?? '').toLowerCase() === email.toLowerCase()))
    fail('account_already_registered');
  let root;
  if (name) {
    root = profileDirectory(config, name);
    await fs.mkdir(root, { mode: 0o700 });
  } else {
    for (let n = 1; ; n++) {
      name = `account-${String(n).padStart(4, '0')}`;
      root = profileDirectory(config, name);
      try { await fs.mkdir(root, { mode: 0o700 }); break; }
      catch (e) { if (e.code !== 'EEXIST') throw e; }
    }
  }
  const configDirectory = path.join(root, 'claude');
  await fs.mkdir(configDirectory, { mode: 0o700 });
  await fs.mkdir(path.join(root, 'leases'), { mode: 0o700 });
  await atomicJson(path.join(configDirectory, '.claude.json'), {});
  await atomicJson(path.join(configDirectory, 'settings.json'), {
    env: { ...privacyEnvironment },
  });
  await initialize(config, { root, name, configDirectory });
  const ids = await readJson(path.join(configDirectory, '.claude.json'));
  if (!['userID', 'machineID'].every(k => /^[a-f0-9]{64}$/.test(ids?.[k] ?? ''))) fail('native_identity_unavailable');
  const record = { version: 1, name, label: label ?? name, createdAt: new Date().toISOString(),
    ...(email ? { email } : {}), identity: Object.fromEntries(['userID', 'machineID'].map(k => [k, digest(ids[k])])), account: null };
  if (autoSwitchEnabled !== undefined) {
    await atomicJson(path.join(root, 'switch-policy.json'), { version: 1, autoSwitchEnabled });
  }
  if (household !== undefined) {
    await atomicJson(path.join(root, 'egress-policy.json'), { version: 1, household });
  }
  await atomicJson(path.join(root, 'profile.json'), record);
  return getProfile(config, name);
}
export async function verifyIdentity(config, p) {
  await regular(p.configDirectory, true);
  const ids = await readJson(globalConfigFile(p));
  for (const key of ['userID', 'machineID']) {
    if (!/^[a-f0-9]{64}$/.test(ids?.[key] ?? '') || digest(ids[key]) !== p.identity[key]) fail('identity_changed');
  }
  for (const other of await listProfiles(config)) {
    if (other.name !== p.name && ['userID', 'machineID'].some(k => other.identity[k] === p.identity[k])) fail('identity_reused');
  }
  const legacyFile = path.join(os.homedir(), '.claude.json');
  try {
    const legacy = JSON.parse((await fs.readFile(legacyFile, 'utf8')).replace(/^\uFEFF/, ''));
    if (p.storage !== 'native-default' && ['userID', 'machineID'].some(k => legacy[k] === ids[k])) fail('legacy_identity_reused');
  } catch (e) { if (e.code !== 'ENOENT') throw e; }
  return ids;
}

const proxyKeys = new Set(['HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY', 'WS_PROXY', 'WSS_PROXY', 'NO_PROXY']);
const preferenceEnvironment = new Set(['CLAUDE_CODE_DISABLE_AUTO_MEMORY', 'CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS',
  'CLAUDE_CODE_SUBAGENT_MODEL', 'CLAUDE_CODE_DISABLE_REFUSAL_FALLBACK',
  'CLAUDE_CODE_NO_FLICKER', 'CLAUDE_CODE_MAX_WEB_SEARCHES_PER_SESSION', 'ANTHROPIC_DEFAULT_OPUS_MODEL',
  'ANTHROPIC_DEFAULT_SONNET_MODEL', 'ANTHROPIC_DEFAULT_HAIKU_MODEL']);
function isolatedKey(key) {
  return /^(CLAUDE|ANTHROPIC|OTEL|CCPICK_|CCPICK|CSWAP)/.test(key) || proxyKeys.has(key) ||
    ['BROWSER', 'NODE_OPTIONS', 'NODE_TLS_REJECT_UNAUTHORIZED', 'NODE_EXTRA_CA_CERTS', 'DISABLE_TELEMETRY',
      'DISABLE_ERROR_REPORTING', 'DISABLE_AUTOUPDATER', 'DISABLE_FEEDBACK_COMMAND', 'DO_NOT_TRACK', 'ELECTRON_RUN_AS_NODE'].includes(key);
}
export function childEnvironment(config, p, proxy, environment = process.env) {
  const env = Object.fromEntries(Object.entries(environment).filter(([k]) => !isolatedKey(k.toUpperCase())));
  Object.assign(env, {
    CLAUDE_CONFIG_DIR: p.configDirectory, BROWSER: config.accountBrowser ?? config.browser,
    ...privacyEnvironment,
    ...backgroundShellEnvironment,
  });
  if (p.storage === 'native-default') delete env.CLAUDE_CONFIG_DIR;
  env.CCPICK_ACCOUNT_PROFILE = p.name;
  for (const key of proxyKeys) env[key] = env[key.toLowerCase()] = key === 'NO_PROXY' ? '127.0.0.1,localhost,::1' : proxy;
  if (config.platform === 'darwin') {
    const user = os.userInfo();
    env.HOME = user.homedir; env.USER = env.LOGNAME = user.username;
    env.CCPICK_FIXED_EGRESS_HOME = config.serviceRoot; env.CCPICK_FIXED_EGRESS_PROFILE = config.networkProfile;
  }
  return env;
}
export async function initializeNative(config, p) {
  const env = childEnvironment(config, p, 'http://127.0.0.1:1');
  const check = await executeNative(config.native, ['--setting-sources', 'user', 'auth', 'status', '--json'],
    { env, cwd: p.root, capture: true, timeoutMs: 20000 });
  let auth;
  try { auth = JSON.parse(check.stdout); } catch { fail('native_initialization_failed'); }
  if (auth.loggedIn !== false) fail('inherited_login_refused');
  await executeNative(config.native, ['--setting-sources', 'user', '-p', 'Initialize local configuration.', '--max-turns', '0', '--no-session-persistence'],
    { env, cwd: p.root, capture: true, timeoutMs: 20000 });
}
export function validateSettings(value, environment, config = {}) {
  if (!object(value) || value.apiKeyHelper || value.forceLoginMethod === 'gateway' || value.forceLoginGatewayUrl)
    fail('settings_conflict');
  if (value.disableAllHooks === true && config.seamlessAccounts !== true) fail('account_hooks_disabled');
  if (value.browser !== undefined && value.browser !== environment.BROWSER) fail('settings_conflict');
  if (value.env !== undefined && !object(value.env)) fail('settings_conflict');
  for (const [key, v] of Object.entries(value.env ?? {})) {
    const name = key.toUpperCase();
    const accountHome = ['HOME', 'USERPROFILE', 'APPDATA', 'LOCALAPPDATA', 'USER', 'LOGNAME'].includes(name);
    if ((accountHome || isolatedKey(name)) && !preferenceEnvironment.has(name) && v !== environment[name]) fail('settings_conflict');
  }
  const executable = structuredClone({ hooks: value.hooks, statusLine: value.statusLine });
  if (typeof config.python === 'string' && path.isAbsolute(config.python) &&
      typeof config.dataRoot === 'string' && path.isAbsolute(config.dataRoot)) {
    const python = config.python.replaceAll('\\', '/');
    const bridge = path.join(config.dataRoot, '..', 'app', 'bridge.py').replaceAll('\\', '/');
    if (!/["$`\r\n]/.test(python + bridge)) {
      const base = `"${python}" "${bridge}"`;
      for (const [event, verb] of [['UserPromptSubmit', 'guard'], ['Notification', 'login-finished'],
        ['SessionStart', 'session-event'], ['Stop', 'session-event'], ['StopFailure', 'session-event'], ['Notification', 'session-event'],
        ['PreToolUse', 'session-event'], ['PostToolUse', 'session-event'], ['PostToolBatch', 'session-event']]) {
        for (const group of Array.isArray(executable.hooks?.[event]) ? executable.hooks[event] : []) {
          if (event === 'Notification' && group?.matcher !== (verb === 'login-finished' ? 'auth_success' : 'idle_prompt')) continue;
          for (const hook of Array.isArray(group?.hooks) ? group.hooks : []) {
            if (hook?.type === 'command' && hook.command === `${base} ${verb}`) hook.command = '';
          }
        }
      }
      if (executable.statusLine?.type === 'command' && executable.statusLine.command === `${base} statusline`)
        executable.statusLine.command = '';
    }
  }
  const executableSettings = JSON.stringify(executable);
  if (/cswap|claude.swap|native_login|login-with-house|ccpick\s+(?:auto|switch|enroll)/i.test(executableSettings)) fail('legacy_hook_refused');
}
export async function checkProfileSettings(config, p, environment, { cwd } = {}) {
  for (const name of ['settings.json', 'settings.local.json', 'remote-settings.json']) {
    const data = await readJson(path.join(p.configDirectory, name), true);
    if (data) validateSettings(data, environment, config);
  }
  const managed = config.platform === 'win32'
    ? path.join(process.env.ProgramFiles ?? 'C:/Program Files', 'ClaudeCode', 'managed-settings.json')
    : '/Library/Application Support/ClaudeCode/managed-settings.json';
  try { validateSettings(JSON.parse((await fs.readFile(managed, 'utf8')).replace(/^\uFEFF/, '')), environment, config); }
  catch (e) { if (e.code !== 'ENOENT') throw e; }
  if (cwd) {
    for (let directory = path.resolve(cwd); ; directory = path.dirname(directory)) {
      for (const name of ['settings.json', 'settings.local.json']) {
        const file = path.join(directory, '.claude', name);
        let bytes;
        try {
          const info = await fs.stat(file);
          if (!info.isFile() || info.size > 1024 * 1024) fail('project_settings_invalid');
          bytes = await fs.readFile(file, 'utf8');
        } catch (e) { if (e.code === 'ENOENT') continue; throw e; }
        let value;
        try { value = JSON.parse(bytes.replace(/^\uFEFF/, '')); }
        catch { fail('project_settings_invalid'); }
        validateSettings(value, environment, config);
      }
      if (config.platform === 'win32' || path.dirname(directory) === directory) break;
    }
  }
}
export function nativeArguments(args = []) {
  const forbidden = new Set(['--settings', '--setting-sources', '--bare', '--bg', '--background', '--session-id',
    '--teleport', '--remote', '--remote-control', '--rc']);
  const commands = new Set(['auth', 'login', 'logout', 'agents', 'remote-control', 'rc', 'remote', 'sync', 'bridge',
    'desktop', 'setup-token', 'update', 'upgrade', 'install', 'logs', 'attach', 'kill', 'respawn', 'rm', 'status', 'daemon', 'run']);
  if (commands.has(args[0]) || args.some(a => forbidden.has(a.split('=')[0]))) fail('unsupported_native_option');
  if (args[0] === 'stop') {
    if (args.length !== 2 || !(/^[a-f0-9]{8}$/i.test(args[1]) || ['--help', '-h'].includes(args[1])))
      fail('unsupported_native_option');
    return [...args, '--setting-sources', 'user,project,local'];
  }
  for (let i = 0; i < args.length; i++) {
    if (args[i] === '--resume' || args[i] === '-r' || args[i].startsWith('--resume=')) {
      const value = args[i].includes('=') ? args[i].slice(9) : args[i + 1];
      if (value && !value.startsWith('-') && /[\\/:]/.test(value)) fail('external_session_refused');
    }
  }
  return [...args];
}
export function executeNative(executable, args, { env, cwd, capture = false, timeoutMs, onSpawn } = {}) {
  return new Promise((resolve, reject) => {
    const child = spawn(executable, args, { env, cwd, shell: false, windowsHide: capture,
      stdio: capture ? ['ignore', 'pipe', 'pipe'] : 'inherit' });
    let stdout = '', stderr = '', tooLarge = false;
    if (capture) {
      child.stdout.setEncoding('utf8'); child.stderr.setEncoding('utf8');
      child.stdout.on('data', s => { stdout += s; if (stdout.length > 1024 * 1024) { tooLarge = true; child.kill(); } });
      child.stderr.on('data', s => { stderr += s; if (stderr.length > 1024 * 1024) { tooLarge = true; child.kill(); } });
    }
    let trackingError = false;
    const tracked = onSpawn && child.pid ? Promise.resolve(onSpawn(child.pid)).catch(() => {
      trackingError = true; child.kill();
    }) : Promise.resolve();
    const interrupt = () => child.kill('SIGINT');
    const terminate = () => child.kill('SIGTERM');
    process.on('SIGINT', interrupt); process.on('SIGTERM', terminate);
    let timedOut = false;
    const timer = timeoutMs ? setTimeout(() => { timedOut = true; child.kill(); }, timeoutMs) : null;
    const cleanup = () => { clearTimeout(timer); process.off('SIGINT', interrupt); process.off('SIGTERM', terminate); };
    child.once('error', () => { cleanup(); reject(new Error('native_start_failed')); });
    child.once('exit', async (code, signal) => {
      cleanup();
      await tracked;
      if (trackingError) reject(new Error('profile_tracking_failed'));
      else if (tooLarge) reject(new Error('native_output_too_large'));
      else resolve({ code: code ?? (signal === 'SIGINT' ? 130 : 1), stdout, stderr, ...(timedOut ? { timedOut: true } : {}) });
    });
  });
}
export async function authStatus(config, p, env, invoke = executeNative) {
  const result = await invoke(config.native, ['--setting-sources', 'user', 'auth', 'status', '--json'],
    { env, cwd: p.root, capture: true, timeoutMs: 20000 });
  let status;
  try { status = JSON.parse(result.stdout); } catch { fail('auth_status_failed'); }
  if (status.loggedIn === false) return { loggedIn: false };
  if (result.code !== 0 || status.loggedIn !== true || status.authMethod !== 'claude.ai') fail('unexpected_auth_source');
  const configJson = await readJson(globalConfigFile(p));
  const account = configJson.oauthAccount;
  if (!account?.accountUuid || !account?.emailAddress) fail('account_identity_unavailable');
  if (p.email && p.email.toLowerCase() !== account.emailAddress.toLowerCase()) fail('wrong_account');
  if (p.account && p.account.uuid !== account.accountUuid) fail('wrong_account');
  return { loggedIn: true, uuid: account.accountUuid, email: account.emailAddress };
}
export async function bindAccount(config, p, account) {
  if (!account.loggedIn) fail('login_required');
  for (const other of await listProfiles(config)) {
    if (other.name !== p.name && other.account?.uuid === account.uuid) fail('account_already_registered');
  }
  const record = await readJson(path.join(p.root, 'profile.json'));
  if (record.account && record.account.uuid !== account.uuid) fail('wrong_account');
  await atomicJson(path.join(p.root, 'profile.json'), { ...record, account: { uuid: account.uuid, email: account.email },
    lastLoginAt: new Date().toISOString() });
}
export async function completeOnboardingAfterLogin(config, p, account) {
  if (account?.loggedIn !== true) fail('login_required');
  if (![account.uuid, account.email].every(value => typeof value === 'string' && value)) fail('account_identity_unavailable');
  const current = await getProfile(config, p.name);
  if (!current.account) fail('login_required');
  const client = await verifyIdentity(config, current);
  const oauth = client.oauthAccount;
  if (![current.account.uuid, current.account.email, oauth?.accountUuid, oauth?.emailAddress]
    .every(value => typeof value === 'string' && value)) fail('account_identity_unavailable');
  if (account.uuid !== current.account.uuid || account.uuid !== oauth.accountUuid ||
      [current.account.email, oauth.emailAddress, current.email ?? account.email]
        .some(email => email.toLowerCase() !== account.email.toLowerCase())) fail('wrong_account');
  if (client.hasCompletedOnboarding === true) return false;
  await atomicJson(globalConfigFile(current), { ...client, hasCompletedOnboarding: true });
  return true;
}

function alive(pid) {
  if (!Number.isSafeInteger(pid) || pid < 1) return false;
  try { process.kill(pid, 0); return true; } catch (e) { return e.code !== 'ESRCH'; }
}
export async function withLeaseLock(p, action) {
  const dir = path.join(p.root, 'leases');
  await regular(dir, true);
  const mutex = path.join(dir, 'mutex.json');
  let lock;
  for (let tries = 0; tries < 30; tries++) {
    try {
      lock = await fs.open(mutex, 'wx', 0o600);
      break;
    } catch (e) {
      if (process.platform === 'win32' && ['EPERM', 'EACCES', 'EBUSY'].includes(e.code)) {
        if (tries === 29) throw e;
        await new Promise(r => setTimeout(r, 50)); continue;
      }
      if (e.code !== 'EEXIST') throw e;
      let old;
      try { old = await readJson(mutex); }
      catch (readError) {
        if (readError.code === 'ENOENT') continue;
        if (!(readError instanceof SyntaxError) && !(process.platform === 'win32' &&
            ['EPERM', 'EACCES', 'EBUSY'].includes(readError.code))) throw readError;
        await new Promise(r => setTimeout(r, 50)); continue;
      }
      if (!alive(old.pid)) await fs.unlink(mutex).catch(e => { if (e.code !== 'ENOENT') throw e; });
      else await new Promise(r => setTimeout(r, 50));
    }
  }
  if (!lock) fail('profile_busy');
  try {
    await lock.writeFile(JSON.stringify({ pid: process.pid }));
    const records = [];
    for (const name of await fs.readdir(dir)) {
      if (name === 'mutex.json' || !name.endsWith('.json')) continue;
      const existingFile = path.join(dir, name);
      let existing;
      try { existing = await readJson(existingFile); }
      catch (error) {
        if (error.code === 'ENOENT') continue;
        throw error;
      }
      if (!alive(existing.pid) && !alive(existing.childPid) && !alive(existing.nativePid)) { await fs.unlink(existingFile); continue; }
      records.push(existing);
    }
    return await action(records, dir);
  } finally { await lock.close(); await fs.unlink(mutex); }
}
export async function lease(p, mode, { followDefault = false } = {}) {
  if (!['run', 'login', 'readiness'].includes(mode)) fail('invalid_lease');
  const token = randomUUID();
  const file = path.join(p.root, 'leases', `${token}.json`);
  const record = { pid: process.pid, mode, token, childPid: null, followDefault,
    ...(followDefault ? { handoffProtocol: 2 } : {}) };
  await withLeaseLock(p, async records => {
    if (records.some(r => mode === 'login' || r.mode === 'login' ||
      (mode === 'readiness' && r.mode === 'readiness'))) fail('profile_busy');
    await atomicJson(file, record);
  });
  return {
    id: token,
    onSpawn: async childPid => withLeaseLock(p, async records => {
      const current = records.find(item => item.token === token && item.pid === process.pid);
      if (!current) fail('profile_tracking_failed');
      await atomicJson(file, { ...current, childPid });
    }),
    release: async () => {
      const current = await readJson(file, true);
      if (current?.token === token) await fs.unlink(file);
    },
  };
}
export async function setAutoSwitchEnabled(config, name, enabled) {
  if (typeof enabled !== 'boolean') fail('invalid_arguments');
  const p = await getProfile(config, name);
  await withLeaseLock(p, async () => {
    await atomicJson(path.join(p.root, 'switch-policy.json'), { version: 1, autoSwitchEnabled: enabled });
  });
  return getProfile(config, name);
}
export async function exportRecipe(config, target) {
  const accounts = (await listProfiles(config)).map(p => ({ name: p.name, label: p.label,
    ...(p.email ? { email: p.email } : {}), ...(p.autoSwitchEnabled === false ? { autoSwitchEnabled: false } : {}) }));
  await fs.writeFile(target, JSON.stringify({ version: 1, kind: 'claude-profile-recipe', accounts }, null, 2) + '\n',
    { mode: 0o600, flag: 'wx' });
  return accounts.length;
}
export async function importRecipe(config, file, dependencies = {}) {
  const recipe = await readJson(file);
  if (recipe?.version !== 1 || recipe.kind !== 'claude-profile-recipe' || !Array.isArray(recipe.accounts) ||
      Object.keys(recipe).some(k => !['version', 'kind', 'accounts'].includes(k))) fail('invalid_recipe');
  const names = new Set();
  for (const item of recipe.accounts) {
    if (!object(item) || Object.keys(item).some(k => !['name', 'label', 'email', 'autoSwitchEnabled'].includes(k)) || !validName(item.name) ||
        names.has(item.name)) fail('invalid_recipe');
    names.add(item.name); textValue(item.label);
    if (item.autoSwitchEnabled !== undefined && typeof item.autoSwitchEnabled !== 'boolean') fail('invalid_recipe');
    if (item.email && !/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(item.email)) fail('invalid_recipe');
    if (await readJson(path.join(profileDirectory(config, item.name), 'profile.json'), true)) fail('profile_exists');
  }
  for (const item of recipe.accounts) await createProfile(config, item, dependencies);
  return recipe.accounts.length;
}
