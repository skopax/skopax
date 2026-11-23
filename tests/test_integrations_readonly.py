import dataclasses
import re
from contextlib import contextmanager
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from skopax.app import create_app
from skopax.contract import Capabilities, Field, RuleSet
from skopax.integrationview import ROW_KEYS, RUN_KEYS, bare_url, reader_row
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
ROW_EXPECTED = frozenset({'id', 'name', 'category', 'description', 'origin', 'state', 'enabled', 'icon', 'edition', 'companyKnowledge', 'knowledgeAlso'})
LIST_EXTRA = frozenset({'health', 'instanceCount', 'instancesOn', 'hasFields', 'outside', 'knowing', 'knowledgeError'})
DETAIL_KEYS = ('fields', 'instances', 'setup', 'checks', 'tools', 'panels', 'rules', 'reason', 'version', 'author', 'licence')
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
def test_a_signed_in_non_admin_reads_only_what_is_connected_with_no_instance_detail(store, role):
    # given
    with served(store) as c:
        signed_in(c, store, role)

        # when
        res = c.get('/api/integrations')

    # then
    assert res.status_code == 200
    assert [w for w in SEEDED if w in res.text] == []
    vault = vault_of(res.json())
    assert vault['instanceCount'] == 1 and vault['instancesOn'] == 1 and vault['hasFields'] is True
    assert [k for k in DETAIL_KEYS if k in vault] == []
    assert 'main' not in res.text and 'shop,checkout' not in res.text and 'vault.example.com' not in res.text
    assert res.json()['readOnly'] is True


def test_the_reader_view_carries_no_checks_thresholds_or_setup(store):
    # given
    with served(store) as c:
        signed_in(c, store, 'viewer')

        # when
        vault = vault_of(c.get('/api/integrations').json())

    # then
    assert set(vault) <= ROW_EXPECTED | LIST_EXTRA
    assert vault['name'] == 'Vault demo' and vault['state'] == 'ok' and vault['enabled'] is True


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


def test_reader_row_sends_only_a_generic_health_sentence_and_no_instance_rows():
    # given
    row = {'id': 'vault', 'name': 'Vault demo', 'reason': f'refused token={TOKEN_WORD}', 'module': 'pkg.vault', 'fields': [{'key': 'url', 'kind': 'url'}], 'state': 'ok',
           'health': {'state': 'error', 'text': f'https://{USER_WORD}:{USER_WORD}@vault.example.com/s/{PATH_WORD} answered 401 for prod-east with {PREFIXED[1]}', 'at': None, 'extra': 'x',
                      'instances': [{'name': 'prod-east', 'state': 'error', 'text': f'prod-east: password: {HEADER_WORD}, cookie={KEYED_WORD}, {PREFIXED[2]}', 'at': None, 'detail': 'x'}]},
           'instances': [{'name': 'prod-east', 'enabled': True, 'problem': f'Bearer {QUERY_WORD}-and-more', 'values': {'url': f'https://{USER_WORD}@vault.example.com/{PATH_WORD}'}}]}

    # when
    out = reader_row(row)

    # then
    text = repr(out)
    assert [w for w in SEEDED if w in text] == []
    assert 'prod-east' not in text and 'vault.example.com' not in text and 'reason' not in out and 'module' not in out and 'instances' not in out
    assert out['health'] == {'state': 'error', 'text': 'Needs attention: an admin can see the details', 'at': None}
    assert (out['instanceCount'], out['instancesOn'], out['hasFields'], out['outside']) == (1, 0, True, False)


@pytest.mark.parametrize('state, words', [('ok', 'Connected'), ('warn', 'Needs attention: an admin can see the details'), ('error', 'Needs attention: an admin can see the details'), ('none', 'Off'), ('pending', 'Checking')])
def test_reader_health_is_one_fixed_sentence_per_state(state, words):
    # when
    out = reader_row({'id': 'vault', 'state': 'ok', 'health': {'state': state, 'text': 'prod-east: refused', 'at': None}})

    # then
    assert out['health']['text'] == words


def test_a_viewer_never_sees_an_instance_name_in_a_health_problem(store):
    # given
    with served(store) as c:
        add_instance(store, VAULT, 'prod-east', sealer_key=SEAL, enabled=True, **GIVEN)
        c.app.state.instances.reread()
        signed_in(c, store, 'viewer')

        # when
        res = c.get('/api/integrations')

    # then
    assert res.status_code == 200 and 'prod-east' not in res.text
    assert vault_of(res.json())['instanceCount'] >= 1


def test_reader_row_counts_instances_that_are_on_and_without_a_problem_and_tells_knowledge():
    # given
    row = {'id': 'wiki', 'state': 'ok', 'companyKnowledge': 'space', 'fields': [{'key': 'space'}], 'checks': [{'id': 'c'}],
           'instances': [{'name': 'a', 'enabled': True, 'problem': None, 'values': {'space': 'ENG'}}, {'name': 'b', 'enabled': True, 'problem': 'down', 'values': {}}, {'name': 'c', 'enabled': False, 'values': {'space': ''}}]}
    mcp = {'id': 'mcp-tools', 'state': 'ok', 'fields': [{'key': 'knowledge'}], 'instances': [{'name': 'x', 'enabled': True, 'values': {'knowledge': 'true'}}, {'name': 'y', 'enabled': True, 'values': {}}],
           'health': {'state': 'warn', 'text': 't', 'at': None, 'instances': [{'name': 'x', 'state': 'error', 'text': 'e', 'at': None}, {'name': 'y', 'state': 'ok', 'text': 'o', 'at': None}]}}

    # when
    out, via = reader_row(row), reader_row(mcp)

    # then
    assert (out['instanceCount'], out['instancesOn'], out['knowing'], out['outside']) == (3, 1, True, False)
    assert via['knowing'] is True and via['knowledgeError'] is True and 'instances' not in via['health']
    assert reader_row({'id': 'tool', 'state': 'ok', 'fields': [], 'checks': [], 'panels': []})['outside'] is True


@pytest.mark.parametrize('url, shown', [('https://u:p@shop.example.com:8443/s/x?y=1#z', 'https://shop.example.com:8443'), ('http://shop.example.com/a', 'http://shop.example.com'),
                                        ('ftp://shop.example.com/a', ''), ('https://[::1]:8443/a', 'https://[::1]:8443'), ('http://[fe80::1]/x', 'http://[fe80::1]'), ('shop.example.com/a', ''), ('javascript:alert(1)', ''), ('https://[bad', '')])
def test_a_reader_sees_only_scheme_host_and_port_of_a_web_address(url, shown):
    # then
    assert bare_url(url) == shown


def test_a_reader_row_is_built_from_explicit_key_lists():
    # given
    row = {k: 'x' for k in ROW_KEYS} | {'module': 'pkg.vault', 'reason': 'r', 'version': '1', 'author': 'a', 'licence': 'l', 'rules': [{'id': 'r'}], 'setup': {'steps': ['a']}, 'tools': [{'name': 't'}], 'localeDrops': [],
                                        'fields': [{'key': 'url', 'kind': 'url'}], 'checks': [{'id': 'c'}], 'panels': [{'tab': 't'}], 'instances': [{'name': 'main', 'values': {}, 'secrets': {}}], 'health': {'state': 'ok', 'text': 't', 'at': None, 'instances': []}}

    # when
    out = reader_row(row)

    # then
    assert set(ROW_KEYS) == ROW_EXPECTED and frozenset(RUN_KEYS) == RUN_EXPECTED
    assert set(out) == ROW_EXPECTED | LIST_EXTRA
    assert out['instanceCount'] == 1
