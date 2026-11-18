import datetime as dt

from skopax.engine import parse_iso
from skopax.investigator.pricing import PARTS, POD_BY, SOURCES
from skopax.jiralinks import SWEEP_BY
from skopax.postdeploy import SERVER_BY
from skopax import clock

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


def add(into: dict, row: dict) -> None:
    for p in PARTS + ('total',):
        into['usd'][p] += float((row.get('usd') or {}).get(p) or 0)
    for p in PARTS:
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


def summary(rows: list[dict], meta: dict[str, dict], today: str, days: int, spent: float, cap: float) -> dict:
    first, bucket, keys = window(days, today)
    per = {k: dict.fromkeys(SOURCES, 0.0) for k in keys}
    cache = {k: {s: {'usd': money(), 'tokens': tokens()} for s in SOURCES} for k in keys}
    src = {s: {'calls': 0, 'runs': set(), 'usd': money(), 'tokens': tokens(), 'models': {}} for s in SOURCES}
    models: dict[str, dict] = {}
    cards: dict[str, dict] = {}
    people: dict[str, dict] = {}
    runs: dict[str, dict] = {}
    whole = {'usd': money(), 'tokens': tokens()}
    for r in rows:
        s = r.get('source')
        if s not in src:
            continue
        total = float((r.get('usd') or {}).get('total') or 0)
        model = r.get('model') or '?'
        rid = r.get('runId')
        x = src[s]
        n = int(r.get('calls') or 1)
        x['calls'] += n
        add(x, r)
        add(whole, r)
        x['models'][model] = x['models'].get(model, 0) + n
        m = models.setdefault(model, {'model': model, 'calls': 0, 'usd': 0.0})
        m['calls'] += n
        m['usd'] += total
        b = bucket_of(r, bucket)
        if b in per:
            per[b][s] += total
            add(cache[b][s], r)
        card_keys = [] if CHAT in (s, r.get('gateFor'), r.get('talkFor')) else r.get('keys') or []
        if rid:
            x['runs'].add(rid)
            run = runs.setdefault(rid, {'id': rid, 'at': r.get('at') or '', 'source': s, 'by': r.get('by'), 'keys': card_keys, 'effort': r.get('effort'),
                                        'models': {}, 'calls': 0, 'usd': money(), 'tokens': tokens()})
            run['at'] = min(run['at'], r.get('at') or run['at'])
            run['calls'] += n
            run['models'][model] = run['models'].get(model, 0) + n
            add(run, r)
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
                          'avgRun': round(x['usd']['total'] / len(x['runs']), 4) if x['runs'] else None, 'models': x['models']})
    newest = sorted(runs.values(), key=lambda run: run['at'], reverse=True)[:RUN_LIST]
    return {
        'days': days, 'from': first, 'to': today, 'bucket': bucket,
        'today': {'day': today, 'spent': round(spent, 4), 'cap': cap},
        'total': {'usd': round(whole['usd']['total'], 4), 'calls': sum(x['calls'] for x in src.values()), 'runs': len(runs), 'cacheHit': hit_share(whole['tokens'])},
        'avg': {'slackAsk': by_source[SOURCES.index('slack')]['avgRun'], 'podRun': by_source[SOURCES.index('pod')]['avgRun']},
        'perBucket': [{'t': k, **{s: round(per[k][s], 4) for s in SOURCES}, 'total': round(sum(per[k].values()), 4)} for k in keys],
        'sources': by_source,
        'models': sorted(({**m, 'usd': round(m['usd'], 4)} for m in models.values()), key=lambda m: -m['usd']),
        'cache': [{'t': k, 'hit': {s: hit_share(cache[k][s]['tokens']) for s in SOURCES},
                   'writeUsd': {s: round(cache[k][s]['usd']['cacheWrite5m'] + cache[k][s]['usd']['cacheWrite1h'], 4) for s in SOURCES}} for k in keys],
        'topCards': sorted(({'key': c['key'], 'runs': len(c['runs']), 'usd': round(c['usd'], 4)} for c in cards.values()), key=lambda c: -c['usd'])[:TOP],
        'topAskers': sorted(({'by': p['by'], 'runs': len(p['runs']), 'calls': p['calls'], 'usd': round(p['usd'], 4)} for p in people.values()), key=lambda p: -p['usd'])[:TOP],
        'runs': [run_row(run, meta.get(run['id']) or {}) for run in newest],
    }


def run_row(run: dict, meta: dict) -> dict:
    private = CHAT in (run['source'], meta.get('trigger'))
    return {'id': run['id'], 'at': meta.get('startedAt') or run['at'], 'source': run['source'], 'by': run['by'], 'trigger': meta.get('trigger'), 'outcome': meta.get('outcome'),
            'what': CHAT_WHAT if private else what(meta, run['keys']), 'keys': [] if private else meta.get('keys') or run['keys'],
            'model': max(run['models'], key=run['models'].get), 'effort': run['effort'],
            'calls': run['calls'], 'toolCalls': len(meta['toolCalls']) if 'toolCalls' in meta else None, 'seconds': seconds(meta),
            'usd': rounded(run['usd']), 'cacheHit': hit_share(run['tokens'])}
