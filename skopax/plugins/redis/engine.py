import json
import re
from collections.abc import Mapping

from skopax.contract import anomaly
from skopax.kit import access
from skopax.kit.options import pairs
from skopax.kit.promql import Prometheus, graph

ID = 'redis'
WINDOW = '30m'
APP_ROWS = 25
EXPORTER_ROWS = 100
BUDGET_S = 200
RULES = {'app_times': 3, 'app_floor_ms': 5, 'app_min_calls': 300, 'app_errors': 10}
OPS = ('instances', 'apps')
BY = 'namespace, job, net_sock_peer_addr'
ORDINAL = re.compile(r'-\d+$')
ARROW = ' \u2192 '
GRAPH = 'Prometheus graph'


def matchers(selector: str, *more: str) -> str:
    given = (selector or '').strip().strip('{}').strip()
    parts = [p for p in (given, *more) if p]
    return '{' + ','.join(parts) + '}' if parts else ''


def peer_names(raw: object) -> tuple[dict[str, str], list[str]]:
    found, bad = pairs(raw)
    return {k.lower(): v for k, v in found.items()}, bad


def friendly(addr: object, names: Mapping[str, str]) -> object:
    if not names or not isinstance(addr, str) or not addr:
        return addr
    lowered = addr.lower()
    return names.get(lowered) or names.get(lowered.rsplit(':', 1)[0] if lowered.count(':') == 1 else lowered) or addr


def place(ns: str) -> str:
    return f' in namespace {ns}' if ns != '-' else ''


def in_namespace(ns: str) -> tuple[str, ...]:
    return (f'namespace={json.dumps(ns)}',) if ns != '-' else ()


def exporter_rows(p: Prometheus, selector: str, now: float) -> list[dict]:
    sel = matchers(selector)
    found: dict[tuple[str, str], dict] = {}

    def row(m: dict) -> dict | None:
        name = m.get('pod') or m.get('instance')
        if not name:
            return None
        ns = m.get('namespace') or '-'
        return found.setdefault((ns, name), {'ns': ns, 'name': name})

    def put(expr: str, key: str, digits: int = 3) -> None:
        for m, v in p.vec(expr, now):
            r = row(m)
            if r is not None:
                r[key] = round(v, digits)

    put(f'redis_up{sel}', 'up', 0)
    for m, _ in p.vec(f'redis_instance_info{sel}', now):
        r = row(m)
        if r is not None:
            r.update(role=m.get('role'), version=m.get('redis_version'))
    put(f'redis_connected_slaves{sel}', 'replicas', 0)
    put(f'redis_memory_used_bytes{sel}', 'memoryBytes', 0)
    put(f'redis_connected_clients{sel}', 'clients', 0)
    put(f'rate(redis_commands_processed_total{sel}[{WINDOW}])', 'ops', 2)
    put(f'increase(redis_rejected_connections_total{sel}[{WINDOW}])', 'rejected', 0)
    put(f'increase(redis_evicted_keys_total{sel}[{WINDOW}])', 'evicted', 0)
    return sorted(found.values(), key=lambda x: (x['ns'], x['name']))


def capped(rows: list[dict]) -> list[dict]:
    if len(rows) <= EXPORTER_ROWS:
        return rows
    first = sorted(rows, key=lambda r: (r.get('up') != 0, bool(r.get('role'))))[:EXPORTER_ROWS]
    return sorted(first, key=lambda x: (x['ns'], x['name']))


def app_rows(p: Prometheus, selector: str, now: float, names: Mapping[str, str] | None = None) -> list[dict]:
    sel, failing = matchers(selector), matchers(selector, 'error!="none"')
    rows: dict[tuple, dict] = {}

    def put(expr: str, key: str, digits: int = 3) -> None:
        for m, v in p.vec(expr, now):
            if m.get('job'):
                k = (m.get('namespace') or '-', m['job'], m.get('net_sock_peer_addr'))
                rows.setdefault(k, {'ns': k[0], 'job': k[1], 'redis': friendly(k[2], names or {})})[key] = round(v, digits)

    for label, offset in (('ms', ''), ('d1', ' offset 1d'), ('d7', ' offset 7d')):
        put(f'1000 * sum by ({BY}) (rate(lettuce_seconds_sum{sel}[{WINDOW}]{offset})) / sum by ({BY}) (rate(lettuce_seconds_count{sel}[{WINDOW}]{offset}))', label)
    put(f'sum by ({BY}) (increase(lettuce_seconds_count{sel}[{WINDOW}]))', 'calls', 0)
    put(f'sum by ({BY}) (increase(lettuce_seconds_count{failing}[{WINDOW}]))', 'errors', 0)
    return sorted(rows.values(), key=lambda x: -(x.get('calls') or 0))


def set_name(group: list[dict]) -> str:
    names = {ORDINAL.sub('', r['name']) for r in group}
    return names.pop() if len(names) == 1 else ID


def exporter_cards(rows: list[dict]) -> list[dict]:
    found = []

    def card(ns: str, svc: str, sig: str, sub: str, sev: str, value: float, label: str, what: str) -> None:
        x = anomaly(ID, sig, ns, svc, sub, sev, value, None, None, '', label, what)
        x.update(ranKey='redis:exporter', redis='exporter')
        found.append(x)

    for r in rows:
        if r.get('up') == 0:
            card(r['ns'], r['name'], 'redis-down', 'up', 'crit', 0, 'Redis down', f"The Redis exporter cannot reach Redis on {r['name']}{place(r['ns'])}.")
    for ns in sorted({r['ns'] for r in rows}):
        group = [r for r in rows if r['ns'] == ns]
        if not any(r.get('role') for r in group):
            continue
        svc = set_name(group)
        masters = [r for r in group if r.get('role') == 'master']
        if not masters:
            card(ns, svc, 'redis-master', 'master', 'crit', 0, 'Redis has no master', f'No Redis{place(ns)} reports the master role, so writes to it fail until a replica is promoted.')
            continue
        replicas, wanted = sum(m.get('replicas') or 0 for m in masters), len(group) - len(masters)
        if replicas < wanted:
            what = (f"The Redis master {masters[0]['name']}{place(ns)} has {replicas:g} connected replicas of {wanted}" if len(masters) == 1 else
                    f'The {len(masters)} Redis masters{place(ns)} have {replicas:g} connected replicas in total for {wanted} replica instances')
            card(ns, svc, 'redis-repl', 'replicas', 'warn', replicas, 'Redis replicas', f'{what}; a failover now could lose writes.')
    for r in rows:
        for key, sig, label, text in (('evicted', 'redis-evict', 'Redis evictions', 'evicted keys: memory is full and Redis is dropping cached data'),
                                      ('rejected', 'redis-reject', 'Redis rejected connections', 'rejected connections: the connection limit was hit')):
            if (r.get(key) or 0) > 0:
                card(r['ns'], r['name'], sig, key, 'warn', r[key], label, f"Redis {r['name']}{place(r['ns'])} had {r[key]:g} {text} in the last 30 min.")
    return found


def app_cards(rows: list[dict], rules: Mapping[str, float]) -> list[dict]:
    found = []
    for row in rows:
        ms, calls = row.get('ms'), row.get('calls') or 0
        base = max([v for v in (row.get('d1'), row.get('d7')) if v is not None] or [0])
        where = f"{row['job']} ({row['ns']}){ARROW}{row.get('redis') or 'Redis'}"
        if ms is not None and base and calls >= rules['app_min_calls'] and ms >= rules['app_floor_ms'] and ms >= rules['app_times'] * base:
            x = anomaly(ID, 'redis-app', row['ns'], row['job'], 'latency', 'warn', round(ms, 2), row.get('d1'), row.get('d7'), 'ms', 'Redis call time',
                        f"{where}: Redis calls took {ms:.2f} ms on average over {int(calls):,} calls in the last 30 min, against {row.get('d1')} ms a day and {row.get('d7')} ms a week earlier "
                        '(measured in the service, so network and pool waits count too).')
            x.update(ranKey='redis:app', redis='app')
            found.append(x)
        if (row.get('errors') or 0) >= rules['app_errors']:
            x = anomaly(ID, 'redis-app-err', row['ns'], row['job'], 'errors', 'warn', row['errors'], None, None, '', 'Redis call errors',
                        f"{where}: {int(row['errors']):,} of {int(calls):,} Redis calls failed in the last 30 min.")
            x.update(ranKey='redis:app', redis='app')
            found.append(x)
    return found


def call_graph(ns: str, job: str, selector: str) -> str:
    where = matchers(selector, *in_namespace(ns), f'job={json.dumps(job)}')
    return f'1000 * sum(rate(lettuce_seconds_sum{where}[5m])) / sum(rate(lettuce_seconds_count{where}[5m]))'


def exporter_graph(card: dict, selector: str) -> str:
    where = matchers(selector, *in_namespace(card['ns']))
    return {'redis-repl': f'redis_connected_slaves{where}', 'redis-evict': f'increase(redis_evicted_keys_total{where}[{WINDOW}])',
            'redis-reject': f'increase(redis_rejected_connections_total{where}[{WINDOW}])'}.get(card['sig'], f'redis_up{where}')


def card_urls(card: dict, ui: str, selector: str) -> list[dict]:
    expr = exporter_graph(card, selector) if card.get('redis') == 'exporter' else call_graph(card['ns'], card['svc'], selector)
    url = graph(expr, ui)
    return [{'label': GRAPH, 'url': url}] if url else []


def status_of(errors: list[str], empty: list[str], ran: list[str], refused: list[str] = ()) -> str:
    if errors:
        return access.status('error: ' + '; '.join(errors) + (f" \u00b7 no data for {', '.join(empty)}" if empty else ''), refused)
    if not ran and not refused:
        return 'ok \u00b7 no data'
    return access.status(f"ok \u00b7 no data for {', '.join(empty)}" if empty else 'ok', refused)


def links(card: dict, info: object) -> list[dict] | None:
    if card.get('src') != ID:
        return None
    return [{'label': u['label'], 'url': u['url']} for u in card.get('urls') or []
            if isinstance(u, dict) and isinstance(u.get('label'), str) and isinstance(u.get('url'), str) and u['url'].startswith('https://')]
