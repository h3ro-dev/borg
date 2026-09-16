// Local UI acceptance checks. Use an installed Playwright module via PLAYWRIGHT_MODULE.
// No production dependencies; no provider calls or installed BORG required.
import { createServer } from "node:http";
import { readFile, mkdir, writeFile } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";
import assert from "node:assert/strict";
import { verifyConfigurator } from "./verify-configure.mjs";
const root = path.dirname(fileURLToPath(import.meta.url));
const { chromium, webkit } = await import(
  process.env.PLAYWRIGHT_MODULE
    ? pathToFileURL(path.join(process.env.PLAYWRIGHT_MODULE, "index.mjs")).href
    : "playwright"
);
const mime = {
  ".html": "text/html",
  ".css": "text/css",
  ".js": "text/javascript",
  ".mjs": "text/javascript",
  ".json": "application/json",
  ".svg": "image/svg+xml",
  ".ttf": "font/ttf",
  ".webp": "image/webp",
  ".glb": "model/gltf-binary",
  ".md": "text/markdown; charset=utf-8",
  ".txt": "text/plain; charset=utf-8",
};
const server = createServer(async (req, res) => {
  try {
    const url = new URL(req.url, "http://localhost");
    if (!url.pathname.startsWith("/borg/")) {
      res.writeHead(404).end();
      return;
    }
    const relative =
      decodeURIComponent(url.pathname.slice("/borg/".length)) || "index.html";
    const publicRoot = relative.startsWith('platform/') ? path.dirname(root) : root;
    const target = path.resolve(publicRoot, relative);
    if (!target.startsWith(publicRoot + path.sep)) {
      res.writeHead(403).end();
      return;
    }
    const content = await readFile(target);
    res
      .writeHead(200, {
        "Content-Type":
          mime[path.extname(target)] || "application/octet-stream",
      })
      .end(content);
  } catch {
    res.writeHead(404).end();
  }
});
await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
const origin = `http://127.0.0.1:${server.address().port}`;
const results = { path: "/borg/", checks: [], screenshots: [], failures: [] };
const evidence = path.join(root, "evidence");
await mkdir(evidence, { recursive: true });
let browser;
try {
  browser = await chromium.launch({
    headless: true,
    ...(process.env.CHROMIUM_EXECUTABLE
      ? { executablePath: process.env.CHROMIUM_EXECUTABLE }
      : {}),
  });
  results.chromium = browser.version();
  const context = await browser.newContext({
    permissions: ["clipboard-read", "clipboard-write"],
  });
  const page = await context.newPage();
  page.setDefaultTimeout(15000);
  const errors = [];
  page.on("pageerror", (error) => errors.push(error.message));
  page.on("response", (response) => {
    if (response.status() >= 400)
      errors.push(`${response.status()} ${response.url()}`);
  });
  for (const width of [320, 390, 768, 1024, 1440, 1920]) {
    await page.setViewportSize({ width, height: width < 768 ? 844 : 1000 });
    await page.goto(origin + "/borg/");
    await page.evaluate(() => document.fonts.ready);
    await page.locator("#download-blueprint").waitFor();
    const dimensions = await page.evaluate(() => ({
      viewport: innerWidth,
      content: document.documentElement.scrollWidth,
    }));
    assert(
      dimensions.content <= dimensions.viewport,
      `Horizontal overflow at ${width}: ${JSON.stringify(dimensions)}`,
    );
    assert(await page.locator("h1").isVisible());
    if ([390, 1440].includes(width)) {
      const name = width === 390 ? "mobile.png" : "desktop.png";
      await page.screenshot({
        path: path.join(evidence, name),
        fullPage: true,
        timeout: 60000,
        animations: "disabled",
      });
      results.screenshots.push(`evidence/${name}`);
    }
    results.checks.push(
      `No horizontal overflow and rendered heading at ${width}px`,
    );
  }
  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.goto(origin + "/borg/");
  await page.keyboard.press("Tab");
  assert(
    await page
      .locator(".skip-link")
      .evaluate((el) => el === document.activeElement),
  );
  await page.keyboard.press("Enter");
  assert.equal(new URL(page.url()).hash, "#main");
  results.checks.push("Keyboard skip link targets main content");
  await page.locator("#tab-local").focus();
  await page.keyboard.press("ArrowRight");
  assert.equal(
    await page.locator("#tab-web").getAttribute("aria-selected"),
    "true",
  );
  assert(await page.locator("#panel-web").isVisible());
  assert(!(await page.locator("#panel-local").isVisible()));
  await page.keyboard.press("End");
  assert.equal(
    await page.locator("#tab-existing").getAttribute("aria-selected"),
    "true",
  );
  await page.keyboard.press("ArrowRight");
  assert.equal(
    await page.locator("#tab-local").getAttribute("aria-selected"),
    "true",
  );
  await page.keyboard.press("ArrowLeft");
  assert.equal(
    await page.locator("#tab-existing").getAttribute("aria-selected"),
    "true",
  );
  await page.keyboard.press("Home");
  assert.equal(
    await page.locator("#tab-local").getAttribute("aria-selected"),
    "true",
  );
  const outline = await page
    .locator("#tab-local")
    .evaluate((el) => getComputedStyle(el).outlineStyle);
  assert.notEqual(outline, "none");
  results.checks.push(
    "Tabs: keyboard arrows, wrapping, Home/End, focused outline, selected state and associated panels",
  );
  for (const [tab, code] of [
    ["local", "install-command"],
    ["existing", "inspect-command"],
  ]) {
    await page.locator(`#tab-${tab}`).click();
    await page.locator(`[data-copy="${code}"]`).click();
    const expected = (await page.locator(`#${code}`).textContent()).trim();
    assert.equal(
      await page.evaluate(() => navigator.clipboard.readText()),
      expected,
    );
    assert.match(await page.locator(".copy-status").textContent(), /copied/);
  }
  results.checks.push(
    "Both copy buttons write exact displayed commands to native clipboard and announce success",
  );
  await page.locator('[data-copy="agent-prompt"]').click();
  assert.equal(await page.evaluate(() => navigator.clipboard.readText()),
    (await page.locator('#agent-prompt').textContent()).trim());
  assert.match(await page.locator('.copy-status').textContent(), /Agent prompt copied/);
  await page.locator('#fleet').scrollIntoViewIfNeeded();
  await page.locator('#fleet img').evaluate(image => image.decode());
  assert(await page.locator('#fleet img').evaluate(image => image.complete && image.naturalWidth === 2200));
  results.checks.push('Homepage agent prompt copies exactly; full Blender fleet image loads');
  await page.evaluate(() => {
    navigator.clipboard.writeText = async () => {
      throw new DOMException("Denied", "NotAllowedError");
    };
  });
  await page.locator('[data-copy="inspect-command"]').click();
  assert.match(
    await page.locator(".copy-status").textContent(),
    /Clipboard unavailable/,
  );
  assert.equal(
    await page.evaluate(() => window.getSelection().toString()),
    (await page.locator("#inspect-command").textContent()).trim(),
  );
  results.checks.push(
    "Clipboard denial selects commands and announces manual-copy fallback",
  );
  await page.emulateMedia({ reducedMotion: "reduce" });
  assert.equal(
    await page
      .locator("html")
      .evaluate((el) => getComputedStyle(el).scrollBehavior),
    "auto",
  );
  assert.equal(
    await page
      .locator(".hero-copy")
      .evaluate((el) => getComputedStyle(el).animationName),
    "none",
  );
  results.checks.push(
    "Reduced motion disables entrance animation and smooth scrolling",
  );
  const links = await page
    .locator("a[href]")
    .evaluateAll((elements) => elements.map((el) => el.getAttribute("href")));
  for (const href of links.filter((link) => link.startsWith("#")))
    assert.equal(await page.locator(href).count(), 1);
  const catalog = JSON.parse(await readFile(path.join(root, '../platform/catalog.json')));
  const documentedLinks = new Set(catalog.items.flatMap(item => [item.docs, item.source]));
  assert(links.filter(link => link.startsWith('https://')).every(link =>
    link.startsWith('https://github.com/h3ro-dev/borg') || documentedLinks.has(link)));
  assert.equal(await page.locator('input[type="password"], input[type="email"]').count(), 0);
  results.checks.push(
    "All in-page anchors resolve; public links point to project or catalog source/docs; no credential inputs",
  );
  assert.deepEqual(errors, []);
  results.checks.push("No JavaScript errors or HTTP asset errors under /borg/");
  if (process.env.AXE_SCRIPT) {
    await page.addScriptTag({ path: process.env.AXE_SCRIPT });
    const audits = [];
    for (const tab of ["local", "web", "existing"]) {
      await page.locator(`#tab-${tab}`).click();
      const audit = await page.evaluate(
        async () =>
          await axe.run(document, {
            runOnly: { type: "tag", values: ["wcag2a", "wcag2aa", "wcag21aa"] },
          }),
      );
      audits.push({
        panel: tab,
        violations: audit.violations,
        passes: audit.passes.length,
        incomplete: audit.incomplete.map((row) => ({
          id: row.id,
          description: row.description,
        })),
      });
    }
    await writeFile(
      path.join(evidence, "accessibility-checks.json"),
      JSON.stringify(audits, null, 2) + "\n",
    );
    assert(
      audits.every((audit) => audit.violations.length === 0),
      "Accessibility audit reported violations; inspect evidence/accessibility-checks.json",
    );
    results.checks.push(
      "axe WCAG 2 A/AA and 2.1 AA: zero automated violations in all three setup panels",
    );
  }
  await verifyConfigurator({ page, origin, evidence, results });
  const nojs = await browser.newContext({
    javaScriptEnabled: false,
    viewport: { width: 390, height: 844 },
  });
  const fallback = await nojs.newPage();
  await fallback.goto(origin + "/borg/");
  for (const id of ["local", "web", "existing"])
    assert(await fallback.locator(`#panel-${id}`).isVisible());
  assert(!(await fallback.locator(".setup-tabs").isVisible()));
  assert.equal(await fallback.locator(".copy-button:visible").count(), 0);
  assert.equal(await fallback.locator("#install-command").count(), 1);
  results.checks.push(
    "No-JavaScript fallback exposes all setup guidance and selectable commands without inactive controls",
  );
  await nojs.close();
  await context.close();
  await browser.close();
  browser = null;
  if (process.env.CHECK_WEBKIT === "1") {
    browser = await webkit.launch({ headless: true });
    results.webkit = browser.version();
    const page = await browser.newPage({
      viewport: { width: 390, height: 844 },
    });
    await page.goto(origin + "/borg/");
    await page.evaluate(() => document.fonts.ready);
    await page.locator("#download-blueprint").waitFor();
    assert(
      await page.evaluate(
        () => document.documentElement.scrollWidth <= innerWidth,
      ),
    );
    await page.locator("#tab-web").click();
    assert(await page.locator("#panel-web").isVisible());
    await page.screenshot({
      path: path.join(evidence, "webkit-mobile.png"),
      fullPage: true,
      timeout: 60000,
    });
    results.screenshots.push("evidence/webkit-mobile.png");
    results.checks.push("WebKit mobile render, overflow and setup interaction");
  }
} catch (error) {
  results.failures.push(error.stack || error.message);
  process.exitCode = 1;
} finally {
  if (browser) await browser.close();
  await new Promise((resolve) => server.close(resolve));
  results.state = results.failures.length ? "FAIL" : "PASS";
  await writeFile(
    path.join(evidence, "browser-checks.json"),
    JSON.stringify(results, null, 2) + "\n",
  );
  console.log(JSON.stringify(results, null, 2));
}
