import assert from 'node:assert/strict';
import { execFileSync, spawn } from 'node:child_process';
import fs from 'node:fs';
import net from 'node:net';
import os from 'node:os';
import path from 'node:path';
import test from 'node:test';
import { fileURLToPath } from 'node:url';

import { bootstrap } from '../borg-conductor.mjs';

const CODEX_BIN = process.env.BORG_TEST_CODEX_BIN;
const testDirectory = path.dirname(fileURLToPath(import.meta.url));
const cliPath = path.join(testDirectory, '../borg-conductor.mjs');

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

async function awaitNativeStatus(port, child, diagnostics, tokenPath, deadlineMs = Date.now() + 20_000) {
  while (Date.now() < deadlineMs) {
    if (child.exitCode !== null) {
      throw new Error(`conductor exited ${child.exitCode}: ${diagnostics.join('')}`);
    }
    try {
      const token = fs.existsSync(tokenPath) ? fs.readFileSync(tokenPath, 'utf8').trim() : null;
      const response = await fetch(`http://127.0.0.1:${port}/status`, {
        headers: token ? { authorization: `Bearer ${token}` } : {},
        signal: AbortSignal.timeout(1_000),
      });
      if (response.ok) return response.json();
    } catch {
      // A failed interaction is readiness evidence; retry until the bounded deadline.
    }
    await new Promise((resolve) => setTimeout(resolve, 50));
  }
  throw new Error(`native /status did not become ready: ${diagnostics.join('')}`);
}

test('real Codex app-server initializes with a separate unauthenticated profile', {
  skip: CODEX_BIN ? false : 'set BORG_TEST_CODEX_BIN to the native Codex executable',
  timeout: 30_000,
}, async (t) => {
  const parent = fs.mkdtempSync(path.join(os.tmpdir(), 'borg-native-canary-'));
  const borgHome = path.join(parent, 'borg');
  const port = await unusedLoopbackPort();
  const diagnostics = [];
  await bootstrap({
    borgHome,
    owner: 'native-canary-owner',
    instanceId: 'e2c0fcf4-1a83-4b51-bbe2-eebf8035dd21',
    port,
    nodeBin: process.execPath,
    codexBin: path.resolve(CODEX_BIN),
  });
  const profile = path.join(borgHome, 'conductors/primary/profile');
  const tokenPath = path.join(profile, '.conductor/http-token');
  assert.equal(fs.existsSync(path.join(profile, 'auth.json')), false);

  const child = spawn(process.execPath, [cliPath, 'start', '--config', 'conductors/config.json', '--lane', 'primary'], {
    env: { ...process.env, BORG_HOME: borgHome },
    stdio: ['ignore', 'pipe', 'pipe'],
  });
  child.stdout.on('data', (chunk) => diagnostics.push(chunk.toString()));
  child.stderr.on('data', (chunk) => diagnostics.push(chunk.toString()));
  t.after(async () => {
    if (child.exitCode === null) child.kill('SIGTERM');
    await Promise.race([
      new Promise((resolve) => child.once('exit', resolve)),
      new Promise((resolve) => setTimeout(resolve, 2_000)),
    ]);
    if (child.exitCode === null) child.kill('SIGKILL');
    fs.rmSync(parent, { recursive: true, force: true });
  });

  const status = await awaitNativeStatus(port, child, diagnostics, tokenPath);
  assert.equal(status.ok, true);
  assert.equal(status.port, port);
  assert.equal(status.codexHome, profile);
  assert.deepEqual(status.supportedRoles, ['leaf', 'lead']);
  assert.deepEqual(status.threads, {});
  assert.equal(fs.existsSync(path.join(profile, 'auth.json')), false);

  const routedStatus = JSON.parse(execFileSync(process.execPath, [
    cliPath, 'status', '--config', 'conductors/config.json',
  ], { encoding: 'utf8', env: { ...process.env, BORG_HOME: borgHome } }));
  assert.equal(routedStatus.conductors[0].state, 'PASS');
  assert.ok(Array.isArray(routedStatus.conductors[0].status.nativeThreads.data));
});
