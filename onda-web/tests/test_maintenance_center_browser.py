"""Read-only transparency panel in a real browser, with no external users."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import threading
import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope='module')
def maintenance_browser():
    playwright = pytest.importorskip('playwright.sync_api')
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path in ('/maintenance-center.js', '/maintenance-center.css'):
                content = (ROOT / 'public' / self.path[1:]).read_bytes()
                mime = 'application/javascript' if self.path.endswith('.js') else 'text/css'
            else:
                content = b'''<!doctype html><html><head><meta name="viewport" content="width=device-width,initial-scale=1"><style>:root{--text:#eef0f2;--muted:#a0b3ca;--panel-inset:#101417;--line:#293139;--lime:#5aabff;--danger:#f3aaa6;font-family:Arial}*{box-sizing:border-box}body{margin:0;background:#071225;padding:20px}#autocura-transparency{max-width:800px;margin:auto}</style><link rel="stylesheet" href="/maintenance-center.css"></head><body><div id="autocura-transparency"></div><script src="/maintenance-center.js"></script></body></html>'''
                mime = 'text/html'
            self.send_response(200); self.send_header('Content-Type', mime + '; charset=utf-8'); self.end_headers(); self.wfile.write(content)
        def log_message(self, *_args):
            pass
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
    try:
        with playwright.sync_playwright() as instance:
            try:
                browser = instance.chromium.launch(headless=True)
            except playwright.Error:
                pytest.skip('Playwright Chromium is not installed in this API-only environment')
            yield browser, f'http://127.0.0.1:{server.server_port}'
            browser.close()
    finally:
        server.shutdown(); server.server_close(); thread.join(timeout=5)


@pytest.fixture
def maintenance_page(maintenance_browser):
    browser, origin = maintenance_browser
    with browser.new_context() as context:
        page = context.new_page(); page.goto(origin)
        page.wait_for_function('Boolean(window.OndaMaintenanceCenter)')
        yield page


def data():
    return {'automated': True, 'state': 'active', 'checked_at': '2026-10-09T05:31:00Z', 'diagnostics_available': True,
            'run': {'id': 42, 'status': 'completed', 'conclusion': 'success', 'updated_at': '2026-10-09T05:30:00Z', 'url': 'https://evil.invalid/'},
            'evidence': {'report_run_id': 42, 'matches_latest_run': True, 'verified_successful_report': True},
            'checks': [{'label': 'Serviço', 'status': 'passed'}, {'label': 'YouTube', 'status': 'blocked', 'message': 'A plataforma bloqueou o teste.'},
                       {'label': 'Conversão MP3', 'status': 'failed', 'message': 'A conversão precisa de revisão.'}],
            'current_versions': [{'component': 'yt-dlp', 'label': 'yt-dlp', 'version': '2026.8.19', 'source': 'runtime_check'},
                                 {'component': 'ffmpeg', 'label': 'FFmpeg', 'version': '7.0.2-static', 'source': 'runtime_check'},
                                 {'component': 'deno', 'label': 'Deno', 'version': '2.9.7', 'source': 'approved_dependency'}],
            'component_changes': [{'component': 'yt-dlp', 'label': 'yt-dlp', 'before': '2026.7.10', 'after': '2026.8.19', 'at': '2026-10-09T05:30:00Z'}],
            'quarantined_versions': 1, 'quarantine': [{'message': 'A versão candidata falhou.', 'created_at': '2026-10-09T05:30:00Z',
                                                       'versions': [{'component': 'yt-dlp', 'version': '2026.8.19'}]}]}


def test_visible_tests_components_changes_and_safe_exact_run_link(maintenance_page):
    page = maintenance_page
    page.evaluate('data => OndaMaintenanceCenter.render(data)', data())
    assert page.locator('.maintenance-run-title').inner_text() == 'Concluída\nAutomático ativo'
    assert page.locator('.maintenance-stat > strong').all_inner_texts() == ['1', '1', '1', '0']
    assert page.locator('.maintenance-version dd').all_inner_texts() == ['2026.8.19', '7.0.2-static', '2.9.7']
    assert page.locator('.maintenance-run-link').get_attribute('href') == 'https://github.com/RichardXLR/BraXYTDown/actions/runs/42'
    assert '2026.7.10' in page.locator('.maintenance-changes').inner_text()
    page.locator('.maintenance-checks summary').click()
    assert page.locator('.maintenance-check-list').is_visible()
    assert 'Bloqueado pela fonte' in page.locator('.maintenance-check-list').inner_text()
    page.locator('.maintenance-quarantine summary').click()
    assert 'A versão candidata falhou.' in page.locator('.maintenance-quarantine').inner_text()


def test_unknown_state_and_failed_latest_run_never_show_active_updater(maintenance_page):
    page = maintenance_page
    page.evaluate('OndaMaintenanceCenter.render({state:"verification_unavailable"})')
    assert 'Automático ativo' not in page.locator('#autocura-transparency').inner_text()
    assert 'Aguardando confirmação' in page.locator('.maintenance-run-title').inner_text()
    assert 'As pendências ainda não puderam ser verificadas.' in page.locator('#autocura-transparency').inner_text()
    payload = data(); payload['automated'] = False; payload['state'] = 'attention_required'
    payload['run']['conclusion'] = 'failure'; payload['run']['id'] = 43
    payload['evidence']['matches_latest_run'] = False
    page.evaluate('data => OndaMaintenanceCenter.render(data)', payload)
    assert 'Automático ativo' not in page.locator('#autocura-transparency').inner_text()
    assert 'último relatório bem-sucedido confirmado' in page.locator('#autocura-transparency').inner_text()
    assert page.locator('.maintenance-run-card').count() == 1


def test_remote_labels_are_text_and_untrusted_urls_are_not_used(maintenance_page):
    page = maintenance_page
    malicious = '<img src=x onerror="window.__attacked=true">'
    payload = data(); payload['current_versions'][0]['label'] = malicious
    payload['checks'][0]['label'] = malicious
    payload['failure'] = {'message': malicious}
    payload['quarantine'][0]['message'] = malicious
    page.evaluate('data => OndaMaintenanceCenter.render(data)', payload)
    assert page.locator('#autocura-transparency img').count() == 0
    assert page.locator('#autocura-transparency script').count() == 0
    assert page.evaluate('Boolean(window.__attacked)') is False
    assert malicious in page.locator('#autocura-transparency').inner_text()
    assert 'evil.invalid' not in page.locator('#autocura-transparency').inner_html()


@pytest.mark.parametrize('width', [320, 390, 768, 1440])
def test_panel_fits_small_and_large_screens_without_horizontal_overflow(maintenance_page, width):
    page = maintenance_page; page.set_viewport_size({'width': width, 'height': 900})
    payload = data(); payload['component_changes'][0]['label'] = 'a' * 64
    payload['component_changes'][0]['after'] = '2.' + '9' * 60
    page.evaluate('data => OndaMaintenanceCenter.render(data)', payload)
    assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth')
    assert page.locator('.maintenance-run-link').is_visible()


def test_pending_and_version_distribution_are_explicit(maintenance_page):
    page = maintenance_page; payload = data()
    payload['current_versions'][1]['version'] = None; payload['current_versions'][1]['package_version'] = '0.6.0'
    payload['pending'] = {'operation': 'manual_rollback', 'created_at': '2026-10-09T05:30:00Z'}
    page.evaluate('data => OndaMaintenanceCenter.render(data)', payload)
    assert 'binário não confirmado' in page.locator('.maintenance-version').nth(1).inner_text()
    assert 'Recuperação aguardando confirmação' in page.locator('.maintenance-issues').inner_text()


def test_unavailable_report_does_not_claim_empty_pending_list_is_success(maintenance_page):
    page = maintenance_page; payload = data(); payload['diagnostics_available'] = False
    payload['quarantine'] = []; payload['quarantined_versions'] = 0
    page.evaluate('data => OndaMaintenanceCenter.render(data)', payload)
    assert 'As pendências ainda não puderam ser verificadas.' in page.locator('.maintenance-issues').inner_text()
