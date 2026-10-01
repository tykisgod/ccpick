import fs from 'node:fs/promises';
import path from 'node:path';
import { X509Certificate, createPrivateKey, randomUUID } from 'node:crypto';
import { executeNative, regular, fail } from './core.mjs';

export async function runtimeCertificate(root, { openssl } = {}) {
  const certFile = path.join(root, 'api-cert.pem'), keyFile = path.join(root, 'api-key.pem');
  try {
    await regular(certFile); await regular(keyFile);
  } catch (error) {
    if (error.code !== 'ENOENT') throw error;
    if (await fs.stat(certFile).catch(() => null) || await fs.stat(keyFile).catch(() => null))
      fail('runtime_certificate_incomplete');
    const executable = openssl ?? (process.platform === 'win32'
      ? path.join(process.env.ProgramFiles ?? 'C:/Program Files', 'Git/usr/bin/openssl.exe') : '/usr/bin/openssl');
    const requestFile = path.join(root, `.certificate-${randomUUID()}.cnf`);
    await fs.writeFile(requestFile, '[req]\nprompt=no\ndistinguished_name=dn\nx509_extensions=server\n' +
      '[dn]\nCN=CCPick local API channel\n[server]\n' +
      'subjectAltName=DNS:api.anthropic.com,DNS:platform.claude.com\n' +
      'extendedKeyUsage=serverAuth\nbasicConstraints=critical,CA:FALSE\n', { mode: 0o600, flag: 'wx' });
    let result;
    try {
      result = await executeNative(executable, ['req', '-x509', '-newkey', 'rsa:2048', '-sha256',
        '-nodes', '-days', '730', '-keyout', keyFile, '-out', certFile, '-config', requestFile],
        { capture: true, timeoutMs: 20_000 });
    } finally { await fs.unlink(requestFile).catch(() => {}); }
    if (result.code) fail('runtime_certificate_unavailable');
    await fs.chmod(certFile, 0o600); await fs.chmod(keyFile, 0o600);
  }
  const cert = await fs.readFile(certFile), key = await fs.readFile(keyFile);
  const parsed = new X509Certificate(cert);
  if (parsed.checkHost('api.anthropic.com') !== 'api.anthropic.com' ||
      parsed.checkHost('platform.claude.com') !== 'platform.claude.com' ||
      !parsed.checkPrivateKey(createPrivateKey(key)) || new Date(parsed.validFrom).getTime() > Date.now() ||
      new Date(parsed.validTo).getTime() <= Date.now() + 7 * 86400_000)
    fail('runtime_certificate_expired');
  return { cert, key, certFile, fingerprint: parsed.fingerprint256 };
}
