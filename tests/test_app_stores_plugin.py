import copy
import json

import jwt
import pytest
from fastapi.testclient import TestClient

from skopax.app import create_app
from skopax.contract import InstanceInfo
from skopax.engine import iso, merge
from skopax.instances import Instances, PluginState
from skopax.investigator import prompt
from skopax.investigator.tools import CORE_TOOLS
from skopax.kit.google import NEEDED, TOKEN_URL, GoogleAuth
from skopax.kube import Kube
from skopax.messages import msg
from skopax.plugins import app_stores
from skopax.plugins.app_stores import engine
from skopax.plugins.app_stores.engine import LOOKUP_URL, NO_PLAY, NO_REVIEWS, PACIFIC, PLAY_NEEDS, PLAY_SCOPES, PUBLISHER, REPORTING, RULES, AppStore, Play, run_appstore, run_play
from skopax.plugintools import PluginTools
from skopax.registry import BUILTIN, Registry, describe
from skopax.remote import HttpError, Unreachable
from skopax.runtime import Runtime
from skopax.sealer import Sealer, master_key
from skopax.store import Store
from skopax.sweeps import Reads
from tests.conftest import settings
from tests.fake_google import ACCESS, KEY, BROKEN_KEY, Google
from tests.fake_http import FakeHttp
from tests.plugin_kit import add_instance
from tests.test_investigator_tools import NOW, box

DAY = 86400
APP_ID = '1234567890'
EAST_ID = '9876543210'
PKG = 'com.example.shop'
VALUES = {'apple-app-id': APP_ID, 'countries': 'us', 'play-package': PKG, 'credentials': KEY}
SEAL = 'stores-key'
ALICE = 'alice@example.com'
DOT = ' \u00b7 '


def answer(count: int, avg: float, name: str = 'Shop', version: str = '1.2.6') -> dict:
    return {'results': [{'trackName': name, 'userRatingCount': count, 'averageUserRating': avg, 'version': version}]}


def lookup(count: int, avg: float) -> FakeHttp:
    return FakeHttp({('GET', LOOKUP_URL): answer(count, avg)})


def per_country(answers: dict) -> FakeHttp:
    def got(query, body):
        return answers[query['country'][0]]
    return FakeHttp({('GET', LOOKUP_URL): got})


def snapshots() -> list[dict]:
    return [{'at': iso(NOW - 9 * DAY), 'count': 400, 'avg': 4.6, 'version': '1.2.0'}, {'at': iso(NOW - DAY), 'count': 477, 'avg': 4.57652, 'version': '1.2.5'},
            {'at': iso(NOW - 6 * 3600), 'count': 490, 'avg': 4.55, 'version': '1.2.6'}]


def prior(country: str = 'us', app_id: str = APP_ID) -> dict:
    return {'appId': app_id, 'country': country, 'name': 'Shop', 'snapshots': snapshots()}


def day_row(day: int, rate: float, metric: str, users: int = 40000) -> dict:
    return {'aggregationPeriod': 'DAILY', 'startTime': {'year': 2026, 'month': 10, 'day': day, 'timeZone': {'id': PACIFIC}},
            'metrics': [{'metric': metric, 'decimalValue': {'value': str(rate)}}, {'metric': 'distinctUsers', 'decimalValue': {'value': str(users)}}]}


def review(stars: int, at: float) -> dict:
    return {'reviewId': 'r', 'authorName': 'x', 'comments': [{'userComment': {'starRating': stars, 'lastModified': {'seconds': str(int(at))}, 'appVersionName': '1.2.5'}}]}


FRESH = {'freshnessInfo': {'freshnesses': [{'aggregationPeriod': 'HOURLY', 'latestEndTime': {'year': 2026, 'month': 10, 'day': 17}},
                                           {'aggregationPeriod': 'DAILY', 'latestEndTime': {'year': 2026, 'month': 10, 'day': 16, 'timeZone': {'id': PACIFIC}}}]}}


def play_http(over: dict | None = None) -> Google:
    return Google({('GET', f'{REPORTING}/apps/{PKG}/crashRateMetricSet'): FRESH, ('GET', f'{REPORTING}/apps/{PKG}/anrRateMetricSet'): FRESH,
                   ('POST', f'{REPORTING}/apps/{PKG}/crashRateMetricSet:query'): {'rows': [day_row(8, 0.004, 'userPerceivedCrashRate'), day_row(15, 0.0125, 'userPerceivedCrashRate')]},
                   ('POST', f'{REPORTING}/apps/{PKG}/anrRateMetricSet:query'): {'rows': [day_row(8, 0.001, 'userPerceivedAnrRate'), day_row(15, 0.003, 'userPerceivedAnrRate')]},
                   ('GET', f'{PUBLISHER}/applications/{PKG}/reviews'): {'reviews': [review(1, NOW - 3600 * i) for i in range(1, 7)] + [review(5, NOW - 600), review(1, NOW - 2 * DAY)]},
                   ('GET', LOOKUP_URL): answer(497, 4.4930), **(over or {})})


def play(http) -> Play:
    return Play(GoogleAuth(KEY, http, lambda: NOW, PLAY_SCOPES), PKG)


class Ctx:
    def __init__(self, config: dict, transport, rows: dict | None = None, cards: list | None = None, rules: dict | None = None):
        self.config = config
        self.transport = transport
        self.now = NOW
        self.rows = rows or {}
        self.cards = cards or []
        self.given = rules or {}

    @property
    def instance(self) -> InstanceInfo:
        return InstanceInfo('main', True, None, self.config)

    def http(self):
        return self.transport

    def rules(self, set_id: str) -> dict:
        assert set_id == 'rules'
        return {**RULES, **self.given}

    def previous(self, section: str) -> list | None:
        return self.rows.get(section)

    def open_cards(self, plugin_wide: bool = False) -> list[dict]:
        return list(self.cards)


def tool_runtime(store, http, instances: dict | None = None) -> Runtime:
    reg = Registry.of(app_stores.PLUGIN)
    runtime = Runtime(reg, PluginState(store.db), Instances(store.db, reg, lambda: Sealer(SEAL)), http)
    runtime.reader = lambda plugin: Reads(store, plugin, None)
    for i, (name, values) in enumerate(({'main': VALUES} if instances is None else instances).items()):
        add_instance(store, app_stores.PLUGIN, name, sealer_key=SEAL, primary=i == 0, **values)
    runtime.refresh()
    return runtime


def stores_box(store: Store, http, instances: dict | None = None, **kw):
    return box(store, plugin_tools=PluginTools(tool_runtime(store, http, instances)), **kw)


def call(b, **args):
    text, err = b.call('stores', args)
    return (text if err else json.loads(text)), err


def app_with(store, tmp_path, http, instances: dict | None = None):
    sealing = master_key(store.db, settings())
    for i, (name, values) in enumerate(({'main': VALUES} if instances is None else instances).items()):
        add_instance(store, app_stores.PLUGIN, name, sealer_key=sealing, primary=i == 0, **values)
    return TestClient(create_app(settings(), store, registry=Registry.of(app_stores.PLUGIN), transport=http, kube=Kube(root=tmp_path)))


def seeded(store: Store, *rows: dict) -> None:
    store.save({'initialized': True, 'anomalies': [], 'sources': {}, 'muted': [], 'sweptIso': iso(NOW - 900), 'appStore': [dict(r, plugin='app-stores') for r in rows]}, None)


def by_key(state: dict) -> dict:
    return {a['key']: a for a in state['anomalies']}


def further(country: str) -> dict:
    return {'key': f'store-rating|-/ios-app|ratings:{country}', 'src': 'appstore', 'sub': f'ratings:{country}', 'country': country, 'ranKey': f'appstore:{country}', 'status': 'open'}


@pytest.mark.parametrize('avg, sev', [(4.4930, 'warn'), (4.4182, 'crit'), (4.60, None), (4.40, None)])
def test_app_store_works_out_the_average_of_the_new_ratings_against_a_day_earlier(avg, sev):
    # given
    store = AppStore(APP_ID, 'us', lookup(497, avg))

    # when
    out = run_appstore([store], [prior()], NOW, RULES)

    # then
    assert out.result.ok and [c['sev'] for c in out.result.found] == ([sev] if sev else [])
    assert [s['count'] for s in out.sections['appStore'][0]['snapshots']] == [477, 490, 497]
    if sev == 'warn':
        assert out.result.found[0]['now'] == 2.5 and '20 new App Store ratings (US store) in the last 24 h average 2.5 stars' in out.result.found[0]['what']
    if sev:
        assert engine.links_for(out.result.found[0]) == [{'label': 'App Store listing', 'url': f'https://apps.apple.com/us/app/id{APP_ID}'}]


def test_app_store_stays_quiet_under_the_floor_and_fails_on_an_empty_lookup():
    # when
    few = run_appstore([AppStore(APP_ID, 'us', lookup(480, 1.0))], [prior()], NOW, RULES)
    gone = run_appstore([AppStore(APP_ID, 'us', FakeHttp({('GET', LOOKUP_URL): {'results': []}}))], [prior()], NOW, RULES)

    # then
    assert few.result.ok and few.result.found == [] and few.result.ran == ('appstore',)
    assert not gone.result.ok and f'no app with id {APP_ID}' in gone.result.status


def test_app_store_with_a_zero_floor_stays_quiet_when_no_rating_came_in():
    # when
    out = run_appstore([AppStore(APP_ID, 'us', lookup(477, 4.57652))], [prior()], NOW, {**RULES, 'rating_floor': 0})

    # then
    assert out.result.ok and out.result.found == []


def test_the_app_store_keeps_one_rating_snapshot_an_hour_however_often_it_is_checked():
    # given
    store = AppStore(APP_ID, 'us', FakeHttp({('GET', LOOKUP_URL): answer(500, 4.5)}))
    recent = dict(prior(), snapshots=[{'at': iso(NOW - 26 * 3600), 'count': 480, 'avg': 4.5, 'version': '1.2.5'}, {'at': iso(NOW - 15 * 60), 'count': 499, 'avg': 4.5, 'version': '1.2.6'}])
    older = dict(prior(), snapshots=[{'at': iso(NOW - 26 * 3600), 'count': 480, 'avg': 4.5, 'version': '1.2.5'}, {'at': iso(NOW - 61 * 60), 'count': 499, 'avg': 4.5, 'version': '1.2.6'}])

    # when
    skip = run_appstore([store], [recent], NOW, RULES)
    keep = run_appstore([store], [older], NOW, RULES)

    # then
    assert [s['count'] for s in skip.sections['appStore'][0]['snapshots']] == [480, 499]
    assert [s['count'] for s in keep.sections['appStore'][0]['snapshots']] == [480, 499, 500]


def test_a_drop_card_that_climbs_back_while_still_low_is_not_pushed_again_as_escalated():
    # given
    def ratings(avg: float) -> list[dict]:
        base = {'at': iso(NOW - DAY), 'count': 1000, 'avg': 4.5}
        return engine.evaluate_ratings({'at': iso(NOW), 'count': 1020, 'avg': (4.5 * 1000 + avg * 20) / 1020}, base, APP_ID, 'us', RULES)

    state = {'initialized': True, 'anomalies': []}
    first, later = ratings(1.2), ratings(2.5)
    _, pushed, _ = merge(state, copy.deepcopy(first), {}, NOW)

    # when
    events, push, _ = merge(state, copy.deepcopy(later), {}, NOW + 3600)

    # then
    assert len(first) == len(later) == 1 and [a['key'] for a in pushed] == [first[0]['key']]
    assert [e['ev'] for e in events] == [] and push == []


def test_each_further_country_keys_its_cards_link_and_snapshot_row_by_its_code():
    # given
    http = per_country({'us': answer(497, 4.57), 'de': answer(497, 4.4930)})
    ctx = Ctx(dict(VALUES, countries='us, DE'), http, rows={'appStore': [prior('us'), prior('de')]})

    # when
    out = app_stores.run_appstore_check(ctx)

    # then
    assert (out.result.ok, out.result.status) == (True, 'ok')
    assert set(out.result.ran) == {'appstore', 'appstore:de'}
    [card] = out.result.found
    assert (card['key'], card['ranKey'], card['country']) == ('store-rating|-/ios-app|ratings:de', 'appstore:de', 'de')
    assert '(DE store)' in card['what']
    assert engine.links_for(card) == [{'label': 'App Store listing', 'url': f'https://apps.apple.com/de/app/id{APP_ID}'}]
    assert [(r['country'], r['appId'], 'plugin' in r, 'cluster' in r) for r in out.sections['appStore']] == [('us', APP_ID, False, False), ('de', APP_ID, False, False)]


def test_a_country_that_fails_keeps_its_cards_and_its_snapshots():
    # given
    http = per_country({'us': answer(497, 4.57), 'de': HttpError(500, LOOKUP_URL)})
    kept = prior('de')

    # when
    out = app_stores.run_appstore_check(Ctx(dict(VALUES, countries='us,de'), http, rows={'appStore': [prior('us'), kept]}))

    # then
    assert (out.result.ok, out.result.status, out.result.ran) == (True, 'error: de: App Store lookup HTTP 500', ('appstore',))
    assert out.sections['appStore'][1] == kept


def test_a_country_taken_off_the_list_closes_its_cards():
    # given
    http = per_country({'us': answer(497, 4.57)})

    # when
    out = app_stores.run_appstore_check(Ctx(VALUES, http, rows={'appStore': [prior('us')]}, cards=[further('gb')]))

    # then
    assert set(out.result.ran) == {'appstore', 'appstore:gb'}


def test_a_country_without_a_day_of_snapshots_is_no_data_and_clears_nothing():
    # given
    http = per_country({'us': answer(497, 4.57), 'de': answer(497, 4.4930)})

    # when
    none = app_stores.run_appstore_check(Ctx(VALUES, http))
    some = app_stores.run_appstore_check(Ctx(dict(VALUES, countries='us,de'), http, rows={'appStore': [prior('us')]}))

    # then
    assert (none.result.ok, none.result.status, none.result.ran) == (True, 'ok \u00b7 no data', ('appstore:no-data',))
    assert [s['count'] for s in none.sections['appStore'][0]['snapshots']] == [497], 'the first snapshot is kept for tomorrow'
    assert (some.result.status, some.result.ran) == ('ok \u00b7 no data for de', ('appstore',))


def test_countries_past_the_time_budget_are_left_unread():
    # given
    ticks = iter([0.0, 0.0, 8.0, 9.0])
    stores = [AppStore(APP_ID, c, per_country({'us': answer(497, 4.57), 'de': answer(497, 4.4930)})) for c in ('us', 'de')]

    # when
    out = run_appstore(stores, [prior('us'), prior('de')], NOW, RULES, clock=lambda: next(ticks), budget=10)

    # then
    assert (out.result.ok, out.result.status, out.result.ran) == (True, 'ok \u00b7 read 1 of 2 countries', ('appstore',))
    assert out.sections['appStore'][1] == prior('de')


@pytest.mark.parametrize('over, text', [({'countries': 'us,usa'}, 'countries must be two-letter store codes such as us, gb or de'),
                                        ({'countries': 'us,gb,de,fr,it,es,nl,se,no,dk,fi'}, 'countries takes at most 10 store codes'),
                                        ({'apple-app-id': 'id12'}, 'apple-app-id must be the numeric App Store id')])
def test_a_bad_app_store_setting_fails_the_check_with_a_fixed_text(over, text):
    # given
    http = per_country({'us': answer(497, 4.57)})

    # when
    out = app_stores.run_appstore_check(Ctx(dict(VALUES, **over), http))

    # then
    assert (out.result.ok, out.result.status) == (False, f'error: {text}')
    assert http.calls == []


def test_play_cards_vitals_over_the_bad_behaviour_line_a_doubled_anr_rate_and_a_run_of_low_reviews():
    # given
    http = play_http()

    # when
    out = run_play(play(http), NOW, RULES)

    # then
    assert out.result.ok
    cards = {c['key']: c for c in out.result.found}
    crash, anr, low = cards['play-vitals|-/android-app|crash'], cards['play-vitals|-/android-app|anr'], cards['store-rating|-/android-app|reviews']
    assert (crash['sev'], crash['now'], crash['d1']) == ('crit', 1.25, 0.4) and 'on 2026-10-15' in crash['what'] and '40,000 users' in crash['what']
    assert (anr['sev'], low['sev'], low['now']) == ('warn', 'warn', 6) and '6 of 7 Google Play reviews' in low['what']
    body = http.hits(f'{REPORTING}/apps/{PKG}/crashRateMetricSet:query')[0][3]
    assert body['timelineSpec'] == {'aggregationPeriod': 'DAILY', 'startTime': {'year': 2026, 'month': 10, 'day': 8, 'timeZone': {'id': PACIFIC}},
                                    'endTime': {'year': 2026, 'month': 10, 'day': 16, 'timeZone': {'id': PACIFIC}}}
    scope = jwt.decode(http.forms[0]['assertion'], options={'verify_signature': False})['scope']
    assert scope.split() == list(PLAY_SCOPES) and out.sections['play'][0]['reviews'] == {'last24h': 7, 'low24h': 6}


def test_each_part_clears_only_its_own_cards():
    # when
    out = run_play(play(play_http()), NOW, RULES)

    # then
    assert out.result.ok and out.result.status == 'ok'
    assert set(out.result.ran) == {'play', 'play:crash', 'play:anr', 'play-reviews', 'play:access:vitals', 'play:access:reviews'}, 'the bare key closes cards stored before the split'
    assert {c['key']: c['ranKey'] for c in out.result.found} == {'play-vitals|-/android-app|crash': 'play:crash', 'play-vitals|-/android-app|anr': 'play:anr',
                                                                'store-rating|-/android-app|reviews': 'play-reviews'}


def test_a_refused_reviews_call_keeps_the_vitals_cards_and_raises_a_warn_card_naming_the_permission():
    # given
    refused = HttpError(403, 'x', '{"error": {"message": "The caller does not have permission"}}')
    http = play_http({('GET', f'{PUBLISHER}/applications/{PKG}/reviews'): refused})

    # when
    out = run_play(play(http), NOW, RULES)

    # then
    assert (out.result.ok, out.result.status) == (True, 'ok \u00b7 no access to Play reviews')
    assert set(out.result.ran) == {'play', 'play:crash', 'play:anr', 'play:access:vitals', 'play:access:reviews'}, 'the review cards stay open'
    cards = {c['key']: c for c in out.result.found}
    assert set(cards) == {'play-vitals|-/android-app|anr', 'play-vitals|-/android-app|crash', 'no-access|-/play|reviews'}
    assert cards['no-access|-/play|reviews']['label'] == f'App stores cannot read Play reviews: the service account lacks {NEEDED}', 'no doc names the reviews permission'
    assert NO_REVIEWS in cards['no-access|-/play|reviews']['what'] and 'View app information' not in cards['no-access|-/play|reviews']['what']
    assert 'The caller does not have permission' not in json.dumps(out.result.found) + out.result.status
    assert set(out.sections['play'][0]['vitals']) == {'crash', 'anr'} and 'reviews' not in out.sections['play'][0]


def test_play_without_a_console_grant_says_how_to_invite_the_account():
    # given
    refused = HttpError(403, 'x', '{"error": {"message": "The caller does not have permission"}}')
    http = play_http({('GET', f'{REPORTING}/apps/{PKG}/crashRateMetricSet'): refused})

    # when
    out = run_play(play(http), NOW, RULES)

    # then
    assert (out.result.ok, out.result.status) == (True, 'ok \u00b7 no access to Play vitals')
    assert set(out.result.ran) == {'play-reviews', 'play:access:vitals', 'play:access:reviews'}
    card = next(c for c in out.result.found if c['ranKey'] == 'play:access:vitals')
    assert 'invite it under Users and permissions' in card['what'] and card['label'] == f'App stores cannot read Play vitals: the service account lacks {PLAY_NEEDS}'
    assert NO_PLAY in card['what']
    assert not Play(GoogleAuth(KEY, http, lambda: NOW, PLAY_SCOPES), '').enabled


def test_vitals_without_a_daily_row_are_no_data_and_a_server_error_names_its_part():
    # given
    hourly = {'freshnessInfo': {'freshnesses': [{'aggregationPeriod': 'HOURLY', 'latestEndTime': {'year': 2026, 'month': 10, 'day': 17}}]}}
    stale = play_http({('GET', f'{REPORTING}/apps/{PKG}/crashRateMetricSet'): hourly, ('GET', f'{REPORTING}/apps/{PKG}/anrRateMetricSet'): hourly})
    busy = play_http({('GET', f'{PUBLISHER}/applications/{PKG}/reviews'): HttpError(404, 'x')})
    down = play_http({('GET', f'{PUBLISHER}/applications/{PKG}/reviews'): HttpError(404, 'x'), ('GET', f'{REPORTING}/apps/{PKG}/crashRateMetricSet'): HttpError(404, 'x')})

    # when
    no_data, part, none = (run_play(play(h), NOW, RULES) for h in (stale, busy, down))

    # then
    assert (no_data.result.ok, no_data.result.status) == (True, 'ok \u00b7 no data for Play vitals')
    assert set(no_data.result.ran) == {'play-reviews', 'play:access:vitals', 'play:access:reviews'}, 'the vitals cards stay open'
    assert (part.result.ok, part.result.status, set(part.result.ran)) == (True, 'error: reviews: Play reviews HTTP 404', {'play', 'play:crash', 'play:anr', 'play:access:vitals'})
    assert (none.result.ok, none.result.status) == (False, 'error: vitals: Play vitals HTTP 404; reviews: Play reviews HTTP 404')


def test_a_part_that_failed_for_another_reason_keeps_its_keys_out_of_ran():
    # given
    vitals_down = play_http({('GET', f'{REPORTING}/apps/{PKG}/crashRateMetricSet'): HttpError(503, 'x'), ('GET', f'{REPORTING}/apps/{PKG}/anrRateMetricSet'): HttpError(503, 'x')})

    # when
    out = run_play(play(vitals_down), NOW, RULES)

    # then
    assert (out.result.ok, out.result.status) == (True, 'error: vitals: Play vitals HTTP 503')
    assert set(out.result.ran) == {'play-reviews', 'play:access:reviews'}, 'no vitals key, no vitals access key: its cards are held (P5-C129)'


def test_a_refused_key_gives_each_play_part_an_access_card_naming_both_causes():
    # given
    refused = Google(token=HttpError(400, 'x', '{"error": "invalid_grant"}'))

    # when
    out = run_play(play(refused), NOW, RULES)

    # then
    assert (out.result.ok, out.result.status) == (True, 'ok \u00b7 no access to Play vitals, Play reviews'), 'P5-C127a: a refused key is a refusal, not a failed pass'
    assert set(out.result.ran) == {'play:access:vitals', 'play:access:reviews'}
    assert {c['ranKey']: c['label'] for c in out.result.found} == {
        'play:access:vitals': f'App stores cannot read Play vitals: the service account is invalid or lacks {PLAY_NEEDS}',
        'play:access:reviews': f'App stores cannot read Play reviews: the service account is invalid or lacks {NEEDED}'}
    assert out.sections == {} and len(refused.forms) == 1, 'the stored row is kept, and the key is tried once'


def test_a_refused_key_holds_the_play_cards_and_its_access_cards_clear_once_it_is_read(store: Store, tmp_path):
    # given
    http = play_http()

    # when
    with app_with(store, tmp_path, http, instances={'main': dict(VALUES, **{'apple-app-id': ''})}) as c:
        c.app.state.sweeper.run('app-stores', 'play', NOW)
        before = store.load()
        http.token = HttpError(400, TOKEN_URL, '{"error": "invalid_grant"}')
        c.app.state.sweeper.run('app-stores', 'play', NOW)
        refused = store.load()
        http.token = {'access_token': ACCESS, 'expires_in': 3599}
        c.app.state.sweeper.run('app-stores', 'play', NOW)
        read = by_key(store.load())

    # then
    cards = by_key(refused)
    assert refused['sources']['play'] == 'ok \u00b7 no access to Play vitals, Play reviews'
    assert cards['play-vitals|-/android-app|crash']['status'] == 'open', 'the vitals card is held'
    assert cards['no-access|-/play|vitals']['status'] == cards['no-access|-/play|reviews']['status'] == 'open'
    assert refused['play'] == before['play'], 'the stored row is kept'
    assert read['no-access|-/play|vitals']['status'] != 'open' and read['no-access|-/play|reviews']['status'] != 'open'


def test_a_refused_key_in_the_stores_tool_names_both_causes(store: Store):
    # given
    http = play_http()
    http.token = HttpError(400, TOKEN_URL, '{"error": "invalid_grant"}')
    b = stores_box(store, http)

    # when
    vitals, reviews = b.call('stores', {'op': 'vitals'}), b.call('stores', {'op': 'reviews'})

    # then
    tail = ': Google refused the service account key (HTTP 400 invalid_grant)'
    assert vitals == (f'the service account is invalid or lacks {PLAY_NEEDS}{tail}', True)
    assert reviews == (f'the service account is invalid or lacks {NEEDED}{tail}', True)


def test_a_play_check_stops_at_its_time_budget(monkeypatch):
    # given
    monkeypatch.setattr(app_stores, 'BUDGET_S', 0)
    http = play_http()

    # when
    out = app_stores.run_play_check(Ctx(VALUES, http))

    # then
    assert (out.result.ok, out.result.status) == (False, 'error: Google reads stopped at their 0 s budget') and http.calls == []


def test_a_store_without_an_app_id_is_quiet():
    # given
    http = play_http()
    config = dict(VALUES, **{'apple-app-id': '', 'play-package': ''})

    # when
    apple = app_stores.run_appstore_check(Ctx(config, http, cards=[further('de')]))
    google = app_stores.run_play_check(Ctx(dict(config, credentials=''), http))

    # then
    assert (apple.result.ok, apple.result.status, apple.result.found) == (True, f"ok \u00b7 {msg('app-stores.not_set_up', store='App Store')}", [])
    assert set(apple.result.ran) == {'appstore', 'appstore:de'}, 'its old cards close (P5-C84)'
    assert (google.result.ok, google.result.status, google.result.found) == (True, f"ok \u00b7 {msg('app-stores.not_set_up', store='Google Play')}", [])
    assert set(google.result.ran) == {'play', 'play:crash', 'play:anr', 'play-reviews', 'play:access:vitals', 'play:access:reviews'}
    assert http.calls == []


def test_a_play_package_without_a_key_fails_with_the_fixed_key_text():
    # when
    out = app_stores.run_play_check(Ctx(dict(VALUES, credentials=''), play_http()))
    bad = app_stores.run_play_check(Ctx(dict(VALUES, **{'play-package': 'not a package'}), play_http()))

    # then
    assert (out.result.ok, out.result.status) == (False, f"error: {msg('google.bad_credentials')}")
    assert (bad.result.ok, bad.result.status) == (False, 'error: play-package must be a package name such as com.example.app')


def test_the_stores_tool_reads_rating_windows_vitals_by_version_and_reviews_without_text(store: Store):
    # given
    by_version = {'rows': [{'startTime': {'year': 2026, 'month': 10, 'day': 15}, 'dimensions': [{'dimension': 'versionCode', 'valueLabel': '1.2.5 (481)'}],
                            'metrics': [{'metric': 'userPerceivedCrashRate', 'decimalValue': {'value': '0.0021'}}, {'metric': 'distinctUsers', 'decimalValue': {'value': '30000'}}]}]}
    http = play_http({('POST', f'{REPORTING}/apps/{PKG}/crashRateMetricSet:query'): by_version})
    seeded(store, prior())
    b = stores_box(store, http)

    # when
    ratings = call(b)[0]
    vitals = call(b, op='vitals', by='version', days=7)[0]
    reviews = call(b, op='reviews')[0]
    bad = b.call('stores', {'op': 'vitals', 'days': 40})

    # then
    assert app_stores.PLUGIN.tools[0].scan and 'stores' in [t['name'] for t in b.definitions()]
    assert ratings['now']['count'] == 497 and [w['newRatings'] for w in ratings['windows']] == [77, 13] and ratings['store'] == 'App Store (US)'
    assert vitals['crash'] == [{'day': '2026-10-15', 'rate': '0.21 %', 'users': 30000, 'version': '1.2.5 (481)'}] and vitals['badBehaviour'] == {'crash': '1.09 %', 'anr': '0.47 %'}
    sent = http.hits(f'{REPORTING}/apps/{PKG}/crashRateMetricSet:query')[0][3]
    assert sent['dimensions'] == ['versionCode'] and sent['timelineSpec']['startTime']['day'] == 9
    assert reviews['count'] == 8 and reviews['byStars'] == {'1': 7, '5': 1} and set(reviews['reviews'][0]) == {'at', 'stars', 'version'}
    assert bad == ('days must be at most 28, got 40', True) and b.used['stores'] == 3


def test_the_stores_tool_says_which_half_is_off_and_which_countries_it_knows(store: Store):
    # given
    no_play = stores_box(store, play_http(), instances={'main': dict(VALUES, **{'play-package': ''})})
    no_apple = stores_box(store, play_http(), instances={'main': dict(VALUES, **{'apple-app-id': ''})})

    # when
    play_off = no_play.call('stores', {'op': 'reviews'})
    apple_off = no_apple.call('stores', {})
    other = no_play.call('stores', {'country': 'de'})

    # then
    assert play_off == ('the Google Play part is off: play-package is empty', True)
    assert apple_off == ('the App Store part is off: apple-app-id is empty', True)
    assert other == ('country must be one of us', True)


def test_a_refused_tool_call_names_the_missing_permission(store: Store):
    # given
    refused = HttpError(403, 'x', '{"error": {"message": "The caller does not have permission"}}')
    b = stores_box(store, play_http({('GET', f'{PUBLISHER}/applications/{PKG}/reviews'): refused, ('GET', f'{REPORTING}/apps/{PKG}/crashRateMetricSet'): refused}))

    # when
    reviews = b.call('stores', {'op': 'reviews'})
    vitals = b.call('stores', {'op': 'vitals'})

    # then
    assert reviews[1] and reviews[0].startswith(NO_REVIEWS) and 'View app information' not in reviews[0], 'no unverified permission is named for reviews'
    assert vitals[1] and vitals[0].startswith(NO_PLAY) and 'View app information' in vitals[0]


def test_one_vitals_metric_without_data_keeps_the_vitals_cards_open():
    # given
    hourly = {'freshnessInfo': {'freshnesses': [{'aggregationPeriod': 'HOURLY', 'latestEndTime': {'year': 2026, 'month': 10, 'day': 17}}]}}
    http = play_http({('GET', f'{REPORTING}/apps/{PKG}/anrRateMetricSet'): hourly})

    # when
    out = run_play(play(http), NOW, RULES)

    # then
    assert (out.result.ok, out.result.status) == (True, 'ok \u00b7 no data for Play ANR rate')
    assert set(out.result.ran) == {'play:crash', 'play-reviews', 'play:access:vitals', 'play:access:reviews'}, 'an open ANR card is never closed by a pass that did not read ANR'
    assert {c['key']: c['ranKey'] for c in out.result.found} == {'play-vitals|-/android-app|crash': 'play:crash', 'store-rating|-/android-app|reviews': 'play-reviews'}
    assert out.sections['play'][0]['vitals']['anr'] is None


def test_a_recovered_crash_card_closes_while_anr_data_is_missing(store: Store, tmp_path):
    # given
    hourly = {'freshnessInfo': {'freshnesses': [{'aggregationPeriod': 'HOURLY', 'latestEndTime': {'year': 2026, 'month': 10, 'day': 17}}]}}
    calm = {'rows': [day_row(8, 0.004, 'userPerceivedCrashRate'), day_row(15, 0.004, 'userPerceivedCrashRate')]}
    http = play_http()

    # when
    with app_with(store, tmp_path, http, instances={'main': dict(VALUES, **{'apple-app-id': ''})}) as c:
        c.app.state.sweeper.run('app-stores', 'play', NOW)
        before = by_key(store.load())
        http.routes.update({('POST', f'{REPORTING}/apps/{PKG}/crashRateMetricSet:query'): calm, ('GET', f'{REPORTING}/apps/{PKG}/anrRateMetricSet'): hourly})
        c.app.state.sweeper.run('app-stores', 'play', NOW)
        after = by_key(store.load())

    # then
    assert (before['play-vitals|-/android-app|crash']['status'], before['play-vitals|-/android-app|anr']['status']) == ('open', 'open')
    assert after['play-vitals|-/android-app|crash']['status'] != 'open', 'crash was read and is calm again'
    assert after['play-vitals|-/android-app|anr']['status'] == 'open', 'ANR was not read, so its card is held'
    assert store.load()['sources']['play'] == 'ok \u00b7 no data for Play ANR rate'


def test_the_app_runs_both_store_checks_once_an_instance_serves(store: Store, tmp_path):
    # given
    http = play_http()
    seeded(store, prior())

    # when
    with app_with(store, tmp_path, http) as c:
        jobs = {j.name: j for j in c.app.state.scheduler.jobs}
        tiles = {p['id']: p for p in c.get('/api/server').json()['plugins']}
        c.app.state.sweeper.run('app-stores', 'appstore', NOW)
        c.app.state.sweeper.run('app-stores', 'play', NOW)
    state = store.load()
    by = by_key(state)

    # then
    assert [(jobs[f'app-stores.{k}'].period, jobs[f'app-stores.{k}'].offset) for k in ('appstore', 'play')] == [(900, 690), (900, 735)]
    assert tiles['app-stores']['passes'] == ['appstore', 'play']
    assert (state['sources']['appstore'], state['sources']['play']) == ('ok', 'ok')
    assert by['store-rating|-/ios-app|ratings']['links'] == [{'label': 'App Store listing', 'url': f'https://apps.apple.com/us/app/id{APP_ID}'}]
    assert by['play-vitals|-/android-app|crash']['links'] == [{'label': 'Play Console', 'url': 'https://play.google.com/console'}]
    assert [(r['plugin'], r['country'], 'cluster' in r) for r in state['appStore']] == [('app-stores', 'us', False)]
    assert [(r['plugin'], r['package']) for r in state['play']] == [('app-stores', PKG)]


def test_app_stores_identity_and_manifest():
    # given
    registry = Registry.load({}, entry_points=False, core_tools=CORE_TOOLS, core_knowledge=prompt.core_anchors())

    # when
    found = registry.get('app-stores')
    manifest = describe(app_stores.PLUGIN, icon=True)
    fields = {f.key: f for f in app_stores.PLUGIN.fields}
    checks = {c.id: c for c in app_stores.PLUGIN.checks}
    tool, rules = app_stores.PLUGIN.tools[0], app_stores.PLUGIN.rules[0]

    # then
    assert 'skopax.plugins.app_stores' in BUILTIN and found is not None and found.state == 'ok' and found.plugin is app_stores.PLUGIN
    assert json.loads(json.dumps(manifest, allow_nan=False)) == manifest
    assert (manifest['id'], manifest['name'], manifest['version'], manifest['category'], manifest['author'], manifest['licence'], manifest['core'], manifest['edition']) == (
        'app-stores', 'App stores', '0.1.0', 'apps', 'Skopax', 'AGPL-3.0-only', '>=0.1,<0.8', 'community')
    assert manifest['capabilities'] == {'hosts': ['itunes.apple.com', 'playdeveloperreporting.googleapis.com', 'androidpublisher.googleapis.com', 'oauth2.googleapis.com'],
                                        'secrets': ['credentials'], 'agentReads': False}
    assert manifest['uses'] == []
    assert [(f.key, f.kind, f.required) for f in fields.values()] == [('apple-app-id', 'text', False), ('countries', 'text', False), ('play-package', 'text', False),
                                                                      ('credentials', 'secret', False)]
    assert [(c.id, c.period, c.offset, c.ran, c.sections, c.tool, c.once) for c in checks.values()] == [
        ('appstore', 900, 690, ('appstore',), ('appStore',), 'stores', False), ('play', 900, 735, ('play', 'play-reviews'), ('play',), 'stores', False)]
    assert all(c.section_help for c in checks.values())
    assert (rules.id, set(rules.help) == set(rules.defaults), dict(rules.defaults) == RULES) == ('rules', True, True)
    assert (tool.name, tool.kind, tool.per_run, tool.scan, tool.instance_input, tool.reach) == ('stores', 'read', 6, True, 'instance', ())
    assert registry.icon('app-stores').startswith('<svg')
    assert [k.id for k in app_stores.PLUGIN.knowledge] == ['app-stores'] and len(app_stores.PLUGIN.knowledge[0].text.splitlines()) <= 40
    assert app_stores.PLUGIN.client is None and app_stores.PLUGIN.panels == ()


def test_app_stores_defaults_are_neutral():
    # then
    assert {f.key: f.default for f in app_stores.PLUGIN.fields} == {'apple-app-id': '', 'countries': 'us', 'play-package': '', 'credentials': ''}
    assert {s.key: s.default for s in app_stores.PLUGIN.settings} == {}
    assert dict(app_stores.PLUGIN.rules[0].defaults) == {'rating_floor': 5, 'rating_warn': 3.0, 'rating_crit': 2.0, 'rating_crit_floor': 10, 'vitals_times': 2, 'low_reviews': 5}


def test_the_app_stores_test_skips_a_store_whose_check_is_switched_off(store):
    # given
    def tested(http, off: set[str]):
        return tool_runtime(store, http, instances={}).connection_test(app_stores.PLUGIN, None, dict(VALUES), 5, NOW, off_checks=off)

    play_refused = play_http({('GET', f'{PUBLISHER}/applications/{PKG}/reviews'): HttpError(403, 'x')})
    apple_refused = play_http({('GET', LOOKUP_URL): HttpError(403, LOOKUP_URL)})

    # when
    no_play, no_apple, neither = tested(play_refused, {'play'}), tested(apple_refused, {'appstore'}), tested(play_http(), {'appstore', 'play'})
    apple_only = tool_runtime(store, play_http(), instances={}).connection_test(app_stores.PLUGIN, None, dict(VALUES, **{'play-package': '', 'credentials': ''}), 5, NOW,
                                                                               off_checks={'appstore'})

    # then
    assert (no_play['ok'], no_play['summary']) == (True, f'App Store{DOT}Shop 1.2.6 (US)') and play_refused.hits(f'{PUBLISHER}/applications/{PKG}/reviews') == []
    assert (no_apple['ok'], no_apple['summary']) == (True, f'Google Play{DOT}{PKG}') and apple_refused.hits(LOOKUP_URL) == []
    assert (neither['ok'], neither['summary']) == (True, app_stores.ALL_OFF) and (apple_only['ok'], apple_only['summary']) == (True, app_stores.ALL_OFF)


def test_app_stores_connection_test_reports_ok_and_failures(store):
    # given
    unauthorised = HttpError(401, 'x', '{"error": {"message": "Request had invalid authentication credentials."}}')
    cases = {'ok': (play_http(), VALUES), 'apple only': (play_http(), dict(VALUES, **{'play-package': '', 'credentials': ''})),
             'refused': (play_http({('GET', f'{REPORTING}/apps/{PKG}/crashRateMetricSet'): unauthorised}), VALUES),
             'reviews refused': (play_http({('GET', f'{PUBLISHER}/applications/{PKG}/reviews'): HttpError(403, 'x')}), VALUES),
             'apple forbidden': (play_http({('GET', LOOKUP_URL): HttpError(403, LOOKUP_URL)}), dict(VALUES, **{'play-package': '', 'credentials': ''})),
             'key refused': (Google({('GET', LOOKUP_URL): answer(497, 4.49)}, token=HttpError(401, 'x', '{"error": "invalid_client"}')), VALUES),
             'apple unreachable': (play_http({('GET', LOOKUP_URL): Unreachable('https://itunes.apple.com/lookup unreachable (timed out)')}), VALUES),
             'google unreachable': (Google({('GET', LOOKUP_URL): answer(497, 4.49)}, token=Unreachable('down')), VALUES),
             'broken key': (play_http(), dict(VALUES, credentials=BROKEN_KEY)), 'nothing': (play_http(), {'countries': 'us'})}

    # when
    got = {}
    for name, (http, values) in cases.items():
        got[name] = tool_runtime(store, http, instances={}).connection_test(app_stores.PLUGIN, None, dict(values), 5, NOW)

    # then
    assert (got['ok']['ok'], got['ok']['summary']) == (True, f'App Store{DOT}Shop 1.2.6 (US){DOT}Google Play{DOT}{PKG}')
    assert (got['apple only']['ok'], got['apple only']['summary']) == (True, f'App Store{DOT}Shop 1.2.6 (US)')
    assert (got['refused']['ok'], got['refused']['summary']) == (
        False, f'Play vitals: the service account is invalid or lacks {PLAY_NEEDS} (HTTP 401: Request had invalid authentication credentials.)')
    assert (got['reviews refused']['ok'], got['reviews refused']['summary']) == (False, f'Play reviews: {NO_REVIEWS} (HTTP 403)')
    assert (got['apple forbidden']['ok'], got['apple forbidden']['summary']) == (False, 'App Store lookup HTTP 403'), 'the App Store lookup takes no credentials'
    assert (got['key refused']['ok'], got['key refused']['summary']) == (False, msg('connection.rejected', status=401))
    assert (got['apple unreachable']['summary'], got['google unreachable']['summary']) == (msg('connection.unreachable'), msg('connection.unreachable'))
    assert (got['broken key']['ok'], got['broken key']['summary']) == (False, msg('google.bad_credentials'))
    assert (got['nothing']['ok'], got['nothing']['summary']) == (False, app_stores.NOTHING_SET_UP)
    assert len(cases['ok'][0].hits(f'{PUBLISHER}/applications/{PKG}/reviews')) == 1 and 'maxResults=1' in cases['ok'][0].hits(f'{PUBLISHER}/applications/{PKG}/reviews')[0][1]


def test_app_stores_second_instance_is_scoped(store: Store, tmp_path):
    # given
    http = FakeHttp({('GET', LOOKUP_URL): lambda q, b: answer(497, 4.57) if q['id'] == [APP_ID] else answer(497, 4.4930)})
    seeded(store, prior(), dict(prior(app_id=EAST_ID), cluster='east'))
    east = {'apple-app-id': EAST_ID, 'countries': 'us'}

    # when
    with app_with(store, tmp_path, http, instances={'main': dict(VALUES, **{'play-package': '', 'credentials': ''}), 'east': east}) as c:
        c.app.state.sweeper.run('app-stores', 'appstore', NOW)
    state = store.load()
    [card] = state['anomalies']

    # then
    assert (card['cluster'], card['ranKey'], card['appId']) == ('east', 'appstore-east', EAST_ID)
    assert state['sources']['appstore-east'] == 'ok' and state['sources']['appstore'] == 'ok'
    assert sorted((r.get('cluster') or '', r['appId']) for r in state['appStore']) == [('', APP_ID), ('east', EAST_ID)]


def test_app_stores_rules_come_from_the_rule_set(store: Store, tmp_path):
    # given
    seeded(store, prior())

    # when
    with app_with(store, tmp_path, lookup(497, 4.4930), instances={'main': dict(VALUES, **{'play-package': '', 'credentials': ''})}) as c:
        c.app.state.sweeper.run('app-stores', 'appstore', NOW)
        before = by_key(store.load())
        c.app.state.runtime.live.set('app-stores.rules', '{"rating_warn": 2.0}', ALICE)
        c.app.state.sweeper.run('app-stores', 'appstore', NOW + 900)
        after = by_key(store.load())

    # then
    assert before['store-rating|-/ios-app|ratings']['status'] == 'open'
    assert after['store-rating|-/ios-app|ratings']['status'] != 'open'
