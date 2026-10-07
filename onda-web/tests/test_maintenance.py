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
