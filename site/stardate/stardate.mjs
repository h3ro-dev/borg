// Stardate log: parse, validate and render the Markdown entries. Zero dependencies.
// Entries are the source of truth; publish.mjs writes the static pages from them and
// verify-stardate.mjs proves the published pages still match.
import { readFile, readdir } from 'node:fs/promises';
import path from 'node:path';

export const STATUSES = ['running', 'finished', 'superseded'];
export const SECTIONS = ['What we did', 'What we learned', 'Open questions', 'Lab book log'];
export const FIELDS = ['stardate', 'title', 'date', 'time', 'status', 'summary'];
export const REQUIRED = ['stardate', 'title', 'date', 'status', 'summary'];
export const LATEST_START = '<!-- stardate:latest -->';
export const LATEST_END = '<!-- /stardate:latest -->';
const MONTHS = ['January', 'February', 'March', 'April', 'May', 'June', 'July', 'August',
  'September', 'October', 'November', 'December'];
const STAMP = /^(?:(\d{4}-\d{2}-\d{2})(?: (\d{2}:\d{2}))?|(\d{2}-\d{2}) (\d{2}:\d{2})|(\d{2}:\d{2})|([a-z][a-z-]*[a-z]))$/;

/** Stardate = four-digit year, a dot, three-digit day of the year (America/Denver date). */
export function stardateFor(date) {
  const [year, month, day] = date.split('-').map(Number);
  const start = Date.UTC(year, 0, 1);
  const ordinal = Math.round((Date.UTC(year, month - 1, day) - start) / 86400000) + 1;
  return `${year}.${String(ordinal).padStart(3, '0')}`;
}

function validDate(value) {
  if (!/^\d{4}-\d{2}-\d{2}$/.test(value)) return false;
  const [year, month, day] = value.split('-').map(Number);
  const date = new Date(Date.UTC(year, month - 1, day));
  return date.getUTCFullYear() === year && date.getUTCMonth() === month - 1 && date.getUTCDate() === day;
}

export function longDate(value) {
  const [year, month, day] = value.split('-').map(Number);
  return `${day} ${MONTHS[month - 1]} ${year}`;
}

export function escapeHtml(value) {
  return String(value).replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;');
}

const slugify = value => value.toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-|-$/g, '');

/** Split one Markdown file into front matter, the four sections and the lab lines. */
export function parseEntry(text, file) {
  const errors = [];
  const entry = { file, meta: {}, sections: new Map(), lab: [], errors };
  const source = text.replace(/\r\n/g, '\n');
  const match = source.match(/^---\n([\s\S]*?)\n---\n([\s\S]*)$/);
  if (!match) {
    errors.push('missing front matter block between --- lines');
    return entry;
  }
  for (const line of match[1].split('\n')) {
    if (!line.trim()) continue;
    const pair = line.match(/^([a-z_]+):\s*(.*)$/);
    if (!pair) { errors.push(`unreadable front matter line: ${line}`); continue; }
    const [, key, raw] = pair;
    if (!FIELDS.includes(key)) errors.push(`unknown front matter key: ${key}`);
    if (key in entry.meta) errors.push(`duplicate front matter key: ${key}`);
    entry.meta[key] = raw.replace(/^"(.*)"$/, '$1').replace(/^'(.*)'$/, '$1').trim();
  }
  let current = null;
  for (const line of match[2].split('\n')) {
    const heading = line.match(/^(#{1,6})\s+(.*?)\s*$/);
    if (heading) {
      if (heading[1] !== '##') errors.push(`only level-2 headings are allowed: ${line}`);
      current = heading[2];
      if (entry.sections.has(current)) errors.push(`duplicate section: ${current}`);
      entry.sections.set(current, []);
      continue;
    }
    if (current) entry.sections.get(current).push(line);
    else if (line.trim()) errors.push('text before the first section');
  }
  for (const [name, lines] of entry.sections) entry.sections.set(name, lines.join('\n').trim());
  for (const line of (entry.sections.get('Lab book log') || '').split('\n')) {
    if (!line.trim()) continue;
    const lab = line.match(/^- `([^`]+)` (\S.*)$/);
    if (!lab || !STAMP.test(lab[1])) { errors.push(`lab line must be "- \`STAMP\` text": ${line}`); continue; }
    entry.lab.push({ stamp: lab[1], text: lab[2] });
  }
  return entry;
}

/** Everything a writer (person or nightly job) can get wrong, as plain messages. */
export function validateEntry(entry) {
  const errors = [...entry.errors];
  const { meta } = entry;
  for (const key of REQUIRED) if (!meta[key]) errors.push(`missing front matter: ${key}`);
  if (meta.date && !validDate(meta.date)) errors.push(`date must be a real YYYY-MM-DD date: ${meta.date}`);
  if (meta.date && validDate(meta.date) && meta.stardate !== stardateFor(meta.date))
    errors.push(`stardate ${meta.stardate} does not match date ${meta.date} (expected ${stardateFor(meta.date)})`);
  if ('time' in meta && !/^([01]\d|2[0-3]):[0-5]\d$/.test(meta.time)) errors.push(`time must be HH:MM: ${meta.time}`);
  if (meta.status && !STATUSES.includes(meta.status)) errors.push(`status must be one of ${STATUSES.join(', ')}`);
  if (meta.title && meta.title.length > 80) errors.push('title is longer than 80 characters');
  if (meta.summary && meta.summary.length > 280) errors.push('summary is longer than 280 characters');
  const expected = meta.stardate ? meta.stardate.replace('.', '-') + '-' : '';
  const name = path.basename(entry.file || '');
  if (!new RegExp(`^${expected.replace('.', '\\.')}[a-z0-9]+(?:-[a-z0-9]+)*\\.md$`).test(name))
    errors.push(`file name must be ${expected}<slug>.md with a lowercase slug`);
  const names = [...entry.sections.keys()];
  if (names.join('|') !== SECTIONS.join('|'))
    errors.push(`sections must be exactly, in order: ${SECTIONS.join(' / ')} (found: ${names.join(' / ') || 'none'})`);
  for (const section of SECTIONS) if (entry.sections.has(section) && !entry.sections.get(section))
    errors.push(`section is empty: ${section}`);
  if (!entry.lab.length) errors.push('lab book log needs at least one line');
  const body = [...entry.sections.values()].join('\n');
  if (/<[a-z!/]/i.test(body)) errors.push('raw HTML is not allowed');
  if (/!?\[[^\]]*\]\(/.test(body)) errors.push('links and images are not allowed in entries');
  if (/^```/m.test(body)) errors.push('code blocks are not allowed');
  return errors;
}

export function sortEntries(entries) {
  return [...entries].sort((a, b) =>
    b.meta.stardate.localeCompare(a.meta.stardate)
    || (b.meta.time || '').localeCompare(a.meta.time || '')
    || a.slug.localeCompare(b.slug));
}

export async function loadEntries(site) {
  const dir = path.join(site, 'stardate', 'entries');
  const files = (await readdir(dir)).filter(name => name.endsWith('.md')).sort();
  const entries = [];
  for (const name of files) {
    const entry = parseEntry(await readFile(path.join(dir, name), 'utf8'), name);
    entry.errors = validateEntry(entry);
    entry.slug = name.replace(/\.md$/, '');
    entry.page = entry.slug + '.html';
    entries.push(entry);
  }
  return sortEntries(entries.filter(entry => entry.meta.stardate)).concat(entries.filter(entry => !entry.meta.stardate));
}

// ---------- Markdown subset -> HTML ----------

function inline(text) {
  return text.split(/(`[^`]+`)/).map(part => {
    if (/^`[^`]+`$/.test(part)) return `<code>${escapeHtml(part.slice(1, -1))}</code>`;
    return escapeHtml(part)
      .replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>')
      .replace(/(^|[^*\w])\*(?!\s)(.+?)\*(?!\w)/g, '$1<em>$2</em>')
      .replace(/ -- /g, ' – ');
  }).join('');
}

function renderList(lines) {
  const items = [];
  for (const line of lines) {
    const bullet = line.match(/^(\s*)(?:[-*]|(\d+)\.)\s+(.*)$/);
    if (bullet) items.push({ depth: bullet[1].length >= 2 ? 1 : 0, ordered: !!bullet[2], text: bullet[3] });
    else if (items.length) items[items.length - 1].text += ' ' + line.trim();
  }
  let html = '';
  let index = 0;
  const walk = depth => {
    const tag = items[index].ordered ? 'ol' : 'ul';
    html += `<${tag}>`;
    while (index < items.length && items[index].depth >= depth) {
      const item = items[index++];
      html += `<li>${inline(item.text)}`;
      if (index < items.length && items[index].depth > depth) walk(items[index].depth);
      html += '</li>';
    }
    html += `</${tag}>`;
  };
  while (index < items.length) walk(items[index].depth);
  return html;
}

function renderTable(lines, label) {
  const cells = line => line.trim().replace(/^\||\|$/g, '').split('|').map(cell => cell.trim());
  const [head, , ...rows] = lines;
  // A number, optionally with a unit and a few words ("535 extra copies"). Longer prose that merely starts with a
  // number ("90 kept by JEV; 3 held back ...") stays a normal, wrapping cell.
  const numeric = value => /^[−\-+~≈]?[$]?[\d.,]+(?:\s?[%×xs]|\s?ms|\s?GB|\s?pp)?(?:\s[^;]{0,16})?$/.test(value);
  const headers = cells(head);
  return `<div class="sd-table" role="region" tabindex="0" aria-label="${escapeHtml(label)}"><table>`
    + `<thead><tr>${headers.map(cell => `<th scope="col">${inline(cell)}</th>`).join('')}</tr></thead><tbody>`
    + rows.map(row => `<tr>${cells(row).map((cell, i) =>
      i === 0 ? `<th scope="row">${inline(cell)}</th>` : `<td${numeric(cell) ? ' class="num"' : ''}>${inline(cell)}</td>`).join('')}</tr>`).join('')
    + '</tbody></table></div>';
}

export function renderMarkdown(markdown, context = 'Data table') {
  const blocks = [];
  let buffer = [];
  let tables = 0;
  const flush = () => {
    if (!buffer.length) return;
    if (buffer.every(line => line.trim().startsWith('|'))) {
      tables++;
      blocks.push(renderTable(buffer, `${context}, table ${tables}`));
    } else if (/^\s*(?:[-*]|\d+\.)\s+/.test(buffer[0])) blocks.push(renderList(buffer));
    else blocks.push(`<p>${inline(buffer.map(line => line.trim()).join(' '))}</p>`);
    buffer = [];
  };
  for (const line of markdown.split('\n')) {
    if (!line.trim()) { flush(); continue; }
    const isTable = line.trim().startsWith('|');
    const isList = /^\s*(?:[-*]|\d+\.)\s+/.test(line);
    const kind = block => block.trim().startsWith('|') ? 'table' : /^\s*(?:[-*]|\d+\.)\s+/.test(block) ? 'list' : 'text';
    if (buffer.length) {
      const previous = kind(buffer[0]);
      const next = isTable ? 'table' : isList ? 'list' : 'text';
      if (previous !== next && !(previous === 'list' && next === 'text' && /^\s+/.test(line))) flush();
    }
    buffer.push(line);
  }
  flush();
  return blocks.join('\n');
}

// ---------- Pages ----------

function stampParts(stamp, entry) {
  const [, fullDate, fullTime, shortDate, shortTime, timeOnly, phase] = stamp.match(STAMP);
  if (phase) return { label: phase.replaceAll('-', ' '), datetime: null };
  const year = entry.meta.date.slice(0, 4);
  if (fullDate) return { label: stamp, datetime: fullTime ? `${fullDate}T${fullTime}` : fullDate };
  if (shortDate) return { label: stamp, datetime: `${year}-${shortDate}T${shortTime}` };
  return { label: timeOnly, datetime: `${entry.meta.date}T${timeOnly}` };
}

function statusBadge(status) {
  return `<span class="sd-status" data-status="${escapeHtml(status)}">${escapeHtml(status[0].toUpperCase() + status.slice(1))}</span>`;
}

function when(entry) {
  const time = entry.meta.time ? `, ${entry.meta.time}` : '';
  const datetime = entry.meta.date + (entry.meta.time ? `T${entry.meta.time}` : '');
  return `<time datetime="${datetime}">${longDate(entry.meta.date)}${time}</time>`;
}

function shell({ title, description, canonical, root, current, body }) {
  const nav = [
    [`${root}#system`, 'The system', ' class="sd-keep"'],
    ['./', 'Stardate log', ` class="sd-keep"${current === 'log' ? ' aria-current="page"' : ''}`],
    [`${root}#fleet`, 'The fleet', ''],
    [`${root}#configure`, 'Configure', ''],
    [`${root}guide.html`, 'Install guide', ''],
  ].map(([href, label, extra]) => `<a href="${href}"${extra}>${label}</a>`).join('');
  return `<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="theme-color" content="#080b09">
<meta name="description" content="${escapeHtml(description)}">
<meta name="generator" content="Stardate log publisher (static, from Markdown entries)">
<title>${escapeHtml(title)}</title>
<link rel="canonical" href="${canonical}">
<link rel="icon" href="../mark.svg" type="image/svg+xml">
<link rel="stylesheet" href="${root}styles.css">
<link rel="stylesheet" href="./stardate.css">
</head>
<body class="sd-page">
<a class="skip-link" href="#main">Skip to content</a>
<header class="header wrap sd-header">
<a class="brand" href="${root}" aria-label="BORG Collective home"><img src="../mark.svg" width="36" height="40" alt=""><span>BORG<span class="brand-sub">COLLECTIVE</span></span></a>
<nav class="sd-nav" aria-label="Main navigation">${nav}<a class="nav-source" href="https://github.com/h3ro-dev/borg">GitHub <span aria-hidden="true">↗</span></a></nav>
</header>
<main id="main" tabindex="-1" class="wrap sd-main">
${body}
</main>
<footer class="footer wrap">
<a class="brand" href="${root}"><img src="../mark.svg" width="30" height="34" alt=""><span>BORG<span class="brand-sub">COLLECTIVE</span></span></a>
<p>An independent open-source project.<br>Not affiliated with Star Trek or its rights holders.</p>
<nav aria-label="Footer navigation"><a href="./">Stardate log</a><a href="${root}#system">The system</a><a href="${root}guide.html">Install guide</a><a href="https://github.com/h3ro-dev/borg">GitHub ↗</a></nav>
</footer>
</body>
</html>
`;
}

const ORIGIN = 'https://borg.utlyze.com/assets/stardate/';

export function renderEntryPage(entry, { newer, older } = {}) {
  const { meta } = entry;
  const sections = SECTIONS.map((name, i) => {
    const id = slugify(name);
    const number = String(i + 1).padStart(2, '0');
    const content = name === 'Lab book log'
      ? `<ol class="sd-log">${entry.lab.map(line => {
        const stamp = stampParts(line.stamp, entry);
        const tag = stamp.datetime ? `<time datetime="${stamp.datetime}">${escapeHtml(stamp.label)}</time>` : `<span class="sd-phase">${escapeHtml(stamp.label)}</span>`;
        return `<li>${tag}<p>${inline(line.text)}</p></li>`;
      }).join('')}</ol>`
      : renderMarkdown(entry.sections.get(name), `${name}`);
    return `<section class="sd-section sd-${id}" aria-labelledby="${id}">
<h2 id="${id}"><span class="sd-num" aria-hidden="true">${number}</span> ${name}</h2>
${content}
</section>`;
  }).join('\n');
  const pager = [
    older ? `<a class="sd-pager-older" href="./${older.page}"><span>Older entry</span> ${escapeHtml(older.meta.title)}</a>` : '',
    `<a class="sd-pager-all" href="./">All entries</a>`,
    newer ? `<a class="sd-pager-newer" href="./${newer.page}"><span>Newer entry</span> ${escapeHtml(newer.meta.title)}</a>` : '',
  ].join('');
  const body = `<nav class="sd-crumbs" aria-label="Breadcrumb"><ol><li><a href="./">Stardate log</a></li><li><span aria-current="page">Stardate ${meta.stardate}</span></li></ol></nav>
<article class="sd-entry" aria-labelledby="entry-title">
<header class="sd-entry-head">
<p class="sd-stardate">STARDATE <b>${meta.stardate}</b></p>
<h1 id="entry-title">${escapeHtml(meta.title)}</h1>
<dl class="sd-meta">
<div><dt>Date</dt><dd>${when(entry)} <span class="sd-tz">America/Denver</span></dd></div>
<div><dt>Status</dt><dd>${statusBadge(meta.status)}</dd></div>
<div><dt>Format</dt><dd>Did · Learned · Open · Log</dd></div>
</dl>
<p class="sd-summary">${inline(meta.summary)}</p>
</header>
${sections}
</article>
<nav class="sd-pager" aria-label="More entries">${pager}</nav>`;
  return shell({
    title: `Stardate ${meta.stardate}: ${meta.title} — BORG Collective`,
    description: meta.summary,
    canonical: ORIGIN + entry.page,
    root: '../../',
    current: 'entry',
    body,
  });
}

function entryCard(entry, href, headingLevel = 3) {
  const { meta } = entry;
  return `<article class="sd-card" aria-labelledby="card-${entry.slug}">
<p class="sd-card-meta"><span class="sd-card-stardate">STARDATE ${meta.stardate}</span> ${when(entry)} ${statusBadge(meta.status)}</p>
<h${headingLevel} id="card-${entry.slug}"><a href="${href}">${escapeHtml(meta.title)}</a></h${headingLevel}>
<p>${inline(meta.summary)}</p>
</article>`;
}

export function renderIndexPage(entries) {
  const groups = new Map();
  for (const entry of entries) {
    if (!groups.has(entry.meta.stardate)) groups.set(entry.meta.stardate, []);
    groups.get(entry.meta.stardate).push(entry);
  }
  const list = [...groups].map(([stardate, group]) => `<li class="sd-day">
<h3 class="sd-day-head"><span>Stardate ${stardate}</span> ${longDate(group[0].meta.date)}</h3>
<ol class="sd-day-entries">${group.map(entry => `<li>${entryCard(entry, './' + entry.page, 4)}</li>`).join('')}</ol>
</li>`).join('\n');
  const newest = entries[0];
  const body = `<section class="sd-hero" aria-labelledby="log-title">
<p class="eyebrow"><span class="square" aria-hidden="true"></span> LAB BOOK / THE COLLECTIVE</p>
<h1 id="log-title">STARDATE<br><span>LOG.</span></h1>
<p class="sd-lede">Every experiment the collective runs is written up here, the same way every time: what we did, what we learned, what we still don’t know, and the lab book log.</p>
<dl class="sd-key">
<div><dt>Reading a stardate</dt><dd><b>${newest ? newest.meta.stardate : '2026.268'}</b> is the year, then the day of the year in America/Denver. Day ${newest ? Number(newest.meta.stardate.slice(5)) : 268} of ${newest ? newest.meta.date.slice(0, 4) : '2026'} is ${newest ? longDate(newest.meta.date) : '25 September 2026'}.</dd></div>
<div><dt>Status</dt><dd><b>Running</b> means the work is still going. <b>Finished</b> means it reached a result. <b>Superseded</b> means a later entry replaced it.</dd></div>
<div><dt>Entries</dt><dd>${entries.length} so far, newest first. Numbers come from the collective’s own reports; names, machines and accounts are left out.</dd></div>
</dl>
</section>
<section class="sd-list" aria-labelledby="entries-title">
<h2 id="entries-title">Entries, newest first</h2>
<ol class="sd-days">
${list}
</ol>
</section>`;
  return shell({
    title: 'Stardate log — the lab book of the BORG Collective',
    description: 'Every experiment the collective runs, in one format: what we did, what we learned, open questions and the lab book log.',
    canonical: ORIGIN,
    root: '../../',
    current: 'log',
    body,
  });
}

/** The home page block between the stardate:latest markers. */
export function renderLatest(entries, count = 3) {
  return `${LATEST_START}
<ol class="log-latest">${entries.slice(0, count).map(entry => `<li>${entryCard(entry, './assets/stardate/' + entry.page)}</li>`).join('')}</ol>
${LATEST_END}`;
}

/** Every generated file, keyed by path relative to site/. */
export async function buildAll(site) {
  const entries = await loadEntries(site);
  const errors = entries.flatMap(entry => entry.errors.map(message => `${entry.file}: ${message}`));
  const files = new Map();
  if (errors.length) return { entries, errors, files };
  entries.forEach((entry, i) => {
    files.set(`assets/stardate/${entry.page}`, renderEntryPage(entry, { newer: entries[i - 1], older: entries[i + 1] }));
  });
  files.set('assets/stardate/index.html', renderIndexPage(entries));
  const home = await readFile(path.join(site, 'index.html'), 'utf8');
  const start = home.indexOf(LATEST_START);
  const end = home.indexOf(LATEST_END);
  if (start < 0 || end < start) errors.push('index.html: missing stardate:latest markers');
  else files.set('index.html', home.slice(0, start) + renderLatest(entries) + home.slice(end + LATEST_END.length));
  return { entries, errors, files };
}
