import random
import string
import threading
import time

import mongomock
import pytest

from skopax import instances as inst
from skopax.contract import Capabilities, Field
from skopax.instances import InvalidInstance
from skopax.messages import msg
from skopax.registry import Registry
from tests.conftest import AUTH
from tests.plugins.base import minimal
from tests.test_admin_plugins import HERE, served
from tests.test_instances import Box

ASKED: list[tuple[str, str]] = []
GATE = threading.Event()


def selector(value: str) -> str | None:
    ASKED.append(('selector', value))
    return None if value.startswith(('#', '.')) else f'Selector {value} must start with # or .'


def prefix(value: str) -> str | None:
    ASKED.append(('prefix', value))
    return None if value.endswith('/') else 'A prefix ends with /'


def crash(value: str) -> str | None:
    raise RuntimeError(f'parser blew up on {value} at /srv/secret/path')


def slow(value: str) -> str | None:
    GATE.wait(5)
    return None


def odd(value: str) -> object:
    return 42


def wordy(value: str) -> str:
    return 'line one\n\tline two ' + 'x' * 400


def token(value: str) -> str | None:
    ASKED.append(('token', value))
    return None if value.startswith('tok-') else 'A token starts with tok-'


CHECKED = minimal('checked', 'Checked', capabilities=Capabilities(secrets=('token',)), fields=(
    Field('selector', 'Selector', 'text', required=True, check=selector), Field('prefix', 'Prefix', 'text', check=prefix), Field('note', 'Note', 'text'),
    Field('token', 'Token', 'secret', check=token), Field('crash', 'Crash', 'text', check=crash), Field('slow', 'Slow', 'text', check=slow),
    Field('odd', 'Odd', 'text', check=odd), Field('wordy', 'Wordy', 'text', check=wordy)))


@pytest.fixture(autouse=True)
def reset():
    ASKED.clear()
    GATE.clear()
    yield
    GATE.set()


@pytest.fixture
def box():
    return Box(mongomock.MongoClient(tz_aware=True)['fc'], CHECKED)


def refused(call) -> InvalidInstance:
    with pytest.raises(InvalidInstance) as e:
        call()
    return e.value


def test_a_bad_value_is_refused_at_create_under_its_field_and_a_good_one_saves(box):
    # when
    bad = refused(lambda: box.instances.create('checked', 'main', {'selector': 'div', 'prefix': 'logs/'}, True, 'alice'))
    made = box.instances.create('checked', 'main', {'selector': '#main', 'prefix': 'logs/', 'token': 'tok-1'}, True, 'alice')

    # then
    assert (bad.field, bad.args[0]) == ('selector', 'Selector div must start with # or .')
    assert box.instances.get('checked', 'main') is made and made.values['selector'] == '#main'
    assert ('token', 'tok-1') in ASKED, 'a secret field is checked on its typed value'


def test_an_edit_checks_only_the_values_it_sends_and_a_refused_edit_changes_nothing(box):
    # given
    box.instances.create('checked', 'main', {'selector': '#main', 'prefix': 'logs/'}, True, 'alice')
    ASKED.clear()

    # when
    bad = refused(lambda: box.instances.update('checked', 'main', {'prefix': 'logs', 'note': 'n'}, 'alice'))
    box.instances.update('checked', 'main', {'note': 'kept'}, 'alice')
    cleared = box.instances.update('checked', 'main', {'prefix': ''}, 'alice')

    # then
    assert (bad.field, bad.args[0]) == ('prefix', 'A prefix ends with /')
    assert ASKED == [('prefix', 'logs')], 'unchanged and cleared values are not asked again'
    assert cleared.values.get('note') == 'kept' and 'prefix' not in cleared.values and cleared.values['selector'] == '#main'


def test_a_check_that_crashes_answers_oddly_or_rambles_refuses_with_core_text_only(box):
    # given
    base = {'selector': '#main'}

    # when
    crashed = refused(lambda: box.instances.create('checked', 'a', base | {'crash': 'v'}, True, 'alice'))
    odd_one = refused(lambda: box.instances.create('checked', 'b', base | {'odd': 'v'}, True, 'alice'))
    long_one = refused(lambda: box.instances.create('checked', 'c', base | {'wordy': 'v'}, True, 'alice'))

    # then
    assert (crashed.field, crashed.args[0]) == ('crash', msg('instances.invalid.check_failed', field='Crash'))
    assert (odd_one.field, odd_one.args[0]) == ('odd', msg('instances.invalid.check_failed', field='Odd'))
    assert long_one.field == 'wordy' and long_one.args[0].startswith('line one line two x') and len(long_one.args[0]) == inst.FIELD_CHECK_TEXT_MAX
    assert box.instances.all('checked') == []


def test_a_slow_check_refuses_in_bounded_time_and_a_save_while_it_still_runs_refuses_at_once(box, monkeypatch):
    # given
    monkeypatch.setattr(inst, 'FIELD_CHECK_S', 0.2)
    base = {'selector': '#main', 'slow': 'v'}

    # when
    late = refused(lambda: box.instances.create('checked', 'a', base, True, 'alice'))
    again = refused(lambda: box.instances.create('checked', 'b', base, True, 'alice'))
    other = box.instances.create('checked', 'c', {'selector': '#ok'}, True, 'alice')
    GATE.set()

    # then
    assert (late.field, late.args[0]) == ('slow', msg('instances.invalid.check_slow', field='Slow'))
    assert (again.field, again.args[0]) == ('slow', msg('instances.invalid.check_slow', field='Slow'))
    assert other.name == 'c', 'a slow check holds no other save'


def test_a_check_that_is_not_callable_breaks_the_plugin():
    # when
    found = Registry.of(minimal('bad', 'Bad', fields=(Field('x', 'X', 'text', check='starts with #'),)), directory=HERE).get('bad')

    # then
    assert found.state == 'broken' and msg('plugins.why.field_check', key='x') in found.reason


def test_admin_shows_the_refusal_under_the_field(store):
    # given
    with served(store, CHECKED) as c:
        # when
        answer = c.post('/api/admin/plugins/checked/instances', json={'name': 'main', 'values': {'selector': 'div'}, 'enabled': True}, headers=AUTH)
        listed = c.get('/api/admin/plugins', headers=AUTH).json()['plugins']

    # then
    assert answer.status_code == 422 and answer.json() == {'detail': 'Selector div must start with # or .', 'field': 'selector'}
    fields = next(p for p in listed if p['id'] == 'checked')['fields']
    assert all('check' not in f for f in fields)


def echo(value: str) -> str:
    return f'{value} is wrong'


def half(value: str) -> str:
    return f'the part {value[2:12]} is wrong'


ECHOED = minimal('echoed', 'Echoed', capabilities=Capabilities(secrets=('api-key', 'part')), fields=(
    Field('api-key', 'Key', 'secret', check=echo), Field('part', 'Part', 'secret', check=half), Field('site', 'Site', 'text', check=echo)))


@pytest.fixture
def echoed():
    return Box(mongomock.MongoClient(tz_aware=True)['fe'], ECHOED)


def test_a_secret_echoed_by_its_check_shows_as_stars(echoed):
    # when
    said = refused(lambda: echoed.instances.create('echoed', 'main', {'api-key': 'tok-abc123'}, True, 'alice'))
    plain = refused(lambda: echoed.instances.create('echoed', 'main', {'site': 'tok-abc123'}, True, 'alice'))

    # then
    assert (said.field, said.args[0]) == ('api-key', '*** is wrong')
    assert plain.args[0] == 'tok-abc123 is wrong', 'a field that is not secret is shown as typed'


def test_a_multi_line_secret_echoed_by_its_check_never_shows(echoed):
    # given
    value = '{\n  "k": "abc123xyz"\n}'

    # when
    whole = refused(lambda: echoed.instances.create('echoed', 'main', {'api-key': value}, True, 'alice'))
    part = refused(lambda: echoed.instances.create('echoed', 'main', {'part': 'sk-live-0123456789abcdef'}, True, 'alice'))

    # then
    assert 'abc123xyz' not in whole.args[0] and whole.args[0] == '*** is wrong'
    assert (part.field, part.args[0]) == ('part', msg('instances.invalid.check_failed', field='Part')), 'a run of 8 or more characters of the secret falls back to core text'


SECRET = ''.join(random.Random(7).choices(string.ascii_letters + string.digits, k=4000))
FILLER = ''.join(random.Random(9).choices(string.ascii_letters + string.digits, k=999991))


def flood(value: str) -> str:
    return value[-8:] + ' ' + FILLER


FLOODED = minimal('flooded', 'Flooded', capabilities=Capabilities(secrets=('api-key',)), fields=(Field('api-key', 'Key', 'secret', check=flood),))


def test_a_huge_echo_of_a_long_secret_is_refused_with_core_text_in_bounded_time():
    # given
    box = Box(mongomock.MongoClient(tz_aware=True)['ff'], FLOODED)

    # when
    began = time.monotonic()
    said = refused(lambda: box.instances.create('flooded', 'main', {'api-key': SECRET}, True, 'alice'))
    took = time.monotonic() - began

    # then
    assert (said.field, said.args[0]) == ('api-key', msg('instances.invalid.check_failed', field='Key'))
    assert took < 0.5, f'took {took:.2f} s'
