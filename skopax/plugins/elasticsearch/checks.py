import re
from collections.abc import Mapping

from skopax import severity
from skopax.contract import Context, InstanceInfo, PassResult, Sweep, anomaly, fmt, iso
from skopax.kit.access import SIG as ACCESS_SIG
from skopax.kit.elastic import (Budget, ElasticError, access_card, client_for, discover_for, epoch, index_of, indices_of, kql_levels, kql_text, level_filter, log_fields, namespace_problem,
                                no_access, quoted, refusal, refused_status, service_logs_link)
from skopax.kit import options
from skopax.kit.options import compiled, number, pairs

OFFS = (1, 7)
WARM_S = 1800
TOP = 30
APM_TOP = 50
SINCE = 'now-2h'
LINK_SPAN_S = 2 * 3600
REPORT_PAD_S = 300
REPORT_MIN_S = 1800
APM_RULES = {'p95_times': 2, 'p95_floor_ms': 200}
APM_HELP = {'p95_times': 'An APM p95 card opens when the p95 of the last hour passes this many times the higher of the 1 and 7 day baselines.',
            'p95_floor_ms': 'It also has to be at least this many milliseconds above that baseline.'}
LOG_HELP = {'logTop': 'ERROR and WARN log patterns of the last hour that are new, growing or loud, with their counts 1 and 7 days earlier'}
NO_DATA = 'apm:no-data'
LOGS_NO_DATA = 'logs:no-data'
LOGS_ACCESS = 'logs:access:logs'
APM_ACCESS = 'apm:access:apm'
ID_RUN = re.compile(r'[0-9a-f]{8}-[0-9a-f-]{4,}')
MASKED = '<masked>'
MASK_CHARS = 400
MASK_HINT = ' (check message-mask: Elasticsearch may not accept it)'
CLASSIC = 'classic'
OTEL = 'otel'
OTEL_PATTERN = re.compile(r'\.otel(?:-|\*|$)', re.I)
SHAPES = {CLASSIC: {'tx': 'processor.event == "transaction"', 'ms': 'transaction.duration.us / 1000.0', 'fail': 'event.outcome == "failure"', 'rq': 'transaction.type == "request"', 'svc': 'service.name'},
          OTEL: {'tx': '(kind IN ("Server", "Consumer") OR parent_span_id IS NULL OR parent_span_id == "")', 'ms': 'duration / 1000000.0', 'fail': 'status.code == "Error"', 'rq': 'kind == "Server"',
                 'svc': 'resource.attributes.service.name'}}
SHAPE_NAMES = {CLASSIC: 'classic APM', OTEL: 'OTel-native'}


def window() -> str:
    return ' OR '.join(['@timestamp >= NOW() - 60 minutes'] + [f'(@timestamp >= NOW() - {o} days - 60 minutes AND @timestamp < NOW() - {o} days)' for o in OFFS])


def literal(text: str) -> str:
    return '"' + text.replace('\\', '\\\\').replace('"', '\\"').replace('\n', '\\n').replace('\r', '\\r').replace('\t', '\\t') + '"'


def mask_problem(config: Mapping[str, str]) -> str | None:
    mask = str(config.get('message-mask') or '').strip()
    if not mask:
        return None
    found = compiled(mask)
    if found is None:
        return 'message-mask is not a valid pattern'
    if found.search('') is not None:
        return 'message-mask matches an empty text, so it would cut every line'
    return 'message-mask backtracks too much on some texts' if options.slow(found) else None


def mask_of(config: Mapping[str, str]) -> str:
    return '' if mask_problem(config) else str(config.get('message-mask') or '').strip()


def p95_overrides(config: Mapping[str, str]) -> tuple[dict[str, tuple[float | None, float | None]], list[str]]:
    found, bad = pairs(config.get('apm-p95-overrides'))
    out: dict[str, tuple[float | None, float | None]] = {}
    for service, spec in found.items():
        left, sep, right = spec.partition(':')
        times, floor = number(left) if left.strip() else None, number(right) if right.strip() else None
        if (left.strip() and times is None) or (right.strip() and floor is None) or (times is None and floor is None):
            bad.append(f'{service}={spec}')
        else:
            out[service] = (times, floor)
    return out, bad


def logs_query(config: Mapping[str, str], with_error: bool = True) -> str:
    named = log_fields(config)
    error = str(config.get('error-field') or '').strip() if with_error else ''
    text = f"COALESCE({quoted(error)}, {quoted(named['message'])})" if error else quoted(named['message'])
    mask = mask_of(config)
    if mask:
        text = f'REPLACE(SUBSTRING({text}, 1, {MASK_CHARS}), {literal(mask)}, "{MASKED}")'
    return f'''FROM {index_of(config, 'logs')}
| WHERE ({window()}) AND {level_filter(named['level'])}
| EVAL level = TO_UPPER({quoted(named['level'])})
| EVAL w = CASE(@timestamp >= NOW() - 60 minutes, "now", @timestamp >= NOW() - 2 days, "d1", "d7"), msg = SUBSTRING({text}, 1, 90)
| STATS now = COUNT(CASE(w == "now", 1, null)), d1 = COUNT(CASE(w == "d1", 1, null)), d7 = COUNT(CASE(w == "d7", 1, null)) BY app = {quoted(named['service'])}, level, msg
| EVAL base = GREATEST(d1, d7)
| WHERE (level == "ERROR" AND now >= 5 AND now > 2 * base + 3) OR (level == "WARN" AND now >= 30 AND now >= 5 * base) OR (level == "ERROR" AND now >= 30)
  OR (level == "WARN" AND now >= {severity.WARN_LOG_GROWTH_MIN} AND ((d1 > 0 AND now >= {severity.WARN_LOG_GROWTH_TIMES} * d1) OR (d7 > 0 AND now >= {severity.WARN_LOG_GROWTH_TIMES} * d7)))
| KEEP app, level, msg, now, d1, d7
| SORT now DESC | LIMIT {TOP}'''


def per_service(default: float, own: Mapping[str, float]) -> str:
    if not own:
        return f'{default:g}'
    cases = ', '.join(f'svc == {literal(s)}, {float(v)!r}' for s, v in own.items())
    return f'CASE({cases}, {float(default)!r})'


def shape_of(pattern: str) -> str:
    return OTEL if OTEL_PATTERN.search(pattern) else CLASSIC


def families(config: Mapping[str, str]) -> list[tuple[str, str]]:
    parts = indices_of(index_of(config, 'apm'))
    grouped = {shape: ','.join(p for p in parts if shape_of(p) == shape) for shape in (CLASSIC, OTEL)}
    return [(shape, index) for shape, index in grouped.items() if index]


def family_index(config: Mapping[str, str], shape: str) -> str:
    return dict(families(config)).get(shape) or index_of(config, 'apm')


def apm_query(config: Mapping[str, str], rules: Mapping[str, float], shape: str = CLASSIC, index: str | None = None) -> str:
    times, floor = rules.get('p95_times', APM_RULES['p95_times']), rules.get('p95_floor_ms', APM_RULES['p95_floor_ms'])
    over = p95_overrides(config)[0]
    times, floor = per_service(times, {s: t for s, (t, _) in over.items() if t is not None}), per_service(floor, {s: f for s, (_, f) in over.items() if f is not None})
    named = SHAPES[shape]
    return f'''FROM {index or family_index(config, shape)}
| WHERE {named['tx']} AND ({window()})
| EVAL w = CASE(@timestamp >= NOW() - 60 minutes, "now", @timestamp >= NOW() - 2 days, "d1", "d7"), ms = {named['ms']}, f = CASE({named['fail']}, 1, 0), rq = {named['rq']}
| STATS n_now = COUNT(CASE(w == "now", 1, null)), n_d1 = COUNT(CASE(w == "d1", 1, null)), n_d7 = COUNT(CASE(w == "d7", 1, null)), r_now = COUNT(CASE(w == "now" AND rq, 1, null)), r_d1 = COUNT(CASE(w == "d1" AND rq, 1, null)), r_d7 = COUNT(CASE(w == "d7" AND rq, 1, null)), p95_now = PERCENTILE(CASE(w == "now" AND rq, ms, null), 95), p95_d1 = PERCENTILE(CASE(w == "d1" AND rq, ms, null), 95), p95_d7 = PERCENTILE(CASE(w == "d7" AND rq, ms, null), 95), f_now = SUM(CASE(w == "now", f, 0)), f_d1 = SUM(CASE(w == "d1", f, 0)), f_d7 = SUM(CASE(w == "d7", f, 0)) BY svc = {named['svc']}
| WHERE n_now >= 100 AND n_d1 >= 50 AND n_d7 >= 50
| EVAL p95_base = GREATEST(p95_d1, p95_d7), fr_now = f_now * 1.0 / n_now, fr_base = GREATEST(f_d1 * 1.0 / n_d1, f_d7 * 1.0 / n_d7)
| EVAL slow = r_now >= 100 AND r_d1 >= 50 AND r_d7 >= 50 AND p95_now > {times} * p95_base AND p95_now - p95_base >= {floor}, failing = f_now >= 10 AND fr_now > 2 * fr_base + 0.005
| WHERE slow OR failing
| KEEP svc, n_now, r_now, p95_now, p95_base, f_now, fr_now, fr_base, slow, failing
| LIMIT {APM_TOP}'''


def evaluate_logs(rows: list[dict] | None) -> tuple[list[dict], list[dict]]:
    found, top = [], []
    for r in rows or []:
        if not isinstance(r, dict):
            continue
        app, level, msg = r.get('app') or '-', r.get('level') or '', (r.get('msg') or '').replace('\n', ' ')
        now, d1, d7 = r.get('now') or 0, r.get('d1') or 0, r.get('d7') or 0
        base = max(d1, d7)
        top.append(dict(app=app, level=level, msg=msg, now=now, d1=d1, d7=d7))
        error_rule = level == 'ERROR'
        flagged = (error_rule and now >= 5 and now > 2 * base + 3) or (level == 'WARN' and ((now >= 30 and now >= 5 * max(base, 1)) or severity.warn_log(now, d1, d7) == severity.HIGH))
        if not flagged:
            continue
        if error_rule:
            sev = 'crit' if base == 0 and now >= 50 else 'high'
        else:
            sev = severity.warn_log(now, d1, d7) if now >= 50 else 'info'
        kind = 'new' if base == 0 else 'growing'
        x = anomaly('logs', 'log', '-', app, '', sev, now, d1, d7, '', f'{level} pattern',
                    f'{kind} {level} pattern, {now} in the last 60 min against {d1} a day ago and {d7} a week ago: {msg}', 5)
        x['key'] = f'log|{app}|{level}|' + ID_RUN.sub('<id>', msg)
        x['msg'] = msg
        x['short'] = f'{sev.upper()} {app} {kind} {level} \u00d7{now}: {msg[:50]}'
        found.append(x)
        top[-1]['flag'] = True
    return found, top


def evaluate_apm(rows: list[dict] | None, warm: set[tuple[str, str]], rules: Mapping[str, float], overrides: Mapping[str, tuple[float | None, float | None]] | None = None) -> list[dict]:
    found = []
    warm_names = {svc for _, svc in warm}
    for r in rows or []:
        if not isinstance(r, dict):
            continue
        svc = str(r.get('svc') or '-')
        own_times, own_floor = (overrides or {}).get(svc, (None, None))
        factor = own_times if own_times is not None else rules.get('p95_times', APM_RULES['p95_times'])
        floor = own_floor if own_floor is not None else rules.get('p95_floor_ms', APM_RULES['p95_floor_ms'])
        if svc.replace('_', '-') in warm_names:
            continue
        base = r.get('p95_base') or 0
        now = r.get('p95_now') or 0
        if r.get('slow') and now > factor * base and now - base >= floor:
            x = anomaly('apm', 'apm-p95', '-', svc, '', 'info', now, r.get('p95_base'), None, 'ms', 'APM p95',
                        f'APM p95 {fmt(now, "ms")} over {r.get("r_now")} sampled HTTP requests in the last 60 min, the baseline is {fmt(base, "ms")} '
                        f'(request transactions only, messaging and scheduled work left out; info only)', 200)
            x['key'] = f'apm|{svc}|p95'
            found.append(x)
        if r.get('failing'):
            x = anomaly('apm', 'apm-fail', '-', svc, '', 'high', r.get('f_now') or 0, None, None, '', 'failed transactions',
                        f'{r.get("f_now")} failed transactions ({r.get("fr_now") or 0:.2%}) in the last 60 min, the baseline share is {r.get("fr_base") or 0:.2%}', 10)
            x['key'] = f'apm|{svc}|fail'
            found.append(x)
    return found


def warm_services(deploys: list) -> set[tuple[str, str]]:
    return {(d['ns'], d['svc']) for d in deploys or [] if isinstance(d, dict) and isinstance(d.get('ns'), str) and isinstance(d.get('svc'), str) and d['ns'] and d['svc']}


def needle(msg: str, masked: bool = False) -> str:
    cut = re.split(r'["\\\n]|[0-9a-f]{8}-' + (f'|{MASKED}' if masked else ''), msg or '')[0]
    if len(cut) > 60:
        cut = re.sub(r'\w+$', '', cut[:60]) if re.fullmatch(r'\w\w', cut[59:61]) else cut[:60]
    return cut.strip(' .,:;=(-')


def log_lines_link(config: Mapping[str, str], app: str, level: str, msg: str, since: str) -> str:
    named = log_fields(config)
    error = str(config.get('error-field') or '').strip()
    n = needle(msg, bool(mask_of(config)))
    lines = ' or '.join(f'{field}:{kql_text(n)}' for field in dict.fromkeys(x for x in (named['message'], error) if x))
    cond = f' and ({lines})' if n else ''
    return discover_for(config, f"{named['service']}:{kql_text(app)} and {kql_levels(named['level'], (level,))}{cond}", since)


def links(card: dict, info: InstanceInfo) -> list[dict] | None:
    config = info.config
    if not str(config.get('ui-url') or '') or card.get('sig') == ACCESS_SIG or str(card.get('sig') or '').endswith('-fields'):
        return None
    svc, ns = str(card.get('svc') or ''), str(card.get('ns') or '')
    if card.get('src') == 'logflow':
        return [dict(label='Log lines in Kibana', url=discover_for(config, '*', SINCE))]
    if card.get('src') == 'logs' and card.get('sig') == 'log':
        parts = str(card.get('key') or '').split('|')
        level = parts[2] if len(parts) > 2 else 'ERROR'
        return [dict(label='Log lines in Kibana', url=log_lines_link(config, svc, level, str(card.get('msg') or ''), SINCE)),
                dict(label=f'All {svc} errors in Kibana', url=service_logs_link(config, svc, SINCE))]
    if card.get('src') == 'apm':
        return [dict(label=f'{svc} errors in Kibana', url=service_logs_link(config, svc, SINCE))]
    if svc and ns and ns != '-' and '/' not in svc:
        return [dict(label='Logs', url=service_logs_link(config, svc, SINCE))]
    return None


def row_links(row: dict, span: dict, info: InstanceInfo) -> dict[str, str]:
    config = info.config
    svc = str(row.get('svc') or '')
    start, end = epoch(span.get('from')), epoch(span.get('to'))
    if not str(config.get('ui-url') or '') or not svc or '/' in svc or start is None or end is None or end < start:
        return {}
    until = max(end, start + REPORT_MIN_S) + REPORT_PAD_S
    return {'logs': service_logs_link(config, svc, iso(start - REPORT_PAD_S), iso(until))}


def apm_count(config: Mapping[str, str], shape: str = CLASSIC, index: str | None = None) -> str:
    return f'FROM {index or family_index(config, shape)}\n| WHERE {SHAPES[shape]["tx"]} AND @timestamp >= NOW() - 60 minutes\n| STATS n = COUNT(*)'


def svc_of(row: Mapping[str, object]) -> str:
    return str(row.get('svc') or '-')


def merged(answers: list[list[dict]]) -> list[dict]:
    if len(answers) < 2:
        return [r for rows in answers for r in rows if isinstance(r, dict)]
    out = []
    for signal, other in (('slow', 'failing'), ('failing', 'slow')):
        sizes = [{} for _ in answers]
        for i, rows in enumerate(answers):
            for r in rows:
                if isinstance(r, dict) and r.get(signal):
                    sizes[i][svc_of(r)] = max(sizes[i].get(svc_of(r), 0), r.get('n_now') or 0)
        owner = {}
        for i, sized in enumerate(sizes):
            for svc, n in sized.items():
                if svc not in owner or n > sizes[owner[svc]][svc]:
                    owner[svc] = i
        out += [dict(r, **{other: False}) for i, rows in enumerate(answers) for r in rows if isinstance(r, dict) and r.get(signal) and owner[svc_of(r)] == i]
    return out


def run_logs(ctx: Context) -> Sweep:
    config = ctx.instance.config
    error = str(config.get('error-field') or '').strip()
    budget = Budget()
    client = client_for(ctx.instance, ctx.http())
    index = index_of(config, 'logs')
    try:
        problem = namespace_problem(config)
        if problem:
            raise ElasticError(problem)
        try:
            rows = client.query(logs_query(config), timeout=budget.left())
        except ElasticError as e:
            if not error or error not in e.unknown:
                raise
            rows = client.query(logs_query(config, with_error=False), timeout=budget.left())
        found, top = evaluate_logs(rows)
    except ElasticError as e:
        if refusal(client, e, indices_of(index), budget):
            return no_access('logs', access_card('logs', 'Elasticsearch', 'logs', index, refused_status(e), bool(client.api_key)), {'logTop': []})
        if e.missing:
            return Sweep(PassResult('logs', True, 'ok \u00b7 no data', [], {}, (LOGS_NO_DATA, LOGS_ACCESS)), sections={'logTop': []})
        hint = MASK_HINT if mask_of(config) and e.status == 400 and not e.unknown else ''
        return Sweep(PassResult('logs', False, f'error: {e.reason}{hint}'))
    since = iso(ctx.now - LINK_SPAN_S)
    for r in top:
        link = log_lines_link(config, r['app'], r['level'], r['msg'], since)
        if link:
            r['link'] = link
    return Sweep(PassResult('logs', True, 'ok', found, {}, ('logs', LOGS_ACCESS)), sections={'logTop': top})


def run_apm(ctx: Context) -> Sweep:
    config = ctx.instance.config
    rules = ctx.rules('apm')
    warm = warm_services(ctx.deploys(ctx.now - WARM_S))
    budget = Budget()
    client = client_for(ctx.instance, ctx.http(), 'apm')
    quiet = Sweep(PassResult('apm', True, 'ok \u00b7 no data', [], {}, (NO_DATA, APM_ACCESS)))
    answers: list[list[dict]] = []
    absent: list[tuple[str, ElasticError]] = []
    live = False
    for shape, index in families(config):
        try:
            rows = client.query(apm_query(config, rules, shape, index), timeout=budget.left())
            counted = None if rows else client.query(apm_count(config, shape, index), timeout=budget.left())
        except ElasticError as e:
            if e.denied or e.missing or e.unknown:
                absent.append((index, e))
                continue
            return Sweep(PassResult('apm', False, f'error: {e.reason}'))
        answers.append(rows)
        live = live or bool(rows) or any(isinstance(r, dict) and r.get('n') for r in counted or [])
    if not live:
        denied = [(index, e) for index, e in absent if e.denied]
        hidden = [] if answers else [(index, e) for index, e in absent if not e.denied and refusal(client, e, indices_of(index), budget)]
        if denied or (hidden and len(hidden) == len(absent)):
            refused = denied + hidden
            return no_access('apm', access_card('apm', 'Elasticsearch', 'apm', ','.join(index for index, _ in refused), refused_status(refused[0][1]), bool(client.api_key)))
        return quiet
    return Sweep(PassResult('apm', True, 'ok', evaluate_apm(merged(answers), warm, rules, p95_overrides(config)[0]), {}, ('apm', APM_ACCESS)))
