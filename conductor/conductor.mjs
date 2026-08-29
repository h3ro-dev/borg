#!/usr/bin/env node
// codex-conductor — drive a `codex app-server` over stdio and expose a local
// HTTP control plane, so an outer orchestrator (human, script, or another
// agent) can start threads, fire turns, steer them mid-flight, and watch the
// full notification stream.
//
//   node conductor.mjs                      # starts app-server + HTTP on 127.0.0.1:4747
//   CONDUCTOR_PORT=5050 CODEX_HOME=~/.codex-acct2 node conductor.mjs   # account sharding
//
// HTTP API (all JSON):
//   GET  /status                         conductor + child health, known threads
//   GET  /threads?limit=20               thread/list passthrough
//   GET  /events?threadId=&afterSeq=     buffered notification stream (per thread)
//   POST /rpc        {method, params, timeoutMs?}            raw JSON-RPC passthrough
//   POST /thread/start  {cwd, model?, instructions?, sandbox?, personality?}
//   POST /thread/resume {threadId, cwd?}
//   POST /turn/start    {threadId, text}  returns {turnId} as soon as the turn starts
//   POST /turn/steer    {threadId, expectedTurnId, text}
//   POST /turn/interrupt {threadId, turnId}
//
// Safety posture: threads default to sandbox=workspace-write and
// approvalPolicy=never. If the server ever asks for an approval anyway, the
// conductor DENIES it and logs loudly — it never silently grants.

import { spawn } from 'node:child_process';
import http from 'node:http';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

export const LOG_DIRECTORY_MODE = 0o700;
export const LOG_FILE_MODE = 0o600;

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

export function buildThreadResumeParams(body = {}) {
  if (!body || typeof body !== 'object' || Array.isArray(body)) {
    throw new Error('thread resume body must be an object');
  }
  if (typeof body.threadId !== 'string' || !body.threadId.trim()) {
    throw new Error('threadId is required');
  }
  const sandbox = body.sandbox || 'read-only';
  if (!['read-only', 'workspace-write', 'danger-full-access'].includes(sandbox)) {
    throw new Error('unsupported resume sandbox');
  }
  const approvalPolicy = body.approvalPolicy || 'never';
  if (!['never', 'on-request', 'untrusted'].includes(approvalPolicy)) {
    throw new Error('unsupported resume approval policy');
  }
  return {
    threadId: body.threadId,
    ...(body.cwd ? { cwd: body.cwd } : {}),
    sandbox,
    approvalPolicy,
    ...(body.runtimeWorkspaceRoots ? { runtimeWorkspaceRoots: body.runtimeWorkspaceRoots } : {}),
    ...(body.model ? { model: body.model } : {}),
    ...(body.instructions ? { developerInstructions: body.instructions } : {}),
    ...(body.personality ? { personality: body.personality } : {}),
    ...(body.config ? { config: body.config } : {}),
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

function startConductor() {

const PORT = parseInt(process.env.CONDUCTOR_PORT || '4747', 10);
const HOST = '127.0.0.1';
const CODEX_BIN = process.env.CODEX_BIN || 'codex';
const LOG_DIR = process.env.CONDUCTOR_LOGS || path.join(process.cwd(), 'logs');
const EVENT_RING_MAX = 5000;

ensurePrivateLogDirectory(LOG_DIR);
ensurePrivateLogDirectory(path.join(LOG_DIR, 'threads'));
const bootTs = new Date().toISOString().replace(/[:.]/g, '-');
const mainLog = openPrivateLogStream(path.join(LOG_DIR, `conductor-${bootTs}.jsonl`));

function log(kind, data) {
  const entry = { ts: new Date().toISOString(), kind, data };
  mainLog.write(JSON.stringify(entry) + '\n');
  if (kind === 'server-request' || kind === 'child-exit' || kind === 'parse-error') {
    console.error(`[conductor] ${kind}:`, typeof data === 'string' ? data.slice(0, 300) : data);
  }
}

// ---------- app-server child ----------
const child = spawn(CODEX_BIN, ['app-server'], { stdio: ['pipe', 'pipe', 'pipe'] });
child.stderr.on('data', (d) => log('child-stderr', childStderrLogMetadata(d)));
child.on('exit', (code, sig) => { log('child-exit', { code, sig }); process.exit(1); });

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
const threads = new Map(); // threadId -> {cwd, startedAt, lastTurnId, lastTurnStatus}

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

async function readBody(req) {
  let data = '';
  for await (const c of req) data += c;
  return data ? JSON.parse(data) : {};
}

const server = http.createServer(async (req, res) => {
  const url = new URL(req.url, `http://${HOST}:${PORT}`);
  try {
    await initialized;
    if (req.method === 'GET' && url.pathname === '/status') {
      return json(res, 200, {
        ok: true, childPid: child.pid, port: PORT,
        codexHome: process.env.CODEX_HOME || null,
        threads: Object.fromEntries(threads), eventSeq: seq,
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
      const out = events.filter((e) => e.seq > after && (!tid || threadIdOf(e.params) === tid)).slice(0, limit);
      return json(res, 200, { events: out, lastSeq: seq });
    }
    if (req.method === 'POST' && url.pathname === '/rpc') {
      const b = await readBody(req);
      return json(res, 200, await rpc(b.method, b.params || {}, b.timeoutMs || 120000));
    }
    if (req.method === 'POST' && url.pathname === '/thread/start') {
      const b = await readBody(req);
      const params = {
        cwd: b.cwd,
        sandbox: b.sandbox || 'workspace-write',
        approvalPolicy: b.approvalPolicy || 'never',
        ...(b.model ? { model: b.model } : {}),
        ...(b.instructions ? { developerInstructions: b.instructions } : {}),
        ...(b.personality ? { personality: b.personality } : {}),
        ...(b.config ? { config: b.config } : {}),
      };
      const r = await rpc('thread/start', params, 60000);
      const tid = r.thread?.id;
      if (tid) threads.set(tid, { cwd: b.cwd, startedAt: new Date().toISOString() });
      return json(res, 200, { threadId: tid, raw: r });
    }
    if (req.method === 'POST' && url.pathname === '/thread/resume') {
      const b = await readBody(req);
      const r = await rpc('thread/resume', buildThreadResumeParams(b), 60000);
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
    return json(res, 500, { error: e.message, rpc: e.rpc || null });
  }
});

server.listen(PORT, HOST, () => {
  console.log(`[conductor] listening on http://${HOST}:${PORT} (app-server pid ${child.pid}, logs in ${LOG_DIR})`);
});

process.on('SIGINT', () => { child.kill(); process.exit(0); });
process.on('SIGTERM', () => { child.kill(); process.exit(0); });
}

if (pathsReferToSameFile(process.argv[1], fileURLToPath(import.meta.url))) startConductor();
