(() => {
  'use strict';
  const root = document.getElementById('download-history-dashboard');
  if (!root) return;
  let controller = null, generation = 0, loadedAt = 0, records = [], unavailable = [];
  const number = value => typeof value === 'number' && Number.isFinite(value) && value >= 0 ? value : 0;
  const nf = new Intl.NumberFormat('pt-BR', { maximumFractionDigits: 1 });
  function el(tag, cls, text) { const node = document.createElement(tag); if (cls) node.className = cls; if (text !== undefined) node.textContent = String(text); return node; }
  const bytes = value => { const n = number(value); return n >= 1024 ** 3 ? `${nf.format(n / 1024 ** 3)} GiB` : n >= 1024 ** 2 ? `${nf.format(n / 1024 ** 2)} MiB` : `${nf.format(n / 1024)} KiB`; };
  const speed = row => number(row.elapsedSeconds) > 0 ? number(row.transferredBytes) / row.elapsedSeconds / 1024 ** 2 : 0;
  const status = { ready: 'Concluído', failed: 'Falhou', cancelled: 'Cancelado', recoverable_failure: 'Nova tentativa' };
  function chart(rows, key, title, unit) {
    const card = el('article', 'performance-chart studio-card'); card.append(el('h2', '', title));
    const values = rows.slice(0, 24).reverse().map(row => key === 'speed' ? speed(row) : number(row[key]));
    const max = Math.max(0, ...values) || 1; const ns = 'http://www.w3.org/2000/svg';
    const svg = document.createElementNS(ns, 'svg'); svg.setAttribute('viewBox', '0 0 600 150'); svg.setAttribute('role', 'img');
    svg.setAttribute('aria-label', values.length ? `${title}. ${values.map(v => nf.format(v)).join(', ')} ${unit}. Da mais antiga para a mais recente.` : `${title}. Ainda sem medições.`);
    const width = 570 / Math.max(1, values.length);
    values.forEach((value, i) => { const bar = document.createElementNS(ns, 'rect'); const h = value / max * 124; Object.entries({ x: 15 + i * width, y: 135 - h, width: Math.max(2, width - 5), height: h, rx: 3 }).forEach(([k, v]) => bar.setAttribute(k, String(v))); svg.append(bar); });
    card.append(svg, el('p', 'performance-chart-caption', `Escala: ${nf.format(max)} ${unit}`)); return card;
  }
  function render(data = records) {
    const expanded = Boolean(root.querySelector('.performance-details')?.open);
    const trendsExpanded = Boolean(root.querySelector('.performance-trends')?.open);
    const fragment = document.createDocumentFragment();
    if (unavailable.length) fragment.append(el('p', 'performance-warning', `Não foi possível consultar: ${unavailable.join(' e ')}. As demais medições continuam abaixo.`));
    if (!data.length) { const empty = el('div', 'performance-empty studio-card'); empty.append(el('h2', '', 'Nenhuma execução ainda'), el('p', '', 'Faça um download para acompanhar suas medições.')); const link = el('a', 'primary-link', 'Baixar um arquivo →'); link.href = '/?tab=download'; empty.append(link); fragment.append(empty); root.replaceChildren(fragment); return; }
    const stats = el('div', 'performance-stats');
    const done = data.filter(row => row.status === 'ready'); const failed = data.filter(row => ['failed', 'recoverable_failure'].includes(row.status));
    for (const [label, value] of [['Execuções', data.length], ['Concluídas', done.length], ['Falhas / tentativas', failed.length], ['Volume recebido', bytes(data.reduce((s, row) => s + number(row.transferredBytes), 0))]]) { const card = el('article', 'performance-stat studio-card'); card.append(el('span', '', label), el('strong', '', value)); stats.append(card); }
    fragment.append(stats);
    const charts = el('div', 'performance-charts'); charts.append(chart(data, 'speed', 'Vazão média', 'MiB/s'), chart(data, 'elapsedSeconds', 'Tempo de preparo', 's'));
    const card = el('div', 'performance-table-wrap studio-card'); const table = el('table', 'performance-table');
    const caption = el('caption', 'sr-only', 'Medições das suas execuções mais recentes'); const head = el('thead'); const heading = el('tr');
    const columns = ['Quando', 'Formato', 'Resultado', 'Tempo', 'Vazão média', 'Arquivo'];
    columns.forEach(text => { const cell = el('th', '', text); cell.scope = 'col'; heading.append(cell); }); head.append(heading);
    const body = el('tbody');
    for (const row of data) { const line = el('tr'); const date = new Date(number(row.at) * 1000); const label = status[row.status] || 'Desconhecido';
      [date.toLocaleString('pt-BR'), String(row.format || '').toUpperCase(), label, `${nf.format(number(row.elapsedSeconds))} s`, `${nf.format(speed(row))} MiB/s`, bytes(row.outputBytes)].forEach((value, i) => { const cell = el('td', i === 2 ? `performance-result result-${row.status}` : '', value); cell.dataset.label = columns[i];
        const measured = window.OndaStageTimes?.validate(row.stageSeconds);
        if (i === 3 && measured) {
          const details = el('details', 'history-stage-times'); details.append(el('summary', '', 'Tempos por etapa'));
          const times = el('dl', 'stage-time-list');
          for (const [key, label] of Object.entries(window.OndaStageTimes.labels)) {
            const item = el('div'); item.append(el('dt', '', key === 'delivering' ? 'Envio pelo servidor' : label), el('dd', '', Object.hasOwn(measured, key) ? window.OndaStageTimes.format(measured[key]) : 'Não medido')); times.append(item);
          }
          const largest = Object.entries(measured).sort((a,b) => b[1] - a[1])[0];
          details.append(times, el('p', 'stage-time-note', `Maior tempo observado: ${window.OndaStageTimes.labels[largest[0]]}. Inclui novas tentativas; envio pelo servidor difere da recepção no dispositivo.`));
          cell.append(details);
        }
        line.append(cell); }); body.append(line);
    }
    table.append(caption, head, body); card.append(table); fragment.append(card); root.replaceChildren(fragment);
    const trends = el('details', 'performance-trends interface-disclosure'); trends.open = trendsExpanded;
    const trendsSummary = el('summary', '', 'Gráficos de desempenho'); const trendsMarker = el('span', '', '+'); trendsMarker.setAttribute('aria-hidden', 'true'); trendsSummary.append(trendsMarker);
    const trendsContent = el('div', 'disclosure-content'); trendsContent.append(charts); trends.append(trendsSummary, trendsContent); root.append(trends);
    const details = el('details', 'performance-details interface-disclosure'); details.open = expanded;
    const summary = el('summary', '', 'Medições técnicas'); const marker = el('span', '', '+'); marker.setAttribute('aria-hidden', 'true'); summary.append(marker); details.append(summary);
    const content = el('div', 'disclosure-content');
    content.append(el('p', 'performance-note', 'Vazão = bytes recebidos ÷ tempo total, incluindo processamento. CPU e pico de RAM são observações do processo do servidor e seus subprocessos, compartilhadas entre tarefas simultâneas. O pico de RAM é cumulativo desde a inicialização.'));
    const resourceScroll = el('div', 'performance-resource-scroll'); resourceScroll.tabIndex = 0; resourceScroll.setAttribute('aria-label', 'Tabela de medições técnicas');
    const resources = el('table', 'performance-resource-table');
    const resourceHead = el('thead'); const rowHead = el('tr'); ['Formato / caminho', 'Recebido', 'CPU observada', 'Pico RAM observado', 'Código'].forEach(value => { const cell = el('th', '', value); cell.scope = 'col'; rowHead.append(cell); }); resourceHead.append(rowHead); const resourceBody = el('tbody');
    for (const row of data) { const line = el('tr'); [`${String(row.format || '').toUpperCase()} · ${row.mode === 'worker' ? 'Worker' : 'Vercel'}`, bytes(row.transferredBytes), `${nf.format(number(row.processCpuSeconds))} s`, bytes(row.processPeakRssBytes), row.code ? String(row.code).slice(0, 64) : '—'].forEach(value => line.append(el('td', '', value))); resourceBody.append(line); }
    resources.append(resourceHead, resourceBody); resourceScroll.append(resources); content.append(resourceScroll); details.append(content); root.append(details);
  }
  async function load(force = false) {
    if (!window.OndaAuth?.signedIn || controller || (!force && Date.now() - loadedAt < 30000)) return;
    const scope = ++generation; controller = new AbortController(); const current = controller;
    const button = document.getElementById('refresh-download-history'); if (button) button.disabled = true;
    if (!records.length) root.replaceChildren(el('p', '', 'Consultando suas medições…'));
    const timer = setTimeout(() => current.abort(), 20000);
    try {
      const paths = ['/api/downloads/history', '/api/jobs/history'];
      const results = await Promise.allSettled(paths.map(async path => { const response = await OndaAuth.fetch(path, { signal: current.signal, cache: 'no-store' }); if (path.includes('/jobs/') && response.status === 503) { const error = await response.json(); if (error.code === 'worker_not_configured') return { records: [] }; } if (!response.ok) throw new Error('Unavailable'); return response.json(); }));
      if (scope !== generation || current.signal.aborted || !OndaAuth.signedIn) return;
      const map = new Map(); unavailable = [];
      results.forEach((result, i) => { if (result.status === 'rejected') unavailable.push(i ? 'worker' : 'Vercel'); else for (const row of (Array.isArray(result.value.records) ? result.value.records.slice(0, 120) : [])) if (row && typeof row.id === 'string' && number(row.at) > Date.now() / 1000 - 30 * 86400 && status[row.status]) map.set(`${row.mode}:${row.id}`, row); });
      records = [...map.values()].sort((a, b) => b.at - a.at); loadedAt = Date.now(); render();
    } catch { if (scope === generation && OndaAuth.signedIn) { unavailable = ['histórico']; render(); } }
    finally { clearTimeout(timer); if (controller === current) controller = null; if (button) button.disabled = false; }
  }
  document.getElementById('refresh-download-history')?.addEventListener('click', () => load(true));
  addEventListener('onda:tab', event => { if (event.detail?.tab === 'performance') load(); });
  addEventListener('onda:auth', event => { if (!event.detail?.signedIn) { generation++; controller?.abort(); records = []; unavailable = []; loadedAt = 0; root.replaceChildren(); } });
  if (document.getElementById('tab-performance')?.getAttribute('aria-selected') === 'true') load();
  window.OndaDownloadHistory = Object.freeze({ render });
})();
