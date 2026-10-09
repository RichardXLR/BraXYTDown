/* Cookies are retained only on this device. Account state APIs never see this vault. */
(() => {
  'use strict';

  const DATABASE = 'onda.session.v1';
  const STORE = 'accounts';
  const VERSION = 1;
  const MAX_BYTES = 64 * 1024;
  const MAX_FILENAME_BYTES = 512;
  const MAX_METADATA_BYTES = 2048;
  const encoder = new TextEncoder();
  const decoder = new TextDecoder('utf-8', { fatal: true });
  const scopes = new Map();
  const connections = new WeakMap();
  let databasePromise = null;
  let persistenceRequested = false;

  const messages = Object.freeze({
    unsupported: 'Este navegador não permite salvar a sessão com segurança. Os cookies podem ser usados temporariamente.',
    storage: 'Não foi possível acessar os cookies salvos neste dispositivo. Verifique as permissões de armazenamento do navegador.',
    invalid: 'Os cookies ou o nome do arquivo excedem o tamanho permitido.',
    corrupt: 'Não foi possível ler os cookies salvos. Remova a sessão e importe os cookies novamente.',
    closed: 'A sessão local foi fechada.',
    removed: 'Os cookies foram removidos. Uma gravação anterior foi cancelada.',
    changed: 'A sessão foi alterada em outra aba. Tente novamente.',
  });

  function failure(code) {
    const error = new Error(messages[code] || messages.storage);
    error.name = 'OndaCookieStoreError';
    error.code = Object.hasOwn(messages, code) ? code : 'storage';
    return error;
  }

  function sanitized(error, fallback = 'storage') {
    return error?.name === 'OndaCookieStoreError' && Object.hasOwn(messages, error.code)
      ? failure(error.code) : failure(fallback);
  }

  function wellFormed(value) {
    for (let i = 0; i < value.length; i += 1) {
      const character = value.charCodeAt(i);
      if (character >= 0xd800 && character <= 0xdbff) {
        const next = value.charCodeAt(++i);
        if (!(next >= 0xdc00 && next <= 0xdfff)) return false;
      } else if (character >= 0xdc00 && character <= 0xdfff) return false;
    }
    return true;
  }

  function validateMetadata(metadata) {
    if (metadata === null || metadata === undefined) return null;
    const exactKeys = (value, keys) => value && typeof value === 'object' && !Array.isArray(value)
      && Object.keys(value).length === keys.length && keys.every(key => Object.hasOwn(value, key));
    const timestamp = value => Number.isInteger(value) && value > 0 && value <= 4102444800000;
    const count = (value, minimum = 0) => Number.isInteger(value) && value >= minimum && value <= 300;
    if (!exactKeys(metadata, ['version', 'contentHash', 'savedAt', 'validation']) || metadata.version !== 1
      || typeof metadata.contentHash !== 'string' || !/^[0-9a-f]{64}$/.test(metadata.contentHash)
      || !timestamp(metadata.savedAt)) throw failure('invalid');
    let validation = null;
    if (metadata.validation !== null) {
      const value = metadata.validation;
      const keys = value?.status === 'valid'
        ? ['checkedAt', 'scope', 'status', 'validCookies', 'expiredCookies', 'ignoredCookies', 'sessionCookies', 'earliestExpiry']
        : ['checkedAt', 'scope', 'status', 'code'];
      if (!exactKeys(value, keys) || !timestamp(value.checkedAt) || typeof value.scope !== 'string'
        || value.scope.length > 253 || !/^(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z0-9-]{2,63}$/.test(value.scope)) throw failure('invalid');
      if (value.status === 'valid') {
        if (!count(value.validCookies, 1) || !count(value.expiredCookies) || !count(value.ignoredCookies)
          || !count(value.sessionCookies) || value.sessionCookies > value.validCookies
          || !(value.earliestExpiry === null || (Number.isInteger(value.earliestExpiry)
            && value.earliestExpiry >= 0 && value.earliestExpiry <= 999999999999))) throw failure('invalid');
      } else if (value.status !== 'invalid' || !['invalid_cookies', 'cookie_domain', 'cookies_expired',
        'cookie_https_required', 'cookies_not_supported', 'timeout', 'validation_failed'].includes(value.code)) throw failure('invalid');
      validation = Object.fromEntries(keys.map(key => [key, value[key]]));
    }
    const clean = { version: 1, contentHash: metadata.contentHash, savedAt: metadata.savedAt, validation };
    if (encoder.encode(JSON.stringify(clean)).byteLength > MAX_METADATA_BYTES) throw failure('invalid');
    return clean;
  }

  function validatePayload(cookies, filename, metadata = null) {
    if (typeof cookies !== 'string' || !cookies || !wellFormed(cookies)
      || encoder.encode(cookies).byteLength > MAX_BYTES
      || typeof filename !== 'string' || filename.length > 180 || !wellFormed(filename)
      || /[\u0000-\u001f\u007f-\u009f]/.test(filename)
      || encoder.encode(filename).byteLength > MAX_FILENAME_BYTES) throw failure('invalid');
    const cleanMetadata = validateMetadata(metadata);
    return { cookies, filename, ...(cleanMetadata ? { metadata: cleanMetadata } : {}) };
  }

  async function metadataMatches(payload) {
    if (!payload.metadata) return true;
    let normalized = payload.cookies.trim();
    if (normalized.startsWith('{')) {
      try {
        const records = JSON.parse(normalized)?.cookies;
        if (Array.isArray(records)) normalized = JSON.stringify(records);
      } catch {}
    }
    const digest = await crypto.subtle.digest('SHA-256', encoder.encode(normalized));
    const hash = [...new Uint8Array(digest)].map(value => value.toString(16).padStart(2, '0')).join('');
    return hash === payload.metadata.contentHash;
  }

  function nonce() {
    return [...crypto.getRandomValues(new Uint8Array(16))]
      .map(value => value.toString(16).padStart(2, '0')).join('');
  }

  function emptyRecord(id) {
    return { id, version: VERSION, epoch: nonce(), revision: nonce(), key: null, sealed: null };
  }

  function recordIsValid(record, id) {
    if (!record || record.id !== id || record.version !== VERSION
      || !/^[0-9a-f]{32}$/.test(record.epoch) || !/^[0-9a-f]{32}$/.test(record.revision)) return false;
    if (record.key === null && record.sealed === null) return true;
    const { key, sealed } = record;
    return key instanceof CryptoKey && key.type === 'secret' && key.extractable === false
      && key.algorithm.name === 'AES-GCM' && key.algorithm.length === 256
      && key.usages.length === 2 && key.usages.includes('encrypt') && key.usages.includes('decrypt')
      && sealed && sealed.iv instanceof ArrayBuffer && sealed.iv.byteLength === 12
      && sealed.ciphertext instanceof ArrayBuffer && sealed.ciphertext.byteLength >= 16
      && sealed.ciphertext.byteLength <= MAX_BYTES * 6 + MAX_FILENAME_BYTES * 6 + MAX_METADATA_BYTES + 64;
  }

  function checkedRecord(record, id) {
    if (!recordIsValid(record, id)) throw failure('corrupt');
    return record;
  }

  function invalidateConnection(database) {
    // A delayed event from an old connection must not discard its replacement.
    if (databasePromise === connections.get(database)) databasePromise = null;
  }

  function connect() {
    if (databasePromise) return databasePromise;
    const pending = new Promise((resolve, reject) => {
      let request;
      let finished = false;
      const timer = setTimeout(() => finish(null, failure('storage')), 10000);
      function finish(database, error) {
        if (finished) { database?.close(); return; }
        finished = true;
        clearTimeout(timer);
        if (error) reject(error); else resolve(database);
      }
      // Existing, compatible schema upgrades do not invalidate encrypted v1 records.
      // Omitting the version avoids trying to downgrade another tab's database.
      try { request = indexedDB.open(DATABASE); }
      catch { finish(null, failure('storage')); return; }
      request.onupgradeneeded = () => {
        if (finished) { try { request.transaction.abort(); } catch {} return; }
        if (!request.result.objectStoreNames.contains(STORE)) {
          request.result.createObjectStore(STORE, { keyPath: 'id' });
        }
      };
      request.onsuccess = () => {
        const database = request.result;
        if (finished) { database.close(); return; }
        try {
          if (!database.objectStoreNames.contains(STORE)
            || database.transaction(STORE, 'readonly').objectStore(STORE).keyPath !== 'id') {
            database.close(); finish(null, failure('storage')); return;
          }
        } catch { database.close(); finish(null, failure('storage')); return; }
        connections.set(database, pending);
        database.onversionchange = () => { invalidateConnection(database); database.close(); };
        database.onclose = () => invalidateConnection(database);
        finish(database);
      };
      request.onerror = () => finish(null, failure('storage'));
      request.onblocked = () => finish(null, failure('storage'));
    });
    databasePromise = pending;
    pending.catch(() => { if (databasePromise === pending) databasePromise = null; });
    return pending;
  }

  // Mutators are synchronous: encryption finishes before opening a write transaction.
  function transaction(database, id, mode, mutate = null) {
    return new Promise((resolve, reject) => {
      let tx;
      let result;
      let error = null;
      let timer;
      try {
        tx = mode === 'readwrite'
          ? database.transaction(STORE, mode, { durability: 'strict' })
          : database.transaction(STORE, mode);
      } catch (caught) {
        // Retrying is safe only before a transaction (and therefore any mutation) starts.
        reject(caught?.name === 'InvalidStateError' ? caught : sanitized(caught));
        return;
      }
      try {
        timer = setTimeout(() => { error = failure('storage'); try { tx.abort(); } catch {} }, 10000);
        const objectStore = tx.objectStore(STORE);
        const request = objectStore.get(id);
        request.onsuccess = () => {
          try {
            result = mutate ? mutate(request.result, objectStore) : request.result;
          } catch (caught) {
            error = sanitized(caught);
            tx.abort();
          }
        };
        tx.oncomplete = () => { clearTimeout(timer); resolve(result); };
        tx.onabort = tx.onerror = () => { clearTimeout(timer); reject(error || failure('storage')); };
      } catch (caught) {
        clearTimeout(timer);
        reject(sanitized(caught));
      }
    });
  }

  async function access(id, mode, mutate = null) {
    for (let attempt = 0; attempt < 2; attempt += 1) {
      const database = await connect();
      try { return await transaction(database, id, mode, mutate); }
      catch (error) {
        if (error?.name !== 'InvalidStateError' || attempt > 0) throw sanitized(error);
        invalidateConnection(database);
      }
    }
  }

  function releaseScope(id, state) {
    if (state.handles === 0 && state.pending === 0 && scopes.get(id) === state) scopes.delete(id);
  }

  function enqueue(id, state, operation) {
    state.pending += 1;
    const pending = state.tail.then(operation, operation);
    // The shared queue must never retain load()'s decrypted return value.
    state.tail = pending.then(() => undefined, () => undefined);
    return pending.then(result => {
      state.pending -= 1; releaseScope(id, state); return result;
    }, error => {
      state.pending -= 1; releaseScope(id, state); throw sanitized(error);
    });
  }

  function aad(id, epoch) {
    return encoder.encode(`${DATABASE}:${id}:v${VERSION}:${epoch}`);
  }

  function requestPersistence() {
    if (persistenceRequested || !navigator.storage?.persist) return;
    persistenceRequested = true;
    // Browsers decide whether to protect storage from automatic eviction.
    // Their decision never changes whether this write succeeded.
    try { Promise.resolve(navigator.storage.persist()).catch(() => {}); } catch {}
  }

  async function open(accountId) {
    if (!globalThis.isSecureContext || !globalThis.indexedDB || !globalThis.crypto?.subtle
      || typeof globalThis.CryptoKey !== 'function') throw failure('unsupported');
    if (typeof accountId !== 'string' || !accountId.trim() || accountId.length > 256
      || !wellFormed(accountId)) throw failure('invalid');
    let id;
    let state;
    try {
      const hash = await crypto.subtle.digest('SHA-256', encoder.encode(`${DATABASE}:account:${accountId}`));
      id = [...new Uint8Array(hash)].map(value => value.toString(16).padStart(2, '0')).join('');
      state = scopes.get(id);
      if (!state) {
        state = { tail: Promise.resolve(), removal: 0, handles: 0, pending: 0 };
        scopes.set(id, state);
      }
      state.handles += 1;
      await access(id, 'readwrite', (record, objectStore) => {
        if (record === undefined) { objectStore.put(emptyRecord(id)); return; }
        // Keep an unreadable record intact until the user explicitly removes it.
        // Opening still returns a handle so remove() can repair corrupted storage.
      });
    } catch (error) {
      if (state) { state.handles -= 1; releaseScope(id, state); }
      throw sanitized(error);
    }
    let closed = false;
    const assertOpen = () => { if (closed) throw failure('closed'); };
    const assertCurrent = generation => {
      assertOpen();
      if (state.removal !== generation) throw failure('removed');
    };
    const read = async () => checkedRecord(await access(id, 'readonly'), id);

    return Object.freeze({
      load() {
        try { assertOpen(); } catch (error) { return Promise.reject(error); }
        const generation = state.removal;
        return enqueue(id, state, async () => {
          assertCurrent(generation);
          for (let attempt = 0; attempt < 3; attempt += 1) {
            const record = await read();
            assertCurrent(generation);
            if (record.sealed === null) return null;
            let plaintext;
            try {
              const decoded = await crypto.subtle.decrypt({ name: 'AES-GCM', iv: record.sealed.iv,
                additionalData: aad(id, record.epoch) }, record.key, record.sealed.ciphertext);
              assertCurrent(generation);
              plaintext = JSON.parse(decoder.decode(decoded));
              if (!plaintext || ![2, 3].includes(Object.keys(plaintext).length)
                || !Object.hasOwn(plaintext, 'cookies') || !Object.hasOwn(plaintext, 'filename')
                || (Object.keys(plaintext).length === 3 && !Object.hasOwn(plaintext, 'metadata'))) throw failure('corrupt');
              plaintext = validatePayload(plaintext.cookies, plaintext.filename, plaintext.metadata);
              if (!await metadataMatches(plaintext)) throw failure('corrupt');
              assertCurrent(generation);
            } catch (error) {
              assertCurrent(generation);
              throw sanitized(error, 'corrupt');
            }
            const current = await read();
            assertCurrent(generation);
            if (current.epoch !== record.epoch || current.sealed === null) return null;
            if (current.revision === record.revision) return Object.freeze(plaintext);
          }
          throw failure('changed');
        });
      },

      save(cookies, filename = '', metadata = null) {
        let payload;
        try { assertOpen(); payload = validatePayload(cookies, filename, metadata); }
        catch (error) { return Promise.reject(sanitized(error)); }
        const generation = state.removal;
        // Capture the removal epoch immediately, even while an older operation is queued.
        const baseline = read();
        baseline.catch(() => {});
        return enqueue(id, state, async () => {
          const start = await baseline;
          assertCurrent(generation);
          const current = await read();
          assertCurrent(generation);
          if (current.epoch !== start.epoch) throw failure('removed');
          if (!await metadataMatches(payload)) throw failure('invalid');
          assertCurrent(generation);
          const key = current.key || await crypto.subtle.generateKey(
            { name: 'AES-GCM', length: 256 }, false, ['encrypt', 'decrypt']);
          assertCurrent(generation);
          const iv = crypto.getRandomValues(new Uint8Array(12));
          const ciphertext = await crypto.subtle.encrypt({ name: 'AES-GCM', iv,
            additionalData: aad(id, start.epoch) }, key, encoder.encode(JSON.stringify(payload)));
          assertCurrent(generation);
          await access(id, 'readwrite', (record, objectStore) => {
            assertCurrent(generation);
            checkedRecord(record, id);
            if (record.epoch !== start.epoch) throw failure('removed');
            objectStore.put({ ...record, key, sealed: { iv: iv.buffer, ciphertext }, revision: nonce() });
          });
          assertCurrent(generation);
          requestPersistence();
        });
      },

      remove() {
        try { assertOpen(); } catch (error) { return Promise.reject(error); }
        state.removal += 1;
        return enqueue(id, state, async () => {
          await access(id, 'readwrite', (_record, objectStore) => {
            // Removal also repairs an unreadable record; old ciphertext and its key disappear together.
            objectStore.put(emptyRecord(id));
          });
        });
      },

      close() {
        if (closed) return;
        closed = true; state.handles -= 1; releaseScope(id, state);
      },
    });
  }

  window.OndaCookieStore = Object.freeze({ open });
})();
