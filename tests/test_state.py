from fastapi.testclient import TestClient

from tests.conftest import AUTH, anomaly, settings, state
from skopax.app import create_app


def test_put_then_get_returns_the_same_state_in_order(client: TestClient):
    # given
    body = state(anomaly('log|b|WARN|second x/y'), anomaly('alert|A|', sev='crit'))

    # when
    put = client.put('/api/state', json=body, headers=AUTH)
    got = client.get('/api/state').json()

    # then
    assert put.status_code == 200
    assert put.json() == {'rev': 1}
    assert got['rev'] == 1
    assert [a['key'] for a in got['anomalies']] == ['log|b|WARN|second x/y', 'alert|A|']
    assert got['sources'] == {'prom': 'ok'}
    assert 'pos' not in got['anomalies'][0]


def test_put_without_or_with_wrong_token_is_refused(client: TestClient):
    # when
    missing = client.put('/api/state', json=state())
    wrong = client.put('/api/state', json=state(), headers={'Authorization': 'Bearer nope'})

    # then
    assert missing.status_code == 401
    assert wrong.status_code == 401


def test_writes_are_refused_when_no_token_is_configured(store):
    # given
    with TestClient(create_app(settings(token=''), store)) as c:
        # when
        res = c.put('/api/state', json=state(), headers={'Authorization': 'Bearer '})

    # then
    assert res.status_code == 401


def test_put_with_a_stale_base_rev_conflicts(client: TestClient):
    # given
    client.put('/api/state', json=state(anomaly('a')), headers=AUTH)

    # when
    res = client.put('/api/state', json=state(anomaly('b')), headers={**AUTH, 'X-Base-Rev': '0'})

    # then
    assert res.status_code == 409
    assert res.json()['rev'] == 1
    assert [a['key'] for a in client.get('/api/state').json()['anomalies']] == ['a']


def test_put_with_the_current_base_rev_succeeds(client: TestClient):
    # given
    client.put('/api/state', json=state(anomaly('a')), headers=AUTH)

    # when
    res = client.put('/api/state', json=state(anomaly('b')), headers={**AUTH, 'X-Base-Rev': '1'})

    # then
    assert res.json() == {'rev': 2}


def test_put_drops_anomalies_missing_from_the_new_state(client: TestClient):
    # given
    client.put('/api/state', json=state(anomaly('a'), anomaly('b')), headers=AUTH)

    # when
    client.put('/api/state', json=state(anomaly('b')), headers=AUTH)

    # then
    assert [a['key'] for a in client.get('/api/state').json()['anomalies']] == ['b']


def test_put_rejects_anomalies_without_a_key(client: TestClient):
    # when
    res = client.put('/api/state', json={'anomalies': [{'sev': 'warn'}]}, headers=AUTH)

    # then
    assert res.status_code == 422


def test_rev_endpoint_reports_the_rev(client: TestClient):
    # given
    client.put('/api/state', json=state(anomaly('a')), headers=AUTH)

    # when
    res = client.get('/api/rev').json()

    # then
    assert res['rev'] == 1
    assert set(res) == {'rev', 'updatedAt'}
