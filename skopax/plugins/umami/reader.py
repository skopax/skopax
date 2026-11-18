from skopax.contract import iso
from skopax.kit.umami import EVENT, Umami, UmamiError, numbers, pairs, span

OPS = ('active', 'stats', 'metrics', 'event_data')
METRICS = ('path', 'entry', 'exit', 'title', 'referrer', 'event', 'hostname', 'country', 'device', 'browser', 'os')
COMPARES = {'day': 86400, 'week': 7 * 86400}
MAX_HOURS = 168
MAX_ROWS = 30
ROWS = 10
COMPARE_ROWS = 100
PATH_LIMIT = 200


def whole(value, default: int, low: int, high: int, what: str) -> int:
    got = default if value is None else value
    if not isinstance(got, int) or isinstance(got, bool) or not low <= got <= high:
        raise UmamiError(f'{what} must be {low}-{high}', 400)
    return got


def wanted(a: dict, now: float, default_event: str, allowed: tuple[str, ...]) -> dict:
    op = a.get('op') or 'stats'
    if op not in OPS:
        raise UmamiError(f"op must be one of {', '.join(OPS)}", 400)
    hours = whole(a.get('hours'), 1, 1, MAX_HOURS, 'hours')
    limit = whole(a.get('limit'), ROWS, 1, MAX_ROWS, 'limit')
    compare = a.get('compare')
    if compare is not None and compare not in COMPARES:
        raise UmamiError(f"compare must be one of {', '.join(COMPARES)}", 400)
    filters = {}
    path = str(a.get('path') or '').strip()
    if path:
        if not path.startswith('/') or len(path) > PATH_LIMIT:
            raise UmamiError(f'path must start with / and be at most {PATH_LIMIT} characters', 400)
        filters['path'] = path
    event = str(a.get('event') or '').strip()
    if event and not EVENT.fullmatch(event):
        raise UmamiError('event must be an event name such as signup', 400)
    if event and op != 'event_data':
        filters['event'] = event
    kind = a.get('type') or 'path'
    if op == 'metrics' and kind not in METRICS:
        raise UmamiError(f"type must be one of {', '.join(METRICS)}", 400)
    prop = a.get('property') or (allowed[0] if allowed else '')
    if op == 'event_data':
        if not allowed:
            raise UmamiError('op event_data is off: this instance lists no event properties', 400)
        if prop not in allowed:
            raise UmamiError(f"property must be one of {', '.join(allowed)} (ids and session data are not read)", 400)
        if not (event or default_event):
            raise UmamiError('event is required for op event_data: this instance names no event', 400)
    windows = [('now', now - hours * 3600, now)]
    if compare and op != 'active':
        back = COMPARES[compare]
        windows.append((f'{compare} before', now - hours * 3600 - back, now - back))
    return {'op': op, 'type': kind, 'property': prop, 'event': event or default_event, 'limit': limit, 'filters': filters, 'windows': windows}


def read(um: Umami, w: dict) -> dict:
    if w['op'] == 'active':
        got = um.get('active', {})
        return {'op': 'active', 'visitors': int((got or {}).get('visitors') or 0) if isinstance(got, dict) else 0}
    out = {'op': w['op'], 'window': {'from': iso(w['windows'][0][1]), 'to': iso(w['windows'][0][2])}}
    if w['op'] == 'stats':
        per = [numbers(um.get('stats', span(s, e) | w['filters'])) for _, s, e in w['windows']]
        out['now'] = per[0]
        if len(per) > 1:
            out[w['windows'][1][0]] = per[1]
        return out | ({'filters': w['filters']} if w['filters'] else {})
    if w['op'] == 'metrics':
        def fetch(s, e, n):
            return pairs(um.get('metrics', span(s, e) | {'type': w['type'], 'limit': n} | w['filters']), 'x', 'y', n, 'metrics')
        key, count = w['type'], 'count'
    else:
        def fetch(s, e, n):
            return pairs(um.get('event-data/values', span(s, e) | {'eventName': w['event'], 'propertyName': w['property']} | w['filters']), 'value', 'total', n, 'event data')
        key, count = w['property'], 'events'
        out['event'] = w['event']
    lists = [fetch(s, e, w['limit'] if i == 0 else COMPARE_ROWS) for i, (_, s, e) in enumerate(w['windows'])]
    rows = [{key: name, count: n} for name, n in lists[0]]
    if len(lists) > 1:
        before, name, cut = dict(lists[1]), w['windows'][1][0], len(lists[1]) >= COMPARE_ROWS
        for row in rows:
            row[name] = before.get(row[key], None if cut else 0)
        if any(row[name] is None for row in rows):
            out['note'] = f'{name} is null for a row outside the top {COMPARE_ROWS} of that window: it had fewer, but how many is unknown'
    return out | {'rows': rows} | ({'filters': w['filters']} if w['filters'] else {})
