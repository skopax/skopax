import pytest

from tests.conftest import anomaly
from tests.fake_anthropic import reply, text
from tests.test_investigator import KEY, NOW, seed
from tests.test_investigator_slack import slack_action, slack_investigator
from skopax import clock
from skopax.engine import iso
from skopax.investigator import STALE_AFTER_S
from skopax.store import Store


def asked(store: Store, age_s: float, ts: str) -> dict:
    act = slack_action(store, target=f'slack:C1:{ts}', slack={'where': 'card', 'channel': 'C1', 'threadTs': ts, 'ts': ts, 'user': 'U1', 'placeholderTs': ts, 'language': 'nl'})
    store.actions.update_one({'_id': act['id']}, {'$set': {'requestedAt': iso(NOW - age_s)}})
    return act


def halt(store: Store, why: str) -> None:
    if why == 'stopped':
        store.set_investigator_stop(True, 'nina.white@example.com')
    else:
        store.add_spend(clock.day(NOW), 1000.0)


@pytest.mark.parametrize('why', ['capped', 'stopped'])
def test_a_stale_slack_ask_is_failed_and_answered_while_the_investigator_is_halted(store: Store, why: str):
    # given
    seed(store, anomaly(KEY), history=[])
    stale = asked(store, STALE_AFTER_S + 600, '1792200000.000100')
    fresh = asked(store, 300, '1792223700.000100')
    inv, seen = slack_investigator(store, [])
    halt(store, why)

    # when
    record = inv.run_once(NOW)

    # then
    assert record is None
    assert (store.get_action(stale['id'])['status'], store.get_action(stale['id'])['result']) == ('failed', 'not picked up in time')
    assert store.get_action(fresh['id'])['status'] == 'requested'
    assert [(a['id'], outcome) for a, outcome, _ in seen] == [(stale['id'], 'failed')]
    assert inv.client.requests == []
    assert any(r['text'] == 'reaped 1 stale Slack ask' for r in store.list_activity(None, 10))


def test_a_free_room_fails_a_stale_slack_ask_instead_of_answering_it_hours_late(store: Store):
    # given
    seed(store, anomaly(KEY), history=[])
    stale = asked(store, STALE_AFTER_S + 600, '1792200000.000100')
    inv, seen = slack_investigator(store, [reply(text('A late answer.'), stop='end_turn')])

    # when
    record = inv.run_once(NOW)

    # then
    assert record is None or record.get('actionId') != stale['id']
    assert store.get_action(stale['id'])['status'] == 'failed'
    assert [(a['id'], outcome) for a, outcome, _ in seen] == [(stale['id'], 'failed')]
    assert inv.client.requests == []
