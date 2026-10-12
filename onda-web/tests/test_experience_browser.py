"""Owned HTTP flows for actionable errors, stage timing, keyboard and public cache."""
import json
from pathlib import Path
import pytest
from api import index,security
from test_browser_media_flow import media_browser,media_server,SDK

ROOT=Path(__file__).resolve().parents[1]

def open_studio(page,origin):
    page.route('https://*.clerk.accounts.dev/**',lambda route:route.fulfill(status=200,content_type='text/javascript',body=SDK))
    page.goto(origin)
    page.wait_for_function("() => document.body.dataset.account === 'signed-in'")
    if page.locator('.intro-dialog[open]').count():page.locator('[data-intro-skip]').click()
    page.wait_for_function("() => !document.body.classList.contains('intro-open')")


@pytest.mark.parametrize('theme',['dark','light'])
@pytest.mark.parametrize('code,action,target',[
    ('cookies_expired','cookies','cookies-input'),
    ('format_unavailable','format',None),
    ('output_too_large','trim','trim-start'),
    ('busy','retry',None),
    ('drm','link','video-url'),
])
def test_source_errors_offer_safe_effective_actions(media_browser,media_server,monkeypatch,code,action,target,theme):
    origin,_=media_server
    def fail(*args,**kwargs):raise security.AudioError('Falha da fonte simulada para um arquivo próprio.',code,422)
    monkeypatch.setattr(index,'prepare_download',fail)
    with media_browser.new_context(viewport={'width':390,'height':900},reduced_motion='reduce') as context:
        page=context.new_page();open_studio(page,origin)
        page.locator('#tab-personalization').click();page.locator('#preference-theme').select_option(theme);page.locator('#tab-download').click()
        page.locator('#video-url').fill('https://ondaittoux.online/canary.wav')
        page.locator('#source-player[data-state="ready"]').wait_for(timeout=15000)
        page.locator('#download-button').click()
        button=page.locator(f'[data-error-action="{action}"]');button.wait_for()
        assert page.locator('#status-announcement').get_attribute('role')=='alert'
        import os
        configured=os.environ.get('ONDA_AXE_PATH')
        checker=Path(configured) if configured else ROOT/'artifacts/experience/axe/node_modules/axe-core/axe.min.js'
        if checker.is_file():
            page.add_script_tag(path=str(checker))
            violations=page.evaluate("async()=> (await axe.run({exclude:[['iframe']]},{runOnly:{type:'tag',values:['wcag2a','wcag2aa','wcag21aa','wcag22aa']}})).violations.map(v=>({id:v.id,nodes:v.nodes.map(n=>({target:n.target,summary:n.failureSummary}))}))")
            assert not violations,(theme,code,violations)
        if action=='retry':
            assert button.is_disabled();button.wait_for()
            page.wait_for_function("() => !document.querySelector('[data-error-action=retry]').disabled")
        else:
            assert page.locator('[data-error-action=retry]').count()==0
            button.click()
            if target:assert page.locator('#'+target).evaluate('el=>el===document.activeElement')
            if action=='cookies':assert page.locator('#advanced-options').get_attribute('open') is not None
            if action=='trim':assert page.locator('#media-tools').get_attribute('open') is not None
            if action=='format':assert page.locator('input[name=format]:checked').evaluate('el=>el===document.activeElement')
        assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
        page.locator('#video-url').fill('https://ondaittoux.online/canary.mp4')
        assert page.locator('#error-actions').is_hidden()


def test_stage_times_and_keyboard_work_with_real_owned_download(media_browser,media_server):
    origin,files=media_server
    with media_browser.new_context(viewport={'width':320,'height':900},reduced_motion='reduce') as context:
        page=context.new_page();open_studio(page,origin)
        page.locator('#video-url').fill('https://ondaittoux.online/canary.wav')
        page.locator('#source-player[data-state="ready"]').wait_for(timeout=15000)
        page.locator('#download-button').click()
        page.locator('#save-dialog[open]').wait_for(timeout=30000)
        page.keyboard.press('Escape')
        page.locator('#download-stage-times>summary').click()
        assert page.locator('[data-stage-time=converting]').inner_text().endswith(' s')
        assert page.locator('[data-stage-time=delivering]').inner_text().endswith(' s')
        rows=[json.loads(raw)['records'][0] for path,(raw,_) in files.items() if path.startswith('metrics/v1/')]
        assert len(rows)==1
        assert rows[0]['stageSeconds']['converting']>0
        assert sum(rows[0]['stageSeconds'].values()) <= rows[0]['elapsedSeconds']+.02
        page.locator('#tab-download').focus();page.keyboard.press('ArrowRight')
        assert page.locator('#tab-library').get_attribute('aria-selected')=='true'
        assert page.locator('#tab-library').evaluate('el=>el===document.activeElement')
        page.locator('#tab-help').click()
        assert page.locator('#app-install-title').is_visible()
        assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')


def test_worker_caches_public_shell_and_offline_page_only(media_browser,media_server):
    origin,files=media_server
    with media_browser.new_context() as context:
        page=context.new_page();open_studio(page,origin)
        page.wait_for_function('() => Boolean(navigator.serviceWorker.controller)',timeout=30000)
        cached=page.evaluate("""async()=>{const names=(await caches.keys()).filter(n=>n.startsWith('onda-shell-'));const cache=await caches.open(names[0]);return(await cache.keys()).map(r=>new URL(r.url).pathname);} """)
        assert '/index.html' in cached and '/offline.html' in cached and '/manifest.webmanifest' in cached
        assert not any(path.startswith(('/api/','/download/')) or path.endswith(('.mp4','.wav')) or path=='/autocura.json' for path in cached)
        assert 20 <= len(cached) <= 64
        if media_browser.browser_type.name=='chromium':
            session=context.new_cdp_session(page)
            manifest=session.send('Page.getAppManifest')
            assert manifest['url']==origin+'/manifest.webmanifest'
            assert not manifest.get('errors'),manifest.get('errors')
            installable=session.send('Page.getInstallabilityErrors')
            assert not installable.get('installabilityErrors'),installable
        await_ready=page.evaluate('() => Boolean(navigator.serviceWorker.controller)');assert await_ready
        page.unroute_all()
        # Stop the actual owned HTTP origin: this also tests Wi-Fi/network
        # failures while navigator.onLine still reports true. Playwright
        # protocol-level offline mode bypasses workers in Firefox/WebKit.
        files.disconnect()
        page.goto(origin+'/?tab=download')
        assert page.locator('h1').inner_text()=='Vamos reconectar.'
        assert page.locator('#offline-retry').is_visible()
        assert page.locator('#onda-workspace').count()==0


def test_available_app_update_waits_until_owned_file_is_saved(media_browser,media_server,tmp_path):
    origin,_=media_server
    with media_browser.new_context(accept_downloads=True) as context:
        context.add_init_script("""window.__activatedUpdates=0;Object.defineProperty(navigator,'serviceWorker',{value:{register:async()=>({waiting:{postMessage:()=>window.__activatedUpdates++},update:async()=>{},addEventListener:()=>{}}),addEventListener:()=>{}},configurable:true});""")
        page=context.new_page();open_studio(page,origin)
        page.wait_for_function("() => !document.getElementById('app-update-shortcut').hidden && !document.getElementById('app-update-notice').hidden")
        page.evaluate('window.showSaveFilePicker=undefined')
        page.locator('#video-url').fill('https://ondaittoux.online/canary.wav')
        page.locator('#source-player[data-state="ready"]').wait_for(timeout=15000)
        page.locator('#download-button').click();page.locator('#save-dialog[open]').wait_for(timeout=30000)
        assert page.locator('#app-update-shortcut').is_disabled()
        assert page.evaluate('window.__activatedUpdates')==0
        with page.expect_download() as download:page.locator('#save-button').click()
        download.value.save_as(tmp_path/'owned.mp3')
        page.wait_for_function("() => !document.getElementById('app-update-shortcut').disabled")
        page.locator('#app-update-shortcut').click()
        page.wait_for_function('window.__activatedUpdates===1')


@pytest.mark.parametrize('width,theme',[(320,'dark'),(320,'light'),(1440,'dark'),(1440,'light')])
def test_accessibility_has_no_automated_wcag_violations(media_browser,media_server,width,theme):
    import os
    configured=os.environ.get('ONDA_AXE_PATH')
    script=Path(configured) if configured else ROOT/'artifacts/experience/axe/node_modules/axe-core/axe.min.js'
    if not script.is_file():
        if configured:pytest.fail('Configured accessibility checker was not installed')
        pytest.skip('Axe is installed by the dedicated browser quality workflow')
    origin,_=media_server
    with media_browser.new_context(viewport={'width':width,'height':900},reduced_motion='reduce') as context:
        page=context.new_page();open_studio(page,origin);page.add_script_tag(path=str(script))
        page.locator('#tab-personalization').click();page.locator('#preference-theme').select_option(theme)
        for tab in ('download','compatibility','personalization','help'):
            page.locator('#tab-'+tab).click()
            if tab=='download':page.locator('#media-tools>summary').click();page.locator('#advanced-options>summary').click()
            violations=page.evaluate("""async()=>{const result=await axe.run({exclude:[['iframe']]},{runOnly:{type:'tag',values:['wcag2a','wcag2aa','wcag21aa','wcag22aa']}});return result.violations.map(v=>({id:v.id,nodes:v.nodes.map(n=>n.target)}));} """)
            assert not violations,(width,theme,tab,violations)
            assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
