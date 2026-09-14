#!/usr/bin/env node
// Provider launch bus retained from the supplied Grok conductor. Every path
// and provider is resolved from this BORG installation; it owns no global queue.

import { spawn } from 'node:child_process';
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

function dispatchClaude(packet, text, config) {
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

export async function launchWork(raw, options = {}) {
  const packet = readLaunchPacket(raw);
  const product = options.config || loadProductConfig(options.configPath
    || path.join(canonicalAbsolute(process.env.BORG_HOME, 'BORG_HOME'), 'conductors/config.json'));
  const config = resolveLaunchBusConfig(product);
  ensureLaunchDirs(config);
  const text = wrapPrompt(packet, config);
  let handle;
  if (packet.runtime === 'grok') handle = await dispatchGrok(packet, text, config);
  else if (packet.runtime === 'claude') handle = dispatchClaude(packet, text, config);
  else handle = dispatchCodex(packet, text, config);
  const receipt = {
    schemaVersion: 1,
    workId: packet.workId,
    runtime: packet.runtime,
    launchedBy: packet.launchedBy,
    launchedAt: new Date().toISOString(),
    cwd: packet.cwd,
    state: 'STARTED',
    notifyId: crypto.randomUUID(),
    ...handle,
  };
  privateWrite(path.join(config.launchRoot, 'unread', `${packet.workId}.json`), JSON.stringify(receipt, null, 2));
  privateWrite(path.join(config.launchRoot, 'active', `${packet.workId}.json`), JSON.stringify(receipt, null, 2));
  fs.appendFileSync(path.join(config.launchRoot, 'ledger.jsonl'), `${JSON.stringify(receipt)}\n`, { mode: 0o600 });
  appendNotification(config, packet.runtime, `work ${packet.workId} started`);
  return receipt;
}

const isMain = process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url);
if (isMain) {
  const file = process.argv[2];
  if (!file) {
    process.stderr.write('usage: node launch-bus.mjs <packet.json>\n');
    process.exitCode = 2;
  } else {
    launchWork(JSON.parse(fs.readFileSync(file, 'utf8')))
      .then((receipt) => process.stdout.write(`${JSON.stringify(receipt, null, 2)}\n`))
      .catch((error) => { process.stderr.write(`${error.message}\n`); process.exitCode = 1; });
  }
}
