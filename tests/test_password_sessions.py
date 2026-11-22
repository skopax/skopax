from fastapi.testclient import TestClient

from skopax import users as users_module
from skopax.auth import SESSION_COOKIE
from skopax.store import Store
from skopax.users import Users
from tests.conftest import ADMIN_EMAIL, AUTH
from tests.test_add_user import DANA, PASSWORD, USERS, admin_of, sso, sso_client
from tests.test_login import NEW_PASSWORD, make_client, post_login, with_password

CAROL = 'carol@example.com'
THIRD = 'a third long passphrase'


def signed_in(client: TestClient) -> str | None:
    return client.get('/api/me').json()['user']


def session_of(store: Store, email: str, password: str = PASSWORD) -> TestClient:
    client = make_client(store)
    assert post_login(client, email=email, password=password).status_code == 303
    return client


def test_an_admin_password_reset_ends_the_other_password_sessions_of_that_person(store: Store):
    # given
    Users(store.db).create(CAROL, 'Carol', 'viewer', PASSWORD)
    carol = session_of(store, CAROL)
    admin = TestClient(carol.app, base_url='https://testserver', headers=AUTH, follow_redirects=False)

    # when
    before = signed_in(carol)
    res = admin.patch(f'{USERS}/{CAROL}', json={'password': NEW_PASSWORD})
    after = signed_in(carol)
    again = session_of(store, CAROL, NEW_PASSWORD)

    # then
    assert res.status_code == 200
    assert (before, after) == (CAROL, None), 'the reset ends the session that signed in with the old password'
    assert signed_in(again) == CAROL


def test_a_self_change_keeps_the_current_session_and_ends_the_other_ones(store: Store):
    # given
    Users(store.db).create(CAROL, 'Carol', 'viewer', PASSWORD)
    here, there = session_of(store, CAROL), session_of(store, CAROL)

    # when
    res = here.post('/api/me/password', json={'current': PASSWORD, 'new': NEW_PASSWORD})
    kept, ended = signed_in(here), signed_in(there)
    later = here.post('/api/me/password', json={'current': NEW_PASSWORD, 'new': THIRD})

    # then
    assert res.status_code == 204 and later.status_code == 204
    assert (kept, ended) == (CAROL, None)
    assert signed_in(here) == CAROL


def test_an_admin_resetting_their_own_password_on_the_access_page_stays_signed_in(store: Store):
    # given
    with_password(store, ADMIN_EMAIL, PASSWORD)
    here, there = session_of(store, ADMIN_EMAIL), session_of(store, ADMIN_EMAIL)

    # when
    res = here.patch(f'{USERS}/{ADMIN_EMAIL}', json={'password': NEW_PASSWORD}, headers={'Origin': 'https://testserver'})

    # then
    assert res.status_code == 200, res.text
    assert (signed_in(here), signed_in(there)) == (ADMIN_EMAIL, None)


def test_a_password_reset_leaves_the_sso_session_of_a_linked_account_alone(store: Store):
    # given
    client = sso_client(store)
    admin = admin_of(client)
    assert admin.post(USERS, json={'email': DANA, 'name': 'Dana', 'role': 'viewer', 'password': PASSWORD, 'sso': True}).status_code == 201
    assert sso(client).status_code == 303
    before = signed_in(client)

    # when
    res = admin.patch(f'{USERS}/{DANA}', json={'password': NEW_PASSWORD})

    # then
    assert res.status_code == 200
    assert (before, signed_in(client)) == (DANA, DANA)


def test_a_password_on_an_sso_account_is_hashed_once(store: Store, monkeypatch):
    # given
    admin = admin_of(sso_client(store))
    admin.post(USERS, json={'email': DANA, 'name': 'Dana', 'role': 'viewer', 'sso': True})
    real, calls = users_module.hash_password, []
    monkeypatch.setattr(users_module, 'hash_password', lambda password: calls.append(password) or real(password))

    # when
    res = admin.patch(f'{USERS}/{DANA}', json={'password': PASSWORD})

    # then
    assert res.status_code == 200
    assert calls == [PASSWORD]
    assert Users(store.db).authenticate(DANA, PASSWORD) is not None


def cookie_of(client: TestClient) -> str:
    return client.cookies.get(SESSION_COOKIE)


def with_cookie(client: TestClient, cookie: str) -> TestClient:
    client.cookies.clear()
    client.cookies.set(SESSION_COOKIE, cookie)
    return client


def test_a_second_pod_keeps_the_new_session_and_refuses_the_old_one_with_one_extra_read(store: Store, monkeypatch):
    # given
    Users(store.db).create(CAROL, 'Carol', 'viewer', PASSWORD)
    pod_a = session_of(store, CAROL)
    old = cookie_of(pod_a)
    pod_b = with_cookie(make_client(store), old)
    assert signed_in(pod_b) == CAROL
    reads, real = [], Users.get
    monkeypatch.setattr(Users, 'get', lambda self, email: reads.append(email) or real(self, email))

    # when
    assert pod_a.post('/api/me/password', json={'current': PASSWORD, 'new': NEW_PASSWORD}).status_code == 204
    fresh = signed_in(with_cookie(pod_b, cookie_of(pod_a)))
    refreshed = len(reads)
    stale = [signed_in(with_cookie(pod_b, old)) for _ in range(5)]

    # then
    assert fresh == CAROL, 'the new session stays signed in on a pod that still holds the old account in its cache'
    assert stale == [None] * 5
    assert len(reads) - refreshed <= 1, 'an old cookie cannot force a database read per request'


def test_a_password_reset_over_a_token_never_revives_the_cookie_sent_with_it(store: Store):
    # given
    with_password(store, ADMIN_EMAIL, PASSWORD)
    client = session_of(store, ADMIN_EMAIL)
    Users(store.db).set_password(ADMIN_EMAIL, THIRD)

    # when
    res = client.patch(f'{USERS}/{ADMIN_EMAIL}', json={'password': NEW_PASSWORD}, headers=AUTH)

    # then
    assert res.status_code == 200
    assert not [c for c in res.headers.get_list('set-cookie') if c.startswith(f'{SESSION_COOKIE}=')]
