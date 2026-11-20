import base64
import dataclasses
import hashlib
import html
import io
import json
import logging
import threading
import time
import urllib.error
import urllib.parse
from email.message import Message

import pytest
from fastapi.testclient import TestClient

from skopax import auth, oidc, signin, sso
from skopax.app import create_app
from skopax.contract import Hooks
from skopax.messages import msg
from skopax.oidc import OidcError
from skopax.registry import Registry
from skopax.sessions import session_secret, unb64
from skopax.sso import SignOn, SsoConfig, SsoHttp, basic_auth, post_form
from skopax.store import Store
from skopax.users import Users
from tests.conftest import AUTH
from tests.plugins.base import minimal
from tests.sso_kit import OSCAR, REDIRECT, SECRET, FakeIdp, Listener, app_client, idp_settings, log_in, scripted_server, server_url, stop_server
from tests.test_oidc import drip, one_shot_server

SESSION = 'skopax_session='
PENDING = 'skopax_login='


def cookies_set(res, prefix: str) -> list[str]:
    return [c for c in res.headers.get_list('set-cookie') if c.startswith(prefix)]


def signed_in(res) -> bool:
    return any('max-age=0' not in c.lower() for c in cookies_set(res, SESSION))


def pending_cleared(res) -> bool:
    return any('max-age=0' in c.lower() for c in cookies_set(res, PENDING))


def shows(res, key: str) -> bool:
    return html.escape(msg(key)) in res.text


def raw(store: Store, email: str) -> dict | None:
    return store.db['users'].find_one({'_id': email})


def start(client: TestClient, idp: FakeIdp, **headers) -> dict:
    return idp.authorize(client.get('/login/sso', follow_redirects=False, headers=headers).headers['location'])


def challenge_of(verifier: str) -> str:
    return base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b'=').decode()


def test_state_is_required_and_must_match(store: Store):
    # given
    idp = FakeIdp()
    client = app_client(store, idp)
    other_state = start(app_client(store, idp), idp)['state']
    attempts = {'missing': {'code': 'c'}, 'empty': {'code': 'c', 'state': ''}, 'another login': {'code': 'c', 'state': other_state}}

    # when
    refused = {}
    for name, params in attempts.items():
        start(client, idp)
        res = client.get('/authorized', params=params, follow_redirects=False)
        refused[name] = (res.status_code, shows(res, 'login.mismatch'), signed_in(res), pending_cleared(res), client.cookies.get('skopax_login'))
    exchanged = len(idp.exchanges)
    ok = log_in(client, idp)

    # then
    assert refused == {name: (400, True, False, True, None) for name in attempts}
    assert exchanged == 0
    assert ok.status_code == 303 and signed_in(ok) and pending_cleared(ok)


def test_nonce_binds_the_id_token_to_this_login(store: Store):
    # given
    idp = FakeIdp()
    first = log_in(app_client(store, idp), idp, sub='bob@example.com', email='bob@example.com')
    replayed = idp.minted[-1]
    bob_before = raw(store, 'bob@example.com')

    # when
    refused = {}
    for name, nonce in (('without', None), ('another', 'another-nonce')):
        email = f'{name}@example.com'
        res = log_in(app_client(store, idp), idp, sub=email, email=email, nonce=nonce)
        refused[name] = (res.status_code, shows(res, 'login.failed'), signed_in(res), raw(store, email))
    idp.response = {'access_token': replayed, 'id_token': replayed, 'token_type': 'Bearer'}
    replay = log_in(app_client(store, idp), idp)
    idp.response = None
    access_mode = FakeIdp()
    access_mode.access_claims = {'sub': 'carol@example.com', 'email': 'carol@example.com'}
    by_access_token = log_in(app_client(store, access_mode, s=idp_settings(access_mode, sso_claims='access-token')), access_mode)
    by_id_token = log_in(app_client(store, access_mode), access_mode, sub='alice@example.com', email='alice@example.com')

    # then
    assert first.status_code == 303 and signed_in(first)
    assert refused == {name: (401, True, False, None) for name in ('without', 'another')}
    assert (replay.status_code, shows(replay, 'login.failed'), signed_in(replay)) == (401, True, False)
    assert raw(store, 'bob@example.com') == bob_before
    assert by_access_token.status_code == 303 and signed_in(by_access_token)
    assert raw(store, 'carol@example.com') is not None and raw(store, OSCAR) is None
    assert by_id_token.status_code == 303 and raw(store, 'alice@example.com') is not None


def test_pkce_s256_reaches_both_ends_and_plain_is_never_used(store: Store):
    # given
    idp = FakeIdp()
    client = app_client(store, idp)
    plain_only = FakeIdp()
    plain_only.discovery['code_challenge_methods_supported'] = ['plain']
    refusing = app_client(store, plain_only)

    # when
    res = log_in(client, idp)
    sent = idp.authorizations[-1]
    token_request = idp.exchanges[-1]['data']
    blocked = refusing.get('/login/sso', follow_redirects=False)
    signon = refusing.app.state.signon
    with pytest.raises(OidcError) as refused:
        signon.start('/')

    # then
    assert res.status_code == 303
    assert sent['code_challenge_method'] == 'S256'
    assert sent['code_challenge'] == challenge_of(token_request['code_verifier'])
    assert 'plain' not in repr(idp.authorizations) and 'plain' not in repr(idp.exchanges)
    assert blocked.status_code == 503 and shows(blocked, 'login.failed') and 'location' not in blocked.headers
    assert signon.active() is True
    assert refused.value.reason == 'pkce'


def test_the_redirect_uri_comes_from_config_never_the_host_header(store: Store):
    # given
    idp = FakeIdp()
    configured = app_client(store, idp)
    from_base = app_client(store, idp, s=idp_settings(idp, sso_redirect_uri=''))
    unset = idp_settings(idp, sso_redirect_uri='', base_url='')
    nowhere = app_client(store, idp, s=unset)
    evil = {'Host': 'evil.example.com'}

    # when
    seen = []
    for client in (configured, from_base):
        state = start(client, idp, **evil)
        res = client.get('/authorized', params={'code': 'auth-code', 'state': state['state']}, headers=evil, follow_redirects=False)
        seen.append((res.status_code, len(idp.exchanges), state['redirect_uri'], idp.exchanges[-1]['data']['redirect_uri']))
    off = nowhere.get('/login/sso', follow_redirects=False)
    bad = {uri: SsoConfig.of(idp_settings(idp, sso_redirect_uri=uri), SECRET).problem() for uri in (
        'http://skopax.example.com/authorized', 'https://skopax.example.com/callback', 'https://skopax.example.com/authorized?x=1', '/authorized')}

    # then
    assert seen == [(303, 1, REDIRECT, REDIRECT), (303, 2, REDIRECT, REDIRECT)]
    assert SsoConfig.of(unset, SECRET).problem() == msg('sso.problem.redirect', field='sso.redirect-uri')
    assert off.status_code == 404 and shows(off, 'login.sso_off')
    assert nowhere.app.state.signon.active() is False
    assert bad == dict.fromkeys(bad, msg('sso.problem.redirect', field='sso.redirect-uri'))
    assert SsoConfig.of(idp_settings(idp, sso_redirect_uri='http://localhost:8080/authorized'), SECRET).problem() is None


def test_sso_never_signs_in_to_or_writes_a_local_account(store: Store):
    # given
    idp = FakeIdp()
    Users(store.db).create('alice@example.com', 'Alice', 'viewer', password='a-long-password-1')
    before = raw(store, 'alice@example.com')

    # when
    res = log_in(app_client(store, idp), idp, sub='alice@example.com', email='alice@example.com', name='Oscar Green', roles=['SKOPAX_ADMIN'])
    bob = log_in(app_client(store, idp), idp, sub='bob@example.com', email='bob@example.com', name='Bob')

    # then
    assert (res.status_code, shows(res, 'login.failed'), signed_in(res)) == (401, True, False)
    assert raw(store, 'alice@example.com') == before
    assert bob.status_code == 303 and signed_in(bob)
    assert (raw(store, 'bob@example.com')['source'], raw(store, 'bob@example.com')['name']) == ('sso', 'Bob')


@pytest.mark.parametrize('doc', [
    {'_id': 'carol@example.com', 'email': 'carol@example.com', 'name': 'Carol', 'role': 'admin', 'source': 'local', 'disabled': False, 'lastLogin': None},
    {'_id': 'carol@example.com', 'email': 'carol@example.com', 'name': 'Carol', 'role': 'admin', 'disabled': False},
], ids=['local', 'no-source'])
def test_a_local_account_created_mid_login_is_not_taken_over(store: Store, monkeypatch, doc: dict):
    # given
    idp = FakeIdp()
    store.db['users'].insert_one(dict(doc))
    real = Users.get
    missed = []

    def racing(self, email):
        if email == 'carol@example.com' and not missed:
            missed.append(email)
            return None
        return real(self, email)

    monkeypatch.setattr(Users, 'get', racing)

    # when
    res = log_in(app_client(store, idp), idp, sub='carol@example.com', email='carol@example.com', name='Oscar Green', roles=['SKOPAX_ADMIN'])
    bob = log_in(app_client(store, idp), idp, sub='bob@example.com', email='bob@example.com')

    # then
    assert missed == ['carol@example.com']
    assert (res.status_code, shows(res, 'login.failed'), signed_in(res)) == (401, True, False)
    assert raw(store, 'carol@example.com') == doc
    assert bob.status_code == 303 and signed_in(bob)


def test_email_verified_false_is_refused_and_absent_follows_the_switch(store: Store):
    # given
    idp = FakeIdp()
    lenient = idp_settings(idp)
    strict = idp_settings(idp, sso_require_verified_email=True)

    def attempt(s, name: str, value) -> tuple[int, bool, bool, bool]:
        email = f'{name}@example.com'
        res = log_in(app_client(store, idp, s=s), idp, sub=email, email=email, email_verified=value)
        return res.status_code, shows(res, 'login.failed'), signed_in(res), raw(store, email) is not None

    refused, accepted = (401, True, False, False), (303, False, True, True)

    # when
    falses = [attempt(lenient, f'false-{i}', value) for i, value in enumerate((False, 'false', 'FALSE', ' False ', 0, '0', '', 'no', 'yes'))]
    absent_lenient = attempt(lenient, 'absent-lenient', None)
    absent_strict = attempt(strict, 'absent-strict', None)
    trues = [attempt(s, f'true-{i}', value) for i, (s, value) in enumerate(((lenient, True), (strict, True), (lenient, 'TRUE'), (strict, 'true')))]
    false_strict = attempt(strict, 'false-strict', 'false')

    # then
    assert falses == [refused] * 9
    assert absent_lenient == accepted
    assert absent_strict == refused
    assert trues == [accepted] * 4
    assert false_strict == refused


def test_client_authentication_follows_discovery(store: Store):
    # given
    secret = 'se:cr/et +1'
    both, basic, unlisted = FakeIdp(), FakeIdp(), FakeIdp()
    basic.discovery['token_endpoint_auth_methods_supported'] = ['client_secret_basic']
    del unlisted.discovery['token_endpoint_auth_methods_supported']
    forced = FakeIdp()
    forced.discovery['token_endpoint_auth_methods_supported'] = ['client_secret_basic']

    # when
    sent = []
    for idp, over in ((both, {}), (basic, {}), (unlisted, {}), (forced, {'sso_client_auth': 'post'})):
        res = log_in(app_client(store, idp, s=idp_settings(idp, sso_client_secret=secret, **over)), idp)
        call = idp.exchanges[-1]
        sent.append((res.status_code, call['data'].get('client_secret'), call['headers'].get('Authorization')))

    # then
    pair = urllib.parse.quote_plus('skopax') + ':' + urllib.parse.quote_plus(secret)
    expected_basic = 'Basic ' + base64.b64encode(pair.encode()).decode()
    assert sent == [(303, secret, None), (303, None, expected_basic), (303, secret, None), (303, secret, None)]


def hook_app(store: Store, idp: FakeIdp, hook, **over) -> TestClient:
    plugin = dataclasses.replace(minimal('tokens', 'Tokens'), hooks=Hooks(oidc_token_request=hook))
    app = create_app(idp_settings(idp, **over), store, registry=Registry.of(plugin), sso_http=SsoHttp(fetch=idp.fetch, exchange=idp.exchange))
    return TestClient(app, base_url='https://testserver')


def test_the_token_request_hook_cannot_see_or_replace_secrets(store: Store, caplog):
    # given
    seen = []

    def hook(request):
        seen.append(request)
        return {'params': {'code_verifier': 'x', 'client_secret': 'planted', 'audience': 'api'}, 'headers': {'Authorization': 'Bearer stolen', 'X-Channel': 'web'}}

    def boom(request):
        raise RuntimeError('hook-detail')

    post, basic, broken = FakeIdp(), FakeIdp(), FakeIdp()
    basic.discovery['token_endpoint_auth_methods_supported'] = ['client_secret_basic']

    # when
    with caplog.at_level(logging.WARNING):
        client = hook_app(store, post, hook, sso_token_headers='X-Client: web; Authorization: Bearer gateway-key')
        signed = [log_in(client, post), log_in(client, post)]
        by_basic = log_in(hook_app(store, basic, hook), basic)
        failed = log_in(hook_app(store, broken, boom), broken)

    # then
    hidden = ('client_secret', 'code', 'code_verifier', 'authorization')
    assert len(seen) == 3
    assert all(set(request) == {'url', 'headers', 'params'} for request in seen)
    assert all(k.lower() not in hidden for request in seen for part in ('headers', 'params') for k in request[part])
    assert seen[0] == {'url': 'https://idp.example.com/token', 'headers': {'X-Client': 'web'},
                       'params': {'grant_type': 'authorization_code', 'redirect_uri': REDIRECT, 'client_id': 'skopax'}}
    assert [r.status_code for r in signed] == [303, 303] and by_basic.status_code == 303
    first = post.exchanges[0]
    assert first['data']['code_verifier'] != 'x' and challenge_of(first['data']['code_verifier']) == post.authorizations[0]['code_challenge']
    assert first['data']['audience'] == 'api' and first['data']['client_secret'] == SECRET
    assert first['headers'] == {'X-Client': 'web', 'Authorization': 'Bearer gateway-key', 'X-Channel': 'web'}
    assert basic.exchanges[0]['headers']['Authorization'].startswith('Basic ') and basic.exchanges[0]['data']['code_verifier'] != 'x'
    assert 'client_secret' not in basic.exchanges[0]['data'] and basic.exchanges[0]['data']['audience'] == 'api'
    dropped = [r for r in caplog.records if r.name == 'skopax.sso' and 'dropped' in r.getMessage()]
    assert len(dropped) == 2 and all('Authorization' in r.getMessage() and 'code_verifier' in r.getMessage() for r in dropped)
    assert (failed.status_code, shows(failed, 'login.failed'), signed_in(failed), broken.exchanges) == (503, True, False, [])
    assert any('tokens' in r.getMessage() for r in caplog.records if r.name == 'skopax.runtime')
    assert 'hook-detail' not in caplog.text


def test_the_token_request_sends_no_scope(store: Store):
    # given
    idp = FakeIdp()

    # when
    res = log_in(app_client(store, idp), idp)

    # then
    assert res.status_code == 303
    assert idp.authorizations[-1]['scope'] == 'openid email profile'
    assert 'scope' not in idp.exchanges[-1]['data']


def test_an_idp_error_never_reaches_the_page(store: Store, caplog):
    # given
    idp = FakeIdp()
    idp.status = 401
    idp.error_body = ('{"error": "invalid_client", "error_description": "wrong secret ' + SECRET + '"}').encode()
    client = app_client(store, idp)

    # when
    with caplog.at_level(logging.DEBUG):
        res = log_in(client, idp)
        idp.status = None
        ok = log_in(client, idp)

    # then
    assert (res.status_code, shows(res, 'login.failed'), signed_in(res)) == (401, True, False)
    assert SECRET not in res.text and 'invalid_client' not in res.text
    assert SECRET not in caplog.text and 'invalid_client' not in caplog.text
    assert ok.status_code == 303 and signed_in(ok)


class Answer(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_post_form_maps_errors_without_the_body(monkeypatch):
    # given
    sent = []
    answers = iter([
        Answer(b'{"access_token": "t"}'),
        urllib.error.HTTPError('https://idp.example.com/token', 400, 'refused', Message(), io.BytesIO(b'{"error": "' + SECRET.encode() + b'"}')),
        urllib.error.URLError('connection refused'),
        Answer(b'x' * (sso.oidc.MAX_BODY + 1)),
        Answer(b'not json'),
        Answer(b'[1]'),
    ])

    def opener(request, timeout, watch):
        sent.append((request.full_url, request.data, dict(request.header_items()), timeout))
        answer = next(answers)
        if isinstance(answer, Exception):
            raise answer
        return answer

    monkeypatch.setattr(oidc, 'open_url', opener)

    # when
    ok = post_form('https://idp.example.com/token', {'code': 'c', 'grant_type': 'authorization_code'}, {'X-Client': 'web'})
    failures = []
    for _ in range(5):
        with pytest.raises(OidcError) as caught:
            post_form('https://idp.example.com/token', {}, {})
        failures.append((caught.value.reason, caught.value.detail, str(caught.value), caught.value.__cause__, caught.value.__context__ is None or caught.value.__suppress_context__))

    # then
    assert ok == {'access_token': 't'}
    assert sent[0] == ('https://idp.example.com/token', b'code=c&grant_type=authorization_code',
                       {'Accept': 'application/json', 'Content-type': 'application/x-www-form-urlencoded', 'X-client': 'web'}, 10)
    assert [f[:2] for f in failures] == [('exchange', 'status 400'), ('unreachable', 'URLError'), ('exchange', 'the response is larger than 1 MiB'),
                                         ('exchange', 'the response is not JSON'), ('exchange', 'the response is not a JSON object')]
    assert all(SECRET not in f[2] and f[3] is None and f[4] for f in failures)


def test_the_old_post_form_home_is_gone():
    # then
    assert not hasattr(auth, 'post_form') and not hasattr(auth, 'Sso')
    assert sso.post_form is post_form


def test_no_idp_call_runs_on_the_event_loop(store: Store):
    # given
    idp = FakeIdp()

    # when
    res = log_in(app_client(store, idp), idp)

    # then
    assert res.status_code == 303
    assert idp.calls and idp.exchanges
    assert idp.on_loop == []


class Config:
    def __init__(self, idp: FakeIdp):
        self.idp = idp
        self.settings = idp_settings(idp)

    def __call__(self) -> SsoConfig:
        return SsoConfig.of(self.settings, SECRET)


def test_a_login_started_before_the_config_changed_must_start_again(store: Store):
    # given
    idp = FakeIdp()
    config = Config(idp)
    signon = SignOn(config, SsoHttp(idp.fetch, idp.exchange), fp_key=lambda: b'k' * 32)
    provider = signon.provider()
    _, kept = signon.start('/c/7')
    _, stale = signon.start('/')

    # when
    same_provider = signon.provider() is provider
    idp.nonce = kept['nonce']
    claims = signon.finish(kept, 'auth-code', kept['state'], None)
    config.settings = dataclasses.replace(config.settings, sso_scope='openid email')
    with pytest.raises(OidcError) as changed:
        signon.finish(stale, 'auth-code', stale['state'], None)
    kept_provider = signon.provider() is provider

    # then
    assert same_provider and kept_provider, 'a scope change keeps the cached provider'
    assert claims['email'] == OSCAR and kept['next'] == '/c/7'
    assert set(kept) == {'state', 'nonce', 'verifier', 'next', 'redirect', 'issuer', 'fp'}
    assert changed.value.reason == 'changed' and len(idp.exchanges) == 1


def test_a_cancelled_or_expired_login_gets_its_own_text(store: Store):
    # given
    idp = FakeIdp()
    client = app_client(store, idp)
    stranger = app_client(store, idp)

    # when
    start(client, idp)
    cancelled = client.get('/authorized', params={'error': 'access_denied'}, follow_redirects=False)
    expired = stranger.get('/authorized', params={'code': 'c', 'state': 's'}, follow_redirects=False)
    ok = log_in(client, idp)

    # then
    assert (cancelled.status_code, shows(cancelled, 'login.cancelled'), signed_in(cancelled), pending_cleared(cancelled)) == (400, True, False, True)
    assert (expired.status_code, shows(expired, 'login.expired'), signed_in(expired), pending_cleared(expired)) == (400, True, False, True)
    assert idp.exchanges and ok.status_code == 303


def test_an_unreachable_provider_is_a_503_and_no_session(store: Store):
    # given
    down_first = FakeIdp()
    down_first.fail = True
    keys_down = FakeIdp()
    client = app_client(store, keys_down)
    pending = start(client, keys_down)
    keys_down.fail = True

    # when
    before = app_client(store, down_first).get('/login/sso', follow_redirects=False)
    after = client.get('/authorized', params={'code': 'auth-code', 'state': pending['state']}, follow_redirects=False)
    written = raw(store, OSCAR)
    healthy = FakeIdp()
    ok = log_in(app_client(store, healthy), healthy)

    # then
    assert (before.status_code, shows(before, 'login.failed'), 'location' in before.headers) == (503, True, False)
    assert (after.status_code, shows(after, 'login.failed'), signed_in(after), pending_cleared(after)) == (503, True, False, True)
    assert written is None and ok.status_code == 303 and signed_in(ok)
    assert len(keys_down.exchanges) == 1


def test_a_disabled_sso_user_is_refused_without_any_write(store: Store):
    # given
    idp = FakeIdp()
    users = Users(store.db)
    users.create('carol@example.com', 'Carol', 'viewer', source='sso')
    users.set_disabled('carol@example.com', True)
    users.create('bob@example.com', 'Bob', 'viewer', source='sso', name_from='idp')
    before = raw(store, 'carol@example.com')

    # when
    res = log_in(app_client(store, idp), idp, sub='carol@example.com', email='carol@example.com', name='Oscar Green', roles=['SKOPAX_ADMIN'])
    bob = log_in(app_client(store, idp), idp, sub='bob@example.com', email='bob@example.com', name='Bob Stone', roles=['SKOPAX_ADMIN'])

    # then
    assert (res.status_code, shows(res, 'login.failed'), signed_in(res)) == (401, True, False)
    assert raw(store, 'carol@example.com') == before
    stored = raw(store, 'bob@example.com')
    assert bob.status_code == 303 and signed_in(bob)
    assert (stored['name'], stored['role'], stored['roleFrom'], stored['lastLogin'] is not None) == ('Bob Stone', 'admin', 'claim', True)


def test_the_pending_cookie_fingerprint_is_keyed_by_the_session_secret(store: Store):
    # given
    idp = FakeIdp()
    header_value = 'header-secret-1'
    s = idp_settings(idp, sso_token_headers=f'X-Api-Key: {header_value}')
    here, there = app_client(store, idp, s=s), app_client(store, idp, s=s)

    # when
    started = here.get('/login/sso', follow_redirects=False)
    sealed = started.cookies['skopax_login']
    body = unb64(sealed.rsplit('.', 1)[0]).decode()
    pending = json.loads(body)
    params = idp.authorize(started.headers['location'])
    there.cookies.set('skopax_login', sealed)
    shared = there.get('/authorized', params={'code': 'auth-code', 'state': params['state']}, follow_redirects=False)
    key = session_secret(store.db, s)
    config = SsoConfig.of(s, SECRET)

    # then
    plain = [hashlib.sha256(v.encode()).hexdigest() for v in (SECRET, header_value)]
    pieces = {h[:n] for h in plain for n in (8, 16, 32, 64)}
    assert not any(piece in body for piece in pieces)
    assert SECRET not in body and header_value not in body
    assert pending['fp'] == config.fingerprint(key)
    assert pending['fp'] not in (config.fingerprint(b''), config.fingerprint(b'another-key'))
    assert SsoConfig.of(s, 'another-secret').fingerprint(key) != pending['fp']
    assert SsoConfig.of(dataclasses.replace(s, sso_token_headers='X-Api-Key: header-secret-2'), SECRET).fingerprint(key) != pending['fp']
    assert len(pending['fp']) == 16
    assert shared.status_code == 303 and signed_in(shared)


def test_log_in_claims_apply_to_that_call_only(store: Store):
    # given
    idp = FakeIdp()

    # when
    first = log_in(app_client(store, idp), idp, sub='bob@example.com', email='bob@example.com')
    second = log_in(app_client(store, idp), idp)

    # then
    assert (first.status_code, second.status_code) == (303, 303)
    assert raw(store, 'bob@example.com') is not None and raw(store, OSCAR) is not None


def test_a_sign_on_needs_its_fingerprint_key():
    # given
    idp = FakeIdp()
    config = lambda: SsoConfig.of(idp_settings(idp), SECRET)

    # when
    with pytest.raises(TypeError):
        SignOn(config, SsoHttp(idp.fetch, idp.exchange))
    built = SignOn(config, SsoHttp(idp.fetch, idp.exchange), fp_key=lambda: b'k' * 32)

    # then
    assert built.fp_key() == b'k' * 32


def test_a_redirect_from_the_token_endpoint_is_an_exchange_failure_and_reaches_no_other_host(store: Store, caplog):
    # given
    elsewhere = Listener()
    moved = (302, {'Location': elsewhere.url + '/elsewhere/token'}, b'{}')
    token = scripted_server([moved, moved])
    idp = FakeIdp()
    idp.discovery['token_endpoint'] = server_url(token) + '/token'
    s = idp_settings(idp, sso_client_auth='basic', sso_token_headers='X-Gateway: gateway-token-value')
    client = TestClient(create_app(s, store, sso_http=SsoHttp(fetch=idp.fetch)), base_url='https://testserver')

    # when
    with caplog.at_level(logging.INFO):
        res = log_in(client, idp)
    written = raw(store, OSCAR)
    with pytest.raises(OidcError) as direct:
        post_form(server_url(token) + '/token', {'grant_type': 'authorization_code'}, {'Authorization': basic_auth('skopax', SECRET)})
    pending = start(client, idp)
    token.answers.append((200, {}, json.dumps(idp.exchange('', {}, {})).encode()))
    ok = client.get('/authorized', params={'code': 'auth-code', 'state': pending['state']}, follow_redirects=False)
    seen = list(token.seen)
    stop_server(token)
    elsewhere.close()

    # then
    assert (res.status_code, shows(res, 'login.failed'), signed_in(res)) == (401, True, False)
    assert written is None
    assert 'login failed: exchange' in caplog.text
    assert (direct.value.reason, direct.value.detail) == ('exchange', 'status 302')
    sent = {k.lower(): v for k, v in seen[0]['headers'].items()}
    assert len(seen) == 3 and seen[0]['method'] == 'POST' and seen[0]['path'] == '/token'
    assert sent['authorization'] == basic_auth('skopax', SECRET) and sent['x-gateway'] == 'gateway-token-value'
    form = urllib.parse.parse_qs(seen[2]['body'].decode())
    assert (ok.status_code, signed_in(ok)) == (303, True), 'a 200 from the token endpoint signs in over the same transport'
    assert form['grant_type'] == ['authorization_code'] and form['code_verifier'] and 'client_secret' not in form
    assert elsewhere.accepted == 0, 'the client secret and the gateway header never reach the redirect target'


def test_a_dripping_token_endpoint_stops_at_the_total_deadline(monkeypatch):
    # given
    monkeypatch.setattr(oidc, 'FETCH_TOTAL_S', 1.5)
    stop = threading.Event()
    server, worker = one_shot_server(lambda conn: (conn.recv(65536), drip(conn, stop, b'HTTP/1.1 200 OK\r\nX-Slow: ', 30)))
    url = f'http://127.0.0.1:{server.getsockname()[1]}/token'
    started = time.monotonic()

    # when
    with pytest.raises(OidcError) as caught:
        post_form(url, {'grant_type': 'authorization_code'}, {})
    took = time.monotonic() - started
    stop.set()
    worker.join(5)
    server.close()

    # then
    assert caught.value.reason == 'unreachable'
    assert took <= 1.5 + 1


class Tunable:
    def __init__(self, idp: FakeIdp):
        self.settings = idp_settings(idp)
        self.secret = SECRET

    def __call__(self) -> SsoConfig:
        return SsoConfig.of(self.settings, self.secret)


def test_the_provider_cache_follows_the_issuer_the_endpoint_overrides_and_the_switch_only():
    # given
    idp = FakeIdp()
    config = Tunable(idp)
    signon = SignOn(config, SsoHttp(idp.fetch, idp.exchange), fp_key=lambda: b'k' * 32)
    first = signon.provider()
    first.endpoints()
    first.keys().key_for('k1')
    fetched = dict(idp.calls)
    kept = {}

    # when
    for name, change in {'scope': {'sso_scope': 'openid email'}, 'claims': {'sso_claims': 'access-token'}, 'client-auth': {'sso_client_auth': 'basic'},
                         'require-verified-email': {'sso_require_verified_email': True}, 'roles-claim': {'sso_roles_claim': 'groups'}}.items():
        config.settings = dataclasses.replace(config.settings, **change)
        provider = signon.provider()
        kept[name] = provider is first and provider.keys() is first.keys()
    config.secret = 'another-secret'
    kept['secret'] = signon.provider() is first
    signon.provider().endpoints()
    signon.provider().keys().key_for('k1')
    refetched = idp.calls != fetched
    dropped = {}
    config.settings = dataclasses.replace(config.settings, sso_token_url='https://idp.example.com/other-token')
    dropped['token-url'] = signon.provider() is not first
    config.settings = dataclasses.replace(config.settings, sso_token_url='')
    back = signon.provider()
    config.settings = dataclasses.replace(config.settings, sso_issuer='https://idp2.example.com')
    dropped['issuer'] = signon.provider() is not back
    config.settings = dataclasses.replace(config.settings, sso_issuer=idp.issuer)
    again = signon.provider()
    config.settings = dataclasses.replace(config.settings, sso_on=False)
    off = signon.provider()
    config.settings = dataclasses.replace(config.settings, sso_on=True)
    on = signon.provider()
    dropped['off'] = off is not again
    dropped['on'] = on is not off and on is not again

    # then
    assert kept == dict.fromkeys(kept, True)
    assert not refetched, 'the cached discovery document and keys are kept'
    assert dropped == dict.fromkeys(dropped, True)


def test_a_login_never_finishes_while_sso_is_off(store: Store):
    # given
    idp = FakeIdp()
    client = app_client(store, idp)
    pending = start(client, idp)
    config = Tunable(idp)
    config.settings = dataclasses.replace(config.settings, sso_on=False)
    signon = SignOn(config, SsoHttp(idp.fetch, idp.exchange), fp_key=lambda: b'k' * 32)
    _, started_off = signon.start('/')

    # when
    switched = client.put('/api/admin/settings', json={'key': 'sso.enabled', 'value': 'false'}, headers=AUTH)
    after = client.get('/authorized', params={'code': 'auth-code', 'state': pending['state']}, follow_redirects=False)
    page_exchanges = len(idp.exchanges)
    with pytest.raises(OidcError) as off:
        signon.finish(started_off, 'auth-code', started_off['state'], None)
    config.settings = dataclasses.replace(config.settings, sso_on=True)
    _, started_on = signon.start('/')
    idp.nonce = started_on['nonce']
    claims = signon.finish(started_on, 'auth-code', started_on['state'], None)

    # then
    assert switched.status_code == 200
    assert (after.status_code, shows(after, 'login.restart'), signed_in(after)) == (400, True, False)
    assert page_exchanges == 0 and raw(store, OSCAR) is None
    assert off.value.reason == 'changed'
    assert len(idp.exchanges) == 1 and claims['email'] == OSCAR


def test_only_database_and_resolver_refusals_are_unavailable():
    # when
    statuses = {reason: signin.Refused(reason).status for reason in ('database', 'resolver', 'hook', 'local-account')}

    # then
    assert signin.UNAVAILABLE == ('database', 'resolver')
    assert statuses == {'database': 503, 'resolver': 503, 'hook': 401, 'local-account': 401}
