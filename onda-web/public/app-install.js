(() => {
  'use strict';
  const install = document.getElementById('app-install-button');
  const update = document.getElementById('app-update-button');
  const message = document.getElementById('app-install-message');
  const updateMessage = document.getElementById('app-update-message');
  const standalone = matchMedia('(display-mode: standalone)');
  let prompt = null, registration = null, phase = 'idle', updating = false, reloadRequested = false, installedThisSession = false;
  const ios = /iPad|iPhone|iPod/.test(navigator.userAgent) || /Macintosh/.test(navigator.userAgent) && navigator.maxTouchPoints > 1;
  const pending = () => phase !== 'idle' || Boolean(document.querySelector('#save-dialog[open]')) || document.getElementById('save-open')?.hidden === false;
  function render() {
    const installed = installedThisSession || standalone.matches || navigator.standalone === true;
    install.hidden = installed || !prompt;
    message.textContent = installed ? 'O Onda já está aberto como aplicativo.' : prompt ? 'Instale para abrir o Onda pela tela inicial.' : ios ? 'No Safari, toque em Compartilhar e depois em Adicionar à Tela de Início.' : 'No menu do navegador, procure Instalar aplicativo ou Adicionar à tela inicial.';
    document.getElementById('app-update-notice').hidden = !registration?.waiting;
    document.getElementById('app-update-shortcut').disabled = updating || pending();
    update.hidden = !registration?.waiting;
    update.disabled = updating || pending();
    updateMessage.hidden = !registration?.waiting;
    updateMessage.textContent = pending() ? 'Há uma atualização disponível. Conclua a operação e salve o arquivo antes de atualizar.' : 'Uma nova versão da interface está disponível.';
  }
  addEventListener('beforeinstallprompt', event => { event.preventDefault(); prompt = event; render(); });
  addEventListener('appinstalled', () => { prompt = null; installedThisSession = true; install.hidden = true; message.textContent = 'O Onda foi instalado. Abra pelo ícone na tela inicial.'; });
  addEventListener('onda:phase', event => { phase = event.detail?.phase || 'idle'; render(); });
  document.getElementById('save-dialog')?.addEventListener('close', render);
  standalone.addEventListener('change', render);
  install.addEventListener('click', async () => {
    if (!prompt) return;
    const current = prompt; prompt = null; install.disabled = true;
    try { await current.prompt(); await current.userChoice; }
    catch { message.textContent = 'Abra o menu do navegador para adicionar o Onda à tela inicial.'; }
    finally { install.disabled = false; render(); }
  });
  document.getElementById('app-update-shortcut').addEventListener('click', () => update.click());
  update.addEventListener('click', async () => {
    if (!registration?.waiting || pending() || updating) return;
    updating = true; render();
    try {
      if (window.OndaAuth?.signedIn && !await window.OndaAccount.flush()) throw new Error('Account changes pending');
      if (pending() || !registration?.waiting) return;
      reloadRequested = true;
      registration.waiting.postMessage({ type: 'ACTIVATE_UPDATE' });
    } catch { updateMessage.textContent = 'Suas alterações ainda não foram sincronizadas. Tente atualizar novamente.'; }
    finally { updating = false; update.disabled = pending(); document.getElementById('app-update-shortcut').disabled = pending(); }
  });
  navigator.serviceWorker?.addEventListener('controllerchange', () => { if (reloadRequested) location.reload(); });
  async function register() {
    if (!('serviceWorker' in navigator) || !isSecureContext) return;
    try {
      registration = await navigator.serviceWorker.register('/app-worker.js', { scope: '/', updateViaCache: 'none' });
      registration.addEventListener('updatefound', () => { const worker = registration.installing; worker?.addEventListener('statechange', render); });
      render();
      await registration.update();
    } catch { /* The network site remains fully usable if caching is unavailable. */ }
  }
  render();
  if ('requestIdleCallback' in window) requestIdleCallback(register, { timeout: 10000 }); else setTimeout(register, 400);
})();
