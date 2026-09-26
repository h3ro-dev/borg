import assert from 'node:assert/strict';
import crypto from 'node:crypto';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import test from 'node:test';

import * as router from '../router/router.mjs';
import { buildDefaultConfig } from '../config.mjs';

const NOW = Date.parse('2026-09-26T12:00:00.000Z');
const account = { type: 'chatgpt', email: 'synthetic-owner', planType: 'pro' };

function fixture(t) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'borg-router-lifecycle-'));
  fs.chmodSync(root, 0o700);
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  const config = buildDefaultConfig(root, process.execPath, { nodeBin: process.execPath });
  config.statePath = path.join(root, 'private/router');
  config.conductors[0].accountPin = router.accountIdentityDigest(account);
  fs.mkdirSync(path.join(config.statePath, 'dispatch-receipts'), { recursive: true, mode: 0o700 });
  return { root, config };
}

function putReceipt(config, name, changes = {}) {
  const receipt = {
    schemaVersion: 1,
    attemptId: name,
    workId: `work-${name}`,
    cwd: path.dirname(config.statePath),
    laneId: 'primary',
    machineId: 'local',
    state: 'DISPATCHED',
    phase: 'TURN_STARTED',
    attemptedAt: '2026-09-26T10:00:00.000Z',
    dispatchedAt: '2026-09-26T10:00:01.000Z',
    threadId: `thread-${name}`,
    turnId: `turn-${name}`,
    nativeStartAttempted: true,
    events: [],
    ...changes,
  };
  const receiptPath = path.join(config.statePath, 'dispatch-receipts', `${name}.json`);
  fs.writeFileSync(receiptPath, `${JSON.stringify(receipt, null, 2)}\n`, { mode: 0o600 });
  return { receipt, receiptPath };
}

function readJson(file) {
  return JSON.parse(fs.readFileSync(file, 'utf8'));
}

function intentDigest(workId, cwd) {
  return crypto.createHash('sha256').update(JSON.stringify({ workId, cwd })).digest('hex');
}

test('reconcile writes terminal receipt states only from exact native turn readback', async (t) => {
  const { config } = fixture(t);
  const rows = [
    ['complete', 'completed', 'COMPLETED'],
    ['failure', { type: 'failed' }, 'FAILED'],
    ['cancel', 'interrupted', 'CANCELLED'],
    ['running', 'inProgress', 'DISPATCHED'],
  ];
  const files = new Map(rows.map(([name]) => [name, putReceipt(config, name).receiptPath]));
  const operations = [];
  const result = await router.reconcileReceipts(config, {
    nowMs: NOW,
    archiveAfterMs: 30 * 24 * 60 * 60 * 1000,
    conductorProvider: async (_lane, operation) => {
      operations.push(operation);
      assert.equal(operation.kind, 'rpc');
      assert.equal(operation.method, 'thread/read');
      assert.equal(operation.params.includeTurns, true);
      const name = operation.params.threadId.slice('thread-'.length);
      const status = rows.find(([candidate]) => candidate === name)[1];
      return { thread: {
        id: operation.params.threadId,
        turns: [{ id: `turn-${name}`, status, completedAt: '2026-09-26T11:00:00.000Z' }],
      } };
    },
  });

  assert.equal(result.reconciled, 3);
  assert.equal(result.archived, 0);
  assert.equal(operations.length, 4);
  for (const [name, _native, expected] of rows) {
    const receipt = readJson(files.get(name));
    assert.equal(receipt.state, expected);
    if (expected !== 'DISPATCHED') {
      assert.equal(receipt.reconciledAt, new Date(NOW).toISOString());
      assert.equal(receipt.terminalAt, '2026-09-26T11:00:00.000Z');
      assert.equal(receipt.nativeEvidence.method, 'thread/read');
      assert.equal(receipt.nativeEvidence.threadId, `thread-${name}`);
      assert.equal(receipt.nativeEvidence.turnId, `turn-${name}`);
    }
  }
});

test('reconcile keeps claims active when native evidence is missing, mismatched, or unreadable', async (t) => {
  const { config } = fixture(t);
  const missing = putReceipt(config, 'missing');
  const mismatch = putReceipt(config, 'mismatch');
  const unreadable = putReceipt(config, 'unreadable');
  const result = await router.reconcileReceipts(config, {
    nowMs: NOW,
    conductorProvider: async (_lane, operation) => {
      if (operation.params.threadId === 'thread-missing') return { thread: { id: 'thread-missing', turns: [] } };
      if (operation.params.threadId === 'thread-mismatch') {
        return { thread: { id: 'different-thread', turns: [{ id: 'turn-mismatch', status: 'completed' }] } };
      }
      throw new Error('native unavailable');
    },
  });
  assert.equal(result.reconciled, 0);
  assert.equal(result.errors.length, 3);
  for (const item of [missing, mismatch, unreadable]) {
    assert.equal(readJson(item.receiptPath).state, 'DISPATCHED');
  }
});

test('dispatch reconciles native terminal work before the first claims scan', async (t) => {
  const { root, config } = fixture(t);
  const old = putReceipt(config, 'old', { cwd: root });
  const calls = [];
  const conductorProvider = async (lane, operation) => {
    if (operation.method === 'thread/read') {
      calls.push('thread/read');
      return { thread: { id: 'thread-old', turns: [{ id: 'turn-old', status: 'completed' }] } };
    }
    if (operation.kind === 'status') {
      calls.push('status');
      return { ok: true, port: lane.port, codexHome: lane.codexHome, supportedRoles: ['leaf'], threads: {} };
    }
    if (operation.method === 'account/read') return { account };
    if (operation.method === 'account/rateLimits/read') {
      return { rateLimits: { limitId: 'codex', primary: {
        usedPercent: 5, windowDurationMins: 300, resetsAt: '2026-09-27T12:00:00.000Z',
      } } };
    }
    if (operation.kind === 'thread-start') return { threadId: 'thread-new' };
    if (operation.kind === 'turn-start') return { turnId: 'turn-new' };
    throw new Error(`unexpected operation: ${operation.kind}`);
  };
  const result = await router.dispatch(config, {
    cwd: root,
    workId: 'new-work',
    prompt: 'synthetic dispatch',
    nowMs: NOW,
    capacityProvider: async () => ({
      observedAt: new Date(NOW).toISOString(), reachable: true, loadPerCore: 0.1, memoryUsePercent: 20,
    }),
    claimsProvider: async () => {
      calls.push('claims');
      assert.equal(readJson(old.receiptPath).state, 'COMPLETED');
      return { observedAt: new Date(NOW).toISOString(), active: [] };
    },
    conductorProvider,
  });
  assert.equal(result.receipt.state, 'DISPATCHED');
  assert.equal(calls[0], 'thread/read');
  assert.ok(calls.indexOf('thread/read') < calls.indexOf('claims'));
});

test('standalone rank also reconciles before its claims scan', async (t) => {
  const { config } = fixture(t);
  const old = putReceipt(config, 'rank-old');
  const calls = [];
  await router.rank(config, {
    nowMs: NOW,
    capacityProvider: async () => ({
      observedAt: new Date(NOW).toISOString(), reachable: true, loadPerCore: 0.1, memoryUsePercent: 20,
    }),
    claimsProvider: async () => {
      calls.push('claims');
      assert.equal(readJson(old.receiptPath).state, 'COMPLETED');
      return { observedAt: new Date(NOW).toISOString(), active: [] };
    },
    conductorProvider: async (lane, operation) => {
      if (operation.method === 'thread/read') {
        calls.push('thread/read');
        return { thread: { id: 'thread-rank-old', turns: [{ id: 'turn-rank-old', status: 'completed' }] } };
      }
      if (operation.kind === 'status') {
        return { ok: true, port: lane.port, codexHome: lane.codexHome, supportedRoles: ['leaf'], threads: {} };
      }
      if (operation.method === 'account/read') return { account };
      if (operation.method === 'account/rateLimits/read') {
        return { rateLimits: { limitId: 'codex', primary: {
          usedPercent: 5, windowDurationMins: 300, resetsAt: '2026-09-27T12:00:00.000Z',
        } } };
      }
      throw new Error(`unexpected operation: ${operation.kind}`);
    },
  });
  assert.equal(calls[0], 'thread/read');
  assert.ok(calls.indexOf('thread/read') < calls.indexOf('claims'));
});

test('aged terminal receipts move to archive and their durable intent follows them', async (t) => {
  const { config } = fixture(t);
  const { receipt, receiptPath } = putReceipt(config, 'aged', {
    state: 'COMPLETED',
    terminalAt: '2026-09-01T00:00:00.000Z',
    nativeEvidence: { method: 'thread/read', threadId: 'thread-aged', turnId: 'turn-aged' },
  });
  const intents = path.join(config.statePath, 'intents');
  fs.mkdirSync(intents, { recursive: true, mode: 0o700 });
  const intentPath = path.join(intents, `${intentDigest(receipt.workId, receipt.cwd)}.json`);
  fs.writeFileSync(intentPath, `${JSON.stringify({ schemaVersion: 1, receiptPath, state: receipt.state })}\n`, { mode: 0o600 });

  const result = await router.reconcileReceipts(config, {
    nowMs: NOW,
    archiveAfterMs: 24 * 60 * 60 * 1000,
    conductorProvider: async () => { throw new Error('terminal receipts need no native reread'); },
  });
  const archived = path.join(config.statePath, 'dispatch-receipts', 'archive', 'aged.json');
  assert.equal(result.archived, 1);
  assert.equal(fs.existsSync(receiptPath), false);
  assert.equal(fs.existsSync(archived), true);
  assert.equal(readJson(intentPath).receiptPath, archived);
  const inspected = await router.inspectDispatch(config, { cwd: receipt.cwd, workId: receipt.workId });
  assert.equal(inspected.receipt.state, 'COMPLETED');
  assert.equal(inspected.completionVerified, true);
});

test('dispatch lock preserves live and young empty locks and recovers only stale owners', async (t) => {
  const { config } = fixture(t);
  const lockPath = path.join(config.statePath, 'dispatch.lock');
  fs.mkdirSync(config.statePath, { recursive: true, mode: 0o700 });
  fs.writeFileSync(lockPath, `${process.pid}\n`, { mode: 0o600 });
  const liveOld = new Date(NOW - 11 * 60 * 1000);
  fs.utimesSync(lockPath, liveOld, liveOld);
  await assert.rejects(router.acquireDispatchLock(config.statePath, 20, {
    nowMs: NOW, processAlive: () => true,
  }), /dispatch lock busy/);
  assert.equal(fs.readFileSync(lockPath, 'utf8'), `${process.pid}\n`);
  assert.deepEqual(fs.readdirSync(config.statePath).filter((name) => name.startsWith('dispatch.lock.stale-')), []);

  fs.writeFileSync(lockPath, '', { mode: 0o600 });
  fs.utimesSync(lockPath, new Date(NOW), new Date(NOW));
  await assert.rejects(router.acquireDispatchLock(config.statePath, 20, {
    nowMs: NOW, processAlive: () => false,
  }), /dispatch lock busy/);
  assert.equal(fs.readFileSync(lockPath, 'utf8'), '');
  assert.deepEqual(fs.readdirSync(config.statePath).filter((name) => name.startsWith('dispatch.lock.stale-')), []);

  for (const [name, body, aged, processAlive] of [
    ['dead', '999999999\n', false, () => false],
    ['aged', 'not-a-pid\n', true, () => false],
    ['zero', '', true, () => false],
  ]) {
    fs.rmSync(lockPath, { force: true });
    fs.writeFileSync(lockPath, body, { mode: 0o600 });
    if (aged) {
      const old = new Date(NOW - 11 * 60 * 1000);
      fs.utimesSync(lockPath, old, old);
    }
    const release = await router.acquireDispatchLock(config.statePath, 50, {
      nowMs: NOW, processAlive,
    });
    const stale = fs.readdirSync(config.statePath)
      .filter((entry) => entry.startsWith('dispatch.lock.stale-'));
    assert.ok(stale.length >= 1, `${name} lock must be retained under a stale name`);
    assert.equal(fs.existsSync(lockPath), true);
    await release();
    assert.equal(fs.existsSync(lockPath), false);
  }
});

test('overlapping lock acquirers cannot both win while the first lock body is empty', async (t) => {
  const { config } = fixture(t);
  const lockPath = path.join(config.statePath, 'dispatch.lock');
  fs.mkdirSync(config.statePath, { recursive: true, mode: 0o700 });
  const originalOpen = fs.promises.open;
  let intercepted = false;
  let announceOpen;
  let resumeWrite;
  const opened = new Promise((resolve) => { announceOpen = resolve; });
  const resume = new Promise((resolve) => { resumeWrite = resolve; });
  fs.promises.open = async (...args) => {
    const handle = await originalOpen(...args);
    if (!intercepted && args[0] === lockPath && args[1] === 'wx') {
      intercepted = true;
      const originalWrite = handle.writeFile.bind(handle);
      handle.writeFile = async (...writeArgs) => {
        announceOpen();
        await resume;
        return originalWrite(...writeArgs);
      };
    }
    return handle;
  };
  t.after(() => {
    fs.promises.open = originalOpen;
    resumeWrite();
  });

  const settle = (promise) => promise.then(
    (release) => ({ acquired: true, release }),
    (error) => ({ acquired: false, error }),
  );
  const first = settle(router.acquireDispatchLock(config.statePath, 250, { nowMs: NOW }));
  await opened;
  fs.utimesSync(lockPath, new Date(NOW), new Date(NOW));
  const second = settle(router.acquireDispatchLock(config.statePath, 100, { nowMs: NOW }));
  await new Promise((resolve) => setTimeout(resolve, 25));
  resumeWrite();
  const outcomes = await Promise.all([first, second]);
  try {
    assert.equal(outcomes.filter((outcome) => outcome.acquired).length, 1);
    assert.match(outcomes.find((outcome) => !outcome.acquired).error.message, /dispatch lock busy/);
  } finally {
    await Promise.allSettled(outcomes.filter((outcome) => outcome.acquired)
      .map((outcome) => outcome.release()));
  }
});

test('dispatch lock recovers a recycled live pid with a different process start time', async (t) => {
  const { config } = fixture(t);
  const lockPath = path.join(config.statePath, 'dispatch.lock');
  fs.mkdirSync(config.statePath, { recursive: true, mode: 0o700 });
  fs.writeFileSync(lockPath, `${JSON.stringify({
    schemaVersion: 1,
    lockId: 'previous-process',
    pid: process.pid,
    processStartTime: 'old-process-start',
    acquiredAt: new Date(NOW).toISOString(),
  })}\n`, { mode: 0o600 });
  let release;
  await assert.doesNotReject(async () => {
    release = await router.acquireDispatchLock(config.statePath, 100, {
      nowMs: NOW,
      processAlive: () => true,
      processStartTime: async () => 'current-process-start',
    });
  });
  const stale = fs.readdirSync(config.statePath)
    .find((entry) => entry.startsWith('dispatch.lock.stale-'));
  assert.ok(stale);
  assert.equal(readJson(path.join(config.statePath, stale)).lockId, 'previous-process');
  assert.equal(readJson(lockPath).processStartTime, 'current-process-start');
  await release();
});

test('stale recovery never retires a replacement lock inode', async (t) => {
  const { config } = fixture(t);
  const lockPath = path.join(config.statePath, 'dispatch.lock');
  fs.mkdirSync(config.statePath, { recursive: true, mode: 0o700 });
  fs.writeFileSync(lockPath, `${JSON.stringify({ pid: 999999999, processStartTime: 'dead-start' })}\n`, {
    mode: 0o600,
  });
  const originalLink = fs.promises.link;
  let swapped = false;
  fs.promises.link = async (source, destination) => {
    if (!swapped && source === lockPath) {
      swapped = true;
      await fs.promises.rename(lockPath, `${lockPath}.before-swap`);
      await fs.promises.writeFile(lockPath, `${JSON.stringify({
        schemaVersion: 1,
        lockId: 'replacement',
        pid: process.pid,
        processStartTime: 'current-process-start',
      })}\n`, { mode: 0o600, flag: 'wx' });
    }
    return originalLink(source, destination);
  };
  t.after(() => { fs.promises.link = originalLink; });

  await assert.rejects(router.acquireDispatchLock(config.statePath, 70, {
    nowMs: NOW,
    processAlive: (pid) => pid === process.pid,
    processStartTime: async () => 'current-process-start',
  }), /dispatch lock busy/);
  assert.equal(readJson(lockPath).lockId, 'replacement');
  assert.deepEqual(fs.readdirSync(config.statePath).filter((entry) => (
    entry.startsWith('dispatch.lock.stale-')
  )), []);
});

test('native conductor calls send each lane profile token and tolerate a missing token', async (t) => {
  const { config } = fixture(t);
  const lane = config.conductors[0];
  const token = 'b'.repeat(64);
  const tokenDirectory = path.join(lane.codexHome, '.conductor');
  fs.mkdirSync(tokenDirectory, { recursive: true, mode: 0o700 });
  fs.writeFileSync(path.join(tokenDirectory, 'http-token'), `${token}\n`, { mode: 0o600 });
  const calls = [];
  const fetchImpl = async (url, init) => {
    calls.push({ url, init });
    return { ok: true, async json() { return url.includes('/threads') ? { data: [] } : { ok: true }; } };
  };
  await router.nativeConductorProvider(lane, { kind: 'status' }, 5000, fetchImpl);
  await router.nativeConductorProvider(lane, {
    kind: 'rpc', method: 'thread/read', params: { threadId: 'thread-1', includeTurns: true },
  }, 5000, fetchImpl);
  assert.equal(calls.length, 3);
  assert.ok(calls.every((call) => call.init.headers.authorization === `Bearer ${token}`));

  fs.unlinkSync(path.join(tokenDirectory, 'http-token'));
  calls.length = 0;
  await router.nativeConductorProvider(lane, { kind: 'status' }, 5000, fetchImpl);
  assert.ok(calls.every((call) => !call.init.headers?.authorization));
});
