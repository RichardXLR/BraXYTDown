(() => {
  'use strict';
  const cookies = new Set(['invalid_cookies', 'cookies_expired', 'cookie_domain', 'cookie_https_required', 'source_session_invalid', 'source_login_required', 'cookies_required', 'login_required']);
  const access = new Set(['source_private_access', 'source_blocked', 'source_unavailable', 'unsupported_source', 'removed', 'private', 'drm', 'drm_protected', 'unsafe_url', 'unsafe_redirect']);
  const format = new Set(['format_unavailable', 'no_audio', 'no_video', 'unavailable_resolution', 'conversion_failed', 'invalid_format', 'processing_failed']);
  const capacity = new Set(['timeout', 'deadline_exceeded', 'source_too_large', 'output_too_large', 'workspace_exhausted', 'memory_limit', 'storage_quota']);
  const transient = new Set(['busy', 'rate_limited', 'upstream_rate_limited', 'upstream_unavailable', 'upstream_timeout', 'storage_busy', 'worker_unavailable', 'network_error', 'download_incomplete', 'incomplete']);
  function plan(code) {
    if (cookies.has(code)) return ['cookies', 'test'];
    if (code === 'video_password_required' || code === 'video_password_invalid') return ['password', 'test'];
    if (format.has(code)) return ['format', 'test'];
    if (capacity.has(code)) return ['quality', 'trim', 'test'];
    if (access.has(code)) return ['link', 'test'];
    if (code === 'cancelled') return ['quality'];
    if (transient.has(code)) return ['retry', 'test'];
    if (code === 'authentication_required' || code === 'auth_forbidden') return ['account'];
    return ['test', 'format'];
  }
  const labels = { cookies: 'Renovar cookies', password: 'Informar senha do vídeo', test: 'Testar acesso', format: 'Ajustar formato', quality: 'Revisar qualidade', trim: 'Selecionar um trecho', retry: 'Tentar novamente', link: 'Revisar link', account: 'Entrar novamente' };
  let timer = null, generation = 0;
  function clear() {
    generation++; clearTimeout(timer); timer = null;
    const root = document.getElementById('error-actions');
    if (root) { root.replaceChildren(); root.hidden = true; }
  }
  function render(error, { run, ensure = () => true, allowRetry = true } = {}) {
    clear();
    const root = document.getElementById('error-actions');
    if (!root) return;
    const version = generation;
    const actions = plan(error?.code).filter(action => allowRetry || action !== 'retry');
    const cooldown = Math.max(1, Math.min(3600, Math.ceil(Number(error?.retryAfter) || 3)));
    for (const action of actions) {
      const button = document.createElement('button');
      button.type = 'button'; button.className = 'secondary-button'; button.dataset.errorAction = action; button.textContent = labels[action];
      if (action === 'retry') {
        button.disabled = true; button.textContent = `Tentar novamente em ${cooldown} s`;
        timer = setTimeout(() => { if (version === generation) { button.disabled = false; button.textContent = labels.retry; } }, cooldown * 1000);
      }
      button.addEventListener('click', () => { if (version !== generation || !ensure()) return; run?.(action); });
      root.append(button);
    }
    root.hidden = !actions.length;
  }
  addEventListener('onda:auth', event => { if (!event.detail?.signedIn) clear(); });
  window.OndaErrorActions = Object.freeze({ plan, render, clear });
})();
