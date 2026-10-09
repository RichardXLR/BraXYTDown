'use strict';
// Exercise the complete account entry point through its public controls and
// the SDK listener. The SDK fixture deliberately reports a null session before
// signOut resolves, matching the event order that previously interrupted logout.
// No credentials, remote requests, or production accounts are used here.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const source = fs.readFileSync(path.resolve(__dirname, '../public/account.js'), 'utf8');

function deferred() {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}

async function until(condition, message) {
  const deadline = Date.now() + 1500;
  while (!condition()) {
    if (Date.now() >= deadline) throw new Error(message);
    await new Promise(resolve => setTimeout(resolve, 1));
  }
}

function browser({ holdStudio = null, failStudio = null, holdWrites = false, failUserButton = false,
  savedCache = new Map(), initialIdentity = 'user_local_A' } = {}) {
  const events = [], scripts = [], reloads = [], fetches = [], saved = savedCache;
  const heldScripts = new Map(), sdkListeners = [], writes = [];
  let signOutCalls = 0, signOutCompleted = false, pendingSignOut = null;
  const elements = new Map();
  function element(id) {
    if (elements.has(id)) return elements.get(id);
    const listeners = new Map();
    const result = {
      id, hidden: false, inert: false, disabled: false, dataset: {}, style: {},
      setAttribute(name, value) { this[name] = value; },
      removeAttribute(name) {
        if (name.startsWith('data-')) delete this.dataset[name.slice(5)];
        else delete this[name];
      },
      addEventListener(name, listener) {
        if (!listeners.has(name)) listeners.set(name, []);
        listeners.get(name).push(listener);
      },
      remove() {}, focus() {},
      async click() {
        await Promise.all((listeners.get('click') || []).map(listener => listener({ currentTarget: this })));
      },
    };
    elements.set(id, result);
    return result;
  }
  const body = element('body');
  body.dataset.account = 'loading';
  const document = {
    body, hidden: false, documentElement: { style: {} },
    getElementById: element,
    querySelector: () => null,
    addEventListener() {},
    createElement: () => element(`script-${scripts.length}`),
    head: {
      append(script) {
        scripts.push(script.src);
        if (script.src === holdStudio) heldScripts.set(script.src, script);
        else if (script.src === failStudio && scripts.filter(url => url === script.src).length === 1) queueMicrotask(() => script.onerror());
        else queueMicrotask(() => script.onload());
      },
    },
  };
  const clerk = {
    user: { id: initialIdentity },
    session: { getToken: async () => 'local-fixture-token' },
    load: async () => {},
    addListener(listener) { sdkListeners.push(listener); return () => {}; },
    mountUserButton() { if (failUserButton) throw new Error('Fixture user control could not mount.'); },
    mountSignIn() {}, mountSignUp() {},
    unmountSignIn() {}, unmountSignUp() {},
    signOut(options) {
      assert.equal(options.redirectUrl, '/');
      signOutCalls += 1;
      pendingSignOut = deferred();
      // SDK listeners run while the redirect/signOut handoff is still pending.
      clerk.user = null; clerk.session = null;
      for (const listener of sdkListeners) listener({ user: null, session: null });
      return pendingSignOut.promise.then(() => { signOutCompleted = true; });
    },
  };
  const location = {
    href: 'https://onda.example/', origin: 'https://onda.example',
    reload() { reloads.push({ interruptedSignOut: signOutCalls > 0 && !signOutCompleted }); },
  };
  const context = {
    console, URL, TextEncoder, Headers, Request, Response, ReadableStream,
    AbortController, DOMException, setTimeout, clearTimeout, queueMicrotask,
    document, location, navigator: { onLine: true }, history: { replaceState() {} },
    window: { Clerk: clerk, __internal_ClerkUICtor: function TestUI() {} },
    localStorage: {
      getItem: key => saved.get(key) ?? null,
      setItem: (key, value) => saved.set(key, value),
      removeItem: key => saved.delete(key),
    },
    CustomEvent: class CustomEvent { constructor(type, options) { this.type = type; this.detail = options.detail; } },
    dispatchEvent: event => events.push(event), addEventListener() {},
    async fetch(url, options = {}) {
      fetches.push({ url, options, identity: clerk.user?.id });
      if (url === '/api/auth/config') {
        return Response.json({ configured: true, publishableKey: 'pk_test_localfixture', frontendApi: 'https://clerk.example' });
      }
      if (url === '/api/account/state') {
        if (options.method === 'PUT' && holdWrites) {
          const pending = deferred();
          writes.push({ update: JSON.parse(options.body), pending });
          return pending.promise;
        }
        assert.equal(options.method || 'GET', 'GET', 'The initial hydration must not rewrite account data.');
        return Response.json({ schema: 1, revision: 2, state: {
          preferences: { accent: 'cyan' }, sound: true, draft: { url: 'https://vimeo.com/12345' }, history: [],
        } });
      }
      if (url === '/api/download') {
        return new Response(new ReadableStream({
          start(controller) {
            options.signal.addEventListener('abort', () => controller.error(new DOMException('Stopped after logout', 'AbortError')), { once: true });
          },
        }), { status: 200 });
      }
      throw new Error(`Unexpected fixture request: ${url}`);
    },
  };
  vm.runInNewContext(source, context, { filename: 'public/account.js' });
  return {
    context, clerk, body, element, events, scripts, reloads, fetches, saved, writes,
    get signOutCalls() { return signOutCalls; },
    finishSignOut() { pendingSignOut.resolve(); },
    emit(identity) {
      clerk.user = identity ? { id: identity } : null;
      clerk.session = identity ? { getToken: async () => 'local-fixture-token' } : null;
      for (const listener of sdkListeners) listener({ user: clerk.user, session: clerk.session });
    },
    releaseScript(url) { const script = heldScripts.get(url); assert(script, `No script held at ${url}`); script.onload(); },
  };
}

async function logoutWhileSdkHandoffIsPending() {
  const page = browser();
  await until(() => page.body.dataset.account === 'signed-in', 'Account did not hydrate.');
  assert.equal(page.context.window.OndaAuth.signedIn, true);
  assert.equal(page.context.window.OndaAccount.storage.getItem('onda.preferences.sound.v1'), '1');
  assert.equal(page.saved.size, 1);

  const download = await page.context.window.OndaAuth.fetch('/api/download');
  const pendingRead = download.body.getReader().read();
  const abortedRead = assert.rejects(pendingRead, { name: 'AbortError' });
  const signal = page.fetches.find(call => call.url === '/api/download').options.signal;
  const cookieWrite = deferred();
  let cookieFlushes = 0;
  page.context.window.OndaSession = { flushCookieSave() { cookieFlushes += 1; return cookieWrite.promise; } };
  const button = page.element('account-sign-out');
  const logout = button.click();
  await until(() => cookieFlushes === 1, 'The cookie save barrier was not reached.');
  assert.equal(button.disabled, true);
  assert.equal(page.signOutCalls, 0, 'Cookie persistence must settle before the SDK session ends.');

  cookieWrite.resolve();
  await until(() => page.signOutCalls === 1, 'SDK signOut was not called.');
  await abortedRead;
  assert.equal(signal.aborted, true, 'Logout must abort a download already streaming.');
  assert.equal(page.body.dataset.account, 'signed-out');
  assert.equal(page.element('account-gate').hidden, false);
  assert.equal(page.element('onda-workspace').hidden, true);
  assert.equal(page.element('onda-workspace').inert, true);
  assert.equal(page.context.window.OndaAccount.ready, false);
  assert.equal(page.context.window.OndaAuth.signedIn, false);
  assert.equal(page.context.window.OndaAccount.storage.getItem('onda.preferences.v2'), null);
  assert.equal(page.saved.size, 0, 'The previous account snapshot must leave the shared document.');
  assert(page.events.some(event => event.type === 'onda:auth' && event.detail.signedIn === false));
  assert.equal(page.reloads.length, 0, 'A null SDK session must not interrupt the still-pending logout redirect.');
  assert.equal(button.disabled, true, 'The handoff remains pending until the SDK finishes.');
  await button.click();
  assert.equal(page.signOutCalls, 1, 'A second click must not start another logout.');
  await assert.rejects(page.context.window.OndaAuth.fetch('/api/account/state'), /sessão terminou/);
  assert.throws(() => page.context.window.OndaAccount.storage.setItem('onda.preferences.sound.v1', '1'), /Entre na sua conta/);

  page.finishSignOut();
  await logout;
  assert.equal(button.disabled, false);
  assert.equal(page.reloads.length, 0);
  const requestsBeforeSwitch = page.fetches.length;
  const scriptsBeforeSwitch = page.scripts.length;
  page.emit('user_local_B');
  assert.equal(page.reloads.length, 1, 'A new account requires a fresh document after studio code ran.');
  assert.equal(page.reloads[0].interruptedSignOut, false);
  assert.equal(page.fetches.length, requestsBeforeSwitch, 'Existing studio closures must not hydrate the new account.');
  assert.equal(page.scripts.length, scriptsBeforeSwitch);
  assert.equal(page.context.window.OndaAccount.ready, false);
}

async function sdkOwnedLogoutAndDirectAccountSwitch() {
  // The Clerk user menu can end a session without invoking Onda's logout button.
  const page = browser();
  await until(() => page.body.dataset.account === 'signed-in', 'Account did not hydrate for SDK logout.');
  page.emit(null);
  assert.equal(page.body.dataset.account, 'signed-out');
  assert.equal(page.reloads.length, 0, 'SDK-owned logout needs the same uninterrupted null transition.');
  page.emit('user_local_B');
  assert.equal(page.reloads.length, 1);
  assert.equal(page.context.window.OndaAccount.ready, false);

  const switched = browser();
  await until(() => switched.body.dataset.account === 'signed-in', 'Account did not hydrate for direct switch.');
  switched.emit('user_local_B');
  assert.equal(switched.reloads.length, 1, 'A direct A-to-B switch must also isolate existing closures.');
  assert.equal(switched.element('onda-workspace').hidden, true);
  assert.equal(switched.context.window.OndaAuth.signedIn, false);
}

async function sdkLogoutWhileCookiesAreStillSaving() {
  const page = browser();
  await until(() => page.body.dataset.account === 'signed-in', 'Account did not hydrate for the save barrier.');
  const cookieWrite = deferred();
  let flushes = 0;
  page.context.window.OndaSession = {
    flushCookieSave() { flushes += 1; return cookieWrite.promise; },
  };
  const button = page.element('account-sign-out');
  const logout = button.click();
  await until(() => flushes === 1, 'Logout did not reach the cookie save barrier.');
  const secondClick = button.click();
  await secondClick;
  assert.equal(flushes, 1, 'An action remains exclusive during its cookie-save barrier.');
  page.emit(null);
  page.emit('user_local_B');
  cookieWrite.resolve();
  await until(() => page.signOutCalls > 0 || button.disabled === false, 'Logout did not leave its old save barrier.');
  assert.equal(page.signOutCalls, 0, 'The old callback cannot sign out an identity which replaced it.');
  await logout;
  assert.equal(page.reloads.length, 1);
  assert.equal(page.context.window.OndaAccount.ready, false);
}

async function identityChangesWhileStudioIsBooting() {
  const page = browser({ holdStudio: '/app.js' });
  await until(() => page.scripts.includes('/app.js'), 'Studio did not reach its held script.');
  page.emit('user_local_B');
  assert.equal(page.reloads.length, 1, 'A partly initialized studio also requires an isolated document.');
  assert.equal(page.context.window.OndaAccount.ready, false);
  page.releaseScript('/app.js');
  await new Promise(resolve => setTimeout(resolve, 10));
  assert.equal(page.body.dataset.account, 'signed-out', 'A stale boot completion cannot reopen the previous account.');
  assert(!page.scripts.includes('/player.js'), 'The cancelled account cannot continue loading studio scripts.');
}

async function nextIdentityAfterInterruptedStudioBoot() {
  // The SDK may finish logout before a script already loading completes. Even
  // after that boot promise settles, closures from the old studio can exist.
  const page = browser({ holdStudio: '/app.js' });
  await until(() => page.scripts.includes('/app.js'), 'Studio did not reach its held script before logout.');
  page.emit(null);
  assert.equal(page.reloads.length, 0, 'A boot-time null session must also allow SDK logout to finish.');
  page.releaseScript('/app.js');
  await new Promise(resolve => setTimeout(resolve, 10));
  const scriptsBeforeLogin = page.scripts.length;
  page.emit('user_local_B');
  assert.equal(page.reloads.length, 1, 'Settled partial boot cannot reuse old closures for a new account.');
  assert.equal(page.scripts.length, scriptsBeforeLogin, 'No studio script may run twice for different accounts in one document.');
  assert.equal(page.context.window.OndaAccount.ready, false);
}

async function retryAfterPartlyLoadedStudioStartsFreshDocument() {
  const page = browser({ failStudio: '/player.js' });
  await until(() => page.body.dataset.account === 'error', 'Failed studio script did not show the retry gate.');
  assert(page.scripts.includes('/app.js'), 'The failure must happen after stateful studio scripts ran.');
  const beforeRetry = [...page.scripts];
  await page.element('account-retry').click();
  assert.equal(page.reloads.length, 1, 'Retry must isolate partially initialized modules in a fresh document.');
  assert.deepEqual(page.scripts, beforeRetry, 'Retry cannot execute the studio modules twice in one document.');
}

async function retryAfterUserControlFailureStartsFreshDocument() {
  const page = browser({ failUserButton: true });
  await until(() => page.body.dataset.account === 'error', 'Failed user control did not show the retry gate.');
  assert(page.scripts.includes('/intro.js'), 'The failure must happen after all studio scripts loaded.');
  const beforeRetry = [...page.scripts];
  await page.element('account-retry').click();
  assert.equal(page.reloads.length, 1, 'A failed final auth control must not leave retry stuck behind an invisible workspace.');
  assert.deepEqual(page.scripts, beforeRetry);
}

async function logoutSavesChangesMadeWhileItsWriteIsPending() {
  const page = browser({ holdWrites: true });
  await until(() => page.body.dataset.account === 'signed-in', 'Account did not hydrate for the pending-write check.');
  const account = page.context.window.OndaAccount;
  account.storage.setItem('onda.preferences.sound.v1', '0');
  const logout = page.element('account-sign-out').click();
  await until(() => page.writes.length === 1, 'Logout did not start its first save.');
  const preferences = JSON.parse(account.storage.getItem('onda.preferences.v2'));
  account.storage.setItem('onda.preferences.v2', JSON.stringify({ ...preferences, theme: 'light' }));
  const first = page.writes[0];
  first.pending.resolve(Response.json({ schema: 1, revision: 3, state: first.update.state }));
  await until(() => page.writes.length > 1 || page.signOutCalls > 0, 'Logout did not finish or save its pending changes.');
  assert.equal(page.signOutCalls, 0, 'Logout must save an edit made during its first PUT before ending the SDK session.');
  assert.equal(page.writes.length, 2);
  const second = page.writes[1];
  assert.equal(second.update.base_revision, 3);
  assert.equal(second.update.state.preferences.theme, 'light');
  second.pending.resolve(Response.json({ schema: 1, revision: 4, state: second.update.state }));
  await until(() => page.signOutCalls === 1, 'Logout did not start after all pending state was saved.');
  page.finishSignOut(); await logout;
}

async function logoutSavesBackgroundChangesWhileCookiesSettle() {
  const page = browser({ holdWrites: true });
  await until(() => page.body.dataset.account === 'signed-in', 'Account did not hydrate for the cookie/save interleaving.');
  const cookieWrite = deferred();
  let flushes = 0;
  page.context.window.OndaSession = {
    flushCookieSave() { flushes += 1; return cookieWrite.promise; },
  };
  const logout = page.element('account-sign-out').click();
  await until(() => flushes === 1, 'Logout did not reach its cookie barrier.');
  assert.equal(page.element('onda-workspace').inert, true, 'New interactions are paused while logout settles saves.');
  // A background completion can still write history while user interactions are paused.
  const history = [{ url: 'https://example.org/own-media.mp4', media_type: 'video', format: 'mp4', quality: 'source',
    title: 'Local fixture media', timestamp: Date.now() }];
  page.context.window.OndaAccount.storage.setItem('onda.audio.history.v1', JSON.stringify(history));
  cookieWrite.resolve();
  await until(() => page.writes.length === 1 || page.signOutCalls > 0, 'Logout did not inspect the background change.');
  assert.equal(page.signOutCalls, 0, 'Changes during the cookie barrier also need a cloud acknowledgement.');
  const write = page.writes[0];
  assert.equal(write.update.state.history[0].url, history[0].url);
  write.pending.resolve(Response.json({ schema: 1, revision: 3, state: write.update.state }));
  await until(() => page.signOutCalls === 1, 'Logout did not continue after saving background history.');
  page.finishSignOut(); await logout;
}

async function failedLogoutSaveRestoresTheExistingWorkspace() {
  const page = browser({ holdWrites: true });
  await until(() => page.body.dataset.account === 'signed-in', 'Account did not hydrate for a failed logout save.');
  page.context.window.OndaAccount.storage.setItem('onda.preferences.sound.v1', '0');
  const button = page.element('account-sign-out');
  const logout = button.click();
  await until(() => page.writes.length === 1, 'Logout did not begin its failing save.');
  assert.equal(page.element('onda-workspace').inert, true);
  page.writes[0].pending.resolve(new Response(null, { status: 500 }));
  await logout;
  assert.equal(page.signOutCalls, 0, 'Failed saving must retain the active identity and local pending data.');
  assert.equal(page.context.window.OndaAccount.ready, true);
  assert.equal(page.context.window.OndaAccount.storage.getItem('onda.preferences.sound.v1'), '0');
  assert.equal(page.element('onda-workspace').inert, false, 'Failed logout must restore interaction.');
  assert.equal(button.disabled, false);
  page.emit(null); // Clear the fixture retry timer without making remote requests.
}

async function verifyPendingHistoryRecoversOnlyForItsOwner(saved, expectedURL) {
  const other = browser({ savedCache: saved, initialIdentity: 'user_local_B' });
  await until(() => other.body.dataset.account === 'signed-in', 'Other account did not hydrate.');
  assert.deepEqual(JSON.parse(other.context.window.OndaAccount.storage.getItem('onda.audio.history.v1')), [],
    'A new identity cannot inherit the previous account pending history.');
  other.emit(null);
  const owner = browser({ savedCache: saved, holdWrites: true });
  await until(() => owner.body.dataset.account === 'signed-in', 'Returning owner did not hydrate.');
  const history = JSON.parse(owner.context.window.OndaAccount.storage.getItem('onda.audio.history.v1'));
  assert.equal(history[0]?.url, expectedURL, 'The returning owner must recover the accepted but unacknowledged history.');
  const save = owner.context.window.OndaAccount.flush();
  await until(() => owner.writes.length === 1, 'Recovered local changes did not reach cloud synchronization.');
  const write = owner.writes[0];
  assert.equal(write.update.base_revision, 2);
  assert.equal(write.update.state.history[0].url, expectedURL);
  write.pending.resolve(Response.json({ schema: 1, revision: 3, state: write.update.state }));
  assert.equal(await save, true);
  owner.emit(null);
  assert.equal(saved.size, 0, 'After the cloud acknowledgement a normal logout removes the local snapshot.');
}

async function logoutPreservesLateBackgroundChangeDuringSdkHandoff() {
  const saved = new Map();
  const page = browser({ savedCache: saved, holdWrites: true });
  await until(() => page.body.dataset.account === 'signed-in', 'Account did not hydrate for the SDK handoff.');
  const sdkHandoff = deferred();
  const actualSignOut = page.clerk.signOut.bind(page.clerk);
  let handoffStarted = false;
  page.clerk.signOut = options => {
    handoffStarted = true;
    return sdkHandoff.promise.then(() => actualSignOut(options));
  };
  const logout = page.element('account-sign-out').click();
  await until(() => handoffStarted, 'The asynchronous SDK handoff did not begin.');
  const url = 'https://example.org/completed-during-logout.mp4';
  page.context.window.OndaAccount.storage.setItem('onda.audio.history.v1', JSON.stringify([
    { url, media_type: 'video', format: 'mp4', quality: 'source', title: 'Completed during SDK handoff', timestamp: Date.now() },
  ]));
  sdkHandoff.resolve();
  await until(() => page.signOutCalls === 1, 'The SDK did not end the session.');
  page.finishSignOut(); await logout;
  assert.equal(page.context.window.OndaAccount.ready, false);
  assert.equal(page.context.window.OndaAccount.storage.getItem('onda.audio.history.v1'), null, 'The signed-out document still clears all account state from memory.');
  assert.equal(page.writes.length, 0, 'This change arrived after the final cloud flush and cannot use an ended SDK session.');
  assert(saved.has('onda.account.state.v1:user_local_A'), 'The unacknowledged account snapshot must survive the SDK reset.');
  await verifyPendingHistoryRecoversOnlyForItsOwner(saved, url);
}

async function sdkOwnedLogoutPreservesUnacknowledgedOwnerState() {
  const saved = new Map();
  const page = browser({ savedCache: saved });
  await until(() => page.body.dataset.account === 'signed-in', 'Account did not hydrate for external SDK logout.');
  const url = 'https://example.org/completed-before-sdk-logout.mp4';
  page.context.window.OndaAccount.storage.setItem('onda.audio.history.v1', JSON.stringify([
    { url, media_type: 'video', format: 'mp4', quality: 'source', title: 'Completed before external SDK logout', timestamp: Date.now() },
  ]));
  page.emit(null);
  assert.equal(page.context.window.OndaAccount.ready, false);
  assert(saved.has('onda.account.state.v1:user_local_A'), 'SDK-owned logout must also retain unacknowledged edits for their owner.');
  await verifyPendingHistoryRecoversOnlyForItsOwner(saved, url);
}

const watchdog = setTimeout(() => {
  console.error('Account lifecycle timed out.');
  process.exitCode = 1;
}, 5000);
(async () => {
  await logoutWhileSdkHandoffIsPending();
  await sdkOwnedLogoutAndDirectAccountSwitch();
  await sdkLogoutWhileCookiesAreStillSaving();
  await identityChangesWhileStudioIsBooting();
  await nextIdentityAfterInterruptedStudioBoot();
  await retryAfterPartlyLoadedStudioStartsFreshDocument();
  await retryAfterUserControlFailureStartsFreshDocument();
  await logoutSavesChangesMadeWhileItsWriteIsPending();
  await logoutSavesBackgroundChangesWhileCookiesSettle();
  await failedLogoutSaveRestoresTheExistingWorkspace();
  await logoutPreservesLateBackgroundChangeDuringSdkHandoff();
  await sdkOwnedLogoutPreservesUnacknowledgedOwnerState();
  console.log('Account lifecycle: pending SDK logout, cookie save barrier, stream cancellation and account isolation passed.');
})().catch(error => { console.error(error); process.exitCode = 1; })
  .finally(() => clearTimeout(watchdog));
