// Check the published static surface, including links followed by a plain HTTP agent.
import assert from 'node:assert/strict';
import { readFile, stat } from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const site = path.dirname(fileURLToPath(import.meta.url));
const repo = path.dirname(site);
const origin = 'https://borg.utlyze.com/';
const pages = ['index.html', 'guide.html'];
const ids = new Map();
const localFile = pathname => path.join(pathname.startsWith('/platform/') ? repo : site, pathname);
for (const page of pages) {
  const html = await readFile(path.join(site, page), 'utf8');
  const matches = [...html.matchAll(/\bid="([^"]+)"/g)].map(match => match[1]);
  assert.equal(new Set(matches).size, matches.length, `Duplicate ID in ${page}`);
  ids.set('/' + page, new Set(matches));
}
let checked = 0;
async function check(href, from) {
  const url = new URL(href.replaceAll('&amp;', '&'), new URL(from, origin));
  if (url.origin !== new URL(origin).origin) return;
  const pathname = url.pathname.endsWith('/') ? url.pathname + 'index.html' : url.pathname;
  assert((await stat(localFile(pathname))).isFile(), `Missing target ${href} from ${from}`);
  if (url.hash && ids.has(pathname)) {
    assert(ids.get(pathname).has(decodeURIComponent(url.hash.slice(1))), `Missing anchor ${href} from ${from}`);
  }
  checked++;
}
for (const page of pages) {
  const html = await readFile(path.join(site, page), 'utf8');
  for (const match of html.matchAll(/\b(?:href|src)="([^"]+)"/g)) await check(match[1], page);
}
for (const document of ['llms.txt', 'agent-guide.md']) {
  const text = await readFile(path.join(site, document), 'utf8');
  assert(text.length > 500, `${document} must contain readable orientation`);
  for (const match of text.matchAll(/https:\/\/borg\.utlyze\.com\/[^\s)<>"`\]]*/g)) {
    await check(match[0], document);
  }
}
console.log(`Verified ${checked} local page, asset and agent-document links.`);
