import datetime as dt
import math
import re
from urllib.parse import urlencode

from skopax.kit.promql import signal_queries


def timestamp(value: str | None) -> dt.datetime | None:
    try:
        parsed = dt.datetime.fromisoformat(value)
        return parsed.astimezone(dt.timezone.utc) if parsed.tzinfo else None
    except (TypeError, ValueError):
        return None


def window_of(window: dict) -> tuple[dt.datetime, dt.datetime] | None:
    start, end = timestamp(window.get('from')), timestamp(window.get('to'))
    if start is None or end is None or end < start:
        return None
    return start, end


def row_links(row: dict, window: dict, ui: str) -> dict[str, str]:
    bounds = window_of(window)
    if not ui or bounds is None or not row.get('ns') or not row.get('svc'):
        return {}
    start, end = bounds
    span = f'{max(300, int((end - start).total_seconds()))}s'
    names = {re.split(r'[\s=]', part.strip())[0] for part in (row.get('metrics') or '').split(',') + (row.get('flags') or [])}
    queries = signal_queries(row['ns'], row['svc'], span)
    missing = names.intersection(queries).difference(row.get('links') or {})
    host = ui.rstrip('/')
    at = timestamp(row.get('at')) or start
    graph_start = min(at, start) - dt.timedelta(minutes=30)
    graph_end = end + dt.timedelta(minutes=15)
    return {name: host + '/query?' + urlencode({
        'g0.expr': queries[name], 'g0.tab': 'graph',
        'g0.range_input': f'{math.ceil((graph_end - graph_start).total_seconds() / 60)}m',
        'g0.end_input': graph_end.strftime('%Y-%m-%d %H:%M:%S'),
    }) for name in sorted(missing)}
