import assert from 'node:assert/strict';
import crypto from 'node:crypto';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import test from 'node:test';
import * as router from '../router/router.mjs';
import { buildDefaultConfig } from '../config.mjs';

// Workspace and work-ID ownership regressions for ROUTER-REVIEW R1/R2. The
// configured claims collector runs unless a test injects external claims.
const NOW = Date.parse('2026-09-16T12:00:00Z');
const account = { type: 'chatgpt', email: 'owner@example.test', planType: 'pro' };
const claims = (active = []) => ({ observedAt: new Date(NOW).toISOString(), active });
function fixture(t, { collector = 'router-state' } = {}) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'borg-ownership-'));
  fs.chmodSync(root, 0o700);
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  const workspace = path.join(root, 'workspace');
  fs.mkdirSync(path.join(workspace, 'child'), { recursive: true });
  fs.mkdirSync(path.join(root, 'workspace-other'));
  fs.symlinkSync(workspace, path.join(root, 'alias'));
  const config = buildDefaultConfig(root, process.execPath, { nodeBin: process.execPath });
  config.conductors[0].accountPin = router.accountIdentityDigest(account);
  if (collector === 'command') {
    config.machines[0].claims = { kind: 'command', command: process.execPath, maxAgeMs: 15000,
      args: ['-e', `console.log(JSON.stringify({observedAt:${JSON.stringify(new Date(NOW).toISOString())},active:[]}))`] };
  }
  const calls = []; const bodies = [];
  const native = async (lane, op) => {
    calls.push(op.kind);
    if (op.kind === 'status') return { ok: true, port: lane.port, codexHome: lane.codexHome, supportedRoles: ['leaf'], threads: {} };
    if (op.method === 'account/read') return { account };
    if (op.method === 'account/rateLimits/read') return { rateLimits: { limitId: 'codex', primary: { usedPercent: 5, windowDurationMins: 300, resetsAt: '2026-09-17T12:00:00Z' } } };
    if (op.kind === 'thread-start') { bodies.push(op.body); return { threadId: `thread-${bodies.length}` }; }
    if (op.kind === 'turn-start') return { turnId: `turn-${bodies.length}` };
    throw new Error('unexpected operation');
  };
  const options = { cwd: workspace, workId: 'first', prompt: 'Synthetic ownership check only', nowMs: NOW, stageTimeoutMs: 5000,
    capacityProvider: async () => ({ observedAt: new Date(NOW).toISOString(), reachable: true, loadPerCore: 0.2, memoryUsePercent: 35 }),
    conductorProvider: native };
  const starts = () => calls.filter((kind) => kind === 'thread-start').length;
  return { root, workspace, config, options, native, calls, bodies, starts };
}
const route = (f, changes) => router.dispatch(f.config, { ...f.options, ...changes });

test('command claims collector cannot hide this router\'s dispatched workspace claim', async (t) => {
  const f = fixture(t, { collector: 'command' });
  assert.equal((await route(f, {})).receipt.state, 'DISPATCHED');
  await assert.rejects(route(f, { workId: 'second' }), /WORKSPACE_CLAIM_CONFLICT/);
  assert.equal(f.starts(), 1);
  assert.equal(f.calls.filter((kind) => kind === 'turn-start').length, 1);
  const refused = await router.inspectDispatch(f.config, { cwd: f.workspace, workId: 'second' });
  assert.equal(refused.receipt.state, 'PRE_START_FAILED');
  assert.equal(refused.noStartProven, true);
});
test('command claims collector cannot hide an uncertain launch or its work ID', async (t) => {
  const f = fixture(t, { collector: 'command' });
  await assert.rejects(route(f, { conductorProvider: async (lane, op) => {
    if (op.kind === 'thread-start') { f.calls.push(op.kind); throw Object.assign(new Error('socket hang up'), { code: 'ECONNRESET' }); }
    return f.native(lane, op);
  } }), /outcome unknown.*do not retry/i);
  assert.equal((await router.inspectDispatch(f.config, { cwd: f.workspace, workId: 'first' })).receipt.state, 'UNKNOWN_DO_NOT_RETRY');
  await assert.rejects(route(f, { workId: 'second' }), /WORKSPACE_CLAIM_CONFLICT/);
  await assert.rejects(route(f, { cwd: path.join(f.root, 'workspace-other') }), /WORK_ID_CLAIM_CONFLICT/);
  await assert.rejects(route(f, {}), /duplicate intent.*do not retry/i);
  assert.equal(f.starts(), 1);
});
test('a failed local receipt scan refuses dispatch instead of trusting empty external claims', async (t) => {
  const f = fixture(t, { collector: 'command' });
  const receipts = path.join(f.config.statePath, 'dispatch-receipts');
  fs.mkdirSync(receipts, { recursive: true, mode: 0o700 });
  fs.writeFileSync(path.join(receipts, 'shared.json'), JSON.stringify({ state: 'DISPATCHED', workId: 'owned', cwd: f.workspace }), { mode: 0o644 });
  await assert.rejects(route(f, { cwd: path.join(f.root, 'workspace-other') }), /UNSAFE_RECEIPT/);
  const status = await router.inspectDispatch(f.config, { cwd: path.join(f.root, 'workspace-other'), workId: 'first' });
  assert.equal(status.receipt.state, 'PRE_START_FAILED');
  assert.equal(status.receipt.errorClass, 'UNSAFE_RECEIPT');
  assert.equal(f.starts(), 0);
});
test('an active local receipt without a workspace fails the ledger scan closed', async (t) => {
  const f = fixture(t, { collector: 'command' });
  const receipts = path.join(f.config.statePath, 'dispatch-receipts');
  fs.mkdirSync(receipts, { recursive: true, mode: 0o700 });
  fs.writeFileSync(path.join(receipts, 'no-cwd.json'), JSON.stringify({ state: 'UNKNOWN_DO_NOT_RETRY', workId: 'owned', attemptId: 'owned' }), { mode: 0o600 });
  await assert.rejects(route(f, { cwd: path.join(f.root, 'workspace-other') }), /RECEIPT_CWD_INVALID/);
  assert.equal(f.starts(), 0);
});
test('an alias of a claimed workspace is the same workspace for claims, intents and status', async (t) => {
  const f = fixture(t); const alias = path.join(f.root, 'alias'); const canonical = fs.realpathSync.native(f.workspace);
  const first = await route(f, {});
  assert.equal(first.receipt.cwd, canonical);
  assert.equal(f.bodies[0].cwd, canonical, 'the native thread starts in the claimed identity');
  await assert.rejects(route(f, { cwd: alias, workId: 'second' }), /WORKSPACE_CLAIM_CONFLICT/);
  await assert.rejects(route(f, { cwd: alias }), /duplicate intent.*do not retry/i);
  for (const cwd of [alias, f.workspace, canonical]) {
    assert.equal((await router.inspectDispatch(f.config, { cwd, workId: 'first' })).receipt.attemptId, first.receipt.attemptId);
  }
  assert.equal(f.starts(), 1);
});
test('parent and child workspaces conflict in both directions, including through an alias', async (t) => {
  const f = fixture(t); const child = path.join(f.workspace, 'child');
  await route(f, {});
  await assert.rejects(route(f, { cwd: child, workId: 'nested' }), /WORKSPACE_CLAIM_CONFLICT/);
  await assert.rejects(route(f, { cwd: path.join(f.root, 'alias', 'child'), workId: 'nested-alias' }), /WORKSPACE_CLAIM_CONFLICT/);
  const g = fixture(t);
  await route(g, { cwd: path.join(g.workspace, 'child') });
  await assert.rejects(route(g, { workId: 'parent' }), /WORKSPACE_CLAIM_CONFLICT/);
  assert.equal(f.starts() + g.starts(), 2);
});
test('sibling worktrees sharing a name prefix remain independently admissible', async (t) => {
  const f = fixture(t);
  assert.equal((await route(f, {})).receipt.state, 'DISPATCHED');
  assert.equal((await route(f, { cwd: path.join(f.root, 'workspace-other'), workId: 'sibling' })).receipt.state, 'DISPATCHED');
  assert.equal(f.starts(), 2);
});
test('a letter-case spelling of a claimed workspace conflicts on case-insensitive volumes', async (t) => {
  const f = fixture(t); const upper = path.join(f.root, 'WORKSPACE');
  if (!fs.existsSync(upper)) { t.skip('temporary volume is case-sensitive'); return; }
  await route(f, {});
  await assert.rejects(route(f, { cwd: upper, workId: 'second' }), /WORKSPACE_CLAIM_CONFLICT/);
  assert.equal(f.starts(), 1);
});
test('a firmlinked spelling realpath keeps distinct conflicts by device and inode', async (t) => {
  const f = fixture(t); const firmlinked = path.join('/System/Volumes/Data', fs.realpathSync.native(f.workspace));
  let same = false;
  try { const [a, b] = [fs.statSync(f.workspace), fs.statSync(firmlinked)]; same = a.dev === b.dev && a.ino === b.ino; } catch { /* not macOS */ }
  if (!same || fs.realpathSync.native(firmlinked) === fs.realpathSync.native(f.workspace)) { t.skip('no distinct firmlinked spelling here'); return; }
  await route(f, {});
  await assert.rejects(route(f, { cwd: firmlinked, workId: 'second' }), /WORKSPACE_CLAIM_CONFLICT/);
  assert.equal(f.starts(), 1);
});
test('external claims resolve aliases and deleted paths; malformed ones refuse, absent ones reserve only work IDs', async (t) => {
  const f = fixture(t); const other = path.join(f.root, 'workspace-other'); const deleted = path.join(f.workspace, 'deleted', 'deeper');
  const external = (claim) => ({ claimsProvider: async () => claims([{ workId: 'external', state: 'DISPATCHED', attemptId: 'external', ...claim }]) });
  await assert.rejects(route(f, { workId: 'w1', ...external({ cwd: path.join(f.root, 'alias') }) }), /WORKSPACE_CLAIM_CONFLICT/);
  await assert.rejects(route(f, { workId: 'w2', ...external({ cwd: deleted }) }), /WORKSPACE_CLAIM_CONFLICT/);
  await assert.rejects(route(f, { workId: 'w3', ...external({ cwd: 'relative/workspace' }) }), /WORKSPACE_CLAIM_UNRESOLVED/);
  await assert.rejects(route(f, { workId: 'w4', ...external({ cwd: 42 }) }), /WORKSPACE_CLAIM_UNRESOLVED/);
  assert.equal(f.starts(), 0);
  assert.equal((await route(f, { cwd: other, workId: 'w5', ...external({ cwd: deleted }) })).receipt.state, 'DISPATCHED');
  assert.equal((await route(f, { workId: 'w6', ...external({}) })).receipt.state, 'DISPATCHED');
  await assert.rejects(route(f, { cwd: other, workId: 'w7', ...external({}) }), /WORKSPACE_CLAIM_CONFLICT/);
  await assert.rejects(route(f, { workId: 'external', cwd: path.join(f.workspace, 'child'), ...external({}) }), /WORK_ID_CLAIM_CONFLICT/);
  assert.equal(f.starts(), 2);
});
test('an intent recorded under the lexical alias path still blocks replay and stays visible', async (t) => {
  const f = fixture(t); const alias = path.join(f.root, 'alias');
  const receipts = path.join(f.config.statePath, 'dispatch-receipts'); const intents = path.join(f.config.statePath, 'intents');
  fs.mkdirSync(receipts, { recursive: true, mode: 0o700 }); fs.mkdirSync(intents, { mode: 0o700 });
  const receiptPath = path.join(receipts, 'legacy.json');
  fs.writeFileSync(receiptPath, JSON.stringify({ schemaVersion: 1, attemptId: 'legacy', workId: 'first', cwd: alias, state: 'PRE_START_FAILED', nativeStartAttempted: false }), { mode: 0o600 });
  const digest = crypto.createHash('sha256').update(JSON.stringify({ workId: 'first', cwd: alias })).digest('hex');
  fs.writeFileSync(path.join(intents, `${digest}.json`), JSON.stringify({ schemaVersion: 1, receiptPath, state: 'PRE_START_FAILED' }), { mode: 0o600 });
  await assert.rejects(route(f, { cwd: alias }), /duplicate intent.*do not retry/i);
  const status = await router.inspectDispatch(f.config, { cwd: alias, workId: 'first' });
  assert.equal(status.receipt.attemptId, 'legacy');
  assert.equal(f.starts(), 0);
});
