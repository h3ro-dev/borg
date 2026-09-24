// Fixed read-only child for receipt-reader.mjs. Never dispatches or mutates state.
import fs from 'node:fs';
import path from 'node:path';

const MAX_ENTRIES = 10000;
const MAX_FILE_BYTES = 65536;
const MAX_TOTAL_BYTES = 4 * 1024 * 1024;
function reject(code) { const error = new Error(code); error.code = code; throw error; }
function privateStat(target, directory = false) {
  const stat = fs.lstatSync(target);
  if (stat.isSymbolicLink() || (directory ? !stat.isDirectory() : !stat.isFile()) || (stat.mode & 0o077) !== 0) reject('UNSAFE_RECEIPT');
  return stat;
}
function record(target) {
  const stat = privateStat(target);
  if (stat.size > MAX_FILE_BYTES) reject('RECEIPT_FILE_LIMIT');
  const fd = fs.openSync(target, fs.constants.O_RDONLY | (fs.constants.O_NOFOLLOW ?? 0) | (fs.constants.O_NONBLOCK ?? 0));
  try {
    const actual = fs.fstatSync(fd);
    if (!actual.isFile() || (actual.mode & 0o077) !== 0 || actual.ino !== stat.ino || actual.dev !== stat.dev) reject('UNSAFE_RECEIPT');
    const buffer = Buffer.alloc(MAX_FILE_BYTES + 1);
    let bytes = 0;
    while (bytes < buffer.length) {
      const count = fs.readSync(fd, buffer, bytes, buffer.length - bytes, null);
      if (!count) break;
      bytes += count;
    }
    if (bytes > MAX_FILE_BYTES) reject('RECEIPT_FILE_LIMIT');
    let value;
    try { value = JSON.parse(buffer.subarray(0, bytes).toString('utf8')); }
    catch { reject('RECEIPT_JSON_INVALID'); }
    if (!value || typeof value !== 'object' || Array.isArray(value)) reject('RECEIPT_JSON_INVALID');
    return { value, bytes };
  } finally { fs.closeSync(fd); }
}
function directory(target) {
  privateStat(target, true);
  const handle = fs.opendirSync(target);
  const values = [];
  let seen = 0; let total = 0;
  try {
    let entry;
    while ((entry = handle.readSync()) !== null) {
      if (++seen > MAX_ENTRIES) reject('RECEIPT_ENTRY_LIMIT');
      if (!entry.name.endsWith('.json')) continue;
      if (!entry.isFile() || entry.isSymbolicLink()) reject('UNSAFE_RECEIPT');
      const read = record(path.join(target, entry.name));
      total += read.bytes;
      if (total > MAX_TOTAL_BYTES) reject('RECEIPT_TOTAL_LIMIT');
      values.push(read.value);
    }
  } finally { handle.closeSync(); }
  return values;
}
const [mode, target] = process.argv.slice(2);
try {
  if (!['record','directory'].includes(mode) || !path.isAbsolute(target ?? '')) reject('RECEIPT_REQUEST_INVALID');
  let value;
  // Only a missing requested root is empty/not-found. A receipt disappearing
  // during enumeration makes the WHOLE scan unknown, not partially successful.
  try { fs.lstatSync(target); }
  catch (error) {
    if (error.code !== 'ENOENT') throw error;
    process.stdout.write(JSON.stringify({ ok: true, value: mode === 'directory' ? [] : null }));
    process.exit(0);
  }
  value = mode === 'directory' ? directory(target) : record(target).value;
  process.stdout.write(JSON.stringify({ ok: true, value }));
} catch (error) {
  process.stdout.write(JSON.stringify({ ok: false, code: /^[A-Z_]+$/.test(error.code ?? '') ? error.code : 'RECEIPT_READ_FAILED' }));
  process.exitCode = 1;
}
