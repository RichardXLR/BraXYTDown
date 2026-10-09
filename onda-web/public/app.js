'use strict';

(() => {
  const $ = (id) => document.getElementById(id);
  const form = $('download-form');
  const input = $('video-url');
  const downloadButton = $('download-button');
  const inspectButton = $('inspect-button');
  const status = $('operation-status');
  const statusTitle = $('status-title');
  const statusMessage = $('status-message');
  const progressTrack = $('progress-track');
  const progressFill = $('progress-fill');
  const cancelButton = $('cancel-button');
  const saveButton = $('save-button');
  const saveDialog = $('save-dialog');
  const saveOpen = $('save-open');
  const HISTORY_KEY = 'onda.audio.history.v1';
  const DRAFT_KEY = 'onda.media.draft.v2';
  let draftRestored = false;
  let providersLoaded = false;
  let providersController = null;
  let maintenanceController = null;
  let maintenanceLoadedAt = 0;
  const LOSSLESS = new Set(['wav', 'flac', 'aiff']);
  const FORMATS = new Set(['mp3', 'm4a', 'wav', 'flac', 'ogg', 'opus', 'aac', 'aiff']);
  const VIDEO_FORMATS = new Set(['mp4', 'webm', 'mkv', 'mov']);
  const VIDEO_RESOLUTIONS = new Set(['source', '2160', '1440', '1080', '720', '480', '360']);
  const QUALITIES = new Set(['128', '192', '256', '320', 'source']);
  const EXAMPLE_URL = new URL('/canary.wav', window.location.origin).href;
  const VIDEO_EXAMPLE_URL = new URL('/canary.mp4', window.location.origin).href;
  const MAX_COOKIE_BYTES = 65536;
  const MAX_DOWNLOAD_BYTES = 100 * 1024 * 1024;
  let cookieValidationController = null;
  let sessionImportGeneration = 0;
  let validatedCookieLink = '';
  let cookieVault = null;
  let sessionLocked = false;
  let cookieSaveSequence = 0;
  let cookieSavePromise = Promise.resolve(true);
  let cookieMetadata = null;
  const cookieVaultReady = window.OndaCookieStore.open(window.Clerk.user.id).then((vault) => {
    if (sessionLocked) { vault.close(); return null; }
    cookieVault = vault;
    return vault;
  }).catch(() => null);
  let activeController = null;
  let activeKind = null;
  let timeoutId = null;
  let timedOut = false;
  let objectURL = null;
  let metadata = null;
  let metadataURL = '';
  let lastQuality = '320';
  let history = readHistory();
  let compatibilityStatusController = null;
  let currentMediaType = null;
  let clipboardGeneration = 0;

  function saveDraft() {
    if (!draftRestored) return;
    const fields = ['media_type', 'format', 'quality', 'video_format', 'video_resolution'];
    const draft = Object.fromEntries(fields.map((name) => [name, form.elements[name].value]));
    draft.url = input.value.trim().slice(0, 4096);
    draft.trim_start = $('trim-start').value.slice(0, 32);
    draft.trim_end = $('trim-end').value.slice(0, 32);
    for (const name of ['strip-metadata', 'normalize-audio', 'mute-video']) draft[name] = $(name).checked;
    try { window.OndaAccount.storage.setItem(DRAFT_KEY, JSON.stringify(draft)); } catch { /* The form remains usable without storage. */ }
  }

  function restoreDraft() {
    try {
      const draft = JSON.parse(window.OndaAccount.storage.getItem(DRAFT_KEY) || 'null');
      if (draft && typeof draft === 'object' && !Array.isArray(draft)) {
        const allowed = { media_type: new Set(['audio', 'video']), format: FORMATS, quality: new Set(['128', '192', '256', '320']),
          video_format: VIDEO_FORMATS, video_resolution: VIDEO_RESOLUTIONS };
        for (const [name, choices] of Object.entries(allowed)) if (choices.has(String(draft[name]))) form.elements[name].value = String(draft[name]);
        if (typeof draft.url === 'string' && draft.url.length <= 4096 && (!draft.url || isPublicURL(draft.url))) input.value = draft.url;
        for (const name of ['trim_start', 'trim_end']) if (typeof draft[name] === 'string' && draft[name].length <= 32) $(name.replace('_', '-')).value = draft[name];
        for (const name of ['strip-metadata', 'normalize-audio', 'mute-video']) if (typeof draft[name] === 'boolean') $(name).checked = draft[name];
      }
    } catch { /* Ignore invalid or unavailable local storage. */ }
    draftRestored = true;
  }

  async function loadProviders() {
    if (providersLoaded || providersController) return;
    const controller = new AbortController();
    providersController = controller;
    const timer = window.setTimeout(() => controller.abort(), 15000);
    try {
      const response = await window.OndaAuth.fetch('/api/compatibility/providers', { signal: controller.signal });
      if (!response.ok) throw new Error('catalog unavailable');
      const data = await response.json();
      if (sessionLocked || providersController !== controller || controller.signal.aborted) return;
      if (!Array.isArray(data.providers)) throw new Error('catalog invalid');
      const selector = $('compatibility-platform');
      const selected = selector.value;
      const fragment = document.createDocumentFragment();
      const all = document.createElement('option'); all.value = ''; all.textContent = 'Detectar automaticamente'; fragment.append(all);
      for (const provider of data.providers) {
        if (!provider || typeof provider.id !== 'string' || typeof provider.name !== 'string') continue;
        const option = document.createElement('option'); option.value = provider.id; option.textContent = provider.name; fragment.append(option);
      }
      selector.replaceChildren(fragment);
      if ([...selector.options].some((option) => option.value === selected)) selector.value = selected;
      providersLoaded = true;
    } catch { /* The automatic source test remains available; retry next time the tab opens. */ }
    finally { window.clearTimeout(timer); providersController = null; }
  }

  function renderMaintenance(data) {
    const labels = { active: 'AutoCura automático ativo', not_configured: 'AutoCura aguardando ativação',
      disabled: 'AutoCura pausado no GitHub', awaiting_first_run: 'Aguardando primeira verificação',
      checking: 'AutoCura em verificação', attention_required: 'AutoCura precisa de atenção',
      report_unconfirmed: 'Relatório aguardando confirmação', verification_unavailable: 'Estado do AutoCura não confirmado' };
    $('healing-title').textContent = labels[data.state] || 'Estado do AutoCura não confirmado';
    $('healing-message').textContent = typeof data.message === 'string' ? data.message : 'Atualize o status para verificar a manutenção.';
    const date = data.last_check ? new Date(data.last_check) : null;
    $('autocura-last-check').textContent = date && Number.isFinite(date.getTime()) ? date.toLocaleString('pt-BR') : 'Nenhuma execução verificada';
    $('autocura-next-check').textContent = typeof data.schedule === 'string' ? data.schedule : 'Aguardando ativação no GitHub';
    $('autocura-details').dataset.state = data.state || 'unknown';
    window.OndaMaintenanceCenter?.render(data);
  }

  async function loadMaintenance(force = false) {
    if (maintenanceController || (!force && Date.now() - maintenanceLoadedAt < 120000)) return;
    const controller = new AbortController(); maintenanceController = controller;
    const timer = window.setTimeout(() => controller.abort(), 15000);
    try {
      const response = await window.OndaAuth.fetch('/api/maintenance', { signal: controller.signal, cache: 'no-store' });
      if (!response.ok) throw new Error('maintenance unavailable');
      const data = await response.json();
      if (sessionLocked || maintenanceController !== controller || controller.signal.aborted) return;
      renderMaintenance(data);
      maintenanceLoadedAt = Date.now();
    } catch {
      if (sessionLocked || maintenanceController !== controller) return;
      renderMaintenance({ state: 'verification_unavailable', message: 'Não foi possível confirmar a manutenção nesta consulta. Atualize o status para tentar novamente.' });
    } finally { window.clearTimeout(timer); maintenanceController = null; }
  }

  function selectedMediaType() {
    return form.elements.media_type.value === 'video' ? 'video' : 'audio';
  }

  function mediaLabel(type = selectedMediaType()) {
    return type === 'video' ? 'vídeo' : 'áudio';
  }

  function parseTime(value) {
    const text = String(value).trim().replace(',', '.');
    if (!text) return null;
    if (/^\d+(?:\.\d{1,3})?$/.test(text)) {
      const seconds = Number(text);
      return Number.isFinite(seconds) ? seconds : undefined;
    }
    const parts = text.split(':');
    if (![2, 3].includes(parts.length) || !parts.slice(0, -1).every((part) => /^\d+$/.test(part)) || !/^\d{1,2}(?:\.\d{1,3})?$/.test(parts.at(-1))) return undefined;
    const numbers = parts.map(Number);
    if (numbers.at(-1) >= 60 || (parts.length === 3 && numbers[1] >= 60)) return undefined;
    const seconds = numbers.reduce((total, part) => total * 60 + part, 0);
    return Number.isFinite(seconds) ? seconds : undefined;
  }

  function timeForInput(seconds) {
    if (seconds == null || !Number.isFinite(Number(seconds))) return '';
    const value = Number(seconds);
    const hours = Math.floor(value / 3600);
    const minutes = Math.floor((value % 3600) / 60);
    const secondParts = (value % 60).toFixed(3).replace(/\.?0+$/, '').split('.');
    const remainder = secondParts[0].padStart(2, '0') + (secondParts[1] ? `.${secondParts[1]}` : '');
    return hours ? `${hours}:${String(minutes).padStart(2, '0')}:${remainder}` : `${minutes}:${remainder}`;
  }

  function updateTools() {
    const muted = selectedMediaType() === 'video' && $('mute-video').checked;
    $('normalize-audio').disabled = Boolean(activeController) || muted;
    $('mute-video').disabled = Boolean(activeController) || selectedMediaType() !== 'video';
    $('normalize-help').textContent = muted ? 'Indisponível em vídeo sem áudio. Desative “Vídeo sem áudio” para normalizar o volume.' : 'Ajusta o volume para uma referência comum. Não remove ruído nem recria qualidade.';
    const count = Number($('strip-metadata').checked) + Number($('normalize-audio').checked && !muted) + Number(muted) + Number(Boolean($('trim-start').value.trim() || $('trim-end').value.trim()));
    $('tools-active-count').textContent = `${count} ${count === 1 ? 'ativo' : 'ativos'}`;
    $('tools-error').hidden = true;
    $('trim-start').removeAttribute('aria-invalid');
    $('trim-end').removeAttribute('aria-invalid');
    if (window.OndaDownloadEstimate) updateDownloadEstimate();
  }

  function updateDownloadEstimate() {
    window.OndaDownloadEstimate?.render(metadataURL === input.value.trim() ? metadata : null, {
      media_type: selectedMediaType(), format: selectedFormat(), quality: selectedQuality(),
      video_resolution: form.elements.video_resolution.value,
      trim_start: parseTime($('trim-start').value), trim_end: parseTime($('trim-end').value),
      mute: selectedMediaType() === 'video' && $('mute-video').checked,
      normalize_audio: $('normalize-audio').checked, strip_metadata: $('strip-metadata').checked,
    });
  }

  function toolsError(message, field) {
    $('tools-error').textContent = message;
    $('tools-error').hidden = false;
    $('media-tools').open = true;
    window.OndaUI?.activate('download');
    field.setAttribute('aria-invalid', 'true');
    field.focus();
  }

  function getTools() {
    const start = parseTime($('trim-start').value);
    const end = parseTime($('trim-end').value);
    if (start === undefined) { toolsError('Informe o começo em mm:ss, hh:mm:ss ou segundos. Os segundos devem ser menores que 60 em horários.', $('trim-start')); return null; }
    if (end === undefined) { toolsError('Informe o fim em mm:ss, hh:mm:ss ou segundos. Os segundos devem ser menores que 60 em horários.', $('trim-end')); return null; }
    if (end !== null && end <= (start ?? 0)) { toolsError('O fim do trecho deve ser posterior ao começo.', $('trim-end')); return null; }
    const video = selectedMediaType() === 'video';
    const mute = video && $('mute-video').checked;
    return {
      video_resolution: video ? form.elements.video_resolution.value : 'source',
      trim_start: start,
      trim_end: end,
      strip_metadata: $('strip-metadata').checked,
      mute,
      normalize_audio: $('normalize-audio').checked && !mute,
    };
  }

  function resetTools() {
    $('trim-start').value = '';
    $('trim-end').value = '';
    $('strip-metadata').checked = true;
    $('normalize-audio').checked = false;
    $('mute-video').checked = false;
    updateTools();
  }

  function describeCookies(raw) {
    if (!raw) return { text: 'Nenhum cookie fornecido.' };
    const bytes = new TextEncoder().encode(raw).byteLength;
    if (bytes > MAX_COOKIE_BYTES) return { error: 'Este conteúdo ultrapassa 64 KB. Reduza ou limpe os cookies.' };
    if (raw.startsWith('[') || raw.startsWith('{')) {
      try {
        const data = JSON.parse(raw);
        const records = Array.isArray(data) ? data : data?.cookies;
        if (!Array.isArray(records) || !records.length || records.length > 300) return { error: 'O JSON deve conter uma lista de 1 a 300 cookies.' };
        return { text: `JSON · ${records.length} registros · ${formatBytes(bytes)}. Valide para o link informado.` };
      } catch { return { error: 'O JSON está incompleto ou inválido. Importe uma exportação de cookies válida.' }; }
    }
    if (!/^# (?:Netscape HTTP|HTTP) Cookie File(?:\r?\n|$)/.test(raw)) return { error: 'Use cookies Netscape (TXT) ou uma exportação JSON.' };
    return { text: `Netscape · ${formatBytes(bytes)}. Valide para o link informado.` };
  }

  function cookieContent(raw) {
    if (!raw.startsWith('{')) return raw;
    // Pasted browser-storage exports receive the same treatment as files:
    // origins and localStorage are never stored or sent as session data.
    try {
      const records = JSON.parse(raw).cookies;
      return Array.isArray(records) ? JSON.stringify(records) : raw;
    } catch { return raw; }
  }

  function updateCookieStatus(message, invalid = false) {
    validatedCookieLink = '';
    const content = describeCookies($('cookies-input').value.trim());
    const hasError = invalid || (!message && Boolean(content.error));
    $('cookies-status').textContent = message || content.error || content.text;
    $('cookies-status').classList.toggle('invalid', hasError);
    $('cookies-status').classList.remove('validated');
    if (hasError) $('cookies-input').setAttribute('aria-invalid', 'true');
    else $('cookies-input').removeAttribute('aria-invalid');
    updateCookieApplicability();
    window.OndaCookieCenter?.render({ cookies: cookieContent($('cookies-input').value.trim()), url: input.value.trim(), metadata: cookieMetadata });
  }

  function invalidateCookieValidation() {
    cookieValidationController?.abort();
    cookieValidationController = null;
    $('validate-cookies').disabled = Boolean(activeController);
    $('validate-cookies').textContent = 'Validar para este link';
    updateCookieStatus();
  }

  function cookieMemoryStatus(text, state = 'idle') {
    $('cookies-memory-status').textContent = text;
    $('cookies-memory-status').dataset.state = state;
  }

  function persistCookies() {
    const sequence = ++cookieSaveSequence;
    const raw = $('cookies-input').value.trim();
    const cookies = cookieContent(raw);
    const filename = $('cookies-filename').textContent.replace(/[\u0000-\u001f\u007f-\u009f]/g, '').slice(0, 160).replace(/[\ud800-\udbff]$/, '');
    const previousMetadata = window.OndaCookieCenter ? cookieMetadata : null;
    if (describeCookies(raw).error) {
      cookieMemoryStatus('Alteração não salva. Revise o formato dos cookies.', 'error');
      return Promise.resolve(false);
    }
    cookieMemoryStatus(cookies ? 'Salvando neste dispositivo…' : 'Removendo cookies salvos…', 'pending');
    cookieSavePromise = (async () => {
      const vault = await cookieVaultReady;
      if (sessionLocked || sequence !== cookieSaveSequence) return false;
      if (!vault) throw new Error('storage unavailable');
      const nextMetadata = cookies && window.OndaCookieCenter ? await window.OndaCookieCenter.createMetadata(cookies, previousMetadata) : null;
      if (sessionLocked || sequence !== cookieSaveSequence) return false;
      if (cookies) await vault.save(cookies, filename === 'Nenhum arquivo selecionado' ? '' : filename, nextMetadata);
      else await vault.remove();
      if (sessionLocked || sequence !== cookieSaveSequence) return false;
      if (window.OndaCookieCenter) cookieMetadata = nextMetadata;
      window.OndaCookieCenter?.render({ cookies, url: input.value.trim(), metadata: cookieMetadata });
      cookieMemoryStatus(cookies ? 'Salvos neste dispositivo até você remover.' : 'Nenhum cookie salvo neste dispositivo.', cookies ? 'saved' : 'idle');
      return true;
    })().catch(() => {
      if (!sessionLocked && sequence === cookieSaveSequence) cookieMemoryStatus('Não foi possível salvar neste dispositivo. Os campos continuam disponíveis nesta página.', 'error');
      return false;
    });
    return cookieSavePromise;
  }

  async function restoreSavedCookies() {
    const generation = sessionImportGeneration;
    cookieMemoryStatus('Verificando cookies salvos…', 'pending');
    try {
      const vault = await cookieVaultReady;
      if (!vault) throw new Error('storage unavailable');
      const saved = await vault.load();
      if (sessionLocked || generation !== sessionImportGeneration) return;
      if (saved) {
        const restoredMetadata = window.OndaCookieCenter ? await window.OndaCookieCenter.createMetadata(saved.cookies, saved.metadata || null) : null;
        if (sessionLocked || generation !== sessionImportGeneration) return;
        $('cookies-input').value = saved.cookies;
        $('cookies-filename').textContent = saved.filename || 'Sessão restaurada';
        cookieMetadata = restoredMetadata;
        updateCookieStatus();
      }
      cookieMemoryStatus(saved ? 'Restaurados deste dispositivo. Permanecem salvos até você remover.' : 'Nenhum cookie salvo neste dispositivo.', saved ? 'saved' : 'idle');
    } catch {
      if (!sessionLocked && generation === sessionImportGeneration) cookieMemoryStatus('O armazenamento local está indisponível. Você pode usar uma sessão temporária nesta página.', 'error');
    }
  }

  function accessOptions(report = false) {
    const password = $('video-password').value;
    const agent = $('session-user-agent').value.trim();
    const invalidPassword = password.length > 256 || /[\u0000-\u001f\u007f-\u009f\ud800-\udfff]/u.test(password);
    const invalidAgent = agent.length > 512 || /[^\x20-\x7e]/.test(agent);
    if (invalidPassword || invalidAgent) {
      if (report) {
        const field = $(invalidPassword ? 'video-password' : 'session-user-agent');
        field.setAttribute('aria-invalid', 'true');
        $('advanced-options').open = true;
        window.OndaUI?.activate('download');
        field.focus();
        announce(invalidPassword ? 'A senha do vídeo deve ter até 256 caracteres, sem caracteres de controle.' : 'A identificação deve ter até 512 caracteres de texto simples, sem quebras de linha.');
      }
      return null;
    }
    return { ...(password ? { video_password: password } : {}), ...(agent ? { user_agent: agent } : {}) };
  }

  function cookiesMatchSource(cookies, url) {
    if (!cookies || !url) return Boolean(cookies);
    if (detectSource(url) === 'Arquivo direto') return false;
    if (validatedCookieLink === url) return true;
    let host;
    let domains;
    try {
      host = new URL(url).hostname.toLowerCase().replace(/\.$/, '');
      if (cookies.startsWith('[') || cookies.startsWith('{')) {
        const data = JSON.parse(cookies);
        const records = Array.isArray(data) ? data : data.cookies;
        domains = records.map((cookie) => cookie.domain);
      } else {
        domains = cookies.split(/\r?\n/).slice(1).filter((line) => line.trim() && (!line.startsWith('#') || line.startsWith('#HttpOnly_'))).map((line) => line.replace(/^#HttpOnly_/, '').split('\t')[0]);
      }
      // This only decides whether to include an optional session. The server
      // still applies the public-suffix, HTTPS, expiry and cookie policies.
      if (!domains.length || domains.some((domain) => typeof domain !== 'string' || !/^\.?[a-z0-9.-]+$/i.test(domain))) return true;
      domains = domains.map((domain) => domain.toLowerCase().replace(/^\./, ''));
      const families = [
        [['youtube.com', 'youtube-nocookie.com', 'youtu.be'], ['youtube.com', 'youtube-nocookie.com', 'youtu.be', 'google.com', 'googlevideo.com', 'ytimg.com']],
        [['facebook.com', 'fb.watch', 'fb.com'], ['facebook.com', 'fb.watch', 'fb.com']],
        [['twitter.com', 'x.com', 't.co'], ['twitter.com', 'x.com', 't.co']],
      ];
      const family = families.find(([sources]) => sources.some((source) => host === source || host.endsWith(`.${source}`)));
      if (family) return domains.some((domain) => family[1].some((scope) => domain === scope || domain.endsWith(`.${scope}`)));
      const platforms = ['tiktok.com', 'vimeo.com', 'soundcloud.com', 'spotify.com', 'instagram.com', 'dailymotion.com', 'twitch.tv', 'bilibili.com', 'reddit.com', 'pinterest.com'];
      const platform = platforms.find((root) => host === root || host.endsWith(`.${root}`));
      return platform ? domains.some((domain) => domain === platform || domain.endsWith(`.${platform}`))
        : domains.some((domain) => host === domain || host.endsWith(`.${domain}`) || domain.endsWith(`.${host}`));
    } catch { return true; }
  }

  function updateCookieApplicability() {
    const cookies = $('cookies-input').value.trim();
    const url = input.value.trim();
    $('cookies-use-hint').textContent = !cookies ? 'Os cookies serão usados apenas nas ações que você iniciar.'
      : !url ? 'Seus cookies serão usados quando corresponderem à plataforma do link.'
      : cookiesMatchSource(cookies, url) ? 'Esta sessão será incluída nas suas solicitações para este link.'
      : 'Cookies preservados. Este link usa acesso público porque pertence a outra fonte.';
  }

  function getCookies() {
    const raw = $('cookies-input').value.trim();
    const content = describeCookies(raw);
    if (content.error) {
      updateCookieStatus();
      window.OndaUI?.activate('download');
      $('advanced-options').open = true;
      $('cookies-input').focus();
      announce(content.error);
      return null;
    }
    if (!accessOptions(true)) return null;
    return cookieContent(raw);
  }

  function withCookies(payload, cookies) {
    return { ...payload, ...(cookiesMatchSource(cookies, payload.url || input.value.trim()) ? { cookies } : {}), ...accessOptions() };
  }

  window.OndaSession = Object.freeze({
    requestOptions() {
      const cookies = getCookies();
      return cookies === null ? null : withCookies({}, cookies);
    },
    flushCookieSave() { return cookieSavePromise; },
  });

  async function validateCookies() {
    if (activeController || cookieValidationController) return;
    const url = getURL();
    if (!url) return;
    const cookies = getCookies();
    if (cookies === null) return;
    if (!cookies) { updateCookieStatus('Importe ou cole os cookies antes de validar.'); $('cookies-input').focus(); return; }
    const controller = new AbortController();
    cookieValidationController = controller;
    $('validate-cookies').disabled = true;
    $('validate-cookies').textContent = 'Validando…';
    updateCookieStatus('Verificando formato, plataforma e expiração…');
    const timeout = setTimeout(() => controller.abort(), 20000);
    try {
      const response = await window.OndaAuth.fetch('/api/session/validate', {
        method: 'POST', cache: 'no-store', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ url, cookies }), signal: controller.signal,
      });
      if (sessionLocked || cookieValidationController !== controller) return;
      if (controller.signal.aborted) throw new DOMException('Validação cancelada.', 'AbortError');
      if (!response.ok) throw await responseError(response);
      const data = await response.json();
      if (cookieValidationController !== controller || input.value.trim() !== url || cookieContent($('cookies-input').value.trim()) !== cookies) return;
      if (controller.signal.aborted) throw new DOMException('Validação cancelada.', 'AbortError');
      const count = Number(data.validCookies);
      if (!Number.isInteger(count) || count < 1 || count > 300) throw new Error('O serviço não confirmou a sessão. Valide novamente.');
      const ignored = Number(data.ignoredCookies) || 0;
      const expired = Number(data.expiredCookies) || 0;
      if (window.OndaCookieCenter) {
        const validatedMetadata = await window.OndaCookieCenter.createValidation(cookies, url, data, cookieMetadata);
        if (sessionLocked || cookieValidationController !== controller || controller.signal.aborted
            || input.value.trim() !== url || cookieContent($('cookies-input').value.trim()) !== cookies) return;
        cookieMetadata = validatedMetadata;
        await persistCookies();
        if (sessionLocked || cookieValidationController !== controller || controller.signal.aborted
            || input.value.trim() !== url || cookieContent($('cookies-input').value.trim()) !== cookies) return;
      }
      const details = [`${count} cookies compatíveis com a plataforma`];
      if (ignored) details.push(`${ignored} de outros escopos ignorados`);
      if (expired) details.push(`${expired} expirados ignorados`);
      updateCookieStatus(`${details.join(' · ')}. A disponibilidade do vídeo é verificada ao baixar.`);
      $('cookies-status').classList.add('validated');
      validatedCookieLink = url;
      updateCookieApplicability();
      announce('Cookies validados para o link informado.');
    } catch (error) {
      if (sessionLocked || cookieValidationController !== controller || input.value.trim() !== url
          || cookieContent($('cookies-input').value.trim()) !== cookies) return;
      if (window.OndaCookieCenter) {
        try {
          const failedMetadata = await window.OndaCookieCenter.createValidationFailure(cookies, url,
            error.name === 'AbortError' ? 'timeout' : error.code, cookieMetadata);
          if (sessionLocked || cookieValidationController !== controller || input.value.trim() !== url
              || cookieContent($('cookies-input').value.trim()) !== cookies) return;
          cookieMetadata = failedMetadata;
          await persistCookies();
        } catch { /* Failure details remain visible when local storage is unavailable. */ }
      }
      if (!sessionLocked && cookieValidationController === controller && input.value.trim() === url
          && cookieContent($('cookies-input').value.trim()) === cookies) updateCookieStatus(error.name === 'AbortError' ? 'A validação demorou demais. Tente novamente.' : error.message, true);
    } finally {
      clearTimeout(timeout);
      if (cookieValidationController === controller) {
        cookieValidationController = null;
        $('validate-cookies').disabled = Boolean(activeController);
        $('validate-cookies').textContent = 'Validar para este link';
      }
    }
  }

  function clearSessionFields() {
    sessionImportGeneration += 1;
    cookieMetadata = null;
    invalidateCookieValidation();
    $('cookies-input').value = '';
    $('cookies-file').value = '';
    $('cookies-filename').textContent = 'Nenhum arquivo selecionado';
    $('video-password').value = '';
    $('video-password').type = 'password';
    $('video-password').removeAttribute('aria-invalid');
    $('toggle-video-password').textContent = 'Mostrar';
    $('toggle-video-password').setAttribute('aria-pressed', 'false');
    $('toggle-video-password').setAttribute('aria-label', 'Mostrar senha do vídeo');
    $('session-user-agent').value = '';
    $('session-user-agent').removeAttribute('aria-invalid');
    updateCookieStatus();
  }

  function lockSourceSession() {
    sessionLocked = true;
    cookieSaveSequence += 1;
    cookieVault?.close();
    clearSessionFields();
  }

  function selectedFormat() {
    return selectedMediaType() === 'video' ? form.elements.video_format.value : form.elements.format.value;
  }

  function selectedQuality() {
    return selectedMediaType() === 'video' || LOSSLESS.has(selectedFormat()) ? 'source' : form.elements.quality.value;
  }

  function announce(message) {
    $('announcement').textContent = message;
  }

  function isPublicURL(value) {
    try {
      const url = new URL(value);
      return ['https:', 'http:'].includes(url.protocol) && !url.username && !url.password && Boolean(url.hostname);
    } catch {
      return false;
    }
  }

  function getURL() {
    const value = input.value.trim();
    if (!isPublicURL(value)) {
      input.setAttribute('aria-invalid', 'true');
      showStatus('error', 'Esse link precisa de um ajuste', 'Cole um endereço completo começando com https:// ou http://.');
      input.focus();
      return null;
    }
    input.removeAttribute('aria-invalid');
    return value;
  }

  function detectSource(value) {
    try {
      const url = new URL(value);
      const host = url.hostname.toLowerCase().replace(/\.$/, '').replace(/^www\./, '');
      if (host === 'youtu.be' || host === 'youtube.com' || host.endsWith('.youtube.com')) return 'YouTube';
      if (host === 'tiktok.com' || host.endsWith('.tiktok.com')) return 'TikTok';
      if (host === 'soundcloud.com' || host.endsWith('.soundcloud.com')) return 'SoundCloud';
      if (host === 'vimeo.com' || host.endsWith('.vimeo.com')) return 'Vimeo';
      if (host === 'instagram.com' || host.endsWith('.instagram.com')) return 'Instagram';
      // Query strings and fragments can contain filename-like values. Match
      // only the actual media path, using the formats accepted by the service.
      if (/\.(mp3|m4a|mp4|webm|mov|wav|flac|aac|ogg|opus|aif|aiff|avi|mkv|mpeg|mpg|m4v|wma|ts|m2ts)$/i.test(url.pathname)) return 'Arquivo direto';
      return host;
    } catch {
      return '';
    }
  }

  function updateSource() {
    // Every edit invalidates clipboard reads, even when an edited link returns
    // to its previous value before the permission prompt finishes.
    clipboardGeneration += 1;
    if (validatedCookieLink && validatedCookieLink !== input.value.trim()) invalidateCookieValidation();
    const source = detectSource(input.value.trim());
    updateCookieApplicability();
    $('source-label').textContent = source ? `${source} · acesso depende da disponibilidade da fonte` : 'YouTube, TikTok e outras fontes suportadas';
    $('url-help').classList.toggle('detected', Boolean(source));
    inspectButton.disabled = Boolean(activeController) || !isPublicURL(input.value.trim());
    if (input.value.trim() !== metadataURL) {
      metadata = null;
      metadataURL = '';
      $('media-preview').hidden = true;
    }
    input.removeAttribute('aria-invalid');
    window.dispatchEvent(new CustomEvent('onda:source'));
    saveDraft();
    if (window.OndaDownloadEstimate) updateDownloadEstimate();
    window.OndaCookieCenter?.render({ cookies: cookieContent($('cookies-input').value.trim()), url: input.value.trim(), metadata: cookieMetadata });
  }

  function updateChoices() {
    const type = selectedMediaType();
    const video = type === 'video';
    const format = selectedFormat();
    const lossless = LOSSLESS.has(format);
    form.querySelectorAll('.choice').forEach((label) => {
      label.classList.toggle('selected', label.querySelector('input').checked);
    });
    form.querySelectorAll('.media-type-choice').forEach((label) => label.classList.toggle('selected', label.querySelector('input').checked));
    $('audio-format-fieldset').hidden = video;
    $('audio-format-fieldset').disabled = video;
    $('audio-quality-fieldset').hidden = video;
    $('audio-quality-fieldset').disabled = video;
    $('video-format-fieldset').hidden = !video;
    $('video-format-fieldset').disabled = !video;
    $('video-resolution-fieldset').hidden = !video;
    $('video-resolution-fieldset').disabled = !video;
    $('mute-video-option').hidden = !video;
    $('quality-chips').hidden = lossless;
    $('lossless-choice').hidden = !lossless;
    $('button-format').textContent = format.toUpperCase();
    $('quality-note').textContent = video ? 'Até 4K (2160p) e 30 fps. A orientação é preservada; fontes menores não são ampliadas.' : lossless
      ? 'Formato sem perda. A conversão não aumenta a qualidade da fonte.'
      : 'O arquivo final depende da qualidade do áudio de origem.';
    if (!video && !lossless) lastQuality = form.elements.quality.value;
    $('download-button-label').textContent = `Baixar ${mediaLabel(type)}`;
    $('studio-format-count').textContent = video ? '4 formatos de vídeo' : '8 formatos de áudio';
    $('card-mode-title').textContent = video ? 'VÍDEO POR LINK' : 'ÁUDIO POR LINK';
    $('hero-media-word').textContent = video ? 'seu vídeo.' : 'seu som.';
    $('download-limits').textContent = video ? 'Sem limite por duração · 4K / 30 fps · até 100 MB por arquivo' : 'Sem limite por duração · até 100 MB por arquivo';
    document.body.dataset.mediaType = type;
    updateTools();
    if (currentMediaType !== type) {
      if (currentMediaType !== null) { releaseFile(); status.hidden = true; metadata = null; metadataURL = ''; $('media-preview').hidden = true; }
      currentMediaType = type;
      try {
        const url = new URL(window.location.href);
        if (video) url.searchParams.set('media', 'video'); else url.searchParams.delete('media');
        window.history.replaceState(null, '', url.pathname + url.search + url.hash);
      } catch { /* Mode still works when history is unavailable. */ }
      window.dispatchEvent(new CustomEvent('onda:media', { detail: { media_type: type } }));
    }
    if (window.OndaDownloadEstimate) updateDownloadEstimate();
  }

  function showStatus(kind, title, message) {
    status.hidden = false;
    status.className = `operation-status ${kind}`;
    statusTitle.textContent = title;
    statusMessage.textContent = message;
    if (activeKind !== 'download') $('download-progress').hidden = true;
    if (activeKind === 'download' && ['error', 'info'].includes(kind)) $('download-progress').dataset.state = 'stopped';
    window.dispatchEvent(new CustomEvent('onda:feedback', { detail: { kind } }));
    if (kind !== 'success') saveButton.hidden = true;
    if (kind !== 'loading') {
      progressTrack.hidden = true;
      progressTrack.classList.remove('indeterminate');
    }
  }

  function releaseFile() {
    if (saveDialog.open) saveDialog.close();
    saveOpen.hidden = true;
    if (objectURL) URL.revokeObjectURL(objectURL);
    objectURL = null;
    saveButton.hidden = true;
    saveButton.removeAttribute('href');
    saveButton.removeAttribute('download');
  }

  function setBusy(kind) {
    if (kind && cookieValidationController) invalidateCookieValidation();
    activeKind = kind;
    window.dispatchEvent(new CustomEvent('onda:phase', { detail: { phase: kind || 'idle' } }));
    downloadButton.disabled = Boolean(kind);
    inspectButton.disabled = Boolean(kind) || !isPublicURL(input.value.trim());
    input.readOnly = Boolean(kind);
    $('paste-button').disabled = Boolean(kind);
    $('example-button').disabled = Boolean(kind);
    form.querySelectorAll('input[type="radio"]').forEach((radio) => { radio.disabled = Boolean(kind); });
    $('strip-metadata').disabled = Boolean(kind);
    $('normalize-audio').disabled = Boolean(kind) || (selectedMediaType() === 'video' && $('mute-video').checked);
    $('mute-video').disabled = Boolean(kind) || selectedMediaType() !== 'video';
    $('trim-start').readOnly = Boolean(kind);
    $('trim-end').readOnly = Boolean(kind);
    $('reset-tools').disabled = Boolean(kind);
    cancelButton.hidden = !kind || kind === 'compatibility';
    $('cookies-input').readOnly = Boolean(kind);
    $('cookies-file').disabled = Boolean(kind);
    $('clear-cookies').disabled = Boolean(kind);
    $('import-cookies').disabled = Boolean(kind);
    $('validate-cookies').disabled = Boolean(kind) || Boolean(cookieValidationController);
    $('video-password').readOnly = Boolean(kind);
    $('session-user-agent').readOnly = Boolean(kind);
    $('toggle-video-password').disabled = Boolean(kind);
    $('use-browser-agent').disabled = Boolean(kind);
    $('cookies-dropzone').setAttribute('aria-disabled', String(Boolean(kind)));
    $('test-current-link').disabled = Boolean(kind);
    $('test-platform-link').disabled = Boolean(kind);
    $('compatibility-platform').disabled = Boolean(kind);
    $('compatibility-link').readOnly = Boolean(kind);
    $('compatibility-canaries').querySelectorAll('button').forEach((button) => { button.disabled = Boolean(kind); });
    $('cancel-compatibility-test').hidden = kind !== 'compatibility';
    $('test-current-link').textContent = kind === 'compatibility' ? 'Testando a fonte…' : 'Testar meu link →';
    $('download-button-label').textContent = kind === 'download' ? `Preparando seu ${mediaLabel()}…` : `Baixar ${mediaLabel()}`;
    inspectButton.textContent = kind === 'inspect' ? 'Analisando…' : 'Analisar e estimar →';
    form.setAttribute('aria-busy', kind ? 'true' : 'false');
  }

  function startOperation(kind) {
    timedOut = false;
    const controller = new AbortController();
    activeController = controller;
    timeoutId = window.setTimeout(() => {
      if (activeController !== controller || sessionLocked) return;
      timedOut = true;
      controller.abort();
    }, 280000);
    setBusy(kind);
    return controller;
  }

  function ownsOperation(controller) {
    return activeController === controller && !sessionLocked;
  }

  function ensureOperation(controller) {
    if (!ownsOperation(controller) || controller.signal.aborted) {
      throw new DOMException('Operação cancelada.', 'AbortError');
    }
  }

  function endOperation(controller) {
    if (activeController !== controller) return;
    if (timeoutId) window.clearTimeout(timeoutId);
    timeoutId = null;
    activeController = null;
    if (!sessionLocked) setBusy(null);
  }

  async function responseError(response) {
    let message = '';
    let code = '';
    try {
      const payload = await response.json();
      if (typeof payload.error === 'string' && payload.error) message = payload.error;
      else if (typeof payload.detail === 'string') message = payload.detail;
      else if (payload.detail && typeof payload.detail.message === 'string') message = payload.detail.message;
      else if (typeof payload.message === 'string') message = payload.message;
      if (typeof payload.code === 'string') code = payload.code;
      else if (payload.detail && typeof payload.detail.code === 'string') code = payload.detail.code;
    } catch {
      // Some gateways return HTML or plain text rather than the API error shape.
    }
    if (!message) {
      if (response.status === 413) message = 'Este arquivo ultrapassa o tamanho permitido pelo serviço. Tente um conteúdo mais curto.';
      else if (response.status === 429) message = 'Há muitas solicitações agora. Aguarde um pouco e tente novamente.';
      else if ([502, 503, 504].includes(response.status)) message = 'O serviço de conversão não está disponível agora. Tente novamente em alguns instantes.';
      else message = `Não foi possível processar o link (erro ${response.status}). Tente outro link público.`;
    }
    const error = new Error(message);
    error.code = code;
    return error;
  }

  function showOperationError(error) {
    if (error.name === 'AbortError') {
      showStatus(timedOut ? 'error' : 'info', timedOut ? 'O processamento levou mais tempo que o previsto' : 'Operação cancelada', timedOut ? 'O limite de espera foi atingido. Tente um conteúdo mais curto ou outro link.' : 'Você pode ajustar suas escolhas e tentar novamente.');
    } else {
      const message = error instanceof TypeError
        ? 'Não conseguimos conectar ao serviço. Verifique sua conexão e tente novamente.'
        : error.message || 'Não foi possível concluir. Tente novamente.';
      showStatus('error', 'Não foi possível concluir', message);
    }
  }

  function formatDuration(seconds) {
    if (!Number.isFinite(Number(seconds)) || Number(seconds) <= 0) return '';
    const total = Math.floor(Number(seconds));
    const hours = Math.floor(total / 3600);
    const minutes = Math.floor((total % 3600) / 60);
    const remainder = String(total % 60).padStart(2, '0');
    return hours ? `${hours}:${String(minutes).padStart(2, '0')}:${remainder}` : `${minutes}:${remainder}`;
  }

  function renderMetadata(data, url) {
    metadata = data;
    metadataURL = url;
    $('media-title').textContent = typeof data.title === 'string' && data.title ? data.title : 'Conteúdo da fonte selecionada';
    $('media-source').textContent = typeof data.source === 'string' && data.source ? data.source : detectSource(url);
    const duration = formatDuration(data.duration);
    $('media-duration').textContent = duration ? `Duração: ${duration}` : 'Duração não informada pela fonte';
    const thumb = $('media-thumbnail');
    thumb.hidden = true;
    thumb.removeAttribute('src');
    if (typeof data.thumbnail === 'string' && isPublicURL(data.thumbnail)) {
      thumb.referrerPolicy = 'no-referrer';
      thumb.loading = 'lazy';
      thumb.onerror = () => { thumb.hidden = true; };
      thumb.src = data.thumbnail;
      thumb.hidden = false;
    }
    $('media-preview').hidden = false;
    if (window.OndaDownloadEstimate) updateDownloadEstimate();
  }

  async function inspect() {
    if (activeController) return;
    const url = getURL();
    if (!url) return;
    const cookies = getCookies();
    if (cookies === null) return;
    const controller = startOperation('inspect');
    showStatus('loading', 'Buscando os detalhes do link', 'Verificando título, duração e disponibilidade na fonte.');
    try {
      const response = await window.OndaAuth.fetch('/api/inspect', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(withCookies({ url, media_type: selectedMediaType() }, cookies)),
        signal: controller.signal,
      });
      ensureOperation(controller);
      if (!response.ok) throw await responseError(response);
      const data = await response.json();
      ensureOperation(controller);
      renderMetadata(data, url);
      showStatus('info', 'Link analisado', `Escolha o formato e clique em “Baixar ${mediaLabel()}” para continuar.`);
    } catch (error) {
      if (ownsOperation(controller)) showOperationError(controller.signal.aborted ? new DOMException('Operação cancelada.', 'AbortError') : error);
    } finally {
      endOperation(controller);
    }
  }

  function formatBytes(value) {
    if (!Number.isFinite(value) || value <= 0) return '0 KB';
    if (value < 1024 * 1024) return `${Math.max(1, Math.round(value / 1024))} KB`;
    return `${(value / (1024 * 1024)).toLocaleString('pt-BR', { minimumFractionDigits: 1, maximumFractionDigits: 1 })} MB`;
  }

  function formatSpeed(value) {
    if (value < 1024) return `${Math.round(value).toLocaleString('pt-BR')} B/s`;
    if (value < 1024 * 1024) return `${(value / 1024).toLocaleString('pt-BR', { maximumFractionDigits: 1 })} KB/s`;
    return `${formatBytes(value)}/s`;
  }

  function filenameFromHeader(header, format) {
    if (header) {
      const utf8 = header.match(/filename\*\s*=\s*UTF-8''([^;]+)/i);
      if (utf8) {
        try { return safeFilename(decodeURIComponent(utf8[1].trim().replace(/^"|"$/g, '')), format); } catch { /* Try the regular filename. */ }
      }
      const regular = header.match(/filename\s*=\s*(?:"([^"]+)"|([^;]+))/i);
      if (regular) return safeFilename((regular[1] || regular[2]).trim(), format);
    }
    return `onda-${selectedMediaType()}.${format}`;
  }

  function safeFilename(value, format) {
    const clean = value.replace(/[\\/\u0000-\u001f\u007f]/g, '-').trim().slice(0, 220);
    return clean && clean !== '.' && clean !== '..' ? clean : `onda-${selectedMediaType()}.${format}`;
  }

  function titleFromHeader(header) {
    if (!header) return '';
    try { return decodeURIComponent(header); } catch { return header; }
  }

  function renderDownloadProgress(event) {
    if (event.type !== 'progress') return;
    const labels = {
      extracting: ['Extraindo os detalhes', 'Identificando as faixas disponíveis na fonte.'],
      downloading: ['Transferindo da fonte', 'Recebendo a mídia para preparar o arquivo.'],
      converting: ['Processando seu arquivo', 'Aplicando o formato e os ajustes selecionados.'],
      delivering: ['Recebendo seu arquivo', 'A conversão terminou. Transferindo para este dispositivo.'],
      ready: ['Arquivo pronto', 'Transferência concluída e arquivo verificado.'],
    };
    const label = labels[event.stage];
    if (!label) return;
    const panel = $('download-progress');
    const changed = panel.dataset.stage !== event.stage;
    panel.hidden = false; panel.dataset.state = 'active'; panel.dataset.stage = event.stage;
    if (changed) {
      statusTitle.textContent = label[0]; statusMessage.textContent = label[1];
      $('download-speed').textContent = '—';
    }
    const order = ['extracting', 'downloading', 'converting', 'ready'];
    const index = event.stage === 'delivering' ? 2 : order.indexOf(event.stage);
    panel.querySelectorAll('[data-download-stage]').forEach((step) => {
      const position = order.indexOf(step.dataset.downloadStage);
      step.dataset.state = event.stage === 'ready' || position < index ? 'complete' : position === index ? 'active' : 'waiting';
      if (step.dataset.state === 'active') step.setAttribute('aria-current', 'step');
      else step.removeAttribute('aria-current');
    });
    const finite = (value) => typeof value === 'number' && Number.isFinite(value) && value >= 0;
    const received = finite(event.downloadedBytes) ? event.downloadedBytes : null;
    const total = finite(event.totalBytes) && event.totalBytes > 0 ? event.totalBytes : null;
    if (received !== null) $('download-transferred').textContent = total ? `${formatBytes(received)} / ${formatBytes(total)}` : `${formatBytes(received)} recebidos`;
    else if (event.stage === 'extracting') $('download-transferred').textContent = 'Aguardando a fonte';
    if (finite(event.speedBytesPerSecond) && event.speedBytesPerSecond > 0) $('download-speed').textContent = formatSpeed(event.speedBytesPerSecond);
    let percent = received !== null && total ? received / total * 100 : null;
    let detail = event.stage === 'delivering' ? 'Onda → dispositivo' : event.stage === 'ready' ? 'Concluído' : event.stage === 'downloading' ? 'Fonte → Onda' : 'Identificando a mídia';
    if (event.stage === 'converting') {
      detail = finite(event.processedSeconds) ? `${formatDuration(event.processedSeconds) || '00:00'} processados` : 'Aplicando seus ajustes';
      if (finite(event.durationSeconds) && event.durationSeconds > 0 && finite(event.processedSeconds)) percent = event.processedSeconds / event.durationSeconds * 100;
      if (finite(event.outputBytes) && event.outputBytes > 0) detail += ` · ${formatBytes(event.outputBytes)}`;
    }
    if (finite(event.attempt) && event.attempt > 1) detail += ` · tentativa ${event.attempt}`;
    $('download-stage-detail').textContent = detail;
    progressTrack.hidden = false;
    if (event.stage === 'ready') percent = 100;
    progressTrack.classList.toggle('indeterminate', percent === null);
    progressFill.style.width = percent === null ? '0%' : `${Math.max(0, Math.min(event.stage === 'ready' ? 100 : 99, percent))}%`;
    if (percent !== null) { progressTrack.setAttribute('role', 'progressbar'); progressTrack.setAttribute('aria-valuemin', '0'); progressTrack.setAttribute('aria-valuemax', '100'); progressTrack.setAttribute('aria-valuenow', String(Math.round(Math.max(0, Math.min(100, percent))))); }
    else { progressTrack.removeAttribute('role'); progressTrack.removeAttribute('aria-valuenow'); }
    window.dispatchEvent(new CustomEvent('onda:download-progress', { detail: { stage: event.stage, downloadedBytes: received, totalBytes: total } }));
  }

  async function receiveDownload(response, controller, contentType, onProgress) {
    ensureOperation(controller);
    const lengthHeader = response.headers.get('Content-Length');
    const encoding = (response.headers.get('Content-Encoding') || 'identity').trim().toLowerCase();
    // Fetch transparently decodes compressed responses. Their Content-Length
    // describes the encoded wire bytes and cannot verify the decoded Blob.
    const total = encoding === 'identity' && /^\d+$/.test(lengthHeader || '') ? Number(lengthHeader) : 0;
    const knownLength = Number.isSafeInteger(total) && total > 0;
    if (knownLength && total > MAX_DOWNLOAD_BYTES) {
      try { await response.body?.cancel(); } catch { /* The rejection is already final. */ }
      throw new Error('O arquivo ultrapassa 100 MB. Escolha uma resolução menor ou ajuste o trecho.');
    }
    let blob;
    if (response.body && typeof response.body.getReader === 'function') {
      const reader = response.body.getReader();
      const chunks = [];
      let received = 0;
      let complete = false;
      const cancelReader = () => {
        try { Promise.resolve(reader.cancel()).catch(() => {}); } catch { /* Already released. */ }
      };
      controller.signal.addEventListener('abort', cancelReader, { once: true });
      try {
        while (true) {
          ensureOperation(controller);
          const { done, value } = await reader.read();
          ensureOperation(controller);
          if (done) break;
          received += value.byteLength;
          if (received > MAX_DOWNLOAD_BYTES) throw new Error('O arquivo ultrapassa 100 MB. Escolha uma resolução menor ou ajuste o trecho.');
          if (knownLength && received > total) throw new Error('A transferência retornou um tamanho inesperado. Tente novamente.');
          chunks.push(value);
          onProgress?.(received, knownLength ? total : 0);
        }
        if (knownLength && received !== total) throw new Error('A transferência ficou incompleta. Tente novamente.');
        blob = new Blob(chunks, { type: contentType });
        complete = true;
      } finally {
        controller.signal.removeEventListener('abort', cancelReader);
        if (!complete) cancelReader();
        reader.releaseLock();
      }
    } else {
      blob = await response.blob();
      ensureOperation(controller);
      if (blob.size > MAX_DOWNLOAD_BYTES) throw new Error('O arquivo ultrapassa 100 MB. Escolha uma resolução menor ou ajuste o trecho.');
      if (knownLength && blob.size !== total) throw new Error('A transferência ficou incompleta. Tente novamente.');
    }
    return blob;
  }

  async function download(event) {
    event.preventDefault();
    if (activeController) return;
    const url = getURL();
    if (!url) return;
    const cookies = getCookies();
    if (cookies === null) return;
    const options = getTools();
    if (options === null) return;
    const type = selectedMediaType();
    const format = selectedFormat();
    const quality = selectedQuality();
    releaseFile();
    const controller = startOperation('download');
    showStatus('loading', `Preparando seu ${mediaLabel(type)}`, 'Verificando a fonte e recuperando falhas temporárias automaticamente. O tempo depende do conteúdo e dos ajustes.');
    progressTrack.hidden = false;
    progressTrack.classList.add('indeterminate');
    progressFill.style.width = '0%';
    renderDownloadProgress({ type: 'progress', stage: 'extracting' });
    try {
      const response = await window.OndaAuth.fetch('/api/download', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', Accept: window.OndaDownloadStream?.CONTENT_TYPE || 'application/octet-stream' },
        body: JSON.stringify(withCookies({ url, media_type: type, format, quality, ...options }, cookies)),
        signal: controller.signal,
      });
      ensureOperation(controller);
      if (!response.ok) throw await responseError(response);
      const contentType = response.headers.get('Content-Type') || 'application/octet-stream';
      if (contentType.includes('application/json')) throw await responseError(response);
      const streamed = contentType.toLowerCase().startsWith('application/x-onda-download');
      const mediaType = contentType.split(';', 1)[0].trim().toLowerCase();
      const allowedType = streamed || (type === 'video'
        ? mediaType.startsWith('video/') || ['application/octet-stream', 'application/x-matroska'].includes(mediaType)
        : mediaType.startsWith('audio/') || ['application/octet-stream', 'application/ogg'].includes(mediaType));
      if (!allowedType) {
        try { await response.body?.cancel(); } catch { /* Preserve the invalid-response error. */ }
        throw new Error(`O serviço retornou uma resposta inesperada em vez do ${mediaLabel(type)}. Tente novamente em alguns instantes.`);
      }
      let lastUpdate = 0;
      let fileDetails = null;
      const result = streamed ? await window.OndaDownloadStream.receive(response, {
        signal: controller.signal, ensureOperation: () => ensureOperation(controller), maxBytes: MAX_DOWNLOAD_BYTES,
        onEvent(event) {
          ensureOperation(controller);
          if (event.type === 'progress') renderDownloadProgress(event);
          if (event.type === 'file') {
            if (!(type === 'video' ? event.mime.startsWith('video/') : event.mime.startsWith('audio/'))) throw new Error('O arquivo recebido não corresponde ao tipo de mídia escolhido.');
            fileDetails = event;
          }
        },
      }) : null;
      if (!streamed) renderDownloadProgress({ type: 'progress', stage: 'delivering' });
      const blob = result?.blob || await receiveDownload(response, controller, contentType, (received, total) => {
        const now = Date.now();
        if (now - lastUpdate > 250 || (total && received >= total)) {
          statusMessage.textContent = total ? `${formatBytes(received)} de ${formatBytes(total)} recebidos.` : `${formatBytes(received)} recebidos. O tamanho total não foi informado.`;
          if (total) {
            progressTrack.classList.remove('indeterminate');
            progressFill.style.width = `${Math.min(100, received / total * 100)}%`;
          }
          lastUpdate = now;
        }
      });
      ensureOperation(controller);
      if (blob.size === 0) throw new Error(`A fonte não retornou um arquivo de ${mediaLabel(type)}. Tente outro link.`);
      if (fileDetails && !(type === 'video' ? fileDetails.mime.startsWith('video/') : fileDetails.mime.startsWith('audio/'))) throw new Error('O arquivo recebido não corresponde ao tipo de mídia escolhido.');
      const filename = fileDetails ? safeFilename(fileDetails.name, format) : filenameFromHeader(response.headers.get('Content-Disposition'), format);
      const title = fileDetails?.title || titleFromHeader(response.headers.get('X-Media-Title') || response.headers.get('X-Audio-Title')) || (metadataURL === url && metadata?.title) || filename.replace(/\.[^.]+$/, '');
      objectURL = URL.createObjectURL(blob);
      saveButton.href = objectURL;
      saveButton.download = filename;
      saveButton.hidden = false;
      const attempts = Number(fileDetails?.recovery?.attempts || response.headers.get('X-Recovery-Attempts') || 1);
      const recovered = (Number.isInteger(attempts) && attempts > 1 && attempts <= 3)
        || fileDetails?.recovery?.resumed === true || fileDetails?.recovery?.conversion_recovered === true || fileDetails?.recovery?.queued === true
        || response.headers.get('X-Recovery-Resumed') === '1' || response.headers.get('X-Recovery-Conversion') === '1'
        || response.headers.get('X-Recovery-Queued') === '1';
      const resolution = Number(fileDetails?.resolution || response.headers.get('X-Media-Resolution'));
      const outputDetail = `${format.toUpperCase()}${type === 'video' && resolution > 0 && resolution <= 2160 ? ` · ${resolution}p` : ''} · ${formatBytes(blob.size)}`;
      if (!streamed) renderDownloadProgress({ type: 'progress', stage: 'ready', downloadedBytes: blob.size, totalBytes: blob.size });
      showStatus('success', `Seu ${mediaLabel(type)} está pronto`, `${recovered ? 'Recuperado automaticamente. ' : ''}${outputDetail}. Clique em “Salvar arquivo” para baixar no seu dispositivo.`);
      $('save-file-detail').textContent = outputDetail;
      progressTrack.hidden = true;
      window.dispatchEvent(new CustomEvent('onda:download-file', { detail: { size: blob.size, resolution: resolution || null, recovery: fileDetails?.recovery || { attempts } } }));
      saveOpen.hidden = false;
      saveDialog.showModal();
      saveButton.focus({ preventScroll: true });
      addHistory({ url, media_type: type, format, quality, options, title: String(title), timestamp: Date.now() });
    } catch (error) {
      if (ownsOperation(controller)) {
        const cancelled = controller.signal.aborted;
        controller.abort();
        releaseFile();
        showOperationError(cancelled ? new DOMException('Operação cancelada.', 'AbortError') : error);
      }
    } finally {
      endOperation(controller);
    }
  }

  function readHistory() {
    try {
      const stored = JSON.parse(window.OndaAccount.storage.getItem(HISTORY_KEY) || '[]');
      if (!Array.isArray(stored)) return [];
      const records = [];
      for (const entry of stored) {
        if (!entry || !isPublicURL(entry.url) || !Number.isFinite(entry.timestamp) || (entry.media_type != null && !['audio', 'video'].includes(entry.media_type))) continue;
        const type = entry.media_type === 'video' ? 'video' : 'audio';
        if (!(type === 'video' ? VIDEO_FORMATS : FORMATS).has(entry.format) || !QUALITIES.has(String(entry.quality))) continue;
        const record = { url: entry.url, media_type: type, format: entry.format, quality: type === 'video' ? 'source' : String(entry.quality), title: typeof entry.title === 'string' ? entry.title.slice(0, 400) : '', timestamp: entry.timestamp };
        if (entry.options && typeof entry.options === 'object') {
          const options = entry.options;
          const validTime = (value) => typeof value === 'number' && Number.isFinite(value) && value >= 0 && value <= Number.MAX_SAFE_INTEGER ? value : null;
          const start = validTime(options.trim_start);
          const end = validTime(options.trim_end);
          const validRange = end === null || end > (start ?? 0);
          const mute = type === 'video' && options.mute === true;
          record.options = { video_resolution: VIDEO_RESOLUTIONS.has(String(options.video_resolution)) ? String(options.video_resolution) : 'source', trim_start: validRange ? start : null, trim_end: validRange ? end : null, strip_metadata: options.strip_metadata !== false, mute, normalize_audio: options.normalize_audio === true && !mute };
        }
        records.push(record);
        if (records.length === 5) break;
      }
      return records;
    } catch {
      return [];
    }
  }

  function addHistory(entry) {
    history = [entry, ...history.filter((item) => item.url !== entry.url || item.format !== entry.format || item.media_type !== entry.media_type)].slice(0, 5);
    try { window.OndaAccount.storage.setItem(HISTORY_KEY, JSON.stringify(history)); } catch { /* History is optional if storage is unavailable. */ }
    renderHistory();
  }

  function renderHistory() {
    $('history-section').hidden = history.length === 0;
    const empty = $('history-empty');
    if (empty) empty.hidden = history.length > 0;
    const count = $('history-count');
    if (count) {
      count.textContent = String(history.length);
      count.setAttribute('aria-label', history.length === 1 ? '1 link recente' : `${history.length} links recentes`);
    }
    $('clear-history').disabled = history.length === 0;
    const list = $('history-list');
    list.replaceChildren();
    for (const entry of history) {
      const li = document.createElement('li');
      const badge = document.createElement('span');
      badge.className = 'history-format';
      badge.textContent = entry.format.toUpperCase();
      const info = document.createElement('div');
      info.className = 'history-info';
      const title = document.createElement('p');
      title.className = 'history-title';
      title.textContent = typeof entry.title === 'string' && entry.title ? entry.title : entry.url;
      title.title = entry.url;
      const meta = document.createElement('p');
      meta.className = 'history-meta';
      const date = new Date(entry.timestamp).toLocaleString('pt-BR', { day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit' });
      const type = entry.media_type === 'video' ? 'video' : 'audio';
      const options = entry.options;
      const qualityLabel = type === 'video' ? options?.video_resolution && options.video_resolution !== 'source' ? `${options.video_resolution}p` : 'Original até 4K'
        : entry.quality === 'source' ? 'Formato sem perda' : `${entry.quality} kbps`;
      const adjustments = [];
      if (options?.trim_start != null || options?.trim_end != null) adjustments.push(`Trecho ${timeForInput(options.trim_start) || 'início'}–${timeForInput(options.trim_end) || 'fim'}`);
      if (options?.mute) adjustments.push('Sem áudio');
      if (options?.normalize_audio) adjustments.push('Volume normalizado');
      meta.textContent = `${type === 'video' ? 'Vídeo' : 'Áudio'} · ${detectSource(entry.url)} · ${qualityLabel}${adjustments.length ? ` · ${adjustments.join(' · ')}` : ''} · ${date}`;
      info.append(title, meta);
      const retry = document.createElement('button');
      retry.type = 'button';
      retry.className = 'history-retry';
      retry.textContent = 'Usar link →';
      retry.setAttribute('aria-label', `Usar novamente o link de ${title.textContent}`);
      retry.addEventListener('click', () => {
        if (activeController) { announce('Aguarde a operação atual ou clique em Cancelar.'); return; }
        input.value = entry.url;
        form.elements.media_type.value = type;
        if (type === 'video') form.elements.video_format.value = entry.format;
        else {
          form.elements.format.value = entry.format;
          form.elements.quality.value = entry.quality === 'source' ? lastQuality : String(entry.quality);
        }
        form.elements.video_resolution.value = options?.video_resolution || 'source';
        $('trim-start').value = timeForInput(options?.trim_start);
        $('trim-end').value = timeForInput(options?.trim_end);
        $('strip-metadata').checked = options?.strip_metadata !== false;
        $('mute-video').checked = options?.mute === true;
        $('normalize-audio').checked = options?.normalize_audio === true;
        updateChoices();
        updateSource();
        window.OndaUI?.activate('download');
        $('baixar').scrollIntoView({ behavior: window.matchMedia('(prefers-reduced-motion: reduce)').matches ? 'auto' : 'smooth', block: 'start' });
        input.focus({ preventScroll: true });
        announce(`Link, formato e ajustes restaurados. Clique em Baixar ${mediaLabel(type)} para continuar.`);
      });
      li.append(badge, info, retry);
      list.append(li);
    }
  }

  async function checkHealth() {
    const controller = new AbortController();
    const timer = window.setTimeout(() => controller.abort(), 12000);
    try {
      const response = await window.OndaAuth.fetch('/api/health', { signal: controller.signal, cache: 'no-store' });
      if (!response.ok) throw new Error('Unavailable');
      const data = await response.json();
      const available = data.ok !== false && data.ready !== false && data.available !== false && data.status !== 'unavailable' && data.status !== 'error' && data.status !== 'degraded';
      $('service-dot').classList.toggle('available', available);
      $('service-dot').classList.toggle('unavailable', !available);
      $('service-label').textContent = available ? 'Serviço disponível' : 'Serviço em preparação';
    } catch {
      $('service-dot').classList.add('unavailable');
      $('service-label').textContent = 'Serviço indisponível agora';
    } finally {
      window.clearTimeout(timer);
    }
  }

  function displayVersion(value) {
    if (value && typeof value === 'object') value = value.version;
    return ['string', 'number'].includes(typeof value) && String(value).trim() ? String(value).split('\n')[0].slice(0, 110) : 'Não informado';
  }

  function renderCompatibility(data) {
    const versions = data.versions || {};
    $('version-ytdlp').textContent = displayVersion(versions.ytDlp ?? versions.yt_dlp);
    $('version-ffmpeg').textContent = displayVersion(versions.ffmpeg);
    $('version-deno').textContent = displayVersion(versions.deno);
    const unavailable = ['error', 'unavailable', 'failed'].includes(data.status) || data.ok === false;
    const limited = ['degraded', 'partial', 'limited'].includes(data.status);
    $('compatibility-state').textContent = unavailable ? 'Serviço indisponível' : limited ? 'Disponibilidade parcial' : 'Configuração consultada';
    $('compatibility-dot').classList.toggle('available', !unavailable && !limited);
    $('compatibility-dot').classList.toggle('unavailable', unavailable || limited);
    $('compatibility-message').textContent = typeof data.message === 'string' && data.message
      ? data.message : 'Estas são as ferramentas da versão publicada. Teste uma fonte para verificar o acesso atual.';
    const count = Number(data.sitesCount);
    $('compatibility-count').textContent = Number.isFinite(count) && count >= 0 && data.sitesCount != null
      ? `${count.toLocaleString('pt-BR')} extratores no catálogo · acesso depende de cada link` : 'Catálogo de extratores não informado pelo serviço.';
    if (!maintenanceLoadedAt) {
      $('healing-title').textContent = 'Verificar a manutenção automática';
      $('healing-message').textContent = 'Abra esta aba para consultar o agendamento e a execução do AutoCura no GitHub.';
    }
    const limits = data.limits || {};
    const duration = Number(limits.maxDuration);
    const losslessDuration = Number(limits.maxLosslessDuration);
    const output = Number(limits.maxOutputMB);
    if (limits.maxDuration === null && limits.maxVideoDuration === null && output > 0) {
      const source = Number(limits.maxSourceMB);
      const deadline = Number(limits.operationTimeoutSeconds);
      $('compatibility-limits').textContent = `Sem limite por duração · ${source > 0 ? `${source.toLocaleString('pt-BR')} MB de fonte / ` : ''}${output.toLocaleString('pt-BR')} MB de saída${deadline > 0 ? ` · processamento até ${deadline} s` : ''}`;
    } else if (duration > 0 && losslessDuration > 0 && output > 0) {
      const videoDuration = Number(limits.maxVideoDuration);
      $('compatibility-limits').textContent = `Áudio: ${Math.floor(duration / 60)} min com perda / ${Math.floor(losslessDuration / 60)} min sem perda${videoDuration > 0 ? ` · vídeo: ${Math.floor(videoDuration / 60)} min` : ''} · ${output.toLocaleString('pt-BR')} MB de saída`;
    }
    const canaries = Array.isArray(data.canaries) ? data.canaries.slice(0, 8) : [];
    $('compatibility-canaries').replaceChildren();
    for (const canary of canaries) {
      if (!canary || typeof canary.url !== 'string') continue;
      let canaryURL;
      try { canaryURL = new URL(canary.url, window.location.origin).href; } catch { continue; }
      if (!isPublicURL(canaryURL)) continue;
      const button = document.createElement('button');
      button.type = 'button';
      button.className = 'canary-button';
      button.textContent = `${typeof canary.name === 'string' && canary.name ? canary.name : detectSource(canaryURL)} →`;
      button.disabled = Boolean(activeController);
      button.addEventListener('click', () => testCompatibility(canaryURL));
      $('compatibility-canaries').append(button);
    }
  }

  async function loadCompatibility() {
    compatibilityStatusController?.abort();
    const controller = new AbortController();
    compatibilityStatusController = controller;
    const timer = window.setTimeout(() => controller.abort(), 20000);
    $('refresh-compatibility').disabled = true;
    try {
      const response = await window.OndaAuth.fetch('/api/compatibility', { signal: controller.signal, cache: 'no-store' });
      if (!response.ok) throw await responseError(response);
      const data = await response.json();
      if (sessionLocked || compatibilityStatusController !== controller || controller.signal.aborted) return;
      renderCompatibility(data);
    } catch (error) {
      if (sessionLocked || compatibilityStatusController !== controller) return;
      $('compatibility-state').textContent = 'Não foi possível consultar o serviço';
      $('compatibility-dot').classList.remove('available');
      $('compatibility-dot').classList.add('unavailable');
      $('compatibility-message').textContent = error.name === 'AbortError' ? 'A consulta excedeu o tempo de espera. Use “Atualizar status” para tentar novamente.'
        : error instanceof TypeError ? 'Verifique sua conexão e atualize o status para tentar novamente.' : error.message;
      // Maintenance has its own request. A failure of the extractor catalog
      // must not replace a maintenance result that has already been verified.
      if (!maintenanceLoadedAt) {
        $('healing-title').textContent = 'Configuração não confirmada';
        $('healing-message').textContent = 'Não foi possível confirmar a configuração de manutenção nesta consulta.';
      }
    } finally {
      window.clearTimeout(timer);
      if (compatibilityStatusController === controller) {
        compatibilityStatusController = null;
        $('refresh-compatibility').disabled = false;
      }
    }
  }

  function showCompatibilityResult(kind, title, message) {
    $('compatibility-result').hidden = false;
    $('compatibility-result').className = `compatibility-result ${kind}`;
    $('compatibility-result-title').textContent = title;
    $('compatibility-result-message').textContent = message;
  }

  async function testCompatibility(explicitURL, provider = '') {
    if (activeController) {
      announce('Aguarde a operação atual ou cancele antes de iniciar outro teste.');
      return;
    }
    const url = explicitURL || input.value.trim();
    if (!isPublicURL(url)) {
      showCompatibilityResult('error', 'Escolha um link para testar', 'Cole um endereço completo no formulário acima ou escolha uma amostra disponível.');
      window.OndaUI?.activate('download');
      showStatus('info', 'Escolha um link para testar', 'Cole um endereço completo aqui. Depois, abra Compatibilidade para testar o acesso.');
      input.focus();
      return;
    }
    const cookies = getCookies();
    if (cookies === null) {
      showCompatibilityResult('error', 'Confira o acesso à fonte', 'Revise o formato dos cookies e os campos de sessão antes de testar.');
      return;
    }
    const controller = startOperation('compatibility');
    showCompatibilityResult('loading', 'Testando o acesso à fonte', `${detectSource(url)} · aguarde a resposta do serviço.`);
    try {
      const response = await window.OndaAuth.fetch('/api/compatibility/test', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(withCookies({ url, media_type: selectedMediaType(), ...(provider ? { provider } : {}) }, cookies)), signal: controller.signal,
      });
      ensureOperation(controller);
      if (!response.ok) throw await responseError(response);
      const data = await response.json();
      ensureOperation(controller);
      const success = data.ok === true;
      const elapsed = Number(data.elapsedMs);
      const elapsedText = Number.isFinite(elapsed) && elapsed >= 0 ? ` · ${(elapsed / 1000).toLocaleString('pt-BR', { maximumFractionDigits: 1 })} s` : '';
      const details = data.details;
      const detailText = typeof details === 'string' ? details : details && typeof details.message === 'string' ? details.message
        : details && typeof details.title === 'string' ? details.title : '';
      const errorText = typeof data.error === 'string' ? data.error : 'A fonte não está acessível nesta tentativa. Cookies não garantem acesso.';
      showCompatibilityResult(success ? 'success' : 'error', success ? 'Metadados acessíveis nesta verificação' : 'A fonte não passou no teste',
        `${success ? `${detailText ? `${detailText} · ` : ''}Teste de acesso concluído. A conversão e o download não foram testados.` : errorText}${elapsedText}`);
    } catch (error) {
      if (!ownsOperation(controller)) return;
      const aborted = controller.signal.aborted || error.name === 'AbortError';
      const message = aborted ? timedOut ? 'O limite de espera foi atingido. Você pode tentar novamente manualmente.' : 'Você pode escolher outro link e iniciar um novo teste.'
        : error instanceof TypeError ? 'Não conseguimos conectar ao serviço. Verifique sua conexão.' : error.message || 'Não foi possível concluir a verificação.';
      showCompatibilityResult(aborted && !timedOut ? 'info' : 'error', aborted && !timedOut ? 'Teste cancelado' : 'Não foi possível concluir o teste', message);
    } finally {
      endOperation(controller);
    }
  }

  form.addEventListener('submit', download);
  form.addEventListener('input', (event) => { if (!event.target.closest('#advanced-options')) saveDraft(); });
  form.addEventListener('change', saveDraft);
  form.addEventListener('change', (event) => {
    if (event.target.matches('input[type="radio"]')) updateChoices();
    else if (event.target.matches('input[type="checkbox"]')) updateTools();
  });
  $('trim-start').addEventListener('input', updateTools);
  $('trim-end').addEventListener('input', updateTools);
  $('reset-tools').addEventListener('click', () => {
    if (activeController) return;
    resetTools();
    saveDraft();
    announce('Ajustes restaurados. Remover metadados continua ativado por padrão.');
  });
  input.addEventListener('input', () => { invalidateCookieValidation(); updateSource(); });
  inspectButton.addEventListener('click', inspect);
  $('cookies-input').addEventListener('input', () => { sessionImportGeneration += 1; cookieMetadata = null; invalidateCookieValidation(); void persistCookies(); });
  $('clear-cookies').addEventListener('click', async () => {
    clearSessionFields();
    const removed = await persistCookies();
    announce(removed ? 'Cookies removidos deste dispositivo. Os campos da sessão também foram limpos.' : 'Campos limpos. Não foi possível confirmar a remoção dos cookies salvos; tente novamente.');
  });
  async function importCookieFile(file) {
    if (!file || activeController) return;
    const generation = ++sessionImportGeneration;
    invalidateCookieValidation();
    if (file.size > MAX_COOKIE_BYTES) {
      $('cookies-file').value = '';
      $('cookies-filename').textContent = 'Nenhum arquivo selecionado';
      updateCookieStatus('Arquivo não importado: o limite é 64 KB.', true);
      return;
    }
    try {
      let text = (await file.text()).trim();
      if (generation !== sessionImportGeneration || activeController) return;
      if (text.startsWith('{') || text.startsWith('[')) {
        const data = JSON.parse(text);
        const records = Array.isArray(data) ? data : data?.cookies;
        if (!Array.isArray(records)) throw new Error('O JSON deve conter uma lista de cookies.');
        // Browser storage exports can also contain localStorage. Keep only
        // the deliberately imported cookie records, without browser storage.
        text = JSON.stringify(records);
      }
      const content = describeCookies(text);
      if (content.error) throw new Error(content.error);
      $('cookies-input').value = text;
      cookieMetadata = null;
      $('cookies-filename').textContent = file.name;
      updateCookieStatus();
      void persistCookies();
      announce('Cookies importados. O salvamento neste dispositivo é automático.');
    } catch (error) {
      if (generation !== sessionImportGeneration) return;
      $('cookies-file').value = '';
      $('cookies-filename').textContent = 'Nenhum arquivo selecionado';
      updateCookieStatus(error instanceof SyntaxError ? 'O arquivo JSON não é válido. Exporte novamente os cookies da plataforma.' : 'Não foi possível importar. Use Netscape (TXT) ou JSON, com até 64 KB e 300 cookies.', true);
    }
  }
  $('cookies-file').addEventListener('change', () => importCookieFile($('cookies-file').files?.[0]));
  $('import-cookies').addEventListener('click', () => $('cookies-file').click());
  $('validate-cookies').addEventListener('click', validateCookies);
  $('cookies-dropzone').addEventListener('dragover', (event) => {
    event.preventDefault();
    if (!activeController) $('cookies-dropzone').classList.add('is-dragging');
  });
  $('cookies-dropzone').addEventListener('dragleave', (event) => {
    if (!$('cookies-dropzone').contains(event.relatedTarget)) $('cookies-dropzone').classList.remove('is-dragging');
  });
  $('cookies-dropzone').addEventListener('drop', (event) => {
    event.preventDefault();
    $('cookies-dropzone').classList.remove('is-dragging');
    if (activeController) return;
    const files = event.dataTransfer?.files;
    if (files?.length !== 1) { updateCookieStatus('Importe um arquivo de cookies por vez.', true); return; }
    importCookieFile(files[0]);
  });
  $('toggle-video-password').addEventListener('click', () => {
    const shown = $('video-password').type === 'password';
    $('video-password').type = shown ? 'text' : 'password';
    $('toggle-video-password').setAttribute('aria-pressed', String(shown));
    $('toggle-video-password').setAttribute('aria-label', shown ? 'Ocultar senha do vídeo' : 'Mostrar senha do vídeo');
    $('toggle-video-password').textContent = shown ? 'Ocultar' : 'Mostrar';
  });
  $('use-browser-agent').addEventListener('click', () => {
    $('session-user-agent').value = navigator.userAgent.slice(0, 512);
    $('session-user-agent').removeAttribute('aria-invalid');
    announce('Identificação deste navegador preenchida. Não será salva na conta.');
  });
  for (const field of [$('video-password'), $('session-user-agent')]) field.addEventListener('input', () => field.removeAttribute('aria-invalid'));
  $('refresh-compatibility').addEventListener('click', () => { loadCompatibility(); loadMaintenance(true); });
  $('test-current-link').addEventListener('click', () => testCompatibility());
  $('test-platform-link').addEventListener('click', () => {
    const url = $('compatibility-link').value.trim();
    if (!isPublicURL(url)) { showCompatibilityResult('error', 'Informe um link para esta fonte', 'Cole um endereço público completo no campo de teste.'); $('compatibility-link').focus(); return; }
    testCompatibility(url, $('compatibility-platform').value);
  });
  window.addEventListener('onda:tab', (event) => { if (event.detail?.tab === 'compatibility') { loadProviders(); loadMaintenance(); } });
  $('cancel-compatibility-test').addEventListener('click', () => activeKind === 'compatibility' && activeController?.abort());
  cancelButton.addEventListener('click', () => {
    if (activeController) {
      activeController.abort();
      cancelButton.disabled = true;
      cancelButton.textContent = 'Cancelando…';
      window.setTimeout(() => { cancelButton.disabled = false; cancelButton.textContent = 'Cancelar'; }, 0);
    }
  });
  $('paste-button').addEventListener('click', async () => {
    if (sessionLocked || activeController) return;
    const generation = ++clipboardGeneration;
    const originalValue = input.value;
    if (!navigator.clipboard?.readText) {
      input.focus();
      announce('Use Ctrl + V ou pressione o campo para colar seu link.');
      showStatus('info', 'Cole o link no campo acima', 'Seu navegador não permite colar automaticamente. Use Ctrl + V ou pressione o campo para colar.');
      return;
    }
    try {
      const text = await navigator.clipboard.readText();
      // Permission prompts can outlive this action. Preserve edits made while
      // waiting and never write clipboard content back after sign-out.
      if (sessionLocked || activeController || generation !== clipboardGeneration || input.value !== originalValue) return;
      input.value = text.trim();
      updateSource();
      input.focus();
      announce(text.trim() ? 'Link colado. Escolha áudio ou vídeo, o formato e os ajustes.' : 'Sua área de transferência está vazia.');
    } catch {
      if (sessionLocked || activeController || generation !== clipboardGeneration || input.value !== originalValue) return;
      input.focus();
      showStatus('info', 'Cole o link no campo acima', 'O acesso à área de transferência não foi permitido. Use Ctrl + V ou pressione o campo para colar.');
    }
  });
  $('example-button').addEventListener('click', () => {
    if (activeController) return;
    input.value = selectedMediaType() === 'video' ? VIDEO_EXAMPLE_URL : EXAMPLE_URL;
    updateSource();
    input.focus();
    announce(selectedMediaType() === 'video' ? 'Exemplo de vídeo curto preenchido. Escolha o formato e os ajustes.' : 'Exemplo de áudio curto preenchido. Escolha o formato e os ajustes.');
  });
  $('clear-history').addEventListener('click', () => {
    history = [];
    try { window.OndaAccount.storage.removeItem(HISTORY_KEY); } catch { /* Storage may be unavailable. */ }
    renderHistory();
    announce('Histórico da conta removido.');
  });
  saveButton.addEventListener('click', () => {
    announce('Download enviado ao navegador. Verifique sua pasta de downloads.');
    // The browser owns file saving and does not expose a completion event.
    // Keep the Blob URL alive until the download has been handed off.
    setTimeout(() => { if (saveDialog.open) saveDialog.close(); saveButton.hidden = true; saveOpen.hidden = true; }, 0);
    const savedURL = objectURL;
    setTimeout(() => { if (objectURL === savedURL) releaseFile(); }, 60000);
  });
  saveOpen.addEventListener('click', () => { if (objectURL) { saveButton.hidden = false; saveDialog.showModal(); saveButton.focus({ preventScroll: true }); } });
  window.addEventListener('onda:account-state', () => {
    history = readHistory();
    renderHistory();
    if (!activeController && !form.contains(document.activeElement)) { restoreDraft(); updateChoices(); updateSource(); }
  });
  window.addEventListener('onda:auth', (event) => {
    if (!event.detail?.signedIn) { lockSourceSession(); releaseFile(); activeController?.abort(); compatibilityStatusController?.abort(); providersController?.abort(); maintenanceController?.abort(); window.OndaDownloadEstimate?.render(null, null); $('download-progress').hidden = true; }
  });
  window.addEventListener('pagehide', () => {
    lockSourceSession();
    releaseFile();
    activeController?.abort();
    compatibilityStatusController?.abort();
    providersController?.abort();
    maintenanceController?.abort();
  });
  restoreDraft();
  const queryMedia = new URL(window.location.href).searchParams.get('media');
  if (['audio', 'video'].includes(queryMedia)) form.elements.media_type.value = queryMedia;
  window.OndaMedia = {
    setType(type) {
      if (activeController || !['audio', 'video'].includes(type)) return false;
      form.elements.media_type.value = type;
      updateChoices();
      saveDraft();
      return true;
    },
    get type() { return selectedMediaType(); },
  };
  updateChoices();
  updateSource();
  renderHistory();
  void restoreSavedCookies();
  checkHealth();
  loadCompatibility();
  if (!$('panel-compatibility').hidden) { loadProviders(); loadMaintenance(); }
})();
