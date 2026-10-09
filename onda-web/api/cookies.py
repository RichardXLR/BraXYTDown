"""Optional exported sessions, restricted to one media service and held in memory.

The uploaded BraXYTDown cookie_auth module informed the Netscape/browser distinction.
This parser is an independent implementation; no browser, password or file access occurs.
"""
from __future__ import annotations

import http.cookiejar
import json
import math
import re
import time
import urllib.parse

from publicsuffixlist import PublicSuffixList
from yt_dlp.cookies import YoutubeDLCookieJar

from .security import AudioError, public_url

MAX_COOKIE_BYTES = 64 * 1024
MAX_COOKIES = 300
PSL = PublicSuffixList(accept_unknown=False, only_icann=False)
FAMILIES = (
    ({"youtube.com", "youtube-nocookie.com", "youtu.be"},
     {"youtube.com", "youtube-nocookie.com", "youtu.be", "google.com", "googlevideo.com", "ytimg.com"}),
    ({"facebook.com", "fb.watch", "fb.com"}, {"facebook.com", "fb.watch", "fb.com"}),
    ({"twitter.com", "x.com", "t.co"}, {"twitter.com", "x.com", "t.co"}),
)
COOKIE_NAME = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]{1,256}$")
DOMAIN = re.compile(r"^(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z0-9-]{2,63}$")


def invalid_cookies(code="invalid_cookies"):
    return AudioError("Importe cookies em formato Netscape (.txt) ou JSON, com até 64 KB, da plataforma desse link.", code, 422)


def cookie_scopes(url: str) -> frozenset[str]:
    host, _ = public_url(url, resolve=False)
    root = PSL.privatesuffix(host)
    if not root:
        raise invalid_cookies("cookie_domain")
    for domains, allowed in FAMILIES:
        if root in domains:
            return frozenset(allowed)
    return frozenset({root})


def scoped_domain(domain: str, scopes: frozenset[str]) -> bool:
    return any(domain == scope or domain.endswith("." + scope) for scope in scopes)


class ServiceCookiePolicy(http.cookiejar.DefaultCookiePolicy):
    def __init__(self, scopes):
        super().__init__(strict_ns_domain=self.DomainStrictNonDomain)
        self.scopes = scopes

    def allowed_domain(self, domain):
        clean = domain.lstrip(".").lower()
        return bool(PSL.privatesuffix(clean)) and scoped_domain(clean, self.scopes)

    def set_ok(self, cookie, request):
        return self.allowed_domain(cookie.domain) and super().set_ok(cookie, request)

    def return_ok(self, cookie, request):
        parsed = urllib.parse.urlsplit(request.full_url)
        host = (parsed.hostname or "").lower()
        return (parsed.scheme == "https" and scoped_domain(host, self.scopes) and self.allowed_domain(cookie.domain)
                and super().return_ok(cookie, request))


class RequestCookieJar(YoutubeDLCookieJar):
    def __init__(self, scopes):
        super().__init__(filename=None, policy=ServiceCookiePolicy(scopes))

    def save(self, *_args, **_kwargs):
        raise RuntimeError("Request cookies cannot be persisted")


def _netscape_entries(content: str):
    lines = content.splitlines()
    if not lines or lines[0].strip() not in ("# Netscape HTTP Cookie File", "# HTTP Cookie File"):
        raise invalid_cookies()
    for line in lines[1:]:
        if not line.strip():
            continue
        http_only = line.startswith("#HttpOnly_")
        if line.startswith("#") and not http_only:
            continue
        if http_only:
            line = line.removeprefix("#HttpOnly_")
        fields = line.split("\t")
        if len(fields) != 7:
            raise invalid_cookies()
        domain, subdomains, path, secure, expires, name, value = fields
        if (subdomains not in ("TRUE", "FALSE") or secure not in ("TRUE", "FALSE")
                or not expires.isascii() or not expires.isdigit() or len(expires) > 12):
            raise invalid_cookies()
        yield domain, subdomains == "TRUE", path, secure == "TRUE", int(expires) or None, name, value, http_only, False


def _json_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise invalid_cookies()
        result[key] = value
    return result


def _json_constant(_value):
    # json.loads normally accepts NaN and Infinity, which are not browser JSON.
    raise invalid_cookies()


def _json_expiry(value):
    if value is None:
        return None
    if (isinstance(value, bool) or not isinstance(value, (int, float))
            or value < -1 or value > 999_999_999_999 or not math.isfinite(value)
            or (value < 0 and value != -1)):
        raise invalid_cookies()
    # Playwright/browser exports use -1 for a cookie valid for this session.
    return None if value == -1 else int(value)


def _json_entries(content: str):
    try:
        payload = json.loads(content, object_pairs_hook=_json_object, parse_constant=_json_constant)
    except (ValueError, RecursionError):
        raise invalid_cookies() from None
    if isinstance(payload, dict):
        payload = payload.get("cookies")
    if not isinstance(payload, list) or len(payload) > MAX_COOKIES:
        raise invalid_cookies()
    for item in payload:
        if not isinstance(item, dict):
            raise invalid_cookies()
        domain = item.get("domain")
        if not isinstance(domain, str):
            raise invalid_cookies("cookie_domain")
        booleans = {key: item.get(key, default) for key, default in (
            ("hostOnly", not domain.startswith(".")), ("secure", False), ("httpOnly", False),
            ("session", None), ("partitioned", False),
        )}
        if any(not isinstance(value, bool) for key, value in booleans.items() if key != "session" or key in item):
            raise invalid_cookies()
        # Validate both known expiry fields when an export supplies both.
        expirations = {key: _json_expiry(item[key]) for key in ("expirationDate", "expires") if key in item}
        if len(set(expirations.values())) > 1:
            raise invalid_cookies()
        expiry = next(iter(expirations.values()), None)
        # Some extensions serialize session cookies with a zero sentinel. A
        # positive expiry remains authoritative even if a stale session flag
        # accompanies it; importing must never revive an expired cookie.
        zero_sentinel = expiry == 0 and all(item[key] == 0 for key in expirations)
        if booleans["session"] is True and zero_sentinel:
            expiry = None
        elif booleans["session"] is False and expiry is None:
            raise invalid_cookies()
        partitioned = (booleans["partitioned"] or bool(item.get("partitionKey"))
                       or bool(item.get("firstPartyDomain")))
        yield (domain, not booleans["hostOnly"], item.get("path", "/"), booleans["secure"], expiry,
               item.get("name"), item.get("value"), booleans["httpOnly"], partitioned)


def _parse_session(raw: str | None, url: str, *, direct_media=False):
    if raw is None or raw == "":
        return None, None
    if direct_media:
        raise AudioError("Cookies de sessão só podem ser usados em links da plataforma, não em arquivos diretos.", "cookies_not_supported", 422)
    if urllib.parse.urlsplit(url).scheme != "https":
        raise AudioError("Use um link HTTPS para enviar cookies de sessão à plataforma.", "cookie_https_required", 422)
    if not isinstance(raw, str):
        raise invalid_cookies()
    try:
        if len(raw.encode("utf-8")) > MAX_COOKIE_BYTES:
            raise invalid_cookies()
    except UnicodeError:
        raise invalid_cookies() from None
    content = raw.lstrip("\ufeff \t\r\n")
    kind = "json" if content.startswith(("[", "{")) else "netscape"
    entries = _json_entries(content) if kind == "json" else _netscape_entries(content)
    scopes = cookie_scopes(url)
    jar = RequestCookieJar(scopes)
    rows = 0
    expired = 0
    ignored = 0
    now = time.time()
    try:
        for domain, subdomains, path, secure, expiration, name, value, http_only, partitioned in entries:
            rows += 1
            if rows > MAX_COOKIES:
                raise invalid_cookies()
            if (not isinstance(path, str) or not isinstance(name, str) or not isinstance(value, str)
                    or not path.startswith("/") or len(path) > 1024 or not COOKIE_NAME.fullmatch(name)
                    or len(value) > 4096 or any(ord(char) < 32 or ord(char) == 127 or 0xD800 <= ord(char) <= 0xDFFF
                                             for char in path + value)
                    or any(char in value for char in ";\r\n")):
                raise invalid_cookies()
            clean_domain = domain.removeprefix(".").lower()
            if (not DOMAIN.fullmatch(clean_domain) or domain.startswith("..")
                    or not PSL.privatesuffix(clean_domain)
                    or (domain.startswith(".") and not subdomains)):
                # Public/private suffix cookies (e.g. .com, .co.uk, .github.io) are unsafe.
                raise invalid_cookies("cookie_domain")
            if not scoped_domain(clean_domain, scopes) or partitioned:
                ignored += 1
                continue  # A browser export may include other sites. Never keep those entries.
            cookie_domain = "." + clean_domain if subdomains else clean_domain
            if expiration is not None and expiration <= now:
                expired += 1
                try:
                    jar.clear(cookie_domain, path, name)
                except KeyError:
                    pass
                continue
            jar.set_cookie(http.cookiejar.Cookie(
                version=0, name=name, value=value, port=None, port_specified=False,
                domain=cookie_domain, domain_specified=subdomains,
                domain_initial_dot=subdomains, path=path, path_specified=True,
                secure=secure, expires=expiration, discard=expiration is None,
                comment=None, comment_url=None, rest={"HttpOnly": None} if http_only else {}, rfc2109=False,
            ))
        if not len(jar):
            if expired:
                raise AudioError("Os cookies dessa plataforma expiraram. Exporte uma sessão nova.", "cookies_expired", 422)
            raise AudioError("O arquivo não contém cookies da plataforma desse link.", "cookie_domain", 422)
        expiries = [cookie.expires for cookie in jar if cookie.expires is not None]
        return jar, {
            "format": kind, "validCookies": len(jar), "ignoredCookies": ignored,
            "expiredCookies": expired, "scopes": sorted({cookie.domain.lstrip(".") for cookie in jar}),
            "earliestExpiry": min(expiries, default=None),
            "sessionCookies": sum(cookie.expires is None for cookie in jar),
        }
    except BaseException:
        jar.clear()
        raise


def parse_session(raw: str | None, url: str, *, direct_media=False) -> RequestCookieJar | None:
    """Read a Netscape or browser JSON export without writing session data to disk."""
    jar, _metadata = _parse_session(raw, url, direct_media=direct_media)
    return jar


def parse_netscape(raw: str | None, url: str, *, direct_media=False) -> RequestCookieJar | None:
    """Compatibility entry point: both formats now use the same restricted policy."""
    return parse_session(raw, url, direct_media=direct_media)


def validate_session(raw: str | None, url: str, *, direct_media=False) -> dict:
    """Return a safe, value-free import summary, then immediately clear the jar.

    This validates the export, scope and local expiry only. It does not claim
    that a service will accept a session or that its access restrictions change.
    """
    jar, metadata = _parse_session(raw, url, direct_media=direct_media)
    if jar is None:
        raise invalid_cookies()
    try:
        return metadata
    finally:
        jar.clear()
