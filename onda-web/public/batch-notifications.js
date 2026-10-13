(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  const button = $('batch-notifications-toggle'), status = $('batch-notifications-status');
  if (!button || !status) return;
  const user = window.Clerk.user.id;
  const current = () => window.OndaAuth?.signedIn && window.Clerk.user?.id === user && !ended;
  const seen = new Set();
  const storageKey = `onda-batch-notices:${user}`;
  let registration = null, config = null, subscribed = false, busy = false, ended = false, dismissTimer = null;
  const notice = document.createElement('aside');
  notice.id = 'batch-ready-notice'; notice.className = 'batch-ready-notice'; notice.hidden = true;
  notice.setAttribute('aria-label', 'Arquivo pronto');
  notice.innerHTML = '<div class="batch-ready-icon" aria-hidden="true"><svg viewBox="0 0 24 24"><path d="M12 3v12m-4-4 4 4 4-4M5 16v4h14v-4"/></svg></div><div class="batch-ready-copy" role="status" aria-live="polite" aria-atomic="true"><strong>Seu ZIP está pronto</strong><p>O pacote já pode ser baixado.</p></div><button type="button" class="secondary-button" data-notice-open>Ver arquivo</button><button type="button" class="batch-notice-dismiss" aria-label="Dispensar aviso de arquivo pronto">×</button>';
  document.body.append(notice);
  function readSeen() {
    try { const saved = JSON.parse(localStorage.getItem(storageKey) || '[]'); if (Array.isArray(saved)) for (const key of saved.slice(-64)) if (typeof key === 'string' && key.length < 120) seen.add(key); } catch { /* Session-only deduplication remains available. */ }
  }
  function render(text) {
    button.disabled = busy || !supported();
    button.setAttribute('aria-pressed', String(subscribed));
    $('batch-notifications-label').textContent = busy ? 'Aguarde…' : subscribed ? 'Desativar avisos' : 'Ativar avisos';
    status.textContent = text || (subscribed ? 'Avisos ativos neste dispositivo.' : supported() ? 'Receba um aviso quando o ZIP estiver pronto.' : /iPhone|iPad|iPod/.test(navigator.userAgent) || /Macintosh/.test(navigator.userAgent) && navigator.maxTouchPoints > 1 ? 'No iPhone ou iPad, adicione o Onda à tela inicial para ativar os avisos.' : 'Este navegador mostra o aviso dentro do Onda.');
  }
  function supported() { return isSecureContext && 'Notification' in window && 'PushManager' in window && 'serviceWorker' in navigator; }
  function timed(promise, ms = 10000) {
    let timer;
    return Promise.race([promise, new Promise((_, reject) => { timer = setTimeout(() => reject(new Error('O navegador demorou para configurar os avisos. Tente novamente.')), ms); })]).finally(() => clearTimeout(timer));
  }
  async function api(path, body) {
    if (!current()) throw new DOMException('Sessão encerrada.', 'AbortError');
    const response = await window.OndaAuth.fetch(path, body ? { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body), cache: 'no-store' } : { cache: 'no-store' });
    const data = await response.json();
    if (!current()) throw new DOMException('Sessão encerrada.', 'AbortError');
    if (!response.ok) throw new Error(data.error || 'Os avisos não foram ativados. Tente novamente.');
    return data;
  }
  async function setup() {
    registration = await timed(navigator.serviceWorker.ready);
    if (!current()) throw new DOMException('Sessão encerrada.', 'AbortError');
    config = await api('/api/batch-notifications');
    if (!/^[a-f0-9]{64}$/.test(config.owner)) throw new Error('Não foi possível vincular os avisos à conta.');
  }
  async function deviceOwner(owner) {
    const worker = registration?.active;
    if (!worker) throw new Error('Atualize o aplicativo para ativar os avisos.');
    const channel = new MessageChannel();
    try {
      await timed(new Promise((resolve, reject) => {
        channel.port1.onmessage = event => event.data?.ok ? resolve() : reject(new Error('Não foi possível proteger os avisos neste dispositivo.'));
        worker.postMessage({ type: 'SET_PUSH_OWNER', owner }, [channel.port2]);
      }));
    } finally { channel.port1.close(); channel.port2.close(); }
  }
  function keyBytes(value) {
    const raw = atob(value.replace(/-/g, '+').replace(/_/g, '/') + '='.repeat((4 - value.length % 4) % 4));
    return Uint8Array.from(raw, char => char.charCodeAt(0));
  }
  function currentKey(subscription) {
    const existing = subscription?.options?.applicationServerKey, expected = keyBytes(config.publicKey);
    return !existing || existing.byteLength === expected.byteLength && [...new Uint8Array(existing)].every((byte, i) => byte === expected[i]);
  }
  async function bind(subscription) {
    await deviceOwner(config.owner);
    if (!current()) throw new DOMException('Sessão encerrada.', 'AbortError');
    await api('/api/batch-notifications', subscription.toJSON());
    subscribed = true;
  }
  async function toggle() {
    if (busy || !current() || !supported()) return;
    busy = true; render();
    try {
      if (subscribed) {
        const subscription = await timed(registration.pushManager.getSubscription());
        await deviceOwner(null);
        if (subscription) {
          if (!await subscription.unsubscribe()) throw new Error('Tente desativar os avisos novamente.');
          subscribed = false;
          await api('/api/batch-notifications/disable', { endpoint: subscription.endpoint });
        }
        subscribed = false;
      } else {
        // Permission is requested only during this explicit user gesture.
        const permission = Notification.permission === 'granted' ? 'granted' : await Notification.requestPermission();
        if (!current()) return;
        if (permission !== 'granted') { render(permission === 'denied' ? 'Permita notificações nas configurações deste site no navegador.' : 'Ative os avisos quando quiser. Sua fila continua normalmente.'); return; }
        await setup();
        let subscription = await timed(registration.pushManager.getSubscription());
        if (subscription && !currentKey(subscription)) { if (!await subscription.unsubscribe()) throw new Error('Não foi possível renovar os avisos. Tente novamente.'); subscription = null; }
        if (!subscription) subscription = await timed(registration.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: keyBytes(config.publicKey) }), 20000);
        if (!current()) { await subscription.unsubscribe(); return; }
        await bind(subscription);
      }
      render();
    } catch (error) { if (current()) render(error.name === 'AbortError' ? 'O serviço de avisos do navegador está indisponível. Tente ativar novamente.' : error.message || 'Os avisos não foram ativados. Tente novamente.'); }
    finally { busy = false; if (current()) { button.disabled = !supported(); $('batch-notifications-label').textContent = subscribed ? 'Desativar avisos' : 'Ativar avisos'; } }
  }
  function focus() {
    if (!current()) return;
    window.OndaUI?.activate('download');
    const center = $('batch-center');
    if (!center || center.hidden) return;
    center.open = true;
    center.scrollIntoView({ behavior: matchMedia('(prefers-reduced-motion: reduce)').matches ? 'instant' : 'smooth', block: 'center' });
    const target = center.querySelector('.batch-job-actions .secondary-button') || $('batch-refresh');
    target?.focus({ preventScroll: true });
    notice.hidden = true;
  }
  function ready(job) {
    if (!current() || job.status !== 'ready' || !job.file || job.expires * 1000 <= Date.now()) return;
    const key = `${job.id}:${job.created}`;
    if (seen.has(key)) return;
    // Consult persistent deduplication again to cooperate with other open tabs.
    readSeen(); if (seen.has(key)) return;
    seen.add(key);
    try { localStorage.setItem(storageKey, JSON.stringify([...seen].slice(-64))); } catch { /* No personal media values are persisted. */ }
    notice.hidden = false;
    clearTimeout(dismissTimer);
    dismissTimer = setTimeout(() => { if (!notice.contains(document.activeElement) && !notice.matches(':hover')) notice.hidden = true; }, 12000);
    dispatchEvent(new CustomEvent('onda:feedback', { detail: { kind: 'success' } }));
  }
  button.addEventListener('click', toggle);
  notice.querySelector('[data-notice-open]').addEventListener('click', focus);
  notice.querySelector('.batch-notice-dismiss').addEventListener('click', () => { notice.hidden = true; });
  document.addEventListener('pointerdown', event => { if (!notice.contains(event.target)) notice.hidden = true; });
  addEventListener('onda:phase', event => { if (event.detail?.phase && event.detail.phase !== 'idle') notice.hidden = true; });
  function workerMessage(event) {
    if (!current() || event.data?.owner !== config?.owner) return;
    if (event.data.type === 'BATCH_READY' || event.data.type === 'BATCH_NOTIFICATION_OPEN') {
      dispatchEvent(new CustomEvent('onda:batch-ready', { detail: { focus: event.data.type === 'BATCH_NOTIFICATION_OPEN' } }));
    }
  }
  navigator.serviceWorker?.addEventListener('message', workerMessage);
  function stop(event) {
    if (event.detail?.signedIn) return;
    ended = true; clearTimeout(dismissTimer); notice.remove();
    navigator.serviceWorker?.removeEventListener('message', workerMessage);
    // Revoke locally even if the Clerk session has already ended/offline.
    void (async () => {
      try {
        registration = registration || await timed(navigator.serviceWorker.ready);
        try { await deviceOwner(null); } finally {
          await (await timed(registration.pushManager.getSubscription()))?.unsubscribe();
          for (const notification of await registration.getNotifications()) if (notification.data?.owner === config?.owner) notification.close();
        }
      } catch { /* Revoked browser subscriptions are removed after provider 410. */ }
    })();
  }
  addEventListener('onda:auth', stop);
  window.OndaBatchNotifications = Object.freeze({ ready, focus });
  readSeen(); render();
  if (supported() && Notification.permission === 'granted') {
    busy = true; render();
    void (async () => {
      let error = null;
      try { await setup(); const subscription = await timed(registration.pushManager.getSubscription()); if (subscription && current() && currentKey(subscription)) await bind(subscription); }
      catch { error = 'Não foi possível verificar os avisos. Tente ativá-los novamente.'; }
      finally { busy = false; if (current()) render(error); }
    })();
  }
})();
