import fs from 'node:fs/promises';
import path from 'node:path';
import os from 'node:os';
import { readJson, fail } from './core.mjs';

export async function syncResources(config, p) {
  if (p.storage === 'native-default') return;
  const template = await readJson(path.join(config.dataRoot, '..', 'template.json'), true);
  if (!template?.sharedResources) return;
  const base = path.join(os.homedir(), '.claude');
  for (const name of template.sharedResources) {
    if (!['CLAUDE.md', 'skills', 'commands', 'agents'].includes(name)) fail('invalid_template');
    const source = path.join(base, name), target = path.join(p.configDirectory, name);
    const info = await fs.lstat(source);
    if (info.isSymbolicLink()) fail('unsafe_shared_resource');
    if (name === 'CLAUDE.md') {
      if (!info.isFile()) fail('unsafe_shared_resource');
      const old = await fs.lstat(target).catch(e => { if (e.code !== 'ENOENT') throw e; return null; });
      if (old?.isSymbolicLink()) fail('unsafe_shared_resource');
      const content = await fs.readFile(source);
      if (!old || !content.equals(await fs.readFile(target))) await fs.writeFile(target, content, { mode: 0o600 });
    } else {
      if (!info.isDirectory()) fail('unsafe_shared_resource');
      try { await fs.symlink(source, target, process.platform === 'win32' ? 'junction' : 'dir'); }
      catch (e) {
        if (e.code !== 'EEXIST') throw e;
        if (path.resolve(await fs.realpath(target)) !== path.resolve(source)) fail('shared_resource_conflict');
      }
    }
  }
}
