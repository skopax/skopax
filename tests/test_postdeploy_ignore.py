import pytest
from fastapi.testclient import TestClient

from tests.conftest import AUTH, RELEASE_IGNORE
from tests.test_auth import ROLE, TokenEndpoint, access_token, app_client, log_in
from skopax.store import Store, iso, utc_now


def test_the_ignore_list_reads_the_default_until_the_token_saves_one(store: Store, client: TestClient):
    # when
    resp = client.get('/api/postdeploy-ignore')

    # then
    assert resp.status_code == 200
    assert resp.json() == {'services': list(RELEASE_IGNORE), 'updatedAt': None, 'updatedBy': None}


def test_only_the_service_token_can_change_the_ignore_list(store: Store):
    # given
    operator = app_client(store, TokenEndpoint(access_token([ROLE])))
    log_in(operator)
    body = {'services': [' help-center ', 'status-board', 'help-center']}

    # when
    as_operator = operator.put('/api/postdeploy-ignore', json=body)
    as_token = operator.put('/api/postdeploy-ignore', json=body, headers=AUTH)
    read = operator.get('/api/postdeploy-ignore').json()

    # then
    assert as_operator.status_code == 401
    assert as_token.status_code == 200
    saved = as_token.json()
    assert saved['services'] == ['help-center', 'status-board'] and saved['updatedBy'] == 'token:bootstrap' and saved['updatedAt'].startswith(iso(utc_now())[:10])
    assert read == saved
    line = store.list_activity(None, 5)[0]
    assert (line['kind'], line['who'], line['text']) == ('switch', 'token:bootstrap', 'Deploy check ignores 2 services: help-center, status-board')


def test_an_empty_list_checks_every_service(store: Store, client: TestClient):
    # when
    resp = client.put('/api/postdeploy-ignore', json={'services': []}, headers=AUTH)

    # then
    assert resp.json()['services'] == []
    assert store.list_activity(None, 1)[0]['text'] == 'Deploy check ignores 0 services: none'


@pytest.mark.parametrize('body', [{'services': ['Help-Center']}, {'services': ['bad name']}, {'services': ['-x']}, {'services': ['x'] * 51},
                                  {'services': 'help-center'}, {}, {'services': [], 'all': True}])
def test_the_ignore_list_body_is_validated(store: Store, client: TestClient, body):
    # when
    resp = client.put('/api/postdeploy-ignore', json=body, headers=AUTH)

    # then
    assert resp.status_code == 422
    assert store.postdeploy_ignore() is None
