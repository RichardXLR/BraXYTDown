"""Actual Chromium OPFS, protocol integrity, abort and cleanup checks."""
import functools
from http.server import ThreadingHTTPServer,SimpleHTTPRequestHandler
import json
from pathlib import Path
import threading
from playwright.sync_api import sync_playwright
root=Path(__file__).resolve().parents[1]
class Handler(SimpleHTTPRequestHandler):
    def log_message(self,*args):pass
    def do_GET(self):
        if self.path=='/benchmark.html':
            self.send_response(200);self.send_header('Content-Type','text/html');self.end_headers();self.wfile.write(b'<html><body>Onda storage verification</body></html>')
        else:super().do_GET()
server=ThreadingHTTPServer(('127.0.0.1',0),functools.partial(Handler,directory=str(root/'public')))
threading.Thread(target=server.serve_forever,daemon=True).start()
records=[]
with sync_playwright() as p:
    browser=p.chromium.launch(headless=True,args=['--no-sandbox'])
    page=browser.new_page();page.goto(f'http://127.0.0.1:{server.server_port}/benchmark.html')
    page.add_script_tag(url='/download-storage.js');page.add_script_tag(url='/download-stream.js')
    for size in (100,250,500):
        records.append(page.evaluate('''async size => {
          const bytes=size*1024**2, userId='owned-storage-benchmark';
          const json=value=>{const data=new TextEncoder().encode(JSON.stringify(value));const out=new Uint8Array(5+data.length);out[0]=1;new DataView(out.buffer).setUint32(1,data.length);out.set(data,5);return out;};
          let sent=0,stage=0;
          const body=new ReadableStream({pull(controller){
            if(stage===0){controller.enqueue(json({type:'file',name:'Own.wav',title:'Own',mime:'audio/wav',size:bytes}));stage=1;return;}
            if(sent<bytes){const n=Math.min(65536,bytes-sent);const out=new Uint8Array(5+n);out[0]=2;new DataView(out.buffer).setUint32(1,n);out[5]=(sent/65536)%251;sent+=n;controller.enqueue(out);return;}
            if(stage===1){controller.enqueue(json({type:'complete',size:bytes}));stage=2;return;}controller.close();
          }});
          const before=performance.now();const result=await OndaDownloadStream.receive(new Response(body,{headers:{'Content-Type':OndaDownloadStream.CONTENT_TYPE}}),{maxBytes:bytes,sinkFactory:metadata=>OndaDownloadStorage.create(metadata,{userId})});
          const receivedSeconds=(performance.now()-before)/1000;
          let position=0;const reader=result.blob.stream().getReader();
          while(true){const {value,done}=await reader.read();if(done)break;for(let i=0;i<value.length;i++){const absolute=position+i;const expected=absolute%65536===0?(absolute/65536)%251:0;if(value[i]!==expected)throw new Error('Integrity mismatch');}position+=value.length;}
          await result.cleanup();
          if(result.storageKind!=='disk'||position!==bytes)throw new Error('OPFS output mismatch');
          return {nominalMiB:size,receivedBytes:position,storageKind:result.storageKind,fileBacked:result.blob instanceof File,integrityVerified:true,receivedSeconds};
        }''',size))
    failures=page.evaluate('''async()=>{
      const userId='owned-storage-failure';const metadata={size:32,mime:'audio/wav'};
      const sink=await OndaDownloadStorage.create(metadata,{userId});await sink.write(new Uint8Array(16));let incomplete=false;
      try{await sink.commit();}catch(_){incomplete=true;}await sink.abort();
      let current=true;const scope=()=>{if(!current)throw new DOMException('Account changed','AbortError');};
      const stale=await OndaDownloadStorage.create(metadata,{userId,ensureOperation:scope});await stale.write(new Uint8Array(32));current=false;let rejected=false;
      try{await stale.commit();}catch(_){rejected=true;}await stale.abort();
      const account=Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256',new TextEncoder().encode(userId)))).map(v=>v.toString(16).padStart(2,'0')).join('');
      const directory=await(await(await navigator.storage.getDirectory()).getDirectoryHandle('onda-downloads')).getDirectoryHandle(account);
      let remaining=0;for await(const _ of directory.entries())remaining++;
      if(!incomplete||!rejected||remaining)throw new Error('Cleanup or account isolation failed');
      return {incompleteRejected:incomplete,accountChangeRejected:rejected,temporaryFilesRemaining:remaining};
    }''')
    browser.close()
server.shutdown()
result={'browser':'Chromium','files':records,'failureChecks':failures}
(root/'artifacts/modernization/browser-storage.json').write_text(json.dumps(result,indent=2)+'\n')
print(json.dumps(result))
