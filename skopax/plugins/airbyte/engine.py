import collections
import concurrent.futures
import dataclasses
import datetime
import math
import re
import time
import urllib.parse
from collections.abc import Mapping

from skopax import clock
from skopax.contract import HttpError, ReadError, Transport, Unreachable, anomaly, call, iso
from skopax.kit import options
from skopax.kit.text import duration

API = '/api/public/v1'
HEALTH = '/api/v1/health'
ALLOW = frozenset({('POST', '/applications/token'), ('GET', '/jobs'), ('GET', '/connections')})
RULES = dict(jobs=15, long_x=2, long_slack_s=1200, no_base_s=3600, failed_keep_s=26 * 3600)
RULE_HELP = {
    'jobs': 'Sync jobs read for each watched connection; the usual duration and rows are the median of the successful ones among them.',
    'long_x': 'A running job is long once it has run longer than this many times its usual duration and also longer than its usual duration plus long_slack_s.',
    'long_slack_s': 'Seconds beyond its usual duration that a running job must also pass before it counts as long.',
    'no_base_s': 'Seconds a running job may take when the connection has no successful run to compare with.',
    'failed_keep_s': 'Seconds a failed sync stays flagged after its job ended.',
}
ISO_DUR = re.compile(r'P(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:([\d.]+)S)?)?')
PAGE = 100
PAGES = 5
WORKERS = 6
TIMEOUT_S = 30
BUDGET_S = 200
JOBS_MAX = 100
READERS = 'airbyte-jobs'
NOT_LISTED = 'not listed'
DENIED_CODES = (401, 403)
DENIED = 'the application lacks the needed permission (it reads only what its owning user can access)'
INVALID = 'the application is invalid or lacks the needed permission (it reads only what its owning user can access)'
CREDENTIALS = 'the client id and secret were refused'
TOKEN = '/applications/token'
TOOL_JOBS = 20
TOOL_JOBS_DEFAULT = 10
MATCH_JOBS = 15
OPS = ('connections', 'jobs')
CONNECTION_ID = re.compile(r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}')
NO_REASON = 'the Airbyte public API has no failure reason, attempt count or logs: open the job link or the timeline for them'
RUNNING = ('running', 'pending', 'incomplete')
DASH = '\u2013'
SEVERITIES = ('info', 'warn', 'crit')
RETRY = ' Airbyte retries a failed attempt inside the same job and the public API has no attempt count, so a run this long is most likely a retry loop.'
BETWEEN = ' (status incomplete: its last attempt failed and Airbyte retries)'


class AirbyteError(ReadError):
    def __init__(self, reason: str, code: int | None = None, part: str | None = None, cause: Exception | None = None):
        super().__init__(code if code is not None else 502, reason)
        self.reason = reason
        self.code = code
        self.part = part
        self.cause = cause

    @property
    def denied(self) -> bool:
        return self.code in DENIED_CODES


def part_of(path: str) -> str:
    return 'jobs' if path == '/jobs' else 'connections'


class NoToken(AirbyteError):
    pass


class Airbyte:
    def __init__(self, url: str, client_id: str, client_secret: str, transport: Transport, budget: float | None = None):
        self.url = (url or '').rstrip('/')
        self.client_id = client_id
        self.client_secret = client_secret
        self.transport = transport
        self.budget = budget
        self.deadline = None if budget is None else time.monotonic() + budget

    @classmethod
    def of(cls, config: Mapping[str, str], transport: Transport, budget: float | None = None) -> 'Airbyte':
        return cls(config.get('url') or '', config.get('client-id') or '', config.get('client-secret') or '', transport, budget)

    def _timeout(self, method: str, path: str) -> float:
        if self.deadline is None:
            return TIMEOUT_S
        left = self.deadline - time.monotonic()
        if left <= 0:
            raise AirbyteError(f'{method} {path}: the {self.budget:g} s budget is spent', part=part_of(path))
        return min(TIMEOUT_S, left)

    def call(self, method: str, path: str, token: str | None = None, body: dict | None = None, **params) -> dict:
        if (method, path) not in ALLOW:
            raise ValueError(f'airbyte: {method} {path} is not on the read-only allowlist')
        url = self.url + API + path + ('?' + urllib.parse.urlencode(params) if params else '')
        timeout = self._timeout(method, path)
        try:
            got = call(self.transport, method, url, {'Authorization': 'Bearer ' + token} if token else {}, body, timeout=timeout).body
        except HttpError as e:
            why = (f': {CREDENTIALS}' if path == TOKEN else f': {INVALID}' if e.code == 401 else f': {DENIED}') if e.code in DENIED_CODES else ''
            raise AirbyteError(f'{method} {path} HTTP {e.code}{why}', e.code, part_of(path), e) from None
        except Unreachable as e:
            raise AirbyteError(f'{method} {path} unreachable', part=part_of(path), cause=e) from None
        return got if isinstance(got, dict) else {}


def token_of(client: Airbyte) -> str:
    token = client.call('POST', '/applications/token', body=dict(client_id=client.client_id, client_secret=client.client_secret, grant_type='client_credentials')).get('access_token')
    if not isinstance(token, str) or not token:
        raise NoToken('POST /applications/token answered without an access token')
    return token


def page_of(client: Airbyte, token: str, page: int) -> list[dict]:
    got = client.call('GET', '/connections', token, limit=PAGE, offset=PAGE * page).get('data')
    return [c for c in got if isinstance(c, dict)] if isinstance(got, list) else []


def connections_of(client: Airbyte, token: str) -> tuple[list[dict], bool]:
    conns = []
    for page in range(PAGES):
        got = page_of(client, token, page)
        conns += got
        if len(got) < PAGE:
            return conns, False
    return conns, True


def watched_of(raw: str) -> list[str]:
    out: dict[str, str] = {}
    for part in (raw or '').split(','):
        entry = part.strip()
        if entry:
            out.setdefault(entry.lower(), entry)
    return list(out.values())


def severities_of(raw: object) -> tuple[dict[str, str], list[str]]:
    found, bad = options.pairs(raw)
    out: dict[str, str] = {}
    for key, value in found.items():
        level = value.lower()
        if level in SEVERITIES and key.lower() not in out:
            out[key.lower()] = level
        else:
            bad.append(f'{key}={value}')
    return out, bad


def workspaces_of(raw: object) -> list[str]:
    return list(dict.fromkeys(x.lower() for x in options.entries(raw)))


def name_pattern(raw: object):
    return options.guarded(raw)


def schedule_of(s) -> str | None:
    s = s if isinstance(s, dict) else {}
    if s.get('cronExpression'):
        return 'cron ' + str(s['cronExpression'])
    return ' '.join(str(x) for x in (s.get('scheduleType'), s.get('basicTiming')) if x) or None


def name_of(c: dict) -> str | None:
    name = c.get('name')
    return name if isinstance(name, str) and name.strip() else None


def picked(c: dict) -> dict:
    return dict(id=c.get('connectionId'), name=name_of(c), status=c.get('status'), schedule=schedule_of(c.get('schedule')), workspaceId=c.get('workspaceId'))


def keys_of(c: dict) -> set[str]:
    return {str(c.get('connectionId') or '').lower(), str(c.get('name') or '').strip().lower()} - {''}


def placeholder(entry: str) -> dict:
    return dict(conn=dict(id=entry, name=entry, status=NOT_LISTED, schedule=None, workspaceId=None), jobs=[], missing=True)


@dataclasses.dataclass
class PassRead:
    data: list[dict]
    cut: bool
    access: dict[str, int | None]
    listed: int
    failed: dict[str, str] = dataclasses.field(default_factory=dict)


def read_jobs(client: Airbyte, token: str, ids: list[str], jobs: int, budget: float, started: float, clock) -> tuple[dict[str, list[dict]], dict[str, int], dict[str, str]]:
    def jobs_of(i: str) -> list[dict]:
        got = client.call('GET', '/jobs', token, connectionId=i, jobType='sync', limit=jobs, orderBy='createdAt|DESC').get('data')
        return [j for j in got if isinstance(j, dict)] if isinstance(got, list) else []

    ex = concurrent.futures.ThreadPoolExecutor(WORKERS, thread_name_prefix=READERS)
    try:
        futures = {i: ex.submit(jobs_of, i) for i in ids}
        done, pending = concurrent.futures.wait(list(futures.values()), timeout=max(0.0, budget - (clock() - started)))
        late = f'{len(done)} of {len(futures)} connections read in {budget:g} s'
        found, denied, failed = {}, {}, {}
        for i, f in futures.items():
            if f in pending:
                failed[i] = late
                continue
            error = f.exception()
            if error is None:
                found[i] = f.result()
            elif isinstance(error, AirbyteError) and error.denied:
                denied[i] = error.code
            elif isinstance(error, AirbyteError):
                failed[i] = error.reason
            else:
                raise error
        return found, denied, failed
    finally:
        ex.shutdown(wait=False, cancel_futures=True)


def probe_jobs(client: Airbyte, token: str) -> int | None:
    try:
        client.call('GET', '/jobs', token, jobType='sync', limit=1)
    except AirbyteError as e:
        if not e.denied:
            raise
        return e.code
    return None


def matches(c: dict, wanted: set[str], pattern) -> bool:
    if not wanted and pattern is None:
        return True
    return bool(keys_of(c) & wanted) or (pattern is not None and name_of(c) is not None and options.search(pattern, name_of(c)) is not None)


def airbyte_pass(client: Airbyte, watched: list[str], jobs: int, budget: float | None = None, clock=time.monotonic, probe: bool = False, pattern=None,
                 workspaces: list[str] | tuple[str, ...] = ()) -> PassRead:
    started = clock()
    budget = BUDGET_S if budget is None else budget
    token = token_of(client)
    listed, cut = connections_of(client, token)
    wanted = {entry.lower() for entry in watched}
    named = [c for c in listed if isinstance(c.get('connectionId'), str) and c['connectionId'] and matches(c, wanted, pattern)]
    chosen = {c['connectionId']: picked(c) for c in named if not workspaces or str(c.get('workspaceId') or '').lower() in workspaces}
    seen = set().union(*(keys_of(c) for c in named))
    access: dict[str, int | None] = {'connections': None}
    found, denied, failed = read_jobs(client, token, list(chosen), jobs, budget, started, clock) if chosen else ({}, {}, {})
    if found or denied:
        access['jobs'] = next(iter(denied.values()), None)
    elif probe and not chosen:
        access['jobs'] = probe_jobs(client, token)
    data = [dict(conn=chosen[i], jobs=[], denied=True) if i in denied else dict(conn=chosen[i], jobs=[], failed=True) if i in failed else dict(conn=chosen[i], jobs=found[i])
            for i in chosen]
    return PassRead(data + [placeholder(entry) for entry in watched if entry.lower() not in seen], cut, access, len(listed), failed)


def iso_secs(d) -> float | None:
    m = ISO_DUR.fullmatch(d) if isinstance(d, str) and d else None
    if not m:
        return None
    days, h, mi, s = m.groups()
    return int(days or 0) * 86400 + int(h or 0) * 3600 + int(mi or 0) * 60 + float(s or 0)


def seconds_of(s: object) -> float | None:
    if not isinstance(s, str):
        return None
    try:
        return datetime.datetime.fromisoformat(s.replace('Z', '+00:00')).timestamp()
    except ValueError:
        return None


def number(v: object) -> float | None:
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) else None


def median(xs: list[float]) -> float | None:
    xs = sorted(xs)
    return None if not xs else xs[len(xs) // 2] if len(xs) % 2 else (xs[len(xs) // 2 - 1] + xs[len(xs) // 2]) / 2


def timeline(url: str, ws: object, i: object) -> str | None:
    return f'{url.rstrip("/")}/workspaces/{ws}/connections/{i}/timeline' if url and ws and i else None


def job_url(url: str, ws: object, i: object, job: object) -> str | None:
    found = timeline(url, ws, i)
    return None if found is None or job is None else found + '?' + urllib.parse.urlencode(dict(jobId=job, openLogs='true'))


def at(t: float) -> str:
    return f'{clock.hm(t)} {clock.label(t)}'


def labels_of(data: list[dict], kept: Mapping[str, str] | None = None) -> dict[str, str]:
    named = [(d['conn']['id'], d['conn']['name'] or d['conn']['id']) for d in data if not d.get('missing')]
    counts = collections.Counter(name for _, name in named)
    out = {}
    for i, name in named:
        forms = (name, f'{name} ({str(i)[:8]})')
        held = (kept or {}).get(i)
        out[i] = held if held in forms else forms[1] if counts[name] > 1 else name
    return out


def item(c: dict, name: str, sig: str, took: float, base: float | None, label: str, what: str, short: str, job: object, sev: str = 'warn') -> dict:
    x = anomaly('airbyte', 'airbyte-' + sig, '-', name, 'sync', sev, took, base, None, 'dur', label, what, 86400, short)
    x.update(key=f'airbyte|{name}|{sig}', ranKey=f"airbyte:{c['id']}", connId=c['id'], jobId=job, workspaceId=c.get('workspaceId'))
    return x


def evaluate(data: list[dict], now: float, url: str, rules: Mapping[str, float], kept: Mapping[str, str] | None = None, severities: Mapping[str, str] | None = None) -> tuple[list[dict], list[dict]]:
    found, rows = [], []
    labels = labels_of(data, kept)
    for d in data:
        c, jobs = d['conn'], d['jobs']
        sev = (severities or {}).get(str(c['id']).lower()) or (severities or {}).get(str(c['name'] or '').lower()) or 'warn'
        tag = sev.upper()
        name, ws = labels.get(c['id'], c['name'] or c['id']), c.get('workspaceId')
        ok = [j for j in jobs if j.get('status') == 'succeeded' and iso_secs(j.get('duration'))]
        base = median([iso_secs(j['duration']) for j in ok])
        base_rows = median([number(j.get('rowsSynced')) or 0 for j in ok])
        r = dict(id=c['id'], name=name, workspaceId=ws, connStatus=c['status'], schedule=c['schedule'], usualS=base and round(base), usualRows=base_rows, okRuns=len(ok),
                 lastOk=ok[0].get('startTime') if ok else None, url=timeline(url, ws, c['id']))
        if d.get('missing'):
            r['missing'] = True
        if d.get('denied'):
            r['jobsDenied'] = True
        if d.get('failed'):
            r['jobsFailed'] = True
        rows.append(r)
        last = jobs[0] if jobs else None
        start = seconds_of(last.get('startTime')) if last else None
        if start is None:
            continue
        running = last.get('status') in RUNNING
        took = now - start if running else iso_secs(last.get('duration')) or max(0, (seconds_of(last.get('lastUpdatedAt')) or start) - start)
        end = None if running else start + took
        job = last.get('jobId')
        synced = number(last.get('rowsSynced'))
        r.update(job=job, jobUrl=job_url(url, ws, c['id'], job),
                 status=last.get('status'), start=last['startTime'], end=iso(end) if end else None, durS=round(took), rows=synced)
        limit = max(rules['long_x'] * base, base + rules['long_slack_s']) if base else rules['no_base_s']
        long_run = took > limit
        usual = (f'its {len(ok)} recent successful runs took {duration(base)}' + (f' and {round(base_rows):,} rows' if base_rows else '') + ' (median)') if base else f'no successful run among its last {len(jobs)} jobs'
        retry = RETRY if long_run else ''
        between = BETWEEN if last.get('status') == 'incomplete' else ''
        rows_txt = f', {round(synced or 0):,} rows' + (' so far' if running else '')
        if running and long_run:
            found.append(item(c, name, 'long-run', took, base, 'sync running long',
                              f'{name}: job {job} has been running for {duration(took)} since {at(start)}{between}{rows_txt}; {usual}.{retry}',
                              f'{tag} Airbyte {name} running {duration(took)} (usual {duration(base) if base else DASH})', job, sev))
        elif last.get('status') == 'failed' and now - end < rules['failed_keep_s']:
            found.append(item(c, name, 'failed', took, base, 'sync failed',
                              f'{name}: job {job} {last.get("status")} at {at(end)} after {duration(took)} (started {at(start)}{rows_txt}); {usual}.{retry}',
                              f'{tag} Airbyte {name} sync {last.get("status")} after {duration(took)}', job, sev))
    rows.sort(key=lambda r: str(r['name']))
    return found, rows


def links_for(a: dict, url: str) -> list[dict] | None:
    if a.get('src') != 'airbyte':
        return None
    found = timeline(url, a.get('workspaceId'), a.get('connId'))
    if found is None:
        return []
    job = a.get('jobId')
    return [dict(label='Airbyte connection timeline', url=found)] + ([dict(label=f'Airbyte job {job} logs', url=job_url(url, a['workspaceId'], a['connId'], job))] if job is not None else [])


def connection_row(url: str, c: dict) -> dict:
    return dict(id=c.get('connectionId'), name=name_of(c), status=c.get('status'), schedule=schedule_of(c.get('schedule')),
                timeline=timeline(url, c.get('workspaceId'), c.get('connectionId')))


def job_row(url: str, conn: dict, j: dict) -> dict:
    secs = iso_secs(j.get('duration'))
    return dict(id=j.get('jobId'), status=j.get('status'), start=j.get('startTime'), durationS=round(secs) if secs is not None else None, duration=duration(secs) if secs else None,
                rows=j.get('rowsSynced'), bytes=j.get('bytesSynced'), link=job_url(url, conn.get('workspaceId'), conn['id'], j.get('jobId')))


class AirbyteRun:
    def __init__(self, client: Airbyte):
        self.client = client
        self.token: str | None = None
        self.listed: list[dict] | None = None

    def _token(self) -> str:
        if self.token is None:
            self.token = token_of(self.client)
        return self.token

    def _connections(self) -> list[dict]:
        if self.listed is None:
            self.listed = [dict(connection_row(self.client.url, c), workspaceId=c.get('workspaceId')) for c in connections_of(self.client, self._token())[0]]
        return self.listed

    @staticmethod
    def _shown(r: dict) -> dict:
        return {k: v for k, v in r.items() if k != 'workspaceId'}

    def connections(self, name: str | None) -> dict:
        rows = self._connections()
        kept = [self._shown(r) for r in rows if not name or name.lower() in (r['name'] or '').lower()]
        out = {'connections': kept}
        return out | ({'note': f'no connection name contains {name!r}; {len(rows)} connections listed'} if name and not kept else {})

    def _pick(self, connection: str) -> list[dict]:
        wanted = connection.strip().lower()
        rows = self._connections()
        if CONNECTION_ID.fullmatch(wanted):
            hit = next((r for r in rows if r['id'] == wanted), None)
            if hit is None:
                raise ReadError(404, f'no connection {wanted} among the connections this application lists (it may not exist, or the application cannot see it)')
            return [hit]
        exact = [r for r in rows if (r['name'] or '').lower() == wanted]
        hits = exact or [r for r in rows if wanted in (r['name'] or '').lower()]
        if not hits:
            raise ReadError(404, f'no connection named like {connection!r} among the connections this application lists (or the application cannot see it); op connections lists them')
        return hits

    def _last_jobs(self, hits: list[dict]) -> tuple[list[dict], int]:
        token = self._token()

        def last(r: dict) -> tuple[dict | None, str | None]:
            try:
                found = self.client.call('GET', '/jobs', token, connectionId=r['id'], jobType='sync', limit=1, orderBy='createdAt|DESC').get('data')
            except AirbyteError as e:
                if e.denied:
                    raise
                return None, e.reason
            return (job_row(self.client.url, r, found[0]) if isinstance(found, list) and found and isinstance(found[0], dict) else None), None

        with concurrent.futures.ThreadPoolExecutor(WORKERS) as ex:
            jobs = list(ex.map(last, hits[:MATCH_JOBS]))
        rows = [{'name': r['name'], 'id': r['id'], 'status': r.get('status')} | ({'lastJob': j} if j else {}) | ({'lastJobError': why} if why else {})
                for r, (j, why) in zip(hits[:MATCH_JOBS], jobs)] + [{'name': r['name'], 'id': r['id']} for r in hits[MATCH_JOBS:]]
        return rows, sum(1 for _, why in jobs if why)

    def jobs(self, connection: str, limit: int) -> dict:
        hits = self._pick(connection)
        if len(hits) > 1:
            matches, unread = self._last_jobs(hits)
            return {'matches': matches, 'note': f'{len(hits)} connections match {connection!r}, each with its last sync job'
                    + (f' (the first {MATCH_JOBS})' if len(hits) > MATCH_JOBS else '') + ': call jobs again with the id of the one you mean for more of its jobs'
                    + (f'; last job unreadable for {unread}' if unread else '')}
        conn = hits[0]
        got = self.client.call('GET', '/jobs', self._token(), connectionId=conn['id'], jobType='sync', limit=limit, orderBy='createdAt|DESC').get('data')
        jobs = [j for j in got if isinstance(j, dict)] if isinstance(got, list) else []
        return {'connection': self._shown(conn), 'timeline': conn['timeline'], 'jobs': [job_row(self.client.url, conn, j) for j in jobs[:limit]], 'note': NO_REASON}
