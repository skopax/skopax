import dataclasses

import pytest
from fastapi.testclient import TestClient

from tests.conftest import AUTH, anomaly, ready_app, settings, state
from tests.fake_anthropic import FakeAnthropic, reply, text, tool
from tests.test_investigator import KEY, NOW, seed
from tests.test_investigator_slack import first_brief
from tests.test_investigator_tools import sources
from skopax import chatui
from skopax.chatui.text import escaped, refusal
from skopax.app import create_app
from skopax.config import Settings
from skopax.investigator import Investigator, prompt
from skopax.investigator.tools import WRITE_NAMES, Effects
from skopax.kube import Kube
from skopax.messages import msg
from skopax.store import Store

ANSWER = '*checkout-service*: one client, 40 lines (esql, 14:05 UTC).'
NINA = 'nina.white@example.com'
PAUL = 'paul.stone@example.com'


def chat_action(store: Store, question: str = 'Why did it go up?', chat: str = 'c1', cards=(KEY,), by: str = NINA, **extra) -> dict:
    return store.add_action({'type': 'ask', 'source': 'chat', 'key': 'ask', 'target': f'chat:{chat}', 'text': question, 'cards': list(cards), 'label': question, 'requestedBy': by,
                             'chat': {'id': chat}, **extra})


def chat_investigator(store: Store, replies: list, **over) -> Investigator:
    s = dataclasses.replace(settings(), anthropic_api_key='sk-test', investigate_mode='live', **over)
    return Investigator(store, sources(), Effects(lambda key, by: {}, lambda key, text, by, *_, notice=None: {}, lambda *a: None), s, client=FakeAnthropic(replies), clock=lambda: NOW)


def test_a_chat_message_runs_read_only_on_the_chat_model_with_the_earlier_messages_and_its_answer_lands_in_the_chat(store: Store):
    # given
    seed(store, anomaly(KEY, num=14), history=[])
    act = chat_action(store, thread=[{'who': 'person', 'text': 'What is this card?'}, {'who': 'bot', 'text': 'checkout-service ERROR 40'}])
    inv = chat_investigator(store, [reply(tool('note', {'key': KEY, 'text': 'x'})), reply(text(ANSWER), stop='end_turn')], chat_model='claude-sonnet-5-5')

    # when
    record = inv.run_once(NOW)

    # then
    offered = {t['name'] for t in inv.client.requests[0]['tools']}
    assert not offered & WRITE_NAMES and {'propose_card_action', 'propose_investigation'} <= offered and 'propose_ticket' not in offered
    brief = first_brief(inv)
    assert brief.startswith(f'Chat ask by {NINA} on the Chat page of Skopax (action {act["id"]}).')
    assert '> person: What is this card?\n> bot: checkout-service ERROR 40' in brief and brief.count('Why did it go up?') == 1
    assert (record['trigger'], record['model']) == ('chat', 'claude-sonnet-5-5') and inv.client.requests[0]['model'] == 'claude-sonnet-5-5'
    [message] = store.chat(NINA, 'c1')['messages']
    assert (message['text'], message['status'], message['answer'], message['cards']) == ('Why did it go up?', 'done', ANSWER, [KEY])
    assert store.anomaly(KEY).get('note') is None


def test_a_chat_can_propose_an_investigation_of_a_card_that_an_operator_starts_with_a_button(store: Store):
    # given
    seed(store, anomaly(KEY, num=14), history=[])
    chat_action(store, 'look at this card')
    inv = chat_investigator(store, [reply(tool('propose_investigation', {'key': KEY})), reply(text('I suggested looking at the card.'), stop='end_turn')])

    # when
    inv.run_once(NOW)

    # then
    [message] = store.chat(NINA, 'c1')['messages']
    assert message['cardAction'] == {'key': KEY, 'action': 'investigate'} and message['answer'] == 'I suggested looking at the card.'
    assert not store.list_actions('requested', 10), 'nothing is queued until an operator clicks'


def test_chats_belong_to_the_person_who_wrote_them_and_a_pending_message_shows_the_run_progress(store: Store):
    # given
    seed(store, anomaly(KEY, num=14), history=[])
    first = chat_action(store, 'First question', chat='c1')
    store.update_action(first['id'], 'done', 'short answer', 'requested')
    chat_action(store, 'Another chat', chat='c2', cards=())
    chat_action(store, 'A question from Paul', chat='c3', by=PAUL)
    store.add_action({'type': 'ask', 'source': 'page', 'key': 'ask', 'target': 'ask', 'text': 'page ask', 'cards': [], 'requestedBy': NINA})

    # when
    mine, other = store.chats(NINA, 20), store.chats(PAUL, 20)
    c1 = store.chat(NINA, 'c1')
    foreign = store.chat(PAUL, 'c1')

    # then
    assert [(c['id'], c['title'], c['messages'], c['pending']) for c in mine] == [('c2', 'Another chat', 1, True), ('c1', 'First question', 1, False)]
    assert [c['id'] for c in other] == ['c3']
    assert c1['messages'][0]['answer'] == 'short answer' and foreign is None
    assert [r['text'] for r in store.requests(20)] == ['page ask'], 'chat messages stay out of the Requests tab'


def test_a_logged_in_person_chats_with_a_daily_cap_one_message_at_a_time_and_only_about_the_product(store: Store, tmp_path):
    # given
    store.save({**state(anomaly(KEY, num=14)), 'initialized': True}, None)
    s = dataclasses.replace(settings(), chat_daily_cap=2)

    # when
    with TestClient(ready_app(s, store, kube=Kube(root=tmp_path))) as c:
        anonymous = c.post('/api/chats', json={'text': 'x'})
        empty = c.post('/api/chats', json={'text': '   '}, headers=AUTH)
        first = c.post('/api/chats', json={'text': 'Why is this card open?', 'cards': [KEY]}, headers=AUTH)
        chat_id = first.json()['id']
        busy = c.post('/api/chats', json={'text': 'second', 'chatId': chat_id}, headers=AUTH)
        asked = store.list_actions('requested', 10)[0]
        store.update_action(asked['id'], 'done', 'answer', 'requested')
        second = c.post('/api/chats', json={'text': 'And then?', 'chatId': chat_id}, headers=AUTH)
        store.update_action(store.list_actions('requested', 10)[0]['id'], 'done', 'answer 2', 'requested')
        capped = c.post('/api/chats', json={'text': 'third', 'chatId': chat_id}, headers=AUTH)
        unknown = c.post('/api/chats', json={'text': 'x', 'chatId': 'nope'}, headers=AUTH)
        listed = c.get('/api/chats', headers=AUTH).json()
        read = c.get(f'/api/chats/{chat_id}', headers=AUTH).json()
        missing = c.get('/api/chats/nope', headers=AUTH)

    # then
    assert (anonymous.status_code, empty.status_code, first.status_code, busy.status_code, second.status_code, capped.status_code, unknown.status_code, missing.status_code) == (401, 422, 201, 409, 201, 429, 404, 404)
    assert 'wait for the answer' in busy.json()['detail'] and '2 chat messages a day' in capped.json()['detail']
    assert (asked['source'], asked['requestedBy'], asked['cards'], asked['chat']) == ('chat', 'token:bootstrap', [KEY], {'id': chat_id})
    follow = store.get_action(read['messages'][1]['id'])
    assert follow['thread'] == [{'who': 'person', 'text': 'Why is this card open?'}, {'who': 'bot', 'text': 'answer'}] and follow['cards'] == [KEY], 'a follow-up keeps the chat card and carries the earlier messages'
    assert (listed['cap'], listed['usedToday'], listed['model']) == (2, 2, s.chat_model) and [x['id'] for x in listed['chats']] == [chat_id]
    assert [(m['text'], m['answer']) for m in read['messages']] == [('Why is this card open?', 'answer'), ('And then?', 'answer 2')]
    assert read['cards'] == [{'key': KEY, 'num': 14, 'title': 'svc / x'}] or read['cards'][0]['key'] == KEY


def test_a_chat_stays_private_on_every_page_that_lists_runs_actions_and_activity(store: Store, tmp_path):
    # given
    seed(store, anomaly(KEY, num=14), history=[])
    act = chat_action(store, 'Secret question: why did client 42 get an error?')
    inv = chat_investigator(store, [reply(tool('card', {'key': 'no|such|card'})), reply(text('Secret answer: client 42.'), stop='end_turn')])
    record = inv.run_once(NOW)
    waiting = chat_action(store, 'A pending secret question', chat='c9')

    # when
    with TestClient(create_app(settings(), store, kube=Kube(root=tmp_path))) as c:
        pages = [c.get('/api/investigations').text, c.get(f"/api/investigations/{record['id']}").text, c.get('/api/activity').text, c.get('/api/actions?limit=500').text,
                 c.get('/api/requests').text, c.get('/api/investigator/status').text, c.get('/api/tool-usage').text]
        run = c.get(f"/api/investigations/{record['id']}").json()
    board = inv.board(NOW)

    # then
    for page in pages + [str(board)]:
        assert 'Secret' not in page and 'client 42' not in page and 'no|such|card' not in page
    assert run['ask'] == {'private': True} and run['trigger'] == 'chat' and [t['name'] for t in run['toolCalls']] == ['card'] and 'recap' not in run
    assert [w['text'] for w in board['waiting'] if w['id'] == waiting['id']] == ['Chat message (private)']
    assert store.past_runs(key=KEY) == [], 'later runs on the card do not read a private chat'
    assert store.chat(NINA, 'c1')['messages'][0]['answer'] == 'Secret answer: client 42.', 'the owner still reads it in the chat'
    assert act['id']


def test_the_owner_shares_a_chat_by_link_for_logged_in_readers_and_takes_it_back(store: Store, tmp_path):
    # given
    store.save({**state(anomaly(KEY, num=14)), 'initialized': True}, None)
    first = chat_action(store, 'A question to share', chat='c1', by='token:bootstrap')
    store.update_action(first['id'], 'done', 'answer', 'requested')
    chat_action(store, 'A question from someone else', chat='c2', by=PAUL)

    # when
    with TestClient(create_app(settings(), store, kube=Kube(root=tmp_path))) as c:
        shared = c.put('/api/chats/c1/share', json={'on': True}, headers=AUTH)
        not_mine = c.put('/api/chats/c2/share', json={'on': True}, headers=AUTH)
        anonymous = c.get('/api/chats/c1')
        mine = c.get('/api/chats/c1', headers=AUTH).json()
        foreign_before = c.get('/api/chats/c2', headers=AUTH)
    as_reader_shared = store.chat_for(PAUL, 'c1')
    store.share_chat('token:bootstrap', 'c1', False)
    as_reader_private = store.chat_for(PAUL, 'c1')

    # then
    assert (shared.status_code, not_mine.status_code, anonymous.status_code, foreign_before.status_code) == (200, 404, 401, 404)
    assert shared.json()['shared'] is True and mine['shared'] is True and mine['mine'] is True
    assert as_reader_shared['mine'] is False and as_reader_shared['by'] == 'token:bootstrap' and as_reader_shared['messages'][0]['answer'] == 'answer'
    assert as_reader_private is None
    reply_to_shared = store.chat(PAUL, 'c1')
    assert reply_to_shared is None, 'a reader cannot write into a shared chat'


def test_card_runs_are_told_the_card_language_a_locale_registered_and_asks_and_english_are_not(store: Store):
    # given
    note = 'Language: write everything that lands on a card or a deploy report in German.'
    prompt.use_writing(lambda: {'de': note})
    seed(store, anomaly(KEY, num=14))
    german = chat_investigator(store, [reply(text('OK.'), stop='end_turn')], investigate_language='de')
    german.run_once(NOW)
    chat_action(store, 'Why did it go up?', chat='c5')
    asked = chat_investigator(store, [reply(text(ANSWER), stop='end_turn')], investigate_language='de')
    asked.run_once(NOW)

    # then
    assert Settings(mongo_uri='mongodb://unused', mongo_db='skopax', token='t', commit_id='c').investigate_language == 'en', 'English is the default'
    assert first_brief(german).rstrip().endswith(note)
    assert prompt.language_note('en') == '' and prompt.language_note('') == '' and prompt.language_note('DE') == '\n\n' + note
    assert 'Language: write' not in first_brief(asked), 'an answer follows the language of the question'


def test_a_chat_message_is_refused_with_the_reason_while_the_investigator_is_not_ready(store: Store, tmp_path):
    # given
    store.save({**state(anomaly(KEY, num=14)), 'initialized': True}, None)
    fake = FakeAnthropic([])
    scenes = {'no-key': dataclasses.replace(settings(), investigate_mode='live'), 'mode-off': dataclasses.replace(settings(), anthropic_api_key='sk-test')}

    # when
    refused = {}
    for name, s in scenes.items():
        app = create_app(s, store, anthropic_client=fake, kube=Kube(root=tmp_path))
        with TestClient(app) as c:
            page = c.post('/api/chats', json={'text': 'Why is this card open?'}, headers=AUTH)
        with pytest.raises(chatui.Refused) as chat_page:
            app.state.chat_bridge.post(NINA, 'Why is this card open?', 'aaaabbbb-cccc-4ddd-8eee-ffff00003333', None)
        refused[name] = (page.status_code, page.json()['detail'], chat_page.value.code, str(chat_page.value), app.state.intake.not_ready(), refusal(chat_page.value, 'en'))
    with TestClient(ready_app(settings(), store, kube=Kube(root=tmp_path))) as c:
        accepted = c.post('/api/chats', json={'text': 'Why is this card open?'}, headers=AUTH)

    # then
    no_key = ' '.join([msg('chat.not_ready'), msg('features.needs.anthropic_key')])
    mode_off = ' '.join([msg('chat.not_ready'), msg('features.needs.mode')])
    no_key_md = ' '.join([msg('chat.not_ready'), escaped(msg('features.needs.anthropic_key'))])
    mode_off_md = ' '.join([msg('chat.not_ready'), escaped(msg('features.needs.mode'))])
    assert refused == {'no-key': (409, no_key, 'not-ready', no_key, no_key, no_key_md), 'mode-off': (409, mode_off, 'not-ready', mode_off, mode_off, mode_off_md)}, 'the chat refusal names the needs again, escaped for Markdown'
    assert 'AI investigator' in no_key
    assert accepted.status_code == 201 and [a['requestedBy'] for a in store.list_actions(None, 10)] == ['token:bootstrap']
