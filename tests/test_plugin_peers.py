import collections
import dataclasses
import ssl
import threading
from pathlib import Path

import pytest

from skopax import engine, monitor, registry, runtime as runtime_module
from skopax.app import create_app
from skopax.contract import AgentRead, Capabilities, Check, Field, PassResult, ReadError, Sweep, Tool, anomaly
from skopax.engine import iso
from skopax.instances import InvalidInstance, Instances, PluginState
from skopax.kube import Kube
from skopax.messages import msg
from skopax.plugintools import PluginTools
from skopax.registry import Registry
from skopax.runtime import Runtime
from skopax.scheduler import Scheduler
from skopax.sealer import Sealer, master_key
from skopax.sweeps import Sweeper, allowed
from tests.conftest import settings
from tests.kube_kit import FakeKube, echoer, kube_runtime
from tests.plugin_kit import add_instance
from tests.plugins import demo_plugin, peer_plugin
from tests.test_instances import pem

HERE = Path(demo_plugin.__file__).parent
KEY = 'peer-key'
NOW = 1_792_224_000.0
DEMO = demo_plugin.PLUGIN
PEER = peer_plugin.PLUGIN
SEEN: dict = {}


def runtime_of(store, *plugins, transport=None) -> Runtime:
    reg = Registry.of(*plugins, directory=HERE)
    runtime = Runtime(reg, PluginState(store.db), Instances(store.db, reg, lambda: Sealer(KEY)), transport)
    runtime.refresh()
    return runtime


def sweeper_of(store, runtime: Runtime) -> Sweeper:
    def apply(results, now, links, after=None, check=None, screen=None, clusters=None):
        return engine.apply(store, results, now, links, after=after, screen=screen, clusters=clusters)

    return Sweeper(runtime, store, apply)


def passes(store) -> dict:
    return {k: v['status'] for k, v in (store.load().get('serverPasses') or {}).items()}


def open_keys(store) -> set[str]:
    return {a['key'] for a in store.load()['anomalies'] if a['status'] == 'open'}


def noting(ctx) -> Sweep:
    name = ctx.instance.name
    first = ctx.previous('notes')
    seen = {'previous': None if first is None else list(first), 'deploys': ctx.deploys(ctx.now - 3600), 'cards': ctx.open_cards(), 'wide': ctx.open_cards(plugin_wide=True), 'shared': ctx.previous('services')}
    if first is not None:
        first.append({'name': 'planted'})
    seen['again'] = ctx.previous('notes')
    SEEN[name] = seen
    card = anomaly('demo', 'note', 'shop', name, '', 'warn', 1, None, None, '', 'Note', 'a note')
    return Sweep(PassResult('pulse', True, 'ok', [card], {}, ('demo',)), {'notes': [{'name': name}]})


def quiet(ctx) -> Sweep:
    return Sweep(PassResult('quiet', True, 'ok', [], {}, ('quiet',)))


def notes_tool(ctx, args):
    return {'rows': ctx.previous('notes'), 'cards': ctx.open_cards(), 'deploys': ctx.deploys(ctx.now - 3600)}


NOTED = dataclasses.replace(DEMO, checks=(Check('pulse', 'Demo pulse', 900, 0, ('demo',), noting, sections=('notes',)), Check('quiet', 'Quiet', 900, 0, ('quiet',), quiet)))
TOOLED = dataclasses.replace(NOTED, tools=(Tool('demo_notes', 'Read the notes section', {'type': 'object', 'properties': {}}, notes_tool),))


@pytest.fixture(autouse=True)
def clean_seen():
    SEEN.clear()
    yield
    SEEN.clear()


def spy(store, monkeypatch) -> collections.Counter:
    counted: collections.Counter = collections.Counter()
    for name in sorted(n for n, member in vars(type(store)).items() if not n.startswith('_') and callable(member)):
        real = getattr(store, name)

        def wrapped(*args, real=real, name=name, **kwargs):
            if threading.current_thread().name.startswith('sweep-'):
                counted[name] += 1
            return real(*args, **kwargs)

        monkeypatch.setattr(store, name, wrapped)
    return counted


def test_a_plugin_reads_through_another_plugins_instance(store, monkeypatch):
    # given
    cert = pem()
    add_instance(store, DEMO, 'main', sealer_key=KEY, primary=True, url='https://demo.example.com', ca=cert)
    add_instance(store, PEER, 'main', sealer_key=KEY, primary=True, source='main')
    runtime = runtime_of(store, DEMO, PEER)
    real, seen = ssl.create_default_context, []
    monkeypatch.setattr(ssl, 'create_default_context', lambda *a, **k: seen.append(k.get('cadata')) or real(*a, **k))

    # when
    sweeper_of(store, runtime).run('peer', 'echo', NOW)
    peer = runtime.context(PEER, runtime.instances.get('peer', 'main'), NOW).peer('source')
    sender = peer.http()
    card = next(a for a in store.load()['anomalies'] if a['key'] == 'peer|shop/checkout-service|main')

    # then
    assert card['what'] == 'https://demo.example.com' and card['status'] == 'open'
    assert seen == [cert]
    assert peer.info.name == 'main' and peer.info.config['url'] == 'https://demo.example.com'
    assert sender is runtime_module.pem_transport(cert)


def test_a_peer_field_must_name_a_used_plugin_and_an_existing_instance(store):
    # given
    other = dataclasses.replace(PEER, fields=(Field('source', 'Source', 'instance', required=True, choices=('other',)),))
    add_instance(store, DEMO, 'main', sealer_key=KEY, primary=True, url='https://demo.example.com')
    runtime = runtime_of(store, DEMO, PEER)

    # when
    broken = Registry.of(other, directory=HERE).get('peer')
    with pytest.raises(InvalidInstance) as unknown:
        runtime.instances.create('peer', 'main', {'source': 'nope'}, True, 'alice')
    with pytest.raises(InvalidInstance) as malformed:
        runtime.instances.create('peer', 'main', {'source': 'Not A Name'}, True, 'alice')
    made = runtime.instances.create('peer', 'main', {'source': 'main'}, True, 'alice')
    with pytest.raises(InvalidInstance) as changed:
        runtime.instances.update('peer', 'main', {'source': 'gone'}, 'alice')

    # then
    assert broken.state == 'broken' and msg('plugins.why.instance_field', key='source') in broken.reason
    assert (unknown.value.field, unknown.value.text) == ('source', msg('peers.unknown', plugin='Demo', name='nope'))
    assert (malformed.value.field, malformed.value.text) == ('source', msg('instances.invalid.instance', field='Source'))
    assert 'Not A Name' not in malformed.value.text
    assert made.values['source'] == 'main'
    assert (changed.value.field, changed.value.text) == ('source', msg('peers.unknown', plugin='Demo', name='gone'))
    assert runtime.instances.get('peer', 'main').values['source'] == 'main'


@pytest.mark.parametrize('over, reason', [
    ({'uses': ('Bad Id',)}, msg('plugins.why.uses', id='Bad Id')),
    ({'uses': ('peer', 'demo')}, msg('plugins.why.uses', id='peer')),
    ({'fields': (Field('source', 'Source', 'instance', choices=('demo', 'other')),)}, msg('plugins.why.instance_field', key='source')),
    ({'fields': (Field('source', 'Source', 'instance'),)}, msg('plugins.why.instance_field', key='source')),
    ({'max_instances': 0}, msg('plugins.why.max_instances')),
    ({'capabilities': Capabilities(agent_reads=True), 'agent_reads': (AgentRead('peer-read', ('text',), (r'peer\.src',), lambda config: [], lambda config, args: {},
                                                                                 fields=(Field('peer.src', 'Source', 'instance', choices=('demo',)),)),)},
     msg('plugins.why.agent_instance', key='peer.src')),
])
def test_a_bad_uses_instance_field_or_limit_breaks_the_plugin(over, reason):
    # given
    variant = dataclasses.replace(PEER, **over)

    # when
    loaded = Registry.of(variant, directory=HERE).get('peer')

    # then
    assert loaded.state == 'broken' and reason in loaded.reason


def test_an_unavailable_peer_fails_only_that_instance(store):
    # given
    for name, primary in (('main', True), ('east', False)):
        add_instance(store, DEMO, name, sealer_key=KEY, primary=primary, url=f'https://{name}.example.com')
        add_instance(store, PEER, name, sealer_key=KEY, primary=primary, source=name)
    runtime = runtime_of(store, DEMO, PEER)
    runtime.instances.set_enabled('demo', 'east', False, 'alice')
    sweeper = sweeper_of(store, runtime)

    # when
    started = Scheduler(store, sweeper.jobs, clock=lambda: NOW).run_due()
    found = passes(store)
    cards = open_keys(store)

    # then
    assert 'peer.echo' in started
    assert found['echo'] == 'ok'
    assert found['echo-east'] == 'error: ' + msg('peers.unavailable', plugin='Demo', instance='east')
    assert 'peer|shop/checkout-service|main' in cards and 'peer|shop/checkout-service|east' not in cards


def test_a_missing_used_plugin_does_not_break_the_reader(store):
    # given
    add_instance(store, PEER, 'main', sealer_key=KEY, primary=True, source='main')
    runtime = runtime_of(store, PEER)

    # when
    sweeper_of(store, runtime).run('peer', 'echo', NOW)

    # then
    assert runtime.registry.get('peer').state == 'ok'
    assert passes(store)['echo'] == 'error: ' + msg('peers.unavailable', plugin='demo', instance='main')


def test_a_ported_check_keeps_the_peer_text_through_monitor_failed():
    # given
    down = ReadError(503, msg('peers.unavailable', plugin='Demo', instance='east') + '\x00')

    # when
    shown = monitor.failed(down)
    other = monitor.failed(ValueError('token=s3cret'))

    # then
    assert shown == 'error: ' + msg('peers.unavailable', plugin='Demo', instance='east')
    assert other == 'error: ValueError'


def test_monitor_writes_its_dash_as_an_escape():
    # given
    source = Path(monitor.__file__).read_text(encoding='utf-8')

    # when
    shown = monitor.pct(5, 0)

    # then
    assert source.isascii() and shown == '\u2013'


def test_describe_lists_uses_and_max_instances():
    # given
    limited = dataclasses.replace(DEMO, max_instances=1)

    # when
    peer, demo = registry.describe(PEER), registry.describe(limited)

    # then
    assert (peer['uses'], peer['maxInstances']) == (['demo'], None)
    assert (demo['uses'], demo['maxInstances']) == ([], 1)
    assert (peer['fields'][0]['kind'], peer['fields'][0]['choices']) == ('instance', ['demo'])


def test_peers_lists_every_runnable_instance_primary_first(store):
    # given
    for name, primary in (('east', False), ('main', True)):
        add_instance(store, DEMO, name, sealer_key=KEY, primary=primary, url=f'https://{name}.example.com')
    add_instance(store, PEER, 'main', sealer_key=KEY, primary=True, source='main')
    runtime = runtime_of(store, DEMO, PEER)
    ctx = runtime.context(PEER, runtime.instances.get('peer', 'main'), NOW)

    # when
    both = [p.info.name for p in ctx.peers('demo')]
    own = [p.info.name for p in ctx.peers('peer')]
    runtime.instances.set_enabled('demo', 'east', False, 'alice')
    one = [p.info.name for p in ctx.peers('demo')]
    runtime.state.set('demo', False, 'alice')
    none = ctx.peers('demo')
    with pytest.raises(ReadError) as refused:
        ctx.peers('prometheus')
    with pytest.raises(ReadError) as not_a_peer:
        ctx.peer('missing')

    # then
    assert (both, own, one, none) == (['main', 'east'], ['main'], ['main'], [])
    assert (refused.value.status, refused.value.message) == (403, msg('peers.not_used', plugin='prometheus'))
    assert (not_a_peer.value.status, not_a_peer.value.message) == (400, msg('peers.not_instance', field='missing'))


def test_an_empty_peer_field_gives_no_peer(store):
    # given
    optional = dataclasses.replace(PEER, fields=(Field('source', 'Source', 'instance', choices=('demo',)),))
    add_instance(store, optional, 'main', sealer_key=KEY, primary=True)
    runtime = runtime_of(store, DEMO, optional)

    # when
    found = runtime.context(optional, runtime.instances.get('peer', 'main'), NOW).peer('source')

    # then
    assert found is None


def test_only_the_kubernetes_plugin_reaches_a_peers_agent(store):
    # given
    agents = {'main': {'mode': 'agent', 'url': 'https://main.example.com', 'token': 'main-token-0001'}, 'east': {'mode': 'agent', 'url': 'https://east.example.com', 'token': 'east-token-0002'}}
    runtime = kube_runtime(store, agents, kube=FakeKube(), plugins=(echoer(),))
    host = runtime.registry.get('kubernetes').plugin
    other = runtime.registry.get('echoer').plugin
    ctx = runtime.context(host, runtime.instances.get('kubernetes', 'main'), NOW)

    # when
    peers = ctx.peers('kubernetes')
    reached = ctx.agent_of(peers[1])
    with pytest.raises(ReadError) as refused:
        runtime.context(other, runtime.instances.get('kubernetes', 'main'), NOW).agent_of(peers[1])

    # then
    assert [p.info.name for p in peers] == ['main', 'east'] and reached.label == 'east'
    assert (refused.value.status, refused.value.message) == (403, msg('agent.host_only'))


def test_the_sub_key_rule_allows_ran_keys_and_their_colon_sub_keys():
    # given
    check = Check('tick', 'Tick', 900, 0, ('tick', 'tock'), quiet)

    # when
    verdicts = [allowed(key, check) for key in ('tick', 'tock:a', 'tick:a:b', 'tickle', 'tick-east', 'other:tick')]

    # then
    assert verdicts == [True, True, True, False, False, False]


def test_context_reads_previous_rows_deploys_and_open_cards(store, monkeypatch):
    # given
    for name, primary in (('main', True), ('east', False)):
        add_instance(store, NOTED, name, sealer_key=KEY, primary=primary, url=f'https://{name}.example.com')
    later = NOW + 900
    rolling = {'ns': 'shop', 'svc': 'cart', 'at': iso(later - 60), 'toImage': 'cart:2'}
    stale = {'ns': 'shop', 'svc': 'old', 'at': iso(later - 7200), 'toImage': 'old:1'}
    quieted = {'key': 'quiet|shop/east', 'sev': 'warn', 'status': 'open', 'svc': 'east', 'label': 'Quiet', 'src': 'quiet', 'cluster': 'east', 'ranKey': 'quiet-east', 'now': 1,
               'short': 'WARN EAST east Quiet', 'firstSeen': iso(NOW - 60), 'lastSeen': iso(NOW - 60)}
    store.save({'initialized': True, 'anomalies': [quieted], 'muted': [], 'notes': [{'name': 'legacy'}, {'plugin': 'other', 'name': 'foreign'}], 'deploys': [stale, rolling]}, None)
    store.add_deploys([{'ns': 'shop', 'svc': 'checkout-service', 'toImage': 'checkout:2', 'at': iso(later - 600)}, {'ns': 'shop', 'svc': 'payments', 'toImage': 'payments:2', 'at': iso(later - 7200)}])
    runtime = runtime_of(store, NOTED)
    sweeper = sweeper_of(store, runtime)
    sweeper.run('demo', 'pulse', NOW)
    first = {name: SEEN[name]['previous'] for name in SEEN}
    SEEN.clear()
    counted = spy(store, monkeypatch)

    # when
    sweeper.run('demo', 'pulse', later)
    reads = dict(counted)
    counted.clear()
    sweeper.run('demo', 'quiet', later)
    idle = dict(counted)
    outside = runtime.context(NOTED, runtime.instances.get('demo', 'east'), later)

    # then
    assert first == {'main': [], 'east': []}
    assert [r['name'] for r in SEEN['main']['previous']] == ['main'] and [r['name'] for r in SEEN['east']['previous']] == ['east']
    assert [r['name'] for r in SEEN['main']['again']] == ['main']
    assert [d['svc'] for d in SEEN['east']['deploys']] == ['checkout-service', 'cart']
    assert [a['key'] for a in SEEN['east']['cards']] == ['note|shop/east'] and [a['key'] for a in SEEN['main']['cards']] == ['note|shop/main']
    assert sorted(a['key'] for a in SEEN['east']['wide']) == ['note|shop/east', 'quiet|shop/east'] and [a['key'] for a in SEEN['main']['wide']] == ['note|shop/main']
    assert SEEN['main']['shared'] is None
    assert reads == {'load': 1, 'deploys_since': 1}
    assert idle == {}
    assert (outside.previous('notes'), outside.deploys(later - 3600), outside.open_cards()) == (None, [], [])


def test_a_tool_reads_its_own_plugins_sections_in_its_own_thread(store, monkeypatch, tmp_path):
    # given
    for name, primary in (('main', True), ('east', False)):
        add_instance(store, TOOLED, name, sealer_key=master_key(store.db, settings()), primary=primary, url=f'https://{name}.example.com')
    store.save({'initialized': True, 'anomalies': [], 'muted': [], 'notes': [{'plugin': 'demo', 'name': 'main'}, {'plugin': 'demo', 'cluster': 'east', 'name': 'east'}, {'plugin': 'other', 'name': 'foreign'}]}, None)
    app = create_app(settings(), store, registry=Registry.of(TOOLED, directory=HERE), kube=Kube(root=tmp_path))
    app.state.runtime.refresh()
    tools = PluginTools(app.state.runtime)
    threads, real = [], store.load
    monkeypatch.setattr(store, 'load', lambda: threads.append(threading.current_thread().name) or real())

    # when
    east = tools.call('demo_notes', {'instance': 'east'}, NOW)
    main = tools.call('demo_notes', {}, NOW)

    # then
    assert [r['name'] for r in east['rows']] == ['east'] and [r['name'] for r in main['rows']] == ['main']
    assert (east['cards'], east['deploys']) == ([], [])
    assert threads == ['plugin-tool', 'plugin-tool']
