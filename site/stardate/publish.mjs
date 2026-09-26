#!/usr/bin/env node
// Publish the Stardate log: render every Markdown entry to a static page, rebuild the
// log index and refresh the home page's latest-entries block. Zero dependencies.
//   node site/stardate/publish.mjs          write the pages
//   node site/stardate/publish.mjs --check  exit 1 if any published page is out of date
import { readFile, writeFile, readdir, unlink } from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { buildAll } from './stardate.mjs';

const site = path.dirname(path.dirname(fileURLToPath(import.meta.url)));
const check = process.argv.includes('--check');
const { entries, errors, files } = await buildAll(site);
if (errors.length) {
  console.error(`Stardate: ${errors.length} problem(s) in the entries; nothing was written.`);
  for (const error of errors) console.error('  - ' + error);
  process.exit(1);
}
const stale = [];
for (const [relative, content] of files) {
  const target = path.join(site, relative);
  const current = await readFile(target, 'utf8').catch(() => null);
  if (current === content) continue;
  stale.push(relative);
  if (!check) await writeFile(target, content);
}
// Remove pages whose entry file is gone, so a deleted entry never lingers online.
const published = path.join(site, 'assets', 'stardate');
for (const name of await readdir(published)) {
  if (!name.endsWith('.html') || files.has(`assets/stardate/${name}`)) continue;
  stale.push(`assets/stardate/${name} (orphan)`);
  if (!check) await unlink(path.join(published, name));
}
if (check && stale.length) {
  console.error(`Stardate: published pages are out of date. Run: node site/stardate/publish.mjs\n  - ${stale.join('\n  - ')}`);
  process.exit(1);
}
console.log(check
  ? `Stardate: ${entries.length} entries; every published page is current.`
  : `Stardate: ${entries.length} entries; ${stale.length ? 'updated ' + stale.join(', ') : 'nothing to update'}.`);
