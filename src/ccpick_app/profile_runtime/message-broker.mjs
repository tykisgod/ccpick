import http from 'node:http';
import https from 'node:https';
import tls from 'node:tls';
import { createHash, timingSafeEqual } from 'node:crypto';

const HOST = 'api.anthropic.com';
const LOCAL_HEADER = 'x-ccpick-account-runtime';
const ROUTES = new Set(['/v1/messages', '/v1/messages/count_tokens']);
const HOP = new Set(['host', 'connection', 'keep-alive', 'proxy-authenticate', 'proxy-authorization',
  'te', 'trailer', 'transfer-encoding', 'upgrade', 'expect']);
const PRIVATE_HEADERS = new Set(['authorization', 'x-api-key', 'x-cc-atis', LOCAL_HEADER, 'x-ccpick-account-scope',
  'origin', 'forwarded', 'x-forwarded-for', 'x-real-ip', 'client-ip', 'x-client-ip',
  'true-client-ip', 'cf-connecting-ip', 'x-originating-ip', 'x-cluster-client-ip']);
const UUID = /^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$/i;
const digest = value => createHash('sha256').update(value).digest();
const abortError = () => Object.assign(new Error('request_cancelled'), { name: 'AbortError' });

function abortable(promise, signal) {
  if (signal.aborted) return Promise.reject(abortError());
  return new Promise((resolve, reject) => {
    const aborted = () => { signal.removeEventListener('abort', aborted); reject(abortError()); };
    signal.addEventListener('abort', aborted, { once: true });
    Promise.resolve(promise).then(resolve, reject).finally(() => signal.removeEventListener('abort', aborted));
  });
}

function boundedInteger(value, fallback, maximum) {
  const result = value ?? fallback;
  if (!Number.isSafeInteger(result) || result < 1 || result > maximum) throw new Error('invalid_broker_limit');
  return result;
}

function validPath(path) {
  if (typeof path !== 'string' || !path.startsWith('/') || path.startsWith('//') || /[\s\\#]/.test(path)) return false;
  return ROUTES.has(path.split('?')[0]);
}

function headersFor(headers, privateHeaders = false) {
  const blocked = new Set(HOP);
  if (privateHeaders) for (const name of PRIVATE_HEADERS) blocked.add(name);
  for (const part of String(headers.connection ?? '').split(',')) blocked.add(part.trim().toLowerCase());
  const result = {};
  for (const [name, value] of Object.entries(headers))
    if (!blocked.has(name.toLowerCase()) && !(privateHeaders && name.toLowerCase().startsWith('x-ccpick-')) && value !== undefined)
      result[name.toLowerCase()] = value;
  return result;
}

function frozenSnapshot(value) {
  const { accessToken, deviceId, accountUuid, organizationUuid, profileId, generation } = value ?? {};
  if (typeof accessToken !== 'string' || !accessToken || accessToken.length > 16_384 || /[\s\x00-\x1f\x7f]/.test(accessToken) ||
      typeof deviceId !== 'string' || !/^[a-f0-9]{64}$/i.test(deviceId) || typeof accountUuid !== 'string' || !UUID.test(accountUuid) ||
      (organizationUuid != null && (typeof organizationUuid !== 'string' || !UUID.test(organizationUuid)))) throw new Error('invalid_account_snapshot');
  return Object.freeze({ accessToken, deviceId, accountUuid, organizationUuid, profileId, generation });
}

function objectSpans(text, start = 0, limit = text.length) {
  let i = start;
  const whitespace = () => { while (i < limit && /[ \t\r\n]/.test(text[i])) i++; };
  function stringEnd() {
    i++;
    while (i < limit) { if (text[i] === '\\') i += 2; else if (text[i++] === '"') return; }
    throw new Error('invalid_json');
  }
  function endValue() {
    if (text[i] === '"') { stringEnd(); return; }
    if (text[i] === '{' || text[i] === '[') {
      let depth = 0;
      do {
        if (text[i] === '"') { stringEnd(); continue; }
        if (text[i] === '{' || text[i] === '[') depth++;
        if (text[i] === '}' || text[i] === ']') depth--;
        if (depth > 256) throw new Error('json_depth');
        i++;
      } while (depth > 0 && i < limit);
      return;
    }
    while (i < limit && !/[,\]} \t\r\n]/.test(text[i])) i++;
  }
  whitespace(); if (text[i++] !== '{') throw new Error('object_required');
  const properties = new Map(); whitespace();
  if (text[i] !== '}') for (;;) {
    if (text[i] !== '"') throw new Error('invalid_json');
    const keyStart = i; stringEnd(); const name = JSON.parse(text.slice(keyStart, i));
    if (properties.has(name)) throw new Error('ambiguous_identity');
    whitespace(); if (text[i++] !== ':') throw new Error('invalid_json'); whitespace();
    const valueStart = i; endValue(); properties.set(name, { start: valueStart, end: i }); whitespace();
    if (text[i] !== ',') break; i++; whitespace();
  }
  if (text[i++] !== '}') throw new Error('invalid_json'); whitespace();
  if (i !== limit) throw new Error('invalid_json');
  return properties;
}

function parseRequest(bytes, allowMissingMetadata = false) {
  const text = new TextDecoder('utf-8', { fatal: true, ignoreBOM: true }).decode(bytes);
  JSON.parse(text); // Strict syntax validation; the parsed conversation is discarded.
  const root = objectSpans(text);
  const metadata = root.get('metadata');
  if (!metadata) {
    if (allowMissingMetadata) return { text, user: null };
    throw new Error('metadata_required');
  }
  const user = objectSpans(text, metadata.start, metadata.end).get('user_id');
  if (!user) throw new Error('identity_required');
  const identityText = JSON.parse(text.slice(user.start, user.end));
  if (typeof identityText !== 'string') throw new Error('identity_required');
  const identity = JSON.parse(identityText);
  const spans = objectSpans(identityText);
  if (typeof identity.device_id !== 'string' || typeof identity.account_uuid !== 'string' ||
      typeof identity.session_id !== 'string' || !identity.session_id) throw new Error('identity_required');
  if (['tk', 'ti', 'signature', 'signed'].some(key => spans.has(key))) throw new Error('signed_identity_unsupported');
  return { text, user, identityText, spans };
}

function replaceSpans(text, changes) {
  for (const value of changes.sort((a, b) => b.start - a.start))
    text = text.slice(0, value.start) + value.replacement + text.slice(value.end);
  return text;
}

function encodeRequest(parsed, snapshot) {
  if (!parsed.user) return Buffer.from(parsed.text);
  const identity = replaceSpans(parsed.identityText, [
    { ...parsed.spans.get('device_id'), replacement: JSON.stringify(snapshot.deviceId) },
    { ...parsed.spans.get('account_uuid'), replacement: JSON.stringify(snapshot.accountUuid) },
  ]);
  return Buffer.from(replaceSpans(parsed.text, [{ ...parsed.user, replacement: JSON.stringify(identity) }]));
}

export function rewriteMessageIdentity(bytes, snapshot) {
  return encodeRequest(parseRequest(bytes), frozenSnapshot(snapshot));
}

/** Single-send transport. No credentials are put on CONNECT, no direct fallback,
 * no redirects, and no TLS trust override. signal remains active through SSE. */
export function createHouseTransport(proxy, options = {}) {
  const endpoint = new URL(proxy);
  if (endpoint.protocol !== 'http:' || !['127.0.0.1', '[::1]'].includes(endpoint.hostname) ||
      !endpoint.port || endpoint.username || endpoint.password || endpoint.pathname !== '/' || endpoint.search || endpoint.hash)
    throw new Error('invalid_residential_proxy');
  const connectTimeoutMs = boundedInteger(options.connectTimeoutMs, 15_000, 120_000);
  const responseTimeoutMs = boundedInteger(options.responseTimeoutMs, 120_000, 600_000);
  return ({ path, headers, body, signal }) => new Promise((resolve, reject) => {
    if (!validPath(path) || !Buffer.isBuffer(body)) { reject(new Error('invalid_upstream_request')); return; }
    let connect, socket, secure, agent, request, response, timer, settled = false, finished = false;
    const cleanup = () => {
      clearTimeout(timer); signal?.removeEventListener('abort', onAbort);
      request?.destroy(); response?.destroy(); agent?.destroy(); secure?.destroy(); socket?.destroy(); connect?.destroy();
    };
    const fail = () => {
      if (finished) return; finished = true;
      cleanup(); if (!settled) reject(new Error(signal?.aborted ? 'request_cancelled' : 'residential_transport_failed'));
    };
    const onAbort = fail;
    if (signal?.aborted) { fail(); return; }
    signal?.addEventListener('abort', onAbort, { once: true });
    timer = setTimeout(fail, connectTimeoutMs); timer.unref();
    try {
      connect = http.request({ hostname: endpoint.hostname.replace(/^\[|\]$/g, ''), port: endpoint.port,
        method: 'CONNECT', path: `${HOST}:443`, headers: { host: `${HOST}:443` }, agent: false });
      connect.once('error', fail);
      connect.once('response', fail);
      connect.once('connect', (incoming, tunnel, head) => {
        socket = tunnel;
        if (finished || incoming.statusCode !== 200 || head.length) { fail(); socket.destroy(); return; }
        socket.once('error', fail);
        try { secure = tls.connect({ socket, servername: HOST, rejectUnauthorized: true, ca: tls.rootCertificates }); }
        catch { fail(); return; }
        secure.once('error', fail);
        secure.once('close', () => { if (!finished) fail(); });
        secure.once('secureConnect', () => {
          if (finished) { secure.destroy(); return; }
          if (!secure.authorized) { fail(); return; }
          clearTimeout(timer); timer = setTimeout(fail, responseTimeoutMs); timer.unref();
          agent = new https.Agent({ keepAlive: false, maxSockets: 1 });
          agent.createConnection = (_settings, callback) => { callback(null, secure); };
          try {
            request = https.request({ hostname: HOST, port: 443, method: 'POST', path,
              headers: { ...headers, host: HOST }, agent, rejectUnauthorized: true }, incomingResponse => {
              if (finished) { incomingResponse.destroy(); return; }
              response = incomingResponse; clearTimeout(timer); settled = true;
              response.once('error', fail); response.once('aborted', fail);
              response.once('end', () => { if (!finished) { finished = true; cleanup(); } });
              resolve(response);
            });
            request.once('error', fail); request.end(body);
          } catch { fail(); }
        });
      });
      connect.end();
    } catch { fail(); }
  });
}

/** getSnapshot({headers, signal}) must return one coherent account bundle. The
 * callback owns refresh/selection coordination. This module never reads a vault,
 * changes native configuration, retries a model request, or writes a credential. */
export function createMessageBroker(options = {}) {
  if (typeof options.getSnapshot !== 'function') throw new Error('snapshot_provider_required');
  if (typeof options.localKey !== 'string' || options.localKey.length < 32 || options.localKey.length > 512 || /[^\x21-\x7e]/.test(options.localKey))
    throw new Error('local_runtime_key_required');
  const expectedKey = digest(options.localKey);
  const transport = options.transport ?? createHouseTransport(options.upstreamProxy);
  if (typeof transport !== 'function') throw new Error('invalid_upstream_transport');
  const maxBodyBytes = boundedInteger(options.maxBodyBytes, 32 * 1024 * 1024, 128 * 1024 * 1024);
  const maxBufferedBytes = boundedInteger(options.maxBufferedBytes, Math.max(maxBodyBytes, 64 * 1024 * 1024), 512 * 1024 * 1024);
  const requestTimeoutMs = boundedInteger(options.requestTimeoutMs, 30_000, 600_000);
  const snapshotTimeoutMs = boundedInteger(options.snapshotTimeoutMs, 30_000, 600_000);
  const counts = { received: 0, dispatched: 0, completed: 0, rejected: 0, failed: 0, cancelled: 0 };
  const active = new Set(); let closing = false, bufferedBytes = 0, closePromise;
  const server = http.createServer((req, res) => { void handle(req, res); });
  server.requestTimeout = requestTimeoutMs;
  server.headersTimeout = Math.min(requestTimeoutMs, 30_000);

  function errorReply(res, status, code) {
    if (res.destroyed) return;
    if (res.headersSent) { res.destroy(); return; }
    const data = JSON.stringify({ type: 'error', error: { type: 'api_error', message: code } });
    res.writeHead(status, { 'content-type': 'application/json', 'content-length': Buffer.byteLength(data),
      'cache-control': 'no-store', connection: 'close' }); res.end(data);
  }
  async function handle(req, res, official = false) {
    counts.received++;
    const controller = new AbortController(); const { signal } = controller;
    let reserved = 0, completed = false, timer, response;
    const releaseBody = () => { bufferedBytes -= reserved; reserved = 0; };
    const cancel = () => { if (!completed && !signal.aborted) { counts.cancelled++; controller.abort(); response?.destroy(); } };
    const stop = () => { cancel(); res.destroy(); req.destroy(); };
    active.add(stop);
    req.once('aborted', cancel); res.once('close', cancel);
    const reject = (status, code) => { counts.rejected++; completed = true; errorReply(res, status, code); };
    try {
      const source = req.socket.remoteAddress;
      if (!['127.0.0.1', '::1', '::ffff:127.0.0.1'].includes(source) || Object.hasOwn(req.headers, 'origin') ||
          Object.keys(req.headers).some(name => name.startsWith('sec-fetch-'))) return reject(403, 'local_client_denied');
      const rawCount = name => req.rawHeaders.filter((_, i) => i % 2 === 0 && req.rawHeaders[i].toLowerCase() === name).length;
      const acceptedHost = official ? [HOST, `${HOST}:443`].includes(req.headers.host) : req.headers.host === `127.0.0.1:${server.address()?.port}`;
      if (rawCount('host') !== 1 || !acceptedHost) return reject(403, 'local_host_denied');
      const key = req.headers[LOCAL_HEADER];
      const authorization = req.headers.authorization;
      const validKey = value => typeof value === 'string' && value.length <= 512 && timingSafeEqual(digest(value), expectedKey);
      const validLocalHeader = rawCount(LOCAL_HEADER) === 1 && validKey(key);
      const validLocalBearer = rawCount('authorization') === 1 && typeof authorization === 'string' &&
        authorization.startsWith('Bearer ') && validKey(authorization.slice(7));
      if (rawCount('authorization') > 1 || rawCount(LOCAL_HEADER) > 1 ||
          (key !== undefined && !validLocalHeader) || (!validLocalHeader && !validLocalBearer))
        return reject(403, 'local_runtime_key_invalid');
      if (closing) return reject(503, 'local_broker_stopping');
      if (req.method !== 'POST' || !validPath(req.url)) return reject(404, 'unsupported_messages_route');
      if (!/^application\/json(?:\s*;|$)/i.test(req.headers['content-type'] ?? '') ||
          (req.headers['content-encoding'] && req.headers['content-encoding'] !== 'identity')) return reject(415, 'unsupported_messages_encoding');
      if (Number(req.headers['content-length'] ?? 0) > maxBodyBytes) return reject(413, 'messages_body_too_large');
      timer = setTimeout(() => { errorReply(res, 408, 'messages_upload_timed_out'); controller.abort(); req.destroy(); }, requestTimeoutMs); timer.unref();
      const chunks = [];
      for await (const chunk of req) {
        if (signal.aborted) throw abortError();
        if (reserved + chunk.length > maxBodyBytes) return reject(413, 'messages_body_too_large');
        if (bufferedBytes + chunk.length > maxBufferedBytes) return reject(503, 'messages_buffer_busy');
        reserved += chunk.length; bufferedBytes += chunk.length; chunks.push(chunk);
      }
      clearTimeout(timer); if (signal.aborted) return;
      let parsed;
      try { parsed = parseRequest(Buffer.concat(chunks), req.url.split('?')[0] === '/v1/messages/count_tokens'); }
      catch { return reject(400, 'unsupported_messages_identity'); }
      chunks.length = 0;
      const incomingHeaders = { ...req.headers }; delete incomingHeaders[LOCAL_HEADER];
      timer = setTimeout(() => { errorReply(res, 503, 'account_admission_timed_out'); controller.abort(); }, snapshotTimeoutMs); timer.unref();
      let snapshot;
      try { snapshot = frozenSnapshot(await abortable(options.getSnapshot({ headers: Object.freeze(incomingHeaders), signal }), signal)); }
      catch { if (!signal.aborted) reject(503, 'account_snapshot_unavailable'); return; }
      clearTimeout(timer); if (signal.aborted) return;
      let body = encodeRequest(parsed, snapshot); parsed = null;
      const headers = headersFor(req.headers, true);
      headers.authorization = `Bearer ${snapshot.accessToken}`;
      headers['anthropic-beta'] = [...new Set([...String(headers['anthropic-beta'] ?? '').split(',').map(value => value.trim()).filter(Boolean),
        'oauth-2025-04-20'])].join(',');
      headers['content-length'] = String(body.length);
      if (Object.hasOwn(req.headers, 'x-organization-uuid') && snapshot.organizationUuid) headers['x-organization-uuid'] = snapshot.organizationUuid;
      else delete headers['x-organization-uuid'];
      counts.dispatched++;
      const pendingResponse = Promise.resolve(transport({ path: req.url, headers, body, signal }));
      void pendingResponse.then(incoming => { if (signal.aborted) incoming?.destroy(); }, () => {});
      response = await abortable(pendingResponse, signal);
      body = null;
      releaseBody();
      if (signal.aborted || closing) { response.destroy(); return; }
      response.once('error', () => { counts.failed++; res.destroy(); });
      response.once('aborted', () => res.destroy());
      response.once('close', () => { if (!response.readableEnded) res.destroy(); });
      res.writeHead(response.statusCode ?? 502, headersFor(response.headers));
      await new Promise(resolve => {
        response.once('end', resolve); response.once('error', resolve); response.once('aborted', resolve); response.once('close', resolve);
        res.once('close', resolve); response.pipe(res);
      });
      if (response.complete || response.readableEnded) { completed = true; counts.completed++; }
    } catch {
      if (!signal.aborted) { counts.failed++; errorReply(res, 502, 'messages_upstream_unavailable'); }
    } finally {
      clearTimeout(timer); releaseBody(); active.delete(stop);
      if (!completed) controller.abort();
    }
  }
  return {
    server,
    handleOfficial: (req, res) => handle(req, res, true),
    listen(port = 0) {
      if (closing || server.listening) return Promise.reject(new Error('broker_not_startable'));
      return new Promise((resolve, reject) => {
        const failed = error => { server.off('listening', started); reject(error); };
        const started = () => { server.off('error', failed); resolve(server.address()); };
        server.once('error', failed); server.once('listening', started); server.listen(port, '127.0.0.1');
      });
    },
    status: () => ({ ...counts, active: active.size, bufferedBytes, closing }),
    close() {
      if (closePromise) return closePromise;
      closing = true; for (const stop of active) stop();
      closePromise = new Promise(resolve => { server.close(resolve); server.closeAllConnections(); });
      return closePromise;
    },
  };
}
