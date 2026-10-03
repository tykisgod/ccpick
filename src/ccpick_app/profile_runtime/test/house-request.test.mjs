import test from 'node:test';
import assert from 'node:assert/strict';
import http from 'node:http';
import https from 'node:https';
import tls from 'node:tls';
import net from 'node:net';
import fs from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import { once } from 'node:events';
import { PassThrough, Writable } from 'node:stream';
import { setTimeout as delay } from 'node:timers/promises';
import { houseJson, authRetryAt } from '../house-request.mjs';
import { runtimeCertificate } from '../runtime-certificate.mjs';

const token = 'INVENTED-HOUSE-ACCESS-NOT-REAL';
test('429 deadlines preserve the 30 minute minimum and longer seconds or HTTP dates', () => {
  const now = Date.parse('2026-10-02T00:00:00Z');
  for (const value of [undefined, '', 'bad', '-1', '5', 'Infinity', '1e1000'])
    assert.equal(authRetryAt(value, now), now + 1_800_000);
  assert.equal(authRetryAt('7200', now), now + 7_200_000);
  assert.equal(authRetryAt('Fri, 02 Oct 2026 03:00:00 GMT', now), now + 10_800_000);
});

test('house 429 exposes only its stable reason/status and the longer absolute retry deadline', async t => {
  const now = Date.now(), f = await fake(t, { responseStatus: 429, headers: { 'retry-after': '7200' },
    payload: 'INVENTED PRIVATE RESPONSE' });
  await assert.rejects(houseJson(f.proxy, 'profile', { token }), error => {
    assert.equal(error.message, 'auth_rate_limited'); assert.equal(error.status, 429);
    assert(error.retryAt >= now + 7_200_000 && error.retryAt <= Date.now() + 7_200_000);
    assert(!JSON.stringify(error).includes('PRIVATE')); return true;
  });
  assert.equal(f.calls.length, 1);
});
async function until(predicate) {
  const deadline = Date.now() + 2000;
  while (!predicate()) { assert(Date.now() < deadline, 'fixture condition timed out'); await delay(5); }
}
async function directory(t) {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), 'ccpick-house-test-'));
  if (process.platform !== 'win32') await fs.chmod(root, 0o700);
  t.after(() => fs.rm(root, { recursive: true, force: true })); return root;
}
async function fake(t, { connectStatus = 200, responseStatus = 200, payload = '{"ok":true}', delayedTLS = false,
  tlsError = false, bodyFailure = false, headers = {} } = {}) {
  const connects = [], handshakes = [], calls = [], sockets = new Set();
  const proxy = http.createServer();
  proxy.on('connect', (request, socket) => {
    connects.push({ path: request.url, headers: request.headers }); sockets.add(socket);
    socket.on('error', () => {}); socket.once('end', () => socket.destroy()); socket.once('close', () => sockets.delete(socket));
    socket.write(`HTTP/1.1 ${connectStatus} Fixture\r\n\r\n`);
  });
  proxy.listen(0, '127.0.0.1'); await once(proxy, 'listening');
  t.mock.method(tls, 'connect', options => {
    const secure = new PassThrough(); secure.authorized = true; secure.encrypted = true;
    secure.once('close', () => options.socket.destroy()); handshakes.push({ options, secure });
    if (!delayedTLS) queueMicrotask(() => tlsError ? secure.destroy(new Error('synthetic_tls_error')) : secure.emit('secureConnect'));
    return secure;
  });
  t.mock.method(https, 'request', (options, callback) => {
    const chunks = [];
    const request = new Writable({ write(chunk, _, done) { chunks.push(chunk); done(); } });
    request.once('finish', () => {
      calls.push({ options, body: Buffer.concat(chunks) });
      const response = new PassThrough(); response.statusCode = responseStatus; response.headers = headers;
      callback(response);
      if (bodyFailure) { response.emit('aborted'); response.destroy(); }
      else { response.complete = true; response.end(payload); }
    });
    return request;
  });
  t.after(async () => { for (const { secure } of handshakes) secure.destroy(); for (const socket of sockets) socket.destroy();
    await new Promise(resolve => proxy.close(resolve)); });
  return { proxy: `http://127.0.0.1:${proxy.address().port}`, connects, handshakes, calls };
}

test('known account endpoints have fixed CONNECT targets, methods and body serialization', async t => {
  const f = await fake(t);
  assert.deepEqual(await houseJson(f.proxy, 'profile', { token }), { ok: true });
  const grant = { grant_type: 'refresh_token', refresh_token: 'INVENTED-REFRESH-NOT-REAL', client_id: 'fixture' };
  await houseJson(f.proxy, 'refresh', { body: grant });
  await houseJson(f.proxy, 'login', { body: { grant_type: 'authorization_code', code: 'INVENTED-CODE' } });
  assert.deepEqual(f.connects.map(value => value.path), ['api.anthropic.com:443', 'platform.claude.com:443', 'platform.claude.com:443']);
  assert(f.connects.every(value => value.headers.authorization === undefined && value.headers['proxy-authorization'] === undefined));
  assert.equal(f.calls[0].options.path, '/api/oauth/profile'); assert.equal(f.calls[0].options.method, 'GET');
  assert.equal(f.calls[0].options.headers.authorization, `Bearer ${token}`);
  assert.equal(f.calls[1].options.path, '/v1/oauth/token'); assert.equal(f.calls[1].options.method, 'POST');
  assert.deepEqual(JSON.parse(f.calls[1].body), grant); assert.equal(f.calls[1].options.headers.authorization, undefined);
  assert(f.handshakes.every(value => value.options.rejectUnauthorized === true));
});

test('invalid endpoint, nonlocal proxy and malformed bearer fail before any socket', () => {
  assert.throws(() => houseJson('http://127.0.0.1:1', 'arbitrary'), /unsupported_account_endpoint/);
  for (const proxy of ['https://127.0.0.1:11811', 'http://example.com:11811', 'http://user:pass@127.0.0.1:11811',
    'http://127.0.0.1:11811/path', 'http://127.0.0.1:11811?direct=true'])
    assert.throws(() => houseJson(proxy, 'profile', { token }), /invalid_house_proxy/);
  assert.throws(() => houseJson('http://127.0.0.1:1', 'profile', { token: 'bad\r\nAuthorization: stolen' }), /auth_unverified/);
});

for (const [status, expected] of [[400, 'login_required'], [401, 'login_required'], [403, 'auth_forbidden'],
  [429, 'auth_rate_limited'], [302, 'auth_unverified'], [500, 'auth_unverified']])
  test(`upstream ${status} produces only a stable local error and never follows redirects/retries`, async t => {
    const f = await fake(t, { responseStatus: status, payload: `SECRET ${token}`, headers: { location: 'https://untrusted.invalid/' } });
    await assert.rejects(houseJson(f.proxy, 'profile', { token }), error => error.message === expected && error.status === status);
    assert.equal(f.calls.length, 1); assert.equal(f.connects.length, 1);
  });

for (const options of [{ payload: 'not JSON' }, { payload: 'x'.repeat(1024 * 1024 + 1) }, { bodyFailure: true }])
  test(`invalid/oversized/aborted JSON response fails closed: ${options.bodyFailure ? 'aborted' : options.payload.length}`, async t => {
    const f = await fake(t, options);
    await assert.rejects(houseJson(f.proxy, 'profile', { token, timeoutMs: 1000 }), /auth_unverified|network_unavailable/);
    assert.equal(f.calls.length, 1);
  });

for (const options of [{ connectStatus: 407 }, { tlsError: true }])
  test(`CONNECT or TLS failure cannot send the bearer: ${JSON.stringify(options)}`, async t => {
    const f = await fake(t, options);
    await assert.rejects(houseJson(f.proxy, 'profile', { token }), /network_unavailable/);
    assert.equal(f.calls.length, 0); assert.equal(f.connects.length, 1);
  });

test('timed out TLS handshake cannot send credentials from a stale ready callback', async t => {
  const f = await fake(t, { delayedTLS: true });
  const pending = houseJson(f.proxy, 'profile', { token, timeoutMs: 25 });
  await assert.rejects(pending, /network_unavailable/); assert.equal(f.handshakes.length, 1);
  f.handshakes[0].secure.emit('secureConnect'); await delay(10);
  assert(f.handshakes[0].secure.destroyed); assert.equal(f.calls.length, 0);
});

test('actual local CONNECT plus TLS verifies the certificate and keeps OAuth private inside the tunnel', async t => {
  const root = await directory(t), cert = await runtimeCertificate(root);
  const requests = [], connects = [], sockets = new Set();
  const upstream = https.createServer({ cert: cert.cert, key: cert.key }, async (req, res) => {
    const chunks = []; for await (const chunk of req) chunks.push(chunk);
    requests.push({ path: req.url, headers: req.headers, body: Buffer.concat(chunks).toString() });
    res.setHeader('content-type', 'application/json'); res.end('{"fixture":"real TLS, local only"}');
  });
  upstream.listen(0, '127.0.0.1'); await once(upstream, 'listening');
  const proxy = http.createServer();
  proxy.on('connect', (req, client) => {
    connects.push({ path: req.url, headers: req.headers });
    const peer = net.connect(upstream.address().port, '127.0.0.1');
    for (const socket of [client, peer]) { sockets.add(socket); socket.on('error', () => {}); socket.once('close', () => sockets.delete(socket)); }
    peer.once('connect', () => { client.write('HTTP/1.1 200 Connection Established\r\n\r\n'); client.pipe(peer); peer.pipe(client); });
    peer.once('close', () => client.destroy()); client.once('close', () => peer.destroy());
  });
  proxy.listen(0, '127.0.0.1'); await once(proxy, 'listening');
  t.after(async () => { for (const socket of sockets) socket.destroy(); upstream.closeAllConnections();
    await Promise.all([new Promise(resolve => proxy.close(resolve)), new Promise(resolve => upstream.close(resolve))]); });
  const origin = `http://127.0.0.1:${proxy.address().port}`;
  await assert.rejects(houseJson(origin, 'profile', { token }), /network_unavailable/);
  assert.equal(requests.length, 0);
  assert.deepEqual(await houseJson(origin, 'profile', { token, ca: cert.cert }), { fixture: 'real TLS, local only' });
  assert.equal(requests.length, 1); assert.equal(requests[0].headers.authorization, `Bearer ${token}`);
  assert(connects.every(value => value.path === 'api.anthropic.com:443' && value.headers.authorization === undefined));
  await houseJson(origin, 'refresh', { ca: cert.cert, body: { refresh_token: 'INVENTED-REFRESH-NOT-REAL' } });
  assert.equal(connects.at(-1).path, 'platform.claude.com:443');
  assert.equal(requests.at(-1).path, '/v1/oauth/token');
});

test('runtime certificate is reused and never rotates a partially existing pair', async t => {
  const root = await directory(t);
  const first = await runtimeCertificate(root), second = await runtimeCertificate(root);
  assert.equal(first.fingerprint, second.fingerprint); assert(first.key.equals(second.key));
  const incomplete = await directory(t);
  await fs.writeFile(path.join(incomplete, 'api-cert.pem'), first.cert, { mode: 0o600 });
  await assert.rejects(runtimeCertificate(incomplete), /runtime_certificate_incomplete/);
  assert((await fs.readFile(path.join(incomplete, 'api-cert.pem'))).equals(first.cert));
});
