import copy
import json
from pathlib import Path

import jwt
import pytest
from fastapi.testclient import TestClient

from skopax import clock, toolusage
from skopax.app import create_app
from skopax.contract import InstanceInfo
from skopax.engine import iso, merge
from skopax.instances import Instances, PluginState
from skopax.investigator import prompt
from skopax.investigator.tools import CORE_TOOLS
from skopax.investigator.toolset import ToolSet
from skopax.kit.google import TOKEN_URL, GoogleAuth
from skopax.kube import Kube
from skopax.messages import msg
from skopax.plugins import ga4, gtm
from skopax.plugins.ga4 import engine
from skopax.plugins.ga4.engine import GA_API, NEEDS, NO_ACCESS, PARTIAL, SCOPES, Analytics, attach_publish, attach_to_ga, day_window
from skopax.plugintools import PluginTools
from skopax.registry import BUILTIN, Registry, describe
from skopax.remote import HttpError, Unreachable
from skopax.runtime import Runtime
from skopax.sealer import Sealer, master_key
from skopax.store import Store
from tests.conftest import settings
from tests.fake_google import ACCESS, KEY, Google
from tests.plugin_kit import add_instance
from tests.test_investigator_tools import NOW, box

PROPERTY = '123456789'
APP_PROPERTY = '987654321'
REALTIME = f'{GA_API}/properties/{PROPERTY}:runRealtimeReport'
REPORT = f'{GA_API}/properties/{PROPERTY}:runReport'
METADATA = f'{GA_API}/properties/{PROPERTY}/metadata'
VALUES = {'property-id': PROPERTY, 'site': 'example.com', 'credentials': KEY}
SEAL = 'ga-key'
ALICE = 'alice@example.com'
DAY = 86400


def realtime_answer(query, body) -> dict:
    return {'dimensionHeaders': [{'name': 'unifiedScreenName'}], 'metricHeaders': [{'name': 'activeUsers', 'type': 'TYPE_INTEGER'}],
            'rows': [{'dimensionValues': [{'value': 'Checkout | example.com'}], 'metricValues': [{'value': '42'}]},
                     {'dimensionValues': [{'value': f'Sign in {ALICE}'}], 'metricValues': [{'value': '7'}]}], 'rowCount': 2}


def report_answer(query, body) -> dict:
    return {'dimensionHeaders': [{'name': 'eventName'}, {'name': 'dateRange'}], 'metricHeaders': [{'name': 'eventCount'}, {'name': 'engagementRate'}],
            'rows': [{'dimensionValues': [{'value': 'login'}, {'value': 'current'}], 'metricValues': [{'value': '120'}, {'value': '0.61234567'}]},
                     {'dimensionValues': [{'value': 'login'}, {'value': 'compare'}], 'metricValues': [{'value': '410'}, {'value': '0.7'}]}],
            'rowCount': 2, 'metadata': {'dataLossFromOtherRow': True}}


def daily(web: tuple[int, int] = (1200, 3000), day: str = '20261015', week: str = '20261008') -> dict:
    return {'dimensionHeaders': [{'name': 'platform'}, {'name': 'date'}, {'name': 'dateRange'}], 'metricHeaders': [{'name': 'activeUsers'}, {'name': 'keyEvents'}],
            'rows': [{'dimensionValues': [{'value': 'web'}, {'value': day}, {'value': 'current'}], 'metricValues': [{'value': '100000'}, {'value': str(web[0])}]},
                     {'dimensionValues': [{'value': 'web'}, {'value': week}, {'value': 'compare'}], 'metricValues': [{'value': '102000'}, {'value': str(web[1])}]},
                     {'dimensionValues': [{'value': 'iOS'}, {'value': day}, {'value': 'current'}], 'metricValues': [{'value': '100'}, {'value': '1'}]},
                     {'dimensionValues': [{'value': 'iOS'}, {'value': week}, {'value': 'compare'}], 'metricValues': [{'value': '400'}, {'value': '2'}]}]}


def google(over: dict | None = None, token: object = None) -> Google:
    return Google({('POST', REALTIME): realtime_answer, ('POST', REPORT): report_answer, ('GET', METADATA): {'name': f'properties/{PROPERTY}/metadata', 'dimensions': [{}], 'metrics': [{}]},
                   **(over or {})}, token)


def ga(http: Google, key: str = KEY, prop: str = PROPERTY, app: str = '') -> Analytics:
    return Analytics(GoogleAuth(key, http, lambda: NOW, SCOPES), prop, app)


def tool_runtime(store, http, instances: dict | None = None) -> Runtime:
    reg = Registry.of(ga4.PLUGIN)
    runtime = Runtime(reg, PluginState(store.db), Instances(store.db, reg, lambda: Sealer(SEAL)), http)
    for i, (name, values) in enumerate(({'main': VALUES} if instances is None else instances).items()):
        add_instance(store, ga4.PLUGIN, name, sealer_key=SEAL, primary=i == 0, **values)
    runtime.refresh()
    return runtime


def ga_box(store: Store, http, instances: dict | None = None, **kw):
    return box(store, plugin_tools=PluginTools(tool_runtime(store, http, instances)), **kw)


def call(b, **args):
    text, err = b.call('analytics', args)
    return (text if err else json.loads(text)), err


def sent(http: Google, url: str) -> dict:
    return http.hits(url)[-1][3]


def app_with(store, tmp_path, http, instances: dict | None = None, plugins: tuple = (ga4.PLUGIN,)):
    sealing = master_key(store.db, settings())
    for i, (name, values) in enumerate(({'main': VALUES} if instances is None else instances).items()):
        add_instance(store, ga4.PLUGIN, name, sealer_key=sealing, primary=i == 0, **values)
    if gtm.PLUGIN in plugins:
        add_instance(store, gtm.PLUGIN, 'main', sealer_key=sealing, primary=True, container='GTM-ABCD12', credentials=KEY)
    return TestClient(create_app(settings(), store, registry=Registry.of(*plugins), transport=http, kube=Kube(root=tmp_path)))


def by_key(state: dict) -> dict:
    return {a['key']: a for a in state['anomalies']}


def publish(to: str, at: float, name: str | None = 'Ads fix', changes: str | None = '1 tags changed (Ads conversion)', kind: str = 'gtm') -> dict:
    return {'id': f'gtm-GTM-ABCD12-{to}', 'ns': 'gtm', 'svc': 'GTM-ABCD12', 'at': iso(at), 'fromImage': f'v{int(to) - 1}', 'toImage': f'v{to}', 'kind': kind,
            'gtm': {'name': name, 'description': None, 'changes': changes, 'storedAt': None}}


def test_analytics_is_a_read_source_tool_offered_once_an_instance_serves(store: Store):
    # when
    none = [t['name'] for t in ga_box(store, google(), instances={}).definitions()]
    offered = [t['name'] for t in ga_box(store, google()).definitions()]

    # then
    assert ga4.PLUGIN.tools[0].scan and 'analytics' in offered and 'analytics' not in none
    assert not ga(google(), key='{"client_email": "x"}').enabled and not ga(google(), key='not json').enabled and not ga(google(), prop='G-ABC123').enabled


def test_realtime_signs_in_with_the_service_account_and_reads_the_last_minutes(store: Store):
    # given
    http = google()
    b = ga_box(store, http)

    # when
    out, err = call(b, op='realtime', dimensions=['unifiedScreenName'], minutes=10, filter={'dimension': 'country', 'value': 'Germany'})
    again = call(b, op='realtime')

    # then
    assert not err and not again[1]
    form = http.forms[0]
    claims = jwt.decode(form['assertion'], options={'verify_signature': False})
    assert form['grant_type'] == 'urn:ietf:params:oauth:grant-type:jwt-bearer' and len(http.forms) == 2, 'one sign-in per tool call'
    assert (claims['iss'], claims['scope'].split(), claims['exp'] - claims['iat']) == ('reader@analytics.example.com', list(SCOPES), 3600)
    assert SCOPES == ('https://www.googleapis.com/auth/analytics.readonly',), 'its own scope only'
    assert jwt.get_unverified_header(form['assertion'])['kid'] == 'kid-1'
    assert http.hits(REALTIME)[0][2]['Authorization'] == f'Bearer {ACCESS}'
    assert http.hits(REALTIME)[0][3] == {
        'dimensions': [{'name': 'unifiedScreenName'}], 'metrics': [{'name': 'activeUsers'}],
        'minuteRanges': [{'name': 'last 10 min', 'startMinutesAgo': 9, 'endMinutesAgo': 0}],
        'orderBys': [{'metric': {'metricName': 'activeUsers'}, 'desc': True}], 'limit': 20,
        'dimensionFilter': {'filter': {'fieldName': 'country', 'stringFilter': {'matchType': 'EXACT', 'value': 'Germany', 'caseSensitive': False}}},
        'returnPropertyQuota': True}
    assert sent(http, REALTIME)['minuteRanges'][0]['startMinutesAgo'] == 29
    assert out['rows'][0] == {'unifiedScreenName': 'Checkout | example.com', 'activeUsers': 42} and out['property'] == 'web'
    assert ALICE not in json.dumps(out) and out['rowCount'] == 2 and 'note' not in out


def test_report_compares_days_and_says_recent_days_are_partial(store: Store):
    # given
    http = google()
    b = ga_box(store, http)

    # when
    week, err = call(b, op='report', dimensions=['eventName'], metrics=['eventCount', 'engagementRate'], compare='week', filter={'dimension': 'eventName', 'value': 'log', 'match': 'begins'})
    previous = call(b, op='report', days=3, end='2026-10-10', compare='previous')[0]
    plain = call(b, op='report', end='yesterday')[0]

    # then
    assert not err
    assert sent(http, REPORT) == {'dateRanges': [{'startDate': 'yesterday', 'endDate': 'yesterday', 'name': 'current'}], 'dimensions': [],
                                  'metrics': [{'name': 'activeUsers'}, {'name': 'eventCount'}], 'orderBys': [{'metric': {'metricName': 'activeUsers'}, 'desc': True}],
                                  'limit': 20, 'returnPropertyQuota': True}
    assert http.hits(REPORT)[0][3]['dateRanges'] == [{'startDate': 'today', 'endDate': 'today', 'name': 'current'},
                                                     {'startDate': '7daysAgo', 'endDate': '7daysAgo', 'name': 'compare'}], 'GA4 reads relative days in the property time zone'
    assert http.hits(REPORT)[0][3]['dimensionFilter']['filter']['stringFilter']['matchType'] == 'BEGINS_WITH'
    assert previous['range'] == [{'startDate': '2026-10-08', 'endDate': '2026-10-10', 'name': 'current'}, {'startDate': '2026-10-05', 'endDate': '2026-10-07', 'name': 'compare'}]
    assert week['rows'] == [{'eventName': 'login', 'dateRange': 'current', 'eventCount': 120, 'engagementRate': 0.6123},
                            {'eventName': 'login', 'dateRange': 'compare', 'eventCount': 410, 'engagementRate': 0.7}]
    assert PARTIAL in week['note'] and 'folded into (other)' in week['note'] and 'dateRange current or compare' in week['note']
    assert PARTIAL not in previous['note'] and PARTIAL in plain['note']


@pytest.mark.parametrize('args, message, used', [
    ({'op': 'report', 'dimensions': ['pagePathPlusQueryString']}, 'report dimensions must be from date, dateHour', 1),
    ({'op': 'realtime', 'dimensions': ['pagePath']}, 'realtime dimensions must be from appVersion', 1),
    ({'op': 'realtime', 'metrics': ['sessions']}, 'realtime metrics must be from activeUsers', 1),
    ({'op': 'report', 'days': 8}, 'days must be at most 7', 0),
    ({'op': 'report', 'end': '2026-10-19'}, 'end is more than a day ahead of today', 1),
    ({'op': 'report', 'end': 'last week'}, 'end must be today, yesterday or YYYY-MM-DD', 1),
    ({'op': 'realtime', 'minutes': 60}, 'minutes must be at most 30', 0),
    ({'op': 'realtime', 'filter': {'dimension': 'city', 'value': ' '}}, 'filter value must be 1-200 characters', 1),
    ({'op': 'report', 'filter': {'dimension': 'unifiedScreenName', 'value': 'x'}}, 'report filter dimension must be from', 1),
    ({'op': 'report', 'dimensions': ['date', 'hour', 'country', 'city']}, 'dimensions takes at most 3 items', 0),
])
def test_bad_input_is_refused_before_any_call(store: Store, args, message, used):
    # given
    http = google()
    b = ga_box(store, http)

    # when
    text, err = b.call('analytics', args)

    # then
    assert err and message in text
    assert http.calls == [] and b.used['analytics'] == used, 'a refusal inside the tool costs one call of the run (P5-C81), a schema refusal none'


def test_refusals_quota_and_google_errors_are_named_and_the_run_cap_holds(store: Store):
    # given
    denied = ga_box(store, google({('POST', REALTIME): HttpError(403, REALTIME, '{"error": {"code": 403, "status": "PERMISSION_DENIED"}}')}))
    quota = ga_box(store, google({('POST', REALTIME): HttpError(429, REALTIME)}))
    invalid = ga_box(store, google({('POST', REPORT): HttpError(400, REPORT, f'{{"error": {{"code": 400, "message": "Field x is not valid for {ALICE}"}}}}')}))
    bad_key = ga_box(store, google(token=HttpError(400, TOKEN_URL, '{"error": "invalid_grant", "error_description": "Invalid JWT Signature."}')))
    b = ga_box(store, google())

    # when
    answers = [call(b, op='realtime')[1] for _ in range(ga4.PER_RUN + 1)]
    wrong = call(invalid, op='report')[0]

    # then
    assert call(denied, op='realtime') == (f'{NO_ACCESS} (HTTP 403)', True) and 'Viewer' in NO_ACCESS
    assert call(quota, op='realtime') == ('the Google Analytics quota is used up for now (HTTP 429)', True)
    assert wrong.startswith('Google Analytics HTTP 400: Field x is not valid for') and ALICE not in wrong
    assert call(bad_key, op='realtime') == (f'the service account is invalid or lacks {NEEDS}: Google refused the service account key (HTTP 400 invalid_grant)', True)
    assert answers == [False] * ga4.PER_RUN + [True]


def test_the_app_property_is_read_when_asked_and_named_when_missing_or_malformed(store: Store):
    # given
    http = google({('POST', f'{GA_API}/properties/{APP_PROPERTY}:runRealtimeReport'): realtime_answer})
    both = ga_box(store, http, instances={'main': dict(VALUES, **{'app-property-id': APP_PROPERTY})})
    web_only = ga_box(store, google())
    malformed = ga_box(store, google(), instances={'main': dict(VALUES, **{'app-property-id': 'G-ABC123'})})
    app_only = ga(google(), prop='', app=APP_PROPERTY)

    # when
    app, err = call(both, op='realtime', property='app')
    missing = call(web_only, op='realtime', property='app')

    # then
    assert not err and app['property'] == 'app'
    assert missing == ('the app GA4 property id is not set; the web property may hold the app streams (streamName)', True)
    assert call(malformed, op='realtime', property='app') == (engine.BAD_APP_PROPERTY, True)
    assert app_only.enabled and app_only.property_of(None) == APP_PROPERTY


def test_a_403_carries_googles_reason(store: Store):
    # given
    body = '{"error": {"code": 403, "message": "Google Analytics Data API has not been used in project 123456789012 before or it is disabled.", "status": "PERMISSION_DENIED"}}'
    b = ga_box(store, google({('POST', REALTIME): HttpError(403, REALTIME, body)}))

    # when
    text, err = call(b, op='realtime')

    # then
    assert err and text == f'{NO_ACCESS} (HTTP 403: Google Analytics Data API has not been used in project 123456789012 before or it is disabled.)'


def test_a_401_after_a_good_sign_in_names_both_causes_and_the_next_call_signs_in_again(store: Store):
    # given
    http = google({('POST', REALTIME): HttpError(401, REALTIME)})
    b = ga_box(store, http)

    # when
    first, second = call(b, op='realtime'), call(b, op='realtime')

    # then
    assert first == second == (f'the service account is invalid or lacks {NEEDS} (HTTP 401)', True)
    assert len(http.forms) == 2


def test_the_key_and_the_access_token_never_reach_answers_errors_or_the_run_record(store: Store):
    # given
    b = ga_box(store, google({('POST', REPORT): HttpError(500, REPORT, f'{{"error": {{"message": "boom {ACCESS}"}}}}')}), run_id='run-1')

    # when
    results = [b.call('analytics', {'op': 'realtime'}), b.call('analytics', {'op': 'report'})]
    seen = json.dumps([results, b.calls, list(store.activity.find({}, {'_id': 0}))], default=str)

    # then
    assert results[1] == ('Google Analytics HTTP 500: boom ***', True)
    assert ACCESS not in seen and 'PRIVATE KEY' not in seen and 'kid-1' not in seen


def test_ga_compares_the_day_before_yesterday_with_a_week_earlier_per_platform():
    # given
    http = Google({('POST', REPORT): lambda q, b: daily()})

    # when
    out = ga4.run(Ctx(VALUES, http)).result

    # then
    asked = http.hits(REPORT)[0][3]
    assert [(r['startDate'], r['endDate']) for r in asked['dateRanges']] == [('2daysAgo', '2daysAgo'), ('9daysAgo', '9daysAgo')]
    assert asked['dimensions'] == [{'name': 'platform'}, {'name': 'date'}]
    assert out.ok and out.status == 'ok' and set(out.ran) == {'ga', 'ga:access:web'}
    assert [c['key'] for c in out.found] == ['ga-drop|-/example.com|web:keyEvents']
    assert '1,200 key events on web on 2026-10-15, 40 % of the same weekday a week earlier (3,000)' in out.found[0]['what']
    assert (out.found[0]['short'], out.found[0]['slack']) == ('WARN GA4 web key events 1,200 on 2026-10-15 (week before 3,000)', 3000)
    assert engine.links_for(out.found[0]) == [{'label': 'GA4 property', 'url': 'https://analytics.google.com/analytics/web/#/p123456789/reports/intelligenthome'}]
    assert ga4.links({'src': 'prom', 'sig': 'err'}, InstanceInfo('main', True, None, VALUES)) is None


def test_without_a_site_the_cards_name_the_property():
    # given
    http = Google({('POST', REPORT): lambda q, b: daily()})

    # when
    out = ga4.run(Ctx({'property-id': PROPERTY, 'credentials': KEY}, http)).result

    # then
    assert [c['key'] for c in out.found] == ['ga-drop|-/123456789|web:keyEvents']


def test_the_day_is_the_propertys_own_day_whatever_the_configured_zone():
    # given
    clock.set_zone('Asia/Tokyo')
    http = Google({('POST', REPORT): lambda q, b: daily(day='20261014', week='20261007')})

    # when
    out = ga4.run(Ctx(VALUES, http, now=NOW + 17 * 3600)).result

    # then
    assert [r['startDate'] for r in http.hits(REPORT)[0][3]['dateRanges']] == ['2daysAgo', '9daysAgo']
    assert out.found[0]['gaDay'] == '2026-10-14' and 'on web on 2026-10-14' in out.found[0]['what']


def test_the_tools_default_day_is_the_propertys_today_whatever_the_configured_zone(store: Store):
    # given
    clock.set_zone('Asia/Tokyo')
    http = google()
    b = ga_box(store, http)

    # when
    out, err = call(b, op='report', days=2, compare='previous')

    # then
    assert not err and sent(http, REPORT)['dateRanges'] == [{'startDate': 'yesterday', 'endDate': 'today', 'name': 'current'},
                                                            {'startDate': '3daysAgo', 'endDate': '2daysAgo', 'name': 'compare'}]
    assert PARTIAL in out['note']


def test_a_drop_card_that_climbs_back_while_still_low_is_not_pushed_again_as_escalated():
    # given
    def rows(events: int) -> list[dict]:
        return [{'platform': 'web', 'dateRange': 'current', 'activeUsers': 90000, 'keyEvents': events}, {'platform': 'web', 'dateRange': 'compare', 'activeUsers': 90000, 'keyEvents': 3000}]

    state = {'initialized': True, 'anomalies': []}
    first, later = (engine.evaluate_ga('2026-11-08', PROPERTY, 'example.com', rows(v), engine.RULES) for v in (500, 1500))
    _, pushed, _ = merge(state, copy.deepcopy(first), {}, NOW)

    # when
    events, push, _ = merge(state, copy.deepcopy(later), {}, NOW + 3600)

    # then
    assert len(first) == len(later) == 1 and [a['key'] for a in pushed] == [first[0]['key']]
    assert [e['ev'] for e in events] == [] and push == []


def test_a_drop_card_names_the_gtm_publish_before_it():
    # given
    card, old = {'what': 'Sign-ups fell.'}, {'what': 'Sign-ups fell.'}
    rows = [publish('134', NOW - 3 * 3600), publish('130', NOW - 5 * DAY, name=None, changes=None), publish('135', NOW - 3600, kind='other')]

    # when
    attach_publish([card], rows, NOW - 48 * 3600, NOW)
    attach_publish([old], rows, NOW - DAY, NOW - 4 * 3600)

    # then
    assert card['gtmPublish'] == {'container': 'GTM-ABCD12', 'version': '134', 'name': 'Ads fix', 'at': iso(NOW - 3 * 3600), 'changes': '1 tags changed (Ads conversion)'}
    assert card['what'].startswith('Sign-ups fell. GTM GTM-ABCD12 v134 "Ads fix" went live around ') and card['what'].endswith(': 1 tags changed (Ads conversion).')
    assert old == {'what': 'Sign-ups fell.'}


def test_ga_cards_take_the_publish_from_two_days_before_their_day_on_web_only():
    # given
    day = iso(NOW)[:10]
    web, app = {'what': 'down.', 'gaDay': day, 'sub': 'web:keyEvents'}, {'what': 'down.', 'gaDay': day, 'sub': 'Android:keyEvents'}

    # when
    attach_to_ga([web, app], [publish('134', NOW - 3 * 3600)])

    # then
    start, end = day_window(day)
    assert end - start == 4 * DAY and web['gtmPublish']['version'] == '134' and 'gtmPublish' not in app


def test_the_ga_check_reads_the_publishes_of_its_whole_window_through_the_app(store: Store, tmp_path):
    # given
    http = Google({('POST', REPORT): lambda q, b: daily()})
    publishes = [{'from': '133', 'to': '134', 'name': 'Ads fix', 'at': iso(NOW - 3 * DAY), 'changes': '1 tags changed (Ads conversion)'}]
    store.save({'anomalies': [], 'gtm': [{'plugin': 'gtm', 'container': 'GTM-ABCD12', 'live': {'id': '134'}, 'publishes': publishes}]}, None)

    # when
    with app_with(store, tmp_path, http, plugins=(ga4.PLUGIN, gtm.PLUGIN)) as c:
        c.app.state.sweeper.run('ga4', 'ga', NOW)
    card = by_key(store.load())['ga-drop|-/example.com|web:keyEvents']

    # then
    assert NOW - 3 * DAY < NOW - 48 * 3600, 'older than 48 h, yet inside the window of the day the check judges'
    assert card['gtmPublish']['version'] == '134' and 'GTM GTM-ABCD12 v134 "Ads fix" went live around' in card['what']


def test_an_empty_report_is_no_data_and_keeps_the_drop_cards_open():
    # given
    http = Google({('POST', REPORT): {'dimensionHeaders': [{'name': 'platform'}, {'name': 'dateRange'}], 'rowCount': 0}})

    # when
    out = ga4.run(Ctx(VALUES, http)).result

    # then
    assert (out.ok, out.status, set(out.ran), out.found) == (True, 'ok \u00b7 no data', {'ga:no-data', 'ga:access:web'}, [])


def test_a_refused_property_is_never_read_as_no_data_and_gets_a_warn_card_naming_the_role():
    # given
    denied = HttpError(403, REPORT, '{"error": {"code": 403, "message": "User does not have sufficient permissions for this property.", "status": "PERMISSION_DENIED"}}')
    http = Google({('POST', REPORT): denied})

    # when
    out = ga4.run(Ctx(VALUES, http)).result

    # then
    assert (out.ok, out.status, out.ran) == (True, 'ok \u00b7 no access to the web property', ('ga:access:web',))
    [card] = out.found
    assert (card['key'], card['sev'], card['ranKey'], card['src']) == ('no-access|-/ga|web', 'warn', 'ga:access:web', 'ga')
    assert card['label'] == f'Google Analytics 4 cannot read the web property: the service account lacks {NEEDS}'
    assert 'sufficient permissions' not in card['what']


def test_a_refused_key_in_a_pass_is_a_warn_card_naming_both_causes():
    # given
    http = Google({('POST', REPORT): lambda q, b: daily()}, token=HttpError(400, TOKEN_URL, '{"error": "invalid_grant"}'))

    # when
    out = ga4.run(Ctx(VALUES, http)).result

    # then
    assert (out.ok, out.status, out.ran) == (True, 'ok \u00b7 no access to the web property', ('ga:access:web',))
    assert out.found[0]['label'] == f'Google Analytics 4 cannot read the web property: the service account is invalid or lacks {NEEDS}'


def test_the_access_card_clears_at_the_first_pass_that_reads_the_property_again(store: Store, tmp_path):
    # given
    answers = {'refuse': True}
    http = Google({('POST', REPORT): lambda q, b: HttpError(403, REPORT) if answers['refuse'] else daily((3000, 3000))})

    # when
    with app_with(store, tmp_path, http) as c:
        c.app.state.sweeper.run('ga4', 'ga', NOW)
        refused = by_key(store.load())
        answers['refuse'] = False
        c.app.state.sweeper.run('ga4', 'ga', NOW)
        read = by_key(store.load())

    # then
    assert refused['no-access|-/ga|web']['status'] == 'open'
    assert read['no-access|-/ga|web']['status'] != 'open'
    assert store.load()['sources']['ga'] == 'ok'


@pytest.mark.parametrize('answer', [HttpError(403, REPORT), HttpError(500, REPORT, '{"error": {"message": "Backend failure."}}')], ids=['refused', 'server error'])
def test_a_drop_card_stays_open_through_a_pass_that_cannot_read(store: Store, tmp_path, answer):
    # given
    answers = {'now': daily()}
    http = Google({('POST', REPORT): lambda q, b: answers['now']})

    # when
    with app_with(store, tmp_path, http) as c:
        c.app.state.sweeper.run('ga4', 'ga', NOW)
        before = by_key(store.load())
        answers['now'] = answer
        c.app.state.sweeper.run('ga4', 'ga', NOW)
        after = by_key(store.load())

    # then
    assert before['ga-drop|-/example.com|web:keyEvents']['status'] == 'open'
    assert after['ga-drop|-/example.com|web:keyEvents']['status'] == 'open', 'the card is held'


def test_a_server_error_fails_the_pass_with_a_fixed_reason():
    # given
    http = Google({('POST', REPORT): HttpError(500, REPORT, '{"error": {"message": "Backend failure."}}')})

    # when
    out = ga4.run(Ctx(VALUES, http)).result

    # then
    assert (out.ok, out.status) == (False, 'error: Google Analytics HTTP 500')


@pytest.mark.parametrize('values, text', [({'property-id': 'G-ABC123'}, engine.BAD_PROPERTY), ({'property-id': PROPERTY, 'site': 'example.com/shop'}, engine.BAD_SITE)])
def test_a_bad_property_or_site_fails_the_pass_before_any_call(values, text):
    # given
    http = Google()

    # when
    out = ga4.run(Ctx(dict(values, credentials=KEY), http)).result

    # then
    assert (out.ok, out.status) == (False, f'error: {text}') and http.calls == []


def test_a_ga_pass_stops_at_its_time_budget(monkeypatch):
    # given
    monkeypatch.setattr(ga4, 'BUDGET_S', 0)
    http = Google({('POST', REPORT): lambda q, b: daily()})

    # when
    out = ga4.run(Ctx(VALUES, http)).result

    # then
    assert (out.ok, out.status) == (False, 'error: Google reads stopped at their 0 s budget') and http.calls == []


def test_the_app_runs_the_ga_check_once_a_day_with_its_own_scope(store: Store, tmp_path):
    # given
    http = Google({('POST', REPORT): lambda q, b: daily()})

    # when
    with app_with(store, tmp_path, http) as c:
        jobs = {j.name: j for j in c.app.state.scheduler.jobs}
        tiles = {p['id']: p for p in c.get('/api/server').json()['plugins']}
        c.app.state.sweeper.run('ga4', 'ga', NOW)
    state = store.load()

    # then
    assert (jobs['ga4.ga'].period, jobs['ga4.ga'].offset) == (86400, 27000)
    assert tiles['ga4']['passes'] == ['ga'] and state['sources']['ga'] == 'ok'
    card = by_key(state)['ga-drop|-/example.com|web:keyEvents']
    assert card['links'][0] == {'label': 'GA4 property', 'url': 'https://analytics.google.com/analytics/web/#/p123456789/reports/intelligenthome'}
    assert jwt.decode(http.forms[0]['assertion'], options={'verify_signature': False})['scope'] == 'https://www.googleapis.com/auth/analytics.readonly'


def test_the_analytics_tool_status_is_off_without_an_instance_and_on_with_one(store: Store):
    # when
    off = ToolSet(tool_runtime(store, google(), instances={})).status('analytics', None)
    one = ToolSet(tool_runtime(store, google())).status('analytics', None)

    # then
    assert (off['state'], one) == ('off', {'state': 'on', 'reason': None})


def test_the_analytics_tool_row_carries_what_the_investigator_is_told(store: Store):
    # given
    runs = [{'_id': 'r1', 'startedAt': '2026-10-28T10:00:00Z', 'trigger': 'cards', 'toolCalls': [{'name': 'analytics', 'ms': 900, 'error': None, 'truncated': False}]}]

    # when
    tools = {t['name']: t for t in toolusage.summary(runs, 1, '2026-10-22T00:00:00Z', None, PluginTools(tool_runtime(store, google())))['tools']}

    # then
    assert 'use realtime for right now' in tools['analytics']['description']


def test_ga4_identity_and_manifest():
    # given
    registry = Registry.load({}, entry_points=False, core_tools=CORE_TOOLS, core_knowledge=prompt.core_anchors())

    # when
    found = registry.get('ga4')
    manifest = describe(ga4.PLUGIN, icon=True)
    check, tool, rules = ga4.PLUGIN.checks[0], ga4.PLUGIN.tools[0], ga4.PLUGIN.rules[0]

    # then
    assert 'skopax.plugins.ga4' in BUILTIN and found is not None and found.state == 'ok' and found.plugin is ga4.PLUGIN
    assert json.loads(json.dumps(manifest, allow_nan=False)) == manifest
    assert (manifest['id'], manifest['name'], manifest['version'], manifest['category'], manifest['author'], manifest['licence'], manifest['core'], manifest['edition']) == (
        'ga4', 'Google Analytics 4', '0.1.0', 'marketing', 'Skopax', 'Apache-2.0', '>=0.1,<0.6', 'community')
    assert manifest['capabilities'] == {'hosts': ['analyticsdata.googleapis.com', 'oauth2.googleapis.com'], 'secrets': ['credentials'], 'agentReads': False}
    assert manifest['uses'] == []
    assert [(f.key, f.kind, f.required) for f in ga4.PLUGIN.fields] == [('property-id', 'text', True), ('app-property-id', 'text', False), ('site', 'text', False),
                                                                    ('credentials', 'secret', True)]
    assert (check.id, check.period, check.offset, check.ran, check.sections, check.tool, check.once) == ('ga', 86400, 27000, ('ga',), (), 'analytics', False)
    assert (rules.id, set(rules.help) == set(rules.defaults), dict(rules.defaults) == engine.RULES) == ('rules', True, True)
    assert (tool.name, tool.kind, tool.per_run, tool.scan, tool.instance_input) == ('analytics', 'read', 6, True, 'instance')
    assert registry.icon('ga4').startswith('<svg') and ga4.PLUGIN.releases is None
    assert [k.id for k in ga4.PLUGIN.knowledge] == ['ga4'] and len(ga4.PLUGIN.knowledge[0].text.splitlines()) <= 40


def test_ga4_defaults_are_neutral():
    # then
    assert {f.key: f.default for f in ga4.PLUGIN.fields} == {'property-id': '', 'app-property-id': '', 'site': '', 'credentials': ''}
    assert {s.key: s.default for s in ga4.PLUGIN.settings} == {}
    assert dict(ga4.PLUGIN.rules[0].defaults) == {'drop': 0.3, 'floor': 500}


def test_ga4_connection_test_reports_ok_and_failures(store):
    # given
    unauthorised = HttpError(401, METADATA, '{"error": {"message": "Request had invalid authentication credentials.", "status": "UNAUTHENTICATED"}}')
    cases = {'ok': (google(), VALUES), 'refused': (google({('GET', METADATA): unauthorised}), VALUES),
             'forbidden': (google({('GET', METADATA): HttpError(403, METADATA)}), VALUES),
             'key refused': (google(token=HttpError(401, TOKEN_URL, '{"error": "invalid_client"}')), VALUES),
             'unreachable': (google(token=Unreachable('https://oauth2.googleapis.com/token unreachable (timed out)')), VALUES),
             'bad property': (google(), dict(VALUES, **{'property-id': 'G-ABC123'})), 'bad app property': (google(), dict(VALUES, **{'app-property-id': 'x'}))}

    # when
    got = {name: tool_runtime(store, http, instances={}).connection_test(ga4.PLUGIN, None, dict(values), 5, NOW) for name, (http, values) in cases.items()}

    # then
    assert (got['ok']['ok'], got['ok']['summary']) == (True, 'Google Analytics 4 \u00b7 web property 123456789')
    assert (got['refused']['ok'], got['refused']['summary']) == (
        False, f'the web property: the service account is invalid or lacks {NEEDS} (HTTP 401: Request had invalid authentication credentials.)')
    assert (got['forbidden']['ok'], got['forbidden']['summary']) == (False, f'the web property: {NO_ACCESS} (HTTP 403)')
    assert (got['key refused']['ok'], got['key refused']['summary']) == (False, msg('connection.rejected', status=401))
    assert (got['unreachable']['ok'], got['unreachable']['summary']) == (False, msg('connection.unreachable'))
    assert (got['bad property']['ok'], got['bad property']['summary']) == (False, engine.BAD_PROPERTY)
    assert (got['bad app property']['ok'], got['bad app property']['summary']) == (False, engine.BAD_APP_PROPERTY)
    assert len(cases['ok'][0].hits(METADATA)) == 1


def test_ga4_second_instance_is_scoped(store: Store, tmp_path):
    # given
    east_report = f'{GA_API}/properties/555555555:runReport'
    http = Google({('POST', REPORT): lambda q, b: daily((3000, 3000)), ('POST', east_report): lambda q, b: daily()})
    east = {'property-id': '555555555', 'site': 'shop.example.com', 'credentials': KEY}

    # when
    with app_with(store, tmp_path, http, instances={'main': VALUES, 'east': east}) as c:
        c.app.state.sweeper.run('ga4', 'ga', NOW)
    state = store.load()
    card = next(a for a in state['anomalies'] if a['sig'] == 'ga-drop')

    # then
    assert (card['cluster'], card['ranKey']) == ('east', 'ga-east')
    assert state['sources']['ga-east'] == 'ok' and state['sources']['ga'] == 'ok'


def test_ga4_rules_come_from_the_rule_set(store: Store, tmp_path):
    # given
    http = Google({('POST', REPORT): lambda q, b: daily()})

    # when
    with app_with(store, tmp_path, http) as c:
        c.app.state.sweeper.run('ga4', 'ga', NOW)
        before = by_key(store.load())
        c.app.state.runtime.live.set('ga4.rules', '{"drop": 0.7}', ALICE)
        c.app.state.sweeper.run('ga4', 'ga', NOW)
        after = by_key(store.load())

    # then
    assert before['ga-drop|-/example.com|web:keyEvents']['status'] == 'open'
    assert after['ga-drop|-/example.com|web:keyEvents']['status'] != 'open'


class Ctx:
    def __init__(self, config: dict, transport, name: str = 'main', primary: bool = True, now: float = NOW, rules: dict | None = None, deploys: list | None = None):
        self.config = config
        self.transport = transport
        self.now = now
        self.name = name
        self.primary = primary
        self.given = rules or {}
        self.released = deploys or []

    @property
    def instance(self) -> InstanceInfo:
        return InstanceInfo(self.name, self.primary, None if self.primary else self.name, self.config)

    def http(self):
        return self.transport

    def rules(self, set_id: str) -> dict:
        assert set_id == 'rules'
        return {**engine.RULES, **self.given}

    def deploys(self, since: float) -> list[dict]:
        return [d for d in self.released if d['at'] >= iso(since)]

    def open_cards(self, plugin_wide: bool = False) -> list[dict]:
        return []


def test_an_explicit_end_may_be_the_property_s_today_when_its_zone_runs_ahead(store: Store):
    # given
    http = google()
    b = ga_box(store, http)

    # when
    out, err = call(b, op='report', end='2026-10-18')

    # then
    assert not err and sent(http, REPORT)['dateRanges'][0]['endDate'] == '2026-10-18'


def test_a_publish_late_in_the_property_s_day_is_still_named_when_its_zone_runs_behind():
    # given
    day = iso(NOW)[:10]
    web = {'what': 'down.', 'gaDay': day, 'sub': 'web:keyEvents'}
    start, end = day_window(day)

    # when
    attach_to_ga([web], [publish('135', start + 2 * DAY + 20 * 3600)])

    # then
    assert web['gtmPublish']['version'] == '135'
    assert 'a day after' in (Path(ga4.__file__).parent / 'ga4.md').read_text(encoding='utf-8')
