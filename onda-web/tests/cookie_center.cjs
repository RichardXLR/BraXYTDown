'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { webcrypto } = require('node:crypto');
const source = fs.readFileSync(path.resolve(__dirname, '../public/cookie-center.js'), 'utf8');
const now = Date.UTC(2026, 9, 9, 12);
const youtube = 'https://www.youtube.com/watch?v=fixture-only';
const secret = 'fixture-sensitive-never-render';
const cookie = (changes = {}) => ({ domain: '.youtube.com', name: 'fixture-private-name', value: secret, secure: true, session: true, ...changes });
const json = (items) => JSON.stringify(items);
function fixture() {
  const fields = new Map(), intervals = new Map(), events = new Map();
  let sequence = 0;
  function field(id) {
    if (!fields.has(id)) fields.set(id, { textContent: '', hidden: false, dataset: {} });
    return fields.get(id);
  }
  const context = { URL, TextEncoder, Intl, Date, crypto: webcrypto,
    document: { getElementById: field },
    setInterval(callback) { const id = ++sequence; intervals.set(id, callback); return id; },
    clearInterval(id) { intervals.delete(id); },
    window: { addEventListener(event, callback) { events.set(event, callback); } },
  };
  vm.createContext(context); vm.runInContext(source, context);
  return { center: context.window.OndaCookieCenter, fields, field, intervals, events };
}
const cases = {
  async safe_output() {
    const { center, fields } = fixture();
    center.render({ cookies: json([cookie(), cookie({ domain: '.tiktok.com' })]), url: youtube });
    const output = [...fields.values()].map(item => item.textContent).join(' ');
    assert(output.includes('YouTube') && output.includes('TikTok'));
    assert(!output.includes(secret) && !output.includes('fixture-private-name'));
  },
  async next_expiry_and_session() {
    const { center } = fixture();
    const summary = center.summarize({ cookies: json([cookie(), cookie({ expires: now / 1000 + 3600, session: false })]), url: youtube }, now);
    assert.equal(summary.state, 'renew-soon'); assert.equal(summary.sessionCookies, 1);
    assert.equal(summary.earliestExpiry, now / 1000 + 3600);
    assert(summary.expiration.includes('sem data'));
    assert(summary.renewal.includes('48 horas'));
  },
  async expiries_respect_platform() {
    const { center } = fixture();
    const cookies = json([cookie({ expires: now / 1000 + 1000000, session: false }), cookie({ domain: '.tiktok.com', expires: now / 1000 - 1, session: false })]);
    assert.equal(center.summarize({ cookies, url: youtube }, now).state, 'ready');
    assert.equal(center.summarize({ cookies, url: 'https://www.tiktok.com/@fixture/video/123' }, now).state, 'expired');
  },
  async positive_expiry_authoritative() {
    const { center } = fixture();
    const summary = center.summarize({ cookies: json([cookie({ expires: now / 1000 - 1 })]), url: youtube }, now);
    assert.equal(summary.state, 'expired'); assert.equal(summary.sessionCookies, 0);
  },
  async session_sentinels() {
    const { center } = fixture();
    for (const item of [cookie({ expires: -1 }), cookie({ expires: 0 }), cookie({ expirationDate: null })]) {
      const summary = center.summarize({ cookies: json([item]), url: youtube }, now);
      assert.equal(summary.sessionCookies, 1); assert.equal(summary.earliestExpiry, null);
      assert(summary.renewal.includes('Sem prazo garantido'));
    }
  },
  async invalid_cookie_dates() {
    const { center } = fixture();
    for (const item of [cookie({ expires: true }), cookie({ expires: '123' }), cookie({ expires: 1, expirationDate: 2 }), cookie({ session: false })]) {
      assert.equal(center.summarize({ cookies: json([item]), url: youtube }, now).state, 'invalid');
    }
  },
  async netscape_http_only() {
    const { center } = fixture();
    const text = `# Netscape HTTP Cookie File\n#HttpOnly_.youtube.com\tTRUE\t/\tTRUE\t${now / 1000 + 7200}\tprivate-name\t${secret}\n`;
    const summary = center.summarize({ cookies: text, url: youtube }, now);
    assert.equal(summary.platforms, 'YouTube'); assert.equal(summary.state, 'renew-soon');
  },
  async partitioned_ignored() {
    const { center } = fixture();
    const summary = center.summarize({ cookies: json([cookie({ partitionKey: 'fixture-partition', expires: now / 1000 - 1 }), cookie()]), url: youtube }, now);
    assert.equal(summary.state, 'ready'); assert.equal(summary.expiredCookies, 0);
  },
  async limits_and_unsafe_domains() {
    const { center } = fixture();
    for (const cookies of [json(Array.from({ length: 301 }, () => cookie())), 'x'.repeat(65537), json([cookie({ domain: '<img src=x>' })])]) {
      const result = center.summarize({ cookies }, now);
      assert.equal(result.state, 'invalid'); assert(!JSON.stringify(result).includes(secret));
    }
  },
  async metadata_content_binding() {
    const { center } = fixture();
    const cookies = json([cookie()]);
    const metadata = await center.createValidation(cookies, youtube, { validCookies: 1, ignoredCookies: 0, expiredCookies: 0, sessionCookies: 1, earliestExpiry: null }, null, now);
    assert.equal(metadata.validation.scope, 'youtube.com'); assert.equal(metadata.savedAt, now);
    const same = await center.createMetadata(json({ cookies: [cookie()], origins: [{ localStorage: [{ name: 'private', value: secret }] }] }), metadata, now + 1000);
    assert.equal(same.contentHash, metadata.contentHash); assert.equal(same.savedAt, now);
    assert.equal(same.validation.checkedAt, now);
    const changed = await center.createMetadata(json([cookie({ value: 'changed-fixture' })]), metadata, now + 1000);
    assert.notEqual(changed.contentHash, metadata.contentHash); assert.equal(changed.validation, null);
    assert(!JSON.stringify(metadata).includes(secret) && !JSON.stringify(metadata).includes('private-name'));
  },
  async validation_scope_and_labels() {
    const { center } = fixture();
    const cookies = json([cookie()]);
    const metadata = await center.createValidation(cookies, `${youtube}&privateQuery=${secret}#fragment`, { validCookies: 1, ignoredCookies: 0, expiredCookies: 0, sessionCookies: 1, earliestExpiry: null }, null, now);
    assert.equal(center.scopeFor('https://youtu.be/fixture'), 'youtube.com');
    assert.equal(center.scopeFor('https://www.youtube.com./watch?v=fixture'), 'youtube.com');
    assert.equal(center.scopeFor('https://twitter.com/fixture'), 'x.com');
    assert.equal(center.scopeFor('http://youtube.com/fixture'), null);
    assert.equal(center.scopeFor('https://user:private@youtube.com/fixture'), null);
    assert(!JSON.stringify(metadata).includes(secret));
    assert(!center.summarize({ cookies, url: 'https://youtu.be/another-fixture', metadata }, now).lastValidation.includes('outra plataforma'));
    assert(center.summarize({ cookies, url: 'https://www.tiktok.com/@fixture/video/1', metadata }, now).lastValidation.includes('outra plataforma'));
  },
  async failure_sanitized() {
    const { center } = fixture();
    const metadata = await center.createValidationFailure(json([cookie()]), youtube, secret, null, now);
    assert.equal(metadata.validation.code, 'validation_failed');
    assert(!JSON.stringify(metadata).includes(secret));
    assert(center.summarize({ cookies: json([cookie()]), url: youtube, metadata }, now).lastValidation.includes('precisa de atenção'));
  },
  async timers_removed() {
    const { center, field, intervals, events } = fixture();
    center.render({ cookies: json([cookie()]), url: youtube });
    center.render({ cookies: json([cookie()]), url: youtube });
    assert.equal(intervals.size, 1); assert.equal(field('cookie-center').hidden, false);
    center.render({ cookies: '' }); assert.equal(intervals.size, 0); assert.equal(field('cookie-center').hidden, true);
    center.render({ cookies: json([cookie()]) }); events.get('pagehide')(); assert.equal(intervals.size, 0);
  },
};
(async () => {
  const name = process.argv[2]; assert.equal(typeof cases[name], 'function', `Known case: ${name}`);
  await cases[name](); process.stdout.write(`${name} passed.\n`);
})().catch(error => { process.stderr.write(`${error.stack}\n`); process.exitCode = 1; });
