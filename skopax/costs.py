import datetime as dt

from skopax.engine import parse_iso
from skopax.investigator.guard import CATEGORIES, NONE, USE as GUARD
from skopax.investigator.pricing import PARTS, POD_BY, SOURCES
from skopax.jiralinks import SWEEP_BY
from skopax.postdeploy import SERVER_BY
from skopax import clock

GUARD_CATEGORIES = tuple(c for c in CATEGORIES if c != NONE)

ADVISOR = 'advisor'
MACHINE_BY = frozenset({POD_BY, SERVER_BY, SWEEP_BY})
TOP = 10
RUN_LIST = 100
CHAT = 'chat'
CHAT_WHAT = 'Chat message (private)'


def window(days: int, today: str) -> tuple[str, str, list[str]]:
    last = dt.date.fromisoformat(today)
    first = (last - dt.timedelta(days=days - 1)).isoformat()
    if days == 1:
        return first, 'hour', [f'{today}T{h:02d}' for h in range(24)]
    return first, 'day', [(last - dt.timedelta(days=n)).isoformat() for n in range(days - 1, -1, -1)]


def bucket_of(row: dict, bucket: str) -> str:
    if bucket == 'day':
        return row.get('day') or ''
    return clock.local(parse_iso(row['at'])).strftime('%Y-%m-%dT%H')


def money() -> dict:
    return dict.fromkeys(PARTS + ('total',), 0.0)


def tokens() -> dict:
    return dict.fromkeys(PARTS, 0)


def add(into: dict, row: dict, counted: bool = True) -> None:
    for p in PARTS + ('total',):
        into['usd'][p] += float((row.get('usd') or {}).get(p) or 0)
    for p in PARTS if counted else ():
        into['tokens'][p] += int((row.get('tokens') or {}).get(p) or 0)


def hit_share(t: dict) -> float | None:
    prompt = t['cacheRead'] + t['input'] + t['cacheWrite5m'] + t['cacheWrite1h']
    return round(t['cacheRead'] / prompt, 3) if prompt else None


def rounded(usd: dict) -> dict:
    return {k: round(v, 4) for k, v in usd.items()}


def seconds(meta: dict) -> int | None:
    try:
        return round(parse_iso(meta['endedAt']) - parse_iso(meta['startedAt']))
    except (KeyError, TypeError, ValueError):
        return None


def what(meta: dict, keys: list[str]) -> str:
    return (meta.get('ask') or {}).get('text') or (meta.get('report') or {}).get('summary') or ', '.join(meta.get('keys') or keys)


def new_run(rid: str, at: str, source: str, by, keys: list, effort) -> dict:
    return {'id': rid, 'at': at, 'source': source, 'by': by, 'keys': keys, 'effort': effort, 'models': {}, 'calls': 0, 'usd': money(), 'tokens': tokens(), 'advisor': 0.0, 'advisorModels': {}}


def objection_runs(flagged: dict[str, dict], runs: dict[str, dict], meta: dict[str, dict], limit: int | None) -> list[dict]:
    found = [runs.get(rid) or new_run(rid, f['at'], GUARD, None, [], None) for rid, f in flagged.items()]
    newest = sorted(found, key=lambda run: run['at'], reverse=True)[:limit]
    return [run_row(run, meta.get(run['id']) or {}) | {'objections': {c: flagged[run['id']]['n'][c] for c in GUARD_CATEGORIES if flagged[run['id']]['n'].get(c)}} for run in newest]


def summary(rows: list[dict], meta: dict[str, dict], today: str, days: int, spent: float, cap: float, guard_on: bool = False, objections: bool = False, every_run: bool = False) -> dict:
    first, bucket, keys = window(days, today)
    per = {k: dict.fromkeys(SOURCES, 0.0) for k in keys}
    cache = {k: {s: {'usd': money(), 'tokens': tokens()} for s in SOURCES} for k in keys}
    src = {s: {'calls': 0, 'runs': set(), 'usd': money(), 'tokens': tokens(), 'models': {}, 'categories': {}, 'advisor': {'calls': 0, 'runs': set(), 'usd': 0.0, 'estimated': 0.0, 'models': set()}} for s in SOURCES}
    advised = 0.0
    models: dict[str, dict] = {}
    cards: dict[str, dict] = {}
    people: dict[str, dict] = {}
    runs: dict[str, dict] = {}
    flagged: dict[str, dict] = {}
    whole = {'usd': money(), 'tokens': tokens()}
    for r in rows:
        s = r.get('source')
        if s not in src:
            continue
        total = float((r.get('usd') or {}).get('total') or 0)
        model = r.get('model') or '?'
        rid = r.get('runId')
        x = src[s]
        by_advisor = r.get('role') == ADVISOR
        n = 0 if by_advisor else int(r.get('calls') or 1)
        x['calls'] += n
        if s == GUARD and not by_advisor and r.get('guardCategory') in GUARD_CATEGORIES:
            x['categories'][r['guardCategory']] = x['categories'].get(r['guardCategory'], 0) + n
            if r.get('guardRun'):
                f = flagged.setdefault(r['guardRun'], {'at': r.get('at') or '', 'n': {}})
                f['n'][r['guardCategory']] = f['n'].get(r['guardCategory'], 0) + n
        add(x, r, not by_advisor)
        add(whole, r, not by_advisor)
        if by_advisor:
            x['advisor']['calls'] += int(r.get('calls') or 1)
            x['advisor']['usd'] += total
            if r.get('estimated'):
                x['advisor']['estimated'] += total
            x['advisor']['models'].add(model)
            if rid:
                x['advisor']['runs'].add(rid)
            advised += total
        else:
            x['models'][model] = x['models'].get(model, 0) + n
        m = models.setdefault(model, {'model': model, 'calls': 0, 'usd': 0.0})
        m['calls'] += n
        m['usd'] += total
        b = bucket_of(r, bucket)
        if b in per:
            per[b][s] += total
            add(cache[b][s], r, not by_advisor)
        card_keys = [] if CHAT in (s, r.get('gateFor'), r.get('talkFor')) else r.get('keys') or []
        if not rid and r.get('guardRun'):
            x['runs'].add(r['guardRun'])
        if rid:
            x['runs'].add(rid)
            run = runs.get(rid) or runs.setdefault(rid, new_run(rid, r.get('at') or '', s, r.get('by'), card_keys, r.get('effort')))
            run['at'] = min(run['at'], r.get('at') or run['at'])
            run['calls'] += n
            if by_advisor:
                run['advisor'] += total
                run['advisorModels'][model] = run['advisorModels'].get(model, 0) + 1
            else:
                run['models'][model] = run['models'].get(model, 0) + n
            add(run, r, not by_advisor)
        for k in card_keys:
            c = cards.setdefault(k, {'key': k, 'runs': set(), 'usd': 0.0})
            c['usd'] += total / len(card_keys)
            if rid:
                c['runs'].add(rid)
        who = r.get('by')
        if who and who not in MACHINE_BY:
            p = people.setdefault(who, {'by': who, 'runs': set(), 'calls': 0, 'usd': 0.0})
            p['calls'] += n
            p['usd'] += total
            if rid:
                p['runs'].add(rid)
    by_source = []
    for s in SOURCES:
        x = src[s]
        by_source.append({'source': s, 'calls': x['calls'], 'runs': len(x['runs']), 'usd': rounded(x['usd']), 'cacheHit': hit_share(x['tokens']),
                          'avgRun': round(x['usd']['total'] / len(x['runs']), 4) if x['runs'] else None, 'models': x['models'],
                          **({'categories': {c: x['categories'][c] for c in GUARD_CATEGORIES if x['categories'].get(c)}} if s == GUARD else {}),
                          'advisor': {'calls': x['advisor']['calls'], 'runs': len(x['advisor']['runs']), 'usd': round(x['advisor']['usd'], 4), 'estimatedUsd': round(x['advisor']['estimated'], 4), 'models': sorted(x['advisor']['models'])} if x['advisor']['calls'] else None})
    limit = None if every_run else RUN_LIST
    newest = sorted(runs.values(), key=lambda run: run['at'], reverse=True)[:limit]
    return {
        'days': days, 'from': first, 'to': today, 'bucket': bucket,
        'today': {'day': today, 'spent': round(spent, 4), 'cap': cap},
        'total': {'usd': round(whole['usd']['total'], 4), 'calls': sum(x['calls'] for x in src.values()), 'runs': len(runs), 'cacheHit': hit_share(whole['tokens']), 'advisorUsd': round(advised, 4)},
        'avg': {'slackAsk': by_source[SOURCES.index('slack')]['avgRun'], 'podRun': by_source[SOURCES.index('pod')]['avgRun']},
        'perBucket': [{'t': k, **{s: round(per[k][s], 4) for s in SOURCES}, 'total': round(sum(per[k].values()), 4)} for k in keys],
        'sources': by_source,
        'models': sorted(({**m, 'usd': round(m['usd'], 4)} for m in models.values()), key=lambda m: -m['usd']),
        'cache': [{'t': k, 'hit': {s: hit_share(cache[k][s]['tokens']) for s in SOURCES},
                   'writeUsd': {s: round(cache[k][s]['usd']['cacheWrite5m'] + cache[k][s]['usd']['cacheWrite1h'], 4) for s in SOURCES}} for k in keys],
        'topCards': sorted(({'key': c['key'], 'runs': len(c['runs']), 'usd': round(c['usd'], 4)} for c in cards.values()), key=lambda c: -c['usd'])[:TOP],
        'topAskers': sorted(({'by': p['by'], 'runs': len(p['runs']), 'calls': p['calls'], 'usd': round(p['usd'], 4)} for p in people.values()), key=lambda p: -p['usd'])[:TOP],
        'runs': [run_row(run, meta.get(run['id']) or {}) for run in newest],
        'guardOn': guard_on,
        **({'objectionRuns': objection_runs(flagged, runs, meta, limit)} if objections else {}),
    }


def run_row(run: dict, meta: dict) -> dict:
    private = CHAT in (run['source'], meta.get('trigger'))
    return {'id': run['id'], 'at': meta.get('startedAt') or run['at'], 'source': run['source'], 'by': run['by'], 'trigger': meta.get('trigger'), 'outcome': meta.get('outcome'),
            'what': CHAT_WHAT if private else what(meta, run['keys']), 'keys': [] if private else meta.get('keys') or run['keys'],
            'model': max(run['models'] or run['advisorModels'], key=(run['models'] or run['advisorModels']).get) if run['models'] or run['advisorModels'] else None, 'effort': run['effort'],
            'calls': run['calls'], 'advisorUsd': round(run['advisor'], 4), 'advisorModel': max(run['advisorModels'], key=run['advisorModels'].get) if run['advisorModels'] else None, 'advisorCalls': int(meta.get('advisorCalls') or 0), 'toolCalls': len(meta['toolCalls']) if 'toolCalls' in meta else None, 'seconds': seconds(meta),
            'usd': rounded(run['usd']), 'cacheHit': hit_share(run['tokens'])}
