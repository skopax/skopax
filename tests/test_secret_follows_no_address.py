import dataclasses
from pathlib import Path

import mongomock
import pytest
from fastapi.testclient import TestClient

from skopax import remote
from skopax.app import create_app
from skopax.contract import Capabilities, Field, SourceData
from skopax.instances import Instances, SourceOwned, address_owners, agent_fields, effective_fields, moved_addresses
from skopax.messages import msg
from skopax.plugins import elasticsearch, grafana, kubernetes, prometheus, website
from skopax.registry import Registry
from skopax.sealer import Sealer
from tests.conftest import AUTH, settings
from tests.kube_kit import FakeKube, echoer
from tests.plugins import demo_plugin
from tests.plugins.base import minimal
from tests.test_instances import Box, pem

AGENT = {'mode': 'agent', 'url': 'https://agent.example.com', 'token': 'agent-token-0001', 'echo.url': 'https://echo.example.com', 'echo.token': 'echo-token-0002'}


def refuse(method, url, headers, data, timeout):
    raise remote.Unreachable(f'{url} unreachable (test)')


def admin(store, *plugins) -> TestClient:
    return TestClient(create_app(settings(), store, registry=Registry.of(*plugins), kube=FakeKube(), transport=refuse))


def patch(c: TestClient, plugin: str, name: str, values: dict):
    return c.patch(f'/api/admin/plugins/{plugin}/instances/{name}', json={'values': values}, headers=AUTH)


def test_a_grafana_patch_that_moves_the_address_asks_for_the_token_again(store):
    # given
    with admin(store, prometheus.PLUGIN, grafana.PLUGIN) as c:
        c.post('/api/admin/plugins/grafana/instances', json={'name': 'main', 'values': {'url': 'https://grafana.example.com', 'token': 'glsa_one'}, 'enabled': False}, headers=AUTH)

        # when
        bare = patch(c, 'grafana', 'main', {'url': 'https://grafana-two.example.com'})
        kept = c.app.state.instances.get('grafana', 'main')
        same = patch(c, 'grafana', 'main', {'url': 'https://grafana.example.com', 'skip-datasources': 'Loki'})
        typed = patch(c, 'grafana', 'main', {'url': 'https://grafana-two.example.com', 'token': 'glsa_two'})
        moved = c.app.state.instances.get('grafana', 'main')

    # then
    assert bare.status_code == 422 and bare.json() == {'detail': msg('integrations.reenter_secret'), 'field': 'token'}
    assert (kept.values['url'], kept.secrets['token']) == ('https://grafana.example.com', 'glsa_one')
    assert same.status_code == 200
    assert typed.status_code == 200 and (moved.values['url'], moved.secrets['token']) == ('https://grafana-two.example.com', 'glsa_two')


def test_a_prometheus_patch_asks_for_the_shown_secret_and_drops_a_hidden_one(store):
    # given
    first = {'url': 'https://prom.example.com', 'auth': 'bearer', 'token': 'token-one', 'password': 'password-one'}
    with admin(store, prometheus.PLUGIN) as c:
        c.post('/api/admin/plugins/prometheus/instances', json={'name': 'main', 'values': first, 'enabled': False}, headers=AUTH)

        # when
        ui = patch(c, 'prometheus', 'main', {'ui-url': 'https://prom-ui.example.com'})
        bare = patch(c, 'prometheus', 'main', {'url': 'https://prom-two.example.com'})
        before = c.app.state.instances.get('prometheus', 'main')
        typed = patch(c, 'prometheus', 'main', {'url': 'https://prom-two.example.com', 'token': 'token-two'})
        moved = c.app.state.instances.get('prometheus', 'main')

    # then
    assert ui.status_code == 200
    assert bare.status_code == 422 and bare.json() == {'detail': msg('integrations.reenter_secret'), 'field': 'token'}
    assert dict(before.secrets) == {'token': 'token-one', 'password': 'password-one'}
    assert typed.status_code == 200 and moved.values['url'] == 'https://prom-two.example.com' and dict(moved.secrets) == {'token': 'token-two'}


def test_a_moved_agent_or_agent_read_address_asks_for_the_secrets_that_go_there(store):
    # given
    with admin(store, kubernetes.PLUGIN, echoer()) as c:
        c.post('/api/admin/plugins/kubernetes/instances', json={'name': 'east', 'values': AGENT, 'enabled': False}, headers=AUTH)

        # when
        echo = patch(c, 'kubernetes', 'east', {'echo.url': 'https://echo-two.example.com'})
        echo_typed = patch(c, 'kubernetes', 'east', {'echo.url': 'https://echo-two.example.com', 'echo.token': 'echo-token-0005'})
        own = patch(c, 'kubernetes', 'east', {'url': 'https://moved.example.com', 'token': 'agent-token-0009'})
        own_typed = patch(c, 'kubernetes', 'east', {'url': 'https://moved.example.com', 'token': 'agent-token-0009', 'echo.token': 'echo-token-0006'})
        stored = c.app.state.instances.get('kubernetes', 'east')

    # then
    assert echo.status_code == 422 and echo.json() == {'detail': msg('integrations.reenter_secret'), 'field': 'echo.token'}
    assert echo_typed.status_code == 200
    assert own.status_code == 422 and own.json() == {'detail': msg('integrations.reenter_secret'), 'field': 'echo.token'}
    assert own_typed.status_code == 200
    assert (stored.values['url'], stored.secrets['token'], stored.values['echo.url'], stored.secrets['echo.token']) == (
        'https://moved.example.com', 'agent-token-0009', 'https://echo-two.example.com', 'echo-token-0006')


def connect(c: TestClient, plugin: str, name: str, values: dict):
    return c.post(f'/api/admin/plugins/{plugin}/test', json={'name': name, 'values': values}, headers=AUTH)


ES = {'url': 'https://es.example.com', 'api-key': 'es-key-0001', 'apm-url': 'https://apm.example.com', 'apm-api-key': 'apm-key-0002'}


def test_a_link_only_address_moves_no_secret_in_test_or_patch(store):
    # given
    first = {'url': 'https://prom.example.com', 'ui-url': 'https://prom-ui.example.com', 'auth': 'bearer', 'token': 'token-one'}
    with admin(store, prometheus.PLUGIN) as c:
        c.post('/api/admin/plugins/prometheus/instances', json={'name': 'main', 'values': first, 'enabled': False}, headers=AUTH)

        # when
        tried = connect(c, 'prometheus', 'main', {'ui-url': 'https://prom-ui-two.example.com'})
        cleared = connect(c, 'prometheus', 'main', {'ui-url': ''})
        saved = patch(c, 'prometheus', 'main', {'ui-url': 'https://prom-ui-two.example.com'})
        kept = c.app.state.instances.get('prometheus', 'main')

    # then
    assert (tried.status_code, cleared.status_code, saved.status_code) == (200, 200, 200)
    assert (kept.values['ui-url'], kept.secrets['token']) == ('https://prom-ui-two.example.com', 'token-one')


def test_a_required_address_that_is_also_a_link_still_asks_for_the_secrets_that_go_there(store):
    # given
    first = {'url': 'https://www.example.com', 'browser-url': 'http://browser.example.com:9222', 'scan-headers': 'X-Pass: one'}
    with admin(store, website.PLUGIN) as c:
        c.post('/api/admin/plugins/website/instances', json={'name': 'main', 'values': first, 'enabled': False}, headers=AUTH)

        # when
        site = patch(c, 'website', 'main', {'url': 'https://www.example.net'})
        browser = patch(c, 'website', 'main', {'browser-url': 'http://browser-two.example.com:9222'})
        kept = c.app.state.instances.get('website', 'main')
        typed = patch(c, 'website', 'main', {'url': 'https://www.example.net', 'scan-headers': 'X-Pass: two'})
        moved = c.app.state.instances.get('website', 'main')

    # then
    refused = {'detail': msg('integrations.reenter_secret'), 'field': 'scan-headers'}
    assert (site.status_code, site.json(), browser.status_code, browser.json()) == (422, refused, 422, refused)
    assert (kept.values['url'], kept.values['browser-url'], kept.secrets['scan-headers']) == ('https://www.example.com', 'http://browser.example.com:9222', 'X-Pass: one')
    assert typed.status_code == 200 and (moved.values['url'], moved.secrets['scan-headers']) == ('https://www.example.net', 'X-Pass: two')


def test_adding_a_host_that_receives_a_secret_asks_for_it_again_and_removing_one_does_not(store):
    # given
    first = {'url': 'https://www.example.com', 'hosts': 'example.net, shop.example.org', 'scan-headers': 'X-Pass: one'}
    with admin(store, website.PLUGIN) as c:
        c.post('/api/admin/plugins/website/instances', json={'name': 'main', 'values': first, 'enabled': False}, headers=AUTH)

        # when
        added = patch(c, 'website', 'main', {'hosts': 'example.net, shop.example.org, collector.example.com'})
        renamed = patch(c, 'website', 'main', {'hosts': 'example.net, store.example.org'})
        kept = c.app.state.instances.get('website', 'main')
        typed = patch(c, 'website', 'main', {'hosts': 'example.net, shop.example.org, collector.example.com', 'scan-headers': 'X-Pass: two'})
        removed = patch(c, 'website', 'main', {'hosts': 'Example.net'})
        after = c.app.state.instances.get('website', 'main')

    # then
    refused = {'detail': msg('integrations.reenter_secret'), 'field': 'scan-headers'}
    assert (added.status_code, added.json(), renamed.status_code, renamed.json()) == (422, refused, 422, refused)
    assert (kept.values['hosts'], kept.secrets['scan-headers']) == ('example.net, shop.example.org', 'X-Pass: one')
    assert typed.status_code == 200 and removed.status_code == 200, removed.json()
    assert (after.values['hosts'], after.secrets['scan-headers']) == ('Example.net', 'X-Pass: two')


TWENTY = [f'h{i}.example.net' for i in range(20)]


@pytest.mark.parametrize('stored, edited, removed', [
    ('example.net collector.example.com', 'example.net, collector.example.com', ''),
    ('example.net;collector.example.com', 'example.net, collector.example.com', ''),
    (', '.join(TWENTY + ['collector.example.com']), ', '.join(TWENTY[1:] + ['collector.example.com']), ', '.join(TWENTY)),
], ids=['joined by a space', 'joined by a semicolon', 'the 21st host'])
def test_an_edit_that_makes_a_stored_but_unused_host_receive_the_secret_asks_for_it_again(store, stored, edited, removed):
    # given
    with admin(store, website.PLUGIN) as c:
        c.post('/api/admin/plugins/website/instances', json={'name': 'main', 'values': {'url': 'https://www.example.com', 'hosts': stored}, 'enabled': False}, headers=AUTH)
        patch(c, 'website', 'main', {'scan-headers': 'X-Pass: one'})

        # when
        activated = patch(c, 'website', 'main', {'hosts': edited})
        trimmed = patch(c, 'website', 'main', {'hosts': removed})

    # then
    assert (activated.status_code, activated.json()) == (422, {'detail': msg('integrations.reenter_secret'), 'field': 'scan-headers'})
    assert trimmed.status_code == 200, 'a removal that brings no host into use asks for nothing'


def test_clearing_an_address_whose_reads_no_other_one_takes_over_moves_no_secret(store):
    # given
    first = {'url': 'https://www.example.com', 'browser-url': 'http://browser.example.com:9222', 'scan-headers': 'X-Pass: one'}
    with admin(store, website.PLUGIN) as c:
        c.post('/api/admin/plugins/website/instances', json={'name': 'main', 'values': first, 'enabled': False}, headers=AUTH)

        # when
        cleared = patch(c, 'website', 'main', {'browser-url': ''})
        kept = c.app.state.instances.get('website', 'main')
        moved = patch(c, 'website', 'main', {'browser-url': 'http://browser-two.example.com:9222'})

    # then
    assert cleared.status_code == 200 and (kept.values.get('browser-url', ''), kept.secrets['scan-headers']) == ('', 'X-Pass: one')
    assert (moved.status_code, moved.json()) == (422, {'detail': msg('integrations.reenter_secret'), 'field': 'scan-headers'}), 'a new address still asks'


def test_an_address_flag_must_fit_its_field_kind():
    # given
    from skopax.contract import Field
    bad = [dataclasses.replace(demo_plugin.PLUGIN, fields=(*demo_plugin.PLUGIN.fields, Field('names', 'Names', 'url', address='hosts'))),
           dataclasses.replace(demo_plugin.PLUGIN, fields=(*demo_plugin.PLUGIN.fields, Field('spot', 'Spot', 'text', address='alone'))),
           dataclasses.replace(demo_plugin.PLUGIN, fields=(*demo_plugin.PLUGIN.fields, Field('spot', 'Spot', 'url', address='elsewhere'))),
           dataclasses.replace(demo_plugin.PLUGIN, fields=(*demo_plugin.PLUGIN.fields, Field('names', 'Names', 'text', hosts_of=lambda value, config: []))),
           dataclasses.replace(demo_plugin.PLUGIN, fields=(*demo_plugin.PLUGIN.fields, Field('names', 'Names', 'text', address='hosts', hosts_of='host_list')))]

    # when
    found = [Registry.of(p, directory=Path(demo_plugin.__file__).parent).get(p.id) for p in bad]

    # then
    assert [f.state for f in found] == ['broken'] * 5 and all('address' in f.reason for f in found), [f.reason for f in found]


def test_a_cleared_address_whose_reads_go_to_another_one_asks_again_in_test_and_patch(store):
    # given
    with admin(store, elasticsearch.PLUGIN) as c:
        c.post('/api/admin/plugins/elasticsearch/instances', json={'name': 'main', 'values': ES, 'enabled': False}, headers=AUTH)

        # when
        tried = connect(c, 'elasticsearch', 'main', {'apm-url': ''})
        saved = patch(c, 'elasticsearch', 'main', {'apm-url': ''})
        kept = c.app.state.instances.get('elasticsearch', 'main')
        typed = patch(c, 'elasticsearch', 'main', {'apm-url': '', 'api-key': 'es-key-0003', 'apm-api-key': 'apm-key-0004'})

    # then
    assert tried.status_code == 422 and tried.json()['field'] == 'api-key'
    assert saved.status_code == 422 and saved.json() == {'detail': msg('integrations.reenter_secret'), 'field': 'api-key'}
    assert (kept.values['apm-url'], kept.secrets['apm-api-key']) == ('https://apm.example.com', 'apm-key-0002')
    assert typed.status_code == 200


def test_a_cleared_address_with_no_other_address_to_take_its_reads_moves_nothing(store):
    # given
    with admin(store, kubernetes.PLUGIN, echoer()) as c:
        c.post('/api/admin/plugins/kubernetes/instances', json={'name': 'east', 'values': AGENT, 'enabled': False}, headers=AUTH)

        # when
        tried = connect(c, 'kubernetes', 'east', {'echo.url': ''})
        saved = patch(c, 'kubernetes', 'east', {'echo.url': ''})
        kept = c.app.state.instances.get('kubernetes', 'east')

    # then
    assert (tried.status_code, saved.status_code) == (200, 200)
    assert 'echo.url' not in kept.values and kept.secrets['token'] == 'agent-token-0001'



HUB = {'mode': 'agent', 'url': 'https://agent.example.com', 'token': 'agent-token-0001', 'elastic.logging-url': 'https://logs.example.com',
       'elastic.logging-api-key': 'logs-key-0001', 'elastic.apm-url': 'https://apm.example.com', 'elastic.apm-api-key': 'apm-key-0001'}


def test_clearing_an_agent_read_address_asks_for_no_secret_but_moving_it_still_does(store):
    # given
    with admin(store, kubernetes.PLUGIN, elasticsearch.PLUGIN) as c:
        c.post('/api/admin/plugins/kubernetes/instances', json={'name': 'east', 'values': HUB, 'enabled': False}, headers=AUTH)

        # when
        tried = connect(c, 'kubernetes', 'east', {'elastic.apm-url': ''})
        cleared = patch(c, 'kubernetes', 'east', {'elastic.apm-url': ''})
        kept = c.app.state.instances.get('kubernetes', 'east')
        moved = patch(c, 'kubernetes', 'east', {'elastic.logging-url': 'https://logs-two.example.com'})

    # then
    assert (tried.status_code, cleared.status_code) == (200, 200)
    assert 'elastic.apm-url' not in kept.values
    assert (kept.secrets['elastic.logging-api-key'], kept.secrets['elastic.apm-api-key'], kept.secrets['token']) == ('logs-key-0001', 'apm-key-0001', 'agent-token-0001')
    assert moved.status_code == 422 and moved.json()['field'].startswith('elastic.')

def test_one_rule_says_which_address_moved():
    # given
    split = minimal('split', 'Split', checks=(), fields=(Field('url', 'URL', 'url', required=True), Field('apm-url', 'APM URL', 'url'), Field('ui-url', 'UI URL', 'url', ui=True),
                                                        Field('cloud', 'Cloud', 'url', default='https://cloud.example.com')))
    single = minimal('single', 'Single', checks=(), fields=(Field('url', 'URL', 'url', required=True, ui=True),))
    hub = kubernetes.PLUGIN
    fields = {f.key: f for f in split.fields}
    before = {'url': 'https://a.example.com', 'apm-url': 'https://b.example.com', 'ui-url': 'https://c.example.com', 'cloud': 'https://own.example.com'}

    def moved(plugin, given, was=before, owners=None):
        return moved_addresses(plugin, {f.key: f for f in plugin.fields} if plugin is not split else fields, owners or {}, was, given)

    # then
    assert moved(split, {'ui-url': 'https://d.example.com'}) == set() and moved(split, {'ui-url': ''}) == set()
    assert moved(single, {'url': 'https://d.example.com'}, {'url': 'https://a.example.com'}) == {'single'}
    assert moved(split, {'apm-url': ''}) == {'split'} and moved(split, {'apm-url': '', 'url': None}) == {'split'}
    assert moved(split, {'cloud': ''}) == {'split'} and moved(split, {'cloud': ''}, {**before, 'cloud': 'https://cloud.example.com'}) == set()
    assert moved(split, {'apm-url': None, 'url': 'https://a.example.com'}) == set()
    assert moved(hub, {'via': ''}, {'mode': 'agent', 'url': 'https://agent.example.com', 'via': 'west'}) == {'kubernetes'}
    assert moved(hub, {'via': ''}, {'mode': 'agent', 'via': 'west'}) == set()



def test_among_the_built_in_plugins_only_the_link_only_addresses_move_no_secret():
    # given
    registry = Registry.load({}, entry_points=False)
    exempt = set()

    # when
    for plugin in registry.usable():
        extra = agent_fields(registry, plugin.id)
        fields = {f.key: f for f in effective_fields(plugin, extra)}
        urls = [k for k, f in fields.items() if f.kind == 'url']
        before = {k: f'https://host-{i}.example.com' for i, k in enumerate(urls)}
        owners = address_owners(registry, plugin.id, extra)
        exempt |= {(plugin.id, k) for k in urls if not moved_addresses(plugin, fields, owners, before, {k: 'https://moved.example.com'})}

    # then
    assert exempt == {('elasticsearch', 'ui-url'), ('prometheus', 'ui-url')}

def test_the_mcp_tools_plugin_keeps_its_own_rule():
    # given
    own = minimal('mcp-tools', 'MCP server', checks=(), capabilities=Capabilities(hosts=('@url',), secrets=('header-value',)), fields=(Field('url', 'URL', 'url', required=True), Field('header-value', 'Header value', 'secret')))
    db = mongomock.MongoClient(tz_aware=True)['skopax']
    instances = Instances(db, Registry.of(own, directory=Path(demo_plugin.__file__).parent), lambda: Sealer('k'))
    instances.create('mcp-tools', 'docs', {'url': 'https://mcp.example.com', 'header-value': 'secret-one'}, True, 'alice')

    # when
    moved = instances.update('mcp-tools', 'docs', {'url': 'https://mcp-two.example.com'}, 'alice')

    # then
    assert (moved.values['url'], moved.secrets['header-value']) == ('https://mcp-two.example.com', 'secret-one')


def test_a_settings_source_instance_keeps_its_own_rule():
    # given
    db = mongomock.MongoClient(tz_aware=True)['skopax']
    owned = Box(db, demo_plugin.PLUGIN, sources=[SourceData('config file', {}, ({'plugin': 'demo', 'name': 'main', 'url': 'https://demo.example.com', 'token': 'src-token-0001'},))])
    plain_db = mongomock.MongoClient(tz_aware=True)['skopax']
    plain = Box(plain_db, demo_plugin.PLUGIN, sources=[SourceData('config file', {}, ({'plugin': 'demo', 'name': 'main', 'url': 'https://demo.example.com'},))])
    plain.instances.update('demo', 'main', {'token': 'admin-token-0001'}, 'alice')

    # when
    with pytest.raises(SourceOwned) as refused:
        owned.instances.update('demo', 'main', {'url': 'https://two.example.com'}, 'alice')
    moved = plain.instances.update('demo', 'main', {'url': 'https://two.example.com'}, 'alice')

    # then
    assert refused.value.field == 'url'
    assert (moved.source, moved.values['url'], moved.secrets['token']) == ('config file', 'https://two.example.com', 'admin-token-0001')


@pytest.mark.parametrize('first, login', [
    ({'hosts': 'example.com, collector.example.org', 'login-hosts': 'collector.example.org'}, ''),
    ({'hosts': 'example.com, collector.example.org', 'login-hosts': 'example.org'}, 'login.example.com'),
], ids=['the same host', 'a parent of the host'])
def test_removing_a_login_host_that_brings_a_stored_host_into_use_asks_for_the_headers_again(store, first, login):
    # given
    with admin(store, website.PLUGIN) as c:
        c.post('/api/admin/plugins/website/instances', json={'name': 'main', 'values': {'url': 'https://www.example.com', **first}, 'enabled': False}, headers=AUTH)
        added = patch(c, 'website', 'main', {'scan-headers': 'X-Pass: one'})

        # when
        tried = connect(c, 'website', 'main', {'login-hosts': login})
        saved = patch(c, 'website', 'main', {'login-hosts': login})
        kept = c.app.state.instances.get('website', 'main')
        typed = patch(c, 'website', 'main', {'login-hosts': login, 'scan-headers': 'X-Pass: two'})

    # then
    refused = {'detail': msg('integrations.reenter_secret'), 'field': 'scan-headers'}
    assert added.status_code == 200
    assert (tried.status_code, tried.json()['field'], saved.status_code, saved.json()) == (422, 'scan-headers', 422, refused)
    assert (kept.values['login-hosts'], kept.secrets['scan-headers']) == (first['login-hosts'], 'X-Pass: one')
    assert typed.status_code == 200


def test_changing_a_login_host_that_covers_no_listed_host_asks_for_nothing(store):
    # given
    first = {'url': 'https://www.example.com', 'hosts': 'example.com', 'login-hosts': 'login.example.net', 'scan-headers': 'X-Pass: one'}
    with admin(store, website.PLUGIN) as c:
        c.post('/api/admin/plugins/website/instances', json={'name': 'main', 'values': first, 'enabled': False}, headers=AUTH)

        # when
        cleared = patch(c, 'website', 'main', {'login-hosts': ''})
        covered = patch(c, 'website', 'main', {'login-hosts': 'example.com'})
        kept = c.app.state.instances.get('website', 'main')

    # then
    assert (cleared.status_code, covered.status_code) == (200, 200)
    assert kept.secrets['scan-headers'] == 'X-Pass: one'


def test_a_host_parser_that_raises_counts_a_pure_removal_as_a_change(store):
    # given
    def broken(value, config):
        raise ValueError('cannot parse')

    shop = dataclasses.replace(website.PLUGIN, fields=tuple(dataclasses.replace(f, hosts_of=broken) if f.key == 'hosts' else f for f in website.PLUGIN.fields))
    first = {'url': 'https://www.example.com', 'hosts': 'example.net, shop.example.org', 'scan-headers': 'X-Pass: one'}
    with admin(store, shop) as c:
        c.post('/api/admin/plugins/website/instances', json={'name': 'main', 'values': first, 'enabled': False}, headers=AUTH)

        # when
        removed = patch(c, 'website', 'main', {'hosts': 'example.net'})

    # then
    assert (removed.status_code, removed.json()) == (422, {'detail': msg('integrations.reenter_secret'), 'field': 'scan-headers'})


GRAFANA = {'url': 'https://grafana.example.com', 'token': 'glsa_one'}


def test_a_trust_change_asks_for_the_token_again_in_test_and_patch(store):
    # given
    first, second = pem(), pem()
    with admin(store, prometheus.PLUGIN, grafana.PLUGIN) as c:
        c.post('/api/admin/plugins/grafana/instances', json={'name': 'main', 'values': {**GRAFANA, 'ca': first, 'ca-pinned': 'true'}, 'enabled': False}, headers=AUTH)

        # when
        same = patch(c, 'grafana', 'main', {'ca': first.replace('\n', '\r\n') + '\n', 'ca-pinned': 'true'})
        tried = connect(c, 'grafana', 'main', {'ca': second})
        leaf = patch(c, 'grafana', 'main', {'ca': second})
        unpinned = patch(c, 'grafana', 'main', {'ca-pinned': 'false'})
        cleared = patch(c, 'grafana', 'main', {'ca': ''})
        kept = c.app.state.instances.get('grafana', 'main')
        typed = patch(c, 'grafana', 'main', {'ca-pinned': 'false', 'token': 'glsa_two'})
        pinned = patch(c, 'grafana', 'main', {'ca-pinned': 'true'})
        added = patch(c, 'grafana', 'main', {'ca': second})

    # then
    refused = {'detail': msg('integrations.reenter_trust'), 'field': 'token'}
    label = next(f.label for f in grafana.PLUGIN.fields if f.key == 'token')
    assert same.status_code == 200, same.json()
    assert (tried.status_code, tried.json()) == (422, {'detail': msg('integrations.reenter_trust_test', field=label), 'field': 'token'})
    assert [(r.status_code, r.json()) for r in (leaf, unpinned, cleared)] == [(422, refused)] * 3
    assert (kept.values['ca-pinned'], kept.secrets['token']) == ('true', 'glsa_one')
    assert typed.status_code == 200 and pinned.status_code == 200
    assert (added.status_code, added.json()) == (422, refused)


@pytest.mark.parametrize('login', ['www.example.com', 'example.com'], ids=['the url host', 'a parent of the url host'])
def test_removing_the_url_host_from_the_login_hosts_asks_for_the_headers_again(store, login):
    # given
    with admin(store, website.PLUGIN) as c:
        made = c.post('/api/admin/plugins/website/instances', json={'name': 'main', 'values': {'url': 'https://www.example.com', 'login-hosts': login}, 'enabled': False}, headers=AUTH)
        patch(c, 'website', 'main', {'scan-headers': 'X-Pass: one'})

        # when
        tried = connect(c, 'website', 'main', {'login-hosts': ''})
        saved = patch(c, 'website', 'main', {'login-hosts': ''})
        kept = c.app.state.instances.get('website', 'main')

    # then
    assert made.status_code == 201, made.json()
    assert (tried.status_code, tried.json()['field']) == (422, 'scan-headers')
    assert (saved.status_code, saved.json()['field']) == (422, 'scan-headers')
    assert (kept.values['login-hosts'], kept.secrets['scan-headers']) == (login, 'X-Pass: one')
