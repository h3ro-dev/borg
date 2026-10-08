import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { execFileSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import { resolveLaunchBusConfig, reconcileLaunches, closeLaunch, launchWork } from './launch-bus.mjs';

function fixture(t) {
  const home = fs.mkdtempSync(path.join(os.tmpdir(), 'launch-reconcile-'));
  t.after(() => fs.rmSync(home, { recursive: true, force: true }));
  const product = { borgHome: home, providers: { claude: { enabled: true, binary: '/bin/sh' } } };
  const config = resolveLaunchBusConfig(product);
  for (const name of ['active', 'done', 'failed', 'unread', 'logs']) fs.mkdirSync(path.join(config.launchRoot, name), { recursive: true });
  const file = (folder, id, ext = 'json') => path.join(config.launchRoot, folder, `${id}.${ext}`);
  function record(id, result = false, runtime = 'claude') {
    const row = { schemaVersion: 1, workId: id, runtime, launchedBy: 'fixture', launchedAt: new Date().toISOString(),
      state: 'STARTED', pid: 12345, notifyId: id, logFile: file('logs', id, 'log') };
    for (const folder of ['active', 'unread']) fs.writeFileSync(file(folder, id), JSON.stringify(row));
    fs.writeFileSync(row.logFile, 'last log line\n');
    if (result) fs.writeFileSync(file('done', id, 'md'), 'fixture result');
    return row;
  }
  const run = (options = {}) => reconcileLaunches({ config: product, processState: () => 'gone', ...options });
  const ledger = () => fs.readFileSync(path.join(config.launchRoot, 'ledger.jsonl'), 'utf8').trim().split('\n').map(JSON.parse);
  return { home, product, config, file, record, run, ledger };
}

test('dead launches close with fresh results or explicit failure, preserving unread and attempt identity', (t) => {
  const f = fixture(t); const good = f.record('good', true); f.record('bad');
  const result = f.run();
  assert.deepEqual(result.closed, { FINISHED: 1, FAILED: 1 });
  assert.equal(JSON.parse(fs.readFileSync(f.file('done', 'good'))).outcome, 'RESULT_WRITTEN');
  assert.equal(JSON.parse(fs.readFileSync(f.file('failed', 'bad'))).outcome, 'NO_RESULT');
  assert.equal(f.ledger().find((r) => r.workId === 'good').launchedAt, good.launchedAt);
  assert.equal(f.ledger().find((r) => r.workId === 'good').resultSha256.length, 64);
  assert.ok(fs.existsSync(f.file('unread', 'good')));
  assert.equal(fs.readdirSync(path.join(f.config.launchRoot, 'active')).length, 0);
  assert.equal(f.run().scanned, 0);
  assert.equal(f.ledger().length, 2);
});

test('running and unknown stay untouched, reused pid closes, other runtimes skipped', (t) => {
  const f = fixture(t);
  for (const id of ['running', 'unknown', 'other']) f.record(id);
  f.record('gemini', false, 'gemini');
  const result = f.run({ processState: (r) => r.workId });
  assert.equal(result.live, 1); assert.equal(result.unknown, 1); assert.equal(result.skipped, 1);
  assert.equal(result.closed.FAILED, 1);
  assert.ok(fs.existsSync(f.file('active', 'running')));
  assert.ok(fs.existsSync(f.file('active', 'unknown')));
});

test('stale result fails with priorResult', (t) => {
  const f = fixture(t); f.record('stale', true);
  fs.utimesSync(f.file('done', 'stale', 'md'), new Date(0), new Date(0));
  f.run(); assert.equal(JSON.parse(fs.readFileSync(f.file('failed', 'stale'))).priorResult, true);
});

test('dry run changes no files and work filter limits selection', (t) => {
  const f = fixture(t); f.record('one', true); f.record('two');
  const snapshot = () => fs.readdirSync(f.home, { recursive: true }).sort().map((p) => {
    const target = path.join(f.home, p); return [p, fs.statSync(target).isFile() ? fs.readFileSync(target).toString('base64') : null];
  });
  const before = snapshot();
  assert.equal(f.run({ dryRun: true, workId: 'one' }).scanned, 1);
  assert.deepEqual(snapshot(), before);
});

test('closing an old attempt preserves the newer active attempt and is idempotent', (t) => {
  const f = fixture(t); const old = f.record('same');
  const newer = { ...old, launchedAt: new Date(Date.now() + 5000).toISOString() };
  fs.writeFileSync(f.file('active', 'same'), JSON.stringify(newer));
  const closure = { state: 'FAILED', outcome: 'NO_RESULT', closedBy: 'launch-bus reconcile', finishedAt: new Date().toISOString() };
  closeLaunch(f.config, old, closure); closeLaunch(f.config, old, closure);
  assert.deepEqual(JSON.parse(fs.readFileSync(f.file('active', 'same'))), newer);
  assert.equal(f.ledger().length, 1);
});

test('CLI reconcile dry-run JSON', (t) => {
  const f = fixture(t);
  fs.mkdirSync(path.join(f.home, 'conductors'));
  fs.writeFileSync(path.join(f.home, 'conductors/config.json'), JSON.stringify(f.product));
  const output = execFileSync(process.execPath, [fileURLToPath(new URL('./launch-bus.mjs', import.meta.url)), 'reconcile', '--dry-run', '--json'],
    { env: { ...process.env, BORG_HOME: f.home }, encoding: 'utf8' });
  assert.equal(JSON.parse(output).scanned, 0); assert.equal(JSON.parse(output).dryRun, true);
});

for (const code of [3, 0]) test(`exit handler records code ${code} without reconcile`, async (t) => {
  const f = fixture(t); const id = `exit-${code}`;
  const binary = path.join(f.home, 'fake-claude');
  fs.writeFileSync(binary, `#!/bin/sh\ncat >/dev/null\n${code === 0 ? 'printf result > "$BORG_TEST_RESULT_FILE"\n' : ''}exit ${code}\n`, { mode: 0o700 });
  f.product.providers.claude.binary = binary;
  const previous = process.env.BORG_TEST_RESULT_FILE;
  process.env.BORG_TEST_RESULT_FILE = f.file('done', id, 'md');
  t.after(() => { if (previous === undefined) delete process.env.BORG_TEST_RESULT_FILE; else process.env.BORG_TEST_RESULT_FILE = previous; });
  await launchWork({ workId: id, runtime: 'claude', cwd: f.home, prompt: 'fixture' }, { config: f.product });
  const until = Date.now() + 5000;
  while (Date.now() < until && fs.existsSync(f.file('active', id))) await new Promise((resolve) => setTimeout(resolve, 20));
  const row = f.ledger().find((r) => r.closedBy === 'launch-bus exit');
  assert.ok(row); assert.equal(row.state, code === 0 ? 'FINISHED' : 'FAILED');
  assert.equal(row.outcome, code === 0 ? 'RESULT_WRITTEN' : 'EXIT_3');
});
