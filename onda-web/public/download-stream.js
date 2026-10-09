(() => {
  'use strict';
  const CONTENT_TYPE = 'application/x-onda-download;version=1';
  const MAX_OUTPUT = 100 * 1024 * 1024;
  const MAX_JSON = 16 * 1024;
  const MAX_BINARY = 1024 * 1024;
  const STAGES = new Set(['extracting', 'downloading', 'converting', 'delivering', 'ready']);
  const COUNTERS = ['downloadedBytes', 'totalBytes', 'speedBytesPerSecond', 'processedSeconds', 'durationSeconds', 'outputBytes', 'attempt'];
  const MIMES = new Set(['audio/mpeg', 'audio/mp4', 'audio/wav', 'audio/flac', 'audio/ogg', 'audio/aac', 'audio/aiff', 'video/mp4', 'video/webm', 'video/x-matroska', 'video/quicktime']);
  function invalid(message = 'A transferência está incompleta ou retornou dados inválidos. Tente novamente.') {
    const error = new Error(message);
    error.code = 'download_incomplete';
    return error;
  }
  function validText(value, maximum) {
    return typeof value === 'string' && value.length > 0 && value.length <= maximum && !/[\u0000-\u001f\u007f]/.test(value);
  }
  function recovery(value) {
    if (!value || typeof value !== 'object' || Array.isArray(value)) return undefined;
    const result = {};
    if (Number.isInteger(value.attempts) && value.attempts >= 1 && value.attempts <= 3) result.attempts = value.attempts;
    for (const key of ['resumed', 'conversion_recovered', 'queued', 'exhausted']) {
      if (typeof value[key] === 'boolean') result[key] = value[key];
    }
    return result;
  }
  async function receive(response, { signal, ensureOperation = () => {}, onEvent = () => {}, maxBytes = MAX_OUTPUT } = {}) {
    const beforeReaderFailure = async error => {
      try { await response.body?.cancel(); } catch (_) { /* Preserve the original failure. */ }
      throw error;
    };
    if (!Number.isSafeInteger(maxBytes) || maxBytes <= 0 || maxBytes > MAX_OUTPUT) return beforeReaderFailure(invalid());
    const check = () => {
      if (signal?.aborted) throw new DOMException('Download cancelado.', 'AbortError');
      ensureOperation();
    };
    try { check(); } catch (error) { return beforeReaderFailure(error); }
    const contentType = response.headers?.get('Content-Type') || '';
    if (!/^application\/x-onda-download\s*;\s*version=1\s*$/i.test(contentType)) return beforeReaderFailure(invalid('O serviço retornou um protocolo de download incompatível.'));
    if (!response.body?.getReader) return beforeReaderFailure(invalid('Seu navegador não oferece suporte à transferência com progresso. Atualize o navegador.'));
    const reader = response.body.getReader();
    const chunks = [];
    const header = new Uint8Array(5);
    let headerUsed = 0, payload = null, payloadUsed = 0, kind = 0;
    let metadata = null, received = 0, complete = false, succeeded = false, events = 0;
    const now = () => (typeof performance !== 'undefined' ? performance.now() : Date.now()) / 1000;
    let deliveryStarted = null, lastDeliveryTime = null, lastDeliveryBytes = 0;
    const decoder = new TextDecoder('utf-8', { fatal: true });
    const abort = () => { Promise.resolve(reader.cancel()).catch(() => {}); };
    signal?.addEventListener('abort', abort, { once: true });
    function notify(event) {
      check();
      onEvent(event);
      check();
    }
    function reportDelivery(force = false) {
      if (!metadata || deliveryStarted === null) return;
      const time = now();
      if (!force && lastDeliveryTime !== null && time - lastDeliveryTime < .25) return;
      const event = { type: 'progress', stage: 'delivering', downloadedBytes: received, totalBytes: metadata.size, measurement: 'browser' };
      const elapsed = time - (lastDeliveryTime ?? deliveryStarted);
      if (elapsed > 0) event.speedBytesPerSecond = (received - lastDeliveryBytes) / elapsed;
      lastDeliveryTime = time;
      lastDeliveryBytes = received;
      notify(event);
    }
    function consume() {
      check();
      if (complete) throw invalid();
      if (kind === 2) {
        if (!metadata || received + payload.length > metadata.size || received + payload.length > maxBytes) throw invalid();
        received += payload.length;
        chunks.push(payload);
        reportDelivery();
        return;
      }
      let event;
      try { event = JSON.parse(decoder.decode(payload)); } catch (_) { throw invalid(); }
      if (!event || typeof event !== 'object' || Array.isArray(event) || ++events > 10000) throw invalid();
      if (event.type === 'heartbeat') return;
      if (event.type === 'progress') {
        if (!STAGES.has(event.stage)) throw invalid();
        if (event.stage === 'ready' && (!metadata || received !== metadata.size)) throw invalid();
        const progress = { type: 'progress', stage: event.stage };
        for (const key of COUNTERS) {
          if (event[key] === undefined) continue;
          if (typeof event[key] !== 'number' || !Number.isFinite(event[key]) || event[key] < 0 || event[key] > Number.MAX_SAFE_INTEGER) throw invalid();
          progress[key] = event[key];
        }
        // Readiness is confirmed only by the mandatory footer and clean EOF.
        // Transfer-to-browser counters are measured locally below, not from
        // bytes merely queued by the server's ASGI writer.
        if (event.stage !== 'ready' && event.stage !== 'delivering') notify(progress);
        return;
      }
      if (event.type === 'error') {
        const error = new Error(validText(event.error, 512) ? event.error : 'Não foi possível concluir o download. Tente novamente.');
        error.code = typeof event.code === 'string' && /^[a-z_]{1,64}$/.test(event.code) ? event.code : 'download_failed';
        error.recovery = recovery(event.recovery);
        throw error;
      }
      if (event.type === 'file') {
        if (metadata || !Number.isSafeInteger(event.size) || event.size <= 0 || event.size > maxBytes
            || !validText(event.name, 240) || /[\\/]/.test(event.name) || !validText(event.title, 160) || !MIMES.has(event.mime)) throw invalid();
        metadata = { type: 'file', name: event.name, title: event.title, mime: event.mime, size: event.size, recovery: recovery(event.recovery) };
        deliveryStarted = now();
        if (event.resolution !== undefined) {
          if (!Number.isInteger(event.resolution) || event.resolution <= 0 || event.resolution > 2160) throw invalid();
          metadata.resolution = event.resolution;
        }
        notify(metadata);
        return;
      }
      if (event.type === 'complete') {
        if (!metadata || !Number.isSafeInteger(event.size) || event.size !== metadata.size || received !== event.size) throw invalid();
        complete = true;
        return;
      }
      throw invalid();
    }
    try {
      while (true) {
        check();
        const next = await reader.read();
        check();
        if (next.done) break;
        if (!(next.value instanceof Uint8Array)) throw invalid();
        let offset = 0;
        while (offset < next.value.length) {
          if (complete) throw invalid();
          if (!payload) {
            const amount = Math.min(5 - headerUsed, next.value.length - offset);
            header.set(next.value.subarray(offset, offset + amount), headerUsed);
            headerUsed += amount;
            offset += amount;
            if (headerUsed !== 5) continue;
            kind = header[0];
            const length = new DataView(header.buffer).getUint32(1, false);
            if (!length || kind !== 1 && kind !== 2 || length > (kind === 1 ? MAX_JSON : MAX_BINARY)
                || kind === 2 && (!metadata || received + length > metadata.size || received + length > maxBytes)) throw invalid();
            payload = new Uint8Array(length);
            payloadUsed = 0;
          }
          const amount = Math.min(payload.length - payloadUsed, next.value.length - offset);
          payload.set(next.value.subarray(offset, offset + amount), payloadUsed);
          payloadUsed += amount;
          offset += amount;
          if (payloadUsed === payload.length) {
            consume();
            payload = null;
            payloadUsed = 0;
            headerUsed = 0;
          }
        }
      }
      check();
      if (!complete || headerUsed || payload || !metadata || received !== metadata.size) throw invalid();
      const blob = new Blob(chunks, { type: metadata.mime });
      check();
      if (blob.size !== metadata.size) throw invalid();
      reportDelivery(true);
      notify({ type: 'progress', stage: 'ready', downloadedBytes: received, totalBytes: metadata.size, measurement: 'browser' });
      succeeded = true;
      return { blob, metadata };
    } finally {
      signal?.removeEventListener('abort', abort);
      if (!succeeded) {
        chunks.length = 0;
        try { await reader.cancel(); } catch (_) { /* Preserve the original failure. */ }
      }
      reader.releaseLock();
    }
  }
  window.OndaDownloadStream = Object.freeze({ receive, CONTENT_TYPE });
})();
