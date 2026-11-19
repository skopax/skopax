import json
import time
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from skopax import messages, promqlchecks
from skopax.app import create_app
from skopax.plugins import prometheus
from skopax.promqlchecks import PASS
from skopax.sealer import master_key
from skopax.push import Pusher
from skopax.slack import anomaly_message
from tests.conftest import AUTH, settings, state
from tests.fake_http import FakeHttp
from tests.plugin_kit import PROM_URL, add_instance
from tests.test_admin_small_talk import admin_of
from tests.test_auth import ROLE, app_client

NOW = float(int(time.time()))
Q = 'redis_memory_used_bytes / redis_memory_max_bytes * 100'
NASTY = '<!channel> & *bold* <@U123|x> <img src=x onerror=alert(1)>'
BASE = {'name': 'Redis memory', 'source': 'prometheus', 'instance': 'main', 'query': Q, 'op': '>', 'threshold': 90, 'by': ['instance'],
        'title': '{instance} memory at {value} %', 'text': 'Redis {instance} is nearly full.'}


def prom(rows: list[tuple[dict, float]]) -> FakeHttp:
    answer = {'status': 'success', 'data': {'resultType': 'vector', 'result': [{'metric': m, 'value': [NOW, str(v)]} for m, v in rows]}}
    return FakeHttp({('GET', f'{PROM_URL}/api/v1/query'): answer})


def serving(store, fake: FakeHttp) -> TestClient:
    store.save(state(), None)
    add_instance(store, prometheus.PLUGIN, 'main', sealer_key=master_key(store.db, settings()), primary=True, url=PROM_URL)
    return TestClient(create_app(settings(), store, transport=fake))


def open_cards(store) -> dict:
    return {a['key']: a for a in store.load().get('anomalies') or [] if a.get('src') == PASS and a['status'] == 'open'}


def test_only_admins_list_save_preview_or_delete_custom_checks(store):
    # given
    store.save(state(), None)
    guest, operator = app_client(store), admin_of(store, (ROLE,))
    calls = [('get', '/api/admin/custom-checks', None), ('post', '/api/admin/custom-checks', BASE), ('put', '/api/admin/custom-checks/redis-memory', BASE),
             ('delete', '/api/admin/custom-checks/redis-memory', None), ('post', '/api/admin/custom-checks/preview', BASE)]

    # when
    codes = [[getattr(c, m)(path, **({'json': body} if body is not None else {})).status_code for m, path, body in calls] for c in (guest, operator)]

    # then
    assert codes == [[401] * 5, [403] * 5]
    assert promqlchecks.Definitions(store.db).list() == []


def test_an_admin_adds_lists_changes_and_deletes_a_check_and_each_change_lands_in_activity(store):
    # given
    with serving(store, prom([])) as c:
        # when
        made = c.post('/api/admin/custom-checks', json=BASE, headers=AUTH)
        again = c.post('/api/admin/custom-checks', json=BASE, headers=AUTH)
        listed = c.get('/api/admin/custom-checks', headers=AUTH).json()
        changed = c.put('/api/admin/custom-checks/redis-memory', json=BASE | {'id': 'other-id', 'name': 'Cache memory', 'threshold': 95}, headers=AUTH)
        gone = c.delete('/api/admin/custom-checks/redis-memory', headers=AUTH)
        missing = [c.put('/api/admin/custom-checks/redis-memory', json=BASE, headers=AUTH).status_code, c.delete('/api/admin/custom-checks/redis-memory', headers=AUTH).status_code]
        empty = c.get('/api/admin/custom-checks', headers=AUTH).json()

    # then
    assert made.status_code == 201 and made.json()['id'] == 'redis-memory' and made.json()['createdBy'] == made.json()['updatedBy']
    assert again.status_code == 409 and 'redis-memory' in again.json()['detail']
    assert [i['id'] for i in listed['items']] == ['redis-memory'] and listed['items'][0]['run'] is None
    assert listed['sources'] == {'prometheus': ['main'], 'grafana': []}
    assert listed['limits']['cards'] == promqlchecks.CARDS_MAX and listed['limits']['series'] == promqlchecks.SERIES_MAX
    assert listed['choices']['op'] == list(promqlchecks.COMPARISONS) and listed['choices']['severity'] == ['info', 'warn', 'crit']
    assert changed.status_code == 200 and (changed.json()['id'], changed.json()['name'], changed.json()['threshold']) == ('redis-memory', 'Cache memory', 95.0)
    assert gone.status_code == 204 and missing == [404, 404] and empty['items'] == []
    lines = [a['text'] for a in store.list_activity(None, 50) if a['kind'] == 'admin']
    assert lines == ['Custom check Cache memory deleted', 'Custom check Cache memory changed', 'Custom check Redis memory added']


@pytest.mark.parametrize('body, said', [
    (BASE | {'op': 'matches'}, 'comparison'),
    (BASE | {'title': '{instance!r}'}, 'title'),
    ([1, 2], 'object'),
    (BASE | {'baselines': [[1]]}, 'baseline'),
    (BASE | {'baselines': [{}]}, 'baseline'),
    (BASE | {'for': '\u00b2'}, 'for'),
    (BASE | {'severity': {}}, 'severity'),
])
def test_a_bad_check_is_refused_with_the_reason(store, body, said):
    # given
    with serving(store, prom([])) as c:
        # when
        saved = c.post('/api/admin/custom-checks', json=body, headers=AUTH)
        shown = c.post('/api/admin/custom-checks/preview', json=body, headers=AUTH)

    # then
    assert saved.status_code == shown.status_code == 422
    assert said in json.dumps(saved.json()) and said in json.dumps(shown.json())


def test_the_number_of_checks_is_capped(store, monkeypatch):
    # given
    monkeypatch.setattr(promqlchecks, 'CHECKS_MAX', 1)
    with serving(store, prom([])) as c:
        # when
        first = c.post('/api/admin/custom-checks', json=BASE, headers=AUTH)
        second = c.post('/api/admin/custom-checks', json=BASE | {'id': 'second'}, headers=AUTH)

    # then
    assert first.status_code == 201 and second.status_code == 409 and 'at most 1' in second.json()['detail']


def test_preview_runs_the_query_once_and_shows_the_cards_without_saving(store):
    # given
    fake = prom([({'instance': 'cache-a'}, 95), ({'instance': 'cache-b'}, 10)])
    with serving(store, fake) as c:
        # when
        got = c.post('/api/admin/custom-checks/preview', json=BASE, headers=AUTH).json()
        listed = c.get('/api/admin/custom-checks', headers=AUTH).json()

    # then
    assert got['ok'] and got['series'] == 2 and [x['title'] for x in got['cards']] == ['cache-a memory at 95 %']
    assert len(fake.hits(f'{PROM_URL}/api/v1/query')) == 1
    assert listed['items'] == [] and open_cards(store) == {}


def test_the_pass_runs_as_a_core_job_and_a_deleted_or_disabled_check_clears_its_cards_at_once(store):
    # given
    fake = prom([({'instance': 'cache-a'}, 95)])
    with serving(store, fake) as c:
        idle = [j.name for j in c.app.state.scheduler.jobs]
        c.post('/api/admin/custom-checks', json=BASE, headers=AUTH)
        c.post('/api/admin/custom-checks', json=BASE | {'id': 'other'}, headers=AUTH)
        jobs = {j.name: j for j in c.app.state.scheduler.jobs}
        c.app.state.custom_checks.run_once(NOW)
        opened = sorted(open_cards(store))
        run = c.get('/api/admin/custom-checks', headers=AUTH).json()['items'][0]['run']

        # when
        c.app.state.scheduler.queued()
        c.app.state.scheduler._nudged.clear()
        c.delete('/api/admin/custom-checks/other', headers=AUTH)
        nudged = PASS in c.app.state.scheduler.queued()
        after_delete = sorted(open_cards(store))
        c.put('/api/admin/custom-checks/redis-memory', json=BASE | {'enabled': False}, headers=AUTH)
        after_disable = sorted(open_cards(store))

    # then
    assert PASS not in idle and (jobs[PASS].period, jobs[PASS].offset) == (300, 75)
    assert [k.split('|')[1] for k in opened] == ['other', 'redis-memory']
    assert (run['state'], run['cards']) == ('ok', 1)
    assert [k.split('|')[1] for k in after_delete] == ['redis-memory'] and after_disable == []
    assert nudged, 'a delete asks for a pass soon'
    assert store.load()['serverPasses'][PASS]['status'] == 'ok'


def test_a_label_value_stays_inert_on_the_card_and_in_slack(store):
    # given
    fake = prom([({'instance': NASTY}, 95)])
    with serving(store, fake) as c:
        c.post('/api/admin/custom-checks', json=BASE, headers=AUTH)
        c.app.state.custom_checks.run_once(NOW)
        page = c.get('/', headers=AUTH).text

    # when
    (card,) = open_cards(store).values()
    message = anomaly_message(card, 'https://skopax.example.com/#c', 'server')
    text = json.dumps(message)

    # then
    inert = NASTY.replace('*', '\u2217')
    assert card['svc'] == 'Redis memory' and card['label'].startswith(inert) and card['what'].startswith(f'Redis {inert} is nearly full.') and card['checkLabels'] == {'instance': NASTY}
    assert '<!channel>' not in text and '<@U123' not in text and '<img' not in text and '*bold*' not in text
    assert '&lt;!channel&gt; &amp; \u2217bold\u2217' in message['text']
    assert message['blocks'][1]['text']['text'].startswith(f"*<https://skopax.example.com/#c|#{card['num']} Redis memory / &lt;!channel&gt; &amp; \u2217bold\u2217")
    posted = []
    pusher = Pusher(SimpleNamespace(token='xoxb-test', post=posted.append), 'U999', 'https://skopax.example.com')
    pusher.send([card])
    (dm,) = posted
    assert dm['channel'] == 'U999' and '&lt;!channel&gt; &amp; \u2217bold\u2217' in dm['text']
    assert '<!channel>' not in dm['text'] and '<@U123' not in dm['text'] and '*bold*' not in dm['text'] and '<img' not in dm['text']
    assert '<img src=x' not in page and '<!channel>' not in page


def test_history_lines_come_from_the_catalog(store, monkeypatch):
    # given
    for k in ('added', 'changed', 'deleted'):
        monkeypatch.setitem(messages.MESSAGES, f'custom_checks.{k}', f'<{k}> {{name}}')
    with serving(store, prom([])) as c:
        # when
        c.post('/api/admin/custom-checks', json=BASE, headers=AUTH)
        c.put('/api/admin/custom-checks/redis-memory', json=BASE, headers=AUTH)
        c.delete('/api/admin/custom-checks/redis-memory', headers=AUTH)

    # then
    assert [a['text'] for a in store.list_activity(None, 50) if a['kind'] == 'admin'] == ['<deleted> Redis memory', '<changed> Redis memory', '<added> Redis memory']
