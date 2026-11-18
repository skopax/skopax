import dataclasses
import logging
from types import SimpleNamespace

from skopax import sections
from skopax.contract import Check, PassResult, Sweep
from skopax.sweeps import Sweeper
from tests.plugins import demo_plugin

MARKER = 'orders-db-0'
ROW = {'part': 'volumes', 'ns': 'shop', 'pvc': 'data-0', 'pct': 40}
CHECK = Check('cap', 'Capacity', 900, 0, ('cap',), lambda ctx: Sweep(PassResult('cap', True, 'ok', [], {}, ())), sections=('capacity', 'redis', 'kafka'))
PLUGIN = dataclasses.replace(demo_plugin.PLUGIN, checks=(CHECK,))


def stored() -> dict:
    return {'at': '2026-10-01T00:00:00Z', 'volumes': [{'ns': 'shop', 'pvc': MARKER, 'pct': 91}]}


def warned(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]


class Serving:
    def __init__(self, plugin, *instances):
        self.found = [(plugin, list(instances))]

    def serving(self):
        return self.found


def test_merge_rows_reads_a_stored_dict_section_as_no_rows_and_warns_once(caplog):
    # given
    state = {'capacity': stored()}

    # when
    with caplog.at_level(logging.WARNING):
        sections.merge_rows(state, 'capacity', [dict(ROW)], 'demo', None)

    # then
    assert state['capacity'] == [dict(ROW, plugin='demo')]
    assert len(warned(caplog)) == 1 and 'capacity' in warned(caplog)[0]
    assert MARKER not in caplog.text


def test_drop_rows_reads_a_stored_dict_section_as_no_rows_and_warns_once(caplog):
    # given
    state = {'capacity': stored()}

    # when
    with caplog.at_level(logging.WARNING):
        sections.drop_rows(state, 'capacity', 'demo', 'east')

    # then
    assert state['capacity'] == []
    assert len(warned(caplog)) == 1 and 'capacity' in warned(caplog)[0]
    assert MARKER not in caplog.text


def test_a_heal_reads_stored_dict_sections_as_no_rows_and_warns_once_per_section(caplog):
    # given
    kept = {'part': 'groups', 'group': 'billing', 'plugin': 'demo'}
    state = {'capacity': stored(), 'redis': {'instances': [{'pod': MARKER}]}, 'kafka': [dict(kept)]}

    # when
    with caplog.at_level(logging.WARNING):
        Sweeper(Serving(PLUGIN, SimpleNamespace(name='main', primary=True)), None, lambda *a, **k: None)._heal(state, PLUGIN)

    # then
    assert (state['capacity'], state['redis'], state['kafka']) == ([], [], [kept])
    assert sorted(next(s for s in ('capacity', 'redis') if s in m) for m in warned(caplog)) == ['capacity', 'redis']
    assert MARKER not in caplog.text and not any('kafka' in m for m in warned(caplog))


def test_every_reader_of_a_stored_dict_section_warns_once_however_often_it_is_read(caplog):
    # given
    sweeper = Sweeper(Serving(PLUGIN, SimpleNamespace(name='main', primary=True)), None, lambda *a, **k: None)

    # when
    with caplog.at_level(logging.WARNING):
        for _ in range(3):
            read = sections.stored_rows({'capacity': stored()}, 'capacity')
            sections.merge_rows({'capacity': stored()}, 'capacity', [dict(ROW)], 'demo', None)
            sections.drop_rows({'capacity': stored()}, 'capacity', 'demo', 'east')
            sweeper._heal({'capacity': stored()}, PLUGIN)

    # then
    assert read == []
    assert len(warned(caplog)) == 1 and 'capacity' in warned(caplog)[0]
    assert not hasattr(Sweeper, '_stored_rows'), 'one reader of stored rows, in sections'


def test_a_missing_or_list_section_merges_and_drops_without_a_warning(caplog):
    # given
    state = {'kafka': [{'part': 'groups', 'group': 'billing', 'plugin': 'demo'}]}

    # when
    with caplog.at_level(logging.WARNING):
        sections.merge_rows(state, 'capacity', [dict(ROW)], 'demo', None)
        sections.drop_rows(state, 'kafka', 'demo', None)
        sections.drop_rows(state, 'redis', 'demo', None)

    # then
    assert state == {'kafka': [], 'capacity': [dict(ROW, plugin='demo')]}
    assert warned(caplog) == []
