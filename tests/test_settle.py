import datetime as dt

from tests.test_deploy_reports import report
from tests.conftest import RELEASE_IGNORE
from skopax.settle import SETTLED_BY, Settle
from skopax.store import Store, iso, utc_now

NOW = dt.datetime(2026, 10, 15, 6, 0, tzinfo=dt.timezone.utc)


def at(minutes: float) -> str:
    return iso(NOW - dt.timedelta(minutes=minutes))


def deploys(store: Store, *ages: tuple[str, float]) -> None:
    store.add_deploys([{'ns': 'shop', 'svc': svc, 'fromImage': '1a1a1a1a', 'toImage': f'{i:08x}', 'at': at(age)} for i, (svc, age) in enumerate(ages)])


def release(store: Store) -> Store:
    deploys(store, ('checkout-service', 300), ('checkout-service', 80), ('search-service', 70), ('orders-service', 55))
    return store


def test_a_settled_release_queues_one_postdeploy_action(store: Store):
    # given
    release(store)
    settle = Settle(store, True)

    # when
    first = settle.run_once(NOW)
    again = settle.run_once(NOW + dt.timedelta(minutes=5))
    other_pod = Settle(Store(store.db), True).run_once(NOW + dt.timedelta(minutes=10))
    listed = store.list_actions(None, 10)

    # then
    assert first['type'] == 'postdeploy' and first['key'] == 'postdeploy' and first['status'] == 'requested'
    assert first['requestedBy'] == SETTLED_BY
    assert first['label'] == 'Post-deploy check: release settled, 3 services 04:40–05:05 UTC'
    assert first['release'] == {'firstAt': at(80), 'lastAt': at(55), 'lastId': first['release']['lastId'], 'deploys': 3, 'services': ['checkout-service', 'orders-service', 'search-service']}
    assert (again, other_pod) == (None, None)
    assert [a['id'] for a in listed] == [first['id']]


def test_deploys_of_services_on_the_default_ignore_list_neither_settle_a_release_nor_join_one(store: Store):
    # given
    deploys(store, ('help-ui', 80), ('checkout-service', 70), ('tools-gateway', 55))

    # when
    action = Settle(store, True).run_once(NOW)

    # then
    assert RELEASE_IGNORE == ('help-ui', 'tools-gateway', 'tools-mcp')
    assert action['release']['services'] == ['checkout-service'] and action['release']['deploys'] == 1
    assert action['release']['lastAt'] == at(70) and action['label'] == 'Post-deploy check: release settled, checkout-service 04:50 UTC'


def test_a_release_of_only_ignored_services_queues_nothing_until_the_list_is_emptied(store: Store):
    # given
    deploys(store, ('help-ui', 80), ('tools-mcp', 55))

    # when
    ignored = Settle(store, True).run_once(NOW)
    store.set_postdeploy_ignore({'services': []})
    counted = Settle(store, True).run_once(NOW)

    # then
    assert ignored is None
    assert counted['release']['services'] == ['help-ui', 'tools-mcp']


def test_a_release_still_landing_or_long_past_queues_nothing(store: Store):
    # given
    deploys(store, ('checkout-service', 50), ('orders-service', 10))

    # when
    running = Settle(store, True).run_once(NOW)
    stale = Settle(store, True).run_once(NOW + dt.timedelta(hours=4))

    # then
    assert (running, stale) == (None, None)
    assert store.list_actions(None, 10) == []


def test_only_a_report_that_names_the_last_deploy_covers_the_release(store: Store):
    # given
    release(store)
    last = store.deploys_since(at(60))[-1]
    store.add_deploy_report(report())
    store.deploy_reports.update_one({}, {'$set': {'createdAt': iso(NOW - dt.timedelta(minutes=5))}})
    other = Store(store.db)
    other.deploy_reports = store.db['reports_b']
    other.add_deploy_report(report(rows=[{'ns': 'payments', 'svc': last['svc'], 'toImage': last['toImage']}]))
    other.deploy_reports.update_one({}, {'$set': {'createdAt': iso(NOW - dt.timedelta(minutes=90))}})

    # when
    by_time = Settle(store, True).run_once(NOW)
    by_row = Settle(other, True).run_once(NOW)

    # then
    assert by_time is not None and by_time['requestedBy'] == SETTLED_BY, 'a report that does not name the last deploy no longer covers the release (22.10: 35 of 38 deploys were missed that way)'
    assert by_row is None


def test_an_open_or_a_later_postdeploy_action_covers_the_release(store: Store):
    # given
    release(store)
    asked = store.add_action({'type': 'postdeploy', 'key': 'postdeploy', 'requestedBy': 'oscar'})

    # when
    while_open = Settle(store, True).run_once(NOW)
    store.update_action(asked['id'], 'done', 'checked', None)
    after_done = Settle(store, True).run_once(NOW)
    store.actions.update_one({'_id': asked['id']}, {'$set': {'requestedAt': at(120), 'status': 'done'}})
    older_done = Settle(store, True).run_once(NOW)

    # then
    assert (while_open, after_done) == (None, None)
    assert older_done['requestedBy'] == SETTLED_BY


def test_a_later_wave_after_a_queued_one_queues_again(store: Store):
    # given
    release(store)
    first = Settle(store, True).run_once(NOW)
    store.update_action(first['id'], 'done', 'checked', None)
    store.actions.update_one({'_id': first['id']}, {'$set': {'requestedAt': iso(NOW)}})
    deploys(store, ('payments-api', -20))

    # when
    later = Settle(store, True).run_once(NOW + dt.timedelta(minutes=65))

    # then
    assert later['release']['services'] == ['checkout-service', 'orders-service', 'payments-api', 'search-service']
    assert later['id'] != first['id']


def test_the_default_clock_is_now(store: Store):
    # given
    deploys(store, ('checkout-service', 0))
    store.deploys.update_one({}, {'$set': {'at': iso(utc_now() - dt.timedelta(minutes=40))}})

    # when
    queued = Settle(store, True).run_once()

    # then
    assert queued['release']['deploys'] == 1
