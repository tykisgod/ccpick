import fs from 'node:fs/promises';
import net from 'node:net';
import { fail } from './core.mjs';

export function upstreamProxy(value) {
  let url;
  try { url = new URL(value); } catch { fail('upstream_proxy_invalid'); }
  if (url.protocol !== 'http:' || !['127.0.0.1', '[::1]'].includes(url.hostname) ||
      !url.port || url.username || url.password || url.pathname !== '/' || url.search || url.hash)
    fail('upstream_proxy_invalid');
  return url;
}

/** A user-owned CONNECT proxy supplies routing. No discovery, credentials,
 * firewall changes, endpoint probes, or direct fallback are performed here. */
export async function prepareNetwork(config, { requireBrowser = true, connect = net.connect } = {}) {
  const url = upstreamProxy(config.upstreamProxy);
  await fs.access(config.native);
  if (requireBrowser) await fs.access(config.browser);
  await new Promise((resolve, reject) => {
    let done = false;
    const socket = connect({ host: url.hostname.replace(/[\[\]]/g, ''), port: Number(url.port) });
    const finish = error => {
      if (done) return; done = true; clearTimeout(timer); socket.destroy();
      error ? reject(new Error('upstream_proxy_unavailable')) : resolve();
    };
    const timer = setTimeout(() => finish(true), 3000);
    socket.once('error', finish); socket.once('connect', () => finish());
  });
  return { proxy: url.href };
}
