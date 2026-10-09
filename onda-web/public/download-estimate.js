/* Estimates use public media metadata only. No URLs, cookies or account data are retained. */
(() => {
  'use strict';

  const OUTPUT_LIMIT = 100 * 1024 * 1024;
  const MAX_DURATION = 10 * 365.25 * 24 * 60 * 60;
  const AUDIO_FORMATS = new Set(['mp3', 'm4a', 'aac', 'ogg', 'opus', 'wav', 'aiff', 'flac']);
  const VIDEO_FORMATS = new Set(['mp4', 'mov', 'mkv', 'webm']);
  const LOSSLESS = new Set(['wav', 'aiff', 'flac']);
  const QUALITIES = [128, 192, 256, 320];
  const VIDEO_CODECS = {
    mp4: new Set(['h264', 'hevc', 'av1', 'vp9']), mov: new Set(['h264', 'hevc']),
    mkv: new Set(['h264', 'hevc', 'av1', 'vp9', 'vp8']), webm: new Set(['vp9', 'vp8', 'av1']),
  };

  function positive(value, ceiling = Number.MAX_SAFE_INTEGER) {
    return typeof value === 'number' && Number.isFinite(value) && value > 0 && value <= ceiling ? value : null;
  }

  function edge(item) {
    const width = positive(item?.width, 16384);
    const height = positive(item?.height, 16384);
    return width && height ? Math.min(width, height) : positive(item?.resolution, 16384) || height || width;
  }

  function unknown(message = 'Analise o link para estimar o tamanho.', referenceBytes = null) {
    return { state: 'unknown', approximate: true, minBytes: null, maxBytes: null,
      expectedBytes: null, message, note: '', warning: '', referenceBytes };
  }

  function invalid(message) {
    return { ...unknown(message), state: 'invalid' };
  }

  function interval(base, lower, upper, details) {
    if (!positive(base)) return unknown('A origem não informou dados suficientes para esta estimativa.');
    const minBytes = Math.max(1, Math.round(base * lower));
    const maxBytes = Math.max(minBytes, Math.round(base * upper));
    if (!Number.isSafeInteger(maxBytes)) return unknown('Não foi possível estimar esse tamanho com segurança.');
    const warning = minBytes > OUTPUT_LIMIT
      ? 'A estimativa ultrapassa 100 MB. Uma qualidade menor ou um trecho pode ajudar.'
      : maxBytes >= OUTPUT_LIMIT * .8
        ? 'O arquivo pode se aproximar ou ultrapassar 100 MB. O tamanho será confirmado no preparo.' : '';
    return { state: 'ready', approximate: true, minBytes, maxBytes, expectedBytes: Math.round(base),
      message: 'Tamanho estimado', note: '', warning, referenceBytes: null, ...details };
  }

  function durationFor(metadata, selection) {
    const duration = positive(metadata?.duration, MAX_DURATION);
    if (!duration) return { state: 'unknown' };
    const start = selection.trim_start == null ? 0 : selection.trim_start;
    const end = selection.trim_end == null ? duration : selection.trim_end;
    if (typeof start !== 'number' || !Number.isFinite(start) || start < 0
        || typeof end !== 'number' || !Number.isFinite(end) || end <= start
        || start >= duration || end > duration + .05) return { state: 'invalid' };
    return { state: 'ready', sourceDuration: duration, duration: Math.min(end, duration) - start,
      trimmed: start > 0 || end < duration };
  }

  function formatsFor(metadata) {
    return Array.isArray(metadata?.formats) ? metadata.formats.slice(0, 200).filter(item =>
      item && typeof item === 'object' && !Array.isArray(item)) : [];
  }

  function sourceContainer(metadata, formats) {
    const name = typeof metadata.container === 'string' ? metadata.container.toLowerCase() : '';
    if (AUDIO_FORMATS.has(name) || VIDEO_FORMATS.has(name)) return name;
    // A lone audio representation can identify a lossless source without relying on its URL.
    const audio = formats.filter(item => item.hasAudio === true && item.hasVideo === false);
    return audio.length === 1 && typeof audio[0].container === 'string' ? audio[0].container.toLowerCase() : '';
  }

  function estimateAudio(metadata, selection, timing, formats) {
    const format = selection.format;
    if (!AUDIO_FORMATS.has(format)) return invalid('Escolha um formato de áudio válido.');
    const details = { duration: timing.duration, qualityLabel: format.toUpperCase() };
    if (metadata.hasAudio === false && !formats.some(item => item.hasAudio === true)) {
      return { ...unknown('A análise não encontrou uma faixa de áudio.'), state: 'unavailable' };
    }
    if (!LOSSLESS.has(format)) {
      const quality = typeof selection.quality === 'string' && /^\d{3}$/.test(selection.quality)
        ? Number(selection.quality) : selection.quality;
      if (!QUALITIES.includes(quality)) return invalid('Escolha uma qualidade de áudio válida.');
      const base = quality * 1000 * timing.duration / 8;
      const variable = format === 'ogg' || format === 'opus';
      return interval(base, variable ? .82 : .94, variable ? 1.22 : 1.08, { ...details,
        qualityLabel: `${quality} kbps`, note: timing.trimmed
          ? 'Estimativa do trecho escolhido. O formato e o conteúdo podem alterar o tamanho.'
          : 'A duração e a qualidade escolhida orientam esta faixa aproximada.' });
    }
    const sourceBytes = positive(metadata.sourceSizeBytes);
    const container = sourceContainer(metadata, formats);
    if (sourceBytes && container === format && !timing.trimmed && !selection.normalize_audio
        && !formats.some(item => item.hasVideo === true) && metadata.hasVideo !== true) {
      return interval(sourceBytes, .96, 1.08, { ...details, note: 'Referência da faixa original sem perda. O arquivo final pode variar.' });
    }
    const pcmBytes = timing.duration * 44100 * 2 * 2;
    if (format === 'wav' || format === 'aiff') return interval(pcmBytes + 4096, .98, 1.03, { ...details,
      note: 'Referência em áudio estéreo de 16 bits e 44,1 kHz. Preservar uma faixa original pode alterar o tamanho.' });
    // FLAC depends on the signal. Its source bitrate is not an output bitrate promise.
    return interval(pcmBytes, .30, .90, { ...details,
      expectedBytes: Math.round(pcmBytes * .60),
      note: 'A compressão sem perda depende do áudio; por isso a faixa estimada é mais ampla.' });
  }

  function formatValue(item, duration) {
    const bytes = positive(item.sizeBytes);
    if (bytes) return bytes;
    const bitrate = positive(item.bitrateKbps, 1000000)
      || ((positive(item.videoBitrateKbps, 1000000) || 0) + (positive(item.audioBitrateKbps, 1000000) || 0));
    return positive(bitrate) ? bitrate * 1000 * duration / 8 : null;
  }

  function videoScore(item, format) {
    const codec = typeof item.videoCodec === 'string' ? item.videoCodec : '';
    const compatible = VIDEO_CODECS[format].has(codec) && (positive(item.fps, 1000) || 30) <= 30;
    return compatible ? codec === 'h264' && (format === 'mp4' || format === 'mov') ? 2 : 1 : 0;
  }

  function estimateVideo(metadata, selection, timing, formats) {
    const format = selection.format;
    if (!VIDEO_FORMATS.has(format)) return invalid('Escolha um formato de vídeo válido.');
    const resolution = selection.video_resolution == null ? 'source' : String(selection.video_resolution);
    if (!['source', '360', '480', '720', '1080', '1440', '2160'].includes(resolution)) {
      return invalid('Escolha uma resolução válida.');
    }
    const cap = resolution === 'source' ? 2160 : Number(resolution);
    let videos = formats.filter(item => item.hasVideo === true && positive(edge(item), 2160)
      && (!positive(item.width, 16384) || !positive(item.height, 16384)
        || Math.max(item.width, item.height) <= 3840));
    const sourceEdge = edge(metadata);
    if (!videos.length && sourceEdge && sourceEdge <= cap && sourceEdge <= 2160
        && (positive(metadata.sourceSizeBytes) || positive(metadata.sourceBitrateKbps)
          || positive(metadata.videoBitrateKbps))) {
      videos = [{ ...metadata, resolution: sourceEdge, hasVideo: true,
        sizeBytes: metadata.sourceSizeBytes, bitrateKbps: metadata.sourceBitrateKbps,
        hasAudio: metadata.hasAudio, container: metadata.container }];
    }
    const candidates = videos.filter(item => edge(item) <= cap);
    candidates.sort((a, b) => edge(b) - edge(a) || videoScore(b, format) - videoScore(a, format)
      || Number(b.hasAudio === true) - Number(a.hasAudio === true)
      || (formatValue(b, timing.sourceDuration) || 0) - (formatValue(a, timing.sourceDuration) || 0));
    const selected = candidates[0];
    if (!selected) return unknown('A origem ainda não informou o tamanho ou a taxa desta qualidade. O preparo confirmará o arquivo.');
    let base = formatValue(selected, timing.sourceDuration);
    if (!positive(base)) return unknown('A origem não informou o tamanho ou a taxa desta qualidade.');
    const audioBitrate = positive(selected.audioBitrateKbps, 1000000) || positive(metadata.audioBitrateKbps, 1000000);
    const sourceAudioBytes = audioBitrate ? audioBitrate * 1000 * timing.sourceDuration / 8 : null;
    if (selection.mute && selected.hasAudio !== false && sourceAudioBytes) {
      base = Math.max(1, base - sourceAudioBytes);
    } else if (!selection.mute && selected.hasAudio === false) {
      const audio = formats.filter(item => item.hasAudio === true && item.hasVideo === false)
        .sort((a, b) => (positive(b.audioBitrateKbps) || positive(b.bitrateKbps) || 0)
          - (positive(a.audioBitrateKbps) || positive(a.bitrateKbps) || 0))[0];
      if (audio) base += formatValue(audio, timing.sourceDuration)
        || (positive(audio.audioBitrateKbps) || 192) * 1000 * timing.sourceDuration / 8;
      else if (positive(metadata.audioBitrateKbps)) base += metadata.audioBitrateKbps * 1000 * timing.sourceDuration / 8;
    }
    base *= timing.duration / timing.sourceDuration;
    const actualEdge = edge(selected);
    const limited = resolution !== 'source' && actualEdge < cap;
    const copyPossible = !timing.trimmed && !selection.normalize_audio
      && videoScore(selected, format) > 0 && positive(selected.fps, 30);
    const note = [limited ? sourceEdge && actualEdge === sourceEdge
      ? `A fonte oferece até ${Math.round(sourceEdge)}p; a estimativa usa essa qualidade.`
      : `Estimativa na faixa disponível de ${Math.round(actualEdge)}p; a fonte não será ampliada.` : '',
      copyPossible ? 'A faixa original serve de referência; o tamanho pode variar no preparo.'
        : timing.trimmed ? 'Estimativa do trecho. A conversão pode mudar o tamanho final.'
          : 'O formato, o som e a conversão podem alterar o tamanho final.'].filter(Boolean).join(' ');
    return interval(base, copyPossible ? .90 : .55, copyPossible ? 1.18 : 1.65,
      { duration: timing.duration, resolution: Math.round(actualEdge), qualityLabel: `${Math.round(actualEdge)}p`, note });
  }

  function estimate(metadata, selection = {}) {
    if (!selection || typeof selection !== 'object' || Array.isArray(selection)) return invalid('Escolha um formato para estimar.');
    if (!metadata || typeof metadata !== 'object' || Array.isArray(metadata)) return unknown();
    const timing = durationFor(metadata, selection);
    if (timing.state === 'unknown') return unknown('A origem não informou a duração; o tamanho final será confirmado no preparo.', positive(metadata.sourceSizeBytes));
    if (timing.state === 'invalid') return invalid('Ajuste o início e o fim do trecho para estimar.');
    const type = selection.media_type || 'audio';
    if (type !== 'audio' && type !== 'video') return invalid('Escolha áudio ou vídeo para estimar.');
    return type === 'video' ? estimateVideo(metadata, selection, timing, formatsFor(metadata))
      : estimateAudio(metadata, selection, timing, formatsFor(metadata));
  }

  function compare(metadata, selection = {}) {
    if (!metadata || !selection || typeof selection !== 'object' || Array.isArray(selection)) return [];
    if (selection.media_type !== 'video') {
      const format = LOSSLESS.has(selection.format) ? 'mp3' : selection.format;
      return QUALITIES.map(quality => ({ label: `${LOSSLESS.has(selection.format) ? 'MP3 · ' : ''}${quality} kbps`,
        quality, estimate: estimate(metadata, { ...selection, format, quality }) }))
        .filter(item => item.estimate.state === 'ready');
    }
    const levels = [...new Set(formatsFor(metadata).filter(item => item.hasVideo === true)
      .map(edge).filter(value => positive(value, 2160)).map(Math.round))]
      .filter(value => [360, 480, 720, 1080, 1440, 2160].includes(value)).sort((a, b) => a - b);
    return levels.map(value => ({ label: `${value}p`, resolution: value,
      estimate: estimate(metadata, { ...selection, video_resolution: String(value) }) }))
      .filter(item => item.estimate.state === 'ready' && item.estimate.resolution === item.resolution);
  }

  function formatBytes(value) {
    if (!positive(value)) return '—';
    const units = ['KB', 'MB', 'GB', 'TB'];
    let amount = value / 1024;
    let unit = 0;
    while (amount >= 1024 && unit < units.length - 1) { amount /= 1024; unit += 1; }
    return `${amount.toLocaleString('pt-BR', { maximumFractionDigits: amount < 10 ? 1 : 0 })} ${units[unit]}`;
  }

  function formatRange(result) {
    const lower = formatBytes(result.minBytes);
    const upper = formatBytes(result.maxBytes);
    return lower === upper ? `≈ ${upper}` : `${lower} – ${upper}`;
  }

  function render(metadata, selection) {
    if (typeof document === 'undefined') return;
    const region = document.getElementById('download-estimate');
    const value = document.getElementById('estimate-value');
    const detail = document.getElementById('estimate-detail');
    const comparisons = document.getElementById('estimate-quality-comparison');
    if (!region || !value || !detail || !comparisons) return;
    if (metadata === null && selection === null) { region.hidden = true; comparisons.replaceChildren(); return; }
    region.hidden = false;
    const result = estimate(metadata, selection);
    region.dataset.state = result.state;
    region.dataset.warning = result.warning ? 'true' : 'false';
    value.textContent = result.state === 'ready' ? formatRange(result) : '—';
    detail.textContent = result.state === 'ready'
      ? [result.note, result.warning].filter(Boolean).join(' ')
      : [result.message, result.referenceBytes ? `Origem: ${formatBytes(result.referenceBytes)}; esse valor não é o tamanho final.` : ''].filter(Boolean).join(' ');
    comparisons.replaceChildren();
    if (result.state !== 'ready') return;
    for (const item of compare(metadata, selection)) {
      const row = document.createElement('div');
      row.className = 'estimate-comparison-item';
      const label = document.createElement('span');
      label.textContent = item.label;
      const size = document.createElement('strong');
      size.textContent = formatRange(item.estimate);
      row.append(label, size);
      comparisons.append(row);
    }
  }

  const api = Object.freeze({ estimate, compare, render, formatBytes, OUTPUT_LIMIT });
  if (typeof window !== 'undefined') window.OndaDownloadEstimate = api;
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
})();
