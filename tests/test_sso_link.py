import html
import json
import logging
import time

import pytest
from fastapi.testclient import TestClient

from skopax.app import create_app
from skopax.clock import stamp
from skopax.auth import SESSION_COOKIE
from skopax.messages import msg
from skopax.sessions import Sessions, session_secret
from skopax.sso import SsoHttp
from skopax.store import Store
from skopax.users import LINK_PENDING_S, Users, link_admits
from tests.conftest import AUTH, settings
from tests.plugin_kit import registry_with
from tests.plugins import resolver_plugin
from tests.sso_kit import FakeIdp, idp_settings, log_in

ALICE = 'alice@example.com'
BOB = 'bob@example.com'
DAVE = 'dave@example.com'
PASSWORD = 'alice-long-password-1'
SUBJECT = 'idp-subject-7f3a'
OTHER_SUBJECT = 'idp-subject-c91e'
ROLE_MAP = json.dumps([{'value': 'ops-admins', 'role': 'admin'}, {'value': 'ops-operators', 'role': 'operator'}, {'value': 'ops-viewers', 'role': 'viewer'}])


class Clock:
    def __init__(self):
        self.now = float(int(time.time()))

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture(autouse=True)
def answers():
    resolver_plugin.reset()
    yield resolver_plugin.ANSWERS
    resolver_plugin.reset()


def client_for(store: Store, clock: Clock | None = None, idp: FakeIdp | None = None, **over) -> TestClient:
    idp = idp or FakeIdp()
    s = idp_settings(idp, sso_role_map=ROLE_MAP, **over)
    client = TestClient(create_app(s, store, registry=registry_with(resolver_plugin), sso_http=SsoHttp(fetch=idp.fetch, exchange=idp.exchange), clock=clock),
                        base_url='https://testserver', follow_redirects=False)
    client.idp = idp
    return client


def admin_of(client: TestClient) -> TestClient:
    return TestClient(client.app, base_url='https://testserver', headers=AUTH, follow_redirects=False)


def raw(store: Store, email: str) -> dict | None:
    return store.db['users'].find_one({'_id': email})


def local_alice(store: Store, role: str = 'admin') -> Users:
    users = Users(store.db)
    users.create(ALICE, 'Alice', role, password=PASSWORD)
    return users


def link(client: TestClient, allow: bool, email: str = ALICE):
    return admin_of(client).patch(f'/api/admin/users/{email}', json={'ssoLink': allow})


def sso(client: TestClient, **claims):
    return log_in(client, client.idp, **{'email': ALICE, 'email_verified': True, 'sub': SUBJECT, 'roles': []} | claims)


def has_session(res) -> bool:
    return any(c.startswith(f'{SESSION_COOKIE}=') and 'max-age=0' not in c.lower() for c in res.headers.get_list('set-cookie'))


def refused(res) -> bool:
    return res.status_code == 401 and html.escape(msg('login.failed')) in res.text and not has_session(res)


def me(client: TestClient, cookie: str | None = None) -> dict:
    if cookie is None:
        return client.get('/api/me').json()
    return TestClient(client.app, base_url='https://testserver').get('/api/me', headers={'Cookie': f'{SESSION_COOKIE}={cookie}'}).json()


def cookie_of(res) -> str:
    for c in res.headers.get_list('set-cookie'):
        if c.startswith(f'{SESSION_COOKIE}='):
            return c.split(';', 1)[0].split('=', 1)[1]
    raise AssertionError('no session cookie')


def password_login(client: TestClient):
    return client.post('/login', data={'email': ALICE, 'password': PASSWORD, 'next': '/'})


def other_subject_warnings(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.name == 'skopax.signin' and r.levelno == logging.WARNING]


def test_without_a_link_sso_never_signs_in_to_a_local_account(store: Store):
    # given
    local_alice(store)
    client = client_for(store)
    before = raw(store, ALICE)

    # when
    res = sso(client, roles=['ops-admins'], name='Oscar Green')
    bob = sso(client_for(store), email=BOB, sub=BOB, name='Bob')

    # then
    assert refused(res)
    assert me(client)['user'] is None
    assert raw(store, ALICE) == before
    assert bob.status_code == 303 and has_session(bob)
    assert (raw(store, BOB)['source'], raw(store, BOB)['name']) == ('sso', 'Bob')


def test_a_link_needs_a_verified_email_claim_and_pins_the_subject(store: Store, caplog):
    # given
    local_alice(store)
    client = client_for(store)
    linked = link(client, True)
    pending = raw(store, ALICE)
    caplog.set_level(logging.WARNING)

    # when
    no_email = sso(client_for(store), email=None, email_verified=True, sub=ALICE)
    after_no_email = raw(store, ALICE)
    unverified = sso(client_for(store), email_verified=None)
    after_unverified = raw(store, ALICE)
    bind = sso(client, email_verified='true', name='Oscar Green', roles=['ops-viewers'])
    bound = raw(store, ALICE)
    me_bound = me(client)
    caplog.clear()
    other = sso(client_for(store), sub=OTHER_SUBJECT, email_verified=True)
    warned = other_subject_warnings(caplog)
    after_other = raw(store, ALICE)
    elsewhere = FakeIdp(issuer='https://other-idp.example.com')
    caplog.clear()
    other_issuer = sso(client_for(store, idp=elsewhere))
    warned_issuer = other_subject_warnings(caplog)
    after_other_issuer = raw(store, ALICE)
    again = sso(client_for(store))

    # then
    assert linked.status_code == 200 and linked.json()['ssoLink'] == {'state': 'pending', 'boundAt': None, 'allowedAt': pending['ssoLink']['at'],
                                                                     'expiresAt': stamp(pending['ssoLink']['atTs'] + LINK_PENDING_S)}
    assert isinstance(pending['ssoLink']['at'], str) and pending['ssoLink']['at']
    assert pending['ssoLink']['state'] == 'pending'
    assert refused(no_email) and after_no_email == pending
    assert refused(unverified) and after_unverified == pending
    assert bind.status_code == 303 and has_session(bind)
    assert (me_bound['user'], me_bound['userRole']) == (ALICE, 'admin')
    assert (bound['ssoLink']['state'], bound['ssoLink']['iss'], bound['ssoLink']['sub']) == ('bound', 'https://idp.example.com', SUBJECT)
    assert isinstance(bound['ssoLink']['boundAtTs'], int) and bound['ssoLink']['boundAt']
    assert (bound['name'], bound['role'], bound['source'], bound['pw']) == ('Alice', 'admin', 'local', pending['pw'])
    assert refused(other) and after_other == bound
    assert len(warned) == 1 and ALICE in warned[0]
    assert refused(other_issuer) and after_other_issuer == bound
    assert len(warned_issuer) == 1 and ALICE in warned_issuer[0]
    assert again.status_code == 303 and has_session(again)


def test_a_pending_link_expires_after_seven_days(store: Store):
    # given
    local_alice(store)
    late_clock, on_time_clock = Clock(), Clock()
    late, on_time = client_for(store, late_clock), client_for(store, on_time_clock)
    link(late, True)
    pending = raw(store, ALICE)

    # when
    late_clock.advance(LINK_PENDING_S + 1)
    expired = sso(late)
    after_expired = raw(store, ALICE)
    on_time_clock.now = pending['ssoLink']['atTs'] + LINK_PENDING_S
    within = sso(on_time)

    # then
    assert refused(expired)
    assert after_expired == pending
    assert within.status_code == 303 and has_session(within)
    assert raw(store, ALICE)['ssoLink']['state'] == 'bound'


def test_unlinking_ends_sso_sessions_of_that_account_within_thirty_seconds(store: Store):
    # given
    local_alice(store)
    clock = Clock()
    serving, other_pod = client_for(store, clock), client_for(store, clock)
    link(serving, True)
    sso_cookie = cookie_of(sso(serving))
    password_cookie = cookie_of(password_login(serving))
    assert me(other_pod, sso_cookie)['user'] == ALICE
    assert me(serving, sso_cookie)['user'] == ALICE

    # when
    unlinked = link(serving, False)
    at_once = me(serving, sso_cookie)['user']
    clock.advance(29)
    cached_window = me(other_pod, sso_cookie)['user']
    clock.advance(2)
    after = {name: me(c, cookie)['user'] for name, (c, cookie) in
             {'sso here': (serving, sso_cookie), 'sso other pod': (other_pod, sso_cookie), 'password here': (serving, password_cookie),
              'password other pod': (other_pod, password_cookie)}.items()}

    # then
    assert unlinked.status_code == 200 and unlinked.json()['ssoLink'] is None
    assert 'ssoLink' not in raw(store, ALICE)
    assert at_once is None
    assert cached_window == ALICE
    assert after == {'sso here': None, 'sso other pod': None, 'password here': ALICE, 'password other pod': ALICE}


def test_a_linked_account_keeps_its_role_and_password(store: Store, answers):
    # given
    local_alice(store)
    client = client_for(store)
    answers[(ALICE, 'page')] = 'viewer'
    link(client, True)
    before = raw(store, ALICE)

    # when
    res = sso(client, roles=['ops-viewers'])
    stored = raw(store, ALICE)
    signed = me(client)
    by_password = password_login(client_for(store))

    # then
    assert res.status_code == 303 and has_session(res)
    assert (signed['user'], signed['userRole']) == (ALICE, 'admin')
    assert not any(user.get('email') == ALICE for user, _ in resolver_plugin.SEEN)
    assert (stored['role'], stored.get('roleFrom'), stored.get('adminRole'), stored['pw'], stored['name']) == \
        ('admin', before.get('roleFrom'), before.get('adminRole'), before['pw'], 'Alice')
    assert by_password.status_code == 303 and has_session(by_password)


def test_only_local_accounts_can_be_linked(store: Store):
    # given
    users = Users(store.db)
    users.create(BOB, 'Bob', 'viewer', source='sso')
    local_alice(store)
    client = client_for(store)
    before = raw(store, BOB)

    # when
    answers = {allow: link(client, allow, BOB) for allow in (True, False)}
    mixed = admin_of(client).patch(f'/api/admin/users/{BOB}', json={'ssoLink': True, 'disabled': True})
    control = link(client, True)

    # then
    assert {allow: (res.status_code, res.json()['detail']) for allow, res in answers.items()} == \
        {allow: (422, msg('access.sso_link_local_only')) for allow in (True, False)}
    assert (mixed.status_code, mixed.json()['detail']) == (422, msg('access.sso_link_local_only'))
    assert raw(store, BOB) == before
    assert control.status_code == 200 and raw(store, ALICE)['ssoLink']['state'] == 'pending'


def test_link_changes_write_activity_and_never_expose_the_subject(store: Store):
    # given
    local_alice(store)
    users = Users(store.db)
    users.create(DAVE, 'Dave', 'operator')
    client = client_for(store, public_read=False)
    public = client_for(store)
    sealer = Sessions(session_secret(store.db, settings()), 3600, 7200)
    dave_cookie = sealer.issue(DAVE, 'operator', 'local')
    operator = {'Cookie': f'{SESSION_COOKIE}={dave_cookie}'}
    store.add_activity('pass', 'server', 'prom ok')

    # when
    linked = link(client, True)
    sso(client)
    listed = admin_of(client).get('/api/admin/users')
    unlinked = link(client, False)
    closed_anonymous = TestClient(client.app, base_url='https://testserver').get('/api/activity', params={'limit': 50})
    seen = {who: TestClient(app.app, base_url='https://testserver').get('/api/activity', params={'limit': 50}, headers=headers).json()
            for who, app, headers in (('anonymous', public, {}), ('operator', client, operator), ('admin', client, AUTH))}

    # then
    lines = {msg('access.sso_linked', email=ALICE, days=LINK_PENDING_S // 86400), msg('access.sso_bound', email=ALICE), msg('access.sso_unlinked', email=ALICE)}
    admin_rows = {(r['kind'], r['text']) for r in seen['admin']}
    assert {('access', line) for line in lines} <= admin_rows
    assert me(client, dave_cookie)['userRole'] == 'operator'
    assert closed_anonymous.status_code == 401
    for who in ('anonymous', 'operator'):
        assert 'prom ok' in [r['text'] for r in seen[who]], who
        assert not [r for r in seen[who] if r['text'] in lines or ALICE in r['text']], who
    row = next(r for r in listed.json() if r['email'] == ALICE)
    assert row['ssoLink']['state'] == 'bound' and set(row['ssoLink']) == {'state', 'boundAt', 'allowedAt', 'expiresAt'} and row['ssoLink']['allowedAt'] is None
    assert row['ssoLink']['expiresAt'] is None
    assert all(set(r) >= {'ssoLink'} for r in listed.json())
    for body in (listed.text, linked.text, unlinked.text):
        assert '"sub"' not in body and '"iss"' not in body and SUBJECT not in body and 'idp.example.com' not in body


def test_relinking_to_another_subject_never_revives_an_old_session(store: Store):
    # given
    local_alice(store)
    clock = Clock()
    client = client_for(store, clock)
    link(client, True)
    first = cookie_of(sso(client, sub=SUBJECT))
    assert me(client, first)['user'] == ALICE

    # when
    clock.advance(5)
    link(client, False)
    link(client, True)
    clock.advance(5)
    second = sso(client, sub=OTHER_SUBJECT)
    clock.advance(31)
    old, new = me(client, first)['user'], me(client, cookie_of(second))['user']

    # then
    assert second.status_code == 303 and raw(store, ALICE)['ssoLink']['sub'] == OTHER_SUBJECT
    assert old is None
    assert new == ALICE
    assert not link_admits({'ssoLink': {'state': 'pending', 'boundAtTs': 0}}, 10)
    assert link_admits({'ssoLink': {'state': 'bound', 'boundAtTs': 0}}, 10)


def test_a_user_disabled_mid_login_is_not_written(store: Store, monkeypatch):
    # given
    users = Users(store.db)
    users.create('carol@example.com', 'Carol', 'viewer', source='sso')
    users.set_disabled('carol@example.com', True)
    users.create(BOB, 'Bob', 'viewer', source='sso', name_from='idp')
    before = raw(store, 'carol@example.com')
    real = Users.get
    raced = []

    def enabled_view(self, email):
        found = real(self, email)
        if email == 'carol@example.com' and not raced:
            raced.append(email)
            return found | {'disabled': False}
        return found

    monkeypatch.setattr(Users, 'get', enabled_view)

    # when
    res = sso(client_for(store), email='carol@example.com', sub='carol@example.com', name='Oscar Green', roles=['ops-admins'])
    bob = sso(client_for(store), email=BOB, sub=BOB, name='Bob Stone')

    # then
    assert raced == ['carol@example.com']
    assert refused(res)
    assert raw(store, 'carol@example.com') == before
    assert bob.status_code == 303 and raw(store, BOB)['name'] == 'Bob Stone'


CHANGES = {
    'unlinked after binding': (True, {'$unset': {'ssoLink': ''}}),
    'disabled after binding': (True, {'$set': {'disabled': True}}),
    'unlinked while pending': (False, {'$unset': {'ssoLink': ''}}),
    'disabled while pending': (False, {'$set': {'disabled': True}}),
    'bound by another subject while pending': (False, None),
}


@pytest.mark.parametrize('change', list(CHANGES))
def test_a_link_changed_mid_login_is_not_used(store: Store, monkeypatch, change: str):
    # given
    local_alice(store)
    client = client_for(store)
    link(client, True)
    bound_first, update = CHANGES[change]
    if bound_first:
        sso(client)
    stale = Users(store.db).get(ALICE)
    if update is None:
        sso(client_for(store), sub=OTHER_SUBJECT)
    else:
        store.db['users'].update_one({'_id': ALICE}, update)
    before = raw(store, ALICE)
    real = Users.get
    raced = []

    def stale_view(self, email):
        if email == ALICE and not raced:
            raced.append(email)
            return dict(stale)
        return real(self, email)

    monkeypatch.setattr(Users, 'get', stale_view)

    # when
    res = sso(client_for(store))
    bob = sso(client_for(store), email=BOB, sub=BOB)

    # then
    assert raced == [ALICE]
    assert refused(res)
    assert raw(store, ALICE) == before
    assert bob.status_code == 303 and has_session(bob)


def test_an_expired_pending_link_shows_as_expired(store: Store):
    # given
    local_alice(store)
    clock = Clock()
    client = client_for(store, clock)
    link(client, True)
    at = raw(store, ALICE)['ssoLink']['atTs']
    stored_at = raw(store, ALICE)['ssoLink']['at']

    def row() -> dict:
        return next(r for r in admin_of(client).get('/api/admin/users').json() if r['email'] == ALICE)['ssoLink']

    # when
    clock.now = at + LINK_PENDING_S
    on_the_edge = row()
    clock.now = at + LINK_PENDING_S + 1
    listed = row()
    patched = admin_of(client).patch(f'/api/admin/users/{ALICE}', json={'disabled': False}).json()['ssoLink']
    refreshed = link(client, True).json()['ssoLink']

    # then
    allowed, expires = stored_at, stamp(at + LINK_PENDING_S)
    assert on_the_edge == {'state': 'pending', 'boundAt': None, 'allowedAt': allowed, 'expiresAt': expires}
    assert listed == {'state': 'expired', 'boundAt': None, 'allowedAt': allowed, 'expiresAt': expires}
    assert patched == {'state': 'expired', 'boundAt': None, 'allowedAt': allowed, 'expiresAt': expires}
    again = raw(store, ALICE)['ssoLink']
    assert refreshed == {'state': 'pending', 'boundAt': None, 'allowedAt': again['at'], 'expiresAt': stamp(again['atTs'] + LINK_PENDING_S)}
    assert refreshed['allowedAt'] != allowed
    assert raw(store, ALICE)['ssoLink']['state'] == 'pending'


def test_a_user_disabled_before_the_role_write_is_not_written(store: Store, monkeypatch):
    # given
    users = Users(store.db)
    users.create('carol@example.com', 'Carol', 'viewer', source='sso')
    real = Users.upsert_sso
    disabled = {}

    def then_disabled(self, email, name, default_role, **kw):
        found = real(self, email, name, default_role, **kw)
        if email == 'carol@example.com' and not disabled:
            store.db['users'].update_one({'_id': email}, {'$set': {'disabled': True}})
            disabled['doc'] = raw(store, email)
        return found

    monkeypatch.setattr(Users, 'upsert_sso', then_disabled)

    # when
    res = sso(client_for(store), email='carol@example.com', sub='carol@example.com', roles=['ops-admins'])
    bob = sso(client_for(store), email=BOB, sub=BOB, roles=['ops-admins'])

    # then
    assert disabled['doc']['disabled'] is True
    assert refused(res)
    assert raw(store, 'carol@example.com') == disabled['doc']
    assert bob.status_code == 303 and (raw(store, BOB)['role'], raw(store, BOB)['roleFrom']) == ('admin', 'claim')
