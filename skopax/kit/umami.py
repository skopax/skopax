import hashlib
import math
import re
import threading
import time
from collections.abc import Callable, Mapping
from urllib.parse import quote, urlencode, urlsplit

from skopax.contract import HttpError, InstanceInfo, ReadError, Transport, Unreachable, call
from skopax.redact import redact

WEBSITE = re.compile(r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}')
EVENT = re.compile(r'[\w.:-]{1,50}')
HOST = re.compile(r'(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z0-9-]{2,63}')
SERVER_API = '/api'
LOGIN = '/api/auth/login'
HEARTBEAT = '/api/heartbeat'
KEY_HEADER = 'x-umami-api-key'
STATS = ('pageviews', 'visitors', 'visits', 'bounces', 'totaltime')
TIMEOUT = 20
TEXT_LIMIT = 200
PATH_LIMIT = 200
DAY = 86400
CLOUD = 'cloud'
TOKEN = 'token'
SERVER = 'server'
CREDENTIALS = {CLOUD: 'API key', TOKEN: 'token', SERVER: 'account'}
LOGINS_MAX = 64
REFUSED = (401, 403)
PERMISSION = 'view access to the website'
WHO = 'its user must own the website, be an admin, or be in the team that holds it'
NO_CREDENTIALS = 'set the API key (Umami Cloud), the token (your own Umami server), or the username and password (your own Umami server)'
BOTH_KEYS = 'set either the API key (Umami Cloud) or the token (your own Umami server), not both'
NO_PASSWORD = 'the password is missing: the login form needs the username and the password'
NO_USERNAME = 'the username is missing: the login form needs the username and the password'
NO_TOKEN = 'Umami answered the login without a token'
BAD_WEBSITE = 'website-id must be the UUID of the website in Umami'
BAD_EVENT = 'the event field must be an event name: letters, digits, _ . : or -, at most 50 characters'
BAD_PROPERTIES = 'the properties field must list event property names, comma-separated: letters, digits, _ . : or -, at most 50 characters each'
NO_DOMAIN = 'Umami gave no usable domain for this website; set the site field'
NOT_FOUND = 'website not found: Umami has no website with this website-id (check the id on its settings page)'
BAD_WINDOW = 'days and rows must be whole numbers of 1 or more'
WHERE = {'': 'website'}

LOGINS: dict[tuple[str, str, str], str] = {}
LOGINS_GUARD = threading.Lock()


def shape(what: str) -> str:
    return f'Umami answered {what} in an unknown shape'


BAD_SHAPE = shape('stats')


class UmamiError(ReadError):
    def __init__(self, reason: str, status: int = 502, refused: int | None = None, both: bool = False):
        super().__init__(status, reason)
        self.reason = reason
        self.refused = refused
        self.both = both


class LoginFailed(HttpError):
    pass


def standing(code: int) -> str:
    return 'is invalid or lacks' if code == 401 else 'lacks'


def refusal(code: int, credential: str, both: bool = False) -> str:
    return f'Umami refused the website (HTTP {code}): the {credential} {standing(401 if both else code)} {PERMISSION}; {WHO}'


def _given(config: Mapping[str, str]) -> tuple[bool, bool, bool, bool]:
    return (bool((config.get('api-key') or '').strip()), bool((config.get('token') or '').strip()), bool((config.get('username') or '').strip()),
            bool(config.get('password') or ''))


def form_of(config: Mapping[str, str]) -> str:
    key, token, user, password = _given(config)
    if key and token:
        return ''
    if key:
        return CLOUD
    if token:
        return TOKEN
    return SERVER if user and password else ''


def problem_of(config: Mapping[str, str]) -> str:
    key, token, user, password = _given(config)
    if key and token:
        return BOTH_KEYS
    if key or token or (user and password):
        return ''
    if user:
        return NO_PASSWORD
    return NO_USERNAME if password else NO_CREDENTIALS


class Umami:
    def __init__(self, url: str, website_id: str, transport: Transport, api_key: str = '', username: str = '', password: str = '', budget: float | None = None,
                 clock: Callable[[], float] = time.monotonic, raw: bool = False, token: str = ''):
        self.url = (url or '').strip().rstrip('/')
        self.website_id = (website_id or '').strip().lower()
        self.transport = transport
        self.api_key = (api_key or '').strip()
        self.static = (token or '').strip()
        self.username = (username or '').strip()
        self.password = password or ''
        given = {'api-key': self.api_key, 'token': self.static, 'username': self.username, 'password': self.password}
        self.form = form_of(given)
        self.problem = problem_of(given)
        self.budget = budget
        self.clock = clock
        self.deadline = None if budget is None else clock() + budget
        self.raw = raw
        self.reads = 0
        self.token = ''

    @classmethod
    def of(cls, config: Mapping[str, str], transport: Transport, budget: float | None = None, raw: bool = False) -> 'Umami':
        return cls(config.get('url') or '', config.get('website-id') or '', transport, config.get('api-key') or '', config.get('username') or '', config.get('password') or '',
                   budget, raw=raw, token=config.get('token') or '')

    @property
    def credential(self) -> str:
        return CREDENTIALS.get(self.form, 'credential')

    def check_site(self) -> None:
        if not WEBSITE.fullmatch(self.website_id):
            raise UmamiError(BAD_WEBSITE, 400)
        if self.problem:
            raise UmamiError(self.problem, 400)

    def root(self) -> str:
        return self.url if self.form == CLOUD else self.url + SERVER_API

    def address(self, what: str = '', params: dict | None = None) -> str:
        path = f'{self.root()}/websites/{quote(self.website_id, safe="")}' + (f'/{what}' if what else '')
        return path + (f'?{urlencode(params)}' if params else '')

    def _timeout(self) -> float:
        if self.deadline is None:
            return TIMEOUT
        left = self.deadline - self.clock()
        if left <= 0:
            raise UmamiError(f'Umami reads stopped after {self.reads} calls: the {self.budget:g} s budget is spent')
        return min(TIMEOUT, left)

    def _call(self, method: str, url: str, headers: dict[str, str], body: dict | None = None) -> object:
        timeout = self._timeout()
        self.reads += 1
        return call(self.transport, method, url, headers, body, timeout=timeout).body

    def _sent(self, method: str, url: str, named: str, headers: dict[str, str], body: dict | None = None, renew: bool = False, both: bool = False) -> object:
        try:
            try:
                return self._call(method, url, headers, body)
            except HttpError as e:
                if not renew or e.code != 401:
                    raise
                self.forget()
                return self._call(method, url, self.headers(), body)
        except HttpError as e:
            if self.raw:
                raise
            if e.code in REFUSED:
                raise UmamiError(refusal(e.code, self.credential, both), 403, e.code, both) from None
            raise UmamiError(f'Umami HTTP {e.code} on {named}') from None
        except Unreachable:
            if self.raw:
                raise
            raise UmamiError(f'Umami unreachable on {named}') from None

    def _login_key(self) -> tuple[str, str, str]:
        return self.url, self.username, hashlib.sha256(self.password.encode()).hexdigest()

    def login(self) -> str:
        if self.token:
            return self.token
        key = self._login_key()
        with LOGINS_GUARD:
            kept = LOGINS.get(key, '')
        if kept:
            self.token = kept
            return kept
        try:
            body = self._sent('POST', self.url + LOGIN, 'login', {}, {'username': self.username, 'password': self.password}, both=True)
        except HttpError as e:
            raise LoginFailed(e.code, 'login') from None
        token = body.get('token') if isinstance(body, dict) else None
        if not isinstance(token, str) or not token.strip():
            raise UmamiError(NO_TOKEN)
        self.token = token.strip()
        with LOGINS_GUARD:
            LOGINS.pop(key, None)
            LOGINS[key] = self.token
            while len(LOGINS) > LOGINS_MAX:
                LOGINS.pop(next(iter(LOGINS)))
        return self.token

    def forget(self) -> None:
        key = self._login_key()
        with LOGINS_GUARD:
            if LOGINS.get(key) == self.token:
                LOGINS.pop(key, None)
        self.token = ''

    def headers(self) -> dict[str, str]:
        if self.form == CLOUD:
            return {KEY_HEADER: self.api_key}
        if self.form == TOKEN:
            return {'Authorization': f'Bearer {self.static}'}
        return {'Authorization': f'Bearer {self.login()}'}

    def get(self, what: str, params: dict | None = None) -> object:
        self.check_site()
        return self._sent('GET', self.address(what, params), WHERE.get(what, what), self.headers(), renew=self.form == SERVER)

    def website(self) -> dict:
        body = self.get('')
        if body is None:
            raise UmamiError(NOT_FOUND, 404)
        return body if isinstance(body, dict) else {}

    def domain(self) -> str:
        found = domain_of(self.website())
        if not found:
            raise UmamiError(NO_DOMAIN)
        return found


def domain_of(body: object) -> str:
    raw = body.get('domain') if isinstance(body, dict) else None
    text = raw.strip().lower() if isinstance(raw, str) else ''
    if '://' in text:
        text = urlsplit(text).hostname or ''
    return text if HOST.fullmatch(text) else ''


def span(start: float, end: float) -> dict:
    return {'startAt': int(start * 1000), 'endAt': int(end * 1000)}


def count_of(value: object, what: str) -> int:
    got = value.get('value') if isinstance(value, dict) else value
    if got is None or got == '':
        return 0
    if isinstance(got, str) and got.strip().isdigit():
        return int(got.strip())
    if isinstance(got, (int, float)) and not isinstance(got, bool) and math.isfinite(got):
        return int(got)
    raise UmamiError(shape(what))


def numbers(body: object) -> dict[str, int]:
    if not isinstance(body, dict):
        raise UmamiError(BAD_SHAPE)
    return {k: count_of(body.get(k), 'stats') for k in STATS}


def pairs(body: object, key: str, count: str, limit: int, what: str) -> list[tuple[str, int]]:
    rows = body if isinstance(body, list) else []
    return [(redact(str(r.get(key) if r.get(key) is not None else '(none)'))[:TEXT_LIMIT], count_of(r.get(count), what)) for r in rows if isinstance(r, dict)][:limit]


def names(raw: str, problem: str) -> tuple[str, ...]:
    listed = tuple(dict.fromkeys(p.strip() for p in (raw or '').split(',') if p.strip()))
    if any(not EVENT.fullmatch(p) for p in listed):
        raise UmamiError(problem, 400)
    return listed


def event_of(config: Mapping[str, str]) -> str:
    event = (config.get('event') or '').strip()
    if event and not EVENT.fullmatch(event):
        raise UmamiError(BAD_EVENT, 400)
    return event


def properties_of(config: Mapping[str, str]) -> tuple[str, ...]:
    return names(config.get('properties') or '', BAD_PROPERTIES)


def path_ok(path: object) -> bool:
    if not isinstance(path, str) or not path.startswith('/') or path.startswith('//') or '\\' in path or len(path) > PATH_LIMIT:
        return False
    if any(not c.isprintable() or c.isspace() for c in path):
        return False
    parts = urlsplit(path)
    return not parts.scheme and not parts.netloc


def top_paths(info: InstanceInfo, transport: Transport, days: int, rows: int) -> list[str]:
    if any(not isinstance(n, int) or isinstance(n, bool) or n < 1 for n in (days, rows)):
        raise UmamiError(BAD_WINDOW, 400)
    um = Umami.of(info.config, transport)
    now = time.time()
    body = um.get('metrics', span(now - days * DAY, now) | {'type': 'path', 'limit': rows})
    out: list[str] = []
    for row in body if isinstance(body, list) else []:
        path = row.get('x') if isinstance(row, dict) else None
        if path_ok(path) and path not in out:
            out.append(path)
    return out
