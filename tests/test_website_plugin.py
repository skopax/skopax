import base64
import dataclasses
import json
from contextlib import contextmanager
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from skopax.app import create_app
from skopax.contract import InstanceInfo, Tool
from skopax.engine import iso
from skopax.instances import Instances, PluginState
from skopax.investigator import Investigator
from skopax.investigator.tools import IMAGE_MAX_BYTES, SHOT_FACTS
from skopax.kit import browser as browser_kit, web
from skopax.messages import msg
from skopax.plugins import website
from skopax.plugintools import PluginTools
from skopax.registry import BUILTIN, Registry, describe
from skopax.remote import HttpError, Unreachable
from skopax.runtime import Runtime
from skopax.sealer import Sealer, master_key
from skopax.store import Store
from tests.conftest import AUTH, anomaly, settings
from tests.fake_anthropic import FakeAnthropic, reply, text, tool
from tests.fake_cdp import JPEG, FakeChrome, Site
from tests.fake_http import FakePages
from tests.plugin_kit import KIT_KEY, add_instance
from tests.plugins import demo_plugin
from tests.plugins.base import minimal
from tests.test_investigator import KEY, NOW, seed
from tests.test_investigator_tools import sources
from tests.website_kit import CHAIRS, FIELDS, HOME, SITE, effects, page, quiet, site_box, through

HERE = Path(demo_plugin.__file__).parent
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


def ok_page():
    return page(200, b'<title>Shop</title>', content_type='text/html')


@contextmanager
def app_with(store: Store, pages: FakePages, instances: dict[str, dict]):
    key = master_key(store.db, settings())
    for i, (name, values) in enumerate(instances.items()):
        add_instance(store, website.PLUGIN, name, sealer_key=key, primary=i == 0, **({'url': SITE} | values))
    with TestClient(create_app(settings(), store, transport=through(pages))) as c:
        yield c


def browser_rows(store: Store) -> list[dict]:
    return [r for r in store.load().get('browser') or [] if r.get('plugin') == 'website']


def cards(store: Store) -> dict:
    return {a['key']: a for a in store.load()['anomalies']}


def test_the_built_in_registry_loads_the_website_plugin():
    # when
    loaded = Registry.load({}, entry_points=False)

    # then
    assert 'skopax.plugins.website' in BUILTIN and loaded.get('website').state == 'ok' and loaded.get('website').plugin is website.PLUGIN
    assert loaded.icon('website').startswith('<svg')


def test_website_identity_and_manifest():
    # when
    manifest = describe(website.PLUGIN, icon=True)

    # then
    assert json.loads(json.dumps(manifest, allow_nan=False)) == manifest
    assert (manifest['id'], manifest['name'], manifest['version'], manifest['category'], manifest['author'], manifest['licence'], manifest['core'], manifest['edition']) == (
        'website', 'Website', '0.1.0', 'web', 'Skopax', 'AGPL-3.0-only', '>=0.1,<0.7', 'community')
    assert manifest['capabilities'] == {'hosts': ['@url', '@browser-url', '*'], 'secrets': ['scan-headers'], 'agentReads': False}
    assert manifest['uses'] == ['umami', 'browser']
    assert [(c['id'], c['period'], c['offset'], c['ran']) for c in manifest['checks']] == [('pagescan', 300, 0, ['pagescan']), ('pagescan-digest', 3600, 75, ['pagescan-digest'])]
    assert [t['name'] for t in manifest['tools']] == ['page', 'screenshot', 'page_scan']
    assert [(f['key'], f['kind'], f['required']) for f in manifest['fields']] == [
        ('url', 'url', True), ('hosts', 'text', False), ('login-hosts', 'text', False), ('login-paths', 'text', False), ('api-paths', 'text', False),
        ('refused-paths', 'text', False), ('browser', 'instance', False), ('browser-url', 'url', False), ('description', 'text', False), ('umami', 'instance', False), ('scan-headers', 'secret', False),
        ('tracking-params', 'text', False), ('kept-headers', 'text', False), ('scan-skip', 'text', False), ('release-services', 'text', False), ('user-agent', 'text', False), ('internal-subdomains', 'bool', False)]
    assert next(f for f in website.PLUGIN.fields if f.key == 'url').ui is True and next(f for f in website.PLUGIN.fields if f.key == 'umami').choices == ('umami',)
    assert website.PLUGIN.client is not None and website.PLUGIN.test is not None and website.PLUGIN.probe_targets is not None and website.PLUGIN.links is not None
    assert len((Path(website.__file__).parent / 'website.md').read_text(encoding='utf-8').splitlines()) <= 40


def test_website_defaults_are_neutral():
    # then
    assert {f.key: f.default for f in website.PLUGIN.fields} == {'url': '', 'hosts': '', 'login-hosts': '', 'login-paths': '', 'api-paths': '', 'refused-paths': '',
                                                                  'browser': '', 'browser-url': '', 'description': '', 'umami': '', 'scan-headers': '', 'tracking-params': '', 'kept-headers': '', 'scan-skip': '', 'release-services': '',
                                                                  'user-agent': '', 'internal-subdomains': 'false'}
    assert {s.key: s.default for s in website.PLUGIN.settings} == {'page-scan': 'false', 'scan-hours': '1', 'recheck-days': '7', 'pages': '50', 'extra': '', 'ignore': '',
                                                                  'model': 'claude-opus-5-5', 'release-delay': '10', 'digest-hour': '10', 'scan-prompt': ''}
    assert website.PLUGIN.rules == ()


def test_website_connection_test_reports_ok_and_failures(store: Store, chrome: FakeChrome):
    # given
    answers = {'ok': ok_page(), 'moved': page(301, b'', location='/en/'), 'rejected': page(401, b''), 'refused': page(403, b'<title>Forbidden</title>'),
               'challenged': page(403, b'', cf_mitigated='challenge'), 'gone': page(404, b''), 'down': Unreachable('https://shop.example.com/ unreachable (ConnectionRefusedError)')}

    def tried(name: str, values: dict) -> dict:
        with TestClient(create_app(settings(), store, transport=through(FakePages({HOME: answers[name]})))) as c:
            res = c.post('/api/admin/plugins/website/test', json={'values': {'url': SITE} | values}, headers=AUTH)
        assert res.status_code == 200
        return res.json()

    # when
    ok = tried('ok', {})
    moved = tried('moved', {})
    with_browser = tried('ok', {'browser-url': f'http://127.0.0.1:{chrome.port}'})
    browser_down = tried('ok', {'browser-url': DOWN_URL})
    rejected = tried('rejected', {})
    refused = tried('refused', {})
    challenged = tried('challenged', {})
    gone = tried('gone', {})
    down = tried('down', {})
    plain = tried('ok', {'url': 'http://shop.example.com'})

    # then
    assert (ok['ok'], ok['summary']) == (True, 'shop.example.com answered 200')
    assert (moved['ok'], moved['summary']) == (True, 'shop.example.com answered 301')
    assert (with_browser['ok'], with_browser['summary']) == (True, 'shop.example.com answered 200 \u00b7 browser Chrome/157.0.8103.15')
    assert (browser_down['ok'], browser_down['summary']) == (False, f'{browser_kit.DOWN} (Unreachable)')
    assert (rejected['ok'], rejected['summary']) == (False, 'shop.example.com answered 401: the site refused the page check (a firewall or bot rule may block it)')
    assert (refused['ok'], refused['summary']) == (False, 'shop.example.com answered 403: the site refused the page check (a firewall or bot rule may block it)')
    assert (challenged['ok'], challenged['summary']) == (False, f'{web.CHALLENGE} (HTTP 403)')
    assert (gone['ok'], gone['summary']) == (False, msg('connection.status', status=404))
    assert (down['ok'], down['summary']) == (False, msg('connection.unreachable'))
    assert (plain['ok'], plain['summary']) == (False, 'http://shop.example.com: https only')


def test_a_refused_browser_read_names_its_status_in_the_connection_test(store: Store):
    # given
    def refusing(method, url, headers, data, timeout):
        if url.endswith('/json/version'):
            raise HttpError({'rejected': 401, 'refused': 403, 'broken': 500}[calls[0]], url)
        return ok_page()

    calls: list[str] = []

    def tried(name: str) -> dict:
        calls[:] = [name]
        with TestClient(create_app(settings(), store, transport=refusing)) as c:
            return c.post('/api/admin/plugins/website/test', json={'values': {'url': SITE, 'browser-url': 'http://127.0.0.1:9222'}}, headers=AUTH).json()

    # when
    rejected = tried('rejected')
    broken = tried('broken')
    refused = tried('refused')

    # then
    assert (rejected['ok'], rejected['summary']) == (False, msg('connection.rejected', status=401))
    assert (refused['ok'], refused['summary']) == (False, f'{browser_kit.REFUSED} (HTTP 403)')
    assert (broken['ok'], broken['summary']) == (False, f'{browser_kit.DOWN} (HTTP 500)')


def test_screenshots_need_a_browser_url(store: Store):
    # given
    b = site_box(store, through(FakePages()))
    runtime = b.sources.plugin_tools.runtime

    # when
    out, err = b.call('screenshot', {'url': CHAIRS})

    # then
    assert runtime.client('website') is None
    assert err is True and out == 'no tool named screenshot' and b.shots == [], 'without a browser-url the tool is not offered'


def test_the_screenshot_and_page_scan_tools_are_offered_only_from_an_instance_with_a_browser_url(store: Store):
    # given
    from skopax.investigator.toolset import ToolSet
    bare = site_box(store, through(FakePages())).sources.plugin_tools.runtime
    held = {name: ToolSet(bare).status(name, None) for name in ('page', 'screenshot', 'page_scan')}

    # when
    offered = [d['name'] for d in PluginTools(bare).definitions()]

    # then
    assert {t.name: t.needs_field for t in website.PLUGIN.tools} == {'page': '', 'screenshot': 'browser', 'page_scan': 'browser'}
    off = {'state': 'off', 'reason': msg('tools.off.needs_field', field='Headless browser')}
    assert held == {'page': {'state': 'on', 'reason': None}, 'screenshot': off, 'page_scan': off}
    assert offered == ['page']


def test_the_client_is_one_browser_per_browser_url(store: Store, chrome: FakeChrome):
    # given
    url = f'http://127.0.0.1:{chrome.port}'
    runtime = site_box(store, through(FakePages()), {'main': {'browser-url': url}, 'east': {'browser-url': url, 'url': 'https://east.example.com'}}).sources.plugin_tools.runtime

    # when
    main, east = runtime.client('website', 'main'), runtime.client('website', 'east')

    # then
    assert isinstance(main, browser_kit.Browser) and main is east and main.url == url


def test_login_and_api_paths_come_from_the_fields(store: Store, chrome: FakeChrome):
    # given
    pages = FakePages({'https://shop.example.com/members/home': ok_page(), 'https://shop.example.com/graph/q': ok_page()})
    url = f'http://127.0.0.1:{chrome.port}'
    b = site_box(store, through(pages), {'main': {'browser-url': url, 'login-paths': '/members', 'api-paths': 'graph'}, 'east': {'browser-url': url}})

    # when
    members = b.call('page', {'url': 'https://shop.example.com/members/home'})
    graph = b.call('page', {'url': 'https://shop.example.com/graph/q'})
    shot = b.call('screenshot', {'url': 'https://shop.example.com/members/home'})
    east_members = b.call('page', {'url': 'https://shop.example.com/members/home', 'instance': 'east'})
    east_graph = b.call('page', {'url': 'https://shop.example.com/graph/q', 'instance': 'east'})
    east_login = b.call('page', {'url': 'https://shop.example.com/login', 'instance': 'east'})

    # then
    assert members[1] is True and 'login pages are refused (/members)' in members[0]
    assert graph[1] is True and 'API paths are refused (/api/, /graph/)' in graph[0]
    assert shot[1] is True and 'login pages are refused (/members)' in shot[0] and 'Page.navigate' not in chrome.methods()
    assert east_members[1] is False and east_graph[1] is False
    assert east_login[1] is True and 'login pages are refused' in east_login[0]
    assert pages.urls() == ['https://shop.example.com/members/home', 'https://shop.example.com/graph/q']


def test_the_site_is_a_probe_target():
    # given
    info = InstanceInfo('east', False, 'east', {'url': 'https://east.example.com/', 'browser-url': DOWN_URL})

    # then
    assert website.PLUGIN.probe_targets(info) == [{'name': 'website-east', 'url': 'https://east.example.com/'}]


def test_a_browser_url_that_is_not_plain_http_fails_the_connection_test(store: Store):
    # when
    with TestClient(create_app(settings(), store, transport=through(FakePages({HOME: ok_page()})))) as c:
        res = c.post('/api/admin/plugins/website/test', json={'values': {'url': SITE, 'browser-url': 'https://browser.example.com'}}, headers=AUTH).json()

    # then
    assert (res['ok'], res['summary']) == (False, browser_kit.NOT_HTTP)


def shot_tool(images: list[dict]):
    def run(ctx, args):
        return images.pop(0)

    return Tool('big_shot', 'Returns a picture.', {'type': 'object', 'properties': {}, 'required': [], 'additionalProperties': False}, run, scan=True)


def picture_box(store: Store, images: list[dict]):
    plugin = minimal('pictures', 'Pictures', checks=(), tools=(shot_tool(images),))
    registry = Registry.of(plugin, directory=HERE)
    runtime = Runtime(registry, PluginState(store.db), Instances(store.db, registry, lambda: Sealer(KIT_KEY)))
    runtime.refresh()
    return site_box(store, None, runtime=runtime, run_id='run-1')


def image(data: bytes, **facts) -> dict:
    return {'image': {'media_type': 'image/png', 'data': base64.b64encode(data).decode()}, 'facts': facts, 'text': 'a note from the plug-in'}


def test_a_top_level_image_that_is_no_picture_is_rendered_as_text(store: Store):
    # given
    b = picture_box(store, [{'app': 'checkout-service', 'image': 'registry.example.com/shop/checkout-service:1.2'}])

    # when
    out, err = b.call('big_shot', {})

    # then
    assert err is False and json.loads(out) == {'app': 'checkout-service', 'image': 'registry.example.com/shop/checkout-service:1.2'}
    assert store.screenshots.count_documents({}) == 0 and b.shots == []


def test_core_keeps_only_the_known_facts_of_a_tool_image(store: Store):
    # given
    facts = {'url': CHAIRS, 'at': '2026-10-17T08:00:00Z', 'viewport': 'phone', 'width': 390, 'height': 700, 'title': 'T' * 500, 'marked': False,
             'overflow': [{'verdict': 'scrolls', 'revealed': ['Price', 7], 'offscreen': ['Fees', None]}, {'verdict': 'sideways', 'revealed': []}, 'x'], 'blocked': {'otherHosts': 2, 'api': 'many'},
             'blockedHosts': ['tags.example.org'], 'junk': {'a': 1}, 'status': True}
    b = picture_box(store, [image(b'\x89PNG' + b'p' * 100, **facts)])

    # when
    content, err = b.call('big_shot', {})

    # then
    shown = json.loads(content[0]['text'])
    kept = store.screenshot(shown['id'])
    assert err is False and content[1]['source']['media_type'] == 'image/png'
    assert shown['marked'] is False and shown['overflow'] == [{'verdict': 'scrolls', 'revealed': ['Price'], 'offscreen': ['Fees']}] and shown['blocked'] == {'otherHosts': 2}
    assert shown['title'] == 'T' * 200 and shown['text'] == 'a note from the plug-in' and shown['blockedHosts'] == ['tags.example.org']
    assert 'junk' not in shown and 'status' not in shown
    assert kept['marked'] is False and kept['overflow'] == [{'verdict': 'scrolls', 'revealed': ['Price'], 'offscreen': ['Fees']}] and kept['mime'] == 'image/png'
    assert 'junk' not in kept and 'blockedHosts' not in kept and 'text' not in kept
    assert set(SHOT_FACTS) >= {'marked', 'overflow', 'viewport', 'width', 'height', 'cut', 'title', 'finalUrl', 'url', 'at', 'status', 'blocked'}


@pytest.mark.parametrize('bad, said', [
    ({'image': {'media_type': 'image/gif', 'data': base64.b64encode(b'x').decode()}, 'facts': {}}, 'big_shot returned an image that is not a JPEG or PNG'),
    ({'image': {'media_type': 'image/png', 'data': 'not base64 !!'}, 'facts': {}}, 'big_shot returned an image that is not valid base64'),
    ({'image': {'media_type': 'image/png'}, 'facts': {}}, 'big_shot returned an image that is not valid base64'),
    (image(b'x' * (IMAGE_MAX_BYTES + 1)), f'the image is {(IMAGE_MAX_BYTES + 1) // 1024} KB, over the {IMAGE_MAX_BYTES // 1024} KB limit'),
])
def test_an_image_core_cannot_take_is_a_tool_error_and_nothing_is_stored(store: Store, bad, said):
    # given
    b = picture_box(store, [bad])

    # when
    out, err = b.call('big_shot', {})

    # then
    assert err is True and out == said
    assert store.screenshots.count_documents({}) == 0 and b.shots == []


def test_a_tool_image_is_stored_and_shown_to_the_model(store: Store, chrome: FakeChrome):
    # given
    seed(store, anomaly(KEY, firstSeen=iso(NOW - 600)))
    runtime = site_box(store, through(FakePages()), {'main': {'browser-url': f'http://127.0.0.1:{chrome.port}'}}).sources.plugin_tools.runtime
    s = dataclasses.replace(settings(), anthropic_api_key='sk-test', investigate_mode='live')
    inv = Investigator(store, sources(plugin_tools=PluginTools(runtime)), effects(), s,
                       client=FakeAnthropic([reply(tool('screenshot', {'url': CHAIRS})), reply(text('The page shows the chairs.'), stop='end_turn')]), clock=lambda: NOW)

    # when
    record = inv.run_once(NOW)
    with TestClient(create_app(settings(), store)) as c:
        shots = store.investigation(record['id'])['screenshots']
        served = c.get(f"/api/screenshots/{shots[0]['id']}")

    # then
    blocks = inv.client.requests[1]['messages'][-1]['content'][0]['content']
    assert [b['type'] for b in blocks] == ['text', 'image'] and blocks[1]['source']['media_type'] == 'image/jpeg'
    assert json.loads(blocks[0]['text'])['id'] == shots[0]['id'] and [x['url'] for x in shots] == [CHAIRS]
    assert served.status_code == 200 and served.content == JPEG and served.headers['content-type'] == 'image/jpeg'
    ended = [a for a in store.list_activity(None, 20) if a['kind'] == 'run' and a['text'].startswith('run done')]
    assert ended[0]['screenshots'] == shots


def test_the_connection_test_sends_the_scan_headers_to_the_site_and_fails_on_a_bare_public_suffix(store: Store):
    # given
    pages = FakePages({HOME: ok_page()})

    # when
    with TestClient(create_app(settings(), store, transport=through(pages))) as c:
        passed = c.post('/api/admin/plugins/website/test', json={'values': {'url': SITE, 'scan-headers': 'X-Shop-Check: s3cret'}}, headers=AUTH).json()
        suffix = c.post('/api/admin/plugins/website/test', json={'values': {'url': SITE, 'hosts': 'shop.example.net, co.uk'}}, headers=AUTH).json()

    # then
    assert passed['ok'] is True and pages.calls[0][1]['X-Shop-Check'] == 's3cret'
    assert suffix['ok'] is False and 'co.uk' in suffix['summary'] and 'public suffix' in suffix['summary']
    assert len(pages.calls) == 1


def test_the_browser_list_keeps_a_bounded_number_of_browsers(monkeypatch):
    # given
    website.BROWSERS.clear()

    # when
    for n in range(website.BROWSERS_MAX + 5):
        website.client(InstanceInfo('main', True, None, {'browser-url': f'http://browser-{n}:9222'}), None)
    kept = list(website.BROWSERS)

    # then
    assert len(kept) == website.BROWSERS_MAX and kept[-1] == (f'http://browser-{website.BROWSERS_MAX + 4}:9222', None)
    assert website.client(InstanceInfo('main', True, None, {'browser-url': 'http://browser-20:9222'}), None) is website.BROWSERS[('http://browser-20:9222', None)]


def test_subdomains_may_be_internal_is_an_off_by_default_switch_and_the_hosts_rule_is_declared():
    # when
    field = next(f for f in website.PLUGIN.fields if f.key == 'internal-subdomains')

    # then
    assert (field.kind, field.default, field.label) == ('bool', 'false', 'Subdomains may be internal')
    assert 'public' in field.help and '*' in website.PLUGIN.capabilities.hosts
    assert 'only to the site URL host and the more hosts' in next(f for f in website.PLUGIN.fields if f.key == 'scan-headers').help


def test_the_connection_test_names_the_scan_headers_it_parsed_never_their_values_and_the_help_says_semicolons(store: Store):
    # given
    pages = FakePages({HOME: ok_page()})

    # when
    with TestClient(create_app(settings(), store, transport=through(pages))) as c:
        two = c.post('/api/admin/plugins/website/test', json={'values': {'url': SITE, 'scan-headers': 'X-Bypass: s3cret; X-Other: two'}}, headers=AUTH).json()
        fused = c.post('/api/admin/plugins/website/test', json={'values': {'url': SITE, 'scan-headers': 'X-Bypass: s3cret X-Other: two'}}, headers=AUTH).json()

    # then
    assert two['ok'] is True and 'scan headers X-Bypass, X-Other' in two['summary'] and 's3cret' not in json.dumps(two)
    assert fused['ok'] is False and 'another "Name:"' in fused['summary'] and 's3cret' not in json.dumps(fused)
    help_text = next(f for f in website.PLUGIN.fields if f.key == 'scan-headers').help
    assert 'separated by semicolons' in help_text and 'new line' not in help_text
