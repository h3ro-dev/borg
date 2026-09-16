// Configurator browser acceptance; invoked by verify.mjs against the canonical planner.
import assert from 'node:assert/strict';
import { readFile, writeFile } from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { execFileSync } from 'node:child_process';

export async function verifyConfigurator({ page, origin, evidence, results }) {
  const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
  const catalog = JSON.parse(await readFile(path.join(root, 'platform/catalog.json')));
  const { validateBlueprint, planBlueprint } = await import('../platform/planner.mjs');
  await page.goto(origin + '/borg/#configure');
  await page.locator('#download-blueprint').waitFor();
  while (await page.locator('#choices-component .choice-category:not([open]) > summary').count()) await page.locator('#choices-component .choice-category:not([open]) > summary').first().click();
  const download = async name => {
    const pending = page.waitForEvent('download');
    await page.locator('#download-blueprint').click();
    const file = await pending;
    assert.equal(file.suggestedFilename(), 'borg-blueprint.json');
    const target = path.join(evidence, name + '.json');
    await file.saveAs(target);
    const blueprint = JSON.parse(await readFile(target));
    assert.deepEqual(validateBlueprint(blueprint, catalog), { valid: true, errors: [] });
    assert.deepEqual(Object.keys(blueprint).sort(), ['catalog_version', 'goal', 'machines', 'schema']);
    return blueprint;
  };
  const baseline = await download('blueprint-default');
  assert(!await page.locator('.blueprint-delivery').evaluate(node => [...node.childNodes].some(child => child.nodeType === Node.TEXT_NODE && child.textContent.trim() === 'null')));
  assert.equal(baseline.machines.length, 1);
  assert.equal(baseline.machines[0].profile, 'full');
  const baselineRam = planBlueprint(baseline, catalog).machines[0].estimate.ram_gb;
  await page.locator('#choice-codex').uncheck();
  assert(!(await page.locator('#choice-router').isChecked()));
  await page.locator('#choice-router').check();
  assert(await page.locator('#choice-codex').isChecked());
  await page.locator('#choice-claude').check();
  assert(await page.locator('#choice-launch-bus').isChecked());
  await page.locator('#choice-cursor').check();
  assert(await page.locator('#choice-launch-bus').isChecked());
  await page.locator('#choice-training').check();
  assert(await page.locator('#choice-adapters').isChecked());
  const training = await download('blueprint-training');
  assert.equal(training.machines[0].workload.training, true);
  assert(planBlueprint(training, catalog).machines[0].estimate.ram_gb > baselineRam);
  await page.locator('#machine-profile').selectOption('tools');
  assert(await page.locator('#choice-training').isDisabled());
  assert(!(await page.locator('#choice-training').isChecked()));
  const tools = await download('blueprint-tools');
  assert.equal(tools.machines[0].workload.training, false);
  assert(!tools.machines[0].components.includes('adapters'));
  assert(planBlueprint(tools, catalog).machines[0].estimate.ram_gb < planBlueprint(training, catalog).machines[0].estimate.ram_gb);
  results.checks.push('Dependencies added and removed transitively; training synchronized; role switch removes incompatible choices');

  await page.locator('#machine-profile').selectOption('full');
  await page.locator('#choice-desktop').check();
  await page.locator('#machine-platform').selectOption('linux-x64');
  assert(await page.locator('#choice-desktop').isDisabled());
  assert(!(await page.locator('#choice-desktop').isChecked()));
  assert.match(await page.locator('.platform-note').textContent(), /acceptance is pending/);
  await page.locator('#machine-platform').selectOption('windows');
  await download('blueprint-windows');
  assert(!(await page.locator('#blueprint-commands').inputValue()).includes('./install.sh'));
  assert.match(await page.locator('.blueprint-delivery').textContent(), /Windows.*unsupported/);
  await page.locator('#machine-platform').selectOption('macos-arm64');
  results.checks.push('Linux compatibility removals and pending acceptance; Windows blueprint export but no install command');

  await page.locator('#workload-agents').fill('129');
  assert(await page.locator('#blueprint-errors').isVisible());
  assert.equal(await page.locator('#workload-agents').getAttribute('aria-invalid'), 'true');
  await page.locator('#download-blueprint').click();
  assert.match(await page.locator('#configure-status').textContent(), /Download paused/);
  assert.equal(await page.locator('#blueprint-commands').count(), 0);
  await page.locator('#workload-agents').fill('1.5');
  assert(await page.locator('#blueprint-errors').isVisible());
  await page.locator('#workload-agents').fill('');
  assert(await page.locator('#blueprint-errors').isVisible());
  await page.locator('#workload-agents').fill('12');
  await page.locator('#machine-label').fill(' ');
  assert(await page.locator('#blueprint-errors').isVisible());
  await page.locator('#machine-label').fill('<img src=x onerror=alert(1)>');
  assert.equal(await page.locator('.machine-summary img').count(), 0);
  const escaped = await download('blueprint-label');
  assert.equal(escaped.machines[0].label, '<img src=x onerror=alert(1)>');
  await page.locator('#machine-label').fill('Build machine');
  const larger = await download('blueprint-larger');
  assert(planBlueprint(larger, catalog).machines[0].estimate.ram_gb > baselineRam);
  results.checks.push('Bounds, integers, blank fields, whitespace labels and safe text rendering; invalid export blocked; estimates grow with workload');

  await page.locator('#add-machine').click();
  assert.equal(await page.locator('#machine-label').inputValue(), 'Node 2');
  await page.locator('#machine-preset').selectOption('custom');
  await page.locator('#machine-profile').selectOption('tools');
  await page.locator('#choices-integration > summary').click();
  while (await page.locator('#choices-integration .choice-category:not([open]) > summary').count()) await page.locator('#choices-integration .choice-category:not([open]) > summary').first().click();
  await page.locator('#choice-github').check();
  assert(await page.locator('#choice-figma').isVisible());
  await page.locator('#choice-figma').check();
  await page.locator('#machine-label').fill('Worker machine');
  const multi = await download('blueprint-multiple');
  assert.deepEqual(multi.machines.map(machine => machine.id), ['node-1', 'node-2']);
  assert.deepEqual(multi.machines[1].integrations, ['github', 'figma']);
  const estimates = planBlueprint(multi, catalog).machines.map(machine => machine.estimate.ram_gb);
  assert(estimates[0] > estimates[1]);
  await page.locator('#select-node-1').click();
  assert.equal(await page.locator('#machine-label').inputValue(), 'Build machine');
  await page.locator('#remove-machine').click();
  await page.locator('#add-machine').click();
  assert.equal(await page.locator('#select-node-3').count(), 1);
  assert.equal(await page.locator('#select-node-1').count(), 0);
  const stable = await download('blueprint-stable-ids');
  assert.deepEqual(stable.machines.map(machine => machine.id), ['node-2', 'node-3']);
  results.checks.push('Multiple machines retain independent workload/choices; integration selections exported; removed IDs are never reused');

  for (const preset of catalog.goals) {
    await page.locator('#machine-preset').selectOption(preset.id);
    await download('blueprint-preset-' + preset.id);
  }
  await page.locator('#choice-codex').focus();
  await page.keyboard.press('Space');
  assert(await page.locator('#choice-codex').isChecked());
  assert.equal(await page.locator('#choice-codex').evaluate(node => node === document.activeElement), true);
  await page.keyboard.press('Tab');
  assert.equal(await page.locator('#details-codex > summary').evaluate(node => node === document.activeElement), true);
  await page.keyboard.press('Enter');
  assert.equal(await page.locator('#details-codex').getAttribute('open'), '');
  results.checks.push('All catalog presets export valid blueprints; keyboard checkbox focus survives dependency update and details open with Enter');

  await page.evaluate(() => { navigator.clipboard.writeText = async text => { window.__copied = text; }; });
  await page.locator('#copy-blueprint-commands').click();
  assert.equal(await page.evaluate(() => window.__copied), await page.locator('#blueprint-commands').inputValue());
  await page.evaluate(() => { navigator.clipboard.writeText = async () => { throw new Error('Denied'); }; });
  await page.locator('#copy-blueprint-commands').click();
  assert.match(await page.locator('#configure-status').textContent(), /Clipboard unavailable/);
  assert.equal(await page.locator('#blueprint-commands').evaluate(node => node.selectionEnd - node.selectionStart), (await page.locator('#blueprint-commands').inputValue()).length);
  results.checks.push('Exact CLI commands copied; clipboard denial leaves focused selected text for manual copy');

  for (const width of [320, 390, 768, 1440]) {
    await page.setViewportSize({ width, height: 960 });
    await page.locator('#configure').scrollIntoViewIfNeeded();
    assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), `Configurator overflow at ${width}`);
    if (process.env.AXE_SCRIPT) {
      await page.addScriptTag({ path: process.env.AXE_SCRIPT });
      const audit = await page.evaluate(() => axe.run(document.querySelector('#configure'), { runOnly: { type: 'tag', values: ['wcag2a', 'wcag2aa', 'wcag21aa'] } }));
      await writeFile(path.join(evidence, `configure-axe-${width}.json`), JSON.stringify(audit.violations, null, 2));
      assert.equal(audit.violations.length, 0, `axe configurator violations at ${width}`);
    }
    await page.locator('#configure').screenshot({ path: path.join(evidence, `configure-${width}.png`), animations: 'disabled', timeout: 60000 });
  }
  results.checks.push('Configurator with multiple machines fits 320/390/768/1440px; axe WCAG 2/2.1 AA zero violations when AXE_SCRIPT supplied');
  // Keep CLI roundtrip opt-in until the installer lane is integrated; record proof explicitly.
  const cliRoot = process.env.BLUEPRINT_CLI_ROOT;
  if (cliRoot) {
    for (const name of ['default', 'training', 'tools', 'windows', 'label', 'larger', 'multiple', 'stable-ids', ...catalog.goals.map(goal => 'preset-' + goal.id)]) {
      const output = execFileSync('python3', [path.join(cliRoot, 'borg.py'), 'blueprint', 'inspect', path.join(evidence, 'blueprint-' + name + '.json')], { cwd: cliRoot, encoding: 'utf8' });
      await writeFile(path.join(evidence, 'cli-' + name + '.txt'), output);
    }
    results.checks.push('Every browser-exported blueprint roundtrips frozen python3 borg.py blueprint inspect CLI');
  } else results.checks.push('CLI roundtrip pending: set BLUEPRINT_CLI_ROOT to integrated installer checkout');
}
