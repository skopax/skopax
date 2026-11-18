import dataclasses
import datetime
import decimal

import pytest
from fastapi.testclient import TestClient

from skopax.app import create_app
from skopax.contract import Check, PassResult, Sweep
from skopax.kube import Kube
from skopax.registry import Registry
from skopax.sealer import master_key
from skopax.sweeps import _json, _storable
from tests.conftest import settings
from tests.plugin_kit import add_instance
from tests.test_sweeps import DEMO, HERE, NO_CARDS, NOW


def nested(depth: int):
    value: object = 'leaf'
    for _ in range(depth):
        value = [value]
    return value


GOOD = [
    {}, {'a': None, 'b': True, 'c': 'text', 'd': 5, 'e': 1.5, 'f': [1, 'x', {'g': []}]}, {'n': 2 ** 63 - 1}, {'n': -2 ** 63}, {'k': nested(30)},
]
BAD = [
    ('bytes', {'a': b'abc'}), ('non utf8 bytes', {'a': b'\xff\xfe'}), ('bytearray', {'a': bytearray(b'x')}), ('set', {'a': {1, 2}}), ('frozenset', {'a': frozenset({1})}), ('Decimal', {'a': decimal.Decimal('1.5')}),
    ('tuple', {'a': (1, 2)}), ('date', {'a': datetime.date(2026, 1, 1)}), ('aware datetime', {'a': datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc)}), ('int key', {1: 'a'}),
    ('bytes key', {b'a': 1}), ('tuple key', {(1, 2): 1}), ('2**63', 2 ** 63), ('-2**63-1', -2 ** 63 - 1), ('nan', {'a': float('nan')}), ('inf', {'a': float('inf')}), ('object', object()),
    ('too deep', nested(34)), ('deep dict', {'k': nested(40)}), ('NUL key', {'a\x00b': 1}),
]


@pytest.mark.parametrize('value', GOOD)
def test_json_shaped_values_are_storable(value):
    # then
    assert _storable(value) is True


@pytest.mark.parametrize('label,value', BAD)
def test_values_that_are_not_json_shaped_are_refused(label, value):
    # then
    assert _storable(value) is False, label


def test_a_cycle_is_refused_not_a_crash():
    # given
    loop: list = []
    loop.append(loop)

    # then
    assert _storable(loop) is False and _storable({'a': loop}) is False


def test_the_depth_bound_is_thirty_two_levels():
    # then
    assert _json(nested(32)) is True and _json(nested(33)) is False
    assert _storable({'k': nested(31)}) is True and _storable({'k': nested(32)}) is False


def test_a_pass_of_a_hyphenated_instance_name_survives_the_heal(store, tmp_path):
    # given
    one = Check('pulse', 'Pulse', 900, 0, ('demo',), lambda ctx: Sweep(NO_CARDS, {}), sections=('services',))
    other = Check('beat', 'Beat', 900, 0, ('demo-b',), lambda ctx: Sweep(PassResult('beat', True, 'ok', [], {}, ()), {}), sections=('services',))
    plug = dataclasses.replace(DEMO, checks=(one, other))
    key = master_key(store.db, settings())
    add_instance(store, plug, 'main', sealer_key=key, primary=True, url='https://demo.example.com')
    add_instance(store, plug, 'east-two', sealer_key=key, url='https://east.example.com')
    with TestClient(create_app(settings(), store, registry=Registry.of(plug, directory=HERE), kube=Kube(root=tmp_path))) as c:

        # when
        c.app.state.sweeper.run('demo', 'pulse', NOW)
        c.app.state.sweeper.run('demo', 'beat', NOW + 1)
        state = store.load()

    # then
    assert {'pulse', 'pulse-east-two', 'beat', 'beat-east-two'} <= set(state['sources'])


def test_the_shape_check_does_not_lean_on_the_encoder_for_ints_and_keys():
    # then
    assert _json(2 ** 63 - 1) is True and _json(2 ** 63) is False and _json(-2 ** 63) is True and _json(-2 ** 63 - 1) is False
    assert _json({'a': 1}) is True and _json({1: 'a'}) is False and _json(True) is True and _json(b'x') is False
