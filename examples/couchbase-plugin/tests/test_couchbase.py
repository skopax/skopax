import json
from dataclasses import dataclass, field
from urllib.parse import parse_qs, unquote, urlsplit

import pytest

from skopax.contract import HttpError, InstanceInfo, Peer, ReadError
from skopax.kit import access
from skopax.remote import Reply

import skopax_couchbase
from skopax_couchbase import CARD_TYPES, LINK, MAX_ROWS, PLUGIN, RULES, links, nodes, queries, run

NOW = 1_792_224_000.0
PROM = 'http://prometheus.example.com:9090'
UI = 'https://prometheus.example.com'
CLUSTER = 'orders-prod'
SELECTOR = 'job=~".*couchbase.*",env="prod"'
Q = queries(SELECTOR)
NODES = ('node-1.example.com:8091', 'node-2.example.com:8091', 'node-3.example.com:8091')
N1, N2, N3 = NODES
GIB = 2 ** 30


def key(subject: str, sig: str, instance: str = 'orders') -> str:
    return f'couchbase|{instance}|{subject}|{sig}'


def healthy() -> dict:
    data = {(k, i): [] for k in Q for i in range(3)}
    for k, v in dict(up=1, uptime=86400 * 200, conn=90, imem=3 * GIB, iquota=4 * GIB).items():
        data[(k, 0)] = [({'instance': n}, v) for n in NODES]
    for k, v in dict(failover=0, stuck=0, rebalance=0, req=2000, errors=0, timeouts=0, rtime=2000 * 35e6, queue=0, auth=0).items():
        data[(k, 0)] = [({}, v)]
    return data


def put(data: dict, k: str, i: int, v) -> dict:
    data[(k, i)] = [({}, v)]
    return data


def node_put(data: dict, k: str, i: int, node: str, v) -> dict:
    rows = [(m, x) for m, x in data[(k, i)] if m.get('instance') != node]
    data[(k, i)] = rows + ([({'instance': node}, v)] if v is not None else [])
    return data


class FakeProm:
    def __init__(self, data: dict | None = None, error: Exception | None = None, queries: dict | None = None):
        self.data = data or {}
        self.by_query = {q: k for k, q in (queries or Q).items()}
        self.error = error
        self.calls: list[str] = []

    def __call__(self, method, url, headers, body, timeout):
        params = parse_qs(urlsplit(url).query)
        q = params['query'][0]
        self.calls.append(q)
        if self.error is not None:
            raise self.error
        at = float(params['time'][0]) if 'time' in params else NOW
        i = {0: 0, 1: 1, 7: 2}[round((NOW - at) / 86400)]
        rows = self.data.get((self.by_query.get(q), i), [])
        return Reply({'status': 'success', 'data': {'resultType': 'vector', 'result': [{'metric': m, 'value': [at, str(v)]} for m, v in rows]}}, {})


@dataclass
class Ctx:
    prom: object = None
    name: str = 'orders'
    config: dict = field(default_factory=lambda: {'prometheus': 'metrics', 'cluster': CLUSTER, 'selector': SELECTOR})
    overrides: dict = field(default_factory=dict)
    rows: list | None = None
    peered: bool = True
    ui: str = UI
    now: float = NOW

    @property
    def instance(self) -> InstanceInfo:
        return InstanceInfo(self.name, True, None, self.config)

    def peer(self, name: str) -> Peer | None:
        assert name == 'prometheus'
        if not self.peered:
            return None
        return Peer(InstanceInfo('metrics', False, 'metrics', {'url': PROM, 'ui-url': self.ui}), lambda: self.prom)

    def rules(self, set_id: str) -> dict:
        assert set_id == 'rules'
        return dict(self.overrides)

    def previous(self, section: str) -> list | None:
        assert section == 'couchbase'
        return self.rows


def raised(data: dict, **given) -> set[tuple[str, str]]:
    return {(c['key'], c['sev']) for c in run(Ctx(FakeProm(data), **given)).result.found}


def test_the_plugin_takes_one_instance_per_cluster_and_reads_through_a_prometheus_peer():
    # when
    fields = {f.key: f for f in PLUGIN.fields}
    [check] = PLUGIN.checks
    [tool] = PLUGIN.tools

    # then
    assert PLUGIN.max_instances is None and PLUGIN.uses == ('prometheus',)
    assert (fields['prometheus'].kind, fields['prometheus'].choices, fields['prometheus'].required) == ('instance', ('prometheus',), True)
    assert (fields['cluster'].kind, fields['selector'].kind, fields['selector'].default) == ('text', 'text', 'job=~".*couchbase.*"')
    assert (check.id, check.ran, check.sections, check.tool) == ('couchbase', ('couchbase',), ('couchbase',), 'couchbase_nodes')
    assert [(r.id, dict(r.defaults)) for r in PLUGIN.rules] == [('rules', RULES)] and set(PLUGIN.rules[0].help) == set(RULES)
    assert (tool.name, tool.kind, tool.per_run, tool.scan) == ('couchbase_nodes', 'read', 4, True)
    assert '{field:cluster}' in tool.description and '{field:prometheus}' in tool.description
    assert {t.sig for t in CARD_TYPES} == set(LINK) and all(t.sig.startswith('couchbase-') and len(t.sig) <= 30 for t in CARD_TYPES)
    assert [(p.tab, p.script, p.group) for p in PLUGIN.panels] == [('couchbase', 'panel.js', 'observe')]


def test_a_healthy_cluster_raises_nothing_and_fills_one_row_per_node_from_the_up_series():
    # given
    prom = FakeProm(node_put(healthy(), 'up', 0, 'node-0.example.com:8091', 1))

    # when
    sweep = run(Ctx(prom))

    # then
    out = sweep.result
    assert (out.name, out.ok, out.status, out.found, out.ran) == ('couchbase', True, 'ok', [], ('couchbase', 'couchbase:access:metrics'))
    assert len(prom.calls) == 3 * len(Q)
    rows = sweep.sections['couchbase']
    assert [r['node'] for r in rows if r['part'] == 'node'] == ['node-0.example.com:8091', *NODES]
    assert rows[1] == {'part': 'node', 'name': CLUSTER, 'node': N1, 'up': 1, 'uptime': 86400 * 200, 'conn': 90, 'connD1': None, 'connD7': None,
                       'imem': 3 * GIB, 'iquota': 4 * GIB, 'imemPct': 75.0}
    [summary] = [r for r in rows if r['part'] == 'summary']
    assert (summary['name'], summary['req'], summary['errors'], summary['share'], summary['avgMs'], summary['queue']) == (CLUSTER, 2000, 0, 0.0, 35.0, 0)
    assert (summary['selector'], summary['source'], summary['ui'], summary['at']) == (SELECTOR, 'metrics', UI, '2026-10-17T08:00:00Z')
    assert all('cluster' not in r for r in rows)


def test_every_card_kind_is_a_declared_card_type_with_a_graph():
    # given
    data = node_put(node_put(healthy(), 'up', 0, N1, 0), 'uptime', 0, N2, 60)
    node_put(node_put(data, 'conn', 0, N3, 1000), 'conn', 1, N3, 100)
    node_put(data, 'imem', 0, N2, 3.9 * GIB)
    node_put(data, 'up', 1, 'node-9.example.com:8091', 1)
    for k, v in dict(failover=1, stuck=1, errors=100, timeouts=50, rtime=2000 * 900e6, queue=50, auth=40).items():
        put(data, k, 0, v)

    # when
    found = run(Ctx(FakeProm(data))).result.found

    # then
    assert {c['sig'] for c in found} == {t.sig for t in CARD_TYPES}
    assert all(c['src'] == 'couchbase' and c['ns'] == 'couchbase' and c['ranKey'] == 'couchbase' for c in found)
    assert all(c['key'] == key(c['svc'], c['sig'].removeprefix('couchbase-')) for c in found)
    assert all(len(c['urls']) == 1 and c['urls'][0]['url'].startswith(UI + '/query?') for c in found)
    assert all(c['what'].isascii() and c['short'].isascii() for c in found)


def test_no_up_series_at_all_is_an_error_pass_not_a_card_per_node():
    # given
    data = healthy()
    data[('up', 0)] = []

    # when
    out = run(Ctx(FakeProm(data))).result

    # then
    assert (out.ok, out.status, out.found) == (False, f'error: Prometheus holds no Couchbase up series for {CLUSTER}', [])


def test_a_down_node_is_critical_a_node_gone_since_yesterday_warns_and_both_skip_their_other_rules():
    # given
    data = node_put(healthy(), 'up', 0, N3, 0)
    node_put(data, 'uptime', 0, N3, 10)
    node_put(data, 'up', 1, 'node-4.example.com:8091', 1)

    # when
    sweep = run(Ctx(FakeProm(data)))

    # then
    cards = {c['key']: c for c in sweep.result.found}
    assert {(k, c['sev'], c['label']) for k, c in cards.items()} == {(key(N3, 'down'), 'crit', 'node down'), (key('node-4.example.com:8091', 'down'), 'warn', 'not scraped')}
    assert [r['node'] for r in sweep.sections['couchbase'] if r['part'] == 'node'] == [*NODES, 'node-4.example.com:8091']


@pytest.mark.parametrize('uptime, expect', [(3599, True), (3600, False)])
def test_a_restart_in_the_last_hour_warns(uptime, expect):
    # then
    assert raised(node_put(healthy(), 'uptime', 0, N1, uptime)) == ({(key(N1, 'restart'), 'warn')} if expect else set())


def test_auto_failover_and_a_stuck_rebalance_are_critical_and_a_running_one_is_info():
    # given
    stuck = put(put(put(healthy(), 'failover', 0, 1), 'stuck', 0, 1), 'rebalance', 0, 1)
    running = put(healthy(), 'rebalance', 0, 1)

    # then
    assert raised(stuck) == {(key(CLUSTER, 'failover'), 'crit'), (key(CLUSTER, 'rebalance'), 'crit')}
    assert raised(running) == {(key(CLUSTER, 'rebalance'), 'info')}


@pytest.mark.parametrize('errors, base, expect', [
    (19, None, None), (20, None, None), (22, None, 'warn'), (100, None, 'crit'),
    (40, 20, None), (42, 20, 'warn'),
])
def test_n1ql_errors_use_one_percent_until_a_baseline_exists(errors, base, expect):
    # given
    data = put(healthy(), 'errors', 0, errors)
    if base is not None:
        put(put(data, 'req', 1, 2000), 'errors', 1, base)

    # then
    assert raised(data) == ({(key(CLUSTER, 'n1ql-errors'), expect)} if expect else set())


@pytest.mark.parametrize('timeouts, base, expect', [(4, None, False), (5, None, True), (6, 3, False), (7, 3, True), (7, None, True)])
def test_n1ql_timeouts_need_five_and_twice_the_baseline(timeouts, base, expect):
    # given
    data = put(healthy(), 'timeouts', 0, timeouts)
    if base is not None:
        put(data, 'timeouts', 2, base)

    # then
    assert raised(data) == ({(key(CLUSTER, 'n1ql-timeouts'), 'warn')} if expect else set())


@pytest.mark.parametrize('req, avg_ms, base_ms, expect', [
    (300, 200, None, False), (300, 201, None, True), (299, 500, None, False),
    (300, 200, 100, False), (300, 201, 100, True), (300, 260, 140, False), (300, 281, 140, True),
])
def test_n1ql_latency_uses_200_ms_until_a_baseline_exists(req, avg_ms, base_ms, expect):
    # given
    data = put(put(healthy(), 'req', 0, req), 'rtime', 0, req * avg_ms * 1e6)
    if base_ms is not None:
        put(put(data, 'req', 1, 1000), 'rtime', 1, 1000 * base_ms * 1e6)

    # then
    assert raised(data) == ({(key(CLUSTER, 'n1ql-latency'), 'warn')} if expect else set())


def test_a_long_queue_warns_and_rest_auth_failures_are_info():
    # then
    assert raised(put(healthy(), 'queue', 0, RULES['queue'])) == set()
    assert raised(put(healthy(), 'queue', 0, RULES['queue'] + 1)) == {(key(CLUSTER, 'n1ql-queue'), 'warn')}
    assert raised(put(healthy(), 'auth', 0, RULES['auth'] - 1)) == set()
    assert raised(put(healthy(), 'auth', 0, RULES['auth'])) == {(key(CLUSTER, 'auth-fail'), 'info')}


@pytest.mark.parametrize('used_gib, expect', [(3.5996, False), (3.6, True)])
def test_index_memory_from_90_percent_of_the_quota_is_info_only(used_gib, expect):
    # given
    found = run(Ctx(FakeProm(node_put(healthy(), 'imem', 0, N2, used_gib * GIB)))).result.found

    # then
    assert {(c['key'], c['sev']) for c in found} == ({(key(N2, 'index-memory'), 'info')} if expect else set())
    assert all('since' not in c['what'] and '4.0 GiB' in c['what'] for c in found)


@pytest.mark.parametrize('conn, d1, d7, expect', [
    (1000, None, None, False), (300, 100, None, False), (301, 100, None, True), (450, 300, None, False), (501, 300, None, True), (301, None, 100, True), (301, 100, 300, False),
])
def test_a_connection_jump_needs_a_baseline_of_one_or_seven_days(conn, d1, d7, expect):
    # given
    data = node_put(healthy(), 'conn', 0, N3, conn)
    node_put(node_put(data, 'conn', 1, N3, d1), 'conn', 2, N3, d7)

    # then
    assert raised(data) == ({(key(N3, 'conn-jump'), 'warn')} if expect else set())


def test_the_rules_come_from_the_rule_set_of_the_instance():
    # given
    data = node_put(healthy(), 'uptime', 0, N1, 5000)

    # then
    assert raised(data) == set()
    assert raised(data, overrides={'uptime_s': 7200}) == {(key(N1, 'restart'), 'warn')}
    assert raised(put(healthy(), 'queue', 0, 5), overrides={'queue': 4}) == {(key(CLUSTER, 'n1ql-queue'), 'warn')}


@pytest.mark.parametrize('code, status', [(401, 401), (403, 403)])
def test_a_refused_read_raises_one_access_card_and_keeps_the_other_cards_open(code, status):
    # when
    out = run(Ctx(FakeProm(error=HttpError(code, PROM)))).result

    # then
    [card] = out.found
    assert (out.ok, out.ran, out.status) == (True, ('couchbase:access:metrics',), access.status('ok', ['Couchbase metrics']))
    assert (card['sig'], card['ranKey'], card['sev']) == ('no-access', 'couchbase:access:metrics', 'warn')
    assert card['label'] == access.text('Couchbase', 'Couchbase metrics', credential='', status=status)


def test_no_peer_or_a_failing_prometheus_is_an_error_pass_that_clears_nothing():
    # when
    unset = run(Ctx(FakeProm(healthy()), peered=False)).result
    down = run(Ctx(FakeProm(error=HttpError(503, PROM)))).result

    # then
    assert (unset.ok, unset.status, unset.ran) == (False, 'error: no Prometheus instance is set', ())
    assert (down.ok, down.status, down.ran) == (False, 'error: Prometheus query HTTP 503', ())


@pytest.mark.parametrize('selector', ['job="couchbase"', 'job=~".*couchbase.*"', ' job =~ "a|b" , env!="dev",zone!~"x\\"y" ', 'instance="node-1.example.com:8091"'])
def test_a_selector_of_label_matchers_is_used_in_every_query(selector):
    # given
    prom = FakeProm(healthy(), queries=queries(selector))

    # when
    out = run(Ctx(prom, config={'prometheus': 'metrics', 'cluster': CLUSTER, 'selector': selector})).result

    # then
    assert out.ok and out.status == 'ok'
    assert len(prom.calls) == 3 * len(Q) and all('{' + selector.strip() + '}' in q or '{' + selector.strip() + ',' in q for q in prom.calls)


@pytest.mark.parametrize('selector', ['', '   ', 'job', 'job=couchbase', 'job="a"}', 'job="a"} or up{x="1"', 'job="a', 'job="a",', ',job="a"', 'job="a" env="b"',
                                      'job="a\nb"', 'job="a",\nenv="b"', 'job="a",\tenv="b"', '9job="a"', 'job=="a"', 'x="' + 'y' * 300 + '"'])
def test_a_selector_that_could_break_out_of_the_braces_fails_the_pass_and_the_test_without_a_query(selector):
    # given
    prom = FakeProm(healthy())
    ctx = Ctx(prom, config={'prometheus': 'metrics', 'cluster': CLUSTER, 'selector': selector})

    # when
    out = run(ctx).result
    said = skopax_couchbase.test(ctx)

    # then
    assert (out.ok, out.found, out.ran) == (False, [], ())
    assert out.status == 'error: the selector must be label matchers such as job=~".*couchbase.*"'
    assert (said.ok, said.summary) == (False, 'the selector must be label matchers such as job=~".*couchbase.*"')
    assert prom.calls == []


def test_card_links_are_prometheus_graphs_on_an_https_ui_only():
    # given
    data = put(node_put(healthy(), 'uptime', 0, N1, 60), 'queue', 0, 50)

    # when
    found = run(Ctx(FakeProm(data))).result.found
    plain = run(Ctx(FakeProm(data), ui='http://prometheus.example.com:9090')).result.found
    node = next(c for c in found if c['svc'] == N1)
    cluster = next(c for c in found if c['svc'] == CLUSTER)

    # then
    [link] = links(node, InstanceInfo('orders', True, None, {}))
    assert link['label'] == 'Prometheus graph'
    expr = parse_qs(urlsplit(link['url']).query)['g0.expr'][0]
    assert expr == 'kv_uptime_seconds{' + SELECTOR + ',instance=' + json.dumps(N1) + '}'
    assert 'instance' not in unquote(cluster['urls'][0]['url']) and 'n1ql_queued_requests' in unquote(cluster['urls'][0]['url'])
    assert all(c['urls'] == [] for c in plain) and all(links(c, InstanceInfo('orders', True, None, {})) == [] for c in plain)
    assert links({'src': 'other', 'urls': [{'label': 'x', 'url': 'https://x.example.com'}]}, InstanceInfo('orders', True, None, {})) is None
    assert links({'src': 'couchbase', 'urls': [{'label': 'x', 'url': 'javascript:alert(1)'}, 'x', {'label': 'y', 'url': UI}]}, InstanceInfo('orders', True, None, {})) == [
        {'label': 'y', 'url': UI}]


def test_two_clusters_keep_distinct_card_keys():
    # given
    data = put(node_put(healthy(), 'uptime', 0, N1, 60), 'queue', 0, 50)

    # when
    orders = run(Ctx(FakeProm(data), name='orders')).result.found
    search = run(Ctx(FakeProm(data), name='search')).result.found

    # then
    assert len(orders) == len(search) == 2
    assert {c['key'] for c in orders}.isdisjoint({c['key'] for c in search})
    assert all(c['key'].startswith('couchbase|orders|') for c in orders) and all(c['key'].startswith('couchbase|search|') for c in search)


def test_test_connection_counts_the_nodes_that_are_up():
    # given
    prom = FakeProm(node_put(healthy(), 'up', 0, N3, 0))

    # when
    said = skopax_couchbase.test(Ctx(prom))
    empty = skopax_couchbase.test(Ctx(FakeProm({})))
    unset = skopax_couchbase.test(Ctx(prom, peered=False))
    refused = skopax_couchbase.test(Ctx(FakeProm(error=HttpError(403, PROM))))

    # then
    assert (said.ok, said.summary) == (True, f'2 of 3 Couchbase nodes of {CLUSTER} up through Prometheus metrics')
    assert prom.calls == [Q['up']]
    assert (empty.ok, empty.summary) == (True, f'Prometheus metrics answered, but it holds no Couchbase up series for {CLUSTER}')
    assert (unset.ok, unset.summary) == (False, 'no Prometheus instance is set')
    assert (refused.ok, refused.summary) == (False, access.text('Couchbase', 'Couchbase metrics', credential='', status=403))


def test_the_tool_reads_the_node_table_of_the_last_pass_down_nodes_first():
    # given
    rows = run(Ctx(FakeProm(node_put(healthy(), 'up', 0, N2, 0)))).sections['couchbase']
    ctx = Ctx(rows=[dict(r, plugin='couchbase') for r in rows])

    # when
    out = nodes(ctx, {})
    one = nodes(ctx, {'node': 'node-3'})

    # then
    assert set(out) == {'cluster', 'at', 'nodes', 'summary', 'note'}
    assert (out['cluster'], out['at']) == (CLUSTER, '2026-10-17T08:00:00Z')
    assert [n['node'] for n in out['nodes']] == [N2, N1, N3]
    assert set(out['nodes'][0]) == {'node', 'up', 'uptime', 'conn', 'connD1', 'connD7', 'imem', 'iquota', 'imemPct'}
    assert out['summary']['req'] == 2000 and 'plugin' not in out['summary'] and 'first' not in out['note']
    assert [n['node'] for n in one['nodes']] == [N3]


def test_the_tool_caps_its_rows_and_says_so():
    # given
    many = [{'part': 'node', 'name': CLUSTER, 'node': f'node-{n:02}.example.com:8091', 'up': 1} for n in range(MAX_ROWS + 10)]
    ctx = Ctx(rows=many + [{'part': 'summary', 'name': CLUSTER, 'at': '2026-10-17T08:00:00Z'}])

    # when
    capped = nodes(ctx, {})
    small = nodes(ctx, {'limit': 3})

    # then
    assert MAX_ROWS == 50 and len(capped['nodes']) == MAX_ROWS
    assert capped['note'].startswith(f'first {MAX_ROWS} of {MAX_ROWS + 10} nodes')
    assert [n['node'] for n in small['nodes']] == ['node-00.example.com:8091', 'node-01.example.com:8091', 'node-02.example.com:8091']


@pytest.mark.parametrize('args, rows, status, text', [
    ({}, None, 404, 'no Couchbase pass has written a node table for this instance yet'),
    ({'limit': 0}, [], 400, 'limit must be 1-50'),
    ({'limit': 51}, [], 400, 'limit must be 1-50'),
    ({'limit': 'many'}, [], 400, 'limit must be 1-50'),
])
def test_the_tool_refuses_a_bad_limit_and_says_when_no_pass_ran(args, rows, status, text):
    # when
    with pytest.raises(ReadError) as e:
        nodes(Ctx(rows=rows), args)

    # then
    assert (e.value.status, e.value.message) == (status, text)


def test_the_module_exports_the_plugin():
    # then
    assert skopax_couchbase.PLUGIN is PLUGIN and PLUGIN.id == 'couchbase' and PLUGIN.version == '0.1.0'
