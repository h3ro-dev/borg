import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { describe, test } from 'node:test';
import * as router from '../router/router.mjs';
import { buildDefaultConfig } from '../config.mjs';

// Independent regressions for review findings R1 (command collector hides the
// router's own durable claims) and R2 (workspace identity is lexical only).
// Every case goes through public dispatch()/inspectDispatch() with the DEFAULT
// claims provider, so a fix may live in observeClaims() or in dispatch().
// Capacity and the native conductor are synthetic; nothing is launched.
// A fix may either canonicalize workspaces or reject noncanonical spellings:
// both are accepted as long as no second native thread/turn request is made.

const account = { type: 'chatgpt', email: 'owner@example.test', planType: 'pro' };
const LIFECYCLE = new Set(['thread-start', 'turn-start']);
const WORKSPACE_REFUSAL = /WORKSPACE|CONFLICT|OVERLAP|ALIAS|SYMLINK|CANONICAL/i;
const UNRESOLVED_REFUSAL = /WORKSPACE|CONFLICT|OVERLAP|ALIAS|SYMLINK|CANONICAL|CLAIM|IDENTITY|UNRESOLV/i;
const WORK_ID_REFUSAL = /WORK_ID|CONFLICT|duplicate intent/i;
const UNREADABLE_CLAIMS = /RECEIPT_[A-Z_]+|UNSAFE_RECEIPT|CLAIMS_[A-Z_]+|LEDGER/;
// Fixed external collector: prints the claims it is given, observed now.
const COLLECTOR = 'process.stdout.write(JSON.stringify({ schemaVersion: 1, observedAt: new Date().toISOString(), '
  + 'source: "synthetic-external", active: JSON.parse(process.argv[1]) }))';
const TMP_REAL = fs.realpathSync.native(os.tmpdir());
const CASE_INSENSITIVE = (() => {
  const probe = fs.mkdtempSync(path.join(TMP_REAL, 'borg-case-'));
  try { fs.mkdirSync(path.join(probe, 'Probe')); return fs.existsSync(path.join(probe, 'PROBE')); }
  finally { fs.rmSync(probe, { recursive: true, force: true }); }
})();

function fixture(t, { collector = 'router-state' } = {}) {
  // Canonical root, so that only the aliases each case creates are noncanonical.
  const root = fs.realpathSync.native(fs.mkdtempSync(path.join(os.tmpdir(), 'borg-collision-')));
  fs.chmodSync(root, 0o700);
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  const config = buildDefaultConfig(root, process.execPath, { nodeBin: process.execPath });
  config.conductors[0].accountPin = router.accountIdentityDigest(account);
  const external = (active) => {
    config.machines[0].claims = { kind: 'command', command: process.execPath,
      args: ['-e', COLLECTOR, JSON.stringify(active)], maxAgeMs: 15000 };
  };
  if (collector === 'command') external([]);
  const calls = [];
  let serial = 0;
  const native = (mode) => async (lane, op) => {
    calls.push(op.kind);
    if (op.kind === 'status') return { ok: true, port: lane.port, codexHome: lane.codexHome, supportedRoles: ['leaf', 'lead'], threads: {} };
    if (op.method === 'account/read') return { account };
    if (op.method === 'account/rateLimits/read') return { rateLimits: { limitId: 'codex',
      primary: { usedPercent: 5, windowDurationMins: 300, resetsAt: new Date(Date.now() + 86_400_000).toISOString() } } };
    if (op.kind === 'thread-start') {
      if (mode === 'thread-error') throw new Error('synthetic lost thread response');
      return mode === 'thread-no-id' ? {} : { threadId: `thread-${++serial}` };
    }
    if (op.kind === 'turn-start') {
      if (mode === 'turn-error') throw new Error('synthetic lost turn response');
      return mode === 'turn-no-id' ? {} : { turnId: `turn-${serial}` };
    }
    throw new Error('unexpected operation');
  };
  const tree = (...parts) => path.join(root, 'tree', ...parts);
  const dir = (...parts) => { fs.mkdirSync(tree(...parts), { recursive: true }); return tree(...parts); };
  const link = (target, ...parts) => {
    fs.mkdirSync(path.dirname(tree(...parts)), { recursive: true });
    fs.symlinkSync(target, tree(...parts));
    return tree(...parts);
  };
  const send = (cwd, workId, mode = 'ok', extra = {}) => router.dispatch(config, {
    cwd, workId, prompt: 'Synthetic collision review only', stageTimeoutMs: 10000,
    capacityProvider: async () => ({ observedAt: new Date().toISOString(), reachable: true, loadPerCore: 0.2, memoryUsePercent: 35 }),
    conductorProvider: native(mode), ...extra,
  });
  return { root, config, calls, tree, dir, link, external, send,
    lifecycle: () => calls.filter((kind) => LIFECYCLE.has(kind)).length,
    threadStarts: () => calls.filter((kind) => kind === 'thread-start').length };
}

const DISPATCHED = { name: 'DISPATCHED', mode: 'ok', state: 'DISPATCHED' };
const PRIORS = [
  DISPATCHED,
  { name: 'UNKNOWN_DO_NOT_RETRY (lost thread response)', mode: 'thread-error', state: 'UNKNOWN_DO_NOT_RETRY' },
  { name: 'UNKNOWN_DO_NOT_RETRY (thread response without ID)', mode: 'thread-no-id', state: 'UNKNOWN_DO_NOT_RETRY' },
  { name: 'STARTED_TURN_UNKNOWN (lost turn response)', mode: 'turn-error', state: 'STARTED_TURN_UNKNOWN' },
  { name: 'STARTED_TURN_UNKNOWN (turn response without ID)', mode: 'turn-no-id', state: 'STARTED_TURN_UNKNOWN' },
  // Crash fixtures: the last state the router persisted before the process died.
  { name: 'THREAD_STARTED (router died before turn start)', mode: 'turn-error', state: 'THREAD_STARTED',
    crash: { state: 'THREAD_STARTED', phase: 'TURN_START_PENDING', errorClass: null } },
  { name: 'ATTEMPTING (router died during thread start)', mode: 'thread-error', state: 'ATTEMPTING',
    crash: { state: 'ATTEMPTING', phase: 'THREAD_START_PENDING', errorClass: null } },
];

// Leave one owned claim in `prior.state` through a real public dispatch.
async function claim(f, cwd, workId, prior = DISPATCHED, { aliasInput = false } = {}) {
  const before = f.lifecycle();
  let refusal = null;
  await f.send(cwd, workId, prior.mode).catch((error) => { refusal = error; });
  if (aliasInput && f.lifecycle() === before) {
    // Rejecting a noncanonical spelling before any native request is safe.
    assert.match(String(refusal), WORKSPACE_REFUSAL);
    return null;
  }
  assert.equal(f.threadStarts(), before + 1, `fixture claim did not reach native start: ${refusal}`);
  let status = await router.inspectDispatch(f.config, { cwd, workId });
  assert.equal(status.found, true, 'status lookup must find the claim through the spelling used to create it');
  if (prior.crash) {
    fs.writeFileSync(status.receiptPath, JSON.stringify({ ...status.receipt, ...prior.crash }), { mode: 0o600 });
    status = await router.inspectDispatch(f.config, { cwd, workId });
  }
  assert.equal(status.receipt.state, prior.state);
  return status;
}

async function refused(f, cwd, workId, pattern = WORKSPACE_REFUSAL) {
  const before = f.lifecycle();
  await assert.rejects(f.send(cwd, workId), pattern,
    `work ${workId} was dispatched into ${path.relative(f.root, cwd)} despite an overlapping active claim`);
  assert.equal(f.lifecycle(), before, 'refusal must precede every native thread/turn request');
}

async function admitted(f, cwd, workId) {
  const before = f.threadStarts();
  const result = await f.send(cwd, workId);
  assert.equal(result.receipt.state, 'DISPATCHED');
  assert.equal(f.threadStarts(), before + 1);
}

function ledger(f) {
  const directory = path.join(f.config.statePath, 'dispatch-receipts');
  fs.mkdirSync(directory, { recursive: true, mode: 0o700 });
  return directory;
}

function putReceipt(f, name, body, mode = 0o600) {
  const target = path.join(ledger(f), name);
  fs.writeFileSync(target, typeof body === 'string' ? body : JSON.stringify(body), { mode });
  fs.chmodSync(target, mode);
  return target;
}

// Each case owns a private temporary tree, so cases can run concurrently.
describe('router collision review (R1/R2)', { concurrency: 4 }, () => {

  // --- R1: every active local receipt state is a claim, whatever the collector.

  for (const collector of ['router-state', 'command']) {
    for (const prior of PRIORS) {
      test(`[${collector}] ${prior.name} claim blocks a different work ID in the same workspace`, async (t) => {
        const f = fixture(t, { collector });
        const ws = f.dir('ws');
        await claim(f, ws, 'first', prior);
        await refused(f, ws, 'second');
      });
    }
    for (const prior of [DISPATCHED, PRIORS[1]]) {
      test(`[${collector}] ${prior.name} claim reserves its work ID in another workspace`, async (t) => {
        const f = fixture(t, { collector });
        await claim(f, f.dir('wt', 'a'), 'shared', prior);
        await refused(f, f.dir('wt', 'b'), 'shared', WORK_ID_REFUSAL);
      });
    }
  }

  test('[command] an empty external view still admits the first dispatch exactly once', async (t) => {
    const f = fixture(t, { collector: 'command' });
    await admitted(f, f.dir('ws'), 'only');
    assert.equal(f.lifecycle(), 2);
  });

  test('[command] a failing external collector refuses before native start', async (t) => {
    const f = fixture(t, { collector: 'command' });
    f.config.machines[0].claims.args = ['-e', 'process.exit(3)'];
    await refused(f, f.dir('ws'), 'first', UNREADABLE_CLAIMS);
  });

  // --- Corrupt local ledger: an unreadable ledger is never an empty claim list.

  const CORRUPT = [
    ['malformed JSON', (f, ws) => putReceipt(f, 'broken.json', `{"state":"DISPATCHED","workId":"owner","cwd":${JSON.stringify(ws)}`)],
    ['a non-object JSON record', (f) => putReceipt(f, 'array.json', '[]')],
    ['a group/world-readable receipt', (f, ws) => putReceipt(f, 'shared.json', { state: 'DISPATCHED', workId: 'owner', cwd: ws, attemptId: 'a1' }, 0o644)],
    ['a symlinked receipt entry', (f, ws) => {
      const outside = path.join(f.root, 'outside.json');
      fs.writeFileSync(outside, JSON.stringify({ state: 'DISPATCHED', workId: 'owner', cwd: ws, attemptId: 'a1' }), { mode: 0o600 });
      fs.symlinkSync(outside, path.join(ledger(f), 'redirect.json'));
    }],
    ['an unknown receipt state', (f, ws) => putReceipt(f, 'future.json', { state: 'RECONCILING', workId: 'owner', cwd: ws, attemptId: 'a1' })],
    ['an active receipt without a work ID', (f, ws) => putReceipt(f, 'anonymous.json', { state: 'UNKNOWN_DO_NOT_RETRY', cwd: ws, attemptId: 'a1' })],
    ['an oversize receipt', (f, ws) => putReceipt(f, 'huge.json', { state: 'DISPATCHED', workId: 'owner', cwd: ws, attemptId: 'a1', pad: 'x'.repeat(70000) })],
  ];
  for (const collector of ['router-state', 'command']) {
    for (const [name, corrupt] of CORRUPT) {
      test(`[${collector}] local ledger with ${name} refuses before native start`, async (t) => {
        const f = fixture(t, { collector });
        const ws = f.dir('ws');
        corrupt(f, ws);
        await refused(f, ws, 'second', UNREADABLE_CLAIMS);
      });
    }
  }

  // --- R2: filesystem identity, alias and ancestor/descendant overlap.

  const OVERLAPS = [
    ['a directory symlink alias of the claimed workspace', (f) => { const ws = f.dir('ws'); return [ws, f.link(ws, 'via-link')]; }],
    ['the real path after a claim made through its symlink alias', (f) => { const ws = f.dir('ws'); return [f.link(ws, 'via-link'), ws, true]; }],
    ['a child of the claimed workspace', (f) => [f.dir('ws'), f.dir('ws', 'child')]],
    ['the parent of a claimed child workspace', (f) => [f.dir('ws', 'child'), f.dir('ws')]],
    ['a deep descendant of the claimed workspace', (f) => [f.dir('ws'), f.dir('ws', 'a', 'b', 'c')]],
    ['a distant ancestor of a claimed deep workspace', (f) => [f.dir('ws', 'a', 'b', 'c'), f.dir('ws')]],
    ['a child reached through a symlinked parent', (f) => {
      const ws = f.dir('ws'); f.dir('ws', 'child');
      return [ws, path.join(f.link(ws, 'via-link'), 'child')];
    }],
    ['a parent reached through a symlink after its child was claimed', (f) => {
      const ws = f.dir('ws');
      return [f.dir('ws', 'child'), f.link(ws, 'via-link')];
    }],
    ['an unrelated-looking symlink that resolves inside the claimed tree', (f) => [f.dir('ws'), f.link(f.dir('ws', 'child'), 'elsewhere', 'into')]],
    ['the owning parent after a child was claimed through an outside symlink', (f) => [f.link(f.dir('ws', 'child'), 'elsewhere', 'into'), f.dir('ws'), true]],
  ];
  const RELATION = Object.fromEntries(OVERLAPS);
  const CORE = ['a directory symlink alias of the claimed workspace', 'a child of the claimed workspace', 'the parent of a claimed child workspace'];

  for (const [name, build] of OVERLAPS) {
    test(`[router-state] DISPATCHED claim blocks ${name}`, async (t) => {
      const f = fixture(t);
      const [first, second, aliasInput] = build(f);
      if (await claim(f, first, 'first', DISPATCHED, { aliasInput }) === null) return;
      await refused(f, second, 'second');
    });
  }
  for (const prior of PRIORS.filter((row) => row.mode.endsWith('-error'))) {
    for (const name of CORE) {
      test(`[router-state] ${prior.name} claim blocks ${name}`, async (t) => {
        const f = fixture(t);
        const [first, second] = RELATION[name](f);
        await claim(f, first, 'first', prior);
        await refused(f, second, 'second');
      });
    }
  }
  for (const name of CORE) {
    test(`[command] DISPATCHED local claim blocks ${name}`, async (t) => {
      const f = fixture(t, { collector: 'command' });
      const [first, second] = RELATION[name](f);
      await claim(f, first, 'first');
      await refused(f, second, 'second');
    });
  }

  test('[router-state] a system-level ancestor symlink (tmpdir spelling) cannot re-enter a claimed workspace',
    { skip: os.tmpdir() === TMP_REAL && 'tmpdir is already canonical here' }, async (t) => {
      const f = fixture(t);
      const ws = f.dir('ws');
      await claim(f, ws, 'first');
      const spelled = path.join(os.tmpdir(), path.relative(TMP_REAL, ws));
      assert.notEqual(spelled, ws);
      await refused(f, spelled, 'second');
    });

  test('[router-state] a case-variant spelling on a case-insensitive volume cannot re-enter a claimed workspace',
    { skip: !CASE_INSENSITIVE && 'volume is case-sensitive' }, async (t) => {
      const f = fixture(t);
      const ws = f.dir('ws');
      await claim(f, ws, 'first');
      await refused(f, f.tree('WS'), 'second');
    });

  test('[router-state] a claimed child stays owned after its directory disappears', async (t) => {
    const f = fixture(t);
    const child = f.dir('ws', 'child');
    await claim(f, child, 'first');
    fs.rmSync(child, { recursive: true });
    await refused(f, f.tree('ws'), 'second', UNRESOLVED_REFUSAL);
  });

  test('[router-state] a claim made through a symlink stays owned after the symlink is removed', async (t) => {
    const f = fixture(t);
    const ws = f.dir('ws');
    const alias = f.link(ws, 'via-link');
    if (await claim(f, alias, 'first', DISPATCHED, { aliasInput: true }) === null) return;
    fs.rmSync(alias);
    await refused(f, ws, 'second', UNRESOLVED_REFUSAL);
  });

  for (const [name, build] of [
    ['parent and child', (f) => [f.dir('ws'), f.dir('ws', 'child')]],
    ['workspace and its symlink alias', (f) => { const ws = f.dir('ws'); return [ws, f.link(ws, 'via-link')]; }],
  ]) {
    test(`[router-state] concurrent dispatches into ${name} start exactly one native thread`, async (t) => {
      const f = fixture(t);
      const [one, two] = build(f);
      const results = await Promise.allSettled([f.send(one, 'first'), f.send(two, 'second')]);
      assert.equal(results.filter((result) => result.status === 'fulfilled').length, 1);
      assert.match(String(results.find((result) => result.status === 'rejected').reason), WORKSPACE_REFUSAL);
      assert.equal(f.threadStarts(), 1);
    });
  }

  test('[router-state] status lookup through a symlink alias finds the same attempt or refuses the alias', async (t) => {
    const f = fixture(t);
    const ws = f.dir('ws');
    const sent = await claim(f, ws, 'first');
    const alias = f.link(ws, 'via-link');
    let status;
    try { status = await router.inspectDispatch(f.config, { cwd: alias, workId: 'first' }); }
    catch (error) { assert.match(String(error), WORKSPACE_REFUSAL); return; }
    assert.equal(status.found, true, 'an alias of an owned workspace must not read as NOT_FOUND');
    assert.equal(status.receipt.attemptId, sent.receipt.attemptId);
  });

  // --- External collector claims use the same identity rules.

  const externalClaim = (cwd) => [{ workId: 'external-owner', attemptId: 'external-attempt', cwd, state: 'DISPATCHED' }];
  for (const [name, build, pattern] of [
    ['the exact workspace', (f) => { const ws = f.dir('ws'); return [ws, ws]; }],
    ['a symlink alias of the workspace', (f) => { const ws = f.dir('ws'); return [f.link(ws, 'via-link'), ws]; }],
    ['a parent of the workspace', (f) => [f.dir('ws'), f.dir('ws', 'child')]],
    ['a child of the workspace', (f) => [f.dir('ws', 'child'), f.dir('ws')]],
    ['a missing child of the workspace', (f) => { const ws = f.dir('ws'); return [path.join(ws, 'gone'), ws]; }, UNRESOLVED_REFUSAL],
  ]) {
    test(`[command] same-machine external claim on ${name} refuses before native start`, async (t) => {
      const f = fixture(t, { collector: 'command' });
      const [claimed, target] = build(f);
      f.external(externalClaim(claimed));
      await refused(f, target, 'mine', pattern);
    });
  }

  // --- Controls: a fix must not conflate siblings, prefixes or terminal receipts.

  const SIBLINGS = [
    ['sibling worktrees', (f) => [[f.dir('wt', 'a'), 'first'], [f.dir('wt', 'b'), 'second']]],
    ['string-prefix siblings (repo then repo-other)', (f) => [[f.dir('repo'), 'first'], [f.dir('repo-other'), 'second']]],
    ['string-prefix siblings (repo-other then repo)', (f) => [[f.dir('repo-other'), 'first'], [f.dir('repo'), 'second']]],
    ['sibling children of an unclaimed parent', (f) => [[f.dir('ws', 'child-a'), 'first'], [f.dir('ws', 'child-b'), 'second']]],
  ];
  for (const collector of ['router-state', 'command']) {
    for (const [name, build] of SIBLINGS) {
      test(`[${collector}] control: ${name} are independently admissible`, async (t) => {
        const f = fixture(t, { collector });
        const [[one, idOne], [two, idTwo]] = build(f);
        await admitted(f, one, idOne);
        await admitted(f, two, idTwo);
      });
    }
    test(`[${collector}] control: a PRE_START_FAILED receipt is not an active workspace claim`, async (t) => {
      const f = fixture(t, { collector });
      const ws = f.dir('ws');
      await assert.rejects(f.send(ws, 'failed', 'ok', { capacityProvider: async () => null }), /CAPACITY_UNKNOWN/);
      assert.equal((await router.inspectDispatch(f.config, { cwd: ws, workId: 'failed' })).receipt.state, 'PRE_START_FAILED');
      await admitted(f, f.dir('ws', 'child'), 'child');
    });
  }
  test('[command] control: an external claim on a string-prefix sibling does not block', async (t) => {
    const f = fixture(t, { collector: 'command' });
    f.external(externalClaim(f.dir('repo')));
    await admitted(f, f.dir('repo-other'), 'mine');
  });
  test('[command] control: an external claim on a sibling worktree does not block', async (t) => {
    const f = fixture(t, { collector: 'command' });
    f.external(externalClaim(f.dir('wt', 'a')));
    await admitted(f, f.dir('wt', 'b'), 'mine');
  });
});
