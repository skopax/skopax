import concurrent.futures
from urllib.parse import quote, urlencode

from skopax import clock
from skopax.contract import HttpError, InstanceInfo, PassResult, ReadError, Sweep, Transport, Unreachable, alert_sev, anomaly, call, iso, send
from skopax.errkind import kind
from skopax.kit import access
from skopax.kit.alerts import ALERT_KEEP, QUIET_ALERTS
from skopax.redact import redact

ALERTS = 'ALERTS{alertstate=~"firing|pending"}'
FOR_STATE = 'ALERTS_FOR_STATE'
WATCHDOG = 'Watchdog'
TIMEOUT = 20
POOL = 6
MAX_DATASOURCES = 12
MAX_SERIES = 50
MAX_LABELS = 15
LABEL_LIMIT = 150
ERROR_LIMIT = 200
MANAGED_ROWS = 50
SKIP_LABELS = ('__name__', 'alertstate')
MANAGED_SKIP = ('normal', 'inactive')
SILENT = ('no firing or pending ALERTS series and no Watchdog: either nothing fires or this source evaluates no alert rules; '
          'it does not prove the source is healthy')
NOT_SUCCESS = 'the answer is not a Prometheus success'
FIRING = 'ALERTS{alertstate="firing"}'
RANK = {'info': 0, 'warn': 1, 'high': 2, 'crit': 3}
NAME_LIMIT = 60
STATUS_LIMIT = 100
MAX_CARDS = 50
NAMES_SHOWN = 20
PASS = 'grafana'
NO_DATA = f'{PASS}:no-data'
SECTION = 'grafanaAlerts'
NAME = 'Grafana'
LIST = 'datasources'
LIST_RIGHT = 'datasources:read'
QUERY_RIGHT = 'datasources:query'
MANAGED = 'Grafana-managed alerts'
MANAGED_RIGHT = 'alert.rules:read'


class GrafanaError(Exception):
    def __init__(self, reason: str, code: int | None = None, answered: bool = False):
        super().__init__(reason)
        self.reason = reason
        self.code = code
        self.answered = answered


def same_url(url: str) -> str:
    return str(url or '').strip().rstrip('/').lower()


def labels_of(metric: dict) -> dict:
    kept = {k: redact(str(v))[:LABEL_LIMIT] for k, v in sorted(metric.items()) if k not in SKIP_LABELS}
    return dict(list(kept.items())[:MAX_LABELS])


def matches(value, wanted: str | None) -> bool:
    return not wanted or wanted in str(value or '').lower()


class Grafana:
    def __init__(self, url: str, token: str, transport: Transport = send, timeout: float = TIMEOUT):
        self.url = (url or '').rstrip('/')
        self.token = token
        self.transport = transport
        self.timeout = timeout

    def _get(self, path: str, what: str, **params) -> object:
        url = f'{self.url}{path}' + (f'?{urlencode(params)}' if params else '')
        try:
            return call(self.transport, 'GET', url, {'Authorization': f'Bearer {self.token}'}, timeout=self.timeout).body
        except HttpError as e:
            raise GrafanaError(f'{what}: HTTP {e.code}', e.code) from None
        except Unreachable:
            raise GrafanaError(f'{what}: unreachable') from None

    def health(self) -> dict:
        body = self._get('/api/health', 'Grafana health')
        return body if isinstance(body, dict) else {}

    def listed(self) -> list:
        found = self._get('/api/datasources', 'Grafana datasources')
        if not isinstance(found, list):
            raise GrafanaError('Grafana datasources: the answer is not a list', answered=True)
        return found

    def datasources(self) -> list[dict]:
        return [d for d in self.listed() if isinstance(d, dict) and d.get('type') == 'prometheus' and d.get('uid')]

    def query(self, uid: str, name: str, expr: str) -> list[dict]:
        body = self._get(f'/api/datasources/proxy/uid/{quote(uid, safe="")}/api/v1/query', name, query=expr)
        if not isinstance(body, dict) or body.get('status') != 'success':
            raise GrafanaError(f'{name}: {NOT_SUCCESS}', answered=True)
        data = body.get('data')
        result = data.get('result') if isinstance(data, dict) else None
        return result if isinstance(result, list) else []

    def managed(self) -> list[dict]:
        body = self._get('/api/prometheus/grafana/api/v1/alerts', 'Grafana-managed alerts')
        data = body.get('data') if isinstance(body, dict) else None
        found = data.get('alerts') if isinstance(data, dict) else None
        return found if isinstance(found, list) else []


def client_for(info: InstanceInfo, transport: Transport, timeout: float = TIMEOUT) -> Grafana:
    return Grafana(info.config.get('url') or '', info.config.get('token') or '', transport, timeout)


def split(sources: list[dict]) -> tuple[list[dict], list[dict]]:
    ordered = sorted(sources, key=lambda d: (not d.get('isDefault'), d.get('id') or 0, d.get('name') or ''))
    first, kept, twins = {}, [], []
    for d in ordered:
        url = same_url(d.get('url'))
        if url and url in first:
            twins.append({'name': d.get('name'), 'duplicateOf': first[url]})
            continue
        if url:
            first[url] = d.get('name')
        kept.append(d)
    return kept, twins


def identity(m: dict) -> tuple:
    return tuple(sorted((k, v) for k, v in m.items() if k not in SKIP_LABELS))


def moment(value: object) -> float | None:
    try:
        at = float(value)
        iso(at)
        clock.local(at)
    except (TypeError, ValueError, OverflowError, OSError):
        return None
    return at


def some_names(names: list[str]) -> str:
    shown = ', '.join(names[:NAMES_SHOWN])
    return shown + (f' and {len(names) - NAMES_SHOWN} more' if len(names) > NAMES_SHOWN else '')


def datasource_alerts(g: Grafana, d: dict, alertname: str | None, detail: bool) -> dict:
    name = d.get('name') or d['uid']
    series = [s for s in g.query(d['uid'], name, ALERTS) if isinstance(s, dict)]
    rows = [(s.get('metric') or {}) for s in series]
    watchdog = any(m.get('alertname') == WATCHDOG for m in rows)
    counted = [m for m in rows if m.get('alertname') != WATCHDOG and matches(m.get('alertname'), alertname)]
    groups: dict[tuple, int] = {}
    for m in counted:
        k = (m.get('alertname'), m.get('severity'), m.get('alertstate'))
        groups[k] = groups.get(k, 0) + 1
    out = {'name': name, 'uid': d['uid'], 'firing': sum(1 for m in counted if m.get('alertstate') == 'firing'),
           'pending': sum(1 for m in counted if m.get('alertstate') == 'pending'), 'watchdog': watchdog,
           'alerts': [{'alertname': a, 'severity': sev, 'state': st, 'series': n}
                      for (a, sev, st), n in sorted(groups.items(), key=lambda x: (x[0][2] != 'firing', -x[1], str(x[0][0])))]}
    if not rows:
        out['note'] = SILENT
    if detail and counted:
        since = {}
        try:
            for s in g.query(d['uid'], name, FOR_STATE):
                if isinstance(s, dict):
                    since[identity(s.get('metric') or {})] = (s.get('value') or [None, None])[1]
        except GrafanaError:
            since = {}
        out['series'] = []
        for m in counted[:MAX_SERIES]:
            at = moment(since.get(identity(m)))
            out['series'].append({'alertname': m.get('alertname'), 'state': m.get('alertstate'), 'labels': labels_of(m)} | ({'since': iso(at)} if at is not None else {}))
        if len(counted) > MAX_SERIES:
            out['seriesCount'] = len(counted)
    return out


def managed_alerts(g: Grafana, alertname: str | None) -> dict:
    groups: dict[tuple, int] = {}
    for a in g.managed():
        if not isinstance(a, dict):
            continue
        labels, state = a.get('labels') or {}, str(a.get('state') or '').lower()
        if state in MANAGED_SKIP or not matches(labels.get('alertname'), alertname):
            continue
        k = (labels.get('alertname'), labels.get('severity'), state)
        groups[k] = groups.get(k, 0) + 1
    rows = [{'alertname': a, 'severity': sev, 'state': st, 'count': n} for (a, sev, st), n in sorted(groups.items(), key=lambda x: (-x[1], str(x[0][0])))]
    return {'alerts': rows[:MANAGED_ROWS]} | ({'alertCount': len(rows)} if len(rows) > MANAGED_ROWS else {})


def alert_summary(g: Grafana, datasource: str | None, alertname: str | None, now: float) -> dict:
    wanted, name_filter = (datasource or '').strip().lower() or None, (alertname or '').strip().lower() or None
    kept, twins = split(g.datasources())
    picked = [d for d in kept if matches(d.get('name'), wanted)]
    redirected = [t for t in twins if matches(t['name'], wanted)] if wanted and not picked else []
    if redirected:
        picked = [d for d in kept if d.get('name') in {t['duplicateOf'] for t in redirected}]
    if wanted and not picked:
        raise ReadError(400, f"no Prometheus datasource named like {datasource!r}; the datasources are {some_names([str(d.get('name')) for d in kept])}")
    left_out = picked[MAX_DATASOURCES:]
    picked = picked[:MAX_DATASOURCES]
    detail = bool(wanted or name_filter)
    with concurrent.futures.ThreadPoolExecutor(min(POOL, max(1, len(picked)))) as pool:
        jobs = [(d, pool.submit(datasource_alerts, g, d, name_filter, detail)) for d in picked]
    answered, unreachable = [], []
    for d, job in jobs:
        try:
            answered.append(job.result())
        except GrafanaError as e:
            unreachable.append({'name': d.get('name'), 'error': access.text(NAME, str(d.get('name')), QUERY_RIGHT, status=e.code) if access.denied(e.code) else e.reason[:ERROR_LIMIT]})
        except Exception as e:
            unreachable.append({'name': d.get('name'), 'error': f"{d.get('name')}: {kind(e)}"})
    out = {'grafana': g.url, 'at': iso(now), 'query': ALERTS,
           'totals': {'firing': sum(x['firing'] for x in answered), 'pending': sum(x['pending'] for x in answered), 'datasources': len(answered)},
           'datasources': answered, 'unreachable': unreachable, 'duplicates': [t for t in twins if matches(t['name'], wanted) or matches(t['duplicateOf'], wanted)]}
    if detail:
        out['filter'] = {k: v for k, v in (('datasource', wanted), ('alertname', name_filter)) if v}
    if redirected:
        out['note'] = '; '.join(f"{t['name']} has the same URL as {t['duplicateOf']}, so it is read as {t['duplicateOf']}" for t in redirected)
    if left_out:
        out['notQueried'] = [d.get('name') for d in left_out]
    if not wanted:
        try:
            out['grafanaManaged'] = managed_alerts(g, name_filter)
        except GrafanaError as e:
            out['grafanaManaged'] = {'error': access.text(NAME, MANAGED, MANAGED_RIGHT, status=e.code) if access.denied(e.code) else e.reason[:ERROR_LIMIT]}
    return out


def skip_list(raw: str) -> tuple[str, ...]:
    return tuple(n.strip() for n in (raw or '').split(',') if n.strip())


def checked(sources: list[dict], skip: tuple[str, ...], covered: tuple[str, ...]) -> tuple[list[dict], list[str], list[str]]:
    kept, _ = split(sources)
    names = {n.lower() for n in skip}
    urls = {same_url(u) for u in covered if u}
    picked, left = [], []
    for d in kept:
        if str(d.get('name') or '').lower() in names or same_url(d.get('url')) in urls:
            left.append(str(d.get('name')))
        else:
            picked.append(d)
    return picked[:MAX_DATASOURCES], left, [str(d.get('name') or d.get('uid')) for d in picked[MAX_DATASOURCES:]]


def firing_series(g: Grafana, d: dict) -> list[tuple[dict, str | None]]:
    name = d.get('name') or d['uid']
    rows = [s.get('metric') or {} for s in g.query(d['uid'], name, FIRING) if isinstance(s, dict)]
    rows = [m for m in rows if m.get('alertname') not in QUIET_ALERTS]
    since: dict[tuple, str] = {}
    if rows:
        try:
            for s in g.query(d['uid'], name, FOR_STATE):
                if isinstance(s, dict):
                    since[identity(s.get('metric') or {})] = (s.get('value') or [None, None])[1]
        except GrafanaError:
            since = {}
    return [(m, since.get(identity(m))) for m in rows]


def since_text(since: str | None) -> str:
    at = moment(since) if since else None
    return '' if at is None else f' since {clock.date(at)} {clock.label(at)}'


def alert_card(source: str, m: dict, since: str | None) -> dict:
    name = str(m.get('alertname') or '?')
    sev = alert_sev(m.get('severity'))
    ident = ','.join([f'cluster={source}'] + [f'{k}={m[k]}' for k in ALERT_KEEP if k in m])
    svc = m.get('job') or m.get('service') or m.get('deployment') or m.get('ingress') or m.get('consumergroup') or m.get('domain') or '-'
    labels = ', '.join(f'{k}={v}' for k, v in labels_of(m).items() if k not in ('alertname', 'severity'))
    x = anomaly('grafana', 'alert', str(m.get('namespace') or '-'), str(svc), name, sev, 1, None, None, '', name[:NAME_LIMIT],
                f'Prometheus alert {name} is firing in {source} (read through Grafana){since_text(since)}' + (f': {labels}' if labels else ''))
    x.update(key=f'alert|{name}|{ident}', datasource=source, ranKey=f'{PASS}:{source}', alertName=name, alertLabel=str(m.get('severity') or '').strip().lower(),
             short=f'{sev.upper()} alert {name[:NAME_LIMIT]} in {source}' + (f' {svc}' if svc != '-' else ''))
    return x


def sweep(g: Grafana, skip: tuple[str, ...], covered: tuple[str, ...], now: float) -> Sweep:
    try:
        picked, left, over = checked(g.datasources(), skip, covered)
    except GrafanaError as e:
        if access.denied(e.code):
            return Sweep(PassResult(PASS, True, access.status('ok', [LIST]), [access.card(PASS, NAME, LIST, LIST_RIGHT, e.code)], {}, (access.ran_key(PASS, LIST),)))
        return Sweep(PassResult(PASS, False, f'error: {e.reason}'))
    with concurrent.futures.ThreadPoolExecutor(min(POOL, max(1, len(picked)))) as pool:
        jobs = [(d, pool.submit(firing_series, g, d)) for d in picked]
    found, ran, errors, names, failed, capped, refused = [], [], [], [], [], [], []
    for d, job in jobs:
        name = str(d.get('name') or d['uid'])
        try:
            rows = job.result()
        except GrafanaError as e:
            if access.denied(e.code):
                refused.append(name)
                found.append(access.card(PASS, NAME, name, QUERY_RIGHT, e.code))
                continue
            errors.append(e.reason[:STATUS_LIMIT])
            failed.append(name)
            continue
        except Exception as e:
            errors.append(f'{name}: {kind(e)}')
            failed.append(name)
            continue
        names.append(name)
        cards: dict[str, dict] = {}
        for m, since in rows:
            card = alert_card(name, m, since)
            kept = cards.get(card['key'])
            if kept is None or RANK[card['sev']] > RANK[kept['sev']]:
                cards[card['key']] = card
        if len(cards) > MAX_CARDS:
            capped.append(f'read {MAX_CARDS} of {len(cards)} alerts in {name}')
            found += sorted(cards.values(), key=lambda x: -RANK[x['sev']])[:MAX_CARDS]
        else:
            ran.append(f'{PASS}:{name}')
            found += cards.values()
    ran += [f'{PASS}:{name}' for name in left]
    status = ' \u00b7 '.join(['error: ' + '; '.join(errors) if errors else 'ok' if picked or left else 'ok \u00b7 no data'] + capped)
    if over:
        status += f' \u00b7 read {len(picked)} of {len(picked) + len(over)} datasources'
    status = access.status(status, refused)
    summary = {'part': 'summary', 'at': iso(now), 'checked': names, 'failed': failed, 'skipped': left, 'notChecked': over}
    keys = tuple(ran or [NO_DATA]) + tuple(access.ran_key(PASS, part) for part in [LIST] + names + refused + left)
    return Sweep(PassResult(PASS, bool(names) or bool(refused) or not picked, status, found, {}, keys), sections={SECTION: [summary]})


def links_for(a: dict, url: str) -> list[dict] | None:
    if a.get('src') != 'grafana' or a.get('sig') == access.SIG:
        return None
    return [dict(label='Alert rules in Grafana', url=f"{url.rstrip('/')}/alerting/list?search={quote(str(a.get('sub') or a.get('label') or ''))}")]
