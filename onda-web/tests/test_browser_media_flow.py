"""Browser -> real local HTTP -> authenticated fixture API -> FFmpeg -> save.

The Clerk identity and Blob transport are explicit test doubles. The download
stream travels over HTTP, not a browser route mock. No live account is created.
"""
import functools
from http.server import SimpleHTTPRequestHandler,ThreadingHTTPServer
import json
import mimetypes
import os
from pathlib import Path
import threading
from urllib.parse import urlsplit,parse_qs
import pytest
from fastapi.testclient import TestClient
from yt_dlp.networking import Response
from api import account,auth,download_history,engine,index,security

ROOT=Path(__file__).resolve().parents[1]
SDK="""window.__internal_ClerkUICtor=class {};window.Clerk={user:{id:'user_fixture',firstName:'Teste',imageUrl:''},session:{id:'sess_fixture',getToken:async()=> 'fixture_token'},load:async()=>{},addListener:()=>()=>{},mountUserButton:el=>{el.textContent='Conta';},unmountUserButton:()=>{},mountSignIn:()=>{},unmountSignIn:()=>{},mountSignUp:()=>{},unmountSignUp:()=>{}};"""

@pytest.fixture(scope='module',params=os.environ.get('ONDA_TEST_BROWSERS','chromium').split(','))
def media_browser(request):
    playwright=pytest.importorskip('playwright.sync_api')
    with playwright.sync_playwright() as p:
        try:browser=getattr(p,request.param).launch()
        except playwright.Error:
            if os.environ.get('ONDA_TEST_BROWSERS'):raise
            pytest.skip('Browser engine not installed')
        yield browser
        browser.close()

@pytest.fixture
def media_server(monkeypatch):
    files={}
    def blob(method,url,*,headers,body=None,**kwargs):
        parsed=urlsplit(url)
        path=parse_qs(parsed.query)['pathname'][0] if method=='PUT' else parsed.path.lstrip('/')
        if method=='GET':
            if path not in files:return 404,{},b''
            data,etag=files[path];return 200,{'ETag':etag},data
        assert headers['x-vercel-blob-access']=='private'
        old=files.get(path)
        if old and headers.get('x-if-match')!=old[1]:return 412,{},b''
        import hashlib
        files[path]=body,'"'+hashlib.sha256(body).hexdigest()+'"'
        return 201,{},b'{}'
    credentials=account.BlobCredentials('fixturestore','fixture_not_a_live_credential')
    monkeypatch.setattr(account,'blob_http',blob)
    monkeypatch.setattr(download_history,'blob_http',blob)
    monkeypatch.setattr(account.BlobCredentials,'from_environment',classmethod(lambda cls:credentials))
    class Transport:
        def __init__(self,guard,**kwargs):self.guard=guard
        def __enter__(self):return self
        def __exit__(self,*args):pass
        def send(self,request):
            name=Path(urlsplit(request.url).path).name
            if name not in {'canary.wav','canary.mp4'}:raise ValueError('owned fixture required')
            path=ROOT/'public'/name
            response=Response(path.open('rb'),request.url,{'Content-Type':mimetypes.guess_type(str(path))[0] or 'video/mp4','Content-Length':str(path.stat().st_size),'ETag':'"owned-fixture"'},200)
            return security.BoundedResponse(response,self.guard)
    monkeypatch.setattr(engine,'direct_handler',Transport)
    client=TestClient(index.app)
    class Handler(SimpleHTTPRequestHandler):
        def log_message(self,*args):pass
        def api(self):
            size=int(self.headers.get('Content-Length',0))
            body=self.rfile.read(size) if size else None
            response=client.request(self.command,self.path,headers={k:v for k,v in self.headers.items() if k.lower() not in {'host','content-length'}},content=body)
            self.send_response(response.status_code)
            for key,value in response.headers.items():
                if key.lower() not in {'content-length','transfer-encoding','content-encoding'}:self.send_header(key,value)
            self.send_header('Content-Length',str(len(response.content)));self.end_headers();self.wfile.write(response.content)
        def do_GET(self):
            if self.path.startswith('/api/'):self.api()
            else:super().do_GET()
        def do_POST(self):self.api()
        def do_PUT(self):self.api()
    server=ThreadingHTTPServer(('127.0.0.1',0),functools.partial(Handler,directory=str(ROOT/'public')))
    origin=f'http://127.0.0.1:{server.server_port}'
    monkeypatch.setenv('CLERK_ALLOWED_ORIGINS',origin)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    try:yield origin,files
    finally:server.shutdown();server.server_close();thread.join(5);client.close()

@pytest.mark.parametrize('width,media',[(1440,'video'),(390,'audio')])
def test_owned_audio_video_download_saves_and_history_appears(media_browser,media_server,tmp_path,width,media):
    origin,files=media_server
    with media_browser.new_context(viewport={'width':width,'height':900},accept_downloads=True,has_touch=width<500) as context:
        page=context.new_page();errors=[];page.on('pageerror',lambda error:errors.append(str(error)))
        page.route('https://*.clerk.accounts.dev/**',lambda route:route.fulfill(status=200,content_type='text/javascript',body=SDK))
        page.goto(origin,wait_until='domcontentloaded')
        page.wait_for_function("() => document.body.dataset.account==='signed-in'")
        if page.locator('.intro-dialog[open]').count():page.locator('[data-intro-skip]').click()
        page.wait_for_function("() => !document.body.classList.contains('intro-open')")
        page.evaluate("() => {window.showSaveFilePicker=undefined;}")
        if media=='video':page.locator('label.media-type-choice').filter(has=page.locator('input[value="video"]')).click()
        page.locator('#video-url').fill(f'https://ondaittoux.online/canary.{"mp4" if media=="video" else "wav"}')
        # Wait for the automatic preview to settle before clicking the control
        # below it; its entrance changes the button's vertical position.
        page.locator('#source-player[data-state="ready"]').wait_for(timeout=15000)
        page.locator('#download-button').click()
        try:page.locator('#save-dialog[open]').wait_for(timeout=30000)
        except Exception:
            state=page.evaluate("() => ({title:document.getElementById('status-title')?.textContent,message:document.getElementById('status-message')?.textContent,dialogs:[...document.querySelectorAll('dialog')].map(d=>({open:d.open,kind:d.className})),account:document.body.dataset.account,url:document.getElementById('video-url').value})")
            pytest.fail('Owned fixture flow did not finish: '+json.dumps(state)+' errors='+str(errors))
        box=page.locator('#save-button').bounding_box()
        assert abs(box['x']+box['width']/2-width/2)<5
        with page.expect_download() as download:page.locator('#save-button').click()
        target=tmp_path/download.value.suggested_filename;download.value.save_as(target)
        assert target.stat().st_size>1000
        page.wait_for_function("() => !document.getElementById('save-dialog').open")
        page.locator('#tab-performance').click()
        page.locator('.performance-table').wait_for(timeout=10000)
        assert 'Concluído' in page.locator('.performance-table').inner_text()
        assert page.evaluate('() => document.documentElement.scrollWidth <= innerWidth')
        # Tabs must show their content at the top, with all sections retained
        # inside the desktop workspace rather than below the sidebar.
        page.evaluate('() => scrollTo(0, document.documentElement.scrollHeight)')
        page.locator('#tab-personalization').click()
        page.wait_for_function('() => scrollY === 0')
        assert page.locator('#panel-personalization').evaluate("p => p.parentElement.matches('main.workspace-content')")
        assert page.locator('#panel-personalization h1').is_visible()
        assert page.locator('#panel-personalization h1').bounding_box()['y'] < 250
        assert not errors
        metrics=[json.loads(data) for path,(data,_) in files.items() if path.startswith('metrics/v1/')]
        assert len(metrics)==1 and metrics[0]['records'][0]['status']=='ready'
        assert metrics[0]['records'][0]['outputBytes']==target.stat().st_size
