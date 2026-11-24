import json
import logging
import re
import time
import urllib.parse
from collections.abc import Callable, Mapping

from skopax import severity
from skopax.contract import HttpError, PassResult, Sweep, Transport, Unreachable, anomaly, iso, send
from skopax.kit import access, flags, options
from skopax.kit.promql import auth_headers, credential
from skopax.messages import msg
from skopax.redact import redact_secrets
from skopax.remote import JSON_ACCEPT, PAGE_READ, Reply

log = logging.getLogger('skopax.plugins.loki')

NAME = 'Loki'
PASS = 'loki'
SIG = 'log'
PART = 'logs'
SECTION = 'lokiTop'
NO_DATA = f'{PASS}:no-data'
ACCESS = access.ran_key(PASS, PART)
API = '/loki/api/v1'
TENANT_HEADER = 'X-Scope-OrgID'
TIMEOUT = 20
ANSWER_LIMIT = 8 * 1024 * 1024
PASS_BUDGET_S = 150
WINDOW_S = 3600
DAY_S = 86400
WINDOWS = (('now', 0), ('d1', DAY_S), ('d7', 7 * DAY_S))
MAX_RECOUNT = 10
MAX_SERIES = 200
MAX_SERVICES = 10
MAX_LINES = 200
MAX_PATTERNS = 3
MAX_NEEDLES = 3
NEEDLE_MIN = 4
PATTERN_LIMIT = 90
TOP = 30
REASON_LIMIT = 200
SERVICE_LIMIT = 100
SELECTOR_LIMIT = 1000
ERROR_MIN = 5
WARN_MIN = 30
DEFAULT_SERVICE = 'app'
FALLBACK_SERVICE = 'service_name'
DEFAULT_PATTERN = '(?i)(error|exception|fatal|panic)'
ERROR_LEVELS = '(?i)(error|err|fatal|critical|crit|panic|emerg|alert)'
WARN_LEVELS = '(?i)(warn|warning)'
MIN_LEVELS = ('error', 'warn')
MIN_LABELS = {'error': 'Error', 'warn': 'Warning'}
HELD = '[held: the text reads like instructions to an AI]'
DOT = ' \u00b7 '
REDIRECTS = (301, 302, 303, 307, 308)
NO_TENANT_WORDS = ('no org id', 'no orgid', 'org id', 'x-scope-orgid', 'tenant')
TENANT_BAR = 'Tenant is one tenant id; a | would make Loki read several tenants at once, so it is refused'
TENANT_MISSING = 'Loki needs a tenant: the server answered that no org id was sent. Set Tenant to the tenant id it expects (sent as X-Scope-OrgID).'
IDENT = re.compile(r'[A-Za-z_][A-Za-z0-9_]*\Z')
MATCHER = re.compile(r'\s*([A-Za-z_][A-Za-z0-9_]*)\s*(=~|!~|!=|=)\s*')
TIMESTAMP = re.compile(r'^\s*\[?\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:[.,]\d+)?(?:Z|[+-]\d{2}:?\d{2})?\]?\s*')
STAMP_ANY = re.compile(r'\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:[.,]\d+)?(?:Z|[+-]\d{2}:?\d{2})?')
UUID = re.compile(r'\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b')
HEX = re.compile(r'\b(?:0x)?(?=[0-9a-fA-F]*\d)(?=[0-9a-fA-F]*[a-fA-F])[0-9a-fA-F]{8,}\b')
IP = re.compile(r'\b\d{1,3}(?:\.\d{1,3}){3}(?::\d+)?\b')
LONG_NUMBER = re.compile(r'\d{4,}')
PLACEHOLDER = re.compile(r'<(?:id|ip|n|time)>')
UNSAFE = re.compile(r'<(?:id|ip|n|time)>|\[[^\]]*\]|[^\x20-\x7e]|["\\<>&*`\[\]]')
LOGFMT_MSG = re.compile(r'(?:^|\s)(?:msg|message)=(?:"((?:[^"\\]|\\.)*)"|(\S+))')
JSON_KEYS = ('msg', 'message', 'error', 'err', 'log')


def nanos(at: float) -> str:
    return str(int(round(at * 1e9)))


class LokiError(Exception):
    def __init__(self, reason: str, code: int | None = None, answered: bool = False, tenant: bool = False, spent: bool = False, large: bool = False):
        super().__init__(reason)
        self.large = large
        self.reason = reason
        self.code = code
        self.answered = answered
        self.tenant = tenant
        self.spent = spent


class SelectorError(ValueError):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def flat(text: object) -> str:
    return ' '.join(str(text if text is not None else '').split())


def masked(text: object, limit: int) -> str:
    return redact_secrets(flat(text))[:limit]


def planted(text: str) -> bool:
    return bool(flags.scan(PASS, text))


def clean(text: object, limit: int) -> str:
    shown = masked(text, limit)
    return HELD if planted(shown) else shown


class Budget:
    def __init__(self, seconds: float = PASS_BUDGET_S, clock: Callable[[], float] = time.monotonic):
        self.clock = clock
        self.seconds = seconds
        self.end = clock() + seconds

    def left(self, cap: float) -> float:
        rest = self.end - self.clock()
        if rest < 1:
            raise LokiError(f'Loki pass ran out of its {self.seconds:g} s', spent=True)
        return min(cap, rest)


def quoted_end(text: str, at: int) -> int:
    mark = text[at]
    i = at + 1
    while i < len(text):
        c = text[i]
        if mark == '"' and c == '\\':
            i += 2
            continue
        if c == mark:
            return i + 1
        i += 1
    raise SelectorError('a quoted value is not closed')


def unquote(raw: str) -> str:
    if raw.startswith('`'):
        return raw[1:-1]
    try:
        value = json.loads(raw)
    except ValueError:
        raise SelectorError('a quoted value has an escape LogQL does not read') from None
    return value if isinstance(value, str) else str(value)


def split_selector(query: str) -> tuple[list[tuple[str, str, str]], str]:
    text = str(query or '')
    start = len(text) - len(text.lstrip())
    if start >= len(text) or text[start] != '{':
        raise SelectorError('a LogQL stream selector starts with {')
    i = start + 1
    matchers: list[tuple[str, str, str]] = []
    while True:
        while i < len(text) and text[i].isspace():
            i += 1
        if i < len(text) and text[i] == '}' and not matchers:
            raise SelectorError('a stream selector needs at least one label matcher')
        m = MATCHER.match(text, i)
        if not m:
            raise SelectorError('a stream selector holds label matchers such as namespace="prod" or app=~"web-.*"')
        i = m.end()
        if i >= len(text) or text[i] not in '"`':
            raise SelectorError('a matcher value is a quoted text')
        end = quoted_end(text, i)
        matchers.append((m.group(1), m.group(2), unquote(text[i:end])))
        i = end
        while i < len(text) and text[i].isspace():
            i += 1
        if i < len(text) and text[i] == ',':
            i += 1
            continue
        if i < len(text) and text[i] == '}':
            return matchers, text[i + 1:]
        raise SelectorError('a stream selector ends with }')


def render(matchers: list[tuple[str, str, str]]) -> str:
    return '{' + ', '.join(f'{n}{op}{json.dumps(v)}' for n, op, v in matchers) + '}'


def selector_problem(value: object) -> str | None:
    text = str(value or '').strip()
    if not text:
        return 'Selector is required, for example {namespace=~"prod-.*"}'
    if len(text) > SELECTOR_LIMIT:
        return f'Selector is longer than {SELECTOR_LIMIT} characters'
    try:
        matchers, rest = split_selector(text)
    except SelectorError as e:
        return f'Selector is not a LogQL stream selector: {e.reason}'
    if rest.strip():
        return 'Selector is a stream selector only, such as {namespace="prod"}, without a line filter or pipeline'
    if not any(op in ('=', '=~') and v and not (op == '=~' and re.fullmatch(r'\.\*|\.\+', v)) for _, op, v in matchers):
        return 'Selector needs at least one matcher that narrows the streams, such as namespace="prod"'
    for _, op, v in matchers:
        if op in ('=~', '!~') and options.compiled(v) is None:
            return 'Selector holds a pattern that cannot be read'
    return None


def instance_matchers(config: Mapping[str, str]) -> list[tuple[str, str, str]]:
    problem = selector_problem(config.get('selector'))
    if problem:
        raise LokiError(problem, answered=True)
    return split_selector(str(config.get('selector') or '').strip())[0]


def fits(op: str, wanted: str, value: str) -> bool:
    if op == '=':
        return value == wanted
    if op == '!=':
        return value != wanted
    found = options.compiled(wanted)
    if found is None:
        return False
    hit = found.fullmatch(value) is not None
    return hit if op == '=~' else not hit


def confine(instance: list[tuple[str, str, str]], extra: list[tuple[str, str, str]]) -> list[tuple[str, str, str]]:
    for name, op, value in extra:
        for own_name, own_op, own_value in instance:
            if own_name != name or (own_op, own_value) == (op, value):
                continue
            if op == '=' and not fits(own_op, own_value, value):
                raise SelectorError(f'{name}={json.dumps(value)} is outside the instance selector {render(instance)}; the query stays inside it')
    return instance + [m for m in extra if m not in instance]


def pipeline_problem(rest: str) -> str | None:
    depth = 0
    i = 0
    while i < len(rest):
        c = rest[i]
        if c in '"`':
            try:
                i = quoted_end(rest, i)
            except SelectorError as e:
                return e.reason
            continue
        if c == '#' or ord(c) < 32 and c not in ' \t':
            return 'the query holds no comment (#) and no line break outside quotes'
        if c in '{}[]':
            return 'the query holds one stream selector and a pipeline: no second selector, range or brace outside quotes'
        if c == '(':
            depth += 1
        elif c == ')':
            depth -= 1
            if depth < 0:
                return 'the pipeline has a ) without its ('
        i += 1
    return 'the pipeline has a ( without its )' if depth else None


def name_of(config: Mapping[str, str], key: str, default: str = '') -> str:
    value = str(config.get(key) or '').strip() or default
    return value if IDENT.match(value) else ''


def tenant_problem(value: object) -> str | None:
    return TENANT_BAR if '|' in str(value or '') else None


def option_problem(config: Mapping[str, str]) -> str | None:
    problem = tenant_problem(config.get('tenant'))
    if problem:
        return problem
    for key, label in (('service-label', 'Service label'), ('level-label', 'Level label')):
        value = str(config.get(key) or '').strip()
        if value and not IDENT.match(value):
            return f'{label} is a label name: letters, digits and _, not starting with a digit'
    pattern = str(config.get('error-pattern') or '').strip()
    if pattern:
        found = options.compiled(pattern)
        if found is None:
            return 'Error pattern is not a valid pattern'
        if found.search('') is not None:
            return 'Error pattern matches an empty text, so it would read every line'
    return None


def service_label(config: Mapping[str, str]) -> str:
    return name_of(config, 'service-label', DEFAULT_SERVICE) or DEFAULT_SERVICE


def levels_of(config: Mapping[str, str]) -> tuple[str, ...]:
    warn = str(config.get('min-severity') or 'error').strip().lower() == 'warn' and name_of(config, 'level-label')
    return ('ERROR', 'WARN') if warn else ('ERROR',)


def level_filter(config: Mapping[str, str], level: str) -> str:
    label = name_of(config, 'level-label')
    if label:
        return f' | {label}=~{json.dumps(ERROR_LEVELS if level == "ERROR" else WARN_LEVELS)}'
    pattern = str(config.get('error-pattern') or '').strip()
    return f' |~ {json.dumps(pattern if pattern and not option_problem(config) else DEFAULT_PATTERN)}'


def by_labels(config: Mapping[str, str]) -> list[str]:
    own = service_label(config)
    return [own] + ([FALLBACK_SERVICE] if own != FALLBACK_SERVICE else [])


def service_of(metric: Mapping, labels: list[str]) -> tuple[str, str]:
    for name in labels:
        value = metric.get(name) if isinstance(metric, Mapping) else None
        if isinstance(value, str) and value:
            return value, name
    return '', ''


def service_filter(labels: list[str], source: str, value: str) -> str:
    if not source:
        return ''.join(f' | {n}=""' for n in labels)
    before = labels[:labels.index(source)]
    return ''.join(f' | {n}=""' for n in before) + f' | {source}={json.dumps(value)}'


class Loki:
    def __init__(self, url: str, headers: Mapping[str, str], transport: Transport = send, timeout: float = TIMEOUT, budget: Budget | None = None,
                 secrets: tuple[str, ...] = ()):
        self.url = (url or '').rstrip('/')
        self.headers = dict(headers)
        self.transport = transport
        self.timeout = timeout
        self.budget = budget
        self.secrets = tuple(s for s in secrets if s)

    def _scrub(self, text: object) -> str:
        shown = flat(text.decode('utf-8', 'replace') if isinstance(text, bytes) else text)
        for secret in self.secrets:
            shown = shown.replace(secret, '[redacted]')
        return clean(shown, REASON_LIMIT)

    def request(self, path: str, params: Mapping | list | None = None, what: str = 'Loki') -> Reply:
        query = urllib.parse.urlencode(params or {}, doseq=True)
        url = self.url + path + (f'?{query}' if query else '')
        timeout = self.budget.left(self.timeout) if self.budget else self.timeout
        headers = {'Accept': JSON_ACCEPT, **self.headers, PAGE_READ: str(ANSWER_LIMIT)}
        try:
            reply = self.transport('GET', url, headers, None, timeout)
        except HttpError as e:
            raise self._status(what, e.code, (e.headers or {}).get('location', ''), e.body) from None
        except Unreachable:
            raise LokiError(f'{what}: unreachable') from None
        status = reply.status if isinstance(reply.status, int) else 200
        if status != 200:
            raise self._status(what, status, (reply.headers or {}).get('location', ''), reply.body)
        if isinstance(reply.body, (bytes, str)) and len(reply.body) > ANSWER_LIMIT:
            raise LokiError(f'{what}: the answer is larger than {ANSWER_LIMIT // (1024 * 1024)} MB', answered=True, large=True)
        return reply

    def _status(self, what: str, code: int | None, location: str, body: object) -> LokiError:
        said = self._scrub(body or '')
        if code in REDIRECTS:
            where = f' to {masked(location, REASON_LIMIT).split("?")[0]}' if location else ''
            return LokiError(f'{what}: HTTP {code}, a redirect{where} that is not followed; set URL to the final Loki address', code, answered=True)
        if code == 401 and TENANT_HEADER not in self.headers and any(w in said.lower() for w in NO_TENANT_WORDS):
            return LokiError(TENANT_MISSING, 401, answered=True, tenant=True)
        if access.denied(code):
            return LokiError(f'{what}: HTTP {code}, the credentials were refused', code, answered=True)
        if code == 404:
            return LokiError(f'{what}: HTTP 404, no Loki API at {self.url}{API}; set URL to the Loki base address', code, answered=True)
        tail = f': {said}' if said else ''
        return LokiError(f'{what}: HTTP {code}{tail}', code, answered=True)

    def json(self, path: str, params: Mapping | list | None = None, what: str = 'Loki') -> object:
        raw = self.request(path, params, what).body
        if isinstance(raw, (bytes, str)):
            try:
                raw = json.loads(raw or b'null')
            except ValueError:
                raise LokiError(msg('connection.not_api'), answered=True) from None
        return raw

    def data(self, path: str, params: Mapping | list | None, what: str) -> object:
        body = self.json(API + path, params, what)
        if not isinstance(body, dict) or body.get('status') != 'success' or 'data' not in body:
            raise LokiError(f'{what}: the answer is not a Loki answer', answered=True)
        return body['data']

    def version(self) -> str | None:
        try:
            body = self.json(API + '/status/buildinfo', None, 'Loki buildinfo')
        except LokiError as e:
            if e.code == 404:
                return None
            raise
        if not isinstance(body, dict):
            raise LokiError(msg('connection.not_api'), answered=True)
        found = body.get('version')
        return masked(found, 40) if isinstance(found, str) and found else None

    def ready(self) -> bool | None:
        try:
            self.request('/ready', None, 'Loki ready')
        except LokiError as e:
            if e.spent:
                raise
            return False if e.code == 503 else None
        return True

    def vector(self, query: str, at: float, what: str = 'Loki query') -> list[tuple[dict, float]]:
        data = self.data('/query', {'query': query, 'time': nanos(at)}, what)
        rows = data.get('result') if isinstance(data, dict) else None
        out = []
        for r in (rows if isinstance(rows, list) else [])[:MAX_SERIES]:
            if not isinstance(r, dict) or not isinstance(r.get('value'), list) or len(r['value']) != 2:
                continue
            try:
                value = float(r['value'][1])
            except (TypeError, ValueError):
                continue
            if value == value and abs(value) != float('inf'):
                out.append((r.get('metric') if isinstance(r.get('metric'), dict) else {}, value))
        return out

    def streams(self, query: str, start: float, end: float, limit: int, what: str = 'Loki query_range') -> list[tuple[dict, list[tuple[float, str]]]]:
        data = self.data('/query_range', {'query': query, 'start': nanos(start), 'end': nanos(end), 'limit': str(limit), 'direction': 'backward'}, what)
        rows = data.get('result') if isinstance(data, dict) else None
        out, seen = [], 0
        for r in rows if isinstance(rows, list) else []:
            if seen >= limit:
                break
            if not isinstance(r, dict) or not isinstance(r.get('values'), list):
                continue
            lines = []
            for v in r['values'][:limit - seen]:
                if isinstance(v, list) and len(v) >= 2 and isinstance(v[1], str):
                    try:
                        at = int(str(v[0])) / 1e9
                    except ValueError:
                        continue
                    lines.append((at, v[1]))
            seen += len(lines)
            out.append((r.get('stream') if isinstance(r.get('stream'), dict) else {}, lines))
        return out

    def series(self, selector: str, start: float, end: float, what: str = 'Loki series') -> list[dict]:
        data = self.data('/series', {'match[]': selector, 'start': nanos(start), 'end': nanos(end)}, what)
        return [r for r in data if isinstance(r, dict)] if isinstance(data, list) else []

    def names(self, path: str, selector: str, start: float, end: float, what: str) -> list[str]:
        data = self.data(path, {'query': selector, 'start': nanos(start), 'end': nanos(end)}, what)
        return [x for x in data if isinstance(x, str)] if isinstance(data, list) else []


def headers_of(config: Mapping[str, str]) -> dict[str, str]:
    out = dict(auth_headers(config))
    tenant = flat(config.get('tenant') or '')
    if tenant:
        out[TENANT_HEADER] = tenant
    return out


def client_for(config: Mapping[str, str], transport: Transport, timeout: float = TIMEOUT, budget: Budget | None = None) -> Loki:
    secrets = tuple(str(config.get(k) or '') for k in ('token', 'password'))
    return Loki(str(config.get('url') or ''), headers_of(config), transport, timeout, budget, secrets)


def message_of(line: str) -> str:
    text = line.strip()
    if text.startswith('{'):
        try:
            found = json.loads(text)
        except ValueError:
            found = None
        if isinstance(found, dict):
            for key in JSON_KEYS:
                value = found.get(key)
                if isinstance(value, str) and value.strip():
                    return value
    m = LOGFMT_MSG.search(text)
    if m:
        return (m.group(1) or m.group(2) or '').replace('\\"', '"')
    return text


def normalised(line: str) -> str:
    text = TIMESTAMP.sub('', message_of(line))
    text = STAMP_ANY.sub('<time>', text)
    text = UUID.sub('<id>', text)
    text = HEX.sub('<id>', text)
    text = IP.sub('<ip>', text)
    text = LONG_NUMBER.sub('<n>', text)
    return flat(text)[:PATTERN_LIMIT]


def needles_of(pattern: str, raw: str) -> list[str]:
    shown = redact_secrets(pattern)
    chunks = [c.strip() for c in UNSAFE.split(shown)]
    found = sorted({c for c in chunks if len(c) >= NEEDLE_MIN and c in raw}, key=lambda c: (-len(c), c))
    return found[:MAX_NEEDLES]


def judged(level: str, now: float, d1: float, d7: float) -> str | None:
    base = max(d1, d7)
    if level == 'ERROR':
        if not (now >= ERROR_MIN and now > 2 * base + 3):
            return None
        return severity.CRIT if base == 0 and now >= 50 else severity.HIGH
    flagged = (now >= WARN_MIN and now >= 5 * max(base, 1)) or severity.warn_log(now, d1, d7) == severity.HIGH
    if not flagged:
        return None
    return severity.warn_log(now, d1, d7) if now >= 50 else severity.INFO


def metric_query(selector: str, pipeline: str, labels: list[str]) -> str:
    return f'topk({MAX_SERIES}, sum by ({", ".join(labels)}) (count_over_time({selector}{pipeline} [1h])))'


def count_query(selector: str, pipeline: str) -> str:
    return f'sum(count_over_time({selector}{pipeline} [1h]))'


def filters_of(needles: list[str]) -> str:
    return ''.join(f' |= {json.dumps(n)}' for n in needles)


def card_of(svc: str, level: str, pattern: str, now: int, d1: int, d7: int, sev: str) -> dict:
    kind = 'new' if max(d1, d7) == 0 else 'growing'
    x = anomaly(PASS, SIG, '-', svc, '', sev, now, d1, d7, '', f'{level} pattern',
                f'{kind} {level} pattern in Loki, {now} in the last 60 min against {d1} a day ago and {d7} a week ago: {pattern}', 5)
    x['key'] = f'{SIG}|{svc}|{level}|{pattern}'
    x['msg'] = pattern
    x['ranKey'] = PASS
    x['short'] = f'{sev.upper()} {svc} {kind} {level} \u00d7{now}: {pattern[:50]}'
    return x


def counted(client: Loki, query: str, now: float) -> dict[str, int]:
    out = {}
    for w, back in WINDOWS:
        out[w] = int(round(sum(v for _, v in client.vector(query, now - back, 'Loki pattern count'))))
    return out


def sampled(client: Loki, selector: str, pipeline: str, now: float) -> list[tuple[str, int, str]]:
    groups: dict[str, list] = {}
    for _, lines in client.streams(selector + pipeline, now - WINDOW_S, now, MAX_LINES, 'Loki sample'):
        for _, line in lines:
            pattern = normalised(line)
            if pattern:
                group = groups.setdefault(pattern, [0, line])
                group[0] += 1
    ranked = sorted(groups.items(), key=lambda g: (-g[1][0], g[0]))
    return [(p, n, raw) for p, (n, raw) in ranked[:MAX_PATTERNS]]


def served(svc_raw: str) -> str:
    svc = clean(svc_raw, SERVICE_LIMIT) if svc_raw else '-'
    return '-' if svc == HELD else svc


def judge_row(svc: str, level: str, shown: str, exact: dict[str, int], row: dict) -> dict | None:
    sev = judged(level, exact['now'], exact['d1'], exact['d7'])
    if not sev:
        return None
    row['flag'] = True
    return card_of(svc, level, shown, exact['now'], exact['d1'], exact['d7'], sev)


def service_pass(client: Loki, config: Mapping[str, str], selector: str, labels: list[str], target: tuple[str, str, str], total: int, now: float) -> tuple[list[dict], list[dict]]:
    svc_raw, source, level = target
    svc = served(svc_raw)
    pipeline = level_filter(config, level) + service_filter(labels, source, svc_raw)
    found, rows = [], []
    patterns = sampled(client, selector, pipeline, now)
    lines = sum(n for _, n, _ in patterns) or 1
    for pattern, seen, raw in patterns:
        shown = clean(pattern, PATTERN_LIMIT)
        needles = needles_of(pattern, raw)
        if not needles:
            rows.append({'svc': svc, 'level': level, 'msg': shown, 'now': seen if total <= lines else int(round(seen * total / lines)), 'estimated': True})
            continue
        exact = counted(client, count_query(selector, pipeline + filters_of(needles)), now)
        row = {'svc': svc, 'level': level, 'msg': shown, **exact}
        if (svc == svc_raw or not svc_raw) and shown != HELD and not planted(' '.join(needles)):
            row.update(source=source, needles=needles)
        card = judge_row(svc, level, shown, exact, row)
        if card:
            found.append(card)
        rows.append(row)
    return found, rows


def recount(client: Loki, config: Mapping[str, str], selector: str, labels: list[str], previous: list | None, seen: set[tuple[str, str, str]], now: float) -> tuple[list[dict], list[dict]]:
    found, rows = [], []
    for row in [r for r in previous or [] if isinstance(r, dict) and r.get('flag')][:MAX_RECOUNT]:
        svc, level, shown, source, needles = row.get('svc'), row.get('level'), row.get('msg'), row.get('source'), row.get('needles')
        if (svc, level, shown) in seen or level not in levels_of(config) or not isinstance(needles, list) or not needles or not isinstance(svc, str) or not isinstance(shown, str):
            continue
        if not all(isinstance(n, str) and n and len(n) <= PATTERN_LIMIT for n in needles[:MAX_NEEDLES]) or (source and source not in labels) or (not source and svc != '-'):
            continue
        pipeline = level_filter(config, level) + service_filter(labels, source or '', '' if svc == '-' else svc)
        exact = counted(client, count_query(selector, pipeline + filters_of(needles[:MAX_NEEDLES])), now)
        again = {'svc': svc, 'level': level, 'msg': shown, **exact, 'source': source or '', 'needles': needles[:MAX_NEEDLES], 'recounted': True}
        card = judge_row(svc, level, shown, exact, again)
        if card:
            found.append(card)
            rows.append(again)
            seen.add((svc, level, shown))
    return found, rows


def floor_of(level: str) -> int:
    return ERROR_MIN if level == 'ERROR' else WARN_MIN


def quiet(status: str = 'ok \u00b7 no data') -> Sweep:
    return Sweep(PassResult(PASS, True, status, [], {}, (NO_DATA, ACCESS)), sections={SECTION: []})


def sweep(client: Loki, config: Mapping[str, str], now: float, last_status: str | None = None, previous: list | None = None) -> Sweep:
    try:
        matchers = instance_matchers(config)
        problem = option_problem(config)
        if problem:
            raise LokiError(problem, answered=True)
        selector = render(matchers)
        labels = by_labels(config)
        totals: dict[tuple[str, str, str], int] = {}
        capped = False
        for level in levels_of(config):
            rows = client.vector(metric_query(selector, level_filter(config, level), labels), now, 'Loki error count')
            capped = capped or len(rows) >= MAX_SERIES
            for metric, value in rows:
                svc, source = service_of(metric, labels)
                totals[(svc, source, level)] = totals.get((svc, source, level), 0) + int(round(value))
        if not any(totals.values()):
            if not client.streams(selector, now - WINDOW_S, now, 1, 'Loki lines'):
                return quiet()
            return Sweep(PassResult(PASS, True, 'ok', [], {}, (PASS, ACCESS)), sections={SECTION: []})
    except LokiError as e:
        if access.denied(e.code) and not e.tenant:
            card = access.card(PASS, NAME, PART, 'read access to the tenant', e.code, credential(config))
            return Sweep(PassResult(PASS, True, access.status('ok', [PART]), [card], {}, (ACCESS,)))
        return Sweep(PassResult(PASS, False, f'error: {clean(e.reason, REASON_LIMIT)}'))
    wanted = sorted((t for t, n in totals.items() if n >= floor_of(t[2])), key=lambda t: (-totals[t], t))
    found: list[dict] = []
    rows: list[dict] = []
    done = 0
    spent = False
    try:
        for target in wanted[:MAX_SERVICES]:
            cards, top = service_pass(client, config, selector, labels, target, totals[target], now)
            found += cards
            rows += top
            done += 1
        if done == min(len(wanted), MAX_SERVICES):
            cards, top = recount(client, config, selector, labels, previous, {(r['svc'], r['level'], r['msg']) for r in rows}, now)
            found += cards
            rows += top
    except LokiError as e:
        if not e.spent:
            if access.denied(e.code) and not e.tenant:
                card = access.card(PASS, NAME, PART, 'read access to the tenant', e.code, credential(config))
                return Sweep(PassResult(PASS, True, access.status('ok', [PART]), [card], {}, (ACCESS,)))
            return Sweep(PassResult(PASS, False, f'error: {clean(e.reason, REASON_LIMIT)}'))
        spent = True
    tails = []
    if done < len(wanted) or capped:
        shown = f'{len(wanted)}+' if capped else str(len(wanted))
        tails.append(f'read {done} of {shown} services')
        if 'read ' not in (last_status or ''):
            log.warning('a Loki pass read only %d services; no card clears until it reads them all', done)
    if spent:
        tails.append(f'ran out of its {client.budget.seconds if client.budget else PASS_BUDGET_S:g} s')
    found, rows = unique(found, rows)
    if tails:
        have = {(r.get('svc'), r.get('level'), r.get('msg')) for r in rows}
        rows += [r for r in previous or [] if isinstance(r, dict) and (r.get('svc'), r.get('level'), r.get('msg')) not in have]
    rows.sort(key=lambda r: (not r.get('flag'), -int(r.get('now') or 0)))
    keys = (NO_DATA, ACCESS) if tails else (PASS, ACCESS)
    return Sweep(PassResult(PASS, True, DOT.join(['ok'] + tails), found, {}, keys), sections={SECTION: rows[:TOP]})


def unique(found: list[dict], rows: list[dict]) -> tuple[list[dict], list[dict]]:
    cards: dict[str, dict] = {}
    for card in found:
        if card['key'] not in cards or card['now'] > cards[card['key']]['now']:
            cards[card['key']] = card
    kept: dict[tuple, dict] = {}
    for row in rows:
        k = (row.get('svc'), row.get('level'), row.get('msg'))
        if k not in kept or int(row.get('now') or 0) > int(kept[k].get('now') or 0):
            kept[k] = row
    return list(cards.values()), list(kept.values())
