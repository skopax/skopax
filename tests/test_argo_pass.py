import json
from types import SimpleNamespace

import mongomock
from fastapi.testclient import TestClient

from skopax import clock
from skopax.app import create_app
from skopax.contract import iso
from skopax.instances import Instances, PluginState
from skopax.kube import HttpFailed, Kube
from skopax.plugins import argocd
from skopax.plugins.argocd import argo as argo_module
from skopax.plugins.argocd.argo import PERSIST_S, evaluate, links_for, service_of
from skopax.registry import Registry
from skopax.runtime import Runtime
from skopax.sealer import Sealer, master_key
from skopax.store import Store
from tests.conftest import settings
from tests.plugin_kit import KIT_KEY, add_instance
from tests.test_argo import APPS, application, resource
from tests.test_kube import BASE, TOKEN, FakeApi

NOW = 1_792_931_000.0
CHECKOUT = 'shop-checkout-service'
URL = 'https://argocd.example.com'
NS = 'argocd'


def app_in(name: str, ns: str, sync: str = 'Synced', health: str = 'Healthy', phase: str = 'Succeeded', automated: bool = True, project: str = 'default') -> dict:
    a = application(name, sync, health, phase)
    a['spec']['destination']['namespace'] = ns
    a['spec']['project'] = project
    if not automated:
        a['spec']['syncPolicy'] = {}
    return a


def every_app() -> list[dict]:
    return [app_in('web-storefront', 'web'), app_in(CHECKOUT, 'shop', 'OutOfSync', 'Degraded', 'Succeeded'),
            app_in('payments-ledger-service', 'payments', 'Synced', 'Progressing'),
            app_in('accounts-profile-service', 'accounts', 'OutOfSync', 'Healthy', automated=False),
            app_in('ops-batch-jobs', 'ops', 'Synced', 'Suspended')]


def judge(items: list[dict], prev: list[dict], now: float, suffix: str = '') -> tuple[list[dict], list[dict]]:
    return evaluate(items, prev, now, URL, NS, suffix)


def in_cluster(monkeypatch, tmp_path, routes: dict, token: bool = True) -> FakeApi:
    if token:
        (tmp_path / 'token').write_text(TOKEN)
    api = FakeApi(routes)
    monkeypatch.setattr(argo_module, 'local_kube', lambda: Kube(root=tmp_path, base=BASE, transport=api))
    return api


def argo_runtime(transport=None, **values) -> Runtime:
    db = mongomock.MongoClient(tz_aware=True)['argo']
    registry = Registry.of(argocd.PLUGIN)
    runtime = Runtime(registry, PluginState(db), Instances(db, registry, lambda: Sealer(KIT_KEY)), transport)
    add_instance(SimpleNamespace(db=db), argocd.PLUGIN, 'main', sealer_key=KIT_KEY, primary=True, **({'url': URL} | values))
    runtime.refresh()
    return runtime


def run_check(runtime: Runtime, now: float = NOW):
    plugin = argocd.PLUGIN
    return plugin.checks[0].run(runtime.context(plugin, runtime.instances.get(plugin.id, 'main'), now))


def with_instance(store: Store, **values) -> None:
    add_instance(store, argocd.PLUGIN, 'main', sealer_key=master_key(store.db, settings()), primary=True, **({'url': URL} | values))


def test_an_app_name_maps_to_its_service_and_falls_back_to_the_whole_name():
    # then
    assert service_of(CHECKOUT, 'shop') == 'checkout-service'
    assert service_of('shop-checkout-service-live', 'shop', '-live') == 'checkout-service'
    assert service_of('ops-skopax-live', 'ops', '-live') == 'skopax'
    assert service_of('shop-checkout-service-live', 'shop') == 'checkout-service-live'
    assert service_of('varnish-web', 'web') == 'varnish-web'
    assert service_of('argocd-apps', None) == 'argocd-apps'
    assert service_of('-live', None, '-live') == '-live'


def test_a_problem_app_gets_a_card_only_after_15_minutes_and_keeps_its_since():
    # given
    apps = every_app()

    # when
    found1, rows1 = judge(apps, [], NOW)
    found2, rows2 = judge(apps, rows1, NOW + 600)
    found3, rows3 = judge(apps, rows2, NOW + PERSIST_S)

    # then
    assert PERSIST_S == 900
    assert found1 == [] and found2 == []
    assert {r['app'] for r in rows1} == {CHECKOUT, 'payments-ledger-service', 'accounts-profile-service'}
    assert all(r['since'] == iso(NOW) for r in rows3)
    assert {x['key']: x['sev'] for x in found3} == {
        'argo|shop/checkout-service': 'warn',
        'argo|payments/ledger-service': 'warn',
        'argo|accounts/profile-service': 'info',
    }


def test_an_app_that_recovers_starts_its_clock_again():
    # given
    bad, good = [app_in(CHECKOUT, 'shop', 'Synced', 'Degraded')], [app_in(CHECKOUT, 'shop')]

    # when
    _, rows1 = judge(bad, [], NOW)
    found2, rows2 = judge(good, rows1, NOW + 300)
    found3, rows3 = judge(bad, rows2, NOW + 600)
    found4, _ = judge(bad, rows3, NOW + 600 + PERSIST_S - 1)

    # then
    assert (found2, rows2) == ([], [])
    assert rows3[0]['since'] == iso(NOW + 600)
    assert found3 == [] and found4 == []


def test_a_failed_sync_that_left_the_app_behind_is_a_card_at_once():
    # given
    failed = app_in(CHECKOUT, 'shop', 'OutOfSync', 'Healthy', 'Failed')
    errored = app_in('web-storefront', 'web', 'OutOfSync', 'Healthy', 'Error')
    recovered = app_in('payments-ledger-service', 'payments', 'Synced', 'Healthy', 'Failed')

    # when
    found, rows = judge([failed, errored, recovered], [], NOW)

    # then
    assert {x['key']: (x['sev'], x['label']) for x in found} == {
        'argo|shop/checkout-service': ('warn', 'Argo CD sync failed'),
        'argo|web/storefront': ('warn', 'Argo CD sync failed'),
    }
    assert {r['app'] for r in rows} == {CHECKOUT, 'web-storefront'}


def test_the_card_says_what_argo_sees_in_allowlisted_cut_text():
    # given
    app = app_in(CHECKOUT, 'shop', 'OutOfSync', 'Degraded', 'Failed')
    app['status']['resources'] += [resource('Deployment', f'extra-{i}', 'OutOfSync', 'Missing') for i in range(4)]
    manual = app_in('accounts-profile-service', 'accounts', 'OutOfSync', 'Healthy', automated=False)

    # when
    _, rows = judge([app, manual], [], NOW - PERSIST_S)
    found, _ = judge([app, manual], rows, NOW)
    cs, gw = sorted(found, key=lambda x: x['key'])

    # then
    assert (gw['src'], gw['sig'], gw['ns'], gw['svc'], gw['app'], gw['unit'], gw['slack']) == ('argo', 'argo', 'shop', 'checkout-service', CHECKOUT, 'dur', 86400)
    assert (gw['ranKey'], cs['ranKey']) == (f'argo:{CHECKOUT}', 'argo:accounts-profile-service')
    assert gw['now'] == PERSIST_S and gw['d1'] is None
    assert gw['label'] == 'Argo CD sync failed \u00b7 Degraded'
    assert gw['short'] == 'WARN checkout-service Argo CD sync failed \u00b7 Degraded for 15 min'
    assert CHECKOUT in gw['what'] and 'sync OutOfSync' in gw['what'] and 'health Degraded' in gw['what']
    assert 'exceeded its progress deadline' in gw['what'] and 'one or more objects failed to apply' in gw['what']
    assert 'Deployment/checkout-service' in gw['what'] and '+3 more' in gw['what']
    assert f'{URL}/applications/{NS}/{CHECKOUT}' in gw['what']
    since = NOW - PERSIST_S
    assert f'not Synced/Healthy since {clock.hm(since)} {clock.label(since)} (15 min)' in gw['what']
    assert 'last sync Failed at 07:46 UTC: one or more objects failed to apply' in gw['what']
    assert len(gw['what']) < 1500 and 'x' * 301 not in gw['what']
    assert 'SECRET' not in json.dumps(found) and 'cluster' not in gw
    assert cs['label'] == 'Argo CD OutOfSync' and cs['sev'] == 'info'
    assert 'auto-sync off' in cs['what']
    assert 'auto-sync on' in gw['what']


def test_links_cover_argo_cards_only():
    # given
    found, _ = judge([app_in(CHECKOUT, 'shop', 'OutOfSync', 'Healthy', 'Failed')], [], NOW)

    # then
    assert links_for(found[0], URL, NS) == [{'label': 'Argo CD app', 'url': f'{URL}/applications/{NS}/{CHECKOUT}'}]
    assert links_for({'src': 'prom', 'sig': 'err'}, URL, NS) is None


def test_the_pass_reads_every_app_once_and_saves_the_problem_rows(monkeypatch, tmp_path):
    # given
    api = in_cluster(monkeypatch, tmp_path, {APPS: {'items': every_app()}})

    # when
    sweep = run_check(argo_runtime(mode='in-cluster'))

    # then
    assert len(api.calls) == 1
    r = sweep.result
    assert (r.name, r.ok, r.status, r.found, r.sections) == ('argo', True, 'ok', [], {})
    assert r.ran == ('argo', 'argo:accounts-profile-service', 'argo:ops-batch-jobs', 'argo:payments-ledger-service', f'argo:{CHECKOUT}', 'argo:web-storefront', 'argo:access:applications')
    assert [x['app'] for x in sweep.sections['argo']] == ['accounts-profile-service', 'payments-ledger-service', CHECKOUT]
    assert set(sweep.sections['argo'][0]) == {'app', 'ns', 'svc', 'sync', 'health', 'phase', 'since'}


def test_a_failed_pass_reports_a_short_reason_and_leaves_the_rows_alone(monkeypatch, tmp_path):
    # given
    in_cluster(monkeypatch, tmp_path, {APPS: HttpFailed(502, 'applications.argoproj.io answered badly')})

    # when
    sweep = run_check(argo_runtime(mode='in-cluster'))

    # then
    r = sweep.result
    assert (r.name, r.ok, r.sections, sweep.sections) == ('argo', False, {}, {})
    assert r.status.startswith('error: applications: HTTP 502') and TOKEN not in r.status
    assert r.status == 'error: applications: HTTP 502 on Argo CD applications'


def test_the_app_runs_the_argo_pass_every_5_minutes_and_opens_a_card_after_15(store: Store, monkeypatch, tmp_path):
    # given
    store.save_state({'anomalies': [], 'initialized': True, 'sources': {'prom': 'ok'}}, None)
    in_cluster(monkeypatch, tmp_path, {APPS: {'items': [app_in(CHECKOUT, 'shop', 'Synced', 'Degraded')]}})
    with_instance(store, mode='in-cluster')

    # when
    with TestClient(create_app(settings(), store)) as c:
        jobs = {j.name: j for j in c.app.state.scheduler.jobs}
        c.app.state.sweeper.run('argocd', 'argo', NOW)
        first = store.load()
        c.app.state.sweeper.run('argocd', 'argo', NOW + PERSIST_S)
    state = store.load()
    by = {a['key']: a for a in state['anomalies']}

    # then
    assert (jobs['argocd.argo'].period, jobs['argocd.argo'].offset) == (300, 105)
    assert first['anomalies'] == [] and first['argo'][0]['since'] == iso(NOW)
    assert state['sources'] == {'prom': 'ok', 'argo': 'ok'}
    assert state['serverPasses']['argo']['status'] == 'ok'
    card = by['argo|shop/checkout-service']
    assert (card['status'], card['sev'], card['src']) == ('open', 'warn', 'argo')
    assert card['links'][0]['url'] == f'{URL}/applications/{NS}/{CHECKOUT}'


def test_a_healthy_pass_clears_the_open_argo_card(store: Store, monkeypatch, tmp_path):
    # given
    store.save_state({'anomalies': [], 'initialized': True, 'sources': {}}, None)
    answers = {'items': [app_in(CHECKOUT, 'shop', 'OutOfSync', 'Healthy', 'Failed')]}
    in_cluster(monkeypatch, tmp_path, {APPS: lambda q: answers})
    with_instance(store, mode='in-cluster')

    # when
    with TestClient(create_app(settings(), store)) as c:
        c.app.state.sweeper.run('argocd', 'argo', NOW)
        opened = {a['key']: a['status'] for a in store.load()['anomalies']}
        answers['items'] = [app_in(CHECKOUT, 'shop')]
        c.app.state.sweeper.run('argocd', 'argo', NOW + 300)
    state = store.load()

    # then
    assert opened == {'argo|shop/checkout-service': 'open'}
    assert [(a['key'], a['status']) for a in state['anomalies']] == [('argo|shop/checkout-service', 'cleared')]
    assert state['argo'] == []


def test_without_an_argocd_instance_no_argo_job_is_registered(store: Store):
    # when
    with TestClient(create_app(settings(), store)) as c:
        names = [j.name for j in c.app.state.scheduler.jobs]

    # then
    assert 'argocd.argo' not in names and 'argo' not in names


def test_an_empty_automated_block_means_auto_sync_is_on_and_enabled_false_turns_it_off():
    # given
    apps = [app_in(f'web-{name}', 'web', 'OutOfSync', 'Healthy') for name in ('varnish', 'storefront', 'blog')]
    apps[0]['spec']['syncPolicy'] = {'automated': {}}
    apps[1]['spec']['syncPolicy'] = {'automated': {'enabled': False, 'prune': True}}
    apps[2]['spec']['syncPolicy'] = {'automated': None}

    # when
    _, rows = judge(apps, [], NOW - PERSIST_S)
    found, _ = judge(apps, rows, NOW)
    by = {x['app']: x['what'] for x in found}

    # then
    assert 'auto-sync on' in by['web-varnish']
    assert 'auto-sync off' in by['web-storefront'] and 'auto-sync off' in by['web-blog']
