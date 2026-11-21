import pytest

from tests.plugins.base import minimal
from tests.test_plugin_tools import SCHEMA, Rig
from skopax.contract import Capabilities, Field, ReadError, Tool
from skopax.investigator.tools import ERROR_LIMIT
from skopax.plugintools import ERROR_ROOM_MAX

LONG = 'the path is not allowed, try one of ' + ', '.join(f'/api/v1/items/{n}' for n in range(200))


def refusing(room):
    def run(ctx, args):
        raise ReadError(403, LONG, room) if room is not None else ReadError(403, LONG)
    return run


ROOMY = minimal('roomy', 'Roomy', capabilities=Capabilities(), fields=(Field('url', 'URL', 'url', required=True),),
                tools=(Tool('roomy_default', 'Refuses with the default room', SCHEMA, refusing(None), instance_input=None),
                       Tool('roomy_custom', 'Refuses with its own room', SCHEMA, refusing(600), instance_input=None),
                       Tool('roomy_huge', 'Asks for more room than the cap', SCHEMA, refusing(5000), instance_input=None),
                       Tool('roomy_bad', 'Asks for no room', SCHEMA, refusing(0), instance_input=None)))


@pytest.fixture
def rig(store):
    made = Rig(store, ROOMY)
    made.add(ROOMY, 'main', primary=True)
    return made


@pytest.mark.parametrize(('tool', 'room'), [('roomy_default', ERROR_LIMIT), ('roomy_custom', 600), ('roomy_huge', ERROR_ROOM_MAX), ('roomy_bad', ERROR_LIMIT)])
def test_a_plugin_read_error_keeps_its_own_room_up_to_the_cap(rig, tool, room):
    # given
    box = rig.box(run_id='r1')

    # when
    text, error = box.call(tool, {})

    # then
    assert error and LONG.startswith(text) and len(text) == room
    assert box.calls[-1]['error'] == text


def test_the_cap_is_800_and_the_default_300():
    # then
    assert (ERROR_ROOM_MAX, ERROR_LIMIT) == (800, 300)
