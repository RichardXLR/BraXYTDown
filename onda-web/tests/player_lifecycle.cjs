'use strict';
// Execute the complete player against event/transport fixtures. Deferred
// requests intentionally ignore abort to reproduce stale-response races.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
class Events {
  constructor() { this.listeners = new Map(); }
  addEventListener(type, fn) {
    if (!this.listeners.has(type)) this.listeners.set(type, []);
    this.listeners.get(type).push(fn);
  }
  emit(type, detail) { for (const fn of this.listeners.get(type) || []) fn({ type, detail }); }
}
class Element extends Events {
  constructor() { super(); this.value = ''; this.dataset = {}; this.attributes = new Map(); this.hidden = true; }
  setAttribute(key, value) { this.attributes.set(key, value); }
  removeAttribute(key) { this.attributes.delete(key); }
  hasAttribute(key) { return this.attributes.has(key); }
  get src() { return this.attributes.get('src'); }
  set src(value) { this.attributes.set('src', value); }
  pause() {}
  load() {}
}
const document = new Events(), window = new Events();
const classes = new Set(), elements = new Map();
document.body = { classList: { contains(value) { return classes.has(value); } } };
document.hidden = false;
document.getElementById = id => {
  if (!elements.has(id)) elements.set(id, new Element());
  return elements.get(id);
};
document.querySelector = () => ({ dataset: { workspaceTab: 'download' } });
const input = document.getElementById('video-url');
const region = document.getElementById('source-player');
const frame = document.getElementById('source-embed-player');
let observer, nextTimer = 0;
const timers = new Map(), requests = [], sessionCalls = [];
window.OndaAuth = { fetch(url, options) {
  let finish;
  const pending = new Promise(resolve => { finish = resolve; });
  requests.push({ url, options, finish });
  return pending;
} };
window.OndaSession = { requestOptions() { sessionCalls.push(true); return { cookies: 'private fixture' }; } };
const context = { window, document, URL, AbortController, DOMException, Date,
  setTimeout(fn) { const id = ++nextTimer; timers.set(id, fn); return id; },
  clearTimeout(id) { timers.delete(id); },
  MutationObserver: class { constructor(fn) { observer = fn; } observe() {} disconnect() {} },
};
vm.runInNewContext(fs.readFileSync(path.resolve(__dirname, '../public/player.js'), 'utf8'), context);
const tick = () => new Promise(setImmediate);
async function runTimers() {
  const callbacks = [...timers.values()]; timers.clear();
  for (const fn of callbacks) fn();
  await tick();
}
function reply(request, id = 'abcdefghijk') {
  request.finish({ ok: true, headers: { get() { return 'application/json'; } }, async json() {
    return { kind: 'embed', url: `https://www.youtube-nocookie.com/embed/${id}`, provider: 'YouTube' };
  } });
}
(async () => {
  input.value = 'https://www.youtube.com/watch?v=abcdefghijk';
  input.emit('input'); window.emit('onda:source');
  await runTimers();
  assert.equal(requests.length, 1);
  await window.OndaPlayer.refresh();
  assert.equal(requests.length, 1, 'Concurrent events for the same URL must reuse extraction.');
  assert.deepEqual(JSON.parse(requests[0].options.body), { url: input.value });
  assert.equal(sessionCalls.length, 0, 'Automatic previews never read or send credentials.');

  const oldRequest = requests[0];
  input.value = 'https://www.youtube.com/watch?v=lmnopqrstuv';
  input.emit('input');
  assert(oldRequest.options.signal.aborted);
  assert(region.hidden, 'Editing a URL immediately removes the previous player.');
  reply(oldRequest);
  await tick();
  assert(!frame.hasAttribute('src'), 'A late response from a superseded URL cannot restore its media.');
  await runTimers();
  assert.equal(requests.length, 2);

  window.emit('onda:tab', { tab: 'credits' });
  assert(requests[1].options.signal.aborted);
  reply(requests[1], 'lmnopqrstuv'); await tick();
  assert(!frame.hasAttribute('src'));
  input.emit('input'); await runTimers();
  assert.equal(requests.length, 2, 'Inactive tabs must not spend extraction capacity.');
  window.emit('onda:tab', { tab: 'download' }); await tick();
  assert.equal(requests.length, 3, 'Returning to Download resumes a cancelled preview.');

  document.hidden = true; document.emit('visibilitychange');
  assert(requests[2].options.signal.aborted);
  reply(requests[2], 'lmnopqrstuv'); await tick();
  assert(!frame.hasAttribute('src'));
  document.hidden = false; document.emit('visibilitychange'); await tick();
  assert.equal(requests.length, 4);
  reply(requests[3], 'lmnopqrstuv'); await tick();
  assert.equal(frame.src, 'https://www.youtube-nocookie.com/embed/lmnopqrstuv');
  assert.equal(region.hidden, false);

  classes.add('intro-open'); observer();
  assert(!frame.hasAttribute('src'), 'The opening cannot play alongside a provider frame.');
  classes.delete('intro-open'); observer(); await tick();
  assert.equal(requests.length, 4, 'Returning from the opening reuses the validated descriptor.');
  assert(frame.hasAttribute('src'));

  const expired = window.OndaPlayer.refresh({ force: true }); await tick();
  assert.equal(requests.length, 5);
  await runTimers();
  assert(requests[4].options.signal.aborted);
  reply(requests[4], 'lmnopqrstuv'); await expired;
  assert(region.hidden, 'A buffered response arriving after the timeout cannot publish a player.');
  assert.match(document.getElementById('source-player-status').textContent, /demorou demais/);

  const explicit = window.OndaPlayer.refresh({ withCookies: true, force: true });
  await tick();
  assert.equal(sessionCalls.length, 1);
  assert.equal(JSON.parse(requests[5].options.body).cookies, 'private fixture');
  window.emit('onda:auth', { signedIn: false });
  assert(requests[5].options.signal.aborted);
  reply(requests[5], 'lmnopqrstuv'); await explicit;
  assert(region.hidden);
  assert(!frame.hasAttribute('src'), 'Signing out invalidates private descriptors and late replies.');
  await window.OndaPlayer.refresh();
  assert.equal(requests.length, 6, 'Signed-out events cannot start new preview requests.');
  console.log('Player lifecycle: request coalescing, inactive tabs, stale replies, visibility and sign-out privacy passed.');
})().catch(error => { console.error(error); process.exitCode = 1; });
