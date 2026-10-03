import test from 'node:test';
import assert from 'node:assert/strict';
import http from 'node:http';
import https from 'node:https';
import tls from 'node:tls';
import { once } from 'node:events';
import { PassThrough, Writable } from 'node:stream';
import { setTimeout as delay } from 'node:timers/promises';
import { createMessageBroker, createHouseTransport, rewriteMessageIdentity } from '../message-broker.mjs';

const key = 'INVENTED-LOCAL-RUNTIME-KEY-DO-NOT-USE';
const a = { accessToken: 'invented-token-A', deviceId: 'a'.repeat(64), accountUuid: '00000000-0000-4000-8000-000000000001',
  organizationUuid: '00000000-0000-4000-8000-000000000003', profileId: 'account-a', generation: 1 };
const b = { accessToken: 'invented-token-B', deviceId: 'b'.repeat(64), accountUuid: '00000000-0000-4000-8000-000000000002',
  organizationUuid: '00000000-0000-4000-8000-000000000004', profileId: 'account-b', generation: 2 };
function body(session = 'original-session', extra = {}) {
  return JSON.stringify({ model: 'claude-fixture', system: [{ type: 'text', text: 'x-anthropic-billing-header: cc_version=fixture' },
    { type: 'text', text: 'original system 中文', cache_control: { type: 'ephemeral', ttl: '1h' } }],
    messages: [{ role: 'assistant', content: [{ type: 'thinking', thinking: 'original', signature: 'signature-must-stay' }] },
      { role: 'user', content: [{ type: 'tool_result', tool_use_id: 'original-tool', content: 'once-only' }] }],
    tools: [{ name: 'fixture-tool', defer_loading: true, input_schema: { type: 'object' } }],
    metadata: { user_id: JSON.stringify({ device_id: a.deviceId, account_uuid: a.accountUuid,
      session_id: session, parent_session_id: 'parent-preserved' }) }, stream: true, ...extra });
}
const response = (status = 200, data = 'OK', headers = {}) => {
  const result = new PassThrough(); result.statusCode = status; result.headers = headers;
  queueMicrotask(() => result.end(data)); return result;
};
async function fixture(t, options = {}) {
  const calls = [];
  const broker = createMessageBroker({ localKey: key, getSnapshot: async () => b,
    transport: async input => { calls.push(input); return response(); }, ...options });
  const { port } = await broker.listen(); t.after(() => broker.close()); return { broker, port, calls };
}
function send(port, raw = body(), headers = {}, path = '/v1/messages?beta=true') {
  let request;
  const headersPromise = new Promise((resolve, reject) => {
    request = http.request({ hostname: '127.0.0.1', port, path, method: 'POST', agent: false,
      headers: Object.fromEntries(Object.entries({ 'content-type': 'application/json', 'x-ccpick-account-runtime': key,
        authorization: `Bearer ${a.accessToken}`, 'anthropic-beta': 'oauth-fixture,tool-search-fixture,extended-cache-ttl-fixture',
        'anthropic-version': '2023-06-01', ...headers }).filter(([, value]) => value !== undefined)) }, resolve);
    request.once('error', reject); request.end(raw);
  });
  headersPromise.catch(() => {});
  const result = headersPromise.then(async incoming => {
    const chunks = []; for await (const chunk of incoming) chunks.push(chunk);
    return { status: incoming.statusCode, headers: incoming.headers, body: Buffer.concat(chunks).toString() };
  }); result.catch(() => {});
  return { request, headers: headersPromise, result };
}
function assertOwner(call, owner) {
  const identity = JSON.parse(JSON.parse(call.body).metadata.user_id);
  assert.equal(call.headers.authorization, `Bearer ${owner.accessToken}`);
  assert.equal(identity.device_id, owner.deviceId); assert.equal(identity.account_uuid, owner.accountUuid);
}

test('in-flight SSE keeps frozen account and household while the next request changes both', async t => {
  const routeA = { household: 'A', proxy: 'http://127.0.0.1:11811', revision: 'route-a' };
  const routeB = { household: 'B', proxy: 'http://127.0.0.1:11911', revision: 'route-b' };
  let selected = { ...a, egress: routeA };
  const firstStream = new PassThrough(); firstStream.statusCode = 200; firstStream.headers = {};
  const f = await fixture(t, { getSnapshot: async () => selected, transport: async input => {
    f.calls.push(input);
    return f.calls.length === 1 ? firstStream : response(200, 'B-result');
  } });
  const first = send(f.port); await until(() => f.calls.length === 1);
  firstStream.write('A-first\n'); await first.headers;
  selected = { ...b, egress: routeB }; routeA.proxy = routeB.proxy;
  assert.equal((await send(f.port).result).body, 'B-result');
  firstStream.end('A-last\n'); assert.equal((await first.result).body, 'A-first\nA-last\n');
  assertOwner(f.calls[0], a); assertOwner(f.calls[1], b);
  assert.equal(f.calls[0].snapshot.egress.proxy, 'http://127.0.0.1:11811');
  assert.equal(f.calls[1].snapshot.egress.proxy, 'http://127.0.0.1:11911');
  assert(Object.isFrozen(f.calls[0].snapshot.egress));
});
async function until(predicate, timeout = 2000) {
  const end = Date.now() + timeout;
  while (!predicate()) { assert(Date.now() < end, 'fixture condition timed out'); await delay(5); }
}

test('identity patch preserves every other byte, exact numbers, signed content and session IDs', () => {
  const original = body().replace('"stream":true', '"stream":true,"large":9007199254740993123456789');
  const changed = rewriteMessageIdentity(Buffer.from(original), b);
  assert.equal(rewriteMessageIdentity(changed, a).toString(), original);
  const user = JSON.parse(JSON.parse(changed).metadata.user_id);
  assert.equal(user.session_id, 'original-session'); assert.equal(user.parent_session_id, 'parent-preserved');
  assert(changed.toString().includes('9007199254740993123456789'));
});

test('old A request emits atomic B auth/identity, removes sensitive headers and normalizes org', async t => {
  let admission;
  const f = await fixture(t, { getSnapshot: async context => { admission = context; return b; } });
  const result = await send(f.port, body(), { 'x-api-key': 'old-api-key', 'x-cc-atis': 'old-account-signature',
    'x-organization-uuid': a.organizationUuid, 'x-forwarded-for': '192.0.2.1', 'x-claude-code-session-id': 'original-session',
    'x-ccpick-account-scope': 'account-b', 'x-ccpick-future-marker': 'private-marker' }).result;
  assert.equal(result.status, 200); assert.equal(f.calls.length, 1); assertOwner(f.calls[0], b);
  assert.equal(f.calls[0].headers['x-organization-uuid'], b.organizationUuid);
  for (const name of ['x-api-key', 'x-cc-atis', 'x-ccpick-account-runtime', 'x-ccpick-account-scope', 'x-ccpick-future-marker', 'x-forwarded-for']) assert.equal(f.calls[0].headers[name], undefined);
  assert.equal(f.calls[0].headers['anthropic-beta'], 'oauth-fixture,tool-search-fixture,extended-cache-ttl-fixture,oauth-2025-04-20');
  assert.equal(f.calls[0].headers['x-claude-code-session-id'], 'original-session');
  assert.equal(admission.headers.authorization, `Bearer ${a.accessToken}`);
  assert.equal(admission.headers['x-ccpick-account-runtime'], undefined);
  assert.equal(admission.headers['x-ccpick-account-scope'], 'account-b');
  assert(Object.isFrozen(admission.headers));
});

test('persistent local Authorization can authenticate during native login gap with empty IDs', async t => {
  const f = await fixture(t);
  const raw = body('gap', { metadata: { user_id: JSON.stringify({ device_id: '', account_uuid: '', session_id: 'gap' }) } });
  const result = await send(f.port, raw, { 'x-ccpick-account-runtime': undefined, authorization: `Bearer ${key}` }).result;
  assert.equal(result.status, 200); assertOwner(f.calls[0], b);
  assert(!JSON.stringify(f.calls[0].headers).includes(key));
});

test('required OAuth capability is restored during login gap and other capabilities are preserved once', async t => {
  const f = await fixture(t);
  await send(f.port, body(), { 'anthropic-beta': undefined, authorization: `Bearer ${key}` }).result;
  assert.equal(f.calls[0].headers['anthropic-beta'], 'oauth-2025-04-20');
  await send(f.port, body(), { 'anthropic-beta': 'extended-cache-ttl-fixture,oauth-2025-04-20,tool-search-fixture,oauth-2025-04-20' }).result;
  assert.equal(f.calls[1].headers['anthropic-beta'], 'extended-cache-ttl-fixture,oauth-2025-04-20,tool-search-fixture');
});

test('official TLS-handler seam still requires local key, loopback and exact official Host', async t => {
  const f = await fixture(t);
  const listener = http.createServer((req, res) => { void f.broker.handleOfficial(req, res); });
  listener.listen(0, '127.0.0.1'); await once(listener, 'listening');
  t.after(() => new Promise(resolve => listener.close(resolve)));
  const port = listener.address().port;
  assert.equal((await send(port, body(), { host: 'api.anthropic.com', authorization: `Bearer ${key}` }).result).status, 200);
  assert.equal((await send(port, body(), { host: 'api.anthropic.com:443', 'x-ccpick-account-runtime': undefined,
    authorization: `Bearer ${key}` }).result).status, 200);
  assert.equal((await send(port, body(), { host: 'api.anthropic.com', 'x-ccpick-account-runtime': undefined }).result).status, 403);
  assert.equal((await send(port).result).status, 403);
  assert.equal((await send(f.port, body(), { host: 'api.anthropic.com' }).result).status, 403);
  assert.equal(f.calls.length, 2);
  for (const call of f.calls) { assertOwner(call, b); assert(!JSON.stringify(call.headers).includes(key)); }
});

test('org absent in snapshot removes stale org; valid snapshot does not invent an absent header', async t => {
  const f = await fixture(t, { getSnapshot: async () => ({ ...b, organizationUuid: undefined }) });
  await send(f.port, body(), { 'x-organization-uuid': a.organizationUuid }).result;
  assert.equal(f.calls[0].headers['x-organization-uuid'], undefined);
  const g = await fixture(t); await send(g.port).result;
  assert.equal(g.calls[0].headers['x-organization-uuid'], undefined);
});

test('count_tokens supports native body without metadata, preserves query/body and changes auth only', async t => {
  const f = await fixture(t); const raw = '{ "model":"claude-fixture", "messages":[], "tools":[] }';
  assert.equal((await send(f.port, raw, {}, '/v1/messages/count_tokens?beta=true').result).status, 200);
  assert.equal(f.calls[0].body.toString(), raw);
  assert.equal(f.calls[0].path, '/v1/messages/count_tokens?beta=true');
  assert.equal(f.calls[0].headers.authorization, `Bearer ${b.accessToken}`);
  assert.equal((await send(f.port, body(), {}, '/v1/messages/count_tokens').result).status, 200);
  assertOwner(f.calls[1], b);
});

test('snapshot resolves asynchronously once and copied bundle survives later mutation', async t => {
  let release, snapshotCalls = 0;
  const source = { ...a };
  const f = await fixture(t, { getSnapshot: () => { snapshotCalls++; return new Promise(resolve => { release = () => resolve(source); }); },
    transport: async input => { Object.assign(source, b); f.calls.push(input); return response(); } });
  const request = send(f.port); await until(() => release); release();
  assert.equal((await request.result).status, 200); assert.equal(snapshotCalls, 1); assertOwner(f.calls[0], a);
});

test('A stream continues unchanged while subsequent request captures B without restarting either', async t => {
  let selected = a, first;
  const f = await fixture(t, { getSnapshot: async () => selected, transport: async input => {
    f.calls.push(input);
    if (f.calls.length === 1) {
      first = new PassThrough(); first.statusCode = 200; first.headers = { 'content-type': 'text/event-stream' };
      first.write('event: ping\ndata: {"type":"ping"}\n\n'); return first;
    }
    return response(200, 'B finished');
  } });
  const ongoing = send(f.port); await ongoing.headers;
  selected = b; assert.equal((await send(f.port).result).body, 'B finished');
  first.end('event: done\ndata: {"text":"A finished"}\n\n');
  assert.equal((await ongoing.result).body, 'event: ping\ndata: {"type":"ping"}\n\nevent: done\ndata: {"text":"A finished"}\n\n');
  assertOwner(f.calls[0], a); assertOwner(f.calls[1], b);
});

test('240 main/agent/workflow requests with 24 concurrent never mix bundle fields', async t => {
  let selected = a, next = 0;
  const f = await fixture(t, { getSnapshot: async () => { await delay(1); return selected; } });
  await Promise.all(Array.from({ length: 24 }, async () => {
    while (next < 240) {
      const i = next++; selected = i % 2 ? a : b;
      assert.equal((await send(f.port, body(`session-${i % 6}`), { 'x-claude-code-request-class': ['main', 'subagent', 'workflow'][i % 3] }).result).status, 200);
    }
  }));
  assert.equal(f.calls.length, 240);
  for (const call of f.calls) assertOwner(call, call.headers.authorization === `Bearer ${a.accessToken}` ? a : b);
});

test('upstream statuses, errors and retry headers are not rewritten or locally retried', async t => {
  let count = 0;
  const raw = '{ "type":"error", "error":{"type":"authentication_error","message":"original"} }';
  const f = await fixture(t, { transport: async input => { f.calls.push(input); return response([401, 429, 500][count++], raw,
    { 'retry-after': '23', 'anthropic-ratelimit-unified-status': 'rejected', 'request-id': 'same' }); } });
  for (const status of [401, 429, 500]) {
    const result = await send(f.port).result; assert.equal(result.status, status); assert.equal(result.body, raw);
    assert.equal(result.headers['retry-after'], '23'); assert.equal(result.headers['request-id'], 'same');
  }
  assert.equal(f.calls.length, 3);
});

test('malformed, duplicate, signed or encoded inputs fail before credentials are read', async t => {
  let reads = 0;
  const f = await fixture(t, { getSnapshot: async () => { reads++; return b; } });
  const cases = [
    ['{}', {}, 400], [body().replace('"metadata":', '"metadata":{},"metadata":'), {}, 400],
    [Buffer.concat([Buffer.from([0xef, 0xbb, 0xbf]), Buffer.from(body())]), {}, 400],
    [Buffer.concat([Buffer.from(body()), Buffer.from([0xff])]), {}, 400],
    [body('x', { metadata: { user_id: '{"device_id":"a","device_id":"b","account_uuid":"x","session_id":"x"}' } }), {}, 400],
    [body('x', { metadata: { user_id: JSON.stringify({ device_id: '', account_uuid: '', session_id: 'x', tk: 'signed' }) } }), {}, 400],
    [body(), { 'content-encoding': 'gzip' }, 415], [body(), { origin: '' }, 403],
    [body(), { host: 'evil.invalid' }, 403], [body(), { 'sec-fetch-site': 'same-origin' }, 403],
    [body(), { 'x-ccpick-account-runtime': 'wrong' }, 403],
    [body(), { 'x-ccpick-account-runtime': [key, key] }, 403],
    [body(), { authorization: [`Bearer ${key}`, `Bearer ${key}`] }, 403],
  ];
  for (const [raw, headers, status] of cases) assert.equal((await send(f.port, raw, headers).result).status, status);
  for (const path of ['/v1/unknown', '//api.anthropic.com/v1/messages', '/v1/messages/../messages', 'http://evil.invalid/v1/messages'])
    assert.equal((await send(f.port, body(), {}, path).result).status, 404);
  assert.equal(reads, 0); assert.equal(f.calls.length, 0);
});

test('body size and shared buffer bounds fail before transport', async t => {
  const f = await fixture(t, { maxBodyBytes: 8 });
  assert.equal((await send(f.port).result).status, 413); assert.equal(f.calls.length, 0);
  const g = await fixture(t, { maxBufferedBytes: 8 });
  assert.equal((await send(g.port).result).status, 503); assert.equal(g.calls.length, 0);
  assert.equal(g.broker.status().bufferedBytes, 0);
});

test('snapshot timeout and disconnect abort admission; delayed resolution cannot send', async t => {
  let release, signal;
  const f = await fixture(t, { snapshotTimeoutMs: 25, getSnapshot: context => { signal = context.signal;
    return new Promise(resolve => { release = resolve; }); } });
  assert.equal((await send(f.port).result).status, 503); assert(signal.aborted);
  release(b); await delay(10); assert.equal(f.calls.length, 0); assert.equal(f.broker.status().bufferedBytes, 0);
  const g = await fixture(t, { getSnapshot: context => { signal = context.signal; return new Promise(resolve => { release = resolve; }); } });
  const pending = send(g.port); await until(() => g.broker.status().bufferedBytes > 0);
  pending.request.destroy(); await assert.rejects(pending.result); await until(() => signal.aborted);
  release(b); await delay(10); assert.equal(g.calls.length, 0);
});

test('client cancellation aborts streaming upstream without any retry', async t => {
  let signal, stream;
  const f = await fixture(t, { transport: async input => {
    signal = input.signal; f.calls.push(input); stream = new PassThrough(); stream.statusCode = 200; stream.headers = {};
    signal.addEventListener('abort', () => stream.destroy()); stream.write('partial'); return stream;
  } });
  const pending = send(f.port); const incoming = await pending.headers; incoming.destroy();
  await assert.rejects(pending.result); await until(() => signal.aborted);
  assert.equal(f.calls.length, 1); assert(stream.destroyed);
});

test('partial upstream stream failure aborts downstream and is never replayed', async t => {
  let stream;
  const f = await fixture(t, { transport: async input => {
    f.calls.push(input); stream = new PassThrough(); stream.statusCode = 200; stream.headers = {};
    stream.write('partial'); return stream;
  } });
  const pending = send(f.port); await pending.headers; stream.destroy(new Error('synthetic_stream_failure'));
  await assert.rejects(pending.result); assert.equal(f.calls.length, 1);
});

test('closing releases body reservations and disposes of a late injected transport response', async t => {
  let release;
  const f = await fixture(t, { transport: () => new Promise(resolve => { release = resolve; }) });
  const pending = send(f.port); await until(() => release); await f.broker.close();
  await assert.rejects(pending.result); await until(() => f.broker.status().active === 0);
  assert.equal(f.broker.status().bufferedBytes, 0);
  const late = response(); release(late); await until(() => late.destroyed);
});

test('snapshot/transport exceptions never expose details or credentials', async t => {
  const f = await fixture(t, { getSnapshot: () => { throw new Error(`SECRET ${a.accessToken}`); } });
  const denied = await send(f.port).result; assert.equal(denied.status, 503); assert(!denied.body.includes(a.accessToken));
  const g = await fixture(t, { transport: () => { throw new Error(`SECRET ${b.accessToken}`); } });
  const failed = await send(g.port).result; assert.equal(failed.status, 502); assert(!failed.body.includes(b.accessToken));
});

test('house transport rejects nonlocal/nonHTTP/authenticated proxy configurations', () => {
  for (const proxy of ['http://example.com:11808', 'https://127.0.0.1:11808', 'http://localhost:11808',
    'http://user:pass@127.0.0.1:11808', 'http://127.0.0.1:11808/path', 'http://127.0.0.1:11808?fallback=true'])
    assert.throws(() => createHouseTransport(proxy));
  assert.throws(() => createMessageBroker({ getSnapshot: async () => b, transport: async () => response() }), /key/);
});

async function tunnelFixture(t, { status = 200, unauthorized = false, tlsFailure = false, delayTLS = false, head = '', connectTimeoutMs = 250 } = {}) {
  const connections = [], handshakes = [], sends = [], sockets = new Set();
  const proxy = http.createServer();
  proxy.on('connect', (request, socket) => {
    connections.push({ path: request.url, headers: request.headers }); sockets.add(socket);
    socket.on('error', () => {}); socket.once('end', () => socket.destroy()); socket.once('close', () => sockets.delete(socket));
    socket.write(`HTTP/1.1 ${status} Fixture\r\n\r\n${head}`);
  });
  proxy.listen(0, '127.0.0.1'); await once(proxy, 'listening');
  t.mock.method(tls, 'connect', settings => {
    const secure = new PassThrough(); secure.authorized = !unauthorized; secure.encrypted = true;
    secure.once('close', () => settings.socket.destroy()); handshakes.push({ settings, secure });
    if (!delayTLS) queueMicrotask(() => tlsFailure ? secure.destroy(new Error('fake_tls_failure')) : secure.emit('secureConnect'));
    return secure;
  });
  t.mock.method(https, 'request', (settings, callback) => {
    const chunks = []; const outgoing = new Writable({ write(chunk, _, done) { chunks.push(chunk); done(); } });
    outgoing.once('finish', () => { sends.push({ settings, body: Buffer.concat(chunks) }); callback(response(200, 'fake-tunnel')); });
    return outgoing;
  });
  t.after(async () => { for (const { secure } of handshakes) secure.destroy(); for (const socket of sockets) socket.destroy();
    await new Promise(resolve => proxy.close(resolve)); });
  return { transport: createHouseTransport(`http://127.0.0.1:${proxy.address().port}`, { connectTimeoutMs }), connections, handshakes, sends };
}

test('production transport sends only CONNECT official host then verified TLS; proxy sees no API credential', async t => {
  const tunnel = await tunnelFixture(t); const f = await fixture(t, { transport: tunnel.transport });
  assert.equal((await send(f.port).result).body, 'fake-tunnel');
  assert.equal(tunnel.connections.length, 1); assert.equal(tunnel.connections[0].path, 'api.anthropic.com:443');
  assert.equal(tunnel.connections[0].headers.authorization, undefined);
  assert.equal(tunnel.connections[0].headers['proxy-authorization'], undefined);
  assert.equal(tunnel.handshakes[0].settings.servername, 'api.anthropic.com');
  assert.equal(tunnel.handshakes[0].settings.rejectUnauthorized, true);
  assert.deepEqual(tunnel.handshakes[0].settings.ca, tls.rootCertificates);
  assert.equal(tunnel.sends[0].settings.hostname, 'api.anthropic.com');
  assert.equal(tunnel.sends[0].settings.rejectUnauthorized, true);
  assert.equal(tunnel.sends[0].settings.headers.authorization, `Bearer ${b.accessToken}`);
});

for (const options of [{ status: 407 }, { unauthorized: true }, { tlsFailure: true }, { head: 'unexpected-tunnel-bytes' }])
  test(`CONNECT/TLS failure closes locally, sends no API secret and never falls back: ${JSON.stringify(options)}`, async t => {
    const tunnel = await tunnelFixture(t, options); const f = await fixture(t, { transport: tunnel.transport });
    assert.equal((await send(f.port).result).status, 502); assert.equal(tunnel.connections.length, 1); assert.equal(tunnel.sends.length, 0);
  });

test('cancel during TLS setup destroys tunnel and delayed readiness cannot send', async t => {
  const tunnel = await tunnelFixture(t, { delayTLS: true }); const f = await fixture(t, { transport: tunnel.transport });
  const pending = send(f.port); await until(() => tunnel.handshakes.length === 1);
  pending.request.destroy(); await assert.rejects(pending.result);
  await until(() => tunnel.handshakes[0].secure.destroyed);
  tunnel.handshakes[0].secure.emit('secureConnect'); await delay(10); assert.equal(tunnel.sends.length, 0);
});

test('TLS setup timeout closes the checked tunnel without any API request or fallback', async t => {
  const tunnel = await tunnelFixture(t, { delayTLS: true, connectTimeoutMs: 25 });
  const f = await fixture(t, { transport: tunnel.transport });
  assert.equal((await send(f.port).result).status, 502); assert.equal(tunnel.sends.length, 0);
  assert(tunnel.handshakes[0].secure.destroyed);
});
