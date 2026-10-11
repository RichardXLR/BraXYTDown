// Real framed decoder + Node's disk-backed Blob. Not a browser compatibility test.
const fs = require('node:fs');
const fsp = require('node:fs/promises');
const vm = require('node:vm');
const crypto = require('node:crypto');
const path = require('node:path');
const os = require('node:os');
(async () => {
  const [source, sizeText, mode, destination] = process.argv.slice(2);
  const bytes = Number(sizeText) * 1024 ** 2;
  const context = { window: {}, Uint8Array, DataView, TextDecoder, Blob, DOMException, performance };
  vm.createContext(context); vm.runInContext(fs.readFileSync(source, 'utf8'), context);
  const jsonFrame = data => {
    const payload = Buffer.from(JSON.stringify(data)); const header = Buffer.alloc(5); header[0] = 1; header.writeUInt32BE(payload.length, 1); return Buffer.concat([header,payload]);
  };
  let stage = 0, count = 0;
  const body = new ReadableStream({ pull(controller) {
    if (stage === 0) { controller.enqueue(jsonFrame({ type:'file', name:'Own.wav', title:'Own', mime:'audio/wav', size:bytes })); stage=1; return; }
    if (stage === 1 && count < bytes) {
      const n=Math.min(65536,bytes-count); const chunk=Buffer.alloc(n+5);chunk[0]=2;chunk.writeUInt32BE(n,1);count+=n;controller.enqueue(chunk);return;
    }
    if (stage === 1) {controller.enqueue(jsonFrame({type:'complete',size:bytes}));stage=2;return;}
    controller.close();
  } });
  const directory = await fsp.mkdtemp(path.join(os.tmpdir(),'onda-decoder-'));
  const file=path.join(directory,'fixture'); let handle;
  global.gc?.(); const initial=process.memoryUsage();let peakRSS=initial.rss,peakBuffers=initial.arrayBuffers;
  const sample=()=>{const usage=process.memoryUsage();peakRSS=Math.max(peakRSS,usage.rss);peakBuffers=Math.max(peakBuffers,usage.arrayBuffers);};
  const sinkFactory=async metadata=>{
    handle=await fsp.open(file,'w');let written=0;
    return {kind:'disk',async write(chunk){await handle.write(chunk);written+=chunk.length;sample();},async commit(){await handle.close();return fs.openAsBlob(file,{type:metadata.mime});},async abort(){await handle?.close().catch(()=>{});}};
  };
  const started=performance.now();
  const result=await context.window.OndaDownloadStream.receive(new Response(body,{headers:{'Content-Type':'application/x-onda-download;version=1'}}),{maxBytes:bytes,onEvent:sample,...(mode==='disk'?{sinkFactory}:{})});
  sample();const elapsed=(performance.now()-started)/1000;
  const hash=crypto.createHash('sha256');for await(const chunk of result.blob.stream()){hash.update(chunk);sample();}
  const record={nominalMiB:Number(sizeText),mode,receivedBytes:result.blob.size,elapsedSeconds:elapsed,peakRSSDeltaMiB:(peakRSS-initial.rss)/1024**2,peakArrayBuffersDeltaMiB:(peakBuffers-initial.arrayBuffers)/1024**2,sha256:hash.digest('hex')};
  if (record.receivedBytes !== bytes) throw new Error('Wrong file size');
  await result.cleanup?.();await fsp.rm(directory,{recursive:true,force:true});fs.writeFileSync(destination,JSON.stringify(record,null,2)+'\n');console.log(JSON.stringify(record));
})().catch(error=>{console.error(error.message);process.exit(1);});
