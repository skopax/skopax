import datetime as dt

from fastapi.testclient import TestClient

from tests.conftest import AUTH, anomaly, state
from skopax.store import CARD_RUNS_DAYS, iso, utc_now

A, B, GONE = 'log|checkout-service|ERROR|a', 'log|checkout-service|ERROR|b', 'log|payments-api|ERROR|gone'


def days_ago(n: float) -> str:
    return iso(utc_now() - dt.timedelta(days=n))


def runs(store):
    store.add_investigation({'id': 'r1', 'trigger': 'cards', 'mode': 'live', 'keys': [A, GONE], 'startedAt': days_ago(10), 'outcome': 'done', 'usd': 0.5, 'recap': 'older',
                             'toolCalls': [{'name': 'esql', 'input': {'q': 1}}]})
    store.add_investigation({'id': 'r2', 'trigger': 'action', 'mode': 'live', 'keys': [A], 'startedAt': days_ago(2), 'outcome': 'done', 'usd': 0.25, 'recap': 'newest',
                             'toolCalls': [{'name': 'esql'}, {'name': 'kube'}]})
    store.add_investigation({'id': 'c1', 'trigger': 'chat', 'mode': 'live', 'keys': [A], 'startedAt': days_ago(1), 'outcome': 'done', 'usd': 9.0, 'recap': 'chat answer',
                             'ask': {'text': 'chat question', 'by': 'nina.white@example.com', 'source': 'chat'}})


def test_card_runs_give_each_card_on_the_board_its_latest_run_count_and_spend_without_chat_runs(client: TestClient, store):
    # given
    client.put('/api/state', json=state(anomaly(A), anomaly(B)), headers=AUTH)
    runs(store)

    # when
    got = client.get('/api/card-runs').json()

    # then
    assert set(got) == {A}
    assert got[A]['n'] == 2 and got[A]['usd'] == 0.75
    assert got[A]['latest']['id'] == 'r2' and got[A]['latest']['recap'] == 'newest' and got[A]['latest']['toolCalls'] == 2
    assert 'chat' not in str(got)


def test_card_runs_are_empty_without_cards(client: TestClient):
    # when
    got = client.get('/api/card-runs')

    # then
    assert got.status_code == 200 and got.json() == {}


def test_the_run_list_leaves_chat_runs_out_when_asked(client: TestClient, store):
    # given
    runs(store)

    # when
    every = client.get('/api/investigations', params={'limit': 100}).json()
    no_chat = client.get('/api/investigations', params={'limit': 1, 'chats': 'false'}).json()

    # then
    assert [r['id'] for r in every] == ['c1', 'r2', 'r1'] and every[0]['ask'] == {'private': True}
    assert [r['id'] for r in no_chat] == ['r2']


def test_card_runs_leave_out_runs_older_than_the_window(client: TestClient, store):
    # given
    client.put('/api/state', json=state(anomaly(A)), headers=AUTH)
    store.add_investigation({'id': 'old', 'trigger': 'cards', 'mode': 'live', 'keys': [A], 'startedAt': days_ago(CARD_RUNS_DAYS + 1), 'outcome': 'done', 'usd': 3.0, 'recap': 'long ago'})
    store.add_investigation({'id': 'new', 'trigger': 'cards', 'mode': 'live', 'keys': [A], 'startedAt': days_ago(1), 'outcome': 'done', 'usd': 0.5, 'recap': 'recent'})

    # when
    got = client.get('/api/card-runs').json()

    # then
    assert got[A]['n'] == 1 and got[A]['usd'] == 0.5 and got[A]['latest']['id'] == 'new'
