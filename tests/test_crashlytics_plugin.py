import json
import logging
from urllib.parse import parse_qs, quote, urlsplit

import jwt
import pytest
from fastapi.testclient import TestClient

from skopax import toolusage
from skopax.app import create_app
from skopax.contract import InstanceInfo
from skopax.engine import iso
from skopax.instances import Instances, PluginState
from skopax.investigator import prompt
from skopax.investigator.tools import CORE_TOOLS
from skopax.investigator.toolset import ToolSet
from skopax.kit.google import SCOPES, TOKEN_URL, GoogleAuth
from skopax.kube import Kube
from skopax.messages import msg
from skopax.plugins import app_stores, crashlytics, ga4, google_ads, gtm
from skopax.plugins.crashlytics import engine
from skopax.plugins.crashlytics.engine import CRASHLYTICS_API, NO_ACCESS, NO_APPS, Crashlytics
from skopax.plugintools import PluginTools
from skopax.registry import BUILTIN, Registry, describe
from skopax.remote import HttpError, Unreachable
from skopax.runtime import Runtime
from skopax.sealer import Sealer, master_key
from skopax.store import Store
from tests.conftest import AUTH, settings
from tests.fake_google import ACCESS, BROKEN_KEY, KEY, PLANTED, Google
from tests.plugin_kit import add_instance
from tests.test_investigator_tools import NOW, box

ANDROID = '1:123456789012:android:a1b2c3d4e5f60718'
IOS = '1:123456789012:ios:0f1e2d3c4b5a6978'
EAST_ANDROID = '1:210987654321:android:b1b2c3d4e5f60718'
VALUES = {'credentials': KEY, 'android-app-id': ANDROID, 'ios-app-id': IOS}
SEAL = 'crash-key'
ALICE = 'alice@example.com'


def path(app_id: str, report: str) -> str:
    return f'{CRASHLYTICS_API}/projects/{app_id.split(":")[1]}/apps/{quote(app_id, safe="")}/reports/{report}'


def issues(query, body) -> dict:
    late = query.get('filter.interval.end_time') == [iso(NOW)]
    return {'groups': [{'issue': {'id': 'a1b2c3d4e5f6a7b8', 'title': 'com.example.shop.SignInViewModel.verify', 'subtitle': 'NullPointerException for alice@example.com',
                                  'errorType': 'FATAL', 'firstSeenVersion': '4.12.0', 'lastSeenVersion': '4.12.0', 'uri': 'https://console.firebase.google.com/x'},
                        'metrics': [{'eventsCount': 120 if late else 4, 'impactedUsersCount': 90 if late else 3}]},
                       {'issue': {'id': 'ffff0000eeee1111', 'title': 'okhttp3.internal.Timeout', 'errorType': 'NON_FATAL'},
                        'metrics': [{'eventsCount': '7', 'impactedUsersCount': '5'}]}], 'totalSize': 2}


def versions(query, body) -> dict:
    return {'groups': [{'version': {'displayName': '4.12.0 (4120)'}, 'metrics': [{'eventsCount': 130, 'impactedUsersCount': 95}]}], 'totalSize': 1}


def google(over: dict | None = None) -> Google:
    return Google({('GET', path(ANDROID, 'topIssues')): issues, ('GET', path(ANDROID, 'topVersions')): versions, ('GET', path(IOS, 'topIssues')): {'groups': []},
                   ('GET', path(IOS, 'topAppleDevices')): {'groups': []}, **(over or {})})


def tool_runtime(store, http, instances: dict | None = None) -> Runtime:
    reg = Registry.of(crashlytics.PLUGIN)
    runtime = Runtime(reg, PluginState(store.db), Instances(store.db, reg, lambda: Sealer(SEAL)), http)
    for i, (name, values) in enumerate(({'main': VALUES} if instances is None else instances).items()):
        add_instance(store, crashlytics.PLUGIN, name, sealer_key=SEAL, primary=i == 0, **values)
    runtime.refresh()
    return runtime


def crash_box(store: Store, http, instances: dict | None = None, **kw):
    return box(store, plugin_tools=PluginTools(tool_runtime(store, http, instances)), **kw)


def call(b, **args):
    text, err = b.call('crashes', args)
    return (text if err else json.loads(text)), err


def query_of(http: Google, url: str, i: int = 0) -> dict:
    return parse_qs(urlsplit(http.hits(url)[i][1]).query)


def app_with(store, tmp_path, http, instances: dict | None = None):
    sealing = master_key(store.db, settings())
    for i, (name, values) in enumerate(({'main': VALUES} if instances is None else instances).items()):
        add_instance(store, crashlytics.PLUGIN, name, sealer_key=sealing, primary=i == 0, **values)
    return TestClient(create_app(settings(), store, registry=Registry.of(crashlytics.PLUGIN), transport=http, kube=Kube(root=tmp_path)))


def by_key(state: dict) -> dict:
    return {a['key']: a for a in state['anomalies']}


def issue(issue_id: str, kind: str, events: int, users: int, title: str = 'Boom') -> dict:
    return {'issue': {'id': issue_id, 'title': title, 'subtitle': 'x for alice@example.com', 'errorType': kind, 'firstSeenVersion': '1.2.5',
                      'uri': f'https://console.firebase.google.com/{issue_id}'}, 'metrics': [{'eventsCount': events, 'impactedUsersCount': users}]}


def crash_windows(app_id: str, groups: dict) -> dict:
    def answer(query, body):
        return {'groups': groups[query['filter.interval.end_time'][0]]}
    return {('GET', path(app_id, 'topIssues')): answer}


def spike() -> dict:
    t0, t1, t7 = iso(NOW), iso(NOW - 86400), iso(NOW - 7 * 86400)
    return {t0: [issue('aaaaaaaa01', 'FATAL', 30, 25, 'SignInViewModel.verify'), issue('bbbbbbbb02', 'ANR', 12, 6), issue('cccccccc03', 'FATAL', 4, 4, 'Brand new')],
            t1: [issue('aaaaaaaa01', 'FATAL', 2, 2), issue('bbbbbbbb02', 'ANR', 10, 5)], t7: [issue('aaaaaaaa01', 'FATAL', 1, 1), issue('bbbbbbbb02', 'ANR', 9, 5)]}


def quiet() -> dict:
    return {iso(NOW): [], iso(NOW - 86400): [], iso(NOW - 7 * 86400): []}


def test_crashes_is_a_read_source_tool_offered_once_an_instance_serves(store: Store):
    # when
    none = [t['name'] for t in crash_box(store, google(), instances={}).definitions()]
    offered = [t['name'] for t in crash_box(store, google()).definitions()]

    # then
    assert crashlytics.PLUGIN.tools[0].scan and 'crashes' in offered
    assert 'crashes' not in none
    assert Crashlytics(GoogleAuth(KEY, google(), lambda: NOW, SCOPES), android=IOS).apps == {}
    assert Crashlytics(GoogleAuth(KEY, google(), lambda: NOW, SCOPES), ios='1:1:ios:zz').apps == {}


def test_without_a_valid_app_id_the_tool_and_the_pass_say_how_to_set_one(store: Store):
    # given
    b = crash_box(store, google(), instances={'main': {'credentials': KEY, 'ios-app-id': IOS}})

    # when
    answer = call(b, app='android')
    swept = crashlytics.run(Ctx({'credentials': KEY}, google()))

    # then
    assert answer == ('app must be one of ios: the android Firebase app id is not set', True)
    assert (swept.result.ok, swept.result.status) == (False, f'error: {NO_APPS}')


@pytest.mark.parametrize('field, value', [('android-app-id', 'nope'), ('android-app-id', 'com.example.shop'), ('android-app-id', IOS), ('ios-app-id', ANDROID)])
def test_a_malformed_or_mismatched_app_id_fails_the_pass_the_test_and_the_tool_naming_its_field(store: Store, field, value):
    # given
    values = dict(VALUES, **{field: value})
    app = field.split('-')[0]
    text = f"{field} must be a Firebase app id such as 1:<project number>:{app}:<hex>"
    http = google()

    other = 'ios' if app == 'android' else 'android'
    other_id = values[f'{other}-app-id']
    alone = {'credentials': KEY, field: value}

    # when
    swept = crashlytics.run(Ctx(values, http))
    only = crashlytics.run(Ctx(alone, google()))
    tested = tool_runtime(store, google(), instances={}).connection_test(crashlytics.PLUGIN, None, dict(values), 5, NOW)
    answer = call(crash_box(store, google(), instances={'main': values}), app=app)

    # then
    assert (swept.result.ok, swept.result.status) == (True, f'error: {text}'), 'P5-C129: the other app is still read'
    assert set(swept.result.ran) == {f'crashlytics:{other}', f'crashlytics:access:{other}'}, 'its keys stay out of ran, so its cards stay open'
    assert {c[1].split('?')[0] for c in http.calls} == {TOKEN_URL, path(other_id, 'topIssues')}
    assert (only.result.ok, only.result.status) == (False, f'error: {text}'), 'every configured app failed'
    assert (tested['ok'], tested['summary']) == (False, text)
    assert answer == (text, True)


def test_top_issues_compare_the_same_window_a_week_earlier(store: Store):
    # given
    http = google()
    b = crash_box(store, http)

    # when
    out, err = call(b, app='android', hours=2, compare='week', types=['FATAL', 'ANR'], limit=5)

    # then
    assert not err
    now_q, before_q = query_of(http, path(ANDROID, 'topIssues')), query_of(http, path(ANDROID, 'topIssues'), 1)
    assert now_q == {'page_size': ['5'], 'filter.issue.error_types': ['FATAL', 'ANR'],
                     'filter.interval.start_time': [iso(NOW - 7200)], 'filter.interval.end_time': [iso(NOW)]}
    assert (before_q['filter.interval.start_time'], before_q['filter.interval.end_time']) == ([iso(NOW - 7200 - 7 * 86400)], [iso(NOW - 7 * 86400)])
    assert http.hits(path(ANDROID, 'topIssues'))[0][2]['Authorization'] == f'Bearer {ACCESS}' and len(http.forms) == 1
    first = out['rows'][0]
    assert (first['issue'], first['type'], first['events'], first['users'], first['week before']) == ('a1b2c3d4e5f6a7b8', 'FATAL', 120, 90, {'events': 4, 'users': 3})
    assert out['rows'][1]['events'] == 7 and 'subtitle' in first and 'alice@example.com' not in json.dumps(out)
    assert out['window'] == {'from': iso(NOW - 7200), 'to': iso(NOW)} and out['compare']['name'] == 'week before' and out['total'] == 2


def test_versions_devices_and_filters_pick_the_right_report(store: Store):
    # given
    http = google()
    b = crash_box(store, http)

    # when
    listed = call(b, app='android', report='versions')[0]
    call(b, app='android', version='4.12.0 (4120)', issue='a1b2c3d4e5f6a7b8')
    devices = call(b, app='ios', report='devices')

    # then
    assert listed['rows'] == [{'version': '4.12.0 (4120)', 'events': 130, 'users': 95}]
    q = query_of(http, path(ANDROID, 'topIssues'))
    assert q['filter.version.display_names'] == ['4.12.0 (4120)'] and q['filter.issue.id'] == ['a1b2c3d4e5f6a7b8'] and q['page_size'] == ['10']
    assert devices == ({'app': 'ios', 'report': 'devices', 'window': {'from': iso(NOW - 3600), 'to': iso(NOW)}, 'rows': [], 'total': 0}, False)


@pytest.mark.parametrize('args, message, used', [
    ({'app': 'web'}, 'app must be one of android, ios', 0),
    ({'app': 'android', 'hours': 200}, 'hours must be at most 168', 0),
    ({'app': 'android', 'version': '4.12.0'}, 'version must be a Crashlytics version display name', 1),
    ({'app': 'android', 'issue': 'not-an-id'}, 'issue must be a Crashlytics issue id', 1),
    ({'app': 'android', 'types': ['CRASH']}, 'must be one of FATAL, NON_FATAL, ANR', 0),
    ({'app': 'android', 'compare': 'month'}, 'compare must be one of day, week', 0),
])
def test_bad_input_is_refused_before_any_google_call(store: Store, args, message, used):
    # given
    http = google()
    b = crash_box(store, http)

    # when
    text, err = b.call('crashes', args)

    # then
    assert err and message in text
    assert http.calls == [] and b.used['crashes'] == used, 'a refusal inside the tool costs one call of the run (P5-C81), a schema refusal none'


def test_a_missing_app_id_refusals_and_the_run_cap(store: Store):
    # given
    android_only = crash_box(store, google(), instances={'main': {'credentials': KEY, 'android-app-id': ANDROID}})
    denied = crash_box(store, google({('GET', path(ANDROID, 'topIssues')): HttpError(403, path(ANDROID, 'topIssues'))}))
    broken = crash_box(store, google({('GET', path(ANDROID, 'topIssues')): HttpError(500, path(ANDROID, 'topIssues'), f'{{"error": {{"message": "boom {ACCESS}"}}}}')}), run_id='run-1')
    b = crash_box(store, google())

    # when
    answers = [call(b, app='android')[1] for _ in range(crashlytics.PER_RUN + 1)]
    failed = call(broken, app='android')
    seen = json.dumps([failed, broken.calls, list(store.activity.find({}, {'_id': 0}))], default=str)

    # then
    assert call(android_only, app='ios') == ('app must be one of android: the ios Firebase app id is not set', True)
    assert call(denied, app='android') == (f'{NO_ACCESS} (HTTP 403)', True)
    assert failed == ('Crashlytics HTTP 500: boom ***', True)
    assert ACCESS not in seen and 'PRIVATE KEY' not in seen
    assert answers == [False] * crashlytics.PER_RUN + [True]


def test_an_earlier_count_past_the_shown_rows_stays_a_number_and_is_null_only_when_that_list_was_cut(store: Store):
    # given
    def group(i: int, n: int) -> dict:
        return {'issue': {'id': f'{i:016x}', 'title': f'crash {i}', 'errorType': 'FATAL'}, 'metrics': [{'eventsCount': n, 'impactedUsersCount': n // 2}]}

    def ranked(earlier: list[dict]):
        def answer(query, body):
            late = query.get('filter.interval.end_time') == [iso(NOW)]
            groups = [group(7, 30), group(8, 4)] if late else earlier
            return {'groups': groups[:int(query['page_size'][0])], 'totalSize': len(groups)}
        return answer

    known_http = google({('GET', path(ANDROID, 'topIssues')): ranked([group(i, 100 - i) for i in range(1, 4)] + [group(7, 20)])})
    cut_http = google({('GET', path(ANDROID, 'topIssues')): ranked([group(i, 500 - i) for i in range(100, 160)])})

    # when
    known, err = call(crash_box(store, known_http), app='android', compare='week', limit=1)
    unknown = call(crash_box(store, cut_http), app='android', compare='week', limit=2)[0]

    # then
    assert not err and [(r['issue'], r['week before']) for r in known['rows']] == [('0000000000000007', {'events': 20, 'users': 10})] and 'note' not in known
    assert query_of(known_http, path(ANDROID, 'topIssues'), 1)['page_size'] == ['50']
    assert [r['week before'] for r in unknown['rows']] == [None, None] and 'outside the top 50' in unknown['note']


def test_crashlytics_flags_a_crash_spike_with_users_and_a_new_issue_but_not_known_anrs():
    # given
    http = Google({**crash_windows(ANDROID, spike()), **crash_windows(IOS, quiet())})

    # when
    found, ran, notes, errors = engine.crash_pass(Crashlytics(GoogleAuth(KEY, http, lambda: NOW, SCOPES), ANDROID, IOS), NOW, engine.RULES)

    # then
    assert set(ran) == {'crashlytics', 'crashlytics:android', 'crashlytics:access:android', 'crashlytics:ios', 'crashlytics:access:ios'} and notes == [] and errors == []
    cards = {c['key']: c for c in found}
    rate = cards['crash-rate|-/android-app|FATAL']
    assert (rate['sev'], rate['now'], rate['d1'], rate['d7'], rate['ranKey']) == ('crit', 34, 2, 1, 'crashlytics:android')
    assert 'SignInViewModel.verify (30 events, 25 users' in rate['what'] and 'alice' not in rate['what']
    assert 'in the hour to 17 Oct 08:00 UTC' in rate['what']
    assert cards['crash-new|-/android-app|cccccccc03']['label'] == 'new crash'
    assert 'crash-rate|-/android-app|ANR' not in cards and not [k for k in cards if 'ios-app' in k]
    assert engine.links_for(rate) == [{'label': 'Top Crashlytics issue', 'url': 'https://console.firebase.google.com/aaaaaaaa01'}]
    assert crashlytics.links({'src': 'prom', 'sig': 'err'}, InstanceInfo('main', True, None, VALUES)) is None


def test_the_app_runs_the_crashlytics_check_once_an_instance_serves(store: Store, tmp_path):
    # given
    http = Google({**crash_windows(ANDROID, spike()), **crash_windows(IOS, quiet())})

    # when
    with app_with(store, tmp_path, http) as c:
        jobs = {j.name: j for j in c.app.state.scheduler.jobs}
        tiles = {p['id']: p for p in c.get('/api/server').json()['plugins']}
        c.app.state.sweeper.run('crashlytics', 'crashlytics', NOW)
    state = store.load()
    by = by_key(state)

    # then
    assert (jobs['crashlytics.crashlytics'].period, jobs['crashlytics.crashlytics'].offset) == (900, 480)
    assert tiles['crashlytics']['passes'] == ['crashlytics']
    assert state['sources']['crashlytics'] == 'ok'
    assert (by['crash-rate|-/android-app|FATAL']['sev'], by['crash-rate|-/android-app|FATAL']['ranKey']) == ('crit', 'crashlytics:android')
    assert by['crash-rate|-/android-app|FATAL']['links'][0] == {'label': 'Top Crashlytics issue', 'url': 'https://console.firebase.google.com/aaaaaaaa01'}
    scope = jwt.decode(http.forms[0]['assertion'], options={'verify_signature': False})['scope']
    assert scope == 'https://www.googleapis.com/auth/cloud-platform', 'its own scope only, no Analytics or Tag Manager'


def test_the_crashes_tool_status_is_off_without_an_instance_and_on_with_one(store: Store):
    # given
    none = tool_runtime(store, google(), instances={})

    # when
    off = ToolSet(none).status('crashes', None)
    one = ToolSet(tool_runtime(store, google())).status('crashes', None)

    # then
    assert (off['state'], one) == ('off', {'state': 'on', 'reason': None})


def test_the_crashes_tool_row_carries_what_the_investigator_is_told(store: Store):
    # given
    runs = [{'_id': 'r1', 'startedAt': '2026-10-28T10:00:00Z', 'trigger': 'cards', 'toolCalls': [{'name': 'crashes', 'ms': 900, 'error': None, 'truncated': False}]}]

    # when
    tools = {t['name']: t for t in toolusage.summary(runs, 1, '2026-10-22T00:00:00Z', None, PluginTools(tool_runtime(store, google())))['tools']}

    # then
    assert 'An empty answer with no error means no issues in that window' in tools['crashes']['description']


def test_a_crashlytics_pass_stops_at_its_time_budget(store: Store, tmp_path, monkeypatch):
    # given
    monkeypatch.setattr(crashlytics, 'BUDGET_S', 0)
    http = Google({**crash_windows(ANDROID, spike()), **crash_windows(IOS, quiet())})

    # when
    swept = crashlytics.run(Ctx(VALUES, http))

    # then
    assert (swept.result.ok, swept.result.status) == (False, 'error: Google reads stopped at their 0 s budget')
    assert http.calls == []


def test_a_failed_android_app_is_its_own_error_and_ios_is_still_read():
    # given
    broken = HttpError(500, path(ANDROID, 'topIssues'), '{"error": {"message": "Backend failure for project x."}}')
    http = Google({('GET', path(ANDROID, 'topIssues')): broken, **crash_windows(IOS, spike())})

    # when
    swept = crashlytics.run(Ctx(VALUES, http))

    # then
    assert (swept.result.ok, swept.result.status) == (True, 'error: the android app: Crashlytics HTTP 500'), 'P5-C129: the failure shows and names its part'
    assert set(swept.result.ran) == {'crashlytics:ios', 'crashlytics:access:ios'}, 'the android cards are held and the bare key is not listed'
    assert 'crash-rate|-/ios-app|FATAL' in {c['key'] for c in swept.result.found}
    assert 'Backend failure' not in swept.result.status


def test_a_failed_ios_app_after_android_was_read_keeps_androids_findings_and_keys():
    # given
    stale = HttpError(404, path(IOS, 'topIssues'), '{"error": {"message": "App not found."}}')
    http = Google({**crash_windows(ANDROID, spike()), ('GET', path(IOS, 'topIssues')): stale})

    # when
    swept = crashlytics.run(Ctx(VALUES, http))

    # then
    assert (swept.result.ok, swept.result.status) == (True, 'error: the ios app: Crashlytics HTTP 404')
    assert set(swept.result.ran) == {'crashlytics:android', 'crashlytics:access:android'}
    assert 'crash-rate|-/android-app|FATAL' in {c['key'] for c in swept.result.found}


def test_when_every_app_fails_the_pass_fails_and_names_each():
    # given
    http = Google({('GET', path(ANDROID, 'topIssues')): HttpError(500, 'x'), ('GET', path(IOS, 'topIssues')): HttpError(404, 'x')})

    # when
    swept = crashlytics.run(Ctx(VALUES, http))

    # then
    assert (swept.result.ok, swept.result.status) == (False, 'error: the android app: Crashlytics HTTP 500; the ios app: Crashlytics HTTP 404')


def test_a_failed_app_next_to_a_refused_app_keeps_the_access_card():
    # given
    http = Google({('GET', path(ANDROID, 'topIssues')): HttpError(403, 'x'), ('GET', path(IOS, 'topIssues')): HttpError(503, 'x')})

    # when
    swept = crashlytics.run(Ctx(VALUES, http))

    # then
    assert (swept.result.ok, swept.result.status) == (True, 'error: the ios app: Crashlytics HTTP 503 \u00b7 no access to the android app')
    assert set(swept.result.ran) == {'crashlytics:access:android'}
    assert [c['ranKey'] for c in swept.result.found] == ['crashlytics:access:android']


def test_a_refused_app_is_never_read_as_no_crashes_and_gets_a_warn_card_naming_the_role():
    # given
    denied = HttpError(403, path(ANDROID, 'topIssues'), '{"error": {"code": 403, "message": "The caller does not have permission", "status": "PERMISSION_DENIED"}}')
    http = Google({('GET', path(ANDROID, 'topIssues')): denied, **crash_windows(IOS, spike())})

    # when
    swept = crashlytics.run(Ctx(VALUES, http))

    # then
    result = swept.result
    assert (result.ok, result.status) == (True, 'ok \u00b7 no access to the android app')
    assert set(result.ran) == {'crashlytics:ios', 'crashlytics:access:android', 'crashlytics:access:ios'}, 'the android cards stay open and the old check-wide key is not cleared'
    cards = {c['key']: c for c in result.found}
    card = cards['no-access|-/crashlytics|android']
    assert (card['sev'], card['ranKey'], card['src']) == ('warn', 'crashlytics:access:android', 'crashlytics')
    assert card['label'] == f'Crashlytics cannot read the android app: the service account lacks {engine.NEEDS}'
    assert 'roles/firebasecrashlytics.viewer' in card['label'] and NO_ACCESS in card['what']
    assert cards['crash-rate|-/ios-app|FATAL']['ranKey'] == 'crashlytics:ios'


def test_the_access_card_clears_at_the_first_pass_that_reads_the_app_again(store: Store, tmp_path):
    # given
    answers = {'refuse': True}
    windows = crash_windows(ANDROID, quiet())[('GET', path(ANDROID, 'topIssues'))]

    def android(query, body):
        return HttpError(403, path(ANDROID, 'topIssues')) if answers['refuse'] else windows(query, body)

    http = Google({('GET', path(ANDROID, 'topIssues')): android, **crash_windows(IOS, quiet())})

    # when
    with app_with(store, tmp_path, http) as c:
        c.app.state.sweeper.run('crashlytics', 'crashlytics', NOW)
        refused = by_key(store.load())
        answers['refuse'] = False
        c.app.state.sweeper.run('crashlytics', 'crashlytics', NOW)
        read = by_key(store.load())

    # then
    assert refused['no-access|-/crashlytics|android']['status'] == 'open'
    assert read['no-access|-/crashlytics|android']['status'] != 'open'
    assert store.load()['sources']['crashlytics'] == 'ok'


def test_a_refused_key_opens_an_access_card_per_app_and_holds_the_crash_cards(store: Store, tmp_path):
    # given
    http = Google({**crash_windows(ANDROID, spike()), **crash_windows(IOS, quiet())})

    # when
    with app_with(store, tmp_path, http) as c:
        c.app.state.sweeper.run('crashlytics', 'crashlytics', NOW)
        http.token = HttpError(400, TOKEN_URL, '{"error": "invalid_grant"}')
        c.app.state.sweeper.run('crashlytics', 'crashlytics', NOW)
        refused = store.load()
        http.token = {'access_token': ACCESS, 'expires_in': 3599}
        http.routes.update(crash_windows(ANDROID, quiet()))
        c.app.state.sweeper.run('crashlytics', 'crashlytics', NOW)
        read = by_key(store.load())

    # then
    cards = by_key(refused)
    assert refused['sources']['crashlytics'] == 'ok \u00b7 no access to the android app, the ios app', 'P5-C127a: a refused key is a refusal, not a failed pass'
    assert cards['crash-rate|-/android-app|FATAL']['status'] == 'open', 'the crash card is held'
    for app in ('android', 'ios'):
        assert cards[f'no-access|-/crashlytics|{app}']['status'] == 'open'
        assert cards[f'no-access|-/crashlytics|{app}']['label'] == f'Crashlytics cannot read the {app} app: the service account is invalid or lacks {engine.NEEDS}'
        assert read[f'no-access|-/crashlytics|{app}']['status'] != 'open', 'the access card clears at the first pass that reads the app'
    assert read['crash-rate|-/android-app|FATAL']['status'] != 'open'


def test_a_refused_key_in_the_crashes_tool_names_both_causes(store: Store):
    # given
    b = crash_box(store, Google(token=HttpError(400, TOKEN_URL, '{"error": "invalid_grant"}')))

    # when
    answer = call(b, app='android')

    # then
    assert answer == (f'the service account is invalid or lacks {engine.NEEDS}: Google refused the service account key (HTTP 400 invalid_grant)', True)


def test_a_removed_app_id_closes_that_apps_cards(store: Store, tmp_path):
    # given
    http = Google({**crash_windows(ANDROID, spike()), **crash_windows(IOS, quiet())})

    # when
    with app_with(store, tmp_path, http) as c:
        c.app.state.sweeper.run('crashlytics', 'crashlytics', NOW)
        before = by_key(store.load())
        c.patch('/api/admin/plugins/crashlytics/instances/main', json={'values': {'android-app-id': ''}}, headers=AUTH)
        c.app.state.sweeper.run('crashlytics', 'crashlytics', NOW)
        after = by_key(store.load())

    # then
    assert before['crash-rate|-/android-app|FATAL']['status'] == 'open'
    assert after['crash-rate|-/android-app|FATAL']['status'] != 'open'


def test_crashlytics_identity_and_manifest():
    # given
    registry = Registry.load({}, entry_points=False, core_tools=CORE_TOOLS, core_knowledge=prompt.core_anchors())

    # when
    found = registry.get('crashlytics')
    manifest = describe(crashlytics.PLUGIN, icon=True)
    fields = {f.key: f for f in crashlytics.PLUGIN.fields}
    check, tool, rules = crashlytics.PLUGIN.checks[0], crashlytics.PLUGIN.tools[0], crashlytics.PLUGIN.rules[0]

    # then
    assert 'skopax.plugins.crashlytics' in BUILTIN and found is not None and found.state == 'ok' and found.plugin is crashlytics.PLUGIN
    assert json.loads(json.dumps(manifest, allow_nan=False)) == manifest
    assert (manifest['id'], manifest['name'], manifest['version'], manifest['category'], manifest['author'], manifest['licence'], manifest['core'], manifest['edition']) == (
        'crashlytics', 'Crashlytics', '0.1.0', 'apps', 'Skopax', 'AGPL-3.0-only', '>=0.1,<0.8', 'community')
    assert manifest['capabilities'] == {'hosts': ['firebasecrashlytics.googleapis.com', 'oauth2.googleapis.com'], 'secrets': ['credentials'], 'agentReads': False}
    assert manifest['uses'] == []
    assert [(f.key, f.kind, f.required) for f in fields.values()] == [('project', 'text', False), ('android-app-id', 'text', False), ('ios-app-id', 'text', False),
                                                                      ('credentials', 'secret', True)]
    assert (check.id, check.period, check.offset, check.ran, check.sections, check.tool, check.once) == ('crashlytics', 900, 480, ('crashlytics',), (), 'crashes', False)
    assert (rules.id, set(rules.help) == set(rules.defaults), dict(rules.defaults) == engine.RULES) == ('rules', True, True)
    assert (tool.name, tool.kind, tool.per_run, tool.scan, tool.instance_input, tool.reach) == ('crashes', 'read', 6, True, 'instance', ())
    assert registry.icon('crashlytics').startswith('<svg')
    assert [k.id for k in crashlytics.PLUGIN.knowledge] == ['crashlytics'] and len(crashlytics.PLUGIN.knowledge[0].text.splitlines()) <= 40
    assert crashlytics.PLUGIN.client is None and crashlytics.PLUGIN.panels == ()


def test_crashlytics_defaults_are_neutral():
    # then
    assert {f.key: f.default for f in crashlytics.PLUGIN.fields} == {'project': '', 'android-app-id': '', 'ios-app-id': '', 'credentials': ''}
    assert {s.key: s.default for s in crashlytics.PLUGIN.settings} == {}
    assert dict(crashlytics.PLUGIN.rules[0].defaults) == {'crash_floor': 5, 'crash_times': 2, 'crash_gap': 3, 'crash_crit_users': 20, 'anr_floor': 10, 'anr_times': 2,
                                                          'anr_gap': 5, 'new_users': 3}


def test_crashlytics_connection_test_reports_ok_and_failures(store):
    # given
    refused = HttpError(401, path(ANDROID, 'topIssues'), '{"error": {"message": "Request had invalid authentication credentials.", "status": "UNAUTHENTICATED"}}')
    forbidden = HttpError(403, path(IOS, 'topIssues'), '{"error": {"message": "The caller does not have permission", "status": "PERMISSION_DENIED"}}')
    cases = {'ok': (google(), VALUES), 'named': (google(), dict(VALUES, project='shop-app')),
             'refused': (google({('GET', path(ANDROID, 'topIssues')): refused}), VALUES),
             'forbidden': (google({('GET', path(IOS, 'topIssues')): forbidden}), VALUES),
             'key refused': (Google(token=HttpError(400, 'x', '{"error": "invalid_grant"}')), VALUES),
             'unreachable': (Google(token=Unreachable('https://oauth2.googleapis.com/token unreachable (timed out)')), VALUES),
             'broken key': (google(), dict(VALUES, credentials=BROKEN_KEY)), 'no apps': (google(), {'credentials': KEY})}

    # when
    got = {}
    for name, (http, values) in cases.items():
        got[name] = tool_runtime(store, http, instances={}).connection_test(crashlytics.PLUGIN, None, dict(values), 5, NOW)

    # then
    assert (got['ok']['ok'], got['ok']['summary']) == (True, 'Crashlytics \u00b7 Firebase project 123456789012 \u00b7 android, ios')
    assert got['named']['summary'] == 'Crashlytics \u00b7 Firebase project shop-app \u00b7 android, ios'
    assert (got['refused']['ok'], got['refused']['summary']) == (
        False, f'the android app: the service account is invalid or lacks {engine.NEEDS} (HTTP 401: Request had invalid authentication credentials.)')
    assert (got['forbidden']['ok'], got['forbidden']['summary']) == (False, f'the ios app: {NO_ACCESS} (HTTP 403: The caller does not have permission)')
    assert engine.NEEDS in NO_ACCESS and 'roles/firebasecrashlytics.viewer' in got['forbidden']['summary']
    assert (got['key refused']['ok'], got['key refused']['summary']) == (False, msg('connection.rejected', status=400))
    assert (got['unreachable']['ok'], got['unreachable']['summary']) == (False, msg('connection.unreachable'))
    assert (got['broken key']['ok'], got['broken key']['summary']) == (False, msg('google.bad_credentials'))
    assert (got['no apps']['ok'], got['no apps']['summary']) == (False, NO_APPS)
    asked = query_of(cases['ok'][0], path(ANDROID, 'topIssues'))
    assert asked['page_size'] == ['1'] and asked['filter.interval.start_time'] == [iso(NOW - 3600)]
    assert [len(cases['ok'][0].hits(path(app, 'topIssues'))) for app in (ANDROID, IOS)] == [1, 1], 'each app is one part the check needs'


def test_crashlytics_second_instance_is_scoped(store: Store, tmp_path):
    # given
    http = Google({**crash_windows(ANDROID, quiet()), **crash_windows(IOS, quiet()), **crash_windows(EAST_ANDROID, spike())})
    east = {'credentials': KEY, 'android-app-id': EAST_ANDROID}

    # when
    with app_with(store, tmp_path, http, instances={'main': VALUES, 'east': east}) as c:
        c.app.state.sweeper.run('crashlytics', 'crashlytics', NOW)
    state = store.load()
    rate = next(a for a in state['anomalies'] if a['sig'] == 'crash-rate')

    # then
    assert (rate['cluster'], rate['ranKey']) == ('east', 'crashlytics:android-east')
    assert state['sources']['crashlytics-east'] == 'ok' and state['sources']['crashlytics'] == 'ok'


def test_crashlytics_rules_come_from_the_rule_set(store: Store, tmp_path):
    # given
    http = Google({**crash_windows(ANDROID, spike()), **crash_windows(IOS, quiet())})

    # when
    with app_with(store, tmp_path, http) as c:
        c.app.state.sweeper.run('crashlytics', 'crashlytics', NOW)
        before = by_key(store.load())
        c.app.state.runtime.live.set('crashlytics.rules', '{"new_users": 5}', ALICE)
        c.app.state.sweeper.run('crashlytics', 'crashlytics', NOW)
        after = by_key(store.load())

    # then
    assert before['crash-new|-/android-app|cccccccc03']['status'] == 'open'
    assert after['crash-new|-/android-app|cccccccc03']['status'] != 'open'
    assert after['crash-rate|-/android-app|FATAL']['status'] == 'open'


@pytest.mark.parametrize('plugin, check, values', [(crashlytics.PLUGIN, 'crashlytics', {'android-app-id': ANDROID}), (app_stores.PLUGIN, 'play', {'play-package': 'com.example.shop'}),
                                                    (ga4.PLUGIN, 'ga', {'property-id': '123456789'}), (gtm.PLUGIN, 'gtm', {'container': 'GTM-ABCD12'}),
                                                    (google_ads.PLUGIN, 'gads', {'customer-id': '123-456-7890', 'developer-token': 'DEVTOKEN-77aa-shop-0042'})])
def test_google_credentials_never_leave_the_server(store: Store, tmp_path, caplog, plugin, check, values):
    # given
    http = Google()

    # when
    with TestClient(create_app(settings(), store, registry=Registry.of(plugin), transport=http, kube=Kube(root=tmp_path))) as c:
        c.app.state.scheduler.nudge = lambda name: None
        with caplog.at_level(logging.DEBUG):
            made = c.post(f'/api/admin/plugins/{plugin.id}/instances', json={'name': 'main', 'values': {**values, 'credentials': BROKEN_KEY}, 'enabled': True}, headers=AUTH)
            listing = c.get('/api/admin/plugins', headers=AUTH)
            stored_test = c.post(f'/api/admin/plugins/{plugin.id}/test', json={'name': 'main', 'values': {}}, headers=AUTH)
            typed_test = c.post(f'/api/admin/plugins/{plugin.id}/test', json={'values': {**values, 'credentials': '{"private_key": "' + PLANTED}}, headers=AUTH)
            c.app.state.sweeper.run(plugin.id, check, NOW)
            changed = c.patch(f'/api/admin/plugins/{plugin.id}/instances/main', json={'values': {'credentials': BROKEN_KEY + ' '}}, headers=AUTH)
            feeds = [c.get('/api/admin/plugins', headers=AUTH), c.get('/api/activity', headers=AUTH), c.get('/api/state', headers=AUTH), c.get('/api/server', headers=AUTH)]
        stored = repr(list(store.db['instances'].find()))
    state = store.load()

    # then
    replies = [made, listing, stored_test, typed_test, changed, *feeds]
    assert [r.status_code for r in replies] == [201, 200, 200, 200, 200, 200, 200, 200, 200]
    assert (stored_test.json()['ok'], stored_test.json()['summary']) == (False, msg('google.bad_credentials'))
    assert (typed_test.json()['ok'], typed_test.json()['summary']) == (False, msg('google.bad_credentials'))
    assert state['sources'][check] == f"error: {msg('google.bad_credentials')}"
    seen = ''.join(r.text for r in replies) + caplog.text + repr(list(store.activity.find())) + stored + json.dumps(state, default=str)
    assert PLANTED not in seen and 'BEGIN PRIVATE KEY' not in seen
    assert http.calls == [], 'a broken key never reaches Google'


class Ctx:
    def __init__(self, config: dict, transport, name: str = 'main', primary: bool = True, rules: dict | None = None):
        self.config = config
        self.transport = transport
        self.now = NOW
        self.name = name
        self.primary = primary
        self.given = rules or {}

    @property
    def instance(self) -> InstanceInfo:
        return InstanceInfo(self.name, self.primary, None if self.primary else self.name, self.config)

    def http(self):
        return self.transport

    def rules(self, set_id: str) -> dict:
        assert set_id == 'rules'
        return {**engine.RULES, **self.given}

    def open_cards(self, plugin_wide: bool = False) -> list[dict]:
        return []
