#!/usr/bin/env node
// codex-conductor — drive a `codex app-server` over stdio and expose a local
// HTTP control plane, so an outer orchestrator (human, script, or another
// agent) can start threads, fire turns, steer them mid-flight, and watch the
// full notification stream.
//
//   node conductor.mjs                      # starts app-server + HTTP on 127.0.0.1:4747
//   BORG_HOME=/opt/borg CODEX_HOME=/opt/borg/conductors/primary/profile node conductor.mjs
//
// HTTP API (all JSON):
//   GET  /status                         conductor + child health, known threads, supported roles
//   GET  /threads?limit=20               thread/list passthrough
//   GET  /events?threadId=&afterSeq=     buffered notification stream (per thread)
//   POST /rpc        {method, params, timeoutMs?}            allowlisted JSON-RPC passthrough
//   POST /thread/start  {cwd, model?, instructions?, role?, sandbox?, personality?}
//   POST /lead/thread/start  {cwd, model?, instructions?, sandbox?, personality?}
//   POST /thread/resume {threadId, cwd?, role?, model?, predecessor?}
//   POST /turn/start    {threadId, text}  returns {turnId} as soon as the turn starts
//   POST /turn/steer    {threadId, expectedTurnId, text}
//   POST /turn/interrupt {threadId, turnId}
//
// Safety posture (owner ruling 2026-08-29, re-ruled 2026-09-01): threads default to
// sandbox=danger-full-access — callers may pass a narrower sandbox explicitly — and
// approvalPolicy=never. If the server ever asks for an approval anyway, the
// conductor DENIES it and logs loudly — it never silently grants.

import { spawn } from 'node:child_process';
import crypto from 'node:crypto';
import http from 'node:http';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { ensureHttpToken } from './http-auth.mjs';
export function ownerPolicyPaths(borgHome) {
  if (typeof borgHome !== 'string' || !path.isAbsolute(borgHome)
      || path.normalize(borgHome) !== borgHome || path.resolve(borgHome) !== borgHome) {
    throw new Error('BORG_HOME must be a canonical absolute path');
  }
  return {
    seat: path.join(borgHome, 'policies', 'SEAT-RULES.md'),
    lead: path.join(borgHome, 'policies', 'LEAD-RULES.md'),
  };
}

function readRequiredPolicy(file, role) {
  try {
    const rules = fs.readFileSync(file, 'utf8').trim();
    if (!rules) throw new Error(`missing or empty owner policy for ${role}: ${file}`);
    return rules;
  } catch (error) {
    if (error.code === 'ENOENT') {
      throw new Error(`missing or empty owner policy for ${role}: ${file}`);
    }
    throw error;
  }
}

export function loadOwnerPolicies(borgHome) {
  const files = ownerPolicyPaths(borgHome);
  return {
    seat: readRequiredPolicy(files.seat, 'seat'),
    lead: readRequiredPolicy(files.lead, 'lead'),
    files,
  };
}

class BadRequestError extends Error {
  constructor(message) {
    super(message);
    this.statusCode = 400;
  }
}

function normalizeRole(role) {
  if (role === undefined) return 'leaf';
  if (role !== 'lead' && role !== 'leaf') {
    throw new BadRequestError('invalid role: expected lead or leaf');
  }
  return role;
}

function seatDeveloperInstructions(extra, role, policies) {
  const rules = role === 'lead' ? policies?.lead : policies?.seat;
  if (!rules) throw new BadRequestError(`owner ${role} policy missing`);
  const local = typeof extra === 'string' ? extra.trim() : '';
  return [rules, local].filter(Boolean).join('\n\n');
}

export function buildThreadStartParams(body = {}, policies = {}) {
  const role = normalizeRole(body.role);
  return {
    cwd: body.cwd,
    sandbox: body.sandbox || 'danger-full-access',
    approvalPolicy: body.approvalPolicy || 'never',
    ...(body.model ? { model: body.model } : {}),
    developerInstructions: seatDeveloperInstructions(body.instructions, role, policies),
    ...(body.personality ? { personality: body.personality } : {}),
    ...(body.config ? { config: body.config } : {}),
  };
}

export const LOG_DIRECTORY_MODE = 0o700;
export const LOG_FILE_MODE = 0o600;
export const MAX_BODY_BYTES = 64 * 1024;
export const MAX_EVENT_PAGE = 200;
export const RPC_METHOD_ALLOWLIST = new Set([
  'account/read',
  'account/rateLimits/read',
  'model/list',
  'thread/read',
  'hooks/list',
  'config/read',
]);

function allowedHost(value) {
  if (typeof value !== 'string') return false;
  return /^(?:(?:127\.0\.0\.1|localhost)(?::[0-9]+)?|\[::1\](?::[0-9]+)?)$/i.test(value);
}

function browserRequestReason(headers = {}) {
  if (Object.hasOwn(headers, 'origin')) return 'origin_forbidden';
  if (Object.hasOwn(headers, 'sec-fetch-site')
      && String(headers['sec-fetch-site']).toLowerCase() !== 'none') {
    return 'sec_fetch_site_forbidden';
  }
  return null;
}

function jsonContentType(headers = {}) {
  const value = headers['content-type'];
  return typeof value === 'string' && /^application\/json(?:\s*;|$)/i.test(value.trim());
}

function bearerReason(header, expected) {
  if (header === undefined) return 'missing_token';
  if (typeof header !== 'string') return 'bad_token';
  const match = /^Bearer ([a-f0-9]{64})$/i.exec(header);
  if (!match) return 'bad_token';
  const supplied = Buffer.from(match[1], 'utf8');
  const wanted = Buffer.from(expected, 'utf8');
  return supplied.length === wanted.length && crypto.timingSafeEqual(supplied, wanted)
    ? null
    : 'bad_token';
}

export async function readBody(req) {
  const declared = Number(req.headers?.['content-length'] || 0);
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
  return data ? JSON.parse(data) : {};
}

export function paginateEvents(events, options = {}, lastSeq = 0) {
  const afterSeq = Number.isInteger(options.afterSeq) && options.afterSeq >= 0 ? options.afterSeq : 0;
  const requested = Number.isInteger(options.limit) && options.limit > 0 ? options.limit : MAX_EVENT_PAGE;
  const limit = Math.min(requested, MAX_EVENT_PAGE);
  const matching = events.filter((event) => {
    const params = event.params || {};
    const eventThreadId = params.threadId || params.thread?.id || null;
    return event.seq > afterSeq && (!options.threadId || eventThreadId === options.threadId);
  });
  const page = matching.slice(0, limit);
  const nextAfterSeq = page.length ? page[page.length - 1].seq : afterSeq;
  return {
    events: page,
    lastSeq,
    nextAfterSeq,
    hasMore: matching.length > page.length,
  };
}

const INITIALIZE_PARAMS = {
  clientInfo: { name: 'codex-conductor', title: 'Codex Conductor', version: '0.1.0' },
  capabilities: { experimentalApi: true },
};

const CURRENT_DECLINE_APPROVALS = new Set([
  'item/commandExecution/requestApproval',
  'item/fileChange/requestApproval',
]);
const LEGACY_DENY_APPROVALS = new Set([
  'applyPatchApproval',
  'execCommandApproval',
]);

export function initializedNotification() {
  return { jsonrpc: '2.0', method: 'initialized' };
}

export async function initializeProtocol(rpcCall, writeLine) {
  const result = await rpcCall('initialize', INITIALIZE_PARAMS);
  writeLine(JSON.stringify(initializedNotification()) + '\n');
  return result;
}

export function responseForServerRequest(message, nowMs = Date.now()) {
  const { id, method } = message;
  let result;
  if (CURRENT_DECLINE_APPROVALS.has(method)) {
    result = { decision: 'decline' };
  } else if (LEGACY_DENY_APPROVALS.has(method)) {
    result = { decision: 'denied' };
  } else if (method === 'item/permissions/requestApproval') {
    // The 0.144 protocol has no decline enum for this request. An empty
    // profile grants no additional filesystem or network permissions.
    result = { permissions: {}, scope: 'turn' };
  } else if (method === 'mcpServer/elicitation/request') {
    result = { action: 'decline' };
  } else if (method === 'currentTime/read') {
    result = { currentTimeAt: Math.floor(nowMs / 1000) };
  } else {
    return {
      jsonrpc: '2.0',
      id,
      error: { code: -32601, message: 'Conductor has no handler for this server request' },
    };
  }
  return { jsonrpc: '2.0', id, result };
}

export function serverRequestLogMetadata(message) {
  const params = message?.params || {};
  return {
    id: message?.id,
    method: message?.method,
    threadId: params.threadId || params.thread?.id || null,
    turnId: params.turnId || params.turn?.id || null,
    itemId: params.itemId || params.item?.id || null,
    approvalId: params.approvalId || null,
  };
}

function statusLabel(status) {
  if (typeof status === 'string') return status;
  if (status && typeof status.type === 'string') return status.type;
  return null;
}

export function eventLogMetadata(event) {
  const params = event?.params || {};
  return {
    seq: event?.seq,
    ts: event?.ts,
    method: event?.method,
    threadId: params.threadId || params.thread?.id || null,
    turnId: params.turnId || params.turn?.id || null,
    itemId: params.itemId || params.item?.id || null,
    status: statusLabel(params.status || params.turn?.status || params.item?.status),
  };
}

export function initializationLogMetadata(result) {
  return {
    userAgent: result?.userAgent || null,
    platformFamily: result?.platformFamily || null,
    platformOs: result?.platformOs || null,
  };
}

export function childStderrLogMetadata(data) {
  const text = Buffer.isBuffer(data) ? data.toString() : String(data);
  let category = 'other';
  if (/auth|log.?in|credential/i.test(text)) category = 'authentication';
  else if (/config|toml/i.test(text)) category = 'configuration';
  else if (/protocol|version|unsupported|incompatib/i.test(text)) category = 'compatibility';
  else if (/error|fail|panic/i.test(text)) category = 'error';
  return {
    bytes: Buffer.byteLength(text),
    lines: text.split('\n').filter(Boolean).length,
    category,
  };
}

export function buildThreadResumeParams(body = {}, policies = {}) {
  if (!body || typeof body !== 'object' || Array.isArray(body)) {
    throw new Error('thread resume body must be an object');
  }
  if (typeof body.threadId !== 'string' || !body.threadId.trim()) {
    throw new Error('threadId is required');
  }
  const sandbox = body.sandbox || 'danger-full-access';
  if (!['read-only', 'workspace-write', 'danger-full-access'].includes(sandbox)) {
    throw new Error('unsupported resume sandbox');
  }
  const approvalPolicy = body.approvalPolicy || 'never';
  if (!['never', 'on-request', 'untrusted'].includes(approvalPolicy)) {
    throw new Error('unsupported resume approval policy');
  }
  const role = normalizeRole(body.role);
  return {
    threadId: body.threadId,
    ...(body.cwd ? { cwd: body.cwd } : {}),
    sandbox,
    approvalPolicy,
    ...(body.runtimeWorkspaceRoots ? { runtimeWorkspaceRoots: body.runtimeWorkspaceRoots } : {}),
    ...(body.model ? { model: body.model } : {}),
    developerInstructions: seatDeveloperInstructions(body.instructions, role, policies),
    ...(body.personality ? { personality: body.personality } : {}),
    ...(body.config ? { config: body.config } : {}),
  };
}

function protocolError(message) {
  return Object.assign(new Error(message), { statusCode: 502 });
}

function predecessorForResume(body, previous) {
  if (!Object.hasOwn(body, 'predecessor')) return previous?.predecessor ?? null;
  if (body.predecessor === null
      || (typeof body.predecessor === 'object' && !Array.isArray(body.predecessor))) {
    return body.predecessor;
  }
  throw new BadRequestError('predecessor must be an object or null');
}

export function resumedThreadRecord(body, resumeResponse, readResponse, previous = null, now = new Date()) {
  const resumed = resumeResponse?.thread;
  const read = readResponse?.thread;
  if (!resumed || !read) throw protocolError('resume requires native thread response and complete readback');
  const ids = [body?.threadId, resumed.id, read.id];
  if (ids.some((id) => typeof id !== 'string' || !id.trim()) || new Set(ids).size !== 1) {
    throw protocolError('resume thread identity mismatch');
  }
  if (typeof resumeResponse.cwd !== 'string' || !path.isAbsolute(resumeResponse.cwd)) {
    throw protocolError('resume response missing effective absolute cwd');
  }
  if (!Array.isArray(read.turns)) throw protocolError('thread/read did not include complete turns');

  const latestTurn = read.turns.at(-1) || null;
  const threadStatus = read.status ?? resumed.status ?? null;
  const threadStatusName = statusLabel(threadStatus);
  const resumedAt = now.toISOString();
  const role = normalizeRole(body.role ?? previous?.role);
  const responseHasEffort = Object.hasOwn(resumeResponse, 'reasoningEffort');
  return {
    cwd: resumeResponse.cwd,
    persistedCwd: read.cwd ?? resumed.cwd ?? null,
    startedAt: previous?.startedAt ?? resumedAt,
    resumedAt,
    role,
    model: resumeResponse.model ?? previous?.model ?? body.model ?? null,
    effort: responseHasEffort ? resumeResponse.reasoningEffort : (previous?.effort ?? null),
    predecessor: predecessorForResume(body, previous),
    status: threadStatus,
    lastTurnId: latestTurn?.id ?? null,
    lastTurnStatus: statusLabel(latestTurn?.status)
      ?? (threadStatusName === 'active' ? 'running' : 'idle'),
    parentThreadId: read.parentThreadId ?? null,
    forkedFromId: read.forkedFromId ?? null,
    sessionId: read.sessionId ?? null,
    createdAt: read.createdAt ?? null,
    updatedAt: read.updatedAt ?? null,
    modelProvider: resumeResponse.modelProvider ?? read.modelProvider ?? null,
    approvalPolicy: resumeResponse.approvalPolicy ?? null,
    sandbox: resumeResponse.sandbox ?? null,
    runtimeWorkspaceRoots: resumeResponse.runtimeWorkspaceRoots ?? null,
  };
}

export function ensurePrivateLogDirectory(dir) {
  const existed = fs.existsSync(dir);
  fs.mkdirSync(dir, { recursive: true, mode: LOG_DIRECTORY_MODE });
  const stat = fs.lstatSync(dir);
  if (!stat.isDirectory() || stat.isSymbolicLink()) {
    throw new Error(`refusing non-directory log path: ${dir}`);
  }
  if (typeof process.getuid === 'function' && stat.uid !== process.getuid()) {
    throw new Error(`refusing log directory owned by another user: ${dir}`);
  }
  if (existed && (stat.mode & 0o022) !== 0) {
    throw new Error(`refusing group/world-writable log directory: ${dir}`);
  }
  // Never chmod a caller-supplied existing directory: CONDUCTOR_LOGS may be
  // a broader path. Newly created log directories are always owner-only.
  if (!existed) fs.chmodSync(dir, LOG_DIRECTORY_MODE);
}

export function openPrivateLogStream(file) {
  const flags = fs.constants.O_APPEND
    | fs.constants.O_CREAT
    | fs.constants.O_WRONLY
    | fs.constants.O_NOFOLLOW;
  const fd = fs.openSync(file, flags, LOG_FILE_MODE);
  try {
    const stat = fs.fstatSync(fd);
    if (!stat.isFile()) throw new Error(`refusing non-regular log file: ${file}`);
    fs.fchmodSync(fd, LOG_FILE_MODE);
    return fs.createWriteStream(file, { fd, flags: 'a', autoClose: true });
  } catch (error) {
    fs.closeSync(fd);
    throw error;
  }
}

export function threadLogFileName(threadId) {
  const value = String(threadId);
  if (!/^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$/.test(value)) {
    throw new Error('refusing unsafe thread id for log filename');
  }
  return `${value}.jsonl`;
}

export function pathsReferToSameFile(left, right) {
  if (!left || !right) return false;
  try {
    return fs.realpathSync.native(left) === fs.realpathSync.native(right);
  } catch {
    return false;
  }
}

export function startConductor(options = {}) {

const PORT = Number(options.port ?? process.env.CONDUCTOR_PORT ?? 4747);
const HOST = '127.0.0.1';
const CODEX_BIN = options.codexBin || process.env.CODEX_BIN || 'codex';
const CODEX_HOME = options.codexHome || process.env.CODEX_HOME || null;
const BORG_HOME = options.borgHome || process.env.BORG_HOME;
const LOG_DIR = options.logsPath || process.env.CONDUCTOR_LOGS || path.join(process.cwd(), 'logs');
const POLICIES = options.policies || loadOwnerPolicies(BORG_HOME);
const EVENT_RING_MAX = 5000;
const AUTH_MODE = options.authMode ?? process.env.CONDUCTOR_AUTH ?? 'enforce';

if (!Number.isInteger(PORT) || PORT < 1024 || PORT > 65535) {
  throw new Error('CONDUCTOR_PORT must be an integer from 1024 to 65535');
}
if (!CODEX_HOME || !path.isAbsolute(CODEX_HOME)) {
  throw new Error('CODEX_HOME must be an absolute dedicated profile path');
}
if (!['report', 'enforce'].includes(AUTH_MODE)) {
  throw new Error('CONDUCTOR_AUTH must be report or enforce');
}
const HTTP_TOKEN = ensureHttpToken(CODEX_HOME);

ensurePrivateLogDirectory(LOG_DIR);
ensurePrivateLogDirectory(path.join(LOG_DIR, 'threads'));
const bootTs = new Date().toISOString().replace(/[:.]/g, '-');
const mainLog = openPrivateLogStream(path.join(LOG_DIR, `conductor-${bootTs}.jsonl`));
const authLog = openPrivateLogStream(path.join(LOG_DIR, `http-auth-${bootTs}.jsonl`));
const authCounters = {
  mode: AUTH_MODE,
  unauthenticatedCount: 0,
  lastUnauthenticatedAt: null,
  disallowedRpcCount: 0,
};
const authLogTimes = new Map();

function log(kind, data) {
  const entry = { ts: new Date().toISOString(), kind, data };
  mainLog.write(JSON.stringify(entry) + '\n');
  if (kind === 'server-request' || kind === 'child-exit' || kind === 'parse-error') {
    console.error(`[conductor] ${kind}:`, typeof data === 'string' ? data.slice(0, 300) : data);
  }
}

function recordHttpViolation(req, pathname, reason) {
  const nowMs = Date.now();
  const ts = new Date(nowMs).toISOString();
  if (reason === 'missing_token' || reason === 'bad_token') {
    authCounters.unauthenticatedCount += 1;
    authCounters.lastUnauthenticatedAt = ts;
  }
  if (reason === 'disallowed_rpc_method') authCounters.disallowedRpcCount += 1;
  const key = `${pathname}\u0000${reason}`;
  const previous = authLogTimes.get(key);
  if (previous !== undefined && nowMs - previous < 60_000) return;
  authLogTimes.set(key, nowMs);
  authLog.write(`${JSON.stringify({
    ts,
    method: req.method || null,
    path: pathname,
    reason,
    userAgent: typeof req.headers['user-agent'] === 'string'
      ? req.headers['user-agent'].slice(0, 512)
      : null,
  })}\n`);
}

// ---------- app-server child ----------
const child = spawn(CODEX_BIN, ['app-server'], {
  stdio: ['pipe', 'pipe', 'pipe'],
  env: { ...process.env, CODEX_HOME },
});
let closing = false;
child.stderr.on('data', (d) => log('child-stderr', childStderrLogMetadata(d)));
child.on('exit', (code, sig) => {
  log('child-exit', { code, sig });
  if (!closing && options.exitOnChildExit !== false) process.exit(1);
});

let nextId = 1;
const pending = new Map();

function rpc(method, params = {}, timeoutMs = 120000) {
  const id = nextId++;
  child.stdin.write(JSON.stringify({ jsonrpc: '2.0', id, method, params }) + '\n');
  log('rpc-out', { id, method });
  return new Promise((resolve, reject) => {
    const t = setTimeout(() => {
      pending.delete(id);
      reject(new Error(`rpc timeout: ${method} (${timeoutMs}ms)`));
    }, timeoutMs);
    pending.set(id, { resolve, reject, t, method });
  });
}

// ---------- event store ----------
let seq = 0;
const events = []; // ring of {seq, ts, method, params}
const threadFiles = new Map();

function threadIdOf(params) {
  return params?.threadId || params?.thread?.id || null;
}

function recordEvent(m) {
  const e = { seq: ++seq, ts: new Date().toISOString(), method: m.method, params: m.params };
  events.push(e);
  if (events.length > EVENT_RING_MAX) events.shift();
  const tid = threadIdOf(m.params);
  if (tid) {
    let f = threadFiles.get(tid);
    if (!f) {
      f = openPrivateLogStream(path.join(LOG_DIR, 'threads', threadLogFileName(tid)));
      threadFiles.set(tid, f);
    }
    // Keep full event payloads only in the bounded in-memory ring used by the
    // localhost API. Persist metadata, never prompts, commands, diffs, or tool
    // payloads that may contain credentials or client data.
    f.write(JSON.stringify(eventLogMetadata(e)) + '\n');
  }
  log('event', { method: m.method, threadId: tid });
}

// server → client requests (approvals etc.) are denied, never granted.
function respondToServerRequest(m) {
  const response = responseForServerRequest(m);
  const metadata = serverRequestLogMetadata(m);
  log(response.error ? 'server-request-unhandled' : 'server-request', metadata);
  child.stdin.write(JSON.stringify(response) + '\n');
}

// ---------- stdio framing ----------
let buf = '';
child.stdout.on('data', (chunk) => {
  buf += chunk.toString();
  let idx;
  while ((idx = buf.indexOf('\n')) >= 0) {
    const line = buf.slice(0, idx); buf = buf.slice(idx + 1);
    if (!line.trim()) continue;
    let m;
    try { m = JSON.parse(line); } catch { log('parse-error', { bytes: Buffer.byteLength(line) }); continue; }
    if (m.id !== undefined && (m.result !== undefined || m.error !== undefined)) {
      const p = pending.get(m.id);
      if (p) {
        pending.delete(m.id); clearTimeout(p.t);
        if (m.error) p.reject(Object.assign(new Error(m.error.message || 'rpc error'), { rpc: m.error }));
        else p.resolve(m.result);
      } else {
        log('orphan-response', { id: m.id });
      }
    } else if (m.method && m.id !== undefined) {
      respondToServerRequest(m);
    } else if (m.method) {
      recordEvent(m);
    }
  }
});

// ---------- thread bookkeeping ----------
const threads = new Map(); // threadId -> effective settings plus native lifecycle readback

function trackFromEvent(e) {
  const tid = threadIdOf(e.params);
  if (!tid) return;
  const t = threads.get(tid) || { startedAt: e.ts };
  if (e.method === 'turn/started') { t.lastTurnId = e.params.turn?.id; t.lastTurnStatus = 'running'; }
  if (e.method === 'turn/completed') { t.lastTurnId = e.params.turn?.id; t.lastTurnStatus = e.params.turn?.status || 'completed'; }
  if (e.method === 'thread/status/changed') t.status = e.params.status;
  threads.set(tid, t);
}
// hook tracking into recordEvent via the ring (cheap: wrap)
const _record = recordEvent;
// eslint-disable-next-line no-func-assign
recordEvent = function (m) { _record(m); trackFromEvent({ method: m.method, params: m.params, ts: new Date().toISOString() }); };

// ---------- init ----------
const initialized = initializeProtocol(rpc, (line) => {
  child.stdin.write(line);
  log('notification-out', { method: 'initialized' });
}).then((r) => {
  log('initialized', initializationLogMetadata(r));
  return r;
});

// wait for a matching event with timeout
function waitForEvent(pred, timeoutMs) {
  return new Promise((resolve) => {
    const startSeq = seq;
    const deadline = Date.now() + timeoutMs;
    const iv = setInterval(() => {
      const hit = events.find((e) => e.seq > startSeq && pred(e));
      if (hit) { clearInterval(iv); resolve(hit); }
      else if (Date.now() > deadline) { clearInterval(iv); resolve(null); }
    }, 100);
  });
}

// ---------- HTTP ----------
function json(res, code, obj) {
  const body = JSON.stringify(obj, null, 1);
  res.writeHead(code, { 'content-type': 'application/json' });
  res.end(body);
}

const server = http.createServer(async (req, res) => {
  const url = new URL(req.url, `http://${HOST}:${PORT}`);
  try {
    if (!allowedHost(req.headers.host)) return json(res, 403, { error: 'host not allowed' });
    const browserReason = browserRequestReason(req.headers);
    if (browserReason) return json(res, 403, { error: 'browser request not allowed' });
    if (req.method === 'GET' && url.pathname === '/healthz') return json(res, 200, { ok: true });
    const authReason = bearerReason(req.headers.authorization, HTTP_TOKEN);
    if (authReason) {
      recordHttpViolation(req, url.pathname, authReason);
      if (AUTH_MODE === 'enforce') return json(res, 401, { error: 'bearer token required' });
    }
    if (req.method === 'POST' && !jsonContentType(req.headers)) {
      recordHttpViolation(req, url.pathname, 'invalid_content_type');
      if (AUTH_MODE === 'enforce') return json(res, 415, { error: 'application/json required' });
    }
    await initialized;
    if (req.method === 'GET' && url.pathname === '/status') {
      return json(res, 200, {
        ok: true, childPid: child.pid, port: PORT,
        codexHome: CODEX_HOME,
        supportedRoles: POLICIES.lead ? ['leaf', 'lead'] : ['leaf'],
        threads: Object.fromEntries(threads), eventSeq: seq,
        auth: { ...authCounters },
      });
    }
    if (req.method === 'GET' && url.pathname === '/threads') {
      const limit = parseInt(url.searchParams.get('limit') || '20', 10);
      return json(res, 200, await rpc('thread/list', { limit }));
    }
    if (req.method === 'GET' && url.pathname === '/events') {
      const tid = url.searchParams.get('threadId');
      const after = parseInt(url.searchParams.get('afterSeq') || '0', 10);
      const limit = parseInt(url.searchParams.get('limit') || '200', 10);
      return json(res, 200, paginateEvents(events, {
        threadId: tid,
        afterSeq: Number.isInteger(after) && after >= 0 ? after : 0,
        limit: Number.isInteger(limit) && limit > 0 ? limit : MAX_EVENT_PAGE,
      }, seq));
    }
    if (req.method === 'POST' && url.pathname === '/rpc') {
      const b = await readBody(req);
      if (!RPC_METHOD_ALLOWLIST.has(b.method)) {
        recordHttpViolation(req, url.pathname, 'disallowed_rpc_method');
        if (AUTH_MODE === 'enforce') return json(res, 403, { error: 'RPC method not allowed' });
      }
      return json(res, 200, await rpc(b.method, b.params || {}, b.timeoutMs || 120000));
    }
    if (req.method === 'POST'
      && (url.pathname === '/thread/start' || url.pathname === '/lead/thread/start')) {
      let b = await readBody(req);
      const leadOnly = url.pathname === '/lead/thread/start';
      if (leadOnly) {
        if (!b || typeof b !== 'object' || Array.isArray(b)) {
          throw new BadRequestError('lead thread start body must be an object');
        }
        if (b.role !== undefined) {
          const requestedRole = normalizeRole(b.role);
          if (requestedRole !== 'lead') throw new BadRequestError('lead endpoint requires role lead');
        }
        if (!POLICIES.lead) throw new BadRequestError('lead rules missing');
        b = { ...b, role: 'lead' };
      }
      const role = normalizeRole(b.role);
      const params = buildThreadStartParams(b, POLICIES);
      const r = await rpc('thread/start', params, 60000);
      const tid = r.thread?.id;
      if (tid) {
        threads.set(tid, { cwd: b.cwd, startedAt: new Date().toISOString(), role });
        log('thread-start', { threadId: tid, role });
      }
      return json(res, 200, { threadId: tid, raw: r });
    }
    if (req.method === 'POST' && url.pathname === '/thread/resume') {
      const b = await readBody(req);
      const previous = threads.get(b.threadId) || null;
      const effectiveBody = { ...b, role: b.role ?? previous?.role };
      const r = await rpc('thread/resume', buildThreadResumeParams(effectiveBody, POLICIES), 60000);
      const tid = r.thread?.id;
      if (typeof tid !== 'string' || !tid.trim() || tid !== b.threadId) {
        throw protocolError('resume thread identity mismatch');
      }
      const readback = await rpc('thread/read', { threadId: tid, includeTurns: true }, 60000);
      const record = resumedThreadRecord(effectiveBody, r, readback, previous);
      threads.set(tid, record);
      log('thread-resume', { threadId: tid, role: record.role });
      return json(res, 200, r);
    }
    if (req.method === 'POST' && url.pathname === '/turn/start') {
      const b = await readBody(req);
      // fire the turn; the RPC may not resolve until the turn ENDS, so we
      // return as soon as we observe turn/started for this thread.
      const rpcPromise = rpc('turn/start', {
        threadId: b.threadId,
        input: [{ type: 'text', text: b.text }],
        ...(b.model ? { model: b.model } : {}),
        ...(b.effort ? { effort: b.effort } : {}),
      }, 6 * 60 * 60 * 1000);
      rpcPromise.then((r) => log('turn-final', { threadId: b.threadId, turn: r.turn?.id, status: r.turn?.status }))
        .catch((e) => log('turn-error', { threadId: b.threadId, errorName: e.name || 'Error' }));
      const started = await waitForEvent(
        (e) => e.method === 'turn/started' && threadIdOf(e.params) === b.threadId, 30000);
      return json(res, 200, { turnId: started?.params?.turn?.id || null, startedSeq: started?.seq || null });
    }
    if (req.method === 'POST' && url.pathname === '/turn/steer') {
      const b = await readBody(req);
      const r = await rpc('turn/steer', {
        threadId: b.threadId, expectedTurnId: b.expectedTurnId,
        input: [{ type: 'text', text: b.text }],
      }, 60000);
      return json(res, 200, r);
    }
    if (req.method === 'POST' && url.pathname === '/turn/interrupt') {
      const b = await readBody(req);
      const r = await rpc('turn/interrupt', { threadId: b.threadId, turnId: b.turnId }, 60000);
      return json(res, 200, r);
    }
    return json(res, 404, { error: 'unknown endpoint' });
  } catch (e) {
    return json(res, e.statusCode || 500, { error: e.message, rpc: e.rpc || null });
  }
});

server.listen(PORT, HOST, () => {
  console.log(`[conductor] listening on http://${HOST}:${PORT} (app-server pid ${child.pid}, logs in ${LOG_DIR})`);
  options.onListening?.({ host: HOST, port: PORT, childPid: child.pid });
});

const close = () => {
  if (closing) return;
  closing = true;
  child.kill();
  server.close();
};
if (options.installSignalHandlers !== false) {
  process.once('SIGINT', close);
  process.once('SIGTERM', close);
}
return { child, server, initialized, close };
}

if (pathsReferToSameFile(process.argv[1], fileURLToPath(import.meta.url))) startConductor();
