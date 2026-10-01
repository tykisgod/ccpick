import path from 'node:path';
import { readJson, atomicJson, listProfiles, getProfile, verifyIdentity, fail } from './core.mjs';
import { inheritedRuntimeScope, runtimeDirectory } from './runtime-client.mjs';

export const statePath = config => path.join(config.dataRoot, '..', 'state.json');
export async function registryState(config) {
  const state = await readJson(statePath(config), true);
  if (!state || state.version !== 2 || state.enabled !== true) fail('registry_not_enabled');
  return state;
}
export async function resolveProfile(config, query) {
  const profiles = await listProfiles(config);
  const exact = profiles.filter(p => [p.name, p.legacySlot, p.account?.email, p.email, p.label]
    .some(v => typeof v === 'string' && v.toLowerCase() === String(query).toLowerCase()));
  if (exact.length === 1) return exact[0];
  if (exact.length > 1) fail('ambiguous_account');
  fail('account_not_found');
}
export async function selectProfile(config, query, { expectedState } = {}) {
  const state = await registryState(config);
  if (state.maintenance) fail('migration_in_progress');
  const p = await resolveProfile(config, query);
  if (!p.account) fail('login_required');
  const ids = await verifyIdentity(config, p);
  if (ids.oauthAccount?.accountUuid !== p.account.uuid) fail('wrong_account');
  const current = await registryState(config);
  if (current.maintenance) fail('migration_in_progress');
  if (expectedState && (current.selected !== expectedState.selected || current.selectedAt !== expectedState.selectedAt))
    fail('selection_changed');
  const generation = new Date(Math.max(Date.now(), (Date.parse(current.selectedAt) || 0) + 1)).toISOString();
  const selectionReceipt = Object.freeze({ profileId: p.name, generation });
  await atomicJson(statePath(config), { ...current, selected: p.name, selectedAt: generation });
  return { ...p, selectionReceipt };
}
export async function contextProfile(config, args = [], environment = process.env) {
  const state = await registryState(config);
  const scope = inheritedRuntimeScope(config, environment);
  if (scope !== null) {
    if (scope === 'default') return getProfile(config, state.selected);
    const active = await readJson(path.join(runtimeDirectory(config, scope), '..', 'active.json'), true);
    return getProfile(config, active?.profileId ?? scope);
  }
  let name = environment.CCPICK_ACCOUNT_PROFILE;
  const directory = environment.CLAUDE_CONFIG_DIR;
  if (name) {
    const p = await getProfile(config, name);
    if (directory && path.resolve(directory).toLowerCase() !== path.resolve(p.configDirectory).toLowerCase()) fail('profile_context_conflict');
    return p;
  }
  if (directory) {
    const profiles = await listProfiles(config);
    const match = profiles.find(p => path.resolve(p.configDirectory).toLowerCase() === path.resolve(directory).toLowerCase());
    if (!match) fail('unmanaged_config_directory');
    return match;
  }
  if (environment.CLAUDE_CODE_SESSION_ID || environment.CLAUDECODE === '1') {
    const adopted = (await listProfiles(config)).filter(p => p.storage === 'native-default');
    if (adopted.length !== 1) fail('profile_context_unknown');
    return adopted[0];
  }
  return getProfile(config, state.selected);
}

export function installationForService(root) {
  return path.join(root, 'private', 'account-profiles', 'install.json');
}
