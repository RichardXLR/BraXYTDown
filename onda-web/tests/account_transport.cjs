'use strict';
// Run the actual account synchronization and streaming transport in an isolated
// browser-like context. Every request is supplied by the fixture; no network,
// Clerk account or storage credentials are used.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const source = fs.readFileSync(path.resolve(__dirname, '../public/account.js'), 'utf8');
const start = source.indexOf('  const gate');
const end = source.indexOf('  async function refreshAccount');
assert(start > 0 && end > start, 'Account transport source boundaries must exist.');
const tested = source.slice(start, end);
const saved = new Map();
const context = {
  assert, console, URL, TextEncoder, Headers, Request, Response, ReadableStream, AbortController, DOMException,
  document: { getElementById: () => null },
  location: { href: 'https://onda-audio.vercel.app', origin: 'https://onda-audio.vercel.app' },
  navigator: { onLine: true },
  window: { Clerk: { user: { id: 'user_test' }, session: { getToken: async () => 'test-only' } } },
  dispatchEvent: () => {}, CustomEvent: class {},
  setTimeout: () => 1, clearTimeout: () => {},
  localStorage: { getItem: key => saved.get(key) || null, setItem: (key, value) => saved.set(key, value) },
};
context.fetch = async () => { throw new Error('A local test must supply its own fetch implementation.'); };
const cases = `
(async () => {
  accountReady = true; clerkLoaded = true; userId = 'user_test';
  const events = []; let fetchSignal;
  function stalled(statusCode, options) {
    let bodyController, cancelled = false;
    fetchSignal = options.signal;
    const stream = new ReadableStream({
      start(controller) {
        bodyController = controller;
        options.signal.addEventListener('abort', () => {
          events.push('abort');
          if (!cancelled) controller.error(new DOMException('Fetch aborted', 'AbortError'));
        }, { once: true });
      },
      cancel() { cancelled = true; events.push('reader-cancel'); },
    });
    return new Response(stream, { status: statusCode });
  }
  fetch = async (url, options) => stalled(409, options);
  const conflict = await authenticatedFetch('/api/account/state');
  assert.equal(conflict.status, 409);
  await conflict.body.cancel();
  assert.deepEqual(events, ['reader-cancel', 'abort']);
  assert.equal(fetchSignal.aborted, true); assert.equal(requests.size, 0);

  // Exercise the actual flush loop: conflict body cancellation, remote reload,
  // field merge and successful revision retry, without network or credentials.
  const remote = defaults(); remote.preferences.theme = 'light';
  state = defaults(); state.preferences.accent = 'violet'; dirty.set('preferences.accent', ++changeSequence);
  const calls = [];
  fetch = async (url, options) => {
    calls.push([url, options.method || 'GET']);
    if (calls.length === 1) return stalled(409, options);
    if (calls.length === 2) return Response.json({ schema: 1, revision: 3, state: remote });
    const update = JSON.parse(options.body);
    assert.equal(update.base_revision, 3);
    assert.equal(update.state.preferences.accent, 'violet');
    assert.equal(update.state.preferences.theme, 'light');
    return Response.json({ schema: 1, revision: 4, state: update.state });
  };
  assert.equal(await flush(), true);
  assert.deepEqual(calls.map(item => item[1]), ['PUT', 'GET', 'PUT']);
  assert.equal(revision, 4); assert.equal(dirty.size, 0); assert.equal(requests.size, 0);

  // Session logout must still interrupt an active file stream.
  fetch = async (url, options) => stalled(200, options);
  const download = await authenticatedFetch('/api/download');
  const reading = download.body.getReader().read();
  const failedRead = assert.rejects(reading, { name: 'AbortError' });
  abortRequests();
  await failedRead;
  assert.equal(requests.size, 0); assert.equal(fetchSignal.aborted, true);

  // The original caller signal retains the same cancellation behavior.
  const caller = new AbortController();
  const requested = await authenticatedFetch('/api/download', { signal: caller.signal });
  const pending = requested.body.getReader().read();
  const failedCallerRead = assert.rejects(pending, { name: 'AbortError' });
  caller.abort(); await failedCallerRead;
  assert.equal(requests.size, 0); assert.equal(fetchSignal.aborted, true);
  console.log('Account transport: 409 cancellation/retry, revision merge, logout and caller stream cancellation passed.');
})()
`;
// Unresolved stream reads alone do not keep Node alive. Keep a watchdog active
// so a cancellation deadlock fails instead of silently exiting successfully.
const watchdog = setTimeout(() => {
  console.error('Account transport timed out while waiting for stream cancellation.');
  process.exitCode = 1;
}, 5000);
(async () => { await vm.runInNewContext(tested + cases, context); })()
  .catch(error => { console.error(error); process.exitCode = 1; })
  .finally(() => clearTimeout(watchdog));
