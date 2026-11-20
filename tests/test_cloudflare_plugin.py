import dataclasses
import datetime as dt
import json
import time
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient

from skopax import clock
from skopax.app import create_app
from skopax.engine import iso
from skopax.instances import Instances, PluginState
from skopax.messages import msg
from skopax.plugins import cloudflare
from skopax.plugins.cloudflare import checks, client as cf_client, tools
from skopax.plugins.cloudflare.client import API, GQL, STATUS, Cloudflare, CloudflareError, Zone
from skopax.plugintools import PluginTools
from skopax.registry import BUILTIN, Registry, describe
from skopax.remote import HttpError, Reply, Unreachable
from skopax.runtime import Runtime
from skopax.sealer import Sealer, master_key
from skopax.slack import Slack
from skopax.store import Store
from tests.conftest import settings
from tests.fake_http import FakeHttp
from tests.fake_slack import FakeSlackApi
from tests.plugin_kit import KIT_KEY, add_instance
from tests.test_engine import NOW
from tests.test_investigator_tools import box
from tests.test_slack import FakeSlack

TOKEN = 'cf-api-token-3e9a'
USER = 'alice@example.com'
PUSH_USER = 'U0123ABCD'
ACCOUNT = '5d0e2f4a6b8c0d1e3f5a7b9c1d2e4f6a'
OTHER_ACCOUNT = '8a6c4e2f0b9d7f5a3c1e8b6d4f2a0c9e'
SHOP_ID = '3c1d5e7f9a0b2c4d6e8f0a1b3c5d7e9f'
BLOG_ID = '7e9f1a3b5c7d9e0f2a4b6c8d0e1f3a5b'
TOOLS_ID = '9b8a7f6e5d4c3b2a1f0e9d8c7b6a5f4e'
DOCS_ID = '2f4e6d8c0b1a3f5e7d9c2b4a6f8e0d1c'
SHOP, BLOG, TOOLS, DOCS = 'shop.example.com', 'blog.example.com', 'tools.example.net', 'docs.example.org'
SHOP_Z, BLOG_Z, TOOLS_Z, DOCS_Z = Zone(SHOP, SHOP_ID, ACCOUNT), Zone(BLOG, BLOG_ID, ACCOUNT), Zone(TOOLS, TOOLS_ID, ACCOUNT), Zone(DOCS, DOCS_ID, OTHER_ACCOUNT)
KNOWN = (SHOP_Z, BLOG_Z, TOOLS_Z, DOCS_Z)
ZONES = f'{API}/zones'
VERIFY = f'{API}/user/tokens/verify'
CHECKS = f'{API}/zones/{SHOP_ID}/healthchecks'
CERTS = f'{API}/zones/{SHOP_ID}/ssl/certificate_packs'
BLOG_CHECKS = f'{API}/zones/{BLOG_ID}/healthchecks'
BLOG_CERTS = f'{API}/zones/{BLOG_ID}/ssl/certificate_packs'
DOCS_CHECKS = f'{API}/zones/{DOCS_ID}/healthchecks'
DOCS_CERTS = f'{API}/zones/{DOCS_ID}/ssl/certificate_packs'
TOKYO, OSAKA = 'Tokyo, Japan - (NRT)', 'Osaka, Japan - (KIX)'
WATCHED = f'{TOKYO}; CDN/Cache; WAF; Authoritative DNS'
SEEN = f'{TOKYO}; CDN/Cache; WAF'
STATUS_PAGE = 'https://www.cloudflarestatus.com/'
ACCOUNT_VERIFY = f'{API}/accounts/{ACCOUNT}/tokens/verify'
DENIED_BODY = '{"success": false, "errors": [{"code": 9109, "message": "Unauthorized to access requested resource"}]}'
MID = '\u00b7'
TIMES = '\u00d7'


def days(n: float) -> str:
    return iso(NOW + n * 86400)


HEALTH = [
    {'name': 'api-origin', 'address': 'api.shop.example.com', 'http_config': {'path': '/actuator/health'}, 'status': 'unhealthy', 'failure_reason': 'HTTP 503', 'suspended': False},
    {'name': 'web-origin', 'address': 'shop.example.com', 'http_config': {'path': '/'}, 'status': 'healthy', 'suspended': False},
    {'name': 'old-origin', 'address': 'old.shop.example.com', 'http_config': {'path': '/'}, 'status': 'unhealthy', 'suspended': True},
]
HEALTH_2 = [{'name': 'cdn-origin', 'address': 'cdn.shop.example.com', 'http_config': {'path': '/ping'}, 'status': 'unknown', 'failure_reason': 'no data yet', 'suspended': False}]
CERT_PACKS = [
    {'id': 'u-1', 'type': 'universal', 'status': 'active', 'hosts': ['shop.example.com', '*.shop.example.com'], 'certificates': [{'expires_on': days(10)}, {'expires_on': days(80)}]},
    {'id': 'a-1', 'type': 'advanced', 'status': 'active', 'hosts': ['api.shop.example.com'], 'certificates': [{'expires_on': days(2)}]},
    {'id': 'l-1', 'type': 'legacy', 'status': 'active', 'hosts': ['x.shop.example.com'], 'certificates': [{'expires_on': days(200)}]},
    {'id': 'd-1', 'type': 'dedicated', 'status': 'pending_validation', 'hosts': ['a.shop.example.com', 'b.shop.example.com', 'c.shop.example.com', 'd.shop.example.com'], 'certificates': []},
]
STATUS_BODY = {
    'components': [
        {'name': TOKYO, 'status': 'major_outage'},
        {'name': 'CDN/Cache', 'status': 'degraded_performance'},
        {'name': 'WAF', 'status': 'operational'},
        {'name': OSAKA, 'status': 'major_outage'},
    ],
    'incidents': [
        {'id': 'mxthd6onaplz', 'name': 'CDN/Cache issues in Europe', 'status': 'investigating', 'impact': 'minor', 'components': [{'name': 'CDN/Cache'}, {'name': 'Network'}]},
        {'id': 'zzz', 'name': 'Workers KV latency', 'status': 'monitoring', 'impact': 'minor', 'components': [{'name': 'Workers KV'}]},
    ],
}


def listing(pages: list[list[dict]]):
    def answer(q, _):
        page = int(q['page'][0])
        return {'success': True, 'result': pages[page - 1], 'result_info': {'page': page, 'total_pages': len(pages)}}
    return answer


def lookup(*known: Zone):
    def answer(q, _):
        name = q['name'][0]
        found = [{'id': z.id, 'name': z.name, 'status': 'active', 'account': {'id': z.account, 'name': 'Example'}} for z in known if z.name == name]
        return {'success': True, 'result': found, 'result_info': {'page': 1, 'total_pages': 1, 'count': len(found)}}
    return answer


def bucket(at: dt.datetime, req: int, codes: dict[int, int], cached: int = 0) -> dict:
    return {'dimensions': {'datetime': checks.edge_t(at)}, 'sum': {'requests': req, 'cachedRequests': cached, 'threats': 0, 'responseStatusMap': [{'edgeResponseStatus': k, 'requests': v} for k, v in codes.items()]},
            'uniq': {'uniques': req // 10}}


def split_row(at: dt.datetime, edge: int, origin: int, n: int) -> dict:
    return {'count': n, 'dimensions': {'datetimeHour': checks.edge_t(at), 'edgeResponseStatus': edge, 'originResponseStatus': origin}}


def top_row(at: dt.datetime, edge: int, origin: int, n: int, host: str, path: str, ua: str) -> dict:
    row = split_row(at, edge, origin, n)
    row['dimensions'].update(clientRequestHTTPHost=host, clientRequestPath=path, userAgent=ua)
    return row


def edge_detail(now: float, conn_from_origin: int = 2900) -> dict:
    p, h = checks.edge_hour(now) - dt.timedelta(hours=1), checks.edge_hour(now)
    split = [split_row(p, 502, 502, conn_from_origin), split_row(p, 502, 0, 3400 - conn_from_origin), split_row(p, 522, 0, 100),
             split_row(p, 500, 500, 280), split_row(p, 503, 0, 20), split_row(h, 502, 502, 7)]
    top = [top_row(h, 502, 502, 7, SHOP, '/later', 'curl/8'),
           top_row(p, 502, 502, conn_from_origin, SHOP, '/garden/chairs/oak', 'WhatsApp/2.23.20.0'),
           top_row(p, 502, 0, 3400 - conn_from_origin, 'api.shop.example.com', '/v1/x', ''),
           top_row(p, 500, 500, 280, SHOP, '/blog/y', 'Mozilla/5.0 (iPhone; CPU iPhone OS 18_7 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko)'),
           top_row(p, 522, 0, 100, 'cdn.shop.example.com', '/a.png', 'Mozilla/5.0')]
    return {'rows': {SHOP_ID: {'split': split, 'top': top}, BLOG_ID: {'split': [], 'top': []}}, 'error': None}


def edge_hours(now: float) -> dict:
    h = checks.edge_hour(now)
    shop = [bucket(h - dt.timedelta(days=1), 600_000, {200: 599_000, 502: 30}), bucket(h - dt.timedelta(days=1, hours=1), 590_000, {502: 20, 500: 10}),
            bucket(h - dt.timedelta(hours=1), 610_000, {200: 600_000, 502: 3500, 522: 100, 500: 300, 503: 20}, 540_000), bucket(h, 1_200, {200: 1_200})]
    blog = [bucket(h, 3, {522: 2, 200: 1})]
    return {SHOP_ID: shop, BLOG_ID: blog}


def edge_data(now: float, detail: dict | None = None) -> dict:
    h = checks.edge_hour(now)
    week = {SHOP_ID: {checks.edge_t(h - dt.timedelta(days=7, hours=1)): {'req': 700_000, 'codes': {502: 40}}}}
    return {'hour': checks.edge_t(h), 'hours': edge_hours(now), 'week': {'rows': week, 'error': None}, 'detail': detail if detail is not None else edge_detail(now)}


def edge_gql(now: float, empty: bool = False):
    hours = edge_hours(now)

    def answer(_, body):
        if 'httpRequests1hGroups' in body['query']:
            asked = body['variables']['z']
            return {'data': {'viewer': {'zones': [] if empty else [{'zoneTag': t, 'httpRequests1hGroups': hours.get(t, [])} for t in asked]}}}
        return {'data': {'viewer': {'zones': []}}}
    return answer


def cf_routes(over: dict | None = None, now: float = NOW) -> dict:
    routes = {('GET', ZONES): lookup(*KNOWN), ('GET', VERIFY): {'success': True, 'result': {'id': 'tok-1', 'status': 'active'}},
              ('GET', CHECKS): listing([HEALTH, HEALTH_2]), ('GET', CERTS): listing([CERT_PACKS]), ('GET', STATUS): STATUS_BODY,
              ('GET', BLOG_CHECKS): listing([[]]), ('GET', BLOG_CERTS): listing([[]]),
              ('GET', DOCS_CHECKS): listing([[{'name': 'docs-origin', 'address': 'docs.example.org', 'http_config': {'path': '/'}, 'status': 'unhealthy', 'failure_reason': 'HTTP 502', 'suspended': False}]]),
              ('GET', DOCS_CERTS): listing([[{'type': 'universal', 'status': 'active', 'hosts': [DOCS], 'certificates': [{'expires_on': days(80)}]}]]), ('POST', GQL): edge_gql(now)}
    routes.update(over or {})
    return routes


def fake_cf(over: dict | None = None) -> FakeHttp:
    return FakeHttp(cf_routes(over))


def cf_runtime(store: Store, http=None, **instances: dict) -> Runtime:
    registry = Registry.of(cloudflare.PLUGIN)
    runtime = Runtime(registry, PluginState(store.db), Instances(store.db, registry, lambda: Sealer(KIT_KEY)), http)
    for index, (name, values) in enumerate((instances or {'main': {}}).items()):
        add_instance(store, cloudflare.PLUGIN, name, sealer_key=KIT_KEY, primary=index == 0, **({'token': TOKEN, 'zones': SHOP} | values))
    runtime.refresh()
    return runtime


def connection_of(runtime: Runtime, name: str = 'main'):
    return cloudflare.test(runtime.context(cloudflare.PLUGIN, runtime.instances.get('cloudflare', name), NOW))


def app_with(store: Store, http, slack: Slack | None = None, push_user: str = '', post_sev: str = 'warn', **instances: dict) -> TestClient:
    key = master_key(store.db, settings())
    for index, (name, values) in enumerate(instances.items()):
        add_instance(store, cloudflare.PLUGIN, name, sealer_key=key, primary=index == 0, **({'token': TOKEN, 'zones': SHOP} | values))
    return TestClient(create_app(dataclasses.replace(settings(), slack_push_user=push_user, slack_post_sev=post_sev), store, transport=http, **({'slack': slack} if slack else {})))


def sweep(c: TestClient, *check_ids: str, now: float = NOW):
    return [c.app.state.sweeper.run('cloudflare', check_id, now) for check_id in check_ids or ('cf', 'edge')]


def cards_of(store: Store) -> dict[str, dict]:
    return {a['key']: a for a in store.load()['anomalies']}


def tool_box(store: Store, http, run_id: str | None = None, **instances: dict):
    return box(store, run_id=run_id, plugin_tools=PluginTools(cf_runtime(store, http, **instances)))


def part(rows: list[dict], name: str) -> list[dict]:
    return [r for r in rows if r.get('part') == name]


def gql_posts(http: FakeHttp) -> list:
    return http.hits(GQL)


def test_cloudflare_identity_and_manifest():
    # given
    registry = Registry.of(cloudflare.PLUGIN)

    # when
    loaded = registry.get('cloudflare')
    manifest = describe(cloudflare.PLUGIN, icon=True)

    # then
    assert (loaded.state, loaded.reason) == ('ok', None) and 'skopax.plugins.cloudflare' in BUILTIN
    assert Registry.load({}, entry_points=False).get('cloudflare').state == 'ok'
    assert json.loads(json.dumps(manifest)) == manifest
    assert (manifest['name'], manifest['version'], manifest['category'], manifest['author'], manifest['licence'], manifest['core'], manifest['edition']) == (
        'Cloudflare', '0.1.0', 'web', 'Skopax', 'Apache-2.0', '>=0.1,<0.5', 'community')
    assert manifest['capabilities'] == {'hosts': ['api.cloudflare.com', 'www.cloudflarestatus.com'], 'secrets': ['token'], 'agentReads': False} and manifest['uses'] == []
    assert [(f['key'], f['kind'], f['required']) for f in manifest['fields']] == [('token', 'secret', True), ('zones', 'text', True), ('zone-checks', 'text', False), ('account', 'text', False), ('status-components', 'text', False)]
    assert [(c['id'], c['period'], c['offset'], c['ran']) for c in manifest['checks']] == [('cf', 900, 60, ['cf']), ('edge', 900, 60, ['edge'])]
    assert [(c.id, c.sections, c.tool) for c in cloudflare.PLUGIN.checks] == [('cf', ('cloudflare',), None), ('edge', ('edge',), 'edge')]
    assert [(t.name, t.kind, t.per_run, t.scan, t.instance_input) for t in cloudflare.PLUGIN.tools] == [('cloudflare_graphql', 'read', 6, True, 'instance'), ('edge', 'read', None, True, 'instance')]
    assert [r['id'] for r in manifest['rules']] == ['edge']
    assert manifest['panels'] == [{'tab': 'cf', 'script': 'panel.js', 'fragment': None, 'label': 'Cloudflare', 'group': 'observe'}]
    assert manifest['knowledge'] == ['cloudflare'] and len(cloudflare.PLUGIN.knowledge[0].text.splitlines()) <= 40
    assert registry.icon('cloudflare').startswith('<svg')


def test_cloudflare_defaults_are_neutral():
    # when
    fields = {f.key: f.default for f in cloudflare.PLUGIN.fields}
    rules = {r.id: dict(r.defaults) for r in cloudflare.PLUGIN.rules}

    # then
    assert fields == {'token': '', 'zones': '', 'zone-checks': '', 'account': '', 'status-components': ''}
    assert cloudflare.PLUGIN.settings == ()
    assert rules == {'edge': dict(min_req=5000, conn_crit_n=200, conn_crit_pct=0.5, conn_warn_n=50, conn_warn_pct=0.05, conn_info_n=20, conn_info_pct=0.02,
                                  srv_crit_n=500, srv_crit_pct=1.0, srv_warn_n=200, srv_warn_pct=0.2, srv_info_n=50, srv_info_pct=0.05)}
    assert checks.CERT_WARN_DAYS == 14


def test_cloudflare_connection_test_reports_ok_and_failures(store: Store):
    # given
    good = fake_cf()
    denied = fake_cf({('GET', VERIFY): HttpError(401, VERIFY)})
    down = fake_cf({('GET', VERIFY): Unreachable('x')})
    disabled = fake_cf({('GET', VERIFY): {'success': True, 'result': {'id': 'tok-1', 'status': 'disabled'}}})

    # when
    ok, refused, unreachable, off = (connection_of(cf_runtime(store, http, main={'zones': f'{SHOP}, {BLOG}'})) for http in (good, denied, down, disabled))
    unseen = connection_of(cf_runtime(store, fake_cf(), main={'zones': f'{SHOP}, gone.example.com, other.example.com'}))

    # then
    assert (ok.ok, ok.summary) == (True, f'Cloudflare token active {MID} zones: {SHOP}, {BLOG}') and isinstance(ok.ms, int)
    assert (refused.ok, refused.summary) == (False, msg('connection.rejected', status=401))
    assert (unreachable.ok, unreachable.summary) == (False, msg('connection.unreachable'))
    assert (off.ok, off.summary) == (False, 'Cloudflare token is disabled')
    assert (unseen.ok, unseen.summary) == (False, 'Cloudflare token active, but it cannot see the zones gone.example.com, other.example.com')
    assert [c[1].split('?')[0] for c in good.calls] == [VERIFY, ZONES, ZONES, CHECKS, CERTS, GQL]
    assert all(c[2]['Authorization'] == f'Bearer {TOKEN}' for c in good.calls)
    assert TOKEN not in ok.summary


def test_zone_names_are_resolved_to_ids_once_per_run(store: Store):
    # given
    http = fake_cf()
    failing = FakeHttp({('GET', ZONES): HttpError(403, ZONES)})
    client, broken = Cloudflare(TOKEN, http), Cloudflare(TOKEN, failing)

    # when
    first = client.zones([SHOP, BLOG, 'gone.example.com', 'not a zone'])
    again = client.zones([SHOP, BLOG])
    direct = [parse_qs(urlsplit(c[1]).query)['name'][0] for c in http.hits(ZONES)]
    with pytest.raises(CloudflareError) as e1:
        broken.zones([SHOP])
    with pytest.raises(CloudflareError) as e2:
        broken.zones([BLOG])
    with app_with(store, http, main={'zones': f'{SHOP}, {BLOG}', 'status-components': WATCHED}) as c:
        before = len(http.hits(ZONES))
        sweep(c, 'cf')
        cf_lookups = len(http.hits(ZONES)) - before
        sweep(c, 'edge')
        edge_lookups = len(http.hits(ZONES)) - before - cf_lookups

    # then
    assert first == ([SHOP_Z, BLOG_Z], ['gone.example.com', 'not a zone']) and again == ([SHOP_Z, BLOG_Z], [])
    assert direct == [SHOP, BLOG, 'gone.example.com']
    assert (e1.value.reason, e2.value.reason) == ('Cloudflare API zones HTTP 403', 'Cloudflare API zones HTTP 403') and len(failing.calls) == 1
    assert (cf_lookups, edge_lookups) == (2, 2)
    assert http.hits(CHECKS) and http.hits(BLOG_CHECKS) and http.hits(CERTS) and http.hits(BLOG_CERTS)
    assert gql_posts(http)[0][3]['variables']['z'] == [SHOP_ID, BLOG_ID]


def test_cf_pass_reads_health_checks_certificates_and_the_status_page():
    # given
    http = fake_cf()

    # when
    got = checks.cf_pass(Cloudflare(TOKEN, http), [SHOP_Z], checks.components_of(WATCHED), NOW)
    found, rows = got.found, got.rows
    by = {x['key']: x for x in found}

    # then
    assert sorted(by) == [f'cf-cert|cloudflare/advanced|{SHOP}|a-1', f'cf-cert|cloudflare/dedicated|{SHOP}|d-1', f'cf-cert|cloudflare/universal|{SHOP}|u-1', f'cf-health|cloudflare/api-origin|{SHOP}',
                          f'cf-health|cloudflare/cdn-origin|{SHOP}', 'cf-incident|mxthd6onaplz', 'cf-status|cloudflare/CDN/Cache', f'cf-status|cloudflare/{TOKYO}']
    assert by[f'cf-health|cloudflare/api-origin|{SHOP}']['sev'] == 'crit'
    assert by[f'cf-health|cloudflare/api-origin|{SHOP}']['what'] == 'Cloudflare health check api-origin (api.shop.example.com/actuator/health) is unhealthy: HTTP 503'
    assert by[f'cf-health|cloudflare/cdn-origin|{SHOP}']['sev'] == 'warn'
    universal = by[f'cf-cert|cloudflare/universal|{SHOP}|u-1']
    assert (universal['sev'], universal['now']) == ('warn', 10.0)
    assert universal['short'] == f'WARN Cloudflare universal certificate pack of {SHOP} expires in 10 days'
    assert by[f'cf-cert|cloudflare/advanced|{SHOP}|a-1']['sev'] == 'crit'
    assert by[f'cf-cert|cloudflare/dedicated|{SHOP}|d-1']['what'] == (f'dedicated certificate pack of {SHOP} (a.shop.example.com, b.shop.example.com, c.shop.example.com) '
                                                               'has status pending_validation')
    assert by[f'cf-status|cloudflare/{TOKYO}']['sev'] == 'warn'
    assert by[f'cf-status|cloudflare/{TOKYO}']['what'].endswith('traffic through that data center is re-routed or degraded')
    assert by['cf-status|cloudflare/CDN/Cache']['sev'] == 'info'
    assert by['cf-incident|mxthd6onaplz']['what'] == 'Cloudflare incident (investigating, minor) on CDN/Cache: CDN/Cache issues in Europe'
    assert all(x['src'] == 'cf' and 'cluster' not in x and 'plugin' not in x for x in found)
    assert {x['ranKey'] for x in found if x['sig'] == 'cf-health'} == {f'cf:{SHOP}:checks'} and {x['ranKey'] for x in found if x['sig'] == 'cf-cert'} == {f'cf:{SHOP}:certs'}
    assert {x['ranKey'] for x in found if x['sig'] in ('cf-status', 'cf-incident')} == {'cf:status'}
    assert [c['name'] for c in part(rows, 'checks')] == ['api-origin', 'web-origin', 'old-origin', 'cdn-origin'] and {c['zone'] for c in part(rows, 'checks')} == {SHOP}
    assert part(rows, 'components') == [{'part': 'components', 'name': TOKYO, 'status': 'major_outage'}, {'part': 'components', 'name': 'CDN/Cache', 'status': 'degraded_performance'},
                                        {'part': 'components', 'name': 'WAF', 'status': 'operational'}]
    assert part(rows, 'certs')[0] == {'part': 'certs', 'zone': SHOP, 'id': 'u-1', 'type': 'universal', 'status': 'active', 'expires': days(10)}
    assert got.read == {'health checks': True, 'certificates': True, 'status': True} and got.ran == [f'cf:{SHOP}:checks', f'cf:{SHOP}:certs', 'cf:status'] and got.denied == {}
    assert all('cluster' not in r and 'plugin' not in r for r in rows)
    assert [c[2].get('Authorization') for c in http.hits(CHECKS)] == [f'Bearer {TOKEN}', f'Bearer {TOKEN}']
    assert 'Authorization' not in http.hits(STATUS)[0][2]


def test_status_components_take_names_with_commas_split_on_semicolons_or_lines():
    # when
    parsed = checks.components_of(f' {TOKYO} ;CDN/Cache\nWAF;; ')

    # then
    assert parsed == [TOKYO, 'CDN/Cache', 'WAF']
    assert 'Semicolon' in next(f for f in cloudflare.PLUGIN.fields if f.key == 'status-components').help


def test_an_api_error_fails_only_its_part_with_a_short_reason_and_a_refusal_is_not_an_error(store: Store):
    # given
    broken = fake_cf({('GET', CERTS): HttpError(500, CERTS, f'{{"errors": [{{"message": "token {TOKEN} broke the backend"}}]}}')})
    unsuccessful = fake_cf({('GET', CERTS): {'success': False, 'errors': [{'message': 'Internal error'}]}})
    refused = fake_cf({('GET', CERTS): HttpError(403, CERTS)})
    expired = fake_cf({('GET', CHECKS): HttpError(401, CHECKS)})
    path = f'/zones/{SHOP_ID}/ssl/certificate_packs'

    # when
    with pytest.raises(CloudflareError) as e1:
        Cloudflare(TOKEN, broken).listing(path)
    with pytest.raises(CloudflareError) as e2:
        Cloudflare(TOKEN, unsuccessful).listing(path)
    failed = checks.cf_pass(Cloudflare(TOKEN, broken), [SHOP_Z], [], NOW)
    certs = checks.cf_pass(Cloudflare(TOKEN, refused), [SHOP_Z], [], NOW)
    health = checks.cf_pass(Cloudflare(TOKEN, expired), [SHOP_Z], [], NOW)
    with app_with(store, broken, main={}) as c:
        sweep(c, 'cf')
        state = store.load()

    # then
    assert e1.value.reason == 'Cloudflare API certificate_packs HTTP 500' and TOKEN not in e1.value.shown(TOKEN) and 'broke the backend' in e1.value.shown(TOKEN)
    assert e2.value.reason == 'Cloudflare API certificate_packs answered an error' and 'Internal error' in e2.value.detail
    assert failed.errors == {'certificates': 'Cloudflare API certificate_packs HTTP 500'} and failed.ran == [f'cf:{SHOP}:checks', 'cf:status']
    assert certs.denied == {'certificates': [SHOP]} and certs.ran == [f'cf:{SHOP}:checks', 'cf:status'] and not certs.errors
    assert health.denied == {'health checks': [SHOP]} and health.ran == [f'cf:{SHOP}:certs', 'cf:status']
    assert state['sources']['cf'] == 'error: certificates: Cloudflare API certificate_packs HTTP 500'


def test_a_zone_without_health_checks_is_read_as_having_none(store: Store):
    # given
    http = fake_cf({('GET', CHECKS): HttpError(404, CHECKS, '{"success": false, "errors": [{"code": 1002, "message": "not entitled"}]}'),
                    ('GET', BLOG_CHECKS): {'success': False, 'errors': [{'code': 1002, 'message': 'Health Checks are not available'}]}})

    # when
    got = checks.cf_pass(Cloudflare(TOKEN, http), [SHOP_Z, BLOG_Z], [], NOW)
    found, rows = got.found, got.rows
    with app_with(store, http, main={'zones': f'{SHOP}, {BLOG}'}) as c:
        sweep(c, 'cf')
        state = store.load()

    # then
    assert not [x for x in found if x['sig'] == 'cf-health'] and part(rows, 'checks') == []
    assert got.read == {'health checks': True, 'certificates': True} and got.denied == {} and f'cf:{SHOP}:checks' in got.ran and f'cf:{BLOG}:checks' in got.ran
    assert state['sources']['cf'] == 'ok'
    summary = part(state['cloudflare'], 'summary')
    assert len(summary) == 1 and summary[0]['noHealthChecks'] == [SHOP, BLOG] and summary[0]['zones'] == [SHOP, BLOG]


def test_no_status_check_without_components(store: Store):
    # given
    http = fake_cf()

    # when
    with app_with(store, http, main={'status-components': WATCHED}) as c:
        sweep(c, 'cf')
        before = {k for k, a in cards_of(store).items() if a['status'] == 'open'}
        reads = len(http.hits(STATUS))
        c.app.state.runtime.instances.update('cloudflare', 'main', {'status-components': ''}, USER)
        c.app.state.runtime.refresh()
        sweep(c, 'cf', now=NOW + 900)
        state = store.load()
    cards = cards_of(store)

    # then
    assert {'cf-incident|mxthd6onaplz', f'cf-status|cloudflare/{TOKYO}'} <= before and reads == 1
    assert len(http.hits(STATUS)) == 1
    assert cards['cf-incident|mxthd6onaplz']['status'] == 'cleared' and cards[f'cf-status|cloudflare/{TOKYO}']['status'] == 'cleared'
    assert cards[f'cf-health|cloudflare/api-origin|{SHOP}']['status'] == 'open'
    assert state['sources']['cf'] == 'ok' and part(state['cloudflare'], 'components') == []
    assert part(state['cloudflare'], 'summary')[0]['statusOff'] is True


def test_empty_lists_from_a_200_are_none_and_close_their_cards(store: Store):
    # given
    http = fake_cf()

    # when
    with app_with(store, http, main={}) as c:
        sweep(c, 'cf')
        opened = {k for k, a in cards_of(store).items() if a['status'] == 'open'}
        http.routes.update({('GET', CHECKS): listing([[]]), ('GET', CERTS): listing([[]])})
        sweep(c, 'cf', now=NOW + 900)
        state = store.load()
    cards = cards_of(store)

    # then
    assert f'cf-health|cloudflare/api-origin|{SHOP}' in opened
    assert state['sources']['cf'] == 'ok'
    assert all(cards[k]['status'] == 'cleared' for k in opened)
    assert part(state['cloudflare'], 'checks') == [] and part(state['cloudflare'], 'certs') == []


def test_edge_without_data_for_any_zone_is_ok_and_quiet_and_names_the_empty_zones(store: Store):
    # given
    http = fake_cf()

    # when
    with app_with(store, http, main={'zones': f'{SHOP}, {BLOG}, {TOOLS}'}) as c:
        sweep(c, 'edge', now=NOW + 360)
        opened = {k for k, a in cards_of(store).items() if a['status'] == 'open'}
        partly = store.load()['sources']['edge']
        http.routes[('POST', GQL)] = edge_gql(NOW + 360, empty=True)
        sweep(c, 'edge', now=NOW + 1260)
        state = store.load()
    cards = cards_of(store)

    # then
    assert f'edge|{SHOP}|edge-conn' in opened
    assert partly == f'ok {MID} no data for {TOOLS}'
    assert state['sources']['edge'] == f'ok {MID} no data'
    assert all(cards[k]['status'] == 'open' for k in opened)
    assert {r['zone']: r['req'] for r in state['edge']} == {SHOP: None, BLOG: None, TOOLS: None}


def test_a_watched_zone_the_token_cannot_see_is_named_and_none_seen_fails(store: Store):
    # given
    http = fake_cf()

    # when
    with app_with(store, http, main={'zones': f'{SHOP}, gone.example.com'}) as c:
        sweep(c)
        partly = dict(store.load()['sources'])
    store.db['instances'].delete_many({})
    with app_with(store, http, main={'zones': 'gone.example.com'}) as c:
        sweep(c)
        none = dict(store.load()['sources'])

    # then
    assert partly['cf'] == f'ok {MID} no data for gone.example.com'
    assert partly['edge'] == f'ok {MID} no data for gone.example.com'
    assert none['cf'] == none['edge'] == 'error: zones: no watched zone found'


def test_evaluate_edge_falls_back_to_the_complete_hour_and_flags_the_share():
    # given
    data = edge_data(NOW + 360)

    # when
    found, rows = checks.evaluate_edge(data, [SHOP_Z, BLOG_Z, TOOLS_Z], NOW + 360, checks.EDGE_RULE)
    by = {x['key']: x for x in found}
    zones = {r['zone']: r for r in rows}
    at = NOW - 3600
    clock.set_zone('Asia/Tokyo')
    tokyo = {x['key']: x for x in checks.evaluate_edge(data, [SHOP_Z], NOW + 360, checks.EDGE_RULE)[0]}[f'edge|{SHOP}|edge-conn']['what']
    clock.set_zone('UTC')

    # then
    assert sorted(by) == [f'edge|{SHOP}|edge-5xx', f'edge|{SHOP}|edge-conn']
    conn = by[f'edge|{SHOP}|edge-conn']
    assert (conn['sev'], conn['src'], conn['ranKey'], conn['ns'], conn['unit']) == ('crit', 'cf', f'edge:{SHOP}', 'cloudflare', '%')
    assert (conn['now'], conn['d1'], conn['d7']) == (0.59, 0.003, 0.006)
    assert f'the {clock.hm(at)} {clock.label(at)} hour (complete)' in conn['what'] and f'Codes: 502 {TIMES} 3,500, 522 {TIMES} 100.' in conn['what']
    assert 'the 07:00 UTC hour (complete)' in conn['what']
    assert 'the 16:00 JST hour (complete)' in tokyo
    assert by[f'edge|{SHOP}|edge-5xx']['sev'] == 'info'
    assert zones[SHOP]['running'] is False and zones[SHOP]['evaluated'] is True
    assert zones[BLOG]['evaluated'] is False
    assert zones[TOOLS]['req'] is None
    assert [r['zone'] for r in rows][0] == SHOP
    assert zones[SHOP]['link'] == f'https://dash.cloudflare.com/{ACCOUNT}/{SHOP}/analytics/traffic' and 'url' not in zones[SHOP]
    assert conn['urls'] == [{'label': f'Cloudflare traffic analytics for {SHOP}', 'url': f'https://dash.cloudflare.com/{ACCOUNT}/{SHOP}/analytics/traffic'}]
    assert all('cluster' not in x and 'plugin' not in x for x in found + rows)


def test_evaluate_edge_says_when_our_origin_sent_the_5xx_and_names_the_top_source():
    # given
    data = edge_data(NOW + 360)

    # when
    found, _ = checks.evaluate_edge(data, [SHOP_Z, BLOG_Z, TOOLS_Z], NOW + 360, checks.EDGE_RULE)
    by = {x['key']: x for x in found}
    conn, srv = by[f'edge|{SHOP}|edge-conn'], by[f'edge|{SHOP}|edge-5xx']

    # then
    assert 'Origin status (sampled): 83 % were sent by our origin itself and Cloudflare passed them on, so they are in our ingress logs too; look there first.' in conn['what']
    assert conn['what'].endswith(f' Top (sampled): 2,900 {TIMES} 502 {SHOP}/garden/chairs/oak {MID} WhatsApp/2.23.20.0; 500 {TIMES} 502 api.shop.example.com/v1/x {MID} no user agent.')
    assert 'never appear in our ingress logs' not in conn['what'] and '/later' not in conn['what']
    assert conn['short'] == f'CRIT Cloudflare {SHOP} 502 / 52x 0.59% of edge requests (3,600 in the hour, 83 % from our origin)'
    assert 'Origin status (sampled): 93 % were sent by our origin itself' in srv['what']
    assert srv['what'].endswith(f'Top (sampled): 280 {TIMES} 500 {SHOP}/blog/y {MID} Mozilla/5.0 (iPhone; CPU iPhone OS 18_7 like Mac OS X) Apple.')


def test_evaluate_edge_says_when_cloudflare_made_the_5xx_itself():
    # given
    data = edge_data(NOW + 360, edge_detail(NOW + 360, conn_from_origin=400))

    # when
    found, _ = checks.evaluate_edge(data, [SHOP_Z, BLOG_Z, TOOLS_Z], NOW + 360, checks.EDGE_RULE)
    conn = {x['key']: x for x in found}[f'edge|{SHOP}|edge-conn']

    # then
    assert ('Origin status (sampled): 89 % were made at the Cloudflare edge with no answer from our origin, so those are not in our ingress logs; '
            f'the other 11 % came from our origin. Top (sampled): 3,000 {TIMES} 502 api.shop.example.com/v1/x') in conn['what']
    assert conn['short'].endswith('(3,600 in the hour, 11 % from our origin)')


def test_evaluate_edge_without_origin_detail_keeps_both_readings_open():
    # given
    data = edge_data(NOW + 360, {'rows': {}, 'error': 'Cloudflare GraphQL HTTP 500'})

    # when
    found, rows = checks.evaluate_edge(data, [SHOP_Z, BLOG_Z, TOOLS_Z], NOW + 360, checks.EDGE_RULE)
    conn = {x['key']: x for x in found}[f'edge|{SHOP}|edge-conn']

    # then
    assert conn['what'].endswith('502 can come from our origin (Cloudflare passes it on, so it is in our ingress logs too) or from Cloudflare itself; '
                                 '520\u2013526 are Cloudflare failing to reach or get a timely answer from our origin, which never shows in our ingress logs.')
    assert conn['short'] == f'CRIT Cloudflare {SHOP} 502 / 52x 0.59% of edge requests (3,600 in the hour)'
    assert checks.evaluate_edge({k: v for k, v in data.items() if k != 'detail'}, [SHOP_Z, BLOG_Z, TOOLS_Z], NOW + 360, checks.EDGE_RULE) == (found, rows)


def test_edge_pass_asks_every_dataset_and_keeps_going_without_the_sampled_ones():
    # given
    def gql(_, body):
        if 'httpRequestsAdaptiveGroups' in body['query']:
            return {'errors': [{'message': 'quota exceeded for this dataset'}]}
        return {'data': {'viewer': {'zones': [{'zoneTag': 'z1', 'httpRequests1hGroups': [{'dimensions': {'datetime': 'x'}}]}]}}}
    http = FakeHttp({('POST', GQL): gql})

    # when
    data = checks.edge_pass(Cloudflare(TOKEN, http), [SHOP_Z, BLOG_Z, TOOLS_Z], NOW)

    # then
    assert data['hour'] == checks.edge_t(checks.edge_hour(NOW))
    assert data['hours'] == {'z1': [{'dimensions': {'datetime': 'x'}}]}
    assert data['week']['error'] == 'Cloudflare GraphQL answered an error'
    assert data['detail'] == {'rows': {}, 'error': 'Cloudflare GraphQL answered an error'}
    first = http.calls[0][3]
    assert first['variables']['z'] == [SHOP_ID, BLOG_ID, TOOLS_ID]
    assert http.calls[0][2]['Authorization'] == f'Bearer {TOKEN}'
    detail = http.calls[2][3]
    assert 'originResponseStatus' in detail['query'] and 'edgeResponseStatus_geq:500' in detail['query']
    assert (detail['variables']['a'], detail['variables']['b']) == (checks.edge_t(checks.edge_hour(NOW) - dt.timedelta(hours=1)), checks.edge_t(checks.edge_hour(NOW) + dt.timedelta(hours=1)))


def test_edge_pass_keeps_the_origin_split_and_top_rows_per_zone():
    # given
    split, top = [split_row(checks.edge_hour(NOW), 502, 502, 9)], [top_row(checks.edge_hour(NOW), 502, 502, 9, SHOP, '/', 'x')]

    def gql(_, body):
        if 'originResponseStatus' in body['query']:
            return {'data': {'viewer': {'zones': [{'zoneTag': 'z1', 'split': split, 'top': top}]}}}
        if 'httpRequestsAdaptiveGroups' in body['query']:
            return {'data': {'viewer': {'zones': []}}}
        return {'data': {'viewer': {'zones': [{'zoneTag': 'z1', 'httpRequests1hGroups': []}]}}}

    # when
    data = checks.edge_pass(Cloudflare(TOKEN, FakeHttp({('POST', GQL): gql})), [SHOP_Z], NOW)

    # then
    assert data['detail'] == {'rows': {'z1': {'split': split, 'top': top}}, 'error': None}


def test_run_passes_reports_each_pass_on_its_own(store: Store):
    # given
    http = fake_cf({('POST', GQL): HttpError(500, GQL)})

    # when
    with app_with(store, http, main={'status-components': WATCHED}) as c:
        sweep(c)
        state = store.load()

    # then
    assert state['serverPasses']['cf']['status'] == state['sources']['cf'] == f'ok {MID} no data for Authoritative DNS'
    assert {r['part'] for r in state['cloudflare']} == {'checks', 'certs', 'components', 'summary'}
    assert state['sources']['edge'] == 'error: analytics: Cloudflare GraphQL HTTP 500'


def test_links_follow_the_local_check():
    # given
    info = cloudflare.InstanceInfo('main', True, None, {'zones': SHOP})
    pinned = cloudflare.InstanceInfo('main', True, None, {'zones': SHOP, 'account': OTHER_ACCOUNT})
    carried = {'src': 'cf', 'sig': 'edge-conn', 'svc': SHOP, 'urls': [{'label': 'Cloudflare traffic analytics for shop', 'url': f'https://dash.cloudflare.com/{ACCOUNT}/{SHOP}/analytics/traffic'},
                                                                 {'label': 'plain', 'url': 'http://dash.example.com/x'}]}

    # then
    assert cloudflare.links({'src': 'logs', 'sig': 'log'}, info) is None
    assert cloudflare.links({'src': 'cf', 'sig': 'cf-health'}, info) == []
    assert cloudflare.links({'src': 'cf', 'sig': 'cf-incident'}, info) == [{'label': 'Cloudflare status page', 'url': STATUS_PAGE}]
    assert cloudflare.links(carried, info) == [carried['urls'][0], {'label': 'Cloudflare status page', 'url': STATUS_PAGE}]
    assert cloudflare.links(carried, pinned)[0]['url'] == f'https://dash.cloudflare.com/{OTHER_ACCOUNT}/{SHOP}/analytics/traffic'
    assert cloudflare.links({'src': 'cf', 'sig': 'edge-conn', 'svc': SHOP}, info) == [{'label': 'Cloudflare status page', 'url': STATUS_PAGE}]


def test_cloudflare_second_instance_is_scoped(store: Store):
    # given
    http = fake_cf()

    # when
    with app_with(store, http, main={'status-components': WATCHED}, east={'zones': DOCS}) as c:
        sweep(c, 'cf')
        state = store.load()
    cards = cards_of(store)

    # then
    east = cards[f'cf-health|cloudflare/docs-origin|{DOCS}@east']
    assert (east['cluster'], east['ranKey'], east['short'].split(' ')[1]) == ('east', f'cf:{DOCS}:checks-east', 'EAST')
    main = cards[f'cf-health|cloudflare/api-origin|{SHOP}']
    assert 'cluster' not in main and main['ranKey'] == f'cf:{SHOP}:checks'
    assert set(state['serverPasses']) == {'cf', 'cf-east'} and state['sources']['cf-east'] == 'ok'
    assert {r.get('cluster') for r in state['cloudflare'] if r.get('zone') == DOCS} == {'east'}
    assert [r['part'] for r in state['cloudflare'] if r.get('cluster') == 'east' and r['part'] == 'summary'] == ['summary']


def test_cloudflare_rules_come_from_the_rule_set(store: Store):
    # given
    http = fake_cf({('POST', GQL): edge_gql(NOW + 360)})

    # when
    with app_with(store, http, main={}) as c:
        sweep(c, 'edge', now=NOW + 360)
        default = cards_of(store)[f'edge|{SHOP}|edge-conn']['sev']
        c.app.state.runtime.live.set('cloudflare.edge', '{"conn_crit_pct": 5}', USER)
        store.anomalies.delete_many({})
        store.save_state({'anomalies': [], 'initialized': True}, None)
        sweep(c, 'edge', now=NOW + 360)
        stricter = cards_of(store)[f'edge|{SHOP}|edge-conn']['sev']

    # then
    assert (default, stricter) == ('crit', 'warn')


def test_every_pass_test_and_tool_has_a_time_budget_and_each_call_gets_only_the_time_left():
    # given
    ticks = iter([0.0, 0.0, 8.0, 11.0])
    asked = []

    def transport(method, url, headers, data, timeout):
        asked.append(timeout)
        return Reply({'success': True, 'result': [], 'result_info': {'total_pages': 1}}, {})

    client = Cloudflare(TOKEN, transport, budget=10, clock=lambda: next(ticks))

    # when
    client.listing('/zones/x/healthchecks')
    client.listing('/zones/x/healthchecks')
    with pytest.raises(CloudflareError) as late:
        client.listing('/zones/x/healthchecks')

    # then
    assert asked == [10.0, 2.0]
    assert late.value.reason == 'Cloudflare API healthchecks not read: the 10 s budget is used up'
    assert cf_client.BUDGET_S < 240 and cf_client.TEST_BUDGET_S < 15 and cf_client.TOOL_BUDGET_S < 120


def test_a_pass_that_used_its_budget_fails_with_a_fixed_reason(store: Store, monkeypatch):
    # given
    http = fake_cf()
    monkeypatch.setattr(cf_client, 'BUDGET_S', 0)

    # when
    with app_with(store, http, main={}) as c:
        sweep(c, 'cf')
        state = store.load()

    # then
    assert state['sources']['cf'] == 'error: zones: Cloudflare API zones not read: the 0 s budget is used up'
    assert http.calls == []


def test_a_cf_pass_push_is_sent_as_a_dm_and_marked_on_the_state(store: Store):
    # given
    transport = FakeSlack()
    http = fake_cf({('POST', GQL): HttpError(500, GQL)})

    store.save_state({'anomalies': [], 'initialized': True}, None)

    # when
    with app_with(store, http, slack=Slack('xoxb-test', '#ops', transport, FakeSlackApi()), push_user=PUSH_USER, post_sev='crit', main={'status-components': SEEN}) as c:
        applied = sweep(c, 'cf')[0]
        sweep(c, 'edge')
        state = c.get('/api/state').json()

    # then
    assert {a['key'] for a in applied.push} >= {f'cf-health|cloudflare/api-origin|{SHOP}', 'cf-incident|mxthd6onaplz'}
    dms = [p for _, p in transport.calls if p['channel'] == PUSH_USER]
    posts = [p for _, p in transport.calls if p['channel'] != PUSH_USER]
    crit = {a['key'] for a in applied.push if a['sev'] == 'crit'}
    assert len(dms) == 1 and dms[0]['text'].startswith(f'Skopax: CRIT api-origin {SHOP} health check; ')
    assert len(posts) == len(crit) and all('Ongoing since' in p['text'] for p in posts), 'each critical card is posted to the channel at once with its numbers'
    assert not any(msg('autosend.crit_now.last') in json.dumps(p) for p in posts), 'the investigator is off here, so no post promises an investigation'
    assert {k for k in crit if (store.anomaly(k).get('slackPost') or {}).get('at')} == crit
    assert state['serverPushes'][0]['dm'] is True
    assert state['sources']['cf'] == 'ok'
    assert state['sources']['edge'].startswith('error: ')
    assert part(state['cloudflare'], 'checks')[0]['name'] == 'api-origin'


def test_an_instance_switches_on_only_the_cloudflare_jobs_and_keeps_its_token_sealed(store: Store):
    # given
    http = fake_cf()

    # when
    with TestClient(create_app(settings(), store, transport=http)) as c:
        off = [j.name for j in c.app.state.scheduler.jobs if j.name.startswith('cloudflare.')]
        off_info = c.get('/api/server').json()
    with app_with(store, http, main={}) as c:
        jobs = {j.name: j for j in c.app.state.scheduler.jobs if j.name.startswith('cloudflare.')}
        info = c.get('/api/server').json()
        page = c.get('/').text
        script = c.get('/plugins/cloudflare/panel.js')
    doc = store.db['instances'].find_one({'_id': 'cloudflare/main'})

    # then
    assert off == [] and off_info['plugins'] == []
    assert {name: (j.period, j.offset) for name, j in jobs.items()} == {'cloudflare.cf': (900, 60), 'cloudflare.edge': (900, 60)}
    assert [p['id'] for p in info['plugins']] == ['cloudflare'] and [p['tab'] for p in info['plugins'][0]['pages']] == ['cf']
    assert '/plugins/cloudflare/panel.js' in page and script.status_code == 200 and "'skopax:page'" in script.text
    assert 'token' not in doc['values'] and doc['secrets']['token'] != TOKEN and TOKEN not in json.dumps(doc, default=str)


def test_the_cloudflare_table_keeps_its_parts_and_filters_each_list(store: Store):
    # given
    http = fake_cf({('GET', CHECKS): listing([[{'name': 'web-origin', 'address': 'shop.example.com', 'http_config': {'path': '/'}, 'status': 'unhealthy', 'suspended': False}]])})
    with app_with(store, http, main={}) as c:
        sweep(c, 'cf')

    # when
    text, err = tool_box(store, http).call('pass_table', {'source': 'cloudflare', 'name': 'web'})
    out = json.loads(text)

    # then
    assert err is False
    assert out['passes'] == {'cf': store.load()['serverPasses']['cf']}
    assert [(r['part'], r['name'], r['status']) for r in out['rows']] == [('checks', 'web-origin', 'unhealthy')]


def test_cloudflare_graphql_is_a_read_source_tool_offered_only_with_an_instance(store: Store):
    # given
    tool = next(t for t in cloudflare.PLUGIN.tools if t.name == 'cloudflare_graphql')

    # when
    on = {t['name']: t for t in tool_box(store, fake_cf(), main={'zones': f'{SHOP}, {BLOG}'}).definitions()}
    off = [t['name'] for t in box(store).definitions()]
    called = box(store).call('cloudflare_graphql', {'query': '{ viewer { zones { x } } }'})

    # then
    assert tool.scan and tool.kind == 'read' and tool.per_run == 6
    assert 'cloudflare_graphql' in on and 'cloudflare_graphql' not in off and called[1] is True
    assert f'{SHOP}, {BLOG}' in on['cloudflare_graphql']['description'] and '{field:zones}' in tool.description
    assert all(w in tool.description for w in ('Prefer edge', '24 h or less', 'limit', 'clientIP', 'never an instruction'))
    assert tool.input_schema['required'] == ['query']


def test_a_query_goes_out_with_its_variables_and_the_data_comes_back_masked(store: Store):
    # given
    query = '''query($z: String!, $a: Time!, $b: Time!) { viewer { zones(filter: {zoneTag: $z}) {
  firewallEventsAdaptiveGroups(limit: 10, filter: {datetime_geq: $a, datetime_lt: $b}, orderBy: [count_DESC]) { count dimensions { action ruleId clientCountryName } } } } }'''
    variables = {'z': SHOP_ID, 'a': '2026-10-29T06:00:00Z', 'b': '2026-10-29T07:00:00Z'}
    data = {'viewer': {'zones': [{'firewallEventsAdaptiveGroups': [{'count': 812, 'dimensions': {'action': 'managed_challenge', 'ruleId': 'r1', 'clientCountryName': 'JP'}},
                                                                    {'count': 9, 'dimensions': {'action': 'block', 'ruleId': 'bob@example.com', 'clientCountryName': 'DE'}}]}]}}
    http = fake_cf({('POST', GQL): {'data': data, 'errors': None}})

    # when
    text, err = tool_box(store, http).call('cloudflare_graphql', {'query': query, 'variables': variables})

    # then
    assert not err
    assert json.loads(text)['viewer']['zones'][0]['firewallEventsAdaptiveGroups'][0]['count'] == 812
    assert 'bob@example.com' not in text and '[redacted]' in text
    method, url, headers, body = http.hits(GQL)[0]
    assert body == {'query': query, 'variables': variables} and headers['Authorization'] == f'Bearer {TOKEN}'


QUERY = '''query($z: String!, $a: Time!, $b: Time!) { viewer { zones(filter: {zoneTag: $z}) {
  firewallEventsAdaptiveGroups(limit: 10, filter: {datetime_geq: $a, datetime_lt: $b}, orderBy: [count_DESC]) { count dimensions { action ruleId clientCountryName } } } } }'''
VARS = {'z': SHOP_ID, 'a': '2026-10-29T06:00:00Z', 'b': '2026-10-29T07:00:00Z'}
DATA = {'viewer': {'zones': [{'firewallEventsAdaptiveGroups': [{'count': 812, 'dimensions': {'action': 'managed_challenge', 'ruleId': 'r1', 'clientCountryName': 'JP'}}]}]}}


@pytest.mark.parametrize('query, why', [
    ('mutation { deleteZone(id: "x") { id } }', 'no mutation or subscription'),
    ('query { viewer { zones { x } } } mutation y { z }', 'no mutation or subscription'),
    ('subscription { live }', 'no mutation or subscription'),
    ('viewer { zones { x } }', 'only one read query'),
    ('{ viewer { zones(filter: {zoneTag: "z"}) { httpRequestsAdaptiveGroups(limit: 5) { dimensions { clientIP } } } } }', 'clientIP is never read'),
    ('{ viewer { zones { firewallEventsAdaptive(limit: 5) { ua: userAgent } } } }', 'userAgent is never read'),
    ('{ viewer { zones { httpRequestsAdaptive(limit: 5) { clientRequestQuery } } } }', 'clientRequestQuery is never read'),
    ('{ viewer { zones { httpRequestsAdaptive(limit: 5) { clientRefererQuery } } } }', 'clientRefererQuery is never read'),
    ('{ viewer {' + ' x' * tools.GQL_QUERY_LIMIT + ' } }', 'over 4,000 characters'),
])
def test_anything_but_one_private_field_free_query_is_refused_before_a_call(store: Store, query, why):
    # given
    http = fake_cf({('POST', GQL): {'data': DATA}})

    # when
    text, err = tool_box(store, http).call('cloudflare_graphql', {'query': query})

    # then
    assert err and why in text, text
    assert http.calls == []


def test_big_variables_are_refused_and_errors_without_data_are_a_tool_error(store: Store):
    # given
    http = fake_cf({('POST', GQL): {'data': None, 'errors': [{'message': f'zone 3c1d is not accessible by token {TOKEN}? no: limit 10000 exceeded'}]}})
    b = tool_box(store, http)

    # when
    big = b.call('cloudflare_graphql', {'query': QUERY, 'variables': {'z': 'x' * 3000}})
    refused = b.call('cloudflare_graphql', {'query': QUERY, 'variables': VARS})

    # then
    assert big[1] and 'at most 2,000 characters' in big[0]
    assert refused[1] and refused[0].startswith('Cloudflare GraphQL: zone 3c1d') and TOKEN not in refused[0] and 'by token ***' in refused[0]


def test_partial_errors_ride_along_and_a_long_answer_is_cut_and_says_so(store: Store):
    # given
    rows = [{'count': i, 'dimensions': {'clientRequestPath': f'/p/{i}' + 'x' * 100}} for i in range(400)]
    http = fake_cf({('POST', GQL): {'data': {'viewer': {'zones': [{'g': rows}]}}, 'errors': [{'message': 'one leg failed'}]}})

    # when
    text, err = tool_box(store, http).call('cloudflare_graphql', {'query': QUERY, 'variables': VARS})

    # then
    assert not err
    assert '[errors: one leg failed]' in text and f'[cut at {tools.GQL_TEXT_LIMIT:,} of' in text
    assert len(text) < tools.GQL_TEXT_LIMIT + 300


def test_no_instance_refused_http_and_the_per_run_cap(store: Store):
    # given
    b = tool_box(store, fake_cf({('POST', GQL): {'data': DATA}}), run_id='run-1')
    denied = box(store, plugin_tools=PluginTools(cf_runtime(store, fake_cf({('POST', GQL): HttpError(403, GQL)}))))

    # when
    off = box(store).call('cloudflare_graphql', {'query': QUERY})
    no_rights = denied.call('cloudflare_graphql', {'query': QUERY, 'variables': VARS})
    answers = [b.call('cloudflare_graphql', {'query': QUERY, 'variables': VARS})[1] for _ in range(7)]

    # then
    assert off[1] is True
    assert no_rights == ('Cloudflare GraphQL HTTP 403; the token lacks the read permission of this dataset (Analytics Read for zone traffic)', True)
    assert answers == [False] * 6 + [True]
    assert TOKEN not in json.dumps([b.calls, list(store.activity.find({}, {'_id': 0}))], default=str)


def test_a_refused_query_costs_a_call_of_the_run(store: Store):
    # given
    b = tool_box(store, fake_cf({('POST', GQL): {'data': DATA}}))

    # when
    refused = [b.call('cloudflare_graphql', {'query': 'mutation { x }'})[1] for _ in range(3)]
    answers = [b.call('cloudflare_graphql', {'query': QUERY, 'variables': VARS})[1] for _ in range(4)]

    # then
    assert refused == [True] * 3 and answers == [False] * 3 + [True]


def test_a_declared_variable_left_out_or_an_empty_zone_filter_is_refused_before_a_call_and_names_the_zone_tags(store: Store):
    # given
    http = fake_cf({('POST', GQL): {'data': DATA}})
    b = tool_box(store, http, main={'zones': f'{SHOP}, {BLOG}, gone.example.com'})
    zone_only = 'query($zone: String!) { viewer { zones(filter: {zoneTag: $zone}) { httpRequestsAdaptiveGroups(limit: 15) { count } } } }'
    optional = 'query($zone: String!, $host: String) { viewer { zones(filter: {zoneTag: $zone}) { httpRequestsAdaptiveGroups(limit: 15) { count } } } }'
    account_only = 'query($account: String!) { viewer { accounts(filter: {accountTag: $account}) { x } } }'

    # when
    no_vars = b.call('cloudflare_graphql', {'query': zone_only})
    some = b.call('cloudflare_graphql', {'query': QUERY, 'variables': {'z': VARS['z'], 'a': '', 'b': None}})
    empty = b.call('cloudflare_graphql', {'query': '{ viewer { zones(filter: {zoneTag: ""}) { httpRequestsAdaptiveGroups(limit: 5) { count } } } }'})
    account = b.call('cloudflare_graphql', {'query': account_only})
    kept = b.call('cloudflare_graphql', {'query': optional, 'variables': {'zone': VARS['z']}})

    # then
    assert no_vars[1] and no_vars[0].startswith('the query declares $zone but variables gives no value; send variables {"zone": ')
    assert f'zone tags: {SHOP} = {SHOP_ID}, {BLOG} = {BLOG_ID}, gone.example.com = not found; account tag: {ACCOUNT}' in no_vars[0]
    assert some[1] and 'declares $a, $b but' in some[0] and 'zone tags' not in some[0]
    assert empty[1] and empty[0].startswith('the zone filter is empty') and f'{SHOP} = {SHOP_ID}' in empty[0]
    assert account[1] and f'account tag: {ACCOUNT}' in account[0]
    assert not kept[1]
    assert len(gql_posts(http)) == 1


def test_a_failed_tag_lookup_still_refuses_the_query_with_the_reason(store: Store):
    # given
    http = fake_cf({('GET', ZONES): HttpError(403, ZONES), ('POST', GQL): {'data': DATA}})
    b = tool_box(store, http)

    # when
    text, err = b.call('cloudflare_graphql', {'query': '{ viewer { zones(filter: {zoneTag: ""}) { x } } }'})

    # then
    assert err and text.startswith('the zone filter is empty') and f'zones {SHOP} (their tags could not be looked up: Cloudflare API zones HTTP 403; the token lacks Zone Read)' in text
    assert gql_posts(http) == []


def test_cloudflare_s_own_empty_zone_error_comes_back_with_the_zone_tags(store: Store):
    # given
    http = fake_cf({('POST', GQL): {'data': None, 'errors': [{'message': 'invalid zone filter: empty zone filters not allowed'}]}})

    # when
    text, err = tool_box(store, http).call('cloudflare_graphql', {'query': QUERY, 'variables': VARS})

    # then
    assert err and text.startswith(f'Cloudflare GraphQL: invalid zone filter: empty zone filters not allowed; zone tags: {SHOP} = {SHOP_ID}')


STATUS_ZONE = {'zoneTag': SHOP_ID,
               'codes': [{'count': 900, 'dimensions': {'datetimeFiveMinutes': '2026-10-17T07:55:00Z', 'edgeResponseStatus': 200}},
                         {'count': 12, 'dimensions': {'datetimeFiveMinutes': '2026-10-17T07:55:00Z', 'edgeResponseStatus': 502}},
                         {'count': 800, 'dimensions': {'datetimeFiveMinutes': '2026-10-17T07:50:00Z', 'edgeResponseStatus': 200}}],
               'cache': [{'count': 700, 'dimensions': {'datetimeFiveMinutes': '2026-10-17T07:55:00Z', 'cacheStatus': 'hit'}},
                         {'count': 212, 'dimensions': {'datetimeFiveMinutes': '2026-10-17T07:55:00Z', 'cacheStatus': 'dynamic'}},
                         {'count': 800, 'dimensions': {'datetimeFiveMinutes': '2026-10-17T07:50:00Z', 'cacheStatus': 'hit'}}]}
TOP_ZONE = {'zoneTag': SHOP_ID,
            'codes': [{'count': 30, 'dimensions': {'edgeResponseStatus': 502}}, {'count': 5, 'dimensions': {'edgeResponseStatus': 500}}],
            'paths': [{'count': 28, 'dimensions': {'clientRequestPath': '/garden/chairs/oak'}}, {'count': 7, 'dimensions': {'clientRequestPath': '/'}}],
            'countries': [{'count': 33, 'dimensions': {'clientCountryName': 'JP'}}, {'count': 2, 'dimensions': {'clientCountryName': 'DE'}}]}
ASN_ZONE = {'zoneTag': SHOP_ID, 'asns': [{'count': 29, 'dimensions': {'clientAsn': 64500, 'clientASNDescription': 'EXAMPLE-NET'}},
                                         {'count': 6, 'dimensions': {'clientAsn': 64501, 'clientASNDescription': 'OTHER-NET'}}]}


def zone_answer(zone: dict) -> dict:
    return {'data': {'viewer': {'zones': [zone]}}}


def edge_fake(asn=None, over: dict | None = None) -> FakeHttp:
    def gql(_, body):
        q = body['query']
        if 'asns:' in q:
            return asn if asn is not None else zone_answer(ASN_ZONE)
        if 'paths:' in q:
            return zone_answer(TOP_ZONE)
        return zone_answer(STATUS_ZONE)
    return fake_cf({('POST', GQL): gql, **(over or {})})


def edge_call(store: Store, http: FakeHttp, **args):
    text, err = tool_box(store, http, main={'zones': f'{SHOP}, {BLOG}, {TOOLS}'}).call('edge', args)
    return (text if err else json.loads(text)), err


def test_edge_is_a_read_source_tool_with_fixed_parameters(store: Store):
    # when
    tool = next(t for t in cloudflare.PLUGIN.tools if t.name == 'edge')
    props = tool.input_schema['properties']
    offered = next(t for t in tool_box(store, edge_fake()).definitions() if t['name'] == 'edge')

    # then
    assert tool.scan and tool.kind == 'read'
    assert props['op']['enum'] == ['status', 'top'] and set(props) == {'op', 'host', 'path', 'minutes', 'status'}
    assert props['minutes'] == {'type': 'integer', 'minimum': 5, 'maximum': 10080}
    assert tool.input_schema['required'] == ['op', 'host'] and tool.input_schema['additionalProperties'] is False
    assert set(offered['input_schema']['properties']) == {'op', 'host', 'path', 'minutes', 'status', 'instance'}
    assert SHOP in offered['description'] and '{field:zones}' in tool.description


def test_without_a_cloudflare_instance_edge_is_off(store: Store):
    # when
    no_instance = box(store).call('edge', {'op': 'status', 'host': SHOP})

    # then
    assert no_instance[1] is True and 'edge' not in [t['name'] for t in box(store).definitions()]


def test_status_counts_each_five_minute_bucket_by_status_and_cache_newest_first(store: Store):
    # given
    http = edge_fake()

    # when
    out, err = edge_call(store, http, op='status', host='API.shop.example.com', path='/v2/orders', minutes=12)

    # then
    assert err is False
    body = gql_posts(http)[0][3]
    assert body['variables'] == {'z': [SHOP_ID], 'a': '2026-10-17T07:45:00Z', 'b': '2026-10-17T08:00:00Z', 'host': 'api.shop.example.com', 'path': '/v2/orders%'}
    assert 'datetimeFiveMinutes edgeResponseStatus' in body['query'] and 'datetimeFiveMinutes cacheStatus' in body['query']
    assert 'clientRequestPath_like:$path' in body['query'] and 'clientRequestHTTPHost:$host' in body['query']
    assert out['zone'] == SHOP and out['host'] == 'api.shop.example.com' and out['path'] == '/v2/orders'
    assert (out['from'], out['to'], out['requests']) == ('2026-10-17T07:45:00Z', '2026-10-17T08:00:00Z', 1712)
    assert out['status'] == {'200': 1700, '502': 12} and out['cache'] == {'hit': 1500, 'dynamic': 212}
    assert out['buckets'] == [{'at': '2026-10-17T07:55:00Z', 'requests': 912, 'status': {'200': 900, '502': 12}, 'cache': {'hit': 700, 'dynamic': 212}},
                              {'at': '2026-10-17T07:50:00Z', 'requests': 800, 'status': {'200': 800}, 'cache': {'hit': 800}}]


def test_top_lists_codes_paths_asns_and_countries_for_a_status_class(store: Store):
    # given
    http = edge_fake()

    # when
    out, err = edge_call(store, http, op='top', host=SHOP, status='5XX')

    # then
    assert err is False
    main, asn = gql_posts(http)[0][3], gql_posts(http)[1][3]
    assert 'edgeResponseStatus_geq:500, edgeResponseStatus_lt:600' in main['query'] and 'clientRequestPath_like' not in main['query']
    assert 'clientASNDescription' not in main['query'] and 'clientASNDescription' in asn['query']
    assert main['variables'] == {'z': [SHOP_ID], 'a': '2026-10-17T07:00:00Z', 'b': '2026-10-17T08:00:00Z', 'host': SHOP}
    assert out['status'] == '5xx' and out['requests'] == 35 and out['codes'] == {'500': 5, '502': 30}
    assert out['paths'] == [{'path': '/garden/chairs/oak', 'requests': 28}, {'path': '/', 'requests': 7}]
    assert out['asns'] == [{'asn': 64500, 'name': 'EXAMPLE-NET', 'requests': 29}, {'asn': 64501, 'name': 'OTHER-NET', 'requests': 6}]
    assert out['countries'] == [{'country': 'JP', 'requests': 33}, {'country': 'DE', 'requests': 2}]


def test_one_code_is_its_own_range_and_a_refused_asn_field_keeps_the_rest(store: Store):
    # given
    http = edge_fake(asn={'errors': [{'message': 'zone does not have access to the field clientASNDescription'}]})

    # when
    out, err = edge_call(store, http, op='top', host=f'www.{BLOG}', status='429', minutes=1440)

    # then
    assert err is False
    assert 'edgeResponseStatus_geq:429, edgeResponseStatus_lt:430' in gql_posts(http)[0][3]['query']
    assert gql_posts(http)[0][3]['variables']['z'] == [BLOG_ID] and out['zone'] == BLOG
    assert out['asns'] == 'not available on this zone: Cloudflare GraphQL answered an error'
    assert out['paths'][0]['requests'] == 28


def test_no_query_asks_for_client_ips_query_strings_or_user_agents(store: Store):
    # given
    http = edge_fake()

    # when
    edge_call(store, http, op='status', host=SHOP)
    edge_call(store, http, op='top', host=SHOP, status='4xx', path='/stores')

    # then
    queries = ' '.join(c[3]['query'] for c in gql_posts(http))
    assert len(gql_posts(http)) == 3
    assert 'clientIP' not in queries and 'clientRequestQuery' not in queries and 'userAgent' not in queries


def test_the_window_is_capped_per_op(store: Store):
    # given
    http = edge_fake()

    # when
    status, _ = edge_call(store, http, op='status', host=SHOP, minutes=10080)
    top, _ = edge_call(store, http, op='top', host=SHOP, status='5xx', minutes=10080)
    over, err = edge_call(store, http, op='top', host=SHOP, status='5xx', minutes=99999)

    # then
    assert status['from'] == '2026-10-17T02:00:00Z'
    assert top['from'] == '2026-10-10T08:00:00Z'
    assert (over, err) == ('minutes must be at most 10080, got 99999', True)


@pytest.mark.parametrize('args, why', [
    ({'op': 'status', 'host': 'example.com'}, f'host must be a host name under {SHOP}, {BLOG}, {TOOLS}'),
    ({'op': 'status', 'host': 'evilshop.example.com'}, 'host must be a host name under'),
    ({'op': 'status', 'host': f'{SHOP}", x: "y'}, 'host must be a host name under'),
    ({'op': 'status', 'host': SHOP, 'path': '/a?b=1'}, 'path must be a path prefix'),
    ({'op': 'status', 'host': SHOP, 'path': 'cards'}, 'path must be a path prefix'),
    ({'op': 'status', 'host': SHOP, 'path': '/a%'}, 'path must be a path prefix'),
    ({'op': 'top', 'host': SHOP}, 'status is required'),
    ({'op': 'top', 'host': SHOP, 'status': '6xx'}, 'status must be 5xx, 4xx or one code such as 429'),
    ({'op': 'top', 'host': SHOP, 'status': '5xx; {'}, 'status must be 5xx, 4xx or one code such as 429'),
    ({'op': 'graphql', 'host': SHOP}, 'op must be one of status, top, got "graphql"'),
])
def test_bad_parameters_are_refused_before_any_cloudflare_call(store: Store, args, why):
    # given
    http = edge_fake()

    # when
    text, err = tool_box(store, http, main={'zones': f'{SHOP}, {BLOG}, {TOOLS}'}).call('edge', args)

    # then
    assert err and why in text
    assert http.calls == []


def test_a_cloudflare_failure_comes_back_as_a_tool_error(store: Store):
    # given
    http = fake_cf({('POST', GQL): HttpError(401, GQL)})
    unseen = fake_cf({('GET', ZONES): lookup()})
    no_zones = fake_cf({('GET', ZONES): HttpError(403, ZONES)})

    # when
    text, err = tool_box(store, http).call('edge', {'op': 'status', 'host': SHOP})
    hidden = tool_box(store, unseen).call('edge', {'op': 'status', 'host': SHOP})
    lookup_refused = tool_box(store, no_zones).call('edge', {'op': 'status', 'host': SHOP})

    # then
    assert err and text == 'Cloudflare GraphQL HTTP 401; the token is invalid or lacks Analytics Read'
    assert hidden == (f'the token cannot see the zone {SHOP}', True) and gql_posts(unseen) == []
    assert lookup_refused == ('Cloudflare API zones HTTP 403; the token lacks Zone Read', True)


def test_tool_descriptions_state_the_contract_the_code_enforces():
    # given
    desc = {t.name: t.description for t in cloudflare.PLUGIN.tools}

    # then
    assert 'sampled' in desc['edge'] and '5-minute' in desc['edge'] and 'client IPs' in desc['edge'] and 'query strings' in desc['edge'] and 'UTC' in desc['edge']
    assert '4,000 characters' in desc['cloudflare_graphql'] and '15,000 characters' in desc['cloudflare_graphql'] and 'At most 6 calls per run' in desc['cloudflare_graphql']
    assert 'zone tag' in desc['cloudflare_graphql'] and 'account tag' in desc['cloudflare_graphql']


def test_a_token_that_straddles_the_message_cut_is_masked_before_the_cut(store: Store):
    # given
    echoed = {'errors': [{'message': 'x' * 85 + TOKEN + ' was refused'}]}
    http = fake_cf({('POST', GQL): echoed})
    lookup_refused = fake_cf({('GET', ZONES): {'success': False, 'errors': [{'message': 'y' * 87 + TOKEN}]}})

    # when
    text, err = tool_box(store, http).call('edge', {'op': 'status', 'host': SHOP})
    hidden, refused = tool_box(store, lookup_refused).call('edge', {'op': 'status', 'host': SHOP})

    # then
    assert err and refused
    assert text.startswith('Cloudflare GraphQL answered an error: ' + 'x' * 85 + '***') and TOKEN[:5] not in text
    assert hidden.startswith('Cloudflare API zones answered an error: ' + 'y' * 87 + '***') and TOKEN[:5] not in hidden


def test_a_test_that_used_its_budget_says_the_service_did_not_answer_in_time(store: Store, monkeypatch):
    # given
    http = fake_cf()
    monkeypatch.setattr(cf_client, 'TEST_BUDGET_S', 0)

    # when
    late = connection_of(cf_runtime(store, http))

    # then
    assert (late.ok, late.summary) == (False, msg('connection.timeout'))
    assert http.calls == []


def hours_answer(by_tag: dict):
    def answer(_, body):
        if 'httpRequests1hGroups' in body['query']:
            return {'data': {'viewer': {'zones': [{'zoneTag': t, 'httpRequests1hGroups': b} for t, b in by_tag.items()]}}}
        return {'data': {'viewer': {'zones': []}}}
    return answer


def failing_hour(now: float, codes: dict[int, int]) -> list[dict]:
    return [bucket(checks.edge_hour(now) - dt.timedelta(hours=1), 610_000, codes)]


def test_a_refused_health_check_read_keeps_its_cards_and_raises_an_access_card(store: Store):
    # given
    http = fake_cf()
    health, access = f'cf-health|cloudflare/api-origin|{SHOP}', 'no-access|-/cf|health-checks'

    # when
    with app_with(store, http, main={'zones': f'{SHOP}, {BLOG}'}) as c:
        sweep(c, 'cf')
        before = cards_of(store)
        http.routes[('GET', CHECKS)] = HttpError(403, CHECKS, DENIED_BODY)
        http.routes[('GET', CERTS)] = listing([[]])
        sweep(c, 'cf', now=NOW + 900)
        during, refused = cards_of(store), store.load()
        http.routes[('GET', CHECKS)] = listing([[HEALTH[1]]])
        sweep(c, 'cf', now=NOW + 1800)
        back = store.load()['sources']['cf']
    after = cards_of(store)

    # then
    assert before[health]['status'] == 'open' and during[health]['status'] == 'open'
    assert during[f'cf-cert|cloudflare/advanced|{SHOP}|a-1']['status'] == 'cleared'
    assert refused['sources']['cf'] == f'ok {MID} no access to health checks'
    card = during[access]
    assert (card['status'], card['sev'], card['ranKey'], card['label']) == ('open', 'warn', 'cf:access:health-checks', 'Cloudflare cannot read health checks: the token lacks Health Checks Read')
    assert card['short'] == 'WARN Cloudflare cannot read health checks: the token lacks Health Checks Read' and SHOP in card['what'] and BLOG not in card['what']
    assert part(refused['cloudflare'], 'summary')[0]['noAccess'] == [{'part': 'health checks', 'permission': 'Health Checks Read', 'zones': [SHOP], 'invalid': False}]
    assert back == 'ok' and after[health]['status'] == 'cleared' and after[access]['status'] == 'cleared'


def test_a_refused_zone_lookup_keeps_every_zone_card_and_still_reads_the_status_page(store: Store):
    # given
    http = fake_cf()

    # when
    with app_with(store, http, main={'status-components': SEEN}) as c:
        sweep(c, 'cf')
        http.routes[('GET', ZONES)] = HttpError(403, ZONES, DENIED_BODY)
        sweep(c, 'cf', now=NOW + 900)
        state = store.load()
    cards = cards_of(store)

    # then
    assert state['sources']['cf'] == f'ok {MID} no access to zones'
    assert cards[f'cf-health|cloudflare/api-origin|{SHOP}']['status'] == 'open' and cards[f'cf-cert|cloudflare/advanced|{SHOP}|a-1']['status'] == 'open'
    assert cards['no-access|-/cf|zones']['label'] == 'Cloudflare cannot read zones: the token lacks Zone Read' and cards['no-access|-/cf|zones']['status'] == 'open'
    assert cards[f'cf-status|cloudflare/{TOKYO}']['status'] == 'open' and part(state['cloudflare'], 'components')


def test_a_refused_analytics_read_keeps_the_edge_cards_and_raises_an_access_card(store: Store):
    # given
    http = fake_cf()
    conn, access = f'edge|{SHOP}|edge-conn', 'no-access|-/edge|analytics'
    authz = {'data': None, 'errors': [{'message': 'not authorized for that zone', 'extensions': {'code': 'authz'}}]}

    # when
    with app_with(store, http, main={}) as c:
        sweep(c, 'edge', now=NOW + 360)
        http.routes[('POST', GQL)] = HttpError(403, GQL)
        sweep(c, 'edge', now=NOW + 1260)
        refused, during = store.load()['sources']['edge'], cards_of(store)
        http.routes[('POST', GQL)] = authz
        sweep(c, 'edge', now=NOW + 2160)
        denied = store.load()['sources']['edge']
        http.routes[('POST', GQL)] = hours_answer({SHOP_ID: failing_hour(NOW + 3060, {200: 610_000})})
        sweep(c, 'edge', now=NOW + 3060)
    after = cards_of(store)

    # then
    assert refused == denied == f'ok {MID} no access to analytics'
    assert during[conn]['status'] == 'open' and during[access]['status'] == 'open'
    assert (during[access]['ranKey'], during[access]['label']) == ('edge:access:analytics', 'Cloudflare cannot read analytics: the token lacks Analytics Read')
    assert after[access]['status'] == 'cleared' and after[conn]['status'] == 'cleared'


def test_a_zone_without_edge_data_keeps_its_cards_while_other_zones_clear(store: Store):
    # given
    now = NOW + 360
    bad = {200: 600_000, 502: 3500, 522: 100}
    http = fake_cf({('POST', GQL): hours_answer({SHOP_ID: failing_hour(now, bad), BLOG_ID: failing_hour(now, bad)})})

    # when
    with app_with(store, http, main={'zones': f'{SHOP}, {BLOG}'}) as c:
        sweep(c, 'edge', now=now)
        before = cards_of(store)
        http.routes[('POST', GQL)] = hours_answer({BLOG_ID: failing_hour(now, {200: 610_000})})
        sweep(c, 'edge', now=now)
        state = store.load()
    cards = cards_of(store)

    # then
    assert before[f'edge|{SHOP}|edge-conn']['status'] == before[f'edge|{BLOG}|edge-conn']['status'] == 'open'
    assert state['sources']['edge'] == f'ok {MID} no data for {SHOP}'
    assert cards[f'edge|{SHOP}|edge-conn']['status'] == 'open' and cards[f'edge|{BLOG}|edge-conn']['status'] == 'cleared'


def test_a_zone_removed_from_the_field_closes_its_cards(store: Store):
    # given
    http = fake_cf({('GET', BLOG_CHECKS): listing([[{'name': 'blog-origin', 'address': BLOG, 'http_config': {'path': '/'}, 'status': 'unhealthy', 'suspended': False}]])})

    # when
    with app_with(store, http, main={'zones': f'{SHOP}, {BLOG}'}) as c:
        sweep(c, 'cf')
        sweep(c, 'edge', now=NOW + 360)
        c.app.state.runtime.instances.update('cloudflare', 'main', {'zones': BLOG}, USER)
        c.app.state.runtime.refresh()
        sweep(c, 'cf', now=NOW + 900)
        sweep(c, 'edge', now=NOW + 1260)
    cards = cards_of(store)

    # then
    assert cards[f'cf-health|cloudflare/api-origin|{SHOP}']['status'] == 'cleared' and cards[f'cf-cert|cloudflare/advanced|{SHOP}|a-1']['status'] == 'cleared'
    assert cards[f'edge|{SHOP}|edge-conn']['status'] == 'cleared'
    assert cards[f'cf-health|cloudflare/blog-origin|{BLOG}']['status'] == 'open'


def test_switching_the_status_check_off_closes_its_cards_even_when_the_lists_are_empty(store: Store):
    # given
    http = fake_cf()

    # when
    with app_with(store, http, main={'status-components': SEEN}) as c:
        sweep(c, 'cf')
        http.routes.update({('GET', CHECKS): listing([[]]), ('GET', CERTS): listing([[]])})
        c.app.state.runtime.instances.update('cloudflare', 'main', {'status-components': ''}, USER)
        c.app.state.runtime.refresh()
        sweep(c, 'cf', now=NOW + 900)
        state = store.load()
    cards = cards_of(store)

    # then
    assert cards['cf-incident|mxthd6onaplz']['status'] == 'cleared' and cards[f'cf-status|cloudflare/{TOKYO}']['status'] == 'cleared'
    assert state['sources']['cf'] == 'ok'


def test_watched_components_missing_from_the_status_page_keep_their_cards(store: Store):
    # given
    http = fake_cf()

    # when
    with app_with(store, http, main={'status-components': SEEN}) as c:
        sweep(c, 'cf')
        http.routes[('GET', STATUS)] = {'components': [{'name': OSAKA, 'status': 'operational'}], 'incidents': []}
        sweep(c, 'cf', now=NOW + 900)
        state = store.load()
    cards = cards_of(store)

    # then
    assert state['sources']['cf'] == f'ok {MID} no data for {TOKYO}, CDN/Cache, WAF'
    assert cards['cf-incident|mxthd6onaplz']['status'] == 'open' and cards[f'cf-status|cloudflare/{TOKYO}']['status'] == 'open'


def test_two_packs_of_one_type_get_their_own_cards():
    # given
    packs = [{'id': 'a-1', 'type': 'advanced', 'status': 'pending_validation', 'hosts': ['x.shop.example.com'], 'certificates': []},
             {'id': 'a-2', 'type': 'advanced', 'status': 'active', 'hosts': ['y.shop.example.com'], 'certificates': [{'expires_on': days(2)}]}]
    http = fake_cf({('GET', CERTS): listing([packs])})

    # when
    got = checks.cf_pass(Cloudflare(TOKEN, http), [SHOP_Z], [], NOW)
    certs = {x['key']: x['sev'] for x in got.found if x['sig'] == 'cf-cert'}

    # then
    assert certs == {f'cf-cert|cloudflare/advanced|{SHOP}|a-1': 'warn', f'cf-cert|cloudflare/advanced|{SHOP}|a-2': 'crit'}
    assert [r['id'] for r in part(got.rows, 'certs')] == ['a-1', 'a-2']


def test_connection_test_fails_naming_a_missing_permission(store: Store):
    # given
    cases = [
        ({('GET', CHECKS): HttpError(403, CHECKS, DENIED_BODY)}, f'Cloudflare cannot read health checks of {SHOP}: the token lacks Health Checks Read'),
        ({('GET', CERTS): {'success': False, 'errors': [{'code': 9109, 'message': 'Unauthorized to access requested resource'}]}},
         f'Cloudflare cannot read certificates of {SHOP}: the token lacks SSL and Certificates Read'),
        ({('POST', GQL): HttpError(403, GQL)}, f'Cloudflare cannot read analytics of {SHOP}: the token lacks Analytics Read'),
        ({('GET', ZONES): HttpError(403, ZONES, DENIED_BODY)}, 'Cloudflare cannot read zones: the token lacks Zone Read'),
    ]
    absent = fake_cf({('GET', CHECKS): HttpError(404, CHECKS)})

    # when
    got = [connection_of(cf_runtime(store, fake_cf(over))) for over, _ in cases]
    plain = connection_of(cf_runtime(store, absent))

    # then
    assert [(r.ok, r.summary) for r in got] == [(False, text) for _, text in cases]
    assert (plain.ok, plain.summary) == (True, f'Cloudflare token active {MID} zones: {SHOP}')


def test_a_test_with_an_unsuccessful_answer_says_what_failed(store: Store):
    # given
    http = fake_cf({('GET', ZONES): {'success': False, 'errors': [{'code': 1000, 'message': 'Invalid zone name'}]}})

    # when
    got = connection_of(cf_runtime(store, http))

    # then
    assert (got.ok, got.summary) == (False, 'Cloudflare API zones answered an error')


def test_an_account_owned_token_is_verified_through_its_account(store: Store):
    # given
    account_token = fake_cf({('GET', VERIFY): HttpError(401, VERIFY), ('GET', ACCOUNT_VERIFY): {'success': True, 'result': {'id': 'tok-2', 'status': 'active'}}})
    pinned_verify = f'{API}/accounts/{OTHER_ACCOUNT}/tokens/verify'
    pinned = fake_cf({('GET', VERIFY): HttpError(403, VERIFY), ('GET', pinned_verify): {'success': True, 'result': {'id': 'tok-3', 'status': 'active'}}})
    invalid = fake_cf({('GET', VERIFY): HttpError(401, VERIFY), ('GET', ZONES): HttpError(401, ZONES)})

    # when
    owned = connection_of(cf_runtime(store, account_token))
    chosen = connection_of(cf_runtime(store, pinned, main={'account': OTHER_ACCOUNT}))
    rejected = connection_of(cf_runtime(store, invalid))

    # then
    assert (owned.ok, owned.summary) == (True, f'Cloudflare token active {MID} zones: {SHOP}') and account_token.hits(ACCOUNT_VERIFY)
    assert (chosen.ok, chosen.summary) == (True, f'Cloudflare token active {MID} zones: {SHOP}') and pinned.hits(pinned_verify)
    assert (rejected.ok, rejected.summary) == (False, msg('connection.rejected', status=401))


def test_a_token_at_the_graphql_message_cut_is_masked(store: Store):
    # given
    http = fake_cf({('POST', GQL): {'data': None, 'errors': [{'message': 'z' * 195 + TOKEN + ' is refused'}]}})

    # when
    text, err = tool_box(store, http).call('cloudflare_graphql', {'query': QUERY, 'variables': VARS})

    # then
    assert err and text.startswith('Cloudflare GraphQL: ' + 'z' * 195 + '***') and TOKEN[:5] not in text


def test_asns_say_what_happened_when_the_asn_query_is_not_answered(store: Store):
    # given
    down = edge_fake(asn=Unreachable('x'))
    broken = edge_fake(asn=HttpError(500, GQL))

    # when
    unreachable, _ = edge_call(store, down, op='top', host=SHOP, status='5xx')
    failed, _ = edge_call(store, broken, op='top', host=SHOP, status='5xx')

    # then
    assert unreachable['asns'] == 'ASN query not read: Cloudflare GraphQL unreachable'
    assert failed['asns'] == 'ASN query not read: Cloudflare GraphQL HTTP 500'


def test_a_test_with_many_zones_on_a_slow_connection_checks_one_zone_per_part_and_passes(store: Store):
    # given
    names = [f'z{i}.example.com' for i in range(16)]
    many = [Zone(n, f'{i + 1:032x}', ACCOUNT) for i, n in enumerate(names)]
    first = many[0].id
    fake = fake_cf({('GET', ZONES): lookup(*many), ('GET', f'{API}/zones/{first}/healthchecks'): listing([[]]), ('GET', f'{API}/zones/{first}/ssl/certificate_packs'): listing([[]])})

    def slow(method, url, headers, data, timeout):
        time.sleep(0.3)
        return fake(method, url, headers, data, timeout)

    # when
    started = time.monotonic()
    got = connection_of(cf_runtime(store, slow, main={'zones': ', '.join(names)}))
    took = time.monotonic() - started

    # then
    assert (got.ok, got.summary) == (True, f'Cloudflare token active {MID} zones: ' + ', '.join(names))
    assert took < 12 and len(fake.calls) == 1 + len(names) + 3
    assert [c[1].split('?')[0] for c in fake.calls[-3:]] == [f'{API}/zones/{first}/healthchecks', f'{API}/zones/{first}/ssl/certificate_packs', GQL]


def test_an_invalid_token_is_named_as_invalid_or_lacking_the_permission(store: Store):
    # given
    http = fake_cf()
    revoked = '{"success": false, "errors": [{"code": 10000, "message": "Authentication error"}]}'

    # when
    with app_with(store, http, main={}) as c:
        http.routes[('GET', ZONES)] = HttpError(401, ZONES)
        sweep(c, 'cf')
        invalid = cards_of(store)['no-access|-/cf|zones']['label']
        store.anomalies.delete_many({})
        store.save_state({'anomalies': [], 'initialized': True}, None)
        http.routes[('GET', ZONES)] = lookup(*KNOWN)
        http.routes[('GET', CHECKS)] = HttpError(403, CHECKS, revoked)
        sweep(c, 'cf')
        authentication = cards_of(store)['no-access|-/cf|health-checks']['label']
        store.anomalies.delete_many({})
        store.save_state({'anomalies': [], 'initialized': True}, None)
        http.routes[('GET', CHECKS)] = HttpError(403, CHECKS, DENIED_BODY)
        sweep(c, 'cf')
        lacking = cards_of(store)['no-access|-/cf|health-checks']['label']

    # then
    assert invalid == 'Cloudflare cannot read zones: the token is invalid or lacks Zone Read'
    assert authentication == 'Cloudflare cannot read health checks: the token is invalid or lacks Health Checks Read'
    assert lacking == 'Cloudflare cannot read health checks: the token lacks Health Checks Read'


def test_a_failed_status_page_never_blinds_the_zone_checks(store: Store):
    # given
    http = fake_cf()
    health = f'cf-health|cloudflare/api-origin|{SHOP}'

    # when
    with app_with(store, http, main={'status-components': SEEN}) as c:
        sweep(c, 'cf')
        before = cards_of(store)
        http.routes[('GET', STATUS)] = HttpError(503, STATUS)
        result = sweep(c, 'cf', now=NOW + 900)
        state = store.load()
    cards = cards_of(store)

    # then
    assert before[f'cf-status|cloudflare/{TOKYO}']['status'] == 'open'
    assert state['sources']['cf'] == 'error: status: Cloudflare status page HTTP 503'
    assert cards[health]['status'] == 'open' and cards[f'cf-status|cloudflare/{TOKYO}']['status'] == 'open'
    assert part(state['cloudflare'], 'checks') and part(state['cloudflare'], 'certs')


def test_one_zone_that_fails_keeps_only_its_own_cards_and_the_other_zones_are_judged(store: Store):
    # given
    http = fake_cf()
    shop = f'cf-health|cloudflare/api-origin|{SHOP}'
    docs = f'cf-health|cloudflare/docs-origin|{DOCS}'

    # when
    with app_with(store, http, main={'zones': f'{SHOP}, {DOCS}'}) as c:
        sweep(c, 'cf')
        before = cards_of(store)
        http.routes[('GET', CHECKS)] = HttpError(500, CHECKS)
        http.routes[('GET', DOCS_CHECKS)] = listing([[]])
        sweep(c, 'cf', now=NOW + 900)
        state = store.load()
    cards = cards_of(store)

    # then
    assert before[shop]['status'] == before[docs]['status'] == 'open'
    assert state['sources']['cf'] == 'error: health checks: Cloudflare API healthchecks HTTP 500'
    assert cards[shop]['status'] == 'open' and cards[docs]['status'] == 'cleared'


def test_cloudflare_lists_an_access_key_only_for_a_part_it_read_or_was_refused(store: Store):
    # given
    runtime = cf_runtime(store, fake_cf({('GET', ZONES): HttpError(403, ZONES, DENIED_BODY)}), main={'status-components': SEEN})
    failing = cf_runtime(store, fake_cf({('GET', CHECKS): HttpError(500, CHECKS)}))

    # when
    refused = cloudflare.run_cf(runtime.context(cloudflare.PLUGIN, runtime.instances.get('cloudflare', 'main'), NOW)).result
    broken = cloudflare.run_cf(failing.context(cloudflare.PLUGIN, failing.instances.get('cloudflare', 'main'), NOW)).result

    # then
    assert 'cf:access:zones' in refused.ran and 'cf:access:health-checks' not in refused.ran and 'cf:access:certificates' not in refused.ran
    assert 'cf:access:health-checks' not in broken.ran and {'cf:access:zones', 'cf:access:certificates', f'cf:{SHOP}:certs'} <= set(broken.ran)
    assert f'cf:{SHOP}:checks' not in broken.ran and broken.ok


def test_a_refused_analytics_read_with_an_invalid_token_says_so(store: Store):
    # given
    runtime = cf_runtime(store, fake_cf({('POST', GQL): HttpError(401, GQL)}))

    # when
    out = cloudflare.run_edge(runtime.context(cloudflare.PLUGIN, runtime.instances.get('cloudflare', 'main'), NOW)).result

    # then
    [card] = out.found
    assert card['label'] == 'Cloudflare cannot read analytics: the token is invalid or lacks Analytics Read'
    assert (card['key'], card['ranKey'], card['sig']) == ('no-access|-/edge|analytics', 'edge:access:analytics', 'no-access')


def test_the_cloudflare_test_skips_the_parts_only_switched_off_checks_read(store: Store):
    # given
    refusing = fake_cf({('POST', GQL): HttpError(403, GQL), ('GET', CHECKS): HttpError(403, CHECKS, DENIED_BODY)})

    def tested(off: set[str]):
        runtime = cf_runtime(store, refusing)
        ctx = runtime.context(cloudflare.PLUGIN, runtime.instances.get('cloudflare', 'main'), NOW)
        ctx.instance = dataclasses.replace(ctx.instance, off_checks=frozenset(off))
        return cloudflare.test(ctx)

    # when
    both, no_edge, no_cf, neither = tested(set()), tested({'edge'}), tested({'cf'}), tested({'cf', 'edge'})

    # then
    assert (both.ok, no_edge.ok) == (False, False) and 'health checks' in no_edge.summary
    assert (no_cf.ok, no_cf.summary) == (False, f'Cloudflare cannot read analytics of {SHOP}: the token lacks Analytics Read')
    assert (neither.ok, neither.summary) == (True, f'Cloudflare token active {MID} zones: {SHOP}')
