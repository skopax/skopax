import dataclasses

from fastapi.testclient import TestClient

from tests.conftest import ADMIN_EMAIL, AUTH, anomaly, settings, state
from tests.fake_anthropic import FakeAnthropic, reply, text, tool
from tests.test_engine import item, seeded
from tests.plugin_kit import prom_tools
from tests.test_investigator_tools import FakeProm, box, sources
from skopax.app import create_app
from skopax.auth import SESSION_COOKIE
from skopax.engine import PassResult, apply, iso
from skopax.investigator import Investigator
from skopax.investigator.tools import Effects
from skopax.sessions import Sessions, session_secret
from skopax.store import ACTIVITY_TEXT_LIMIT, Store
from skopax.users import PW_GEN, Users

NOW = 1_792_224_000.0
KEY = 'log|a|ERROR|x'


def test_activity_is_listed_newest_first_filtered_by_since_and_without_internals(store: Store):
    # given
    store.add_activity('pass', 'server', 'prom ok')
    store.add_activity('tool', 'investigator', 'esql · 120 ms', runId='r1', rows=None)

    # when
    rows = store.list_activity(None, 10)
    newer = store.list_activity(rows[0]['t'], 10)

    # then
    assert [r['text'] for r in rows] == ['esql · 120 ms', 'prom ok']
    assert rows[0]['runId'] == 'r1' and 'rows' not in rows[0] and 'at' not in rows[0] and rows[0]['id']
    assert newer[0]['id'] == rows[0]['id'] and all(r['t'] >= rows[0]['t'] for r in newer)


def test_activity_text_is_cut_to_the_limit(store: Store):
    # when
    store.add_activity('pass', 'server', 'x' * (ACTIVITY_TEXT_LIMIT + 50))

    # then
    assert len(store.list_activity(None, 1)[0]['text']) == ACTIVITY_TEXT_LIMIT


def test_server_passes_leave_a_pass_line_and_a_merge_line(store: Store):
    # given
    seeded(store)

    # when
    apply(store, [PassResult('prom', True, 'ok', [item('cf-a|x', now=30)]), PassResult('logs', False, 'HTTP 503')], NOW)
    rows = store.list_activity(None, 10)

    # then
    assert [(r['text'], r.get('error')) for r in rows if r['kind'] == 'pass'] == [('prom ok · logs HTTP 503', 'logs HTTP 503')]
    assert any(r['kind'] == 'merge' and 'new' in r['text'] for r in rows)
    assert all(r['who'] == 'server' for r in rows)


def test_every_tool_call_and_every_write_leave_a_line_with_the_run_id(store: Store):
    # given
    store.save_state(state(anomaly(KEY)), None)
    b = box(store, keys=(KEY,), run_id='run-7', plugin_tools=prom_tools(FakeProm([])))

    # when
    b.call('promql', {'expr': 'up'})
    b.call('promql', {})
    b.call('note', {'key': KEY, 'text': 'one client'})
    rows = store.list_activity(None, 10)

    # then
    assert [(r['kind'], r['runId']) for r in rows] == [('write', 'run-7'), ('tool', 'run-7'), ('tool', 'run-7')]
    assert rows[0]['text'].startswith('note · ') and KEY in rows[0]['text']
    assert 'error' in rows[1]['text'] and 'expr is required' in rows[1]['text']
    assert rows[2]['text'].startswith('promql · ') and 'ms' in rows[2]['text']


def test_a_shadow_write_is_marked_proposed_in_the_stream(store: Store):
    # given
    store.save_state(state(anomaly(KEY)), None)
    b = box(store, mode='shadow', keys=(KEY,), run_id='run-8')

    # when
    b.call('note', {'key': KEY, 'text': 'one client'})

    # then
    assert store.list_activity(None, 1)[0]['text'].startswith('note (proposed) · ')


def test_a_run_leaves_a_start_and_an_end_line(store: Store):
    # given
    st = state(anomaly(KEY, firstSeen=iso(NOW - 600))) | {'initialized': True, 'history': [{'t': iso(NOW - 300), 'key': KEY, 'ev': 'new', 'sev': 'warn', 'text': KEY, 'muted': False}]}
    store.save_state(st, None)
    s = dataclasses.replace(settings(), anthropic_api_key='sk-test', investigate_mode='shadow')
    inv = Investigator(store, sources(), Effects(lambda k, b: {}, lambda k, t, b, notice=None: {}), s, client=FakeAnthropic([reply(tool('note', {'key': KEY, 'text': 'x'})), reply(text('recap'), stop='end_turn')]), clock=lambda: NOW)

    # when
    record = inv.run_once(NOW)
    runs = [r for r in store.list_activity(None, 20) if r['kind'] == 'run']

    # then
    assert [r['text'].split(' · ')[0] for r in runs] == ['run done', 'run started']
    assert all(r['runId'] == record['id'] and r['who'] == 'investigator' for r in runs)
    assert '1 tool calls' in runs[0]['text'] and '$' in runs[0]['text']


def test_the_activity_api_lists_the_stream_and_pause_resume_leave_a_switch_line(store: Store):
    # given
    store.add_activity('pass', 'server', 'prom ok')

    # when
    with TestClient(create_app(settings(), store)) as c:
        c.put('/api/investigate', json={'stopped': True}, headers=AUTH)
        c.put('/api/investigate', json={'stopped': False}, headers=AUTH)
        rows = c.get('/api/activity', params={'limit': 10}).json()
        since = c.get('/api/activity', params={'since': rows[0]['t'], 'limit': 10}).json()

    # then
    assert [(r['kind'], r['who'], r['text']) for r in rows[:2]] == [('switch', 'token:bootstrap', 'Resume'), ('switch', 'token:bootstrap', 'Pause')]
    assert rows[-1]['text'] == 'prom ok'
    assert since[0]['id'] == rows[0]['id'] and all(r['t'] >= rows[0]['t'] for r in since)


def test_access_changes_in_the_stream_are_shown_to_admins_only(store: Store):
    # given
    users = Users(store.db)
    users.create('carol@example.com', 'Carol', 'viewer')
    users.set_password(ADMIN_EMAIL, 'admin-long-password-1')
    sealer = Sessions(session_secret(store.db, settings()), 3600, 7200)
    viewer = {'Cookie': f"{SESSION_COOKIE}={sealer.issue('carol@example.com', 'viewer', 'local')}"}
    admin = {'Cookie': f"{SESSION_COOKIE}={sealer.issue(ADMIN_EMAIL, 'admin', 'local', users.get(ADMIN_EMAIL).get(PW_GEN))}"}
    with TestClient(create_app(settings(public_read=True), store), base_url='https://testserver') as c:
        c.post('/api/admin/users', json={'email': 'dana@example.com', 'name': 'Dana', 'role': 'admin', 'password': 'correct horse battery'}, headers=AUTH)
        c.patch('/api/admin/users/dana@example.com', json={'disabled': True}, headers=AUTH)
        reader = c.post('/api/admin/tokens', json={'name': 'reader', 'role': 'viewer'}, headers=AUTH).json()
        c.delete(f"/api/admin/tokens/{c.post('/api/admin/tokens', json={'name': 'ci', 'role': 'admin'}, headers=AUTH).json()['id']}", headers=AUTH)
        store.add_activity('pass', 'server', 'prom ok')
        store.add_activity('access', 'token:bootstrap', 'API token newest created with the admin role')

        # when
        seen = {who: c.get('/api/activity', params={'limit': 50}, headers=headers).json() for who, headers in
                (('anonymous', {}), ('viewer', viewer), ('viewer token', {'Authorization': f"Bearer {reader['token']}"}), ('bad token', {'Authorization': 'Bearer nope'}),
                 ('admin', admin), ('admin token', AUTH))}
        newest = c.get('/api/activity', params={'limit': 1}).json()

    # then
    kinds = {who: [r['kind'] for r in rows] for who, rows in seen.items()}
    for who in ('anonymous', 'viewer', 'viewer token', 'bad token'):
        assert 'access' not in kinds[who] and 'prom ok' in [r['text'] for r in seen[who]], who
        assert not [r for r in seen[who] if 'dana@example.com' in r['text'] or 'API token' in r['text']], who
    for who in ('admin', 'admin token'):
        assert kinds[who].count('access') == 6, who
        assert {r['text'] for r in seen[who] if r['kind'] == 'access'} == {'User dana@example.com created with the admin role', 'User dana@example.com disabled',
                                                                          'API token reader created with the viewer role', 'API token ci created with the admin role',
                                                                          'API token ci revoked', 'API token newest created with the admin role'}, who
    assert [r['text'] for r in newest] == ['prom ok'], 'the limit counts only the rows the reader may see'
