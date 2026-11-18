import math
from time import monotonic
from urllib.parse import quote, urlsplit

from skopax.contract import HttpError, ReadError, Transport, Unreachable, anomaly, call

ID = 'crux'
NAME = 'Chrome UX Report'
DEFAULT_PORTS = {'http': 80, 'https': 443}
OFF_ORIGIN = 'url or origin must be an address on a configured origin (the same scheme, host and port)'
HISTORY_URL = 'https://chromeuxreport.googleapis.com/v1/records:queryHistoryRecord'
VIS_UI = 'https://cruxvis.withgoogle.com/#/'
PSI_UI = 'https://pagespeed.web.dev/analysis'
FORMS = ('PHONE', 'DESKTOP')
FORM_WORD = {'PHONE': 'mobile', 'DESKTOP': 'desktop'}
METRICS = {
    'interaction_to_next_paint': ('inp', 'INP', 'ms', 200, 500),
    'largest_contentful_paint': ('lcp', 'LCP', 'ms', 2500, 4000),
    'cumulative_layout_shift': ('cls', 'CLS', '', 0.1, 0.25),
}
SHORTS = tuple(m[0] for m in METRICS.values())
BANDS = ('good', 'needs improvement', 'poor')
PERIODS = 6
RULES = {'worse': 0.2}
RULE_HELP = {'worse': 'A page-speed card is raised when a real-user metric (INP, LCP or CLS at the 75th percentile) is this share worse than a week earlier (0.2 = 20 % worse).'}
TIMEOUT = 30
PASS = 'crux'
SECTION = 'crux'
NO_DATA = f'{PASS}:no-data'
PART = 'origins'
ACCESS = f'{PASS}:access:{PART}'
PERMISSION = 'access to the Chrome UX Report API'
NO_ACCESS = f'{NAME} cannot read {PART}: the API key lacks {PERMISSION}'
DENIED = (401, 403)
DOT = ' \u00b7 '
NONE_SHOWN = '\u2013'
TOOL_FORMS = ('PHONE', 'DESKTOP', 'ALL')
TOOL_PERIODS = 8
MAX_PERIODS = 25
BUDGET_S = 200
TOOL_BUDGET_S = 60
MIN_CALL_S = 5
REFUSED = ' (key refused, API not enabled or origin malformed)'
ENTRY_SHOWN = 60


class CruxError(ReadError):
    def __init__(self, reason: str, status: int = 502):
        super().__init__(status, reason)
        self.reason = reason


class Denied(CruxError):
    def __init__(self, code: int):
        super().__init__(f'{NO_ACCESS} (HTTP {code})', code)
        self.code = code


class OutOfTime(Exception):
    pass


def normal_origin(entry: str) -> str | None:
    try:
        parts = urlsplit(entry if '://' in entry else f'https://{entry}')
        port = parts.port
    except ValueError:
        return None
    scheme = parts.scheme.lower()
    if scheme not in DEFAULT_PORTS or not parts.hostname:
        return None
    host = f'[{parts.hostname}]' if ':' in parts.hostname else parts.hostname
    return f'{scheme}://{host}' + (f':{port}' if port and port != DEFAULT_PORTS[scheme] else '')


def origins_of(raw: str | None) -> tuple[tuple[str, ...], tuple[str, ...]]:
    good, bad = [], []
    for item in (raw or '').split(','):
        entry = item.strip()
        if entry:
            origin = normal_origin(entry)
            if origin:
                good.append(origin)
            else:
                bad.append(entry)
    return tuple(dict.fromkeys(good)), tuple(dict.fromkeys(bad))


def origin_list(raw: str | None) -> tuple[str, ...]:
    return origins_of(raw)[0]


def malformed(entry: str) -> str:
    return f'origin {entry[:ENTRY_SHOWN]} is not an http(s) origin like https://www.example.com'


def number(v) -> float | None:
    if isinstance(v, bool):
        return None
    try:
        x = float(v)
    except (TypeError, ValueError, OverflowError):
        return None
    return None if math.isnan(x) or math.isinf(x) else x


def band(metric: str, v: float) -> int:
    _, _, _, good, poor = METRICS[metric]
    return 0 if v <= good else 1 if v <= poor else 2


def shown(metric: str, v: float | None) -> str:
    if v is None:
        return NONE_SHOWN
    unit = METRICS[metric][2]
    return f'{round(v)} {unit}' if unit else f'{v:.2f}'


def day_of(d: object) -> str | None:
    if not isinstance(d, dict):
        return None
    year, month, day = d.get('year'), d.get('month', 1), d.get('day', 1)
    if not all(isinstance(v, int) and not isinstance(v, bool) for v in (year, month, day)) or not year:
        return None
    return f'{year:04d}-{month:02d}-{day:02d}'


def query(transport: Transport, api_key: str, body: dict, timeout: float) -> object:
    return call(transport, 'POST', f'{HISTORY_URL}?key={quote(api_key, safe="")}', None, body, timeout=timeout).body


class Crux:
    def __init__(self, api_key: str, origins: tuple[str, ...], transport: Transport, budget: float = BUDGET_S, clock=None):
        self.api_key = api_key
        self.origins = origins
        self.transport = transport
        self.clock = clock or monotonic
        self.deadline = self.clock() + budget

    def history(self, key: dict, form: str | None, periods: int = PERIODS) -> dict:
        left = self.deadline - self.clock()
        if left < MIN_CALL_S:
            raise OutOfTime()
        body = key | ({'formFactor': form} if form else {}) | {'metrics': list(METRICS), 'collectionPeriodCount': periods}
        target = key.get('origin') or key.get('url')
        try:
            got = query(self.transport, self.api_key, body, min(TIMEOUT, left))
        except HttpError as e:
            if e.code == 404:
                return {}
            if e.code in DENIED:
                raise Denied(e.code) from None
            raise CruxError(f'CrUX HTTP {e.code}' + (REFUSED if e.code == 400 else '') + f': {target} {form or "ALL"}') from None
        except Unreachable:
            raise CruxError('CrUX: the Chrome UX Report API could not be reached') from None
        record = got.get('record') if isinstance(got, dict) else None
        return record if isinstance(record, dict) else {}


def series(record: dict) -> dict:
    listed = record.get('collectionPeriods')
    out = {'periods': [day_of(p.get('lastDate')) if isinstance(p, dict) else None for p in (listed if isinstance(listed, list) else [])]}
    metrics = record.get('metrics') if isinstance(record.get('metrics'), dict) else {}
    for metric, (short, *_rest) in METRICS.items():
        found = metrics.get(metric) if isinstance(metrics.get(metric), dict) else {}
        timeseries = found.get('percentilesTimeseries') if isinstance(found.get('percentilesTimeseries'), dict) else {}
        p75s = timeseries.get('p75s') if isinstance(timeseries.get('p75s'), list) else []
        out[short] = [number(v) for v in p75s]
    return out


def has_data(s: dict) -> bool:
    return any(v is not None for short in SHORTS for v in s.get(short) or [])


def article(name: str) -> str:
    return 'an' if name[:1].upper() in 'AEFHILMNORSX' else 'a'


def evaluate(origin: str, form: str, s: dict, rules: dict) -> list[dict]:
    found = []
    host = origin.split('://', 1)[-1]
    for metric, (short, name, unit, good, poor) in METRICS.items():
        values = s.get(short) or []
        if len(values) < 2 or values[-1] is None or values[-2] is None:
            continue
        now, before = values[-1], values[-2]
        b_now, b_before = band(metric, now), band(metric, before)
        worse = before > 0 and now >= (1 + rules['worse']) * before
        if not (b_now > b_before or (worse and b_now > 0)):
            continue
        end = (s.get('periods') or [None])[-1]
        word = FORM_WORD.get(form, form.lower())
        x = anomaly(PASS, 'crux', '-', host, f'{word}:{short}', 'warn', round(now, 3), None, None, unit,
                    f'{word} {name} p75',
                    f'Real Chrome users on {word} had {article(name)} {name} p75 of {shown(metric, now)} on {host} in the 28 days to {end or "the latest period"} '
                    f'({BANDS[b_now]}; good is up to {shown(metric, good)}, poor above {shown(metric, poor)}), '
                    f'up from {shown(metric, before)} ({BANDS[b_before]}) in the period a week earlier. '
                    'CrUX is a 28-day rolling window, so a change shows here days after the release that caused it.',
                    1, f'WARN {host} {word} {name} p75 {shown(metric, now)} (week before {shown(metric, before)})')
        x.update(origin=origin, formFactor=form, previous=round(before, 3))
        found.append(x)
    return found


def access_card(code: int) -> dict:
    x = anomaly(PASS, 'access', '-', ID, PART, 'warn', 1, None, None, '', NO_ACCESS,
                f'The Chrome UX Report API refused the API key (HTTP {code}), so page speed was not read and the open page-speed cards stay as they are. '
                'Enable the Chrome UX Report API in the Google Cloud project of the key, and allow it if the key has API restrictions.',
                1, f'WARN {NO_ACCESS}')
    x['ranKey'] = ACCESS
    return x


def run_pass(c: Crux, rules: dict) -> tuple[list[dict], list[dict], list[str], int, int | None]:
    found, rows, empty, read = [], [], [], 0
    for origin in c.origins:
        try:
            got = [(form, series(c.history({'origin': origin}, form))) for form in FORMS]
        except OutOfTime:
            break
        except Denied as e:
            return found, rows, empty, read, e.code
        read += 1
        for form, s in got:
            rows.append({'part': 'trend', 'origin': origin, 'formFactor': form} | s)
            found += evaluate(origin, form, s, rules)
        if not any(has_data(s) for _, s in got):
            empty.append(origin)
    return found, rows, empty, read, None


def links_for(a: dict) -> list[dict] | None:
    if a.get('src') != PASS or a.get('sig') == 'access':
        return None
    origin = str(a.get('origin') or f"https://{a.get('svc')}")
    form = a.get('formFactor') if a.get('formFactor') in FORMS else 'PHONE'
    return [dict(label='CrUX trend', url=f"{VIS_UI}?view=cwvsummary&url={quote(origin, safe='')}&identifier=origin&device={form}"),
            dict(label='PageSpeed Insights', url=f"{PSI_UI}?url={quote(origin + '/', safe='')}")]


def origin_of(url: str) -> str | None:
    try:
        parts = urlsplit(url)
    except ValueError:
        return None
    return normal_origin(f'{parts.scheme}://{parts.netloc.rpartition("@")[2]}') if parts.scheme and parts.netloc else None


def ours(target: str, origins: tuple[str, ...]) -> bool:
    found = origin_of(target)
    return found is not None and found in origins


def wanted(a: dict, origins: tuple[str, ...]) -> dict:
    url, origin = str(a.get('url') or '').strip(), str(a.get('origin') or '').strip().rstrip('/')
    if url and origin:
        raise CruxError('give url or origin, not both', 400)
    if not origins:
        raise CruxError('no origin is set on this instance', 400)
    target = url or origin or origins[0]
    if not ours(target, origins):
        raise CruxError(OFF_ORIGIN, 400)
    form = str(a.get('form') or 'PHONE').upper()
    if form not in TOOL_FORMS:
        raise CruxError(f"form must be one of {', '.join(TOOL_FORMS)}", 400)
    try:
        periods = int(a.get('periods') or TOOL_PERIODS)
    except (TypeError, ValueError, OverflowError):
        periods = 0
    if not 1 <= periods <= MAX_PERIODS:
        raise CruxError(f'periods must be 1-{MAX_PERIODS}', 400)
    return {'key': {'url': url} if url else {'origin': origin_of(target)}, 'form': form, 'periods': periods}


def read(c: Crux, w: dict) -> dict:
    target = w['key'].get('url') or w['key'].get('origin')
    try:
        record = c.history(w['key'], None if w['form'] == 'ALL' else w['form'], w['periods'])
    except OutOfTime:
        raise CruxError('CrUX did not answer in time', 504) from None
    if not record:
        return {'target': target, 'form': w['form'], 'note': 'CrUX has no data for it: too few Chrome visits in 28 days, or the URL is written differently (try the origin)'}
    s = series(record)
    latest = {}
    for metric, (short, name, unit, good, poor) in METRICS.items():
        values = [v for v in s.get(short) or [] if v is not None]
        if values:
            latest[short] = {'p75': shown(metric, values[-1]), 'band': BANDS[band(metric, values[-1])], 'good': shown(metric, good), 'poor': f'above {shown(metric, poor)}'}
    return {'target': target, 'form': w['form'], 'periodEnds': s['periods'], 'p75': {k: s[k] for k in SHORTS}, 'latest': latest,
            'note': 'each value is the p75 of real Chrome users over the 28 days ending on its date; the dates are a week apart'}
