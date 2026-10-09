"""Read-only, cached GitHub evidence for scheduled AutoCura maintenance.

A workflow file or a bundled snapshot alone never proves an active updater.
Only a successful real run and its persisted release report confirm activation.
No request performs updates or receives administrator credentials.
"""
from __future__ import annotations
import asyncio
from contextvars import ContextVar
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
import re
import threading
import time
import urllib.error
import urllib.request

REPOSITORY = 'RichardXLR/BraXYTDown'
WORKFLOW = 'onda-autocura.yml'
CACHE_SECONDS = 300
REQUEST_BUDGET_SECONDS = 8
_DEADLINE = ContextVar('maintenance_deadline', default=None)
_LOCK = threading.Lock()
_CACHE = None
_CACHE_TIME = 0


def read_json(url):
    request = urllib.request.Request(url, headers={
        'User-Agent': 'Onda-Maintenance/3.0', 'Accept': 'application/vnd.github+json'})
    class SameHostRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, request, fp, code, message, headers, target):
            from urllib.parse import urlsplit
            original, destination = urlsplit(request.full_url), urlsplit(target)
            if destination.scheme != 'https' or destination.hostname != original.hostname:
                raise ValueError('unexpected_github_redirect')
            return super().redirect_request(request, fp, code, message, headers, target)
    deadline = _DEADLINE.get() or time.monotonic() + REQUEST_BUDGET_SECONDS
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError('github_budget_exhausted')
    try:
        response = urllib.request.build_opener(SameHostRedirect()).open(request, timeout=min(3, remaining))
    except urllib.error.HTTPError as error:
        error.close()
        raise
    with response:
        # read1 returns available chunks rather than waiting for the entire
        # body. A slow upstream cannot extend the total evidence budget.
        chunks, size = [], 0
        read = getattr(response, 'read1', response.read)
        socket = getattr(getattr(getattr(response, 'fp', None), 'raw', None), '_sock', None)
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError('github_budget_exhausted')
            if socket is not None:
                socket.settimeout(min(3, remaining))
            chunk = read(min(16384, 256 * 1024 + 1 - size))
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
            if size > 256 * 1024:
                raise ValueError('github_response_too_large')
        value = json.loads(b''.join(chunks))
        if not isinstance(value, dict):
            raise ValueError('invalid_github_report')
        return value


def next_check():
    now = datetime.now(timezone.utc)
    following = now.replace(hour=5, minute=23, second=0, microsecond=0)
    if following <= now:
        following += timedelta(days=1)
    return following.isoformat()


_COMPONENTS = {'yt-dlp': 'yt-dlp', 'deno': 'Deno', 'ffmpeg': 'FFmpeg',
               'imageio-ffmpeg': 'Distribuição FFmpeg'}
_CHECK_LABELS = {'runtime': 'Funcionamento do serviço', 'runtime_authentication': 'Proteção das contas',
                 'download_progress': 'Progresso e integridade do download',
                 'packaged_versions': 'Versões instaladas', 'me_at_zoo_metadata': 'YouTube · metadados 1',
                 'big_buck_bunny_metadata': 'YouTube · metadados 2',
                 'authorized_youtube_audio': 'YouTube · áudio autorizado'}
_CHECK_LABELS.update({f'owned_tone_{fmt}': f'Conversão de áudio · {fmt.upper()}'
                      for fmt in ('mp3', 'm4a', 'wav', 'flac', 'ogg', 'opus', 'aac', 'aiff')})
_CHECK_LABELS.update({f'owned_clip_{fmt}_edited': f'Conversão de vídeo · {fmt.upper()}'
                      for fmt in ('mp4', 'webm', 'mkv', 'mov')})
_CHECK_LABELS['owned_clip_mp4_muted'] = 'Vídeo sem áudio · MP4'
_MESSAGES = {
    'platform_blocked': 'A plataforma bloqueou o acesso nesta verificação.',
    'network_unavailable': 'A conexão com a fonte não ficou disponível.',
    'upstream_timeout': 'A fonte não respondeu a tempo.', 'timeout': 'A verificação excedeu o tempo disponível.',
    'upstream_error': 'A fonte retornou uma falha temporária.',
    'busy': 'O serviço estava ocupado durante o teste.', 'unavailable': 'O serviço não estava disponível.',
    'unconfirmed_interrupted_promotion': 'Uma publicação interrompida não confirmou todos os testes.',
    'candidate_compatibility_failed': 'A versão candidata falhou nos testes de compatibilidade.',
    'post_promotion_compatibility_failed': 'A verificação após a publicação falhou; a versão anterior foi preservada.',
    'candidate_application_tests_failed': 'Os testes do aplicativo reprovaram a versão candidata.',
    'post_promotion_check_failed': 'A verificação após a publicação não foi aprovada.',
    'post_promotion_failed': 'A publicação não foi concluída com segurança.',
    'production_changed_during_recovery': 'A produção mudou durante a recuperação; é necessária uma revisão.',
    'durable_checkpoint_required_before_promotion': 'O registro persistente precisa ser confirmado antes de publicar.',
    'production_alias_baselines_not_aligned': 'Os domínios precisam apontar para a mesma versão antes da atualização.',
    'production_alias_configuration_changed': 'A configuração dos domínios mudou durante a recuperação.',
    'wheel_integrity_failed': 'A integridade de um componente não foi confirmada.',
    'verified_dependency_install_failed': 'Um componente verificado não pôde ser instalado.',
    'toolchain_functional_failed': 'Os componentes de mídia falharam no teste funcional.',
    'dependency_download_failed': 'O download de um componente não foi concluído.',
    'packaged_runtime_version_mismatch': 'As versões instaladas não correspondem às versões aprovadas.',
    'invalid_extraction_result': 'A extração não retornou metadados válidos.',
}


def _date(value):
    if not isinstance(value, str) or not 10 <= len(value) <= 40:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        return value if parsed.tzinfo is not None else None
    except (ValueError, OverflowError):
        return None


def _version(value):
    return value if isinstance(value, str) and re.fullmatch(r'v?[0-9][A-Za-z0-9.+_-]{0,62}', value) else None


def _versions(value):
    if not isinstance(value, dict):
        return {}
    return {name: version for name, raw in list(value.items())[:80]
            if isinstance(name, str) and re.fullmatch(r'[a-z0-9][a-z0-9-]{0,63}', name)
            and (version := _version(raw)) is not None}


def _reason(value):
    code = value if isinstance(value, str) and value in _MESSAGES else 'maintenance_check_failed'
    return {'code': code, 'message': _MESSAGES.get(code, 'A verificação requer revisão no workflow do GitHub.')}


def _run(value):
    identifier = value.get('id')
    if isinstance(identifier, bool) or not isinstance(identifier, (int, str)) or not re.fullmatch(r'[1-9][0-9]{0,19}', str(identifier)):
        identifier = None
    else:
        identifier = int(identifier)
    status = value.get('status') if value.get('status') in ('queued', 'in_progress', 'completed', 'waiting', 'requested', 'pending') else 'unknown'
    conclusion = value.get('conclusion') if value.get('conclusion') in ('success', 'failure', 'cancelled', 'timed_out', 'action_required', 'neutral', 'skipped', 'stale') else None
    return {'id': identifier, 'status': status, 'conclusion': conclusion,
            'started_at': _date(value.get('run_started_at') or value.get('created_at')),
            'updated_at': _date(value.get('updated_at')),
            'url': f'https://github.com/{REPOSITORY}/actions/runs/{identifier}' if identifier else None}


def _details(result, report, journal, successful_runs):
    """Project only bounded, public diagnostic fields, never raw errors/URLs."""
    execution = report.get('execution')
    execution = execution if isinstance(execution, dict) else {}
    report_id = _run({'id': execution.get('run_id')})['id'] if execution.get('repository') == REPOSITORY else None
    result['evidence'] = {'source': 'github', 'report_run_id': report_id,
                          'matches_latest_run': report_id is not None and report_id == (result.get('run') or {}).get('id'),
                          'verified_successful_report': report_id is not None and report_id in successful_runs,
                          'reported_at': _date(report.get('snapshot_at'))}
    check_report = report.get('last_check')
    check_report = check_report if isinstance(check_report, dict) else {}
    result['checks'] = []
    for index, item in enumerate((check_report.get('checks') or [])[:50] if isinstance(check_report.get('checks'), list) else []):
        if not isinstance(item, dict):
            continue
        raw_name = item.get('name')
        name = raw_name if isinstance(raw_name, str) and raw_name in _CHECK_LABELS else f'additional_check_{index + 1}'
        status = item.get('status') if item.get('status') in ('passed', 'blocked', 'failed') else 'unknown'
        reason = _reason(item.get('code')) if status != 'passed' else {'code': None, 'message': None}
        result['checks'].append({'name': name, 'label': _CHECK_LABELS.get(name, 'Teste adicional'), 'status': status, **reason})
    result['check_summary'] = {status: sum(item['status'] == status for item in result['checks'])
                               for status in ('passed', 'blocked', 'failed', 'unknown')}
    result['failed_checks'] = [item for item in result['checks'] if item['status'] == 'failed']
    gate = report.get('release_gate') or check_report.get('release_gate')
    if isinstance(gate, dict):
        accepted = gate.get('accepted_existing_blocks')
        result['release_gate'] = {'status': gate.get('status') if gate.get('status') in ('passed', 'blocked', 'failed') else 'unknown',
                                  'youtube_verified': gate.get('youtube_verified') is True,
                                  'accepted_existing_blocks': [name for name in accepted[:50] if isinstance(name, str) and name in _CHECK_LABELS] if isinstance(accepted, list) else []}
    else:
        result['release_gate'] = None
    versions = _versions(journal.get('versions')) or _versions(report.get('versions'))
    actual = {}
    trustworthy_versions = result['evidence']['verified_successful_report'] and report.get('state') == 'passed'
    if journal.get('pending'):
        trustworthy_versions = False
    journal_versions = _versions(journal.get('versions'))
    if journal_versions and journal_versions != _versions(report.get('versions')):
        trustworthy_versions = False
    if trustworthy_versions and check_report.get('status') == 'passed':
        for check in check_report.get('checks', []) if isinstance(check_report.get('checks'), list) else []:
            if isinstance(check, dict) and check.get('name') == 'packaged_versions' and check.get('status') == 'passed' and isinstance(check.get('versions'), dict):
                actual = check['versions']
                break
    result['current_versions'] = []
    for name, actual_key in (('yt-dlp', 'ytDlp'), ('ffmpeg', 'ffmpeg'), ('deno', 'deno')) if trustworthy_versions else []:
        installed = _version(actual.get(actual_key))
        version = installed or versions.get(name)
        result['current_versions'].append({'component': name, 'label': _COMPONENTS[name], 'version': version,
            'source': 'runtime_check' if installed else 'approved_dependency' if version else 'unavailable',
            'package_version': versions.get('imageio-ffmpeg') if name == 'ffmpeg' else None})
    # A failed candidate never supplies before/after versions. Only two
    # consecutive actual promotions can prove the last successful changes.
    history = report.get('history')
    promotions = [item for item in history[-20:] if isinstance(item, dict) and item.get('event') == 'promoted' and _versions(item.get('versions'))] if isinstance(history, list) else []
    result['component_changes'] = []
    if trustworthy_versions and len(promotions) >= 2:
        before, after = _versions(promotions[-2]['versions']), _versions(promotions[-1]['versions'])
        if after == versions:
            for name, version in list(after.items())[:40]:
                if before.get(name) is not None and before[name] != version:
                    result['component_changes'].append({'component': name, 'label': _COMPONENTS.get(name, name),
                        'before': before[name], 'after': version, 'at': _date(promotions[-1].get('at'))})
    quarantine = journal.get('quarantine', report.get('quarantine'))
    result['quarantined_versions'] = len(quarantine) if isinstance(quarantine, list) else 0
    result['quarantine'] = []
    for item in quarantine[-10:] if isinstance(quarantine, list) else []:
        if isinstance(item, dict):
            reason = _reason(item.get('reason'))
            result['quarantine'].append({'created_at': _date(item.get('created_at')), 'reason': reason['code'], 'message': reason['message'],
                'versions': [{'component': name, 'version': version} for name, version in list(_versions(item.get('versions')).items())[:40]]})
    pending = journal.get('pending')
    result['pending'] = None
    if isinstance(pending, dict) and pending:
        operation = 'manual_rollback' if pending.get('operation') == 'manual_rollback' else 'promotion'
        result['pending'] = {'operation': operation, 'created_at': _date(pending.get('created_at')),
                             'status': 'awaiting_confirmation'}
    result['failure'] = _reason(journal['last_error']) if journal.get('last_error') else None
    result['components'] = 'all_pinned_python_dependencies' if report.get('components') == 'all_pinned_python_dependencies' else None


def inspect_maintenance():
    token = _DEADLINE.set(time.monotonic() + REQUEST_BUDGET_SECONDS)
    try:
        return _inspect_maintenance()
    finally:
        _DEADLINE.reset(token)


def _inspect_maintenance():
    checked = datetime.now(timezone.utc).isoformat()
    result = {'automated': False, 'state': 'not_configured', 'checked_at': checked,
              'repository': REPOSITORY, 'repository_url': 'https://github.com/' + REPOSITORY,
              'workflow_url': f'https://github.com/{REPOSITORY}/actions/workflows/{WORKFLOW}',
              'schedule': None, 'next_check': None, 'last_check': None,
              'run': None, 'release_gate': None, 'current_versions': [], 'component_changes': [],
              'checks': [], 'check_summary': {'passed': 0, 'blocked': 0, 'failed': 0, 'unknown': 0},
              'failed_checks': [], 'quarantine': [], 'quarantined_versions': 0, 'pending': None, 'failure': None,
              'evidence': {'source': 'github', 'report_run_id': None, 'matches_latest_run': False, 'verified_successful_report': False, 'reported_at': None},
              'message': 'O AutoCura aguarda a publicação do workflow no GitHub e uma primeira execução verificada.'}
    api = f'https://api.github.com/repos/{REPOSITORY}/actions/workflows/{WORKFLOW}'
    try:
        workflow = read_json(api)
    except urllib.error.HTTPError as error:
        if error.code == 404:
            return result
        result.update(state='verification_unavailable', message='O GitHub não permitiu confirmar a manutenção agora. Atualize o status mais tarde.')
        return result
    except (OSError, ValueError, RecursionError):
        result.update(state='verification_unavailable', message='Não foi possível confirmar a manutenção no GitHub nesta consulta.')
        return result
    if workflow.get('state') != 'active':
        result.update(state='disabled', message='O workflow de manutenção está desativado no GitHub.')
        return result
    result.update(schedule='Diariamente às 05:23 UTC', next_check=next_check(), state='awaiting_first_run',
                  message='O workflow está agendado. A ativação aguarda uma execução real bem-sucedida.')
    try:
        runs = read_json(api + '/runs?per_page=5').get('workflow_runs', [])
        if not isinstance(runs, list):
            raise ValueError('invalid_github_runs')
        latest = runs[0] if runs else None
        if not isinstance(latest, dict):
            return result
        result['run'] = _run(latest)
        result['last_check'] = result['run']['updated_at'] or result['run']['started_at']
        status, conclusion = result['run']['status'], result['run']['conclusion']
        if status != 'completed':
            result.update(state='checking', message='O GitHub está verificando a manutenção. A versão publicada continua disponível.')
        elif conclusion != 'success':
            result.update(state='attention_required', message='A última execução de manutenção falhou. Consulte o workflow; a configuração ou compatibilidade precisa de revisão.')
        # Fetch the persisted diagnostics even after a failed job. Its last
        # successful components remain useful historical evidence, but never
        # prove that a newer pending or failed job succeeded.
        report, journal = {}, {}
        diagnostics_available = True
        for path in ('public/autocura.json', '.autocura/state.json'):
            try:
                value = read_json(f'https://raw.githubusercontent.com/{REPOSITORY}/main/onda-web/{path}')
                if path.startswith('public/'):
                    report = value
                else:
                    journal = value
            except (OSError, ValueError, TypeError, RecursionError):
                diagnostics_available = False
        successful_runs = {_run(item)['id'] for item in runs[:5] if isinstance(item, dict)
                           and item.get('status') == 'completed' and item.get('conclusion') == 'success'}
        _details(result, report, journal, successful_runs)
        result['diagnostics_available'] = diagnostics_available
        if status != 'completed' or conclusion != 'success':
            return result
        # IDs must agree: an old successful report cannot validate a newer job.
        if result['pending'] is not None:
            result.update(state='attention_required', message='Há uma atualização ou recuperação aguardando confirmação. O AutoCura precisa concluir esse registro antes de publicar outra versão.')
        elif result['failure'] is not None:
            result.update(state='attention_required', message='O relatório registrou uma falha de manutenção. Consulte os detalhes e a execução no GitHub.')
        elif (report.get('automation_enabled') is True and result['evidence']['matches_latest_run']
                and result['pending'] is None and result['failure'] is None
                and report.get('state', 'passed') == 'passed'
                and (not isinstance(report.get('last_check'), dict) or report['last_check'].get('status', 'passed') == 'passed')):
            result.update(automated=True, state='active', message='AutoCura ativo no GitHub: verificação diária, testes de compatibilidade e publicação das versões aprovadas.')
        elif not report:
            result.update(state='verification_unavailable', message='O workflow concluiu, mas seu relatório não ficou disponível nesta consulta.')
        elif result['evidence']['matches_latest_run'] and (report.get('state', 'passed') != 'passed'
                or (isinstance(report.get('last_check'), dict) and report['last_check'].get('status', 'passed') != 'passed')):
            result.update(state='attention_required', message='A execução concluiu, mas o relatório não aprovou a atualização. A versão anterior é preservada até a revisão dos testes.')
        else:
            result.update(state='report_unconfirmed', message='O workflow concluiu, mas o relatório correspondente ainda não confirmou a automação.')
    except (OSError, ValueError, TypeError, RecursionError):
        result.update(state='verification_unavailable', message='O workflow existe, mas não foi possível verificar sua execução e relatório agora.')
    return result


def maintenance_status():
    global _CACHE, _CACHE_TIME
    with _LOCK:
        if _CACHE is not None and time.monotonic() - _CACHE_TIME < CACHE_SECONDS:
            return deepcopy(_CACHE)
        _CACHE = inspect_maintenance()
        _CACHE_TIME = time.monotonic()
        return deepcopy(_CACHE)


def register(app):
    @app.get('/api/maintenance')
    async def maintenance():
        return await asyncio.to_thread(maintenance_status)
