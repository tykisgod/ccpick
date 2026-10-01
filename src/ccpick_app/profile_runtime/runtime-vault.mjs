import path from 'node:path';
import { spawn } from 'node:child_process';
import { randomUUID } from 'node:crypto';
import { fileURLToPath } from 'node:url';
import { atomicJson, readJson, getProfile, listProfiles, createProfile, verifyIdentity,
  globalConfigFile, digest, fail } from './core.mjs';
import { houseJson } from './house-request.mjs';

const CLIENT_ID = '9d1c250a-e61b-44d9-88ed-5944d1962f5e'; // public-protocol-id
const tokenOf = c => c?.claudeAiOauth?.accessToken;
const credentialFields = ['accessToken', 'refreshToken', 'refreshTokenExpiresAt', 'expiresAt'];

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
        { stdio: ['pipe', 'pipe', 'ignore'], windowsHide: true });
      let output = '', done = false;
      const timer = setTimeout(() => { child.kill(); finish(new Error('credential_store_unavailable')); }, 15_000);
      function finish(error, result) {
        if (done) return; done = true; clearTimeout(timer);
        error ? reject(error) : resolve(result);
      }
      child.on('error', () => finish(new Error('credential_store_unavailable')));
      child.stdout.on('data', chunk => {
        output += chunk;
        if (output.length > 1024 * 1024) { child.kill(); finish(new Error('credential_store_unavailable')); }
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
  #config; #proxy; #store; #request; #locks = new Map(); #cache = new Map();
  constructor(config, proxy, { store = credentialStore(config), request = houseJson } = {}) {
    this.#config = config; this.#proxy = proxy; this.#store = store; this.#request = request;
  }
  #serial(name, action) {
    const pending = (this.#locks.get(name) ?? Promise.resolve()).then(action);
    this.#locks.set(name, pending.catch(() => {}));
    return pending;
  }
  async verifyGrant(credentials) {
    const accessToken = tokenOf(credentials);
    if (typeof accessToken !== 'string' || !accessToken) fail('login_required');
    const profile = await this.#request(this.#proxy, 'profile', { token: accessToken });
    const accountUuid = profile?.account?.uuid, organizationUuid = profile?.organization?.uuid;
    if (!accountUuid || !organizationUuid || typeof profile.account.email !== 'string') fail('auth_unverified');
    return { accountUuid, organizationUuid, email: profile.account.email, profile };
  }
  async #read(name) {
    const p = await getProfile(this.#config, name);
    const ids = await verifyIdentity(this.#config, p);
    if (!p.account) fail('login_required');
    if (ids.oauthAccount?.accountUuid !== p.account.uuid) fail('wrong_account');
    return { p, ids, credentials: await this.#store.read(p.configDirectory) };
  }
  loadAccount(name) {
    return this.#serial(name, async () => {
      let { p, ids, credentials } = await this.#read(name);
      let oauth = credentials.claudeAiOauth;
      if (!tokenOf(credentials)) fail('login_required');
      if (!Number.isFinite(oauth.expiresAt) || oauth.expiresAt <= Date.now() + 60_000) {
        if (!oauth.refreshToken) fail('login_required');
        const before = digest(JSON.stringify(credentials));
        const refreshed = await this.#request(this.#proxy, 'refresh', { body: {
          grant_type: 'refresh_token', refresh_token: oauth.refreshToken,
          client_id: oauth.clientId ?? CLIENT_ID, scope: (oauth.scopes ?? []).join(' '),
        } });
        if (typeof refreshed.access_token !== 'string' || !refreshed.access_token ||
            !Number.isFinite(refreshed.expires_in) || refreshed.expires_in <= 0) fail('auth_renewal_failed');
        const next = { ...credentials, claudeAiOauth: { ...oauth, accessToken: refreshed.access_token,
          refreshToken: refreshed.refresh_token ?? oauth.refreshToken,
          expiresAt: Date.now() + refreshed.expires_in * 1000,
          ...(typeof refreshed.scope === 'string' ? { scopes: refreshed.scope.split(' ') } : {}) } };
        if (refreshed.account?.uuid && refreshed.account.uuid !== p.account.uuid) fail('wrong_account');
        if (digest(JSON.stringify(await this.#store.read(p.configDirectory))) !== before) fail('credentials_changed');
        await this.#store.write(p.configDirectory, next);
        credentials = next; oauth = next.claudeAiOauth;
        const remote = await this.verifyGrant(next);
        if (remote.accountUuid !== p.account.uuid || remote.organizationUuid !== ids.oauthAccount.organizationUuid)
          fail('wrong_account');
        this.#cache.set(name, { fingerprint: digest(oauth.accessToken), until: Date.now() + 60_000 });
      }
      const cached = this.#cache.get(name);
      if (!cached || cached.fingerprint !== digest(oauth.accessToken) || cached.until <= Date.now()) {
        const remote = await this.verifyGrant(credentials);
        if (remote.accountUuid !== p.account.uuid || remote.organizationUuid !== ids.oauthAccount.organizationUuid)
          fail('wrong_account');
        this.#cache.set(name, { fingerprint: digest(oauth.accessToken), until: Date.now() + 60_000 });
      }
      return { name, accountUuid: p.account.uuid, ids, credentials, expiresAt: oauth.expiresAt };
    });
  }
  recoverGrant(input) {
    return this.#serial('enrollment', () => this.#recoverGrant(input));
  }
  async #recoverGrant({ credentials, receipt, persist }) {
    for (const p of await listProfiles(this.#config)) {
      const existing = await this.#store.read(p.configDirectory);
      if (existing.ccpickRuntimeGrant?.id !== receipt.id) continue;
      const ids = await verifyIdentity(this.#config, p);
      if (p.account && ids.oauthAccount?.accountUuid === p.account.uuid) return { name: p.name };
      credentials = existing; break;
    }
    const oauth = credentials?.claudeAiOauth;
    if (!Number.isFinite(oauth?.expiresAt) || oauth.expiresAt <= Date.now() + 60_000) {
      if (!oauth?.refreshToken) fail('login_required');
      const response = await this.#request(this.#proxy, 'refresh', { body: {
        grant_type: 'refresh_token', refresh_token: oauth.refreshToken,
        client_id: oauth.clientId ?? CLIENT_ID, scope: (oauth.scopes ?? []).join(' '),
      } });
      if (typeof response.access_token !== 'string' || !response.access_token ||
          !Number.isFinite(response.expires_in) || response.expires_in <= 0) fail('auth_renewal_failed');
      credentials = { claudeAiOauth: { ...oauth, accessToken: response.access_token,
        refreshToken: response.refresh_token ?? oauth.refreshToken, expiresAt: Date.now() + response.expires_in * 1000,
        ...(typeof response.scope === 'string' ? { scopes: response.scope.split(/\s+/).filter(Boolean) } : {}) } };
      await persist(credentials);
    }
    return this.#importGrant({ credentials, receipt, recovery: true });
  }
  importGrant(input) {
    return this.#serial('enrollment', () => this.#importGrant(input));
  }
  async #importGrant({ credentials, remote, receipt, recovery = false }) {
      const verified = await this.verifyGrant(credentials);
      if (remote && (verified.accountUuid !== remote.accountUuid ||
          (remote.organizationUuid && verified.organizationUuid !== remote.organizationUuid))) fail('wrong_account');
      const incomingReceipt = receipt ?? { id: randomUUID(), capturedAt: new Date().toISOString() };
      if (typeof incomingReceipt.id !== 'string' || !incomingReceipt.id ||
          !Number.isFinite(Date.parse(incomingReceipt.capturedAt))) fail('auth_unverified');
      const profiles = await listProfiles(this.#config);
      let p = profiles.find(profile => profile.account?.uuid === verified.accountUuid);
      if (!p) p = profiles.find(profile => !profile.account && profile.email?.toLowerCase() === verified.email.toLowerCase());
      if (!p) p = await createProfile(this.#config, { label: verified.email, email: verified.email });
      return this.#serial(p.name, async () => {
        p = await getProfile(this.#config, p.name);
        const ids = await verifyIdentity(this.#config, p);
        if (p.account && p.account.uuid !== verified.accountUuid) fail('wrong_account');
        const existing = await this.#store.read(p.configDirectory);
        const priorReceipt = existing.ccpickRuntimeGrant;
        const sameReceipt = priorReceipt?.id === incomingReceipt.id;
        const superseded = priorReceipt && Date.parse(priorReceipt.capturedAt) > Date.parse(incomingReceipt.capturedAt);
        if (superseded) return { name: p.name };
        const keepExisting = sameReceipt && p.account && (recovery || tokenOf(existing) !== tokenOf(credentials) ||
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
        this.#cache.delete(p.name);
        return { name: p.name };
      });
  }
}
