import dataclasses

from fastapi.testclient import TestClient

from skopax import remote
from skopax.app import create_app
from skopax.contract import AgentRead, Capabilities, Field, TestResult
from skopax.messages import msg
from skopax.plugins import kubernetes
from skopax.registry import Registry
from tests.conftest import AUTH, settings
from tests.kube_kit import ECHO_FIELDS, ECHO_KEYS, FakeKube, echo_run, echo_sources, echoer
from tests.plugins.base import minimal
from tests.test_admin_plugins import DEMO, GOOD, HERE, SEEN

AGENT = {'mode': 'agent', 'url': 'https://agent.example.com', 'token': 'agent-token-0001', 'echo.url': 'https://echo.example.com', 'echo.token': 'echo-token-0002'}


def refuse(method, url, headers, data, timeout):
    raise remote.Unreachable(f'{url} unreachable (test)')


def test_the_admin_form_and_routes_take_every_field_an_agent_instance_accepts(store):
    # given
    registry = Registry.of(kubernetes.PLUGIN, echoer())

    with TestClient(create_app(settings(), store, registry=registry, kube=FakeKube(), transport=refuse)) as c:
        # when
        row = {p['id']: p for p in c.get('/api/admin/plugins', headers=AUTH).json()['plugins']}['kubernetes']
        tested = c.post('/api/admin/plugins/kubernetes/test', json={'values': AGENT}, headers=AUTH)
        added = c.post('/api/admin/plugins/kubernetes/instances', json={'name': 'east', 'values': AGENT, 'enabled': False}, headers=AUTH)
        edited = c.patch('/api/admin/plugins/kubernetes/instances/east', json={'values': {'echo.url': 'https://echo-two.example.com', 'echo.token': 'echo-token-0003'}}, headers=AUTH)
        stored = c.app.state.instances.get('kubernetes', 'east')

    # then
    assert [f['key'] for f in row['fields']] == ['mode', 'url', 'token', 'via', 'echo.url', 'echo.token']
    assert [f['kind'] for f in row['fields'][-2:]] == ['url', 'secret']
    assert tested.status_code == 200 and set(tested.json()) == {'ok', 'summary', 'ms'}
    assert (added.status_code, edited.status_code) == (201, 200)
    assert stored.values['echo.url'] == 'https://echo-two.example.com' and stored.secrets['echo.token'] == 'echo-token-0003'


def test_a_plugins_own_agent_read_fields_are_not_fields_of_its_own_instances(store):
    # given
    read = dataclasses.replace(DEMO.agent_reads[0], keys=DEMO.agent_reads[0].keys + ('ns',), fields=(Field('ns', 'Namespace', 'text'),))
    plugin = dataclasses.replace(DEMO, agent_reads=(read,))
    SEEN.clear()

    with TestClient(create_app(settings(), store, registry=Registry.of(plugin, directory=HERE))) as c:
        # when
        tested = c.post('/api/admin/plugins/demo/test', json={'values': {**GOOD, 'ns': 'shop'}}, headers=AUTH)
        plain = c.post('/api/admin/plugins/demo/test', json={'values': GOOD}, headers=AUTH)
        fields = [f['key'] for f in c.app.state.runtime.fields_of('demo')]

    # then
    assert tested.status_code == 422 and 'ns' in tested.text
    assert plain.status_code == 200 and fields == ['url', 'token', 'ca', 'mode']


SEEN_CONFIG: list[dict] = []


def config_probe(ctx):
    SEEN_CONFIG.append(dict(ctx.instance.config))
    return TestResult(True, 'probed', 1)


def zoned_echoer():
    zone = Field('echo.zone', 'Echo zone', 'text', default='eu-west')
    read = AgentRead('echoer-read', ('text',), ECHO_KEYS + (r'echo\.zone',), echo_sources, echo_run, fields=ECHO_FIELDS + (zone,))
    return minimal('echoer', 'Echoer', capabilities=Capabilities(secrets=('echo.token',), agent_reads=True), agent_reads=(read,))


def probed(store):
    SEEN_CONFIG.clear()
    registry = Registry.of(dataclasses.replace(kubernetes.PLUGIN, test=config_probe), zoned_echoer())
    return TestClient(create_app(settings(), store, registry=registry, kube=FakeKube(), transport=refuse))


def test_the_test_button_answers_with_the_agents_own_summary(store):
    # given
    registry = Registry.of(kubernetes.PLUGIN, echoer())

    with TestClient(create_app(settings(), store, registry=registry, kube=FakeKube(), transport=refuse)) as c:
        # when
        tested = c.post('/api/admin/plugins/kubernetes/test', json={'values': AGENT}, headers=AUTH)

    # then
    answer = tested.json()
    assert tested.status_code == 200 and set(answer) == {'ok', 'summary', 'ms'}
    assert answer['ok'] is False and answer['summary'] == 'the new agent is unreachable'
    assert isinstance(answer['ms'], int) and answer['ms'] >= 0


def test_the_test_instance_holds_every_agent_read_value_and_agent_read_defaults(store):
    # given
    with probed(store) as c:
        # when
        tested = c.post('/api/admin/plugins/kubernetes/test', json={'values': AGENT}, headers=AUTH)

    # then
    assert tested.json() == {'ok': True, 'summary': 'probed', 'ms': 1}
    assert SEEN_CONFIG == [{'mode': 'agent', 'url': 'https://agent.example.com', 'token': 'agent-token-0001', 'echo.url': 'https://echo.example.com', 'echo.token': 'echo-token-0002', 'echo.zone': 'eu-west'}]


def test_a_moved_agent_address_asks_for_every_stored_secret_by_its_own_label(store):
    # given
    with probed(store) as c:
        c.post('/api/admin/plugins/kubernetes/instances', json={'name': 'east', 'values': AGENT, 'enabled': False}, headers=AUTH)

        # when
        moved = c.post('/api/admin/plugins/kubernetes/test', json={'name': 'east', 'values': {'url': 'https://moved.example.com', 'token': 'agent-token-0009'}}, headers=AUTH)
        bare = c.post('/api/admin/plugins/kubernetes/test', json={'name': 'east', 'values': {'url': 'https://moved.example.com'}}, headers=AUTH)

    # then
    assert bare.status_code == 422 and bare.json() == {'detail': msg('integrations.reenter', field='Token'), 'field': 'token'}
    assert moved.status_code == 422
    assert moved.json() == {'detail': msg('integrations.reenter', field='Echo token'), 'field': 'echo.token'}
    assert SEEN_CONFIG == []


def test_a_moved_agent_read_address_asks_only_for_the_secrets_of_that_plugin(store):
    # given
    with probed(store) as c:
        c.post('/api/admin/plugins/kubernetes/instances', json={'name': 'east', 'values': AGENT, 'enabled': False}, headers=AUTH)

        # when
        moved = c.post('/api/admin/plugins/kubernetes/test', json={'name': 'east', 'values': {'echo.url': 'https://echo-two.example.com'}}, headers=AUTH)
        typed = c.post('/api/admin/plugins/kubernetes/test', json={'name': 'east', 'values': {'echo.url': 'https://echo-two.example.com', 'echo.token': 'echo-token-0005'}}, headers=AUTH)

    # then
    assert moved.status_code == 422
    assert moved.json() == {'detail': msg('integrations.reenter', field='Echo token'), 'field': 'echo.token'}
    assert typed.json() == {'ok': True, 'summary': 'probed', 'ms': 1}
    assert [(s['token'], s['echo.url'], s['echo.token']) for s in SEEN_CONFIG] == [('agent-token-0001', 'https://echo-two.example.com', 'echo-token-0005')]
