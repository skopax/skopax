import dataclasses

import pytest

from skopax import engine
from skopax.contract import Check
from skopax.engine import anomaly, iso, merge
from skopax.registry import Registry
from tests.plugins import demo_plugin

NOW = 1_793_790_000.0
H = 3600
RUN = demo_plugin.PLUGIN.checks[0].run


def card(key: str, src: str, seen_h: float, **extra) -> dict:
    a = anomaly(src, key.split('|')[0], 'ns', key.split('|')[1], '', 'warn', 5, 1, 1, '', 'label', 'what')
    a.update(key=key, status='open', firstSeen=iso(NOW - seen_h * H), lastSeen=iso(NOW - seen_h * H), pushedAt=None, pushedSev=None, pushedPeak=None, note=None, peak=5, muted=None, **extra)
    return a


def daily_plugin(daily: bool):
    check = Check('weekly-scan', 'Weekly scan', 86400, 0, ('scan-pass',), RUN, daily=daily)
    return dataclasses.replace(demo_plugin.PLUGIN, checks=(check,))


@pytest.fixture(autouse=True)
def reset_daily():
    yield
    engine.set_daily(())


def closed_keys(state: dict) -> set[str]:
    return {a['key'] for a in state['anomalies'] if a['status'] == 'cleared'}


def test_a_daily_checks_card_stays_open_36_hours_without_a_pass_and_closes_after_3_days():
    # given
    found = Registry.of(daily_plugin(True)).get('demo')
    engine.set_daily(engine.daily_keys_of([found.plugin]))
    state = {'initialized': True, 'anomalies': [card('scan|a', 'weekly-scan', 36), card('scan|b', 'scan-pass', 36), card('scan|c', 'weekly-scan', 80), card('scan|d', 'other', 36)]}

    # when
    merge(state, [], {}, NOW)

    # then
    assert found.state == 'ok' and found.plugin.checks[0].daily is True
    assert closed_keys(state) == {'scan|c', 'scan|d'}, 'the check id and its ran keys get 3 days; any other source still gets 1 day'


def test_a_check_that_is_not_daily_keeps_the_one_day_limit_and_the_defaults_stay():
    # given
    engine.set_daily(engine.daily_keys_of([daily_plugin(False)]))
    state = {'initialized': True, 'anomalies': [card('scan|a', 'weekly-scan', 36), card('crux|x', 'crux', 50)]}

    # when
    merge(state, [], {}, NOW)

    # then
    assert closed_keys(state) == {'scan|a'}
    assert Check('a', 'A', 1, 0, (), RUN).daily is False


def test_a_runtime_refresh_publishes_the_daily_keys_of_the_loaded_plugins(store):
    # given
    from skopax.instances import Instances, PluginState
    from skopax.runtime import Runtime
    from skopax.sealer import Sealer
    registry = Registry.of(daily_plugin(True))
    runtime = Runtime(registry, PluginState(store.db), Instances(store.db, registry, lambda: Sealer('k')), None)

    # when
    runtime.refresh()

    # then
    assert engine.PLUGIN_DAILY == {'weekly-scan', 'scan-pass'}


def test_a_real_pass_keeps_sub_key_and_second_instance_cards_of_a_daily_check_open_and_carries_no_routes(store, tmp_path, monkeypatch):
    # given
    from fastapi.testclient import TestClient
    from skopax.app import create_app
    from skopax.contract import PassResult, Sweep
    from skopax.kube import Kube
    from skopax.runtime import Runtime
    from skopax.sealer import master_key
    from tests.conftest import settings
    from tests.plugin_kit import add_instance, no_loop
    from tests.test_admin_plugins import HERE
    plan = {'cards': True}

    def scan(ctx):
        card = anomaly('demo', 'scan-gap', 'shop', 'checkout-service', 'spring', 'warn', 5, None, None, '', 'Scan gap', 'no scan')
        card.update(ranKey='scan:shop', noRoutes=True)
        return Sweep(PassResult('weekly-scan', True, 'ok', [card] if plan['cards'] else [], {}, ()), {})

    plugin = dataclasses.replace(demo_plugin.PLUGIN, checks=(Check('weekly-scan', 'Weekly scan', 86400, 0, ('scan',), scan, daily=True),))
    key = master_key(store.db, settings())
    for i, name in enumerate(('main', 'east')):
        add_instance(store, plugin, name, sealer_key=key, primary=i == 0, url='https://demo.example.com')
    monkeypatch.setattr(Runtime, 'loop', no_loop)

    # when
    with TestClient(create_app(settings(), store, registry=Registry.of(plugin, directory=HERE), kube=Kube(root=tmp_path))) as c:
        c.app.state.sweeper.run('demo', 'weekly-scan', NOW)
        first = {a['key']: a for a in store.load()['anomalies']}
        plan['cards'] = False
        c.app.state.sweeper.run('demo', 'weekly-scan', NOW + 36 * H)
        later = {a['key']: a['status'] for a in store.load()['anomalies']}
        c.app.state.sweeper.run('demo', 'weekly-scan', NOW + 80 * H)
        last = {a['key']: a['status'] for a in store.load()['anomalies']}

    # then
    assert sorted(a['ranKey'] for a in first.values()) == ['scan:shop', 'scan:shop-east']
    assert all(a['noRoutes'] is True for a in first.values()), 'noRoutes survives the pass on both instances'
    assert set(later.values()) == {'open'}, 'a sub-key and a second instance of a daily check count as daily'
    assert set(last.values()) == {'cleared'}
