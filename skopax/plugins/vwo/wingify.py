import datetime
import math
import re
import time
from collections.abc import Callable, Mapping
from urllib.parse import urlencode

from skopax.contract import HttpError, PassResult, ReadError, Sweep, Transport, Unreachable, anomaly, call, iso
from skopax.errkind import Authored
from skopax.kit.text import cut

API = 'https://app.wingify.com/api/v2'
APP = 'https://app.wingify.com/'
NAME = 'VWO'
CHECK = 'wingify'
NO_TOKEN = 'no VWO API token is set on this instance'
OPS = ('campaigns', 'campaign', 'report')
TIMEOUT = 20
PAGE = 100
MAX_CAMPAIGNS = 300
WHOLE = (400, 404, 416, 422)
ROWS = 100
NAME_LIMIT = 160
URLS_SHOWN = 5
CHANGES_KEPT = 30
CARD_SECONDS = 24 * 3600
STATUS = re.compile(r'[A-Z_]{2,30}')
ACCOUNT = re.compile(r'current|\d{1,12}')
RUNNING = 'RUNNING'
STOPPED = ('PAUSED', 'STOPPED', 'TRASHED', 'ARCHIVED', 'DELETED')
RAW_KEYS = 80
REPORT_FROM = 'Wingify API'
REPORT_LINES = 40
LINE_LIMIT = 300
DECISION_LIMIT = 120
DECIDED = 'decided by VWO'
LEANING = 'leaning, not decided by VWO'
REPORT_NOTE = ('chance to beat control, lift and their ranges are VWO\'s running numbers. A variation marked "leaning, not decided by VWO" is only leaning: '
               'early highs often fade, so it is no result. Only VWO\'s own decision (winner, decision made, a decision value or smart decision) is a result.')
NO_DECISION = 'VWO has not decided on {scope} yet; every number below is leaning, not decided by VWO'
NO_RESULTS = 'VWO sent no results for this campaign yet'
ARROW = chr(0x2192)
DOT = f' {chr(0xb7)} '
NO_DATA = f'ok{DOT}no data'
PERMISSION = 'Browse permission'
REFUSED = (401, 403)
HINTS = {401: f': the token was refused (it needs {PERMISSION})', 403: f': the token may not read this ({PERMISSION} is enough)', 404: ': not found',
         429: ': rate limited, try again in a minute'}
PARTS = {'account': 'the account', 'campaigns': 'campaigns', 'results': 'campaign results'}
CARRY_S = 7 * 86_400
SHOWN_IDS = 10
CAMPAIGN_KEYS = ('id', 'name', 'type', 'status', 'urls', 'percentTraffic')
CHANGE_KEYS = ('id', 'name', 'type', 'urls', 'from', 'to', 'at', 'edited', 'firstSeen')

VERDICT_PASS = 'wingify-verdict'
VERDICT_SECTION = 'wingifyVerdicts'
VERDICT_EVERY_S = 6 * 3600
VERDICT_SEV = 'warn'
VERDICT_READS = 3
VERDICT_RETRY_S = 3000
VERDICT_WAIT_S = 15
VERDICT_KEPT = 6
VERDICT_KINDS = {'winner': 'A/B test: VWO picked a winner', 'decision': 'A/B test: VWO made a decision'}
ENTRY_KEYS = ('readAt', 'kind', 'lines', 'hit', 'error', 'refused', 'name', 'url', 'seenAt')
BUDGET_S = 200
READ_RESERVE_S = 2 * TIMEOUT + VERDICT_WAIT_S


class WingifyError(Authored, ReadError):
    def __init__(self, reason: str, status: int = 502, code: int | None = None):
        super().__init__(status, reason)
        self.reason = reason
        self.code = code


class RateLimited(WingifyError):
    pass


class Refused(WingifyError):
    pass


def refusal(path: str, code: int) -> WingifyError:
    reason = f'Wingify HTTP {code} on {path or "/"}{HINTS.get(code, "")}'
    kind = Refused if code in REFUSED else RateLimited if code == 429 else WingifyError
    return kind(reason, code=code)


class Wingify:
    def __init__(self, token: str = '', account: str = 'current', url: str = API, transport: Transport | None = None):
        self.token = str(token or '').strip()
        given = str(account or 'current').strip()
        self.account = given if ACCOUNT.fullmatch(given) else 'current'
        self.url = (url or API).rstrip('/')
        self.transport = transport

    @classmethod
    def of(cls, config: Mapping[str, str], transport: Transport) -> 'Wingify':
        return cls(config.get('api-token') or '', config.get('account-id') or 'current', API, transport)

    @property
    def enabled(self) -> bool:
        return bool(self.token)

    def raw(self, path: str, timeout: float = TIMEOUT, **params) -> object:
        if not self.token:
            raise WingifyError(NO_TOKEN, 503)
        query = {k: v for k, v in params.items() if v not in (None, '')}
        url = f'{self.url}/accounts/{self.account}{path}' + ('?' + urlencode(query) if query else '')
        body = call(self.transport, 'GET', url, {'token': self.token}, timeout=timeout).body
        return body.get('_data') if isinstance(body, dict) and '_data' in body else body

    def get(self, path: str, **params) -> object:
        try:
            return self.raw(path, **params)
        except HttpError as e:
            raise refusal(path, e.code) from None
        except Unreachable:
            raise WingifyError(f'Wingify unreachable on {path or "/"}') from None

    def pages(self) -> tuple[list[dict], bool]:
        out: list[dict] = []
        while len(out) < MAX_CAMPAIGNS:
            page = rows_in(self.get('/campaigns', limit=PAGE, offset=len(out)))
            out += page
            if len(page) < PAGE:
                return out, False
        return out, True

    def campaigns(self) -> list[dict]:
        return self.pages()[0]

    def beyond(self, read: int) -> bool:
        try:
            extra = self.get('/campaigns', limit=PAGE, offset=read)
        except (Refused, RateLimited):
            raise
        except WingifyError as e:
            if e.code in WHOLE:
                return False
            raise
        return bool(rows_in(extra))

    def listing(self) -> tuple[list[dict], bool]:
        out, full = self.pages()
        return out, full and self.beyond(len(out))

    def campaign(self, campaign_id: int) -> dict:
        data = self.get(f'/campaigns/{int(campaign_id)}')
        if not isinstance(data, dict):
            raise WingifyError(f'Wingify answered no campaign {int(campaign_id)}')
        return data


def rows_in(data: object) -> list[dict]:
    page = data.get('partialCollection') if isinstance(data, dict) else data
    return [c for c in page or [] if isinstance(c, dict)] if isinstance(page, list) else []


def clip(url, limit: int = 300) -> str:
    return ' '.join(str(url or '').split())[:limit]


def when(seconds) -> str | None:
    try:
        return iso(float(seconds)) if seconds else None
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def seconds_of(text) -> float | None:
    if not isinstance(text, str):
        return None
    try:
        return datetime.datetime.fromisoformat(text).timestamp()
    except ValueError:
        return None


def urls_of(c: dict) -> list[str]:
    found = [clip(c.get('primaryUrl'))] if isinstance(c.get('primaryUrl'), str) and c.get('primaryUrl') else []
    for u in c.get('urls') or []:
        value = clip(u.get('value') if isinstance(u, dict) else u)
        if value and value not in found:
            found.append(value)
    return found[:URLS_SHOWN]


def summary(c: dict) -> dict:
    row = {'id': c.get('id'), 'name': cut(c.get('name'), NAME_LIMIT), 'type': c.get('type'), 'status': c.get('status'), 'platform': c.get('platform'), 'urls': urls_of(c),
           'createdOn': when(c.get('createdOn')), 'percentTraffic': c.get('percentTraffic')}
    return {k: v for k, v in row.items() if v not in (None, [], '')}


def details(c: dict) -> dict:
    out = summary(c)
    out['variations'] = [{k: v for k, v in {'id': v.get('id'), 'name': cut(v.get('name'), NAME_LIMIT), 'isControl': v.get('isControl'), 'percentSplit': v.get('percentSplit')}.items() if v is not None}
                         for v in c.get('variations') or [] if isinstance(v, dict)]
    out['goals'] = [{k: v for k, v in {'id': g.get('id'), 'name': cut(g.get('name'), NAME_LIMIT), 'type': g.get('type')}.items() if v is not None} for g in c.get('goals') or [] if isinstance(g, dict)]
    for key in ('excludedUrls', 'schedules', 'thresholds', 'stats'):
        if c.get(key):
            out[key] = c[key]
    return out


def wanted(a: dict) -> dict:
    op = str(a.get('op') or 'campaigns')
    if op not in OPS:
        raise WingifyError('op is one of ' + ', '.join(OPS), 400)
    if op in ('campaign', 'report'):
        try:
            return {'op': op, 'id': int(a.get('id'))}
        except (TypeError, ValueError, OverflowError):
            raise WingifyError(f'op {op} needs id, the campaign number from op campaigns', 400) from None
    status = str(a.get('status') or '').strip().upper()
    if status and not STATUS.fullmatch(status):
        raise WingifyError('status is a Wingify status such as RUNNING or PAUSED', 400)
    return {'op': op, 'status': status, 'contains': str(a.get('contains') or '').strip().lower()[:100]}


def read(w: Wingify, a: dict) -> dict:
    if a['op'] == 'campaign':
        raw = w.campaign(a['id'])
        return {'campaign': details(raw) | {'rawKeys': sorted(str(k) for k in raw)[:RAW_KEYS]}}
    if a['op'] == 'report':
        return report(w, a['id'])
    listed, full = w.pages()
    more: bool | str = False
    if full:
        try:
            more = w.beyond(len(listed))
        except RateLimited as e:
            more = f'unknown: the read past {MAX_CAMPAIGNS} campaigns was refused for now (HTTP {e.code})'
    rows = [summary(c) for c in listed]
    by_status: dict[str, int] = {}
    for r in rows:
        by_status[str(r.get('status'))] = by_status.get(str(r.get('status')), 0) + 1
    if a['status']:
        rows = [r for r in rows if r.get('status') == a['status']]
    if a['contains']:
        rows = [r for r in rows if a['contains'] in (r.get('name') or '').lower() or any(a['contains'] in u.lower() for u in r.get('urls') or [])]
    rows.sort(key=lambda r: (r.get('status') != RUNNING, -(seconds_of(r.get('createdOn')) or 0)))
    return {'byStatus': by_status, 'campaigns': rows[:ROWS], 'truncated': len(rows) > ROWS} | ({'more': more} if more else {})


def num(v) -> float | None:
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
        return None
    return float(v)


def whole(v) -> int | None:
    n = num(v)
    return int(n) if n is not None and n >= 0 else None


def chance(v) -> float | None:
    n = num(v)
    if n is None or n < 0 or n > 100:
        return None
    return round(n if n <= 1 else n / 100, 4)


def median(v) -> float | None:
    n = num(v.get('median')) if isinstance(v, dict) else num(v)
    return None if n is None else round(n, 5)


def interval(v) -> list[float] | None:
    if not isinstance(v, dict):
        return None
    pair = next((x for k, x in v.items() if str(k).startswith('credibleInterval') and isinstance(x, (list, tuple))), None)
    if not pair or len(pair) != 2:
        return None
    low, high = num(pair[0]), num(pair[1])
    return None if low is None or high is None else [round(low, 5), round(high, 5)]


def ident(v) -> str | None:
    if isinstance(v, bool):
        return None
    if isinstance(v, int):
        return str(v)
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    if isinstance(v, str) and v.strip().isdigit():
        return str(int(v.strip()))
    return None


def name_of(item: dict, kind: str, item_id: str) -> str:
    return cut(item.get('name'), NAME_LIMIT) or f'{kind} {item_id}'


def as_dict(v) -> dict:
    return v if isinstance(v, dict) else {}


def flag(entry: dict, agg: dict, key: str) -> bool:
    return entry.get(key) is True or agg.get(key) is True


def decision_of(entry: dict, agg: dict) -> str | None:
    for v in (agg.get('decision'), entry.get('decision')):
        if v not in (None, False, '', [], {}):
            text = cut(v, DECISION_LIMIT)
            if text:
                return text
    return None


def kept(row: dict) -> dict:
    return {k: v for k, v in row.items() if v is not None and v != {}}


def arm(entry, name: str, control: bool) -> dict:
    entry = as_dict(entry)
    agg = as_dict(entry.get('aggregated'))
    row = {'name': name, 'visitors': whole(agg.get('visitorCount')), 'conversions': whole(agg.get('conversionCount')), 'rate': median(agg.get('conversionRate'))}
    if not entry:
        row['noData'] = True
    vwo = {'winner': flag(entry, agg, 'isWinner'), 'decisionMade': flag(entry, agg, 'isDecisionMade'), 'smartDecision': flag(entry, agg, 'isSmartDecision'),
           'decision': decision_of(entry, agg), 'possibleWinner': flag(entry, agg, 'possibleWinner')}
    decided = vwo['winner'] or vwo['decisionMade'] or vwo['smartDecision'] or vwo['decision'] is not None
    said = {'vwo': {k: v for k, v in vwo.items() if v}, 'status': DECIDED if decided else LEANING}
    if control:
        return kept(row | (said if decided else {}))
    graph = as_dict(agg.get('decisionGraphData'))
    same = graph.get('practicalEquivalancePercentage', graph.get('practicalEquivalencePercentage'))
    row |= {'lift': median(agg.get('relativeImprovementRate')), 'liftRange': interval(agg.get('relativeImprovementRate')),
            'beatChance': chance(graph.get('betterPercentage')), 'worseChance': chance(graph.get('worsePercentage')), 'sameChance': chance(same)} | said
    return kept(row)


def thresholds(c: dict) -> dict:
    t = as_dict(c.get('thresholds'))
    out = {'winChance': chance(t.get('winningPercent')), 'loseChance': chance(t.get('losingPercent')), 'visitors': whole(t.get('visitors'))}
    return {k: v for k, v in out.items() if v}


def results(c: dict) -> dict:
    names: dict[str, str] = {}
    control = None
    for v in c.get('variations') or []:
        vid = ident(v.get('id')) if isinstance(v, dict) else None
        if vid and vid not in names:
            names[vid] = name_of(v, 'variation', vid)
            if control is None and v.get('isControl') is True:
                control = vid
    control = control or '1'
    data: dict[str, dict[str, dict]] = {}
    for e in c.get('variationGoalData') or []:
        gid, vid = (ident(e.get('goal')), ident(e.get('variation'))) if isinstance(e, dict) else (None, None)
        if gid and vid:
            data.setdefault(gid, {})[vid] = e
    goals: dict[str, dict] = {}
    for g in c.get('goals') or []:
        gid = ident(g.get('id')) if isinstance(g, dict) else None
        if gid and gid not in goals:
            goals[gid] = g
    for gid in sorted(set(data) - set(goals), key=int):
        goals[gid] = {}
    out = []
    for gid, g in goals.items():
        row = {'goal': name_of(g, 'goal', gid), 'primary': True if g.get('isPrimary') is True else None, 'type': g.get('type') if isinstance(g.get('type'), str) else None}
        got = data.get(gid) or {}
        if not got:
            out.append(kept(row | {'noData': True}))
            continue
        arms = [vid for vid in names if vid != control] + sorted((vid for vid in got if vid not in names and vid != control), key=int)
        row['control'] = arm(got.get(control), names.get(control) or f'variation {control}', True)
        row['variations'] = [arm(got.get(vid), names.get(vid) or f'variation {vid}', False) for vid in arms]
        out.append(kept(row))
    return kept({'campaign': summary(c), 'statsComplete': c.get('isStatsComplete') if isinstance(c.get('isStatsComplete'), bool) else None,
                 'mab': c.get('isMAB') if isinstance(c.get('isMAB'), bool) else None, 'vwoThresholds': thresholds(c), 'goals': out})


def scope(res: dict) -> list[dict]:
    goals = res.get('goals') or []
    return [g for g in goals if g.get('primary')] or goals


def pct(v) -> str:
    return '?' if v is None else f'{v * 100:.1f} %'


def signed(v) -> str:
    return '?' if v is None else f'{v * 100:+.1f} %'


def count_of(v) -> str:
    return '?' if v is None else f'{v:,}'


def vwo_said(v: dict) -> str:
    vwo = v.get('vwo') or {}
    said = (['winner'] if vwo.get('winner') else []) + ([f'decision "{vwo["decision"]}"'] if vwo.get('decision') else []) + \
           (['decision made'] if vwo.get('decisionMade') and not vwo.get('decision') else []) + (['smart decision'] if vwo.get('smartDecision') else [])
    if v.get('status') == DECIDED:
        return f'{DECIDED}: ' + ', '.join(said)
    return LEANING + (' (VWO marks it a possible winner)' if vwo.get('possibleWinner') else '')


def line(g: dict, v: dict) -> str:
    c = g.get('control') or {}
    goal = g['goal'] + (' (primary goal)' if g.get('primary') else '')
    if v.get('noData'):
        return f'{goal}: "{v["name"]}" has no results yet'[:LINE_LIMIT]
    lift = f"lift {signed(v.get('lift'))}" + (f" ({signed(v['liftRange'][0])} to {signed(v['liftRange'][1])})" if v.get('liftRange') else '')
    text = (f'{goal}: "{v["name"]}" against "{c.get("name", "control")}": conversion {pct(c.get("rate"))} {ARROW} {pct(v.get("rate"))}, {lift}, '
            f'{pct(v.get("beatChance"))} chance to beat control, visitors {count_of(c.get("visitors"))} / {count_of(v.get("visitors"))}; {vwo_said(v)}')
    return text[:LINE_LIMIT]


def control_line(g: dict) -> str:
    c = g.get('control') or {}
    goal = g['goal'] + (' (primary goal)' if g.get('primary') else '')
    return f'{goal}: control "{c.get("name")}": conversion {pct(c.get("rate"))}, visitors {count_of(c.get("visitors"))}; {vwo_said(c)}'[:LINE_LIMIT]


def goal_lines(g: dict) -> list[str]:
    head = [control_line(g)] if (g.get('control') or {}).get('status') == DECIDED else []
    return head + [line(g, v) for v in g.get('variations') or []]


def decisions(res: dict) -> list[dict]:
    hits = []
    for g in scope(res):
        c = g.get('control') or {}
        if c.get('status') == DECIDED:
            hits.append({'kind': 'decision', 'goal': g['goal'], 'variation': c['name'], 'decision': (c.get('vwo') or {}).get('decision'), 'line': control_line(g)})
        for v in g.get('variations') or []:
            if v.get('status') != DECIDED:
                continue
            vwo = v.get('vwo') or {}
            hits.append({'kind': 'winner' if vwo.get('winner') else 'decision', 'goal': g['goal'], 'variation': v['name'], 'decision': vwo.get('decision'), 'line': line(g, v)})
    return sorted(hits, key=lambda h: h['kind'] != 'winner')


def report(w: Wingify, campaign_id: int) -> dict:
    res = results(w.campaign(campaign_id))
    goals = res.get('goals') or []
    lines = [text for g in goals for text in goal_lines(g)]
    hits = decisions(res)
    where = 'the primary goal' if any(g.get('primary') for g in goals) else 'any goal'
    verdict = [h['line'] for h in hits] or [NO_DECISION.format(scope=where) if lines else NO_RESULTS]
    out = {'from': REPORT_FROM} | res | {'verdict': verdict, 'lines': lines[:REPORT_LINES], 'note': REPORT_NOTE}
    if len(lines) > REPORT_LINES:
        out['linesCut'] = True
    return out


def label_of(change: dict) -> str:
    if change.get('edited'):
        return 'A/B test changed'
    if change.get('firstSeen'):
        return 'A/B test first seen'
    if change.get('from') is None:
        return 'A/B test created'
    if change.get('to') == RUNNING:
        return 'A/B test started'
    if change.get('to') in STOPPED:
        return 'A/B test stopped'
    return 'A/B test changed'


def edits(old: dict, new: dict) -> list[str]:
    out = []
    if 'urls' in old and sorted(old.get('urls') or []) != sorted(new.get('urls') or []):
        out.append(f"pages {', '.join(old.get('urls') or []) or 'none'} {ARROW} {', '.join(new.get('urls') or []) or 'none'}")
    if old.get('percentTraffic') is not None and new.get('percentTraffic') is not None and old['percentTraffic'] != new['percentTraffic']:
        out.append(f"traffic share {old['percentTraffic']} % {ARROW} {new['percentTraffic']} %")
    return out


def page_of(urls) -> str | None:
    first = urls[0] if isinstance(urls, list) and urls else None
    return first if isinstance(first, str) and first.startswith('https://') else None


def change_cards(changes: list[dict], now: float) -> list[dict]:
    out = []
    for ch in changes:
        at = seconds_of(ch.get('at'))
        if at is None or now - at >= CARD_SECONDS:
            continue
        label = label_of(ch)
        name = str(ch.get('name') or f"campaign {ch['id']}")
        urls = [str(u) for u in ch.get('urls') or []] if isinstance(ch.get('urls'), list) else []
        where = f" on {', '.join(urls)}" if urls else ''
        moved = (f"was changed while {ch.get('to')}: {'; '.join(str(e) for e in ch['edited'])}" if ch.get('edited')
                 else f"is listed for the first time with status {ch.get('to')}; it may be older, because only the first {MAX_CAMPAIGNS} campaigns are read" if ch.get('firstSeen')
                 else f"is new with status {ch.get('to')}" if ch.get('from') is None else f"went from {ch.get('from')} to {ch.get('to')}")
        x = anomaly(CHECK, 'ab-test', '-', CHECK, f"c{ch['id']}" + ('|edit' if ch.get('edited') else ''), 'info', 1, None, None, '', label,
                    f"Wingify (VWO) {ch.get('type') or 'campaign'} \"{name}\" (#{ch['id']}){where} {moved}. "
                    'An experiment changes what some visitors see on those pages without a deploy; check conversions, page errors and layout cards on them after it.',
                    1, f'INFO {label}: {name[:100]}')
        x.update(url=page_of(urls), campaignId=ch['id'])
        out.append(x)
    return out


def rows_of(previous: list | None, part: str) -> list[dict]:
    return [r for r in previous or [] if isinstance(r, dict) and r.get('part') == part]


def learned(previous: list | None) -> dict[str, dict] | None:
    if not rows_of(previous, 'summary'):
        return None
    return {ident(r.get('id')): r for r in rows_of(previous, 'campaign') if ident(r.get('id'))}


def cut_note(read_count: int) -> str:
    return f'read {read_count} campaigns, more not read'


def older(c: dict, since: float | None) -> bool | None:
    created = seconds_of(c.get('createdOn'))
    return None if since is None or created is None else created < since


def carried(before: dict[str, dict], current: dict, now: float, fallback: str) -> list[dict]:
    out = []
    for cid, r in before.items():
        seen_at = r.get('seenAt') if seconds_of(r.get('seenAt')) is not None else fallback
        if cid not in current and now - seconds_of(seen_at) <= CARRY_S:
            out.append({'part': 'campaign'} | {k: r[k] for k in CAMPAIGN_KEYS if r.get(k) is not None} | {'seenAt': seen_at})
    return out


def changes_of(previous: list | None) -> list[dict]:
    return [{k: r[k] for k in CHANGE_KEYS if k in r} for r in rows_of(previous, 'change') if r.get('id') is not None and seconds_of(r.get('at')) is not None]


def access_card(check: str, part: str, code: object, which: str = '') -> dict:
    label = f"{NAME} cannot read {PARTS[part]}: the token {'is invalid or lacks' if code == 401 else 'lacks'} {PERMISSION}"
    answered = f'answered HTTP {code}' if isinstance(code, int) and not isinstance(code, bool) else 'refused the token'
    x = anomaly(CHECK, 'no-access', '-', 'vwo', f'{check}:{part}', 'warn', 1, None, None, '', label,
                f'The VWO API {answered} when the {check} check read {PARTS[part]}{which}. The token needs {PERMISSION}; '
                f'until it can read them, the {check} check leaves its open cards as they are.', 1, f'WARN {label}')
    x.update(ranKey=f'{check}:access:{part}')
    return x


def access_ran(check: str, *parts: str) -> tuple[str, ...]:
    return tuple(f'{check}:access:{part}' for part in parts)


def run_pass(w: Wingify, previous: list | None, now: float) -> Sweep:
    try:
        listing, cut_off = w.listing()
    except Refused as e:
        return Sweep(PassResult(CHECK, True, f'ok{DOT}no access to {PARTS["campaigns"]}', [access_card(CHECK, 'campaigns', e.code)], {}, access_ran(CHECK, 'campaigns')))
    except WingifyError as e:
        return Sweep(PassResult(CHECK, False, f'error: {e.reason}'))
    listed = [c for c in listing if ident(c.get('id'))]
    if not listed:
        return Sweep(PassResult(CHECK, True, NO_DATA, [], {}, (f'{CHECK}:no-data',) + access_ran(CHECK, 'campaigns')))
    current = {ident(c['id']): summary(c) for c in listed}
    before = learned(previous)
    prior = (rows_of(previous, 'summary') or [{}])[0]
    slid = bool(prior.get('cut')) or cut_off
    checked = prior.get('checkedAt') if seconds_of(prior.get('checkedAt')) is not None else iso(now)
    changes = changes_of(previous)
    if before is not None:
        for cid, c in current.items():
            old = before.get(cid)
            known_older = older(c, seconds_of(prior.get('checkedAt'))) if slid and old is None else False
            if known_older:
                continue
            row = {'id': c['id'], 'name': c.get('name') or f'campaign {cid}', 'type': c.get('type'), 'urls': c.get('urls') or [], 'to': c.get('status'), 'at': iso(now)}
            if old is None and known_older is None:
                changes.insert(0, row | {'from': None, 'firstSeen': True})
            elif old is None or old.get('status') != c.get('status'):
                changes.insert(0, row | {'from': None if old is None else old.get('status')})
            elif c.get('status') == RUNNING and edits(old, c):
                changes.insert(0, row | {'from': old.get('status'), 'edited': edits(old, c)})
    changes = changes[:CHANGES_KEPT]
    rows = [{'part': 'summary', 'checkedAt': iso(now), 'running': sum(1 for c in current.values() if c.get('status') == RUNNING), 'total': len(current)} | ({'cut': True} if cut_off else {})]
    rows += [{'part': 'campaign'} | {k: c[k] for k in CAMPAIGN_KEYS if c.get(k) is not None} | {'seenAt': iso(now)} for c in current.values()]
    if cut_off and before:
        rows += carried(before, current, now, checked)
    rows += [{'part': 'change'} | ch for ch in changes]
    status = DOT.join(['ok'] + ([cut_note(len(listing))] if cut_off else []))
    return Sweep(PassResult(CHECK, True, status, change_cards(changes, now), {}, (CHECK,) + access_ran(CHECK, 'campaigns')), sections={CHECK: rows})


def links_for(card: dict) -> list[dict] | None:
    if card.get('src') != CHECK:
        return None
    page = [{'label': 'Open the page', 'url': card['url']}] if str(card.get('url') or '').startswith('https://') else []
    return page + [{'label': 'VWO', 'url': APP}]


def verdict_entry(res: dict, now: float) -> dict:
    hits = decisions(res)
    if not hits:
        return {'readAt': now}
    top = hits[0]
    return {'readAt': now, 'kind': top['kind'], 'lines': [h['line'] for h in hits][:VERDICT_KEPT],
            'hit': {k: top[k] for k in ('goal', 'variation', 'decision') if top.get(k)}}


def verdict_card(c: dict, got: dict, read_at: float) -> dict:
    kind, lines, hit = got['kind'], [str(x) for x in got.get('lines') or []], as_dict(got.get('hit'))
    label = VERDICT_KINDS[kind]
    x = anomaly(CHECK, 'ab-verdict', '-', CHECK, f"c{c['id']}", VERDICT_SEV, 1, None, None, '', label,
                f"The Wingify (VWO) results of \"{c.get('name')}\" (#{c['id']}), read {iso(read_at)} from the Wingify API, carry VWO's own decision: {' / '.join(lines)}. "
                f"{'The campaign is still running' if c.get('status') == RUNNING else 'The campaign was running when it was last listed'}; "
                "the team that owns it decides whether to stop it or roll out a variation.", 1, f"{VERDICT_SEV.upper()} {label}: {str(c.get('name'))[:100]}")
    x.update(campaignId=c['id'], campaignName=cut(c.get('name'), NAME_LIMIT), verdict=lines, verdictKind=kind, goalName=hit.get('goal'), variationName=hit.get('variation'),
             decision=hit.get('decision'), ranKey=VERDICT_PASS, url=page_of(c.get('urls')))
    return x


def due_at(entry: dict) -> float:
    return (num(entry.get('readAt')) or 0) + (VERDICT_RETRY_S if entry.get('error') else VERDICT_EVERY_S)


def entries_of(previous: list | None) -> dict[str, dict]:
    return {ident(r.get('id')): {k: r[k] for k in ENTRY_KEYS if k in r} for r in previous or [] if isinstance(r, dict) and ident(r.get('id'))}


def about(c: dict, cid: str, now: float) -> dict:
    url = page_of(c.get('urls'))
    return {'name': c.get('name') or f'campaign {cid}', 'seenAt': now} | ({'url': url} if url else {})


def unlisted(cid: str, entry: dict) -> dict:
    return {'id': int(cid), 'name': entry.get('name') or f'campaign {cid}', 'urls': [entry['url']] if isinstance(entry.get('url'), str) else []}


def recent(entry: dict, now: float) -> bool:
    return now - (num(entry.get('seenAt')) or num(entry.get('readAt')) or now) <= CARRY_S


def kept_of(old: dict) -> dict:
    return {k: old[k] for k in ('kind', 'lines', 'hit') if k in old}


def denied_cards(seen: dict[str, dict], ids: list[str]) -> list[dict]:
    denied = [i for i in ids if (seen.get(i) or {}).get('refused')]
    if not denied:
        return []
    shown = ', '.join(f'#{i}' for i in denied[:SHOWN_IDS]) + (f' and {len(denied) - SHOWN_IDS} more' if len(denied) > SHOWN_IDS else '')
    return [access_card(VERDICT_PASS, 'results', seen[denied[0]].get('refused'), f' of {shown}')]


def verdict_pass(w: Wingify, previous: list | None, now: float, reads: int = VERDICT_READS, sleep: Callable[[float], None] = time.sleep,
                 clock: Callable[[], float] = time.monotonic) -> Sweep:
    started = clock()
    ran = access_ran(VERDICT_PASS, 'campaigns', 'results')
    seen = entries_of(previous)
    try:
        listing, cut_off = w.listing()
    except Refused as e:
        cards = [access_card(VERDICT_PASS, 'campaigns', e.code)] + denied_cards(seen, list(seen))
        parts = ', '.join(PARTS[p] for p in ('campaigns', 'results')[:len(cards)])
        return Sweep(PassResult(VERDICT_PASS, True, f'ok{DOT}no access to {parts}', cards, {}, ran))
    except WingifyError as e:
        return Sweep(PassResult(VERDICT_PASS, False, f'error: {e.reason}'))
    listed = [c for c in listing if ident(c.get('id'))]
    if not listed:
        return Sweep(PassResult(VERDICT_PASS, True, NO_DATA, [], {}, (f'{VERDICT_PASS}:no-data',) + access_ran(VERDICT_PASS, 'campaigns')))
    running = [summary(c) for c in listed if c.get('status') == RUNNING]
    due = sorted((c for c in running if now >= due_at(seen.get(ident(c['id'])) or {})),
                 key=lambda c: num((seen.get(ident(c['id'])) or {}).get('readAt')) or 0)[:reads]
    failures, done, answered, refused = [], len(due), False, False
    for index, c in enumerate(due):
        if clock() - started + READ_RESERVE_S > BUDGET_S:
            done = index
            break
        cid = ident(c['id'])
        old = seen.get(cid) or {}
        for attempt in (1, 2):
            try:
                seen[cid] = verdict_entry(results(w.campaign(int(cid))), now)
                answered = True
            except RateLimited as e:
                if attempt == 1:
                    sleep(VERDICT_WAIT_S)
                    continue
                failures.append(f'#{cid}: {e.reason[:120]}, twice, so it is read first next run')
            except Refused as e:
                seen[cid] = kept_of(old) | {'readAt': now, 'error': e.reason[:200], 'refused': e.code}
                refused = True
            except WingifyError as e:
                seen[cid] = kept_of(old) | {'readAt': now, 'error': e.reason[:200]}
                failures.append(f'#{cid}: {e.reason[:120]}')
            break
    if answered and not refused:
        for entry in seen.values():
            entry.pop('refused', None)
    shown = {ident(c['id']): c for c in running}
    known = {ident(c['id']) for c in listed}
    for cid, c in shown.items():
        if cid in seen:
            seen[cid] |= about(c, cid, now)
    seen = {k: v for k, v in seen.items() if k in shown or (cut_off and k not in known and recent(v, now))}
    cards = [verdict_card(shown.get(k) or unlisted(k, v), v, num(v.get('readAt')) or now) for k, v in seen.items() if v.get('kind') in VERDICT_KINDS]
    denied = denied_cards(seen, list(seen))
    notes = ([f'no access to {PARTS["results"]}'] if denied else []) + ([cut_note(len(listing))] if cut_off else []) + \
            ([f"{len(failures)} result read{'' if len(failures) == 1 else 's'} failed: " + '; '.join(failures)] if failures else []) + \
            ([f'read {done} of {len(due)} {PARTS["results"]}'] if done < len(due) else [])
    rows = [{'id': k} | v for k, v in seen.items()]
    return Sweep(PassResult(VERDICT_PASS, True, DOT.join(['ok'] + notes)[:300], cards + denied, {}, (VERDICT_PASS,) + ran), sections={VERDICT_SECTION: rows})
