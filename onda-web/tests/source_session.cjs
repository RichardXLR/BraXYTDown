'use strict';
// Exercise the production helpers, with only DOM and vault I/O supplied by fixtures.
// This test does not contact Clerk, a platform, or any network endpoint.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const source = fs.readFileSync(path.resolve(__dirname, '../public/app.js'), 'utf8');
function between(start, end) {
  const first = source.indexOf(start), last = source.indexOf(end, first);
  assert(first >= 0 && last > first, `Production helper boundaries must exist: ${start}`);
  return source.slice(first, last);
}
const production = [
  between('  function describeCookies(', '  function updateCookieStatus('),
  between('  function persistCookies(', '  async function restoreSavedCookies('),
  between('  function accessOptions(', '  function updateCookieApplicability('),
  between('  function getCookies(', '  window.OndaSession ='),
  between('  function detectSource(', '  function updateSource('),
].join('\n');
const fields = new Map();
const field = id => {
  if (!fields.has(id)) fields.set(id, { value: '', textContent: '', open: false,
    setAttribute() {}, removeAttribute() {}, focus() {} });
  return fields.get(id);
};
const saves = [], removals = [];
const context = { assert, URL, TextEncoder, console, MAX_COOKIE_BYTES: 65536,
  $: field, input: field('video-url'), formatBytes: bytes => `${bytes} B`,
  updateCookieStatus() {}, announce() {}, cookieMemoryStatus() {},
  window: { OndaUI: { activate() {} } },
  cookieVaultReady: Promise.resolve({ async save(cookies, filename) { saves.push({ cookies, filename }); },
    async remove() { removals.push(true); } }),
  cookieSaveSequence: 0, cookieSavePromise: Promise.resolve(true), sessionLocked: false,
  validatedCookieLink: '', saves, removals,
};
field('cookies-filename').textContent = 'Nenhum arquivo selecionado';
const cases = `
(async () => {
  const records = [{domain: '.youtube.com', name: 'fixture', value: 'fixture-cookie', secure: true}];
  const browserSecret = 'browser-storage-secret-must-not-be-saved-or-uploaded';
  const pasted = JSON.stringify({cookies: records,
    origins: [{origin: 'https://youtube.com', localStorage: [{name: 'accessToken', value: browserSecret}]}],
    anotherExportField: browserSecret});
  $('cookies-input').value = pasted;
  input.value = 'https://www.youtube.com/watch?v=fixture';
  assert.deepEqual(JSON.parse(getCookies()), records);
  const request = withCookies({url: input.value, media_type: 'video'}, getCookies());
  assert.deepEqual(JSON.parse(request.cookies), records);
  assert(!JSON.stringify(request).includes(browserSecret));
  assert.equal(await persistCookies(), true);
  assert.deepEqual(JSON.parse(saves.at(-1).cookies), records);
  assert(!JSON.stringify(saves).includes(browserSecret));

  // Original input size is checked before stripping unrelated wrapper fields.
  const originalSaveCount = saves.length;
  $('cookies-input').value = JSON.stringify({cookies: records, origins: ['x'.repeat(65536)]});
  assert($('cookies-input').value.length > MAX_COOKIE_BYTES);
  assert.equal(getCookies(), null);
  assert.equal(await persistCookies(), false);
  assert.equal(saves.length, originalSaveCount);
  $('cookies-input').value = JSON.stringify({cookies: records, origins: ['á'.repeat(33000)]});
  assert($('cookies-input').value.length < MAX_COOKIE_BYTES);
  assert.equal(getCookies(), null);
  assert.equal(await persistCookies(), false);
  assert.equal(saves.length, originalSaveCount);

  // These sibling hosts share a platform, even when cookies are host-only.
  for (const [url, domain] of [
    ['https://www.soundcloud.com/artist/track', 'api-v2.soundcloud.com'],
    ['https://player.vimeo.com/video/123', 'www.vimeo.com'],
    ['https://open.spotify.com/track/123', 'accounts.spotify.com'],
    ['https://www.tiktok.com/@fixture/video/123', 'm.tiktok.com'],
    ['https://www.instagram.com/reel/fixture', 'i.instagram.com'],
  ]) {
    const cookies = JSON.stringify([{domain, name: 'fixture', value: 'fixture'}]);
    assert.equal(withCookies({url}, cookies).cookies, cookies, url);
  }

  const youtubeCookies = JSON.stringify(records);
  assert(!('cookies' in withCookies({url: 'https://www.tiktok.com/@fixture/video/123'}, youtubeCookies)));
  assert(!('cookies' in withCookies({url: 'https://youtube.com.evil.org/watch?v=fixture'}, youtubeCookies)));
  assert(!('cookies' in withCookies({url: 'https://evil-youtube.com/watch?v=fixture'}, youtubeCookies)));
  assert(!('cookies' in withCookies({url: 'https://media.example.org/fixture.mp4'}, youtubeCookies)));

  // Family aliases intentionally accepted by the server remain usable.
  for (const [url, domain] of [
    ['https://youtu.be/fixture', 'accounts.google.com'],
    ['https://www.youtube.com/watch?v=fixture', '.googlevideo.com'],
    ['https://x.com/fixture/status/123', '.twitter.com'],
    ['https://twitter.com/fixture/status/123', 'api.x.com'],
    ['https://fb.watch/fixture/', '.facebook.com'],
  ]) {
    const cookies = JSON.stringify([{domain, name: 'fixture', value: 'fixture'}]);
    assert.equal(withCookies({url}, cookies).cookies, cookies, url);
  }
  const netscape = '# Netscape HTTP Cookie File\\n.google.com\\tTRUE\\t/\\tTRUE\\t0\\tfixture\\tfixture\\n';
  assert.equal(withCookies({url: 'https://youtu.be/fixture'}, netscape).cookies, netscape);

  // Backend validation can confirm a generic provider's sibling scopes.
  const generic = 'https://media.example.org/watch/123';
  const genericCookies = JSON.stringify([{domain: 'login.example.org', name: 'fixture', value: 'fixture'}]);
  assert(!('cookies' in withCookies({url: generic}, genericCookies)));
  validatedCookieLink = generic;
  assert.equal(withCookies({url: generic}, genericCookies).cookies, genericCookies);
  assert(!('cookies' in withCookies({url: 'https://other.example.org/watch/123'}, genericCookies)));
  validatedCookieLink = 'https://media.example.org/fixture.mp4';
  assert(!('cookies' in withCookies({url: validatedCookieLink}, genericCookies)));
  validatedCookieLink = '';

  // Password and User-Agent forwarding coexist with platform-scoped cookies.
  $('video-password').value = 'fixture-video-password';
  $('session-user-agent').value = 'Fixture Browser/1.0';
  const options = withCookies({url: 'https://www.youtube.com/watch?v=fixture'}, youtubeCookies);
  assert.equal(options.video_password, 'fixture-video-password');
  assert.equal(options.user_agent, 'Fixture Browser/1.0');
  assert.equal(options.cookies, youtubeCookies);
  console.log('Source sessions: pasted-export privacy, original byte bounds, platform siblings, aliases and validation scope passed.');
})()
`;
(async () => { await vm.runInNewContext(production + cases, context); })()
  .catch(error => { console.error(error); process.exitCode = 1; });
