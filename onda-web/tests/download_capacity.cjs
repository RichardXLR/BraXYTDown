'use strict';
// Exercise the shipped storage admission policy without allocating large files.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const { webcrypto } = require('node:crypto');
const source = fs.readFileSync(require('node:path').join(__dirname, '../public/download-storage.js'), 'utf8');
const MB = 1024 ** 2;
function fixture(storage) {
  const context = { navigator: { storage }, window: {}, setTimeout, clearTimeout,
    crypto: webcrypto, TextEncoder, Uint8Array, Blob };
  vm.createContext(context); vm.runInContext(source, context);
  return context.window.OndaDownloadStorage;
}
const range = (min, max = min) => ({ state: 'ready', minBytes: min * MB, maxBytes: max * MB });
const cases = {
  async quota_is_not_physical_disk_and_usage_is_subtracted() {
    const api = fixture({ getDirectory() {}, estimate: async () => ({ quota: 600 * MB, usage: 100 * MB }) });
    const capacity = await api.capacity();
    assert.equal(capacity.kind, 'disk'); assert.equal(capacity.availableBytes, 500 * MB);
    const result = api.assess(range(450, 480), capacity);
    assert.equal(result.state, 'available'); assert.equal(result.route, 'disk');
    assert.equal(result.budgetBytes, 484 * MB);
  },
  async lower_upper_range_and_reserve_classify_risk() {
    const api = fixture({});
    const capacity = { kind: 'disk', availableBytes: 150 * MB };
    assert.equal(api.assess(range(130, 140), capacity).state, 'risk');
    assert.equal(api.assess(range(140, 160), capacity).state, 'insufficient');
    assert.equal(api.assess(range(120, 134), capacity).state, 'available');
    assert.equal(api.assess(range(134, 134), capacity).state, 'available');
  },
  async memory_fallback_prevents_false_quota_rejection() {
    const api = fixture({});
    const capacity = { kind: 'disk', availableBytes: 0 };
    const small = api.assess(range(20, 80), capacity);
    assert.equal(small.state, 'available'); assert.equal(small.route, 'memory');
    assert.equal(api.assess(range(90, 110), capacity).state, 'risk');
    assert.equal(api.assess(range(110, 120), capacity).state, 'insufficient');
  },
  async unsupported_opfs_uses_existing_safe_memory_ceiling() {
    const api = fixture({ estimate() { throw new Error('Must not be called without OPFS'); } });
    const capacity = await api.capacity();
    assert.equal(capacity.kind, 'memory'); assert.equal(capacity.availableBytes, null);
    assert.equal(api.assess(range(101), capacity).state, 'insufficient');
    assert.equal(api.assess(range(99, 100), capacity).state, 'available');
  },
  async unknown_size_never_uses_source_bytes_as_final_output() {
    const api = fixture({});
    for (const estimate of [null, {}, { state: 'unknown', referenceBytes: 1024 ** 3 },
      range(0), range(-1), range(10, 9), { ...range(10), maxBytes: Infinity },
      { ...range(10), minBytes: '1' }, { ...range(10), maxBytes: Number.MAX_SAFE_INTEGER + 1 }]) {
      assert.equal(api.assess(estimate, { kind: 'disk', availableBytes: 10 * MB }).state, 'unknown');
    }
  },
  async missing_rejected_or_malformed_quota_does_not_block() {
    for (const value of [null, {}, { quota: 1 }, { quota: 0, usage: '0' },
      { quota: Infinity, usage: 0 }, { quota: 100, usage: -1 }]) {
      const api = fixture({ getDirectory() {}, estimate: async () => value });
      const capacity = await api.capacity();
      assert.equal(capacity.kind, 'unknown');
      assert.equal(api.assess(range(200), capacity).state, 'unknown');
    }
    const api = fixture({ getDirectory() {}, estimate: async () => { throw new Error('private browser policy'); } });
    assert.equal((await api.capacity()).kind, 'unknown');
    assert.equal((await fixture({ getDirectory() {} }).capacity()).kind, 'unknown');
  },
  async zero_quota_and_usage_over_quota_are_real_no_disk_space() {
    for (const quota of [{ quota: 0, usage: 0 }, { quota: 100, usage: 200 }]) {
      const api = fixture({ getDirectory() {}, estimate: async () => quota });
      const capacity = await api.capacity(); assert.equal(capacity.availableBytes, 0);
      assert.equal(api.assess(range(110), capacity).state, 'insufficient');
    }
  },
  async slow_quota_has_bounded_wait_and_late_rejection_is_consumed() {
    let reject;
    const api = fixture({ getDirectory() {}, estimate: () => new Promise((_, fail) => { reject = fail; }) });
    const start = performance.now();
    assert.equal((await api.capacity({ timeoutMs: 10 })).kind, 'unknown');
    assert.ok(performance.now() - start < 1000);
    reject(new Error('late private fixture')); await new Promise(resolve => setTimeout(resolve, 20));
  },
  async large_outputs_use_quota_without_new_artificial_ceiling() {
    const api = fixture({});
    const capacity = { kind: 'disk', availableBytes: 3 * 1024 ** 3 };
    for (const size of [100, 250, 500, 1024, 2048]) assert.equal(api.assess(range(size), capacity).state, 'available');
    assert.equal(api.assess(range(4097), { kind: 'disk', availableBytes: 10 * 1024 ** 3 }).state, 'insufficient');
  },
  async receiver_rechecks_exact_size_before_writing() {
    let directories = 0;
    const api = fixture({ getDirectory() { directories++; }, estimate: async () => ({ quota: 120 * MB, usage: 20 * MB }) });
    await assert.rejects(api.create({ size: 110 * MB, mime: 'video/mp4' }, { userId: 'owned-fixture' }), error => error.code === 'memory_limit');
    assert.equal(directories, 0);
    const small = await api.create({ size: 16, mime: 'audio/mpeg' }, { userId: 'owned-fixture' });
    assert.equal(small.kind, 'memory'); await small.abort();
  },
};
(async () => {
  const name = process.argv[2]; assert.ok(cases[name], 'Known capacity scenario required');
  await cases[name](); console.log(`${name} passed.`);
})().catch(error => { console.error(error); process.exitCode = 1; });
