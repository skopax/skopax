import json
import urllib.error

import pytest
from fastapi.testclient import TestClient

from skopax.app import TEST_TIMEOUT_S as CORE_TEST_TIMEOUT_S, create_app
from skopax.contract import InstanceInfo, iso
from skopax.investigator.tools import ToolError
from skopax.kube import HttpFailed
from skopax.messages import msg
from skopax.plugins import argocd
from skopax.plugins.argocd import argo as argo_module
from skopax.plugins.argocd.argo import OFF, PAGE, PERSIST_S, ApiSource, Argo, ArgoError, evaluate
from skopax.registry import BUILTIN, Registry, describe
from skopax.remote import HttpError, Reply, Unreachable
from skopax.sweeps import CHECK_TIMEOUT_S
from skopax.sealer import master_key
from skopax.store import Store
from tests.conftest import settings, state
from tests.fake_http import FakeHttp
from tests.plugin_kit import add_instance
from tests.test_argo import APPS, application
from tests.test_argo_pass import CHECKOUT, NOW, URL, app_in, argo_runtime, in_cluster, run_check, with_instance
from tests.test_argo_tool import argo_tools
from tests.test_investigator_tools import box

EAST = 'https://argocd-east.example.com'
API_TOKEN = 'argocd-api-token-7c41'
LIST = f'{URL}/api/v1/applications'
VERSION = f'{URL}/api/version'
DOT = '\u00b7'


def info(**config) -> InstanceInfo:
    return InstanceInfo('main', True, None, {'url': URL, 'namespace': 'argocd'} | config)


def connection_of(transport=None, **values):
    runtime = argo_runtime(transport, **values)
    plugin = argocd.PLUGIN
    return plugin.test(runtime.context(plugin, runtime.instances.get(plugin.id, 'main'), NOW))


def timed(kube, seen: list):
    inner = kube.transport

    def send(url, headers, timeout):
        seen.append(timeout)
        return inner(url, headers, timeout)

    kube.transport = send
    return kube


def cards(store: Store) -> dict[str, dict]:
    return {a['key']: a for a in store.load()['anomalies']}


def serving(http: FakeHttp, store: Store, **instances: dict) -> TestClient:
    key = master_key(store.db, settings())
    for index, (name, values) in enumerate(instances.items()):
        add_instance(store, argocd.PLUGIN, name, sealer_key=key, primary=index == 0, **({'url': URL} | values))
    return TestClient(create_app(settings(), store, transport=http))


def test_argocd_identity_and_manifest():
    # given
    plugin = argocd.PLUGIN

    # when
    manifest = describe(plugin, icon=True)
    loaded = Registry.load({}, entry_points=False).get('argocd')

    # then
    assert json.loads(json.dumps(manifest, allow_nan=False)) == manifest
    assert 'skopax.plugins.argocd' in BUILTIN and loaded.state == 'ok'
    assert (manifest['id'], manifest['name'], manifest['version'], manifest['category'], manifest['author'], manifest['licence'], manifest['core'], manifest['edition']) == (
        'argocd', 'Argo CD', '0.1.0', 'infrastructure', 'Skopax', 'Apache-2.0', '>=0.1,<0.4', 'community')
    assert manifest['capabilities'] == {'hosts': ['@url', 'kubernetes.default.svc'], 'secrets': ['token'], 'agentReads': False}
    assert manifest['uses'] == [] and manifest['maxInstances'] is None and manifest['rules'] == [] and manifest['settings'] == [] and manifest['panels'] == []
    assert manifest['checks'] == [{'id': 'argo', 'label': 'Argo CD applications', 'period': 300, 'offset': 105, 'ran': ['argo']}]
    check = plugin.checks[0]
    assert (check.sections, check.tool, check.once) == (('argo',), 'argo', False) and set(check.section_help) == {'argo'}
    assert [(f['key'], f['kind'], f['required'], f['ui'], f['showIf']) for f in manifest['fields']] == [
        ('mode', 'choice', True, False, None), ('url', 'url', True, True, None), ('token', 'secret', False, False, ['mode', 'api']), ('ca', 'pem', False, False, None),
        ('ca-pinned', 'bool', False, False, ['ca', '*']), ('namespace', 'text', False, False, None), ('projects', 'text', False, False, None), ('app-suffix', 'text', False, False, None)]
    assert manifest['fields'][0]['choices'] == ['api', 'in-cluster']
    assert [(t['name'], t['kind']) for t in manifest['tools']] == [('argo', 'read')] and manifest['knowledge'] == ['argocd']
    assert plugin.description.endswith('.') and plugin.description.count('.') == 1
    assert Registry.of(plugin).icon('argocd').startswith('<svg') and len(plugin.knowledge[0].text.splitlines()) <= 40 and plugin.knowledge[0].slack


def test_argocd_connection_test_reports_ok_and_failures(monkeypatch, tmp_path):
    # given
    good = FakeHttp({('GET', VERSION): {'Version': 'v2.13.1+af54ef8'}, ('GET', LIST): {'items': [application(), application('web-storefront')]}})
    bare = FakeHttp({('GET', VERSION): {'BuildDate': '2026-01-01'}, ('GET', LIST): {'items': None}})
    refused = FakeHttp({('GET', VERSION): HttpError(401, VERSION)})
    down = FakeHttp({('GET', VERSION): Unreachable('x')})
    blind = FakeHttp({('GET', VERSION): {'Version': 'v2.13.1+af54ef8'}, ('GET', LIST): HttpError(403, LIST, '{"message": "permission denied"}')})

    # when
    results = [connection_of(good, token=API_TOKEN), connection_of(bare), connection_of(refused, token=API_TOKEN), connection_of(down, token=API_TOKEN),
               connection_of(blind, token=API_TOKEN)]
    api = in_cluster(monkeypatch, tmp_path, {APPS: {'metadata': {'continue': 'next'}, 'items': [application(), application('web-storefront')]}})
    inside = connection_of(mode='in-cluster')
    in_cluster(monkeypatch, tmp_path, {APPS: HttpFailed(403, 'forbidden')})
    forbidden = connection_of(mode='in-cluster')
    in_cluster(monkeypatch, tmp_path / 'none', {}, token=False)
    off = connection_of(mode='in-cluster')

    # then
    assert [(r.ok, r.summary) for r in results] == [(True, 'Argo CD v2.13.1+af54ef8: 2 applications'),
                                                   (True, 'Argo CD unknown version: 0 applications; a token without the RBAC permission applications, get sees none either'),
                                                   (False, msg('connection.rejected', status=401)), (False, msg('connection.unreachable')),
                                                   (False, 'Argo CD refused to read applications (HTTP 403): the token lacks the RBAC permission applications, get')]
    assert [c[1] for c in good.calls] == [VERSION, LIST + argo_module.NAMES] and all(c[2]['Authorization'] == f'Bearer {API_TOKEN}' for c in good.calls)
    assert all('Authorization' not in c[2] for c in bare.calls) and [c[1] for c in blind.calls] == [VERSION, LIST + argo_module.NAMES]
    assert (inside.ok, inside.summary) == (True, 'Argo CD in this cluster: 2+ applications in namespace argocd')
    assert api.calls[0][1] == {'limit': [str(PAGE)]} and len(api.calls) == 1
    assert (forbidden.ok, forbidden.summary) == (False, 'Argo CD refused to read applications (HTTP 403): the service account lacks get and list on applications.argoproj.io in the namespace argocd')
    assert (off.ok, off.summary) == (False, OFF)
    assert all(isinstance(r.ms, int) and r.ms >= 0 for r in results + [inside, forbidden, off])


def test_the_connection_test_shares_one_budget_under_cores_limit(monkeypatch, tmp_path):
    # given
    now = [100.0]
    monkeypatch.setattr(argocd, 'monotonic', lambda: now[0])

    def slow(seconds: float):
        seen: list = []

        def send(method, url, headers, data, timeout):
            seen.append((url, timeout))
            if url == VERSION:
                now[0] += seconds
                return Reply({'Version': 'v2.13.1'}, {})
            return Reply({'items': []}, {})

        return send, seen

    quick, quick_seen = slow(5)
    late, late_seen = slow(argocd.TEST_BUDGET_S + 1)
    kube_seen: list = []
    api = in_cluster(monkeypatch, tmp_path, {APPS: {'items': []}})
    inner = argo_module.local_kube
    monkeypatch.setattr(argo_module, 'local_kube', lambda: timed(inner(), kube_seen))

    # when
    ok = connection_of(quick, token=API_TOKEN)
    over = connection_of(late, token=API_TOKEN)
    inside = connection_of(mode='in-cluster')

    # then
    assert argocd.TEST_BUDGET_S < CORE_TEST_TIMEOUT_S
    can_i = f'{URL}/api/v1/account/can-i/applications/get/*/*'
    assert ok.ok and quick_seen == [(VERSION, argocd.TEST_BUDGET_S), (LIST + argo_module.NAMES, argocd.TEST_BUDGET_S - 5), (can_i, argocd.TEST_BUDGET_S - 5)], 'an empty list asks can-i inside the same budget'
    assert (over.ok, over.summary) == (False, msg('connection.timeout')) and late_seen == [(VERSION, argocd.TEST_BUDGET_S)]
    assert inside.ok and kube_seen == [argocd.TEST_BUDGET_S] and len(api.calls) == 1


def test_argocd_defaults_are_neutral():
    # when
    defaults = {f.key: f.default for f in argocd.PLUGIN.fields}

    # then
    assert defaults == {'mode': 'api', 'url': '', 'token': '', 'ca': '', 'ca-pinned': 'false', 'namespace': 'argocd', 'projects': '', 'app-suffix': ''}
    assert {s.key: s.default for s in argocd.PLUGIN.settings} == {}


def test_argocd_second_instance_is_scoped(store: Store):
    # given
    store.save_state({'anomalies': [], 'initialized': True, 'sources': {}}, None)
    http = FakeHttp({('GET', LIST): {'items': [app_in(CHECKOUT, 'shop', 'OutOfSync', 'Healthy', 'Failed')]},
                     ('GET', f'{EAST}/api/v1/applications'): {'items': [app_in('web-storefront', 'web', 'OutOfSync', 'Healthy', 'Failed'),
                                                                        app_in('payments-ledger-service', 'payments', 'Synced', 'Degraded')]}})

    # when
    with serving(http, store, main={'token': API_TOKEN}, east={'url': EAST, 'token': API_TOKEN}) as c:
        c.app.state.sweeper.run('argocd', 'argo', NOW)
        first = cards(store)
        c.app.state.sweeper.run('argocd', 'argo', NOW + PERSIST_S)
        state = store.load()
    found = cards(store)

    # then
    main, east = first['argo|shop/checkout-service'], first['argo|web/storefront@east']
    assert 'cluster' not in main and main['ranKey'] == f'argo:{CHECKOUT}' and main['links'][0]['url'] == f'{URL}/applications/argocd/{CHECKOUT}'
    assert (east['cluster'], east['ranKey'], east['short']) == ('east', 'argo:web-storefront-east', 'WARN EAST storefront Argo CD sync failed for 0 s')
    assert east['links'][0]['url'] == f'{EAST}/applications/argocd/web-storefront'
    assert 'argo|payments/ledger-service@east' not in first
    assert found['argo|payments/ledger-service@east']['cluster'] == 'east' and found['argo|payments/ledger-service@east']['status'] == 'open'
    assert set(state['serverPasses']) >= {'argo', 'argo-east'} and state['serverPasses']['argo-east']['status'] == 'ok'
    assert {(r['app'], r.get('cluster')) for r in state['argo']} == {(CHECKOUT, None), ('web-storefront', 'east'), ('payments-ledger-service', 'east')}


def test_both_modes_feed_the_same_evaluation(monkeypatch, tmp_path):
    # given
    fixture = [app_in(CHECKOUT, 'shop', 'OutOfSync', 'Degraded', 'Failed'), app_in('web-storefront', 'web'), app_in('payments-ledger-service', 'payments', 'Synced', 'Progressing')]
    http = FakeHttp({('GET', LIST): {'items': fixture}})
    api = in_cluster(monkeypatch, tmp_path, {APPS: {'items': fixture}})

    # when
    by_api = run_check(argo_runtime(http, token=API_TOKEN))
    by_kube = run_check(argo_runtime(FakeHttp(), mode='in-cluster'))

    # then
    assert by_api.result.found and by_api.sections['argo']
    assert (by_api.result, by_api.sections) == (by_kube.result, by_kube.sections)
    assert [c[1].split('?')[0] for c in http.calls] == [LIST] and len(api.calls) == 1


def test_links_use_the_instance_url_and_namespace():
    # given
    where = info(url='https://argocd.example.com/', namespace='team-apps')
    found, _ = evaluate([app_in(CHECKOUT, 'shop', 'OutOfSync', 'Healthy', 'Failed')], [], NOW, 'https://argocd.example.com/', 'team-apps')

    # when
    links = argocd.PLUGIN.links(found[0], where)
    other = argocd.PLUGIN.links({'src': 'prom', 'sig': 'err'}, where)
    bare = argocd.PLUGIN.links({'src': 'argo', 'sig': 'argo'}, where)

    # then
    assert links == [{'label': 'Argo CD app', 'url': f'https://argocd.example.com/applications/team-apps/{CHECKOUT}'}]
    assert (other, bare) == (None, [])
    assert found[0]['what'].endswith(f'. https://argocd.example.com/applications/team-apps/{CHECKOUT}')
    assert argocd.PLUGIN.probe_targets(where) == [{'name': 'argocd-main', 'url': 'https://argocd.example.com/healthz'}]


def test_an_empty_application_list_is_an_ok_pass_that_closes_nothing(store: Store):
    # given
    store.save_state({'anomalies': [], 'initialized': True, 'sources': {}}, None)
    answers = {'items': [app_in(CHECKOUT, 'shop', 'OutOfSync', 'Healthy', 'Failed')]}
    http = FakeHttp({('GET', LIST): lambda query, body: answers})

    # when
    with serving(http, store, main={'token': API_TOKEN}) as c:
        c.app.state.sweeper.run('argocd', 'argo', NOW)
        rows = store.load()['argo']
        answers['items'] = None
        c.app.state.sweeper.run('argocd', 'argo', NOW + 300)
        answers['items'] = []
        c.app.state.sweeper.run('argocd', 'argo', NOW + 600)
        state = store.load()
    quiet = run_check(argo_runtime(FakeHttp({('GET', LIST): {'items': []}})))

    # then
    assert cards(store)['argo|shop/checkout-service']['status'] == 'open'
    assert state['serverPasses']['argo']['status'] == f'ok {DOT} no data' and state['argo'] == rows
    assert (quiet.result.ok, quiet.result.status, quiet.result.ran, quiet.result.found, quiet.sections) == (True, f'ok {DOT} no data', ('argo:no-data',), [], {})


def test_an_app_outside_the_watched_projects_closes_its_card_at_the_next_pass(store: Store):
    # given
    store.save_state({'anomalies': [], 'initialized': True, 'sources': {}}, None)
    apps = [app_in(CHECKOUT, 'shop', 'OutOfSync', 'Healthy', 'Failed', project='shop-team'),
            app_in('payments-ledger-service', 'payments', 'OutOfSync', 'Healthy', 'Failed', project='payments-team')]
    http = FakeHttp({('GET', LIST): {'items': apps}, **{('GET', f"{LIST}/{a['metadata']['name']}"): a for a in apps}})

    # when
    with serving(http, store, main={'token': API_TOKEN}) as c:
        c.app.state.sweeper.run('argocd', 'argo', NOW)
        before = {k: a['status'] for k, a in cards(store).items()}
        with_instance(store, token=API_TOKEN, projects=' payments-team , ,other ')
        c.app.state.runtime.refresh()
        c.app.state.sweeper.run('argocd', 'argo', NOW + 300)
        after = {k: a['status'] for k, a in cards(store).items()}
        tools = c.app.state.investigator.sources.plugin_tools
        listed = tools.call('argo', {'op': 'apps'}, NOW)
        shown = tools.call('argo', {'op': 'app', 'name': 'payments-ledger-service'}, NOW)
        with pytest.raises(ToolError) as hidden:
            tools.call('argo', {'op': 'app', 'name': CHECKOUT}, NOW)

    # then
    assert before == {'argo|shop/checkout-service': 'open', 'argo|payments/ledger-service': 'open'}
    assert after == {'argo|shop/checkout-service': 'cleared', 'argo|payments/ledger-service': 'open'}
    assert [r.get('name') for r in listed] == ['payments-ledger-service', None]
    assert listed[-1] == {'note': '1 of 1 applications are not Synced or not Healthy'}
    assert shown['name'] == 'payments-ledger-service'
    assert hidden.value.text == f'Argo CD application {CHECKOUT} is not in the watched projects'


def test_a_cut_read_keeps_the_cards_of_the_apps_it_did_not_read(store: Store, monkeypatch, tmp_path):
    # given
    store.save_state({'anomalies': [], 'initialized': True, 'sources': {}}, None)
    monkeypatch.setattr(argo_module, 'PASS_PAGES', 2)
    late = app_in('zz-late-service', 'zz', 'OutOfSync', 'Healthy', 'Failed')
    pages = {'full': {None: {'metadata': {'continue': 'p2'}, 'items': [app_in(CHECKOUT, 'shop', 'OutOfSync', 'Healthy', 'Failed')]},
                      'p2': {'metadata': {'continue': ''}, 'items': [late]}},
             'cut': {None: {'metadata': {'continue': 'p2', 'remainingItemCount': 2}, 'items': [app_in(CHECKOUT, 'shop')]},
                     'p2': {'metadata': {'continue': 'p3', 'remainingItemCount': 1}, 'items': [app_in('web-storefront', 'web')]},
                     'p3': {'metadata': {'continue': ''}, 'items': [late]}}}
    mode = ['full']
    api = in_cluster(monkeypatch, tmp_path, {APPS: lambda query: pages[mode[0]][(query.get('continue') or [None])[0]]})
    with_instance(store, mode='in-cluster')

    # when
    with TestClient(create_app(settings(), store)) as c:
        c.app.state.sweeper.run('argocd', 'argo', NOW)
        before = {k: a['status'] for k, a in cards(store).items()}
        mode[0] = 'cut'
        api.calls.clear()
        c.app.state.sweeper.run('argocd', 'argo', NOW + 300)
        state = store.load()
    after = {k: a['status'] for k, a in cards(store).items()}

    # then
    assert before == {'argo|shop/checkout-service': 'open', 'argo|zz/late-service': 'open'}
    assert after == {'argo|shop/checkout-service': 'cleared', 'argo|zz/late-service': 'open'}
    assert len(api.calls) == 2
    assert state['serverPasses']['argo']['status'] == f'ok {DOT} read 2 of 3 applications'
    assert {r['app']: r['since'] for r in state['argo']} == {'zz-late-service': iso(NOW)}


def test_the_pass_pages_on_under_its_own_time_budget(monkeypatch, tmp_path):
    # given
    now = [1000.0]
    monkeypatch.setattr(argo_module, 'monotonic', lambda: now[0])

    def page(query):
        now[0] += 60
        at = int((query.get('continue') or ['0'])[0])
        return {'metadata': {'continue': str(at + 1)}, 'items': [app_in(f'shop-service-{at}', 'shop')]}

    api = in_cluster(monkeypatch, tmp_path, {APPS: page})

    # when
    sweep = run_check(argo_runtime(FakeHttp(), mode='in-cluster'))

    # then
    assert argo_module.PASS_BUDGET_S + argo_module.KUBE_TIMEOUT_S < CHECK_TIMEOUT_S and argo_module.PASS_PAGES > argo_module.PAGES
    assert len(api.calls) == 3 and argo_module.PASS_BUDGET_S == 180
    assert (sweep.result.ok, sweep.result.status) == (True, f'ok {DOT} read 3 applications, more not read')
    assert sweep.result.ran == ('argo:shop-service-0', 'argo:shop-service-1', 'argo:shop-service-2', 'argo:access:applications')


def test_api_mode_reads_the_applications_with_the_token_and_keeps_it_out_of_errors():
    # given
    http = FakeHttp({('GET', LIST): {'metadata': {}, 'items': [application(CHECKOUT, 'OutOfSync')]},
                     ('GET', f'{LIST}/{CHECKOUT}'): application(CHECKOUT, 'OutOfSync')})
    echo = FakeHttp({('GET', LIST): HttpError(401, LIST, json.dumps({'error': 'x', 'code': 16, 'message': f'token {API_TOKEN} is not valid'}))})
    odd = FakeHttp({('GET', LIST): ['not', 'an', 'object']})
    down = FakeHttp({('GET', LIST): Unreachable('x')})
    blank = FakeHttp({('GET', LIST): {'items': None}})

    # when
    a = Argo(ApiSource(URL, API_TOKEN, http), URL, 'argocd')
    rows = a.apps()
    one = a.app(CHECKOUT)
    errors = []
    for source in (ApiSource(URL, API_TOKEN, echo), ApiSource(URL, API_TOKEN, odd), ApiSource(URL, API_TOKEN, down)):
        with pytest.raises(ArgoError) as e:
            Argo(source, URL, 'argocd').apps()
        errors.append(e.value)
    empty = Argo(ApiSource(URL, '', blank), URL, 'argocd').apps()
    with pytest.raises(ToolError) as refused:
        argo_tools(echo, token=API_TOKEN).call('argo', {'op': 'apps'}, NOW)

    # then
    assert [r.get('name') for r in rows] == [CHECKOUT, None] and one['name'] == CHECKOUT
    assert [c[1] for c in http.calls] == [LIST, f'{LIST}/{CHECKOUT}'] and all(c[2]['Authorization'] == f'Bearer {API_TOKEN}' for c in http.calls)
    assert 'Authorization' not in blank.calls[0][2] and empty == [{'note': 'Argo CD listed no applications; a token without the RBAC permission applications, get sees none either'}]
    invalid = 'Argo CD refused to read applications (HTTP 401): the token is invalid or lacks the RBAC permission applications, get'
    assert [(e.status, e.reason) for e in errors] == [(401, invalid), (502, 'Argo CD applications answered without an object'), (502, 'Argo CD applications unreachable')]
    assert errors[0].message == invalid
    assert all(API_TOKEN not in e.message and API_TOKEN not in e.reason for e in errors)
    assert refused.value.text == invalid


def test_pass_table_offers_the_argo_rows_while_an_instance_serves(store: Store):
    # given
    rows = [{'app': CHECKOUT, 'ns': 'shop', 'svc': 'checkout-service', 'sync': 'OutOfSync', 'health': 'Degraded', 'phase': 'Failed', 'since': iso(NOW), 'plugin': 'argocd'},
            {'app': 'payments-ledger-service', 'ns': 'payments', 'svc': 'ledger-service', 'sync': 'Synced', 'health': 'Progressing', 'since': iso(NOW), 'plugin': 'argocd'}]
    store.save_state({**state(), 'argo': rows}, None)
    b = box(store, plugin_tools=argo_tools())

    # when
    tool = next(t for t in b.definitions() if t['name'] == 'pass_table')
    out, err = b.call('pass_table', {'source': 'argo', 'name': 'checkout'})

    # then
    assert 'argo' in tool['input_schema']['properties']['source']['enum'] and 'the argo check of Argo CD' in tool['description']
    assert err is False and json.loads(out)['rows'] == rows[:1]


def test_a_pass_keeps_only_the_fields_it_reads_of_each_listed_application():
    # given
    big = application(CHECKOUT, 'OutOfSync', 'Degraded', 'Failed')
    http = FakeHttp({('GET', LIST): {'items': [big]}})

    # when
    [kept] = Argo(ApiSource(URL, 'tok', http), URL, 'argocd').read().items

    # then
    assert 'SECRET' not in json.dumps(kept) and 'history' not in kept['status'] and 'managedFields' not in json.dumps(kept)
    assert [r['name'] for r in kept['status']['resources']] == [r['name'] for r in big['status']['resources'] if (r.get('health') or {}).get('status', 'Healthy') != 'Healthy'
                                                                 or r.get('status', 'Synced') != 'Synced']
    assert evaluate([kept], [], NOW, URL, 'argocd')[0][0]['what'] == evaluate([big], [], NOW, URL, 'argocd')[0][0]['what'], 'the card reads the same from the kept fields'


def test_the_api_test_asks_the_list_for_names_only():
    # given
    http = FakeHttp({('GET', VERSION): {'Version': 'v2.13.1'}, ('GET', LIST): {'items': [{'metadata': {'name': 'a'}}, {'metadata': {'name': 'b'}}]}})

    # when
    result = connection_of(http, token='tok')

    # then
    assert (result.ok, result.summary) == (True, 'Argo CD v2.13.1: 2 applications')
    assert 'fields=items.metadata.name' in http.hits(LIST)[0][1]


@pytest.mark.parametrize('hang', [TimeoutError('timed out'), urllib.error.URLError(TimeoutError('timed out'))])
def test_a_hung_in_cluster_test_says_it_timed_out(monkeypatch, tmp_path, hang):
    # given
    in_cluster(monkeypatch, tmp_path, {APPS: hang})

    # when
    result = connection_of(mode='in-cluster')

    # then
    assert (result.ok, result.summary) == (False, msg('connection.timeout'))


def test_the_projects_note_says_when_the_listing_was_cut(monkeypatch, tmp_path):
    # given
    page = {'items': [app_in('web-storefront', 'web', project='web')], 'metadata': {'continue': 'next'}}
    in_cluster(monkeypatch, tmp_path, {APPS: page})

    # when
    rows = argo_tools(None, mode='in-cluster', projects='shop').call('argo', {'op': 'apps'}, NOW)

    # then
    assert rows[-1]['note'].startswith('no application is in the watched projects (') and 'Argo CD holds more than the' in rows[-1]['note']
