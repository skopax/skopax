import logging
import threading
import time
from pathlib import Path

from skopax.contract import Check, PassResult, Sweep
from skopax.engine import iso
from skopax.instances import Instances, PluginState
from skopax.registry import Registry
from skopax.runtime import Runtime
from skopax.sealer import Sealer
from skopax.sweeps import Reads
from tests.plugins import base
from tests.plugins.base import minimal

NOW = 1_792_224_000.0
SINCE = NOW - 86400


def run(ctx) -> Sweep:
    return Sweep(PassResult('notes', True, 'ok', [], {}, ('notes',)))


def noted(rows: list[dict], since: float) -> list[dict]:
    return [{'ns': 'notes', 'svc': r['name'], 'at': r['at'], 'fromImage': 'v1', 'toImage': 'v2', 'kind': 'other', 'plugin': 'other', 'cluster': 'other'} for r in rows]


def plugin(id: str, releases) -> object:
    return minimal(id, id.title(), checks=(Check(id, id.title(), 900, 0, (id,), run, sections=('notes',)),), releases=releases)


def runtime_of(store, *plugins, hook_timeout: float = 10) -> Runtime:
    reg = Registry.of(*plugins, directory=Path(base.__file__).parent)
    runtime = Runtime(reg, PluginState(store.db), Instances(store.db, reg, lambda: Sealer('k')), hook_timeout=hook_timeout)
    runtime.refresh()
    return runtime


def state_with(*rows: dict) -> dict:
    return {'anomalies': [], 'notes': list(rows)}


def row(plugin_id: str, name: str, at: float) -> dict:
    return {'plugin': plugin_id, 'name': name, 'at': iso(at)}


def test_release_rows_come_from_the_plugins_own_rows_with_its_kind_and_no_borrowed_keys(store):
    # given
    seen = []

    def kept(rows, since):
        seen.append((rows, since))
        return noted(rows, since) + [{'ns': 'notes', 'svc': 'no-image', 'at': iso(NOW)}, {'ns': 'notes', 'svc': 'bad-time', 'at': 'yesterday', 'toImage': 'v2'}, 'not a row']

    runtime = runtime_of(store, plugin('relnotes', kept))
    state = state_with(row('relnotes', 'fresh', NOW - 60), row('relnotes', 'old', NOW - 3 * 86400), row('other', 'theirs', NOW - 60), {'name': 'legacy', 'at': iso(NOW)})

    # when
    got = runtime.releases(state.get, SINCE)

    # then
    assert got == [{'ns': 'notes', 'svc': 'fresh', 'at': iso(NOW - 60), 'fromImage': 'v1', 'toImage': 'v2', 'kind': 'relnotes'}]
    assert [r['name'] for r in seen[0][0]] == ['fresh', 'old'] and seen[0][1] == SINCE, 'only its own rows, and core drops what is older than since'
    assert state['notes'][0] == row('relnotes', 'fresh', NOW - 60), 'the hook gets copies'


def test_release_rows_keep_only_the_allowed_keys_and_are_capped(store):
    # given
    long = 'x' * 500

    def wordy(rows, since):
        return [{'id': 'r-1', 'ns': 'notes', 'svc': 'c', 'at': iso(NOW), 'fromImage': 'v1', 'toImage': 'v2', 'title': long, 'note': 'not allowed', 'other': {'a': 1},
                 'wordy': {'changes': long, 'names': [long] * 50, 'deep': {'more': [{'text': long}]}, 'count': 3}}]

    runtime = runtime_of(store, plugin('wordy', wordy))

    # when
    [got] = runtime.releases(state_with(row('wordy', 'c', NOW)).get, SINCE)

    # then
    assert set(got) == {'id', 'ns', 'svc', 'at', 'fromImage', 'toImage', 'title', 'kind', 'wordy'}
    assert len(got['title']) == 200 and len(got['wordy']['changes']) == 200 and got['wordy']['count'] == 3
    assert len(got['wordy']['names']) == 20 and all(len(n) == 200 for n in got['wordy']['names'])
    assert got['wordy']['deep'] == {'more': [{'text': 'x' * 200}]}


def test_a_raising_or_slow_hook_drops_only_that_plugins_rows_with_one_warning(store, caplog):
    # given
    release = threading.Event()

    def boom(rows, since):
        raise RuntimeError('planted-text')

    def slow(rows, since):
        release.wait(5)
        return noted(rows, since)

    runtime = runtime_of(store, plugin('raiser', boom), plugin('sleeper', slow), plugin('tidy', noted), hook_timeout=1)
    state = state_with(row('raiser', 'a', NOW), row('sleeper', 'b', NOW), row('tidy', 'c', NOW))

    # when
    with caplog.at_level(logging.WARNING, logger='skopax.runtime'):
        first = runtime.releases(state.get, SINCE)
        again = runtime.releases(state.get, SINCE)
    release.set()

    # then
    assert [r['svc'] for r in first] == [r['svc'] for r in again] == ['c'], 'a slow hook ahead of a healthy one never takes its rows'
    warned = [r.getMessage() for r in caplog.records]
    assert len([w for w in warned if 'raiser' in w]) == 1 and len([w for w in warned if 'sleeper' in w]) == 1
    assert [p.id for p in runtime.active()] == ['raiser', 'sleeper', 'tidy'], 'the slow hook comes before the healthy one'
    assert not [w for w in warned if 'tidy' in w], 'the healthy plug-in is never blamed'
    assert 'RuntimeError' in caplog.text and 'planted-text' not in caplog.text, 'the kind only, never the text'


def test_readers_that_overlap_all_get_the_rows(store):
    # given
    def paced(rows, since):
        time.sleep(0.05)
        return noted(rows, since)

    runtime = runtime_of(store, plugin('relnotes', paced))
    state = state_with(row('relnotes', 'c', NOW))
    got: list = []

    # when
    readers = [threading.Thread(target=lambda: got.append(runtime.releases(state.get, SINCE))) for _ in range(3)]
    for t in readers:
        t.start()
    for t in readers:
        t.join(5)

    # then
    assert [[r['svc'] for r in rows] for rows in got] == [['c'], ['c'], ['c']]


def test_only_the_declared_sections_of_hook_plugins_are_read(store):
    # given
    asked: list = []
    state = state_with(row('relnotes', 'c', NOW))

    def read(name):
        asked.append(name)
        return state.get(name)

    without = runtime_of(store, plugin('quiet', None))
    with_hook = runtime_of(store, plugin('relnotes', noted))

    # when
    none = without.releases(read, SINCE)
    some = with_hook.releases(read, SINCE)

    # then
    assert none == [] and [r['svc'] for r in some] == ['c']
    assert asked == ['notes'], 'nothing is read when no serving plug-in has the hook, and only its own sections otherwise'


def test_a_check_reads_release_rows_next_to_the_deploys_and_none_are_stored(store):
    # given
    runtime = runtime_of(store, plugin('relnotes', noted))
    store.save(state_with(row('relnotes', 'c', NOW - 60)), None)
    store.add_deploys([{'ns': 'shop', 'svc': 'checkout-service', 'fromImage': 'aaaa1111', 'toImage': 'bbbb2222', 'at': iso(NOW - 120)}])
    reads = Reads(store, runtime.registry.usable()[0], None, runtime.releases)

    # when
    rows = reads.deploys(SINCE)
    plain = Reads(store, runtime.registry.usable()[0], None).deploys(SINCE)

    # then
    assert [(r['ns'], r['svc'], r.get('kind')) for r in rows] == [('shop', 'checkout-service', None), ('notes', 'c', 'relnotes')]
    assert [r['ns'] for r in plain] == ['shop']
    assert [d['ns'] for d in store.deploys_since(iso(SINCE))] == ['shop']


def test_a_plugin_without_the_hook_or_switched_off_adds_nothing(store):
    # given
    runtime = runtime_of(store, plugin('relnotes', noted), plugin('quiet', None))
    state = state_with(row('relnotes', 'c', NOW), row('quiet', 'q', NOW))

    # when
    on = runtime.releases(state.get, SINCE)
    runtime.state.set('relnotes', False, 'alice@example.com')
    runtime.state.refresh()
    off = runtime.releases(state.get, SINCE)

    # then
    assert [r['svc'] for r in on] == ['c'] and off == []
