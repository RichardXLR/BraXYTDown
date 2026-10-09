"""A workflow or stale report must never advertise an active updater."""
import urllib.error
import pytest
from api import maintenance


def remote_evidence(monkeypatch, workflow='active', run=None, report=None):
    calls = []
    def read(url):
        calls.append(url)
        if url.endswith('onda-autocura.yml'):
            return {'state': workflow}
        if '/runs?' in url:
            return {'workflow_runs': [run] if run else []}
        return report or {}
    monkeypatch.setattr(maintenance, 'read_json', read)
    return calls


def test_missing_workflow_is_inactive_and_does_not_fetch_other_sources(monkeypatch):
    calls = []
    def missing(url):
        calls.append(url)
        raise urllib.error.HTTPError(url, 404, 'Not Found', {}, None)
    monkeypatch.setattr(maintenance, 'read_json', missing)
    result = maintenance.inspect_maintenance()
    assert result['automated'] is False and result['state'] == 'not_configured'
    assert len(calls) == 1
    assert result['schedule'] is None


def test_successful_job_requires_its_own_persisted_report(monkeypatch):
    run = {'id': 42, 'status': 'completed', 'conclusion': 'success', 'updated_at': '2026-10-07T12:00:00Z'}
    report = {'automation_enabled': True, 'execution': {'repository': maintenance.REPOSITORY, 'run_id': '41'}}
    remote_evidence(monkeypatch, run=run, report=report)
    assert maintenance.inspect_maintenance()['automated'] is False
    report['execution']['run_id'] = '42'
    result = maintenance.inspect_maintenance()
    assert result['automated'] is True and result['state'] == 'active'
    assert result['last_check'] == run['updated_at']
    assert result['schedule'] == 'Diariamente às 05:23 UTC'


def test_real_report_exposes_nested_release_gate_and_existing_platform_block(monkeypatch):
    gate = {'status': 'passed', 'youtube_verified': False, 'accepted_existing_blocks': ['me_at_zoo_metadata']}
    remote_evidence(monkeypatch,
        run={'id': 42, 'status': 'completed', 'conclusion': 'success'},
        report={'automation_enabled': True,
                'execution': {'repository': maintenance.REPOSITORY, 'run_id': '42'},
                'last_check': {'release_gate': gate}})
    result = maintenance.inspect_maintenance()
    assert result['automated'] is True
    assert result['release_gate'] == gate


@pytest.mark.parametrize('status,conclusion,expected', [
    ('in_progress', None, 'checking'), ('completed', 'failure', 'attention_required'),
    ('completed', 'cancelled', 'attention_required'), ('completed', 'timed_out', 'attention_required')])
def test_bad_or_unfinished_execution_does_not_enable_automation(monkeypatch, status, conclusion, expected):
    run = {'id': 42, 'status': status, 'conclusion': conclusion}
    report = {'automation_enabled': True, 'execution': {'repository': maintenance.REPOSITORY, 'run_id': '42'}}
    remote_evidence(monkeypatch, run=run, report=report)
    result = maintenance.inspect_maintenance()
    assert result['automated'] is False and result['state'] == expected


def test_disabled_workflow_stays_inactive(monkeypatch):
    calls = remote_evidence(monkeypatch, workflow='disabled_manually')
    result = maintenance.inspect_maintenance()
    assert result['state'] == 'disabled' and result['automated'] is False
    assert len(calls) == 1


def test_outage_and_cache_are_honest_and_bounded(monkeypatch):
    calls = []
    def outage(url):
        calls.append(url)
        raise OSError('offline')
    monkeypatch.setattr(maintenance, 'read_json', outage)
    monkeypatch.setattr(maintenance, '_CACHE', None)
    first = maintenance.maintenance_status()
    second = maintenance.maintenance_status()
    assert first == second
    assert first['state'] == 'verification_unavailable' and first['automated'] is False
    assert len(calls) == 1


@pytest.mark.parametrize('run,expected', [
    ({'id': 42, 'status': 'in_progress', 'conclusion': None}, 'checking'),
    ({'id': 42, 'status': 'completed', 'conclusion': 'failure'}, 'attention_required'),
])
def test_missing_report_does_not_hide_pending_or_failed_workflow(monkeypatch, run, expected):
    calls = []

    def read(url):
        calls.append(url)
        if url.endswith('onda-autocura.yml'):
            return {'state': 'active'}
        if '/runs?' in url:
            return {'workflow_runs': [run]}
        raise urllib.error.HTTPError(url, 404, 'Not Found', {}, None)

    monkeypatch.setattr(maintenance, 'read_json', read)
    result = maintenance.inspect_maintenance()
    assert result['state'] == expected and result['automated'] is False
    assert result['run']['id'] == 42
    assert len(calls) == 4


@pytest.mark.parametrize('execution', [['unexpected'], 'invalid', 42, True, None])
def test_invalid_execution_report_never_crashes_or_enables_automation(monkeypatch, execution):
    remote_evidence(monkeypatch,
        run={'id': 42, 'status': 'completed', 'conclusion': 'success'},
        report={'automation_enabled': True, 'execution': execution})
    result = maintenance.inspect_maintenance()
    assert result['state'] == 'report_unconfirmed' and result['automated'] is False


def test_missing_run_identifiers_cannot_confirm_a_report(monkeypatch):
    remote_evidence(monkeypatch,
        run={'status': 'completed', 'conclusion': 'success'},
        report={'automation_enabled': True, 'execution': {'repository': maintenance.REPOSITORY}})
    result = maintenance.inspect_maintenance()
    assert result['state'] == 'report_unconfirmed' and result['automated'] is False


@pytest.mark.parametrize('runs', [None, 'invalid', {'0': {'id': 42}}])
def test_invalid_workflow_runs_fail_closed(monkeypatch, runs):
    monkeypatch.setattr(maintenance, 'read_json', lambda url:
        {'state': 'active'} if url.endswith('onda-autocura.yml') else {'workflow_runs': runs})
    result = maintenance.inspect_maintenance()
    assert result['state'] == 'verification_unavailable' and result['automated'] is False


def test_cached_release_gate_and_run_cannot_be_mutated_by_a_caller(monkeypatch):
    calls = []

    def inspect():
        calls.append(True)
        return {'state': 'active', 'run': {'id': 42},
                'release_gate': {'accepted_existing_blocks': ['youtube_canary']}}

    monkeypatch.setattr(maintenance, 'inspect_maintenance', inspect)
    monkeypatch.setattr(maintenance, '_CACHE', None)
    first = maintenance.maintenance_status()
    first['run']['id'] = 7
    first['release_gate']['accepted_existing_blocks'].clear()
    second = maintenance.maintenance_status()
    assert second['run']['id'] == 42
    assert second['release_gate']['accepted_existing_blocks'] == ['youtube_canary']
    assert calls == [True]
