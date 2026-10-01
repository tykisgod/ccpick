import test from 'node:test';
import { createHash } from 'node:crypto';
import assert from 'node:assert/strict';
import { ActiveRuntime, accessOnly } from '../active-runtime.mjs';

const verifier = 'invented-pkce-verifier-123456789012345678901234567890';
const oauthChallenge = createHash('sha256').update(verifier).digest('base64url');
const bind = (runtime, name, oauthState = 'state-1') => runtime.observeLoginGrant({
  oauthState, codeVerifier: verifier, accessToken: `invented-access-${name}` });
const clone = value => structuredClone(value);
const grant = (name, extra = {}) => ({ claudeAiOauth: {
  accessToken: `invented-access-${name}`, refreshToken: `invented-refresh-${name}`,
  refreshTokenExpiresAt: 9_000_000, expiresAt: 1_000_000,
  scopes: ['user:profile', 'user:inference'], ...extra,
} });
const account = (name, digit) => ({ name, accountUuid: `account-${name}`,
  ids: { userID: digit.repeat(64), machineID: String(Number(digit) + 3).repeat(64),
    oauthAccount: { accountUuid: `account-${name}`, organizationUuid: `org-${name}` } },
  credentials: grant(name),
});
function fixture() {
  const accounts = new Map(['A', 'B', 'C'].map((name, i) => [name, account(name, String(i + 1))]));
  const state = { selection: { selected: 'A', selectedAt: 1 }, control: undefined,
    mirror: { ids: clone(accounts.get('A').ids), credentials: accessOnly(grant('A')), profileId: 'A' },
    imports: [], quarantined: [], writes: [], loads: [], verified: [] };
  const seams = {};
  const adapter = {
    async readSelection() { return clone(state.selection); },
    async loadAccount(name) {
      state.loads.push(name);
      if (seams.loadAccount) await seams.loadAccount(name);
      if (!accounts.has(name)) throw new Error('account_not_found');
      return clone(accounts.get(name));
    },
    async writeMirror(value) {
      state.writes.push(clone(value)); state.mirror = clone(value);
      await seams.writeMirror?.(value);
    },
    async readMirror() { return clone(state.mirror); },
    async readControl() { return clone(state.control); },
    async writeControl(value) { state.control = clone(value); await seams.writeControl?.(value); },
    async verifyGrant(credentials) {
      state.verified.push(clone(credentials));
      await seams.verifyGrant?.(credentials);
      const token = credentials.claudeAiOauth.accessToken;
      const name = token.replace('invented-access-', '').split('-')[0];
      if (!accounts.has(name)) throw new Error('auth_unverified');
      return { accountUuid: `account-${name}`, organizationUuid: `org-${name}` };
    },
    async importGrant(value) {
      const entry = [...accounts.values()].find(a => a.accountUuid === value.remote.accountUuid);
      if (!entry) throw new Error('wrong_account');
      state.imports.push(clone(value)); entry.credentials = clone(value.credentials);
      return { name: entry.name };
    },
    async select(name, { expectedState }) {
      if (state.selection.maintenance) throw new Error('runtime_unavailable');
      if (expectedState.selected !== state.selection.selected ||
          expectedState.selectedAt !== state.selection.selectedAt) throw new Error('selection_changed');
      state.selection = { selected: name, selectedAt: state.selection.selectedAt + 1 };
      return Object.freeze({ profileId: name, generation: state.selection.selectedAt });
    },
    async quarantineGrant(staged, reason) { state.quarantined.push({ staged: clone(staged), reason }); },
  };
  let now = 1_000;
  const runtime = () => new ActiveRuntime(adapter, { now: () => now });
  return { state, seams, adapter, accounts, runtime, setNow: value => { now = value; },
    stage: (name, credentials = grant(name)) => {
      state.mirror = { profileId: 'untrusted-local-name', ids: clone(accounts.get('C').ids), credentials };
    } };
}

test('access-only copies preserve the vault grant and omit native dead-token fields', () => {
  const original = grant('A'); const mirror = accessOnly(original);
  assert.equal(mirror.claudeAiOauth.refreshToken, undefined);
  assert.equal(mirror.claudeAiOauth.refreshTokenExpiresAt, undefined);
  assert.equal(original.claudeAiOauth.refreshToken, 'invented-refresh-A');
  assert.throws(() => accessOnly({}), /login_required/);
});

test('snapshots contain one complete account and stay immutable across a picker change', async () => {
  const f = fixture(); const runtime = f.runtime(); const a = await runtime.snapshot();
  const b = await runtime.select('B');
  assert.equal(a.profileId, 'A'); assert.equal(a.accessToken, 'invented-access-A');
  assert.equal(b.profileId, 'B'); assert.equal(b.deviceId, '2'.repeat(64));
  assert.equal(b.accountUuid, 'account-B'); assert.equal(b.expiresAt, 1_000_000);
  assert.ok(Object.isFrozen(a)); assert.ok(Object.isFrozen(b));
  assert.equal(f.state.mirror.credentials.claudeAiOauth.refreshToken, undefined);
});

test('guarded selection refuses a later manual choice before loading target credentials', async () => {
  const f = fixture(), runtime = f.runtime();
  f.state.selection = { selected: 'A', selectedAt: 'generation-a' }; await runtime.snapshot();
  const expectedState = clone(f.state.selection);
  await runtime.select('C');
  const loads = f.state.loads.length, writes = f.state.writes.length;
  await assert.rejects(runtime.select('B', { expectedState }), /^Error: selection_changed$/);
  assert.equal(f.state.selection.selected, 'C');
  assert.equal(f.state.loads.length, loads); assert.equal(f.state.writes.length, writes);
});

test('a queued automatic guard is checked after the earlier manual choice commits', async () => {
  const f = fixture(), runtime = f.runtime();
  f.state.selection = { selected: 'A', selectedAt: 'generation-a' }; await runtime.snapshot();
  const expectedState = clone(f.state.selection);
  let release, entered;
  const waiting = new Promise(resolve => { entered = resolve; });
  f.seams.loadAccount = async name => {
    if (name === 'C' && !release) await new Promise(resolve => { release = resolve; entered(); });
  };
  const manual = runtime.select('C'); await waiting;
  const automatic = runtime.select('B', { expectedState });
  release(); await manual;
  await assert.rejects(automatic, /^Error: selection_changed$/);
  assert.equal(f.state.selection.selected, 'C');
  assert.equal(f.state.loads.includes('B'), false);
});

test('matching guard retains the existing CAS check during target authentication', async () => {
  const f = fixture(), runtime = f.runtime();
  f.state.selection = { selected: 'A', selectedAt: 'generation-a' }; await runtime.snapshot();
  const expectedState = clone(f.state.selection);
  f.seams.loadAccount = async name => {
    if (name === 'B') f.state.selection = { selected: 'C', selectedAt: 'later-external-choice' };
  };
  await assert.rejects(runtime.select('B', { expectedState }), /^Error: selection_changed$/);
  assert.equal(f.state.selection.selected, 'C');
});

test('guarded selection validates complete fields and matching guards can commit', async () => {
  const f = fixture(), runtime = f.runtime();
  f.state.selection = { selected: 'A', selectedAt: 'generation-a' }; await runtime.snapshot();
  const bad = [null, {}, [], { selected: 'A' }, { selectedAt: 'generation-a' },
    { selected: 'A', selectedAt: null }, { selected: 'A', selectedAt: '' },
    { selected: 'A', selectedAt: 'generation-a', ignored: true }];
  const loads = f.state.loads.length;
  for (const expectedState of bad) await assert.rejects(runtime.select('B', { expectedState }), /^Error: selection_changed$/);
  assert.equal(f.state.loads.length, loads); assert.equal(f.state.selection.selected, 'A');
  const result = await runtime.select('B', { expectedState: clone(f.state.selection) });
  assert.equal(result.profileId, 'B'); assert.equal(f.state.selection.selected, 'B');
});

test('guarded selection never adopts another generation while publishing its own commit', async () => {
  for (const manual of ['A', 'B']) {
    const f = fixture(), runtime = f.runtime();
    f.state.selection = { selected: 'A', selectedAt: 'generation-a' }; await runtime.snapshot();
    const expectedState = clone(f.state.selection);
    let replaced = false;
    f.seams.writeControl = async () => {
      if (!replaced && f.state.selection.selected === 'B') {
        replaced = true; f.state.selection = { selected: manual, selectedAt: 'human-generation' };
      }
    };
    await assert.rejects(runtime.select('B', { expectedState }), /^Error: selection_changed$/);
    assert.deepEqual(f.state.selection, { selected: manual, selectedAt: 'human-generation' });
  }
});

test('a persisted login intent survives service restart and imports by authenticated UUID', async () => {
  const f = fixture(); const first = f.runtime();
  await first.beginLogin({ sessionId: 's1', oauthState: 'state-1', oauthChallenge });
  await bind(first, 'B'); f.stage('B'); const result = await f.runtime().loginFinished();
  assert.equal(result.profileId, 'B'); assert.equal(result.deviceId, '2'.repeat(64));
  assert.equal(f.accounts.get('B').credentials.claudeAiOauth.refreshToken, 'invented-refresh-B');
  assert.equal(f.accounts.get('C').credentials.claudeAiOauth.accessToken, 'invented-access-C');
  assert.equal(f.state.imports.length, 1); assert.equal(f.state.control.intent, undefined);
  assert.equal(f.state.mirror.credentials.claudeAiOauth.refreshToken, undefined);
});

test('a newer external picker wins while an earlier login grant is being verified', async () => {
  const f = fixture(); const runtime = f.runtime();
  await runtime.beginLogin({ sessionId: 's1', oauthState: 'state-1', oauthChallenge });
  await bind(runtime, 'B'); f.stage('B');
  f.seams.verifyGrant = async () => { f.state.selection = { selected: 'C', selectedAt: 7 }; };
  const result = await runtime.snapshot();
  assert.equal(result.profileId, 'C'); assert.equal(f.state.selection.selected, 'C');
  assert.equal(f.state.imports[0].remote.accountUuid, 'account-B');
  assert.equal(f.state.mirror.profileId, 'C');
});

test('unknown unclaimed credentials fail closed without overwriting or importing them', async () => {
  const f = fixture(); const runtime = f.runtime(); await runtime.snapshot(); f.stage('B');
  await assert.rejects(runtime.snapshot(), /unclaimed_login_credentials/);
  assert.equal(f.state.mirror.credentials.claudeAiOauth.accessToken, 'invented-access-B');
  assert.equal(f.state.imports.length, 0); assert.equal(f.state.quarantined.length, 0);
});

test('explicit picker recovery preserves an unclaimed grant and restores access-only credentials', async () => {
  const f = fixture(); const runtime = f.runtime(); await runtime.snapshot(); f.stage('B');
  const result = await runtime.select('C');
  assert.equal(result.profileId, 'C'); assert.equal(f.state.imports.length, 0);
  assert.equal(f.state.quarantined[0].staged.credentials.claudeAiOauth.refreshToken, 'invented-refresh-B');
  assert.equal(f.state.mirror.credentials.claudeAiOauth.accessToken, 'invented-access-C');
  assert.equal(f.state.mirror.credentials.claudeAiOauth.refreshToken, undefined);
});

test('an expired intent can be recovered by a new explicit login without claiming the old grant', async () => {
  const f = fixture(); const runtime = f.runtime(); await runtime.beginLogin({ sessionId: 's1', oauthState: 'state-1', oauthChallenge });
  f.setNow(700_000); f.stage('B');
  await assert.rejects(runtime.snapshot(), /unclaimed_login_credentials/);
  const next = await runtime.beginLogin({ sessionId: 's2', oauthState: 'new-state' });
  assert.equal(f.state.quarantined.length, 1); assert.equal(f.state.imports.length, 0);
  assert.equal(f.state.mirror.profileId, 'A'); assert.equal(f.state.control.intent.id, next.id);
});

test('known stale account credentials are preserved and the current mirror is restored', async () => {
  const f = fixture(); const runtime = f.runtime(); await runtime.snapshot(); await runtime.select('B');
  f.stage('A'); const result = await runtime.snapshot();
  assert.equal(result.profileId, 'B'); assert.equal(f.state.mirror.profileId, 'B');
  assert.equal(f.state.quarantined[0].reason, 'stale_runtime_credentials');
  assert.equal(f.state.mirror.credentials.claudeAiOauth.refreshToken, undefined);
});

test('a claimed new refresh grant is imported even when the access token is reused', async () => {
  const f = fixture(); const runtime = f.runtime(); await runtime.beginLogin({ sessionId: 's1', oauthState: 'state-1', oauthChallenge });
  await bind(runtime, 'A');
  f.stage('A', grant('A', { refreshToken: 'invented-rotated-refresh-A' }));
  await runtime.loginFinished();
  assert.equal(f.state.imports.length, 1);
  assert.equal(f.accounts.get('A').credentials.claudeAiOauth.refreshToken, 'invented-rotated-refresh-A');
  assert.equal(f.state.mirror.credentials.claudeAiOauth.refreshToken, undefined);
});

test('maintenance during asynchronous login verification never authorizes a snapshot', async () => {
  const f = fixture(); const runtime = f.runtime(); await runtime.beginLogin({ sessionId: 's1', oauthState: 'state-1', oauthChallenge });
  await bind(runtime, 'B'); f.stage('B');
  f.seams.verifyGrant = async () => { f.state.selection.maintenance = true; };
  await assert.rejects(runtime.snapshot(), /runtime_unavailable/);
  assert.equal(f.state.selection.selected, 'A');
});

test('selection is rechecked after a native mirror write before publishing', async () => {
  const f = fixture(); const runtime = f.runtime(); let changed = false;
  f.seams.writeMirror = async () => {
    if (!changed) { changed = true; f.state.selection = { selected: 'B', selectedAt: 9 }; }
  };
  const result = await runtime.snapshot();
  assert.equal(result.profileId, 'B'); assert.equal(f.state.mirror.profileId, 'B');
});

test('expiring snapshots reload the verified vault without needing an adapter refresh predicate', async () => {
  const f = fixture(); const runtime = f.runtime(); await runtime.snapshot();
  f.setNow(950_001);
  f.accounts.get('A').credentials = grant('A-renewed', { expiresAt: 2_000_000 });
  const result = await runtime.snapshot();
  assert.equal(result.accessToken, 'invented-access-A-renewed'); assert.equal(result.expiresAt, 2_000_000);
});

test('one active OAuth dialog is enforced; the same session can restart with a new OAuth state', async () => {
  const f = fixture(); const runtime = f.runtime();
  const first = await runtime.beginLogin({ sessionId: 's1', oauthState: 'state-1', oauthChallenge });
  assert.deepEqual(await runtime.beginLogin({ sessionId: 's1', oauthState: 'state-1', oauthChallenge }), first);
  await assert.rejects(runtime.beginLogin({ sessionId: 's2', oauthState: 'state-2' }), /login_in_progress/);
  const second = await runtime.beginLogin({ sessionId: 's1', oauthState: 'state-2' });
  assert.notEqual(second.id, first.id); assert.equal(f.state.control.intent.oauthState, 'state-2');
  await runtime.select('B');
  await runtime.beginLogin({ sessionId: 's2', oauthState: 'state-3' });
  assert.equal(f.state.control.intent.selection.selected, 'B');
});

test('an active intent alone never authorizes an unobserved token', async () => {
  const f = fixture(); const runtime = f.runtime();
  await runtime.beginLogin({ sessionId: 's1', oauthState: 'state-1', oauthChallenge });
  f.stage('B');
  await assert.rejects(runtime.snapshot(), /unclaimed_login_credentials/);
  assert.equal(f.state.imports.length, 0);
  assert.equal(f.state.mirror.credentials.claudeAiOauth.accessToken, 'invented-access-B');
});

test('token exchange is bound to the browser state and PKCE challenge before and after HTTP', async () => {
  const f = fixture(); const runtime = f.runtime();
  const intent = await runtime.beginLogin({ sessionId: 's1', oauthState: 'state-1', oauthChallenge });
  assert.deepEqual(await runtime.validateLoginExchange({ oauthState: 'state-1', codeVerifier: verifier }),
    { intentId: intent.id });
  await assert.rejects(runtime.validateLoginExchange({ oauthState: 'wrong-state', codeVerifier: verifier }),
    /login_state_mismatch/);
  await assert.rejects(runtime.validateLoginExchange({ oauthState: 'state-1', codeVerifier: 'wrong-verifier' }),
    /login_pkce_mismatch/);
  await assert.rejects(runtime.observeLoginGrant({ oauthState: 'state-1', codeVerifier: 'wrong-verifier',
    accessToken: 'invented-access-B' }), /login_pkce_mismatch/);
  await bind(runtime, 'B');
  assert.equal(f.state.control.intent.allowedToken,
    createHash('sha256').update('invented-access-B').digest('hex'));
  assert.ok(!JSON.stringify(f.state.control).includes('invented-access-B'));
  await assert.rejects(bind(runtime, 'C'), /login_grant_conflict/);
});

test('late bound login data cannot reverse an explicit picker and survives restart', async () => {
  const f = fixture(); const runtime = f.runtime();
  await runtime.beginLogin({ sessionId: 's1', oauthState: 'state-1', oauthChallenge });
  await bind(runtime, 'B'); await runtime.select('C');
  assert.equal(runtime.status().loginPending, false);
  f.stage('B'); const afterRestart = await f.runtime().snapshot();
  assert.equal(afterRestart.profileId, 'C');
  assert.equal(f.state.imports.length, 1);
  assert.equal(f.accounts.get('B').credentials.claudeAiOauth.refreshToken, 'invented-refresh-B');
  assert.equal(f.state.control.boundGrants.length, 0);
});

test('a new login state rejects an old exchange but retains an already bound old grant only for import', async () => {
  const f = fixture(); const runtime = f.runtime();
  await runtime.beginLogin({ sessionId: 's1', oauthState: 'state-1', oauthChallenge });
  await bind(runtime, 'B'); await runtime.select('C');
  const newer = await runtime.beginLogin({ sessionId: 's2', oauthState: 'state-2', oauthChallenge });
  await assert.rejects(runtime.validateLoginExchange({ oauthState: 'state-1', codeVerifier: verifier }),
    /login_state_mismatch/);
  f.stage('B'); const result = await runtime.snapshot();
  assert.equal(result.profileId, 'C'); assert.equal(f.state.imports.length, 1);
  assert.equal(f.state.control.intent.id, newer.id);
  await bind(runtime, 'A', 'state-2'); f.stage('A');
  assert.equal((await runtime.snapshot()).profileId, 'A');
});

test('expired exchanges and malformed persisted control are rejected', async () => {
  const f = fixture(); const runtime = f.runtime();
  await runtime.beginLogin({ sessionId: 's1', oauthState: 'state-1', oauthChallenge });
  f.setNow(700_000);
  await assert.rejects(runtime.validateLoginExchange({ oauthState: 'state-1', codeVerifier: verifier }),
    /login_intent_expired/);
  f.state.control = { version: 1, knownTokens: 'invalid' };
  await assert.rejects(f.runtime().snapshot(), /runtime_control_invalid/);
});
