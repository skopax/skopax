import base64
import datetime
import json
import math
import re
import time
import urllib.parse
from collections.abc import Mapping
from typing import Callable

from skopax.contract import HttpError, InstanceInfo, Peer, ReadError, Transport, Unreachable, call, send

ID = 'prometheus'
CHECK = 'prom'
CANCEL_MIN_S = 1
WATCHED = '$namespaces'
POD = re.compile(r'^(.*)-[a-z0-9]{6,10}-[a-z0-9]{5}$')
SIDECARS = re.compile(r'istio|linkerd|otel|envoy|init|fluent|vault-agent')
MAX_POINTS = 500
TEST_TIMEOUT_S = 15
clock = time.monotonic


def credential(config: Mapping[str, str]) -> str:
    mode = config.get('auth') or 'none'
    if mode == 'basic' and config.get('username'):
        return 'password'
    return 'token' if mode == 'bearer' and config.get('token') else ''


class PrometheusError(Exception):
    def __init__(self, reason: str, code: int | None = None):
        super().__init__(reason)
        self.reason = reason
        self.code = code


def scoped(query: str, namespaces: str) -> str:
    return query.replace(WATCHED, json.dumps(namespaces))


class Prometheus:
    def __init__(self, url: str, transport: Transport = send, sleep=time.sleep, *, namespaces: str = '.*'):
        self.url = url.rstrip('/')
        self.transport = transport
        self.sleep = sleep
        self.namespaces = namespaces

    @property
    def enabled(self) -> bool:
        return bool(self.url)

    def watches(self, ns: str) -> bool:
        return re.fullmatch(self.namespaces, ns) is not None

    def call(self, path: str, **params) -> dict:
        url = f'{self.url}/api/v1/{path}?' + urllib.parse.urlencode(params)
        for attempt in (0, 1):
            try:
                body = call(self.transport, 'GET', url, timeout=60).body
                break
            except HttpError as e:
                raise PrometheusError(f'Prometheus {path} HTTP {e.code}', e.code) from None
            except Unreachable:
                if attempt:
                    raise PrometheusError(f'Prometheus {path} unreachable') from None
                self.sleep(1)
        if not isinstance(body, dict) or body.get('status') != 'success' or not isinstance(body.get('data'), dict):
            raise PrometheusError(f'Prometheus {path} answered an error')
        return body['data']

    def vec(self, expr: str, at: float) -> list[tuple[dict, float]]:
        out = []
        for x in self.instant(expr, at):
            v = float(x['value'][1])
            if not math.isnan(v) and abs(v) != float('inf'):
                out.append((x['metric'], v))
        return out

    def instant(self, expr: str, at: float) -> list[dict]:
        return self.call('query', query=expr, time=f'{at:.3f}')['result']

    def range_query(self, expr: str, start: float, end: float, step: int) -> list[dict]:
        return self.call('query_range', query=expr, start=f'{start:.3f}', end=f'{end:.3f}', step=str(step))['result']

    def series(self, expr: str, at: float, ident: Callable[[dict], tuple]) -> dict:
        out = {}
        for m, v in self.vec(expr, at):
            k = ident(m)
            out[k] = out.get(k, 0) + v
        return out

    def detect(self, ns_re: str, hours: int, now: float) -> list[tuple[str, str, str, str, float]]:
        start = now - hours * 3600
        res = self.call('query_range', query=f'count by (namespace,pod,container,image) (kube_pod_container_info{{namespace=~{json.dumps(ns_re)}}})',
                        start=start, end=now, step=120)['result']
        seen = {}
        for s in res:
            m = s['metric']
            if SIDECARS.search(m.get('container', '')) or not re.fullmatch(ns_re, m['namespace']):
                continue
            p = POD.match(m['pod'])
            if not p:
                continue
            key = (m['namespace'], p.group(1))
            t0 = float(s['values'][0][0])
            img = m['image'].rsplit(':', 1)[-1]
            seen.setdefault(key, {})
            seen[key][img] = min(seen[key].get(img, t0), t0)
        out = []
        for key, imgs in seen.items():
            if len(imgs) < 2:
                continue
            order = sorted(imgs.items(), key=lambda x: x[1])
            new_img, t_new = order[-1]
            if t_new <= start + 120:
                continue
            out.append((key[0], key[1], order[-2][0], new_img, t_new))
        return sorted(out, key=lambda x: x[4])


def out_err(sel: str, window: str, by: str | None = None) -> str:
    count, total = 'http_client_requests_seconds_count', 'http_client_requests_seconds_sum'
    cancel = sel + ',outcome="UNKNOWN",exception="none"'
    head = f'sum by ({by})' if by else 'sum'
    return (f'{head} (increase({count}{{{sel},outcome="SERVER_ERROR"}}[{window}]) or increase({count}{{{sel},outcome="UNKNOWN",exception!="none"}}[{window}])'
            f' or (increase({count}{{{cancel}}}[{window}]) and increase({total}{{{cancel}}}[{window}]) / increase({count}{{{cancel}}}[{window}]) >= {CANCEL_MIN_S}))')


def signal_queries(ns: str, svc: str, window: str) -> dict[str, str]:
    sel = f'namespace={json.dumps(ns)},job={json.dumps(svc)}'
    pod = f'namespace={json.dumps(ns)},pod=~{json.dumps(svc + "-[a-z0-9]+-[a-z0-9]+")}'

    def ingress(extra: str = '') -> str:
        return (f'sum(increase(nginx_ingress_controller_requests{{exported_namespace={json.dumps(ns)},ingress={json.dumps(svc)}{extra}}}[{window}])'
                f' or increase(nginx_ingress_controller_requests{{exported_namespace={json.dumps(ns)},exported_service={json.dumps(svc)}{extra}}}[{window}]))')

    return {
        'req': f'sum(increase(http_server_requests_seconds_count{{{sel}}}[{window}]))',
        '5xx': f'sum(increase(http_server_requests_seconds_count{{{sel},status=~"5.."}}[{window}]))',
        'out-err': out_err(sel, window),
        'gw-err': f'sum(increase(krakend_proxy_duration_count{{{sel},error="true"}}[{window}]))',
        'gw-req': f'sum(increase(krakend_proxy_duration_count{{{sel}}}[{window}]))',
        'ing-5xx': ingress(',status=~"5.."'),
        'ing-req': ingress(),
        'restarts': f'sum(increase(kube_pod_container_status_restarts_total{{{pod}}}[{window}]))',
        'unavail': f'sum(kube_deployment_status_replicas_unavailable{{namespace={json.dumps(ns)},deployment={json.dumps(svc)}}})',
        'hikari-pend': f'max(max_over_time(hikaricp_connections_pending{{{sel}}}[{window}]))',
        'hikari-tmo': f'sum(increase(hikaricp_connections_timeout_total{{{sel}}}[{window}]))',
        'kafka-lag': f'max(max_over_time(kafka_consumer_fetch_manager_records_lag_max{{{sel}}}[{window}]))',
    }


def auth_headers(config: Mapping[str, str]) -> dict[str, str]:
    mode = config.get('auth') or 'none'
    if mode == 'bearer' and config.get('token'):
        return {'Authorization': f"Bearer {config['token']}"}
    if mode == 'basic' and config.get('username'):
        raw = f"{config['username']}:{config.get('password') or ''}".encode('utf-8')
        return {'Authorization': 'Basic ' + base64.b64encode(raw).decode('ascii')}
    return {}


def with_headers(transport: Transport, extra: dict[str, str]) -> Transport:
    def send(method: str, url: str, headers: dict[str, str], data: bytes | None, timeout: float):
        return transport(method, url, {**headers, **extra}, data, timeout)

    return send


def client_for(info: InstanceInfo, transport: Transport) -> Prometheus:
    extra = auth_headers(info.config)
    return Prometheus(info.config['url'], with_headers(transport, extra) if extra else transport, namespaces=info.config.get('namespaces') or '.*')


def ranged(client: Prometheus, expr: str, at: float, minutes: int) -> list[dict]:
    step = max(60, minutes * 60 // 200)
    return [{'metric': r.get('metric'), 'values': (r.get('values') or [])[-MAX_POINTS:]} for r in client.range_query(expr, at - minutes * 60, at, step)]


def prom_link(expr, ui, rng='2d', end=None):
    q = {'g0.expr': expr, 'g0.tab': 'graph', 'g0.range_input': rng}
    if end:
        q['g0.end_input'] = datetime.datetime.fromtimestamp(end, datetime.timezone.utc).strftime('%Y-%m-%d %H:%M:%S')
    return f'{ui}/query?' + urllib.parse.urlencode(q)


class Late(Exception):
    pass


def bounded(transport: Transport, budget: float) -> Transport:
    deadline = clock() + budget

    def send(method: str, url: str, headers: dict[str, str], data: bytes | None, timeout: float):
        left = deadline - clock()
        if left <= 0:
            raise Late()
        return transport(method, url, headers, data, min(timeout, max(left, 1)))

    return send


def ui_of(config: Mapping[str, str]) -> str:
    return (config.get('ui-url') or config.get('url') or '').rstrip('/')


def graph(expr: str, ui: str) -> str | None:
    return prom_link(expr, ui) if ui.startswith('https://') else None


def number(row: dict) -> float:
    try:
        return float(row['value'][1])
    except (KeyError, IndexError, TypeError, ValueError):
        return 0.0


def instant(peer: Peer, expr: str) -> list[dict]:
    extra = auth_headers(peer.info.config)
    transport = with_headers(peer.http(), extra) if extra else peer.http()
    url = peer.info.config['url'].rstrip('/') + '/api/v1/query?' + urllib.parse.urlencode({'query': expr})
    body = call(transport, 'GET', url, timeout=TEST_TIMEOUT_S).body
    data = body.get('data') if isinstance(body, dict) and body.get('status') == 'success' else None
    result = data.get('result') if isinstance(data, dict) else None
    if not isinstance(result, list):
        raise ReadError(502, 'Prometheus answered an error')
    return [r for r in result if isinstance(r, dict)]
