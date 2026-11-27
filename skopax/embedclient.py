import json
import logging
import math
import socket
import threading
import time
from collections.abc import Callable
from urllib.parse import urlsplit

import httpcore2
import httpx2

from skopax.ai.compat_ai import GuardedTransport, POOL_CONNECTIONS, KEEPALIVE_S, blocked, inside
from skopax.config import EMBED_MODEL, Settings
from skopax.errkind import Authored
from skopax.mcptools.client import address
from skopax.messages import msg

log = logging.getLogger('skopax.embed')

KINDS = ('down', 'refused', 'mismatch', 'bad_answer', 'auth', 'off')
SCHEMES = ('http', 'https')
URL_MAX = 500
PATH = '/v1/embeddings'
ANSWER_MAX = 8 * 1024 * 1024
KEY_CHARS = frozenset(chr(c) for c in range(0x21, 0x7f))
SHAPED = (400, 413, 422)
REJECTED = (401, 403)
THRESHOLD = 3
FIRST_WAIT_S = 30
LAST_WAIT_S = 600
PROBE_STALE_S = 300
CLOSED, OPEN, HALF_OPEN = 'closed', 'open', 'half-open'
KNOWN_DIMS = {EMBED_MODEL: 384}
LEARNED: dict[str, int] = {}

resolve = socket.getaddrinfo


class EmbedError(Authored, Exception):
    def __init__(self, kind: str, counts: bool = True, local: bool = False):
        text = msg('embed.reason.no_url') if kind == 'off' else msg('embed.blocked') if local else msg(f'embed.{kind}')
        super().__init__(text)
        self.kind = kind
        self.counts = counts


class RefusedAddress(Exception):
    pass


class BlockedAddress(RefusedAddress):
    pass


def dims_of(model: str) -> int | None:
    return KNOWN_DIMS.get(model) or LEARNED.get(model)


def host_of(url: str) -> str:
    try:
        parts = urlsplit(url or '')
        host, port = parts.hostname or '', parts.port
    except ValueError:
        return ''
    shown = f'[{host}]' if ':' in host else host
    return f'{shown}:{port}' if host and port else shown


def addresses(host: str, port: int | None) -> list:
    literal = address(host)
    if literal is not None:
        return [literal]
    return [address(str(info[4][0])) for info in resolve(host, port, type=socket.SOCK_STREAM) or ()]


def check_addresses(found: list, allow_public: bool) -> None:
    if any(ip is None or blocked(ip) for ip in found):
        raise BlockedAddress()
    if not allow_public and any(not inside(ip) for ip in found):
        raise RefusedAddress()


class EmbedBackend(httpcore2.NetworkBackend):
    def __init__(self, allow_public: Callable[[], bool]):
        self.allow_public = allow_public
        self.backend = httpcore2.SyncBackend()

    def connect_tcp(self, host: str, port: int, timeout: float | None = None, local_address: str | None = None, socket_options=None) -> httpcore2.NetworkStream:
        try:
            found = addresses(host, port)
        except (OSError, UnicodeError):
            raise httpcore2.ConnectError() from None
        if not found:
            raise httpcore2.ConnectError()
        check_addresses(found, bool(self.allow_public()))
        failed: Exception = httpcore2.ConnectError()
        for ip in found:
            try:
                return self.backend.connect_tcp(str(ip), port, timeout=timeout, local_address=local_address, socket_options=socket_options)
            except (httpcore2.ConnectError, httpcore2.ConnectTimeout) as e:
                failed = e
        raise failed

    def sleep(self, seconds: float) -> None:
        self.backend.sleep(seconds)


class EmbedTransport(GuardedTransport):
    def __init__(self, allow_public: Callable[[], bool]):
        self.backend = EmbedBackend(allow_public)
        self.pool = httpcore2.ConnectionPool(ssl_context=httpx2.create_ssl_context(), network_backend=self.backend, max_connections=POOL_CONNECTIONS, keepalive_expiry=KEEPALIVE_S)


class Breaker:
    def __init__(self, clock: Callable[[], float] = time.monotonic):
        self.clock = clock
        self.lock = threading.Lock()
        self.generation = 0
        self.seen = False
        self.reset()

    def reset(self) -> None:
        self.mode = CLOSED
        self.failures = 0
        self.trips = 0
        self.until = 0.0
        self.probing = False
        self.probe_at = 0.0
        self.last_kind: str | None = None

    def allow(self) -> bool:
        return self.admit() is not None

    def admit(self) -> int | None:
        with self.lock:
            now = self.clock()
            if self.mode == CLOSED:
                return self.generation
            if self.mode == OPEN and now >= self.until:
                self.mode = HALF_OPEN
                self.probing = False
            if self.mode == HALF_OPEN and (not self.probing or now - self.probe_at > PROBE_STALE_S):
                self.probing, self.probe_at = True, now
                return self.generation
            return None

    def stale(self, generation: int | None) -> bool:
        return generation is not None and generation != self.generation

    def success(self, generation: int | None = None) -> None:
        with self.lock:
            self.seen = True
            if self.stale(generation):
                return
            if self.mode != CLOSED:
                self.generation += 1
            self.reset()

    def reached(self, kind: str) -> None:
        with self.lock:
            self.seen = True
            if self.mode != CLOSED:
                self.generation += 1
            self.reset()
            self.last_kind = kind

    def neutral(self, kind: str, generation: int | None = None) -> None:
        with self.lock:
            self.seen = True
            self.last_kind = kind
            if not self.stale(generation):
                self.probing = False

    def note(self, kind: str) -> None:
        with self.lock:
            self.seen = True
            self.last_kind = kind

    def failure(self, kind: str, generation: int | None = None) -> None:
        with self.lock:
            self.seen = True
            self.last_kind = kind
            if self.stale(generation):
                return
            self.probing = False
            if self.mode == HALF_OPEN:
                self.trip()
                return
            self.failures += 1
            if self.mode == CLOSED and self.failures >= THRESHOLD:
                self.trip()

    def trip(self) -> None:
        wait = min(FIRST_WAIT_S * 2 ** self.trips, LAST_WAIT_S)
        self.trips += 1
        self.mode = OPEN
        self.generation += 1
        self.until = self.clock() + wait
        log.warning('the embedding service is marked down for %d seconds', wait)

    def state(self) -> dict:
        with self.lock:
            now = self.clock()
            mode = HALF_OPEN if self.mode == OPEN and now >= self.until else self.mode
            return {'state': mode, 'failures': self.failures, 'retry_in': max(0.0, self.until - now) if mode == OPEN else 0, 'kind': self.last_kind, 'seen': self.seen}


BREAKER = Breaker()


def checked_url(url: str) -> str:
    text = url.strip()
    try:
        parts = urlsplit(text)
        parts.port
    except ValueError:
        raise EmbedError('refused') from None
    if (parts.scheme.lower() not in SCHEMES or '@' in parts.netloc or not parts.hostname or parts.query or parts.fragment or '?' in text or '#' in text or len(text) > URL_MAX
            or any(c.isspace() for c in text)):
        raise EmbedError('refused')
    return text.rstrip('/')


def checked_target(url: str, allow_public: bool) -> str:
    base = checked_url(url)
    parts = urlsplit(base)
    try:
        found = addresses(parts.hostname or '', parts.port)
    except (OSError, UnicodeError):
        raise EmbedError('down') from None
    if not found:
        raise EmbedError('down')
    try:
        check_addresses(found, allow_public)
    except BlockedAddress:
        raise EmbedError('refused', local=True) from None
    except RefusedAddress:
        raise EmbedError('refused') from None
    return base


def number(value) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise EmbedError('bad_answer')
    try:
        found = float(value)
    except OverflowError:
        raise EmbedError('bad_answer') from None
    if not math.isfinite(found):
        raise EmbedError('bad_answer')
    return found


def vectors_of(raw: bytes, count: int, model: str) -> list[list[float]]:
    try:
        body = json.loads(raw)
    except ValueError:
        raise EmbedError('bad_answer') from None
    if not isinstance(body, dict):
        raise EmbedError('bad_answer')
    if body.get('model') != model:
        raise EmbedError('mismatch')
    data = body.get('data')
    if not isinstance(data, list) or len(data) != count:
        raise EmbedError('bad_answer')
    slots: list[list[float] | None] = [None] * count
    for item in data:
        index = item.get('index') if isinstance(item, dict) else None
        vector = item.get('embedding') if isinstance(item, dict) else None
        if isinstance(index, bool) or not isinstance(index, int) or not 0 <= index < count or slots[index] is not None or not isinstance(vector, list) or not vector:
            raise EmbedError('bad_answer')
        slots[index] = [number(v) for v in vector]
    found = [v for v in slots if v is not None]
    if len(found) != count:
        raise EmbedError('bad_answer')
    expected = dims_of(model) or len(found[0])
    if any(len(v) != expected for v in found):
        raise EmbedError('bad_answer')
    LEARNED.setdefault(model, expected)
    return found


def failed_status(status: int) -> EmbedError:
    if status in REJECTED:
        return EmbedError('auth')
    if status in SHAPED:
        return EmbedError('bad_answer', counts=False)
    if status == 429 or status >= 500:
        return EmbedError('down')
    return EmbedError('bad_answer')


class EmbedClient:
    def __init__(self, settings: Callable[[], Settings], breaker: Breaker | None = None, transport: httpx2.BaseTransport | None = None):
        self.settings = settings
        self.breaker = breaker or BREAKER
        self.http = httpx2.Client(transport=transport or EmbedTransport(lambda: bool(self.settings().embed_allow_public)), follow_redirects=False, trust_env=False)

    def __repr__(self) -> str:
        return f'EmbedClient({host_of(self.settings().embed_url)})'

    def embed_queries(self, texts: list[str]) -> list[list[float]]:
        s = self.settings()
        return self.run(s, texts, s.query_prefix, s.embed_timeout_seconds)

    def embed_passages(self, texts: list[str]) -> list[list[float]]:
        s = self.settings()
        return self.run(s, texts, s.passage_prefix, s.embed_batch_timeout_seconds)

    def run(self, s: Settings, texts: list[str], prefix: str, timeout: float) -> list[list[float]]:
        out: list[list[float]] = []
        size = max(1, s.embed_batch)
        for at in range(0, len(texts), size):
            out.extend(self.send(s, [prefix + t for t in texts[at:at + size]], timeout))
        return out

    def send(self, s: Settings, inputs: list[str], timeout: float) -> list[list[float]]:
        if not s.embed_url.strip():
            raise EmbedError('off', counts=False)
        key = s.embed_api_key
        if key and not set(key) <= KEY_CHARS:
            raise self.noted(EmbedError('auth', counts=False))
        try:
            base = checked_target(s.embed_url, bool(s.embed_allow_public))
        except EmbedError as e:
            raise self.noted(e) from None
        ticket = self.breaker.admit()
        if ticket is None:
            raise EmbedError('down')
        try:
            found = self.post(base, s, inputs, timeout, key)
        except EmbedError as e:
            (self.breaker.failure if e.counts else self.breaker.neutral)(e.kind, ticket)
            log.warning('the embedding service call failed: %s', e.kind)
            raise
        except BlockedAddress:
            self.breaker.neutral('refused', ticket)
            raise EmbedError('refused', local=True) from None
        except RefusedAddress:
            self.breaker.neutral('refused', ticket)
            raise EmbedError('refused') from None
        except Exception as e:
            self.breaker.failure('down', ticket)
            log.warning('the embedding service call failed: %s', type(e).__name__)
            raise EmbedError('down') from None
        self.breaker.success(ticket)
        return found

    def noted(self, error: EmbedError) -> EmbedError:
        self.breaker.note(error.kind)
        return error

    def post(self, base: str, s: Settings, inputs: list[str], timeout: float, key: str) -> list[list[float]]:
        headers = {'Content-Type': 'application/json', 'Accept': 'application/json'}
        if key:
            headers['Authorization'] = f'Bearer {key}'
        body = json.dumps({'input': inputs, 'model': s.embed_model, 'encoding_format': 'float'}).encode()
        with self.http.stream('POST', base + PATH, content=body, headers=headers, timeout=httpx2.Timeout(timeout)) as reply:
            if reply.status_code != 200:
                raise failed_status(reply.status_code)
            chunks, size = [], 0
            for chunk in reply.iter_bytes():
                size += len(chunk)
                if size > ANSWER_MAX:
                    raise EmbedError('bad_answer')
                chunks.append(chunk)
        return vectors_of(b''.join(chunks), len(inputs), s.embed_model)


def status(settings: Settings, breaker: Breaker | None = None) -> dict:
    state = (breaker or BREAKER).state()
    shown = {'model': settings.embed_model, 'dims': dims_of(settings.embed_model), 'host': host_of(settings.embed_url)}
    if not settings.embed_url.strip():
        return {'up': False, 'kind': 'off', 'message': msg('embed.reason.no_url'), **shown}
    kind = state['kind'] or ('down' if state['state'] != CLOSED else None)
    if kind is None and not state['seen']:
        return {'up': None, 'kind': 'unknown', 'message': '', **shown}
    up = kind is None and state['state'] == CLOSED
    return {'up': up, 'kind': kind, 'message': '' if kind is None else msg(f'embed.{kind}'), **shown}
