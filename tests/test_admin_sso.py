import base64
import copy
import dataclasses
import hashlib
import json
import logging
import threading
import time

import pytest
from fastapi.testclient import TestClient
from pymongo.errors import AutoReconnect

from skopax import oidc, sso
from skopax.apitokens import ApiTokens
from skopax.app import create_app
from skopax.auth import LOGIN_COOKIE, SESSION_COOKIE
from skopax.messages import MESSAGES, msg
from skopax.sessions import Sessions, session_secret
from skopax.sso import FIELDS, InvalidSecret, SsoHttp
from skopax.store import Store
from skopax.tunables import SESSIONS_AFTER_KEY
from skopax.users import Users
from tests.conftest import ADMIN_EMAIL, AUTH
from tests.sso_kit import OSCAR, REDIRECT, SECRET, FakeIdp, Listener, app_client, idp_settings, log_in, scripted_server, server_url, stop_server
from tests.test_auth import Clock
from tests.test_sso_config import authorize_at, endpoint_of, me, second_idp, two_idps

SSO = '/api/admin/sso'
TEST = '/api/admin/sso/test'
SETTINGS = '/api/admin/settings'
NOTICES = '/api/admin/notices'
CHECKS = ['discovery', 'issuer', 'pkce', 'keys', 'client-auth', 'redirect', 'scope']
PASSWORD = 'correct horse battery'
ALICE = 'alice@example.com'
MARKER = 'marker-from-the-idp-body'


def sso_messages() -> dict:
    return {k: v for k, v in MESSAGES.items() if k.startswith('sso.')}


def client_of(store: Store, s, http: SsoHttp, clock=None) -> TestClient:
    return TestClient(create_app(s, store, sso_http=http, clock=clock), base_url='https://testserver')


def view(client: TestClient) -> dict:
    return client.get(SSO, headers=AUTH).json()


def save(client: TestClient, headers=AUTH, **body):
    return client.put(SSO, json=body, headers=headers)


def changes(store: Store) -> list[tuple]:
    return sorted(((c['key'], c['old'], c['new']) for c in store.db['tunable_changes'].find({})), key=repr)


def access_lines(store: Store) -> list[str]:
    return [a['text'] for a in store.activity.find({'kind': 'access'})]


def check(res, check_id: str) -> dict:
    return next(c for c in res.json()['checks'] if c['id'] == check_id)


def switched_off_logs(caplog) -> int:
    return sum('single sign-on was switched off' in r.getMessage() for r in caplog.records)


def test_admin_reads_and_saves_sso_with_validation(store: Store):
    # given
    idp, idp2 = FakeIdp(), second_idp()
    client = client_of(store, idp_settings(idp), two_idps(idp, idp2))

    # when
    first = client.get(SSO, headers=AUTH)
    refused = save(client, values={'issuer': 'http://idp.example.com', 'scope': 'openid email'})
    unknown = save(client, values={'audience': 'skopax'})
    too_long = save(client, values={'scope': 'openid ' + 'x' * 4000})
    owned = save(client, values={'scope': 'openid email', 'client-secret': 'another-secret'})
    after_refusals = view(client)
    refused_writes = changes(store)
    saved = save(client, values={'issuer': idp2.issuer})
    after_save = view(client)
    moved = authorize_at(client)
    reset = save(client, values={'issuer': None})
    back = view(client)

    # then
    body = first.json()
    assert first.status_code == 200
    assert set(body['values']) == set(FIELDS) - {'client-secret'}
    assert body['values']['issuer'] == {'value': idp.issuer, 'source': 'config', 'from': 'environment'}
    assert body['values']['scope'] == {'value': 'openid email profile', 'source': 'default', 'from': None}
    assert (body['enabled'], body['active'], body['problem'], body['redirectUri']) == (True, True, None, REDIRECT)
    assert body['provider'] == {'ok': True, 'since': None, 'error': None}
    assert body['secret'] == {'set': True, 'from': 'environment', 'unreadable': False, 'updatedBy': None, 'updatedAt': None}
    assert SECRET not in first.text
    assert (refused.status_code, refused.json()) == (422, {'detail': msg('sso.url_scheme', field='Issuer URL'), 'field': 'issuer'})
    assert (unknown.status_code, [d['loc'] for d in unknown.json()['detail']]) == (422, [['body', 'values']])
    assert msg('sso.unknown_field', fields='audience') in unknown.json()['detail'][0]['msg']
    assert (too_long.status_code, [d['loc'] for d in too_long.json()['detail']]) == (422, [['body', 'values', 'scope']])
    assert (owned.status_code, owned.json()['detail']) == (409, msg('sso.secret_source_owned', source='environment'))
    assert after_refusals == body and refused_writes == [] and store.db['admin_secrets'].count_documents({}) == 0
    assert saved.status_code == 200 and saved.json() == after_save
    assert saved.json()['values']['issuer'] == {'value': idp2.issuer, 'source': 'admin', 'from': None}
    assert endpoint_of(moved) == idp2.discovery['authorization_endpoint']
    assert reset.status_code == 200 and back['values']['issuer'] == body['values']['issuer']
    assert endpoint_of(authorize_at(client)) == idp.discovery['authorization_endpoint']


def test_save_and_turn_on_refuses_a_config_with_a_problem(store: Store):
    # given
    idp = FakeIdp()
    client = app_client(store, idp, idp_settings(idp, sso_client_secret='', sso_on=False))
    problem = msg('sso.problem.secret', field='sso.client-secret')

    # when
    before = view(client)
    refused = save(client, values={'scope': 'openid email'}, enabled=True)
    after = view(client)
    refused_writes = changes(store)
    secrets_after_refusal = store.db['admin_secrets'].count_documents({})
    turned_on = save(client, values={'client-secret': 'a-real-secret-value'}, enabled=True)

    # then
    assert (before['enabled'], before['active'], before['problem']) == (False, False, problem)
    assert (refused.status_code, refused.json()['detail']) == (422, problem)
    assert after == before and refused_writes == [] and secrets_after_refusal == 0
    assert turned_on.status_code == 200 and (turned_on.json()['enabled'], turned_on.json()['active'], turned_on.json()['problem']) == (True, True, None)
    assert authorize_at(client).startswith(idp.discovery['authorization_endpoint'])


def test_the_sso_secret_never_leaves_the_server(store: Store, caplog):
    # given
    caplog.set_level(logging.DEBUG)
    secret = 's3cret-' + 'x' * 24
    hashed = hashlib.sha256(secret.encode()).hexdigest()
    idp = FakeIdp()
    client = app_client(store, idp, idp_settings(idp, sso_client_secret=''))
    signon = client.app.state.signon
    hooked, raised = [], []
    signon.token_request = lambda request: hooked.append(copy.deepcopy(request)) or {}
    finish = signon.finish

    def watched(*args, **kwargs):
        try:
            return finish(*args, **kwargs)
        except Exception as e:
            raised.append(e)
            raise

    signon.finish = watched

    # when
    saved = save(client, values={'client-secret': secret})
    shown = client.get(SSO, headers=AUTH)
    idp.status = 401
    start = client.get('/login/sso', follow_redirects=False)
    pending = client.cookies.get(LOGIN_COOKIE)
    params = idp.authorize(start.headers['location'])
    login = client.get('/authorized', params={'code': 'auth-code', 'state': params['state']}, follow_redirects=False)
    tested = client.post(TEST, json={}, headers=AUTH)
    idp.fail = True
    failed = client.post(TEST, json={'values': {'client-secret': ''}}, headers=AUTH)
    history = client.get('/api/admin/history', headers=AUTH)
    activity = client.get('/api/activity', headers=AUTH)

    # then
    assert saved.status_code == 200 and shown.json()['secret']['set'] is True
    assert idp.exchanges and idp.exchanges[-1]['data']['client_secret'] == secret
    assert login.status_code >= 400 and raised and tested.json()['ok'] is True and failed.json()['ok'] is False
    for res in (saved, shown, start, login, tested, failed, history, activity):
        assert secret not in res.text and hashed not in res.text and secret not in str(res.headers)
    body = pending.rsplit('.', 1)[0]
    decoded = base64.urlsafe_b64decode(body + '=' * (-len(body) % 4)).decode()
    assert 'state' in decoded and secret not in decoded and hashed not in decoded and secret not in pending
    sealed = store.db['admin_secrets'].find_one({'_id': 'sso.client-secret'})
    assert sealed['value'].startswith('enc:v1:') and secret not in str(sealed)
    assert access_lines(store) and all(secret not in str(a) for a in store.activity.find({}))
    rows = list(store.db['tunable_changes'].find({}))
    assert [(r['key'], r['old'], r['new']) for r in rows] == [('sso.client-secret', None, 'set')] and secret not in str(rows)
    assert caplog.records and not [r for r in caplog.records if secret in r.getMessage() or secret in str(r.args) or secret in (r.exc_text or '')]
    assert hooked and secret not in json.dumps(hooked)
    assert all(secret not in str(e) and secret not in repr(e) for e in raised)
    assert not any(secret in url for url in idp.calls)


def test_the_basic_auth_value_and_token_headers_never_leave_the_server(store: Store, caplog):
    # given
    caplog.set_level(logging.DEBUG)
    secret = 'b4sic-' + 'y' * 24
    gateway = 'gateway-' + 'z' * 24
    authorization = sso.basic_auth('skopax', secret)
    elsewhere = Listener()
    refused = (401, {}, ('{"error": "invalid_client", "echo": "' + authorization + ' ' + gateway + '"}').encode())
    moved = (302, {'Location': elsewhere.url + '/elsewhere/token'}, b'{}')
    token = scripted_server([refused, moved])
    idp = FakeIdp()
    idp.discovery['token_endpoint'] = server_url(token) + '/token'
    s = idp_settings(idp, sso_client_secret=secret, sso_client_auth='basic', sso_token_headers=f'X-Gateway: {gateway}')
    client = client_of(store, s, SsoHttp(fetch=idp.fetch))
    signon = client.app.state.signon
    raised = []
    finish = signon.finish

    def watched(*args, **kwargs):
        try:
            return finish(*args, **kwargs)
        except Exception as e:
            raised.append(e)
            raise

    signon.finish = watched

    # when
    first = log_in(client, idp)
    second = log_in(client, idp)
    shown = client.get(SSO, headers=AUTH)
    seen = list(token.seen)
    stop_server(token)
    elsewhere.close()

    # then
    assert (first.status_code, second.status_code) == (401, 401)
    assert [(e.reason, e.detail) for e in raised] == [('exchange', 'status 401'), ('exchange', 'status 302')]
    assert len(seen) == 2 and all({k.lower(): v for k, v in r['headers'].items()}['authorization'] == authorization for r in seen)
    assert elsewhere.accepted == 0
    hidden = (secret, authorization, authorization.split()[1], gateway)
    for res in (first, second, shown):
        assert not [h for h in hidden if h in res.text or h in str(res.headers)]
    assert caplog.records and not [r for r in caplog.records for h in hidden if h in r.getMessage() or h in str(r.args) or h in (r.exc_text or '')]
    assert not [e for e in raised for h in hidden if h in str(e) or h in repr(e) or h in repr(e.__cause__) or h in repr(e.__context__)]


def failing_fetch(idp: FakeIdp, monkeypatch, case: str, ended: threading.Event):
    if case == 'unreachable':
        idp.fail = True
    elif case == 'issuer':
        idp.discovery['issuer'] = f'https://other.example.com/{MARKER}'
    elif case == 's256':
        idp.discovery['code_challenge_methods_supported'] = ['plain']
    elif case == 'empty jwks':
        idp.jwks = lambda: {'keys': [], 'note': MARKER}
    elif case == 'unlisted auth':
        idp.discovery['token_endpoint_auth_methods_supported'] = ['private_key_jwt']
    elif case == 'huge':
        def opener(request, timeout, watch):
            return Body(b'{"pad": "' + MARKER.encode() + b'x' * (3 << 20) + b'"}')

        monkeypatch.setattr(oidc, 'open_url', opener)
        return oidc.get_json
    elif case == 'slow':
        monkeypatch.setattr(sso, 'TEST_TIMEOUT_S', 0.2)

        def slow(url: str) -> dict:
            time.sleep(1)
            ended.set()
            return idp.fetch(url)

        return slow
    elif case == 'crash':
        def crash(url: str) -> dict:
            raise RuntimeError(MARKER)

        return crash
    idp.discovery['note'] = MARKER
    return idp.fetch


class Body:
    def __init__(self, data: bytes):
        self.data = data
        self.at = 0

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self, size: int = -1) -> bytes:
        part = self.data[self.at:self.at + size]
        self.at += len(part)
        return part


TEST_CASES = {
    'ok': (None, None, {}),
    'unreachable': ('discovery', 'sso.test.discovery_unreachable', {}),
    'issuer': ('issuer', 'sso.test.issuer_mismatch', {}),
    's256': ('pkce', 'sso.test.pkce_missing', {}),
    'empty jwks': ('keys', 'sso.test.keys_none', {}),
    'unlisted auth': ('client-auth', 'sso.test.client_auth_unlisted', {'method': 'client_secret_post'}),
    'huge': ('discovery', 'sso.test.discovery_invalid', {}),
    'slow': ('discovery', 'sso.test.timeout', {'seconds': '0.2'}),
    'crash': ('discovery', 'sso.test.stopped', {}),
}


@pytest.mark.parametrize('case', list(TEST_CASES))
def test_test_connection_reports_each_check_with_fixed_texts_and_never_500s(store: Store, monkeypatch, case: str):
    # given
    idp, ended = FakeIdp(), threading.Event()
    fetch = failing_fetch(idp, monkeypatch, case, ended)
    client = client_of(store, idp_settings(idp), SsoHttp(fetch=fetch, exchange=idp.exchange))
    failing, key, values = TEST_CASES[case]

    # when
    res = client.post(TEST, json={}, headers=AUTH)
    if case == 'slow':
        ended.wait(5)

    # then
    body = res.json()
    assert res.status_code == 200
    assert [c['id'] for c in body['checks']] == CHECKS
    assert isinstance(body['ms'], int) and body['ms'] >= 0
    assert MARKER not in res.text and SECRET not in res.text
    assert idp.exchanges == []
    if failing is None:
        assert body['ok'] is True and all(c['ok'] for c in body['checks'])
        assert body['summary'] == msg('sso.test.ok')
        assert check(res, 'keys')['text'] == msg('sso.test.keys_ok', count=1)
        assert check(res, 'client-auth')['text'] == msg('sso.test.client_auth_ok', method='client_secret_post')
        assert check(res, 'redirect')['text'] == msg('sso.test.redirect_ok', uri=REDIRECT)
        return
    expected = msg(key, **values)
    assert body['ok'] is False and check(res, failing) == {'id': failing, 'ok': False, 'text': expected}
    assert body['summary'] == msg('sso.test.failed', count=sum(not c['ok'] for c in body['checks']))
    assert check(res, 'redirect')['ok'] is True and check(res, 'scope')['ok'] is True


def test_test_connection_uses_typed_values_and_the_stored_secret(store: Store):
    # given
    idp, idp2 = FakeIdp(), second_idp()
    client = client_of(store, idp_settings(idp, sso_client_secret=''), two_idps(idp, idp2))

    # when
    no_secret = client.post(TEST, json={'values': {'client-secret': ''}}, headers=AUTH)
    typed_secret = client.post(TEST, json={'values': {'client-secret': 'typed-secret-value'}}, headers=AUTH)
    save(client, values={'client-secret': 'stored-secret-value'})
    authorize_at(client)
    live_provider = client.app.state.signon.provider()
    before = dict(idp.calls)
    typed = client.post(TEST, json={'values': {'issuer': idp2.issuer, 'client-secret': ''}}, headers=AUTH)
    typed_bad = client.post(TEST, json={'values': {'issuer': 'http://idp2.example.com'}}, headers=AUTH)
    after = view(client)

    # then
    assert check(no_secret, 'client-auth') == {'id': 'client-auth', 'ok': False, 'text': msg('sso.test.client_auth_no_secret')}
    assert check(typed_secret, 'client-auth')['ok'] is True
    assert typed.status_code == 200 and typed.json()['ok'] is True
    assert check(typed, 'client-auth') == {'id': 'client-auth', 'ok': True, 'text': msg('sso.test.client_auth_ok', method='client_secret_post')}
    assert idp2.calls.get(idp2.discovery_url) == 1 and idp.calls == before and client.app.state.signon.provider() is live_provider
    assert (typed_bad.status_code, typed_bad.json()) == (422, {'detail': msg('sso.url_scheme', field='Issuer URL'), 'field': 'issuer'})
    assert after['values']['issuer']['value'] == idp.issuer and store.db['tunable_changes'].count_documents({'key': 'sso.issuer'}) == 0


def operator_cookie(store: Store, s) -> str:
    Users(store.db).create('bob@example.com', 'Bob', 'operator')
    return Sessions(session_secret(store.db, s), 3600, 7200).issue('bob@example.com', 'operator', 'local')


def test_an_sso_change_writes_history_and_activity(store: Store):
    # given
    idp, idp2 = FakeIdp(), second_idp()
    s = idp_settings(idp, sso_client_secret='')
    client = client_of(store, s, two_idps(idp, idp2))
    operator = client_of(store, s, two_idps(idp, idp2))
    operator.cookies.set(SESSION_COOKIE, operator_cookie(store, s))

    # when
    nothing = save(client)
    lines_after_nothing = access_lines(store)
    saved = save(client, values={'issuer': idp2.issuer, 'scope': 'openid email profile groups', 'client-secret': 'stored-secret-value', 'claims': 'id-token'})
    switched = save(client, enabled=False)
    admin_view = [(a['kind'], a['text']) for a in client.get('/api/activity', headers=AUTH).json()]
    operator_view = [a['text'] for a in operator.get('/api/activity').json()]
    history = client.get('/api/admin/history', headers=AUTH).json()
    issuer_row = next(h for h in history if h['key'] == 'sso.issuer')
    rows, lines = changes(store), access_lines(store)
    reverted = client.delete(SETTINGS, params={'key': issuer_row['key']}, headers=AUTH) if issuer_row['old'] is None else None

    # then
    assert nothing.status_code == 200 and lines_after_nothing == []
    assert saved.status_code == 200 and switched.status_code == 200
    assert rows == sorted([('sso.client-secret', None, 'set'), ('sso.enabled', None, 'false'), ('sso.issuer', None, idp2.issuer),
                           ('sso.scope', None, 'openid email profile groups')], key=repr)
    saved_line = msg('sso.saved', fields=', '.join(['Issuer URL', msg('sso.secret_label'), 'Scope']))
    assert ('access', saved_line) in admin_view and ('access', msg('sso.switched_off')) in admin_view
    assert lines == [saved_line, msg('sso.switched_off')]
    assert operator.get('/api/me').json()['user'] == 'bob@example.com'
    assert saved_line not in operator_view and msg('sso.switched_off') not in operator_view
    assert reverted is not None and reverted.status_code == 200
    assert view(client)['values']['issuer'] == {'value': idp.issuer, 'source': 'config', 'from': 'environment'}


def test_turning_sso_off_needs_a_way_back_in(store: Store, caplog):
    # given
    caplog.set_level(logging.WARNING)
    idp, clock = FakeIdp(), Clock()
    users = Users(store.db)
    users.create('dave@example.com', 'Dave', 'admin', password=PASSWORD)
    users.update('dave@example.com', {'disabled': True})
    users.create('erin@example.com', 'Erin', 'admin', password=PASSWORD)
    users.update('erin@example.com', {'source': 'sso'})
    client = app_client(store, idp, clock=clock)
    log_in(client, idp, sub=ALICE, email=ALICE, name='Alice', roles=['SKOPAX_ADMIN'])
    old_cookie = client.cookies.get(SESSION_COOKIE)

    # when
    who = me(client)
    by_route = save(client, headers={}, values={'scope': 'openid email'}, enabled=False)
    by_settings = client.put(SETTINGS, json={'key': 'sso.enabled', 'value': 'false'})
    still = view(client)
    refused_writes = changes(store)
    logs_after_refusals = switched_off_logs(caplog)
    clock.advance(5)
    by_token = save(client, enabled=False)
    logs_after_token = switched_off_logs(caplog)
    clock.advance(31)
    ended = me(client)
    save(client, enabled=True)
    clock.advance(5)
    stale = TestClient(client.app, base_url='https://testserver')
    stale.cookies.set(SESSION_COOKIE, old_cookie)
    stale_after_on = me(stale)
    log_in(client, idp, sub=ALICE, email=ALICE, name='Alice', roles=['SKOPAX_ADMIN'])
    users.create('frank@example.com', 'Frank', 'admin', password=PASSWORD)
    with_local = save(client, headers={}, enabled=False)
    save(client, enabled=True)
    clock.advance(5)
    log_in(client, idp, sub=ALICE, email=ALICE, name='Alice', roles=['SKOPAX_ADMIN'])
    with_local_settings = client.put(SETTINGS, json={'key': 'sso.enabled', 'value': 'false'})

    # then
    refusal = msg('sso.off_needs_local_admin')
    assert who == ALICE
    assert (by_route.status_code, by_route.json()['detail']) == (409, refusal)
    assert (by_settings.status_code, by_settings.json()['detail']) == (409, refusal)
    assert still['enabled'] is True and refused_writes == [] and logs_after_refusals == 0
    assert by_token.status_code == 200 and by_token.json()['enabled'] is False and logs_after_token == 1
    assert ended is None and stale_after_on is None
    assert with_local.status_code == 200 and with_local.json()['enabled'] is False
    assert with_local_settings.status_code == 200 and view(client)['enabled'] is False


def test_an_sso_made_api_token_never_counts_as_the_way_back_in(store: Store):
    # given
    idp, clock = FakeIdp(), Clock()
    client = app_client(store, idp, clock=clock)
    log_in(client, idp, sub=ALICE, email=ALICE, name='Alice', roles=['SKOPAX_ADMIN'])
    made = client.post('/api/admin/tokens', json={'name': 'sso-made', 'role': 'admin'})
    plain, _ = ApiTokens(store.db).create('password-made', 'admin', ADMIN_EMAIL)
    sso_made = {'Authorization': 'Bearer ' + made.json()['token']}

    # when
    by_route = save(client, headers=sso_made, enabled=False)
    by_settings = client.put(SETTINGS, json={'key': 'sso.enabled', 'value': 'false'}, headers=sso_made)
    still = view(client)['enabled']
    by_plain = save(client, headers={'Authorization': 'Bearer ' + plain}, enabled=False)
    save(client, enabled=True)
    clock.advance(5)
    by_bootstrap = client.put(SETTINGS, json={'key': 'sso.enabled', 'value': 'false'}, headers=AUTH)

    # then
    refusal = msg('sso.off_needs_local_admin')
    assert made.status_code == 201 and store.db['api_tokens'].find_one({'name': 'sso-made'})['via'] == 'sso'
    assert (by_route.status_code, by_route.json()['detail']) == (409, refusal)
    assert (by_settings.status_code, by_settings.json()['detail']) == (409, refusal)
    assert still is True
    assert by_plain.status_code == 200 and by_plain.json()['enabled'] is False
    assert by_bootstrap.status_code == 200 and view(client)['enabled'] is False
    assert 'bootstrap token' in refusal and 'API token made from a password session' in refusal


def test_a_reset_that_turns_sso_off_needs_a_way_back_in_too(store: Store):
    # given
    idp = FakeIdp()
    client = app_client(store, idp, idp_settings(idp, sso_on=False))
    client.put(SETTINGS, json={'key': 'sso.enabled', 'value': 'true'}, headers=AUTH)
    log_in(client, idp, sub=ALICE, email=ALICE, name='Alice', roles=['SKOPAX_ADMIN'])
    sso_made = {'Authorization': 'Bearer ' + client.post('/api/admin/tokens', json={'name': 'sso-made', 'role': 'admin'}).json()['token']}

    # when
    who = me(client)
    reset = client.delete(SETTINGS, params={'key': 'sso.enabled'})
    by_sso_made = client.delete(SETTINGS, params={'key': 'sso.enabled'}, headers=sso_made)
    still = view(client)['enabled']
    Users(store.db).set_password(ADMIN_EMAIL, PASSWORD)
    allowed = client.delete(SETTINGS, params={'key': 'sso.enabled'})

    # then
    assert who == ALICE
    assert (reset.status_code, reset.json()['detail']) == (409, msg('sso.off_needs_local_admin')) and still is True
    assert (by_sso_made.status_code, by_sso_made.json()['detail']) == (409, msg('sso.off_needs_local_admin'))
    assert allowed.status_code == 200 and view(client)['enabled'] is False


def test_generic_settings_writes_validate_sso_and_mcp_keys(store: Store):
    # given
    idp = FakeIdp()
    client = app_client(store, idp)
    bad = {'sso.issuer': ('http://idp.example.com', msg('sso.url_scheme', field='Issuer URL')),
           'sso.claims': ('userinfo', 'Claims come from must be one of id-token, access-token'),
           'sso.client-auth': ('jwt', 'Client authentication must be one of auto, post, basic'),
           'sso.default-role': ('admin', 'Default role must be one of viewer, operator'),
           'sso.role-map': ('[{"value": "ops-admins"}]', msg('sso.role_map_invalid', reason=msg('sso.role_map.pair', entry=1))),
           'sso.enabled': ('maybe', 'Single sign-on must be true or false'),
           'sso.require-verified-email': ('yes', 'Require verified email must be true or false'),
           'mcp.wait-seconds': ('99', 'MCP wait (seconds) must be between 0 and 55'),
           'mcp.dive-cap': ('many', 'MCP investigations per person per hour must be a whole number')}

    # when
    refused = {key: client.put(SETTINGS, json={'key': key, 'value': value}, headers=AUTH) for key, (value, _) in bad.items()}
    secret_put = client.put(SETTINGS, json={'key': 'sso.client-secret', 'value': 'another-secret'}, headers=AUTH)
    secret_reset = client.delete(SETTINGS, params={'key': 'sso.client-secret'}, headers=AUTH)
    refused_writes = changes(store)
    scope = client.put(SETTINGS, json={'key': 'sso.scope', 'value': 'email'}, headers=AUTH)
    parked = view(client)
    login = client.get('/login/sso', follow_redirects=False)
    wait = client.put(SETTINGS, json={'key': 'mcp.wait-seconds', 'value': '10'}, headers=AUTH)
    role_map = client.put(SETTINGS, json={'key': 'sso.role-map', 'value': '[{"value": "ops-admins", "role": "admin"}]'}, headers=AUTH)

    # then
    assert {key: (res.status_code, res.json()['detail']) for key, res in refused.items()} == {key: (422, text) for key, (_, text) in bad.items()}
    unknown = 'no setting named sso.client-secret on the admin page'
    assert (secret_put.status_code, secret_put.json()['detail']) == (404, unknown) and (secret_reset.status_code, secret_reset.json()['detail']) == (404, unknown)
    assert refused_writes == [] and store.db['admin_secrets'].count_documents({}) == 0
    assert scope.status_code == 200
    assert (parked['enabled'], parked['active'], parked['problem']) == (True, False, msg('sso.problem.scope', field='sso.scope'))
    assert login.status_code != 302
    assert wait.status_code == 200 and role_map.status_code == 200


def test_discovery_failure_shows_as_an_admin_notice(store: Store):
    # given
    idp, clock = FakeIdp(), Clock()
    client = app_client(store, idp, clock=clock)

    def provider_notices() -> list[dict]:
        return [n for n in client.get(NOTICES, headers=AUTH).json()['items'] if n['id'] == 'sso-provider']

    # when
    authorize_at(client)
    healthy = provider_notices()
    idp.fail = True
    clock.advance(oidc.DISCOVERY_TTL_S + 1)
    stale_login = client.get('/login/sso', follow_redirects=False)
    calls = dict(idp.calls)
    failing = provider_notices()
    calls_after_notices = dict(idp.calls)
    provider = view(client)['provider']
    since = provider['since']
    save(client, enabled=False)
    off = provider_notices()

    # then
    assert healthy == []
    assert stale_login.status_code == 302 and (provider['ok'], provider['error']) == (False, 'discovery')
    assert failing == [{'id': 'sso-provider', 'level': 'warn', 'text': msg('sso.notice.provider'), 'at': since}] and since
    assert since not in failing[0]['text'] and '{' not in failing[0]['text']
    assert calls_after_notices == calls
    assert off == []


def test_a_whitespace_secret_is_refused_and_only_empty_clears(store: Store):
    # given
    idp = FakeIdp()
    client = app_client(store, idp, idp_settings(idp, sso_client_secret=''))
    save(client, values={'client-secret': 'stored-secret-value'})

    # when
    blank = save(client, values={'client-secret': '   '})
    blank_test = client.post(TEST, json={'values': {'client-secret': ' \t '}}, headers=AUTH)
    kept = view(client)['secret']['set']
    rows_before_clear = store.db['tunable_changes'].count_documents({})
    cleared = save(client, values={'client-secret': ''})

    # then
    assert (blank.status_code, blank.json()) == (422, {'detail': msg('sso.secret_invalid'), 'field': 'client-secret'})
    assert (blank_test.status_code, blank_test.json()) == (422, {'detail': msg('sso.secret_invalid'), 'field': 'client-secret'})
    assert kept is True and rows_before_clear == 1
    assert cleared.status_code == 200 and cleared.json()['secret']['set'] is False
    assert changes(store) == sorted([('sso.client-secret', None, 'set'), ('sso.client-secret', 'set', None)], key=repr)


def test_test_connection_with_manual_endpoints_or_unlisted_pkce(store: Store):
    # given
    idp = FakeIdp()
    manual = app_client(store, idp, idp_settings(idp, sso_authorize_url=idp.discovery['authorization_endpoint'], sso_token_url=idp.discovery['token_endpoint'],
                                                 sso_jwks_url=idp.jwks_url))
    unlisted_idp = second_idp()
    del unlisted_idp.discovery['code_challenge_methods_supported']
    unlisted = app_client(store, unlisted_idp)

    # when
    by_hand = manual.post(TEST, json={}, headers=AUTH)
    listed_none = unlisted.post(TEST, json={}, headers=AUTH)

    # then
    assert by_hand.json()['ok'] is True and idp.calls == {idp.jwks_url: 1}
    assert check(by_hand, 'discovery') == {'id': 'discovery', 'ok': True, 'text': msg('sso.test.discovery_manual')}
    assert check(by_hand, 'issuer') == {'id': 'issuer', 'ok': True, 'text': msg('sso.test.issuer_manual')}
    assert check(by_hand, 'pkce') == {'id': 'pkce', 'ok': True, 'text': msg('sso.test.pkce_unlisted')}
    assert listed_none.json()['ok'] is True and unlisted_idp.calls.get(unlisted_idp.discovery_url) == 1
    assert check(listed_none, 'pkce') == {'id': 'pkce', 'ok': True, 'text': msg('sso.test.pkce_unlisted')}
    assert check(listed_none, 'issuer')['text'] == msg('sso.test.issuer_ok')


def test_a_second_test_connection_while_one_runs_is_busy(store: Store, monkeypatch):
    # given
    monkeypatch.setattr(sso, 'TEST_TIMEOUT_S', 0.2)
    idp, release, done = FakeIdp(), threading.Event(), threading.Event()
    held = [True]

    def fetch(url: str) -> dict:
        if held[0]:
            held[0] = False
            release.wait(5)
            done.set()
        return idp.fetch(url)

    client = client_of(store, idp_settings(idp), SsoHttp(fetch=fetch, exchange=idp.exchange))
    signon = client.app.state.signon

    # when
    first = client.post(TEST, json={}, headers=AUTH)
    busy = client.post(TEST, json={}, headers=AUTH)
    release.set()
    done.wait(5)
    ends = time.monotonic() + 5
    while signon.testing.locked() and time.monotonic() < ends:
        time.sleep(0.01)
    after = client.post(TEST, json={}, headers=AUTH)

    # then
    assert first.status_code == 200 and check(first, 'discovery')['text'] == msg('sso.test.timeout', seconds='0.2')
    assert (busy.status_code, busy.json()) == (409, {'detail': msg('sso.test_busy')})
    assert after.status_code == 200 and after.json()['ok'] is True


def test_a_database_error_mid_save_still_clears_the_mcp_cache_and_notes_what_was_saved(store: Store, monkeypatch):
    # given
    idp = FakeIdp()
    client = app_client(store, idp)
    live = client.app.state.live
    mcp_auth = client.app.state.mcp_auth
    real = live.set
    calls = []

    def failing(key, value, by):
        calls.append(key)
        if len(calls) == 2:
            raise AutoReconnect('the database went away')
        return real(key, value, by)

    monkeypatch.setattr(live, 'set', failing)
    mcp_auth.cache['warm'] = (float('inf'), None, None)
    epoch = mcp_auth.epoch

    # when
    res = save(client, values={'scope': 'openid email', 'claims': 'access-token'})
    lines = access_lines(store)

    # then
    assert res.status_code == 503 and calls == ['sso.scope', 'sso.claims']
    assert mcp_auth.cache == {} and mcp_auth.epoch == epoch + 1
    assert lines == [msg('sso.saved', fields='Scope')]
    assert changes(store) == [('sso.scope', None, 'openid email')]


def test_a_database_error_while_noting_the_save_never_masks_the_original_error(store: Store, monkeypatch, caplog):
    # given
    idp = FakeIdp()
    client = app_client(store, idp, idp_settings(idp, sso_client_secret=''))

    def refused(*args):
        raise InvalidSecret()

    def unnoted(*args):
        raise AutoReconnect('the database went away')

    monkeypatch.setattr(client.app.state.sso_secret, 'set', refused)
    monkeypatch.setattr(store, 'add_activity', unnoted)

    # when
    with caplog.at_level(logging.WARNING):
        res = save(client, values={'scope': 'openid email', 'client-secret': 'another-client-secret'})
    warned = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]

    # then
    assert (res.status_code, res.json()) == (422, {'detail': msg('sso.secret_invalid'), 'field': 'client-secret'})
    assert changes(store) == [('sso.scope', None, 'openid email')]
    assert warned == ['the single sign-on save was not written to the activity: AutoReconnect']


def test_a_failed_stop_stamp_write_still_notes_the_save_and_the_switch(store: Store, monkeypatch, caplog):
    # given
    client = app_client(store, FakeIdp())
    live = client.app.state.live

    def unmarked(*args):
        raise AutoReconnect('the database went away')

    monkeypatch.setattr(live.tunables, 'mark', unmarked)

    # when
    with caplog.at_level(logging.WARNING):
        res = save(client, values={'scope': 'openid email'}, enabled=False)
    warned = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    stamp = store.db['tunables'].find_one({'_id': SESSIONS_AFTER_KEY})

    # then
    assert res.status_code == 200 and res.json()['enabled'] is False
    assert access_lines(store) == [msg('sso.saved', fields='Scope'), msg('sso.switched_off')]
    assert warned == ['the single sign-on stop was not written, the next settings refresh writes it: AutoReconnect']
    assert stamp is not None and stamp['value'], 'the refresh while off still wrote the stop stamp'


def test_the_sso_view_shows_the_reset_value_the_role_map_in_force_and_the_mcp_base(store: Store):
    # given
    idp = FakeIdp()
    named = idp_settings(idp, admin_role='ops-admins', operator_role='ops-operators', base_url='https://skopax.example.com/')
    plain = client_of(store, named, SsoHttp(fetch=idp.fetch, exchange=idp.exchange))
    no_operator = client_of(store, idp_settings(idp, admin_role='ops-admins', operator_role=''), SsoHttp(fetch=idp.fetch))
    env_map = client_of(store, idp_settings(idp, sso_role_map=json.dumps([{'value': 'ops-viewers', 'role': 'viewer'}])), SsoHttp(fetch=idp.fetch))
    env_off = client_of(store, idp_settings(idp, sso_on=False), SsoHttp(fetch=idp.fetch))

    # when
    first = view(plain)
    single = view(no_operator)
    from_env = view(env_map)
    plain.put(SETTINGS, json={'key': 'mcp.resource-url', 'value': 'https://mcp.example.com'}, headers=AUTH)
    with_resource = view(plain)
    saved = save(plain, values={'role-map': json.dumps([{'value': 'ops-admins', 'role': 'admin'}])})
    after_map = view(plain)
    env_off.put(SETTINGS, json={'key': 'sso.enabled', 'value': 'true'}, headers=AUTH)
    over_env = view(env_off)

    # then
    assert first['roleMapInUse'] == [{'value': 'ops-admins', 'role': 'admin'}, {'value': 'ops-operators', 'role': 'operator'}]
    assert first['switch']['baseValue'] is True and first['mcpBase'] == 'https://skopax.example.com'
    assert with_resource['mcpBase'] == 'https://skopax.example.com'
    assert saved.status_code == 200 and after_map['roleMapInUse'] == []
    assert single['roleMapInUse'] == [{'value': 'ops-admins', 'role': 'admin'}]
    assert from_env['roleMapInUse'] == []
    assert (over_env['switch']['value'], over_env['switch']['baseValue'], over_env['enabled']) == ('true', False, True)


def test_the_sign_in_labels_and_texts_say_what_the_card_says(store: Store):
    # given
    idp = FakeIdp()
    client = app_client(store, idp)
    wanted = {'sso.client-id': 'Client ID', 'sso.claims': 'Claims come from', 'sso.require-verified-email': 'Require verified email', 'sso.jwks-url': 'Keys (JWKS) URL',
              'mcp.resource-url': 'Skopax address for MCP (without /mcp)'}
    label = wanted['mcp.resource-url']
    refused_value = {'https://skopax.example.com/mcp': 422, 'https://skopax.example.com/mcp/': 422, 'https://skopax.example.com/MCP': 422, 'https://skopax.example.com/ops': 200}

    # when
    items = {i['key']: i for i in client.get(SETTINGS, headers=AUTH).json()['items']}
    answers = {value: client.put(SETTINGS, json={'key': 'mcp.resource-url', 'value': value}, headers=AUTH) for value in refused_value}
    kept = client.app.state.mcp_auth.resource()

    # then
    assert {key: items[key]['label'] for key in wanted} == wanted
    assert 'the roles claim set on the Single sign-on card' in items['mcp.required-role']['help'] and 'of Sign-in' not in items['mcp.required-role']['help']
    assert {value: res.status_code for value, res in answers.items()} == refused_value
    assert {res.json()['detail'] for res in answers.values() if res.status_code == 422} == {msg('mcp.resource_url_mcp', field=label)}
    assert kept == 'https://skopax.example.com/ops/mcp'
    assert 'SKOPAX_BASE_URL' in msg('sso.problem.redirect', field='sso.redirect-uri') and 'restart' in msg('sso.problem.redirect', field='sso.redirect-uri')
    assert 'Redirect URI setting (sso.redirect-uri)' in msg('sso.problem.redirect', field='sso.redirect-uri')
    assert '{since}' not in sso_messages()['sso.notice.provider']


def test_the_switch_off_texts_say_that_api_tokens_from_sso_sessions_are_refused_and_stay_refused_after_switching_on(store: Store, caplog):
    # given
    idp = FakeIdp()
    client = app_client(store, idp)

    # when
    with caplog.at_level(logging.WARNING):
        off = save(client, enabled=False)
    help_text = {i['key']: i for i in client.get(SETTINGS, headers=AUTH).json()['items']}['sso.enabled']['help']
    logged = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING and 'single sign-on was switched off' in r.getMessage()]

    # then
    assert off.status_code == 200
    refused, kept_off = 'API tokens made from single sign-on sessions are refused', 'stay refused after switching back on'
    texts = [msg('sso.switched_off'), help_text]
    assert msg('sso.switched_off') in access_lines(store) and logged == [msg('sso.stopped_log')]
    assert all(refused in text and kept_off in text and 'while it is off' not in text for text in texts)
    assert 'every API token made from a single sign-on session is refused and stays refused after switching back on' in logged[0] and 'while it is off' not in logged[0]
