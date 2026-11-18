import mongomock
import pytest

from skopax.contract import SourceData
from skopax.instances import Instances, SourceOwned, Writer, agent_fields
from skopax.messages import msg
from skopax.plugins import elasticsearch, kubernetes
from skopax.registry import Registry
from skopax.sealer import Sealer
from skopax.settingsources import SourceSet
from tests.conftest import AUTH
from tests.test_admin_plugins import listed, served

KEY = 'owner-key'
EAST = {'plugin': 'kubernetes', 'name': 'east', 'mode': 'agent', 'url': 'https://east.example.com', 'token': 'source-token-0021'}
LOGS = 'https://logs.example.com'


def sourced(rows):
    registry = Registry.of(kubernetes.PLUGIN, elasticsearch.PLUGIN)
    feed = {'rows': tuple(rows)}
    instances = Instances(mongomock.MongoClient(tz_aware=True)['owners'], registry, lambda: Sealer(KEY), sources=lambda: [SourceData('config file', {}, feed['rows'])],
                          extra_fields=lambda plugin_id: agent_fields(registry, plugin_id))
    instances.refresh()
    return instances, feed


def test_a_source_owns_only_the_fields_it_supplies_and_takes_a_field_back_once_it_supplies_it():
    # given
    instances, feed = sourced([EAST])
    writer = Writer(instances)
    before = instances.get('kubernetes', 'east').owned

    # when
    with pytest.raises(SourceOwned) as address:
        writer.put('kubernetes', 'east', {'url': 'https://elsewhere.example.net'}, {}, 'migration')
    with pytest.raises(SourceOwned) as relay:
        instances.update('kubernetes', 'east', {'via': 'hub'}, 'alice@example.com')
    writer.put('kubernetes', 'east', {'elastic.logging-url': LOGS}, {'elastic.logging-api-key': 'logs-key-0022'}, 'migration')
    free = instances.get('kubernetes', 'east')
    feed['rows'] = ({**EAST, 'elastic.logging-url': 'https://source-logs.example.com'},)
    instances.refresh()
    taken = instances.get('kubernetes', 'east')

    # then
    assert 'elastic.logging-url' not in before and 'elastic.apm-url' not in before and {'url', 'token', 'via'} <= before
    assert address.value.field == 'url' and relay.value.field == 'via'
    assert free.values['elastic.logging-url'] == LOGS and free.secrets['elastic.logging-api-key'] == 'logs-key-0022'
    assert 'elastic.logging-url' in taken.owned and taken.values['elastic.logging-url'] == 'https://source-logs.example.com'
    with pytest.raises(SourceOwned):
        writer.put('kubernetes', 'east', {'elastic.logging-url': LOGS}, {}, 'migration')


def test_admin_shows_an_unsupplied_agent_read_url_as_editable_and_a_supplied_one_as_read_only(store):
    # given
    feed = SourceData('config file', {}, (EAST,))
    sources = SourceSet([('config file', lambda: feed)], {'config file': feed})

    with served(store, registry=Registry.of(kubernetes.PLUGIN, elasticsearch.PLUGIN), sources=sources) as c:
        # when
        saved = c.patch('/api/admin/plugins/kubernetes/instances/east', json={'values': {'elastic.logging-url': LOGS}}, headers=AUTH)
        moved = c.patch('/api/admin/plugins/kubernetes/instances/east', json={'values': {'url': 'https://elsewhere.example.net'}}, headers=AUTH)
        row = {i['name']: i for i in listed(c)['kubernetes']['instances']}['east']

    # then
    assert saved.status_code == 200, saved.text
    assert moved.status_code == 409 and moved.json()['detail'] == msg('instances.source_owned', field='URL', source='config file')
    assert 'elastic.logging-url' not in row['owned'] and 'url' in row['owned'] and 'token' in row['owned']
    assert row['values']['elastic.logging-url'] == LOGS


def test_a_source_that_gives_only_an_agent_read_secret_owns_the_agents_own_address_and_relay(store):
    # given
    from skopax.plugins import langfuse
    row = {'plugin': 'kubernetes', 'name': 'east', 'mode': 'agent', 'url': 'https://east.example.com', 'elastic.logging-api-key': 'logs-key-0023'}
    registry = Registry.of(kubernetes.PLUGIN, elasticsearch.PLUGIN, langfuse.PLUGIN)
    feed = SourceData('config file', {}, (row,))
    instances = Instances(mongomock.MongoClient(tz_aware=True)['owners'], registry, lambda: Sealer(KEY), sources=lambda: [feed],
                          extra_fields=lambda plugin_id: agent_fields(registry, plugin_id))
    instances.refresh()
    instances.update('kubernetes', 'east', {'token': 'admin-token-0027'}, 'alice@example.com')
    writer = Writer(instances)
    sources = SourceSet([('config file', lambda: feed)], {'config file': feed})

    # when
    refused = []
    for values in ({'url': 'https://elsewhere.example.net'}, {'via': 'hub'}):
        with pytest.raises(SourceOwned) as owned:
            writer.put('kubernetes', 'east', values, {}, 'migration')
        refused.append(owned.value.field)
    writer.put('kubernetes', 'east', {'langfuse.url': 'https://langfuse.example.com'}, {}, 'migration')
    with served(store, registry=registry, sources=sources) as c:
        c.patch('/api/admin/plugins/kubernetes/instances/east', json={'values': {'token': 'admin-token-0027'}}, headers=AUTH)
        moved = c.patch('/api/admin/plugins/kubernetes/instances/east', json={'values': {'url': 'https://elsewhere.example.net'}}, headers=AUTH)
        relayed = c.patch('/api/admin/plugins/kubernetes/instances/east', json={'values': {'via': 'hub'}}, headers=AUTH)
        free = c.patch('/api/admin/plugins/kubernetes/instances/east', json={'values': {'langfuse.url': 'https://langfuse.example.com'}}, headers=AUTH)
        row_view = {i['name']: i for i in listed(c)['kubernetes']['instances']}['east']

    # then
    assert refused == ['url', 'via']
    assert instances.get('kubernetes', 'east').values['langfuse.url'] == 'https://langfuse.example.com'
    assert (moved.status_code, relayed.status_code, free.status_code) == (409, 409, 200), (moved.text, relayed.text, free.text)
    assert {'url', 'via', 'elastic.logging-api-key'} <= set(row_view['owned']) and 'langfuse.url' not in row_view['owned']
