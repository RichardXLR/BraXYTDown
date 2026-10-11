"""Real browser engines; mobile viewports are emulation, not physical devices."""
import functools
from http.server import SimpleHTTPRequestHandler,ThreadingHTTPServer
import os
from pathlib import Path
import threading
import pytest

ROOT=Path(__file__).resolve().parents[1]

@pytest.fixture(scope='module',params=os.environ.get('ONDA_TEST_BROWSERS','chromium').split(','))
def history_browser(request):
    playwright=pytest.importorskip('playwright.sync_api')
    class Handler(SimpleHTTPRequestHandler):
        def log_message(self,*args):pass
        def do_GET(self):
            if self.path=='/fixture':
                content=b'''<html><head><meta name="viewport" content="width=device-width,initial-scale=1"><style>*{box-sizing:border-box}body{margin:0;padding:16px;font-family:Arial;background:#081627;color:#fff}:root{--text:#fff;--muted:#9eafc4;--line:#24394f;--lime:#5aa9ff}.studio-card{border:1px solid #24394f;border-radius:18px}.sr-only{position:absolute;clip:rect(0,0,0,0);height:1px;width:1px}</style><link rel="stylesheet" href="/download-history.css"></head><body><button id="refresh-download-history">Atualizar</button><div id="download-history-dashboard"></div><script src="/download-history.js"></script><script src="/download-storage.js"></script><script src="/download-stream.js"></script></body></html>'''
                self.send_response(200);self.send_header('Content-Type','text/html');self.end_headers();self.wfile.write(content)
            else:super().do_GET()
    server=ThreadingHTTPServer(('127.0.0.1',0),functools.partial(Handler,directory=str(ROOT/'public')))
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    try:
        with playwright.sync_playwright() as p:
            try:browser=getattr(p,request.param).launch()
            except playwright.Error:
                if os.environ.get('ONDA_TEST_BROWSERS'):raise
                pytest.skip('Browser engine not installed')
            yield browser,f'http://127.0.0.1:{server.server_port}'
            browser.close()
    finally:server.shutdown();server.server_close();thread.join(5)

@pytest.mark.parametrize('width',[320,390,768,1440])
def test_history_is_responsive_and_renders_only_actual_numeric_metrics(history_browser,width):
    browser,origin=history_browser
    with browser.new_context(viewport={'width':width,'height':900}) as context:
        page=context.new_page();page.goto(origin+'/fixture')
        page.evaluate("""() => OndaDownloadHistory.render([{id:'fixture',at:Date.now()/1000,mode:'worker',format:'mp4',status:'ready',elapsedSeconds:10,transferredBytes:104857600,outputBytes:1000,processCpuSeconds:2,processPeakRssBytes:52428800}])""")
        assert page.locator('.performance-stat strong').all_inner_texts()==['1','1','0','100 MiB']
        assert '10 MiB/s' in page.locator('.performance-table').inner_text()
        assert page.evaluate('() => document.documentElement.scrollWidth <= innerWidth')
        assert page.locator('svg[role=img]').count()==2
        page.evaluate('() => OndaDownloadHistory.render([])')
        assert page.locator('.performance-empty').is_visible()

def test_history_remote_labels_cannot_execute_markup(history_browser):
    browser,origin=history_browser
    with browser.new_context() as context:
        page=context.new_page();page.goto(origin+'/fixture')
        page.evaluate("""() => OndaDownloadHistory.render([{id:'fixture',at:Date.now()/1000,mode:'inline',format:'<img src=x onerror=window.attacked=true>',status:'failed',code:'<script>window.attacked=true</script>'}])""")
        assert page.locator('#download-history-dashboard img, #download-history-dashboard script').count()==0
        assert page.evaluate('() => Boolean(window.attacked)') is False

def test_download_storage_protocol_integrity_and_cleanup_across_engines(history_browser):
    browser,origin=history_browser
    with browser.new_context() as context:
        page=context.new_page();page.goto(origin+'/fixture')
        result=page.evaluate("""async()=>{
          const size=4*1024*1024, userId='fixture-account';
          const json=v=>{const d=new TextEncoder().encode(JSON.stringify(v)),f=new Uint8Array(d.length+5);f[0]=1;new DataView(f.buffer).setUint32(1,d.length);f.set(d,5);return f;};
          let sent=0,stage=0;
          const body=new ReadableStream({pull(c){if(stage===0){stage++;c.enqueue(json({type:'file',name:'Fixture.wav',title:'Own fixture',mime:'audio/wav',size}));return;}if(sent<size){const b=new Uint8Array(65541);b[0]=2;new DataView(b.buffer).setUint32(1,65536);b.fill(42,5);sent+=65536;c.enqueue(b);return;}if(stage===1){stage++;c.enqueue(json({type:'complete',size}));return;}c.close();}});
          const r=await OndaDownloadStream.receive(new Response(body,{headers:{'Content-Type':OndaDownloadStream.CONTENT_TYPE}}),{maxBytes:size,sinkFactory:m=>OndaDownloadStorage.create(m,{userId})});
          const data=new Uint8Array(await r.blob.arrayBuffer());if(data.length!==size||data.some(v=>v!==42))throw new Error('Integrity failure');await r.cleanup();
          const sink=await OndaDownloadStorage.create({size:64,mime:'audio/wav'},{userId});await sink.write(new Uint8Array(32));let rejected=false;try{await sink.commit();}catch{rejected=true;}await sink.abort();
          return {kind:r.storageKind,size,rejected};
        }""")
        assert result['size']==4*1024**2 and result['rejected']
        assert result['kind'] in ('disk','memory')
