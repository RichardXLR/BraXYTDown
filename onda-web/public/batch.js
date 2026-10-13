(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  const center = $('batch-center');
  if (!center) return;
  const owner = window.Clerk.user.id;
  const active = new Set(['queued', 'running', 'packing']);
  const retryIdentities = new Map();
  const labels = { queued: 'Na fila', running: 'Preparando arquivos', packing: 'Montando e verificando ZIP', ready: 'ZIP disponível', failed: 'Pacote não concluído', cancelled: 'Cancelado' };
  const stages = { extracting: 'Extraindo informações', downloading: 'Transferindo mídia', converting: 'Convertendo e ajustando', delivering: 'Guardando no servidor', ready: 'Arquivo pronto', recoverable_failure: 'Tentando recuperar' };
  let jobs = [], polling = null, generation = 0, request = null, identity = null, fingerprint = null, submitting = false;
  const signedIn = () => window.OndaAuth.signedIn && window.Clerk.user?.id === owner;
  const bytes = value => window.OndaDownloadEstimate.formatBytes(value);
  const message = text => { $('batch-message').textContent = text; };
  const node = (tag, text, className) => { const element = document.createElement(tag); if (text) element.textContent = text; if (className) element.className = className; return element; };
  async function api(url, options = {}) {
    if (!signedIn()) throw new DOMException('Sessão encerrada.', 'AbortError');
    const response = await window.OndaAuth.fetch(url, options);
    if (!response.ok) {
      const data = await response.json().catch(() => ({}));
      const error = new Error(typeof data.error === 'string' ? data.error : 'A fila não respondeu. Tente atualizar em instantes.');
      error.status = response.status; error.code = data.code; throw error;
    }
    return response;
  }
  function action(text, run, primary = false) {
    const button = node('button', text, primary ? 'secondary-button' : 'text-button'); button.type = 'button';
    button.addEventListener('click', async () => { button.disabled = true; try { await run(); } catch (error) { if (error.name !== 'AbortError') message(error.message); } finally { button.disabled = false; } });
    return button;
  }
  function render(automatic = false) {
    const list = $('batch-list'), focused = document.activeElement;
    // Avoid replacing a keyboard user's focused action during automatic polling.
    if (automatic && focused && list.contains(focused)) return;
    list.replaceChildren();
    for (const job of jobs) {
      if (job.status === 'cancelled') continue;
      const card = node('article', '', 'batch-job');
      card.append(node('h3', `${labels[job.status] || 'Preparando'} · ${job.items.length} arquivos`));
      const done = job.items.filter(item => ['ready', 'failed'].includes(item.status)).length;
      card.append(node('p', `${done} de ${job.items.length} processados${job.file ? ` · ${bytes(job.file.size)}` : ''}`));
      const items = node('ol');
      for (const item of job.items) {
        const li = node('li', item.title || item.source);
        let detail = item.status === 'failed' ? 'Não concluído: revise o acesso ou o formato.' : item.status === 'ready' ? `${item.format.toUpperCase()} · ${bytes(item.size)}` : item.status === 'running' ? (stages[item.progress?.stage] || 'Preparando') : 'Aguardando';
        const p = item.progress;
        if (item.status === 'running' && Number.isFinite(p?.downloadedBytes)) detail += ` · ${bytes(p.downloadedBytes)}`;
        if (item.status === 'running' && p?.speedBytesPerSecond > 0) detail += ` · ${bytes(p.speedBytesPerSecond)}/s`;
        li.append(node('span', detail));
        if (item.status === 'running' && p?.totalBytes > 0 && Number.isFinite(p.downloadedBytes)) {
          const progress = node('progress'); progress.max = p.totalBytes; progress.value = Math.min(p.totalBytes, p.downloadedBytes); progress.setAttribute('aria-label', 'Transferência da mídia'); li.append(progress);
        }
        items.append(li);
      }
      card.append(items);
      const actions = node('div', '', 'batch-job-actions');
      if (job.status === 'ready') actions.append(action('Receber e salvar ZIP', () => receive(job), true));
      if (job.items.some(item => item.status === 'failed')) actions.append(action('Revisar cookies e acesso', () => { $('advanced-options').open = true; $('cookies-input').focus(); }));
      if (['ready', 'failed'].includes(job.status) && job.items.some(item => item.status === 'failed')) actions.append(action('Repetir só os pendentes', async () => {
        const options = window.OndaBatchBridge.snapshot(); if (!options) return;
        const prior = retryIdentities.get(job.id), signature = JSON.stringify(options);
        const key = prior?.signature === signature ? prior.key : crypto.randomUUID();
        retryIdentities.set(job.id, { key, signature });
        const response = await api(`/api/batches/${job.id}/retry`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ ...options, idempotencyKey: key }) });
        const data = await response.json(); if (!signedIn()) return;
        retryIdentities.delete(job.id); jobs = [data.batch, ...jobs.filter(row => row.id !== data.batch.id)]; render(); schedule(); message('Somente os arquivos pendentes foram enviados com os ajustes atuais.');
      }));
      actions.append(action(active.has(job.status) ? 'Cancelar pacote' : 'Remover pacote', async () => { await api(`/api/batches/${job.id}`, { method: 'DELETE' }); jobs = jobs.filter(row => row.id !== job.id); render(); message('Pacote removido da sua fila.'); }));
      card.append(actions, node('p', `Disponível até ${new Date(job.expires * 1000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}.`, 'batch-note'));
      list.append(card);
    }
    if (!list.childElementCount) list.append(node('p', 'Sua fila está vazia.', 'batch-note'));
  }
  function schedule() {
    clearTimeout(polling);
    if (signedIn() && jobs.some(job => active.has(job.status))) polling = setTimeout(poll, document.hidden ? 60000 : 20000);
  }
  async function poll() {
    if (!signedIn()) return;
    try {
      for (const job of [...jobs].filter(row => active.has(row.status))) {
        const response = await api(`/api/batches/${job.id}`, { cache: 'no-store' });
        const data = await response.json();
        if (!signedIn()) return;
        jobs = jobs.map(row => row.id === job.id ? data.batch : row);
      }
      render(true);
    } catch (error) { if (error.name !== 'AbortError') message('A conexão com a fila foi interrompida. O servidor continua; use “Atualizar fila”.'); }
    finally { schedule(); }
  }
  async function refresh() {
    const token = ++generation;
    request?.abort(); request = new AbortController();
    const response = await api('/api/batches', { signal: request.signal, cache: 'no-store' });
    const data = await response.json();
    if (token !== generation || !signedIn()) return;
    jobs = data.batches; render(); schedule();
  }
  async function enqueue() {
    if (submitting) return;
    const urls = [...new Set($('batch-links').value.split(/\r?\n/).map(value => value.trim()).filter(Boolean))];
    if (!urls.length || urls.length > 12) { message('Informe de 1 a 12 links, um por linha.'); $('batch-links').focus(); return; }
    const options = window.OndaBatchBridge.snapshot(); if (!options) return;
    const payload = { urls, ...options };
    const signature = JSON.stringify(payload);
    if (signature !== fingerprint) { identity = crypto.randomUUID(); fingerprint = signature; }
    submitting = true; $('batch-add').disabled = true;
    try {
      message('Enviando para a fila do servidor…');
      const response = await api('/api/batches', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ ...payload, idempotencyKey: identity }) });
      const data = await response.json();
      if (!signedIn()) return;
      jobs = [data.batch, ...jobs.filter(row => row.id !== data.batch.id)];
      identity = null; fingerprint = null; $('batch-links').value = ''; render(); schedule();
      message('Pacote aceito. Você pode fechar a página e voltar à mesma conta para salvar o ZIP.');
    } catch (error) { if (error.name !== 'AbortError') message(error.message); }
    finally { submitting = false; $('batch-add').disabled = false; }
  }
  async function receive(job) {
    const operation = window.OndaBatchBridge.begin();
    const { controller, ensure, heartbeat } = operation;
    let sink = null, completed = false, offset = 0;
    try {
      const capacity = await window.OndaDownloadStorage.capacity(); ensure();
      const check = window.OndaDownloadStorage.assess({ state: 'ready', minBytes: job.file.size, maxBytes: job.file.size }, capacity);
      if (check.state === 'insufficient') throw new Error('O armazenamento disponível neste navegador não comporta o ZIP. Libere espaço e tente novamente.');
      sink = await window.OndaDownloadStorage.create({ size: job.file.size, mime: 'application/zip' }, { userId: owner, ensureOperation: ensure });
      while (offset < job.file.size) {
        const end = Math.min(job.file.size - 1, offset + 2 * 1024 * 1024 - 1);
        let attempts = 0;
        while (offset <= end) {
          ensure();
          try {
            const start = offset;
            const response = await api(`/api/batches/${job.id}/file`, { headers: { Range: `bytes=${start}-${end}` }, signal: controller.signal, cache: 'no-store' });
            if (response.status !== 206 || response.headers.get('Content-Range') !== `bytes ${start}-${end}/${job.file.size}`) throw new Error('O servidor retornou um trecho inválido. Atualize a fila.');
            const reader = response.body.getReader();
            try {
              while (true) {
                const { done, value } = await reader.read(); ensure();
                if (done) break;
                if (offset + value.byteLength > end + 1) throw new Error('O arquivo recebido excedeu o tamanho previsto.');
                await sink.write(value); offset += value.byteLength; heartbeat();
                message(`Recebendo ZIP: ${bytes(offset)} de ${bytes(job.file.size)}.`);
              }
              if (offset !== end + 1) throw new TypeError('Transferência interrompida.');
            } finally { await reader.cancel().catch(() => {}); reader.releaseLock(); }
          } catch (error) {
            if (error.name === 'AbortError' || !(error instanceof TypeError || [429, 502, 503, 504].includes(error.status)) || ++attempts >= 3) throw error;
            await new Promise(resolve => setTimeout(resolve, attempts * 1000)); ensure();
          }
        }
      }
      ensure(); const blob = await sink.commit(); ensure();
      operation.complete({ blob, cleanup: () => sink.abort() }, job.file.name); completed = true; message('ZIP recebido. Salve o arquivo para concluir.');
    } catch (error) { if (error.name !== 'AbortError') message(error.message); else message('Recebimento cancelado. O ZIP continua na sua fila.'); }
    finally { if (!completed) await sink?.abort().catch(() => {}); operation.end(); }
  }
  $('batch-add').addEventListener('click', enqueue);
  $('batch-refresh').addEventListener('click', () => refresh().catch(error => message(error.message)));
  document.addEventListener('visibilitychange', () => { if (!document.hidden) { clearTimeout(polling); void poll(); } });
  window.addEventListener('onda:auth-state', event => { if (!event.detail?.signedIn) { generation++; request?.abort(); clearTimeout(polling); jobs = []; identity = fingerprint = null; retryIdentities.clear(); $('batch-links').value = ''; $('batch-list').replaceChildren(); } });
  window.addEventListener('pagehide', () => { generation++; request?.abort(); clearTimeout(polling); });
  (async () => {
    try { const response = await api('/api/batches/capabilities'); const config = await response.json(); if (config.configured && signedIn()) { center.hidden = false; await refresh(); } }
    catch { /* The single downloader remains available if the queue is offline. */ }
  })();
})();
