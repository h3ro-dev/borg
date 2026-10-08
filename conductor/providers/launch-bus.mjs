#!/usr/bin/env node
// Provider launch bus retained from the supplied Grok conductor. Every path
// and provider is resolved from this BORG installation; it owns no global queue.

import { spawn, execFileSync } from 'node:child_process';
import crypto from 'node:crypto';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const RUNTIMES = new Set(['grok', 'claude', 'codex']);

function canonicalAbsolute(value, label) {
  if (typeof value !== 'string' || !path.isAbsolute(value)
      || path.normalize(value) !== value || path.resolve(value) !== value) {
    throw new Error(`${label} must be a canonical absolute path`);
  }
  return value;
}

export function resolveLaunchBusConfig(product) {
  if (!product || typeof product !== 'object' || Array.isArray(product)) {
    throw new Error('product config must be an object');
  }
  const borgHome = canonicalAbsolute(product.borgHome, 'BORG_HOME');
  const grok = product.providers?.grok ?? { enabled: false };
  const claude = product.providers?.claude ?? { enabled: false };
  return {
    borgHome,
    launchRoot: path.join(borgHome, 'private/provider-launches'),
    notificationRoot: path.join(borgHome, 'private/provider-notifications'),
    providers: {
      grok: {
        ...grok,
        host: grok.host ?? '127.0.0.1',
        capabilities: ['reasoning', 'interrupt-resume-steer'],
        missingCapabilities: ['codex-account-rpc', 'codex-allowance-routing'],
      },
      claude: {
        ...claude,
        capabilities: ['headless-turn'],
        missingCapabilities: ['native-thread-status', 'mid-turn-steer', 'provider-allowance-routing'],
      },
      codex: {
        enabled: true,
        nodeBin: product.runtime?.nodeBin,
        routeScript: path.join(borgHome, 'app/conductor/borg-conductor.mjs'),
        capabilities: ['native-thread-status', 'mid-turn-steer', 'provider-allowance-routing'],
        missingCapabilities: [],
      },
    },
  };
}

function loadProductConfig(configPath) {
  return JSON.parse(fs.readFileSync(canonicalAbsolute(configPath, 'config path'), 'utf8'));
}

function ensurePrivateDirectory(directory) {
  fs.mkdirSync(directory, { recursive: true, mode: 0o700 });
  fs.chmodSync(directory, 0o700);
}

function ensureLaunchDirs(config) {
  for (const name of ['unread', 'active', 'done', 'failed', 'logs', 'prompts']) {
    ensurePrivateDirectory(path.join(config.launchRoot, name));
  }
  ensurePrivateDirectory(config.notificationRoot);
}

export function readLaunchPacket(raw) {
  if (!raw || typeof raw !== 'object' || Array.isArray(raw)) throw new Error('packet must be an object');
  if (typeof raw.workId !== 'string' || !raw.workId.trim()) throw new Error('workId required');
  if (typeof raw.cwd !== 'string' || !path.isAbsolute(raw.cwd)) throw new Error('absolute cwd required');
  if (typeof raw.prompt !== 'string' || !raw.prompt.trim()) throw new Error('prompt required');
  if (/ignore (all )?(previous|prior) instructions/i.test(raw.prompt)) {
    throw new Error('packet looks like an instruction override; refused');
  }
  const runtime = (raw.runtime || 'grok').toLowerCase();
  if (!RUNTIMES.has(runtime)) throw new Error('runtime must be grok, claude, or codex');
  return {
    workId: raw.workId.trim(),
    runtime,
    cwd: path.normalize(raw.cwd),
    prompt: raw.prompt,
    model: typeof raw.model === 'string' ? raw.model : null,
    launchedBy: typeof raw.launchedBy === 'string' ? raw.launchedBy : 'borg-launch-bus',
  };
}

function wrapPrompt(packet, config) {
  return [
    'LAUNCH NOTICE',
    `workId: ${packet.workId}`,
    `runtime: ${packet.runtime}`,
    `launchedBy: ${packet.launchedBy}`,
    "The local launch bus owns this work. Follow this installation's owner policy and the task brief.",
    `When finished, write a short result to ${path.join(config.launchRoot, 'done', `${packet.workId}.md`)}`,
    '',
    packet.prompt,
  ].join('\n');
}

async function request(base, method, pathname, body) {
  const response = await fetch(`${base}${pathname}`, {
    method,
    headers: body ? { 'content-type': 'application/json' } : undefined,
    body: body ? JSON.stringify(body) : undefined,
  });
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || `${method} ${pathname} ${response.status}`);
  return data;
}

function privateWrite(target, value) {
  fs.writeFileSync(target, value, { mode: 0o600 });
  fs.chmodSync(target, 0o600);
}

function appendNotification(config, runtime, text) {
  const target = path.join(config.notificationRoot, `${runtime}.jsonl`);
  fs.appendFileSync(target, `${JSON.stringify({ at: new Date().toISOString(), text })}\n`, { mode: 0o600 });
  fs.chmodSync(target, 0o600);
}

async function dispatchGrok(packet, text, config) {
  const provider = config.providers.grok;
  if (provider.enabled !== true) throw new Error('Grok provider is disabled');
  if (provider.host !== '127.0.0.1') throw new Error('Grok provider host must be 127.0.0.1');
  const base = `http://${provider.host}:${provider.port}`;
  const thread = await request(base, 'POST', '/thread/start', {
    cwd: packet.cwd, workId: packet.workId, model: packet.model || undefined, protectedAction: false,
  });
  const turn = await request(base, 'POST', '/turn/start', {
    threadId: thread.threadId, text, model: packet.model || undefined, protectedAction: false,
  });
  return { threadId: thread.threadId, turnId: turn.turnId, channel: base };
}

function dispatchClaude(packet, text, config, attempt) {
  const provider = config.providers.claude;
  if (provider.enabled !== true) throw new Error('Claude provider is disabled');
  const binary = canonicalAbsolute(provider.binary, 'Claude binary');
  const promptFile = path.join(config.launchRoot, 'prompts', `${packet.workId}.txt`);
  const logFile = path.join(config.launchRoot, 'logs', `${packet.workId}.claude.log`);
  privateWrite(promptFile, text);
  const child = spawn(binary, ['-p'], {
    cwd: packet.cwd,
    detached: true,
    stdio: ['pipe', 'pipe', 'pipe'],
    env: { ...process.env, CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC: '1' },
  });
  let closed = false;
  const record = { ...attempt, pid: child.pid, logFile, channel: 'claude-code-headless' };
  const finish = (code, signal, error = null) => {
    if (closed) return;
    closed = true;
    const result = resultFor(config, record);
    closeLaunch(config, record, {
      ...result,
      state: code === 0 && result.outcome === 'RESULT_WRITTEN' ? 'FINISHED' : 'FAILED',
      outcome: error ? `EXIT_${error.code || 'ERROR'}` : signal ? `SIGNAL_${signal}`
        : code !== 0 ? `EXIT_${code}` : result.outcome,
      exitCode: code, finishedAt: new Date().toISOString(), finishedAtSource: 'exit',
      closedBy: 'launch-bus exit',
    });
  };
  child.on('error', (error) => finish(null, null, error));
  child.on('close', (code, signal) => finish(code, signal));
  // A spawn failure or early exit may close stdin before the prompt is written.
  child.stdin.on('error', () => {});
  child.stdin.end(text);
  const output = fs.createWriteStream(logFile, { flags: 'a', mode: 0o600 });
  child.stdout.pipe(output);
  child.stderr.pipe(output);
  child.unref();
  return { pid: child.pid, logFile, channel: 'claude-code-headless' };
}

function dispatchCodex(packet, text, config) {
  const provider = config.providers.codex;
  const nodeBin = canonicalAbsolute(provider.nodeBin, 'Node binary');
  const promptFile = path.join(config.launchRoot, 'prompts', `${packet.workId}.txt`);
  const logFile = path.join(config.launchRoot, 'logs', `${packet.workId}.codex.log`);
  privateWrite(promptFile, text);
  const child = spawn(nodeBin, [provider.routeScript, 'route',
    '--config', path.join(config.borgHome, 'conductors/config.json'),
    '--cwd', packet.cwd,
    '--prompt-file', promptFile,
    '--work-id', packet.workId,
    '--capability', 'tools',
    ...(packet.model ? ['--model', packet.model] : []),
  ], {
    detached: true,
    stdio: ['ignore', 'pipe', 'pipe'],
    env: { ...process.env, BORG_HOME: config.borgHome },
  });
  const output = fs.createWriteStream(logFile, { flags: 'a', mode: 0o600 });
  child.stdout.pipe(output);
  child.stderr.pipe(output);
  child.unref();
  return { pid: child.pid, logFile, channel: 'borg-conductor-route' };
}

export function resultFor(config, record) {
  const resultFile = path.join(config.launchRoot, 'done', `${record.workId}.md`);
  try {
    const stat = fs.statSync(resultFile);
    if (stat.isFile() && stat.mtimeMs >= Date.parse(record.launchedAt) - 1000) {
      return { outcome: 'RESULT_WRITTEN', resultFile,
        resultSha256: crypto.createHash('sha256').update(fs.readFileSync(resultFile)).digest('hex'),
        priorResult: false, finishedAt: stat.mtime.toISOString() };
    }
    return { outcome: 'NO_RESULT', priorResult: true, resultFile: null, resultSha256: null };
  } catch (error) {
    if (error.code !== 'ENOENT') throw error;
    return { outcome: 'NO_RESULT', priorResult: false, resultFile: null, resultSha256: null };
  }
}

export function closeLaunch(config, record, closure) {
  ensureLaunchDirs(config);
  // The exit error and close events, or a later reconcile, must not double-close.
  for (const folder of ['done', 'failed']) {
    const target = path.join(config.launchRoot, folder, `${record.workId}.json`);
    try {
      const previous = JSON.parse(fs.readFileSync(target, 'utf8'));
      if (previous.launchedAt === record.launchedAt && previous.closedBy) return previous;
    } catch (error) { if (error.code !== 'ENOENT') throw error; }
  }
  const row = { schemaVersion: 1, workId: record.workId, runtime: record.runtime,
    launchedBy: record.launchedBy, launchedAt: record.launchedAt, notifyId: record.notifyId,
    pid: record.pid ?? null, exitCode: null, ...closure };
  const closed = { ...record, ...row };
  privateWrite(path.join(config.launchRoot, closure.state === 'FINISHED' ? 'done' : 'failed',
    `${record.workId}.json`), JSON.stringify(closed, null, 2));
  const ledger = path.join(config.launchRoot, 'ledger.jsonl');
  fs.appendFileSync(ledger, `${JSON.stringify(row)}\n`, { mode: 0o600 });
  fs.chmodSync(ledger, 0o600);
  const active = path.join(config.launchRoot, 'active', `${record.workId}.json`);
  try {
    if (JSON.parse(fs.readFileSync(active, 'utf8')).launchedAt === record.launchedAt) fs.unlinkSync(active);
  } catch (error) { if (error.code !== 'ENOENT') throw error; }
  appendNotification(config, record.runtime, `work ${record.workId} ${closure.state.toLowerCase()}`);
  return closed;
}

export function processState(record, config) {
  if (!Number.isInteger(record.pid) || record.pid <= 0) return 'unknown';
  try { process.kill(record.pid, 0); }
  catch (error) { if (error.code === 'ESRCH') return 'gone'; }
  let observed;
  try {
    observed = execFileSync('/bin/ps', ['-o', 'lstart=,args=', '-p', String(record.pid)],
      { encoding: 'utf8', env: { ...process.env, LC_ALL: 'C' } }).trim();
  } catch (error) {
    // ps returns 1 with no output if a process disappears between the probes.
    if (error.status === 1 && !String(error.stdout || '').trim()) return 'gone';
    return 'unknown';
  }
  if (!observed) return 'gone';
  const match = observed.match(/^(\w+\s+\w+\s+\d+\s+\d+:\d+:\d+\s+\d+)\s+(.+)$/);
  if (!match) return 'unknown';
  const started = Date.parse(match[1]);
  const launched = Date.parse(record.launchedAt);
  if (!Number.isFinite(started) || !Number.isFinite(launched)) return 'unknown';
  return Math.abs(started - launched) > 120000 || !match[2].includes(config.providers.claude.binary)
    ? 'other' : 'running';
}

export function reconcileLaunches(options = {}) {
  const product = options.config || loadProductConfig(options.configPath
    || path.join(canonicalAbsolute(process.env.BORG_HOME, 'BORG_HOME'), 'conductors/config.json'));
  const config = resolveLaunchBusConfig(product);
  const summary = { scanned: 0, closed: { FINISHED: 0, FAILED: 0 }, live: 0,
    unknown: 0, skipped: 0, dryRun: !!options.dryRun, workIds: { closed: [], live: [], unknown: [] } };
  const active = path.join(config.launchRoot, 'active');
  if (!fs.existsSync(active)) return summary;
  for (const name of fs.readdirSync(active).filter((name) => name.endsWith('.json')).sort()) {
    const record = JSON.parse(fs.readFileSync(path.join(active, name), 'utf8'));
    if (options.workId && record.workId !== options.workId) continue;
    summary.scanned++;
    if (record.runtime !== 'claude') { summary.skipped++; continue; }
    const state = (options.processState || processState)(record, config);
    if (!['gone', 'other'].includes(state)) {
      const key = state === 'running' ? 'live' : 'unknown';
      summary[key]++; summary.workIds[key].push(record.workId); continue;
    }
    const result = resultFor(config, record);
    const finished = result.outcome === 'RESULT_WRITTEN';
    const closure = { ...result, state: finished ? 'FINISHED' : 'FAILED',
      finishedAt: result.finishedAt || new Date().toISOString(),
      finishedAtSource: finished ? 'result_mtime' : 'reconciled', closedBy: 'launch-bus reconcile' };
    if (!finished) {
      try { closure.lastLogLine = fs.readFileSync(record.logFile, 'utf8').trimEnd().split('\n').at(-1).slice(-200); }
      catch { closure.lastLogLine = ''; }
    }
    if (!options.dryRun) closeLaunch(config, record, closure);
    summary.closed[closure.state]++; summary.workIds.closed.push(record.workId);
  }
  return summary;
}

export function reconcileCLI(args) {
  const options = {};
  for (let index = 0; index < args.length; index++) {
    if (args[index] === '--dry-run') options.dryRun = true;
    else if (args[index] === '--json') continue;
    else if (args[index] === '--work-id' && args[index + 1]) options.workId = args[++index];
    else throw new Error(`unknown reconcile option: ${args[index]}`);
  }
  return reconcileLaunches(options);
}

export async function launchWork(raw, options = {}) {
  const packet = readLaunchPacket(raw);
  const product = options.config || loadProductConfig(options.configPath
    || path.join(canonicalAbsolute(process.env.BORG_HOME, 'BORG_HOME'), 'conductors/config.json'));
  const config = resolveLaunchBusConfig(product);
  ensureLaunchDirs(config);
  const text = wrapPrompt(packet, config);
  const attempt = {
    schemaVersion: 1, workId: packet.workId, runtime: packet.runtime,
    launchedBy: packet.launchedBy, launchedAt: new Date().toISOString(),
    cwd: packet.cwd, state: 'STARTED', notifyId: crypto.randomUUID(),
  };
  let handle;
  if (packet.runtime === 'grok') handle = await dispatchGrok(packet, text, config, attempt);
  else if (packet.runtime === 'claude') handle = dispatchClaude(packet, text, config, attempt);
  else handle = dispatchCodex(packet, text, config, attempt);
  const receipt = { ...attempt, ...handle };
  privateWrite(path.join(config.launchRoot, 'unread', `${packet.workId}.json`), JSON.stringify(receipt, null, 2));
  privateWrite(path.join(config.launchRoot, 'active', `${packet.workId}.json`), JSON.stringify(receipt, null, 2));
  fs.appendFileSync(path.join(config.launchRoot, 'ledger.jsonl'), `${JSON.stringify(receipt)}\n`, { mode: 0o600 });
  appendNotification(config, packet.runtime, `work ${packet.workId} started`);
  return receipt;
}

const isMain = process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url);
if (isMain) {
  const file = process.argv[2];
  if (file === 'reconcile') {
    try { process.stdout.write(`${JSON.stringify(reconcileCLI(process.argv.slice(3)), null, 2)}\n`); }
    catch (error) { process.stderr.write(`${error.message}\n`); process.exitCode = 1; }
  } else if (!file) {
    process.stderr.write('usage: node launch-bus.mjs <packet.json>\n');
    process.exitCode = 2;
  } else {
    launchWork(JSON.parse(fs.readFileSync(file, 'utf8')))
      .then((receipt) => process.stdout.write(`${JSON.stringify(receipt, null, 2)}\n`))
      .catch((error) => { process.stderr.write(`${error.message}\n`); process.exitCode = 1; });
  }
}
