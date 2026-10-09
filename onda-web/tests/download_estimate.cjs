'use strict';
const assert = require('node:assert/strict');
const { estimate, compare, formatBytes, OUTPUT_LIMIT } = require('../public/download-estimate.js');

const audio = { media_type: 'audio', format: 'mp3', quality: '192', trim_start: null, trim_end: null };
const video = { media_type: 'video', format: 'mp4', quality: 'source', video_resolution: 'source' };
const metadata = {
  duration: 60, width: 1920, height: 1080,
  formats: [
    { width: 640, height: 360, hasVideo: true, hasAudio: true, sizeBytes: 2_000_000, videoCodec: 'h264', fps: 30 },
    { width: 1280, height: 720, hasVideo: true, hasAudio: true, bitrateKbps: 1500, videoCodec: 'h264', fps: 30 },
    { width: 1920, height: 1080, hasVideo: true, hasAudio: false, videoBitrateKbps: 3000, videoCodec: 'h264', fps: 30 },
    { hasVideo: false, hasAudio: true, bitrateKbps: 128, audioBitrateKbps: 128, audioCodec: 'aac', container: 'm4a' },
  ],
};

const cases = {
  audio_bitrate_and_overhead() {
    const result = estimate({ duration: 60 }, audio);
    assert.equal(result.state, 'ready');
    assert.equal(result.expectedBytes, 1_440_000);
    assert.ok(result.minBytes < result.expectedBytes && result.maxBytes > result.expectedBytes);
    assert.equal(result.approximate, true);
    assert.equal(result.warning, '');
    const opus = estimate({ duration: 60 }, { ...audio, format: 'opus' });
    assert.ok(opus.maxBytes - opus.minBytes > result.maxBytes - result.minBytes);
  },
  duration_and_trim() {
    const full = estimate({ duration: 120 }, audio);
    const cut = estimate({ duration: 120 }, { ...audio, trim_start: 10, trim_end: 40 });
    assert.equal(cut.duration, 30);
    assert.equal(cut.expectedBytes * 4, full.expectedBytes);
    assert.equal(estimate({ duration: 120 }, { ...audio, trim_start: 30 }).duration, 90);
    assert.equal(estimate({ duration: 120 }, { ...audio, trim_end: 120.01 }).duration, 120);
    for (const trim of [
      { trim_start: 120 }, { trim_start: -1 }, { trim_start: NaN },
      { trim_start: '10' }, { trim_start: 10, trim_end: 10 }, { trim_end: 121 },
    ]) assert.equal(estimate({ duration: 120 }, { ...audio, ...trim }).state, 'invalid');
  },
  unknown_duration_is_not_a_file_size() {
    for (const duration of [null, undefined, 0, -1, '60', NaN, Infinity, 1e10]) {
      const result = estimate({ duration, sourceSizeBytes: 15_000_000 }, audio);
      assert.equal(result.state, 'unknown');
      assert.equal(result.expectedBytes, null);
      assert.equal(result.referenceBytes, 15_000_000);
    }
    assert.equal(estimate(null, audio).state, 'unknown');
    assert.equal(estimate([], audio).state, 'unknown');
    assert.equal(estimate({ duration: 60 }, null).state, 'invalid');
    assert.equal(estimate({ duration: 60 }, { ...audio, quality: 512 }).state, 'invalid');
    assert.equal(estimate({ duration: 60 }, { ...audio, format: 'exe' }).state, 'invalid');
    assert.equal(estimate({ duration: 60 }, { ...audio, media_type: 'unknown' }).state, 'invalid');
  },
  pcm_and_flac_are_distinct() {
    const wav = estimate({ duration: 60 }, { ...audio, format: 'wav', quality: 'source' });
    const aiff = estimate({ duration: 60 }, { ...audio, format: 'aiff', quality: 'source' });
    const flac = estimate({ duration: 60 }, { ...audio, format: 'flac', quality: 'source' });
    assert.equal(wav.expectedBytes, 60 * 44100 * 2 * 2 + 4096);
    assert.equal(aiff.expectedBytes, wav.expectedBytes);
    assert.ok(flac.minBytes < flac.expectedBytes && flac.maxBytes > flac.expectedBytes);
    assert.ok(flac.maxBytes - flac.minBytes > wav.maxBytes - wav.minBytes);
    assert.ok(flac.maxBytes < wav.expectedBytes);
    const original = estimate({ duration: 60, container: 'flac', hasVideo: false, sourceSizeBytes: 5_000_000 },
      { ...audio, format: 'flac', quality: 'source' });
    assert.equal(original.expectedBytes, 5_000_000);
    const changed = estimate({ duration: 60, container: 'flac', sourceSizeBytes: 5_000_000 },
      { ...audio, format: 'flac', quality: 'source', normalize_audio: true });
    assert.notEqual(changed.expectedBytes, original.expectedBytes);
  },
  video_uses_available_rate_not_invented_quality() {
    const result = estimate(metadata, { ...video, video_resolution: '720' });
    assert.equal(result.state, 'ready');
    assert.equal(result.resolution, 720);
    assert.equal(result.expectedBytes, 1500 * 1000 * 60 / 8);
    const lower = estimate(metadata, { ...video, video_resolution: '480' });
    assert.equal(lower.resolution, 360);
    assert.match(lower.note, /360p/);
    assert.equal(estimate({ duration: 60, width: 1920, height: 1080 }, video).state, 'unknown');
    assert.equal(estimate({ duration: 60, sourceSizeBytes: 10_000_000 }, video).state, 'unknown');
    assert.equal(estimate(metadata, { ...video, video_resolution: '4320' }).state, 'invalid');
    assert.equal(estimate({ duration: 60, formats: [{ width: 7680, height: 4320, hasVideo: true, sizeBytes: 1_000_000 }] }, video).state, 'unknown');
  },
  video_separate_audio_mute_and_trim() {
    const full = estimate(metadata, video);
    assert.equal(full.expectedBytes, (3000 + 128) * 1000 * 60 / 8);
    const muted = estimate(metadata, { ...video, mute: true });
    assert.equal(muted.expectedBytes, 3000 * 1000 * 60 / 8);
    const cut = estimate(metadata, { ...video, trim_start: 10, trim_end: 40 });
    assert.equal(cut.expectedBytes * 2, full.expectedBytes);
    assert.ok(cut.maxBytes / cut.expectedBytes > full.maxBytes / full.expectedBytes);
    const normalized = estimate(metadata, { ...video, normalize_audio: true });
    assert.ok(normalized.maxBytes - normalized.minBytes > full.maxBytes - full.minBytes);
    const muxed = { duration: 60, width: 1280, height: 720, formats: [{ hasVideo: true, hasAudio: true,
      width: 1280, height: 720, sizeBytes: 10_000_000, audioBitrateKbps: 128, videoCodec: 'h264', fps: 30 }] };
    assert.equal(estimate(muxed, { ...video, mute: true }).expectedBytes, 10_000_000 - 128 * 1000 * 60 / 8);
  },
  lower_source_is_never_upscaled() {
    const small = { duration: 60, width: 1280, height: 720, sourceSizeBytes: 5_000_000, sourceBitrateKbps: 800,
      videoCodec: 'h264', fps: 30 };
    const result = estimate(small, { ...video, video_resolution: '2160' });
    assert.equal(result.resolution, 720);
    assert.equal(result.expectedBytes, 5_000_000);
    assert.match(result.note, /720p/);
    const missingLowerFormat = estimate(small, { ...video, video_resolution: '360' });
    assert.equal(missingLowerFormat.state, 'unknown');
    assert.equal(missingLowerFormat.expectedBytes, null);
  },
  comparisons_only_use_reported_video_resolutions() {
    const choices = compare(metadata, video);
    assert.deepEqual(choices.map(item => item.resolution), [360, 720, 1080]);
    assert.ok(choices.every(item => item.estimate.state === 'ready'));
    assert.deepEqual(compare({ duration: 60 }, video), []);
    const audioChoices = compare({ duration: 60 }, audio);
    assert.deepEqual(audioChoices.map(item => item.quality), [128, 192, 256, 320]);
    assert.ok(audioChoices[3].estimate.expectedBytes > audioChoices[0].estimate.expectedBytes);
    assert.match(compare({ duration: 60 }, { ...audio, format: 'wav' })[0].label, /^MP3/);
    assert.deepEqual(compare({ duration: null }, audio), []);
  },
  limit_warnings_are_about_estimates() {
    const near = estimate({ duration: OUTPUT_LIMIT * .85 * 8 / 320000 }, { ...audio, quality: 320 });
    assert.match(near.warning, /aproximar ou ultrapassar/);
    const large = estimate({ duration: OUTPUT_LIMIT * 2 * 8 / 320000 }, { ...audio, quality: 320 });
    assert.match(large.warning, /ultrapassa 100 MB/);
    assert.ok(large.expectedBytes > OUTPUT_LIMIT, 'Never silently clamp an estimate to the service limit');
    assert.equal(estimate({ duration: 1 }, audio).warning, '');
  },
  dense_untrusted_metadata_is_bounded() {
    const dense = { duration: 60, formats: Array.from({ length: 10000 }, (_, index) => index < 200
      ? null : { width: 1280, height: 720, hasVideo: true, sizeBytes: 5_000_000 }) };
    assert.equal(estimate(dense, video).state, 'unknown');
    assert.deepEqual(compare(dense, video), []);
    assert.equal(estimate({ duration: 60, width: 1280, height: 720, sourceSizeBytes: Number.MAX_VALUE }, video).state, 'unknown');
    assert.equal(estimate({ duration: 60, width: 1280, height: 720, sourceBitrateKbps: -1 }, video).state, 'unknown');
    assert.equal(estimate({ duration: 60, formats: [{ width: 1280, height: 720, hasVideo: true, bitrateKbps: NaN }] }, video).state, 'unknown');
    const original = JSON.stringify(metadata);
    estimate(metadata, video);
    compare(metadata, video);
    assert.equal(JSON.stringify(metadata), original);
  },
  render_unknown_ready_and_logout() {
    class Element {
      constructor() { this.hidden = true; this.dataset = {}; this.children = []; this.textContent = ''; }
      append(...children) { this.children.push(...children); }
      replaceChildren(...children) { this.children = [...children]; }
      set innerHTML(_) { throw new Error('Untrusted data must never reach innerHTML'); }
    }
    const elements = Object.fromEntries(['download-estimate', 'estimate-value', 'estimate-detail', 'estimate-quality-comparison']
      .map(id => [id, new Element()]));
    global.document = { getElementById: id => elements[id], createElement: () => new Element() };
    try {
      const { render } = require('../public/download-estimate.js');
      render(null, audio);
      assert.equal(elements['download-estimate'].hidden, false);
      assert.equal(elements['download-estimate'].dataset.state, 'unknown');
      assert.match(elements['estimate-detail'].textContent, /Analise o link/);
      assert.equal(elements['estimate-quality-comparison'].children.length, 0);
      render(metadata, video);
      assert.equal(elements['download-estimate'].dataset.state, 'ready');
      assert.equal(elements['estimate-quality-comparison'].children.length, 3);
      assert.ok(elements['estimate-value'].textContent.includes('MB'));
      render({ duration: null, sourceSizeBytes: 10_000_000 }, audio);
      assert.equal(elements['estimate-quality-comparison'].children.length, 0);
      assert.match(elements['estimate-detail'].textContent, /não é o tamanho final/);
      render(null, null);
      assert.equal(elements['download-estimate'].hidden, true);
      assert.equal(elements['estimate-quality-comparison'].children.length, 0);
    } finally { delete global.document; }
  },
  byte_format_is_honest_and_localized() {
    assert.equal(formatBytes(null), '—');
    assert.equal(formatBytes(NaN), '—');
    assert.equal(formatBytes(-1), '—');
    assert.equal(formatBytes(1024 * 1024), '1 MB');
    assert.equal(formatBytes(1536 * 1024), '1,5 MB');
    assert.equal(formatBytes(2 * 1024 ** 3), '2 GB');
  },
};

const name = process.argv[2];
assert.ok(Object.hasOwn(cases, name), `Unknown case: ${name}`);
cases[name]();
console.log(`${name} passed.`);
