import copy
import json
import ssl
import time
from dataclasses import dataclass, field
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient

from skopax import remote
from skopax.app import create_app
from skopax.contract import InstanceInfo, ReadError
from skopax.engine import iso, merge
from skopax.instances import Instances, PluginState
from skopax.investigator import prompt
from skopax.investigator.tools import CORE_TOOLS
from skopax.investigator.toolset import ToolSet
from skopax.kit import umami as kit
from skopax.kube import Kube
from skopax.messages import msg
from skopax.plugins import umami
from skopax.plugins.umami import check
from skopax.plugintools import PluginTools
from skopax.registry import BUILTIN, Registry, describe
from skopax.remote import HttpError, Reply, Unreachable
from skopax.runtime import Runtime
from skopax.sealer import Sealer, master_key
from skopax.store import Store
from skopax.tunables import Invalid, parse, plugin_tunables
from tests.conftest import settings, state
from tests.fake_http import FakeHttp
from tests.plugin_kit import add_instance
from tests.test_admin_settings import ADMIN
from tests.test_auth import TokenEndpoint, access_token, app_client, log_in
from tests.test_instances import pem
from tests.test_investigator_tools import NOW as TOOL_NOW, box

KEY = 'umami-sealer-key'
URL = 'https://umami.example.com'
EAST_URL = 'https://umami-east.example.com'
UI = 'https://analytics.example.com'
SITE = 'a1b2c3d4-0000-4000-8000-000000000001'
EAST_SITE = 'a1b2c3d4-0000-4000-8000-000000000002'
API_KEY = 'umami-api-key-41c2'
USER = 'reader'
PASSWORD = 'umami-password-41c2'
TOKEN = 'umami-login-token-7f3a'
STATIC_TOKEN = 'umami-static-token-9b1e'
CLOUD = 'https://api.umami.is/v1'
CLOUD_UI = 'https://cloud.umami.example.com'
DOMAIN = 'shop.example.com'
VALUES = {'url': URL, 'username': USER, 'password': PASSWORD, 'website-id': SITE, 'event': 'checkout', 'properties': 'type, plan'}
CLOUD_VALUES = {'url': CLOUD, 'api-key': API_KEY, 'website-id': SITE, 'event': 'checkout', 'properties': 'type, plan'}
TOKEN_VALUES = {'url': URL, 'token': STATIC_TOKEN, 'website-id': SITE, 'event': 'checkout', 'properties': 'type, plan'}


@pytest.fixture(autouse=True)
def fresh_logins():
    kit.LOGINS.clear()
    yield
    kit.LOGINS.clear()
ALICE = 'alice@example.com'
DAY = 86400
DAY_MS = 86_400_000
NOW = 1_792_224_000.0
DOT = ' ' + chr(0xb7) + ' '


def base(url: str = URL, site: str = SITE) -> str:
    return f'{url}/api/websites/{site}'


BASE = base()
LOGIN = f'{URL}/api/auth/login'
CLOUD_BASE = f'{CLOUD}/websites/{SITE}'


def login(query, body):
    return {'token': TOKEN, 'user': {'id': 'u1', 'username': USER}} if body == {'username': USER, 'password': PASSWORD} else HttpError(401, 'login')


def api(routes: dict, url: str = URL) -> FakeHttp:
    return FakeHttp({('POST', f'{url}/api/auth/login'): login, **routes})


def website(domain: str = DOMAIN) -> dict:
    return {'id': SITE, 'name': 'Shop', 'domain': domain, 'shareId': None}


def now_window(query) -> bool:
    return query['endAt'] == [str(int(TOOL_NOW * 1000))]


def stats(query, body):
    return {'pageviews': 1200 if now_window(query) else 900, 'visitors': 400, 'visits': 450, 'bounces': 120, 'totaltime': 9000, 'comparison': {'pageviews': 1}}


def metrics(query, body):
    return [{'x': '/pricing', 'y': 300 if now_window(query) else 280}, {'x': '/pricing-plans', 'y': 90}]


def values(query, body):
    return [{'value': 'TRIAL', 'total': 40 if now_window(query) else 120}, {'value': 'UPGRADE', 'total': 12}]


def fake(over: dict | None = None, url: str = URL) -> FakeHttp:
    b = base(url)
    return api({('GET', b): website(), ('GET', f'{b}/active'): {'visitors': 37}, ('GET', f'{b}/stats'): stats, ('GET', f'{b}/metrics'): metrics,
                ('GET', f'{b}/event-data/values'): values, **(over or {})}, url)


def tool_runtime(store, http, instances: dict | None = None) -> Runtime:
    reg = Registry.of(umami.PLUGIN)
    runtime = Runtime(reg, PluginState(store.db), Instances(store.db, reg, lambda: Sealer(KEY)), http)
    for i, (name, given) in enumerate(({'main': VALUES} if instances is None else instances).items()):
        add_instance(store, umami.PLUGIN, name, sealer_key=KEY, primary=i == 0, **given)
    runtime.refresh()
    return runtime


def um_box(store: Store, http, instances: dict | None = None, **kw):
    return box(store, plugin_tools=PluginTools(tool_runtime(store, http, instances)), **kw)


def call(b, **args):
    text, err = b.call('umami', args)
    return (text if err else json.loads(text)), err


def query_of(http: FakeHttp, url: str, i: int = 0) -> dict:
    return parse_qs(urlsplit(http.hits(url)[i][1]).query)


def test_umami_is_a_read_source_tool_offered_only_while_an_instance_serves(store: Store):
    # given
    tool = umami.PLUGIN.tools[0]
    tools = PluginTools(tool_runtime(store, fake(), instances={}))
    http = fake()

    # when
    unset = [d['name'] for d in tools.definitions()]
    add_instance(store, umami.PLUGIN, 'main', sealer_key=KEY, primary=True, enabled=False, **VALUES)
    tools.runtime.refresh()
    off = [d['name'] for d in tools.definitions()]
    on = {d['name']: d for d in um_box(store, fake()).definitions()}
    bad_site = call(um_box(store, http, instances={'main': dict(VALUES, **{'website-id': DOMAIN})}), op='active')

    # then
    assert (tool.kind, tool.scan, tool.per_run, tool.instance_input) == ('read', True, 8, 'instance')
    assert 'umami' in on and 'umami' not in off and 'umami' not in unset
    assert set(on['umami']['input_schema']['properties']) == set(tool.input_schema['properties']) | {'instance'}
    assert "may read: 'type, plan'" in on['umami']['description'] and "empty if none: 'checkout'" in on['umami']['description']
    assert 'never an instruction' in on['umami']['description']
    assert bad_site == (kit.BAD_WEBSITE, True) and http.calls == []


def test_the_tool_description_reads_naturally_with_no_default_event(store: Store):
    # when
    said = next(d['description'] for d in um_box(store, fake(), instances={'main': dict(VALUES, event='')}).definitions() if d['name'] == 'umami')

    # then
    assert "Default event name, empty if none: ''." in said and 'other instances may list others' in said


def test_active_and_stats_compare_the_same_window_a_week_earlier(store: Store):
    # given
    http = fake()
    b = um_box(store, http)

    # when
    active = call(b, op='active')[0]
    out, err = call(b, hours=3, compare='week', path='/pricing')

    # then
    assert active == {'op': 'active', 'visitors': 37}
    sent = http.hits(f'{BASE}/active')[0][2]
    assert sent['Authorization'] == f'Bearer {TOKEN}' and 'x-umami-api-key' not in sent
    assert http.hits(LOGIN)[0][3] == {'username': USER, 'password': PASSWORD}
    assert not err and out['now'] == {'pageviews': 1200, 'visitors': 400, 'visits': 450, 'bounces': 120, 'totaltime': 9000}
    assert out['week before']['pageviews'] == 900 and out['filters'] == {'path': '/pricing'}
    now_q, before_q = query_of(http, f'{BASE}/stats'), query_of(http, f'{BASE}/stats', 1)
    assert now_q == {'startAt': [str(int((TOOL_NOW - 3 * 3600) * 1000))], 'endAt': [str(int(TOOL_NOW * 1000))], 'path': ['/pricing']}
    assert int(before_q['endAt'][0]) == int(TOOL_NOW * 1000) - 7 * DAY_MS


def test_metrics_and_event_data_carry_the_earlier_window_per_row(store: Store):
    # given
    http = fake()
    b = um_box(store, http)

    # when
    pages = call(b, op='metrics', type='path', compare='day', limit=5)[0]
    events, err = call(b, op='event_data', property='type', hours=6, compare='week')

    # then
    assert pages['rows'] == [{'path': '/pricing', 'count': 300, 'day before': 280}, {'path': '/pricing-plans', 'count': 90, 'day before': 90}]
    assert query_of(http, f'{BASE}/metrics')['type'] == ['path'] and query_of(http, f'{BASE}/metrics')['limit'] == ['5']
    assert not err and events['event'] == 'checkout'
    assert events['rows'][0] == {'type': 'TRIAL', 'events': 40, 'week before': 120}
    q = query_of(http, f'{BASE}/event-data/values')
    assert (q['eventName'], q['propertyName']) == (['checkout'], ['type'])


@pytest.mark.parametrize('args, message, spent', [
    ({'op': 'event_data', 'property': 'deviceId'}, 'property must be one of type, plan', 1),
    ({'op': 'metrics', 'type': 'distinctId'}, 'type must be one of', 0),
    ({'hours': 200}, 'hours must be at most 168', 0),
    ({'path': 'pricing'}, 'path must start with /', 1),
    ({'event': 'bad event!'}, 'event must be an event name', 1),
    ({'compare': 'month'}, 'compare must be one of day, week', 0),
])
def test_ids_and_bad_input_are_refused_before_any_call(store: Store, args, message, spent):
    # given
    http = fake()
    b = um_box(store, http)

    # when
    text, err = b.call('umami', args)

    # then
    assert err and message in text and http.calls == [] and b.used['umami'] == spent


def test_event_data_needs_an_event_and_a_listed_property_from_the_instance(store: Store):
    # given
    http = fake()
    no_event = um_box(store, http, instances={'main': dict(VALUES, event='')})
    no_props = um_box(store, http, instances={'main': dict(VALUES, properties=' , ')})

    # when
    missing = call(no_event, op='event_data')
    named = call(no_event, op='event_data', event='signup')[0]
    closed = call(no_props, op='event_data', event='signup')

    # then
    assert missing == ('event is required for op event_data: this instance names no event', True)
    assert named['event'] == 'signup' and named['rows'][0]['type'] == 'TRIAL'
    assert closed == ('op event_data is off: this instance lists no event properties', True)
    assert [c[1].split('?')[0] for c in http.calls] == [LOGIN, f'{BASE}/event-data/values']


def test_refusals_an_unreachable_umami_and_the_run_cap(store: Store):
    # given
    denied = um_box(store, fake({('GET', f'{BASE}/stats'): HttpError(401, f'{BASE}/stats')}))
    down = um_box(store, fake({('GET', f'{BASE}/stats'): Unreachable(f'{BASE}/stats unreachable (timed out) key={API_KEY}')}))
    b = um_box(store, fake())

    # when
    answers = [call(b, op='active')[1] for _ in range(umami.PLUGIN.tools[0].per_run + 1)]

    # then
    assert call(denied) == (kit.refusal(401, 'account'), True)
    assert kit.refusal(401, 'account') == ('Umami refused the website (HTTP 401): the account is invalid or lacks view access to the website; '
                                           'its user must own the website, be an admin, or be in the team that holds it')
    assert kit.refusal(403, 'API key').startswith('Umami refused the website (HTTP 403): the API key lacks view access to the website; ')
    assert call(down) == ('Umami unreachable on stats', True)
    assert answers == [False] * 8 + [True]


def test_the_app_hands_its_umami_instance_to_the_investigator(store: Store, tmp_path):
    # given
    http = fake()

    # when
    with app_with(store, tmp_path, http) as c:
        tools = c.app.state.investigator.sources.plugin_tools
        offered = [d for d in tools.definitions() if d['name'] == 'umami']
        active = tools.call('umami', {'op': 'active'}, TOOL_NOW)

    # then
    assert offered and tools.cap('umami') == 8 and tools.scan('umami')
    assert active == {'op': 'active', 'visitors': 37}
    assert PASSWORD not in json.dumps(offered) and TOKEN not in json.dumps(offered)


def test_an_earlier_count_past_the_shown_rows_stays_a_number_and_is_null_only_when_that_list_was_cut(store: Store):
    # given
    def known_values(query, body):
        if now_window(query):
            return [{'value': 'plan-0', 'total': 100}, {'value': 'plan-new', 'total': 50}, {'value': 'plan-low', 'total': 10}]
        return [{'value': f'plan-{i}', 'total': 200 - i} for i in range(1, 4)] + [{'value': 'plan-0', 'total': 90}]

    def crowded(query, body):
        return [{'value': 'plan-new', 'total': 50}] if now_window(query) else [{'value': f'plan-{i}', 'total': 500 - i} for i in range(200)]

    b = um_box(store, fake({('GET', f'{BASE}/event-data/values'): known_values}))
    full = um_box(store, fake({('GET', f'{BASE}/event-data/values'): crowded}))
    http = fake()

    # when
    known, err = call(b, op='event_data', property='plan', limit=2, compare='week')
    unknown = call(full, op='event_data', property='plan', limit=2, compare='week')[0]
    call(um_box(store, http), op='metrics', type='path', compare='day', limit=1)

    # then
    assert not err and known['rows'] == [{'plan': 'plan-0', 'events': 100, 'week before': 90}, {'plan': 'plan-new', 'events': 50, 'week before': 0}]
    assert 'note' not in known
    assert unknown['rows'] == [{'plan': 'plan-new', 'events': 50, 'week before': None}] and 'outside the top 100' in unknown['note']
    assert (query_of(http, f'{BASE}/metrics')['limit'], query_of(http, f'{BASE}/metrics', 1)['limit']) == (['1'], ['100'])


def window_api(pageviews: dict, types: dict, url: str = URL, site: str = SITE, domain: str = DOMAIN) -> FakeHttp:
    b = base(url, site)

    def window_stats(query, body):
        return {'pageviews': pageviews[query['endAt'][0]], 'visitors': 1, 'visits': 1, 'bounces': 0, 'totaltime': 0}

    def window_values(query, body):
        return [{'value': t, 'total': n} for t, n in types[query['endAt'][0]].items()]
    return api({('GET', b): website(domain), ('GET', f'{b}/stats'): window_stats, ('GET', f'{b}/event-data/values'): window_values}, url)


def ends(now: float = NOW) -> tuple[str, str, str]:
    at = int(now * 1000)
    return str(at), str(at - DAY_MS), str(at - 7 * DAY_MS)


def collapsed(url: str = URL, site: str = SITE) -> FakeHttp:
    e0, e1, e7 = ends()
    return window_api({e0: 200, e1: 1000, e7: 900}, {e0: {'TRIAL': 10, 'UPGRADE': 5}, e1: {'TRIAL': 200, 'UPGRADE': 90}, e7: {'TRIAL': 180, 'UPGRADE': 100}}, url, site)


def calm(url: str = URL, site: str = SITE) -> FakeHttp:
    e0, e1, e7 = ends()
    return window_api({e0: 1000, e1: 1000, e7: 900}, {e0: {'TRIAL': 200}, e1: {'TRIAL': 200}, e7: {'TRIAL': 190}}, url, site)


def silent(url: str = URL) -> FakeHttp:
    e0, e1, e7 = ends()
    return window_api({e0: 0, e1: 0, e7: 0}, {e0: {}, e1: {}, e7: {}}, url)


@dataclass
class Ctx:
    transport: object
    config: dict = field(default_factory=lambda: dict(VALUES))
    now: float = NOW
    name: str = 'main'
    rules_given: dict = field(default_factory=dict)
    deploys_given: list = field(default_factory=list)
    asked: list = field(default_factory=list)

    @property
    def instance(self) -> InstanceInfo:
        return InstanceInfo(self.name, True, None, self.config)

    def deploys(self, since: float) -> list[dict]:
        self.asked.append(since)
        return [d for d in self.deploys_given if d['at'] >= iso(since)]

    def http(self):
        return self.transport

    def rules(self, set_id: str) -> dict:
        assert set_id == 'rules'
        return {**check.RULES, **self.rules_given}


def test_umami_opens_a_crit_card_when_events_collapse_and_a_warn_per_value_and_for_pageviews():
    # given
    http = collapsed()

    # when
    out = umami.run(Ctx(http)).result

    # then
    assert out.ok and out.name == 'umami' and out.status == 'ok' and out.ran == ('umami', 'umami:pageviews', 'umami:events', 'umami:access:website')
    cards = {c['key']: c for c in out.found}
    assert {c['sig']: c['ranKey'] for c in out.found} == {'umami-redirect': 'umami:events', 'umami-pageviews': 'umami:pageviews'}
    assert cards['umami-redirect|-/shop.example.com|checkout']['sev'] == 'crit'
    assert (cards['umami-redirect|-/shop.example.com|checkout']['now'], cards['umami-redirect|-/shop.example.com|checkout']['d1']) == (15, 290)
    assert cards['umami-redirect|-/shop.example.com|checkout:TRIAL']['sev'] == 'warn' and 'checkout:UPGRADE' in ' '.join(cards)
    assert cards['umami-pageviews|-/shop.example.com|pageviews']['now'] == 200
    assert all(c['siteId'] == SITE and c['src'] == 'umami' and 'cluster' not in c and 'plugin' not in c for c in out.found)
    info = InstanceInfo('main', True, None, dict(VALUES))
    assert umami.links(cards['umami-pageviews|-/shop.example.com|pageviews'], info) == [{'label': 'Umami website', 'url': f'{URL}/websites/{SITE}'}]


def test_umami_card_texts_are_neutral_and_name_the_event_and_property():
    # given
    http = collapsed()

    # when
    cards = {c['key']: c for c in umami.run(Ctx(http)).result.found}

    # then
    crit = cards['umami-redirect|-/shop.example.com|checkout']
    per = cards['umami-redirect|-/shop.example.com|checkout:TRIAL']
    views = cards['umami-pageviews|-/shop.example.com|pageviews']
    assert crit['what'].startswith('Umami counted 15 checkout events in the hour to ') and 'By type: TRIAL 10 (1d 200 / 7d 180), UPGRADE 5 (1d 90 / 7d 100).' in crit['what']
    assert crit['short'] == 'CRIT shop.example.com checkout events 15 in the last hour (1d 290 / 7d 280)' and crit['label'] == 'checkout events down'
    assert per['what'].startswith('Umami counted 10 checkout events with type TRIAL in the hour to ') and per['label'] == 'TRIAL checkout events down'
    assert views['short'] == 'WARN shop.example.com Umami pageviews 200 in the last hour (1d 1000 / 7d 900)'
    assert all(' UTC' in c['what'] for c in cards.values())


def test_umami_stays_quiet_without_a_week_of_data_or_under_the_floors():
    # given
    e0, e1, e7 = ends()
    young = window_api({e0: 10, e1: 1000, e7: 0}, {e0: {'TRIAL': 1}, e1: {'TRIAL': 200}, e7: {}})
    small = window_api({e0: 100, e1: 400, e7: 400}, {e0: {'TRIAL': 5}, e1: {'TRIAL': 20}, e7: {'TRIAL': 25}})

    # when
    found = [umami.run(Ctx(h)).result.found for h in (young, small)]

    # then
    assert found == [[], []]


def test_a_failing_umami_reports_the_error_and_leaves_its_cards_alone():
    # given
    http = api({('GET', BASE): website(), ('GET', f'{BASE}/stats'): HttpError(500, f'{BASE}/stats')})

    # when
    out = umami.run(Ctx(http)).result

    # then
    assert not out.ok and out.status == 'error: Umami HTTP 500 on stats' and out.found == []


def publish(to: str, at: float, name: str | None = 'Ads fix', changes: str | None = '1 tags changed (Ads conversion)', kind: str = 'gtm') -> dict:
    return {'id': f'gtm-GTM-ABCD12-{to}', 'ns': 'gtm', 'svc': 'GTM-ABCD12', 'at': iso(at), 'fromImage': f'v{int(to) - 1}', 'toImage': f'v{to}', 'kind': kind,
            'gtm': {'name': name, 'description': None, 'changes': changes, 'storedAt': None}}


def test_a_drop_card_names_the_gtm_publish_of_the_last_two_days():
    # given
    rows = [publish('130', NOW - 3 * DAY), publish('133', NOW - 30 * 3600, name=None, changes=None), publish('134', NOW - 3 * 3600), publish('135', NOW - 3600, kind='other')]
    ctx = Ctx(collapsed(), deploys_given=rows)

    # when
    found = umami.run(ctx).result.found

    # then
    assert ctx.asked == [NOW - 48 * 3600] and len(found) == 4
    for card in found:
        assert card['gtmPublish'] == {'container': 'GTM-ABCD12', 'version': '134', 'name': 'Ads fix', 'at': iso(NOW - 3 * 3600), 'changes': '1 tags changed (Ads conversion)'}
        assert ' GTM GTM-ABCD12 v134 "Ads fix" went live around ' in card['what'] and card['what'].endswith(': 1 tags changed (Ads conversion).')


def test_a_drop_card_without_a_recent_gtm_publish_is_left_as_it_was():
    # given
    plain = umami.run(Ctx(collapsed())).result.found
    old = umami.run(Ctx(collapsed(), deploys_given=[publish('130', NOW - 3 * DAY), publish('135', NOW - 3600, kind='other')])).result.found

    # then
    assert old == plain and all('gtmPublish' not in c and ' GTM ' not in c['what'] for c in old)


DROP_LEVELS = (0, 600)


def test_an_umami_pageview_card_that_climbs_back_while_still_low_is_not_pushed_again_as_escalated():
    # given
    state_doc = {'initialized': True, 'anomalies': []}
    first, later = (check.evaluate({'now': (low, {}), '1d': (2000, {}), '7d': (2000, {})}, NOW, check.RULES, DOMAIN, SITE, 'checkout', 'type') for low in DROP_LEVELS)
    _, pushed, _ = merge(state_doc, copy.deepcopy(first), {}, NOW)

    # when
    events, push, _ = merge(state_doc, copy.deepcopy(later), {}, NOW + 3600)

    # then
    assert len(first) == len(later) == 1 and [a['key'] for a in pushed] == [first[0]['key']]
    assert [e['ev'] for e in events] == [] and push == []


def test_an_umami_with_nothing_counted_is_ok_with_no_data_and_closes_no_card(store: Store, tmp_path):
    # given
    busy = collapsed()
    quiet_api = silent()

    # when
    with app_with(store, tmp_path, busy) as c:
        c.app.state.sweeper.run('umami', 'umami', NOW)
        before = by_key(store.load())
        busy.routes.update(quiet_api.routes)
        c.app.state.sweeper.run('umami', 'umami', NOW)
    after = store.load()
    quiet = umami.run(Ctx(quiet_api)).result

    # then
    assert before['umami-pageviews|-/shop.example.com|pageviews']['status'] == 'open'
    assert (quiet.ok, quiet.status, quiet.ran, quiet.found) == (True, f'ok{DOT}no data', ('umami:no-data', 'umami:access:website'), [])
    assert after['sources']['umami'] == f'ok{DOT}no data'
    assert by_key(after)['umami-pageviews|-/shop.example.com|pageviews']['status'] == 'open', 'a pass that read nothing closes no card (P5-C82)'


def test_an_empty_part_is_named_in_the_status_and_the_pass_still_clears():
    # given
    e0, e1, e7 = ends()
    no_events = window_api({e0: 1000, e1: 1000, e7: 900}, {e0: {}, e1: {}, e7: {}})

    # when
    out = umami.run(Ctx(no_events)).result

    # then
    assert (out.ok, out.status, out.ran) == (True, f'ok{DOT}no data for checkout events', ('umami:pageviews', 'umami:access:website'))


def test_without_an_event_only_pageviews_are_watched_and_the_event_cards_close(store: Store, tmp_path):
    # given
    http = collapsed()

    # when
    with app_with(store, tmp_path, http) as c:
        c.app.state.sweeper.run('umami', 'umami', NOW)
        before = {k for k, a in by_key(store.load()).items() if a['status'] == 'open'}
        c.app.state.runtime.instances.update('umami', 'main', {'event': ''}, ALICE)
        c.app.state.sweeper.run('umami', 'umami', NOW)
    after = by_key(store.load())
    alone = umami.run(Ctx(http, dict(VALUES, event=''))).result

    # then
    assert 'umami-redirect|-/shop.example.com|checkout' in before
    assert after['umami-redirect|-/shop.example.com|checkout']['status'] == 'cleared', 'a switched-off part closes its cards (P5-C84)'
    assert after['umami-pageviews|-/shop.example.com|pageviews']['status'] == 'open'
    assert [a['sig'] for a in alone.found] == ['umami-pageviews'] and alone.ran == ('umami', 'umami:pageviews', 'umami:events', 'umami:access:website')
    assert not http.hits(f'{BASE}/event-data/values')[3:]


def test_a_switched_off_event_closes_its_cards_even_on_a_pass_with_no_pageviews(store: Store, tmp_path):
    # given
    http = collapsed()

    # when
    with app_with(store, tmp_path, http) as c:
        c.app.state.sweeper.run('umami', 'umami', NOW)
        c.app.state.runtime.instances.update('umami', 'main', {'event': ''}, ALICE)
        http.routes.update(silent().routes)
        c.app.state.sweeper.run('umami', 'umami', NOW)
    after = store.load()
    quiet = umami.run(Ctx(silent(), dict(VALUES, event=''))).result

    # then
    assert (quiet.status, quiet.ran) == (f'ok{DOT}no data', ('umami:no-data', 'umami:events', 'umami:access:website'))
    assert after['sources']['umami'] == f'ok{DOT}no data'
    assert {k: a['status'] for k, a in by_key(after).items()} == {'umami-redirect|-/shop.example.com|checkout': 'cleared', 'umami-redirect|-/shop.example.com|checkout:TRIAL': 'cleared',
                                                                  'umami-redirect|-/shop.example.com|checkout:UPGRADE': 'cleared',
                                                                  'umami-pageviews|-/shop.example.com|pageviews': 'open'}


def test_old_cards_without_a_part_key_close_on_a_pass_that_read_every_part(store: Store, tmp_path):
    # given
    old = {'key': 'umami-redirect|-/shop.example.com|checkout:GONE', 'src': 'umami', 'sig': 'umami-redirect', 'ns': '-', 'svc': DOMAIN, 'sub': 'checkout:GONE', 'sev': 'warn',
           'status': 'open', 'now': 1, 'd1': 50, 'd7': 50, 'unit': '', 'label': 'GONE checkout events down', 'what': 'old card', 'short': 'WARN old card', 'slack': 50,
           'firstSeen': iso(NOW - 3600), 'lastSeen': iso(NOW - 900), 'peak': 1, 'pushedAt': None, 'pushedSev': None, 'pushedPeak': None}
    store.save(state(old), None)

    # when
    with app_with(store, tmp_path, calm()) as c:
        c.app.state.sweeper.run('umami', 'umami', NOW)
    found = by_key(store.load())

    # then
    assert found['umami-redirect|-/shop.example.com|checkout:GONE']['status'] == 'cleared', 'the bare key closes cards stored before the part keys (P5-C120)'


def refused_api(code: int, where: str = '/stats') -> FakeHttp:
    http = collapsed()
    http.routes[('GET', BASE + where if where else BASE)] = HttpError(code, BASE + where)
    return http


def test_a_refused_read_is_never_no_data_but_one_access_card_and_the_other_cards_stay_open(store: Store, tmp_path):
    # given
    http = collapsed()
    refused = refused_api(401)

    # when
    with app_with(store, tmp_path, http) as c:
        c.app.state.sweeper.run('umami', 'umami', NOW)
        before = {k for k, a in by_key(store.load()).items() if a['status'] == 'open'}
        http.routes.update(refused.routes)
        c.app.state.sweeper.run('umami', 'umami', NOW)
        during = store.load()
        http.routes.update(collapsed().routes)
        c.app.state.sweeper.run('umami', 'umami', NOW)
        after = by_key(store.load())

    # then
    access = by_key(during)['umami-access|-/shop.example.com|website']
    assert during['sources']['umami'] == f'ok{DOT}no access to the website'
    assert (access['status'], access['sev'], access['ranKey']) == ('open', 'warn', 'umami:access:website')
    assert access['label'] == 'Umami cannot read the website: the account is invalid or lacks view access to the website'
    assert 'HTTP 401' in access['what'] and 'own the website, be an admin, or be in the team' in access['what']
    assert {k for k, a in by_key(during).items() if a['status'] == 'open'} == before | {access['key']}, 'the refused cards stay open (P5-C111)'
    assert after[access['key']]['status'] == 'cleared' and {k for k, a in after.items() if a['status'] == 'open'} == before


def test_a_quiet_pass_after_access_returns_closes_the_access_card(store: Store, tmp_path):
    # given
    http = refused_api(403)

    # when
    with app_with(store, tmp_path, http) as c:
        c.app.state.sweeper.run('umami', 'umami', NOW)
        during = by_key(store.load())
        http.routes.update(silent().routes)
        c.app.state.sweeper.run('umami', 'umami', NOW)
        after = store.load()

    # then
    assert during['umami-access|-/shop.example.com|website']['status'] == 'open'
    assert during['umami-access|-/shop.example.com|website']['label'] == 'Umami cannot read the website: the account lacks view access to the website'
    assert after['sources']['umami'] == f'ok{DOT}no data'
    assert by_key(after)['umami-access|-/shop.example.com|website']['status'] == 'cleared', 'a quiet pass read the website, so the access card closes (P5-C111, P5-C88)'


def test_a_refusal_on_any_read_keeps_every_card_and_names_the_instance_when_the_domain_is_refused():
    # given
    events = refused_api(403, '/event-data/values')
    domain = refused_api(401, '')

    # when
    late = umami.run(Ctx(events)).result
    first = umami.run(Ctx(domain)).result
    named = umami.run(Ctx(refused_api(401), dict(VALUES, site='shop'))).result

    # then
    assert (late.ok, late.status, late.ran) == (True, f'ok{DOT}no access to the website', ('umami:access:website',))
    assert [(a['key'], a['ranKey']) for a in late.found] == [('umami-access|-/shop.example.com|website', 'umami:access:website')]
    assert [a['key'] for a in first.found] == ['umami-access|-/main|website'] and 'HTTP 401' in first.found[0]['short'] and first.found[0]['siteId'] == SITE
    assert [a['key'] for a in named.found] == ['umami-access|-/shop|website']


def test_top_paths_says_when_the_key_cannot_read_the_website():
    # given
    http = api({('GET', f'{BASE}/metrics'): HttpError(403, f'{BASE}/metrics')})

    # when
    with pytest.raises(kit.UmamiError) as refused:
        kit.top_paths(InstanceInfo('main', True, None, dict(VALUES)), http, 7, 200)

    # then
    assert (refused.value.refused, refused.value.message) == (403, kit.refusal(403, 'account'))


def test_a_pass_that_outruns_its_budget_stops_with_a_fixed_reason(monkeypatch):
    # given
    http = calm()
    timeouts: list[float] = []

    def slow(method, url, headers, data, timeout):
        timeouts.append(timeout)
        time.sleep(0.15)
        return http(method, url, headers, data, timeout)

    monkeypatch.setattr(check, 'BUDGET_S', 0.4)

    # when
    started = time.monotonic()
    sweep = umami.run(Ctx(slow))
    took = time.monotonic() - started

    # then
    assert not sweep.result.ok and sweep.result.status.startswith('error: Umami reads stopped after ') and sweep.result.status.endswith(' calls: the 0.4 s budget is spent')
    assert took < 1.5 and all(t <= 0.4 for t in timeouts)
    assert check.BUDGET_S < 240


def test_cards_name_the_site_field_or_else_the_website_domain():
    # given
    http = collapsed()

    # when
    named = umami.run(Ctx(http, dict(VALUES, site='shop'))).result.found
    asked = len(http.hits(BASE))
    fallback = umami.run(Ctx(http)).result.found
    nameless = umami.run(Ctx(FakeHttp({**collapsed().routes, ('GET', BASE): {'id': SITE, 'domain': 'not a host'}}))).result

    # then
    assert {a['svc'] for a in named} == {'shop'} and asked == 1, 'the website record is read every pass, so a website id that does not exist fails'
    assert {a['svc'] for a in fallback} == {DOMAIN} and len(http.hits(BASE)) == 2
    assert (nameless.ok, nameless.status) == (False, f'error: {kit.NO_DOMAIN}')


def test_a_bad_event_or_property_field_fails_the_pass_with_a_fixed_reason():
    # given
    http = calm()

    # when
    event = umami.run(Ctx(http, dict(VALUES, event='bad event!'))).result
    props = umami.run(Ctx(http, dict(VALUES, properties='type, bad one'))).result
    site = umami.run(Ctx(http, dict(VALUES, **{'website-id': 'shop'}))).result

    # then
    assert (event.ok, event.status) == (False, f'error: {kit.BAD_EVENT}')
    assert (props.ok, props.status) == (False, f'error: {kit.BAD_PROPERTIES}')
    assert (site.ok, site.status) == (False, f'error: {kit.BAD_WEBSITE}')
    assert http.calls == []


def test_links_open_the_website_on_the_ui_url_and_skip_other_cards():
    # given
    plain = InstanceInfo('main', True, None, dict(VALUES))
    with_ui = InstanceInfo('main', True, None, dict(VALUES, **{'ui-url': UI + '/'}))
    old = {'src': 'umami', 'sig': 'umami-pageviews', 'siteId': EAST_SITE}

    # when
    got = [umami.links({'src': 'umami', 'sig': 'umami-pageviews'}, plain), umami.links(old, with_ui), umami.links({'src': 'umami', 'siteId': '../x'}, plain)]

    # then
    assert got == [[{'label': 'Umami website', 'url': f'{URL}/websites/{SITE}'}], [{'label': 'Umami website', 'url': f'{UI}/websites/{EAST_SITE}'}],
                   [{'label': 'Umami website', 'url': f'{URL}/websites/{SITE}'}]]
    assert umami.links({'src': 'prom', 'sig': 'err'}, plain) is None


def app_with(store, tmp_path, http, instances: dict | None = None):
    sealing = master_key(store.db, settings())
    for i, (name, given) in enumerate(({'main': VALUES} if instances is None else instances).items()):
        add_instance(store, umami.PLUGIN, name, sealer_key=sealing, primary=i == 0, **given)
    return TestClient(create_app(settings(), store, registry=Registry.of(umami.PLUGIN), transport=http, kube=Kube(root=tmp_path)))


def by_key(doc: dict) -> dict:
    return {a['key']: a for a in doc['anomalies']}


def test_the_app_runs_the_umami_check_once_an_instance_serves(store: Store, tmp_path):
    # given
    http = collapsed()

    # when
    with app_with(store, tmp_path, http, instances={'main': dict(VALUES, **{'ui-url': UI})}) as c:
        jobs = {j.name: j for j in c.app.state.scheduler.jobs}
        tiles = {p['id']: p for p in c.get('/api/server').json()['plugins']}
        c.app.state.sweeper.run('umami', 'umami', NOW)
    doc = store.load()
    card = by_key(doc)['umami-redirect|-/shop.example.com|checkout']

    # then
    assert (jobs['umami.umami'].period, jobs['umami.umami'].offset) == (900, 420)
    assert tiles['umami']['passes'] == ['umami'] and tiles['umami']['pages'] == []
    assert doc['sources']['umami'] == 'ok' and card['status'] == 'open' and card['sev'] == 'crit'
    assert card['links'] == [{'label': 'Umami website', 'url': f'{UI}/websites/{SITE}'}]
    assert [(r['name'], r['ok']) for r in store.check_runs_since('2000-01-01T00:00:00Z')] == [('umami', True)]


def test_without_an_instance_no_umami_job_is_registered(store: Store, tmp_path):
    # when
    with app_with(store, tmp_path, fake(), instances={}) as c:
        names = [j.name for j in c.app.state.scheduler.jobs]

    # then
    assert 'umami.umami' not in names


def test_umami_identity_and_manifest():
    # given
    registry = Registry.load({}, entry_points=False, core_tools=CORE_TOOLS, core_knowledge=prompt.core_anchors())

    # when
    got = registry.get('umami')
    manifest = describe(umami.PLUGIN, icon=True)
    fields = {f.key: f for f in umami.PLUGIN.fields}
    probe, tool, rules = umami.PLUGIN.checks[0], umami.PLUGIN.tools[0], umami.PLUGIN.rules[0]

    # then
    assert 'skopax.plugins.umami' in BUILTIN and got is not None and got.state == 'ok' and got.plugin is umami.PLUGIN
    assert json.loads(json.dumps(manifest, allow_nan=False)) == manifest
    assert (manifest['id'], manifest['name'], manifest['version'], manifest['category'], manifest['author'], manifest['licence'], manifest['core'], manifest['edition']) == (
        'umami', 'Umami', '0.1.0', 'marketing', 'Skopax', 'Apache-2.0', '>=0.1,<0.5', 'community')
    assert manifest['capabilities'] == {'hosts': ['@url', '@ui-url'], 'secrets': ['api-key', 'token', 'password'], 'agentReads': False} and manifest['uses'] == []
    assert [(f.key, f.kind, f.required, f.ui) for f in fields.values()] == [('url', 'url', True, True), ('ui-url', 'url', False, True), ('api-key', 'secret', False, False),
                                                                             ('token', 'secret', False, False), ('username', 'text', False, False), ('password', 'secret', False, False),
                                                                             ('website-id', 'text', True, False), ('site', 'text', False, False), ('event', 'text', False, False),
                                                                             ('properties', 'text', False, False), ('ca', 'pem', False, False), ('ca-pinned', 'bool', False, False)]
    assert 'https://api.umami.is/v1' in fields['url'].help and 'Umami Cloud' in fields['api-key'].help and 'own Umami server' in fields['username'].help
    assert 'own Umami server' in fields['token'].help and 'not both' in fields['token'].help and 'not both' in fields['api-key'].help
    assert 'dashboard' in fields['ui-url'].help
    assert (probe.id, probe.period, probe.offset, probe.ran, probe.sections, probe.tool, probe.once) == ('umami', 900, 420, ('umami',), (), 'umami', False)
    assert (rules.id, rules.label, set(rules.help) == set(rules.defaults)) == ('rules', 'Umami', True)
    assert (tool.name, tool.kind, tool.per_run, tool.scan, tool.instance_input, tool.reach) == ('umami', 'read', 8, True, 'instance', ())
    assert manifest['panels'] == [] and registry.icon('umami').startswith('<svg')
    assert [k.id for k in umami.PLUGIN.knowledge] == ['umami'] and len(umami.PLUGIN.knowledge[0].text.splitlines()) <= 40
    assert umami.PLUGIN.client is None and umami.PLUGIN.max_instances is None and umami.PLUGIN.env_instances is None


def test_umami_defaults_are_neutral():
    # then
    assert {f.key: f.default for f in umami.PLUGIN.fields} == {'url': '', 'ui-url': '', 'api-key': '', 'token': '', 'username': '', 'password': '', 'website-id': '', 'site': '',
                                                               'event': '', 'properties': 'type', 'ca': '', 'ca-pinned': 'false'}
    assert {s.key: s.default for s in umami.PLUGIN.settings} == {}
    assert dict(umami.PLUGIN.rules[0].defaults) == {'redirect_warn': 0.5, 'redirect_crit': 0.2, 'redirect_floor': 50, 'redirect_crit_floor': 100, 'type_warn': 0.4, 'type_floor': 30,
                                                    'pageview_warn': 0.5, 'pageview_floor': 500}


def test_umami_connection_test_reports_ok_and_failures(store):
    # given
    cases = {'ok': fake(), 'refused': api({('GET', BASE): HttpError(401, BASE)}), 'forbidden': api({('GET', BASE): HttpError(403, BASE)}),
             'unreachable': api({('GET', BASE): remote.Unreachable('down')}), 'failing': api({('GET', BASE): HttpError(500, BASE)}),
             'nameless': api({('GET', BASE): {'id': SITE, 'domain': ''}}), 'no-login': FakeHttp({('POST', LOGIN): HttpError(401, LOGIN)}),
             'login-forbidden': FakeHttp({('POST', LOGIN): HttpError(403, LOGIN)}), 'no-token': FakeHttp({('POST', LOGIN): {'user': {'id': 'u1'}}})}

    # when
    got = {}
    for name, http in cases.items():
        kit.LOGINS.clear()
        got[name] = tool_runtime(store, http, instances={}).connection_test(umami.PLUGIN, None, dict(VALUES), 5, NOW)
    bad = FakeHttp()
    got['bad-site'] = tool_runtime(store, bad, instances={}).connection_test(umami.PLUGIN, None, dict(VALUES, **{'website-id': DOMAIN}), 5, NOW)

    # then
    assert (got['ok']['ok'], got['ok']['summary']) == (True, f'Umami{DOT}{DOMAIN}')
    assert (got['refused']['ok'], got['refused']['summary']) == (False, kit.refusal(401, 'account')), 'a read 401 right after a good login is two-sided (P5-C127)'
    assert (got['forbidden']['ok'], got['forbidden']['summary']) == (False, kit.refusal(403, 'account'))
    assert (got['unreachable']['ok'], got['unreachable']['summary']) == (False, msg('connection.unreachable'))
    assert (got['failing']['ok'], got['failing']['summary']) == (False, msg('connection.status', status=500))
    assert (got['nameless']['ok'], got['nameless']['summary']) == (False, kit.NO_DOMAIN)
    assert (got['no-login']['ok'], got['no-login']['summary']) == (False, msg('connection.rejected', status=401)), 'a refused login stays a credentials failure'
    assert (got['login-forbidden']['ok'], got['login-forbidden']['summary']) == (False, msg('connection.rejected', status=403)), 'a refused login never says the credentials were accepted'
    assert (got['no-token']['ok'], got['no-token']['summary']) == (False, kit.NO_TOKEN)
    assert (got['bad-site']['ok'], got['bad-site']['summary']) == (False, kit.BAD_WEBSITE) and bad.calls == []
    asked = cases['ok'].hits(BASE)
    assert len(asked) == 1 and asked[0][2]['Authorization'] == f'Bearer {TOKEN}'
    assert PASSWORD not in json.dumps(got) and TOKEN not in json.dumps(got)


def test_test_connection_names_the_site_field_without_needing_a_domain(store):
    # given
    http = api({('GET', BASE): {'id': SITE, 'domain': 'localhost:3000'}})

    # when
    named = tool_runtime(store, http, instances={}).connection_test(umami.PLUGIN, None, dict(VALUES, site='shop'), 5, NOW)
    plain = tool_runtime(store, http, instances={}).connection_test(umami.PLUGIN, None, dict(VALUES), 5, NOW)

    # then
    assert (named['ok'], named['summary']) == (True, f'Umami{DOT}shop')
    assert (plain['ok'], plain['summary']) == (False, kit.NO_DOMAIN)


@pytest.mark.parametrize('given, said', [
    ({'username': USER}, 'the password is missing: the login form needs the username and the password'),
    ({'password': PASSWORD}, 'the username is missing: the login form needs the username and the password'),
    ({}, 'set the API key (Umami Cloud), the token (your own Umami server), or the username and password (your own Umami server)'),
    ({'api-key': API_KEY, 'token': STATIC_TOKEN}, 'set either the API key (Umami Cloud) or the token (your own Umami server), not both'),
])
def test_without_one_complete_credential_test_pass_and_tool_say_what_is_wrong_before_any_call(store, given, said):
    # given
    config = {k: v for k, v in VALUES.items() if k not in ('username', 'password')} | given
    http = fake()

    # when
    tested = tool_runtime(store, http, instances={}).connection_test(umami.PLUGIN, None, dict(config), 5, NOW)
    swept = umami.run(Ctx(http, dict(config))).result
    asked = call(um_box(store, http, instances={'main': config}), op='active')

    # then
    assert (tested['ok'], tested['summary']) == (False, said)
    assert (swept.ok, swept.status) == (False, f'error: {said}')
    assert asked == (said, True) and http.calls == []
    assert API_KEY not in json.dumps(tested) and STATIC_TOKEN not in json.dumps(tested)


def test_the_token_form_reads_api_with_its_bearer_and_never_logs_in(store):
    # given
    http = fake()
    tested_api = api({('GET', BASE): website()})
    refused = api({('GET', BASE): HttpError(401, BASE)})
    forbidden = api({('GET', BASE): HttpError(403, BASE)})

    # when
    active = call(um_box(store, http, instances={'main': TOKEN_VALUES}), op='active')[0]
    with_login = call(um_box(store, http, instances={'main': dict(TOKEN_VALUES, username=USER, password=PASSWORD)}), op='active')[0]
    tested = [tool_runtime(store, h, instances={}).connection_test(umami.PLUGIN, None, dict(TOKEN_VALUES), 5, NOW) for h in (tested_api, refused, forbidden)]
    card = umami.run(Ctx(refused_api(401), dict(TOKEN_VALUES))).result.found[0]
    targets = [t['name'] for t in tool_runtime(store, http, instances={'main': TOKEN_VALUES}).probe_targets()]

    # then
    assert active == with_login == {'op': 'active', 'visitors': 37}
    assert [c[1] for c in http.calls] == [f'{BASE}/active', f'{BASE}/active'] and not tested_api.hits(LOGIN)
    assert all(c[2]['Authorization'] == f'Bearer {STATIC_TOKEN}' and 'x-umami-api-key' not in c[2] for c in http.calls)
    assert [(t['ok'], t['summary']) for t in tested] == [(True, f'Umami{DOT}{DOMAIN}'), (False, msg('connection.rejected', status=401)), (False, kit.refusal(403, 'token'))]
    assert card['label'] == 'Umami cannot read the website: the token is invalid or lacks view access to the website'
    assert targets == ['umami-main'] and STATIC_TOKEN not in json.dumps(tested)


class Rotating:
    def __init__(self, inner: FakeHttp, deny: bool = False):
        self.inner = inner
        self.deny = deny
        self.issued = 0
        self.valid = ''

    def __call__(self, method, url, headers, data, timeout):
        if url == LOGIN:
            self.issued += 1
            self.valid = f'login-token-{self.issued}'
            return Reply({'token': self.valid}, {})
        if self.deny or headers.get('Authorization') != f'Bearer {self.valid}':
            raise HttpError(401, url.split('?')[0])
        return self.inner(method, url, headers, data, timeout)


def test_the_login_token_is_kept_across_passes_tool_calls_and_test_until_a_401(store):
    # given
    server = Rotating(collapsed())

    # when
    first = umami.run(Ctx(server)).result
    second = umami.run(Ctx(server)).result
    tool = call(um_box(store, server), op='stats')[1]
    tested = tool_runtime(store, server, instances={}).connection_test(umami.PLUGIN, None, dict(VALUES), 5, NOW)
    kept = server.issued
    server.valid = 'restarted'
    third = umami.run(Ctx(server)).result

    # then
    assert first.ok and second.ok and not tool and tested['ok'] and kept == 1, 'two passes, a tool call and a Test make one login'
    assert server.issued == 2 and third.ok and third.status == 'ok' and len(third.found) == len(first.found), 'a 401 makes exactly one new login and one retry'


def test_a_refused_account_logs_in_once_more_and_then_raises_the_access_card():
    # given
    server = Rotating(collapsed(), deny=True)

    # when
    out = umami.run(Ctx(server)).result

    # then
    assert server.issued == 2 and out.ok and out.status == f'ok{DOT}no access to the website'
    assert [a['label'] for a in out.found] == ['Umami cannot read the website: the account is invalid or lacks view access to the website']


def test_test_connection_after_a_good_login_names_the_account_for_a_read_401(store):
    # given
    server = Rotating(collapsed(), deny=True)

    # when
    tested = tool_runtime(store, server, instances={}).connection_test(umami.PLUGIN, None, dict(VALUES), 5, NOW)

    # then
    assert (tested['ok'], tested['summary']) == (False, kit.refusal(401, 'account')) and server.issued == 2
    assert 'the account is invalid or lacks view access to the website' in tested['summary']


def test_the_login_cache_follows_the_credentials():
    # given
    server = Rotating(collapsed())
    umami.run(Ctx(server))

    # when
    umami.run(Ctx(server, dict(VALUES, password='another-password-55')))

    # then
    assert server.issued == 2 and len(kit.LOGINS) <= kit.LOGINS_MAX
    assert not any(PASSWORD in str(k) or 'another-password-55' in str(k) for k in kit.LOGINS)


def cloud_api(over: dict | None = None) -> FakeHttp:
    e0, e1, e7 = ends()
    counts = {e0: 200, e1: 1000, e7: 900}

    def cloud_stats(query, body):
        return {'pageviews': counts.get(query['endAt'][0], 1200), 'visitors': 400, 'visits': 450, 'bounces': 120, 'totaltime': 9000}
    return FakeHttp({('GET', CLOUD_BASE): website(), ('GET', f'{CLOUD_BASE}/active'): {'visitors': 12}, ('GET', f'{CLOUD_BASE}/stats'): cloud_stats,
                     ('GET', f'{CLOUD_BASE}/event-data/values'): [], ('GET', f'{CLOUD_BASE}/metrics'): [{'x': '/pricing', 'y': 9}], **(over or {})})


def test_umami_cloud_reads_its_v1_root_with_the_api_key_header_and_no_login(store):
    # given
    http = cloud_api()
    info = InstanceInfo('main', True, None, dict(CLOUD_VALUES))

    # when
    active = call(um_box(store, http, instances={'main': CLOUD_VALUES}), op='active')[0]
    swept = umami.run(Ctx(http, dict(CLOUD_VALUES))).result
    paths = kit.top_paths(info, http, 7, 200)

    # then
    assert active == {'op': 'active', 'visitors': 12} and paths == ['/pricing']
    assert swept.ok and [a['key'] for a in swept.found] == ['umami-pageviews|-/shop.example.com|pageviews']
    assert {c[1].split('?')[0] for c in http.calls} == {CLOUD_BASE, f'{CLOUD_BASE}/active', f'{CLOUD_BASE}/stats', f'{CLOUD_BASE}/event-data/values', f'{CLOUD_BASE}/metrics'}
    assert all(c[2].get('x-umami-api-key') == API_KEY and 'Authorization' not in c[2] for c in http.calls)
    assert not [c for c in http.calls if '/api/' in c[1]]


def test_umami_cloud_test_connection_and_refusals_name_the_api_key(store):
    # given
    ok = cloud_api()
    refused = cloud_api({('GET', CLOUD_BASE): HttpError(401, CLOUD_BASE)})
    forbidden = cloud_api({('GET', CLOUD_BASE): HttpError(403, CLOUD_BASE)})

    # when
    tested = [tool_runtime(store, h, instances={}).connection_test(umami.PLUGIN, None, dict(CLOUD_VALUES), 5, NOW) for h in (ok, refused, forbidden)]
    card = umami.run(Ctx(cloud_api({('GET', f'{CLOUD_BASE}/stats'): HttpError(403, 'stats')}), dict(CLOUD_VALUES))).result.found[0]

    # then
    assert [(t['ok'], t['summary']) for t in tested] == [(True, f'Umami{DOT}{DOMAIN}'), (False, msg('connection.rejected', status=401)), (False, kit.refusal(403, 'API key'))]
    assert card['label'] == 'Umami cannot read the website: the API key lacks view access to the website'
    assert API_KEY not in json.dumps(tested)


def test_umami_cloud_has_no_probe_target_and_links_only_through_its_dashboard_address(store):
    # given
    runtime = tool_runtime(store, cloud_api(), instances={'main': CLOUD_VALUES, 'east': dict(VALUES, url=EAST_URL)})
    card = {'src': 'umami', 'sig': 'umami-pageviews', 'siteId': SITE}

    # when
    targets = [t['name'] for t in runtime.probe_targets()]
    bare = umami.links(card, InstanceInfo('main', True, None, dict(CLOUD_VALUES)))
    dashboard = umami.links(card, InstanceInfo('main', True, None, dict(CLOUD_VALUES, **{'ui-url': CLOUD_UI})))

    # then
    assert targets == ['umami-east']
    assert bare == [] and dashboard == [{'label': 'Umami website', 'url': f'{CLOUD_UI}/websites/{SITE}'}]


def test_an_api_key_picks_the_cloud_form_even_with_a_username_and_password_set(store):
    # given
    http = cloud_api()

    # when
    active = call(um_box(store, http, instances={'main': dict(CLOUD_VALUES, username=USER, password=PASSWORD)}), op='active')[0]

    # then
    assert active == {'op': 'active', 'visitors': 12} and [c[1] for c in http.calls] == [f'{CLOUD_BASE}/active']


def test_stats_take_numbers_or_value_objects_and_an_unknown_shape_fails_with_a_fixed_reason(store):
    # given
    e0, e1, e7 = ends()
    pageviews = {e0: {'value': 200, 'prev': 900}, e1: {'value': 1000, 'prev': 0}, e7: 900}
    boxed = api({('GET', BASE): website(), ('GET', f'{BASE}/stats'): lambda q, b: {'pageviews': pageviews[q['endAt'][0]], 'visitors': {'value': 3}},
                 ('GET', f'{BASE}/event-data/values'): []})
    odd = api({('GET', BASE): website(), ('GET', f'{BASE}/stats'): {'pageviews': [1, 2]}, ('GET', f'{BASE}/event-data/values'): []})

    # when
    read = umami.run(Ctx(boxed)).result
    broken = umami.run(Ctx(odd)).result
    tool = call(um_box(store, odd), op='stats')

    # then
    assert read.ok and [(a['sig'], a['now'], a['d1'], a['d7']) for a in read.found] == [('umami-pageviews', 200, 1000, 900)]
    assert (broken.ok, broken.status) == (False, f'error: {kit.BAD_SHAPE}') and tool == (kit.BAD_SHAPE, True)
    assert kit.BAD_SHAPE == 'Umami answered stats in an unknown shape'


def test_umami_second_instance_is_scoped(store: Store, tmp_path):
    # given
    http = calm()
    http.routes.update(collapsed(EAST_URL, EAST_SITE).routes)

    # when
    with app_with(store, tmp_path, http, instances={'main': VALUES, 'east': dict(VALUES, url=EAST_URL, **{'website-id': EAST_SITE})}) as c:
        c.app.state.sweeper.run('umami', 'umami', NOW)
    doc = store.load()
    east = [a for a in doc['anomalies'] if a.get('cluster') == 'east']

    # then
    assert east and {(a['cluster'], a['ranKey']) for a in east} == {('east', 'umami:events-east'), ('east', 'umami:pageviews-east')}
    assert doc['sources']['umami-east'] == 'ok' and doc['sources']['umami'] == 'ok'
    assert not [a for a in doc['anomalies'] if a.get('cluster') != 'east']
    assert {link['url'] for a in east for link in a['links']} == {f'{EAST_URL}/websites/{EAST_SITE}'}


def test_umami_rules_come_from_the_rule_set(store: Store, tmp_path):
    # given
    e0, e1, e7 = ends()
    http = window_api({e0: 600, e1: 1000, e7: 900}, {e0: {'TRIAL': 200}, e1: {'TRIAL': 200}, e7: {'TRIAL': 190}})

    # when
    with app_with(store, tmp_path, http) as c:
        c.app.state.sweeper.run('umami', 'umami', NOW)
        before = set(by_key(store.load()))
        c.app.state.runtime.live.set('umami.rules', '{"pageview_warn": 0.7}', ALICE)
        c.app.state.sweeper.run('umami', 'umami', NOW)
        after = by_key(store.load())

    # then
    assert 'umami-pageviews|-/shop.example.com|pageviews' not in before
    assert after['umami-pageviews|-/shop.example.com|pageviews']['status'] == 'open' and after['umami-pageviews|-/shop.example.com|pageviews']['sev'] == 'warn'


def test_umami_probe_target_is_the_heartbeat_of_each_instance(store):
    # given
    runtime = tool_runtime(store, fake(), instances={'main': VALUES, 'east': dict(VALUES, url=EAST_URL + '/')})

    # when
    targets = sorted(runtime.probe_targets(), key=lambda t: t['name'])

    # then
    assert [(t['name'], t['url'], t['instance']) for t in targets] == [('umami-east', f'{EAST_URL}/api/heartbeat', 'east'), ('umami-main', f'{URL}/api/heartbeat', 'main')]


def test_umami_custom_ca_reaches_the_transport_and_the_probe_target(store, monkeypatch):
    # given
    cert = pem()
    runtime = tool_runtime(store, None, instances={'main': dict(VALUES, ca=cert)})
    real, seen = ssl.create_default_context, []
    monkeypatch.setattr(ssl, 'create_default_context', lambda *a, **k: seen.append(k.get('cadata')) or real(*a, **k))

    # when
    ctx = runtime.context(umami.PLUGIN, runtime.instances.get('umami', 'main'), NOW)
    sender = ctx.http()
    targets = runtime.probe_targets()

    # then
    assert seen == [cert] and sender is not remote.send
    assert [(t['name'], t.get('pem')) for t in targets] == [('umami-main', cert)]


def test_umami_tool_status_is_off_unreachable_from_the_probe_and_on_otherwise(store):
    # given
    at = iso(NOW)
    target = {'name': 'umami-main', 'plugin': 'umami', 'instance': 'main', 'url': f'{URL}/api/heartbeat'}
    unset = ToolSet(tool_runtime(store, fake(), instances={}))
    serving = ToolSet(tool_runtime(store, fake()))

    # when
    off = unset.status('umami', {'at': at, 'targets': [{**target, 'ok': False, 'error': 'dns: gaierror'}]})
    cut = serving.status('umami', {'at': at, 'targets': [{**target, 'ok': False, 'error': 'dns: gaierror'}]})
    fine = serving.status('umami', {'at': at, 'targets': [{**target, 'ok': True, 'status': 200, 'error': None}]})

    # then
    assert off == {'state': 'off', 'reason': msg('toolset.off', name='umami')}
    assert cut == {'state': 'unreachable', 'reason': msg('tools.unreachable', targets='umami-main'), 'at': at}
    assert fine == {'state': 'on', 'reason': None} and serving.status('umami', None) == {'state': 'on', 'reason': None}


def test_top_paths_reads_the_busiest_paths_of_the_days_asked():
    # given
    http = api({('GET', f'{BASE}/metrics'): [{'x': '/pricing-notes', 'y': 900}, {'x': '/', 'y': 800}, {'x': None, 'y': 3}, {'x': '(none)', 'y': 2},
                                              {'x': 'https://evil.example.com/x', 'y': 2}, {'x': '//evil.example/x', 'y': 2}, {'x': '/\\evil.example.com', 'y': 2},
                                              {'x': '/a b', 'y': 1}, {'x': '/pricing-notes', 'y': 1}, {'x': '/docs/a:b', 'y': 1}]})
    info = InstanceInfo('main', True, None, dict(VALUES))

    # when
    started = time.time()
    paths = kit.top_paths(info, http, 7, 200)
    asked = query_of(http, f'{BASE}/metrics')

    # then
    assert paths == ['/pricing-notes', '/', '/docs/a:b'], 'a path that starts with // or holds a backslash or a scheme never leaves the kit'
    assert (asked['type'], asked['limit']) == (['path'], ['200'])
    assert int(asked['endAt'][0]) - int(asked['startAt'][0]) == 7 * DAY_MS and int(asked['endAt'][0]) >= int(started * 1000)
    assert http.hits(f'{BASE}/metrics')[0][2]['Authorization'] == f'Bearer {TOKEN}' and [c[1].split('?')[0] for c in http.calls] == [LOGIN, f'{BASE}/metrics']


def test_top_paths_refuses_a_bad_website_and_reports_a_failed_read_with_a_fixed_reason():
    # given
    down = api({('GET', f'{BASE}/metrics'): HttpError(502, f'{BASE}/metrics')})
    info = InstanceInfo('main', True, None, dict(VALUES))
    bad = InstanceInfo('main', True, None, dict(VALUES, **{'website-id': DOMAIN}))

    # when
    with pytest.raises(ReadError) as failed:
        kit.top_paths(info, down, 7, 200)
    with pytest.raises(ReadError) as refused:
        kit.top_paths(bad, FakeHttp(), 7, 200)

    # then
    assert (failed.value.status, failed.value.message) == (502, 'Umami HTTP 502 on metrics')
    assert (refused.value.status, refused.value.message) == (400, kit.BAD_WEBSITE)


def rules_tunable():
    return {t.key: t for t in plugin_tunables(umami.PLUGIN)}['umami.rules']


def test_umami_rules_are_checked_against_their_known_numeric_keys():
    # given
    base_settings = settings()

    # when
    errors = []
    for raw in ('{"nope": 1}', '{"redirect_warn": -1}', '[1]'):
        with pytest.raises(Invalid) as e:
            parse(rules_tunable(), raw, base_settings)
        errors.append(str(e.value))

    # then
    assert errors == ['Umami has no rule named nope', 'Umami: redirect_warn must be a number of 0 or more', 'Umami rules must be a JSON object of numbers']
    assert parse(rules_tunable(), '{"redirect_warn": 0.4, "type_warn": 0.4}', base_settings) == '{"redirect_warn":0.4}'


def test_an_umami_rule_threshold_refuses_values_that_are_not_finite():
    # when
    errors = []
    for raw in ('{"redirect_warn": NaN}', '{"redirect_warn": Infinity}'):
        with pytest.raises(Invalid) as e:
            parse(rules_tunable(), raw, settings())
        errors.append(str(e.value))

    # then
    assert errors == ['Umami: redirect_warn must be a number of 0 or more'] * 2


def test_admins_see_the_umami_rules_with_their_defaults_change_them_live_and_keep_the_history(store: Store):
    # given
    store.save(state(), None)
    admin = app_client(store, TokenEndpoint(access_token([ADMIN])))
    log_in(admin)

    # when
    listed = {i['key']: i for i in admin.get('/api/admin/settings').json()['items']}
    rules = admin.put('/api/admin/settings', json={'key': 'umami.rules', 'value': json.dumps({'redirect_warn': 0.3})})
    history = admin.get('/api/admin/history').json()

    # then
    assert listed['umami.rules']['rules'] == listed['umami.rules']['defaults'] == dict(check.RULES)
    assert rules.json()['item']['rules']['redirect_warn'] == 0.3 and rules.json()['item']['value'] == '{"redirect_warn":0.3}'
    assert [(h['key'], h['old'], h['new']) for h in history] == [('umami.rules', None, '{"redirect_warn":0.3}')]


def test_an_unknown_website_id_fails_test_and_the_pass_as_not_found_with_or_without_the_site_field(store):
    # given
    http = api({('GET', BASE): None, ('GET', f'{BASE}/stats'): {'pageviews': 0, 'visitors': 0, 'visits': 0, 'bounces': 0, 'totaltime': 0}})

    # when
    plain = tool_runtime(store, http, instances={}).connection_test(umami.PLUGIN, None, dict(VALUES), 5, NOW)
    named = tool_runtime(store, http, instances={}).connection_test(umami.PLUGIN, None, dict(VALUES, site='shop'), 5, NOW)
    out = umami.run(Ctx(http, dict(VALUES, site='shop'))).result

    # then
    assert (plain['ok'], plain['summary']) == (False, kit.NOT_FOUND) == (named['ok'], named['summary'])
    assert 'site field' not in kit.NOT_FOUND and kit.NOT_FOUND.startswith('website not found')
    assert (out.ok, out.status) == (False, f'error: website: {kit.NOT_FOUND}')


def test_a_refused_login_inside_a_pass_or_a_tool_uses_the_two_sided_wording(store):
    # given
    http = FakeHttp({('POST', LOGIN): HttpError(403, LOGIN)})

    # when
    out = umami.run(Ctx(http)).result
    told = call(um_box(store, http), op='stats')

    # then
    assert out.status == f'ok{DOT}no access to the website'
    assert [a['label'] for a in out.found] == ['Umami cannot read the website: the account is invalid or lacks view access to the website']
    assert told[1] is True and 'the account is invalid or lacks view access to the website' in told[0]


def test_an_access_card_without_a_site_or_domain_is_named_after_the_instance():
    # when
    out = umami.run(Ctx(refused_api(401, ''), name='east')).result

    # then
    assert [a['key'] for a in out.found] == ['umami-access|-/east|website'] and out.found[0]['siteId'] == SITE


def test_the_publish_helper_lives_in_the_kit_and_both_analytics_plugins_use_it():
    # given
    from skopax.kit import releases
    from skopax.plugins.ga4 import engine as ga4_engine

    # then
    assert check.attach_publish is releases.attach_publish and ga4_engine.attach_publish is releases.attach_publish
    assert not hasattr(check, 'publish_near') and not hasattr(ga4_engine, 'publish_near')


def test_the_umami_check_reads_gtm_publishes_through_the_gtm_plugin_releases(store: Store, tmp_path):
    # given
    from skopax.plugins import gtm
    publishes = [{'from': '133', 'to': '134', 'name': 'Ads fix', 'at': iso(NOW - 3 * 3600), 'changes': '1 tag changed (Ads conversion)'}]
    store.save({'anomalies': [], 'gtm': [{'plugin': 'gtm', 'container': 'GTM-ABCD12', 'live': {'id': '134'}, 'publishes': publishes}]}, None)
    from tests.fake_google import KEY as GOOGLE_KEY
    sealing = master_key(store.db, settings())
    add_instance(store, umami.PLUGIN, 'main', sealer_key=sealing, primary=True, **VALUES)
    add_instance(store, gtm.PLUGIN, 'main', sealer_key=sealing, primary=True, container='GTM-ABCD12', credentials=GOOGLE_KEY)

    # when
    with TestClient(create_app(settings(), store, registry=Registry.of(umami.PLUGIN, gtm.PLUGIN), transport=collapsed(), kube=Kube(root=tmp_path))) as c:
        c.app.state.sweeper.run('umami', 'umami', NOW)
    card = by_key(store.load())['umami-redirect|-/shop.example.com|checkout']

    # then
    assert card['gtmPublish']['version'] == '134' and 'GTM GTM-ABCD12 v134 "Ads fix" went live around' in card['what']
