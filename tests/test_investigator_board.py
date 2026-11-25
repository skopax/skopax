import datetime as dt

from fastapi.testclient import TestClient

from tests.conftest import anomaly, settings
from tests.test_investigator import KEY, NOW, seed
from tests.test_investigator_slack import PERMALINK, slack_action, slack_investigator
from skopax import clock
from skopax.app import create_app
from skopax.engine import iso
from skopax.kube import Kube
from skopax.store import Store

ROOM_0 = 'investigator-room-0'


def at(t: float) -> dt.datetime:
    return dt.datetime.fromtimestamp(t, dt.timezone.utc)


def later_in_thread(store: Store) -> dict:
    return slack_action(store, text='And when did it start?', slack={'where': 'card', 'channel': 'C1', 'threadTs': '1792224000.000100', 'ts': '1792224000.000400', 'user': 'U2',
                                                                    'language': 'nl', 'permalink': PERMALINK}, requestedBy='nina.white')


def busy(store: Store) -> tuple[dict, dict, dict]:
    seed(store, anomaly(KEY), history=[])
    first = slack_action(store)
    store.update_action(first['id'], 'processing', None, 'requested')
    store.add_investigation({'id': 'run-1', 'mode': 'live', 'trigger': 'slack', 'actionId': first['id'], 'keys': [KEY], 'pod': 'skopax-abc:1a2b3c4d', 'room': ROOM_0, 'startedAt': iso(NOW - 90),
                             'model': 'claude-sonnet-5-5', 'effort': 'high', 'outcome': 'running', 'ask': {'text': 'Why did it go up?', 'by': 'adam.baker', 'source': 'slack', 'thread': PERMALINK}})
    store.take_lease('skopax-abc:1a2b3c4d:9f8e7d', 1200, ROOM_0)
    store.add_activity('tool', 'investigator', 'esql · 420 ms · 12 rows', runId='run-1', tool='esql')
    store.add_activity('tool', 'investigator', 'kube · 80 ms · 3 rows', runId='run-1', tool='kube')
    queued = later_in_thread(store)
    page = store.add_action({'type': 'ask', 'source': 'page', 'key': 'ask', 'target': 'ask', 'text': 'What happened last night?', 'cards': [], 'requestedBy': 'oscar.green@example.com'})
    store.add_investigation({'id': 'run-0', 'mode': 'live', 'trigger': 'cards', 'actionId': None, 'keys': [KEY], 'pod': 'skopax-abc:1a2b3c4d', 'startedAt': iso(NOW - 900), 'endedAt': iso(NOW - 800),
                             'model': 'claude-opus-5-5', 'outcome': 'done', 'usd': 0.42, 'recap': 'One client.', 'toolCalls': [{'name': 'esql'}, {'name': 'promql'}]})
    return first, queued, page


def test_the_board_shows_what_each_room_runs_what_waits_and_why_and_the_limits(store: Store):
    # given
    first, queued, page = busy(store)
    inv, _ = slack_investigator(store, [])

    # when
    board = inv.board(NOW)

    # then
    rooms = {r['id']: r for r in board['rooms']}
    assert [r['name'] for r in board['rooms']] == ['Room 1', 'Room 2', 'Cards'] and (board['mode'], board['halted']) == ('live', None)
    run = rooms[ROOM_0]['run']
    assert (rooms[ROOM_0]['pod'], run['id'], run['trigger'], run['toolCalls'], run['lastTool']) == ('skopax-abc', 'run-1', 'slack', 2, 'kube')
    assert run['ask'] == {'text': 'Why did it go up?', 'by': 'adam.baker', 'source': 'slack', 'thread': PERMALINK} and run['startedAt'] == iso(NOW - 90) and run['model'] == 'claude-sonnet-5-5'
    assert rooms['investigator-room-1']['run'] is None and rooms['investigator']['run'] is None and board['unplaced'] == []
    assert [(w['id'], w['why']) for w in board['waiting']] == [(queued['id'], 'behind an earlier question in the same Slack thread'), (page['id'], 'next for a free room')]
    assert board['waiting'][0]['by'] == 'nina.white' and board['waiting'][0]['text'] == 'And when did it start?' and board['waiting'][0]['thread'] == PERMALINK
    assert [(r['id'], r['outcome'], r['toolCalls'], r['usd']) for r in board['recent']] == [('run-0', 'done', 2, 0.42)]
    assert board['limits']['slackAsks'] == 2 and board['limits']['slackAskCap'] == 10 and board['limits']['cap'] == inv.cap and board['limits']['spentToday'] == 0
    assert board['limits']['askers'] == [{'by': 'adam.baker', 'asks': 1}, {'by': 'nina.white', 'asks': 1}]


def test_a_running_run_carries_its_cost_so_far_from_the_spend_ledger_and_a_failed_recent_run_its_reason(store: Store):
    # given
    busy(store)
    store.investigations.update_one({'_id': 'run-1'}, {'$set': {'startedAt': iso(NOW - 2 * 86400)}})
    store.add_ledger({'runId': 'run-1', 'day': clock.day(NOW - 2 * 86400), 'model': 'claude-sonnet-5-5', 'usd': {'total': 0.12}}, at(NOW - 2 * 86400 + 60))
    store.add_ledger({'runId': 'run-1', 'day': clock.day(NOW), 'model': 'claude-sonnet-5-5', 'usd': {'total': 0.0345}}, at(NOW - 30))
    store.add_ledger({'runId': 'run-1', 'day': clock.day(NOW - 3 * 86400), 'model': 'claude-sonnet-5-5', 'usd': {'total': 9.0}}, at(NOW - 3 * 86400))
    store.add_ledger({'runId': 'run-0', 'day': clock.day(NOW), 'model': 'claude-opus-5-5', 'usd': {'total': 0.42}}, at(NOW - 850))
    store.add_investigation({'id': 'run-p', 'mode': 'live', 'trigger': 'chat', 'actionId': None, 'keys': [], 'pod': 'skopax-abc:1a2b3c4d', 'room': 'investigator-room-1', 'startedAt': iso(NOW - 60),
                             'model': 'claude-sonnet-5-5', 'outcome': 'running', 'ask': {'text': 'private', 'by': 'oscar.green', 'source': 'chat'}})
    store.add_activity('tool', 'investigator', 'esql · 10 ms', runId='run-p', tool='esql')
    store.add_ledger({'runId': 'run-p', 'day': clock.day(NOW), 'model': 'claude-sonnet-5-5', 'usd': {'total': 0.5}}, at(NOW - 20))
    store.add_investigation({'id': 'run-f', 'mode': 'live', 'trigger': 'cards', 'actionId': None, 'keys': [KEY], 'pod': 'skopax-abc:1a2b3c4d', 'startedAt': iso(NOW - 700), 'endedAt': iso(NOW - 650),
                             'model': 'claude-opus-5-5', 'outcome': 'failed', 'usd': 0.01, 'error': 'no Anthropic API key is set', 'toolCalls': []})
    store.add_investigation({'id': 'run-c', 'mode': 'live', 'trigger': 'chat', 'actionId': None, 'keys': [], 'pod': 'skopax-abc:1a2b3c4d', 'startedAt': iso(NOW - 600), 'endedAt': iso(NOW - 590),
                             'model': 'claude-opus-5-5', 'outcome': 'failed', 'usd': 0.01, 'error': 'private chat detail', 'toolCalls': []})
    inv, _ = slack_investigator(store, [])

    # when
    board = inv.board(NOW)

    # then
    run = next(r['run'] for r in board['rooms'] if r['id'] == ROOM_0)
    assert run['usd'] == 0.1545, 'a long run keeps its early spend and a row from before it started is not counted'
    chat = next(r['run'] for r in board['rooms'] if r['id'] == 'investigator-room-1')
    assert chat['id'] == 'run-p' and 'usd' not in chat and 'lastTool' not in chat and 'lastToolAt' not in chat, 'a private chat run shows neither its cost nor its tools'
    store.ensure_indexes()
    assert any(i['key'] == [('runId', 1), ('at', 1)] for i in store.ledger.index_information().values()), 'the run lookup has its own index'
    assert [(r['id'], r.get('error')) for r in board['recent']] == [('run-c', None), ('run-f', 'no Anthropic API key is set'), ('run-0', None)], 'a chat run keeps its reason to itself'
    assert 'error' not in store.list_investigations(None, 5, page=True)[0], 'the run lists stay as they were'


def test_an_investigator_that_is_on_but_cannot_run_says_why_and_one_switched_off_does_not(store: Store):
    # given
    busy(store)
    inv, _ = slack_investigator(store, [])
    inv.keyed = lambda: False

    # when
    no_key = inv.board(NOW)
    inv.keyed = lambda: True
    inv.model = 'claude-unknown-9'
    no_model = inv.board(NOW)
    inv.model = 'claude-sonnet-5-5'
    inv.on = lambda: False
    switched = inv.board(NOW)
    inv.on = lambda: True
    inv.mode = 'off'
    never = inv.board(NOW)

    # then
    assert (no_key['enabled'], no_key['problem']) == (False, 'key')
    assert (no_model['enabled'], no_model['problem']) == (False, 'model')
    assert (switched['halted'], switched['problem']) == ('off', None)
    assert (never['mode'], never['problem']) == ('off', None)


def test_a_stopped_investigator_says_so_on_every_waiting_request_and_an_old_run_without_a_room_is_listed_apart(store: Store):
    # given
    busy(store)
    store.investigations.update_one({'_id': 'run-1'}, {'$unset': {'room': ''}})
    store.set_investigator_stop(True, 'oscar.green')
    inv, _ = slack_investigator(store, [])

    # when
    board = inv.board(NOW)

    # then
    assert board['halted'] == 'stopped' and {w['why'] for w in board['waiting']} == {'the investigator is stopped'}
    assert [r['id'] for r in board['unplaced']] == ['run-1'] and all(r['run'] is None for r in board['rooms'])


def test_the_status_endpoint_adds_the_per_person_and_page_caps(store: Store, tmp_path):
    # given
    busy(store)

    # when
    with TestClient(create_app(settings(), store, kube=Kube(root=tmp_path))) as c:
        got = c.get('/api/investigator/status').json()

    # then
    assert got['limits']['perPersonCap'] == 4 and got['limits']['pageAskCap'] == 6 and got['limits']['pageAsks'] == 1
    assert len(got['rooms']) == 3 and len(got['waiting']) == 2
