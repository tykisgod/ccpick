import path from 'node:path';
import fs from 'node:fs/promises';
import { constants } from 'node:fs';
import { spawn } from 'node:child_process';
import { randomUUID } from 'node:crypto';
import { fileURLToPath } from 'node:url';
import { atomicJson, readJson, getProfile, listProfiles, createProfile, verifyIdentity,
  globalConfigFile, profileDirectory, digest, fail, regular, validName, object } from './core.mjs';
import { houseJson, authRetryAt } from './house-request.mjs';

const CLIENT_ID = '9d1c250a-e61b-44d9-88ed-5944d1962f5e'; // public-protocol-id
const tokenOf = c => c?.claudeAiOauth?.accessToken;
const credentialFields = ['accessToken', 'refreshToken', 'refreshTokenExpiresAt', 'expiresAt'];
const bindingQueues = new Map(), bindingRevisions = new Map();
const BINDING_LIMIT = 4096, BINDING_RETENTION_MS = 24 * 60 * 60 * 1000;
const sameKeys = (value, keys) => object(value) && Object.keys(value).length === keys.length &&
  keys.every(key => Object.hasOwn(value, key));
const stamp = info => [info.dev, info.ino, info.size, info.mtimeMs, info.ctimeMs].join(':');

async function bearerBindings(config, action) {
  const root = path.resolve(config.dataRoot, '..', 'active-runtime');
  const file = path.join(root, 'bearer-bindings.json');
  const previous = bindingQueues.get(file) ?? Promise.resolve();
  const pending = previous.then(async () => {
    await regular(path.dirname(root), true);
    await fs.mkdir(root, { mode: 0o700 }).catch(error => { if (error.code !== 'EEXIST') throw error; });
    await regular(root, true);
    const read = async () => {
      let info;
      try { info = await regular(file); }
      catch (error) { if (error.code === 'ENOENT') return { value: { version: 1, bindings: {} }, revision: null }; throw error; }
      if (info.nlink !== 1 || info.size <= 0 || info.size > 1024 * 1024) fail('runtime_bearer_index_invalid');
      const handle = await fs.open(file, constants.O_RDONLY | (constants.O_NOFOLLOW ?? 0));
      let bytes;
      try {
        const opened = await handle.stat(); bytes = await handle.readFile();
        if (stamp(info) !== stamp(opened) || stamp(opened) !== stamp(await handle.stat()) ||
            stamp(info) !== stamp(await regular(file))) fail('runtime_bearer_index_changed');
      } finally { await handle.close(); }
      let value;
      try { value = JSON.parse(bytes.toString('utf8')); } catch { fail('runtime_bearer_index_invalid'); }
      if (!sameKeys(value, ['version', 'bindings']) || value.version !== 1 || !object(value.bindings) ||
          Object.keys(value.bindings).length > BINDING_LIMIT) fail('runtime_bearer_index_invalid');
      for (const [hash, binding] of Object.entries(value.bindings)) {
        if (!/^[a-f0-9]{64}$/.test(hash) || !sameKeys(binding, ['profileId', 'accountHash', 'expiresAt', 'retainUntil']) ||
            typeof binding.profileId !== 'string' || !validName(binding.profileId) ||
            typeof binding.accountHash !== 'string' || !/^[a-f0-9]{64}$/.test(binding.accountHash) ||
            !Number.isSafeInteger(binding.expiresAt) || binding.expiresAt <= 0 ||
            !Number.isSafeInteger(binding.retainUntil) || binding.retainUntil <= 0 ||
            binding.retainUntil > binding.expiresAt) fail('runtime_bearer_index_invalid');
      }
      return { value, revision: digest(bytes) };
    };
    const before = await read();
    if (bindingRevisions.has(file) && bindingRevisions.get(file) !== before.revision)
      fail('runtime_bearer_index_changed');
    bindingRevisions.set(file, before.revision);
    const value = structuredClone(before.value);
    for (const [hash, binding] of Object.entries(value.bindings))
      if (binding.expiresAt <= Date.now() || binding.retainUntil <= Date.now()) delete value.bindings[hash];
    const result = await action(value.bindings);
    if (Object.keys(value.bindings).length > BINDING_LIMIT) fail('runtime_bearer_index_invalid');
    if (JSON.stringify(value) !== JSON.stringify(before.value)) {
      const temporary = `${file}.${randomUUID()}.tmp`, bytes = JSON.stringify(value, null, 2) + '\n';
      try {
        await fs.writeFile(temporary, bytes, { flag: 'wx', mode: 0o600 });
        for (let attempt = 0; ; attempt++) {
          if ((await read()).revision !== before.revision) fail('runtime_bearer_index_changed');
          try { await fs.rename(temporary, file); break; }
          catch (error) {
            if (process.platform !== 'win32' || !['EPERM', 'EACCES', 'EBUSY'].includes(error.code) || attempt >= 7) throw error;
            await new Promise(resolve => setTimeout(resolve, 25 * (attempt + 1)));
          }
        }
        bindingRevisions.set(file, digest(bytes));
      } finally { await fs.unlink(temporary).catch(error => { if (error.code !== 'ENOENT') throw error; }); }
    }
    return result;
  });
  const tail = pending.catch(() => {}); bindingQueues.set(file, tail);
  try { return await pending; }
  finally { if (bindingQueues.get(file) === tail) bindingQueues.delete(file); }
}

export function credentialsFromOAuthResponse(response, capturedAt) {
  const exchangedAt = Date.parse(capturedAt);
  if (typeof response?.access_token !== 'string' || !response.access_token ||
      !Number.isFinite(response.expires_in) || response.expires_in <= 0 || !Number.isFinite(exchangedAt))
    fail('auth_unverified');
  return { claudeAiOauth: { accessToken: response.access_token,
    expiresAt: exchangedAt + response.expires_in * 1000,
    ...(typeof response.refresh_token === 'string' && response.refresh_token ? { refreshToken: response.refresh_token } : {}),
    ...(typeof response.scope === 'string' ? { scopes: response.scope.split(/\s+/).filter(Boolean) } : {}) } };
}

export function credentialStore(config) {
  async function mac(directory, operation, value) {
    return new Promise((resolve, reject) => {
      const child = spawn(config.python, [fileURLToPath(new URL('./runtime-credentials.py', import.meta.url)),
        path.join(config.dataRoot, '..', 'install.json'), directory, operation],
        { stdio: ['pipe', 'pipe', 'ignore'], windowsHide: true, detached: config.platform === 'darwin' });
      let output = '', done = false;
      const stopAdapter = () => {
        if (config.platform === 'darwin' && child.pid) {
          try { process.kill(-child.pid, 'SIGTERM'); } catch (error) { if (error.code !== 'ESRCH') child.kill(); }
        } else child.kill();
      };
      const timer = setTimeout(() => { stopAdapter(); finish(new Error('credential_store_unavailable')); }, 15_000);
      function finish(error, result) {
        if (done) return; done = true; clearTimeout(timer);
        error ? reject(error) : resolve(result);
      }
      child.on('error', () => finish(new Error('credential_store_unavailable')));
      child.stdout.on('data', chunk => {
        output += chunk;
        if (output.length > 1024 * 1024) { stopAdapter(); finish(new Error('credential_store_unavailable')); }
      });
      child.on('close', code => {
        if (code) return finish(new Error('credential_store_unavailable'));
        try { finish(null, JSON.parse(output)); } catch { finish(new Error('credential_store_unavailable')); }
      });
      child.stdin.on('error', () => {});
      child.stdin.end(value === undefined ? undefined : JSON.stringify(value));
    });
  }
  return {
    read: directory => config.platform === 'darwin' ? mac(directory, 'read') :
      readJson(path.join(directory, '.credentials.json'), true).then(value => value ?? {}),
    write: (directory, value) => config.platform === 'darwin' ? mac(directory, 'write', value) :
      atomicJson(path.join(directory, '.credentials.json'), value),
  };
}

export class RuntimeVault {
  #config; #proxy; #store; #request; #egress; #locks = new Map(); #cache = new Map();
  #backoff = new Map(); #grantProfiles = new Map();
  #preserveRefresh; #acknowledgeRefresh; #pendingRefresh;
  constructor(config, proxy, { store = credentialStore(config), request = houseJson, egress,
    preserveRefresh, acknowledgeRefresh, pendingRefresh = () => false } = {}) {
    this.#config = config; this.#proxy = proxy; this.#store = store; this.#request = request; this.#egress = egress;
    this.#preserveRefresh = preserveRefresh; this.#acknowledgeRefresh = acknowledgeRefresh;
    this.#pendingRefresh = pendingRefresh;
  }
  #serial(name, action) {
    const pending = (this.#locks.get(name) ?? Promise.resolve()).then(action);
    this.#locks.set(name, pending.catch(() => {}));
    return pending;
  }
  #backoffFile(profile) {
    return profile ? path.join(profileDirectory(this.#config, profile.name), 'auth-backoff.json') :
      path.join(this.#config.dataRoot, '..', 'auth-enrollment-backoff.json');
  }
  async #retryAt(profile) {
    const file = this.#backoffFile(profile);
    const saved = await readJson(file, true);
    if (saved && (saved.version !== 1 || !Number.isFinite(saved.retryAt) || saved.retryAt < 0))
      fail('auth_unverified');
    const usage = profile ? await readJson(path.join(profileDirectory(this.#config, profile.name), 'usage.json'), true) : null;
    const previous = usage?.error === 'http-429' && Number.isFinite(usage.nextPollAt) &&
      Number.isFinite(usage.nextPollAt * 1000) ? usage.nextPollAt * 1000 : 0;
    return Math.max(saved?.retryAt ?? 0, this.#backoff.get(file) ?? 0, previous);
  }
  async #requireReady(profile) {
    const retryAt = await this.#retryAt(profile);
    if (retryAt > Date.now()) throw Object.assign(new Error('auth_rate_limited'), { status: 429, retryAt });
  }
  async #accountRequest(profile, kind, options, { proxy, household, egress } = {}) {
    return this.#serial(`auth:${profile?.name ?? 'enrollment'}`, async () => {
      await this.#requireReady(profile);
      const route = egress ?? (proxy === undefined && this.#egress && (profile
        ? await this.#egress.account(profile.name)
        : await this.#egress.group(household ?? await this.#egress.defaultGroup())));
      if (household && route && route.household !== household) fail('wrong_account');
      if (route && (typeof route.proxy !== 'string' || !route.proxy)) fail('account_egress_invalid');
      try { return await this.#request(proxy ?? route?.proxy ?? this.#proxy, kind, options); }
      catch (error) {
        if (error?.status !== 429 && error?.message !== 'auth_rate_limited') throw error;
        const retryAt = Math.max(authRetryAt(undefined),
          Number.isFinite(error.retryAt) ? error.retryAt : 0, await this.#retryAt(profile));
        const file = this.#backoffFile(profile);
        this.#backoff.set(file, retryAt);
        await atomicJson(file, { version: 1, retryAt });
        throw Object.assign(new Error('auth_rate_limited'), { status: 429, retryAt });
      }
    });
  }
  async #profileForGrant(credentials) {
    const token = tokenOf(credentials), known = typeof token === 'string' && this.#grantProfiles.get(digest(token));
    if (known) return getProfile(this.#config, known);
    const binding = typeof token === 'string' && token ? await this.#binding(token) : undefined;
    if (binding) {
      const profile = await getProfile(this.#config, binding.profileId);
      if (!profile.account || digest(profile.account.uuid) !== binding.accountHash) fail('auth_unverified');
      return profile;
    }
    const refresh = credentials?.claudeAiOauth?.refreshToken;
    for (const profile of await listProfiles(this.#config)) {
      const existing = await this.#store.read(profile.configDirectory);
      if ((token && token === tokenOf(existing)) ||
          (refresh && refresh === existing?.claudeAiOauth?.refreshToken)) return profile;
    }
    return undefined;
  }
  exchangeLogin(options, { proxy } = {}) {
    return this.#accountRequest(undefined, 'login', options, { proxy });
  }
  async verifyGrant(credentials, options = {}) {
    const accessToken = tokenOf(credentials);
    if (typeof accessToken !== 'string' || !accessToken) fail('login_required');
    const profileHint = typeof options?.name === 'string' ? options : options?.profile;
    const local = profileHint ?? await this.#profileForGrant(credentials);
    const profile = await this.#accountRequest(local, 'profile', { token: accessToken }, options);
    const accountUuid = profile?.account?.uuid, organizationUuid = profile?.organization?.uuid;
    if (!accountUuid || !organizationUuid || typeof profile.account.email !== 'string') fail('auth_unverified');
    const registered = (await listProfiles(this.#config)).find(p => p.account?.uuid === accountUuid);
    if (registered) {
      this.#grantProfiles.set(digest(accessToken), registered.name);
      await this.#requireReady(registered);
    }
    return { accountUuid, organizationUuid, email: profile.account.email, profile };
  }
  async #read(name) {
    const p = await getProfile(this.#config, name);
    const ids = await verifyIdentity(this.#config, p);
    if (!p.account) fail('login_required');
    if (ids.oauthAccount?.accountUuid !== p.account.uuid) fail('wrong_account');
    return { p, ids, credentials: await this.#store.read(p.configDirectory) };
  }
  #binding(token) {
    return bearerBindings(this.#config, bindings => bindings[digest(token)]);
  }
  #remember(credentials, profile) {
    const oauth = credentials?.claudeAiOauth;
    if (typeof oauth?.accessToken !== 'string' || !oauth.accessToken || !Number.isSafeInteger(oauth.expiresAt) ||
        oauth.expiresAt <= Date.now()) return Promise.resolve();
    return bearerBindings(this.#config, bindings => {
      const hash = digest(oauth.accessToken), accountHash = digest(profile.account.uuid), existing = bindings[hash];
      if (existing && (existing.profileId !== profile.name || existing.accountHash !== accountHash))
        fail('runtime_bearer_binding_conflict');
      bindings[hash] = { profileId: profile.name, accountHash, expiresAt: oauth.expiresAt,
        retainUntil: Math.min(oauth.expiresAt, existing?.retainUntil ?? Date.now() + BINDING_RETENTION_MS) };
    });
  }
  loadAccount(name) {
    return this.#serial(name, async () => {
      if (await this.#pendingRefresh(name)) fail('auth_renewal_failed');
      let { p, ids, credentials } = await this.#read(name);
      const egress = this.#egress && await this.#egress.account(name);
      await this.#requireReady(p);
      let oauth = credentials.claudeAiOauth;
      if (!tokenOf(credentials)) fail('login_required');
      if (!Number.isFinite(oauth.expiresAt) || oauth.expiresAt <= Date.now() + 60_000) {
        if (!oauth.refreshToken) fail('login_required');
        if (oauth.expiresAt > Date.now() && !await this.#binding(oauth.accessToken)) {
          const prior = await this.verifyGrant(credentials, { profile: p, egress });
          if (prior.accountUuid !== p.account.uuid || prior.organizationUuid !== ids.oauthAccount.organizationUuid)
            fail('wrong_account');
          await this.#remember(credentials, p);
        }
        const before = digest(JSON.stringify(credentials));
        const refreshed = await this.#accountRequest(p, 'refresh', { body: {
          grant_type: 'refresh_token', refresh_token: oauth.refreshToken,
          client_id: oauth.clientId ?? CLIENT_ID, scope: (oauth.scopes ?? []).join(' '),
        } }, { egress });
        if (typeof refreshed.access_token !== 'string' || !refreshed.access_token ||
            !Number.isFinite(refreshed.expires_in) || refreshed.expires_in <= 0) fail('auth_renewal_failed');
        const next = { ...credentials, claudeAiOauth: { ...oauth, accessToken: refreshed.access_token,
          refreshToken: refreshed.refresh_token ?? oauth.refreshToken,
          expiresAt: Date.now() + refreshed.expires_in * 1000,
          ...(typeof refreshed.scope === 'string' ? { scopes: refreshed.scope.split(' ') } : {}) } };
        const receipt = await this.#preserveRefresh?.({ name, accountUuid: p.account.uuid,
          organizationUuid: ids.oauthAccount.organizationUuid, ...(egress ? { household: egress.household } : {}),
          credentials: { claudeAiOauth: next.claudeAiOauth } });
        if (receipt) next.ccpickRuntimeGrant = { id: receipt.id, capturedAt: receipt.capturedAt };
        if (refreshed.account?.uuid && refreshed.account.uuid !== p.account.uuid) fail('wrong_account');
        if (digest(JSON.stringify(await this.#store.read(p.configDirectory))) !== before) fail('credentials_changed');
        await this.#store.write(p.configDirectory, next);
        credentials = next; oauth = next.claudeAiOauth;
        const remote = await this.verifyGrant(next, { profile: p, egress });
        if (remote.accountUuid !== p.account.uuid || remote.organizationUuid !== ids.oauthAccount.organizationUuid)
          fail('wrong_account');
        if (receipt) await this.#acknowledgeRefresh?.({ receipt, name });
        this.#cache.set(name, { fingerprint: digest(oauth.accessToken), revision: egress?.revision, until: Date.now() + 60_000 });
      }
      const cached = this.#cache.get(name);
      if (!cached || cached.fingerprint !== digest(oauth.accessToken) || cached.revision !== egress?.revision || cached.until <= Date.now()) {
        const remote = await this.verifyGrant(credentials, { profile: p, egress });
        if (remote.accountUuid !== p.account.uuid || remote.organizationUuid !== ids.oauthAccount.organizationUuid)
          fail('wrong_account');
        this.#cache.set(name, { fingerprint: digest(oauth.accessToken), revision: egress?.revision, until: Date.now() + 60_000 });
      }
      if (egress && (await this.#egress.account(name)).revision !== egress.revision) fail('selection_changed');
      await this.#remember(credentials, p);
      return { name, accountUuid: p.account.uuid, ids, credentials, expiresAt: oauth.expiresAt, ...(egress ? { egress } : {}) };
    });
  }
  async bearerEgress(token) {
    if (typeof token !== 'string' || !token) fail('auth_unverified');
    const fingerprint = digest(token);
    let binding = await this.#binding(token), name = binding?.profileId;
    if (!name) for (const p of await listProfiles(this.#config)) {
      if (!p.account) continue;
      const credentials = await this.#store.read(p.configDirectory);
      const current = tokenOf(credentials);
      if (current && digest(current) === fingerprint) {
        await this.loadAccount(p.name);
        binding = await this.#binding(token); name = binding?.profileId;
        break;
      }
    }
    if (!name) fail('auth_unverified');
    const profile = await getProfile(this.#config, name);
    if (!profile.account || digest(profile.account.uuid) !== binding.accountHash) fail('auth_unverified');
    return this.#egress ? this.#egress.account(name) : { proxy: this.#proxy };
  }
  recoverGrant(input) {
    return this.#serial('enrollment', () => this.#recoverGrant(input));
  }
  async #refreshBinding(receipt) {
    if (receipt?.reason !== 'oauth_refresh_completed') return undefined;
    const p = await getProfile(this.#config, receipt.profileId);
    const ids = await verifyIdentity(this.#config, p);
    if (!p.account || p.account.uuid !== receipt.accountUuid ||
        ids.oauthAccount?.accountUuid !== receipt.accountUuid ||
        ids.oauthAccount?.organizationUuid !== receipt.organizationUuid) fail('wrong_account');
    await this.#requireReady(p);
    return p;
  }
  async #recoverGrant({ credentials, receipt, persist, household = receipt?.household }) {
    const refreshProfile = await this.#refreshBinding(receipt);
    let renewedRecovery = false;
    for (const p of await listProfiles(this.#config)) {
      const existing = await this.#store.read(p.configDirectory);
      if (existing.ccpickRuntimeGrant?.id !== receipt.id) continue;
      const ids = await verifyIdentity(this.#config, p);
      if (p.account && ids.oauthAccount?.accountUuid === p.account.uuid) {
        if (!refreshProfile) return { name: p.name };
        if (p.name !== refreshProfile.name) fail('wrong_account');
        if (existing.claudeAiOauth?.expiresAt > Date.now() + 60_000) {
          const remote = await this.verifyGrant(existing, { profile: p, household });
          if (remote.accountUuid !== receipt.accountUuid || remote.organizationUuid !== receipt.organizationUuid)
            fail('wrong_account');
          return { name: p.name };
        }
        if (tokenOf(credentials) && (tokenOf(credentials) !== tokenOf(existing) ||
            credentials.claudeAiOauth?.refreshToken !== existing.claudeAiOauth?.refreshToken)) {
          renewedRecovery = true;
          break;
        }
      }
      credentials = existing; break;
    }
    const oauth = credentials?.claudeAiOauth;
    const knownProfile = refreshProfile ?? await this.#profileForGrant(credentials);
    if (!Number.isFinite(oauth?.expiresAt) || oauth.expiresAt <= Date.now() + 60_000) {
      if (!oauth?.refreshToken) fail('login_required');
      const response = await this.#accountRequest(knownProfile, 'refresh', { body: {
        grant_type: 'refresh_token', refresh_token: oauth.refreshToken,
        client_id: oauth.clientId ?? CLIENT_ID, scope: (oauth.scopes ?? []).join(' '),
      } }, { household });
      if (typeof response.access_token !== 'string' || !response.access_token ||
          !Number.isFinite(response.expires_in) || response.expires_in <= 0) fail('auth_renewal_failed');
      credentials = { claudeAiOauth: { ...oauth, accessToken: response.access_token,
        refreshToken: response.refresh_token ?? oauth.refreshToken, expiresAt: Date.now() + response.expires_in * 1000,
        ...(typeof response.scope === 'string' ? { scopes: response.scope.split(/\s+/).filter(Boolean) } : {}) } };
      await persist(credentials);
      renewedRecovery = true;
      if (knownProfile) this.#grantProfiles.set(digest(tokenOf(credentials)), knownProfile.name);
    }
    return this.#importGrant({ credentials, receipt, recovery: true, renewedRecovery, household });
  }
  importGrant(input) {
    return this.#serial('enrollment', () => this.#importGrant(input));
  }
  async #importGrant({ credentials, remote, receipt, recovery = false, renewedRecovery = false, household = receipt?.household }) {
      const refreshProfile = await this.#refreshBinding(receipt);
      const verified = await this.verifyGrant(credentials, { profile: refreshProfile, household });
      if (refreshProfile && (verified.accountUuid !== receipt.accountUuid ||
          verified.organizationUuid !== receipt.organizationUuid)) fail('wrong_account');
      if (remote && (verified.accountUuid !== remote.accountUuid ||
          (remote.organizationUuid && verified.organizationUuid !== remote.organizationUuid))) fail('wrong_account');
      const incomingReceipt = receipt ?? { id: randomUUID(), capturedAt: new Date().toISOString() };
      if (typeof incomingReceipt.id !== 'string' || !incomingReceipt.id ||
          !Number.isFinite(Date.parse(incomingReceipt.capturedAt))) fail('auth_unverified');
      const profiles = await listProfiles(this.#config);
      let p = profiles.find(profile => profile.account?.uuid === verified.accountUuid);
      if (!p) p = profiles.find(profile => !profile.account && profile.email?.toLowerCase() === verified.email.toLowerCase());
      if (!p) p = await createProfile(this.#config, { label: verified.email, email: verified.email, ...(household ? { household } : {}) });
      if (household && this.#egress && (await this.#egress.account(p.name)).household !== household) fail('wrong_account');
      return this.#serial(p.name, async () => {
        p = await getProfile(this.#config, p.name);
        const ids = await verifyIdentity(this.#config, p);
        if (p.account && p.account.uuid !== verified.accountUuid) fail('wrong_account');
        const existing = await this.#store.read(p.configDirectory);
        const priorReceipt = existing.ccpickRuntimeGrant;
        const sameReceipt = priorReceipt?.id === incomingReceipt.id;
        const superseded = priorReceipt && Date.parse(priorReceipt.capturedAt) > Date.parse(incomingReceipt.capturedAt);
        if (superseded) return { name: p.name };
        const keepExisting = sameReceipt && p.account && !renewedRecovery && (recovery || tokenOf(existing) !== tokenOf(credentials) ||
          existing.claudeAiOauth?.refreshToken !== credentials.claudeAiOauth?.refreshToken);
        const optional = { ...(existing.claudeAiOauth ?? {}) };
        for (const name of credentialFields) delete optional[name];
        const incomingMetadata = { ...(credentials.claudeAiOauth ?? {}) };
        for (const name of credentialFields) delete incomingMetadata[name];
        const next = keepExisting ? { ...existing,
          claudeAiOauth: { ...existing.claudeAiOauth, ...incomingMetadata } } : { ...existing,
          claudeAiOauth: { ...optional, ...structuredClone(credentials.claudeAiOauth) },
          ccpickRuntimeGrant: { id: incomingReceipt.id, capturedAt: incomingReceipt.capturedAt } };
        const account = verified.profile.account, org = verified.profile.organization;
        const oauthAccount = { accountUuid: account.uuid, emailAddress: account.email,
          organizationUuid: org.uuid, ...(account.display_name ? { displayName: account.display_name } : {}),
          ...(org.organization_type ? { organizationRole: org.organization_type } : {}) };
        if (JSON.stringify(next) !== JSON.stringify(existing)) await this.#store.write(p.configDirectory, next);
        await atomicJson(globalConfigFile(p), { ...ids, oauthAccount, hasCompletedOnboarding: true });
        const { root, configDirectory, ...manifest } = p;
        await atomicJson(path.join(root, 'profile.json'), { ...manifest,
          account: { uuid: account.uuid, email: account.email } });
        await this.#remember(credentials, { ...p, account: { uuid: account.uuid } });
        this.#cache.delete(p.name);
        return { name: p.name };
      });
  }
}
