import path from 'node:path';
import { getProfile, verifyIdentity, globalConfigFile, readJson, atomicJson, withLeaseLock,
  executeNative, fail } from './core.mjs';
import { registryState } from './registry.mjs';
import { prepareNetwork } from './network.mjs';
import { inheritedRuntimeScope, runtimeRequest } from './runtime-client.mjs';
import { AccountEgressRouter } from './account-egress.mjs';

export function loginUrl(value, email, { allowNoHint = false } = {}) {
  if (typeof value !== 'string' || value.length > 32768 || /[\x00-\x20\x7f"\\]/.test(value)) fail('invalid_login_url');
  let url;
  try { url = new URL(value); } catch { fail('invalid_login_url'); }
  if (url.protocol !== 'https:' || url.username || url.password || url.port ||
      !['claude.ai', 'claude.com', 'console.anthropic.com', 'platform.claude.com'].includes(url.hostname)) fail('invalid_login_url');
  const oauth = ['/oauth/authorize', '/cai/oauth/authorize'].includes(url.pathname);
  if (!oauth && ['state', 'code_challenge', 'redirect_uri'].some(k => url.searchParams.has(k))) fail('invalid_login_url');
  if (oauth) {
    if (!['claude.ai', 'claude.com'].includes(url.hostname)) fail('unsupported_login_method');
    for (const key of ['state', 'client_id', 'code_challenge', 'redirect_uri']) {
      if (url.searchParams.getAll(key).length !== 1 || !url.searchParams.get(key)) fail('invalid_login_url');
    }
    if (!email && !allowNoHint) fail('account_identity_unavailable');
    if (email) url.searchParams.set('login_hint', email);
  }
  return { url: url.href, oauth };
}

async function sessionProfile(config, environment) {
  const state = await registryState(config);
  if (state.maintenance) fail('migration_in_progress');
  if (!environment.CCPICK_ACCOUNT_PROFILE || !/^[a-f0-9-]{36}$/.test(environment.CCPICK_ACCOUNT_LEASE ?? '')) fail('managed_session_required');
  const p = await getProfile(config, environment.CCPICK_ACCOUNT_PROFILE);
  const directory = environment.CLAUDE_CONFIG_DIR;
  if ((p.storage === 'native-default' && directory && path.resolve(directory) !== path.resolve(p.configDirectory)) ||
      (p.storage !== 'native-default' && (!directory || path.resolve(directory) !== path.resolve(p.configDirectory)))) fail('profile_context_conflict');
  await verifyIdentity(config, p);
  return p;
}

export async function openAccountBrowser(config, value, { environment = process.env,
  network = prepareNetwork, open = executeNative, request = runtimeRequest } = {}) {
  const scope = inheritedRuntimeScope(config, environment);
  if (scope !== null) {
    if (!/^[a-f0-9-]{36}$/.test(environment.CCPICK_RUNTIME_CLIENT_ID ?? '')) fail('managed_session_required');
    const target = loginUrl(value, undefined, { allowNoHint: true });
    let browserConfig = config;
    if (target.oauth) {
      const url = new URL(target.url);
      const intent = await request(config, 'login-begin', { scope, sessionId: environment.CCPICK_RUNTIME_CLIENT_ID,
        oauthState: url.searchParams.get('state'), oauthChallenge: url.searchParams.get('code_challenge') });
      if (intent?.household) browserConfig = (await new AccountEgressRouter(config, { network }).group(intent.household)).networkConfig;
    } else {
      const route = await request(config, 'browser-route', { scope });
      if (route?.scope !== scope || !['A', 'B', 'C', 'D'].includes(route.household)) fail('account_egress_invalid');
      browserConfig = (await new AccountEgressRouter(config, { network }).group(route.household)).networkConfig;
    }
    await network(browserConfig, { checkOnly: true });
    const browserEnvironment = { ...environment, CCPICK_FIXED_EGRESS_HOME: browserConfig.serviceRoot,
      CCPICK_FIXED_EGRESS_PROFILE: browserConfig.networkProfile };
    const result = await open(browserConfig.browser, [target.url], { env: browserEnvironment, capture: true, timeoutMs: 120000 });
    if (result.code !== 0) fail('house_browser_not_ready');
    return 0;
  }
  const p = await sessionProfile(config, environment);
  const target = loginUrl(value, p.account?.email ?? p.email);
  const token = environment.CCPICK_ACCOUNT_LEASE;
  let entered = false;
  await withLeaseLock(p, async (records, dir) => {
    const own = records.find(r => r.token === token);
    if (!own || !own.childPid) fail('managed_session_required');
    if (!target.oauth) return;
    if (records.some(r => r.token !== token)) fail('interactive_login_busy');
    if (own.mode === 'run' || own.interactiveLogin) {
      if (!p.account) fail('account_identity_unavailable');
      await atomicJson(path.join(dir, `${token}.json`), { ...own, mode: 'login', interactiveLogin: true });
      entered = true;
    } else if (own.mode !== 'login') fail('managed_session_required');
  });
  try {
    await network(config, { checkOnly: true });
    const result = await open(config.browser, [target.url], { env: environment, capture: true, timeoutMs: 120000 });
    if (result.code !== 0) fail('house_browser_not_ready');
    return 0;
  } catch (error) {
    if (entered) await finishInteractiveLogin(config, environment).catch(() => {});
    throw error;
  }
}

export async function finishInteractiveLogin(config, environment = process.env, { request = runtimeRequest } = {}) {
  const scope = inheritedRuntimeScope(config, environment);
  if (scope !== null) {
    await request(config, 'login-finished', { scope, sessionId: environment.CCPICK_RUNTIME_CLIENT_ID },
      { start: false, timeoutMs: 3000 }).catch(() => {});
    return 0;
  }
  const p = await sessionProfile(config, environment);
  const token = environment.CCPICK_ACCOUNT_LEASE;
  await withLeaseLock(p, async (records, dir) => {
    const own = records.find(r => r.token === token);
    if (!own) fail('managed_session_required');
    if (!own.interactiveLogin) return;
    const current = await readJson(globalConfigFile(p));
    if (!p.account || current.oauthAccount?.accountUuid !== p.account.uuid ||
        current.oauthAccount?.emailAddress?.toLowerCase() !== p.account.email.toLowerCase()) fail('wrong_account');
    const { interactiveLogin, ...record } = own;
    await atomicJson(path.join(dir, `${token}.json`), { ...record, mode: 'run' });
  });
  return 0;
}
