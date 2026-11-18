import json
import time
from collections.abc import Callable, Mapping
from time import monotonic
from urllib.parse import quote, urlsplit

from skopax.contract import HttpError, PassResult, ReadError, Sweep, Transport, Unreachable, anomaly, call, iso
from skopax.remote import call_text

ID = 'safe-browsing'
NAME = 'Safe Browsing'
STATUS_URL = 'https://transparencyreport.google.com/transparencyreport/api/v3/safebrowsing/status'
API_URL = 'https://safebrowsing.googleapis.com/v4/threatMatches:find'
THREATS = {'MALWARE': 'malware', 'UNWANTED_SOFTWARE': 'unwanted software', 'SOCIAL_ENGINEERING': 'social engineering (Deceptive pages)',
           'POTENTIALLY_HARMFUL_APPLICATION': 'harmful apps'}
REPORT_UI = 'https://transparencyreport.google.com/safe-browsing/search'
CONSOLE_UI = 'https://search.google.com/search-console/security-issues'
USER_AGENT = 'Mozilla/5.0 (compatible; site-checks/0.1)'
CLIENT = {'clientId': 'site-checks', 'clientVersion': '0.1.0'}
TIMEOUT = 15
STATUS_LIMIT = 256 * 1024
API_URLS = 500
URLS_SHOWN = 3
SPACING_S = 3.0
THREAT_TYPE_MAX = 40
FLAGS = ((2, 'malware'), (3, 'unwanted software'), (4, 'social engineering (Deceptive pages)'), (5, 'harmful apps'))
CLEAN = 1
LISTED = 3
PASS = 'safebrowsing'
SECTION = 'safeBrowsing'
NO_DATA = f'{PASS}:no-data'
PART = 'hosts'
ACCESS = f'{PASS}:access:{PART}'
PERMISSION = 'access to the Safe Browsing API'
NO_ACCESS = f'{NAME} cannot read {PART}: the API key lacks {PERMISSION}'
DENIED = (401, 403)
REFUSED = ' (key refused or API not enabled)'
PUSHED = '(rate limited?)'
OUT_OF_TIME = 'out of time'
DOT = ' \u00b7 '
TOOL_URLS = 20
BUDGET_S = 200
TOOL_BUDGET_S = 60


class NotChecked(ReadError):
    def __init__(self, reason: str, status: int = 502):
        super().__init__(status, reason)
        self.reason = reason


class Denied(NotChecked):
    def __init__(self, code: int):
        super().__init__(f'{NO_ACCESS} (HTTP {code})', code)
        self.code = code


def pause(seconds: float) -> None:
    time.sleep(seconds)


def host_list(raw: str | None) -> tuple[str, ...]:
    out = []
    for item in (raw or '').split(','):
        host = item.strip().lower()
        if '/' in host:
            try:
                host = urlsplit(host if '://' in host else f'//{host}').hostname or ''
            except ValueError:
                host = ''
        if host:
            out.append(host)
    return tuple(dict.fromkeys(out))


def snapshot_at(value: object) -> str | None:
    if not isinstance(value, (int, float)) or isinstance(value, bool) or value <= 0:
        return None
    try:
        return iso(value / 1000)
    except (OverflowError, ValueError, OSError):
        return None


def parse(host: str, text: str) -> dict:
    body = text.split('\n', 1)[1] if text.startswith(")]}'") and '\n' in text else text
    try:
        rows = json.loads(body)
        row = rows[0]
        if row[0] != 'sb.ssr' or not isinstance(row[1], int) or isinstance(row[1], bool):
            raise ValueError
    except (ValueError, IndexError, TypeError, KeyError, RecursionError):
        raise NotChecked(f'{host}: not a Safe Browsing status {PUSHED}') from None
    flags = [word for i, word in FLAGS if len(row) > i and row[i] is True]
    return {'host': host, 'status': row[1], 'flags': flags, 'checkedAt': snapshot_at(row[7] if len(row) > 7 else None)}


def threat_url(m: dict) -> str:
    threat = m.get('threat')
    url = threat.get('url') if isinstance(threat, dict) else None
    return url if isinstance(url, str) else ''


def threat_word(m: dict) -> str:
    name = m.get('threatType')
    return THREATS.get(name) or str(name)[:THREAT_TYPE_MAX].lower()


def host_of(url: str) -> str | None:
    try:
        return urlsplit(url).hostname
    except ValueError:
        return None


class SafeBrowsing:
    def __init__(self, hosts: tuple[str, ...], api_key: str, transport: Transport, sleep: Callable[[float], None] = pause, clock: Callable[[], float] = monotonic,
                 budget: float = BUDGET_S):
        self.hosts = hosts
        self.api_key = api_key
        self.transport = transport
        self.sleep = sleep
        self.clock = clock
        self.deadline = clock() + budget

    @classmethod
    def of(cls, config: Mapping[str, str], transport: Transport, budget: float = BUDGET_S) -> 'SafeBrowsing':
        return cls(host_list(config.get('hosts')), (config.get('api-key') or '').strip(), transport, pause, monotonic, budget)

    def left(self) -> float:
        return self.deadline - self.clock()

    def wait(self) -> float:
        return max(1.0, min(TIMEOUT, self.left()))

    def lookup(self, host: str) -> dict:
        try:
            text = call_text(self.transport, f'{STATUS_URL}?site={quote(host, safe="")}', {'User-Agent': USER_AGENT}, self.wait(), STATUS_LIMIT)
        except HttpError as e:
            raise NotChecked(f'{host}: HTTP {e.code} {PUSHED}') from None
        except Unreachable:
            raise NotChecked(f'{host}: status page unreachable') from None
        return parse(host, text)

    def find(self, urls: list[str]) -> list[dict]:
        body = {'client': dict(CLIENT), 'threatInfo': {'threatTypes': list(THREATS), 'platformTypes': ['ANY_PLATFORM'], 'threatEntryTypes': ['URL'],
                                                       'threatEntries': [{'url': u} for u in urls]}}
        got = call(self.transport, 'POST', f'{API_URL}?key={quote(self.api_key, safe="")}', None, body, timeout=self.wait()).body
        found = got.get('matches') if isinstance(got, dict) else None
        return [m for m in found if isinstance(m, dict)] if isinstance(found, list) else []

    def matches(self, urls: list[str]) -> list[dict]:
        try:
            return self.find(urls)
        except HttpError as e:
            if e.code in DENIED:
                raise Denied(e.code) from None
            raise NotChecked(f'Safe Browsing API HTTP {e.code}' + (REFUSED if e.code == 400 else '')) from None
        except Unreachable:
            raise NotChecked('Safe Browsing API unreachable') from None

    def lookup_all(self, hosts: tuple[str, ...]) -> list[dict]:
        found = self.matches([f'https://{h}/' for h in hosts])
        rows = []
        for h in hosts:
            mine = [m for m in found if host_of(threat_url(m)) == h]
            flags = sorted({threat_word(m) for m in mine})
            urls = list(dict.fromkeys(threat_url(m) for m in mine))
            rows.append({'host': h, 'status': LISTED if flags else CLEAN, 'flags': flags, 'checkedAt': None} | ({'urls': urls} if urls else {}))
        return rows


def card(r: dict) -> dict | None:
    if r['status'] == CLEAN and not r['flags']:
        return None
    sev = 'crit' if r['flags'] else 'warn'
    listed = ', '.join(r['flags']) or 'unsafe content on some pages'
    urls = r.get('urls') or []
    what = (f"Google Safe Browsing lists {r['host']}: {listed} (status {r['status']}"
            + (f", Google's snapshot {r['checkedAt'][:16].replace('T', ' ')} UTC" if r.get('checkedAt') else '') + ')'
            + (f": {', '.join(urls[:URLS_SHOWN])}" + (f' and {len(urls) - URLS_SHOWN} more' if len(urls) > URLS_SHOWN else '') if urls else '') + '. '
            'Chrome shows visitors a red warning page while the listing lasts. Status 3 means some pages, not the whole host; '
            'the Security issues report in Search Console names the cause and is where a review is requested.')
    x = anomaly(PASS, 'safebrowsing', '-', r['host'], 'listed', sev, 1, None, None, '', 'Safe Browsing listing', what, 1,
                f"{sev.upper()} {r['host']} listed by Google Safe Browsing: {listed}")
    x['ranKey'] = f"{PASS}:{r['host']}"
    x['host'] = r['host']
    return x


def access_card(code: int) -> dict:
    x = anomaly(PASS, 'access', '-', ID, PART, 'warn', 1, None, None, '', NO_ACCESS,
                f'The Safe Browsing API refused the API key (HTTP {code}), so no host was checked and the open listing cards stay as they are. '
                'Enable the Safe Browsing API in the Google Cloud project of the key, and allow it if the key has API restrictions, or clear the key to read '
                "Google's public site status page instead.",
                1, f'WARN {NO_ACCESS}')
    x['ranKey'] = ACCESS
    return x


def leftovers(cards: list[dict], hosts: tuple[str, ...]) -> tuple[str, ...]:
    gone = set()
    for c in cards:
        if not isinstance(c, dict) or c.get('src') != PASS or c.get('sig') == 'access':
            continue
        host = c.get('host') or c.get('svc')
        if isinstance(host, str) and host and host not in hosts:
            gone.add(host)
    return tuple(f'{PASS}:{h}' for h in sorted(gone))


def section(rows: list[dict], now: float, via: str, **extra) -> dict[str, list[dict]]:
    return {SECTION: [{'part': 'host'} | r for r in rows] + [{'part': 'summary', 'at': iso(now), 'via': via} | extra]}


def run_pass(sb: SafeBrowsing, now: float, cards: list[dict]) -> Sweep:
    total = len(sb.hosts)
    gone = leftovers(cards, sb.hosts)
    if not total:
        return Sweep(PassResult(PASS, True, f'ok{DOT}no data', [], {}, (NO_DATA,) + gone + (ACCESS,)))
    if sb.api_key:
        hosts = sb.hosts[:API_URLS]
        try:
            rows = sb.lookup_all(hosts)
        except Denied as e:
            return Sweep(PassResult(PASS, True, f'ok{DOT}no access to {PART}', [access_card(e.code)], {}, gone + (ACCESS,)))
        except NotChecked as e:
            return Sweep(PassResult(PASS, False, f'error: {PART}: {e.reason}'))
        found = [x for x in (card(r) for r in rows) if x]
        status = 'ok' if len(rows) == total else f'ok{DOT}read {len(rows)} of {total} hosts'
        ran = tuple(f'{PASS}:{r["host"]}' for r in rows) + gone + (ACCESS,)
        return Sweep(PassResult(PASS, True, status, found, {}, ran), sections=section(rows, now, 'api'))
    found, ran, rows, stopped = [], [], [], None
    for i, host in enumerate(sb.hosts):
        if i:
            if sb.left() < TIMEOUT + SPACING_S:
                stopped = OUT_OF_TIME
                break
            sb.sleep(SPACING_S)
        try:
            r = sb.lookup(host)
        except NotChecked as e:
            stopped = e.reason
            break
        rows.append(r)
        ran.append(f'{PASS}:{host}')
        x = card(r)
        if x:
            found.append(x)
    if not rows:
        return Sweep(PassResult(PASS, False, f'error: {PART}: {stopped}'))
    if stopped and stopped != OUT_OF_TIME:
        status = f'error: {PART}: {stopped}'
    else:
        status = 'ok' if len(rows) == total else f'ok{DOT}read {len(rows)} of {total} hosts'
    extra = {'stopped': stopped} if stopped else {}
    return Sweep(PassResult(PASS, True, status, found, {}, tuple(ran) + gone + (ACCESS,)), sections=section(rows, now, 'status page', **extra))


def links_for(a: dict) -> list[dict] | None:
    if a.get('src') != PASS or a.get('sig') == 'access':
        return None
    return [dict(label='Safe Browsing site status', url=f"{REPORT_UI}?url={quote(str(a.get('host') or a.get('svc') or ''), safe='')}"),
            dict(label='Search Console security issues', url=CONSOLE_UI)]


def wanted(a: dict) -> dict:
    given = a.get('urls')
    if given is None or given == []:
        raise NotChecked('urls needs at least one address', 400)
    if not isinstance(given, list):
        raise NotChecked('urls must be a list of addresses', 400)
    urls = [str(u).strip() for u in given if str(u).strip()]
    if not urls:
        raise NotChecked('urls needs at least one address', 400)
    if len(urls) > TOOL_URLS:
        raise NotChecked(f'at most {TOOL_URLS} urls per call', 400)
    for u in urls:
        try:
            parts = urlsplit(u)
            ok = parts.scheme in ('http', 'https') and bool(parts.hostname)
        except ValueError:
            ok = False
        if not ok:
            raise NotChecked(f'{u[:100]} is not an http(s) address', 400)
    return {'urls': list(dict.fromkeys(urls))}


def read(sb: SafeBrowsing, w: dict) -> dict:
    found = sb.matches(w['urls'])
    rows = []
    for u in w['urls']:
        threats = sorted({threat_word(m) for m in found if threat_url(m) == u})
        rows.append({'url': u, 'flagged': bool(threats), 'threats': threats})
    return {'urls': rows, 'note': 'the API answers only for these exact addresses (and their parent paths and host); Search Console > Security issues has the full picture'}
