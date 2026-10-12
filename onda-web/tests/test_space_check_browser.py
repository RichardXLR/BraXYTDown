"""Actual UI -> owned authenticated HTTP -> FFmpeg with controlled quota reports.

Quota/duration overrides exercise estimation uncertainty, not actual device disk
exhaustion. Browser engine coverage does not represent physical phone testing.
"""
import threading
import pytest
from api import index, security
from test_browser_media_flow import media_browser, media_server
from test_experience_browser import open_studio

URL = 'https://ondaittoux.online/canary.wav'
MB = 1024 ** 2

def observe(monkeypatch, duration):
    events = []
    # The shared API cache is covered separately; synthetic metadata must not
    # outlive this fixture and influence another scenario's duration/quota.
    monkeypatch.setattr(index, 'cached_metadata', lambda *args: None)
    prepare = index.prepare_download
    def inspection(*args, **kwargs):
        events.append('inspect')
        return {'title': 'Fonte própria para estimativa', 'source': 'Arquivo direto',
                'duration': duration, 'hasAudio': True, 'formats': []}
    def processing(*args, **kwargs):
        events.append('prepare')
        return prepare(*args, **kwargs)
    monkeypatch.setattr(index, 'inspect_media', inspection)
    monkeypatch.setattr(index, 'prepare_download', processing)
    return events

def set_quota(context, *, quota=200*MB, usage=10*MB, supported=True, rejected=False):
    # WebKit's native estimate method ignores assignment. Replace the report
    # explicitly while keeping real, correctly bound OPFS/permission methods.
    result = "throw new Error('Owned fixture quota unavailable');" if rejected else f'return{{quota:{quota},usage:{usage}}};'
    directory = 'native?.getDirectory?.bind(native)' if supported else 'undefined'
    context.add_init_script(f"""(()=>{{
      const native=navigator.storage;
      window.__capacityReads=0;
      Object.defineProperty(navigator,'storage',{{configurable:true,value:{{
        estimate:async()=>{{window.__capacityReads++;{result}}},
        getDirectory:{directory},persist:native?.persist?.bind(native),persisted:native?.persisted?.bind(native)
      }}}});
    }})();""")

def source(page, origin):
    open_studio(page, origin)
    page.locator('#video-url').fill(URL)
    page.locator('#source-player[data-state="ready"]').wait_for(timeout=15000)

@pytest.mark.parametrize('width,theme', [(320, 'dark'), (1440, 'light')])
def test_insufficient_space_stops_before_processing_and_explicit_try_reuses_analysis(media_browser, media_server, monkeypatch, width, theme):
    origin, _ = media_server
    events = observe(monkeypatch, 7200)
    with media_browser.new_context(viewport={'width':width,'height':900}, reduced_motion='reduce') as context:
        set_quota(context, quota=48*MB, usage=4*MB)
        page = context.new_page(); source(page, origin)
        page.locator('#tab-personalization').click(); page.locator('#preference-theme').select_option(theme); page.locator('#tab-download').click()
        page.locator('#download-button').click()
        try:
            page.wait_for_function("document.getElementById('space-check').dataset.state==='insufficient' && !document.getElementById('download-button').disabled")
        except Exception:
            report = page.evaluate("""async()=>({capacity:await OndaDownloadStorage.capacity(),quota:await navigator.storage.estimate(),state:document.getElementById('space-check').dataset.state,title:document.getElementById('status-title').textContent,size:document.getElementById('estimate-value').textContent,estimate:document.getElementById('estimate-detail').textContent,reads:window.__capacityReads})""")
            pytest.fail(f'Owned quota scenario failed: {report}; calls={events}')
        assert events == ['inspect']
        assert page.locator('#space-check-actions').is_visible()
        assert page.locator('#space-check').evaluate('el=>el===document.activeElement')
        page.locator('.space-check-details>summary').click()
        assert '44 MB' in page.locator('#space-check-detail').inner_text()
        assert 'não mede o espaço físico' in page.locator('#space-check-detail').inner_text()
        assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
        # Test the newly visible state with the same pinned checker as the CI.
        import os
        from pathlib import Path
        checker = Path(os.environ.get('ONDA_AXE_PATH', 'artifacts/experience/axe/node_modules/axe-core/axe.min.js'))
        if checker.is_file():
            page.add_script_tag(path=str(checker))
            violations = page.evaluate("async()=> (await axe.run({exclude:[['iframe']]},{runOnly:{type:'tag',values:['wcag2a','wcag2aa','wcag21aa','wcag22aa']}})).violations.map(v=>({id:v.id,nodes:v.nodes.map(n=>n.target)}))")
            assert not violations, violations
        page.locator('#space-continue').click()
        page.locator('#save-dialog[open]').wait_for(timeout=30000)
        assert events == ['inspect', 'prepare']
        assert page.evaluate('window.__capacityReads') >= 2
        assert page.locator('#space-check-actions').is_hidden()

def test_ambiguous_upper_estimate_can_be_resolved_by_lower_quality(media_browser, media_server, monkeypatch):
    origin, _ = media_server
    events = observe(monkeypatch, 3000)
    with media_browser.new_context(reduced_motion='reduce') as context:
        set_quota(context, quota=140*MB, usage=10*MB)
        page = context.new_page(); source(page, origin)
        page.locator('#download-button').click()
        page.wait_for_function("document.getElementById('space-check').dataset.state==='risk' && !document.getElementById('download-button').disabled")
        assert events == ['inspect']
        page.locator('#space-review-quality').click()
        assert page.locator('input[name=quality]:checked').evaluate('el=>el===document.activeElement')
        page.locator('.quality-chips label').filter(has=page.locator('input[value="128"]')).click()
        assert page.locator('#space-check-actions').is_hidden()
        assert page.locator('#space-check').get_attribute('data-state') == 'available'
        page.locator('#download-button').click(); page.locator('#save-dialog[open]').wait_for(timeout=30000)
        assert events == ['inspect', 'prepare']

@pytest.mark.parametrize('capacity_kind', ['unknown', 'exhausted', 'unsupported'])
def test_unavailable_quota_or_small_memory_fallback_preserves_working_download(media_browser, media_server, monkeypatch, capacity_kind):
    origin, _ = media_server
    events = observe(monkeypatch, 60)
    with media_browser.new_context(reduced_motion='reduce') as context:
        set_quota(context, quota=0, usage=0, supported=capacity_kind!='unsupported', rejected=capacity_kind=='unknown')
        page = context.new_page(); source(page, origin)
        page.locator('#download-button').click(); page.locator('#save-dialog[open]').wait_for(timeout=30000)
        assert events == ['inspect', 'prepare']
        expected = 'unknown' if capacity_kind == 'unknown' else 'available'
        assert page.locator('#space-check').get_attribute('data-state') == expected
        assert page.locator('#space-check-actions').is_hidden()

def test_unknown_output_size_does_not_block_based_on_original_file(media_browser, media_server, monkeypatch):
    origin, _ = media_server
    events = observe(monkeypatch, None)
    with media_browser.new_context(reduced_motion='reduce') as context:
        set_quota(context, quota=0, usage=0)
        page = context.new_page(); source(page, origin)
        page.locator('#download-button').click(); page.locator('#save-dialog[open]').wait_for(timeout=30000)
        assert events == ['inspect', 'prepare']
        assert page.locator('#space-check').get_attribute('data-state') == 'unknown'
        assert 'Tamanho final desconhecido' in page.locator('#space-check-title').inner_text()

def test_cancelled_preflight_never_starts_processing(media_browser, media_server, monkeypatch):
    origin, _ = media_server
    entered = threading.Event(); release = threading.Event(); events = []
    monkeypatch.setattr(index, 'cached_metadata', lambda *args: None)
    def inspection(*args, **kwargs):
        entered.set(); release.wait(10)
        return {'title':'Fonte própria', 'duration':60, 'formats':[]}
    def processing(*args, **kwargs):
        events.append('prepare'); raise AssertionError('Cancelled work must not start')
    monkeypatch.setattr(index, 'inspect_media', inspection)
    monkeypatch.setattr(index, 'prepare_download', processing)
    with media_browser.new_context(reduced_motion='reduce') as context:
        set_quota(context)
        page = context.new_page(); source(page, origin); page.locator('#download-button').click()
        try:
            assert entered.wait(5)
            page.locator('#cancel-button').click()
            release.set()
            page.wait_for_function("!document.getElementById('download-button').disabled")
            assert not events
            assert page.locator('#space-check-actions').is_hidden()
            assert not page.locator('#save-dialog').evaluate('el=>el.open')
        finally: release.set()

def test_optional_inspection_failure_does_not_break_existing_download(media_browser, media_server, monkeypatch):
    origin, _ = media_server
    monkeypatch.setattr(index, 'cached_metadata', lambda *args: None)
    def fail(*args, **kwargs): raise security.AudioError('Fonte própria não forneceu metadados.', 'upstream_timeout', 504)
    monkeypatch.setattr(index, 'inspect_media', fail)
    with media_browser.new_context(reduced_motion='reduce') as context:
        set_quota(context)
        page = context.new_page(); source(page, origin)
        page.locator('#download-button').click(); page.locator('#save-dialog[open]').wait_for(timeout=30000)
        assert page.locator('#space-check').get_attribute('data-state') == 'unknown'

def test_analysis_reused_only_for_same_source_session_and_media_type(media_browser, media_server, monkeypatch):
    origin, _ = media_server
    events = observe(monkeypatch, 7200)
    with media_browser.new_context(reduced_motion='reduce') as context:
        set_quota(context, quota=0, usage=0)
        page = context.new_page(); source(page, origin)
        page.locator('#inspect-button').click()
        page.wait_for_function("!document.getElementById('inspect-button').disabled")
        page.locator('#download-button').click()
        page.wait_for_function("!document.getElementById('space-check-actions').hidden && !document.getElementById('download-button').disabled")
        assert events == ['inspect']
        page.locator('#advanced-options>summary').click()
        page.locator('#session-user-agent').fill('OwnedFixture/2')
        assert page.locator('#space-check-actions').is_hidden()
        page.locator('#download-button').click()
        page.wait_for_function("!document.getElementById('space-check-actions').hidden && !document.getElementById('download-button').disabled")
        assert events == ['inspect', 'inspect']
