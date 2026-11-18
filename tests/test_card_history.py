import datetime as dt

import mongomock
import pytest
from fastapi.testclient import TestClient
from pymongo.errors import OperationFailure, PyMongoError

from tests.conftest import AUTH, anomaly, state
from tests.fake_anthropic import reply, text
from tests.test_investigator import NOW, seed
from tests.test_investigator_slack import PERMALINK, slack_action, slack_investigator
from skopax import history
from skopax.config import load_settings
from skopax.engine import PassResult, apply, iso
from skopax.redact import REDACTED
from skopax.store import Store, ttl_index

KEY = 'log|checkout-service|ERROR|boom'
OTHER = 'log|orders-service|ERROR|x'
DAY = 86400
CARD = '4111 1111 1111 1111'


def ago(seconds: float) -> str:
    return iso(NOW - seconds)


def card_of(key: str, sev: str = 'warn', status: str = 'open', first: float = 7200, **extra) -> dict:
    return anomaly(key, sev=sev, status=status, src='logs', ns='-', unit='', sig='log', now=40, d1=5, d7=4, peak=40, firstSeen=ago(first), lastSeen=ago(600),
                   short=f'{sev.upper()} {key}', what=f'{key} happened', **extra)


def seeded(store: Store, *cards: dict, muted: list | None = None) -> None:
    store.save_state({'initialized': True, 'anomalies': list(cards), 'muted': muted or [], 'sources': {'logs': 'ok'}, 'history': []}, None)


def docs(store: Store, key: str = KEY) -> list[dict]:
    return sorted(store.card_history.find({'key': key}), key=lambda d: d['firstSeen'])


def test_a_card_that_drops_out_of_the_state_leaves_its_occurrence_in_the_history(store: Store):
    # given
    old = card_of(KEY, sev='info', status='cleared', first=3 * DAY, clearedAt=ago(2 * DAY), note={'text': 'one client', 'at': ago(2 * DAY)}, recommend='nothing to do')
    seeded(store, old)

    # when
    apply(store, [PassResult('logs', True, 'ok', [])], NOW)

    # then
    assert store.load()['anomalies'] == []
    [doc] = docs(store)
    assert doc['_id'] == f"{KEY}|{old['firstSeen']}"
    assert {k: doc[k] for k in ('svc', 'label', 'ns', 'src', 'sev', 'firstSeen', 'lastSeen', 'clearedAt', 'peak', 'unit', 'outcome')} == {
        'svc': 'checkout-service', 'label': 'ERROR pattern', 'ns': '-', 'src': 'logs', 'sev': 'info', 'firstSeen': old['firstSeen'], 'lastSeen': old['lastSeen'],
        'clearedAt': old['clearedAt'], 'peak': 40, 'unit': '', 'outcome': 'cleared'}
    assert (doc['note'], doc['recommend'], doc['reopens']) == ('one client', 'nothing to do', 0)
    assert [e['ev'] for e in doc['events']] == ['left']
    assert isinstance(doc['atDt'], dt.datetime)


def test_a_reopen_closes_the_old_occurrence_with_what_was_on_it(store: Store):
    # given
    old = card_of(KEY, status='cleared', first=10 * 3600, clearedAt=ago(5 * 3600), triage='handled', triagedAt=ago(4 * 3600), note={'text': 'ShipCorp 456, known', 'at': ago(4 * 3600)})
    seeded(store, old)
    fired = {k: v for k, v in card_of(KEY).items() if k not in ('firstSeen', 'lastSeen', 'peak', 'status')}

    # when
    apply(store, [PassResult('logs', True, 'ok', [fired])], NOW)

    # then
    live = store.anomaly(KEY)
    assert live['firstSeen'] == iso(NOW) and live['status'] == 'open'
    [doc] = docs(store)
    assert doc['firstSeen'] == old['firstSeen'] and doc['outcome'] == 'handled' and doc['note'] == 'ShipCorp 456, known'
    assert [e['ev'] for e in doc['events']] == ['reopened']


def test_resolve_on_the_page_records_no_action_needed_with_the_reason(client: TestClient, store: Store):
    # given
    client.put('/api/state', json=state(card_of(KEY), card_of(OTHER)), headers=AUTH)

    # when
    client.post('/api/actions', json={'type': 'resolve', 'key': KEY, 'reason': 'one bot, nothing to do', 'label': 'Resolve'}, headers=AUTH)

    # then
    [doc] = docs(store)
    assert (doc['outcome'], doc['triage'], doc['note']) == ('handled', 'handled', 'one bot, nothing to do')
    assert [e['ev'] for e in doc['events']] == ['handled']
    assert docs(store, OTHER) == []


def test_resolve_kinds_record_their_own_outcome_with_how_and_who(client: TestClient, store: Store):
    # given
    client.put('/api/state', json=state(card_of(KEY), card_of(OTHER)), headers=AUTH)
    store.card_ids.insert_one({'_id': OTHER, 'n': 7})

    # when
    client.post('/api/actions', json={'type': 'resolve', 'key': KEY, 'kind': 'fixed', 'ref': 'OPS-140', 'label': 'Resolve'}, headers=AUTH)
    client.post('/api/actions', json={'type': 'resolve', 'key': OTHER, 'kind': 'duplicate', 'ref': KEY, 'label': 'Resolve'}, headers=AUTH)
    store.card_ids.insert_one({'_id': KEY, 'n': 6})
    [fixed], [dup] = docs(store), docs(store, OTHER)

    # then
    assert (fixed['outcome'], fixed['triage'], {k: fixed['resolution'][k] for k in ('kind', 'ref', 'by', 'via')}) == ('fixed', 'handled', {'kind': 'fixed', 'ref': 'OPS-140', 'by': 'token:bootstrap', 'via': 'page'})
    assert (dup['outcome'], dup['resolution']['ref']) == ('duplicate', KEY), 'a key with no number yet stays a key'
    assert history.outcome_text(fixed['outcome'], fixed['resolution']) == 'Fixed (OPS-140) · via page · bootstrap'
    assert history.outcome_text(dup['outcome'], dup['resolution']) == f'Duplicate of {KEY} · via page · bootstrap'


@pytest.mark.parametrize('outcome, resolution, text', [
    ('handled', None, 'Resolved'),
    ('handled', {'kind': 'handled', 'by': 'nina.white@example.com', 'via': 'slack'}, 'No action needed · via Slack · nina.white'),
    ('fixed', {'kind': 'fixed', 'by': 'jira-links', 'via': 'system', 'ref': 'OPS-1'}, 'Fixed (OPS-1) · via Skopax · Jira link sweep'),
    ('duplicate', {'kind': 'duplicate', 'ref': '#12', 'by': 'pod', 'via': 'system'}, 'Duplicate of #12 · via Skopax · investigator'),
    ('handled', {'kind': 'handled', 'by': 'paul.stone@example.com', 'via': 'chat'}, 'No action needed · via chat · paul.stone'),
    ('auto-resolved', {'kind': 'handled', 'by': 'auto', 'via': 'system'}, 'auto-resolved'),
    ('progress', {'kind': 'fixed', 'by': 'x', 'via': 'page'}, 'In progress'),
])
def test_the_outcome_text_says_how_via_and_who_and_old_handled_cards_read_resolved(outcome: str, resolution, text: str):
    # when
    said = history.outcome_text(outcome, resolution)

    # then
    assert said == text


def test_the_backfill_reads_the_kind_of_a_pod_resolve_from_its_write_line():
    # given
    run = {'_id': 'r1', 'startedAt': ago(DAY), 'trigger': 'auto', 'keys': [KEY], 'writes': [{'tool': 'triage', 'key': KEY, 'summary': 'handled OPS-1 (Nina White) · duplicate #22'}]}
    doc = {}

    # when
    history._apply_run(doc, run, KEY)

    # then
    assert doc == {'outcome': 'duplicate', 'triage': 'handled', 'triagedBy': 'pod', 'ticket': 'OPS-1', 'owner': 'Nina White',
                   'resolution': {'kind': 'duplicate', 'ref': '#22', 'by': 'pod', 'via': 'system', 'at': ago(DAY)}}


def test_a_persons_resolve_beats_an_old_auto_resolve_stamp(store: Store):
    # given
    seeded(store, card_of(KEY, status='cleared', first=5 * DAY, clearedAt=ago(4 * DAY), autoResolvedAt=ago(DAY)))

    # when
    store.triage(KEY, 'handled', resolution={'kind': 'fixed', 'by': 'nina.white@example.com', 'via': 'page'})
    [doc] = docs(store)

    # then
    assert doc['outcome'] == 'fixed'
    assert history.outcome({'triage': 'handled', 'autoResolvedAt': ago(DAY), 'resolution': {'kind': 'handled', 'via': 'system', 'by': 'auto'}}, 'handled') == 'auto-resolved'
    assert history.outcome({'triage': 'handled', 'autoResolvedAt': ago(DAY), 'resolution': {'kind': 'fixed', 'via': 'system', 'by': 'jira-links'}}, 'handled') == 'fixed', 'the sweep beats an old stamp too'
    assert history.outcome({'triage': 'handled', 'autoResolvedAt': ago(DAY)}, 'handled') == 'auto-resolved'


def test_take_records_in_progress_with_ticket_and_owner(client: TestClient, store: Store):
    # given
    client.put('/api/state', json=state(card_of(KEY)), headers=AUTH)

    # when
    client.post('/api/actions', json={'type': 'take', 'key': KEY, 'ticket': 'OPS-135', 'label': 'Take'}, headers=AUTH)

    # then
    [doc] = docs(store)
    assert (doc['outcome'], doc['ticket'], doc['owner']) == ('progress', 'OPS-135', 'token:bootstrap')


def test_mute_and_known_noise_record_their_kind(client: TestClient, store: Store):
    # given
    client.put('/api/state', json=state(card_of(KEY), card_of(OTHER)), headers=AUTH)

    # when
    client.post('/api/actions', json={'type': 'mute', 'key': KEY, 'hours': 24, 'label': 'Mute'}, headers=AUTH)
    client.post('/api/actions', json={'type': 'known', 'key': OTHER, 'reason': 'month end', 'label': 'Known noise'}, headers=AUTH)

    # then
    assert [(d['outcome'], d['muted']) for d in docs(store)] == [('muted', 'muted on the page for 24 h')]
    assert [(d['outcome'], d['muted']) for d in docs(store, OTHER)] == [('known', 'month end')]


def test_store_triage_records_progress_and_handled_only(store: Store):
    # given
    seeded(store, card_of(KEY), card_of(OTHER))

    # when
    store.triage(KEY, 'investigating')
    store.triage(KEY, 'dived')
    store.triage(OTHER, 'handled', ticket='OPS-134', by='jira-links')

    # then
    assert docs(store) == []
    assert [(d['outcome'], d['ticket']) for d in docs(store, OTHER)] == [('handled', 'OPS-134')]


def test_auto_resolve_records_auto_resolved(store: Store):
    # given
    seeded(store, card_of(KEY, status='cleared', first=5 * DAY, clearedAt=ago(4 * DAY)))

    # when
    apply(store, [], NOW, auto_resolve_days=3)

    # then
    [doc] = docs(store)
    assert doc['outcome'] == 'auto-resolved' and [e['ev'] for e in doc['events']] == ['auto-resolved']
    assert {k: doc['resolution'][k] for k in ('kind', 'by', 'via')} == {'kind': 'handled', 'by': 'auto', 'via': 'system'}


def test_a_resolved_card_that_fires_again_comes_back_without_its_resolution(store: Store):
    # given
    old = card_of(KEY, status='cleared', first=10 * 3600, clearedAt=ago(5 * 3600), triage='handled', triagedAt=ago(4 * 3600),
                  resolution={'kind': 'fixed', 'ref': 'OPS-140', 'by': 'nina.white@example.com', 'via': 'page', 'at': ago(4 * 3600)})
    kept = card_of(OTHER, status='cleared', first=10 * 3600, clearedAt=ago(3600), triage='handled', triagedAt=ago(1800),
                   resolution={'kind': 'duplicate', 'ref': '#12', 'by': 'nina.white@example.com', 'via': 'chat', 'at': ago(1800)})
    seeded(store, old, kept)
    fired = {k: v for k, v in card_of(KEY).items() if k not in ('firstSeen', 'lastSeen', 'peak', 'status')}

    # when
    apply(store, [PassResult('logs', True, 'ok', [fired])], NOW)

    # then
    live = store.anomaly(KEY)
    assert live['status'] == 'open' and live.get('triage') is None and live.get('resolution') is None
    assert store.anomaly(OTHER)['resolution']['kind'] == 'duplicate', 'a pass carries the resolution of a card that stays resolved'
    [doc] = docs(store)
    assert (doc['outcome'], doc['resolution']['ref']) == ('fixed', 'OPS-140')


def test_the_seen_before_line_carries_how_the_last_occurrence_was_resolved(store: Store):
    # given
    live = card_of(KEY, first=3600)
    seeded(store, live)
    how = {'kind': 'fixed', 'ref': 'OPS-120', 'by': 'nina.white@example.com', 'via': 'page', 'at': ago(DAY)}
    store.card_history.insert_one({'_id': f'{KEY}|{ago(2 * DAY)}', 'key': KEY, 'firstSeen': ago(2 * DAY), 'lastSeen': ago(2 * DAY - 600), 'clearedAt': ago(DAY), 'outcome': 'fixed', 'resolution': how})

    # when
    apply(store, [], NOW)

    # then
    seen = store.anomaly(KEY)['seenBefore']
    assert (seen['outcome'], seen['resolution']) == ('fixed', how)


def test_a_triage_written_through_put_state_is_recorded_once(client: TestClient, store: Store):
    # given
    rev = client.put('/api/state', json=state(card_of(KEY)), headers=AUTH).json()['rev']
    taken = state(card_of(KEY, triage='progress', ticket='OPS-124', owner='Oscar Green'))

    # when
    rev = client.put('/api/state', json=taken, headers=AUTH | {'X-Base-Rev': str(rev)}).json()['rev']
    client.put('/api/state', json=taken, headers=AUTH | {'X-Base-Rev': str(rev)})

    # then
    [doc] = docs(store)
    assert (doc['outcome'], doc['ticket'], doc['owner']) == ('progress', 'OPS-124', 'Oscar Green')
    assert [e['ev'] for e in doc['events']] == ['progress']


def test_note_recommend_and_mute_reason_are_masked_and_cut(store: Store):
    # given
    long = 'client adam@example.com card ' + CARD + ' ' + 'x' * 3000
    seeded(store, card_of(KEY, note={'text': long, 'at': ago(60)}, recommend=long))

    # when
    store.triage(KEY, 'handled')

    # then
    [doc] = docs(store)
    for field in ('note', 'recommend'):
        assert 'adam@example.com' not in doc[field] and CARD not in doc[field] and REDACTED in doc[field]
        assert len(doc[field]) <= history.TEXT_LIMIT + 1


def test_the_doc_points_at_the_runs_of_its_occurrence_and_the_runs_point_back(store: Store):
    # given
    live = card_of(KEY, first=3600)
    seeded(store, live)
    store.add_investigation({'id': 'r-old', 'keys': [KEY], 'startedAt': ago(2 * DAY), 'outcome': 'done', 'trigger': 'cards', 'recap': 'older occurrence'})
    store.add_investigation({'id': 'r-1', 'keys': [KEY], 'startedAt': ago(3000), 'outcome': 'done', 'trigger': 'cards', 'recap': 'ShipCorp answers 456 invalid client ip\nsecond line'})
    store.add_investigation({'id': 'r-2', 'keys': [KEY], 'startedAt': ago(1000), 'outcome': 'done', 'trigger': 'slack', 'recap': 'a Slack answer'})

    # when
    store.triage(KEY, 'handled')

    # then
    [doc] = docs(store)
    assert (doc['runId'], doc['recap']) == ('r-1', 'ShipCorp answers 456 invalid client ip')
    assert doc['runs'] == ['r-2', 'r-1']
    assert store.investigation('r-1')['cardHistory'] == [doc['_id']] and store.investigation('r-2')['cardHistory'] == [doc['_id']]
    assert 'cardHistory' not in store.investigation('r-old')


def test_reopens_count_the_earlier_occurrences_of_the_key(store: Store):
    # given
    first = card_of(KEY, first=3 * DAY)
    seeded(store, first)
    store.triage(KEY, 'handled')
    seeded(store, card_of(KEY, first=3600))

    # when
    store.triage(KEY, 'handled')

    # then
    assert [d['reopens'] for d in docs(store)] == [0, 1]


def test_a_failing_history_write_never_breaks_the_state_write(store: Store, monkeypatch):
    # given
    seeded(store, card_of(KEY, sev='info', status='cleared', first=3 * DAY, clearedAt=ago(2 * DAY)))

    def broken(*a, **k):
        raise PyMongoError('history is down')

    monkeypatch.setattr(store.card_history, 'update_one', broken)

    # when
    applied = apply(store, [PassResult('logs', True, 'ok', [])], NOW)

    # then
    assert applied is not None and store.load()['anomalies'] == []


def test_a_slack_ask_about_a_card_lands_on_its_occurrence_with_the_masked_answer(store: Store):
    # given
    seed(store, card_of('log|a|ERROR|x', first=600), history=[])
    slack_action(store, cards=('log|a|ERROR|x',))
    answer = 'Client +44 7700 900123 alone wrote 40 lines.\n' + 'y' * 2000
    inv, _ = slack_investigator(store, [reply(text(answer), stop='end_turn')])

    # when
    record = inv.run_once(NOW)

    # then
    [doc] = docs(store, 'log|a|ERROR|x')
    [ask] = doc['asks']
    assert (ask['question'], ask['by'], ask['thread'], ask['runId']) == ('Why did it go up?', 'adam.baker', PERMALINK, record['id'])
    assert '7700' not in ask['answer'] and REDACTED in ask['answer'] and len(ask['answer']) <= history.ANSWER_LIMIT + 1
    assert ask['at'] and 'usd' in ask
    assert doc['lastAsk']['line'].startswith('Client') and doc['outcome'] == 'open'
    assert store.investigation(record['id'])['cardHistory'] == [doc['_id']]


def test_keep_days_come_from_the_config_with_365_as_default(monkeypatch):
    # given

    # when
    default = load_settings(lambda: {})
    set_ = load_settings(lambda: {'history.keep-days': '200', 'investigate.keep-days': '400'})

    # then
    assert (default.history_keep_days, default.investigate_keep_days) == (365, 365)
    assert (set_.history_keep_days, set_.investigate_keep_days) == (200, 400)


def ttl(coll, field: str) -> int | None:
    return next((i.get('expireAfterSeconds') for i in coll.index_information().values() if i['key'] == [(field, 1)]), None)


def test_ttl_indexes_follow_the_keep_days_and_the_ledger_never_ends_before_the_runs():
    # given
    long = Store(mongomock.MongoClient(tz_aware=True)['a'], history_days=200, investigate_days=400)
    short = Store(mongomock.MongoClient(tz_aware=True)['b'], investigate_days=30)

    # when
    long.ensure_indexes()
    short.ensure_indexes()

    # then
    assert (ttl(long.investigations, 'startedAtDt'), ttl(long.ledger, 'atDt'), ttl(long.card_history, 'atDt')) == (400 * DAY, 400 * DAY, 200 * DAY)
    assert (ttl(short.investigations, 'startedAtDt'), ttl(short.ledger, 'atDt'), ttl(short.card_history, 'atDt')) == (30 * DAY, 90 * DAY, 365 * DAY)


class ConflictingIndex:
    name = 'investigations'

    def __init__(self, code: int, refuse: bool = False, drop_code: int | None = None):
        self.code = code
        self.refuse = refuse
        self.drop_code = drop_code
        self.commands = []
        self.created = []
        self.dropped = []
        self.database = self

    def create_index(self, field, **opts):
        self.created.append((field, opts))
        if not self.dropped:
            raise OperationFailure('Index already exists with a different expireAfterSeconds', code=self.code)

    def drop_index(self, spec):
        self.dropped.append(spec)
        if self.drop_code is not None:
            raise OperationFailure('drop refused', code=self.drop_code)

    def command(self, cmd):
        self.commands.append(cmd)
        if self.refuse:
            raise OperationFailure('not authorized on skopax to execute command collMod', code=13)
        return {'ok': 1}


def test_a_changed_ttl_is_applied_with_collmod():
    # given
    coll = ConflictingIndex(85)

    # when
    ttl_index(coll, 'startedAtDt', 365 * DAY)

    # then
    assert coll.created == [('startedAtDt', {'expireAfterSeconds': 365 * DAY})]
    assert coll.commands == [{'collMod': 'investigations', 'index': {'keyPattern': {'startedAtDt': 1}, 'expireAfterSeconds': 365 * DAY}}]
    assert coll.dropped == []


def test_a_refused_collmod_drops_and_recreates_the_index_with_the_new_ttl(caplog):
    # given
    coll = ConflictingIndex(85, refuse=True)

    # when
    ttl_index(coll, 'startedAtDt', 365 * DAY)

    # then
    assert coll.dropped == [[('startedAtDt', 1)]]
    assert coll.created == [('startedAtDt', {'expireAfterSeconds': 365 * DAY})] * 2
    assert 'keeps its old TTL' not in caplog.text


def test_an_index_the_other_pod_already_dropped_is_still_recreated():
    # given
    coll = ConflictingIndex(85, refuse=True, drop_code=27)

    # when
    ttl_index(coll, 'startedAtDt', 365 * DAY)

    # then
    assert coll.created[-1] == ('startedAtDt', {'expireAfterSeconds': 365 * DAY}) and len(coll.created) == 2


def test_a_refused_drop_keeps_the_old_ttl_and_lets_the_start_go_on(caplog):
    # given
    coll = ConflictingIndex(85, refuse=True, drop_code=13)

    # when
    ttl_index(coll, 'startedAtDt', 365 * DAY)

    # then
    assert len(coll.commands) == 1 and len(coll.created) == 1
    assert 'keeps its old TTL' in caplog.text
    assert 'refused: OperationFailure code 13; the drop too: OperationFailure code 13' in caplog.text and 'not authorized' not in caplog.text


def test_any_other_index_failure_still_raises():
    # given
    coll = ConflictingIndex(13)

    # when
    with pytest.raises(OperationFailure):
        ttl_index(coll, 'startedAtDt', 365 * DAY)

    # then
    assert coll.commands == []


def backfill_inputs() -> tuple[list[dict], list[dict], dict]:
    runs = [
        {'_id': 'r-a1', 'keys': [KEY], 'startedAt': ago(20 * DAY), 'endedAt': ago(20 * DAY - 60), 'trigger': 'cards', 'outcome': 'done', 'usd': 0.4,
         'recap': 'ShipCorp 456 invalid client ip\nmore', 'writes': [{'tool': 'note', 'key': KEY, 'summary': 'ShipCorp answers 456'}, {'tool': 'assess', 'key': KEY, 'summary': 'ShipCorp → ask ShipCorp to allow the new IP'},
                                                                 {'tool': 'triage', 'key': KEY, 'summary': 'progress OPS-120 (Oscar Green)'}]},
        {'_id': 'r-a2', 'keys': [KEY], 'startedAt': ago(20 * DAY - 1800), 'endedAt': ago(20 * DAY - 1900), 'trigger': 'slack', 'outcome': 'done', 'usd': 0.1,
         'recap': 'Client adam@example.com was hit', 'ask': {'text': 'Why?', 'by': 'adam.baker', 'source': 'slack', 'thread': PERMALINK}},
        {'_id': 'r-a3', 'keys': [KEY], 'startedAt': ago(3 * DAY), 'trigger': 'action', 'outcome': 'done', 'recap': 'same 456 again', 'writes': []},
        {'_id': 'r-live', 'keys': [OTHER], 'startedAt': ago(1800), 'trigger': 'cards', 'outcome': 'done', 'recap': 'live one', 'writes': []},
        {'_id': 'r-old-live', 'keys': [OTHER], 'startedAt': ago(10 * DAY), 'trigger': 'cards', 'outcome': 'done', 'recap': 'earlier orders', 'writes': []},
        {'_id': 'r-report', 'keys': [], 'startedAt': ago(DAY), 'trigger': 'assess', 'outcome': 'done', 'recap': 'report'},
    ]
    actions = [
        {'_id': 'a-1', 'type': 'resolve', 'key': KEY, 'requestedAt': ago(3 * DAY - 600), 'requestedBy': 'op@example.com', 'status': 'done', 'reason': 'weekly 456 again', 'result': 'No action needed'},
        {'_id': 'a-2', 'type': 'slack', 'key': 'report:x', 'requestedAt': ago(DAY), 'status': 'done'},
        {'_id': 'a-3', 'type': 'known', 'key': 'log|old-gone|WARN|noise', 'requestedAt': ago(5 * DAY), 'requestedBy': 'op@example.com', 'status': 'done', 'reason': 'noise'},
    ]
    live = card_of(OTHER, first=3600)
    return runs, actions, {'anomalies': [live], 'history': [{'t': ago(20 * DAY), 'key': KEY, 'ev': 'new', 'sev': 'crit', 'text': f'CRIT {KEY}'}]}


def test_backfill_turns_runs_and_actions_into_occurrences():
    # given
    runs, actions, st = backfill_inputs()

    # when
    out = {d['_id']: d for d in history.build_backfill(runs, actions, st, NOW)}

    # then
    first, second = sorted((d for d in out.values() if d['key'] == KEY), key=lambda d: d['firstSeen'])
    assert first['firstSeen'] == ago(20 * DAY) and first['backfill'] is True
    assert (first['outcome'], first['ticket'], first['owner'], first['note'], first['recommend']) == ('progress', 'OPS-120', 'Oscar Green', 'ShipCorp answers 456', 'ask ShipCorp to allow the new IP')
    assert (first['svc'], first['sev'], first['runId'], first['recap'], first['runs']) == ('checkout-service', 'crit', 'r-a1', 'ShipCorp 456 invalid client ip', ['r-a2', 'r-a1'])
    [ask] = first['asks']
    assert (ask['question'], ask['by'], ask['thread'], ask['runId']) == ('Why?', 'adam.baker', PERMALINK, 'r-a2') and 'adam@example.com' not in ask['answer']
    assert (second['outcome'], second['note'], second['reopens'], second['runId']) == ('handled', 'weekly 456 again', 1, 'r-a3')
    gone = next(d for d in out.values() if d['key'] == 'log|old-gone|WARN|noise')
    assert (gone['outcome'], gone['svc'], gone['muted']) == ('known', 'old-gone', 'noise')
    orders = [d for d in out.values() if d['key'] == OTHER]
    assert [d['runId'] for d in orders] == ['r-old-live']
    assert not any(d['key'].startswith('report:') for d in out.values())


def test_backfill_runs_once_and_twice_writes_nothing_new(store: Store):
    # given
    runs, actions, st = backfill_inputs()
    seeded(store, *st['anomalies'])
    for r in runs:
        store.add_investigation({'id': r['_id'], **{k: v for k, v in r.items() if k != '_id'}})
    for a in actions:
        store.actions.insert_one(a)

    # when
    first = history.backfill(store, NOW)
    second = history.backfill(store, NOW)
    again = store.add_card_history_backfill(history.build_backfill(*backfill_inputs(), NOW))

    # then
    assert first == 4 and second == 0 and again == 0
    assert store.card_history.count_documents({}) == 4
    assert store.investigation('r-a1')['cardHistory'] == [f'{KEY}|{ago(20 * DAY)}']


def test_annotate_puts_the_seen_before_line_on_the_live_card(store: Store):
    # given
    live = card_of(KEY, first=3600)
    seeded(store, live)
    store.card_history.insert_many([
        {'_id': f'{KEY}|{ago(9 * DAY)}', 'key': KEY, 'firstSeen': ago(9 * DAY), 'lastSeen': ago(9 * DAY - 600), 'clearedAt': ago(9 * DAY - 300), 'outcome': 'cleared'},
        {'_id': f'{KEY}|{ago(2 * DAY)}', 'key': KEY, 'firstSeen': ago(2 * DAY), 'lastSeen': ago(2 * DAY - 600), 'clearedAt': ago(DAY), 'outcome': 'handled', 'ticket': 'OPS-120'},
        {'_id': f"{KEY}|{live['firstSeen']}", 'key': KEY, 'firstSeen': live['firstSeen'], 'outcome': 'open',
         'lastAsk': {'at': ago(600), 'by': 'adam.baker', 'line': 'one client, 40 lines'}},
    ])

    # when
    apply(store, [], NOW)

    # then
    seen = store.anomaly(KEY)['seenBefore']
    assert seen == {'count': 2, 'lastAt': ago(DAY), 'outcome': 'handled', 'ticket': 'OPS-120', 'slack': {'at': ago(600), 'line': 'one client, 40 lines'}}


def test_annotate_drops_the_line_when_nothing_was_seen_before(store: Store):
    # given
    seeded(store, card_of(KEY, seenBefore={'count': 1, 'lastAt': ago(DAY), 'outcome': 'handled'}))

    # when
    apply(store, [], NOW)

    # then
    assert 'seenBefore' not in store.anomaly(KEY)


def test_the_runs_of_a_card_come_with_their_trigger_and_slack_thread(client: TestClient, store: Store):
    # given
    store.add_investigation({'id': 'r-s', 'keys': [KEY], 'startedAt': ago(600), 'outcome': 'done', 'trigger': 'slack', 'mode': 'live', 'recap': 'answer',
                             'ask': {'text': 'Why?', 'by': 'adam.baker', 'source': 'slack', 'thread': PERMALINK}})

    # when
    [run] = client.get('/api/investigations', params={'key': KEY}).json()

    # then
    assert (run['id'], run['trigger'], run['ask']['thread'], run['recap'], run['outcome']) == ('r-s', 'slack', PERMALINK, 'answer', 'done')


def test_the_parts_of_a_second_instance_key_leave_its_instance_tail_out():
    # when
    east, main = history.key_parts('err|shop/checkout-service@east', {'east'}), history.key_parts('err|shop/checkout-service', {'east'})
    mail = history.key_parts('log|mailer|ERROR|bounce from postmaster@corp', {'east'})

    # then
    assert east == main == {'svc': 'checkout-service', 'ns': 'shop', 'label': 'err'}
    assert mail['label'] == 'ERROR bounce from postmaster@corp', 'only a known instance name is an instance tail'


def test_a_backfilled_card_of_a_second_instance_that_is_closed_leaves_its_instance_tail_out():
    # given
    key = 'err|shop/checkout-service@east'
    runs = [{'_id': 'r-east', 'keys': [key], 'startedAt': ago(5 * DAY), 'trigger': 'cards', 'outcome': 'done', 'recap': 'east errors', 'writes': []}]

    # when
    [doc] = history.build_backfill(runs, [], {'anomalies': []}, NOW, {'main', 'east'})

    # then
    assert (doc['key'], doc['svc'], doc['ns'], doc['label']) == (key, 'checkout-service', 'shop', 'err')
