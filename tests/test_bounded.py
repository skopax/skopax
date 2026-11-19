import threading

import pytest

from skopax.bounded import Bound, Busy, TimedOut


def test_a_finished_call_leaves_no_entry_behind_for_its_owner():
    # given
    bound = Bound('prune')

    # when
    answers = [bound.call(f'owner-{i}', lambda i=i: i, 5, wait=True) for i in range(50)]
    bound.call('plain', lambda: 'x', 5)
    for worker in [t for t in threading.enumerate() if t.name == bound.name]:
        worker.join(5)

    # then
    assert answers == list(range(50))
    assert bound._since == {} and bound._busy == {}


def test_a_failed_call_is_pruned_too():
    # given
    bound = Bound('prune')

    # when
    with pytest.raises(ZeroDivisionError):
        bound.call('owner', lambda: 1 / 0, 5, wait=True)
    for worker in [t for t in threading.enumerate() if t.name == bound.name]:
        worker.join(5)

    # then
    assert bound._since == {} and bound._busy == {}


def test_a_call_that_timed_out_keeps_its_entry_while_it_still_runs_so_a_stuck_owner_is_still_seen():
    # given
    now = [100.0]
    bound = Bound('prune', clock=lambda: now[0])
    gate, started = threading.Event(), threading.Event()

    def held() -> str:
        started.set()
        gate.wait(5)
        return 'late'

    first = threading.Thread(target=lambda: pytest.raises(TimedOut, bound.call, 'owner', held, 0.2, wait=True))
    first.start()
    started.wait(5)
    first.join(5)
    now[0] += 50

    # when
    with pytest.raises(Busy):
        bound.call('owner', lambda: 'again', 5, wait=True, stuck=10)
    kept = dict(bound._since)
    gate.set()
    for worker in [t for t in threading.enumerate() if t.name == bound.name]:
        worker.join(5)

    # then
    assert list(kept) == ['owner']
    assert bound._since == {} and bound._busy == {}
    assert bound.call('owner', lambda: 'free', 5, wait=True) == 'free'


def test_a_new_call_that_took_the_owner_over_is_not_pruned_by_the_old_one():
    # given
    bound = Bound('prune')
    gate, started, second_in = threading.Event(), threading.Event(), threading.Event()
    got: dict = {}

    def first() -> str:
        started.set()
        gate.wait(5)
        return 'first'

    def second() -> str:
        second_in.set()
        gate.wait(5)
        return 'second'

    one = threading.Thread(target=lambda: got.update(one=bound.call('owner', first, 5, wait=True)))
    two = threading.Thread(target=lambda: got.update(two=bound.call('owner', second, 5, wait=True)))

    # when
    one.start()
    started.wait(5)
    two.start()
    gate.set()
    one.join(5)
    two.join(5)

    # then
    assert got == {'one': 'first', 'two': 'second'}
    assert bound._since == {} and bound._busy == {}
