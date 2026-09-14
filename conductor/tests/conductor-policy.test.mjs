import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import test from 'node:test';

import {
  buildThreadResumeParams,
  buildThreadStartParams,
  loadOwnerPolicies,
  paginateEvents,
  readBody,
  resumedThreadRecord,
  responseForServerRequest,
} from '../conductor.mjs';

test('owner policies are required from BORG_HOME and role instructions remain distinct', () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'borg-policy-'));
  try {
    const policyDirectory = path.join(root, 'policies');
    fs.mkdirSync(policyDirectory, { recursive: true });
    fs.writeFileSync(path.join(policyDirectory, 'SEAT-RULES.md'), 'new owner seat policy\n');
    fs.writeFileSync(path.join(policyDirectory, 'LEAD-RULES.md'), 'new owner lead policy\n');
    const policies = loadOwnerPolicies(root);

    assert.equal(buildThreadStartParams({ cwd: '/tmp/work' }, policies).developerInstructions,
      'new owner seat policy');
    assert.equal(buildThreadStartParams({ cwd: '/tmp/work', role: 'lead', instructions: 'task' }, policies)
      .developerInstructions, 'new owner lead policy\n\ntask');
    assert.equal(buildThreadResumeParams({ threadId: 't-1' }, policies).developerInstructions,
      'new owner seat policy');
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
});

test('missing new-owner policies fail before a native thread can start', () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'borg-policy-missing-'));
  try {
    assert.throws(() => loadOwnerPolicies(root), /missing or empty owner policy/);
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
});

test('native approval callbacks retain fail-closed responses', () => {
  assert.deepEqual(responseForServerRequest({
    id: 7,
    method: 'item/commandExecution/requestApproval',
  }), { jsonrpc: '2.0', id: 7, result: { decision: 'decline' } });
  assert.deepEqual(responseForServerRequest({
    id: 8,
    method: 'item/permissions/requestApproval',
  }), { jsonrpc: '2.0', id: 8, result: { permissions: {}, scope: 'turn' } });
});

test('event pagination stays bounded and reports the next exact cursor', () => {
  const events = Array.from({ length: 240 }, (_, index) => ({ seq: index + 1, params: { threadId: 't-1' } }));
  const page = paginateEvents(events, { threadId: 't-1', afterSeq: 0, limit: 999 }, 240);
  assert.equal(page.events.length, 200);
  assert.equal(page.nextAfterSeq, 200);
  assert.equal(page.hasMore, true);
  assert.equal(page.lastSeq, 240);
});

test('HTTP JSON bodies fail with 413 above the protocol byte cap', async () => {
  const request = {
    headers: { 'content-length': String(65 * 1024) },
    async *[Symbol.asyncIterator]() { yield Buffer.alloc(65 * 1024); },
  };
  await assert.rejects(readBody(request), (error) => error.statusCode === 413);
});

test('resume bookkeeping fails closed on mismatched native identity or partial history', () => {
  const body = { threadId: 'expected-thread' };
  const response = { cwd: '/tmp/effective', thread: { id: 'expected-thread' } };
  assert.throws(() => resumedThreadRecord(body, response, {
    thread: { id: 'different-thread', turns: [] },
  }), /identity mismatch/);
  assert.throws(() => resumedThreadRecord(body, response, {
    thread: { id: 'expected-thread' },
  }), /complete turns/);
});
