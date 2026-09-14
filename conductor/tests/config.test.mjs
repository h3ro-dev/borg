import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import test from 'node:test';
import { fileURLToPath } from 'node:url';

import {
  buildDefaultConfig,
  validateInstallConfig,
} from '../config.mjs';

const BORG_HOME = '/opt/borg-owner';

test('distributed config template validates without an account or fleet default', () => {
  const testDirectory = path.dirname(fileURLToPath(import.meta.url));
  const template = JSON.parse(fs.readFileSync(path.join(testDirectory, '../config.example.json'), 'utf8'));
  const config = validateInstallConfig(template, { expectedBorgHome: '/opt/borg-owner' });
  assert.deepEqual(config.machines.map((machine) => machine.id), ['local']);
  assert.deepEqual(config.conductors.map((lane) => lane.id), ['primary']);
  assert.equal(config.conductors[0].accountPin, null);
});

test('default config contains one owner-local machine and one dedicated unauthenticated profile', () => {
  const config = validateInstallConfig(
    buildDefaultConfig(BORG_HOME, '/opt/codex/bin/codex', { nodeBin: '/opt/node/bin/node' }),
    { expectedBorgHome: BORG_HOME },
  );

  assert.equal(config.borgHome, BORG_HOME);
  assert.equal(config.owner, 'owner');
  assert.equal(config.instance_id, '00000000-0000-4000-8000-000000000000');
  assert.equal(config.ports.conductor, 4747);
  assert.equal(config.runtime.nodeBin, '/opt/node/bin/node');
  assert.equal(config.appPath, path.join(BORG_HOME, 'app/conductor'));
  assert.deepEqual(config.machines.map((machine) => machine.id), ['local']);
  assert.deepEqual(config.conductors.map((lane) => lane.id), ['primary']);
  assert.equal(config.conductors[0].machineId, 'local');
  assert.equal(config.conductors[0].accountProfile, 'primary');
  assert.equal(config.conductors[0].codexHome, path.join(BORG_HOME, 'conductors/primary/profile'));
  assert.equal(config.conductors[0].logsPath, path.join(BORG_HOME, 'conductors/primary/logs'));
  assert.equal(config.conductors[0].accountPin, null);
  assert.equal(config.requiredVersions.node, '24.21.0');
  assert.equal(config.requiredVersions.codex, '0.146.0');
});

test('install config rejects noncanonical homes, relative runtime paths, remote listeners, and credentials', () => {
  const valid = buildDefaultConfig(BORG_HOME, '/opt/codex/bin/codex', { nodeBin: '/opt/node/bin/node' });

  assert.throws(
    () => validateInstallConfig({ ...valid, borgHome: '/opt/other' }, { expectedBorgHome: BORG_HOME }),
    /BORG_HOME does not match/,
  );
  assert.throws(
    () => validateInstallConfig({
      ...valid,
      conductors: [{ ...valid.conductors[0], codexHome: 'relative/profile' }],
    }, { expectedBorgHome: BORG_HOME }),
    /codexHome must be a canonical absolute path/,
  );
  assert.throws(
    () => validateInstallConfig({
      ...valid,
      conductors: [{ ...valid.conductors[0], host: '0.0.0.0' }],
    }, { expectedBorgHome: BORG_HOME }),
    /host must be 127.0.0.1/,
  );
  assert.throws(
    () => validateInstallConfig({ ...valid, accessToken: 'must-not-ship' }, { expectedBorgHome: BORG_HOME }),
    /credential-shaped key/,
  );
});

test('machine and lane inventory comes only from config and validates references', () => {
  const valid = buildDefaultConfig(BORG_HOME, '/opt/codex/bin/codex', { nodeBin: '/opt/node/bin/node' });
  const secondMachine = {
    id: 'build-host',
    capacity: {
      kind: 'command',
      command: '/opt/borg/bin/capacity-probe',
      args: ['--json'],
      maxAgeMs: 15_000,
      loadPerCoreLimit: 1.5,
      memoryUseLimitPercent: 92,
    },
    claims: { kind: 'command', command: '/opt/borg/bin/claims-probe', args: ['--json'], maxAgeMs: 15_000 },
  };
  const secondLane = {
    ...valid.conductors[0],
    id: 'secondary',
    machineId: 'build-host',
    accountProfile: 'secondary',
    codexHome: `${BORG_HOME}/conductors/secondary/profile`,
    logsPath: `${BORG_HOME}/conductors/secondary/logs`,
    port: 4748,
  };

  const config = validateInstallConfig({
    ...valid,
    machines: [...valid.machines, secondMachine],
    conductors: [...valid.conductors, secondLane],
  }, { expectedBorgHome: BORG_HOME });
  assert.deepEqual(config.machines.map((machine) => machine.id), ['local', 'build-host']);

  assert.throws(() => validateInstallConfig({
    ...valid,
    conductors: [{ ...valid.conductors[0], machineId: 'missing' }],
  }, { expectedBorgHome: BORG_HOME }), /unknown machineId/);

  assert.throws(() => validateInstallConfig({
    ...valid,
    machines: [...valid.machines, secondMachine],
    conductors: [...valid.conductors, { ...secondLane, accountProfile: 'primary' }],
  }, { expectedBorgHome: BORG_HOME }), /dedicated account profile name/);
});
