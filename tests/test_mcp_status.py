import time

from fastapi.testclient import TestClient
from pymongo.errors import AutoReconnect

from skopax import clock as clocks
from skopax.app import create_app
from skopax.store import Store
from skopax.users import Users
from tests.conftest import AUTH, settings
from tests.sso_kit import FakeIdp, app_client
from tests.test_mcp_identity import ALICE, BOB, CAROL, Clock, mcp_settings, mint, rpc

SSO = '/api/admin/sso'
DAY = 86400


def status(client: TestClient) -> dict:
    res = client.get(SSO, headers=AUTH)
    assert res.status_code == 200, res.text
    return res.json()['mcp']


def test_the_status_says_not_set_up_and_names_the_first_missing_piece(store: Store):
    # given
    client = TestClient(create_app(settings(), store), base_url='https://testserver')

    # when
    with client as c:
        got = status(c)

    # then
    assert (got['state'], got['missing'], got['ssoOn'], got['people'], got['last']) == ('not-set-up', 'sso', False, 0, None)


def test_a_set_up_install_with_no_mcp_use_waits_and_shows_which_values_are_defaults(store: Store):
    # given
    idp = FakeIdp()
    client = app_client(store, idp, mcp_settings(idp))

    # when
    with client as c:
        got = status(c)

    # then
    assert (got['state'], got['missing'], got['ssoOn'], got['on'], got['mounted'], got['people'], got['last']) == ('waiting', None, True, True, True, 0, None)
    assert got['values']['client-id'] == {'value': 'skopax-mcp', 'default': True}
    assert got['values']['audience'] == {'value': 'skopax-mcp', 'default': True}
    assert got['values']['issuer']['default'] is True and got['values']['issuer']['value']


def test_the_status_counts_people_of_the_last_week_and_names_the_last_one(store: Store):
    # given
    idp = FakeIdp()
    users = Users(store.db)
    users.upsert_sso(ALICE, 'Alice Reed', 'viewer')
    users.upsert_sso(BOB, 'Bob Stone', 'viewer')
    clock = Clock()
    client = app_client(store, idp, mcp_settings(idp), clock=clock)
    token = mint(idp, email=ALICE)
    other = mint(idp, email=BOB)

    # when
    with client as c:
        store.db['mcp_use'].insert_one({'_id': 'old@example.com', 'at': '2020-01-01T00:00:00Z'})
        assert rpc(c, 'tools/list', token).status_code == 200
        clock.now += 120
        assert rpc(c, 'tools/list', other).status_code == 200
        got = status(c)
        raw = c.get(SSO, headers=AUTH).text

    # then
    assert (got['state'], got['people'], got['days']) == ('connected', 2, 7)
    assert got['last']['by'] == 'Bob Stone' and got['last']['at'].endswith('Z')
    assert token not in raw and other not in raw


def test_the_status_is_for_admins_only(store: Store):
    # given
    idp = FakeIdp()
    client = app_client(store, idp, mcp_settings(idp))

    # when
    with client as c:
        anonymous = c.get(SSO)
        wrong = c.get(SSO, headers={'Authorization': 'Bearer nope'})

    # then
    assert anonymous.status_code in (401, 403) and wrong.status_code in (401, 403)
    assert 'mcp' not in anonymous.text and 'mcp' not in wrong.text.replace('"detail"', '')


def test_the_last_use_write_happens_at_most_once_a_minute_per_person(store: Store):
    # given
    idp = FakeIdp()
    Users(store.db).upsert_sso(ALICE, 'Alice Reed', 'viewer')
    clock = Clock()
    client = app_client(store, idp, mcp_settings(idp), clock=clock)

    # when
    with client as c:
        rpc(c, 'tools/list', mint(idp, email=ALICE, jti='1'))
        first = store.db['mcp_use'].find_one({'_id': ALICE})['at']
        clock.now += 31
        rpc(c, 'tools/list', mint(idp, email=ALICE, jti='2'))
        held = store.db['mcp_use'].find_one({'_id': ALICE})['at']
        clock.now += 31
        rpc(c, 'tools/list', mint(idp, email=ALICE, jti='3'))
        later = store.db['mcp_use'].find_one({'_id': ALICE})['at']

    # then
    assert held == first and later > first
    assert store.db['mcp_use'].count_documents({}) == 1


def people(client: TestClient) -> dict:
    res = client.get('/api/admin/users', headers=AUTH)
    assert res.status_code == 200, res.text
    return {u['email']: u for u in res.json()}


def test_a_person_who_only_used_mcp_is_listed_as_mcp_only_and_a_page_user_as_both(store: Store):
    # given
    idp = FakeIdp()
    users = Users(store.db)
    users.upsert_sso(BOB, 'Bob Stone', 'viewer')
    users.col.update_one({'_id': BOB}, {'$set': {'lastLogin': '2026-01-01T00:00:00Z'}})
    store.add_activity('access', 'Bob Stone', 'renamed themselves')
    users.create(CAROL, 'Carol', 'viewer', 'a-long-enough-password')
    client = app_client(store, idp, mcp_settings(idp))

    # when
    with client as c:
        assert rpc(c, 'tools/list', mint(idp, email=ALICE, name='Alice Reed')).status_code == 200
        assert rpc(c, 'tools/list', mint(idp, email=ALICE, name='Alice Reed', jti='2')).status_code == 200
        assert rpc(c, 'tools/list', mint(idp, email=BOB)).status_code == 200
        listed = people(c)

    # then
    assert listed[ALICE]['mcp']['only'] is True and listed[ALICE]['mcp']['at'].endswith('Z')
    assert listed[BOB]['mcp']['only'] is False and listed[BOB]['lastLogin'] == '2026-01-01T00:00:00Z'
    assert listed[CAROL]['mcp'] is None


def test_a_page_login_after_mcp_use_turns_mcp_only_into_page_and_mcp(store: Store):
    # given
    idp = FakeIdp()
    client = app_client(store, idp, mcp_settings(idp))

    # when
    with client as c:
        assert rpc(c, 'tools/list', mint(idp, email=ALICE, name='Alice Reed')).status_code == 200
        before = people(c)[ALICE]['mcp']['only']
        Users(store.db).col.update_one({'_id': ALICE}, {'$set': {'lastLogin': '2030-01-01T00:00:00Z'}})
        after = people(c)[ALICE]['mcp']['only']

    # then
    assert (before, after) == (True, False)


def test_the_linked_local_account_gets_its_mcp_use_recorded(store: Store):
    # given
    idp = FakeIdp()
    users = Users(store.db)
    users.create(ALICE, 'Alice', 'admin', 'a-long-enough-password')
    users.set_sso_link(ALICE, True, 'admin@example.com')
    users.bind_sso(ALICE, idp.issuer, 'alice-subject', time.time())
    users.touch_login(ALICE)
    store.add_activity('admin', ALICE, 'changed a setting')
    client = app_client(store, idp, mcp_settings(idp))

    # when
    with client as c:
        res = rpc(c, 'tools/list', mint(idp, email=ALICE, sub='alice-subject', email_verified=True))
        got = status(c)
        listed = people(c)

    # then
    assert res.status_code == 200
    assert store.db['mcp_use'].find_one({'_id': ALICE})['at']
    assert (got['people'], got['last']['by']) == (1, 'Alice')
    assert listed[ALICE]['mcp']['only'] is False


def old_login(store: Store, email: str, name: str) -> Users:
    users = Users(store.db)
    users.upsert_sso(email, name, 'viewer')
    users.col.update_one({'_id': email}, {'$set': {'lastLogin': '2026-01-01T00:00:00Z'}})
    return users


def test_a_person_with_an_old_login_and_no_evidence_at_all_is_undecided_until_a_page_login(store: Store):
    # given
    idp = FakeIdp()
    users = old_login(store, BOB, 'Bob Stone')
    client = app_client(store, idp, mcp_settings(idp))

    # when
    with client as c:
        assert rpc(c, 'tools/list', mint(idp, email=BOB)).status_code == 200
        first = people(c)[BOB]['mcp']['only']
        users.col.update_one({'_id': BOB}, {'$set': {'lastLogin': '2026-06-01T00:00:00Z'}})
        after = people(c)[BOB]['mcp']['only']

    # then
    assert (first, after) == (None, False)


def test_a_person_with_an_old_login_and_only_mcp_made_actions_is_mcp_only(store: Store):
    # given
    idp = FakeIdp()
    old_login(store, BOB, 'Bob Stone')
    store.add_action({'type': 'take', 'key': 'k1', 'target': 'k1', 'requestedBy': 'Bob Stone', 'via': 'mcp', 'label': 'Take via MCP'})
    store.add_action({'type': 'ask', 'source': 'chat', 'key': 'ask', 'target': 'chat:1', 'requestedBy': 'Bob Stone', 'via': 'mcp'})
    store.add_action({'type': 'ask', 'source': 'slack', 'key': 'ask', 'target': 'x', 'requestedBy': 'Bob Stone'})
    client = app_client(store, idp, mcp_settings(idp))

    # when
    with client as c:
        assert rpc(c, 'tools/list', mint(idp, email=BOB)).status_code == 200
        listed = people(c)

    # then
    assert listed[BOB]['mcp']['only'] is True


def test_a_page_action_or_a_page_chat_ask_by_the_person_means_page_and_mcp(store: Store):
    # given
    idp = FakeIdp()
    old_login(store, BOB, 'Bob Stone')
    old_login(store, CAROL, 'Carol Cole')
    store.add_action({'type': 'take', 'key': 'k1', 'target': 'k1', 'requestedBy': 'Bob Stone', 'label': 'Take'})
    store.add_action({'type': 'ask', 'source': 'chat', 'key': 'ask', 'target': 'chat:2', 'requestedBy': CAROL})
    client = app_client(store, idp, mcp_settings(idp))

    # when
    with client as c:
        assert rpc(c, 'tools/list', mint(idp, email=BOB)).status_code == 200
        assert rpc(c, 'tools/list', mint(idp, email=CAROL)).status_code == 200
        listed = people(c)

    # then
    assert listed[BOB]['mcp']['only'] is False and listed[CAROL]['mcp']['only'] is False


def test_a_first_seed_from_a_racing_call_is_not_overwritten(store: Store):
    # given
    users = Users(store.db, now=Clock())
    users.upsert_sso(BOB, 'Bob Stone', 'viewer')
    store.db['mcp_use'].insert_one({'_id': BOB, 'at': clocks.stamp(users.now()), 'seed': 'first-wins'})

    # when
    users.record_mcp(BOB, seeded=True)

    # then
    assert store.db['mcp_use'].find_one({'_id': BOB})['seed'] == 'first-wins'


def test_a_person_with_a_login_from_before_the_record_and_a_page_action_is_page_and_mcp(store: Store):
    # given
    idp = FakeIdp()
    users = Users(store.db)
    users.upsert_sso(BOB, 'Bob Stone', 'viewer')
    users.col.update_one({'_id': BOB}, {'$set': {'lastLogin': '2026-01-01T00:00:00Z'}})
    store.add_activity('access', 'Bob Stone', 'renamed themselves')
    users.upsert_sso(CAROL, 'Carol Cole', 'viewer')
    users.col.update_one({'_id': CAROL}, {'$set': {'lastLogin': '2026-01-01T00:00:00Z'}})
    store.add_activity('admin', CAROL, 'changed a setting')
    client = app_client(store, idp, mcp_settings(idp))

    # when
    with client as c:
        assert rpc(c, 'tools/list', mint(idp, email=BOB)).status_code == 200
        assert rpc(c, 'tools/list', mint(idp, email=CAROL)).status_code == 200
        listed = people(c)

    # then
    assert listed[BOB]['mcp']['only'] is False and listed[CAROL]['mcp']['only'] is False


def test_a_failing_use_record_never_refuses_mcp_access(store: Store, monkeypatch, caplog):
    # given
    idp = FakeIdp()
    monkeypatch.setattr(Users, 'record_mcp', lambda self, *a, **k: (_ for _ in ()).throw(AutoReconnect('down')))
    client = app_client(store, idp, mcp_settings(idp))

    # when
    with client as c:
        res = rpc(c, 'tools/list', mint(idp, email=ALICE, name='Alice Reed'))

    # then
    assert res.status_code == 200
    assert [r.getMessage() for r in caplog.records if 'AutoReconnect' in r.getMessage()]


def test_a_missing_seed_follows_a_login_the_server_wrote_inside_the_minute(store: Store):
    # given
    users = Users(store.db, now=Clock())
    users.upsert_sso(BOB, 'Bob Stone', 'viewer')
    users.col.update_one({'_id': BOB}, {'$set': {'lastLogin': None}})

    # when
    users.record_mcp(BOB)
    users.col.update_one({'_id': BOB}, {'$set': {'lastLogin': '2031-01-01T00:00:00Z'}})
    users.record_mcp(BOB, seeded=True)

    # then
    assert store.db['mcp_use'].find_one({'_id': BOB})['seed'] == '2031-01-01T00:00:00Z'
