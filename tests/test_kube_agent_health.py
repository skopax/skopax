from contextlib import contextmanager

from fastapi.testclient import TestClient

from skopax.app import create_app
from skopax.messages import msg
from skopax.store import Store
from tests.conftest import AUTH, settings, state

AT = '2026-11-20T08:00:00Z'
SEEN = '2026-11-20T07:59:30Z'
LOCAL = {'mode': 'in-cluster'}
EAST = {'mode': 'agent', 'url': 'https://east.example.com', 'token': 'east-token-0002'}
WEST = {'mode': 'agent', 'url': 'https://west.example.com', 'token': 'west-token-0003'}
EXTRA = {'mode': 'agent', 'url': 'https://extra.example.com', 'token': 'extra-token-0004'}

ROWS = [
    {'name': 'prod', 'role': 'master', 'status': 'Ready', 'lastSeen': SEEN, 'plugin': 'kubernetes'},
    {'name': 'east', 'role': 'agent', 'status': 'Ready', 'lastSeen': SEEN, 'plugin': 'kubernetes'},
    {'name': 'west', 'role': 'agent', 'status': 'NotReady', 'error': 'agent unreachable (timed out)', 'lastSeen': None, 'plugin': 'kubernetes'},
]


def health(c: TestClient) -> dict:
    plugin = next(p for p in c.get('/api/admin/plugins', headers=AUTH).json()['plugins'] if p['id'] == 'kubernetes')
    return {i['name']: i for i in plugin['health']['instances']}


def seeded(store: Store, rows: list[dict], passes: dict | None = None):
    store.save_state({**state(), 'initialized': True, 'serverPasses': passes if passes is not None else {'nodes': {'at': AT, 'status': 'ok'}}, 'nodes': rows}, None)


@contextmanager
def booted(store: Store, rows: list[dict], names: dict, passes: dict | None = None, off: tuple = ()):
    with TestClient(create_app(settings(), store)) as c:
        for name, values in names.items():
            c.app.state.instances.create('kubernetes', name, values, True, 'alice@example.com', off_checks=off if name == 'prod' else ())
        seeded(store, rows, passes)
        yield c


def test_an_agent_instance_reads_its_health_from_the_primary_heartbeat_row(store: Store):
    # given
    with booted(store, ROWS, {'prod': LOCAL, 'east': EAST, 'west': WEST, 'extra': EXTRA}) as c:
        # when
        shown = health(c)

    # then
    assert (shown['east']['state'], shown['east']['text'], shown['east']['at']) == ('ok', msg('integrations.health.ok'), AT)
    assert (shown['west']['state'], shown['west']['text'], shown['west']['at']) == ('error', 'agent unreachable (timed out)', AT)
    assert (shown['extra']['state'], shown['extra']['text'], shown['extra']['at']) == ('pending', msg('integrations.health.pending'), None)


def test_a_ready_agent_with_a_cluster_error_warns_with_that_error(store: Store):
    # given
    rows = [ROWS[0], {**ROWS[1], 'clusterError': 'the agent has no cluster token'}]
    with booted(store, rows, {'prod': LOCAL, 'east': EAST}) as c:
        # when
        shown = health(c)

    # then
    assert (shown['east']['state'], shown['east']['text']) == ('warn', 'the agent has no cluster token')


def test_a_switched_off_primary_heartbeat_keeps_the_agents_as_they_were(store: Store):
    # given
    with booted(store, ROWS, {'prod': LOCAL, 'east': EAST}, off=('nodes',)) as c:
        # when
        shown = health(c)

    # then
    assert (shown['east']['state'], shown['east']['text']) == ('none', msg('integrations.health.no_checks'))


def test_the_primary_line_is_unchanged_and_the_plugin_top_turns_error_for_a_down_agent(store: Store):
    # given
    with booted(store, ROWS, {'prod': LOCAL, 'east': EAST, 'west': WEST}) as c:
        # when
        plugin = next(p for p in c.get('/api/admin/plugins', headers=AUTH).json()['plugins'] if p['id'] == 'kubernetes')

    # then
    by_name = {i['name']: i for i in plugin['health']['instances']}
    assert (by_name['prod']['state'], by_name['prod']['at']) == ('ok', AT)
    assert plugin['health']['state'] == 'error'


def test_an_old_row_does_not_read_as_fresh_when_the_last_pass_failed(store: Store):
    # given
    failed = {'nodes': {'at': AT, 'status': 'error: nodes of prod: HTTP 500'}}
    with booted(store, ROWS, {'prod': LOCAL, 'east': EAST, 'west': WEST}, failed) as c:
        # when
        shown = health(c)

    # then
    assert (shown['east']['state'], shown['east']['at']) == ('ok', SEEN)
    assert shown['west']['at'] is None


def test_a_non_primary_instance_in_this_cluster_mode_reads_its_master_row(store: Store):
    # given
    rows = [ROWS[0], {'name': 'second', 'role': 'master', 'status': 'Ready', 'lastSeen': SEEN, 'plugin': 'kubernetes'}]
    with booted(store, rows, {'prod': LOCAL, 'second': LOCAL}) as c:
        # when
        shown = health(c)

    # then
    assert (shown['second']['state'], shown['second']['text'], shown['second']['at']) == ('ok', msg('integrations.health.ok'), AT)
