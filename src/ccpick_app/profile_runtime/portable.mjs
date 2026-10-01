import fs from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { randomUUID } from 'node:crypto';
import { loadInstall, readJson, atomicJson, createProfile, getProfile, listProfiles, verifyIdentity, fail } from './core.mjs';
import { registryState, resolveProfile, contextProfile } from './registry.mjs';
import { inheritedRuntimeScope, launchRuntime, runtimeRequest } from './runtime-client.mjs';
import { checkedSelectionGuard, checkedSelectionReceipt } from './active-runtime.mjs';
import { upstreamProxy, prepareNetwork } from './network.mjs';

const safeReason = error => /^[a-z_]{1,80}$/.test(error?.message ?? '') ? error.message : 'runtime_unavailable';
const publicProfile = profile => ({ name: profile.name, label: profile.label,
  email: profile.account?.email ?? profile.email ?? '', account: profile.account ?? null,
  autoSwitchEnabled: profile.autoSwitchEnabled, createdAt: profile.createdAt });

export const launch = launchRuntime;
export async function selectRuntimeProfile(config, args, { request = runtimeRequest, resolve = resolveProfile } = {}) {
  if (config.seamlessAccounts !== true) fail('seamless_runtime_disabled');
  let expectedState;
  if (args.length === 5 && args[1] === '--expected-selected' && args[3] === '--expected-selected-at')
    expectedState = checkedSelectionGuard({ selected: args[2], selectedAt: args[4] });
  else if (args.length !== 1) fail('invalid_arguments');
  const profile = await resolve(config, args[0]);
  const reply = await request(config, expectedState === undefined ? 'select' : 'select-guarded', {
    name: profile.name, ...(expectedState === undefined ? {} : { expectedState }),
  });
  if (expectedState === undefined) return profile;
  const selectionReceipt = checkedSelectionReceipt(reply?.selectionReceipt);
  if (selectionReceipt.profileId !== profile.name) fail('selection_changed');
  return { ...profile, selectionReceipt };
}

export async function registerBrowser(config, input, environment = process.env, { request = runtimeRequest } = {}) {
  const scope = inheritedRuntimeScope(config, environment);
  if (scope === null) fail('managed_session_required');
  const sessionId = /^[a-f0-9-]{36}$/.test(environment.CCPICK_RUNTIME_CLIENT_ID ?? '')
    ? environment.CCPICK_RUNTIME_CLIENT_ID : randomUUID();
  if (typeof input !== 'string' || input.length > 32768 || /[\x00-\x20\x7f"\\]/.test(input)) fail('invalid_login_url');
  let url; try { url = new URL(input); } catch { fail('invalid_login_url'); }
  if (url.protocol !== 'https:' || url.username || url.password || url.port ||
      !['claude.ai', 'claude.com', 'console.anthropic.com', 'platform.claude.com'].includes(url.hostname)) fail('invalid_login_url');
  if (['/oauth/authorize', '/cai/oauth/authorize'].includes(url.pathname)) {
    if (!['claude.ai', 'claude.com'].includes(url.hostname)) fail('unsupported_login_method');
    for (const key of ['state', 'client_id', 'code_challenge', 'redirect_uri'])
      if (url.searchParams.getAll(key).length !== 1 || !url.searchParams.get(key)) fail('invalid_login_url');
    await request(config, 'login-begin', { scope, sessionId,
      oauthState: url.searchParams.get('state'), oauthChallenge: url.searchParams.get('code_challenge') });
  } else if (['state', 'code_challenge', 'redirect_uri'].some(key => url.searchParams.has(key))) fail('invalid_login_url');
  return { ok: true };
}

export async function addAccount(config, { email, label } = {}, { create = createProfile, launch = launchRuntime, request = runtimeRequest } = {}) {
  const profile = await create(config, { email, label });
  const stateFile = path.join(config.dataRoot, '..', 'state.json');
  const state = await registryState(config);
  const firstAccount = !state.selected;
  if (!state.selected) await atomicJson(stateFile, { ...state, selected: profile.name, selectedAt: new Date().toISOString() });
  const beforeLogin = await registryState(config);
  const code = await launch(config, profile.name, 'login', [], { runtimeScope: profile.name, loginEmail: email });
  if (code !== 0) return { ok: false, name: profile.name, reason: 'login_required' };
  const verified = await getProfile(config, profile.name);
  if (!verified.account) fail('auth_unverified');
  if (!firstAccount) return { ok: true, ...publicProfile(verified), selected: false };
  try {
    await request(config, 'select-guarded', { name: profile.name, scope: 'default',
      expectedState: checkedSelectionGuard({ selected: beforeLogin.selected, selectedAt: beforeLogin.selectedAt }) });
    return { ok: true, ...publicProfile(verified), selected: true };
  } catch (error) {
    if (error.message !== 'selection_changed') throw error;
    return { ok: true, ...publicProfile(verified), selected: false, reason: 'selection_changed' };
  }
}

export async function portableCommand(config, command, args = [], dependencies = {}) {
  upstreamProxy(config.upstreamProxy);
  const request = dependencies.request ?? runtimeRequest;
  if (command === 'list') return { ok: true, profiles: (await listProfiles(config)).map(publicProfile) };
  if (command === 'status') {
    const state = await registryState(config);
    return { ok: true, selected: state.selected ?? null, selectedAt: state.selectedAt ?? null,
      maintenance: Boolean(state.maintenance), managed: true };
  }
  if (command === 'context') return { ok: true, profile: publicProfile(await contextProfile(config, args)) };
  if (command === 'ready') {
    const profile = await resolveProfile(config, args[0]);
    return request(config, 'ready', { name: profile.name });
  }
  if (command === 'select' || command === 'select-guarded') {
    const profile = await resolveProfile(config, args[0]);
    let expectedState;
    if (command === 'select-guarded') {
      if (args.length !== 3) fail('selection_changed');
      expectedState = checkedSelectionGuard({ selected: args[1], selectedAt: args[2] });
      if (profile.autoSwitchEnabled === false) fail('account_manual_only');
    }
    return request(config, command, { name: profile.name, ...(expectedState ? { expectedState } : {}) });
  }
  if (command === 'auto-policy') {
    if (args.length !== 2 || !['on', 'off'].includes(args[1])) fail('invalid_arguments');
    const profile = await resolveProfile(config, args[0]);
    await atomicJson(path.join(profile.root, 'switch-policy.json'), { version: 1, autoSwitchEnabled: args[1] === 'on' });
    return { ok: true, name: profile.name, autoSwitchEnabled: args[1] === 'on' };
  }
  if (command === 'add') {
    if (!args[0] || args.length > 2) fail('invalid_arguments');
    return addAccount(config, { email: args[0], label: args[1] }, dependencies);
  }
  if (command === 'login') {
    const state = await registryState(config);
    const profile = await resolveProfile(config, args[0] ?? state.selected);
    await verifyIdentity(config, profile);
    const code = await (dependencies.launch ?? launchRuntime)(config, profile.name, 'login', [],
      { runtimeScope: profile.name, loginEmail: profile.account?.email ?? profile.email });
    return { ok: code === 0, code };
  }
  if (command === 'run') {
    const profile = await contextProfile(config);
    const scope = inheritedRuntimeScope(config) ?? 'default';
    const code = await (dependencies.launch ?? launchRuntime)(config, profile.name, 'run', args,
      { runtimeScope: scope });
    return { ok: code === 0, code };
  }
  if (command === 'doctor') {
    const state = await registryState(config);
    await (dependencies.network ?? prepareNetwork)(config, { checkOnly: true });
    return { ok: true, enabled: true, profiles: (await listProfiles(config)).length,
      selected: state.selected ?? null, upstream: 'configured_loopback_connect',
      upstreamListening: true, accountsVerified: false,
      systemTrustModified: false, globalBackendModified: false };
  }
  if (command === 'browser-register') return registerBrowser(config, args[0], dependencies.environment);
  fail('runtime_command_invalid');
}

export async function main(argv = process.argv.slice(2)) {
  try {
    const [install, command, ...args] = argv;
    if (!install || !command) fail('invalid_arguments');
    const config = await loadInstall(install);
    if (command === 'browser-register') {
      let input = ''; for await (const chunk of process.stdin) { input += chunk; if (input.length > 32768) fail('invalid_login_url'); }
      const value = JSON.parse(input);
      if (!value || Object.keys(value).length !== 1 || typeof value.url !== 'string') fail('invalid_login_url');
      args.push(value.url);
    }
    const result = await portableCommand(config, command, args);
    if (!['run', 'login'].includes(command) || result?.ok === false) process.stdout.write(JSON.stringify(result) + '\n');
    return result?.code ?? (result?.ok === false ? 1 : 0);
  } catch (error) { process.stderr.write(safeReason(error) + '\n'); return 1; }
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) process.exitCode = await main();
