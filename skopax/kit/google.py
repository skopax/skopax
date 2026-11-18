import json
import re
import time
from collections.abc import Callable
from urllib.parse import urlencode

import jwt

from skopax.contract import HttpError, ReadError, TestResult, Transport, Unreachable, anomaly, call
from skopax.errkind import kind
from skopax.kit import connection
from skopax.messages import msg

TOKEN_URL = 'https://oauth2.googleapis.com/token'
SCOPES = ('https://www.googleapis.com/auth/analytics.readonly', 'https://www.googleapis.com/auth/cloud-platform', 'https://www.googleapis.com/auth/tagmanager.readonly')
TOKEN_SECONDS = 3600
TOKEN_SLACK = 60
TIMEOUT = 30
ERROR_LIMIT = 200
RETRY_CODES = (500, 502, 503, 504)
RETRY_PAUSE = 2.0
KEY_REFUSED = (400, 401, 403)
DENIED = 'PERMISSION_DENIED'
UNAUTHENTICATED = 'UNAUTHENTICATED'
NEEDED = 'the needed permission'
ACCOUNT = 'the service account'
CODE = re.compile(r'[a-z_]{1,40}')
MASK = '***'


class GoogleError(ReadError):
    def __init__(self, status: int, message: str, reason: str | None = None, reached: bool = True, needs: str = '', invalid: bool = False, key_refused: bool = False):
        super().__init__(status, message)
        self.reason = reason or message
        self.reached = reached
        self.needs = needs
        self.invalid = invalid
        self.key_refused = key_refused

    @property
    def denied(self) -> bool:
        return bool(self.needs)

    @property
    def lacks(self) -> str:
        return f'is invalid or lacks {self.needs or NEEDED}' if self.invalid else f'lacks {self.needs or NEEDED}'

    def named(self, part: str) -> 'GoogleError':
        return GoogleError(self.status, f'{part}: {self.message}', f'{part}: {self.reason}', self.reached, self.needs, self.invalid, self.key_refused)

    def as_refusal(self, needs: str) -> 'GoogleError':
        if not self.key_refused:
            return self
        head = f'{ACCOUNT} is invalid or lacks {needs or NEEDED}'
        return GoogleError(self.status, f'{head}: {self.message}', f'{head}: {self.reason}', self.reached, needs or NEEDED, True, True)


def service_key(text: str) -> dict | None:
    try:
        key = json.loads(text or '')
    except ValueError:
        return None
    if not isinstance(key, dict) or not isinstance(key.get('client_email'), str) or not isinstance(key.get('private_key'), str) or not key['client_email'] or not key['private_key']:
        return None
    return key


def google_error(body: str, field: str, limit: int | None = ERROR_LIMIT) -> str:
    try:
        got = json.loads(body or '')
    except ValueError:
        return ''
    error = got.get('error') if isinstance(got, dict) else None
    if field == 'error':
        found = error if isinstance(error, str) else ''
    else:
        found = str(error.get(field) or '') if isinstance(error, dict) else ''
    return found[:limit]


def bounded(transport: Transport, budget: float, clock: Callable[[], float] = time.monotonic) -> Transport:
    deadline = clock() + budget

    def send(method: str, url: str, headers: dict[str, str], data: bytes | None, timeout: float):
        left = deadline - clock()
        if left <= 0:
            raise GoogleError(504, f'Google reads stopped at their {budget:g} s budget')
        return transport(method, url, headers, data, min(timeout, max(left, 1)))

    return send


def connection_test(ask: Callable[[], str]) -> TestResult:
    def run() -> str:
        try:
            return ask()
        except GoogleError as e:
            if not e.reached:
                raise Unreachable(e.reason) from None
            if e.key_refused:
                raise ReadError(e.status, msg('connection.rejected', status=e.status)) from None
            raise

    return connection.result(run)


def access_key(check: str, part: str) -> str:
    return f'{check}:access:{part}'


def access_card(check: str, plugin: str, part: str, shown: str, error: GoogleError) -> dict:
    label = f'{plugin} cannot read {shown}: {ACCOUNT} {error.lacks}'
    what = f'{plugin} could not read {shown}: {error.reason}. The cards of this part stay open until a pass reads it again, and this card clears then.'
    x = anomaly(check, 'no-access', '-', check, part, 'warn', 1, None, None, '', label, what, 1, f'WARN {label}')
    x['ranKey'] = access_key(check, part)
    return x


class GoogleAuth:
    def __init__(self, credentials: str, transport: Transport, clock: Callable[[], float], scopes: tuple[str, ...], pause: Callable[[float], None] = time.sleep):
        self.key = service_key(credentials)
        self.transport = transport
        self.clock = clock
        self.scopes = scopes
        self.pause = pause
        self.token: str | None = None
        self.expires = 0.0
        self.hidden: list[str] = []

    @property
    def enabled(self) -> bool:
        return self.key is not None

    def hide(self, *secrets: str) -> None:
        self.hidden += [s for s in secrets if isinstance(s, str) and s and s not in self.hidden]

    def scrub(self, text: str) -> str:
        own = ([self.token] if self.token else []) + self.hidden
        if self.key is not None:
            own += [str(self.key.get(k)) for k in ('private_key', 'private_key_id') if self.key.get(k)]
        for secret in own:
            text = text.replace(secret, MASK)
        return text[:ERROR_LIMIT]

    def drop(self) -> None:
        self.token = None

    def _assertion(self, now: float) -> str | None:
        if self.key is None:
            return None
        claims = {'iss': self.key['client_email'], 'scope': ' '.join(self.scopes), 'aud': TOKEN_URL, 'iat': int(now), 'exp': int(now) + TOKEN_SECONDS}
        headers = {'kid': self.key['private_key_id']} if self.key.get('private_key_id') else None
        try:
            return jwt.encode(claims, self.key['private_key'], algorithm='RS256', headers=headers)
        except (ValueError, TypeError, jwt.PyJWTError):
            return None

    def access(self) -> str:
        now = self.clock()
        if self.token and now < self.expires - TOKEN_SLACK:
            return self.token
        assertion = self._assertion(now)
        if assertion is None:
            raise GoogleError(400, msg('google.bad_credentials'))
        form = urlencode({'grant_type': 'urn:ietf:params:oauth:grant-type:jwt-bearer', 'assertion': assertion}).encode()
        body = self._mint(form)
        token = body.get('access_token') if isinstance(body, dict) else None
        if not isinstance(token, str) or not token:
            raise GoogleError(502, 'Google sign-in answered without an access token')
        try:
            lasts = float(body.get('expires_in') or TOKEN_SECONDS)
        except (TypeError, ValueError):
            lasts = TOKEN_SECONDS
        self.token = token
        self.expires = now + lasts
        return self.token

    def _mint(self, form: bytes) -> object:
        for attempt in (0, 1):
            try:
                return self.transport('POST', TOKEN_URL, {'Accept': 'application/json', 'Content-Type': 'application/x-www-form-urlencoded'}, form, TIMEOUT).body
            except HttpError as e:
                if e.code in KEY_REFUSED:
                    said = google_error(e.body, 'error')
                    code = f' {said}' if CODE.fullmatch(said) else ''
                    raise GoogleError(e.code, f'Google refused the service account key (HTTP {e.code}{code})', key_refused=True) from None
                if attempt or e.code not in RETRY_CODES:
                    raise GoogleError(e.code, f'Google sign-in HTTP {e.code}') from None
            except Unreachable as e:
                raise GoogleError(502, f'Google sign-in: {kind(e)}', reached=False) from None
            self.pause(RETRY_PAUSE)
        return None

    def call(self, method: str, url: str, body: dict | None, what: str, refused: str, headers: dict[str, str] | None = None, retry: bool = True,
             needs: str = NEEDED) -> dict:
        try:
            return self.once(method, url, body, what, headers)
        except HttpError as e:
            if not retry or method != 'GET' or e.code not in RETRY_CODES:
                raise self.failure(e, what, refused, needs) from None
        self.pause(RETRY_PAUSE)
        try:
            return self.once(method, url, body, what, headers)
        except HttpError as e:
            raise self.failure(e, what, refused, needs) from None

    def once(self, method: str, url: str, body: dict | None, what: str, headers: dict[str, str] | None) -> dict:
        token = self.access()
        try:
            got = call(self.transport, method, url, {'Authorization': f'Bearer {token}', **(headers or {})}, body, timeout=TIMEOUT).body
        except Unreachable as e:
            raise GoogleError(502, f'{what}: {kind(e)}', reached=False) from None
        return got if isinstance(got, dict) else {}

    def failure(self, e: HttpError, what: str, refused: str, needs: str = NEEDED) -> GoogleError:
        said = self.scrub(google_error(e.body, 'message', None))
        state = google_error(e.body, 'status')
        needs = needs or NEEDED
        if e.code == 401 or state == UNAUTHENTICATED:
            self.drop()
            head = f'{ACCOUNT} is invalid or lacks {needs}'
            reason = f'{head} (HTTP {e.code})'
            return GoogleError(e.code, f'{head} (HTTP {e.code}: {said})' if said else reason, reason, needs=needs, invalid=True)
        if e.code == 403 or state == DENIED:
            reason = f'{refused} (HTTP {e.code})'
            return GoogleError(e.code, f'{refused} (HTTP {e.code}: {said})' if said else reason, reason, needs=needs)
        if e.code == 429:
            return GoogleError(429, f'the {what} quota is used up for now (HTTP 429)')
        reason = f'{what} HTTP {e.code}'
        return GoogleError(e.code, f'{reason}: {said.rstrip(".")}' if said else reason, reason)
