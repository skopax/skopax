from tests.test_settle import NOW, at, deploys
from skopax.settle import SETTLED_BY, Settle
from skopax.store import Store


def checking(store: Store, minutes: float) -> dict:
    act = store.add_action({'type': 'postdeploy', 'key': 'postdeploy', 'target': 'postdeploy', 'requestedBy': SETTLED_BY})
    store.update_action(act['id'], 'processing', None, 'requested')
    store.actions.update_one({'_id': act['id']}, {'$set': {'requestedAt': at(minutes), 'processingAt': at(minutes)}})
    return act


def test_a_check_left_processing_by_a_dead_pod_fails_and_the_next_release_is_checked(store: Store):
    # given
    deploys(store, ('checkout-service', 290), ('payments-api', 40))
    dead = checking(store, 250)

    # when
    queued = Settle(store, True).run_once(NOW)

    # then
    assert queued is not None and queued['release']['services'] == ['payments-api']
    assert (store.get_action(dead['id'])['status'], store.get_action(dead['id'])['result']) == ('failed', 'the pod died mid-run')


def test_a_check_still_running_keeps_covering_the_release(store: Store):
    # given
    deploys(store, ('payments-api', 40))
    running = checking(store, 10)

    # when
    queued = Settle(store, True).run_once(NOW)

    # then
    assert queued is None and store.get_action(running['id'])['status'] == 'processing'
