import re
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

from skopax.plugins import prometheus
from skopax.plugins.prometheus import engine as pm
from tests.fake_http import FakeHttp
from tests.plugin_kit import PROM_URL, prom_runtime
from tests.test_engine import NOW

GET = 'GET'
METRIC = re.compile(r'([a-zA-Z_:][a-zA-Z0-9_:]*)\{')
FAMILY = {'log4j2_': 'spring', 'http_server_': 'spring', 'http_client_': 'spring', 'hikaricp_': 'spring', 'krakend_': 'krakend', 'nginx_ingress_': 'ingress', 'kube_': 'kube', 'kafka_': 'kafka'}
ALERT = {'state': 'firing', 'activeAt': '2026-10-19T10:00:00Z', 'labels': {'alertname': 'HighLatency', 'namespace': 'shop', 'job': 'checkout-service', 'severity': 'warning'}, 'annotations': {'summary': 'slow'}}
UNKNOWN = 'unknown profile "jvm" (use spring, krakend, ingress, kube or kafka)'


def family_of(metric: str) -> str | None:
    return next((profile for prefix, profile in FAMILY.items() if metric.startswith(prefix)), None)


def queries() -> dict[str, list[str]]:
    out = {spec['sig']: [spec['q']] for spec in pm.RATES + pm.TRAFFIC + [pm.LAG]}
    out.update({spec['sig']: [spec['q'], spec['n']] for spec in pm.LATENCY})
    out.update({key: [q] for key, q in pm.HARD.items()})
    return out


def vector(results: list[tuple[dict, float]]) -> dict:
    return {'status': 'success', 'data': {'resultType': 'vector', 'result': [{'metric': m, 'value': [NOW, str(v)]} for m, v in results]}}


def busy(params, body):
    q = params['query'][0]
    now = params['time'][0] == f'{NOW:.3f}'
    if 'log4j2_events_total' in q and 'level="error"' in q and now:
        return vector([({'namespace': 'shop', 'job': 'checkout-service'}, 50)])
    if 'krakend_proxy_duration_count' in q and 'error="true"' in q and now:
        return vector([({'namespace': 'edge', 'job': 'api-gateway'}, 90)])
    if 'kube_pod_container_status_restarts_total' in q:
        return vector([({'namespace': 'shop', 'pod': 'checkout-service-7d9f8c6b5d-abcde'}, 3.0)])
    return vector([])


def routes() -> FakeHttp:
    return FakeHttp({
        (GET, f'{PROM_URL}/api/v1/query'): busy,
        (GET, f'{PROM_URL}/api/v1/query_range'): {'status': 'success', 'data': {'resultType': 'matrix', 'result': []}},
        (GET, f'{PROM_URL}/api/v1/alerts'): {'status': 'success', 'data': {'alerts': [ALERT]}},
        (GET, f'{PROM_URL}/api/v1/status/buildinfo'): {'status': 'success', 'data': {'version': '2.53.0'}},
    })


def asked(http: FakeHttp) -> list[str]:
    return [parse_qs(urlsplit(c[1]).query)['query'][0] for c in http.calls if 'query' in parse_qs(urlsplit(c[1]).query)]


def swept(store, profiles: str | None):
    http = routes()
    runtime = prom_runtime(store, {'main': {} if profiles is None else {'profiles': profiles}}, http)
    runtime.live = SimpleNamespace(rules_of=lambda key: {})
    ctx = runtime.context(prometheus.PLUGIN, runtime.instances.get('prometheus', 'main'), NOW)
    return prometheus.run(ctx), prometheus.test(ctx), http


def test_every_query_belongs_to_exactly_one_profile():
    # given
    asked_by = queries()

    # when
    families = {key: {family_of(m) for q in qs for m in METRIC.findall(q)} for key, qs in asked_by.items()}

    # then
    assert pm.PROFILES == ('spring', 'krakend', 'ingress', 'kube', 'kafka')
    assert set(pm.PROFILE_OF) == set(asked_by) | {'detect'}
    assert all(len(found) == 1 and None not in found for found in families.values()), families
    assert {key: next(iter(found)) for key, found in families.items()} == {k: v for k, v in pm.PROFILE_OF.items() if k != 'detect'}
    assert pm.PROFILE_OF['detect'] == 'kube' and set(pm.PROFILE_OF.values()) == set(pm.PROFILES)
    field = next(f for f in prometheus.PLUGIN.fields if f.key == 'profiles')
    assert (field.kind, field.default) == ('text', 'spring,krakend,ingress,kube,kafka')


def test_a_disabled_profile_runs_none_of_its_queries(store):
    # when
    narrow, _, narrow_http = swept(store, 'spring')
    full, _, full_http = swept(store, None)

    # then
    sent = asked(narrow_http)
    assert sent and not [q for q in sent if 'krakend_' in q or 'kube_' in q or 'nginx_ingress_' in q or 'kafka_' in q]
    assert any('log4j2_events_total' in q for q in sent) and any('http_server_requests_seconds' in q for q in sent)
    assert len(narrow_http.hits(f'{PROM_URL}/api/v1/alerts')) == 1 and narrow_http.hits(f'{PROM_URL}/api/v1/query_range') == []
    assert {c['key'] for c in narrow.result.found} == {'err|shop/checkout-service', 'alert|HighLatency|namespace=shop,job=checkout-service'}, 'alerts belong to no profile and always run'
    assert narrow.result.ok and narrow.result.ran == ('prom', 'kube', 'alert', 'prom:access:metrics') and narrow.deploys == []
    assert {r['svc'] for r in narrow.sections['services']} == {'checkout-service'}
    assert {c['key'] for c in full.result.found} >= {'err|shop/checkout-service', 'gw-err|edge/api-gateway', 'restarts|shop/checkout-service'}
    assert any('krakend_' in q for q in asked(full_http)) and len(full_http.hits(f'{PROM_URL}/api/v1/query_range')) == 1


def test_an_unknown_profile_is_refused(store):
    # when
    refused, tested, http = swept(store, 'spring,jvm')
    spaced, _, _ = swept(store, ' Spring , kube ')

    # then
    assert (refused.result.ok, refused.result.status) == (False, f'error: {UNKNOWN}')
    assert refused.result.found == [] and refused.sections == {}
    assert (tested.ok, tested.summary) == (False, UNKNOWN)
    assert http.calls == [], 'nothing is asked while a profile name is wrong'
    assert spaced.result.ok, 'names are trimmed and read without case'
