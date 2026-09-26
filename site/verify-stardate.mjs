// Stardate log acceptance.
//   node site/verify-stardate.mjs     static checks: format, freshness, order, links, privacy
// verify.mjs also imports verifyStardate() for the browser checks (widths, keyboard, no-JS, axe).
import assert from 'node:assert/strict';
import { readFile, readdir, stat, writeFile } from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { buildAll, SECTIONS, STATUSES } from './stardate/stardate.mjs';

const site = path.dirname(fileURLToPath(import.meta.url));
const published = path.join(site, 'assets', 'stardate');

// Public-site privacy floor for everything written into the log. Values stay out of the output.
export const PRIVACY_PATTERNS = [
  ['email address', /[\w.+-]+@[\w-]+\.[\w.-]+/],
  ['numbered machine name', /\bstudio[-_ ]?\d+\b|\b[a-z][a-z0-9-]*-studio\b|\bmacbook\b|\bmac ?mini\b/i],
  ['user home or private path', /\/Users\/|~\/|\bLibrary\/|\/Projects\//i],
  ['IP address', /\b(?:\d{1,3}\.){3}\d{1,3}\b/],
  ['URL', /\bhttps?:\/\//i],
  ['session or run id', /\bsession-[0-9a-f]{4,}|\blive-\d{8}|\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-/i],
  ['internal hash or release id', /\b(?=[0-9a-f]*\d)(?=[0-9a-f]*[a-f])[0-9a-f]{8,}\b/i],
  ['tracker id', /\beco-[a-z0-9]{4,}/i],
  ['token or key', /\b(?:sk-|ghp_|gho_|xox[abprs]-|AKIA)[A-Za-z0-9_-]{8,}|BEGIN [A-Z ]*PRIVATE KEY/],
  ['file name', /\b[\w-]+\.(?:py|json|jsonl|plist|sqlite|db|sh|log)\b/i],
  ['environment variable', /\b[A-Z][A-Z0-9]+_[A-Z0-9_]{3,}\b/],
  ['provider company name', /\b(?:anthropic|openai|xai)\b/i],
];

// Names of people, clients, accounts and machines must never be written into this public repo,
// not even as test patterns. Keep them in a private file (one term per line) and point
// STARDATE_DENYLIST at it; the lead's privacy review runs with it set.
async function privateTerms() {
  if (!process.env.STARDATE_DENYLIST) return [];
  const lines = (await readFile(process.env.STARDATE_DENYLIST, 'utf8')).split('\n').map(line => line.trim());
  const terms = lines.filter(line => line && !line.startsWith('#'));
  const escape = term => term.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
  return terms.length ? [['private denylist term', new RegExp(`\\b(?:${terms.map(escape).join('|')})\\b`, 'i')]] : [];
}

function visibleText(html) {
  return html.replace(/<script[\s\S]*?<\/script>/g, ' ').replace(/<style[\s\S]*?<\/style>/g, ' ')
    .replace(/<\/(?:td|th|li|p|h\d)>/g, ' | ').replace(/<[^>]+>/g, ' ').replace(/&[a-z]+;/g, ' ').replace(/\s+/g, ' ');
}

export function privacyFindings(label, text, patterns = PRIVACY_PATTERNS) {
  return patterns.filter(([, pattern]) => pattern.test(text)).map(([name]) => `${label}: ${name}`);
}

export async function verifyStardateStatic() {
  const checks = [];
  const { entries, errors, files } = await buildAll(site);
  assert.deepEqual(errors, [], `Entry format problems:\n${errors.join('\n')}`);
  assert(entries.length >= 8, 'The log must cover at least the eight founding experiments');
  checks.push(`${entries.length} entries parse: front matter, stardate = year + day of year, status in ${STATUSES.join('/')}, sections ${SECTIONS.join(' / ')}`);

  for (const [relative, expected] of files) {
    const actual = await readFile(path.join(site, relative), 'utf8').catch(() => null);
    assert.equal(actual, expected, `${relative} is stale; run node site/stardate/publish.mjs`);
  }
  const pages = (await readdir(published)).filter(name => name.endsWith('.html'));
  assert.deepEqual(pages.sort(), [...files.keys()].filter(key => key.startsWith('assets/stardate/')).map(key => path.basename(key)).sort(),
    'Published pages and entries must match one to one');
  checks.push(`${files.size} published files are byte-identical to a fresh render (no stale or orphan pages)`);

  const index = await readFile(path.join(published, 'index.html'), 'utf8');
  const order = [...index.matchAll(/href="\.\/(\d{4}-\d{3}-[a-z0-9-]+)\.html"/g)].map(match => match[1]);
  assert.deepEqual(order, entries.map(entry => entry.slug), 'Index must list every entry exactly once');
  for (let i = 1; i < entries.length; i++) {
    const [a, b] = [entries[i - 1].meta, entries[i].meta];
    assert(a.stardate > b.stardate || (a.stardate === b.stardate && (a.time || '') >= (b.time || '')), `Index order is not newest first at ${entries[i].slug}`);
  }
  checks.push('Index lists every entry once, newest first; home page shows the three newest');

  for (const entry of entries) {
    const html = await readFile(path.join(published, entry.page), 'utf8');
    const ids = [...html.matchAll(/\bid="([^"]+)"/g)].map(match => match[1]);
    assert.equal(new Set(ids).size, ids.length, `Duplicate id in ${entry.page}`);
    for (const section of SECTIONS) assert(html.includes(`</span> ${section}</h2>`), `${entry.page} lacks section ${section}`);
    for (const match of html.matchAll(/\b(?:href|src)="([^"#]+)(?:#[^"]*)?"/g)) {
      const target = new URL(match[1], 'https://borg.utlyze.com/assets/stardate/');
      if (target.origin !== 'https://borg.utlyze.com') {
        assert(target.href.startsWith('https://github.com/h3ro-dev/borg'), `Unexpected external link in ${entry.page}`);
        continue;
      }
      const file = target.pathname.endsWith('/') ? target.pathname + 'index.html' : target.pathname;
      assert((await stat(path.join(site, file))).isFile(), `Missing target ${match[1]} from ${entry.page}`);
    }
  }
  checks.push('Every entry page has the four sections, unique ids and resolving local links');

  const patterns = [...PRIVACY_PATTERNS, ...await privateTerms()];
  const findings = [];
  const entryDir = path.join(site, 'stardate', 'entries');
  for (const name of await readdir(entryDir)) {
    if (name.endsWith('.md')) findings.push(...privacyFindings(name, await readFile(path.join(entryDir, name), 'utf8'), patterns));
  }
  for (const name of pages) findings.push(...privacyFindings(name, visibleText(await readFile(path.join(published, name), 'utf8')), patterns));
  assert.deepEqual(findings, [], `Privacy patterns found:\n${findings.join('\n')}`);
  checks.push(`Privacy floor: ${patterns.length} pattern classes${process.env.STARDATE_DENYLIST ? ' (with the private denylist)' : ''}, zero hits in entry sources and published page text`);
  return checks;
}

export async function verifyStardate({ browser, origin, evidence, results, axeScript = process.env.AXE_SCRIPT }) {
  const index = `${origin}/borg/assets/stardate/`;
  const context = await browser.newContext();
  const page = await context.newPage();
  const errors = [];
  page.on('pageerror', error => errors.push(error.message));
  page.on('response', response => { if (response.status() >= 400) errors.push(`${response.status()} ${response.url()}`); });
  const audits = [];
  for (const width of [320, 768, 1280, 1920]) {
    await page.setViewportSize({ width, height: width < 768 ? 844 : 1000 });
    for (const url of [index, `${index}2026-268-grok-hooks-ab.html`]) {
      await page.goto(url);
      await page.evaluate(() => document.fonts.ready);
      assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), `Stardate overflow at ${width}: ${url}`);
      assert(await page.locator('h1').isVisible());
      if (axeScript) {
        await page.addScriptTag({ path: axeScript });
        const audit = await page.evaluate(() => axe.run(document, { runOnly: { type: 'tag', values: ['wcag2a', 'wcag2aa', 'wcag21aa'] } }));
        audits.push({ width, page: url.replace(origin, ''), violations: audit.violations.map(v => ({ id: v.id, nodes: v.nodes.length })), passes: audit.passes.length });
      }
    }
  }
  if (audits.length) {
    await writeFile(path.join(evidence, 'stardate-accessibility.json'), JSON.stringify(audits, null, 2));
    assert(audits.every(audit => audit.violations.length === 0), 'Stardate accessibility violations; see evidence/stardate-accessibility.json');
    results.checks.push('Stardate index and entry: axe WCAG 2 A/AA and 2.1 AA zero violations at 320/768/1280/1920');
  }
  results.checks.push('Stardate index and entry fit 320/768/1280/1920 with no horizontal overflow');

  await page.setViewportSize({ width: 1280, height: 1000 });
  await page.goto(index);
  await page.keyboard.press('Tab');
  assert(await page.locator('.skip-link').evaluate(el => el === document.activeElement));
  await page.keyboard.press('Enter');
  assert.equal(new URL(page.url()).hash, '#main');
  let focusedEntry = false;
  for (let i = 0; i < 20 && !focusedEntry; i++) {
    await page.keyboard.press('Tab');
    focusedEntry = await page.evaluate(() => !!document.activeElement.closest('.sd-card'));
  }
  assert(focusedEntry, 'Keyboard reaches the first entry card');
  const ring = await page.evaluate(() => getComputedStyle(document.activeElement.closest('.sd-card')).outlineStyle);
  assert.notEqual(ring, 'none', 'Focused entry card shows a visible outline');
  await page.keyboard.press('Enter');
  await page.waitForURL(/\d{4}-\d{3}-[a-z0-9-]+\.html$/);
  assert.equal(await page.locator('.sd-section h2').count(), 4);
  results.checks.push('Stardate keyboard path: skip link, visible focus on entry cards, Enter opens the entry with its four sections');

  const plain = await browser.newContext({ javaScriptEnabled: false, viewport: { width: 390, height: 844 } });
  const nojs = await plain.newPage();
  await nojs.goto(index);
  const cards = await nojs.locator('.sd-card').count();
  const expected = (await readdir(published)).filter(name => /^\d{4}-\d{3}-.+\.html$/.test(name)).length;
  assert.equal(cards, expected, 'Every entry is listed without JavaScript');
  for (const href of await nojs.locator('.sd-card h4 a').evaluateAll(links => links.map(link => link.href))) {
    await nojs.goto(href);
    assert(await nojs.locator('#lab-book-log').isVisible());
    assert(await nojs.locator('.sd-log li').first().isVisible());
  }
  await nojs.goto(`${origin}/borg/`);
  assert.equal(await nojs.locator('.log-latest .sd-card').count(), 3);
  await plain.close();
  results.checks.push(`No-JavaScript: all ${expected} entries listed and every entry page readable in full; home shows the three newest`);
  await page.screenshot({ path: path.join(evidence, 'stardate-entry-1280.png'), fullPage: true });
  results.screenshots.push('evidence/stardate-entry-1280.png');
  assert.deepEqual(errors, [], 'Stardate pages load without script or asset errors');
  await context.close();
}

if (import.meta.url === pathToFileURL(process.argv[1]).href) {
  try {
    for (const check of await verifyStardateStatic()) console.log('PASS ' + check);
  } catch (error) {
    console.error('FAIL ' + error.message);
    process.exitCode = 1;
  }
}
