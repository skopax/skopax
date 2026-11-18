import datetime as dt
import re
import time
from collections.abc import Callable
from urllib.parse import urlencode

from skopax.contract import HttpError, PassResult, ReadError, Sweep, Transport, Unreachable, anomaly, call, iso
from skopax.errkind import kind
from skopax.kit.google import GoogleAuth, GoogleError, access_card, access_key

NAME = 'App stores'
APPSTORE = 'appstore'
PLAY = 'play'
PLAY_REVIEWS = 'play-reviews'
DOT = ' \u00b7 '
LOOKUP_URL = 'https://itunes.apple.com/lookup'
APP_STORE_UI = 'https://apps.apple.com/{country}/app/id{id}'
REPORTING = 'https://playdeveloperreporting.googleapis.com/v1beta1'
PUBLISHER = 'https://androidpublisher.googleapis.com/androidpublisher/v3'
PLAY_UI = 'https://play.google.com/console'
PLAY_SCOPES = ('https://www.googleapis.com/auth/playdeveloperreporting', 'https://www.googleapis.com/auth/androidpublisher')
DEFAULT_COUNTRY = 'us'
MAX_COUNTRIES = 10
PACIFIC = 'America/Los_Angeles'
DAY = 86400
KEEP_S = 8 * DAY
BASE_MIN_S = 20 * 3600
SNAP_GAP_S = 3600
TIMEOUT = 20
BUDGET_S = 200
MIN_LEFT_S = 3
NAME_LIMIT = 60
VERSION_LIMIT = 40
VITALS = {'crash': ('crashRateMetricSet', 'userPerceivedCrashRate', 'user-perceived crash rate', 0.0109),
          'anr': ('anrRateMetricSet', 'userPerceivedAnrRate', 'user-perceived ANR rate', 0.0047)}
RULES = {'rating_floor': 5, 'rating_warn': 3.0, 'rating_crit': 2.0, 'rating_crit_floor': 10, 'vitals_times': 2, 'low_reviews': 5}
RULE_HELP = {
    'rating_floor': 'New App Store ratings since the snapshot of about a day earlier must reach this count before their average is judged.',
    'rating_warn': 'Warning when the average of the new App Store ratings is at or below this many stars.',
    'rating_crit': 'Critical when the average of the new ratings is at or below this many stars.',
    'rating_crit_floor': 'The critical level also needs at least this many new ratings.',
    'vitals_times': ("Warning when the Google Play crash or freeze rate is at least this many times the rate a week earlier and at least half of Google's "
                     'bad-behaviour line; above that line it is critical.'),
    'low_reviews': 'Warning when this many one- or two-star Google Play reviews arrive within a day.',
}
APPSTORE_OFF = 'the App Store part is off: apple-app-id is empty'
PLAY_OFF = 'the Google Play part is off: play-package is empty'
PLAY_NEEDS = 'the Play Console permission View app information (read-only)'
NO_PLAY = 'the service account has no access to this app in Play Console; invite it under Users and permissions with View app information (read-only)'
NO_REVIEWS = 'the service account cannot read the Play reviews of this app; grant it the needed Play Console permission under Users and permissions'
PARTS = {'vitals': 'Play vitals', 'reviews': 'Play reviews'}
PART_NEEDS = {'vitals': PLAY_NEEDS, 'reviews': ''}
KINDS = {'crash': 'Play crash rate', 'anr': 'Play ANR rate'}
OPS = ('ratings', 'vitals', 'reviews')
BY = ('day', 'version')
TOOL_DAYS = 14
MAX_DAYS = 28
TOOL_ROWS = 200
APP_ID = re.compile(r'\d{1,20}')
COUNTRY = re.compile(r'[a-z]{2}')
PACKAGE = re.compile(r'[A-Za-z][A-Za-z0-9_]{0,60}(\.[A-Za-z][A-Za-z0-9_]{0,60}){1,10}')
BAD_APP_ID = 'apple-app-id must be the numeric App Store id'
BAD_COUNTRIES = 'countries must be two-letter store codes such as us, gb or de'
MANY_COUNTRIES = f'countries takes at most {MAX_COUNTRIES} store codes'
BAD_PACKAGE = 'play-package must be a package name such as com.example.app'


class StoreError(ReadError):
    def __init__(self, status: int, message: str):
        super().__init__(status, message)
        self.reason = message


def app_id_of(text: object) -> str:
    value = str(text or '').strip()
    if value and not APP_ID.fullmatch(value):
        raise StoreError(400, BAD_APP_ID)
    return value


def countries_of(text: object) -> tuple[str, ...]:
    found = tuple(dict.fromkeys(p.strip().lower() for p in str(text or '').split(',') if p.strip()))
    if any(not COUNTRY.fullmatch(c) for c in found):
        raise StoreError(400, BAD_COUNTRIES)
    if len(found) > MAX_COUNTRIES:
        raise StoreError(400, MANY_COUNTRIES)
    return found or (DEFAULT_COUNTRY,)


def package_of(text: object) -> str:
    value = str(text or '').strip()
    if value and not PACKAGE.fullmatch(value):
        raise StoreError(400, BAD_PACKAGE)
    return value


def stars(v: float) -> str:
    return f'{v:.1f}'


def pct(v: float) -> str:
    return f'{100 * v:.2f} %'


def seconds(text: object) -> float | None:
    try:
        return dt.datetime.fromisoformat(str(text)).timestamp()
    except ValueError:
        return None


def number(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def short_text(value: object, limit: int) -> str | None:
    return value[:limit] if isinstance(value, str) and value else None


class AppStore:
    def __init__(self, app_id: str, country: str, transport: Transport):
        self.app_id = app_id
        self.country = country
        self.transport = transport

    def fetch(self, timeout: float = TIMEOUT) -> dict:
        got = call(self.transport, 'GET', f"{LOOKUP_URL}?{urlencode({'id': self.app_id, 'country': self.country})}", timeout=timeout).body
        rows = got.get('results') if isinstance(got, dict) else None
        first = rows[0] if isinstance(rows, list) and rows else None
        if not isinstance(first, dict):
            raise StoreError(404, f'App Store lookup: no app with id {self.app_id} in the {self.country.upper()} store')
        count, avg = first.get('userRatingCount'), first.get('averageUserRating')
        if not number(count) or not number(avg):
            raise StoreError(502, 'App Store lookup: the answer has no rating count or average')
        return {'count': int(count), 'avg': float(avg), 'version': short_text(first.get('version'), VERSION_LIMIT), 'name': short_text(first.get('trackName'), NAME_LIMIT)}

    def lookup(self, timeout: float = TIMEOUT) -> dict:
        try:
            return self.fetch(timeout)
        except HttpError as e:
            raise StoreError(502, f'App Store lookup HTTP {e.code}') from None
        except Unreachable as e:
            raise StoreError(502, f'App Store lookup: {kind(e)}') from None


def matching(rows: list, store: AppStore) -> dict | None:
    found = next((r for r in rows or [] if isinstance(r, dict) and r.get('country') == store.country and r.get('appId') == store.app_id), None)
    return None if found is None else {k: v for k, v in found.items() if k not in ('plugin', 'cluster')}


def kept_snapshots(row: dict | None, now: float) -> list[dict]:
    out = []
    for s in (row or {}).get('snapshots') or []:
        at = seconds(s.get('at')) if isinstance(s, dict) else None
        if at is not None and now - at <= KEEP_S and number(s.get('count')) and number(s.get('avg')):
            out.append(s)
    return out


def baseline(snaps: list[dict], now: float) -> dict | None:
    old = [s for s in snaps if now - seconds(s['at']) >= BASE_MIN_S]
    return min(old, key=lambda s: abs(now - seconds(s['at']) - DAY)) if old else None


def evaluate_ratings(cur: dict, base: dict | None, app_id: str, country: str, rules: dict, first: bool = True) -> list[dict]:
    if not base:
        return []
    new = cur['count'] - base['count']
    if new <= 0 or new < rules['rating_floor']:
        return []
    avg = (cur['avg'] * cur['count'] - base['avg'] * base['count']) / new
    if not 0.5 <= avg <= 5.5:
        return []
    avg = min(5.0, max(1.0, avg))
    if avg <= rules['rating_crit'] and new >= rules['rating_crit_floor']:
        sev = 'crit'
    elif avg <= rules['rating_warn']:
        sev = 'warn'
    else:
        return []
    hours = round((seconds(cur['at']) - seconds(base['at'])) / 3600)
    store = f'{country.upper()} store'
    x = anomaly(APPSTORE, 'store-rating', '-', 'ios-app', 'ratings' if first else f'ratings:{country}', sev, round(avg, 2), None, None, '', 'new App Store ratings',
                f"{new} new App Store ratings ({store}) in the last {hours} h average {stars(avg)} stars; the all-time average is {stars(cur['avg'])} "
                f"from {cur['count']} ratings and the current version is {cur.get('version') or '?'}. The average of the new ratings is worked out from "
                'the count and all-time average Apple publishes, so it has no review text; read the reviews in App Store Connect.',
                5, f"{sev.upper()} {new} new App Store ratings ({store}) average {stars(avg)} stars in {hours} h")
    x['appId'] = app_id
    x['country'] = country
    if not first:
        x['ranKey'] = f'{APPSTORE}:{country}'
    return [x]


def gone_keys(cards: list[dict], further: tuple[str, ...]) -> list[str]:
    out = []
    for card in cards:
        country = card.get('country')
        if card.get('src') == APPSTORE and isinstance(country, str) and COUNTRY.fullmatch(country) and card.get('sub') == f'ratings:{country}' and country not in further:
            out.append(f'{APPSTORE}:{country}')
    return list(dict.fromkeys(out))


def run_appstore(stores: list[AppStore], prior: list, now: float, rules: dict, gone: list[str] | tuple[str, ...] = (), clock: Callable[[], float] = time.monotonic,
                 budget: float = BUDGET_S) -> Sweep:
    deadline = clock() + budget
    found, ran, rows, errors, empty, unread = [], [], [], [], [], []
    for i, store in enumerate(stores):
        old = matching(prior, store)
        left = deadline - clock()
        if left < MIN_LEFT_S:
            unread.append(store.country)
            rows += [old] if old else []
            continue
        try:
            cur = dict(store.lookup(min(TIMEOUT, left)), at=iso(now))
        except StoreError as e:
            errors.append(f'{store.country}: {e.reason}')
            rows += [old] if old else []
            continue
        snaps = kept_snapshots(old, now)
        base = baseline(snaps, now)
        if base is None:
            empty.append(store.country)
        else:
            found += evaluate_ratings(cur, base, store.app_id, store.country, rules, i == 0)
            ran.append(APPSTORE if i == 0 else f'{APPSTORE}:{store.country}')
        if not snaps or now - seconds(snaps[-1]['at']) >= SNAP_GAP_S:
            snaps = snaps + [{k: cur[k] for k in ('at', 'count', 'avg', 'version')}]
        rows.append({'appId': store.app_id, 'country': store.country, 'name': cur.get('name'), 'snapshots': snaps})
    read = len(stores) - len(unread) - len(errors)
    if errors and not read and not unread:
        return Sweep(PassResult(APPSTORE, False, 'error: ' + '; '.join(errors)))
    notes = ([('no data' if len(empty) == read else f"no data for {', '.join(empty)}")] if empty else []) + ([f'read {len(stores) - len(unread)} of {len(stores)} countries'] if unread else [])
    status = DOT.join([('error: ' + '; '.join(errors)) if errors else 'ok'] + notes)
    keys = tuple(dict.fromkeys(ran + list(gone))) or (f'{APPSTORE}:no-data',)
    return Sweep(PassResult(APPSTORE, True, status, found, {}, keys), sections={'appStore': rows})


def pacific_day(d: object) -> dt.date | None:
    if not isinstance(d, dict) or not d.get('year'):
        return None
    try:
        return dt.date(d['year'], d.get('month', 1), d.get('day', 1))
    except (TypeError, ValueError):
        return None


def day_spec(d: dt.date) -> dict:
    return {'year': d.year, 'month': d.month, 'day': d.day, 'timeZone': {'id': PACIFIC}}


def metric_of(row: dict, name: str) -> float | None:
    for m in row.get('metrics') or []:
        if m.get('metric') == name:
            try:
                return float((m.get('decimalValue') or {}).get('value'))
            except (TypeError, ValueError):
                return None
    return None


class Play:
    def __init__(self, auth: GoogleAuth, package: str):
        self.auth = auth
        self.package = package

    @property
    def enabled(self) -> bool:
        return self.auth.enabled and bool(self.package)

    def latest_day(self, metric_set: str, retry: bool = True) -> dt.date | None:
        got = self.auth.call('GET', f'{REPORTING}/apps/{self.package}/{metric_set}', None, PARTS['vitals'], NO_PLAY, retry=retry, needs=PLAY_NEEDS)
        for f in (got.get('freshnessInfo') or {}).get('freshnesses') or []:
            if f.get('aggregationPeriod') == 'DAILY':
                return pacific_day(f.get('latestEndTime'))
        return None

    def rows(self, kind: str, days: int, by_version: bool = False) -> list[dict]:
        metric_set, metric, _, _ = VITALS[kind]
        end = self.latest_day(metric_set)
        if end is None:
            return []
        body = {'timelineSpec': {'aggregationPeriod': 'DAILY', 'startTime': day_spec(end - dt.timedelta(days=days)), 'endTime': day_spec(end)},
                'metrics': [metric, 'distinctUsers'], 'dimensions': ['versionCode'] if by_version else []}
        got = self.auth.call('POST', f'{REPORTING}/apps/{self.package}/{metric_set}:query', body, PARTS['vitals'], NO_PLAY, needs=PLAY_NEEDS)
        out = []
        for row in got.get('rows') or []:
            day = pacific_day(row.get('startTime'))
            if day:
                version = next((str(d.get('valueLabel') or d.get('stringValue') or d.get('int64Value')) for d in row.get('dimensions') or [] if d.get('dimension') == 'versionCode'), None)
                out.append({'day': day, 'rate': metric_of(row, metric), 'users': metric_of(row, 'distinctUsers')} | ({'version': version} if version else {}))
        return out

    def daily(self, kind: str) -> dict[dt.date, tuple[float | None, float | None]]:
        return {r['day']: (r['rate'], r['users']) for r in self.rows(kind, 8)}

    def reviews(self, limit: int = 100, retry: bool = True) -> list[dict]:
        got = self.auth.call('GET', f'{PUBLISHER}/applications/{self.package}/reviews?maxResults={limit}', None, PARTS['reviews'], NO_REVIEWS, retry=retry)
        out = []
        for r in got.get('reviews') or []:
            user = next((c.get('userComment') for c in r.get('comments') or [] if c.get('userComment')), None) or {}
            try:
                at = float((user.get('lastModified') or {}).get('seconds'))
            except (TypeError, ValueError):
                continue
            out.append({'stars': user.get('starRating'), 'at': at, 'version': user.get('appVersionName')})
        return out


def evaluate_vitals(kind: str, days: dict, rules: dict) -> tuple[list[dict], dict | None]:
    if not days:
        return [], None
    last = max(days)
    rate, users = days[last]
    before = days.get(last - dt.timedelta(days=7), (None, None))[0]
    _, _, word, bad = VITALS[kind]
    row = {'day': last.isoformat(), 'rate': rate, 'weekBefore': before, 'users': users, 'bad': bad}
    if rate is None:
        return [], row
    if rate > bad:
        sev = 'crit'
    elif before is not None and rate >= rules['vitals_times'] * before and rate >= bad / 2:
        sev = 'warn'
    else:
        return [], row
    x = anomaly(PLAY, 'play-vitals', '-', 'android-app', kind, sev, round(100 * rate, 3), None if before is None else round(100 * before, 3), None, '%',
                f'Android {word}',
                f"Google Play's {word} for the Android app was {pct(rate)} on {last.isoformat()} (Pacific day"
                + (f', {int(users):,} users' if users else '') + ')'
                + (f', {pct(before)} a week earlier' if before is not None else '')
                + f'. Above {pct(bad)} Google treats the app as bad behaviour and can show it lower in Play and warn users on its listing.',
                0.1, f'{sev.upper()} Android {word} {pct(rate)} on {last.isoformat()}')
    return [x], row


def evaluate_reviews(reviews: list[dict], now: float, rules: dict) -> tuple[list[dict], dict]:
    recent = [r for r in reviews if now - r['at'] <= DAY]
    low = [r for r in recent if isinstance(r.get('stars'), int) and r['stars'] <= 2]
    row = {'last24h': len(recent), 'low24h': len(low)}
    if len(low) < rules['low_reviews']:
        return [], row
    versions = ', '.join(sorted({str(r['version']) for r in low if r.get('version')})) or 'unknown'
    x = anomaly(PLAY, 'store-rating', '-', 'android-app', 'reviews', 'warn', len(low), None, None, '', 'low Play reviews',
                f'{len(low)} of {len(recent)} Google Play reviews with text in the last 24 h gave one or two stars (app versions {versions}). '
                'Play Console holds the review texts; ratings without text do not show here.',
                1, f'WARN {len(low)} one- or two-star Play reviews in 24 h')
    return [x], row


def play_vitals(play: Play, rules: dict) -> tuple[list[dict], dict]:
    found, vitals = [], {}
    for kind in VITALS:
        cards, row = evaluate_vitals(kind, play.daily(kind), rules)
        found += cards
        vitals[kind] = row
    return found, vitals


def metric_key(kind: str) -> str:
    return f'{PLAY}:{kind}'


def run_play(play: Play, now: float, rules: dict) -> Sweep:
    key_refused = None
    try:
        play.auth.access()
    except GoogleError as e:
        if not e.key_refused:
            return Sweep(PassResult(PLAY, False, f'error: {e.reason}'))
        key_refused = e
    found, ran, errors, empty, blank, refused, read = [], [], [], [], [], [], []
    section = {'at': iso(now), 'package': play.package}
    for part, key, fetch in (('vitals', PLAY, lambda: play_vitals(play, rules)), ('reviews', PLAY_REVIEWS, lambda: evaluate_reviews(play.reviews(), now, rules))):
        error = None if key_refused is None else key_refused.as_refusal(PART_NEEDS[part])
        if error is None:
            try:
                cards, row = fetch()
            except GoogleError as e:
                error = e.as_refusal(PART_NEEDS[part])
            except Exception as e:
                errors.append(f'{part}: {kind(e)}')
                continue
        if error is not None:
            if error.denied:
                refused.append(PARTS[part])
                found.append(access_card(PLAY, NAME, part, PARTS[part], error))
                ran.append(access_key(PLAY, part))
            else:
                errors.append(f'{part}: {error.reason}')
            continue
        read.append(part)
        ran.append(access_key(PLAY, part))
        section[part] = row
        missing = [k for k in VITALS if not isinstance(row.get(k), dict) or row[k].get('rate') is None] if part == 'vitals' else []
        if len(missing) == len(VITALS):
            blank.append(part)
        empty += [PARTS[part]] if len(missing) == len(VITALS) else [KINDS[k] for k in missing]
        for x in cards:
            x.update(package=play.package, ranKey=metric_key(x['sub']) if part == 'vitals' else key)
        found += cards
        ran += [metric_key(k) for k in VITALS if k not in missing] if part == 'vitals' else []
        if not missing:
            ran.append(key)
    if not read and not refused:
        return Sweep(PassResult(PLAY, False, 'error: ' + '; '.join(errors)))
    notes = ([('no data' if len(blank) == len(read) else f"no data for {', '.join(empty)}")] if empty else []) + ([f"no access to {', '.join(refused)}"] if refused else [])
    status = DOT.join([('error: ' + '; '.join(errors)) if errors else 'ok'] + notes)
    return Sweep(PassResult(PLAY, True, status, found, {}, tuple(ran)), sections={'play': [section]} if read else {})


def links_for(a: dict, country: str = DEFAULT_COUNTRY) -> list[dict] | None:
    if a.get('src') == APPSTORE:
        app_id, where = str(a.get('appId') or ''), str(a.get('country') or country)
        return [dict(label='App Store listing', url=APP_STORE_UI.format(country=where, id=app_id))] if APP_ID.fullmatch(app_id) and COUNTRY.fullmatch(where) else []
    if a.get('src') == PLAY:
        return [dict(label='Play Console', url=PLAY_UI)]
    return None


def wanted(a: dict) -> dict:
    op = a.get('op') or 'ratings'
    if op not in OPS:
        raise ReadError(400, f"op must be one of {', '.join(OPS)}")
    days = a.get('days') or TOOL_DAYS
    if not isinstance(days, int) or isinstance(days, bool) or not 1 <= days <= MAX_DAYS:
        raise ReadError(400, f'days must be 1-{MAX_DAYS}')
    by = a.get('by') or 'day'
    if by not in BY:
        raise ReadError(400, f"by must be one of {', '.join(BY)}")
    return {'op': op, 'days': days, 'by': by, 'country': str(a.get('country') or '').strip().lower()}


def windows(snaps: list[dict]) -> list[dict]:
    out = []
    for a, b in zip(snaps, snaps[1:]):
        new = b['count'] - a['count']
        if new <= 0:
            continue
        avg = (b['avg'] * b['count'] - a['avg'] * a['count']) / new
        out.append({'from': a['at'], 'to': b['at'], 'newRatings': new, 'avgStars': round(min(5.0, max(1.0, avg)), 2) if 0.5 <= avg <= 5.5 else None})
    return out


def read_ratings(store: AppStore, row: dict | None) -> dict:
    snaps = [x for x in (row or {}).get('snapshots') or [] if isinstance(x, dict) and x.get('at') and number(x.get('count')) and number(x.get('avg'))]
    return {'store': f'App Store ({store.country.upper()})', 'appId': store.app_id, 'now': store.lookup(), 'snapshots': snaps[-MAX_DAYS:], 'windows': windows(snaps)[-MAX_DAYS:],
            'note': 'Apple publishes only the rating count and the all-time average; avgStars of a window is worked out from them. No review texts.'}


def read_play(play: Play, w: dict) -> dict:
    if w['op'] == 'vitals':
        out = {'package': play.package, 'days': 'Pacific days', 'badBehaviour': {k: pct(v[3]) for k, v in VITALS.items()}}
        for kind in VITALS:
            rows = play.rows(kind, w['days'], w['by'] == 'version')
            out[kind] = [{'day': r['day'].isoformat(), 'rate': None if r['rate'] is None else pct(r['rate']), 'users': None if r['users'] is None else int(r['users'])}
                         | ({'version': r['version']} if r.get('version') else {}) for r in sorted(rows, key=lambda r: (r['day'], r.get('version') or ''))][-TOOL_ROWS:]
        return out
    reviews = play.reviews()
    by_stars: dict[str, int] = {}
    for r in reviews:
        by_stars[str(r.get('stars'))] = by_stars.get(str(r.get('stars')), 0) + 1
    return {'package': play.package, 'count': len(reviews), 'byStars': by_stars,
            'reviews': [{'at': iso(r['at']), 'stars': r.get('stars'), 'version': r.get('version')} for r in sorted(reviews, key=lambda r: -r['at'])][:100],
            'note': 'Play gives reviews with text from the last week only; texts and reviewer names are left out here'}
