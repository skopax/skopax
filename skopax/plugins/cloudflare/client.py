import json
import re
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from urllib.parse import urlencode

from skopax.contract import HttpError, InstanceInfo, Transport, Unreachable, call

API = 'https://api.cloudflare.com/client/v4'
GQL = f'{API}/graphql'
VERIFY = f'{API}/user/tokens/verify'
STATUS = 'https://www.cloudflarestatus.com/api/v2/summary.json'
STATUS_PAGE = 'https://www.cloudflarestatus.com/'
DASH = 'https://dash.cloudflare.com'
PER_PAGE = 50
MAX_PAGES = 20
CALL_TIMEOUT_S = 30
GQL_TIMEOUT_S = 45
TEST_CALL_S = 7
BUDGET_S = 200
TEST_BUDGET_S = 12
TOOL_BUDGET_S = 100
DETAIL_LIMIT = 300
NOT_AVAILABLE = (400, 404)
DENIED = (401, 403)
PERMISSION_CODES = frozenset({9103, 9106, 9107, 9109, 10000})
INVALID_CODES = frozenset({9103, 9106, 9107, 10000})
AUTHZ = re.compile(r'\bnot authori[sz]ed\b|\bunauthori[sz]ed\b', re.I)
PERMISSIONS = {'zones': 'Zone Read', 'health checks': 'Health Checks Read', 'certificates': 'SSL and Certificates Read', 'analytics': 'Analytics Read'}
SLUGS = {'zones': 'zones', 'health checks': 'health-checks', 'certificates': 'certificates', 'analytics': 'analytics'}
ZONE_NAME = re.compile(r'(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z0-9-]{2,63}')
TAG = re.compile(r'[0-9a-f]{32}')
WORD = re.compile(r'[a-z_]{1,20}')


class CloudflareError(Exception):
    def __init__(self, reason: str, detail: str = '', code: int | None = None, answered: bool = True, refused: bool = False, denied: bool = False,
                 invalid: bool = False):
        super().__init__(reason)
        self.reason = reason
        self.detail = detail
        self.code = code
        self.answered = answered
        self.refused = refused
        self.denied = denied
        self.invalid = invalid

    def again(self) -> 'CloudflareError':
        return CloudflareError(self.reason, self.detail, self.code, self.answered, self.refused, self.denied, self.invalid)

    def unavailable(self) -> bool:
        return not self.denied and (self.code in NOT_AVAILABLE or (self.code is None and self.refused))

    def answer(self) -> bool:
        return self.code is None and self.answered

    def shown(self, token: str) -> str:
        detail = (self.detail.replace(token, '***') if token else self.detail)[:DETAIL_LIMIT]
        return f'{self.reason}: {detail}' if detail else self.reason


@dataclass(frozen=True)
class Zone:
    name: str
    id: str
    account: str


def errors_of(body: object) -> list[dict]:
    if isinstance(body, str):
        try:
            body = json.loads(body)
        except ValueError:
            return []
    errors = body.get('errors') if isinstance(body, dict) else None
    return [e for e in errors if isinstance(e, dict)] if isinstance(errors, list) else []


def messages(body: object) -> str:
    return '; '.join(str(e.get('message')) for e in errors_of(body)[:2])


def permission_refused(body: object, codes: frozenset = PERMISSION_CODES) -> bool:
    return any(e.get('code') in codes for e in errors_of(body))


def authz(body: object) -> bool:
    for e in errors_of(body):
        extensions = e.get('extensions') if isinstance(e.get('extensions'), dict) else {}
        if str(extensions.get('code') or '').lower() in ('authz', 'unauthorized', 'forbidden') or AUTHZ.search(str(e.get('message') or '')):
            return True
    return False


def lacks(part: str, invalid: bool = False) -> str:
    return f"the token {'is invalid or lacks' if invalid else 'lacks'} {PERMISSIONS[part]}"


def zone_names(text: str | None) -> list[str]:
    names = [z.strip().lower().rstrip('.') for z in (text or '').split(',')]
    return list(dict.fromkeys(z for z in names if z))


def account_id(text: str | None) -> str:
    value = (text or '').strip().lower()
    return value if TAG.fullmatch(value) else ''


class Cloudflare:
    def __init__(self, token: str, transport: Transport, budget: float | None = None, clock: Callable[[], float] = time.monotonic, call_limit: float | None = None):
        self.token = token
        self.transport = transport
        self.clock = clock
        self.budget = budget
        self.call_limit = call_limit
        self.deadline = None if budget is None else clock() + budget
        self._zones: dict[str, Zone | None] = {}
        self._failed: CloudflareError | None = None
        self._guard = threading.Lock()

    def _timeout(self, limit: float, what: str) -> float:
        if self.call_limit is not None:
            limit = min(limit, self.call_limit)
        if self.deadline is None:
            return limit
        left = self.deadline - self.clock()
        if left <= 0:
            raise CloudflareError(f'{what} not read: the {self.budget:g} s budget is used up', answered=False) from TimeoutError()
        return min(limit, left)

    def _call(self, method: str, url: str, what: str, auth: bool = True, body: object = None, limit: float = CALL_TIMEOUT_S) -> object:
        timeout = self._timeout(limit, what)
        headers = {'Authorization': f'Bearer {self.token}'} if auth and self.token else {}
        try:
            return call(self.transport, method, url, headers, body, timeout=timeout).body
        except HttpError as e:
            raise CloudflareError(f'{what} HTTP {e.code}', e.body or '', e.code, denied=e.code in DENIED or permission_refused(e.body or ''),
                                  invalid=e.code == 401 or permission_refused(e.body or '', INVALID_CODES)) from None
        except Unreachable:
            raise CloudflareError(f'{what} unreachable', answered=False) from None

    def page_of(self, path: str, page: int, what: str, params: Mapping[str, str] | None = None) -> tuple[list[dict], int]:
        body = self._call('GET', f'{API}{path}?' + urlencode({**(params or {}), 'page': page, 'per_page': PER_PAGE}), what)
        if not isinstance(body, dict) or not body.get('success', False):
            raise CloudflareError(f'{what} answered an error', messages(body), refused=True, denied=permission_refused(body), invalid=permission_refused(body, INVALID_CODES))
        result = body.get('result')
        info = body.get('result_info') if isinstance(body.get('result_info'), dict) else {}
        total = info.get('total_pages')
        return [r for r in result if isinstance(r, dict)] if isinstance(result, list) else [], total if isinstance(total, int) and total > 0 else 1

    def listing(self, path: str, what: str | None = None, params: Mapping[str, str] | None = None, pages: int = MAX_PAGES) -> list[dict]:
        what = what or f"Cloudflare API {path.rsplit('/', 1)[-1]}"
        out: list[dict] = []
        for page in range(1, pages + 1):
            found, total = self.page_of(path, page, what, params)
            out += found
            if page >= total:
                break
        return out

    def verify(self, account: str = '') -> str:
        body = self._call('GET', f'{API}/accounts/{account}/tokens/verify' if account else VERIFY, 'Cloudflare token check')
        result = body.get('result') if isinstance(body, dict) else None
        status = result.get('status') if isinstance(result, dict) else None
        return status if isinstance(status, str) and WORD.fullmatch(status) else 'unknown'

    def _lookup(self, name: str) -> Zone | None:
        for z in self.listing('/zones', 'Cloudflare API zones', {'name': name}):
            tag = z.get('id')
            if str(z.get('name') or '').lower() != name or not isinstance(tag, str) or not TAG.fullmatch(tag):
                continue
            owner = z.get('account') if isinstance(z.get('account'), dict) else {}
            return Zone(name, tag, account_id(owner.get('id') if isinstance(owner.get('id'), str) else ''))
        return None

    def zones(self, names: list[str]) -> tuple[list[Zone], list[str]]:
        with self._guard:
            if self._failed is not None:
                raise self._failed.again()
            for name in names:
                if name in self._zones:
                    continue
                if not ZONE_NAME.fullmatch(name):
                    self._zones[name] = None
                    continue
                try:
                    self._zones[name] = self._lookup(name)
                except CloudflareError as e:
                    self._failed = e.again()
                    raise
            found = [self._zones[n] for n in names if self._zones[n] is not None]
            return found, [n for n in names if self._zones[n] is None]

    def status(self) -> dict:
        body = self._call('GET', STATUS, 'Cloudflare status page', auth=False)
        return body if isinstance(body, dict) else {}

    def raw_graphql(self, query: str, variables: dict) -> dict:
        out = self._call('POST', GQL, 'Cloudflare GraphQL', body={'query': query, 'variables': variables}, limit=GQL_TIMEOUT_S)
        return out if isinstance(out, dict) else {}

    def graphql(self, query: str, **variables) -> list[dict]:
        out = self.raw_graphql(query, variables)
        if out.get('errors'):
            raise CloudflareError('Cloudflare GraphQL answered an error', messages(out), denied=authz(out))
        data = out.get('data') if isinstance(out.get('data'), dict) else {}
        viewer = data.get('viewer') if isinstance(data.get('viewer'), dict) else {}
        zones = viewer.get('zones')
        return [z for z in zones if isinstance(z, dict)] if isinstance(zones, list) else []


def client_for(info: InstanceInfo, transport: Transport, budget: float | None = None, call_limit: float | None = None) -> Cloudflare:
    return Cloudflare(info.config.get('token') or '', transport, budget, call_limit=call_limit)


def traffic(account: str, zone: str) -> str | None:
    return f'{DASH}/{account}/{zone}/analytics/traffic' if account and ZONE_NAME.fullmatch(zone) else None
