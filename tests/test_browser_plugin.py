import json

import pytest
from fastapi.testclient import TestClient

from skopax import clock
from skopax.app import create_app
from skopax.engine import iso
from skopax.investigator.toolset import ToolSet
from skopax.messages import msg
from skopax.plugins import browser, website
from skopax.plugintools import PluginTools
from skopax.registry import BUILTIN, Registry, describe
from skopax.remote import HttpError
from skopax.sealer import master_key
from skopax.store import Store
from tests.conftest import AUTH, settings
from tests.fake_cdp import FakeChrome, Site
from tests.fake_http import FakePages
from tests.plugin_kit import add_instance
from tests.test_investigator import NOW
from tests.website_kit import CHAIRS, SITE, quiet, site_box, through, website_runtime

DOWN_URL = 'http://127.0.0.1:9'
ROW_KEYS = {'name', 'ok', 'version', 'lastSeen', 'error'}


@pytest.fixture(autouse=True)
def no_waiting(monkeypatch):
    quiet(monkeypatch)


@pytest.fixture
def chrome():
    c = FakeChrome({CHAIRS: Site()})
    yield c
    c.close()


def url_of(c: FakeChrome) -> str:
    return f'http://127.0.0.1:{c.port}'


def app_with(store: Store, instances: dict[str, dict], browsers: dict[str, dict]):
    key = master_key(store.db, settings())
    for i, (name, values) in enumerate(browsers.items()):
        add_instance(store, browser.PLUGIN, name, sealer_key=key, primary=i == 0, **values)
    for i, (name, values) in enumerate(instances.items()):
        add_instance(store, website.PLUGIN, name, sealer_key=key, primary=i == 0, **({'url': SITE} | values))
    return TestClient(create_app(settings(), store, transport=through(FakePages())))


def cards(store: Store) -> dict:
    return {a['key']: a for a in store.load()['anomalies']}


def browser_rows(store: Store) -> list[dict]:
    return [r for r in store.load().get('browser') or [] if r.get('plugin') == 'browser']


def test_the_built_in_registry_loads_the_browser_plugin():
    # when
    loaded = Registry.load({}, entry_points=False)
    manifest = describe(browser.PLUGIN, icon=True)

    # then
    assert 'skopax.plugins.browser' in BUILTIN and loaded.get('browser').state == 'ok' and loaded.get('browser').plugin is browser.PLUGIN
    assert loaded.icon('browser').startswith('<svg')
    assert json.loads(json.dumps(manifest, allow_nan=False)) == manifest
    assert (manifest['id'], manifest['name'], manifest['category']) == ('browser', 'Headless browser', 'other')
    assert [(f['key'], f['kind'], f['required']) for f in manifest['fields']] == [('url', 'url', True)]
    assert manifest['capabilities']['secrets'] == [] and manifest['capabilities']['hosts'] == ['@url']
    assert 'browser' in website.PLUGIN.uses


def test_a_website_instance_with_a_picked_browser_takes_shots_through_its_url(store: Store, chrome: FakeChrome):
    # given
    b = site_box(store, through(FakePages()), {'main': {'browser': 'chrome'}}, browsers={'chrome': {'url': url_of(chrome)}})
    runtime = b.sources.plugin_tools.runtime

    # when
    offered = [d['name'] for d in PluginTools(runtime).definitions()]
    out, err = b.call('screenshot', {'url': CHAIRS})

    # then
    assert {'page', 'screenshot', 'page_scan'} <= set(offered)
    assert err is False and 'Garden Chairs' in str(out) and len(b.shots) == 1
    assert chrome.methods().count('Page.captureScreenshot') == 1


def test_the_old_browser_url_wins_over_a_picked_browser(store: Store, chrome: FakeChrome):
    # given
    other = FakeChrome({CHAIRS: Site()})
    try:
        b = site_box(store, through(FakePages()), {'main': {'browser-url': url_of(chrome), 'browser': 'other'}}, browsers={'other': {'url': url_of(other)}})

        # when
        out, err = b.call('screenshot', {'url': CHAIRS})

        # then
        assert err is False and chrome.methods().count('Page.captureScreenshot') == 1
        assert other.methods().count('Page.captureScreenshot') == 0
    finally:
        other.close()


@pytest.mark.parametrize('instance,browsers', [
    ({}, {}),
    ({}, {'chrome': {'url': 'http://browser:9222'}}),
    ({'browser': 'chrome'}, {'chrome': {'url': 'http://browser:9222', 'enabled': False}}),
    ({'browser': 'gone'}, {'chrome': {'url': 'http://browser:9222'}}),
])
def test_without_a_browser_the_screenshot_tools_are_not_offered(store: Store, instance: dict, browsers: dict):
    # given
    enabled = {k: {u: v for u, v in values.items() if u != 'enabled'} for k, values in browsers.items()}
    runtime = website_runtime(store, through(FakePages()), {'main': instance}, enabled)
    if any(v.get('enabled') is False for v in browsers.values()):
        for name in browsers:
            store.db['instances'].update_one({'_id': f'browser/{name}'}, {'$set': {'enabled': False}})
        runtime.refresh()

    # when
    offered = [d['name'] for d in PluginTools(runtime).definitions()]
    held = ToolSet(runtime).status('screenshot', None)

    # then
    assert offered == ['page']
    assert held == {'state': 'off', 'reason': msg('tools.off.needs_field', field='Headless browser')}


def test_the_browser_plugin_owns_the_health_row_and_the_cards(store: Store, chrome: FakeChrome):
    # given
    assert [c.id for c in website.PLUGIN.checks if c.id == 'browser'] == []
    with app_with(store, {'main': {'browser': 'chrome'}}, {'chrome': {'url': url_of(chrome)}}) as c:
        sweeper = c.app.state.sweeper

        # when
        sweeper.run('browser', 'browser', NOW)
        answered = browser_rows(store)
        store.db['instances'].update_one({'_id': 'browser/chrome'}, {'$set': {'values.url': DOWN_URL}})
        c.app.state.runtime.refresh()
        sweeper.run('browser', 'browser', NOW + 120)
        sweeper.run('browser', 'browser', NOW + 300)
        late = dict(cards(store))

    # then
    assert answered == [{'name': 'browser', 'ok': True, 'version': 'Chrome/157.0.8103.15', 'lastSeen': iso(NOW), 'error': None, 'plugin': 'browser'}]
    down = late['browser-down|127.0.0.1']
    assert (down['status'], down['sev'], down['label'], down['src']) == ('open', 'warn', 'Screenshot browser not answering', 'browser')
    assert browser.ACCESS_KEY == 'access|-/website|browser' and browser.ACCESS == 'browser:access:browser'


def test_the_connection_test_reads_the_browser_version(store: Store, chrome: FakeChrome):
    # given
    with app_with(store, {}, {}) as c:
        # when
        good = c.post('/api/admin/plugins/browser/test', json={'values': {'url': url_of(chrome)}}, headers=AUTH).json()
        bad = c.post('/api/admin/plugins/browser/test', json={'values': {'url': DOWN_URL}}, headers=AUTH).json()

    # then
    assert good['ok'] is True and 'Chrome/157.0.8103.15' in good['summary']
    assert bad['ok'] is False


def test_moving_the_old_browser_url_creates_the_instance_and_switches_the_website(store: Store, chrome: FakeChrome):
    # given
    up = url_of(chrome)
    with app_with(store, {'main': {}}, {}) as c:
        store.db['instances'].update_one({'_id': 'website/main'}, {'$set': {'values.browser-url': up}})
        c.app.state.runtime.refresh()

        # when
        refused = c.post('/api/admin/plugins/website/instances/main/move-browser')
        moved = c.post('/api/admin/plugins/website/instances/main/move-browser', headers=AUTH)
        listed = {p['id']: p for p in c.get('/api/admin/plugins', headers=AUTH).json()['plugins']}
        again = c.post('/api/admin/plugins/website/instances/main/move-browser', headers=AUTH)
        offered = [d['name'] for d in PluginTools(c.app.state.runtime).definitions()]

    # then
    assert refused.status_code in (401, 403) and moved.status_code == 200, moved.text
    name = moved.json()['browser']
    made = {i['name']: i for i in listed['browser']['instances']}[name]
    assert made['values']['url'] == up
    site = {i['name']: i for i in listed['website']['instances']}['main']
    assert site['values'].get('browser-url', '') == '' and site['values']['browser'] == name
    texts = [d['text'] for d in store.activity.find()]
    assert any(name in t and 'Headless browser' in t for t in texts) and any('main' in t and 'browser' in t for t in texts)
    assert again.status_code == 409
    assert 'screenshot' in offered


def test_browser_check_writes_a_row_and_a_card_after_5_min(store: Store, chrome: FakeChrome):
    # given
    up = f'http://127.0.0.1:{chrome.port}'
    with app_with(store, {}, {'main': {'url': up}}) as c:
        sweeper = c.app.state.sweeper

        # when
        sweeper.run('browser', 'browser', NOW)
        answered = browser_rows(store)
        store.db['instances'].update_one({'_id': 'browser/main'}, {'$set': {'values.url': DOWN_URL}})
        c.app.state.runtime.refresh()
        sweeper.run('browser', 'browser', NOW + 120)
        early = (browser_rows(store), dict(cards(store)))
        sweeper.run('browser', 'browser', NOW + 300)
        late = (browser_rows(store), dict(cards(store)))
        store.db['instances'].update_one({'_id': 'browser/main'}, {'$set': {'values.url': up}})
        c.app.state.runtime.refresh()
        sweeper.run('browser', 'browser', NOW + 600)
        back = (browser_rows(store), dict(cards(store)))

    # then
    assert answered == [{'name': 'browser', 'ok': True, 'version': 'Chrome/157.0.8103.15', 'lastSeen': iso(NOW), 'error': None, 'plugin': 'browser'}]
    assert early[0] == [{'name': 'browser', 'ok': False, 'version': 'Chrome/157.0.8103.15', 'lastSeen': iso(NOW), 'error': 'Unreachable', 'plugin': 'browser'}]
    assert 'browser-down|127.0.0.1' not in early[1]
    down = late[1]['browser-down|127.0.0.1']
    assert (down['status'], down['sev'], down['label'], down['src']) == ('open', 'warn', 'Screenshot browser not answering', 'browser')
    assert late[0][0]['ok'] is False and late[0][0]['lastSeen'] == iso(NOW)
    assert back[0][0]['ok'] is True and back[0][0]['lastSeen'] == iso(NOW + 600)
    assert back[1]['browser-down|127.0.0.1']['status'] == 'cleared'
    assert store.load()['sources']['browser'] == 'ok'


def test_one_failed_read_after_an_answer_opens_no_card_and_the_second_does(store: Store, chrome: FakeChrome):
    # given
    up = f'http://127.0.0.1:{chrome.port}'
    with app_with(store, {}, {'main': {'url': up}}) as c:
        sweeper = c.app.state.sweeper
        sweeper.run('browser', 'browser', NOW)
        store.db['instances'].update_one({'_id': 'browser/main'}, {'$set': {'values.url': DOWN_URL}})
        c.app.state.runtime.refresh()

        # when
        sweeper.run('browser', 'browser', NOW + 300)
        first = dict(cards(store))
        sweeper.run('browser', 'browser', NOW + 600)
        second = dict(cards(store))

    # then
    assert 'browser-down|127.0.0.1' not in first
    assert second['browser-down|127.0.0.1']['status'] == 'open' and f'last answered at {clock.date(NOW)} {clock.label(NOW)}.' in second['browser-down|127.0.0.1']['what']


def test_a_browser_card_never_starts_an_investigation():
    # given
    from skopax.investigator.select import eligible
    card = browser.down_card('browser', None) | {'status': 'open', 'firstSeen': iso(NOW - 60)}
    other = card | {'key': 'err|shop/checkout-service', 'src': 'demo'}
    history = [{'t': iso(NOW - 60), 'key': k, 'ev': 'new', 'sev': 'warn'} for k in (card['key'], other['key'])]

    # when
    picked = eligible({'anomalies': [card, other], 'history': history}, 'live', [], NOW, 5)

    # then
    assert [a['key'] for a in picked] == ['err|shop/checkout-service']


def test_a_browser_that_never_answered_gets_its_card_at_the_second_failed_pass(store: Store):
    # given
    with app_with(store, {}, {'main': {'url': DOWN_URL}}) as c:
        sweeper = c.app.state.sweeper

        # when
        sweeper.run('browser', 'browser', NOW)
        first = dict(cards(store))
        sweeper.run('browser', 'browser', NOW + 300)
        second = dict(cards(store))

    # then
    assert 'browser-down|127.0.0.1' not in first
    assert second['browser-down|127.0.0.1']['status'] == 'open' and '5 min' not in second['browser-down|127.0.0.1']['what']
    assert 'not answered since it was set up' in second['browser-down|127.0.0.1']['what']
    assert browser_rows(store) == [{'name': 'browser', 'ok': False, 'version': None, 'lastSeen': None, 'error': 'Unreachable', 'plugin': 'browser'}]


def test_the_browser_row_has_the_keys_the_nodes_panel_reads(store: Store, chrome: FakeChrome):
    # given
    with app_with(store, {}, {'main': {'url': f'http://127.0.0.1:{chrome.port}'}, 'east': {'url': DOWN_URL}}) as c:

        # when
        c.app.state.sweeper.run('browser', 'browser', NOW)

    # then
    rows = browser_rows(store)
    assert [set(r) - {'plugin', 'cluster'} for r in rows] == [ROW_KEYS, ROW_KEYS]
    assert [(r['name'], r['ok'], r.get('cluster')) for r in rows] == [('browser', True, None), ('browser', False, 'east')]
    assert all(isinstance(r['lastSeen'], str) for r in rows if r['ok']) and rows[1]['error'] == 'Unreachable'


def test_a_refused_browser_read_raises_the_access_card_and_keeps_the_down_card(store: Store, chrome: FakeChrome):
    # given
    def refusing(method, url, headers, data, timeout):
        raise HttpError(403, url)

    up = f'http://127.0.0.1:{chrome.port}'
    with app_with(store, {}, {'main': {'url': DOWN_URL}}) as c:
        sweeper = c.app.state.sweeper
        sweeper.run('browser', 'browser', NOW)
        sweeper.run('browser', 'browser', NOW + 300)
        c.app.state.runtime.transport = refusing

        # when
        sweeper.run('browser', 'browser', NOW + 600)
        refused = (dict(cards(store)), store.load()['sources']['browser'], browser_rows(store))
        c.app.state.runtime.transport = through(FakePages())
        store.db['instances'].update_one({'_id': 'browser/main'}, {'$set': {'values.url': up}})
        c.app.state.runtime.refresh()
        sweeper.run('browser', 'browser', NOW + 900)
        back = dict(cards(store))

    # then
    access = refused[0][browser.ACCESS_KEY]
    assert (access['status'], access['sev'], access['ranKey']) == ('open', 'warn', 'browser:access:browser')
    assert access['label'] == 'Website cannot read browser: the browser service refused the request'
    assert refused[0]['browser-down|127.0.0.1']['status'] == 'open'
    assert refused[1] == 'ok \u00b7 no access to browser'
    assert refused[2][0]['error'] == 'HttpError code 403' and refused[2][0]['ok'] is False
    assert back[browser.ACCESS_KEY]['status'] == 'cleared' and back['browser-down|127.0.0.1']['status'] == 'cleared'


def test_a_second_browser_instance_is_scoped(store: Store, chrome: FakeChrome):
    # given
    with app_with(store, {}, {'main': {'url': f'http://127.0.0.1:{chrome.port}'}, 'east': {'url': DOWN_URL}}) as c:
        sweeper = c.app.state.sweeper

        # when
        sweeper.run('browser', 'browser', NOW)
        sweeper.run('browser', 'browser', NOW + 300)

    # then
    east = next(a for a in store.load()['anomalies'] if a.get('cluster') == 'east')
    assert (east['cluster'], east['ranKey'], east['status']) == ('east', 'browser-east', 'open')
    assert store.load()['sources']['browser-east'] == 'ok \u00b7 browser down' and store.load()['sources']['browser'] == 'ok'


def test_a_down_browser_says_so_in_its_status_on_every_pass_and_on_its_tile(store: Store):
    # given
    with app_with(store, {}, {'main': {'url': DOWN_URL}}) as c:
        sweeper = c.app.state.sweeper

        # when
        sweeper.run('browser', 'browser', NOW)
        first = store.load()['sources']['browser']
        sweeper.run('browser', 'browser', NOW + 300)
        second = store.load()['sources']['browser']
        tile = next(t for t in c.get('/api/server', headers=AUTH).json()['plugins'] if t['id'] == 'browser')

    # then
    assert first == second == 'ok \u00b7 browser down'
    assert 'browser-down|127.0.0.1' in cards(store)
    assert tile['instances'][0]['status'] == 'ok \u00b7 browser down'


def test_admin_integrations_shows_the_browser_down_status_instead_of_healthy(store: Store):
    # given
    with app_with(store, {}, {'main': {'url': DOWN_URL}}) as c:
        c.app.state.sweeper.run('browser', 'browser', NOW)

        # when
        found = next(p for p in c.get('/api/admin/plugins', headers=AUTH).json()['plugins'] if p['id'] == 'browser')

    # then
    health = found['health']
    assert (health['state'], health['text']) == ('warn', 'browser down')
    assert (found['health']['instances'][0]['state'], found['health']['instances'][0]['text']) == ('warn', 'browser down')


def listed_instances(c, plugin: str) -> dict:
    found = {p['id']: p for p in c.get('/api/admin/plugins', headers=AUTH).json()['plugins']}
    return {i['name']: i for i in found[plugin]['instances']}


def test_the_upgrade_moves_every_old_browser_url_once_and_reuses_a_shared_address(store: Store):
    # given
    up = 'http://browser.example.com:9222'
    key = master_key(store.db, settings())
    add_instance(store, website.PLUGIN, 'main', sealer_key=key, primary=True, url=SITE, **{'browser-url': up})
    add_instance(store, website.PLUGIN, 'east', sealer_key=key, url='https://east.example.com', **{'browser-url': up})
    add_instance(store, website.PLUGIN, 'bare', sealer_key=key, url='https://bare.example.com')

    # when
    with TestClient(create_app(settings(), store, transport=through(FakePages()))) as c:
        sites = listed_instances(c, 'website')
        browsers = listed_instances(c, 'browser')
        texts = [d['text'] for d in store.activity.find()]
    with TestClient(create_app(settings(), store, transport=through(FakePages()))) as c:
        again = listed_instances(c, 'browser')
        added = c.post('/api/admin/plugins/website/instances/bare/move-browser', headers=AUTH)

    # then
    assert list(browsers) == ['main'] and browsers['main']['values']['url'] == up
    assert [(sites[n]['values'].get('browser'), sites[n]['values'].get('browser-url', '')) for n in ('main', 'east')] == [('main', ''), ('main', '')]
    assert sites['bare']['values'].get('browser', '') == ''
    assert 'Browser moved to Headless browser main for Website main' in texts and 'Browser moved to Headless browser main for Website east' in texts
    assert list(again) == ['main'] and added.status_code == 409
    assert store.counters.find_one({'_id': 'browser-move'})['done'] is True


def test_a_settings_source_instance_keeps_its_old_browser_url_and_is_still_watched(store: Store, chrome: FakeChrome):
    # given
    from skopax.contract import SourceData
    from skopax.settingsources import SourceSet
    up = url_of(chrome)
    feed = SourceData('config file', {}, ({'plugin': 'website', 'name': 'east', 'url': 'https://east.example.com', 'browser-url': up},))
    sources = SourceSet([('config file', lambda: feed)], {'config file': feed})

    # when
    with TestClient(create_app(settings(), store, transport=through(FakePages()), sources=sources)) as c:
        sites = listed_instances(c, 'website')
        browsers = listed_instances(c, 'browser')
        c.app.state.sweeper.run('browser', 'browser', NOW)

    # then
    assert sites['east']['values']['browser-url'] == up and sites['east']['values'].get('browser', '') == ''
    assert [b['values']['url'] for b in browsers.values()] == [up]
    assert browser_rows(store)[0]['ok'] is True


def test_a_website_created_without_the_browser_field_gets_the_only_browser_and_a_deliberate_blank_stays(store: Store):
    # given
    with app_with(store, {}, {'chrome': {'url': 'http://browser.example.com:9222'}}) as c:
        # when
        made = c.post('/api/admin/plugins/website/instances', json={'name': 'one', 'values': {'url': SITE}, 'enabled': False}, headers=AUTH)
        none = c.post('/api/admin/plugins/website/instances', json={'name': 'two', 'values': {'url': SITE, 'browser': ''}, 'enabled': False}, headers=AUTH)
        saved = c.patch('/api/admin/plugins/website/instances/two', json={'values': {'description': 'A shop'}}, headers=AUTH)
        cleared = c.patch('/api/admin/plugins/website/instances/one', json={'values': {'browser': ''}}, headers=AUTH)
        again = c.patch('/api/admin/plugins/website/instances/one', json={'values': {'description': 'A shop'}}, headers=AUTH)
        sites = listed_instances(c, 'website')

    # then
    assert made.status_code == 201 and none.status_code == 201 and saved.status_code == 200 and cleared.status_code == 200 and again.status_code == 200
    assert sites['one']['values'].get('browser', '') == '' and sites['two']['values'].get('browser', '') == ''


def test_a_website_saved_before_any_browser_existed_gets_the_only_browser_on_its_next_save(store: Store):
    # given
    with app_with(store, {'old': {}}, {}) as c:
        add_instance(store, browser.PLUGIN, 'chrome', sealer_key=master_key(store.db, settings()), primary=True, url='http://browser.example.com:9222')
        c.app.state.runtime.refresh()

        # when
        saved = c.patch('/api/admin/plugins/website/instances/old', json={'values': {'description': 'A shop'}}, headers=AUTH)
        sites = listed_instances(c, 'website')

    # then
    assert saved.status_code == 200 and sites['old']['values']['browser'] == 'chrome'


def test_two_browsers_leave_a_new_website_without_a_pick(store: Store):
    # given
    with app_with(store, {}, {'a': {'url': 'http://a.example.com:9222'}, 'b': {'url': 'http://b.example.com:9222'}}) as c:
        # when
        c.post('/api/admin/plugins/website/instances', json={'name': 'one', 'values': {'url': SITE}, 'enabled': False}, headers=AUTH)
        sites = listed_instances(c, 'website')

    # then
    assert sites['one']['values'].get('browser', '') == ''


def card(key: str, cluster: str | None, ran: str | None = None) -> dict:
    x = {'key': key, 'sev': 'warn', 'status': 'open', 'svc': 'x', 'label': 'Screenshot browser not answering', 'src': 'browser', 'now': 1, 'short': 'WARN ' + key,
         'firstSeen': iso(NOW - 900), 'lastSeen': iso(NOW - 300)}
    if cluster:
        x['cluster'] = cluster
    if ran:
        x['ranKey'] = ran
    return x


def test_the_upgrade_names_each_browser_after_its_website_and_tidies_what_the_old_check_left(store: Store):
    # given
    key = master_key(store.db, settings())
    add_instance(store, website.PLUGIN, 'main', sealer_key=key, primary=True, url=SITE, **{'browser-url': 'http://a.example.com:9222'})
    add_instance(store, website.PLUGIN, 'east', sealer_key=key, url='https://east.example.com', **{'browser-url': 'http://b.example.com:9222'})
    add_instance(store, website.PLUGIN, 'twin', sealer_key=key, url='https://twin.example.com', **{'browser-url': 'http://b.example.com:9222'})
    row = {'name': 'browser', 'ok': True, 'version': 'v', 'lastSeen': iso(NOW), 'error': None, 'plugin': 'website'}
    store.save({'initialized': True, 'sweptIso': iso(NOW - 300), 'muted': [], 'sources': {'browser': 'ok', 'browser-east': 'ok', 'browser-twin': 'ok'},
                'anomalies': [card('browser-down|a.example.com', None), card('browser-down|b.example.com@east', 'east', 'browser-east'), card('browser-down|b.example.com@twin', 'twin', 'browser-twin'),
                              card('browser-down|gone.example.com@gone', 'gone', 'browser-gone'),
                              card('access|-/website|browser@east', 'east', 'browser:access:browser-east'), card('access|-/website|browser@twin', 'twin', 'browser:access:browser-twin'),
                              card('access|-/website|browser', None, 'browser:access:browser')],
                'browser': [row, dict(row, cluster='east'), dict(row, cluster='twin')]}, None)

    # when
    with TestClient(create_app(settings(), store, transport=through(FakePages()))) as c:
        browsers = listed_instances(c, 'browser')
        sites = listed_instances(c, 'website')
    state = store.load()

    # then
    assert {n: b['values']['url'] for n, b in browsers.items()} == {'main': 'http://a.example.com:9222', 'east': 'http://b.example.com:9222'}
    assert browsers['main']['primary'] is True
    assert {n: sites[n]['values'].get('browser') for n in sites} == {'main': 'main', 'east': 'east', 'twin': 'east'}
    states = sorted((a['key'], a.get('cluster') or '', a['status']) for a in state['anomalies'])
    assert states == sorted([('browser-down|a.example.com', '', 'open'), ('browser-down|b.example.com@east', 'east', 'open'), ('browser-down|b.example.com@twin', 'twin', 'cleared'),
                      ('browser-down|gone.example.com@gone', 'gone', 'cleared'),
                      ('access|-/website|browser@east', 'east', 'open'), ('access|-/website|browser@twin', 'twin', 'cleared'), ('access|-/website|browser', '', 'open')])
    assert [r for r in state.get('browser') or [] if r.get('plugin') == 'website'] == []
    assert set(state['sources']) == {'browser'}


def test_one_website_that_cannot_be_changed_does_not_stop_the_others(store: Store, monkeypatch):
    # given
    from skopax.instances import Instances, SourceOwned
    key = master_key(store.db, settings())
    add_instance(store, website.PLUGIN, 'main', sealer_key=key, primary=True, url=SITE, **{'browser-url': 'http://a.example.com:9222'})
    add_instance(store, website.PLUGIN, 'east', sealer_key=key, url='https://east.example.com', **{'browser-url': 'http://b.example.com:9222'})
    add_instance(store, website.PLUGIN, 'off', sealer_key=key, enabled=False, url='https://off.example.com', **{'browser-url': 'http://c.example.com:9222'})
    real = Instances.update

    def refusing(self, plugin, name, values, by, kept=None):
        if name == 'main' and 'browser' in values:
            raise SourceOwned('browser-url', 'config file', 'Browser URL')
        return real(self, plugin, name, values, by, kept)

    monkeypatch.setattr(Instances, 'update', refusing)

    # when
    with TestClient(create_app(settings(), store, transport=through(FakePages()))) as c:
        browsers = listed_instances(c, 'browser')
        sites = listed_instances(c, 'website')

    # then
    assert sorted(b['values']['url'] for b in browsers.values()) == ['http://a.example.com:9222', 'http://b.example.com:9222']
    assert sites['main']['values']['browser-url'] == 'http://a.example.com:9222' and sites['east']['values'].get('browser') == 'east'
    assert sites['off']['values']['browser-url'] == 'http://c.example.com:9222' and sites['off']['values'].get('browser', '') == ''
    assert store.counters.find_one({'_id': 'browser-move'})['done'] is True
