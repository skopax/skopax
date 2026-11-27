import json
from types import SimpleNamespace
from urllib.parse import parse_qs

import mongomock
import pytest

from skopax.contract import ReadError
from skopax.instances import Instances, PluginState
from skopax.messages import msg
from skopax.plugins import sharepoint
from skopax.plugins.sharepoint import client as graph_client
from skopax.plugins.sharepoint.client import Graph
from skopax.plugintools import PluginTools
from skopax.registry import BUILTIN, Registry, describe
from skopax.remote import HttpError, Reply, Unreachable
from skopax.runtime import Runtime
from skopax.sealer import Sealer
from tests.plugin_kit import KIT_KEY, add_instance
from tests.sharepoint_kit import (by_path, plain_zip, understated, CLIENT, DRIVE, FOLDER_ID, GRAPH, HOST, LOGIN, SECRET, SITE, SITE_ID, TENANT, VALUES, FakeGraph, base_routes, docx, file_json, folder_json,
                                  token_reply)

NOW = 1_792_224_000.0
USER = 'alice@example.com'
DOWNLOAD = f'https://{HOST}/sites/ops/_layouts/15/download.aspx'


class Clock:
    def __init__(self, now: float = 1000.0):
        self.now = now

    def __call__(self) -> float:
        return self.now


def graph(http: FakeGraph, clock: Clock | None = None, **kw) -> Graph:
    config = {'tenant': TENANT, 'client_id': CLIENT, 'secret': SECRET, 'site': SITE, 'library': '', 'folder': 'Runbooks/Prod'} | kw
    return Graph(transport=http, clock=clock or Clock(), **config)


def tool():
    return next(t for t in sharepoint.PLUGIN.tools if t.name == 'sharepoint_docs')


def run_tool(g: Graph, **args):
    return tool().run(SimpleNamespace(client=lambda: g, now=NOW, instance=None), args)


def refused(g: Graph, **args) -> ReadError:
    with pytest.raises(ReadError) as e:
        run_tool(g, **args)
    return e.value


def kit_store() -> SimpleNamespace:
    return SimpleNamespace(db=mongomock.MongoClient(tz_aware=True)['kit'])


def sp_runtime(store, transport=None, named: str | None = 'main', **values) -> Runtime:
    registry = Registry.of(sharepoint.PLUGIN)
    runtime = Runtime(registry, PluginState(store.db), Instances(store.db, registry, lambda: Sealer(KIT_KEY)), transport)
    if named:
        add_instance(store, sharepoint.PLUGIN, named, sealer_key=KIT_KEY, primary=True, **(VALUES | values))
    runtime.refresh()
    return runtime


def download(item: str, body: bytes) -> dict:
    return {('GET', f'{GRAPH}/drives/{DRIVE}/items/{item}/content'): Reply(b'', {'location': f'{DOWNLOAD}?item={item}'}, 302),
            ('GET', DOWNLOAD): lambda q, h, d: Reply(body if q.get('item') == [item] else b'', {}, 200)}


def test_sharepoint_identity_manifest_and_setup():
    # given
    registry = Registry.of(sharepoint.PLUGIN)

    # when
    loaded = registry.get('sharepoint')
    manifest = describe(sharepoint.PLUGIN, icon=True)
    setup = sharepoint.PLUGIN.setup

    # then
    assert json.loads(json.dumps(manifest)) == manifest
    assert (loaded.state, loaded.reason) == ('ok', None) and 'skopax.plugins.sharepoint' in BUILTIN
    assert Registry.load({}, entry_points=False).get('sharepoint').state == 'ok'
    assert (manifest['name'], manifest['category'], manifest['edition']) == ('SharePoint and OneDrive', 'other', 'community')
    assert manifest['maxInstances'] is None or manifest['maxInstances'] >= 5
    assert manifest['capabilities'] == {'hosts': ['@site', 'login.microsoftonline.com', 'graph.microsoft.com'], 'secrets': ['client-secret'], 'agentReads': False}
    assert [(f['key'], f['label'], f['kind'], f['required']) for f in manifest['fields']] == [
        ('tenant-id', 'Tenant ID', 'text', True), ('client-id', 'Client ID', 'text', True), ('client-secret', 'Client secret', 'secret', True),
        ('site', 'Site URL', 'url', True), ('library', 'Library', 'text', False), ('folder', 'Folder', 'text', True),
        ('index-pages', 'Index pages for search by meaning', 'bool', False)]
    assert {t.name: (t.kind, t.per_run) for t in sharepoint.PLUGIN.tools} == {'sharepoint_docs': ('read', 8)}
    assert [(k.id, k.inline) for k in sharepoint.PLUGIN.knowledge] == [('sharepoint', True)] and len(sharepoint.PLUGIN.knowledge[0].text) <= 2000
    assert setup is not None and len(setup.steps) >= 5 and setup.docs.startswith('https://learn.microsoft.com/')
    assert any('Sites.Selected' in s for s in setup.steps) and any('Files.Read.All' in s and 'Sites.Read.All' in s for s in setup.steps)
    assert '<g ' in (sharepoint.HOME / sharepoint.PLUGIN.icon).read_text(encoding='utf-8')


def test_fields_are_checked_with_clear_messages():
    # given
    checks = {f.key: f.check for f in sharepoint.PLUGIN.fields}

    # when
    good = [checks['tenant-id'](TENANT), checks['client-id'](CLIENT), checks['site'](SITE), checks['site']('https://contoso-my.sharepoint.com/personal/jane_contoso_com'),
            checks['folder']('Runbooks/Prod'), checks['folder']('Team notes')]
    bad_ids = [checks['tenant-id'](x) for x in ('contoso.onmicrosoft.com', '1234', TENANT + 'x')]
    bad_sites = [checks['site'](x) for x in ('http://contoso.sharepoint.com/sites/ops', 'https://contoso.example.com/sites/ops', 'https://contoso.sharepoint.com',
                                             'https://contoso.sharepoint.com/sites/ops?x=1', 'https://evil.com/.sharepoint.com/sites/ops')]
    bad_folders = [checks['folder'](x) for x in ('Runbooks/../Secrets', 'Runbooks\\Prod', 'a:b')]

    # then
    assert good == [None] * 6
    assert bad_ids == [msg('sharepoint.invalid.guid', field='Tenant ID')] * 3
    assert bad_sites == [msg('sharepoint.invalid.site')] * 5
    assert bad_folders == [msg('sharepoint.invalid.folder')] * 3


def test_the_token_is_fetched_with_client_credentials_and_cached_until_shortly_before_expiry():
    # given
    clock = Clock()
    issued = iter(['tok-1', 'tok-2'])
    http = FakeGraph(base_routes() | {('POST', LOGIN): lambda q, h, d: token_reply(next(issued))})
    g = graph(http, clock)

    # when
    run_tool(g, op='search', text='cart')
    run_tool(g, op='search', text='checkout')
    before = len(http.calls)
    clock.now += 3599 - graph_client.TOKEN_EARLY_S + 1
    run_tool(g, op='search', text='cart')

    # then
    posts = http.hits(LOGIN)
    assert len(posts) == 2
    method, _, headers, data = posts[0]
    form = parse_qs(data.decode())
    assert headers['Content-Type'] == 'application/x-www-form-urlencoded'
    assert form == {'client_id': [CLIENT], 'client_secret': [SECRET], 'scope': ['https://graph.microsoft.com/.default'], 'grant_type': ['client_credentials']}
    sent = [c[2]['Authorization'] for c in http.calls if c[1].startswith(GRAPH)]
    later = [c[2]['Authorization'] for c in http.calls[before:] if c[1].startswith(GRAPH)]
    assert set(sent[:len(sent) - len(later)]) == {'Bearer tok-1'} and later and set(later) == {'Bearer tok-2'}


def test_the_site_library_and_folder_are_resolved_by_name():
    # given
    http = FakeGraph(base_routes() | {
        ('GET', f'{GRAPH}/sites/{SITE_ID}/drives'): {'value': [{'id': 'b!wiki', 'name': 'Wiki'}, {'id': DRIVE, 'name': 'Team Docs'}]},
        ('GET', f'{GRAPH}/drives/{DRIVE}/root:/Runbooks/Prod'): folder_json('Prod', FOLDER_ID, '/Runbooks', 3),
    })
    named = graph(http, library='team docs')
    missing = graph(http, library='Nope')

    # when
    place = named.place()
    gone = refused(missing, op='search', text='cart')

    # then
    assert (place.site, place.drive, place.folder_id, place.folder) == (SITE_ID, DRIVE, FOLDER_ID, 'Runbooks/Prod')
    assert http.hits(f'{GRAPH}/drives/{DRIVE}/root:/Runbooks/Prod') and not http.hits(f'{GRAPH}/sites/{SITE_ID}/drive/root:/Runbooks/Prod')
    assert (gone.status, gone.message) == (404, msg('sharepoint.no_library', library='Nope'))


def test_search_walks_only_the_folder_and_drops_results_outside_it():
    # given
    http = FakeGraph(base_routes())
    g = graph(http)

    # when
    found = run_tool(g, op='search', text='checkout')
    every = run_tool(g, op='search', text='md docx png')
    none = run_tool(g, op='search', text='kafka')

    # then
    assert found['hits'] == [{'path': 'Checkout/checkout-service.md', 'id': 'F4', 'url': f'https://{HOST}/sites/ops/Shared%20Documents/Runbooks/Prod/Checkout/checkout-service.md',
                              'readable': True}]
    assert sorted(h['path'] for h in every['hits']) == ['Checkout/checkout-service.md', 'Payments outage.docx', 'cart-restart.md', 'diagram.png']
    assert {h['path']: h['readable'] for h in every['hits']}['diagram.png'] is False
    assert none['hits'] == [] and none['note'] == msg('sharepoint.nothing_found', folder='Runbooks/Prod')
    assert not [u for u in http.graph_calls() if 'search' in u]
    listed = [u.split('?')[0] for u in http.graph_calls() if u.split('?')[0].endswith('/children')]
    assert set(listed) == {f'{GRAPH}/drives/{DRIVE}/items/{FOLDER_ID}/children', f'{GRAPH}/drives/{DRIVE}/items/FOLDER2/children'}


def test_search_follows_next_links_only_on_graph_and_stops_at_its_caps(monkeypatch):
    # given
    monkeypatch.setattr(graph_client, 'WALK_ITEMS', 3)
    page2 = f'{GRAPH}/drives/{DRIVE}/items/{FOLDER_ID}/children/page2'
    routes = base_routes() | {
        ('GET', f'{GRAPH}/drives/{DRIVE}/items/{FOLDER_ID}/children'): {'value': [file_json('a.md', 'A', '/Runbooks/Prod')], '@odata.nextLink': page2},
        ('GET', page2): {'value': [file_json('b.md', 'B', '/Runbooks/Prod'), file_json('c.md', 'C', '/Runbooks/Prod'), file_json('d.md', 'D', '/Runbooks/Prod')],
                         '@odata.nextLink': 'https://evil.example.com/next'},
    }
    http = FakeGraph(routes)

    # when
    out = run_tool(graph(http), op='search', text='md')

    # then
    assert len(out['hits']) == 3 and out['partial'] is True
    assert not [c for c in http.calls if 'evil.example.com' in c[1]]


def test_read_returns_masked_text_and_word_paragraphs_without_sending_the_token_to_the_download():
    # given
    routes = base_routes() | {
        ('GET', by_path('cart-restart.md')): file_json('cart-restart.md', 'F1', '/Runbooks/Prod'),
        ('GET', f'{GRAPH}/drives/{DRIVE}/items/F2'): file_json('Payments outage.docx', 'F2', '/Runbooks/Prod', size=5000),
    }
    http = FakeGraph(routes | download('F1', b'# Cart\npassword=hunter2hunter2\nRestart it.'))
    g = graph(http)
    word = FakeGraph(routes | download('F2', docx(['Payments outage', 'Call alice@example.com, then fail over.'])))

    # when
    text = run_tool(g, op='read', path='cart-restart.md')
    doc = run_tool(graph(word), op='read', id='F2')

    # then
    assert text['path'] == 'cart-restart.md' and text['url'].endswith('/Runbooks/Prod/cart-restart.md')
    assert 'Restart it.' in text['text'] and 'hunter2hunter2' not in text['text']
    assert doc['path'] == 'Payments outage.docx' and doc['text'].startswith('Payments outage\n') and 'fail over.' in doc['text'] and 'alice@example.com' not in doc['text']
    fetched = [c for c in http.calls if c[1].startswith(DOWNLOAD)]
    assert fetched and all('Authorization' not in c[2] for c in fetched)


def test_reads_outside_the_folder_or_of_other_kinds_are_refused():
    # given
    big = graph_client.TEXT_BYTES + 1
    routes = base_routes() | {
        ('GET', f'{GRAPH}/drives/{DRIVE}/items/F5'): file_json('stray.md', 'F5', '/Runbooks/Prod-old'),
        ('GET', f'{GRAPH}/drives/{DRIVE}/items/F6'): file_json('remote.md', 'F6', '/Runbooks/Prod', drive='b!other'),
        ('GET', f'{GRAPH}/drives/{DRIVE}/items/F7'): file_json('shortcut.md', 'F7', '/Runbooks/Prod', remoteItem={'id': 'x', 'parentReference': {'driveId': 'b!other'}}),
        ('GET', f'{GRAPH}/drives/{DRIVE}/items/F3'): file_json('diagram.png', 'F3', '/Runbooks/Prod'),
        ('GET', f'{GRAPH}/drives/{DRIVE}/items/F8'): file_json('huge.md', 'F8', '/Runbooks/Prod', size=big),
        ('GET', f'{GRAPH}/drives/{DRIVE}/items/FOLDER2'): folder_json('Checkout', 'FOLDER2', '/Runbooks/Prod'),
    }
    http = FakeGraph(routes)
    g = graph(http)

    # when
    walked = [refused(g, op='read', path=p) for p in ('../Secrets/a.md', 'Checkout/..\\x.md', 'a:b.md')]
    outside = [refused(g, op='read', id=i) for i in ('F5', 'F6', 'F7')]
    kind, large, folder = refused(g, op='read', id='F3'), refused(g, op='read', id='F8'), refused(g, op='read', id='FOLDER2')
    neither = refused(g, op='read')

    # then
    assert all(e.status == 403 for e in walked + outside)
    assert outside[0].message == msg('sharepoint.outside', path='stray.md', folder='Runbooks/Prod')
    assert walked[0].message == msg('sharepoint.outside', path='../Secrets/a.md', folder='Runbooks/Prod')
    assert (kind.status, kind.message) == (415, msg('sharepoint.not_text', name='diagram.png'))
    assert (large.status, large.message) == (413, msg('sharepoint.too_large', name='huge.md', limit='1 MB'))
    assert (folder.status, folder.message) == (400, msg('sharepoint.not_a_file', path='Checkout'))
    assert neither.status == 400
    assert not [c for c in http.calls if '/content' in c[1] or c[1].startswith(DOWNLOAD)]


def test_word_files_with_macros_entities_or_a_huge_body_are_refused(monkeypatch):
    # given
    monkeypatch.setattr(graph_client, 'DOCX_XML_BYTES', 4000)
    routes = base_routes() | {('GET', f'{GRAPH}/drives/{DRIVE}/items/F2'): file_json('Payments outage.docx', 'F2', '/Runbooks/Prod', size=5000)}
    cases = {
        'macro': docx(['x'], extra={'word/vbaProject.bin': b'\x00' * 10}),
        'entity': docx([], xml=b'<?xml version="1.0"?><!DOCTYPE d [<!ENTITY a "aaaa">]><d>&a;</d>'),
        'utf16': docx([], xml='<?xml version="1.0" encoding="UTF-16"?><!DOCTYPE d [<!ENTITY a "aaaa">]><d>&a;</d>'.encode('utf-16')),
        'multibyte': docx([], xml=b'<?xml version="1.0" encoding="Shift_JIS"?><d>x</d>'),
        'bomb': docx(['a' * 10000]),
        'understated': understated(docx(['b' * 20000]), 'word/document.xml', 100),
        'broken': b'not a zip file',
        'no body': plain_zip({'word/other.xml': b'<x/>'}),
        'bad xml': docx([], xml=b'<w:document'),
    }

    # when
    out = {name: refused(graph(FakeGraph(routes | download('F2', body))), op='read', id='F2') for name, body in cases.items()}

    # then
    why = {'macro': 'it holds macros', 'entity': 'it declares entities', 'utf16': 'it declares entities', 'multibyte': 'its text cannot be parsed', 'bomb': 'its text is too large', 'understated': 'not a Word document',
           'broken': 'not a Word document', 'no body': 'not a Word document', 'bad xml': 'its text cannot be parsed'}
    assert {name: e.message for name, e in out.items()} == {name: msg('sharepoint.bad_docx', name='Payments outage.docx', why=w) for name, w in why.items()}
    assert {name: e.status for name, e in out.items()} == {name: 413 if name == 'bomb' else 415 for name in cases}


def test_a_long_document_is_cut():
    # given
    routes = base_routes() | {('GET', f'{GRAPH}/drives/{DRIVE}/items/F1'): file_json('cart-restart.md', 'F1', '/Runbooks/Prod')}
    http = FakeGraph(routes | download('F1', ('word ' * 10000).encode()))

    # when
    out = run_tool(graph(http), op='read', id='F1')

    # then
    assert out['cut'] is True and len(out['text']) == graph_client.TEXT_LIMIT + 1


def test_off_and_bad_input_are_refused():
    # given
    off = graph(FakeGraph(base_routes()), secret='')

    # when
    down = refused(off, op='search', text='cart')
    bad_op = refused(graph(FakeGraph(base_routes())), op='list')
    empty = refused(graph(FakeGraph(base_routes())), op='search', text='  ')

    # then
    assert (down.status, down.message) == (503, msg('integrations.off', name='SharePoint and OneDrive'))
    assert bad_op.status == 400 and empty.status == 400


def check(http: FakeGraph, **values):
    rt = sp_runtime(kit_store(), http, **values)
    return sharepoint.test(rt.context(sharepoint.PLUGIN, rt.instances.get('sharepoint', 'main'), NOW))


def test_the_connection_test_names_the_part_and_the_permission():
    # given
    good = FakeGraph(base_routes())
    secret = FakeGraph(base_routes() | {('POST', LOGIN): HttpError(401, LOGIN, json.dumps({'error': 'invalid_client', 'error_description': 'AADSTS7000215 secret'}))})
    site403 = FakeGraph(base_routes() | {('GET', f'{GRAPH}/sites/{HOST}:/sites/ops'): HttpError(403, 'x')})
    site404 = FakeGraph(base_routes() | {('GET', f'{GRAPH}/sites/{HOST}:/sites/ops'): HttpError(404, 'x')})
    folder404 = FakeGraph(base_routes() | {('GET', f'{GRAPH}/sites/{SITE_ID}/drive/root:/Runbooks/Prod'): HttpError(404, 'x')})
    children403 = FakeGraph(base_routes() | {('GET', f'{GRAPH}/drives/{DRIVE}/items/{FOLDER_ID}/children'): HttpError(403, 'x')})
    is_file = FakeGraph(base_routes() | {('GET', f'{GRAPH}/sites/{SITE_ID}/drive/root:/Runbooks/Prod'): file_json('Prod', 'X', '/Runbooks')})
    down = FakeGraph({('POST', LOGIN): Unreachable('x')})

    # when
    ok, bad, denied, gone, nofolder, nolist, notfolder, unreachable = (check(good), check(secret), check(site403), check(site404), check(folder404), check(children403),
                                                                      check(is_file), check(down))
    wrong = check(good, site='https://contoso.example.com/sites/ops')

    # then
    assert (ok.ok, ok.summary) == (True, msg('sharepoint.test.ok', site=SITE, library='the default library', folder='Runbooks/Prod', count='4 items'))
    assert (bad.ok, bad.summary) == (False, msg('sharepoint.token_refused', status=401, error='invalid_client'))
    assert (denied.ok, denied.summary) == (False, msg('sharepoint.denied', part='the site', status=403))
    assert (gone.ok, gone.summary) == (False, msg('sharepoint.no_site', site=SITE))
    assert (nofolder.ok, nofolder.summary) == (False, msg('sharepoint.no_folder', folder='Runbooks/Prod'))
    assert (nolist.ok, nolist.summary) == (False, msg('sharepoint.denied', part='the folder', status=403))
    assert (notfolder.ok, notfolder.summary) == (False, msg('sharepoint.not_folder', folder='Runbooks/Prod'))
    assert (unreachable.ok, unreachable.summary) == (False, msg('enrich.unreachable', source='Microsoft Entra'))
    assert (wrong.ok, wrong.summary) == (False, msg('sharepoint.invalid.site'))
    assert 'AADSTS7000215' not in bad.summary and SECRET not in bad.summary


def test_the_tool_and_the_site_host_are_offered_only_while_sharepoint_serves():
    # given
    store = kit_store()
    runtime = sp_runtime(store, FakeGraph(base_routes()), named=None)
    tools = PluginTools(runtime)

    # when
    before = ([d['name'] for d in tools.definitions()], runtime.ui_hosts())
    runtime.instances.create('sharepoint', 'ops', VALUES, True, USER)
    runtime.instances.create('sharepoint', 'eng', VALUES | {'site': 'https://contoso-my.sharepoint.com/personal/jane_contoso_com'}, True, USER)
    shown = {d['name']: d for d in tools.definitions()}
    hosts = runtime.ui_hosts()
    runtime.instances.set_enabled('sharepoint', 'ops', False, USER)
    runtime.instances.set_enabled('sharepoint', 'eng', False, USER)
    after = ([d['name'] for d in tools.definitions()], runtime.ui_hosts())

    # then
    assert before == ([], ()) and after == ([], ())
    assert list(shown) == ['sharepoint_docs'] and set(shown['sharepoint_docs']['input_schema']['properties']['instance']['enum']) == {'ops', 'eng'}
    assert set(hosts) == {HOST, 'contoso-my.sharepoint.com'}


def test_token_reply_without_a_token_is_a_failure():
    # given
    http = FakeGraph(base_routes() | {('POST', LOGIN): token_reply() | {'access_token': ''}})

    # when
    out = refused(graph(http), op='search', text='cart')

    # then
    assert out.status == 502


def test_the_tool_runs_through_the_runtime_with_the_unsealed_secret_on_the_named_instance():
    # given
    http = FakeGraph(base_routes())
    runtime = sp_runtime(kit_store(), http, named='ops')
    tools = PluginTools(runtime)

    # when
    out = tools.call('sharepoint_docs', {'op': 'search', 'text': 'cart', 'instance': 'ops'}, NOW)

    # then
    assert [h['path'] for h in out['hits']] == ['cart-restart.md']
    assert parse_qs(http.hits(LOGIN)[0][3].decode())['client_secret'] == [SECRET]


def test_a_raw_encoded_slash_or_dot_segment_in_a_parent_path_fails_closed():
    # given
    paths = ['/drives/d/root:/Runbooks/Prod', '/drives/d/root:/Runbooks%2FProd', '/drives/d/root:/Runbooks%5cProd', '/drives/d/root:/Runbooks/Prod/%2e%2e/x',
             '/drives/d/root:/Runbooks/./Prod', '/drives/d/root:/Runbooks//Prod', '/drives/d/root:']

    # when
    out = [graph_client.parent_path({'parentReference': {'path': p}}) for p in paths]

    # then
    assert out == ['Runbooks/Prod', None, None, None, None, None, '']


def test_a_read_by_id_is_refused_unless_its_parent_chain_reaches_the_folder():
    # given
    routes = base_routes() | {
        ('GET', f'{GRAPH}/drives/{DRIVE}/items/F4'): file_json('checkout-service.md', 'F4', '/Runbooks/Prod/Checkout'),
        ('GET', f'{GRAPH}/drives/{DRIVE}/items/FOLDER2'): folder_json('Checkout', 'FOLDER2', '/Runbooks/Prod'),
        ('GET', f'{GRAPH}/drives/{DRIVE}/items/F9'): file_json('fake.md', 'F9', '/Runbooks/Prod', pid='ROGUE'),
        ('GET', f'{GRAPH}/drives/{DRIVE}/items/ROGUE'): folder_json('Prod', 'ROGUE', '/Elsewhere', pid='ROOT'),
        ('GET', f'{GRAPH}/drives/{DRIVE}/items/ROOT'): {'id': 'ROOT', 'name': 'root', 'folder': {}, 'root': {}, 'parentReference': {'driveId': DRIVE}},
    } | download('F4', b'# Checkout')
    http = FakeGraph(routes)
    g = graph(http)

    # when
    good = run_tool(g, op='read', id='F4')
    bad = refused(g, op='read', id='F9')

    # then
    assert good['path'] == 'Checkout/checkout-service.md' and http.hits(f'{GRAPH}/drives/{DRIVE}/items/FOLDER2')
    assert (bad.status, bad.message) == (403, msg('sharepoint.outside', path='fake.md', folder='Runbooks/Prod'))
    assert not http.hits(f'{GRAPH}/drives/{DRIVE}/items/F9/content')


def test_a_read_by_path_whose_details_come_back_outside_the_folder_is_refused():
    # given
    http = FakeGraph(base_routes() | {('GET', by_path('cart.md')): file_json('cart.md', 'FX', '/Other', pid='OTHER')})

    # when
    out = refused(graph(http), op='read', path='cart.md')

    # then
    assert (out.status, out.message) == (403, msg('sharepoint.outside', path='cart.md', folder='Runbooks/Prod'))
    assert not [c for c in http.calls if '/content' in c[1]]


@pytest.mark.parametrize('cap, value', [('WALK_FOLDERS', 1), ('WALK_CALLS', 1)])
def test_the_walk_stops_at_its_folder_and_call_caps(monkeypatch, cap, value):
    # given
    monkeypatch.setattr(graph_client, cap, value)
    http = FakeGraph(base_routes())

    # when
    out = run_tool(graph(http), op='search', text='md')

    # then
    assert out['partial'] is True and [h['path'] for h in out['hits']] == ['cart-restart.md']
    assert len([u for u in http.graph_calls() if u.split('?')[0].endswith('/children')]) == 1


def test_the_walk_stops_at_its_deadline(monkeypatch):
    # given
    clock = Clock()
    http = FakeGraph(base_routes())

    def slow(method, url, headers, data, timeout):
        if url.split('?')[0].endswith('/children'):
            clock.now += graph_client.WALK_DEADLINE_S
        return http(method, url, headers, data, timeout)

    # when
    out = run_tool(Graph(TENANT, CLIENT, SECRET, SITE, '', 'Runbooks/Prod', transport=slow, clock=clock), op='search', text='md')

    # then
    assert out['partial'] is True and len([u for u in http.graph_calls() if u.split('?')[0].endswith('/children')]) == 1


def test_a_refused_next_link_marks_the_search_partial():
    # given
    http = FakeGraph(base_routes() | {('GET', f'{GRAPH}/drives/{DRIVE}/items/{FOLDER_ID}/children'): {'value': [file_json('a.md', 'A', '/Runbooks/Prod')],
                                                                                                    '@odata.nextLink': 'https://evil.example.com/next'}})

    # when
    out = run_tool(graph(http), op='search', text='md')

    # then
    assert out['partial'] is True and [h['path'] for h in out['hits']] == ['a.md']
    assert not [c for c in http.calls if 'evil.example.com' in c[1]]


def test_a_word_file_with_too_many_parts_is_refused(monkeypatch):
    # given
    monkeypatch.setattr(graph_client, 'DOCX_ENTRIES', 3)
    routes = base_routes() | {('GET', f'{GRAPH}/drives/{DRIVE}/items/F2'): file_json('Payments outage.docx', 'F2', '/Runbooks/Prod', size=5000)}
    body = docx(['x'], extra={'a.xml': b'1', 'b.xml': b'2'})

    # when
    out = refused(graph(FakeGraph(routes | download('F2', body))), op='read', id='F2')

    # then
    assert (out.status, out.message) == (415, msg('sharepoint.bad_docx', name='Payments outage.docx', why='too many parts'))


def test_a_nested_paragraph_is_printed_once():
    # given
    w = 'http://schemas.openxmlformats.org/wordprocessingml/2006/main'
    xml = f'<w:document xmlns:w="{w}"><w:body><w:p><w:r><w:t>outer</w:t></w:r><w:p><w:r><w:t>inner</w:t></w:r></w:p></w:p><w:p><w:r><w:t>last</w:t></w:r></w:p></w:body></w:document>'
    routes = base_routes() | {('GET', f'{GRAPH}/drives/{DRIVE}/items/F2'): file_json('Payments outage.docx', 'F2', '/Runbooks/Prod', size=5000)}

    # when
    out = run_tool(graph(FakeGraph(routes | download('F2', docx([], xml=xml.encode())))), op='read', id='F2')

    # then
    assert out['text'] == 'outerinner\nlast'


def test_a_graph_401_drops_the_token_and_names_admin_consent():
    # given
    issued = iter(['tok-1', 'tok-2'])
    state = {'refuse': True}

    def children(q, h, d):
        if state.pop('refuse', False):
            return HttpError(401, 'x')
        return {'value': [file_json('cart-restart.md', 'F1', '/Runbooks/Prod')]}

    http = FakeGraph(base_routes() | {('POST', LOGIN): lambda q, h, d: token_reply(next(issued)),
                                      ('GET', f'{GRAPH}/drives/{DRIVE}/items/{FOLDER_ID}/children'): children})
    g = graph(http)

    # when
    first = refused(g, op='search', text='cart')
    second = run_tool(g, op='search', text='cart')

    # then
    assert (first.status, first.message) == (403, msg('sharepoint.denied_token', part='the folder'))
    assert second['hits'] and len(http.hits(LOGIN)) == 2 and http.calls[-1][2]['Authorization'] == 'Bearer tok-2'


def test_throttling_names_the_retry_after():
    # given
    http = FakeGraph(base_routes() | {('GET', f'{GRAPH}/drives/{DRIVE}/items/{FOLDER_ID}/children'): HttpError(429, 'x', '', {'Retry-After': '17'})})

    # when
    out = refused(graph(http), op='search', text='cart')

    # then
    assert (out.status, out.message) == (502, msg('sharepoint.throttled', seconds='17'))


@pytest.mark.parametrize('where', ['http://contoso.sharepoint.com/dl', 'https://evil.example.com/dl', 'https://contoso.sharepoint.com.evil.example.com/dl',
                                   'https://user@contoso.sharepoint.com/dl'])
def test_a_download_redirect_off_sharepoint_or_not_https_is_refused(where):
    # given
    http = FakeGraph(base_routes() | {('GET', f'{GRAPH}/drives/{DRIVE}/items/F1'): file_json('cart-restart.md', 'F1', '/Runbooks/Prod'),
                                      ('GET', f'{GRAPH}/drives/{DRIVE}/items/F1/content'): Reply(b'', {'location': where}, 302)})

    # when
    out = refused(graph(http), op='read', id='F1')

    # then
    assert (out.status, out.message) == (502, msg('sharepoint.no_download'))
    assert [c[1] for c in http.calls if not c[1].startswith((GRAPH, LOGIN))] == []


def test_a_download_over_the_cap_without_size_details_is_too_large():
    # given
    routes = base_routes() | {('GET', f'{GRAPH}/drives/{DRIVE}/items/F1'): file_json('cart-restart.md', 'F1', '/Runbooks/Prod', size=None),
                              ('GET', f'{GRAPH}/drives/{DRIVE}/items/F8'): file_json('cut.md', 'F8', '/Runbooks/Prod', size=None)}
    big = FakeGraph(routes | download('F1', b'x' * (graph_client.TEXT_BYTES + 1)))
    cut = FakeGraph(routes | {('GET', f'{GRAPH}/drives/{DRIVE}/items/F8/content'): Unreachable('https://x gave a cut-off answer')})

    # when
    over, chopped = refused(graph(big), op='read', id='F1'), refused(graph(cut), op='read', id='F8')

    # then
    assert (over.status, over.message) == (413, msg('sharepoint.too_large', name='cart-restart.md', limit='1 MB'))
    assert (chopped.status, chopped.message) == (502, msg('enrich.unreachable', source='Microsoft Graph'))


@pytest.mark.parametrize('bad', ['..', '.', '---', '!!'])
def test_an_item_id_needs_a_letter_or_digit(bad):
    # when
    out = refused(graph(FakeGraph(base_routes())), op='read', id=bad)

    # then
    assert out.status == 400
