"""Read-only, cached GitHub evidence for scheduled AutoCura maintenance.

A workflow file or a bundled snapshot alone never proves an active updater.
Only a successful real run and its persisted release report confirm activation.
No request performs updates or receives administrator credentials.
"""
from __future__ import annotations
import asyncio
from datetime import datetime, timedelta, timezone
import json
import threading
import time
import urllib.error
import urllib.request

REPOSITORY = 'RichardXLR/BraXYTDown'
WORKFLOW = 'onda-autocura.yml'
CACHE_SECONDS = 300
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
    with urllib.request.build_opener(SameHostRedirect()).open(request, timeout=3) as response:
        data = response.read(256 * 1024 + 1)
        if len(data) > 256 * 1024:
            raise ValueError('github_response_too_large')
        value = json.loads(data)
        if not isinstance(value, dict):
            raise ValueError('invalid_github_report')
        return value


def next_check():
    now = datetime.now(timezone.utc)
    following = now.replace(hour=5, minute=23, second=0, microsecond=0)
    if following <= now:
        following += timedelta(days=1)
    return following.isoformat()


def inspect_maintenance():
    checked = datetime.now(timezone.utc).isoformat()
    result = {'automated': False, 'state': 'not_configured', 'checked_at': checked,
              'repository': REPOSITORY, 'repository_url': 'https://github.com/' + REPOSITORY,
              'workflow_url': f'https://github.com/{REPOSITORY}/actions/workflows/{WORKFLOW}',
              'schedule': None, 'next_check': None, 'last_check': None,
              'message': 'O AutoCura aguarda a publicação do workflow no GitHub e uma primeira execução verificada.'}
    api = f'https://api.github.com/repos/{REPOSITORY}/actions/workflows/{WORKFLOW}'
    try:
        workflow = read_json(api)
    except urllib.error.HTTPError as error:
        if error.code == 404:
            return result
        result.update(state='verification_unavailable', message='O GitHub não permitiu confirmar a manutenção agora. Atualize o status mais tarde.')
        return result
    except (OSError, ValueError):
        result.update(state='verification_unavailable', message='Não foi possível confirmar a manutenção no GitHub nesta consulta.')
        return result
    if workflow.get('state') != 'active':
        result.update(state='disabled', message='O workflow de manutenção está desativado no GitHub.')
        return result
    result.update(schedule='Diariamente às 05:23 UTC', next_check=next_check(), state='awaiting_first_run',
                  message='O workflow está agendado. A ativação aguarda uma execução real bem-sucedida.')
    try:
        runs = read_json(api + '/runs?per_page=1').get('workflow_runs', [])
        report = read_json(f'https://raw.githubusercontent.com/{REPOSITORY}/main/onda-web/public/autocura.json')
        latest = runs[0] if runs else None
        if not isinstance(latest, dict):
            return result
        result['last_check'] = latest.get('updated_at') or latest.get('created_at')
        result['run'] = {'id': latest.get('id'), 'status': latest.get('status'), 'conclusion': latest.get('conclusion')}
        if latest.get('status') != 'completed':
            result.update(state='checking', message='O GitHub está verificando a manutenção. A versão publicada continua disponível.')
            return result
        if latest.get('conclusion') != 'success':
            result.update(state='attention_required', message='A última execução de manutenção falhou. Consulte o workflow; a configuração ou compatibilidade precisa de revisão.')
            return result
        execution = report.get('execution') or {}
        gate = report.get('release_gate')
        result['release_gate'] = gate
        result['quarantined_versions'] = len(report.get('quarantine', [])) if isinstance(report.get('quarantine'), list) else 0
        result['components'] = report.get('components')
        # IDs must agree: an old successful report cannot validate a newer job.
        if (report.get('automation_enabled') is True and execution.get('repository') == REPOSITORY
                and str(execution.get('run_id')) == str(latest.get('id'))):
            result.update(automated=True, state='active', message='AutoCura ativo no GitHub: verificação diária, testes de compatibilidade e publicação das versões aprovadas.')
        else:
            result.update(state='report_unconfirmed', message='O workflow concluiu, mas o relatório correspondente ainda não confirmou a automação.')
    except (OSError, ValueError, TypeError):
        result.update(state='verification_unavailable', message='O workflow existe, mas não foi possível verificar sua execução e relatório agora.')
    return result


def maintenance_status():
    global _CACHE, _CACHE_TIME
    with _LOCK:
        if _CACHE is not None and time.monotonic() - _CACHE_TIME < CACHE_SECONDS:
            return dict(_CACHE)
        _CACHE = inspect_maintenance()
        _CACHE_TIME = time.monotonic()
        return dict(_CACHE)


def register(app):
    @app.get('/api/maintenance')
    async def maintenance():
        return await asyncio.to_thread(maintenance_status)
