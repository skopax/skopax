import time

import pytest
from fastapi.testclient import TestClient

from skopax import messages, promqlchecks
from skopax.app import create_app
from skopax.checkpresets import presets
from skopax.plugins import grafana, prometheus
from skopax.sealer import master_key
from tests.conftest import AUTH, settings, state
from tests.fake_http import FakeHttp
from tests.plugin_kit import PROM_URL, add_instance
from tests.test_admin_small_talk import admin_of
from tests.test_auth import ROLE, app_client

NOW = float(int(time.time()))
GRAFANA = 'https://grafana.example.com'
PATH = '/api/admin/custom-checks'
COUCHBASE = {'instance': 'main', 'cluster': 'orders-prod', 'selector': 'job="couchbase"'}
REDIS = {'instance': 'ops', 'datasource': 'cloud-metrics', 'name': 'cache-prod'}


def vector(rows) -> dict:
    return {'status': 'success', 'data': {'resultType': 'vector', 'result': [{'metric': m, 'value': [NOW, str(v)]} for m, v in rows]}}


def sources(answers: dict) -> FakeHttp:
    def answer(params, body):
        return vector(answers.get(params['query'][0], []))

    return FakeHttp({('GET', f'{PROM_URL}/api/v1/query'): answer, ('GET', f'{GRAFANA}/api/datasources'): [{'uid': 'ds-1', 'name': 'cloud-metrics', 'type': 'prometheus'}],
                     ('GET', f'{GRAFANA}/api/datasources/proxy/uid/ds-1/api/v1/query'): answer})


def serving(store, fake: FakeHttp) -> TestClient:
    store.save(state(), None)
    key = master_key(store.db, settings())
    add_instance(store, prometheus.PLUGIN, 'main', sealer_key=key, primary=True, url=PROM_URL)
    add_instance(store, grafana.PLUGIN, 'ops', sealer_key=key, primary=True, url=GRAFANA, token='test-token-1')
    return TestClient(create_app(settings(), store, transport=fake))


def query_of(preset_id: str, values: dict, key: str) -> str:
    _, checks = presets()[preset_id].fill(values)
    prefix = {'couchbase': f"cb-{values.get('cluster')}-", 'managed-redis': f"redis-{values.get('name')}-"}[preset_id]
    return next(c['query'] for c in checks if c['id'] == prefix + key)


def test_only_admins_list_render_or_add_presets(store):
    # given
    store.save(state(), None)
    guest, operator = app_client(store), admin_of(store, (ROLE,))
    calls = [('get', f'{PATH}/presets', None), ('post', f'{PATH}/presets/couchbase/render', {'values': COUCHBASE}), ('post', f'{PATH}/presets/couchbase', {'values': COUCHBASE})]

    # when
    codes = [[getattr(c, m)(path, **({'json': body} if body is not None else {})).status_code for m, path, body in calls] for c in (guest, operator)]

    # then
    assert codes == [[401] * 3, [403] * 3]
    assert promqlchecks.Definitions(store.db).list() == []


def test_the_presets_are_listed_with_their_placeholders(store):
    # given
    with serving(store, sources({})) as c:
        # when
        got = c.get(f'{PATH}/presets', headers=AUTH)
        listed = c.get(PATH, headers=AUTH).json()

    # then
    assert got.status_code == 200
    items = got.json()['items']
    assert [(i['id'], i['name'], i['source']) for i in items] == [('couchbase', 'Couchbase', 'prometheus'), ('csp', 'CSP reports', 'elasticsearch'), ('managed-redis', 'Managed Redis', 'grafana')]
    assert [p['key'] for p in items[2]['placeholders']] == ['instance', 'datasource', 'name', 'selector', 'prefix', 'label']
    assert listed['presets'] == [{'id': 'couchbase', 'name': 'Couchbase', 'nameKey': 'cluster'}, {'id': 'csp', 'name': 'CSP reports', 'nameKey': 'name'}, {'id': 'managed-redis', 'name': 'Managed Redis', 'nameKey': 'name'}]


def test_render_fills_the_placeholders_and_each_check_previews_against_a_fake_prometheus(store):
    # given
    down = query_of('couchbase', COUCHBASE, 'down')
    fake = sources({down: [({'instance': 'node-1.example.com:8091'}, 0), ({'instance': 'node-2.example.com:8091'}, 1)]})
    with serving(store, fake) as c:
        # when
        rendered = c.post(f'{PATH}/presets/couchbase/render', json={'values': COUCHBASE}, headers=AUTH)
        checks = rendered.json()['checks']
        shown = [c.post(f'{PATH}/preview', json=d, headers=AUTH).json() for d in checks[:2]]
        listed = c.get(PATH, headers=AUTH).json()

    # then
    assert rendered.status_code == 200 and rendered.json()['values'] == COUCHBASE
    assert [d['id'] for d in checks] == [f"cb-orders-prod-{k['key']}" for k in presets()['couchbase'].checks]
    assert checks[0]['query'] == 'max by (instance) (up{job="couchbase"})'
    assert shown[0]['ok'] and [x['title'] for x in shown[0]['cards']] == ['node-1.example.com:8091 down']
    assert shown[0]['cards'][0]['sev'] == 'crit' and shown[0]['cards'][0]['text'].startswith('Couchbase node node-1.example.com:8091 in orders-prod is down')
    assert shown[1]['ok'] and shown[1]['cards'] == []
    assert listed['items'] == []


def test_a_grafana_preset_previews_through_the_datasource_and_adds_its_checks(store):
    # given
    memory = query_of('managed-redis', REDIS, 'memory-crit')
    fake = sources({memory: [({'instance': 'cache-a:6379'}, 97.5)]})
    with serving(store, fake) as c:
        checks = c.post(f'{PATH}/presets/managed-redis/render', json={'values': REDIS}, headers=AUTH).json()['checks']
        crit = next(d for d in checks if d['id'] == 'redis-cache-prod-memory-crit')

        # when
        shown = c.post(f'{PATH}/preview', json=crit, headers=AUTH).json()
        made = c.post(f'{PATH}/presets/managed-redis', json={'values': REDIS}, headers=AUTH)
        listed = c.get(PATH, headers=AUTH).json()
        c.app.state.custom_checks.run_once(NOW)
        opened = [a for a in store.load().get('anomalies') or [] if a.get('src') == promqlchecks.PASS and a['status'] == 'open']

    # then
    assert shown['ok'] and [(x['title'], x['sev']) for x in shown['cards']] == [('cache-a:6379 memory at 97.5 %', 'crit')]
    assert fake.hits(f'{GRAFANA}/api/datasources/proxy/uid/ds-1/api/v1/query')
    assert made.status_code == 201 and [d['id'] for d in made.json()['items']] == [d['id'] for d in checks]
    assert all(i['preset'] == 'managed-redis' and i['presetValues']['name'] == 'cache-prod' for i in listed['items'])
    assert [(a['svc'], a['label'], a['sev']) for a in opened] == [('Redis cache-prod memory critical', 'cache-a:6379 memory at 97.5 %', 'crit')]
    lines = [a['text'] for a in store.list_activity(None, 50) if a['kind'] == 'admin']
    assert lines == [f'Preset Managed Redis added for cache-prod: {len(checks)} custom checks']


def test_an_added_preset_check_keeps_its_tag_through_an_edit_from_the_page(store):
    # given
    with serving(store, sources({})) as c:
        made = c.post(f'{PATH}/presets/couchbase', json={'values': COUCHBASE}, headers=AUTH).json()['items']
        body = {k: v for k, v in made[0].items() if k in promqlchecks.FIELDS and k != 'id'} | {'threshold': 0.9}

        # when
        changed = c.put(f"{PATH}/{made[0]['id']}", json=body, headers=AUTH)
        gone = c.delete(f"{PATH}/{made[1]['id']}", headers=AUTH)
        listed = c.get(PATH, headers=AUTH).json()['items']

    # then
    assert changed.status_code == 200 and (changed.json()['preset'], changed.json()['threshold']) == ('couchbase', 0.9)
    assert gone.status_code == 204 and len(listed) == len(made) - 1
    assert all(i['preset'] == 'couchbase' for i in listed)


def test_the_same_preset_twice_is_refused_naming_the_existing_checks(store):
    # given
    with serving(store, sources({})) as c:
        c.post(f'{PATH}/presets/couchbase', json={'values': COUCHBASE}, headers=AUTH)

        # when
        again = c.post(f'{PATH}/presets/couchbase', json={'values': COUCHBASE}, headers=AUTH)
        other = c.post(f'{PATH}/presets/couchbase', json={'values': COUCHBASE | {'cluster': 'search-prod', 'selector': 'job="search"'}}, headers=AUTH)

    # then
    assert again.status_code == 409 and 'Couchbase orders-prod node down' in again.json()['detail']
    assert other.status_code == 201


def test_the_cap_refuses_a_whole_preset_and_leaves_no_partial_checks(store, monkeypatch):
    # given
    monkeypatch.setattr(promqlchecks, 'CHECKS_MAX', len(presets()['couchbase'].checks) + 1)
    with serving(store, sources({})) as c:
        c.post(f'{PATH}/presets/managed-redis', json={'values': REDIS}, headers=AUTH)

        # when
        refused = c.post(f'{PATH}/presets/couchbase', json={'values': COUCHBASE}, headers=AUTH)
        listed = c.get(PATH, headers=AUTH).json()['items']

    # then
    assert refused.status_code == 409 and 'at most' in refused.json()['detail']
    assert {i['preset'] for i in listed} == {'managed-redis'}


@pytest.mark.parametrize('values, said', [
    (COUCHBASE | {'selector': 'job="a"} or up{x="1"'}, 'Selector'),
    (COUCHBASE | {'selector': 'job="a" or vector(1)'}, 'Selector'),
    (COUCHBASE | {'cluster': 'Orders Prod'}, 'Cluster'),
    (['main'], 'object'),
])
def test_bad_placeholder_values_are_refused_before_any_query(store, values, said):
    # given
    fake = sources({})
    with serving(store, fake) as c:
        # when
        rendered = c.post(f'{PATH}/presets/couchbase/render', json={'values': values}, headers=AUTH)
        made = c.post(f'{PATH}/presets/couchbase', json={'values': values}, headers=AUTH)
        listed = c.get(PATH, headers=AUTH).json()['items']

    # then
    assert rendered.status_code == made.status_code == 422
    assert said in rendered.json()['detail'] and said in made.json()['detail']
    assert listed == [] and fake.hits(f'{PROM_URL}/api/v1/query') == []


def test_an_unknown_preset_is_404(store):
    # given
    with serving(store, sources({})) as c:
        # when
        codes = [c.post(f'{PATH}/presets/none/render', json={'values': {}}, headers=AUTH).status_code,
                 c.post(f'{PATH}/presets/none', json={'values': {}}, headers=AUTH).status_code]

    # then
    assert codes == [404, 404]


def test_the_preset_history_line_comes_from_the_catalog(store, monkeypatch):
    # given
    monkeypatch.setitem(messages.MESSAGES, 'custom_checks.preset_added', '<{preset}> {name} {count}')
    with serving(store, sources({})) as c:
        # when
        c.post(f'{PATH}/presets/couchbase', json={'values': COUCHBASE}, headers=AUTH)

    # then
    assert [a['text'] for a in store.list_activity(None, 50) if a['kind'] == 'admin'] == [f"<Couchbase> orders-prod {len(presets()['couchbase'].checks)}"]


@pytest.mark.parametrize('preset_id, values', [('couchbase', COUCHBASE), ('managed-redis', REDIS)])
def test_a_selector_that_matches_nothing_previews_as_exactly_one_no_series_card(store, preset_id, values):
    # given
    def answer(params, body):
        return vector([({}, 1)] if params['query'][0].startswith('absent(') else [])

    fake = FakeHttp({('GET', f'{PROM_URL}/api/v1/query'): answer, ('GET', f'{GRAFANA}/api/datasources'): [{'uid': 'ds-1', 'name': 'cloud-metrics', 'type': 'prometheus'}],
                     ('GET', f'{GRAFANA}/api/datasources/proxy/uid/ds-1/api/v1/query'): answer})
    with serving(store, fake) as c:
        checks = c.post(f'{PATH}/presets/{preset_id}/render', json={'values': values}, headers=AUTH).json()['checks']

        # when
        shown = [(d['id'], c.post(f'{PATH}/preview', json=d, headers=AUTH).json()) for d in checks]

    # then
    cards = [(check_id, x['sev'], x['text']) for check_id, r in shown for x in r['cards']]
    assert all(r['ok'] for _, r in shown)
    assert len(cards) == 1 and cards[0][0].endswith('-no-series') and cards[0][1] == 'crit'
    assert 'matches no' in cards[0][2]
