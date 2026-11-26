import dataclasses
import datetime as dt
import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient

from skopax import engine, toolusage
from skopax.app import create_app
from skopax.contract import InstanceInfo
from skopax.engine import iso
from skopax.instances import Instances, PluginState
from skopax.investigator import prompt
from skopax.investigator.tools import CORE_TOOLS, Effects, Sources, Toolbox
from skopax.messages import msg
from skopax.plugins import dengage
from skopax.plugins.dengage import check, client
from skopax.plugintools import PluginTools
from skopax.redact import REDACTED
from skopax.registry import BUILTIN, Registry, card_types_view, describe
from skopax.remote import HttpError, Reply, Unreachable
from skopax.runtime import Runtime
from skopax.sealer import Sealer, master_key
from skopax.store import Store
from skopax.sweeps import Sweeper
from skopax.tunables import Live, TunableStore, plugin_tunables
from tests.conftest import AUTH, settings
from tests.plugin_kit import add_instance as kit_instance

HOME = Path(dengage.__file__).resolve().parent
SETTINGS = settings()
TOOL_NOW = 1_792_224_000.0

USERKEY = 'reader-USERKEY-0042'
PASSWORD = 'dengage-password-VERYSECRET'
KEY = 'dengage-key'
URL = 'https://dengage.example.com/rest'
EAST_URL = 'https://dengage-east.example.com/rest'
APP = 'https://app.dengage.com/'
API_ZONE = dt.timezone(dt.timedelta(hours=2))
NOW = dt.datetime(2026, 11, 13, 12, 0, tzinfo=dt.timezone.utc).timestamp()
HOUR = 3600
DAY = 86400
ALICE = 'alice@example.com'
VALUES = {'url': URL, 'userkey': USERKEY, 'password': PASSWORD, 'channels': 'push', 'utc-hours': '2'}
BAD_OFFSET = 'the UTC offset (utc-hours) must be a number of hours from -12 to 14'
BAD_URL = 'the API URL (url) must be an https address'


@pytest.fixture(autouse=True)
def no_held_tokens():
    client.TOKENS.clear()
    yield
    client.TOKENS.clear()


def raw(send_id: str, name: str, hours_ago: float, selected: int, relayed: int, blacklisted: int = 0, at_now: float = NOW, **over) -> dict:
    at = dt.datetime.fromtimestamp(at_now - hours_ago * HOUR, API_ZONE).strftime('%Y-%m-%dT%H:%M:%S')
    return {'sendId': send_id, 'sendName': name, 'campaignId': 'c-' + send_id, 'campaignName': name, 'contentId': 'k-' + send_id, 'status': 'COMPLETED', 'deliveryDate': at,
            'selectedCount': selected, 'preparedCount': selected - blacklisted, 'relayedCount': relayed, 'blacklistedCount': blacklisted, 'jobId': 7} | over


def daily(days: int = 27, selected: int = 50_000, relayed: int = 46_000, blacklisted: int = 1_000, first: int = 1) -> list[dict]:
    return [raw(f'd{k:02d}', 'Daily campaign', 24 * k + 3, selected, relayed, blacklisted) for k in range(first, first + days)]


DATA = tuple(f'dengage:{c}' for c in client.CHANNELS)
ACCESS = tuple(f'dengage:access:{c}' for c in client.CHANNELS)
NO_SMS = json.dumps({'message': 'You may not do this', 'missingPermissions': ['SmsSendsRead']})


class Api:
    def __init__(self, sends: dict | None = None, fail: int | None = None, body: str = '', expired: tuple = (), max_days: float = 30, denied: dict | None = None,
                 lost: tuple = (), broken: dict | None = None, min_limit: int = 100):
        self.min_limit = min_limit
        self.broken = dict(broken or {})
        self.sends = sends or {}
        self.fail = fail
        self.body = body
        self.expired = expired
        self.max_days = max_days
        self.denied = dict(denied or {})
        self.lost = lost
        self.calls: list[dict] = []
        self.logins: list[dict] = []

    def __call__(self, method, url, headers, data, timeout):
        where = urlsplit(url)
        if where.path.endswith('/login'):
            sent = json.loads(data)
            self.logins.append(sent)
            if sent != {'userkey': USERKEY, 'password': PASSWORD}:
                raise HttpError(401, url)
            return Reply({'access_token': f'jwt-{len(self.logins)}', 'expires_in': 3600}, {})
        query = {k: v[0] for k, v in parse_qs(where.query).items()}
        self.calls.append({'method': method, 'path': where.path, 'query': query, 'headers': dict(headers)})
        if self.fail:
            raise HttpError(self.fail, url.split('?')[0], self.body)
        if headers.get('Authorization') in self.expired:
            raise HttpError(401, url.split('?')[0])
        if where.path.rsplit('/', 1)[1] in self.denied:
            raise HttpError(403, url.split('?')[0], self.denied[where.path.rsplit('/', 1)[1]])
        if where.path.rsplit('/', 1)[1] in self.broken:
            raise HttpError(self.broken[where.path.rsplit('/', 1)[1]], url.split('?')[0])
        if where.path.rsplit('/', 1)[1] in self.lost:
            raise HttpError(401, url.split('?')[0])
        span = dt.datetime.fromisoformat(query['endDate']) - dt.datetime.fromisoformat(query['beginDate'])
        if span > dt.timedelta(days=self.max_days):
            raise HttpError(400, url.split('?')[0], '{"code": 400, "message": "date range is too long"}')
        if not self.min_limit <= int(query['limit']) <= 1000:
            raise HttpError(400, url.split('?')[0], '{"code": 400, "message": "limit value must be between 100 and 1000"}')
        channel = where.path.rsplit('/', 1)[1]
        rows = [s for s in self.sends.get(channel, []) if not isinstance(s, dict) or query['beginDate'] <= str(s.get('deliveryDate')).replace('T', ' ') <= query['endDate'] or s['deliveryDate'] == 'yesterday']
        offset, limit = int(query['offset']), int(query['limit'])
        return Reply({'code': 0, 'message': 'Successful', 'transactionId': 't1',
                      'data': {'affectedRowCount': len(rows), 'queryForNextPage': offset + limit < len(rows), 'sendList': rows[offset:offset + limit]}}, {})


def dengage_of(transport, channels: str = 'push', now: float = NOW, password: str = PASSWORD, utc_hours: str = '2', clock=None) -> client.Dengage:
    return client.Dengage(URL, USERKEY, password, transport, utc_hours=utc_hours, channels=channels, clock=clock or (lambda: now))


@dataclass
class Ctx:
    transport: object
    config: dict = field(default_factory=lambda: dict(VALUES))
    now: float = NOW
    before: list | None = None
    rules_given: dict = field(default_factory=dict)
    name: str = 'main'

    @property
    def instance(self) -> InstanceInfo:
        return InstanceInfo(self.name, True, None, self.config)

    def http(self):
        return self.transport

    def rules(self, set_id: str) -> dict:
        assert set_id == 'rules'
        return {**check.RULES, **self.rules_given}

    def previous(self, section: str) -> list | None:
        assert section == 'dengage'
        return self.before


def swept(api, now: float = NOW, before: list | None = None, rules: dict | None = None, **config):
    return dengage.run(Ctx(api, dict(VALUES, **config), now, before, rules or {}))


def rows_of(rows: list[dict], part: str) -> list[dict]:
    return [r for r in rows if r.get('part') == part]


def channel_row(rows: list[dict], channel: str = 'push') -> dict:
    return next(r for r in rows_of(rows, 'channel') if r['channel'] == channel)


class FakeHttp:
    def __init__(self, routes: dict):
        self.routes = dict(routes)

    def __call__(self, method, url, headers, data, timeout):
        got = self.routes.get((method, url.split('?')[0]))
        if got is None:
            raise HttpError(404, url.split('?')[0])
        if isinstance(got, Exception):
            raise got
        return Reply(got, {})


def add_instance(store, name: str, primary: bool | None = None, enabled: bool | None = None, **values) -> dict:
    secret_keys = {f.key for f in dengage.PLUGIN.fields if f.kind == 'secret'}
    sealer = Sealer(KEY)
    doc = {'_id': f'dengage/{name}', 'plugin': 'dengage', 'name': name, 'values': {k: v for k, v in values.items() if k not in secret_keys},
           'secrets': {k: sealer.seal(v) for k, v in values.items() if k in secret_keys}, 'createdBy': 'test', 'createdAt': iso(NOW)}
    if primary is not None:
        doc['primary'] = primary
    if enabled is not None:
        doc['enabled'] = enabled
    store.db['instances'].replace_one({'_id': doc['_id']}, doc, upsert=True)
    return doc


def runtime_of(store, http, instances: dict | None = None) -> Runtime:
    reg = Registry.of(dengage.PLUGIN, directory=HOME)
    runtime = Runtime(reg, PluginState(store.db), Instances(store.db, reg, lambda: Sealer(KEY)), http)
    runtime.live = Live(SETTINGS, TunableStore(store.db), extra=lambda: tuple(t for p in runtime.active() for t in plugin_tunables(p)))
    for i, (name, values) in enumerate(({'main': VALUES} if instances is None else instances).items()):
        add_instance(store, name, primary=i == 0, **values)
    runtime.refresh()
    return runtime


def toolbox(store, plugin_tools: PluginTools) -> Toolbox:
    return Toolbox(Sources(None, None, plugin_tools=plugin_tools), store, Effects(lambda key, by: {}, lambda *a, **k: {}), ['log|a|ERROR|x'], 'live', TOOL_NOW,
                   SETTINGS.base_url, settings=dataclasses.replace(SETTINGS, link_hosts=('example.com',)))


def dengage_box(store, http, instances: dict | None = None):
    return toolbox(store, PluginTools(runtime_of(store, http, instances)))


class Board:
    def __init__(self, store, http, instances: dict | None = None):
        self.store = store
        self.runtime = runtime_of(store, http, instances)
        self.instances = self.runtime.instances
        self.sweeper = Sweeper(self.runtime, store, self.apply)

    def __enter__(self) -> 'Board':
        return self

    def __exit__(self, *exc) -> None:
        return None

    def apply(self, results, now, links, after=None, check=None, screen=None, clusters=None):
        return engine.apply(self.store, results, now, links, after=after, screen=screen, clusters=clusters)

    def run(self, now: float):
        return self.sweeper.run('dengage', 'dengage', now)

    def switch_off(self, name: str) -> dict:
        before = self.instances.fresh('dengage', name)
        after = self.instances.set_enabled('dengage', name, False, ALICE)
        self.runtime.refresh()
        self.sweeper.retire('dengage', before.info(), NOW)
        return after.view()

    def change(self, name: str, values: dict) -> dict:
        after = self.instances.update('dengage', name, values, ALICE)
        self.runtime.refresh()
        return after.view()


def board(store, http, instances: dict | None = None) -> Board:
    return Board(store, http, instances)


def stored_state(*anomalies: dict) -> dict:
    return {'anomalies': list(anomalies), 'sweptIso': iso(NOW - HOUR), 'nextIso': iso(NOW), 'sources': {}, 'history': []}


def by_key(state: dict) -> dict:
    return {a['key']: a for a in state['anomalies']}


def test_the_token_is_kept_until_it_runs_out_and_the_keys_go_only_to_the_login():
    # given
    clock = [NOW]
    api = Api({'push': [raw('s1', 'Spring sale', 2, 52_340, 48_112, 310)]})
    d = dengage_of(api, clock=lambda: clock[0])

    # when
    first, second = d.sends('push', NOW - DAY, NOW), d.sends('push', NOW - DAY, NOW)
    rebuilt = dengage_of(api, clock=lambda: clock[0]).sends('push', NOW - DAY, NOW)
    clock[0] += HOUR
    third = d.sends('push', NOW - DAY, NOW)

    # then
    assert len(first) == len(second) == len(rebuilt) == len(third) == 1
    assert api.logins == [{'userkey': USERKEY, 'password': PASSWORD}] * 2, 'the token is kept until it runs out, also for a client built again for the next pass'
    assert [c['headers']['Authorization'] for c in api.calls] == ['Bearer jwt-1', 'Bearer jwt-1', 'Bearer jwt-1', 'Bearer jwt-2']
    assert all(c['method'] == 'GET' and c['path'] == '/rest/sends/push' and PASSWORD not in json.dumps(c) and USERKEY not in json.dumps(c) for c in api.calls)
    assert not any(USERKEY in k or PASSWORD in k for k in client.TOKENS)


def test_a_permission_granted_after_the_last_login_is_seen_without_waiting_for_the_token_to_run_out():
    # given
    api = Api({'push': [raw('s1', 'Spring sale', 2, 900, 880)]})
    first_allowed = [99]

    def tokens_carry_permissions(method, url, headers, data, timeout):
        if '/sends/' in url and int(headers['Authorization'].rsplit('-', 1)[1]) < first_allowed[0]:
            raise HttpError(403, url.split('?')[0], '{"message": "no"}')
        return api(method, url, headers, data, timeout)
    d = dengage_of(tokens_carry_permissions)

    # when
    with pytest.raises(client.DengageError):
        d.sends('push', NOW - DAY, NOW)
    first_allowed[0] = len(api.logins) + 1
    rows = d.sends('push', NOW - DAY, NOW)

    # then
    assert len(rows) == 1 and len(api.logins) == 3, 'the token from before the permission change is dropped on a 403 and the next login sees the permission'
    assert [c['headers']['Authorization'] for c in api.calls][-1] == 'Bearer jwt-3'


def test_a_refused_token_is_renewed_once_and_errors_name_the_cause_without_the_keys():
    # given
    renewed = Api({'push': [raw('s1', 'Spring sale', 2, 900, 880)]}, expired=('Bearer jwt-1',))
    denied = Api(fail=403, body='{"message": "You may not do this", "missingPermissions": ["SendsRead"], "allPermissionsRequired": true}')
    plain = Api(fail=403, body='<html>Forbidden</html>')
    limited = Api(fail=429)

    def down(method, url, headers, data, timeout):
        raise Unreachable(f'{url} unreachable (password {PASSWORD} userkey {USERKEY})')

    # when
    rows = dengage_of(renewed).sends('push', NOW - DAY, NOW)
    errors, kinds = [], []
    for make in (lambda: dengage_of(denied), lambda: dengage_of(limited), lambda: dengage_of(Api(), password='wrong-password'), lambda: dengage_of(down), lambda: dengage_of(plain)):
        client.TOKENS.clear()
        with pytest.raises(client.DengageError) as e:
            make().sends('push', NOW - DAY, NOW)
        errors.append((e.value.message, e.value.reason))
        kinds.append((type(e.value).__name__, getattr(e.value, 'permission', None)))
    with pytest.raises(client.DengageError) as unknown:
        dengage_of(Api()).sends('fax', NOW - DAY, NOW)

    # then
    assert len(rows) == 1 and len(renewed.logins) == 2 and [c['headers']['Authorization'] for c in renewed.calls] == ['Bearer jwt-1', 'Bearer jwt-2']
    assert errors[0] == ('Dengage HTTP 403 on /sends/push: the API user lacks SendsRead; Dengage said: You may not do this', 'Dengage HTTP 403 on /sends/push')
    assert len(denied.logins) == 2 and len(denied.calls) == 2, 'a refusal is tried once more with a fresh login, then reported'
    assert errors[4][0] == 'Dengage HTTP 403 on /sends/push: the API user lacks the needed permission; Dengage said: <html>Forbidden</html>'
    assert errors[1][0] == 'Dengage HTTP 429 on /sends/push: rate limited, try again in a minute'
    assert errors[2][0] == 'Dengage HTTP 401 on /login: the userkey or password was refused'
    assert errors[3] == ('Dengage unreachable on /login', 'Dengage unreachable on /login')
    assert all(PASSWORD not in m and USERKEY not in m for m, _ in errors)
    assert unknown.value.message.startswith('channel is one of push, email, sms')
    assert kinds == [('DengageRefused', 'SendsRead'), ('DengageError', None), ('DengageError', None), ('DengageError', None), ('DengageRefused', '')], \
        'only a refusal of the data itself is a missing permission; a refused login is a wrong key or password'


def test_a_server_error_body_reaches_the_tool_with_the_instance_secrets_masked(store: Store):
    # given
    echo = Api(fail=400, body=json.dumps({'message': f'bad request for {USERKEY} with {PASSWORD}'}))

    # when
    text, err = dengage_box(store, echo).call('dengage', {})
    status = swept(echo).result.status

    # then
    assert err and text == 'Dengage HTTP 400 on /sends/push: Dengage said: bad request for *** with ***'
    assert status == 'error: push: Dengage HTTP 400 on /sends/push', 'a pass status names the code only, never the server body'


def test_a_secret_the_server_echoes_json_escaped_is_masked_too(store: Store):
    # given
    odd = 'pa"ss\\word/SECRET-\u00e9'

    def echo(method, url, headers, data, timeout):
        if url.endswith('/login'):
            return Reply({'access_token': 'jwt-odd', 'expires_in': 3600}, {})
        raise HttpError(400, url.split('?')[0], json.dumps({'message': f'bad request with {odd}', 'missingPermissions': [f'Read-{odd}']}))

    def refused(method, url, headers, data, timeout):
        if url.endswith('/login'):
            return Reply({'access_token': 'jwt-odd', 'expires_in': 3600}, {})
        raise HttpError(403, url.split('?')[0], json.dumps({'message': f'denied for {odd}', 'missingPermissions': [f'Read-{odd}']}))

    # when
    said, said_err = dengage_box(store, echo, instances={'main': dict(VALUES, password=odd)}).call('dengage', {})
    denied, denied_err = dengage_box(store, refused, instances={'main': dict(VALUES, password=odd)}).call('dengage', {})

    # then
    assert said_err and said == 'Dengage HTTP 400 on /sends/push: Dengage said: bad request with ***'
    assert denied_err and denied == 'Dengage HTTP 403 on /sends/push: the API user lacks Read-***; Dengage said: denied for ***'
    assert odd not in said + denied and 'SECRET' not in said + denied


def test_sends_are_read_page_by_page_in_the_api_time_zone_and_broken_rows_are_left_out():
    # given
    many = [raw(f's{k}', f'Campaign {k}', 1 + k / 1000, 10, 9) for k in range(1001)]
    odd = [raw('s 1/x', 'Card  campaign\n alice@example.com', 2, 100, 90, tagResponseList=[{'tagId': 4, 'tagName': 'card'}, {'tagId': 5}]),
           raw('', 'no id', 2, 1, 1), raw('s2', 'no date', 2, 1, 1, deliveryDate='yesterday'), 'not a row']
    paged, messy = Api({'push': many}), Api({'sms': odd})

    # when
    rows = dengage_of(paged).sends('push', NOW - DAY, NOW)
    [only] = dengage_of(messy).sends('sms', NOW - DAY, NOW)
    utc = dengage_of(messy, utc_hours='0').sends('sms', NOW - DAY, NOW + DAY)

    # then
    assert len(rows) == 1001 and [c['query']['offset'] for c in paged.calls] == ['0', '1000'] and paged.calls[0]['query']['limit'] == '1000'
    assert paged.calls[0]['query']['beginDate'] == '2026-11-12 14:00:00' and paged.calls[0]['query']['endDate'] == '2026-11-13 14:00:00'
    assert only['id'] == 's1x' and only['at'] == iso(NOW - 2 * HOUR) and only['channel'] == 'sms' and only['tags'] == ['card']
    assert only['name'] == f'Card campaign {REDACTED}' and 'campaign' not in only
    assert (only['selected'], only['prepared'], only['relayed'], only['excluded']) == (100, 100, 90, 0)
    assert utc[0]['at'] == iso(NOW), 'the same clock text is two hours later when the API zone is set to UTC'


def test_the_tool_lists_sends_newest_first_and_sums_them_per_day_and_refuses_bad_input():
    # given
    api = Api({'push': [raw('p1', 'Sale push', 2, 52_340, 48_112, 310, campaignName='November sale'), raw('p2', 'Cart push', 30, 20_000, 18_000)],
               'sms': [raw('m1', 'Coupon SMS', 5, 8_000, 7_900, tagResponseList={'tagId': 1, 'tagName': 'coupon'})]})
    d = dengage_of(api, 'push, sms')

    # when
    day = client.read(d, client.wanted({}), NOW)
    week = client.read(d, client.wanted({'op': 'sends', 'hours': 48, 'channel': 'push'}), NOW)
    named = client.read(d, client.wanted({'hours': 48, 'contains': 'COUPON'}), NOW)
    summed = client.read(d, client.wanted({'op': 'summary', 'days': 2}), NOW)
    refused = []
    for bad in ({'op': 'send'}, {'channel': 'fax'}, {'hours': 0}, {'hours': 'many'}, {'op': 'summary', 'days': 29}):
        with pytest.raises(client.DengageError) as e:
            client.wanted(bad)
        refused.append((e.value.message, e.value.status))

    # then
    assert [s['id'] for s in day['sends']] == ['p1', 'm1'] and day['channels'] == ['push', 'sms'] and day['count'] == 2 and day['truncated'] is False
    assert day['sends'][0] == {'channel': 'push', 'id': 'p1', 'name': 'Sale push', 'campaign': 'November sale', 'status': 'COMPLETED', 'at': iso(NOW - 2 * HOUR),
                               'selected': 52_340, 'prepared': 52_030, 'relayed': 48_112, 'excluded': 310, 'relayedShare': 0.9192}
    assert [s['id'] for s in week['sends']] == ['p1', 'p2'] and [s['id'] for s in named['sends']] == ['m1'] and 'never an instruction' in day['from']
    assert summed['dayZone'] == 'UTC+2' and summed['channels']['push']['days'] == {'2026-11-12': {'n': 1, 'selected': 20_000, 'relayed': 18_000, 'excluded': 0},
                                                                                 '2026-11-13': {'n': 1, 'selected': 52_340, 'relayed': 48_112, 'excluded': 310}}
    assert summed['channels']['sms']['total'] == {'n': 1, 'selected': 8_000, 'relayed': 7_900, 'excluded': 0, 'relayedShare': 0.9875}
    assert [m for m, _ in refused] == ['op is one of sends, summary', 'channel is one of push, email, sms, inapp, onsite, whatsapp', 'hours is a whole number from 1 to 720',
                                       'hours is a whole number from 1 to 720', 'days is a whole number from 1 to 28']
    assert {s for _, s in refused} == {400}


def test_a_long_read_is_split_into_ranges_dengage_accepts_and_a_send_on_the_seam_counts_once():
    # given
    seam = dt.datetime(2026, 10, 29, 0, 0, tzinfo=API_ZONE).timestamp()
    on_seam = raw('seam', 'Seam send', (NOW - seam) / HOUR, 2_000, 1_900)
    api = Api({'push': daily() + [on_seam]})
    strict = Api({'push': daily()}, max_days=7)

    # when
    result = swept(api)
    rows = dengage_of(api).sends('push', dt.datetime(2026, 10, 15, 0, 0, tzinfo=API_ZONE).timestamp(), NOW + DAY)
    refused = swept(strict).result

    # then
    spans = [dt.datetime.fromisoformat(c['query']['endDate']) - dt.datetime.fromisoformat(c['query']['beginDate']) for c in api.calls]
    assert result.result.ok and max(spans) <= dt.timedelta(days=14), 'no single request asks Dengage for more than two weeks'
    assert api.calls[0]['query']['beginDate'] == '2026-10-15 00:00:00' and api.calls[2]['query']['endDate'] == '2026-11-14 14:00:00'
    assert [r['id'] for r in rows].count('seam') == 1 and len(rows) == 28
    assert channel_row(result.sections['dengage'])['days']['2026-10-29']['n'] == 2
    assert not refused.ok and refused.status == 'error: push: Dengage HTTP 400 on /sends/push'


def test_every_check_reads_four_weeks_and_a_big_send_gets_an_info_card_for_a_day():
    # given
    sends = daily() + [raw('big', 'Spring sale', 2, 52_340, 48_112, 1_100), raw('small', 'Trial', 1, 400, 390)]
    api = Api({'push': sends})
    info = InstanceInfo('main', True, None, dict(VALUES))

    # when
    first = swept(api)
    second = swept(api, now=NOW + 900, before=first.sections['dengage'])
    later = swept(api, now=NOW + 23 * HOUR, before=second.sections['dengage'])

    # then
    assert len(api.calls) == 9 and [c['query']['beginDate'] for c in api.calls][::3] == ['2026-10-15 00:00:00', '2026-10-15 00:00:00', '2026-10-16 00:00:00'], \
        'every pass reads the 4 weeks up to its own day, in three ranges'
    assert (first.result.ok, first.result.status, first.result.name, first.result.ran) == (True, 'ok', 'dengage', ('dengage',) + DATA + ACCESS)
    by = {c['key']: c for c in first.result.found}
    assert sorted(by) == ['dengage-send|-/dengage|push-big'], 'the small send and yesterday\'s send raise nothing'
    card = by['dengage-send|-/dengage|push-big']
    assert (card['sev'], card['src'], card['label'], card['now'], card['ranKey']) == ('info', 'dengage', 'Push send went out', 48_112, 'dengage:push')
    assert card['what'].startswith('Dengage push send "Spring sale" went out 13 Nov 10:00 UTC: selected 52,340, prepared 51,240, relayed 48,112, excluded 1,100.')
    assert 'cluster' not in card and 'plugin' not in card
    rows = first.sections['dengage']
    push = channel_row(rows)
    assert push['last'] == iso(NOW - HOUR) and push['sends24h'] == 2
    assert len(push['days']) == 28 and push['days']['2026-11-13'] == {'n': 2, 'selected': 52_740, 'relayed': 48_502, 'excluded': 1_100}
    assert [r['id'] for r in rows_of(rows, 'recent')] == ['small', 'big']
    assert rows_of(rows, 'summary') == [{'part': 'summary', 'checkedAt': iso(NOW)}]
    assert not any('cluster' in r or 'plugin' in r for r in rows)
    assert channel_row(second.sections['dengage'])['days'] == push['days'], 'each pass rebuilds the days and counts nothing twice'
    assert [c['key'] for c in second.result.found] == [c['key'] for c in first.result.found] and later.result.found == []
    assert dengage.links(card, info) == [{'label': 'Dengage', 'url': APP}] and dengage.links({'src': 'vwo'}, info) is None


def test_a_send_that_relays_far_less_than_usual_or_is_excluded_much_more_gets_a_warning():
    # given
    usual = daily()
    low = raw('low', 'Spring sale', 2, 40_000, 20_000, 800)
    listed = raw('listed', 'Newsletter', 3, 40_000, 33_000, 10_000)
    fine = raw('fine', 'Weekend offer', 4, 40_000, 36_000, 900)
    tiny = raw('tiny', 'Trial', 5, 300, 10)
    fresh = raw('fresh', 'First e-mail', 2, 10_000, 6_500, 3_000)

    # when
    found = {c['key']: c for c in swept(Api({'push': usual + [low, listed, fine, tiny]})).result.found}
    alone = swept(Api({'email': [fresh, raw('ok', 'Second e-mail', 3, 10_000, 7_100, 2_900)]}), channels='email').result.found
    loose = swept(Api({'push': usual + [low]}), rules={'relay_drop': 0.5}).result.found

    # then
    assert sorted(found) == ['dengage-delivery|-/dengage|push-listed', 'dengage-delivery|-/dengage|push-low', 'dengage-send|-/dengage|push-fine']
    relay = found['dengage-delivery|-/dengage|push-low']
    assert (relay['sev'], relay['now'], relay['unit'], relay['problem'], relay['share'], relay['usualShare']) == ('warn', 50.0, '%', 'relay', 0.5, 0.92)
    assert relay['label'] == 'Push send reached far fewer contacts than it selected'
    assert "it relayed 20,000 of the 40,000 contacts it selected (50.0 %); 95 % of this channel's 27 large sends of the 4 weeks before relayed at least 92.0 %" in relay['what']
    black = found['dengage-delivery|-/dengage|push-listed']
    assert (black['problem'], black['share'], black['usualShare'], black['usualKept'], black['now']) == ('excluded', 0.25, 0.02, 0.95, 25.0)
    assert 'Dengage excluded 10,000 of the 40,000 contacts it selected (25.0 %; no notification permission, no push token, a duplicate token, or the blacklist); 95 % of this channel\'s 27 large sends of the 4 weeks before excluded at most 2.0 %' in black['what']
    assert [c['key'] for c in alone] == ['dengage-delivery|-/dengage|email-fresh', 'dengage-send|-/dengage|email-ok']
    assert 'there is no 4-week baseline for this channel yet, so the limit is 70.0 % of the selected contacts' in alone[0]['what'] and alone[0]['usualShare'] is None
    assert [c['sig'] for c in loose] == ['dengage-send'], 'the threshold comes from the rules'
    assert check.RULES['relay_drop'] == 0.7 and check.RULES['event_min_selected'] == 1000 and check.RULES['usual_share'] == 0.95 and 'blacklist_times' not in check.RULES


def test_a_campaign_part_that_always_loses_a_quarter_to_exclusion_is_usual_and_only_a_real_jump_warns():
    # given
    pairs = [s for k in range(1, 28) for s in (raw(f'a{k:02d}', f'Promo_{k:02d}_part 1', 24 * k + 3, 45_000, 33_700, 11_300),
                                              raw(f'b{k:02d}', f'Promo_{k:02d}_part 2', 24 * k + 3, 48_000, 47_990, 0))]
    usual = raw('usual', 'Weekly_part 1', 2, 45_644, 34_124, 11_519)
    second = raw('second', 'Weekly_part 2', 2, 48_959, 48_955, 0)
    jump = raw('jump', 'Coupon_part 1', 3, 45_000, 24_000, 21_000)

    # when
    found = {c['key']: c for c in swept(Api({'push': pairs + [usual, second, jump]})).result.found}

    # then
    assert found['dengage-send|-/dengage|push-usual']['sig'] == 'dengage-send', 'part 1 at its usual 25 % is a plain send'
    assert found['dengage-send|-/dengage|push-second']['sig'] == 'dengage-send'
    card = found['dengage-delivery|-/dengage|push-jump']
    assert (card['problem'], card['share'], card['usualShare']) == ('excluded', 0.4667, 0.2511)
    assert "95 % of this channel's 54 large sends of the 4 weeks before excluded at most 25.1 %" in card['what']


def reminders(count: int = 5, excluded: tuple = (19_000, 18_800, 19_200, 18_900, 19_100), name: str = 'Monthly reminder') -> list[dict]:
    return [raw(f'm{k:02d}', name, 24 * k + 3, 50_000, 50_000 - excluded[(k - 1) % len(excluded)], excluded[(k - 1) % len(excluded)]) for k in range(1, count + 1)]


def others() -> list[dict]:
    return [raw(f'o{k:02d}', 'Other campaign', 24 * k + 4, 50_000, 46_000, 1_000) for k in range(1, 28)]


def test_a_recurring_campaign_that_always_excludes_a_share_opens_no_delivery_card_once_it_has_five_earlier_sends():
    # given
    today = raw('today', 'Monthly reminder', 2, 50_000, 31_000, 19_000)

    # when
    found = swept(Api({'push': daily() + others() + reminders() + [today]})).result.found

    # then
    assert [(c['key'], c['sig']) for c in found] == [('dengage-send|-/dengage|push-today', 'dengage-send')]


def test_the_same_campaign_jumping_far_above_its_own_exclusions_opens_a_card_that_names_the_campaign_baseline():
    # given
    jump = raw('jump', 'Monthly reminder', 2, 50_000, 25_000, 30_000)

    # when
    [card] = [c for c in swept(Api({'push': daily() + others() + reminders() + [jump]})).result.found if c['sig'] == 'dengage-delivery']

    # then
    assert card['key'] == 'dengage-delivery|-/dengage|push-jump'
    assert (card['problem'], card['share'], card['baseline']) == ('excluded', 0.6, 'campaign')
    assert "95 % of this campaign's 5 earlier sends of the 4 weeks before excluded at most 38.4 %" in card['what']
    assert card['label'] == 'Push send excluded many contacts'


def test_a_campaign_with_fewer_than_five_earlier_sends_is_judged_against_the_channel():
    # given
    fresh = raw('fresh', 'Monthly reminder', 2, 50_000, 40_000, 19_000)

    # when
    [card] = [c for c in swept(Api({'push': daily() + others() + reminders(count=2) + [fresh]})).result.found if c['sig'] == 'dengage-delivery']

    # then
    assert (card['problem'], card['baseline'], card['usualShare']) == ('excluded', 'channel', 0.02)
    assert "95 % of this channel's 56 large sends of the 4 weeks before excluded at most 2.0 %" in card['what']


def test_a_send_without_a_campaign_name_is_matched_to_its_earlier_sends_by_its_own_name():
    # given
    history = [raw(f'n{k}', 'Monthly reminder', 24 * k + 3, 50_000, 31_000, 19_000, campaignName=None) for k in range(1, 6)]
    today = raw('today', 'Monthly reminder', 2, 50_000, 31_000, 19_000, campaignName=None)

    # when
    found = swept(Api({'push': daily() + history + [today]})).result.found

    # then
    assert [c['sig'] for c in found] == ['dengage-send']


def test_the_campaign_minimum_is_a_rule_and_a_lower_one_judges_a_young_campaign_on_its_own_sends():
    # given
    today = raw('today', 'Monthly reminder', 2, 50_000, 31_000, 19_000)
    api = Api({'push': daily() + others() + reminders(count=2) + [today]})

    # when
    strict = swept(api).result.found
    loose = swept(api, rules={'campaign_min_sends': 2}).result.found

    # then
    assert [c['sig'] for c in strict] == ['dengage-delivery'] and [c['sig'] for c in loose] == ['dengage-send']
    assert check.RULES['campaign_min_sends'] == 5 and check.RULE_HELP['campaign_min_sends']


def test_the_wording_says_excluded_with_its_reasons_once_and_never_blacklist():
    # given
    listed = raw('listed', 'Newsletter', 3, 40_000, 33_000, 10_000)
    plain = raw('plain', 'Spring sale', 2, 52_340, 48_112, 1_100)

    # when
    found = {c['key']: c for c in swept(Api({'push': daily() + [listed, plain]})).result.found}

    # then
    card = found['dengage-delivery|-/dengage|push-listed']
    assert card['label'] == 'Push send excluded many contacts' and card['excluded'] == 10_000 and 'blacklisted' not in card
    assert 'Dengage excluded 10,000 of the 40,000 contacts it selected (25.0 %; no notification permission' in card['what']
    assert card['what'].count('no notification permission, no push token, a duplicate token, or the blacklist') == 1
    assert 'excluded 10,000' in card['what'] and 'blacklisted' not in card['what']
    assert 'excluded 1,100' in found['dengage-send|-/dengage|push-plain']['what']
    texts = [c['what'] + c['label'] + c.get('title', '') for c in found.values()] + list(check.RULE_HELP.values()) + [client.NOTE, dengage.DESCRIPTION, dengage.PLUGIN.knowledge[0].text]
    assert not [t for t in texts if 'blacklist' in t.lower().replace('or the blacklist', '').replace('the blacklist', '')], 'only the explanation names the blacklist'


def test_each_channel_names_its_own_exclusion_reasons_and_an_email_card_never_mentions_push_tokens():
    # given
    listed = lambda name, sel: raw(name, 'Newsletter', 3, sel, sel - 12_000, 12_000)

    # when
    email = swept(Api({'email': daily() + [listed('e1', 40_000)]}), channels='email').result.found
    sms = swept(Api({'sms': daily() + [listed('s1', 40_000)]}), channels='sms').result.found
    other = swept(Api({'whatsapp': daily() + [listed('w1', 40_000)]}), channels='whatsapp').result.found

    # then
    [mail] = [c for c in email if c['sig'] == 'dengage-delivery']
    [text] = [c for c in sms if c['sig'] == 'dengage-delivery']
    [rest] = [c for c in other if c['sig'] == 'dengage-delivery']
    assert 'no valid address, unsubscribed, or the blacklist' in mail['what'] and 'push' not in mail['what'].replace('Dengage push', '').lower()
    assert 'no valid number, opted out, or the blacklist' in text['what'] and 'token' not in text['what']
    assert "Dengage's own filters or the blacklist" in rest['what'] and 'token' not in rest['what']


def test_a_campaign_minimum_of_zero_and_a_baseline_minimum_of_zero_do_not_crash_the_pass():
    # given
    fresh = raw('fresh', 'Brand new', 2, 50_000, 46_000, 1_000)
    api = Api({'push': [fresh]})

    # when
    zero_campaign = swept(api, rules={'campaign_min_sends': 0}).result
    zero_baseline = swept(api, rules={'baseline_min_sends': 0}).result

    # then
    assert zero_campaign.ok and zero_baseline.ok
    assert [c['sig'] for c in zero_campaign.found] == ['dengage-send'] and [c['sig'] for c in zero_baseline.found] == ['dengage-send']


def test_a_card_stored_in_the_old_shape_is_replaced_under_the_same_key_at_the_next_pass(store: Store):
    # given
    listed = raw('listed', 'Newsletter', 3, 40_000, 28_000, 12_000)
    key = 'dengage-delivery|-/dengage|push-listed'
    stored = old_card(key, 'dengage-delivery', 'push-listed') | {'problem': 'blacklist', 'blacklisted': 12_000, 'label': 'Push send lost many contacts to the blacklist'}
    store.save_state(stored_state(stored), None)

    # when
    with board(store, Api({'push': daily() + [listed]})) as c:
        c.run(NOW)
        after = by_key(store.load())

    # then
    card = after[key]
    assert card['status'] == 'open' and card['problem'] == 'excluded' and card['excluded'] == 12_000 and 'blacklisted' not in card
    assert card['label'] == 'Push send excluded many contacts'


def test_the_tool_reports_excluded_counts_and_still_reads_the_api_field_blacklisted_count():
    # given
    api = Api({'push': [raw('a', 'Spring sale', 5, 1_000, 900, 100)]})
    d = dengage_of(api)

    # when
    sent = client.read(d, {'op': 'sends', 'channel': 'push', 'hours': 24, 'contains': ''}, NOW)
    summed = client.read(d, {'op': 'summary', 'channel': 'push', 'days': 3}, NOW)

    # then
    row = sent['sends'][0]
    assert row['excluded'] == 100 and 'blacklisted' not in row
    assert summed['channels']['push']['total']['excluded'] == 100 and 'excluded' in summed['note'] and 'blacklist' not in summed['note'].replace('or the blacklist', '')


def test_a_channel_that_sent_most_days_and_then_stops_gets_a_silence_warning_but_its_usual_gaps_do_not():
    # given
    stopped = daily(days=25, first=3)
    weekdays = [s for s in daily(days=27, first=3) if dt.datetime.fromisoformat(s['deliveryDate']).weekday() < 5]
    rare = daily(days=5, first=3)

    # when
    [card] = swept(Api({'push': stopped})).result.found
    usual_gap = swept(Api({'push': weekdays})).result.found
    few = swept(Api({'push': rare})).result.found
    short = swept(Api({'push': daily(days=25, first=1)})).result.found

    # then
    assert (card['key'], card['sev'], card['label'], card['now']) == ('dengage-silence|-/dengage|push', 'warn', 'No push send for 75 hours', 75.0)
    assert card['what'].startswith('Dengage has completed no push send since 10 Nov 09:00 UTC (75 h). In the 28 days up to then push went out on 25 days and the longest gap between send days was 1 day.')
    assert (card['quietHours'], card['activeDays'], card['lastSendAt'], card['channel']) == (75, 25, iso(NOW - 75 * HOUR), 'push')
    assert usual_gap == [], 'a channel that never sends on weekends is not silent after a weekend-long gap'
    assert few == [] and short == [], 'a day without a send is under the 48 hour limit'


def test_a_failed_read_fails_the_pass_without_the_keys_and_keeps_the_last_state(store: Store):
    # given
    api = Api({'push': daily() + [raw('big', 'Spring sale', 2, 52_340, 48_112)]})

    # when
    with board(store, api) as c:
        c.run(NOW)
        before = store.load()
        api.fail, api.body = 500, json.dumps({'message': f'failed for {PASSWORD}'})
        c.run(NOW + 900)
    after = store.load()
    login_refused = swept(Api({'push': []}), password='wrong-password').result

    # then
    assert before['sources']['dengage'] == 'ok' and after['sources']['dengage'] == 'error: push: Dengage HTTP 500 on /sends/push'
    assert by_key(after)['dengage-send|-/dengage|push-big']['status'] == 'open'
    assert after['dengage'] == before['dengage'], 'a failed read keeps the rows of the last good pass'
    assert (login_refused.ok, login_refused.status) == (False, 'error: Dengage HTTP 401 on /login: the userkey or password was refused')
    assert PASSWORD not in json.dumps(after, default=str) and USERKEY not in json.dumps(after, default=str)


def test_switching_the_instance_off_closes_its_cards_once(store: Store):
    # given
    api = Api({'push': daily() + [raw('big', 'Spring sale', 2, 52_340, 48_112)]})

    # when
    with board(store, api) as c:
        c.run(NOW)
        opened = by_key(store.load())['dengage-send|-/dengage|push-big']['status']
        off = c.switch_off('main')
        calls = len(api.calls)
        c.run(NOW + 900)
    state = store.load()

    # then
    assert opened == 'open' and off['enabled'] is False
    assert by_key(state)['dengage-send|-/dengage|push-big']['status'] != 'open'
    assert 'dengage' not in state['sources'] and not state.get('dengage') and len(api.calls) == calls, 'a switched-off instance reads nothing more'


def test_a_channel_taken_off_the_list_closes_its_cards_at_the_next_pass(store: Store):
    # given
    api = Api({'push': daily() + [raw('big', 'Spring sale', 2, 52_340, 48_112)], 'sms': daily(days=25, first=3)})

    # when
    with board(store, api, instances={'main': dict(VALUES, channels='push, sms')}) as c:
        c.run(NOW)
        before = {k: a['status'] for k, a in by_key(store.load()).items()}
        changed = c.change('main', {'channels': 'push'})
        c.run(NOW + 900)
    state = store.load()
    after = by_key(state)

    # then
    assert before == {'dengage-send|-/dengage|push-big': 'open', 'dengage-silence|-/dengage|sms': 'open'} and changed['values']['channels'] == 'push'
    assert after['dengage-silence|-/dengage|sms']['status'] != 'open' and after['dengage-send|-/dengage|push-big']['status'] == 'open'
    assert [r['channel'] for r in rows_of(state['dengage'], 'channel')] == ['push'] and state['sources']['dengage'] == 'ok'


def test_a_dengage_with_no_sends_at_all_is_ok_with_no_data_and_closes_no_card(store: Store):
    # given
    api = Api({'sms': daily(days=25, first=3)})

    # when
    with board(store, api, instances={'main': dict(VALUES, channels='sms')}) as c:
        c.run(NOW)
        before = by_key(store.load())['dengage-silence|-/dengage|sms']['status']
        api.sends = {}
        c.run(NOW + 900)
    state = store.load()
    quiet = swept(Api({}), channels='push, sms').result

    # then
    assert before == 'open'
    assert (quiet.ok, quiet.status, quiet.found) == (True, 'ok \u00b7 no data', [])
    assert quiet.ran == ('dengage:email', 'dengage:inapp', 'dengage:onsite', 'dengage:whatsapp') + ACCESS + ('dengage:no-data',), \
        'only the channels switched off are cleared; push and sms read nothing and keep their cards'
    assert state['sources']['dengage'] == 'ok \u00b7 no data'
    assert by_key(state)['dengage-silence|-/dengage|sms']['status'] == 'open', 'a pass that read nothing closes no card'
    assert channel_row(state['dengage'], 'sms')['last'] == iso(NOW - 75 * HOUR), 'the last send seen is kept from the pass before'


def test_an_empty_channel_is_named_in_the_status_and_only_the_channels_read_are_cleared():
    # given
    api = Api({'push': daily() + [raw('big', 'Spring sale', 2, 52_340, 48_112)]})

    # when
    sweep = swept(api, channels='push, sms, email')

    # then
    assert (sweep.result.ok, sweep.result.status) == (True, 'ok \u00b7 no data for sms, email')
    assert sweep.result.ran == ('dengage:push', 'dengage:inapp', 'dengage:onsite', 'dengage:whatsapp') + ACCESS
    assert [c['key'] for c in sweep.result.found] == ['dengage-send|-/dengage|push-big']


def test_a_channel_the_api_user_may_not_read_raises_an_access_card_and_keeps_its_cards(store: Store):
    # given
    api = Api({'push': daily() + [raw('big', 'Spring sale', 2, 52_340, 48_112)], 'sms': daily(days=25, first=3)})

    # when
    with board(store, api, instances={'main': dict(VALUES, channels='push, sms')}) as c:
        c.run(NOW)
        api.denied = {'sms': NO_SMS}
        c.run(NOW + 900)
        refused = store.load()
        api.denied = {}
        c.run(NOW + 1800)
        back = store.load()
    sweep = swept(Api({'push': daily()}, denied={'push': '<html>Forbidden</html>'}), channels='push, email')
    lost = swept(Api({'push': daily(), 'sms': daily()}, lost=('sms',)), channels='push, sms')

    # then
    assert (lost.result.ok, lost.result.status) == (True, 'ok \u00b7 no access to sms')
    assert [(c['label'], c['ranKey'], c['permission']) for c in lost.result.found] == [
        ('Dengage cannot read sms: the API user is invalid or lacks the needed permission', 'dengage:access:sms', 'the needed permission')], \
        'a 401 on a read after a good sign-in is either a dead token or a missing permission'
    assert channel_row(lost.sections['dengage'], 'sms')['noAccess'] == 'the needed permission'
    cards = by_key(refused)
    access = cards['dengage-access|-/dengage|sms']
    assert refused['sources']['dengage'] == 'ok \u00b7 no access to sms'
    assert (access['status'], access['sev'], access['ranKey'], access['label']) == ('open', 'warn', 'dengage:access:sms', 'Dengage cannot read sms: the API user lacks SmsSendsRead')
    assert access['permission'] == 'SmsSendsRead' and 'SmsSendsRead' in access['what']
    assert cards['dengage-silence|-/dengage|sms']['status'] == 'open', 'a refused channel is never read as no sends: its cards stay open'
    assert cards['dengage-send|-/dengage|push-big']['status'] == 'open'
    assert channel_row(refused['dengage'], 'sms')['noAccess'] == 'SmsSendsRead' and channel_row(refused['dengage'], 'sms')['last'] == iso(NOW - 75 * HOUR)
    assert by_key(back)['dengage-access|-/dengage|sms']['status'] != 'open' and back['sources']['dengage'] == 'ok'
    assert by_key(back)['dengage-silence|-/dengage|sms']['status'] == 'open'
    assert (sweep.result.ok, sweep.result.status) == (True, 'ok \u00b7 no access to push \u00b7 no data for email')
    assert [c['label'] for c in sweep.result.found] == ['Dengage cannot read push: the API user lacks the needed permission']
    assert 'dengage:push' not in sweep.result.ran and 'dengage:email' not in sweep.result.ran and set(ACCESS) <= set(sweep.result.ran)


def test_a_refused_tool_call_names_the_missing_permission(store: Store):
    # given
    api = Api({'sms': daily()}, denied={'sms': NO_SMS})
    lost = Api({'sms': daily()}, lost=('sms',))

    # when
    text, err = dengage_box(store, api, instances={'main': dict(VALUES, channels='sms')}).call('dengage', {'op': 'summary'})
    client.TOKENS.clear()
    gone, gone_err = dengage_box(store, lost, instances={'main': dict(VALUES, channels='sms')}).call('dengage', {'op': 'summary'})

    # then
    assert err and text == 'Dengage HTTP 403 on /sends/sms: the API user lacks SmsSendsRead; Dengage said: You may not do this'
    assert gone_err and gone == 'Dengage HTTP 401 on /sends/sms: the API user is invalid or lacks the needed permission'
    assert len(lost.logins) == 2, 'the 401 came after a good sign-in and a fresh one'


@pytest.mark.parametrize('given', ['three', '15', '-12.5', 'nan', 'inf', '1e400'])
def test_a_utc_offset_that_is_not_a_number_of_hours_from_minus_12_to_14_fails_the_pass_the_test_and_the_tool(store: Store, given):
    # given
    api = Api({'push': [raw('s1', 'Spring sale', 2, 900, 880)]})
    values = dict(VALUES, **{'utc-hours': given})

    # when
    sweep = swept(api, **{'utc-hours': given})
    tested = runtime_of(store, api, instances={}).connection_test(dengage.PLUGIN, None, values, 5, NOW)
    text, err = dengage_box(store, api, instances={'main': values}).call('dengage', {})

    # then
    assert (sweep.result.ok, sweep.result.status) == (False, f'error: {BAD_OFFSET}')
    assert (tested['ok'], tested['summary']) == (False, BAD_OFFSET)
    assert err and text == BAD_OFFSET
    assert api.logins == [] and api.calls == []


@pytest.mark.parametrize('given, hours', [('-12', -12), ('14', 14), (' 5.5 ', 5.5), ('', 0)])
def test_a_utc_offset_from_minus_12_to_14_is_taken(given, hours):
    # when
    d = client.Dengage.of(dict(VALUES, **{'utc-hours': given}), Api())

    # then
    assert d.offset == hours


def test_the_keys_never_go_to_a_plain_http_address(store: Store):
    # given
    api = Api({'push': [raw('s1', 'Spring sale', 2, 900, 880)]})
    plain = dict(VALUES, url='http://dengage.example.com/rest')

    # when
    sweep = swept(api, url=plain['url'])
    tested = runtime_of(store, api, instances={}).connection_test(dengage.PLUGIN, None, plain, 5, NOW)

    # then
    assert (sweep.result.ok, sweep.result.status) == (False, f'error: {BAD_URL}')
    assert (tested['ok'], tested['summary']) == (False, BAD_URL)
    assert api.logins == [] and api.calls == []


def test_a_pass_that_outruns_its_budget_stops_with_a_fixed_reason(monkeypatch):
    # given
    api = Api({'push': daily()})
    timeouts: list[float] = []

    def slow(method, url, headers, data, timeout):
        timeouts.append(timeout)
        time.sleep(0.15)
        return api(method, url, headers, data, timeout)

    monkeypatch.setattr(check, 'BUDGET_S', 0.4)

    # when
    started = time.monotonic()
    sweep = dengage.run(Ctx(slow))
    took = time.monotonic() - started

    # then
    assert not sweep.result.ok and sweep.result.status.startswith('error: push: Dengage reads stopped after ') and sweep.result.status.endswith(' calls: the 0.4 s budget is spent')
    assert took < 1.5 and sweep.sections == {} and all(t <= 0.4 for t in timeouts)
    assert check.BUDGET_S < 240 and dengage.TOOL_BUDGET_S < 120


def test_channels_zone_and_address_come_from_the_instance_fields():
    # when
    plain = client.Dengage.of({'userkey': USERKEY, 'password': PASSWORD}, Api())
    tuned = client.Dengage.of({'url': 'https://dev-api.example.com/rest/', 'userkey': USERKEY, 'password': PASSWORD, 'channels': 'sms, fax, PUSH, sms', 'utc-hours': '0'}, Api())
    unknown = client.Dengage.of({'userkey': USERKEY, 'password': PASSWORD, 'channels': 'fax'}, Api())

    # then
    assert (plain.url, plain.watched, plain.offset) == ('https://api.dengage.com/rest', ('push', 'email', 'sms'), 0)
    assert (tuned.url, tuned.watched, tuned.offset) == ('https://dev-api.example.com/rest', ('sms', 'push'), 0)
    assert unknown.watched == ('push', 'email', 'sms')


def test_the_tool_is_offered_only_while_an_instance_serves_and_checks_count_as_its_calls(store: Store):
    # given
    api = Api({'push': [raw('p1', 'Sale push', 2, 52_340, 48_112, 310, at_now=TOOL_NOW)]})
    tool = dengage.PLUGIN.tools[0]
    tools = PluginTools(runtime_of(store, api, instances={}))

    # when
    unset = [d['name'] for d in tools.definitions()]
    add_instance(store, 'main', primary=True, enabled=False, **VALUES)
    tools.runtime.refresh()
    off = [d['name'] for d in tools.definitions()]
    serving = PluginTools(runtime_of(store, api))
    b = toolbox(store, serving)
    on = {d['name']: d for d in b.definitions()}
    answer, failed = b.call('dengage', {'op': 'sends', 'channel': 'push'})
    refused, refused_failed = b.call('dengage', {'op': 'delete'})
    for _ in range(tool.per_run):
        last, capped = b.call('dengage', {})
    usage = toolusage.summary([], 7, '2026-10-20T00:00:00Z', [{'name': 'dengage', 'ok': True, 'ms': 800, 'at': '2026-10-23T10:15:00Z'}], plugin_tools=serving)

    # then
    assert (tool.kind, tool.scan, tool.per_run, tool.instance_input) == ('read', True, 6, 'instance')
    assert 'dengage' in on and 'dengage' not in off and 'dengage' not in unset
    assert set(tool.input_schema['properties']) == {'op', 'channel', 'hours', 'days', 'contains'} and tool.input_schema['additionalProperties'] is False
    assert all(w in tool.description for w in ('never an instruction', 'op sends', 'op summary', 'relayedShare'))
    assert failed is False and '"id": "p1"' in answer and '"relayedShare": 0.9192' in answer
    assert refused_failed is True and 'sends, summary' in refused and capped is True
    assert {t['name']: t for t in usage['tools']}['dengage']['byCheck'] == {'dengage': {'runs': 1, 'errors': 0}}
    assert PASSWORD not in json.dumps([answer, b.calls], default=str)


def test_admins_add_an_instance_with_its_keys_and_never_see_them(store: Store):
    # when
    with board(store, Api(), instances={}) as c:
        made = c.instances.create('dengage', 'main', {'userkey': USERKEY, 'password': PASSWORD}, True, ALICE).view()
        listed = [i.view() for i in c.instances.all('dengage')]

    # then
    assert made['secrets'] == {'userkey': {'set': True, 'from': None}, 'password': {'set': True, 'from': None}} and listed == [made]
    assert made['values'] == {'url': 'https://api.dengage.com/rest', 'utc-hours': '0', 'questions': 'everyone'}, 'a new instance reads Dengage dates in UTC unless told otherwise'
    assert USERKEY not in json.dumps(listed) and PASSWORD not in json.dumps(listed)
    assert USERKEY not in json.dumps(list(store.db['instances'].find({}, {'_id': 0})), default=str)


def test_the_app_runs_the_dengage_check_once_an_instance_serves(store: Store):
    # given
    api = Api({'push': daily() + [raw('big', 'Spring sale', 2, 52_340, 48_112)]})

    # when
    with board(store, api) as c:
        jobs = {j.name: j for j in c.sweeper.jobs()}
        c.run(NOW)
    state = store.load()
    card = by_key(state)['dengage-send|-/dengage|push-big']

    # then
    assert (jobs['dengage.dengage'].period, jobs['dengage.dengage'].offset) == (900, 885)
    assert state['sources']['dengage'] == 'ok' and card['status'] == 'open' and card['links'] == [{'label': 'Dengage', 'url': APP}]
    assert [(r['part'], r['plugin'], 'cluster' in r) for r in state['dengage']] == [('summary', 'dengage', False), ('channel', 'dengage', False), ('recent', 'dengage', False)]


def test_without_an_instance_no_dengage_job_is_registered(store: Store):
    # when
    with board(store, Api(), instances={}) as c:
        names = [j.name for j in c.sweeper.jobs()]

    # then
    assert 'dengage.dengage' not in names


def test_dengage_identity_and_manifest():
    # given
    registry = Registry.of(dengage.PLUGIN, directory=HOME, core_tools=CORE_TOOLS, core_knowledge=prompt.core_anchors())

    # when
    got = registry.get('dengage')
    manifest = describe(dengage.PLUGIN, icon=True)
    fields = {f.key: f for f in dengage.PLUGIN.fields}
    probe, tool, rules = dengage.PLUGIN.checks[0], dengage.PLUGIN.tools[0], dengage.PLUGIN.rules[0]

    # then
    assert got is not None and (got.state, got.reason) == ('ok', None) and got.plugin is dengage.PLUGIN and got.drops == ()
    assert 'skopax.plugins.dengage' in BUILTIN and Registry.load({}, entry_points=False).get('dengage').state == 'ok'
    assert json.loads(json.dumps(manifest, allow_nan=False)) == manifest
    assert (manifest['id'], manifest['name'], manifest['version'], manifest['category'], manifest['author'], manifest['licence'], manifest['core'], manifest['edition']) == (
        'dengage', 'Dengage', '0.1.0', 'marketing', 'Skopax', 'AGPL-3.0-only', '>=0.1,<0.7', 'community')
    assert card_types_view(dengage.PLUGIN) == [{'sig': 'dengage-send', 'label': 'Dengage sends', 'alias': ''}, {'sig': 'dengage-delivery', 'label': 'Dengage delivery', 'alias': ''},
                                               {'sig': 'dengage-silence', 'label': 'Dengage silence', 'alias': ''}, {'sig': 'dengage-access', 'label': 'No access', 'alias': 'no-access'}]
    assert probe.own_findings is True and dengage.PLUGIN.migrations == ()
    assert manifest['setup']['docs'] == 'https://dev.dengage.com/reference/authentication' and len(manifest['setup']['steps']) == 5
    assert manifest['capabilities'] == {'hosts': ['@url'], 'secrets': ['userkey', 'password'], 'agentReads': False} and manifest['uses'] == []
    assert [(f.key, f.kind, f.required, f.ui) for f in fields.values()] == [('url', 'url', True, True), ('userkey', 'secret', True, False), ('password', 'secret', True, False),
                                                                             ('channels', 'text', False, False), ('utc-hours', 'text', False, False)]
    assert (probe.id, probe.period, probe.offset, probe.ran, probe.sections, probe.tool, probe.once) == ('dengage', 900, 885, ('dengage',), ('dengage',), 'dengage', False)
    assert probe.section_help['dengage']
    assert (rules.id, rules.label, set(rules.help) == set(rules.defaults)) == ('rules', 'Dengage', True)
    assert (tool.name, tool.kind, tool.per_run, tool.scan, tool.instance_input, tool.reach) == ('dengage', 'read', 6, True, 'instance', ())
    assert manifest['panels'] == [] and registry.icon('dengage').startswith('<svg')
    assert [k.id for k in dengage.PLUGIN.knowledge] == ['dengage'] and len(dengage.PLUGIN.knowledge[0].text.splitlines()) <= 40
    assert dengage.PLUGIN.client is None and dengage.PLUGIN.max_instances is None and dengage.PLUGIN.probe_targets is None


def test_dengage_defaults_read_dates_in_utc():
    # then
    assert {f.key: f.default for f in dengage.PLUGIN.fields} == {'url': 'https://api.dengage.com/rest', 'userkey': '', 'password': '', 'channels': '', 'utc-hours': '0'}
    assert {s.key: s.default for s in dengage.PLUGIN.settings} == {}
    assert dict(dengage.PLUGIN.rules[0].defaults) == {'event_min_selected': 1000, 'card_hours': 24, 'delivery_min_selected': 500, 'usual_share': 0.95, 'relay_drop': 0.7,
                                                      'blacklist_gap': 0.15, 'blacklist_floor': 0.05, 'baseline_min_sends': 5, 'campaign_min_sends': 5, 'silence_hours': 48, 'silence_active_days': 15}


def test_dengage_connection_test_reports_ok_and_failures(store: Store):
    # given
    login = f'{URL}/login'
    cases = {'ok': Api(), 'refused': FakeHttp({('POST', login): HttpError(401, login)}), 'unreachable': FakeHttp({('POST', login): Unreachable('down')}),
             'blank': FakeHttp({('POST', login): {'expires_in': 3600}}), 'no-sms': Api(denied={'sms': NO_SMS}), 'unnamed': Api(denied={'push': '{}'}),
             'lost': Api(lost=('sms',))}
    values = dict(VALUES, channels='push, sms')

    # when
    got = {name: runtime_of(store, http, instances={}).connection_test(dengage.PLUGIN, None, values, 5, NOW) for name, http in cases.items()}

    # then
    assert (got['ok']['ok'], got['ok']['summary']) == (True, 'Dengage \u00b7 signed in')
    assert (got['refused']['ok'], got['refused']['summary']) == (False, msg('connection.rejected', status=401))
    assert (got['unreachable']['ok'], got['unreachable']['summary']) == (False, msg('connection.unreachable'))
    assert (got['blank']['ok'], got['blank']['summary']) == (False, 'Dengage /login answered without an access token')
    assert (got['no-sms']['ok'], got['no-sms']['summary']) == (False, 'Dengage cannot read sms: the API user lacks SmsSendsRead')
    assert (got['unnamed']['ok'], got['unnamed']['summary']) == (False, 'Dengage cannot read push: the API user lacks the needed permission')
    assert (got['lost']['ok'], got['lost']['summary']) == (False, 'Dengage cannot read sms: the API user is invalid or lacks the needed permission'), \
        'a 401 on a read after the sign-in passed is two-sided; only a 401 on the sign-in itself is connection.rejected'
    assert cases['ok'].logins == [{'userkey': USERKEY, 'password': PASSWORD}]
    assert [(c['path'], c['query']['limit']) for c in cases['ok'].calls] == [('/rest/sends/push', '100'), ('/rest/sends/sms', '100')], 'one cheap read per watched channel, at the lowest limit Dengage accepts'
    assert PASSWORD not in json.dumps(got) and USERKEY not in json.dumps(got)


def test_dengage_second_instance_is_scoped(store: Store):
    # given
    main, east = Api({'push': daily()}), Api({'push': daily() + [raw('big', 'Spring sale', 2, 52_340, 48_112)]})

    def by_host(method, url, headers, data, timeout):
        return (east if url.startswith(EAST_URL) else main)(method, url, headers, data, timeout)

    # when
    with board(store, by_host, instances={'main': VALUES, 'east': dict(VALUES, url=EAST_URL)}) as c:
        c.run(NOW)
    state = store.load()
    card = next(a for a in state['anomalies'] if a.get('sendId') == 'big')

    # then
    assert (card['cluster'], card['ranKey']) == ('east', 'dengage:push-east')
    assert state['sources']['dengage-east'] == 'ok' and state['sources']['dengage'] == 'ok'
    assert card['links'] == [{'label': 'Dengage', 'url': APP}]
    assert sorted((r.get('cluster') or '', r['part']) for r in state['dengage'] if r['part'] != 'recent') == [('', 'channel'), ('', 'summary'), ('east', 'channel'), ('east', 'summary')]
    assert len(main.logins) == len(east.logins) == 1, 'each instance signs in on its own'


def test_the_excluded_share_gap_defaults_to_fifteen_points_and_a_value_someone_set_is_kept():
    # given
    jump = raw('jump', 'Spring sale', 2, 40_000, 36_000, 5_200)
    api = Api({'push': daily() + [jump]})

    # when
    byDefault = swept(api).result.found
    strict = swept(api, rules={'blacklist_gap': 0.05}).result.found

    # then
    assert check.RULES['blacklist_gap'] == 0.15 and dict(dengage.PLUGIN.rules[0].defaults)['blacklist_gap'] == 0.15
    assert [c['sig'] for c in byDefault] == ['dengage-send']
    assert [c['sig'] for c in strict] == ['dengage-delivery'] and strict[0]['problem'] == 'excluded'


def test_dengage_rules_come_from_the_rule_set(store: Store):
    # given
    api = Api({'push': daily() + [raw('low', 'Spring sale', 2, 40_000, 20_000, 800)]})

    # when
    with board(store, api) as c:
        c.run(NOW)
        before = by_key(store.load())
        c.runtime.live.set('dengage.rules', '{"relay_drop": 0.5}', ALICE)
        c.run(NOW + 900)
        after = by_key(store.load())

    # then
    assert before['dengage-delivery|-/dengage|push-low']['status'] == 'open' and 'dengage-send|-/dengage|push-low' not in before
    assert after['dengage-delivery|-/dengage|push-low']['status'] != 'open' and after['dengage-send|-/dengage|push-low']['status'] == 'open'


def old_card(key: str, sig: str, sub: str, sev: str = 'warn') -> dict:
    at = iso(NOW - HOUR)
    return {'key': key, 'src': 'dengage', 'sig': sig, 'ns': '-', 'svc': 'dengage', 'sub': sub, 'sev': sev, 'now': 1, 'label': 'stored card', 'what': 'stored before the switch-over',
            'short': f'{sev.upper()} stored card', 'status': 'open', 'firstSeen': at, 'lastSeen': at}


def test_a_full_read_closes_old_cards_kept_under_the_bare_check_key_and_a_refused_channel_keeps_them(store: Store):
    # given
    gone = old_card('dengage-send|-/dengage|push-gone', 'dengage-send', 'push-gone', 'info')
    stored = old_card('dengage-delivery|-/dengage|sms-old', 'dengage-delivery', 'sms-old')
    store.save_state(stored_state(gone, stored), None)
    api = Api({'push': daily(), 'sms': daily()}, denied={'sms': NO_SMS})

    # when
    with board(store, api, instances={'main': dict(VALUES, channels='push, sms')}) as c:
        c.run(NOW)
        refused = store.load()
        api.denied = {}
        c.run(NOW + 900)
        full = store.load()

    # then
    assert refused['sources']['dengage'] == 'ok \u00b7 no access to sms'
    assert by_key(refused)[gone['key']]['status'] == 'open' and by_key(refused)[stored['key']]['status'] == 'open', 'while a channel is refused the bare key stays out of ran'
    assert full['sources']['dengage'] == 'ok'
    assert by_key(full)[gone['key']]['status'] == 'cleared' and by_key(full)[stored['key']]['status'] == 'cleared', 'a full read clears the cards stored under the bare check key'


def test_a_read_cut_at_the_row_cap_says_so_and_keeps_that_channel_open(monkeypatch):
    # given
    monkeypatch.setattr(client, 'MAX_ROWS', 4)
    monkeypatch.setattr(client, 'PAGE', 2)
    fresh = [raw(f'p{k}', 'Spring sale', 1 + k, 2_000, 1_900) for k in range(6)]

    # when
    sweep = swept(Api({'push': fresh, 'sms': daily(days=3)}, min_limit=1), channels='push, sms')

    # then
    assert (sweep.result.ok, sweep.result.status) == (True, 'ok \u00b7 read 4 push sends, more not read')
    assert 'dengage:push' not in sweep.result.ran and 'dengage' not in sweep.result.ran and 'dengage:sms' in sweep.result.ran, 'the cut channel closes none of its cards'
    assert channel_row(sweep.sections['dengage'])['moreNotRead'] is True and 'moreNotRead' not in channel_row(sweep.sections['dengage'], 'sms')


def test_a_tool_read_cut_at_the_row_cap_names_the_channels_it_could_not_read_whole(monkeypatch):
    # given
    monkeypatch.setattr(client, 'MAX_ROWS', 4)
    monkeypatch.setattr(client, 'PAGE', 2)
    api = Api({'push': [raw(f'p{k}', 'Spring sale', 1 + k, 2_000, 1_900) for k in range(9)], 'sms': daily(days=1)}, min_limit=1)
    d = dengage_of(api, 'push, sms')

    # when
    summed = client.read(d, client.wanted({'op': 'summary', 'days': 2}), NOW)
    listed = client.read(dengage_of(api, 'push, sms'), client.wanted({}), NOW)
    whole = client.read(dengage_of(api, 'sms'), client.wanted({'op': 'summary'}), NOW)

    # then
    assert summed['moreNotRead'] == ['push'] and summed['channels']['push']['total']['n'] == 4
    assert listed['moreNotRead'] == ['push']
    assert 'moreNotRead' not in whole
    assert 'moreNotRead' in dengage.PLUGIN.tools[0].description


def test_an_unknown_channel_name_is_named_and_closes_no_cards(store: Store):
    # given
    api = Api({'push': daily() + [raw('big', 'Spring sale', 2, 52_340, 48_112)], 'sms': daily(days=25, first=3)})

    # when
    with board(store, api, instances={'main': dict(VALUES, channels='push, sms')}) as c:
        c.run(NOW)
        changed = c.change('main', {'channels': 'push, smss'})
        c.run(NOW + 900)
    state = store.load()
    tested = runtime_of(store, api, instances={}).connection_test(dengage.PLUGIN, None, dict(VALUES, channels='push, smss'), 5, NOW)
    sweep = swept(api, channels='push, smss')
    only_typos = swept(api, channels='smss, Fax')

    # then
    assert changed['values']['channels'] == 'push, smss' and state['sources']['dengage'] == 'ok \u00b7 unknown channel smss'
    assert by_key(state)['dengage-silence|-/dengage|sms']['status'] == 'open', 'a mistyped channel is not read as switched off'
    assert (tested['ok'], tested['summary']) == (False, 'Dengage has no channel smss; the channels are push, email, sms, inapp, onsite, whatsapp')
    assert sweep.result.ran == ('dengage:push', 'dengage:access:push')
    assert only_typos.result.status.startswith('ok \u00b7 unknown channels smss, fax') and 'dengage:email' not in only_typos.result.ran
    assert runtime_of(store, api, instances={}).connection_test(dengage.PLUGIN, None, dict(VALUES, channels='smss, fax'), 5, NOW)['summary'] == \
        'Dengage has no channels smss, fax; the channels are push, email, sms, inapp, onsite, whatsapp'


def test_one_channel_that_fails_is_named_in_the_status_and_the_others_keep_their_keys():
    # given
    before = [{'part': 'channel', 'channel': 'sms', 'last': iso(NOW - 3 * HOUR), 'sends24h': 1, 'days': {}}]
    api = Api({'push': daily() + [raw('big', 'Spring sale', 2, 52_340, 48_112)]}, broken={'sms': 502})

    # when
    out = swept(api, before=before, channels='push, sms')
    every = swept(Api({}, broken={'push': 502, 'sms': 502}), channels='push, sms').result

    # then
    r = out.result
    assert (r.ok, r.status) == (True, 'error: sms: Dengage HTTP 502 on /sends/sms')
    assert check.data_key('push') in r.ran and check.access_key('push') in r.ran
    assert not {check.CHECK, check.data_key('sms'), check.access_key('sms')} & set(r.ran)
    assert 'dengage-send|-/dengage|push-big' in [c['key'] for c in r.found]
    assert channel_row(out.sections['dengage'], 'sms')['last'] == iso(NOW - 3 * HOUR), 'the failed channel keeps its last good row'
    assert (every.ok, every.status) == (False, 'error: push: Dengage HTTP 502 on /sends/push; sms: Dengage HTTP 502 on /sends/sms')


def layer_copy(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / 'icon.svg').write_text((HOME / 'icon.svg').read_text(encoding='utf-8'), encoding='utf-8')
    (tmp_path / 'layer_dengage.py').write_text("import dataclasses\nfrom skopax.plugins import dengage\n\nPLUGIN = dataclasses.replace(dengage.PLUGIN, name='Dengage layer copy')\n",
                                               encoding='utf-8')
    info = tmp_path / 'layer_pack-1.0.dist-info'
    info.mkdir()
    (info / 'METADATA').write_text('Metadata-Version: 2.1\nName: layer-pack\nVersion: 1.0\n', encoding='utf-8')
    (info / 'entry_points.txt').write_text('[skopax.plugins]\ndengage = layer_dengage:PLUGIN\n', encoding='utf-8')
    monkeypatch.syspath_prepend(str(tmp_path))


REFUSED_COPY = 'Dengage layer copy was not loaded: id dengage is already loaded (the copy that wins is built in)'


def test_an_installed_package_that_registers_its_own_dengage_is_refused_and_the_built_in_serves(tmp_path, monkeypatch, caplog):
    # given
    layer_copy(tmp_path, monkeypatch)

    # when
    with caplog.at_level(logging.WARNING, logger='skopax.registry'):
        reg = Registry.load({})
    copies = [(x.origin, x.plugin.name, x.state, x.reason) for x in reg.loaded() if x.id == 'dengage']

    # then
    assert copies == [('builtin', 'Dengage', 'ok', None), ('entry-point', 'Dengage layer copy', 'broken', REFUSED_COPY)]
    assert reg.get('dengage').plugin is dengage.PLUGIN and dengage.PLUGIN in reg.usable()
    assert [r.getMessage() for r in caplog.records if 'dengage' in r.getMessage()] == [REFUSED_COPY], 'the refusal is logged once at boot'


def test_the_app_boots_with_a_layer_copy_installed_and_keeps_serving_the_saved_instance(tmp_path, monkeypatch, store: Store):
    # given
    layer_copy(tmp_path, monkeypatch)
    kit_instance(store, dengage.PLUGIN, 'main', sealer_key=master_key(store.db, SETTINGS), primary=True, **dict(VALUES, **{'utc-hours': '3'}))
    app = create_app(SETTINGS, store, transport=Api({'push': daily()}))

    # when
    with TestClient(app) as c:
        notices = c.get('/api/admin/notices', headers=AUTH).json()['items']
        rows = [p for p in c.get('/api/admin/plugins', headers=AUTH).json()['plugins'] if p['id'] == 'dengage']
        served = app.state.registry.get('dengage')

    # then
    assert served.origin == 'builtin' and served.plugin is dengage.PLUGIN
    assert {'id': 'plugin:dengage', 'level': 'warn', 'text': REFUSED_COPY} in notices
    assert [(r['origin'], r.get('state'), r['reason']) for r in rows] == [('builtin', 'ok', None), ('package', None, REFUSED_COPY)]
    [main] = rows[0]['instances']
    assert (main['name'], main['values']['utc-hours'], main['secrets']['userkey']['set'], main['secrets']['password']['set']) == ('main', '3', True, True)


def test_an_instance_row_without_a_utc_offset_reads_dengage_dates_in_utc_and_a_saved_offset_is_kept(store: Store):
    # given
    given = {k: v for k, v in VALUES.items() if k != 'utc-hours'}

    # when
    runtime = runtime_of(store, Api(), instances={'main': given, 'east': dict(VALUES, **{'utc-hours': '3'})})
    plain, kept = (runtime.instances.get('dengage', name).info().config for name in ('main', 'east'))

    # then
    assert plain['utc-hours'] == '0' and client.Dengage.of(plain, Api()).offset == 0
    assert kept['utc-hours'] == '3' and client.Dengage.of(kept, Api()).offset == 3


def test_delivery_and_silence_cards_carry_every_field_a_card_sentence_reads():
    # given
    low = raw('low', 'Spring sale', 2, 40_000, 20_000, 800)
    listed = raw('listed', 'Newsletter', 3, 40_000, 33_000, 10_000)

    # when
    found = {c['key']: c for c in swept(Api({'push': daily() + [low, listed]})).result.found}
    [silent] = swept(Api({'sms': daily(days=25, first=3)}), channels='sms').result.found

    # then
    relay, black = found['dengage-delivery|-/dengage|push-low'], found['dengage-delivery|-/dengage|push-listed']
    assert {k: relay[k] for k in ('sig', 'sendName', 'channel', 'selected', 'relayed', 'excluded', 'share', 'usualShare', 'problem')} == {
        'sig': 'dengage-delivery', 'sendName': 'Spring sale', 'channel': 'push', 'selected': 40_000, 'relayed': 20_000, 'excluded': 800, 'share': 0.5,
        'usualShare': 0.92, 'problem': 'relay'}
    assert (black['problem'], black['usualKept']) == ('excluded', 0.95)
    assert {k: silent[k] for k in ('sig', 'channel', 'quietHours', 'activeDays', 'lastSendAt')} == {
        'sig': 'dengage-silence', 'channel': 'sms', 'quietHours': 75, 'activeDays': 25, 'lastSendAt': iso(NOW - 75 * HOUR)}
