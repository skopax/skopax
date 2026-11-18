import datetime as dt
import re

from skopax import clock
from skopax.contract import PassResult, ReadError, Sweep, anomaly
from skopax.kit.google import GoogleAuth, GoogleError, access_card, access_key
from skopax.kit.releases import attach_publish
from skopax.redact import redact

CHECK = 'ga'
NAME = 'Google Analytics 4'
PART = 'web'
SHOWN = 'the web property'
DOT = ' \u00b7 '
GA_API = 'https://analyticsdata.googleapis.com/v1beta'
GA_UI = 'https://analytics.google.com/analytics/web/#/p{pid}/reports/intelligenthome'
SCOPES = ('https://www.googleapis.com/auth/analytics.readonly',)
NEEDS = 'the Viewer role on the GA4 property'
NO_ACCESS = 'the service account has no access to this GA4 property; add it under Admin, Property access management, with the Viewer role'
BAD_PROPERTY = 'property-id must be the numeric GA4 property id'
BAD_APP_PROPERTY = 'app-property-id must be the numeric GA4 property id'
BAD_SITE = 'site must be a host or service name such as example.com'
PROPERTIES = ('web', 'app')
FIELDS = {'web': 'property-id', 'app': 'app-property-id'}
OPS = ('realtime', 'report')
REALTIME_DIMENSIONS = ('appVersion', 'city', 'country', 'deviceCategory', 'eventName', 'minutesAgo', 'platform', 'streamName', 'unifiedScreenName')
REALTIME_METRICS = ('activeUsers', 'eventCount', 'keyEvents', 'screenPageViews')
REPORT_DIMENSIONS = ('date', 'dateHour', 'hour', 'eventName', 'pagePath', 'landingPage', 'pageTitle', 'hostName', 'deviceCategory', 'platform', 'operatingSystem',
                     'browser', 'country', 'city', 'appVersion', 'streamName', 'sessionDefaultChannelGroup', 'sessionSource', 'sessionMedium', 'sessionCampaignName', 'isKeyEvent')
REPORT_METRICS = ('activeUsers', 'newUsers', 'totalUsers', 'sessions', 'engagedSessions', 'screenPageViews', 'eventCount', 'keyEvents', 'engagementRate', 'bounceRate',
                  'averageSessionDuration', 'userEngagementDuration')
MATCHES = {'exact': 'EXACT', 'contains': 'CONTAINS', 'begins': 'BEGINS_WITH'}
COMPARES = ('previous', 'week')
MAX_DIMENSIONS = 3
MAX_METRICS = 4
MAX_MINUTES = 30
MAX_DAYS = 7
MAX_ROWS = 50
ROWS = 20
VALUE_LIMIT = 200
FILTER_LIMIT = 200
ERROR_LIMIT = 200
DAY = re.compile(r'\d{4}-\d{2}-\d{2}')
DATE8 = re.compile(r'(\d{4})(\d{2})(\d{2})')
PROPERTY = re.compile(r'\d{1,20}')
SITE = re.compile(r'[A-Za-z0-9][A-Za-z0-9._-]{0,99}')
PARTIAL = 'GA4 finishes processing a day within 24-48 h: today and yesterday are partial, so compare them only with the same partial window'
RULES = {'drop': 0.3, 'floor': 500}
RULE_HELP = {
    'drop': "Warning when a day's key events or active users are this share lower than the same weekday a week earlier (0.3 = 30 % lower), per platform.",
    'floor': 'A metric is checked only when the earlier day had at least this many, so small numbers raise nothing.',
}
CHECKED_DAYS_BACK = 2
DAILY_METRICS = ('activeUsers', 'keyEvents')
WINDOW_S = 48 * 3600


class AnalyticsError(ReadError):
    def __init__(self, message: str, status: int = 400):
        super().__init__(status, message)
        self.reason = message


class Analytics:
    def __init__(self, auth: GoogleAuth, web: str = '', app: str = ''):
        self.auth = auth
        self.given = {name: str(pid or '').strip() for name, pid in (('web', web), ('app', app))}
        self.properties = {name: pid for name, pid in self.given.items() if PROPERTY.fullmatch(pid)}

    @property
    def enabled(self) -> bool:
        return self.auth.enabled and bool(self.properties)

    def problem(self, name: str) -> str | None:
        return (BAD_PROPERTY if name == 'web' else BAD_APP_PROPERTY) if self.given.get(name) and name not in self.properties else None

    def property_of(self, name) -> str:
        wanted = name or ('web' if 'web' in self.properties else 'app')
        if wanted not in PROPERTIES:
            raise AnalyticsError(f"property must be one of {', '.join(PROPERTIES)}")
        if self.problem(wanted):
            raise AnalyticsError(self.problem(wanted))
        if wanted not in self.properties:
            raise AnalyticsError(f'the {wanted} GA4 property id is not set' + ('; the web property may hold the app streams (streamName)' if wanted == 'app' else ''))
        return self.properties[wanted]

    def run(self, property_id: str, method: str, body: dict) -> dict:
        return self.auth.call('POST', f'{GA_API}/properties/{property_id}:{method}', body, 'Google Analytics', NO_ACCESS, needs=NEEDS)

    def metadata(self, property_id: str) -> dict:
        return self.auth.call('GET', f'{GA_API}/properties/{property_id}/metadata', None, 'Google Analytics', NO_ACCESS, retry=False, needs=NEEDS)


def today(now: float) -> dt.date:
    return clock.local(now).date()


def relative(days_ago: int) -> str:
    return 'today' if days_ago == 0 else 'yesterday' if days_ago == 1 else f'{days_ago}daysAgo'


def names(values, allowed: tuple[str, ...], what: str, op: str, most: int, default: list[str]) -> list[str]:
    picked = [str(x) for x in (values or default)]
    if len(picked) > most:
        raise AnalyticsError(f'{what} takes at most {most} names')
    wrong = [x for x in picked if x not in allowed]
    if wrong:
        raise AnalyticsError(f"{op} {what} must be from {', '.join(allowed)}; got {', '.join(wrong)[:ERROR_LIMIT]}")
    return list(dict.fromkeys(picked))


def dimension_filter(f, allowed: tuple[str, ...], op: str) -> dict | None:
    if not f:
        return None
    if not isinstance(f, dict):
        raise AnalyticsError('filter must be an object {dimension, value, match}')
    dimension = str(f.get('dimension') or '')
    if dimension not in allowed:
        raise AnalyticsError(f"{op} filter dimension must be from {', '.join(allowed)}")
    value = ' '.join(str(f.get('value') or '').split())
    if not value or len(value) > FILTER_LIMIT:
        raise AnalyticsError(f'filter value must be 1-{FILTER_LIMIT} characters')
    match = str(f.get('match') or 'exact')
    if match not in MATCHES:
        raise AnalyticsError(f"filter match must be one of {', '.join(MATCHES)}")
    return {'filter': {'fieldName': dimension, 'stringFilter': {'matchType': MATCHES[match], 'value': value, 'caseSensitive': False}}}


def row_limit(value) -> int:
    limit = ROWS if value is None else value
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= MAX_ROWS:
        raise AnalyticsError(f'limit must be 1-{MAX_ROWS}')
    return limit


def end_day(value, now_day: dt.date) -> dt.date:
    end = str(value or 'today').strip()
    if end == 'today':
        return now_day
    if end == 'yesterday':
        return now_day - dt.timedelta(days=1)
    if not DAY.fullmatch(end):
        raise AnalyticsError('end must be today, yesterday or YYYY-MM-DD')
    try:
        day = dt.date.fromisoformat(end)
    except ValueError:
        raise AnalyticsError('end must be today, yesterday or YYYY-MM-DD') from None
    if day > now_day + dt.timedelta(days=1):
        raise AnalyticsError('end is more than a day ahead of today')
    return day


def date_ranges(days, end, compare, now: float) -> list[dict]:
    span = 1 if days is None else days
    if not isinstance(span, int) or isinstance(span, bool) or not 1 <= span <= MAX_DAYS:
        raise AnalyticsError(f'days must be 1-{MAX_DAYS}')
    if compare and compare not in COMPARES:
        raise AnalyticsError(f"compare must be one of {', '.join(COMPARES)}")
    shift = (span if compare == 'previous' else 7) if compare else None
    named = str(end or 'today').strip()
    if named in ('today', 'yesterday'):
        back = 0 if named == 'today' else 1
        ranges = [{'startDate': relative(back + span - 1), 'endDate': relative(back), 'name': 'current'}]
        if shift is not None:
            ranges.append({'startDate': relative(back + shift + span - 1), 'endDate': relative(back + shift), 'name': 'compare'})
        return ranges
    last = end_day(named, today(now))
    first = last - dt.timedelta(days=span - 1)
    ranges = [{'startDate': first.isoformat(), 'endDate': last.isoformat(), 'name': 'current'}]
    if shift is not None:
        back = dt.timedelta(days=shift)
        ranges.append({'startDate': (first - back).isoformat(), 'endDate': (last - back).isoformat(), 'name': 'compare'})
    return ranges


def recent(ranges: list[dict], now: float) -> bool:
    end = str(ranges[0].get('endDate') or '')
    if not DAY.fullmatch(end):
        return end in ('today', 'yesterday')
    return end >= (today(now) - dt.timedelta(days=1)).isoformat()


def realtime_body(a: dict) -> dict:
    minutes = MAX_MINUTES if a.get('minutes') is None else a.get('minutes')
    if not isinstance(minutes, int) or isinstance(minutes, bool) or not 1 <= minutes <= MAX_MINUTES:
        raise AnalyticsError(f'minutes must be 1-{MAX_MINUTES}')
    dimensions = names(a.get('dimensions'), REALTIME_DIMENSIONS, 'dimensions', 'realtime', MAX_DIMENSIONS, [])
    metrics = names(a.get('metrics'), REALTIME_METRICS, 'metrics', 'realtime', MAX_METRICS, ['activeUsers'])
    body = {'dimensions': [{'name': x} for x in dimensions], 'metrics': [{'name': x} for x in metrics],
            'minuteRanges': [{'name': f'last {minutes} min', 'startMinutesAgo': minutes - 1, 'endMinutesAgo': 0}],
            'orderBys': [{'metric': {'metricName': metrics[0]}, 'desc': True}], 'limit': row_limit(a.get('limit'))}
    flt = dimension_filter(a.get('filter'), REALTIME_DIMENSIONS, 'realtime')
    return body | ({'dimensionFilter': flt} if flt else {})


def report_body(a: dict, now: float) -> dict:
    dimensions = names(a.get('dimensions'), REPORT_DIMENSIONS, 'dimensions', 'report', MAX_DIMENSIONS, [])
    metrics = names(a.get('metrics'), REPORT_METRICS, 'metrics', 'report', MAX_METRICS, ['activeUsers', 'eventCount'])
    body = {'dateRanges': date_ranges(a.get('days'), a.get('end'), a.get('compare'), now), 'dimensions': [{'name': x} for x in dimensions],
            'metrics': [{'name': x} for x in metrics], 'orderBys': [{'metric': {'metricName': metrics[0]}, 'desc': True}], 'limit': row_limit(a.get('limit'))}
    flt = dimension_filter(a.get('filter'), REPORT_DIMENSIONS, 'report')
    return body | ({'dimensionFilter': flt} if flt else {})


def number(text: str):
    try:
        value = float(text)
    except (TypeError, ValueError):
        return redact(str(text))[:VALUE_LIMIT]
    return int(value) if value.is_integer() else round(value, 4)


def rows_of(body: dict) -> list[dict]:
    dims = [h.get('name') for h in body.get('dimensionHeaders') or []]
    mets = [h.get('name') for h in body.get('metricHeaders') or []]
    out = []
    for r in body.get('rows') or []:
        row = {d: redact(str(v.get('value') or ''))[:VALUE_LIMIT] for d, v in zip(dims, r.get('dimensionValues') or [])}
        out.append(row | {m: number(v.get('value')) for m, v in zip(mets, r.get('metricValues') or [])})
    return out


def request(ga: Analytics, a: dict, now: float) -> tuple[str, str, dict]:
    op = a.get('op')
    if op not in OPS:
        raise AnalyticsError(f"op must be one of {', '.join(OPS)}")
    pid = ga.property_of(a.get('property'))
    return op, pid, realtime_body(a) if op == 'realtime' else report_body(a, now)


def read(ga: Analytics, op: str, pid: str, body: dict, now: float) -> dict:
    got = ga.run(pid, 'runRealtimeReport' if op == 'realtime' else 'runReport', body | {'returnPropertyQuota': True})
    name = next(k for k, v in ga.properties.items() if v == pid)
    out = {'op': op, 'property': name, 'range': body.get('minuteRanges') or body.get('dateRanges'), 'rows': rows_of(got), 'rowCount': got.get('rowCount', 0)}
    meta = got.get('metadata') or {}
    notes = [PARTIAL] if op == 'report' and recent(body['dateRanges'], now) else []
    if meta.get('dataLossFromOtherRow'):
        notes.append('some rows were folded into (other) by GA4 cardinality limits')
    if meta.get('samplingMetadatas'):
        notes.append('GA4 sampled this report: the numbers are estimates')
    if op == 'report' and len(body['dateRanges']) > 1:
        notes.append('rows carry dateRange current or compare')
    return out | ({'note': '; '.join(notes)} if notes else {})


def day_of(rows: list[dict]) -> str | None:
    for r in rows:
        m = DATE8.fullmatch(str(r.get('date') or '')) if r.get('dateRange') == 'current' else None
        if m:
            return '-'.join(m.groups())
    return None


def daily_rows(ga: Analytics, now: float) -> tuple[str, str, list[dict]]:
    pid = ga.property_of('web')
    day, week = relative(CHECKED_DAYS_BACK), relative(CHECKED_DAYS_BACK + 7)
    body = {'dateRanges': [{'startDate': day, 'endDate': day, 'name': 'current'}, {'startDate': week, 'endDate': week, 'name': 'compare'}],
            'dimensions': [{'name': 'platform'}, {'name': 'date'}], 'metrics': [{'name': m} for m in DAILY_METRICS], 'limit': 20}
    rows = rows_of(ga.run(pid, 'runReport', body))
    return day_of(rows) or (today(now) - dt.timedelta(days=CHECKED_DAYS_BACK)).isoformat(), pid, rows


def evaluate_ga(day: str, pid: str, svc: str, rows: list[dict], rules: dict) -> list[dict]:
    by = {(r.get('platform'), r.get('dateRange')): r for r in rows}
    found = []
    for platform in sorted({p for p, _ in by}):
        cur, old = by.get((platform, 'current')) or {}, by.get((platform, 'compare')) or {}
        for metric in DAILY_METRICS:
            n, base = cur.get(metric) or 0, old.get(metric) or 0
            if base < rules['floor'] or n >= (1 - rules['drop']) * base:
                continue
            word = 'key events' if metric == 'keyEvents' else 'active users'
            x = anomaly(CHECK, 'ga-drop', '-', svc, f'{platform}:{metric}', 'warn', n, None, None, '', f'GA4 {word} down',
                        f'GA4 counted {n:,} {word} on {platform} on {day}, {round(100 * n / base)} % of the same weekday a week earlier ({base:,}). '
                        f'The check reads the day before yesterday because GA4 finishes processing a day within 24-48 h. A drop in key events with users steady points at '
                        f'tracking (a GTM publish or the consent banner on the web, an app release) or at GA4 no longer counting an event as a key event; both down points at traffic.',
                        base, f'WARN GA4 {platform} {word} {n:,} on {day} (week before {base:,})')
            x['gaProperty'] = pid
            x['gaDay'] = day
            found.append(x)
    return found


def day_window(day: str) -> tuple[float, float]:
    d = dt.date.fromisoformat(day)
    start = dt.datetime(d.year, d.month, d.day, tzinfo=clock.zone()).timestamp()
    return start - WINDOW_S, start + 2 * 86400


def attach_to_ga(cards: list[dict], rows: list[dict]) -> None:
    for card in cards:
        if card.get('gaDay') and str(card.get('sub') or '').startswith('web:'):
            attach_publish([card], rows, *day_window(card['gaDay']))


def refused(error: GoogleError) -> Sweep:
    card = access_card(CHECK, NAME, PART, SHOWN, error)
    return Sweep(PassResult(CHECK, True, f'ok{DOT}no access to {SHOWN}', [card], {}, (access_key(CHECK, PART),)))


def links_for(a: dict) -> list[dict] | None:
    if a.get('src') != CHECK:
        return None
    return [dict(label='GA4 property', url=GA_UI.format(pid=a['gaProperty']))] if PROPERTY.fullmatch(str(a.get('gaProperty') or '')) else []
