import json
from urllib.parse import urlsplit

import pytest

from skopax.contract import NODE_FIELD, AgentRead, Capabilities, Field, ReadError, Tool
from skopax.messages import msg
from skopax.plugintools import PluginTools
from skopax.plugins import kubernetes
from skopax.registry import Registry, field_view
from skopax.sweeps import watched
from tests.kube_kit import Seen, agent_app, app_transport, kube_runtime
from tests.plugins.base import minimal

NOW = 1_794_207_600.0
NODE = {'mode': 'agent', 'url': 'https://node.example.com', 'token': 'node-token-0001'}
DEMO_FIELDS = (Field('demo.url', 'Demo URL', 'url'), Field('demo.secret', 'Demo secret', 'secret'))


def demo_run(config, args):
    return {'url': config.get('demo.url'), 'secret': config.get('demo.secret'), 'text': args.get('text')}


def demo_tool(ctx, args):
    return ctx.node_read('demo', {'text': args.get('text', '')})


def demo(node_keys=None):
    read = AgentRead('demo', ('text',), (r'demo\.(url|secret)',), lambda c: ['demo'] if c.get('demo.url') else [], demo_run, fields=DEMO_FIELDS,
                     node_keys={'url': 'demo.url', 'client-secret': 'demo.secret'} if node_keys is None else node_keys)
    return minimal('demo', 'Demo', capabilities=Capabilities(secrets=('client-secret', 'demo.secret'), agent_reads=True), agent_reads=(read,),
                   fields=(Field('url', 'URL', 'url', required=True), Field('client-secret', 'Client secret', 'secret')),
                   tools=(Tool('demo', 'Demo read through a node', {'type': 'object', 'properties': {'text': {'type': 'string'}}}, demo_tool, instance_input='instance'),))


def demo_setup(store, instances: dict[str, dict], node: dict | None = None):
    plugin = Registry.of(demo()).get('demo').plugin
    far = agent_app('node-token-0001', plugins=(plugin,))
    seen = Seen()
    runtime = kube_runtime(store, {'test': node or NODE}, transport=app_transport({'node.example.com': far}, seen), plugins=(plugin,))
    runtime.state.set('demo', True, 'alice')
    for name, values in instances.items():
        runtime.instances.create('demo', name, values, True, 'alice')
    runtime.refresh()
    return runtime, seen, far, plugin


def test_core_adds_the_node_field_to_a_plug_in_that_opts_in():
    # when
    opted = Registry.of(demo()).get('demo').plugin
    plain = Registry.of(minimal('plain', 'Plain')).get('plain').plugin

    # then
    assert [f.key for f in opted.fields] == ['url', 'client-secret', NODE_FIELD]
    assert {f.key: f for f in opted.fields}[NODE_FIELD].label == 'Through agent node' and {f.key: f for f in opted.fields}[NODE_FIELD].group == 'advanced'
    assert NODE_FIELD not in [f.key for f in plain.fields]
    assert 'reaches this service' in {f.key: f for f in opted.fields}[NODE_FIELD].help and 'cluster' not in {f.key: f for f in opted.fields}[NODE_FIELD].help
    assert msg('integrations.health.through_node', node='n', checks='c').endswith('reads this service')


def test_a_node_key_mapping_must_name_declared_keys():
    # when
    found = Registry.of(demo(node_keys={'url': 'other.url'})).get('demo')

    # then
    assert found.state == 'broken' and 'node keys' in found.reason


def test_a_demo_read_pushes_each_instance_mapped_and_reads_it_back(store):
    # given
    runtime, seen, far, _ = demo_setup(store, {'dev': {'url': 'https://dev.example.com', 'client-secret': 'dev-secret-0001', NODE_FIELD: 'test'},
                                               'qa': {'url': 'https://qa.example.com', 'client-secret': 'qa-secret-0002', NODE_FIELD: 'test'}})
    tools = PluginTools(runtime)

    # when
    dev = tools.call('demo', {'instance': 'dev', 'text': 'a'}, NOW)
    qa = tools.call('demo', {'instance': 'qa', 'text': 'b'}, NOW)
    again = tools.call('demo', {'instance': 'dev', 'text': 'c'}, NOW)

    # then
    assert dev == {'url': 'https://dev.example.com', 'secret': 'dev-secret-0001', 'text': 'a'}
    assert qa == {'url': 'https://qa.example.com', 'secret': 'qa-secret-0002', 'text': 'b'}
    assert again['url'] == 'https://dev.example.com'
    pushed = [json.loads(c[4]) for c in seen.calls if c[0] == 'PUT']
    assert [(p['op'], p['instance'], p['config']) for p in pushed] == [('demo', 'dev', {'demo.secret': 'dev-secret-0001', 'demo.url': 'https://dev.example.com'}),
                                                                       ('demo', 'qa', {'demo.secret': 'qa-secret-0002', 'demo.url': 'https://qa.example.com'})]
    read = [json.loads(c[4]) for c in seen.calls if urlsplit(c[1]).path == '/api/read']
    assert [r['instance'] for r in read] == ['dev', 'qa', 'dev']
    assert sorted(far.app.state.core._scoped) == [('demo', 'dev'), ('demo', 'qa')] and far.app.state.core._pushed == {}


def test_a_demo_instance_without_a_node_or_with_a_wrong_one(store):
    # given
    runtime, seen, _, plugin = demo_setup(store, {'direct': {'url': 'https://dev.example.com'}, 'lost': {'url': 'https://dev.example.com', NODE_FIELD: 'nowhere'}})
    ctx = runtime.context(plugin, runtime.instances.get('demo', 'direct'), NOW)

    # when
    with pytest.raises(ReadError) as e:
        ctx.node_read('demo', {'text': 'x'})

    # then
    assert e.value.message == msg('agent.no_node', op='demo') and seen.calls == []
    assert runtime.instances.get('demo', 'lost').problem == msg('instances.via_node', node='nowhere')
    assert watched(plugin, runtime.instances.get('demo', 'direct')) != [] and watched(plugin, runtime.instances.get('demo', 'lost')) == []


def test_a_connection_moved_to_a_node_clears_the_cards_its_checks_left(store, tmp_path):
    # given
    import dataclasses
    from fastapi.testclient import TestClient
    from skopax.app import create_app
    from skopax.kube import Kube
    from skopax.plugins import kubernetes
    from skopax.sealer import master_key
    from tests.conftest import settings
    from tests.plugin_kit import add_instance
    from tests.plugins import two_checks_plugin
    from tests.test_sweeps import HERE, pair_key, statuses
    read = dataclasses.replace(two_checks_plugin.PLUGIN.agent_reads[0], node_keys={'url': 'url'})
    pair = dataclasses.replace(two_checks_plugin.PLUGIN, agent_reads=(read,))
    key = master_key(store.db, settings())
    for i, name in enumerate(('main', 'east')):
        add_instance(store, pair, name, sealer_key=key, primary=i == 0, url='https://demo.example.com')
    with TestClient(create_app(settings(), store, registry=Registry([Registry.of(pair, directory=HERE).get('demo'), Registry.of(kubernetes.PLUGIN).get('kubernetes')]), kube=Kube(root=tmp_path))) as c:
        sweeper, instances = c.app.state.sweeper, c.app.state.instances
        sweeper.run('demo', 'pulse', NOW)
        sweeper.run('demo', 'beat', NOW)
        instances.create('kubernetes', 'test', NODE, True, 'alice@example.com')
        before = statuses(store.load())

        # when
        instances.update('demo', 'east', {NODE_FIELD: 'test'}, 'alice@example.com')
        c.app.state.runtime.refresh()
        sweeper.heal()
        after = statuses(store.load())

    # then
    assert before[pair_key('pulse', 'east')] == before[pair_key('beat', 'east')] == 'open'
    assert after[pair_key('pulse', 'east')] == after[pair_key('beat', 'east')] == 'cleared'
    assert after[pair_key('pulse', 'main')] == after[pair_key('beat', 'main')] == 'open'


def test_the_node_field_and_the_relay_field_ask_the_page_for_a_pick_of_kubernetes_instances():
    # given
    opted = Registry.of(demo()).get('demo')
    found = Registry.of(kubernetes.PLUGIN).get('kubernetes')

    # when
    node = field_view({f.key: f for f in opted.plugin.fields}[NODE_FIELD])
    relay = field_view({f.key: f for f in found.plugin.fields}['via'])

    # then
    assert opted.state == 'ok' and 'kubernetes' not in opted.plugin.uses
    assert found.state == 'ok'
    assert (node['kind'], node['choices'], node['group']) == ('instance', ['kubernetes'], 'advanced')
    assert (relay['kind'], relay['choices'], relay['showIf']) == ('instance', ['kubernetes'], ['mode', 'agent'])


def test_a_saved_node_name_that_is_no_longer_an_agent_node_still_saves_and_shows_its_problem(store):
    # given
    runtime, _, _, _ = demo_setup(store, {'lost': {'url': 'https://dev.example.com', NODE_FIELD: 'nowhere'}})

    # when
    runtime.instances.update('demo', 'lost', {'url': 'https://other.example.com'}, 'alice')

    # then
    kept = runtime.instances.get('demo', 'lost')
    assert kept.values[NODE_FIELD] == 'nowhere' and kept.problem == msg('instances.via_node', node='nowhere')


def test_a_relay_must_still_name_another_agent_instance(store):
    # given
    runtime = kube_runtime(store, {'hub': NODE})

    # when
    with pytest.raises(Exception) as unknown:
        runtime.instances.create('kubernetes', 'east', {'mode': 'agent', 'url': 'https://east.example.com', 'token': 'east-token-0001', 'via': 'nowhere'}, True, 'alice')
    with pytest.raises(Exception) as itself:
        runtime.instances.create('kubernetes', 'east', {'mode': 'agent', 'url': 'https://east.example.com', 'token': 'east-token-0001', 'via': 'east'}, True, 'alice')
    made = runtime.instances.create('kubernetes', 'west', {'mode': 'agent', 'url': 'https://west.example.com', 'token': 'west-token-0001', 'via': 'hub'}, True, 'alice')

    # then
    assert str(unknown.value) == msg('instances.invalid.via') and str(itself.value) == msg('instances.invalid.via')
    assert made.values['via'] == 'hub'
