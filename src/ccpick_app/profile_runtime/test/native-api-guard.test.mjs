import test from 'node:test';
import assert from 'node:assert/strict';
import http from 'node:http';
import https from 'node:https';
import net from 'node:net';
import tls from 'node:tls';
import { once } from 'node:events';
import { createNativeApiGuard } from '../native-api-guard.mjs';

import fs from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import { after } from 'node:test';
import { runtimeCertificate } from '../runtime-certificate.mjs';
const certificateRoot = await fs.mkdtemp(path.join(os.tmpdir(), 'ccpick-guard-test-'));
if (process.platform !== 'win32') await fs.chmod(certificateRoot, 0o700);
const fixture = await runtimeCertificate(certificateRoot);
after(() => fs.rm(certificateRoot, { recursive: true, force: true }));
const KEY = 'LOCAL-TEST-ONLY-RUNTIME-CHANNEL-SECRET';
const TOKEN_A = 'Bearer INVENTED-ACCOUNT-A';
const TOKEN_B = 'Bearer INVENTED-ACCOUNT-B';
const listen = async server => { server.listen(0, '127.0.0.1'); await once(server, 'listening'); return server.address().port; };

async function setup(t, options = {}) {
  const sockets = new Set(), connects = [], apiCalls = [], modelCalls = [];
  const track = socket => { sockets.add(socket); socket.on('error', () => {}); socket.once('close', () => sockets.delete(socket)); };
  const selector = http.createServer((_req, res) => { res.writeHead(405); res.end(); });
  selector.on('connection', track);
  selector.on('connect', (req, socket, head) => {
    connects.push({ target: req.url, headers: req.headers });
    if (options.onConnect) options.onConnect(req, socket, head, track);
    else socket.end('HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\n\r\n');
  });
  const selectorPort = await listen(selector);
  const guard = createNativeApiGuard({ tls: fixture, upstreamProxy: `http://127.0.0.1:${selectorPort}`, localKey: KEY,
    connectTimeoutMs: 1000,
    resolveProxy: options.resolveProxy,
    scope: options.scope,
    oauthHandler: options.oauthHandler,
    messagesHandler: options.messagesHandler ?? (async (req, res) => {
      let body = ''; for await (const bytes of req) body += bytes;
      modelCalls.push({ path: req.url, headers: { ...req.headers }, body });
      res.writeHead(200, { 'content-type': 'text/event-stream' });
      res.end('event: message_stop\ndata: {"type":"message_stop"}\n\n');
    }),
    ...(options.realTransport ? {} : { apiHandler: options.apiHandler ?? (async (request, res) => {
      apiCalls.push(request); res.writeHead(200, { 'content-type': 'application/json' }); res.end('{"ok":true}');
    }) }),
  });
  await guard.listen();
  t.after(async () => {
    await guard.close();
    for (const socket of sockets) socket.destroy();
    await new Promise(resolve => selector.close(resolve));
  });
  return { guard, selector, selectorPort, connects, apiCalls, modelCalls };
}

function tunnel(port, target = 'api.anthropic.com:443', headers = {}) {
  return new Promise((resolve, reject) => {
    const req = http.request({ host: '127.0.0.1', port, method: 'CONNECT', path: target,
      headers: { host: target, ...headers }, agent: false });
    req.on('error', reject);
    req.on('connect', (res, socket, head) => {
      if (res.statusCode === 200) { assert.equal(head.length, 0); resolve({ socket, status: 200 }); return; }
      const chunks = [head];
      socket.on('data', chunk => chunks.push(chunk));
      socket.on('error', reject);
      socket.on('end', () => resolve({ status: res.statusCode, body: Buffer.concat(chunks).toString() }));
    });
    req.end();
  });
}

async function request(guard, path, { method = 'GET', headers = {}, body = '', servername = 'api.anthropic.com' } = {}) {
  const result = await tunnel(guard.address().port, `${servername}:443`);
  assert.equal(result.status, 200);
  const secure = tls.connect({ socket: result.socket, servername, ca: fixture.cert, rejectUnauthorized: true, ALPNProtocols: ['http/1.1'] });
  await once(secure, 'secureConnect');
  const agent = new https.Agent({ keepAlive: false });
  agent.createConnection = (_options, callback) => callback(null, secure);
  try {
    return await new Promise((resolve, reject) => {
      const req = https.request({ hostname: servername, port: 443, path, method,
        headers: { host: servername, ...(body ? { 'content-length': Buffer.byteLength(body) } : {}), ...headers }, agent }, res => {
        const chunks = [];
        res.on('data', chunk => chunks.push(chunk)); res.on('error', reject);
        res.on('end', () => resolve({ status: res.statusCode, headers: res.headers, body: Buffer.concat(chunks).toString() }));
      });
      req.on('error', reject); req.end(body);
    });
  } finally { agent.destroy(); secure.destroy(); }
}

test('official API model routes keep the complete stream and local broker key', async t => {
  const { guard, modelCalls, apiCalls, connects } = await setup(t);
  for (const path of ['/v1/messages?beta=true', '/v1/messages/count_tokens']) {
    const result = await request(guard, path, { method: 'POST', body: '{"unchanged":9007199254740993}',
      headers: { [ 'x-ccpick-account-runtime' ]: KEY, authorization: 'Bearer ' + KEY, 'content-type': 'application/json' } });
    assert.equal(result.status, 200); assert.match(result.body, /message_stop/);
  }
  assert.equal(modelCalls.length, 2);
  assert.equal(modelCalls[0].headers['x-ccpick-account-runtime'], KEY);
  assert.equal(modelCalls[0].headers.authorization, 'Bearer ' + KEY);
  assert.equal(modelCalls[0].body, '{"unchanged":9007199254740993}');
  assert.equal(apiCalls.length, 0); assert.equal(connects.length, 0);
});

test('both model routes require local key before broker sees body', async t => {
  const { guard, modelCalls } = await setup(t);
  for (const path of ['/v1/messages', '/v1/messages/count_tokens']) {
    for (const key of [undefined, 'wrong']) {
      const result = await request(guard, path, { method: 'POST', headers: key ? { 'x-ccpick-account-runtime': key } : {}, body: '{}' });
      assert.equal(result.status, 403); assert.match(result.body, /model_local_key_required/);
    }
  }
  assert.equal(modelCalls.length, 0);
  const bearerOnly = await request(guard, '/v1/messages', { method: 'POST', body: '{}', headers: { authorization: 'Bearer ' + KEY } });
  assert.equal(bearerOnly.status, 200); assert.equal(modelCalls.length, 1);
});

test('profile and login validation preserve actual bearer and remove local/custom metadata', async t => {
  const { guard, apiCalls } = await setup(t);
  for (const token of [TOKEN_A, TOKEN_B]) {
    assert.equal((await request(guard, '/api/oauth/profile', { headers: { authorization: token,
      'x-ccpick-account-runtime': KEY, 'x-unknown-native-metadata': 'untrusted', 'x-forwarded-for': '192.0.2.1' } })).status, 200);
    assert.equal((await request(guard, '/api/oauth/validate', { method: 'POST', body: 'null',
      headers: { authorization: token, 'content-type': 'application/json' } })).status, 200);
  }
  assert.deepEqual(apiCalls.map(call => call.headers.authorization), [TOKEN_A, TOKEN_A, TOKEN_B, TOKEN_B]);
  for (const call of apiCalls) {
    assert.equal(call.headers['x-ccpick-account-runtime'], undefined);
    assert.equal(call.headers['x-unknown-native-metadata'], undefined);
    assert.equal(call.headers['x-forwarded-for'], undefined);
  }
  assert.equal(apiCalls[1].body.toString(), 'null');
});

test('known bearer-only and anonymous native support routes are admitted', async t => {
  const { guard, apiCalls } = await setup(t);
  for (const path of ['/api/oauth/claude_cli/roles', '/api/oauth/usage?at_wall=1&skip_spend=1',
    '/api/oauth/usage?cedar_ember=1&skip_spend=1', '/api/claude_code/organizations/metrics_enabled',
    '/api/claude_code/policy_limits']) {
    assert.equal((await request(guard, path, { headers: { authorization: TOKEN_A } })).status, 200);
  }
  for (const path of ['/api/hello', '/api/web/domain_info?domain=docs.example.com']) {
    assert.equal((await request(guard, path)).status, 200);
  }
  assert.equal(apiCalls.length, 7);
});

test('nonmodel identity, channel credential, query and request body mismatches fail closed', async t => {
  const { guard, apiCalls } = await setup(t);
  const cases = [
    ['/api/oauth/profile', { headers: { authorization: 'Bearer ' + KEY } }, 'api_local_credential_rejected'],
    ['/api/oauth/profile', { headers: { authorization: TOKEN_A, 'x-organization-uuid': 'old-org' } }, 'api_identity_header_unsupported'],
    ['/api/oauth/profile', { headers: { authorization: TOKEN_A, 'x-cc-atis': 'old-atis' } }, 'api_identity_header_unsupported'],
    ['/api/oauth/profile?account_uuid=old', { headers: { authorization: TOKEN_A } }, 'api_query_unsupported'],
    ['/api/oauth/usage?skip_spend=1&skip_spend=1', { headers: { authorization: TOKEN_A } }, 'api_query_unsupported'],
    ['/api/oauth/profile', { headers: { authorization: TOKEN_A }, body: '{}' }, 'api_body_unsupported'],
    ['/api/oauth/validate', { method: 'POST', headers: { authorization: TOKEN_A }, body: '{"device_id":"old"}' }, 'api_body_unsupported'],
    ['/api/web/domain_info?domain=example.com&account=old', {}, 'api_query_unsupported'],
    ['/api/oauth/profile', {}, 'api_bearer_required'],
  ];
  for (const [path, options, reason] of cases) {
    const result = await request(guard, path, options);
    assert.ok(result.status >= 400); assert.match(result.body, new RegExp(reason));
    assert.ok(!result.body.includes(KEY)); assert.ok(!result.body.includes(TOKEN_A));
  }
  assert.equal(apiCalls.length, 0);
});

test('cloud sessions, hosted connectors, voice and unknown API routes never leave the guard', async t => {
  const { guard, apiCalls, modelCalls, connects } = await setup(t);
  for (const [path, reason] of [
    ['/v1/mcp_servers?limit=1000&include_additional_installs=true', 'hosted_mcp_account_switch_unsupported'],
    ['/v1/code/sessions/old-id', 'cloud_session_account_switch_unsupported'],
    ['/api/oauth/organizations/old-org/code/repos/a/b', 'cloud_session_account_switch_unsupported'],
    ['/v1/environment_providers', 'cloud_session_account_switch_unsupported'],
    ['/api/ws/speech_to_text/voice_stream', 'voice_account_switch_unsupported'],
    ['/api/future/account-bound-feature', 'api_route_unsupported'],
    ['/v1/../api/oauth/profile', 'api_path_invalid'],
    ['/api/oauth/%70rofile', 'api_path_invalid'],
    ['https://api.anthropic.com/api/oauth/profile', 'api_path_invalid'],
    ['/api/oauth/profile', 'api_route_unsupported'],
    ['/v1/messages?account_uuid=old', 'api_query_unsupported'],
  ]) {
    const result = await request(guard, path, { method: 'POST', body: '{}', headers: { authorization: TOKEN_A } });
    assert.equal(result.status, 403); assert.match(result.body, new RegExp(reason));
  }
  assert.equal(apiCalls.length, 0); assert.equal(modelCalls.length, 0); assert.equal(connects.length, 0);
  const hosted = await tunnel(guard.address().port, 'mcp-proxy.anthropic.com:443');
  assert.equal(hosted.status, 403); assert.match(hosted.body, /hosted_mcp_account_switch_unsupported/);
  assert.equal(connects.length, 0);
});

test('generic HTTPS uses selector CONNECT exclusively and preserves tunnel bytes', async t => {
  const { guard, connects } = await setup(t, { onConnect: (_req, socket) => {
    socket.write('HTTP/1.1 200 Connection Established\r\n\r\n'); socket.on('data', bytes => socket.write(bytes));
  } });
  const { socket, status } = await tunnel(guard.address().port, 'downloads.example.invalid:443', {
    'proxy-authorization': 'MUST-NOT-REACH-SELECTOR', 'x-ccpick-account-runtime': KEY });
  assert.equal(status, 200);
  const received = once(socket, 'data'); socket.write('unchanged-tunnel-bytes');
  assert.equal((await received)[0].toString(), 'unchanged-tunnel-bytes'); socket.destroy();
  assert.equal(connects.length, 1); assert.equal(connects[0].target, 'downloads.example.invalid:443');
  assert.equal(connects[0].headers['proxy-authorization'], undefined);
  assert.equal(connects[0].headers['x-ccpick-account-runtime'], undefined);
});

test('CONNECT rejects unsafe authorities/ports and selector refusal never falls back', async t => {
  const { guard, connects } = await setup(t);
  for (const target of ['api.anthropic.com:80', 'api.anthropic.com:8443', 'account-0091@example.com:443',
    'api.anthropic.com:443/path', 'api.anthropic.com']) {
    const result = await tunnel(guard.address().port, target);
    assert.equal(result.status, 403); assert.match(result.body, /connect_target_rejected/);
  }
  const result = await tunnel(guard.address().port, 'download.example.invalid:443');
  assert.equal(result.status, 502); assert.match(result.body, /selector_connect_failed/);
  assert.equal(connects.length, 1);
});

test('default API transport does not trust local interception CA as upstream authority', async t => {
  let apiReceived = 0;
  const sockets = new Set();
  const upstream = https.createServer(fixture, (_req, res) => { apiReceived++; res.end('{}'); });
  upstream.on('connection', socket => { sockets.add(socket); socket.on('error', () => {}); });
  upstream.on('tlsClientError', () => {});
  const port = await listen(upstream);
  t.after(async () => { for (const socket of sockets) socket.destroy(); await new Promise(resolve => upstream.close(resolve)); });
  const { guard, connects } = await setup(t, { realTransport: true, onConnect: (_req, socket, head, track) => {
    const destination = net.connect(port, '127.0.0.1'); track(destination);
    destination.once('connect', () => { socket.write('HTTP/1.1 200 Connection Established\r\n\r\n'); if (head.length) destination.write(head); socket.pipe(destination); destination.pipe(socket); });
    destination.once('close', () => socket.destroy()); socket.once('close', () => destination.destroy());
  } });
  const result = await request(guard, '/api/oauth/profile', { headers: { authorization: TOKEN_A } });
  assert.equal(result.status, 502); assert.match(result.body, /api_upstream_unavailable/);
  assert.equal(apiReceived, 0); assert.equal(connects.length, 1); assert.equal(connects[0].target, 'api.anthropic.com:443');
});

test('handler errors cannot print credentials and model dispatch is never retried', async t => {
  let called = 0;
  const { guard } = await setup(t, { messagesHandler: () => { called++; throw new Error(TOKEN_A + KEY); },
    apiHandler: () => { throw new Error(TOKEN_B + KEY); } });
  const model = await request(guard, '/v1/messages', { method: 'POST', body: '{}', headers: { 'x-ccpick-account-runtime': KEY } });
  assert.equal(model.status, 502); assert.match(model.body, /model_handler_failed/); assert.equal(called, 1);
  const profile = await request(guard, '/api/oauth/profile', { headers: { authorization: TOKEN_B } });
  assert.equal(profile.status, 502); assert.match(profile.body, /api_upstream_unavailable/);
  for (const body of [model.body, profile.body]) { assert.ok(!body.includes('INVENTED')); assert.ok(!body.includes(KEY)); }
});

test('API origin changes and WebSocket upgrades are rejected locally', async t => {
  const { guard, apiCalls, modelCalls, connects } = await setup(t);
  const wrongHost = await request(guard, '/api/oauth/profile', { headers: { host: 'other.invalid', authorization: TOKEN_A } });
  assert.equal(wrongHost.status, 403); assert.match(wrongHost.body, /api_origin_rejected/);
  const browser = await request(guard, '/api/oauth/profile', { headers: { origin: 'https://untrusted.invalid', authorization: TOKEN_A } });
  assert.equal(browser.status, 403); assert.match(browser.body, /api_origin_rejected/);
  const upgrade = await request(guard, '/api/ws/speech_to_text/voice_stream', {
    headers: { connection: 'Upgrade', upgrade: 'websocket', authorization: TOKEN_A } });
  assert.equal(upgrade.status, 403); assert.match(upgrade.body, /voice_account_switch_unsupported/);
  assert.equal(apiCalls.length, 0); assert.equal(modelCalls.length, 0); assert.equal(connects.length, 0);
});

test('configuration rejects nonlocal/nonHTTP selectors and publicly bound listener', async t => {
  for (const proxy of ['http://proxy.example:8080', 'https://127.0.0.1:8080', 'http://user:secret@127.0.0.1:8080',
    'http://127.0.0.1:8080/path', undefined]) {
    assert.throws(() => createNativeApiGuard({ tls: fixture, upstreamProxy: proxy, messagesHandler: () => {} }), /invalid_guard_selector/);
  }
  const { guard } = await setup(t);
  await assert.rejects(guard.listen(0, '0.0.0.0'), /guard_not_startable/);
  assert.throws(() => createNativeApiGuard({ tls: { cert: 'invalid', key: 'invalid' },
    upstreamProxy: 'http://127.0.0.1:8080', messagesHandler: () => {} }), /invalid_guard_tls_identity/);
});

test('platform token grants require an explicit coordinator; other routes and local auth never delegate', async t => {
  const without = await setup(t);
  const blocked = await tunnel(without.guard.address().port, 'platform.claude.com:443');
  assert.equal(blocked.status, 403); assert.match(blocked.body, /oauth_login_handler_required/);
  assert.equal(without.connects.length, 0);
  const grants = [];
  const { guard, connects } = await setup(t, { oauthHandler: async (req, res) => {
    let body = ''; for await (const bytes of req) body += bytes;
    grants.push({ path: req.url, headers: req.headers, body });
    res.writeHead(200, { 'content-type': 'application/json' }); res.end('{"grant":"fixture"}');
  } });
  const opts = { servername: 'platform.claude.com', method: 'POST', body: '{"state":"invented"}' };
  const accepted = await request(guard, '/v1/oauth/token', { ...opts, headers: { 'x-ccpick-account-runtime': KEY } });
  assert.equal(accepted.status, 200); assert.equal(grants.length, 1);
  assert.equal(grants[0].body, opts.body); assert.equal(grants[0].headers['x-ccpick-account-runtime'], undefined);
  for (const [path, extra, reason] of [
    ['/v1/oauth/token?state=old', {}, 'oauth_route_unsupported'],
    ['/v1/oauth/token', { method: 'GET' }, 'oauth_route_unsupported'],
    ['/v1/oauth/other', {}, 'oauth_route_unsupported'],
    ['/v1/oauth/token', { headers: { authorization: 'Bearer ' + KEY } }, 'oauth_authorization_unsupported'],
    ['/v1/oauth/token', { headers: { 'x-organization-uuid': 'old' } }, 'oauth_authorization_unsupported'],
  ]) {
    const result = await request(guard, path, { ...opts, ...extra });
    assert.equal(result.status, 403); assert.match(result.body, new RegExp(reason));
  }
  assert.equal(grants.length, 1); assert.equal(connects.length, 0);
});

test('API routing uses the actual bearer and freezes the route before dispatch', async t => {
  const started = Promise.withResolvers(), release = Promise.withResolvers();
  const routeA = { proxy: 'http://127.0.0.1:12808', revision: 'route-a-1' };
  const routeB = { proxy: 'http://127.0.0.1:13808', revision: 'route-b-1' };
  const calls = [], resolutions = [];
  const { guard } = await setup(t, { resolveProxy: input => {
    resolutions.push(input);
    assert.equal(input.kind, 'api'); assert.equal(input.hostname, 'api.anthropic.com');
    assert.ok(Object.isFrozen(input)); assert.ok(Object.isFrozen(input.headers));
    assert.equal(input.headers['x-ccpick-account-runtime'], undefined);
    return input.headers.authorization === TOKEN_A ? routeA : routeB;
  }, apiHandler: async (input, res) => {
    calls.push(input);
    if (input.headers.authorization === TOKEN_A) { started.resolve(); await release.promise; }
    res.end('{}');
  } });
  const first = request(guard, '/api/oauth/profile', { headers: { authorization: TOKEN_A } });
  await started.promise;
  routeA.proxy = routeB.proxy; routeA.revision = 'mutated-after-admission';
  assert.equal(guard.invalidateTunnels(), 0);
  assert.equal((await request(guard, '/api/oauth/usage', { headers: { authorization: TOKEN_B } })).status, 200);
  release.resolve(); assert.equal((await first).status, 200);
  assert.deepEqual(calls.map(input => [input.headers.authorization, input.proxy, input.revision]), [
    [TOKEN_A, 'http://127.0.0.1:12808/', 'route-a-1'], [TOKEN_B, 'http://127.0.0.1:13808/', 'route-b-1'],
  ]);
  assert.equal(resolutions.length, 2);
});

test('route resolution errors and nonlocal proxies fail closed without fallback or secret disclosure', async t => {
  for (const resolveProxy of [() => { throw new Error(TOKEN_A + KEY + 'http://secret.invalid'); },
    () => ({ proxy: 'http://secret.invalid:1234' }),
    () => ({ proxy: 'http://127.0.0.1:1234', revision: { private: TOKEN_A } })]) {
    const { guard, apiCalls, connects } = await setup(t, { resolveProxy });
    const api = await request(guard, '/api/oauth/profile', { headers: { authorization: TOKEN_A } });
    assert.equal(api.status, 502); assert.match(api.body, /api_upstream_unavailable/);
    const connect = await tunnel(guard.address().port, 'statsig.anthropic.com:443');
    assert.equal(connect.status, 502); assert.match(connect.body, /selector_connect_failed/);
    for (const value of [api.body, connect.body]) {
      assert.ok(!value.includes('INVENTED')); assert.ok(!value.includes(KEY)); assert.ok(!value.includes('secret.invalid'));
    }
    assert.equal(apiCalls.length, 0); assert.equal(connects.length, 0);
  }
});

test('scope guard binds model headers while bearer-only native APIs keep their actual account route', async t => {
  const { guard, modelCalls, apiCalls } = await setup(t, { scope: 'account-a', resolveProxy: input => {
    assert.equal(input.kind, 'api'); assert.equal(input.headers.authorization, TOKEN_A);
    return { proxy: 'http://127.0.0.1:12808', revision: 'actual-account-a' };
  } });
  for (const supplied of [undefined, 'default', 'account-b']) {
    const headers = { authorization: TOKEN_A, ...(supplied ? { 'x-ccpick-account-scope': supplied } : {}) };
    if (supplied) {
      const api = await request(guard, '/api/oauth/profile', { headers });
      assert.equal(api.status, 403); assert.match(api.body, /api_scope_rejected/);
    }
    const model = await request(guard, '/v1/messages', { method: 'POST', body: '{}',
      headers: { ...headers, 'x-ccpick-account-runtime': KEY } });
    assert.equal(model.status, 403); assert.match(model.body, /api_scope_rejected/);
  }
  assert.equal(modelCalls.length, 0); assert.equal(apiCalls.length, 0);
  assert.equal((await request(guard, '/api/oauth/usage', { headers: { authorization: TOKEN_A } })).status, 200);
  assert.equal(apiCalls[0].proxy, 'http://127.0.0.1:12808/'); assert.equal(apiCalls[0].revision, 'actual-account-a');
  assert.equal((await request(guard, '/api/oauth/profile', { headers: {
    authorization: TOKEN_A, 'x-ccpick-account-scope': 'account-a' } })).status, 200);
  const compatible = await setup(t, { scope: 'default' });
  assert.equal((await request(compatible.guard, '/api/oauth/profile', { headers: { authorization: TOKEN_A } })).status, 200);
  assert.equal((await request(compatible.guard, '/api/oauth/profile', { headers: {
    authorization: TOKEN_A, 'x-ccpick-account-scope': 'account-a' } })).status, 403);
});

test('OAuth uses PKCE scope ownership without requiring model custom headers', async t => {
  let grants = 0;
  const { guard } = await setup(t, { scope: 'account-a', oauthHandler: async (_req, res) => { grants++; res.end('{}'); } });
  const options = { servername: 'platform.claude.com', method: 'POST', body: '{}' };
  assert.equal((await request(guard, '/v1/oauth/token', options)).status, 200);
  const refused = await request(guard, '/v1/oauth/token', { ...options, headers: { 'x-ccpick-account-scope': 'account-b' } });
  assert.equal(refused.status, 403); assert.match(refused.body, /oauth_scope_rejected/); assert.equal(grants, 1);
});

test('selection invalidates protected passthrough only; new tunnels use the new proxy', async t => {
  const alternativeSockets = new Set(), alternativeTargets = [];
  const alternative = http.createServer();
  alternative.on('connection', socket => { alternativeSockets.add(socket); socket.on('error', () => {});
    socket.once('close', () => alternativeSockets.delete(socket)); });
  alternative.on('connect', (req, socket) => {
    alternativeTargets.push(req.url); socket.write('HTTP/1.1 200 Connection Established\r\n\r\n');
    socket.on('data', bytes => socket.write(bytes));
  });
  const alternativePort = await listen(alternative);
  t.after(async () => { for (const socket of alternativeSockets) socket.destroy(); await new Promise(resolve => alternative.close(resolve)); });
  let selectedProxy;
  const resolutions = [];
  const { guard, connects, selectorPort } = await setup(t, { resolveProxy: input => {
    resolutions.push(input); assert.equal(input.kind, 'tunnel'); assert.equal(input.headers, undefined);
    return { proxy: selectedProxy, revision: selectedProxy };
  }, onConnect: (_req, socket) => {
    socket.write('HTTP/1.1 200 Connection Established\r\n\r\n'); socket.on('data', bytes => socket.write(bytes));
  } });
  selectedProxy = `http://127.0.0.1:${selectorPort}`;
  const protectedSockets = [];
  for (const host of ['statsig.anthropic.com', 'claude.ai', 'downloads.claude.com', 'claude.app',
    'bridge.claudeusercontent.com', 'claudemcpcontent.com'])
    protectedSockets.push((await tunnel(guard.address().port, `${host}:443`)).socket);
  const preserved = [];
  for (const host of ['api.openai.com', 'downloads.example.invalid', 'anthropic.com.example.invalid'])
    preserved.push((await tunnel(guard.address().port, `${host}:443`)).socket);
  const closed = protectedSockets.map(socket => once(socket, 'close'));
  selectedProxy = `http://127.0.0.1:${alternativePort}`;
  assert.equal(guard.invalidateTunnels(), 6); await Promise.all(closed);
  for (const socket of preserved) {
    const echoed = once(socket, 'data'); socket.write('ordinary-still-alive');
    assert.equal((await echoed)[0].toString(), 'ordinary-still-alive');
  }
  const next = (await tunnel(guard.address().port, 'statsig.anthropic.com:443')).socket;
  const echoed = once(next, 'data'); next.write('next-household');
  assert.equal((await echoed)[0].toString(), 'next-household');
  assert.deepEqual(alternativeTargets, ['statsig.anthropic.com:443']); assert.equal(connects.length, 9);
  assert.equal(resolutions.length, 10);
  for (const socket of [...preserved, next]) socket.destroy();
});

test('invalidation cancels a protected route resolver before any upstream CONNECT', async t => {
  const started = Promise.withResolvers(), route = Promise.withResolvers(); let resolveCalls = 0;
  const { guard, connects, selectorPort } = await setup(t, { resolveProxy: input => {
    resolveCalls++; started.resolve(input); return route.promise;
  } });
  const pending = tunnel(guard.address().port, 'statsig.anthropic.com:443').then(() => 'connected', () => 'closed');
  const input = await started.promise;
  assert.equal(input.signal.aborted, false); assert.equal(guard.invalidateTunnels(), 1);
  assert.equal(await pending, 'closed'); assert.equal(input.signal.aborted, true);
  route.resolve({ proxy: `http://127.0.0.1:${selectorPort}`, revision: 1 });
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(connects.length, 0); assert.equal(resolveCalls, 1);
});

test('invalidation cancels an already dispatched protected CONNECT before late acceptance', async t => {
  const entered = Promise.withResolvers();
  const { guard, connects } = await setup(t, { onConnect: (_req, socket) => entered.resolve(socket) });
  const pending = tunnel(guard.address().port, 'statsig.anthropic.com:443').then(() => 'connected', () => 'closed');
  const upstream = await entered.promise;
  assert.equal(connects.length, 1); assert.equal(guard.invalidateTunnels(), 1);
  assert.equal(await pending, 'closed');
  if (!upstream.destroyed) upstream.write('HTTP/1.1 200 Connection Established\r\n\r\n');
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(guard.status().tunneled, 0); assert.equal(connects.length, 1);
});

test('protected tunnel invalidation leaves an active intercepted model stream intact', async t => {
  const entered = Promise.withResolvers(), release = Promise.withResolvers();
  const { guard } = await setup(t, { messagesHandler: async (_req, res) => {
    res.writeHead(200, { 'content-type': 'text/event-stream' }); res.write('data: before-switch\n\n');
    entered.resolve(); await release.promise; res.end('data: after-switch\n\n');
  } });
  const pending = request(guard, '/v1/messages', { method: 'POST', body: '{}', headers: { 'x-ccpick-account-runtime': KEY } });
  await entered.promise; assert.equal(guard.invalidateTunnels(), 0); release.resolve();
  const result = await pending;
  assert.equal(result.status, 200); assert.equal(result.body, 'data: before-switch\n\ndata: after-switch\n\n');
});
