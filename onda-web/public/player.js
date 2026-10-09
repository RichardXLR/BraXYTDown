'use strict';

(() => {
  const input = document.getElementById('video-url');
  const region = document.getElementById('source-player');
  const stage = document.getElementById('source-player-stage');
  const video = document.getElementById('source-video-player');
  const audio = document.getElementById('source-audio-player');
  const frame = document.getElementById('source-embed-player');
  const empty = document.getElementById('source-player-empty');
  const status = document.getElementById('source-player-status');
  const feedback = document.getElementById('source-preview-feedback');
  const external = document.getElementById('source-player-external');
  const retry = document.getElementById('retry-source-preview');
  if (!input || !region || !stage || !video || !frame || !empty || !status || !external || !retry) return;

  const embedPaths = new Map([
    ['www.youtube-nocookie.com', /^\/embed\/[A-Za-z0-9_-]{11}$/],
    ['www.tiktok.com', /^\/player\/v1\/\d{10,24}$/],
    ['player.vimeo.com', /^\/video\/\d{1,15}$/],
    ['www.dailymotion.com', /^\/embed\/video\/[A-Za-z0-9]{5,16}$/],
    ['player.twitch.tv', /^\/$/],
    ['clips.twitch.tv', /^\/embed$/],
    ['www.facebook.com', /^\/plugins\/video\.php$/],
    ['www.instagram.com', /^\/(?:p|reel|tv)\/[A-Za-z0-9_-]{5,32}\/embed\/$/],
    ['player.bilibili.com', /^\/player\.html$/],
  ]);
  let controller = null;
  let timer = null;
  let version = 0;
  let currentLink = '';
  let descriptor = null;
  let descriptorTime = 0;
  let phase = 'idle';
  let tab = document.querySelector('[data-workspace-tab][aria-selected="true"]')?.dataset.workspaceTab || 'download';
  let pausedEmbed = false;
  let signedOut = false;

  function publicLink(value, secure = false) {
    try {
      if (/[\u0000-\u0020\u007f\\]/.test(value)) return null;
      const url = new URL(value);
      if (!['http:', 'https:'].includes(url.protocol) || (secure && url.protocol !== 'https:') || url.username || url.password) return null;
      if (url.port && !((url.protocol === 'https:' && url.port === '443') || (url.protocol === 'http:' && url.port === '80'))) return null;
      const host = url.hostname.toLowerCase().replace(/\.$/, '');
      if (!host.includes('.') || /(?:^|\.)(?:localhost|local|internal)$/.test(host)) return null;
      if (/^(?:0\.|10\.|127\.|169\.254\.|192\.168\.|172\.(?:1[6-9]|2\d|3[01])\.|224\.|240\.)/.test(host)) return null;
      return url;
    } catch { return null; }
  }

  function allowedEmbed(value) {
    const url = publicLink(value, true);
    return url && embedPaths.get(url.hostname)?.test(url.pathname) ? url.href : null;
  }

  function cancelRequest() {
    clearTimeout(timer);
    timer = null;
    version += 1;
    controller?.abort();
    controller = null;
  }

  function clearMedia() {
    for (const media of [video, audio]) {
      if (!media) continue;
      media.pause();
      media.hidden = true;
      if (media.hasAttribute('src')) {
        media.removeAttribute('src');
        media.load();
      }
    }
    frame.hidden = true;
    frame.removeAttribute('src');
    pausedEmbed = false;
    empty.hidden = true;
  }

  function showState(state, message) {
    region.dataset.state = state;
    stage.setAttribute('aria-busy', String(state === 'loading'));
    status.textContent = message;
    // An unsupported or incomplete link never reserves an empty video panel.
    region.hidden = !(['ready', 'paused'].includes(state) && ['video', 'vertical', 'audio'].includes(region.dataset.kind));
    if (feedback) feedback.hidden = state === 'empty';
  }

  function clear() {
    cancelRequest();
    clearMedia();
    currentLink = '';
    descriptor = null;
    region.dataset.kind = 'empty';
    retry.hidden = true;
    external.hidden = true;
    external.removeAttribute('href');
    showState('empty', 'Cole um link para carregar o player. A reprodução depende do acesso e do suporte da plataforma.');
  }

  function canPresent() {
    return !signedOut && phase === 'idle' && tab === 'download' && !document.hidden && !document.body.classList.contains('intro-open');
  }

  function pause() {
    video.pause();
    audio?.pause();
    if (frame.hasAttribute('src')) {
      // Removing the frame document stops video/audio for every provider,
      // including providers with no public postMessage pause API.
      frame.removeAttribute('src');
      frame.hidden = true;
      empty.hidden = false;
      pausedEmbed = true;
    }
  }

  function showDescriptor(data) {
    clearMedia();
    const kind = data.kind;
    retry.hidden = false;
    if (kind === 'embed') {
      const source = allowedEmbed(data.url);
      if (!source) throw new Error('O serviço retornou uma prévia incompatível.');
      region.dataset.kind = data.vertical ? 'vertical' : 'video';
      frame.title = `${String(data.provider || 'Origem').slice(0, 60)} — ${String(data.title || 'Prévia').slice(0, 240)}`;
      if (canPresent()) {
        frame.src = source;
        frame.hidden = false;
        empty.hidden = true;
      } else pausedEmbed = true;
      showState('ready', data.message || 'Toque em reproduzir para assistir com som.');
    } else if (kind === 'direct') {
      const source = publicLink(data.url, true);
      const target = data.media_type === 'audio' ? audio : data.media_type === 'video' ? video : null;
      if (!source || !target) throw new Error('O serviço retornou uma mídia incompatível.');
      region.dataset.kind = data.media_type;
      target.preload = 'none';
      target.controls = true;
      target.src = source.href;
      target.hidden = false;
      empty.hidden = true;
      showState('ready', data.message || 'Toque em reproduzir. O arquivo vem diretamente da origem.');
    } else if (kind === 'unavailable') {
      region.dataset.kind = 'unavailable';
      showState('unavailable', data.message || 'A origem não disponibiliza uma prévia dentro do site. Use Abrir na fonte.');
    } else throw new Error('O serviço retornou uma prévia incompatível.');
  }

  async function refresh({ withCookies = false, force = false } = {}) {
    clearTimeout(timer);
    timer = null;
    const link = input.value.trim();
    const parsed = publicLink(link);
    if (!link) { clear(); return; }
    if (!parsed) {
      clear();
      showState('unavailable', 'Cole um link HTTP ou HTTPS público válido para carregar o player.');
      return;
    }
    if (!canPresent()) return;
    // Input/change, source updates and tab events may arrive together. Reuse
    // the request already in flight rather than aborting and extracting twice.
    if (!force && controller && currentLink === link) return;
    if (!force && currentLink === link && descriptor && Date.now() - descriptorTime < 3 * 60 * 1000) {
      if (pausedEmbed && canPresent()) showDescriptor(descriptor);
      else if (region.dataset.state === 'paused' && canPresent()) showState('ready', descriptor.message || 'Toque em reproduzir para assistir com som.');
      return;
    }
    cancelRequest();
    const requestVersion = version;
    const requestController = new AbortController();
    controller = requestController;
    currentLink = link;
    descriptor = null;
    clearMedia();
    external.href = parsed.href;
    external.hidden = false;
    retry.hidden = true;
    region.dataset.kind = 'loading';
    showState('loading', 'Preparando o player da fonte…');
    const payload = { url: link };
    // Automatic previews never upload sensitive session data. Only this
    // explicit button action may use the currently supplied session.
    if (withCookies) {
      const session = window.OndaSession?.requestOptions();
      if (session === null) {
        controller = null;
        retry.hidden = false;
        showState('unavailable', 'Confira os campos de Acesso à fonte antes de atualizar a prévia.');
        return;
      }
      if (session) Object.assign(payload, session);
    }
    let timedOut = false;
    const timeout = setTimeout(() => { timedOut = true; requestController.abort(); }, 30000);
    try {
      const response = await window.OndaAuth.fetch('/api/player', {
        method: 'POST', credentials: 'same-origin', cache: 'no-store',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload), signal: requestController.signal,
      });
      if (requestVersion !== version || input.value.trim() !== link) return;
      if (requestController.signal.aborted) throw new DOMException('Prévia cancelada.', 'AbortError');
      const type = response.headers.get('Content-Type') || '';
      if (!type.toLowerCase().includes('application/json')) throw new Error('O serviço de prévia não respondeu. Tente novamente.');
      const data = await response.json();
      if (requestVersion !== version || input.value.trim() !== link) return;
      if (requestController.signal.aborted) throw new DOMException('Prévia cancelada.', 'AbortError');
      if (!response.ok) throw new Error(data.error || 'Não foi possível preparar a prévia agora.');
      descriptor = data;
      descriptorTime = Date.now();
      showDescriptor(data);
    } catch (error) {
      if (requestVersion !== version) return;
      if (requestController.signal.aborted && !timedOut) return;
      clearMedia();
      region.dataset.kind = 'unavailable';
      retry.hidden = false;
      showState('unavailable', timedOut ? 'A origem demorou demais. Use Atualizar prévia para tentar de novo.' : (error.message || 'Não foi possível preparar a prévia agora.'));
    } finally {
      clearTimeout(timeout);
      if (controller === requestController) controller = null;
    }
  }

  function schedule() {
    // Stop the previous source immediately; no audio continues while editing.
    if (input.value.trim() !== currentLink) {
      cancelRequest();
      clearMedia();
      descriptor = null;
      external.hidden = true;
      retry.hidden = true;
      showState('empty', 'Cole o link completo para preparar a prévia.');
    }
    clearTimeout(timer);
    timer = setTimeout(() => refresh(), 600);
  }

  input.addEventListener('input', schedule);
  input.addEventListener('change', () => refresh());
  window.addEventListener('onda:source', schedule);
  retry.addEventListener('click', () => refresh({ withCookies: true, force: true }));
  window.addEventListener('onda:phase', (event) => {
    phase = event.detail?.phase || 'idle';
    retry.disabled = phase !== 'idle';
    if (phase !== 'idle') {
      cancelRequest();
      pause();
      if (currentLink) showState('paused', 'A prévia fica pausada durante o processamento. Seu download continua normalmente.');
    }
    else if (canPresent()) refresh();
  });
  window.addEventListener('onda:tab', (event) => {
    tab = event.detail?.tab || 'download';
    if (tab !== 'download') { cancelRequest(); pause(); }
    else refresh();
  });
  const observer = new MutationObserver(() => {
    if (document.body.classList.contains('intro-open')) { cancelRequest(); pause(); }
    else if (canPresent()) refresh();
  });
  observer.observe(document.body, { attributes: true, attributeFilter: ['class'] });
  document.addEventListener('visibilitychange', () => {
    if (document.hidden) { cancelRequest(); pause(); }
    else refresh();
  });
  window.addEventListener('onda:auth', (event) => {
    if (!event.detail?.signedIn) { signedOut = true; clear(); }
  });
  window.addEventListener('pagehide', () => { cancelRequest(); clearMedia(); observer.disconnect(); });
  window.addEventListener('pageshow', (event) => {
    if (!event.persisted) return;
    observer.observe(document.body, { attributes: true, attributeFilter: ['class'] });
    if (descriptor && canPresent()) showDescriptor(descriptor);
    else refresh();
  });

  for (const media of [video, audio]) {
    media?.addEventListener('error', () => {
      if (!media.hasAttribute('src') || media.hidden || descriptor?.kind !== 'direct') return;
      clearMedia();
      region.dataset.kind = 'unavailable';
      showState('unavailable', 'O navegador ou a origem não permitiu reproduzir este arquivo. Tente atualizar a prévia ou use Abrir na fonte.');
      retry.hidden = false;
    });
    media?.addEventListener('play', () => {
      if (!canPresent()) { media.pause(); return; }
      showState('ready', 'Reproduzindo a prévia da origem. Use os controles do player para ajustar o som.');
    });
  }
  // Provider frames often report access blocks inside their own UI. A load
  // event confirms neither playback nor availability, so no fake success test.
  window.OndaPlayer = Object.freeze({ refresh, pause, clear });
  if (input.value.trim()) refresh();
})();
