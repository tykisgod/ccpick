import { test } from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import { randomBytes } from 'node:crypto';
import { createProfile, getProfile, atomicJson, readJson, lease, childEnvironment, bindAccount } from '../core.mjs';
import { openAccountBrowser, finishInteractiveLogin, loginUrl } from '../browser.mjs';

const url = 'https://claude.com/oauth/authorize?client_id=fixture&state=private-state&code_challenge=fixture-challenge&redirect_uri=http%3A%2F%2Flocalhost%3A12345%2Fcallback';
async function fixture(t) {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), 'account-browser-test-'));
  t.after(() => fs.rm(root, { recursive: true, force: true }));
  const config = { dataRoot: path.join(root, 'data'), platform: process.platform, browser: '/house-browser', accountBrowser: '/ccpick' };
  await fs.mkdir(config.dataRoot, { mode: 0o700 });
  await atomicJson(path.join(root, 'state.json'), { version: 2, enabled: true });
  const p = await createProfile(config, { email: 'account-0087@example.com' }, { initialize: async (_, p) => {
    await atomicJson(path.join(p.configDirectory, '.claude.json'), {
      userID: randomBytes(32).toString('hex'), machineID: randomBytes(32).toString('hex') });
  } });
  const file = path.join(p.configDirectory, '.claude.json');
  await atomicJson(file, { ...await readJson(file), oauthAccount: { accountUuid: 'fixture-account', emailAddress: p.email } });
  await bindAccount(config, p, { loggedIn: true, uuid: 'fixture-account', email: p.email });
  const token = await lease(p, 'run'); await token.onSpawn(process.pid);
  t.after(() => token.release());
  const environment = { ...childEnvironment(config, p, 'http://127.0.0.1:1'), CCPICK_ACCOUNT_LEASE: token.id };
  const opened = [];
  const deps = { environment, network: async () => {}, open: async (exe, args) => { opened.push({ exe, args }); return { code: 0 }; } };
  return { root, config, p, file, token, environment, deps, opened };
}

test('slash login pins the originating account, blocks parallel work and preserves IDs', async t => {
  const f = await fixture(t);
  await atomicJson(path.join(f.root, 'state.json'), { version: 2, enabled: true, selected: 'some-other-account' });
  const before = await fs.readFile(f.file, 'utf8');
  await openAccountBrowser(f.config, url, f.deps);
  assert.equal(f.opened[0].exe, f.config.browser);
  const opened = new URL(f.opened[0].args[0]);
  assert.equal(opened.searchParams.get('login_hint'), 'account-0087@example.com');
  assert.equal(opened.searchParams.get('state'), 'private-state');
  await assert.rejects(lease(f.p, 'run'), /profile_busy/);
  await assert.rejects(lease(f.p, 'login'), /profile_busy/);
  await finishInteractiveLogin(f.config, f.environment);
  const parallel = await lease(f.p, 'run'); await parallel.release();
  assert.equal(await fs.readFile(f.file, 'utf8'), before, 'no credential or identity rewriting by the browser handoff');
});

test('another live session refuses login before opening a browser', async t => {
  const f = await fixture(t), other = await lease(f.p, 'run');
  try { await assert.rejects(openAccountBrowser(f.config, url, f.deps), /interactive_login_busy/); }
  finally { await other.release(); }
  assert.equal(f.opened.length, 0);
});

test('wrong account remains blocked; corrected re-login can finish', async t => {
  const f = await fixture(t);
  await openAccountBrowser(f.config, url, f.deps);
  const original = await readJson(f.file);
  await atomicJson(f.file, { ...original, oauthAccount: { accountUuid: 'wrong', emailAddress: 'account-0088@example.com' } });
  await assert.rejects(finishInteractiveLogin(f.config, f.environment), /wrong_account/);
  await assert.rejects(lease(f.p, 'run'), /profile_busy/);
  await openAccountBrowser(f.config, url, f.deps); // retry uses the registered account hint
  await atomicJson(f.file, original);
  await finishInteractiveLogin(f.config, f.environment);
});

test('cancellation or browser failure does not strand a valid original account', async t => {
  const f = await fixture(t);
  await openAccountBrowser(f.config, url, f.deps);
  await finishInteractiveLogin(f.config, f.environment); // guard after Esc
  await assert.rejects(openAccountBrowser(f.config, url, { ...f.deps, open: async () => ({ code: 1 }) }), /house_browser_not_ready/);
  const parallel = await lease(f.p, 'run'); await parallel.release();
});

test('browser handoff requires its own full preflight even after CLI startup', async t => {
  const f = await fixture(t);
  await assert.rejects(openAccountBrowser(f.config, url, { ...f.deps, network: async (_, options) => {
    assert.equal(options.checkOnly, true);
    assert.notEqual(options.requireBrowser, false);
    throw new Error('house_browser_not_ready');
  } }), /house_browser_not_ready/);
  assert.equal(f.opened.length, 0);
  const parallel = await lease(f.p, 'run'); await parallel.release();
});

test('missing session context, a foreign directory and changed IDs are refused', async t => {
  const f = await fixture(t);
  await assert.rejects(openAccountBrowser(f.config, url, { ...f.deps, environment: {} }), /managed_session_required/);
  await assert.rejects(openAccountBrowser(f.config, url, { ...f.deps, environment: { ...f.environment, CLAUDE_CONFIG_DIR: f.root } }), /profile_context_conflict/);
  await atomicJson(f.file, { ...await readJson(f.file), userID: 'f'.repeat(64) });
  await assert.rejects(openAccountBrowser(f.config, url, f.deps), /identity_changed/);
  assert.equal(f.opened.length, 0);
});

test('managed auth login and ordinary URLs do not change the interactive lease state', async t => {
  const f = await fixture(t);
  await f.token.release();
  const token = await lease(f.p, 'login'); await token.onSpawn(process.pid);
  try {
    const environment = { ...f.environment, CCPICK_ACCOUNT_LEASE: token.id };
    await openAccountBrowser(f.config, url, { ...f.deps, environment });
    assert.equal((await readJson(path.join(f.p.root, 'leases', token.id + '.json'))).interactiveLogin, undefined);
  } finally { await token.release(); }
  const run = await lease(f.p, 'run'); await run.onSpawn(process.pid);
  try {
    await openAccountBrowser(f.config, 'https://claude.com/docs', { ...f.deps, environment: { ...f.environment, CCPICK_ACCOUNT_LEASE: run.id } });
    const parallel = await lease(f.p, 'run'); await parallel.release();
  } finally { await run.release(); }
});

test('login URL accepts only known HTTPS hosts and preserves native OAuth parameters', () => {
  for (const bad of ['http://claude.com/oauth/authorize', 'https://claude.com.evil.test/oauth/authorize',
    'https://account-0089@example.com/oauth/authorize', url + '&state=duplicate', 'https://claude.com/oauth/authorize']) {
    assert.throws(() => loginUrl(bad, 'account-0087@example.com'), /invalid_login_url/);
  }
  assert.equal(loginUrl(url, 'account-0087@example.com').oauth, true);
  assert.equal(loginUrl(url.replace('/oauth/', '/cai/oauth/'), 'account-0087@example.com').oauth, true);
  assert.throws(() => loginUrl(url.replace('/oauth/', '/new-path/oauth/'), 'account-0087@example.com'), /invalid_login_url/);
  assert.throws(() => loginUrl(url.replace('claude.com/', 'platform.claude.com/'), 'account-0087@example.com'), /unsupported_login_method/);
});
