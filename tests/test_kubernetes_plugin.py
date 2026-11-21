import json
import ssl
import typing
from dataclasses import replace
from urllib.parse import urlparse

import mongomock
import pytest
import regex
from fastapi.testclient import TestClient

from skopax import agent as agent_module, agentproto, contract, instances as instances_module, models, redact, remote
from skopax.app import create_app
from skopax.agent import AgentError, RemoteAgent, rev_of
from skopax.config import ConfigurationError
from skopax.contract import AgentRead, Capabilities, Field, ReadError, SourceData
from skopax.instances import Instances, InvalidInstance, PluginState, SourceOwned, agent_fields
from skopax.messages import msg
from skopax.investigator import prompt
from skopax.investigator.tools import CORE_TOOLS, TEST_WAITING, ToolError
from skopax.kube import Kube
from skopax.plugintools import PluginTools
from skopax.plugins import kubernetes
from skopax.registry import AGENT_HOST_FIELDS, BUILTIN, Registry, field_view
from skopax.runtime import Runtime
from skopax.sealer import Sealer
from skopax.settingsources import FILE_ENV, boot, light_boot
from tests.agent_kit import FakeKube as AgentKube
from tests.conftest import AUTH, settings
from tests.kube_kit import KEY, MAIN_NOW, FakeKube, Seen, agent_app, app_transport, echoer, kube_runtime
from tests.plugins.base import minimal
from tests.test_investigator_tools import NOW, box

LOCAL = {'mode': 'in-cluster'}
HUB = {'mode': 'agent', 'url': 'https://hub.example.com', 'token': 'hub-token-0001'}
EAST = {'mode': 'agent', 'url': 'https://east.example.com', 'token': 'east-token-0002'}
VIA_HUB = EAST | {'via': 'hub'}
ECHO = {'echo.url': 'https://echo.example.com', 'echo.token': 'echo-secret-0003'}


def refusing(code: int, body: str = ''):
    def send(method, url, headers, data, timeout):
        raise remote.HttpError(code, url, body)

    return send


def unreachable(method, url, headers, data, timeout):
    raise remote.Unreachable(f'{url} unreachable (timed out)')


def asked(store, instances, **kw):
    return PluginTools(kube_runtime(store, instances, **kw))


def error_of(tools, args, name='kube'):
    with pytest.raises(ToolError) as e:
        tools.call(name, args, NOW)
    return str(e.value)


def test_the_plugin_is_builtin_and_the_core_offers_exactly_one_kube_tool(store):
    # given
    registry = Registry.load({}, entry_points=False, core_tools=CORE_TOOLS, core_knowledge=prompt.core_anchors())

    # when
    found = registry.get('kubernetes')
    tools = PluginTools(kube_runtime(store, {'local': LOCAL}, kube=FakeKube()))

    # then
    assert 'skopax.plugins.kubernetes' in BUILTIN
    assert found is not None and found.state == 'ok' and found.plugin is kubernetes.PLUGIN
    assert (found.plugin.id, found.plugin.name, found.plugin.category) == ('kubernetes', 'Kubernetes', 'infrastructure')
    assert 'kube' not in CORE_TOOLS
    assert [d['name'] for d in tools.definitions()].count('kube') == 1
    assert registry.icon('kubernetes').startswith('<svg')


def test_the_plugin_declares_its_fields_and_host_capabilities():
    # given
    fields = {f.key: f for f in kubernetes.PLUGIN.fields}

    # then
    assert list(fields) == ['mode', 'url', 'token', 'via']
    assert fields['mode'].kind == 'choice' and fields['mode'].choices == ('in-cluster', 'agent')
    assert (fields['url'].kind, fields['url'].show_if) == ('url', ('mode', 'agent'))
    assert (fields['token'].kind, fields['token'].show_if) == ('secret', ('mode', 'agent'))
    assert fields['via'].kind == 'text' and fields['via'].show_if == ('mode', 'agent')
    assert kubernetes.PLUGIN.capabilities.hosts == ('@url', 'kubernetes.default.svc') and kubernetes.PLUGIN.capabilities.secrets == ('token',)
    assert kubernetes.PLUGIN.tools[0].instance_input == 'cluster' and [(c.id, c.once) for c in kubernetes.PLUGIN.checks] == [('nodes', True)]
    assert [(p.tab, p.script, p.label, p.group) for p in kubernetes.PLUGIN.panels] == [('nodes', 'panel.js', 'Nodes', 'connect')]
    assert set(AGENT_HOST_FIELDS) == set(fields)


def test_local_instance_appears_in_cluster_only(store, monkeypatch, tmp_path):
    # given
    missing = tmp_path / 'none'
    inside = tmp_path / 'sa'
    inside.mkdir()
    (inside / 'token').write_text('t')
    registry = Registry.of(kubernetes.PLUGIN)

    # when
    monkeypatch.setattr(kubernetes, 'SA_ROOT', missing)
    outside = kubernetes.env_instances({})
    booted_out = boot({}, registry).sources
    monkeypatch.setattr(kubernetes, 'SA_ROOT', inside)
    in_cluster = kubernetes.env_instances({})
    b = boot({}, registry)
    stored = Instances(store.db, registry, lambda: Sealer(KEY), sources=b.source_set().current)
    stored.refresh()
    light = light_boot({}, registry)

    # then
    assert outside == [] and all(not d.instances for d in booted_out)
    assert in_cluster == [{'name': 'local', 'primary': 'true', 'mode': 'in-cluster'}]
    local = stored.get('kubernetes', 'local')
    assert local is not None and local.primary and local.problem is None and local.values['mode'] == 'in-cluster' and local.source == 'environment'
    assert all(not d.instances for d in light.sources)


def test_kube_tool_routes_by_cluster(store):
    # given
    calls = Seen()
    local = FakeKube([{'name': 'local-node'}])

    def send(method, url, headers, data, timeout):
        calls.calls.append((method, url, timeout, headers, data))
        return remote.Reply({'data': [{'name': 'east-node'}]}, {})

    tools = asked(store, {'local': LOCAL, 'east': EAST}, kube=local, transport=send)

    # when
    default = tools.call('kube', {'op': 'nodes'}, NOW)
    east = tools.call('kube', {'op': 'nodes', 'cluster': 'east'}, NOW)
    local_events = tools.call('kube', {'op': 'events', 'namespace': 'store', 'object': 'catalog-service'}, NOW)
    events = tools.call('kube', {'op': 'events', 'namespace': 'store', 'cluster': 'east'}, NOW)
    logs = tools.call('kube', {'op': 'previous_logs', 'namespace': 'store', 'pod': 'p-1', 'tail': 50, 'cluster': 'east'}, NOW)
    wrong = error_of(tools, {'op': 'nodes', 'cluster': 'west'})

    # then
    assert default == local_events == [{'name': 'local-node'}] and east == events == logs == [{'name': 'east-node'}]
    assert local.asked == [('nodes',), ('events', 'store', 'catalog-service', 60, NOW)]
    sent = [(c[0], c[1].removeprefix('https://east.example.com'), json.loads(c[4]), c[2], c[3]['Authorization']) for c in calls.calls]
    assert sent == [('POST', '/api/kube', {'op': 'nodes', 'args': {}}, 30, 'Bearer east-token-0002'),
                    ('POST', '/api/kube', {'op': 'events', 'args': {'namespace': 'store'}}, 30, 'Bearer east-token-0002'),
                    ('POST', '/api/kube', {'op': 'previous_logs', 'args': {'namespace': 'store', 'pod': 'p-1', 'tail': 50}}, 30, 'Bearer east-token-0002')]
    assert wrong.startswith('cluster must be one of local, east. "west" is not configured')


def test_remote_agent_reads_through_a_relay(store):
    # given
    far = AgentKube()
    target = agent_app('east-token-0002', kube=far)
    relay_seen = Seen()
    relay = agent_app('hub-token-0001', relay_hosts='east.example.com', transport=app_transport({'east.example.com': target}, relay_seen))
    main_seen = Seen()
    tools = asked(store, {'hub': HUB, 'east': VIA_HUB}, transport=app_transport({'hub.example.com': relay, 'east.example.com': target}, main_seen))

    # when
    out = tools.call('kube', {'op': 'pods', 'namespace': 'shop', 'cluster': 'east'}, NOW)
    direct = tools.call('kube', {'op': 'nodes', 'cluster': 'hub'}, NOW)

    # then
    assert out == [{'name': 'checkout-service-1'}] and [n['name'] for n in direct] == ['n1', 'n2']
    assert far.calls == [('pods', 'shop', None)]
    assert main_seen.paths() == [('POST', '/api/relay/api/kube'), ('POST', '/api/kube')]
    assert [urlparse(c[1]).hostname for c in main_seen.calls] == ['hub.example.com', 'hub.example.com']
    assert main_seen.calls[0][2] == 40 and main_seen.calls[0][3]['Authorization'] == 'Bearer hub-token-0001'
    assert main_seen.calls[0][3]['X-Relay-To'] == 'https://east.example.com' and main_seen.calls[0][3]['X-Relay-Authorization'] == 'Bearer east-token-0002'
    assert relay_seen.paths() == [('POST', '/api/kube')] and relay_seen.calls[0][3]['Authorization'] == 'Bearer east-token-0002'


def test_agent_read_pushes_config_when_its_rev_changed(store):
    # given
    far = agent_app('east-token-0002', plugins=(echoer(),))
    seen = Seen()
    runtime = kube_runtime(store, {'east': EAST | ECHO}, transport=app_transport({'east.example.com': far}, seen), plugins=(echoer(),))
    config = {'echo.url': 'https://echo.example.com', 'echo.token': 'echo-secret-0003'}

    # when
    first = runtime.agent_read('east', 'echoer-read', {'text': 'hi'})
    again = runtime.agent_read('east', 'echoer-read', {'text': 'again'})
    runtime.instances.update('kubernetes', 'east', {'echo.url': 'https://other.example.com', 'echo.token': ECHO['echo.token']}, 'alice')
    third = runtime.agent_read('east', 'echoer-read', {'text': 'third'})
    runtime.instances.update('kubernetes', 'east', {'echo.url': 'https://other.example.com'}, 'alice')
    fourth = runtime.agent_read('east', 'echoer-read', {'text': 'fourth'})

    # then
    assert first == {'echo': 'hi', 'url': 'https://echo.example.com', 'token': 'echo-secret-0003'}
    assert again['echo'] == 'again' and third['url'] == 'https://other.example.com' and fourth['echo'] == 'fourth'
    assert seen.paths() == [('PUT', '/api/agent/sources'), ('POST', '/api/read'), ('POST', '/api/read'), ('PUT', '/api/agent/sources'), ('POST', '/api/read'), ('POST', '/api/read')]
    pushes = [json.loads(c[4]) for c in seen.calls if c[0] == 'PUT']
    assert pushes[0] == {'rev': rev_of(config), 'config': config}
    assert pushes[1] == {'rev': rev_of({**config, 'echo.url': 'https://other.example.com'}), 'config': {**config, 'echo.url': 'https://other.example.com'}}
    assert far.app.state.core._rev == pushes[1]['rev']


def test_agent_read_sends_nothing_to_push_for_an_instance_without_agent_read_values(store):
    # given
    far = agent_app('east-token-0002', plugins=(echoer(),))
    seen = Seen()
    runtime = kube_runtime(store, {'east': EAST}, transport=app_transport({'east.example.com': far}, seen), plugins=(echoer(),))

    # when
    with pytest.raises(ReadError) as e:
        runtime.agent_read('east', 'echoer-read', {'text': 'hi'})

    # then
    assert seen.paths() == [('POST', '/api/read')]
    assert e.value.status == 503 and 'east agent' in e.value.message


def test_agent_read_honours_the_declared_timeout_and_adds_the_relay_margin(store):
    # given
    slow = echoer(timeout=7)
    target = agent_app('east-token-0002', plugins=(slow,))
    relay = agent_app('hub-token-0001', relay_hosts='east.example.com', transport=app_transport({'east.example.com': target}))
    seen = Seen()
    runtime = kube_runtime(store, {'hub': HUB, 'east': VIA_HUB | ECHO}, transport=app_transport({'hub.example.com': relay}, seen), plugins=(slow,))
    direct = Seen()
    plain = kube_runtime(store, {'east': EAST | ECHO}, transport=app_transport({'east.example.com': target}, direct), plugins=(slow,))

    # when
    runtime.agent_read('east', 'echoer-read', {'text': 'hi'})
    plain.agent_read('east', 'echoer-read', {'text': 'hi'})

    # then
    assert [c[2] for c in seen.calls if urlparse(c[1]).path.endswith('/read')] == [17]
    assert [c[2] for c in direct.calls if c[0] == 'POST'] == [7]
    assert [c[2] for c in direct.calls if c[0] == 'PUT'] == [15]


def test_agent_read_errors_are_read_errors_with_their_status(store):
    # given
    runtime = kube_runtime(store, {'local': LOCAL, 'east': EAST | ECHO, 'dead': EAST | {'url': 'https://dead.example.com'}}, plugins=(echoer(),), transport=app_transport({'east.example.com': agent_app('wrong-token-0009', plugins=(echoer(),))}))

    # when
    with pytest.raises(ReadError) as refused:
        runtime.agent_read('east', 'echoer-read', {'text': 'hi'})
    with pytest.raises(ReadError) as missing:
        runtime.agent_read('nope', 'echoer-read', {})
    with pytest.raises(ReadError) as in_cluster:
        runtime.agent_read('local', 'echoer-read', {})
    with pytest.raises(ReadError) as gone:
        runtime.agent_read('dead', 'echoer-read', {})
    with pytest.raises(ReadError) as unknown_op:
        runtime.agent_read('east', 'other-read', {})

    # then
    assert isinstance(refused.value, AgentError) and (refused.value.status, refused.value.message) == (401, 'the east agent refused Skopax (its token does not match the agent token)')
    assert (missing.value.status, 'nope' in missing.value.message) == (404, True)
    assert in_cluster.value.status == 400 and 'local' in in_cluster.value.message
    assert gone.value.status == 502 and gone.value.message.startswith('the dead agent is unreachable')
    assert unknown_op.value.status == 404 and unknown_op.value.message == 'op must be one of echoer-read'


def test_via_must_name_another_agent_instance(store):
    # given
    runtime = kube_runtime(store, {'hub': HUB, 'local': LOCAL, 'east': VIA_HUB})
    stored = runtime.instances

    def refused(name, **values):
        with pytest.raises(InvalidInstance) as e:
            stored.create('kubernetes', name, EAST | values, True, 'alice')
        return e.value

    # when
    missing = refused('west', via='nowhere')
    itself = refused('west', via='west')
    in_cluster = refused('west', via='local')
    chained = refused('west', via='east')
    fine = stored.create('kubernetes', 'north', EAST | {'via': 'hub'}, True, 'alice')
    with pytest.raises(InvalidInstance) as updated:
        stored.update('kubernetes', 'hub', {'via': 'east'}, 'alice')
    with pytest.raises(InvalidInstance) as self_update:
        stored.update('kubernetes', 'north', {'via': 'north'}, 'alice')

    # then
    assert [e.field for e in (missing, itself, in_cluster, chained, updated.value, self_update.value)] == ['via'] * 6
    assert 'another agent instance' in str(missing) and str(missing) == str(in_cluster) and str(missing) == str(itself)
    assert 'itself' not in str(chained) and 'relay' in str(chained)
    assert fine.problem is None and stored.get('kubernetes', 'hub').values.get('via') is None


def test_an_instance_whose_relay_goes_away_is_parked_with_the_reason(store):
    # given
    runtime = kube_runtime(store, {'hub': HUB, 'east': VIA_HUB})

    # when
    runtime.instances.delete('kubernetes', 'hub', 'alice')
    parked = runtime.instances.get('kubernetes', 'east')

    # then
    assert parked.problem and 'another agent instance' in parked.problem
    assert [i.name for i in runtime.instances_of('kubernetes')] == []


def test_agent_error_texts_name_the_instance(store):
    # given
    def tool_text(instances, transport, cluster, name='kube'):
        return error_of(asked(store, instances, transport=transport), {'op': 'nodes', 'cluster': cluster}, name)

    body = json.dumps({'error': 'unauthorized'})
    relay_body = json.dumps({'error': 'unauthorized', 'relay': True})
    relay_down = json.dumps({'error': 'the relay agent could not reach east.example.com: timed out', 'relay': True})

    # when
    refused = tool_text({'east': EAST}, refusing(401, body), 'east')
    gone = tool_text({'east': EAST}, unreachable, 'east')
    answered = tool_text({'east': EAST}, refusing(500, 'oops'), 'east')
    via_refused = tool_text({'hub': HUB, 'east': VIA_HUB}, refusing(401, relay_body), 'east')
    via_failed = tool_text({'hub': HUB, 'east': VIA_HUB}, refusing(502, relay_down), 'east')
    via_gone = tool_text({'hub': HUB, 'east': VIA_HUB}, unreachable, 'east')
    off = asked(store, {'hub': HUB, 'east': VIA_HUB}, transport=unreachable)
    off.runtime.instances.set_enabled('kubernetes', 'hub', False, 'alice')
    switched_off = error_of(off, {'op': 'nodes', 'cluster': 'east'})

    # then
    assert refused == 'the east agent refused Skopax (its token does not match the agent token)'
    assert gone == 'the east agent is unreachable'
    assert answered == 'the east agent answered HTTP 500'
    assert via_refused == 'the hub agent, which relays to the east agent, refused Skopax (its own token does not match)'
    assert via_failed.startswith('the hub agent could not relay to the east agent: ')
    assert via_gone == 'the east agent is unreachable through the hub agent'
    assert switched_off == 'the east agent is reached through the hub agent, which is switched off or not set up'


def test_agent_errors_are_read_errors_with_a_default_status():
    # when
    plain = AgentError('the east agent is unreachable: timed out')
    forbidden = AgentError('pods is forbidden', 403)
    odd = [AgentError('x', code) for code in (302, 0, 'denied', 99, 600)]

    # then
    assert isinstance(plain, ReadError) and (plain.status, plain.message, str(plain), plain.code) == (502, 'the east agent is unreachable: timed out', 'the east agent is unreachable: timed out', None)
    assert (forbidden.status, forbidden.code) == (403, 403)
    assert [e.status for e in odd] == [502] * 5


def test_a_test_agent_that_is_down_reads_as_the_unreachable_test_environment(store):
    # given
    tools = asked(store, {'test': EAST}, transport=unreachable)
    other = asked(store, {'east': EAST}, transport=unreachable)

    # when
    text = error_of(tools, {'op': 'nodes'})
    unknown = error_of(tools, {'op': 'nodes', 'cluster': 'test-eu'})
    plain = error_of(other, {'op': 'nodes'})

    # then
    assert text.startswith(TEST_WAITING.split('{why}')[0]) and 'the test agent is unreachable' in text
    assert unknown.startswith(TEST_WAITING.split('{why}')[0]) and 'cluster must be one of' in unknown
    assert plain.startswith('the east agent is unreachable') and 'test environment' not in plain


def test_a_remote_error_text_is_redacted_and_a_timing_out_rule_gives_the_marker_without_breaking_the_tool(store, monkeypatch):
    # given
    secret = json.dumps({'error': 'pods is forbidden for bob@example.com', 'code': 403})
    tools = asked(store, {'east': EAST}, transport=refusing(400, secret))

    # when
    text = error_of(tools, {'op': 'pods', 'namespace': 'shop', 'cluster': 'east'})
    hostile = json.dumps({'error': 'a' * 60 + '!', 'code': 403})
    monkeypatch.setattr(redact, 'RULES', [(('slow', regex.compile(r'(a|aa)+$')),), None])
    slow = error_of(asked(store, {'east': EAST}, transport=refusing(400, hostile)), {'op': 'nodes', 'cluster': 'east'})

    # then
    assert text == 'pods is forbidden for [redacted]'
    assert slow == redact.TIMEOUT_MARK


def test_the_test_button_names_the_release_and_the_node_count(store):
    # given
    far = agent_app('east-token-0002', kube=AgentKube())
    runtime = kube_runtime(store, {'local': LOCAL, 'east': EAST, 'dead': EAST | {'url': 'https://dead.example.com'}}, kube=FakeKube([{'name': 'n1'}, {'name': 'n2'}, {'name': 'n3'}]),
                           transport=app_transport({'east.example.com': far}))
    plugin = runtime.registry.get('kubernetes').plugin

    def run(name):
        return plugin.test(runtime.context(plugin, runtime.instances.get('kubernetes', name), MAIN_NOW))

    # when
    local, east, dead = run('local'), run('east'), run('dead')
    off = FakeKube()
    off.enabled = False
    runtime.kube = off
    closed = run('local')

    # then
    assert (local.ok, local.summary) == (True, 'Cluster answered, 3 nodes')
    assert (east.ok, east.summary) == (True, 'Agent release 1.0, 2 nodes')
    assert dead.ok is False and dead.summary.startswith('the dead agent is unreachable') and local.ms >= 0
    assert closed.ok is False and 'not running in a cluster' in closed.summary


def test_the_kubernetes_section_moved_from_the_core_prompt_to_the_plugin_knowledge(store):
    # given
    idle = kube_runtime(store, {})
    serving = kube_runtime(store, {'local': LOCAL}, kube=FakeKube())
    note = kubernetes.PLUGIN.knowledge[0]

    # when
    core = prompt.system_prompt()
    with_plugin = prompt.system_prompt(serving.knowledge())
    slack = prompt.slack_system_prompt(serving.knowledge())
    without = prompt.slack_system_prompt(idle.knowledge())

    # then
    assert 'kubernetes' not in prompt.core_anchors() and 'kubernetes' not in prompt.SLACK_SECTIONS
    assert (note.id, note.title, note.slack) == ('kubernetes', 'Kubernetes', True) and len(note.text) < 8000 and note.text.isascii()
    assert '## Kubernetes' not in core and '## Kubernetes' in with_plugin and '## Kubernetes' in slack and '## Kubernetes' not in without
    assert 'OOMKilled' in with_plugin and 'OOMKilled' not in core
    assert 'self_review' in core and 'self_review' in without and '{product}' not in core


def test_fields_of_lists_each_plugins_own_fields_and_kubernetes_also_every_agent_read_field(store):
    # given
    own = replace(echoer(), fields=(Field('note', 'Note', 'text'),))
    runtime = kube_runtime(store, {}, plugins=(own,))

    # when
    mine = runtime.fields_of('echoer')
    kube = runtime.fields_of('kubernetes')
    broken = Registry.of(replace(own, id='Bad Id'))
    gone = Runtime(broken, PluginState(store.db), Instances(store.db, broken, lambda: Sealer(KEY)))

    # then
    assert [f['key'] for f in mine] == ['note'] and mine[0] == field_view(own.fields[0])
    assert [f['key'] for f in kube] == ['mode', 'url', 'token', 'via', 'echo.url', 'echo.token']
    assert [(f['required'], f['showIf']) for f in kube[4:]] == [(False, ['mode', 'agent'])] * 2
    assert runtime.fields_of('nope') == [] and gone.fields_of('Bad Id') == []
    assert [f.key for f in runtime.agent_fields('kubernetes')] == ['echo.url', 'echo.token'] and runtime.agent_fields('echoer') == ()


def test_the_unused_agent_models_are_gone():
    # then
    assert not any(hasattr(models, name) for name in ('AgentIn', 'AgentSourceIn', 'AdminSecretIn'))


def test_the_app_offers_kube_and_its_prompt_section_once_the_environment_defines_the_local_instance(store, monkeypatch, tmp_path):
    # given
    (tmp_path / 'token').write_text('t')
    monkeypatch.setattr(kubernetes, 'SA_ROOT', tmp_path)
    b = boot({})
    client = FakeKube([{'name': 'n1'}])
    idle = TestClient(create_app(settings(), store, kube=client))

    # when
    with TestClient(create_app(b.settings, store, registry=b.registry, sources=b.source_set(), kube=client)) as c:
        tools = c.app.state.investigator.sources.plugin_tools
        offered = [d['name'] for d in tools.definitions()]
        answer = tools.call('kube', {'op': 'nodes'}, NOW)
        knowledge = [k.id for k in tools.knowledge()]

    # then
    assert offered.count('kube') == 1 and answer == [{'name': 'n1'}] and client.asked == [('nodes',)]
    assert 'kubernetes' in knowledge
    assert 'kube' not in [d['name'] for d in idle.app.state.investigator.sources.plugin_tools.definitions()]


def test_only_the_kubernetes_plugin_reaches_agents_and_the_cluster_directly(store):
    # given
    runtime = kube_runtime(store, {'east': EAST}, kube=FakeKube(), plugins=(echoer(),))
    other = runtime.registry.get('echoer').plugin
    host = runtime.registry.get('kubernetes').plugin

    # when
    ctx = runtime.context(other, runtime.instances.get('kubernetes', 'east'), NOW)
    with pytest.raises(ReadError) as agent:
        ctx.agent()
    with pytest.raises(ReadError) as cluster:
        ctx.local_kube()
    allowed = runtime.context(host, runtime.instances.get('kubernetes', 'east'), NOW)
    with pytest.raises(ReadError) as bare:
        runtime.context(host, None, NOW).agent()

    # then
    assert (agent.value.status, cluster.value.status) == (403, 403) and agent.value.message == cluster.value.message
    assert (bare.value.status, bare.value.message) == (403, agent.value.message)
    assert allowed.agent().label == 'east' and allowed.local_kube() is runtime.kube
    assert not hasattr(contract.Context, 'agent') and not hasattr(contract.Context, 'local_kube')


def test_the_test_button_shows_fixed_text_for_a_cluster_failure_never_the_error_text(store):
    # given
    secret = 'GET /api/v1/nodes with Bearer abcdefgh12345678 failed'
    forbidden = kube_runtime(store, {'local': LOCAL}, kube=FakeKube(fail=secret, code=403))
    broken = kube_runtime(store, {'local': LOCAL}, kube=FakeKube(fail=secret))
    odd = kube_runtime(store, {'local': LOCAL}, kube=FakeKube(fail=secret))
    odd.kube.nodes = lambda: (_ for _ in ()).throw(RuntimeError(secret))

    # when
    results = [r.registry.get('kubernetes').plugin.test(r.context(r.registry.get('kubernetes').plugin, r.instances.get('kubernetes', 'local'), MAIN_NOW)) for r in (forbidden, broken, odd)]

    # then
    assert [(r.ok, r.summary) for r in results] == [(False, 'Kubernetes cannot read nodes of local: the token lacks list on nodes'), (False, msg('connection.unreachable')),
                                                    (False, msg('connection.failed', error='RuntimeError'))]
    assert all(r.ms >= 0 for r in results)


@pytest.mark.parametrize('raised, shown', [(lambda: ssl.SSLError('bad certificate'), 'connection.tls'), (lambda: TimeoutError('timed out'), 'connection.timeout')], ids=['tls', 'timeout'])
def test_the_test_button_reads_a_cluster_tls_or_timeout_failure_through_the_kube_error(store, tmp_path, raised, shown):
    # given
    (tmp_path / 'token').write_text('t')

    def transport(url, headers, timeout):
        raise raised()

    runtime = kube_runtime(store, {'local': LOCAL}, kube=Kube(tmp_path, 'https://kubernetes.example.com', transport))
    plugin = runtime.registry.get('kubernetes').plugin

    # when
    result = plugin.test(runtime.context(plugin, runtime.instances.get('kubernetes', 'local'), MAIN_NOW))

    # then
    assert (result.ok, result.summary) == (False, msg(shown))


def test_a_plugin_without_checks_still_gives_a_tile_with_its_instances(store, monkeypatch, tmp_path):
    # given
    (tmp_path / 'token').write_text('t')
    monkeypatch.setattr(kubernetes, 'SA_ROOT', tmp_path)
    bare = replace(kubernetes.PLUGIN, checks=(), panels=())
    tiles = {}

    # when
    for name, b in (('bare', boot({}, Registry.of(bare))), ('builtin', boot({}))):
        with TestClient(create_app(replace(b.settings, token=AUTH['Authorization'].split()[1]), store, registry=b.registry, sources=b.source_set(), kube=FakeKube([{'name': 'n1'}]))) as c:
            tiles[name] = {t['id']: t for t in c.get('/api/server', headers=AUTH).json()['plugins']}['kubernetes']

    # then
    assert tiles['bare']['passes'] == [] and tiles['bare']['tab'] is None
    assert tiles['bare']['instances'] == [{'name': 'local', 'primary': True, 'pass': None, 'status': None, 'state': 'none', 'text': msg('integrations.health.no_checks')}]
    assert tiles['builtin']['passes'] == ['nodes'] and tiles['builtin']['tab'] == 'nodes' and tiles['builtin']['instances'][0]['pass'] == 'nodes'


def test_the_test_button_contacts_the_form_not_the_stored_instance(store):
    # given
    seen = Seen()
    east = agent_app('east-token-0002', kube=AgentKube())
    other = agent_app('other-token-0004', kube=AgentKube())
    runtime = kube_runtime(store, {'east': EAST}, transport=app_transport({'east.example.com': east, 'other.example.com': other}, seen))
    plugin = runtime.registry.get('kubernetes').plugin
    runtime.instances.set_enabled('kubernetes', 'east', False, 'alice')
    form = {'mode': 'agent', 'url': 'https://other.example.com', 'token': 'other-token-0004'}

    # when
    unsaved = runtime.connection_test(plugin, None, form, 5, MAIN_NOW)
    moved = runtime.connection_test(plugin, 'east', form, 5, MAIN_NOW)
    disabled = runtime.connection_test(plugin, 'east', {}, 5, MAIN_NOW)

    # then
    assert (unsaved['ok'], moved['ok'], disabled['ok']) == (True, True, True)
    assert [urlparse(c[1]).hostname for c in seen.calls] == ['other.example.com', 'other.example.com', 'east.example.com']
    assert unsaved['summary'] == 'Agent release 1.0, 2 nodes'


def test_the_test_button_resolves_only_the_relay_from_the_store(store):
    # given
    target = agent_app('east-token-0002', kube=AgentKube())
    hub = agent_app('hub-token-0001', relay_hosts='east.example.com', transport=app_transport({'east.example.com': target}))
    seen = Seen()
    runtime = kube_runtime(store, {'hub': HUB}, transport=app_transport({'hub.example.com': hub}, seen))
    runtime.instances.set_enabled('kubernetes', 'hub', False, 'alice')
    plugin = runtime.registry.get('kubernetes').plugin

    # when
    through = runtime.connection_test(plugin, None, VIA_HUB, 5, MAIN_NOW)
    lost = runtime.connection_test(plugin, None, EAST | {'via': 'gone'}, 5, MAIN_NOW)

    # then
    assert through['ok'] is True and urlparse(seen.calls[0][1]).path == '/api/relay/api/agent'
    assert lost['ok'] is False and 'gone' in lost['summary']


def required_reader():
    read = AgentRead('logger-read', ('q',), (r'logs\.url',), lambda c: ['logs'], lambda c, a: {}, fields=(Field('logs.url', 'Logs URL', 'url', required=True),))
    return minimal('logger', 'Logger', capabilities=Capabilities(agent_reads=True), agent_reads=(read,))


def test_a_required_agent_read_field_of_another_plugin_never_stops_a_cluster_or_an_agent(store, monkeypatch, tmp_path):
    # given
    (tmp_path / 'token').write_text('t')
    monkeypatch.setattr(kubernetes, 'SA_ROOT', tmp_path)
    registry = Registry.of(kubernetes.PLUGIN, required_reader())

    # when
    booted = boot({}, registry)
    stored = Instances(store.db, registry, lambda: Sealer(KEY), sources=booted.source_set().current, extra_fields=lambda plugin_id: agent_fields(registry, plugin_id))
    stored.refresh()
    plain = stored.create('kubernetes', 'east', EAST, True, 'alice')
    full = stored.create('kubernetes', 'west', EAST | {'logs.url': 'https://logs.example.com'}, True, 'alice')
    shown = [(f.key, f.required, f.show_if) for f in agent_fields(registry, 'kubernetes')]

    # then
    assert stored.get('kubernetes', 'local').problem is None
    assert plain.problem is None and full.values['logs.url'] == 'https://logs.example.com'
    assert shown == [('logs.url', False, ('mode', 'agent'))]


def test_a_test_instance_is_guarded_on_the_path_the_model_uses(store):
    # given
    live = asked(store, {'local': LOCAL, 'test': EAST}, kube=FakeKube(), transport=unreachable)
    parked = asked(store, {'local': LOCAL, 'test': EAST}, kube=FakeKube(), transport=unreachable)
    parked.runtime.instances.set_enabled('kubernetes', 'test', False, 'alice')
    missing = asked(store, {'local': LOCAL}, kube=FakeKube())

    # when
    texts = [box(store, plugin_tools=t).call('kube', {'op': 'nodes', 'cluster': 'test'}) for t in (live, parked, missing)]
    eu = box(store, plugin_tools=missing).call('kube', {'op': 'nodes', 'cluster': 'test-eu'})
    plain = box(store, plugin_tools=missing).call('kube', {'op': 'nodes', 'cluster': 'west'})

    # then
    assert all(err and text.startswith(TEST_WAITING.split('{why}')[0]) for text, err in texts + [eu])
    assert plain[1] and plain[0].startswith('cluster must be one of local, got "west". "west" is not configured')


def test_a_read_refused_with_503_pushes_the_config_again_once_and_retries(store):
    # given
    far = agent_app('east-token-0002', plugins=(echoer(),))
    seen = Seen()
    runtime = kube_runtime(store, {'east': EAST | ECHO}, transport=app_transport({'east.example.com': far}, seen), plugins=(echoer(),))
    first = runtime.agent_read('east', 'echoer-read', {'text': 'one'})
    far.app.state.core.push('', {})
    seen.calls.clear()

    # when
    again = runtime.agent_read('east', 'echoer-read', {'text': 'two'})
    second = [c[0] for c in seen.calls]

    # then
    assert first['echo'] == 'one' and again['echo'] == 'two'
    assert second == ['POST', 'PUT', 'POST']


def test_a_503_without_any_config_is_not_retried(store):
    # given
    far = agent_app('east-token-0002', plugins=(echoer(),))
    seen = Seen()
    runtime = kube_runtime(store, {'east': EAST}, transport=app_transport({'east.example.com': far}, seen), plugins=(echoer(),))

    # when
    with pytest.raises(ReadError) as e:
        runtime.agent_read('east', 'echoer-read', {'text': 'one'})

    # then
    assert e.value.status == 503 and [c[0] for c in seen.calls] == ['POST']


def test_a_push_follows_a_change_of_the_address_or_the_relay_alone(store):
    # given
    one = agent_app('east-token-0002', plugins=(echoer(),))
    two = agent_app('east-token-0002', plugins=(echoer(),))
    hub_a = agent_app('hub-token-0001', relay_hosts='east.example.com', transport=app_transport({'east.example.com': one}))
    hub_b = agent_app('hubb-token-0005', relay_hosts='east.example.com', transport=app_transport({'east.example.com': one}))
    seen = Seen()
    routes = {'east.example.com': one, 'new.example.com': two, 'hub.example.com': hub_a, 'hubb.example.com': hub_b}
    runtime = kube_runtime(store, {'east': EAST | ECHO}, transport=app_transport(routes, seen), plugins=(echoer(),))
    puts = lambda: [urlparse(c[1]).hostname for c in seen.calls if c[0] == 'PUT']

    # when
    runtime.agent_read('east', 'echoer-read', {'text': 'a'})
    runtime.agent_read('east', 'echoer-read', {'text': 'b'})
    runtime.instances.update('kubernetes', 'east', {'url': 'https://new.example.com', 'token': EAST['token'], 'echo.token': ECHO['echo.token']}, 'alice')
    runtime.agent_read('east', 'echoer-read', {'text': 'c'})
    runtime.instances.create('kubernetes', 'hub', HUB, True, 'alice')
    runtime.instances.create('kubernetes', 'hubb', HUB | {'url': 'https://hubb.example.com', 'token': 'hubb-token-0005'}, True, 'alice')
    runtime.instances.update('kubernetes', 'east', {'url': 'https://east.example.com', 'via': 'hub', 'token': EAST['token'], 'echo.token': ECHO['echo.token']}, 'alice')
    seen.calls.clear()
    runtime.agent_read('east', 'echoer-read', {'text': 'd'})
    runtime.agent_read('east', 'echoer-read', {'text': 'e'})
    runtime.instances.update('kubernetes', 'east', {'via': 'hubb', 'token': EAST['token'], 'echo.token': ECHO['echo.token']}, 'alice')
    runtime.agent_read('east', 'echoer-read', {'text': 'f'})

    # then
    assert puts() == ['hub.example.com', 'hubb.example.com']
    assert [urlparse(c[1]).hostname for c in seen.calls if c[0] == 'POST'] == ['hub.example.com', 'hub.example.com', 'hubb.example.com']


def test_the_first_push_after_a_url_change_goes_to_the_new_address(store):
    # given
    one = agent_app('east-token-0002', plugins=(echoer(),))
    two = agent_app('east-token-0002', plugins=(echoer(),))
    seen = Seen()
    runtime = kube_runtime(store, {'east': EAST | ECHO}, transport=app_transport({'east.example.com': one, 'new.example.com': two}, seen), plugins=(echoer(),))

    # when
    runtime.agent_read('east', 'echoer-read', {'text': 'a'})
    runtime.instances.update('kubernetes', 'east', {'url': 'https://new.example.com', 'token': EAST['token'], 'echo.token': ECHO['echo.token']}, 'alice')
    runtime.agent_read('east', 'echoer-read', {'text': 'b'})

    # then
    assert [(c[0], urlparse(c[1]).hostname) for c in seen.calls] == [('PUT', 'east.example.com'), ('POST', 'east.example.com'), ('PUT', 'new.example.com'), ('POST', 'new.example.com')]


def test_agent_read_values_and_counts_the_agent_always_refuses_are_refused_by_name(store):
    # given
    runtime = kube_runtime(store, {}, plugins=(echoer(),))
    many = tuple(Field(f'many.k{i}', f'Many {i}', 'text') for i in range(61))
    wide = Instances(runtime.instances.db, runtime.registry, lambda: Sealer(KEY), extra_fields=lambda plugin_id: many if plugin_id == 'kubernetes' else ())

    # when
    with pytest.raises(InvalidInstance) as long_value:
        runtime.instances.create('kubernetes', 'east', EAST | {'echo.url': 'https://example.com/' + 'a' * 2000}, True, 'alice')
    with pytest.raises(InvalidInstance) as long_secret:
        runtime.instances.create('kubernetes', 'east', EAST | {'echo.token': 'a' * 2001}, True, 'alice')
    fits = runtime.instances.create('kubernetes', 'east', EAST | {'echo.token': 'a' * 2000}, True, 'alice')
    with pytest.raises(InvalidInstance) as count:
        wide.create('kubernetes', 'west', EAST | {f'many.k{i}': 'v' for i in range(61)}, True, 'alice')
    at_limit = wide.create('kubernetes', 'north', EAST | {f'many.k{i}': 'v' for i in range(60)}, True, 'alice')

    # then
    assert (long_value.value.field, long_secret.value.field) == ('echo.url', 'echo.token')
    assert count.value.field.startswith('many.k')
    assert fits.problem is None and at_limit.problem is None and 'at most 60' in str(count.value) and '2000' in str(long_secret.value)


def test_an_agent_read_says_why_the_agent_cannot_be_read(store):
    # given
    off = kube_runtime(store, {'east': EAST | ECHO}, plugins=(echoer(),))
    off.state.set('kubernetes', False, 'alice')
    disabled = kube_runtime(store, {'east': EAST | ECHO}, plugins=(echoer(),))
    disabled.instances.set_enabled('kubernetes', 'east', False, 'alice')
    parked = kube_runtime(store, {'east': EAST | ECHO}, plugins=(echoer(),))
    parked.instances.col.update_one({'_id': 'kubernetes/east'}, {'$set': {'values.url': 'not a url'}})
    parked.refresh()

    # when
    errors = []
    for runtime in (off, disabled, parked):
        with pytest.raises(ReadError) as e:
            runtime.agent_read('east', 'echoer-read', {'text': 'hi'})
        errors.append((e.value.status, e.value.message))

    # then
    assert errors == [(503, msg('agent.plugin_off', label='east')), (503, msg('agent.disabled', label='east')), (503, msg('agent.parked', label='east'))]


@pytest.mark.parametrize('declared, used', [(0, 1), (500, 75)])
def test_an_agent_read_timeout_is_kept_between_one_and_seventy_five_seconds(store, declared, used):
    # given
    odd = echoer()
    odd = replace(odd, agent_reads=(replace(odd.agent_reads[0], timeout=declared),))
    target = agent_app('east-token-0002', plugins=(odd,))
    seen = Seen()
    runtime = kube_runtime(store, {'east': EAST | ECHO}, transport=app_transport({'east.example.com': target}, seen), plugins=(odd,))

    # when
    runtime.agent_read('east', 'echoer-read', {'text': 'hi'})

    # then
    assert [c[2] for c in seen.calls if c[0] == 'POST'] == [used]


def test_a_switched_off_plugin_has_its_agent_read_values_kept_from_the_push(store):
    # given
    seen = Seen()
    target = agent_app('east-token-0002', plugins=(echoer(), required_reader()))
    runtime = kube_runtime(store, {'east': EAST | ECHO | {'logs.url': 'https://logs.example.com'}}, transport=app_transport({'east.example.com': target}, seen), plugins=(echoer(), required_reader()))
    runtime.state.set('logger', False, 'alice')

    # when
    runtime.agent_read('east', 'echoer-read', {'text': 'hi'})

    # then
    pushed = [json.loads(c[4]) for c in seen.calls if c[0] == 'PUT']
    assert pushed and set(pushed[0]['config']) == {'echo.url', 'echo.token'}


def test_a_long_remote_error_is_cut_to_the_detail_limit():
    # given
    remote_agent = RemoteAgent(lambda *a: (_ for _ in ()).throw(remote.HttpError(500, 'https://east.example.com/api/read', json.dumps({'error': 'x' * 1000}))), 'east')

    # when
    with pytest.raises(AgentError) as e:
        remote_agent.read('echoer-read', {})

    # then
    assert e.value.message == msg('agent.answered_with', label='east', code=500, detail='x' * 300)


def test_a_relay_hub_cannot_take_a_relay_itself(store):
    # given
    runtime = kube_runtime(store, {'hub': HUB, 'east': VIA_HUB, 'backup': EAST | {'url': 'https://backup.example.com'}})

    # when
    with pytest.raises(InvalidInstance) as e:
        runtime.instances.update('kubernetes', 'hub', {'via': 'backup'}, 'alice')

    # then
    assert (e.value.field, e.value.args[0]) == ('via', msg('instances.invalid.via_relays', other='east'))


@pytest.mark.parametrize('instances, problem', [
    ([{'name': 'east', 'mode': 'agent', 'url': 'https://east.example.com', 'token': 't', 'via': 'east'}], 'instances.invalid.via'),
    ([{'name': 'hub', 'mode': 'agent', 'url': 'https://hub.example.com', 'token': 't', 'via': 'far'},
      {'name': 'far', 'mode': 'agent', 'url': 'https://far.example.com', 'token': 't'},
      {'name': 'east', 'mode': 'agent', 'url': 'https://east.example.com', 'token': 't', 'via': 'hub'}], 'instances.invalid.via_relays'),
], ids=['itself', 'chain'])
def test_a_bad_relay_in_a_settings_file_stops_the_boot(tmp_path, instances, problem):
    # given
    path = tmp_path / 'skopax.yaml'
    path.write_text(json.dumps({'plugins': {'kubernetes': {'instances': instances}}}), encoding='utf-8')

    # when
    with pytest.raises(ConfigurationError) as e:
        boot({FILE_ENV: str(path)}, Registry.of(kubernetes.PLUGIN))

    # then
    assert 'field via' in str(e.value) and msg(problem, other='east') in str(e.value)


def test_the_core_prompt_no_longer_steers_to_kube():
    # then
    assert '`kube`' not in prompt.system_prompt() and '`kube`' not in prompt.slack_system_prompt()


def test_values_of_a_removed_plugin_can_be_cleared(store):
    # given
    runtime = kube_runtime(store, {'east': EAST | ECHO}, plugins=(echoer(),))
    db = runtime.instances.col.database
    bare = Registry.of(kubernetes.PLUGIN)
    stored = Instances(db, bare, lambda: Sealer(KEY))
    stored.refresh()

    # when
    with pytest.raises(InvalidInstance):
        stored.update('kubernetes', 'east', {'echo.url': 'https://echo-two.example.com'}, 'alice')
    stored.update('kubernetes', 'east', {'echo.url': '', 'echo.token': ''}, 'alice')

    # then
    doc = db['instances'].find_one({'_id': 'kubernetes/east'})
    assert 'echo.url' not in doc['values'] and 'echo.token' not in doc['secrets'] and doc['values']['url'] == EAST['url']


def test_a_parked_relay_hub_refuses_the_read(store):
    # given
    runtime = kube_runtime(store, {'hub': HUB, 'east': VIA_HUB | ECHO}, plugins=(echoer(),))
    runtime.instances.col.update_one({'_id': 'kubernetes/hub'}, {'$set': {'values.url': 'not a url'}})
    runtime.refresh()

    # when
    with pytest.raises(ReadError) as e:
        runtime.agent_read('east', 'echoer-read', {'text': 'hi'})

    # then
    assert runtime.instances.get('kubernetes', 'hub').problem and e.value.message == msg('agent.relay_off', label='east', relay='hub')


def test_the_plugin_handlers_are_annotated_with_the_context_they_use():
    # when
    hints = {name: typing.get_type_hints(getattr(kubernetes, name))['ctx'] for name in ('kube', 'test')}

    # then
    assert all(hasattr(h, 'agent') and hasattr(h, 'local_kube') for h in hints.values())


def test_the_agent_limits_live_in_one_protocol_module():
    # then
    assert (agentproto.VALUE_LIMIT, agentproto.KEYS_MAX) == (2000, 60)
    assert (agent_module.VALUE_LIMIT, agent_module.KEYS_MAX) == (instances_module.AGENT_VALUE_MAX, instances_module.AGENT_KEYS_MAX) == (2000, 60)


SOURCE_EAST = {'plugin': 'kubernetes', 'name': 'east', 'mode': 'agent', 'url': 'https://east.example.com', 'token': 'source-token-0009'}
ELSEWHERE = {'mode': 'agent', 'url': 'https://elsewhere.example.net', 'token': 'admin-token-0010'}


def sourced_runtime(rows, seen):
    registry = Registry.of(kubernetes.PLUGIN)
    db = mongomock.MongoClient(tz_aware=True)['kit']
    feed = SourceData('config file', {}, tuple(rows))
    box: list[Runtime] = []
    stored = Instances(db, registry, lambda: Sealer(KEY), sources=lambda: [feed], extra_fields=lambda plugin_id: box[0].agent_fields(plugin_id))
    runtime = Runtime(registry, PluginState(db), stored, app_transport({}, seen))
    box.append(runtime)
    runtime.refresh()
    return runtime


def sent_the_source_token(seen):
    return [c[1] for c in seen.calls if 'source-token-0009' in json.dumps(c[3])]


def test_a_saved_via_on_a_source_owned_agent_is_refused_and_an_old_one_is_ignored():
    # given
    seen = Seen()
    runtime = sourced_runtime([SOURCE_EAST], seen)
    runtime.instances.create('kubernetes', 'hub', ELSEWHERE, True, 'alice')

    # when
    with pytest.raises(SourceOwned) as refused:
        runtime.instances.update('kubernetes', 'east', {'via': 'hub'}, 'alice')
    runtime.instances.update('kubernetes', 'east', {'mode': 'agent'}, 'alice')
    runtime.instances.col.update_one({'_id': 'kubernetes/east'}, {'$set': {'values.via': 'hub'}})
    runtime.refresh()
    east = runtime.instances.get('kubernetes', 'east')
    with pytest.raises(ReadError):
        runtime.agent_for('east').info()

    # then
    assert refused.value.field == 'via' and 'via' in east.owned and 'via' not in east.values
    assert all(urlparse(url).hostname != 'elsewhere.example.net' for url in sent_the_source_token(seen))


def test_a_typed_via_on_a_source_owned_agent_is_a_moved_address_in_the_test():
    # given
    seen = Seen()
    runtime = sourced_runtime([SOURCE_EAST], seen)
    runtime.instances.create('kubernetes', 'hub', ELSEWHERE, True, 'alice')
    plugin = runtime.registry.get('kubernetes').plugin

    # when
    with pytest.raises(InvalidInstance) as refused:
        runtime.connection_test(plugin, 'east', {'via': 'hub'}, 5, MAIN_NOW)

    # then
    assert (refused.value.field, refused.value.args[0]) == ('token', msg('integrations.reenter_owned', field='Token', source='config file'))
    assert sent_the_source_token(seen) == []


def test_a_source_via_naming_an_admin_made_hub_parks_the_agent_and_its_test_never_runs():
    # given
    seen = Seen()
    runtime = sourced_runtime([SOURCE_EAST | {'via': 'hub'}], seen)
    runtime.instances.create('kubernetes', 'hub', ELSEWHERE, True, 'alice')
    runtime.refresh()
    plugin = runtime.registry.get('kubernetes').plugin

    # when
    east = runtime.instances.get('kubernetes', 'east')
    with pytest.raises(ReadError) as read:
        runtime.agent_read('east', 'any-read', {})
    with pytest.raises(InvalidInstance) as tested:
        runtime.connection_test(plugin, 'east', {}, 5, MAIN_NOW)

    # then
    assert east.problem == msg('instances.via_not_owned', via='hub', source='config file')
    assert read.value.status in (404, 503) and (tested.value.field, tested.value.args[0]) == ('via', east.problem)
    assert sent_the_source_token(seen) == []


def test_a_source_owned_agent_may_relay_through_a_hub_from_the_same_source():
    # given
    seen = Seen()
    hub = {'plugin': 'kubernetes', 'name': 'hub', 'mode': 'agent', 'url': 'https://hub.example.com', 'token': 'source-hub-0011'}

    # when
    runtime = sourced_runtime([hub, SOURCE_EAST | {'via': 'hub'}], seen)

    # then
    assert runtime.instances.get('kubernetes', 'east').problem is None and runtime.instances.get('kubernetes', 'east').values['via'] == 'hub'


def test_an_unsaved_agent_that_is_down_is_named_as_the_new_agent(store):
    # given
    runtime = kube_runtime(store, {}, transport=unreachable)
    plugin = runtime.registry.get('kubernetes').plugin

    # when
    tested = runtime.connection_test(plugin, None, {'mode': 'agent', 'url': 'https://other.example.com', 'token': 'other-token-0004'}, 5, MAIN_NOW)

    # then
    assert tested['ok'] is False and tested['summary'] == 'the new agent is unreachable'
