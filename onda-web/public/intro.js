'use strict';

(() => {
  const seenKey = 'onda.intro.seen.v2';
  const reducedMotion = matchMedia('(prefers-reduced-motion: reduce)');
  const replayButtons = [...document.querySelectorAll('[data-intro-replay]')];
  let dialog, video, playButton, soundButton, progress, hint, watchdog, closeTimer;
  let closing = false;
  let phase = 'idle';
  let generation = 0;
  let returnFocus = null;
  const soundOnPath = 'm11 5-6 4H2v6h3l6 4ZM15 8a6 6 0 0 1 0 8M18 5a10 10 0 0 1 0 14';
  const soundOffPath = 'm11 5-6 4H2v6h3l6 4ZM16 9l5 6m0-6-5 6';

  function motionAllowed() {
    return window.OndaUI?.preferences.motionAllowed ?? !reducedMotion.matches;
  }
  function renderSound() {
    soundButton.setAttribute('aria-pressed', String(!video.muted));
    soundButton.setAttribute('aria-label', video.muted ? 'Ativar áudio da abertura' : 'Silenciar áudio da abertura');
    soundButton.querySelector('[data-intro-sound-label]').textContent = video.muted ? 'Ativar áudio' : 'Silenciar áudio';
    soundButton.querySelector('path').setAttribute('d', video.muted ? soundOffPath : soundOnPath);
  }
  function stopMedia() {
    clearTimeout(watchdog);
    if (!video) return;
    video.pause();
    video.muted = true;
    video.removeAttribute('src');
    video.load();
  }
  function finish() {
    if (!dialog?.open || closing) return;
    closing = true;
    generation += 1;
    stopMedia();
    dialog.classList.add('intro-leaving');
    const close = () => {
      if (dialog.open) dialog.close();
      dialog.classList.remove('intro-leaving');
      closing = false;
      document.body.classList.remove('intro-open');
      if (returnFocus?.isConnected && returnFocus.getClientRects().length) returnFocus.focus({ preventScroll: true });
      else {
        const heading = [...document.querySelectorAll('main h1')].find((item) => item.getClientRects().length);
        if (heading) { heading.tabIndex = -1; heading.focus({ preventScroll: true }); }
      }
    };
    if (!motionAllowed()) close(); else closeTimer = setTimeout(close, 220);
  }
  function armWatchdog(delay = 8000) {
    clearTimeout(watchdog);
    const token = generation;
    watchdog = setTimeout(() => { if (token === generation && dialog?.open && !document.hidden) finish(); }, delay);
  }
  function build() {
    dialog = document.createElement('dialog');
    dialog.className = 'intro-dialog';
    dialog.setAttribute('aria-labelledby', 'intro-title');
    dialog.setAttribute('aria-describedby', 'intro-description');
    // Fixed application content only. No URL or media metadata enters this markup.
    dialog.innerHTML = `<div class="intro-inner">
      <header class="intro-header"><span class="intro-wordmark">Onda<span aria-hidden="true">.</span></span><button type="button" class="intro-skip" data-intro-skip>Pular abertura <span aria-hidden="true">→</span></button></header>
      <div class="intro-stage"><video id="onda-intro-video" muted playsinline preload="none" poster="/assets/onda-logo.webp" aria-label="Vídeo de abertura do Onda"></video><button type="button" class="intro-play" hidden>Assistir abertura <span aria-hidden="true">▶</span></button></div>
      <footer class="intro-footer"><div><p class="intro-eyebrow">BEM-VINDO AO SEU ESTÚDIO</p><h2 id="intro-title">Sua mídia. Suas escolhas.</h2><p id="intro-description">Áudio e vídeo, do link ao seu próximo arquivo.</p></div><button type="button" class="intro-sound" aria-pressed="false" aria-controls="onda-intro-video"><svg viewBox="0 0 24 24" aria-hidden="true" focusable="false"><path d="${soundOffPath}"/></svg><span data-intro-sound-label>Ativar áudio</span></button></footer>
      <div class="intro-progress-row"><progress class="intro-progress" max="100" value="0" aria-label="Progresso da abertura"></progress><span class="intro-duration" aria-hidden="true">0:30</span></div>
      <p class="intro-hint" aria-live="polite">Sem pressa. Você pode pular a qualquer momento.</p>
    </div>`;
    document.body.append(dialog);
    video = dialog.querySelector('video');
    playButton = dialog.querySelector('.intro-play');
    soundButton = dialog.querySelector('.intro-sound');
    progress = dialog.querySelector('progress');
    hint = dialog.querySelector('.intro-hint');
    dialog.querySelector('[data-intro-skip]').addEventListener('click', finish);
    dialog.addEventListener('cancel', (event) => { event.preventDefault(); finish(); });
    dialog.addEventListener('click', (event) => { if (event.target === dialog) finish(); });
    playButton.addEventListener('click', start);
    soundButton.addEventListener('click', () => {
      video.muted = !video.muted;
      renderSound();
      if (video.paused) start();
    });
    video.addEventListener('playing', () => {
      playButton.hidden = true;
      clearTimeout(watchdog);
      hint.textContent = 'Sem pressa. Você pode pular a qualquer momento.';
    });
    video.addEventListener('waiting', () => { if (dialog.open && video.hasAttribute('src')) armWatchdog(); });
    video.addEventListener('stalled', () => { if (dialog.open && video.hasAttribute('src')) armWatchdog(); });
    video.addEventListener('timeupdate', () => {
      if (Number.isFinite(video.duration) && video.duration > 0) progress.value = video.currentTime / video.duration * 100;
    });
    video.addEventListener('ended', finish);
    video.addEventListener('error', () => { if (dialog.open && video.hasAttribute('src')) finish(); });
    renderSound();
  }
  function loadMedia() {
    if (video.hasAttribute('src')) return;
    const small = matchMedia('(max-width: 700px)').matches || matchMedia('(pointer: coarse)').matches || navigator.connection?.saveData;
    dialog.querySelector('.intro-stage').dataset.size = small ? 'mobile' : 'desktop';
    video.src = small ? '/assets/onda-intro-mobile.mp4' : '/assets/onda-intro-desktop.mp4';
  }
  function start() {
    if (!dialog.open || closing) return;
    loadMedia();
    const token = generation;
    video.play().catch(() => {
      if (token !== generation || !dialog.open || closing) return;
      playButton.hidden = false;
      hint.textContent = 'Toque em Assistir abertura ou pule para entrar no Onda.';
    });
    armWatchdog();
  }
  function open({ automatic = false } = {}) {
    if (phase !== 'idle' || dialog?.open || closing || typeof HTMLDialogElement === 'undefined') return;
    if (!dialog) build();
    if (typeof dialog.showModal !== 'function') return;
    generation += 1;
    returnFocus = automatic ? null : document.activeElement;
    progress.value = 0;
    video.muted = true;
    renderSound();
    hint.textContent = 'Sem pressa. Você pode pular a qualquer momento.';
    playButton.hidden = motionAllowed();
    try { dialog.showModal(); } catch { return; }
    document.body.classList.add('intro-open');
    dialog.querySelector('[data-intro-skip]').focus({ preventScroll: true });
    try { localStorage.setItem(seenKey, '1'); } catch { /* No account or tracking identifier is needed. */ }
    if (motionAllowed()) start();
  }

  replayButtons.forEach((button) => button.addEventListener('click', () => open()));
  addEventListener('onda:phase', (event) => {
    phase = event.detail?.phase || 'idle';
    replayButtons.forEach((button) => { button.disabled = phase !== 'idle'; });
    if (phase !== 'idle') finish();
  });
  addEventListener('onda:preferences', (event) => {
    if (!event.detail?.intro || !event.detail?.motionAllowed) finish();
  });
  document.addEventListener('visibilitychange', () => {
    if (!dialog?.open || closing) return;
    if (document.hidden) { video.pause(); clearTimeout(watchdog); }
    else if (video.hasAttribute('src')) start();
  });
  addEventListener('pagehide', () => {
    generation += 1;
    clearTimeout(closeTimer);
    stopMedia();
    if (dialog?.open) dialog.close();
    dialog?.classList.remove('intro-leaving');
    document.body.classList.remove('intro-open');
    closing = false;
  });
  reducedMotion.addEventListener('change', () => { if (reducedMotion.matches) finish(); });
  let seen = false;
  try { seen = localStorage.getItem(seenKey) === '1'; } catch { /* Intro stays skippable without storage. */ }
  const preferences = window.OndaUI?.preferences;
  if (!seen && preferences?.intro !== false && motionAllowed() && !navigator.connection?.saveData) open({ automatic: true });
})();
