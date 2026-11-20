import dataclasses

import pytest

from skopax import app as skopax_app
from skopax.mcpauth import Forbidden, McpAuth
from skopax.messages import msg
from skopax.roles import Resolvers
from skopax.signin import Refused, SignIn
from skopax.store import Store
from skopax.users import Users
from tests.conftest import ADMIN_EMAIL, settings
from tests.test_add_user import DANA, ERIN, PASSWORD, USERS, Clock, access_lines, admin_of, has_session, raw, refused, sso, sso_client

BOSS = 'boss@example.com'
ISSUER = 'https://idp.example.com'
RESTRICTED = [{'sso_new_accounts': 'added-only'}, {'sso_allowed_domains': ('example.com',)}]
NAMES = ['added-only', 'allowed-domains']


def signin_of(store: Store, **over) -> SignIn:
    return SignIn(Users(store.db), dataclasses.replace(settings(), **over), Resolvers(lambda: ()))


def mcp_refusal(signin: SignIn, claims: dict) -> str | None:
    try:
        McpAuth(signin.settings, None, signin, 'https://skopax.example.com')._signed_in(claims, ISSUER)
    except Forbidden as e:
        return e.detail
    return None


def claims(email: str | None = BOSS, verified=True, **extra) -> dict:
    return {k: v for k, v in {'iss': ISSUER, 'sub': 'boss-sub', 'email': email, 'email_verified': verified, **extra}.items() if v is not None}


@pytest.mark.parametrize('over', RESTRICTED, ids=NAMES)
def test_a_restricted_page_sign_in_trusts_only_a_verified_email_claim(store: Store, over: dict):
    # given
    client = sso_client(store, **over)
    admin_of(client).post(USERS, json={'email': BOSS, 'name': 'Boss', 'role': 'admin', 'sso': True})
    sso(sso_client(store, **over), email=BOSS, sub='boss-sub')
    before, lines = raw(store, BOSS), access_lines(store)

    # when
    named = sso(sso_client(store, **over), email=None, preferred_username=BOSS, sub='boss-sub')
    unverified = sso(sso_client(store, **over), email=BOSS, email_verified=False, sub='boss-sub')
    missing = sso(sso_client(store, **over), email=BOSS, email_verified=None, sub='boss-sub')
    texty = sso(sso_client(store, **over), email=BOSS, email_verified='yes', sub='boss-sub')
    stranger = sso(sso_client(store, **over), email=None, preferred_username='new@example.com', sub='new-sub')
    after, after_lines = raw(store, BOSS), access_lines(store)
    verified = sso(sso_client(store, **over), email=BOSS, sub='boss-sub')

    # then
    assert refused(named) and refused(unverified) and refused(missing) and refused(texty) and refused(stranger)
    assert after == before and after_lines == lines, 'a refusal writes nothing, not even an activity line'
    assert raw(store, 'new@example.com') is None
    assert verified.status_code == 303 and has_session(verified)


@pytest.mark.parametrize('over', RESTRICTED, ids=NAMES)
def test_a_restricted_mcp_sign_in_trusts_only_a_verified_email_claim(store: Store, over: dict):
    # given
    Users(store.db).add_sso(BOSS, 'Boss', 'admin')
    signin = signin_of(store, **over)
    signin.mcp(claims(), ISSUER)
    before = raw(store, BOSS)

    # when
    named = mcp_refusal(signin, claims(None, preferred_username=BOSS))
    unverified = mcp_refusal(signin, claims(verified=False))
    missing = mcp_refusal(signin, claims(verified=None))
    stranger = mcp_refusal(signin, claims(None, preferred_username='new@example.com', sub='new-sub'))
    after = raw(store, BOSS)
    verified = signin.mcp(claims(), ISSUER)

    # then
    assert [named, unverified, missing, stranger] == [msg('mcp.not_allowed')] * 4
    assert after == before and raw(store, 'new@example.com') is None
    assert (verified.email, verified.role) == (BOSS, 'admin')


def test_without_either_setting_the_sign_in_reads_the_address_as_before(store: Store):
    # given
    signin = signin_of(store)

    # when
    page = sso(sso_client(store), email=None, preferred_username=ERIN, sub='erin-sub')
    lenient = sso(sso_client(store), email='new@example.com', email_verified=None, sub='new-sub')
    mcp = signin.mcp(claims(None, preferred_username='mo@example.com', sub='mo-sub'), ISSUER)

    # then
    assert page.status_code == 303 and raw(store, ERIN)['source'] == 'sso'
    assert lenient.status_code == 303 and raw(store, 'new@example.com')['source'] == 'sso'
    assert mcp.email == 'mo@example.com' and raw(store, 'mo@example.com')['source'] == 'sso'


def test_a_pre_added_sso_account_opens_on_the_page_only_with_its_own_verified_email(store: Store):
    # given
    admin_of(sso_client(store)).post(USERS, json={'email': DANA, 'name': 'Dana', 'role': 'admin', 'sso': True})
    pending = raw(store, DANA)

    # when
    missing = sso(sso_client(store), email_verified=None)
    unverified = sso(sso_client(store), email_verified=False)
    named = sso(sso_client(store), email=None, preferred_username=DANA)
    after = raw(store, DANA)
    opened = sso(sso_client(store))
    after_open = raw(store, DANA)
    lenient = sso(sso_client(store), email_verified=None)

    # then
    assert pending['opened'] is False
    assert refused(missing) and refused(unverified) and refused(named)
    assert after == pending, 'a refused first sign-in writes nothing'
    assert opened.status_code == 303 and has_session(opened) and 'opened' not in after_open and after_open['lastLogin'] is not None
    assert lenient.status_code == 303 and has_session(lenient), 'once opened, Require verified email off takes a token without the claim as usual'


def test_a_pre_added_sso_account_opens_over_mcp_only_with_its_own_verified_email(store: Store):
    # given
    users = Users(store.db)
    users.add_sso(DANA, 'Dana', 'operator')
    signin = signin_of(store)
    pending = raw(store, DANA)
    own = lambda **over: claims(DANA, **over)

    # when
    missing = mcp_refusal(signin, own(verified=None))
    unverified = mcp_refusal(signin, own(verified=False))
    named = mcp_refusal(signin, claims(None, preferred_username=DANA))
    after = raw(store, DANA)
    opened = signin.mcp(own(), ISSUER)
    after_open = raw(store, DANA)
    lenient = signin.mcp(own(verified=None), ISSUER)

    # then
    assert [missing, unverified, named] == [msg('mcp.unverified')] * 3
    assert after == pending
    assert (opened.email, opened.role) == (DANA, 'operator') and 'opened' not in after_open and after_open['lastLogin'] is not None
    assert lenient.role == 'operator'


def test_an_account_the_mcp_sign_in_makes_is_open_from_the_start(store: Store):
    # given
    signin = signin_of(store)

    # when
    made = signin.mcp(claims(ERIN, verified=None), ISSUER)

    # then
    assert made.email == ERIN and 'opened' not in raw(store, ERIN)


def test_set_password_works_whether_or_not_the_account_was_opened(store: Store):
    # given
    client = sso_client(store)
    admin = admin_of(client)
    for email in (DANA, ERIN):
        admin.post(USERS, json={'email': email, 'name': email.split('@')[0], 'role': 'viewer', 'sso': True})
    signin_of(store).mcp(claims(ERIN), ISSUER)
    store.db['users'].insert_one({'_id': BOSS, 'email': BOSS, 'name': 'Boss', 'role': 'viewer', 'roleFrom': 'admin', 'source': 'sso', 'disabled': False, 'createdAt': 'x', 'lastLogin': None})
    listed = {u['email']: u for u in admin.get(USERS).json()}

    # when
    dana = admin.patch(f'{USERS}/{DANA}', json={'password': PASSWORD})
    erin = admin.patch(f'{USERS}/{ERIN}', json={'password': PASSWORD})
    boss = admin.patch(f'{USERS}/{BOSS}', json={'password': PASSWORD})

    # then
    assert (listed[DANA]['opened'], 'opened' in listed[ERIN], 'opened' in listed[BOSS]) == (False, False, False)
    for email, res in ((DANA, dana), (ERIN, erin), (BOSS, boss)):
        assert res.status_code == 200 and raw(store, email)['source'] == 'local' and 'opened' not in raw(store, email)


def test_an_opened_account_is_made_local_too(store: Store):
    # given
    users = Users(store.db)
    users.add_sso(DANA, 'Dana', 'viewer')
    signin_of(store).page(claims(DANA), ISSUER)

    # when
    made = users.make_local(DANA, PASSWORD, 'admin@example.com')

    # then
    assert made is not None and raw(store, DANA)['source'] == 'local' and users.authenticate(DANA, PASSWORD) is not None


def test_a_pre_added_account_that_appears_during_a_page_sign_in_is_not_opened_without_a_verified_email(store: Store, monkeypatch):
    # given
    users = Users(store.db)
    signin = SignIn(users, settings(), Resolvers(lambda: ()))
    real_get = users.get

    def raced(email):
        found = real_get(email)
        if found is None:
            users.add_sso(DANA, 'Dana', 'admin')
        return found

    monkeypatch.setattr(users, 'get', raced)

    # when
    with pytest.raises(Refused) as e:
        signin.page(claims(DANA, verified=None), ISSUER)

    # then
    assert e.value.reason == 'unverified' and raw(store, DANA)['opened'] is False and raw(store, DANA)['lastLogin'] is None


def test_add_user_refuses_an_sso_account_outside_the_allowed_domains_but_keeps_a_password_only_one(store: Store):
    # given
    admin = admin_of(sso_client(store, sso_allowed_domains=('example.com',)))

    # when
    sso_only = admin.post(USERS, json={'email': 'zed@example.org', 'name': '', 'role': 'viewer', 'sso': True})
    linked = admin.post(USERS, json={'email': 'zoe@example.org', 'name': '', 'role': 'viewer', 'sso': True, 'password': PASSWORD})
    local = admin.post(USERS, json={'email': 'lou@example.org', 'name': '', 'role': 'admin', 'password': PASSWORD})
    inside = admin.post(USERS, json={'email': ERIN, 'name': '', 'role': 'viewer', 'sso': True})

    # then
    for res in (sso_only, linked):
        assert (res.status_code, res.json()) == (422, {'detail': msg('access.sso_domain'), 'field': 'email'})
    assert raw(store, 'zed@example.org') is None and raw(store, 'zoe@example.org') is None
    assert local.status_code == 201 and raw(store, 'lou@example.org')['source'] == 'local' and 'ssoLink' not in raw(store, 'lou@example.org')
    assert inside.status_code == 201


def test_an_sso_link_outside_the_allowed_domains_is_refused(store: Store):
    # given
    admin = admin_of(sso_client(store, sso_allowed_domains=('example.com',)))
    admin.post(USERS, json={'email': 'lou@example.org', 'name': '', 'role': 'viewer', 'password': PASSWORD})
    admin.post(USERS, json={'email': ERIN, 'name': '', 'role': 'viewer', 'password': PASSWORD})
    before = raw(store, 'lou@example.org')

    # when
    outside = admin.patch(f'{USERS}/lou@example.org', json={'ssoLink': True})
    off = admin.patch(f'{USERS}/lou@example.org', json={'ssoLink': False})
    inside = admin.patch(f'{USERS}/{ERIN}', json={'ssoLink': True})

    # then
    assert (outside.status_code, outside.json()['detail']) == (422, msg('access.sso_domain'))
    assert raw(store, 'lou@example.org') == before
    assert off.status_code == 200
    assert inside.status_code == 200 and raw(store, ERIN)['ssoLink']['state'] == 'pending'


@pytest.mark.parametrize('change', [{'role': 'viewer'}, {'disabled': True}])
def test_a_password_patch_that_would_remove_the_last_admin_writes_nothing(store: Store, change: dict):
    # given
    admin = admin_of(sso_client(store))
    admin.post(USERS, json={'email': DANA, 'name': 'Dana', 'role': 'admin', 'sso': True})
    store.db['users'].update_one({'_id': ADMIN_EMAIL}, {'$set': {'disabled': True}})
    before = raw(store, DANA)

    # when
    res = admin.patch(f'{USERS}/{DANA}', json={'password': PASSWORD} | change)

    # then
    assert (res.status_code, res.json()['detail']) == (409, msg('access.last_admin'))
    assert raw(store, DANA) == before


def test_a_password_patch_with_a_bad_link_or_password_writes_nothing(store: Store):
    # given
    admin = admin_of(sso_client(store, sso_allowed_domains=('example.com',)))
    Users(store.db).add_sso('old@example.org', 'Old', 'viewer')
    admin.post(USERS, json={'email': DANA, 'name': 'Dana', 'role': 'viewer', 'sso': True})
    before_old, before_dana = raw(store, 'old@example.org'), raw(store, DANA)

    # when
    linked = admin.patch(f'{USERS}/old@example.org', json={'password': PASSWORD, 'ssoLink': True})
    short = admin.patch(f'{USERS}/{DANA}', json={'password': 'short', 'role': 'operator'})

    # then
    assert (linked.status_code, linked.json()['detail']) == (422, msg('access.sso_domain'))
    assert (short.status_code, short.json()['field']) == (422, 'password'), 'the Set password form still gets its field message'
    assert raw(store, 'old@example.org') == before_old and raw(store, DANA) == before_dana


def test_a_refused_new_email_writes_one_activity_line_per_hour(store: Store):
    # given
    clock = Clock()
    over = {'sso_new_accounts': 'added-only'}
    client = sso_client(store, clock, **over)
    line = msg('access.sso_not_added', email='zed@example.com')

    # when
    for _ in range(3):
        sso(client, email='zed@example.com', sub='zed')
    sso(client, email='zoe@example.com', sub='zoe')
    within = access_lines(store)
    clock.now += 3600
    sso(client, email='zed@example.com', sub='zed')

    # then
    assert within == [line, msg('access.sso_not_added', email='zoe@example.com')]
    assert access_lines(store) == within + [line]


def test_the_refused_email_memory_is_bounded(store: Store, monkeypatch):
    # given
    monkeypatch.setattr(skopax_app, 'NOT_ADDED_KEEP', 2)
    client = sso_client(store, Clock(), sso_new_accounts='added-only')

    # when
    for name in ('a', 'b', 'c', 'a'):
        sso(client, email=f'{name}@example.com', sub=name)

    # then
    assert access_lines(store) == [msg('access.sso_not_added', email=f'{name}@example.com') for name in ('a', 'b', 'c', 'a')], 'the oldest email is forgotten once more are kept'


def test_a_demotion_that_races_the_last_admin_check_leaves_an_unopened_sso_account_untouched(store: Store, monkeypatch):
    # given
    admin = admin_of(sso_client(store))
    admin.post(USERS, json={'email': DANA, 'name': 'Dana', 'role': 'admin', 'sso': True})
    store.db['users'].update_one({'_id': ADMIN_EMAIL}, {'$set': {'disabled': True}})
    before = raw(store, DANA)
    monkeypatch.setattr(Users, 'is_last_admin', lambda self, email: False)

    # when
    res = admin.patch(f'{USERS}/{DANA}', json={'password': PASSWORD, 'role': 'viewer'})

    # then
    assert (res.status_code, res.json()['detail']) == (409, msg('access.last_admin'))
    assert raw(store, DANA) == before, 'the password, the local source and the link are written together with the role, or not at all'
    assert Users(store.db).active_admins() == 1


def test_a_password_patch_on_an_unopened_sso_account_that_the_role_comes_from_the_idp_for_is_refused(store: Store):
    # given
    admin = admin_of(sso_client(store))
    admin.post(USERS, json={'email': DANA, 'name': 'Dana', 'role': 'viewer', 'sso': True})
    store.db['users'].update_one({'_id': DANA}, {'$set': {'roleFrom': 'claim'}})
    before = raw(store, DANA)

    # when
    res = admin.patch(f'{USERS}/{DANA}', json={'password': PASSWORD, 'role': 'operator'})

    # then
    assert (res.status_code, res.json()['detail']) == (409, msg('access.role_from_idp'))
    assert raw(store, DANA) == before


def test_a_password_patch_on_an_unopened_sso_account_outside_the_allowed_domains_makes_no_pending_link(store: Store):
    # given
    admin = admin_of(sso_client(store, sso_allowed_domains=('example.com',)))
    Users(store.db).add_sso('old@example.org', 'Old', 'viewer')
    Users(store.db).add_sso(ERIN, 'Erin', 'viewer')

    # when
    outside = admin.patch(f'{USERS}/old@example.org', json={'password': PASSWORD})
    inside = admin.patch(f'{USERS}/{ERIN}', json={'password': PASSWORD})

    # then
    assert outside.status_code == 200 and raw(store, 'old@example.org')['source'] == 'local' and 'ssoLink' not in raw(store, 'old@example.org')
    assert inside.status_code == 200 and raw(store, ERIN)['ssoLink']['state'] == 'pending'
    lines = ' '.join(access_lines(store))
    assert msg('access.sso_linked', email='old@example.org', days=7) not in lines and msg('access.sso_linked', email=ERIN, days=7) in lines


def test_a_refused_sso_link_patch_names_its_field(store: Store):
    # given
    admin = admin_of(sso_client(store, sso_allowed_domains=('example.com',)))
    admin.post(USERS, json={'email': 'lou@example.org', 'name': '', 'role': 'viewer', 'password': PASSWORD})

    # when
    res = admin.patch(f'{USERS}/lou@example.org', json={'ssoLink': True})

    # then
    assert (res.status_code, res.json()) == (422, {'detail': msg('access.sso_domain'), 'field': 'ssoLink'})
