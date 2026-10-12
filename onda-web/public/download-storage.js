(() => {
  'use strict';
  const MEMORY_LIMIT = 100 * 1024 * 1024;
  const DISK_LIMIT = 4 * 1024 ** 3;
  const SPACE_RESERVE = 16 * 1024 * 1024;
  const validBytes = value => Number.isSafeInteger(value) && value >= 0;

  // StorageManager describes this origin's quota, not physical disk space.
  // Private modes and some browsers do not expose a useful estimate.
  async function capacity({ timeoutMs = 1200 } = {}) {
    const storage = navigator.storage;
    if (typeof storage?.getDirectory !== 'function') return { kind: 'memory', availableBytes: null };
    const unknown = { kind: 'unknown', availableBytes: null };
    if (typeof storage.estimate !== 'function') return unknown;
    let timer;
    try {
      const quota = await Promise.race([
        Promise.resolve().then(() => storage.estimate()),
        new Promise(resolve => { timer = setTimeout(() => resolve(null), Math.max(10, Math.min(2000, timeoutMs || 1200))); }),
      ]);
      if (!quota || !validBytes(quota.quota) || !validBytes(quota.usage)) return unknown;
      return { kind: 'disk', availableBytes: Math.max(0, quota.quota - quota.usage) };
    } catch { return unknown; }
    finally { clearTimeout(timer); }
  }

  function assess(estimate, capacity) {
    const kind = ['disk', 'memory'].includes(capacity?.kind) ? capacity.kind : 'unknown';
    const availableBytes = kind === 'disk' && validBytes(capacity.availableBytes) ? capacity.availableBytes : null;
    const diskBudget = availableBytes === null ? null : Math.min(DISK_LIMIT, Math.max(0, availableBytes - SPACE_RESERVE));
    // Small outputs can still use the existing bounded memory fallback when
    // the origin quota is exhausted. RAM availability itself is not measurable.
    const budgetBytes = kind === 'memory' ? MEMORY_LIMIT : diskBudget === null ? null : Math.max(MEMORY_LIMIT, diskBudget);
    const known = estimate?.state === 'ready' && validBytes(estimate.minBytes) && estimate.minBytes > 0
      && validBytes(estimate.maxBytes) && estimate.maxBytes >= estimate.minBytes;
    const state = !known || budgetBytes === null ? 'unknown' : estimate.minBytes > budgetBytes ? 'insufficient'
      : estimate.maxBytes > budgetBytes ? 'risk' : 'available';
    const route = diskBudget !== null && known && estimate.maxBytes <= diskBudget ? 'disk'
      : known && estimate.maxBytes <= MEMORY_LIMIT && kind !== 'unknown' ? 'memory' : kind;
    return { state, kind, route, availableBytes, budgetBytes };
  }
  function limitError(message) { const error = new Error(message); error.code = 'memory_limit'; return error; }
  function memorySink(metadata) {
    if (metadata.size > MEMORY_LIMIT) throw limitError('Este navegador não disponibilizou espaço em disco. Para arquivos grandes, use um navegador com armazenamento privado de arquivos.');
    let parts = [], bytes = 0, closed = false;
    return { kind: 'memory', async write(chunk) {
      if (closed || bytes + chunk.length > metadata.size) throw new Error('A transferência retornou um tamanho inválido.');
      bytes += chunk.length;
      if (bytes > MEMORY_LIMIT) throw limitError('A memória segura deste navegador foi atingida.');
      parts.push(chunk);
    }, async commit() {
      if (closed || bytes !== metadata.size) throw new Error('A transferência ficou incompleta.');
      const blob = new Blob(parts, { type: metadata.mime });
      parts = []; closed = true;
      return blob;
    }, async abort() { parts = []; closed = true; } };
  }
  async function create(metadata, { userId, ensureOperation = () => {} } = {}) {
    ensureOperation();
    if (!Number.isSafeInteger(metadata.size) || metadata.size <= 0 || metadata.size > DISK_LIMIT) throw new Error('O tamanho do arquivo não é compatível com este dispositivo.');
    if (!userId || !navigator.storage?.getDirectory) return memorySink(metadata);
    const account = Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256', new TextEncoder().encode(userId)))).map(v => v.toString(16).padStart(2, '0')).join('');
    ensureOperation();
    let directory, handle, writer, name;
    try {
      const space = await capacity();
      ensureOperation();
      if (space.kind === 'disk' && metadata.size + SPACE_RESERVE > space.availableBytes) throw limitError('Não há espaço suficiente no armazenamento do navegador. Libere espaço e tente novamente.');
      directory = await (await navigator.storage.getDirectory()).getDirectoryHandle('onda-downloads', { create: true });
      directory = await directory.getDirectoryHandle(account, { create: true });
      // Only stale random files from this account are eligible for cleanup.
      for await (const [entryName, entry] of directory.entries()) {
        if (/^\d+-[a-f0-9-]+\.part$/.test(entryName) && Date.now() - Number(entryName.split('-')[0]) > 24 * 60 * 60 * 1000 && entry.kind === 'file') await directory.removeEntry(entryName).catch(() => {});
      }
      name = `${Date.now()}-${crypto.randomUUID()}.part`;
      handle = await directory.getFileHandle(name, { create: true });
      writer = await handle.createWritable();
      ensureOperation();
    } catch (error) {
      await writer?.abort().catch(() => {});
      if (directory && name) await directory.removeEntry(name).catch(() => {});
      ensureOperation();
      if (metadata.size > MEMORY_LIMIT) throw error;
      return memorySink(metadata);
    }
    let closed = false, bytes = 0, buffered = 0;
    const buffer = new Uint8Array(1024 * 1024);
    const abort = async () => {
      if (!closed) { closed = true; await writer.abort().catch(() => {}); }
      await directory.removeEntry(name).catch(() => {});
    };
    return { kind: 'disk', async write(chunk) {
      ensureOperation();
      if (closed || bytes + chunk.length > metadata.size) throw new Error('A transferência retornou um tamanho inválido.');
      let offset = 0;
      while (offset < chunk.length) {
        const size = Math.min(buffer.length - buffered, chunk.length - offset);
        buffer.set(chunk.subarray(offset, offset + size), buffered);
        buffered += size; offset += size;
        if (buffered === buffer.length) {
          await writer.write(buffer);
          ensureOperation(); buffered = 0;
        }
      }
      bytes += chunk.length;
    }, async commit() {
      ensureOperation();
      if (bytes !== metadata.size || closed) throw new Error('A transferência ficou incompleta.');
      if (buffered) await writer.write(buffer.subarray(0, buffered));
      ensureOperation();
      await writer.close();
      closed = true;
      ensureOperation();
      return handle.getFile();
    }, abort };
  }
  window.OndaDownloadStorage = Object.freeze({ create, capacity, assess, SPACE_RESERVE, MEMORY_LIMIT, DISK_LIMIT });
})();
