import { execFile as execFileCallback } from 'node:child_process';
import crypto from 'node:crypto';
import { promises as fs } from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { promisify } from 'node:util';

import { validateInstallConfig } from '../config.mjs';
import { readPrivateJsonDirectory, readPrivateJsonRecord } from './receipt-reader.mjs';

// Portable extraction of the supplied conductor-usage-router and
// fleet-placement invariants. Estate-specific roster, SSH shipping, and
// global lane-watch paths are intentionally replaced by validated install
// config plus explicit native capacity/claims probes.

const execFile = promisify(execFileCallback);
const ACTIVE_RECEIPT_STATES = new Set([
  'DISPATCHED',
  'ATTEMPTING',
  'THREAD_STARTED',
  'UNKNOWN_DO_NOT_RETRY',
  'STARTED_TURN_UNKNOWN',
]);

function sha256(value) {
  return crypto.createHash('sha256').update(value).digest('hex');
}

function canonicalAccount(account) {
  if (!account || typeof account !== 'object' || Array.isArray(account)) return null;
  const type = typeof account.type === 'string' ? account.type.trim().toLowerCase() : null;
  const email = typeof account.email === 'string' ? account.email.trim().toLowerCase() : null;
  const planType = typeof account.planType === 'string' ? account.planType.trim().toLowerCase() : null;
  if (!type || !email || !planType) return null;
  return { type, email, planType };
}

export function accountIdentityDigest(account) {
  const canonical = canonicalAccount(account);
  if (!canonical) throw new Error('provider account identity is incomplete');
  return sha256(JSON.stringify(canonical));
}

function timestampMillis(value) {
  const result = typeof value === 'string' ? Date.parse(value) : NaN;
  return Number.isFinite(result) ? result : null;
}

function finite(value, minimum, maximum) {
  const number = typeof value === 'number' ? value : NaN;
  return Number.isFinite(number) && number >= minimum && number <= maximum ? number : null;
}

export function evaluateMachineAdmission(machine, observation, claims, nowMs = Date.now()) {
  const issues = [];
  if (!observation || typeof observation !== 'object') {
    issues.push('CAPACITY_UNKNOWN');
  } else {
    const observedAt = timestampMillis(observation.observedAt);
    if (observedAt === null) issues.push('CAPACITY_TIME_UNKNOWN');
    else if (nowMs - observedAt > machine.capacity.maxAgeMs || observedAt > nowMs + 1_000) {
      issues.push('CAPACITY_STALE');
    }
    if (observation.reachable !== true) issues.push('MACHINE_UNREACHABLE');
    const loadPerCore = finite(observation.loadPerCore, 0, 100);
    const memoryUsePercent = finite(observation.memoryUsePercent, 0, 100);
    if (loadPerCore === null) issues.push('LOAD_UNKNOWN');
    else if (loadPerCore >= machine.capacity.loadPerCoreLimit) issues.push('CAPACITY_HOT');
    if (memoryUsePercent === null) issues.push('MEMORY_UNKNOWN');
    else if (memoryUsePercent >= machine.capacity.memoryUseLimitPercent) issues.push('MEMORY_SATURATED');
  }
  if (!claims || typeof claims !== 'object' || !Array.isArray(claims.active)) {
    issues.push('CLAIMS_UNKNOWN');
  } else {
    const observedAt = timestampMillis(claims.observedAt);
    if (observedAt === null) issues.push('CLAIMS_TIME_UNKNOWN');
    else if (nowMs - observedAt > machine.claims.maxAgeMs || observedAt > nowMs + 1_000) {
      issues.push('CLAIMS_STALE');
    }
    if (claims.active.some((claim) => !claim || typeof claim !== 'object'
        || typeof claim.workId !== 'string' || !claim.workId)) {
      issues.push('CLAIMS_INVALID');
    }
  }
  return {
    machineId: machine.id,
    eligible: issues.length === 0,
    issues: [...new Set(issues)],
    observation: observation ?? null,
    claims: claims ?? null,
  };
}

export function rankCandidates(candidates) {
  return [...candidates].sort((left, right) => (
    Number(right.eligible) - Number(left.eligible)
      || (right.remainingPercent ?? -1) - (left.remainingPercent ?? -1)
      || timestampMillis(left.resetAt) - timestampMillis(right.resetAt)
      || (left.activeTurns ?? Number.MAX_SAFE_INTEGER) - (right.activeTurns ?? Number.MAX_SAFE_INTEGER)
      || String(left.laneId).localeCompare(String(right.laneId))
  ));
}

async function commandJson(probe) {
  const result = await execFile(probe.command, probe.args, {
    encoding: 'utf8',
    timeout: Math.min(probe.maxAgeMs, 60_000),
    maxBuffer: 1024 * 1024,
  });
  return JSON.parse(result.stdout);
}

export async function observeMachineCapacity(machine, nowMs = Date.now()) {
  if (machine.capacity.kind === 'command') return commandJson(machine.capacity);
  if (machine.capacity.kind !== 'local-os') throw new Error('CAPACITY_KIND_UNSUPPORTED');
  const cores = os.cpus().length;
  const totalMemory = os.totalmem();
  const freeMemory = os.freemem();
  if (!Number.isInteger(cores) || cores < 1 || totalMemory <= 0) {
    throw new Error('CAPACITY_UNKNOWN');
  }
  return {
    schemaVersion: 1,
    observedAt: new Date(nowMs).toISOString(),
    source: 'local-os',
    reachable: true,
    logicalCores: cores,
    loadPerCore: os.loadavg()[0] / cores,
    memoryUsePercent: ((totalMemory - freeMemory) / totalMemory) * 100,
  };
}

async function readJsonFiles(directory) {
  return readPrivateJsonDirectory(directory);
}

export async function observeClaims(machine, statePath, nowMs = Date.now()) {
  if (machine.claims.kind === 'command') return commandJson(machine.claims);
  if (machine.claims.kind !== 'router-state') throw new Error('CLAIMS_KIND_UNSUPPORTED');
  return observeLocalReceiptClaims(statePath, nowMs);
}

async function observeLocalReceiptClaims(statePath, nowMs = Date.now()) {
  const receipts = await readJsonFiles(path.join(statePath, 'dispatch-receipts'));
  const terminal = new Set(['PRE_START_FAILED', 'COMPLETED', 'FAILED', 'CANCELLED', 'RECONCILED_NO_START']);
  for (const receipt of receipts) {
    if (!ACTIVE_RECEIPT_STATES.has(receipt.state) && !terminal.has(receipt.state)) {
      throw new Error('RECEIPT_STATE_UNKNOWN');
    }
    if (ACTIVE_RECEIPT_STATES.has(receipt.state) && (typeof receipt.workId !== 'string' || !receipt.workId.trim())) {
      throw new Error('RECEIPT_WORK_ID_INVALID');
    }
    if (ACTIVE_RECEIPT_STATES.has(receipt.state) && (typeof receipt.cwd !== 'string' || !path.isAbsolute(receipt.cwd))) {
      throw new Error('RECEIPT_CWD_INVALID');
    }
  }
  return {
    schemaVersion: 1,
    observedAt: new Date(nowMs).toISOString(),
    source: 'router-state',
    active: receipts.filter((receipt) => ACTIVE_RECEIPT_STATES.has(receipt.state)).map((receipt) => ({
      workId: receipt.workId,
      attemptId: receipt.attemptId,
      cwd: receipt.cwd,
      laneId: receipt.laneId,
      state: receipt.state,
      attemptedAt: receipt.attemptedAt,
    })),
  };
}

async function fetchJson(url, init, timeoutMs) {
  const response = await fetch(url, { ...init, signal: AbortSignal.timeout(timeoutMs) });
  if (!response.ok) throw new Error(`HTTP_${response.status}`);
  try {
    return await response.json();
  } catch {
    throw new Error('INVALID_JSON');
  }
}

export async function nativeConductorProvider(lane, operation, timeoutMs = 5_000) {
  const base = `http://${lane.host}:${lane.port}`;
  if (operation.kind === 'status') {
    const status = await fetchJson(`${base}/status`, { method: 'GET' }, timeoutMs);
    const nativeThreads = await fetchJson(`${base}/threads?limit=100`, { method: 'GET' }, timeoutMs);
    return { ...status, nativeThreads };
  }
  if (operation.kind === 'rpc') {
    return fetchJson(`${base}/rpc`, {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ method: operation.method, params: operation.params ?? {}, timeoutMs }),
    }, timeoutMs);
  }
  if (operation.kind === 'thread-start') {
    const endpoint = operation.role === 'lead' ? '/lead/thread/start' : '/thread/start';
    return fetchJson(`${base}${endpoint}`, {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify(operation.body),
    }, 60_000);
  }
  if (operation.kind === 'turn-start') {
    return fetchJson(`${base}/turn/start`, {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify(operation.body),
    }, 60_000);
  }
  throw new Error(`unknown conductor operation: ${operation.kind}`);
}

function activeTurnCount(status) {
  const observed = status?.nativeThreads?.data ?? status?.nativeThreads?.threads;
  const threads = Array.isArray(observed)
    ? observed
    : status?.threads && typeof status.threads === 'object'
      ? Object.values(status.threads)
      : [];
  return threads.filter((thread) => thread?.lastTurnStatus === 'running'
    || thread?.status?.type === 'active').length;
}

function normalizeReset(value) {
  if (typeof value === 'number' && Number.isFinite(value)) {
    return new Date(value < 10_000_000_000 ? value * 1_000 : value).toISOString();
  }
  if (typeof value === 'string' && Number.isFinite(Date.parse(value))) {
    return new Date(value).toISOString();
  }
  return null;
}

function normalizeWindow(kind, raw, nowMs) {
  if (!raw) return null;
  const usedPercent = finite(raw.usedPercent, 0, 100);
  const resetAt = normalizeReset(raw.resetsAt);
  const duration = finite(raw.windowDurationMins, 1, Number.MAX_SAFE_INTEGER);
  if (usedPercent === null) throw new Error(`${kind.toUpperCase()}_USED_INVALID`);
  if (!resetAt) throw new Error(`${kind.toUpperCase()}_RESET_INVALID`);
  if (Date.parse(resetAt) <= nowMs) throw new Error(`${kind.toUpperCase()}_RESET_STALE`);
  if (duration === null) throw new Error(`${kind.toUpperCase()}_DURATION_INVALID`);
  return { kind, usedPercent, remainingPercent: 100 - usedPercent, resetAt, windowDurationMins: duration };
}

function ineligibleLane(lane, issues, details = {}) {
  return {
    laneId: lane.id,
    machineId: lane.machineId,
    eligible: false,
    remainingPercent: details.remainingPercent ?? null,
    resetAt: details.resetAt ?? null,
    activeTurns: details.activeTurns ?? null,
    issues: [...new Set(issues)],
  };
}

async function probeLane(config, lane, machineAdmission, options) {
  const issues = [...machineAdmission.issues];
  if (!lane.enabled) issues.push('LANE_DISABLED');
  if (!lane.capabilities.includes(options.capability)) issues.push('CAPABILITY_NOT_PROVED');
  let status;
  try {
    status = await options.conductorProvider(lane, { kind: 'status' });
  } catch (error) {
    return ineligibleLane(lane, [...issues, `STATUS_${error.message}`]);
  }
  const activeTurns = activeTurnCount(status);
  if (status?.ok !== true) issues.push('STATUS_NOT_OK');
  if (status?.port !== lane.port) issues.push('PORT_MISMATCH');
  if (status?.codexHome !== lane.codexHome) issues.push('PROFILE_MISMATCH');
  if (options.role === 'lead' && !status?.supportedRoles?.includes('lead')) issues.push('ROLE_NOT_SUPPORTED');
  if (!lane.accountPin) issues.push('ACCOUNT_NOT_PINNED');
  let account;
  try {
    const result = await options.conductorProvider(lane, {
      kind: 'rpc', method: 'account/read', params: {},
    });
    account = result?.account;
  } catch (error) {
    return ineligibleLane(lane, [...issues, `ACCOUNT_${error.message}`], { activeTurns });
  }
  try {
    if (lane.accountPin && accountIdentityDigest(account) !== lane.accountPin) issues.push('ACCOUNT_MISMATCH');
  } catch {
    issues.push('ACCOUNT_UNAUTHENTICATED');
  }
  if (issues.length > 0) return ineligibleLane(lane, issues, { activeTurns });
  let rateBody;
  try {
    rateBody = await options.conductorProvider(lane, {
      kind: 'rpc', method: 'account/rateLimits/read', params: {},
    });
  } catch (error) {
    return ineligibleLane(lane, [`USAGE_${error.message}`], { activeTurns });
  }
  const usageObservedAt = rateBody?.observedAt ? timestampMillis(rateBody.observedAt) : options.nowMs;
  if (usageObservedAt === null || options.nowMs - usageObservedAt > options.usageMaxAgeMs
      || usageObservedAt > options.nowMs + 1_000) {
    return ineligibleLane(lane, ['USAGE_STALE'], { activeTurns });
  }
  const limitId = config.routing.modelLimitIds[String(options.model || '').toLowerCase()]
    || config.routing.defaultLimitId;
  const bucket = rateBody?.rateLimitsByLimitId?.[limitId]
    || (rateBody?.rateLimits?.limitId === limitId ? rateBody.rateLimits : null);
  if (!bucket) return ineligibleLane(lane, ['LIMIT_BUCKET_MISSING'], { activeTurns });
  let windows;
  try {
    windows = [
      normalizeWindow('primary', bucket.primary, options.nowMs),
      normalizeWindow('secondary', bucket.secondary, options.nowMs),
    ].filter(Boolean);
    if (windows.length === 0) throw new Error('NO_USAGE_WINDOWS');
  } catch (error) {
    return ineligibleLane(lane, [error.message], { activeTurns });
  }
  const blocking = windows.sort((left, right) => left.remainingPercent - right.remainingPercent
    || Date.parse(left.resetAt) - Date.parse(right.resetAt))[0];
  if (blocking.remainingPercent <= 0 || bucket.rateLimitReachedType) {
    return ineligibleLane(lane, ['PROVIDER_EXHAUSTED'], {
      activeTurns, remainingPercent: blocking.remainingPercent, resetAt: blocking.resetAt,
    });
  }
  if (bucket.spendControlReached === true) {
    return ineligibleLane(lane, ['PROVIDER_SPEND_CONTROL'], {
      activeTurns, remainingPercent: blocking.remainingPercent, resetAt: blocking.resetAt,
    });
  }
  return {
    laneId: lane.id,
    machineId: lane.machineId,
    eligible: true,
    remainingPercent: blocking.remainingPercent,
    resetAt: blocking.resetAt,
    activeTurns,
    issues: [],
    limitId,
    windows,
  };
}

function stageDeadline(operation, stage, timeoutMs) {
  if (!Number.isInteger(timeoutMs) || timeoutMs < 1 || timeoutMs > 60000) throw new Error('STAGE_TIMEOUT_INVALID');
  let timer;
  return Promise.race([
    Promise.resolve().then(operation),
    new Promise((_, reject) => {
      timer = setTimeout(() => {
        const error = new Error(`${stage}_TIMEOUT`);
        error.code = `${stage}_TIMEOUT`;
        reject(error);
      }, timeoutMs);
    }),
  ]).finally(() => clearTimeout(timer));
}

async function snapshot(config, options) {
  const rows = await Promise.all(config.machines.map(async (machine) => {
    const errors = [];
    const read = async (operation, stage) => {
      try { return await stageDeadline(operation, stage, options.stageTimeoutMs); }
      catch (error) {
        errors.push(error.code || `${stage}_PROBE_FAILED`);
        return null;
      }
    };
    const [capacity, claims] = await Promise.all([
      read(() => options.capacityProvider(machine, options.nowMs), 'CAPACITY'),
      read(() => options.claimsProvider(machine, config.statePath, options.nowMs), 'CLAIMS'),
    ]);
    const admission = evaluateMachineAdmission(machine, capacity, claims, options.nowMs);
    admission.issues = [...new Set([...admission.issues, ...errors])];
    admission.eligible = admission.issues.length === 0;
    return [machine.id, admission];
  }));
  const admissions = new Map(rows);
  const boundedProvider = (lane, operation) => stageDeadline(
    () => options.conductorProvider(lane, operation),
    operation.kind.toUpperCase().replaceAll('-', '_'), options.stageTimeoutMs,
  );
  const candidates = await Promise.all(config.conductors.map((lane) => probeLane(
    config, lane, admissions.get(lane.machineId), { ...options, conductorProvider: boundedProvider },
  )));
  return {
    schemaVersion: 1,
    observedAt: new Date(options.nowMs).toISOString(),
    capability: options.capability,
    model: options.model ?? null,
    admissions: Object.fromEntries(admissions),
    candidates: rankCandidates(candidates),
  };
}

async function ensurePrivateDirectory(directory) {
  await fs.mkdir(directory, { recursive: true, mode: 0o700 });
  const stat = await fs.lstat(directory);
  if (!stat.isDirectory() || stat.isSymbolicLink()) throw new Error(`unsafe private directory: ${directory}`);
  await fs.chmod(directory, 0o700);
}

async function atomicPrivateWrite(target, value, options = {}) {
  const temporary = `${target}.${process.pid}.${crypto.randomUUID()}.tmp`;
  await fs.writeFile(temporary, `${JSON.stringify(value, null, 2)}\n`, { mode: 0o600, flag: 'wx' });
  await fs.chmod(temporary, 0o600);
  if (options.createOnly) {
    try {
      await fs.link(temporary, target);
      await fs.unlink(temporary);
      return;
    } catch (error) {
      await fs.unlink(temporary).catch(() => {});
      throw error;
    }
  }
  await fs.rename(temporary, target);
}

async function acquireLock(statePath, timeoutMs) {
  await ensurePrivateDirectory(statePath);
  const lockPath = path.join(statePath, 'dispatch.lock');
  const deadline = Date.now() + timeoutMs;
  while (true) {
    try {
      const handle = await fs.open(lockPath, 'wx', 0o600);
      await handle.writeFile(`${process.pid}\n`);
      await handle.close();
      return () => fs.unlink(lockPath);
    } catch (error) {
      if (error.code !== 'EEXIST') throw error;
      if (Date.now() >= deadline) throw new Error(`dispatch lock busy; inspect ${lockPath}`);
      await new Promise((resolve) => setTimeout(resolve, 50));
    }
  }
}

function intentDigest(workId, cwd) {
  return sha256(JSON.stringify({ workId, cwd }));
}

// Canonical workspace path: native realpath resolves symlinks and, on
// case-insensitive volumes, letter case. A missing tail (a claimed workspace
// that was since deleted) is kept below its deepest existing ancestor.
async function canonicalWorkspace(value) {
  let existing = path.resolve(value);
  const missing = [];
  while (true) {
    try {
      const real = await fs.realpath(existing);
      return { path: path.join(real, ...missing), existing: real, exists: missing.length === 0 };
    } catch (error) {
      const parent = path.dirname(existing);
      if (!['ENOENT', 'ENOTDIR'].includes(error.code) || parent === existing) throw error;
      missing.unshift(path.basename(existing));
      existing = parent;
    }
  }
}

// Device/inode of the deepest existing directory and each ancestor also
// matches spellings realpath keeps distinct, such as firmlinks and bind mounts.
async function workspaceIdentity(value) {
  const canonical = await canonicalWorkspace(value);
  const ancestry = [];
  for (let current = canonical.existing; ; current = path.dirname(current)) {
    const stat = await fs.stat(current, { bigint: true });
    ancestry.push(`${stat.dev}:${stat.ino}`);
    if (path.dirname(current) === current) break;
  }
  return { ...canonical, ancestry };
}

function isWithin(parent, child) {
  const relative = path.relative(parent, child);
  return relative === '' || (relative !== '..' && !relative.startsWith(`..${path.sep}`)
    && !path.isAbsolute(relative));
}

// Same directory, ancestor or descendant. Segment-aware, so sibling
// worktrees such as `repo` and `repo-other` stay independent.
function workspacesOverlap(left, right) {
  return isWithin(left.path, right.path) || isWithin(right.path, left.path)
    || (left.exists && right.ancestry.includes(left.ancestry[0]))
    || (right.exists && left.ancestry.includes(right.ancestry[0]));
}

async function createReceipt(config, receipt, lexicalCwd) {
  const directory = path.join(config.statePath, 'dispatch-receipts');
  const intents = path.join(config.statePath, 'intents');
  await ensurePrivateDirectory(directory);
  await ensurePrivateDirectory(intents);
  const receiptPath = path.join(directory, `${receipt.attemptedAt.replace(/[:.]/g, '-')}-${receipt.attemptId}.json`);
  const intentPath = path.join(intents, `${intentDigest(receipt.workId, receipt.cwd)}.json`);
  // Intents recorded before canonical workspace identity are keyed by the
  // lexical path and still block replay.
  for (const cwd of new Set([receipt.cwd, lexicalCwd])) {
    try {
      const existing = JSON.parse(await fs.readFile(path.join(intents, `${intentDigest(receipt.workId, cwd)}.json`), 'utf8'));
      throw new Error(`duplicate intent; do not retry: ${existing.receiptPath}`);
    } catch (error) {
      if (error.code !== 'ENOENT') throw error;
    }
  }
  await atomicPrivateWrite(receiptPath, receipt, { createOnly: true });
  try {
    await atomicPrivateWrite(intentPath, { schemaVersion: 1, receiptPath, state: receipt.state }, { createOnly: true });
  } catch (error) {
    await fs.unlink(receiptPath).catch(() => {});
    if (error.code === 'EEXIST') throw new Error('duplicate intent; do not retry');
    throw error;
  }
  return { receiptPath, intentPath };
}

async function updateReceipt(receiptPath, intentPath, receipt) {
  await atomicPrivateWrite(receiptPath, receipt);
  await atomicPrivateWrite(intentPath, { schemaVersion: 1, receiptPath, state: receipt.state });
}

function noEligibleMessage(result) {
  const evidence = result.candidates.map((candidate) => `${candidate.laneId}:${candidate.issues.join('+')}`).join(',');
  return `no eligible conductor${evidence ? `: ${evidence}` : ''}`;
}

export async function rank(configInput, options = {}) {
  const config = validateInstallConfig(configInput, { expectedBorgHome: configInput.borgHome });
  const nowMs = options.nowMs ?? Date.now();
  return snapshot(config, {
    nowMs,
    usageMaxAgeMs: options.usageMaxAgeMs ?? 15_000,
    stageTimeoutMs: options.stageTimeoutMs ?? config.routing.timeoutMs,
    capability: options.capability ?? 'tools',
    role: options.role ?? 'leaf',
    model: options.model,
    capacityProvider: options.capacityProvider ?? observeMachineCapacity,
    claimsProvider: options.claimsProvider ?? observeClaims,
    conductorProvider: options.conductorProvider
      ?? ((lane, operation) => nativeConductorProvider(lane, operation, config.routing.timeoutMs)),
  });
}

// Work IDs are reserved across every claim source and machine. Workspace
// overlap is checked against every claim naming a workspace, resolved on this
// router's filesystem, where dispatch validates cwd; paths reported for
// different machines are never assumed disjoint. A claim without cwd reserves
// only its work ID; a malformed or unresolvable cwd refuses dispatch.
async function assertClaimsAvailable(claimSets, receipt, workspace) {
  const active = claimSets.flatMap((claims) => claims?.active ?? [])
    .filter((claim) => claim.attemptId !== receipt.attemptId);
  if (active.some((claim) => claim.workId === receipt.workId)) throw new Error('WORK_ID_CLAIM_CONFLICT');
  const identities = new Map();
  for (const claim of active) {
    if (claim.cwd === undefined || claim.cwd === null) continue;
    if (typeof claim.cwd !== 'string' || !path.isAbsolute(claim.cwd)) throw new Error('WORKSPACE_CLAIM_UNRESOLVED');
    if (!identities.has(claim.cwd)) {
      try {
        identities.set(claim.cwd, await workspaceIdentity(claim.cwd));
      } catch {
        throw new Error('WORKSPACE_CLAIM_UNRESOLVED');
      }
    }
    if (workspacesOverlap(workspace, identities.get(claim.cwd))) throw new Error('WORKSPACE_CLAIM_CONFLICT');
  }
}

export async function inspectDispatch(configInput, options = {}) {
  const config = validateInstallConfig(configInput, { expectedBorgHome: configInput.borgHome });
  if (typeof options.workId !== 'string' || !options.workId.trim() || !path.isAbsolute(options.cwd ?? '')) {
    throw new Error('workId and absolute cwd are required');
  }
  const workId = options.workId.trim();
  const lexicalCwd = path.resolve(options.cwd);
  const { path: cwd } = await canonicalWorkspace(lexicalCwd);
  let intent = null;
  for (const candidate of new Set([cwd, lexicalCwd])) {
    intent = await readPrivateJsonRecord(path.join(config.statePath, 'intents', `${intentDigest(workId, candidate)}.json`));
    if (intent !== null) break;
  }
  if (intent === null) return { found: false, state: 'NOT_FOUND', noStartProven: false, completionVerified: false };
  const receiptPath = intent.receiptPath;
  if (typeof receiptPath !== 'string' || path.dirname(receiptPath) !== path.join(config.statePath, 'dispatch-receipts')
      || !path.basename(receiptPath).endsWith('.json')) throw new Error('UNSAFE_RECEIPT_REFERENCE');
  const receipt = await readPrivateJsonRecord(receiptPath);
  if (!receipt || receipt.workId !== workId || (receipt.cwd !== cwd && receipt.cwd !== lexicalCwd)) {
    throw new Error('DISPATCH_RECEIPT_MISMATCH');
  }
  return { found: true, receipt, receiptPath,
    noStartProven: receipt.state === 'PRE_START_FAILED' && receipt.nativeStartAttempted === false,
    completionVerified: false };
}

export async function dispatch(configInput, options = {}) {
  const config = validateInstallConfig(configInput, { expectedBorgHome: configInput.borgHome });
  if (typeof options.workId !== 'string' || !options.workId.trim()) throw new Error('workId is required for duplicate-safe dispatch');
  if (typeof options.prompt !== 'string' || !options.prompt.trim()) throw new Error('prompt is required');
  if (!path.isAbsolute(options.cwd ?? '')) throw new Error('absolute cwd is required');
  const lexicalCwd = path.resolve(options.cwd);
  const stat = await fs.stat(lexicalCwd);
  if (!stat.isDirectory()) throw new Error('cwd must be a directory');
  // Receipt, intent, claim checks and the native thread share one filesystem
  // identity, so an alias or nested path is not a new workspace.
  const workspace = await workspaceIdentity(lexicalCwd);
  const { path: cwd } = workspace;
  const stageTimeoutMs = options.stageTimeoutMs ?? config.routing.timeoutMs;
  if (!Number.isInteger(stageTimeoutMs) || stageTimeoutMs < 1 || stageTimeoutMs > 60000) throw new Error('STAGE_TIMEOUT_INVALID');
  const release = await acquireLock(config.statePath, config.routing.lockTimeoutMs);
  let receipt; let paths; let nativeAttempted = false;
  const clock = () => new Date(options.nowMs ?? Date.now()).toISOString();
  const progress = async (phase, changes = {}) => {
    receipt = { ...receipt, ...changes, phase,
      events: [...receipt.events, { phase, at: clock() }].slice(-16) };
    await updateReceipt(paths.receiptPath, paths.intentPath, receipt);
  };
  try {
    const common = {
      ...options, stageTimeoutMs,
      capability: options.capability ?? 'tools', role: options.role ?? 'leaf',
      capacityProvider: options.capacityProvider ?? observeMachineCapacity,
      claimsProvider: options.claimsProvider ?? observeClaims,
      conductorProvider: options.conductorProvider
        ?? ((lane, operation) => nativeConductorProvider(lane, operation, config.routing.timeoutMs)),
    };
    receipt = {
      schemaVersion: 1, attemptId: crypto.randomUUID(), workId: options.workId.trim(), cwd,
      laneId: null, machineId: null, state: 'ATTEMPTING', phase: 'PREPARING',
      attemptedAt: clock(), dispatchedAt: null, threadId: null, turnId: null,
      errorClass: null, nativeStartAttempted: false, stageTimeoutMs,
      promptSha256: sha256(options.prompt), events: [{ phase: 'PREPARING', at: clock() }],
    };
    // Persist the work intent BEFORE any admission scans or native lifecycle call.
    paths = await createReceipt(config, receipt, lexicalCwd);
    await progress('ADMISSION');
    const preliminary = await rank(config, common);
    await progress('RECHECK');
    const final = await rank(config, { ...common, nowMs: options.recheckNowMs ?? options.nowMs ?? Date.now() });
    const selected = final.candidates.find((candidate) => candidate.eligible);
    if (!selected) throw new Error(noEligibleMessage(final));
    // This router's own submitted and uncertain receipts are claims whichever
    // collector each machine uses; a failed ledger scan refuses dispatch.
    const localClaims = await observeLocalReceiptClaims(config.statePath);
    await assertClaimsAvailable([
      ...Object.values(final.admissions).map((admission) => admission.claims), localClaims,
    ], receipt, workspace);
    const lane = config.conductors.find((candidate) => candidate.id === selected.laneId);
    await progress('THREAD_START_PENDING', {
      laneId: lane.id, machineId: lane.machineId, nativeStartAttempted: true,
      remainingPercent: selected.remainingPercent, resetAt: selected.resetAt,
      preliminaryLeader: preliminary.candidates.find((candidate) => candidate.eligible)?.laneId ?? null,
      finalLeader: selected.laneId,
    });
    nativeAttempted = true;
    let thread;
    try {
      thread = await stageDeadline(() => common.conductorProvider(lane, {
        kind: 'thread-start', role: common.role,
        body: { cwd, role: common.role, approvalPolicy: 'never', sandbox: options.sandbox ?? 'danger-full-access',
          ...(options.model ? { model: options.model } : {}),
          ...(options.instructions ? { instructions: options.instructions } : {}) },
      }), 'THREAD_START', stageTimeoutMs);
    } catch (error) {
      await progress('THREAD_START', { state: 'UNKNOWN_DO_NOT_RETRY', errorClass: error.code || error.name || 'Error' });
      throw new Error(`thread start outcome unknown; do not retry: ${paths.receiptPath}`);
    }
    if (!thread?.threadId || typeof thread.threadId !== 'string') {
      await progress('THREAD_START_RESPONSE', { state: 'UNKNOWN_DO_NOT_RETRY', errorClass: 'MISSING_THREAD_ID' });
      throw new Error(`thread start returned no ID; do not retry: ${paths.receiptPath}`);
    }
    await progress('TURN_START_PENDING', { state: 'THREAD_STARTED', threadId: thread.threadId });
    let turn;
    try {
      turn = await stageDeadline(() => common.conductorProvider(lane, {
        kind: 'turn-start', body: { threadId: thread.threadId, text: options.prompt,
          ...(options.model ? { model: options.model } : {}),
          ...(options.effort ? { effort: options.effort } : {}) },
      }), 'TURN_START', stageTimeoutMs);
    } catch (error) {
      await progress('TURN_START', { state: 'STARTED_TURN_UNKNOWN', errorClass: error.code || error.name || 'Error' });
      throw new Error(`thread exists but turn outcome unknown; do not retry: ${paths.receiptPath}`);
    }
    if (!turn?.turnId || typeof turn.turnId !== 'string') {
      await progress('TURN_START_RESPONSE', { state: 'STARTED_TURN_UNKNOWN', errorClass: 'MISSING_TURN_ID' });
      throw new Error(`turn start returned no ID; do not retry: ${paths.receiptPath}`);
    }
    await progress('TURN_STARTED', { state: 'DISPATCHED', dispatchedAt: clock(), turnId: turn.turnId });
    return { receipt, receiptPath: paths.receiptPath, snapshot: final };
  } catch (error) {
    if (paths && !nativeAttempted) {
      await progress(receipt.phase, { state: 'PRE_START_FAILED', nativeStartAttempted: false, errorClass: error.code || error.name || 'Error' });
      error.receiptPath = paths.receiptPath;
      error.message += `; receipt: ${paths.receiptPath}`;
    }
    throw error;
  } finally { await release(); }
}
