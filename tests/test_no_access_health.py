from skopax.messages import msg
from tests.conftest import AUTH
from tests.plugin_kit import add_instance
from tests.test_admin_plugins import DEMO, NOW, PAIR, listed, seed_passes, served
from skopax.engine import iso

REFUSED = 'ok \u00b7 no access to volumes, autoscalers'


def test_a_refused_instance_needs_attention_on_its_tile_and_the_board(store):
    # given
    seed_passes(store, {'pulse': {'at': iso(NOW), 'status': REFUSED}})
    add_instance(store, DEMO, 'main', primary=True, url='https://demo.example.com')

    # when
    with served(store, DEMO) as c:
        health = listed(c)['demo']['health']
        tile = next(p for p in c.get('/api/server', headers=AUTH).json()['plugins'] if p['id'] == 'demo')

    # then
    shown = msg('integrations.health.no_access')
    assert health == {'state': 'warn', 'text': shown, 'at': iso(NOW), 'instances': [{'name': 'main', 'state': 'warn', 'text': shown, 'at': iso(NOW)}]}
    assert shown == 'No access'
    assert tile['instances'][0]['status'] == REFUSED


def test_a_refusal_in_one_check_shows_beside_a_check_that_read(store):
    # given
    seed_passes(store, {'pulse': {'at': iso(NOW), 'status': 'ok'}, 'beat': {'at': iso(NOW + 5), 'status': 'ok \u00b7 no data for beats \u00b7 no access to beats'}})
    add_instance(store, PAIR, 'main', primary=True, url='https://demo.example.com')

    # when
    with served(store, PAIR) as c:
        health = listed(c)['demo']['health']
        tile = next(p for p in c.get('/api/server', headers=AUTH).json()['plugins'] if p['id'] == 'demo')

    # then
    assert (health['state'], health['instances'][0]['state']) == ('warn', 'warn')
    assert 'no access to' in tile['instances'][0]['status']


def test_quiet_read_and_cap_forms_stay_healthy_and_errors_stay_errors(store):
    # given
    forms = {'ok \u00b7 no data': 'ok', 'ok \u00b7 read 12 of 15 datasources': 'ok', 'ok \u00b7 read 1000 queries, more not read': 'ok',
             'error: metrics-1: HTTP 500 \u00b7 no access to logs': 'error'}

    # when
    seen = {}
    for status in forms:
        store.db['instances'].delete_many({})
        seed_passes(store, {'pulse': {'at': iso(NOW), 'status': status}})
        add_instance(store, DEMO, 'main', primary=True, url='https://demo.example.com')
        with served(store, DEMO) as c:
            seen[status] = listed(c)['demo']['health']['state']

    # then
    assert seen == forms


def test_a_down_browser_shows_its_part_as_a_warning_and_a_refusal_beside_it_says_no_access(store):
    # given
    forms = {'ok \u00b7 read 1 of 2 pages \u00b7 browser down': ('warn', 'browser down'),
             'ok \u00b7 browser down \u00b7 no access to browser': ('warn', msg('integrations.health.no_access')),
             'ok \u00b7 no data for browser downtime': ('ok', msg('integrations.health.ok'))}

    # when
    seen, tiles = {}, {}
    for status in forms:
        store.db['instances'].delete_many({})
        seed_passes(store, {'pulse': {'at': iso(NOW), 'status': 'ok'}, 'beat': {'at': iso(NOW + 5), 'status': status}})
        add_instance(store, PAIR, 'main', primary=True, url='https://demo.example.com')
        with served(store, PAIR) as c:
            health = listed(c)['demo']['health']
            seen[status] = (health['state'], health['text'])
            tiles[status] = next(p for p in c.get('/api/server', headers=AUTH).json()['plugins'] if p['id'] == 'demo')['instances'][0]['status']

    # then
    assert seen == forms
    assert tiles == {status: status if state == 'warn' else 'ok' for status, (state, _) in forms.items()}, 'the board tile gets the status that needs attention'
