import threading

from tests.conftest import anomaly
from tests.fake_anthropic import reply, text
from tests.test_investigator import NOW, investigator, seed
from skopax.engine import iso
from skopax.store import LEASE_ID, Store

FIRST, SECOND = 'log|a|ERROR|x', 'log|b|ERROR|y'


def held_first_turn(inv) -> tuple[threading.Event, threading.Event]:
    inside, release = threading.Event(), threading.Event()
    answer = inv.client.beta.messages.create

    def slow(**kw):
        if not inside.is_set():
            inside.set()
            release.wait(5)
        return answer(**kw)

    inv.client.beta.messages.create = slow
    return inside, release


def test_a_second_thread_of_the_same_pod_waits_while_a_card_run_holds_the_card_lease(store: Store):
    # given
    seed(store, anomaly(FIRST, firstSeen=iso(NOW - 600)), anomaly(SECOND, firstSeen=iso(NOW - 500)))
    inv = investigator(store, [reply(text('recap one'), stop='end_turn'), reply(text('recap two'), stop='end_turn')], investigate_max_cards=1)
    inside, release = held_first_turn(inv)
    first = {}
    worker = threading.Thread(target=lambda: first.update(record=inv.run_once(NOW)))
    worker.start()
    assert inside.wait(5)

    # when
    second = inv.run_once(NOW)
    lease = store.leases.find_one({'_id': LEASE_ID})
    release.set()
    worker.join(5)

    # then
    assert second is None
    assert lease is not None and lease['owner'] == inv.owner
    assert first['record']['outcome'] == 'done'
    assert [r['keys'] for r in store.list_investigations(None, 5)] == [first['record']['keys']]
    assert store.leases.find_one({'_id': LEASE_ID}) is None


def test_the_next_card_run_of_the_pod_starts_once_the_first_one_ended(store: Store):
    # given
    seed(store, anomaly(FIRST, firstSeen=iso(NOW - 600)), anomaly(SECOND, firstSeen=iso(NOW - 500)))
    inv = investigator(store, [reply(text('recap one'), stop='end_turn'), reply(text('recap two'), stop='end_turn')], investigate_max_cards=1)

    # when
    runs = [inv.run_once(NOW), inv.run_once(NOW)]

    # then
    assert [r['outcome'] for r in runs] == ['done', 'done']
    assert {k for r in runs for k in r['keys']} == {FIRST, SECOND}
