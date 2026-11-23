import asyncio
import base64
import concurrent.futures
import dataclasses
import hashlib
import hmac
import json
import logging
import os
import threading
import time
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient
from pymongo.errors import ServerSelectionTimeoutError

from tests.conftest import AUTH, settings, state
from tests.sso_kit import KEY, OTHER_KEY, FakeIdp, app_client as idp_client, log_in as idp_log_in
from tests.tagdefs import TEAM_TAGS
from skopax import chatui
from skopax.app import create_app
from skopax.auth import SESSION_COOKIE, safe_next, sso_email
from skopax.config import Settings
from skopax.features import off_text
from skopax.sessions import Sessions, b64, derive_secret, session_secret, unb64
from skopax.store import Store
from skopax.users import Users

DAY = 24 * 3600
THREE_DAYS = 3 * DAY
ISSUER = 'https://idp.example.com'
ROLE = 'SKOPAX_OPERATOR'
FEATURES_ON = {'investigator': True, 'chat': True, 'slack-autosend': True, 'slack-listener': True, 'mcp': True, 'public-read': True, 'push': True, 'postdeploy': True,
               'self-review': False, 'update-check': True, 'tag-report': True, 'warn-digest': True, 'langfuse-tracing': False, 'hide-attribution': False}
FEATURES_OFF = {f: off_text(f) for f, on in FEATURES_ON.items() if not on}
INSTANCES = {'current': {'name': 'skopax.example.com', 'letter': 'S'}, 'links': []}


def sso_settings():
    return dataclasses.replace(
        settings(),
        sso_client_secret='client-secret',
        sso_issuer=ISSUER,
        sso_jwks_url=f'{ISSUER}/jwks',
        sso_redirect_uri='https://skopax.dev.example.com/authorized',
        sso_default_role='operator',
    )


def access_token(roles: list[str], aud: str = 'skopax', key=KEY, issuer: str = ISSUER, sub: str = 'oscar.green@example.com') -> dict:
    return {'claims': {'sub': sub, 'aud': aud, 'iss': issuer, 'roles': roles}, 'key': key}


class TokenEndpoint(FakeIdp):
    def __init__(self, grant: dict | None = None):
        super().__init__(issuer=ISSUER)
        grant = grant or access_token([ROLE])
        self.claims = dict(grant['claims'])
        self.signer = grant['key']


class Clock:
    def __init__(self):
        self.now = float(int(time.time()))

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def app_client(store: Store, endpoint: TokenEndpoint | None = None, sso_on: bool = True, clock: Clock | None = None, s: Settings | None = None) -> TestClient:
    s = s or (sso_settings() if sso_on else settings())
    return idp_client(store, endpoint or TokenEndpoint(), s=s, clock=clock)


def log_in(client: TestClient) -> dict:
    idp_log_in(client, client.idp)
    return client.idp.authorizations[-1]


def legacy_seal(store: Store, clock: Clock, payload: dict, seconds: int) -> str:
    secret = session_secret(store.db, sso_settings())
    body = b64(json.dumps({**payload, 'exp': int(clock()) + seconds}, separators=(',', ':')).encode())
    return body + '.' + b64(hmac.new(secret, body.encode(), hashlib.sha256).digest())


@pytest.fixture
def endpoint() -> TokenEndpoint:
    return TokenEndpoint(access_token([ROLE]))


def test_guest_reads_the_page_but_cannot_queue_actions(store: Store):
    # given
    client = app_client(store)
    store.save(state(), None)

    # when
    page = client.get('/')
    me = client.get('/api/me').json()
    queued = client.post('/api/actions', json={'type': 'dive', 'key': 'k'})

    # then
    assert page.status_code == 200
    assert me == {'chatMax': 1000, 'loginEnabled': True, 'user': None, 'name': None, 'operator': False, 'jira': False, 'jiraUrl': '', 'gitlab': False, 'codeHost': '', 'workspaceLabel': '', 'task': {'project': 'OPS', 'projects': ['OPS', 'WEB', 'APP', 'MOB'], 'parent': '', 'points': 1.0, 'techProject': 'INFRA', 'techAssignee': True, 'titlePrefix': ''}, 'slack': False, 'slackChannel': '#skopax', 'tags': [{'id': k, 'label': v} for k, v in TEAM_TAGS.items()], 'routes': [], 'admin': False, 'chat': False, 'userRole': None, 'source': None, 'sso': True, 'canChangePassword': False, 'canEditName': False, 'features': FEATURES_ON, 'featureOff': FEATURES_OFF, 'instances': INSTANCES}
    assert queued.status_code == 401


def test_login_redirects_to_the_sso_login_with_pkce(store: Store):
    # given
    client = app_client(store)

    # when
    res = client.get('/login/sso', follow_redirects=False)

    # then
    location = urlsplit(res.headers['location'])
    params = parse_qs(location.query)
    assert res.status_code == 302
    assert f'{location.scheme}://{location.netloc}{location.path}' == 'https://idp.example.com/authorize'
    assert params['client_id'] == ['skopax']
    assert params['redirect_uri'] == ['https://skopax.dev.example.com/authorized']
    assert params['code_challenge_method'] == ['S256']
    assert params['scope'] == ['openid email profile']
    assert 'skopax_login' in res.cookies


def test_operator_login_queues_actions_under_their_name(store: Store, endpoint: TokenEndpoint):
    # given
    client = app_client(store, endpoint)

    # when
    params = log_in(client)
    me = client.get('/api/me').json()
    queued = client.post('/api/actions', json={'type': 'dive', 'key': 'k'})

    # then
    call = endpoint.exchanges[0]['data']
    challenge = base64.urlsafe_b64encode(hashlib.sha256(call['code_verifier'].encode()).digest()).rstrip(b'=').decode()
    assert challenge == params['code_challenge']
    assert call['code'] == 'auth-code'
    assert call['client_secret'] == 'client-secret'
    assert me == {'chatMax': 1000, 'loginEnabled': True, 'user': 'oscar.green@example.com', 'name': 'oscar.green@example.com', 'operator': True, 'jira': False, 'jiraUrl': '', 'gitlab': False, 'codeHost': '', 'workspaceLabel': '', 'task': {'project': 'OPS', 'projects': ['OPS', 'WEB', 'APP', 'MOB'], 'parent': '', 'points': 1.0, 'techProject': 'INFRA', 'techAssignee': True, 'titlePrefix': ''}, 'slack': False, 'slackChannel': '#skopax', 'tags': [{'id': k, 'label': v} for k, v in TEAM_TAGS.items()], 'routes': [], 'admin': False, 'chat': False, 'userRole': 'operator', 'source': 'sso', 'sso': True, 'canChangePassword': False, 'canEditName': True, 'features': FEATURES_ON, 'featureOff': FEATURES_OFF, 'instances': INSTANCES}
    assert queued.status_code == 201
    assert queued.json()['requestedBy'] == 'oscar.green@example.com'


def test_me_names_the_environment_label_and_the_task_title_prefix_when_they_are_set(store: Store):
    # given
    labelled = dataclasses.replace(settings(), workspace_label='PROD', jira_title_prefix='OPS: ')

    # when
    with TestClient(create_app(labelled, store)) as c:
        me = c.get('/api/me').json()

    # then
    assert (me['workspaceLabel'], me['task']['titlePrefix']) == ('PROD', 'OPS: ')


def test_any_login_can_act_now_that_the_operator_role_is_gone(store: Store):
    # given
    client = app_client(store, TokenEndpoint(access_token(['JIRA_METRICS_ADMIN'])))

    # when
    log_in(client)
    me = client.get('/api/me').json()
    queued = client.post('/api/actions', json={'type': 'mute', 'key': 'k', 'hours': 24})

    # then
    assert me['user'] == 'oscar.green@example.com'
    assert me['operator'] is True
    assert queued.status_code == 201


@pytest.mark.parametrize('token', [
    access_token([ROLE], aud='other-app'),
    access_token([ROLE], issuer='https://evil.example'),
    access_token([ROLE], key=OTHER_KEY),
], ids=['another-client', 'another-issuer', 'another-key'])
def test_tokens_for_another_client_issuer_or_key_do_not_log_in(store: Store, token: str):
    # given
    client = app_client(store, TokenEndpoint(token))

    # when
    start = client.get('/login/sso', follow_redirects=False)
    state_param = client.idp.authorize(start.headers['location'])['state']
    res = client.get('/authorized', params={'code': 'c', 'state': state_param}, follow_redirects=False)

    # then
    assert res.status_code == 401
    assert SESSION_COOKIE not in res.cookies
    assert client.post('/api/actions', json={'type': 'dive', 'key': 'k'}).status_code == 401


def test_state_mismatch_does_not_log_in(store: Store, endpoint: TokenEndpoint):
    # given
    client = app_client(store, endpoint)
    client.get('/login/sso', follow_redirects=False)

    # when
    res = client.get('/authorized', params={'code': 'c', 'state': 'forged'}, follow_redirects=False)

    # then
    assert res.status_code == 400
    assert endpoint.exchanges == []


@pytest.mark.parametrize('params', [{'code': 'c'}, {'code': 'c', 'state': ''}], ids=['missing', 'empty'])
def test_a_callback_without_state_does_not_log_in(store: Store, endpoint: TokenEndpoint, params: dict):
    # given
    client = app_client(store, endpoint)
    client.get('/login/sso', follow_redirects=False)

    # when
    res = client.get('/authorized', params=params, follow_redirects=False)

    # then
    assert res.status_code == 400
    assert endpoint.exchanges == []
    assert SESSION_COOKIE not in res.cookies


@pytest.mark.parametrize('error, logged', [('access_denied', 'access_denied'), ('denied' + chr(10) + 'FORGED line', 'an unexpected error value'), ('', 'no code')])
def test_a_callback_without_a_code_logs_only_a_well_formed_error_code(store: Store, caplog, error: str, logged: str):
    # given
    client = app_client(store)
    client.get('/login/sso', follow_redirects=False)

    # when
    with caplog.at_level(logging.WARNING):
        res = client.get('/authorized', params={'error': error}, follow_redirects=False)

    # then
    assert res.status_code == 400
    assert f'login returned without a code: {logged}' in caplog.text and 'FORGED' not in caplog.text


def test_tampered_session_cookie_is_ignored(store: Store, endpoint: TokenEndpoint):
    # given
    client = app_client(store, TokenEndpoint(access_token([])))
    log_in(client)
    sig = client.cookies[SESSION_COOKIE].rsplit('.', 1)[1]
    forged = base64.urlsafe_b64encode(b'{"name":"x","op":true,"exp":9999999999}').rstrip(b'=').decode()
    client.cookies.clear()
    client.cookies.set(SESSION_COOKIE, f'{forged}.{sig}')

    # when
    res = client.post('/api/actions', json={'type': 'dive', 'key': 'k'})

    # then
    assert res.status_code == 401


def test_a_logged_in_person_can_no_longer_edit_a_slack_draft(store: Store, endpoint: TokenEndpoint):
    # given
    store.save(state({'key': 'k', 'sev': 'warn', 'status': 'open', 'svc': 'checkout-service', 'label': 'ERROR pattern', 'slackDraft': {'text': 'old', 'by': 'Claude', 'at': '2026-10-13T16:00:00Z'}}), None)
    operator = app_client(store, endpoint)
    reader = app_client(store, TokenEndpoint(access_token(['JIRA_METRICS_ADMIN'])))
    log_in(operator)
    log_in(reader)

    # when
    first = reader.put('/api/slack-drafts', json={'key': 'k', 'text': 'reader'})
    saved = operator.put('/api/slack-drafts', json={'key': 'k', 'text': 'new draft'})

    # then
    assert (first.status_code, saved.status_code) == (410, 410)
    assert store.anomaly('k')['slackDraft']['text'] == 'old'


def test_operator_session_cannot_write_state_reserved_for_the_hourly_check(store: Store, endpoint: TokenEndpoint):
    # given
    client = app_client(store, endpoint)
    log_in(client)

    # when
    as_operator = client.put('/api/state', json=state())
    as_check = client.put('/api/state', json=state(), headers=AUTH)

    # then
    assert as_operator.status_code == 401
    assert as_check.status_code == 200


def test_operator_session_is_refused_from_another_origin(store: Store, endpoint: TokenEndpoint):
    # given
    client = app_client(store, endpoint)
    log_in(client)

    # when
    same_site = client.post('/api/actions', json={'type': 'dive', 'key': 'k'}, headers={'Origin': 'https://testserver'})
    other_site = client.post('/api/actions', json={'type': 'dive', 'key': 'k'}, headers={'Origin': 'https://other.dev.example.com'})

    # then
    assert same_site.status_code == 201
    assert other_site.status_code == 403


def test_logout_drops_the_session(store: Store, endpoint: TokenEndpoint):
    # given
    client = app_client(store, endpoint)
    log_in(client)

    # when
    client.post('/logout', follow_redirects=False)

    # then
    assert client.get('/api/me').json()['user'] is None


def test_without_sso_config_only_the_sso_route_is_off_and_the_token_still_works(store: Store):
    # given
    client = app_client(store, sso_on=False)

    # when
    login = client.get('/login/sso', follow_redirects=False)
    me = client.get('/api/me').json()
    queued = client.post('/api/actions', json={'type': 'dive', 'key': 'k'}, headers=AUTH)

    # then
    assert login.status_code == 404
    assert me['loginEnabled'] is True and me['sso'] is False
    assert queued.status_code == 201
    assert queued.json()['requestedBy'] == 'token:bootstrap'


def session_headers(res) -> list[str]:
    return [c for c in res.headers.get_list('set-cookie') if c.startswith(f'{SESSION_COOKIE}=')]


def session_payload(cookie: str) -> dict:
    value = cookie.split(';')[0].removeprefix(f'{SESSION_COOKIE}=')
    return json.loads(unb64(value.split('.')[0]))


def test_session_is_renewed_once_it_is_older_than_five_minutes(store: Store, endpoint: TokenEndpoint):
    # given
    clock = Clock()
    client = app_client(store, endpoint, clock=clock)
    log_in(client)
    login_at = int(clock.now)
    clock.advance(6 * 60)

    # when
    res = client.get('/api/rev')

    # then
    [cookie] = session_headers(res)
    flags = [f.strip().lower() for f in cookie.split(';')]
    assert f'max-age={THREE_DAYS}' in flags
    assert {'httponly', 'secure', 'samesite=lax'} <= set(flags)
    payload = session_payload(cookie)
    assert payload['exp'] == login_at + 6 * 60 + THREE_DAYS
    assert payload['login'] == login_at
    assert payload['iat'] == login_at + 6 * 60
    assert payload['role'] == 'operator'
    assert payload['src'] == 'sso'


def test_session_is_not_renewed_within_five_minutes_of_the_last_renewal(store: Store, endpoint: TokenEndpoint):
    # given
    clock = Clock()
    client = app_client(store, endpoint, clock=clock)
    log_in(client)

    # when
    clock.advance(4 * 60)
    early = client.get('/api/rev')
    clock.advance(2 * 60)
    renewed = client.get('/api/rev')
    clock.advance(15)
    right_after = client.get('/api/rev')

    # then
    assert session_headers(early) == []
    assert len(session_headers(renewed)) == 1
    assert session_headers(right_after) == []


def test_an_open_page_stays_logged_in_past_eight_hours(store: Store, endpoint: TokenEndpoint):
    # given
    clock = Clock()
    client = app_client(store, endpoint, clock=clock)
    log_in(client)

    # when
    for _ in range(9 * 6):
        clock.advance(10 * 60)
        client.get('/api/rev')

    # then
    assert client.get('/api/me').json()['user'] == 'oscar.green@example.com'
    assert client.post('/api/actions', json={'type': 'dive', 'key': 'k'}).status_code == 201


def test_login_sets_the_session_cookie_for_three_days(store: Store, endpoint: TokenEndpoint):
    # given
    clock = Clock()
    client = app_client(store, endpoint, clock=clock)
    start = client.get('/login/sso', follow_redirects=False)
    state_param = client.idp.authorize(start.headers['location'])['state']

    # when
    res = client.get('/authorized', params={'code': 'auth-code', 'state': state_param}, follow_redirects=False)

    # then
    [cookie] = session_headers(res)
    flags = [f.strip().lower() for f in cookie.split(';')]
    assert f'max-age={THREE_DAYS}' in flags
    assert {'httponly', 'secure', 'samesite=lax'} <= set(flags)
    assert session_payload(cookie)['exp'] == int(clock.now) + THREE_DAYS


def test_a_session_idle_for_two_days_stays_logged_in(store: Store, endpoint: TokenEndpoint):
    # given
    clock = Clock()
    client = app_client(store, endpoint, clock=clock)
    log_in(client)
    clock.advance(2 * DAY)

    # when
    me = client.get('/api/me').json()
    queued = client.post('/api/actions', json={'type': 'dive', 'key': 'k'})

    # then
    assert me['user'] == 'oscar.green@example.com'
    assert queued.status_code == 201


def test_session_ends_after_three_idle_days(store: Store, endpoint: TokenEndpoint):
    # given
    clock = Clock()
    client = app_client(store, endpoint, clock=clock)
    log_in(client)
    clock.advance(THREE_DAYS + 1)

    # when
    res = client.get('/api/me')

    # then
    assert res.json()['user'] is None
    assert session_headers(res) == []


def test_session_ends_seven_days_after_login_even_when_used_daily(store: Store, endpoint: TokenEndpoint):
    # given
    clock = Clock()
    client = app_client(store, endpoint, clock=clock)
    log_in(client)
    cap = int(clock.now) + 7 * DAY

    # when
    for _ in range(6):
        clock.advance(DAY)
        client.get('/api/rev')
    clock.advance(DAY - 60)
    last_minute = client.get('/api/me')
    clock.advance(60 + 1)
    after_cap = client.get('/api/me')

    # then
    assert last_minute.json()['user'] == 'oscar.green@example.com'
    assert session_payload(client.cookies[SESSION_COOKIE])['exp'] == cap
    assert after_cap.json()['user'] is None
    assert session_headers(after_cap) == []


def test_session_ends_seven_days_after_login_even_when_active(store: Store, endpoint: TokenEndpoint):
    # given
    clock = Clock()
    client = app_client(store, endpoint, clock=clock)
    log_in(client)
    cap = int(clock.now) + 7 * 24 * 3600

    # when
    for _ in range(7 * 24 - 1):
        clock.advance(3600)
        client.get('/api/rev')
    last_hour = client.get('/api/me')
    clock.advance(3600 + 1)
    after_cap = client.get('/api/me')

    # then
    assert last_hour.json()['user'] == 'oscar.green@example.com'
    assert session_payload(client.cookies[SESSION_COOKIE])['exp'] == cap
    assert after_cap.json()['user'] is None
    assert session_headers(after_cap) == []


def test_session_cap_shortens_the_renewed_cookie(store: Store, endpoint: TokenEndpoint):
    # given
    clock = Clock()
    client = app_client(store, endpoint, clock=clock)
    log_in(client)
    cap = int(clock.now) + 7 * DAY
    for _ in range(3):
        clock.advance(DAY)
        client.get('/api/rev')

    # when
    clock.advance(2 * DAY)
    res = client.get('/api/rev')

    # then
    [cookie] = session_headers(res)
    assert f'max-age={2 * DAY}' in cookie.lower()
    assert session_payload(cookie)['exp'] == cap


def test_an_eight_hour_cookie_from_before_the_change_slides_to_three_days(store: Store, endpoint: TokenEndpoint):
    # given
    clock = Clock()
    client = app_client(store, endpoint, clock=clock)
    login_at = int(clock.now)
    Users(store.db).create('nina.white@example.com', 'Nina White', 'operator')
    old = legacy_seal(store, clock, {'name': 'nina.white@example.com', 'op': True, 'login': login_at, 'iat': login_at}, 8 * 3600)
    client.cookies.set(SESSION_COOKIE, old)
    clock.advance(7 * 3600)

    # when
    res = client.get('/api/rev')
    clock.advance(2 * DAY)
    later = client.get('/api/me')

    # then
    [cookie] = session_headers(res)
    assert f'max-age={THREE_DAYS}' in cookie.lower()
    payload = session_payload(cookie)
    assert payload['exp'] == login_at + 7 * 3600 + THREE_DAYS
    assert payload['login'] == login_at
    assert later.json()['user'] == 'nina.white@example.com'


def test_session_lengths_follow_the_config(store: Store, endpoint: TokenEndpoint):
    # given
    clock = Clock()
    client = app_client(store, endpoint, clock=clock, s=dataclasses.replace(sso_settings(), sso_session_idle_hours=2, sso_session_max_days=1))
    start = client.get('/login/sso', follow_redirects=False)
    state_param = client.idp.authorize(start.headers['location'])['state']
    login_at = int(clock.now)

    # when
    res = client.get('/authorized', params={'code': 'auth-code', 'state': state_param}, follow_redirects=False)
    for _ in range(23):
        clock.advance(3600)
        client.get('/api/rev')
    last_hour = client.get('/api/me')
    clock.advance(3600 + 1)
    after_cap = client.get('/api/me')

    # then
    [cookie] = session_headers(res)
    assert 'max-age=7200' in cookie.lower()
    assert session_payload(cookie)['exp'] == login_at + 7200
    assert last_hour.json()['user'] == 'oscar.green@example.com'
    assert session_payload(client.cookies[SESSION_COOKIE])['exp'] == login_at + DAY
    assert after_cap.json()['user'] is None


def test_session_cookie_from_before_sliding_counts_its_login_from_its_issue_time(store: Store, endpoint: TokenEndpoint):
    # given
    clock = Clock()
    client = app_client(store, endpoint, clock=clock)
    Users(store.db).create('nina.white@example.com', 'Nina White', 'operator')
    old = legacy_seal(store, clock, {'name': 'nina.white@example.com', 'op': True}, 8 * 3600)
    issued_at = int(clock.now)
    client.cookies.set(SESSION_COOKIE, old)
    clock.advance(6 * 60)

    # when
    res = client.get('/api/me')

    # then
    assert res.json()['operator'] is True
    [cookie] = session_headers(res)
    payload = session_payload(cookie)
    assert payload['login'] == issued_at
    assert payload['exp'] == issued_at + 6 * 60 + THREE_DAYS


def test_logout_is_not_renewed(store: Store, endpoint: TokenEndpoint):
    # given
    clock = Clock()
    client = app_client(store, endpoint, clock=clock)
    log_in(client)
    clock.advance(6 * 60)

    # when
    res = client.post('/logout', follow_redirects=False)

    # then
    [cookie] = session_headers(res)
    assert 'max-age=0' in cookie.lower()
    assert client.get('/api/me').json()['user'] is None


def test_new_login_over_an_old_session_sets_one_session_cookie(store: Store, endpoint: TokenEndpoint):
    # given
    clock = Clock()
    client = app_client(store, endpoint, clock=clock)
    log_in(client)
    clock.advance(6 * 60)
    start = client.get('/login/sso', follow_redirects=False)
    state_param = client.idp.authorize(start.headers['location'])['state']

    # when
    res = client.get('/authorized', params={'code': 'auth-code', 'state': state_param}, follow_redirects=False)

    # then
    [cookie] = session_headers(res)
    assert session_payload(cookie)['login'] == int(clock.now)


@pytest.mark.parametrize('target, expected', [('/', '/'), ('/#logs', '/#logs'), ('//evil.example', '/'), ('https://evil.example', '/'), ('/\\evil', '/'), (None, '/')])
def test_login_only_returns_to_pages_on_this_site(target, expected):
    # when
    result = safe_next(target)

    # then
    assert result == expected


def test_the_login_cookie_is_never_taken_for_a_session(store: Store):
    # given
    client = app_client(store)
    login_cookie = client.get('/login/sso', follow_redirects=False).cookies['skopax_login']
    client.cookies.clear()
    client.cookies.set(SESSION_COOKIE, login_cookie)

    # when
    me = client.get('/api/me')
    chats = client.get('/api/chats')
    asked = client.post('/api/chats', json={'text': 'any open cards?'})

    # then
    assert me.json()['user'] is None
    assert not session_headers(me)
    assert (chats.status_code, asked.status_code) == (401, 401)


def sso_login_as(store: Store, claims: dict, clock: Clock | None = None, s: Settings | None = None) -> tuple[TestClient, object]:
    s = s or sso_settings()
    client = app_client(store, TokenEndpoint({'claims': claims, 'key': KEY}), clock=clock, s=s)
    return client, idp_log_in(client, client.idp)


def test_disabled_user_loses_access_without_relogin(store: Store, endpoint: TokenEndpoint):
    # given
    clock = Clock()
    client = app_client(store, endpoint, clock=clock)
    log_in(client)
    assert client.get('/api/me').json()['user'] == 'oscar.green@example.com'

    # when
    Users(store.db).set_disabled('oscar.green@example.com', True)
    within_cache = client.get('/api/me').json()['user']
    clock.advance(31)
    after = client.get('/api/me').json()
    queued = client.post('/api/actions', json={'type': 'dive', 'key': 'k'})

    # then
    assert within_cache == 'oscar.green@example.com'
    assert after['user'] is None and after['operator'] is False
    assert queued.status_code == 401


def test_stored_role_wins_over_cookie_role(store: Store):
    # given
    users = Users(store.db)
    users.create('carol@example.com', 'Carol', 'viewer')
    client = TestClient(create_app(settings(), store), base_url='https://testserver')
    secret = session_secret(store.db, settings())
    cookie = Sessions(secret, 3600, 7200).issue('carol@example.com', 'admin', 'local')
    client.cookies.set(SESSION_COOKIE, cookie)

    # when
    me = client.get('/api/me').json()
    queued = client.post('/api/actions', json={'type': 'dive', 'key': 'k'})
    admin = client.get('/api/admin/settings')

    # then
    assert me['user'] == 'carol@example.com'
    assert (me['operator'], me['admin']) == (False, False)
    assert queued.status_code == 403
    assert admin.status_code == 403


def test_a_session_for_a_user_that_does_not_exist_is_no_session(store: Store):
    # given
    client = TestClient(create_app(settings(), store), base_url='https://testserver')
    cookie = Sessions(session_secret(store.db, settings()), 3600, 7200).issue('ghost@example.com', 'admin', 'local')
    client.cookies.set(SESSION_COOKIE, cookie)

    # when
    me = client.get('/api/me').json()

    # then
    assert me['user'] is None and me['admin'] is False


@pytest.mark.parametrize('base, redirect, secure', [
    ('http://localhost:8080', 'http://localhost:8080/authorized', False),
    ('https://skopax.example.com', 'https://skopax.example.com/authorized', True),
    ('', 'https://skopax.example.com/authorized', True),
    ('http://localhost:8080', '', False),
    ('', 'http://localhost:8080/authorized', False),
])
def test_cookie_secure_follows_base_url(store: Store, base: str, redirect: str, secure: bool):
    # given
    s = dataclasses.replace(sso_settings(), base_url=base, sso_redirect_uri=redirect)

    # when
    client, res = sso_login_as(store, {'sub': 'oscar.green@example.com', 'roles': []}, s=s)
    start = client.get('/login/sso', follow_redirects=False)

    # then
    [session] = session_headers(res)
    login_cookie = [c for c in start.headers.get_list('set-cookie') if c.startswith('skopax_login=')][0]
    for cookie in (session, login_cookie):
        flags = {f.strip().lower() for f in cookie.split(';')}
        assert ('secure' in flags) is secure
        assert {'httponly', 'samesite=lax'} <= flags


def test_sso_login_creates_the_user_and_the_session_name_is_the_email(store: Store):
    # given
    claims = {'sub': 'abc-123', 'email': 'Dana.Reed@Example.com', 'name': 'Dana Reed', 'roles': []}

    # when
    client, res = sso_login_as(store, claims)

    # then
    stored = Users(store.db).get('dana.reed@example.com')
    assert (stored['name'], stored['role'], stored['source']) == ('Dana Reed', 'operator', 'sso')
    assert session_payload(session_headers(res)[0])['name'] == 'dana.reed@example.com'
    assert client.get('/api/me').json()['user'] == 'dana.reed@example.com'


def test_sso_login_falls_back_to_an_email_shaped_username_or_subject(store: Store):
    # when
    by_username, _ = sso_login_as(store, {'sub': 'abc-123', 'preferred_username': 'erin@example.com'})
    by_subject, _ = sso_login_as(store, {'sub': 'frank@example.com'})

    # then
    assert by_username.get('/api/me').json()['user'] == 'erin@example.com'
    assert by_subject.get('/api/me').json()['user'] == 'frank@example.com'
    assert Users(store.db).get('erin@example.com')['name'] == 'erin@example.com'


def test_sso_login_without_any_email_is_the_generic_failure(store: Store, caplog):
    # when
    client, res = sso_login_as(store, {'sub': 'abc-123', 'name': 'No Mail'})

    # then
    assert res.status_code == 401
    assert 'The login could not be completed.' in res.text
    assert SESSION_COOKIE not in res.cookies
    assert any('email' in r.getMessage() for r in caplog.records)
    assert Users(store.db).count() == 1


def test_a_disabled_user_cannot_sign_in_through_sso(store: Store):
    # given
    users = Users(store.db)
    users.create('gina@example.com', 'Gina', 'operator', source='sso')
    users.set_disabled('gina@example.com', True)

    # when
    client, res = sso_login_as(store, {'sub': 'gina@example.com'})

    # then
    assert res.status_code == 401
    assert 'The login could not be completed.' in res.text
    assert SESSION_COOKIE not in res.cookies
    assert client.get('/api/me').json()['user'] is None


def test_the_admin_role_claim_makes_the_stored_user_an_admin(store: Store):
    # given
    s = dataclasses.replace(sso_settings(), sso_default_role='viewer')

    # when
    plain, _ = sso_login_as(store, {'sub': 'hank@example.com', 'roles': []}, s=s)
    boss, _ = sso_login_as(store, {'sub': 'ivy@example.com', 'roles': [s.admin_role]}, s=s)

    # then
    assert (Users(store.db).get('hank@example.com')['role'], Users(store.db).get('hank@example.com')['roleFrom']) == ('viewer', 'default')
    assert (Users(store.db).get('ivy@example.com')['role'], Users(store.db).get('ivy@example.com')['roleFrom']) == ('admin', 'claim')
    assert plain.get('/api/me').json()['admin'] is False
    assert boss.get('/api/me').json()['admin'] is True


def test_a_stored_role_survives_the_next_sso_login(store: Store):
    # given
    Users(store.db).create('jo@example.com', 'Jo', 'viewer', source='sso')

    # when
    client, _ = sso_login_as(store, {'sub': 'jo@example.com'})

    # then
    me = client.get('/api/me').json()
    stored = Users(store.db).get('jo@example.com')
    assert (me['user'], me['operator']) == ('jo@example.com', False)
    assert (stored['role'], stored['roleFrom'], stored['adminRole']) == ('viewer', 'admin', 'viewer')


def test_sso_login_never_takes_over_a_local_account(store: Store):
    # given
    before = Users(store.db).get('admin@example.com')

    # when
    client, res = sso_login_as(store, {'sub': 'admin@example.com', 'roles': []})

    # then
    assert res.status_code == 401
    assert 'The login could not be completed.' in res.text
    assert SESSION_COOKIE not in res.cookies
    assert Users(store.db).get('admin@example.com') == before


def test_sso_login_treats_an_account_without_a_source_as_local(store: Store):
    # given
    store.db['users'].insert_one({'_id': 'old@example.com', 'email': 'old@example.com', 'name': 'Old', 'role': 'admin', 'disabled': False})
    before = Users(store.db).get('old@example.com')

    # when
    _, res = sso_login_as(store, {'sub': 'old@example.com', 'roles': []})

    # then
    assert res.status_code == 401 and SESSION_COOKIE not in res.cookies
    assert Users(store.db).get('old@example.com') == before


def test_unverified_sso_email_is_refused(store: Store):
    # when
    _, res = sso_login_as(store, {'sub': 'kim@example.com', 'email': 'kim@example.com', 'email_verified': False})
    _, text_res = sso_login_as(store, {'sub': 'pat@example.com', 'email': 'pat@example.com', 'email_verified': 'False'})
    _, ok_res = sso_login_as(store, {'sub': 'lee@example.com', 'email': 'lee@example.com', 'email_verified': True})

    # then
    assert res.status_code == 401 and SESSION_COOKIE not in res.cookies
    assert text_res.status_code == 401 and SESSION_COOKIE not in text_res.cookies
    assert Users(store.db).get('kim@example.com') is None and Users(store.db).get('pat@example.com') is None
    assert ok_res.status_code == 303


def test_claim_admin_is_demoted_when_the_claim_goes_away(store: Store):
    # given
    s = dataclasses.replace(sso_settings(), sso_default_role='viewer')
    sso_login_as(store, {'sub': 'ivy@example.com', 'roles': [s.admin_role]}, s=s)
    assert Users(store.db).get('ivy@example.com')['roleFrom'] == 'claim'

    # when
    again, _ = sso_login_as(store, {'sub': 'ivy@example.com', 'roles': []}, s=s)

    # then
    stored = Users(store.db).get('ivy@example.com')
    assert stored['role'] == 'viewer' and stored['roleFrom'] == 'default' and 'adminRole' not in stored
    assert again.get('/api/me').json()['admin'] is False


def test_an_admin_not_made_by_the_claim_keeps_the_role(store: Store):
    # given
    s = dataclasses.replace(sso_settings(), sso_default_role='viewer')
    Users(store.db).create('max@example.com', 'Max', 'admin', source='sso')

    # when
    client, _ = sso_login_as(store, {'sub': 'max@example.com', 'roles': []}, s=s)
    first = Users(store.db).get('max@example.com')
    client2, _ = sso_login_as(store, {'sub': 'max@example.com', 'roles': [s.admin_role]}, s=s)
    client3, _ = sso_login_as(store, {'sub': 'max@example.com', 'roles': []}, s=s)

    # then
    assert client.get('/api/me').json()['admin'] is True
    assert (first['role'], first['roleFrom'], first['adminRole']) == ('admin', 'admin', 'admin')
    assert client2.get('/api/me').json()['admin'] is True
    assert client3.get('/api/me').json()['admin'] is True
    assert Users(store.db).get('max@example.com')['roleFrom'] == 'admin'


def test_a_disabled_users_cookie_is_not_renewed(store: Store, endpoint: TokenEndpoint):
    # given
    clock = Clock()
    client = app_client(store, endpoint, clock=clock)
    log_in(client)
    Users(store.db).set_disabled('oscar.green@example.com', True)
    clock.advance(6 * 60)

    # when
    res = client.get('/api/rev')

    # then
    assert session_headers(res) == []


@pytest.mark.parametrize('name, expected', [('   ', 'zed@example.com'), ('unknown', 'zed@example.com'), ('  Zed  ', 'Zed'), ('x' * 300, 'x' * 120)])
def test_sso_display_name_is_stripped_cut_and_falls_back_to_the_email(store: Store, name: str, expected: str):
    # when
    client, res = sso_login_as(store, {'sub': 'zed@example.com', 'name': name})

    # then
    assert res.status_code == 303
    assert Users(store.db).get('zed@example.com')['name'] == expected


def test_mcp_and_page_share_the_email_identity_rule():
    # then
    assert sso_email({'email': 'A@Example.com', 'sub': 'x'}) == 'a@example.com'
    assert sso_email({'sub': 'abc'}) is None


class FlakyCollection:
    def __init__(self, db, name):
        self.db = db
        self.name = name

    def __getattr__(self, attr):
        target = getattr(self.db.real[self.name], attr)

        def call(*args, **kwargs):
            self.db.touched.append(self.name)
            try:
                asyncio.get_running_loop()
                self.db.on_loop.append(self.name)
            except RuntimeError:
                pass
            if self.db.down:
                if self.db.on_fail:
                    self.db.on_fail()
                raise ServerSelectionTimeoutError('mongo is down')
            return target(*args, **kwargs)
        return call


class FlakyDb:
    def __init__(self, real):
        self.real = real
        self.down = True
        self.touched = []
        self.on_loop = []
        self.on_fail = None

    def __getitem__(self, name):
        if name in ('meta', 'users'):
            return FlakyCollection(self, name)
        return self.real[name]

    def __getattr__(self, name):
        return getattr(self.real, name)


def flaky_store(store: Store) -> FlakyDb:
    flaky = FlakyDb(store.db)
    store.db = flaky
    return flaky


def test_startup_and_requests_survive_mongo_being_down_and_recover(store: Store, endpoint: TokenEndpoint):
    # given
    secret = session_secret(store.db, sso_settings())
    cookie = Sessions(secret, 3600, 7200).issue('admin@example.com', 'admin', 'local')
    flaky = flaky_store(store)
    s = sso_settings()
    clock = Clock()

    # when
    with app_client(store, endpoint, clock=clock, s=s) as client:
        live = client.get('/actuator/health/liveness')
        client.cookies.set(SESSION_COOKIE, cookie)
        me_down = client.get('/api/me')
        client.cookies.clear()
        start = client.get('/login/sso', follow_redirects=False)
        flaky.down = False
        clock.advance(6)
        me_up = client.get('/api/me')
        client.cookies.set(SESSION_COOKIE, cookie)
        recovered = client.get('/api/me')

    # then
    assert live.status_code == 200
    assert me_down.status_code == 503
    assert start.status_code == 503
    assert me_up.json()['user'] is None
    assert recovered.json()['user'] == 'admin@example.com'


def test_login_fails_with_a_503_while_mongo_is_down(store: Store, endpoint: TokenEndpoint):
    # given
    s = sso_settings()
    flaky = flaky_store(store)
    flaky.down = False
    client = app_client(store, endpoint, s=s)
    start = client.get('/login/sso', follow_redirects=False)
    state_param = client.idp.authorize(start.headers['location'])['state']
    flaky.down = True
    touched_before = len(flaky.touched)

    # when
    res = client.get('/authorized', params={'code': 'auth-code', 'state': state_param}, follow_redirects=False)

    # then
    assert res.status_code == 503
    assert 'The login could not be completed.' in res.text
    assert SESSION_COOKIE not in res.cookies
    assert len(flaky.touched) > touched_before


def test_an_env_key_never_touches_mongo_for_the_secret(store: Store):
    # given
    flaky = flaky_store(store)
    flaky.down = False
    s = dataclasses.replace(settings(), secret_key='from-env')
    cookie = Sessions(derive_secret('from-env'), 3600, 7200).issue('admin@example.com', 'admin', 'local')

    # when
    with TestClient(create_app(s, store), base_url='https://testserver') as client:
        client.cookies.set(SESSION_COOKIE, cookie)
        me = client.get('/api/me').json()

    # then
    assert me['user'] == 'admin@example.com'
    assert 'meta' not in flaky.touched
    assert 'users' in flaky.touched


def down_app(store: Store, clock: Clock, s: Settings | None = None):
    flaky = flaky_store(store)
    s = s or sso_settings()
    client = app_client(store, clock=clock, s=s)
    return client, flaky


def test_a_failed_key_lookup_is_remembered_for_five_seconds(store: Store, caplog):
    # given
    clock = Clock()
    real_secret = session_secret(store.db, sso_settings())
    cookie = Sessions(real_secret, 3600, 7200, clock=clock).issue('admin@example.com', 'admin', 'local')
    client, flaky = down_app(store, clock)
    flaky.down = False
    client.get('/api/me')
    flaky.down = True
    client.cookies.set(SESSION_COOKIE, cookie)
    caplog.set_level('WARNING')

    # when
    before = len(flaky.touched)
    first = client.get('/api/me').json()
    touched = len(flaky.touched)
    second = client.get('/api/me').json()
    inside = len(flaky.touched)
    clock.advance(6)
    flaky.down = False
    later = client.get('/api/me').json()

    # then
    assert first['user'] is None and second['user'] is None
    assert touched > before and inside == touched, 'the first request tried Mongo, the second did not'
    assert len([r for r in caplog.records if 'session key not available' in r.getMessage()]) == 1
    assert later['user'] == 'admin@example.com'


def test_a_failed_user_lookup_is_remembered_for_five_seconds(store: Store, caplog):
    # given
    clock = Clock()
    cookie = Sessions(session_secret(store.db, sso_settings()), 3600, 7200, clock=clock).issue('admin@example.com', 'admin', 'local')
    client, flaky = down_app(store, clock)
    flaky.down = False
    client.cookies.set(SESSION_COOKIE, cookie)
    assert client.get('/api/me').json()['user'] == 'admin@example.com'
    clock.advance(31)
    flaky.down = True
    caplog.set_level('WARNING')

    # when
    before = len(flaky.touched)
    first = client.get('/api/me').json()
    touched = len(flaky.touched)
    second = client.get('/api/me').json()
    inside = len(flaky.touched)
    clock.advance(6)
    flaky.down = False
    later = client.get('/api/me').json()

    # then
    assert first['user'] is None and second['user'] is None
    assert touched > before and inside == touched, 'the first request tried Mongo, the second did not'
    assert len([r for r in caplog.records if 'user lookup failed' in r.getMessage()]) == 1
    assert later['user'] == 'admin@example.com'


def all_at_once(clients: list[TestClient], path: str) -> list[int]:
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(clients)) as pool:
        return [f.result() for f in [pool.submit(lambda c=c: c.get(path).status_code) for c in clients]]


def failing_together(calls: list, count: int):
    gate = threading.Barrier(count, timeout=0.5)

    def fail(*args, **kwargs):
        calls.append(args)
        try:
            gate.wait()
        except threading.BrokenBarrierError:
            pass
        raise ServerSelectionTimeoutError('mongo is down')
    return fail


def test_concurrent_requests_share_one_failing_user_lookup(store: Store, monkeypatch, caplog):
    # given
    caplog.set_level('WARNING')
    cookie = Sessions(session_secret(store.db, settings()), 3600, 7200).issue('admin@example.com', 'admin', 'local')
    app = create_app(settings(public_read=False), store)
    calls = []
    monkeypatch.setattr(Users, 'get', failing_together(calls, 5))
    clients = [TestClient(app, base_url='https://testserver', cookies={SESSION_COOKIE: cookie}) for _ in range(5)]

    # when
    codes = all_at_once(clients, '/api/rev')

    # then
    assert len(calls) == 1, 'one probe; the others wait for it and share its answer'
    assert codes == [503] * 5
    assert len([r for r in caplog.records if 'user lookup failed' in r.getMessage()]) == 1


def test_cold_lookups_of_different_users_run_together_and_those_of_one_user_run_once(store: Store, monkeypatch):
    # given
    secret = session_secret(store.db, settings())
    cookie = lambda email: Sessions(secret, 3600, 7200).issue(email, 'viewer', 'local')
    app = create_app(settings(public_read=False), store)
    real = Users.get
    meets = threading.Barrier(2, timeout=3)
    calls, together = [], []

    def slow_get(self, name):
        calls.append(name)
        try:
            meets.wait()
            together.append(name)
        except threading.BrokenBarrierError:
            pass
        return real(self, name)

    for email in ('ann@example.com', 'bob@example.com'):
        Users(store.db).create(email, email, 'viewer')
    monkeypatch.setattr(Users, 'get', slow_get)
    two = [TestClient(app, base_url='https://testserver', cookies={SESSION_COOKIE: cookie(email)}) for email in ('ann@example.com', 'bob@example.com')]

    # when
    codes = all_at_once(two, '/api/rev')
    different = sorted(together)
    calls.clear()
    meets.reset()
    monkeypatch.setattr(Users, 'get', lambda self, name: calls.append(name) or time.sleep(0.2) or real(self, name))
    app2 = create_app(settings(public_read=False), store)
    same = [TestClient(app2, base_url='https://testserver', cookies={SESSION_COOKIE: cookie('ann@example.com')}) for _ in range(4)]
    codes_same = all_at_once(same, '/api/rev')

    # then
    assert codes == [200, 200] and codes_same == [200] * 4
    assert different == ['ann@example.com', 'bob@example.com'], 'both lookups were inside Users.get at the same moment'
    assert calls == ['ann@example.com'], 'four requests of one user probe once and share the answer'


def test_concurrent_requests_share_one_failing_key_lookup(store: Store, monkeypatch, caplog):
    # given
    caplog.set_level('WARNING')
    cookie = Sessions(session_secret(store.db, settings()), 3600, 7200).issue('admin@example.com', 'admin', 'local')
    app = create_app(settings(public_read=False), store)
    calls = []
    monkeypatch.setattr('skopax.app.session_secret', failing_together(calls, 5))
    clients = [TestClient(app, base_url='https://testserver', cookies={SESSION_COOKIE: cookie}) for _ in range(5)]

    # when
    codes = all_at_once(clients, '/api/rev')

    # then
    assert len(calls) == 1, 'one probe; the others wait for it and share its answer'
    assert codes == [503] * 5
    assert len([r for r in caplog.records if 'session key not available' in r.getMessage()]) == 1


def test_the_chat_secret_is_a_placeholder_until_the_key_resolves(store: Store):
    # given
    clock = Clock()
    s = dataclasses.replace(sso_settings(), chat_ui=True)
    real_secret = session_secret(store.db, s)
    cookie = Sessions(real_secret, 3600, 7200, clock=clock).issue('admin@example.com', 'admin', 'local')
    client, flaky = down_app(store, clock, s)
    placeholder = os.environ['CHAINLIT_AUTH_SECRET']
    client.cookies.set(SESSION_COOKIE, cookie)

    # when
    client.get('/api/me')
    still = os.environ['CHAINLIT_AUTH_SECRET']
    clock.advance(6)
    flaky.down = False
    client.get('/api/me')

    # then
    assert placeholder == still != chatui.secret_of(real_secret)
    assert os.environ['CHAINLIT_AUTH_SECRET'] == chatui.secret_of(real_secret)


def test_cookie_requests_reach_mongo_off_the_event_loop(store: Store):
    # given
    clock = Clock()
    cookie = Sessions(session_secret(store.db, sso_settings()), 3600, 7200, clock=clock).issue('admin@example.com', 'admin', 'local')
    client, flaky = down_app(store, clock)
    client.cookies.set(SESSION_COOKIE, cookie)
    flaky.down = False
    before = len(flaky.touched)

    # when
    client.get('/api/me')
    clock.advance(31)
    client.get('/api/rev')

    # then
    assert len(flaky.touched) > before
    assert flaky.on_loop == []


def test_the_user_pause_counts_from_when_the_lookup_failed(store: Store, caplog):
    # given
    clock = Clock()
    cookie = Sessions(session_secret(store.db, sso_settings()), 3600, 7200, clock=clock).issue('admin@example.com', 'admin', 'local')
    client, flaky = down_app(store, clock)
    flaky.down = False
    client.cookies.set(SESSION_COOKIE, cookie)
    assert client.get('/api/me').json()['user'] == 'admin@example.com'
    clock.advance(31)
    flaky.down = True
    flaky.on_fail = lambda: clock.advance(3)
    caplog.set_level('WARNING')

    # when
    client.get('/api/me')
    touched = len(flaky.touched)
    clock.advance(4)
    client.get('/api/me')

    # then
    assert len(flaky.touched) == touched
    assert len([r for r in caplog.records if 'user lookup failed' in r.getMessage()]) == 1


def test_cookie_names():
    # then
    from skopax.auth import LOGIN_COOKIE
    assert SESSION_COOKIE == 'skopax_session'
    assert LOGIN_COOKIE == 'skopax_login'
