import http from 'node:http';
import https from 'node:https';
import tls from 'node:tls';

const endpoints = new Map([
  ['profile', ['api.anthropic.com', '/api/oauth/profile', 'GET']],
  ['refresh', ['platform.claude.com', '/v1/oauth/token', 'POST']],
  ['login', ['platform.claude.com', '/v1/oauth/token', 'POST']],
]);

export function houseJson(proxy, kind, { token, body, timeoutMs = 30_000, ca } = {}) {
  const endpoint = endpoints.get(kind);
  if (!endpoint) throw new Error('unsupported_account_endpoint');
  if (token !== undefined && (typeof token !== 'string' || !token || /[\x00-\x20\x7f]/.test(token)))
    throw new Error('auth_unverified');
  const url = new URL(proxy);
  if (url.protocol !== 'http:' || !['127.0.0.1', '[::1]'].includes(url.hostname) ||
      !url.port || url.username || url.password || url.pathname !== '/' || url.search || url.hash)
    throw new Error('invalid_house_proxy');
  const [host, pathname, method] = endpoint;
  return new Promise((resolve, reject) => {
    let connector, socket, secure, request, agent, finished = false;
    const stop = (error, value) => {
      if (finished) return;
      finished = true; clearTimeout(timer);
      connector?.destroy(); request?.destroy(); secure?.destroy(); socket?.destroy(); agent?.destroy();
      if (error) reject(error); else resolve(value);
    };
    const timer = setTimeout(() => stop(new Error('network_unavailable')), timeoutMs);
    connector = http.request({ hostname: url.hostname.replaceAll(/[\[\]]/g, ''), port: Number(url.port),
      method: 'CONNECT', path: `${host}:443`, headers: { host: `${host}:443` }, agent: false });
    connector.on('error', () => stop(new Error('network_unavailable')));
    connector.on('response', () => stop(new Error('network_unavailable')));
    connector.on('connect', (response, tunnel, head) => {
      if (finished) { tunnel.destroy(); return; }
      socket = tunnel;
      if (response.statusCode !== 200 || head.length) return stop(new Error('network_unavailable'));
      secure = tls.connect({ socket, servername: host, rejectUnauthorized: true, ca: ca ?? tls.rootCertificates });
      secure.on('error', () => stop(new Error('network_unavailable')));
      secure.once('secureConnect', () => {
        if (finished) { secure.destroy(); return; }
        try {
        const data = body === undefined ? undefined : Buffer.from(JSON.stringify(body));
        agent = new https.Agent({ keepAlive: false });
        agent.createConnection = () => secure;
        request = https.request({ hostname: host, port: 443, path: pathname, method, agent,
          headers: { host, 'content-type': 'application/json', 'cache-control': 'no-cache',
            ...(token ? { authorization: `Bearer ${token}` } : {}),
            ...(data ? { 'content-length': data.length } : {}) } }, response => {
          const chunks = []; let size = 0;
          response.on('data', chunk => {
            size += chunk.length;
            if (size > 1024 * 1024) stop(new Error('auth_unverified'));
            else chunks.push(chunk);
          });
          response.on('error', () => stop(new Error('network_unavailable')));
          response.on('aborted', () => stop(new Error('network_unavailable')));
          response.on('close', () => { if (!response.complete) stop(new Error('network_unavailable')); });
          response.on('end', () => {
            const code = response.statusCode;
            if (code < 200 || code >= 300) {
              const error = new Error(code === 401 || code === 400 ? 'login_required' :
                code === 403 ? 'auth_forbidden' : code === 429 ? 'auth_rate_limited' : 'auth_unverified');
              error.status = code;
              return stop(error);
            }
            try { stop(null, JSON.parse(Buffer.concat(chunks).toString('utf8'))); }
            catch { stop(new Error('auth_unverified')); }
          });
        });
        request.on('error', () => stop(new Error('network_unavailable')));
        request.end(data);
        } catch { stop(new Error('auth_unverified')); }
      });
    });
    connector.end();
  });
}
