// Ship's map acceptance on the home page; invoked by verify.mjs with its /borg/ test server.
import assert from 'node:assert/strict';
import { writeFile } from 'node:fs/promises';
import path from 'node:path';

const PARTS = ['memory', 'jev', 'recall', 'incoming', 'dreaming', 'cache', 'packets', 'deepdream', 'judge', 'fleet', 'rules'];

export async function verifyMap({ page, browser, origin, evidence, results, axeScript = process.env.AXE_SCRIPT }) {
  await page.emulateMedia({ reducedMotion: 'no-preference' });
  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.goto(origin + '/borg/#system');
  await page.evaluate(() => localStorage.removeItem('borg-map-assimilated-v1'));
  await page.reload();
  const section = page.locator('#system');
  await page.waitForFunction(() => document.querySelector('#system').classList.contains('game-on'));
  assert.equal(await page.locator('.part-card').count(), PARTS.length);
  assert.equal(await page.locator('.map-node').count(), PARTS.length + 1);
  assert.match(await page.locator('#map-count').textContent(), /^0 of 11$/);

  await page.locator('.map-node[data-part="memory"]').click();
  assert.equal(await page.locator('#map-card h3').textContent(), 'The memory core');
  assert(await page.locator('#map-card .part-readout').isVisible());
  assert.equal(await page.locator('.map-node[data-part="memory"]').getAttribute('aria-current'), 'true');
  assert.match(await page.locator('#map-count').textContent(), /^1 of 11$/);
  assert(await page.locator('.map-art .cube[data-part="memory"]').evaluate(el => el.classList.contains('is-assimilated')));
  await page.waitForFunction(() => /Memory core|memory core/.test(document.querySelector('.map-announce').textContent));
  assert.equal(new URL(page.url()).hash, '#system', 'Cube click opens the card in place');
  results.checks.push('Map: a cube opens its card (what, why, readout), marks it assimilated, updates the ring and announces politely');

  await page.locator('.map-node[data-part="memory"]').focus();
  await page.keyboard.press('Shift+Tab');
  assert(await page.locator('.map-node[data-part="jev"]').evaluate(el => el === document.activeElement));
  assert.match(await page.locator('.map-hud-text').textContent(), /JEV/);
  assert.notEqual(await page.locator('.map-node[data-part="jev"]').evaluate(el => getComputedStyle(el).outlineStyle), 'none');
  await page.keyboard.press('Enter');
  assert.equal(await page.locator('#map-card h3').textContent(), 'JEV, the judge');
  await page.locator('#map-card .map-button.primary').click();
  assert.match(await page.locator('#map-count').textContent(), /^3 of 11$/);
  results.checks.push('Map keyboard: focus shows a visible ring and scan readout; Enter opens the card; "Next" walks to an unvisited part');

  await page.locator('#map-tour').click();
  assert.match(await page.locator('#map-card .eyebrow').textContent(), /STOP 1 OF 6/);
  assert.equal(await section.getAttribute('data-flow'), '1');
  assert(await page.locator('#map-card').evaluate(el => el === document.activeElement));
  for (let stop = 2; stop <= 6; stop++) {
    await page.locator('#map-card .map-button.primary').click();
    assert.match(await page.locator('#map-card .eyebrow').textContent(), new RegExp(`STOP ${stop} OF 6`));
    assert.equal(await section.getAttribute('data-flow'), String(stop));
  }
  await page.keyboard.press('Escape');
  assert.doesNotMatch(await page.locator('#map-card').textContent(), /STOP 6 OF 6/);
  assert(await page.locator('#map-tour').evaluate(el => el === document.activeElement));
  results.checks.push('Guided tour: six stops follow "How it fits together", highlight their conduits, and Escape returns focus');

  for (const part of PARTS) await page.locator(`.map-node[data-part="${part}"]`).click();
  assert.match(await page.locator('#map-count').textContent(), /^11 of 11$/);
  assert(await section.evaluate(el => el.classList.contains('is-complete')));
  await page.reload();
  assert.match(await page.locator('#map-count').textContent(), /^11 of 11$/);
  await page.locator('#map-reset').click();
  assert.match(await page.locator('#map-count').textContent(), /^0 of 11$/);
  assert.equal(await page.locator('.part-card .part-state').count(), 0);
  results.checks.push('Assimilation: all 11 parts complete the ring, progress survives reload in local storage only, Reset clears it');

  await page.locator('#system').scrollIntoViewIfNeeded();
  await page.waitForFunction(() => document.querySelector('#system').dataset.motion === 'running');
  await page.waitForFunction(() => document.querySelector('#system').dataset.flow);
  assert.equal(await page.locator('.conduit-pulse').first().evaluate(el => getComputedStyle(el).animationName), 'conduit-flow');
  await page.locator('#map-motion-toggle').click();
  assert.equal(await section.getAttribute('data-motion'), 'paused');
  assert.equal(await section.getAttribute('data-flow'), null, 'Pause returns to the static overview');
  await page.waitForTimeout(4600);
  assert.equal(await section.getAttribute('data-flow'), null, 'Pause stops the flow cycle');
  await page.locator('#map-motion-toggle').click();
  await page.emulateMedia({ reducedMotion: 'reduce' });
  await page.reload();
  await page.locator('#system').scrollIntoViewIfNeeded();
  assert.equal(await section.getAttribute('data-motion'), 'paused');
  assert.equal(await page.locator('.conduit-pulse').first().evaluate(el => getComputedStyle(el).animationName), 'none');
  results.checks.push('Flow animation cycles the six steps while visible; the shared motion toggle pauses it; reduced motion starts static');
  await page.emulateMedia({ reducedMotion: 'no-preference' });

  const audits = [];
  for (const width of [320, 768, 1280, 1920]) {
    await page.setViewportSize({ width, height: width < 768 ? 844 : 1000 });
    await page.goto(origin + '/borg/');
    await page.evaluate(() => document.fonts.ready);
    assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), `Home overflow at ${width}`);
    const box = await page.locator('.map-node[data-part="recall"]').boundingBox();
    assert(box.width >= 24 && box.height >= 24, `Map target too small at ${width}`);
    if (axeScript) {
      await page.locator('.map-node[data-part="incoming"]').click();
      await page.addScriptTag({ path: axeScript });
      const audit = await page.evaluate(() => axe.run(document, { runOnly: { type: 'tag', values: ['wcag2a', 'wcag2aa', 'wcag21aa'] } }));
      audits.push({ width, violations: audit.violations.map(v => ({ id: v.id, nodes: v.nodes.map(n => n.target.join(' ')) })), passes: audit.passes.length });
    }
    if (width === 1280) {
      await page.locator('#system').screenshot({ path: path.join(evidence, 'map-1280.png'), animations: 'disabled' });
      results.screenshots.push('evidence/map-1280.png');
    }
  }
  if (audits.length) {
    await writeFile(path.join(evidence, 'home-accessibility-widths.json'), JSON.stringify(audits, null, 2));
    assert(audits.every(audit => audit.violations.length === 0), 'Home accessibility violations; see evidence/home-accessibility-widths.json');
    results.checks.push('Home page with a card open: axe WCAG 2 A/AA and 2.1 AA zero violations at 320/768/1280/1920');
  }
  results.checks.push('Home fits 320/768/1280/1920 without horizontal overflow; map targets stay at least 24px');

  const plain = await browser.newContext({ javaScriptEnabled: false, viewport: { width: 390, height: 844 } });
  const nojs = await plain.newPage();
  await nojs.goto(origin + '/borg/');
  for (const part of PARTS) assert(await nojs.locator(`#part-${part}`).isVisible(), `Card ${part} hidden without JavaScript`);
  assert.equal(await nojs.locator('.flow-steps li:visible').count(), 6);
  assert(await nojs.locator('#map-tour').isHidden());
  await nojs.locator('.map-node[data-part="recall"]').click();
  assert.equal(new URL(nojs.url()).hash, '#part-recall');
  await plain.close();
  results.checks.push('No-JavaScript: the map, all 11 cards and all 6 flow steps are visible; cubes link to their cards; no inert game controls');
}
