'use strict';
const assert = require('node:assert/strict'), fs = require('node:fs'), vm = require('node:vm'), path = require('node:path');
const { webcrypto } = require('node:crypto');
const publicRoot = path.resolve(__dirname, '../public');
const source = fs.readFileSync(path.join(publicRoot, 'app-worker.js'), 'utf8');
function environment({ tamper = false } = {}) {
  const listeners = {}, stores = new Map(), fetched = [];
  const cache = name => { if (!stores.has(name)) stores.set(name, new Map()); const map = stores.get(name); return { put: async (key,response) => map.set(String(key), response.clone()), match: async key => map.get(String(key))?.clone() }; };
  stores.set('onda-shell-last-approved', new Map([['/index.html',new Response('approved')]]));
  stores.set('unrelated-public-cache', new Map());
  const context = { URL, Request, Response, AbortController, setTimeout, clearTimeout, crypto: webcrypto, Uint8Array, Map, Set, Promise,
    caches: { open: async name => cache(name), keys: async () => [...stores.keys()], delete: async name => stores.delete(name) },
    self: { location: { origin: 'https://owned.test' }, navigator: { onLine: true }, clients: { claim: async () => {} }, skipWaiting: () => { context.activated = true; }, addEventListener: (name,callback) => { listeners[name] = callback; } },
    fetch: async (url,options) => { const pathname = new URL(String(url)).pathname; fetched.push({ pathname, options }); const data = fs.readFileSync(path.join(publicRoot, pathname)); return new Response(tamper && pathname.endsWith('/app.js') ? 'tampered' : data); },
  };
  vm.createContext(context); vm.runInContext(source,context);
  return { context,listeners,stores,fetched };
}
async function main() {
  const env = environment();let install;
  env.listeners.install({ waitUntil: value => { install = value; } });await install;
  assert(env.fetched.length > 20);
  assert(env.fetched.every(({ pathname,options }) => !pathname.startsWith('/api/') && !/\.mp4$|\.wav$|autocura/.test(pathname) && options.credentials === 'omit'));
  for (const url of ['https://owned.test/api/account','https://owned.test/api/downloads/history','https://owned.test/download/jobs/owned?signature=fixture','https://owned.test/canary.mp4','https://owned.test/app.js?private=fixture','https://clerk.owned.test/account.js']) {
    let intercepted = false;env.listeners.fetch({ request:new Request(url), respondWith:()=>{intercepted=true;} });assert.equal(intercepted,false,url);
  }
  for (const options of [{method:'POST',body:'fixture'}, {headers:{Authorization:'Bearer fixture'}}, {cache:'no-store'}]) {
    let intercepted=false;env.listeners.fetch({request:new Request('https://owned.test/app.js',options),respondWith:()=>{intercepted=true;}});assert.equal(intercepted,false);
  }
  let intercepted=false;env.listeners.fetch({request:{url:'https://owned.test/?code=temporary&state=fixture',method:'GET',mode:'navigate',headers:new Headers(),cache:'default'},respondWith:()=>{intercepted=true;}});assert.equal(intercepted,false);
  env.context.self.navigator.onLine=false;let offline;
  env.listeners.fetch({request:{url:'https://owned.test/?tab=download',method:'GET',mode:'navigate',headers:new Headers(),cache:'default'},respondWith:value=>{offline=value;}});
  assert((await (await offline).text()).includes('Vamos reconectar.'));
  env.context.self.navigator.onLine=true;
  env.context.fetch=async()=>{throw new TypeError('Owned origin disconnected');};
  let unreachable;env.listeners.fetch({request:{url:'https://owned.test/',method:'GET',mode:'navigate',headers:new Headers(),cache:'default'},respondWith:value=>{unreachable=value;}});
  assert((await (await unreachable).text()).includes('Vamos reconectar.'));
  env.listeners.message({data:{type:'ACTIVATE_UPDATE'},source:{url:'https://untrusted.test'}});assert(!env.context.activated);
  env.listeners.message({data:{type:'ACTIVATE_UPDATE'},source:{url:'https://owned.test'}});assert(env.context.activated);
  let activate;env.listeners.activate({waitUntil:value=>{activate=value;}});await activate;
  assert(env.stores.has('unrelated-public-cache'));assert(!env.stores.has('onda-shell-last-approved'));
  const bad=environment({tamper:true});let failure;bad.listeners.install({waitUntil:value=>{failure=value;}});
  await assert.rejects(failure,/release changed/);assert(bad.stores.has('onda-shell-last-approved'));
  assert.equal([...bad.stores.keys()].filter(key=>key.startsWith('onda-shell-')).length,1);
}
main().catch(error=>{console.error(error);process.exitCode=1;});
