'use strict';

(() => {
  const reduceMotion = matchMedia('(prefers-reduced-motion: reduce)');
  const systemTheme = matchMedia('(prefers-color-scheme: light)');
  const desktop = matchMedia('(min-width: 1080px)');
  const soundKey = 'onda.preferences.sound.v1';
  const preferencesKey = 'onda.preferences.v2';
  const introSeenKey = 'onda.intro.seen.v2';
  const defaults = Object.freeze({ accent: 'blue', theme: 'dark', density: 'comfortable', motion: true, intro: true });
  const allowed = { accent: ['blue', 'cyan', 'violet'], theme: ['dark', 'light', 'system'], density: ['comfortable', 'compact'] };
  const preferenceControls = Object.fromEntries(Object.keys(defaults).map((name) => [name, document.getElementById(`preference-${name}`)]));
  const workspaceTabs = [...document.querySelectorAll('[data-workspace-tab]')];
  const workspacePanels = [...document.querySelectorAll('[data-workspace-panel]')];
  const hashTabs = {
    '#compatibilidade': 'compatibility', '#perguntas': 'help', '#ajuda': 'help', '#formatos': 'help',
    '#historico': 'library', '#biblioteca': 'library', '#baixar': 'download', '#inicio': 'download',
    '#personalizacao': 'personalization', '#creditos': 'credits',
  };
  let activeWorkspace = 'download';
  let phase = 'idle';
  let soundEnabled = false;
  let soundContext = null;
  let lastTone = 0;
  let preferences = { ...defaults };
  const voices = new Set();

  function validatePreferences(value) {
    const result = { ...defaults };
    if (!value || typeof value !== 'object' || Array.isArray(value)) return result;
    for (const [name, choices] of Object.entries(allowed)) if (choices.includes(value[name])) result[name] = value[name];
    for (const name of ['motion', 'intro']) if (typeof value[name] === 'boolean') result[name] = value[name];
    return result;
  }
  try { preferences = validatePreferences(JSON.parse(window.OndaAccount.storage.getItem(preferencesKey))); } catch { /* Defaults also work without storage. */ }
  try { soundEnabled = window.OndaAccount.storage.getItem(soundKey) === '1'; } catch { /* Optional preference. */ }
  function motionAllowed() { return preferences.motion && !reduceMotion.matches; }

  function applyPreferences() {
    const theme = preferences.theme === 'system' ? (systemTheme.matches ? 'light' : 'dark') : preferences.theme;
    Object.assign(document.body.dataset, { accent: preferences.accent, theme, density: preferences.density, motion: motionAllowed() ? 'on' : 'off' });
    document.documentElement.style.colorScheme = theme;
    const themeColor = document.querySelector('meta[name="theme-color"]');
    if (themeColor) themeColor.content = theme === 'light' ? '#f4f7fc' : '#050d1b';
    for (const [name, control] of Object.entries(preferenceControls)) {
      if (!control) continue;
      if (control.type === 'checkbox') control.checked = preferences[name];
      else control.value = preferences[name];
    }
    window.dispatchEvent(new CustomEvent('onda:preferences', { detail: { ...preferences, motionAllowed: motionAllowed() } }));
  }
  function announcePreferences(message) {
    const status = document.getElementById('preferences-status');
    if (!status) return;
    let label = status.querySelector('[data-preferences-message]');
    if (!label) {
      label = document.createElement('span');
      label.dataset.preferencesMessage = '';
      for (const node of [...status.childNodes]) if (node.nodeType === Node.TEXT_NODE) node.remove();
      status.append(label);
    }
    label.textContent = message;
  }
  function persistPreferences() {
    try {
      window.OndaAccount.storage.setItem(preferencesKey, JSON.stringify(preferences));
      announcePreferences('Personalização aplicada. Sincronizando com sua conta…');
      return true;
    } catch {
      announcePreferences('Personalização aplicada. A sincronização ficou pendente.');
      return false;
    }
  }
  for (const [name, control] of Object.entries(preferenceControls)) control?.addEventListener('change', () => {
    const value = control.type === 'checkbox' ? control.checked : control.value;
    if (name in allowed && !allowed[name].includes(value)) return;
    const wasIntroEnabled = preferences.intro;
    preferences = validatePreferences({ ...preferences, [name]: value });
    if (name === 'intro' && preferences.intro && !wasIntroEnabled) {
      try { window.OndaAccount.storage.removeItem(introSeenKey); } catch { /* A visit still works without storage. */ }
    }
    applyPreferences();
    persistPreferences();
  });
  document.getElementById('reset-preferences')?.addEventListener('click', () => {
    preferences = { ...defaults };
    applyPreferences();
    if (persistPreferences()) announcePreferences('Personalização restaurada. Sincronizando com sua conta…');
  });
  reduceMotion.addEventListener('change', applyPreferences);
  systemTheme.addEventListener('change', () => { if (preferences.theme === 'system') applyPreferences(); });
  applyPreferences();

  function stopTones() {
    for (const voice of voices) { try { voice.stop(); } catch { /* The tone may already be stopped. */ } }
    voices.clear();
  }
  function tone(kind = 'tap') {
    if (!soundEnabled || document.hidden || document.querySelector('.intro-dialog[open]')) return;
    const now = performance.now();
    if (kind === 'tap' && now - lastTone < 70) return;
    lastTone = now;
    try {
      const Audio = window.AudioContext || window.webkitAudioContext;
      if (!Audio) return;
      soundContext ||= new Audio();
      if (soundContext.state === 'suspended') soundContext.resume().catch(() => {});
      const notes = kind === 'success' ? [[660, 0], [880, .09]] : kind === 'error' ? [[220, 0], [196, .08]] : [[kind === 'tab' ? 520 : 440, 0]];
      for (const [frequency, offset] of notes) {
        const oscillator = soundContext.createOscillator();
        const gain = soundContext.createGain();
        const start = soundContext.currentTime + offset;
        oscillator.type = 'sine';
        oscillator.frequency.value = frequency;
        gain.gain.setValueAtTime(0, start);
        gain.gain.linearRampToValueAtTime(.018, start + .008);
        gain.gain.exponentialRampToValueAtTime(.0001, start + .1);
        oscillator.connect(gain).connect(soundContext.destination);
        voices.add(oscillator);
        oscillator.onended = () => { voices.delete(oscillator); oscillator.disconnect(); gain.disconnect(); };
        oscillator.start(start);
        oscillator.stop(start + .11);
      }
    } catch { /* Sound never blocks a control or download. */ }
  }
  function renderSoundPreference() {
    for (const button of document.querySelectorAll('[data-sound-toggle]')) {
      button.setAttribute('aria-pressed', String(soundEnabled));
      button.setAttribute('aria-label', soundEnabled ? 'Desligar sons de interação' : 'Ativar sons de interação');
      button.title = soundEnabled ? 'Sons ligados · clique para desligar' : 'Ativar sons de interação';
      const label = button.querySelector('[data-sound-label]');
      if (label) label.textContent = soundEnabled ? 'Sons ligados' : 'Sons desligados';
      let icon = button.querySelector('svg');
      if (!icon) { icon = document.createElementNS('http://www.w3.org/2000/svg', 'svg'); button.prepend(icon); }
      icon.setAttribute('viewBox', '0 0 24 24');
      icon.setAttribute('aria-hidden', 'true');
      icon.setAttribute('focusable', 'false');
      const path = document.createElementNS('http://www.w3.org/2000/svg', 'path');
      path.setAttribute('d', soundEnabled
        ? 'm11 5-6 4H2v6h3l6 4ZM15 8a6 6 0 0 1 0 8M18 5a10 10 0 0 1 0 14'
        : 'm11 5-6 4H2v6h3l6 4ZM16 9l5 6m0-6-5 6');
      icon.replaceChildren(path);
      button.classList.toggle('sound-enabled', soundEnabled);
    }
    document.body.dataset.sound = soundEnabled ? 'on' : 'off';
  }
  function toggleSound() {
    soundEnabled = !soundEnabled;
    try { window.OndaAccount.storage.setItem(soundKey, soundEnabled ? '1' : '0'); } catch { /* Keep it for this page. */ }
    renderSoundPreference();
    if (soundEnabled) tone('tab'); else stopTones();
  }

  function activate(tab, { focus = false, updateURL = true } = {}) {
    if (!workspaceTabs.some((button) => button.dataset.workspaceTab === tab)) tab = 'download';
    if (!workspaceTabs.some((button) => button.dataset.workspaceTab === tab)) return;
    activeWorkspace = tab;
    for (const button of workspaceTabs) {
      const selected = button.dataset.workspaceTab === tab;
      button.setAttribute('aria-selected', String(selected));
      button.tabIndex = selected ? 0 : -1;
      button.classList.toggle('is-active', selected);
      if (selected) {
        button.removeAttribute('data-attention');
        if (!desktop.matches) requestAnimationFrame(() => button.scrollIntoView({ block: 'nearest', inline: 'center', behavior: motionAllowed() ? 'smooth' : 'auto' }));
      }
    }
    for (const panel of workspacePanels) {
      const selected = panel.dataset.workspacePanel === tab;
      panel.hidden = !selected;
      if (selected) {
        panel.classList.remove('panel-enter');
        if (motionAllowed()) requestAnimationFrame(() => panel.classList.add('panel-enter'));
        if (focus) {
          const destination = panel.querySelector('h1, h2') || panel;
          if (!destination.hasAttribute('tabindex')) destination.tabIndex = -1;
          destination.focus({ preventScroll: true });
        }
      }
    }
    if (updateURL) {
      try {
        const url = new URL(location.href);
        if (tab === 'download') url.searchParams.delete('tab'); else url.searchParams.set('tab', tab);
        url.hash = '';
        history.replaceState(null, '', url.pathname + url.search);
      } catch { /* Panels work without URL updates. */ }
    }
    dispatchEvent(new CustomEvent('onda:tab', { detail: { tab } }));
  }
  function tabKeyboard(event, button) {
    let index = workspaceTabs.indexOf(button);
    const forward = desktop.matches ? 'ArrowDown' : 'ArrowRight';
    const backward = desktop.matches ? 'ArrowUp' : 'ArrowLeft';
    if (event.key === forward) index = (index + 1) % workspaceTabs.length;
    else if (event.key === backward) index = (index - 1 + workspaceTabs.length) % workspaceTabs.length;
    else if (event.key === 'Home') index = 0;
    else if (event.key === 'End') index = workspaceTabs.length - 1;
    else return;
    event.preventDefault();
    const target = workspaceTabs[index];
    if (target && !target.disabled) { activate(target.dataset.workspaceTab); target.focus(); tone('tab'); }
  }
  for (const button of workspaceTabs) {
    button.addEventListener('click', () => { activate(button.dataset.workspaceTab); tone('tab'); });
    button.addEventListener('keydown', (event) => tabKeyboard(event, button));
  }
  function updateOrientation() {
    for (const list of document.querySelectorAll('[data-workspace-tablist]')) list.setAttribute('aria-orientation', desktop.matches ? 'vertical' : 'horizontal');
  }
  desktop.addEventListener('change', updateOrientation);
  updateOrientation();
  function mediaType() { return (window.OndaMedia?.type || document.body.dataset.mediaType) === 'video' ? 'video' : 'audio'; }

  document.addEventListener('click', (event) => {
    if (!(event.target instanceof Element)) return;
    const control = event.target.closest('button, a, summary, label, .choice, select');
    if (!control || control.disabled) return;
    if (control.matches('[data-sound-toggle]')) { toggleSound(); return; }
    if (!control.matches('[data-workspace-tab]')) tone();
  }, true);
  addEventListener('onda:phase', (event) => {
    phase = event.detail?.phase || 'idle';
    document.body.dataset.operation = phase;
    for (const label of document.querySelectorAll('[data-operation-label]')) {
      const noun = mediaType() === 'video' ? 'vídeo' : 'áudio';
      label.textContent = phase === 'converting' || phase === 'download' ? `Preparando ${noun}` : phase === 'inspect' || phase === 'compatibility' ? 'Verificando fonte' : 'Pronto para criar';
    }
  });
  addEventListener('onda:feedback', (event) => {
    const kind = event.detail?.kind;
    if (kind === 'success' || kind === 'error') tone(kind);
    if (activeWorkspace !== 'download' && (kind === 'success' || kind === 'error')) workspaceTabs.find((button) => button.dataset.workspaceTab === 'download')?.setAttribute('data-attention', kind);
  });
  addEventListener('onda:account-state', (event) => {
    const saved = event.detail?.state;
    if (!saved) return;
    preferences = validatePreferences(saved.preferences);
    soundEnabled = saved.sound === true;
    if (!soundEnabled) stopTones();
    applyPreferences();
    renderSoundPreference();
  });
  addEventListener('onda:account-sync', (event) => {
    const messages = { saved: 'Personalização salva na conta.', saving: 'Salvando suas escolhas na conta…', pending: 'Suas escolhas serão salvas automaticamente.', offline: 'Sem conexão. Suas escolhas aguardam sincronização.', error: 'Suas escolhas estão neste dispositivo. Tente sincronizar novamente.' };
    announcePreferences(messages[event.detail?.status] || 'Sincronizando suas escolhas…');
  });
  addEventListener('onda:auth', (event) => { if (!event.detail?.signedIn) stopTones(); });
  document.addEventListener('visibilitychange', () => { if (document.hidden) stopTones(); });
  addEventListener('pagehide', () => { stopTones(); soundContext?.close().catch(() => {}); soundContext = null; });
  renderSoundPreference();

  function tabFromURL() { return hashTabs[location.hash] || new URL(location.href).searchParams.get('tab') || 'download'; }
  activate(tabFromURL(), { updateURL: false });
  addEventListener('popstate', () => activate(tabFromURL(), { updateURL: false }));
  addEventListener('hashchange', () => activate(tabFromURL(), { updateURL: false }));
  for (const link of document.querySelectorAll('a[href]')) link.addEventListener('click', (event) => {
    if (event.button !== 0 || event.ctrlKey || event.metaKey || event.shiftKey || event.altKey || link.target === '_blank') return;
    let destination;
    try { destination = new URL(link.href, location.origin); } catch { return; }
    if (destination.origin !== location.origin || !['/', '/index.html'].includes(destination.pathname)) return;
    const tab = link.dataset.openWorkspace || hashTabs[destination.hash] || destination.searchParams.get('tab');
    if (!workspaceTabs.some((button) => button.dataset.workspaceTab === tab)) return;
    event.preventDefault();
    activate(tab, { focus: true });
    if (destination.hash && hashTabs[destination.hash]) {
      const target = document.getElementById(destination.hash.slice(1));
      if (target) { if (!target.hasAttribute('tabindex')) target.tabIndex = -1; target.focus(); }
    }
  });
  window.OndaUI = {
    activate, tone,
    get soundEnabled() { return soundEnabled; },
    get phase() { return phase; },
    get preferences() { return { ...preferences, motionAllowed: motionAllowed() }; },
  };
})();
