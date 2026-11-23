import re
from urllib.parse import quote, urlencode

from skopax import clock
from skopax.contract import ReadError, anomaly, iso
from skopax.kit.google import GoogleAuth, GoogleError, access_card, access_key
from skopax.monitor import above
from skopax.redact import redact

CHECK = 'crashlytics'
NAME = 'Crashlytics'
CRASHLYTICS_API = 'https://firebasecrashlytics.googleapis.com/v1alpha'
NEEDS = 'the Firebase Crashlytics Viewer role (roles/firebasecrashlytics.viewer)'
NO_ACCESS = f"the Google service account lacks {NEEDS} on this app's Firebase project"
NO_APPS = 'no valid Firebase app id is set: give the Android or iOS app id from the Firebase console'
APPS = ('android', 'ios')
APP_FIELDS = {'android': 'android-app-id', 'ios': 'ios-app-id'}
BAD_APP_ID = '{field} must be a Firebase app id such as 1:<project number>:{app}:<hex>'
SCOPES = ('https://www.googleapis.com/auth/cloud-platform',)
REPORTS = {'issues': 'topIssues', 'versions': 'topVersions', 'os': 'topOperatingSystems', 'devices': {'android': 'topAndroidDevices', 'ios': 'topAppleDevices'}}
TYPES = ('FATAL', 'NON_FATAL', 'ANR')
FIELDS = {'versions': 'version', 'os': 'os', 'devices': 'device'}
COMPARES = {'day': 86400, 'week': 7 * 86400}
APP_ID = re.compile(r'1:(\d{1,20}):(android|ios):[0-9a-f]{6,40}')
VERSION = re.compile(r'[^()]{1,60}\s+\([^()]{1,40}\)')
ISSUE_ID = re.compile(r'[0-9a-f]{8,64}')
MAX_HOURS = 168
MAX_ROWS = 20
ROWS = 10
COMPARE_ROWS = 50
TEXT_LIMIT = 200
RULES = {'crash_floor': 5, 'crash_times': 2, 'crash_gap': 3, 'crash_crit_users': 20, 'anr_floor': 10, 'anr_times': 2, 'anr_gap': 5, 'new_users': 3}
RULE_HELP = {
    'crash_floor': 'Crashes in the last hour must reach this count before a card is raised.',
    'crash_times': 'Crashes must also be more than this many times the same hour yesterday or a week ago (the higher of the two).',
    'crash_gap': 'Crashes must also be at least this many above that earlier hour.',
    'crash_crit_users': 'A crash card is critical when this many users were hit in the hour; below it is high.',
    'anr_floor': 'App-not-responding events (Android freezes) in the last hour must reach this count before a card is raised.',
    'anr_times': 'Freezes must also be more than this many times the same hour yesterday or a week ago.',
    'anr_gap': 'Freezes must also be at least this many above that earlier hour.',
    'new_users': 'A crash or freeze not seen in the same hour yesterday or a week ago gets its own card once it hits this many users.',
}
WATCHED = {'FATAL': 'crashes', 'ANR': 'ANRs'}
ONE = {'FATAL': 'crash', 'ANR': 'ANR'}
PASS_ROWS = 50
HOUR = 3600
DAY = 86400


class Crashlytics:
    def __init__(self, auth: GoogleAuth, android: str = '', ios: str = ''):
        self.auth = auth
        self.apps = {}
        self.invalid = []
        for name, app_id in (('android', android), ('ios', ios)):
            value = str(app_id or '').strip()
            m = APP_ID.fullmatch(value)
            if m and m.group(2) == name:
                self.apps[name] = m.group(0)
            elif value:
                self.invalid.append(name)

    def problem(self, app: str | None = None) -> str | None:
        bad = [a for a in self.invalid if app is None or a == app]
        return BAD_APP_ID.format(field=APP_FIELDS[bad[0]], app=bad[0]) if bad else None

    @property
    def enabled(self) -> bool:
        return self.auth.enabled and bool(self.apps)

    def project(self, app: str) -> str:
        return self.apps[app].split(':')[1]

    def report(self, app: str, report: str, params: list[tuple[str, str]], retry: bool = True) -> dict:
        app_id = self.apps[app]
        url = f'{CRASHLYTICS_API}/projects/{self.project(app)}/apps/{quote(app_id, safe="")}/reports/{report}?{urlencode(params)}'
        return self.auth.call('GET', url, None, NAME, NO_ACCESS, retry=retry, needs=NEEDS)


def whole(value, default: int, low: int, high: int, what: str) -> int:
    got = default if value is None else value
    if not isinstance(got, int) or isinstance(got, bool) or not low <= got <= high:
        raise ReadError(400, f'{what} must be {low}-{high}')
    return got


def request(cl: Crashlytics, a: dict, now: float) -> dict:
    app = a.get('app')
    if app not in APPS:
        raise ReadError(400, f"app must be one of {', '.join(APPS)}")
    if cl.problem(app):
        raise ReadError(400, cl.problem(app))
    if app not in cl.apps:
        raise ReadError(400, f"app must be one of {', '.join(cl.apps)}: the {app} Firebase app id is not set" if cl.apps else f'the {app} Firebase app id is not set')
    name = a.get('report') or 'issues'
    if name not in REPORTS:
        raise ReadError(400, f"report must be one of {', '.join(REPORTS)}")
    report = REPORTS[name][app] if isinstance(REPORTS[name], dict) else REPORTS[name]
    hours = whole(a.get('hours'), 1, 1, MAX_HOURS, 'hours')
    limit = whole(a.get('limit'), ROWS, 1, MAX_ROWS, 'limit')
    compare = a.get('compare')
    if compare is not None and compare not in COMPARES:
        raise ReadError(400, f"compare must be one of {', '.join(COMPARES)}")
    filters = [('page_size', str(limit))]
    types = [str(x) for x in a.get('types') or []]
    if any(t not in TYPES for t in types):
        raise ReadError(400, f"types must be from {', '.join(TYPES)}")
    filters += [('filter.issue.error_types', t) for t in dict.fromkeys(types)]
    version = ' '.join(str(a.get('version') or '').split())
    if version and not VERSION.fullmatch(version):
        raise ReadError(400, 'version must be a Crashlytics version display name like "4.12.0 (4120)" (report versions lists them)')
    if version:
        filters.append(('filter.version.display_names', version))
    issue = str(a.get('issue') or '').strip()
    if issue and not ISSUE_ID.fullmatch(issue):
        raise ReadError(400, 'issue must be a Crashlytics issue id (hex, from report issues)')
    if issue:
        filters.append(('filter.issue.id', issue))
    windows = [('now', now - hours * 3600, now)]
    if compare:
        back = COMPARES[compare]
        windows.append((f'{compare} before', now - hours * 3600 - back, now - back))
    return {'app': app, 'report': name, 'api': report, 'filters': filters, 'windows': windows}


def interval(start: float, end: float) -> list[tuple[str, str]]:
    return [('filter.interval.start_time', iso(start)), ('filter.interval.end_time', iso(end))]


def label(group: dict, name: str) -> tuple[str, dict]:
    if name == 'issues':
        issue = group.get('issue') or {}
        return str(issue.get('id') or ''), {'issue': issue.get('id'), 'title': redact(str(issue.get('title') or ''))[:TEXT_LIMIT],
                                              'subtitle': redact(str(issue.get('subtitle') or ''))[:TEXT_LIMIT], 'type': issue.get('errorType'),
                                              'firstSeenVersion': issue.get('firstSeenVersion'), 'lastSeenVersion': issue.get('lastSeenVersion'), 'link': issue.get('uri')}
    entity = group.get('version') or group.get('operatingSystem') or group.get('device') or {}
    shown = redact(str(entity.get('displayName') or entity.get('displayVersion') or entity.get('model') or ''))[:TEXT_LIMIT]
    return shown, {FIELDS[name]: shown}


def counts(group: dict) -> dict:
    metrics = (group.get('metrics') or [{}])[0]
    return {'events': int(metrics.get('eventsCount') or 0), 'users': int(metrics.get('impactedUsersCount') or 0)}


def sized(filters: list[tuple[str, str]], n: int) -> list[tuple[str, str]]:
    return [('page_size', str(n)) if k == 'page_size' else (k, v) for k, v in filters]


def read(cl: Crashlytics, req: dict, now: float) -> dict:
    answers = [cl.report(req['app'], req['api'], (req['filters'] if i == 0 else sized(req['filters'], COMPARE_ROWS)) + interval(start, end))
               for i, (_, start, end) in enumerate(req['windows'])]
    rows, keys = [], {}
    for group in answers[0].get('groups') or []:
        key, row = label(group, req['report'])
        keys[key] = len(rows)
        rows.append({k: v for k, v in row.items() if v is not None} | counts(group))
    out = {'app': req['app'], 'report': req['report'], 'window': {'from': iso(req['windows'][0][1]), 'to': iso(now)}, 'rows': rows,
           'total': answers[0].get('totalSize', len(rows))}
    if len(answers) > 1:
        name, groups, total = req['windows'][1][0], answers[1].get('groups') or [], str(answers[1].get('totalSize') or '')
        before = {label(g, req['report'])[0]: counts(g) for g in groups}
        cut = len(groups) >= COMPARE_ROWS or (total.isdigit() and int(total) > len(groups))
        for key, i in keys.items():
            rows[i][name] = before.get(key, None if cut else {'events': 0, 'users': 0})
        out['compare'] = {'name': name, 'from': iso(req['windows'][1][1]), 'to': iso(req['windows'][1][2])}
        if any(rows[i][name] is None for i in keys.values()):
            out['note'] = f'{name} is null for a row outside the top {COMPARE_ROWS} of that window: it had fewer, but how many is unknown'
    return out


def hour_issues(cl: Crashlytics, app: str, end: float) -> dict[str, dict]:
    params = [('page_size', str(PASS_ROWS))] + [('filter.issue.error_types', t) for t in WATCHED] + interval(end - HOUR, end)
    out = {}
    for group in cl.report(app, REPORTS['issues'], params).get('groups') or []:
        issue = group.get('issue') or {}
        if issue.get('id'):
            out[str(issue['id'])] = {'type': issue.get('errorType'), 'title': redact(str(issue.get('title') or ''))[:TEXT_LIMIT],
                                     'subtitle': redact(str(issue.get('subtitle') or ''))[:TEXT_LIMIT], 'first': issue.get('firstSeenVersion'),
                                     'uri': issue.get('uri')} | counts(group)
    return out


def totals(issues: dict[str, dict], kind: str) -> tuple[int, int]:
    rows = [i for i in issues.values() if i['type'] == kind]
    return sum(i['events'] for i in rows), sum(i['users'] for i in rows)


def crash_item(sig: str, app: str, sub: str, sev: str, now: float, d1: float | None, d7: float | None, label: str, what: str, short: str, link: str | None) -> dict:
    x = anomaly(CHECK, sig, '-', f'{app}-app', sub, sev, now, d1, d7, '', label, what, 3, short)
    x['consoleUrl'] = link
    x['ranKey'] = f'{CHECK}:{app}'
    return x


def evaluate_app(app: str, windows: dict[str, dict[str, dict]], now: float, rules: dict) -> list[dict]:
    issues, before = windows['now'], windows['1d'] | windows['7d']
    until = f'{clock.date(now)} {clock.label(now)}'
    found = []
    for kind, word in WATCHED.items():
        (n, users), (n1, _), (n7, _) = totals(issues, kind), totals(windows['1d'], kind), totals(windows['7d'], kind)
        key = 'crash' if kind == 'FATAL' else 'anr'
        if not above(n, max(n1, n7), rules[f'{key}_times'], rules[f'{key}_gap'], rules[f'{key}_floor']):
            continue
        sev = 'crit' if kind == 'FATAL' and users >= rules['crash_crit_users'] else 'high'
        top = sorted((i for i in issues.values() if i['type'] == kind), key=lambda i: -i['events'])
        lines = '; '.join(f"{i['title']} ({i['events']} events, {i['users']} users, first seen {i['first'] or '?'})" for i in top[:3])
        found.append(crash_item('crash-rate', app, kind, sev, n, n1, n7, f'{word} up',
                                f'Firebase Crashlytics counted {n} {app} {word} from {users} users in the hour to {until}, against {n1} the same hour yesterday and {n7} a week ago. '
                                f'Top issues: {lines}. Users are summed per issue, so one user can count twice.',
                                f'{sev.upper()} {app} app {word} {n} in the last hour (1d {n1} / 7d {n7})', top[0]['uri'] if top else None))
    for issue_id, i in issues.items():
        if i['users'] >= rules['new_users'] and issue_id not in before:
            word = ONE.get(i['type'], 'issue')
            found.append(crash_item('crash-new', app, issue_id, 'warn', i['users'], None, None, f'new {word}',
                                    f"A Crashlytics {app} {word} not seen in the same hour yesterday or a week ago hit {i['users']} users ({i['events']} events) in the hour to {until}: "
                                    f"{i['title']} \u00b7 {i['subtitle']} (first seen in version {i['first'] or '?'}).",
                                    f"WARN {app} app new {word}: {i['title'][:80]} \u00b7 {i['users']} users", i['uri']))
    return found


def crash_pass(cl: Crashlytics, now: float, rules: dict, refused: GoogleError | None = None) -> tuple[list[dict], list[str], list[str], list[str]]:
    found, ran, denied, errors = [], [], [], []
    for app in APPS:
        if app in cl.invalid:
            errors.append(cl.problem(app))
            continue
        if app not in cl.apps:
            ran += [f'{CHECK}:{app}', access_key(CHECK, app)]
            continue
        error = refused
        if error is None:
            try:
                windows = {'now': hour_issues(cl, app, now), '1d': hour_issues(cl, app, now - DAY), '7d': hour_issues(cl, app, now - 7 * DAY)}
            except GoogleError as e:
                error = e.as_refusal(NEEDS)
        if error is not None:
            if error.denied:
                denied.append(f'the {app} app')
                found.append(access_card(CHECK, NAME, app, f'the {app} app', error))
                ran.append(access_key(CHECK, app))
            else:
                errors.append(f'the {app} app: {error.reason}')
            continue
        found += evaluate_app(app, windows, now, rules)
        ran += [f'{CHECK}:{app}', access_key(CHECK, app)]
    if not denied and not errors:
        ran.insert(0, CHECK)
    return found, ran, ([f"no access to {', '.join(denied)}"] if denied else []), errors


def links_for(a: dict) -> list[dict]:
    url = str(a.get('consoleUrl') or '')
    return [dict(label='Crashlytics issue' if a.get('sig') == 'crash-new' else 'Top Crashlytics issue', url=url)] if url.startswith('https://') else []
