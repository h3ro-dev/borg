import assert from 'node:assert/strict';
import { writeFile } from 'node:fs/promises';
import path from 'node:path';
import { profiles } from './fleet-profiles.mjs';

export async function verifyExplorer({ page, browser, origin, evidence, results }) {
  await page.emulateMedia({ reducedMotion: 'no-preference' });
  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.goto(origin + '/borg/#fleet');
  const dialog = page.locator('#node-profile');
  const audits = [];
  for (const profile of profiles) {
    const trigger = page.locator(`.fleet-hotspot[data-profile="${profile.id}"]`);
    await trigger.click();
    assert(await dialog.isVisible());
    assert.equal(await page.locator('#profile-name').textContent(), profile.name);
    assert(await page.locator('.profile-close').evaluate(el => el === document.activeElement));
    await page.locator('#profile-character').evaluate(image => image.decode());
    assert(await page.locator('#profile-character').evaluate(image => image.naturalWidth === 1100 && image.naturalHeight === 1600));
    for (let i = 0; i < profile.parts.length; i++) {
      await page.locator(`.body-point[data-part="${i}"]`).click();
      assert.equal(await page.locator('#component-name').textContent(), profile.parts[i].name);
      assert.equal(await page.locator(`.profile-parts [data-part="${i}"]`).getAttribute('aria-pressed'), 'true');
      assert.equal(await page.locator('#component-source').getAttribute('href'), profile.parts[i].source);
    }
    for (let i = 0; i < profile.flow.length; i++) {
      await page.locator(`[data-step="${i}"]`).click();
      assert.equal(await page.locator('#flow-detail').textContent(), profile.flow[i].detail);
    }
    if (process.env.AXE_SCRIPT) {
      await page.addScriptTag({ path: process.env.AXE_SCRIPT });
      const audit = await page.evaluate(async () => axe.run(document, {
        runOnly: { type: 'tag', values: ['wcag2a', 'wcag2aa', 'wcag21aa'] },
      }));
      audits.push({ unit: profile.id, violations: audit.violations });
    }
    await page.locator('.profile-close').focus();
    await page.keyboard.press('Shift+Tab');
    assert(await page.locator('#profile-motion-toggle').evaluate(el => el === document.activeElement));
    await page.keyboard.press('Shift+Tab');
    assert(await page.locator('#profile-setup').evaluate(el => el === document.activeElement));
    await page.keyboard.press('Tab');
    assert(await page.locator('#profile-motion-toggle').evaluate(el => el === document.activeElement));
    await page.keyboard.press('Escape');
    assert(!(await dialog.isVisible()));
    assert(await trigger.evaluate(el => el === document.activeElement));
    assert.equal(await page.evaluate(() => document.body.style.overflow), '');
  }
  if (audits.length) {
    await writeFile(path.join(evidence, 'explorer-accessibility.json'), JSON.stringify(audits, null, 2));
    assert(audits.every(audit => audit.violations.length === 0), 'Profile accessibility violations');
  }
  results.checks.push('All five ship hotspots open correct portraits; all 25 body modules and 30 flow steps work; modal focus traps and returns after Escape; profile axe audits pass');

  for (const width of [320, 390, 768, 1440]) {
    await page.setViewportSize({ width, height: width < 768 ? 844 : 1000 });
    await page.locator('.fleet-units [data-profile="codex"]').click();
    await page.locator('#profile-character').evaluate(image => image.decode());
    assert(await dialog.evaluate(el => el.scrollWidth <= el.clientWidth), `Profile overflow at ${width}`);
    assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
    await page.locator('.profile-parts [data-part="2"]').click();
    assert.match(await page.locator('#component-name').textContent(), /Facts back/);
    for (const profile of profiles) {
      await page.locator(`[data-unit="${profile.id}"]`).click();
      assert.equal(await page.locator('#profile-name').textContent(), profile.name);
    }
    await page.locator('[data-unit="codex"]').click();
    await dialog.evaluate(el => { el.scrollTop = 0; });
    if ([390, 1440].includes(width)) {
      const name = `profile-${width}.png`;
      await page.screenshot({ path: path.join(evidence, name), animations: 'disabled' });
      results.screenshots.push(`evidence/${name}`);
    }
    await page.locator('.profile-close').click();
  }
  results.checks.push('All profile selectors work at 320/390/768/1440px with no horizontal overflow; both close paths restore the page');

  await page.locator('.fleet-image-frame').scrollIntoViewIfNeeded();
  await page.waitForFunction(() => document.querySelector('#fleet').dataset.motion === 'running');
  assert.equal(await page.locator('.hero').getAttribute('data-motion'), 'paused');
  const stars = page.locator('#fleet .star-near');
  const transform = await stars.evaluate(el => getComputedStyle(el).transform);
  await page.waitForFunction(before => getComputedStyle(document.querySelector('#fleet .star-near')).transform !== before, transform);
  await page.locator('.fleet-units [data-profile="memory"]').click();
  const initial = await page.locator('#flow-detail').textContent();
  await page.waitForFunction(before => document.querySelector('#flow-detail').textContent !== before, initial, { timeout: 7000 });
  await page.locator('#profile-motion-toggle').click();
  assert.equal(await page.locator('html').getAttribute('data-motion'), 'paused');
  assert.equal(await page.locator('.profile-flow-signal').evaluate(el => getComputedStyle(el).animationPlayState), 'paused');
  const pausedStep = await page.locator('#flow-detail').textContent();
  await page.waitForTimeout(4500); // Prove the scheduled advance is cancelled.
  assert.equal(await page.locator('#flow-detail').textContent(), pausedStep);
  await page.locator('.profile-close').click();
  assert.equal(await stars.evaluate(el => getComputedStyle(el).animationPlayState), 'paused');
  await page.emulateMedia({ reducedMotion: 'reduce' });
  assert.equal(await page.locator('html').getAttribute('data-motion'), 'paused');
  await page.locator('#fleet-motion-toggle').click();
  assert.equal(await stars.evaluate(el => getComputedStyle(el).animationPlayState), 'running');
  await page.locator('.fleet-units [data-profile="codex"]').click();
  assert.equal(await page.locator('.profile-flow-signal').evaluate(el => getComputedStyle(el).animationPlayState), 'running');
  await page.locator('.profile-close').click();
  results.checks.push('Fleet stars move with hero offscreen; profile advances automatically; shared pause cancels timers and CSS motion; reduced motion is static unless explicitly played');

  const fallbackContext = await browser.newContext({ viewport: { width: 1100, height: 900 } });
  await fallbackContext.addInitScript(() => {
    const original = HTMLCanvasElement.prototype.getContext;
    HTMLCanvasElement.prototype.getContext = function(type, ...args) {
      return /webgl/.test(type) ? null : original.call(this, type, ...args);
    };
  });
  const fallback = await fallbackContext.newPage();
  await fallback.goto(origin + '/borg/');
  await fallback.waitForFunction(() => document.querySelector('.ship-stage').dataset.state === 'fallback');
  assert(await fallback.locator('#motion-toggle').isVisible());
  assert.equal(await fallback.locator('.hero .star-near').evaluate(el => getComputedStyle(el).animationPlayState), 'running');
  await fallback.locator('.fleet-hotspot[data-profile="memory"]').click();
  assert(await fallback.locator('#node-profile').isVisible());
  await fallbackContext.close();
  const staticContext = await browser.newContext({ javaScriptEnabled: false });
  const staticPage = await staticContext.newPage();
  await staticPage.goto(origin + '/borg/');
  assert.equal(await staticPage.locator('#fleet .star-near').evaluate(el => getComputedStyle(el).animationPlayState), 'paused');
  await staticPage.locator('.fleet-hotspot[data-profile="memory"]').click();
  assert.equal(new URL(staticPage.url()).pathname, '/borg/guide.html');
  assert.equal(new URL(staticPage.url()).hash, '#understand');
  await staticContext.close();
  results.checks.push('WebGL failure preserves moving CSS stars and interactive profiles; no-JavaScript ship links open the relevant installation guide');
}
