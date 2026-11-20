import json

import mongomock
import pytest
from fastapi.testclient import TestClient

from skopax import toolusage
from skopax.app import TEST_TIMEOUT_S as CORE_TEST_TIMEOUT_S, create_app
from skopax.contract import InstanceInfo
from skopax.instances import Instances, InvalidInstance, check_name
from skopax.investigator.tools import ToolError
from skopax.messages import msg
from skopax.plugins import vwo
from skopax.plugins.vwo.wingify import APP, VERDICT_SECTION
from skopax.registry import BUILTIN, Registry, describe
from skopax.remote import HttpError, Unreachable
from skopax.runtime import Runtime
from skopax.sealer import Sealer, master_key
from skopax.store import Store
from tests.conftest import settings
from tests.fake_http import FakeHttp
from tests.plugin_kit import KIT_KEY, add_instance
from tests.test_wingify import API, DOT, SHOP, TOKEN, vwo_runtime, vwo_tools
from tests.test_wingify_verdicts import listed, winner

NOW = 1_794_380_000.0
ACCOUNT = f'{API}/accounts/current'
EAST = f'{API}/accounts/12345'


async def no_loop(self, every=0):
    return None


def connection_of(transport, **values):
    runtime = vwo_runtime(transport, **values)
    plugin = vwo.PLUGIN
    return plugin.test(runtime.context(plugin, runtime.instances.get(plugin.id, 'main'), NOW))


def serving(http: FakeHttp, store: Store, **instances: dict) -> TestClient:
    key = master_key(store.db, settings())
    for index, (name, values) in enumerate(instances.items()):
        add_instance(store, vwo.PLUGIN, name, sealer_key=key, primary=index == 0, **({'api-token': TOKEN} | values))
    return TestClient(create_app(settings(), store, transport=http))


def wingify_cards(state: dict) -> dict[str, dict]:
    return {a['key']: a for a in state['anomalies'] if a.get('src') == 'wingify'}


def by_ran(state: dict) -> dict[str, dict]:
    return {a['ranKey']: a for a in wingify_cards(state).values()}


def started(store: Store) -> None:
    store.save_state({'anomalies': [], 'initialized': True, 'sources': {}}, None)


def test_vwo_identity_and_manifest():
    # given
    plugin = vwo.PLUGIN

    # when
    manifest = describe(plugin, icon=True)
    loaded = Registry.load({}, entry_points=False).get('vwo')

    # then
    assert json.loads(json.dumps(manifest, allow_nan=False)) == manifest
    assert 'skopax.plugins.vwo' in BUILTIN and (loaded.state, loaded.reason) == ('ok', None)
    assert (manifest['id'], manifest['name'], manifest['version'], manifest['category'], manifest['author'], manifest['licence'], manifest['core'], manifest['edition']) == (
        'vwo', 'VWO', '0.1.0', 'marketing', 'Skopax', 'Apache-2.0', '>=0.1,<0.4', 'community')
    assert manifest['capabilities'] == {'hosts': ['app.wingify.com'], 'secrets': ['api-token'], 'agentReads': False}
    assert manifest['uses'] == [] and manifest['maxInstances'] is None and manifest['rules'] == [] and [s['key'] for s in manifest['settings']] == ['auto-resolve-hours'] and manifest['panels'] == []
    assert [(c['id'], c['period'], c['offset'], c['ran']) for c in manifest['checks']] == [('wingify', 300, 225, ['wingify']), ('wingify-verdict', 3600, 1560, ['wingify-verdict'])]
    checks = {c.id: c for c in plugin.checks}
    assert (checks['wingify'].sections, checks['wingify'].tool, checks['wingify'].once) == (('wingify',), 'wingify', False)
    assert (checks['wingify-verdict'].sections, checks['wingify-verdict'].tool, checks['wingify-verdict'].once) == ((VERDICT_SECTION,), 'wingify', False)
    assert set(checks['wingify'].section_help) == {'wingify'} and set(checks['wingify-verdict'].section_help) == {VERDICT_SECTION}
    assert [(f['key'], f['kind'], f['required'], f['ui'], f['showIf']) for f in manifest['fields']] == [('api-token', 'secret', True, False, None), ('account-id', 'text', False, False, None)]
    assert [(t['name'], t['kind']) for t in manifest['tools']] == [('wingify', 'read')] and manifest['knowledge'] == ['vwo']
    [tool] = plugin.tools
    assert (tool.per_run, tool.scan, tool.instance_input, tool.input_schema['additionalProperties']) == (6, True, 'instance', False)
    assert plugin.description.endswith('.') and plugin.description.count('.') == 1
    assert Registry.of(plugin).icon('vwo').startswith('<svg') and len(plugin.knowledge[0].text.splitlines()) <= 40 and plugin.knowledge[0].slack


def test_vwo_connection_test_reports_ok_and_failures():
    # given
    empty = {('GET', f'{ACCOUNT}/campaigns'): {'_data': []}}
    good = FakeHttp({('GET', ACCOUNT): {'_data': {'id': 12345, 'name': 'Shop  marketing'}}, ('GET', f'{ACCOUNT}/campaigns'): {'_data': [listed(5)]},
                     ('GET', f'{ACCOUNT}/campaigns/5'): {'_data': winner(5)}})
    bare = FakeHttp({('GET', ACCOUNT): {'_data': {'id': 12345}}} | empty)
    blank = FakeHttp({('GET', EAST): {'_data': None}, ('GET', f'{EAST}/campaigns'): {'_data': []}})
    personal = FakeHttp({('GET', ACCOUNT): {'_data': {'id': 1, 'name': 'alice@example.com'}}} | empty)
    refused = FakeHttp({('GET', ACCOUNT): HttpError(401, ACCOUNT, f'{{"message": "token {TOKEN} refused"}}')})
    forbidden = FakeHttp({('GET', ACCOUNT): HttpError(403, ACCOUNT)})
    down = FakeHttp({('GET', ACCOUNT): Unreachable('x')})
    no_list = FakeHttp({('GET', ACCOUNT): {'_data': {'id': 1}}, ('GET', f'{ACCOUNT}/campaigns'): HttpError(403, f'{ACCOUNT}/campaigns', f'no rights for {TOKEN}')})
    no_results = FakeHttp({('GET', ACCOUNT): {'_data': {'id': 1}}, ('GET', f'{ACCOUNT}/campaigns'): {'_data': [listed(5)]},
                           ('GET', f'{ACCOUNT}/campaigns/5'): HttpError(403, f'{ACCOUNT}/campaigns/5')})
    revoked_list = FakeHttp({('GET', ACCOUNT): {'_data': {'id': 1}}, ('GET', f'{ACCOUNT}/campaigns'): HttpError(401, f'{ACCOUNT}/campaigns')})
    revoked_results = FakeHttp({('GET', ACCOUNT): {'_data': {'id': 1}}, ('GET', f'{ACCOUNT}/campaigns'): {'_data': [listed(5)]},
                                ('GET', f'{ACCOUNT}/campaigns/5'): HttpError(401, f'{ACCOUNT}/campaigns/5')})
    missing = FakeHttp({('GET', ACCOUNT): HttpError(404, ACCOUNT)})

    # when
    results = [connection_of(good), connection_of(bare), connection_of(blank, **{'account-id': '12345'}), connection_of(personal), connection_of(refused),
               connection_of(forbidden), connection_of(down), connection_of(no_list), connection_of(no_results), connection_of(revoked_list),
               connection_of(revoked_results), connection_of(missing)]

    # then
    assert [(r.ok, r.summary) for r in results] == [(True, 'VWO account Shop marketing'), (True, 'VWO account 12345'), (True, 'VWO account 12345'),
                                                   (True, 'VWO account [redacted]'), (False, msg('connection.rejected', status=401)),
                                                   (False, 'VWO cannot read the account: the token lacks Browse permission (HTTP 403)'), (False, msg('connection.unreachable')),
                                                   (False, 'VWO cannot read campaigns: the token lacks Browse permission (HTTP 403)'),
                                                   (False, 'VWO cannot read campaign results: the token lacks Browse permission (HTTP 403)'),
                                                   (False, msg('connection.rejected', status=401)), (False, msg('connection.rejected', status=401)),
                                                   (False, 'VWO account id current was not found (HTTP 404)')]
    assert [len(c.calls) for c in (forbidden, missing, revoked_list)] == [1, 1, 2], 'a failed account read stops the test there'
    assert good.calls[0][:3] == ('GET', ACCOUNT, {'Accept': 'application/json', 'token': TOKEN})
    assert [c[1] for c in good.calls] == [ACCOUNT, f'{ACCOUNT}/campaigns?limit=1&offset=0', f'{ACCOUNT}/campaigns/5'] and len(bare.calls) == 2
    assert all(TOKEN not in r.summary for r in results) and all(isinstance(r.ms, int) and r.ms >= 0 for r in results)
    assert vwo.TEST_TIMEOUT_S * 3 < CORE_TEST_TIMEOUT_S


def test_the_vwo_test_skips_the_reads_only_switched_off_checks_make():
    # given
    def tested(off: set[str]):
        http = FakeHttp({('GET', ACCOUNT): {'_data': {'id': 1, 'name': 'Shop'}}, ('GET', f'{ACCOUNT}/campaigns'): {'_data': [listed(5)]},
                         ('GET', f'{ACCOUNT}/campaigns/5'): HttpError(403, f'{ACCOUNT}/campaigns/5')})
        runtime = vwo_runtime(http)
        return runtime.connection_test(vwo.PLUGIN, 'main', {}, 5, NOW, off_checks=off), [c[1] for c in http.calls]

    # when
    both, no_verdict, no_changes, neither = tested(set()), tested({'wingify-verdict'}), tested({'wingify'}), tested({'wingify', 'wingify-verdict'})

    # then
    refused = (False, 'VWO cannot read campaign results: the token lacks Browse permission (HTTP 403)')
    assert (both[0]['ok'], both[0]['summary']) == refused and (no_changes[0]['ok'], no_changes[0]['summary']) == refused
    assert (no_verdict[0]['ok'], no_verdict[0]['summary'], no_verdict[1]) == (True, 'VWO account Shop', [ACCOUNT, f'{ACCOUNT}/campaigns?limit=1&offset=0'])
    assert (neither[0]['ok'], neither[0]['summary'], neither[1]) == (True, 'VWO account Shop', [ACCOUNT])


def test_vwo_defaults_are_neutral():
    # when
    defaults = {f.key: f.default for f in vwo.PLUGIN.fields}

    # then
    assert defaults == {'api-token': '', 'account-id': 'current'}
    assert {s.key: s.default for s in vwo.PLUGIN.settings} == {'auto-resolve-hours': '24'}


def test_vwo_second_instance_is_scoped(store: Store):
    # given
    started(store)
    http = FakeHttp({('GET', f'{ACCOUNT}/campaigns'): {'_data': [listed(5)]}, ('GET', f'{ACCOUNT}/campaigns/5'): {'_data': winner(5)},
                     ('GET', f'{EAST}/campaigns'): {'_data': [listed(8)]}, ('GET', f'{EAST}/campaigns/8'): {'_data': winner(8)}})

    # when
    with serving(http, store, main={}, east={'account-id': '12345'}) as c:
        c.app.state.sweeper.run('vwo', 'wingify-verdict', NOW)
        c.app.state.sweeper.run('vwo', 'wingify', NOW)
        state = store.load()

    # then
    found = {a['campaignId']: a for a in wingify_cards(state).values()}
    main, east = found[5], found[8]
    assert 'cluster' not in main and main['ranKey'] == 'wingify-verdict' and main['key'] == 'ab-verdict|-/wingify|c5'
    assert (east['cluster'], east['ranKey'], east['status']) == ('east', 'wingify-verdict-east', 'open') and east['short'].startswith('WARN EAST ')
    passes = state['serverPasses']
    assert {k: passes[k]['status'] for k in ('wingify', 'wingify-east', 'wingify-verdict', 'wingify-verdict-east')} == dict.fromkeys(
        ('wingify', 'wingify-east', 'wingify-verdict', 'wingify-verdict-east'), 'ok')
    assert {(r['id'], r.get('cluster'), r['plugin']) for r in state[VERDICT_SECTION]} == {('5', None, 'vwo'), ('8', 'east', 'vwo')}
    assert {(r['part'], r.get('id'), r.get('cluster')) for r in state['wingify']} == {('summary', None, None), ('summary', None, 'east'), ('campaign', 5, None), ('campaign', 8, 'east')}


def test_verdict_is_not_an_instance_name():
    # given
    registry = Registry.load({}, entry_points=False)
    db = mongomock.MongoClient(tz_aware=True)['vwo-names']
    instances = Instances(db, registry, lambda: Sealer(KIT_KEY))

    # when
    refused = []
    for name in ('verdict', 'verdict-east', 'wingify', 'wingify-verdict'):
        with pytest.raises(InvalidInstance) as e:
            check_name(registry, name, 'vwo')
        refused.append((e.value.field, str(e.value)))
    with pytest.raises(InvalidInstance) as made:
        instances.create('vwo', 'verdict', {'api-token': TOKEN}, True, 'alice')
    check_name(registry, 'east', 'vwo')

    # then
    assert refused == [('name', msg('instances.invalid.reserved', name=n)) for n in ('verdict', 'verdict-east', 'wingify', 'wingify-verdict')]
    assert (made.value.field, str(made.value)) == ('name', msg('instances.invalid.reserved', name='verdict')) and instances.all('vwo') == []


def test_unticking_the_verdict_check_reads_no_results_and_closes_its_cards_while_the_tool_still_reads(store: Store, monkeypatch):
    # given
    monkeypatch.setattr(Runtime, 'loop', no_loop)
    started(store)
    http = FakeHttp({('GET', f'{ACCOUNT}/campaigns'): {'_data': [listed(5)]}, ('GET', f'{ACCOUNT}/campaigns/5'): {'_data': winner(5)}})

    # when
    with serving(http, store, main={}) as c:
        c.app.state.sweeper.run('vwo', 'wingify-verdict', NOW)
        opened = wingify_cards(store.load())
        c.app.state.instances.set_checks('vwo', 'main', ['wingify-verdict'], 'alice')
        c.app.state.runtime.refresh()
        asked = len(http.calls)
        skipped = c.app.state.sweeper.run('vwo', 'wingify-verdict', NOW + 3600)
        quiet = len(http.calls)
        report = c.app.state.investigator.sources.plugin_tools.call('wingify', {'op': 'report', 'id': 5}, NOW + 3600)
        state = store.load()

    # then
    assert opened['ab-verdict|-/wingify|c5']['status'] == 'open'
    closed = wingify_cards(state)['ab-verdict|-/wingify|c5']
    assert closed['status'] == 'cleared' and closed['expired']
    assert skipped is None and quiet == asked, 'an unticked check reads nothing'
    assert 'wingify-verdict' not in state['serverPasses'] and not [r for r in state.get(VERDICT_SECTION) or [] if r.get('plugin') == 'vwo']
    assert report['from'] == 'Wingify API' and report['campaign']['id'] == 5


def test_the_change_check_keeps_its_rows_in_the_state_and_raises_a_card_with_the_page_and_vwo_links(store: Store):
    # given
    started(store)
    answers = {'_data': [listed(5), listed(9, 'PAUSED')]}
    http = FakeHttp({('GET', f'{ACCOUNT}/campaigns'): lambda query, body: answers})

    # when
    with serving(http, store, main={}) as c:
        c.app.state.sweeper.run('vwo', 'wingify', NOW)
        learned = wingify_cards(store.load())
        answers['_data'] = [listed(5), listed(9)]
        c.app.state.sweeper.run('vwo', 'wingify', NOW + 300)
        state = store.load()

    # then
    card = wingify_cards(state)['ab-test|-/wingify|c9']
    assert learned == {} and card['status'] == 'open' and card['label'] == 'A/B test started' and card['url'] == f'{SHOP}/products/9'
    assert card['links'][:2] == [{'label': 'Open the page', 'url': f'{SHOP}/products/9'}, {'label': 'VWO', 'url': APP}]
    assert [r['part'] for r in state['wingify']] == ['summary', 'campaign', 'campaign', 'change']
    assert state['serverPasses']['wingify']['status'] == 'ok'


def test_links_belong_to_wingify_cards_and_open_only_https_pages():
    # given
    info = InstanceInfo('main', True, None, {'api-token': TOKEN})

    # when
    page = vwo.PLUGIN.links({'src': 'wingify', 'url': f'{SHOP}/products'}, info)
    plain = vwo.PLUGIN.links({'src': 'wingify', 'url': 'http://shop.example.com/products'}, info)
    other = vwo.PLUGIN.links({'src': 'prom', 'url': f'{SHOP}/products'}, info)

    # then
    assert page == [{'label': 'Open the page', 'url': f'{SHOP}/products'}, {'label': 'VWO', 'url': APP}]
    assert plain == [{'label': 'VWO', 'url': APP}] and other is None
    assert vwo.PLUGIN.probe_targets is None


def test_both_checks_count_as_calls_of_the_wingify_tool():
    # given
    checks = [{'name': 'wingify', 'ok': True, 'ms': 800, 'at': '2026-10-23T10:05:00Z'},
              {'name': 'wingify-verdict', 'ok': True, 'ms': 2000, 'at': '2026-10-23T11:00:00Z'}]

    # when
    out = toolusage.summary([], 7, '2026-10-22T00:00:00Z', checks, vwo_tools(FakeHttp()))
    by = {t['name']: t for t in out['tools']}

    # then
    assert out['checkRuns'] == 2 and by['wingify']['calls'] == 2 and by['wingify']['kind'] == 'read'
    assert by['wingify']['byCheck'] == {'wingify': {'runs': 1, 'errors': 0}, 'wingify-verdict': {'runs': 1, 'errors': 0}}


def test_tool_errors_reach_the_model_as_fixed_texts_that_name_the_missing_permission_without_the_token():
    # given
    refused = FakeHttp({('GET', f'{ACCOUNT}/campaigns/5'): HttpError(401, f'{ACCOUNT}/campaigns/5', f'token {TOKEN} refused'),
                        ('GET', f'{ACCOUNT}/campaigns'): HttpError(403, f'{ACCOUNT}/campaigns', f'no rights for {TOKEN}')})
    tools = vwo_tools(refused)

    # when
    errors = []
    for args in ({'op': 'report', 'id': 5}, {'op': 'campaigns'}, {'op': 'delete'}, {'op': 'campaign'}):
        with pytest.raises(ToolError) as e:
            tools.call('wingify', args, NOW)
        errors.append(e.value.text)

    # then
    assert errors == ['Wingify HTTP 401 on /campaigns/5: the token was refused (it needs Browse permission)',
                      'Wingify HTTP 403 on /campaigns: the token may not read this (Browse permission is enough)',
                      'op is one of campaigns, campaign, report', 'op campaign needs id, the campaign number from op campaigns']
    assert all(TOKEN not in text for text in errors) and len(refused.calls) == 2


def test_a_refused_listing_keeps_the_verdict_cards_open_and_its_access_card_clears_when_access_returns(store: Store):
    # given
    started(store)
    answers = {'list': {'_data': [listed(5)]}}
    http = FakeHttp({('GET', f'{ACCOUNT}/campaigns'): lambda query, body: answers['list'], ('GET', f'{ACCOUNT}/campaigns/5'): {'_data': winner(5)}})

    # when
    with serving(http, store, main={}) as c:
        c.app.state.sweeper.run('vwo', 'wingify-verdict', NOW)
        answers['list'] = HttpError(403, f'{ACCOUNT}/campaigns', f'no rights for {TOKEN}')
        c.app.state.sweeper.run('vwo', 'wingify-verdict', NOW + 3600)
        refused = store.load()
        answers['list'] = {'_data': [listed(5)]}
        c.app.state.sweeper.run('vwo', 'wingify-verdict', NOW + 7200)
        back = store.load()

    # then
    assert refused['serverPasses']['wingify-verdict']['status'] == f'ok {DOT} no access to campaigns'
    assert by_ran(refused)['wingify-verdict']['status'] == 'open' and by_ran(refused)['wingify-verdict:access:campaigns']['status'] == 'open'
    assert by_ran(refused)['wingify-verdict:access:campaigns']['label'] == 'VWO cannot read campaigns: the token lacks Browse permission'
    assert by_ran(back)['wingify-verdict']['status'] == 'open' and by_ran(back)['wingify-verdict:access:campaigns']['status'] == 'cleared'
    assert [r['id'] for r in back[VERDICT_SECTION]] == ['5']
