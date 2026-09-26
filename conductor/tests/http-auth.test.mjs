import assert from 'node:assert/strict';
import fs from 'node:fs';
import http from 'node:http';
import net from 'node:net';
import os from 'node:os';
import path from 'node:path';
import test from 'node:test';

import * as conductorApi from '../conductor.mjs';

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

function request(port, pathname, options = {}) {
  return new Promise((resolve, reject) => {
    const body = options.body === undefined
      ? null
      : (typeof options.body === 'string' ? options.body : JSON.stringify(options.body));
    const headers = {
      host: `127.0.0.1:${port}`,
      ...(options.headers || {}),
      ...(body === null ? {} : { 'content-length': Buffer.byteLength(body) }),
    };
    const req = http.request({
      host: '127.0.0.1', port, path: pathname, method: options.method || 'GET', headers,
    }, (res) => {
      const chunks = [];
      res.on('data', (chunk) => chunks.push(chunk));
      res.on('end', () => {
        const text = Buffer.concat(chunks).toString('utf8');
        resolve({ status: res.statusCode, body: text ? JSON.parse(text) : null });
      });
    });
    req.once('error', reject);
    if (body !== null) req.end(body);
    else req.end();
  });
}

async function conductorFixture(t, authMode) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), `borg-http-${authMode}-`));
  const fakeCodex = path.join(root, 'fake-codex.mjs');
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
    const result = message.method === 'initialize'
      ? { userAgent: 'fake-codex', platformFamily: 'unix', platformOs: 'test' }
      : { method: message.method, account: { type: 'chatgpt', email: 'synthetic-owner', planType: 'pro' } };
    process.stdout.write(JSON.stringify({ jsonrpc: '2.0', id: message.id, result }) + '\\n');
  }
});
`, { mode: 0o700 });
  const port = await unusedLoopbackPort();
  const codexHome = path.join(root, 'profile');
  let listeningResolve;
  const listening = new Promise((resolve) => { listeningResolve = resolve; });
  const conductor = conductorApi.startConductor({
    port,
    authMode,
    codexBin: fakeCodex,
    codexHome,
    borgHome: path.join(root, 'borg'),
    logsPath: path.join(root, 'logs'),
    policies: { seat: 'owner seat rules', lead: 'owner lead rules' },
    installSignalHandlers: false,
    exitOnChildExit: false,
    onListening: listeningResolve,
  });
  t.after(async () => {
    const closed = conductor.server.listening
      ? new Promise((resolve) => conductor.server.once('close', resolve))
      : Promise.resolve();
    conductor.close();
    await closed;
    fs.rmSync(root, { recursive: true, force: true });
  });
  await listening;
  const tokenPath = path.join(codexHome, '.conductor', 'http-token');
  assert.equal(fs.existsSync(tokenPath), true);
  const token = fs.readFileSync(tokenPath, 'utf8').trim();
  assert.match(token, /^[a-f0-9]{64}$/);
  assert.equal(fs.statSync(tokenPath).mode & 0o777, 0o600);
  assert.equal(fs.statSync(path.dirname(tokenPath)).mode & 0o777, 0o700);
  return { root, port, token, tokenPath };
}

function bearer(token) {
  return { authorization: `Bearer ${token}` };
}

test('fresh conductors enforce bearer, JSON, RPC, host, and browser boundaries by default', async (t) => {
  const f = await conductorFixture(t, undefined);
  const health = await request(f.port, '/healthz');
  assert.deepEqual(health, { status: 200, body: { ok: true } });

  assert.equal((await request(f.port, '/status')).status, 401);
  assert.equal((await request(f.port, '/status', { headers: bearer('0'.repeat(64)) })).status, 401);
  const status = await request(f.port, '/status', { headers: bearer(f.token) });
  assert.equal(status.status, 200);
  assert.equal(status.body.auth.mode, 'enforce');
  assert.equal(status.body.auth.unauthenticatedCount, 2);
  assert.ok(status.body.auth.lastUnauthenticatedAt);

  assert.equal((await request(f.port, '/status', {
    headers: { ...bearer(f.token), host: 'attacker.example' },
  })).status, 403);
  assert.equal((await request(f.port, '/status', {
    headers: { ...bearer(f.token), origin: 'https://attacker.example' },
  })).status, 403);
  assert.equal((await request(f.port, '/status', {
    headers: { ...bearer(f.token), 'sec-fetch-site': 'cross-site' },
  })).status, 403);
  assert.equal((await request(f.port, '/status', {
    headers: { ...bearer(f.token), 'sec-fetch-site': 'none' },
  })).status, 200);

  const noJson = await request(f.port, '/rpc', {
    method: 'POST', headers: bearer(f.token), body: { method: 'account/read', params: {} },
  });
  assert.equal(noJson.status, 415);
  const disallowed = await request(f.port, '/rpc', {
    method: 'POST', headers: { ...bearer(f.token), 'content-type': 'application/json' },
    body: { method: 'dangerous/write', params: {} },
  });
  assert.equal(disallowed.status, 403);
  const allowed = await request(f.port, '/rpc', {
    method: 'POST', headers: { ...bearer(f.token), 'content-type': 'application/json; charset=utf-8' },
    body: { method: 'account/read', params: {} },
  });
  assert.equal(allowed.status, 200);
  assert.equal(allowed.body.method, 'account/read');

  const counters = await request(f.port, '/status', { headers: bearer(f.token) });
  assert.equal(counters.body.auth.disallowedRpcCount, 1);
  assert.deepEqual([...conductorApi.RPC_METHOD_ALLOWLIST].sort(), [
    'account/rateLimits/read', 'account/read', 'config/read', 'hooks/list', 'model/list', 'thread/read',
  ]);
});

test('report mode serves legacy callers, counts violations, and rate-limits audit lines', async (t) => {
  const f = await conductorFixture(t, 'report');
  assert.equal((await request(f.port, '/healthz')).status, 200);
  assert.equal((await request(f.port, '/status')).status, 200);
  assert.equal((await request(f.port, '/status', { headers: bearer(f.token) })).status, 200);
  assert.equal((await request(f.port, '/status', {
    headers: { host: 'attacker.example' },
  })).status, 403, 'host checks stay enforced in report mode without a token');
  assert.equal((await request(f.port, '/status', {
    headers: { ...bearer(f.token), origin: 'https://attacker.example' },
  })).status, 403, 'browser checks stay enforced in report mode with a token');

  for (let index = 0; index < 2; index += 1) {
    const response = await request(f.port, '/rpc', {
      method: 'POST',
      body: { method: 'dangerous/write', params: {} },
    });
    assert.equal(response.status, 200);
  }
  const status = await request(f.port, '/status', { headers: bearer(f.token) });
  assert.equal(status.body.auth.mode, 'report');
  assert.equal(status.body.auth.disallowedRpcCount, 2);
  assert.equal(status.body.auth.unauthenticatedCount, 3);

  await new Promise((resolve) => setImmediate(resolve));
  const auditFile = fs.readdirSync(path.join(f.root, 'logs'))
    .map((name) => path.join(f.root, 'logs', name))
    .find((file) => path.basename(file).startsWith('http-auth-'));
  assert.ok(auditFile);
  const lines = fs.readFileSync(auditFile, 'utf8').trim().split('\n').map(JSON.parse);
  assert.equal(lines.filter((entry) => entry.path === '/rpc' && entry.reason === 'missing_token').length, 1);
  assert.equal(lines.filter((entry) => entry.path === '/rpc' && entry.reason === 'invalid_content_type').length, 1);
  assert.equal(lines.filter((entry) => entry.path === '/rpc' && entry.reason === 'disallowed_rpc_method').length, 1);
  assert.ok(lines.every((entry) => Object.keys(entry).sort().join(',') === 'method,path,reason,ts,userAgent'));
  assert.equal(fs.readFileSync(auditFile, 'utf8').includes(f.token), false);
});
