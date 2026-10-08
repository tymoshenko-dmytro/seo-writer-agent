// Builds ../../static/vendor/tiptap/tiptap.js from entry.js and writes LICENSES.md next to it:
// every package that went into the file, its version and its licence. See ui/vendor/README.md.
import { build } from 'esbuild';
import { existsSync, readFileSync, readdirSync, writeFileSync } from 'node:fs';
import { join } from 'node:path';

const out = '../../static/vendor/tiptap';
const r = await build({ entryPoints: ['entry.js'], bundle: true, minify: true, format: 'iife',
                        outfile: join(out, 'tiptap.js'), legalComments: 'none', metafile: true, logLevel: 'warning' });
const pkgs = new Set();
for (const f of Object.keys(r.metafile.inputs)) {
  const m = f.match(/node_modules\/((?:@[^/]+\/)?[^/]+)\//);
  if (m) pkgs.add(m[1]);
}
let md = '# The licences of what ui/static/vendor/tiptap/tiptap.js is built from\n\n'
       + 'Each package bundled into the file, its version and its licence, as shipped in the package.\n';
for (const p of [...pkgs].sort()) {
  const dir = join('node_modules', p);
  const meta = JSON.parse(readFileSync(join(dir, 'package.json'), 'utf8'));
  const lic = readdirSync(dir).find((f) => /^licen[cs]e/i.test(f));
  md += `\n## ${p} ${meta.version} (${meta.license})\n\n`
      + (lic && existsSync(join(dir, lic)) ? readFileSync(join(dir, lic), 'utf8').trim() + '\n' : '(no licence file in the package)\n');
}
writeFileSync(join(out, 'LICENSES.md'), md);
console.log(`${pkgs.size} packages`);
