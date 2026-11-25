import datetime as dt
import re
from collections.abc import Callable
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from skopax import clock
from skopax.contract import InstanceInfo, PassResult, ReadError, Sweep, anomaly, card_key, iso
from skopax.kit.google import GoogleAuth, GoogleError, access_card, access_key
from skopax.kit.web import CHALLENGE, TRACKING, PageCheck, PageError, Site, decoded, host_of, public_host, refusal
from skopax.redact import redact

ADS_API = 'https://googleads.googleapis.com'
ADS_UI = 'https://ads.google.com/aw/ads'
POLICY_UI = 'https://ads.google.com/aw/policymanager'
SCOPES = ('https://www.googleapis.com/auth/adwords',)
VERSION = 'v26'
NAME = 'Google Ads'
PART = 'account'
DOT = ' \u00b7 '
LINKS = 'gads'
POLICY = 'gads-policy'
BROKEN = 'ad-link'
MOVED = 'ad-link-moved'
UNKNOWN = 'unknown'
DISAPPROVED = 'ad-disapproved'
CHECK_WORDS = {LINKS: 'ad-link check', POLICY: 'policy check'}
NS = 'ads'
NEEDS = 'Read only access to the Google Ads account'
NO_ACCESS = ('the service account has no access to this Google Ads account: add it as a Read only user under Admin, Access and security, '
             'and check login-customer-id and that the Cloud project has Google Ads API access')
BAD_CUSTOMER = 'customer-id must be the 10-digit Google Ads customer id, with or without dashes'
BAD_LOGIN = 'login-customer-id must be the 10-digit id of the manager account, with or without dashes, or empty'
BAD_VERSION = 'api-version must be a Google Ads API version such as v26'
BAD_TOKEN = 'developer-token, when set, must be 10 to 100 letters, digits, - and _'
OPS = ('campaigns', 'daily', 'ads')
QUERY = 'query'
NO_MONEY = 'Google Ads cost, budget, billing and conversion value data are not read by Skopax'
MONEY_WORD = re.compile(r'cost|(?<![a-z])cp[a-z]|_cp[mcv]|bid|budget|target_cpa|target_roas|target_spend|value|micros|invoice|billing|payments|profit|revenue|margin|cart|price|orders')
MONEY_FIELDS = ('recommendation.impact', 'change_event.new_resource', 'change_event.old_resource')
MONEY_KEY = re.compile(r'cost|budget|billing|bid|micros$|cp[cvmea]|targetcpa|targetroas|[a-z]value|value[a-z]|amount|profit|revenue|margin|cart|price|^orders$', re.I)
LITERAL = re.compile(r"'(?:[^'\\]|\\.)*'|\"(?:[^\"\\]|\\.)*\"")
DEFAULT_DAYS = 7
MAX_DAYS = 90
MAX_ROWS = 200
MAX_PAGES = 10
CAMPAIGN_ROWS = 50
TEXT_LIMIT = 300
URL_LIMIT = 300
NAME_LIMIT = 120
ERROR_LIMIT = 200
MAX_URLS = 60
GAP_S = 1.0
TIMEOUT_S = 900
DEADLINE_S = 0.8 * TIMEOUT_S
CAMPAIGNS_SHOWN = 3
TOPICS_SHOWN = 3
AD_LINES = 10
ADS_KEPT = 20
DESTINATIONS = 5
GONE = (404, 410)
BLOCKED = (401, 403, 407, 429)
PAGES = 'pages'
NAMED = 3
URL_SHOWN = 120
DOWN_TOPIC = 'DESTINATION_NOT_WORKING'
CUSTOMER = re.compile(r'\d{10}')
ID_TEXT = re.compile(r'[0-9 -]{10,16}')
API_VERSION = re.compile(r'v\d{1,3}')
DEV_TOKEN = re.compile(r'[A-Za-z0-9_-]{10,100}')
SELECT = re.compile(r'^\s*SELECT\s', re.I)
LIMIT = re.compile(r'\bLIMIT\s+\d+\s*$', re.I)
WEB = re.compile(r'https?://', re.I)
DOTTED = re.compile(r'\s*\.\s*')
TOKEN = re.compile(r'[a-z_][a-z0-9_.]*')
TOPIC = re.compile(r'[^A-Z0-9]+')
ACCOUNT_QUERY = 'SELECT customer.id, customer.descriptive_name FROM customer LIMIT 1'
POLICY_QUERY = ('SELECT campaign.id, campaign.name, ad_group.id, ad_group.name, ad_group_ad.ad.id, ad_group_ad.ad.final_urls, ad_group_ad.policy_summary.approval_status, '
                "ad_group_ad.policy_summary.policy_topic_entries FROM ad_group_ad WHERE ad_group_ad.status = 'ENABLED' AND ad_group.status = 'ENABLED' "
                f"AND campaign.status = 'ENABLED' AND ad_group_ad.policy_summary.approval_status = 'DISAPPROVED' LIMIT {MAX_ROWS + 1}")


class AdsError(ReadError):
    def __init__(self, message: str, status: int = 400):
        super().__init__(status, message)
        self.reason = message


def digits(value) -> str:
    return re.sub(r'\D', '', str(value or ''))


def shown_id(customer: str) -> str:
    return f'{customer[:3]}-{customer[3:6]}-{customer[6:]}' if CUSTOMER.fullmatch(customer) else customer


def id_ok(text: str) -> bool:
    return bool(ID_TEXT.fullmatch(text)) and CUSTOMER.fullmatch(digits(text)) is not None


def cut(value, limit: int = NAME_LIMIT) -> str:
    return redact(' '.join(str(value or '').split()))[:limit]


class GoogleAds:
    def __init__(self, auth: GoogleAuth, developer_token: str = '', customer_id: str = '', login_customer_id: str = '', version: str = ''):
        self.auth = auth
        self.developer_token = str(developer_token or '').strip()
        self.given = (str(customer_id or '').strip(), str(login_customer_id or '').strip())
        self.customer_id = digits(customer_id)
        self.login_customer_id = digits(login_customer_id)
        self.version = str(version or '').strip() or VERSION
        auth.hide(self.developer_token)

    @property
    def shown(self) -> str:
        return f'the account {shown_id(self.customer_id)}'

    def problem(self) -> str | None:
        customer, login = self.given
        if not id_ok(customer):
            return BAD_CUSTOMER
        if login and not id_ok(login):
            return BAD_LOGIN
        if not API_VERSION.fullmatch(self.version):
            return BAD_VERSION
        if self.developer_token and not DEV_TOKEN.fullmatch(self.developer_token):
            return BAD_TOKEN
        return None

    def headers(self) -> dict[str, str]:
        out = {'developer-token': self.developer_token} if self.developer_token else {}
        if CUSTOMER.fullmatch(self.login_customer_id):
            out['login-customer-id'] = self.login_customer_id
        return out

    def search(self, query: str, limit: int = MAX_ROWS) -> tuple[list[dict], bool]:
        url = f'{ADS_API}/{self.version}/customers/{self.customer_id}/googleAds:search'
        rows: list[dict] = []
        token = ''
        for _ in range(MAX_PAGES):
            body = {'query': query} | ({'pageToken': token} if token else {})
            got = self.auth.call('POST', url, body, NAME, NO_ACCESS, self.headers(), needs=NEEDS)
            rows += [r for r in got.get('results') or [] if isinstance(r, dict)]
            more = got.get('nextPageToken')
            token = more if isinstance(more, str) else ''
            if not token or len(rows) > limit:
                break
        return rows[:limit], len(rows) > limit or bool(token)

    def account(self) -> str:
        rows, _ = self.search(ACCOUNT_QUERY, 1)
        customer = rows[0].get('customer') if rows else None
        name = cut(customer.get('descriptiveName')) if isinstance(customer, dict) else ''
        return name or f'account {shown_id(self.customer_id)}'


def day_of(now: float) -> dt.date:
    return clock.local(now).date()


def window(a: dict, today: dt.date) -> tuple[str, str, int]:
    try:
        days = int(a.get('days') or DEFAULT_DAYS)
    except (TypeError, ValueError):
        raise AdsError('days is a whole number') from None
    days = max(1, min(days, MAX_DAYS))
    return (today - dt.timedelta(days=days)).isoformat(), (today - dt.timedelta(days=1)).isoformat(), days


def money_token(token: str) -> bool:
    return MONEY_WORD.search(token) is not None or any(token == f or token.startswith(f + '.') for f in MONEY_FIELDS)


def reads_money(query: str) -> bool:
    flat = DOTTED.sub('.', LITERAL.sub(' ', query.lower()))
    return '*' in flat or "'" in flat or '"' in flat or any(money_token(t) for t in TOKEN.findall(flat))


def unpriced(value, depth: int = 0):
    if depth > 12:
        return None
    if isinstance(value, list):
        return [unpriced(v, depth + 1) for v in value]
    if isinstance(value, dict):
        return {k: unpriced(v, depth + 1) for k, v in value.items() if not (isinstance(k, str) and MONEY_KEY.search(k))}
    return value


def free_query(a: dict) -> dict:
    q = str(a.get('query') or '').strip()
    if not SELECT.match(q):
        raise AdsError('query is one GAQL SELECT (read only)')
    if ';' in q:
        raise AdsError('one GAQL statement, without ;')
    if reads_money(q):
        raise AdsError(NO_MONEY)
    return {'op': QUERY, 'query': q if LIMIT.search(q) else f'{q} LIMIT {MAX_ROWS}'}


def wanted(a: dict) -> dict:
    op = str(a.get('op') or 'campaigns')
    if op not in OPS:
        raise AdsError('op is one of ' + ', '.join(OPS))
    return {'op': op, 'days': a.get('days'), 'campaign': str(a.get('campaign') or '').strip()[:100], 'url': str(a.get('url_contains') or '').strip()[:200]}


def num(value) -> float | int | None:
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return int(f) if f.is_integer() else round(f, 2)


def quoted(text: str) -> str:
    return re.sub(r"[^\w\s.\-/]", '', text, flags=re.UNICODE)


def metrics(m: dict) -> dict:
    return {'clicks': num(m.get('clicks')), 'impressions': num(m.get('impressions')), 'conversions': num(m.get('conversions'))}


def scrubbed(value, depth: int = 0):
    if isinstance(value, str):
        return value[:URL_LIMIT] if WEB.match(value) and not any(c.isspace() for c in value) else cut(value, TEXT_LIMIT)
    if depth > 8:
        return None
    if isinstance(value, list):
        return [scrubbed(v, depth + 1) for v in value]
    if isinstance(value, dict):
        return {k: scrubbed(v, depth + 1) for k, v in value.items()}
    return value


def ads_query(name: str, limit: int) -> str:
    return ('SELECT campaign.name, ad_group.name, ad_group_ad.ad.id, ad_group_ad.ad.final_urls, ad_group_ad.status, ad_group_ad.policy_summary.approval_status '
            "FROM ad_group_ad WHERE ad_group_ad.status = 'ENABLED' AND ad_group.status = 'ENABLED' AND campaign.status = 'ENABLED'" + (f" AND campaign.name LIKE '%{name}%'" if name else '')
            + f' LIMIT {limit}')


def ad_rows(rows: list[dict], url_part: str = '') -> list[dict]:
    out = []
    for r in rows:
        ad = r.get('adGroupAd') or {}
        urls = [str(u)[:URL_LIMIT] for u in (ad.get('ad') or {}).get('finalUrls') or []]
        if url_part and not any(url_part in u for u in urls):
            continue
        out.append({'campaign': cut((r.get('campaign') or {}).get('name'), TEXT_LIMIT), 'adGroup': cut((r.get('adGroup') or {}).get('name'), TEXT_LIMIT),
                    'adId': (ad.get('ad') or {}).get('id'), 'finalUrls': urls, 'approval': (ad.get('policySummary') or {}).get('approvalStatus')})
    return out


def read(ads: GoogleAds, w: dict, today: dt.date) -> dict:
    return unpriced(priced_read(ads, w, today))


def priced_read(ads: GoogleAds, w: dict, today: dt.date) -> dict:
    if w['op'] == QUERY:
        rows, truncated = ads.search(w['query'])
        return {'rows': scrubbed(rows), 'truncated': truncated}
    if w['op'] == 'ads':
        rows, truncated = ads.search(ads_query(quoted(w['campaign']), MAX_ROWS + 1))
        return {'ads': ad_rows(rows, w['url']), 'truncated': truncated}
    start, end, _ = window(w, today)
    span = f"segments.date BETWEEN '{start}' AND '{end}'"
    if w['op'] == 'daily':
        name = quoted(w['campaign'])
        source = 'campaign' if name else 'customer'
        q = (f'SELECT segments.date, metrics.clicks, metrics.impressions, metrics.conversions FROM {source} WHERE {span}'
             + (f" AND campaign.name LIKE '%{name}%'" if name else '') + ' ORDER BY segments.date')
        rows, truncated = ads.search(q)
        by = {}
        for r in rows:
            d = (r.get('segments') or {}).get('date')
            m = metrics(r.get('metrics') or {})
            acc = by.setdefault(d, {'date': d, 'clicks': 0, 'impressions': 0, 'conversions': 0.0})
            for k in ('clicks', 'impressions', 'conversions'):
                acc[k] = round(acc[k] + (m[k] or 0), 2)
        return {'from': start, 'to': end, 'days': [by[d] for d in sorted(by, key=str)], 'truncated': truncated, 'note': 'yesterday and today are still being counted by Google'}
    q = ('SELECT campaign.id, campaign.name, campaign.status, campaign.advertising_channel_type, metrics.clicks, metrics.impressions, metrics.conversions '
         f'FROM campaign WHERE {span} ORDER BY metrics.clicks DESC LIMIT {CAMPAIGN_ROWS}')
    rows, truncated = ads.search(q)
    out = [{'id': (r.get('campaign') or {}).get('id'), 'name': cut((r.get('campaign') or {}).get('name'), TEXT_LIMIT), 'status': (r.get('campaign') or {}).get('status'),
            'channel': (r.get('campaign') or {}).get('advertisingChannelType'), **metrics(r.get('metrics') or {})} for r in rows]
    return {'from': start, 'to': end, 'campaigns': out, 'truncated': truncated}


def untracked(url: str) -> str:
    parts = urlsplit(url.strip())
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if not TRACKING.fullmatch(decoded(k))]
    return urlunsplit((parts.scheme, parts.netloc, parts.path or '/', urlencode(query), ''))


def short(url: str) -> str:
    parts = urlsplit(url)
    return (parts.hostname or '') + (parts.path.rstrip('/') or '/')


def same_path(url: str) -> str:
    return (urlsplit(url).path or '/').rstrip('/') or '/'


def ad_urls(ads: GoogleAds) -> tuple[dict[str, list[dict]], bool]:
    rows, truncated = ads.search(ads_query('', MAX_ROWS + 1), MAX_ROWS)
    out: dict[str, list[dict]] = {}
    for ad in ad_rows(rows):
        for url in ad.get('finalUrls') or []:
            out.setdefault(untracked(url), []).append({'campaign': ad.get('campaign'), 'adGroup': ad.get('adGroup'), 'adId': ad.get('adId')})
    return out, truncated


def campaigns_of(found: list[dict]) -> list[str]:
    return list(dict.fromkeys(f['campaign'] for f in found if f.get('campaign')))


def card(url: str, found: list[dict], sig: str, status: int, final: str | None) -> dict:
    names = campaigns_of(found)
    shown = ', '.join(names[:CAMPAIGNS_SHOWN]) + (f' and {len(names) - CAMPAIGNS_SHOWN} more' if len(names) > CAMPAIGNS_SHOWN else '')
    one = len(found) == 1
    ads = f"{len(found)} ad{'' if one else 's'}" + (f' in {shown}' if shown else '')
    verb = 'sends' if one else 'send'
    if sig == BROKEN:
        what = f'{ads} {verb} visitors to {url}, which answers HTTP {status}: paid clicks land on a broken page until the ad link is changed.'
        label, sev = 'Broken ad link', 'high'
    else:
        what = f'{ads} {verb} visitors to {url}, which now redirects to {final}: point the ads at the new address so the click lands without a redirect.'
        label, sev = 'Ad link redirects', 'info'
    x = anomaly(LINKS, sig, NS, short(url), '', sev, len(found), None, None, '', label, what, 1, f'{label}: {short(url)}')
    x.update(key=f'{sig}|{NS}/{short(url)}', ranKey=f'{LINKS}:{url}', url=url, httpStatus=status, finalUrl=final, adCampaigns=names[:10], adCount=len(found))
    return x


def verdict(result: dict, url: str) -> tuple[str | None, int | None, str | None]:
    if result.get('challenge') or result.get('stopped'):
        return UNKNOWN, None, None
    status = result.get('status')
    chain = result.get('chain') or []
    final = chain[-1]['url'] if chain else url
    if status in BLOCKED:
        return UNKNOWN, status, final
    if status in GONE or (isinstance(status, int) and status >= 500):
        return BROKEN, status, final
    if status == 200 and same_path(final) != same_path(url):
        return MOVED, status, final
    return None, status, final


def raw_host(url: str) -> str:
    try:
        return (urlsplit(url).hostname or '').rstrip('.')
    except ValueError:
        return ''


def own_site(url: str) -> Site | None:
    host = host_of(url)
    return Site((host,)) if host and public_host(host) else None


def site_for(url: str, site: Site | None) -> Site | None:
    return site if site is not None else own_site(url)


def openable(url: str, site: Site | None) -> bool:
    chosen = site_for(url, site)
    return chosen is not None and refusal(url, chosen) is None


def unread(result: dict, url: str, status: int | None) -> str:
    if result.get('challenge'):
        return f'{raw_host(url)}: {CHALLENGE}'
    if result.get('stopped'):
        return f'{raw_host(url)}: a redirect was not followed'
    return f'{raw_host(url)} answered HTTP {status}'


def named(urls: list[str]) -> str:
    return ', '.join(u[:URL_SHOWN] for u in urls[:NAMED]) + (f' and {len(urls) - NAMED} more' if len(urls) > NAMED else '')


def pages(n: int) -> str:
    return f"{n} page{'' if n == 1 else 's'}"


def link_pass(urls: dict[str, list[dict]], truncated: bool, checker: Callable[[Site], PageCheck], site: Site | None, now: float, open_urls: list[str], prior: list,
              clock_of: Callable[[], float], sleep: Callable[[float], None], deadline: float) -> Sweep:
    private = [] if site is not None else [u for u in urls if raw_host(u) and not public_host(raw_host(u))]
    candidates = [(u, found) for u, found in sorted(urls.items()) if openable(u, site)]
    found_cards, ran, rows, tried, refused, missed = [], [], [], [], [], []
    for url, ads_there in candidates[:MAX_URLS]:
        if clock_of() >= deadline:
            break
        if tried:
            sleep(GAP_S)
        tried.append(url)
        try:
            result = checker(site_for(url, site)).get(url)
        except PageError as e:
            if e.status == 400:
                refused.append(url)
                ran.append(f'{LINKS}:{url}')
                continue
            rows.append({'part': 'url', 'url': url, 'error': e.message[:ERROR_LIMIT], 'ads': len(ads_there)})
            missed.append(e.message[:ERROR_LIMIT])
            continue
        sig, status, final = verdict(result, url)
        rows.append({'part': 'url', 'url': url, 'at': result.get('at'), 'status': status, 'final': final, 'verdict': sig, 'ads': len(ads_there)})
        if sig == UNKNOWN:
            missed.append(unread(result, url, status)[:ERROR_LIMIT])
            continue
        ran.append(f'{LINKS}:{url}')
        if sig:
            found_cards.append(card(url, ads_there, sig, status, final))
    read_n = len(tried) - len(refused) - len(missed)
    wanted_n = len(candidates) - len(refused)
    unreached = {u for u, _ in candidates} - set(tried)
    rows += [{k: v for k, v in r.items() if k not in ('plugin', 'cluster')} for r in prior if isinstance(r, dict) and r.get('part') == 'url' and r.get('url') in unreached][:MAX_URLS]
    ran += [f'{LINKS}:{u}' for u in open_urls if (u not in urls and not truncated) or (u in urls and not openable(u, site))]
    if missed and not read_n:
        head = f'error: {PAGES}: no page was read ({missed[0]})'
    elif not urls and not truncated:
        head = f'ok{DOT}no data'
        ran.append(f'{LINKS}:no-data')
    elif truncated:
        head = f'ok{DOT}read {pages(read_n)}, more not read'
    elif not wanted_n:
        head = f'ok{DOT}no data for {PAGES}'
        ran.append(f'{LINKS}:no-data')
    elif read_n < wanted_n:
        head = f'ok{DOT}read {read_n} of {wanted_n} {PAGES}'
    else:
        head = 'ok'
    shut = sorted(set(private) | set(refused))
    status_text = DOT.join([head] + ([f'not opened, not a public host: {named(shut)}'] if shut else []))
    ran.append(access_key(LINKS, PART))
    summary = {'part': 'summary', 'at': iso(now), 'urls': len(urls), 'opened': len(tried) - len(refused)}
    return Sweep(PassResult(LINKS, True, status_text, found_cards, {}, tuple(dict.fromkeys(ran))), sections={'adLinks': rows + [summary]})


def seconds(text: object) -> float | None:
    try:
        return dt.datetime.fromisoformat(str(text)).timestamp()
    except (ValueError, OverflowError, OSError):
        return None


def topic_key(raw: object) -> str:
    return TOPIC.sub('_', str(raw or '').upper()).strip('_')[:NAME_LIMIT] if isinstance(raw, str) else ''


def topic_name(key: str) -> str:
    return key.replace('_', ' ').capitalize()


def topics_of(policy: dict) -> list[dict]:
    out = []
    for entry in policy.get('policyTopicEntries') or []:
        key = topic_key(entry.get('topic')) if isinstance(entry, dict) else ''
        if key:
            out.append({'topic': topic_name(key), 'type': topic_key(entry.get('type')) or None, 'key': key})
    return out


def disapproved(rows: list[dict]) -> dict[str, dict]:
    campaigns: dict[str, dict] = {}
    for r in rows:
        ad = r.get('adGroupAd') or {}
        policy = ad.get('policySummary') or {}
        campaign = r.get('campaign') or {}
        cid = str(campaign.get('id') or '')
        if policy.get('approvalStatus') != 'DISAPPROVED' or not cid.isdigit():
            continue
        entry = campaigns.setdefault(cid, {'name': cut(campaign.get('name')), 'ads': []})
        entry['ads'].append({'adGroup': cut((r.get('adGroup') or {}).get('name')), 'adId': str((ad.get('ad') or {}).get('id') or ''), 'topics': topics_of(policy),
                             'finalUrls': [str(u)[:URL_LIMIT] for u in (ad.get('ad') or {}).get('finalUrls') or []]})
    return campaigns


def link_check(url: str, row: dict | None, key: str | None) -> dict:
    if row is None:
        return {'url': url, 'at': None, 'status': None, 'final': None, 'verdict': None, 'error': None, 'card': None}
    return {'url': url, 'at': row.get('at'), 'status': row.get('status'), 'final': row.get('final'), 'verdict': row.get('verdict'), 'error': row.get('error'),
            'card': key if row.get('verdict') in (BROKEN, MOVED) else None}


def link_line(check: dict) -> str:
    url = check['url']
    if check['at'] is None and check['error'] is None:
        return f'The ad-link check (gads) has not opened {url} yet.'
    at = seconds(check['at'])
    head = f'The ad-link check (gads) opened {url}' + (f' at {clock.date(at)} {clock.label(at)}' if at is not None else '')
    if check['error']:
        return f"{head} and it did not answer: {check['error']}."
    named_card = f" (card {check['card']})" if check['card'] else ''
    if check['verdict'] == BROKEN:
        return f"{head} and got HTTP {check['status']}: the page fails from here too{named_card}."
    if check['verdict'] == MOVED:
        return f"{head} and it redirects to {check['final']}{named_card}."
    if check['verdict'] == UNKNOWN and check['status'] in BLOCKED:
        return f"{head} and got HTTP {check['status']}: the site refused the check, so it could not tell whether the page works."
    if check['verdict'] == UNKNOWN:
        return f'{head} but could not tell whether it answers (a challenge page, or a redirect it does not follow).'
    if check['status'] == 200:
        return (f"{head} and got HTTP 200: the page answers from the server that runs the checks, so a bot, geo or redirect rule may keep Google's crawler out.")
    return f"{head} and got HTTP {check['status']}."


def policy_card(cid: str, campaign: dict, customer: str, link_rows: dict[str, dict], link_cards: dict[str, str]) -> dict:
    ads = campaign['ads']
    n = len(ads)
    counts: dict[str, int] = {}
    for ad in ads:
        for name in dict.fromkeys(t['topic'] for t in ad['topics']):
            counts[name] = counts.get(name, 0) + 1
    ordered = sorted(counts, key=lambda name: -counts[name])
    named = ', '.join(ordered[:TOPICS_SHOWN]) + (f' and {len(ordered) - TOPICS_SHOWN} more' if len(ordered) > TOPICS_SHOWN else '')
    label = f"{n} ad{'' if n == 1 else 's'} disapproved" + (f': {named}' if named else '')
    svc = campaign['name'] or f'campaign {cid}'
    lines = [f"Ad group {ad['adGroup']}, ad {ad['adId']}: "
             + (', '.join(f"{t['topic']} ({t['type'].lower()})" if t['type'] else t['topic'] for t in ad['topics']) or 'no policy topic given')
             + f"; final URL {', '.join(ad['finalUrls']) or 'none'}." for ad in ads[:AD_LINES]]
    if n > AD_LINES:
        lines.append(f'And {n - AD_LINES} more ads.')
    down = list(dict.fromkeys(untracked(u) for ad in ads if any(t['key'] == DOWN_TOPIC for t in ad['topics']) for u in ad['finalUrls']))[:DESTINATIONS]
    checks = [link_check(u, link_rows.get(u), link_cards.get(u)) for u in down]
    what = (f"Google Ads disapproved {n} enabled ad{'' if n == 1 else 's'} of campaign {svc} (id {cid}), so {'it does' if n == 1 else 'they do'} not serve "
            'until the ad or its page is fixed and Google reviews it again. ' + ' '.join(lines + [link_line(c) for c in checks])
            + f' The policy manager of account {shown_id(customer)} names each policy and how to fix or appeal it.')
    x = anomaly(POLICY, DISAPPROVED, NS, svc, '', 'warn', n, None, None, '', label, what)
    x.update(key=f'{POLICY}|{cid}', ranKey=POLICY, adsCustomer=customer, campaignId=cid, adCount=n,
             ads=[{'adGroup': ad['adGroup'], 'adId': ad['adId'], 'topics': [{'topic': t['topic'], 'type': t['type']} for t in ad['topics']], 'finalUrls': ad['finalUrls']}
                  for ad in ads[:ADS_KEPT]], linkChecks=checks)
    return x


def policy_pass(rows: list[dict], truncated: bool, customer: str, prior: list, cards: list[dict], instance: InstanceInfo | None = None) -> Sweep:
    link_rows = {r['url']: r for r in prior if isinstance(r, dict) and r.get('part') == 'url' and isinstance(r.get('url'), str)}
    link_cards = {c['url']: card_key(c['key'], instance) for c in cards if c.get('src') == LINKS and c.get('sig') in (BROKEN, MOVED) and isinstance(c.get('url'), str) and isinstance(c.get('key'), str)}
    found = [policy_card(cid, campaign, customer, link_rows, link_cards) for cid, campaign in sorted(disapproved(rows).items(), key=lambda kv: int(kv[0]))]
    if truncated:
        return Sweep(PassResult(POLICY, True, f'ok{DOT}read {len(rows)} disapproved ads, more not read', found, {}, (access_key(POLICY, PART),)))
    return Sweep(PassResult(POLICY, True, 'ok', found, {}, (POLICY, access_key(POLICY, PART))))


def refused(check: str, ads: GoogleAds, error: GoogleError) -> Sweep:
    warn = access_card(check, NAME, PART, f'{ads.shown} for the {CHECK_WORDS.get(check, check)}', error)
    return Sweep(PassResult(check, True, f'ok{DOT}no access to {ads.shown}', [warn], {}, (access_key(check, PART),)))


def links_for(a: dict) -> list[dict] | None:
    if a.get('src') == LINKS:
        out = [dict(label='Open the ad link', url=a['url'])] if str(a.get('url') or '').startswith('https://') else []
        return out + [dict(label='Google Ads', url=ADS_UI)]
    if a.get('src') == POLICY:
        customer = str(a.get('adsCustomer') or '')
        return [dict(label=f'Policy manager, account {shown_id(customer)}' if CUSTOMER.fullmatch(customer) else 'Policy manager', url=POLICY_UI)]
    return None
