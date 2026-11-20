import time

import pytest
from fastapi.testclient import TestClient

from skopax.app import create_app
from skopax.plugins import elasticsearch
from skopax.promqlchecks import PASS, ROWS_MAX
from skopax.sealer import master_key
from tests.conftest import AUTH, settings, state
from tests.fake_http import FakeHttp
from tests.plugin_kit import add_instance

NOW = float(int(time.time()))
ES_URL = 'https://es.example.com'
PATH = '/api/admin/custom-checks'
Q = 'FROM logs-* | WHERE @timestamp >= ?start AND @timestamp < ?end | STATS value = COUNT(*) BY service = service.name'
BASE = {'name': 'Busy services', 'source': 'elasticsearch', 'instance': 'central', 'query': Q, 'op': '>', 'threshold': 100, 'by': ['service'], 'window': 30,
        'title': '{service} at {value}', 'text': 'Service {service} wrote {value} lines.'}


def elastic(rows: list[dict]) -> FakeHttp:
    names = list(dict.fromkeys(k for r in rows for k in r))
    return FakeHttp({('POST', f'{ES_URL}/_query'): {'columns': [{'name': n, 'type': 'long'} for n in names], 'values': [[r.get(n) for n in names] for r in rows]}})


def serving(store, fake: FakeHttp) -> TestClient:
    store.save(state(), None)
    add_instance(store, elasticsearch.PLUGIN, 'central', sealer_key=master_key(store.db, settings()), primary=True, url=ES_URL)
    return TestClient(create_app(settings(), store, transport=fake))


def test_an_admin_adds_lists_and_previews_an_elasticsearch_check(store):
    # given
    fake = elastic([{'value': 250, 'service': 'shop'}])
    with serving(store, fake) as c:
        # when
        made = c.post(PATH, json=BASE, headers=AUTH)
        listed = c.get(PATH, headers=AUTH).json()
        shown = c.post(f'{PATH}/preview', json=BASE | {'query': 'FROM logs-* | STATS value = COUNT(*) BY service = service.name'}, headers=AUTH).json()

    # then
    assert made.status_code == 201 and made.json()['source'] == 'elasticsearch' and made.json()['window'] == 30
    item = listed['items'][0]
    assert (item['id'], item['source'], item['instance'], item['window']) == ('busy-services', 'elasticsearch', 'central', 30)
    assert listed['sources']['elasticsearch'] == ['central'] and 'elasticsearch' in listed['choices']['source']
    assert (listed['limits']['rows'], listed['limits']['windowMin'], listed['limits']['windowMax'], listed['limits']['window']) == (ROWS_MAX, 5, 1440, 60)
    assert shown['ok'] and shown['warnings'] == ['query has no ?start/?end: reads every document']
    assert [x['title'] for x in shown['cards']] == ['shop at 250'] and shown['cards'][0]['key'].startswith(f'{PASS}|busy-services|')
    sent = fake.hits(f'{ES_URL}/_query')[-1][3]
    assert 'params' not in sent, 'a query without ?start or ?end gets no params'


@pytest.mark.parametrize('over, said', [
    ({'query': 'ROW value = 1'}, 'must start with FROM'),
    ({'query': 'FROM logs-* | ' + 'x' * 2000}, 'at most 2000 characters'),
    ({'window': 0}, 'window'),
    ({'window': 2000}, 'window'),
    ({'window': 'soon'}, 'window'),
])
def test_a_query_that_does_not_start_with_from_is_too_long_or_has_a_bad_window_is_refused(store, over, said):
    # given
    with serving(store, elastic([])) as c:
        # when
        made = c.post(PATH, json=BASE | over, headers=AUTH)
        shown = c.post(f'{PATH}/preview', json=BASE | over, headers=AUTH)

    # then
    assert made.status_code == 422 and said in made.json()['detail']
    assert shown.status_code == 422 and said in shown.json()['detail']
