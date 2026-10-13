"""Browser -> authenticated server queue -> independent worker -> FFmpeg -> ZIP."""
import io
import zipfile
import json
import pytest
from api import batch_notifications as push
from test_browser_media_flow import media_browser, media_server, SDK
from test_batches import batch_environment, run_steps

def open_studio(context,origin):
    page=context.new_page()
    page.goto(origin,wait_until='domcontentloaded')
    try:page.wait_for_function("() => document.body.dataset.account==='signed-in'")
    except Exception:
        import pytest
        pytest.fail('Account fixture did not reopen: '+page.locator('body').inner_text()[:1600])
    if page.locator('.intro-dialog[open]').count():page.locator('[data-intro-skip]').click()
    page.wait_for_function("() => !document.body.classList.contains('intro-open')")
    page.evaluate('() => {window.showSaveFilePicker=undefined;}')
    page.locator('#batch-center').wait_for(state='visible',timeout=10000)
    page.locator('#batch-center > summary').click()
    return page

def test_server_finishes_after_page_closes_and_returning_user_saves_zip(media_browser,media_server,batch_environment,tmp_path):
    origin,_=media_server;files,sent=batch_environment
    # WebKit's service-worker-controlled reload can bypass Playwright's Clerk
    # SDK route and load the real SDK for the fixture key. Offline/SW behavior
    # is covered separately; keep identity substitution stable across pages.
    with media_browser.new_context(viewport={'width':390,'height':844},accept_downloads=True,has_touch=True,reduced_motion='reduce',service_workers='block') as context:
        context.route('https://*.clerk.accounts.dev/**',lambda route:route.fulfill(status=200,content_type='text/javascript',body=SDK))
        page=open_studio(context,origin);errors=[];page.on('pageerror',lambda error:errors.append(str(error)))
        page.locator('#batch-links').fill('https://ondaittoux.online/canary.wav\nhttps://ondaittoux.online/canary.wav?copy=2')
        page.locator('#batch-add').click()
        page.wait_for_function("() => document.getElementById('batch-message').textContent.includes('Pacote aceito')")
        assert 'Na fila' in page.locator('#batch-list').inner_text()
        page.close()
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=1) as executor:executor.submit(run_steps,sent).result(timeout=90)
        page=open_studio(context,origin);page.on('pageerror',lambda error:errors.append(str(error)))
        page.get_by_role('button',name='Receber e salvar ZIP').wait_for(timeout=10000)
        assert 'ZIP disponível' in page.locator('#batch-list').inner_text()
        page.get_by_role('button',name='Receber e salvar ZIP').click()
        try:page.locator('#save-dialog[open]').wait_for(timeout=15000)
        except Exception:
            import pytest
            pytest.fail('ZIP reception did not finish: '+page.locator('#batch-message').inner_text()+' page errors: '+str(errors))
        assert 'ZIP' in page.locator('#save-file-detail').inner_text()
        box=page.locator('#save-button').bounding_box();assert abs(box['x']+box['width']/2-195)<5
        with page.expect_download() as download:page.locator('#save-button').click()
        target=tmp_path/download.value.suggested_filename;download.value.save_as(target)
        with zipfile.ZipFile(target) as archive:
            assert len(archive.namelist())==2 and archive.testzip() is None
        page.wait_for_function("() => !document.getElementById('save-dialog').open")
        assert page.evaluate('() => document.documentElement.scrollWidth<=innerWidth')
        page.get_by_role('button',name='Remover pacote').click()
        page.wait_for_function("() => document.getElementById('batch-list').textContent.includes('Sua fila está vazia')")
        assert not errors


@pytest.mark.parametrize('permission', ['granted', 'denied', 'unavailable', 'restore', 'rejected'])
def test_queue_ready_notifies_deduplicates_and_releases_on_logout(media_browser, media_server, batch_environment, monkeypatch, permission):
    origin, _ = media_server; _, sent = batch_environment; emitted = []
    async def send(_client, _subscription, payload, _ttl, _tag):
        emitted.append(json.loads(payload)); return 201
    monkeypatch.setattr(push, 'send_push', send)
    from test_batch_notifications import subscription
    script = '''(() => {
      const listeners = new Map(); let enrolled = null;
      window.__permissionRequests = 0; window.__unsubscribed = 0;
      Object.defineProperty(window, 'Notification', {configurable:true,value:{permission:MODE==='restore'?'granted':'default',requestPermission:async()=>{window.__permissionRequests++;Notification.permission=PERMISSION;return PERMISSION;}}});
      Object.defineProperty(window, 'PushManager', {configurable:true,value:class {}});
      const registration={active:{postMessage:(_data,ports)=>ports[0].postMessage({ok:true})},update:async()=>{},addEventListener:()=>{},getNotifications:async()=>[],pushManager:{getSubscription:async()=>['unavailable','restore'].includes(MODE)?await new Promise(()=>{}):enrolled,subscribe:async()=>{if(MODE==='rejected')throw new DOMException('Push service unavailable.','AbortError');enrolled={endpoint:SUBSCRIPTION.endpoint,toJSON:()=>SUBSCRIPTION,unsubscribe:async()=>{window.__unsubscribed++;enrolled=null;return true;}};return enrolled;}}};
      Object.defineProperty(navigator, 'serviceWorker', {configurable:true,value:{ready:Promise.resolve(registration),register:async()=>registration,addEventListener:(name,fn)=>{if(!listeners.has(name))listeners.set(name,new Set());listeners.get(name).add(fn);},removeEventListener:(name,fn)=>listeners.get(name)?.delete(fn)}});
      window.__deliverPush=data=>{for(const fn of listeners.get('message')||[])fn({data});};
      if(MODE==='restore'){const original=setTimeout;window.setTimeout=(fn,ms,...args)=>original(fn,ms===10000?150:ms,...args);}
    })();'''.replace('PERMISSION', json.dumps('granted' if permission in ('unavailable','restore','rejected') else permission)).replace('MODE', json.dumps(permission)).replace('SUBSCRIPTION', '(' + json.dumps(subscription()) + ')')
    with media_browser.new_context(viewport={'width':390,'height':844},reduced_motion='reduce',service_workers='block') as context:
        context.add_init_script(script)
        context.route('https://*.clerk.accounts.dev/**', lambda route: route.fulfill(status=200, content_type='text/javascript', body=SDK))
        page = open_studio(context, origin); errors = []; page.on('pageerror', lambda error: errors.append(str(error)))
        assert page.evaluate('window.__permissionRequests') == 0
        if permission == 'unavailable':
            page.evaluate("() => {const original=setTimeout;window.setTimeout=(fn,ms,...args)=>original(fn,ms===10000?150:ms,...args);}")
        page.locator('#batch-notifications-toggle').click()
        page.wait_for_function("() => !document.getElementById('batch-notifications-toggle').disabled")
        assert page.evaluate('window.__permissionRequests') == (0 if permission == 'restore' else 1)
        assert page.locator('#batch-notifications-toggle').get_attribute('aria-pressed') == str(permission == 'granted').lower()
        assert ('ativos' if permission == 'granted' else 'demorou' if permission in ('unavailable','restore') else 'serviço de avisos' if permission == 'rejected' else 'configurações') in page.locator('#batch-notifications-status').inner_text()
        page.locator('#batch-links').fill('https://ondaittoux.online/canary.wav')
        page.locator('#batch-add').click()
        page.wait_for_function("() => document.getElementById('batch-message').textContent.includes('Pacote aceito')")
        page.locator('#tab-library').click()
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=1) as executor: executor.submit(run_steps, sent).result(timeout=90)
        if permission == 'granted':
            assert len(emitted) == 1
            page.evaluate('data => window.__deliverPush(data)', emitted[0])
        else:
            assert not emitted
            page.evaluate("() => dispatchEvent(new CustomEvent('onda:batch-ready', {detail:{}}))")
        page.locator('#batch-ready-notice').wait_for(state='visible',timeout=10000)
        assert page.locator('#panel-library').is_visible()
        page.get_by_role('button',name='Ver arquivo',exact=True).click()
        page.get_by_role('button',name='Receber e salvar ZIP').wait_for()
        assert page.locator('#panel-download').is_visible()
        assert page.locator('#batch-ready-notice').is_hidden()
        page.locator('#batch-refresh').click()
        page.wait_for_function("() => document.getElementById('batch-list').textContent.includes('ZIP disponível')")
        assert page.locator('#batch-ready-notice').is_hidden()
        assert page.evaluate('() => document.documentElement.scrollWidth<=innerWidth')
        page.evaluate("() => { window.Clerk.session=null;dispatchEvent(new CustomEvent('onda:auth',{detail:{signedIn:false}})); }")
        if emitted:
            page.evaluate('data => window.__deliverPush({...data,type:"BATCH_NOTIFICATION_OPEN"})',emitted[0])
            page.wait_for_function('window.__unsubscribed===1')
        assert page.locator('#batch-ready-notice').count() == 0
        assert page.locator('#batch-list').inner_text() == ''
        assert not errors
