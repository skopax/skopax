import datetime as dt

from skopax import clock
from skopax.contract import anomaly, iso
from skopax.kit import access as kit_access
from skopax.kit.text import cut
from skopax.monitor import above, below_both, pct
from skopax.plugins.langfuse.client import PERMISSION, Langfuse, LangfuseError, day_of, level_rows, measure, query, whole

CHECK = 'langfuse'
SECTION = 'langfuseCheck'
HOUR = 3600
DAY = 86400
TOP = 3
MESSAGE_LIMIT = 160
BUDGET_S = 200
LEVELS = ('ERROR', 'WARNING')
PARTS = ('turns', 'steps', 'daily costs')
COVERS = {'metrics': 'step errors, traffic, turn time or cost', 'observations': 'which steps failed (the step-error card still counts them)'}
TIMES = '\u00d7'
BOTH = 'the same window 1 day and 7 days earlier'
RULES = {'errors_warn_times': 3, 'errors_crit_times': 10, 'errors_gap': 5, 'errors_floor': 5, 'errors_crit_floor': 20,
         'traffic_warn': 0.3, 'traffic_crit': 0.05, 'traffic_floor': 20,
         'latency_times': 1.5, 'latency_gap_s': 5, 'latency_min_turns': 20,
         'turn_cost_times': 1.6, 'turn_cost_gap_usd': 0.01, 'turn_cost_min_turns': 20,
         'day_cost_times': 1.5, 'day_cost_gap_usd': 5, 'day_cost_floor_usd': 5}
RULE_HELP = {
    'errors_warn_times': f'Warning when the error and warning steps of the last hour pass this many times the higher of {BOTH}.',
    'errors_crit_times': 'Critical when they pass this many times the higher earlier window.',
    'errors_gap': 'And at least this many steps more than the higher earlier window, so a jump from 1 to 4 raises nothing.',
    'errors_floor': 'A warning needs at least this many error and warning steps in the last hour.',
    'errors_crit_floor': 'The critical level needs at least this many.',
    'traffic_warn': f'Warning when the turns of the last hour fall below this share of {BOTH} (0.3 = under a third of both).',
    'traffic_crit': 'Critical when they fall below this share of both (0.05 = nearly none: an outage or tracing stopped).',
    'traffic_floor': 'Traffic is checked only when both earlier windows had at least this many turns, so quiet night hours raise nothing.',
    'latency_times': 'Warning when the p95 turn time passes this many times the higher earlier p95.',
    'latency_gap_s': 'And is at least this many seconds slower than it.',
    'latency_min_turns': 'Latency is checked only when the last hour has at least this many turns.',
    'turn_cost_times': 'Warning when the average cost of a turn passes this many times the higher earlier average.',
    'turn_cost_gap_usd': 'And is at least this many dollars more per turn.',
    'turn_cost_min_turns': 'Turn cost is checked only when the last hour has at least this many turns.',
    'day_cost_times': 'Warning when yesterday\'s total passes this many times the daily average of the 7 days before.',
    'day_cost_gap_usd': 'And is at least this many dollars more than that average.',
    'day_cost_floor_usd': 'Yesterday is checked only when it cost at least this many dollars.',
}


def window(lf: Langfuse, start: float, end: float, listed: bool, access: dict[str, int | None], failed: dict[str, str] | None = None) -> dict:
    s, e = iso(start), iso(end)
    env = lf.env_metric()
    turns = lf.metric(query(s, e, [('count', 'count'), ('p95', 'latency')], [lf.turn_filter()] + env))
    n = (whole(measure(turns[0], 'count', 'count')) or 0) if turns else 0
    p95 = measure(turns[0], 'p95', 'latency') if turns and n else None
    errors = {level: 0 for level in LEVELS}
    steps = 0
    for r in lf.metric(query(s, e, [('count', 'count')], env, dimensions=('level',))):
        counted = whole(measure(r, 'count', 'count')) or 0
        steps += counted
        if r.get('level') in errors:
            errors[r['level']] = counted
    spent = lf.metric(query(s, e, [('sum', 'totalCost')], env))
    cost = measure(spent[0], 'sum', 'totalCost') if spent else None
    out = {'traces': n, 'steps': steps, 'errors': errors, 'p95': round(p95 / 1000, 3) if p95 is not None else None, 'turnCost': cost / n if cost is not None and n else None}
    if listed and sum(errors.values()):
        try:
            rows = level_rows(lf, s, e, None)
        except LangfuseError as error:
            if not error.refused:
                (failed if failed is not None else {}).setdefault('observations', error.reason)
                out['top'] = None
                out['unread'] = True
                return out
            access['observations'] = error.code
            out['top'] = None
            return out
        access['observations'] = None
        groups: dict[tuple, int] = {}
        for o in rows:
            k = (o['level'], str(o.get('name') or '?')[:80], cut(str(o.get('statusMessage') or ''), MESSAGE_LIMIT))
            groups[k] = groups.get(k, 0) + 1
        out['top'] = sorted(groups.items(), key=lambda kv: -kv[1])[:TOP]
    elif listed:
        out['top'] = []
    return out


def daily_costs(lf: Langfuse, now: float) -> dict[str, float]:
    today = dt.datetime.fromtimestamp(now, dt.timezone.utc).date()
    start = dt.datetime.combine(today - dt.timedelta(days=8), dt.time(), dt.timezone.utc).timestamp()
    out = {}
    for row in lf.metric(query(iso(start), iso(now), [('sum', 'totalCost')], lf.env_metric(), day=True)):
        cost = measure(row, 'sum', 'totalCost')
        if cost is not None and day_of(row):
            out[day_of(row)] = float(cost)
    return out


def card(sig: str, sub: str, sev: str, ns: str, svc: str, now: float, d1, d7, unit: str, label: str, what: str, short: str) -> dict:
    return anomaly(CHECK, sig, ns, svc, sub, sev, now, d1, d7, unit, label, what, max(d1 or 0, d7 or 0) or 1, short)


def evaluate(w: dict, days: dict[str, float], now: float, rules: dict, ns: str, svc: str) -> list[dict]:
    cur, d1, d7 = w['now'], w['1d'], w['7d']
    until = f'{clock.date(now)} {clock.label(now)}'
    found = []
    err, err1, err7 = sum(cur['errors'].values()), sum(d1['errors'].values()), sum(d7['errors'].values())
    base = max(err1, err7)
    sev = ('crit' if above(err, base, rules['errors_crit_times'], rules['errors_gap'], rules['errors_crit_floor'])
           else 'warn' if above(err, base, rules['errors_warn_times'], rules['errors_gap'], rules['errors_floor']) else None)
    if sev:
        listed = cur.get('top')
        top = 'not read on this pass' if cur.get('unread') else f'not readable, the keys lack {PERMISSION}' if listed is None else '; '.join(f'{level} {name} {TIMES}{n}' + (f': {said}' if said else '') for (level, name, said), n in listed)
        found.append(card('lf-errors', '', sev, ns, svc, err, err1, err7, '', 'step errors',
                          f'Langfuse counted {err} error / warning steps in the {svc} traces in the hour to {until} (errors {cur["errors"]["ERROR"]}, warnings {cur["errors"]["WARNING"]}), '
                          f'against {err1} the same hour yesterday and {err7} a week ago. Top steps: {top or "none listed"}. Read them with the langfuse tool, op errors, then op trace on a sample.',
                          f'{sev.upper()} {svc} {err} error steps in the last hour (1d {err1} / 7d {err7})'))
    n, n1, n7 = cur['traces'], d1['traces'], d7['traces']
    sev = ('crit' if below_both(n, n1, n7, rules['traffic_crit'], rules['traffic_floor'])
           else 'warn' if below_both(n, n1, n7, rules['traffic_warn'], rules['traffic_floor']) else None)
    if sev:
        found.append(card('lf-traffic', '', sev, ns, svc, n, n1, n7, '', 'traffic down',
                          f'Langfuse recorded {n} {svc} traces in the hour to {until}, {pct(n, n1)} of the same hour yesterday ({n1}) and {pct(n, n7)} of a week ago ({n7}). '
                          'A drop this size with the app up means requests are failing before they reach the model, the feature is switched off, or tracing stopped.',
                          f'{sev.upper()} {svc} {n} traces in the last hour (1d {n1} / 7d {n7})'))
    p, p1, p7 = cur['p95'], d1['p95'], d7['p95']
    if p is not None and p1 is not None and p7 is not None and n >= rules['latency_min_turns'] and above(p, max(p1, p7), rules['latency_times'], rules['latency_gap_s'], 0):
        found.append(card('lf-latency', '', 'warn', ns, svc, p, p1, p7, 's', 'turns slower',
                          f'The slowest 5 % of {svc} turns took {p:.1f} s or more in the hour to {until} (p95 of {n} turns), against {p1:.1f} s the same hour yesterday '
                          f'and {p7:.1f} s a week ago. Look for a slow tool (op trace on a slow one) or a slower model answer.',
                          f'WARN {svc} p95 {p:.1f} s in the last hour (1d {p1:.1f} / 7d {p7:.1f} s)'))
    c, c1, c7 = cur['turnCost'], d1['turnCost'], d7['turnCost']
    if c is not None and c1 is not None and c7 is not None and n >= rules['turn_cost_min_turns'] and above(c, max(c1, c7), rules['turn_cost_times'], rules['turn_cost_gap_usd'], 0):
        found.append(card('lf-turn-cost', '', 'warn', ns, svc, round(c, 4), round(c1, 4), round(c7, 4), '$', 'cost per turn up',
                          f'A {svc} turn cost ${c:.4f} on average in the hour to {until} (Langfuse cost of the hour over {n} turns), against ${c1:.4f} the same hour yesterday and ${c7:.4f} a week ago. '
                          'More tool rounds, a longer prompt or a lost prompt cache raise it; compare a recent trace with an older one (op trace).',
                          f'WARN {svc} ${c:.4f} per turn in the last hour (1d ${c1:.4f} / 7d ${c7:.4f})'))
    today = dt.datetime.fromtimestamp(now, dt.timezone.utc).date()
    yesterday = str(today - dt.timedelta(days=1))
    before = [days[str(today - dt.timedelta(days=k))] for k in range(2, 9) if str(today - dt.timedelta(days=k)) in days]
    if yesterday in days and len(before) >= 3:
        y, avg = days[yesterday], sum(before) / len(before)
        if above(y, avg, rules['day_cost_times'], rules['day_cost_gap_usd'], rules['day_cost_floor_usd']):
            found.append(card('lf-day-cost', yesterday, 'warn', ns, svc, round(y, 2), round(avg, 2), None, '$', 'cost per day up',
                              f'The {svc} traces cost ${y:.2f} on {yesterday} (Langfuse daily metrics, UTC day), against ${avg:.2f} a day on average over the {len(before)} days before. '
                              'Check whether traffic grew (op stats) or each turn got dearer (the cost per turn card, op trace).',
                              f'WARN {svc} ${y:.2f} on {yesterday} (7 d average ${avg:.2f})'))
    return found


def access_card(part: str, code: int) -> dict:
    what = (f'Langfuse answered HTTP {code} to the {part} read, so the langfuse check cannot judge {COVERS[part]}; the cards that depend on it stay as they are '
            f'until the read works again. Give this instance a project key pair with {PERMISSION}.')
    return kit_access.card(CHECK, 'Langfuse', part, PERMISSION, code, 'keys', plural=True, what=what)


def run_pass(lf: Langfuse, now: float, rules: dict, ns: str, svc: str, probe: bool = False, errors: dict[str, str] | None = None) -> tuple[list[dict], dict | None, list[str], dict[str, int | None]]:
    access: dict[str, int | None] = {'metrics': None}
    try:
        w = {'now': window(lf, now - HOUR, now, True, access, errors), '1d': window(lf, now - DAY - HOUR, now - DAY, False, access),
             '7d': window(lf, now - 7 * DAY - HOUR, now - 7 * DAY, False, access)}
        days = daily_costs(lf, now)
    except LangfuseError as e:
        if not e.refused:
            raise
        return [], None, [], {'metrics': e.code}
    failed = errors if errors is not None else {}
    if probe and 'observations' not in access and 'observations' not in failed:
        try:
            lf.one_observation(iso(now - HOUR), iso(now))
            access['observations'] = None
        except LangfuseError as e:
            if e.refused:
                access['observations'] = e.code
            else:
                failed['observations'] = e.reason
    cur = w['now']
    row = {'part': 'summary', 'at': iso(now), 'turns': cur['traces'], 'steps': cur['steps'], 'errorSteps': cur['errors'], 'p95S': cur['p95'],
           'turnCostUsd': round(cur['turnCost'], 5) if cur['turnCost'] is not None else None, 'turns1d': w['1d']['traces'], 'turns7d': w['7d']['traces']}
    read = {'turns': any(x['traces'] for x in w.values()), 'steps': any(x['steps'] for x in w.values()), 'daily costs': bool(days)}
    return evaluate(w, days, now, rules, ns, svc), row, [part for part in PARTS if not read[part]], access


def links_for(a: dict, url: str) -> list[dict] | None:
    if a.get('src') != CHECK:
        return None
    return [{'label': 'Langfuse traces', 'url': url.rstrip('/') + '/'}]
