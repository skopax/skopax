import concurrent.futures
import dataclasses
import hashlib
import pathlib
import threading

import pytest
from fastapi.testclient import TestClient
from pymongo import ReadPreference, WriteConcern
from pymongo.errors import AutoReconnect

from tests.conftest import ADMIN_EMAIL, AUTH, settings
from tests.test_auth import Clock, TokenEndpoint, access_token, app_client, sso_login_as, sso_settings
from tests.test_setup import app_client as setup_client, form, fresh_store, issued_code
from skopax.app import create_app
from skopax.auth import SESSION_COOKIE
from skopax.messages import msg
from skopax.setup import BadCode, SetupGate
from skopax.store import Store
from skopax.throttle import Throttle
from skopax.users import Users

PASSWORD = 'correct horse battery'
NEW_PASSWORD = 'another long passphrase'
WRONG = 'wrong password value'
HTML = {'Accept': 'text/html'}
WRONG_TEXT = 'Wrong email or password.'
BLOCKED_TEXT = 'Too many attempts. Try again in a few minutes.'
SPILL = 'hunter2-spill-'


def make_client(store: Store, **over) -> TestClient:
    return TestClient(create_app(dataclasses.replace(settings(), **over), store), base_url='https://testserver', follow_redirects=False)


def with_password(store: Store, email: str = ADMIN_EMAIL, password: str = PASSWORD) -> Users:
    users = Users(store.db)
    users.set_password(email, password)
    return users


def post_login(client: TestClient, email: str = ADMIN_EMAIL, password: str = PASSWORD, **extra):
    return client.post('/login', data={'email': email, 'password': password, **extra})


def attempts(store: Store, key: str) -> int:
    return store.db['login_attempts'].count_documents({'key': key})


def test_login_success_sets_session_and_redirects_to_next(store: Store):
    # given
    users = with_password(store)
    client = make_client(store)

    # when
    res = post_login(client, email=' Admin@Example.com ', next='/c/7')
    me = client.get('/api/me').json()

    # then
    assert (res.status_code, res.headers['location']) == (303, '/c/7')
    cookie = next(c for c in res.headers.get_list('set-cookie') if c.startswith(f'{SESSION_COOKIE}='))
    assert 'HttpOnly' in cookie and 'Secure' in cookie and 'samesite=lax' in cookie.lower()
    assert me['user'] == ADMIN_EMAIL and me['source'] == 'local' and me['canChangePassword'] is True
    assert users.get(ADMIN_EMAIL)['lastLogin']


@pytest.mark.parametrize('target', ['https://evil.example/', '//evil.example/x', '/\\evil.example'])
def test_next_cannot_leave_the_site(store: Store, target: str):
    # given
    with_password(store)
    client = make_client(store)

    # when
    res = post_login(client, next=target)

    # then
    assert (res.status_code, res.headers['location']) == (303, '/')


def test_unknown_email_and_wrong_password_look_the_same(store: Store, monkeypatch):
    # given
    with_password(store)
    Users(store.db).create('bob@example.com', 'Bob', 'viewer')
    Users(store.db).create('carol@example.com', 'Carol', 'viewer', PASSWORD)
    Users(store.db).set_disabled('carol@example.com', True)
    client = make_client(store)
    calls = []
    real = hashlib.scrypt
    monkeypatch.setattr(hashlib, 'scrypt', lambda *a, **k: calls.append(1) or real(*a, **k))

    # when
    seen = []
    for email in ('nobody@example.com', ADMIN_EMAIL, 'bob@example.com', 'carol@example.com'):
        before = len(calls)
        res = post_login(client, email=email, password=WRONG if email == ADMIN_EMAIL else PASSWORD)
        seen.append((res.status_code, res.text.count(WRONG_TEXT), len(calls) - before, SESSION_COOKIE in res.cookies, 'www-authenticate' in res.headers))

    # then
    assert seen == [(400, 1, 1, False, False)] * 4, 'a failed form login is a 400 form error, never a 401 without a challenge'


def test_login_throttles_after_five_failures_per_email(store: Store):
    # given
    with_password(store)
    client = make_client(store)

    # when
    first = [post_login(client, password=WRONG) for _ in range(5)]
    blocked = post_login(client, password=PASSWORD)
    other_user_same_ip = post_login(client, email='bob@example.com')

    # then
    assert [r.status_code for r in first] == [400] * 5
    assert blocked.status_code == 429 and BLOCKED_TEXT in blocked.text and SESSION_COOKIE not in blocked.cookies
    assert other_user_same_ip.status_code == 400


def test_login_throttles_after_twenty_failures_per_ip(store: Store):
    # given
    with_password(store)
    client = make_client(store)

    # when
    first = [post_login(client, email=f'user{n}@example.com') for n in range(20)]
    blocked = post_login(client)

    # then
    assert [r.status_code for r in first] == [400] * 20
    assert blocked.status_code == 429 and BLOCKED_TEXT in blocked.text


def test_a_blocked_post_does_not_run_scrypt_or_extend_the_block(store: Store, monkeypatch):
    # given
    with_password(store)
    client = make_client(store)
    for _ in range(5):
        post_login(client, password=WRONG)
    calls = []
    real = hashlib.scrypt
    monkeypatch.setattr(hashlib, 'scrypt', lambda *a, **k: calls.append(1) or real(*a, **k))

    # when
    res = post_login(client)

    # then
    assert res.status_code == 429 and not calls
    assert attempts(store, f'email:{ADMIN_EMAIL}') == 5


def test_a_success_clears_the_email_key_only(store: Store):
    # given
    with_password(store)
    client = make_client(store)
    for _ in range(4):
        post_login(client, password=WRONG)

    # when
    ok = post_login(client)

    # then
    assert ok.status_code == 303
    assert attempts(store, f'email:{ADMIN_EMAIL}') == 0
    assert attempts(store, 'ip:testclient') == 4


def test_throttle_window_expires(store: Store):
    # given
    clock = Clock()
    throttle = Throttle(store.db, clock=clock)

    # when
    held = [throttle.reserve('email:a@example.com', 'ip:1.2.3.4') for _ in range(5)]
    sixth = throttle.reserve('email:a@example.com', 'ip:1.2.3.4')
    clock.advance(901)
    later = throttle.reserve('email:a@example.com', 'ip:1.2.3.4')

    # then
    assert all(held) and sixth is None and later is not None


def test_throttle_clear_and_indexes(store: Store):
    # given
    throttle = Throttle(store.db)
    throttle.ensure_indexes()
    for _ in range(5):
        throttle.reserve('email:a@example.com')

    # when
    throttle.clear('email:a@example.com')

    # then
    assert throttle.reserve('email:a@example.com') is not None
    ttl = [v for v in store.db['login_attempts'].index_information().values() if v.get('expireAfterSeconds') == 900]
    assert ttl and ttl[0]['key'] == [('at', 1)]


def test_the_lifespan_creates_the_attempts_ttl_index(store: Store):
    # given
    app = create_app(settings(), store)
    assert 'login_attempts' not in store.db.list_collection_names()

    # when
    with TestClient(app):
        info = store.db['login_attempts'].index_information()

    # then
    assert any(v.get('expireAfterSeconds') == 900 for v in info.values())


def test_disabled_user_cannot_log_in(store: Store):
    # given
    users = with_password(store)
    users.create('bob@example.com', 'Bob', 'operator', PASSWORD)
    users.set_disabled('bob@example.com', True)
    client = make_client(store)

    # when
    res = post_login(client, email='bob@example.com')

    # then
    assert res.status_code == 400 and WRONG_TEXT in res.text and SESSION_COOKIE not in res.cookies


def test_cross_origin_login_post_is_refused(store: Store):
    # given
    with_password(store)
    client = make_client(store)

    # when
    res = client.post('/login', data={'email': ADMIN_EMAIL, 'password': PASSWORD}, headers={'Origin': 'https://evil.example'})

    # then
    assert res.status_code == 403 and SESSION_COOKIE not in res.cookies
    assert attempts(store, f'email:{ADMIN_EMAIL}') == 0


def test_an_oversize_login_body_is_refused(store: Store):
    # given
    client = make_client(store)

    # when
    res = client.post('/login', data={'email': ADMIN_EMAIL, 'password': 'x' * 20000})

    # then
    assert res.status_code == 413


def test_login_form_shows_next_and_sso_link_only_when_sso_is_on(store: Store):
    # given
    plain = make_client(store)
    sso = app_client(store, TokenEndpoint(access_token([])))

    # when
    off = plain.get('/login?next=/c/7%22%3E%3Cb%3E', headers=HTML)
    on = sso.get('/login?next=/c/7', headers=HTML)

    # then
    assert off.status_code == 200 and '/login/sso' not in off.text and 'Sign in with SSO' not in off.text
    assert '<b>' not in off.text and 'value="/c/7&quot;&gt;&lt;b&gt;"' in off.text
    assert on.status_code == 200 and 'href="/login/sso?next=%2Fc%2F7"' in on.text and 'Sign in with SSO' in on.text
    assert 'value="/c/7"' in on.text
    assert 'http://' not in off.text and 'https://' not in off.text.replace('https://skopax.io', '')


def test_failed_login_rerender_escapes_the_typed_email(store: Store):
    # given
    client = make_client(store)

    # when
    res = post_login(client, email='"><script>x</script>@example.com')

    # then
    assert '<script>' not in res.text and '&lt;script&gt;' in res.text


def test_login_answers_503_when_the_session_key_is_not_available(store: Store, monkeypatch):
    # given
    with_password(store)
    def down(*_):
        raise AutoReconnect('down')
    monkeypatch.setattr('skopax.app.session_secret', down)
    client = make_client(store)

    # when
    res = post_login(client)

    # then
    assert res.status_code == 503 and SESSION_COOKIE not in res.cookies


def test_login_answers_503_when_mongo_fails_during_the_check(store: Store, monkeypatch):
    # given
    with_password(store)
    client = make_client(store)
    def down(self, *_):
        raise AutoReconnect('down')
    monkeypatch.setattr(Users, 'authenticate', down)

    # when
    res = post_login(client)

    # then
    assert res.status_code == 503


def test_get_logout_only_shows_a_log_out_button(store: Store):
    # given
    with_password(store)
    client = make_client(store)
    post_login(client)

    # when
    res = client.get('/logout')
    me = client.get('/api/me').json()

    # then
    assert res.status_code == 200 and res.headers['content-type'].startswith('text/html')
    assert '<form method="post" action="/logout"><button type="submit">Log out</button></form>' in res.text
    assert not [h for h in res.headers.get_list('set-cookie') if h.startswith(SESSION_COOKIE)]
    assert me['user'] == ADMIN_EMAIL, 'a link or a prefetch of /logout does not end the session'


def test_post_logout_drops_the_session_and_refuses_another_site(store: Store):
    # given
    with_password(store)
    client = make_client(store)
    post_login(client)

    # when
    other = client.post('/logout', headers={'Origin': 'https://evil.example.com'})
    kept = client.get('/api/me').json()['user']
    res = client.post('/logout', headers={'Origin': 'https://testserver'})
    gone = client.get('/api/me').json()['user']

    # then
    assert other.status_code == 403 and other.json() == {'detail': msg('login.cross_site')} and kept == ADMIN_EMAIL
    assert (res.status_code, res.headers['location']) == (303, '/') and gone is None


def test_me_reports_the_new_fields(store: Store):
    # given
    with_password(store)
    client = make_client(store)

    # when
    guest = client.get('/api/me').json()
    post_login(client)
    signed = client.get('/api/me').json()

    # then
    assert guest['loginEnabled'] is True and guest['sso'] is False
    assert guest['userRole'] is None and guest['source'] is None and guest['canChangePassword'] is False and guest['name'] is None
    assert signed['userRole'] == 'admin' and signed['admin'] is True and signed['name'] == 'Admin'
    assert not {'role', 'adminRole', 'setupNeeded'} & (set(guest) | set(signed)), 'the old IdP role names and the setup flag are gone'


def test_sso_login_creates_user_with_default_role(store: Store):
    # given
    s = dataclasses.replace(sso_settings(), sso_default_role='viewer')

    # when
    client, res = sso_login_as(store, {'sub': 'dana@example.com', 'name': 'Dana Reed', 'roles': []}, s=s)
    stored = Users(store.db).get('dana@example.com')
    me = client.get('/api/me').json()

    # then
    assert res.status_code == 303
    assert (stored['role'], stored['source'], stored['name']) == ('viewer', 'sso', 'Dana Reed')
    assert me['user'] == 'dana@example.com' and me['operator'] is False and me['source'] == 'sso' and me['canChangePassword'] is False


def test_sso_admin_claim_makes_admin(store: Store):
    # given
    s = dataclasses.replace(sso_settings(), sso_default_role='viewer')

    # when
    client, _ = sso_login_as(store, {'sub': 'ivy@example.com', 'roles': [s.admin_role]}, s=s)

    # then
    assert Users(store.db).get('ivy@example.com')['role'] == 'admin'
    assert client.get('/api/me').json()['admin'] is True


def test_a_lone_surrogate_password_is_an_input_error_not_a_crash(store: Store):
    # given
    users = with_password(store)
    client = make_client(store)
    client.headers['Content-Type'] = 'application/json'
    post_login(client)
    lone = 'long enough \\ud800 pass'

    # when
    created = client.post('/api/admin/users', content='{"email": "erin@example.com", "name": "Erin", "role": "viewer", "password": "' + lone + '"}')
    changed = client.post('/api/me/password', content='{"current": "' + PASSWORD + '", "new": "' + lone + '"}')

    # then
    assert (created.status_code, [e['loc'] for e in created.json()['detail']]) == (422, [['body', 'password']])
    assert (changed.status_code, [e['loc'] for e in changed.json()['detail']]) == (422, [['body', 'new']])
    assert users.get('erin@example.com') is None and users.authenticate(ADMIN_EMAIL, PASSWORD) is not None


@pytest.mark.parametrize('method, path, body', [
    ('PATCH', '/api/admin/users/erin@example.com', {'password': SPILL * 200}),
    ('POST', '/api/admin/users', {'email': 'erin@example.com', 'name': 'Erin', 'role': 'viewer', 'password': SPILL * 200}),
    ('PUT', '/api/admin/settings', {'key': 'slack.channel', 'value': SPILL * 1500}),
], ids=['password on a user change', 'password on a new user', 'settings value'])
def test_a_refused_body_never_comes_back_in_the_422(store: Store, method: str, path: str, body: dict):
    # given
    client = make_client(store)

    # when
    res = client.request(method, path, json=body, headers=AUTH)

    # then
    assert res.status_code == 422
    assert SPILL not in res.text
    assert [sorted(item) for item in res.json()['detail']] == [['loc', 'msg', 'type']], 'the 422 keeps where and why, never what was sent'


def test_an_unknown_field_is_named_in_the_422_but_its_value_is_not(store: Store):
    # given
    client = make_client(store)

    # when
    res = client.put('/api/admin/features/push', json={'on': False, 'colour': SPILL * 10}, headers=AUTH)

    # then
    assert res.status_code == 422
    assert [item['loc'] for item in res.json()['detail']] == [['body', 'colour']], "the name is the client's own key, so it stays"
    assert SPILL not in res.text and [sorted(item) for item in res.json()['detail']] == [['loc', 'msg', 'type']]


def test_change_own_password(store: Store):
    # given
    users = with_password(store)
    client = make_client(store)
    post_login(client)

    # when
    res = client.post('/api/me/password', json={'current': PASSWORD, 'new': NEW_PASSWORD})
    fresh = make_client(store)
    old = post_login(fresh, password=PASSWORD)
    new = post_login(fresh, password=NEW_PASSWORD)

    # then
    assert res.status_code == 204 and res.content == b''
    assert users.authenticate(ADMIN_EMAIL, NEW_PASSWORD) is not None
    assert old.status_code == 400 and new.status_code == 303


def test_a_viewer_changes_their_own_password_and_keeps_the_session(store: Store):
    # given
    users = Users(store.db)
    users.create('carol@example.com', 'Carol', 'viewer', PASSWORD)
    client = make_client(store)
    post_login(client, email='carol@example.com')

    # when
    res = client.post('/api/me/password', json={'current': PASSWORD, 'new': NEW_PASSWORD})
    me = client.get('/api/me').json()

    # then
    assert res.status_code == 204
    assert users.authenticate('carol@example.com', NEW_PASSWORD) is not None
    assert (me['user'], me['userRole']) == ('carol@example.com', 'viewer'), 'the session that changed the password stays signed in'


def test_change_password_refuses_a_wrong_current_or_short_new_password(store: Store):
    # given
    users = with_password(store)
    client = make_client(store)
    post_login(client)

    # when
    wrong = client.post('/api/me/password', json={'current': WRONG, 'new': NEW_PASSWORD})
    short = client.post('/api/me/password', json={'current': PASSWORD, 'new': 'short'})
    missing = client.post('/api/me/password', json={'new': NEW_PASSWORD})

    # then
    assert wrong.status_code == 400 and short.status_code == 400
    assert wrong.json() == short.json() == {'detail': msg('password.error')}
    assert missing.status_code == 422
    assert users.authenticate(ADMIN_EMAIL, PASSWORD) is not None


def test_change_password_counts_wrong_current_against_the_throttle(store: Store):
    # given
    with_password(store)
    client = make_client(store)
    post_login(client)

    # when
    results = [client.post('/api/me/password', json={'current': WRONG, 'new': NEW_PASSWORD}).status_code for _ in range(5)]
    blocked = client.post('/api/me/password', json={'current': PASSWORD, 'new': NEW_PASSWORD})

    # then
    assert results == [400] * 5
    assert blocked.status_code == 429 and blocked.json() == {'detail': BLOCKED_TEXT}
    assert attempts(store, f'email:{ADMIN_EMAIL}') == 5


def test_change_password_needs_a_session_not_a_token(store: Store):
    # given
    with_password(store)
    client = make_client(store)
    body = {'current': PASSWORD, 'new': NEW_PASSWORD}
    bearer = {'Authorization': 'Bearer test-token'}

    # when
    anonymous = client.post('/api/me/password', json=body)
    token_only = client.post('/api/me/password', json=body, headers=bearer)
    post_login(client)
    token_with_session = client.post('/api/me/password', json=body, headers=bearer)
    cross = client.post('/api/me/password', json=body, headers={'Origin': 'https://evil.example'})

    # then
    assert anonymous.status_code == 401 and token_only.status_code == 401 and token_with_session.status_code == 401
    assert cross.status_code == 403
    assert Users(store.db).authenticate(ADMIN_EMAIL, PASSWORD) is not None


def test_an_sso_user_cannot_change_a_password(store: Store):
    # given
    client, _ = sso_login_as(store, {'sub': 'dana@example.com', 'roles': []})

    # when
    res = client.post('/api/me/password', json={'current': PASSWORD, 'new': NEW_PASSWORD})

    # then
    assert res.status_code == 400
    assert 'pw' not in store.db['users'].find_one({'_id': 'dana@example.com'})


def test_change_password_answers_503_when_mongo_fails(store: Store, monkeypatch):
    # given
    with_password(store)
    client = make_client(store)
    post_login(client)
    def down(self, *_):
        raise AutoReconnect('down')
    monkeypatch.setattr(Users, 'set_password', down)

    # when
    res = client.post('/api/me/password', json={'current': PASSWORD, 'new': NEW_PASSWORD})

    # then
    assert res.status_code == 503


def test_slack_operator_lookup_uses_users(store: Store):
    # given
    users = Users(store.db)
    users.create('olga@example.com', 'Olga', 'operator')
    users.create('vic@example.com', 'Vic', 'viewer')
    users.create('dan@example.com', 'Dan', 'operator')
    users.set_disabled('dan@example.com', True)
    operators = create_app(settings(), store).state.operators

    # when
    verdicts = {e: operators.access_of(e)[1] for e in (ADMIN_EMAIL, 'olga@example.com', 'vic@example.com', 'dan@example.com', 'ghost@example.com', 'Olga@Example.com')}

    # then
    assert operators.enabled
    assert verdicts == {ADMIN_EMAIL: True, 'olga@example.com': True, 'vic@example.com': False, 'dan@example.com': False, 'ghost@example.com': False, 'Olga@Example.com': True}


def test_an_explicit_operators_lookup_still_wins(store: Store):
    # given
    operators = create_app(settings(), store, operators_lookup=lambda email: email == 'only@example.com').state.operators

    # when
    verdicts = (operators.access_of('only@example.com')[1], operators.access_of(ADMIN_EMAIL)[1])

    # then
    assert verdicts == (True, False)


def test_setup_throttles_wrong_codes_per_ip(caplog):
    # given
    caplog.set_level('INFO')
    store = fresh_store()
    with setup_client(store) as c:
        c.get('/setup')
        code = issued_code(caplog)

        # when
        wrong = [c.post('/setup', data=form('WRNG-0000')) for _ in range(20)]
        blocked = c.post('/setup', data=form(code))

    # then
    assert [r.status_code for r in wrong] == [400] * 20
    assert blocked.status_code == 429 and msg('setup.error') in blocked.text
    assert Users(store.db).count() == 0


def test_a_successful_setup_clears_the_ip_failures(caplog):
    # given
    caplog.set_level('INFO')
    store = fresh_store()
    with setup_client(store) as c:
        c.get('/setup')
        code = issued_code(caplog)
        for _ in range(3):
            c.post('/setup', data=form('WRNG-0000'))
        before = attempts(store, 'ip:testclient')

        # when
        ok = c.post('/setup', data=form(code))

    # then
    assert before == 3 and ok.status_code == 303
    assert attempts(store, 'ip:testclient') == 0


def from_address(store: Store, n: int, **over) -> TestClient:
    app = create_app(dataclasses.replace(settings(), **over), store)
    return TestClient(app, base_url='https://testserver', follow_redirects=False, client=(f'203.0.113.{n}', 50000))


def from_addresses(store: Store, count: int) -> list[TestClient]:
    app = create_app(settings(), store)
    return [TestClient(app, base_url='https://testserver', follow_redirects=False, client=(f'203.0.113.{n + 1}', 50000)) for n in range(count)]


class Gate:
    def __init__(self, total: int):
        self.total = total
        self.reached = 0
        self.done = 0
        self.open = False
        self.turn = threading.Condition()

    def hold(self, *_a, **_k) -> None:
        with self.turn:
            self.reached += 1
            self.turn.notify_all()
            assert self.turn.wait_for(lambda: self.open, timeout=10)

    def finished(self) -> None:
        with self.turn:
            self.done += 1
            self.turn.notify_all()

    def release_when_every_request_is_held_or_answered(self) -> None:
        with self.turn:
            assert self.turn.wait_for(lambda: self.reached + self.done == self.total, timeout=10)
            self.open = True
            self.turn.notify_all()


def race(gate: Gate, work) -> list:
    def one(n: int):
        try:
            return work(n)
        finally:
            gate.finished()

    with concurrent.futures.ThreadPoolExecutor(max_workers=gate.total) as pool:
        futures = [pool.submit(one, n) for n in range(gate.total)]
        gate.release_when_every_request_is_held_or_answered()
        return [f.result() for f in futures]


def test_login_throttles_per_email_whatever_the_address(store: Store):
    # given
    with_password(store)

    # when
    first = [post_login(from_address(store, n), password=WRONG).status_code for n in range(1, 6)]
    blocked = post_login(from_address(store, 9), password=PASSWORD)

    # then
    assert first == [400] * 5
    assert blocked.status_code == 429 and BLOCKED_TEXT in blocked.text


def test_change_password_throttles_per_email_whatever_the_address(store: Store):
    # given
    with_password(store)
    session = make_client(store)
    post_login(session)
    cookie = session.cookies.get(SESSION_COOKIE)

    def attempt(n: int, current: str) -> int:
        c = from_address(store, n)
        c.cookies.set(SESSION_COOKIE, cookie)
        return c.post('/api/me/password', json={'current': current, 'new': NEW_PASSWORD}).status_code

    # when
    first = [attempt(n, WRONG) for n in range(1, 6)]
    blocked = attempt(9, PASSWORD)

    # then
    assert first == [400] * 5 and blocked == 429


def test_concurrent_wrong_logins_reach_the_password_check_at_most_five_times(store: Store, monkeypatch):
    # given
    with_password(store)
    gate = Gate(30)
    monkeypatch.setattr(Users, 'authenticate', gate.hold)
    clients = from_addresses(store, 30)

    # when
    codes = race(gate, lambda n: post_login(clients[n], password=WRONG).status_code)

    # then
    assert 1 <= gate.reached <= 5
    assert codes.count(400) == gate.reached and codes.count(429) == 30 - gate.reached


def test_concurrent_wrong_current_passwords_reach_the_check_at_most_five_times(store: Store, monkeypatch):
    # given
    with_password(store)
    session = make_client(store)
    post_login(session)
    cookie = session.cookies.get(SESSION_COOKIE)
    clients = from_addresses(store, 30)
    for c in clients:
        c.cookies.set(SESSION_COOKIE, cookie)
    gate = Gate(30)
    monkeypatch.setattr(Users, 'authenticate', gate.hold)

    # when
    codes = race(gate, lambda n: clients[n].post('/api/me/password', json={'current': WRONG, 'new': NEW_PASSWORD}).status_code)

    # then
    assert 1 <= gate.reached <= 5
    assert codes.count(400) == gate.reached and codes.count(429) == 30 - gate.reached


def test_concurrent_wrong_setup_codes_reach_the_code_check_at_most_five_times(caplog, monkeypatch):
    # given
    caplog.set_level('INFO')
    store = fresh_store()
    gate = Gate(30)

    def complete(self, *_a):
        gate.hold()
        raise BadCode()

    monkeypatch.setattr(SetupGate, 'complete', complete)
    app = create_app(settings(), store)
    clients = [TestClient(app, base_url='https://testserver', follow_redirects=False, client=('203.0.113.7', 50000)) for _ in range(30)]

    # when
    codes = race(gate, lambda n: clients[n].post('/setup', data=form('WRNG-0000')).status_code)

    # then
    assert 1 <= gate.reached <= 20
    assert codes.count(400) == gate.reached and codes.count(429) == 30 - gate.reached


def test_a_valid_cookie_with_no_session_key_gets_503_on_password_change(store: Store, monkeypatch):
    # given
    with_password(store)
    session = make_client(store)
    post_login(session)
    cookie = session.cookies.get(SESSION_COOKIE)
    def down(*_):
        raise AutoReconnect('down')
    monkeypatch.setattr('skopax.app.session_secret', down)
    other = make_client(store)
    other.cookies.set(SESSION_COOKIE, cookie)

    # when
    res = other.post('/api/me/password', json={'current': PASSWORD, 'new': NEW_PASSWORD})

    # then
    assert res.status_code == 503 and res.json() == {'detail': msg('db.unavailable')}


def test_the_image_trusts_forwarded_headers_only_from_configured_proxies():
    # given
    text = (pathlib.Path(__file__).parent.parent / 'Dockerfile').read_text()

    # then
    assert '--proxy-headers' in text
    assert '--forwarded-allow-ips' not in text


def test_six_failures_across_emails_from_one_address_do_not_block_it(store: Store):
    # given
    with_password(store)
    client = make_client(store)

    # when
    first = [post_login(client, email=f'user{n}@example.com').status_code for n in range(6)]
    ok = post_login(client)

    # then
    assert first == [400] * 6 and ok.status_code == 303


def test_a_database_error_during_login_does_not_count_as_a_failure(store: Store, monkeypatch):
    # given
    with_password(store)
    client = make_client(store)
    def down(self, *_):
        raise AutoReconnect('down')
    monkeypatch.setattr(Users, 'authenticate', down)

    # when
    res = post_login(client)

    # then
    assert res.status_code == 503
    assert attempts(store, f'email:{ADMIN_EMAIL}') == 0 and attempts(store, 'ip:testclient') == 0


def test_a_database_error_during_password_change_does_not_count_as_a_failure(store: Store, monkeypatch):
    # given
    with_password(store)
    client = make_client(store)
    post_login(client)
    def down(self, *_):
        raise AutoReconnect('down')
    monkeypatch.setattr(Users, 'authenticate', down)

    # when
    res = client.post('/api/me/password', json={'current': PASSWORD, 'new': NEW_PASSWORD})

    # then
    assert res.status_code == 503 and attempts(store, f'email:{ADMIN_EMAIL}') == 0


def test_a_count_failure_inside_reserve_releases_the_new_rows(store: Store):
    # given
    throttle = Throttle(store.db)
    throttle.col = _FailingCount(throttle.col)

    # when
    with pytest.raises(AutoReconnect):
        throttle.reserve('email:a@example.com')

    # then
    assert store.db['login_attempts'].count_documents({}) == 0


class _FailingCount:
    def __init__(self, col):
        self.col = col

    def count_documents(self, *_a, **_k):
        raise AutoReconnect('down')

    def __getattr__(self, name):
        return getattr(self.col, name)


def test_an_invalid_new_password_is_not_counted_as_a_failure(store: Store):
    # given
    with_password(store)
    client = make_client(store)
    post_login(client)

    # when
    res = client.post('/api/me/password', json={'current': PASSWORD, 'new': 'short'})

    # then
    assert res.status_code == 400
    assert attempts(store, f'email:{ADMIN_EMAIL}') == 0 and attempts(store, 'ip:testclient') == 0


def test_a_setup_form_error_is_not_counted_and_keeps_the_code(caplog):
    # given
    caplog.set_level('INFO')
    store = fresh_store()
    with setup_client(store) as c:
        c.get('/setup')
        code = issued_code(caplog)

        # when
        bad = c.post('/setup', data=form(code, password='short', password2='short'))
        counted = attempts(store, 'ip:testclient')
        ok = c.post('/setup', data=form(code))

    # then
    assert bad.status_code == 400 and ok.status_code == 303
    assert counted == 0


def forwarded_warnings(caplog) -> list:
    return [r for r in caplog.records if 'X-Forwarded-For' in r.getMessage() and r.name == 'skopax']


def test_untrusted_forwarded_for_logs_one_warning_per_hour(store: Store, caplog):
    # given
    caplog.set_level('WARNING')
    client = make_client(store)

    # when
    client.get('/actuator/health/liveness')
    none = len(forwarded_warnings(caplog))
    client.get('/actuator/health/liveness', headers={'X-Forwarded-For': '198.51.100.7'})
    first = len(forwarded_warnings(caplog))
    client.get('/actuator/health/liveness', headers={'X-Forwarded-For': '198.51.100.8, 198.51.100.9'})
    second = len(forwarded_warnings(caplog))

    # then
    assert (none, first, second) == (0, 1, 1)
    said = forwarded_warnings(caplog)[0].getMessage()
    assert 'FORWARDED_ALLOW_IPS' in said and 'testclient' in said, 'the warning names the peer address to trust'


def test_forwarded_for_names_the_peer_in_any_form_or_header_line_and_logs_nothing(store: Store, caplog):
    # given
    caplog.set_level('WARNING')
    cases = {'bracketed v6 with a port': ('2001:db8::7', {'X-Forwarded-For': '203.0.113.9, [2001:DB8::7]:443'}),
             'bare v6': ('2001:db8::7', {'X-Forwarded-For': '2001:db8::7'}),
             'v4 with a port': ('198.51.100.7', {'X-Forwarded-For': '198.51.100.7:8080'}),
             'second header line': ('198.51.100.7', [('X-Forwarded-For', '203.0.113.9'), ('X-Forwarded-For', '198.51.100.7')])}

    # when
    warned = []
    for name, (peer, headers) in cases.items():
        caplog.clear()
        TestClient(create_app(settings(), store), base_url='https://testserver', client=(peer, 50000)).get('/actuator/health/liveness', headers=headers)
        if forwarded_warnings(caplog):
            warned.append(name)

    # then
    assert warned == []


def test_forwarded_for_that_names_the_peer_logs_nothing(store: Store, caplog):
    # given
    caplog.set_level('WARNING')
    client = make_client(store)

    # when
    client.get('/actuator/health/liveness', headers={'X-Forwarded-For': '198.51.100.7, testclient'})

    # then
    assert forwarded_warnings(caplog) == []


def test_without_a_client_address_no_shared_ip_bucket_is_used(caplog):
    # given
    caplog.set_level('INFO')
    store = fresh_store()
    with TestClient(create_app(settings(), store), base_url='https://testserver', follow_redirects=False, client=None) as c:
        code = issued_code(caplog)

        # when
        wrong_code = c.post('/setup', data=form('WRNG-0000'))
        rows_after_setup = [d['key'] for d in store.db['login_attempts'].find()]
        done = c.post('/setup', data=form(code))
        Users(store.db).set_password('alice@example.com', PASSWORD)
        wrong_login = post_login(c, email='alice@example.com', password=WRONG)
        rows_after_login = sorted(d['key'] for d in store.db['login_attempts'].find())

    # then
    assert wrong_code.status_code == 400 and rows_after_setup == []
    assert done.status_code == 303
    assert wrong_login.status_code == 400 and rows_after_login == ['email:alice@example.com'], 'no ip:unknown bucket shared by every address-less client'


def test_the_attempts_collection_writes_acknowledged_and_reads_the_primary(store: Store):
    # when
    throttle = Throttle(store.db)

    # then
    assert throttle.col.write_concern.document == WriteConcern(w=1).document == {'w': 1}, 'set on the handle, not left to the server default'
    assert throttle.col.read_preference == ReadPreference.PRIMARY
    assert throttle.reserve() == [] and throttle.clear() is None, 'no keys is a no-op, not an error'
