import math
import re
import threading
import time
import urllib.parse
from collections.abc import Mapping

from skopax.contract import HttpError, InstanceInfo, ReadError, Transport, Unreachable, call
from skopax.kit.promql import with_headers

METRICS_TYPES = ('prometheus',)
QAN_TYPES = ('grafana-clickhouse-datasource',)
KINDS = {'metrics': ('metrics-datasource', METRICS_TYPES, 'metrics'), 'qan': ('qan-datasource', QAN_TYPES, 'QAN')}
DETAIL_LIMIT = 300
CALL_TIMEOUT_S = 30
VERSION_PATHS = ('/v1/version', '/v1/server/version')
REFUSED = (401, 403)
DENIED_TEXT = re.compile(r'not enough privileges|access denied|permission denied|forbidden|\bunauthori[sz]ed\b|not authori[sz]ed', re.I)
PERMISSIONS = {'data sources': 'the Admin role (or datasources:read) to list data sources', 'metrics': 'the Viewer role (query on the metrics data source)',
               'QAN': 'the Viewer role (query on the QAN data source)'}
SLUGS = {'data sources': 'data-sources', 'metrics': 'metrics', 'QAN': 'qan'}
COLUMNS: dict[tuple[str, str], tuple[float, frozenset[str]]] = {}
COLUMNS_TTL_S = 3600
COLUMNS_SQL = "SELECT name FROM system.columns WHERE database = 'pmm' AND table = 'metrics'"
MISSING_COLUMNS = re.compile(r"Missing columns?: ((?:'[A-Za-z_][A-Za-z0-9_.]*'[\s,]*)+)|Unknown (?:expression (?:or function )?)?identifier:? [`']?([A-Za-z_][A-Za-z0-9_.]*)")
HOST_HEADER = re.compile(r'[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?(?::\d{1,5})?')


def host_header(config: Mapping[str, str]) -> str:
    value = str(config.get('host-header') or '').strip()
    return value if HOST_HEADER.fullmatch(value) else ''


def lacks(part: str, invalid: bool = False) -> str:
    return f"the token {'is invalid or lacks' if invalid else 'lacks'} {PERMISSIONS[part]}"


class PmmError(ReadError):
    def __init__(self, reason: str, detail: str = '', code: int | None = None, answered: bool = True, part: str = '', denied: bool = False,
                 cause: Exception | None = None):
        super().__init__(code or 502, reason)
        self.reason = reason
        self.detail = detail
        self.code = code
        self.answered = answered
        self.part = part
        self.denied = denied
        self.cause = cause

    def again(self) -> 'PmmError':
        return PmmError(self.reason, self.detail, self.code, self.answered, self.part, self.denied, self.cause)

    def refused(self) -> bool:
        return self.denied and self.part in PERMISSIONS

    def shown(self, token: str) -> str:
        detail = (self.detail.replace(token, '***') if token else self.detail)[:DETAIL_LIMIT]
        return f'{self.reason}: {detail}' if detail else self.reason


def listed(sources: object) -> list[dict]:
    return [s for s in sources if isinstance(s, dict) and isinstance(s.get('uid'), str) and s['uid']] if isinstance(sources, list) else []


def pick(sources: list[dict], pinned: str, types: tuple[str, ...]) -> dict | None:
    if pinned:
        return next((s for s in sources if s['uid'] == pinned), None)
    return next((s for s in sources if s.get('type') in types), None)


class Pmm:
    def __init__(self, url: str, token: str, transport: Transport, metrics_uid: str = '', qan_uid: str = '', budget: float | None = None, clock=time.monotonic, host: str = ''):
        self.url = url.rstrip('/')
        self.token = token
        extra = ({'Authorization': f'Bearer {token}'} if token else {}) | ({'Host': host} if host else {})
        self.transport = with_headers(transport, extra) if extra else transport
        self._pinned = {'metrics': metrics_uid, 'qan': qan_uid}
        self._uids: dict[str, str] = {}
        self._failed: dict[str, PmmError] = {}
        self._guard = threading.Lock()
        self.budget = budget
        self.clock = clock
        self.deadline = None if budget is None else clock() + budget
        self.reached: set[str] = set()
        self._seen = threading.Lock()

    def pinned(self, kind: str) -> bool:
        return bool(self._pinned[kind])

    def _timeout(self, what: str, part: str) -> float:
        if self.deadline is None:
            return CALL_TIMEOUT_S
        left = self.deadline - self.clock()
        if left <= 0:
            raise PmmError(f'PMM {what} not read: the {self.budget:g} s budget is spent', answered=False, part=part)
        return min(CALL_TIMEOUT_S, left)

    def call(self, what: str, path: str, params: dict | None = None, body: dict | None = None, part: str = '') -> object:
        url = self.url + path + ('?' + urllib.parse.urlencode(params) if params else '')
        try:
            got = call(self.transport, 'POST' if body is not None else 'GET', url, body=body, timeout=self._timeout(what, part)).body
        except HttpError as e:
            raise PmmError(f'PMM {what} HTTP {e.code}', e.body or '', e.code, part=part, denied=e.code in REFUSED, cause=e) from None
        except Unreachable as e:
            raise PmmError(f'PMM {what} unreachable', answered=False, part=part, cause=e) from None
        if part:
            with self._seen:
                self.reached.add(part)
        return got

    def version(self) -> str | None:
        for path in VERSION_PATHS:
            try:
                body = self.call('version', path)
            except PmmError as e:
                if e.code == 404:
                    continue
                raise
            server = body.get('server') if isinstance(body, dict) else None
            found = body.get('version') if isinstance(body, dict) else None
            found = found or (server.get('version') if isinstance(server, dict) else None)
            return found if isinstance(found, str) and found else None
        return None

    def datasources(self) -> list[dict]:
        return listed(self.call('data source list', '/graph/api/datasources', part='data sources'))

    def use(self, kind: str, uid: str) -> None:
        with self._guard:
            self._uids[kind] = uid

    def uid(self, kind: str) -> str:
        with self._guard:
            if kind in self._uids:
                return self._uids[kind]
            if kind in self._failed:
                raise self._failed[kind].again()
            pinned = self._pinned[kind]
            if not pinned:
                try:
                    found = pick(self.datasources(), '', KINDS[kind][1])
                except PmmError as e:
                    self._failed[kind] = e.again()
                    raise
                if found is None:
                    self._failed[kind] = PmmError(f'PMM lists no {KINDS[kind][2]} data source the token can read')
                    raise self._failed[kind].again()
                pinned = found['uid']
            self._uids[kind] = pinned
            return pinned

    def _metrics(self, what: str, path: str, params: dict) -> dict:
        body = self.call(what, f"/graph/api/datasources/proxy/uid/{urllib.parse.quote(self.uid('metrics'), safe='')}/api/v1/{path}", params, part='metrics')
        if not isinstance(body, dict) or body.get('status') != 'success' or not isinstance(body.get('data'), dict):
            error = body.get('error') if isinstance(body, dict) else None
            text = error if isinstance(error, str) else ''
            denied = bool(DENIED_TEXT.search(text)) or (isinstance(body, dict) and str(body.get('errorType') or '').lower() in ('forbidden', 'unauthorized'))
            raise PmmError(f'PMM {what} answered an error', text, part='metrics', denied=denied)
        return body['data']

    def vec(self, expr: str, at: float) -> list[tuple[dict, float]]:
        out = []
        for x in self._metrics('metrics query', 'query', {'query': expr, 'time': f'{at:.3f}'}).get('result') or []:
            v = float(x['value'][1])
            if not math.isnan(v) and abs(v) != float('inf'):
                out.append((x['metric'], v))
        return out

    def range_query(self, expr: str, start: float, end: float, step: int) -> list[dict]:
        params = {'query': expr, 'start': f'{start:.3f}', 'end': f'{end:.3f}', 'step': str(step)}
        return self._metrics('metrics range query', 'query_range', params).get('result') or []

    def qan(self, sql: str, hours: float, now: float) -> list[dict]:
        to = int(now * 1000)
        body = {'from': str(to - int(hours * 3600000)), 'to': str(to), 'queries': [{
            'refId': 'A', 'datasource': {'uid': self.uid('qan'), 'type': QAN_TYPES[0]},
            'rawSql': sql, 'format': 1, 'queryType': 'sql', 'editorType': 'sql'}]}
        got = self.call('QAN query', '/graph/api/ds/query', body=body, part='QAN')
        res = ((got.get('results') or {}).get('A') or {}) if isinstance(got, dict) else {}
        if not isinstance(res, dict) or res.get('error'):
            error = res.get('error') if isinstance(res, dict) else None
            text = error if isinstance(error, str) else ''
            raise PmmError('PMM QAN query answered an error', text, part='QAN', denied=bool(DENIED_TEXT.search(text)))
        rows = []
        for f in res.get('frames') or []:
            names = [x.get('name') for x in (f.get('schema') or {}).get('fields') or []]
            cols = (f.get('data') or {}).get('values') or []
            rows += [dict(zip(names, r)) for r in zip(*cols)] if cols else []
        return rows

    def columns(self, now: float, clock=time.monotonic) -> frozenset[str]:
        try:
            key = (self.url, self.uid('qan'))
            held = COLUMNS.get(key)
            if held and clock() - held[0] < COLUMNS_TTL_S:
                return held[1]
            names = frozenset(str(r.get('name')) for r in self.qan(COLUMNS_SQL, 1, now) if r.get('name'))
        except PmmError:
            return frozenset()
        if names:
            COLUMNS[key] = (clock(), names)
        return names


def missing_columns(text: str) -> list[str]:
    found: list[str] = []
    for many, one in MISSING_COLUMNS.findall(text or ''):
        found += re.findall(r"'([A-Za-z_][A-Za-z0-9_.]*)'", many) if many else [one]
    return list(dict.fromkeys(x.rstrip('.').rsplit('.', 1)[-1] for x in found if x.rstrip('.')))


def client_for(info: InstanceInfo, transport: Transport, budget: float | None = None) -> Pmm:
    config: Mapping[str, str] = info.config
    return Pmm(config.get('url') or '', config.get('token') or '', transport, (config.get('metrics-datasource') or '').strip(), (config.get('qan-datasource') or '').strip(), budget, host=host_header(config))
