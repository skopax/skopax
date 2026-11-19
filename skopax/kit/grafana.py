import json
import math
from urllib.parse import quote, urlencode

from skopax.contract import HttpError, InstanceInfo, Transport, Unreachable, call
from skopax.remote import TimedOut

TIMEOUT = 20
SAID_MAX = 300
LIST_RIGHT = 'datasources:read'
QUERY_RIGHT = 'datasources:query'


class DatasourceError(Exception):
    def __init__(self, reason: str, code: int | None = None, right: str = '', said: str = '', late: bool = False):
        super().__init__(reason)
        self.reason = reason
        self.code = code
        self.right = right
        self.said = said
        self.late = late


def said_of(body: str) -> str:
    try:
        found = json.loads(body)
    except (TypeError, ValueError):
        return ''
    text = found.get('error') if isinstance(found, dict) else None
    return ' '.join(text.split())[:SAID_MAX] if isinstance(text, str) else ''


def find(listed: list, name: str) -> str:
    usable = sorted((d for d in listed if isinstance(d, dict) and d.get('uid')), key=lambda d: d.get('type') != 'prometheus')
    found = next((d for d in usable if d.get('name') == name), None) or next((d for d in usable if d.get('uid') == name), None)
    if not found:
        raise DatasourceError(f'no Grafana datasource named {name}')
    return str(found['uid'])


class Datasource:
    def __init__(self, url: str, token: str, transport: Transport, name: str, timeout: float = TIMEOUT, limit: int | None = None):
        self.url = (url or '').rstrip('/')
        self.token = token
        self.transport = transport
        self.name = name
        self.timeout = timeout
        self.limit = limit
        self.found: str | None = None

    def get(self, path: str, what: str, right: str, **params) -> object:
        url = f'{self.url}{path}' + (f'?{urlencode(params)}' if params else '')
        try:
            return call(self.transport, 'GET', url, {'Authorization': f'Bearer {self.token}'}, timeout=self.timeout, limit=self.limit).body
        except HttpError as e:
            raise DatasourceError(f'{what}: HTTP {e.code}', e.code, right, said_of(e.body)) from None
        except TimedOut:
            raise DatasourceError(f'{what}: timed out', late=True) from None
        except Unreachable:
            raise DatasourceError(f'{what}: unreachable') from None

    def listed(self) -> list:
        found = self.get('/api/datasources', 'Grafana datasources', LIST_RIGHT)
        if not isinstance(found, list):
            raise DatasourceError('Grafana datasources: the answer is not a list')
        return found

    def uid(self, listed: list | None = None) -> str:
        if self.found:
            return self.found
        self.found = find(self.listed() if listed is None else listed, self.name)
        return self.found

    def data(self, path: str, **params) -> dict:
        body = self.get(f'/api/datasources/proxy/uid/{quote(self.uid(), safe="")}/api/v1/{path}', self.name, QUERY_RIGHT, **params)
        if not isinstance(body, dict) or body.get('status') != 'success':
            raise DatasourceError(f'{self.name}: the answer is not a Prometheus success')
        data = body.get('data')
        return data if isinstance(data, dict) else {}

    def result(self, path: str, **params) -> list[dict]:
        found = self.data(path, **params).get('result')
        return [r for r in found if isinstance(r, dict)] if isinstance(found, list) else []

    def vec(self, expr: str, **params) -> list[tuple[dict, float]]:
        out = []
        for s in self.result('query', query=expr, **params):
            try:
                v = float((s.get('value') or [None, None])[1])
            except (TypeError, ValueError, IndexError):
                continue
            if not math.isnan(v) and abs(v) != float('inf'):
                out.append((s.get('metric') or {}, v))
        return out

    def span(self, expr: str, start: float, end: float, step: int) -> list[dict]:
        return self.result('query_range', query=expr, start=f'{start:.0f}', end=f'{end:.0f}', step=str(step))


def client_for(info: InstanceInfo, name: str, transport: Transport, timeout: float = TIMEOUT) -> Datasource:
    return Datasource(info.config.get('url') or '', info.config.get('token') or '', transport, name, timeout)


def explore(ui: str, uid: str, expr: str) -> str | None:
    if not ui.startswith('https://') or not uid:
        return None
    panes = {'a': {'datasource': uid, 'queries': [{'refId': 'A', 'expr': expr, 'datasource': {'type': 'prometheus', 'uid': uid}}], 'range': {'from': 'now-2d', 'to': 'now'}}}
    return f"{ui.rstrip('/')}/explore?schemaVersion=1&panes={quote(json.dumps(panes, separators=(',', ':')), safe='')}"
