import assert from 'node:assert/strict';
import { EventEmitter } from 'node:events';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { PassThrough } from 'node:stream';
import test from 'node:test';
import {
  createConductor,
  extractSessionId,
  HOST,
  loadConfig,
  PROTOCOL,
  STEER_PATH,
} from './grok-conductor.mjs';

function fakeChild(exitCode = 0) {
  const child = new EventEmitter();
  child.stdout = new PassThrough();
  child.stderr = new PassThrough();
  child.kill = () => {
    child.emit('exit', null, 'SIGTERM');
  };
  queueMicrotask(() => {
    child.stdout.write(`${JSON.stringify({ type: 'text', data: 'pong' })}\n`);
    child.stdout.write(`${JSON.stringify({ type: 'end', sessionId: 'sess-1', stopReason: 'end_turn' })}\n`);
    child.emit('exit', exitCode, null);
  });
  return child;
}

function hangingChild(onKill) {
  const child = new EventEmitter();
  child.stdout = new PassThrough();
  child.stderr = new PassThrough();
  child.kill = (signal) => {
    onKill?.(signal);
    queueMicrotask(() => child.emit('exit', null, signal || 'SIGTERM'));
  };
  return child;
}

async function withServer(t, spawnGrok = () => fakeChild(), extra = {}) {
  const stateDirectory = fs.mkdtempSync(path.join(os.tmpdir(), 'grok-conductor-'));
  t.after(() => fs.rmSync(stateDirectory, { recursive: true, force: true }));
  const handle = createConductor({
    port: 0,
    stateDirectory,
    grokBin: '/tmp/fake-grok',
    grokHome: path.join(stateDirectory, 'grok-home'),
    spawnGrok,
    zombieIdleMs: extra.zombieIdleMs,
    steerInterruptMs: extra.steerInterruptMs ?? 200,
    nowFn: extra.nowFn,
    cwdActivityFn: extra.cwdActivityFn,
    seatRulesText: 'new owner test seat policy',
    readinessProvider: () => ({ readyForDispatch: true, loginState: 'proved' }),
    config: {
      laneId: 'grok-test',
      capabilities: ['reasoning', 'tools'],
      maxConcurrentTurns: 2,
    },
  });
  await new Promise((resolve) => handle.server.listen(0, HOST, resolve));
  const { port } = handle.server.address();
  t.after(() => handle.close());
  return { port, stateDirectory, ...handle };
}

async function call(port, method, pathname, body) {
  const protectedBody = method === 'POST' && body && pathname !== '/rpc'
    ? { ...body, protectedAction: false }
    : body;
  const response = await fetch(`http://${HOST}:${port}${pathname}`, {
    method,
    headers: protectedBody ? { 'content-type': 'application/json' } : undefined,
    body: protectedBody ? JSON.stringify(protectedBody) : undefined,
  });
  return { status: response.status, body: await response.json() };
}

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

test('extractSessionId reads camel and snake case', () => {
  assert.equal(extractSessionId({ sessionId: 'abc' }), 'abc');
  assert.equal(extractSessionId({ session_id: 'def' }), 'def');
  assert.equal(extractSessionId({ type: 'thought', data: 'x' }), null);
  assert.equal(extractSessionId({ params: { session_id: 'ghi' } }), 'ghi');
});

test('product config resolves the Grok adapter entirely beneath BORG_HOME', (t) => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'grok-product-config-'));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  const configPath = path.join(root, 'config.json');
  fs.writeFileSync(configPath, JSON.stringify({
    borgHome: root,
    providers: {
      grok: {
        enabled: true,
        host: '127.0.0.1',
        port: 4770,
        grokBin: '/opt/grok/bin/grok',
        grokHome: path.join(root, 'providers/grok/profile'),
        statePath: path.join(root, 'providers/grok/state'),
        expectedVersion: '1.2.3',
      },
    },
  }));
  const config = loadConfig(configPath);
  assert.equal(config.stateDirectory, path.join(root, 'providers/grok/state'));
  assert.equal(config.seatRulesPath, path.join(root, 'policies/SEAT-RULES.md'));
  assert.equal(config.agentLaunchDirectory, path.join(root, 'private/provider-launches'));
  assert.equal(config.laneId, 'grok');
});

test('status is a Grok peer, not a Codex lane', async (t) => {
  const { port } = await withServer(t);
  const { status, body } = await call(port, 'GET', '/status');
  assert.equal(status, 200);
  assert.equal(body.ok, true);
  assert.equal(body.runtime, 'grok');
  assert.equal(body.notACodexLane, true);
  assert.equal(body.protocol, PROTOCOL);
  assert.equal(body.host, HOST);
  assert.equal(body.steerPath, STEER_PATH);
});

test('rpc is refused', async (t) => {
  const { port } = await withServer(t);
  const { status, body } = await call(port, 'POST', '/rpc', { method: 'account/read' });
  assert.equal(status, 501);
  assert.match(body.error, /no Codex app-server RPC/);
});

test('thread and turn start, then complete', async (t) => {
  const { port } = await withServer(t);
  const cwd = fs.mkdtempSync(path.join(os.tmpdir(), 'grok-cwd-'));
  t.after(() => fs.rmSync(cwd, { recursive: true, force: true }));
  const started = await call(port, 'POST', '/thread/start', { cwd });
  assert.equal(started.status, 200);
  assert.ok(started.body.threadId);
  const turn = await call(port, 'POST', '/turn/start', { threadId: started.body.threadId, text: 'ping' });
  assert.equal(turn.status, 200);
  assert.ok(turn.body.turnId);
  assert.ok(turn.body.grokSessionId);
  await sleep(50);
  const status = await call(port, 'GET', '/status');
  const rec = status.body.threads[started.body.threadId];
  assert.equal(rec.lastTurnStatus, 'completed');
  assert.equal(rec.grokSessionId, 'sess-1');
});

test('rejects relative cwd', async (t) => {
  const { port } = await withServer(t);
  const { status } = await call(port, 'POST', '/thread/start', { cwd: 'relative' });
  assert.equal(status, 400);
});

test('sessionId is set at spawn, not only on end', async (t) => {
  const spawns = [];
  const { port } = await withServer(t, (opts) => {
    spawns.push(opts);
    return hangingChild();
  });
  const cwd = fs.mkdtempSync(path.join(os.tmpdir(), 'grok-cwd-'));
  t.after(() => fs.rmSync(cwd, { recursive: true, force: true }));
  const started = await call(port, 'POST', '/thread/start', { cwd, workId: 'session-canary' });
  const turn = await call(port, 'POST', '/turn/start', { threadId: started.body.threadId, text: 'hang' });
  assert.ok(turn.body.grokSessionId);
  assert.equal(spawns.length, 1);
  assert.equal(spawns[0].newSessionId, turn.body.grokSessionId);
  assert.equal(spawns[0].grokSessionId, null);
  const status = await call(port, 'GET', '/status');
  const rec = status.body.threads[started.body.threadId];
  assert.equal(rec.lastTurnStatus, 'running');
  assert.equal(rec.grokSessionId, turn.body.grokSessionId);
  assert.notEqual(rec.grokSessionId, null);
});

test('sessionId captured from first streaming event', async (t) => {
  const { port } = await withServer(t, () => {
    const child = hangingChild();
    queueMicrotask(() => {
      child.stdout.write(`${JSON.stringify({ type: 'thought', session_id: 'early-sess', data: 'hi' })}\n`);
    });
    return child;
  });
  const cwd = fs.mkdtempSync(path.join(os.tmpdir(), 'grok-cwd-'));
  t.after(() => fs.rmSync(cwd, { recursive: true, force: true }));
  const started = await call(port, 'POST', '/thread/start', { cwd });
  await call(port, 'POST', '/turn/start', { threadId: started.body.threadId, text: 'hang' });
  await sleep(40);
  const status = await call(port, 'GET', '/status');
  assert.equal(status.body.threads[started.body.threadId].grokSessionId, 'early-sess');
});

test('next turn resumes the captured session', async (t) => {
  const spawns = [];
  const { port } = await withServer(t, (opts) => {
    spawns.push(opts);
    return fakeChild();
  });
  const cwd = fs.mkdtempSync(path.join(os.tmpdir(), 'grok-cwd-'));
  t.after(() => fs.rmSync(cwd, { recursive: true, force: true }));
  const started = await call(port, 'POST', '/thread/start', { cwd });
  await call(port, 'POST', '/turn/start', { threadId: started.body.threadId, text: 'first' });
  await sleep(50);
  await call(port, 'POST', '/turn/start', { threadId: started.body.threadId, text: 'second' });
  await sleep(50);
  assert.equal(spawns.length, 2);
  assert.ok(spawns[0].newSessionId);
  assert.equal(spawns[0].grokSessionId, null);
  assert.equal(spawns[1].grokSessionId, 'sess-1');
  assert.equal(spawns[1].newSessionId, null);
});

test('steer-while-running interrupts and resumes same session', async (t) => {
  const spawns = [];
  const { port } = await withServer(t, (opts) => {
    spawns.push(opts);
    if (spawns.length === 1) {
      const child = hangingChild();
      queueMicrotask(() => {
        child.stdout.write(`${JSON.stringify({ type: 'thought', sessionId: 'live-sess', data: 'working' })}\n`);
      });
      return child;
    }
    return fakeChild();
  });
  const cwd = fs.mkdtempSync(path.join(os.tmpdir(), 'grok-cwd-'));
  t.after(() => fs.rmSync(cwd, { recursive: true, force: true }));
  const started = await call(port, 'POST', '/thread/start', { cwd, workId: 'steer-canary' });
  const turn = await call(port, 'POST', '/turn/start', { threadId: started.body.threadId, text: 'long work' });
  await sleep(30);
  const before = Date.now();
  const steer = await call(port, 'POST', '/turn/steer', {
    workId: 'steer-canary',
    text: 'stop and summarize',
  });
  const elapsed = Date.now() - before;
  assert.equal(steer.status, 200);
  assert.equal(steer.body.queued, false);
  assert.equal(steer.body.mode, 'interrupt-resume');
  assert.equal(steer.body.path, STEER_PATH);
  assert.ok(elapsed < 2000);
  assert.notEqual(steer.body.turnId, turn.body.turnId);
  await sleep(50);
  assert.equal(spawns.length, 2);
  assert.equal(spawns[1].grokSessionId, 'live-sess');
  assert.match(spawns[1].text, /MID-JOB STEER/);
});

test('steer on idle thread starts a follow-up turn', async (t) => {
  const { port } = await withServer(t);
  const cwd = fs.mkdtempSync(path.join(os.tmpdir(), 'grok-cwd-'));
  t.after(() => fs.rmSync(cwd, { recursive: true, force: true }));
  const started = await call(port, 'POST', '/thread/start', { cwd });
  await call(port, 'POST', '/turn/start', { threadId: started.body.threadId, text: 'ping' });
  await sleep(50);
  const steer = await call(port, 'POST', '/turn/steer', { threadId: started.body.threadId, text: 'again' });
  assert.equal(steer.status, 200);
  assert.equal(steer.body.mode, 'follow-up-turn');
  await sleep(50);
});

test('zombie detect marks interrupted when running with no child', async (t) => {
  const now = { t: 1_700_000_000_000 };
  const { port, threads, children, reapZombies } = await withServer(
    t,
    () => hangingChild(),
    {
      zombieIdleMs: 1000,
      nowFn: () => now.t,
      cwdActivityFn: () => now.t - 60_000,
    },
  );
  const cwd = fs.mkdtempSync(path.join(os.tmpdir(), 'grok-cwd-'));
  t.after(() => fs.rmSync(cwd, { recursive: true, force: true }));
  const started = await call(port, 'POST', '/thread/start', { cwd, workId: 'zombie-canary' });
  await call(port, 'POST', '/turn/start', { threadId: started.body.threadId, text: 'hang' });
  const rec = threads.get(started.body.threadId);
  const turnId = rec.lastTurnId;
  children.delete(turnId);
  rec.lastOutputAt = new Date(now.t - 20_000).toISOString();
  rec.lastSpawnAt = rec.lastOutputAt;
  const reaped = reapZombies();
  assert.deepEqual(reaped, [started.body.threadId]);
  const status = await call(port, 'GET', '/status');
  assert.equal(status.body.threads[started.body.threadId].lastTurnStatus, 'interrupted');
});

test('zombie detect does not reap a live child', async (t) => {
  const now = { t: Date.now() };
  const { port, reapZombies } = await withServer(
    t,
    () => hangingChild(),
    {
      zombieIdleMs: 1000,
      nowFn: () => now.t,
      cwdActivityFn: () => now.t - 60_000,
    },
  );
  const cwd = fs.mkdtempSync(path.join(os.tmpdir(), 'grok-cwd-'));
  t.after(() => fs.rmSync(cwd, { recursive: true, force: true }));
  const started = await call(port, 'POST', '/thread/start', { cwd });
  await call(port, 'POST', '/turn/start', { threadId: started.body.threadId, text: 'hang' });
  assert.deepEqual(reapZombies(), []);
  const status = await call(port, 'GET', '/status');
  assert.equal(status.body.threads[started.body.threadId].lastTurnStatus, 'running');
});

test('interrupt with no child marks a zombie running turn interrupted', async (t) => {
  const { port, threads, children } = await withServer(t, () => hangingChild());
  const cwd = fs.mkdtempSync(path.join(os.tmpdir(), 'grok-cwd-'));
  t.after(() => fs.rmSync(cwd, { recursive: true, force: true }));
  const started = await call(port, 'POST', '/thread/start', { cwd, workId: 'int-zombie' });
  await call(port, 'POST', '/turn/start', { threadId: started.body.threadId, text: 'hang' });
  children.delete(threads.get(started.body.threadId).lastTurnId);
  const stopped = await call(port, 'POST', '/turn/interrupt', { workId: 'int-zombie' });
  assert.equal(stopped.status, 200);
  assert.equal(stopped.body.interrupted, true);
  assert.equal(stopped.body.zombie, true);
  const status = await call(port, 'GET', '/status');
  assert.equal(status.body.threads[started.body.threadId].lastTurnStatus, 'interrupted');
});
