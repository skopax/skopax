import hashlib
import json
import logging
import math
import operator
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from urllib.parse import urlencode

from pymongo.errors import DuplicateKeyError, PyMongoError

from skopax.contract import HttpError, InstanceInfo, PassResult, Transport, Unreachable, anomaly, call, iso
from skopax.errkind import Authored, kind
from skopax.kit.elastic import ElasticError, client_for as elastic_client, esql_discover, refused_text, sources_of
from skopax.kit.grafana import Datasource, DatasourceError, explore, said_of
from skopax.kit.promql import Late, auth_headers, bounded, graph, ui_of, with_headers
from skopax.messages import msg
from skopax.redact import redact, redact_text
from skopax.remote import TimedOut

log = logging.getLogger('skopax.promqlchecks')

PASS = 'custom-checks'
SIG = 'custom-check'
JOB_PERIOD = 300
JOB_OFFSET = 75
CHECKS_MAX = 50
QUERY_TIME_S = 20
PASS_TIME_S = 240
SERIES_MAX = 500
ROWS_MAX = 1000
WINDOW = 60
WINDOW_MIN = 5
WINDOW_MAX = 1440
CARDS_MAX = 20
BODY_MAX = 8 * 1024 * 1024
FOR_MAX = 12
SPLIT_MAX = 5
NAME_MAX = 80
INSTANCE_MAX = 60
DATASOURCE_MAX = 200
QUERY_MAX = 2000
TITLE_MAX = 200
TEXT_MAX = 1000
LABEL_MAX = 200
LABELS_MAX = 30
REASON_MAX = 300
STATUS_NAMES = 5
MORE_NAMES = 5
COMPARISONS = ('>', '>=', '<', '<=', '==', '!=')
HOLDS = {'>': operator.gt, '>=': operator.ge, '<': operator.lt, '<=': operator.le, '==': operator.eq, '!=': operator.ne}
BASELINES = {'1d': 86400, '1w': 604800}
BASELINE_WORDS = {'1d': 'custom_checks.day_ago', '1w': 'custom_checks.week_ago'}
EVALUATED = ('source', 'instance', 'datasource', 'query', 'window', 'op', 'threshold', 'baselines', 'baselineMode', 'by')
MODES = ('ratio', 'difference')
SOURCES = ('prometheus', 'grafana', 'elasticsearch')
SEVERITIES = ('info', 'warn', 'crit')
ID = re.compile(r'[a-z][a-z0-9-]{1,40}')
LABEL = re.compile(r'[a-zA-Z_][a-zA-Z0-9_]*')
PLACEHOLDER = re.compile(r'\{([a-zA-Z_][a-zA-Z0-9_]*)\}')
CONTROL = re.compile(r'[\x00-\x08\x0b-\x1f\x7f]')
LINE_BREAKS = re.compile(r'[\x00-\x1f\x7f]+')
ESQL_FROM = re.compile(r'\s*FROM\s', re.I)
TIME_PARAM = re.compile(r'\?(start|end)\b')
NO_TIME = 'query has no ?start/?end: reads every document'
INERT = str.maketrans({'*': '\u2217', '`': "'"})
FIELDS = ('id', 'name', 'enabled', 'source', 'instance', 'datasource', 'query', 'window', 'op', 'threshold', 'baselines', 'baselineMode', 'severity', 'by', 'for', 'title', 'text')
STAMPS = ('createdBy', 'createdAt', 'updatedBy', 'updatedAt')
TAGS = ('preset', 'presetValues')


class Invalid(Authored, Exception):
    pass


class Exists(Invalid):
    pass


class Full(Invalid):
    pass


class Missing(Invalid):
    pass


class Leftover(Authored, Exception):
    pass


class Failed(Exception):
    def __init__(self, reason: str, down: bool = False, slow: bool = False):
        super().__init__(reason)
        self.reason = reason
        self.down = down
        self.slow = slow


def flat(text: str) -> str:
    return ' '.join(LINE_BREAKS.sub(' ', text).split())


def cut(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit - 3].rstrip() + '...'


def shown(v: float) -> str:
    if v == int(v) and abs(v) < 1e15:
        return str(int(v))
    if abs(v) >= 1e6 or abs(v) < 1e-3:
        return f'{v:.4g}'
    return f'{v:.3f}'.rstrip('0').rstrip('.')


def slug(name: str) -> str:
    made = re.sub(r'-+', '-', re.sub(r'[^a-z0-9]+', '-', name.lower())).strip('-')
    return (f'check-{made}' if made[:1].isdigit() else made)[:41].rstrip('-')


def ran_key(check_id: str) -> str:
    return f'{PASS}:{check_id}'


def card_key(check_id: str, values: list[str]) -> str:
    tail = hashlib.sha256(json.dumps(list(values)).encode('utf-8')).hexdigest()[:16] if values else '-'
    return f'{PASS}|{check_id}|{tail}'


def check_of(key: object) -> str | None:
    parts = key.split('|') if isinstance(key, str) else []
    return parts[1] if len(parts) >= 3 and parts[0] == PASS else None


def _text(raw: dict, name: str, limit: int, required: bool = False, what: str | None = None, lines: bool = False) -> str:
    what = what or name
    value = raw.get(name)
    value = '' if value is None else value
    if not isinstance(value, str):
        raise Invalid(f'{what} must be text')
    value = value.strip()
    if required and not value:
        raise Invalid(f'{what} is required')
    if len(value) > limit:
        raise Invalid(f'{what} is too long (at most {limit} characters)')
    if (CONTROL if lines else LINE_BREAKS).search(value):
        raise Invalid(f'{what} holds control characters')
    return value


def _template(raw: dict, name: str, limit: int) -> str:
    value = _text(raw, name, limit)
    if '{' in PLACEHOLDER.sub('', value) or '}' in PLACEHOLDER.sub('', value):
        raise Invalid(f'the {name} may hold only placeholders such as {{instance}} or {{value}}: no expressions, formats or stray braces')
    return value


def _number(value: object, what: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise Invalid(f'{what} must be a number')
    try:
        got = float(value)
    except (TypeError, ValueError, OverflowError):
        raise Invalid(f'{what} must be a number') from None
    if not math.isfinite(got):
        raise Invalid(f'{what} must be a finite number')
    return got


def _count(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, str)) or not str(value).strip().isascii() or not str(value).strip().isdigit():
        raise Invalid(f'for must be a whole number of passes from 1 to {FOR_MAX}')
    got = int(value)
    if not 1 <= got <= FOR_MAX:
        raise Invalid(f'for must be a whole number of passes from 1 to {FOR_MAX}')
    return got


def _window(value: object) -> int:
    said = f'the window must be a whole number of minutes from {WINDOW_MIN} to {WINDOW_MAX}'
    if isinstance(value, bool) or not isinstance(value, (int, str)) or not str(value).strip().isascii() or not str(value).strip().isdigit():
        raise Invalid(said)
    got = int(value)
    if not WINDOW_MIN <= got <= WINDOW_MAX:
        raise Invalid(said)
    return got


def _given(raw: dict, name: str, default: object) -> object:
    value = raw.get(name)
    return default if value is None or value == '' else value


def clean(raw: object) -> dict:
    if not isinstance(raw, dict):
        raise Invalid('a check must be an object')
    name = _text(raw, 'name', NAME_MAX, True)
    given = raw.get('id')
    check_id = slug(name) if given in (None, '') else given
    if given in (None, '') and not ID.fullmatch(check_id):
        raise Invalid('the name gives no usable id: set an id of 2 to 41 lowercase letters, digits or dashes, starting with a letter')
    if not isinstance(check_id, str) or not ID.fullmatch(check_id):
        raise Invalid('the id must be 2 to 41 lowercase letters, digits or dashes, starting with a letter')
    enabled = raw.get('enabled', True)
    if not isinstance(enabled, bool):
        raise Invalid('enabled must be true or false')
    source = raw.get('source')
    if not isinstance(source, str) or source not in SOURCES:
        raise Invalid(f"the source must be one of {', '.join(SOURCES)}")
    instance = _text(raw, 'instance', INSTANCE_MAX, True)
    datasource = _text(raw, 'datasource', DATASOURCE_MAX, True) if source == 'grafana' else ''
    query = _text(raw, 'query', QUERY_MAX, True, lines=True)
    if source == 'elasticsearch' and not ESQL_FROM.match(query):
        raise Invalid('an Elasticsearch query must start with FROM <index>')
    window = {'window': _window(_given(raw, 'window', WINDOW))} if source == 'elasticsearch' else {}
    op = raw.get('op')
    if not isinstance(op, str) or op not in COMPARISONS:
        raise Invalid(f"the comparison must be one of {' '.join(COMPARISONS)}")
    threshold = _number(raw.get('threshold'), 'the threshold')
    baselines = _given(raw, 'baselines', [])
    if not isinstance(baselines, list) or any(not isinstance(b, str) or b not in BASELINES for b in baselines):
        raise Invalid(f"a baseline must be one of {', '.join(BASELINES)}")
    mode = _given(raw, 'baselineMode', 'ratio')
    if not isinstance(mode, str) or mode not in MODES:
        raise Invalid(f"the baseline mode must be one of {', '.join(MODES)}")
    severity = _given(raw, 'severity', 'warn')
    if not isinstance(severity, str) or severity not in SEVERITIES:
        raise Invalid(f"the severity must be one of {', '.join(SEVERITIES)}")
    by = _given(raw, 'by', [])
    if not isinstance(by, list) or len(by) > SPLIT_MAX or any(not isinstance(b, str) or not LABEL.fullmatch(b) or len(b) > LABEL_MAX for b in by):
        raise Invalid(f'split by at most {SPLIT_MAX} label names such as instance or job')
    return {'id': check_id, 'name': name, 'enabled': enabled, 'source': source, 'instance': instance, 'datasource': datasource, 'query': query, **window, 'op': op,
            'threshold': threshold, 'baselines': [b for b in BASELINES if b in baselines], 'baselineMode': mode, 'severity': severity, 'by': list(dict.fromkeys(by)),
            'for': _count(raw.get('for', 1)), 'title': _template(raw, 'title', TITLE_MAX), 'text': _template(raw, 'text', TEXT_MAX)}


def inert(text: str) -> str:
    return text.translate(INERT)


def render(template: str, labels: dict, value: float, limit: int = TITLE_MAX) -> str:
    def fill(m: re.Match) -> str:
        return shown(value) if m[1] == 'value' else inert(flat(str(labels.get(m[1], '')))[:LABEL_MAX])

    return cut(flat(PLACEHOLDER.sub(fill, template)), limit)


@dataclass
class Group:
    values: list[str]
    labels: dict
    value: float
    bases: dict = field(default_factory=dict)
    compared: dict = field(default_factory=dict)
    fires: bool = False


@dataclass
class Outcome:
    groups: list[Group]
    total: int
    read: int
    skipped: int
    missing: int = 0
    warnings: list = field(default_factory=list)

    def firing(self) -> list[Group]:
        return [g for g in self.groups if g.fires]


def series_of(data: dict) -> tuple[list[tuple[dict, float]], int, int]:
    kind_of, result = data.get('resultType'), data.get('result')
    if kind_of == 'scalar':
        rows = [{'metric': {}, 'value': result}]
    elif kind_of == 'vector' and isinstance(result, list):
        rows = [r for r in result if isinstance(r, dict)]
    else:
        raise Failed(f'the query must give an instant vector or a scalar, not a {flat(str(kind_of))[:20]}')
    total = len(rows)
    rows = sorted(rows, key=lambda r: json.dumps(r.get('metric'), sort_keys=True, default=str))[:SERIES_MAX]
    out, skipped = [], 0
    for r in rows:
        try:
            v = float((r.get('value') or [None, None])[1])
        except (TypeError, ValueError, IndexError, OverflowError):
            v = math.nan
        if not math.isfinite(v):
            skipped += 1
            continue
        metric = r.get('metric') if isinstance(r.get('metric'), dict) else {}
        labels = {str(k)[:LABEL_MAX]: flat(str(x))[:LABEL_MAX] for k, x in sorted(metric.items())[:LABELS_MAX]}
        out.append((labels, v))
    return out, total, skipped


def grouped(series: list[tuple[dict, float]], by: list[str], op: str) -> dict[tuple, tuple[dict, float]]:
    out: dict[tuple, tuple[dict, float]] = {}
    for labels, v in series:
        k = tuple(labels.get(b, '') for b in by)
        was = out.get(k)
        if was is None or (op in ('>', '>=') and v > was[1]) or (op in ('<', '<=') and v < was[1]):
            out[k] = (labels, v)
    return out


def compared(value: float, base: float | None, mode: str) -> float | None:
    if base is None or (mode == 'ratio' and base == 0):
        return None
    got = value / base if mode == 'ratio' else value - base
    return got if math.isfinite(got) else None


def rank(op: str, g: Group) -> tuple:
    lead = -g.value if op in ('>', '>=') else g.value if op in ('<', '<=') else 0
    return lead, g.values


def evaluate(d: dict, asker: 'Asker', now: float) -> Outcome:
    series, total, skipped = asker.series(d, now)
    groups = grouped(series, d['by'], d['op'])
    bases = {o: grouped(asker.series(d, now - BASELINES[o])[0], d['by'], d['op']) for o in d['baselines']} if groups else {}
    hold = HOLDS[d['op']]
    out, missing = [], 0
    for values, (labels, v) in groups.items():
        g = Group(list(values), labels, v)
        for o in d['baselines']:
            found = bases[o].get(values)
            g.bases[o] = None if found is None else found[1]
            g.compared[o] = compared(v, g.bases[o], d['baselineMode'])
        missing += any(g.bases[o] is None for o in d['baselines'])
        g.fires = all(c is not None and hold(c, d['threshold']) for c in g.compared.values()) if d['baselines'] else hold(v, d['threshold'])
        out.append(g)
    out.sort(key=lambda g: rank(d['op'], g))
    return Outcome(out, total, len(series) + skipped, skipped, missing, list(asker.warnings))


def joined(parts: list[str]) -> str:
    return parts[0] if len(parts) == 1 else msg('custom_checks.both', first=parts[0], second=parts[1])


def fact(d: dict, g: Group) -> str:
    if not d['baselines']:
        return msg('custom_checks.fact', name=d['name'], value=shown(g.value), op=d['op'], threshold=shown(d['threshold']))
    parts = joined([msg('custom_checks.to_base', value=shown(g.compared[o]), when=msg(BASELINE_WORDS[o])) for o in d['baselines']])
    then = ', '.join(msg('custom_checks.base_value', when=msg(BASELINE_WORDS[o]), value=shown(g.bases[o])) for o in d['baselines'])
    return msg('custom_checks.fact_baseline', name=d['name'], mode=msg(f"custom_checks.{d['baselineMode']}"), parts=parts, op=d['op'], threshold=shown(d['threshold']),
               value=shown(g.value), then=then)


def card_of(d: dict, g: Group, link: dict | None) -> dict:
    labels = {k: redact(v) for k, v in g.labels.items()}
    title = render(d['title'], labels, g.value) if d['title'] else ''
    title = title or cut(flat(' '.join([d['name']] + [inert(labels.get(b, '')) for b in d['by'] if labels.get(b)])), TITLE_MAX)
    text = render(d['text'], labels, g.value, TEXT_MAX) if d['text'] else ''
    what = f'{text} {fact(d, g)}' if text else fact(d, g)
    sev = d['severity']
    short = cut(msg('custom_checks.short', sev=sev.upper(), title=title, value=shown(g.value)), TITLE_MAX + 40)
    x = anomaly(PASS, SIG, '-', d['name'], '', sev, g.value, g.bases.get('1d'), g.bases.get('1w'), '', title, what, 1, short)
    x.update(key=card_key(d['id'], g.values), ranKey=ran_key(d['id']), checkId=d['id'], checkLabels={b: labels.get(b, '') for b in d['by']})
    if link is not None:
        x['links'] = [link]
    return x


def more_of(d: dict, rest: list[Group], kept: int) -> dict:
    sev = d['severity']
    names = ', '.join(cut(inert(' '.join(g.values)) or d['name'], 60) for g in rest[:MORE_NAMES]) + (', ...' if len(rest) > MORE_NAMES else '')
    what = msg('custom_checks.more', count=len(rest), name=d['name'], op=d['op'], threshold=shown(d['threshold']), kept=kept, names=redact(names))
    x = anomaly(PASS, SIG, '-', d['name'], 'more', sev, len(rest), None, None, '', msg('custom_checks.more_label', count=len(rest)), what, 1,
                msg('custom_checks.more_short', sev=sev.upper(), name=d['name'], count=len(rest)))
    x.update(key=card_key(d['id'], []) + '|more', ranKey=ran_key(d['id']), checkId=d['id'], checkLabels={})
    return x


def cards_of(d: dict, ready: list[Group], link: Callable[[], dict | None], held: frozenset = frozenset()) -> list[dict]:
    ready = sorted(ready, key=lambda g: card_key(d['id'], g.values) not in held)
    shown_groups = ready if len(ready) <= CARDS_MAX else ready[:CARDS_MAX - 1]
    out = [card_of(d, g, link()) for g in shown_groups]
    if len(ready) > len(shown_groups):
        out.append(more_of(d, ready[len(shown_groups):], len(shown_groups)))
    return out


class Asker:
    name = ''
    warnings: tuple = ()

    def data(self, expr: str, at: float) -> dict:
        raise NotImplementedError

    def series(self, d: dict, at: float) -> tuple[list[tuple[dict, float]], int, int]:
        return series_of(self.data(d['query'], at))

    def link(self, expr: str) -> dict | None:
        return None


def late() -> Failed:
    return Failed(f'the queries took longer than {QUERY_TIME_S} s', True)


class PromAsker(Asker):
    def __init__(self, info: InstanceInfo, transport: Transport):
        extra = auth_headers(info.config)
        held = bounded(transport, QUERY_TIME_S)
        self.transport = with_headers(held, extra) if extra else held
        self.url = (info.config.get('url') or '').rstrip('/')
        self.ui = ui_of(info.config)
        self.name = f'Prometheus {info.name}'

    def data(self, expr: str, at: float) -> dict:
        url = f'{self.url}/api/v1/query?' + urlencode({'query': expr, 'time': f'{at:.3f}', 'timeout': f'{QUERY_TIME_S}s'})
        try:
            body = call(self.transport, 'GET', url, timeout=QUERY_TIME_S, limit=BODY_MAX).body
        except (Late, TimedOut):
            raise late() from None
        except HttpError as e:
            said = said_of(e.body)
            raise Failed(f'{self.name} answered HTTP {e.code}' + (f': {said}' if said else '')) from None
        except Unreachable:
            raise Failed(f'{self.name} is unreachable', True) from None
        if not isinstance(body, dict) or body.get('status') != 'success' or not isinstance(body.get('data'), dict):
            raise Failed(f'{self.name} did not answer with a query result')
        return body['data']

    def link(self, expr: str) -> dict | None:
        url = graph(expr, self.ui)
        return {'label': msg('custom_checks.link_prometheus'), 'url': url} if url else None


class GrafanaAsker(Asker):
    def __init__(self, info: InstanceInfo, transport: Transport, ref: str, listings: dict):
        self.info = info
        self.source = Datasource(info.config.get('url') or '', info.config.get('token') or '', bounded(transport, QUERY_TIME_S), ref, QUERY_TIME_S, BODY_MAX)
        self.listings = listings
        self.name = f'Grafana {info.name} {ref}'

    def data(self, expr: str, at: float) -> dict:
        try:
            if not self.source.found:
                listed = self.listings.get(self.info.name)
                if listed is None:
                    listed = self.listings[self.info.name] = self.source.listed()
                self.source.uid(listed)
            return self.source.data('query', query=expr, time=f'{at:.3f}', timeout=f'{QUERY_TIME_S}s')
        except Late:
            raise late() from None
        except DatasourceError as e:
            if e.late:
                raise late() from None
            right = f' (the token needs {e.right})' if e.code in (401, 403) and e.right else ''
            raise Failed(e.reason + right + (f': {e.said}' if e.said else ''), e.code is None and e.reason.endswith(': unreachable')) from None

    def link(self, expr: str) -> dict | None:
        url = explore(self.info.config.get('url') or '', self.source.found or '', expr)
        return {'label': msg('custom_checks.link_grafana'), 'url': url} if url else None


def label_text(value: object) -> str:
    if value is None:
        return ''
    if isinstance(value, list):
        return ', '.join(label_text(x) for x in value)[:LABEL_MAX]
    if isinstance(value, bool):
        return 'true' if value else 'false'
    if isinstance(value, float) and math.isfinite(value):
        return shown(value)
    return flat(str(value))[:LABEL_MAX]


def not_a_number(value: object) -> str:
    return 'true or false' if isinstance(value, bool) else 'a list' if isinstance(value, list) else 'text' if isinstance(value, str) else 'an object'


def rows_of(columns: list[str], rows: list[dict], by: list[str]) -> tuple[list[tuple[dict, float]], int, int]:
    if 'value' not in columns:
        raise Failed('the query returns no column named value: name the number value, such as STATS value = COUNT(*)')
    lacking = next((b for b in by if b not in columns), None)
    if lacking is not None:
        raise Failed(f'the query returns no column named {lacking}, which by names')
    total = len(rows)
    out = []
    for r in rows:
        v = r['value']
        if v is None:
            raise Failed('the value column is null in a row: drop such rows with WHERE value IS NOT NULL')
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            raise Failed(f'the value column must hold numbers, not {not_a_number(v)}')
        if not math.isfinite(v):
            raise Failed('the value column holds a number that is not finite')
        names = list(dict.fromkeys(list(by) + sorted(str(k) for k in r if k != 'value')))[:LABELS_MAX]
        out.append(({k[:LABEL_MAX]: label_text(r.get(k)) for k in names}, float(v)))
    out.sort(key=lambda x: json.dumps(x[0], sort_keys=True))
    return out[:ROWS_MAX], total, 0


class ElasticAsker(Asker):
    def __init__(self, info: InstanceInfo, transport: Transport, window: int):
        self.es = elastic_client(info, bounded(transport, QUERY_TIME_S))
        self.ui = str(info.config.get('ui-url') or '')
        self.window = window
        self.name = f'Elasticsearch {info.name}'
        self.warnings: list[str] = []

    def rows(self, query: str, at: float) -> tuple[list[str], list[dict]]:
        named = set(TIME_PARAM.findall(query))
        params = [{k: v} for k, v in (('start', iso(at - self.window * 60)), ('end', iso(at))) if k in named]
        try:
            return self.es.answer(query, timeout=QUERY_TIME_S, params=params, limit=BODY_MAX)
        except Late:
            raise Failed(late().reason, slow=True) from None
        except ElasticError as e:
            if e.timed_out:
                raise Failed(late().reason, slow=True) from None
            if e.too_large:
                raise Failed(f'{self.name} answered more than {BODY_MAX // 1048576} MB: aggregate with STATS or keep fewer columns') from None
            if not e.status and e.reason.endswith('unreachable'):
                raise Failed(f'{self.name} is unreachable', True) from None
            if not e.status:
                raise Failed(f'{self.name} did not answer with a query result') from None
            said = refused_text(e.status, sources_of(query)).removesuffix(f' (HTTP {e.status})') if e.denied else e.detail
            raise Failed(f'{self.name} answered HTTP {e.status}' + (f': {said}' if said else '')) from None

    def series(self, d: dict, at: float) -> tuple[list[tuple[dict, float]], int, int]:
        columns, got = self.rows(d['query'], at)
        series, total, skipped = rows_of(columns, got, d['by'])
        if total >= ROWS_MAX and not self.warnings:
            self.warnings.append(f'the answer has {total} rows: Elasticsearch gives at most {ROWS_MAX} rows unless the query sets a higher LIMIT, and at most {ROWS_MAX} are read')
        return series, total, skipped

    def link(self, expr: str) -> dict | None:
        url = esql_discover(self.ui, expr, self.window)
        return {'label': msg('custom_checks.link_kibana'), 'url': url} if url else None


class Definitions:
    def __init__(self, db):
        self.col = db['custom_checks']
        self.runs = db['custom_check_state']

    @staticmethod
    def view(doc: dict) -> dict:
        return {k: doc.get(k) for k in FIELDS + TAGS + STAMPS}

    def list(self) -> list[dict]:
        return [self.view(d) for d in sorted(self.col.find({}), key=lambda d: str(d.get('_id')))]

    def get(self, check_id: str) -> dict | None:
        found = self.col.find_one({'_id': check_id})
        return None if found is None else self.view(found)

    def any(self) -> bool:
        return self.col.find_one({}, {'_id': 1}) is not None

    def create(self, d: dict, by: str, tags: dict | None = None) -> dict:
        if self.col.count_documents({}) >= CHECKS_MAX:
            raise Full(f'at most {CHECKS_MAX} custom checks can be set up')
        at = iso(time.time())
        doc = {'_id': d['id'], **d, **{k: v for k, v in (tags or {}).items() if k in TAGS}, 'createdBy': by, 'createdAt': at, 'updatedBy': by, 'updatedAt': at}
        try:
            self.col.insert_one(doc)
        except DuplicateKeyError:
            raise Exists(f"a custom check with the id {d['id']} exists") from None
        if self.col.count_documents({}) > CHECKS_MAX:
            self.col.delete_one({'_id': d['id'], 'createdAt': at})
            raise Full(f'at most {CHECKS_MAX} custom checks can be set up')
        self.runs.delete_one({'_id': d['id']})
        return self.view(doc)

    def update(self, check_id: str, d: dict, by: str) -> dict:
        was = self.col.find_one({'_id': check_id})
        if was is None:
            raise Missing(f'no custom check with the id {check_id}')
        doc = {'_id': check_id, **d, 'id': check_id, **{k: was[k] for k in TAGS if k in was}, 'createdBy': was.get('createdBy'), 'createdAt': was.get('createdAt'), 'updatedBy': by,
               'updatedAt': iso(time.time())}
        self.col.replace_one({'_id': check_id}, doc)
        if any(was.get(k) != doc.get(k) for k in EVALUATED):
            self.runs.update_one({'_id': check_id}, {'$set': {'pending': {}}}, upsert=True)
        return self.view(doc)

    def create_all(self, ds: list[dict], by: str, tags: dict, what: str) -> list[dict]:
        have = self.col.count_documents({})
        if have + len(ds) > CHECKS_MAX:
            raise Full(f'{what} adds {len(ds)} checks, but {max(0, CHECKS_MAX - have)} more fit: at most {CHECKS_MAX} custom checks can be set up')
        made = []
        try:
            for d in ds:
                made.append(self.create(d, by, tags))
        except Exception as e:
            left = []
            for done in made:
                try:
                    self.col.delete_one({'_id': done['id'], 'createdAt': done['createdAt']})
                except PyMongoError:
                    left.append(done['id'])
            if not left:
                raise
            log.error('%s failed half-way and left these custom checks behind: %s', what, ', '.join(left))
            said = f"these checks were added and could not be taken back, delete them by hand: {', '.join(left)}"
            if isinstance(e, Invalid):
                raise type(e)(f'{e.text}; {said}') from None
            raise Leftover(f'{what} failed ({kind(e)}); {said}') from None
        return made

    def delete(self, check_id: str) -> dict | None:
        found = self.col.find_one_and_delete({'_id': check_id})
        self.runs.delete_one({'_id': check_id})
        return None if found is None else self.view(found)

    def state(self, check_id: str) -> dict | None:
        found = self.runs.find_one({'_id': check_id})
        return None if found is None else {k: v for k, v in found.items() if k != '_id'}

    def states(self) -> dict[str, dict]:
        return {str(d['_id']): {k: v for k, v in d.items() if k != '_id'} for d in self.runs.find({})}

    def set_state(self, check_id: str, doc: dict) -> None:
        self.runs.replace_one({'_id': check_id}, {'_id': check_id, **doc}, upsert=True)


def edited_since(was: dict, now: dict) -> bool:
    return any(was.get(k) != now.get(k) for k in EVALUATED)


def status_of(failing: list[str], on: int) -> str:
    if not failing:
        return 'ok'
    names = ', '.join(failing[:STATUS_NAMES])
    if len(failing) > STATUS_NAMES:
        names = msg('custom_checks.failing_more', names=names, count=len(failing) - STATUS_NAMES)
    return cut(msg('custom_checks.failing', count=len(failing), total=on, names=names), REASON_MAX)


def no_links(card: dict) -> None:
    return None


class CustomChecks:
    def __init__(self, store, runtime, apply: Callable, monotonic: Callable[[], float] = time.monotonic):
        self.store = store
        self.runtime = runtime
        self.apply = apply
        self.monotonic = monotonic
        self.defs = Definitions(store.db)

    def enabled(self) -> bool:
        try:
            return self.defs.any() or bool(self.store.open_cards_of(PASS))
        except PyMongoError as e:
            log.warning('the custom checks could not be read: %s', kind(e))
            return False

    def sources(self) -> dict[str, list[str]]:
        return {s: sorted(i.name for i in self.runtime.instances_of(s)) for s in SOURCES}

    def asker(self, d: dict, listings: dict) -> Asker:
        plugin = next((p for p in self.runtime.active() if p.id == d['source']), None)
        found = next((i for i in self.runtime.instances_of(d['source']) if i.name == d['instance']), None) if plugin is not None else None
        if found is None:
            raise Failed(f"no {d['source']} instance named {d['instance']}")
        info = found.info()
        transport = self.runtime.transport_for(plugin, info)
        if d['source'] == 'elasticsearch':
            return ElasticAsker(info, transport, d.get('window') or WINDOW)
        return PromAsker(info, transport) if d['source'] == 'prometheus' else GrafanaAsker(info, transport, d['datasource'], listings)

    def _one(self, d: dict, listings: dict, now: float, old: dict | None = None, held: frozenset = frozenset()) -> tuple[Outcome, dict, list[dict]]:
        try:
            asker = self.asker(d, listings)
            outcome = evaluate(d, asker, now)
            firing = outcome.firing()
            pending = {}
            for g in firing:
                since = (old or {}).get(card_key(d['id'], g.values))
                pending[card_key(d['id'], g.values)] = float(since) if isinstance(since, (int, float)) and not isinstance(since, bool) and since <= now else now
            wait = (d['for'] - 1) * JOB_PERIOD - JOB_PERIOD / 2
            ready = firing if old is None else [g for g in firing if card_key(d['id'], g.values) in held or now - pending[card_key(d['id'], g.values)] >= wait]
            return outcome, pending, cards_of(d, ready, lambda: asker.link(d['query']), held)
        except Failed:
            raise
        except Exception as e:
            raise Failed(f'the check failed ({kind(e)})') from None

    def run_once(self, now: float) -> PassResult:
        defs = self.defs.list()
        states = self.defs.states()
        held = frozenset(a.get('key') for a in self.store.open_cards_of(PASS))
        deadline = self.monotonic() + PASS_TIME_S
        listings: dict = {}
        down: dict[tuple[str, str], bool] = {}
        slow: set[tuple[str, str]] = set()
        found, ran, writes = [], [], {}
        for d in sorted(defs, key=lambda d: (str((states.get(d['id']) or {}).get('ranAt') or ''), d['id'])):
            was = states.get(d['id']) or {}
            if not d['enabled']:
                writes[d['id']] = {'state': 'off', 'at': iso(now), 'pending': {}}
                ran.append(ran_key(d['id']))
                continue
            where = (d['source'], d['instance'])
            tried = False
            try:
                if self.monotonic() >= deadline:
                    raise Failed('not run: the pass ran out of time')
                if where in down:
                    raise Failed(f'not run: {d["source"]} {d["instance"]} did not answer earlier in this pass')
                tried = True
                old = was.get('pending') if isinstance(was.get('pending'), dict) else {}
                outcome, pending, made = self._one(d, listings, now, old, held)
            except Failed as e:
                if e.down or (e.slow and where in slow):
                    down[where] = True
                elif e.slow:
                    slow.add(where)
                writes[d['id']] = {**was, 'state': 'failing', 'reason': redact_text(e.reason, REASON_MAX), 'at': iso(now), 'pending': {}} | ({'ranAt': iso(now)} if tried else {})
                continue
            found += made
            ran.append(ran_key(d['id']))
            writes[d['id']] = {'state': 'ok', 'reason': '', 'at': iso(now), 'ranAt': iso(now), 'series': outcome.total, 'read': outcome.read, 'skipped': outcome.skipped,
                               'noBaseline': outcome.missing, 'firing': len(pending), 'cards': len(made), 'pending': pending}
        now_defs = {d['id']: d for d in self.defs.list()}
        live = {i for i, d in now_defs.items() if d['enabled']}
        loaded = {d['id']: d for d in defs}
        edited = {i for i in live if i in loaded and edited_since(loaded[i], now_defs[i])}
        found = [c for c in found if c.get('checkId') in live - edited]
        ran = [k for k in ran if k not in {ran_key(i) for i in edited}]
        for check_id, doc in writes.items():
            if check_id in edited:
                continue
            if check_id in live:
                self.defs.set_state(check_id, doc)
            elif check_id in now_defs:
                self.defs.set_state(check_id, {'state': 'off', 'at': iso(now), 'pending': {}})
        for check_id in {check_of(k) for k in held} - set(now_defs) - {None}:
            ran.append(ran_key(check_id))
        ran += [ran_key(i) for i in set(now_defs) - live]
        failing = [now_defs[i]['name'] for i in sorted(live, key=lambda i: i) if (writes.get(i) or {}).get('state') == 'failing']
        result = PassResult(PASS, True, status_of(failing, len(live)), found, {}, tuple(sorted(set(ran))))
        self.apply([result], now, no_links)
        return result

    def status(self) -> str:
        states = self.defs.states()
        on = [d for d in self.defs.list() if d['enabled']]
        return status_of([d['name'] for d in on if (states.get(d['id']) or {}).get('state') == 'failing'], len(on))

    def clear(self, check_id: str, now: float) -> None:
        self.apply([PassResult(PASS, True, self.status(), [], {}, (ran_key(check_id),))], now, no_links)

    def preview(self, raw: object, now: float) -> dict:
        d = clean(raw)
        try:
            outcome, pending, made = self._one(d, {}, now)
        except Failed as e:
            return {'ok': False, 'reason': redact_text(e.reason, REASON_MAX), 'cards': [], 'series': 0, 'read': 0, 'skipped': 0, 'noBaseline': 0, 'firing': 0, 'for': d['for'],
                    'warnings': []}
        warnings = ([NO_TIME] if d['source'] == 'elasticsearch' and 'start' not in TIME_PARAM.findall(d['query']) else []) + [redact_text(w, REASON_MAX) for w in outcome.warnings]
        return {'ok': True, 'reason': '', 'warnings': warnings, 'series': outcome.total, 'read': outcome.read, 'skipped': outcome.skipped, 'noBaseline': outcome.missing, 'firing': len(pending),
                'for': d['for'], 'cards': [{'key': c['key'], 'sev': c['sev'], 'title': c['label'], 'text': c['what'], 'value': c['now'], 'd1': c['d1'], 'd7': c['d7'], 'labels': c['checkLabels'],
                           'links': c.get('links') or [], 'more': int(c['now']) if c['key'].endswith('|more') else 0} for c in made]}
