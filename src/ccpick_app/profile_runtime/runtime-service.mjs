import fs from 'node:fs/promises';
import path from 'node:path';
import http from 'node:http';
import { randomBytes, randomUUID, timingSafeEqual } from 'node:crypto';
import { fileURLToPath } from 'node:url';
import { ActiveRuntime, checkedSelectionGuard, checkedSelectionReceipt } from './active-runtime.mjs';
import { RuntimeVault, credentialStore, credentialsFromOAuthResponse } from './runtime-vault.mjs';
import { createMessageBroker } from './message-broker.mjs';
import { createNativeApiGuard, isAccountTunnelHost } from './native-api-guard.mjs';
import { runtimeCertificate } from './runtime-certificate.mjs';
import { loadInstall, readJson, atomicJson, getProfile, globalConfigFile, regular, validName, digest, fail } from './core.mjs';
import { registryState, selectProfile, resolveProfile } from './registry.mjs';
import { prepareNetwork } from './network.mjs';
import { houseJson } from './house-request.mjs';
import { prepareHistory } from './history.mjs';
import { prepareInputHistory } from './input-history.mjs';
import { syncResources } from './resources.mjs';
import { syncRuntimeSettings } from './runtime-settings.mjs';
import { AccountEgressRouter } from './account-egress.mjs';
import { runtimeProcessIdentity } from './runtime-supervisor.mjs';

export const runtimeRoot = config => path.join(config.dataRoot, '..', 'active-runtime');
export const runtimeDirectory = config => path.join(runtimeRoot(config), 'claude');
const allowedReasons = new Set(['login_required', 'wrong_account', 'auth_unverified', 'auth_forbidden',
  'auth_rate_limited', 'network_unavailable', 'selection_changed', 'runtime_unavailable',
  'login_in_progress', 'unclaimed_login_credentials', 'identity_changed', 'credentials_changed',
  'household_not_configured', 'account_egress_invalid', 'account_egress_changed', 'invalid_household',
  'network_not_ready', 'house_service_not_ready', 'egress_client_upgrade_required', 'auth_renewal_failed']);
const publicReason = error => allowedReasons.has(error?.message) ? error.message : 'runtime_unavailable';
const reauthorizationReasons = new Set(['login_required', 'auth_forbidden', 'auth_unverified',
  'auth_rate_limited', 'network_unavailable', 'auth_renewal_failed', 'network_not_ready', 'house_service_not_ready']);
function processExists(pid) {
  if (!Number.isSafeInteger(pid) || pid < 1) return null;
  try { process.kill(pid, 0); return true; }
  catch (error) { return error.code === 'ESRCH' ? false : error.code === 'EPERM' ? true : null; }
}
async function assertLegacyScopesStopped(root, ports, alive) {
  const directory = path.join(root, 'leases');
  const entries = await fs.readdir(directory, { withFileTypes: true }).catch(error => {
    if (error.code === 'ENOENT') return []; throw error;
  });
  for (const entry of entries) {
    if (!entry.isFile() || !entry.name.endsWith('.json')) continue;
    const lease = await readJson(path.join(directory, entry.name));
    if (!lease || lease.scope === undefined || lease.scope === 'default') continue;
    if (!validName(lease.scope)) fail('egress_client_upgrade_required');
    if (lease.egressVersion === 1 && lease.proxyPort === ports[lease.scope]) continue;
    const pids = [lease.childPid, ...(lease.status === 'running' ? [lease.wrapperPid] : [])]
      .filter(pid => Number.isSafeInteger(pid) && pid > 0);
    if (!pids.length || pids.some(pid => alive(pid) !== false)) fail('egress_client_upgrade_required');
  }
}

export async function serveRuntime(config, { network = prepareNetwork, store: suppliedStore,
  requestAccount = houseJson, certificateFactory = runtimeCertificate, brokerTransport, apiHandler,
  recoveryDelayMs = 30_000, recoveryRetryMs = 60_000, isProcessAlive = processExists,
  getProcessIdentity = runtimeProcessIdentity } = {}) {
  if (config.seamlessAccounts !== true) fail('seamless_runtime_disabled');
  const root = runtimeRoot(config), directory = runtimeDirectory(config);
  await fs.mkdir(root, { recursive: true, mode: 0o700 });
  await regular(root, true);
  const lockFile = path.join(root, 'service.lock');
  const instanceId = randomUUID();
  const captured = await getProcessIdentity(process.pid);
  if (!captured || captured.pid !== process.pid || typeof captured.createdAt !== 'string' || !captured.createdAt ||
      typeof captured.executable !== 'string' || !path.isAbsolute(captured.executable)) fail('runtime_process_identity_unavailable');
  const processIdentity = { pid: captured.pid, createdAt: captured.createdAt, executable: captured.executable,
    script: typeof captured.script === 'string' && path.isAbsolute(captured.script) ? captured.script : null };
  const lock = await fs.open(lockFile, 'wx', 0o600);
  try { await lock.writeFile(JSON.stringify({ pid: process.pid, instanceId, processIdentity })); }
  catch (error) {
    await lock.close(); await fs.unlink(lockFile).catch(() => {}); throw error;
  }
  await lock.close();
  let guard, broker, control, interval, closing = false, cleanupTask;
  const scopeGuards = new Map();
  const signalHandlers = [];
  const operations = new Set();
  const track = promise => {
    operations.add(promise);
    promise.then(() => operations.delete(promise), () => operations.delete(promise));
    return promise;
  };
  const store = suppliedStore ?? credentialStore(config);
  const cleanup = () => cleanupTask ??= (async () => {
    closing = true;
    for (const [signal, handler] of signalHandlers) process.removeListener(signal, handler);
    clearInterval(interval);
    await guard?.close(); await broker?.close();
    await Promise.allSettled([...scopeGuards.values()].map(item => item.then(value => value.close())));
    control?.closeAllConnections(); control?.close();
    while (operations.size) await Promise.allSettled([...operations]);
    if ((await readJson(lockFile, true))?.instanceId === instanceId)
      await fs.unlink(lockFile).catch(error => { if (error.code !== 'ENOENT') throw error; });
  })();
  try {
    const previousService = await readJson(path.join(root, 'service.json'), true);
    if (previousService && (!['proxyPort', 'controlPort'].every(name => Number.isInteger(previousService[name]) &&
        previousService[name] > 0 && previousService[name] <= 65535) || previousService.proxyPort === previousService.controlPort))
      fail('runtime_unavailable');
    const router = new AccountEgressRouter(config, { network });
    const householdOptions = Object.freeze(await router.configuredGroups());
    const multiEgress = householdOptions.length > 1;
    const checked = await network(config, { checkOnly: true, requireBrowser: false,
      ...(multiEgress ? { requireHouseReady: false } : {}) });
    if (!multiEgress) router.proxy = checked.proxy;
    const certificate = await certificateFactory(root);
    if (previousService && (previousService.certFile !== certificate.certFile ||
        previousService.certificateFingerprint !== certificate.fingerprint)) fail('runtime_unavailable');
    const keyPath = path.join(root, 'channel-key.json');
    let key = await readJson(keyPath, true);
    if (previousService && !key) fail('runtime_key_invalid');
    if (!key) { key = { value: randomBytes(32).toString('hex') }; await atomicJson(keyPath, key); }
    if (!/^[a-f0-9]{64}$/.test(key.value)) fail('runtime_key_invalid');
    if (previousService?.channelKeyFingerprint && previousService.channelKeyFingerprint !== digest(key.value))
      fail('runtime_key_invalid');
    const supportedHousehold = value => { if (!householdOptions.includes(value)) fail('egress_client_upgrade_required'); return value; };
    const egress = {
      defaultGroup: async () => supportedHousehold(await router.defaultGroup()),
      group: (household, options) => router.group(supportedHousehold(household), options),
      account: async name => {
        const profile = await getProfile(config, name);
        supportedHousehold(profile.household ?? await router.defaultGroup());
        const route = await router.account(name); supportedHousehold(route.household); return route;
      },
      setAccount: (name, household) => router.setAccount(name, supportedHousehold(household)),
    };
    const portsFile = path.join(root, 'scope-proxy-ports.json');
    const portState = await readJson(portsFile, true) ?? { version: 1, ports: {} };
    if (portState.version !== 1 || !portState.ports || typeof portState.ports !== 'object' || Array.isArray(portState.ports) ||
        Object.entries(portState.ports).some(([scope, port]) => scope === 'default' || !validName(scope) || !Number.isInteger(port) || port < 1 || port > 65535 ||
          port === previousService?.proxyPort || port === previousService?.controlPort) ||
        new Set(Object.values(portState.ports)).size !== Object.values(portState.ports).length) fail('runtime_unavailable');
    const scopedEgress = multiEgress || Object.keys(portState.ports).length > 0;
    if (scopedEgress && previousService)
      await assertLegacyScopesStopped(root, portState.ports, isProcessAlive);
    let guardTail = Promise.resolve(), makeGuard;
    let vault;
    const scopes = new Map();
    const journals = new Map();
    const receipts = new Map();
    let lastCapturedAt = 0;
    const preserveGrant = async (scopeRoot, credentials, metadata) => {
      const id = randomUUID();
      const directory = path.join(scopeRoot, 'pending-logins', id);
      lastCapturedAt = Math.max(Date.now(), lastCapturedAt + 1);
      const token = credentials?.oauthResponse?.access_token ?? credentials?.recoveredCredentials?.claudeAiOauth?.accessToken;
      const record = { ...metadata, id, capturedAt: new Date(lastCapturedAt).toISOString(),
        ...(typeof token === 'string' && token ? { tokenFingerprint: digest(token) } : {}) };
      const journal = { directory, record, credentials, nextAttemptAt: Date.now() + recoveryDelayMs };
      journals.set(id, journal);
      rememberReceipt(journal);
      await fs.mkdir(directory, { mode: 0o700, recursive: true });
      await atomicJson(path.join(directory, 'record.json'), record);
      await store.write(directory, credentials);
      return directory;
    };
    const acknowledge = async (journal, profileId) => {
      const profile = await getProfile(config, profileId);
      journal.record = { ...journal.record, acknowledgedAt: new Date().toISOString(), profileId,
        accountUuid: profile.account?.uuid };
      await atomicJson(path.join(journal.directory, 'record.json'), journal.record);
      await store.write(journal.directory, {});
      delete journal.credentials;
      journals.delete(journal.record.id);
    };
    const loadJournals = async scopeRoot => {
      const parent = path.join(scopeRoot, 'pending-logins');
      const entries = await fs.readdir(parent, { withFileTypes: true }).catch(error => {
        if (error.code === 'ENOENT') return []; throw error;
      });
      for (const entry of entries) {
        if (!entry.isDirectory() || !/^[a-f0-9-]{36}$/.test(entry.name)) continue;
        const directory = path.join(parent, entry.name); await regular(directory, true);
        const record = await readJson(path.join(directory, 'record.json'), true);
        if (!record || !Number.isFinite(Date.parse(record.capturedAt))) continue;
        record.id ??= entry.name;
        lastCapturedAt = Math.max(lastCapturedAt, Date.parse(record.capturedAt));
        if (!['oauth_exchange_completed', 'oauth_refresh_completed'].includes(record.reason)) continue;
        const journal = { directory, record, credentials: await store.read(directory), nextAttemptAt: 0 };
        const token = journal.credentials?.oauthResponse?.access_token ?? journal.credentials?.recoveredCredentials?.claudeAiOauth?.accessToken;
        if (!record.tokenFingerprint && typeof token === 'string') record.tokenFingerprint = digest(token);
        rememberReceipt(journal);
        if (record.acknowledgedAt) { await store.write(directory, {}); delete journal.credentials; continue; }
        journals.set(record.id, journal);
      }
    };
    const journalFor = credentials => {
      const token = credentials?.claudeAiOauth?.accessToken;
      return typeof token === 'string' && token ? receipts.get(digest(token)) : undefined;
    };
    function rememberReceipt(journal) {
      const fingerprint = journal.record.tokenFingerprint;
      if (!fingerprint) return;
      const previous = receipts.get(fingerprint);
      if (!previous || Date.parse(previous.record.capturedAt) <= Date.parse(journal.record.capturedAt))
        receipts.set(fingerprint, journal);
    }
    const currentGrant = async credentials => {
      const journal = journalFor(credentials);
      if (!journal?.record.acknowledgedAt) return credentials;
      const account = await vault.loadAccount(journal.record.profileId);
      if (journal.record.accountUuid && account.accountUuid !== journal.record.accountUuid) fail('wrong_account');
      const nativeMetadata = { ...(credentials?.claudeAiOauth ?? {}) };
      for (const name of ['accessToken', 'refreshToken', 'refreshTokenExpiresAt', 'expiresAt']) delete nativeMetadata[name];
      return { ...account.credentials, claudeAiOauth: { ...account.credentials.claudeAiOauth, ...nativeMetadata } };
    };
    const importGrant = async value => {
      const journal = journalFor(value.credentials);
      const credentials = await currentGrant(value.credentials);
      const imported = await vault.importGrant({ ...value, credentials, ...(journal ? { receipt: journal.record, household: journal.record.household } : {}) });
      if (journal && !journal.record.acknowledgedAt) await acknowledge(journal, imported.name);
      await retireSupersededRefresh(imported.name);
      return imported;
    };
    const retireSupersededRefresh = async profileId => {
      const profile = await getProfile(config, profileId);
      const current = await store.read(profile.configDirectory);
      const capturedAt = Date.parse(current.ccpickRuntimeGrant?.capturedAt);
      if (!Number.isFinite(capturedAt)) return;
      for (const journal of [...journals.values()]) {
        if (journal.record.reason !== 'oauth_refresh_completed' || journal.record.profileId !== profileId ||
            profile.account?.uuid !== journal.record.accountUuid ||
            capturedAt <= Date.parse(journal.record.capturedAt)) continue;
        await acknowledge(journal, profileId);
      }
    };
    const recoverJournals = async () => {
      for (const journal of [...journals.values()]) {
        if (!['oauth_exchange_completed', 'oauth_refresh_completed'].includes(journal.record.reason) || journal.nextAttemptAt > Date.now()) continue;
        journal.nextAttemptAt = Date.now() + recoveryRetryMs;
        try {
          if (journal.record.acknowledgedAt) { await acknowledge(journal, journal.record.profileId); continue; }
          if (journal.record.reason === 'oauth_refresh_completed') {
            await retireSupersededRefresh(journal.record.profileId);
            if (!journals.has(journal.record.id)) continue;
          }
          const credentials = journal.credentials.recoveredCredentials ??
            credentialsFromOAuthResponse(journal.credentials.oauthResponse, journal.record.capturedAt);
          const imported = await vault.recoverGrant({ credentials, receipt: journal.record,
            household: journal.record.household, persist: async value => {
            journal.credentials = { ...journal.credentials, recoveredCredentials: value };
            await store.write(journal.directory, journal.credentials);
          } });
          await acknowledge(journal, imported.name);
        } catch (error) {
          await atomicJson(path.join(root, 'last-recovery-error.json'), { reason: publicReason(error),
            at: new Date().toISOString() }).catch(() => {});
        }
      }
    };
    vault = new RuntimeVault(config, checked.proxy, { store, request: requestAccount, egress,
      pendingRefresh: name => [...journals.values()].some(journal =>
        journal.record.reason === 'oauth_refresh_completed' && journal.record.profileId === name && !journal.record.acknowledgedAt),
      preserveRefresh: async ({ name, accountUuid, organizationUuid, credentials, household }) => {
        const directory = await preserveGrant(root, { recoveredCredentials: credentials },
          { reason: 'oauth_refresh_completed', profileId: name, accountUuid, organizationUuid, household });
        return [...journals.values()].find(journal => journal.directory === directory).record;
      },
      acknowledgeRefresh: async ({ receipt, name }) => {
        const journal = journals.get(receipt.id);
        if (journal) await acknowledge(journal, name);
      },
    });
    await loadJournals(root);
    for (const entry of await fs.readdir(path.join(root, 'scopes'), { withFileTypes: true }).catch(error => {
      if (error.code === 'ENOENT') return []; throw error;
    })) if (entry.isDirectory() && validName(entry.name)) await loadJournals(path.join(root, 'scopes', entry.name));
    await recoverJournals();
    const setupScope = async (scope = 'default') => {
      if (closing) fail('runtime_unavailable');
      if (scope !== 'default' && !validName(scope)) fail('runtime_unavailable');
      if (scopes.has(scope)) return scopes.get(scope);
      const pending = track((async () => {
      const scopeRoot = scope === 'default' ? root : path.join(root, 'scopes', scope);
      const scopeDirectory = path.join(scopeRoot, 'claude');
      await fs.mkdir(scopeDirectory, { recursive: true, mode: 0o700 });
      await fs.mkdir(path.join(scopeRoot, 'leases'), { mode: 0o700, recursive: true });
      await regular(scopeRoot, true); await regular(scopeDirectory, true);
      const readSelection = scope === 'default' ? () => registryState(config) :
        () => readJson(path.join(scopeRoot, 'selection.json'));
      if (scope !== 'default' && !await readJson(path.join(scopeRoot, 'selection.json'), true))
        await atomicJson(path.join(scopeRoot, 'selection.json'), { version: 2, enabled: true, selected: scope,
          selectedAt: new Date().toISOString() });
      const selected = await readSelection();
      const profile = await getProfile(config, selected.selected);
      if (!await readJson(path.join(scopeDirectory, 'settings.json'), true)) {
        const settings = structuredClone(await readJson(path.join(profile.configDirectory, 'settings.json'), true) ?? {});
        const bridgePath = path.join(config.dataRoot, '..', 'app', 'bridge.py').replaceAll('\\', '/');
        for (const [event, groups] of Object.entries(settings.hooks ?? {})) {
          settings.hooks[event] = groups.map(group => ({ ...group, hooks: (group.hooks ?? []).filter(hook =>
            !(typeof hook.command === 'string' && hook.command.replaceAll('\\', '/').includes(bridgePath))) }))
            .filter(group => group.hooks.length);
          if (!settings.hooks[event].length) delete settings.hooks[event];
        }
        await atomicJson(path.join(scopeDirectory, 'settings.json'), settings);
        const original = await readJson(globalConfigFile(profile));
        await atomicJson(path.join(scopeDirectory, '.claude.json'), original);
      }
      const runtimeProfile = { root: scopeRoot, name: 'active-runtime', configDirectory: scopeDirectory };
      await syncRuntimeSettings(config, { key: key.value }, scope);
      await prepareHistory(config, runtimeProfile);
      await syncResources(config, runtimeProfile);
      if (!await readJson(path.join(scopeRoot, 'active.json'), true))
        await prepareInputHistory(config, runtimeProfile);
      let publishedRevision;
      const coordinator = new ActiveRuntime({
      readSelection,
      select: scope === 'default' ? async (name, options) => (await selectProfile(config, name, options)).selectionReceipt : async (name, { expectedState }) => {
        const current = await readSelection();
        if (current.selected !== expectedState.selected || current.selectedAt !== expectedState.selectedAt) fail('selection_changed');
        await vault.loadAccount(name);
        const generation = new Date(Math.max(Date.now(), (Date.parse(current.selectedAt) || 0) + 1)).toISOString();
        const receipt = Object.freeze({ profileId: name, generation });
        await atomicJson(path.join(scopeRoot, 'selection.json'), { ...current, selected: name, selectedAt: generation });
        return receipt;
      },
      loadAccount: name => vault.loadAccount(name),
      routeChanged: async snapshot => (await egress.account(snapshot.profileId)).revision !== snapshot.egress?.revision,
      loginHousehold: async (name, requested) => {
        const route = await egress.account(name);
        if (requested !== undefined && requested !== route.household) fail('wrong_account');
        return route.household;
      },
      published: async snapshot => {
        const revision = `${snapshot.profileId}:${snapshot.egress?.revision}`;
        if (publishedRevision && publishedRevision !== revision) {
          if (scope === 'default') guard?.invalidateTunnels();
          else if (scopeGuards.has(scope)) (await scopeGuards.get(scope)).invalidateTunnels();
        }
        publishedRevision = revision;
      },
      bootstrapAccessToken: async name => (await store.read((await getProfile(config, name)).configDirectory))?.claudeAiOauth?.accessToken,
      verifyGrant: async (grant, route) => vault.verifyGrant(await currentGrant(grant), route),
      importGrant,
      readControl: () => readJson(path.join(scopeRoot, 'coordinator.json'), true),
      writeControl: value => atomicJson(path.join(scopeRoot, 'coordinator.json'), value),
      quarantineGrant: async (value, reason) => {
        await preserveGrant(scopeRoot, value.credentials, { reason,
          accountUuid: value.ids?.oauthAccount?.accountUuid });
      },
      readMirror: async () => ({ ids: await readJson(path.join(scopeDirectory, '.claude.json'), true),
        credentials: await store.read(scopeDirectory) }),
      writeMirror: async ({ ids, credentials, profileId }) => {
        const previous = await readJson(path.join(scopeDirectory, '.claude.json'), true) ?? {};
        const next = { ...previous, userID: ids.userID, machineID: ids.machineID,
          oauthAccount: ids.oauthAccount, hasCompletedOnboarding: true };
        await atomicJson(path.join(scopeDirectory, '.claude.json'), next);
        const nativeCredentials = await store.read(scopeDirectory);
        const initialized = await readJson(path.join(scopeRoot, 'active.json'), true);
        const source = initialized || Object.keys(nativeCredentials).length ? nativeCredentials : credentials;
        const { ccpickRuntimeGrant, claudeAiOauth, ...otherCredentials } = source;
        await store.write(scopeDirectory, { ...otherCredentials, claudeAiOauth: credentials.claudeAiOauth });
        await atomicJson(path.join(scopeRoot, 'active.json'), { profileId, updatedAt: new Date().toISOString() });
      },
    });
    try { await coordinator.snapshot(); }
    catch (error) { if (!reauthorizationReasons.has(error.message)) throw error; }
      return { coordinator, directory: scopeDirectory, scope, readSelection };
      })());
      scopes.set(scope, pending);
      try { return await pending; } catch (error) { scopes.delete(scope); throw error; }
    };
    const { coordinator } = await setupScope();
    broker = createMessageBroker({ localKey: key.value, upstreamProxy: checked.proxy,
      ...(brokerTransport ? { transport: brokerTransport } : {}),
      getSnapshot: ({ headers }) => track((async () => {
        if (closing) fail('runtime_unavailable');
        return (await setupScope(headers['x-ccpick-account-scope'] ?? 'default')).coordinator.snapshot();
      })()) });
    const oauthHandler = (request, response) => track((async () => {
      try {
        if (closing) fail('runtime_unavailable');
        let body = '';
        for await (const chunk of request) {
          body += chunk; if (Buffer.byteLength(body) > 32_768) fail('auth_unverified');
        }
        const exchange = JSON.parse(body);
        if (exchange.grant_type !== 'authorization_code' || typeof exchange.code !== 'string') fail('auth_unverified');
        const binding = { oauthState: exchange.state, codeVerifier: exchange.code_verifier };
        let loginCoordinator, loginRoute;
        for (const pending of scopes.values()) {
          const item = await pending;
          if (request.tykScope !== undefined && item.scope !== request.tykScope) continue;
          try {
            const intent = await item.coordinator.validateLoginExchange(binding);
            loginCoordinator = item.coordinator; loginRoute = await egress.group(intent.household ?? await egress.defaultGroup()); break;
          }
          catch (error) { if (!['login_intent_expired', 'login_state_mismatch', 'login_pkce_mismatch'].includes(error.message)) throw error; }
        }
        if (!loginCoordinator) fail('auth_unverified');
        const grant = await vault.exchangeLogin({ body: exchange }, { proxy: loginRoute.proxy });
        await preserveGrant(root, { oauthResponse: grant, exchange: binding }, { reason: 'oauth_exchange_completed', household: loginRoute.household });
        await loginCoordinator.observeLoginGrant({ ...binding, accessToken: grant.access_token });
        response.writeHead(200, { 'content-type': 'application/json', 'cache-control': 'no-store' });
        response.end(JSON.stringify(grant));
      } catch (error) {
        response.writeHead(409, { 'content-type': 'application/json' });
        response.end(JSON.stringify({ error: 'account_login_not_committed', error_description: publicReason(error) }));
      }
    })());
    makeGuard = scope => createNativeApiGuard({ tls: certificate, upstreamProxy: checked.proxy,
      ...(scopedEgress ? { scope } : {}),
      resolveProxy: async input => {
        if (input.kind === 'tunnel' && !isAccountTunnelHost(input.hostname)) return { proxy: checked.proxy };
        if (input.kind === 'api' && input.headers.authorization) {
          const bearer = input.headers.authorization.replace(/^Bearer /i, '');
          const receipt = receipts.get(digest(bearer));
          if (receipt?.record.profileId) return egress.account(receipt.record.profileId);
          if (receipt?.record.household) return egress.group(receipt.record.household);
          if (scopedEgress) return vault.bearerEgress(bearer);
        }
        const scoped = await setupScope(scope);
        return egress.account((await scoped.readSelection()).selected);
      },
      messagesHandler: broker.handleOfficial, localKey: key.value,
      oauthHandler: (req, res) => { if (scopedEgress) req.tykScope = scope; return oauthHandler(req, res); },
      ...(apiHandler ? { apiHandler } : {}) });
    guard = makeGuard('default');
    await guard.listen(previousService?.proxyPort ?? 0);
    if (Object.values(portState.ports).includes(guard.address().port)) fail('runtime_unavailable');
    await syncRuntimeSettings(config, { key: key.value, proxyPort: guard.address().port, egressVersion: 1 });
    const scopeGuard = scope => {
      if (scope === 'default' || (!scopedEgress && !portState.ports[scope])) return Promise.resolve(guard);
      if (scopeGuards.has(scope)) return scopeGuards.get(scope);
      const pending = guardTail.then(async () => {
        const boundary = makeGuard(scope);
        try {
          await boundary.listen(portState.ports[scope] ?? 0);
          portState.ports[scope] = boundary.address().port;
          await atomicJson(portsFile, portState);
          await syncRuntimeSettings(config, { key: key.value, proxyPort: boundary.address().port, egressVersion: 1 }, scope);
          return boundary;
        } catch (error) { await boundary.close(); throw error; }
      });
      guardTail = pending.catch(() => {}); scopeGuards.set(scope, pending);
      return pending;
    };
    for (const scope of Object.keys(portState.ports)) {
      await setupScope(scope); await scopeGuard(scope);
    }
    const authorized = request => {
      const given = request.headers['x-ccpick-account-runtime'];
      const occurrences = request.rawHeaders.filter((value, index) => index % 2 === 0 && value.toLowerCase() === 'x-ccpick-account-runtime').length;
      return !('origin' in request.headers) && occurrences === 1 &&
        request.headers.host === `127.0.0.1:${control.address().port}` &&
        typeof given === 'string' && /^[a-f0-9]{64}$/.test(given) &&
        timingSafeEqual(Buffer.from(given), Buffer.from(key.value));
    };
    control = http.createServer((request, response) => { track((async () => {
      response.setHeader('content-type', 'application/json');
      if (closing || !authorized(request) || request.method !== 'POST') { response.writeHead(403); response.end('{}'); return; }
      const guardedSelection = request.url === '/select-guarded';
      try {
        let body = '';
        for await (const chunk of request) {
          body += chunk; if (Buffer.byteLength(body) > 65536) fail('runtime_unavailable');
        }
        const input = JSON.parse(body || '{}'); let result;
        const expectedState = guardedSelection ? checkedSelectionGuard(input?.expectedState) : undefined;
        if (request.url === '/select' && Object.hasOwn(input, 'expectedState')) fail('selection_changed');
        const scoped = await setupScope(input.scope ?? 'default');
        const current = scoped.coordinator;
        if (request.url === '/status') result = { ...current.status(), pid: process.pid, instanceId, householdOptions };
        else if (request.url === '/browser-route') {
          const route = await egress.account((await scoped.readSelection()).selected);
          result = { household: route.household, scope: scoped.scope };
        }
        else if (request.url === '/register') {
          const boundary = await scopeGuard(scoped.scope);
          const profile = await getProfile(config, (await scoped.readSelection()).selected);
          const household = supportedHousehold(profile.household ?? await router.defaultGroup());
          result = { directory: scoped.directory, scope: scoped.scope, proxyPort: boundary.address().port,
            egressVersion: 1, householdOptions, household };
        }
        else if (request.url === '/egress-set') {
          const profile = await resolveProfile(config, input.name);
          const route = await egress.setAccount(profile.name, input.household);
          const affected = [];
          for (const pending of scopes.values()) {
            const item = await pending;
            if ((await item.readSelection()).selected !== profile.name) continue;
            const boundary = item.scope === 'default' ? guard : await scopeGuards.get(item.scope);
            boundary?.invalidateTunnels(); affected.push(item);
          }
          for (const item of affected) await item.coordinator.snapshot();
          result = { household: route.household, egressVersion: 1 };
        }
        else if (request.url === '/select' || guardedSelection) {
          const profile = await resolveProfile(config, input.name);
          const selected = await current.select(profile.name, { expectedState });
          result = guardedSelection ? { selectionReceipt: checkedSelectionReceipt({
            profileId: selected.profileId, generation: selected.generation }) } : current.status();
        } else if (request.url === '/ready') {
          await vault.loadAccount((await resolveProfile(config, input.name)).name); result = { ready: true };
        } else if (request.url === '/login-begin') result = await current.beginLogin(input);
        else if (request.url === '/login-finished') { await current.loginFinished(); result = current.status(); }
        else fail('runtime_unavailable');
        response.end(JSON.stringify({ ok: true, ...result, ...(guardedSelection ? { selectionGuard: 1 } : {}) }));
      } catch (error) {
        response.writeHead(409); response.end(JSON.stringify({ ok: false, reason: publicReason(error),
          ...(guardedSelection ? { selectionGuard: 1 } : {}) }));
      }
    })()).catch(() => response.destroy()); });
    await new Promise((resolve, reject) => {
      const failed = error => { control.off('listening', started); reject(error); };
      const started = () => { control.off('error', failed); resolve(); };
      control.once('error', failed); control.once('listening', started);
      control.listen(previousService?.controlPort ?? 0, '127.0.0.1');
    });
    if (Object.values(portState.ports).includes(control.address().port)) fail('runtime_unavailable');
    await atomicJson(path.join(root, 'service.json'), { version: 1, pid: process.pid, instanceId, processIdentity,
      startedAt: new Date().toISOString(), controlPort: control.address().port, proxyPort: guard.address().port,
      certFile: certificate.certFile, certificateFingerprint: certificate.fingerprint,
      channelKeyFingerprint: digest(key.value), egressVersion: 1, householdOptions });
    let polling = false;
    const pollFailures = new WeakMap();
    interval = setInterval(() => {
      if (closing || polling) return; polling = true;
      track((async () => {
        try {
          for (const pending of scopes.values()) {
            const scoped = await pending, previous = pollFailures.get(scoped);
            if (previous?.nextAttemptAt > Date.now()) continue;
            try { await scoped.coordinator.snapshot(); pollFailures.delete(scoped); }
            catch (error) {
              const attempts = Math.min((previous?.attempts ?? 0) + 1, 5);
              pollFailures.set(scoped, { attempts, nextAttemptAt: Date.now() + Math.min(2000 * 2 ** (attempts - 1), 30_000) });
              await atomicJson(path.join(root, 'last-error.json'), { reason: publicReason(error),
                scope: scoped.scope, at: new Date().toISOString() }).catch(() => {});
            }
          }
        } finally { try { await recoverJournals(); } finally { polling = false; } }
      })());
    }, 500);
    for (const signal of ['SIGTERM', 'SIGINT']) {
      const handler = () => { cleanup().finally(() => process.exit(0)); };
      signalHandlers.push([signal, handler]); process.once(signal, handler);
    }
    if (typeof process.send === 'function') {
      const stop = () => { cleanup().finally(() => process.exit(0)); };
      const message = value => { if (value?.type === 'runtime-supervisor-stop') stop(); };
      signalHandlers.push(['message', message], ['disconnect', stop]);
      process.on('message', message); process.once('disconnect', stop);
      if (process.connected === false) queueMicrotask(stop);
    }
    return { coordinator, guard, control, close: cleanup };
  } catch (error) { await cleanup(); throw error; }
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  try { await serveRuntime(await loadInstall(process.argv[2])); }
  catch (error) { process.stderr.write(publicReason(error) + '\n'); process.exitCode = 1; }
}
