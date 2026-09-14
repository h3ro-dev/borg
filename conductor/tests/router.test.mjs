import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import test from 'node:test';

import {
  accountIdentityDigest,
  dispatch,
  evaluateMachineAdmission,
  rankCandidates,
} from '../router/router.mjs';
import { buildDefaultConfig } from '../config.mjs';

const NOW = Date.parse('2026-09-14T18:00:00.000Z');

function healthyObservation(overrides = {}) {
  return {
    schemaVersion: 1,
    observedAt: new Date(NOW).toISOString(),
    source: 'local-os',
    reachable: true,
    logicalCores: 8,
    loadPerCore: 0.5,
    memoryUsePercent: 40,
    ...overrides,
  };
}

function freshClaims(overrides = {}) {
  return {
    schemaVersion: 1,
    observedAt: new Date(NOW).toISOString(),
    source: 'router-state',
    active: [],
    ...overrides,
  };
}

test('physical admission fails closed for unknown, stale, hot, memory-saturated, and unknown claims', () => {
  const machine = {
    id: 'local',
    capacity: { maxAgeMs: 15_000, loadPerCoreLimit: 1.5, memoryUseLimitPercent: 92 },
    claims: { maxAgeMs: 15_000 },
  };
  for (const [observation, claims, issue] of [
    [null, freshClaims(), 'CAPACITY_UNKNOWN'],
    [healthyObservation({ observedAt: new Date(NOW - 15_001).toISOString() }), freshClaims(), 'CAPACITY_STALE'],
    [healthyObservation({ loadPerCore: 1.5 }), freshClaims(), 'CAPACITY_HOT'],
    [healthyObservation({ memoryUsePercent: 92 }), freshClaims(), 'MEMORY_SATURATED'],
    [healthyObservation(), null, 'CLAIMS_UNKNOWN'],
    [healthyObservation(), freshClaims({ observedAt: new Date(NOW - 15_001).toISOString() }), 'CLAIMS_STALE'],
  ]) {
    const result = evaluateMachineAdmission(machine, observation, claims, NOW);
    assert.equal(result.eligible, false);
    assert.ok(result.issues.includes(issue), `${issue}: ${JSON.stringify(result)}`);
  }
  assert.deepEqual(evaluateMachineAdmission(machine, healthyObservation(), freshClaims(), NOW).issues, []);
});

test('native allowance is primary and reset time is only the tie break', () => {
  const ranked = rankCandidates([
    { laneId: 'busy-high', eligible: true, remainingPercent: 91, resetAt: '2026-09-16T00:00:00Z', activeTurns: 4 },
    { laneId: 'idle-lower', eligible: true, remainingPercent: 90, resetAt: '2026-09-15T00:00:00Z', activeTurns: 0 },
    { laneId: 'tie-later', eligible: true, remainingPercent: 91, resetAt: '2026-09-17T00:00:00Z', activeTurns: 0 },
  ]);
  assert.deepEqual(ranked.map((lane) => lane.laneId), ['busy-high', 'tie-later', 'idle-lower']);
});

test('wrong pinned account refuses before lifecycle transport', async (t) => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'borg-route-wrong-account-'));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  const config = buildDefaultConfig(root, process.execPath, { nodeBin: process.execPath });
  config.statePath = path.join(root, 'private/router');
  config.conductors[0].accountPin = accountIdentityDigest({
    type: 'chatgpt', email: 'expected@example.test', planType: 'pro',
  });
  const calls = [];

  await assert.rejects(dispatch(config, {
    cwd: root,
    prompt: 'bounded test',
    workId: 'wrong-account',
    nowMs: NOW,
    capacityProvider: async () => healthyObservation(),
    claimsProvider: async () => freshClaims(),
    conductorProvider: async (_lane, operation) => {
      calls.push(operation);
      if (operation.kind === 'status') return { ok: true, port: 4747, codexHome: path.join(root, 'conductors/primary/profile'), supportedRoles: ['leaf', 'lead'], threads: {} };
      if (operation.kind === 'rpc' && operation.method === 'account/read') {
        return { account: { type: 'chatgpt', email: 'wrong@example.test', planType: 'pro' } };
      }
      throw new Error('unexpected lifecycle transport');
    },
  }), /no eligible conductor.*ACCOUNT_MISMATCH/);
  assert.equal(calls.some((call) => call.kind === 'thread-start' || call.kind === 'turn-start'), false);
});

test('provider exhaustion is distinct and does not create a discretionary reservation', async (t) => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'borg-route-exhausted-'));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  const config = buildDefaultConfig(root, process.execPath, { nodeBin: process.execPath });
  config.statePath = path.join(root, 'private/router');
  const account = { type: 'chatgpt', email: 'owner@example.test', planType: 'pro' };
  config.conductors[0].accountPin = accountIdentityDigest(account);
  const calls = [];

  await assert.rejects(dispatch(config, {
    cwd: root,
    prompt: 'bounded test',
    workId: 'exhausted',
    nowMs: NOW,
    capacityProvider: async () => healthyObservation(),
    claimsProvider: async () => freshClaims(),
    conductorProvider: async (_lane, operation) => {
      calls.push(operation);
      if (operation.kind === 'status') return { ok: true, port: 4747, codexHome: path.join(root, 'conductors/primary/profile'), supportedRoles: ['leaf', 'lead'], threads: {} };
      if (operation.method === 'account/read') return { account };
      if (operation.method === 'account/rateLimits/read') return {
        rateLimits: {
          limitId: 'codex',
          primary: { usedPercent: 100, windowDurationMins: 300, resetsAt: '2026-09-14T20:00:00Z' },
        },
      };
      throw new Error('unexpected lifecycle transport');
    },
  }), /no eligible conductor.*PROVIDER_EXHAUSTED/);
  assert.equal(calls.some((call) => call.kind === 'thread-start' || call.kind === 'turn-start'), false);
  assert.equal(fs.existsSync(path.join(config.statePath, 'reservations.json')), false);
});

test('unknown machine capacity refuses dispatch before native lifecycle calls', async (t) => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'borg-route-unknown-capacity-'));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  const config = buildDefaultConfig(root, process.execPath, { nodeBin: process.execPath });
  config.statePath = path.join(root, 'private/router');
  const account = { type: 'chatgpt', email: 'owner@example.test', planType: 'pro' };
  config.conductors[0].accountPin = accountIdentityDigest(account);
  const calls = [];

  await assert.rejects(dispatch(config, {
    cwd: root,
    prompt: 'bounded test',
    workId: 'unknown-capacity',
    nowMs: NOW,
    capacityProvider: async () => null,
    claimsProvider: async () => freshClaims(),
    conductorProvider: async (_lane, operation) => {
      calls.push(operation);
      if (operation.kind === 'status') return { ok: true, port: 4747, codexHome: path.join(root, 'conductors/primary/profile'), supportedRoles: ['leaf', 'lead'], threads: {} };
      if (operation.method === 'account/read') return { account };
      throw new Error('unexpected lifecycle transport');
    },
  }), /no eligible conductor.*CAPACITY_UNKNOWN/);
  assert.equal(calls.some((call) => call.kind === 'thread-start' || call.kind === 'turn-start'), false);
});

test('stale native usage refuses dispatch before native lifecycle calls', async (t) => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'borg-route-stale-usage-'));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  const config = buildDefaultConfig(root, process.execPath, { nodeBin: process.execPath });
  config.statePath = path.join(root, 'private/router');
  const account = { type: 'chatgpt', email: 'owner@example.test', planType: 'pro' };
  config.conductors[0].accountPin = accountIdentityDigest(account);
  const calls = [];

  await assert.rejects(dispatch(config, {
    cwd: root,
    prompt: 'bounded test',
    workId: 'stale-usage',
    nowMs: NOW,
    usageMaxAgeMs: 15_000,
    capacityProvider: async () => healthyObservation(),
    claimsProvider: async () => freshClaims(),
    conductorProvider: async (_lane, operation) => {
      calls.push(operation);
      if (operation.kind === 'status') return { ok: true, port: 4747, codexHome: path.join(root, 'conductors/primary/profile'), supportedRoles: ['leaf', 'lead'], threads: {} };
      if (operation.method === 'account/read') return { account };
      if (operation.method === 'account/rateLimits/read') return {
        observedAt: new Date(NOW - 15_001).toISOString(),
        rateLimits: {
          limitId: 'codex',
          primary: { usedPercent: 5, windowDurationMins: 300, resetsAt: '2026-09-14T20:00:00Z' },
        },
      };
      throw new Error('unexpected lifecycle transport');
    },
  }), /no eligible conductor.*USAGE_STALE/);
  assert.equal(calls.some((call) => call.kind === 'thread-start' || call.kind === 'turn-start'), false);
});

test('duplicate work intent is restart-safe and never starts a second native thread', async (t) => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'borg-route-duplicate-'));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  const config = buildDefaultConfig(root, process.execPath, { nodeBin: process.execPath });
  config.statePath = path.join(root, 'private/router');
  const account = { type: 'chatgpt', email: 'owner@example.test', planType: 'pro' };
  config.conductors[0].accountPin = accountIdentityDigest(account);
  let threadStarts = 0;
  const options = {
    cwd: root,
    prompt: 'bounded test',
    workId: 'same-intent',
    nowMs: NOW,
    capacityProvider: async () => healthyObservation(),
    claimsProvider: async () => freshClaims(),
    conductorProvider: async (_lane, operation) => {
      if (operation.kind === 'status') return { ok: true, port: 4747, codexHome: path.join(root, 'conductors/primary/profile'), supportedRoles: ['leaf', 'lead'], threads: {} };
      if (operation.method === 'account/read') return { account };
      if (operation.method === 'account/rateLimits/read') return {
        rateLimits: {
          limitId: 'codex',
          primary: { usedPercent: 5, windowDurationMins: 300, resetsAt: '2026-09-14T20:00:00Z' },
        },
      };
      if (operation.kind === 'thread-start') { threadStarts += 1; return { threadId: 'thread-1' }; }
      if (operation.kind === 'turn-start') return { turnId: 'turn-1', startedSeq: 4 };
      throw new Error(`unexpected operation ${JSON.stringify(operation)}`);
    },
  };

  const first = await dispatch(config, options);
  assert.equal(first.receipt.state, 'DISPATCHED');
  await assert.rejects(dispatch(config, options), /duplicate intent.*do not retry/i);
  assert.equal(threadStarts, 1);
});
