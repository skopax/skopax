import asyncio
import ipaddress
import json
import logging
import re
import socket
import ssl
import time
from collections.abc import Awaitable, Callable, Iterable, Mapping
from contextvars import ContextVar
from dataclasses import dataclass
from functools import partial
from urllib.parse import SplitResult, urlsplit

import anyio
import anyio.lowlevel
import anyio.to_thread
import httpcore
import httpx
from mcp import types
from mcp.client.auth.utils import extract_field_from_www_auth, extract_scope_from_www_auth
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.exceptions import McpError

from skopax.errkind import Authored, kind
from skopax.messages import msg
from skopax.redact import redact_text
from skopax.mcptools import (CALL_DEADLINE_S, CONNECT_TIMEOUT_S, LIST_DEADLINE_S, LIST_PAGES_MAX, READ_TIMEOUT_S, RESPONSE_BYTES_MAX, SECRET_PART_MIN, SERVER_NAME_MAX,
                             TEARDOWN_S, TOOLS_MAX, scrub, secrets_of)

log = logging.getLogger('skopax.mcptools')

REFUSED_NETS = tuple(ipaddress.ip_network(net) for net in ('169.254.0.0/16', 'fe80::/10', 'fd00:ec2::254/128', '0.0.0.0/8', '::/128', '100.100.100.200/32'))
NAT64 = ipaddress.ip_network('64:ff9b::/96')
LOCAL_HOSTS = ('localhost', '127.0.0.1', '::1')
LOOPBACK_NAMES = ('localhost',)
HEADER_NAME = re.compile(r"[!#$%&'*+\-.^_`|~0-9A-Za-z]+")
PROTECTED = ('host', 'content-type', 'accept', 'content-length', 'transfer-encoding', 'connection', 'mcp-session-id', 'mcp-protocol-version', 'accept-encoding')
BAD_VALUE = ('\r', '\n', '\x00')
PLAIN = ('', 'identity')
REJECTED = (401, 403)
UNAUTHORIZED = 401
FORBIDDEN = 403
NO_SCOPE = 'insufficient_scope'
SCOPE_TOKEN = re.compile(r'[\x21\x23-\x5b\x5d-\x7e]{1,100}')
SCOPES_SHOWN = 5
SCOPE_TEXT_MAX = 160
PERMISSION_WORDS = re.compile(r'\b(?:permission denied|insufficient permissions?|(?:do|does|did) not have (?:the )?(?:required |needed )?permissions?|no permissions?|'
                              r'lacks? (?:the )?(?:required |needed )?permissions?|access (?:is )?denied|unauthori[sz]ed|not authori[sz]ed|insufficient[ _]scope|'
                              r'missing (?:the )?scopes?|lacks? (?:the )?scopes?)\b|^\W*forbidden\s*(?:[:.,;!-]|$)|\b403\W+forbidden\b|\bforbidden\W+403\b', re.IGNORECASE)
WRONG_PATH = 32600
NOT_FOUND = 404
CHAIN_STEPS = 3
LIMIT_TEXT = '2 MB'
POOL_CONNECTIONS = 10
KEEPALIVE_S = 5

Address = ipaddress.IPv4Address | ipaddress.IPv6Address
TRIP: ContextVar[Callable[[Exception], None] | None] = ContextVar('mcptools_trip', default=None)


class Refused(Authored, Exception):
    pass


class McpFailure(Authored, Exception):
    pass


class Unauthorized(McpFailure):
    pass


class TooLarge(Exception):
    pass


def address(host: str) -> Address | None:
    try:
        found = ipaddress.ip_address(host)
    except ValueError:
        return None
    return (found.ipv4_mapped or found) if isinstance(found, ipaddress.IPv6Address) else found


def embedded(ip: Address) -> list[Address]:
    found: list[Address] = [ip]
    if isinstance(ip, ipaddress.IPv6Address) and ip.sixtofour is not None:
        found.append(ip.sixtofour)
    if isinstance(ip, ipaddress.IPv6Address) and ip in NAT64:
        found.append(ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF))
    return found


def refused(ip: Address) -> bool:
    return any(x in net for x in embedded(ip) for net in REFUSED_NETS)


def loopback(host: str) -> bool:
    ip = address(host)
    return host in LOOPBACK_NAMES if ip is None else ip.is_loopback


def split(url: str) -> SplitResult | None:
    try:
        parts = urlsplit(url)
        parts.port
    except ValueError:
        return None
    return parts


def guard_url(url: str, allow_local_http: bool, discovered: bool = False) -> None:
    parts = split(url)
    scheme, host = (parts.scheme.lower(), parts.hostname or '') if parts else ('', '')
    local_http = scheme == 'http' and allow_local_http and not discovered and host in LOCAL_HOSTS
    if parts is not None and scheme != 'https' and not local_http:
        raise Refused(msg('mcptools.url.https'))
    if parts is None or not host or '@' in parts.netloc or '#' in url:
        raise Refused(msg('mcptools.url.shape'))
    ip = address(host)
    if (ip is not None and refused(ip)) or (discovered and loopback(host) and not allow_local_http):
        raise Refused(msg('mcptools.url.blocked'))


def check_headers(headers: Mapping[str, str]) -> None:
    for name, value in headers.items():
        named = isinstance(name, str) and bool(HEADER_NAME.fullmatch(name)) and name.lower() not in PROTECTED
        if not named or not isinstance(value, str) or any(bad in value for bad in BAD_VALUE):
            raise Refused(msg('mcptools.header_refused'))


class GuardedBackend(httpcore.AsyncNetworkBackend):
    def __init__(self):
        self.backend = httpcore.AnyIOBackend()

    async def connect_tcp(self, host: str, port: int, timeout: float | None = None, local_address: str | None = None, socket_options=None) -> httpcore.AsyncNetworkStream:
        try:
            with anyio.fail_after(timeout):
                found = await anyio.to_thread.run_sync(partial(socket.getaddrinfo, host, port, type=socket.SOCK_STREAM), abandon_on_cancel=True)
        except TimeoutError:
            raise httpcore.ConnectTimeout() from None
        except OSError:
            raise httpcore.ConnectError() from None
        addresses = [address(str(info[4][0])) for info in found]
        if not addresses or any(ip is None or refused(ip) or (any(x.is_loopback for x in embedded(ip)) and not loopback(host)) for ip in addresses):
            raise Refused(msg('mcptools.url.resolved'))
        failed: httpcore.ConnectError | httpcore.ConnectTimeout | None = None
        for ip in addresses:
            try:
                return await self.backend.connect_tcp(str(ip), port, timeout=timeout, local_address=local_address, socket_options=socket_options)
            except (httpcore.ConnectError, httpcore.ConnectTimeout) as e:
                failed = e
        raise failed

    async def sleep(self, seconds: float) -> None:
        await self.backend.sleep(seconds)


def tripped(error: Exception) -> Exception:
    stop = TRIP.get()
    if stop is not None:
        stop(error)
    return error


class CappedStream(httpx.AsyncByteStream):
    def __init__(self, stream: httpx.AsyncByteStream, limit: int):
        self.stream = stream
        self.limit = limit

    async def __aiter__(self):
        read = 0
        async for chunk in self.stream:
            read += len(chunk)
            if read > self.limit:
                raise tripped(TooLarge())
            yield chunk

    async def aclose(self) -> None:
        await self.stream.aclose()


class Capped(httpx.AsyncBaseTransport):
    def __init__(self, transport: httpx.AsyncBaseTransport, limit: int = RESPONSE_BYTES_MAX):
        self.transport = transport
        self.limit = limit

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        response = await self.transport.handle_async_request(request)
        if response.headers.get('content-encoding', '').strip().lower() not in PLAIN:
            await response.aclose()
            raise Refused(msg('mcptools.encoded'))
        return httpx.Response(response.status_code, headers=response.headers, stream=CappedStream(response.stream, self.limit), extensions=response.extensions)

    async def aclose(self) -> None:
        await self.transport.aclose()


def client_for(ca: str = '') -> httpx.AsyncClient:
    transport = httpx.AsyncHTTPTransport()
    context = ssl.create_default_context(cadata=ca) if ca else ssl.create_default_context()
    transport._pool = httpcore.AsyncConnectionPool(ssl_context=context, network_backend=GuardedBackend(),
                                                   max_connections=POOL_CONNECTIONS, keepalive_expiry=KEEPALIVE_S)
    return httpx.AsyncClient(transport=Capped(transport), timeout=httpx.Timeout(READ_TIMEOUT_S, connect=CONNECT_TIMEOUT_S), follow_redirects=False,
                             headers={'accept-encoding': 'identity'})


@dataclass(frozen=True)
class Listing:
    server: str
    version: str
    tools: tuple[types.Tool, ...]
    truncated: bool
    ms: int


def leaf(e: BaseException) -> BaseException:
    while isinstance(e, BaseExceptionGroup) and e.exceptions:
        e = e.exceptions[0]
    return e


def chain(e: BaseException) -> list[BaseException]:
    found, todo = [], [(leaf(e), 0)]
    while todo:
        current, steps = todo.pop(0)
        if any(current is seen for seen in found):
            continue
        found.append(current)
        if steps < CHAIN_STEPS:
            todo += [(leaf(x), steps + 1) for x in (current.__cause__, current.__context__) if x is not None]
    return found


def refusal(scope: str, held: str | None, named: bool) -> str:
    if not named:
        return lacks(scope)
    return lacks(scope, held) if held is not None else msg('mcptools.access.open')


def failure_text(e: BaseException, secrets: Iterable[str] = (), held: str | None = None, named: bool = False) -> str:
    found = chain(e)
    for x in found:
        if isinstance(x, Refused):
            return x.text
    if any(isinstance(x, TooLarge) for x in found):
        return msg('mcptools.too_large', limit=LIMIT_TEXT)
    for x in found:
        if isinstance(x, httpx.HTTPStatusError):
            code = x.response.status_code
            if code == FORBIDDEN:
                return refusal(refused_scope(x.response, any_error=True, secrets=secrets) or '', held, named)
            missing = refused_scope(x.response, (UNAUTHORIZED,), secrets=secrets)
            if missing is not None:
                return refusal(missing, held, named)
            return msg('connection.rejected' if code == UNAUTHORIZED else 'connection.status', status=code)
    if any(isinstance(x, ssl.SSLError) for x in found):
        return msg('connection.tls')
    if any(isinstance(x, (TimeoutError, httpx.TimeoutException)) for x in found):
        return msg('connection.timeout')
    if any(isinstance(x, (httpx.TransportError, OSError)) for x in found):
        return msg('connection.unreachable')
    for x in found:
        if isinstance(x, McpError):
            if x.error.code == WRONG_PATH:
                return msg('connection.status', status=NOT_FOUND)
            if PERMISSION_WORDS.search(str(x.error.message or '')):
                return refusal('', held, named)
            return msg('mcptools.server_error', code=x.error.code)
    return msg('connection.failed', error=type(found[0]).__name__)


def body_of(response: httpx.Response) -> dict:
    try:
        found = json.loads(response.content)
    except (httpx.ResponseNotRead, ValueError):
        return {}
    return found if isinstance(found, dict) else {}


def refused_scope(response: httpx.Response, codes: tuple[int, ...] = (FORBIDDEN,), any_error: bool = False, secrets: Iterable[str] = ()) -> str | None:
    if response.status_code not in codes:
        return None
    scope = extract_scope_from_www_auth(response)
    named = extract_field_from_www_auth(response, 'error') == NO_SCOPE or (any_error and bool(scope))
    if not named:
        body = body_of(response)
        if body.get('error') != NO_SCOPE:
            return None
        scope = body.get('scope') if isinstance(body.get('scope'), str) else None
    hidden = tuple(secrets)
    parts = scrub(' '.join([part for part in (scope or '').split() if SCOPE_TOKEN.fullmatch(part)][:SCOPES_SHOWN]), hidden).split()
    shown = redact_text(' '.join(p for p in parts if not pieced(p, hidden))).split()
    kept: list[str] = []
    for part in shown:
        if len(' '.join([*kept, part])) > SCOPE_TEXT_MAX:
            break
        kept.append(part)
    return ' '.join(kept)


def pieced(part: str, secrets: tuple[str, ...]) -> bool:
    return any(s[i:i + SECRET_PART_MIN] in part for s in secrets if len(s) >= SECRET_PART_MIN for i in range(len(s) - SECRET_PART_MIN + 1))


def lacks(scope: str, credential: str | None = None) -> str:
    if credential is not None:
        return msg('mcptools.access.lacks', credential=credential, permission=msg('mcptools.access.scope', scope=scope) if scope else msg('mcptools.access.unknown'))
    return msg('mcptools.no_scope', scope=scope) if scope else msg('mcptools.no_permission')


def unauthorized(e: BaseException) -> bool:
    return any(isinstance(x, httpx.HTTPStatusError) and x.response.status_code == UNAUTHORIZED for x in chain(e))


def printable(text: str) -> str:
    return ' '.join(''.join(ch if ch.isprintable() else ' ' for ch in text).split())[:SERVER_NAME_MAX]


def shown(text: str, secrets: tuple[str, ...]) -> str:
    return printable(scrub(''.join(ch for ch in text if ch.isprintable() or ch.isspace()), secrets))


def on_loop() -> bool:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return False
    return True


class Trip:
    def __init__(self):
        self.error: Exception | None = None
        self.scope: anyio.CancelScope | None = None

    def stop(self, error: Exception) -> None:
        if self.error is None:
            self.error = error
        if self.scope is not None:
            self.scope.cancel()

    async def handler(self, message: object) -> None:
        if isinstance(message, Exception):
            self.stop(message)
        await anyio.lowlevel.checkpoint()


async def read_tools(session: ClientSession, ready: types.InitializeResult) -> tuple[types.Implementation, tuple[types.Tool, ...], bool]:
    found, cursor = [], None
    for _ in range(LIST_PAGES_MAX):
        page = await session.list_tools(params=types.PaginatedRequestParams(cursor=cursor) if cursor else None)
        found += page.tools
        cursor = page.nextCursor
        if not cursor or len(found) >= TOOLS_MAX:
            break
    return ready.serverInfo, tuple(found[:TOOLS_MAX]), bool(cursor) or len(found) > TOOLS_MAX


async def call_tool(name: str, arguments: dict, session: ClientSession, ready: types.InitializeResult) -> types.CallToolResult:
    request = types.ClientRequest(types.CallToolRequest(params=types.CallToolRequestParams(name=name, arguments=arguments)))
    return await session.send_request(request, types.CallToolResult)


Work = Callable[[ClientSession, types.InitializeResult], Awaitable]


class McpClient:
    def __init__(self, url: str, headers: Mapping[str, str], allow_local_http: bool, http: Callable[[], httpx.AsyncClient] | None = None, secrets: Iterable[str] = (),
                 held: str | None = None, named: bool = False):
        self.url = url
        self.held = held
        self.named = named
        self.headers = dict(headers)
        self.allow_local_http = allow_local_http
        self.http = http or client_for
        self.secrets = tuple(secrets)

    def listing(self, deadline: float = LIST_DEADLINE_S, secrets: Iterable[str] = ()) -> Listing:
        started = time.monotonic()
        server, tools, truncated = self._run(read_tools, deadline)
        hidden = tuple(secrets)
        return Listing(shown(server.name, hidden), shown(server.version, hidden), tools, truncated, int((time.monotonic() - started) * 1000))

    def call(self, name: str, arguments: dict, deadline: float = CALL_DEADLINE_S) -> types.CallToolResult:
        return self._run(partial(call_tool, name, arguments), deadline)

    def _run(self, work: Work, deadline: float):
        if on_loop():
            raise RuntimeError('the MCP client blocks its thread, so it never runs on an event loop')
        try:
            guard_url(self.url, self.allow_local_http)
            check_headers(self.headers)
            return asyncio.run(self._session(work, deadline))
        except Exception as e:
            text = failure_text(e, self.hidden(), self.held, self.named)
            if unauthorized(e):
                raise Unauthorized(text) from None
        raise McpFailure(text)

    def hidden(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys([*(s for value in self.headers.values() for s in secrets_of({'header-value': value})), *self.secrets]))

    async def _session(self, work: Work, deadline: float):
        trip, held = Trip(), []
        TRIP.set(trip.stop)
        try:
            with anyio.CancelScope(deadline=anyio.current_time() + deadline) as scope:
                trip.scope = scope
                async with self.http() as client:
                    client.headers.update(self.headers)
                    async with streamable_http_client(self.url, http_client=client) as (read, write, _):
                        async with ClientSession(read, write, message_handler=trip.handler) as session:
                            held.append(await work(session, await session.initialize()))
                            scope.deadline = anyio.current_time() + TEARDOWN_S
        except Exception as e:
            if not held:
                raise
            log.warning('MCP session teardown failed after the answer: %s', kind(e))
        if held:
            if trip.error is not None or scope.cancelled_caught:
                log.warning('MCP session teardown cut after the answer: %s', kind(trip.error) if trip.error is not None else 'timeout')
            return held[0]
        if trip.error is not None:
            raise trip.error
        raise TimeoutError
