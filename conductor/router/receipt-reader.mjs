import { spawn } from 'node:child_process';
import { fileURLToPath } from 'node:url';

const MAX_OUTPUT_BYTES = 8 * 1024 * 1024;
const WORKER = fileURLToPath(new URL('./receipt-reader-worker.mjs', import.meta.url));

// Keep potentially stuck OS directory/file reads out of the router's libuv pool.
// This helper is strictly read-only. A failed or partial scan NEVER means no claims.
function readInChild(mode, target, options = {}) {
  const timeoutMs = options.timeoutMs ?? 5000;
  if (!Number.isInteger(timeoutMs) || timeoutMs < 1 || timeoutMs > 60000) {
    throw new Error('RECEIPT_READ_TIMEOUT_INVALID');
  }
  return new Promise((resolve, reject) => {
    const child = spawn(process.execPath, [WORKER, mode, target], {
      stdio: ['ignore', 'pipe', 'pipe'], windowsHide: true,
    });
    let settled = false;
    let bytes = 0;
    const chunks = [];
    let timer;
    function finish(error, result) {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      if (error) {
        // Sending SIGKILL is not proof of exit. The result makes no exit claim;
        // unref/destroy prevents an uninterruptible read retaining this caller.
        if (child.exitCode === null && child.signalCode === null) child.kill('SIGKILL');
        child.stdout.destroy(); child.stderr.destroy(); child.unref();
        error.targetPath = target;
        error.stage = 'RECEIPT_READ';
        reject(error);
      } else resolve(result);
    }
    function fail(code) {
      const error = new Error(`${code}: ${target}`);
      error.code = code;
      finish(error);
    }
    timer = setTimeout(() => fail('RECEIPT_READ_TIMEOUT'), timeoutMs);
    child.on('error', () => fail('RECEIPT_READER_UNAVAILABLE'));
    child.stdout.on('data', chunk => {
      bytes += chunk.length;
      if (bytes > MAX_OUTPUT_BYTES) fail('RECEIPT_OUTPUT_LIMIT');
      else chunks.push(chunk);
    });
    // Never echo diagnostic stderr, which may contain local/private content.
    child.stderr.on('data', () => {});
    child.on('close', (code) => {
      if (settled) return;
      let envelope;
      try { envelope = JSON.parse(Buffer.concat(chunks).toString('utf8')); }
      catch { fail('RECEIPT_READER_INVALID_OUTPUT'); return; }
      if (code !== 0 || envelope.ok !== true) {
        const errorCode = /^[A-Z_]+$/.test(envelope.code ?? '') ? envelope.code : 'RECEIPT_READ_FAILED';
        fail(errorCode); return;
      }
      const warnings = Array.isArray(envelope.warnings) ? envelope.warnings : [];
      for (const warning of warnings) {
        if (typeof options.onWarning === 'function') options.onWarning(warning);
        else process.emitWarning(warning.message, { code: warning.code });
      }
      finish(null, envelope.value);
    });
  });
}

export async function readPrivateJsonDirectory(directory, options = {}) {
  return readInChild('directory', directory, options);
}

export async function readPrivateJsonDirectoryEntries(directory, options = {}) {
  return readInChild('entries', directory, options);
}

export async function readPrivateJsonRecord(target, options = {}) {
  return readInChild('record', target, options);
}
