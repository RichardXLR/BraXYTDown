"""Optional Netscape sessions, restricted to one media service and held in memory.

The uploaded BraXYTDown cookie_auth module informed the Netscape/browser distinction.
This parser is an independent implementation; no browser, password or file access occurs.
"""
from __future__ import annotations

import http.cookiejar
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
    return AudioError("Use um arquivo de cookies Netscape válido, com até 64 KB, apenas da plataforma do link.", code, 422)


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


def parse_netscape(raw: str | None, url: str, *, direct_media=False) -> RequestCookieJar | None:
    if raw is None or raw == "":
        return None
    if direct_media:
        raise AudioError("Cookies de sessão só podem ser usados em links da plataforma, não em arquivos diretos.", "cookies_not_supported", 422)
    if urllib.parse.urlsplit(url).scheme != "https":
        raise AudioError("Use um link HTTPS para enviar cookies de sessão à plataforma.", "cookie_https_required", 422)
    if not isinstance(raw, str) or len(raw.encode("utf-8")) > MAX_COOKIE_BYTES:
        raise invalid_cookies()
    lines = raw.lstrip("\ufeff").splitlines()
    if not lines or lines[0].strip() not in ("# Netscape HTTP Cookie File", "# HTTP Cookie File"):
        raise invalid_cookies()
    scopes = cookie_scopes(url)
    jar = RequestCookieJar(scopes)
    rows = 0
    expired = 0
    try:
        for line in lines[1:]:
            if not line.strip():
                continue
            http_only = line.startswith("#HttpOnly_")
            if line.startswith("#") and not http_only:
                continue
            if http_only:
                line = line.removeprefix("#HttpOnly_")
            fields = line.split("\t")
            rows += 1
            if len(fields) != 7 or rows > MAX_COOKIES:
                raise invalid_cookies()
            domain, subdomains, path, secure, expires, name, value = fields
            if (subdomains not in ("TRUE", "FALSE") or secure not in ("TRUE", "FALSE")
                    or not path.startswith("/") or len(path) > 1024 or not COOKIE_NAME.fullmatch(name)
                    or len(value) > 4096 or any(ord(char) < 32 or ord(char) == 127 for char in path + value)
                    or any(char in value for char in ";\r\n") or not expires.isascii() or not expires.isdigit()
                    or len(expires) > 12):
                raise invalid_cookies()
            clean_domain = domain.removeprefix(".").lower()
            if (not DOMAIN.fullmatch(clean_domain) or domain.startswith("..")
                    or not PSL.privatesuffix(clean_domain)
                    or (domain.startswith(".") and subdomains != "TRUE")):
                # Public/private suffix cookies (e.g. .com, .co.uk, .github.io) are unsafe.
                raise invalid_cookies("cookie_domain")
            if not scoped_domain(clean_domain, scopes):
                continue  # A browser export may include other sites. Never keep those entries.
            expiration = int(expires)
            if expiration and expiration <= time.time():
                expired += 1
                continue
            include_subdomains = subdomains == "TRUE"
            cookie_domain = "." + clean_domain if include_subdomains else clean_domain
            jar.set_cookie(http.cookiejar.Cookie(
                version=0, name=name, value=value, port=None, port_specified=False,
                domain=cookie_domain, domain_specified=include_subdomains,
                domain_initial_dot=include_subdomains, path=path, path_specified=True,
                secure=secure == "TRUE", expires=expiration or None, discard=expiration == 0,
                comment=None, comment_url=None, rest={"HttpOnly": None} if http_only else {}, rfc2109=False,
            ))
        if not len(jar):
            if expired:
                raise AudioError("Os cookies dessa plataforma expiraram. Exporte uma sessão nova.", "cookies_expired", 422)
            raise AudioError("O arquivo não contém cookies da plataforma desse link.", "cookie_domain", 422)
        return jar
    except BaseException:
        jar.clear()
        raise
