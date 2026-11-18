import datetime as dt

import pytest

from tests.conftest import anomaly, state
from skopax.store import NotFound, Store

AT = dt.datetime(2026, 10, 24, 12, 0, tzinfo=dt.timezone.utc)
BEFORE = '2026-10-24T12:00:00Z'
SINCE = '2026-01-01T00:00:00Z'


def slack_ask(store: Store, channel: str = 'C1', thread_ts: str = '1.1', where: str = 'card', **over) -> dict:
    return store.add_action({'type': 'ask', 'source': 'slack', 'key': 'ask', 'target': f'slack:{channel}:{thread_ts}', 'text': 'why?', 'cards': [],
                             'requestedBy': 'adam.baker@example.com', 'slack': {'where': where, 'channel': channel, 'threadTs': thread_ts, 'ts': thread_ts, 'user': 'U1'}, **over})


def test_a_card_is_found_by_its_post_and_post_channels_cover_cards_and_reports(store: Store):
    # given
    store.save(state(anomaly('k1'), anomaly('k2')), None)
    store.slack_posted('k1', {'channel': 'C1', 'ts': '1792224000.000100', 'at': '2026-10-24T09:00:00Z', 'by': 'server'})
    store.deploy_reports.insert_one({'_id': 'r1', 'createdAt': '2026-10-24T09:00:00Z', 'slackPost': {'channel': 'C2', 'ts': '1792224000.000200'}})

    # when
    hit = store.anomaly_by_post('C1', '1792224000.000100')
    other_channel = store.anomaly_by_post('C2', '1792224000.000100')

    # then
    assert hit['key'] == 'k1' and '_id' not in hit
    assert other_channel is None
    assert store.post_channels() == {'C1', 'C2'}


def test_seen_dedupes_a_message_and_intake_records_list_newest_first(store: Store):
    # when
    first = store.seen('C1', '1.1', AT)
    again = store.seen('C1', '1.1', AT)
    store.seen('C1', '1.2', AT + dt.timedelta(minutes=1))
    store.record_intake('C1', '1.1', {'decision': 'accepted', 'gate': {'onTopic': True}})
    store.record_intake('C1', '1.2', {'decision': 'not-operator', 'user': 'U9'})

    # then
    assert (first, again) == (False, True)
    rows = store.list_intake(10)
    assert [r['id'] for r in rows] == ['C1:1.2', 'C1:1.1']
    assert 'atDt' not in rows[0] and rows[1]['decision'] == 'accepted'
    assert store.count_gate_calls('2026-10-24T11:00:00Z') == 1
    assert store.count_gate_calls('2026-10-24T13:00:00Z') == 0


def test_page_and_slack_asks_are_counted_apart(store: Store):
    # given
    store.add_action({'type': 'ask', 'key': 'ask', 'target': 'ask', 'text': 'old page ask'})
    store.add_action({'type': 'ask', 'source': 'page', 'key': 'ask', 'target': 'ask', 'text': 'page ask'})
    slack_ask(store)

    # then
    assert store.count_actions('ask', SINCE) == 3
    assert store.count_actions('ask', SINCE, exclude_source='slack') == 2
    assert store.count_actions('ask', SINCE, source='slack') == 1


def test_open_thread_ask_and_channel_ask_root(store: Store):
    # given
    card_ask = slack_ask(store, 'C1', '1.1')
    root = slack_ask(store, 'C1', '2.2', where='channel')
    slack_ask(store, 'C1', '2.2', where='channel')
    store.update_action(card_ask['id'], 'done', 'answered', None)

    # then
    assert store.open_thread_ask('C1', '1.1') is None
    assert store.open_thread_ask('C1', '2.2')['id'] == root['id']
    assert store.channel_ask_root('C1', '2.2')['id'] == root['id']
    assert store.channel_ask_root('C1', '1.1') is None


def test_stale_page_asks_and_slack_asks_are_failed_apart(store: Store):
    # given
    page = store.add_action({'type': 'ask', 'source': 'page', 'key': 'ask', 'target': 'ask', 'text': 'q'})
    dive = store.add_action({'type': 'dive', 'key': 'k1', 'target': 'k1'})
    waiting = slack_ask(store, 'C1', '1.1')
    running = slack_ask(store, 'C1', '2.2')
    fresh = slack_ask(store, 'C1', '3.3')
    for a in (page, dive, running):
        store.actions.update_one({'_id': a['id']}, {'$set': {'status': 'processing', 'processingAt': '2026-10-24T10:00:00Z'}})
    store.actions.update_one({'_id': waiting['id']}, {'$set': {'requestedAt': '2026-10-24T10:00:00Z'}})
    store.actions.update_one({'_id': fresh['id']}, {'$set': {'requestedAt': '2026-10-24T12:30:00Z'}})

    # when
    failed = store.fail_stale_actions(BEFORE)
    slack = store.fail_stale_slack_asks(BEFORE)

    # then
    assert failed == 2
    assert sorted(a['id'] for a in slack) == sorted([waiting['id'], running['id']])
    assert {a['status'] for a in slack} == {'failed'}
    assert store.get_action(fresh['id'])['status'] == 'requested'


def test_a_ticket_claim_is_taken_once_and_released_only_without_a_key(store: Store):
    # given
    act = slack_ask(store)

    # when
    won = store.claim_ticket(act['id'], 'adam.baker')
    lost = store.claim_ticket(act['id'], 'nina')
    store.release_ticket(act['id'])
    again = store.claim_ticket(act['id'], 'nina')
    store.ticket_created(act['id'], 'OPS-140')
    store.release_ticket(act['id'])

    # then
    assert won['ticket']['claimedBy'] == 'adam.baker' and lost is None
    assert again['ticket']['claimedBy'] == 'nina'
    assert store.get_action(act['id'])['ticket']['key'] == 'OPS-140'


def test_a_card_action_claim_is_taken_once_and_released_only_before_it_is_applied(store: Store):
    # given
    act = slack_ask(store)

    # when
    won = store.claim_card_action(act['id'], 'adam.baker')
    lost = store.claim_card_action(act['id'], 'nina')
    store.release_card_action(act['id'])
    again = store.claim_card_action(act['id'], 'nina')
    store.card_action_applied(act['id'])
    store.release_card_action(act['id'])

    # then
    assert won['cardActionApplied']['claimedBy'] == 'adam.baker' and lost is None
    assert again['cardActionApplied']['claimedBy'] == 'nina'
    kept = store.get_action(act['id'])['cardActionApplied']
    assert kept['claimedBy'] == 'nina' and kept['at']


def test_get_action_and_set_action_fields_raise_on_an_unknown_id(store: Store):
    # given
    act = slack_ask(store)

    # when
    store.set_action_fields(act['id'], {'slackReply': {'ts': '9.9'}})

    # then
    assert store.get_action(act['id'])['slackReply'] == {'ts': '9.9'}
    with pytest.raises(NotFound):
        store.get_action('nope')
    with pytest.raises(NotFound):
        store.set_action_fields('nope', {'x': 1})
