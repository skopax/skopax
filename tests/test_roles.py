import dataclasses
import json
import logging
import threading
import time

import pytest
from fastapi.testclient import TestClient

from skopax import tunables
from skopax.app import create_app
from skopax.auth import SESSION_COOKIE
from skopax.config import ConfigurationError, load_settings
from skopax.contract import Hooks
from skopax.messages import msg
from skopax.operators import OperatorUnknown, Operators
from skopax import roles
from skopax.roles import MAP_MAX, VALUE_MAX, ResolverError, Resolvers, choose, claim_values, mapped_role, parse_role_map, role_map
from skopax.signin import Refused, SignIn
from skopax.sso import SsoHttp
from skopax.store import Store
from skopax.users import Users
from tests.conftest import ADMIN_EMAIL, AUTH, settings
from tests.plugin_kit import registry_with
from tests.plugins import resolver_plugin
from tests.plugins.base import minimal
from tests.sso_kit import FakeIdp, idp_settings, log_in

ALICE = 'alice@example.com'
BOB = 'bob@example.com'
CAROL = 'carol@example.com'
ISSUER = 'https://idp.example.com'
ROLE_MAP = json.dumps([{'value': 'ops-admins', 'role': 'admin'}, {'value': 'ops-operators', 'role': 'operator'}, {'value': 'ops-viewers', 'role': 'viewer'}])
PAIRS = (('ops-admins', 'admin'), ('ops-operators', 'operator'), ('ops-viewers', 'viewer'))
KEPT = 'Keeping the admin role for '


@pytest.fixture(autouse=True)
def answers():
    resolver_plugin.reset()
    yield resolver_plugin.ANSWERS
    resolver_plugin.reset()


def client_for(store: Store, plugins=(resolver_plugin,), **over) -> TestClient:
    idp = FakeIdp()
    s = idp_settings(idp, **({'sso_role_map': ROLE_MAP} | over))
    client = TestClient(create_app(s, store, registry=registry_with(*plugins), sso_http=SsoHttp(fetch=idp.fetch, exchange=idp.exchange)), base_url='https://testserver')
    client.idp = idp
    return client


def sign_in(client: TestClient, email: str, roles: list[str] | None = None):
    return log_in(client, client.idp, sub=email, email=email, roles=roles or [])


def me_role(client: TestClient) -> str | None:
    return client.get('/api/me').json().get('userRole')


def admin_of(client: TestClient) -> TestClient:
    return TestClient(client.app, base_url='https://testserver', headers=AUTH)


def kept_lines(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.getMessage().startswith(KEPT)]


def seen(email: str, channel: str) -> bool:
    return any(user.get('email') == email and ch == channel for user, ch in resolver_plugin.SEEN)


def test_claim_values_follow_a_path_or_a_literal_key():
    # given
    claims = {'roles': ['SKOPAX_ADMIN', 3, 'ops-viewers'], 'realm_access': {'roles': ['ops-admins'], 'n': 1}, 'https://example.com/roles': ['ops-operators'],
              'groups': 'ops-admins', 'number': 7, 'object': {'a': 'b'}, 'cognito:groups': ['ops-viewers']}

    # then
    assert claim_values(claims, 'roles') == ('SKOPAX_ADMIN', 'ops-viewers')
    assert claim_values(claims, 'realm_access.roles') == ('ops-admins',)
    assert claim_values(claims, 'https://example.com/roles') == ('ops-operators',)
    assert claim_values(claims, 'cognito:groups') == ('ops-viewers',)
    assert claim_values(claims, 'groups') == ('ops-admins',)
    assert claim_values(claims, 'number') == ()
    assert claim_values(claims, 'object') == ()
    assert claim_values(claims, 'realm_access.n') == ()
    assert claim_values(claims, 'missing') == ()
    assert claim_values(claims, 'realm_access.missing') == ()
    assert claim_values(claims, 'groups.deeper') == ()
    assert claim_values(claims, '') == ()


def test_role_map_parses_validates_and_falls_back_to_the_role_settings():
    # given
    pair = lambda value, role='viewer': {'value': value, 'role': role}
    refused = {
        'count': (json.dumps([pair(f'group-{i}') for i in range(MAP_MAX + 1)]), msg('sso.role_map.count', max=MAP_MAX)),
        'repeat': (json.dumps([pair('ops-admins', 'admin'), pair('ops-admins', 'viewer')]), msg('sso.role_map.repeat', value='ops-admins')),
        'role': (json.dumps([pair('ops-admins', 'root')]), msg('sso.role_map.role', entry=1)),
        'empty': (json.dumps([pair('')]), msg('sso.role_map.value', entry=1, max=VALUE_MAX)),
        'long': (json.dumps([pair('x' * (VALUE_MAX + 1))]), msg('sso.role_map.value', entry=1, max=VALUE_MAX)),
        'json': ('not json', msg('sso.role_map.json')),
        'shape': (json.dumps({'value': 'ops-admins', 'role': 'admin'}), msg('sso.role_map.json')),
        'pair': (json.dumps([['ops-admins', 'admin']]), msg('sso.role_map.pair', entry=1)),
    }
    biggest = json.dumps([pair(f'{i:03d}' + 'x' * (VALUE_MAX - 3), 'operator') for i in range(MAP_MAX)])

    # when
    texts = {}
    for name, (raw, _) in refused.items():
        with pytest.raises(tunables.Invalid) as e:
            parse_role_map(raw)
        texts[name] = e.value.text

    # then
    assert texts == {name: msg('sso.role_map_invalid', reason=reason) for name, (_, reason) in refused.items()}
    assert parse_role_map(ROLE_MAP) == PAIRS
    assert len(parse_role_map(biggest)) == MAP_MAX
    assert role_map(settings()) == (('SKOPAX_ADMIN', 'admin'), ('SKOPAX_OPERATOR', 'operator'))
    assert role_map(dataclasses.replace(settings(), operator_role=' ')) == (('SKOPAX_ADMIN', 'admin'),)
    assert role_map(dataclasses.replace(settings(), sso_role_map=ROLE_MAP)) == PAIRS
    assert role_map(dataclasses.replace(settings(), sso_role_map='[]')) == ()
    assert mapped_role(('ops-viewers', 'ops-admins'), PAIRS) == 'admin', 'several matches give the highest role'
    assert mapped_role(('OPS-ADMINS', 'ops-admin'), PAIRS) is None, 'matching is exact and case-sensitive'


def test_role_settings_are_checked_at_boot_and_in_admin(store: Store):
    # given
    pair = lambda value, role='viewer': {'value': value, 'role': role}
    biggest = json.dumps([pair(f'{i:03d}' + 'x' * (VALUE_MAX - 3), 'operator') for i in range(MAP_MAX)])
    admin = TestClient(create_app(settings(), store), headers=AUTH)

    # when
    big = admin.put('/api/admin/settings', json={'key': 'sso.role-map', 'value': biggest})
    bad = admin.put('/api/admin/settings', json={'key': 'sso.role-map', 'value': json.dumps([pair('ops-admins', 'root')])})
    claim = admin.put('/api/admin/settings', json={'key': 'sso.roles-claim', 'value': 'realm_access.roles'})
    as_admin = admin.put('/api/admin/settings', json={'key': 'sso.default-role', 'value': 'admin'})
    as_operator = admin.put('/api/admin/settings', json={'key': 'sso.default-role', 'value': 'operator'})
    live = admin.app.state.runtime.live.current()

    # then
    assert big.status_code == 200 and parse_role_map(live.sso_role_map) == parse_role_map(biggest)
    assert (bad.status_code, bad.json()['detail']) == (422, msg('sso.role_map_invalid', reason=msg('sso.role_map.role', entry=1)))
    assert claim.status_code == 200 and live.sso_roles_claim == 'realm_access.roles'
    assert (as_admin.status_code, as_admin.json()['detail']) == (422, 'Default role must be one of viewer, operator')
    assert as_operator.status_code == 200 and live.sso_default_role == 'operator'
    assert (load_settings(env={}).sso_roles_claim, load_settings(env={}).sso_role_map) == ('roles', '')
    assert parse_role_map(load_settings(env={'SSO_ROLE_MAP': ROLE_MAP, 'SSO_ROLES_CLAIM': 'groups'}).sso_role_map) == PAIRS
    with pytest.raises(ConfigurationError, match='SSO_ROLE_MAP'):
        load_settings(env={'SSO_ROLE_MAP': 'not json'})
    with pytest.raises(ConfigurationError, match='SSO_DEFAULT_ROLE'):
        load_settings(env={'SSO_DEFAULT_ROLE': 'admin'})


@pytest.mark.parametrize('resolved, values, admin_set, expected', [
    ('operator', ('ops-admins',), None, ('operator', 'resolver')),
    (None, ('ops-admins',), 'viewer', ('admin', 'claim')),
    (None, ('unmapped',), 'operator', ('operator', 'admin')),
    (None, (), None, ('viewer', 'default')),
    (None, ('ops-viewers',), 'admin', ('viewer', 'claim')),
])
def test_precedence_resolver_then_claim_then_admin_then_default(resolved, values, admin_set, expected):
    # when
    got = choose(resolved=resolved, mapped=mapped_role(values, PAIRS), admin_set=admin_set, default='viewer')

    # then
    assert got == expected


def test_sign_in_stores_the_role_and_where_it_came_from(store: Store):
    # given
    client = client_for(store)
    Users(store.db).create(CAROL, 'Carol', 'operator', source='sso')

    # when
    sign_in(client, ALICE, ['ops-operators'])
    alice = (Users(store.db).get(ALICE), me_role(client))
    sign_in(client, BOB)
    bob = (Users(store.db).get(BOB), me_role(client))
    carol = []
    for roles in ([], ['ops-viewers'], []):
        sign_in(client, CAROL, roles)
        carol.append((Users(store.db).get(CAROL), me_role(client)))

    # then
    assert (alice[0]['role'], alice[0]['roleFrom'], 'adminRole' in alice[0], alice[1]) == ('operator', 'claim', False, 'operator')
    assert (bob[0]['role'], bob[0]['roleFrom'], 'adminRole' in bob[0], bob[1]) == ('viewer', 'default', False, 'viewer')
    assert [(d['role'], d['roleFrom'], d.get('adminRole'), r) for d, r in carol] == [
        ('operator', 'admin', 'operator', 'operator'), ('viewer', 'claim', 'operator', 'viewer'), ('operator', 'admin', 'operator', 'operator')], 'a legacy role counts as Admin-set and comes back once the claim stops matching'


def test_claim_demotion_never_removes_the_last_active_admin(store: Store, answers, caplog):
    # given
    users = Users(store.db)
    users.set_disabled(ADMIN_EMAIL, True)
    client = client_for(store)
    sign_in(client, ALICE, ['ops-admins'])
    assert (users.get(ALICE)['role'], users.get(ALICE)['roleFrom'], users.active_admins()) == ('admin', 'claim', 1)

    # when
    with caplog.at_level(logging.WARNING, logger='skopax.users'):
        sign_in(client, ALICE)
        alone = (users.get(ALICE), me_role(client), kept_lines(caplog))
        users.create(BOB, 'Bob', 'admin')
        sign_in(client, ALICE)
        later = (users.get(ALICE), me_role(client))
        users.set_disabled(BOB, True)
        sign_in(client, CAROL, ['ops-admins'])
        answers[(CAROL, 'page')] = 'viewer'
        caplog.clear()
        sign_in(client, CAROL, ['ops-admins'])
        carol_alone = (users.get(CAROL), me_role(client), kept_lines(caplog))
        users.set_disabled(BOB, False)
        sign_in(client, CAROL, ['ops-admins'])
        carol_later = (users.get(CAROL), me_role(client))

    # then
    assert (alone[0]['role'], alone[0]['roleFrom']) == ('admin', 'claim'), 'the last active admin keeps the role'
    assert alone[2] == [msg('access.last_admin_kept', email=ALICE)]
    assert alone[1] == 'admin'
    assert (later[0]['role'], later[0]['roleFrom'], later[1]) == ('viewer', 'default', 'viewer')
    assert (carol_alone[0]['role'], carol_alone[0]['roleFrom']) == ('admin', 'claim'), 'the last active admin keeps the role'
    assert carol_alone[2] == [msg('access.last_admin_kept', email=CAROL)]
    assert carol_alone[1] == 'admin'
    assert (carol_later[0]['role'], carol_later[0]['roleFrom'], carol_later[1]) == ('viewer', 'resolver', 'viewer')


def test_two_admins_demoted_at_once_never_leave_zero(store: Store, monkeypatch, caplog):
    # given
    users = Users(store.db)
    users.set_disabled(ADMIN_EMAIL, True)
    signin = SignIn(users, dataclasses.replace(settings(), sso_role_map=ROLE_MAP), Resolvers(lambda: []))
    claims = lambda email, roles: {'iss': ISSUER, 'sub': email, 'email': email, 'roles': roles}
    for email in (ALICE, BOB):
        signin.page(claims(email, ['ops-admins']), ISSUER)
    assert users.active_admins() == 2
    barrier = threading.Barrier(2, timeout=5)
    count = Users.active_admins

    def counted(self):
        barrier.wait()
        return count(self)

    monkeypatch.setattr(Users, 'active_admins', counted)
    errors = []

    def demote(email):
        try:
            signin.page(claims(email, []), ISSUER)
        except Exception as e:
            errors.append(e)

    # when
    with caplog.at_level(logging.WARNING, logger='skopax.users'):
        threads = [threading.Thread(target=demote, args=(email,)) for email in (ALICE, BOB)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(10)
    monkeypatch.setattr(Users, 'active_admins', count)
    docs = [users.get(email) for email in (ALICE, BOB)]

    # then
    assert errors == []
    assert users.active_admins() >= 1
    assert len([d for d in docs if d['role'] != 'admin']) <= 1
    assert all(d['roleFrom'] == 'claim' for d in docs if d['role'] == 'admin'), 'a kept admin keeps roleFrom so a later sign-in can demote'
    assert sorted(kept_lines(caplog)) == sorted(msg('access.last_admin_kept', email=d['email']) for d in docs if d['role'] == 'admin')


def test_admin_cannot_edit_an_idp_managed_role(store: Store, monkeypatch):
    # given
    client = client_for(store)
    admin = admin_of(client)
    users = Users(store.db)
    sign_in(client, ALICE, ['ops-operators'])
    sign_in(client, BOB)
    sign_in(client, CAROL)
    before = users.col.find_one({'_id': ALICE})

    # when
    refused = admin.patch(f'/api/admin/users/{ALICE}', json={'role': 'admin'})
    refused_mixed = admin.patch(f'/api/admin/users/{ALICE}', json={'role': 'admin', 'disabled': True})
    after_refusal = users.col.find_one({'_id': ALICE})
    disabled = admin.patch(f'/api/admin/users/{ALICE}', json={'disabled': True})
    changed = admin.patch(f'/api/admin/users/{BOB}', json={'role': 'operator'})
    sign_in(client, BOB)
    bob_again = users.get(BOB)
    prepare = Users.prepare_change

    def claimed_meanwhile(self, *args, **kwargs):
        out = prepare(self, *args, **kwargs)
        self.col.update_one({'_id': CAROL}, {'$set': {'roleFrom': 'claim'}})
        return out

    monkeypatch.setattr(Users, 'prepare_change', claimed_meanwhile)
    raced = admin.patch(f'/api/admin/users/{CAROL}', json={'role': 'admin'})

    # then
    assert (refused.status_code, refused.json()['detail']) == (409, msg('access.role_from_idp'))
    assert (refused_mixed.status_code, refused_mixed.json()['detail']) == (409, msg('access.role_from_idp'))
    assert after_refusal == before
    assert disabled.status_code == 200 and users.get(ALICE)['disabled'] is True and users.get(ALICE)['role'] == 'operator'
    assert changed.status_code == 200
    assert (bob_again['role'], bob_again['adminRole'], bob_again['roleFrom']) == ('operator', 'operator', 'admin')
    assert (raced.status_code, raced.json()['detail']) == (409, msg('access.role_from_idp'))
    assert users.get(CAROL)['role'] == 'viewer'


def test_local_accounts_ignore_claims_and_resolvers(store: Store, answers):
    # given
    client = client_for(store)
    answers[(ADMIN_EMAIL, 'page')] = 'viewer'
    answers[(ALICE, 'page')] = 'viewer'
    before = Users(store.db).col.find_one({'_id': ADMIN_EMAIL})

    # when
    local = sign_in(client, ADMIN_EMAIL, ['ops-viewers'])
    local_doc = Users(store.db).col.find_one({'_id': ADMIN_EMAIL})
    sso = sign_in(client, ALICE, ['ops-admins'])

    # then
    assert local.status_code == 401 and SESSION_COOKIE not in local.cookies
    assert local_doc == before
    assert not seen(ADMIN_EMAIL, 'page')
    assert sso.status_code == 303 and seen(ALICE, 'page')
    assert (Users(store.db).get(ALICE)['role'], Users(store.db).get(ALICE)['roleFrom']) == ('viewer', 'resolver')


def test_a_failing_resolver_fails_closed_on_every_channel(store: Store, answers):
    # given
    users = Users(store.db)
    client = client_for(store)
    client.app.state.resolvers.timeout = 0.2
    operators = client.app.state.operators
    for email in (ALICE, BOB):
        users.upsert_sso(email, email.split('@')[0].title(), 'viewer')
        users.col.update_one({'_id': email}, {'$set': {'role': 'operator', 'roleFrom': 'admin', 'adminRole': 'operator'}})
    for email in ('ivy@example.com', 'jo@example.com', 'max@example.com'):
        users.create(email, email.split('@')[0].title(), 'operator', source='sso')
    page = {ALICE: 'raise', BOB: 'bad', CAROL: 'sleep'}
    slack = {'ivy@example.com': 'raise', 'jo@example.com': 'bad', 'max@example.com': 'sleep'}
    answers.update({(email, 'page'): a for email, a in page.items()} | {(email, 'slack'): a for email, a in slack.items()})
    before = {email: users.col.find_one({'_id': email}) for email in page}

    # when
    refused = {email: sign_in(client, email, ['ops-admins']) for email in page}
    after = {email: users.col.find_one({'_id': email}) for email in page}
    unknown = {}
    for email in slack:
        with pytest.raises(OperatorUnknown):
            operators.access_of(email)
        unknown[email] = True
    resolver_plugin.reset()
    recovered = operators.access_of('ivy@example.com')
    falls_through = sign_in(client, 'oscar.green@example.com', ['ops-operators'])

    # then
    assert {e: (r.status_code, msg('login.failed') in r.text, SESSION_COOKIE in r.cookies) for e, r in refused.items()} == {e: (503, True, False) for e in page}
    assert after == before, 'no insert, and no name, lastLogin or role write'
    assert unknown == {email: True for email in slack}
    assert recovered == ('ivy@example.com', True), 'the failure was not cached'
    assert falls_through.status_code == 303
    assert (users.get('oscar.green@example.com')['role'], users.get('oscar.green@example.com')['roleFrom']) == ('operator', 'claim')


def test_resolvers_run_in_load_order_and_see_the_channel(store: Store, answers):
    # given
    later_seen = []

    def later(user, channel):
        later_seen.append((user['email'], channel))
        return 'viewer'

    first = dataclasses.replace(resolver_plugin.PLUGIN)
    second = dataclasses.replace(minimal('aaa-resolver', 'Aaa resolver'), hooks=Hooks(role_resolver=later), after=('resolver-test',))
    client = client_for(store, plugins=(second, first))
    answers[(BOB, 'page')] = 'operator'

    # when
    sign_in(client, ALICE, ['ops-admins'])
    sign_in(client, BOB, ['ops-admins'])
    named = client.app.state.runtime.role_resolvers_named()

    # then
    assert [plugin_id for plugin_id, _ in named] == ['resolver-test', 'aaa-resolver']
    assert client.app.state.runtime.role_resolvers() == [hook for _, hook in named]
    assert (Users(store.db).get(ALICE)['role'], Users(store.db).get(ALICE)['roleFrom']) == ('viewer', 'resolver')
    assert (Users(store.db).get(BOB)['role'], Users(store.db).get(BOB)['roleFrom']) == ('operator', 'resolver')
    assert later_seen == [(ALICE, 'page')], 'the first answer that is not None wins'
    user, channel = resolver_plugin.SEEN[0]
    assert channel == 'page'
    assert set(user) == {'email', 'name', 'source', 'role', 'claims'}
    assert (user['email'], user['source'], user['role']) == (ALICE, 'sso', None)
    assert user['claims']['roles'] == ['ops-admins'] and user['claims']['sub'] == ALICE
    assert not {'access_token', 'id_token', 'token'} & set(user['claims'])
    assert json.loads(json.dumps(user['claims'])) == user['claims']


def test_slack_role_follows_the_resolver_then_the_stored_user(store: Store, answers):
    # given
    users = Users(store.db)
    users.create('ivy@example.com', 'Ivy', 'operator', source='sso')
    users.create('jo@example.com', 'Jo', 'admin', source='sso')
    users.set_disabled('jo@example.com', True)
    client = client_for(store)
    signin = client.app.state.signin
    operators = client.app.state.operators
    answers[('jo@example.com', 'slack')] = 'operator'
    answers[(CAROL, 'slack')] = 'operator'

    # when
    stored = signin.slack_role('ivy@example.com')
    disabled = signin.slack_role('jo@example.com')
    nobody = signin.slack_role('nobody@example.com')
    resolved = signin.slack_role(CAROL)

    # then
    assert stored == 'operator'
    assert disabled is None and not seen('jo@example.com', 'slack'), 'disabled first, before any resolver'
    assert nobody is None and seen('nobody@example.com', 'slack')
    assert resolved == 'operator'
    assert operators.access_of(CAROL) == (CAROL, True)
    assert operators.access_of('jo@example.com') == (None, False)
    assert operators.access_of('ivy@example.com') == ('ivy@example.com', True)
    assert operators.access_of('nobody@example.com') == (None, False)


def test_slack_keeps_a_local_account_off_the_resolvers(store: Store, answers):
    # given
    users = Users(store.db)
    local = {'ivy@example.com': 'viewer', 'jo@example.com': 'raise', 'max@example.com': 'sleep'}
    for email in local:
        users.create(email, email.split('@')[0].title(), 'operator')
    users.create(CAROL, 'Carol', 'operator', source='sso')
    client = client_for(store)
    client.app.state.resolvers.timeout = 0.2
    signin, operators = client.app.state.signin, client.app.state.operators
    answers.update({(email, 'slack'): answer for email, answer in local.items()} | {(CAROL, 'slack'): 'viewer'})

    # when
    roles_seen = {email: signin.slack_role(email) for email in local}
    access = {email: operators.access_of(email) for email in local}
    sso_role = signin.slack_role(CAROL)

    # then
    assert roles_seen == {email: 'operator' for email in local}
    assert access == {email: (email, True) for email in local}
    assert not any(seen(email, 'slack') for email in local), 'a local account never reaches a resolver'
    assert sso_role == 'viewer' and seen(CAROL, 'slack'), 'the same resolver answers for an SSO user'


def test_a_disabled_sso_user_is_refused_before_any_resolver(store: Store, answers):
    # given
    users = Users(store.db)
    users.create(ALICE, 'Alice', 'viewer', source='sso')
    users.set_disabled(ALICE, True)
    users.create(BOB, 'Bob', 'viewer', source='sso')
    client = client_for(store)
    answers[(ALICE, 'page')] = 'operator'
    answers[(BOB, 'page')] = 'operator'
    before = users.col.find_one({'_id': ALICE})

    # when
    refused = sign_in(client, ALICE)
    allowed = sign_in(client, BOB)

    # then
    assert refused.status_code == 401 and msg('login.failed') in refused.text and SESSION_COOKIE not in refused.cookies
    assert users.col.find_one({'_id': ALICE}) == before
    assert not seen(ALICE, 'page'), 'disabled first, before any resolver'
    assert allowed.status_code == 303 and seen(BOB, 'page') and users.get(BOB)['role'] == 'operator'


def test_a_non_bmp_role_map_of_full_size_keeps_sign_in_working(store: Store):
    # given
    wide = chr(0x1F600)
    pairs = [{'value': f'{i:02d}' + wide * (VALUE_MAX - 2), 'role': 'operator'} for i in range(MAP_MAX)]
    text = json.dumps(pairs, ensure_ascii=False)
    last = pairs[-1]['value']
    admin = TestClient(create_app(settings(), store), headers=AUTH)

    # when
    saved = admin.put('/api/admin/settings', json={'key': 'sso.role-map', 'value': text})
    stored = admin.app.state.runtime.live.current().sso_role_map
    booted = load_settings(env={'SSO_ROLE_MAP': text}).sso_role_map
    client = client_for(store, sso_role_map=booted)
    mapped = sign_in(client, ALICE, [last])

    # then
    assert saved.status_code == 200 and parse_role_map(stored) == parse_role_map(text)
    assert len(booted) <= len(text) + 2 * MAP_MAX
    assert mapped.status_code == 303 and (Users(store.db).get(ALICE)['role'], Users(store.db).get(ALICE)['roleFrom']) == ('operator', 'claim')


def test_an_unexpected_role_failure_is_the_generic_503_not_a_500(store: Store, monkeypatch):
    # given
    client = client_for(store)

    def broken(settings):
        raise roles.Invalid('a stored map that no longer parses')

    def no_hooks():
        raise RuntimeError('the plug-in runtime failed')

    # when
    monkeypatch.setattr('skopax.signin.role_map', broken)
    bad_map = sign_in(client, ALICE, ['ops-admins'])
    monkeypatch.undo()
    client.app.state.resolvers.hooks = no_hooks
    bad_hooks = sign_in(client, BOB, ['ops-admins'])
    with pytest.raises(ResolverError):
        client.app.state.resolvers.resolve({'email': CAROL}, 'slack')

    # then
    for res in (bad_map, bad_hooks):
        assert res.status_code == 503 and msg('login.failed') in res.text and SESSION_COOKIE not in res.cookies
    assert Users(store.db).get(ALICE) is None and Users(store.db).get(BOB) is None, 'nothing is written before the role is known'


def test_a_failed_role_lookup_logs_one_warning_that_names_the_plugin_and_still_answers_503(store: Store, answers, caplog):
    # given
    client = client_for(store)
    answers[('ivy@example.com', 'slack')] = 'raise'
    answers[('jo@example.com', 'slack')] = 'raise'

    # when
    with caplog.at_level(logging.WARNING):
        down = admin_of(client).get('/api/operators/check', params={'email': 'ivy@example.com'})
        with pytest.raises(OperatorUnknown):
            client.app.state.operators.access_of('jo@example.com')
    warned = [r for r in caplog.records if r.levelno == logging.WARNING]

    # then
    assert (down.status_code, down.json()['detail']) == (503, 'role lookup failed: ResolverError')
    assert [(r.name, r.getMessage()) for r in warned] == [('skopax.operators', 'the role resolver of plug-in resolver-test failed: RuntimeError')] * 2


def test_one_warning_when_resolver_calls_wait_for_a_free_worker(caplog):
    # given
    release = threading.Event()
    for _ in range(50):
        if roles.busy() == 0:
            break
        threading.Event().wait(0.1)
    blocked = Resolvers(lambda: [('slow', lambda user, channel: release.wait(5) and None)], timeout=5)
    waiting = Resolvers(lambda: [('quick', lambda user, channel: None)], timeout=0.2)
    holders = [threading.Thread(target=blocked.resolve, args=({'email': ALICE}, 'slack')) for _ in range(roles.WORKERS)]

    # when
    with caplog.at_level(logging.WARNING, logger='skopax.roles'):
        free = waiting.resolve({'email': BOB}, 'slack')
        for t in holders:
            t.start()
        for _ in range(50):
            if roles.busy() >= roles.WORKERS:
                break
            threading.Event().wait(0.05)
        outcomes = []
        for _ in range(2):
            try:
                outcomes.append(waiting.resolve({'email': CAROL}, 'slack'))
            except ResolverError as e:
                outcomes.append(e.plugin)
        release.set()
        for t in holders:
            t.join(5)
    full = [r.getMessage() for r in caplog.records if r.getMessage() == msg('roles.pool_full', workers=roles.WORKERS)]

    # then
    assert free is None
    assert outcomes == ['quick', 'quick'], 'a queued call counts its wait against the timeout and fails closed'
    assert len(full) == 1


def test_a_refused_submit_does_not_count_a_busy_worker(monkeypatch):
    # given
    before = roles.busy()

    def refuse(*args, **kwargs):
        raise RuntimeError('cannot schedule new futures after shutdown')

    monkeypatch.setattr(roles.EXECUTOR, 'submit', refuse)

    # when
    with pytest.raises(RuntimeError):
        roles.submit(lambda user, channel: None, {'email': 'alice@example.com'}, 'page')

    # then
    assert roles.busy() == before


def claims_of(email: str, roles: list[str] | None = None) -> dict:
    return {'iss': ISSUER, 'sub': email, 'email': email, 'email_verified': True, 'name': email.split('@')[0].title(), 'roles': roles or []}


def resolved_ago(store: Store, email: str, minutes: float) -> None:
    Users(store.db).col.update_one({'_id': email}, {'$set': {'resolvedAtTs': int(time.time() - minutes * 60)}})


def warnings_of(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]


DOWN = 'the role resolver of plug-in resolver-test failed: RuntimeError'


def test_a_resolver_that_fails_once_is_asked_again_and_its_second_answer_counts(caplog):
    # given
    calls: list[str] = []

    def flaky(user, channel):
        calls.append(channel)
        if len(calls) == 1:
            raise ConnectionError('a blip')
        return 'operator'

    resolvers = Resolvers(lambda: [('crm', flaky)], timeout=5)

    # when
    with caplog.at_level(logging.WARNING):
        got = resolvers.resolve({'email': ALICE}, 'page')

    # then
    assert got == 'operator' and calls == ['page', 'page']
    assert warnings_of(caplog) == []


def test_a_resolver_that_fails_after_half_its_time_is_not_asked_again():
    # given
    now = [100.0]
    calls: list[int] = []

    def slow_failure(user, channel):
        calls.append(1)
        now[0] += 3
        raise ConnectionError('down')

    resolvers = Resolvers(lambda: [('crm', slow_failure)], timeout=5, clock=lambda: now[0])
    twice = Resolvers(lambda: [('crm', lambda user, channel: calls.append(2) or 1 / 0)], timeout=5, clock=lambda: now[0])

    # when
    with pytest.raises(ResolverError) as slow:
        resolvers.resolve({'email': ALICE}, 'page')
    with pytest.raises(ResolverError) as quick:
        twice.resolve({'email': ALICE}, 'page')

    # then
    assert calls == [1, 2, 2], 'a quick failure is asked once more, never more'
    assert (slow.value.plugin, slow.value.why, quick.value.why) == ('crm', 'ConnectionError', 'ZeroDivisionError')


def test_a_down_resolver_signs_in_with_the_last_resolved_role_within_the_limit_on_the_page_and_mcp(store: Store, answers, caplog):
    # given
    client = client_for(store)
    signin = client.app.state.signin
    answers[(ALICE, 'page')] = 'operator'
    signin.page(claims_of(ALICE, ['ops-admins']), ISSUER)
    answers[(ALICE, 'page')] = 'raise'
    answers[(ALICE, 'mcp')] = 'raise'
    resolved_ago(store, ALICE, 12.5)
    before = Users(store.db).get(ALICE)

    # when
    with caplog.at_level(logging.WARNING):
        kept = signin.page(claims_of(ALICE, ['ops-admins']), ISSUER)
        caught = warnings_of(caplog)
        caplog.clear()
        mcp_user = signin.mcp(claims_of(ALICE, ['ops-admins']), ISSUER)
        mcp_caught = warnings_of(caplog)

    # then
    assert (before['role'], before['roleFrom']) == ('operator', 'resolver')
    assert (kept['role'], kept['roleFrom'], kept['resolvedAtTs']) == ('operator', 'resolver', before['resolvedAtTs']), 'a fallback never renews the last known role'
    assert mcp_user.role == 'operator'
    assert caught == mcp_caught == [f'{DOWN}; using the last known role from 12 min ago']


def test_a_down_resolver_refuses_beyond_the_limit_a_new_person_and_with_the_limit_at_zero(store: Store, answers, caplog):
    # given
    client = client_for(store)
    closed = client_for(store, roles_stale_max_minutes=0)
    answers[(ALICE, 'page')] = 'operator'
    answers[(BOB, 'page')] = 'operator'
    client.app.state.signin.page(claims_of(ALICE), ISSUER)
    client.app.state.signin.page(claims_of(BOB), ISSUER)
    for email in (ALICE, BOB, CAROL):
        answers[(email, 'page')] = 'raise'
        answers[(email, 'mcp')] = 'raise'
    resolved_ago(store, ALICE, 61)
    resolved_ago(store, BOB, 1)
    tries = ((client, ALICE), (client, CAROL), (closed, BOB))

    # when
    refused = []
    with caplog.at_level(logging.WARNING):
        for c, email in tries:
            for channel in (c.app.state.signin.page, c.app.state.signin.mcp):
                with pytest.raises(Refused) as e:
                    channel(claims_of(email), ISSUER)
                refused.append(e.value.status)

    # then
    assert refused == [503] * 6
    assert warnings_of(caplog) == [DOWN] * 6
    assert Users(store.db).get(CAROL) is None, 'a new person is not created while the resolver is down'


def test_the_stale_limit_is_a_general_setting_from_0_to_1440_minutes(store: Store):
    # given
    live = tunables.Live(settings(), tunables.TunableStore(store.db))

    # when
    item = {e['key']: e for e in live.entries()}['roles.stale-max-minutes']
    loaded = load_settings(env={})

    # then
    assert (item['group'], item['kind'], item['low'], item['high'], item['value']) == ('General', 'int', 0, 1440, '60')
    assert loaded.roles_stale_max_minutes == 60
    with pytest.raises(tunables.Invalid):
        live.set('roles.stale-max-minutes', '1441', ALICE)


def test_operators_keep_the_last_good_verdict_within_the_limit(caplog):
    # given
    now = [1_792_224_000.0]
    down = {'on': False}

    def lookup(email: str) -> bool:
        if down['on']:
            raise ResolverError('crm', 'RoleLookupDown')
        return True

    base = dataclasses.replace(settings(), slack_operator_cache_s=600, roles_stale_max_minutes=60)
    ops = Operators(lookup, None, base, clock=lambda: now[0])
    closed = Operators(lookup, None, dataclasses.replace(base, roles_stale_max_minutes=0), clock=lambda: now[0])
    first, closed_first = ops.access_of(ALICE), closed.access_of(ALICE)
    down['on'] = True
    now[0] += 12 * 60

    # when
    with caplog.at_level(logging.WARNING):
        kept = ops.access_of(ALICE)
        with pytest.raises(OperatorUnknown):
            closed.access_of(ALICE)
        now[0] += 50 * 60
        with pytest.raises(OperatorUnknown):
            ops.access_of(ALICE)

    # then
    assert first == closed_first == kept == (ALICE, True)
    assert warnings_of(caplog) == ['the role resolver of plug-in crm failed: RoleLookupDown; using the last known role from 12 min ago',
                                   'the role resolver of plug-in crm failed: RoleLookupDown', 'the role resolver of plug-in crm failed: RoleLookupDown']


def test_the_stale_limit_saved_in_admin_applies_at_once_to_sign_in_and_slack(store: Store, answers, caplog):
    # given
    client = client_for(store)
    signin, operators, live = client.app.state.signin, client.app.state.operators, client.app.state.live
    answers[(ALICE, 'page')] = 'operator'
    answers[(BOB, 'slack')] = 'operator'
    signin.page(claims_of(ALICE), ISSUER)
    assert operators.access_of(BOB) == (BOB, True)
    operators._verdicts[BOB] = (time.time() - 20 * 60, (BOB, True))
    answers[(ALICE, 'page')] = 'raise'
    answers[(BOB, 'slack')] = 'raise'
    resolved_ago(store, ALICE, 20)

    # when
    kept = signin.page(claims_of(ALICE), ISSUER)['role'], operators.access_of(BOB)
    live.set('roles.stale-max-minutes', '0', ADMIN_EMAIL)
    with pytest.raises(Refused):
        signin.page(claims_of(ALICE), ISSUER)
    with pytest.raises(OperatorUnknown):
        operators.access_of(BOB)

    # then
    assert kept == ('operator', (BOB, True))


def test_an_mcp_sign_in_renews_the_last_known_role_only_when_the_resolver_confirms_the_stored_one(store: Store, answers, caplog):
    # given
    client = client_for(store)
    signin = client.app.state.signin
    answers[(ALICE, 'page')] = 'operator'
    signin.page(claims_of(ALICE), ISSUER)
    resolved_ago(store, ALICE, 90)
    answers[(ALICE, 'mcp')] = 'operator'

    # when
    mcp_user = signin.mcp(claims_of(ALICE), ISSUER)
    renewed = Users(store.db).get(ALICE)
    answers[(ALICE, 'page')] = 'raise'
    with caplog.at_level(logging.WARNING):
        kept = signin.page(claims_of(ALICE), ISSUER)

    # then
    assert mcp_user.role == 'operator'
    assert time.time() - renewed['resolvedAtTs'] < 60 and (renewed['role'], renewed['roleFrom']) == ('operator', 'resolver')
    assert (kept['role'], kept['roleFrom']) == ('operator', 'resolver')


def test_an_mcp_answer_that_differs_from_the_stored_role_leaves_the_stamp_so_a_down_resolver_gives_no_old_admin(store: Store, answers, caplog):
    # given
    client = client_for(store)
    signin = client.app.state.signin
    answers[(ALICE, 'page')] = 'admin'
    signin.page(claims_of(ALICE), ISSUER)
    resolved_ago(store, ALICE, 90)
    before = Users(store.db).get(ALICE)
    answers[(ALICE, 'mcp')] = 'viewer'

    # when
    mcp_user = signin.mcp(claims_of(ALICE), ISSUER)
    after_mcp = Users(store.db).get(ALICE)
    answers[(ALICE, 'page')] = 'raise'
    with caplog.at_level(logging.WARNING), pytest.raises(Refused) as e:
        signin.page(claims_of(ALICE), ISSUER)

    # then
    assert (before['role'], before['roleFrom']) == ('admin', 'resolver')
    assert mcp_user.role == 'viewer'
    assert (after_mcp['role'], after_mcp['roleFrom'], after_mcp['resolvedAtTs']) == ('admin', 'resolver', before['resolvedAtTs']), 'MCP never writes the role and leaves the stamp'
    assert e.value.status == 503
    assert Users(store.db).get(ALICE)['resolvedAtTs'] == before['resolvedAtTs']


def test_operators_keep_the_last_verdict_only_for_a_resolver_failure(caplog):
    # given
    now = [1_792_224_000.0]
    down = {'on': False}

    def lookup(email: str) -> bool:
        if down['on']:
            raise KeyError('a bug in the lookup')
        return True

    ops = Operators(lookup, None, dataclasses.replace(settings(), slack_operator_cache_s=600, roles_stale_max_minutes=60), clock=lambda: now[0])
    first = ops.access_of(ALICE)
    down['on'] = True
    now[0] += 12 * 60

    # when
    with caplog.at_level(logging.WARNING), pytest.raises(OperatorUnknown):
        ops.access_of(ALICE)

    # then
    assert first == (ALICE, True)
    assert warnings_of(caplog) == ['role lookup failed: KeyError']


@pytest.mark.parametrize('raised', [TimeoutError('read timed out'), __import__('socket').timeout('timed out')])
def test_a_resolver_that_raises_its_own_timeout_is_asked_again_like_any_error(caplog, raised):
    # given
    calls: list[str] = []

    def flaky(user, channel):
        calls.append(channel)
        if len(calls) == 1:
            raise raised
        return 'operator'

    resolvers = Resolvers(lambda: [('crm', flaky)], timeout=5)
    always = Resolvers(lambda: [('crm', lambda user, channel: calls.append('again') or (_ for _ in ()).throw(TimeoutError('read timed out')))], timeout=5)

    # when
    got = resolvers.resolve({'email': ALICE}, 'page')
    with pytest.raises(ResolverError) as e:
        always.resolve({'email': ALICE}, 'page')

    # then
    assert got == 'operator' and calls == ['page', 'page', 'again', 'again']
    assert e.value.why == 'TimeoutError', 'a hook that keeps timing out on its own is named by its error, not as no answer'


def test_a_resolver_that_does_not_answer_in_time_is_not_asked_again():
    # given
    gate = threading.Event()
    calls: list[int] = []

    def hangs(user, channel):
        calls.append(1)
        gate.wait(5)
        return 'admin'

    resolvers = Resolvers(lambda: [('crm', hangs)], timeout=0.2)

    # when
    with pytest.raises(ResolverError) as e:
        resolvers.resolve({'email': ALICE}, 'page')
    gate.set()

    # then
    assert calls == [1] and e.value.why == 'no answer within 0.2 s'
