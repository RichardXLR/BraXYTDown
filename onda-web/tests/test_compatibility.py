import pytest
import json
from fastapi.testclient import TestClient

from api import compatibility, engine, index, security
from api.security import AudioError


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(compatibility, 'versions', lambda: {'ytDlp':'test', 'ffmpeg':'7.0', 'deno':'2.9'})
    with TestClient(index.app) as client:
        yield client


def test_compatibility_snapshot_distinguishes_automation_from_manual_checks(client):
    response = client.get('/api/compatibility')
    assert response.status_code == 200
    data = response.json()
    assert data['sitesCount'] > 1000
    assert data['testScope'] == 'metadata'
    assert data['selfHealing']['automated'] is False
    assert 'browserMode' not in data
    assert response.headers['cache-control'] == 'no-store'


@pytest.mark.parametrize('enabled', [False, True])
def test_persisted_snapshot_never_confirms_live_automation(monkeypatch, tmp_path, enabled):
    (tmp_path / 'public').mkdir()
    (tmp_path / 'public' / 'autocura.json').write_text(json.dumps({
        'automation_enabled': enabled, 'automated': enabled, 'status': 'passed',
        'execution': {'repository': 'RichardXLR/BraXYTDown', 'run_id': '42'}}))
    monkeypatch.setattr(compatibility, 'ROOT', tmp_path)
    data = compatibility.release_status()
    assert data['automated'] is False
    assert data['snapshot_only'] is True
    assert data['snapshot_automation_enabled'] is enabled


@pytest.mark.parametrize('report', [[], ['invalid'], 'invalid', 42, None])
def test_invalid_snapshot_does_not_break_compatibility(monkeypatch, tmp_path, report):
    (tmp_path / 'public').mkdir()
    (tmp_path / 'public' / 'autocura.json').write_text(json.dumps(report))
    monkeypatch.setattr(compatibility, 'ROOT', tmp_path)
    data = compatibility.release_status()
    assert data['automated'] is False and data['snapshot_only'] is True
    assert data['state'] == 'not_configured'


def test_version_response_cannot_mutate_cached_toolchain(monkeypatch):
    current = {'ytDlp': 'test', 'ffmpeg': '7.0', 'deno': '2.9'}
    monkeypatch.setattr(compatibility, '_version_data', lambda: current)
    first = compatibility.versions()
    first['ffmpeg'] = 'indisponível'
    assert compatibility.versions()['ffmpeg'] == '7.0'


def test_manual_check_uses_worker_and_does_not_save_or_echo_cookies(client, monkeypatch):
    calls = []
    def inspect(url, guard, cookies=None):
        calls.append((url, cookies))
        guard.check()
        return {'title':'Áudio autorizado', 'source':'YouTube'}
    monkeypatch.setattr(engine, 'inspect_media', inspect)
    secret = '# Netscape HTTP Cookie File\n.youtube.com\tTRUE\t/\tTRUE\t0\tSID\tprivate-test-value'
    response = client.post('/api/compatibility/test', json={
        'url':'https://www.youtube.com/watch?v=jNQXAC9IVRw', 'cookies':secret})
    assert response.status_code == 200
    assert response.json()['ok'] is True
    assert calls[0][1] == secret
    assert secret not in response.text and 'private-test-value' not in response.text
    assert response.json()['scope'] == 'metadata'


@pytest.mark.parametrize('code,expected', [('platform_blocked','blocked'), ('upstream_timeout','blocked'),
                                        ('unavailable','failed'), ('no_audio','failed')])
def test_manual_checks_report_access_failures_without_fake_compatibility_pass(client, monkeypatch, code, expected):
    def inspect(*args, **kwargs):
        raise AudioError('A origem não respondeu.', code, 422)
    monkeypatch.setattr(engine, 'inspect_media', inspect)
    response = client.post('/api/compatibility/test', json={'url':'https://www.youtube.com/watch?v=jNQXAC9IVRw'})
    assert response.status_code == 200
    assert response.json()['ok'] is False
    assert response.json()['status'] == expected
    assert response.json()['code'] == code
    assert response.json()['elapsedMs'] >= 0
    assert index.SLOTS.acquire(blocking=False)
    assert index.SLOTS.acquire(blocking=False)
    index.SLOTS.release()
    index.SLOTS.release()


def test_manual_check_obeys_existing_capacity(client):
    index.SLOTS.acquire()
    index.SLOTS.acquire()
    try:
        response = client.post('/api/compatibility/test', json={'url':'https://example.com/track.wav'})
        assert response.status_code == 429
        assert response.json()['code'] == 'busy'
    finally:
        index.SLOTS.release()
        index.SLOTS.release()


def test_manual_check_rejects_private_urls(client):
    response = client.post('/api/compatibility/test', json={'url':'http://127.0.0.1/private.wav'})
    assert response.status_code == 400
    assert response.json()['code'] == 'unsafe_url'


def test_catalog_exposes_all_working_extractors_and_direct_files(client):
    response = client.get('/api/compatibility/providers')
    assert response.status_code == 200
    entries = response.json()['providers']
    assert len(entries) > 1000
    assert {'id': 'DirectMedia', 'name': 'Arquivo direto de áudio ou vídeo'} in entries
    assert any(item['id'] == 'Youtube' for item in entries)
    assert any(item['id'] == 'Vimeo' for item in entries)
    assert len({item['id'] for item in entries}) == len(entries)


def test_selected_provider_rejects_mismatched_link_without_network_or_cookie_use(client, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail('A mismatched provider must not contact a remote source')
    monkeypatch.setattr(engine, 'inspect_media', forbidden)
    response = client.post('/api/compatibility/test', json={
        'url': 'https://vimeo.com/123456', 'provider': 'Youtube', 'cookies': 'private-test-sentinel'})
    assert response.status_code == 200
    assert response.json()['ok'] is False
    assert response.json()['code'] == 'provider_mismatch'
    assert 'private-test-sentinel' not in response.text


def test_provider_selected_direct_file_uses_existing_guarded_inspection(client, monkeypatch):
    calls = []
    def inspect(url, guard, **options):
        calls.append((url, options))
        guard.check()
        return {'title': 'Owned clip', 'source': 'Arquivo direto'}
    monkeypatch.setattr(engine, 'inspect_media', inspect)
    response = client.post('/api/compatibility/test', json={
        'url': 'https://cdn.example.com/own.mp4', 'provider': 'DirectMedia', 'media_type': 'video'})
    assert response.status_code == 200 and response.json()['ok'] is True
    assert calls[0][1]['media_type'] == 'video'
