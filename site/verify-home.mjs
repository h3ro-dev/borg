// Front page (index.html) acceptance; invoked by verify.mjs with its /borg/ test server.
// Every control gets one keyboard row and one touch row, written to evidence/home-controls.md.
// Also covers layout at six widths, text size, the first-screen call to action, reduced
// motion, the no-JavaScript fallback and forwarding of links to sections that moved.
import assert from 'node:assert/strict';
import path from 'node:path';
import { writeFile } from 'node:fs/promises';

const PROMPT = 'Read https://borg.utlyze.com/llms.txt and the installation runbook it links to.';
const GITHUB = 'https://github.com/h3ro-dev/borg';

// [name, selector, kind, options]. Kinds: hash, page, external, tip, reveal, motion, radio, tab, copy, input, details.
// options: hash/path = expected destination; desktop = menu item hidden on phones; inline = text link
// (WCAG 2.5.8 inline exception); self = needs the "Type it yourself" panel; open = FAQ item to open first.
const CONTROLS = [
  ['Skip link', '.skip', 'hash', { hash: '#main', keyboardOnly: true }],
  ['Header: Get started', '.site-head .btn-acid', 'hash', { hash: '#start' }],
  ['Menu: How it works', '.nav a[href="#how"]', 'hash', { hash: '#how', desktop: true }],
  ['Menu: Will it run?', '.nav a[href="#check"]', 'hash', { hash: '#check', desktop: true }],
  ['Menu: Questions', '.nav a[href="#faq"]', 'hash', { hash: '#faq', desktop: true }],
  ['Menu: GitHub', `.nav a[href="${GITHUB}"]`, 'external', { desktop: true }],
  ['Hero: Get started', '.hero .btn-acid', 'hash', { hash: '#start' }],
  ['Hero: See how it works', '.hero .btn-ghost', 'hash', { hash: '#how' }],
  ['Hero: Will it run on my Mac?', '.needs a', 'hash', { hash: '#check', inline: true }],
  ['Explain: open source', '[aria-controls="tip-oss"]', 'tip', { inline: true }],
  ['Motion switch', '#motion', 'motion', {}],
  ['Explain: Codex', '[aria-controls="tip-codex"]', 'tip', { inline: true }],
  ['Explain: Claude Code', '[aria-controls="tip-claude"]', 'tip', { inline: true }],
  ['Technical name: Memory', '[aria-controls="tech-memory"]', 'reveal', {}],
  ['Technical name: Tools', '[aria-controls="tech-tools"]', 'reveal', {}],
  ['Technical name: Teamwork', '[aria-controls="tech-team"]', 'reveal', {}],
  ['Mac check: kind of computer', '#kind-intel', 'radio', { pill: 'Not tested yet' }],
  ['Mac check: memory', '#ram-32', 'radio', { pill: 'Great fit' }],
  ['Mac check: free space', '#disk-lt20', 'radio', { pill: 'Free up space first' }],
  ['Mac check: Get started', '#r-cta', 'hash', { hash: '#start' }],
  ['Install tab: Type it yourself', '#tab-self', 'tab', { panel: '#panel-self' }],
  ['Install tab: Let your AI do it', '#tab-ai', 'tab', { panel: '#panel-ai', self: true }],
  ['Explain: Terminal', '[aria-controls="tip-term"]', 'tip', { inline: true }],
  ['Copy: message for your AI', '[data-copy="prompt-text"]', 'copy', { expect: PROMPT }],
  ['Owner name field', '#owner', 'input', { self: true }],
  ['Copy: commands', '[data-copy="cmds"]', 'copy', { expect: 'git clone https://github.com/h3ro-dev/borg.git\ncd borg\n./install.sh --owner', self: true }],
  ['Install guide link', '#panel-self .fineprint a', 'page', { path: '/borg/guide.html', inline: true, self: true }],
  ['Question: Is BORG free?', '.faq details:nth-of-type(1) summary', 'details', {}],
  ['Question: Where is my data stored?', '.faq details:nth-of-type(2) summary', 'details', {}],
  ['Question: Will it slow down my Mac?', '.faq details:nth-of-type(3) summary', 'details', {}],
  ['Question: Which AI tools work?', '.faq details:nth-of-type(4) summary', 'details', {}],
  ['Question: Do I need to code?', '.faq details:nth-of-type(5) summary', 'details', {}],
  ['Question: How do I remove it?', '.faq details:nth-of-type(6) summary', 'details', {}],
  ['Question link: extra steps', '.faq a[href="./guide.html#providers"]', 'page', { path: '/borg/guide.html', hash: '#providers', inline: true, open: 4 }],
  ['Question link: Cloudflare', '.faq a[href="./guide.html#web"]', 'page', { path: '/borg/guide.html', hash: '#web', inline: true, open: 4 }],
  ['Question link: removal steps', '.faq a[href="./guide.html#operate"]', 'page', { path: '/borg/guide.html', hash: '#operate', inline: true, open: 6 }],
  ['Go further: How we run ours', '.more a[href="./collective.html#system"]', 'page', { path: '/borg/collective.html', hash: '#system' }],
  ['Go further: Lab notes', '.more a[href="./assets/stardate/"]', 'page', { path: '/borg/assets/stardate/' }],
  ['Go further: Plan several machines', '.more a[href="./collective.html#configure"]', 'page', { path: '/borg/collective.html', hash: '#configure' }],
  ['Go further: Read the code', `.more a[href="${GITHUB}"]`, 'external', {}],
  ['Footer: How we run ours', '.foot a[href="./collective.html"]', 'page', { path: '/borg/collective.html' }],
  ['Footer: Lab notes', '.foot a[href="./assets/stardate/"]', 'page', { path: '/borg/assets/stardate/' }],
  ['Footer: Install guide', '.foot a[href="./guide.html"]', 'page', { path: '/borg/guide.html' }],
  ['Footer: GitHub', `.foot a[href="${GITHUB}"]`, 'external', {}],
  ['Footer: MIT license', `.foot a[href="${GITHUB}/blob/master/LICENSE"]`, 'external', {}],
  ['Footer: Third-party notices', `.foot a[href="${GITHUB}/blob/master/THIRD_PARTY_NOTICES.md"]`, 'external', {}],
  ['Footer: For AI agents', '.foot a[href="./llms.txt"]', 'page', { path: '/borg/llms.txt' }],
];

async function open(page, home, options = {}) {
  await page.goto(home);
  await page.evaluate(() => document.fonts.ready);
  if (options.self) await page.locator('#tab-self').click();
  if (options.open) await page.locator(`.faq details:nth-of-type(${options.open})`).evaluate((el) => { el.open = true; });
}

// Tab through the page once and record each stop. Radios count for their whole group.
async function tabWalk(page) {
  await page.evaluate((controls) => {
    controls.forEach(([, selector, kind], index) => {
      const el = document.querySelector(selector);
      if (!el) return;
      const group = kind === 'radio' ? document.querySelectorAll(`input[name="${el.name}"]`) : [el];
      group.forEach((node) => { node.dataset.ctl = (node.dataset.ctl ? node.dataset.ctl + ' ' : '') + index; });
    });
    window.scrollTo(0, 0);
    // Start Tab from the top of the page, not from whatever was last clicked.
    document.body.setAttribute('tabindex', '-1');
    document.body.focus();
    document.body.removeAttribute('tabindex');
  }, CONTROLS);
  const stops = [];
  for (let i = 0; i < 260; i++) {
    await page.keyboard.press('Tab');
    const stop = await page.evaluate(() => {
      const el = document.activeElement;
      if (!el || el === document.body || el.dataset.walk) return null;
      el.dataset.walk = '1';
      const shown = el.matches('input[type="radio"]') ? el.nextElementSibling : el;
      const style = getComputedStyle(shown);
      const ring = (style.outlineStyle !== 'none' && parseFloat(style.outlineWidth) > 0) || style.boxShadow !== 'none';
      return { ctl: el.dataset.ctl || '', ring };
    });
    if (!stop) break;
    stops.push(stop);
  }
  return stops;
}

async function effect(page, [name, selector, kind, options], how) {
  const el = page.locator(selector).first();
  const act = async (key) => (how === 'touch' ? el.tap() : (await el.focus(), page.keyboard.press(key)));
  switch (kind) {
    case 'hash':
      await act('Enter');
      await page.waitForFunction((hash) => location.hash === hash, options.hash);
      return `goes to ${options.hash}`;
    case 'page':
      await act('Enter');
      await page.waitForURL((url) => url.pathname === options.path && (!options.hash || url.hash === options.hash));
      return `opens ${options.path}${options.hash || ''}`;
    case 'external': {
      const href = await el.getAttribute('href');
      assert(href.startsWith(GITHUB), `${name} points at ${href}`);
      return 'opens GitHub (not followed in the test)';
    }
    case 'tip': {
      const pop = page.locator('#' + (await el.getAttribute('aria-controls')));
      await act('Enter');
      assert.equal(await el.getAttribute('aria-expanded'), 'true');
      assert(await pop.isVisible(), `${name} explanation shows`);
      if (how === 'touch') await page.touchscreen.tap(4, 400); // empty side margin
      else await page.keyboard.press('Escape');
      assert(!(await pop.isVisible()), `${name} explanation closes`);
      return how === 'touch' ? 'opens; tapping elsewhere closes' : 'Enter opens; Escape closes';
    }
    case 'reveal': {
      const pop = page.locator('#' + (await el.getAttribute('aria-controls')));
      await act(' ');
      assert(await pop.isVisible(), `${name} opens`);
      await act(' ');
      assert(!(await pop.isVisible()), `${name} closes`);
      return how === 'touch' ? 'tap opens, tap closes' : 'Space opens, Space closes';
    }
    case 'motion': {
      const before = await page.evaluate(() => document.documentElement.dataset.motion);
      await act(' ');
      const after = await page.evaluate(() => document.documentElement.dataset.motion);
      assert.notEqual(after, before, 'Motion switch changes the page state');
      assert.equal(await el.getAttribute('aria-pressed'), String(after === 'running'));
      return `switches motion ${before} → ${after}`;
    }
    case 'radio': {
      if (how === 'touch') await page.locator('label.opt', { has: page.locator(selector) }).tap();
      else { await el.focus(); await page.keyboard.press(' '); }
      assert(await el.isChecked(), `${name} option selected`);
      assert.equal((await page.textContent('#r-pill')).trim(), options.pill);
      return `selects it; answer reads "${options.pill}"`;
    }
    case 'tab': {
      await act('Enter');
      assert.equal(await el.getAttribute('aria-selected'), 'true');
      assert(await page.locator(options.panel).isVisible(), `${name} panel shows`);
      if (how === 'keyboard') {
        await page.keyboard.press('ArrowRight');
        assert(await page.evaluate(() => document.activeElement.getAttribute('role') === 'tab' && document.activeElement.getAttribute('aria-selected') === 'true'));
        return 'Enter selects; arrow keys move between tabs';
      }
      return 'tap selects and shows its panel';
    }
    case 'copy': {
      await act('Enter');
      await page.waitForFunction((sel) => document.querySelector(sel).textContent.trim() === 'Copied', selector);
      const copied = await page.evaluate(() => navigator.clipboard.readText());
      assert(copied.startsWith(options.expect), `${name} copies the right text`);
      assert(!copied.includes('Downloads BORG'), `${name} leaves out the explanations`);
      return 'copies the exact text and says "Copied"';
    }
    case 'input': {
      if (how === 'touch') await el.tap(); else await el.focus();
      await el.fill('jamie');
      assert.equal(await page.textContent('.owner-slot'), 'jamie');
      await el.fill('Jamie!');
      assert.equal(await el.getAttribute('aria-invalid'), 'true');
      return 'a valid name fills the command; an invalid one is flagged';
    }
    case 'details': {
      const details = el.locator('xpath=..');
      const before = await details.evaluate((d) => d.open);
      await act('Enter');
      assert.notEqual(await details.evaluate((d) => d.open), before, `${name} toggles`);
      return before ? 'closes the answer' : 'opens the answer';
    }
    default:
      throw new Error(`Unknown control kind ${kind}`);
  }
}

export async function verifyHome({ browser, origin, evidence, results }) {
  const home = origin + '/borg/';
  const errors = [];
  const watch = (page) => {
    page.on('pageerror', (error) => errors.push(error.message));
    page.on('response', (response) => { if (response.status() >= 400 && new URL(response.url()).origin === origin) errors.push(`${response.status()} ${response.url()}`); });
  };

  // Layout, text size and the first-screen call to action at six widths.
  const layout = await browser.newContext();
  const page = await layout.newPage();
  watch(page);
  for (const width of [320, 390, 768, 1024, 1440, 1920]) {
    await page.setViewportSize({ width, height: width < 768 ? 844 : 900 });
    await open(page, home);
    const m = await page.evaluate(() => {
      const shown = (e) => { const r = e.getBoundingClientRect(); return r.width > 0 && r.height > 0 && getComputedStyle(e).visibility !== 'hidden'; };
      const text = [...document.querySelectorAll('body *')].filter((e) => shown(e) && [...e.childNodes].some((n) => n.nodeType === 3 && n.textContent.trim()));
      return {
        content: document.documentElement.scrollWidth,
        viewport: innerWidth,
        minFont: Math.min(...text.map((e) => parseFloat(getComputedStyle(e).fontSize))),
        cta: document.querySelector('.hero .btn-acid').getBoundingClientRect().bottom,
        height: innerHeight,
      };
    });
    assert(m.content <= m.viewport, `Front page overflows at ${width}px: ${JSON.stringify(m)}`);
    assert(m.minFont >= 13, `Front page text under 13px at ${width}px: ${m.minFont}`);
    assert(m.cta <= m.height, `Hero "Get started" is below the first screen at ${width}px: ${m.cta} > ${m.height}`);
    if (width === 390 || width === 1440) {
      const file = width === 390 ? 'home-mobile.png' : 'home-desktop.png';
      await page.screenshot({ path: path.join(evidence, file), fullPage: true, animations: 'disabled' });
      results.screenshots.push(`evidence/${file}`);
    }
  }
  results.checks.push('Front page: no horizontal overflow, text at least 13px and the hero "Get started" on the first screen at 320, 390, 768, 1024, 1440 and 1920px');

  // Links to sections that moved to collective.html keep working.
  for (const [hash, expected] of [['#configure', '/borg/collective.html#configure'], ['#system', '/borg/collective.html#system'], ['#part-memory', '/borg/collective.html#part-memory'], ['#setup', '/borg/#start'], ['#how', '/borg/#how']]) {
    await page.goto(home + hash);
    // Same-page hash changes fire no new load event, so poll the address instead of waiting for navigation.
    const deadline = Date.now() + 10000;
    while (new URL(page.url()).pathname + new URL(page.url()).hash !== expected) {
      assert(Date.now() < deadline, `${hash} should open ${expected}, got ${page.url()}`);
      await page.waitForTimeout(100);
    }
  }
  results.checks.push('Front page: old links to moved sections (#configure, #system, #part-*) open collective.html; #setup opens the new start section');
  await layout.close();

  // One keyboard row and one touch row per control.
  const desktop = await browser.newContext({ viewport: { width: 1440, height: 900 }, permissions: ['clipboard-read', 'clipboard-write'] });
  const touch = await browser.newContext({ viewport: { width: 390, height: 844 }, hasTouch: true, isMobile: true, deviceScaleFactor: 2, permissions: ['clipboard-read', 'clipboard-write'] });
  const kb = await desktop.newPage();
  const tp = await touch.newPage();
  watch(kb); watch(tp);
  const stops = [];
  await open(kb, home);
  stops.push(...await tabWalk(kb));
  await open(kb, home, { self: true });
  await kb.evaluate(() => document.querySelectorAll('.faq details').forEach((d) => { d.open = true; }));
  stops.push(...await tabWalk(kb));
  const rows = [];
  for (const [index, control] of CONTROLS.entries()) {
    const [name, selector, , options] = control;
    const row = { control: name, keyboard: '', touch: '' };
    try {
      const reached = stops.find((stop) => stop.ctl.split(' ').includes(String(index)));
      assert(reached, `${name} is not reachable with Tab`);
      assert(reached.ring, `${name} has no visible focus ring`);
      await open(kb, home, options);
      row.keyboard = `PASS · Tab reaches it with a visible focus ring; ${await effect(kb, control, 'keyboard')}`;
    } catch (error) { row.keyboard = `FAIL · ${error.message.split('\n')[0]}`; }
    try {
      if (options.keyboardOnly) row.touch = 'n/a · keyboard-only skip link';
      else if (options.desktop) {
        await open(tp, home);
        assert(!(await tp.locator(selector).isVisible()), `${name} should be hidden on phones`);
        row.touch = 'n/a · menu item hidden on phones; the header "Get started" button stays';
      } else {
        await open(tp, home, options);
        const target = control[2] === 'radio' ? tp.locator('label.opt', { has: tp.locator(selector) }) : tp.locator(selector).first();
        await target.scrollIntoViewIfNeeded();
        const box = await target.boundingBox();
        const size = `${Math.round(box.width)}×${Math.round(box.height)}`;
        if (options.inline) assert(box.height >= 16, `${name} is too small to tap: ${size}`);
        else assert(box.width >= 44 && box.height >= 44, `${name} tap target ${size} is under 44×44`);
        row.touch = `PASS · ${size}${options.inline ? ' inline text link' : ''}; ${await effect(tp, control, 'touch')}`;
      }
    } catch (error) { row.touch = `FAIL · ${error.message.split('\n')[0]}`; }
    rows.push(row);
  }
  await desktop.close();
  await touch.close();
  const table = ['| Control | Keyboard | Touch (390 × 844 phone) |', '| --- | --- | --- |', ...rows.map((r) => `| ${r.control} | ${r.keyboard} | ${r.touch} |`)].join('\n');
  await writeFile(path.join(evidence, 'home-controls.md'), `# Front page controls\n\n${table}\n`);
  await writeFile(path.join(evidence, 'home-controls.json'), JSON.stringify(rows, null, 2));
  const failed = rows.filter((r) => r.keyboard.startsWith('FAIL') || r.touch.startsWith('FAIL'));
  assert.equal(failed.length, 0, `Front page control rows failed:\n${failed.map((r) => `${r.control}: ${r.keyboard} / ${r.touch}`).join('\n')}`);
  results.checks.push(`Front page: ${rows.length} controls each pass a keyboard row and a touch row (evidence/home-controls.md)`);

  // Reduced motion starts paused; without JavaScript the page still offers every install path.
  const calm = await browser.newContext({ reducedMotion: 'reduce' });
  const still = await calm.newPage();
  watch(still);
  await open(still, home);
  assert.equal(await still.evaluate(() => document.documentElement.dataset.motion), 'paused');
  assert.equal((await still.textContent('#motion')).trim(), 'Play motion');
  await calm.close();
  const nojs = await browser.newContext({ javaScriptEnabled: false, viewport: { width: 390, height: 844 } });
  const plain = await nojs.newPage();
  await plain.goto(home);
  assert(await plain.locator('#panel-ai').isVisible() && await plain.locator('#panel-self').isVisible(), 'Both install paths show without JavaScript');
  assert(!(await plain.locator('[role="tablist"]').isVisible()), 'Tabs hide without JavaScript');
  assert(await plain.locator('.result-static').isVisible() && !(await plain.locator('#result').isVisible()), 'Static Mac guidance replaces the live answer without JavaScript');
  assert(!(await plain.locator('#motion').isVisible()), 'Motion switch hides without JavaScript');
  assert.equal(await plain.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
  await nojs.close();
  results.checks.push('Front page: reduced motion starts paused; without JavaScript both install paths, the static Mac guidance and every link remain');
  if (process.env.AXE_SCRIPT) {
    const audit = await browser.newContext({ viewport: { width: 1440, height: 900 } });
    const scan = await audit.newPage();
    const audits = [];
    for (const state of ['default', 'type it yourself, questions open']) {
      await open(scan, home, { self: state !== 'default' });
      if (state !== 'default') await scan.evaluate(() => document.querySelectorAll('.faq details').forEach((d) => { d.open = true; }));
      await scan.addScriptTag({ path: process.env.AXE_SCRIPT });
      const run = await scan.evaluate(async () => await axe.run(document, { runOnly: { type: 'tag', values: ['wcag2a', 'wcag2aa', 'wcag21aa', 'wcag22aa'] } }));
      audits.push({ state, violations: run.violations, passes: run.passes.length });
    }
    await audit.close();
    await writeFile(path.join(evidence, 'home-accessibility.json'), JSON.stringify(audits, null, 2) + '\n');
    assert(audits.every((a) => a.violations.length === 0), 'Front page axe violations; inspect evidence/home-accessibility.json');
    results.checks.push('Front page axe WCAG 2 A/AA, 2.1 AA and 2.2 AA: zero automated violations in both states');
  }
  assert.deepEqual(errors, [], 'Front page loads without script or asset errors');
}
