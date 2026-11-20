import dataclasses
import hashlib
import json
import logging

import pytest
from fastapi.testclient import TestClient
from pymongo.errors import AutoReconnect, OperationFailure

from tests.conftest import ADMIN_EMAIL, AUTH, settings
from tests.test_autosend import KEY, seed
from tests.test_chat import chat_action
from tests.sso_kit import FakeIdp, app_client, idp_settings, log_in
from tests.test_auth import Clock
from skopax import clock, history, notices
from skopax.apitokens import ApiTokens, TokenNameError
from skopax.app import create_app
from skopax.actors import is_token, token_label
from skopax.messages import msg
from skopax.sso import SsoHttp
from skopax.store import AUTO_SEND_BY, Store
from skopax.tunables import SESSIONS_AFTER_KEY
from skopax.users import Users, check_password

PASSWORD = 'correct horse battery'
GENERIC = 'missing or wrong token'
LOW = "this token's role is too low"


def bearer(token: str) -> dict:
    return {'Authorization': f'Bearer {token}'}


def make_client(store: Store, **over) -> TestClient:
    return TestClient(create_app(dataclasses.replace(settings(), **over), store), base_url='https://testserver', follow_redirects=False)


def add_user(store: Store, email: str, role: str) -> None:
    Users(store.db).create(email, email, role, PASSWORD)


def sign_in(client: TestClient, email: str) -> None:
    res = client.post('/login', data={'email': email, 'password': PASSWORD})
    assert res.status_code == 303


def admin_client(store: Store, **over) -> TestClient:
    Users(store.db).set_password(ADMIN_EMAIL, PASSWORD)
    client = make_client(store, **over)
    sign_in(client, ADMIN_EMAIL)
    return client


def test_token_round_trip_and_hash_only_stored(store: Store):
    # given
    tokens = ApiTokens(store.db)

    # when
    token, doc = tokens.create('ci', 'operator', ADMIN_EMAIL)
    found = tokens.resolve(token)
    stored = store.db['api_tokens'].find_one({'_id': doc['id']})
    listed = tokens.list()

    # then
    assert token.startswith('skopax_' + doc['id'] + '_')
    assert (found['name'], found['role'], found['createdBy']) == ('ci', 'operator', ADMIN_EMAIL)
    assert stored['hash'] == hashlib.sha256(token.encode()).hexdigest()
    assert token not in repr(stored) and not any('hash' in row for row in listed)
    assert [(r['name'], r['role'], r['revoked']) for r in listed] == [('ci', 'operator', False)]
    with make_client(store) as c:
        assert c.put('/api/state', json={'anomalies': []}, headers=bearer(token)).status_code == 200


def test_revoked_and_garbled_tokens_fail_alike(store: Store):
    # given
    tokens = ApiTokens(store.db)
    token, doc = tokens.create('ci', 'operator', ADMIN_EMAIL)
    tokens.revoke(doc['id'], ADMIN_EMAIL)
    live, _ = tokens.create('live', 'operator', ADMIN_EMAIL)
    candidates = [token, live + 'x', 'skopax_deadbeef_' + 'a' * 32, 'skopax_zz', 'skopax_', 'nope', 'wa' + 't_deadbeef_' + 'a' * 32]

    # when
    with make_client(store) as c:
        seen = [c.put('/api/state', json={'anomalies': []}, headers=bearer(t)) for t in candidates]

    # then
    assert {(r.status_code, r.json()['detail']) for r in seen} == {(401, GENERIC)}
    assert tokens.resolve(token) is None and tokens.resolve(live + 'x') is None and tokens.resolve(live) is not None


def test_viewer_token_cannot_call_operator_route(store: Store):
    # given
    token, _ = ApiTokens(store.db).create('reader', 'viewer', ADMIN_EMAIL)

    # when
    with make_client(store) as c:
        operator = c.post('/api/actions', json={'type': 'ask', 'key': 'k', 'text': 'Question'}, headers=bearer(token))
        admin = c.get('/api/admin/users', headers=bearer(token))
        login = c.get('/api/notifications', headers=bearer(token))
        machine = c.put('/api/state', json={'anomalies': []}, headers=bearer(token))

    # then
    assert (operator.status_code, operator.json()['detail']) == (403, LOW)
    assert (admin.status_code, admin.json()['detail']) == (403, LOW)
    assert login.status_code == 200
    assert (machine.status_code, machine.json()['detail']) == (403, LOW)


def test_bootstrap_token_is_admin_and_named(store: Store):
    # when
    with make_client(store) as c:
        queued = c.post('/api/actions', json={'type': 'ask', 'key': 'k', 'text': 'Question'}, headers=AUTH)
        users = c.get('/api/admin/users', headers=AUTH)
        made = c.post('/api/admin/tokens', json={'name': 'ci', 'role': 'admin'}, headers=AUTH)

    # then
    assert queued.status_code == 201 and queued.json()['requestedBy'] == 'token:bootstrap'
    assert users.status_code == 200
    assert made.status_code == 201 and made.json()['role'] == 'admin' and made.json()['createdBy'] == 'token:bootstrap'


def test_writer_routes_refuse_cookie_sessions(store: Store):
    # given
    client = admin_client(store)

    # when
    by_cookie = client.put('/api/state', json={'anomalies': []})
    by_token = client.put('/api/state', json={'anomalies': []}, headers=AUTH)
    note = client.post('/api/notes', json={'key': 'k', 'text': 't'})

    # then
    assert by_cookie.status_code in (401, 403) and note.status_code in (401, 403)
    assert by_token.status_code == 200


def test_admin_user_crud_and_last_admin_guard(store: Store):
    # given
    client = admin_client(store)
    last_admin = 'Skopax needs at least one admin.'

    # when
    created = client.post('/api/admin/users', json={'email': 'Bob@Example.com', 'name': 'Bob', 'role': 'viewer', 'password': PASSWORD})
    again = client.post('/api/admin/users', json={'email': 'bob@example.com', 'name': 'Bob', 'role': 'viewer', 'password': PASSWORD})
    short = client.post('/api/admin/users', json={'email': 'carol@example.com', 'name': 'Carol', 'role': 'viewer', 'password': 'short'})
    listed = client.get('/api/admin/users')
    promoted = client.patch('/api/admin/users/bob@example.com', json={'role': 'operator'})
    disabled = client.patch('/api/admin/users/bob@example.com', json={'disabled': True})
    reset = client.patch('/api/admin/users/bob@example.com', json={'password': 'another long passphrase'})
    enabled = client.patch('/api/admin/users/bob@example.com', json={'disabled': False})
    missing = client.patch('/api/admin/users/nobody@example.com', json={'role': 'viewer'})
    demote_last = client.patch(f'/api/admin/users/{ADMIN_EMAIL}', json={'role': 'operator'})
    disable_last = client.patch(f'/api/admin/users/{ADMIN_EMAIL}', json={'disabled': True})
    client.post('/api/admin/users', json={'email': 'dana@example.com', 'name': 'Dana', 'role': 'admin', 'password': PASSWORD})
    demote_one = client.patch(f'/api/admin/users/{ADMIN_EMAIL}', json={'role': 'operator'})
    self_demoted = client.get('/api/admin/users')
    activity = [a['text'] for a in store.activity.find()]

    # then
    assert created.status_code == 201 and created.json()['email'] == 'bob@example.com' and 'pw' not in created.json()
    assert again.status_code == 409 and short.status_code == 422
    assert {u['email'] for u in listed.json()} == {ADMIN_EMAIL, 'bob@example.com'} and not any('pw' in u for u in listed.json())
    assert promoted.status_code == 200 and promoted.json()['role'] == 'operator'
    assert disabled.status_code == 200 and disabled.json()['disabled'] is True
    assert reset.status_code == 200 and 'pw' not in reset.json()
    assert enabled.status_code == 200 and enabled.json()['disabled'] is False
    assert Users(store.db).authenticate('bob@example.com', 'another long passphrase') is not None
    assert Users(store.db).authenticate('bob@example.com', PASSWORD) is None
    assert missing.status_code == 404
    assert (demote_last.status_code, demote_last.json()['detail']) == (409, last_admin)
    assert (disable_last.status_code, disable_last.json()['detail']) == (409, last_admin)
    assert demote_one.status_code == 200 and self_demoted.status_code == 403
    assert 'User bob@example.com created with the viewer role' in activity and 'User bob@example.com disabled' in activity
    assert 'Password changed for bob@example.com' in activity
    assert not any(PASSWORD in t or 'another long' in t for t in activity)


def test_patch_role_change_records_an_admin_set_role(store: Store):
    # given
    users = Users(store.db)
    users.upsert_sso('ivy@example.com', 'Ivy', 'viewer')
    client = admin_client(store)

    # when
    before = users.get('ivy@example.com')
    changed = client.patch('/api/admin/users/ivy@example.com', json={'role': 'operator'})

    # then
    after = users.get('ivy@example.com')
    assert before['roleFrom'] == 'default'
    assert changed.status_code == 200 and (after['role'], after['adminRole'], after['roleFrom']) == ('operator', 'operator', 'admin')


def test_last_claim_admin_keeps_the_role_until_another_admin_exists(store: Store, caplog):
    # given
    users = Users(store.db)
    store.db['users'].delete_many({})
    users.upsert_sso('ivy@example.com', 'Ivy', 'viewer')
    users.apply_sign_in_role('ivy@example.com', 'admin', 'claim')

    # when
    with caplog.at_level(logging.WARNING):
        kept = users.apply_sign_in_role('ivy@example.com', 'viewer', 'default')
    warned = [r for r in caplog.records if r.levelno == logging.WARNING]
    users.create('jo@example.com', 'Jo', 'admin')
    demoted = users.apply_sign_in_role('ivy@example.com', 'viewer', 'default')

    # then
    assert (kept['role'], kept['roleFrom']) == ('admin', 'claim')
    assert len(warned) == 1
    assert (demoted['role'], demoted['roleFrom']) == ('viewer', 'default')


def test_only_an_admin_creates_tokens_and_any_valid_role_is_allowed(store: Store):
    # given
    add_user(store, 'oscar@example.com', 'operator')
    oscar = make_client(store)
    sign_in(oscar, 'oscar@example.com')
    admin = admin_client(store)

    # when
    denied_page = oscar.post('/api/admin/tokens', json={'name': 'x', 'role': 'viewer'})
    over = admin.post('/api/admin/tokens', json={'name': 'ok', 'role': 'admin'})
    bad_role = admin.post('/api/admin/tokens', json={'name': 'bad', 'role': 'root'})
    bad_name = admin.post('/api/admin/tokens', json={'name': '', 'role': 'viewer'})
    inner = ApiTokens(store.db).create('mid', 'operator', ADMIN_EMAIL)[0]
    through_token = make_client(store).post('/api/admin/tokens', json={'name': 'hi', 'role': 'admin'}, headers=bearer(inner))

    # then
    assert denied_page.status_code == 403
    assert over.status_code == 201 and over.json()['token'].startswith('skopax_') and over.json()['createdBy'] == ADMIN_EMAIL
    assert bad_role.status_code == 422 and bad_name.status_code == 422
    assert through_token.status_code == 403


def test_a_token_made_through_an_admin_token_records_the_person_behind_it(store: Store):
    # given
    admin_token = ApiTokens(store.db).create('adm', 'admin', ADMIN_EMAIL)[0]
    admin = admin_client(store)
    Users(store.db).create('ops@example.com', 'Ops', 'operator', PASSWORD)
    ops = make_client(store)
    sign_in(ops, 'ops@example.com')

    # when
    low = admin.patch('/api/admin/users/ops@example.com', json={'role': 'viewer'})
    with make_client(store) as c:
        child = c.post('/api/admin/tokens', json={'name': 'child', 'role': 'viewer'}, headers=bearer(admin_token))

    # then
    assert low.status_code == 200
    assert child.status_code == 201 and child.json()['createdBy'] == ADMIN_EMAIL


def test_tokens_follow_their_creator(store: Store):
    # given
    users = Users(store.db)
    users.create('alice@example.com', 'Alice', 'admin')
    tokens = ApiTokens(store.db)
    alice_op, _ = tokens.create('op', 'operator', 'alice@example.com')
    alice_admin, _ = tokens.create('adm', 'admin', 'alice@example.com')
    bootstrap, _ = tokens.create('machine', 'operator', 'token:bootstrap')
    ask = {'type': 'ask', 'key': 'k', 'text': 'Question'}

    # when
    with make_client(store) as c:
        before = c.post('/api/actions', json=ask, headers=bearer(alice_op)).status_code
        users.set_role('alice@example.com', 'viewer')
        demoted_op = c.post('/api/actions', json=ask, headers=bearer(alice_admin))
        demoted_login = c.get('/api/notifications', headers=bearer(alice_admin)).status_code
        users.set_role('alice@example.com', 'admin')
        restored = c.post('/api/actions', json=ask, headers=bearer(alice_admin)).status_code
        users.set_disabled('alice@example.com', True)
        disabled = c.post('/api/actions', json=ask, headers=bearer(alice_op))
        machine = c.put('/api/state', json={'anomalies': []}, headers=bearer(bootstrap)).status_code
        users.col.delete_one({'_id': 'alice@example.com'})
        deleted = c.get('/api/notifications', headers=bearer(alice_admin))

    # then
    assert before == 201
    assert (demoted_op.status_code, demoted_op.json()['detail']) == (403, LOW) and demoted_login == 200
    assert restored == 201
    assert (disabled.status_code, disabled.json()['detail']) == (401, GENERIC)
    assert machine == 200
    assert (deleted.status_code, deleted.json()['detail']) == (401, GENERIC)


def test_token_made_through_a_token_carries_the_person_behind_it(store: Store):
    # given
    users = Users(store.db)
    users.create('alice@example.com', 'Alice', 'admin')
    parent, _ = ApiTokens(store.db).create('parent', 'admin', 'alice@example.com')

    # when
    with make_client(store) as c:
        child = c.post('/api/admin/tokens', json={'name': 'child', 'role': 'operator'}, headers=bearer(parent))
        boot = c.post('/api/admin/tokens', json={'name': 'boot', 'role': 'viewer'}, headers=AUTH)
        users.set_disabled('alice@example.com', True)
        child_after = c.get('/api/notifications', headers=bearer(child.json()['token']))
        boot_after = c.get('/api/notifications', headers=bearer(boot.json()['token']))

    # then
    assert child.json()['createdBy'] == 'alice@example.com' and boot.json()['createdBy'] == 'token:bootstrap'
    assert child_after.status_code == 401 and boot_after.status_code == 200


def test_revoking_a_token_over_the_api(store: Store):
    # given
    client = admin_client(store)
    made = client.post('/api/admin/tokens', json={'name': 'ci', 'role': 'operator'}).json()

    # when
    listed = client.get('/api/admin/tokens').json()
    gone = client.delete(f"/api/admin/tokens/{made['id']}")
    after = make_client(store).put('/api/state', json={'anomalies': []}, headers=bearer(made['token']))
    missing = client.delete('/api/admin/tokens/ffffffff')
    activity = [a['text'] for a in store.activity.find()]

    # then
    assert [t['name'] for t in listed] == ['ci'] and 'token' not in listed[0] and 'hash' not in listed[0]
    assert gone.status_code == 204 and after.status_code == 401 and missing.status_code == 404
    assert 'API token ci created with the operator role' in activity and 'API token ci revoked' in activity


def test_a_fresh_last_use_skips_the_write_and_a_failed_write_never_fails_auth(store: Store, monkeypatch, caplog):
    # given
    caplog.set_level(logging.WARNING)
    now = [1_000_000.0]
    tokens = ApiTokens(store.db, clock=lambda: now[0])
    token, doc = tokens.create('ci', 'operator', ADMIN_EMAIL)
    tokens.resolve(token)
    writes = []
    real = tokens.col.update_one
    monkeypatch.setattr(tokens.col, 'update_one', lambda *a, **kw: writes.append(a) or real(*a, **kw))

    # when
    now[0] += 30
    fresh = tokens.resolve(token)
    skipped = len(writes)
    now[0] += 31
    monkeypatch.setattr(tokens.col, 'update_one', lambda *a, **kw: (_ for _ in ()).throw(AutoReconnect('down')))
    failed_write = tokens.resolve(token)

    # then
    assert fresh['name'] == 'ci' and skipped == 0, 'a use within the minute writes nothing'
    assert failed_write is not None and failed_write['name'] == 'ci', 'the bookkeeping write failing does not fail the token'
    assert [r.levelno for r in caplog.records if 'last use' in r.getMessage()] == [logging.WARNING]


def test_last_used_is_throttled_to_a_minute(store: Store):
    # given
    now = [1_000_000.0]
    tokens = ApiTokens(store.db, clock=lambda: now[0])
    token, doc = tokens.create('ci', 'operator', ADMIN_EMAIL)
    stored = lambda: store.db['api_tokens'].find_one({'_id': doc['id']})['lastUsed']

    # when
    unused = stored()
    tokens.resolve(token)
    first = stored()
    now[0] += 30
    tokens.resolve(token)
    within = stored()
    now[0] += 31
    tokens.resolve(token)
    after = stored()

    # then
    assert unused is None and first is not None
    assert within == first
    assert after != first


def test_a_named_token_is_a_token_actor_everywhere(store: Store):
    # given
    seed(store, sev='crit')

    # when
    _, named, auto_named = store.slack_draft(KEY, 'Named draft.', 'token:ci')
    _, legacy, auto_legacy = store.slack_draft(KEY, 'Legacy draft.', 'token')
    _, person, auto_person = store.slack_draft(KEY, 'Person draft.', 'oscar@example.com')

    # then
    assert auto_named is not None and auto_legacy is not None and auto_person is None
    assert [is_token(x) for x in ('token', 'token:ci', 'token:bootstrap', 'alice@example.com', '', None, 'tokens')] == [True, True, True, False, False, False, False]
    assert 'token:ci' not in AUTO_SEND_BY


def test_an_actor_with_no_name_reads_investigator_in_notes_history_and_notices():
    # given
    from skopax import actions, statenotes

    # when
    texts = [actions.note_under('seen', 'why', None, 'Muted'), statenotes.who_of(''), history.who_text('Claude'), notices.who_text('')]

    # then
    assert texts == ['seen\n\nMuted by Investigator: why', 'Investigator', 'Investigator', 'Investigator']


def test_the_label_of_a_token_actor_is_the_token_name(store: Store):
    # then
    assert token_label('token:ci') == 'ci' and token_label('token') == 'Investigator' and token_label('token:') == 'Investigator' and token_label('token:bootstrap') == 'bootstrap'
    assert history.who_text('token:ci') == 'ci' and history.who_text('token') == 'Investigator'
    assert notices.who_text('token:ci') == 'ci' and notices.who_text('token') == 'Investigator'


def test_the_actions_source_is_api_for_a_named_token(store: Store):
    # given
    token, _ = ApiTokens(store.db).create('ci', 'operator', ADMIN_EMAIL)

    # when
    with make_client(store) as c:
        named = c.post('/api/actions', json={'type': 'ask', 'key': 'k', 'text': 'Question'}, headers=bearer(token))
        boot = c.post('/api/actions', json={'type': 'ask', 'key': 'k2', 'text': 'Question two'}, headers=AUTH)

    # then
    assert named.status_code == 201 and named.json()['source'] == 'api' and named.json()['requestedBy'] == 'token:ci'
    assert boot.json()['source'] == 'api'


def test_machine_writes_record_the_real_caller(store: Store):
    # given
    token, _ = ApiTokens(store.db).create('ci', 'operator', ADMIN_EMAIL)

    # when
    with make_client(store) as c:
        gate = c.put('/api/gate-examples', json={'examples': 'x'}, headers=bearer(token))
        ignore = c.put('/api/postdeploy-ignore', json={'services': ['shop']}, headers=bearer(token))
    who = {a['who'] for a in store.activity.find()}

    # then
    assert gate.status_code == 200 and ignore.status_code == 200
    assert who == {'token:ci'}
    assert store.gate_examples()['updatedBy'] == 'token:ci' and store.postdeploy_ignore()['updatedBy'] == 'token:ci'


def test_a_database_error_while_resolving_a_token_is_a_503(store: Store, monkeypatch):
    # given
    token, _ = ApiTokens(store.db).create('ci', 'operator', ADMIN_EMAIL)

    def broken(self, *a, **k):
        raise AutoReconnect('down')

    with make_client(store) as c:
        monkeypatch.setattr(ApiTokens, 'resolve', broken)

        # when
        res = c.put('/api/state', json={'anomalies': []}, headers=bearer(token))

    # then
    assert res.status_code == 503 and res.json()['detail'] == msg('db.unavailable')


def test_token_indexes_are_made_at_startup_not_at_create_app(store: Store):
    # when
    app = create_app(settings(), store)
    before = 'api_tokens' in store.db.list_collection_names()
    with TestClient(app):
        pass
    indexes = store.db['api_tokens'].index_information()

    # then
    assert before is False
    assert indexes['createdAt_1']['key'] == [('createdAt', 1)]
    assert indexes['name_1']['key'] == [('name', 1)] and indexes['name_1']['unique'] is True


def test_duplicate_token_names_skip_only_the_name_index_and_the_rest_of_startup_runs(store: Store, caplog):
    # given
    caplog.set_level(logging.INFO, logger='skopax')
    store.db['api_tokens'].insert_many([{'_id': 'aaaa0001', 'name': 'ci', 'role': 'viewer', 'createdAt': '2026-11-01T00:00:00Z'},
                                        {'_id': 'aaaa0002', 'name': 'ci', 'role': 'viewer', 'createdAt': '2026-11-02T00:00:00Z'}])

    # when
    with TestClient(create_app(settings(), store)):
        pass

    # then
    warned = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING and 'api_tokens' in r.getMessage()]
    assert len(warned) == 1 and warned[0].startswith('API token names are not unique, so the unique name index was not built:')
    assert 'Mongo not reachable' not in caplog.text
    assert 'at_1' in store.db['login_attempts'].index_information(), 'the steps after the token indexes still ran'
    assert store.db['meta'].find_one({'_id': 'secret-key'}) is not None


def test_a_token_index_failure_that_is_not_a_duplicate_is_not_called_a_name_clash(store: Store, caplog, monkeypatch):
    # given
    caplog.set_level(logging.INFO, logger='skopax')

    def refuse(self):
        raise OperationFailure('not authorized on skopax to execute command', code=13)

    monkeypatch.setattr(ApiTokens, 'ensure_indexes', refuse)

    # when
    with TestClient(create_app(settings(), store)):
        pass

    # then
    assert 'names are not unique' not in caplog.text
    assert 'index setup skipped' in caplog.text and 'OperationFailure code 13' in caplog.text and 'not authorized' not in caplog.text


def test_a_duplicate_key_failure_with_the_duplicate_code_gets_the_name_clash_warning(store: Store, caplog, monkeypatch):
    # given
    caplog.set_level(logging.INFO, logger='skopax')

    def clash(self):
        raise OperationFailure('E11000 duplicate key error', code=11000)

    monkeypatch.setattr(ApiTokens, 'ensure_indexes', clash)

    # when
    with TestClient(create_app(settings(), store)):
        pass

    # then
    assert 'API token names are not unique' in caplog.text
    assert 'index setup skipped' not in caplog.text


def test_token_names_are_unique_and_safe(store: Store):
    # given
    client = admin_client(store)
    make = lambda name: client.post('/api/admin/tokens', json={'name': name, 'role': 'viewer'})

    # when
    first = make('CI-Bot')
    same = make('ci-bot')
    revoked = client.delete(f"/api/admin/tokens/{first.json()['id']}")
    after_revoke = make('ci-bot')
    reserved = [make('bootstrap'), make('Bootstrap')]
    bad = [make(n) for n in ('a@b', 'has space', '', 'x' * 81, '-lead', '.dot')]
    longest = make('y' * 80)
    huge = make('z' * 401)

    # then
    taken, invalid = (422, msg('access.token_name_taken')), (422, msg('access.token_name_invalid'))
    assert first.status_code == 201 and first.json()['name'] == 'ci-bot'
    assert (same.status_code, same.json()['detail']) == taken and revoked.status_code == 204 and (after_revoke.status_code, after_revoke.json()['detail']) == taken
    assert [(r.status_code, r.json()['detail']) for r in reserved] == [taken, taken]
    assert [(r.status_code, r.json()['detail']) for r in bad] == [invalid] * 6
    assert (huge.status_code, huge.json()['detail']) == invalid, 'an overlong name gets the same text, not a schema error'
    assert longest.status_code == 201
    assert 'bootstrap' not in {t['name'] for t in client.get('/api/admin/tokens').json()}


def test_a_duplicate_insert_race_is_a_token_name_error(store: Store, monkeypatch):
    # given
    tokens = ApiTokens(store.db)
    tokens.ensure_indexes()
    tokens.create('ci', 'viewer', ADMIN_EMAIL)
    real = tokens.col.find_one
    checks = []
    monkeypatch.setattr(tokens.col, 'find_one', lambda *a, **kw: None if not checks.append(1) and len(checks) == 1 else real(*a, **kw))
    inserts = []
    real_insert = tokens.col.insert_one
    monkeypatch.setattr(tokens.col, 'insert_one', lambda *a, **kw: inserts.append(1) or real_insert(*a, **kw))

    # when
    with pytest.raises(TokenNameError) as raced:
        tokens.create('ci', 'viewer', ADMIN_EMAIL)

    # then
    assert inserts == [1], 'the pre-check was passed, so the unique index refused the insert'
    assert raced.value.key == 'access.token_name_taken'
    assert [t['name'] for t in tokens.list()] == ['ci']


def test_a_token_id_collision_is_retried_with_a_new_id(store: Store, monkeypatch):
    # given
    tokens = ApiTokens(store.db)
    tokens.ensure_indexes()
    _, first = tokens.create('ci', 'viewer', ADMIN_EMAIL)
    ids = iter([first['id'], 'abcd1234'])
    monkeypatch.setattr('skopax.apitokens.secrets.token_hex', lambda n: next(ids))

    # when
    token, second = tokens.create('ops', 'viewer', ADMIN_EMAIL)

    # then
    assert second['id'] == 'abcd1234' and token.startswith('skopax_abcd1234_')
    assert sorted(t['name'] for t in tokens.list()) == ['ci', 'ops']
    assert tokens.resolve(token)['name'] == 'ops'


def test_two_tokens_never_share_a_chat(store: Store):
    # given
    tokens = ApiTokens(store.db)
    tokens.ensure_indexes()
    tokens.create('ci', 'operator', ADMIN_EMAIL)
    chat_action(store, 'A private question', chat='c1', by='token:ci')

    # when
    with pytest.raises(TokenNameError):
        tokens.create('ci', 'operator', ADMIN_EMAIL)
    tokens.create('ops', 'operator', ADMIN_EMAIL)

    # then
    assert store.chat_for('token:ci', 'c1') is not None
    assert store.chat_for('token:ops', 'c1') is None and store.chat_for('token:bootstrap', 'c1') is None


def test_a_patch_with_a_bad_field_changes_nothing(store: Store):
    # given
    client = admin_client(store)
    users = Users(store.db)
    users.create('bob@example.com', 'Bob', 'viewer', PASSWORD)
    before_activity = store.activity.count_documents({})

    # when
    mixed = client.patch('/api/admin/users/bob@example.com', json={'role': 'operator', 'password': 'short'})
    bad_role = client.patch('/api/admin/users/bob@example.com', json={'role': 'root', 'disabled': True})
    last_admin_bad_role = client.patch(f'/api/admin/users/{ADMIN_EMAIL}', json={'role': 'root'})
    last_admin_bad_password = client.patch(f'/api/admin/users/{ADMIN_EMAIL}', json={'role': 'viewer', 'password': 'x'})
    bob = users.get('bob@example.com')

    # then
    assert [r.status_code for r in (mixed, bad_role, last_admin_bad_role, last_admin_bad_password)] == [422, 422, 422, 422]
    assert (bob['role'], bob['disabled']) == ('viewer', False)
    assert users.authenticate('bob@example.com', PASSWORD) is not None
    assert users.get(ADMIN_EMAIL)['role'] == 'admin'
    assert store.activity.count_documents({}) == before_activity


def test_one_patch_applies_every_field_and_records_the_admin_role(store: Store):
    # given
    users = Users(store.db)
    users.create('ivy@example.com', 'Ivy', 'viewer', PASSWORD)
    client = admin_client(store)

    # when
    res = client.patch('/api/admin/users/ivy@example.com', json={'role': 'operator', 'disabled': True, 'password': 'another long passphrase'})
    stored = store.db['users'].find_one({'_id': 'ivy@example.com'})
    activity = [a['text'] for a in store.activity.find()]

    # then
    assert res.status_code == 200
    assert (stored['role'], stored['disabled'], stored['roleFrom'], stored['adminRole']) == ('operator', True, 'admin', 'operator')
    assert check_password('another long passphrase', stored['pw'])
    assert 'User ivy@example.com now has the operator role' in activity and 'User ivy@example.com disabled' in activity and 'Password changed for ivy@example.com' in activity


def test_admin_sets_a_password_on_an_sso_user_who_signed_in(store: Store):
    # given
    client = admin_client(store)
    Users(store.db).upsert_sso('carol@example.com', 'Carol', 'viewer')

    # when
    res = client.patch('/api/admin/users/carol@example.com', json={'password': PASSWORD})
    role = client.patch('/api/admin/users/carol@example.com', json={'role': 'operator'})

    # then
    assert res.status_code == 200
    assert store.db['users'].find_one({'_id': 'carol@example.com'})['source'] == 'local'
    assert Users(store.db).authenticate('carol@example.com', PASSWORD) is not None
    assert role.status_code == 200


def test_user_input_errors_reach_the_page_as_message_texts(store: Store):
    # given
    client = admin_client(store)
    user = lambda **over: {'email': 'erin@example.com', 'name': 'Erin', 'role': 'viewer', 'password': PASSWORD} | over

    # when
    seen = {
        'email': client.post('/api/admin/users', json=user(email='not-an-email')),
        'name': client.post('/api/admin/users', json=user(name=' ')),
        'role': client.post('/api/admin/users', json=user(role='owner')),
        'password': client.post('/api/admin/users', json=user(password='short')),
        'patch role': client.patch(f'/api/admin/users/{ADMIN_EMAIL}', json={'role': 'owner'}),
        'patch password': client.patch(f'/api/admin/users/{ADMIN_EMAIL}', json={'password': 'short'}),
    }

    # then
    assert {k: (r.status_code, r.json()['detail']) for k, r in seen.items()} == {
        'email': (422, msg('user.email_invalid')), 'name': (422, msg('user.name_length', max=120)), 'role': (422, msg('user.role_invalid')),
        'password': (422, msg('user.password_length', min=12, max=256)), 'patch role': (422, msg('user.role_invalid')),
        'patch password': (422, msg('user.password_length', min=12, max=256))}
    assert all(isinstance(r.json()['detail'], str) and r.json()['detail'][0].isupper() for r in seen.values()), 'shown on the page as a sentence'


def test_two_admins_demoting_each_other_at_once_leave_one_admin(store: Store, monkeypatch, caplog):
    # given
    client = admin_client(store)
    users = Users(store.db)
    users.create('dana@example.com', 'Dana', 'admin', PASSWORD)
    users.col.update_one({'_id': ADMIN_EMAIL}, {'$set': {'roleFrom': 'default'}})
    before = users.col.find_one({'_id': ADMIN_EMAIL})
    monkeypatch.setattr(Users, 'is_last_admin', lambda self, email: False)

    # when
    first = client.patch('/api/admin/users/dana@example.com', json={'disabled': True})
    second = client.patch(f'/api/admin/users/{ADMIN_EMAIL}', json={'role': 'viewer', 'password': 'another long passphrase'})
    admins_after, admin_after = users.active_admins(), users.col.find_one({'_id': ADMIN_EMAIL})
    users.upsert_sso('ivy@example.com', 'Ivy', 'viewer')
    users.apply_sign_in_role('ivy@example.com', 'admin', 'claim')
    users.set_disabled(ADMIN_EMAIL, True)
    users.apply_sign_in_role('ivy@example.com', 'viewer', 'default')

    # then
    assert first.status_code == 200
    assert (second.status_code, second.json()['detail']) == (409, msg('access.last_admin'))
    assert admins_after == 1
    assert admin_after == before, 'the refused change is rolled back field by field, password and role source included'
    assert users.get('ivy@example.com')['role'] == 'admin'
    assert 'Keeping the admin role for ivy@example.com' in caplog.text, 'the claim demotion shares the guard'


def test_an_empty_patch_writes_nothing(store: Store, monkeypatch):
    # given
    client = admin_client(store)
    writes = []
    monkeypatch.setattr(Users, 'update', lambda self, *a, **kw: writes.append(a))
    monkeypatch.setattr(Users, 'guarded_update', lambda self, *a, **kw: writes.append(a))
    rows = store.activity.count_documents({})

    # when
    res = client.patch(f'/api/admin/users/{ADMIN_EMAIL}', json={})

    # then
    assert res.status_code == 200 and res.json()['email'] == ADMIN_EMAIL and res.json()['role'] == 'admin'
    assert writes == [] and store.activity.count_documents({}) == rows


def test_revoking_twice_is_a_no_op_the_second_time(store: Store):
    # given
    now = [1_800_000_000.0]
    tokens = ApiTokens(store.db, clock=lambda: now[0])
    _, doc = tokens.create('ci', 'viewer', ADMIN_EMAIL)
    client = admin_client(store)

    # when
    first = client.delete(f"/api/admin/tokens/{doc['id']}")
    stamped = store.db['api_tokens'].find_one({'_id': doc['id']})
    now[0] += 600
    again = tokens.revoke(doc['id'], 'someone.else@example.com')
    middle = store.db['api_tokens'].find_one({'_id': doc['id']})
    second = client.delete(f"/api/admin/tokens/{doc['id']}")
    unknown = client.delete('/api/admin/tokens/ffffffff')
    after = store.db['api_tokens'].find_one({'_id': doc['id']})

    # then
    assert (first.status_code, second.status_code, unknown.status_code) == (204, 204, 404)
    assert again is None
    assert {(d['revokedAt'], d['revokedBy']) for d in (stamped, middle, after)} == {(stamped['revokedAt'], ADMIN_EMAIL)}, 'no re-stamp'
    assert [a['text'] for a in store.activity.find()].count('API token ci revoked') == 1


def test_a_role_and_password_patch_on_an_sso_user_writes_both_in_one_change(store: Store):
    # given
    client = admin_client(store)
    users = Users(store.db)
    users.upsert_sso('sam@example.com', 'Sam', 'viewer')

    # when
    res = client.patch('/api/admin/users/sam@example.com', json={'role': 'operator', 'password': 'another long passphrase'})
    doc = users.col.find_one({'_id': 'sam@example.com'})

    # then
    assert res.status_code == 200
    assert (doc['source'], doc['role'], doc['roleFrom']) == ('local', 'operator', 'admin')
    assert users.authenticate('sam@example.com', 'another long passphrase') is not None


STOP_ADMINS = json.dumps([{'value': 'ops-admins', 'role': 'admin'}])
ERIN = 'erin@example.com'


def stop_client(store: Store):
    idp, clock = FakeIdp(), Clock()
    return app_client(store, idp, idp_settings(idp, sso_role_map=STOP_ADMINS), clock=clock), idp, clock


def made(client: TestClient, name: str, role: str = 'viewer', **headers) -> str:
    res = client.post('/api/admin/tokens', json={'name': name, 'role': role}, headers=headers)
    assert res.status_code == 201, res.text
    return res.json()['token']


def answers(client: TestClient, tokens: dict[str, str]) -> dict[str, int]:
    return {name: client.get('/api/notifications/count', headers=bearer(token)).status_code for name, token in tokens.items()}


def test_api_tokens_made_from_an_sso_session_end_with_the_sso_stop(store: Store):
    # given
    client, idp, clock = stop_client(store)
    users = Users(store.db)
    users.set_password(ADMIN_EMAIL, PASSWORD)
    users.create(ERIN, 'Erin', 'admin', PASSWORD)
    users.set_sso_link(ERIN, True, ADMIN_EMAIL)
    password = TestClient(client.app, base_url='https://testserver', follow_redirects=False)
    sign_in(password, ADMIN_EMAIL)
    linked = TestClient(client.app, base_url='https://testserver', follow_redirects=False)
    assert log_in(client, idp, roles=['ops-admins']).status_code == 303
    assert log_in(linked, idp, sub='erin-subject', email=ERIN, email_verified=True).status_code == 303
    tokens = {'sso-made': made(client, 'sso-made'), 'sso-admin': made(client, 'sso-admin', 'admin'), 'linked-made': made(linked, 'linked-made'),
              'pw-made': made(password, 'pw-made'), 'boot-made': made(TestClient(client.app, base_url='https://testserver'), 'boot-made', **AUTH)}
    tokens['child'] = made(TestClient(client.app, base_url='https://testserver'), 'child', **bearer(tokens['sso-admin']))
    pw_admin = made(password, 'pw-admin', 'admin')
    tokens['pw-child'] = made(TestClient(client.app, base_url='https://testserver'), 'pw-child', **bearer(pw_admin))
    rows = {t['name']: t for t in ApiTokens(store.db).list()}

    # when
    before = answers(client, tokens)
    clock.advance(5)
    stopped = client.put('/api/admin/settings', json={'key': 'sso.enabled', 'value': 'false'}, headers=AUTH)
    clock.advance(120)
    used_before = {d['name']: (d.get('usedAt'), d.get('lastUsed')) for d in store.db['api_tokens'].find()}
    during = answers(client, tokens)
    used_after = {d['name']: (d.get('usedAt'), d.get('lastUsed')) for d in store.db['api_tokens'].find()}
    restarted = client.put('/api/admin/settings', json={'key': 'sso.enabled', 'value': 'true'}, headers=AUTH)
    after_on = answers(client, tokens)
    refused = client.get('/api/notifications/count', headers=bearer(tokens['sso-made']))
    clock.advance(5)
    assert log_in(client, idp, roles=['ops-admins']).status_code == 303
    tokens['sso-after'] = made(client, 'sso-after')
    fresh = answers(client, {'sso-after': tokens['sso-after']})
    listed = {t['name']: t.get('endedByStop') for t in client.get('/api/admin/tokens', headers=AUTH).json()}

    # then
    assert {name: row.get('via') for name, row in rows.items()} == {'sso-made': 'sso', 'sso-admin': 'sso', 'linked-made': 'sso', 'pw-made': None, 'boot-made': None, 'child': 'sso',
                                                                     'pw-admin': None, 'pw-child': None}
    assert before == dict.fromkeys(tokens.keys() - {'sso-after'}, 200)
    assert stopped.status_code == 200 and restarted.status_code == 200
    ended = {'sso-made': 401, 'sso-admin': 401, 'linked-made': 401, 'pw-made': 200, 'boot-made': 200, 'child': 401, 'pw-child': 200}
    assert during == ended and after_on == ended
    bumped = {name for name in used_after if used_after[name] != used_before[name]}
    assert bumped == {'pw-made', 'boot-made', 'pw-child'}, 'a token refused by the stop never bumps its last use'
    assert refused.json() == {'detail': msg('access.token_invalid')}
    assert fresh == {'sso-after': 200}
    assert listed == {'sso-made': True, 'sso-admin': True, 'linked-made': True, 'pw-made': False, 'boot-made': False, 'child': True, 'pw-admin': False, 'pw-child': False,
                      'sso-after': False}


def test_an_odd_stop_stamp_is_ignored_with_a_warning_and_an_odd_creation_time_fails_closed(store: Store, caplog):
    # given
    client, idp, clock = stop_client(store)
    Users(store.db).set_password(ADMIN_EMAIL, PASSWORD)
    password = TestClient(client.app, base_url='https://testserver', follow_redirects=False)
    sign_in(password, ADMIN_EMAIL)
    assert log_in(client, idp, roles=['ops-admins']).status_code == 303
    tokens = {'sso-made': made(client, 'sso-made'), 'sso-odd-time': made(client, 'sso-odd-time'), 'pw-made': made(password, 'pw-made'), 'pw-odd-time': made(password, 'pw-odd-time')}
    for name in ('sso-odd-time', 'pw-odd-time'):
        store.db['api_tokens'].update_one({'name': name}, {'$set': {'createdAt': 'yesterday'}})
    live = client.app.state.live

    # when
    no_stamp = answers(client, tokens)
    no_stamp_rows = {t['name']: t.get('endedByStop') for t in client.get('/api/admin/tokens', headers=AUTH).json()}
    odd = {}
    with caplog.at_level(logging.WARNING):
        for stamp in ('not-a-stamp', '', 20261117, None, '2026-11-17 10:00:00'):
            store.db['tunables'].replace_one({'_id': SESSIONS_AFTER_KEY}, {'_id': SESSIONS_AFTER_KEY, 'value': stamp}, upsert=True)
            live.refresh(force=True)
            odd[repr(stamp)] = answers(client, tokens)
            answers(client, tokens)
        odd_rows = {t['name']: t.get('endedByStop') for t in client.get('/api/admin/tokens', headers=AUTH).json()}
    warned = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING and msg('sso.stop_stamp_odd', key=SESSIONS_AFTER_KEY) == r.getMessage()]

    # then
    assert no_stamp == {'sso-made': 200, 'sso-odd-time': 401, 'pw-made': 200, 'pw-odd-time': 200}
    assert no_stamp_rows == {'sso-made': False, 'sso-odd-time': True, 'pw-made': False, 'pw-odd-time': False}
    assert odd == dict.fromkeys(odd, {'sso-made': 200, 'sso-odd-time': 401, 'pw-made': 200, 'pw-odd-time': 200})
    assert odd_rows == {'sso-made': False, 'sso-odd-time': True, 'pw-made': False, 'pw-odd-time': False}
    assert len(warned) == len(odd), 'one warning per odd stamp value, not one per request'


def test_a_token_minted_on_a_lagging_pod_after_the_stop_is_refused_while_sso_is_off_and_after(store: Store):
    # given
    client, idp, clock = stop_client(store)
    pod_clock = Clock()
    other = create_app(idp_settings(idp, sso_role_map=STOP_ADMINS), store, sso_http=SsoHttp(fetch=idp.fetch, exchange=idp.exchange), clock=clock)
    other.state.live.clock = pod_clock
    lagging = TestClient(other, base_url='https://testserver', follow_redirects=False)
    assert log_in(client, idp, roles=['ops-admins']).status_code == 303
    lagging.cookies.set('skopax_session', client.cookies.get('skopax_session'))
    early = made(lagging, 'early')

    # when
    clock.advance(5)
    stopped = client.put('/api/admin/settings', json={'key': 'sso.enabled', 'value': 'false'}, headers=AUTH)
    stamp = store.db['tunables'].find_one({'_id': SESSIONS_AFTER_KEY})['value']
    clock.advance(2)
    minted = lagging.post('/api/admin/tokens', json={'name': 'late', 'role': 'admin'})
    plain = made(TestClient(other, base_url='https://testserver'), 'plain', **AUTH)
    tokens = {'early': early, 'late': minted.json()['token'], 'plain': plain}
    row = store.db['api_tokens'].find_one({'name': 'late'})
    off_on_a = answers(client, tokens)
    off_rows = {t['name']: t.get('endedByStop') for t in client.get('/api/admin/tokens', headers=AUTH).json()}
    pod_clock.advance(16)
    off_on_b = answers(lagging, tokens)
    clock.advance(5)
    restarted = client.put('/api/admin/settings', json={'key': 'sso.enabled', 'value': 'true'}, headers=AUTH)
    pod_clock.advance(16)
    on_a, on_b = answers(client, tokens), answers(lagging, tokens)
    on_rows = {t['name']: t.get('endedByStop') for t in client.get('/api/admin/tokens', headers=AUTH).json()}

    # then
    assert stopped.status_code == 200 and restarted.status_code == 200
    assert minted.status_code == 201 and row['via'] == 'sso' and row['createdAt'] > stamp, 'the lagging pod still honours the SSO session after the stop'
    refused = {'early': 401, 'late': 401, 'plain': 200}
    assert off_on_a == refused, 'a token made from an SSO session does not work while SSO is off'
    assert off_on_b == refused
    assert on_a == refused and on_b == refused, 'and the one minted after the stop stays refused after switching back on'
    assert off_rows == on_rows == {'early': True, 'late': True, 'plain': False}


def test_a_token_with_an_expiry_works_until_then_and_is_refused_after(store: Store):
    # given
    now = [1_000_000.0]
    tokens = ApiTokens(store.db, clock=lambda: now[0])
    token, doc = tokens.create('short', 'viewer', ADMIN_EMAIL, expires_days=7)
    forever, _ = tokens.create('long', 'viewer', ADMIN_EMAIL)

    # when
    early = tokens.resolve(token)
    now[0] += 7 * 86400 - 1
    last = tokens.resolve(token)
    now[0] += 1
    late = tokens.resolve(token)
    kept = tokens.resolve(forever)
    listed = {row['name']: row for row in tokens.list()}

    # then
    assert doc['expiresAt'] == clock.stamp(1_000_000.0 + 7 * 86400)
    assert early is not None and last is not None and late is None and kept is not None
    assert listed['short']['expiresAt'] == doc['expiresAt'] and 'expiresTs' not in listed['short']
    assert listed['long'].get('expiresAt') is None


def test_the_admin_api_takes_an_optional_expiry_in_days(store: Store):
    # given
    admin = admin_client(store)

    # when
    made = admin.post('/api/admin/tokens', json={'name': 'week', 'role': 'viewer', 'expiresDays': 7})
    plain = admin.post('/api/admin/tokens', json={'name': 'plain', 'role': 'viewer'})
    zero = admin.post('/api/admin/tokens', json={'name': 'zero', 'role': 'viewer', 'expiresDays': 0})
    huge = admin.post('/api/admin/tokens', json={'name': 'huge', 'role': 'viewer', 'expiresDays': 3651})
    text = admin.post('/api/admin/tokens', json={'name': 'text', 'role': 'viewer', 'expiresDays': '7'})
    listed = {row['name']: row for row in admin.get('/api/admin/tokens').json()}

    # then
    assert made.status_code == 201 and isinstance(made.json()['expiresAt'], str)
    assert plain.status_code == 201 and plain.json().get('expiresAt') is None
    assert (zero.status_code, huge.status_code, text.status_code) == (422, 422, 422)
    assert listed['week']['expiresAt'] == made.json()['expiresAt'] and 'expiresTs' not in listed['week']
    assert set(listed) == {'week', 'plain'}


def test_the_last_admin_cannot_be_demoted_or_disabled_over_the_api(store: Store):
    # given
    admin = admin_client(store)

    # when
    demoted = admin.patch(f'/api/admin/users/{ADMIN_EMAIL}', json={'role': 'viewer'})
    disabled = admin.patch(f'/api/admin/users/{ADMIN_EMAIL}', json={'disabled': True})
    held = Users(store.db).get(ADMIN_EMAIL)

    # then
    assert (demoted.status_code, demoted.json()['detail']) == (409, msg('access.last_admin'))
    assert (disabled.status_code, disabled.json()['detail']) == (409, msg('access.last_admin'))
    assert (held['role'], held['disabled']) == ('admin', False)
