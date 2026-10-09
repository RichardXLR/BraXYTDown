'use strict';
// Run production handlers against deferred browser/API I/O. No network, auth,
// platform credentials or real user storage is used by these regressions.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const source = fs.readFileSync(path.resolve(__dirname, '../public/app.js'), 'utf8');
function between(start, end) {
  const first = source.indexOf(start), last = source.indexOf(end, first);
  assert(first >= 0 && last > first, `Production boundaries must exist: ${start}`);
  return source.slice(first, last);
}
function fixture() {
  const fields = new Map(), timers = new Map(), saved = new Map(), notices = [];
  let nextTimer = 0;
  function field(id) {
    if (!fields.has(id)) {
      const listeners = new Map(), classes = new Set();
      fields.set(id, { value: '', checked: false, hidden: true, textContent: '', dataset: {},
        addEventListener(type, handler) { listeners.set(type, handler); },
        async emit(type) { return listeners.get(type)?.({ target: this }); },
        classList: { add(name) { classes.add(name); }, remove(name) { classes.delete(name); },
          contains(name) { return classes.has(name); }, toggle(name, value) { if (value) classes.add(name); else classes.delete(name); } },
        setAttribute() {}, removeAttribute() {}, focus() {}, });
    }
    return fields.get(id);
  }
  const context = { URL, TextEncoder, AbortController, DOMException, Date, console,
    $: field, input: field('video-url'), activeController: null, sessionLocked: false, clipboardGeneration: 0,
    setTimeout(fn) { const id = ++nextTimer; timers.set(id, fn); return id; },
    clearTimeout(id) { timers.delete(id); },
    announce(text) { notices.push(text); }, updateSource() { notices.push('source-updated'); },
    showStatus(kind) { notices.push(kind); },
    window: { OndaAccount: { storage: { setItem(key, value) { saved.set(key, value); } } } },
  };
  context.window.setTimeout = context.setTimeout;
  context.window.clearTimeout = context.clearTimeout;
  vm.createContext(context);
  return { context, field, timers, saved, notices };
}
const cases = {
  async reset_persists() {
    const { context, field, saved } = fixture();
    context.form = { elements: Object.fromEntries([
      ['media_type', 'video'], ['format', 'mp3'], ['quality', '320'],
      ['video_format', 'mp4'], ['video_resolution', '2160'],
    ].map(([name, value]) => [name, { value }])) };
    context.draftRestored = true; context.DRAFT_KEY = 'fixture.draft'; context.updateTools = () => {};
    context.input.value = 'https://media.example.org/own.mp4';
    field('trim-start').value = '0:30'; field('trim-end').value = '0:45';
    field('strip-metadata').checked = false;
    field('normalize-audio').checked = true; field('mute-video').checked = true;
    vm.runInContext([
      between('  function saveDraft(', '  function restoreDraft('),
      between('  function resetTools(', '  function describeCookies('),
      between("  $('reset-tools').addEventListener(", "  input.addEventListener('input'"),
    ].join('\n'), context);
    await field('reset-tools').emit('click');
    assert(saved.has('fixture.draft'), 'Resetting tools must save the reset values, so reopening the app does not restore the previous trim/mute settings.');
    const draft = JSON.parse(saved.get('fixture.draft'));
    assert.equal(draft.trim_start, ''); assert.equal(draft.trim_end, '');
    assert.equal(draft['strip-metadata'], true);
    assert.equal(draft['normalize-audio'], false); assert.equal(draft['mute-video'], false);
    assert.equal(draft.video_resolution, '2160');
  },
  async validation_cancelled_buffer() {
    const { context, field, timers } = fixture();
    context.cookieValidationController = null; context.validatedCookieLink = '';
    context.getURL = () => 'https://www.youtube.com/watch?v=abcdefghijk';
    context.getCookies = () => '[{"domain":".youtube.com","name":"fixture","value":"fixture"}]';
    context.input.value = context.getURL(); field('cookies-input').value = context.getCookies();
    context.cookieContent = text => text;
    context.updateCookieStatus = text => { field('cookies-status').textContent = text; };
    context.updateCookieApplicability = () => {};
    let finishJSON;
    context.window.OndaAuth = { async fetch() { return { ok: true,
      json() { return new Promise(resolve => { finishJSON = resolve; }); } }; } };
    vm.runInContext(between('  async function validateCookies(', '  function clearSessionFields('), context);
    const pending = context.validateCookies(); await new Promise(setImmediate);
    for (const fn of [...timers.values()]) fn();
    assert(context.cookieValidationController.signal.aborted);
    finishJSON({ validCookies: 1, ignoredCookies: 0, expiredCookies: 0 }); await pending;
    assert(!field('cookies-status').classList.contains('validated'), 'A buffered response arriving after timeout cannot validate a session.');
    assert.equal(context.validatedCookieLink, '');
    assert.match(field('cookies-status').textContent, /demorou demais/);
    assert.equal(context.cookieValidationController, null);
    assert.equal(field('validate-cookies').disabled, false);
  },
  async validation_current_response() {
    const { context, field } = fixture();
    context.cookieValidationController = null; context.validatedCookieLink = '';
    context.getURL = () => 'https://www.youtube.com/watch?v=abcdefghijk';
    context.getCookies = () => '[{"domain":".youtube.com","name":"fixture","value":"fixture"}]';
    context.input.value = context.getURL(); field('cookies-input').value = context.getCookies();
    context.cookieContent = text => text;
    context.updateCookieStatus = text => { field('cookies-status').textContent = text; };
    context.updateCookieApplicability = () => {};
    context.window.OndaAuth = { async fetch(endpoint, options) {
      assert.equal(endpoint, '/api/session/validate');
      assert.deepEqual(JSON.parse(options.body), { url: context.getURL(), cookies: context.getCookies() });
      return { ok: true, async json() { return { validCookies: 2, ignoredCookies: 1, expiredCookies: 1 }; } };
    } };
    vm.runInContext(between('  async function validateCookies(', '  function clearSessionFields('), context);
    await context.validateCookies();
    assert(field('cookies-status').classList.contains('validated'));
    assert.equal(context.validatedCookieLink, context.getURL());
    assert.match(field('cookies-status').textContent, /2 cookies compatíveis/);
    assert.equal(context.cookieValidationController, null);
    assert.equal(field('validate-cookies').disabled, false);
  },
  async paste_preserves_newer_link() {
    const { context, field, notices } = fixture();
    let finishClipboard;
    context.navigator = { clipboard: { readText() { return new Promise(resolve => { finishClipboard = resolve; }); } } };
    context.input.value = 'https://media.example.org/first.mp4';
    vm.runInContext(between("  $('paste-button').addEventListener(", "  $('example-button').addEventListener("), context);
    const pending = field('paste-button').emit('click');
    context.input.value = 'https://media.example.org/newer.mp4';
    finishClipboard('https://media.example.org/clipboard.mp4'); await pending;
    assert.equal(context.input.value, 'https://media.example.org/newer.mp4', 'A delayed clipboard permission must not overwrite a link edited while waiting.');
    assert(!notices.includes('source-updated'));
  },
  async paste_after_sign_out() {
    const { context, field, notices } = fixture();
    let finishClipboard;
    context.navigator = { clipboard: { readText() { return new Promise(resolve => { finishClipboard = resolve; }); } } };
    vm.runInContext(between("  $('paste-button').addEventListener(", "  $('example-button').addEventListener("), context);
    const pending = field('paste-button').emit('click');
    context.sessionLocked = true;
    finishClipboard('https://media.example.org/private-link.mp4'); await pending;
    assert.equal(context.input.value, '', 'Signing out must prevent a pending clipboard action from restoring account form content.');
    assert(!notices.includes('source-updated'));
  },
  async paste_current_action() {
    const { context, field, notices } = fixture();
    context.navigator = { clipboard: { async readText() { return ' https://media.example.org/own.mp4 '; } } };
    vm.runInContext(between("  $('paste-button').addEventListener(", "  $('example-button').addEventListener("), context);
    await field('paste-button').emit('click');
    assert.equal(context.input.value, 'https://media.example.org/own.mp4');
    assert(notices.includes('source-updated'));
    assert(notices.some(text => text.startsWith('Link colado.')));
  },
  async paste_preserves_link_edited_back_to_original() {
    const { context, field } = fixture();
    let finishClipboard;
    context.navigator = { clipboard: { readText() { return new Promise(resolve => { finishClipboard = resolve; }); } } };
    context.input.value = 'https://media.example.org/first.mp4';
    context.validatedCookieLink = ''; context.metadataURL = ''; context.metadata = null;
    context.inspectButton = field('inspect-button');
    context.detectSource = () => ''; context.updateCookieApplicability = () => {};
    context.invalidateCookieValidation = () => {}; context.saveDraft = () => {};
    context.isPublicURL = () => true;
    context.CustomEvent = class CustomEvent {};
    context.window.dispatchEvent = () => {};
    vm.runInContext([
      between('  function updateSource(', '  function updateChoices('),
      between("  input.addEventListener('input'", "  inspectButton.addEventListener("),
      between("  $('paste-button').addEventListener(", "  $('example-button').addEventListener("),
    ].join('\n'), context);
    const pending = field('paste-button').emit('click');
    context.input.value = 'https://media.example.org/newer.mp4'; await context.input.emit('input');
    context.input.value = 'https://media.example.org/first.mp4'; await context.input.emit('input');
    finishClipboard('https://media.example.org/stale-clipboard.mp4'); await pending;
    assert.equal(context.input.value, 'https://media.example.org/first.mp4', 'Returning to the original value is still a newer edit and must invalidate a pending paste.');
  },
  async paste_latest_request_wins_with_empty_clipboard() {
    const { context, field, notices } = fixture();
    const reads = [];
    context.navigator = { clipboard: { readText() { return new Promise(resolve => { reads.push(resolve); }); } } };
    vm.runInContext(between("  $('paste-button').addEventListener(", "  $('example-button').addEventListener("), context);
    const first = field('paste-button').emit('click');
    const second = field('paste-button').emit('click');
    assert.equal(reads.length, 2);
    reads[1](''); await second;
    assert.equal(context.input.value, '');
    reads[0]('https://media.example.org/stale-clipboard.mp4'); await first;
    assert.equal(context.input.value, '', 'A newer empty clipboard result must still supersede the earlier pending paste.');
    assert.equal(notices.filter(text => text === 'source-updated').length, 1);
  },
  async paste_older_rejection_does_not_replace_latest_feedback() {
    const { context, field, notices } = fixture();
    const reads = [];
    context.navigator = { clipboard: { readText() { return new Promise((resolve, reject) => { reads.push({ resolve, reject }); }); } } };
    vm.runInContext(between("  $('paste-button').addEventListener(", "  $('example-button').addEventListener("), context);
    const first = field('paste-button').emit('click');
    const second = field('paste-button').emit('click');
    reads[1].resolve(''); await second;
    reads[0].reject(new Error('Older denied permission')); await first;
    assert(!notices.includes('info'), 'A superseded paste cannot replace the latest clipboard feedback with an old permission error.');
    assert.equal(notices.filter(text => text === 'source-updated').length, 1);
  },
  async compatibility_preserves_maintenance() {
    const { context, field } = fixture();
    context.compatibilityStatusController = null; context.maintenanceLoadedAt = Date.now();
    field('healing-title').textContent = 'AutoCura automático ativo';
    field('healing-message').textContent = 'Última execução verificada.';
    context.window.OndaAuth = { async fetch() { throw new Error('fixture network unavailable'); } };
    vm.runInContext(between('  async function loadCompatibility(', '  function showCompatibilityResult('), context);
    await context.loadCompatibility();
    assert.equal(field('healing-title').textContent, 'AutoCura automático ativo', 'An independent compatibility request failure must not overwrite verified AutoCura status.');
    assert.equal(field('healing-message').textContent, 'Última execução verificada.');
    assert.equal(field('refresh-compatibility').disabled, false);
    assert.match(field('compatibility-state').textContent, /Não foi possível/);
  },
  async compatibility_failure_before_maintenance() {
    const { context, field } = fixture();
    context.compatibilityStatusController = null; context.maintenanceLoadedAt = 0;
    context.window.OndaAuth = { async fetch() { throw new Error('fixture network unavailable'); } };
    vm.runInContext(between('  async function loadCompatibility(', '  function showCompatibilityResult('), context);
    await context.loadCompatibility();
    assert.equal(field('healing-title').textContent, 'Configuração não confirmada');
    assert.equal(field('refresh-compatibility').disabled, false);
  },
  async direct_url_scope() {
    const { context } = fixture();
    context.validatedCookieLink = '';
    vm.runInContext([
      between('  function detectSource(', '  function updateSource('),
      between('  function cookiesMatchSource(', '  function updateCookieApplicability('),
    ].join('\n'), context);
    const cookies = JSON.stringify([{ domain: 'media.example.org', name: 'fixture', value: 'fixture' }]);
    for (const extension of ['mp3', 'm4a', 'mp4', 'webm', 'mov', 'wav', 'flac', 'aac', 'ogg',
      'opus', 'aif', 'aiff', 'avi', 'mkv', 'mpeg', 'mpg', 'm4v', 'wma', 'ts', 'm2ts']) {
      for (const suffix of ['#t=2', '?signature=fixture#t=2']) {
        const url = `https://media.example.org/own.${extension}${suffix}`;
        assert.equal(context.detectSource(url), 'Arquivo direto', `A direct .${extension} source is detected from its path, independent of query/fragment.`);
        assert.equal(context.cookiesMatchSource(cookies, url), false, 'Saved cookies must not be attached to a direct media request.');
      }
    }
    const spoofed = 'https://media.example.org/watch/123?format=.mp4';
    assert.notEqual(context.detectSource(spoofed), 'Arquivo direto', 'A filename-like query parameter is not the media path.');
    assert.equal(context.cookiesMatchSource(cookies, spoofed), true);
  },
  async trailing_dot_platform_scope() {
    const { context } = fixture();
    context.validatedCookieLink = '';
    vm.runInContext([
      between('  function detectSource(', '  function updateSource('),
      between('  function cookiesMatchSource(', '  function updateCookieApplicability('),
    ].join('\n'), context);
    const cookies = JSON.stringify([{ domain: '.youtube.com', name: 'fixture', value: 'fixture' }]);
    const url = 'https://www.youtube.com./watch?v=abcdefghijk';
    assert.equal(context.detectSource(url), 'YouTube');
    assert.equal(context.cookiesMatchSource(cookies, url), true, 'Platform matching must normalize the DNS trailing dot in the same way as the server.');
  },
};
(async () => {
  const selected = process.argv[2];
  if (selected) { assert(cases[selected], `Unknown case: ${selected}`); await cases[selected](); }
  else for (const run of Object.values(cases)) await run();
  console.log(`App asynchronous regression ${selected || 'suite'} passed.`);
})().catch(error => { console.error(error); process.exitCode = 1; });
