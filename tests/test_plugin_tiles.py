import pytest
from fastapi.testclient import TestClient

from skopax.app import create_app
from skopax.contract import Check, Panel, PassResult, Sweep
from skopax.engine import iso
from skopax.plugins import prometheus
from skopax.registry import Registry
from tests.conftest import AUTH, settings
from tests.plugin_kit import add_instance, prom_runtime
from tests.plugins import two_checks_plugin
from tests.plugins.base import minimal
from tests.test_admin_plugins import HERE
from tests.test_engine import NOW


def quiet(ctx):
    return Sweep(PassResult('alpha', True, 'ok', [], {}, ('alpha',)), {})


PAIR = minimal('pair', 'Pair', checks=(Check('alpha', 'Alpha', 900, 0, ('alpha',), quiet), Check('beta', 'Beta', 900, 0, ('beta',), quiet)))


@pytest.mark.parametrize('alpha, beta, shown', [
    ('ok \u00b7 1 card dropped', 'error: timed out after 30 s', 'error: timed out after 30 s'),
    ('error: timed out after 30 s', 'ok \u00b7 1 card dropped', 'error: timed out after 30 s'),
    ('ok \u00b7 1 card dropped', 'ok', 'ok \u00b7 1 card dropped'),
    ('ok', 'okay then', 'okay then'),
], ids=['dropped-then-error', 'error-then-dropped', 'dropped-and-ok', 'not-quite-ok'])
def test_a_tile_instance_shows_its_first_failing_pass_and_a_pass_that_dropped_cards_is_ok(store, alpha, beta, shown):
    # given
    passes = {'alpha': {'at': iso(NOW), 'status': alpha}, 'beta': {'at': iso(NOW), 'status': beta}}
    store.save_state({'anomalies': [], 'initialized': True, 'sources': {}, 'serverPasses': passes}, None)

    # when
    with TestClient(create_app(settings(), store, registry=Registry.of(PAIR, directory=HERE))) as c:
        tile = {t['id']: t for t in c.get('/api/server', headers=AUTH).json()['plugins']}['pair']

    # then
    assert [(i['name'], i['status']) for i in tile['instances']] == [(None, shown)]


def test_a_panel_without_a_script_loads_no_script(store):
    # given
    runtime = prom_runtime(store, {'main': {'url': 'https://prom.example.com'}})

    # when
    scripts = runtime.page_scripts()

    # then
    assert Panel('svcs') == prometheus.PLUGIN.panels[0] and Panel('svcs').script is None
    assert not any(s.startswith('/plugins/prometheus/') for s in scripts)


def test_a_tile_instance_follows_its_first_watched_check(store):
    # given
    passes = {'pulse': {'at': iso(NOW), 'status': 'error: boom'}, 'beat': {'at': iso(NOW), 'status': 'ok'}, 'pulse-east': {'at': iso(NOW), 'status': 'ok'}}
    store.save_state({'anomalies': [], 'initialized': True, 'sources': {}, 'serverPasses': passes}, None)
    for i, name in enumerate(('main', 'east')):
        add_instance(store, two_checks_plugin.PLUGIN, name, primary=i == 0, url='https://demo.example.com')
    store.db['instances'].update_one({'_id': 'demo/main'}, {'$set': {'offChecks': ['pulse']}})
    store.db['instances'].update_one({'_id': 'demo/east'}, {'$set': {'offChecks': ['beat', 'pulse']}})

    # when
    with TestClient(create_app(settings(), store, registry=Registry.of(two_checks_plugin.PLUGIN, directory=HERE))) as c:
        tile = {t['id']: t for t in c.get('/api/server', headers=AUTH).json()['plugins']}['demo']

    # then
    assert [(i['name'], i['pass'], i['status']) for i in tile['instances']] == [('main', 'beat', 'ok'), ('east', None, None)]
    assert tile['passes'] == ['beat']
