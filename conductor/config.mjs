import fs from 'node:fs';
import path from 'node:path';

const CREDENTIAL_KEY = /(auth|cookie|credential|password|secret|token)/i;
const SAFE_ID = /^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$/;
const SHA256 = /^[a-f0-9]{64}$/;
const UUID = /^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$/i;

function requireObject(value, label) {
  if (!value || typeof value !== 'object' || Array.isArray(value)) {
    throw new Error(`${label} must be an object`);
  }
  return value;
}

function requireString(value, label) {
  if (typeof value !== 'string' || !value.trim()) throw new Error(`${label} is required`);
  return value.trim();
}

function canonicalAbsolute(value, label) {
  const text = requireString(value, label);
  if (!path.isAbsolute(text) || path.normalize(text) !== text || path.resolve(text) !== text) {
    throw new Error(`${label} must be a canonical absolute path`);
  }
  return text;
}

function positiveInteger(value, label, low, high) {
  const number = Number(value);
  if (!Number.isInteger(number) || number < low || number > high) {
    throw new Error(`${label} must be an integer from ${low} to ${high}`);
  }
  return number;
}

function assertNoCredentialKeys(value, label = 'config') {
  if (!value || typeof value !== 'object') return;
  for (const [key, child] of Object.entries(value)) {
    if (CREDENTIAL_KEY.test(key)) {
      throw new Error(`${label}.${key} is a credential-shaped key and is forbidden`);
    }
    assertNoCredentialKeys(child, `${label}.${key}`);
  }
}

function validateProbe(raw, label, defaultKind) {
  const probe = requireObject(raw, label);
  const kind = requireString(probe.kind ?? defaultKind, `${label}.kind`);
  if (!['local-os', 'command', 'router-state'].includes(kind)) {
    throw new Error(`${label}.kind is unsupported`);
  }
  if (kind === 'command') canonicalAbsolute(probe.command, `${label}.command`);
  const args = probe.args ?? [];
  if (!Array.isArray(args) || args.some((value) => typeof value !== 'string')) {
    throw new Error(`${label}.args must be an array of strings`);
  }
  return {
    kind,
    ...(probe.command ? { command: canonicalAbsolute(probe.command, `${label}.command`) } : {}),
    args: [...args],
    maxAgeMs: positiveInteger(probe.maxAgeMs ?? 15_000, `${label}.maxAgeMs`, 1_000, 300_000),
    ...(label.endsWith('.capacity') ? {
      loadPerCoreLimit: Number(probe.loadPerCoreLimit ?? 1.5),
      memoryUseLimitPercent: Number(probe.memoryUseLimitPercent ?? 92),
    } : {}),
  };
}

function validateMachine(raw, index) {
  const label = `machines[${index}]`;
  const machine = requireObject(raw, label);
  const id = requireString(machine.id, `${label}.id`);
  if (!SAFE_ID.test(id)) throw new Error(`${label}.id is invalid`);
  const capacity = validateProbe(machine.capacity, `${label}.capacity`, 'local-os');
  if (!(capacity.loadPerCoreLimit > 0 && capacity.loadPerCoreLimit <= 10)) {
    throw new Error(`${label}.capacity.loadPerCoreLimit must be greater than 0 and at most 10`);
  }
  if (!(capacity.memoryUseLimitPercent > 0 && capacity.memoryUseLimitPercent <= 100)) {
    throw new Error(`${label}.capacity.memoryUseLimitPercent must be greater than 0 and at most 100`);
  }
  const claims = validateProbe(machine.claims, `${label}.claims`, 'router-state');
  if (capacity.kind === 'local-os' && id !== 'local') {
    throw new Error(`${label}.capacity local-os is valid only for machine id local`);
  }
  return { id, capacity, claims };
}

function validateLane(raw, index, borgHome, machineIds) {
  const label = `conductors[${index}]`;
  const lane = requireObject(raw, label);
  const id = requireString(lane.id, `${label}.id`);
  const machineId = requireString(lane.machineId, `${label}.machineId`);
  if (!SAFE_ID.test(id)) throw new Error(`${label}.id is invalid`);
  if (!machineIds.has(machineId)) throw new Error(`${label}.machineId references unknown machineId`);
  const host = requireString(lane.host ?? '127.0.0.1', `${label}.host`);
  if (host !== '127.0.0.1') throw new Error(`${label}.host must be 127.0.0.1`);
  const capabilities = lane.capabilities ?? ['reasoning', 'tools'];
  if (!Array.isArray(capabilities) || capabilities.length === 0
      || capabilities.some((item) => !['reasoning', 'tools'].includes(item))) {
    throw new Error(`${label}.capabilities is invalid`);
  }
  const accountPin = lane.accountPin ?? null;
  if (accountPin !== null && (typeof accountPin !== 'string' || !SHA256.test(accountPin))) {
    throw new Error(`${label}.accountPin must be null or a sha256 digest`);
  }
  const codexHome = canonicalAbsolute(lane.codexHome, `${label}.codexHome`);
  const logsPath = canonicalAbsolute(lane.logsPath, `${label}.logsPath`);
  for (const [candidate, pathLabel] of [[codexHome, 'codexHome'], [logsPath, 'logsPath']]) {
    if (candidate !== borgHome && !candidate.startsWith(`${borgHome}${path.sep}`)) {
      throw new Error(`${label}.${pathLabel} must be inside BORG_HOME`);
    }
  }
  return {
    id,
    machineId,
    accountProfile: requireString(lane.accountProfile, `${label}.accountProfile`),
    codexHome,
    codexBin: canonicalAbsolute(lane.codexBin, `${label}.codexBin`),
    logsPath,
    host,
    port: positiveInteger(lane.port, `${label}.port`, 1024, 65535),
    capabilities: [...new Set(capabilities)],
    accountPin,
    enabled: lane.enabled !== false,
  };
}

export function validateInstallConfig(raw, options = {}) {
  requireObject(raw, 'config');
  assertNoCredentialKeys(raw);
  if (raw.schemaVersion !== 1) throw new Error('schemaVersion must be 1');
  const borgHome = canonicalAbsolute(raw.borgHome, 'borgHome');
  if (options.expectedBorgHome !== undefined
      && canonicalAbsolute(options.expectedBorgHome, 'expected BORG_HOME') !== borgHome) {
    throw new Error('configured BORG_HOME does not match expected BORG_HOME');
  }
  const statePath = canonicalAbsolute(raw.statePath, 'statePath');
  if (statePath !== borgHome && !statePath.startsWith(`${borgHome}${path.sep}`)) {
    throw new Error('statePath must be inside BORG_HOME');
  }
  const requiredVersions = requireObject(raw.requiredVersions, 'requiredVersions');
  const owner = requireString(raw.owner, 'owner');
  if (/[\u0000-\u001f\u007f]/.test(owner)) throw new Error('owner contains control characters');
  const instanceId = requireString(raw.instance_id, 'instance_id');
  if (!UUID.test(instanceId)) throw new Error('instance_id must be a UUID');
  const ports = requireObject(raw.ports, 'ports');
  const conductorPort = positiveInteger(ports.conductor, 'ports.conductor', 1024, 65535);
  const runtime = requireObject(raw.runtime, 'runtime');
  const nodeBin = canonicalAbsolute(runtime.nodeBin, 'runtime.nodeBin');
  const runtimeCodexBin = canonicalAbsolute(runtime.codexBin, 'runtime.codexBin');
  const appPath = canonicalAbsolute(raw.appPath, 'appPath');
  if (appPath !== borgHome && !appPath.startsWith(`${borgHome}${path.sep}`)) {
    throw new Error('appPath must be inside BORG_HOME');
  }
  const machines = raw.machines;
  if (!Array.isArray(machines) || machines.length === 0) throw new Error('machines must be non-empty');
  const normalizedMachines = machines.map(validateMachine);
  const machineIds = new Set(normalizedMachines.map((machine) => machine.id));
  if (machineIds.size !== normalizedMachines.length) throw new Error('machine ids must be unique');
  const conductors = raw.conductors;
  if (!Array.isArray(conductors) || conductors.length === 0) throw new Error('conductors must be non-empty');
  const normalizedConductors = conductors.map((lane, index) => validateLane(lane, index, borgHome, machineIds));
  if (new Set(normalizedConductors.map((lane) => lane.id)).size !== normalizedConductors.length) {
    throw new Error('conductor ids must be unique');
  }
  if (new Set(normalizedConductors.map((lane) => `${lane.host}:${lane.port}`)).size !== normalizedConductors.length) {
    throw new Error('conductor endpoints must be unique');
  }
  if (new Set(normalizedConductors.map((lane) => lane.codexHome)).size !== normalizedConductors.length) {
    throw new Error('each conductor must have a dedicated CODEX_HOME');
  }
  if (new Set(normalizedConductors.map((lane) => lane.accountProfile)).size !== normalizedConductors.length) {
    throw new Error('each conductor must have a dedicated account profile name');
  }
  if (new Set(normalizedConductors.map((lane) => lane.logsPath)).size !== normalizedConductors.length) {
    throw new Error('each conductor must have a dedicated logs path');
  }
  const primary = normalizedConductors.find((lane) => lane.id === 'primary');
  if (!primary || primary.port !== conductorPort) {
    throw new Error('ports.conductor must match the primary conductor port');
  }
  if (primary.codexBin !== runtimeCodexBin) {
    throw new Error('runtime.codexBin must match the primary conductor CODEX_BIN');
  }
  return {
    schemaVersion: 1,
    owner,
    instance_id: instanceId,
    borgHome,
    appPath,
    statePath,
    ports: { conductor: conductorPort },
    runtime: { nodeBin, codexBin: runtimeCodexBin },
    requiredVersions: {
      node: requireString(requiredVersions.node, 'requiredVersions.node'),
      codex: requireString(requiredVersions.codex, 'requiredVersions.codex'),
    },
    routing: {
      timeoutMs: positiveInteger(raw.routing?.timeoutMs ?? 5_000, 'routing.timeoutMs', 500, 60_000),
      lockTimeoutMs: positiveInteger(raw.routing?.lockTimeoutMs ?? 30_000, 'routing.lockTimeoutMs', 1_000, 60_000),
      defaultLimitId: requireString(raw.routing?.defaultLimitId ?? 'codex', 'routing.defaultLimitId'),
      modelLimitIds: { ...(raw.routing?.modelLimitIds ?? {}) },
    },
    machines: normalizedMachines,
    conductors: normalizedConductors,
    providers: { ...(raw.providers ?? {}) },
  };
}

export function buildDefaultConfig(borgHomeInput, codexBinInput, options = {}) {
  const borgHome = canonicalAbsolute(borgHomeInput, 'BORG_HOME');
  const codexBin = canonicalAbsolute(codexBinInput, 'CODEX_BIN');
  const nodeBin = canonicalAbsolute(options.nodeBin, 'NODE_BIN');
  const port = positiveInteger(options.port ?? 4747, 'conductor port', 1024, 65535);
  const owner = requireString(options.owner ?? 'owner', 'owner');
  const instanceId = requireString(
    options.instanceId ?? '00000000-0000-4000-8000-000000000000',
    'instance_id',
  );
  if (!UUID.test(instanceId)) throw new Error('instance_id must be a UUID');
  return {
    schemaVersion: 1,
    owner,
    instance_id: instanceId,
    borgHome,
    appPath: path.join(borgHome, 'app/conductor'),
    statePath: path.join(borgHome, 'private/router'),
    ports: { conductor: port },
    runtime: { nodeBin, codexBin },
    requiredVersions: { node: '24.21.0', codex: '0.146.0' },
    routing: {
      timeoutMs: 5_000,
      lockTimeoutMs: 30_000,
      defaultLimitId: 'codex',
      modelLimitIds: {},
    },
    machines: [{
      id: 'local',
      capacity: {
        kind: 'local-os',
        args: [],
        maxAgeMs: 15_000,
        loadPerCoreLimit: 1.5,
        memoryUseLimitPercent: 92,
      },
      claims: { kind: 'router-state', args: [], maxAgeMs: 15_000 },
    }],
    conductors: [{
      id: 'primary',
      machineId: 'local',
      accountProfile: 'primary',
      codexHome: path.join(borgHome, 'conductors/primary/profile'),
      codexBin,
      logsPath: path.join(borgHome, 'conductors/primary/logs'),
      host: '127.0.0.1',
      port,
      capabilities: ['reasoning', 'tools'],
      accountPin: null,
      enabled: true,
    }],
    providers: {
      grok: { enabled: false, capabilities: ['reasoning', 'interrupt-resume-steer'] },
      claude: { enabled: false, capabilities: ['reasoning'], missingCapabilities: ['native-thread-status', 'mid-turn-steer'] },
    },
  };
}

export function loadInstallConfig(configPathInput, options = {}) {
  const configPath = canonicalAbsolute(configPathInput, 'config path');
  const stat = fs.lstatSync(configPath);
  if (!stat.isFile() || stat.isSymbolicLink()) throw new Error('config must be a regular non-symlink file');
  if ((stat.mode & 0o077) !== 0) throw new Error('config must be owner-only');
  return validateInstallConfig(JSON.parse(fs.readFileSync(configPath, 'utf8')), options);
}

export function laneById(config, laneId = 'primary') {
  const lane = config.conductors.find((candidate) => candidate.id === laneId);
  if (!lane) throw new Error(`unknown conductor lane: ${laneId}`);
  return lane;
}
