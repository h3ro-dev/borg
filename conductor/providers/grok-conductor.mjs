#!/usr/bin/env node
// Grok peer conductor — local HTTP control plane for orchestration.
// Same curl surface as Codex conductor.mjs, different runtime.
// Binds 127.0.0.1 only. Not a paid Codex account lane.
// Mid-job talk: interrupt the grok prompt-file child and --resume the same session.
// ACP/leader inject is not available (no leader.sock; prompt-file child has no stdin).

import { spawn, spawnSync } from 'node:child_process';
import crypto from 'node:crypto';
import fs from 'node:fs';
import http from 'node:http';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

export const HOST = '127.0.0.1';
export const DEFAULT_PORT = 4770;
export const PROTOCOL = 'grok-conductor-http-v1';
export const STEER_PATH = 'interrupt-resume';
export const ZOMBIE_IDLE_MS = 15 * 60 * 1000;
export const STEER_INTERRUPT_MS = 1800;
export const MEMORY_MAX_CONTEXT_BYTES = 10000;
export const MEMORY_HOOK_TIMEOUT_MS = 2000;
export const MEMORY_HOOK_TERM_GRACE_MS = 50;
export const MEMORY_DELIVERY_CHANNEL = 'client_prompt';
export const MEMORY_AUTHORITY_NOTICE = 'Mem0 candidate context only. Current user instructions and AGENTS.md have priority. Repository state, native providers, and live evidence have priority over memory. Verify every candidate before use; memory is never completion proof.';
const EVENT_RING = 2000;
const TEXT_CAP = 20000;
export const MAX_BODY_BYTES = 64 * 1024;
const SAFE_ID = /^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$/;
const MEMORY_OPEN = '<system-reminder>';
const MEMORY_CLOSE = '</system-reminder>';
const PROTECTED_POLICY = `UNPROTECTED ACTION POLICY
This envelope declares protectedAction:false. Do not send, spend, publish, deploy, merge, change credentials or accounts, alter DNS, delete unique data, or reboot. Stop and report BLOCKED_PROTECTED_ACTION if the work requires one of those actions.`;

function nowIso(ms) {
  return new Date(ms ?? Date.now()).toISOString();
}

function json(res, code, obj) {
  const body = JSON.stringify(obj, null, 1);
  res.writeHead(code, { 'content-type': 'application/json' });
  res.end(body);
}

async function readBody(req) {
  const declared = Number(req.headers['content-length'] || 0);
  if (Number.isFinite(declared) && declared > MAX_BODY_BYTES) {
    throw Object.assign(new Error('request body exceeds 65536 bytes'), { statusCode: 413 });
  }
  const chunks = [];
  let bytes = 0;
  for await (const chunk of req) {
    const value = Buffer.from(chunk);
    bytes += value.length;
    if (bytes > MAX_BODY_BYTES) {
      throw Object.assign(new Error('request body exceeds 65536 bytes'), { statusCode: 413 });
    }
    chunks.push(value);
  }
  const data = Buffer.concat(chunks).toString('utf8');
  if (!data) return {};
  return JSON.parse(data);
}

function truncate(value) {
  if (typeof value !== 'string') return value;
  if (value.length <= TEXT_CAP) return value;
  return `${value.slice(0, TEXT_CAP)}…`;
}

export function sha256(value) {
  return crypto.createHash('sha256').update(value).digest('hex');
}

function countOccurrences(value, needle) {
  return value.split(needle).length - 1;
}

export function orderedMemoryIdSetSha256(context) {
  const ids = [];
  const pattern = /\[id=([^;\]]+)/g;
  for (const match of String(context || '').matchAll(pattern)) ids.push(match[1]);
  return sha256(JSON.stringify(ids));
}

export function isAcceptedMemoryContext(context) {
  if (typeof context !== 'string' || !context) return false;
  if (Buffer.byteLength(context, 'utf8') > MEMORY_MAX_CONTEXT_BYTES) return false;
  if (/[\u0000-\u0008\u000B\u000C\u000E-\u001F\u007F]/.test(context)) return false;
  if (!context.startsWith(`${MEMORY_OPEN}\n`) || !context.endsWith(`${MEMORY_CLOSE}\n`)) return false;
  if (countOccurrences(context, MEMORY_OPEN) !== 1 || countOccurrences(context, MEMORY_CLOSE) !== 1) return false;
  if (countOccurrences(context, MEMORY_AUTHORITY_NOTICE) !== 1) return false;
  const expectedPrefix = `${MEMORY_OPEN}\n${MEMORY_AUTHORITY_NOTICE}\nScope: project=`;
  if (!context.startsWith(expectedPrefix)) return false;
  const scopeLine = context.split('\n')[2] || '';
  if (!/^Scope: project=[^;\n]+; machine=[^\n]+\.$/.test(scopeLine)) return false;
  if (countOccurrences(context, '\nMemory candidates:\n') !== 1) return false;
  const closeSuffix = `${MEMORY_CLOSE}\n`;
  const candidateSection = context.slice(context.indexOf('\nMemory candidates:\n') + '\nMemory candidates:\n'.length, -closeSuffix.length);
  if (candidateSection.split('\n').some((line) => line && !line.startsWith('- '))) return false;
  return true;
}

function decodeUtf8(bytes) {
  return new TextDecoder('utf-8', { fatal: true }).decode(bytes);
}

function isProviderFailureEvent(parsed) {
  const type = String(parsed?.type || '').toLowerCase();
  return type === 'error' || type === 'provider_error' || type === 'failure' || type === 'failed';
}

function hasMeaningfulContent(value, depth = 0) {
  if (typeof value === 'string') return value.trim().length > 0;
  if (!value || typeof value !== 'object' || depth >= 3) return false;
  if (Array.isArray(value)) return value.some((item) => hasMeaningfulContent(item, depth + 1));
  return ['text', 'content', 'delta', 'value', 'data'].some((key) => hasMeaningfulContent(value[key], depth + 1));
}

function hasToolCallPayload(parsed) {
  const candidates = [
    parsed,
    parsed?.toolCall,
    parsed?.tool_call,
    parsed?.functionCall,
    parsed?.function_call,
    parsed?.request,
    parsed?.payload,
    parsed?.data,
  ];
  return candidates.some((value) => {
    if (!value || typeof value !== 'object' || Array.isArray(value)) return false;
    return hasMeaningfulContent(value.name)
      || hasMeaningfulContent(value.tool)
      || hasMeaningfulContent(value.arguments)
      || hasMeaningfulContent(value.input);
  });
}

function isModelContentEvent(parsed) {
  const type = String(parsed?.type || '').toLowerCase();
  if (type === 'reasoning' || type === 'assistant' || type === 'text' || type === 'thought') {
    return hasMeaningfulContent(parsed?.data)
      || hasMeaningfulContent(parsed?.text)
      || hasMeaningfulContent(parsed?.content)
      || hasMeaningfulContent(parsed?.delta)
      || hasMeaningfulContent(parsed?.message)
      || hasMeaningfulContent(parsed?.reasoning)
      || hasMeaningfulContent(parsed?.thought)
      || hasMeaningfulContent(parsed?.output);
  }
  return type === 'tool' || type === 'tool_call' || type === 'tool_request' || type === 'tool_use' || type === 'function_call'
    ? hasToolCallPayload(parsed)
    : false;
}

function writePrivateFileAtomic(file, text) {
  const temporary = `${file}.${process.pid}.${crypto.randomUUID()}.tmp`;
  try {
    fs.writeFileSync(temporary, text, { mode: 0o600 });
    fs.renameSync(temporary, file);
  } catch (error) {
    try { fs.unlinkSync(temporary); } catch { /* keep the original error */ }
    throw error;
  }
}

export function buildMemoryHookEnvironment({
  parentEnv = process.env,
  machine,
  tokenFile,
  endpoint,
  endpointFile,
  extraEnv = {},
} = {}) {
  const env = {
    HOME: parentEnv.HOME || os.homedir(),
    PATH: `/opt/homebrew/bin:${parentEnv.PATH || '/usr/bin:/bin:/usr/sbin:/sbin'}`,
    LANG: parentEnv.LANG || 'en_US.UTF-8',
    TMPDIR: parentEnv.TMPDIR || '/tmp',
    MEM0_HARNESS: 'grok',
  };
  if (machine) env.MEM0_MACHINE = machine;
  if (tokenFile) env.MEM0_FLEET_TOKEN_FILE = tokenFile;
  if (endpoint) env.MEM0_FLEET_ENDPOINT = endpoint;
  if (endpointFile) env.MEM0_FLEET_ENDPOINT_FILE = endpointFile;
  for (const [key, value] of Object.entries(extraEnv || {})) {
    if (/^FAKE_MEM0_[A-Z0-9_]+$/.test(key) && value !== undefined && value !== null) {
      env[key] = String(value);
    }
  }
  return env;
}

export function runMemoryHook({
  hookPath,
  mode,
  payload,
  env = buildMemoryHookEnvironment(),
  timeoutMs = MEMORY_HOOK_TIMEOUT_MS,
  spawnFn = spawn,
} = {}) {
  if (!hookPath) return Promise.resolve({ status: 'disabled', stdout: Buffer.alloc(0), exitCode: null, signal: null });
  return new Promise((resolve) => {
    let child;
    let settled = false;
    let timedOut = false;
    let oversized = false;
    let forcedFailure = false;
    let timeoutTimer;
    let terminationTimer;
    let stdoutBytes = 0;
    const stdoutChunks = [];
    const settle = (result) => {
      if (settled) return;
      settled = true;
      clearTimeout(timeoutTimer);
      clearTimeout(terminationTimer);
      resolve({ ...result, stdout: Buffer.concat(stdoutChunks) });
    };
    const destroyChildStreams = () => {
      for (const stream of [child?.stdin, child?.stdout, child?.stderr]) {
        try { stream?.destroy?.(); } catch { /* already closed */ }
      }
    };
    const terminate = (reason) => {
      if (settled) return;
      if (reason === 'timeout') timedOut = true;
      else if (reason === 'oversize') oversized = true;
      else forcedFailure = true;
      if (terminationTimer) return;
      try { child.kill('SIGTERM'); } catch { /* already gone */ }
      if (settled) return;
      terminationTimer = setTimeout(() => {
        if (settled) return;
        try { child.kill('SIGKILL'); } catch { /* already gone */ }
        destroyChildStreams();
        const status = timedOut ? 'timeout' : (oversized ? 'oversize' : 'error');
        settle({ status, exitCode: null, signal: 'SIGKILL' });
      }, MEMORY_HOOK_TERM_GRACE_MS);
    };
    try {
      child = spawnFn(hookPath, [mode], {
        cwd: os.homedir(),
        env,
        stdio: ['pipe', 'pipe', 'pipe'],
      });
    } catch {
      settle({ status: 'error', exitCode: null, signal: null });
      return;
    }
    timeoutTimer = setTimeout(() => terminate('timeout'), Math.max(1, Number(timeoutMs) || MEMORY_HOOK_TIMEOUT_MS));
    child.stdout?.on('data', (chunk) => {
      if (oversized) return;
      const bytes = Buffer.from(chunk);
      stdoutBytes += bytes.length;
      if (stdoutBytes > MEMORY_MAX_CONTEXT_BYTES) {
        oversized = true;
        stdoutChunks.push(bytes.subarray(0, Math.max(0, MEMORY_MAX_CONTEXT_BYTES + 1 - (stdoutBytes - bytes.length))));
        terminate('oversize');
        return;
      }
      stdoutChunks.push(bytes);
    });
    child.stderr?.on('data', () => { /* stderr is deliberately fail-open and never logged */ });
    child.on('error', () => {
      const status = timedOut ? 'timeout' : (oversized ? 'oversize' : 'error');
      settle({ status, exitCode: null, signal: null });
    });
    child.on('close', (code, signal) => {
      const status = timedOut ? 'timeout' : (oversized ? 'oversize' : (forcedFailure ? 'error' : (code === 0 ? 'ok' : 'error')));
      settle({ status, exitCode: code, signal: signal || null });
    });
    try {
      child.stdin?.end(JSON.stringify(payload));
    } catch {
      terminate('error');
    }
  });
}

function assertAbsoluteDir(cwd) {
  if (typeof cwd !== 'string' || !cwd.trim()) {
    throw Object.assign(new Error('cwd is required'), { statusCode: 400 });
  }
  const resolved = path.resolve(cwd);
  if (!path.isAbsolute(cwd) || cwd !== path.normalize(cwd) || resolved !== cwd) {
    throw Object.assign(new Error('cwd must be a normalized absolute path'), { statusCode: 400 });
  }
  let stat;
  try { stat = fs.statSync(resolved); } catch {
    throw Object.assign(new Error('cwd must exist'), { statusCode: 400 });
  }
  if (!stat.isDirectory()) throw Object.assign(new Error('cwd must be a directory'), { statusCode: 400 });
  return resolved;
}

function assertEnvelope(body, allowed) {
  if (!body || typeof body !== 'object' || Array.isArray(body)) {
    throw Object.assign(new Error('request body must be a JSON object'), { statusCode: 400 });
  }
  for (const key of Object.keys(body)) {
    if (!allowed.has(key)) throw Object.assign(new Error(`unknown field: ${key}`), { statusCode: 400 });
  }
  if (body.protectedAction !== false) {
    throw Object.assign(new Error('protectedAction:false is required'), {
      statusCode: body.protectedAction === true ? 403 : 400,
      code: 'PROTECTED_ACTION_REJECTED',
    });
  }
  for (const key of ['threadId', 'workId', 'model']) {
    if (body[key] !== undefined && typeof body[key] !== 'string') {
      throw Object.assign(new Error(`${key} must be a string`), { statusCode: 400 });
    }
  }
  if (body.workId && !SAFE_ID.test(body.workId)) {
    throw Object.assign(new Error('workId has an invalid format'), { statusCode: 400 });
  }
}

export function buildChildEnvironment({ parentEnv = process.env, grokHome, grokBin, memoryBadgePath }) {
  const home = parentEnv.HOME || os.homedir();
  const env = {
    HOME: home,
    PATH: `${path.dirname(grokBin)}:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin`,
    LANG: parentEnv.LANG || 'en_US.UTF-8',
    TMPDIR: parentEnv.TMPDIR || '/tmp',
    GROK_HOME: grokHome,
    GROK_DISABLE_AUTOUPDATER: '1',
  };
  if (memoryBadgePath) env.MEM0_TOKEN_FILE = memoryBadgePath;
  return env;
}

export function inspectReadiness({ grokBin, expectedVersion, readinessMarkerPath }) {
  const binaryPresent = fs.existsSync(grokBin);
  let binaryVersion = null;
  if (binaryPresent) {
    const check = spawnSync(grokBin, ['--version'], { encoding: 'utf8', timeout: 5000, env: buildChildEnvironment({ grokHome: path.join(os.homedir(), '.grok'), grokBin }) });
    if (check.status === 0) binaryVersion = String(check.stdout || check.stderr).trim() || null;
  }
  let marker = null;
  try { marker = JSON.parse(fs.readFileSync(readinessMarkerPath, 'utf8')); } catch { /* not proved */ }
  const versionMatches = Boolean(binaryVersion && expectedVersion && binaryVersion.includes(expectedVersion));
  const loginState = versionMatches && marker?.binaryVersion === binaryVersion && marker?.expectedVersion === expectedVersion
    ? 'proved' : 'not_proved';
  return { binaryPresent, binaryVersion, expectedVersion, loginState, readyForDispatch: binaryPresent && versionMatches && loginState === 'proved' };
}

export function extractSessionId(parsed) {
  if (!parsed || typeof parsed !== 'object') return null;
  for (const key of ['sessionId', 'session_id']) {
    const value = parsed[key];
    if (typeof value === 'string' && value.trim()) return value.trim();
  }
  if (parsed.params && typeof parsed.params === 'object') {
    for (const key of ['sessionId', 'session_id']) {
      const value = parsed.params[key];
      if (typeof value === 'string' && value.trim()) return value.trim();
    }
  }
  return null;
}

export function sessionDirFor(grokHome, cwd, sessionId) {
  if (!grokHome || !cwd || !sessionId) return null;
  return path.join(grokHome, 'sessions', encodeURIComponent(cwd), sessionId);
}

export function defaultSpawnGrok(options) {
  const args = [
    '--prompt-file', options.promptFile,
    '--cwd', options.cwd,
    '--always-approve',
    '--output-format', 'streaming-json',
    '--no-auto-update',
  ];
  if (options.model) args.push('-m', options.model);
  if (options.grokSessionId) args.push('--resume', options.grokSessionId);
  else if (options.newSessionId) args.push('--session-id', options.newSessionId);
  const child = spawn(options.grokBin, args, {
    cwd: options.cwd,
    env: buildChildEnvironment({
      grokHome: options.grokHome,
      grokBin: options.grokBin,
      memoryBadgePath: options.memoryBadgePath,
    }),
    stdio: ['ignore', 'pipe', 'pipe'],
  });
  return child;
}

export function loadConfig(configPath) {
  const raw = JSON.parse(fs.readFileSync(configPath, 'utf8'));
  const productConfig = raw.providers?.grok ? raw : null;
  const source = productConfig ? raw.providers.grok : raw;
  if (source.enabled === false) throw new Error('Grok provider is disabled');
  const host = source.host ?? HOST;
  if (host !== HOST || (source.bindHostLocked !== undefined && source.bindHostLocked !== HOST)) {
    throw new Error('grok-conductor host is locked to 127.0.0.1');
  }
  if (!productConfig) return raw;
  if (typeof raw.borgHome !== 'string' || !path.isAbsolute(raw.borgHome)) {
    throw new Error('BORG_HOME is required in product config');
  }
  return {
    ...source,
    host: HOST,
    bindHostLocked: HOST,
    laneId: source.laneId || 'grok',
    stateDirectory: source.statePath || path.join(raw.borgHome, 'providers/grok/state'),
    seatRulesPath: source.seatRulesPath || path.join(raw.borgHome, 'policies/SEAT-RULES.md'),
    agentLaunchDirectory: source.agentLaunchDirectory
      || path.join(raw.borgHome, 'private/provider-launches'),
  };
}

function ensureDir(dir) {
  fs.mkdirSync(dir, { recursive: true, mode: 0o700 });
}

function latestCwdActivityMs(cwd) {
  let latest = 0;
  try {
    latest = fs.statSync(cwd).mtimeMs;
  } catch {
    return 0;
  }
  let entries;
  try {
    entries = fs.readdirSync(cwd, { withFileTypes: true });
  } catch {
    return latest;
  }
  for (const entry of entries) {
    if (entry.name === 'node_modules' || entry.name === '.git' || entry.name === '.venv') continue;
    try {
      const st = fs.statSync(path.join(cwd, entry.name));
      if (st.mtimeMs > latest) latest = st.mtimeMs;
    } catch {
      // skip unreadable
    }
  }
  return latest;
}

export function createConductor(options = {}) {
  const config = options.config || {};
  const port = Number(options.port ?? config.port ?? DEFAULT_PORT);
  const home = options.home || os.homedir();
  const grokBin = options.grokBin || config.grokBin || path.join(home, '.grok', 'bin', 'grok');
  const grokHome = options.grokHome || config.grokHome || path.join(home, '.grok');
  const stateDirectory = options.stateDirectory || config.stateDirectory || path.join(home, '.local', 'state', 'grok-conductor');
  const maxConcurrentTurns = Number(options.maxConcurrentTurns ?? config.maxConcurrentTurns ?? 4);
  const spawnGrok = options.spawnGrok || defaultSpawnGrok;
  const zombieIdleMs = Number(options.zombieIdleMs ?? ZOMBIE_IDLE_MS);
  const steerInterruptMs = Number(options.steerInterruptMs ?? STEER_INTERRUPT_MS);
  const nowFn = options.nowFn || Date.now;
  const cwdActivityFn = options.cwdActivityFn || latestCwdActivityMs;
  const expectedVersion = options.expectedVersion || config.expectedVersion || null;
  const readinessMarkerPath = options.readinessMarkerPath || config.readinessMarkerPath || path.join(stateDirectory, 'readiness.json');
  const readinessProvider = options.readinessProvider || (() => inspectReadiness({ grokBin, expectedVersion, readinessMarkerPath }));
  const memoryBadgePath = options.memoryBadgePath || config.memoryBadgePath || path.join(stateDirectory, 'mem0.token');
  const memoryHookPath = options.memoryHookPath ?? config.memoryHookPath ?? null;
  const memoryMachine = options.memoryMachine ?? config.memoryMachine ?? process.env.MEM0_MACHINE ?? null;
  const memoryTokenFile = options.memoryTokenFile ?? config.memoryTokenFile ?? process.env.MEM0_FLEET_TOKEN_FILE ?? memoryBadgePath;
  const memoryEndpoint = options.memoryEndpoint ?? config.memoryEndpoint ?? process.env.MEM0_FLEET_ENDPOINT ?? null;
  const memoryEndpointFile = options.memoryEndpointFile ?? config.memoryEndpointFile ?? process.env.MEM0_FLEET_ENDPOINT_FILE ?? null;
  const memoryHookTimeoutMs = Number(options.memoryHookTimeoutMs ?? config.memoryHookTimeoutMs ?? MEMORY_HOOK_TIMEOUT_MS);
  const memoryHookExtraEnv = options.memoryHookExtraEnv || {};
  const seatRulesText = options.seatRulesText ?? (() => {
    const rulesPath = config.seatRulesPath || path.join(path.dirname(fileURLToPath(import.meta.url)), 'cards', 'SEAT-RULES.md');
    return fs.readFileSync(rulesPath, 'utf8');
  })();
  const threadsPath = path.join(stateDirectory, 'threads.json');
  const logsDir = path.join(stateDirectory, 'logs');
  ensureDir(stateDirectory);
  ensureDir(logsDir);
  const promptsDirectory = path.join(stateDirectory, 'prompts');
  ensureDir(promptsDirectory);

  function sweepPrivatePromptFiles() {
    let entries;
    try { entries = fs.readdirSync(promptsDirectory, { withFileTypes: true }); } catch { return; }
    for (const entry of entries) {
      if (!entry.isFile()) continue;
      if (!/^[0-9a-f-]{36}\.txt(?:\.[0-9]+\.[0-9a-f-]{36})?\.tmp$|^[0-9a-f-]{36}\.txt$/i.test(entry.name)) continue;
      try { fs.unlinkSync(path.join(promptsDirectory, entry.name)); } catch { /* best-effort crash cleanup */ }
    }
  }
  sweepPrivatePromptFiles();

  const threads = new Map();
  const pendingRestartInterrupts = [];
  if (fs.existsSync(threadsPath)) {
    try {
      const saved = JSON.parse(fs.readFileSync(threadsPath, 'utf8'));
      for (const [id, rec] of Object.entries(saved.threads || {})) {
        const wasRunning = rec.lastTurnStatus === 'running';
        threads.set(id, {
          ...rec,
          lastTurnStatus: wasRunning ? 'interrupted' : rec.lastTurnStatus,
        });
        if (wasRunning) pendingRestartInterrupts.push(id);
      }
    } catch {
      // start empty if the store is unreadable
    }
  }

  const events = [];
  let seq = 0;
  const children = new Map();
  const pendingTurns = new Map();

  function memoryHookEnabled() {
    return Boolean(memoryHookPath);
  }

  async function prepareMemory({ sessionId, turnId, cwd, prompt, threadId }) {
    const empty = Buffer.alloc(0);
    if (!memoryHookEnabled()) {
      return {
        context: '',
        rawStdout: empty,
        contextSha256: sha256(empty),
        orderedIdSetSha256: orderedMemoryIdSetSha256(''),
        status: 'disabled',
        commitEligible: false,
      };
    }
    const startedAt = Date.now();
    const payload = { sessionId, turnId, cwd, prompt, deliveryChannel: MEMORY_DELIVERY_CHANNEL };
    let result;
    try {
      result = await runMemoryHook({
        hookPath: memoryHookPath,
        mode: 'prepare',
        payload,
        env: buildMemoryHookEnvironment({
          machine: memoryMachine,
          tokenFile: memoryTokenFile,
          endpoint: memoryEndpoint,
          endpointFile: memoryEndpointFile,
          extraEnv: memoryHookExtraEnv,
        }),
        timeoutMs: memoryHookTimeoutMs,
      });
    } catch {
      result = { status: 'error', stdout: empty };
    }
    const rawStdout = Buffer.isBuffer(result.stdout) ? result.stdout : Buffer.from(result.stdout || '');
    const contextSha256 = sha256(rawStdout);
    let context = '';
    let status = result.status === 'ok' ? 'empty' : 'fail-open';
    let commitEligible = result.status === 'ok';
    if (result.status === 'ok' && rawStdout.length) {
      try {
        context = decodeUtf8(rawStdout);
        if (isAcceptedMemoryContext(context)) {
          status = 'accepted';
        } else {
          context = '';
          status = 'malformed';
          commitEligible = false;
        }
      } catch {
        status = 'malformed';
        commitEligible = false;
      }
    }
    record('memory/prepare', {
      threadId,
      turnId,
      sessionId,
      status,
      accepted: status === 'accepted',
      contextBytes: rawStdout.length,
      contextSha256,
      orderedIdSetSha256: orderedMemoryIdSetSha256(context),
      elapsedMs: Math.max(0, Date.now() - startedAt),
    });
    return {
      context,
      rawStdout,
      contextSha256,
      orderedIdSetSha256: orderedMemoryIdSetSha256(context),
      status,
      commitEligible,
    };
  }

  async function commitMemory({ sessionId, turnId, preparation, threadId }) {
    if (!preparation?.commitEligible) return { status: 'skipped' };
    const startedAt = Date.now();
    const payload = {
      sessionId,
      turnId,
      deliveryChannel: MEMORY_DELIVERY_CHANNEL,
      contextSha256: preparation.contextSha256,
    };
    let result;
    try {
      result = await runMemoryHook({
        hookPath: memoryHookPath,
        mode: 'commit',
        payload,
        env: buildMemoryHookEnvironment({
          machine: memoryMachine,
          tokenFile: memoryTokenFile,
          endpoint: memoryEndpoint,
          endpointFile: memoryEndpointFile,
          extraEnv: memoryHookExtraEnv,
        }),
        timeoutMs: memoryHookTimeoutMs,
      });
    } catch {
      result = { status: 'error', stdout: Buffer.alloc(0) };
    }
    record('memory/commit', {
      threadId,
      turnId,
      sessionId,
      status: result.status,
      contextBytes: preparation.rawStdout.length,
      contextSha256: preparation.contextSha256,
      orderedIdSetSha256: preparation.orderedIdSetSha256,
      elapsedMs: Math.max(0, Date.now() - startedAt),
    });
    return { status: result.status };
  }

  function writeWorkStatus(workId, extra) {
    if (!workId) return;
    const outbox = path.join(stateDirectory, 'outbox', `${workId}.json`);
    let prior = {};
    try { prior = JSON.parse(fs.readFileSync(outbox, 'utf8')); } catch { /* new */ }
    const next = {
      ...prior,
      workId,
      updatedAt: nowIso(nowFn()),
      ...extra,
    };
    ensureDir(path.join(stateDirectory, 'outbox'));
    fs.writeFileSync(outbox, JSON.stringify(next, null, 2), { mode: 0o600 });
    const launchRoot = config.agentLaunchDirectory || path.join(home, '.local', 'state', 'agent-launches');
    try {
      ensureDir(path.join(launchRoot, extra.state === 'RUNNING' ? 'active' : 'done'));
      fs.writeFileSync(
        path.join(launchRoot, extra.state === 'RUNNING' ? 'active' : 'done', `${workId}.json`),
        JSON.stringify(next, null, 2),
        { mode: 0o600 },
      );
      const unread = path.join(launchRoot, 'unread', `${workId}.json`);
      if (extra.state !== 'RUNNING' && fs.existsSync(unread)) fs.unlinkSync(unread);
      if (extra.state !== 'RUNNING') {
        const active = path.join(launchRoot, 'active', `${workId}.json`);
        if (fs.existsSync(active) && extra.state !== 'RUNNING') {
          try { fs.unlinkSync(active); } catch { /* keep */ }
        }
      }
    } catch { /* launch dir optional */ }
  }

  function writeReadinessRejection(body, workId) {
    const receiptId = crypto.randomUUID();
    const receipt = {
      receiptId,
      state: 'FAILED',
      terminal: true,
      errorCode: 'GROK_NOT_READY',
      workId: workId || null,
      threadId: typeof body?.threadId === 'string' ? body.threadId : null,
      createdAt: nowIso(nowFn()),
    };
    const dir = path.join(stateDirectory, 'receipts', 'readiness-rejections');
    ensureDir(dir);
    fs.writeFileSync(path.join(dir, `${receiptId}.json`), JSON.stringify(receipt, null, 2), { mode: 0o600 });
    return receiptId;
  }

  function persist() {
    const payload = {
      updatedAt: nowIso(nowFn()),
      threads: Object.fromEntries(threads),
    };
    ensureDir(stateDirectory);
    fs.writeFileSync(threadsPath, JSON.stringify(payload, null, 2), { mode: 0o600 });
  }

  for (const threadId of pendingRestartInterrupts) {
    const rec = threads.get(threadId);
    if (!rec) continue;
    writeWorkStatus(rec.workId, {
      state: 'INTERRUPTED',
      threadId,
      turnId: rec.lastTurnId || null,
      cwd: rec.cwd,
      reason: 'conductor restart while turn marked running',
      grokSessionId: rec.grokSessionId || null,
    });
  }
  if (pendingRestartInterrupts.length) persist();

  function record(method, params = {}) {
    seq += 1;
    const event = { seq, ts: nowIso(nowFn()), method, params };
    events.push(event);
    if (events.length > EVENT_RING) events.shift();
    const line = JSON.stringify({
      seq: event.seq,
      ts: event.ts,
      method,
      threadId: params.threadId || null,
      turnId: params.turnId || null,
      sessionId: params.sessionId || null,
    });
    ensureDir(logsDir);
    fs.appendFileSync(path.join(logsDir, 'events.jsonl'), `${line}\n`, { mode: 0o600 });
    return event;
  }

  function publicThread(rec) {
    return {
      cwd: rec.cwd,
      startedAt: rec.startedAt,
      grokSessionId: rec.grokSessionId || null,
      lastTurnId: rec.lastTurnId || null,
      lastTurnStatus: rec.lastTurnStatus || 'idle',
      workId: rec.workId || null,
      queuedSteer: rec.queuedSteer ? true : false,
      steerPath: STEER_PATH,
      lastOutputAt: rec.lastOutputAt || null,
      status: { type: rec.lastTurnStatus === 'running' ? 'running' : 'idle' },
    };
  }

  function activeTurnCount() {
    let n = 0;
    for (const rec of threads.values()) {
      if (rec.lastTurnStatus === 'running') n += 1;
    }
    return n;
  }

  function rememberSession(rec, sessionId) {
    if (!sessionId) return false;
    const changed = rec.grokSessionId !== sessionId || rec.sessionResumable !== true;
    rec.grokSessionId = sessionId;
    rec.sessionResumable = true;
    if (changed) persist();
    return changed;
  }

  function sessionOnDisk(rec) {
    if (!rec?.grokSessionId) return false;
    const dir = sessionDirFor(grokHome, rec.cwd, rec.grokSessionId);
    return dir ? fs.existsSync(dir) : false;
  }

  function isZombie(rec) {
    if (!rec || rec.lastTurnStatus !== 'running') return false;
    if (pendingTurns.has(rec.lastTurnId)) return false;
    if (children.has(rec.lastTurnId)) return false;
    const now = nowFn();
    const marks = [];
    if (rec.lastOutputAt) marks.push(Date.parse(rec.lastOutputAt));
    if (rec.lastSpawnAt) marks.push(Date.parse(rec.lastSpawnAt));
    const cwdMs = cwdActivityFn(rec.cwd);
    if (cwdMs) marks.push(cwdMs);
    const latest = marks.filter((n) => Number.isFinite(n) && n > 0);
    if (!latest.length) return true;
    return now - Math.max(...latest) >= zombieIdleMs;
  }

  function reapZombies() {
    const reaped = [];
    for (const [threadId, rec] of threads) {
      if (!isZombie(rec)) continue;
      rec.lastTurnStatus = 'interrupted';
      rec.zombieReapedAt = nowIso(nowFn());
      persist();
      record('turn/zombie', { threadId, turnId: rec.lastTurnId || null });
      writeWorkStatus(rec.workId, {
        state: 'INTERRUPTED',
        threadId,
        turnId: rec.lastTurnId || null,
        cwd: rec.cwd,
        reason: 'zombie: lastTurnStatus=running, no grok child, no cwd write for 15m',
        grokSessionId: rec.grokSessionId || null,
      });
      reaped.push(threadId);
    }
    return reaped;
  }

  function resolveThreadId(body = {}) {
    if (typeof body.threadId === 'string' && body.threadId.trim()) {
      const id = body.threadId.trim();
      if (!threads.has(id)) throw Object.assign(new Error('unknown threadId'), { statusCode: 404 });
      return id;
    }
    if (typeof body.workId === 'string' && body.workId.trim()) {
      const workId = body.workId.trim();
      const matches = [...threads.entries()].filter(([, rec]) => rec.workId === workId);
      if (!matches.length) throw Object.assign(new Error('unknown workId'), { statusCode: 404 });
      const running = matches.filter(([, rec]) => rec.lastTurnStatus === 'running');
      const pool = running.length ? running : matches;
      pool.sort((a, b) => String(b[1].startedAt || '').localeCompare(String(a[1].startedAt || '')));
      return pool[0][0];
    }
    throw Object.assign(new Error('threadId or workId is required'), { statusCode: 400 });
  }

  function pendingForThread(threadId) {
    for (const pending of pendingTurns.values()) {
      if (pending.threadId === threadId) return pending;
    }
    return null;
  }

  function waitForChildGone(turnId, ms) {
    return new Promise((resolve) => {
      if (!children.has(turnId)) return resolve(true);
      const child = children.get(turnId);
      let settled = false;
      const done = (ok) => {
        if (settled) return;
        settled = true;
        resolve(ok);
      };
      const timer = setTimeout(() => done(!children.has(turnId)), ms);
      child?.once?.('exit', () => {
        clearTimeout(timer);
        done(true);
      });
    });
  }

  async function startTurn(threadId, text, model, memoryPrompt = text) {
    const rec = threads.get(threadId);
    if (!rec) throw Object.assign(new Error('unknown threadId'), { statusCode: 404 });
    if (pendingForThread(threadId)) {
      throw Object.assign(new Error('thread already has a pending memory preparation'), { statusCode: 409 });
    }
    if (rec.lastTurnStatus === 'running' && children.has(rec.lastTurnId)) {
      throw Object.assign(new Error('thread already has a running turn'), { statusCode: 409 });
    }
    if (rec.lastTurnStatus === 'running' && !children.has(rec.lastTurnId)) {
      rec.lastTurnStatus = 'interrupted';
    }
    if (activeTurnCount() >= maxConcurrentTurns) {
      throw Object.assign(new Error('max concurrent Grok turns reached'), { statusCode: 429 });
    }
    const steerPrefix = rec.queuedSteer ? `STEERING UPDATE:\n${rec.queuedSteer}\n\n` : '';
    rec.queuedSteer = null;
    const turnId = crypto.randomUUID();
    if (!rec.grokSessionId) rec.grokSessionId = crypto.randomUUID();
    const resumable = rec.sessionResumable === true || sessionOnDisk(rec);
    rec.sessionResumable = resumable;
    const pending = { threadId, turnId, cancelled: false };
    pendingTurns.set(turnId, pending);
    rec.lastTurnId = turnId;
    rec.lastTurnStatus = 'running';
    rec.lastSpawnAt = nowIso(nowFn());
    rec.lastOutputAt = rec.lastSpawnAt;
    persist();
    record('turn/started', {
      threadId,
      turnId,
      turn: { id: turnId },
      sessionId: rec.grokSessionId || null,
    });
    writeWorkStatus(rec.workId, {
      state: 'RUNNING',
      threadId,
      turnId,
      cwd: rec.cwd,
      grokSessionId: rec.grokSessionId || null,
    });

    let preparation;
    try {
      preparation = await prepareMemory({
        threadId,
        sessionId: rec.grokSessionId,
        turnId,
        cwd: rec.cwd,
        prompt: memoryPrompt,
      });
    } catch (error) {
      if (pendingTurns.get(turnId) === pending) pendingTurns.delete(turnId);
      if (rec.lastTurnId === turnId && rec.lastTurnStatus === 'running') {
        rec.lastTurnStatus = 'failed';
        persist();
        writeWorkStatus(rec.workId, {
          state: 'FAILED',
          threadId,
          turnId,
          cwd: rec.cwd,
          reason: error.message,
          grokSessionId: rec.grokSessionId || null,
        });
      }
      throw error;
    }
    if (pending.cancelled || pendingTurns.get(turnId) !== pending) {
      if (pendingTurns.get(turnId) === pending) pendingTurns.delete(turnId);
      if (rec.lastTurnId === turnId && rec.lastTurnStatus === 'running') {
        rec.lastTurnStatus = 'interrupted';
        persist();
      }
      throw Object.assign(new Error('turn interrupted before Grok spawn'), { statusCode: 409 });
    }
    const memoryPrefix = preparation.context ? `${preparation.context}\n\n` : '';
    const promptText = `${seatRulesText.trim()}\n\n${PROTECTED_POLICY}\n\n${memoryPrefix}${steerPrefix}${text}`;
    const promptFile = path.join(stateDirectory, 'prompts', `${turnId}.txt`);
    let promptRemoved = false;
    const removePromptFile = () => {
      if (promptRemoved) return;
      promptRemoved = true;
      try { fs.unlinkSync(promptFile); } catch { /* keep if unlink fails */ }
    };
    const clearPending = () => {
      if (pendingTurns.get(turnId) === pending) pendingTurns.delete(turnId);
    };
    try {
      writePrivateFileAtomic(promptFile, promptText);
    } catch (error) {
      clearPending();
      rec.lastTurnStatus = 'failed';
      persist();
      writeWorkStatus(rec.workId, {
        state: 'FAILED',
        threadId,
        turnId,
        cwd: rec.cwd,
        reason: error.message,
        grokSessionId: rec.grokSessionId || null,
      });
      throw error;
    }

    let child;
    let stdoutBuf = '';
    let commitStarted = false;
    let providerFailed = false;
    let terminalFinalized = false;
    let onLine = () => {};
    const memorySessionId = rec.grokSessionId;
    const finalizeTurn = ({ source = 'exit', code = null, signal = null, error = null } = {}) => {
      if (terminalFinalized) return;
      terminalFinalized = true;
      providerFailed = providerFailed || Boolean(error);
      children.delete(turnId);
      removePromptFile();

      const current = threads.get(threadId);
      const matchesCurrentTurn = Boolean(current && current.lastTurnId === turnId);
      const terminalStatus = error
        ? 'failed'
        : (signal === 'SIGTERM' || signal === 'SIGKILL' ? 'interrupted' : (code === 0 ? 'completed' : 'failed'));
      if (source !== 'spawn-sync' && source !== 'spawn-error' && stdoutBuf.trim()) onLine(stdoutBuf);
      if (source !== 'spawn-sync' && source !== 'spawn-error' && matchesCurrentTurn && current.grokSessionId && sessionOnDisk(current)) {
        current.sessionResumable = true;
      }
      let finalStatus = terminalStatus;
      if (matchesCurrentTurn) {
        if (current.lastTurnStatus === 'running') {
          current.lastTurnStatus = terminalStatus;
          persist();
        }
        finalStatus = current.lastTurnStatus || terminalStatus;
      }
      if (error && source !== 'spawn-sync') {
        record('grok/spawn-error', {
          threadId,
          turnId,
          message: error?.message || 'grok child failed to spawn',
        });
      }
      record('turn/completed', {
        threadId,
        turnId,
        turn: { id: turnId, status: finalStatus },
        exitCode: code,
        signal: signal || null,
        sessionId: matchesCurrentTurn ? current.grokSessionId || null : memorySessionId || null,
      });
      if (matchesCurrentTurn) {
        const textBits = events
          .filter((event) => event.params.turnId === turnId && event.method === 'grok/text' && event.params.data)
          .map((event) => event.params.data);
        writeWorkStatus(current.workId, {
          state: finalStatus === 'completed' ? 'COMPLETED' : String(finalStatus).toUpperCase(),
          threadId,
          turnId,
          cwd: current.cwd,
          exitCode: code,
          signal: signal || null,
          reason: error?.message || undefined,
          grokSessionId: current.grokSessionId || null,
          excerpt: textBits.join('').slice(-4000) || null,
        });
      }
    };

    try {
      child = spawnGrok({
        grokBin,
        grokHome,
        cwd: rec.cwd,
        promptFile,
        text: promptText,
        model: model || rec.model || null,
        grokSessionId: resumable ? rec.grokSessionId : null,
        newSessionId: resumable ? null : rec.grokSessionId,
        memoryBadgePath,
      });
    } catch (error) {
      clearPending();
      finalizeTurn({ source: 'spawn-sync', error });
      throw error;
    }
    clearPending();
    children.set(turnId, child);

    const maybeCommit = (parsed) => {
      if (providerFailed || commitStarted || !preparation.commitEligible || !isModelContentEvent(parsed)) return;
      commitStarted = true;
      void commitMemory({
        threadId,
        sessionId: memorySessionId,
        turnId,
        preparation,
      }).catch(() => undefined);
    };

    const touch = () => {
      rec.lastOutputAt = nowIso(nowFn());
    };

    onLine = (line) => {
      if (!line.trim()) return;
      let parsed;
      try { parsed = JSON.parse(line); } catch { return; }
      if (isProviderFailureEvent(parsed)) providerFailed = true;
      const sessionId = extractSessionId(parsed);
      if (sessionId) rememberSession(rec, sessionId);
      touch();
      record(`grok/${parsed.type || 'event'}`, {
        threadId,
        turnId,
        type: parsed.type || null,
        stopReason: parsed.stopReason || null,
        sessionId: sessionId || rec.grokSessionId || null,
        data: truncate(parsed.data || parsed.text || null),
      });
      maybeCommit(parsed);
    };

    child.stdout?.setEncoding('utf8');
    child.stdout?.on('data', (chunk) => {
      touch();
      stdoutBuf += chunk;
      let idx;
      while ((idx = stdoutBuf.indexOf('\n')) >= 0) {
        const line = stdoutBuf.slice(0, idx);
        stdoutBuf = stdoutBuf.slice(idx + 1);
        onLine(line);
      }
    });
    child.stderr?.setEncoding('utf8');
    child.stderr?.on('data', (chunk) => {
      touch();
      record('grok/stderr', { threadId, turnId, bytes: Buffer.byteLength(chunk) });
    });
    child.on('error', (error) => finalizeTurn({ source: 'spawn-error', error }));
    child.on('exit', (code, signal) => finalizeTurn({ source: 'exit', code, signal }));
    child.on('close', (code, signal) => finalizeTurn({ source: 'close', code, signal }));

    return { turnId, startedSeq: seq, grokSessionId: rec.grokSessionId || null };
  }

  function interruptTurn(threadId, turnId) {
    const rec = threads.get(threadId);
    if (!rec) throw Object.assign(new Error('unknown threadId'), { statusCode: 404 });
    const id = turnId || rec.lastTurnId;
    const child = children.get(id);
    const pending = pendingTurns.get(id);
    if (pending && pending.turnId === id && !child) {
      pending.cancelled = true;
      rec.lastTurnStatus = 'interrupted';
      persist();
      writeWorkStatus(rec.workId, {
        state: 'INTERRUPTED',
        threadId,
        turnId: id,
        cwd: rec.cwd,
        reason: 'interrupt while preparing memory context',
        grokSessionId: rec.grokSessionId || null,
      });
      return { threadId, turnId: id, interrupted: true, zombie: false, preparing: true };
    }
    if (!child) {
      if (rec.lastTurnStatus === 'running') {
        rec.lastTurnStatus = 'interrupted';
        persist();
        writeWorkStatus(rec.workId, {
          state: 'INTERRUPTED',
          threadId,
          turnId: id,
          cwd: rec.cwd,
          reason: 'interrupt with no grok child',
          grokSessionId: rec.grokSessionId || null,
        });
        return { threadId, turnId: id, interrupted: true, zombie: true };
      }
      throw Object.assign(new Error('no running turn'), { statusCode: 409 });
    }
    child.kill('SIGTERM');
    const killer = setTimeout(() => {
      if (children.has(id)) {
        try { children.get(id).kill('SIGKILL'); } catch { /* already gone */ }
      }
    }, Math.min(1500, steerInterruptMs));
    killer.unref();
    return { threadId, turnId: id, interrupted: true, zombie: false };
  }

  async function steerTurn(threadId, text, model) {
    const rec = threads.get(threadId);
    if (!rec) throw Object.assign(new Error('unknown threadId'), { statusCode: 404 });
    if (typeof text !== 'string' || !text.trim()) throw new Error('text is required');
    reapZombies();

    const running = rec.lastTurnStatus === 'running';
    const child = children.get(rec.lastTurnId);
    if (running && child) {
      rec.queuedSteer = null;
      persist();
      const oldTurnId = rec.lastTurnId;
      interruptTurn(threadId, oldTurnId);
      await waitForChildGone(oldTurnId, steerInterruptMs);
      if (rec.lastTurnStatus === 'running' && !children.has(oldTurnId)) {
        rec.lastTurnStatus = 'interrupted';
        persist();
      }
      const started = await startTurn(threadId, `MID-JOB STEER:\n${text}`, model, text);
      return {
        queued: false,
        mode: 'interrupt-resume',
        path: STEER_PATH,
        grokSessionId: rec.grokSessionId || null,
        ...started,
      };
    }
    if (running && !child) {
      rec.lastTurnStatus = 'interrupted';
      persist();
      const started = await startTurn(threadId, `MID-JOB STEER:\n${text}`, model, text);
      return {
        queued: false,
        mode: 'interrupt-resume',
        path: STEER_PATH,
        zombie: true,
        grokSessionId: rec.grokSessionId || null,
        ...started,
      };
    }
    const started = await startTurn(threadId, text, model, text);
    return {
      queued: false,
      mode: 'follow-up-turn',
      path: 'resume-follow-up',
      grokSessionId: rec.grokSessionId || null,
      ...started,
    };
  }

  const server = http.createServer(async (req, res) => {
    const url = new URL(req.url, `http://${HOST}:${port}`);
    try {
      if (req.method === 'GET' && url.pathname === '/status') {
        reapZombies();
        const readiness = readinessProvider();
        return json(res, 200, {
          ok: true,
          runtime: 'grok',
          protocol: PROTOCOL,
          notACodexLane: true,
          laneId: config.laneId || 'grok',
          port,
          host: HOST,
          pid: process.pid,
          grokBin,
          threads: Object.fromEntries([...threads].map(([id, rec]) => [id, publicThread(rec)])),
          eventSeq: seq,
          activeTurns: activeTurnCount(),
          maxConcurrentTurns,
          capabilities: config.capabilities || ['reasoning', 'tools'],
          steerPath: STEER_PATH,
          zombieIdleMs,
          sessionCapture: 'streaming-json + --session-id/--resume',
          binaryPresent: Boolean(readiness.binaryPresent),
          binaryVersion: readiness.binaryVersion || null,
          expectedVersion: readiness.expectedVersion || expectedVersion,
          loginState: readiness.loginState || 'not_proved',
          readyForDispatch: readiness.readyForDispatch === true,
        });
      }
      if (req.method === 'GET' && url.pathname === '/threads') {
        const limit = Number.parseInt(url.searchParams.get('limit') || '20', 10);
        const list = [...threads.entries()].slice(-limit).map(([id, rec]) => ({ threadId: id, ...publicThread(rec) }));
        return json(res, 200, { threads: list });
      }
      if (req.method === 'GET' && url.pathname === '/events') {
        const tid = url.searchParams.get('threadId');
        const after = Number.parseInt(url.searchParams.get('afterSeq') || '0', 10);
        const limit = Number.parseInt(url.searchParams.get('limit') || '200', 10);
        const out = events
          .filter((event) => event.seq > after && (!tid || event.params.threadId === tid))
          .slice(0, limit);
        return json(res, 200, { events: out, lastSeq: seq });
      }
      if (req.method === 'POST' && url.pathname === '/rpc') {
        return json(res, 501, {
          error: 'Grok peer has no Codex app-server RPC',
          protocol: PROTOCOL,
        });
      }
      if (req.method === 'POST' && url.pathname === '/thread/start') {
        const body = await readBody(req);
        assertEnvelope(body, new Set(['cwd', 'model', 'workId', 'protectedAction']));
        const cwd = assertAbsoluteDir(body.cwd);
        const threadId = crypto.randomUUID();
        const rec = {
          cwd,
          startedAt: nowIso(nowFn()),
          model: body.model || null,
          workId: body.workId || null,
          grokSessionId: null,
          sessionResumable: false,
          lastTurnId: null,
          lastTurnStatus: 'idle',
          queuedSteer: null,
        };
        threads.set(threadId, rec);
        persist();
        record('thread/started', { threadId, cwd });
        return json(res, 200, { threadId, raw: { runtime: 'grok', cwd } });
      }
      if (req.method === 'POST' && url.pathname === '/thread/resume') {
        const body = await readBody(req);
        if (typeof body.threadId !== 'string' || !body.threadId.trim()) {
          throw new Error('threadId is required');
        }
        const existing = threads.get(body.threadId) || {
          cwd: body.cwd ? assertAbsoluteDir(body.cwd) : null,
          startedAt: nowIso(nowFn()),
          lastTurnStatus: 'idle',
          sessionResumable: false,
        };
        if (body.cwd) existing.cwd = assertAbsoluteDir(body.cwd);
        if (body.grokSessionId) {
          existing.grokSessionId = body.grokSessionId;
          existing.sessionResumable = true;
        }
        if (!existing.cwd) throw new Error('cwd is required to resume an unknown thread');
        threads.set(body.threadId, existing);
        persist();
        return json(res, 200, { threadId: body.threadId, ...publicThread(existing) });
      }
      if (req.method === 'POST' && url.pathname === '/turn/start') {
        const body = await readBody(req);
        const readiness = readinessProvider();
        if (readiness.readyForDispatch !== true) {
          const workId = typeof body.workId === 'string'
            ? body.workId
            : (typeof body.threadId === 'string' ? threads.get(body.threadId)?.workId || null : null);
          writeWorkStatus(workId, {
            state: 'FAILED', threadId: body.threadId || null, turnId: null,
            reason: 'Grok is not ready for dispatch', errorCode: 'GROK_NOT_READY',
          });
          const receiptId = writeReadinessRejection(body, workId);
          return json(res, 503, { error: 'Grok is not ready for dispatch', code: 'GROK_NOT_READY', terminal: true, receiptId });
        }
        assertEnvelope(body, new Set(['threadId', 'workId', 'text', 'model', 'protectedAction']));
        if (typeof body.text !== 'string' || !body.text.trim()) {
          throw Object.assign(new Error('text is required'), { statusCode: 400 });
        }
        if (Buffer.byteLength(body.text) > TEXT_CAP) {
          throw Object.assign(new Error('text exceeds 20000 bytes'), { statusCode: 413 });
        }
        const threadId = resolveThreadId(body);
        const started = await startTurn(threadId, body.text, body.model, body.text);
        return json(res, 200, started);
      }
      if (req.method === 'POST' && url.pathname === '/turn/steer') {
        const body = await readBody(req);
        assertEnvelope(body, new Set(['threadId', 'workId', 'text', 'model', 'protectedAction']));
        const threadId = resolveThreadId(body);
        const steered = await steerTurn(threadId, body.text, body.model);
        return json(res, 200, steered);
      }
      if (req.method === 'POST' && url.pathname === '/turn/interrupt') {
        const body = await readBody(req);
        const threadId = resolveThreadId(body);
        return json(res, 200, interruptTurn(threadId, body.turnId));
      }
      return json(res, 404, { error: 'unknown endpoint' });
    } catch (error) {
      const status = error.statusCode || (error instanceof SyntaxError ? 400 : 500);
      return json(res, status, { error: error.message });
    }
  });

  const reapTimer = setInterval(() => {
    try { reapZombies(); } catch { /* keep serving */ }
  }, 30000);
  reapTimer.unref();

  function close() {
    clearInterval(reapTimer);
    for (const child of children.values()) {
      try { child.kill('SIGTERM'); } catch { /* already gone */ }
    }
    return new Promise((resolve) => server.close(resolve));
  }

  return {
    server,
    threads,
    events,
    children,
    startTurn,
    interruptTurn,
    steerTurn,
    reapZombies,
    resolveThreadId,
    close,
    persist,
  };
}

export function startConductor(options = {}) {
  const borgHome = options.borgHome || process.env.BORG_HOME;
  const configPath = options.configPath || (borgHome ? path.join(borgHome, 'conductors/config.json') : null);
  if (!options.config && !configPath) throw new Error('BORG_HOME or an explicit configPath is required');
  const config = options.config || loadConfig(configPath);
  const { server } = createConductor({ ...options, config });
  const port = Number(options.port ?? config.port ?? DEFAULT_PORT);
  server.listen(port, HOST, () => {
    process.stdout.write(`[grok-conductor] listening on http://${HOST}:${port}\n`);
  });
  process.on('SIGINT', () => process.exit(0));
  process.on('SIGTERM', () => process.exit(0));
  return server;
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  startConductor();
}
