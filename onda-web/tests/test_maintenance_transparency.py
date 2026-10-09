"""Public diagnostic evidence stays bounded and cannot imply a false update."""
import json
import urllib.error
import pytest
from api import maintenance


def evidence(monkeypatch, runs, report=None, journal=None):
    calls = []
    def read(url):
        calls.append(url)
        if url.endswith('onda-autocura.yml'):
            return {'state': 'active'}
        if '/runs?' in url:
            return {'workflow_runs': runs}
        if url.endswith('public/autocura.json'):
            return report or {}
        if url.endswith('.autocura/state.json'):
            return journal or {}
        raise AssertionError('unbounded GitHub request')
    monkeypatch.setattr(maintenance, 'read_json', read)
    return calls


def run(identifier=42, conclusion='success', status='completed'):
    return {'id': identifier, 'status': status, 'conclusion': conclusion,
            'run_started_at': '2026-10-09T05:23:00Z', 'updated_at': '2026-10-09T05:30:00Z',
            'html_url': 'https://evil.invalid/?token=secret'}


def report(identifier=42):
    versions = {'yt-dlp': '2026.8.19', 'deno': '2.9.7', 'imageio-ffmpeg': '0.6.0'}
    return {'automation_enabled': True, 'state': 'passed', 'components': 'all_pinned_python_dependencies',
            'execution': {'repository': maintenance.REPOSITORY, 'run_id': str(identifier)},
            'snapshot_at': '2026-10-09T05:30:00Z', 'versions': versions,
            'history': [
                {'event': 'promoted', 'at': '2026-10-08T05:30:00Z', 'versions': {**versions, 'yt-dlp': '2026.7.10'}},
                {'event': 'promoted', 'at': '2026-10-09T05:30:00Z', 'versions': versions}],
            'last_check': {'status': 'passed', 'checks': [
                {'name': 'runtime', 'status': 'passed'},
                {'name': 'packaged_versions', 'status': 'passed',
                 'versions': {'ytDlp': '2026.08.19', 'deno': '2.9.7', 'ffmpeg': '7.0.2-static'}},
                {'name': 'me_at_zoo_metadata', 'status': 'blocked', 'code': 'platform_blocked'}],
                'release_gate': {'status': 'passed', 'youtube_verified': False,
                                 'accepted_existing_blocks': ['me_at_zoo_metadata']}}}


def test_real_versions_counts_run_link_and_changes(monkeypatch):
    payload = report()
    calls = evidence(monkeypatch, [run()], payload, {'versions': payload['versions']})
    result = maintenance.inspect_maintenance()
    assert result['automated'] and result['evidence']['verified_successful_report']
    assert result['run']['url'] == f'https://github.com/{maintenance.REPOSITORY}/actions/runs/42'
    assert result['run']['started_at'] == '2026-10-09T05:23:00Z'
    assert result['check_summary'] == {'passed': 2, 'blocked': 1, 'failed': 0, 'unknown': 0}
    assert result['current_versions'][1] == {'component': 'ffmpeg', 'label': 'FFmpeg',
                                           'version': '7.0.2-static', 'source': 'runtime_check', 'package_version': '0.6.0'}
    assert result['component_changes'] == [{'component': 'yt-dlp', 'label': 'yt-dlp',
                                          'before': '2026.7.10', 'after': '2026.8.19', 'at': '2026-10-09T05:30:00Z'}]
    assert len(calls) == 4


@pytest.mark.parametrize('latest', [run(43, 'failure'), run(43, None, 'in_progress')])
def test_failed_new_job_preserves_verified_historical_components_without_activation(monkeypatch, latest):
    payload = report()
    evidence(monkeypatch, [latest, run()], payload, {'versions': payload['versions']})
    result = maintenance.inspect_maintenance()
    assert result['automated'] is False
    assert result['state'] in ('checking', 'attention_required')
    assert result['evidence'] == {'source': 'github', 'report_run_id': 42, 'matches_latest_run': False,
                                  'verified_successful_report': True, 'reported_at': '2026-10-09T05:30:00Z'}
    assert result['current_versions'][0]['version'] == '2026.08.19'


@pytest.mark.parametrize('reason', ['missing_run', 'failed_report_run', 'mismatched_journal', 'candidate_report'])
def test_unverified_candidate_versions_are_never_published_components(monkeypatch, reason):
    payload = report()
    runs, journal = [run()], {'versions': payload['versions']}
    if reason == 'missing_run':
        payload['execution']['run_id'] = '41'
    elif reason == 'failed_report_run':
        runs = [run(conclusion='failure')]
    elif reason == 'mismatched_journal':
        journal['versions'] = {**payload['versions'], 'yt-dlp': '2026.7.10'}
    else:
        payload['state'] = 'promotion_pending'
    evidence(monkeypatch, runs, payload, journal)
    result = maintenance.inspect_maintenance()
    assert result['current_versions'] == []
    assert result['component_changes'] == []


def test_latest_failed_report_never_uses_candidate_as_current_version(monkeypatch):
    payload = report(43)
    payload['versions']['yt-dlp'] = '2099.1.1'
    evidence(monkeypatch, [run(43, 'failure'), run(42)], payload, {'versions': payload['versions']})
    result = maintenance.inspect_maintenance()
    assert result['current_versions'] == [] and result['component_changes'] == []
    assert result['state'] == 'attention_required'


def test_unknown_error_text_and_deployment_credentials_are_never_exposed(monkeypatch):
    malicious = 'https://evil.invalid/?token=SecretValue <script>attack()</script>'
    payload = report()
    payload['last_check']['checks'] += [{'name': ['unsafe'], 'status': 'failed', 'code': malicious,
                                        'inspection': {'authorization': malicious}}]
    journal = {'versions': payload['versions'], 'last_error': malicious,
               'pending': {'created_at': malicious, 'operation': malicious, 'candidate': {'url': malicious}},
               'quarantine': [{'reason': malicious, 'created_at': malicious, 'versions': {'yt-dlp': malicious}, 'deployment_id': malicious}],
               'active': {'url': malicious}}
    evidence(monkeypatch, [run()], payload, journal)
    result = maintenance.inspect_maintenance()
    encoded = json.dumps(result)
    assert all(secret not in encoded for secret in ('SecretValue', '<script>', 'evil.invalid', 'authorization', 'deployment_id'))
    assert result['failed_checks'][0]['name'].startswith('additional_check_')
    assert result['failure']['code'] == 'maintenance_check_failed'
    assert result['pending'] == {'operation': 'promotion', 'created_at': None, 'status': 'awaiting_confirmation'}
    assert result['automated'] is False


def test_pending_and_known_failure_have_safe_explanations(monkeypatch):
    payload = report()
    journal = {'versions': payload['versions'], 'last_error': 'production_changed_during_recovery',
               'pending': {'operation': 'manual_rollback', 'created_at': '2026-10-09T05:30:00Z'},
               'quarantine': [{'reason': 'candidate_application_tests_failed', 'created_at': '2026-10-09T05:30:00Z',
                               'versions': payload['versions']}]}
    evidence(monkeypatch, [run()], payload, journal)
    result = maintenance.inspect_maintenance()
    assert result['state'] == 'attention_required' and not result['automated']
    assert result['pending']['operation'] == 'manual_rollback'
    assert 'produção mudou' in result['failure']['message']
    assert result['quarantine'][0]['reason'] == 'candidate_application_tests_failed'


def test_diagnostic_lists_are_bounded(monkeypatch):
    payload = report()
    payload['last_check']['checks'] = [{'name': 'runtime', 'status': 'passed'}] * 1000
    journal = {'versions': payload['versions'], 'quarantine': [{'reason': 'candidate_compatibility_failed'}] * 1000}
    evidence(monkeypatch, [run()], payload, journal)
    result = maintenance.inspect_maintenance()
    assert len(result['checks']) == 50 and result['check_summary']['passed'] == 50
    assert len(result['quarantine']) == 10 and result['quarantined_versions'] == 1000


@pytest.mark.parametrize('identifier', [None, True, '<script>', '0', '1' * 100, ['42'], {'id': 42}])
def test_invalid_run_identifiers_never_create_external_links_or_enable_automation(monkeypatch, identifier):
    latest = run(identifier)
    payload = report()
    evidence(monkeypatch, [latest], payload)
    result = maintenance.inspect_maintenance()
    assert result['run']['url'] is None and not result['automated']


def test_missing_diagnostics_do_not_hide_a_failed_run(monkeypatch):
    calls = evidence(monkeypatch, [run(conclusion='failure')])
    result = maintenance.inspect_maintenance()
    assert result['state'] == 'attention_required' and result['current_versions'] == []
    assert result['check_summary']['passed'] == 0 and len(calls) == 4


def test_report_outage_retains_authoritative_success_but_never_enables_automation(monkeypatch):
    def read(url):
        if url.endswith('onda-autocura.yml'):
            return {'state': 'active'}
        if '/runs?' in url:
            return {'workflow_runs': [run()]}
        raise urllib.error.HTTPError(url, 503, 'offline token=secret', {}, None)
    monkeypatch.setattr(maintenance, 'read_json', read)
    result = maintenance.inspect_maintenance()
    assert result['state'] == 'verification_unavailable' and result['run']['conclusion'] == 'success'
    assert result['diagnostics_available'] is False and not result['automated']


def test_total_github_deadline_is_shared_and_reset(monkeypatch):
    observed = []
    def read(url):
        observed.append(maintenance._DEADLINE.get())
        if url.endswith('onda-autocura.yml'):
            return {'state': 'active'}
        if '/runs?' in url:
            return {'workflow_runs': [run()]}
        return report()
    monkeypatch.setattr(maintenance, 'read_json', read)
    maintenance.inspect_maintenance()
    assert len(observed) == 4 and len(set(observed)) == 1
    assert maintenance._DEADLINE.get() is None


def test_successful_workflow_does_not_hide_failed_compatibility_report(monkeypatch):
    payload = report(); payload['state'] = 'failed'; payload['last_check']['status'] = 'failed'
    payload['last_check']['checks'][0]['status'] = 'failed'
    evidence(monkeypatch, [run()], payload)
    result = maintenance.inspect_maintenance()
    assert result['state'] == 'attention_required' and not result['automated']
    assert result['current_versions'] == [] and len(result['failed_checks']) == 1
