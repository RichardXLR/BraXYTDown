"""Public, pinned HTTP transport shared by extraction and direct media downloads."""
from __future__ import annotations

import http.client
import ipaddress
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field

from yt_dlp.networking import Response
from yt_dlp.networking._urllib import RedirectHandler, UrllibRH
from yt_dlp.networking.exceptions import HTTPError, RequestError, IncompleteRead


class AudioError(Exception):
    def __init__(self, message: str, code: str = "invalid_source", status: int = 422):
        super().__init__(message)
        self.message, self.code, self.status = message, code, status


def public_url(url: str, *, resolve: bool = True) -> tuple[str, list[str]]:
    """Reject ambiguous authorities and every non-public DNS answer, not just the first."""
    if len(url) > 4096 or any(ord(c) < 33 for c in url) or "\\" in url:
        raise AudioError("Cole um link HTTP ou HTTPS público válido.", "invalid_url", 400)
    try:
        parsed = urllib.parse.urlsplit(url)
        host = parsed.hostname
        port = parsed.port
        if (parsed.scheme not in ("http", "https") or not host or parsed.username is not None
                or parsed.password is not None or port not in (None, 80, 443)
                or (port == 80 and parsed.scheme != "http")
                or (port == 443 and parsed.scheme != "https") or "%" in host):
            raise ValueError("invalid authority")
        host = host.encode("idna").decode("ascii").lower().rstrip(".")
        if host == "localhost" or host.endswith((".localhost", ".local", ".internal")) or "." not in host and ":" not in host:
            raise ValueError("local hostname")
        literal = None
        try:
            literal = ipaddress.ip_address(host)
        except ValueError:
            pass
        if literal is not None and not literal.is_global:
            raise ValueError("private address")
        if not resolve:
            return host, [str(literal)] if literal is not None else []
        answers = socket.getaddrinfo(host, port or (443 if parsed.scheme == "https" else 80), type=socket.SOCK_STREAM)
        addresses = list(dict.fromkeys(answer[4][0] for answer in answers))
        if not addresses or any(not ipaddress.ip_address(address).is_global for address in addresses):
            raise ValueError("private DNS answer")
        return host, addresses
    except (ValueError, UnicodeError) as exc:
        raise AudioError("Esse link não aponta para um endereço público permitido.", "unsafe_url", 400) from exc
    except socket.gaierror as exc:
        raise AudioError("Não foi possível encontrar o endereço desse link.", "dns_failed", 422) from exc


@dataclass
class Guard:
    seconds: float = 240
    maximum_bytes: int = 128 * 1024 * 1024
    started: float = field(default_factory=time.monotonic)
    cancelled: threading.Event = field(default_factory=threading.Event)
    received: int = 0
    error: AudioError | None = None
    sockets: set = field(default_factory=set)
    socket_lock: threading.Lock = field(default_factory=threading.Lock)
    byte_lock: threading.Lock = field(default_factory=threading.Lock)

    @property
    def remaining(self) -> float:
        return self.seconds - (time.monotonic() - self.started)

    def check(self):
        if self.error:
            raise self.error
        if self.cancelled.is_set():
            raise AudioError("Download cancelado.", "cancelled", 499)
        if self.remaining <= 0:
            raise AudioError("A origem demorou demais. Tente um vídeo menor ou outro link.", "timeout", 504)

    def count(self, size: int):
        # Native HLS/DASH fragments share one budget across their worker threads.
        with self.byte_lock:
            self.check()
            self.received += size
            if self.received <= self.maximum_bytes:
                return
            self.error = self.error or AudioError("O arquivo de origem ultrapassa o limite de 128 MB.", "source_too_large", 413)
        # Interrupt peers already blocked in a read, without holding the budget
        # lock. check() preserves the original error ahead of cancellation.
        self.abort()
        if self.error:
            raise self.error

    def register_socket(self, connection):
        with self.socket_lock:
            self.sockets.add(connection)
        if self.cancelled.is_set():
            self.close_sockets()
            self.check()

    def close_sockets(self):
        with self.socket_lock:
            connections, self.sockets = self.sockets, set()
        for connection in connections:
            try:
                connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                connection.close()
            except OSError:
                pass

    def abort(self):
        self.cancelled.set()
        # Interrupt blocking header/chunk reads, not only future progress hooks.
        self.close_sockets()


class BoundedResponse(Response):
    def __init__(self, response: Response, guard: Guard):
        super().__init__(response, response.url, response.headers, response.status, response.reason)
        self.guard = guard

    def read(self, amt=None):
        self.guard.check()
        # Metadata callers read without a size. Bound every allocation as well as the total.
        if amt is None or amt < 0:
            chunks = []
            while chunk := self.read(64 * 1024):
                chunks.append(chunk)
                if sum(map(len, chunks)) > 16 * 1024 * 1024:
                    raise AudioError("A resposta desse serviço é grande demais.", "metadata_too_large", 413)
            return b"".join(chunks)
        raw = getattr(self.fp, "fp", None)
        # read1 performs at most one socket read, so a slow sender cannot extend
        # the deadline indefinitely by trickling bytes into a sized read().
        try:
            if isinstance(raw, http.client.HTTPResponse):
                socket_stream = getattr(getattr(raw, "fp", None), "raw", None)
                connection_socket = getattr(socket_stream, "_sock", None)
                if connection_socket:
                    connection_socket.settimeout(min(8, max(.1, self.guard.remaining)))
                data = raw.read1(min(amt, 1024 * 1024))
            else:
                data = self.fp.read(min(amt, 1024 * 1024))
        except (http.client.IncompleteRead, RequestError) as exc:
            # Response adapters can wrap the partial read in TransportError.
            # Inspect both cause links once, without counting a nested error
            # twice or letting an unusual cause graph loop indefinitely.
            pending, seen = [exc], set()
            while pending and len(seen) < 8:
                error = pending.pop()
                if id(error) in seen:
                    continue
                seen.add(id(error))
                if isinstance(error, (http.client.IncompleteRead, IncompleteRead)):
                    partial = error.partial
                    size = partial if isinstance(partial, int) else len(partial)
                    # These bytes cannot be kept, but still consume the master
                    # request budget before a recovery attempt starts.
                    self.guard.count(max(0, size))
                    break
                pending.extend(cause for cause in (
                    getattr(error, "cause", None), error.__cause__
                ) if isinstance(cause, BaseException))
            raise
        self.guard.count(len(data))
        return data


def pinned_connection(base, guard: Guard):
    class PinnedConnection(base):
        def connect(self):
            guard.check()
            scheme = "https" if isinstance(self, http.client.HTTPSConnection) else "http"
            hostname = f"[{self.host}]" if ":" in self.host else self.host
            try:
                _, addresses = public_url(f"{scheme}://{hostname}:{self.port}/")
            except AudioError as exc:
                # A failed DNS lookup is a recoverable transport failure, not
                # proof of an unsafe address. The next bounded attempt must
                # resolve and validate every answer again before connecting.
                if exc.code != "dns_failed":
                    guard.error = exc
                raise
            # TLS still verifies the original hostname. The TCP socket never re-resolves it.
            def connect_pinned(_address, timeout=socket._GLOBAL_DEFAULT_TIMEOUT, source_address=None):
                last_error = None
                for address in addresses:
                    guard.check()
                    try:
                        connection = socket.create_connection((address, self.port), timeout=min(float(timeout), max(.1, guard.remaining)), source_address=source_address)
                        guard.register_socket(connection)
                        return connection
                    except OSError as exc:
                        last_error = exc
                raise last_error or OSError("connection failed")
            self._create_connection = connect_pinned
            result = super().connect()
            guard.register_socket(self.sock)
            return result
    return PinnedConnection


class PublicHTTPHandler(urllib.request.HTTPHandler, urllib.request.HTTPSHandler):
    def __init__(self, guard: Guard, context=None):
        urllib.request.HTTPHandler.__init__(self)
        self.guard = guard
        self.context = context

    def http_open(self, req):
        self.guard.check()
        public_url(req.full_url, resolve=False)
        return self.do_open(pinned_connection(http.client.HTTPConnection, self.guard), req)

    def https_open(self, req):
        self.guard.check()
        public_url(req.full_url, resolve=False)
        return self.do_open(pinned_connection(http.client.HTTPSConnection, self.guard), req, context=self.context)


class PublicRedirectHandler(RedirectHandler):
    def __init__(self, guard):
        self.guard = guard
        self.max_redirections = 5
        self.max_repeats = 2

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        try:
            public_url(newurl)
        except AudioError as exc:
            if exc.code != "dns_failed":
                self.guard.error = exc
            raise
        redirected = super().redirect_request(req, fp, code, msg, headers, newurl)
        if urllib.parse.urlsplit(req.full_url).netloc != urllib.parse.urlsplit(newurl).netloc:
            redirected.remove_header("Authorization")
        return redirected


class PublicRH(UrllibRH):
    """Exclusive yt-dlp handler: no proxy, file, FTP, data or unguarded fallback."""
    _SUPPORTED_URL_SCHEMES = ("http", "https")
    _SUPPORTED_PROXY_SCHEMES = ()
    _SUPPORTED_FEATURES = ()

    def __init__(self, *, guard, **kwargs):
        self.guard = guard
        super().__init__(**kwargs)

    def _prepare_headers(self, _, headers):
        headers["Accept-Encoding"] = "identity"

    def _create_instance(self, proxies, cookiejar, legacy_ssl_support=None):
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}),
            PublicHTTPHandler(self.guard, self._make_sslcontext()),
            urllib.request.HTTPCookieProcessor(cookiejar),
            PublicRedirectHandler(self.guard),
        )
        # build_opener adds file/FTP/data handlers by default; remove them explicitly.
        for name in ("file", "ftp", "data"):
            opener.handle_open.pop(name, None)
        opener.addheaders = []
        return opener

    def _send(self, request):
        self.guard.check()
        try:
            public_url(request.url, resolve=False)
            try:
                response = super()._send(request)
            except HTTPError as exc:
                # Extractors also read error responses (e.g. JSON from a 404).
                if exc.response.headers.get("Content-Encoding", "identity").lower() not in ("", "identity"):
                    exc.response.close()
                    raise AudioError("A origem retornou uma transferência incompatível. Tente outro link.", "unsupported_transfer", 422)
                exc.response = BoundedResponse(exc.response, self.guard)
                raise
            if response.headers.get("Content-Encoding", "identity").lower() not in ("", "identity"):
                response.close()
                raise AudioError("A origem retornou uma transferência incompatível. Tente outro link.", "unsupported_transfer", 422)
            return BoundedResponse(response, self.guard)
        except AudioError as exc:
            # Keep permission/safety/byte failures definitive while allowing
            # temporary resolver failures through the existing recovery loop.
            if exc.code != "dns_failed":
                self.guard.error = exc
            # yt-dlp requires a networking exception; retain the original for the API.
            raise RequestError(cause=exc) from exc
