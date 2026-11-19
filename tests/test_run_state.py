import threading

import pytest

from skopax.contract import RUN_STATE_KEY_MAX, RUN_STATE_KEYS, RUN_STATE_VALUE_MAX, Check, Field, PassResult, RunState, Sweep, Tool
from skopax.instances import Instances
from skopax.plugintools import PluginTools
from skopax.registry import Registry
from skopax.runtime import Runtime
from skopax.sealer import Sealer
from skopax.sweeps import Sweeper
from tests.plugins.base import minimal
from tests.test_admin_plugins import HERE
from tests.test_plugin_tools import NOW, SCHEMA, Rig

SEEN: list[int] = []


def counted(ctx, args):
    cached = ctx.run_state.get('cached')
    if cached is None:
        ctx.run_state.put('cached', f'read once at call {ctx.run_state.count("calls", 0) + 1}')
    return {'calls': ctx.run_state.count('calls'), 'cached': ctx.run_state.get('cached')}


def other(ctx, args):
    return {'calls': ctx.run_state.count('calls')}


def check(ctx):
    SEEN.append(ctx.run_state.count('passes'))
    SEEN.append(ctx.run_state.count('passes'))
    return Sweep(PassResult('counting', True, 'ok', [], {}, ('counting',)))


COUNTER = minimal('counter', 'Counter', fields=(Field('url', 'URL', 'url', required=True),),
                  tools=(Tool('counter_read', 'Counts its calls in a run', SCHEMA, counted, instance_input=None),),
                  checks=(Check('counting', 'Counting', 300, 0, ('counting',), check),))
OTHER = minimal('other', 'Other', fields=(Field('url', 'URL', 'url', required=True),), tools=(Tool('other_read', 'Counts its own calls', SCHEMA, other, instance_input=None),))


@pytest.fixture
def rig(store):
    found = Rig(store)
    found.registry = Registry.of(COUNTER, OTHER, directory=HERE)
    found.instances = Instances(store.db, found.registry, lambda: Sealer(found.key))
    found.runtime = Runtime(found.registry, found.state, found.instances)
    found.tools = PluginTools(found.runtime)
    found.add(COUNTER, 'main', primary=True)
    found.add(OTHER, 'main', primary=True)
    return found


def test_tool_calls_of_one_run_share_one_store_and_the_next_run_starts_empty(rig):
    # given
    first, second = rig.box(), rig.box()

    # when
    one = [first.call('counter_read', {})[0] for _ in range(3)]
    mine = first.call('other_read', {})[0]
    fresh = second.call('counter_read', {})[0]

    # then
    assert ['"calls": 1' in one[0], '"calls": 2' in one[1], '"calls": 3' in one[2]] == [True, True, True]
    assert all('read once at call 1' in text for text in one)
    assert '"calls": 1' in mine, 'each plug-in has its own store in a run'
    assert '"calls": 1' in fresh and 'read once at call 1' in fresh


def test_two_runs_at_once_on_two_threads_never_see_each_other(rig):
    # given
    boxes = [rig.box() for _ in range(4)]
    got: dict[int, list[str]] = {}
    gate = threading.Barrier(4)

    def run(n: int) -> None:
        gate.wait(5)
        got[n] = [boxes[n].call('counter_read', {})[0] for _ in range(5)]

    threads = [threading.Thread(target=run, args=(n,)) for n in range(4)]

    # when
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)

    # then
    assert all([f'"calls": {i + 1}' in text for i, text in enumerate(got[n])] == [True] * 5 for n in range(4))


def test_each_check_run_gets_a_fresh_store(rig, store):
    # given
    SEEN.clear()
    store.save({'anomalies': [], 'history': [], 'initialized': True}, None)
    sweeper = Sweeper(rig.runtime, store, lambda results, now, links, after=None, **kw: None)

    # when
    sweeper.run('counter', 'counting', NOW)
    sweeper.run('counter', 'counting', NOW + 300)

    # then
    assert SEEN == [1, 2, 1, 2]


def test_a_context_built_without_a_run_gets_its_own_store(rig):
    # given
    found = rig.runtime.instances_of('counter')[0]

    # when
    one, two = rig.runtime.context(COUNTER, found, NOW), rig.runtime.context(COUNTER, found, NOW)
    one.run_state.count('x')

    # then
    assert one.run_state is not two.run_state and two.run_state.get('x') is None


def test_the_store_counts_safely_across_threads_and_keeps_only_its_newest_keys():
    # given
    state = RunState()
    threads = [threading.Thread(target=lambda: [state.count('hits') for _ in range(500)]) for _ in range(8)]

    # when
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)
    for i in range(RUN_STATE_KEYS + 5):
        state.put(f'k{i}', i)

    # then
    assert state.get('hits') is None, 'the oldest keys gave way'
    assert state.get('k4') is None and state.get('k5') == 5 and state.get(f'k{RUN_STATE_KEYS + 4}') == RUN_STATE_KEYS + 4
    assert len(state) == RUN_STATE_KEYS


def test_the_counts_reach_their_total_and_a_count_of_zero_only_reads():
    # given
    state = RunState()
    threads = [threading.Thread(target=lambda: [state.count('hits') for _ in range(500)]) for _ in range(8)]

    # when
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)

    # then
    assert state.count('hits', 0) == 4000 and state.count('none', 0) == 0 and state.get('none', 'left') == 'left'


@pytest.mark.parametrize('key', ['', 'x' * (RUN_STATE_KEY_MAX + 1), 7, None])
def test_a_key_that_is_not_a_short_text_is_refused(key):
    # given
    state = RunState()

    # when
    with pytest.raises(ValueError):
        state.put(key, 1)
    with pytest.raises(ValueError):
        state.count(key)

    # then
    assert len(state) == 0


def test_a_count_on_a_key_that_holds_something_else_is_refused():
    # given
    state = RunState()
    state.put('cached', {'rows': 3})

    # when
    with pytest.raises(ValueError):
        state.count('cached')

    # then
    assert state.get('cached') == {'rows': 3}


def test_a_value_over_its_size_cap_is_refused_and_the_old_one_stays():
    # given
    state = RunState()
    state.put('rows', ['small'])

    # when
    with pytest.raises(ValueError) as e:
        state.put('rows', ['x' * RUN_STATE_VALUE_MAX])
    state.put('near', 'y' * (RUN_STATE_VALUE_MAX - 10))

    # then
    assert 'at most 64 KiB' in str(e.value) and state.get('rows') == ['small'] and len(state.get('near')) == RUN_STATE_VALUE_MAX - 10


def test_a_stored_value_is_a_copy_so_a_later_change_cannot_pass_the_cap():
    # given
    state = RunState()
    rows = ['small']
    state.put('rows', rows)

    # when
    rows.append('x' * RUN_STATE_VALUE_MAX)
    kept = state.get('rows')

    # then
    assert kept == ['small']


@pytest.mark.parametrize('value', [{1, 2}, object(), {(1, 2): 'x'}, float('nan')])
def test_a_value_that_is_not_json_data_is_refused(value):
    # given
    state = RunState()

    # when
    with pytest.raises(ValueError) as e:
        state.put('odd', value)

    # then
    assert 'JSON data' in str(e.value) and state.get('odd') is None
