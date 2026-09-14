#!/usr/bin/env node

import { spawnSync } from 'node:child_process';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

import {
  buildDefaultConfig,
  laneById,
  loadInstallConfig,
  validateInstallConfig,
} from './config.mjs';
import { startConductor } from './conductor.mjs';
import { accountIdentityDigest, dispatch, nativeConductorProvider, rank } from './router/router.mjs';

const SEAT_POLICY = `# New owner seat policy

This policy belongs to this BORG installation's owner. Keep each task within its assigned scope, preserve unrelated work, use provider-native authentication, and report evidence honestly. Never copy policy from another owner's installation.
`;

const LEAD_POLICY = `# New owner lead policy

This policy belongs to this BORG installation's owner. Coordinate only work the owner assigned, give each worker bounded ownership and acceptance checks, preserve recovery evidence, and verify the integrated result. Never copy policy from another owner's installation.
`;

function canonicalAbsolute(value, label) {
  if (typeof value !== 'string' || !path.isAbsolute(value)
      || path.normalize(value) !== value || path.resolve(value) !== value) {
    throw new Error(`${label} must be a canonical absolute path`);
  }
  return value;
}

function validateExecutableIfPresent(target, label) {
  if (!fs.existsSync(target)) return;
  let usable = false;
  try {
    usable = fs.statSync(target).isFile();
    fs.accessSync(target, fs.constants.X_OK);
  } catch {
    usable = false;
  }
  if (!usable) throw new Error(`${label} must be an executable file`);
}

function resolveConductorConfigPath(value, borgHome) {
  const candidate = value === undefined
    ? path.join(borgHome, 'conductors/config.json')
    : path.isAbsolute(value)
      ? value
      : path.resolve(borgHome, value);
  const configPath = canonicalAbsolute(candidate, 'conductor config path');
  if (!configPath.startsWith(`${borgHome}${path.sep}`)) {
    throw new Error('conductor config path must be inside BORG_HOME');
  }
  if (configPath === path.join(borgHome, 'config.json')) {
    throw new Error('conductor config path must not be the root-owned BORG_HOME/config.json');
  }
  return configPath;
}

function privateDirectory(directory) {
  fs.mkdirSync(directory, { recursive: true, mode: 0o700 });
  fs.chmodSync(directory, 0o700);
}

function createPrivateFile(target, body) {
  fs.writeFileSync(target, body, { encoding: 'utf8', mode: 0o600, flag: 'wx' });
  fs.chmodSync(target, 0o600);
}

export async function bootstrap({
  borgHome: borgHomeInput,
  configPath: configPathInput,
  owner,
  instanceId,
  port,
  nodeBin,
  codexBin: codexBinInput,
}) {
  for (const [label, value] of [
    ['borgHome', borgHomeInput],
    ['owner', owner],
    ['instanceId', instanceId],
    ['port', port],
    ['nodeBin', nodeBin],
    ['codexBin', codexBinInput],
  ]) {
    if (value === undefined || value === null || value === '') {
      throw new Error(`${label} is required`);
    }
  }
  const borgHome = canonicalAbsolute(borgHomeInput, 'BORG_HOME');
  canonicalAbsolute(nodeBin, 'NODE_BIN');
  const codexBin = canonicalAbsolute(codexBinInput, 'CODEX_BIN');
  validateExecutableIfPresent(nodeBin, 'NODE_BIN');
  validateExecutableIfPresent(codexBin, 'CODEX_BIN');
  const configPath = resolveConductorConfigPath(configPathInput, borgHome);
  const config = buildDefaultConfig(borgHome, codexBin, {
    owner,
    instanceId,
    port,
    nodeBin,
  });
  const seatPolicyPath = path.join(borgHome, 'policies/SEAT-RULES.md');
  const leadPolicyPath = path.join(borgHome, 'policies/LEAD-RULES.md');
  let created = false;
  if (fs.existsSync(configPath)) {
    const existing = loadInstallConfig(configPath, { expectedBorgHome: borgHome });
    const fixedFields = [
      ['owner', existing.owner, config.owner],
      ['instance_id', existing.instance_id, config.instance_id],
      ['ports.conductor', existing.ports.conductor, config.ports.conductor],
      ['runtime.nodeBin', existing.runtime.nodeBin, config.runtime.nodeBin],
      ['runtime.codexBin', existing.runtime.codexBin, config.runtime.codexBin],
      ['appPath', existing.appPath, config.appPath],
      ['primary.codexHome', laneById(existing).codexHome, laneById(config).codexHome],
      ['primary.logsPath', laneById(existing).logsPath, laneById(config).logsPath],
    ];
    const conflict = fixedFields.find(([, actual, expected]) => actual !== expected);
    if (conflict) {
      throw new Error(`bootstrap input ${conflict[0]} conflicts with existing config`);
    }
  }
  for (const directory of [
    borgHome,
    path.dirname(configPath),
    path.join(borgHome, 'policies'),
    config.statePath,
    config.conductors[0].codexHome,
    config.conductors[0].logsPath,
  ]) privateDirectory(directory);
  if (!fs.existsSync(seatPolicyPath)) createPrivateFile(seatPolicyPath, SEAT_POLICY);
  if (!fs.existsSync(leadPolicyPath)) createPrivateFile(leadPolicyPath, LEAD_POLICY);
  if (!fs.existsSync(configPath)) {
    createPrivateFile(configPath, `${JSON.stringify(config, null, 2)}\n`);
    created = true;
  }
  return { created, borgHome, configPath, accountAuthenticated: false };
}

function parseArgs(argv) {
  const command = argv[0] ?? 'help';
  const positional = [];
  const flags = {};
  for (let index = 1; index < argv.length; index += 1) {
    const item = argv[index];
    if (!item.startsWith('--')) {
      positional.push(item);
      continue;
    }
    const key = item.slice(2).replace(/-([a-z])/g, (_, letter) => letter.toUpperCase());
    if (['json'].includes(key)) {
      flags[key] = true;
      continue;
    }
    if (index + 1 >= argv.length || argv[index + 1].startsWith('--')) {
      throw new Error(`missing value for ${item}`);
    }
    flags[key] = argv[++index];
  }
  return { command, positional, ...flags };
}

function usage() {
  return `Usage:
  borg-conductor bootstrap --borg-home ABS [--config ABS|conductors/config.json] --owner ID --instance-id UUID --port N --node-bin ABS --codex-bin ABS
  borg-conductor config [--config ABS|conductors/config.json]
  borg-conductor start [--config ABS|conductors/config.json] [--lane ID]
  borg-conductor status [--config ABS|conductors/config.json]
  borg-conductor auth status|pin|login [--config ABS|conductors/config.json] [--lane ID]
  borg-conductor rank [--config ABS|conductors/config.json] [--capability tools|reasoning] [--model MODEL]
  borg-conductor route --config ABS|conductors/config.json --cwd ABS --prompt-file ABS --work-id ID [--role leaf|lead] [--model MODEL] [--effort EFFORT]
`;
}

function configPathFor(args) {
  const borgHome = canonicalAbsolute(process.env.BORG_HOME, 'BORG_HOME');
  return resolveConductorConfigPath(args.config, borgHome);
}

async function statuses(config) {
  return Promise.all(config.conductors.map(async (lane) => {
    try {
      const status = await nativeConductorProvider(lane, { kind: 'status' }, config.routing.timeoutMs);
      return { laneId: lane.id, state: 'PASS', status };
    } catch (error) {
      return { laneId: lane.id, state: 'FAIL', issue: error.message };
    }
  }));
}

async function authCommand(config, configPath, action, laneId) {
  const lane = laneById(config, laneId);
  if (action === 'login') {
    const result = spawnSync(lane.codexBin, ['login'], {
      stdio: 'inherit',
      env: { ...process.env, CODEX_HOME: lane.codexHome },
    });
    if (result.error) throw result.error;
    if (result.status !== 0) throw new Error(`native codex login exited ${result.status}`);
    return { laneId: lane.id, nativeLogin: 'complete', accountPin: lane.accountPin };
  }
  const result = await nativeConductorProvider(lane, {
    kind: 'rpc', method: 'account/read', params: {},
  }, config.routing.timeoutMs);
  let pin = null;
  try { pin = accountIdentityDigest(result.account); } catch { /* unauthenticated remains explicit */ }
  if (action === 'status') {
    return {
      laneId: lane.id,
      authenticated: pin !== null,
      accountMatchesPin: pin !== null && lane.accountPin !== null && pin === lane.accountPin,
      pinConfigured: lane.accountPin !== null,
    };
  }
  if (action !== 'pin') throw new Error(`unknown auth action: ${action}`);
  if (!pin) throw new Error('native account is not authenticated; run auth login first');
  const updated = {
    ...config,
    conductors: config.conductors.map((candidate) => (
      candidate.id === lane.id ? { ...candidate, accountPin: pin } : candidate
    )),
  };
  const temporary = `${configPath}.${process.pid}.tmp`;
  fs.writeFileSync(temporary, `${JSON.stringify(updated, null, 2)}\n`, { mode: 0o600, flag: 'wx' });
  fs.renameSync(temporary, configPath);
  return { laneId: lane.id, authenticated: true, accountPinned: true };
}

export async function main(argv = process.argv.slice(2)) {
  const args = parseArgs(argv);
  if (args.command === 'help' || args.command === '--help') {
    process.stdout.write(usage());
    return;
  }
  if (args.command === 'bootstrap') {
    process.stdout.write(`${JSON.stringify(await bootstrap({
      borgHome: args.borgHome,
      configPath: args.config,
      owner: args.owner,
      instanceId: args.instanceId,
      port: args.port,
      nodeBin: args.nodeBin,
      codexBin: args.codexBin,
    }), null, 2)}\n`);
    return;
  }
  const configPath = configPathFor(args);
  const config = loadInstallConfig(configPath, { expectedBorgHome: process.env.BORG_HOME ?? undefined });
  if (args.command === 'config') {
    process.stdout.write(`${JSON.stringify(validateInstallConfig(config, { expectedBorgHome: config.borgHome }), null, 2)}\n`);
    return;
  }
  if (args.command === 'start') {
    const lane = laneById(config, args.lane);
    startConductor({
      borgHome: config.borgHome,
      port: lane.port,
      codexHome: lane.codexHome,
      codexBin: lane.codexBin,
      logsPath: lane.logsPath,
    });
    return;
  }
  if (args.command === 'status') {
    process.stdout.write(`${JSON.stringify({ schemaVersion: 1, conductors: await statuses(config) }, null, 2)}\n`);
    return;
  }
  if (args.command === 'auth') {
    process.stdout.write(`${JSON.stringify(await authCommand(
      config, configPath, args.positional[0] ?? 'status', args.lane ?? 'primary',
    ), null, 2)}\n`);
    return;
  }
  if (args.command === 'rank') {
    process.stdout.write(`${JSON.stringify(await rank(config, {
      capability: args.capability,
      model: args.model,
    }), null, 2)}\n`);
    return;
  }
  if (args.command === 'route') {
    const promptPath = canonicalAbsolute(args.promptFile, 'prompt-file');
    const prompt = fs.readFileSync(promptPath, 'utf8');
    const result = await dispatch(config, {
      cwd: canonicalAbsolute(args.cwd, 'cwd'),
      prompt,
      workId: args.workId,
      role: args.role,
      capability: args.capability,
      model: args.model,
      effort: args.effort,
    });
    process.stdout.write(`${JSON.stringify({ ...result.receipt, receiptPath: result.receiptPath }, null, 2)}\n`);
    return;
  }
  throw new Error(`unknown command: ${args.command}`);
}

const isMain = process.argv[1]
  && fileURLToPath(import.meta.url) === path.resolve(process.argv[1]);
if (isMain) {
  main().catch((error) => {
    process.stderr.write(`borg-conductor: ${error.message}\n`);
    process.exitCode = 1;
  });
}
