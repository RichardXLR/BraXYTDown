/* Cookies are retained only on this device. Account state APIs never see this vault. */
(() => {
  'use strict';

  const DATABASE = 'onda.session.v1';
  const STORE = 'accounts';
  const VERSION = 1;
  const MAX_BYTES = 64 * 1024;
  const MAX_FILENAME_BYTES = 512;
  const encoder = new TextEncoder();
  const decoder = new TextDecoder('utf-8', { fatal: true });
  const scopes = new Map();
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

  function validatePayload(cookies, filename) {
    if (typeof cookies !== 'string' || !cookies || !wellFormed(cookies)
      || encoder.encode(cookies).byteLength > MAX_BYTES
      || typeof filename !== 'string' || filename.length > 180 || !wellFormed(filename)
      || /[\u0000-\u001f\u007f-\u009f]/.test(filename)
      || encoder.encode(filename).byteLength > MAX_FILENAME_BYTES) throw failure('invalid');
    return { cookies, filename };
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
      && sealed.ciphertext.byteLength <= MAX_BYTES * 6 + MAX_FILENAME_BYTES * 6 + 64;
  }

  function checkedRecord(record, id) {
    if (!recordIsValid(record, id)) throw failure('corrupt');
    return record;
  }

  function connect() {
    if (databasePromise) return databasePromise;
    databasePromise = new Promise((resolve, reject) => {
      let request;
      let finished = false;
      const timer = setTimeout(() => finish(null, failure('storage')), 10000);
      function finish(database, error) {
        if (finished) { database?.close(); return; }
        finished = true;
        clearTimeout(timer);
        if (error) reject(error); else resolve(database);
      }
      try { request = indexedDB.open(DATABASE, VERSION); }
      catch { finish(null, failure('storage')); return; }
      request.onupgradeneeded = () => {
        if (!request.result.objectStoreNames.contains(STORE)) {
          request.result.createObjectStore(STORE, { keyPath: 'id' });
        }
      };
      request.onsuccess = () => {
        const database = request.result;
        database.onversionchange = () => { database.close(); databasePromise = null; };
        database.onclose = () => { databasePromise = null; };
        finish(database);
      };
      request.onerror = () => finish(null, failure('storage'));
      request.onblocked = () => finish(null, failure('storage'));
    });
    databasePromise.catch(() => { databasePromise = null; });
    return databasePromise;
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

  function enqueue(state, operation) {
    const pending = state.tail.then(operation, operation);
    // The shared queue must never retain load()'s decrypted return value.
    state.tail = pending.then(() => undefined, () => undefined);
    return pending.catch(error => { throw sanitized(error); });
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
    let database;
    try {
      const hash = await crypto.subtle.digest('SHA-256', encoder.encode(`${DATABASE}:account:${accountId}`));
      id = [...new Uint8Array(hash)].map(value => value.toString(16).padStart(2, '0')).join('');
      database = await connect();
      await transaction(database, id, 'readwrite', (record, objectStore) => {
        if (record === undefined) { objectStore.put(emptyRecord(id)); return; }
        // Keep an unreadable record intact until the user explicitly removes it.
        // Opening still returns a handle so remove() can repair corrupted storage.
      });
    } catch (error) { throw sanitized(error); }

    let state = scopes.get(id);
    if (!state) {
      state = { tail: Promise.resolve(), removal: 0 };
      scopes.set(id, state);
    }
    let closed = false;
    const assertOpen = () => { if (closed) throw failure('closed'); };
    const assertCurrent = generation => {
      assertOpen();
      if (state.removal !== generation) throw failure('removed');
    };
    const read = async () => checkedRecord(await transaction(database, id, 'readonly'), id);

    return Object.freeze({
      load() {
        try { assertOpen(); } catch (error) { return Promise.reject(error); }
        const generation = state.removal;
        return enqueue(state, async () => {
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
              if (!plaintext || Object.keys(plaintext).length !== 2) throw failure('corrupt');
              validatePayload(plaintext.cookies, plaintext.filename);
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

      save(cookies, filename = '') {
        let payload;
        try { assertOpen(); payload = validatePayload(cookies, filename); }
        catch (error) { return Promise.reject(sanitized(error)); }
        const generation = state.removal;
        // Capture the removal epoch immediately, even while an older operation is queued.
        const baseline = read();
        baseline.catch(() => {});
        return enqueue(state, async () => {
          const start = await baseline;
          assertCurrent(generation);
          const current = await read();
          assertCurrent(generation);
          if (current.epoch !== start.epoch) throw failure('removed');
          const key = current.key || await crypto.subtle.generateKey(
            { name: 'AES-GCM', length: 256 }, false, ['encrypt', 'decrypt']);
          assertCurrent(generation);
          const iv = crypto.getRandomValues(new Uint8Array(12));
          const ciphertext = await crypto.subtle.encrypt({ name: 'AES-GCM', iv,
            additionalData: aad(id, start.epoch) }, key, encoder.encode(JSON.stringify(payload)));
          assertCurrent(generation);
          await transaction(database, id, 'readwrite', (record, objectStore) => {
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
        return enqueue(state, async () => {
          await transaction(database, id, 'readwrite', (_record, objectStore) => {
            // Removal also repairs an unreadable record; old ciphertext and its key disappear together.
            objectStore.put(emptyRecord(id));
          });
        });
      },

      close() { closed = true; },
    });
  }

  window.OndaCookieStore = Object.freeze({ open });
})();
