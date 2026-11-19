import base64
import fnmatch
import time
import urllib.parse
from collections.abc import Mapping

from skopax.contract import HttpError, ReadError, Transport, Unreachable, anomaly, call
from skopax.remote import PAGE_DEADLINE

SIG = 'sonar-gate'
VALIDATE = '/api/authentication/validate'
SEARCH = '/api/components/search'
STATUS = '/api/qualitygates/project_status'
ALLOW = frozenset({VALIDATE, SEARCH, STATUS})
PAGE = 500
PAGES = 10
CONDITIONS_MAX = 5
TIMEOUT_S = 30
BUDGET_S = 200
TEST_BUDGET_S = 12
FAILING = 'ERROR'
DENIED_CODES = (401, 403)
WORDS = {'LT': 'below', 'GT': 'above', 'EQ': 'equal to', 'NE': 'other than'}


class SonarError(ReadError):
    def __init__(self, reason: str, code: int | None = None, cause: Exception | None = None):
        super().__init__(code if code is not None else 502, reason)
        self.reason = reason
        self.code = code
        self.cause = cause

    @property
    def denied(self) -> bool:
        return self.code in DENIED_CODES


class SonarQube:
    def __init__(self, url: str, token: str, transport: Transport, budget: float = BUDGET_S, clock=time.monotonic):
        self.url = (url or '').rstrip('/')
        self.token = token
        self.transport = transport
        self.clock = clock
        self.budget = budget
        self.deadline = clock() + budget

    @classmethod
    def of(cls, config: Mapping[str, str], transport: Transport, budget: float = BUDGET_S) -> 'SonarQube':
        return cls(config.get('url') or '', config.get('token') or '', transport, budget)

    def call(self, path: str, **params) -> dict:
        if path not in ALLOW:
            raise ValueError(f'sonarqube: GET {path} is not on the read-only allowlist')
        left = self.deadline - self.clock()
        if left <= 0:
            raise SonarError(f'GET {path}: the {self.budget:g} s budget is spent')
        url = self.url + path + ('?' + urllib.parse.urlencode(params) if params else '')
        head = {'Authorization': 'Basic ' + base64.b64encode(f'{self.token}:'.encode()).decode()}
        try:
            got = call(self.transport, 'GET', url, head, timeout=min(TIMEOUT_S, left / PAGE_DEADLINE)).body
        except HttpError as e:
            raise SonarError(f'GET {path} HTTP {e.code}', e.code, e) from None
        except Unreachable as e:
            raise SonarError(f'GET {path} unreachable', cause=e) from None
        return got if isinstance(got, dict) else {}


def rows_of(raw: object) -> list[dict]:
    return [r for r in raw if isinstance(r, dict)] if isinstance(raw, list) else []


def projects_of(client: SonarQube) -> tuple[list[dict], int, bool]:
    found: list[dict] = []
    total = 0
    for page in range(1, PAGES + 1):
        got = client.call(SEARCH, qualifiers='TRK', ps=PAGE, p=page)
        rows = [r for r in rows_of(got.get('components')) if isinstance(r.get('key'), str) and r['key']]
        found += rows
        paging = got.get('paging') if isinstance(got.get('paging'), dict) else {}
        total = paging.get('total') if isinstance(paging.get('total'), int) and not isinstance(paging.get('total'), bool) else len(found)
        if len(rows) < PAGE or len(found) >= total:
            break
    return found, total, total > len(found)


def filters_of(raw: str) -> list[str]:
    return list(dict.fromkeys(p.strip() for p in (raw or '').split(',') if p.strip()))


def watched(rows: list[dict], patterns: list[str]) -> list[dict]:
    if not patterns:
        return rows
    return [r for r in rows if any(fnmatch.fnmatchcase(r['key'], p) or fnmatch.fnmatchcase(str(r.get('name') or '').lower(), p.lower()) for p in patterns)]


def gate_of(client: SonarQube, key: str) -> tuple[str, list[str]] | None:
    got = client.call(STATUS, projectKey=key)
    status = got.get('projectStatus') if isinstance(got.get('projectStatus'), dict) else {}
    if not isinstance(status.get('status'), str):
        return None
    out = []
    for c in rows_of(status.get('conditions')):
        if c.get('status') != FAILING or not isinstance(c.get('metricKey'), str):
            continue
        said = f"{c['metricKey']} is {c.get('actualValue')}" if c.get('actualValue') is not None else c['metricKey']
        word = WORDS.get(str(c.get('comparator')))
        limit = f" (fails when {word} {c['errorThreshold']})" if word and c.get('errorThreshold') is not None else ''
        out.append(said + limit)
    return status['status'], out[:CONDITIONS_MAX]


def gates_of(client: SonarQube, keys: list[str]) -> tuple[dict[str, tuple[str, list[str]]], list[str]]:
    out: dict[str, tuple[str, list[str]]] = {}
    unread: list[str] = []
    for i, key in enumerate(keys):
        try:
            got = gate_of(client, key)
        except SonarError as e:
            if e.code == 401:
                raise
            if isinstance(e.cause, Unreachable) or e.code is None:
                unread += keys[i:]
                break
            unread.append(key)
            continue
        if got is None:
            unread.append(key)
        else:
            out[key] = got
    if keys and not out:
        raise SonarError('no quality gate state could be read')
    return out, unread


def card(project: dict, conditions: list[str] | None) -> dict:
    name = str(project.get('name') or project['key'])
    why = ': ' + '; '.join(conditions) if conditions else ' (SonarQube lists no failed condition)'
    what = f"The SonarQube quality gate of project {name} ({project['key']}) is failing{why}."
    return anomaly('sonarqube', SIG, '-', project['key'], '', 'warn', 1, None, None, '', 'quality gate failed', what)


def link_for(card: Mapping, url: str) -> list[dict] | None:
    base = (url or '').rstrip('/')
    if card.get('src') != 'sonarqube' or card.get('sig') != SIG or not base.startswith('https://') or not card.get('svc'):
        return None
    return [dict(label='SonarQube project', url=f"{base}/dashboard?id={urllib.parse.quote(str(card['svc']), safe='')}")]
