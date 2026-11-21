import datetime as dt
import difflib
import json
import re
import time
import urllib.parse
from collections.abc import Callable, Mapping

from skopax.contract import HttpError, InstanceInfo, PassResult, ReadError, Sweep, Transport, Unreachable, anomaly, call, send
from skopax.kit import access
from skopax.kit.options import entries
from skopax.remote import TimedOut, TooLarge

SAFE = "()':,!*-._~"
QUERY_TIMEOUT = 120
TEST_TIMEOUT = 15
PASS_BUDGET = 200
REJECTED = (401, 403)
PRIVILEGES = '/_security/user/_has_privileges'
FROM = re.compile(r'^\s*FROM\s+([^|]+)', re.I)
METADATA = re.compile(r'\s+METADATA\s.*$', re.I | re.S)
REASON_LIMIT = 400
CLUSTERS = ('logs', 'apm')
LOG_DEFAULTS = {'service': 'service.name', 'level': 'log.level', 'message': 'message'}
FALLBACK = {'service-field': LOG_DEFAULTS['service'], 'level-field': LOG_DEFAULTS['level'], 'message-field': LOG_DEFAULTS['message'], 'log-index': 'logs-*', 'apm-index': 'traces-apm*'}
LEVELS = ('ERROR', 'WARN')
UNKNOWN_COLUMN = re.compile(r'Unknown column \[([^\]]+)\]')
ERROR_TYPE = re.compile(r'[a-z_]{1,64}')
NS_GROUP = 'ns'
NS_MISSING = 'index-namespace needs a group named ns, such as app-(?P<ns>.+)-logs-.*'
DISCOVER = '/app/discover#/'
KEY = 'API key'
KEY_MIN = 8
COLUMN_MAX = 100
COLUMNS_MAX = 10
KIBANA_TIME = re.compile(r'\?(start|end)\b')
PATTERNS: dict[str, tuple[float, list[str]]] = {}
PATTERNS_TTL = 300
PATTERNS_FAILED_TTL = 60
PATTERNS_TIMEOUT = 10
PATTERNS_SHOWN = 10
PATTERNS_CLOSE = 0.5
DATED = re.compile(r'\d{4}[.\-]\d{2}[.\-]\d{2}|-\d{6}$')


class ElasticError(ReadError):
    def __init__(self, reason: str, detail: str = '', status: int = 0, missing: bool = False, unknown: tuple[str, ...] = (), denied: bool = False, timed_out: bool = False, code: str = '', too_large: bool = False):
        super().__init__(status, reason)
        self.code = code if ERROR_TYPE.fullmatch(code) else ''
        self.timed_out = timed_out
        self.too_large = too_large
        self.reason = reason
        self.detail = detail
        self.missing = missing
        self.unknown = unknown
        self.denied = denied

    @property
    def text(self) -> str:
        return f'{self.reason}: {self.detail}'[:REASON_LIMIT] if self.detail else self.reason


def setting(config: Mapping[str, str], key: str) -> str:
    return str(config.get(key) or '').strip() or FALLBACK.get(key, '')


def log_fields(config: Mapping[str, str]) -> dict[str, str]:
    return {'service': setting(config, 'service-field'), 'level': setting(config, 'level-field'), 'message': setting(config, 'message-field')}


def quoted(name: str) -> str:
    return '`' + name.replace('`', '``') + '`'


def level_variants(levels: tuple[str, ...] = LEVELS) -> tuple[str, ...]:
    return tuple(dict.fromkeys(x for level in levels for x in (level.upper(), level.lower(), level.capitalize())))


def level_filter(field: str, levels: tuple[str, ...] = LEVELS) -> str:
    return f'{quoted(field)} IN (' + ', '.join(f'"{x}"' for x in level_variants(levels)) + ')'


def parse_error(body: str) -> tuple[str, str]:
    try:
        error = json.loads(body).get('error')
    except (ValueError, AttributeError):
        return '', ' '.join(str(body or '').split())
    if isinstance(error, str):
        return '', ' '.join(error.split())
    if not isinstance(error, dict):
        return '', ''
    root = next((r for r in error.get('root_cause') or [] if isinstance(r, dict)), None)
    pick = root if root and root.get('reason') and (not error.get('reason') or root.get('type') != error.get('type')) else error
    kind = str(pick.get('type') or '')
    parts = [kind, ' '.join(str(pick.get('reason') or '').split())]
    return kind, ': '.join(x for x in parts if x)


class Elastic:
    def __init__(self, url: str, api_key: str = '', transport: Transport = send, fields: Mapping[str, str] | None = None):
        self.url = (url or '').rstrip('/')
        self.api_key = api_key or ''
        self.transport = transport
        self.fields = dict(fields or log_fields({}))

    @property
    def enabled(self) -> bool:
        return bool(self.url)

    def headers(self) -> dict[str, str]:
        return {'Authorization': f'ApiKey {self.api_key}'} if self.api_key else {}

    def query(self, esql: str, timeout: float = QUERY_TIMEOUT, params: list[dict] | None = None, limit: int | None = None) -> list[dict]:
        return self.answer(esql, timeout, params, limit)[1]

    def answer(self, esql: str, timeout: float = QUERY_TIMEOUT, params: list[dict] | None = None, limit: int | None = None) -> tuple[list[str], list[dict]]:
        sent = {'query': esql} | ({'params': params} if params else {})
        try:
            body = call(self.transport, 'POST', f'{self.url}/_query', self.headers(), sent, timeout=timeout, limit=limit).body
        except HttpError as e:
            raise self._refused(e.code, e.body) from None
        except Unreachable as e:
            raise ElasticError('ES|QL unreachable', timed_out=isinstance(e, TimedOut), too_large=isinstance(e, TooLarge)) from None
        if not isinstance(body, dict):
            raise ElasticError('ES|QL answered without rows')
        names = [c.get('name') if isinstance(c, dict) else None for c in body.get('columns') or []]
        return [n for n in names if isinstance(n, str)], [dict(zip(names, row)) for row in body.get('values') or [] if isinstance(row, list)]

    def index_patterns(self, timeout: float = PATTERNS_TIMEOUT, clock: Callable[[], float] = time.monotonic) -> list[str]:
        held = PATTERNS.get(self.url)
        if held and clock() - held[0] < (PATTERNS_TTL if held[1] else PATTERNS_FAILED_TTL):
            return held[1]
        try:
            body = call(self.transport, 'GET', f'{self.url}/_resolve/index/*', self.headers(), timeout=timeout).body
        except (HttpError, Unreachable):
            PATTERNS[self.url] = (clock(), [])
            return []
        names: list[str] = []
        for part in ('indices', 'data_streams', 'aliases'):
            for row in (body.get(part) if isinstance(body, dict) else None) or []:
                if isinstance(row, dict) and isinstance(row.get('name'), str):
                    names.append(re.sub(r'\*{2,}', '*', DATED.sub('*', row['name'])))
        found = list(dict.fromkeys(names))
        PATTERNS[self.url] = (clock(), found)
        return found

    def shards(self, index: str, timeout: float = TEST_TIMEOUT) -> int:
        try:
            body = call(self.transport, 'POST', f"{self.url}/{urllib.parse.quote(','.join(indices_of(index)), safe='*,:')}/_search", self.headers(), {'size': 0}, timeout=timeout).body
        except HttpError as e:
            raise self._refused(e.code, e.body, 'Elasticsearch') from None
        except Unreachable:
            raise ElasticError('Elasticsearch unreachable') from None
        found = body.get('_shards') if isinstance(body, dict) else None
        total = found.get('total') if isinstance(found, dict) else None
        return total if isinstance(total, int) and not isinstance(total, bool) else 0

    def unreadable(self, indices: tuple[str, ...], timeout: float = TEST_TIMEOUT) -> list[str]:
        if not indices:
            return []
        try:
            body = call(self.transport, 'POST', f'{self.url}{PRIVILEGES}', self.headers(), {'index': [{'names': list(indices), 'privileges': ['read']}]}, timeout=timeout).body
        except HttpError as e:
            if e.code in REJECTED:
                raise self._refused(e.code, e.body, 'Elasticsearch') from None
            return []
        except Unreachable:
            raise ElasticError('Elasticsearch unreachable') from None
        found = body.get('index') if isinstance(body, dict) else None
        if not isinstance(found, dict):
            return []
        return [i for i in indices if not (isinstance(found.get(i), dict) and found[i].get('read') is True)]

    def _masked(self, text: str) -> str:
        return text.replace(self.api_key, '***') if self.api_key else text

    def _refused(self, code: int, body: str, what: str = 'ES|QL') -> ElasticError:
        kind, reason = parse_error(body)
        missing = kind == 'index_not_found_exception' or (kind == 'verification_exception' and 'Unknown index' in reason)
        unknown = tuple(dict.fromkeys(self._masked(x) if len(self.api_key) >= KEY_MIN else x for x in UNKNOWN_COLUMN.findall(reason))) if kind == 'verification_exception' else ()
        return ElasticError(f'{what} HTTP {code}', self._masked(reason), code, missing, unknown, code in REJECTED or kind == 'security_exception', code=kind)


class Budget:
    def __init__(self, seconds: float = PASS_BUDGET, clock: Callable[[], float] = time.monotonic, spent: str = 'ES|QL pass budget spent'):
        self.clock = clock
        self.end = clock() + seconds
        self.spent = spent

    def left(self, cap: float = QUERY_TIMEOUT) -> float:
        rest = self.end - self.clock()
        if rest < 1:
            raise ElasticError(self.spent)
        return min(cap, rest)


def native(e: ElasticError) -> BaseException:
    cause = e.__context__
    return cause if isinstance(cause, (HttpError, Unreachable)) else e


def refusal(es: object, e: ElasticError, indices: tuple[str, ...], budget: Budget) -> bool:
    if e.denied:
        return True
    if not e.missing:
        return False
    left = budget.left(TEST_TIMEOUT)
    try:
        return bool(es.unreadable(indices, left))
    except ElasticError as again:
        return again.denied and bool(es.api_key)


def closest_patterns(sources: tuple[str, ...], names: list[str], count: int = PATTERNS_SHOWN) -> list[str]:
    scored = sorted(((max(difflib.SequenceMatcher(None, src, name).ratio() for src in sources), name) for name in names), key=lambda x: (-x[0], x[1]))
    return [name for ratio, name in scored if ratio >= PATTERNS_CLOSE][:count]


def indices_of(source: str) -> tuple[str, ...]:
    return tuple(x.strip() for x in METADATA.sub('', source or '').split(',') if x.strip())


def sources_of(query: str) -> tuple[str, ...]:
    found = FROM.match(query or '')
    return indices_of(found.group(1)) if found else ()


def no_read(index: str, who: str) -> str:
    return f'The API key lacks the read privilege on {index}, which the {who}.'


def refused_text(status: int, indices: tuple[str, ...]) -> str:
    if status == 401:
        return 'the API key was not accepted by Elasticsearch (HTTP 401)'
    return f"the API key lacks the read privilege on {', '.join(indices) or 'the queried indices'}"


def noun(names: tuple[str, ...]) -> tuple[str, str]:
    return ('field', 'it') if len(names) == 1 else ('fields', 'them')


def fields_card(check: str, src: str, plugin: str, ns: str, svc: str, names: tuple[str, ...], index: str) -> dict:
    word, them = noun(names)
    listed = ', '.join(names)
    label = f'{plugin} logs lack the {word} {listed}'
    x = anomaly(src, f'{check}-fields', ns, svc, 'fields', 'warn', len(names), None, None, '', label,
                f'{index} has no {word} {listed}, or the API key cannot read {them}, so the {check} check reads nothing there. '
                f'Map the {word} in the log pipeline, let the key read {them}, or point the instance at an index that has {them}. '
                f'The card closes at the first pass that finds {them}.', 1, f'WARN {label}')
    x['ranKey'] = f'{check}:fields'
    return x


def access_card(check: str, plugin: str, part: str, index: str, status: int, keyed: bool = True) -> dict:
    permission = f'the read privilege on {index}'
    fix = (f'Set an API key that can read {index}.' if not keyed else 'Replace the API key with a valid one, or grant it the read privilege on ' + index + '.' if status == 401
           else f'Grant the API key the read privilege on {index}.')
    what = (f'Elasticsearch refused to let the {check} check read {index} (HTTP {status}), so the check cannot tell whether anything is wrong there, '
            f'and its cards stay as they were. {fix} The card closes at the first pass that reads {part} again.')
    return access.card(check, plugin, part, permission, status, KEY if keyed else '', what=what)


def no_access(check: str, card: dict, sections: dict[str, list[dict]] | None = None) -> Sweep:
    return Sweep(PassResult(check, True, f"ok \u00b7 no access to {card['sub']}", [card], {}, (card['ranKey'],)), sections=sections or {})


def refused_status(e: ElasticError) -> int:
    return 401 if e.status == 401 else 403


def cluster_of(config: Mapping[str, str], cluster: str) -> tuple[str, str]:
    url, key = str(config.get('url') or ''), str(config.get('api-key') or '')
    if cluster == 'apm':
        return str(config.get('apm-url') or '') or url, str(config.get('apm-api-key') or '') or key
    return url, key


def index_of(config: Mapping[str, str], cluster: str) -> str:
    if cluster == 'logflow':
        return str(config.get('logflow-index') or '').strip() or setting(config, 'log-index')
    return setting(config, 'apm-index' if cluster == 'apm' else 'log-index')


def client_for(info: InstanceInfo, transport: Transport, cluster: str = 'logs') -> Elastic:
    url, key = cluster_of(info.config, cluster)
    return Elastic(url, key, transport, log_fields(info.config))


def namespace_problem(config: Mapping[str, str]) -> str | None:
    raw = str(config.get('index-namespace') or '').strip()
    if not raw:
        return None
    try:
        pattern = re.compile(raw)
    except (re.error, OverflowError, RecursionError):
        return NS_MISSING
    return None if NS_GROUP in pattern.groupindex else NS_MISSING


def namespace_of(row: Mapping[str, object], config: Mapping[str, str]) -> str | None:
    problem = namespace_problem(config)
    if problem:
        raise ElasticError(problem)
    raw = str(config.get('index-namespace') or '').strip()
    if raw:
        found = re.match(raw, str(row.get('_index') or ''))
        value = found[NS_GROUP] if found else None
    else:
        field = str(config.get('namespace-field') or '').strip()
        value = row.get(field) if field else None
    return value if isinstance(value, str) and value else None


def epoch(value: object) -> float | None:
    try:
        parsed = dt.datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    return parsed.timestamp() if parsed.tzinfo else None


def card_urls(label: str, url: str) -> list[dict]:
    return [{'label': label, 'url': url}] if url.startswith('https://') else []


def carried(card: Mapping[str, object], src: str) -> list[dict] | None:
    urls = card.get('urls')
    if card.get('src') != src or not isinstance(urls, list):
        return None
    out = [{'label': u['label'], 'url': u['url']} for u in urls if isinstance(u, dict) and isinstance(u.get('label'), str) and isinstance(u.get('url'), str) and u['url'].startswith('https://')]
    return out or None


def rison(value: str) -> str:
    return "'" + value.replace('!', '!!').replace("'", "!'") + "'"


def kql_text(value: str) -> str:
    return '"' + str(value).replace('\\', '\\\\').replace('"', '\\"') + '"'


def kql_levels(field: str, levels: tuple[str, ...]) -> str:
    return f'{field}:(' + ' or '.join(kql_text(x) for x in level_variants(levels)) + ')'


def extra_columns(config: Mapping[str, str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(x for x in entries(config.get('discover-columns')) if len(x) <= COLUMN_MAX))[:COLUMNS_MAX]


def columns_of(config: Mapping[str, str]) -> tuple[str, ...]:
    named = log_fields(config)
    error = str(config.get('error-field') or '').strip()
    return tuple(dict.fromkeys(x for x in (named['level'], named['service'], named['message'], error, *extra_columns(config)) if x))


def discover(ui: str, data_view: str, kql: str, since: str, columns: tuple[str, ...], until: str | None = None) -> str:
    if not ui:
        return ''
    to = rison(until) if until else 'now'
    g = f'(filters:!(),refreshInterval:(pause:!t,value:60000),time:(from:{rison(since)},to:{to}))'
    source = f'dataSource:(dataViewId:{rison(data_view)},type:dataView),' if data_view else ''
    a = ('(columns:!(' + ','.join(rison(c) for c in columns) + '),' + source +
         f"filters:!(),interval:auto,query:(language:kuery,query:{rison(kql)}),sort:!(!('@timestamp',desc)))")
    return ui.rstrip('/') + DISCOVER + '?_g=' + urllib.parse.quote(g, safe=SAFE) + '&_a=' + urllib.parse.quote(a, safe=SAFE)


def esql_discover(ui: str, esql: str, minutes: int) -> str:
    if not ui.startswith('https://'):
        return ''
    g = f'(filters:!(),refreshInterval:(pause:!t,value:60000),time:(from:now-{int(minutes)}m,to:now))'
    a = f'(dataSource:(type:esql),query:(esql:{rison(KIBANA_TIME.sub(lambda m: "?_t" + m[1], esql))}))'
    return ui.rstrip('/') + DISCOVER + '?_g=' + urllib.parse.quote(g, safe=SAFE) + '&_a=' + urllib.parse.quote(a, safe=SAFE)


def discover_for(config: Mapping[str, str], kql: str, since: str, until: str | None = None, columns: tuple[str, ...] | None = None) -> str:
    return discover(str(config.get('ui-url') or ''), str(config.get('data-view') or ''), kql, since, columns_of(config) if columns is None else columns, until)


def service_kql(config: Mapping[str, str], svc: str) -> str:
    named = log_fields(config)
    return f"{named['service']}:{kql_text(svc)} and {kql_levels(named['level'], LEVELS)}"


def service_logs_link(config: Mapping[str, str], svc: str, since: str = 'now-2h', until: str | None = None) -> str:
    return discover_for(config, service_kql(config, svc), since, until)
