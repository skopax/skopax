import dataclasses
import html
import json
import re
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import skopax
from skopax.app import create_app
from skopax.auth import SESSION_COOKIE
from skopax.clock import stamp
from skopax.config import ConfigurationError, SSO_NEW_ACCOUNTS, load_settings
from skopax.messages import msg
from skopax.signin import Refused, SignIn
from skopax.roles import Resolvers
from skopax.sso import SsoHttp
from skopax.store import Store
from skopax.tunables import registry
from skopax.users import LINK_PENDING_S, PASSWORD_MAX, PASSWORD_MIN, UserExists, Users, check_password
from tests.conftest import AUTH, settings
from tests.sso_kit import FakeIdp, idp_settings, log_in

DANA = 'dana@example.com'
ERIN = 'erin@example.com'
PASSWORD = 'dana-long-password-1'
SUBJECT = 'idp-subject-d4n4'
USERS = '/api/admin/users'
ROLE_MAP = json.dumps([{'value': 'ops-admins', 'role': 'admin'}])


class Clock:
    def __init__(self):
        self.now = float(int(time.time()))

    def __call__(self) -> float:
        return self.now


def sso_client(store: Store, clock: Clock | None = None, idp: FakeIdp | None = None, **over) -> TestClient:
    idp = idp or FakeIdp()
    s = idp_settings(idp, sso_role_map=ROLE_MAP, **over)
    client = TestClient(create_app(s, store, sso_http=SsoHttp(fetch=idp.fetch, exchange=idp.exchange), clock=clock), base_url='https://testserver', follow_redirects=False)
    client.idp = idp
    return client


def admin_of(client: TestClient) -> TestClient:
    return TestClient(client.app, base_url='https://testserver', headers=AUTH, follow_redirects=False)


def plain_admin(store: Store) -> TestClient:
    return TestClient(create_app(settings(), store), base_url='https://testserver', headers=AUTH, follow_redirects=False)


def raw(store: Store, email: str) -> dict | None:
    return store.db['users'].find_one({'_id': email})


def sso(client: TestClient, email: str = DANA, **claims):
    return log_in(client, client.idp, **{'email': email, 'email_verified': True, 'sub': SUBJECT, 'roles': []} | claims)


def has_session(res) -> bool:
    return any(c.startswith(f'{SESSION_COOKIE}=') and 'max-age=0' not in c.lower() for c in res.headers.get_list('set-cookie'))


def refused(res) -> bool:
    return res.status_code == 401 and html.escape(msg('login.failed')) in res.text and not has_session(res)


def access_lines(store: Store) -> list[str]:
    return [a['text'] for a in store.activity.find({'kind': 'access'})]


def no_pydantic(res) -> bool:
    body = res.json()
    return isinstance(body.get('detail'), str) and not re.search(r"'loc'|\"loc\"|'type'|\"type\"|Field required|Input should", res.text)


def test_an_sso_only_user_is_added_as_an_sso_account_with_the_admin_set_role(store: Store):
    # given
    admin = admin_of(sso_client(store))

    # when
    res = admin.post(USERS, json={'email': ' Dana@Example.com ', 'name': '', 'role': 'operator', 'sso': True})
    listed = [u for u in admin.get(USERS).json() if u['email'] == DANA]

    # then
    assert res.status_code == 201
    doc = raw(store, DANA)
    assert (doc['source'], doc['role'], doc['roleFrom'], doc['adminRole'], doc['name'], doc['lastLogin']) == ('sso', 'operator', 'admin', 'operator', 'dana', None)
    assert 'pw' not in doc and 'ssoLink' not in doc
    assert (res.json()['source'], res.json()['lastLogin'], res.json()['ssoLink']) == ('sso', None, None)
    assert len(listed) == 1 and (listed[0]['source'], listed[0]['lastLogin']) == ('sso', None)
    assert msg('access.user_added_sso', email=DANA, role='operator') in access_lines(store)


def test_no_password_and_no_sso_is_refused_with_a_field_message(store: Store):
    # given
    admin = admin_of(sso_client(store))

    # when
    missing = admin.post(USERS, json={'email': DANA, 'name': 'Dana', 'role': 'viewer'})
    empty = admin.post(USERS, json={'email': DANA, 'name': 'Dana', 'role': 'viewer', 'password': '', 'sso': False})

    # then
    for res in (missing, empty):
        assert (res.status_code, res.json()) == (422, {'detail': msg('access.password_or_sso'), 'field': 'password'})
    assert msg('access.password_or_sso') == 'Give a password, or let the account sign in with SSO.'
    assert raw(store, DANA) is None


def test_sso_while_single_sign_on_is_not_active_is_refused(store: Store):
    # given
    off = admin_of(sso_client(store, sso_on=False))
    unset = plain_admin(store)

    # when
    seen = [off.post(USERS, json={'email': DANA, 'name': 'Dana', 'role': 'viewer', 'sso': True}),
            unset.post(USERS, json={'email': DANA, 'name': 'Dana', 'role': 'viewer', 'sso': True, 'password': PASSWORD})]

    # then
    assert [(r.status_code, r.json()) for r in seen] == [(422, {'detail': msg('access.sso_off'), 'field': 'sso'})] * 2
    assert raw(store, DANA) is None


def test_sso_with_a_password_makes_a_local_account_with_a_seven_day_link_in_the_same_write(store: Store, monkeypatch):
    # given
    clock = Clock()
    admin = admin_of(sso_client(store, clock))
    monkeypatch.setattr(Users, 'set_sso_link', lambda *a, **kw: pytest.fail('the link is written with the account, not after it'))

    # when
    res = admin.post(USERS, json={'email': DANA, 'name': 'Dana', 'role': 'viewer', 'sso': True, 'password': PASSWORD})

    # then
    doc = raw(store, DANA)
    assert res.status_code == 201
    assert doc['source'] == 'local' and check_password(PASSWORD, doc['pw'])
    assert (doc['ssoLink']['state'], doc['ssoLink']['atTs'], doc['ssoLink']['by']) == ('pending', clock.now, 'token:bootstrap')
    assert res.json()['ssoLink'] == {'state': 'pending', 'boundAt': None, 'allowedAt': stamp(clock.now), 'expiresAt': stamp(clock.now + LINK_PENDING_S)}
    assert msg('access.sso_linked', email=DANA, days=7) in access_lines(store)


def test_without_sso_a_password_account_is_created_as_before(store: Store):
    # given
    admin = admin_of(sso_client(store))

    # when
    res = admin.post(USERS, json={'email': DANA, 'name': 'Dana', 'role': 'admin', 'password': PASSWORD, 'sso': False})

    # then
    doc = raw(store, DANA)
    assert res.status_code == 201 and doc['source'] == 'local' and 'ssoLink' not in doc and check_password(PASSWORD, doc['pw'])


def test_empty_and_whitespace_fields_give_field_messages(store: Store):
    # given
    admin = admin_of(sso_client(store))
    user = lambda **over: {'email': DANA, 'name': 'Dana', 'role': 'viewer', 'password': PASSWORD} | over
    length = msg('user.password_length', min=PASSWORD_MIN, max=PASSWORD_MAX)

    # when
    seen = {
        'empty email': admin.post(USERS, json=user(email='')),
        'blank email': admin.post(USERS, json=user(email='   ')),
        'blank name': admin.post(USERS, json=user(name='   ')),
        'long name': admin.post(USERS, json=user(name='n' * 121)),
        'empty role': admin.post(USERS, json=user(role='')),
        'short password': admin.post(USERS, json=user(password='short')),
        'short password with sso': admin.post(USERS, json=user(password='short', sso=True)),
        'long password with sso': admin.post(USERS, json=user(password='p' * (PASSWORD_MAX + 1), sso=True)),
    }

    # then
    assert {k: (r.status_code, r.json()) for k, r in seen.items()} == {
        'empty email': (422, {'detail': msg('user.email_invalid'), 'field': 'email'}),
        'blank email': (422, {'detail': msg('user.email_invalid'), 'field': 'email'}),
        'blank name': (422, {'detail': msg('user.name_length', max=120), 'field': 'name'}),
        'long name': (422, {'detail': msg('user.name_length', max=120), 'field': 'name'}),
        'empty role': (422, {'detail': msg('user.role_invalid'), 'field': 'role'}),
        'short password': (422, {'detail': length, 'field': 'password'}),
        'short password with sso': (422, {'detail': length, 'field': 'password'}),
        'long password with sso': (422, {'detail': length, 'field': 'password'}),
    }
    assert raw(store, DANA) is None


def test_malformed_user_bodies_never_answer_with_pydantic_output(store: Store):
    # given
    admin = admin_of(sso_client(store))

    # when
    seen = {
        'no email': admin.post(USERS, json={'name': 'Dana', 'role': 'viewer', 'password': PASSWORD}),
        'role number': admin.post(USERS, json={'email': DANA, 'name': 'Dana', 'role': 3, 'password': PASSWORD}),
        'sso text': admin.post(USERS, json={'email': DANA, 'name': 'Dana', 'role': 'viewer', 'sso': 'yes'}),
        'password number': admin.post(USERS, json={'email': DANA, 'name': 'Dana', 'role': 'viewer', 'password': 123456789012}),
        'extra field': admin.post(USERS, json={'email': DANA, 'name': 'Dana', 'role': 'viewer', 'password': PASSWORD, 'admin': True}),
        'not an object': admin.post(USERS, json=['dana']),
        'patch password number': admin.patch(f'{USERS}/{DANA}', json={'password': 5}),
    }

    # then
    assert {k: (r.status_code, r.json().get('field')) for k, r in seen.items()} == {
        'no email': (422, 'email'), 'role number': (422, 'role'), 'sso text': (422, 'sso'), 'password number': (422, 'password'),
        'extra field': (422, None), 'not an object': (422, None), 'patch password number': (422, 'password')}
    assert all(no_pydantic(r) for r in seen.values()), {k: r.text for k, r in seen.items()}
    assert seen['no email'].json()['detail'] == msg('user.email_invalid')
    assert seen['extra field'].json()['detail'] == msg('access.user_fields')
    assert raw(store, DANA) is None


def test_an_sso_only_account_never_signs_in_with_a_password_and_reads_like_a_wrong_password(store: Store):
    # given
    client = sso_client(store)
    admin_of(client).post(USERS, json={'email': DANA, 'name': 'Dana', 'role': 'viewer', 'sso': True})

    # when
    known = client.post('/login', data={'email': DANA, 'password': PASSWORD, 'next': '/'})
    empty = client.post('/login', data={'email': DANA, 'password': '', 'next': '/'})
    unknown = client.post('/login', data={'email': 'nobody@example.com', 'password': PASSWORD, 'next': '/'})

    # then
    for res in (known, empty, unknown):
        assert res.status_code == 400 and html.escape(msg('login.error')) in res.text and not has_session(res)
    assert known.text.replace(DANA, 'X') == unknown.text.replace('nobody@example.com', 'X'), 'no hint that the account exists'


def test_a_pre_added_sso_account_signs_in_and_keeps_its_admin_set_role_unless_a_claim_gives_one(store: Store):
    # given
    client = sso_client(store)
    admin_of(client).post(USERS, json={'email': DANA, 'name': '', 'role': 'operator', 'sso': True})

    # when
    first = sso(client, name='Dana Doe')
    after_first = raw(store, DANA)
    role_first = client.get('/api/me').json()['userRole']
    claimed = sso(sso_client(store), roles=['ops-admins'])
    after_claim = raw(store, DANA)

    # then
    assert first.status_code == 303 and has_session(first)
    assert (role_first, after_first['role'], after_first['roleFrom'], after_first['source'], after_first['name']) == ('operator', 'operator', 'admin', 'sso', 'Dana Doe')
    assert after_first['lastLogin'] is not None
    assert claimed.status_code == 303 and (after_claim['role'], after_claim['roleFrom'], after_claim['adminRole']) == ('admin', 'claim', 'operator')


def test_setting_a_password_on_a_never_signed_in_sso_account_makes_it_local_with_a_seven_day_link(store: Store):
    # given
    clock = Clock()
    client = sso_client(store, clock)
    admin = admin_of(client)
    admin.post(USERS, json={'email': DANA, 'name': 'Dana', 'role': 'operator', 'sso': True})

    # when
    res = admin.patch(f'{USERS}/{DANA}', json={'password': PASSWORD})
    doc = raw(store, DANA)
    local = Users(store.db).authenticate(DANA, PASSWORD)
    signed = sso(client)
    bound = raw(store, DANA)

    # then
    assert res.status_code == 200
    assert (doc['source'], doc['role'], doc['roleFrom'], doc['ssoLink']['state'], doc['ssoLink']['atTs']) == ('local', 'operator', 'admin', 'pending', clock.now)
    assert res.json()['ssoLink']['expiresAt'] == stamp(clock.now + LINK_PENDING_S)
    assert local is not None and local['email'] == DANA
    assert signed.status_code == 303 and (bound['ssoLink']['state'], bound['ssoLink']['iss'], bound['ssoLink']['sub']) == ('bound', client.idp.issuer, SUBJECT)
    assert msg('access.user_password', email=DANA) in access_lines(store)


def test_a_password_on_an_sso_account_that_signed_in_makes_it_local_too(store: Store):
    # given
    client = sso_client(store)
    admin = admin_of(client)
    admin.post(USERS, json={'email': DANA, 'name': 'Dana', 'role': 'operator', 'sso': True})
    sso(client)

    # when
    res = admin.patch(f'{USERS}/{DANA}', json={'password': PASSWORD})

    # then
    assert res.status_code == 200
    assert (raw(store, DANA)['source'], raw(store, DANA)['ssoLink']['state']) == ('local', 'pending')
    assert Users(store.db).authenticate(DANA, PASSWORD) is not None


def test_an_unverified_email_never_binds_the_link_of_a_new_account(store: Store):
    # given
    client = sso_client(store)
    admin_of(client).post(USERS, json={'email': DANA, 'name': 'Dana', 'role': 'viewer', 'sso': True, 'password': PASSWORD})
    pending = raw(store, DANA)

    # when
    unverified = sso(sso_client(store), email_verified=False)
    missing = sso(sso_client(store), email_verified=None)
    other_email = sso(sso_client(store), email=ERIN)
    after = raw(store, DANA)
    verified = sso(client)

    # then
    assert refused(unverified) and refused(missing)
    assert other_email.status_code == 303, 'another email gets its own SSO account, never this one'
    assert after == pending
    assert verified.status_code == 303 and raw(store, DANA)['ssoLink']['state'] == 'bound'


def test_added_only_refuses_a_new_email_without_creating_an_account(store: Store):
    # given
    client = sso_client(store, sso_new_accounts='added-only')
    admin = admin_of(client)
    admin.post(USERS, json={'email': DANA, 'name': 'Dana', 'role': 'operator', 'sso': True})
    admin.post(USERS, json={'email': ERIN, 'name': 'Erin', 'role': 'viewer', 'sso': True, 'password': PASSWORD})
    lines = len(access_lines(store))

    # when
    stranger = sso(sso_client(store, sso_new_accounts='added-only'), email='zed@example.com', sub='zed')
    after_stranger = access_lines(store)
    added = sso(sso_client(store, sso_new_accounts='added-only'))
    linked = sso(sso_client(store, sso_new_accounts='added-only'), email=ERIN, sub='erin-sub')

    # then
    assert refused(stranger) and raw(store, 'zed@example.com') is None
    assert after_stranger[lines:] == [msg('access.sso_not_added', email='zed@example.com')]
    assert added.status_code == 303 and has_session(added)
    assert linked.status_code == 303 and raw(store, ERIN)['ssoLink']['state'] == 'bound'


def test_allowed_domains_refuse_other_domains_before_any_account_is_written(store: Store):
    # given
    domains = ('Example.COM',)
    client = sso_client(store, sso_allowed_domains=domains)
    admin = admin_of(client)
    admin.post(USERS, json={'email': ERIN, 'name': 'Erin', 'role': 'viewer', 'sso': True, 'password': PASSWORD})
    Users(store.db).upsert_sso('old@example.org', 'Old', 'viewer')
    Users(store.db).create('lou@example.org', 'Lou', 'viewer', PASSWORD, link_by='admin@example.com')
    before_old, before_lou = raw(store, 'old@example.org'), raw(store, 'lou@example.org')

    # when
    outside = sso(sso_client(store, sso_allowed_domains=domains), email='zed@example.org', sub='zed')
    sub_domain = sso(sso_client(store, sso_allowed_domains=domains), email='zed@a.example.com', sub='zed2')
    old = sso(sso_client(store, sso_allowed_domains=domains), email='old@example.org', sub='old')
    lou = sso(sso_client(store, sso_allowed_domains=domains), email='lou@example.org', sub='lou')
    inside = sso(sso_client(store, sso_allowed_domains=domains), email='new@example.com', sub='new')
    erin = sso(sso_client(store, sso_allowed_domains=domains), email=ERIN, sub='erin')

    # then
    assert refused(outside) and raw(store, 'zed@example.org') is None
    assert refused(sub_domain) and raw(store, 'zed@a.example.com') is None
    assert refused(old) and raw(store, 'old@example.org') == before_old
    assert refused(lou) and raw(store, 'lou@example.org') == before_lou
    assert inside.status_code == 303 and raw(store, 'new@example.com')['source'] == 'sso'
    assert erin.status_code == 303 and raw(store, ERIN)['ssoLink']['state'] == 'bound'
    assert not any('zed@' in line for line in access_lines(store)), 'a domain refusal writes no activity'


def test_the_mcp_sign_in_follows_both_rules_without_creating_an_account(store: Store):
    # given
    users = Users(store.db)
    users.add_sso(DANA, 'Dana', 'operator')
    claims = lambda email: {'email': email, 'email_verified': True, 'sub': email, 'iss': 'https://idp.example.com'}
    signin = lambda **over: SignIn(users, dataclasses.replace(settings(), **over), Resolvers(lambda: ()))

    # when
    added_only = signin(sso_new_accounts='added-only')
    outside = signin(sso_allowed_domains=('example.com',))
    known = added_only.mcp(claims(DANA), 'https://idp.example.com')

    # then
    for refuser, email in ((added_only, 'zed@example.com'), (outside, 'zed@example.org')):
        with pytest.raises(Refused) as e:
            refuser.mcp(claims(email), 'https://idp.example.com')
        assert e.value.status == 401 and raw(store, email) is None
    assert (known.email, known.role) == (DANA, 'operator')


def test_the_two_settings_are_validated_wherever_they_are_set(store: Store):
    # given
    client = sso_client(store)
    admin = admin_of(client)
    put = lambda key, value: admin.put('/api/admin/settings', json={'key': key, 'value': value})

    # when
    bad_domain = put('sso.allowed-domains', 'example.com, not a domain')
    wildcard = put('sso.allowed-domains', '*.example.com')
    bad_choice = put('sso.new-accounts', 'everyone')
    card = admin.put('/api/admin/sso', json={'values': {'allowed-domains': 'exa mple.com'}})
    good = put('sso.allowed-domains', 'example.com, Corp.Example.org')
    choice = put('sso.new-accounts', 'added-only')
    view = admin.get('/api/admin/sso').json()['values']

    # then
    assert (bad_domain.status_code, bad_domain.json()['detail']) == (422, msg('sso.domain_invalid', entry='not a domain'))
    assert (wildcard.status_code, wildcard.json()['detail']) == (422, msg('sso.domain_invalid', entry='*.example.com'))
    assert bad_choice.status_code == 422
    assert (card.status_code, card.json()) == (422, {'detail': msg('sso.domain_invalid', entry='exa mple.com'), 'field': 'allowed-domains'})
    assert good.status_code == 200 and choice.status_code == 200
    assert (view['allowed-domains']['value'], view['new-accounts']['value']) == ('example.com, Corp.Example.org', 'added-only')


def test_the_settings_load_from_the_environment_and_refuse_bad_values():
    # when
    loaded = load_settings(env={'SSO_NEW_ACCOUNTS': 'added-only', 'SSO_ALLOWED_DOMAINS': 'example.com, corp.example.org'})
    default = load_settings(env={})

    # then
    assert (loaded.sso_new_accounts, loaded.sso_allowed_domains) == ('added-only', ('example.com', 'corp.example.org'))
    assert (default.sso_new_accounts, default.sso_allowed_domains) == ('anyone', ())
    with pytest.raises(ConfigurationError):
        load_settings(env={'SSO_NEW_ACCOUNTS': 'everyone'})
    with pytest.raises(ConfigurationError):
        load_settings(env={'SSO_ALLOWED_DOMAINS': 'example.com, @example.org'})
    known = {t.key: t for t in registry()}
    assert known['sso.new-accounts'].choices == SSO_NEW_ACCOUNTS and known['sso.allowed-domains'].kind == 'list'


def test_users_store_adds_sso_accounts_and_links_in_one_write(store: Store):
    # given
    users = Users(store.db, now=lambda: 1000.0)

    # when
    made = users.add_sso(DANA, 'Dana', 'admin')
    linked = users.create(ERIN, 'Erin', 'viewer', PASSWORD, link_by='admin@example.com')

    # then
    assert (made['source'], made['role'], made['roleFrom'], made['adminRole'], made['lastLogin'], 'pw' in made) == ('sso', 'admin', 'admin', 'admin', None, False)
    assert linked['ssoLink'] == {'state': 'pending', 'by': 'admin@example.com', 'at': stamp(1000.0), 'atTs': 1000.0}
    with pytest.raises(UserExists):
        users.add_sso(DANA, 'Dana', 'viewer')
    assert users.authenticate(DANA, '') is None


def test_users_store_makes_any_sso_account_local(store: Store):
    # given
    users = Users(store.db, now=lambda: 2000.0)
    users.add_sso(DANA, 'Dana', 'viewer')
    users.upsert_sso(ERIN, 'Erin', 'viewer')

    # when
    made = users.make_local(DANA, PASSWORD, 'admin@example.com')
    signed_in = users.make_local(ERIN, PASSWORD, 'admin@example.com')

    # then
    assert made is not None and (made['source'], made['ssoLink']['state'], made['ssoLink']['atTs']) == ('local', 'pending', 2000.0)
    assert users.authenticate(DANA, PASSWORD) is not None
    assert signed_in is not None and raw(store, ERIN)['source'] == 'local' and users.authenticate(ERIN, PASSWORD) is not None
    assert users.make_local(DANA, PASSWORD, 'admin@example.com') is None


def test_users_store_binds_a_pre_added_password_account_on_its_first_verified_sso_sign_in(store: Store):
    # given
    users = Users(store.db, now=lambda: 3000.0)
    users.create(DANA, 'Dana', 'viewer', PASSWORD, link_by='admin@example.com')

    # when
    late = users.bind_sso(DANA, 'https://idp.example.com', SUBJECT, 3000.0 + LINK_PENDING_S + 1)
    bound = users.bind_sso(DANA, 'https://idp.example.com', SUBJECT, 3000.0 + 60)
    again = users.bind_sso(DANA, 'https://idp.example.com', 'other-subject', 3000.0 + 120)

    # then
    assert (late, bound, again) == (False, True, False)
    assert (raw(store, DANA)['ssoLink']['sub'], raw(store, DANA)['ssoLink']['state']) == (SUBJECT, 'bound')


def test_the_page_uses_the_password_limits_of_the_users_store():
    # given
    admin = (Path(skopax.__file__).parent / 'web' / 'static' / 'admin.js').read_text(encoding='utf-8')

    # when
    found = re.search(r'ACCESS_PW_MIN = (\d+), ACCESS_PW_MAX = (\d+)', admin)

    # then
    assert found and (int(found.group(1)), int(found.group(2))) == (PASSWORD_MIN, PASSWORD_MAX)


def test_an_sso_only_account_opens_only_with_a_verified_email_whatever_the_setting(store: Store):
    # given
    admin_of(sso_client(store)).post(USERS, json={'email': DANA, 'name': 'Dana', 'role': 'admin', 'sso': True})
    pending = raw(store, DANA)

    # when
    unverified = sso(sso_client(store), email_verified=False)
    after_unverified = raw(store, DANA)
    strict = sso(sso_client(store, sso_require_verified_email=True), email_verified=None)
    after_strict = raw(store, DANA)
    lenient = sso(sso_client(store), email_verified=None)
    after_lenient = raw(store, DANA)
    verified = sso(sso_client(store))

    # then
    assert refused(unverified) and after_unverified == pending
    assert refused(strict) and after_strict == pending and after_strict['lastLogin'] is None
    assert refused(lenient) and after_lenient == pending, 'with Require verified email off, a token without the claim still does not open an account added in Admin'
    assert verified.status_code == 303 and has_session(verified)
    assert raw(store, DANA)['lastLogin'] is not None and 'opened' not in raw(store, DANA)
