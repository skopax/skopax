import dataclasses
import json

import anyio
from fastapi.testclient import TestClient

from tests.conftest import AUTH, anomaly, ready_app, settings
from tests.fake_anthropic import FakeAnthropic, reply, text, tool
from tests.test_chat import chat_action
from tests.test_guard_admin import CARD, held_run
from tests.test_guard_wiring import FakeGuard, check, guarded
from tests.test_guard import said
from tests.test_investigator import KEY, NOW, seed
from tests.test_investigator_slack import slack_action
from tests.test_mcp import tools_of
from tests.test_plugin_tools import DEMO, NOTES, PLAIN, URL, WHERE, Rig
from tests.test_auth import sso_settings
from tests.test_read_gate import login_cookie
from skopax import aiuses
from skopax.app import create_app
from skopax.auth import SESSION_COOKIE
from skopax.investigator import public_run
from skopax.investigator.guard import MASK_TEXT, Guard
from skopax.mcpauth import McpUser
from skopax.messages import msg
from skopax.store import Store
from skopax.users import Users

MODEL = 'claude-haiku-5-5'
UNKEYED = 'gpt-6-luna'
CARD_ANSWER = 'checkout-service canary-card-answer went up after the deploy.'
CHAT_ANSWER = 'Your canary-chat-answer is ready.'
PASSED_ANSWER = 'Nothing new on checkout-service.'
CHAT_QUESTION = 'Is my canary-chat-question private?'
REASON = 'names a canary-reason token'
OPERATOR = 'olga.page@example.com'
CANARIES = (CARD_ANSWER, CHAT_ANSWER, CHAT_QUESTION, REASON)


def guard_on(c: TestClient) -> None:
    for key, value in (('investigate.guard-model', MODEL), ('guard.check-answers', 'true'), ('guard.on-hold', 'review')):
        assert c.put('/api/admin/settings', json={'key': key, 'value': value}, headers=AUTH).status_code == 200


def guard_row(c: TestClient) -> dict:
    return next(u for u in c.get('/api/admin/ai-uses', headers=AUTH).json()['uses'] if u['use'] == aiuses.GUARD)


def leaks(found, canaries=CANARIES) -> list[str]:
    seen = json.dumps(found, default=str) if not isinstance(found, str) else found
    return [c for c in canaries if c in seen]


def test_the_guard_row_routes_through_the_running_guard_and_shows_its_problem(store: Store):
    # given
    seed(store, anomaly(KEY, num=14), history=[])
    app = ready_app(settings(), store, anthropic_client=FakeAnthropic([], []))
    guard = app.state.investigator.guard

    # when
    with TestClient(app) as c:
        off = guard_row(c)
        guard_on(c)
        on = guard_row(c)
        failed = guard.answer('q', 'an answer', '', 'r1')
        broken = (guard_row(c), guard.problem())
        assert c.put('/api/admin/settings', json={'key': 'investigate.guard-model', 'value': UNKEYED}, headers=AUTH).status_code == 200
        unkeyed = (guard_row(c), guard.problem())

    # then
    assert isinstance(guard, Guard)
    assert (off['model'], off['problem']) == ('', None)
    assert (on['model'], on['provider'], on['problem']) == (MODEL, 'anthropic', None)
    assert failed.verdict == 'hold' and failed.failure
    assert broken[0]['problem'] == broken[1] == msg('guard.failed', failure=failed.failure)
    assert (unkeyed[0]['model'], unkeyed[0]['provider']) == (UNKEYED, 'openai')
    assert unkeyed[0]['problem'] == unkeyed[1] == msg('guard.no_key', owner='OpenAI', model=UNKEYED)


def test_held_answers_reach_admins_only_through_every_route(store: Store):
    # given
    seed(store, anomaly(KEY, num=14), history=[])
    fake = FakeAnthropic([reply(text(CARD_ANSWER), stop='end_turn'), reply(text(CHAT_ANSWER), stop='end_turn'), reply(text(PASSED_ANSWER), stop='end_turn')],
                         [said('hold', REASON), said('hold', REASON), said('pass', 'fine')])
    app = ready_app(sso_settings(), store, anthropic_client=fake)
    inv = app.state.investigator
    inv.clock = lambda: NOW
    inv.effects.slack_reply = lambda action, outcome, recap: {'ts': '1', 'text': recap}
    Users(store.db).create(OPERATOR, 'Olga Page', 'operator')
    operator = login_cookie(store, OPERATOR, 'operator')
    tools, _ = tools_of(store)
    viewer = McpUser(name=OPERATOR, sub=OPERATOR, email=OPERATOR, role='operator')

    with TestClient(app) as c:
        guard_on(c)
        slack_action(store, mentionedTicket='OPS-123')
        card = inv.run_once(NOW)
        chat = chat_action(store, question=CHAT_QUESTION, by=OPERATOR)
        held_chat = inv.run_once(NOW)
        chat_action(store, question='Anything new?', chat='c2', by=OPERATOR)
        passed_chat = inv.run_once(NOW)

        # when
        admin = {r: c.get(f"/api/investigations/{r}", headers=AUTH).json() for r in (card['id'], held_chat['id'], passed_chat['id'])}
        admin_held = c.get('/api/investigations', params={'held': 'true'}, headers=AUTH)
        anonymous_held = c.get('/api/investigations', params={'held': 'true'})
        c.cookies.set(SESSION_COOKIE, operator)
        public = {r: c.get(f"/api/investigations/{r}") for r in (card['id'], held_chat['id'])}
        routes = {'runs': c.get('/api/investigations', params={'limit': 200}), 'card runs only': c.get('/api/investigations', params={'chats': 'false'}),
                  'card-runs': c.get('/api/card-runs'), 'activity': c.get('/api/activity', params={'limit': 500}), 'notifications': c.get('/api/notifications'),
                  'requests': c.get('/api/requests'), 'own chat': c.get(f"/api/chats/{chat['chat']['id']}")}
        operator_held = c.get('/api/investigations', params={'held': 'true'})
    mcp = {r: anyio.run(tools.run, 'get_investigation', {'run_id': r}, viewer) for r in (card['id'], held_chat['id'])}
    asks = anyio.run(tools.run, 'list_my_asks', {}, viewer)
    ask = anyio.run(tools.run, 'get_ask', {'chat_id': chat['chat']['id']}, viewer)

    # then
    waiting = msg('slackevents.guard.waiting')
    owner = (CHAT_ANSWER, CARD_ANSWER, REASON)
    assert [store.investigations.find_one({'_id': r['id']})['outcome'] for r in (card, held_chat, passed_chat)] == ['held', 'held', 'done']
    assert admin[card['id']]['held']['text'] == CARD_ANSWER and admin[card['id']]['guard']['reason'] == REASON
    assert admin[card['id']]['held']['proposed']['cardAction']['ticket'] == 'OPS-123'
    held_admin = admin[held_chat['id']]
    assert held_admin['held']['text'] == CHAT_ANSWER and held_admin['guard']['reason'] == REASON
    assert held_admin['ask'] == {'private': True} and held_admin['keys'] == [] and CHAT_QUESTION not in json.dumps(held_admin)
    assert all(set(t) == {'name'} for t in held_admin.get('toolCalls') or [])
    assert 'held' not in admin[passed_chat['id']] and 'guard' not in admin[passed_chat['id']] and PASSED_ANSWER not in json.dumps(admin[passed_chat['id']])
    assert admin_held.status_code == 200 and sorted(r['id'] for r in admin_held.json()) == sorted([card['id'], held_chat['id']])
    assert leaks(admin_held.json()) == [] and all(r['held'] == {'until': r['held']['until']} for r in admin_held.json() if 'held' in r)
    assert anonymous_held.status_code in (401, 403) and operator_held.status_code == 403
    assert all(r.status_code == 200 for r in public.values()) and all(r.status_code == 200 for r in routes.values())
    assert public[card['id']].json()['outcome'] == 'held' and public[card['id']].json()['held'] == {'until': admin[card['id']]['held']['until']}
    assert public[held_chat['id']].json()['outcome'] == 'held' and public[held_chat['id']].json()['ask'] == {'private': True}
    for r in public.values():
        assert leaks(r.json()) == [] and 'reason' not in (r.json().get('guard') or {})
    seen = {k: v.json() for k, v in routes.items()}
    assert {card['id'], held_chat['id']} <= {r['id'] for r in seen['runs']} and card['id'] in {r['id'] for r in seen['card runs only']}
    assert any(a.get('runId') == held_chat['id'] and a.get('outcome') == 'held' for a in seen['activity'])
    assert any(r.get('run', {}).get('id') == card['id'] and r['run']['outcome'] == 'held' and r['result'] == waiting for r in seen['requests'])
    assert [i['note'] for i in seen['notifications']['items']] == ['held']
    assert [(m['held'], m['answer']) for m in seen['own chat']['messages']] == [(True, waiting)]
    for name in ('runs', 'card runs only', 'card-runs', 'activity', 'requests'):
        assert leaks(seen[name]) == [], name
    assert leaks(seen['notifications'], owner) == [] and leaks(seen['own chat'], owner) == []
    assert all(r in text and ' held ' in text for r, text in mcp.items()) and all(leaks(text) == [] for text in mcp.values())
    assert 'c1' in asks and leaks(asks, owner) == []
    assert 'status held' in ask and leaks(ask, owner) == []


SPAN = 'acct-7781-canary'


def test_masked_span_not_in_tool_call_inputs(store: Store):
    # given
    seed(store, anomaly(KEY, num=14), history=[])
    recap = f'Most retries on checkout-service come from {SPAN} and started at 09:04 after the deploy of OPS-123, so the deploy is the likely cause here.'
    fake = FakeAnthropic([reply(tool('card', {'key': f'{KEY} {SPAN}'})), reply(tool('note', {'key': KEY, 'text': f'retries from {SPAN} since 09:04'})), reply(text(recap), stop='end_turn')],
                         [said('mask', 'names an account', spans=[SPAN])])
    app = ready_app(settings(), store, anthropic_client=fake)
    inv = app.state.investigator
    inv.clock = lambda: NOW

    with TestClient(app) as c:
        guard_on(c)
        record = inv.investigate([store.anomaly(KEY)], None, NOW)

        # when
        viewer = c.get(f"/api/investigations/{record['id']}")
        listed = c.get('/api/investigations', params={'chats': 'false'})

    # then
    run = store.investigations.find_one({'_id': record['id']})
    assert run['guard']['answers'] == 'mask' and MASK_TEXT in run['recap']
    assert [t['name'] for t in run['toolCalls']][:2] == ['card', 'note']
    assert viewer.status_code == 200 and SPAN not in json.dumps(viewer.json()) and SPAN not in json.dumps(listed.json())
    assert SPAN not in json.dumps(run, default=str)
    assert store.anomaly(KEY)['note']['text'] == f'retries from {MASK_TEXT} since 09:04'


def test_held_plugin_proposal_args_hidden(store: Store):
    # given
    seed(store, anomaly(KEY, num=14), history=[])
    rig = Rig(store, DEMO, WHERE, PLAIN, NOTES)
    rig.add(DEMO, 'main', primary=True, **URL)
    box = rig.box(ask=True, read_only=True, run_id='r1')
    box.hold_writes = True
    box.call('demo_propose', {'title': 'Restart the demo worker', 'text': f'it holds {SPAN}'})
    box.call('card', {'key': f'{KEY} {SPAN}'})
    inv, _ = guarded(store, [], FakeGuard(answers=True, answer=check('hold')))
    done = {'outcome': 'done', 'recap': f'Restart it, {SPAN}.', 'toolCalls': box.calls, 'writes': box.writes, 'flags': [], 'proposal': box.plugin_proposal}

    # when
    held = inv._held(done, slack_action(store), box, done['recap'])

    # then
    assert held['outcome'] == 'held' and SPAN in json.dumps(held['held']['proposed'])
    assert [c['name'] for c in held['toolCalls']] == ['demo_propose', 'card'] and all(c['input'] == '' for c in held['toolCalls'])
    assert SPAN not in json.dumps(public_run(held), default=str)


def test_cannot_clear_model_while_checks_on(store: Store):
    # given
    seed(store, anomaly(KEY, num=14), history=[])
    c = TestClient(ready_app(settings(), store))
    guard_on(c)

    # when
    emptied = c.put('/api/admin/settings', json={'key': 'investigate.guard-model', 'value': ''}, headers=AUTH)
    reset = c.delete('/api/admin/settings', params={'key': 'investigate.guard-model'}, headers=AUTH)
    still = guard_row(c)['model']
    off = c.put('/api/admin/settings', json={'key': 'guard.check-answers', 'value': 'false'}, headers=AUTH)
    cleared = c.put('/api/admin/settings', json={'key': 'investigate.guard-model', 'value': ''}, headers=AUTH)
    env_store = Store(store.db.client['env'], release_ignore=store.release_ignore, self_deploy='platform/skopax')
    Users(env_store.db).create('root@example.com', 'Root', 'admin')
    TestClient(ready_app(dataclasses.replace(settings(), guard_check_answers=True), env_store))
    changes = [r for r in env_store.list_activity(None, 100) if r['kind'] == 'admin']

    # then
    assert (emptied.status_code, emptied.json()['detail']) == (422, msg('guard.checks_need_model'))
    assert (reset.status_code, reset.json()['detail']) == (422, msg('guard.checks_need_model'))
    assert still == MODEL and off.status_code == 200 and cleared.status_code == 200
    assert [r['text'] for r in changes] == [msg('guard.checks_off_no_model')] and changes[0]['who'] == 'server'


def test_held_run_screenshots_admin_only(store: Store):
    # given
    shot = {'id': 'shot-canary-1', 'url': 'https://shop.example.com/checkout', 'at': '2026-11-26T10:00:00Z', 'kb': 1.0, 'link': '/api/screenshots/shot-canary-1'}
    run = held_run() | {'screenshots': [shot], 'held': held_run()['held'] | {'claim': 'tok', 'applied': {'0': 'note applied'}}}
    store.add_investigation(run)
    store.add_screenshot({'_id': 'shot-canary-1', 'runId': 'r1', 'keys': [CARD], 'at': shot['at'], 'size': 3, 'mime': 'image/png', 'data': b'png'})
    store.add_activity('run', 'investigator', 'run held', runId='r1', outcome='held', screenshots=[shot])
    app = create_app(sso_settings(), store)
    Users(store.db).create(OPERATOR, 'Olga Page', 'operator')

    with TestClient(app) as c:
        # when
        admin = [c.get('/api/investigations/r1', headers=AUTH).json(), c.get('/api/screenshots/shot-canary-1', headers=AUTH)]
        c.cookies.set(SESSION_COOKIE, login_cookie(store, OPERATOR, 'operator'))
        viewer = [c.get('/api/investigations/r1').json(), c.get('/api/investigations', params={'chats': 'false'}).json(), c.get('/api/activity').json()]
        image = c.get('/api/screenshots/shot-canary-1')

    # then
    assert admin[0]['screenshots'] == [shot] and admin[1].status_code == 200
    assert 'shot-canary-1' not in json.dumps(viewer) and image.status_code == 404
    assert set(viewer[0]['held']) == {'until'}


def test_tool_error_text_not_in_activity_when_guarded(store: Store):
    # given
    seed(store, anomaly(KEY, num=14), history=[])
    fake = FakeAnthropic([reply(tool('card', {'key': f'{KEY} {SPAN}'})), reply(text('checkout-service retries went up after the deploy.'), stop='end_turn')], [said('hold', REASON)])
    app = ready_app(sso_settings(), store, anthropic_client=fake)
    inv = app.state.investigator
    inv.clock = lambda: NOW
    Users(store.db).create(OPERATOR, 'Olga Page', 'operator')

    with TestClient(app) as c:
        guard_on(c)
        record = inv.investigate([store.anomaly(KEY)], None, NOW)

        # when
        c.cookies.set(SESSION_COOKIE, login_cookie(store, OPERATOR, 'operator'))
        activity = c.get('/api/activity', params={'limit': 500})
        history = c.get('/api/card-history', params={'key': KEY})

    # then
    rows = [r for r in store.list_activity(None, 500) if r.get('runId') == record['id'] and r.get('kind') == 'tool']
    assert store.investigations.find_one({'_id': record['id']})['outcome'] == 'held'
    assert rows and rows[0]['tool'] == 'card' and rows[0]['error'] == msg('guard.tool_failed')
    assert activity.status_code == 200 and history.status_code == 200
    assert SPAN not in json.dumps(activity.json()) and SPAN not in json.dumps(history.json())
    assert SPAN not in json.dumps(store.list_activity(None, 500), default=str)


def test_watch_status_reaches_admins_only(store: Store):
    # given
    watched = held_run() | {'id': 'rw', 'outcome': 'done', 'held': None, 'guard': {'questions': 'pass', 'answers': 'hold', 'masked': 0, 'watch': True, 'noted': True, 'reason': 'x'}}
    fixed = held_run() | {'id': 'rf', 'outcome': 'done', 'held': None, 'guard': {'questions': None, 'answers': 'pass', 'masked': 0, 'outcome': 'fixed', 'first': {'answers': 'hold'}}}
    for run in (watched, fixed):
        run.pop('held')
        store.add_investigation(run)
    app = create_app(sso_settings(), store)
    Users(store.db).create(OPERATOR, 'Olga Page', 'operator')

    # when
    with TestClient(app) as c:
        admin = {r['id']: r for r in c.get('/api/investigations', headers=AUTH).json()}
        admin_one = c.get('/api/investigations/rw', headers=AUTH).json()
        c.cookies.set(SESSION_COOKIE, login_cookie(store, OPERATOR, 'operator'))
        viewer = {r['id']: r for r in c.get('/api/investigations').json()}
        viewer_one = c.get('/api/investigations/rw').json()

    # then
    assert admin['rw']['guard']['watch'] is True and admin['rf']['guard']['outcome'] == 'fixed' and admin_one['guard']['watch'] is True
    assert 'guard' not in viewer['rw'] and 'guard' not in viewer_one
    assert viewer['rf']['guard'] == {'questions': None, 'answers': 'pass', 'masked': 0}


def test_watch_mode_from_the_environment_leaves_one_changes_row(store: Store):
    # given
    Users(store.db).create('root@example.com', 'Root', 'admin')

    # when
    TestClient(ready_app(dataclasses.replace(settings(), guard_mode='watch'), store))
    other = Store(store.db.client['other'], release_ignore=store.release_ignore, self_deploy='platform/skopax')
    TestClient(ready_app(settings(), other))
    rows = [r['text'] for r in store.list_activity(None, 100) if r['kind'] == 'admin']
    quiet = [r['text'] for r in other.list_activity(None, 100) if r['kind'] == 'admin']

    # then
    assert rows == [msg('guard.watch_at_startup')] and quiet == []
