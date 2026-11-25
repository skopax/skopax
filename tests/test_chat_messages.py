import dataclasses

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tests.conftest import AUTH, anomaly, ready_app, settings, state
from tests.fake_anthropic import FakeAnthropic, verdict
from skopax import chatui
from skopax.app import create_app
from skopax.chatui.text import refusal
from skopax.kube import Kube
from skopax.store import Store

KEY = 'log|checkout-service|ERROR|boom'
NINA = 'nina.white@example.com'
THREAD = 'aaaabbbb-cccc-4ddd-8eee-ffff00001111'
EVER = '2000-01-01T00:00:00Z'


def gated(store: Store, tmp_path, gate_replies: list, **over) -> tuple[FastAPI, FakeAnthropic]:
    store.save({**state(anomaly(KEY, num=14)), 'initialized': True}, None)
    fake = FakeAnthropic([], gate_replies)
    return ready_app(dataclasses.replace(settings(), **over), store, anthropic_client=fake, kube=Kube(root=tmp_path)), fake


def test_an_off_topic_chat_message_counts_toward_the_daily_cap_and_not_toward_the_small_talk_cap(store: Store, tmp_path):
    # given
    app, fake = gated(store, tmp_path, [verdict(on_topic=False, reason='asks for a poem') for _ in range(3)], chat_daily_cap=2)

    # when
    with TestClient(app) as c:
        first = c.post('/api/chats', json={'text': 'Write me a poem'}, headers=AUTH)
        second = c.post('/api/chats', json={'text': 'One more poem', 'chatId': first.json()['id']}, headers=AUTH)
        third = c.post('/api/chats', json={'text': 'One last one', 'chatId': first.json()['id']}, headers=AUTH)
        listed = c.get('/api/chats', headers=AUTH).json()

    # then
    assert (first.status_code, second.status_code, third.status_code) == (201, 201, 429)
    assert first.json()['messages'][-1]['answer'] == 'Only questions about Skopax and the production systems Skopax monitors: asks for a poem'
    assert listed['usedToday'] == 2 and store.count_small_talk(EVER) == 0
    assert len(fake.gate_requests) == 2, 'a message over the cap costs no gate call'


def test_a_chat_follow_up_reaches_the_gate_with_the_earlier_messages_and_a_page_ask_without_them(store: Store, tmp_path):
    # given
    app, fake = gated(store, tmp_path, [verdict(), verdict(), verdict()])

    # when
    with TestClient(app) as c:
        first = c.post('/api/chats', json={'text': 'Why is this card open?', 'cards': [KEY]}, headers=AUTH).json()
        store.update_action(store.list_actions('requested', 10)[0]['id'], 'done', 'checkout-service ERROR 40 lines', 'requested')
        c.post('/api/chats', json={'text': 'and why?', 'chatId': first['id']}, headers=AUTH)
        c.post('/api/actions', json={'type': 'ask', 'key': 'ask', 'target': 'ask', 'text': 'Why did checkout-service spike?', 'cards': []}, headers=AUTH)

    # then
    sent = [r['messages'][0]['content'] for r in fake.gate_requests]
    assert '<thread>\n(no earlier messages)\n</thread>' in sent[0]
    assert '<thread>\nperson: Why is this card open?\nbot: checkout-service ERROR 40 lines\n</thread>' in sent[1] and sent[1].endswith('<message>\nand why?\n</message>')
    assert '<thread>\n(no earlier messages)\n</thread>' in sent[2]


def test_a_chat_page_message_over_the_limit_is_refused_with_the_limit_instead_of_being_cut(store: Store, tmp_path):
    # given
    app, fake = gated(store, tmp_path, [verdict()])
    bridge = app.state.chat_bridge

    # when
    with pytest.raises(chatui.Refused) as long:
        bridge.post(NINA, 'x' * 1001, THREAD, None)
    kept = bridge.post(NINA, ' ' + 'y' * 1000 + ' ', THREAD, None)

    # then
    assert (str(long.value), long.value.code) == ('A message is at most 1000 characters.', 'long')
    assert [m['text'] for m in kept['messages']] == ['y' * 1000] and len(fake.gate_requests) == 1


def test_a_chat_answer_is_shown_redacted_and_masked_in_the_follow_up_context_while_the_stored_answer_stays_as_written(store: Store, tmp_path):
    # given
    raw = 'paul.stone@example.com got an error with +44 7700 900123.'
    app, _ = gated(store, tmp_path, [verdict(), verdict()])

    # when
    with TestClient(app) as c:
        first = c.post('/api/chats', json={'text': 'Which client got the error?'}, headers=AUTH).json()
        asked = store.list_actions('requested', 10)[0]
        store.update_action(asked['id'], 'done', raw, 'requested')
        read = c.get(f"/api/chats/{first['id']}", headers=AUTH).json()
        bridged = app.state.chat_bridge.chat('token:bootstrap', first['id'])
        c.post('/api/chats', json={'text': 'Why?', 'chatId': first['id']}, headers=AUTH)

    # then
    assert read['messages'][0]['answer'] == '[redacted] got an error with [redacted].' and bridged['messages'][0]['answer'] == read['messages'][0]['answer']
    assert store.get_action(asked['id'])['result'] == raw
    assert store.list_actions('requested', 10)[0]['thread'] == [{'who': 'person', 'text': 'Which client got the error?'}, {'who': 'bot', 'text': '[redacted] got an error with [redacted].'}]
