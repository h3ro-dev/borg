import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import test from 'node:test';
import { readPrivateJsonDirectory, readPrivateJsonRecord } from '../router/receipt-reader.mjs';

function fixture(t) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'borg-receipt-reader-'));
  fs.chmodSync(root, 0o700);
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  return root;
}
test('real child returns complete private JSON records and explicit missing values', async (t) => {
  const root = fixture(t);
  fs.writeFileSync(path.join(root,'one.json'), JSON.stringify({ state: 'DISPATCHED', workId: 'one' }), { mode: 0o600 });
  fs.writeFileSync(path.join(root,'ignored.txt'), 'unrelated', { mode: 0o600 });
  assert.deepEqual(await readPrivateJsonDirectory(root), [{ state: 'DISPATCHED', workId: 'one' }]);
  assert.deepEqual(await readPrivateJsonRecord(path.join(root,'one.json')), { state: 'DISPATCHED', workId: 'one' });
  assert.deepEqual(await readPrivateJsonDirectory(path.join(root,'missing')), []);
  assert.equal(await readPrivateJsonRecord(path.join(root,'missing.json')), null);
});
test('one malformed receipt makes the entire scan unknown, not partial success', async (t) => {
  const root = fixture(t);
  fs.writeFileSync(path.join(root,'a.json'), '{"workId":"a"}', { mode: 0o600 });
  fs.writeFileSync(path.join(root,'z.json'), '{broken', { mode: 0o600 });
  await assert.rejects(readPrivateJsonDirectory(root), { code: 'RECEIPT_JSON_INVALID' });
});
test('oversized receipts are rejected before unbounded buffering', async (t) => {
  const root = fixture(t);
  fs.writeFileSync(path.join(root,'large.json'), JSON.stringify({ text: 'x'.repeat(65537) }), { mode: 0o600 });
  await assert.rejects(readPrivateJsonDirectory(root), { code: 'RECEIPT_FILE_LIMIT' });
});
test('non-private files and directory symlinks are refused', async (t) => {
  const root = fixture(t);
  const target = path.join(root,'open.json');
  fs.writeFileSync(target, '{}'); fs.chmodSync(target, 0o644);
  await assert.rejects(readPrivateJsonRecord(target), { code: 'UNSAFE_RECEIPT' });
  const alias = path.join(root,'alias'); fs.symlinkSync(root, alias);
  await assert.rejects(readPrivateJsonDirectory(alias), { code: 'UNSAFE_RECEIPT' });
});
test('native child timeout returns a bounded failure rather than claiming empty receipts', async (t) => {
  const root = fixture(t);
  // One millisecond is deliberately shorter than a fresh Node process can load
  // this worker. This tests actual child deadline handling, not mocked claims.
  const start = performance.now();
  await assert.rejects(readPrivateJsonDirectory(root, { timeoutMs: 1 }), { code: 'RECEIPT_READ_TIMEOUT', stage: 'RECEIPT_READ' });
  assert.ok(performance.now() - start < 5000);
});
test('invalid timeout configuration is rejected without starting a reader', async (t) => {
  const root = fixture(t);
  for (const timeoutMs of [null, 0, -1, NaN, 60001, '1']) {
    if (timeoutMs === null) continue; // null deliberately selects the default.
    await assert.rejects(readPrivateJsonDirectory(root, { timeoutMs }), /RECEIPT_READ_TIMEOUT_INVALID/);
  }
});
