import html
import json
import time

import pytest
from fastapi.testclient import TestClient
from pymongo.errors import PyMongoError

from skopax.app import create_app
from skopax.auth import SESSION_COOKIE
from skopax.messages import msg
from skopax.sso import SsoHttp
from skopax.store import Store
from skopax.users import NoPasswordAdmin, Users
from tests.conftest import AUTH
from tests.sso_kit import FakeIdp, idp_settings, log_in

ALICE = 'alice@example.com'
ROOT = 'root@example.com'
NOBODY = 'nobody@example.com'
PASSWORD = 'alice-long-password-1'
NEW_PASSWORD = 'alice-other-password-2'
SUBJECT = 'idp-subject-5a0e'
USERS = '/api/admin/users'
ROLE_MAP = json.dumps([{'value': 'ops-admins', 'role': 'admin'}, {'value': 'ops-operators', 'role': 'operator'}])


class Clock:
    def __init__(self):
        self.now = float(int(time.time()))

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def client_for(store: Store, clock: Clock | None = None, **over) -> TestClient:
    idp = FakeIdp()
    s = idp_settings(idp, sso_role_map=ROLE_MAP, **over)
    client = TestClient(create_app(s, store, sso_http=SsoHttp(fetch=idp.fetch, exchange=idp.exchange), clock=clock), base_url='https://testserver', follow_redirects=False)
    client.idp = idp
    return client


def admin_of(client: TestClient) -> TestClient:
    return TestClient(client.app, base_url='https://testserver', headers=AUTH, follow_redirects=False)


def fresh(client: TestClient) -> TestClient:
    other = TestClient(client.app, base_url='https://testserver', follow_redirects=False)
    other.idp = client.idp
    return other


def raw(store: Store, email: str) -> dict | None:
    return store.db['users'].find_one({'_id': email})


def people(store: Store, role: str = 'admin', root: bool = True) -> Users:
    users = Users(store.db)
    users.create(ALICE, 'Alice', role, password=PASSWORD)
    if root:
        users.create(ROOT, 'Root', 'admin', password=PASSWORD)
    return users


def sso_only(client: TestClient, email: str = ALICE, **extra):
    return admin_of(client).patch(f'{USERS}/{email}', json={'ssoOnly': True} | extra)


def sso(client: TestClient, **claims):
    return log_in(client, client.idp, **{'email': ALICE, 'email_verified': True, 'sub': SUBJECT, 'roles': []} | claims)


def password_login(client: TestClient, email: str = ALICE, password: str = PASSWORD):
    return client.post('/login', data={'email': email, 'password': password, 'next': '/'})


def has_session(res) -> bool:
    return any(c.startswith(f'{SESSION_COOKIE}=') and 'max-age=0' not in c.lower() for c in res.headers.get_list('set-cookie'))


def cookie_of(res) -> str:
    for c in res.headers.get_list('set-cookie'):
        if c.startswith(f'{SESSION_COOKIE}='):
            return c.split(';', 1)[0].split('=', 1)[1]
    raise AssertionError('no session cookie')


def me(client: TestClient, cookie: str) -> str | None:
    return TestClient(client.app, base_url='https://testserver').get('/api/me', headers={'Cookie': f'{SESSION_COOKIE}={cookie}'}).json()['user']


def access_lines(store: Store) -> list[str]:
    return [a['text'] for a in store.activity.find({'kind': 'access'})]


def token_from(client: TestClient, cookie: str, name: str) -> str:
    res = TestClient(client.app, base_url='https://testserver').post('/api/admin/tokens', json={'name': name, 'role': 'viewer'}, headers={'Cookie': f'{SESSION_COOKIE}={cookie}'})
    assert res.status_code == 201, res.text
    return res.json()['token']


def answers(client: TestClient, token: str) -> int:
    return TestClient(client.app, base_url='https://testserver').get('/api/notifications/count', headers={'Authorization': f'Bearer {token}'}).status_code


def test_make_sso_only_drops_the_password_ends_password_sessions_and_keeps_role_name_and_sso_tokens(store: Store):
    # given
    people(store)
    clock = Clock()
    serving, other_pod = client_for(store, clock), client_for(store, clock)
    admin_of(serving).patch(f'{USERS}/{ALICE}', json={'ssoLink': True})
    sso_cookie = cookie_of(sso(fresh(serving)))
    password_cookie = cookie_of(password_login(fresh(serving)))
    sso_token = token_from(serving, sso_cookie, 'from-sso')
    assert me(other_pod, password_cookie) == ALICE
    before = raw(store, ALICE)

    # when
    res = sso_only(serving)
    doc = raw(store, ALICE)
    at_once = me(serving, password_cookie)
    clock.advance(31)
    other_pod_after = me(other_pod, password_cookie)
    kept_sso = me(serving, sso_cookie)
    token_status = answers(serving, sso_token)
    by_password = password_login(fresh(serving))

    # then
    assert res.status_code == 200
    assert (res.json()['source'], res.json()['ssoLink'], 'pw' in res.json()) == ('sso', None, False)
    assert 'pw' not in doc and 'ssoLink' not in doc
    assert (doc['source'], doc['role'], doc['roleFrom'], doc['adminRole'], doc['name'], doc['createdAt'], doc['opened']) == \
        ('sso', 'admin', 'admin', 'admin', 'Alice', before['createdAt'], False)
    assert (at_once, other_pod_after) == (None, None), 'the password session ends here at once and on every pod after the user cache'
    assert kept_sso == ALICE, 'a session from the bound SSO link stays'
    assert token_status == 200, 'an API token made from an SSO session keeps working'
    assert by_password.status_code == 400 and not has_session(by_password)
    assert msg('access.user_sso_only', email=ALICE) in access_lines(store)
    assert not any(PASSWORD in line for line in access_lines(store))


def test_an_sso_only_person_made_from_a_password_account_fails_password_login_like_an_unknown_email(store: Store):
    # given
    people(store)
    client = client_for(store)
    sso_only(client)

    # when
    known = password_login(fresh(client))
    unknown = password_login(fresh(client), NOBODY)

    # then
    for res in (known, unknown):
        assert res.status_code == 400 and html.escape(msg('login.error')) in res.text and not has_session(res)
    assert known.text.replace(ALICE, 'X') == unknown.text.replace(NOBODY, 'X'), 'no hint that the account exists or why it failed'


def test_make_sso_only_is_refused_while_single_sign_on_is_not_active(store: Store):
    # given
    people(store)
    client = client_for(store, sso_on=False)
    before = raw(store, ALICE)

    # when
    res = sso_only(client)

    # then
    assert (res.status_code, res.json()['detail']) == (409, msg('access.sso_only_off'))
    assert raw(store, ALICE) == before


def test_make_sso_only_is_refused_outside_the_allowed_domains(store: Store):
    # given
    people(store)
    client = client_for(store, sso_allowed_domains=('other.example',))
    before = raw(store, ALICE)

    # when
    res = sso_only(client)

    # then
    assert (res.status_code, res.json()['detail']) == (422, msg('access.sso_only_domain'))
    assert raw(store, ALICE) == before


def test_the_last_active_admin_with_a_password_is_never_made_sso_only(store: Store):
    # given
    users = people(store, root=False)
    users.add_sso('sso-admin@example.com', 'Sso Admin', 'admin')
    users.create('off-admin@example.com', 'Off Admin', 'admin', password=PASSWORD)
    users.set_disabled('off-admin@example.com', True)
    client = client_for(store)
    before = raw(store, ALICE)

    # when
    res = sso_only(client)

    # then
    assert (res.status_code, res.json()['detail']) == (409, msg('access.sso_only_last_password_admin'))
    assert raw(store, ALICE) == before
    assert Users(store.db).has_password_admin()


def test_a_non_admin_is_made_sso_only_even_when_no_admin_has_a_password(store: Store):
    # given
    people(store, role='viewer', root=False)
    client = client_for(store)

    # when
    res = sso_only(client)

    # then
    assert res.status_code == 200 and raw(store, ALICE)['source'] == 'sso'


def test_make_sso_only_refuses_bad_bodies_and_sso_people(store: Store):
    # given
    users = people(store)
    users.add_sso('dana@example.com', 'Dana', 'viewer')
    client = client_for(store)
    before = raw(store, ALICE)

    # when
    off = admin_of(client).patch(f'{USERS}/{ALICE}', json={'ssoOnly': False})
    with_password = sso_only(client, password=NEW_PASSWORD)
    with_link = sso_only(client, ssoLink=True)
    loose = admin_of(client).patch(f'{USERS}/{ALICE}', json={'ssoOnly': 'yes'})
    already = sso_only(client, 'dana@example.com')

    # then
    for res in (off, with_password, with_link):
        assert (res.status_code, res.json()['detail']) == (422, msg('access.user_fields'))
    assert loose.status_code == 422
    assert (already.status_code, already.json()['detail']) == (409, msg('access.sso_only_local'))
    assert raw(store, ALICE) == before


def test_users_store_restores_the_account_when_the_change_leaves_no_password_admin(store: Store):
    # given
    users = people(store)
    users.make_sso_only(ROOT)
    before = raw(store, ALICE)

    # when
    with pytest.raises(NoPasswordAdmin):
        users.make_sso_only(ALICE)

    # then
    assert raw(store, ALICE) == before
    assert users.make_sso_only('nobody@example.com') is None


def test_an_old_password_session_stays_ended_after_a_password_is_set_again(store: Store):
    # given
    people(store)
    clock = Clock()
    client = client_for(store, clock)
    old = cookie_of(password_login(fresh(client)))
    sso_only(client)
    clock.advance(5)

    # when
    res = admin_of(client).patch(f'{USERS}/{ALICE}', json={'password': NEW_PASSWORD})
    revived = me(client, old)
    new = password_login(fresh(client), password=NEW_PASSWORD)

    # then
    assert res.status_code == 200 and raw(store, ALICE)['source'] == 'local'
    assert revived is None
    assert new.status_code == 303 and me(client, cookie_of(new)) == ALICE


def test_an_sso_session_ended_by_unlinking_stays_ended_after_make_sso_only(store: Store):
    # given
    people(store)
    clock = Clock()
    client = client_for(store, clock)
    admin_of(client).patch(f'{USERS}/{ALICE}', json={'ssoLink': True})
    old = cookie_of(sso(fresh(client)))
    admin_of(client).patch(f'{USERS}/{ALICE}', json={'ssoLink': False})
    assert me(client, old) is None
    clock.advance(5)

    # when
    res = sso_only(client)
    opened = raw(store, ALICE)['opened']
    revived = me(client, old)
    clock.advance(1)
    again = sso(fresh(client))

    # then
    assert res.status_code == 200 and opened is False
    assert revived is None
    assert again.status_code == 303 and me(client, cookie_of(again)) == ALICE


def test_an_unlinked_person_made_sso_only_opens_only_with_a_verified_email(store: Store):
    # given
    people(store)
    client = client_for(store)
    sso_only(client)

    # when
    unverified = sso(fresh(client), email_verified=None)
    verified = sso(fresh(client))

    # then
    assert not has_session(unverified)
    assert verified.status_code == 303 and has_session(verified)
    assert 'opened' not in raw(store, ALICE)


def test_a_converted_bound_account_still_opens_only_with_a_verified_email_and_keeps_its_sso_session(store: Store):
    # given
    people(store)
    client = client_for(store)
    admin_of(client).patch(f'{USERS}/{ALICE}', json={'ssoLink': True})
    kept = cookie_of(sso(fresh(client)))
    sso_only(client)

    # when
    unverified = sso(fresh(client), email_verified=None)
    still = me(client, kept)
    verified = sso(fresh(client))

    # then
    assert not has_session(unverified)
    assert still == ALICE
    assert verified.status_code == 303 and has_session(verified)
    assert 'opened' not in raw(store, ALICE)


def test_api_tokens_made_from_password_sessions_end_on_conversion_and_sso_made_ones_keep_working(store: Store):
    # given
    people(store)
    clock = Clock()
    client = client_for(store, clock)
    admin_of(client).patch(f'{USERS}/{ALICE}', json={'ssoLink': True})
    sso_token = token_from(client, cookie_of(sso(fresh(client))), 'from-sso')
    pw_token = token_from(client, cookie_of(password_login(fresh(client))), 'from-password')
    assert (answers(client, sso_token), answers(client, pw_token)) == (200, 200)
    clock.advance(5)

    # when
    sso_only(client)
    after = (answers(client, sso_token), answers(client, pw_token))
    revoked = admin_of(client).delete(f"/api/admin/tokens/{[t for t in admin_of(client).get('/api/admin/tokens').json() if t['name'] == 'from-sso'][0]['id']}")
    gone = TestClient(client.app, base_url='https://testserver').get('/api/notifications/count', headers={'Authorization': f'Bearer {pw_token}'})
    dead = TestClient(client.app, base_url='https://testserver').get('/api/notifications/count', headers={'Authorization': f'Bearer {sso_token}'})

    # then
    assert after == (200, 401)
    assert revoked.status_code == 204
    assert (gone.status_code, gone.json()) == (dead.status_code, dead.json()), 'a password-made token reads like a revoked one'


def test_disabling_or_demoting_the_last_password_admin_is_refused_after_a_conversion(store: Store):
    # given
    people(store)
    client = client_for(store)
    assert sso_only(client).status_code == 200
    before = raw(store, ROOT)

    # when
    disabled = admin_of(client).patch(f'{USERS}/{ROOT}', json={'disabled': True})
    demoted = admin_of(client).patch(f'{USERS}/{ROOT}', json={'role': 'operator'})
    renamed = admin_of(client).patch(f'{USERS}/{ROOT}', json={'name': 'Root Two'})

    # then
    for res in (disabled, demoted):
        assert (res.status_code, res.json()['detail']) == (409, msg('access.last_password_admin'))
    assert renamed.status_code == 200
    assert {k: v for k, v in raw(store, ROOT).items() if k not in ('name', 'nameFrom')} == {k: v for k, v in before.items() if k not in ('name', 'nameFrom')}


def test_users_store_restores_a_password_admin_disabled_in_a_race(store: Store):
    # given
    users = people(store)
    users.make_sso_only(ALICE)
    before = raw(store, ROOT)

    # when
    with pytest.raises(NoPasswordAdmin):
        users.guarded_update(ROOT, {'disabled': True})

    # then
    assert raw(store, ROOT) == before


def test_a_failing_check_after_the_write_puts_the_account_back_and_answers_503(store: Store, monkeypatch):
    # given
    people(store)
    client = client_for(store)
    before = raw(store, ALICE)

    def broken(self):
        raise PyMongoError('down')

    monkeypatch.setattr(Users, 'has_password_admin', broken)

    # when
    res = sso_only(client)

    # then
    assert (res.status_code, res.json()['detail']) == (503, msg('access.not_changed'))
    assert raw(store, ALICE) == before
    assert msg('access.user_sso_only', email=ALICE) not in access_lines(store)


def test_a_failing_restore_answers_500_and_leaves_an_audit_line(store: Store, monkeypatch, caplog):
    # given
    people(store)
    client = client_for(store)

    def broken(self):
        raise PyMongoError('down')

    def unrestored(self, key, before, touched, now):
        raise PyMongoError('down')

    monkeypatch.setattr(Users, 'has_password_admin', broken)
    monkeypatch.setattr(Users, 'restore', unrestored)

    # when
    res = sso_only(client)

    # then
    assert (res.status_code, res.json()['detail']) == (500, msg('access.sso_only_unsure'))
    assert raw(store, ALICE)['source'] == 'sso'
    assert msg('access.user_sso_only', email=ALICE) in access_lines(store)
    assert any(r.levelname == 'ERROR' and ALICE in r.getMessage() for r in caplog.records)


def test_a_lost_race_for_the_last_password_admin_is_put_back_and_refused(store: Store, monkeypatch):
    # given
    people(store, root=False)
    client = client_for(store)
    before = raw(store, ALICE)
    monkeypatch.setattr(Users, 'other_password_admin', lambda self, key: True)

    # when
    res = sso_only(client)

    # then
    assert (res.status_code, res.json()['detail']) == (409, msg('access.sso_only_last_password_admin'))
    assert raw(store, ALICE) == before


def test_a_password_set_while_the_account_changed_meanwhile_says_so(store: Store, monkeypatch):
    # given
    client = client_for(store)
    admin_of(client).post(USERS, json={'email': ALICE, 'name': 'Alice', 'role': 'viewer', 'sso': True})
    monkeypatch.setattr(Users, 'make_local', lambda self, *a, **k: None)

    # when
    res = admin_of(client).patch(f'{USERS}/{ALICE}', json={'password': PASSWORD})

    # then
    assert (res.status_code, res.json()['detail']) == (422, msg('access.changed_meanwhile'))


def test_a_password_is_set_again_on_an_sso_person_who_signed_in_and_their_role_becomes_admin_set(store: Store):
    # given
    Users(store.db).create(ROOT, 'Root', 'admin', password=PASSWORD)
    clock = Clock()
    client = client_for(store, clock)
    admin_of(client).post(USERS, json={'email': ALICE, 'name': 'Alice', 'role': 'viewer', 'sso': True})
    old_sso = cookie_of(sso(fresh(client), roles=['ops-operators']))
    assert raw(store, ALICE)['roleFrom'] == 'claim'

    # when
    res = admin_of(client).patch(f'{USERS}/{ALICE}', json={'password': PASSWORD})
    doc = raw(store, ALICE)
    role = admin_of(client).patch(f'{USERS}/{ALICE}', json={'role': 'viewer'})
    by_password = password_login(fresh(client))

    # then
    assert res.status_code == 200
    assert (doc['source'], doc['role'], doc['roleFrom'], doc['adminRole'], doc['ssoLink']['state'], 'opened' in doc) == ('local', 'operator', 'admin', 'operator', 'pending', False)
    assert me(client, old_sso) is None, 'the SSO session ends until the link binds again'
    assert role.status_code == 200 and raw(store, ALICE)['role'] == 'viewer'
    assert by_password.status_code == 303 and has_session(by_password)
    assert msg('access.user_password', email=ALICE) in access_lines(store)
    assert msg('access.user_role_admin_set', email=ALICE, role='operator') in access_lines(store)
