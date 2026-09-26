import assert from 'node:assert/strict';
import fs from 'node:fs';
import net from 'node:net';
import os from 'node:os';
import path from 'node:path';
import test from 'node:test';

import { startConductor } from '../conductor.mjs';

async function unusedLoopbackPort() {
  const listener = net.createServer();
  await new Promise((resolve, reject) => {
    listener.once('error', reject);
    listener.listen(0, '127.0.0.1', resolve);
  });
  const { port } = listener.address();
  await new Promise((resolve, reject) => listener.close((error) => (error ? reject(error) : resolve())));
  return port;
}

test('resume rebuilds bookkeeping from effective native settings and complete readback', async (t) => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'borg-resume-protocol-'));
  const fakeCodex = path.join(root, 'fake-codex.mjs');
  const port = await unusedLoopbackPort();
  const historicalCwd = path.join(root, 'historical-worktree');
  const effectiveCwd = path.join(root, 'relocated-worktree');
  fs.writeFileSync(fakeCodex, `#!${process.execPath}
let input = '';
process.stdin.setEncoding('utf8');
process.stdin.on('data', (chunk) => {
  input += chunk;
  let newline;
  while ((newline = input.indexOf('\\n')) >= 0) {
    const line = input.slice(0, newline);
    input = input.slice(newline + 1);
    if (!line.trim()) continue;
    const message = JSON.parse(line);
    if (message.id === undefined) continue;
    let result;
    if (message.method === 'initialize') {
      result = { userAgent: 'fake-codex', platformFamily: 'unix', platformOs: 'test' };
    } else if (message.method === 'thread/resume') {
      result = {
        cwd: ${JSON.stringify(effectiveCwd)},
        model: 'native-effective-model',
        reasoningEffort: 'high',
        approvalPolicy: 'never',
        approvalsReviewer: 'user',
        sandbox: { type: 'dangerFullAccess' },
        modelProvider: 'test-provider',
        thread: {
          id: 'thread-relocated',
          cwd: ${JSON.stringify(historicalCwd)},
          status: { type: 'idle' },
          turns: [],
        },
      };
    } else if (message.method === 'thread/read') {
      if (message.params.threadId !== 'thread-relocated' || message.params.includeTurns !== true) {
        process.stdout.write(JSON.stringify({ jsonrpc: '2.0', id: message.id,
          error: { code: -32602, message: 'complete readback required' } }) + '\\n');
        continue;
      }
      result = { thread: {
        id: 'thread-relocated',
        cwd: ${JSON.stringify(historicalCwd)},
        status: { type: 'idle' },
        parentThreadId: 'parent-thread',
        forkedFromId: 'fork-source',
        sessionId: 'session-tree',
        createdAt: 1_700_000_000,
        updatedAt: 1_700_000_100,
        turns: [
          { id: 'turn-earlier', status: 'completed', items: [] },
          { id: 'turn-latest', status: 'failed', items: [] },
        ],
      } };
    } else {
      process.stdout.write(JSON.stringify({ jsonrpc: '2.0', id: message.id,
        error: { code: -32601, message: 'unexpected method ' + message.method } }) + '\\n');
      continue;
    }
    process.stdout.write(JSON.stringify({ jsonrpc: '2.0', id: message.id, result }) + '\\n');
  }
});
`, { mode: 0o700 });

  let listeningResolve;
  const listening = new Promise((resolve) => { listeningResolve = resolve; });
  const conductor = startConductor({
    port,
    codexBin: fakeCodex,
    codexHome: path.join(root, 'profile'),
    borgHome: path.join(root, 'borg'),
    logsPath: path.join(root, 'logs'),
    policies: { seat: 'new owner seat rules', lead: 'new owner lead rules' },
    installSignalHandlers: false,
    exitOnChildExit: false,
    onListening: listeningResolve,
  });
  t.after(async () => {
    conductor.close();
    await new Promise((resolve) => conductor.server.close(resolve));
    fs.rmSync(root, { recursive: true, force: true });
  });
  await listening;

  const token = fs.readFileSync(path.join(root, 'profile', '.conductor', 'http-token'), 'utf8').trim();
  const authHeaders = { 'content-type': 'application/json', authorization: `Bearer ${token}` };

  const predecessor = {
    laneId: 'prior-lane',
    threadId: 'thread-relocated',
    receiptDigest: 'sha256:test-evidence',
  };
  const resumed = await fetch(`http://127.0.0.1:${port}/thread/resume`, {
    method: 'POST',
    headers: authHeaders,
    body: JSON.stringify({
      threadId: 'thread-relocated',
      cwd: effectiveCwd,
      role: 'lead',
      model: 'requested-model',
      effort: 'xhigh',
      predecessor,
    }),
  });
  assert.equal(resumed.status, 200);
  assert.equal((await resumed.json()).cwd, effectiveCwd);

  const status = await (await fetch(`http://127.0.0.1:${port}/status`, {
    headers: { authorization: `Bearer ${token}` },
  })).json();
  const record = status.threads['thread-relocated'];
  assert.equal(record.cwd, effectiveCwd, 'top-level native response cwd wins over historical thread cwd');
  assert.equal(record.role, 'lead');
  assert.equal(record.model, 'native-effective-model');
  assert.equal(record.effort, 'high');
  assert.deepEqual(record.predecessor, predecessor);
  assert.equal(record.lastTurnId, 'turn-latest');
  assert.equal(record.lastTurnStatus, 'failed');
  assert.deepEqual(record.status, { type: 'idle' });
  assert.equal(record.parentThreadId, 'parent-thread');
  assert.equal(record.forkedFromId, 'fork-source');
  assert.equal(record.sessionId, 'session-tree');

  const resumedAgain = await fetch(`http://127.0.0.1:${port}/thread/resume`, {
    method: 'POST',
    headers: authHeaders,
    body: JSON.stringify({ threadId: 'thread-relocated', cwd: effectiveCwd }),
  });
  assert.equal(resumedAgain.status, 200);
  const nextStatus = await (await fetch(`http://127.0.0.1:${port}/status`, {
    headers: { authorization: `Bearer ${token}` },
  })).json();
  const nextRecord = nextStatus.threads['thread-relocated'];
  assert.equal(nextRecord.startedAt, record.startedAt);
  assert.equal(nextRecord.role, 'lead', 'an omitted role preserves the attached thread role');
  assert.deepEqual(nextRecord.predecessor, predecessor, 'an omitted predecessor preserves exact evidence');
});
