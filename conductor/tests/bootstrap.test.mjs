import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import test from 'node:test';
import { fileURLToPath } from 'node:url';

import { bootstrap } from '../borg-conductor.mjs';

const testDirectory = path.dirname(fileURLToPath(import.meta.url));
const cliPath = path.join(testDirectory, '../borg-conductor.mjs');

test('bootstrap creates only owner-local policy, config, profile, logs, and private state', async (t) => {
  const parent = fs.mkdtempSync(path.join(os.tmpdir(), 'borg-bootstrap-'));
  t.after(() => fs.rmSync(parent, { recursive: true, force: true }));
  const borgHome = path.join(parent, 'owner-borg');
  const result = await bootstrap({
    borgHome,
    owner: 'example-owner',
    instanceId: '2e2f5e46-c196-44df-a7fd-dd509a1b1486',
    port: 4811,
    nodeBin: path.join(borgHome, 'runtime/node'),
    codexBin: path.join(borgHome, 'runtime/npm/node_modules/.bin/codex'),
  });

  assert.equal(result.created, true);
  assert.equal(fs.statSync(borgHome).mode & 0o777, 0o700);
  assert.equal(fs.statSync(path.join(borgHome, 'conductors/config.json')).mode & 0o777, 0o600);
  assert.equal(fs.statSync(path.join(borgHome, 'conductors/primary/profile')).mode & 0o777, 0o700);
  assert.match(fs.readFileSync(path.join(borgHome, 'policies/SEAT-RULES.md'), 'utf8'), /new owner/i);
  assert.doesNotMatch(fs.readFileSync(path.join(borgHome, 'policies/SEAT-RULES.md'), 'utf8'), /prior owner doctrine/i);
  const config = JSON.parse(fs.readFileSync(path.join(borgHome, 'conductors/config.json'), 'utf8'));
  assert.equal(config.owner, 'example-owner');
  assert.equal(config.instance_id, '2e2f5e46-c196-44df-a7fd-dd509a1b1486');
  assert.equal(config.ports.conductor, 4811);
  assert.equal(config.conductors[0].port, 4811);
  assert.equal(config.runtime.nodeBin, path.join(borgHome, 'runtime/node'));
  assert.equal(config.runtime.codexBin, path.join(borgHome, 'runtime/npm/node_modules/.bin/codex'));
  assert.equal(config.conductors[0].accountPin, null);
  assert.equal(fs.existsSync(path.join(borgHome, 'conductors/primary/profile/auth.json')), false);
});

test('bootstrap is idempotent for identical inputs and preserves owner-edited policies', async (t) => {
  const parent = fs.mkdtempSync(path.join(os.tmpdir(), 'borg-bootstrap-existing-'));
  t.after(() => fs.rmSync(parent, { recursive: true, force: true }));
  const borgHome = path.join(parent, 'owner-borg');
  const options = {
    borgHome,
    owner: 'example-owner',
    instanceId: '2e2f5e46-c196-44df-a7fd-dd509a1b1486',
    port: 4811,
    nodeBin: path.join(borgHome, 'runtime/node'),
    codexBin: path.join(borgHome, 'runtime/npm/node_modules/.bin/codex'),
  };
  await bootstrap(options);
  const seat = path.join(borgHome, 'policies/SEAT-RULES.md');
  fs.writeFileSync(seat, 'owner edited policy\n');
  const second = await bootstrap(options);
  assert.equal(second.created, false);
  assert.equal(fs.readFileSync(seat, 'utf8'), 'owner edited policy\n');
});

test('bootstrap rejects conflicting identity or runtime inputs without overwriting config', async (t) => {
  const parent = fs.mkdtempSync(path.join(os.tmpdir(), 'borg-bootstrap-conflict-'));
  t.after(() => fs.rmSync(parent, { recursive: true, force: true }));
  const borgHome = path.join(parent, 'owner-borg');
  const options = {
    borgHome,
    owner: 'example-owner',
    instanceId: '2e2f5e46-c196-44df-a7fd-dd509a1b1486',
    port: 4811,
    nodeBin: path.join(borgHome, 'runtime/node'),
    codexBin: path.join(borgHome, 'runtime/npm/node_modules/.bin/codex'),
  };
  await bootstrap(options);
  const before = fs.readFileSync(path.join(borgHome, 'conductors/config.json'), 'utf8');
  await assert.rejects(bootstrap({ ...options, port: 4812 }), /conflicts with existing config/);
  assert.equal(fs.readFileSync(path.join(borgHome, 'conductors/config.json'), 'utf8'), before);
});

test('bootstrap requires every installer-owned identity, port, and provider path explicitly', async () => {
  const complete = {
    borgHome: '/opt/example-borg',
    owner: 'example-owner',
    instanceId: '2e2f5e46-c196-44df-a7fd-dd509a1b1486',
    port: 4811,
    nodeBin: '/opt/example-borg/runtime/node',
    codexBin: '/opt/example-borg/runtime/npm/node_modules/.bin/codex',
  };
  for (const key of Object.keys(complete)) {
    await assert.rejects(bootstrap({ ...complete, [key]: undefined }), new RegExp(key, 'i'));
  }
});

test('bootstrap CLI is idempotent with the documented explicit arguments', (t) => {
  const parent = fs.mkdtempSync(path.join(os.tmpdir(), 'borg-bootstrap-cli-'));
  t.after(() => fs.rmSync(parent, { recursive: true, force: true }));
  const borgHome = path.join(parent, 'owner-borg');
  const args = [cliPath, 'bootstrap',
    '--borg-home', borgHome,
    '--config', 'conductors/config.json',
    '--owner', 'example-owner',
    '--instance-id', '2e2f5e46-c196-44df-a7fd-dd509a1b1486',
    '--port', '4811',
    '--node-bin', path.join(borgHome, 'runtime/node'),
    '--codex-bin', path.join(borgHome, 'runtime/npm/node_modules/.bin/codex'),
  ];
  const first = JSON.parse(execFileSync(process.execPath, args, { encoding: 'utf8' }));
  const second = JSON.parse(execFileSync(process.execPath, args, { encoding: 'utf8' }));
  assert.equal(first.created, true);
  assert.equal(second.created, false);
  assert.equal(second.configPath, path.join(borgHome, 'conductors/config.json'));
});

test('bootstrap preserves a preexisting root borg-install config byte-for-byte', async (t) => {
  const parent = fs.mkdtempSync(path.join(os.tmpdir(), 'borg-bootstrap-coexist-'));
  t.after(() => fs.rmSync(parent, { recursive: true, force: true }));
  const borgHome = path.join(parent, 'owner-borg');
  fs.mkdirSync(borgHome, { recursive: true });
  const rootConfig = path.join(borgHome, 'config.json');
  const rootBytes = Buffer.from('{"schema":"borg-install/v1","home":"preserve-exactly","projects":[]}\n');
  fs.writeFileSync(rootConfig, rootBytes, { mode: 0o600 });
  const conductorConfig = path.join(borgHome, 'conductors/config.json');

  await assert.rejects(bootstrap({
    borgHome,
    configPath: path.join(borgHome, 'conductors/directory-runtime.json'),
    owner: 'example-owner',
    instanceId: '2e2f5e46-c196-44df-a7fd-dd509a1b1486',
    port: 4811,
    nodeBin: borgHome,
    codexBin: path.join(borgHome, 'runtime/npm/node_modules/.bin/codex'),
  }), /NODE_BIN must be an executable file/);

  const result = JSON.parse(execFileSync(process.execPath, [cliPath, 'bootstrap',
    '--borg-home', borgHome,
    '--config', 'conductors/config.json',
    '--owner', 'example-owner',
    '--instance-id', '2e2f5e46-c196-44df-a7fd-dd509a1b1486',
    '--port', '4811',
    '--node-bin', path.join(borgHome, 'runtime/node-v24/bin/node'),
    '--codex-bin', path.join(borgHome, 'runtime/npm/node_modules/.bin/codex'),
  ], { encoding: 'utf8', env: { ...process.env, BORG_HOME: borgHome } }));

  assert.equal(result.configPath, conductorConfig);
  assert.deepEqual(fs.readFileSync(rootConfig), rootBytes);
  assert.equal(JSON.parse(fs.readFileSync(conductorConfig, 'utf8')).schemaVersion, 1);
  await assert.rejects(bootstrap({
    borgHome,
    configPath: rootConfig,
    owner: 'example-owner',
    instanceId: '2e2f5e46-c196-44df-a7fd-dd509a1b1486',
    port: 4811,
    nodeBin: path.join(borgHome, 'runtime/node-v24/bin/node'),
    codexBin: path.join(borgHome, 'runtime/npm/node_modules/.bin/codex'),
  }), /must not be the root-owned/);
  assert.deepEqual(fs.readFileSync(rootConfig), rootBytes);
});
