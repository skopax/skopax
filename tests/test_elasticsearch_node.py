import json
from urllib.parse import urlsplit

import pytest

from skopax.contract import ReadError
from skopax.investigator.tools import ToolError
from skopax.messages import msg
from skopax.plugins import elasticsearch
from skopax.plugins.elasticsearch import esql as reads
from skopax.plugintools import PluginTools
from skopax.sweeps import watched
from skopax import remote
from skopax.remote import Reply
from tests.agent_kit import AUTH, agent_boot, agent_client
from tests.fake_http import FakeHttp
from tests.kube_kit import Seen, agent_app, app_transport, kube_runtime

NOW = 1_794_207_600.0
NODE = {'mode': 'agent', 'url': 'https://node.example.com', 'token': 'node-token-0001'}
PROD_URL = 'https://es.example.com'
TEST_URL = 'https://es-test.example.com'
STAGE_URL = 'https://es-stage.example.com'
QUERY = 'FROM logs-* | WHERE @timestamp >= NOW() - 1 hour | STATS n = COUNT(*)'
OWN = {'elastic.logging-url': 'https://es-own.example.com', 'elastic.logging-api-key': 'own-key-0004'}


def rows(n: int = 1) -> dict:
    return {'columns': [{'name': 'n', 'type': 'long'}], 'values': [[n]]}


def node_setup(store, monkeypatch, es: dict | None = None, node: dict | None = None, cluster: FakeHttp | None = None):
    far = agent_app('node-token-0001', plugins=(elasticsearch,))
    cluster = cluster or FakeHttp({('POST', f'{TEST_URL}/_query'): rows(7), ('POST', f'{STAGE_URL}/_query'): rows(9), ('POST', 'https://es-own.example.com/_query'): rows(1)})
    monkeypatch.setattr(reads, 'send', cluster)
    seen = Seen()
    runtime = kube_runtime(store, {'test': node or NODE}, transport=app_transport({'node.example.com': far}, seen), plugins=(elasticsearch.PLUGIN,))
    for name, values in (es if es is not None else {'prod': {'url': PROD_URL, 'api-key': 'prod-key-0001'},
                                                       'test': {'url': TEST_URL, 'api-key': 'test-key-0002', 'via-node': 'test'}}).items():
        runtime.instances.create('elasticsearch', name, values, True, 'alice')
    runtime.refresh()
    return runtime, seen, far, cluster


def pushes(seen: Seen) -> list[dict]:
    return [json.loads(c[4]) for c in seen.calls if c[0] == 'PUT']


def reads_of(seen: Seen) -> list[dict]:
    return [json.loads(c[4]) for c in seen.calls if c[0] == 'POST' and urlsplit(c[1]).path == '/api/read']


def test_a_connection_through_a_node_sends_esql_through_that_node(store, monkeypatch):
    # given
    runtime, seen, far, cluster = node_setup(store, monkeypatch)
    tools = PluginTools(runtime)

    # when
    out = tools.call('esql', {'instance': 'test', 'cluster': 'logs', 'query': QUERY}, NOW)

    # then
    assert out == [{'n': 7}]
    assert reads_of(seen) == [{'op': 'esql', 'args': {'cluster': 'logs', 'query': QUERY}, 'instance': 'test'}]
    assert pushes(seen) == [{'rev': pushes(seen)[0]['rev'], 'instance': 'test', 'op': 'esql',
                             'config': {'elastic.apm-api-key': 'test-key-0002', 'elastic.apm-url': TEST_URL, 'elastic.logging-api-key': 'test-key-0002', 'elastic.logging-url': TEST_URL}}]
    assert {urlsplit(c[1]).hostname for c in seen.calls} == {'node.example.com'}
    assert [(c[1], c[2]['Authorization']) for c in cluster.calls] == [(f'{TEST_URL}/_query', 'ApiKey test-key-0002')]


def test_two_connections_through_one_node_keep_their_own_keys(store, monkeypatch):
    # given
    runtime, seen, far, cluster = node_setup(store, monkeypatch, es={'test': {'url': TEST_URL, 'api-key': 'test-key-0002', 'via-node': 'test'},
                                                                      'stage': {'url': STAGE_URL, 'api-key': 'stage-key-0003', 'via-node': 'test'}},
                                             node=NODE | OWN)
    tools = PluginTools(runtime)

    # when
    first = tools.call('esql', {'instance': 'test', 'query': QUERY}, NOW)
    second = tools.call('esql', {'instance': 'stage', 'query': QUERY}, NOW)
    again = tools.call('esql', {'instance': 'test', 'query': QUERY}, NOW)
    own = runtime.agent_read('test', 'esql', {'cluster': 'logs', 'query': QUERY})

    # then
    assert (first, second, again) == ([{'n': 7}], [{'n': 9}], [{'n': 7}])
    assert [p.get('instance') for p in pushes(seen)] == ['test', 'stage', None]
    assert [(c[1], c[2]['Authorization']) for c in cluster.calls[:3]] == [(f'{TEST_URL}/_query', 'ApiKey test-key-0002'), (f'{STAGE_URL}/_query', 'ApiKey stage-key-0003'),
                                                                          (f'{TEST_URL}/_query', 'ApiKey test-key-0002')]
    assert cluster.calls[3][1] == 'https://es-own.example.com/_query' and cluster.calls[3][2]['Authorization'] == 'ApiKey own-key-0004'
    assert own == [{'n': 1}]


def test_a_wrong_node_name_is_an_instance_problem(store, monkeypatch):
    # given
    runtime, _, _, _ = node_setup(store, monkeypatch, es={'gone': {'url': TEST_URL, 'api-key': 'k-0000001', 'via-node': 'nowhere'},
                                                           'local': {'url': TEST_URL, 'api-key': 'k-0000001', 'via-node': 'inside'},
                                                           'fine': {'url': TEST_URL, 'api-key': 'k-0000001', 'via-node': 'test'}})
    runtime.instances.create('kubernetes', 'inside', {'mode': 'in-cluster'}, True, 'alice')
    runtime.refresh()

    # when
    found = {i.name: i.problem for i in runtime.instances.all('elasticsearch')}

    # then
    assert found['gone'] == msg('instances.via_node', node='nowhere')
    assert found['local'] == msg('instances.via_node', node='inside')
    assert found['fine'] is None
    assert 'nowhere' in found['gone'] and 'Kubernetes agent' in found['gone']


def test_a_node_that_is_switched_off_parks_its_connections(store, monkeypatch):
    # given
    runtime, _, _, _ = node_setup(store, monkeypatch)

    # when
    runtime.instances.set_enabled('kubernetes', 'test', False, 'alice')
    runtime.refresh()

    # then
    assert runtime.instances.get('elasticsearch', 'test').problem == msg('instances.via_node', node='test')
    assert runtime.instances.get('elasticsearch', 'prod').problem is None


def test_a_connection_through_a_node_runs_no_checks(store, monkeypatch):
    # given
    runtime, _, _, _ = node_setup(store, monkeypatch)

    # when
    prod = watched(elasticsearch.PLUGIN, runtime.instances.get('elasticsearch', 'prod'))
    test = watched(elasticsearch.PLUGIN, runtime.instances.get('elasticsearch', 'test'))

    # then
    assert [c.id for c in prod] == ['logs', 'apm', 'logflow']
    assert test == []
    assert elasticsearch.probe_targets(runtime.instances.get('elasticsearch', 'test').info()) == []


def test_test_connection_goes_through_the_node(store, monkeypatch):
    # given
    runtime, seen, _, cluster = node_setup(store, monkeypatch)
    refused = FakeHttp({})
    ctx = runtime.context(elasticsearch.PLUGIN, runtime.instances.get('elasticsearch', 'test'), NOW)

    # when
    ok = elasticsearch.test(ctx)
    monkeypatch.setattr(reads, 'send', refused)
    runtime.instances.update('elasticsearch', 'test', {'url': 'https://es-gone.example.com', 'api-key': 'test-key-0002'}, 'alice')
    runtime.refresh()
    bad = elasticsearch.test(runtime.context(elasticsearch.PLUGIN, runtime.instances.get('elasticsearch', 'test'), NOW))

    # then
    assert ok.ok and ok.summary == msg('elasticsearch.node_test', node='test')
    assert [r['op'] for r in reads_of(seen)][:1] == ['esql'] and reads_of(seen)[0]['instance'] == 'test'
    assert all(urlsplit(c[1]).hostname == 'node.example.com' for c in seen.calls)
    assert cluster.calls and all(c[1] == f'{TEST_URL}/_query' for c in cluster.calls)
    assert not bad.ok and 'HTTP 404' in bad.summary and 'test-key-0002' not in bad.summary


def test_a_connection_through_a_node_without_an_api_key_says_so(store, monkeypatch):
    # given
    runtime, seen, _, _ = node_setup(store, monkeypatch, es={'test': {'url': TEST_URL, 'via-node': 'test'}})

    # when
    with pytest.raises(ToolError) as e:
        PluginTools(runtime).call('esql', {'instance': 'test', 'query': QUERY}, NOW)

    # then
    assert str(e.value) == msg('elasticsearch.node_needs_key', node='test')
    assert seen.calls == []


def test_a_node_named_cluster_gets_a_pointed_error(store, monkeypatch):
    # given
    runtime, seen, _, _ = node_setup(store, monkeypatch)
    tools = PluginTools(runtime)
    schema_refusal = 'cluster must be one of logs, apm, got "test-logs"'

    # when
    with pytest.raises(ToolError) as direct:
        tools.call('esql', {'instance': 'prod', 'cluster': 'test-logs', 'query': QUERY}, NOW)
    checked = tools.refuse_input('esql', {'cluster': 'test-apm', 'query': QUERY}, schema_refusal.replace('test-logs', 'test-apm'))
    plain = tools.refuse_input('esql', {'cluster': 'prod-logz', 'query': QUERY}, schema_refusal.replace('test-logs', 'prod-logz'))

    # then
    assert 'instance: test, cluster: logs' in str(direct.value)
    assert 'instance: test, cluster: apm' in str(checked)
    assert str(plain) == schema_refusal.replace('test-logs', 'prod-logz')
    assert seen.calls == []


def test_the_esql_tool_tells_the_model_that_instance_picks_the_cluster():
    # then
    assert 'instance' in reads.DESCRIPTION and 'cluster stays logs or apm' in reads.DESCRIPTION
    assert 'instance' in elasticsearch.PLUGIN.knowledge[0].text and 'Through agent node' in elasticsearch.PLUGIN.knowledge[0].text


def test_node_errors_keep_the_server_hints(store, monkeypatch):
    # given
    from skopax.remote import HttpError
    from tests.test_elasticsearch_plugin import es_error
    unknown = HttpError(400, f'{TEST_URL}/_query', es_error('verification_exception', 'Found 1 problem\nline 1:40: Unknown column [log]'))
    runtime, _, _, _ = node_setup(store, monkeypatch, cluster=FakeHttp({('POST', f'{TEST_URL}/_query'): unknown}))

    # when
    with pytest.raises(ToolError) as e:
        PluginTools(runtime).call('esql', {'instance': 'test', 'query': 'FROM logs-shop | KEEP log | LIMIT 1'}, NOW)

    # then
    assert 'Unknown column [log]' in str(e.value)
    assert msg('esql.hint.log_column', message='message', fields=msg('esql.hint.fields', level='log.level', service='service.name')) in str(e.value)


def test_the_agent_keeps_pushed_configs_per_instance(monkeypatch):
    # given
    cluster = FakeHttp({('POST', f'{TEST_URL}/_query'): rows(7), ('POST', f'{STAGE_URL}/_query'): rows(9), ('POST', 'https://es-own.example.com/_query'): rows(1)})
    monkeypatch.setattr(reads, 'send', cluster)
    c = agent_client(agent_boot(values=OWN, plugins=(elasticsearch,)))
    body = {'op': 'esql', 'args': {'cluster': 'logs', 'query': QUERY}}

    # when
    c.put('/api/agent/sources', json={'rev': 'a', 'instance': 'test', 'op': 'esql', 'config': {'elastic.logging-url': TEST_URL, 'elastic.logging-api-key': 'test-key-0002'}}, headers=AUTH)
    c.put('/api/agent/sources', json={'rev': 'b', 'instance': 'stage', 'op': 'esql', 'config': {'elastic.logging-url': STAGE_URL, 'elastic.logging-api-key': 'stage-key-0003'}}, headers=AUTH)
    test = c.post('/api/read', json=body | {'instance': 'test'}, headers=AUTH)
    stage = c.post('/api/read', json=body | {'instance': 'stage'}, headers=AUTH)
    own = c.post('/api/read', json=body, headers=AUTH)
    unknown = c.post('/api/read', json=body | {'instance': 'other'}, headers=AUTH)

    # then
    assert (test.json(), stage.json(), own.json()) == ({'data': [{'n': 7}]}, {'data': [{'n': 9}]}, {'data': [{'n': 1}]})
    assert unknown.status_code == 503
    assert [c[1] for c in cluster.calls] == [f'{TEST_URL}/_query', f'{STAGE_URL}/_query', 'https://es-own.example.com/_query']
    assert c.get('/api/agent', headers=AUTH).json()['reads'] == {'esql': ['logs']}


def test_an_old_style_flat_push_still_answers(store, monkeypatch):
    # given
    cluster = FakeHttp({('POST', f'{TEST_URL}/_query'): rows(7)})
    monkeypatch.setattr(reads, 'send', cluster)
    c = agent_client(agent_boot(values=OWN, plugins=(elasticsearch,)))
    flat = {'elastic.logging-url': TEST_URL, 'elastic.logging-api-key': 'test-key-0002'}

    # when
    pushed = c.put('/api/agent/sources', json={'rev': 'r1', 'config': flat}, headers=AUTH)
    plain = c.post('/api/read', json={'op': 'esql', 'args': {'cluster': 'logs', 'query': QUERY}}, headers=AUTH)

    # then
    assert pushed.status_code == 200 and 'instance' not in pushed.json()
    assert plain.json() == {'data': [{'n': 7}]}


def older_agent(seen: Seen):
    held: dict = {}

    def send(method, url, headers, data, timeout):
        seen.calls.append((method, url, timeout, headers, data))
        body = json.loads(data) if data else {}
        if method == 'PUT':
            held.clear()
            held.update(body['config'])
            return Reply({'rev': body['rev'], 'reads': {'esql': reads.agent_sources(held)}}, {})
        return Reply({'data': reads.agent_esql(dict(held), body['args'])}, {})

    return send


def test_a_server_talking_to_an_older_agent_keeps_one_flat_slot(store, monkeypatch):
    # given
    monkeypatch.setattr(reads, 'send', FakeHttp({('POST', f'{TEST_URL}/_query'): rows(7), ('POST', f'{STAGE_URL}/_query'): rows(9)}))
    seen = Seen()
    runtime = kube_runtime(store, {'test': NODE}, transport=older_agent(seen), plugins=(elasticsearch.PLUGIN,))
    runtime.instances.create('elasticsearch', 'test', {'url': TEST_URL, 'api-key': 'test-key-0002', 'via-node': 'test'}, True, 'alice')
    runtime.instances.create('elasticsearch', 'stage', {'url': STAGE_URL, 'api-key': 'stage-key-0003', 'via-node': 'test'}, True, 'alice')
    runtime.refresh()
    tools = PluginTools(runtime)

    # when
    first = tools.call('esql', {'instance': 'test', 'query': QUERY}, NOW)
    second = tools.call('esql', {'instance': 'stage', 'query': QUERY}, NOW)
    again = tools.call('esql', {'instance': 'test', 'query': QUERY}, NOW)
    same = tools.call('esql', {'instance': 'test', 'query': QUERY}, NOW)

    # then
    assert (first, second, again, same) == ([{'n': 7}], [{'n': 9}], [{'n': 7}], [{'n': 7}])
    assert [p['instance'] for p in pushes(seen)] == ['test', 'stage', 'test']


def test_a_node_restart_pushes_the_connection_again(store, monkeypatch):
    # given
    runtime, seen, far, _ = node_setup(store, monkeypatch)
    tools = PluginTools(runtime)
    tools.call('esql', {'instance': 'test', 'query': QUERY}, NOW)

    # when
    far.app.state.core._scoped.clear()
    out = tools.call('esql', {'instance': 'test', 'query': QUERY}, NOW)

    # then
    assert out == [{'n': 7}]
    assert [p['instance'] for p in pushes(seen)] == ['test', 'test']


def test_a_read_error_through_the_node_is_a_read_error(store, monkeypatch):
    # given
    runtime, _, _, _ = node_setup(store, monkeypatch)
    ctx = runtime.context(elasticsearch.PLUGIN, runtime.instances.get('elasticsearch', 'test'), NOW)

    # when
    with pytest.raises(ReadError) as e:
        reads.run_esql(ctx, {'cluster': 'logs', 'query': 'FROM logs-* | STATS n = COUNT(*)'})

    # then
    assert e.value.status == 400 and 'no time bound' in e.value.message


def test_admin_says_why_a_connection_through_a_node_has_no_checks(store):
    # given
    from fastapi.testclient import TestClient
    from skopax.app import create_app
    from tests.conftest import AUTH as ADMIN, settings
    with TestClient(create_app(settings(), store)) as c:
        c.app.state.instances.create('kubernetes', 'test', NODE, True, 'alice@example.com')
        c.app.state.instances.create('elasticsearch', 'test', {'url': TEST_URL, 'api-key': 'test-key-0002', 'via-node': 'test'}, True, 'alice@example.com')
        c.app.state.runtime.refresh()

        # when
        plugin = next(p for p in c.get('/api/admin/plugins', headers=ADMIN).json()['plugins'] if p['id'] == 'elasticsearch')

    # then
    shown = {i['name']: i for i in plugin['health']['instances']}
    labels = ', '.join(check.label for check in elasticsearch.PLUGIN.checks)
    assert (shown['test']['state'], shown['test']['text']) == ('off', msg('integrations.health.through_node', node='test', checks=labels))
    assert 'test-key-0002' not in json.dumps(plugin)


def test_an_unsaved_form_tests_through_the_node(store, monkeypatch):
    # given
    runtime, seen, far, cluster = node_setup(store, monkeypatch, es={})
    form = {'url': TEST_URL, 'api-key': 'test-key-0002', 'via-node': 'test'}

    # when
    out = runtime.connection_test(runtime.registry.get('elasticsearch').plugin, None, form, 15, NOW)
    other = runtime.connection_test(runtime.registry.get('elasticsearch').plugin, None, form, 15, NOW)

    # then
    assert out['ok'] and other['ok'] and out['summary'] == msg('elasticsearch.node_test', node='test')
    slots = [p['instance'] for p in pushes(seen)]
    assert len(slots) == 4 and slots[0] == slots[1] != slots[2] == slots[3] and all(s.startswith('-') and len(s) > 1 for s in slots)
    assert [bool(p['config']) for p in pushes(seen)] == [True, False, True, False]
    assert [r['instance'] for r in reads_of(seen)] == [slots[0], slots[2]]
    assert far.app.state.core._scoped == {} and runtime._pushed == {}
    assert [c[1] for c in cluster.calls] == [f'{TEST_URL}/_query'] * 2


def test_moving_a_connection_to_another_node_asks_for_its_key_again(store, monkeypatch):
    # given
    from skopax.errkind import InvalidInstance
    runtime, _, _, _ = node_setup(store, monkeypatch)
    runtime.instances.create('kubernetes', 'other', NODE, True, 'alice')
    runtime.refresh()

    # when
    with pytest.raises(InvalidInstance) as refused:
        runtime.connection_test(runtime.registry.get('elasticsearch').plugin, 'test', {'via-node': 'other'}, 15, NOW)
    kept = runtime.connection_test(runtime.registry.get('elasticsearch').plugin, 'test', {'log-index': 'logs-shop-*'}, 15, NOW)
    given = runtime.connection_test(runtime.registry.get('elasticsearch').plugin, 'test', {'via-node': 'other', 'api-key': 'test-key-0002'}, 15, NOW)

    # then
    assert refused.value.field == 'api-key'
    assert kept['ok'] and given['ok'] and given['summary'] == msg('elasticsearch.node_test', node='other')


def test_moving_a_connection_to_another_node_clears_it_on_the_old_one(store, monkeypatch):
    # given
    other = agent_app('other-token-0002', plugins=(elasticsearch,))
    runtime, seen, far, _ = node_setup(store, monkeypatch)
    runtime.transport = app_transport({'node.example.com': far, 'other.example.com': other}, seen)
    runtime.instances.create('kubernetes', 'other', {'mode': 'agent', 'url': 'https://other.example.com', 'token': 'other-token-0002'}, True, 'alice')
    runtime.refresh()
    tools = PluginTools(runtime)
    tools.call('esql', {'instance': 'test', 'query': QUERY}, NOW)

    # when
    runtime.instances.update('elasticsearch', 'test', {'via-node': 'other', 'api-key': 'test-key-0002'}, 'alice')
    runtime.refresh()
    out = tools.call('esql', {'instance': 'test', 'query': QUERY}, NOW)

    # then
    assert out == [{'n': 7}]
    sent = [(urlsplit(c[1]).hostname, json.loads(c[4])) for c in seen.calls if c[0] == 'PUT']
    assert [(host, body['instance'], bool(body['config'])) for host, body in sent] == [('node.example.com', 'test', True), ('node.example.com', 'test', False), ('other.example.com', 'test', True)]
    assert far.app.state.core._scoped == {} and list(other.app.state.core._scoped) == [('esql', 'test')]
    assert [k for k in runtime._pushed] == [('other', 'esql', 'test')]


def test_deleting_a_connection_clears_it_on_its_node(store, monkeypatch):
    # given
    runtime, seen, far, _ = node_setup(store, monkeypatch)
    PluginTools(runtime).call('esql', {'instance': 'test', 'query': QUERY}, NOW)

    # when
    runtime.instances.delete('elasticsearch', 'test', 'alice')
    runtime.refresh()
    runtime.refresh()

    # then
    assert [(p['instance'], p['config']) for p in pushes(seen)] == [('test', pushes(seen)[0]['config']), ('test', {})]
    assert far.app.state.core._scoped == {} and runtime._pushed == {}


def test_an_older_agent_never_answers_one_connection_with_anothers_config(store, monkeypatch):
    # given
    import threading
    monkeypatch.setattr(reads, 'send', FakeHttp({('POST', f'{TEST_URL}/_query'): rows(7), ('POST', f'{STAGE_URL}/_query'): rows(9)}))
    seen = Seen()
    held: dict = {}
    entered, gate = threading.Event(), threading.Event()

    def send(method, url, headers, data, timeout):
        seen.calls.append((method, url, timeout, headers, data))
        body = json.loads(data) if data else {}
        if method == 'PUT':
            held.clear()
            held.update(body['config'])
            return Reply({'rev': body['rev'], 'reads': {'esql': reads.agent_sources(held)}}, {})
        if body.get('instance') == 'test':
            entered.set()
            gate.wait(5)
        return Reply({'data': reads.agent_esql(dict(held), body['args'])}, {})

    runtime = kube_runtime(store, {'test': NODE}, transport=send, plugins=(elasticsearch.PLUGIN,))
    runtime.instances.create('elasticsearch', 'test', {'url': TEST_URL, 'api-key': 'test-key-0002', 'via-node': 'test'}, True, 'alice')
    runtime.instances.create('elasticsearch', 'stage', {'url': STAGE_URL, 'api-key': 'stage-key-0003', 'via-node': 'test'}, True, 'alice')
    runtime.refresh()
    tools = PluginTools(runtime)
    got: dict = {}

    # when
    first = threading.Thread(target=lambda: got.update(test=tools.call('esql', {'instance': 'test', 'query': QUERY}, NOW)))
    first.start()
    entered.wait(5)
    second = threading.Thread(target=lambda: got.update(stage=tools.call('esql', {'instance': 'stage', 'query': QUERY}, NOW)))
    second.start()
    second.join(0.3)
    gate.set()
    first.join(5)
    second.join(5)

    # then
    assert got == {'test': [{'n': 7}], 'stage': [{'n': 9}]}
    assert [json.loads(c[4]).get('instance') for c in seen.calls] == ['test', 'test', 'stage', 'stage']


def test_a_cluster_named_after_a_connection_that_does_not_exist_names_the_ones_that_do(store, monkeypatch):
    # given
    runtime, _, _, _ = node_setup(store, monkeypatch)
    tools = PluginTools(runtime)

    # when
    with pytest.raises(ToolError) as direct:
        tools.call('esql', {'instance': 'prod', 'cluster': 'qa-logs', 'query': QUERY}, NOW)
    checked = tools.refuse_input('esql', {'cluster': 'qa-apm', 'query': QUERY}, 'cluster must be one of logs, apm, got "qa-apm"')

    # then
    assert str(direct.value) == msg('plugintools.named_unknown', key='cluster', value='qa-logs', field='instance', name='qa', names='prod, test', choices='logs, apm')
    assert str(checked) == msg('plugintools.named_unknown', key='cluster', value='qa-apm', field='instance', name='qa', names='prod, test', choices='logs, apm')
    assert 'instance: test' not in str(direct.value)


def flat_agent(seen: Seen, gate_on=None, failing: list | None = None):
    import threading
    held: dict = {}
    entered, gate = threading.Event(), threading.Event()

    def send(method, url, headers, data, timeout):
        seen.calls.append((method, url, timeout, headers, data))
        body = json.loads(data) if data else {}
        if method == 'PUT':
            if failing and not body['config']:
                raise failing.pop()
            held.clear()
            held.update(body['config'])
            return Reply({'rev': body['rev'], 'reads': {'esql': reads.agent_sources(held)}}, {})
        if gate_on is not None and gate_on(body):
            entered.set()
            gate.wait(5)
        return Reply({'data': reads.agent_esql(dict(held), body['args'])}, {})

    return send, entered, gate


def flat_runtime(store, monkeypatch, send, node=None):
    monkeypatch.setattr(reads, 'send', FakeHttp({('POST', f'{TEST_URL}/_query'): rows(7), ('POST', 'https://es-own.example.com/_query'): rows(1)}))
    runtime = kube_runtime(store, {'test': node or NODE}, transport=send, plugins=(elasticsearch.PLUGIN,))
    runtime.instances.create('elasticsearch', 'test', {'url': TEST_URL, 'api-key': 'test-key-0002', 'via-node': 'test'}, True, 'alice')
    runtime.refresh()
    return runtime


def test_a_plain_read_and_a_connection_read_on_an_older_agent_never_mix(store, monkeypatch):
    # given
    import threading
    seen = Seen()
    send, entered, gate = flat_agent(seen, gate_on=lambda body: 'instance' not in body)
    runtime = flat_runtime(store, monkeypatch, send, NODE | OWN)
    tools = PluginTools(runtime)
    tools.call('esql', {'instance': 'test', 'query': QUERY}, NOW)
    got: dict = {}

    # when
    plain = threading.Thread(target=lambda: got.update(own=runtime.agent_read('test', 'esql', {'cluster': 'logs', 'query': QUERY})))
    plain.start()
    entered.wait(5)
    scoped = threading.Thread(target=lambda: got.update(test=tools.call('esql', {'instance': 'test', 'query': QUERY}, NOW)))
    scoped.start()
    scoped.join(0.3)
    gate.set()
    plain.join(5)
    scoped.join(5)

    # then
    assert got == {'own': [{'n': 1}], 'test': [{'n': 7}]}


def test_a_slot_that_could_not_be_cleared_is_tried_again(store, monkeypatch):
    # given
    seen = Seen()
    send, _, _ = flat_agent(seen, failing=[remote.Unreachable('https://node.example.com unreachable (timed out)')])
    runtime = flat_runtime(store, monkeypatch, send)
    PluginTools(runtime).call('esql', {'instance': 'test', 'query': QUERY}, NOW)
    runtime.instances.delete('elasticsearch', 'test', 'alice')

    # when
    runtime.refresh()
    kept = dict(runtime._pushed)
    runtime.refresh()

    # then
    assert list(kept.values())[0][3] == 'test'
    assert runtime._pushed == {}
    assert [bool(json.loads(c[4])['config']) for c in seen.calls if c[0] == 'PUT'] == [True, False, False]


def test_the_refresh_does_not_wait_for_a_node_that_is_busy(store, monkeypatch):
    # given
    import threading
    import time
    seen = Seen()
    send, _, _ = flat_agent(seen)
    runtime = flat_runtime(store, monkeypatch, send)
    PluginTools(runtime).call('esql', {'instance': 'test', 'query': QUERY}, NOW)
    runtime.instances.delete('elasticsearch', 'test', 'alice')
    holding, release = threading.Event(), threading.Event()

    def busy():
        with runtime._lock_of('test'):
            holding.set()
            release.wait(5)

    worker = threading.Thread(target=busy)
    worker.start()
    holding.wait(5)

    # when
    began = time.monotonic()
    runtime._forget_stale()
    took = time.monotonic() - began
    release.set()
    worker.join(5)
    runtime._forget_stale()

    # then
    assert took < 3 and runtime._pushed == {}
