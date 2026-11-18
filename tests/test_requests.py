from fastapi.testclient import TestClient

from tests.conftest import settings
from skopax.app import create_app
from skopax.investigator import prompt
from skopax.kube import Kube
from skopax.store import Store


def ask(store: Store, text: str, source: str, at: str, **extra) -> dict:
    doc = store.add_action({'type': 'ask', 'source': source, 'key': 'ask', 'target': f'{source}:{text}', 'text': text, 'label': text, 'requestedBy': 'adam.baker@example.com', **extra})
    store.actions.update_one({'_id': doc['id']}, {'$set': {'requestedAt': at}})
    return doc


def test_requests_list_every_ask_newest_first_with_where_it_came_from_and_its_answer(store: Store, tmp_path):
    # given
    slack = ask(store, 'why is checkout-service slow?', 'slack', '2026-11-09T13:00:00Z',
                slack={'where': 'channel', 'channel': 'C2', 'channelName': 'dev-backend', 'outside': True, 'permalink': 'https://example.slack.com/archives/C2/p1', 'threadTs': '1', 'user': 'U1'})
    page = ask(store, 'what was the 5xx yesterday?', 'page', '2026-11-09T12:00:00Z')
    store.add_action({'type': 'ask', 'source': 'page', 'key': 'ask', 'target': 'x', 'text': 'hello', 'requestedBy': 'a', 'smallTalk': True}, 'done', 'Hello!')
    store.add_action({'type': 'mute', 'key': 'k', 'target': 'k', 'requestedBy': 'a'})
    store.update_action(slack['id'], 'done', 'answered', None)
    store.investigations.insert_one({'_id': 'r1', 'actionId': slack['id'], 'trigger': 'slack', 'mode': 'live', 'startedAt': '2026-11-09T13:00:05Z', 'outcome': 'done', 'usd': 0.42,
                                     'recap': 'The slowness comes from the orders-db connection pool.', 'toolCalls': [{'name': 'esql'}, {'name': 'promql'}]})

    # when
    with TestClient(create_app(settings(), store, kube=Kube(root=tmp_path))) as c:
        rows = c.get('/api/requests').json()
        one = c.get('/api/requests?limit=1').json()

    # then
    assert [r['text'] for r in rows] == ['why is checkout-service slow?', 'what was the 5xx yesterday?'] and [r['text'] for r in one] == ['why is checkout-service slow?']
    first, second = rows
    assert (first['source'], first['status'], first['slack']['channelName'], first['slack']['outside'], first['slack']['permalink']) == ('slack', 'done', 'dev-backend', True, 'https://example.slack.com/archives/C2/p1')
    assert first['run'] == {'id': 'r1', 'outcome': 'done', 'mode': 'live', 'startedAt': '2026-11-09T13:00:05Z', 'usd': 0.42, 'recap': 'The slowness comes from the orders-db connection pool.', 'toolCalls': 2}
    assert 'user' not in first['slack'] and 'thread' not in first and first['slack']['threadTs'] == '1'
    assert (second['source'], second['status'], second['run']) == ('page', 'requested', None)


def test_the_brief_names_a_channel_outside_the_main_channel_and_asks_for_plain_words():
    # given
    action = {'id': 'a1', 'requestedBy': 'adam.baker@example.com', 'text': 'why is it slow?', 'slack': {'user': 'U1', 'channel': 'C2', 'channelName': 'dev-backend', 'outside': True}}

    # when
    outside = prompt.slack_brief([], action, 'https://skopax.example.com', 10)
    home = prompt.slack_brief([], {**action, 'slack': {'user': 'U1', 'channel': 'C1'}}, 'https://skopax.example.com', 10)

    # then
    assert outside.startswith('Slack ask by operator adam.baker@example.com (<@U1>) in #dev-backend, a channel outside the main channel where people may not know Skopax or its cards, so answer in plain words (action a1).')
    assert ' in the main channel (action a1).' in home
