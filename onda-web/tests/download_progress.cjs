'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const context = { window: {}, TextDecoder, Uint8Array, DataView, Blob, DOMException, AbortController, performance };
vm.createContext(context);
vm.runInContext(fs.readFileSync(path.resolve(__dirname, '../public/download-stream.js'), 'utf8'), context);
const { receive, CONTENT_TYPE } = context.window.OndaDownloadStream;
function frame(kind, payload) {
  const body = typeof payload === 'object' && !(payload instanceof Uint8Array) ? new TextEncoder().encode(JSON.stringify(payload)) : payload;
  const bytes = new Uint8Array(5 + body.length);
  bytes[0] = kind;
  new DataView(bytes.buffer).setUint32(1, body.length, false);
  bytes.set(body, 5);
  return bytes;
}
const json = value => frame(1, value);
const file = (mime = 'video/mp4', size = 4) => json({ type: 'file', name: 'Owned.mp4', title: 'Owned', mime, size, recovery: { attempts: 2, resumed: true } });
const complete = size => json({ type: 'complete', size });
function response(parts, options = {}) {
  let index = 0, cancelled = 0, released = 0, settle;
  const reader = {
    async read() {
      options.afterRead?.(index);
      if (options.pending && index === parts.length) return new Promise(resolve => { settle = resolve; });
      if (options.error && index === parts.length) throw options.error;
      return index < parts.length ? { value: parts[index++], done: false } : { done: true };
    },
    async cancel() { cancelled++; settle?.({ done: true }); },
    releaseLock() { released++; },
  };
  return { headers: { get() { return options.mime || CONTENT_TYPE; } }, body: { getReader() { return reader; }, async cancel() { cancelled++; } }, counts() { return { cancelled, released, reads: index }; } };
}
const owned = () => new Uint8Array([1, 2, 3, 4]);
const normal = () => [json({ type: 'progress', stage: 'extracting' }), file(), frame(2, owned()), json({ type: 'progress', stage: 'ready', downloadedBytes: 4, totalBytes: 4 }), complete(4)];
async function reject(parts, expression = /incompleta|inválidos/, options = {}, receiverOptions = {}) {
  const stream = response(parts, options);
  await assert.rejects(receive(stream, receiverOptions), expression);
  assert.equal(stream.counts().released, 1);
  assert(stream.counts().cancelled >= 1);
}
const cases = {
  async split_every_byte() {
    const wire = Buffer.concat(normal().map(part => Buffer.from(part)));
    const events = [];
    const stream = response([...wire].map(byte => new Uint8Array([byte])));
    const result = await receive(stream, { onEvent: event => events.push(event) });
    assert.deepEqual([...new Uint8Array(await result.blob.arrayBuffer())], [1, 2, 3, 4]);
    assert.equal(result.metadata.recovery.attempts, 2);
    assert.equal(result.blob.type, 'video/mp4');
    assert(events.some(event => event.stage === 'delivering' && event.measurement === 'browser' && event.downloadedBytes === 4));
    assert.equal(events.at(-1).stage, 'ready');
    assert.deepEqual(stream.counts(), { cancelled: 0, released: 1, reads: wire.length });
  },
  async several_frames_one_read() {
    const events = [];
    const wire = new Uint8Array(Buffer.concat(normal().map(part => Buffer.from(part))));
    const result = await receive(response([wire]), { onEvent: event => events.push(event) });
    assert.equal(result.blob.size, 4);
    assert.equal(events.filter(event => event.stage === 'ready').length, 1);
  },
  async missing_footer() { await reject(normal().slice(0, -1)); },
  async incomplete_binary() { await reject([file(), frame(2, owned()).slice(0, -1)]); },
  async incomplete_header() { await reject([file(), new Uint8Array([2, 0, 0])]); },
  async duplicate_file() { await reject([file(), file(), frame(2, owned()), complete(4)]); },
  async duplicate_complete() { await reject([...normal(), complete(4)]); },
  async binary_before_metadata() { await reject([frame(2, owned()), file(), complete(4)]); },
  async exceeds_declared_size() { await reject([file('video/mp4', 3), frame(2, owned()), complete(3)]); },
  async exceeds_user_budget() { await reject(normal(), undefined, {}, { maxBytes: 3 }); },
  async excessive_frame() {
    for (const [kind, length] of [[1, 16385], [2, 1048577]]) {
      const header = new Uint8Array(5); header[0] = kind;
      new DataView(header.buffer).setUint32(1, length, false);
      await reject([file(), header]);
    }
  },
  async unknown_frame_kind() { await reject([file(), frame(3, owned())]); },
  async footer_wrong_size() { await reject([file(), frame(2, owned()), complete(3)]); },
  async rejects_invalid_progress() {
    await reject([json({ type: 'progress', stage: 'extracting', downloadedBytes: 'private' })]);
    await reject([json({ type: 'progress', stage: 'unknown' })]);
  },
  async propagated_source_error() {
    await reject([json({ type: 'error', code: 'timeout', error: 'A origem demorou demais.', recovery: { attempts: 3, exhausted: true } })], error => error.code === 'timeout' && error.recovery.exhausted);
  },
  async abort_pending_reader() {
    const controller = new AbortController();
    const stream = response([file()], { pending: true });
    const pending = receive(stream, { signal: controller.signal });
    await new Promise(setImmediate);
    controller.abort();
    await assert.rejects(pending, { name: 'AbortError' });
    assert(stream.counts().cancelled >= 1);
    assert.equal(stream.counts().released, 1);
  },
  async stale_operation_after_buffered_read() {
    let owns = true;
    await reject(normal(), { name: 'AbortError' }, { afterRead(index) { if (index === 2) owns = false; } },
      { ensureOperation() { if (!owns) throw new DOMException('Changed user', 'AbortError'); } });
  },
  async no_ready_before_valid_eof() {
    const events = [];
    await reject(normal(), /connection lost/, { error: new TypeError('connection lost') }, { onEvent: event => events.push(event) });
    assert(!events.some(event => event.stage === 'ready'));
  },
  async all_server_mimes() {
    for (const mime of ['audio/mpeg', 'audio/mp4', 'audio/wav', 'audio/flac', 'audio/ogg', 'audio/aac', 'audio/aiff', 'video/mp4', 'video/webm', 'video/x-matroska', 'video/quicktime']) {
      const result = await receive(response([file(mime), frame(2, owned()), complete(4)]));
      assert.equal(result.blob.type, mime);
    }
  },
  async rejects_preconditions_and_closes_body() {
    const wrongProtocol = response(normal(), { mime: 'video/mp4' });
    await assert.rejects(receive(wrongProtocol), /protocolo/);
    assert.deepEqual(wrongProtocol.counts(), { cancelled: 1, released: 0, reads: 0 });
    const aborted = response(normal());
    const controller = new AbortController(); controller.abort();
    await assert.rejects(receive(aborted, { signal: controller.signal }), { name: 'AbortError' });
    assert.deepEqual(aborted.counts(), { cancelled: 1, released: 0, reads: 0 });
  },
};
const selected = process.argv[2];
assert(cases[selected], `Unknown case ${selected}`);
cases[selected]().then(() => console.log(`${selected}: passed`)).catch(error => { console.error(error); process.exitCode = 1; });
