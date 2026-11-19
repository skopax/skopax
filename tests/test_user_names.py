import dataclasses
import logging

import mongomock
import pytest
from fastapi.testclient import TestClient

from skopax.app import create_app
from skopax.auth import login_name
from skopax.messages import msg
from skopax.roles import Resolvers
from skopax.signin import SignIn
from skopax.store import Store
from skopax.users import NAME_MAX, InvalidUser, Users, clean_name, name_from
from tests.conftest import ADMIN_EMAIL, AUTH, RELEASE_IGNORE, settings
from tests.test_add_user import DANA, PASSWORD, USERS, access_lines, admin_of, raw, sso, sso_client
from tests.test_setup import app_client as setup_client, form, fresh_store, issued_code

ROOT = 'root@example.com'
ISSUER = 'https://idp.example.com'
ME = '/api/me'


def session_client(store: Store, email: str, **over) -> TestClient:
    client = TestClient(create_app(dataclasses.replace(settings(), **over), store), base_url='https://testserver', follow_redirects=False)
    res = client.post('/login', data={'email': email, 'password': PASSWORD})
    assert res.status_code == 303
    return client


def signin_of(store: Store) -> SignIn:
    return SignIn(Users(store.db), settings(), Resolvers(lambda: ()))


def mcp_claims(email: str, **extra) -> dict:
    return {'iss': ISSUER, 'sub': f'{email}-sub', 'email': email, 'email_verified': True} | extra


@pytest.mark.parametrize('doc, effective', [
    ({'name': 'Admin', 'email': ROOT}, 'default'),
    ({'name': ROOT, 'email': ROOT}, 'default'),
    ({'name': 'Root@Example.com', 'email': ROOT}, 'default'),
    ({'name': 'Root Person', 'email': ROOT}, 'admin'),
    ({'name': 'Root Person', 'email': ROOT, 'source': 'local'}, 'admin'),
    ({'name': 'Root Person', 'email': ROOT, 'source': 'sso'}, 'idp'),
    ({'name': ROOT, 'email': ROOT, 'source': 'sso'}, 'default'),
    ({'name': 'Admin', 'email': ROOT, 'nameFrom': 'self'}, 'self'),
    ({'name': 'Root Person', 'email': ROOT, 'nameFrom': 'idp'}, 'idp'),
])
def test_a_missing_name_source_reads_as_default_only_for_the_setup_name_or_the_email(doc: dict, effective: str):
    # then
    assert name_from(doc) == effective


def test_the_idp_name_is_the_name_claim_then_given_and_family_then_the_username_then_the_email():
    # then
    assert login_name({'name': 'Ada Lovelace', 'given_name': 'A', 'family_name': 'L', 'email': 'ada@example.com'}) == 'Ada Lovelace'
    assert login_name({'given_name': 'Ada', 'family_name': 'Lovelace', 'preferred_username': 'ada', 'email': 'ada@example.com'}) == 'Ada Lovelace'
    assert login_name({'given_name': 'Ada', 'preferred_username': 'ada', 'email': 'ada@example.com'}) == 'ada'
    assert login_name({'family_name': 'Lovelace', 'email': 'ada@example.com'}) == 'ada@example.com'
    assert login_name({'given_name': ' ', 'family_name': 'Lovelace', 'email': 'ada@example.com'}) == 'ada@example.com'


def test_the_env_admin_named_admin_takes_the_idp_name_at_its_linked_sso_sign_in():
    # given
    store = Store(mongomock.MongoClient(tz_aware=True)['skopax'], release_ignore=RELEASE_IGNORE)
    client = sso_client(store, admin_email=ROOT, admin_password=PASSWORD)
    client.get('/setup')
    made = raw(store, ROOT)
    admin_of(client).patch(f'{USERS}/{ROOT}', json={'ssoLink': True})

    # when
    signed = sso(client, email=ROOT, sub='root-sub', name='Root Person')
    after = raw(store, ROOT)
    again = sso(client, email=ROOT, sub='root-sub', name='Root P. Person')

    # then
    assert (made['name'], made['nameFrom']) == (msg('setup.admin_name'), 'default')
    assert signed.status_code == 303
    assert (after['name'], after['nameFrom']) == ('Root Person', 'idp')
    assert again.status_code == 303 and raw(store, ROOT)['name'] == 'Root P. Person', 'an IdP name follows the IdP on each sign-in'


def test_a_legacy_env_admin_without_a_name_source_is_renamed_at_its_linked_sso_sign_in(store: Store):
    # given
    users = Users(store.db)
    store.db['users'].update_one({'_id': ADMIN_EMAIL}, {'$unset': {'nameFrom': ''}})
    client = sso_client(store)
    admin_of(client).patch(f'{USERS}/{ADMIN_EMAIL}', json={'ssoLink': True})

    # when
    signed = sso(client, email=ADMIN_EMAIL, sub='admin-sub', given_name='Alice', family_name='Archer')

    # then
    assert signed.status_code == 303
    assert (users.get(ADMIN_EMAIL)['name'], users.get(ADMIN_EMAIL)['nameFrom']) == ('Alice Archer', 'idp')


def test_an_sso_account_named_by_its_email_or_by_the_idp_follows_the_idp_and_a_chosen_name_stays(store: Store):
    # given
    client = sso_client(store)
    sso(client, email=DANA)
    fallback = raw(store, DANA)
    store.db['users'].update_one({'_id': DANA}, {'$unset': {'nameFrom': ''}})
    store.db['users'].insert_one({'_id': 'bob@example.com', 'email': 'bob@example.com', 'name': 'Bobby', 'role': 'viewer', 'source': 'sso', 'disabled': False, 'createdAt': 'x', 'lastLogin': None})
    store.db['users'].insert_one({'_id': 'mixed@example.com', 'email': 'mixed@example.com', 'name': 'Mixed@Example.com', 'role': 'viewer', 'source': 'sso', 'disabled': False, 'createdAt': 'x', 'lastLogin': None})
    store.db['users'].insert_one({'_id': 'cy@example.com', 'email': 'cy@example.com', 'name': 'Cy Self', 'nameFrom': 'self', 'role': 'viewer', 'source': 'sso', 'disabled': False, 'createdAt': 'x', 'lastLogin': None})

    # when
    sso(client, email=DANA, name='Dana Diaz')
    sso(client, email='bob@example.com', sub='bob-sub', name='Bob Baker')
    sso(client, email='cy@example.com', sub='cy-sub', name='Cy Idp')
    sso(client, email='mixed@example.com', sub='mixed-sub', name='Max Mixed')

    # then
    assert (fallback['name'], fallback['nameFrom']) == (DANA, 'default'), 'with no name claim the email is a fallback, not a chosen name'
    assert (raw(store, DANA)['name'], raw(store, DANA)['nameFrom']) == ('Dana Diaz', 'idp')
    assert (raw(store, 'bob@example.com')['name'], raw(store, 'bob@example.com')['nameFrom']) == ('Bob Baker', 'idp'), 'a legacy SSO account with a real name keeps following the IdP'
    assert raw(store, 'cy@example.com')['name'] == 'Cy Self'
    assert raw(store, 'mixed@example.com')['name'] == 'Max Mixed', 'an email kept in its provider case is still a fallback'
    listed = {u['email']: u['nameFrom'] for u in admin_of(client).get(USERS).json()}
    assert (listed['bob@example.com'], listed['cy@example.com'], listed[DANA]) == ('idp', 'self', 'idp'), 'the admin list says where each name came from'


def test_a_legacy_local_account_with_a_real_name_keeps_it_at_its_linked_sso_sign_in(store: Store):
    # given
    users = Users(store.db)
    users.create(DANA, 'Dana from Ops', 'viewer', PASSWORD)
    store.db['users'].update_one({'_id': DANA}, {'$unset': {'nameFrom': ''}})
    client = sso_client(store)
    admin_of(client).patch(f'{USERS}/{DANA}', json={'ssoLink': True})
    listed = {u['email']: u['nameFrom'] for u in admin_of(client).get(USERS).json()}

    # when
    signed = sso(client, email=DANA, name='Dana Idp')

    # then
    assert signed.status_code == 303
    assert listed[DANA] == 'admin'
    assert raw(store, DANA)['name'] == 'Dana from Ops' and 'nameFrom' not in raw(store, DANA)


def test_a_new_sso_account_stores_its_idp_name_and_a_long_name_is_cut_to_the_limit(store: Store):
    # given
    client = sso_client(store)

    # when
    sso(client, email=DANA, given_name='Dana', family_name='D' * 200)

    # then
    doc = raw(store, DANA)
    assert doc['nameFrom'] == 'idp' and doc['name'] == ('Dana ' + 'D' * 200)[:NAME_MAX]


def test_an_admin_added_person_with_no_name_takes_the_idp_name_but_a_typed_name_stays(store: Store):
    # given
    client = sso_client(store)
    admin_of(client).post(USERS, json={'email': DANA, 'role': 'viewer', 'sso': True})
    admin_of(client).post(USERS, json={'email': 'erin@example.com', 'name': 'Erin E', 'role': 'viewer', 'sso': True})
    before = (raw(store, DANA)['nameFrom'], raw(store, 'erin@example.com')['nameFrom'])

    # when
    sso(client, email=DANA, name='Dana Diaz')
    sso(client, email='erin@example.com', sub='erin-sub', name='Erin Evans')

    # then
    assert before == ('default', 'admin')
    assert raw(store, DANA)['name'] == 'Dana Diaz'
    assert raw(store, 'erin@example.com')['name'] == 'Erin E'


def test_the_mcp_sign_in_names_new_accounts_but_never_rewrites_a_known_name(store: Store):
    # given
    signin = signin_of(store)
    store.db['users'].insert_one({'_id': 'bob@example.com', 'email': 'bob@example.com', 'name': 'bob@example.com', 'nameFrom': 'default', 'role': 'viewer', 'source': 'sso', 'disabled': False, 'createdAt': 'x', 'lastLogin': None})

    # when
    signin.mcp(mcp_claims(DANA, name='Dana Diaz'), ISSUER)
    signin.mcp(mcp_claims('erin@example.com'), ISSUER)
    signin.mcp(mcp_claims('bob@example.com', name='Bob Baker'), ISSUER)

    # then
    assert (raw(store, DANA)['name'], raw(store, DANA)['nameFrom']) == ('Dana Diaz', 'idp')
    assert (raw(store, 'erin@example.com')['name'], raw(store, 'erin@example.com')['nameFrom']) == ('erin@example.com', 'default')
    assert raw(store, 'bob@example.com')['name'] == 'bob@example.com', 'MCP access tokens often carry no profile claims, so only the page sign-in refreshes names'


def test_an_admin_renames_anyone_with_an_activity_line_and_the_idp_no_longer_overrides_it(store: Store):
    # given
    client = sso_client(store)
    sso(client, email=DANA, name='Dana Diaz')
    admin = admin_of(client)

    before = raw(store, DANA)

    # when
    renamed = admin.patch(f'{USERS}/{DANA}', json={'name': '  Dana from Ops  '})
    after = raw(store, DANA)
    sso(client, email=DANA, name='Dana Idp')
    empty = admin.patch(f'{USERS}/{DANA}', json={'name': '   '})
    long = admin.patch(f'{USERS}/{DANA}', json={'name': 'x' * (NAME_MAX + 1)})
    wrong = admin.patch(f'{USERS}/{DANA}', json={'name': 7})
    missing = admin.patch(f'{USERS}/nobody@example.com', json={'name': 'Nobody'})

    # then
    assert renamed.status_code == 200 and renamed.json()['name'] == 'Dana from Ops' and renamed.json()['nameFrom'] == 'admin'
    changed = {k for k in set(before) | set(after) if before.get(k) != after.get(k)}
    assert changed == {'name', 'nameFrom'}, 'a rename writes only the name and its source'
    assert raw(store, DANA)['name'] == 'Dana from Ops', 'an admin-chosen name survives the next sign-in'
    assert (empty.status_code, empty.json()) == (422, {'detail': msg('user.name_length', max=NAME_MAX), 'field': 'name'})
    assert (long.status_code, long.json()['field']) == (422, 'name')
    assert (wrong.status_code, wrong.json()['field']) == (422, 'name')
    assert missing.status_code == 404
    assert msg('access.user_renamed', email=DANA, name='Dana from Ops') in access_lines(store)
    assert len([t for t in access_lines(store) if 'renamed' in t]) == 1


def test_only_admins_rename_other_people(store: Store):
    # given
    Users(store.db).create('vic@example.com', 'Vic', 'operator', PASSWORD)
    client = session_client(store, 'vic@example.com')

    # when
    res = client.patch(f'{USERS}/{ADMIN_EMAIL}', json={'name': 'Mallory'})

    # then
    assert res.status_code == 403
    assert raw(store, ADMIN_EMAIL)['name'] == 'Admin'


def test_a_signed_in_person_renames_themselves_and_the_page_sees_it_at_once(store: Store):
    # given
    Users(store.db).create('vic@example.com', 'vic@example.com', 'viewer', PASSWORD)
    client = session_client(store, 'vic@example.com')
    before = client.get(ME).json()

    # when
    res = client.patch(ME, json={'name': '  Vic Viewer '})
    after = client.get(ME).json()

    # then
    assert before['canEditName'] is True and before['name'] == 'vic@example.com'
    assert res.status_code == 200 and res.json() == {'name': 'Vic Viewer'}
    assert after['name'] == 'Vic Viewer', 'no stale cached name after the change'
    assert (raw(store, 'vic@example.com')['name'], raw(store, 'vic@example.com')['nameFrom']) == ('Vic Viewer', 'self')
    assert msg('access.self_renamed', name='Vic Viewer') in access_lines(store)


def test_a_self_chosen_sso_name_survives_the_next_sign_in(store: Store):
    # given
    client = sso_client(store)
    sso(client, email=DANA, name='Dana Diaz')

    # when
    res = client.patch(ME, json={'name': 'Dee'})
    sso(client, email=DANA, name='Dana Idp')

    # then
    assert res.status_code == 200
    assert (raw(store, DANA)['name'], raw(store, DANA)['nameFrom']) == ('Dee', 'self')


def test_a_self_rename_needs_a_session_from_this_site_and_a_valid_name(store: Store):
    # given
    Users(store.db).create('vic@example.com', 'Vic', 'viewer', PASSWORD)
    client = session_client(store, 'vic@example.com')
    anonymous = TestClient(create_app(settings(), store), base_url='https://testserver')

    # when
    by_token = anonymous.patch(ME, json={'name': 'Bot'}, headers=AUTH)
    by_token_with_session = client.patch(ME, json={'name': 'Bot'}, headers=AUTH)
    signed_out = anonymous.patch(ME, json={'name': 'Nobody'})
    cross = client.patch(ME, json={'name': 'Evil'}, headers={'Origin': 'https://evil.example.com'})
    empty = client.patch(ME, json={'name': ''})
    long = client.patch(ME, json={'name': 'x' * (NAME_MAX + 1)})
    extra = client.patch(ME, json={'name': 'Vic', 'role': 'admin'})
    me_by_token = anonymous.get(ME, headers=AUTH).json()

    # then
    assert by_token.status_code == 401 and by_token_with_session.status_code == 401
    assert signed_out.status_code == 401
    assert cross.status_code == 403
    assert (empty.status_code, empty.json()) == (422, {'detail': msg('user.name_length', max=NAME_MAX), 'field': 'name'})
    assert (long.status_code, long.json()['field']) == (422, 'name')
    assert extra.status_code == 422
    assert me_by_token['canEditName'] is False
    assert (raw(store, 'vic@example.com')['name'], raw(store, 'vic@example.com')['nameFrom']) == ('Vic', 'admin')
    assert not [t for t in access_lines(store) if 'name' in t.lower()]


def test_the_setup_page_records_a_typed_name_and_a_plain_create_counts_as_admin_chosen(caplog):
    # given
    store = fresh_store()
    caplog.set_level(logging.INFO, logger='skopax')
    client = setup_client(store)
    client.get('/setup')
    code = issued_code(caplog)

    # when
    done = client.post('/setup', data=form(code, email='typed@example.com', name='Typed Person'))
    Users(store.db).create('plain@example.com', 'Plain', 'viewer', PASSWORD)

    # then
    assert done.status_code == 303
    assert (raw(store, 'typed@example.com')['name'], raw(store, 'typed@example.com')['nameFrom']) == ('Typed Person', 'self')
    assert raw(store, 'plain@example.com')['nameFrom'] == 'admin'


def test_a_sign_in_without_a_name_claim_keeps_a_real_name_but_replaces_the_setup_name(store: Store):
    # given
    client = sso_client(store)
    sso(client, email=DANA, name='Dana Diaz')
    admin_of(client).post(USERS, json={'email': 'erin@example.com', 'role': 'viewer', 'sso': True})
    store.db['users'].update_one({'_id': ADMIN_EMAIL}, {'$unset': {'nameFrom': ''}})
    admin_of(client).patch(f'{USERS}/{ADMIN_EMAIL}', json={'ssoLink': True})

    # when
    sso(client, email=DANA)
    sso(client, email='erin@example.com', sub='erin-sub')
    sso(client, email=ADMIN_EMAIL, sub='admin-sub')

    # then
    assert (raw(store, DANA)['name'], raw(store, DANA)['nameFrom']) == ('Dana Diaz', 'idp')
    assert (raw(store, 'erin@example.com')['name'], raw(store, 'erin@example.com')['nameFrom']) == ('erin', 'default')
    assert (raw(store, ADMIN_EMAIL)['name'], raw(store, ADMIN_EMAIL)['nameFrom']) == (ADMIN_EMAIL, 'default')


def test_a_rename_that_lands_during_an_idp_refresh_wins(store: Store, monkeypatch):
    # given
    users = Users(store.db)
    users.create(DANA, DANA, 'viewer', PASSWORD, name_from='default')
    real = users.col.find_one

    def raced(*args, **kwargs):
        held = real(*args, **kwargs)
        store.db['users'].update_one({'_id': DANA}, {'$set': {'name': 'Chosen', 'nameFrom': 'self'}})
        return held

    monkeypatch.setattr(users.col, 'find_one', raced)

    # when
    changed = users.idp_name(DANA, 'Dana Idp')

    # then
    assert changed is False
    assert (raw(store, DANA)['name'], raw(store, DANA)['nameFrom']) == ('Chosen', 'self')


def test_saving_the_same_name_again_writes_and_logs_nothing(store: Store, monkeypatch):
    # given
    Users(store.db).create('vic@example.com', 'Vic', 'viewer', PASSWORD)
    client = session_client(store, 'vic@example.com')
    admin = admin_of(sso_client(store))
    client.patch(ME, json={'name': 'Vic V'})
    Users(store.db).create(DANA, 'Dana', 'viewer', PASSWORD)
    admin.patch(f'{USERS}/{DANA}', json={'name': 'Dana D'})
    lines = access_lines(store)
    writes = []
    real_rename, real_update = Users.rename, Users.guarded_update
    monkeypatch.setattr(Users, 'rename', lambda self, *a, **k: writes.append('rename') or real_rename(self, *a, **k))
    monkeypatch.setattr(Users, 'guarded_update', lambda self, *a, **k: writes.append('update') or real_update(self, *a, **k))

    # when
    self_again = client.patch(ME, json={'name': ' Vic V '})
    admin_again = admin.patch(f'{USERS}/{DANA}', json={'name': 'Dana D'})

    # then
    assert (self_again.status_code, self_again.json()) == (200, {'name': 'Vic V'})
    assert admin_again.status_code == 200 and admin_again.json()['name'] == 'Dana D'
    assert writes == []
    assert access_lines(store) == lines


def test_a_name_loses_control_and_bidi_characters_and_is_refused_when_nothing_is_left():
    # then
    assert clean_name('Ada' + chr(0x202E) + ' Love' + chr(0) + 'lace' + chr(0x85) + chr(0x2066)) == 'Ada Lovelace'
    assert clean_name(chr(7) + ' Bo ' + chr(0x2069)) == 'Bo'
    with pytest.raises(InvalidUser):
        clean_name(chr(0x202A) + chr(0x1B) + ' ' + chr(0x9F))


def test_the_rename_routes_refuse_a_name_of_only_hidden_characters(store: Store):
    # given
    Users(store.db).create('vic@example.com', 'Vic', 'viewer', PASSWORD)
    client = session_client(store, 'vic@example.com')
    hidden = chr(0x202E) + chr(0x2066) + chr(1)

    # when
    self_res = client.patch(ME, json={'name': hidden})
    admin_res = admin_of(sso_client(store)).patch(f'{USERS}/vic@example.com', json={'name': 'Vic' + chr(0x202E)})

    # then
    assert (self_res.status_code, self_res.json()['field']) == (422, 'name')
    assert admin_res.status_code == 200 and raw(store, 'vic@example.com')['name'] == 'Vic'
