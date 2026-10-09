"""Encrypted local cookie vault exercised with a real browser and IndexedDB.

No Clerk users, backend endpoints, third-party cookies, or external sites are used.
The release's API-only environment can skip these when Playwright is unavailable.
"""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import threading

import pytest


ROOT = Path(__file__).resolve().parents[1]
COOKIE = "# Netscape HTTP Cookie File\n.example.org\tTRUE\t/\tTRUE\t0\tsession\tfixture-cookie-sensitive-marker\n"
FILENAME = "fixture-private-filename.txt"


@pytest.fixture(scope="module")
def vault_browser():
    playwright = pytest.importorskip("playwright.sync_api")

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            content = ((ROOT / "public/session-store.js").read_bytes()
                       if self.path == "/session-store.js" else
                       b'<!doctype html><html><body><script src="/session-store.js"></script></body></html>')
            self.send_response(200)
            self.send_header("Content-Type", "application/javascript" if self.path.endswith(".js") else "text/html")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(content)

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    with playwright.sync_playwright() as instance:
        try:
            browser = instance.chromium.launch(headless=True)
        except playwright.Error:
            server.shutdown()
            pytest.skip("Playwright Chromium is not installed in this API-only environment")
        yield browser, f"http://127.0.0.1:{server.server_port}"
        browser.close()
    server.shutdown()
    server.server_close()
    thread.join(timeout=5)


@pytest.fixture
def vault_page(vault_browser):
    browser, origin = vault_browser
    with browser.new_context() as context:
        page = context.new_page()
        page.goto(origin)
        page.wait_for_function("() => Boolean(window.OndaCookieStore)")
        yield page, context, origin


def test_local_encryption_survives_reload_without_exportable_keys_or_plaintext(vault_page):
    page, _, _ = vault_page
    page.evaluate("""async data => {
      window.vault = await OndaCookieStore.open('fixture-account-a');
      await vault.save(data.cookies, data.filename);
    }""", {"cookies": COOKIE, "filename": FILENAME})
    properties = page.evaluate("""async markers => {
      const database = await new Promise((resolve, reject) => {
        const request = indexedDB.open('onda.session.v1');
        request.onsuccess = () => resolve(request.result); request.onerror = () => reject();
      });
      const records = await new Promise(resolve => {
        const request = database.transaction('accounts').objectStore('accounts').getAll();
        request.onsuccess = () => resolve(request.result);
      });
      const record = records[0];
      let exported = false;
      try { await crypto.subtle.exportKey('raw', record.key); exported = true; } catch {}
      const serialized = JSON.stringify(records);
      database.close();
      return {count: records.length, keyExtractable: record.key.extractable, exported,
        algorithm: record.key.algorithm.name, idHasAccountName: record.id.includes('fixture-account-a'),
        plaintextPresent: markers.some(marker => serialized.includes(marker)),
        localStorageEntries: localStorage.length, sessionStorageEntries: sessionStorage.length};
    }""", ["fixture-cookie-sensitive-marker", FILENAME])
    assert properties == {"count": 1, "keyExtractable": False, "exported": False,
                          "algorithm": "AES-GCM", "idHasAccountName": False, "plaintextPresent": False,
                          "localStorageEntries": 0, "sessionStorageEntries": 0}
    page.reload()
    restored = page.evaluate("""async () => {
      window.vault = await OndaCookieStore.open('fixture-account-a'); return vault.load();
    }""")
    assert restored == {"cookies": COOKIE, "filename": FILENAME}


def test_account_isolation_close_and_explicit_removal(vault_page):
    page, _, _ = vault_page
    result = page.evaluate("""async cookies => {
      const first = await OndaCookieStore.open('fixture-account-a');
      const second = await OndaCookieStore.open('fixture-account-b');
      await first.save(cookies); await second.save('other-account-cookie'); first.close();
      let closed;
      try { await first.load(); } catch (error) { closed = error.code; }
      const reopened = await OndaCookieStore.open('fixture-account-a');
      const persisted = (await reopened.load()).cookies === cookies;
      await reopened.remove(); reopened.close();
      const final = await OndaCookieStore.open('fixture-account-a');
      return {closed, persisted, removed: await final.load() === null,
        otherPreserved: (await second.load()).cookies === 'other-account-cookie'};
    }""", COOKIE)
    assert result == {"closed": "closed", "persisted": True, "removed": True, "otherPreserved": True}


def test_removal_cancels_inflight_and_queued_saves_without_resurrection(vault_page):
    page, _, _ = vault_page
    result = page.evaluate("""async () => {
      const first = await OndaCookieStore.open('fixture-account-a');
      const second = await OndaCookieStore.open('fixture-account-a');
      let resume, reached;
      const original = crypto.subtle.encrypt.bind(crypto.subtle);
      const started = new Promise(resolve => { reached = resolve; });
      crypto.subtle.encrypt = async (...args) => {
        reached(); await new Promise(resolve => { resume = resolve; }); return original(...args);
      };
      const saving = first.save('old-inflight-cookie').then(() => 'saved', error => error.code);
      await started;
      const queued = second.save('old-queued-cookie').then(() => 'saved', error => error.code);
      const removing = second.remove(); resume();
      const outcomes = await Promise.all([saving, queued]); await removing;
      crypto.subtle.encrypt = original;
      const absent = await first.load() === null;
      await first.save('new-explicit-cookie');
      return {outcomes, absent, replacement: (await second.load()).cookies};
    }""")
    assert result == {"outcomes": ["removed", "removed"], "absent": True, "replacement": "new-explicit-cookie"}


def test_removal_in_another_tab_invalidates_an_older_encryption(vault_page):
    page, context, origin = vault_page
    page.evaluate("""async () => {
      window.vault = await OndaCookieStore.open('fixture-account-a');
      const original = crypto.subtle.encrypt.bind(crypto.subtle);
      crypto.subtle.encrypt = async (...args) => {
        window.encryptionStarted = true;
        await new Promise(resolve => { window.resumeEncryption = resolve; });
        return original(...args);
      };
      window.saving = vault.save('stale-tab-cookie').then(() => 'saved', error => error.code);
    }""")
    page.wait_for_function("() => window.encryptionStarted")
    other = context.new_page()
    other.goto(origin)
    other.evaluate("""async () => {
      const vault = await OndaCookieStore.open('fixture-account-a'); await vault.remove(); vault.close();
    }""")
    result = page.evaluate("""async () => {
      resumeEncryption(); const outcome = await saving; return {outcome, absent: await vault.load() === null};
    }""")
    assert result == {"outcome": "removed", "absent": True}


def test_closed_handle_never_returns_a_pending_decrypted_cookie(vault_page):
    page, _, _ = vault_page
    result = page.evaluate("""async () => {
      const vault = await OndaCookieStore.open('fixture-account-a'); await vault.save('account-private-cookie');
      let resume, reached;
      const original = crypto.subtle.decrypt.bind(crypto.subtle);
      const started = new Promise(resolve => { reached = resolve; });
      crypto.subtle.decrypt = async (...args) => {
        reached(); await new Promise(resolve => { resume = resolve; }); return original(...args);
      };
      const loading = vault.load().then(() => 'leaked', error => error.code);
      await started; vault.close(); resume();
      const outcome = await loading; crypto.subtle.decrypt = original;
      const reopened = await OndaCookieStore.open('fixture-account-a');
      return {outcome, preserved: (await reopened.load()).cookies === 'account-private-cookie'};
    }""")
    assert result == {"outcome": "closed", "preserved": True}


def test_remove_cancels_pending_restore_and_rotates_the_key(vault_page):
    page, _, _ = vault_page
    result = page.evaluate("""async () => {
      const vault = await OndaCookieStore.open('fixture-account-a'); await vault.save('previous-cookie');
      let resume, reached;
      const original = crypto.subtle.decrypt.bind(crypto.subtle);
      const started = new Promise(resolve => { reached = resolve; });
      crypto.subtle.decrypt = async (...args) => {
        reached(); await new Promise(resolve => { resume = resolve; }); return original(...args);
      };
      const loading = vault.load().then(() => 'leaked', error => error.code);
      await started; const removing = vault.remove(); resume();
      const outcome = await loading; await removing; crypto.subtle.decrypt = original;
      return {outcome, absent: await vault.load() === null};
    }""")
    assert result == {"outcome": "removed", "absent": True}


def test_concurrent_handles_keep_newest_save_and_use_fresh_gcm_nonces(vault_page):
    page, _, _ = vault_page
    result = page.evaluate("""async () => {
      const first = await OndaCookieStore.open('fixture-account-a');
      const second = await OndaCookieStore.open('fixture-account-a');
      await Promise.all([first.save('older-cookie', 'older.txt'), second.save('newer-cookie', 'newer.txt')]);
      const final = await first.load();
      const database = await new Promise(resolve => {
        const request = indexedDB.open('onda.session.v1'); request.onsuccess = () => resolve(request.result);
      });
      const read = () => new Promise(resolve => {
        const request = database.transaction('accounts').objectStore('accounts').getAll();
        request.onsuccess = () => resolve(request.result[0]);
      });
      const before = await read(); await second.save('newer-cookie', 'newer.txt'); const after = await read();
      database.close();
      return {final, nonceChanged: [...new Uint8Array(before.sealed.iv)].join() !== [...new Uint8Array(after.sealed.iv)].join(),
        ciphertextChanged: [...new Uint8Array(before.sealed.ciphertext)].join() !== [...new Uint8Array(after.sealed.ciphertext)].join()};
    }""")
    assert result == {"final": {"cookies": "newer-cookie", "filename": "newer.txt"},
                      "nonceChanged": True, "ciphertextChanged": True}


def test_corrupted_ciphertext_is_sanitized_and_explicit_removal_repairs_it(vault_page):
    page, _, _ = vault_page
    result = page.evaluate("""async () => {
      const vault = await OndaCookieStore.open('fixture-account-a'); await vault.save('fixture-cookie-sensitive-marker');
      const database = await new Promise(resolve => {
        const request = indexedDB.open('onda.session.v1'); request.onsuccess = () => resolve(request.result);
      });
      await new Promise(resolve => {
        const transaction = database.transaction('accounts', 'readwrite'); const store = transaction.objectStore('accounts');
        const request = store.getAll(); request.onsuccess = () => {
          const record = request.result[0]; new Uint8Array(record.sealed.ciphertext)[0] ^= 1; store.put(record);
        }; transaction.oncomplete = resolve;
      }); database.close();
      let error;
      try { await vault.load(); } catch (caught) { error = {code: caught.code, sensitive: caught.message.includes('fixture-cookie-sensitive-marker')}; }
      await vault.remove(); return {error, repaired: await vault.load() === null};
    }""")
    assert result == {"error": {"code": "corrupt", "sensitive": False}, "repaired": True}


def test_size_limits_count_utf8_bytes_and_reject_invalid_filenames(vault_page):
    page, _, _ = vault_page
    result = page.evaluate("""async () => {
      const vault = await OndaCookieStore.open('fixture-account-a');
      await vault.save('a'.repeat(65536));
      const accepted = (await vault.load()).cookies.length === 65536;
      const inputs = [['a'.repeat(65537), ''], ['á'.repeat(32769), ''], ['valid', 'x'.repeat(181)],
        ['valid', 'unsafe\\nfilename'], ['', ''], ['\\ud800', '']];
      const errors = [];
      for (const [cookie, filename] of inputs) {
        try { await vault.save(cookie, filename); errors.push('saved'); } catch (error) { errors.push(error.code); }
      }
      return {accepted, errors};
    }""")
    assert result == {"accepted": True, "errors": ["invalid"] * 6}


def test_storage_failures_are_sanitized_without_localstorage_fallback(vault_page):
    page, _, _ = vault_page
    result = page.evaluate("""async () => {
      indexedDB.open = () => { throw new Error('fixture-private-error-marker'); };
      let usedLocalStorage = false;
      Storage.prototype.setItem = () => { usedLocalStorage = true; throw new Error('unsafe fallback'); };
      try { await OndaCookieStore.open('fixture-account-a'); return {unexpected: true}; }
      catch (error) { return {code: error.code, exposed: error.message.includes('fixture-private-error-marker'), usedLocalStorage}; }
    }""")
    assert result == {"code": "storage", "exposed": False, "usedLocalStorage": False}


def test_missing_secure_storage_capabilities_never_use_plaintext_fallback(vault_page):
    page, _, _ = vault_page
    result = page.evaluate("""async () => {
      Object.defineProperty(window, 'isSecureContext', {value: false});
      try { await OndaCookieStore.open('fixture-account-a'); return {unexpected: true}; }
      catch (error) { return {code: error.code, localEntries: localStorage.length}; }
    }""")
    assert result == {"code": "unsupported", "localEntries": 0}


def test_close_cancels_pending_save_but_preserves_previous_saved_session(vault_page):
    page, _, _ = vault_page
    result = page.evaluate("""async () => {
      const vault = await OndaCookieStore.open('fixture-account-a'); await vault.save('previous-saved-cookie');
      let resume, reached;
      const original = crypto.subtle.encrypt.bind(crypto.subtle);
      const started = new Promise(resolve => { reached = resolve; });
      crypto.subtle.encrypt = async (...args) => {
        reached(); await new Promise(resolve => { resume = resolve; }); return original(...args);
      };
      const saving = vault.save('unfinished-cookie').then(() => 'saved', error => error.code);
      await started; vault.close(); resume(); const outcome = await saving; crypto.subtle.encrypt = original;
      const reopened = await OndaCookieStore.open('fixture-account-a');
      return {outcome, previous: (await reopened.load()).cookies};
    }""")
    assert result == {"outcome": "closed", "previous": "previous-saved-cookie"}


def test_corrupt_record_can_be_explicitly_removed_after_reopening(vault_page):
    page, _, _ = vault_page
    result = page.evaluate("""async () => {
      const vault = await OndaCookieStore.open('fixture-account-a'); await vault.save('previous-cookie'); vault.close();
      const database = await new Promise(resolve => {
        const request = indexedDB.open('onda.session.v1'); request.onsuccess = () => resolve(request.result);
      });
      await new Promise(resolve => {
        const transaction = database.transaction('accounts', 'readwrite'); const store = transaction.objectStore('accounts');
        const request = store.getAll(); request.onsuccess = () => {
          const record = request.result[0]; record.sealed.iv = new ArrayBuffer(2); store.put(record);
        }; transaction.oncomplete = resolve;
      }); database.close();
      const reopened = await OndaCookieStore.open('fixture-account-a');
      let error;
      try { await reopened.load(); } catch (caught) { error = caught.code; }
      const removing = reopened.remove(); reopened.close(); await removing;
      const fresh = await OndaCookieStore.open('fixture-account-a');
      return {error, removed: await fresh.load() === null};
    }""")
    assert result == {"error": "corrupt", "removed": True}


def test_browser_persistence_request_is_best_effort_and_not_a_save_failure(vault_page):
    page, _, _ = vault_page
    result = page.evaluate("""async () => {
      let requested = 0;
      navigator.storage.persist = () => { requested += 1; return Promise.reject(new Error('fixture-browser-policy')); };
      const vault = await OndaCookieStore.open('fixture-account-a');
      await vault.save('first-cookie'); await vault.save('second-cookie');
      return {requested, saved: (await vault.load()).cookies};
    }""")
    assert result == {"requested": 1, "saved": "second-cookie"}


def capture_vault_connections(page):
    page.add_init_script("""(() => {
      const original = indexedDB.open.bind(indexedDB);
      window.vaultConnections = [];
      window.vaultOpenRequests = 0;
      indexedDB.open = (...args) => {
        window.vaultOpenRequests += 1;
        const request = original(...args);
        request.addEventListener('success', () => window.vaultConnections.push(request.result));
        return request;
      };
    })();""")
    page.reload()


def test_existing_handle_reconnects_after_actual_connection_close(vault_page):
    page, _, _ = vault_page
    capture_vault_connections(page)
    result = page.evaluate("""async () => {
      const vault = await OndaCookieStore.open('fixture-account-a');
      await vault.save('saved-before-close');
      vaultConnections.at(-1).close();
      const restored = (await vault.load()).cookies;
      vaultConnections.at(-1).close();
      await vault.save('saved-after-reconnect');
      const updated = (await vault.load()).cookies;
      vaultConnections.at(-1).close();
      await vault.remove();
      return {restored, updated, removed: await vault.load() === null, connections: vaultOpenRequests};
    }""")
    assert result == {"restored": "saved-before-close", "updated": "saved-after-reconnect",
                      "removed": True, "connections": 4}


def test_existing_handle_reconnects_after_real_compatible_version_upgrade(vault_page):
    page, _, _ = vault_page
    capture_vault_connections(page)
    result = page.evaluate("""async () => {
      const vault = await OndaCookieStore.open('fixture-account-a');
      await vault.save('saved-before-upgrade');
      const previous = vaultConnections.at(-1);
      const upgraded = await new Promise((resolve, reject) => {
        const request = indexedDB.open('onda.session.v1', 2);
        request.onsuccess = () => resolve(request.result); request.onerror = () => reject(request.error);
      });
      upgraded.close();
      const restored = (await vault.load()).cookies;
      await vault.save('saved-after-upgrade');
      const updated = (await vault.load()).cookies;
      await vault.remove();
      return {restored, updated, removed: await vault.load() === null,
        previousVersion: previous.version, currentVersion: vaultConnections.at(-1).version};
    }""")
    assert result == {"restored": "saved-before-upgrade", "updated": "saved-after-upgrade",
                      "removed": True, "previousVersion": 1, "currentVersion": 2}


def test_late_old_connection_event_does_not_discard_reconnected_database(vault_page):
    page, _, _ = vault_page
    capture_vault_connections(page)
    result = page.evaluate("""async () => {
      const vault = await OndaCookieStore.open('fixture-account-a');
      await vault.save('saved-cookie');
      const previous = vaultConnections.at(-1);
      previous.close();
      await vault.load();
      const opens = vaultOpenRequests;
      // Browsers may deliver an old connection's close notification after its replacement opens.
      previous.dispatchEvent(new Event('close'));
      await vault.save('replacement-cookie');
      return {extraConnections: vaultOpenRequests - opens, restored: (await vault.load()).cookies};
    }""")
    assert result == {"extraConnections": 0, "restored": "replacement-cookie"}


def test_reopening_while_removal_is_queued_keeps_cancellation_and_saved_data(vault_page):
    page, _, _ = vault_page
    result = page.evaluate("""async () => {
      const first = await OndaCookieStore.open('fixture-account-a');
      await first.save('previous-cookie');
      let resume, reached;
      const original = crypto.subtle.encrypt.bind(crypto.subtle);
      const started = new Promise(resolve => { reached = resolve; });
      crypto.subtle.encrypt = async (...args) => {
        reached(); await new Promise(resolve => { resume = resolve; }); return original(...args);
      };
      const saving = first.save('unfinished-cookie').then(() => 'saved', error => error.code);
      await started;
      const removal = first.remove(); first.close(); first.close();
      const reopened = await OndaCookieStore.open('fixture-account-a');
      const obsolete = reopened.save('queued-before-removal-completes').then(() => 'saved', error => error.code);
      resume();
      const outcomes = await Promise.all([saving, obsolete]); await removal;
      crypto.subtle.encrypt = original;
      const absent = await reopened.load() === null;
      await reopened.save('explicit-new-cookie'); reopened.close();
      const fresh = await OndaCookieStore.open('fixture-account-a');
      return {outcomes, absent, saved: (await fresh.load()).cookies};
    }""")
    assert result == {"outcomes": ["closed", "removed"], "absent": True, "saved": "explicit-new-cookie"}


def test_idle_account_queues_are_released_but_pending_removals_remain_tracked(vault_page):
    page, _, _ = vault_page
    page.add_init_script("""(() => {
      const original = Map.prototype.set;
      Map.prototype.set = function (key, value) {
        if (typeof key === 'string' && /^[a-f0-9]{64}$/.test(key)
          && value?.tail instanceof Promise && Number.isInteger(value.removal)) window.accountQueues = this;
        return original.call(this, key, value);
      };
    })();""")
    page.reload()
    result = page.evaluate("""async () => {
      const first = await OndaCookieStore.open('fixture-account-a');
      await first.save('preserved-cookie');
      const second = await OndaCookieStore.open('fixture-account-a');
      first.close(); first.close();
      const shared = accountQueues.size;
      second.close();
      const idle = accountQueues.size;
      const reopened = await OndaCookieStore.open('fixture-account-a');
      const restored = (await reopened.load()).cookies;
      const removal = reopened.remove(); reopened.close();
      const pending = accountQueues.size;
      await removal;
      const released = accountQueues.size;
      const fresh = await OndaCookieStore.open('fixture-account-a');
      const removed = await fresh.load() === null; fresh.close();
      return {shared, idle, restored, pending, released, removed, final: accountQueues.size};
    }""")
    assert result == {"shared": 1, "idle": 0, "restored": "preserved-cookie", "pending": 1,
                      "released": 0, "removed": True, "final": 0}
