import datetime as dt

from tests.conftest import anomaly
from skopax import summary
from skopax.store import Store, iso

NOW = dt.datetime(2026, 10, 21, 6, 30, tzinfo=dt.timezone.utc).timestamp()


def at(minutes_ago: float) -> str:
    return iso(dt.datetime.fromtimestamp(NOW - minutes_ago * 60, dt.timezone.utc))


def deploys(store: Store, *ages: tuple[str, float]) -> None:
    store.add_deploys([{'ns': 'shop', 'svc': svc, 'fromImage': '1a1a1a1a', 'toImage': f'{i:08x}', 'at': at(age)} for i, (svc, age) in enumerate(ages)])


def test_nothing_open_is_all_clear(store: Store):
    # when
    box = summary.build({'anomalies': []}, store, NOW)

    # then
    assert box == {'headline': 'All clear', 'points': [], 'level': 'ok', 'at': '2026-10-21T06:30:00Z'}


def test_open_cards_lead_worst_first_with_their_change_and_the_rest_are_counted(store: Store):
    # given
    cards = [
        anomaly('w1', 'warn', now=40, d1=5, d7=4, firstSeen='2026-10-21T05:00:00Z'),
        anomaly('c1', 'crit', svc='orders-service', label='5xx', now=300, d1=10, d7=20, firstSeen='2026-10-21T06:00:00Z'),
        anomaly('i1', 'info', svc='auth-service', label='p95', unit='ms', now=900, d1=600, d7=650),
        anomaly('m1', 'crit', muted='known'),
        anomaly('h1', 'crit', triage='handled'),
        anomaly('p1', 'warn', triage='progress'),
        anomaly('kid', 'warn', parent='w1'),
        anomaly('orphan', 'warn', svc='sms-sender', label='ERROR pattern', now=3, d1=0, d7=0, parent='cleared-parent', firstSeen='2026-10-21T06:10:00Z'),
        anomaly('cleared-parent', 'warn', status='cleared'),
        anomaly('w3', 'warn', svc='email-service', label='restarts', now=2, d1=0, d7=0, firstSeen='2026-10-21T06:20:00Z'),
    ]

    # when
    box = summary.build({'anomalies': cards}, store, NOW)

    # then
    assert box['level'] == 'crit'
    assert box['headline'] == 'orders-service 5xx ×15; checkout-service ERROR pattern ×8.0; sms-sender ERROR pattern new; email-service restarts new; 1 waiting to be addressed', 'information never counts'
    assert box['points'] == []


def test_a_release_adds_its_line_running_then_pending_then_report_ready(store: Store):
    # given
    deploys(store, ('checkout-service', 60), ('search-service', 50), ('orders-service', 5))

    # when
    running = summary.build({'anomalies': []}, store, NOW)['headline']
    deploys(store, ('payments-api', 40))
    store.delete_deploy(next(d['id'] for d in store.deploys_since(at(10)) if d['svc'] == 'orders-service'))
    pending = summary.build({'anomalies': []}, store, NOW)['headline']
    store.add_deploy_report({'summary': 'ok', 'createdAt': at(2), 'rows': []})
    ready = summary.build({'anomalies': []}, store, NOW)['headline']

    # then
    assert running == 'All clear; Release running since 05:30 UTC, 3 services'
    assert pending == 'All clear; Release 05:30–05:50 UTC (3 services) finished, post-deploy check pending'
    assert ready == 'All clear; Release 05:30–05:50 UTC (3 services): deploy report ready'


def test_a_release_older_than_three_hours_is_not_mentioned(store: Store):
    # given
    deploys(store, ('checkout-service', 200), ('search-service', 190))

    # then
    assert summary.build({'anomalies': []}, store, NOW)['headline'] == 'All clear'


def test_nothing_open_but_cards_waiting_is_a_warning_with_only_the_count(store: Store):
    # given
    cards = [
        anomaly('w1', 'warn', status='cleared', svc='email-service', label='restarts', firstSeen='2026-10-21T05:00:00Z'),
        anomaly('c1', 'crit', status='cleared', svc='orders-service', label='5xx', firstSeen='2026-10-21T06:00:00Z'),
    ]

    # when
    box = summary.build({'anomalies': cards}, store, NOW)

    # then
    assert box['level'] == 'warn'
    assert box['headline'] == '2 waiting to be addressed'


def test_many_waiting_cards_are_only_counted(store: Store):
    # given
    cards = [anomaly(f'k{i}', 'warn', status='cleared', svc=f'svc-{i}', label='5xx', firstSeen=f'2026-10-21T05:0{i}:00Z') for i in range(6)]

    # when
    box = summary.build({'anomalies': cards}, store, NOW)

    # then
    assert box['headline'] == '6 waiting to be addressed'
    assert box['level'] == 'warn'


def test_waiting_cards_follow_the_open_cards_and_keep_the_open_level(store: Store):
    # given
    cards = [
        anomaly('o1', 'warn', svc='orders-service', label='5xx', now=300, d1=10, d7=20),
        anomaly('w1', 'crit', status='cleared'),
    ]

    # when
    box = summary.build({'anomalies': cards}, store, NOW)

    # then
    assert box['level'] == 'warn'
    assert box['headline'] == 'orders-service 5xx ×15; 1 waiting to be addressed'


def test_waiting_count_sits_between_the_open_cards_and_the_release_line(store: Store):
    # given
    deploys(store, ('checkout-service', 60), ('search-service', 50))
    cards = [anomaly('o1', 'warn', svc='orders-service', label='5xx', now=3, d1=0, d7=0), anomaly('w1', 'warn', status='cleared')]

    # when
    box = summary.build({'anomalies': cards}, store, NOW)

    # then
    assert box['headline'] == 'orders-service 5xx new; 1 waiting to be addressed; Release 05:30–05:40 UTC (2 services) finished, post-deploy check pending'


def test_cleared_cards_that_are_muted_addressed_info_or_nested_do_not_wait(store: Store):
    # given
    cards = [
        anomaly('m1', 'crit', status='cleared', muted='known'),
        anomaly('h1', 'crit', status='cleared', triage='handled'),
        anomaly('p1', 'warn', status='cleared', triage='progress'),
        anomaly('i1', 'info', status='cleared'),
        anomaly('parent', 'warn', status='cleared', triage='handled'),
        anomaly('kid', 'warn', status='cleared', parent='parent'),
    ]

    # when
    box = summary.build({'anomalies': cards}, store, NOW)

    # then
    assert box['level'] == 'ok'
    assert box['headline'] == 'All clear'


def test_deploys_of_ignored_services_make_no_release_line(store: Store):
    # given
    store.set_postdeploy_ignore({'services': ['release-board', 'tracking-service'], 'updatedAt': at(1), 'updatedBy': 'token'})
    store.add_deploys([{'ns': 'platform', 'svc': 'release-board', 'fromImage': 'a', 'toImage': 'b', 'at': at(50)},
                       {'ns': 'platform', 'svc': 'tracking-service', 'fromImage': 'c', 'toImage': 'd', 'at': at(45)}])

    # when
    alone = summary.build({'anomalies': []}, store, NOW)['headline']
    deploys(store, ('checkout-service', 40))
    mixed = summary.build({'anomalies': []}, store, NOW)['headline']

    # then
    assert alone == 'All clear', 'an internal tool deploy is no release: no "post-deploy check pending" that never comes'
    assert mixed == 'All clear; Release 05:50–05:50 UTC (1 service) finished, post-deploy check pending', 'only the checked services count and set the times'
