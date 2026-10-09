'use strict';
// Run the production operation/receiver functions. Fixture streams are small;
// the byte ceiling is lowered only in this VM to exercise boundaries cheaply.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const source = fs.readFileSync(path.resolve(__dirname, '../public/app.js'), 'utf8');
function between(start, end) {
  const first = source.indexOf(start), last = source.indexOf(end, first);
  assert(first >= 0 && last > first, `Production helper boundaries must exist: ${start}`);
  return source.slice(first, last);
}
const timers = new Map(), busy = [];
let nextTimer = 0;
const context = { assert, AbortController, DOMException, Blob, Promise, Uint8Array,
  activeController: null, sessionLocked: false, timeoutId: null, timedOut: false,
  MAX_DOWNLOAD_BYTES: 8,
  setBusy(kind) { busy.push(kind); },
  window: { setTimeout(fn) { const id = ++nextTimer; timers.set(id, fn); return id; },
    clearTimeout(id) { timers.delete(id); } },
  getURL() { return 'https://fixture.example/watch/1'; }, getCookies() { return ''; },
  selectedMediaType() { return 'video'; }, withCookies(payload) { return payload; },
  mediaLabel() { return 'vídeo'; }, showStatus() {}, renderMetadata() {}, showOperationError() {},
};
vm.createContext(context);
vm.runInContext([
  between('  function startOperation(', '  async function responseError('),
  between('  async function receiveDownload(', '  async function download('),
  between('  async function inspect(', '  function formatBytes('),
].join('\n'), context);
const { startOperation, endOperation, ensureOperation, receiveDownload } = context;
const headers = values => ({ get(name) { return values[name] || null; } });
function response(parts, values = {}, options = {}) {
  let index = 0, cancelled = 0, released = 0;
  const reader = {
    async read() {
      if (options.error && index === parts.length) throw options.error;
      return index < parts.length ? { value: new Uint8Array(parts[index++]), done: false } : { done: true };
    },
    async cancel() { cancelled += 1; },
    releaseLock() { released += 1; },
  };
  return { headers: headers(values), body: { getReader() { return reader; }, async cancel() { cancelled += 1; } },
    counts() { return { cancelled, released, reads: index }; } };
}
const controller = () => startOperation('download');
(async () => {
  const first = controller(), oldTimer = timers.get(context.timeoutId);
  const second = controller();
  oldTimer();
  assert(!second.signal.aborted, 'An old timeout must not abort a newer request.');
  endOperation(first);
  assert.equal(context.activeController, second, 'Old cleanup cannot release a newer operation.');
  assert.throws(() => ensureOperation(first), { name: 'AbortError' });
  context.sessionLocked = true;
  assert.throws(() => ensureOperation(second), { name: 'AbortError' });
  const busyBeforeLock = busy.length;
  endOperation(second);
  assert.equal(busy.length, busyBeforeLock, 'A signed-out account cannot dispatch an idle/resume event.');
  context.sessionLocked = false;

  // A response can already be buffered when sign-out cancels the fetch.
  // Resolving its JSON afterward must not paint metadata or reopen the studio.
  let finishMetadata;
  const metadataEvents = [];
  context.window.OndaAuth = { async fetch() { return { ok: true,
    json() { return new Promise(resolve => { finishMetadata = resolve; }); } }; } };
  context.renderMetadata = () => metadataEvents.push('metadata');
  context.showStatus = kind => metadataEvents.push(kind);
  const inspection = context.inspect();
  await new Promise(setImmediate);
  context.sessionLocked = true;
  context.activeController.abort();
  finishMetadata({ title: 'Private fixture' });
  await inspection;
  assert.deepEqual(metadataEvents, ['loading'], 'A buffered private response cannot update a signed-out page.');
  context.sessionLocked = false;

  const full = response([[1, 2], [3, 4]], { 'Content-Length': '4' });
  const progress = [];
  const blob = await receiveDownload(full, controller(), 'video/mp4', (done, total) => progress.push([done, total]));
  assert.deepEqual([...new Uint8Array(await blob.arrayBuffer())], [1, 2, 3, 4]);
  assert.deepEqual(progress, [[2, 4], [4, 4]]);
  assert.deepEqual(full.counts(), { cancelled: 0, released: 1, reads: 2 });

  const truncated = response([[1, 2]], { 'Content-Length': '4' });
  await assert.rejects(receiveDownload(truncated, controller(), 'audio/mpeg'), /incompleta/);
  assert.equal(truncated.counts().released, 1);
  assert.equal(truncated.counts().cancelled, 1);
  const overDeclared = response([[1, 2, 3]], { 'Content-Length': '2' });
  await assert.rejects(receiveDownload(overDeclared, controller(), 'audio/mpeg'), /inesperado/);
  assert.equal(overDeclared.counts().cancelled, 1);

  const advertisedHuge = response([[1]], { 'Content-Length': '9' });
  await assert.rejects(receiveDownload(advertisedHuge, controller(), 'audio/mpeg'), /100 MB/);
  assert.equal(advertisedHuge.counts().reads, 0, 'Oversized responses stop before allocating output.');
  assert.equal(advertisedHuge.counts().cancelled, 1);
  const unadvertisedHuge = response([[1, 2, 3, 4, 5], [6, 7, 8, 9]]);
  await assert.rejects(receiveDownload(unadvertisedHuge, controller(), 'audio/mpeg'), /100 MB/);
  assert.equal(unadvertisedHuge.counts().released, 1);

  const encoded = response([[1, 2, 3, 4]], { 'Content-Length': '2', 'Content-Encoding': 'gzip' });
  assert.equal((await receiveDownload(encoded, controller(), 'audio/mpeg')).size, 4,
    'Decoded responses must not compare output size to compressed wire size.');
  const failed = response([[1]], {}, { error: new TypeError('fixture connection lost') });
  await assert.rejects(receiveDownload(failed, controller(), 'audio/mpeg'), /connection lost/);
  assert.equal(failed.counts().released, 1);
  assert.equal(failed.counts().cancelled, 1);

  let settle, cancelled = 0, released = 0;
  const waiting = { headers: headers({}), body: { getReader() { return {
    read() { return new Promise(resolve => { settle = resolve; }); },
    async cancel() { cancelled += 1; settle?.({ done: true }); },
    releaseLock() { released += 1; },
  }; } } };
  const pendingController = controller();
  const pending = receiveDownload(waiting, pendingController, 'video/mp4');
  pendingController.abort();
  await assert.rejects(pending, { name: 'AbortError' });
  assert(cancelled >= 1);
  assert.equal(released, 1);

  const fallback = { headers: headers({ 'Content-Length': '4' }), async blob() { return new Blob([new Uint8Array([1, 2])]); } };
  await assert.rejects(receiveDownload(fallback, controller(), 'audio/mpeg'), /incompleta/);
  console.log('Download transport: ownership, abort, bounded bytes, truncation, decoding and reader cleanup passed.');
})().catch(error => { console.error(error); process.exitCode = 1; });
