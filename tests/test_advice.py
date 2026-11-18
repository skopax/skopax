from fastapi.testclient import TestClient

from tests.conftest import AUTH, anomaly, state


def seed(client: TestClient, *items: dict) -> None:
    client.put('/api/state', json=state(*items), headers=AUTH)


def find(client: TestClient, key: str) -> dict:
    return next(a for a in client.get('/api/state').json()['anomalies'] if a['key'] == key)


def test_assessment_sets_suspects_recommendation_and_code(client: TestClient):
    # given
    seed(client, anomaly('log|checkout-service|ERROR|boom'))
    body = {
        'key': 'log|checkout-service|ERROR|boom',
        'suspects': ['deploy at 09:02 changed the client', 'provider timeout'],
        'recommend': 'roll back the 09:02 image',
        'code': [{'label': 'CartClient.kt:42', 'url': 'https://gitlab.com/acme/x/-/blob/abc/CartClient.kt#L42', 'by': [{'name': 'Dev', 'lines': 3}]}],
    }

    # when
    res = client.post('/api/assessments', json=body, headers=AUTH)

    # then
    assert res.status_code == 200
    assert res.json() == {'rev': 2}
    a = find(client, 'log|checkout-service|ERROR|boom')
    assert a['suspects'] == body['suspects']
    assert a['recommend'] == 'roll back the 09:02 image'
    assert a['code'][0]['url'] == body['code'][0]['url']
    assert a['code'][0]['by'] == [{'name': 'Dev', 'lines': 3}]
    assert a['assessedAt']


def test_an_assessment_over_the_api_cannot_store_a_suggested_fix_because_its_current_lines_were_not_read_from_gitlab(client: TestClient):
    # given
    seed(client, anomaly('k'))
    code = {'label': 'A.kt:3', 'url': 'https://gitlab.com/acme/x/-/blob/abc/A.kt#L3', 'repo': 'x', 'lines': '3', 'current': 'made up', 'suggestion': 'fixed()', 'language': 'kotlin'}

    # when
    res = client.post('/api/assessments', json={'key': 'k', 'code': [code]}, headers=AUTH)

    # then
    assert res.status_code == 200
    assert find(client, 'k')['code'] == [{'label': 'A.kt:3', 'url': 'https://gitlab.com/acme/x/-/blob/abc/A.kt#L3', 'repo': 'x', 'lines': '3'}]


def test_assessment_keeps_fields_it_does_not_send(client: TestClient):
    # given
    seed(client, anomaly('k', recommend='old advice', suspects=['old']))

    # when
    client.post('/api/assessments', json={'key': 'k', 'suspects': ['new']}, headers=AUTH)

    # then
    a = find(client, 'k')
    assert a['suspects'] == ['new']
    assert a['recommend'] == 'old advice'


def test_assessment_clears_fields_sent_empty(client: TestClient):
    # given
    seed(client, anomaly('k', recommend='old advice', suspects=['old'], code=[{'label': 'a', 'url': 'https://gitlab.com/a'}]))

    # when
    res = client.post('/api/assessments', json={'key': 'k', 'suspects': [], 'recommend': '', 'code': []}, headers=AUTH)

    # then
    assert res.status_code == 200
    a = find(client, 'k')
    assert 'suspects' not in a
    assert 'recommend' not in a
    assert 'code' not in a


def test_assessment_for_an_unknown_key_is_not_found(client: TestClient):
    # when
    res = client.post('/api/assessments', json={'key': 'missing', 'recommend': 'x'}, headers=AUTH)

    # then
    assert res.status_code == 404


def test_assessment_rejects_a_code_link_without_a_valid_url(client: TestClient):
    # given
    seed(client, anomaly('k'))

    # when
    res = client.post('/api/assessments', json={'key': 'k', 'code': [{'label': 'x', 'url': 'not a url'}]}, headers=AUTH)

    # then
    assert res.status_code == 422


def test_note_marks_the_item_investigated_unless_in_progress(client: TestClient):
    # given
    seed(client, anomaly('a'), anomaly('b', triage='progress'))

    # when
    client.post('/api/notes', json={'key': 'a', 'text': 'checked, provider blip'}, headers=AUTH)
    client.post('/api/notes', json={'key': 'b', 'text': 'still on it'}, headers=AUTH)

    # then
    assert find(client, 'a')['note']['text'] == 'checked, provider blip'
    assert find(client, 'a')['triage'] == 'dived'
    assert find(client, 'b')['triage'] == 'progress'


