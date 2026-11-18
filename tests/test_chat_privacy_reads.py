import dataclasses
import time

from fastapi.testclient import TestClient

from tests.conftest import AUTH, anomaly, settings
from tests.fake_anthropic import FakeAnthropic, reply, text, tool
from tests.test_chat import chat_action
from tests.test_costs import TODAY, row
from tests.test_investigator import KEY, seed
from tests.test_investigator_tools import sources
from skopax import costs
from skopax.app import create_app
from skopax.investigator import Investigator
from skopax.investigator.tools import Effects
from skopax.kube import Kube
from skopax.store import Store


def chat_run(store: Store) -> dict:
    s = dataclasses.replace(settings(), anthropic_api_key='sk-test', investigate_mode='live')
    replies = [reply(tool('card', {'key': 'no|such|card'})), reply(text('Secret answer: client 42.'), stop='end_turn')]
    inv = Investigator(store, sources(), Effects(lambda key, by: {}, lambda key, text, by, *_, notice=None: {}, lambda *a: None), s, client=FakeAnthropic(replies), clock=time.time)
    return inv.run_once(time.time())


def test_a_chat_run_shows_no_question_cards_or_tool_error_text_on_costs_tools_and_usage(store: Store, tmp_path):
    # given
    seed(store, anomaly(KEY, num=14), history=[])
    chat_action(store, 'Secret question: why did client 42 get an error?')
    record = chat_run(store)

    # when
    with TestClient(create_app(settings(), store, kube=Kube(root=tmp_path))) as c:
        costs = c.get('/api/costs?days=30', headers=AUTH).json()
        tools = c.get('/api/tool-usage?days=30').json()
        usage = c.get('/api/investigator/usage?days=30').json()

    # then
    for page in (costs, tools, usage):
        shown = str(page)
        assert 'Secret' not in shown and 'client 42' not in shown and 'no|such|card' not in shown and KEY not in shown
    [run] = [r for r in costs['runs'] if r['id'] == record['id']]
    assert (run['source'], run['what'], run['keys'], run['toolCalls']) == ('chat', 'Chat message (private)', [], 1)
    assert costs['topCards'] == [] and usage['keys'] == []
    [card] = [t for t in tools['tools'] if t['name'] == 'card']
    assert (card['calls'], card['errors'], card['byTrigger']) == (1, 1, {'chat': 1})
    assert [e['error'] for e in card['recentErrors']] == ['(private chat)'], 'the error still counts, its text stays in the chat'


def test_the_topic_gate_and_small_talk_of_a_chat_message_add_their_spend_but_no_cards_to_costs():
    # given
    rows = [row('gate', {'input': 0.001}, {'input': 2000}, by='nina.white@example.com', keys=(KEY,), gateFor='chat'),
            row('smalltalk', {'output': 0.002}, {'output': 300}, by='nina.white@example.com', keys=(KEY,), talkFor='chat'),
            row('gate', {'input': 0.001}, {'input': 2000}, by='paul.stone@example.com', keys=('log|b|ERROR|y',), gateFor='page')]

    # when
    out = costs.summary(rows, {}, TODAY, 7, 0.004, 20.0)

    # then
    assert out['total']['usd'] == 0.004
    assert [c['key'] for c in out['topCards']] == ['log|b|ERROR|y']


def test_a_card_run_list_skips_private_chats_so_ten_chat_messages_do_not_hide_the_real_run(store: Store):
    # given
    seed(store, anomaly(KEY, num=14), history=[])
    store.add_investigation({'id': 'real', 'trigger': 'cards', 'keys': [KEY], 'startedAt': '2026-11-10T08:00:00Z', 'outcome': 'done'})
    for n in range(10):
        store.add_investigation({'id': f'chat{n}', 'trigger': 'chat', 'keys': [KEY], 'startedAt': f'2026-11-10T09:{n:02d}:00Z', 'outcome': 'done',
                                 'ask': {'text': f'Secret question {n}', 'source': 'chat'}})

    # when
    page = store.list_investigations(KEY, 10, page=True)
    everything = store.list_investigations(None, 20)

    # then
    assert [r['id'] for r in page] == ['real']
    assert len(everything) == 11 and all(r['keys'] == [] and r['ask'] == {'private': True} for r in everything if r['trigger'] == 'chat')
