import json
import math
import re
from urllib.parse import quote, urlencode

from skopax.contract import CardType, Check, Context, Field, HttpError, InstanceInfo, Peer, PassResult, Plugin, ReadError, RuleSet, Sweep, TestResult, Tool, Unreachable, \
    anomaly, call, iso
from skopax.kit import access, connection
from skopax.kit.promql import Late, bounded
from skopax.monitor import failed

ID = 'managed-redis'
NAME = 'Managed Redis'
TOOL = 'managed_redis'
PART = 'metrics'
PART_NAME = 'Redis metrics'
WINDOW = '30m'
OPS = ('instances', 'history')
HISTORY_HOURS = 48
HISTORY_POINTS = 120
MAX_ROWS = 50
TIMEOUT = 20
BUDGET_S = 200
NAME_MAX = 100
LIST_RIGHT = 'datasources:read'
QUERY_RIGHT = 'datasources:query'
NO_PEER = 'no Grafana instance is set'
DEFAULT_PREFIX = 'redis_'
DEFAULT_LABEL = 'instance'
PREFIX = re.compile(r'[a-zA-Z_:][a-zA-Z0-9_:]*')
LABEL = re.compile(r'[a-zA-Z_][a-zA-Z0-9_]*')
BAD_PREFIX = f'the metric prefix must be a metric name start such as {DEFAULT_PREFIX}'
BAD_LABEL = f'the instance label must be a label name such as {DEFAULT_LABEL}'
QUERIES = dict(
    up='max by ({l}) ({p}up)',
    clients='max by ({l}) (max_over_time({p}connected_clients[{w}]))',
    maxClients='max by ({l}) ({p}config_maxclients)',
    memory='max by ({l}) (max_over_time({p}memory_used_bytes[{w}]))',
    maxMemory='max by ({l}) ({p}memory_max_bytes)',
    evicted='sum by ({l}) (increase({p}evicted_keys_total[{w}]))',
    rejected='sum by ({l}) (increase({p}rejected_connections_total[{w}]))',
    uptime='min by ({l}) ({p}uptime_in_seconds)',
    ops='sum by ({l}) (rate({p}commands_processed_total[{w}]))',
    **{key: '1e6 * sum by ({l}) (increase({p}commands_duration_seconds_total[{w}]' + offset + ')) / sum by ({l}) (increase({p}commands_processed_total[{w}]' + offset + '))'
       for key, offset in (('avgUs', ''), ('avgD1', ' offset 1d'), ('avgD7', ' offset 7d'))},
)
HISTORY = {
    'up': 'max by ({l}) ({p}up{{{sel}}})',
    'clients': 'max by ({l}) ({p}connected_clients{{{sel}}})',
    'memory': 'max by ({l}) ({p}memory_used_bytes{{{sel}}})',
    'evicted': 'sum by ({l}) (rate({p}evicted_keys_total{{{sel}}}[5m]))',
    'rejected': 'sum by ({l}) (rate({p}rejected_connections_total{{{sel}}}[5m]))',
    'uptime': 'min by ({l}) ({p}uptime_in_seconds{{{sel}}})',
    'ops': 'sum by ({l}) (rate({p}commands_processed_total{{{sel}}}[5m]))',
}
LINK = {
    'managed-redis-down': 'max by ({l}) ({p}up{{{sel}}})',
    'managed-redis-memory': '100 * max by ({l}) ({p}memory_used_bytes{{{sel}}}) / max by ({l}) ({p}memory_max_bytes{{{sel}}})',
    'managed-redis-clients': '100 * max by ({l}) ({p}connected_clients{{{sel}}}) / max by ({l}) ({p}config_maxclients{{{sel}}})',
    'managed-redis-latency': '1e6 * sum by ({l}) (rate({p}commands_duration_seconds_total{{{sel}}}[5m])) / sum by ({l}) (rate({p}commands_processed_total{{{sel}}}[5m]))',
    'managed-redis-evicted': 'sum by ({l}) (increase({p}evicted_keys_total{{{sel}}}[30m]))',
    'managed-redis-rejected': 'sum by ({l}) (increase({p}rejected_connections_total{{{sel}}}[30m]))',
    'managed-redis-restart': 'min by ({l}) ({p}uptime_in_seconds{{{sel}}})',
}
ROW_KEYS = ('name', 'up', 'clients', 'maxClients', 'clientsPct', 'memory', 'maxMemory', 'memoryPct', 'evicted', 'rejected', 'uptime', 'ops', 'avgUs', 'avgD1', 'avgD7')
RULES = {'mem_warn': 85, 'mem_crit': 95, 'conn_warn': 80, 'rt_times': 3, 'rt_floor_us': 200, 'uptime_s': 3600}
RULE_HELP = {
    'mem_warn': 'Warning when an instance peaks at this percent of its memory limit in 30 minutes.',
    'mem_crit': 'Critical when an instance peaks at this percent of its memory limit in 30 minutes.',
    'conn_warn': 'Warning when an instance peaks at this percent of its client limit (maxclients).',
    'rt_times': 'Warning when the average command time is this many times the same 30 minutes 1 or 7 days earlier.',
    'rt_floor_us': 'The command time is checked only when it is at least this many microseconds.',
    'uptime_s': 'Warning when an instance has been up for less than this many seconds.',
}
SECTION_HELP = {ID: 'one row per managed Redis instance (part instance) with 30 min peaks and the average command time, and one summary row naming the datasource'}
INSTANCES_NOTE = ('one row per Redis instance, named by its label: up (1 or 0), clients and memory as the peak of the last 30 min with their limits and the percent of them '
                  '(no percent when no limit is set), evicted keys and rejected connections in 30 min, uptime in seconds, ops/s, and the average command time in us '
                  'with avgD1 and avgD7, the same 30 min 1 and 7 days earlier')
DESCRIPTION = ('Managed Redis, read only, through Grafana instance {field:grafana} and its datasource {field:datasource}, for Redis that cannot be reached directly. '
               f'op instances (default): every Redis instance with up, clients and memory against their limits, evicted keys, rejected connections, uptime, ops/s and the '
               f'average command time in us with the same 30 min 1 and 7 days earlier, down ones first, at most {MAX_ROWS}. op history (name, metric, hours 1-{HISTORY_HOURS}): '
               f"one metric of one instance over time ({', '.join(HISTORY)}). Use it on managed Redis cards and on a cache question. At most 6 calls per run.")
SCHEMA = {'type': 'object', 'properties': {'op': {'type': 'string', 'enum': list(OPS)}, 'name': {'type': 'string'}, 'metric': {'type': 'string', 'enum': list(HISTORY)},
                                           'hours': {'type': 'integer', 'minimum': 1, 'maximum': HISTORY_HOURS}}, 'additionalProperties': False}


class SourceError(Exception):
    def __init__(self, reason: str, code: int | None = None, right: str = ''):
        super().__init__(reason)
        self.reason = reason
        self.code = code
        self.right = right


class Datasource:
    def __init__(self, url: str, token: str, transport, name: str):
        self.url = (url or '').rstrip('/')
        self.token = token
        self.transport = transport
        self.name = name
        self.found: str | None = None

    def get(self, path: str, what: str, right: str, **params) -> object:
        url = f'{self.url}{path}' + (f'?{urlencode(params)}' if params else '')
        try:
            return call(self.transport, 'GET', url, {'Authorization': f'Bearer {self.token}'}, timeout=TIMEOUT).body
        except HttpError as e:
            raise SourceError(f'{what}: HTTP {e.code}', e.code, right) from None
        except Unreachable:
            raise SourceError(f'{what}: unreachable') from None

    def uid(self) -> str:
        if self.found:
            return self.found
        listed = self.get('/api/datasources', 'Grafana datasources', LIST_RIGHT)
        if not isinstance(listed, list):
            raise SourceError('Grafana datasources: the answer is not a list')
        found = next((d for d in listed if isinstance(d, dict) and d.get('name') == self.name and d.get('uid')), None)
        if not found:
            raise SourceError(f'no Grafana datasource named {self.name}')
        self.found = str(found['uid'])
        return self.found

    def result(self, path: str, **params) -> list[dict]:
        body = self.get(f'/api/datasources/proxy/uid/{quote(self.uid(), safe="")}/api/v1/{path}', self.name, QUERY_RIGHT, **params)
        if not isinstance(body, dict) or body.get('status') != 'success':
            raise SourceError(f'{self.name}: the answer is not a Prometheus success')
        data = body.get('data')
        found = data.get('result') if isinstance(data, dict) else None
        return [r for r in found if isinstance(r, dict)] if isinstance(found, list) else []

    def vec(self, expr: str) -> list[tuple[dict, float]]:
        out = []
        for s in self.result('query', query=expr):
            try:
                v = float((s.get('value') or [None, None])[1])
            except (TypeError, ValueError, IndexError):
                continue
            if not math.isnan(v) and abs(v) != float('inf'):
                out.append((s.get('metric') or {}, v))
        return out

    def span(self, expr: str, start: float, end: float, step: int) -> list[dict]:
        return self.result('query_range', query=expr, start=f'{start:.0f}', end=f'{end:.0f}', step=str(step))


class Where:
    def __init__(self, ctx: Context):
        config = ctx.instance.config
        self.source = (config.get('datasource') or '').strip()
        self.prefix = (config.get('prefix') or '').strip()
        self.label = (config.get('label') or '').strip()

    def problem(self) -> str | None:
        if len(self.prefix) > NAME_MAX or not PREFIX.fullmatch(self.prefix):
            return BAD_PREFIX
        if len(self.label) > NAME_MAX or not LABEL.fullmatch(self.label):
            return BAD_LABEL
        return None

    def matcher(self, name: str) -> str:
        return f'{self.label}={json.dumps(name)}'

    def expr(self, template: str, name: str) -> str:
        return template.format(l=self.label, p=self.prefix, sel=self.matcher(name))


def queries(prefix: str, label: str) -> dict[str, str]:
    return {k: q.format(l=label, p=prefix, w=WINDOW) for k, q in QUERIES.items()}


def client_for(peer: Peer, where: Where, transport=None) -> Datasource:
    config = peer.info.config
    return Datasource(config.get('url') or '', config.get('token') or '', transport or peer.http(), where.source)


def pct(used, limit) -> float | None:
    return None if used is None or not limit else round(100 * used / limit, 2)


def rows_of(d: Datasource, where: Where) -> list[dict]:
    q = queries(where.prefix, where.label)
    rows = {str(m.get(where.label)): {'name': str(m.get(where.label)), 'up': round(v, 3)} for m, v in d.vec(q['up']) if m.get(where.label)}
    for key, expr in q.items():
        if key == 'up':
            continue
        for m, v in d.vec(expr):
            name = str(m.get(where.label) or '')
            if name in rows:
                rows[name][key] = round(v, 3)
    for row in rows.values():
        row['clientsPct'] = pct(row.get('clients'), row.get('maxClients'))
        row['memoryPct'] = pct(row.get('memory'), row.get('maxMemory'))
    return [{k: r.get(k) for k in ROW_KEYS} for r in sorted(rows.values(), key=lambda x: x['name'])]


def explore(ui: str, uid: str, expr: str) -> str | None:
    if not ui.startswith('https://') or not uid:
        return None
    panes = {'a': {'datasource': uid, 'queries': [{'refId': 'A', 'expr': expr, 'datasource': {'type': 'prometheus', 'uid': uid}}], 'range': {'from': 'now-2d', 'to': 'now'}}}
    return f"{ui.rstrip('/')}/explore?schemaVersion=1&panes={quote(json.dumps(panes, separators=(',', ':')), safe='')}"


def cards_of(rows: list[dict], rules: dict, instance: str, where: Where, ui: str = '', uid: str = '') -> list[dict]:
    found = []

    def card(row: dict, sub: str, sev: str, value: float, unit: str, label: str, what: str, d1=None, d7=None) -> None:
        sig = f'{ID}-{sub}'
        x = anomaly(ID, sig, '-', row['name'], '', sev, value, d1, d7, unit, label, what)
        x.update(key=f'{ID}|{instance}|{row["name"]}|{sub}', ranKey=ID)
        link = explore(ui, uid, where.expr(LINK[sig], row['name']))
        if link:
            x['urls'] = [{'label': 'Redis metric in Grafana', 'url': link}]
        found.append(x)

    for row in rows:
        name = row['name']
        if row['up'] < 0.5:
            card(row, 'down', 'crit', row['up'], '', 'Redis down',
                 f'Redis {name} is down: {where.prefix}up is 0, so its exporter cannot reach it. Clients that need it fail or fall back to their source.')
            continue
        mem, conn, rt = row.get('memoryPct'), row.get('clientsPct'), row.get('avgUs')
        if mem is not None and mem >= rules['mem_warn']:
            card(row, 'memory', 'crit' if mem >= rules['mem_crit'] else 'warn', mem, '%', 'Redis memory',
                 f'Redis {name} peaked at {mem:g} % of its memory limit in the last 30 min. Near the limit, writes fail or keys get evicted, depending on the '
                 'eviction policy; find the keys that grow (big caches without a TTL) before it fills.')
        if conn is not None and conn >= rules['conn_warn']:
            card(row, 'clients', 'warn', conn, '%', 'Redis clients',
                 f'Redis {name} peaked at {conn:g} % of its client limit in the last 30 min ({row.get("clients"):g} clients). A full limit refuses new connections, '
                 'so pods that start or scale out cannot reach Redis.')
        base = [v for v in (row.get('avgD1'), row.get('avgD7')) if v is not None]
        if rt is not None and base and rt >= rules['rt_floor_us'] and rt >= rules['rt_times'] * max(base):
            card(row, 'latency', 'warn', rt, 'us', 'Redis command time',
                 f'Redis {name} averaged {rt:g} us per command in the last 30 min against {row.get("avgD1")} us a day and {row.get("avgD7")} us a week earlier. '
                 'Redis runs commands on one thread, so slow commands (KEYS, big SCANs, large values) queue everything behind them.', row.get('avgD1'), row.get('avgD7'))
        for sub, label, text in (('evicted', 'Redis evictions', 'evicted keys: memory is full and Redis is dropping cached data'),
                                 ('rejected', 'Redis rejected connections', 'rejected connections: the client limit was hit')):
            if (row.get(sub) or 0) > 0:
                card(row, sub, 'warn', row[sub], '', label, f'Redis {name} reported {row[sub]:g} {text} in the last 30 min.')
        if row.get('uptime') is not None and row['uptime'] < rules['uptime_s']:
            card(row, 'restart', 'warn', row['uptime'], '', 'Redis restart',
                 f'Redis {name} has been up for only {row["uptime"]:g} s: it restarted, so clients reconnected and an in-memory cache may start cold.')
    return found


def refused(e: SourceError) -> dict:
    return access.card(ID, NAME, PART, e.right, status=e.code, name=PART_NAME)


def run(ctx: Context) -> Sweep:
    where = Where(ctx)
    bad = where.problem()
    if bad:
        return Sweep(PassResult(ID, False, f'error: {bad}'))
    peer = ctx.peer('grafana')
    if peer is None:
        return Sweep(PassResult(ID, False, f'error: {NO_PEER}'))
    d = client_for(peer, where, bounded(peer.http(), BUDGET_S))
    reached = access.ran_key(ID, PART)
    try:
        rows = rows_of(d, where)
    except Late:
        return Sweep(PassResult(ID, False, f'error: {PART_NAME}: timed out'))
    except SourceError as e:
        if access.denied(e.code):
            return Sweep(PassResult(ID, True, access.status('ok', [PART_NAME]), [refused(e)], {}, (reached,)))
        return Sweep(PassResult(ID, False, f'error: {e.reason}'))
    except Exception as e:
        return Sweep(PassResult(ID, False, failed(e)))
    if not rows:
        return Sweep(PassResult(ID, False, f'error: datasource {d.name} holds no {where.prefix}up series'))
    found = cards_of(rows, {**RULES, **ctx.rules('rules')}, ctx.instance.name, where, peer.info.config.get('url') or '', d.found or '')
    summary = {'part': 'summary', 'at': iso(ctx.now), 'source': peer.info.name, 'datasource': d.name, 'prefix': where.prefix, 'label': where.label}
    return Sweep(PassResult(ID, True, 'ok', found, {}, (ID, reached)), sections={ID: [{'part': 'instance', **r} for r in rows] + [summary]})


def wanted(args: dict) -> dict:
    op = args.get('op') or 'instances'
    if op not in OPS:
        raise ReadError(400, f"op must be one of {', '.join(OPS)}")
    try:
        hours = int(args.get('hours') or 24)
    except (TypeError, ValueError):
        raise ReadError(400, f'hours must be 1-{HISTORY_HOURS}') from None
    if not 1 <= hours <= HISTORY_HOURS:
        raise ReadError(400, f'hours must be 1-{HISTORY_HOURS}')
    metric = args.get('metric') or 'memory'
    if op == 'history' and metric not in HISTORY:
        raise ReadError(400, f"metric must be one of {', '.join(HISTORY)}")
    name = str(args.get('name') or '').strip()
    if op == 'history' and not name:
        raise ReadError(400, 'op history needs name (a Redis instance as the label names it)')
    return {'op': op, 'hours': hours, 'metric': metric, 'name': name}


def history(d: Datasource, where: Where, w: dict, now: float) -> dict:
    step = max(60, w['hours'] * 3600 // HISTORY_POINTS)
    series = d.span(where.expr(HISTORY[w['metric']], w['name']), now - w['hours'] * 3600, now, step)
    points = [[iso(float(t)), float(v)] for s in series for t, v in (s.get('values') or [])]
    return {'name': w['name'], 'metric': w['metric'], 'stepSeconds': step, 'points': points[-HISTORY_POINTS:]}


def instances(d: Datasource, where: Where) -> dict:
    found = sorted(rows_of(d, where), key=lambda r: (r['up'] >= 0.5, r['name']))
    kept = found[:MAX_ROWS]
    more = f'first {len(kept)} of {len(found)} instances, down ones first; ' if len(kept) < len(found) else ''
    return {'instances': kept, 'note': more + INSTANCES_NOTE}


def read(ctx: Context, args: dict) -> object:
    where = Where(ctx)
    bad = where.problem()
    if bad:
        raise ReadError(400, bad)
    w = wanted(args)
    peer = ctx.peer('grafana')
    if peer is None:
        raise ReadError(503, NO_PEER)
    d = client_for(peer, where)
    try:
        return instances(d, where) if w['op'] == 'instances' else history(d, where, w, ctx.now)
    except SourceError as e:
        if access.denied(e.code):
            raise ReadError(e.code, access.text(NAME, PART_NAME, e.right, status=e.code)) from None
        raise ReadError(502, e.reason) from None


def test(ctx: Context) -> TestResult:
    def ask() -> str:
        where = Where(ctx)
        bad = where.problem()
        if bad:
            raise ReadError(400, bad)
        peer = ctx.peer('grafana')
        if peer is None:
            raise ReadError(400, NO_PEER)
        d = client_for(peer, where)
        try:
            found = d.vec(queries(where.prefix, where.label)['up'])
        except SourceError as e:
            said = access.tested(NAME, PART_NAME, e.code, e.right)
            raise ReadError(e.code or 502, said or e.reason) from None
        names = sorted({str(m.get(where.label)) for m, _ in found if m.get(where.label)})
        if not names:
            return f'Grafana {peer.info.name} answered, but datasource {d.name} holds no {where.prefix}up series'
        shown = ', '.join(names[:5]) + (f' and {len(names) - 5} more' if len(names) > 5 else '')
        return f"{len(names)} Redis instance{'' if len(names) == 1 else 's'} through Grafana {peer.info.name} ({d.name}): {shown}"

    return connection.result(ask)


def links(card: dict, info: InstanceInfo) -> list[dict] | None:
    if card.get('src') != ID:
        return None
    return [{'label': u['label'], 'url': u['url']} for u in card.get('urls') or []
            if isinstance(u, dict) and isinstance(u.get('label'), str) and isinstance(u.get('url'), str) and u['url'].startswith('https://')]


CARD_TYPES = (CardType('managed-redis-down', 'Managed Redis down'), CardType('managed-redis-memory', 'Managed Redis memory'),
              CardType('managed-redis-clients', 'Managed Redis clients'), CardType('managed-redis-latency', 'Managed Redis latency'),
              CardType('managed-redis-evicted', 'Managed Redis evictions'), CardType('managed-redis-rejected', 'Managed Redis rejections'),
              CardType('managed-redis-restart', 'Managed Redis restart'))

PLUGIN = Plugin(
    id=ID, name=NAME, version='0.1.0', category='infrastructure',
    description='Example plug-in: Redis instances you cannot reach directly, such as a managed cloud Redis, read from redis_exporter series through a Grafana datasource.',
    icon='icon.svg', author='Skopax', licence='Apache-2.0', core='>=0.1,<0.2', uses=('grafana',),
    fields=(Field('grafana', 'Grafana', 'instance', required=True, choices=('grafana',), help='The Grafana instance whose datasource holds the Redis series.'),
            Field('datasource', 'Datasource', 'text', required=True, help='The name of the Grafana Prometheus datasource that holds the Redis series.'),
            Field('prefix', 'Metric prefix', 'text', default=DEFAULT_PREFIX,
                  help='The start of every Redis metric name: redis_ for redis_exporter (redis_up, redis_connected_clients, ...). Change it only when your metrics are renamed.'),
            Field('label', 'Instance label', 'text', default=DEFAULT_LABEL, help='The label that names each Redis instance in the series.')),
    test=test,
    checks=(Check(ID, 'Managed Redis', 900, 600, (ID,), run, tool=TOOL, sections=(ID,), section_help=SECTION_HELP),),
    rules=(RuleSet('rules', 'Managed Redis thresholds', dict(RULES), RULE_HELP),),
    tools=(Tool(TOOL, DESCRIPTION, SCHEMA, read, per_run=6, scan=True),),
    links=links,
    card_types=CARD_TYPES,
)
