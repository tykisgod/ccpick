import http from 'node:http';
import https from 'node:https';
import tls from 'node:tls';
import { createHash, timingSafeEqual, X509Certificate } from 'node:crypto';

const API = 'api.anthropic.com';
const OAUTH = 'platform.claude.com';
const LOCAL_HEADER = 'x-ccpick-account-runtime';
const MODEL_PATHS = new Set(['/v1/messages', '/v1/messages/count_tokens']);
const HOP = new Set(['connection', 'keep-alive', 'proxy-authenticate', 'proxy-authorization',
  'te', 'trailer', 'transfer-encoding', 'upgrade']);
const API_HEADERS = new Set(['authorization', 'content-type', 'accept', 'accept-encoding',
  'user-agent', 'anthropic-version', 'anthropic-beta', 'cache-control', 'pragma', 'if-none-match']);
const IDENTITY_HEADERS = /^(?:x-(?:organization|organisation|account|device|machine)(?:-|$)|x-cc-atis$)/i;
const digest = value => createHash('sha256').update(value).digest();
const loopback = host => ['127.0.0.1', '::1', '::ffff:127.0.0.1'].includes(host);
const fail = reason => Object.assign(new Error(reason), { reason });

function reply(res, status, reason) {
  if (res.destroyed || res.writableEnded) return;
  if (res.headersSent) { res.destroy(); return; }
  const body = JSON.stringify({ type: 'error', error: { type: 'permission_error', message: reason }, reason });
  res.writeHead(status, { 'content-type': 'application/json', 'content-length': Buffer.byteLength(body),
    'cache-control': 'no-store', connection: 'close' });
  res.end(body);
}

function connectReply(socket, status, reason) {
  if (socket.destroyed) return;
  const body = JSON.stringify({ reason });
  socket.end(`HTTP/1.1 ${status} ${status === 403 ? 'Forbidden' : 'Bad Gateway'}\r\n` +
    `Content-Type: application/json\r\nContent-Length: ${Buffer.byteLength(body)}\r\nConnection: close\r\n\r\n${body}`);
}

function selectorUrl(value) {
  let url;
  try { url = new URL(value); } catch { throw fail('invalid_guard_selector'); }
  if (url.protocol !== 'http:' || !['127.0.0.1', '[::1]'].includes(url.hostname) || !url.port ||
      url.username || url.password || url.pathname !== '/' || url.search || url.hash)
    throw fail('invalid_guard_selector');
  return url;
}

function authority(value) {
  if (typeof value !== 'string' || !/^(?:[a-z0-9.-]+|\[[a-f0-9:]+\]):443$/i.test(value)) return null;
  try {
    const url = new URL(`https://${value}`);
    const hostname = url.hostname.toLowerCase().replace(/\.$/, '');
    if (!hostname || hostname.includes('..')) return null;
    return { hostname, target: `${hostname}:443` };
  } catch { return null; }
}

function uniqueHeaders(req) {
  const seen = new Set();
  for (let i = 0; i < req.rawHeaders.length; i += 2) {
    const name = req.rawHeaders[i].toLowerCase();
    if (['host', 'authorization', 'content-length', 'transfer-encoding', LOCAL_HEADER].includes(name) && seen.has(name)) return false;
    seen.add(name);
  }
  return true;
}

function validOrigin(req, host) {
  return loopback(req.socket.remoteAddress) && req.socket.servername?.toLowerCase() === host &&
    new RegExp(`^${host.replaceAll('.', '\\.')}(:443)?$`, 'i').test(req.headers.host ?? '') &&
    req.headers.origin === undefined && uniqueHeaders(req);
}

function route(req) {
  const raw = req.url;
  if (typeof raw !== 'string' || !raw.startsWith('/') || raw.startsWith('//') || /[\s\\#]/.test(raw))
    throw fail('api_path_invalid');
  const url = new URL(raw, `https://${API}`);
  if (raw.split('?')[0] !== url.pathname || /%/.test(url.pathname)) throw fail('api_path_invalid');
  const pathname = url.pathname;
  const params = [...url.searchParams];
  if (new Set(params.map(([key]) => key)).size !== params.length) throw fail('api_query_unsupported');
  if (MODEL_PATHS.has(pathname)) {
    if (req.method !== 'POST') throw fail('api_method_unsupported');
    if (params.some(([key, value]) => key !== 'beta' || value !== 'true')) throw fail('api_query_unsupported');
    return { model: true };
  }
  if (pathname === '/v1/mcp_servers' || pathname.startsWith('/v1/mcp/')) throw fail('hosted_mcp_account_switch_unsupported');
  if (pathname.startsWith('/api/ws/')) throw fail('voice_account_switch_unsupported');
  if (pathname.startsWith('/v1/code/') || pathname.startsWith('/v1/environment') || pathname.startsWith('/v1/session_ingress/') ||
      pathname.startsWith('/api/oauth/organizations/')) throw fail('cloud_session_account_switch_unsupported');
  const get = new Set(['/api/oauth/profile', '/api/oauth/claude_cli/roles', '/api/oauth/usage',
    '/api/claude_code/organizations/metrics_enabled', '/api/claude_code/policy_limits', '/api/hello', '/api/web/domain_info']);
  if (!(get.has(pathname) && req.method === 'GET') && !(pathname === '/api/oauth/validate' && req.method === 'POST'))
    throw fail('api_route_unsupported');
  if (pathname === '/api/web/domain_info') {
    if (params.length !== 1 || params[0][0] !== 'domain' || params[0][1].length > 253 ||
        !/^(?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.)*[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.?$/i.test(params[0][1]))
      throw fail('api_query_unsupported');
  } else if (pathname === '/api/oauth/usage') {
    if (params.some(([key, value]) => !['at_wall', 'skip_spend', 'cedar_ember'].includes(key) || value !== '1'))
      throw fail('api_query_unsupported');
  } else if (params.length) throw fail('api_query_unsupported');
  return { model: false, anonymous: pathname === '/api/hello' || pathname === '/api/web/domain_info',
    nullable: pathname === '/api/oauth/validate' };
}

function responseHeaders(headers) {
  const blocked = new Set(HOP);
  for (const part of String(headers.connection ?? '').split(',')) blocked.add(part.trim().toLowerCase());
  return Object.fromEntries(Object.entries(headers).filter(([name]) => !blocked.has(name.toLowerCase())));
}

async function smallBody(req, timeoutMs) {
  return new Promise((resolve, reject) => {
    const chunks = []; let size = 0;
    const cleanup = () => {
      clearTimeout(timer); req.off('data', data); req.off('end', end); req.off('error', error); req.off('aborted', error);
    };
    const error = () => { cleanup(); req.pause(); reject(fail('api_request_incomplete')); };
    const data = chunk => {
      size += chunk.length;
      if (size > 64) { cleanup(); req.pause(); reject(fail('api_body_unsupported')); }
      else chunks.push(chunk);
    };
    const end = () => { cleanup(); resolve(Buffer.concat(chunks)); };
    const timer = setTimeout(error, timeoutMs); timer.unref();
    req.on('data', data); req.once('end', end); req.once('error', error); req.once('aborted', error);
  });
}

/** Child-scoped official-host TLS boundary. Caller owns CA issuance, its private
 * files and NODE_EXTRA_CA_CERTS on the protected process tree. Never install
 * this CA into system/browser trust. No requests, credentials or URLs are logged.
 *
 * messagesHandler(req,res) receives the unconsumed model stream and local key.
 * oauthHandler(req,res), when supplied, owns bounded grant parsing, pending-login
 * state/challenge validation and grant persistence BEFORE replying to native.
 * It must reject refresh grants: refresh is owned by the account vault. Without
 * this handler platform.claude.com is blocked, never transparently tunneled.
 * apiHandler({method,path,headers,body,signal},res), when supplied, replaces only
 * the non-model transport (an offline-test seam). Default transport verifies
 * public upstream TLS via upstreamProxy; it never uses the supplied local CA.
 */
export function createNativeApiGuard(options = {}) {
  const proxy = selectorUrl(options.upstreamProxy);
  if (typeof options.messagesHandler !== 'function' ||
      (options.apiHandler !== undefined && typeof options.apiHandler !== 'function') ||
      (options.oauthHandler !== undefined && typeof options.oauthHandler !== 'function')) throw fail('invalid_guard_handler');
  const localKey = options.localKey;
  if (localKey !== undefined && (typeof localKey !== 'string' || !localKey || localKey.length > 4096 || /[\s\x00-\x1f\x7f]/.test(localKey)))
    throw fail('invalid_guard_local_key');
  const timeoutMs = options.connectTimeoutMs ?? 15000;
  if (!Number.isSafeInteger(timeoutMs) || timeoutMs < 1 || timeoutMs > 120000) throw fail('invalid_guard_timeout');
  let context;
  try {
    const cert = new X509Certificate(options.tls?.cert);
    if (!cert.checkHost(API, { subject: 'never' }) ||
        (options.oauthHandler && !cert.checkHost(OAUTH, { subject: 'never' }))) throw new Error();
    context = tls.createSecureContext({ cert: options.tls.cert, key: options.tls.key, minVersion: 'TLSv1.2' });
  } catch { throw fail('invalid_guard_tls_identity'); }
  const sockets = new Set(), outbound = new Set();
  const counts = { intercepted: 0, tunneled: 0, model: 0, api: 0, oauth: 0, rejected: 0, failed: 0 };
  let closing = false, closePromise;
  function track(socket) {
    sockets.add(socket); socket.once('close', () => sockets.delete(socket)); socket.on('error', () => {}); return socket;
  }
  function connect(target) {
    return new Promise((resolve, reject) => {
      if (closing) { reject(fail('guard_closed')); return; }
      let settled = false;
      const request = http.request({ hostname: proxy.hostname.replace(/^\[|\]$/g, ''), port: proxy.port,
        method: 'CONNECT', path: target, headers: { host: target }, agent: false });
      outbound.add(request);
      const done = () => { clearTimeout(timer); outbound.delete(request); };
      const error = () => { if (settled) return; settled = true; done(); request.destroy(); reject(fail('selector_connect_failed')); };
      const timer = setTimeout(error, timeoutMs); timer.unref();
      request.once('error', error);
      request.once('close', () => { if (!settled) error(); });
      request.once('connect', (response, socket, head) => {
        track(socket);
        if (settled || closing) { socket.destroy(); error(); return; }
        if (response.statusCode !== 200 || head.length) { socket.destroy(); error(); return; }
        settled = true; done(); resolve(socket);
      });
      request.end();
    });
  }

  async function forwardApi(input, res) {
    let socket, secure, agent, request, response, timer;
    const stop = () => { clearTimeout(timer); request?.destroy(); response?.destroy(); agent?.destroy(); secure?.destroy(); socket?.destroy(); };
    input.signal.addEventListener('abort', stop, { once: true });
    try {
      socket = await connect(`${API}:443`);
      if (input.signal.aborted) throw fail('api_request_incomplete');
      secure = track(tls.connect({ socket, servername: API, ca: tls.rootCertificates,
        rejectUnauthorized: true, ALPNProtocols: ['http/1.1'] }));
      await new Promise((resolve, reject) => {
        timer = setTimeout(() => reject(fail('api_tls_failed')), timeoutMs); timer.unref();
        secure.once('secureConnect', resolve); secure.once('error', () => reject(fail('api_tls_failed')));
        secure.once('close', () => reject(fail('api_tls_failed')));
      });
      clearTimeout(timer);
      if (input.signal.aborted) throw fail('api_request_incomplete');
      agent = new https.Agent({ keepAlive: false, maxSockets: 1 });
      agent.createConnection = (_options, callback) => { callback(null, secure); };
      await new Promise((resolve, reject) => {
        request = https.request({ protocol: 'https:', hostname: API, port: 443, path: input.path,
          method: input.method, headers: input.headers, agent }, incoming => {
          response = incoming; clearTimeout(timer);
          response.once('error', reject); response.once('aborted', () => reject(fail('api_response_incomplete')));
          response.once('end', resolve); res.once('close', resolve);
          res.writeHead(response.statusCode ?? 502, responseHeaders(response.headers)); response.pipe(res);
        });
        timer = setTimeout(() => reject(fail('api_response_timeout')), timeoutMs); timer.unref();
        request.once('error', reject); request.end(input.body);
      });
    } finally { input.signal.removeEventListener('abort', stop); stop(); }
  }

  async function handleApi(req, res) {
    req.on('error', () => {}); res.on('error', () => {});
    const reject = (status, reason) => { counts.rejected++; reply(res, status, reason); };
    if (closing) return reject(503, 'guard_closed');
    if (!validOrigin(req, API)) return reject(403, 'api_origin_rejected');
    let selected;
    try { selected = route(req); } catch (error) { return reject(403, error.reason ?? 'api_route_unsupported'); }
    if (selected.model) {
      const matches = value => typeof value === 'string' && timingSafeEqual(digest(value), digest(localKey));
      const header = req.headers[LOCAL_HEADER];
      const bearer = typeof req.headers.authorization === 'string' && req.headers.authorization.startsWith('Bearer ')
        ? req.headers.authorization.slice(7) : undefined;
      if (localKey !== undefined && ((header !== undefined && !matches(header)) || (!matches(header) && !matches(bearer))))
        return reject(403, 'model_local_key_required');
      counts.model++;
      try { await options.messagesHandler(req, res); }
      catch { counts.failed++; reply(res, 502, 'model_handler_failed'); }
      return;
    }
    if (Object.keys(req.headers).some(name => IDENTITY_HEADERS.test(name))) return reject(403, 'api_identity_header_unsupported');
    const headers = {};
    const nominated = new Set(String(req.headers.connection ?? '').toLowerCase().split(',').map(part => part.trim()));
    for (const [name, value] of Object.entries(req.headers)) {
      if (!API_HEADERS.has(name) || nominated.has(name)) continue;
      if (typeof value !== 'string' || (localKey !== undefined && value.includes(localKey))) return reject(403, 'api_local_credential_rejected');
      headers[name] = value;
    }
    if (!selected.anonymous && !/^Bearer [^\s]+$/i.test(headers.authorization ?? '')) return reject(401, 'api_bearer_required');
    const controller = new AbortController();
    const stop = () => controller.abort(); res.once('close', stop); req.once('aborted', stop);
    try {
      const body = await smallBody(req, timeoutMs);
      if (body.length && (!selected.nullable || body.toString('utf8').trim() !== 'null')) return reject(403, 'api_body_unsupported');
      if (req.method === 'POST') headers['content-length'] = String(body.length);
      if (controller.signal.aborted) return;
      counts.api++;
      await (options.apiHandler ?? forwardApi)({ method: req.method, path: req.url, headers, body, signal: controller.signal }, res);
    } catch (error) {
      counts.failed++;
      reply(res, 502, ['api_body_unsupported', 'api_request_incomplete'].includes(error.reason) ? error.reason : 'api_upstream_unavailable');
    } finally { res.off('close', stop); req.off('aborted', stop); }
  }

  async function handleOauth(req, res) {
    req.on('error', () => {}); res.on('error', () => {});
    const reject = reason => { counts.rejected++; reply(res, 403, reason); };
    if (closing) return reject('guard_closed');
    if (!validOrigin(req, OAUTH)) return reject('oauth_origin_rejected');
    if (req.method !== 'POST' || req.url !== '/v1/oauth/token') return reject('oauth_route_unsupported');
    if (req.headers.authorization !== undefined || Object.keys(req.headers).some(name => IDENTITY_HEADERS.test(name)))
      return reject('oauth_authorization_unsupported');
    delete req.headers[LOCAL_HEADER];
    counts.oauth++;
    try { await options.oauthHandler(req, res); }
    catch { counts.failed++; reply(res, 502, 'oauth_handler_failed'); }
  }

  function site(host, handler) {
    const api = https.createServer({ cert: options.tls.cert, key: options.tls.key, minVersion: 'TLSv1.2', ALPNProtocols: ['http/1.1'],
      SNICallback: (name, callback) => callback(name.toLowerCase() === host ? null : fail('api_tls_name_rejected'), context),
      requestTimeout: 0, headersTimeout: 60000, handshakeTimeout: timeoutMs }, (req, res) => { void handler(req, res); });
    api.on('secureConnection', track);
    api.on('tlsClientError', () => { counts.rejected++; });
    api.on('clientError', (_error, socket) => socket.destroy());
    api.on('connect', (_req, socket) => connectReply(socket, 403, 'nested_connect_unsupported'));
    api.on('upgrade', (_req, socket) => connectReply(socket, 403, 'voice_account_switch_unsupported'));
    return api;
  }
  const sites = new Map([[API, site(API, handleApi)]]);
  if (options.oauthHandler) sites.set(OAUTH, site(OAUTH, handleOauth));
  const server = http.createServer({ requestTimeout: 0, headersTimeout: 60000 }, (_req, res) => {
    counts.rejected++; reply(res, 403, 'https_connect_required');
  });
  server.on('connection', track);
  server.on('clientError', (_error, socket) => socket.destroy());
  server.on('upgrade', (_req, socket) => connectReply(socket, 403, 'https_connect_required'));
  server.on('connect', (req, socket, head) => {
    const target = authority(req.url);
    if (closing || !loopback(socket.remoteAddress) || !target || !uniqueHeaders(req) || req.headers.origin !== undefined) {
      counts.rejected++; connectReply(socket, 403, 'connect_target_rejected'); return;
    }
    if (target.hostname === 'mcp-proxy.anthropic.com') {
      counts.rejected++; connectReply(socket, 403, 'hosted_mcp_account_switch_unsupported'); return;
    }
    if (target.hostname === OAUTH && !options.oauthHandler) {
      counts.rejected++; connectReply(socket, 403, 'oauth_login_handler_required'); return;
    }
    if (sites.has(target.hostname)) {
      counts.intercepted++;
      socket.write('HTTP/1.1 200 Connection Established\r\n\r\n');
      if (head.length) socket.unshift(head);
      sites.get(target.hostname).emit('connection', socket); return;
    }
    void (async () => {
      let upstream;
      const stop = () => upstream?.destroy(); socket.once('close', stop);
      try {
        upstream = await connect(target.target);
        if (socket.destroyed || closing) { upstream.destroy(); return; }
        counts.tunneled++; socket.write('HTTP/1.1 200 Connection Established\r\n\r\n');
        upstream.once('close', () => socket.destroy()); upstream.once('error', () => socket.destroy());
        if (head.length) upstream.write(head);
        socket.pipe(upstream); upstream.pipe(socket);
      } catch { counts.failed++; connectReply(socket, 502, 'selector_connect_failed'); }
    })();
  });
  return {
    server,
    listen(port = 0, host = '127.0.0.1') {
      if (!['127.0.0.1', '::1'].includes(host) || closing || server.listening) return Promise.reject(fail('guard_not_startable'));
      return new Promise((resolve, reject) => {
        const error = () => { server.off('listening', started); reject(fail('guard_listen_failed')); };
        const started = () => { server.off('error', error); resolve(server.address()); };
        server.once('error', error); server.once('listening', started); server.listen(port, host);
      });
    },
    address: () => server.address(),
    status: () => ({ ...counts, sockets: sockets.size, closing }),
    close() {
      if (closePromise) return closePromise;
      closing = true; for (const req of outbound) req.destroy(); for (const socket of sockets) socket.destroy();
      closePromise = new Promise(resolve => server.close(resolve)); for (const api of sites.values()) api.close(); return closePromise;
    },
  };
}
