import math
import re
import urllib.parse
from collections.abc import Mapping

from skopax.contract import HttpError, Transport, Unreachable, call
from skopax.kit.promql import PrometheusError, with_headers

LIST = '/graph/api/datasources'
PROXY = '/graph/api/datasources/proxy/uid/{uid}/api/v1/query'
TIMEOUT_S = 30
HOST_HEADER = re.compile(r'[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?(?::\d{1,5})?')


class PmmMetrics:
    def __init__(self, config: Mapping[str, str], transport: Transport):
        self.url = str(config.get('url') or '').rstrip('/')
        token = str(config.get('token') or '')
        host = str(config.get('host-header') or '').strip()
        extra = ({'Authorization': f'Bearer {token}'} if token else {}) | ({'Host': host} if HOST_HEADER.fullmatch(host) else {})
        self.transport = with_headers(transport, extra) if extra else transport
        self.pinned = str(config.get('metrics-datasource') or '').strip()
        self.uid = ''

    def get(self, what: str, path: str, params: dict | None = None) -> object:
        url = self.url + path + ('?' + urllib.parse.urlencode(params) if params else '')
        try:
            return call(self.transport, 'GET', url, timeout=TIMEOUT_S).body
        except HttpError as e:
            raise PrometheusError(f'PMM {what} HTTP {e.code}', e.code) from None
        except Unreachable:
            raise PrometheusError(f'PMM {what} unreachable') from None

    def source(self) -> str:
        if not self.uid:
            self.uid = self.pinned or self.found()
        return self.uid

    def found(self) -> str:
        listed = self.get('data source list', LIST)
        for s in listed if isinstance(listed, list) else []:
            if isinstance(s, dict) and s.get('type') == 'prometheus' and isinstance(s.get('uid'), str) and s['uid']:
                return s['uid']
        raise PrometheusError('PMM lists no metrics data source the token can read')

    def vec(self, expr: str, at: float) -> list[tuple[dict, float]]:
        body = self.get('metrics query', PROXY.format(uid=urllib.parse.quote(self.source(), safe='')), {'query': expr, 'time': f'{at:.3f}'})
        data = body.get('data') if isinstance(body, dict) else None
        if not isinstance(body, dict) or body.get('status') != 'success' or not isinstance(data, dict):
            raise PrometheusError('PMM metrics query answered an error')
        out = []
        for x in data.get('result') or []:
            v = float(x['value'][1])
            if not math.isnan(v) and abs(v) != float('inf'):
                out.append((x['metric'], v))
        return out
