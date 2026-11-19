from collections.abc import Mapping

from skopax.contract import Context, PassResult, Sweep, anomaly, iso
from skopax.kit.elastic import (Budget, Elastic, ElasticError, access_card, client_for, epoch, fields_card, index_of, indices_of, namespace_of, namespace_problem, no_access, quoted, refusal,
                                refused_status)

WINDOW_MIN = 20
RULES = {'floor': 200, 'lag_warn_min': 5, 'lag_crit_min': 15, 'all_drop': 0.2}
HELP = {'floor': 'A namespace is watched only when both earlier windows had at least this many log lines; a watched one with no lines raises a warning.',
        'lag_warn_min': 'Warning when the newest log line of a busy namespace is this many minutes old.',
        'lag_crit_min': 'Critical when it is this many minutes old.',
        'all_drop': 'Critical when the log lines of all namespaces together fall below this share of both earlier windows.'}
SECTION_HELP = {'logflow': 'Log lines per namespace in the last 20 minutes against the same window 1 and 7 days earlier, and the age of the newest line'}
NO_DATA = 'logflow:no-data'
FIELDS_KEY = 'logflow:fields'
ACCESS = 'logflow:access:logs'
NO_SOURCE = 'log flow needs index-namespace or namespace-field'
PIPELINE = ('Check the log shipping path (agents, queue and ingest) and the disk of the Elasticsearch cluster, which makes its indices read-only at 95 % full. '
            'Until it catches up every log-based card is late or missing.')


def by_index(config: Mapping[str, str]) -> bool:
    return bool(str(config.get('index-namespace') or '').strip())


def namespace_field(config: Mapping[str, str]) -> str:
    return str(config.get('namespace-field') or '').strip()


def window_query(config: Mapping[str, str], back_days: int, newest: bool) -> str:
    shift = f' - {back_days} days' if back_days else ''
    upper = f' AND @timestamp < NOW(){shift}' if back_days else ''
    stats = 'n = COUNT(*), newest = MAX(@timestamp)' if newest else 'n = COUNT(*)'
    source = f"FROM {index_of(config, 'logflow')}" + (' METADATA _index' if by_index(config) else '')
    group = '_index' if by_index(config) else quoted(namespace_field(config))
    return f'{source}\n| WHERE @timestamp >= NOW(){shift} - {WINDOW_MIN} minutes{upper}\n| STATS {stats} BY {group}'


def count(value: object) -> int:
    return int(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else 0


def by_ns(rows: list[dict], config: Mapping[str, str]) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for r in rows:
        if not isinstance(r, dict):
            continue
        ns = namespace_of(r, config)
        if ns is None:
            continue
        x = out.setdefault(ns, {'n': 0, 'newest': None})
        x['n'] += count(r.get('n'))
        newest = r.get('newest')
        if isinstance(newest, str) and newest and (x['newest'] is None or newest > x['newest']):
            x['newest'] = newest
    return out


def evaluate(cur: dict, d1: dict, d7: dict, now: float, rules: Mapping[str, float]) -> tuple[list[dict], list[dict], dict]:
    rows, found = [], []
    for ns in sorted(set(cur) | set(d1) | set(d7)):
        c = cur.get(ns) or {}
        r = {'ns': ns, 'n': c.get('n', 0), 'd1': (d1.get(ns) or {}).get('n', 0), 'd7': (d7.get(ns) or {}).get('n', 0)}
        at = epoch(c.get('newest'))
        if at is not None:
            r['newest'] = c['newest']
            r['lagMin'] = round(max(0.0, now - at) / 60, 1)
        rows.append(r)
        if r['n'] == 0 and min(r['d1'], r['d7']) >= rules['floor']:
            x = anomaly('logflow', 'logs-stopped', ns, 'logs', 'flow', 'warn', 0, r['d1'], r['d7'], '', 'Logs stopped arriving',
                        f'No log line from {ns} reached Elasticsearch in the last {WINDOW_MIN} min, against {r["d1"]:,} in the same window a day and {r["d7"]:,} a week earlier. '
                        'Either every pod of the namespace stopped logging (scaled down, crash loop before the first line) or its shipping broke; '
                        'until it is back the log cards of that namespace see nothing.', 1)
            x['ranKey'] = 'logflow'
            found.append(x)
    total = {k: sum(r[k] for r in rows) for k in ('n', 'd1', 'd7')}
    lags = [r['lagMin'] for r in rows if r.get('lagMin') is not None and min(r['d1'], r['d7']) >= rules['floor']]
    lag = min(lags) if lags else None
    stalled = total['n'] < rules['all_drop'] * min(total['d1'], total['d7']) and min(total['d1'], total['d7']) >= rules['floor']
    if stalled or (lag is not None and lag >= rules['lag_warn_min']):
        sev = 'crit' if stalled or lag >= rules['lag_crit_min'] else 'warn'
        short = f'{sev.upper()} log pipeline: ' + (f'{total["n"]:,} lines in {WINDOW_MIN} min (1d {total["d1"]:,} / 7d {total["d7"]:,})' if stalled else f'newest line {lag:g} min old')
        x = anomaly('logflow', 'logs-lag', '-', 'pipeline', 'lag', sev, total['n'] if stalled else lag, None, None, '', 'Log pipeline behind',
                    (f'Only {total["n"]:,} log lines reached Elasticsearch in the last {WINDOW_MIN} min, against {total["d1"]:,} a day and {total["d7"]:,} a week earlier. ' if stalled else
                     f'The freshest log line of even the busiest namespace is {lag:g} min old. ') + PIPELINE, 1, short)
        x['ranKey'] = 'logflow'
        found.append(x)
    return rows, found, {'at': iso(now), 'windowMin': WINDOW_MIN, 'lagMin': lag, 'total': total}


def quiet(found: list[dict] | None = None) -> Sweep:
    return Sweep(PassResult('logflow', True, 'ok \u00b7 no data', found or [], {}, (NO_DATA, FIELDS_KEY, ACCESS)), sections={'logflow': []})


def sweep(es: Elastic, config: Mapping[str, str], now: float, rules: Mapping[str, float], budget: Budget | None = None) -> Sweep:
    budget = budget or Budget()
    index = index_of(config, 'logflow')
    field = '' if by_index(config) else namespace_field(config)
    try:
        problem = namespace_problem(config)
        if problem:
            raise ElasticError(problem)
        if not by_index(config) and not field:
            raise ElasticError(NO_SOURCE)
        cur = by_ns(es.query(window_query(config, 0, True), timeout=budget.left()), config)
        d1 = by_ns(es.query(window_query(config, 1, False), timeout=budget.left()), config)
        d7 = by_ns(es.query(window_query(config, 7, False), timeout=budget.left()), config)
    except ElasticError as e:
        if refusal(es, e, indices_of(index), budget):
            return no_access('logflow', access_card('logflow', 'Elasticsearch', 'logs', index, refused_status(e), bool(es.api_key)), {'logflow': []})
        if e.missing:
            return quiet()
        if field and field in e.unknown:
            return quiet([fields_card('logflow', 'logflow', 'Elasticsearch', '-', 'logs', (field,), index)])
        return Sweep(PassResult('logflow', False, f'error: {e.reason}'))
    if not (cur or d1 or d7):
        return quiet()
    rows, found, summary = evaluate(cur, d1, d7, now, rules)
    section = [{'part': 'namespace', **r} for r in rows] + [{'part': 'summary', **summary}]
    return Sweep(PassResult('logflow', True, 'ok', found, {}, ('logflow', FIELDS_KEY, ACCESS)), sections={'logflow': section})


def run_logflow(ctx: Context) -> Sweep:
    return sweep(client_for(ctx.instance, ctx.http()), ctx.instance.config, ctx.now, ctx.rules('logflow'))
