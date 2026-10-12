'use strict';
// BEGIN GENERATED SHELL
const CACHE_VERSION = "cca141740acfdf9b1e20adcd";
const SHELL = {
  "/accessibility.css": "229712f0179e63e28cc1e496844b545f526cb99de03f3231589fac502920af23",
  "/account.css": "a538da08271e58b97006a624f78cfc366e469c4d3d213f522bb73481c707070a",
  "/account.js": "e04b506da1e6fbf950ab0e4fbfbd8221e28ad0bb665ab1929608ba9e10631e18",
  "/app-install.css": "897678bc9bfa7bcd335ba4f176975be4495d521c8f5de6ef3d34518455c45308",
  "/app-install.js": "b31d0b92752f6273fa481e7840cd7ad472d69717423de5953c9c4819974c700e",
  "/app.js": "07e4f9214a7513fe1bf9228f4bd54bd1655f15fe8965857e3ffb2fdee6e8b9e1",
  "/assets/fonts/manrope-latin-variable.woff2": "a30ddcd349703aff7464c34bef3fffdff405ee50c113440d7c8693c02d210972",
  "/assets/onda-app-icon.png": "c748266086fae5ae2e926aed95a8427fd87f6bd1cc3b753763ebe5eb982c7fe1",
  "/assets/onda-icon.png": "e72c4dbfc231aaf27f37912b0672868fabf4463136672061d7b3ef2ea0d93cc6",
  "/assets/onda-logo.webp": "f0013ebdf872190e3eaf0183dd71f188ff654f8c99aa669e52283462de3e22b1",
  "/atelier.css": "901178223c417a8db9896b2f46ae5a94b7e21e686ed97e5c7c032aa2274b7e8b",
  "/brand.css": "e77a5037d6129f2ac9002f2c302ef4c3023b57c778947a47746117a20d34ade5",
  "/clarity.css": "75567a476910527bea8f77a9353415488af3ed23fb3168a7732f83edf428d1ed",
  "/clerk-portuguese.js": "5213adeb2db8f7e455ef15afe48a48bf28888049bd0b29689b1cc0f711b2ca34",
  "/cookie-center.js": "a70899d064e6200273cb41d0ff58c956e94916aa20ef0bcc56a8731257d3e068",
  "/download-estimate.js": "8050c3c3c72e17d24c1437d7b897f0a207e24a304121c5d956b6f9a4a3e9b5b1",
  "/download-experience.css": "79a51a0943ae2f450d1b17bcaf89f7009a14e5b5dcd655984000341a53214eb8",
  "/download-history.css": "0065540e0b0223eae7670baef650cb9d1b5346eab172054ec36d83efa4f78487",
  "/download-history.js": "fbbb053004e624ea3abb11d1311cd81d94e4cbdc7a6bbfffd48f74a1fdb52745",
  "/download-storage.js": "52e0f26e2b4e0401ba7b19365a1053425da71d8252d83e2ab280577f378fa38e",
  "/download-stream.js": "17cf223f01f9f6c790f3ad0dfc8771b725902d00894edc294af2b529b5ed1c92",
  "/error-actions.js": "b46fa32f31d1f740253bbc71dcb98e51037be90267ce56e4f3c494ea278cab98",
  "/experience.css": "7e4e46c7e6485993d54f3fd33216f5d0d36ee1633b6c905bacebabef497e621f",
  "/identity.css": "b10140d7d94e45f31234790360e192fef6929963a17ff0fed5ed0345762e8ab0",
  "/index.html": "9f859298af794758c9dffc12b918c6955bbdc589bf4afd83702ae41061a2d2c7",
  "/intro.js": "9b884c1a72edb175488d60cb94ab1ea37840a7fac06429ab25d8b0d1fc514e95",
  "/maintenance-center.css": "4ead2df696c79cd904cadb753dfa1449e28163f5f008be4ad12c5273de27d7f2",
  "/maintenance-center.js": "e0ffde1297b744783cf312d22687fae4cde19f86e191c72758fd4091a155d49c",
  "/manifest.webmanifest": "1640f12276844205ac378a6b28fcbb05387b16c03e53e6ecb0314d1d0ef1c55c",
  "/offline.html": "7d8d2c607aa66dfefb726a1c6777849f43d73b4d0cf9d0b5890e57adbd1c9701",
  "/offline.js": "ec3b1dfa5dc287161fedaceefe5a03b8bf50c24a1cf6135d055972830e774bfa",
  "/player.js": "7ade9566ea8298b03a534f1471a475d7b7990007530481255f935bf5d63ee2f0",
  "/save.css": "7eed23f1f8658461d8f73f9dbcef0ccf28054e308db7417934aec13155339c8e",
  "/session-store.js": "bf15f12bb0d756e8349d45a85fb86c76a9bd7adfec65ca7544f4e2fb3573d64c",
  "/stage-times.js": "3839d460984ac550f15e536ba4d8010f2caa80ee2af47de60219f9e87f489537",
  "/styles.css": "4e16d1c30791db302780ba7a1ab957dc67c7c054a7b572ddbcb791b7fe8321da",
  "/ui.css": "65c14af70b75ab2e88f43cf80ba10f341044026e7521d09be25854dc3f437e38",
  "/ui.js": "c64bd9c65e69ac4a3b88c12d867ddab7e97b36e3d5d44b3691a5a10779e5b90a"
};
// END GENERATED SHELL
const PREFIX = 'onda-shell-';
const CACHE = PREFIX + CACHE_VERSION;
const origin = self.location.origin;
const knownPaths = new Set(Object.keys(SHELL));
function publicRequest(request) {
  const url = new URL(request.url);
  return request.method === 'GET' && url.origin === origin && !url.search
    && knownPaths.has(url.pathname) && !request.headers.has('Authorization')
    && request.cache !== 'no-store';
}
async function installShell() {
  const cache = await caches.open(CACHE);
  try {
    const entries = Object.entries(SHELL);
    for (let i = 0; i < entries.length; i += 6) {
      await Promise.all(entries.slice(i, i + 6).map(async ([path, expected]) => {
        const response = await fetch(new URL(path, origin), { credentials: 'omit', cache: 'reload', redirect: 'error' });
        if (!response.ok || response.type === 'opaque' || /(?:private|no-store)/i.test(response.headers.get('Cache-Control') || '')) throw new Error('Public shell unavailable');
        const bytes = await response.clone().arrayBuffer();
        const digest = [...new Uint8Array(await crypto.subtle.digest('SHA-256', bytes))].map(n => n.toString(16).padStart(2, '0')).join('');
        if (digest !== expected) throw new Error('Shell release changed while installing');
        await cache.put(path, response);
      }));
    }
  } catch (error) { await caches.delete(CACHE); throw error; }
}
self.addEventListener('install', event => { event.waitUntil(installShell()); });
self.addEventListener('activate', event => {
  event.waitUntil((async () => {
    for (const key of await caches.keys()) if (key.startsWith(PREFIX) && key !== CACHE) await caches.delete(key);
    await self.clients.claim();
  })());
});
self.addEventListener('message', event => {
  if (event.data?.type === 'ACTIVATE_UPDATE' && event.source?.url && new URL(event.source.url).origin === origin) self.skipWaiting();
});
self.addEventListener('fetch', event => {
  const request = event.request, url = new URL(request.url);
  // APIs, sessions, media, signed delivery links and third parties always use
  // their normal network/authentication path. Never write runtime responses.
  if (request.method !== 'GET' || url.origin !== origin || request.headers.has('Authorization') || request.cache === 'no-store') return;
  if (request.mode === 'navigate' && ['/', '/index.html'].includes(url.pathname)
      && [...url.searchParams.keys()].every(key => ['tab', 'media', 'account'].includes(key))) {
    event.respondWith((async () => {
      const cache = await caches.open(CACHE);
      if (self.navigator.onLine === false) return await cache.match('/offline.html') || fetch(request);
      // A device can report online even when its Wi-Fi has no internet. Probe
      // only a public HEAD, never an account endpoint or a personal response.
      const check = new AbortController();
      const timer = setTimeout(() => check.abort(), 2000);
      try {
        const response = await fetch(new URL('/index.html', origin), { method: 'HEAD', credentials: 'omit', cache: 'no-store', redirect: 'error', signal: check.signal });
        if (!response.ok) throw new Error('Public site unavailable');
        return await cache.match('/index.html') || fetch(request);
      } catch { return await cache.match('/offline.html') || fetch(request); }
      finally { clearTimeout(timer); }
    })());
    return;
  }
  if (publicRequest(request)) event.respondWith((async () => await (await caches.open(CACHE)).match(url.pathname) || fetch(request))());
});
