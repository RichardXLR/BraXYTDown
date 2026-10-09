'use strict';

// The account is opened before the studio scripts run. The server verifies
// every session and chooses the account identifier; it never accepts one here.
(() => {
  const gate = document.getElementById('account-gate');
  const workspace = document.getElementById('onda-workspace');
  const loading = document.getElementById('account-loading');
  const loadingText = document.getElementById('account-loading-text');
  const authForm = document.getElementById('account-auth-form');
  const authTabs = document.getElementById('account-auth-tabs');
  const errorPanel = document.getElementById('account-error');
  const errorMessage = document.getElementById('account-error-message');
  const keys = Object.freeze({
    'onda.preferences.v2': 'preferences',
    'onda.preferences.sound.v1': 'sound',
    'onda.intro.seen.v2': 'intro_seen',
    'onda.media.draft.v2': 'draft',
    'onda.audio.history.v1': 'history',
  });
  const choices = Object.freeze({
    accent: ['blue', 'cyan', 'violet'], theme: ['dark', 'light', 'system'], density: ['comfortable', 'compact'],
    media_type: ['audio', 'video'], format: ['mp3', 'm4a', 'wav', 'flac', 'ogg', 'opus', 'aac', 'aiff'],
    quality: ['128', '192', '256', '320'], video_format: ['mp4', 'webm', 'mkv', 'mov'],
    video_resolution: ['source', '2160', '1440', '1080', '720', '480', '360'],
  });
  const preferencesDefault = Object.freeze({ accent: 'blue', theme: 'dark', density: 'comfortable', motion: true, intro: true });
  const draftDefault = Object.freeze({
    media_type: 'audio', format: 'mp3', quality: '320', video_format: 'mp4', video_resolution: 'source',
    url: '', trim_start: '', trim_end: '', 'strip-metadata': true, 'normalize-audio': false, 'mute-video': false,
  });
  const paths = new Set(['sound', 'intro_seen', 'history',
    ...Object.keys(preferencesDefault).map((key) => `preferences.${key}`),
    ...Object.keys(draftDefault).map((key) => `draft.${key}`)]);
  const dirty = new Map();
  const requests = new Set();
  const studioScripts = ['/ui.js', '/session-store.js', '/app.js', '/player.js', '/intro.js'];
  let state = defaults();
  let revision = 0;
  let userId = null;
  let accountReady = false;
  let appLoaded = false;
  let studioStarted = false;
  let appBooting = false;
  let clerkLoaded = false;
  let clerkLoading = null;
  let clerkUnsubscribe = null;
  let authGeneration = 0;
  let changeSequence = 0;
  let historyReplacement = null;
  let saveTimer = null;
  let maximumWaitTimer = null;
  let retryTimer = null;
  let inFlight = null;
  let failureCount = 0;
  let lastRemoteRead = 0;
  let syncState = 'loading';
  let authMode = new URL(location.href).searchParams.get('account') === 'sign-up' ? 'sign-up' : 'sign-in';
  let mountedAuthMode = null;
  let signingOut = false;

  function copy(value) { return JSON.parse(JSON.stringify(value)); }
  function defaults() { return { preferences: { ...preferencesDefault }, sound: false, intro_seen: false, draft: { ...draftDefault }, history: [] }; }
  function plain(value) { return value !== null && typeof value === 'object' && !Array.isArray(value); }
  function validLink(value, empty = false) {
    if (empty && value === '') return true;
    if (typeof value !== 'string' || value.length > 4096) return false;
    try { const url = new URL(value); return ['https:', 'http:'].includes(url.protocol) && !url.username && !url.password; } catch { return false; }
  }
  function normalizedState(value) {
    if (!plain(value)) throw new Error('Os dados da conta não estão disponíveis agora.');
    const result = defaults();
    if (plain(value.preferences)) {
      for (const name of ['accent', 'theme', 'density']) if (choices[name].includes(value.preferences[name])) result.preferences[name] = value.preferences[name];
      for (const name of ['motion', 'intro']) if (typeof value.preferences[name] === 'boolean') result.preferences[name] = value.preferences[name];
    }
    for (const name of ['sound', 'intro_seen']) if (typeof value[name] === 'boolean') result[name] = value[name];
    if (plain(value.draft)) {
      for (const name of ['media_type', 'format', 'quality', 'video_format', 'video_resolution']) {
        if (choices[name].includes(value.draft[name])) result.draft[name] = value.draft[name];
      }
      if (validLink(value.draft.url, true)) result.draft.url = value.draft.url;
      for (const name of ['trim_start', 'trim_end']) if (typeof value.draft[name] === 'string' && value.draft[name].length <= 32) result.draft[name] = value.draft[name];
      for (const name of ['strip-metadata', 'normalize-audio', 'mute-video']) if (typeof value.draft[name] === 'boolean') result.draft[name] = value.draft[name];
    }
    if (Array.isArray(value.history)) for (const record of value.history.slice(0, 5)) {
      if (!plain(record) || !validLink(record.url) || typeof record.timestamp !== 'number' || !Number.isFinite(record.timestamp) || record.timestamp < 0) continue;
      const type = record.media_type === 'video' ? 'video' : 'audio';
      if (!(type === 'video' ? choices.video_format : choices.format).includes(record.format)) continue;
      if (![...choices.quality, 'source'].includes(String(record.quality))) continue;
      const entry = {
        url: record.url, media_type: type, format: record.format, quality: type === 'video' ? 'source' : String(record.quality),
        title: typeof record.title === 'string' ? record.title.slice(0, 400) : '', timestamp: record.timestamp,
      };
      if (plain(record.options)) {
        const source = record.options;
        const time = (v) => typeof v === 'number' && Number.isFinite(v) && v >= 0 && v <= Number.MAX_SAFE_INTEGER ? v : null;
        const start = time(source.trim_start);
        const end = time(source.trim_end);
        const rangeValid = end === null || end > (start ?? 0);
        const mute = type === 'video' && source.mute === true;
        entry.options = { video_resolution: choices.video_resolution.includes(source.video_resolution) ? source.video_resolution : 'source',
          trim_start: rangeValid ? start : null, trim_end: rangeValid ? end : null,
          strip_metadata: source.strip_metadata !== false, normalize_audio: source.normalize_audio === true && !mute, mute };
      }
      result.history.push(entry);
    }
    return result;
  }
  function validatedSnapshot(payload) {
    if (!plain(payload) || payload.schema !== 1 || !Number.isSafeInteger(payload.revision) || payload.revision < 0) throw new Error('Não foi possível verificar os dados da conta.');
    return { revision: payload.revision, state: normalizedState(payload.state) };
  }
  function cacheKey() { return userId ? `onda.account.state.v1:${userId}` : null; }
  function readCache() {
    try {
      const raw = localStorage.getItem(cacheKey());
      if (!raw || new TextEncoder().encode(raw).length > 40000) return null;
      const value = JSON.parse(raw);
      const snapshot = validatedSnapshot(value);
      const pending = Array.isArray(value.pending) ? value.pending.filter((path) => paths.has(path)) : [];
      return { ...snapshot, pending, history_reset: value.history_reset === true && pending.includes('history') };
    } catch { return null; }
  }
  function saveCache() {
    if (!userId || !accountReady) return;
    try { localStorage.setItem(cacheKey(), JSON.stringify({ schema: 1, revision, state, pending: [...dirty.keys()], history_reset: historyReplacement !== null && dirty.has('history') })); } catch { /* Cloud synchronization still works when device storage is unavailable. */ }
  }
  function getPath(source, path) { return path.split('.').reduce((value, name) => value[name], source); }
  function setPath(target, path, value) {
    const parts = path.split('.');
    if (parts.length === 1) target[parts[0]] = copy(value);
    else target[parts[0]][parts[1]] = copy(value);
  }
  function mergeHistory(remote, local) {
    // A blank local history is an explicit removal, not an empty contribution.
    if (local.length === 0) return [];
    const seen = new Set();
    const entries = [...local, ...remote].sort((a, b) => b.timestamp - a.timestamp);
    return entries.filter((entry) => {
      const options = entry.options || {};
      const identity = JSON.stringify([entry.url, entry.media_type, entry.format, entry.quality,
        options.video_resolution ?? 'source', options.trim_start ?? null, options.trim_end ?? null,
        options.strip_metadata !== false, options.normalize_audio === true, options.mute === true]);
      if (seen.has(identity)) return false;
      seen.add(identity);
      return true;
    }).slice(0, 5).map(copy);
  }
  function mergePending(remote, local) {
    const merged = copy(remote);
    for (const path of dirty.keys()) {
      const value = path === 'history' && historyReplacement === null ? mergeHistory(remote.history, local.history) : getPath(local, path);
      setPath(merged, path, value);
    }
    return merged;
  }
  function announceState() {
    dispatchEvent(new CustomEvent('onda:account-state', { detail: { state: copy(state), pending: dirty.size > 0 } }));
  }
  function status(kind) {
    syncState = kind;
    const messages = {
      loading: 'Sincronizando', saved: 'Salvo na conta', pending: 'Alterações pendentes', saving: 'Salvando na conta',
      offline: 'Aguardando conexão', error: 'Sincronização pendente',
    };
    const message = messages[kind] || messages.error;
    const button = document.getElementById('account-sync');
    if (button) {
      button.dataset.state = kind;
      button.setAttribute('aria-label', `Dados da conta: ${message.toLowerCase()}. Clique para sincronizar.`);
      button.title = kind === 'error' || kind === 'offline' ? 'As alterações continuam neste dispositivo. Clique para tentar salvar na conta.' : 'Preferências, histórico e ajustes acompanham sua conta.';
    }
    const label = document.getElementById('account-sync-label');
    if (label) label.textContent = message;
    const announcement = document.getElementById('account-sync-announcement');
    if (announcement) announcement.textContent = message;
    dispatchEvent(new CustomEvent('onda:account-sync', { detail: { status: kind, message } }));
  }
  function cancelTimers() {
    clearTimeout(saveTimer); clearTimeout(maximumWaitTimer); clearTimeout(retryTimer);
    saveTimer = maximumWaitTimer = retryTimer = null;
  }
  function queueSave() {
    saveCache();
    status(navigator.onLine === false ? 'offline' : 'pending');
    clearTimeout(saveTimer);
    saveTimer = setTimeout(() => flush(), 2000);
    if (!maximumWaitTimer) maximumWaitTimer = setTimeout(() => flush(), 15000);
  }
  function changeSection(name, value) {
    if (!accountReady || !userId) throw new Error('Entre na sua conta para salvar suas escolhas.');
    const candidate = normalizedState({ ...state, [name]: value });
    if (new TextEncoder().encode(JSON.stringify(candidate)).length > 32768) throw new Error('Os dados ultrapassaram o espaço disponível para a conta.');
    const changed = name === 'preferences' || name === 'draft' ? Object.keys(candidate[name]).map((key) => `${name}.${key}`) : [name];
    let hasChanges = false;
    for (const path of changed) {
      if (JSON.stringify(getPath(state, path)) === JSON.stringify(getPath(candidate, path))) continue;
      dirty.set(path, ++changeSequence);
      if (path === 'history' && candidate.history.length === 0) historyReplacement = changeSequence;
      hasChanges = true;
    }
    if (!hasChanges) return;
    state = candidate;
    queueSave();
  }
  const storage = Object.freeze({
    getItem(key) {
      if (!accountReady || !(key in keys)) return null;
      const name = keys[key];
      if (name === 'sound') return state.sound ? '1' : '0';
      if (name === 'intro_seen') return state.intro_seen ? '1' : null;
      return JSON.stringify(state[name]);
    },
    setItem(key, value) {
      if (!(key in keys)) throw new Error('Esta informação não faz parte dos dados da conta.');
      const name = keys[key];
      changeSection(name, name === 'sound' || name === 'intro_seen' ? value === '1' : JSON.parse(String(value)));
    },
    removeItem(key) {
      if (!(key in keys)) return;
      changeSection(keys[key], defaults()[keys[key]]);
    },
  });

  function signedIn() { return Boolean(clerkLoaded && window.Clerk?.session && window.Clerk?.user?.id === userId); }
  function abortRequests() {
    for (const controller of requests) controller.abort();
    requests.clear();
  }
  async function authenticatedFetch(resource, options = {}) {
    const destination = new URL(resource instanceof Request ? resource.url : resource, location.origin);
    if (destination.origin !== location.origin || !destination.pathname.startsWith('/api/')) throw new Error('A sessão só pode ser usada no serviço Onda.');
    if (!signedIn()) throw new Error('Sua sessão terminou. Entre novamente para continuar.');
    const generation = authGeneration;
    const token = await window.Clerk.session.getToken();
    if (!token || generation !== authGeneration || !signedIn()) throw new Error('Sua sessão terminou. Entre novamente para continuar.');
    const controller = new AbortController();
    requests.add(controller);
    const originalSignal = options.signal || (resource instanceof Request ? resource.signal : null);
    const abort = () => controller.abort(originalSignal?.reason);
    if (originalSignal?.aborted) abort(); else originalSignal?.addEventListener('abort', abort, { once: true });
    const finish = () => { requests.delete(controller); originalSignal?.removeEventListener('abort', abort); };
    const headers = new Headers(resource instanceof Request ? resource.headers : undefined);
    new Headers(options.headers).forEach((value, key) => headers.set(key, value));
    headers.set('Authorization', `Bearer ${token}`);
    try {
      const response = await fetch(resource, { ...options, headers, credentials: 'same-origin', signal: controller.signal });
      if (generation !== authGeneration || !signedIn()) { controller.abort(); throw new Error('Sua sessão terminou.'); }
      // Keep logout cancellation active while a converted file is streaming.
      if (!response.body) { finish(); return response; }
      const reader = response.body.getReader();
      return new Response(new ReadableStream({
        async pull(stream) {
          try {
            const chunk = await reader.read();
            if (chunk.done) { finish(); stream.close(); } else stream.enqueue(chunk.value);
          } catch (error) { finish(); stream.error(error); }
        },
        async cancel(reason) {
          // Cancel the reader before aborting its fetch. Aborting first errors
          // the body and makes deliberate cancellation (e.g. a 409 retry) reject.
          try { await reader.cancel(reason); }
          finally { controller.abort(reason); finish(); }
        },
      }), { status: response.status, statusText: response.statusText, headers: response.headers });
    } catch (error) { finish(); throw error; }
  }
  async function readRemote() {
    const response = await authenticatedFetch('/api/account/state', { cache: 'no-store' });
    if (!response.ok) {
      await response.body?.cancel();
      throw new Error(response.status === 401 ? 'Sua sessão não pôde ser validada. Entre novamente.' : 'Não foi possível sincronizar sua conta agora. Tente novamente em instantes.');
    }
    const result = validatedSnapshot(await response.json());
    lastRemoteRead = Date.now();
    return result;
  }
  async function flush() {
    if (inFlight) return inFlight;
    if (!accountReady || !signedIn() || dirty.size === 0) return dirty.size === 0;
    clearTimeout(saveTimer); clearTimeout(maximumWaitTimer); clearTimeout(retryTimer);
    saveTimer = maximumWaitTimer = retryTimer = null;
    const generation = authGeneration;
    inFlight = (async () => {
      status('saving');
      try {
        for (let attempt = 0; attempt < 3; attempt += 1) {
          const sentState = copy(state);
          const sentChanges = new Map(dirty);
          const response = await authenticatedFetch('/api/account/state', { method: 'PUT', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ schema: 1, base_revision: revision, state: sentState }), keepalive: true });
          if (generation !== authGeneration) return false;
          if (response.status === 409) {
            await response.body?.cancel();
            const remote = await readRemote();
            if (generation !== authGeneration) return false;
            revision = remote.revision;
            state = mergePending(remote.state, state);
            saveCache(); announceState();
            continue;
          }
          if (!response.ok) { await response.body?.cancel(); throw new Error('A sincronização ficou pendente.'); }
          const saved = validatedSnapshot(await response.json());
          if (generation !== authGeneration) return false;
          // An older in-flight write cannot acknowledge a more recent clear.
          if (historyReplacement !== null && sentChanges.get('history') >= historyReplacement) historyReplacement = null;
          for (const [path, version] of sentChanges) if (dirty.get(path) === version) dirty.delete(path);
          revision = saved.revision;
          state = mergePending(saved.state, state);
          failureCount = 0;
          saveCache(); announceState(); status(dirty.size ? 'pending' : 'saved');
          return true;
        }
        throw new Error('Outra sessão atualizou a conta. Vamos tentar novamente.');
      } catch {
        if (generation !== authGeneration) return false;
        saveCache();
        status(navigator.onLine === false ? 'offline' : 'error');
        failureCount += 1;
        retryTimer = setTimeout(() => flush(), Math.min(30000, 2000 * 2 ** Math.min(failureCount, 4)));
        return false;
      } finally {
        inFlight = null;
        if (generation === authGeneration && dirty.size && !retryTimer) saveTimer = setTimeout(() => flush(), 2000);
      }
    })();
    return inFlight;
  }
  async function refreshAccount() {
    if (!accountReady || !signedIn() || inFlight) return;
    if (dirty.size) { await flush(); return; }
    const generation = authGeneration;
    try {
      const remote = await readRemote();
      if (generation !== authGeneration) return;
      revision = remote.revision;
      state = mergePending(remote.state, state);
      saveCache(); announceState();
      if (dirty.size) queueSave(); else status('saved');
    } catch { if (generation === authGeneration) status(navigator.onLine === false ? 'offline' : 'error'); }
  }
  function showLoading(message) {
    loadingText.textContent = message;
    loading.hidden = false; errorPanel.hidden = true; authTabs.hidden = true; authForm.hidden = true;
    gate.hidden = false; workspace.hidden = true; workspace.inert = true;
  }
  function showError(message) {
    loading.hidden = true; errorPanel.hidden = false; authTabs.hidden = true; authForm.hidden = true;
    errorMessage.textContent = message;
    gate.hidden = false; workspace.hidden = true; workspace.inert = true;
    document.body.dataset.account = 'error';
  }
  const appearance = {
    options: { socialButtonsVariant: 'blockButton' },
    variables: { colorPrimary: '#7cbcff', colorBackground: '#0c1c32', colorText: '#f2f7ff', colorTextSecondary: '#b3c7dd', colorInputBackground: '#081426', colorInputText: '#f2f7ff', colorNeutral: '#c9def2', colorDanger: '#ffb4ae', borderRadius: '1rem', fontFamily: 'Manrope, system-ui, sans-serif' },
    elements: { rootBox: 'onda-clerk-root', cardBox: 'onda-clerk-card-box', card: 'onda-clerk-card', userButtonPopoverCard: 'onda-clerk-popover', userButtonAvatarBox: 'onda-account-avatar', formFieldInput: 'onda-clerk-input', formButtonPrimary: 'onda-clerk-primary', socialButtonsIconButton: 'onda-clerk-social', socialButtonsBlockButton: 'onda-clerk-social', socialButtonsProviderIcon: 'onda-clerk-provider-icon' },
  };
  function unmountAuth() {
    if (!clerkLoaded) return;
    if (mountedAuthMode === 'sign-in') window.Clerk.unmountSignIn(authForm);
    if (mountedAuthMode === 'sign-up') window.Clerk.unmountSignUp(authForm);
    mountedAuthMode = null;
  }
  function mountAuth(mode = authMode, { resetRoute = false } = {}) {
    if (!clerkLoaded) return;
    if (resetRoute) {
      const address = new URL(location.href);
      address.hash = '';
      address.searchParams.set('account', mode);
      history.replaceState(null, '', address.pathname + address.search);
    }
    unmountAuth(); authMode = mode;
    document.body.dataset.account = 'signed-out';
    loading.hidden = true; errorPanel.hidden = true; authTabs.hidden = false; authForm.hidden = false;
    gate.hidden = false; workspace.hidden = true; workspace.inert = true;
    for (const kind of ['sign-in', 'sign-up']) document.getElementById(`account-${kind}`).setAttribute('aria-selected', String(mode === kind));
    const destination = new URL(location.href); destination.searchParams.delete('account'); destination.hash = '';
    const options = { appearance, routing: 'hash', forceRedirectUrl: destination.href };
    if (mode === 'sign-up') window.Clerk.mountSignUp(authForm, { ...options, signInUrl: '/?account=sign-in' });
    else window.Clerk.mountSignIn(authForm, { ...options, signUpUrl: '/?account=sign-up' });
    mountedAuthMode = mode;
  }
  function loadScript(url, attributes = {}) {
    return new Promise((resolve, reject) => {
      const script = document.createElement('script');
      script.src = url; script.async = false;
      for (const [name, value] of Object.entries(attributes)) script.setAttribute(name, value);
      const timeout = setTimeout(() => { script.remove(); reject(new Error('A conexão com o acesso seguro demorou demais. Tente novamente.')); }, 30000);
      script.onload = () => { clearTimeout(timeout); resolve(); };
      script.onerror = () => { clearTimeout(timeout); script.remove(); reject(new Error('Não foi possível carregar o acesso seguro. Verifique sua conexão e tente novamente.')); };
      document.head.append(script);
    });
  }
  function resetAccount() {
    cancelTimers(); abortRequests(); accountReady = false;
    try { if (cacheKey()) localStorage.removeItem(cacheKey()); } catch { /* Active state is still cleared from memory. */ }
    state = defaults(); revision = 0; dirty.clear(); historyReplacement = null; inFlight = null;
    dispatchEvent(new CustomEvent('onda:auth', { detail: { signedIn: false } }));
    workspace.hidden = true; workspace.inert = true; gate.hidden = false;
    document.body.removeAttribute('data-theme'); document.body.removeAttribute('data-accent');
    document.documentElement.style.colorScheme = 'dark';
    document.body.dataset.account = 'signed-out';
    userId = null;
  }
  async function openAccount(identity) {
    if (appBooting || !identity || identity !== window.Clerk?.user?.id || !window.Clerk?.session) return;
    appBooting = true;
    const generation = ++authGeneration;
    userId = identity;
    showLoading('Retomando suas escolhas…');
    unmountAuth();
    const cache = readCache();
    state = cache?.state || defaults(); revision = cache?.revision || 0; dirty.clear();
    for (const path of cache?.pending || []) dirty.set(path, ++changeSequence);
    historyReplacement = cache?.history_reset ? dirty.get('history') : null;
    try {
      try {
        const remote = await readRemote();
        if (generation !== authGeneration) return;
        state = mergePending(remote.state, state); revision = remote.revision;
        status(dirty.size ? 'pending' : 'saved');
      } catch (error) {
        if (!cache) throw error;
        status(navigator.onLine === false ? 'offline' : 'error');
      }
      if (generation !== authGeneration || !signedIn()) return;
      accountReady = true;
      saveCache();
      // A returning account is hydrated before any preferences, draft, sound,
      // history or opening-animation code can read or write account state.
      studioStarted = true;
      for (const script of studioScripts) {
        await loadScript(script);
        if (generation !== authGeneration || !signedIn()) return;
      }
      appLoaded = true;
      const userButton = document.getElementById('account-user-button');
      window.Clerk.mountUserButton(userButton, { appearance, afterSignOutUrl: '/', userProfileMode: 'modal' });
      workspace.hidden = false; workspace.inert = false; gate.hidden = true;
      document.body.dataset.account = 'signed-in';
      dispatchEvent(new CustomEvent('onda:auth', { detail: { signedIn: true } }));
      if (dirty.size) queueSave();
      const heading = document.getElementById('hero-title');
      if (heading && !document.querySelector('.intro-dialog[open]')) { heading.tabIndex = -1; heading.focus({ preventScroll: true }); }
    } catch (error) {
      if (generation === authGeneration) showError(error.message || 'Não foi possível abrir seu estúdio agora.');
    } finally { appBooting = false; }
  }
  function observeSession({ user, session }) {
    const identity = user && session ? user.id : null;
    if (identity === userId) return;
    if (userId || studioStarted || appLoaded || appBooting) {
      authGeneration += 1;
      resetAccount();
      // Clerk publishes the signed-out state before its signOut promise and
      // redirect finish. Reloading here interrupts that work and can reopen
      // the previous session. A fresh document is required only when another
      // signed-in account would receive the existing studio closures.
      if (identity && (studioStarted || appLoaded || appBooting)) { location.reload(); return; }
    }
    if (identity) void openAccount(identity);
    else mountAuth();
  }
  async function start() {
    if (clerkLoaded) {
      if (window.Clerk?.user && window.Clerk?.session) {
        if (appLoaded) { await refreshAccount(); return; }
        await openAccount(window.Clerk.user.id);
      } else mountAuth();
      return;
    }
    if (clerkLoading) return clerkLoading;
    showLoading('Preparando seu acesso seguro…');
    clerkLoading = (async () => {
      try {
        const response = await fetch('/api/auth/config', { cache: 'no-store', credentials: 'same-origin' });
        if (!response.ok) throw new Error('Não foi possível carregar o acesso seguro. Tente novamente.');
        const config = await response.json();
        if (!config.configured || typeof config.publishableKey !== 'string' || typeof config.frontendApi !== 'string') throw new Error('O acesso seguro está sendo configurado. Tente novamente em instantes.');
        if (!/^pk_(test|live)_[A-Za-z0-9+/=_-]+$/.test(config.publishableKey)) throw new Error('O acesso seguro precisa de uma revisão de configuração.');
        const host = new URL(config.frontendApi.includes('://') ? config.frontendApi : `https://${config.frontendApi}`);
        if (host.protocol !== 'https:' || host.username || host.password || host.port || (host.pathname !== '/' && host.pathname !== '') || host.search || host.hash) throw new Error('O endereço do acesso seguro é inválido.');
        await loadScript(`${host.origin}/npm/@clerk/ui@1/dist/ui.browser.js`, { crossorigin: 'anonymous' });
        await loadScript(`${host.origin}/npm/@clerk/clerk-js@6/dist/clerk.browser.js`, { crossorigin: 'anonymous', 'data-clerk-publishable-key': config.publishableKey });
        if (!window.Clerk || !window.__internal_ClerkUICtor) throw new Error('O acesso seguro não foi carregado. Tente novamente.');
        await window.Clerk.load({ ui: { ClerkUI: window.__internal_ClerkUICtor }, appearance, localization: window.OndaClerkLocalization });
        clerkLoaded = true;
        clerkUnsubscribe = window.Clerk.addListener(observeSession);
        if (window.Clerk.user && window.Clerk.session) await openAccount(window.Clerk.user.id);
        else mountAuth();
      } catch (error) { showError(error.message || 'Não foi possível iniciar o acesso seguro.'); }
      finally { clerkLoading = null; }
    })();
    return clerkLoading;
  }
  window.OndaAuth = Object.freeze({ fetch: authenticatedFetch, get signedIn() { return signedIn() && accountReady; } });
  window.OndaAccount = Object.freeze({ storage, flush, refresh: refreshAccount, get ready() { return accountReady; }, get syncStatus() { return syncState; } });

  document.getElementById('account-retry').addEventListener('click', () => start());
  document.getElementById('account-sign-in').addEventListener('click', () => mountAuth('sign-in', { resetRoute: true }));
  document.getElementById('account-sign-up').addEventListener('click', () => mountAuth('sign-up', { resetRoute: true }));
  document.getElementById('account-sync').addEventListener('click', () => dirty.size ? flush() : refreshAccount());
  document.getElementById('account-sign-out').addEventListener('click', async (event) => {
    if (!signedIn() || signingOut) return;
    const button = event.currentTarget; button.disabled = true;
    try {
      if (dirty.size && !await flush()) { status(navigator.onLine === false ? 'offline' : 'error'); return; }
      await window.OndaSession?.flushCookieSave?.();
      signingOut = true;
      await window.Clerk.signOut({ redirectUrl: '/' });
    } catch { status('error'); }
    finally { signingOut = false; button.disabled = false; }
  });
  document.addEventListener('visibilitychange', () => {
    if (document.hidden) { if (dirty.size) void flush(); }
    else if (Date.now() - lastRemoteRead > 20000) void refreshAccount();
  });
  addEventListener('online', () => dirty.size ? void flush() : void refreshAccount());
  addEventListener('offline', () => { if (accountReady) status('offline'); });
  addEventListener('pagehide', () => { saveCache(); if (dirty.size) void flush(); });
  addEventListener('pageshow', (event) => {
    if (!event.persisted) return;
    if (!signedIn()) { authGeneration += 1; resetAccount(); }
    // pagehide cancels transfers and locks the decrypted source session.
    // Restore from the current account in a fresh document after bfcache.
    location.reload();
  });
  addEventListener('storage', (event) => {
    if (!accountReady || !userId || event.key !== cacheKey() || !event.newValue) return;
    const other = readCache();
    if (!other || other.revision < revision) return;
    const local = state;
    revision = other.revision;
    state = mergePending(other.state, local);
    if (other.history_reset && historyReplacement === null) {
      state.history = copy(other.state.history);
      historyReplacement = ++changeSequence;
      dirty.set('history', historyReplacement);
    }
    for (const path of other.pending) if (!dirty.has(path)) dirty.set(path, ++changeSequence);
    announceState();
    if (dirty.size) queueSave(); else status('saved');
  });
  void start();
})();
