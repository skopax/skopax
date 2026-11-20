import dataclasses
import re
from contextlib import contextmanager
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from skopax.app import create_app
from skopax.contract import Capabilities, Field, RuleSet
from skopax.integrationview import FIELD_KEYS, INSTANCE_KEYS, ROW_KEYS, RUN_KEYS, bare_url, reader_row
from skopax.messages import msg
from skopax.promqlchecks import clean
from skopax.registry import Registry
from skopax.users import Users
from tests.conftest import AUTH, settings
from tests.plugin_kit import add_instance
from tests.plugins import demo_plugin
from tests.plugins.base import minimal
from tests.test_promql_checks_api import BASE as CHECK
from tests.test_read_gate import walk

HERE = Path(demo_plugin.__file__).parent
PASSWORD = 'a long test password'
SEAL = 'a test sealing phrase'
TOKEN_WORD = 'plain-word-token-41'
PEM_WORD = 'plain-word-pem-42'
USER_WORD = 'plain-word-user-43'
QUERY_WORD = 'plain-word-query-44'
HEADER_WORD = 'plain-word-header-45'
HOOK_WORD = 'plain-word-hook-46'
PATH_WORD = 'plain-word-path-47'
BASIC_WORD = 'cGxhaW4td29yZC00OA'
KEYED_WORD = 'plain-word-keyed-49'
PREFIXED = ('xoxb-plain-word-50', 'ghp_plainword51', 'glpat-plainword52')
BARE_WORD = 'plain-word-bare-53'
SEEDED = (TOKEN_WORD, PEM_WORD, USER_WORD, QUERY_WORD, HEADER_WORD, HOOK_WORD, PATH_WORD, BASIC_WORD, KEYED_WORD, BARE_WORD) + PREFIXED
ROW_EXPECTED = frozenset({'id', 'name', 'version', 'category', 'description', 'author', 'licence', 'origin', 'state', 'reason', 'enabled', 'icon', 'edition', 'companyKnowledge',
                          'knowledgeAlso', 'setup', 'checks', 'tools', 'panels', 'fields', 'instances', 'health', 'rules'})
INSTANCE_EXPECTED = frozenset({'name', 'primary', 'enabled', 'source', 'problem', 'values', 'secrets', 'offChecks'})
FIELD_EXPECTED = frozenset({'key', 'label', 'kind', 'required', 'default', 'help', 'choices', 'choiceLabels', 'showIf', 'group'})
RUN_EXPECTED = frozenset({'state', 'reason', 'at', 'ranAt', 'series', 'read', 'skipped', 'noBaseline', 'firing', 'cards'})
PEM_TEXT = f'-----BEGIN PRIVATE KEY-----\n{PEM_WORD}\n-----END PRIVATE KEY-----'
FIELDS = (
    Field('url', 'URL', 'url', required=True),
    Field('token', 'Token', 'secret'),
    Field('ca', 'Client key', 'pem'),
    Field('header-value', 'Header value', 'text'),
    Field('namespaces', 'Namespaces', 'text'),
    Field('mode', 'Mode', 'choice', choices=('fast', 'slow')),
    Field('hook', 'Webhook URL', 'url'),
    Field('endpoint', 'Endpoint', 'url'),
)
VAULT = dataclasses.replace(minimal('vault', 'Vault demo'), fields=FIELDS, capabilities=Capabilities(hosts=('*.example.com',), secrets=('token',)), rules=(RuleSet('lag', 'Lag', {'warn': 100.0, 'crit': 500.0}),))
GIVEN = {'url': f'https://vault.example.com/api?team=shop&token={QUERY_WORD}', 'token': TOKEN_WORD,
         'header-value': HEADER_WORD, 'namespaces': 'shop,checkout', 'mode': 'slow', 'hook': f'https://hooks.example.com/services/{HOOK_WORD}',
         'endpoint': f'https://mcp.example.com/s/{PATH_WORD}/mcp'}
ADMIN_WRITES = re.compile(r'^/api/admin/(plugins|custom-checks|mcp-tools)')


def add_user(store, email: str, role: str) -> None:
    Users(store.db).create(email, email, role, PASSWORD)


@contextmanager
def served(store, **over):
    add_instance(store, VAULT, 'main', sealer_key=SEAL, primary=True, enabled=True, **GIVEN)
    app = create_app(dataclasses.replace(settings(), secret_key=SEAL, **over), store, registry=Registry.of(VAULT, directory=HERE))
    with TestClient(app, base_url='https://testserver', follow_redirects=False) as c:
        yield c


def signed_in(c: TestClient, store, role: str) -> TestClient:
    email = f'{role}@example.com'
    add_user(store, email, role)
    assert c.post('/login', data={'email': email, 'password': PASSWORD}).status_code == 303
    return c


def vault_of(body: dict) -> dict:
    return next(p for p in body['plugins'] if p['id'] == 'vault')


@pytest.mark.parametrize('role', ['viewer', 'operator'])
def test_a_signed_in_non_admin_reads_integrations_without_any_secret_value(store, role):
    # given
    with served(store) as c:
        signed_in(c, store, role)

        # when
        res = c.get('/api/integrations')

    # then
    assert res.status_code == 200
    assert [w for w in SEEDED if w in res.text] == []
    vault = vault_of(res.json())
    inst = vault['instances'][0]
    assert inst['name'] == 'main' and inst['enabled'] is True
    assert inst['values']['url'] == 'https://vault.example.com' and inst['values']['endpoint'] == 'https://mcp.example.com'
    assert inst['values']['namespaces'] == 'shop,checkout' and inst['values']['mode'] == 'slow'
    assert 'header-value' not in inst['values'] and 'hook' not in inst['values']
    assert inst['secrets'] == {'token': {'set': True}, 'header-value': {'set': True}, 'hook': {'set': True}}
    assert res.json()['readOnly'] is True


def test_the_reader_view_carries_checks_on_and_threshold_values(store):
    # given
    with served(store) as c:
        signed_in(c, store, 'viewer')
        put = c.put('/api/admin/settings', json={'key': 'vault.lag', 'value': '{"warn": 150}'}, headers=AUTH)

        # when
        vault = vault_of(c.get('/api/integrations').json())

    # then
    assert put.status_code == 200
    assert [ch['id'] for ch in vault['checks']] == ['vault'] and vault['instances'][0]['offChecks'] == []
    assert vault['rules'] == [{'id': 'lag', 'label': 'Lag', 'defaults': {'warn': 100.0, 'crit': 500.0}, 'help': {}, 'values': {'warn': 150.0, 'crit': 500.0}}]


def test_the_admin_catalog_still_shows_plain_values_to_admins(store):
    # given
    with served(store) as c:
        # when
        body = c.get('/api/admin/plugins', headers=AUTH).json()

    # then
    assert vault_of(body)['instances'][0]['values']['namespaces'] == 'shop,checkout'


@pytest.mark.parametrize('public_read', [True, False])
def test_anonymous_visitors_cannot_read_integrations(store, public_read):
    # given
    with served(store, public_read=public_read) as c:
        # when
        res = c.get('/api/integrations')
        checks = c.get('/api/integrations/custom-checks')

    # then
    assert res.status_code == 401 and checks.status_code == 401


@pytest.mark.parametrize('role', ['viewer', 'operator'])
def test_every_integrations_write_route_refuses_non_admins(store, role):
    # given
    with served(store) as c:
        signed_in(c, store, role)
        routes = sorted({(m, p) for m, p in walk(c.app.routes) if m in ('POST', 'PUT', 'PATCH', 'DELETE') and ADMIN_WRITES.match(p)})
        concrete = [(m, re.sub(r'\{[^}]*\}', 'vault', p)) for m, p in routes]

        # when
        answers = {(m, p): c.request(m, p, json={}, headers={'origin': 'https://testserver'}) for m, p in concrete}

    # then
    assert len(routes) > 10
    assert {k: r.status_code for k, r in answers.items() if r.status_code != 403} == {}
    assert {k: r.json()['detail'] for k, r in answers.items() if r.json()['detail'] != msg('access.need_admin')} == {}


@pytest.mark.parametrize('role', ['viewer', 'operator'])
def test_every_integrations_write_route_refuses_non_admin_tokens(store, role):
    # given
    with served(store) as c:
        made = c.post('/api/admin/tokens', json={'name': f'{role}-ci', 'role': role}, headers=AUTH)
        bearer = {'Authorization': f"Bearer {made.json()['token']}"}
        routes = sorted({(m, p) for m, p in walk(c.app.routes) if m in ('POST', 'PUT', 'PATCH', 'DELETE') and ADMIN_WRITES.match(p)})

        # when
        read = c.get('/api/integrations', headers=bearer)
        codes = {(m, p): c.request(m, re.sub(r'\{[^}]*\}', 'vault', p), json={}, headers=bearer).status_code for m, p in routes}

    # then
    assert made.status_code == 201 and read.status_code == 200
    assert [w for w in SEEDED if w in read.text] == []
    assert {k: v for k, v in codes.items() if v != 403} == {}


def test_a_viewer_reads_custom_checks_with_masked_run_texts_but_cannot_add_one(store):
    # given
    with served(store) as c:
        defs = c.app.state.custom_checks.defs
        made = defs.create(clean(CHECK), 'admin@example.com')
        defs.set_state(made['id'], {'state': 'failing', 'reason': f'https://{USER_WORD}:{USER_WORD}@prometheus.example.com/s/{PATH_WORD}/api refused', 'at': '2026-11-20T10:00:00Z',
                                    'pending': {'k': {'since': 1}}, 'extra': 'x'})
        signed_in(c, store, 'viewer')

        # when
        read = c.get('/api/integrations/custom-checks')
        write = c.post('/api/admin/custom-checks', json=CHECK, headers={'origin': 'https://testserver'})

    # then
    assert read.status_code == 200 and read.json()['readOnly'] is True
    assert [i['name'] for i in read.json()['items']] == ['Redis memory'] and read.json()['items'][0]['run']['state'] == 'failing'
    assert USER_WORD not in read.text and PATH_WORD not in read.text
    assert read.json()['items'][0]['run'] == {'state': 'failing', 'reason': 'https://prometheus.example.com refused', 'at': '2026-11-20T10:00:00Z'}
    assert not {'createdBy', 'updatedBy'} & set(read.json()['items'][0]), 'no editor names for readers'
    assert write.status_code == 403 and defs.list() == [made]


def test_reader_row_masks_secrets_in_health_problem_and_reason_texts():
    # given
    row = {'id': 'vault', 'name': 'Vault demo', 'reason': f'refused token={TOKEN_WORD}; sent Basic {BASIC_WORD} and {PREFIXED[0]}', 'module': 'pkg.vault',
           'fields': [{'key': 'url', 'kind': 'url'}, {'key': 'ca', 'kind': 'pem'}, {'key': 'socket', 'kind': 'url'}, {'key': 'email', 'kind': 'text'}],
           'health': {'state': 'error', 'text': f'https://{USER_WORD}:{USER_WORD}@vault.example.com/s/{PATH_WORD} answered 401; retried vault.example.com/k/{BARE_WORD} with {PREFIXED[1]}', 'at': None, 'extra': 'x',
                      'instances': [{'name': 'main', 'state': 'error', 'text': f'password: {HEADER_WORD}, cookie={KEYED_WORD}, {PREFIXED[2]}', 'at': None, 'detail': 'x'}]},
           'instances': [{'name': 'main', 'primary': True, 'enabled': True, 'source': 'admin', 'problem': f'Bearer {QUERY_WORD}-and-more',
                          'values': {'url': f'https://{USER_WORD}:{USER_WORD}@vault.example.com/s/{PATH_WORD}', 'ca': PEM_TEXT, 'socket': 'unix:///run/vault.sock', 'email': 'alice@example.com', 'stray': 'x'},
                          'secrets': {}, 'owned': ['url'], 'offChecks': [], 'extra': 'x'}]}

    # when
    out = reader_row(row, [])

    # then
    text = repr(out)
    assert [w for w in SEEDED if w in text] == []
    assert out['instances'][0]['values'] == {'url': 'https://vault.example.com', 'email': 'alice@example.com'}, 'emails stay visible'
    assert out['instances'][0]['secrets'] == {'ca': {'set': True}, 'socket': {'set': True}}, 'a non-web address is set but hidden'
    assert set(out['instances'][0]) == INSTANCE_EXPECTED
    assert 'module' not in out
    assert out['health']['text'] == 'https://vault.example.com answered 401; retried vault.example.com with [redacted]'
    assert set(out['health']) == {'state', 'text', 'at', 'instances'} and set(out['health']['instances'][0]) == {'name', 'state', 'text', 'at'}


@pytest.mark.parametrize('url, shown', [('https://u:p@shop.example.com:8443/s/x?y=1#z', 'https://shop.example.com:8443'), ('http://shop.example.com/a', 'http://shop.example.com'),
                                        ('ftp://shop.example.com/a', ''), ('https://[::1]:8443/a', 'https://[::1]:8443'), ('http://[fe80::1]/x', 'http://[fe80::1]'), ('shop.example.com/a', ''), ('javascript:alert(1)', ''), ('https://[bad', '')])
def test_a_reader_sees_only_scheme_host_and_port_of_a_web_address(url, shown):
    # then
    assert bare_url(url) == shown


def test_a_reader_row_is_built_from_explicit_key_lists():
    # given
    row = {k: 'x' for k in ROW_KEYS} | {'module': 'pkg.vault', 'localeDrops': [], 'secretsKey': 'env', 'maxInstances': 2, 'settings': [], 'hooks': ['role_resolver'], 'agentReads': [],
                                        'fields': [{'key': 'url', 'label': 'URL', 'kind': 'url', 'default': 'https://a.example.com/p', 'suggest': True, 'ui': True}],
                                        'checks': [{'id': 'c', 'label': 'C', 'period': 60, 'ran': ['c']}], 'tools': [{'name': 't', 'kind': 'read', 'description': 'd'}],
                                        'panels': [{'tab': 't', 'label': 'T', 'group': 'g', 'script': 's.js', 'fragment': 'f'}], 'setup': {'steps': ['a'], 'docs': 'https://a.example.com', 'docsLabel': 'A', 'x': 1},
                                        'instances': [{'name': 'main', 'values': {}, 'secrets': {}, 'owned': ['url'], 'offChecks': [], 'extra': 1}], 'health': {'state': 'ok', 'text': 't', 'at': None, 'instances': []}}

    # when
    out = reader_row(row, [])

    # then
    assert set(out) == set(ROW_KEYS) == ROW_EXPECTED
    assert (frozenset(INSTANCE_KEYS), frozenset(FIELD_KEYS), frozenset(RUN_KEYS)) == (INSTANCE_EXPECTED, FIELD_EXPECTED, RUN_EXPECTED)
    assert out['fields'] == [{'key': 'url', 'label': 'URL', 'kind': 'url', 'default': 'https://a.example.com'}]
    assert (out['checks'], out['tools'], out['panels']) == ([{'id': 'c', 'label': 'C'}], [{'name': 't', 'kind': 'read'}], [{'tab': 't', 'label': 'T', 'group': 'g'}])
    assert set(out['setup']) == {'steps', 'docs', 'docsLabel'}
    assert set(out['instances'][0]) == {'name', 'primary', 'enabled', 'source', 'problem', 'values', 'secrets', 'offChecks'}
