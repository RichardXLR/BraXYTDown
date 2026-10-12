(() => {
  'use strict';
  const MEMORY_LIMIT = 100 * 1024 * 1024;
  const DISK_LIMIT = 4 * 1024 ** 3;
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
      const quota = await navigator.storage.estimate();
      if (Number.isFinite(quota.quota) && metadata.size + 16 * 1024 * 1024 > quota.quota - (quota.usage || 0)) throw limitError('Não há espaço suficiente no armazenamento do navegador. Libere espaço e tente novamente.');
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
  window.OndaDownloadStorage = Object.freeze({ create, MEMORY_LIMIT, DISK_LIMIT });
})();
