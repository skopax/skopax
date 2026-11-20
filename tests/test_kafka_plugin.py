import json
import re
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient

from skopax.app import create_app
from skopax.engine import iso
from skopax.instances import Instances, PluginState
from skopax.investigator.tools import ToolError
from skopax.kit import promql
from skopax.messages import msg
from skopax.plugins import kafka, prometheus
from skopax.plugins.kafka import engine as kafka_engine
from skopax.plugintools import PluginTools
from skopax.registry import BUILTIN, Registry, describe
from skopax.remote import HttpError, Unreachable
from skopax.runtime import Runtime
from skopax.sealer import Sealer
from tests.conftest import settings
from tests.fake_http import FakeHttp
from tests.plugin_kit import add_instance

NOW = 1_792_224_000.0
GET = 'GET'
KEY = 'kafka-key'
MAIN = 'https://prom.example.com'
EAST = 'https://prom-east.example.com'
UI = 'https://prometheus.example.com'
USER = 'alice@example.com'
SUFFIX = ''
NO_DATA = 'ok \u00b7 no data'
DOT = ' \u00b7 '
CLUSTERS = {'namespace': {'streaming': 'streaming', 'pipeline': 'pipeline'}, 'job': {'streaming': 'streaming-exporter', 'pipeline': 'pipeline-exporter'}}


def vec(rows: list[tuple[dict, float]]) -> dict:
    return {'status': 'success', 'data': {'resultType': 'vector', 'result': [{'metric': m, 'value': [NOW, str(v)]} for m, v in rows]}}


def app_answer(e: str, ns: str, job: str) -> dict:
    who = {'namespace': ns, 'job': job}
    listener = 'spring_kafka_listener' in e
    if 'result="failure"' in e:
        if 'offset' in e:
            return vec([(who, 1)])
        return vec([(who, 40 if listener else 0)])
    if e.startswith('1000 *'):
        return vec([(who, 12.5)])
    if 'increase' in e:
        return vec([(who, 500)])
    return vec([])


def watched(e: str, ns: str) -> bool:
    keep, drop = re.search(r'namespace=~("[^"]*")', e), re.search(r'namespace!~("[^"]*")', e)
    return (keep is None or re.fullmatch(json.loads(keep.group(1)), ns) is not None) and (drop is None or re.fullmatch(json.loads(drop.group(1)), ns) is None)


def answer(prefix: str = '', namespace: str | None = None):
    def go(q, body):
        e = q['query'][0]
        if 'spring_kafka' in e:
            return app_answer(e, prefix + 'commerce', prefix + 'inventory-service')
        label = 'job' if 'by (job' in e else 'namespace'
        c = lambda name, **more: {label: prefix + CLUSTERS[label][name], **({'namespace': namespace} if namespace and label == 'job' else {}), **more}
        g = lambda name, group: c(name, consumergroup=prefix + group)
        if 'kafka_broker_info' in e:
            return vec([(c('streaming'), 3), (c('pipeline'), 3)])
        if 'kafka_brokers' in e:
            return vec([(c('streaming'), 2), (c('pipeline'), 3)])
        if 'kube_pod_container_status_restarts_total' in e:
            return vec([(c('pipeline', pod='pipeline-kafka-0'), 2)] if watched(e, 'pipeline') else [])
        if 'kubelet_volume_stats' in e:
            return vec([(c('streaming', persistentvolumeclaim='data-streaming-kafka-0'), 84.2), (c('streaming', persistentvolumeclaim='data-streaming-kafka-1'), 91.5),
                        (c('pipeline', persistentvolumeclaim='data-pipeline-kafka-0'), 22.0)])
        if 'kafka_topic_partition_leader' in e:
            return vec([(c('streaming'), 4)])
        if f'min_over_time(sum by ({label}) (kafka_topic_partition_under_replicated' in e:
            return vec([(c('streaming'), 2), (c('pipeline'), 0)])
        if 'under_replicated' in e:
            return vec([(c('streaming'), 3), (c('pipeline'), 0)])
        if e.startswith(f'count by ({label}) (kafka_topic_partitions'):
            return vec([(c('streaming'), 174), (c('pipeline'), 14)])
        if e.startswith(f'sum by ({label}) (kafka_topic_partitions'):
            return vec([(c('streaming'), 625), (c('pipeline'), 89)])
        if 'kafka_topic_partition_current_offset' in e and 'topk' not in e:
            return vec([(c('streaming'), 85.4), (c('pipeline'), 219.0)])
        if e.startswith(f'count by ({label}) (kafka_consumergroup_members'):
            return vec([(c('streaming'), 2), (c('pipeline'), 1)])
        if 'kafka_consumergroup_members' in e and '[1d:10m]' in e:
            return vec([(g('streaming', 'profile-service'), 4), (g('streaming', 'cart-service'), 6), (g('streaming', 'old-group'), 0)])
        if 'kafka_consumergroup_members' in e and '[15m:1m]' in e:
            return vec([(g('streaming', 'profile-service'), 0), (g('streaming', 'cart-service'), 6), (g('streaming', 'old-group'), 0)])
        if 'kafka_consumergroup_members' in e:
            return vec([(g('streaming', 'profile-service'), 0), (g('streaming', 'cart-service'), 6), (g('streaming', 'old-group'), 0), (g('pipeline', 'log-shipper'), 36)])
        if 'kafka_consumergroup_lag_sum' in e and 'min_over_time' in e:
            return vec([(g('streaming', 'profile-service'), 900), (g('streaming', 'cart-service'), 101), (g('streaming', 'old-group'), 5000)])
        if 'kafka_consumergroup_lag_sum' in e:
            return vec([(g('streaming', 'profile-service'), 950), (g('streaming', 'cart-service'), 101), (g('streaming', 'old-group'), 5000), (g('pipeline', 'log-shipper'), 400)])
        return vec([])

    return go


def queries(http: FakeHttp) -> list[str]:
    return [parse_qs(urlsplit(c[1]).query)['query'][0] for c in http.calls if c[1].split('?')[0].endswith('/api/v1/query')]


def add_all(store, prom: dict[str, dict], readers: dict[str, dict], key: str = '') -> None:
    for index, (name, values) in enumerate(prom.items()):
        add_instance(store, prometheus.PLUGIN, name, sealer_key=key, primary=index == 0, **values)
    for index, (name, values) in enumerate(readers.items()):
        add_instance(store, kafka.PLUGIN, name, sealer_key=key, primary=index == 0, **values)


def app_of(store, http, prom: dict[str, dict], readers: dict[str, dict]) -> TestClient:
    add_all(store, prom, readers)
    return TestClient(create_app(settings(), store, transport=http, registry=Registry.of(prometheus.PLUGIN, kafka.PLUGIN)))


def runtime_of(store, http, prom: dict[str, dict], readers: dict[str, dict]) -> Runtime:
    registry = Registry.of(prometheus.PLUGIN, kafka.PLUGIN)
    runtime = Runtime(registry, PluginState(store.db), Instances(store.db, registry, lambda: Sealer(KEY)), http)
    add_all(store, prom, readers, KEY)
    runtime.refresh()
    return runtime


def run_once(c: TestClient, name: str = 'main', now: float = NOW):
    state = c.app.state
    return kafka.run(state.runtime.context(kafka.PLUGIN, state.instances.get('kafka', name), now))


def open_keys(store) -> set[str]:
    return {a['key'] for a in store.load()['anomalies'] if a['status'] == 'open'}


def passes(store) -> dict[str, str]:
    return {k: v['status'] for k, v in store.load()['serverPasses'].items()}


def part(rows: list[dict], name: str) -> list[dict]:
    return [r for r in rows if r['part'] == name]


MAIN_ONLY = {'main': {'url': MAIN, 'ui-url': UI}}
OTHERS = {'main': {'url': MAIN, 'ui-url': UI, 'namespaces': 'commerce'}}
BOTH = {'main': {'url': MAIN, 'ui-url': UI}, 'east': {'url': EAST}}
STRIMZI = {'main': {'prometheus': 'main', 'preset': 'strimzi'}}
READER = {'main': {'prometheus': 'main'}}


def test_kafka_identity_and_manifest():
    # given
    registry = Registry.of(prometheus.PLUGIN, kafka.PLUGIN)

    # when
    loaded = registry.get('kafka')
    manifest = describe(kafka.PLUGIN, icon=True)
    check, tool = kafka.PLUGIN.checks[0], kafka.PLUGIN.tools[0]

    # then
    assert (loaded.state, loaded.reason) == ('ok', None) and 'skopax.plugins.kafka' in BUILTIN
    assert Registry.load({}, entry_points=False).get('kafka').state == 'ok'
    assert json.loads(json.dumps(manifest, allow_nan=False)) == manifest
    assert (manifest['name'], manifest['version'], manifest['category'], manifest['author'], manifest['licence'], manifest['core'], manifest['edition']) == (
        'Kafka', '0.1.0', 'infrastructure', 'Skopax', 'Apache-2.0', '>=0.1,<0.5', 'community')
    assert manifest['uses'] == ['prometheus'] and manifest['capabilities'] == {'hosts': [], 'secrets': [], 'agentReads': False}
    assert [(f['key'], f['kind'], f['required'], f['choices']) for f in manifest['fields']] == [
        ('prometheus', 'instance', True, ['prometheus']), ('preset', 'choice', False, ['strimzi', 'kafka-exporter']), ('namespaces', 'regex', False, [])]
    assert [(c['id'], c['period'], c['offset'], c['ran']) for c in manifest['checks']] == [('kafka', 900, 840, ['kafka'])]
    assert (check.sections, check.tool) == (('kafka',), 'kafka')
    assert (tool.name, tool.kind, tool.per_run, tool.scan, tool.instance_input) == ('kafka', 'read', 6, True, 'instance')
    assert tool.input_schema['properties']['op']['enum'] == ['clusters', 'groups', 'group', 'topics', 'apps', 'history'] and 'cluster' in tool.input_schema['properties']
    assert [(r['id'], r['defaults']) for r in manifest['rules']] == [('rules', kafka.RULES)]
    assert manifest['panels'] == [{'tab': 'kafka', 'script': 'panel.js', 'fragment': None, 'label': 'Kafka', 'group': 'observe'}]
    assert manifest['knowledge'] == ['kafka'] and len(kafka.PLUGIN.knowledge[0].text.splitlines()) <= 40
    assert registry.icon('kafka').startswith('<svg')


def test_kafka_defaults_are_neutral():
    # when
    fields = {f.key: f.default for f in kafka.PLUGIN.fields}
    rules = {r.id: dict(r.defaults) for r in kafka.PLUGIN.rules}

    # then
    assert fields == {'prometheus': '', 'preset': 'kafka-exporter', 'namespaces': '.*'}
    assert kafka.PLUGIN.settings == ()
    assert rules == {'rules': {'disk_warn': 80, 'disk_crit': 90, 'gone_lag': 100, 'app_failures': 10, 'app_times': 3}}


def test_kafka_connection_test_reports_ok_and_failures(store):
    # given
    path = f'{MAIN}/api/v1/query'
    good = FakeHttp({(GET, path): vec([({'job': 'streaming-exporter'}, 3), ({'job': 'pipeline-exporter'}, 2)])})
    empty = FakeHttp({(GET, path): vec([])})
    denied = FakeHttp({(GET, path): HttpError(401, path)})
    down = FakeHttp({(GET, path): Unreachable('x')})

    def check(http, readers=None, off: bool = False):
        runtime = runtime_of(store, http, MAIN_ONLY, readers or READER)
        if off:
            runtime.instances.set_enabled('prometheus', 'main', False, USER)
        return kafka.test(runtime.context(kafka.PLUGIN, runtime.instances.get('kafka', 'main'), NOW))

    # when
    ok, quiet, refused, unreachable, switched = check(good), check(empty), check(denied), check(down), check(FakeHttp(), off=True)
    strimzi = check(FakeHttp({(GET, path): vec([({'namespace': 'streaming'}, 3)])}), STRIMZI)

    # then
    assert (ok.ok, ok.summary) == (True, 'Kafka with 5 brokers in 2 clusters through Prometheus main') and ok.ms >= 0
    assert (quiet.ok, quiet.summary) == (True, 'Prometheus main answered, but it holds no kafka_exporter series yet')
    assert (refused.ok, refused.summary) == (False, msg('connection.no_credentials', status=401))
    assert (unreachable.ok, unreachable.summary) == (False, msg('connection.unreachable'))
    assert (switched.ok, switched.summary) == (False, msg('peers.unavailable', plugin='Prometheus', instance='main'))
    assert (strimzi.ok, strimzi.summary) == (True, 'Kafka with 3 brokers in 1 cluster through Prometheus main')
    assert parse_qs(urlsplit(good.calls[0][1]).query)['query'] == ['max by (job) (kafka_brokers{namespace=~".*"})']


def test_the_kafka_check_cards_brokers_partitions_disk_gone_consumers_and_failing_listeners(store):
    # given
    http = FakeHttp({(GET, f'{MAIN}/api/v1/query'): answer()})

    # when
    with app_of(store, http, OTHERS, STRIMZI) as c:
        out = run_once(c)
    sent = queries(http)

    # then
    result = out.result
    assert result.ok and result.status == 'ok' and result.ran == ('kafka:clusters', 'kafka:groups', 'kafka:app', 'kafka:access:clusters', 'kafka:access:groups', 'kafka:access:app') and result.sections == {}
    cards = {x['key']: x for x in result.found}
    assert cards['kafka-broker|streaming/streaming|brokers']['sev'] == 'crit' and cards['kafka-broker|streaming/streaming|brokers']['now'] == 2
    assert cards['kafka-broker|streaming/streaming|brokers']['ranKey'] == 'kafka:clusters'
    assert cards['kafka-offline|streaming/streaming|offline']['sev'] == 'crit'
    assert cards['kafka-urp|streaming/streaming|urp']['now'] == 3
    assert cards['kafka-disk|streaming/data-streaming-kafka-1|disk']['sev'] == 'crit' and cards['kafka-disk|streaming/data-streaming-kafka-1|disk']['now'] == 91.5
    assert cards['kafka-gone|streaming/kafka/profile-service|members']['now'] == 950 and cards['kafka-gone|streaming/kafka/profile-service|members']['ranKey'] == 'kafka:groups'
    assert not [k for k in cards if 'old-group' in k or 'cart-service' in k or 'log-shipper' in k], 'a group that had no members all day is retired, not gone'
    assert cards['kafka-restart|pipeline/pipeline-kafka-0|restarts']['now'] == 2, 'namespaces the Prometheus pass does not watch for restarts get their restarts from the Kafka pass'
    err = cards['kafka-consume-err|commerce/inventory-service|in']
    assert err['now'] == 40 and err['ranKey'] == 'kafka:app' and 'cluster' not in err
    assert not [k for k in cards if k.startswith('kafka-send-err')]
    assert {x['ranKey'] for x in result.found} == {'kafka:clusters', 'kafka:groups', 'kafka:app'} and not [x for x in result.found if 'cluster' in x]
    rows = out.sections['kafka']
    clusters, groups, apps = part(rows, 'clusters'), part(rows, 'groups'), part(rows, 'app')
    assert [c['ns'] for c in clusters] == ['pipeline', 'streaming'] and clusters[1]['diskPvc'] == 'data-streaming-kafka-1'
    assert clusters[1]['expected'] == 3 and clusters[1]['msgs'] == 85.4 and clusters[0]['restarts'] == {'pipeline-kafka-0': 2}
    assert groups[0]['group'] == 'old-group' and {g['group'] for g in groups} == {'old-group', 'profile-service', 'cart-service', 'log-shipper'}
    assert [(a['source'], a['job']) for a in apps] == [('main', 'inventory-service')]
    assert rows[-1] == {'part': 'summary', 'at': iso(NOW), 'source': 'main', 'sourcePrimary': True, 'preset': 'strimzi'}
    assert not [r for r in rows if 'cluster' in r or 'plugin' in r]
    assert cards['kafka-gone|streaming/kafka/profile-service|members']['urls'][0]['url'].startswith(f'{UI}/query?')
    assert groups[0]['link'].startswith(f'{UI}/query?') and apps[0]['inLink'].startswith(f'{UI}/query?')
    restarts = [q for q in sent if 'kube_pod_container_status_restarts_total' in q]
    assert restarts == ['sum by (namespace, pod) (increase(kube_pod_container_status_restarts_total{namespace=~".*",namespace!~"commerce"}[30m])) > 0.5']


def test_an_exporter_that_stops_answering_is_its_own_card_and_a_failed_part_keeps_the_rest(store):
    # given
    def silent(q, body):
        e = q['query'][0]
        if 'kafka_brokers' in e and 'info' not in e:
            return vec([({'namespace': 'pipeline'}, 3)])
        if 'by (namespace, consumergroup)' in e:
            return HttpError(502, MAIN)
        return answer()(q, body)

    http = FakeHttp({(GET, f'{MAIN}/api/v1/query'): silent})

    # when
    with app_of(store, http, MAIN_ONLY, STRIMZI) as c:
        out = run_once(c)

    # then
    assert out.result.ok and out.result.ran == ('kafka:clusters', 'kafka:app', 'kafka:access:clusters', 'kafka:access:app') and out.result.status.startswith('error: groups: ')
    assert 'kafka-exporter|streaming/streaming|exporter' in {x['key'] for x in out.result.found}
    assert part(out.sections['kafka'], 'groups') == []


def test_an_unreadable_answer_fails_only_its_part_and_names_only_the_error_kind(store):
    # given
    def odd(q, body):
        if 'by (job, namespace, consumergroup)' in q['query'][0]:
            return {'status': 'success', 'data': {'result': [{'metric': {'job': 'streaming-exporter', 'consumergroup': 'x'}, 'value': [NOW, 'token=s3cret']}]}}
        return answer()(q, body)

    http = FakeHttp({(GET, f'{MAIN}/api/v1/query'): odd})

    # when
    with app_of(store, http, MAIN_ONLY, READER) as c:
        out = run_once(c)

    # then
    assert out.result.ok and out.result.ran == ('kafka:clusters', 'kafka:app', 'kafka:access:clusters', 'kafka:access:app')
    assert out.result.status == 'error: groups: ValueError'
    assert part(out.sections['kafka'], 'groups') == [] and part(out.sections['kafka'], 'clusters')


def test_kafka_presets_change_names_not_thresholds(store):
    # given
    http = FakeHttp({(GET, f'{MAIN}/api/v1/query'): answer()})

    # when
    with app_of(store, http, MAIN_ONLY, STRIMZI) as c:
        strimzi = run_once(c)
        strimzi_sent = queries(http)
        http.calls.clear()
        c.app.state.instances.update('kafka', 'main', {'preset': 'kafka-exporter'}, USER)
        plain = run_once(c)
        plain_sent = queries(http)

    # then
    s, p = ({x['key']: x for x in out.result.found} for out in (strimzi, plain))
    same = lambda x: (x['sig'], x['sub'], x['sev'], x['now'], x['ranKey'])
    assert same(s['kafka-gone|streaming/kafka/profile-service|members']) == same(p['kafka-gone|streaming-exporter/kafka/profile-service|members'])
    assert s['kafka-gone|streaming/kafka/profile-service|members']['svc'] == p['kafka-gone|streaming-exporter/kafka/profile-service|members']['svc'] == 'kafka/profile-service'
    assert same(s['kafka-broker|streaming/streaming|brokers']) == same(p['kafka-broker|streaming-exporter/streaming-exporter|brokers'])
    assert [same(x) for x in strimzi.result.found if x['sig'] not in ('kafka-disk', 'kafka-restart')] == [same(x) for x in plain.result.found]
    assert [q for q in strimzi_sent if 'kubelet_volume_stats' in q] and [q for q in strimzi_sent if 'kube_pod_container_status_restarts_total' in q]
    assert not [q for q in plain_sent if 'kubelet_volume_stats' in q or 'kube_pod_container_status_restarts_total' in q]
    assert 'max by (job) (kafka_brokers{namespace=~".*"})' in plain_sent and 'max by (namespace) (kafka_brokers{namespace=~".*"})' in strimzi_sent
    assert (strimzi.result.status, plain.result.status) == ('ok', 'ok' + SUFFIX)
    assert part(plain.sections['kafka'], 'summary')[0]['preset'] == 'kafka-exporter'


def test_the_namespaces_field_reaches_the_exporter_queries(store):
    # given
    http = FakeHttp({(GET, f'{MAIN}/api/v1/query'): vec([])})

    # when
    with app_of(store, http, MAIN_ONLY, {'main': {'prometheus': 'main', 'namespaces': 'stream-.*'}}) as c:
        run_once(c)
    sent = queries(http)

    # then
    assert 'max by (job) (kafka_brokers{namespace=~"stream-.*"})' in sent
    assert 'count by (job) (kafka_topic_partitions{namespace=~"stream-.*",topic!~"__.*"})' in sent
    assert 'sum by (namespace, job) (increase(spring_kafka_listener_seconds_count[30m]))' in sent
    assert not [q for q in sent if '{}' in q or '{,' in q]


def test_kafka_without_series_is_ok_and_quiet(store):
    # given
    path = f'{MAIN}/api/v1/query'
    http = FakeHttp({(GET, path): answer()})

    # when
    with app_of(store, http, MAIN_ONLY, READER) as c:
        c.app.state.sweeper.run('kafka', 'kafka', NOW)
        before = open_keys(store)
        http.routes[(GET, path)] = vec([])
        out = run_once(c, now=NOW + 900)
        c.app.state.sweeper.run('kafka', 'kafka', NOW + 900)
        after = open_keys(store)
        state = store.load()

    # then
    assert {'kafka-broker|streaming-exporter/streaming-exporter|brokers', 'kafka-gone|streaming-exporter/kafka/profile-service|members'} <= before
    assert (out.result.ok, out.result.status, out.result.found, out.result.ran) == (True, NO_DATA, [], ('kafka:access:clusters', 'kafka:access:groups', 'kafka:access:app', 'kafka:no-data'))
    assert after == before and passes(store)['kafka'] == NO_DATA
    assert state['kafka'] == [{'part': 'summary', 'at': iso(NOW + 900), 'source': 'main', 'sourcePrimary': True, 'preset': 'kafka-exporter', 'plugin': 'kafka'}]


def test_a_failed_part_with_the_rest_empty_closes_nothing(store):
    # given
    path = f'{MAIN}/api/v1/query'
    http = FakeHttp({(GET, path): answer()})

    def failing(q, body):
        return HttpError(502, MAIN) if 'consumergroup)' in q['query'][0] else vec([])

    # when
    with app_of(store, http, MAIN_ONLY, READER) as c:
        c.app.state.sweeper.run('kafka', 'kafka', NOW)
        before = open_keys(store)
        http.routes[(GET, path)] = failing
        out = run_once(c, now=NOW + 900)
        c.app.state.sweeper.run('kafka', 'kafka', NOW + 900)
        after = open_keys(store)

    # then
    assert out.result.ok and out.result.found == [] and out.result.ran == ('kafka:access:clusters', 'kafka:access:app', 'kafka:no-data')
    assert out.result.status == 'error: groups: Prometheus query HTTP 502' + DOT + 'no data for clusters, services'
    assert after == before and passes(store)['kafka'] == out.result.status


def test_a_partly_empty_pass_names_the_empty_part_and_keeps_its_cards(store):
    # given
    path = f'{MAIN}/api/v1/query'
    full = answer()
    http = FakeHttp({(GET, path): full})

    def no_groups(q, body):
        return vec([]) if 'consumergroup)' in q['query'][0] else full(q, body)

    # when
    with app_of(store, http, MAIN_ONLY, READER) as c:
        c.app.state.sweeper.run('kafka', 'kafka', NOW)
        before = open_keys(store)
        http.routes[(GET, path)] = no_groups
        out = run_once(c, now=NOW + 900)
        c.app.state.sweeper.run('kafka', 'kafka', NOW + 900)
        after = open_keys(store)

    # then
    assert 'kafka-gone|streaming-exporter/kafka/profile-service|members' in before
    assert out.result.ran == ('kafka:clusters', 'kafka:app', 'kafka:access:clusters', 'kafka:access:groups', 'kafka:access:app') and out.result.status == NO_DATA + ' for groups'
    assert after == before


@pytest.mark.parametrize('peer, carded, clause', [
    ({}, False, ',namespace!~".*"'),
    ({'profiles': 'spring,krakend'}, True, ''),
    ({'namespaces': 'shop|billing'}, True, ',namespace!~"shop|billing"'),
], ids=['default-peer', 'kube-off', 'other-namespaces'])
def test_kafka_restarts_leave_out_the_namespaces_the_peer_watches_with_kube(store, peer, carded, clause):
    # given
    http = FakeHttp({(GET, f'{MAIN}/api/v1/query'): answer()})

    # when
    with app_of(store, http, {'main': {'url': MAIN, 'ui-url': UI, **peer}}, STRIMZI) as c:
        out = run_once(c)
    restarts = [q for q in queries(http) if 'kube_pod_container_status_restarts_total' in q]

    # then
    assert restarts == [f'sum by (namespace, pod) (increase(kube_pod_container_status_restarts_total{{namespace=~".*"{clause}}}[30m])) > 0.5']
    assert ('kafka-restart|pipeline/pipeline-kafka-0|restarts' in {x['key'] for x in out.result.found}) is carded


def test_an_unticked_prometheus_check_leaves_its_restarts_to_kafka_until_it_is_ticked_again(store):
    # given
    http = FakeHttp({(GET, f'{MAIN}/api/v1/query'): answer()})
    card = 'kafka-restart|pipeline/pipeline-kafka-0|restarts'

    def tick(c, off: list[str]) -> None:
        store.db['instances'].update_one({'_id': 'prometheus/main'}, {'$set': {'offChecks': off}})
        c.app.state.runtime.refresh()

    def restarts() -> list[str]:
        return [q for q in queries(http) if 'kube_pod_container_status_restarts_total' in q]

    # when
    with app_of(store, http, MAIN_ONLY, STRIMZI) as c:
        c.app.state.sweeper.run('kafka', 'kafka', NOW)
        ticked, asked = open_keys(store), restarts()
        tick(c, ['prom'])
        http.calls.clear()
        c.app.state.sweeper.run('kafka', 'kafka', NOW + 900)
        unticked, widened = open_keys(store), restarts()
        tick(c, [])
        http.calls.clear()
        c.app.state.sweeper.run('kafka', 'kafka', NOW + 1800)
        again, narrowed = store.load(), restarts()

    # then
    assert card not in ticked and asked[-1].count('namespace!~".*"') == 1
    assert card in unticked and widened == ['sum by (namespace, pod) (increase(kube_pod_container_status_restarts_total{namespace=~".*"}[30m])) > 0.5']
    assert narrowed == asked
    assert {a['key']: a['status'] for a in again['anomalies']}[card] == 'cleared'


def test_one_broker_restart_gives_one_card_with_the_default_prometheus(store):
    # given
    http = FakeHttp({(GET, f'{MAIN}/api/v1/query'): answer(), (GET, f'{MAIN}/api/v1/query_range'): {'status': 'success', 'data': {'result': []}},
                     (GET, f'{MAIN}/api/v1/alerts'): {'status': 'success', 'data': {'alerts': []}}})

    # when
    with app_of(store, http, MAIN_ONLY, STRIMZI) as c:
        c.app.state.sweeper.run('prometheus', 'prom', NOW)
        c.app.state.sweeper.run('kafka', 'kafka', NOW)
        state = store.load()
    restarts = [a for a in state['anomalies'] if a['status'] == 'open' and a['svc'] == 'pipeline-kafka-0']

    # then
    assert [(a['src'], a['sig']) for a in restarts] == [('kube', 'restarts')]
    assert state['serverPasses']['kafka']['status'] == 'ok' and 'kafka-broker|streaming/streaming|brokers' in {a['key'] for a in state['anomalies']}


def test_cluster_rows_are_capped_with_troubled_ones_kept(store):
    # given
    names = [f'c{i:03d}' for i in range(120)]

    def crowd(q, body):
        e = q['query'][0]
        if 'kafka_broker_info' in e:
            return vec([({'job': n}, 3) for n in names])
        if 'kafka_brokers' in e:
            return vec([({'job': n}, 2 if n == 'c119' else 3) for n in names])
        return vec([])

    http = FakeHttp({(GET, f'{MAIN}/api/v1/query'): crowd})

    # when
    with app_of(store, http, MAIN_ONLY, READER) as c:
        out = run_once(c)
    tool = PluginTools(runtime_of(store, http, MAIN_ONLY, READER)).call('kafka', {}, NOW)

    # then
    clusters = part(out.sections['kafka'], 'clusters')
    assert len(clusters) == kafka_engine.CLUSTER_ROWS == 100 and clusters[-1]['ns'] == 'c119' and [r['ns'] for r in clusters] == sorted(r['ns'] for r in clusters)
    assert 'kafka-broker|c119/c119|brokers' in {x['key'] for x in out.result.found}
    assert len(tool['clusters']) == 100 and tool['clusters'][-1]['ns'] == 'c119' and tool['note'].startswith('first 100 of 120 clusters')


def test_group_rows_carry_the_namespace_under_the_kafka_exporter_preset(store):
    # given
    http = FakeHttp({(GET, f'{MAIN}/api/v1/query'): answer(namespace='streaming')})

    # when
    with app_of(store, http, MAIN_ONLY, READER) as c:
        out = run_once(c)
    sent = queries(http)

    # then
    groups = part(out.sections['kafka'], 'groups')
    assert {g['namespace'] for g in groups} == {'streaming'} and {g['ns'] for g in groups} == {'streaming-exporter', 'pipeline-exporter'}
    assert 'max by (job, namespace, consumergroup) (kafka_consumergroup_members{namespace=~".*"})' in sent
    assert 'kafka-gone|streaming-exporter/kafka/profile-service|members' in {x['key'] for x in out.result.found}


def test_a_slow_prometheus_keeps_the_parts_that_answered_in_time(store, monkeypatch):
    # given
    http = FakeHttp({(GET, f'{MAIN}/api/v1/query'): answer()})
    ticks = iter([0.0] * 13 + [1000.0] * 100)

    # when
    with app_of(store, http, MAIN_ONLY, READER) as c:
        monkeypatch.setattr(promql, 'clock', lambda: next(ticks))
        out = run_once(c)

    # then
    assert out.result.ok and out.result.ran == ('kafka:clusters', 'kafka:access:clusters') and out.result.status == 'error: groups: timed out; services: timed out'
    assert part(out.sections['kafka'], 'clusters') and not [q for q in queries(http) if 'consumergroup)' in q or 'spring_kafka' in q]


def test_kafka_second_instance_is_scoped(store):
    # given
    http = FakeHttp({(GET, f'{MAIN}/api/v1/query'): answer(), (GET, f'{EAST}/api/v1/query'): answer('east-')})

    # when
    with app_of(store, http, BOTH, {'main': {'prometheus': 'main'}, 'east': {'prometheus': 'east'}}) as c:
        c.app.state.sweeper.run('kafka', 'kafka', NOW)
        state = store.load()
    cards = {a['key']: a for a in state['anomalies']}

    # then
    east = cards['kafka-gone|east-streaming-exporter/kafka/east-profile-service|members@east']
    assert (east['cluster'], east['ranKey']) == ('east', 'kafka:groups-east')
    assert cards['kafka-broker|east-streaming-exporter/east-streaming-exporter|brokers@east']['ranKey'] == 'kafka:clusters-east'
    main = cards['kafka-gone|streaming-exporter/kafka/profile-service|members']
    assert 'cluster' not in main and main['ranKey'] == 'kafka:groups'
    assert passes(store) == {'kafka': 'ok' + SUFFIX, 'kafka-east': 'ok' + SUFFIX}
    assert east['links'][0]['url'].startswith(f'{EAST}/query?') and main['links'][0]['url'].startswith(f'{UI}/query?')


def test_kafka_follows_its_prometheus_instance(store):
    # given
    http = FakeHttp({(GET, f'{MAIN}/api/v1/query'): answer(), (GET, f'{EAST}/api/v1/query'): answer('east-')})

    # when
    with app_of(store, http, BOTH, {'main': {'prometheus': 'main'}, 'east': {'prometheus': 'east'}}) as c:
        c.app.state.sweeper.run('kafka', 'kafka', NOW)
        before = open_keys(store)
        c.app.state.instances.set_enabled('prometheus', 'east', False, USER)
        c.app.state.sweeper.run('kafka', 'kafka', NOW + 900)
        after = open_keys(store)
        found = passes(store)

    # then
    assert 'kafka-gone|east-streaming-exporter/kafka/east-profile-service|members@east' in before
    assert found['kafka-east'] == 'error: ' + msg('peers.unavailable', plugin='Prometheus', instance='east')
    assert found['kafka'] == 'ok' + SUFFIX and after == before


def test_kafka_rules_come_from_the_rule_set(store):
    # given
    http = FakeHttp({(GET, f'{MAIN}/api/v1/query'): answer()})

    # when
    with app_of(store, http, MAIN_ONLY, STRIMZI) as c:
        default = {x['key'] for x in run_once(c).result.found}
        c.app.state.runtime.live.set('kafka.rules', '{"gone_lag": 1000}', USER)
        raised = {x['key'] for x in run_once(c).result.found}

    # then
    assert default - raised == {'kafka-gone|streaming/kafka/profile-service|members'} and raised < default


def test_the_kafka_tool_reads_clusters_groups_one_group_topics_apps_and_a_history(store):
    # given
    def tool_answer(q, body):
        e = q['query'][0]
        if 'by (namespace, topic) (kafka_consumergroup_lag_sum' in e:
            return vec([({'namespace': 'streaming', 'topic': 'commerce.CartUpdated'}, 7), ({'namespace': 'streaming', 'topic': 'commerce.Other'}, 1)])
        if 'kafka_consumergroup_current_offset' in e:
            return vec([({'namespace': 'streaming', 'topic': 'commerce.CartUpdated'}, 0.4)])
        if 'topk(' in e:
            return vec([({'namespace': 'pipeline', 'topic': 'app-logs'}, 176.2)])
        return answer()(q, body)

    history_body = {'status': 'success', 'data': {'result': [{'metric': {'namespace': 'streaming'}, 'values': [[NOW - 60, '90'], [NOW, '120']]}]}}
    http = FakeHttp({(GET, f'{MAIN}/api/v1/query'): tool_answer, (GET, f'{MAIN}/api/v1/query_range'): history_body})
    tools = PluginTools(runtime_of(store, http, MAIN_ONLY, STRIMZI))

    # when
    clusters = tools.call('kafka', {}, NOW)
    groups = tools.call('kafka', {'op': 'groups', 'cluster': 'pipeline'}, NOW)
    one = tools.call('kafka', {'op': 'group', 'group': 'cart-service'}, NOW)
    topics = tools.call('kafka', {'op': 'topics'}, NOW)
    apps = tools.call('kafka', {'op': 'apps', 'job': 'inventory-service'}, NOW)
    history = tools.call('kafka', {'op': 'history', 'group': 'cart-service', 'hours': 6}, NOW)
    with pytest.raises(ToolError) as nameless:
        tools.call('kafka', {'op': 'history'}, NOW)
    with pytest.raises(ToolError) as quoted:
        tools.call('kafka', {'op': 'group', 'group': 'a"b'}, NOW)
    with pytest.raises(ToolError) as hours:
        tools.call('kafka', {'op': 'history', 'group': 'cart-service', 'hours': 'many'}, NOW)

    # then
    assert tools.scan('kafka') and tools.cap('kafka') == 6
    assert [c['ns'] for c in clusters['clusters']] == ['pipeline', 'streaming']
    assert [g['group'] for g in groups['groups']] == ['log-shipper']
    assert one['topics'][0] == {'ns': 'streaming', 'topic': 'commerce.CartUpdated', 'lag': 7, 'd1': 7, 'd7': 7, 'readPerS': 0.4}
    assert topics['topics'][0]['topic'] == 'app-logs' and topics['topics'][0]['msgs'] == 176.2
    assert [(a['source'], a['job']) for a in apps['apps']] == [('main', 'inventory-service')]
    assert history['points'][-1][1] == 120 and history['stepSeconds'] == 180
    assert 'needs group' in str(nameless.value)
    assert str(quoted.value) == 'group may hold only letters, digits, dot, dash and underscore'
    assert str(hours.value) == 'hours must be 1-48'


def test_the_kafka_tool_is_not_offered_without_an_instance(store):
    # given
    tools = PluginTools(runtime_of(store, FakeHttp(), MAIN_ONLY, {}))

    # when
    offered = [d['name'] for d in tools.definitions()]

    # then
    assert 'kafka' not in offered and tools.get('kafka') is None and 'promql' in offered


def test_kafka_links_are_the_card_urls_over_https_only():
    # given
    own = {'src': 'kafka', 'urls': [{'label': 'Lag graph', 'url': f'{UI}/query?g0.expr=x'}, {'label': 'Plain', 'url': 'http://prom.example.com/query'}, {'label': 'Script', 'url': 'javascript:alert(1)'}]}

    # when
    mine = kafka.links(own, None)
    other = kafka.links({'src': 'prom', 'urls': own['urls']}, None)

    # then
    assert mine == [{'label': 'Lag graph', 'url': f'{UI}/query?g0.expr=x'}] and other is None


def test_a_refused_kafka_part_is_a_warn_card_and_its_own_cards_stay_open(store):
    # given
    path = f'{MAIN}/api/v1/query'
    full = answer()
    http = FakeHttp({(GET, path): full})
    card = 'no-access|-/kafka|groups'
    gone = 'kafka-gone|streaming-exporter/kafka/profile-service|members'

    def refusing(q, body):
        return HttpError(403, MAIN) if 'consumergroup)' in q['query'][0] else full(q, body)

    # when
    with app_of(store, http, MAIN_ONLY, READER) as c:
        c.app.state.sweeper.run('kafka', 'kafka', NOW)
        before = open_keys(store)
        http.routes[(GET, path)] = refusing
        out = run_once(c, now=NOW + 900)
        c.app.state.sweeper.run('kafka', 'kafka', NOW + 900)
        refused = open_keys(store)
        http.routes[(GET, path)] = full
        c.app.state.sweeper.run('kafka', 'kafka', NOW + 1800)
        back = {a['key']: a['status'] for a in store.load()['anomalies']}
    all_refused = FakeHttp({(GET, path): HttpError(403, path)})
    runtime = runtime_of(store, all_refused, MAIN_ONLY, READER)
    tested = kafka.test(runtime.context(kafka.PLUGIN, runtime.instances.get('kafka', 'main'), NOW))
    with pytest.raises(ToolError) as told:
        PluginTools(runtime).call('kafka', {'op': 'groups'}, NOW)

    # then
    assert gone in before and card not in before
    assert out.result.ok and out.result.status == 'ok \u00b7 no access to groups'
    [warn] = [x for x in out.result.found if x['key'] == card]
    assert (warn['sev'], warn['ranKey'], warn['label']) == ('warn', 'kafka:access:groups', 'Kafka cannot read groups: the server requires credentials and none are set')
    assert set(out.result.ran) == {'kafka:clusters', 'kafka:app', 'kafka:access:clusters', 'kafka:access:groups', 'kafka:access:app'}
    assert refused == before | {card}
    assert back[card] == 'cleared' and back[gone] == 'open'
    assert (tested.ok, tested.summary) == (False, 'Kafka cannot read clusters: the server requires credentials and none are set')
    assert str(told.value) == 'Kafka cannot read groups: the server requires credentials and none are set'
