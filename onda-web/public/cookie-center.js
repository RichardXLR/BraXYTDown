/* Value-free cookie summaries. Validation metadata is stored only in the encrypted device vault. */
(() => {
  'use strict';

  const MAX_BYTES = 64 * 1024;
  const MAX_COOKIES = 300;
  const RENEW_WINDOW = 48 * 60 * 60 * 1000;
  const encoder = new TextEncoder();
  const DOMAIN = /^(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z0-9-]{2,63}$/;
  const groups = [
    ['YouTube', 'youtube.com', ['youtube.com', 'youtube-nocookie.com', 'youtu.be', 'google.com', 'googlevideo.com', 'ytimg.com']],
    ['TikTok', 'tiktok.com', ['tiktok.com']],
    ['Instagram', 'instagram.com', ['instagram.com']],
    ['Facebook', 'facebook.com', ['facebook.com', 'fb.watch', 'fb.com']],
    ['X / Twitter', 'x.com', ['x.com', 'twitter.com', 't.co']],
    ['Vimeo', 'vimeo.com', ['vimeo.com']],
    ['SoundCloud', 'soundcloud.com', ['soundcloud.com']],
    ['Twitch', 'twitch.tv', ['twitch.tv']],
    ['Dailymotion', 'dailymotion.com', ['dailymotion.com', 'dai.ly']],
    ['Reddit', 'reddit.com', ['reddit.com']],
    ['Pinterest', 'pinterest.com', ['pinterest.com']],
    ['Bilibili', 'bilibili.com', ['bilibili.com']],
  ];
  const failureCodes = new Set(['invalid_cookies', 'cookie_domain', 'cookies_expired', 'cookie_https_required', 'cookies_not_supported', 'timeout', 'validation_failed']);
  let timer = null;
  let lastArguments = null;

  function normalized(cookies) {
    if (typeof cookies !== 'string' || encoder.encode(cookies).byteLength > MAX_BYTES) throw new Error('Sessão fora do tamanho permitido.');
    const text = cookies.trim();
    if (text.startsWith('{')) {
      try {
        const records = JSON.parse(text)?.cookies;
        if (Array.isArray(records)) return JSON.stringify(records);
      } catch {}
    }
    return text;
  }

  function safeDomain(value) {
    if (typeof value !== 'string') return null;
    const domain = value.toLowerCase().replace(/^\./, '').replace(/\.$/, '');
    return domain.length <= 253 && DOMAIN.test(domain) ? domain : null;
  }

  function belongs(domain, root) { return domain === root || domain.endsWith(`.${root}`); }
  function groupFor(domain) { return groups.find(([, , domains]) => domains.some(root => belongs(domain, root))); }

  function scopeFor(url) {
    try {
      const parsed = new URL(url);
      if (parsed.protocol !== 'https:' || parsed.username || parsed.password) return null;
      const host = safeDomain(parsed.hostname);
      if (!host) return null;
      // Google export domains may accompany YouTube cookies, but a Google URL
      // itself is not represented as a YouTube validation.
      const group = groupFor(host);
      if (group?.[0] === 'YouTube' && !['youtube.com', 'youtube-nocookie.com', 'youtu.be'].some(root => belongs(host, root))) return host;
      return group?.[1] || host;
    } catch { return null; }
  }

  function timestamp(value) { return Number.isInteger(value) && value > 0 && value <= 4102444800000; }
  function count(value, minimum = 0) { return Number.isInteger(value) && value >= minimum && value <= MAX_COOKIES; }
  function expiry(value) { return value === null || (Number.isInteger(value) && value >= 0 && value <= 999999999999); }

  function cleanValidation(value) {
    if (!value || !timestamp(value.checkedAt) || typeof value.scope !== 'string' || safeDomain(value.scope) !== value.scope) return null;
    if (value.status === 'invalid' && failureCodes.has(value.code)) {
      return { checkedAt: value.checkedAt, scope: value.scope, status: 'invalid', code: value.code };
    }
    if (value.status !== 'valid' || !count(value.validCookies, 1) || !count(value.expiredCookies)
      || !count(value.ignoredCookies) || !count(value.sessionCookies) || value.sessionCookies > value.validCookies
      || !expiry(value.earliestExpiry)) return null;
    return { checkedAt: value.checkedAt, scope: value.scope, status: 'valid', validCookies: value.validCookies,
      expiredCookies: value.expiredCookies, ignoredCookies: value.ignoredCookies,
      sessionCookies: value.sessionCookies, earliestExpiry: value.earliestExpiry };
  }

  async function fingerprint(cookies) {
    const digest = await crypto.subtle.digest('SHA-256', encoder.encode(normalized(cookies)));
    return [...new Uint8Array(digest)].map(value => value.toString(16).padStart(2, '0')).join('');
  }

  async function createMetadata(cookies, previous = null, now = Date.now()) {
    if (!timestamp(now)) throw new Error('Data inválida para a sessão.');
    const contentHash = await fingerprint(cookies);
    const unchanged = previous?.version === 1 && previous.contentHash === contentHash;
    return { version: 1, contentHash, savedAt: unchanged && timestamp(previous.savedAt) ? previous.savedAt : now,
      validation: unchanged ? cleanValidation(previous.validation) : null };
  }

  async function createValidation(cookies, url, data, previous = null, now = Date.now()) {
    const metadata = await createMetadata(cookies, previous, now);
    const validation = cleanValidation({ checkedAt: now, scope: scopeFor(url), status: 'valid',
      validCookies: data?.validCookies, expiredCookies: data?.expiredCookies, ignoredCookies: data?.ignoredCookies,
      sessionCookies: data?.sessionCookies, earliestExpiry: data?.earliestExpiry });
    if (!validation) throw new Error('O serviço não confirmou os dados da sessão.');
    return { ...metadata, validation };
  }

  async function createValidationFailure(cookies, url, code, previous = null, now = Date.now()) {
    const metadata = await createMetadata(cookies, previous, now);
    const scope = scopeFor(url);
    if (!scope) return metadata;
    return { ...metadata, validation: { checkedAt: now, scope, status: 'invalid',
      code: failureCodes.has(code) ? code : 'validation_failed' } };
  }

  function readEntries(cookies) {
    const text = normalized(cookies);
    let records;
    if (text.startsWith('[') || text.startsWith('{')) {
      const data = JSON.parse(text);
      records = Array.isArray(data) ? data : data?.cookies;
      if (!Array.isArray(records) || !records.length || records.length > MAX_COOKIES) throw new Error('Formato inválido.');
      return records.map(item => {
        if (!item || typeof item !== 'object' || Array.isArray(item)) throw new Error('Formato inválido.');
        const domain = safeDomain(item.domain);
        if (!domain) throw new Error('Domínio inválido.');
        const supplied = ['expirationDate', 'expires'].filter(key => Object.hasOwn(item, key)).map(key => item[key]);
        if (supplied.some(value => value !== null && (typeof value !== 'number' || !Number.isFinite(value)
          || value < -1 || value > 999999999999 || (value < 0 && value !== -1)))) throw new Error('Data inválida.');
        const expirations = supplied.map(value => value === null || value === -1 ? null : Math.trunc(value));
        if (new Set(expirations).size > 1) throw new Error('Datas conflitantes.');
        let expires = expirations[0] ?? null;
        if (item.session === true && expires === 0 && supplied.every(value => value === 0)) expires = null;
        if (item.session === false && expires === null) throw new Error('Data ausente.');
        return { domain, expires, partitioned: Boolean(item.partitioned || item.partitionKey || item.firstPartyDomain) };
      });
    }
    const lines = text.split(/\r?\n/);
    if (!['# Netscape HTTP Cookie File', '# HTTP Cookie File'].includes(lines[0]?.trim())) throw new Error('Formato inválido.');
    records = lines.slice(1).filter(line => line.trim() && (!line.startsWith('#') || line.startsWith('#HttpOnly_')));
    if (!records.length || records.length > MAX_COOKIES) throw new Error('Formato inválido.');
    return records.map(line => {
      const fields = line.replace(/^#HttpOnly_/, '').split('\t');
      const domain = safeDomain(fields[0]);
      if (fields.length !== 7 || !domain || !/^[0-9]{1,12}$/.test(fields[4])) throw new Error('Formato inválido.');
      return { domain, expires: Number(fields[4]) || null, partitioned: false };
    });
  }

  function dateLabel(milliseconds) {
    if (!Number.isFinite(milliseconds) || milliseconds > 8640000000000000) return 'data não disponível';
    return new Intl.DateTimeFormat('pt-BR', { dateStyle: 'short', timeStyle: 'short' }).format(new Date(milliseconds));
  }

  function summarize({ cookies = '', url = '', metadata = null } = {}, now = Date.now()) {
    if (typeof cookies !== 'string' || !cookies.trim()) return { empty: true };
    let entries;
    try { entries = readEntries(cookies); }
    catch { return { empty: false, state: 'invalid', platforms: 'Revise o formato do arquivo', expiration: 'Não foi possível ler as datas.',
      lastValidation: 'Valide após corrigir o formato.', renewal: 'Use uma exportação Netscape ou JSON válida, com até 300 cookies e 64 KB.' }; }
    const platforms = [...new Set(entries.map(entry => groupFor(entry.domain)?.[0] || entry.domain))].sort().join(' · ');
    const scope = scopeFor(url);
    const relevant = entries.filter(entry => !entry.partitioned && (!scope
      || (groupFor(scope)?.[1] === scope ? groupFor(entry.domain)?.[1] === scope : belongs(entry.domain, scope) || belongs(scope, entry.domain))));
    const sessionCookies = relevant.filter(entry => entry.expires === null).length;
    const expired = relevant.filter(entry => entry.expires !== null && entry.expires * 1000 <= now).length;
    const expirations = relevant.filter(entry => entry.expires !== null && entry.expires * 1000 > now).map(entry => entry.expires * 1000);
    const earliest = expirations.length ? Math.min(...expirations) : null;
    const due = earliest !== null && earliest - now <= RENEW_WINDOW;
    let expiration = !relevant.length ? 'Nenhum cookie corresponde à plataforma deste link.'
      : earliest !== null ? `Próxima expiração: ${dateLabel(earliest)}${sessionCookies ? ` · ${sessionCookies} sem data de expiração` : ''}`
      : sessionCookies ? 'Cookies de sessão: sem data de expiração informada.' : 'Os cookies desta plataforma estão expirados.';
    if (expired && (earliest !== null || sessionCookies)) expiration = `${expired} expirado(s) · ${expiration}`;
    const validation = cleanValidation(metadata?.validation);
    const sameScope = !scope || validation?.scope === scope;
    const label = validation && (groupFor(validation.scope)?.[0] || validation.scope);
    const lastValidation = validation
      ? `${validation.status === 'valid' ? 'Formato e escopo verificados' : 'Validação precisa de atenção'} em ${dateLabel(validation.checkedAt)} · ${label}${sameScope ? '' : ' (outra plataforma)'}`
      : 'Ainda não validado. Use “Validar para este link”.';
    const renewal = expired ? 'Há cookies expirados. Exporte uma sessão atualizada; os demais serão avaliados ao baixar.'
      : due ? 'A próxima expiração ocorre em até 48 horas. Renove a exportação para evitar interrupções.'
      : !relevant.length ? 'Os cookies continuam salvos; valide uma sessão da plataforma do link.'
      : sessionCookies ? 'Sem prazo garantido: a plataforma pode encerrar a sessão. Renove se o acesso deixar de funcionar.'
      : 'A expiração é uma referência do arquivo; a plataforma pode encerrar a sessão antes dessa data.';
    return { empty: false, state: expired ? 'expired' : due ? 'renew-soon' : 'ready', platforms, expiration, lastValidation, renewal,
      earliestExpiry: earliest === null ? null : earliest / 1000, expiredCookies: expired, sessionCookies };
  }

  function paint(summary) {
    const center = document.getElementById('cookie-center');
    if (!center) return;
    center.hidden = summary.empty;
    center.dataset.state = summary.state || 'empty';
    for (const [id, value] of [['cookie-platforms', summary.platforms], ['cookie-expiration', summary.expiration],
      ['cookie-last-validation', summary.lastValidation], ['cookie-renewal-note', summary.renewal]]) {
      const field = document.getElementById(id);
      if (field) field.textContent = value || '';
    }
  }

  function stopTimer() { if (timer !== null) clearInterval(timer); timer = null; lastArguments = null; }
  function render(arguments_ = {}) {
    const summary = summarize(arguments_);
    paint(summary);
    if (summary.empty) { stopTimer(); return summary; }
    lastArguments = arguments_;
    if (timer === null) timer = setInterval(() => { if (lastArguments) paint(summarize(lastArguments)); }, 60000);
    return summary;
  }

  window.addEventListener('pagehide', stopTimer);
  window.OndaCookieCenter = Object.freeze({ render, summarize, createMetadata, createValidation, createValidationFailure, scopeFor, stopTimer });
})();
