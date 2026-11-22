import base64
import json
from types import SimpleNamespace

import mongomock
import pytest

from skopax.app import TEST_TIMEOUT_S as CORE_TEST_TIMEOUT_S
from skopax.contract import ReadError
from skopax.instances import Instances, PluginState
from skopax.messages import msg
from skopax.plugins import confluence
from skopax.plugins.confluence import client as wiki_client
from skopax.plugins.confluence.client import GATEWAY, Confluence
from skopax.registry import BUILTIN, Registry, describe
from skopax.remote import HttpError, Reply, Unreachable
from skopax.runtime import Runtime
from skopax.sealer import Sealer
from tests.fake_http import FakeHttp
from tests.plugin_kit import KIT_KEY, add_instance
from tests.test_confluence_tool import EMAIL, SITE, TENANT_INFO, TOKEN, fake, fresh_cloud_ids, wiki

NOW = 1_800_000_000.0
SPACE_LIST = f'{GATEWAY}/cid-1/wiki/api/v2/spaces'
SEARCH = f'{GATEWAY}/cid-1/wiki/rest/api/search'
BASIC = 'Basic ' + base64.b64encode(f'{EMAIL}:{TOKEN}'.encode()).decode()


def connection_of(http: FakeHttp):
    db = mongomock.MongoClient(tz_aware=True)['wiki']
    registry = Registry.of(confluence.PLUGIN)
    runtime = Runtime(registry, PluginState(db), Instances(db, registry, lambda: Sealer(KIT_KEY)), http)
    add_instance(SimpleNamespace(db=db), confluence.PLUGIN, 'main', sealer_key=KIT_KEY, primary=True, url=SITE, email=EMAIL, spaces='DOCS', **{'api-token': TOKEN})
    runtime.refresh()
    return confluence.PLUGIN.test(runtime.context(confluence.PLUGIN, runtime.instances.get('confluence', 'main'), NOW))


def test_confluence_identity_and_manifest():
    # given
    plugin = confluence.PLUGIN

    # when
    manifest = describe(plugin, icon=True)
    loaded = Registry.load({}, entry_points=False).get('confluence')

    # then
    assert json.loads(json.dumps(manifest, allow_nan=False)) == manifest
    assert 'skopax.plugins.confluence' in BUILTIN and loaded.state == 'ok'
    assert (manifest['id'], manifest['name'], manifest['version'], manifest['category'], manifest['author'], manifest['licence'], manifest['core'], manifest['edition']) == (
        'confluence', 'Confluence', '0.1.0', 'other', 'Skopax', 'Apache-2.0', '>=0.1,<0.6', 'community')
    assert manifest['capabilities'] == {'hosts': ['@url', 'api.atlassian.com'], 'secrets': ['api-token'], 'agentReads': False}
    assert manifest['uses'] == [] and manifest['maxInstances'] == 1 and manifest['checks'] == [] and manifest['rules'] == [] and manifest['settings'] == []
    assert [(f['key'], f['kind'], f['required'], f['ui']) for f in manifest['fields']] == [('url', 'url', True, True), ('email', 'text', True, False),
                                                                                          ('api-token', 'secret', True, False), ('spaces', 'text', False, False)]
    assert 'empty' in manifest['fields'][3]['help'].lower() and 'nothing' in manifest['fields'][3]['help']
    assert [(t['name'], t['kind']) for t in manifest['tools']] == [('confluence', 'read')] and manifest['knowledge'] == ['confluence']
    assert plugin.description.endswith('.') and plugin.description.count('.') == 1
    assert Registry.of(plugin).icon('confluence').startswith('<svg') and len(plugin.knowledge[0].text.splitlines()) <= 40


def test_confluence_connection_test_reports_ok_and_failures():
    # given
    good = FakeHttp({('GET', TENANT_INFO): {'cloudId': 'cid-1'}, ('GET', SPACE_LIST): {'results': [{'key': 'DOCS'}], 'size': 1}, ('GET', SEARCH): {'results': []}})
    refused = FakeHttp({('GET', TENANT_INFO): {'cloudId': 'cid-1'}, ('GET', SPACE_LIST): HttpError(401, SPACE_LIST)})
    down = FakeHttp({('GET', TENANT_INFO): Unreachable('x')})
    no_cloud = FakeHttp({('GET', TENANT_INFO): {'cloud': 'none'}})

    # when
    results = [connection_of(http) for http in (good, refused, down, no_cloud)]

    # then
    assert [(r.ok, r.summary) for r in results[:3]] == [(True, 'Confluence at example.atlassian.net'), (False, msg('connection.rejected', status=401)),
                                                       (False, msg('connection.unreachable'))]
    assert (results[3].ok, results[3].summary) == (False, 'tenant_info answered without a cloudId')
    assert all(isinstance(r.ms, int) and r.ms >= 0 for r in results)
    assert [c[1] for c in good.calls] == [TENANT_INFO, f'{SPACE_LIST}?keys=DOCS&limit=1', f'{SEARCH}?cql=type+%3D+page+AND+space+in+%28%22DOCS%22%29&limit=1']
    assert good.calls[1][2]['Authorization'] == good.calls[2][2]['Authorization'] == BASIC
    assert 'Authorization' not in good.calls[0][2]


def test_the_connection_test_shares_one_deadline_across_its_calls(monkeypatch):
    # given
    now = [100.0]
    monkeypatch.setattr(confluence, 'monotonic', lambda: now[0])

    def slow(seconds: float):
        seen: list = []

        def send(method, url, headers, data, timeout):
            seen.append((url.split('?')[0], timeout))
            if url == TENANT_INFO:
                now[0] += seconds
                return Reply({'cloudId': 'cid-1'}, {})
            return Reply({'results': [{'key': 'DOCS'}]}, {})

        return send, seen

    quick, quick_seen = slow(5)
    late, late_seen = slow(confluence.TEST_BUDGET_S + 1)

    # when
    ok = connection_of(quick)
    over = connection_of(late)

    # then
    assert confluence.TEST_BUDGET_S < CORE_TEST_TIMEOUT_S
    assert ok.ok and quick_seen == [(TENANT_INFO, confluence.TEST_BUDGET_S), (SPACE_LIST, confluence.TEST_BUDGET_S - 5), (SEARCH, confluence.TEST_BUDGET_S - 5)]
    assert (over.ok, over.summary) == (False, msg('connection.timeout')) and late_seen == [(TENANT_INFO, confluence.TEST_BUDGET_S)]


def test_confluence_defaults_are_neutral():
    # when
    defaults = {f.key: f.default for f in confluence.PLUGIN.fields}

    # then
    assert defaults == {'url': '', 'email': '', 'api-token': '', 'spaces': ''}
    assert {s.key: s.default for s in confluence.PLUGIN.settings} == {}


def test_the_cloud_id_is_looked_up_once_per_site_and_the_test_always_asks(fresh_cloud_ids):
    # given
    http = fake()

    # when
    wiki(http).search('runbook', None)
    wiki(http).search('runbook', None)
    connection_of(http)
    other = Confluence('https://other.atlassian.net', EMAIL, TOKEN, ('DOCS',), FakeHttp({('GET', 'https://other.atlassian.net/_edge/tenant_info'): {'cloudId': 'cid-2'},
                                                                                       ('GET', f'{GATEWAY}/cid-2/wiki/rest/api/search'): {'results': []}}))
    other.search('runbook', None)

    # then
    assert len(http.hits(TENANT_INFO)) == 2 and len(http.hits(SEARCH)) == 2
    assert wiki_client.CLOUD_IDS == {SITE: 'cid-1', 'https://other.atlassian.net': 'cid-2'}


def test_a_failed_cloud_id_lookup_is_a_fixed_read_error_and_is_not_cached(fresh_cloud_ids):
    # given
    refused = FakeHttp({('GET', TENANT_INFO): HttpError(404, TENANT_INFO)})
    down = FakeHttp({('GET', TENANT_INFO): Unreachable('secret-host.internal unreachable')})

    # when
    errors = [pytest.raises(ReadError, wiki(http).search, 'runbook', None).value for http in (refused, down)]

    # then
    assert [(e.status, e.message) for e in errors] == [(502, 'Confluence cloud id: HTTP 404 from tenant_info'), (502, 'Confluence could not be reached')]
    assert wiki_client.CLOUD_IDS == {}
