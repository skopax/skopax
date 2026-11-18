import dataclasses
import logging
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from skopax import engine
from skopax.app import create_app
from skopax.contract import Check, PassResult, SourceData, Sweep, anomaly
from skopax.engine import STALE_S, iso
from skopax.instances import Instances, PluginState
from skopax.kube import Kube
from skopax.messages import msg
from skopax.registry import Registry
from skopax.runtime import Runtime
from skopax.sealer import Sealer, master_key
from skopax.settingsources import Follower, SourceSet
from skopax.sweeps import WINDOW_S, Sweeper
from tests.conftest import AUTH, settings
from tests.plugin_kit import add_instance
from tests.plugins import cache_plugin, demo_plugin, linker_plugin

HERE = Path(demo_plugin.__file__).parent
KEY = 'keys-key'
NOW = 1_792_224_000.0
DEMO = demo_plugin.PLUGIN
CACHE = cache_plugin.PLUGIN
LINKER = linker_plugin.PLUGIN
LOGS = linker_plugin.LOGS
PLAN: dict = {}
SEEN: dict = {}
NOT_A_LIST = 'stored section capacity is not a list of rows; reading it as no rows'


def card(name: str, key: str) -> dict:
    return dict(anomaly('demo', 'err', 'shop', 'checkout-service', f'{name}-{key}', 'warn', 5, None, None, '', 'ERROR logs', 'five errors'), ranKey=key)


def scripted(ctx) -> Sweep:
    plan = PLAN.get(ctx.instance.name, {})
    if plan.get('fail'):
        raise RuntimeError('down')
    return Sweep(PassResult('pulse', True, 'ok', [card(ctx.instance.name, k) for k in plan.get('cards', ())], {}, tuple(plan.get('ran', ()))))


def quiet(ctx) -> Sweep:
    return Sweep(PassResult('quiet', True, 'ok', [], {}, ('quiet',)))


def once_pulse(ctx) -> Sweep:
    peers = [p.info.name for p in ctx.peers('demo')]
    SEEN.setdefault('runs', []).append(ctx.instance.name)
    SEEN['peers'] = peers
    cards = [dict(anomaly('demo', 'node', 'cluster', name, '', 'warn', 1, None, None, '', 'Node', 'a node'), ranKey=f'demo:{name}', cluster=name) for name in peers]
    return Sweep(PassResult('pulse', True, 'ok', cards, {}, tuple(f'demo:{name}' for name in peers)), {'members': [{'node': name} for name in peers]})


def named_links(card, inst):
    return [{'label': f'Demo {inst.name}', 'url': f'https://example.com/demo/{inst.name}'}]


SUBBED = dataclasses.replace(DEMO, checks=(Check('pulse', 'Demo pulse', 900, 0, ('demo',), scripted),))
LINKED = dataclasses.replace(DEMO, links=named_links, after=('linker',))


def once_demo():
    return dataclasses.replace(DEMO, checks=(Check('pulse', 'Demo pulse', 900, 0, ('demo',), once_pulse, sections=('members',), once=True),))


def cache_links(card, inst):
    return [{'label': f'Cache {inst.name}', 'url': f'https://example.com/cache/{inst.name}'}]


def moving_demo():
    return dataclasses.replace(DEMO, checks=(Check('pulse', 'Demo pulse', 900, 0, ('demo',), scripted), Check('quiet', 'Quiet', 900, 0, ('quiet',), quiet, once=True)))


def listed(primary: str) -> SourceData:
    return SourceData.of({'instances': [{'plugin': 'demo', 'name': n, 'url': 'https://demo.example.com', 'primary': str(n == primary).lower()} for n in ('main', 'east')]}, 'feed')


def sourced_app(store, tmp_path, sources):
    return TestClient(create_app(settings(), store, sources=sources, registry=Registry.of(moving_demo(), directory=HERE), kube=Kube(root=tmp_path)))


class Serving:
    def __init__(self, plugin, *instances):
        self.found = [(plugin, list(instances))]

    def serving(self):
        return self.found


@pytest.fixture(autouse=True)
def clean():
    PLAN.clear()
    SEEN.clear()
    linker_plugin.reset()
    yield
    PLAN.clear()
    SEEN.clear()
    linker_plugin.reset()


async def no_loop(self, every=0):
    return None


class Applying:
    def __init__(self, store):
        self.store = store
        self.results: list = []

    def __call__(self, results, now, links, after=None, check=None, screen=None, **extra):
        self.results.append(list(results))
        return engine.apply(self.store, results, now, links, after=after, screen=screen, **extra)


def add_two(store, plugin, names=('main', 'east'), key=KEY):
    for i, name in enumerate(names):
        add_instance(store, plugin, name, sealer_key=key, primary=i == 0, url='https://demo.example.com')


def runtime_of(store, *plugins) -> Runtime:
    reg = Registry.of(*plugins, directory=HERE)
    runtime = Runtime(reg, PluginState(store.db), Instances(store.db, reg, lambda: Sealer(KEY)), None)
    runtime.refresh()
    return runtime


def app_of(store, tmp_path, plugin):
    return TestClient(create_app(settings(), store, registry=Registry.of(plugin, directory=HERE), kube=Kube(root=tmp_path)))


def by_key(state: dict) -> dict:
    return {a['key']: a for a in state['anomalies']}


def test_sub_ran_keys_are_accepted_and_scoped(store):
    # given
    add_two(store, SUBBED)
    applying = Applying(store)
    sweeper = Sweeper(runtime_of(store, SUBBED), store, applying)
    PLAN.update(main={'ran': ('demo:a',), 'cards': ('demo:a',)}, east={'ran': ('demo:a',), 'cards': ('demo:a',)})

    # when
    sweeper.run('demo', 'pulse', NOW)
    ran = {r.name: set(r.ran) for r in applying.results[-1]}
    opened = by_key(store.load())
    PLAN.update(main={'ran': ('demo:a',)}, east={'ran': ('other:a',)})
    sweeper.run('demo', 'pulse', NOW + 900)
    after = store.load()

    # then
    main_card, east_card = opened[card('main', 'demo:a')['key']], opened[card('east', 'demo:a')['key'] + '@east']
    assert ran == {'pulse': {'demo:a'}, 'pulse-east': {'demo:a-east'}}
    assert main_card['ranKey'] == 'demo:a' and not main_card.get('cluster')
    assert east_card['ranKey'] == 'demo:a-east' and east_card['cluster'] == 'east'
    assert after['serverPasses']['pulse']['status'] == 'ok'
    assert after['serverPasses']['pulse-east']['status'] == msg('sweeps.ran_outside', keys='other:a')
    assert by_key(after)[main_card['key']]['status'] == 'cleared' and by_key(after)[east_card['key']]['status'] == 'open'


def test_a_card_the_pass_does_not_report_is_kept_and_clears_only_when_stale(store):
    # given
    add_two(store, SUBBED, names=('main',))
    sweeper = Sweeper(runtime_of(store, SUBBED), store, Applying(store))
    a, b, x = (card('main', k)['key'] for k in ('demo:a', 'demo:b', 'demo:x'))

    # when
    PLAN.update(main={'ran': ('demo:a',), 'cards': ('demo:a', 'demo:b')})
    sweeper.run('demo', 'pulse', NOW)
    capped = store.load()
    PLAN.update(main={'cards': ('demo:x',)})
    sweeper.run('demo', 'pulse', NOW + 900)
    keyed = store.load()
    PLAN.update(main={'ran': ('demo:a',)})
    sweeper.run('demo', 'pulse', NOW + 1800)
    kept = store.load()
    sweeper.run('demo', 'pulse', NOW + 1800 + STALE_S + 60)
    stale = store.load()

    # then
    assert capped['serverPasses']['pulse']['status'] == 'ok' and keyed['serverPasses']['pulse']['status'] == 'ok'
    assert {k: by_key(capped)[k]['status'] for k in (a, b)} == {a: 'open', b: 'open'}
    assert by_key(keyed)[x]['status'] == 'open' and by_key(keyed)[x]['ranKey'] == 'demo:x'
    assert {k: by_key(kept)[k]['status'] for k in (a, b, x)} == {a: 'cleared', b: 'open', x: 'open'}
    assert all(by_key(stale)[k]['status'] == 'cleared' and by_key(stale)[k]['expired'] for k in (b, x))


@pytest.mark.parametrize('how', ['delete', 'off', 'unchecked'])
def test_a_sub_key_card_closes_when_its_instance_goes_or_its_check_is_unchecked(store, tmp_path, monkeypatch, how):
    # given
    monkeypatch.setattr(Runtime, 'loop', no_loop)
    add_two(store, SUBBED, key=master_key(store.db, settings()))
    PLAN.update(main={'ran': ('demo:a',), 'cards': ('demo:a',)}, east={'ran': ('demo:a',), 'cards': ('demo:a',)})
    with app_of(store, tmp_path, SUBBED) as c:
        c.app.state.sweeper.run('demo', 'pulse', NOW)

        # when
        if how == 'delete':
            c.delete('/api/admin/plugins/demo/instances/east', headers=AUTH)
        elif how == 'off':
            c.patch('/api/admin/plugins/demo/instances/east', json={'enabled': False}, headers=AUTH)
        else:
            c.app.state.instances.set_checks('demo', 'east', ['pulse'], 'alice')
            c.app.state.runtime.refresh()
        state = by_key(store.load())

    # then
    east_card, main_card = state[card('east', 'demo:a')['key'] + '@east'], state[card('main', 'demo:a')['key']]
    assert east_card['ranKey'] == 'demo:a-east' and east_card['status'] == 'cleared' and east_card['expired']
    assert main_card['status'] == 'open'


def test_a_once_check_runs_on_the_primary_only_and_is_not_scoped(store):
    # given
    plugin = once_demo()
    add_two(store, plugin)
    applying = Applying(store)
    sweeper = Sweeper(runtime_of(store, plugin), store, applying)
    jobs = sweeper.jobs()

    # when
    for job in jobs:
        job.run(NOW)
    state = store.load()

    # then
    opened = [a for a in state['anomalies'] if a['status'] == 'open']
    assert [job.name for job in jobs] == ['demo.pulse']
    assert SEEN['runs'] == ['main'] and SEEN['peers'] == ['main', 'east']
    assert [(r.name, set(r.ran)) for r in applying.results[-1]] == [('pulse', {'demo:main', 'demo:east'})]
    assert set(state['serverPasses']) == {'pulse'}
    assert sorted(a['ranKey'] for a in opened) == ['demo:east', 'demo:main'] and not any('cluster' in a for a in opened)
    assert state['members'] == [{'node': 'main', 'plugin': 'demo'}, {'node': 'east', 'plugin': 'demo'}]


def test_a_once_check_waits_while_no_primary_is_runnable(store):
    # given
    plugin = once_demo()
    add_instance(store, plugin, 'main', sealer_key=KEY, primary=True, url='not a url')
    add_instance(store, plugin, 'east', sealer_key=KEY, primary=False, url='https://demo.example.com')
    runtime = runtime_of(store, plugin)
    sweeper = Sweeper(runtime, store, Applying(store))

    # when
    applied = sweeper.run('demo', 'pulse', NOW)

    # then
    assert runtime.instances.get('demo', 'main').problem and [i.name for i in runtime.instances_of('demo')] == ['east']
    assert applied is None and 'runs' not in SEEN


def test_a_waiting_instance_beside_one_without_a_check_keeps_the_plugin_waiting(store, tmp_path, monkeypatch):
    # given
    monkeypatch.setattr(Runtime, 'loop', no_loop)
    add_two(store, once_demo(), key=master_key(store.db, settings()))

    # when
    with app_of(store, tmp_path, once_demo()) as c:
        row = next(p for p in c.get('/api/admin/plugins', headers=AUTH).json()['plugins'] if p['id'] == 'demo')

    # then
    assert {r['name']: r['state'] for r in row['health']['instances']} == {'main': 'pending', 'east': 'none'}
    assert (row['health']['state'], row['health']['text']) == ('pending', msg('integrations.health.pending'))


def test_other_instances_skip_a_once_check_in_tiles_health_and_retire(store, tmp_path, monkeypatch):
    # given
    monkeypatch.setattr(Runtime, 'loop', no_loop)
    plugin = once_demo()
    add_two(store, plugin, key=master_key(store.db, settings()))
    with app_of(store, tmp_path, plugin) as c:
        c.app.state.sweeper.run('demo', 'pulse', NOW)

        # when
        tile = next(t for t in c.get('/api/server', headers=AUTH).json()['plugins'] if t['id'] == 'demo')
        row = next(p for p in c.get('/api/admin/plugins', headers=AUTH).json()['plugins'] if p['id'] == 'demo')
        unchecked = c.patch('/api/admin/plugins/demo/instances/east', json={'offChecks': ['pulse']}, headers=AUTH)
        c.app.state.runtime.refresh()
        deleted = c.delete('/api/admin/plugins/demo/instances/east', headers=AUTH)
        state = store.load()

    # then
    health = {r['name']: r for r in row['health']['instances']}
    assert tile['passes'] == ['pulse']
    assert {i['name']: (i['pass'], i['status']) for i in tile['instances']} == {'main': ('pulse', 'ok'), 'east': (None, None)}
    assert health['main']['state'] == 'ok' and (health['east']['state'], health['east']['text']) == ('none', msg('integrations.health.no_checks'))
    assert (row['health']['state'], row['health']['text']) == ('ok', msg('integrations.health.ok'))
    assert unchecked.status_code == 200 and deleted.status_code == 204
    assert sorted((a['ranKey'], a['status']) for a in state['anomalies']) == [('demo:east', 'open'), ('demo:main', 'open')]
    assert set(state['serverPasses']) == {'pulse'}


def test_a_once_check_owns_only_its_bare_pass(store):
    # given
    plugin = once_demo()
    add_two(store, plugin)
    sweeper = Sweeper(runtime_of(store, plugin), store, Applying(store))
    passes = dict.fromkeys(('pulse', 'pulse-east'), 'ok')
    store.save({'initialized': True, 'anomalies': [], 'sources': dict(passes), 'serverPasses': {k: {'at': '2026-10-26T08:00:00Z', 'status': v} for k, v in passes.items()}, 'muted': []}, None)

    # when
    targets = sweeper._seen_targets(plugin)
    sweeper.heal()
    healed = store.load()

    # then
    assert [(t.name, t.primary) for t in targets] == [('-', True)]
    assert set(healed['sources']) == {'pulse'} and set(healed['serverPasses']) == {'pulse'}


def test_links_come_from_every_serving_plugin_owner_first(store, caplog):
    # given
    add_two(store, LINKED)
    add_instance(store, LINKER, 'main', sealer_key=KEY, primary=True, url='https://logs.example.com')
    runtime = runtime_of(store, LINKER, LINKED)
    from_east = {'key': 'err|shop/checkout-service', 'src': 'demo', 'ranKey': 'demo-east', 'cluster': 'east'}
    demo_east, demo_main = named_links({}, runtime.instances.get('demo', 'east'))[0], named_links({}, runtime.instances.get('demo', 'main'))[0]

    # when
    east = runtime.links(from_east)
    main = runtime.links({'key': 'k', 'src': 'demo'})
    unowned = runtime.links({'key': 'k', 'src': 'zzz', 'cluster': 'east'})
    linker_plugin.PLAN['extra'] = [{'label': 'Same', 'url': demo_east['url']}]
    shared = runtime.links(from_east)
    linker_plugin.PLAN['extra'] = [{'label': f'More {i}', 'url': f'https://logs.example.com/{i}'} for i in range(9)]
    many = runtime.links(from_east)
    linker_plugin.PLAN.update(only=[{'label': 'No url'}, 'text', {'label': 'Bad url', 'url': 5}, {'label': 'Plain', 'url': 'http://logs.example.com/plain'}, {'label': 5, 'url': 'https://logs.example.com/n'},
                                    {'label': 'Spaced', 'url': 'https://logs.example.com/a b'}, {'label': 'Extra', 'url': 'https://logs.example.com/extra', 'odd': {1, 2}}])
    malformed = runtime.links(from_east)
    linker_plugin.PLAN.update(only=[])
    empty = runtime.links({'key': 'k', 'src': 'demo', 'cluster': 'ghost'})
    linker_plugin.PLAN['raise'] = True
    with caplog.at_level(logging.WARNING, logger='skopax.runtime'):
        raised = [runtime.links(from_east), runtime.links(from_east)]
        linker_plugin.PLAN['raise'] = False
        back = runtime.links(from_east)
        linker_plugin.PLAN['raise'] = True
        again = runtime.links(from_east)

    # then
    assert [p.id for p in runtime.active()] == ['linker', 'demo']
    assert east == [demo_east, LOGS] and main == [demo_main, LOGS] and unowned == [LOGS, demo_main]
    assert shared == [demo_east, LOGS]
    assert len(many) == 8 and many[:3] == [demo_east, LOGS, {'label': 'More 0', 'url': 'https://logs.example.com/0'}]
    assert malformed == [demo_east, {'label': 'Extra', 'url': 'https://logs.example.com/extra'}] and empty is None
    assert raised == [[demo_east], [demo_east]] and back == [demo_east] and again == [demo_east]
    assert [r.getMessage() for r in caplog.records].count(msg('runtime.links_failed', plugin='linker', error='RuntimeError')) == 2


def test_a_hanging_links_hook_of_another_plugin_does_not_stop_the_demo_pass(store, caplog):
    # given
    release = threading.Event()

    def hangs(card, inst):
        release.wait(2)
        return [dict(LOGS)]

    linked = dataclasses.replace(LINKED, checks=SUBBED.checks)
    hanging = dataclasses.replace(LINKER, links=hangs)
    add_two(store, linked)
    add_instance(store, hanging, 'main', sealer_key=KEY, primary=True, url='https://logs.example.com')
    runtime = runtime_of(store, hanging, linked)
    runtime.hook_timeout = 0.3
    sweeper = Sweeper(runtime, store, Applying(store))
    store.save({'initialized': True, 'anomalies': [], 'sources': {}, 'muted': []}, None)
    PLAN.update(main={'ran': ('demo:x',), 'cards': ('demo:x',)}, east={'ran': ('demo:y',), 'cards': ('demo:y',)})

    # when
    began = time.monotonic()
    with caplog.at_level(logging.WARNING, logger='skopax.runtime'):
        sweeper.run('demo', 'pulse', NOW)
    took = time.monotonic() - began
    stalled = by_key(store.load())
    release.set()
    for worker in [t for t in threading.enumerate() if t.name == 'plugin-links']:
        worker.join(3)
    sweeper.run('demo', 'pulse', NOW + 900)
    back = by_key(store.load())

    # then
    assert took < 0.3 + 1.0
    assert len(stalled) == 2 and all(a['status'] == 'open' for a in stalled.values())
    assert not any(x['label'] == 'Logs' for a in stalled.values() for x in a.get('links') or [])
    assert [r.getMessage() for r in caplog.records if r.name == 'skopax.runtime'] == [msg('runtime.links_failed', plugin='linker', error='TimedOut')]
    assert sorted(tuple(x['label'] for x in a['links']) for a in back.values()) == [('Demo east', 'Logs'), ('Demo main', 'Logs')]


def test_concurrent_links_steps_both_get_the_link_of_a_healthy_slow_hook(store, caplog):
    # given
    def slow(card, inst):
        time.sleep(0.2)
        return [dict(LOGS)]

    linker = dataclasses.replace(LINKER, links=slow)
    add_instance(store, linker, 'main', sealer_key=KEY, primary=True, url='https://logs.example.com')
    runtime = runtime_of(store, linker)
    got = []
    workers = [threading.Thread(target=lambda: got.append(runtime.links({'key': 'k', 'src': 'zzz'}))) for _ in range(2)]

    # when
    with caplog.at_level(logging.WARNING, logger='skopax.runtime'):
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(5)

    # then
    assert got == [[LOGS], [LOGS]], 'one links step waits for the other instead of finding the hook busy'
    assert [r.getMessage() for r in caplog.records if r.name == 'skopax.runtime'] == []


def test_seen_targets_read_each_twin_pass_by_its_longest_check_id(store):
    # given
    sweeper = Sweeper(runtime_of(store, CACHE), store, Applying(store))
    passes = dict.fromkeys(('tick', 'tick-cache', 'tick-east', 'tick-cache-east'), 'ok')
    store.save({'initialized': True, 'anomalies': [], 'sources': passes, 'muted': []}, None)

    # when
    targets = sweeper._seen_targets(CACHE)

    # then
    assert [(t.name, t.primary) for t in targets] == [('-', True), ('east', False)]


def test_the_heal_keeps_each_twin_pass_while_its_own_instance_serves(store):
    # given
    add_two(store, CACHE)
    runtime = runtime_of(store, CACHE)
    sweeper = Sweeper(runtime, store, Applying(store))
    passes = dict.fromkeys(('tick', 'tick-cache', 'tick-east', 'tick-cache-east'), 'ok')
    store.save({'initialized': True, 'anomalies': [], 'sources': dict(passes), 'serverPasses': {k: {'at': '2026-10-26T08:00:00Z', 'status': v} for k, v in passes.items()}, 'muted': []}, None)

    # when
    sweeper.heal()
    both = set(store.load()['sources'])
    runtime.instances.delete('cache-demo', 'east', 'alice')
    runtime.refresh()
    sweeper.heal()
    after = store.load()

    # then
    assert both == set(passes)
    assert set(after['sources']) == {'tick', 'tick-cache'} and set(after['serverPasses']) == {'tick', 'tick-cache'}


def test_unchecked_leftovers_read_a_stored_non_list_section_as_no_rows_and_warn_once(store, caplog):
    # given
    plugin = dataclasses.replace(DEMO, checks=(Check('pulse', 'Demo pulse', 900, 0, ('demo',), scripted, sections=('capacity',)), Check('quiet', 'Quiet', 900, 0, ('quiet',), quiet)))
    add_two(store, plugin)
    runtime = runtime_of(store, plugin)
    for name in ('main', 'east'):
        runtime.instances.set_checks('demo', name, ['pulse'], 'alice')
    runtime.refresh()
    sweeper = Sweeper(runtime, store, Applying(store))
    state = {'initialized': True, 'anomalies': [], 'muted': [], 'capacity': {'at': NOW, 'volumes': ['orders-db-0']}}

    # when
    with caplog.at_level(logging.WARNING, logger='skopax.sweeps'):
        found = [sweeper._unchecked_leftovers(state), sweeper._unchecked_leftovers(state)]

    # then
    assert found == [[], []]
    assert [r.getMessage() for r in caplog.records] == [NOT_A_LIST]
    assert 'orders-db-0' not in caplog.text


def test_a_pass_clears_only_the_cards_of_its_own_instance(store):
    # given
    add_two(store, SUBBED)
    sweeper = Sweeper(runtime_of(store, SUBBED), store, Applying(store))
    legacy = dict(card('main', 'demo:z'), cluster='main', status='open', firstSeen=iso(NOW - 600), lastSeen=iso(NOW - 600))
    store.save({'initialized': True, 'anomalies': [legacy], 'sources': {}, 'muted': []}, None)
    mine, theirs = card('main', 'demo:x-east')['key'], card('east', 'demo:y')['key'] + '@east'

    # when
    PLAN.update(main={'ran': ('demo:x-east', 'demo:z'), 'cards': ('demo:x-east',)}, east={'ran': ('demo:y',), 'cards': ('demo:y',)})
    sweeper.run('demo', 'pulse', NOW)
    first = by_key(store.load())
    PLAN.update(main={'fail': True}, east={'ran': ('demo:x', 'demo:y'), 'cards': ('demo:y',)})
    sweeper.run('demo', 'pulse', NOW + 900)
    east_only = by_key(store.load())
    PLAN.update(main={'ran': ('demo:x-east', 'demo:y-east'), 'cards': ('demo:x-east',)}, east={'fail': True})
    sweeper.run('demo', 'pulse', NOW + 1800)
    main_only = by_key(store.load())
    PLAN.update(main={'ran': ('demo:x-east',)}, east={'ran': ('demo:y',)})
    sweeper.run('demo', 'pulse', NOW + 2700)
    both = by_key(store.load())

    # then
    assert (first[mine]['ranKey'], first[mine].get('cluster'), first[theirs]['ranKey'], first[theirs]['cluster']) == ('demo:x-east', None, 'demo:y-east', 'east')
    assert first[legacy['key']]['status'] == 'cleared'
    assert east_only[mine]['status'] == 'open' and main_only[theirs]['status'] == 'open'
    assert both[mine]['status'] == 'cleared' and both[theirs]['status'] == 'cleared'


def test_a_primary_move_keeps_the_cards_pass_and_rows_of_a_once_check(store, tmp_path, monkeypatch):
    # given
    monkeypatch.setattr(Runtime, 'loop', no_loop)
    plugin = once_demo()
    add_two(store, plugin, key=master_key(store.db, settings()))
    with app_of(store, tmp_path, plugin) as c:
        c.app.state.sweeper.run('demo', 'pulse', NOW)
        before = store.load()

        # when
        moved = c.patch('/api/admin/plugins/demo/instances/east', json={'primary': True}, headers=AUTH)
        after = store.load()

    # then
    assert moved.status_code == 200 and c.app.state.instances.primary('demo').name == 'east'
    assert sorted((a['ranKey'], a['status']) for a in after['anomalies']) == [('demo:east', 'open'), ('demo:main', 'open')]
    assert after['serverPasses']['pulse'] == before['serverPasses']['pulse'] and after['sources']['pulse'] == 'ok'
    assert after['members'] == before['members'] == [{'node': 'main', 'plugin': 'demo'}, {'node': 'east', 'plugin': 'demo'}]


def test_a_primary_move_hands_the_old_primarys_once_choices_to_the_new_one(store, tmp_path, monkeypatch):
    # given
    monkeypatch.setattr(Runtime, 'loop', no_loop)
    plugin = dataclasses.replace(DEMO, checks=(Check('pulse', 'Demo pulse', 900, 0, ('demo',), once_pulse, sections=('members',), once=True), Check('quiet', 'Quiet', 900, 0, ('quiet',), quiet)))
    add_two(store, plugin, names=('main', 'east', 'west'), key=master_key(store.db, settings()))
    with app_of(store, tmp_path, plugin) as c:
        instances = c.app.state.instances
        instances.set_checks('demo', 'east', ['pulse', 'quiet'], 'alice')
        instances.set_checks('demo', 'west', ['quiet'], 'alice')
        SEEN.clear()

        # when
        moved = c.patch('/api/admin/plugins/demo/instances/east', json={'primary': True}, headers=AUTH)
        c.app.state.runtime.refresh()
        c.app.state.sweeper.run('demo', 'pulse', NOW)
        after_move = {i.name: sorted(i.off_checks) for i in instances.all('demo')}
        ran = list(SEEN.get('runs', []))
        c.patch('/api/admin/plugins/demo/instances/east', json={'offChecks': ['pulse', 'quiet']}, headers=AUTH)
        gone = c.delete('/api/admin/plugins/demo/instances/east', headers=AUTH)
        after_delete = {i.name: (i.primary, sorted(i.off_checks)) for i in instances.all('demo')}

    # then
    assert (moved.status_code, gone.status_code) == (200, 204)
    assert after_move == {'main': [], 'east': ['quiet'], 'west': ['quiet']}, 'east takes the once check as the old primary had it and keeps its own other checks'
    assert ran == ['east'], 'the once check runs on the new primary without anyone ticking its box again'
    assert after_delete == {'main': (True, ['pulse']), 'west': (False, ['quiet'])}, 'the heir of a deleted primary takes its once choices too'


def test_a_settings_source_that_moves_the_primary_rescopes_the_cards_and_hands_over_the_once_choices_once(store, tmp_path, monkeypatch):
    # given
    monkeypatch.setattr(Runtime, 'loop', no_loop)
    box = {'data': listed('main')}
    sources = SourceSet([('feed', lambda: box['data'])], {'feed': box['data']})
    with sourced_app(store, tmp_path, sources) as c:
        runtime, sweeper, instances = c.app.state.runtime, c.app.state.sweeper, c.app.state.instances
        clock = [1000.0]
        sweeper.clock = lambda: clock[0]
        instances.set_checks('demo', 'main', ['quiet'], 'alice')
        PLAN.update(main={'ran': ('demo:x',), 'cards': ('demo:x',)}, east={'ran': ('demo:y',), 'cards': ('demo:y',)})
        sweeper.run('demo', 'pulse', NOW)
        before = by_key(store.load())

        # when
        handled, hook = [], instances.on_primary_moved
        instances.on_primary_moved = lambda plugin_id, old, new: (handled.append((plugin_id, old.name, new.name)), hook(plugin_id, old, new))
        follower = Follower(sources, runtime.live, instances, {})
        box['data'] = listed('east')
        follower.tick()
        runtime.refresh()
        moved = by_key(store.load())
        chosen = {i.name: (i.primary, sorted(i.off_checks)) for i in instances.all('demo')}
        PLAN.update(main={'ran': ('demo:w',), 'cards': ('demo:w',)}, east={'ran': ('demo:z',), 'cards': ('demo:z',)})
        clock[0] += WINDOW_S + 1
        sweeper.run('demo', 'pulse', NOW + 900)
        runtime.refresh()
        sources.refresh()
        runtime.refresh()
        again = by_key(store.load())

    # then
    old, promoted = card('main', 'demo:x')['key'], card('east', 'demo:y')['key'] + '@east'
    assert (before[old].get('cluster'), before[promoted]['cluster']) == (None, 'east')
    assert {k: (a['status'], (a.get('note') or {}).get('text')) for k, a in moved.items()} == {old: ('cleared', msg('cards.rescoped', plugin='Demo', instance='main')),
                                                                                                promoted: ('cleared', msg('cards.rescoped', plugin='Demo', instance='east'))}
    assert chosen == {'east': (True, ['quiet']), 'main': (False, ['quiet'])}, 'east takes the once choice the old primary had'
    assert handled == [('demo', 'main', 'east')], 'the runtime refresh and the follower see one move'
    fresh = {card('east', 'demo:z')['key']: None, card('main', 'demo:w')['key'] + '@main': 'main'}
    assert {k: (again[k]['status'], again[k].get('cluster')) for k in fresh} == {k: ('open', cluster) for k, cluster in fresh.items()}, 'the move is handled once'


def test_one_settings_source_move_seen_by_two_replicas_is_rescoped_once(store, tmp_path, monkeypatch):
    # given
    monkeypatch.setattr(Runtime, 'loop', no_loop)
    box, runs = {'data': listed('main')}, []
    sets = [SourceSet([('feed', lambda: box['data'])], {'feed': box['data']}) for _ in range(2)]
    with sourced_app(store, tmp_path, sets[0]) as one, sourced_app(store, tmp_path, sets[1]) as two:
        for c in (one, two):
            real = c.app.state.sweeper.primary_changed
            monkeypatch.setattr(c.app.state.sweeper, 'primary_changed', lambda plugin_id, old, new, now, real=real: (runs.append((plugin_id, old.name, new.name)), real(plugin_id, old, new, now))[1])
        PLAN.update(main={'ran': ('demo:x',), 'cards': ('demo:x',)}, east={'ran': ('demo:y',), 'cards': ('demo:y',)})
        one.app.state.sweeper.run('demo', 'pulse', NOW)

        # when
        box['data'] = listed('east')
        for c, sources in ((one, sets[0]), (two, sets[1])):
            sources.refresh()
            c.app.state.runtime.refresh()
        for c in (one, two):
            c.app.state.runtime.refresh()
        moved = by_key(store.load())
        primaries = [c.app.state.instances.primary('demo').name for c in (one, two)]

    # then
    assert primaries == ['east', 'east']
    assert runs == [('demo', 'main', 'east')], 'only the replica that wins the claim rescopes the cards'
    assert sorted(a['status'] for a in moved.values()) == ['cleared', 'cleared']


def test_a_source_move_whose_rescope_fails_is_retried_on_the_next_refresh(store, tmp_path, monkeypatch, caplog):
    # given
    monkeypatch.setattr(Runtime, 'loop', no_loop)
    box, tries = {'data': listed('main')}, []
    sources = SourceSet([('feed', lambda: box['data'])], {'feed': box['data']})
    with sourced_app(store, tmp_path, sources) as c:
        runtime, sweeper = c.app.state.runtime, c.app.state.sweeper
        real = sweeper.primary_changed

        def flaky(plugin_id, old, new, now):
            tries.append(new.name)
            if len(tries) in (1, 3):
                raise RuntimeError('state store busy')
            return real(plugin_id, old, new, now)

        monkeypatch.setattr(sweeper, 'primary_changed', flaky)
        PLAN.update(main={'ran': ('demo:x',), 'cards': ('demo:x',)}, east={'ran': ('demo:y',), 'cards': ('demo:y',)})
        sweeper.run('demo', 'pulse', NOW)

        # when
        box['data'] = listed('east')
        sources.refresh()
        with caplog.at_level(logging.WARNING):
            runtime.refresh()
            failed = by_key(store.load())
            runtime.refresh()
            retried = by_key(store.load())
            runtime.refresh()
            once = list(tries)
            box['data'] = listed('main')
            sources.refresh()
            runtime.refresh()
            runtime.refresh()
        warned = [r.getMessage() for r in caplog.records if 'primary instance of plug-in' in r.getMessage()]

    # then
    assert sorted(a['status'] for a in failed.values()) == ['open', 'open']
    assert sorted(a['status'] for a in retried.values()) == ['cleared', 'cleared']
    assert once == ['east', 'east'], 'the failed rescope released its claim and ran again on the next refresh, and only then'
    assert tries == ['east', 'east', 'main', 'main']
    assert warned == [msg('instances.primary_move_failed', plugin='demo', error='RuntimeError')] * 2, 'a completed move resets the warning, so a later failing move logs again'
    assert 'state store busy' not in caplog.text


def test_a_primary_move_another_replica_made_through_the_api_is_not_handled_again_by_a_refresh(store, tmp_path, monkeypatch):
    # given
    monkeypatch.setattr(Runtime, 'loop', no_loop)
    plugin = moving_demo()
    key = master_key(store.db, settings())
    add_two(store, plugin, key=key)
    with app_of(store, tmp_path, plugin) as c:
        runtime, instances = c.app.state.runtime, c.app.state.instances
        PLAN.update(main={'ran': ('demo:x',), 'cards': ('demo:x',)}, east={'ran': ('demo:y',), 'cards': ('demo:y',)})
        c.app.state.sweeper.run('demo', 'pulse', NOW)
        other = Instances(store.db, Registry.of(plugin, directory=HERE), lambda: Sealer(key))
        other.refresh()

        # when
        other.make_primary('demo', 'east', 'alice')
        runtime.refresh()
        after = by_key(store.load())

    # then
    assert instances.primary('demo').name == 'east'
    assert sorted(a['status'] for a in after.values()) == ['open', 'open'], 'the replica that moved it through the API rescopes the cards itself'


def test_a_settings_source_move_across_a_restart_is_handled_once(store, tmp_path, monkeypatch):
    # given
    monkeypatch.setattr(Runtime, 'loop', no_loop)
    runs, real = [], Sweeper.primary_changed
    monkeypatch.setattr(Sweeper, 'primary_changed', lambda self, plugin_id, old, new, now: (runs.append((plugin_id, old.name, new.name)), real(self, plugin_id, old, new, now))[1])
    before, after = listed('main'), listed('east')
    with sourced_app(store, tmp_path, SourceSet([('feed', lambda: before)], {'feed': before})) as c:
        c.app.state.instances.set_checks('demo', 'main', ['quiet'], 'alice')
        PLAN.update(main={'ran': ('demo:x',), 'cards': ('demo:x',)}, east={'ran': ('demo:y',), 'cards': ('demo:y',)})
        c.app.state.sweeper.run('demo', 'pulse', NOW)

    # when
    with sourced_app(store, tmp_path, SourceSet([('feed', lambda: after)], {'feed': after})) as c:
        c.app.state.runtime.refresh()
        moved = by_key(store.load())
        chosen = {i.name: (i.primary, sorted(i.off_checks)) for i in c.app.state.instances.all('demo')}
    with sourced_app(store, tmp_path, SourceSet([('feed', lambda: after)], {'feed': after})) as c:
        c.app.state.runtime.refresh()

    # then
    assert runs == [('demo', 'main', 'east')], 'the move that arrived with the restart is handled once, and not again at the next start'
    assert sorted(a['status'] for a in moved.values()) == ['cleared', 'cleared']
    assert chosen == {'east': (True, ['quiet']), 'main': (False, ['quiet'])}


def test_an_admin_move_then_a_restart_is_not_handled_again(store, tmp_path, monkeypatch):
    # given
    monkeypatch.setattr(Runtime, 'loop', no_loop)
    runs, real = [], Sweeper.primary_changed
    monkeypatch.setattr(Sweeper, 'primary_changed', lambda self, plugin_id, old, new, now: (runs.append((plugin_id, old.name, new.name)), real(self, plugin_id, old, new, now))[1])
    plugin = moving_demo()
    add_two(store, plugin, key=master_key(store.db, settings()))
    with app_of(store, tmp_path, plugin) as c:
        clock = [1000.0]
        c.app.state.sweeper.clock = lambda: clock[0]
        PLAN.update(main={'ran': ('demo:x',), 'cards': ('demo:x',)}, east={'ran': ('demo:y',), 'cards': ('demo:y',)})
        c.app.state.sweeper.run('demo', 'pulse', NOW)
        moved = c.patch('/api/admin/plugins/demo/instances/east', json={'primary': True}, headers=AUTH)
        clock[0] += WINDOW_S + 1
        PLAN.update(main={'ran': ('demo:w',), 'cards': ('demo:w',)}, east={'ran': ('demo:z',), 'cards': ('demo:z',)})
        c.app.state.sweeper.run('demo', 'pulse', NOW + 900)

    # when
    with app_of(store, tmp_path, plugin) as c:
        c.app.state.runtime.refresh()
        after = by_key(store.load())

    # then
    assert moved.status_code == 200 and runs == [('demo', 'main', 'east')], 'only the PATCH route rescoped'
    fresh = {card('east', 'demo:z')['key']: None, card('main', 'demo:w')['key'] + '@main': 'main'}
    assert {k: (after[k]['status'], after[k].get('cluster')) for k in fresh} == {k: ('open', cluster) for k, cluster in fresh.items()}


def test_an_admin_delete_of_the_primary_is_rescoped_once_across_a_refresh_and_a_restart(store, tmp_path, monkeypatch):
    # given
    monkeypatch.setattr(Runtime, 'loop', no_loop)
    runs, real = [], Sweeper.primary_changed
    monkeypatch.setattr(Sweeper, 'primary_changed', lambda self, plugin_id, old, new, now: (runs.append((plugin_id, old.name, new.name)), real(self, plugin_id, old, new, now))[1])
    plugin = moving_demo()
    add_two(store, plugin, key=master_key(store.db, settings()))
    with app_of(store, tmp_path, plugin) as c:
        PLAN.update(main={'ran': ('demo:x',), 'cards': ('demo:x',)}, east={'ran': ('demo:y',), 'cards': ('demo:y',)})
        c.app.state.sweeper.run('demo', 'pulse', NOW)

        # when
        gone = c.delete('/api/admin/plugins/demo/instances/main', headers=AUTH)
        c.app.state.runtime.refresh()
        c.app.state.runtime.refresh()
    with app_of(store, tmp_path, plugin) as c:
        c.app.state.runtime.refresh()
        primary = c.app.state.instances.primary('demo').name

    # then
    assert (gone.status_code, primary) == (204, 'east')
    assert runs == [('demo', 'main', 'east')], 'only the DELETE route rescoped; the record it wrote stops a second one'


class FailingOnce:
    def __init__(self, col, when):
        self.col, self.when, self.failed = col, when, False

    def __getattr__(self, name):
        return getattr(self.col, name)

    def update_one(self, query, change, *args, **kwargs):
        if not self.failed and self.when(query):
            self.failed = True
            raise RuntimeError('database blip')
        return self.col.update_one(query, change, *args, **kwargs)

    def find(self, *args, **kwargs):
        if not self.failed and self.when(None):
            self.failed = True
            raise RuntimeError('database blip')
        return self.col.find(*args, **kwargs)


def test_a_failed_rescope_whose_release_also_fails_is_claimed_again_with_its_token(store, tmp_path, monkeypatch, caplog):
    # given
    monkeypatch.setattr(Runtime, 'loop', no_loop)
    box, tries = {'data': listed('main')}, []
    sources = SourceSet([('feed', lambda: box['data'])], {'feed': box['data']})
    with sourced_app(store, tmp_path, sources) as c:
        runtime, sweeper, instances = c.app.state.runtime, c.app.state.sweeper, c.app.state.instances
        real = sweeper.primary_changed

        def flaky(plugin_id, old, new, now):
            tries.append(new.name)
            if len(tries) == 1:
                raise RuntimeError('state store busy')
            return real(plugin_id, old, new, now)

        monkeypatch.setattr(sweeper, 'primary_changed', flaky)
        monkeypatch.setattr(instances, 'moves', FailingOnce(instances.moves, lambda query: query is not None and set(query) == {'_id', 'claim'}))
        PLAN.update(main={'ran': ('demo:x',), 'cards': ('demo:x',)}, east={'ran': ('demo:y',), 'cards': ('demo:y',)})
        sweeper.run('demo', 'pulse', NOW)

        # when
        box['data'] = listed('east')
        sources.refresh()
        with caplog.at_level(logging.WARNING):
            runtime.refresh()
            stuck = (instances.moves.find_one({'_id': 'demo'}) or {}).get('primary')
            failed = by_key(store.load())
            runtime.refresh()
            retried = by_key(store.load())
            runtime.refresh()

    # then
    assert instances.moves.failed and stuck == 'east', 'the release failed, so the record still names the new primary under the claim'
    assert sorted(a['status'] for a in failed.values()) == ['open', 'open']
    assert sorted(a['status'] for a in retried.values()) == ['cleared', 'cleared']
    assert tries == ['east', 'east'], 'the token kept for the move claims it again on the next refresh'


def test_a_move_warning_starts_again_whenever_a_plugin_is_marked_done_and_an_unread_record_has_its_own_text(store, tmp_path, monkeypatch, caplog):
    # given
    monkeypatch.setattr(Runtime, 'loop', no_loop)
    box, tries = {'data': listed('main')}, []
    sources = SourceSet([('feed', lambda: box['data'])], {'feed': box['data']})
    with sourced_app(store, tmp_path, sources) as c:
        runtime, sweeper, instances = c.app.state.runtime, c.app.state.sweeper, c.app.state.instances
        real = sweeper.primary_changed

        def flaky(plugin_id, old, new, now):
            tries.append(new.name)
            if len(tries) in (1, 2):
                raise RuntimeError('state store busy')
            return real(plugin_id, old, new, now)

        monkeypatch.setattr(sweeper, 'primary_changed', flaky)

        # when
        with caplog.at_level(logging.WARNING):
            box['data'] = listed('east')
            sources.refresh()
            runtime.refresh()
            instances.moves.update_one({'_id': 'demo'}, {'$set': {'primary': 'east'}})
            runtime.refresh()
            box['data'] = listed('main')
            sources.refresh()
            monkeypatch.setattr(instances, 'moves', FailingOnce(instances.moves, lambda query: query is None))
            runtime.refresh()
            runtime.refresh()
            runtime.refresh()
        said = [r.getMessage() for r in caplog.records if r.name == 'skopax.instances']

    # then
    assert tries == ['east', 'main', 'main'], 'the first move was taken over by another replica; the second failed once and was retried'
    assert said == [msg('instances.primary_move_failed', plugin='demo', error='RuntimeError'), msg('instances.primary_records_unread', error='RuntimeError'),
                    msg('instances.primary_move_failed', plugin='demo', error='RuntimeError')]
    assert 'moved' not in msg('instances.primary_records_unread', error='RuntimeError')


MOVE_FAILED, NOT_RESET = 'instances.primary_move_failed', 'instances.primary_record_not_reset'


@pytest.mark.parametrize('away, failing, logged', [
    pytest.param(False, 0, (MOVE_FAILED,), id='the-source-moves-it-back'),
    pytest.param(False, 1, (MOVE_FAILED,), id='the-source-moves-it-back-after-a-failed-release'),
    pytest.param(False, 2, (MOVE_FAILED, NOT_RESET), id='the-release-back-fails-once-and-is-tried-again'),
    pytest.param(True, 0, (MOVE_FAILED,), id='the-plugin-loses-every-primary-then-gets-the-same-one-back'),
])
def test_a_held_move_whose_old_primary_is_primary_again_is_released_without_a_rescope(store, tmp_path, monkeypatch, caplog, away, failing, logged):
    # given
    monkeypatch.setattr(Runtime, 'loop', no_loop)
    caplog.set_level(logging.WARNING)
    box, tries = {'data': listed('main')}, []
    sources = SourceSet([('feed', lambda: box['data'])], {'feed': box['data']})
    with sourced_app(store, tmp_path, sources) as c:
        runtime, sweeper, instances = c.app.state.runtime, c.app.state.sweeper, c.app.state.instances
        real = sweeper.primary_changed

        def flaky(plugin_id, old, new, now):
            tries.append((old.name, new.name))
            if len(tries) == 1:
                raise RuntimeError('state store busy')
            return real(plugin_id, old, new, now)

        monkeypatch.setattr(sweeper, 'primary_changed', flaky)
        for _ in range(failing):
            monkeypatch.setattr(instances, 'moves', FailingOnce(instances.moves, lambda query: query is not None and set(query) == {'_id', 'claim'}))
        PLAN.update(main={'ran': ('demo:x',), 'cards': ('demo:x',)}, east={'ran': ('demo:y',), 'cards': ('demo:y',)})
        sweeper.run('demo', 'pulse', NOW)
        box['data'] = listed('east')
        sources.refresh()
        runtime.refresh()
        held = list(instances._held)
        if away:
            box['data'] = SourceData.of({'instances': []}, 'feed')
            sources.refresh()
            runtime.refresh()

        # when
        box['data'] = listed('main')
        sources.refresh()
        runtime.refresh()
        runtime.refresh()
        record = {k: v for k, v in instances.moves.find_one({'_id': 'demo'}).items() if k not in ('_id', 'at')}
        after = by_key(store.load())
    said = [r.getMessage() for r in caplog.records if r.name == 'skopax.instances']

    # then
    assert held == ['demo'] and tries == [('main', 'east')], 'the move back is released by its token, so primary_changed never runs from main to main'
    assert {k: a['status'] for k, a in after.items()} == {card('main', 'demo:x')['key']: 'open', card('east', 'demo:y')['key'] + '@east': 'open'}
    assert record == {'primary': 'main'} and not instances._held
    assert said == [msg(key, plugin='demo', error='RuntimeError') for key in logged]
    assert 'database blip' not in caplog.text and 'state store busy' not in caplog.text


def test_run_now_of_a_once_check_looks_at_the_primary_only(store, tmp_path, monkeypatch):
    # given
    monkeypatch.setattr(Runtime, 'loop', no_loop)
    add_two(store, once_demo(), key=master_key(store.db, settings()))
    nudged = []
    with app_of(store, tmp_path, once_demo()) as c:
        monkeypatch.setattr(c.app.state.scheduler, 'nudge', nudged.append)
        c.patch('/api/admin/plugins/demo/instances/main', json={'offChecks': ['pulse']}, headers=AUTH)
        c.app.state.runtime.refresh()
        SEEN.clear()

        # when
        unchecked = c.post('/api/checks/demo/pulse/run', headers=AUTH)
        refused = list(nudged)
        c.patch('/api/admin/plugins/demo/instances/main', json={'offChecks': []}, headers=AUTH)
        c.app.state.runtime.refresh()
        nudged.clear()
        checked = c.post('/api/checks/demo/pulse/run', headers=AUTH)
        c.app.state.sweeper.run('demo', 'pulse', NOW)
        ran, asked = list(SEEN['runs']), list(nudged)
        c.patch('/api/admin/plugins/demo/instances/main', json={'enabled': False}, headers=AUTH)
        c.app.state.runtime.refresh()
        nudged.clear()
        stopped = c.post('/api/checks/demo/pulse/run', headers=AUTH)

    # then
    assert (unchecked.status_code, unchecked.json().get('detail'), refused) == (409, msg('checks.unchecked_once', check='Demo pulse', name='Demo'), []), 'east has the box ticked, but a once check runs on the primary only'
    assert unchecked.json()['detail'] == 'Demo pulse is switched off on the primary instance of Demo'
    assert (checked.status_code, asked) == (202, ['demo.pulse'])
    assert ran == ['main'], 'the nudged job runs on the primary'
    assert (stopped.status_code, stopped.json().get('detail'), nudged) == (409, msg('checks.primary_not_serving', check='Demo pulse', name='Demo'), [])
    assert stopped.json()['detail'] == 'The primary instance of Demo is not serving, and Demo pulse runs on the primary only'


def test_a_once_check_toggle_counts_only_on_the_primary(store, tmp_path, monkeypatch):
    # given
    monkeypatch.setattr(Runtime, 'loop', no_loop)
    plugin = dataclasses.replace(DEMO, checks=(Check('pulse', 'Demo pulse', 900, 0, ('demo',), once_pulse, sections=('members',), once=True), Check('quiet', 'Quiet', 900, 0, ('quiet',), quiet)))
    add_two(store, plugin, key=master_key(store.db, settings()))
    with app_of(store, tmp_path, plugin) as c:

        # when
        row = next(p for p in c.get('/api/admin/plugins', headers=AUTH).json()['plugins'] if p['id'] == 'demo')
        east = c.patch('/api/admin/plugins/demo/instances/east', json={'offChecks': ['pulse', 'quiet']}, headers=AUTH)
        added = c.post('/api/admin/plugins/demo/instances', json={'name': 'west', 'values': {'url': 'https://demo.example.com'}, 'enabled': True, 'offChecks': ['pulse']}, headers=AUTH)
        main = c.patch('/api/admin/plugins/demo/instances/main', json={'offChecks': ['pulse']}, headers=AUTH)
        stored = {i.name: sorted(i.off_checks) for i in c.app.state.instances.all('demo')}
        lines = [d['text'] for d in store.activity.find()]

    # then
    assert [(x['id'], x.get('once')) for x in row['checks']] == [('pulse', True), ('quiet', None)]
    assert (east.status_code, added.status_code, main.status_code) == (200, 201, 200)
    assert east.json()['instance']['offChecks'] == ['quiet'] and added.json()['offChecks'] == []
    assert stored == {'main': ['pulse'], 'east': ['quiet'], 'west': []}
    assert [line for line in lines if 'Demo pulse' in line] == [msg('integrations.check_off', plugin='Demo', instance='main', check='Demo pulse')]


@pytest.mark.parametrize('flag', ['once', 'legacy_alias'])
def test_a_check_flag_that_is_not_a_bool_breaks_its_plugin(flag):
    # given
    plugin = dataclasses.replace(DEMO, checks=(dataclasses.replace(DEMO.checks[0], **{flag: 'false'}),))

    # when
    found = Registry.of(plugin, directory=HERE).get('demo')

    # then
    assert found.state == 'broken' and found.reason.endswith(msg('plugins.why.shape', what=f'check {flag}'))


def test_the_heal_reads_a_twin_pass_by_its_longest_check_id():
    # given
    passes = dict.fromkeys(('tick-cache', 'tick-cache-cache'), 'ok')
    state = {'sources': dict(passes), 'serverPasses': {k: {'at': '2026-10-26T08:00:00Z', 'status': v} for k, v in passes.items()}}

    # when
    Sweeper(Serving(CACHE, SimpleNamespace(name='cache', primary=False)), None, lambda *a, **k: None)._heal(state, CACHE)

    # then
    assert set(state['sources']) == set(state['serverPasses']) == {'tick-cache-cache'}


def test_links_find_the_owner_by_the_stem_of_its_ran_key(store):
    # given
    cached = dataclasses.replace(CACHE, links=cache_links, after=('linker',))
    add_two(store, LINKED)
    add_instance(store, LINKER, 'main', sealer_key=KEY, primary=True, url='https://logs.example.com')
    add_two(store, cached, names=('main', 'east'))
    runtime = runtime_of(store, LINKER, LINKED, cached)
    labels = lambda card: [x['label'] for x in runtime.links(card) or []]

    # when
    sub_key = labels({'key': 'k', 'src': 'zzz', 'ranKey': 'demo:a-east', 'cluster': 'east'})
    twin = labels({'key': 'k', 'src': 'zzz', 'ranKey': 'tick-cache-east', 'cluster': 'east'})
    unowned = labels({'key': 'k', 'src': 'zzz', 'cluster': 'east'})

    # then
    assert [p.id for p in runtime.active()] == ['linker', 'cache-demo', 'demo']
    assert sub_key == ['Demo east', 'Logs', 'Cache main']
    assert twin == ['Cache east', 'Logs', 'Demo main']
    assert unowned == ['Logs', 'Cache main', 'Demo main']
