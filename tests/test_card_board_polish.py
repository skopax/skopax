import logging
import re
from pathlib import Path

from pymongo.errors import PyMongoError

from tests.conftest import AUTH, anomaly, state
from tests.test_tag_responsibles import ROLE, app_client, signed_in
from skopax import store as store_module
from skopax.store import TAG_HUE_NAMES, Store

KEY = 'log|checkout-service|ERROR|boom'
OTHER = 'log|shop-web|ERROR|other'


def test_an_admin_picks_a_tag_colour_that_every_viewer_gets_and_reset_goes_back_to_the_hash(store: Store, client):
    # when
    picked = client.put('/api/admin/tags/seo/color', json={'color': 3}, headers=AUTH)
    same = client.put('/api/admin/tags/seo/color', json={'color': 3}, headers=AUTH)
    seen = {t['id']: t.get('color') for t in client.get('/api/me', headers=AUTH).json()['tags']}
    admin = {t['id']: t.get('color') for t in client.get('/api/admin/tags', headers=AUTH).json()['tags']}
    reset = client.put('/api/admin/tags/seo/color', json={'color': None}, headers=AUTH)
    after = {t['id']: t.get('color') for t in client.get('/api/me', headers=AUTH).json()['tags']}

    # then
    assert picked.status_code == 200 and picked.json()['color'] == 3 and same.status_code == 200
    assert seen['seo'] == 3 and seen['developer'] is None and admin['seo'] == 3
    assert reset.status_code == 200 and reset.json().get('color') is None and after['seo'] is None
    lines = [a['text'] for a in store.list_activity(None, 50) if a['text'].startswith('Colour of')]
    assert lines == ['Colour of SEO: Olive \u2192 default', 'Colour of SEO: default \u2192 Olive']


def test_a_tag_colour_is_one_of_the_ten_hues_of_a_known_tag(client):
    # when
    codes = [client.put('/api/admin/tags/seo/color', json={'color': c}, headers=AUTH).status_code for c in (10, -1, 'red', 2.5)]
    unknown = client.put('/api/admin/tags/nope/color', json={'color': 1}, headers=AUTH)

    # then
    assert codes == [422, 422, 422, 422]
    assert unknown.status_code == 404


def test_only_admins_pick_tag_colours(store: Store):
    # given
    store.save(state(), None)
    guest, viewer, operator = app_client(store), signed_in(store, ()), signed_in(store, (ROLE,))

    # when
    codes = [c.put('/api/admin/tags/seo/color', json={'color': 1}).status_code for c in (guest, viewer, operator)]

    # then
    assert codes == [401, 403, 403]


def test_card_history_reads_the_actions_and_activity_of_one_card_only(store: Store, client):
    # given
    store.save_state(state(anomaly(KEY), anomaly(OTHER)), None)
    store.add_action({'type': 'take', 'target': KEY, 'key': KEY, 'label': 'Take', 'requestedBy': 'dora.lane@example.com'}, 'done')
    store.add_action({'type': 'take', 'target': OTHER, 'key': OTHER, 'label': 'Take', 'requestedBy': 'dora.lane@example.com'}, 'done')
    store.add_activity('tags', 'dora.lane@example.com', f'SEO tag added \u00b7 {KEY}', key=KEY)
    store.add_activity('tags', 'dora.lane@example.com', f'SEO tag added \u00b7 {OTHER}', key=OTHER)
    store.add_activity('access', 'admin@example.com', f'secret change \u00b7 {KEY}', key=KEY)

    # when
    got = client.get('/api/card-history', params={'key': KEY, 'limit': 50}, headers=AUTH)
    missing = client.get('/api/card-history', params={'key': 'log|nope|ERROR|x'}, headers=AUTH)
    too_many = client.get('/api/card-history', params={'key': KEY, 'limit': 1000}, headers=AUTH)

    # then
    assert got.status_code == 200
    body = got.json()
    assert [a['target'] for a in body['actions']] == [KEY]
    assert [a['text'] for a in body['activity'] if a['kind'] == 'tags'] == [f'SEO tag added \u00b7 {KEY}']
    assert missing.status_code == 404 and too_many.status_code == 422


def test_card_history_hides_access_lines_from_non_admins(store: Store):
    # given
    store.save_state(state(anomaly(KEY)), None)
    store.add_activity('access', 'admin@example.com', f'secret change \u00b7 {KEY}', key=KEY)
    store.add_activity('assign', 'dora.lane@example.com', f'assigned to Dora \u00b7 {KEY}', key=KEY)
    viewer = signed_in(store, ())

    # when
    rows = viewer.get('/api/card-history', params={'key': KEY}).json()['activity']

    # then
    assert [r['kind'] for r in rows] == ['assign']


def test_the_hue_names_match_the_page_palette_in_order():
    # given
    page = (Path(__file__).parent.parent / 'skopax' / 'web' / 'static' / 'tagtones.js').read_text(encoding='utf-8')

    # when
    names = re.findall(r"\{name: '([A-Za-z]+)'", page)

    # then
    assert tuple(names) == TAG_HUE_NAMES


def test_tag_colours_fall_back_to_none_with_one_warning_when_mongo_fails(store: Store, caplog):
    # given
    class Down:
        def find(self, *a, **k):
            raise PyMongoError('down')
    real = store.db
    class Db:
        def __getitem__(self, name):
            return Down() if name == 'tag_colors' else real[name]
        def __getattr__(self, name):
            return getattr(real, name)
    store.db = Db()

    # when
    with caplog.at_level(logging.WARNING):
        got = store.tag_colors()

    # then
    assert got == {}
    warned = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warned) == 1 and 'tag colours' in warned[0].getMessage()


def test_tag_colours_warn_once_a_minute_while_mongo_stays_down(store: Store, caplog, monkeypatch):
    # given
    class Down:
        def find(self, *a, **k):
            raise PyMongoError('down')
    real = store.db
    class Db:
        def __getitem__(self, name):
            return Down() if name == 'tag_colors' else real[name]
        def __getattr__(self, name):
            return getattr(real, name)
    store.db = Db()
    now = [1000.0]
    monkeypatch.setattr(store_module.time, 'monotonic', lambda: now[0])

    # when
    with caplog.at_level(logging.WARNING):
        results = [store.tag_colors()]
        now[0] += 30
        results += [store.tag_colors(), store.tag_colors()]
        quiet = len([r for r in caplog.records if 'tag colours' in r.getMessage()])
        now[0] += 31
        results.append(store.tag_colors())

    # then
    assert results == [{}, {}, {}, {}]
    assert quiet == 1, 'one warning for the first minute'
    assert len([r for r in caplog.records if 'tag colours' in r.getMessage()]) == 2, 'and the next one a minute later'
