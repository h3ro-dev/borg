import crypto from 'node:crypto';
import fs from 'node:fs';
import path from 'node:path';

export const HTTP_TOKEN_DIRECTORY = '.conductor';
export const HTTP_TOKEN_FILE = 'http-token';

function tokenPaths(codexHome) {
  if (typeof codexHome !== 'string' || !path.isAbsolute(codexHome)) {
    throw new Error('CODEX_HOME must be absolute before reading its conductor token');
  }
  const directory = path.join(codexHome, HTTP_TOKEN_DIRECTORY);
  return { directory, tokenPath: path.join(directory, HTTP_TOKEN_FILE) };
}

function owned(stat, label) {
  if (typeof process.getuid === 'function' && stat.uid !== process.getuid()) {
    throw new Error(`${label} is owned by another user`);
  }
}

function privateTokenDirectory(directory, allowMissing) {
  let stat;
  try {
    stat = fs.lstatSync(directory);
  } catch (error) {
    if (allowMissing && error.code === 'ENOENT') return null;
    throw error;
  }
  if (!stat.isDirectory() || stat.isSymbolicLink()) {
    throw new Error('conductor token directory must be a regular directory');
  }
  owned(stat, 'conductor token directory');
  if ((stat.mode & 0o077) !== 0) throw new Error('conductor token directory must be mode 0700');
  return stat;
}

export function httpTokenPath(codexHome) {
  return tokenPaths(codexHome).tokenPath;
}

export function readHttpToken(codexHome, options = {}) {
  const { directory, tokenPath } = tokenPaths(codexHome);
  const allowMissing = options.allowMissing === true;
  if (!privateTokenDirectory(directory, allowMissing)) return null;
  let fd;
  try {
    fd = fs.openSync(tokenPath, fs.constants.O_RDONLY | (fs.constants.O_NOFOLLOW ?? 0));
  } catch (error) {
    if (allowMissing && error.code === 'ENOENT') return null;
    throw error;
  }
  try {
    const stat = fs.fstatSync(fd);
    if (!stat.isFile() || stat.nlink !== 1 || (stat.mode & 0o077) !== 0 || stat.size > 128) {
      throw new Error('conductor token must be one owner-only regular file');
    }
    owned(stat, 'conductor token');
    const buffer = Buffer.alloc(129);
    const bytes = fs.readSync(fd, buffer, 0, buffer.length, 0);
    if (bytes > 128) throw new Error('conductor token is oversized');
    const token = buffer.subarray(0, bytes).toString('utf8').trim();
    if (!/^[a-f0-9]{64}$/.test(token)) throw new Error('conductor token is malformed');
    return token;
  } finally {
    fs.closeSync(fd);
  }
}

export function ensureHttpToken(codexHome) {
  const { directory, tokenPath } = tokenPaths(codexHome);
  fs.mkdirSync(directory, { recursive: true, mode: 0o700 });
  const stat = fs.lstatSync(directory);
  if (!stat.isDirectory() || stat.isSymbolicLink()) {
    throw new Error('conductor token directory must be a regular directory');
  }
  owned(stat, 'conductor token directory');
  fs.chmodSync(directory, 0o700);
  try {
    return readHttpToken(codexHome);
  } catch (error) {
    if (error.code !== 'ENOENT') throw error;
  }

  const token = crypto.randomBytes(32).toString('hex');
  let fd;
  try {
    fd = fs.openSync(tokenPath, fs.constants.O_WRONLY | fs.constants.O_CREAT
      | fs.constants.O_EXCL | (fs.constants.O_NOFOLLOW ?? 0), 0o600);
    fs.writeSync(fd, `${token}\n`);
    fs.fsyncSync(fd);
    fs.fchmodSync(fd, 0o600);
  } catch (error) {
    if (error.code !== 'EEXIST') throw error;
  } finally {
    if (fd !== undefined) fs.closeSync(fd);
  }
  return readHttpToken(codexHome);
}
