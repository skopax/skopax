import json
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient

from skopax.app import create_app
from skopax.engine import iso
from skopax.instances import Instances, PluginState
from skopax.investigator.tools import ToolError
from skopax.kit import promql
from skopax.messages import msg
from skopax.plugins import prometheus, redis
from skopax.plugins.redis import engine as redis_engine
from skopax.plugintools import PluginTools
from skopax.registry import BUILTIN, Registry, describe
from skopax.remote import HttpError, Unreachable
from skopax.runtime import Runtime
from skopax.sealer import Sealer, master_key
from tests.conftest import settings
from tests.fake_http import FakeHttp
from tests.plugin_kit import add_instance

NOW = 1_792_224_000.0
GET = 'GET'
KEY = 'redis-key'
MAIN = 'https://prom.example.com'
EAST = 'https://prom-east.example.com'
UI = 'https://prometheus.example.com'
USER = 'alice@example.com'
PODS = ('redis-0', 'redis-1', 'redis-2')
NO_DATA = 'ok \u00b7 no data'
DOT = ' \u00b7 '


def vec(rows: list[tuple[dict, float]]) -> dict:
    return {'status': 'success', 'data': {'resultType': 'vector', 'result': [{'metric': m, 'value': [NOW, str(v)]} for m, v in rows]}}


def exporter_answer(expr: str, ns: str) -> dict:
    at = lambda pod: {'namespace': ns, 'pod': pod, 'instance': '10.0.0.7:9121'}
    if expr.startswith('redis_up'):
        return vec([(at(p), 0 if p.endswith('1') else 1) for p in PODS])
    if expr.startswith('redis_instance_info'):
        return vec([(dict(at(p), role='master' if p.endswith('2') else 'slave', redis_version='7.2.4'), 1) for p in PODS])
    if expr.startswith('redis_connected_slaves'):
        return vec([(at('redis-2'), 1)])
    return vec([(at(p), 3) for p in PODS])


def lettuce_answer(expr: str, ns: str, jobs: tuple[str, str]) -> dict:
    slow, failing = jobs
    who = lambda job: {'namespace': ns, 'job': job, 'net_sock_peer_addr': 'redis.example.com'}
    if 'error!="none"' in expr:
        return vec([(who(failing), 20)])
    if 'increase(lettuce_seconds_count' in expr:
        return vec([(who(slow), 1000), (who(failing), 50)])
    if 'offset 1d' in expr:
        return vec([(who(slow), 2), (who(failing), 1)])
    if 'offset 7d' in expr:
        return vec([(who(slow), 2.5), (who(failing), 1)])
    return vec([(who(slow), 12), (who(failing), 9)])


def answer(ns: str = 'cache', app_ns: str = 'commerce', jobs: tuple[str, str] = ('cart-service', 'checkout-service')):
    def go(q, body):
        expr = q['query'][0]
        return lettuce_answer(expr, app_ns, jobs) if 'lettuce_seconds' in expr else exporter_answer(expr, ns)

    return go


def east_answer():
    return answer('cache-east', 'orders', ('orders-service', 'payments-api'))


def queries(http: FakeHttp) -> list[str]:
    return [parse_qs(urlsplit(c[1]).query)['query'][0] for c in http.calls if c[1].split('?')[0].endswith('/api/v1/query')]


def add_all(store, prom: dict[str, dict], readers: dict[str, dict], key: str = '') -> None:
    for index, (name, values) in enumerate(prom.items()):
        add_instance(store, prometheus.PLUGIN, name, sealer_key=key, primary=index == 0, **values)
    for index, (name, values) in enumerate(readers.items()):
        add_instance(store, redis.PLUGIN, name, sealer_key=key, primary=index == 0, **values)


def app_of(store, http, prom: dict[str, dict], readers: dict[str, dict]) -> TestClient:
    add_all(store, prom, readers)
    return TestClient(create_app(settings(), store, transport=http, registry=Registry.of(prometheus.PLUGIN, redis.PLUGIN)))


def runtime_of(store, http, prom: dict[str, dict], readers: dict[str, dict]) -> Runtime:
    registry = Registry.of(prometheus.PLUGIN, redis.PLUGIN)
    runtime = Runtime(registry, PluginState(store.db), Instances(store.db, registry, lambda: Sealer(KEY)), http)
    add_all(store, prom, readers, KEY)
    runtime.refresh()
    return runtime


def run_once(c: TestClient, name: str = 'main', now: float = NOW):
    state = c.app.state
    return redis.run(state.runtime.context(redis.PLUGIN, state.instances.get('redis', name), now))


def open_keys(store) -> set[str]:
    return {a['key'] for a in store.load()['anomalies'] if a['status'] == 'open'}


def passes(store) -> dict[str, str]:
    return {k: v['status'] for k, v in store.load()['serverPasses'].items()}


MAIN_ONLY = {'main': {'url': MAIN, 'ui-url': UI}}
BOTH = {'main': {'url': MAIN, 'ui-url': UI}, 'east': {'url': EAST}}
READER = {'main': {'prometheus': 'main'}}
READERS = {'main': {'prometheus': 'main'}, 'east': {'prometheus': 'east'}}


def test_redis_identity_and_manifest():
    # given
    registry = Registry.of(prometheus.PLUGIN, redis.PLUGIN)

    # when
    loaded = registry.get('redis')
    manifest = describe(redis.PLUGIN, icon=True)
    check, tool = redis.PLUGIN.checks[0], redis.PLUGIN.tools[0]

    # then
    assert (loaded.state, loaded.reason) == ('ok', None) and 'skopax.plugins.redis' in BUILTIN
    assert Registry.load({}, entry_points=False).get('redis').state == 'ok'
    assert json.loads(json.dumps(manifest, allow_nan=False)) == manifest
    assert (manifest['name'], manifest['version'], manifest['category'], manifest['author'], manifest['licence'], manifest['core'], manifest['edition']) == (
        'Redis', '0.1.0', 'infrastructure', 'Skopax', 'Apache-2.0', '>=0.1,<0.2', 'community')
    assert manifest['uses'] == ['prometheus'] and manifest['capabilities'] == {'hosts': [], 'secrets': [], 'agentReads': False}
    assert [(f['key'], f['kind'], f['required'], f['choices']) for f in manifest['fields']] == [('prometheus', 'instance', True, ['prometheus']), ('selector', 'text', False, [])]
    assert [(c['id'], c['period'], c['offset'], c['ran']) for c in manifest['checks']] == [('redis', 900, 780, ['redis'])]
    assert (check.sections, check.tool) == (('redis',), 'redis')
    assert (tool.name, tool.kind, tool.per_run, tool.scan, tool.instance_input) == ('redis', 'read', 6, True, 'instance')
    assert [(r['id'], r['defaults']) for r in manifest['rules']] == [('rules', redis.RULES)]
    assert manifest['panels'] == [{'tab': 'redis', 'script': 'panel.js', 'fragment': None, 'label': 'Redis', 'group': 'observe'}]
    assert manifest['knowledge'] == ['redis'] and len(redis.PLUGIN.knowledge[0].text.splitlines()) <= 40
    assert registry.icon('redis').startswith('<svg')


def test_redis_defaults_are_neutral():
    # when
    fields = {f.key: f.default for f in redis.PLUGIN.fields}
    rules = {r.id: dict(r.defaults) for r in redis.PLUGIN.rules}

    # then
    assert fields == {'prometheus': '', 'selector': ''}
    assert redis.PLUGIN.settings == ()
    assert rules == {'rules': {'app_times': 3, 'app_floor_ms': 5, 'app_min_calls': 300, 'app_errors': 10}}


def test_redis_connection_test_reports_ok_and_failures(store):
    # given
    path = f'{MAIN}/api/v1/query'
    good = FakeHttp({(GET, path): vec([({'redis_version': '7.2.4'}, 3)])})
    empty = FakeHttp({(GET, path): vec([])})
    denied = FakeHttp({(GET, path): HttpError(401, path)})
    down = FakeHttp({(GET, path): Unreachable('x')})

    def check(http, off: bool = False):
        runtime = runtime_of(store, http, MAIN_ONLY, READER)
        if off:
            runtime.instances.set_enabled('prometheus', 'main', False, USER)
        return redis.test(runtime.context(redis.PLUGIN, runtime.instances.get('redis', 'main'), NOW))

    # when
    ok, quiet, refused, unreachable, switched = check(good), check(empty), check(denied), check(down), check(FakeHttp(), off=True)

    # then
    assert (ok.ok, ok.summary) == (True, 'Redis 7.2.4 on 3 instances through Prometheus main') and ok.ms >= 0
    assert (quiet.ok, quiet.summary) == (True, 'Prometheus main answered, but it holds no redis_exporter series yet')
    assert (refused.ok, refused.summary) == (False, msg('connection.no_credentials', status=401))
    assert (unreachable.ok, unreachable.summary) == (False, msg('connection.unreachable'))
    assert (switched.ok, switched.summary) == (False, msg('peers.unavailable', plugin='Prometheus', instance='main'))
    assert parse_qs(urlsplit(good.calls[0][1]).query)['query'] == ['count by (redis_version) (redis_instance_info)']


def test_the_redis_check_cards_the_exporter_set_and_slow_services(store):
    # given
    http = FakeHttp({(GET, f'{MAIN}/api/v1/query'): answer()})

    # when
    with app_of(store, http, MAIN_ONLY, READER) as c:
        out = run_once(c)

    # then
    result = out.result
    assert result.ok and result.status == 'ok' and result.ran == ('redis:exporter', 'redis:app', 'redis:access:exporter', 'redis:access:app') and result.sections == {}
    cards = {x['key']: x for x in result.found}
    assert cards['redis-down|cache/redis-1|up']['sev'] == 'crit' and cards['redis-down|cache/redis-1|up']['ranKey'] == 'redis:exporter'
    assert cards['redis-repl|cache/redis|replicas']['sev'] == 'warn' and 'redis-master|cache/redis|master' not in cards
    assert cards['redis-app|commerce/cart-service|latency']['now'] == 12 and 'redis-app|commerce/checkout-service|latency' not in cards
    assert cards['redis-app-err|commerce/checkout-service|errors']['now'] == 20 and cards['redis-app-err|commerce/checkout-service|errors']['ranKey'] == 'redis:app'
    assert 'redis-evict|cache/redis-0|evicted' in cards and 'redis-reject|cache/redis-2|rejected' in cards
    assert not [x for x in result.found if 'cluster' in x]
    assert cards['redis-app|commerce/cart-service|latency']['urls'][0]['url'].startswith(f'{UI}/query?')
    rows = out.sections['redis']
    exporter, apps = [r for r in rows if r['part'] == 'exporter'], [r for r in rows if r['part'] == 'app']
    assert [r['role'] for r in exporter] == ['slave', 'slave', 'master'] and exporter[2]['version'] == '7.2.4'
    assert apps[0]['job'] == 'cart-service' and apps[0]['redis'] == 'redis.example.com' and apps[0]['source'] == 'main'
    assert apps[0]['link'].startswith(f'{UI}/query?')
    assert rows[-1] == {'part': 'summary', 'at': iso(NOW), 'source': 'main'}
    assert not [r for r in rows if 'cluster' in r or 'plugin' in r]


def test_a_failed_part_keeps_the_rest(store):
    # given
    def broken(q, body):
        if q['query'][0].startswith('redis_up'):
            return HttpError(502, MAIN)
        return answer()(q, body)

    http = FakeHttp({(GET, f'{MAIN}/api/v1/query'): broken})

    # when
    with app_of(store, http, MAIN_ONLY, READER) as c:
        out = run_once(c)

    # then
    assert out.result.ok and out.result.ran == ('redis:app', 'redis:access:app') and out.result.status == 'error: instances: Prometheus query HTTP 502'
    assert not [r for r in out.sections['redis'] if r['part'] == 'exporter'] and not [x for x in out.result.found if x.get('redis') == 'exporter']


def test_an_unreadable_answer_fails_only_its_part_and_names_only_the_error_kind(store):
    # given
    def odd(q, body):
        if q['query'][0].startswith('redis_up'):
            return {'status': 'success', 'data': {'result': [{'metric': {'pod': 'redis-0'}, 'value': [NOW, 'token=s3cret']}]}}
        return answer()(q, body)

    http = FakeHttp({(GET, f'{MAIN}/api/v1/query'): odd})

    # when
    with app_of(store, http, MAIN_ONLY, READER) as c:
        out = run_once(c)

    # then
    assert out.result.ok and out.result.ran == ('redis:app', 'redis:access:app') and out.result.status == 'error: instances: ValueError'
    assert [r['part'] for r in out.sections['redis']][-1] == 'summary' and not [r for r in out.sections['redis'] if r['part'] == 'exporter']


def test_the_selector_reaches_every_query_as_valid_promql(store):
    # given
    plain, chosen = FakeHttp({(GET, f'{MAIN}/api/v1/query'): vec([])}), FakeHttp({(GET, f'{MAIN}/api/v1/query'): vec([])})

    # when
    with app_of(store, plain, MAIN_ONLY, READER) as c:
        run_once(c)
    with app_of(store, chosen, MAIN_ONLY, {'main': {'prometheus': 'main', 'selector': '{env="prod"}'}}) as c:
        run_once(c)
    bare, scoped = queries(plain), queries(chosen)

    # then
    assert 'redis_up' in bare and 'rate(redis_commands_processed_total[30m])' in bare
    assert 'sum by (namespace, job, net_sock_peer_addr) (increase(lettuce_seconds_count{error!="none"}[30m]))' in bare
    assert ('1000 * sum by (namespace, job, net_sock_peer_addr) (rate(lettuce_seconds_sum[30m] offset 1d)) / '
            'sum by (namespace, job, net_sock_peer_addr) (rate(lettuce_seconds_count[30m] offset 1d))') in bare
    assert 'redis_up{env="prod"}' in scoped and 'increase(redis_evicted_keys_total{env="prod"}[30m])' in scoped
    assert 'sum by (namespace, job, net_sock_peer_addr) (increase(lettuce_seconds_count{env="prod",error!="none"}[30m]))' in scoped
    assert len(bare) == len(scoped) == 13 and not [q for q in bare + scoped if '{}' in q or '{,' in q or ',}' in q]


def test_redis_without_series_is_ok_and_quiet(store):
    # given
    path = f'{MAIN}/api/v1/query'
    http = FakeHttp({(GET, path): answer()})

    # when
    with app_of(store, http, MAIN_ONLY, READER) as c:
        c.app.state.sweeper.run('redis', 'redis', NOW)
        before = open_keys(store)
        http.routes[(GET, path)] = vec([])
        out = run_once(c, now=NOW + 900)
        c.app.state.sweeper.run('redis', 'redis', NOW + 900)
        after = open_keys(store)
        state = store.load()

    # then
    assert {'redis-down|cache/redis-1|up', 'redis-app|commerce/cart-service|latency'} <= before
    assert (out.result.ok, out.result.status, out.result.found, out.result.ran) == (True, NO_DATA, [], ('redis:access:exporter', 'redis:access:app', 'redis:no-data'))
    assert after == before and passes(store)['redis'] == NO_DATA
    assert state['redis'] == [{'part': 'summary', 'at': iso(NOW + 900), 'source': 'main', 'plugin': 'redis'}]


def test_a_failed_part_with_the_rest_empty_closes_nothing(store):
    # given
    path = f'{MAIN}/api/v1/query'
    http = FakeHttp({(GET, path): answer()})

    def failing(q, body):
        return HttpError(502, MAIN) if q['query'][0].startswith('redis_up') else vec([])

    # when
    with app_of(store, http, MAIN_ONLY, READER) as c:
        c.app.state.sweeper.run('redis', 'redis', NOW)
        before = open_keys(store)
        http.routes[(GET, path)] = failing
        out = run_once(c, now=NOW + 900)
        c.app.state.sweeper.run('redis', 'redis', NOW + 900)
        after = open_keys(store)

    # then
    assert out.result.ok and out.result.found == [] and out.result.ran == ('redis:access:app', 'redis:no-data')
    assert out.result.status == 'error: instances: Prometheus query HTTP 502' + DOT + 'no data for services'
    assert after == before and passes(store)['redis'] == out.result.status


def test_a_partly_empty_pass_names_the_empty_part_and_keeps_its_cards(store):
    # given
    path = f'{MAIN}/api/v1/query'
    full = answer()
    http = FakeHttp({(GET, path): full})

    def no_exporter(q, body):
        return full(q, body) if 'lettuce_seconds' in q['query'][0] else vec([])

    # when
    with app_of(store, http, MAIN_ONLY, READER) as c:
        c.app.state.sweeper.run('redis', 'redis', NOW)
        before = open_keys(store)
        http.routes[(GET, path)] = no_exporter
        out = run_once(c, now=NOW + 900)
        c.app.state.sweeper.run('redis', 'redis', NOW + 900)
        after = open_keys(store)

    # then
    assert 'redis-down|cache/redis-1|up' in before and 'redis-app|commerce/cart-service|latency' in before
    assert out.result.ran == ('redis:app', 'redis:access:exporter', 'redis:access:app') and out.result.status == NO_DATA + ' for instances'
    assert after == before


def test_replica_cards_count_every_master_of_a_namespace():
    # given
    cluster = [{'ns': 'cache', 'name': f'rc-{i}', 'up': 1, 'role': 'master' if i < 3 else 'slave', **({'replicas': 1} if i < 3 else {})} for i in range(6)]
    standalone = [{'ns': 'cache', 'name': name, 'up': 1, 'role': 'master', 'replicas': 0} for name in ('sessions-0', 'carts-0')]
    behind = [dict(r, replicas=0) if r['name'] == 'rc-1' else r for r in cluster]

    # when
    quiet = [x['key'] for rows in (cluster, standalone) for x in redis_engine.exporter_cards(rows)]
    lost = {x['sig']: x for x in redis_engine.exporter_cards(behind)}

    # then
    assert quiet == [], 'a healthy Redis Cluster of 3 masters and 3 replicas, and two standalone instances, give no replica card'
    assert (lost['redis-repl']['key'], lost['redis-repl']['now']) == ('redis-repl|cache/rc|replicas', 2)
    assert lost['redis-repl']['what'] == 'The 3 Redis masters in namespace cache have 2 connected replicas in total for 3 replica instances; a failover now could lose writes.'


def test_exporter_rows_are_capped_with_down_and_roleless_ones_kept(store):
    # given
    pods = [f'redis-{i:03d}' for i in range(130)]

    def crowd(q, body):
        expr = q['query'][0]
        at = lambda pod: {'namespace': 'cache', 'pod': pod}
        if expr.startswith('redis_up'):
            return vec([(at(p), 0 if p == 'redis-129' else 1) for p in pods])
        if expr.startswith('redis_instance_info'):
            return vec([(dict(at(p), role='master'), 1) for p in pods if p != 'redis-128'])
        return vec([])

    http = FakeHttp({(GET, f'{MAIN}/api/v1/query'): crowd})

    # when
    with app_of(store, http, MAIN_ONLY, READER) as c:
        out = run_once(c)
    tool = PluginTools(runtime_of(store, http, MAIN_ONLY, READER)).call('redis', {}, NOW)

    # then
    exporter = [r for r in out.sections['redis'] if r['part'] == 'exporter']
    assert len(exporter) == redis_engine.EXPORTER_ROWS == 100 and [r['name'] for r in exporter][-2:] == ['redis-128', 'redis-129']
    assert [r['name'] for r in exporter] == sorted(r['name'] for r in exporter)
    assert 'redis-down|cache/redis-129|up' in {x['key'] for x in out.result.found}
    assert len(tool['instances']) == 100 and tool['instances'][-1]['name'] == 'redis-129' and tool['note'].startswith('first 100 of 130 instances')


def test_a_slow_prometheus_keeps_the_parts_that_answered_in_time(store, monkeypatch):
    # given
    http = FakeHttp({(GET, f'{MAIN}/api/v1/query'): answer()})
    ticks = iter([0.0] * 9 + [1000.0] * 100)

    # when
    with app_of(store, http, MAIN_ONLY, READER) as c:
        monkeypatch.setattr(promql, 'clock', lambda: next(ticks))
        out = run_once(c)

    # then
    assert out.result.ok and out.result.ran == ('redis:exporter', 'redis:access:exporter') and out.result.status == 'error: services: timed out'
    assert [r for r in out.sections['redis'] if r['part'] == 'exporter'] and not [q for q in queries(http) if 'lettuce_seconds' in q]


def test_redis_follows_its_prometheus_instance(store):
    # given
    http = FakeHttp({(GET, f'{MAIN}/api/v1/query'): answer(), (GET, f'{EAST}/api/v1/query'): east_answer()})

    # when
    with app_of(store, http, BOTH, READERS) as c:
        c.app.state.sweeper.run('redis', 'redis', NOW)
        before = open_keys(store)
        c.app.state.instances.set_enabled('prometheus', 'east', False, USER)
        c.app.state.sweeper.run('redis', 'redis', NOW + 900)
        after = open_keys(store)
        found = passes(store)

    # then
    assert 'redis-app|orders/orders-service|latency@east' in before and 'redis-app|commerce/cart-service|latency' in before
    assert found['redis'] == 'ok'
    assert found['redis-east'] == 'error: ' + msg('peers.unavailable', plugin='Prometheus', instance='east')
    assert after == before


def test_redis_second_instance_is_scoped(store):
    # given
    http = FakeHttp({(GET, f'{MAIN}/api/v1/query'): answer(), (GET, f'{EAST}/api/v1/query'): east_answer()})

    # when
    with app_of(store, http, BOTH, READERS) as c:
        c.app.state.sweeper.run('redis', 'redis', NOW)
        state = store.load()
    cards = {a['key']: a for a in state['anomalies']}

    # then
    east = cards['redis-app|orders/orders-service|latency@east']
    assert (east['cluster'], east['ranKey']) == ('east', 'redis:app-east')
    assert (cards['redis-down|cache-east/redis-1|up@east']['cluster'], cards['redis-down|cache-east/redis-1|up@east']['ranKey']) == ('east', 'redis:exporter-east')
    main = cards['redis-app|commerce/cart-service|latency']
    assert 'cluster' not in main and main['ranKey'] == 'redis:app'
    assert passes(store) == {'redis': 'ok', 'redis-east': 'ok'}
    assert east['links'][0]['url'].startswith(f'{EAST}/query?') and main['links'][0]['url'].startswith(f'{UI}/query?')
    assert {r['source'] for r in state['redis'] if r.get('cluster') == 'east' and r['part'] in ('app', 'summary')} == {'east'}
    assert {r['source'] for r in state['redis'] if 'cluster' not in r and r['part'] in ('app', 'summary')} == {'main'}


def test_redis_rules_come_from_the_rule_set(store):
    # given
    http = FakeHttp({(GET, f'{MAIN}/api/v1/query'): answer()})

    # when
    with app_of(store, http, MAIN_ONLY, READER) as c:
        default = {x['key'] for x in run_once(c).result.found}
        c.app.state.runtime.live.set('redis.rules', '{"app_floor_ms": 20}', USER)
        raised = {x['key'] for x in run_once(c).result.found}

    # then
    assert 'redis-app|commerce/cart-service|latency' in default and 'redis-app|commerce/cart-service|latency' not in raised
    assert default - raised == {'redis-app|commerce/cart-service|latency'}


def test_the_redis_tool_reads_instances_and_apps(store):
    # given
    http = FakeHttp({(GET, f'{MAIN}/api/v1/query'): answer()})
    tools = PluginTools(runtime_of(store, http, MAIN_ONLY, READER))

    # when
    instances = tools.call('redis', {}, NOW)
    apps = tools.call('redis', {'op': 'apps', 'job': 'cart-service'}, NOW)
    with pytest.raises(ToolError) as history:
        tools.call('redis', {'op': 'history'}, NOW)

    # then
    assert tools.scan('redis') and tools.cap('redis') == 6
    assert [r['name'] for r in instances['instances']] == list(PODS) and instances['instances'][1]['up'] == 0
    assert [a['job'] for a in apps['apps']] == ['cart-service'] and apps['apps'][0]['ms'] == 12
    assert str(history.value) == 'op must be one of instances, apps'


def test_the_redis_tool_is_not_offered_without_an_instance(store):
    # given
    tools = PluginTools(runtime_of(store, FakeHttp(), MAIN_ONLY, {}))

    # when
    offered = [d['name'] for d in tools.definitions()]

    # then
    assert 'redis' not in offered and tools.get('redis') is None and 'promql' in offered


def test_redis_links_are_the_card_urls_over_https_only():
    # given
    info = None
    own = {'src': 'redis', 'urls': [{'label': 'Prometheus graph', 'url': f'{UI}/query?g0.expr=up'}, {'label': 'Plain', 'url': 'http://prom.example.com/query'},
                                    {'label': 'Script', 'url': 'javascript:alert(1)'}, {'label': 3, 'url': f'{UI}/x'}]}

    # when
    mine = redis.links(own, info)
    other = redis.links({'src': 'prom', 'urls': own['urls']}, info)
    bare = redis.links({'src': 'redis'}, info)

    # then
    assert mine == [{'label': 'Prometheus graph', 'url': f'{UI}/query?g0.expr=up'}]
    assert other is None and bare == []


def test_a_refused_redis_part_is_a_warn_card_and_its_own_cards_stay_open(store):
    # given
    path = f'{MAIN}/api/v1/query'
    full = answer()
    http = FakeHttp({(GET, path): full})
    card = 'no-access|-/redis|app'
    slow = 'redis-app|commerce/cart-service|latency'

    def refusing(q, body):
        return HttpError(401, MAIN) if 'lettuce_seconds' in q['query'][0] else full(q, body)

    # when
    basic = {'main': {'url': MAIN, 'ui-url': UI, 'auth': 'basic', 'username': 'alice', 'password': 'pw-0001'}}
    add_all(store, basic, {}, master_key(store.db, settings()))
    with app_of(store, http, {}, READER) as c:
        c.app.state.sweeper.run('redis', 'redis', NOW)
        before = open_keys(store)
        http.routes[(GET, path)] = refusing
        out = run_once(c, now=NOW + 900)
        c.app.state.sweeper.run('redis', 'redis', NOW + 900)
        refused = open_keys(store)
        http.routes[(GET, path)] = full
        c.app.state.sweeper.run('redis', 'redis', NOW + 1800)
        back = {a['key']: a['status'] for a in store.load()['anomalies']}
    runtime = runtime_of(store, FakeHttp({(GET, path): HttpError(403, path)}), basic, READER)
    tested = redis.test(runtime.context(redis.PLUGIN, runtime.instances.get('redis', 'main'), NOW))
    with pytest.raises(ToolError) as told:
        PluginTools(runtime).call('redis', {'op': 'apps'}, NOW)

    # then
    assert slow in before and card not in before
    assert out.result.ok and out.result.status == 'ok \u00b7 no access to services'
    [warn] = [x for x in out.result.found if x['key'] == card]
    assert (warn['sev'], warn['ranKey'], warn['label']) == ('warn', 'redis:access:app', 'Redis cannot read services: the password is invalid or lacks the needed permission')
    assert set(out.result.ran) == {'redis:exporter', 'redis:access:exporter', 'redis:access:app'}
    assert refused == before | {card}
    assert back[card] == 'cleared' and back[slow] == 'open'
    assert str(told.value) == 'Redis cannot read services: the password lacks the needed permission'
    assert (tested.ok, tested.summary) == (False, 'Redis cannot read instances: the password lacks the needed permission')
