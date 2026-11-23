import base64
import functools
import http.client
import ipaddress
import json
import re
import socket
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Callable
from urllib.parse import urlsplit

from skopax.errkind import kind

MARKER = 'x-skopax-'
PAGE_READ = 'X-Skopax-Page-Read'
TEXT_READ = 'X-Skopax-Text-Read'
JSON_READ = 'X-Skopax-Json-Read'
PIN = 'X-Skopax-Pin'
PAGE_LIMIT = 2 * 1024 * 1024
PAGE_MAX = 8 * 1024 * 1024
BODY_LIMIT = 4096
PIN_MISMATCH = 'certificate does not match the pinned certificate'
PIN_SOCKET = 'a pinned TLS context wraps only a connected, blocking socket'
PEM_CERT = re.compile(f'{ssl.PEM_HEADER}.+?{ssl.PEM_FOOTER}', re.S)
ANSWER_LIMIT = 1024 * 1024
TEXT_LIMIT = 2 * 1024 * 1024
JSON_LIMIT = 64 * 1024 * 1024
JSON_MAX = 64 * 1024 * 1024
MB = 1024 * 1024
PAGE_DEADLINE = 2
LOOKUP_S = 5.0
TUNNEL_HEAD = 8192
DEADLINE_SLACK = 0.05
PORTS = {'http': 80, 'https': 443}
JSON_ACCEPT = 'application/json'
TEXT_ACCEPT = 'text/plain, text/html, */*;q=0.1'


class HttpError(Exception):
    def __init__(self, code: int, where: str, body: str = '', headers: dict[str, str] | None = None):
        super().__init__(f'HTTP {code} on {where}')
        self.code = code
        self.body = body[:BODY_LIMIT]
        self.headers = {str(k).lower(): str(v) for k, v in (headers or {}).items()}


class Unreachable(Exception):
    pass


class NotJson(Unreachable):
    pass


class TimedOut(Unreachable):
    pass


class TooLarge(Unreachable):
    pass


@dataclass(frozen=True)
class Reply:
    body: object
    headers: dict[str, str]
    status: int | None = None


Transport = Callable[[str, str, dict[str, str], bytes | None, float], Reply]


def origin(url: str) -> tuple | None:
    try:
        u = urlsplit(url)
        return u.scheme, u.hostname, u.port or PORTS.get(u.scheme)
    except ValueError:
        return None


def same_host(old: str, new: str) -> bool:
    a, b = origin(old), origin(new)
    if a is None or b is None or a[1] != b[1]:
        return False
    return a == b or (a[0], a[2], b[0], b[2]) == ('http', 80, 'https', 443)


class HostRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new is not None and not same_host(req.full_url, new.full_url):
            new.headers.clear()
        return new


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def peer_pinned(conn: ssl.SSLSocket) -> None:
    if conn.getpeercert(binary_form=True) not in conn.context.pins:
        raise ssl.SSLCertVerificationError(ssl.SSL_ERROR_SSL, PIN_MISMATCH)


def connected(sock: socket.socket) -> bool:
    try:
        sock.getpeername()
    except OSError:
        return False
    return True


class PinnedSocket(ssl.SSLSocket):
    checked = False

    def do_handshake(self, block: bool = False) -> None:
        super().do_handshake(block)
        peer_pinned(self)
        self.checked = True


class PinnedContext(ssl.SSLContext):
    sslsocket_class = PinnedSocket
    pins: frozenset[bytes] = frozenset()

    def wrap_socket(self, sock, server_side=False, do_handshake_on_connect=True, suppress_ragged_eofs=True, server_hostname=None, session=None):
        if sock.gettimeout() == 0.0 or not connected(sock):
            raise ValueError(PIN_SOCKET)
        tls = super().wrap_socket(sock, server_side, True, suppress_ragged_eofs, server_hostname, session)
        if not tls.checked:
            tls.close()
            raise ValueError(PIN_SOCKET)
        return tls

    def wrap_bio(self, incoming, outgoing, server_side=False, server_hostname=None, session=None):
        raise ValueError(PIN_SOCKET)


def pinned_context(pem: str) -> PinnedContext:
    pins = frozenset(ssl.PEM_cert_to_DER_cert(block) for block in PEM_CERT.findall(pem))
    context = PinnedContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    context.pins = pins
    return context


@functools.cache
def tls_context(pem: str, pinned: bool = False) -> ssl.SSLContext:
    return pinned_context(pem) if pinned else ssl.create_default_context(cadata=pem)


WATCH = threading.local()


def watched(sock: socket.socket) -> socket.socket:
    found = getattr(WATCH, 'value', None)
    if found is not None:
        socks, expired = found
        socks.append(sock)
        if expired.is_set():
            shut(sock)
            raise TimeoutError
    return sock


class WatchedHttp(http.client.HTTPConnection):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        dial = self._create_connection
        self._create_connection = lambda *a, **k: watched(dial(*a, **k))

    def connect(self):
        super().connect()
        watched(self.sock)


class WatchedHttps(http.client.HTTPSConnection):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        dial = self._create_connection
        self._create_connection = lambda *a, **k: watched(dial(*a, **k))

    def connect(self):
        super().connect()
        watched(self.sock)


class WatchedHttpHandler(urllib.request.HTTPHandler):
    def http_open(self, req):
        return self.do_open(WatchedHttp, req)


class WatchedHttpsHandler(urllib.request.HTTPSHandler):
    def https_open(self, req):
        return self.do_open(WatchedHttps, req, context=self._context)


@functools.cache
def opener(context: ssl.SSLContext | None = None, follow: bool = True, proxied: bool = True) -> urllib.request.OpenerDirector:
    handlers = ([] if proxied else [urllib.request.ProxyHandler({})]) + [WatchedHttpHandler, WatchedHttpsHandler(context=context), HostRedirect if follow else NoRedirect]
    return urllib.request.build_opener(*handlers)


def open_url(req: urllib.request.Request, timeout: float, context: ssl.SSLContext | None = None):
    return opener(context).open(req, timeout=timeout)


def open_plain(req: urllib.request.Request, timeout: float, context: ssl.SSLContext | None = None):
    return opener(context, False).open(req, timeout=timeout)


def open_direct(req: urllib.request.Request, timeout: float, context: ssl.SSLContext | None = None, follow: bool = True):
    return opener(context, follow, False).open(req, timeout=timeout)


def marked(headers: dict[str, str] | None, name: str) -> str | None:
    value = next((v for k, v in (headers or {}).items() if str(k).lower() == name.lower()), None)
    return None if value is None else str(value).strip()


def limit_of(headers: dict[str, str] | None, name: str, ceiling: int = PAGE_MAX) -> int | None:
    text = marked(headers, name)
    if text is None:
        return None
    return min(int(text), ceiling) if text.isascii() and text.isdigit() and int(text) > 0 else ceiling


def page_limit(headers: dict[str, str] | None) -> int | None:
    return limit_of(headers, PAGE_READ)


def unmarked(headers: dict[str, str] | None) -> dict[str, str]:
    return {k: v for k, v in (headers or {}).items() if not str(k).lower().startswith(MARKER)}


class ProxyRefused(OSError):
    def __init__(self, code: int | None):
        super().__init__('proxy refused')
        self.code = code


class Cut(OSError):
    pass


def shut(sock: socket.socket) -> None:
    try:
        sock.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass


def proxy_for(scheme: str, host: str) -> urllib.parse.SplitResult | None:
    found = urllib.request.getproxies_environment()
    raw = found.get(scheme)
    if not raw or urllib.request.proxy_bypass_environment(host, found):
        return None
    parts = urlsplit(raw if '://' in raw else f'http://{raw}')
    if parts.scheme.lower() != 'http' or not parts.hostname:
        raise ProxyRefused(None)
    return parts


class Wire(http.client.HTTPConnection):
    def __init__(self, host: str, port: int | None, timeout: float, address: str | None, socks: list, expired: threading.Event,
                 proxy: urllib.parse.SplitResult | None = None, deadline: float | None = None):
        super().__init__(host, port, timeout=timeout)
        self.deadline = deadline
        self.address = address
        self.socks = socks
        self.expired = expired
        self.proxy = proxy

    def keep(self, sock: socket.socket) -> socket.socket:
        self.socks.append(sock)
        if self.expired.is_set():
            shut(sock)
            raise TimeoutError
        return sock

    def wait(self) -> float:
        if self.deadline is None:
            return self.timeout
        left = self.deadline - time.monotonic()
        if left <= 0:
            raise TimeoutError
        return min(self.timeout, left)

    def target(self) -> str:
        return self.address or self.host

    def reach(self, host: str, port: int) -> socket.socket:
        found = [host] if is_address(host) else addresses(host, self.wait())
        last: Exception | None = None
        for one in found:
            if self.expired.is_set():
                raise TimeoutError
            try:
                return self.keep(socket.create_connection((one, port), self.wait()))
            except OSError as e:
                if self.expired.is_set():
                    raise
                last = e
        raise last or OSError('no address')

    def direct(self) -> socket.socket:
        return self.reach(self.address or self.host, self.port)

    def tunnel(self) -> socket.socket:
        proxy = self.proxy
        sock = self.reach(proxy.hostname, proxy.port or 80)
        where = self.target()
        shown = f'[{where}]' if ':' in where else where
        lines = [f'CONNECT {shown}:{self.port} HTTP/1.1', f'Host: {shown}:{self.port}']
        if proxy.username is not None:
            pair = f'{urllib.parse.unquote(proxy.username)}:{urllib.parse.unquote(proxy.password or "")}'
            lines.append('Proxy-Authorization: Basic ' + base64.b64encode(pair.encode()).decode())
        sock.sendall(('\r\n'.join(lines) + '\r\n\r\n').encode())
        head = b''
        while b'\r\n\r\n' not in head:
            sock.settimeout(self.wait())
            got = sock.recv(1024)
            if not got:
                raise ProxyRefused(None)
            head += got
            if len(head) > TUNNEL_HEAD:
                raise ProxyRefused(None)
        status = head.split(b'\r\n', 1)[0].split()
        code = int(status[1]) if len(status) > 1 and status[1].isdigit() else None
        if code is None or not 200 <= code < 300:
            raise ProxyRefused(code)
        return sock

    def dial(self) -> socket.socket:
        return self.tunnel() if self.proxy is not None else self.direct()

    def connect(self):
        self.sock = self.dial()


class TlsWire(Wire):
    default_port = PORTS['https']

    def __init__(self, host: str, port: int | None, timeout: float, address: str | None, socks: list, expired: threading.Event,
                 proxy: urllib.parse.SplitResult | None = None, context: ssl.SSLContext | None = None, deadline: float | None = None):
        super().__init__(host, port, timeout, address, socks, expired, proxy, deadline)
        self.context = context or ssl.create_default_context()

    def connect(self):
        wrapped = self.keep(self.context.wrap_socket(self.dial(), server_hostname=self.host, do_handshake_on_connect=False))
        wrapped.settimeout(self.wait())
        wrapped.do_handshake()
        wrapped.settimeout(self.timeout)
        self.sock = wrapped


def wire_for(url: str, timeout: float, context: ssl.SSLContext | None, address: str | None, socks: list, expired: threading.Event,
             deadline: float | None = None, proxied: bool = True) -> tuple[Wire, str]:
    parts = urlsplit(url)
    scheme = parts.scheme.lower()
    if scheme not in PORTS or not parts.hostname:
        raise ValueError('not an http(s) url')
    target = (parts.path or '/') + (f'?{parts.query}' if parts.query else '')
    proxy = proxy_for(scheme, parts.hostname) if proxied else None
    if scheme == 'https':
        return TlsWire(parts.hostname, parts.port, timeout, address, socks, expired, proxy, context, deadline), target
    return Wire(parts.hostname, parts.port, timeout, address, socks, expired, proxy, deadline), target


def whole(resp: http.client.HTTPResponse, body: bytes, limit: int) -> bool:
    length = resp.getheader('content-length')
    if resp.getheader('transfer-encoding') or length is None or not length.strip().isdigit():
        return True
    return len(body) >= min(int(length), limit + 1)


def read_page(method: str, url: str, headers: dict[str, str], data: bytes | None, timeout: float, limit: int, context: ssl.SSLContext | None = None,
              address: str | None = None, proxied: bool = True) -> Reply:
    where = url.split('?')[0]
    sent = unmarked(headers)
    if not any(k.lower() == 'connection' for k in sent):
        sent['Connection'] = 'close'
    budget = PAGE_DEADLINE * timeout
    socks: list = []
    expired = threading.Event()

    def stop() -> None:
        expired.set()
        for sock in list(socks):
            shut(sock)

    ends = time.monotonic() + budget

    def late(e: BaseException | None = None) -> bool:
        return expired.is_set() or (isinstance(e, TimeoutError) and time.monotonic() >= ends - DEADLINE_SLACK)

    timer = threading.Timer(budget, stop)
    timer.daemon = True
    timer.start()
    conn = None
    try:
        conn, target = wire_for(url, timeout, context, address, socks, expired, ends, proxied)
        conn.request(method, target, body=data, headers=sent)
        resp = conn.getresponse()
        head = {k.lower(): v for k, v in resp.getheaders()}
        quiet = method.upper() == 'HEAD' or resp.status in (204, 304)
        body = b'' if quiet else resp.read(limit + 1)
        if expired.is_set():
            raise TimeoutError
        if not quiet and not whole(resp, body, limit):
            raise Cut()
        return Reply(body or b'', head, resp.status)
    except ProxyRefused as e:
        if late():
            raise Unreachable(f'{where} gave no full answer within {budget:g} s') from None
        said = f' (HTTP {e.code})' if e.code is not None else ''
        raise Unreachable(f'{where} unreachable: the proxy refused the tunnel{said}') from None
    except (Cut, http.client.IncompleteRead):
        if late():
            raise Unreachable(f'{where} gave no full answer within {budget:g} s') from None
        raise Unreachable(f'{where} gave a cut-off answer') from None
    except (http.client.HTTPException, OSError, ValueError) as e:
        if late(e):
            raise Unreachable(f'{where} gave no full answer within {budget:g} s') from None
        raise Unreachable(f'{where} unreachable ({kind(e)})') from None
    finally:
        timer.cancel()
        if conn is not None:
            conn.close()


def is_address(text: str) -> bool:
    try:
        ipaddress.ip_address(str(text).strip('[]'))
    except ValueError:
        return False
    return True


def addresses(host: str, timeout: float = LOOKUP_S) -> list[str]:
    box: dict = {}

    def look() -> None:
        try:
            box['found'] = socket.getaddrinfo(host, PORTS['https'])
        except Exception as e:
            box['error'] = e

    worker = threading.Thread(target=look, name='skopax-lookup', daemon=True)
    worker.start()
    worker.join(timeout)
    if worker.is_alive():
        raise TimeoutError(f'no address within {timeout:g} s')
    if 'error' in box:
        raise box['error']
    return list(dict.fromkeys(str(found[4][0]) for found in box['found']))


def pin_of(headers: dict[str, str] | None, where: str) -> str | None:
    pin = marked(headers, PIN)
    if pin is None:
        return None
    try:
        return str(ipaddress.ip_address(pin))
    except ValueError:
        raise Unreachable(f'{where}: the pinned address is not an IP address') from None


def call_page(transport: 'Transport', url: str, headers: dict[str, str] | None = None, timeout: float = 30, limit: int = PAGE_LIMIT, pin: str | None = None) -> Reply:
    reply = transport('GET', url, {**(headers or {}), PAGE_READ: str(limit)} | ({PIN: pin} if pin else {}), None, timeout)
    if not isinstance(reply.body, bytes):
        raise Unreachable(f"{url.split('?')[0]} answered without a page")
    return reply


def sender(context: ssl.SSLContext | None = None, follow: bool = True, limit: int | None = None, proxied: bool = True) -> Transport:
    def send(method: str, url: str, headers: dict[str, str], data: bytes | None, timeout: float) -> Reply:
        where = url.split('?')[0]
        page, text, pin = page_limit(headers), limit_of(headers, TEXT_READ), pin_of(headers, where)
        asked = limit_of(headers, JSON_READ, JSON_MAX)
        headers = unmarked(headers)
        if pin is not None and page is None:
            raise Unreachable(f'{where}: a pinned address is only for a page read')
        if page is not None:
            return read_page(method, url, headers, data, timeout, page, context, pin, proxied)
        cap = min((n for n in (limit, text, asked) if n is not None), default=JSON_LIMIT)
        budget = PAGE_DEADLINE * timeout
        socks: list = []
        expired = threading.Event()

        def stop() -> None:
            expired.set()
            for sock in list(socks):
                shut(sock)

        timer = threading.Timer(budget, stop)
        timer.daemon = True
        timer.start()
        WATCH.value = (socks, expired)
        try:
            opened = (open_url if follow else open_plain) if proxied else functools.partial(open_direct, follow=follow)
            with opened(urllib.request.Request(url, data=data, headers=headers, method=method), timeout, context) as resp:
                raw, head, code = resp.read(cap + 1), {k.lower(): v for k, v in resp.headers.items()}, getattr(resp, 'status', None)
            if expired.is_set():
                raise TimeoutError
        except urllib.error.HTTPError as e:
            raise HttpError(e.code, where, error_body(e), dict(e.headers.items()) if e.headers is not None else None) from None
        except (http.client.HTTPException, OSError, ValueError) as e:
            if expired.is_set():
                raise TimedOut(f'{where} gave no full answer within {budget:g} s') from None
            reason = getattr(e, 'reason', e)
            if isinstance(reason, TimeoutError):
                raise TimedOut(f"{where} unreachable ({kind(reason)})") from None
            raise Unreachable(f"{where} unreachable ({kind(reason) if isinstance(reason, BaseException) else reason})") from None
        finally:
            timer.cancel()
            WATCH.value = None
        if len(raw) > cap:
            raise TooLarge(f'{where} answered more than {cap // MB} MB' if cap % MB == 0 else f'{where} answered more than {cap} bytes')
        if text is not None:
            return Reply(raw.decode('utf-8', 'replace'), head, code)
        try:
            return Reply(json.loads(raw or b'null'), head, code)
        except ValueError:
            raise NotJson(f'{where} answered without JSON') from None

    return send


send = sender()
direct = sender(proxied=False)
relay_send = sender(follow=False, limit=ANSWER_LIMIT)


def unproxied(transport: Transport) -> Transport:
    return direct if transport is send else transport


def error_body(e: urllib.error.HTTPError) -> str:
    try:
        return (e.read(BODY_LIMIT) or b'').decode('utf-8', 'replace')
    except Exception:
        return ''


def call(transport: Transport, method: str, url: str, headers: dict[str, str] | None = None, body: object = None, timeout: float = 30, limit: int | None = None) -> Reply:
    head = {'Accept': JSON_ACCEPT, **(headers or {})} | ({JSON_READ: str(limit)} if limit else {})
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        head['Content-Type'] = 'application/json'
    return transport(method, url, head, data, timeout)


def call_text(transport: Transport, url: str, headers: dict[str, str] | None = None, timeout: float = 30, limit: int = TEXT_LIMIT) -> str:
    body = transport('GET', url, {'Accept': TEXT_ACCEPT, **unmarked(headers), TEXT_READ: str(limit)}, None, timeout).body
    if isinstance(body, bytes):
        return body.decode('utf-8', 'replace')
    if isinstance(body, str):
        return body
    raise Unreachable(f"{url.split('?')[0]} answered without text")
