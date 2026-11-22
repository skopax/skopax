import ssl
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import mongomock
import pytest
from fastapi.testclient import TestClient

from skopax import contract, messages, remote
from skopax.app import create_app
from skopax.contract import Capabilities, Field
from skopax.engine import iso
from skopax.instances import Instances, InvalidInstance, PluginState, check_name
from skopax.investigator.tools import CORE_TOOLS, READ_TOOLS, ToolError, allowed_link_hosts
from skopax.kit.promql import Prometheus, PrometheusError, out_err, signal_queries
from skopax.kube import Kube
from skopax.messages import msg
from skopax.plugins import prometheus
from skopax.plugins.prometheus import engine as pm
from skopax.plugins.prometheus.engine import evaluate_prom, links_for, prom_pass
from skopax.plugintools import PluginTools
from skopax.postdeploy import PostDeploy
from skopax.registry import BUILTIN, Registry, describe
from skopax.remote import HttpError, Unreachable
from skopax.runtime import Runtime
from skopax.sealer import Sealer
from skopax.settingsources import boot
from skopax.store import Store
from tests import test_postdeploy as pd
from tests.conftest import settings
from tests.fake_http import FakeHttp
from tests.plugin_kit import KIT_KEY, PROM_URL, add_instance, prom_runtime, prom_tools
from tests.plugins.base import minimal
from tests.test_engine import NOW
from tests.test_instances import pem
from tests.test_investigator_tools import FakeProm, box

PROD = 'http://prometheus.monitoring:9090'
UI = 'https://prometheus.example.com'
EAST = 'http://prometheus.east:9090'
EAST_UI = 'https://prometheus.east.example.com'
USER = 'alice@example.com'
SPECS = pm.RATES + pm.TRAFFIC + [pm.LAG]
FAKE_SVC = 'parity-fake-service-zz'


def prom_data() -> dict:
    data = {(s['sig'], i): {} for s in SPECS for i in range(3)}
    data.update({(s['sig'] + n, i): {} for s in pm.LATENCY for n in ('', '#n') for i in range(3)})

    def put(sig, key, *values):
        for i, v in enumerate(values):
            if v is not None:
                data[(sig, i)][key] = v

    put('req', ('shop', 'checkout-service', ''), 100, 5000, 4800)
    put('req', ('billing', 'payments-api', ''), 20000, 3000, 2000)
    put('req', ('storefront', 'gone-service', ''), None, 1000, 900)
    put('ing-req', ('storefront', 'ingress/shop-web', ''), 10000, 9000, 9500)
    put('gw-req', ('identity', 'api-gateway-public', ''), 30000, 29000, 31000)
    put('err', ('shop', 'checkout-service', ''), 50, 5, 3)
    put('warn', ('billing', 'payments-api', ''), 400, 100, 90)
    put('5xx', ('billing', 'payments-api', ''), 500, 10, 8)
    put('ing-5xx', ('storefront', 'ingress/shop-web', ''), 300, 4, 6)
    put('gw-err', ('identity', 'api-gateway-public', ''), 900, 30, 20)
    put('hikari-tmo', ('shop', 'checkout-service', ''), 12, 0, 0)
    put('out-err', ('members', 'accounts-service', ''), 80, 2, 1)
    put('p95-route', ('identity', 'api-gateway-public', '/v1/orders'), 2.0, 0.5, 0.4)
    put('p95-route#n', ('identity', 'api-gateway-public', '/v1/orders'), 1000, 900, 950)
    put('p95-ingress', ('storefront', 'ingress/shop-web', ''), 1.5, 0.2, 0.25)
    put('p95-ingress#n', ('storefront', 'ingress/shop-web', ''), 5000, 4000, 4500)
    put('avg-latency', ('billing', 'payments-api', ''), 0.9, 0.1, 0.12)
    put('avg-latency#n', ('billing', 'payments-api', ''), 20000, 3000, 2000)
    put('kafka-lag', ('streaming', 'kafka/logstash-consumer', ''), 50000, 100, 80)
    return data


HARD = {
    'restarts': [({'namespace': 'shop', 'pod': 'checkout-service-7d9f8c6b5d-abcde'}, 3.0)],
    'reason': [({'namespace': 'shop', 'pod': 'checkout-service-7d9f8c6b5d-abcde', 'reason': 'OOMKilled'}, 1.0)],
    'waiting': [({'namespace': 'billing', 'pod': 'payments-api-6c8d9f7b4c-xyz12', 'reason': 'CrashLoopBackOff'}, 1.0)],
    'unavail': [({'namespace': 'billing', 'deployment': 'payments-api'}, 1.0), ({'namespace': 'storefront', 'deployment': FAKE_SVC}, 1.0)],
    'noready': [({'namespace': 'billing', 'deployment': 'payments-api'}, 0.0)],
    'notready': [({'node': 'node-1'}, 0.0)],
    'pressure': [({'node': 'node-2', 'condition': 'DiskPressure'}, 1.0)],
}
ALERTS = [
    {'state': 'firing', 'activeAt': '2026-10-19T10:00:00Z', 'labels': {'alertname': 'KubePodCrashLooping', 'severity': 'warning', 'namespace': 'billing', 'job': 'kube-state-metrics', 'pod': 'x'},
     'annotations': {'summary': 'Pod is crash looping.\nSecond line'}},
    {'state': 'firing', 'labels': {'alertname': 'Watchdog', 'severity': 'none'}},
    {'state': 'pending', 'labels': {'alertname': 'KafkaLag', 'severity': 'critical'}},
    {'state': 'firing', 'activeAt': '2026-10-19T11:00:00Z', 'labels': {'alertname': 'DiskFull', 'severity': 'critical', 'persistentvolumeclaim': 'data-0'}, 'annotations': {'description': 'disk'}},
]
DEPLOYS = [('storefront', FAKE_SVC, 'aaaa1111', 'bbbb2222', NOW - 600), ('storefront', FAKE_SVC + '-old', 'cccc3333', 'dddd4444', NOW - 7200)]


def vector(results: list[tuple[dict, float]]) -> dict:
    return {'status': 'success', 'data': {'resultType': 'vector', 'result': [{'metric': m, 'value': [NOW, str(v)]} for m, v in results]}}


def range_result(series: list[tuple[dict, float]]) -> dict:
    return {'status': 'success', 'data': {'resultType': 'matrix', 'result': [{'metric': m, 'values': [[t, '1'], [t + 120, '1']]} for m, t in series]}}


DETECT_SERIES = [
    ({'namespace': 'billing', 'pod': 'payments-api-6c8d9f7b4c-xyz12', 'container': 'java', 'image': 'images/payments-api/master:aaaa1111'}, NOW - 86000),
    ({'namespace': 'billing', 'pod': 'payments-api-7f9a8b6c5d-qwe34', 'container': 'java', 'image': 'images/payments-api/master:bbbb2222'}, NOW - 900),
    ({'namespace': 'billing', 'pod': 'payments-api-7f9a8b6c5d-qwe34', 'container': 'istio-proxy', 'image': 'istio:1'}, NOW - 900),
    ({'namespace': 'legacy', 'pod': 'checkout-service-1a2b3c4d5e-zzz99', 'container': 'java', 'image': 'images/cart/master:cccc3333'}, NOW - 86000),
]


def router(restarts: bool = True) -> FakeHttp:
    def query(params, body):
        q = params['query'][0]
        if restarts and 'kube_pod_container_status_restarts_total' in q:
            return vector(HARD['restarts'])
        return vector([])

    return FakeHttp({
        ('GET', f'{PROD}/api/v1/query'): query,
        ('GET', f'{PROD}/api/v1/query_range'): range_result(DETECT_SERIES),
        ('GET', f'{PROD}/api/v1/alerts'): {'status': 'success', 'data': {'alerts': []}},
    })


def errors_router(series: list[tuple[str, str]]) -> FakeHttp:
    def query(params, body):
        q = params['query'][0]
        if 'log4j2_events_total' in q and 'level="error"' in q and params['time'][0] == f'{NOW:.3f}':
            return vector([({'namespace': ns, 'job': svc}, 50) for ns, svc in series])
        return vector([])

    return FakeHttp({
        ('GET', f'{PROD}/api/v1/query'): query,
        ('GET', f'{PROD}/api/v1/query_range'): range_result([]),
        ('GET', f'{PROD}/api/v1/alerts'): {'status': 'success', 'data': {'alerts': []}},
    })


def plain(specs: list[dict]) -> list[dict]:
    return [{k: (v.__name__ if callable(v) else v) for k, v in s.items()} for s in specs]


def test_the_client_asks_the_api_and_keeps_finite_values_only():
    # given
    http = FakeHttp({('GET', f'{PROD}/api/v1/query'): vector([({'job': 'a'}, 1.5), ({'job': 'b'}, float('nan'))])})

    # when
    got = Prometheus(PROD + '/', http).vec('up', NOW)

    # then
    assert got == [({'job': 'a'}, 1.5)]
    assert http.calls[0][1].startswith(f'{PROD}/api/v1/query?query=up&time=')


def test_the_client_retries_an_unreachable_server_once_and_names_refusals():
    # given
    waits = []
    answers = [Unreachable('down'), vector([])]
    flaky = FakeHttp({('GET', f'{PROD}/api/v1/query'): lambda p, b: answers.pop(0)})
    refused = FakeHttp({('GET', f'{PROD}/api/v1/query'): HttpError(503, 'x')})
    down = FakeHttp({('GET', f'{PROD}/api/v1/query'): Unreachable(f'{PROD}/api/v1/query unreachable (timed out)')})

    # then
    assert Prometheus(PROD, flaky, sleep=waits.append).vec('up', NOW) == []
    with pytest.raises(PrometheusError, match='Prometheus query HTTP 503'):
        Prometheus(PROD, refused, sleep=waits.append).vec('up', NOW)
    with pytest.raises(PrometheusError, match='unreachable'):
        Prometheus(PROD, down, sleep=waits.append).vec('up', NOW)
    assert len(down.calls) == 2 and waits == [1, 1]


def test_detect_finds_image_changes_inside_the_window_and_skips_sidecars():
    # when
    got = Prometheus(PROD, router()).detect('.*', 24, NOW)

    # then
    assert got == [('billing', 'payments-api', 'aaaa1111', 'bbbb2222', NOW - 900)]


def test_prom_pass_asks_every_query_three_times_plus_the_hard_checks_alerts_and_deploys():
    # given
    http = router()

    # when
    data, hard, alerts, deploys = pm.prom_pass(Prometheus(PROD, http), NOW)

    # then
    assert len(data) == 3 * (len(SPECS) + 2 * len(pm.LATENCY))
    assert set(hard) == set(pm.HARD) and hard['restarts'] == HARD['restarts']
    assert alerts == [] and deploys == [('billing', 'payments-api', 'aaaa1111', 'bbbb2222', NOW - 900)]
    assert len(http.hits(f'{PROD}/api/v1/alerts')) == 1 and len(http.hits(f'{PROD}/api/v1/query_range')) == 1


def test_evaluate_prom_flags_rates_latency_kube_and_alerts_and_fills_image_by():
    # when
    found, rows, routes, deps, warm = evaluate_prom(NOW, prom_data(), HARD, ALERTS, DEPLOYS, lambda ns, svc, new: {'names': ['Ada'], 'sha': new})

    # then
    sigs = {x['sig'] for x in found}
    assert {'traffic-drop', 'traffic-spike', 'err', '5xx', 'ing-5xx', 'gw-err', 'hikari-tmo', 'p95-route', 'p95-ingress', 'avg-latency', 'kafka-lag',
            'restarts', 'waiting', 'unavailable', 'no-ready-pods', 'node-not-ready', 'node-pressure', 'alert'} <= sigs
    assert {x['key'] for x in found if x['sig'] == 'alert'} == {'alert|KubePodCrashLooping|namespace=billing,job=kube-state-metrics', 'alert|DiskFull|persistentvolumeclaim=data-0'}
    assert rows[('storefront', FAKE_SVC)]['imageBy'] == {'names': ['Ada'], 'sha': 'bbbb2222'}
    assert warm == {('storefront', FAKE_SVC)}
    assert not any(x['sig'] == 'unavailable' and x['svc'] == FAKE_SVC for x in found)
    assert routes[0]['route'] == '/v1/orders' and [d['svc'] for d in deps] == [FAKE_SVC, FAKE_SVC + '-old']


def test_namespace_filter_comes_from_settings():
    # given
    http = errors_router([('shop', 'checkout-service'), ('shop-staging', 'checkout-service'), ('payments', 'payments-api')])

    # when
    found = evaluate_prom(NOW, *prom_pass(Prometheus(PROD, http, namespaces='shop|payments'), NOW))[0]

    # then
    asked = [parse_qs(urlsplit(c[1]).query)['query'][0] for c in http.calls if 'query' in parse_qs(urlsplit(c[1]).query)]
    assert {x['ns'] for x in found} == {'shop', 'payments'}
    assert any('namespace=~"shop|payments"' in q for q in asked)
    assert not any('-prod' in q for q in asked)


def asked_queries(http) -> list[str]:
    return [parse_qs(urlsplit(c[1]).query)['query'][0] for c in http.calls if 'query' in parse_qs(urlsplit(c[1]).query)]


def test_the_ingress_queries_exclude_nothing_unless_a_pattern_is_set():
    # given
    plain, excluding, quoted = router(), router(), router()

    # when
    prom_pass(Prometheus(PROD, plain), NOW)
    prom_pass(Prometheus(PROD, excluding), NOW, ingress_exclude='.*-canary')
    prom_pass(Prometheus(PROD, quoted), NOW, ingress_exclude='a"b')

    # then
    ingress = [q for q in asked_queries(plain) if 'nginx_ingress' in q]
    assert ingress and not any('ingress!~' in q for q in asked_queries(plain))
    assert all('exported_namespace=~".*"' in q for q in ingress)
    marked = [q for q in asked_queries(excluding) if 'ingress!~' in q]
    assert marked and all('nginx_ingress' in q and 'exported_namespace=~".*",ingress!~".*-canary"' in q for q in marked)
    assert len(marked) == len([q for q in asked_queries(excluding) if 'nginx_ingress' in q])
    assert any('ingress!~"a\\"b"' in q for q in asked_queries(quoted))
    assert not any(pm.INGRESS_EXCLUDE in q for q in asked_queries(excluding) + asked_queries(plain))


def test_the_ingress_exclusion_is_a_regex_setting_with_no_default_that_reaches_the_pass(monkeypatch):
    # given
    fields = {f.key: f for f in prometheus.PLUGIN.fields}
    seen = []
    monkeypatch.setattr(prometheus, 'prom_pass', lambda client, now, profiles, ingress_exclude='': seen.append(ingress_exclude) or ({}, {}, [], []))
    monkeypatch.setattr(prometheus, 'evaluate_prom', lambda *args: ([], {}, [], [], set()))

    def ctx(config: dict) -> SimpleNamespace:
        info = contract.InstanceInfo('main', True, None, {'url': PROD, **config})
        return SimpleNamespace(instance=info, http=lambda: FakeHttp({}), now=NOW, rules=lambda name: {})

    # when
    field = fields['ingress-exclude']
    prometheus.run(ctx({}))
    prometheus.run(ctx({'ingress-exclude': '.*-canary'}))

    # then
    assert (field.label, field.kind, field.default, field.required) == ('Ingress exclusion pattern', 'regex', '', False)
    assert field.help.strip() and list(fields).index('ingress-exclude') == list(fields).index('namespaces') + 1
    assert seen == ['', '.*-canary']


def test_cards_have_no_cluster_segment():
    # given
    http = errors_router([('shop', 'checkout-service')])

    # when
    found = evaluate_prom(NOW, *prom_pass(Prometheus(PROD, http), NOW))[0]

    # then
    assert {x['sig'] for x in found} == {'err'}
    assert all(x['key'] == '|'.join(p for p in (x['sig'], 'shop/checkout-service', x['sub']) if p) for x in found)
    assert not any('cluster=' in x['key'] or 'cluster' in x for x in found)


def test_links_cover_only_the_prometheus_sources():
    # then
    assert links_for({'src': 'logs', 'sig': 'log'}, UI) is None
    assert links_for({'src': 'cf', 'sig': 'cf-health'}, UI) is None
    assert links_for({'src': 'kube', 'sig': 'node-not-ready', 'ns': 'cluster', 'svc': 'node-1'}, UI) == []


GET = 'GET'
ALERT = {'state': 'firing', 'activeAt': '2026-10-19T10:00:00Z', 'labels': {'alertname': 'HighLatency', 'namespace': 'shop', 'job': 'checkout-service', 'severity': 'warning'}, 'annotations': {'summary': 'slow'}}


def rich_routes(base: str, errors: int = 50, alerts: list | None = None, down: list | None = None) -> dict:
    def query(params, body):
        if down:
            raise HttpError(502, 'x')
        q = params['query'][0]
        if 'log4j2_events_total' in q and 'level="error"' in q and params['time'][0] == f'{NOW:.3f}':
            return vector([({'namespace': 'shop', 'job': 'checkout-service'}, errors)])
        if 'condition="Ready"' in q:
            return vector([({'node': 'node-1'}, 0)])
        return vector([])

    return {(GET, f'{base}/api/v1/query'): query, (GET, f'{base}/api/v1/query_range'): range_result([]),
            (GET, f'{base}/api/v1/alerts'): {'status': 'success', 'data': {'alerts': [ALERT] if alerts is None else alerts}}}


def app_with(store: Store, http, **instances: dict) -> TestClient:
    for index, (name, values) in enumerate(instances.items()):
        add_instance(store, prometheus.PLUGIN, name, primary=index == 0, **values)
    return TestClient(create_app(settings(), store, transport=http))


def card_map(store: Store) -> dict[str, dict]:
    return {a['key']: a for a in store.load()['anomalies']}


def test_prometheus_identity_and_manifest():
    # given
    registry = Registry.of(prometheus.PLUGIN)

    # when
    loaded = registry.get('prometheus')
    manifest = describe(prometheus.PLUGIN, icon=True)

    # then
    assert (loaded.state, loaded.reason) == ('ok', None) and 'skopax.plugins.prometheus' in BUILTIN
    assert Registry.load({}, entry_points=False).get('prometheus').state == 'ok'
    assert (manifest['name'], manifest['version'], manifest['category'], manifest['author'], manifest['licence'], manifest['core'], manifest['edition']) == (
        'Prometheus', '0.1.0', 'metrics', 'Skopax', 'Apache-2.0', '>=0.1,<0.6', 'community')
    assert manifest['capabilities'] == {'hosts': ['@url', '@ui-url'], 'secrets': ['token', 'password'], 'agentReads': False}
    assert [(f['key'], f['kind']) for f in manifest['fields']] == [('url', 'url'), ('ui-url', 'url'), ('auth', 'choice'), ('token', 'secret'), ('username', 'text'), ('password', 'secret'), ('ca', 'pem'), ('ca-pinned', 'bool'), ('namespaces', 'regex'), ('ingress-exclude', 'regex'), ('profiles', 'text')]
    assert manifest['checks'] == [{'id': 'prom', 'label': 'Errors, latency, restarts and alerts', 'period': 900, 'offset': 120, 'ran': ['prom', 'kube', 'alert']}]
    assert [(t['name'], t['kind']) for t in manifest['tools']] == [('promql', 'read')] and manifest['knowledge'] == ['promql']
    assert manifest['rules'][0]['id'] == 'rates' and manifest['rules'][0]['defaults'] == {'err': 10, 'warn': 100, '5xx': 5, 'out-err': 10, 'gw-err': 20, 'ing-5xx': 10, 'hikari-tmo': 1, 'kafka-lag': 1000}
    assert registry.icon('prometheus').startswith('<svg') and len(prometheus.PLUGIN.knowledge[0].text) < 8000


def test_env_defines_the_primary_instance(store: Store, tmp_path):
    # given
    env = {'PROMETHEUS_URL': PROD, 'PROMETHEUS_UI_URL': UI, 'PROMETHEUS_NAMESPACES': 'shop|payments'}

    # when
    b = boot(env)
    bare = boot({'PROMETHEUS_URL': PROD})
    none = boot({})
    with TestClient(create_app(b.settings, store, registry=b.registry, sources=b.source_set(), kube=Kube(root=tmp_path))) as c:
        main = c.app.state.instances.get('prometheus', 'main')
        jobs = [j.name for j in c.app.state.scheduler.jobs]

    # then
    assert b.sources[0].instances == ({'plugin': 'prometheus', 'name': 'main', 'primary': 'true', 'url': PROD, 'ui-url': UI, 'namespaces': 'shop|payments'},)
    assert bare.sources[0].instances[0]['namespaces'] == '.*' and bare.sources[0].instances[0]['ui-url'] == ''
    assert none.sources[0].instances == ()
    assert (main.primary, main.enabled, main.problem, main.values['url'], main.values['namespaces']) == (True, True, None, PROD, 'shop|payments')
    assert jobs == ['probe', 'version-check', 'postdeploy', 'prometheus.prom']
    assert not hasattr(b.settings, 'prometheus_url') and not hasattr(b.settings, 'prometheus_namespaces') and not hasattr(b.settings, 'prometheus_ui_url')


def test_connection_test_reports_version_and_latency(store: Store):
    # given
    path = f'{PROM_URL}/api/v1/status/buildinfo'
    good, denied, down = (FakeHttp({(GET, path): answer}) for answer in ({'status': 'success', 'data': {'version': '2.53.0'}}, HttpError(401, path), Unreachable('x')))

    def check(http):
        rt = prom_runtime(store, {'main': {}}, http)
        return prometheus.test(rt.context(prometheus.PLUGIN, rt.instances.get('prometheus', 'main'), NOW))

    # when
    ok, refused, unreachable = check(good), check(denied), check(down)

    # then
    assert (ok.ok, ok.summary) == (True, 'Prometheus 2.53.0') and isinstance(ok.ms, int) and ok.ms >= 0
    assert (refused.ok, refused.summary) == (False, messages.msg('connection.no_credentials', status=401)), 'an instance with no auth sent no credentials, so none were refused'
    assert unreachable.ok is False and unreachable.summary == messages.msg('connection.unreachable')
    assert good.calls[0][1] == path


def test_auth_and_custom_ca_reach_the_transport(store: Store, monkeypatch):
    # given
    http = FakeHttp({(GET, f'{PROM_URL}/api/v1/query'): vector([])})
    rt = prom_runtime(store, {'bearer': {'auth': 'bearer', 'token': 't'}, 'basic': {'auth': 'basic', 'username': 'u', 'password': 'p'}, 'plain': {}}, http)

    def ask(runtime, name):
        ctx = runtime.context(prometheus.PLUGIN, runtime.instances.get('prometheus', name), NOW)
        client = prometheus.client_for(ctx.instance, ctx.http())
        client.vec('up', NOW)
        return client

    # when
    for name in ('bearer', 'basic', 'plain'):
        ask(rt, name)
    cert = pem()
    secured = prom_runtime(store, {'main': {'ca': cert}})
    real, seen = ssl.create_default_context, []
    monkeypatch.setattr(ssl, 'create_default_context', lambda *a, **k: seen.append(k.get('cadata')) or real(*a, **k))
    ctx = secured.context(prometheus.PLUGIN, secured.instances.get('prometheus', 'main'), NOW)
    sender = ctx.http()

    # then
    sent = [c[2] for c in http.calls]
    assert sent[0]['Authorization'] == 'Bearer t' and sent[1]['Authorization'] == 'Basic dTpw' and 'Authorization' not in sent[2]
    assert seen == [cert] and sender is not remote.send and prometheus.client_for(ctx.instance, sender).transport is sender


def test_second_instance_keys_follow_todays_scheme(store: Store):
    # given
    http = FakeHttp(rich_routes(PROD) | rich_routes(EAST))

    # when
    with app_with(store, http, main={'url': PROD}, east={'url': EAST}) as c:
        applied = c.app.state.sweeper.run('prometheus', 'prom', NOW)
        state = store.load()
    cards = card_map(store)

    # then
    assert applied is not None
    assert set(cards) == {'err|shop/checkout-service', 'alert|HighLatency|namespace=shop,job=checkout-service', 'node-not-ready|cluster/node-1', 'err|shop/checkout-service@east',
                          'alert|HighLatency|cluster=east,namespace=shop,job=checkout-service@east', 'node-not-ready|cluster-east/node-1@east'}
    assert not any('cluster' in cards[k] for k in ('err|shop/checkout-service', 'alert|HighLatency|namespace=shop,job=checkout-service', 'node-not-ready|cluster/node-1'))
    east = cards['node-not-ready|cluster-east/node-1@east']
    assert (east['cluster'], east['short'], east['ranKey']) == ('east', 'CRIT EAST node-1 node not ready', 'kube-east')
    assert cards['alert|HighLatency|cluster=east,namespace=shop,job=checkout-service@east']['cluster'] == 'east'
    assert set(state['serverPasses']) == {'prom', 'prom-east'} and state['serverPasses']['prom']['status'] == 'ok'
    assert state['serverPasses']['prom-east']['status'] == 'ok' and cards['err|shop/checkout-service@east']['ranKey'] == 'prom-east'


def test_rate_floors_come_from_the_rule_set(store: Store):
    # given
    http = FakeHttp(rich_routes(PROD, errors=40, alerts=[]))

    # when
    with app_with(store, http, main={'url': PROD}) as c:
        c.app.state.sweeper.run('prometheus', 'prom', NOW)
        default = {k for k in card_map(store) if k.startswith('err|')}
        c.app.state.runtime.live.set('prometheus.rates', '{"err": 50}', USER)
        store.anomalies.delete_many({})
        store.save_state({'anomalies': [], 'initialized': True}, None)
        c.app.state.sweeper.run('prometheus', 'prom', NOW)
        raised = {k for k in card_map(store) if k.startswith('err|')}

    # then
    assert default == {'err|shop/checkout-service'} and raised == set()


def test_a_failed_run_is_a_failed_pass_and_the_rows_stay(store: Store):
    # given
    down = []
    http = FakeHttp(rich_routes(PROD, alerts=[], down=down))

    # when
    with app_with(store, http, main={'url': PROD}) as c:
        c.app.state.sweeper.run('prometheus', 'prom', NOW)
        first = store.load()
        down.append(1)
        c.app.state.sweeper.run('prometheus', 'prom', NOW + 900)
        second = store.load()

    # then
    assert first['sources']['prom'] == 'ok' and second['sources']['prom'] == 'error: Prometheus query HTTP 502'
    assert second['services'] == first['services'] and second['deploys'] == first['deploys']


def test_promql_runs_on_the_chosen_cluster(store: Store):
    # given
    http = FakeHttp({(GET, f'{PROD}/api/v1/query'): vector([({'job': 'main'}, 1)]), (GET, f'{EAST}/api/v1/query'): vector([({'job': 'east'}, 2)]),
                     (GET, f'{PROD}/api/v1/query_range'): HttpError(503, 'x')})
    tools = PluginTools(prom_runtime(store, {'main': {'url': PROD}, 'east': {'url': EAST}}, http))

    # when
    default = tools.call('promql', {'expr': 'up'}, NOW)
    east = tools.call('promql', {'expr': 'up', 'cluster': 'east'}, NOW)
    definition = next(d for d in tools.definitions() if d['name'] == 'promql')
    with pytest.raises(ToolError) as refused:
        tools.call('promql', {'expr': 'up', 'range_minutes': 30}, NOW)
    with pytest.raises(ToolError) as unknown:
        tools.call('promql', {'expr': 'up', 'cluster': 'west'}, NOW)

    # then
    assert default[0]['metric'] == {'job': 'main'} and east[0]['metric'] == {'job': 'east'}
    assert definition['input_schema']['properties']['cluster']['enum'] == ['main', 'east'] and 'default: main' in definition['input_schema']['properties']['cluster']['description']
    assert str(refused.value) == 'Prometheus query_range HTTP 503' and 'must be one of' in str(unknown.value)
    assert len(http.hits(f'{EAST}/api/v1/query')) == 1


def test_the_investigator_offers_exactly_one_promql_and_core_no_longer_owns_it(store: Store):
    # when
    plain = [d['name'] for d in box(store).definitions()]
    served = [d['name'] for d in box(store, plugin_tools=prom_tools(FakeProm([]))).definitions()]

    # then
    assert plain.count('promql') == 0 and served.count('promql') == 1
    assert 'promql' not in CORE_TOOLS and 'promql' not in {t['name'] for t in READ_TOOLS}


def test_an_unavailable_default_instance_is_named_not_guessed(store: Store):
    # given
    rt = prom_runtime(store, {'main': {'url': PROD}, 'east': {'url': EAST}}, FakeHttp())
    rt.instances.set_enabled('prometheus', 'main', False, USER)
    rt.refresh()

    # when
    with pytest.raises(ToolError) as refused:
        PluginTools(rt).call('promql', {'expr': 'up'}, NOW)

    # then
    assert 'default instance main of prometheus is not available' in str(refused.value) and 'east' in str(refused.value)


def test_links_and_report_row_links_follow_the_card_instance(store: Store):
    # given
    rt = prom_runtime(store, {'main': {'url': PROD, 'ui-url': UI}, 'east': {'url': EAST, 'ui-url': EAST_UI, 'namespaces': 'shop'}}, FakeHttp())
    card = {'src': 'prom', 'sig': 'err', 'ns': 'shop', 'svc': 'checkout-service', 'sub': ''}
    report = {'windowFrom': '2026-10-13T05:06:00Z', 'windowTo': '2026-10-13T05:35:00Z', 'rows': [
        {'ns': 'shop', 'svc': 'a-service', 'metrics': 'req 9 (1/2)'}, {'ns': 'shop', 'svc': 'b-service', 'cluster': 'east', 'metrics': 'req 9 (1/2)', 'links': {'err': 'https://kibana.example.com/x'}},
        {'ns': 'shop', 'svc': 'c-service', 'cluster': 'gone', 'metrics': 'req 9 (1/2)'}]}

    # when
    main_card, east_card = rt.links(card), rt.links(card | {'cluster': 'east'})
    other = rt.links({'src': 'logs', 'sig': 'log'})
    linked = rt.row_links(report)['rows']

    # then
    assert [urlsplit(x['url']).netloc for x in main_card] == ['prometheus.example.com'] and [urlsplit(x['url']).netloc for x in east_card] == ['prometheus.east.example.com']
    assert other is None
    assert [urlsplit(r.get('links', {}).get('req', '')).netloc for r in linked] == ['prometheus.example.com', 'prometheus.east.example.com', '']
    assert linked[1]['links']['err'] == 'https://kibana.example.com/x'
    assert parse_qs(urlsplit(linked[0]['links']['req']).query)['g0.expr'] == ['sum(increase(http_server_requests_seconds_count{namespace="shop",job="a-service"}[1740s]))']


def test_postdeploy_measures_each_row_on_its_instance(store: Store):
    # given
    store.add_deploys([{'ns': pd.NS, 'svc': 'checkout-service', 'fromImage': 'aaaaaaaa', 'toImage': 'bbbbbbbb', 'at': pd.at(3600)},
                       {'ns': pd.NS, 'svc': 'orders-service', 'fromImage': 'cccccccc', 'toImage': 'dddddddd', 'at': pd.at(1800), 'cluster': 'east'},
                       {'ns': pd.NS, 'svc': 'billing-service', 'fromImage': 'eeeeeeee', 'toImage': 'ffffffff', 'at': pd.at(1700), 'cluster': 'gone'}])
    pd.settled_action(store, 3600, 1700)
    main = pd.prom({('checkout-service', 'err', 0): 40, ('orders-service', 'restarts', 0): 9})
    east = pd.prom({('orders-service', 'restarts', 0): 1, ('checkout-service', 'err', 0): 99})
    asked = []
    clients = {None: main, 'east': east}
    check = PostDeploy(store, lambda cluster: asked.append(cluster) or clients.get(cluster), pd.FakeJira([], enabled=False), clock=lambda: pd.NOW)

    # when
    report = store.deploy_report(check.run_once(pd.NOW)['id'])
    rows = {r['svc']: r for r in report['rows']}

    # then
    assert rows['checkout-service']['flags'] == ['err up'] and 'cluster' not in rows['checkout-service']
    assert rows['orders-service']['flags'] == ['restarts=1'] and rows['orders-service']['cluster'] == 'east'
    assert rows['billing-service']['flags'] == [messages.msg('reports.no_prometheus', instance='gone')] and rows['billing-service']['metrics'] == ''
    assert set(asked) == {None, 'east', 'gone'}
    assert PostDeploy(store, lambda cluster: None, pd.FakeJira([], enabled=False)).enabled is False
    assert PostDeploy(store, lambda cluster: None, pd.FakeJira([], enabled=False), serving=lambda: True).enabled is True


def test_server_info_lists_the_prometheus_tile(store: Store):
    # given
    http = FakeHttp(rich_routes(PROD, errors=0, alerts=[]) | rich_routes(EAST, errors=0, alerts=[]))

    # when
    with app_with(store, http, main={'url': PROD}, east={'url': EAST}) as c:
        c.app.state.sweeper.run('prometheus', 'prom', NOW)
        info = c.get('/api/server').json()

    # then
    assert info['plugins'] == [{'id': 'prometheus', 'name': 'Prometheus', 'icon': '/plugins/prometheus/icon.svg', 'tab': 'svcs', 'passes': ['prom', 'prom-east'],
                                'instances': [{'name': 'main', 'primary': True, 'pass': 'prom', 'status': 'ok', 'state': 'ok', 'text': msg('integrations.health.ok')},
                                              {'name': 'east', 'primary': False, 'pass': 'prom-east', 'status': 'ok', 'state': 'ok', 'text': msg('integrations.health.ok')}], 'pages': [], 'queued': False, 'types': []}]
    assert 'prometheus' not in info


def test_prom_kube_and_alert_are_reserved_instance_names():
    # given
    registry = Registry.of(prometheus.PLUGIN)

    # then
    assert {'prom', 'kube', 'alert'} <= registry.reserved_names()
    for name in ('prom', 'kube', 'alert', 'prom-east'):
        with pytest.raises(InvalidInstance):
            check_name(registry, name, 'prometheus')


def test_the_app_runs_prometheus_only_when_it_has_an_enabled_instance_and_records_the_deploys(store: Store):
    # given
    now = NOW
    store.save_state({'anomalies': [], 'initialized': True, 'sources': {'cf': 'ok'}}, None)
    store.add_deploys([{'ns': 'billing', 'svc': 'payments-api', 'toImage': 'bbbb2222', 'fromImage': 'aaaa1111', 'at': iso(now - 900),
                        'imageBy': {'names': ['Ada'], 'sha': 'bbbb2222'}}])
    http = router()

    # when
    with app_with(store, http, main={'url': PROD, 'ui-url': UI}) as c:
        jobs = {j.name: j for j in c.app.state.scheduler.jobs}
        info = c.get('/api/server').json()
        applied = c.app.state.sweeper.run('prometheus', 'prom', now)
    state = store.load()

    # then
    assert (jobs['prometheus.prom'].period, jobs['prometheus.prom'].offset) == (900, 120)
    assert [p['id'] for p in info['plugins']] == ['prometheus']
    assert applied is not None
    assert state['sources'] == {'cf': 'ok', 'prom': 'ok'}
    assert set(state['serverPasses']) == {'prom'}
    assert {a['key'] for a in state['anomalies']} == {'restarts|shop/checkout-service'}
    assert all(a['links'] for a in state['anomalies'])
    row = next(r for r in state['services'] if r['svc'] == 'payments-api')
    assert row['imageBy'] == {'names': ['Ada'], 'sha': 'bbbb2222'} and row['status'] == 'ok'
    assert [(d['svc'], d['old'], d['new']) for d in state['deploys']] == [('payments-api', 'aaaa1111', 'bbbb2222')]
    assert [d['toImage'] for d in store.deploys_since(iso(now - 3600))] == ['bbbb2222']


def test_without_urls_no_prometheus_job_is_registered(store: Store):
    # when
    with TestClient(create_app(settings(), store)) as c:
        names = [j.name for j in c.app.state.scheduler.jobs]
        info = c.get('/api/server').json()

    # then
    assert 'prometheus.prom' not in names and info['plugins'] == []


def test_failed_outbound_calls_leave_out_fast_cancellations_but_keep_slow_ones():
    # given
    rule = pm.scoped(next(r for r in pm.RATES if r['sig'] == 'out-err')['q'], '.*')
    cancel = 'namespace=~".*",outcome="UNKNOWN",exception="none"'

    # then
    assert rule.startswith('sum by (namespace, job) (') and 'outcome=~"SERVER_ERROR|UNKNOWN"' not in rule
    assert 'outcome="SERVER_ERROR"}[60m])' in rule and 'outcome="UNKNOWN",exception!="none"}[60m])' in rule
    assert (f'(increase(http_client_requests_seconds_count{{{cancel}}}[60m]) and increase(http_client_requests_seconds_sum{{{cancel}}}[60m])'
            f' / increase(http_client_requests_seconds_count{{{cancel}}}[60m]) >= 1)') in rule


def test_the_out_err_graph_link_and_the_deploy_report_count_with_the_same_rule():
    # given
    card = {'src': 'prom', 'sig': 'out-err', 'ns': 'web', 'svc': 'web-bff'}
    sel = 'namespace="web",job="web-bff"'

    # when
    graph = next(link for link in links_for(card, UI) if link['label'] == 'Prometheus graph')['url']
    report = signal_queries('web', 'web-bff', '2h')['out-err']

    # then
    assert parse_qs(urlsplit(graph).query)['g0.expr'] == [out_err(sel, '60m', 'clientName, uri, status, exception')]
    assert report == out_err(sel, '2h') and report.startswith('sum (') and 'exception!="none"' in report and '>= 1))' in report


def test_the_postdeploy_job_follows_the_instances_present_at_boot_and_added_later(store: Store):
    # given
    http = FakeHttp(rich_routes(PROD, alerts=[]))

    # when
    with TestClient(create_app(settings(), store, transport=http)) as c:
        before = [j.name for j in c.app.state.scheduler.jobs]
        before_info = c.get('/api/server').json()['postdeploy']
        c.app.state.instances.create('prometheus', 'main', {'url': PROD}, True, USER)
        c.app.state.runtime.refresh()
        after = [j.name for j in c.app.state.scheduler.jobs]
        after_info = c.get('/api/server').json()['postdeploy']
    with app_with(Store(mongomock.MongoClient(tz_aware=True)['skopax']), http, east={'url': PROD}) as c:
        at_boot = [j.name for j in c.app.state.scheduler.jobs]

    # then
    assert 'postdeploy' not in before and before_info is False
    assert 'postdeploy' in after and after_info is True
    assert at_boot == ['version-check', 'postdeploy', 'prometheus.prom']


def test_the_postdeploy_job_runs_each_row_on_its_own_available_instance(store: Store):
    # given
    http = FakeHttp({(GET, f'{EAST}/api/v1/query'): vector([]), (GET, f'{PROD}/api/v1/query'): vector([])})
    store.add_deploys([{'ns': pd.NS, 'svc': 'checkout-service', 'fromImage': 'aaaaaaaa', 'toImage': 'bbbbbbbb', 'at': pd.at(3600)},
                       {'ns': pd.NS, 'svc': 'orders-service', 'fromImage': 'cccccccc', 'toImage': 'dddddddd', 'at': pd.at(1800), 'cluster': 'east'}])
    pd.settled_action(store, 3600, 1800)
    add_instance(store, prometheus.PLUGIN, 'main', primary=True, enabled=False, url=PROD)
    add_instance(store, prometheus.PLUGIN, 'east', url=EAST)

    # when
    with TestClient(create_app(settings(), store, transport=http)) as c:
        job = next(j for j in c.app.state.scheduler.jobs if j.name == 'postdeploy')
        job.run(pd.NOW)
    rows = {r['svc']: r for r in store.list_deploy_reports(1)[0]['rows']}

    # then
    assert rows['checkout-service']['flags'] == [messages.msg('reports.no_prometheus', instance='main')]
    assert rows['orders-service']['flags'] == [] and rows['orders-service']['cluster'] == 'east'
    assert http.hits(f'{PROD}/api/v1/query') == [] and len(http.hits(f'{EAST}/api/v1/query')) > 0


def test_a_url_only_instance_makes_its_host_an_allowed_link_host(store: Store):
    # given
    only_url = prom_runtime(store, {'main': {'url': 'https://prom.example.net'}})
    with_ui = prom_runtime(store, {'main': {'url': 'https://prom.example.net', 'ui-url': 'https://graphs.example.net'}})

    # then
    assert 'prom.example.net' in allowed_link_hosts(settings(), only_url.ui_hosts())
    assert 'graphs.example.net' in allowed_link_hosts(settings(), with_ui.ui_hosts()) and 'prom.example.net' not in allowed_link_hosts(settings(), with_ui.ui_hosts())


def test_the_postdeploy_job_measures_a_primary_row_on_the_primary_and_an_east_row_on_east(store: Store):
    # given
    def answers(errors: float, restarts: float):
        def query(params, body):
            q = params['query'][0]
            if 'log4j2_events_total' in q and 'level="error"' in q:
                return vector([({}, errors if params['time'][0] == f'{pd.NOW:.3f}' else 5)])
            if 'kube_pod_container_status_restarts_total' in q:
                return vector([({}, restarts)])
            return vector([])

        return query

    http = FakeHttp({(GET, f'{PROD}/api/v1/query'): answers(40, 0), (GET, f'{EAST}/api/v1/query'): answers(5, 1)})
    store.add_deploys([{'ns': pd.NS, 'svc': 'checkout-service', 'fromImage': 'aaaaaaaa', 'toImage': 'bbbbbbbb', 'at': pd.at(3600)},
                       {'ns': pd.NS, 'svc': 'orders-service', 'fromImage': 'cccccccc', 'toImage': 'dddddddd', 'at': pd.at(1800), 'cluster': 'east'}])
    pd.settled_action(store, 3600, 1800)
    add_instance(store, prometheus.PLUGIN, 'main', primary=True, url=PROD)
    add_instance(store, prometheus.PLUGIN, 'east', url=EAST)

    # when
    with TestClient(create_app(settings(), store, transport=http)) as c:
        next(j for j in c.app.state.scheduler.jobs if j.name == 'postdeploy').run(pd.NOW)
    rows = {r['svc']: r for r in store.list_deploy_reports(1)[0]['rows']}

    # then
    assert rows['checkout-service']['flags'] == ['err up'] and 'cluster' not in rows['checkout-service']
    assert rows['orders-service']['flags'] == ['restarts=1'] and rows['orders-service']['cluster'] == 'east'
    assert len(http.hits(f'{PROD}/api/v1/query')) > 0 and len(http.hits(f'{EAST}/api/v1/query')) > 0


def test_a_plugin_with_a_url_field_and_no_ui_field_adds_no_link_host(store: Store):
    # given
    plain = minimal('internal-api', 'Internal API', capabilities=Capabilities(), fields=(Field('url', 'URL', 'url', required=True),))
    registry = Registry.of(plain, prometheus.PLUGIN)
    runtime = Runtime(registry, PluginState(store.db), Instances(store.db, registry, lambda: Sealer(KIT_KEY)))
    add_instance(store, plain, 'main', sealer_key=KIT_KEY, primary=True, url='https://internal.example.org')
    add_instance(store, prometheus.PLUGIN, 'main', sealer_key=KIT_KEY, primary=True, url='https://prom.example.net')
    runtime.refresh()

    # then
    hosts = runtime.ui_hosts()
    assert hosts == ('prom.example.net',) and 'internal.example.org' not in allowed_link_hosts(settings(), hosts)


def test_a_tile_instance_reports_its_failing_pass(store: Store):
    # given
    now = NOW
    store.save_state({'anomalies': [], 'initialized': True, 'sources': {}, 'serverPasses': {'prom': {'at': iso(now), 'status': 'ok'}, 'prom-east': {'at': iso(now), 'status': 'error: timed out after 30 s'}}}, None)

    # when
    with app_with(store, router(), main={'url': PROD}, east={'url': EAST}) as c:
        tile = c.get('/api/server').json()['plugins'][0]

    # then
    assert [(i['name'], i['status']) for i in tile['instances']] == [('main', 'ok'), ('east', 'error: timed out after 30 s')]


def test_the_contract_hands_plugins_the_remote_transport():
    # then
    assert (contract.Transport, contract.HttpError, contract.Unreachable, contract.call, contract.send) == (remote.Transport, remote.HttpError, remote.Unreachable, remote.call, remote.send)


def test_only_the_field_keyed_ca_is_trusted_as_the_certificate_authority(store: Store, monkeypatch):
    # given
    cert = pem()
    two = minimal('two-pems', 'Two pems', capabilities=Capabilities(), fields=(Field('url', 'URL', 'url'), Field('client', 'Client certificate', 'pem'), Field('ca', 'CA certificate', 'pem')))
    seen = []
    monkeypatch.setattr('skopax.runtime.pem_transport', lambda given, pinned=False: seen.append(given) or remote.send)
    registry = Registry.of(two)
    runtime = Runtime(registry, PluginState(store.db), Instances(store.db, registry, lambda: Sealer(KIT_KEY)))
    add_instance(store, two, 'client-only', sealer_key=KIT_KEY, primary=True, url='https://two.example.com', client=cert)
    add_instance(store, two, 'both', sealer_key=KIT_KEY, url='https://two.example.com', client='not the ca', ca=cert)
    runtime.refresh()

    # when
    plain = runtime.context(two, runtime.instances.get('two-pems', 'client-only'), NOW).http()
    trusted = runtime.context(two, runtime.instances.get('two-pems', 'both'), NOW).http()

    # then
    assert plain is remote.send and trusted is remote.send and seen == [cert]


def test_a_transport_that_cannot_be_built_is_a_failed_test_not_a_crash():
    # given
    class Ctx:
        instance = prometheus.InstanceInfo('main', True, None, {'url': PROM_URL})

        def http(self):
            raise ssl.SSLError('bad certificate text')

    # when
    result = prometheus.test(Ctx())

    # then
    assert (result.ok, result.summary) == (False, messages.msg('connection.tls'))


@pytest.mark.parametrize('args', [{'expr': 'up', 'at': 'noon'}, {'expr': 'up', 'range_minutes': 'ten'}])
def test_promql_refuses_a_time_or_range_that_is_not_a_number_with_a_plain_reason(args):
    # given
    class Ctx:
        now = NOW
        instance = prometheus.InstanceInfo('main', True, None, {'url': PROM_URL})

        def http(self):
            raise AssertionError('no request should go out')

    # when
    with pytest.raises(prometheus.ReadError) as refused:
        prometheus.promql(Ctx(), args)

    # then
    assert (refused.value.status, refused.value.message) == (400, 'at must be unix seconds and range_minutes a whole number of minutes')


def test_a_row_link_must_be_https(store: Store):
    # given
    rt = prom_runtime(store, {'main': {'url': PROD, 'ui-url': 'http://prometheus.example.com'}}, FakeHttp())
    report = {'windowFrom': '2026-10-13T05:06:00Z', 'windowTo': '2026-10-13T05:35:00Z', 'rows': [{'ns': 'shop', 'svc': 'a-service', 'metrics': 'req 9 (1/2)'}]}

    # when
    linked = rt.row_links(report)['rows']

    # then
    assert 'links' not in linked[0]


def test_a_refused_prometheus_read_is_a_warn_card_and_its_cards_stay_open(store: Store):
    # given
    good = rich_routes(PROD)
    http = FakeHttp(dict(good))
    card = 'no-access|-/prom|metrics'

    # when
    with app_with(store, http, main={'url': PROD}) as c:
        c.app.state.sweeper.run('prometheus', 'prom', NOW)
        before, rows = {k for k, a in card_map(store).items() if a['status'] == 'open'}, store.load()['services']
        http.routes.update({key: HttpError(403, key[1]) for key in good})
        c.app.state.sweeper.run('prometheus', 'prom', NOW + 900)
        refused, state = {k for k, a in card_map(store).items() if a['status'] == 'open'}, store.load()
        http.routes.update(good)
        c.app.state.sweeper.run('prometheus', 'prom', NOW + 1800)
        back = {k: a['status'] for k, a in card_map(store).items()}
    denied = FakeHttp({(GET, f'{PROM_URL}/api/v1/status/buildinfo'): HttpError(403, PROM_URL), (GET, f'{PROM_URL}/api/v1/query'): HttpError(401, PROM_URL)})
    rt = prom_runtime(store, {'main': {'auth': 'basic', 'username': 'alice', 'password': 'pw-0001'}}, denied)
    tested = prometheus.test(rt.context(prometheus.PLUGIN, rt.instances.get('prometheus', 'main'), NOW))
    with pytest.raises(ToolError) as told:
        PluginTools(rt).call('promql', {'expr': 'up'}, NOW)

    # then
    assert 'err|shop/checkout-service' in before and card not in before
    assert state['serverPasses']['prom']['status'] == 'ok \u00b7 no access to metrics' and refused == before | {card}
    warn = card_map(store)[card]
    assert (warn['sev'], warn['ranKey'], warn['label']) == ('warn', 'prom:access:metrics', 'Prometheus cannot read metrics: the server requires credentials and none are set')
    assert state['services'] == rows
    assert back[card] == 'cleared'
    assert (tested.ok, tested.summary) == (False, 'Prometheus cannot read metrics: the password lacks the needed permission')
    assert str(told.value) == 'Prometheus cannot read metrics: the password is invalid or lacks the needed permission'


def test_the_connection_test_names_what_was_sent_when_prometheus_refuses_it(store: Store):
    # given
    path = f'{PROM_URL}/api/v1/status/buildinfo'

    def check(values, code):
        rt = prom_runtime(store, {'main': values}, FakeHttp({(GET, path): HttpError(code, path)}))
        return prometheus.test(rt.context(prometheus.PLUGIN, rt.instances.get('prometheus', 'main'), NOW)).summary

    # when
    bearer_401, bearer_403 = check({'auth': 'bearer', 'token': 't'}, 401), check({'auth': 'bearer', 'token': 't'}, 403)
    open_401, open_403 = check({}, 401), check({}, 403)

    # then
    assert bearer_401 == messages.msg('connection.rejected', status=401)
    assert bearer_403 == 'Prometheus cannot read metrics: the token lacks the needed permission'
    assert open_401 == 'The service requires credentials and none are set (HTTP 401)'
    assert open_403 == 'Prometheus cannot read metrics: the server requires credentials and none are set'


def test_every_prometheus_sign_in_field_explains_itself():
    # when
    helps = {f.key: f.help for f in prometheus.PLUGIN.fields if f.key in ('auth', 'token', 'username', 'password')}

    # then
    assert set(helps) == {'auth', 'token', 'username', 'password'} and all(helps.values())
