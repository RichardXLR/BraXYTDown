'use strict';
// Use the production synchronization functions with controlled request timing.
// Delayed reads must not undo a write which completed after those reads began.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const source = fs.readFileSync(path.resolve(__dirname, '../public/account.js'), 'utf8');
const start = source.indexOf('  const gate');
const end = source.indexOf('  function showLoading');
assert(start > 0 && end > start, 'Account synchronization source boundaries must exist.');
const context = {
  assert, console, URL, TextEncoder, Headers, Request, Response, ReadableStream,
  AbortController, DOMException,
  document: { getElementById: () => null },
  location: { href: 'https://onda.example/', origin: 'https://onda.example' },
  navigator: { onLine: true },
  window: { Clerk: { user: { id: 'user_fixture' }, session: { getToken: async () => 'fixture-only' } } },
  dispatchEvent() {}, CustomEvent: class {}, setTimeout: () => 1, clearTimeout() {},
  localStorage: { getItem: () => null, setItem() {} },
  fetch: async () => { throw new Error('Every local request must be supplied by the fixture.'); },
};
const cases = `
(async () => {
  accountReady = true; clerkLoaded = true; userId = 'user_fixture'; revision = 3;
  let resolveRead, reads = 0;
  const staleSnapshot = copy(state);
  fetch = async (_url, options) => {
    if ((options.method || 'GET') === 'GET') {
      reads += 1;
      return new Promise(resolve => { resolveRead = resolve; });
    }
    const update = JSON.parse(options.body);
    assert.equal(update.base_revision, 3);
    return Response.json({ schema: 1, revision: 4, state: update.state });
  };
  const staleRefresh = refreshAccount();
  while (!resolveRead) await Promise.resolve();
  changeSection('preferences', { ...state.preferences, accent: 'violet' });
  assert.equal(await flush(), true);
  assert.equal(revision, 4); assert.equal(dirty.size, 0);
  resolveRead(Response.json({ schema: 1, revision: 3, state: staleSnapshot }));
  await staleRefresh;
  assert.equal(revision, 4, 'A delayed GET cannot roll back an acknowledged revision.');
  assert.equal(state.preferences.accent, 'violet', 'A delayed GET cannot undo a saved preference.');

  // Inverse timing: a newer GET completes before the acknowledgement of a PUT
  // that was committed earlier. Preserve the newer snapshot and resend pending
  // fields through its current CAS revision instead of claiming they are saved.
  state = defaults(); revision = 3;
  let resolveAdvancedRead, resolveOlderAck;
  const inverseWrites = [];
  fetch = async (_url, options) => {
    if ((options.method || 'GET') === 'GET') {
      return new Promise(resolve => { resolveAdvancedRead = resolve; });
    }
    const update = JSON.parse(options.body); inverseWrites.push(update);
    if (inverseWrites.length === 1) return new Promise(resolve => { resolveOlderAck = resolve; });
    assert.equal(update.base_revision, 5, 'Resend must use the current account revision.');
    assert.equal(update.state.preferences.density, 'compact', 'Resend must preserve the newer field from another tab.');
    assert.equal(update.state.preferences.accent, 'violet', 'The local pending field must be resent.');
    return Response.json({ schema: 1, revision: 6, state: update.state });
  };
  const advancedRefresh = refreshAccount();
  while (!resolveAdvancedRead) await Promise.resolve();
  changeSection('preferences', { ...state.preferences, accent: 'violet' });
  const delayedWrite = flush();
  while (!resolveOlderAck) await Promise.resolve();
  const advancedSnapshot = defaults(); advancedSnapshot.preferences.density = 'compact';
  resolveAdvancedRead(Response.json({ schema: 1, revision: 5, state: advancedSnapshot }));
  await advancedRefresh;
  assert.equal(revision, 5); assert.equal(dirty.has('preferences.accent'), true);
  resolveOlderAck(Response.json({ schema: 1, revision: 4, state: inverseWrites[0].state }));
  assert.equal(await delayedWrite, true);
  assert.equal(revision, 6, 'An obsolete PUT acknowledgement cannot roll back the newer GET.');
  assert.equal(state.preferences.density, 'compact');
  assert.equal(state.preferences.accent, 'violet');
  assert.deepEqual(inverseWrites.map(update => update.base_revision), [3, 5]);
  assert.equal(dirty.size, 0);

  // A GET used to recover a 409 is subject to the same request ordering.
  state = defaults(); revision = 3;
  let resolveBackgroundRead, resolveConflictRead, conflictReads = 0;
  const conflictWrites = [];
  fetch = async (_url, options) => {
    if ((options.method || 'GET') === 'GET') {
      conflictReads += 1;
      return new Promise(resolve => {
        if (conflictReads === 1) resolveBackgroundRead = resolve;
        else resolveConflictRead = resolve;
      });
    }
    const update = JSON.parse(options.body); conflictWrites.push(update);
    if (conflictWrites.length === 1) return new Response(null, { status: 409 });
    assert.equal(update.base_revision, 5, 'An older conflict reload must not roll back the resend revision.');
    assert.equal(update.state.preferences.density, 'compact');
    return Response.json({ schema: 1, revision: 6, state: update.state });
  };
  const conflictBackground = refreshAccount();
  while (!resolveBackgroundRead) await Promise.resolve();
  changeSection('preferences', { ...state.preferences, accent: 'violet' });
  const conflictingWrite = flush();
  while (!resolveConflictRead) await Promise.resolve();
  resolveBackgroundRead(Response.json({ schema: 1, revision: 5, state: advancedSnapshot }));
  await conflictBackground;
  resolveConflictRead(Response.json({ schema: 1, revision: 4, state: defaults() }));
  assert.equal(await conflictingWrite, true);
  assert.equal(revision, 6); assert.equal(state.preferences.density, 'compact');
  assert.equal(state.preferences.accent, 'violet'); assert.equal(dirty.size, 0);
  assert.deepEqual(conflictWrites.map(update => update.base_revision), [3, 5]);

  let failRead;
  fetch = async (_url, options) => {
    if ((options.method || 'GET') === 'GET') {
      return new Promise((_resolve, reject) => { failRead = reject; });
    }
    const update = JSON.parse(options.body);
    return Response.json({ schema: 1, revision: 7, state: update.state });
  };
  const obsoleteRead = refreshAccount();
  while (!failRead) await Promise.resolve();
  changeSection('preferences', { ...state.preferences, theme: 'light' });
  assert.equal(await flush(), true);
  failRead(new TypeError('Local fixture network failure'));
  await obsoleteRead;
  assert.equal(syncState, 'saved', 'An obsolete read failure cannot label a completed write as pending.');

  let resolveShared, sharedCalls = 0;
  fetch = async () => {
    sharedCalls += 1;
    return new Promise(resolve => { resolveShared = resolve; });
  };
  const first = refreshAccount();
  const second = refreshAccount();
  while (!resolveShared) await Promise.resolve();
  assert.equal(sharedCalls, 1, 'Focus, reconnect and manual refresh must share the current read.');
  const newest = copy(state); newest.preferences.theme = 'dark';
  resolveShared(Response.json({ schema: 1, revision: 8, state: newest }));
  await Promise.all([first, second]);
  assert.equal(revision, 8); assert.equal(state.preferences.theme, 'dark');

  // Changes made during a remote read still belong to the local pending merge.
  let resolveEditingRead;
  fetch = async () => new Promise(resolve => { resolveEditingRead = resolve; });
  const editingRefresh = refreshAccount();
  while (!resolveEditingRead) await Promise.resolve();
  changeSection('preferences', { ...state.preferences, accent: 'cyan' });
  const newerRemote = copy(newest); newerRemote.preferences.density = 'compact';
  resolveEditingRead(Response.json({ schema: 1, revision: 9, state: newerRemote }));
  await editingRefresh;
  assert.equal(revision, 9);
  assert.equal(state.preferences.accent, 'cyan');
  assert.equal(state.preferences.density, 'compact');
  assert.equal(dirty.has('preferences.accent'), true);
  assert.equal(requests.size, 0);
  console.log('Account consistency: delayed reads, shared refresh and pending field merge passed.');
})()
`;
const watchdog = setTimeout(() => {
  console.error('Account consistency timed out.'); process.exitCode = 1;
}, 5000);
(async () => { await vm.runInNewContext(source.slice(start, end) + cases, context); })()
  .catch(error => { console.error(error); process.exitCode = 1; })
  .finally(() => clearTimeout(watchdog));
