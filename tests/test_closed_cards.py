import datetime as dt

import pytest
from fastapi.testclient import TestClient

from tests.conftest import anomaly
from skopax.store import Store, iso, utc_now

DAY = 86400
LIVE = 'log|checkout-service|ERROR|still firing'


def ago(seconds: float) -> str:
    return iso(utc_now() - dt.timedelta(seconds=seconds))


def row(key: str, last_days: float, outcome: str = 'handled', first_days: float | None = None, **extra) -> dict:
    first, last = ago((first_days if first_days is not None else last_days + 0.1) * DAY), ago(last_days * DAY)
    return {'_id': f'{key}|{first}', 'key': key, 'svc': key.split('|')[1], 'label': 'ERROR pattern', 'ns': '-', 'src': 'logs', 'sev': 'warn', 'firstSeen': first, 'lastSeen': last,
            'clearedAt': last, 'outcome': outcome, 'triage': 'handled', 'note': 'Supplier answered 503 for ten minutes\nsecond line', 'recommend': 'nothing to do',
            'events': [{'ev': outcome, 'at': last}], 'asks': [{'at': last, 'by': 'nina.white@example.com', 'question': 'is this the supplier?'}], 'runs': ['r1'], 'reopens': 0, **extra}


def test_closed_cards_list_only_occurrences_that_left_the_page_newest_first_with_the_list_fields(client: TestClient, store: Store):
    # given
    live = anomaly(LIVE, firstSeen=ago(3600))
    store.save_state({'initialized': True, 'anomalies': [live]}, None)
    store.card_history.insert_many([
        row('log|payments-api|WARN|a', 1, num=41, ticket='OPS-140', owner='nina.white@example.com'),
        row('log|inventory-service|ERROR|b', 3, outcome='cleared'),
        row(LIVE, 0.01, outcome='progress', first_days=3600 / DAY),
        row(LIVE, 2, outcome='handled'),
        row('log|checkout-service|ERROR|old', 20),
    ])
    store.card_ids.insert_one({'_id': 'log|inventory-service|ERROR|b', 'n': 77})

    # when
    got = client.get('/api/history/cards').json()

    # then
    assert [(r['key'], r['outcome']) for r in got['items']] == [('log|payments-api|WARN|a', 'handled'), (LIVE, 'handled'), ('log|inventory-service|ERROR|b', 'cleared')], 'the live occurrence and the 20 day old one stay out'
    assert got['total'] == 3 and got['page'] == 0 and got['size'] == 50
    first = got['items'][0]
    assert first == {'key': 'log|payments-api|WARN|a', 'num': 41, 'svc': 'payments-api', 'ns': '-', 'label': 'ERROR pattern', 'sev': 'warn', 'src': 'logs', 'firstSeen': first['firstSeen'],
                     'lastSeen': first['lastSeen'], 'clearedAt': first['lastSeen'], 'outcome': 'handled', 'ticket': 'OPS-140', 'owner': 'nina.white@example.com', 'reopens': 0,
                     'note': 'Supplier answered 503 for ten minutes'}
    assert got['items'][2]['num'] == 77, 'a row written before cards had numbers gets its number from the card ids'
    assert 'asks' not in str(got) and 'recommend' not in str(got) and 'events' not in str(got)
    assert set(got['outcomes']) >= {'handled', 'auto-resolved', 'progress', 'muted', 'known', 'cleared', 'open'}


def test_text_number_outcome_and_window_narrow_the_list(client: TestClient, store: Store):
    # given
    store.save_state({'initialized': True, 'anomalies': []}, None)
    store.card_history.insert_many([
        row('log|payments-api|WARN|a', 1, num=41),
        row('log|inventory-service|ERROR|b', 3, outcome='muted', muted='known batch noise'),
        row('log|checkout-service|ERROR|c', 40, outcome='known'),
    ])

    # when
    by_text = client.get('/api/history/cards', params={'q': 'INVENTORY'}).json()
    by_number = client.get('/api/history/cards', params={'q': '#41'}).json()
    by_outcome = client.get('/api/history/cards', params={'outcome': 'muted'}).json()
    wider = client.get('/api/history/cards', params={'days': 60}).json()
    between = client.get('/api/history/cards', params={'from': ago(5 * DAY)[:10], 'to': ago(2 * DAY)}).json()
    regex = client.get('/api/history/cards', params={'q': '.*('}).json()

    # then
    assert [r['key'] for r in by_text['items']] == ['log|inventory-service|ERROR|b'] and by_text['items'][0]['muted'] == 'known batch noise'
    assert [r['num'] for r in by_number['items']] == [41]
    assert [r['outcome'] for r in by_outcome['items']] == ['muted']
    assert len(wider['items']) == 3 and wider['total'] == 3
    assert [r['key'] for r in between['items']] == ['log|inventory-service|ERROR|b']
    assert regex['items'] == [], 'the text is matched literally, not as a pattern'


def test_pages_and_the_limits(client: TestClient, store: Store):
    # given
    store.save_state({'initialized': True, 'anomalies': []}, None)
    store.card_history.insert_many([row(f'log|svc-{i:02d}|ERROR|x', 0.5 + i * 0.01) for i in range(7)])

    # when
    one = client.get('/api/history/cards', params={'size': 3}).json()
    three = client.get('/api/history/cards', params={'size': 3, 'page': 2}).json()
    refused = [client.get('/api/history/cards', params=p).status_code for p in ({'days': 366}, {'size': 101}, {'outcome': 'gone'}, {'from': 'yesterday'},
                                                                              {'from': ago(DAY), 'to': ago(2 * DAY)}, {'page': -1})]

    # then
    assert [r['key'] for r in one['items']] == ['log|svc-00|ERROR|x', 'log|svc-01|ERROR|x', 'log|svc-02|ERROR|x'] and one['total'] == 7
    assert [r['key'] for r in three['items']] == ['log|svc-06|ERROR|x']
    assert refused == [422] * 6


@pytest.mark.parametrize('bounds', [{'from': '0001-01-01T00:00:00+23:59'}, {'to': '9999-12-31T23:59:59-23:59'}])
def test_a_bound_past_the_ends_of_the_calendar_is_refused_like_any_bad_value(client: TestClient, store: Store, bounds: dict):
    # given
    store.save_state({'initialized': True, 'anomalies': []}, None)

    # when
    res = client.get('/api/history/cards', params=bounds)

    # then
    assert (res.status_code, res.json()['detail']) == (422, 'from and to are dates like 2026-11-04 or ISO times like 2026-11-04T09:00:00Z')


def test_fixed_and_duplicate_are_outcomes_of_their_own_and_each_row_says_how_it_was_resolved(client: TestClient, store: Store):
    # given
    store.save_state({'initialized': True, 'anomalies': []}, None)
    fixed = {'kind': 'fixed', 'ref': 'OPS-140', 'by': 'nina.white@example.com', 'via': 'page', 'at': ago(DAY)}
    dup = {'kind': 'duplicate', 'ref': '#12', 'by': 'adam.baker@example.com', 'via': 'slack', 'at': ago(DAY)}
    store.card_history.insert_many([row('log|payments-api|WARN|a', 1, outcome='fixed', resolution=fixed), row('log|inventory-service|ERROR|b', 1.5, outcome='duplicate', resolution=dup),
                                    row('log|checkout-service|ERROR|c', 2)])

    # when
    only_fixed = client.get('/api/history/cards', params={'outcome': 'fixed'}).json()
    only_dup = client.get('/api/history/cards', params={'outcome': 'duplicate'}).json()
    every = client.get('/api/history/cards').json()

    # then
    assert [(r['key'], r['resolution']) for r in only_fixed['items']] == [('log|payments-api|WARN|a', fixed)]
    assert [(r['key'], r['resolution']['ref']) for r in only_dup['items']] == [('log|inventory-service|ERROR|b', '#12')]
    assert [r.get('resolution') for r in every['items']][-1] is None, 'an old handled row has no resolution and the page reads it as Resolved'
    assert {'fixed': 'Fixed', 'duplicate': 'Duplicate'}.items() <= every['outcomes'].items()


def test_the_list_is_open_like_the_page_state(store: Store, client: TestClient):
    # given
    store.save_state({'initialized': True, 'anomalies': []}, None)

    # when
    got = client.get('/api/history/cards')

    # then
    assert got.status_code == 200 and got.json()['items'] == []
