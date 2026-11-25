import dataclasses
import json
import logging
from pathlib import Path

import httpx2
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

from tests.conftest import AUTH, settings
from tests.fake_anthropic import FakeModels
from tests.test_compat_ai import Maker as CompatMaker, connection as compat_connection, public_dns
from skopax import ai, clock
from skopax.ai import FETCH_TYPE, SEARCH_TYPE, Usage, compat_ai, vertex_ai
from skopax.app import create_app
from skopax.config import load_settings
from skopax.investigator import pricing
from skopax.messages import msg
from skopax.store import Store

PATH = '/api/admin/ai-vertex'
MARK = 'MARK-' + 'q' * 20
CLIENT_EMAIL = 'skopax@example.com'
REF = 'vertex/claude-opus-5-5'
PROJECT = 'shop-prod-1234'
USAGE = Usage(input=1_000_000, cache_read=1_000_000, cache_write=1_000_000, cache_write_long=1_000_000, output=1_000_000, web_searches=100)


def make_pem() -> str:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode()


PEM = make_pem()


def service_key(**over) -> str:
    return json.dumps({'type': 'service_account', 'project_id': PROJECT, 'private_key_id': MARK, 'private_key': PEM, 'client_email': CLIENT_EMAIL, 'client_id': '1',
                       'token_uri': 'https://oauth2.googleapis.com/token'} | over)


def federation(path: str = '/var/run/secrets/tokens/gcp', **over) -> dict:
    return {'type': 'external_account', 'audience': '//iam.googleapis.com/projects/123456/locations/global/workloadIdentityPools/pool1/providers/prov1',
            'subject_token_type': 'urn:ietf:params:oauth:token-type:jwt', 'token_url': 'https://sts.googleapis.com/v1/token',
            'credential_source': {'file': path, 'format': {'type': 'text'}}} | over


def body(**over) -> dict:
    return {'project': PROJECT, 'region': 'eu', 'credentials': {'mode': 'key', 'key': service_key()}, 'models': [{'id': 'claude-opus-5-5', 'vertexId': 'claude-opus-5-5'}]} | over


def message(text: str = 'ok', calls: tuple = (), usage: dict | None = None) -> dict:
    content = ([{'type': 'text', 'text': text}] if text else []) + [{'type': 'tool_use', 'id': cid, 'name': name, 'input': args} for cid, name, args in calls]
    return {'id': 'msg_1', 'type': 'message', 'role': 'assistant', 'model': 'claude-opus-5-5', 'content': content, 'stop_reason': 'tool_use' if calls else 'end_turn',
            'usage': usage or {'input_tokens': 100, 'output_tokens': 10}}


def failure(status: int) -> httpx2.Response:
    return httpx2.Response(status, json={'type': 'error', 'error': {'type': 'api_error', 'message': 'upstream ' + MARK}})


class Maker:
    def __init__(self, *replies):
        self.replies = list(replies)
        self.requests: list[dict] = []
        self.built: list = []

    def respond(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append({'host': request.url.host, 'path': request.url.path, 'body': json.loads(request.content), 'headers': dict(request.headers)})
        found = self.replies.pop(0) if self.replies else message()
        return found if isinstance(found, httpx2.Response) else httpx2.Response(200, json=found)

    def __call__(self, conn, token_dir, timeout, retries):
        self.built.append(conn)
        return vertex_ai.client_for(conn, token_dir, timeout, retries, transport=httpx2.MockTransport(self.respond), access_token='stub-token')


def connection_of(**over) -> vertex_ai.Connection:
    base = {'name': 'vertex', 'project': PROJECT, 'region': 'eu', 'mode': 'pod', 'models': (vertex_ai.Model('claude-opus-5-5', 'claude-opus-5-5', 4.0, 20.0),)}
    return vertex_ai.Connection(**(base | over))


def adapter(maker: Maker, **over) -> vertex_ai.VertexAi:
    return vertex_ai.VertexAi(maker(connection_of(**over), '/var/run/secrets', 30, 0), 'vertex', {'claude-opus-5-5': 'claude-opus-5-5'})


def app(store: Store, maker: Maker | None = None, **over):
    return create_app(dataclasses.replace(settings(), **({'anthropic_api_key': ''} | over)), store, ai_factory=FakeModels(), compat_factory=CompatMaker(), vertex_factory=maker or Maker())


def put(c: TestClient, name: str = 'vertex', **over):
    return c.put(f'{PATH}/{name}', json=body(**over), headers=AUTH)


def test_a_turn_goes_to_the_regional_endpoint_with_cache_marks_and_no_field_vertex_rejects():
    # given
    maker = Maker(message('Looking.', (('toolu_1', 'list_pods', {'ns': 'shop'}),)))
    tools = [{'name': 'list_pods', 'description': 'Lists pods', 'input_schema': {'type': 'object', 'properties': {'ns': {'type': 'string'}}}},
             {'type': SEARCH_TYPE, 'name': 'web_search', 'max_uses': 3, 'allowed_domains': ['example.com']},
             {'type': FETCH_TYPE, 'name': 'web_fetch', 'max_uses': 4}, {'type': 'web_search_20260209', 'name': 'web_search'}]

    # when
    got = adapter(maker).turn(REF, 'high', 'SYSTEM', tools, [{'role': 'user', 'content': 'BRIEF'}], 4000, '1h')

    # then
    sent = maker.requests[0]
    assert sent['host'] == 'aiplatform.eu.rep.googleapis.com'
    assert sent['path'] == f'/v1/projects/{PROJECT}/locations/eu/publishers/anthropic/models/claude-opus-5-5:rawPredict'
    assert 'model' not in sent['body'] and sent['body']['anthropic_version']
    assert sent['body']['system'] == [{'type': 'text', 'text': 'SYSTEM', 'cache_control': {'type': 'ephemeral', 'ttl': '1h'}}]
    assert sent['body']['messages'] == [{'role': 'user', 'content': [{'type': 'text', 'text': 'BRIEF', 'cache_control': {'type': 'ephemeral'}}]}]
    assert [(t.get('type'), t['name']) for t in sent['body']['tools']] == [(None, 'list_pods'), (SEARCH_TYPE, 'web_search')]
    assert sent['body']['thinking'] == {'type': 'adaptive'} and sent['body']['output_config'] == {'effort': 'high'}
    assert not {'fallbacks', 'betas', 'anthropic_beta'} & set(sent['body']) and 'anthropic-beta' not in sent['headers']
    assert (got.model, got.stop, [c.name for c in got.calls]) == (REF, 'tool_use', ['list_pods'])


def test_a_turn_never_sends_fallbacks_even_when_asked_and_leaves_out_an_empty_tool_list():
    # given
    maker = Maker()

    # when
    adapter(maker).turn(REF, 'low', 'SYSTEM', [{'type': FETCH_TYPE, 'name': 'web_fetch'}], [{'role': 'user', 'content': 'hi'}], 100, None, 'off', True)

    # then
    sent = maker.requests[0]['body']
    assert 'tools' not in sent and 'fallbacks' not in sent and 'thinking' not in sent and sent['system'][0]['cache_control'] == {'type': 'ephemeral'}


def test_a_once_call_sends_the_vertex_model_id_and_the_schema_without_betas():
    # given
    maker = Maker(message('{"a": 1}'))

    # when
    got = adapter(maker).once(REF, 'SYSTEM', [{'role': 'user', 'content': 'Q'}], 200, 'low', schema={'type': 'object'})

    # then
    sent = maker.requests[0]
    assert sent['path'].endswith('/models/claude-opus-5-5:rawPredict') and sent['body']['output_config']['format']['type'] == 'json_schema'
    assert 'fallbacks' not in sent['body'] and got.model == REF and got.text == '{"a": 1}'


def test_a_model_not_on_the_connection_is_refused_before_anything_is_sent():
    # given
    maker = Maker()

    # when
    with pytest.raises(compat_ai.Refused):
        adapter(maker).turn('vertex/claude-other', 'low', 'SYSTEM', [], [{'role': 'user', 'content': 'hi'}], 100)

    # then
    assert maker.requests == []


@pytest.mark.parametrize('region, host', [('global', 'aiplatform.googleapis.com'), ('us', 'aiplatform.us.rep.googleapis.com'), ('eu', 'aiplatform.eu.rep.googleapis.com'),
                                          ('europe-west4', 'europe-west4-aiplatform.googleapis.com'), ('us-east5', 'us-east5-aiplatform.googleapis.com')])
def test_each_region_maps_to_one_fixed_host(region, host):
    # given
    maker = Maker()

    # when
    adapter(maker, region=region).once(REF, 'S', [{'role': 'user', 'content': 'Q'}], 10, 'low')

    # then
    assert vertex_ai.host_of(region) == host and maker.requests[0]['host'] == host and f'/locations/{region}/' in maker.requests[0]['path']


def test_a_region_outside_the_list_has_no_host():
    # when
    with pytest.raises(compat_ai.Refused):
        vertex_ai.host_of('evil.example.com/x')

    # then
    assert vertex_ai.REGIONS[:3] == ('global', 'us', 'eu') and all(vertex_ai.host_of(r).endswith('googleapis.com') for r in vertex_ai.REGIONS)


def test_the_environment_cannot_move_the_client_to_another_host_or_region(monkeypatch):
    # given
    monkeypatch.setenv('ANTHROPIC_VERTEX_BASE_URL', 'https://evil.example.com/v1')
    monkeypatch.setenv('CLOUD_ML_REGION', 'us-east5')
    monkeypatch.setenv('ANTHROPIC_VERTEX_PROJECT_ID', 'other-project-1')
    maker = Maker()

    # when
    adapter(maker).once(REF, 'S', [{'role': 'user', 'content': 'Q'}], 10, 'low')

    # then
    assert maker.requests[0]['host'] == 'aiplatform.eu.rep.googleapis.com' and f'/projects/{PROJECT}/locations/eu/' in maker.requests[0]['path']


def test_the_transport_refuses_any_other_host_scheme_or_redirect():
    # given
    seen = []
    inner = httpx2.MockTransport(lambda r: seen.append(r.url.host) or httpx2.Response(302, headers={'location': 'https://evil.example.com/'}))
    locked = vertex_ai.Locked('aiplatform.googleapis.com', inner)
    http = httpx2.Client(transport=locked, follow_redirects=False)

    # when
    other = [pytest.raises(httpx2.ConnectError, http.get, url) for url in ('https://evil.example.com/v1', 'http://aiplatform.googleapis.com/v1', 'https://aiplatform.googleapis.com:8443/v1')]
    redirected = http.get('https://aiplatform.googleapis.com/v1')

    # then
    assert len(other) == 3 and seen == ['aiplatform.googleapis.com'] and redirected.status_code == 302


def test_google_auth_failures_become_a_plain_api_error():
    # given
    from google.auth.exceptions import RefreshError

    def boom(**kw):
        raise RefreshError('token ' + MARK)

    client = vertex_ai.client_for(connection_of(), '/var/run/secrets', 5, 0, transport=httpx2.MockTransport(lambda r: httpx2.Response(200, json=message())), access_token='x')
    client.messages.create = boom
    held = vertex_ai.VertexAi(client, 'vertex', {'claude-opus-5-5': 'claude-opus-5-5'})

    # when
    with pytest.raises(vertex_ai.CredentialError) as failed:
        held.once(REF, 'S', [{'role': 'user', 'content': 'Q'}], 10, 'low')

    # then
    assert isinstance(failed.value, held.api_error) and MARK not in str(failed.value) and failed.value.__cause__ is None


def test_vertex_has_every_capability_except_web_fetch_and_server_side_fallbacks():
    # then
    assert vertex_ai.VERTEX_CAPABILITIES == frozenset({'tools', 'images', 'web_search', 'thinking', 'cache'})
    assert vertex_ai.VertexAi.capabilities == vertex_ai.VERTEX_CAPABILITIES and 'web_fetch' in ai.PROVIDER_CAPABILITIES['anthropic'] and 'web_fetch' not in ai.PROVIDER_CAPABILITIES['openai']


def test_a_vertex_connection_offers_the_vertex_capabilities_to_the_clients(store: Store):
    # given
    c = TestClient(app(store, Maker(), investigate_mode='live', investigate_model=REF))
    put(c)
    inv = c.app.state.investigator

    # when
    caps = inv.clients.capabilities('vertex:vertex')

    # then
    assert caps == vertex_ai.VERTEX_CAPABILITIES and 'web_fetch' not in caps and 'fallbacks' not in caps


def test_a_key_must_be_a_service_account_key_with_google_endpoints_only():
    # then
    assert json.loads(vertex_ai.checked_key(service_key()))['client_email'] == CLIENT_EMAIL
    for bad in (service_key(type='authorized_user'), service_key(token_uri='https://evil.example.com/token'), service_key(client_email='nope'), service_key(private_key='x'),
                '{not json', '', '[]', 'x' * (vertex_ai.KEY_MAX + 1), None, 5):
        with pytest.raises(compat_ai.Refused) as refused:
            vertex_ai.checked_key(bad)
        assert refused.value.text == msg('vertex.key_invalid') and MARK not in refused.value.text
    assert 'extra' not in json.loads(vertex_ai.checked_key(service_key(extra='x')))


def test_a_federation_config_may_only_read_a_token_file_under_the_allowed_path(tmp_path):
    # given
    root = str(tmp_path)
    good = federation(str(tmp_path / 'tokens' / 'gcp'))
    outside = tmp_path.parent / 'elsewhere'
    outside.mkdir(exist_ok=True)
    (tmp_path / 'link').symlink_to(outside)

    # when
    accepted = vertex_ai.checked_federation(good, root)
    refusals = {name: pytest.raises(compat_ai.Refused, vertex_ai.checked_federation, cfg, root).value.text for name, cfg in {
        'etc': federation('/etc/passwd'), 'up': federation(f'{root}/../elsewhere/t'), 'rel': federation('tokens/gcp'), 'sibling': federation(root + '-x/t'),
        'link': federation(f'{root}/link/t'), 'nul': federation(f'{root}/a\x00b')}.items()}

    # then
    assert accepted['credential_source']['file'] == str(tmp_path / 'tokens' / 'gcp')
    assert set(refusals.values()) == {msg('vertex.federation_path', root=root)}


def test_a_federation_config_refuses_url_and_executable_sources_and_foreign_endpoints():
    # given
    base = federation()
    cases = [federation(credential_source={'url': 'http://169.254.169.254/token'}), federation(credential_source={'executable': {'command': '/bin/sh'}}),
             federation(credential_source={'file': '/var/run/secrets/t', 'url': 'http://x'}), federation(token_url='https://evil.example.com/token'),
             federation(service_account_impersonation_url='https://evil.example.com/v1/projects/-/serviceAccounts/skopax@example.com:generateAccessToken'),
             federation(audience='//iam.googleapis.com/projects/1/locations/global/workforcePools/p/providers/q'), federation(subject_token_type='urn:ietf:params:oauth:token-type:saml2'),
             federation(type='service_account'), base | {'quota_project_id': 'x'}, federation(universe_domain='evil.example.com'), {}, [], '', 'nope']

    # when
    texts = [pytest.raises(compat_ai.Refused, vertex_ai.checked_federation, c, '/var/run/secrets').value.text for c in cases]

    # then
    assert set(texts) <= {msg('vertex.federation_invalid'), msg('vertex.federation_source')} and len(texts) == len(cases)
    assert vertex_ai.checked_federation(federation(service_account_impersonation_url='https://iamcredentials.googleapis.com/v1/projects/-/serviceAccounts/skopax@example.com:generateAccessToken'),
                                        '/var/run/secrets')['service_account_impersonation_url'].startswith('https://iamcredentials.googleapis.com/')


def test_the_token_directory_defaults_to_the_pod_secrets_mount_and_follows_the_environment():
    # then
    assert load_settings(env={}).vertex_token_dir == '/var/run/secrets/tokens' == vertex_ai.TOKEN_DIR
    assert load_settings(env={'SKOPAX_VERTEX_TOKEN_DIR': '/mnt/tokens'}).vertex_token_dir == '/mnt/tokens'
    assert load_settings(env={'SKOPAX_VERTEX_TOKEN_DIR': '  '}).vertex_token_dir == '/var/run/secrets/tokens'


def test_the_connection_view_has_the_pinned_shape_and_never_the_key(store: Store):
    # given
    c = TestClient(app(store))

    # when
    saved = put(c)
    shown = c.get(f'{PATH}/vertex', headers=AUTH)
    listed = c.get(PATH, headers=AUTH)

    # then
    assert saved.status_code == shown.status_code == listed.status_code == 200
    view = saved.json()
    assert set(view) == {'name', 'project', 'region', 'credentials', 'models', 'lastTest', 'company'}
    assert view == shown.json() == listed.json()['items'][0]
    assert view['credentials'] == {'mode': 'key', 'set': True} and view['company'] == 'Google Cloud Vertex AI (eu)' and view['lastTest'] is None
    assert view['models'] == [{'id': 'claude-opus-5-5', 'vertexId': 'claude-opus-5-5', 'input': 4.0, 'output': 20.0}]
    assert listed.json()['regions'] == list(vertex_ai.REGIONS)
    assert MARK not in saved.text + shown.text + listed.text and 'BEGIN' not in saved.text + shown.text + listed.text


def test_the_key_is_sealed_at_rest_and_in_no_log_change_or_error(store: Store, caplog):
    # given
    caplog.set_level(logging.DEBUG)
    c = TestClient(app(store, Maker(failure(403))))
    put(c)

    # when
    tested = c.post(f'{PATH}/vertex/test', json={}, headers=AUTH)
    bad = c.put(f'{PATH}/vertex', json={'project': PROJECT, 'region': 'eu', 'credentials': {'mode': 'key', 'key': {'private_key_id': MARK}}, 'models': 'x'}, headers=AUTH)
    junk = c.put(f'{PATH}/vertex', json={'credentials': MARK}, headers=AUTH)
    stored = store.db['admin_secrets'].find_one({'_id': 'vertex.vertex'})
    history = c.get('/api/admin/changes', headers=AUTH).text

    # then
    assert stored['value'] != service_key() and MARK not in json.dumps(stored['value']) and 'BEGIN' not in json.dumps(stored)
    assert (bad.status_code, junk.status_code) == (422, 422) and bad.json() == junk.json() == {'detail': msg('vertex.invalid')}
    assert MARK not in tested.text + bad.text + junk.text + history + caplog.text and 'BEGIN' not in tested.text + history + caplog.text


def test_changing_the_project_or_region_needs_the_key_again_and_keeps_the_key_otherwise(store: Store):
    # given
    c = TestClient(app(store))
    put(c)
    kept = {'mode': 'key'}

    # when
    same = put(c, credentials=kept, models=[{'id': 'claude-opus-5-5', 'vertexId': 'claude-opus-5-5', 'input': 1, 'output': 2}])
    region = put(c, credentials=kept, region='europe-west4')
    project = put(c, credentials=kept, project='other-proj-1234')
    again = put(c, credentials={'mode': 'key', 'key': service_key()}, region='europe-west4')

    # then
    assert same.status_code == 200 and same.json()['models'][0]['input'] == 1.0
    assert (region.status_code, region.json()['detail']) == (422, msg('vertex.key_reenter')) and project.status_code == 422
    assert again.status_code == 200 and again.json()['region'] == 'europe-west4'
    assert store.db['admin_secrets'].find_one({'_id': 'vertex.vertex'})['region'] == 'europe-west4'


def test_the_pod_and_federation_modes_store_no_key_and_never_ask_for_one(store: Store):
    # given
    c = TestClient(app(store))
    put(c)

    # when
    pod = put(c, credentials={'mode': 'pod'})
    stored_pod = store.db['admin_secrets'].find_one({'_id': 'vertex.vertex'})
    fed = put(c, credentials={'mode': 'federation', 'config': federation()})
    stored_fed = store.db['admin_secrets'].find_one({'_id': 'vertex.vertex'})
    fed_again = put(c, credentials={'mode': 'federation'}, region='europe-west4')
    back = put(c, credentials={'mode': 'key'})

    # then
    assert pod.json()['credentials'] == {'mode': 'pod', 'set': True} and 'value' not in stored_pod and 'federation' not in stored_pod
    assert fed.json()['credentials'] == {'mode': 'federation', 'set': True} and 'value' not in stored_fed and stored_fed['federation']['credential_source']['file'].startswith('/var/run/secrets/')
    assert fed_again.status_code == 200 and (back.status_code, back.json()['detail']) == (422, msg('vertex.key_needed'))


def test_a_federation_config_outside_the_allowed_path_is_refused_on_save_and_the_use_is_held(store: Store):
    # given
    c = TestClient(app(store, Maker(), vertex_token_dir='/mnt/tokens', investigate_mode='live', investigate_model=REF))

    # when
    outside = put(c, credentials={'mode': 'federation', 'config': federation('/var/run/secrets/tokens/gcp')})
    inside = put(c, credentials={'mode': 'federation', 'config': federation('/mnt/tokens/gcp')})
    usable = vertex_ai.factory('vertex:vertex') is not None
    vertex_ai.BOUND.token_dir = lambda: '/srv/other'
    moved = vertex_ai.factory('vertex:vertex')
    c.app.state.vertex_connections.token_dir = lambda: '/srv/other'
    blocked = {n['id']: n['text'] for n in c.get('/api/admin/notices', headers=AUTH).json()['items']}

    # then
    assert (outside.status_code, outside.json()['detail']) == (422, msg('vertex.federation_path', root='/mnt/tokens')) and inside.status_code == 200 and usable
    assert blocked['ai-vertex-token:vertex'] == msg('vertex.token_path_blocked', name='vertex')
    assert moved is None and vertex_ai.usable(c.app.state.vertex_connections.get('vertex'), '/srv/other') is False


def test_the_request_is_checked_for_names_models_prices_and_regions(store: Store):
    # given
    c = TestClient(app(store))
    models = [{'id': 'a', 'vertexId': 'claude-x'}]

    # when
    refused = [put(c, name=name, **over).json()['detail'] for name, over in [
        ('Bad_Name', {}), ('vertex', {'project': 'x'}), ('vertex', {'region': 'moon-1'}), ('vertex', {'credentials': {'mode': 'oauth'}}),
        ('vertex', {'models': [{'id': 'a', 'vertexId': 'claude/x'}]}), ('vertex', {'models': models + models}), ('vertex', {'models': [{'id': 'a', 'vertexId': 'm', 'input': 1}]}),
        ('vertex', {'models': [{'id': 'a b', 'vertexId': 'm'}]})]]

    # then
    assert refused == [msg('compat.name_invalid'), msg('vertex.project_invalid'), msg('vertex.region_invalid'), msg('vertex.mode_invalid'), msg('vertex.models_invalid'),
                       msg('vertex.models_invalid'), msg('vertex.price_half'), msg('vertex.models_invalid')]
    assert c.get(PATH, headers=AUTH).json()['items'] == []


def test_prices_start_from_the_anthropic_list_and_stay_editable(store: Store):
    # given
    c = TestClient(app(store))

    # when
    listed = put(c, models=[{'id': 'opus', 'vertexId': 'claude-opus-5-5'}, {'id': 'old', 'vertexId': 'claude-opus-4-5@20251101'}, {'id': 'mine', 'vertexId': 'claude-sonnet-5-5', 'input': 3, 'output': 15}]).json()

    # then
    assert [(m['id'], m['input'], m['output']) for m in listed['models']] == [('opus', 4.0, 20.0), ('old', None, None), ('mine', 3.0, 15.0)]


def test_a_vertex_name_cannot_take_a_compat_name_and_the_reverse(store: Store, public_dns):
    # given
    c = TestClient(app(store))
    c.put('/api/admin/ai-connections/deepseek', json=compat_connection(), headers=AUTH)
    put(c, name='shared')

    # when
    vertex_on_compat = put(c, name='deepseek')
    compat_on_vertex = c.put('/api/admin/ai-connections/shared', json=compat_connection(), headers=AUTH)

    # then
    assert (vertex_on_compat.status_code, vertex_on_compat.json()['detail']) == (422, msg('vertex.name_taken'))
    assert (compat_on_vertex.status_code, compat_on_vertex.json()['detail']) == (422, msg('vertex.name_taken'))


def test_delete_removes_the_connection_and_its_models_and_a_second_delete_is_quiet(store: Store):
    # given
    c = TestClient(app(store))
    put(c)

    # when
    first = c.delete(f'{PATH}/vertex', headers=AUTH)
    second = c.delete(f'{PATH}/vertex', headers=AUTH)
    after = c.get(f'{PATH}/vertex', headers=AUTH)

    # then
    assert (first.status_code, second.status_code, after.status_code) == (204, 204, 404) and vertex_ai.models() == ()
    assert store.db['admin_secrets'].find_one({'_id': 'vertex.vertex'}) is None


def test_the_routes_need_a_signed_in_admin_from_this_site(store: Store):
    # given
    from tests.test_login import PASSWORD, make_client, post_login, with_password
    from skopax.users import Users
    with_password(store)
    Users(store.db).create('olive@example.com', 'Olive', 'operator', PASSWORD)
    anonymous, operator, admin = make_client(store), make_client(store), make_client(store)
    post_login(operator, 'olive@example.com')
    post_login(admin)
    routes = [('GET', PATH), ('GET', f'{PATH}/vertex'), ('PUT', f'{PATH}/vertex'), ('DELETE', f'{PATH}/vertex'), ('POST', f'{PATH}/vertex/test')]

    # when
    nobody = [anonymous.request(m, p, json=body()).status_code for m, p in routes]
    plain = [operator.request(m, p, json=body(), headers={'origin': 'https://testserver'}).status_code for m, p in routes]
    cross = [admin.request(m, p, json=body(), headers={'origin': 'https://evil.example.com'}).status_code for m, p in routes[2:]]

    # then
    assert nobody == [401] * 5 and plain == [403] * 5 and cross == [403] * 3
    assert store.db['admin_secrets'].find_one({'_id': 'vertex.vertex'}) is None


def test_pricing_charges_a_vertex_model_by_its_own_prices_with_cache_multipliers_and_searches(store: Store):
    # given
    c = TestClient(app(store))
    put(c, models=[{'id': 'claude-opus-5-5', 'vertexId': 'claude-opus-5-5', 'input': 4, 'output': 20}, {'id': 'bare', 'vertexId': 'claude-opus-4-5@20251101'}])

    # when
    cost, priced_as = pricing.charge(REF, 'vertex:vertex', USAGE)
    unpriced, fallback = pricing.charge('vertex/bare', 'vertex:vertex', USAGE)

    # then
    assert priced_as is None and cost == pytest.approx(4 + 0.4 + 5 + 8 + 20 + 1.0)
    assert pricing.provider_of(REF) == 'vertex:vertex' and pricing.known(REF) and pricing.price_of(REF) == {'input': 4.0, 'output': 20.0}
    assert fallback == max(pricing.priced_models('vertex:vertex'), key=lambda m: pricing.usd(m, USAGE)) and unpriced == pytest.approx(pricing.usd(fallback, USAGE))
    assert REF in pricing.models_of('vertex:vertex') and ai.provider_of_ref(REF) == 'vertex:vertex'


def test_vertex_prices_leave_with_the_connection(store: Store):
    # given
    c = TestClient(app(store))
    put(c)
    before = pricing.known(REF)

    # when
    c.delete(f'{PATH}/vertex', headers=AUTH)

    # then
    assert before and not pricing.known(REF) and ai.provider_of_ref(REF) == 'compat:vertex'


def test_the_data_label_names_the_region_and_the_model_picks_vertex_in_the_model_settings(store: Store):
    # given
    c = TestClient(app(store, Maker()))
    put(c)

    # when
    picked = c.put('/api/admin/settings', json={'key': 'investigate.model', 'value': REF}, headers=AUTH)
    row = next(r for r in c.get('/api/admin/ai-uses', headers=AUTH).json()['uses'] if r['use'] == 'card')

    # then
    assert picked.status_code == 200 and (row['provider'], row['model'], row['company'], row['problem']) == ('vertex:vertex', REF, 'Google Cloud Vertex AI (eu)', None)
    assert ai.company_of(REF) == 'Google Cloud Vertex AI (eu)' and REF in ai.extra_models()


def test_an_unusable_vertex_use_is_held_and_never_moved_to_another_provider(store: Store):
    # given
    c = TestClient(app(store, Maker(), anthropic_api_key='sk-test', investigate_mode='live', investigate_model=REF))
    put(c)
    inv = c.app.state.investigator
    clients = inv.clients

    # when
    working = (clients.keyed('vertex:vertex'), clients.get('vertex:vertex') is not None, inv.use_route('card'))
    found = c.app.state.vertex_connections.get('vertex')
    c.app.state.vertex_connections.get = lambda name: dataclasses.replace(found, key='', unreadable=True) if name == 'vertex' else None
    broken = (clients.keyed('vertex:vertex'), clients.get('vertex:vertex'))
    row = next(r for r in c.get('/api/admin/ai-uses', headers=AUTH).json()['uses'] if r['use'] == 'card')
    c.app.state.vertex_connections.get = lambda name: None
    gone = (clients.keyed('vertex:vertex'), clients.get('vertex:vertex'), clients.get('vertex'))

    # then
    assert working[:2] == (True, True) and working[2][:2] == ('vertex:vertex', REF)
    assert broken == (False, None) and row['problem'] == msg('ai.use_held', owner='vertex')
    assert gone[:2] == (False, None) and not isinstance(gone[2], vertex_ai.VertexAi)


def test_the_connection_survives_a_restart_and_a_lost_key_is_noticed(store: Store):
    # given
    first = TestClient(app(store))
    put(first)
    first.put('/api/admin/settings', json={'key': 'investigate.model', 'value': REF}, headers=AUTH)
    vertex_ai.unbind()
    ai.unregister_provider('vertex')
    second = app(store)

    # when
    booted = (second.state.vertex_connections.get('vertex').view()['credentials'], second.state.investigator.model)
    store.db['admin_secrets'].update_one({'_id': 'vertex.vertex'}, {'$set': {'value': 'sealed:not-readable'}})
    second.state.vertex_connections.refresh()
    notices = {n['id']: n['text'] for n in TestClient(second).get('/api/admin/notices', headers=AUTH).json()['items']}

    # then
    assert booted == ({'mode': 'key', 'set': True}, REF) and notices['ai-vertex:vertex'] == msg('vertex.unreadable', name='vertex') and second.state.vertex_connections.get('vertex').view()['credentials']['set'] is False


def test_test_connection_asks_each_model_once_forces_one_tool_call_and_confirms_the_region(store: Store):
    # given
    maker = Maker(message('ok'), message('', (('toolu_1', 'echo', {'text': 'ok'}),)), message('ok'))
    c = TestClient(app(store, maker))
    put(c, models=[{'id': 'opus', 'vertexId': 'claude-opus-5-5'}, {'id': 'haiku', 'vertexId': 'claude-haiku-5-5'}])

    # when
    result = c.post(f'{PATH}/vertex/test', json={}, headers=AUTH)
    view = c.get(f'{PATH}/vertex', headers=AUTH).json()

    # then
    out = result.json()
    assert result.status_code == 200 and (out['ok'], out['tools']) == (True, True) and 'regionOk' not in out and out['summary'] == msg('vertex.test_ok', count=2, region='eu')
    assert [m['id'] for m in out['models']] == ['opus', 'haiku'] and all(m['ok'] for m in out['models']) and isinstance(out['ms'], int)
    assert [r['path'].split('/models/')[1] for r in maker.requests] == ['claude-opus-5-5:rawPredict', 'claude-opus-5-5:rawPredict', 'claude-haiku-5-5:rawPredict']
    assert maker.requests[1]['body']['tool_choice'] == {'type': 'tool', 'name': 'echo'} and 'tools' not in maker.requests[0]['body']
    assert view['lastTest']['ok'] is True and view['lastTest']['summary'] == out['summary'] and view['lastTest']['at']
    assert maker.built[-1].key and 'fallbacks' not in json.dumps([r['body'] for r in maker.requests])


def test_test_connection_says_what_went_wrong_in_plain_words(store: Store):
    # given
    cases = [(401, 'vertex.test_rejected', {}), (403, 'vertex.test_forbidden', {'model': 'claude-opus-5-5', 'project': PROJECT}),
             (404, 'vertex.test_no_model', {'model': 'claude-opus-5-5', 'region': 'eu'}), (429, 'vertex.test_quota', {'region': 'eu'}), (500, 'vertex.test_status', {'status': 500})]
    c = TestClient(app(store, Maker(*[failure(s) for s, _, _ in cases])))
    put(c)
    texts = []

    # when
    for _ in cases:
        texts.append(c.post(f'{PATH}/vertex/test', json={}, headers=AUTH).json())

    # then
    assert [t['summary'] for t in texts] == [msg(key, **vals) for _, key, vals in cases]
    assert all(t['ok'] is False and MARK not in json.dumps(t) for t in texts)
    assert 'roles/aiplatform.user' in texts[1]['summary'] and 'global, us or eu' in texts[2]['summary']


def test_a_test_with_typed_values_is_not_saved_and_needs_the_key_again_for_a_new_project(store: Store):
    # given
    maker = Maker()
    c = TestClient(app(store, maker))
    put(c)

    # when
    typed = c.post(f'{PATH}/vertex/test', json={'region': 'global', 'credentials': {'mode': 'pod'}}, headers=AUTH)
    moved = c.post(f'{PATH}/vertex/test', json={'project': 'other-proj-1234'}, headers=AUTH)
    unknown = c.post(f'{PATH}/nothing/test', json={}, headers=AUTH)
    fresh = c.post(f'{PATH}/draft/test', json=body(region='us'), headers=AUTH)

    # then
    assert typed.status_code == 200 and maker.requests[0]['host'] == 'aiplatform.googleapis.com'
    assert c.get(f'{PATH}/vertex', headers=AUTH).json()['lastTest'] is None and c.get(f'{PATH}/vertex', headers=AUTH).json()['region'] == 'eu'
    assert (moved.status_code, moved.json()['detail']) == (422, msg('vertex.key_reenter')) and unknown.status_code == 404
    assert fresh.status_code == 200 and c.get(f'{PATH}/draft', headers=AUTH).status_code == 404


def test_a_failed_login_stops_the_test_and_skips_the_rest(store: Store):
    # given
    maker = Maker(failure(401))
    c = TestClient(app(store, maker))
    put(c, models=[{'id': 'a', 'vertexId': 'claude-opus-5-5'}, {'id': 'b', 'vertexId': 'claude-haiku-5-5'}])

    # when
    out = c.post(f'{PATH}/vertex/test', json={}, headers=AUTH).json()

    # then
    assert len(maker.requests) == 1 and [m['summary'] for m in out['models']] == [msg('vertex.test_rejected'), msg('vertex.test_skipped')]


def test_the_key_is_built_into_google_credentials_without_a_network_call():
    # given
    conn = connection_of(mode='key', key=vertex_ai.checked_key(service_key()))

    # when
    credentials = vertex_ai.google_credentials(conn, '/var/run/secrets')

    # then
    assert credentials.service_account_email == CLIENT_EMAIL and credentials.token is None and list(credentials.scopes) == vertex_ai.SCOPES


def test_a_federation_config_is_built_into_google_credentials_only_under_the_allowed_path(tmp_path):
    # given
    info = vertex_ai.checked_federation(federation(str(tmp_path / 'gcp')), str(tmp_path))
    conn = connection_of(mode='federation', federation=info)

    # when
    credentials = vertex_ai.google_credentials(conn, str(tmp_path))
    with pytest.raises(compat_ai.Refused):
        vertex_ai.google_credentials(conn, '/srv/other')

    # then
    assert credentials.token is None and vertex_ai.google_credentials(connection_of(), '/x') is None


def test_a_card_run_on_vertex_gets_web_search_but_no_page_reads_and_spends_by_the_vertex_prices(store: Store):
    # given
    import skopax.investigator as investigator_module
    from tests.conftest import anomaly
    from tests.test_investigator import KEY as CARD, NOW, seed
    seed(store, anomaly(CARD, firstSeen='2026-10-22T05:00:00Z'))
    maker = Maker(message('Checked checkout-service.', usage={'input_tokens': 2000, 'output_tokens': 100, 'cache_read_input_tokens': 1000}))
    c = TestClient(app(store, maker, investigate_mode='live'))
    put(c)
    picked = c.put('/api/admin/settings', json={'key': 'investigate.model', 'value': REF}, headers=AUTH).status_code
    inv = c.app.state.investigator
    seen = []
    real = investigator_module.web_from
    investigator_module.web_from = lambda domains, searches, fetches, own=(): seen.append((searches, fetches)) or real(domains, searches, fetches, own)

    # when
    try:
        record = inv.run_once(NOW)
    finally:
        investigator_module.web_from = real

    # then
    sent = maker.requests[0]['body']
    assert picked == 200 and seen == [(3, 0)] and (record['outcome'], record['model']) == ('done', REF)
    assert [t.get('type') for t in sent['tools'] if t.get('type')] == [SEARCH_TYPE] and not {'fallbacks', 'betas'} & set(sent)
    row = store.ledger.find_one({'runId': record['id']})
    assert (row['provider'], row['model'], 'pricedAs' in row) == ('vertex:vertex', REF, False) and row['usd']['total'] == pytest.approx(pricing.usd(REF, Usage(input=2000, cache_read=1000, output=100)))
    assert store.spent(clock.day(NOW)) > 0


def test_a_federation_token_path_swapped_for_a_symlink_later_is_refused_at_refresh_and_the_use_is_held(store: Store, tmp_path):
    # given
    from google.auth.exceptions import RefreshError
    root = tmp_path / 'tokens'
    root.mkdir()
    (root / 'gcp').write_text('token')
    outside = tmp_path / 'elsewhere'
    outside.write_text('stolen')
    c = TestClient(app(store, Maker(), vertex_token_dir=str(root)))
    saved = put(c, credentials={'mode': 'federation', 'config': federation(str(root / 'gcp'))})
    conn = c.app.state.vertex_connections.get('vertex')
    credentials = vertex_ai.google_credentials(conn, str(root))
    before = vertex_ai.factory('vertex:vertex') is not None

    # when
    (root / 'gcp').unlink()
    (root / 'gcp').symlink_to(outside)
    with pytest.raises(RefreshError) as refused:
        credentials.refresh(None)
    held = vertex_ai.factory('vertex:vertex')

    # then
    assert saved.status_code == 200 and before and held is None and str(root) in str(refused.value)
    assert vertex_ai.factory('vertex:vertex', 30.0, 1) is None


def test_a_refresh_under_the_allowed_path_goes_on_to_google_signing_in(tmp_path):
    # given
    root = tmp_path / 'tokens'
    root.mkdir()
    (root / 'gcp').write_text('token')
    info = vertex_ai.checked_federation(federation(str(root / 'gcp')), str(root))
    credentials = vertex_ai.google_credentials(connection_of(mode='federation', federation=info), str(root))
    sent = []
    from google.auth.exceptions import TransportError

    class Seen:
        def __call__(self, url, method='GET', body=None, headers=None, timeout=None, **kw):
            sent.append(url)
            raise TransportError('no network in tests')

    # when
    with pytest.raises(TransportError):
        credentials.refresh(Seen())

    # then
    assert sent == ['https://sts.googleapis.com/v1/token']


@pytest.mark.parametrize('vertex_id, wanted', [('claude-opus-5-5', {'type': 'adaptive'}), ('claude-haiku-5-5', {'type': 'disabled'})])
def test_a_once_call_with_thinking_on_never_sends_the_unconfirmed_between_tools_type(vertex_id, wanted):
    # given
    maker = Maker()
    held = vertex_ai.VertexAi(maker(connection_of(), '/var/run/secrets/tokens', 30, 0), 'vertex', {'m': vertex_id})

    # when
    held.once('vertex/m', 'S', [{'role': 'user', 'content': 'Q'}], 100, 'low', 'auto')
    held.once('vertex/m', 'S', [{'role': 'user', 'content': 'Q'}], 100, 'low', 'off')

    # then
    assert [r['body']['thinking'] for r in maker.requests] == [wanted, {'type': 'disabled'}]
    assert 'between_tools' not in json.dumps([r['body'] for r in maker.requests])


def test_a_model_removed_from_the_connection_holds_the_use_it_still_points_at_and_never_falls_back_to_the_card_model(store: Store):
    # given
    c = TestClient(app(store, Maker(), anthropic_api_key='sk-test', investigate_mode='live'))
    put(c, models=[{'id': 'claude-opus-5-5', 'vertexId': 'claude-opus-5-5'}, {'id': 'other', 'vertexId': 'claude-haiku-5-5'}])
    picked = [c.put('/api/admin/settings', json={'key': key, 'value': value}, headers=AUTH).status_code
              for key, value in (('investigate.model', 'claude-sonnet-5-5'), ('investigate.model-critical', REF), ('investigate.slack-model', REF))]
    inv = c.app.state.investigator
    before = inv.card_route({'crit'})

    # when
    put(c, credentials={'mode': 'key'}, models=[{'id': 'other', 'vertexId': 'claude-haiku-5-5'}])
    critical = inv.card_route({'crit'})
    slack = inv.use_route('slack')
    rows = {r['use']: r for r in c.get('/api/admin/ai-uses', headers=AUTH).json()['uses']}
    notices = {n['id']: n['text'] for n in c.get('/api/admin/notices', headers=AUTH).json()['items']}
    setting = next(i for i in c.get('/api/admin/settings', headers=AUTH).json()['items'] if i['key'] == 'investigate.model-critical')

    # then
    assert picked == [200, 200, 200] and before[:3] == ('vertex:vertex', REF, None)
    assert critical[:3] == ('vertex:vertex', REF, None) and critical[3] == 'critical' and slack == ('vertex:vertex', REF, None)
    assert rows['critical']['problem'] == msg('ai.model_unknown', model=REF) and rows['slack']['problem'] == msg('ai.model_unknown', model=REF)
    assert rows['card']['model'] == 'claude-sonnet-5-5' and rows['card']['problem'] is None
    assert notices['ai-use:critical'] == msg('ai.use_problem', use='Critical cards', problem=msg('ai.model_unknown', model=REF)) and 'ai-use:slack' in notices
    assert setting['problem'] == msg('ai.model_unknown', model=REF)


def test_the_page_kit_shapes_match_the_real_vertex_list_test_result_and_model_refs(store: Store):
    # given
    shapes = json.loads((Path(__file__).resolve().parent / 'page' / 'skopax-ai-shapes.json').read_text())
    c = TestClient(app(store, Maker(message('ok'), message('', (('toolu_1', 'echo', {'text': 'ok'}),)))))
    put(c, name='eu', models=[{'id': 'claude-opus-5-5', 'vertexId': 'claude-opus-5-5'}])
    c.put('/api/admin/settings', json={'key': 'investigate.slack-model', 'value': 'eu/claude-opus-5-5'}, headers=AUTH)

    # when
    tested = c.post(f'{PATH}/eu/test', json={}, headers=AUTH).json()
    listed = c.get(PATH, headers=AUTH).json()
    uses = c.get('/api/admin/ai-uses', headers=AUTH).json()['uses']
    settings_now = {i['key']: i for i in c.get('/api/admin/settings', headers=AUTH).json()['items']}

    # then
    item = listed['items'][0]
    assert sorted(listed) == shapes['vertexes'] and sorted(item) == shapes['vertex'] and sorted(item['credentials']) == shapes['vertexCredentials']
    assert all(sorted(m) == shapes['vertexModel'] for m in item['models'])
    assert sorted(tested) == shapes['vertexTest'] and tested['ok'] is True and all(sorted(m) == shapes['vertexTestModel'] for m in tested['models'])
    assert sorted(item['lastTest']) == shapes['vertexLast']
    assert [r for r in vertex_ai.REGIONS if r in shapes['vertexRegions']] == shapes['vertexRegions'] and listed['regions'] == list(vertex_ai.REGIONS)
    row = next(r for r in uses if r['use'] == 'slack')
    assert sorted(row) == shapes['uses'] and (row['provider'], row['model']) == ('vertex:eu', 'eu/claude-opus-5-5')
    assert settings_now['investigate.slack-model']['choiceGroups']['eu/claude-opus-5-5'] == 'eu'


@pytest.mark.parametrize('use,key', [('chat', 'chat.model'), ('gate', 'investigate.gate-model'), ('review', 'self-review.model'), ('plugin', 'website.model')])
def test_a_model_removed_from_the_connection_holds_every_other_use_too_and_sends_nothing_elsewhere(store: Store, use: str, key: str):
    # given
    maker = Maker()
    models = FakeModels()
    c = TestClient(create_app(dataclasses.replace(settings(), anthropic_api_key='sk-test', investigate_mode='live'), store, ai_factory=models, compat_factory=CompatMaker(), vertex_factory=maker))
    put(c, models=[{'id': 'claude-opus-5-5', 'vertexId': 'claude-opus-5-5'}, {'id': 'other', 'vertexId': 'claude-haiku-5-5'}])
    picked = c.put('/api/admin/settings', json={'key': key, 'value': REF}, headers=AUTH).status_code
    before = next(r for r in c.get('/api/admin/ai-uses', headers=AUTH).json()['uses'] if r['use'] == use)

    # when
    put(c, credentials={'mode': 'key'}, models=[{'id': 'other', 'vertexId': 'claude-haiku-5-5'}])
    after = next(r for r in c.get('/api/admin/ai-uses', headers=AUTH).json()['uses'] if r['use'] == use)
    notices = {n['id']: n['text'] for n in c.get('/api/admin/notices', headers=AUTH).json()['items']}
    setting = next(i for i in c.get('/api/admin/settings', headers=AUTH).json()['items'] if i['key'] == key)

    # then
    assert picked == 200 and (before['provider'], before['model'], before['problem']) == ('vertex:vertex', REF, None)
    assert (after['provider'], after['model'], after['problem']) == ('vertex:vertex', REF, msg('ai.model_unknown', model=REF))
    assert setting['value'] == REF
    assert maker.requests == []
    wanted = msg('ai.model_unknown', model=REF)
    held = notices.get(f'ai-plugin:{key}') if use == 'plugin' else notices.get(f'ai-use:{use}')
    assert held is not None and wanted in held


@pytest.mark.parametrize('kind', ['vertex', 'compat'])
def test_a_critical_model_on_a_deleted_connection_holds_and_never_falls_back_to_the_card_route(store: Store, public_dns, kind: str):
    # given
    maker = Maker()
    models = FakeModels()
    c = TestClient(create_app(dataclasses.replace(settings(), anthropic_api_key='sk-test', investigate_mode='live'), store, ai_factory=models, compat_factory=CompatMaker(), vertex_factory=maker))
    ref = REF if kind == 'vertex' else 'deepseek/deepseek-chat'
    if kind == 'vertex':
        put(c)
    else:
        c.put('/api/admin/ai-connections/deepseek', json=compat_connection(), headers=AUTH)
    picked = c.put('/api/admin/settings', json={'key': 'investigate.model-critical', 'value': ref}, headers=AUTH).status_code
    inv = c.app.state.investigator
    before = inv.card_route({'crit'})

    # when
    c.delete(f'{PATH}/vertex' if kind == 'vertex' else '/api/admin/ai-connections/deepseek', headers=AUTH)
    after = inv.card_route({'crit'})
    row = next(r for r in c.get('/api/admin/ai-uses', headers=AUTH).json()['uses'] if r['use'] == 'critical')
    notices = {n['id'] for n in c.get('/api/admin/notices', headers=AUTH).json()['items']}

    # then
    assert picked == 200 and before[1] == ref and before[3] == 'critical'
    assert after[1] == ref and after[2] is None and after[3] == 'critical' and after[0] not in ('anthropic', 'openai')
    assert row['model'] == ref and row['provider'] == after[0] and row['problem'] and 'ai-use:critical' in notices
    assert maker.requests == [] and models.asked == []


@pytest.mark.parametrize('field, mode, limit', [('key', 'key', 20000), ('config', 'federation', 8000)])
def test_a_key_or_config_longer_than_the_validator_allows_is_refused_by_the_request_model(store: Store, field: str, mode: str, limit: int):
    # given
    from pydantic import ValidationError
    from skopax.models import VertexCredentialsIn, VertexConnectionTestIn
    c = TestClient(app(store))

    # when
    fits = VertexCredentialsIn(mode=mode, **{field: 'x' * limit})
    fits_object = VertexCredentialsIn(mode=mode, **{field: {'k': 'x' * (limit - 10)}})
    refused = []
    for value in ('x' * (limit + 1), {'k': 'x' * limit}):
        try:
            VertexCredentialsIn(mode=mode, **{field: value})
        except ValidationError as e:
            refused.append('at most' in str(e))
    routed = [c.put(f'{PATH}/vertex', json=body(credentials={'mode': mode, field: v}), headers=AUTH).status_code for v in ('x' * (limit + 1), {'k': 'x' * limit})]
    tested = c.post(f'{PATH}/vertex/test', json={'credentials': {'mode': mode, field: 'x' * (limit + 1)}}, headers=AUTH).status_code

    # then
    assert getattr(fits, field) and getattr(fits_object, field)
    assert refused == [True, True] and routed == [422, 422] and tested == 422
    assert VertexConnectionTestIn(credentials={'mode': mode}).credentials.mode == mode


@pytest.mark.parametrize('root, fine', [('/var/run/secrets/tokens', True), ('/var/run', True), ('/', False), ('/var', False), ('/var/', False), ('tokens/dir', False), ('', False), ('/var/../run', False)])
def test_only_an_absolute_directory_of_at_least_two_parts_can_hold_federation_tokens(root: str, fine: bool):
    # given
    path = root.rstrip('/') + '/gcp'

    # when
    allowed = vertex_ai.token_dir_ok(root)

    # then
    assert allowed is fine
    assert vertex_ai.token_path_ok(path, root) is False or fine


def test_a_token_directory_that_is_too_wide_refuses_saves_holds_the_use_and_says_so(store: Store):
    # given
    c = TestClient(app(store, Maker(), vertex_token_dir='/var/run/secrets/tokens', investigate_mode='live', investigate_model=REF))
    saved = put(c, credentials={'mode': 'federation', 'config': federation('/var/run/secrets/tokens/gcp')})

    # when
    vertex_ai.BOUND.token_dir = lambda: '/'
    c.app.state.vertex_connections.token_dir = lambda: '/'
    wide = put(c, credentials={'mode': 'federation', 'config': federation('/etc/passwd')})
    held = vertex_ai.factory('vertex:vertex')
    notices = {n['id']: n['text'] for n in c.get('/api/admin/notices', headers=AUTH).json()['items']}

    # then
    assert saved.status_code == 200 and wide.status_code == 422 and held is None
    assert notices['ai-vertex-token:vertex'] == msg('vertex.token_dir_invalid', name='vertex')


def test_a_name_stored_as_both_a_vertex_and_a_compat_connection_is_left_out_of_both_and_noticed(store: Store, public_dns):
    # given
    c = TestClient(app(store))
    put(c, name='eu')
    store.db['admin_secrets'].insert_one({'_id': 'compat.eu', 'baseUrl': 'https://api.example.com/v1', 'models': [{'id': 'm'}], 'tools': True})

    # when
    c.app.state.ai_connections.refresh()
    c.app.state.vertex_connections.refresh()
    notices = {n['id']: n['text'] for n in c.get('/api/admin/notices', headers=AUTH).json()['items']}
    listed = c.get(PATH, headers=AUTH).json()['items']
    compat_listed = c.get('/api/admin/ai-connections', headers=AUTH).json()['items']
    removed = c.delete('/api/admin/ai-connections/eu', headers=AUTH).status_code
    c.app.state.vertex_connections.refresh()
    after = [v['name'] for v in c.get(PATH, headers=AUTH).json()['items']]
    cleared = {n['id'] for n in c.get('/api/admin/notices', headers=AUTH).json()['items']}

    # then
    assert listed == [] and compat_listed == [] and notices['ai-name-clash:eu'] == msg('vertex.name_clash', name='eu')
    assert removed == 204 and after == ['eu'] and 'ai-name-clash:eu' not in cleared
