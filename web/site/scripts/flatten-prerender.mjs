// Cloudflare sirve `/precios` desde `precios.html` sin redirigir; con `precios/index.html`
// respondería 307 hacia `/precios/`.
import { readdirSync, renameSync, rmdirSync, statSync } from 'node:fs';
import { join } from 'node:path';

const root = new URL('../dist/site/', import.meta.url).pathname;

function flatten(dir) {
  for (const name of readdirSync(dir)) {
    const path = join(dir, name);
    if (!statSync(path).isDirectory()) {
      continue;
    }
    flatten(path);
    const entries = readdirSync(path);
    if (entries.includes('index.html')) {
      renameSync(join(path, 'index.html'), `${path}.html`);
      if (entries.length === 1) {
        rmdirSync(path);
      }
    }
  }
}

flatten(root);
