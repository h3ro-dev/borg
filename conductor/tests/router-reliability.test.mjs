import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import test from 'node:test';
import * as router from '../router/router.mjs';
import { buildDefaultConfig } from '../config.mjs';

const NOW = Date.parse('2026-09-16T12:00:00Z');
const account = { type: 'chatgpt', email: 'owner@example.test', planType: 'pro' };
const observation = () => ({ observedAt: new Date(NOW).toISOString(), reachable: true, loadPerCore: 0.2, memoryUsePercent: 35 });
const claims = (active = []) => ({ observedAt: new Date(NOW).toISOString(), active });
function fixture(t) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'borg-reliability-'));
  fs.chmodSync(root, 0o700);
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  const config = buildDefaultConfig(root, process.execPath, { nodeBin: process.execPath });
  config.statePath = path.join(root, 'private/router');
  config.conductors[0].accountPin = router.accountIdentityDigest(account);
  const calls = [];
  const native = async (lane, op) => {
    calls.push(op.kind);
    if (op.kind === 'status') return { ok: true, port: lane.port, codexHome: lane.codexHome, supportedRoles: ['leaf','lead'], threads: {} };
    if (op.method === 'account/read') return { account };
    if (op.method === 'account/rateLimits/read') return { rateLimits: { limitId: 'codex', primary: { usedPercent: 5, windowDurationMins: 300, resetsAt: '2026-09-17T12:00:00Z' } } };
    if (op.kind === 'thread-start') return { threadId: 'thread-test' };
    if (op.kind === 'turn-start') return { turnId: 'turn-test' };
    throw new Error('unexpected operation');
  };
  const options = { cwd: root, workId: 'repair', prompt: 'Synthetic acceptance only', nowMs: NOW, stageTimeoutMs: 2000,
    capacityProvider: async () => observation(), claimsProvider: async () => claims(), conductorProvider: native };
  return { root, config, options, native, calls };
}
function putReceipt(config, name, value) {
  const dir = path.join(config.statePath, 'dispatch-receipts');
  fs.mkdirSync(dir, { recursive: true, mode: 0o700 });
  fs.writeFileSync(path.join(dir, `${name}.json`), JSON.stringify(value), { mode: 0o600 });
}
async function bounded(promise, ms = 1500) {
  let timer;
  try { return await Promise.race([promise, new Promise((_, reject) => { timer = setTimeout(() => reject(new Error('TEST_WATCHDOG_EXPIRED')), ms); })]); }
  finally { clearTimeout(timer); }
}

for (const value of [null, false, true, '', ' ', [], {}, '0']) {
  test(`unknown telemetry ${JSON.stringify(value)} is not safe zero`, () => {
    const machine = { id: 'local', capacity: { maxAgeMs: 15000, loadPerCoreLimit: 1.5, memoryUseLimitPercent: 92 }, claims: { maxAgeMs: 15000 } };
    for (const field of ['loadPerCore', 'memoryUsePercent']) {
      const result = router.evaluateMachineAdmission(machine, { ...observation(), [field]: value }, claims(), NOW);
      assert.equal(result.eligible, false, `${field}=${JSON.stringify(value)} must refuse`);
      assert.ok(result.issues.includes(field === 'loadPerCore' ? 'LOAD_UNKNOWN' : 'MEMORY_UNKNOWN'));
    }
  });
}
test('missing native usage never creates a worker', async (t) => {
  const f = fixture(t);
  await assert.rejects(router.dispatch(f.config, { ...f.options, conductorProvider: async (lane, op) => {
    if (op.method === 'account/rateLimits/read') return { rateLimits: { limitId: 'codex', primary: { usedPercent: null, windowDurationMins: 300, resetsAt: '2026-09-17T12:00:00Z' } } };
    return f.native(lane, op);
  } }), /PRIMARY_USED_INVALID/);
  assert.ok(!f.calls.includes('thread-start'));
});
test('DISPATCHED and uncertain receipts remain visible as active claims', async (t) => {
  const f = fixture(t);
  for (const state of ['DISPATCHED','ATTEMPTING','THREAD_STARTED','UNKNOWN_DO_NOT_RETRY','STARTED_TURN_UNKNOWN']) {
    putReceipt(f.config, state, { state, workId: state, cwd: f.root, attemptId: state });
  }
  const result = await router.observeClaims(f.config.machines[0], f.config.statePath, NOW);
  assert.equal(result.active.length, 5);
  assert.equal(result.active.find(row => row.state === 'DISPATCHED').cwd, f.root);
});
test('a differently named work item cannot bypass an existing workspace claim', async (t) => {
  const f = fixture(t);
  await assert.rejects(router.dispatch(f.config, { ...f.options,
    claimsProvider: async () => claims([{ workId: 'other-work', cwd: f.root, state: 'DISPATCHED', attemptId: 'other-attempt' }])
  }), /WORKSPACE_CLAIM_CONFLICT/);
  assert.ok(!f.calls.includes('thread-start'));
});
test('work identity stays reserved across different workspace paths', async (t) => {
  const f = fixture(t);
  await assert.rejects(router.dispatch(f.config, { ...f.options,
    claimsProvider: async () => claims([{ workId: 'repair', cwd: path.join(f.root, 'another'), state: 'UNKNOWN_DO_NOT_RETRY', attemptId: 'other-attempt' }])
  }), /WORK_ID_CLAIM_CONFLICT/);
  assert.ok(!f.calls.includes('thread-start'));
});
test('unsafe JSON symlink does not silently turn into empty claims', async (t) => {
  const f = fixture(t); putReceipt(f.config, 'real', { state: 'DISPATCHED', workId: 'owned' });
  fs.symlinkSync(path.join(f.config.statePath,'dispatch-receipts','real.json'), path.join(f.config.statePath,'dispatch-receipts','alias.json'));
  await assert.rejects(router.observeClaims(f.config.machines[0], f.config.statePath, NOW), /UNSAFE_RECEIPT/);
});
test('unknown receipt state fails closed rather than disappearing', async (t) => {
  const f = fixture(t); putReceipt(f.config, 'future', { state: 'NEW_UNKNOWN_STATE', workId: 'owned' });
  await assert.rejects(router.observeClaims(f.config.machines[0], f.config.statePath, NOW), /RECEIPT_STATE_UNKNOWN/);
});
test('admission failure leaves a status receipt with no native start', async (t) => {
  const f = fixture(t);
  await assert.rejects(router.dispatch(f.config, { ...f.options, capacityProvider: async () => null }), /CAPACITY_UNKNOWN/);
  assert.equal(typeof router.inspectDispatch, 'function', 'restart-safe status lookup must exist');
  const result = await router.inspectDispatch(f.config, { cwd: f.root, workId: 'repair' });
  assert.equal(result.receipt.state, 'PRE_START_FAILED');
  assert.equal(result.receipt.threadId, null);
  assert.equal(result.noStartProven, true);
  assert.equal(result.completionVerified, false);
  assert.ok(result.receipt.events.some(event => event.phase === 'ADMISSION'));
});
test('stalled admission is bounded and records its exact stage', async (t) => {
  const f = fixture(t);
  await assert.rejects(bounded(router.dispatch(f.config, { ...f.options, stageTimeoutMs: 40,
    capacityProvider: async () => new Promise(() => {})
  })), /CAPACITY_TIMEOUT/);
  const result = await router.inspectDispatch(f.config, { cwd: f.root, workId: 'repair' });
  assert.equal(result.receipt.state, 'PRE_START_FAILED');
  assert.ok(!f.calls.includes('thread-start'));
});
test('thread timeout blocks replay and a late response cannot start a turn', async (t) => {
  const f = fixture(t); let finishStart;
  await assert.rejects(bounded(router.dispatch(f.config, { ...f.options, stageTimeoutMs: 40,
    conductorProvider: async (lane, op) => {
      if (op.kind === 'thread-start') { f.calls.push(op.kind); return new Promise(resolve => { finishStart = resolve; }); }
      return f.native(lane, op);
    }
  })), /outcome unknown.*do not retry/i);
  const result = await router.inspectDispatch(f.config, { cwd: f.root, workId: 'repair' });
  assert.equal(result.receipt.state, 'UNKNOWN_DO_NOT_RETRY');
  assert.equal(result.receipt.errorClass, 'THREAD_START_TIMEOUT');
  assert.equal(result.noStartProven, false);
  finishStart({ threadId: 'late-thread' });
  await new Promise(resolve => setImmediate(resolve));
  assert.ok(!f.calls.includes('turn-start'));
  await assert.rejects(router.dispatch(f.config, f.options), /duplicate intent.*do not retry/i);
  assert.equal(f.calls.filter(kind => kind === 'thread-start').length, 1);
});
test('successful dispatch exposes IDs but never claims completed work', async (t) => {
  const f = fixture(t); const sent = await router.dispatch(f.config, f.options);
  const result = await router.inspectDispatch(f.config, { cwd: f.root, workId: 'repair' });
  assert.equal(result.receipt.attemptId, sent.receipt.attemptId);
  assert.equal(result.receipt.state, 'DISPATCHED');
  assert.equal(result.receipt.threadId, 'thread-test');
  assert.equal(result.receipt.turnId, 'turn-test');
  assert.equal(result.completionVerified, false);
  assert.equal(result.noStartProven, false);
  assert.ok(result.receipt.events.some(event => event.phase === 'RECHECK'));
  assert.ok(!JSON.stringify(result).includes(f.options.prompt));
});
test('missing status receipt is not evidence of a safe retry', async (t) => {
  const f = fixture(t);
  assert.equal(typeof router.inspectDispatch, 'function');
  const result = await router.inspectDispatch(f.config, { cwd: f.root, workId: 'never-recorded' });
  assert.equal(result.found, false);
  assert.equal(result.noStartProven, false);
});

import { spawnSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';
test('CLI route-status reads persisted IDs without dispatching or probing providers', async (t) => {
  const f = fixture(t); await router.dispatch(f.config, f.options);
  const configPath = path.join(f.root,'conductors','config.json');
  fs.mkdirSync(path.dirname(configPath), { recursive: true, mode: 0o700 });
  fs.writeFileSync(configPath, JSON.stringify(f.config), { mode: 0o600 });
  const result = spawnSync(process.execPath, [fileURLToPath(new URL('../borg-conductor.mjs', import.meta.url)),
    'route-status', '--cwd', f.root, '--work-id', 'repair'],
    { encoding: 'utf8', timeout: 20000, env: { ...process.env, BORG_HOME: f.root } });
  assert.equal(result.status, 0, result.stderr);
  const status = JSON.parse(result.stdout);
  assert.equal(status.receipt.threadId, 'thread-test');
  assert.equal(status.receipt.turnId, 'turn-test');
  assert.equal(status.completionVerified, false);
  assert.equal(f.calls.filter(kind => kind === 'thread-start').length, 1);
});

test('a timed-out candidate does not block an eligible fallback before native start', async (t) => {
  const f = fixture(t);
  f.config.conductors.push({ ...f.config.conductors[0], id: 'backup', accountProfile: 'backup', port: 4748,
    codexHome: path.join(f.root,'conductors/backup/profile'), logsPath: path.join(f.root,'conductors/backup/logs') });
  const result = await router.dispatch(f.config, { ...f.options, stageTimeoutMs: 40,
    conductorProvider: async (lane, op) => {
      if (lane.id === 'primary' && op.kind === 'status') return new Promise(() => {});
      return f.native(lane, op);
    } });
  assert.equal(result.receipt.laneId, 'backup');
  assert.equal(f.calls.filter(kind => kind === 'thread-start').length, 1);
  assert.equal(f.calls.filter(kind => kind === 'turn-start').length, 1);
});
test('turn timeout keeps native thread identity and blocks a replacement launch', async (t) => {
  const f = fixture(t);
  await assert.rejects(router.dispatch(f.config, { ...f.options, stageTimeoutMs: 40,
    conductorProvider: async (lane, op) => {
      if (op.kind === 'turn-start') { f.calls.push(op.kind); return new Promise(() => {}); }
      return f.native(lane, op);
    } }), /turn outcome unknown.*do not retry/i);
  const status = await router.inspectDispatch(f.config, { cwd: f.root, workId: 'repair' });
  assert.equal(status.receipt.state, 'STARTED_TURN_UNKNOWN');
  assert.equal(status.receipt.threadId, 'thread-test');
  assert.equal(status.receipt.errorClass, 'TURN_START_TIMEOUT');
  assert.equal(status.noStartProven, false);
  await assert.rejects(router.dispatch(f.config, f.options), /duplicate intent.*do not retry/i);
  assert.equal(f.calls.filter(kind => kind === 'thread-start').length, 1);
});
test('concurrent same-intent calls start exactly one native thread', async (t) => {
  const f = fixture(t);
  const results = await Promise.allSettled([router.dispatch(f.config, f.options), router.dispatch(f.config, f.options)]);
  assert.equal(results.filter(result => result.status === 'fulfilled').length, 1);
  assert.match(results.find(result => result.status === 'rejected').reason.message, /duplicate intent.*do not retry/i);
  assert.equal(f.calls.filter(kind => kind === 'thread-start').length, 1);
});
