import { createHash, randomUUID } from 'node:crypto';

const fingerprint = value => createHash('sha256').update(value ?? '').digest('hex');
const fail = reason => { throw new Error(reason); };
const sameSelection = (a, b) => a?.selected === b?.selected && a?.selectedAt === b?.selectedAt;
const tokenOf = value => value?.claudeAiOauth?.accessToken;
const hasRefresh = value => ['refreshToken', 'refreshTokenExpiresAt'].some(
  key => Object.hasOwn(value?.claudeAiOauth ?? {}, key));
const available = selection => selection?.selected && !selection.maintenance;

export function checkedSelectionGuard(value) {
  if (!value || typeof value !== 'object' || Array.isArray(value) ||
      Object.keys(value).length !== 2 || !['selected', 'selectedAt'].every(key =>
        typeof value[key] === 'string' && value[key].length > 0 && value[key].length <= 128 &&
        value[key].trim() === value[key] && !/[\x00-\x1f\x7f]/.test(value[key]))) fail('selection_changed');
  return Object.freeze({ selected: value.selected, selectedAt: value.selectedAt });
}

export function checkedSelectionReceipt(value) {
  if (!value || typeof value !== 'object' || Array.isArray(value) ||
      Object.keys(value).length !== 2 || !Object.hasOwn(value, 'profileId') || !Object.hasOwn(value, 'generation'))
    fail('selection_changed');
  const guard = checkedSelectionGuard({ selected: value.profileId, selectedAt: value.generation });
  return Object.freeze({ profileId: guard.selected, generation: guard.selectedAt });
}

export function accessOnly(credentials) {
  const result = structuredClone(credentials);
  if (typeof tokenOf(result) !== 'string' || !tokenOf(result)) fail('login_required');
  delete result.claudeAiOauth.refreshToken;
  delete result.claudeAiOauth.refreshTokenExpiresAt;
  return result;
}

function bundle(account, selection) {
  const ids = account.ids;
  const oauth = ids?.oauthAccount;
  const token = tokenOf(account.credentials);
  if (!account.name || !token || oauth?.accountUuid !== account.accountUuid ||
      !['userID', 'machineID'].every(k => /^[a-f0-9]{64}$/.test(ids?.[k] ?? '')))
    fail('account_snapshot_invalid');
  return Object.freeze({ profileId: account.name, accessToken: token,
    deviceId: ids.userID, machineId: ids.machineID, accountUuid: account.accountUuid,
    organizationUuid: oauth.organizationUuid, generation: selection.selectedAt,
    expiresAt: account.credentials.claudeAiOauth.expiresAt,
    ...(account.egress ? { egress: account.egress } : {}) });
}

export class ActiveRuntime {
  #tail = Promise.resolve();
  #snapshot;
  #selection;
  #intent;
  #mirroredToken;
  #knownTokens = new Set();
  #boundGrants = new Map();
  #adapter;
  #now;
  #loaded = false;
  #ready = false;

  constructor(adapter, { now = Date.now } = {}) {
    this.#adapter = adapter;
    this.#now = now;
  }

  #serial(action) {
    const task = this.#tail.then(action);
    this.#tail = task.catch(() => {});
    return task;
  }

  async #publish(selection) {
    const account = await this.#adapter.loadAccount(selection.selected);
    const next = bundle(account, selection);
    let current = await this.#adapter.readSelection();
    if (!available(current) || !sameSelection(selection, current)) return false;
    await this.#adapter.writeMirror({ ids: structuredClone(account.ids),
      credentials: accessOnly(account.credentials), profileId: account.name });
    current = await this.#adapter.readSelection();
    if (!available(current) || !sameSelection(selection, current)) return false;
    this.#knownTokens.add(fingerprint(next.accessToken));
    this.#mirroredToken = fingerprint(next.accessToken);
    this.#snapshot = next;
    this.#selection = selection;
    await this.#adapter.published?.(next);
    await this.#saveControl();
    return true;
  }

  async #loadControl() {
    if (!this.#loaded) {
      const control = await this.#adapter.readControl?.();
      if (control) {
        if (control.version !== 1 || !Array.isArray(control.knownTokens) ||
            !control.knownTokens.every(value => /^[a-f0-9]{64}$/.test(value)))
          fail('runtime_control_invalid');
        this.#intent = control.intent;
        this.#boundGrants = new Map((control.boundGrants ?? []).map(value => [value.tokenHash, value]));
        this.#mirroredToken = control.mirroredToken;
        this.#knownTokens = new Set(control.knownTokens ?? []);
      } else {
        const initial = await this.#adapter.readSelection();
        const token = this.#adapter.bootstrapAccessToken ? await this.#adapter.bootstrapAccessToken(initial.selected) :
          bundle(await this.#adapter.loadAccount(initial.selected), initial).accessToken;
        if (typeof token === 'string' && token) {
          this.#mirroredToken = fingerprint(token);
          this.#knownTokens.add(this.#mirroredToken);
        }
      }
      this.#loaded = true;
    }
  }

  async #quarantine(staged, reason) {
    if (!this.#adapter.quarantineGrant) fail('runtime_quarantine_unavailable');
    await this.#adapter.quarantineGrant(staged, reason);
  }

  async #reconcile({ recoverUnclaimed = false } = {}) {
    await this.#loadControl();
    for (let attempt = 0; attempt < 8; attempt++) {
      const selection = await this.#adapter.readSelection();
      if (!selection?.selected || selection.maintenance) fail('runtime_unavailable');
      const staged = await this.#adapter.readMirror();
      const token = tokenOf(staged?.credentials);
      const tokenHash = token && fingerprint(token);
      const changed = tokenHash && tokenHash !== this.#mirroredToken;
      const refreshPresent = hasRefresh(staged?.credentials);
      const unknown = tokenHash && !this.#knownTokens.has(tokenHash);
      const bound = tokenHash && this.#boundGrants.get(tokenHash);
      if (token && bound && (changed || refreshPresent)) {
        const remote = await this.#adapter.verifyGrant(staged.credentials, { household: bound.intent.household });
        if (!remote?.accountUuid) fail('auth_unverified');
        const imported = await this.#adapter.importGrant({ credentials: staged.credentials,
          remote, intentId: bound.intent.id, household: bound.intent.household });
        this.#knownTokens.add(tokenHash);
        const current = await this.#adapter.readSelection();
        if (available(current) && this.#intent?.id === bound.intent.id &&
            !this.#intent.superseded && sameSelection(bound.intent.selection, current))
          await this.#adapter.select(imported.name, { expectedState: current });
        if (this.#intent?.id === bound.intent.id) this.#intent = undefined;
        this.#boundGrants.delete(tokenHash);
        await this.#saveControl();
        if (!(await this.#publish(await this.#adapter.readSelection()))) continue;
        return this.#snapshot;
      }
      if (unknown) {
        if (!recoverUnclaimed) fail('unclaimed_login_credentials');
        await this.#quarantine(staged, 'unclaimed_login_credentials');
        this.#intent = undefined;
        await this.#saveControl();
        if (!(await this.#publish(selection))) continue;
        return this.#snapshot;
      }
      if (token && (changed || refreshPresent)) {
        await this.#quarantine(staged, 'stale_runtime_credentials');
        if (!(await this.#publish(selection))) continue;
        return this.#snapshot;
      }
      if (!this.#snapshot || !sameSelection(selection, this.#selection)) {
        if (!(await this.#publish(selection))) continue;
      } else if (await this.#adapter.routeChanged?.(this.#snapshot) || (this.#adapter.needsRefresh ? this.#adapter.needsRefresh(this.#snapshot) :
          !Number.isFinite(this.#snapshot.expiresAt) || this.#snapshot.expiresAt <= this.#now() + 60_000)) {
        if (!(await this.#publish(selection))) continue;
      }
      return this.#snapshot;
    }
    fail('selection_changed');
  }

  beginLogin({ sessionId, oauthState, oauthChallenge, household } = {}) {
    return this.#serial(async () => {
      await this.#loadControl();
      if (this.#intent?.expiresAt > this.#now() && !this.#intent.superseded) {
        if (!sessionId || this.#intent.sessionId !== sessionId) fail('login_in_progress');
        if (!oauthState || oauthState === this.#intent.oauthState)
          return { id: this.#intent.id, expiresAt: this.#intent.expiresAt, household: this.#intent.household };
        this.#intent = undefined;
        await this.#saveControl();
      }
      try { await this.#reconcileStatus({ recoverUnclaimed: true }); }
      catch (error) {
        if (!['login_required', 'auth_forbidden', 'auth_unverified', 'auth_rate_limited',
          'network_unavailable', 'auth_renewal_failed'].includes(error.message)) throw error;
      }
      const selection = { ...await this.#adapter.readSelection() };
      const resolvedHousehold = await this.#adapter.loginHousehold?.(selection.selected, household);
      const intent = { id: randomUUID(), sessionId, oauthState, oauthChallenge,
        ...(resolvedHousehold ? { household: resolvedHousehold } : {}),
        selection, expiresAt: this.#now() + 10 * 60_000 };
      this.#intent = intent;
      await this.#saveControl();
      return { id: intent.id, expiresAt: intent.expiresAt, household: intent.household };
    });
  }

  #exchangeIntent({ oauthState, codeVerifier }) {
    const intent = this.#intent;
    if (!intent || intent.expiresAt <= this.#now()) fail('login_intent_expired');
    if (typeof oauthState !== 'string' || !oauthState || oauthState !== intent.oauthState)
      fail('login_state_mismatch');
    if (typeof codeVerifier !== 'string' || !codeVerifier || !intent.oauthChallenge ||
        createHash('sha256').update(codeVerifier).digest('base64url') !== intent.oauthChallenge)
      fail('login_pkce_mismatch');
    return intent;
  }

  validateLoginExchange(request) {
    return this.#serial(async () => {
      await this.#loadControl();
      const intent = this.#exchangeIntent(request);
      return { intentId: intent.id, ...(intent.household !== undefined ? { household: intent.household } : {}) };
    });
  }

  observeLoginGrant({ oauthState, codeVerifier, accessToken }) {
    return this.#serial(async () => {
      await this.#loadControl();
      const intent = this.#exchangeIntent({ oauthState, codeVerifier });
      if (typeof accessToken !== 'string' || !accessToken) fail('auth_unverified');
      const tokenHash = fingerprint(accessToken);
      if (intent.allowedToken && intent.allowedToken !== tokenHash) fail('login_grant_conflict');
      intent.allowedToken = tokenHash;
      this.#boundGrants.set(tokenHash, { tokenHash, intent: structuredClone(intent) });
      await this.#saveControl();
      return { intentId: intent.id };
    });
  }

  async #reconcileStatus(options) {
    try {
      const snapshot = await this.#reconcile(options);
      this.#ready = true;
      return snapshot;
    } catch (error) { this.#ready = false; throw error; }
  }

  snapshot() { return this.#serial(() => this.#reconcileStatus()); }
  loginFinished() { return this.snapshot(); }

  select(name, { expectedState: requestedState } = {}) {
    const requested = requestedState === undefined ? undefined : structuredClone(requestedState);
    return this.#serial(async () => {
      await this.#loadControl();
      const expectedState = await this.#adapter.readSelection();
      if (!available(expectedState)) fail('runtime_unavailable');
      if (requested !== undefined && !sameSelection(checkedSelectionGuard(requested), expectedState)) fail('selection_changed');
      await this.#adapter.loadAccount(name); // validate before changing default
      this.#ready = false;
      const committed = await this.#adapter.select(name, { expectedState });
      const receipt = requested === undefined ? undefined : checkedSelectionReceipt(committed);
      if (receipt && receipt.profileId !== name) fail('selection_changed');
      if (this.#intent) this.#intent.superseded = true;
      await this.#saveControl();
      const snapshot = await this.#reconcileStatus({ recoverUnclaimed: true });
      if (receipt && (snapshot.profileId !== receipt.profileId || snapshot.generation !== receipt.generation))
        fail('selection_changed');
      return snapshot;
    });
  }

  status() {
    const value = this.#snapshot;
    return value ? { ready: this.#ready, profileId: value.profileId, generation: value.generation,
      loginPending: Boolean(this.#intent && !this.#intent.superseded && this.#intent.expiresAt > this.#now()) } : { ready: false };
  }

  async #saveControl() {
    await this.#adapter.writeControl?.({ version: 1, intent: this.#intent,
      mirroredToken: this.#mirroredToken, knownTokens: [...this.#knownTokens],
      boundGrants: [...this.#boundGrants.values()] });
  }
}
