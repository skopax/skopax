import datetime as dt

from fastapi.testclient import TestClient

from tests.conftest import anomaly, settings, state
from tests.fake_anthropic import reply, text, tool
from tests.test_assign import assigner
from tests.test_auth import ROLE, TokenEndpoint, access_token, app_client, log_in
from tests.test_chat import chat_investigator
from tests.test_investigator import NOW, seed
from tests.test_mcp import call, mcp_client, numbered, token
from tests.test_slack import FakeSlack
from skopax.app import create_app
from skopax.notices import Notices
from skopax.store import Store, utc_now

CARD = 'log|checkout-service|ERROR|boom'
OTHER = 'log|payments-api|ERROR|npe'
NINA = 'nina.white@example.com'
PAUL = 'paul.stone@example.com'
SARA = 'sara.bell@example.com'


def board(store: Store, *cards: dict) -> None:
    store.save({**state(*cards), 'initialized': True}, None)
    numbered(store, *cards)


def inbox(store: Store, user: str) -> list[dict]:
    return store.notices_of(user, 50)


def kinds(store: Store, user: str) -> list[str]:
    return [n['kind'] for n in inbox(store, user)][::-1]


def page_client(store: Store, sub: str) -> TestClient:
    return app_client(store, TokenEndpoint(access_token([ROLE], sub=sub)))


def test_the_assignee_hears_about_the_assignment_and_the_one_it_moved_away_from_hears_too_but_nobody_about_their_own_move(store: Store):
    # given
    board(store, anomaly(CARD, num=14, svc='checkout-service', label='ERROR lines'))
    a = assigner(store, FakeSlack())
    a.notices = Notices(store)

    # when
    a.assign(CARD, 'Nina White', NINA, PAUL)
    a.assign(CARD, 'Nina White', NINA, PAUL)
    a.assign(CARD, 'Sara Bell', None, NINA)
    a.assign(CARD, 'Paul Stone', PAUL, PAUL)

    # then
    assert kinds(store, 'nina.white') == ['assigned'], 'once, though the same assignment came twice; she moved it away herself, so no unassigned for her'
    assert kinds(store, 'sara.bell') == ['assigned', 'unassigned'], 'a display name maps to the login handle'
    assert kinds(store, 'paul.stone') == [], 'taking it yourself is your own action'
    [row] = inbox(store, 'nina.white')
    assert (row['num'], row['title'], row['by'], row['read'], row['key']) == (14, 'checkout-service / ERROR lines', 'paul.stone', False, CARD)


def test_status_changes_on_a_held_card_reach_its_holder_from_the_page_mcp_and_the_server_but_not_from_the_holder(store: Store):
    # given
    board(store, anomaly(CARD, num=14, owner='Nina White', sev='warn'), anomaly(OTHER, num=15, owner='Nina White', sev='crit'))
    notices = Notices(store)

    def act(action: dict) -> None:
        seen = notices.watch(action)
        if action['type'] == 'severity':
            store.set_severity(action['key'], action['sev'], action['requestedBy'])
        elif action['type'] == 'take':
            store.triage(action['key'], 'progress', ticket=action.get('ticket'), owner=action['requestedBy'])
        elif action['type'] == 'resolve':
            store.triage(action['key'], 'handled', resolution={'kind': 'fixed', 'ref': 'OPS-12', 'by': action['requestedBy'], 'via': 'page'})
        elif action['type'] == 'group':
            store.group(action['parent'], [action['key']])
        else:
            store.mute(action['key'], 'x', None, 'known') if action['type'] == 'known' else None
        notices.applied(action, seen)

    # when
    act({'type': 'severity', 'key': CARD, 'sev': 'crit', 'requestedBy': PAUL})
    act({'type': 'resolve', 'key': CARD, 'requestedBy': NINA})
    act({'type': 'take', 'key': OTHER, 'ticket': 'OPS-40', 'requestedBy': PAUL})
    act({'type': 'known', 'key': 'log|*|ERROR|*', 'requestedBy': SARA})
    act({'type': 'group', 'key': OTHER, 'parent': CARD, 'requestedBy': SARA})

    # then
    rows = {(n['kind'], n['key']): n for n in inbox(store, 'nina.white')}
    assert set(rows) == {('severity', CARD), ('ticket', OTHER), ('known', CARD)}, 'her own resolve is not news to her; the take moved the card to Paul, so the later changes go to him'
    assert rows[('severity', CARD)]['note'] == 'warn → crit' and rows[('ticket', OTHER)]['note'] == 'OPS-40 · in progress' and rows[('ticket', OTHER)]['text'].startswith('Jira ticket linked · OPS-40 · in progress · by ')
    assert rows[('known', CARD)]['text'] == 'Marked as known noise · by sara.bell'
    assert {(n['kind'], n['note']) for n in inbox(store, 'paul.stone')} == {('known', ''), ('merged', '#14')}


def test_taking_a_card_tells_the_last_holder_and_the_new_holder_hears_nothing_of_their_own_mute(store: Store):
    # given
    board(store, anomaly(CARD, num=14, sev='warn', owner='Nina White', assigned={'by': SARA, 'at': '2026-11-12T08:00:00Z', 'email': NINA}))

    # when
    with mcp_client(store) as c:
        call(c, token(sub=PAUL), 'take_card', {'card': 14, 'reason': 'looking into it'})
        call(c, token(sub=PAUL), 'mute_card', {'card': 14, 'hours': 2})

    # then
    assert kinds(store, 'nina.white') == ['progress'], 'Paul took Nina\'s card, so she hears it moved to him; the mute is his own card now'
    assert kinds(store, 'paul.stone') == []
    assert store.anomaly(CARD)['owner'] in ('Paul Stone', PAUL)


def test_a_burst_is_one_notification_and_a_flapping_card_tells_its_holder_once_an_hour(store: Store):
    # given
    board(store, anomaly(CARD, num=14, owner=NINA))
    notices = Notices(store)
    card = store.anomaly(CARD)
    events = [{'key': CARD, 'ev': 'reopened', 'muted': False}, {'key': CARD, 'ev': 'cleared', 'muted': False}]

    # when
    for _ in range(3):
        notices.posted(CARD, PAUL, thread=True, text='we are on it')
        notices.fired(events, [card])
    store.notices.update_many({}, {'$set': {'at': utc_now() - dt.timedelta(minutes=5)}})
    notices.posted(CARD, PAUL, thread=True, text='fixed now')
    notices.fired(events, [card])

    # then
    assert sorted(kinds(store, 'nina.white')) == ['fired', 'stopped', 'thread', 'thread'], 'a thread update five minutes later is news again; fired and stopped wait an hour'


def test_engine_events_skip_muted_and_merged_cards_and_auto_resolve_jira_and_reminders_reach_the_holder(store: Store):
    # given
    board(store, anomaly(CARD, num=14, owner=NINA, triage='handled'), anomaly(OTHER, num=15, owner=NINA, parent=CARD), anomaly('log|x|ERROR|y', num=16, owner=NINA))
    notices = Notices(store)
    cards = store.load()['anomalies']

    # when
    notices.fired([{'key': OTHER, 'ev': 'reopened', 'muted': False}, {'key': 'log|x|ERROR|y', 'ev': 'escalated', 'muted': True}], cards)
    notices.auto_resolved([CARD])
    notices.jira_swept([{'card': CARD, 'issue': 'OPS-7', 'closed': 'Done'}, {'card': 'log|x|ERROR|y', 'issue': 'OPS-8'}])
    notices.reminded(['log|x|ERROR|y'])

    # then
    rows = [(n['kind'], n['key'], n['note'], n['by']) for n in inbox(store, 'nina.white')][::-1]
    assert rows == [('auto', CARD, '', 'Skopax'), ('resolved', CARD, 'OPS-7 Done', 'Jira link sweep'), ('ticket', 'log|x|ERROR|y', 'OPS-8 · in progress', 'Jira link sweep'),
                    ('reminder', 'log|x|ERROR|y', '', 'Skopax')]


def test_a_finished_dive_tells_the_holder_and_the_requester_and_a_chat_answer_tells_only_the_asker(store: Store):
    # given
    board(store, anomaly(CARD, num=14, owner=NINA))
    notices = Notices(store)

    # when
    notices.run_done([CARD], {'type': 'dive', 'key': CARD, 'requestedBy': SARA}, 'done', 'PayCo gives 502.\nDetails...')
    notices.run_done([CARD], None, 'failed', '')
    notices.run_done([], {'type': 'ask', 'source': 'chat', 'text': 'Why did it go up?', 'requestedBy': PAUL, 'chat': {'id': 'c9'}}, 'done', 'One client.')
    notices.run_done([], {'type': 'ask', 'source': 'slack', 'text': 'x', 'requestedBy': PAUL}, 'done', 'y')

    # then
    assert [(n['kind'], n['note']) for n in inbox(store, 'sara.bell')] == [('run', 'PayCo gives 502.')]
    assert [(n['kind'], n['note']) for n in inbox(store, 'nina.white')] == [('run', 'PayCo gives 502.')], 'the failed auto run a second later is the same burst'
    [answer] = inbox(store, 'paul.stone')
    assert (answer['kind'], answer['title'], answer['note'], answer['link'], answer['num']) == ('answer', 'Why did it go up?', 'One client.', '#t=chat/thread/c9', None)


def test_an_mcp_ask_answered_by_the_investigator_lands_in_the_askers_bell(store: Store, tmp_path):
    # given
    seed(store, anomaly(CARD, num=14), history=[])
    numbered(store, anomaly(CARD, num=14))
    with mcp_client(store, tmp_path=tmp_path, ready=True) as c:
        call(c, token(sub=PAUL), 'ask', {'question': 'Why did it go up?', 'cards': [14], 'wait_seconds': 0})
    inv = chat_investigator(store, [reply(tool('card', {'key': CARD})), reply(text('One client.'), stop='end_turn')])
    inv.notices = Notices(store)

    # when
    inv.run_once(NOW)

    # then
    [row] = inbox(store, 'paul.stone')
    assert (row['kind'], row['note'], row['by']) == ('answer', 'One client.', 'Investigator') and row['link'].startswith('#t=chat/thread/')


def test_the_bell_api_lists_only_your_own_marks_them_read_and_needs_a_login(store: Store):
    # given
    board(store, anomaly(CARD, num=14, owner=NINA), anomaly(OTHER, num=15, owner=SARA))
    notices = Notices(store)
    notices.posted(CARD, PAUL)
    notices.reminded([CARD])
    notices.posted(OTHER, PAUL)
    anonymous = TestClient(create_app(settings(), store))

    # when
    with page_client(store, NINA) as c:
        log_in(c)
        first = c.get('/api/notifications').json()
        count = c.get('/api/notifications/count').json()
        newest = first['items'][0]['id']
        one = c.post('/api/notifications/read', json={'ids': [newest]}).json()
        older = c.get('/api/notifications', params={'before': newest, 'limit': 1}).json()
        none = c.get('/api/notifications', params={'before': 'not-mine'}).json()
        rest = c.post('/api/notifications/read', json={'all': True}).json()
        bad = c.post('/api/notifications/read', json={}).status_code
        both = c.post('/api/notifications/read', json={'ids': ['x'], 'all': True}).status_code
        theirs = c.post('/api/notifications/read', json={'ids': [n['id'] for n in inbox(store, 'sara.bell')]}).json()
    with anonymous as c:
        locked = (c.get('/api/notifications').status_code, c.get('/api/notifications/count').status_code, c.post('/api/notifications/read', json={'all': True}).status_code)

    # then
    assert [n['kind'] for n in first['items']] == ['reminder', 'slack'] and first['unread'] == 2 and count == {'unread': 2}
    assert all('user' not in n and 'at' not in n for n in first['items'])
    assert one == {'read': 1, 'unread': 1}
    assert [n['kind'] for n in older['items']] == ['slack'] and none['items'] == [], 'before is the id of the last one shown'
    assert rest == {'read': 1, 'unread': 0} and (bad, both) == (422, 422)
    assert theirs == {'read': 0, 'unread': 0} and [n['read'] for n in inbox(store, 'sara.bell')] == [False], "someone else's ids are not yours to mark"
    assert locked == (401, 401, 401)


def test_list_notifications_over_mcp_reads_the_same_inbox_with_links(store: Store):
    # given
    board(store, anomaly(CARD, num=14, owner=NINA, svc='checkout-service', label='ERROR lines'))
    notices = Notices(store)
    notices.posted(CARD, PAUL)
    notices.run_done([], {'type': 'ask', 'source': 'chat', 'text': 'Why?', 'requestedBy': NINA, 'chat': {'id': 'c1'}}, 'done', 'One client.')

    # when
    with mcp_client(store) as c:
        listed, failed = call(c, token(sub=NINA), 'list_notifications', {'limit': 5})
        empty, _ = call(c, token(sub=SARA), 'list_notifications', {'unread_only': True})

    # then
    assert not failed and listed.splitlines()[0] == '2 unread'
    assert '- unread · Chat answer ready Why? · One client. · by Investigator' in listed and 'https://skopax.example.com/#t=chat/thread/c1' in listed
    assert '- unread · Posted to Slack · #14 checkout-service / ERROR lines · by paul.stone' in listed and 'https://skopax.example.com/c/14' in listed
    assert empty == 'No notifications unread. 0 unread in all.'


def test_a_linked_jira_ticket_is_its_own_notification_whether_or_not_the_card_moves_to_in_progress():
    # given
    from skopax.notices import changes
    open_card = {'key': 'k', 'triage': None, 'ticket': None}

    # when
    took = changes(open_card, {'key': 'k', 'triage': 'progress', 'ticket': 'OPS-1'})
    linked = changes({'key': 'k', 'triage': 'progress', 'ticket': None}, {'key': 'k', 'triage': 'progress', 'ticket': 'OPS-2'})
    plain = changes(open_card, {'key': 'k', 'triage': 'progress', 'ticket': None})
    same = changes({'key': 'k', 'triage': None, 'ticket': 'OPS-3'}, {'key': 'k', 'triage': 'progress', 'ticket': 'OPS-3'})

    # then
    assert took == [('ticket', 'OPS-1 · in progress')]
    assert linked == [('ticket', 'OPS-2')]
    assert plain == [('progress', '')], 'In progress with no ticket stays In progress'
    assert same == [('progress', 'OPS-3')], 'a ticket that was already there is not news: it is In progress, naming the ticket'
