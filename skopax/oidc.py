import functools
import hmac
import http.client
import io
import json
import logging
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Callable, Mapping, Sequence

import jwt

from skopax.clock import stamp
from skopax.errkind import kind as error_kind

log = logging.getLogger('skopax.oidc')

DISCOVERY_PATH = '/.well-known/openid-configuration'
DISCOVERY_TTL_S = 3600
KEYS_TTL_S = 600
RETRY_S = 30
STALE_MAX_S = 86400
CLOCK_SKEW_S = 30
ALGORITHMS = ('RS256',)
FETCH_TIMEOUT_S = 10
FETCH_TOTAL_S = 10
MAX_BODY = 1 << 20
CHUNK = 64 << 10
LOOPBACK = ('localhost', '127.0.0.1', '::1')
FIELDS = {'authorize': 'authorization_endpoint', 'token': 'token_endpoint', 'jwks': 'jwks_uri'}
REQUIRED = ['exp', 'iat', 'iss', 'aud', 'sub']
CLIENT_CLAIMS = ('azp', 'client_id', 'cid')
FETCH_ERRORS = (OSError, ValueError, http.client.HTTPException)


class AuthError(Exception):
    pass


class OidcError(AuthError):
    def __init__(self, reason: str, detail: str = ''):
        self.reason = reason
        self.detail = detail
        super().__init__(f'{reason}: {detail}' if detail else reason)


class Watch:
    def __init__(self, total: float):
        self.ends = time.monotonic() + total
        self.lock = threading.Lock()
        self.current = None
        self.expired = False

    def left(self) -> float:
        return self.ends - time.monotonic()

    def step(self) -> float:
        left = self.left()
        if left <= 0 or self.expired:
            self.expire()
            raise too_slow()
        return min(FETCH_TIMEOUT_S, left)

    def arm(self, sock) -> None:
        sock.settimeout(self.step())

    def hold(self, sock):
        with self.lock:
            self.current = sock
        self.step()
        return sock

    def expire(self) -> None:
        with self.lock:
            self.expired = True
            sock = self.current
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass


class TimedIO(socket.SocketIO):
    def __init__(self, sock, watch: Watch):
        super().__init__(sock, 'rb')
        self.watch = watch

    def readinto(self, b):
        self.watch.arm(self._sock)
        return super().readinto(b)


class Timed:
    def __init__(self, sock, watch: Watch):
        self.raw = sock
        self.watch = watch

    def makefile(self, mode: str = 'rb', *args, **kwargs):
        reader = io.BufferedReader(TimedIO(self.raw, self.watch))
        self.raw._io_refs += 1
        return reader

    def sendall(self, data, *args):
        self.watch.arm(self.raw)
        return self.raw.sendall(data, *args)

    def __getattr__(self, name: str):
        return getattr(self.raw, name)


def timed_socket(watch: Watch, address, timeout=None, source_address=None) -> Timed:
    raw = socket.create_connection(address, watch.step(), source_address)
    try:
        watch.hold(raw)
    except OidcError:
        raw.close()
        raise
    return Timed(raw, watch)


class TimedHttp(http.client.HTTPConnection):
    def __init__(self, watch: Watch, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.watch = watch
        self._create_connection = functools.partial(timed_socket, watch)


class TimedHttps(http.client.HTTPSConnection):
    def __init__(self, watch: Watch, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.watch = watch
        self._create_connection = functools.partial(timed_socket, watch)

    def connect(self) -> None:
        http.client.HTTPConnection.connect(self)
        raw = self.sock.raw
        self.watch.arm(raw)
        tls = self._context.wrap_socket(raw, server_hostname=self._tunnel_host or self.host)
        try:
            self.watch.hold(tls)
        except OidcError:
            tls.close()
            raise
        self.sock = Timed(tls, self.watch)


class WatchedHttp(urllib.request.HTTPHandler):
    def __init__(self, watch: Watch):
        super().__init__()
        self.watch = watch

    def http_open(self, req):
        return self.do_open(functools.partial(TimedHttp, self.watch), req)


class WatchedHttps(urllib.request.HTTPSHandler):
    def __init__(self, watch: Watch):
        super().__init__()
        self.watch = watch

    def https_open(self, req):
        return self.do_open(functools.partial(TimedHttps, self.watch), req, context=self._context)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def open_url(request: urllib.request.Request, timeout: float, watch: Watch):
    return urllib.request.build_opener(NoRedirect(), WatchedHttp(watch), WatchedHttps(watch)).open(request, timeout=timeout)


def too_slow() -> OidcError:
    return OidcError('unreachable', f'the response took longer than {FETCH_TOTAL_S:g} seconds')


def read_capped(resp, watch: Watch) -> bytes:
    read = getattr(resp, 'read1', None) or resp.read
    parts, size = [], 0
    while size <= MAX_BODY:
        if watch.left() <= 0:
            raise too_slow()
        try:
            part = read(min(CHUNK, MAX_BODY + 1 - size))
        except TimeoutError:
            raise too_slow() from None
        if not part:
            break
        parts.append(part)
        size += len(part)
    return b''.join(parts)


def gave_up(e: BaseException, watch: Watch) -> bool:
    seen = set()
    while isinstance(e, BaseException) and id(e) not in seen:
        seen.add(id(e))
        if isinstance(e, (TimeoutError, OidcError)) or watch.expired:
            return True
        nxt = e.reason if isinstance(e, urllib.error.URLError) else e.__cause__ or e.__context__
        e = nxt if isinstance(nxt, BaseException) else None
    return False


def fetch_body(request: urllib.request.Request) -> bytes:
    total = FETCH_TOTAL_S
    watch = Watch(total)
    timer = threading.Timer(total, watch.expire)
    timer.daemon = True
    timer.start()
    try:
        with open_url(request, min(FETCH_TIMEOUT_S, total), watch) as resp:
            body = read_capped(resp, watch)
    except OidcError:
        raise
    except FETCH_ERRORS as e:
        if isinstance(e, urllib.error.HTTPError):
            e.close()
        if gave_up(e, watch):
            raise too_slow() from None
        raise
    finally:
        timer.cancel()
    if watch.expired or watch.left() <= 0:
        raise too_slow()
    return body


def get_json(url: str) -> dict:
    body = fetch_body(urllib.request.Request(url, headers={'Accept': 'application/json'}))
    if len(body) > MAX_BODY:
        raise OidcError('discovery', 'the response is larger than 1 MiB')
    try:
        doc = json.loads(body)
    except (ValueError, RecursionError) as e:
        raise OidcError('discovery', 'the response is not JSON') from e
    if not isinstance(doc, dict):
        raise OidcError('discovery', 'the response is not a JSON object')
    return doc


def url_problem(url: str) -> str | None:
    try:
        parts = urllib.parse.urlsplit(url)
        host = parts.hostname
    except (TypeError, ValueError, AttributeError):
        return 'sso.url_scheme'
    if not host or not (parts.scheme == 'https' or parts.scheme == 'http' and host in LOOPBACK):
        return 'sso.url_scheme'
    if '@' in parts.netloc:
        return 'sso.url_userinfo'
    if '#' in url:
        return 'sso.url_fragment'
    return None


def strings(value) -> tuple[str, ...]:
    return tuple(str(v) for v in value) if isinstance(value, list) else ()


@dataclass(frozen=True)
class Endpoints:
    issuer: str
    authorize: str
    token: str
    jwks: str
    auth_methods: tuple[str, ...] = ()
    pkce: tuple[str, ...] = ()
    discovered: bool = True


class Discovery:
    def __init__(self, issuer: str, overrides: Mapping[str, str] = {}, fetch: Callable[[str], dict] = get_json, clock: Callable[[], float] = time.monotonic):
        self.issuer = issuer
        self.overrides = {k: v for k, v in overrides.items() if k in FIELDS and v}
        self.fetch = fetch
        self.clock = clock
        self.lock = threading.Lock()
        self.cached: Endpoints | None = None
        self.loaded_at: float | None = None
        self.tried_at: float | None = None
        self.failure: OidcError | None = None
        self.health: tuple[bool | None, str | None, str | None] = (None, None, None)

    @property
    def url(self) -> str:
        return self.issuer.removesuffix('/') + DISCOVERY_PATH

    def endpoints(self) -> Endpoints:
        for name, value in [('issuer', self.issuer), *self.overrides.items()]:
            problem = url_problem(value)
            if problem:
                raise OidcError('endpoint', f'{name}: {problem}')
        if len(self.overrides) == len(FIELDS):
            return Endpoints(issuer=self.issuer, discovered=False, **self.overrides)
        with self.lock:
            now = self.clock()
            if self.cached is not None and now - self.loaded_at < DISCOVERY_TTL_S:
                return self.cached
            if self.failure is None or now - self.tried_at >= RETRY_S:
                self._load(now)
            if self.failure is None:
                return self.cached
            if self.cached is not None and now - self.loaded_at < STALE_MAX_S:
                return self.cached
            raise OidcError(self.failure.reason, self.failure.detail)

    def status(self) -> dict:
        healthy, since, error = self.health
        return {'ok': healthy is not False, 'since': since, 'error': error}

    def _load(self, now: float) -> None:
        self.tried_at = now
        try:
            found = self.read(self.fetch(self.url))
        except OidcError as e:
            self._failed(e)
        except FETCH_ERRORS as e:
            self._failed(OidcError('discovery', error_kind(e)))
        else:
            self.cached, self.loaded_at, self.failure = found, now, None
            self._settle(True, None)

    def _failed(self, error: OidcError) -> None:
        if self.failure is None:
            log.warning('the identity provider discovery failed (%s), keeping the last good document while it is under a day old', error.reason)
        self.failure = error
        self._settle(False, error.reason)

    def _settle(self, healthy: bool, error: str | None) -> None:
        was, since, _ = self.health
        self.health = (healthy, since if healthy == was else stamp(time.time()), error)

    def read(self, doc: dict) -> Endpoints:
        if not isinstance(doc, dict):
            raise OidcError('discovery', 'the document is not a JSON object')
        issuer = doc.get('issuer')
        if not isinstance(issuer, str) or issuer.removesuffix('/') != self.issuer.removesuffix('/'):
            raise OidcError('issuer', 'the document names another issuer')
        urls = {}
        for name, field in FIELDS.items():
            value = self.overrides.get(name) or doc.get(field)
            if not isinstance(value, str) or not value:
                raise OidcError('discovery', f'the document has no {field}')
            problem = url_problem(value)
            if problem:
                raise OidcError('endpoint', f'{field}: {problem}')
            urls[name] = value
        return Endpoints(issuer=issuer, auth_methods=strings(doc.get('token_endpoint_auth_methods_supported')), pkce=strings(doc.get('code_challenge_methods_supported')), **urls)


def signing_keys(entries) -> list[jwt.PyJWK]:
    keys = []
    for entry in entries:
        if not isinstance(entry, dict) or entry.get('kty') != 'RSA' or entry.get('use', 'sig') != 'sig' or entry.get('alg', 'RS256') not in ALGORITHMS:
            continue
        try:
            keys.append(jwt.PyJWK(entry, algorithm='RS256'))
        except (jwt.PyJWTError, ValueError, TypeError, KeyError):
            continue
    return keys


class SigningKeys:
    def __init__(self, url: str, fetch: Callable[[str], dict] = get_json, clock: Callable[[], float] = time.monotonic):
        self.url = url
        self.fetch = fetch
        self.clock = clock
        self.keys: list[jwt.PyJWK] = []
        self.loaded_at: float | None = None
        self.tried_at: float | None = None
        self.failing = False
        self.lock = threading.Lock()

    def key_for(self, kid: str | None) -> object:
        with self.lock:
            now = self.clock()
            if (self.loaded_at is None or now - self.loaded_at >= KEYS_TTL_S) and self._may_try(now):
                self._load(now)
            match = self._pick(kid, now)
            if match is None and self._may_try(now):
                self._load(now)
                match = self._pick(kid, now)
            usable = bool(self._usable(now))
        if match is None and not usable:
            raise OidcError('unreachable', 'no usable signing keys could be fetched')
        if match is None:
            raise OidcError('keys', 'no signing key matches the token')
        return match.key

    def _may_try(self, now: float) -> bool:
        return self.tried_at is None or now - self.tried_at >= RETRY_S

    def _load(self, now: float) -> None:
        self.tried_at = now
        try:
            doc = self.fetch(self.url)
            entries = doc.get('keys') if isinstance(doc, dict) else None
            found = signing_keys(entries) if isinstance(entries, list) else []
            if not found:
                raise OidcError('keys', 'the key set has no usable signing keys')
        except (OidcError, *FETCH_ERRORS) as e:
            if not self.failing:
                log.warning('the signing keys could not be fetched (%s), keeping the last good keys while they are under a day old', error_kind(e))
            self.failing = True
            return
        self.keys, self.loaded_at, self.failing = found, now, False

    def _usable(self, now: float) -> list[jwt.PyJWK]:
        return self.keys if self.loaded_at is not None and now - self.loaded_at < STALE_MAX_S else []

    def _pick(self, kid: str | None, now: float) -> jwt.PyJWK | None:
        keys = self._usable(now)
        if kid is None:
            return keys[0] if len(keys) == 1 else None
        return next((k for k in keys if k.key_id == kid), None)


def same(value, expected: str) -> bool:
    return isinstance(value, str) and hmac.compare_digest(value.encode(), expected.encode())


def verify(token: str, keys: SigningKeys, *, issuers: Sequence[str], audiences: Sequence[str], nonce: str | None = None, client_id: str | None = None,
           allowed_clients: Sequence[str] = ()) -> dict:
    for name, value in (('issuers', issuers), ('audiences', audiences), ('allowed_clients', allowed_clients)):
        if isinstance(value, str):
            raise TypeError(f'{name} must be a sequence of strings, not one string')
    try:
        header = jwt.get_unverified_header(token)
    except jwt.PyJWTError as e:
        raise OidcError('signature', error_kind(e)) from e
    if header.get('alg') not in ALGORITHMS:
        raise OidcError('algorithm', 'the token is not signed with an allowed algorithm')
    kid = header.get('kid')
    if kid is not None and not isinstance(kid, str):
        raise OidcError('keys', 'the token key id is not a string')
    key = keys.key_for(kid)
    try:
        claims = jwt.decode(token, key, algorithms=list(ALGORITHMS), audience=list(audiences), issuer=list(issuers), leeway=CLOCK_SKEW_S, options={'require': REQUIRED})
    except (jwt.ExpiredSignatureError, jwt.ImmatureSignatureError) as e:
        raise OidcError('expired', error_kind(e)) from e
    except jwt.InvalidAudienceError as e:
        raise OidcError('audience', error_kind(e)) from e
    except jwt.InvalidIssuerError as e:
        raise OidcError('issuer', error_kind(e)) from e
    except (jwt.DecodeError, jwt.InvalidAlgorithmError, jwt.InvalidKeyError) as e:
        raise OidcError('signature', error_kind(e)) from e
    except (jwt.PyJWTError, ValueError, TypeError) as e:
        raise OidcError('claims', error_kind(e)) from e
    if nonce is not None and not same(claims.get('nonce'), nonce):
        raise OidcError('nonce', 'the token does not carry this login nonce')
    aud = claims.get('aud')
    if client_id is not None and (isinstance(aud, list) and len(aud) > 1 or 'azp' in claims) and not same(claims.get('azp'), client_id):
        raise OidcError('client', 'the authorized party is another client')
    if allowed_clients:
        party = next((claims[k] for k in CLIENT_CLAIMS if claims.get(k) not in (None, '')), None)
        if not isinstance(party, str) or party not in allowed_clients:
            raise OidcError('client', 'the token was issued to a client that is not allowed')
    return claims


class Provider:
    def __init__(self, issuer: str, overrides: Mapping[str, str] = {}, fetch: Callable[[str], dict] = get_json, clock: Callable[[], float] = time.monotonic):
        self.discovery = Discovery(issuer, overrides, fetch, clock)
        self.fetch = fetch
        self.clock = clock
        self.lock = threading.Lock()
        self.current: SigningKeys | None = None

    def endpoints(self) -> Endpoints:
        return self.discovery.endpoints()

    def keys(self) -> SigningKeys:
        url = self.endpoints().jwks
        with self.lock:
            if self.current is None or self.current.url != url:
                self.current = SigningKeys(url, self.fetch, self.clock)
            return self.current

    def status(self) -> dict:
        return self.discovery.status()
