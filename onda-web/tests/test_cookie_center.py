"""Value-free cookie summary and account-vault metadata behavior."""
from pathlib import Path
import shutil
import subprocess

import pytest

from test_session_store import COOKIE, ROOT, vault_browser, vault_page


@pytest.mark.parametrize("case", [
    "safe_output", "next_expiry_and_session", "expiries_respect_platform",
    "positive_expiry_authoritative", "session_sentinels", "invalid_cookie_dates",
    "netscape_http_only", "partitioned_ignored", "limits_and_unsafe_domains",
    "metadata_content_binding", "validation_scope_and_labels", "failure_sanitized", "timers_removed",
])
def test_value_free_cookie_center(case):
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is unavailable in this API-only environment")
    completed = subprocess.run([node, str(ROOT / "tests/cookie_center.cjs"), case],
                               cwd=ROOT, capture_output=True, text=True, timeout=10)
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "passed." in completed.stdout


def load_center(page):
    page.add_script_tag(content=(ROOT / "public/cookie-center.js").read_text())


def test_metadata_encrypted_and_retained_across_reload(vault_page):
    page, _, _ = vault_page
    load_center(page)
    result = page.evaluate("""async cookies => {
      const metadata = await OndaCookieCenter.createValidation(cookies, 'https://example.org/video',
        { validCookies: 1, ignoredCookies: 0, expiredCookies: 0, sessionCookies: 1, earliestExpiry: null });
      const vault = await OndaCookieStore.open('fixture-account-a');
      await vault.save(cookies, 'fixture.txt', metadata);
      const db = await new Promise(resolve => { const request = indexedDB.open('onda.session.v1'); request.onsuccess = () => resolve(request.result); });
      const record = await new Promise(resolve => { const request = db.transaction('accounts').objectStore('accounts').getAll(); request.onsuccess = () => resolve(request.result[0]); });
      db.close();
      const serial = JSON.stringify(record);
      return { metadata, sealed: !serial.includes('example.org') && !serial.includes(metadata.contentHash)
        && !serial.includes('fixture-cookie-sensitive-marker'), localEntries: localStorage.length, sessionEntries: sessionStorage.length };
    }""", COOKIE)
    assert result["sealed"] and result["localEntries"] == result["sessionEntries"] == 0
    page.reload()
    restored = page.evaluate("""async () => { const vault = await OndaCookieStore.open('fixture-account-a'); return vault.load(); }""")
    assert restored["cookies"] == COOKIE and restored["metadata"] == result["metadata"]


def test_metadata_cannot_attach_to_different_cookie_content(vault_page):
    page, _, _ = vault_page
    load_center(page)
    result = page.evaluate("""async cookies => {
      const metadata = await OndaCookieCenter.createMetadata(cookies);
      const vault = await OndaCookieStore.open('fixture-account-a'); await vault.save(cookies, 'original.txt');
      let rejected; try { await vault.save(cookies.replace('sensitive-marker', 'changed-marker'), 'changed.txt', metadata); } catch (error) { rejected = error.code; }
      return { rejected, previousPreserved: (await vault.load()).filename === 'original.txt' };
    }""", COOKIE)
    assert result == {"rejected": "invalid", "previousPreserved": True}


def test_metadata_account_isolation_and_explicit_removal(vault_page):
    page, _, _ = vault_page
    load_center(page)
    result = page.evaluate("""async cookies => {
      const metadata = await OndaCookieCenter.createValidationFailure(cookies, 'https://example.org/video', 'cookie_domain');
      const first = await OndaCookieStore.open('fixture-account-a'); await first.save(cookies, '', metadata); first.close();
      const other = await OndaCookieStore.open('fixture-account-b'); const isolated = await other.load() === null;
      const restored = await OndaCookieStore.open('fixture-account-a'); const retained = (await restored.load()).metadata.validation.code === 'cookie_domain';
      await restored.remove(); return { isolated, retained, removed: await restored.load() === null };
    }""", COOKIE)
    assert result == {"isolated": True, "retained": True, "removed": True}


@pytest.mark.parametrize("mutation", [
    "metadata.cookies = 'private-extra-field'",
    "metadata.validation = { checkedAt: Date.now(), scope: 'https://example.org/private', status: 'invalid', code: 'cookie_domain' }",
    "metadata.validation = { checkedAt: Date.now(), scope: 'example.org', status: 'invalid', code: 'private-error-content' }",
    "metadata.validation = { checkedAt: Date.now(), scope: 'example.org', status: 'valid', validCookies: 1, ignoredCookies: 0, expiredCookies: 0, sessionCookies: 2, earliestExpiry: null }",
])
def test_vault_rejects_unsafe_metadata_without_secret_echo(vault_page, mutation):
    page, _, _ = vault_page
    load_center(page)
    result = page.evaluate("""async data => {
      const metadata = await OndaCookieCenter.createMetadata(data.cookies);
      Function('metadata', data.mutation)(metadata);
      const vault = await OndaCookieStore.open('fixture-account-a');
      try { await vault.save(data.cookies, '', metadata); return { accepted: true }; }
      catch (error) { return { code: error.code, safe: !error.message.includes('private') }; }
    }""", {"cookies": COOKIE, "mutation": mutation})
    assert result == {"code": "invalid", "safe": True}
