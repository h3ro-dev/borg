// Focused guide acceptance, used by the site's existing /borg/ test server.
import assert from 'node:assert/strict';
import path from 'node:path';

export async function verifyGuide({ page, origin, evidence, results }) {
  const url = `${origin}/borg/guide.html`;
  await page.goto(url);
  await page.locator('.guide-copy').first().waitFor();
  assert.equal(await page.locator('.guide-content h2').count(), 11);
  assert.equal(await page.locator('.guide-code').count(), 24);
  const anchors = await page.locator('.guide-toc a').evaluateAll(links =>
    links.map(link => ({ hash: link.hash, exists: !!document.getElementById(link.hash.slice(1)) })));
  assert(anchors.every(link => link.exists), JSON.stringify(anchors));
  assert.equal(await page.locator('#web').count(), 1);
  assert.equal(await page.locator('#operate').count(), 1);
  const prompt = await page.locator('code.language-text').textContent();
  assert(prompt.includes('https://borg.utlyze.com/guide.html'));
  assert(prompt.includes('https://borg.utlyze.com/agent-guide.md'));
  // Stub the browser clipboard, not any application content or action.
  await page.evaluate(() => {
    window.guideCopiedText = null;
    Object.defineProperty(navigator.clipboard, 'writeText', {
      configurable: true,
      value: async text => { window.guideCopiedText = text; },
    });
  });
  await page.locator('.guide-copy').first().click();
  assert.equal(await page.evaluate(() => window.guideCopiedText),
    await page.locator('.guide-code code').first().textContent());
  for (const width of [1440, 768, 390, 320]) {
    await page.setViewportSize({ width, height: 960 });
    assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1), `guide overflow ${width}`);
    if (width === 1440 || width === 390) {
      await page.goto(url);
      await page.evaluate(() => document.fonts.ready);
      const file = path.join(evidence, `guide-${width}.png`);
      await page.screenshot({ path: file });
      results.screenshots.push(file);
    }
  }
  const context = await page.context().browser().newContext({ javaScriptEnabled: false, viewport: { width: 390, height: 844 } });
  try {
    const plain = await context.newPage();
    await plain.goto(url);
    assert.equal(await plain.locator('.guide-copy').count(), 0);
    assert(await plain.locator('#full-install').isVisible());
    assert(await plain.locator('#blueprint-install').isVisible());
    assert.equal(await plain.locator('pre').count(), 24);
    await plain.locator('details summary').first().click();
    assert(await plain.locator('details').first().getAttribute('open') !== null);
    assert(await plain.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1));
  } finally {
    await context.close();
  }
  results.checks.push('guide: static steps, anchors, copy, absolute prompt URLs, four viewport widths and no-JS optional navigation');
}
