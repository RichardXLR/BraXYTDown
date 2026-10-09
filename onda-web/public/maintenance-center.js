'use strict';

// Read-only diagnostics. All remote labels use text nodes; run links are
// reconstructed from numeric IDs on this repository, never remote URLs.
(() => {
  const repository = 'RichardXLR/BraXYTDown';
  const statusLabels = { passed: 'Aprovado', blocked: 'Bloqueado pela fonte', failed: 'Falhou', unknown: 'Sem confirmação' };
  const runLabels = { success: 'Concluída', failure: 'Falhou', cancelled: 'Cancelada', timed_out: 'Tempo excedido', action_required: 'Precisa de atenção', neutral: 'Sem aprovação', skipped: 'Não executada', stale: 'Expirada' };
  function node(tag, className, value) {
    const element = document.createElement(tag);
    if (className) element.className = className;
    if (value !== undefined && value !== null) element.textContent = String(value);
    return element;
  }
  function date(value) {
    if (typeof value !== 'string' || value.length > 40) return 'Horário não confirmado';
    const parsed = new Date(value);
    return Number.isFinite(parsed.getTime()) ? parsed.toLocaleString('pt-BR', { dateStyle: 'short', timeStyle: 'short' }) : 'Horário não confirmado';
  }
  function text(value, fallback = '', maximum = 160) { return typeof value === 'string' ? value.slice(0, maximum) : fallback; }
  function list(value, limit = 50) { return Array.isArray(value) ? value.slice(0, limit).filter((item) => item && typeof item === 'object' && !Array.isArray(item)) : []; }
  function count(value) { return Number.isSafeInteger(value) && value >= 0 ? Math.min(value, 10000) : 0; }
  function heading(container, title, description) {
    const header = node('div', 'maintenance-section-heading');
    header.append(node('h4', '', title));
    if (description) header.append(node('p', '', description));
    container.append(header);
  }
  function render(data = {}) {
    const root = document.getElementById('autocura-transparency');
    if (!root) return;
    if (!data || typeof data !== 'object' || Array.isArray(data)) data = {};
    const fragment = document.createDocumentFragment();
    const run = data.run && typeof data.run === 'object' ? data.run : null;
    const runCard = node('div', 'maintenance-run-card');
    const runText = node('div', 'maintenance-run-copy');
    runText.append(node('span', 'maintenance-eyebrow', 'ÚLTIMA EXECUÇÃO'));
    const conclusion = run && typeof run.conclusion === 'string' && Object.hasOwn(runLabels, run.conclusion) ? runLabels[run.conclusion] : 'Sem confirmação';
    const label = !run ? 'Aguardando evidência do GitHub' : run.status === 'completed' ? conclusion : run.status === 'unknown' ? 'Sem confirmação' : 'Em andamento';
    const title = node('div', 'maintenance-run-title');
    title.append(node('strong', '', label));
    const pill = node('span', 'maintenance-pill', data.automated === true && data.state === 'active' ? 'Automático ativo' : run && data.state !== 'verification_unavailable' ? 'Verificado no GitHub' : 'Aguardando confirmação');
    pill.dataset.tone = data.state === 'active' && data.automated === true ? 'passed' : data.state === 'attention_required' ? 'failed' : 'unknown';
    title.append(pill);
    runText.append(title, node('p', 'maintenance-date', run ? date(run.updated_at || run.started_at) : 'Nenhuma execução confirmada nesta consulta'));
    runCard.append(runText);
    if (run && Number.isSafeInteger(run.id) && run.id > 0) {
      const link = node('a', 'maintenance-run-link', 'Ver execução ↗');
      link.href = `https://github.com/${repository}/actions/runs/${run.id}`;
      link.target = '_blank'; link.rel = 'noopener noreferrer';
      link.setAttribute('aria-label', `Ver execução ${run.id} do AutoCura no GitHub`);
      runCard.append(link);
    }
    fragment.append(runCard);
    const reported = data.evidence && typeof data.evidence === 'object' ? data.evidence : {};
    if (reported.report_run_id && !reported.matches_latest_run) {
      fragment.append(node('p', 'maintenance-context', reported.verified_successful_report
        ? 'Os componentes e testes abaixo pertencem ao último relatório bem-sucedido confirmado. A execução mais recente ainda não confirmou uma atualização.'
        : 'O relatório disponível pertence a outra execução. Ele não confirma a versão publicada nem o sucesso da execução mais recente.'));
    } else if (data.diagnostics_available === false || !reported.report_run_id) {
      fragment.append(node('p', 'maintenance-context', 'Os detalhes do relatório não ficaram disponíveis nesta consulta. Nenhum teste ausente é considerado aprovado.'));
    }
    const checks = list(data.checks);
    const stats = node('div', 'maintenance-summary');
    stats.setAttribute('aria-label', 'Resumo dos testes do relatório');
    for (const [status, title] of [['passed', 'Aprovados'], ['blocked', 'Bloqueados'], ['failed', 'Falhos'], ['unknown', 'Sem confirmação']]) {
      const card = node('div', 'maintenance-stat'); card.dataset.status = status;
      card.append(node('strong', '', checks.filter((item) => item.status === status).length), node('span', '', title));
      stats.append(card);
    }
    fragment.append(stats);
    const versionsSection = node('section', 'maintenance-section');
    heading(versionsSection, 'Componentes publicados', 'Versões confirmadas pelo relatório e pela execução correspondente.');
    const versions = list(data.current_versions, 3);
    if (!versions.length) versionsSection.append(node('p', 'maintenance-empty', 'As versões publicadas ainda não puderam ser confirmadas.'));
    else {
      const rows = node('dl', 'maintenance-versions');
      for (const component of versions) {
        const row = node('div', 'maintenance-version');
        row.append(node('dt', '', text(component.label, 'Componente', 64)), node('dd', '', text(component.version, 'Sem confirmação', 64)));
        if (!component.version && component.package_version) row.append(node('p', '', `Distribuição ${text(component.package_version, '', 64)}; binário não confirmado.`));
        else row.append(node('p', '', component.source === 'runtime_check' ? 'Verificado em execução' : 'Dependência aprovada'));
        rows.append(row);
      }
      versionsSection.append(rows);
    }
    fragment.append(versionsSection);
    const changesSection = node('section', 'maintenance-section');
    heading(changesSection, 'Alterações da última publicação');
    const changes = list(data.component_changes, 40);
    if (!changes.length) changesSection.append(node('p', 'maintenance-empty', versions.length
      ? 'Nenhuma mudança de versão confirmada entre as duas últimas publicações registradas.'
      : 'A comparação de versões aguarda evidência de publicações aprovadas.'));
    else {
      const rows = node('ul', 'maintenance-changes');
      for (const change of changes) {
        const row = node('li', '');
        row.append(node('strong', '', text(change.label, 'Componente', 64)));
        const values = node('span', 'maintenance-change-values');
        values.append(node('span', '', text(change.before, 'Não confirmado', 64)), node('span', 'maintenance-change-arrow', '→'), node('strong', '', text(change.after, 'Não confirmado', 64)));
        row.append(values); rows.append(row);
      }
      changesSection.append(rows, node('p', 'maintenance-date', `Publicação registrada: ${date(changes[0].at)}`));
    }
    fragment.append(changesSection);
    if (checks.length) {
      const details = node('details', 'maintenance-checks');
      const summary = node('summary', '', `Ver ${checks.length} testes do relatório`);
      details.append(summary);
      const rows = node('ul', 'maintenance-check-list');
      for (const check of checks) {
        const row = node('li', '');
        const status = Object.hasOwn(statusLabels, check.status) ? check.status : 'unknown';
        const title = node('div', 'maintenance-check-title');
        const badge = node('span', 'maintenance-pill', statusLabels[status]); badge.dataset.tone = status;
        title.append(node('strong', '', text(check.label, 'Teste', 100)), badge);
        row.append(title);
        if (check.message) row.append(node('p', '', text(check.message)));
        rows.append(row);
      }
      details.append(rows);
      fragment.append(details);
      if (checks.some((item) => item.status === 'blocked')) fragment.append(node('p', 'maintenance-context', 'Um bloqueio da plataforma permanece visível. Ele não conta como teste aprovado nem garante que o download dessa fonte funcione.'));
    }
    const issues = list(data.quarantine, 10);
    const pending = data.pending && typeof data.pending === 'object' ? data.pending : null;
    const failure = data.failure && typeof data.failure === 'object' ? data.failure : null;
    const totalQuarantine = count(data.quarantined_versions);
    const issueSection = node('section', 'maintenance-section maintenance-issues');
    heading(issueSection, 'Pendências e quarentena', 'Versões reprovadas ficam isoladas para preservar a última versão aprovada.');
    if (pending) {
      const item = node('div', 'maintenance-issue'); item.dataset.kind = 'pending';
      item.append(node('strong', '', pending.operation === 'manual_rollback' ? 'Recuperação aguardando confirmação' : 'Publicação aguardando confirmação'), node('p', '', 'O registro de atualização ainda está aberto. A próxima execução deve verificar a recuperação antes de publicar outra versão.'), node('span', 'maintenance-date', date(pending.created_at)));
      issueSection.append(item);
    }
    if (failure) {
      const item = node('div', 'maintenance-issue');
      item.append(node('strong', '', 'Falha registrada no workflow'), node('p', '', text(failure.message, 'A execução requer revisão no GitHub.')));
      issueSection.append(item);
    }
    if (totalQuarantine) {
      const details = node('details', 'maintenance-quarantine');
      details.append(node('summary', '', `${totalQuarantine} ${totalQuarantine === 1 ? 'versão em quarentena' : 'versões em quarentena'}`));
      for (const issue of issues) {
        const item = node('div', 'maintenance-issue');
        item.append(node('strong', '', text(issue.message, 'A versão foi isolada após falhar na verificação.')), node('span', 'maintenance-date', date(issue.created_at)));
        const selected = list(issue.versions, 40).filter((version) => ['yt-dlp', 'deno', 'imageio-ffmpeg'].includes(version.component));
        if (selected.length) item.append(node('p', 'maintenance-quarantine-versions', selected.map((version) => `${version.component}: ${text(version.version, '', 64)}`).join(' · ')));
        details.append(item);
      }
      if (totalQuarantine > issues.length) details.append(node('p', 'maintenance-context', 'Os registros mais recentes aparecem aqui. O histórico completo está no repositório.'));
      issueSection.append(details);
    }
    if (!pending && !failure && !totalQuarantine) issueSection.append(node('p', 'maintenance-empty', reported.report_run_id && data.diagnostics_available !== false
      ? 'Nenhuma pendência registrada no relatório consultado.' : 'As pendências ainda não puderam ser verificadas.'));
    fragment.append(issueSection);
    const footer = node('p', 'maintenance-context maintenance-evidence-date');
    footer.textContent = `Consulta ao GitHub: ${date(data.checked_at)}. O status pode permanecer em cache por até 5 minutos.`;
    fragment.append(footer);
    root.replaceChildren(fragment);
  }
  window.OndaMaintenanceCenter = Object.freeze({ render });
})();
