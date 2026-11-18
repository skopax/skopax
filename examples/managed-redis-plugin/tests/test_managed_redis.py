import json
from dataclasses import dataclass, field
from urllib.parse import parse_qs, urlsplit

import pytest

from skopax.contract import HttpError, InstanceInfo, Peer, ReadError
from skopax.kit import access
from skopax.remote import Reply

import skopax_managed_redis
from skopax_managed_redis import CARD_TYPES, MAX_ROWS, PLUGIN, RULES, links, queries, read, run

NOW = 1_793_000_000.0
GRAFANA = 'https://grafana.example.com'
TOKEN = 'test-token-1'
UID = 'ds-1'
SOURCE = 'cloud-metrics'
MAIN, CACHE = 'cache-main.example.com:6379', 'cache-sessions.example.com:6379'
GIB = 2 ** 30
Q = queries('redis_', 'instance')


def healthy() -> dict:
    data = {k: [] for k in Q}
    for k, v in dict(up=1, clients=100, maxClients=10000, memory=2 * GIB, maxMemory=8 * GIB, evicted=0, rejected=0, uptime=86400 * 30, ops=700,
                     avgUs=100, avgD1=90, avgD7=110).items():
        data[k] = [({'instance': n}, v) for n in (MAIN, CACHE)]
    return data


def put(data: dict, k: str, name: str, v) -> dict:
    data[k] = [(m, x) for m, x in data[k] if m.get('instance') != name] + ([({'instance': name}, v)] if v is not None else [])
    return data


@dataclass
class FakeGrafana:
    data: dict = field(default_factory=healthy)
    listed: object = field(default_factory=lambda: [{'uid': 'x', 'name': 'other', 'type': 'prometheus'}, {'uid': UID, 'name': SOURCE, 'type': 'prometheus'}])
    error: Exception | None = None
    queries: dict = field(default_factory=lambda: Q)
    asked: list = field(default_factory=list)

    def send(self, method, url, headers, body, timeout):
        parts = urlsplit(url)
        q = {k: v[0] for k, v in parse_qs(parts.query).items()}
        self.asked.append((parts.path, q, headers.get('Authorization')))
        if parts.path == '/api/datasources':
            answer = self.listed
        elif self.error is not None:
            raise self.error
        elif parts.path == f'/api/datasources/proxy/uid/{UID}/api/v1/query_range':
            answer = {'status': 'success', 'data': {'result': [{'metric': {'instance': MAIN}, 'values': [[NOW - 60 * n, str(n)] for n in range(200, 0, -1)]}]}}
        elif parts.path == f'/api/datasources/proxy/uid/{UID}/api/v1/query':
            by_query = {expr: k for k, expr in self.queries.items()}
            rows = self.data.get(by_query.get(q['query']), [])
            answer = {'status': 'success', 'data': {'resultType': 'vector', 'result': [{'metric': m, 'value': [NOW, str(v)]} for m, v in rows]}}
        else:
            raise HttpError(404, url)
        if isinstance(answer, Exception):
            raise answer
        return Reply(answer, {})


@dataclass
class Ctx:
    grafana: FakeGrafana = field(default_factory=FakeGrafana)
    name: str = 'cloud'
    config: dict = field(default_factory=lambda: {'grafana': 'main', 'datasource': SOURCE, 'prefix': 'redis_', 'label': 'instance'})
    overrides: dict = field(default_factory=dict)
    peered: bool = True
    url: str = GRAFANA
    now: float = NOW

    @property
    def instance(self) -> InstanceInfo:
        return InstanceInfo(self.name, True, None, self.config)

    def peer(self, name: str) -> Peer | None:
        assert name == 'grafana'
        if not self.peered:
            return None
        return Peer(InstanceInfo('main', True, None, {'url': self.url, 'token': TOKEN}), lambda: self.grafana.send)

    def rules(self, set_id: str) -> dict:
        assert set_id == 'rules'
        return dict(self.overrides)


def key(name: str, sig: str, instance: str = 'cloud') -> str:
    return f'managed-redis|{instance}|{name}|{sig}'


def raised(data: dict, **given) -> set[tuple[str, str]]:
    return {(c['key'], c['sev']) for c in run(Ctx(FakeGrafana(data=data), **given)).result.found}


def test_the_plugin_takes_one_instance_per_datasource_and_reads_through_a_grafana_peer():
    # when
    fields = {f.key: f for f in PLUGIN.fields}
    [check] = PLUGIN.checks
    [tool] = PLUGIN.tools

    # then
    assert PLUGIN.max_instances is None and PLUGIN.uses == ('grafana',)
    assert (fields['grafana'].kind, fields['grafana'].choices, fields['grafana'].required) == ('instance', ('grafana',), True)
    assert (fields['datasource'].kind, fields['datasource'].required) == ('text', True)
    assert (fields['prefix'].default, fields['label'].default) == ('redis_', 'instance')
    assert (check.id, check.ran, check.sections, check.tool) == ('managed-redis', ('managed-redis',), ('managed-redis',), 'managed_redis')
    assert [(r.id, dict(r.defaults)) for r in PLUGIN.rules] == [('rules', RULES)] and set(PLUGIN.rules[0].help) == set(RULES)
    assert (tool.name, tool.kind, tool.per_run, tool.scan) == ('managed_redis', 'read', 6, True)
    assert '{field:grafana}' in tool.description and '{field:datasource}' in tool.description
    assert all(t.sig.startswith('managed-redis-') and len(t.sig) <= 30 for t in CARD_TYPES)
    assert PLUGIN.panels == ()


def test_the_default_queries_use_the_community_redis_exporter_names():
    # then
    assert Q['up'] == 'max by (instance) (redis_up)'
    assert Q['clients'] == 'max by (instance) (max_over_time(redis_connected_clients[30m]))'
    assert Q['memory'] == 'max by (instance) (max_over_time(redis_memory_used_bytes[30m]))'
    assert Q['maxMemory'] == 'max by (instance) (redis_memory_max_bytes)'
    assert Q['evicted'] == 'sum by (instance) (increase(redis_evicted_keys_total[30m]))'
    assert Q['avgD7'] == ('1e6 * sum by (instance) (increase(redis_commands_duration_seconds_total[30m] offset 7d)) / '
                          'sum by (instance) (increase(redis_commands_processed_total[30m] offset 7d))')
    assert queries('cloud_redis_', 'name')['up'] == 'max by (name) (cloud_redis_up)'


def test_a_healthy_set_raises_nothing_and_fills_one_row_per_instance():
    # given
    ctx = Ctx()

    # when
    sweep = run(ctx)

    # then
    out = sweep.result
    assert (out.name, out.ok, out.status, out.found, out.ran) == ('managed-redis', True, 'ok', [], ('managed-redis', 'managed-redis:access:metrics'))
    rows = sweep.sections['managed-redis']
    assert [r['name'] for r in rows if r['part'] == 'instance'] == [MAIN, CACHE]
    assert rows[0] == {'part': 'instance', 'name': MAIN, 'up': 1, 'clients': 100, 'maxClients': 10000, 'clientsPct': 1.0, 'memory': 2 * GIB, 'maxMemory': 8 * GIB,
                       'memoryPct': 25.0, 'evicted': 0, 'rejected': 0, 'uptime': 86400 * 30, 'ops': 700, 'avgUs': 100, 'avgD1': 90, 'avgD7': 110}
    assert rows[-1] == {'part': 'summary', 'at': '2026-10-26T07:33:20Z', 'source': 'main', 'datasource': SOURCE, 'prefix': 'redis_', 'label': 'instance'}
    assert all(auth == f'Bearer {TOKEN}' for _, _, auth in ctx.grafana.asked)
    assert [p for p, _, _ in ctx.grafana.asked].count('/api/datasources') == 1


def test_every_card_kind_is_a_declared_card_type_with_a_grafana_link():
    # given
    data = put(healthy(), 'up', CACHE, 0)
    for k, v in dict(memory=7.8 * GIB, clients=9000, avgUs=900, evicted=5, rejected=2, uptime=60).items():
        put(data, k, MAIN, v)

    # when
    found = run(Ctx(FakeGrafana(data=data))).result.found

    # then
    assert {c['sig'] for c in found} == {t.sig for t in CARD_TYPES}
    assert {(c['key'], c['sev']) for c in found} == {(key(CACHE, 'down'), 'crit'), (key(MAIN, 'memory'), 'crit'), (key(MAIN, 'clients'), 'warn'),
                                                     (key(MAIN, 'latency'), 'warn'), (key(MAIN, 'evicted'), 'warn'), (key(MAIN, 'rejected'), 'warn'),
                                                     (key(MAIN, 'restart'), 'warn')}
    assert all(c['src'] == 'managed-redis' and c['ranKey'] == 'managed-redis' for c in found)
    assert all(c['urls'][0]['label'] == 'Redis metric in Grafana' and c['urls'][0]['url'].startswith(f'{GRAFANA}/explore?') for c in found)
    assert all(c['what'].isascii() and c['short'].isascii() for c in found)


@pytest.mark.parametrize('used_gib, max_gib, expect', [(6.7, 8, None), (6.8, 8, 'warn'), (7.6, 8, 'crit'), (7.9, 0, None)])
def test_memory_warns_near_its_limit_and_is_skipped_without_one(used_gib, max_gib, expect):
    # given
    data = put(put(healthy(), 'memory', MAIN, used_gib * GIB), 'maxMemory', MAIN, max_gib * GIB)

    # then
    assert raised(data) == ({(key(MAIN, 'memory'), expect)} if expect else set())


@pytest.mark.parametrize('avg, d1, d7, expect', [
    (900, None, None, False), (150, 40, 50, False), (269, 90, None, False), (300, 100, 90, True), (300, None, 100, True), (300, 90, 110, False),
])
def test_command_time_needs_a_baseline_of_one_or_seven_days(avg, d1, d7, expect):
    # given
    data = put(put(put(healthy(), 'avgUs', MAIN, avg), 'avgD1', MAIN, d1), 'avgD7', MAIN, d7)

    # when
    found = run(Ctx(FakeGrafana(data=data))).result.found

    # then
    assert {(c['key'], c['sev']) for c in found} == ({(key(MAIN, 'latency'), 'warn')} if expect else set())
    assert all((c['now'], c['d1'], c['d7']) == (avg, d1, d7) for c in found)


def test_the_rules_come_from_the_rule_set_of_the_instance():
    # given
    data = put(healthy(), 'memory', MAIN, 5 * GIB)

    # then
    assert raised(data) == set()
    assert raised(data, overrides={'mem_warn': 60}) == {(key(MAIN, 'memory'), 'warn')}
    assert raised(put(healthy(), 'uptime', MAIN, 5000), overrides={'uptime_s': 7200}) == {(key(MAIN, 'restart'), 'warn')}


@pytest.mark.parametrize('where, code, right', [('list', 403, 'datasources:read'), ('query', 401, 'datasources:query'), ('query', 403, 'datasources:query')])
def test_a_refused_read_raises_one_access_card_and_keeps_the_other_cards_open(where, code, right):
    # given
    grafana = FakeGrafana(listed=HttpError(code, GRAFANA)) if where == 'list' else FakeGrafana(error=HttpError(code, GRAFANA))

    # when
    out = run(Ctx(grafana)).result

    # then
    [card] = out.found
    assert (out.ok, out.ran, out.status) == (True, ('managed-redis:access:metrics',), access.status('ok', ['Redis metrics']))
    assert (card['sig'], card['ranKey']) == ('no-access', 'managed-redis:access:metrics')
    assert card['label'] == access.text('Managed Redis', 'Redis metrics', right, status=code)


def test_no_series_no_datasource_no_peer_or_a_failing_grafana_is_an_error_pass_that_clears_nothing():
    # when
    empty = run(Ctx(FakeGrafana(data={}))).result
    missing = run(Ctx(config={'grafana': 'main', 'datasource': 'gone', 'prefix': 'redis_', 'label': 'instance'})).result
    unset = run(Ctx(peered=False)).result
    down = run(Ctx(FakeGrafana(listed=HttpError(502, GRAFANA)))).result
    broken = run(Ctx(FakeGrafana(listed={'not': 'a list'}))).result

    # then
    assert (empty.ok, empty.status, empty.ran) == (False, f'error: datasource {SOURCE} holds no redis_up series', ())
    assert (missing.ok, missing.status, missing.ran) == (False, 'error: no Grafana datasource named gone', ())
    assert (unset.ok, unset.status, unset.ran) == (False, 'error: no Grafana instance is set', ())
    assert (down.ok, down.status, down.ran) == (False, 'error: Grafana datasources: HTTP 502', ())
    assert (broken.ok, broken.status) == (False, 'error: Grafana datasources: the answer is not a list')


@pytest.mark.parametrize('prefix, label', [('redis_', 'instance'), ('cloud:redis_', 'name'), ('_x', '_y')])
def test_a_prefix_and_label_of_metric_and_label_names_are_used_in_every_query(prefix, label):
    # given
    data = {k: [({label: MAIN}, v)] for k, v in dict(up=1, clients=1, maxClients=10, memory=1, maxMemory=10, evicted=0, rejected=0, uptime=86400, ops=1,
                                                     avgUs=1, avgD1=1, avgD7=1).items()}
    grafana = FakeGrafana(data=data, queries=queries(prefix, label))

    # when
    sweep = run(Ctx(grafana, config={'grafana': 'main', 'datasource': SOURCE, 'prefix': prefix, 'label': label}))

    # then
    assert sweep.result.ok and [r['name'] for r in sweep.sections['managed-redis'] if r['part'] == 'instance'] == [MAIN]
    asked = [q['query'] for p, q, _ in grafana.asked if p.endswith('/query')]
    assert len(asked) == len(Q) and all(f'by ({label})' in q and prefix in q for q in asked)


@pytest.mark.parametrize('prefix, label, problem', [
    ('', 'instance', 'the metric prefix must be a metric name start such as redis_'),
    ('redis-', 'instance', 'the metric prefix must be a metric name start such as redis_'),
    ('redis_{x="1"} or ', 'instance', 'the metric prefix must be a metric name start such as redis_'),
    ('9redis', 'instance', 'the metric prefix must be a metric name start such as redis_'),
    ('redis_', '', 'the instance label must be a label name such as instance'),
    ('redis_', 'name) (x', 'the instance label must be a label name such as instance'),
    ('redis_', 'a:b', 'the instance label must be a label name such as instance'),
    ('redis_', 'x' * 101, 'the instance label must be a label name such as instance'),
])
def test_a_prefix_or_label_that_could_change_the_query_fails_the_pass_the_test_and_the_tool_without_a_request(prefix, label, problem):
    # given
    ctx = Ctx(config={'grafana': 'main', 'datasource': SOURCE, 'prefix': prefix, 'label': label})

    # when
    out = run(ctx).result
    said = skopax_managed_redis.test(ctx)
    with pytest.raises(ReadError) as e:
        read(ctx, {})

    # then
    assert (out.ok, out.status, out.found, out.ran) == (False, f'error: {problem}', [], ())
    assert (said.ok, said.summary) == (False, problem)
    assert (e.value.status, e.value.message) == (400, problem)
    assert ctx.grafana.asked == []


def test_card_links_open_grafana_explore_on_an_https_grafana_only():
    # given
    data = put(healthy(), 'evicted', MAIN, 5)

    # when
    [card] = run(Ctx(FakeGrafana(data=data))).result.found
    [plain] = run(Ctx(FakeGrafana(data=data), url='http://grafana.example.com:3000')).result.found

    # then
    [link] = links(card, InstanceInfo('cloud', True, None, {}))
    panes = json.loads(parse_qs(urlsplit(link['url']).query)['panes'][0])
    assert link == card['urls'][0] and link['url'].startswith(f'{GRAFANA}/explore?schemaVersion=1&panes=')
    assert panes['a']['queries'][0]['expr'] == 'sum by (instance) (increase(redis_evicted_keys_total{instance="cache-main.example.com:6379"}[30m]))'
    assert (panes['a']['datasource'], panes['a']['queries'][0]['datasource'], panes['a']['range']) == (UID, {'type': 'prometheus', 'uid': UID}, {'from': 'now-2d', 'to': 'now'})
    assert 'urls' not in plain and links(plain, InstanceInfo('cloud', True, None, {})) == []
    assert links({'src': 'other', 'urls': [{'label': 'x', 'url': GRAFANA}]}, InstanceInfo('cloud', True, None, {})) is None


def test_two_instances_keep_distinct_card_keys():
    # given
    data = put(healthy(), 'evicted', MAIN, 5)

    # when
    cloud = run(Ctx(FakeGrafana(data=data), name='cloud')).result.found
    backup = run(Ctx(FakeGrafana(data=data), name='backup')).result.found

    # then
    assert [c['key'] for c in cloud] == [key(MAIN, 'evicted')] and [c['key'] for c in backup] == [key(MAIN, 'evicted', 'backup')]


def test_test_connection_names_the_redis_instances_it_found():
    # when
    said = skopax_managed_redis.test(Ctx())
    empty = skopax_managed_redis.test(Ctx(FakeGrafana(data={})))
    unset = skopax_managed_redis.test(Ctx(peered=False))
    refused = skopax_managed_redis.test(Ctx(FakeGrafana(listed=HttpError(403, GRAFANA))))

    # then
    assert (said.ok, said.summary) == (True, f'2 Redis instances through Grafana main ({SOURCE}): {MAIN}, {CACHE}')
    assert (empty.ok, empty.summary) == (True, f'Grafana main answered, but datasource {SOURCE} holds no redis_up series')
    assert (unset.ok, unset.summary) == (False, 'no Grafana instance is set')
    assert (refused.ok, refused.summary) == (False, access.text('Managed Redis', 'Redis metrics', 'datasources:read', status=403))


def test_the_tool_reads_the_instances_down_ones_first_and_caps_the_rows():
    # given
    many = {k: [({'instance': f'cache-{n:02}.example.com:6379'}, 1) for n in range(MAX_ROWS + 10)] for k in Q}
    put(many, 'up', 'cache-30.example.com:6379', 0)

    # when
    small = read(Ctx(), {})
    capped = read(Ctx(FakeGrafana(data=many)), {'op': 'instances'})

    # then
    assert set(small) == {'instances', 'note'} and [r['name'] for r in small['instances']] == [MAIN, CACHE]
    assert set(small['instances'][0]) == {'name', 'up', 'clients', 'maxClients', 'clientsPct', 'memory', 'maxMemory', 'memoryPct', 'evicted', 'rejected', 'uptime', 'ops',
                                          'avgUs', 'avgD1', 'avgD7'}
    assert not small['note'].startswith('first')
    assert MAX_ROWS == 50 and len(capped['instances']) == MAX_ROWS and capped['instances'][0]['name'] == 'cache-30.example.com:6379'
    assert capped['note'].startswith(f'first {MAX_ROWS} of {MAX_ROWS + 10} instances, down ones first; ')


def test_the_tool_reads_one_metric_of_one_instance_over_time():
    # given
    grafana = FakeGrafana()

    # when
    out = read(Ctx(grafana), {'op': 'history', 'name': MAIN, 'metric': 'memory', 'hours': 6})

    # then
    assert (out['name'], out['metric'], out['stepSeconds']) == (MAIN, 'memory', 180)
    assert len(out['points']) == 120 and out['points'][-1] == ['2026-10-26T07:32:20Z', 1.0]
    [(path, q, _)] = [a for a in grafana.asked if a[0].endswith('/query_range')]
    assert q['query'] == 'max by (instance) (redis_memory_used_bytes{instance="cache-main.example.com:6379"})'
    assert (float(q['end']) - float(q['start']), q['step']) == (6 * 3600, '180')


@pytest.mark.parametrize('args, text', [
    ({'op': 'apps'}, 'op must be one of instances, history'),
    ({'op': 'history'}, 'op history needs name (a Redis instance as the label names it)'),
    ({'op': 'history', 'name': MAIN, 'metric': 'cpu'}, 'metric must be one of up, clients, memory, evicted, rejected, uptime, ops'),
    ({'op': 'history', 'name': MAIN, 'hours': 49}, 'hours must be 1-48'),
    ({'op': 'history', 'name': MAIN, 'hours': 'x'}, 'hours must be 1-48'),
])
def test_the_tool_refuses_bad_arguments(args, text):
    # when
    with pytest.raises(ReadError) as e:
        read(Ctx(), args)

    # then
    assert (e.value.status, e.value.message) == (400, text)


def test_the_tool_names_a_refused_read_and_a_missing_peer():
    # when
    with pytest.raises(ReadError) as refused:
        read(Ctx(FakeGrafana(error=HttpError(403, GRAFANA))), {})
    with pytest.raises(ReadError) as unset:
        read(Ctx(peered=False), {})

    # then
    assert (refused.value.status, refused.value.message) == (403, access.text('Managed Redis', 'Redis metrics', 'datasources:query', status=403))
    assert (unset.value.status, unset.value.message) == (503, 'no Grafana instance is set')
