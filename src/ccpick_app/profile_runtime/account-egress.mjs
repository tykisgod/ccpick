import path from 'node:path';
import { atomicJson, digest, fail, getProfile, object, readEgressPolicy, readJson, regular, validName } from './core.mjs';
import { prepareNetwork } from './network.mjs';

const households = ['A', 'B', 'C', 'D'];
const sameKeys = (value, keys) => object(value) && Object.keys(value).length === keys.length &&
  keys.every(key => Object.hasOwn(value, key));
const allowedGroups = value => object(value) && Object.keys(value).every(key => households.includes(key));
function freeze(value) {
  if (object(value)) { for (const child of Object.values(value)) freeze(child); Object.freeze(value); }
  return value;
}
function checkedProxy(value) {
  let url;
  try { url = new URL(value); } catch { fail('network_not_ready'); }
  if (url.protocol !== 'http:' || !['127.0.0.1', '[::1]'].includes(url.hostname) ||
      !url.port || url.username || url.password || url.pathname !== '/' || url.search || url.hash) fail('network_not_ready');
  return value;
}

/** Resolve a complete account route before handing it to authentication or transport. */
export class AccountEgressRouter {
  constructor(config, { proxy, network = prepareNetwork, readyTtlMs = 60000, now = Date.now } = {}) {
    this.config = config;
    this.proxy = proxy;
    this.network = network;
    if (typeof readyTtlMs !== 'number' || !Number.isFinite(readyTtlMs) || readyTtlMs < 0) fail('invalid_arguments');
    this.readyTtlMs = Math.min(300000, readyTtlMs);
    this.now = now;
    this.ready = new Map();
    this.inflight = new Map();
    this.preparedRevision = null;
  }
  async _configuration() {
    const file = path.join(this.config.serviceRoot, 'config/account-egress.json');
    let value, absent = false;
    try { value = await readJson(file); }
    catch (error) { if (error.code === 'ENOENT') absent = true; else throw error; }
    if (absent) {
      const legacy = { version: 1, defaultGroup: 'A', groups: { A: {
        serviceRoot: this.config.serviceRoot, networkProfile: this.config.networkProfile } } };
      return { ...legacy, legacy: true, digest: digest(JSON.stringify(legacy)) };
    }
    if (!sameKeys(value, ['version', 'defaultGroup', 'groups']) || value.version !== 1 ||
        !households.includes(value.defaultGroup) || !allowedGroups(value.groups) ||
        !Object.keys(value.groups).length || !Object.hasOwn(value.groups, value.defaultGroup)) fail('account_egress_invalid');
    for (const route of Object.values(value.groups)) {
      if (!sameKeys(route, ['serviceRoot', 'networkProfile']) || typeof route.serviceRoot !== 'string' ||
          !path.isAbsolute(route.serviceRoot) || !validName(route.networkProfile)) fail('account_egress_invalid');
    }
    return { ...value, legacy: false, digest: digest(JSON.stringify(value)) };
  }
  async defaultGroup() { return (await this._configuration()).defaultGroup; }
  async configuredGroups() { return Object.freeze(Object.keys((await this._configuration()).groups)); }
  async _routeDigest(route) {
    const documents = [];
    for (const name of ['profiles.json', 'selector.json']) {
      try { documents.push(await readJson(path.join(route.serviceRoot, 'config', name))); }
      catch (error) { if (error.code === 'ENOENT') documents.push('absent'); else throw error; }
    }
    return digest(JSON.stringify(documents));
  }
  async group(household, { fresh = false } = {}) {
    if (!households.includes(household)) fail('invalid_household');
    const routes = await this._configuration();
    const route = routes.groups[household];
    if (!route) fail('household_not_configured');
    const networkConfig = { ...this.config, serviceRoot: route.serviceRoot, networkProfile: route.networkProfile };
    if (route.serviceRoot !== this.config.serviceRoot) {
      if (typeof this.config.browser !== 'string' || !path.isAbsolute(this.config.browser)) fail('account_egress_invalid');
      const relativeBrowser = path.relative(this.config.serviceRoot, this.config.browser);
      if (!relativeBrowser || relativeBrowser === '..' || relativeBrowser.startsWith('..' + path.sep) ||
          path.isAbsolute(relativeBrowser)) fail('account_egress_invalid');
      networkConfig.browser = path.join(route.serviceRoot, relativeBrowser);
    }
    const networkDigest = await this._routeDigest(route);
    const revision = digest(JSON.stringify([routes.digest, household, networkDigest]));
    if (!fresh && this.proxy && route.serviceRoot === this.config.serviceRoot && route.networkProfile === this.config.networkProfile) {
      if (this.preparedRevision === null) this.preparedRevision = revision;
      if (this.preparedRevision === revision) return freeze({ household, proxy: checkedProxy(this.proxy), revision, networkConfig });
    }
    const previous = this.ready.get(revision);
    if (!fresh && previous && this.now() >= previous.checkedAt && this.now() - previous.checkedAt < this.readyTtlMs)
      return previous.snapshot;
    if (this.inflight.has(revision)) return this.inflight.get(revision);
    const check = (async () => {
      await regular(route.serviceRoot, true);
      const result = await this.network(networkConfig, { checkOnly: true, requireBrowser: false });
      if (result?.apiMode !== 'official') fail('official_mode_required');
      const proxy = checkedProxy(result.proxy);
      if ((await this._configuration()).digest !== routes.digest || await this._routeDigest(route) !== networkDigest)
        fail('account_egress_changed');
      const snapshot = freeze({ household, proxy, revision, networkConfig });
      this.ready.set(revision, { checkedAt: this.now(), snapshot });
      return snapshot;
    })();
    this.inflight.set(revision, check);
    try { return await check; } finally { this.inflight.delete(revision); }
  }
  async account(name) {
    const profile = await getProfile(this.config, name);
    const configuration = await this._configuration();
    const policy = await readEgressPolicy(this.config, name);
    const household = policy?.household ?? configuration.defaultGroup;
    const route = await this.group(household);
    const current = await readEgressPolicy(this.config, name);
    if (JSON.stringify(current) !== JSON.stringify(policy) ||
        (await this._configuration()).digest !== configuration.digest) fail('account_egress_changed');
    return freeze({ ...route, revision: digest(JSON.stringify([route.revision, name, current])),
      profileId: profile.name, householdSource: policy ? 'explicit' : 'default' });
  }
  async setAccount(name, household) {
    if (!households.includes(household)) fail('invalid_household');
    const profile = await getProfile(this.config, name);
    const before = await this._configuration();
    if (!before.groups[household]) fail('household_not_configured');
    const route = await this.group(household, { fresh: true });
    if ((await this._configuration()).digest !== before.digest) fail('account_egress_changed');
    const policy = { version: 1, household };
    await atomicJson(path.join(profile.root, 'egress-policy.json'), policy);
    return freeze({ ...route, profileId: name, householdSource: 'explicit',
      revision: digest(JSON.stringify([route.revision, name, policy])) });
  }
}
