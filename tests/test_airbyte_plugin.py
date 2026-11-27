import json
import threading
import time
from dataclasses import dataclass, field

import pytest
from fastapi.testclient import TestClient

from skopax import remote
from skopax.app import create_app
from skopax.contract import InstanceInfo, ReadError
from skopax.engine import iso
from skopax.instances import Instances, PluginState
from skopax.investigator import prompt
from skopax.investigator.tools import CORE_TOOLS
from skopax.investigator.toolset import ToolSet
from skopax.kube import Kube
from skopax.messages import msg
from skopax.plugins import airbyte
from skopax.plugins.airbyte import engine
from skopax.plugintools import PluginTools
from skopax.registry import BUILTIN, Registry, describe
from skopax.remote import HttpError
from skopax.runtime import Runtime
from skopax.sealer import Sealer, master_key
from skopax.store import Store
from tests.conftest import settings, state as state_of
from tests.fake_http import FakeHttp
from tests.plugin_kit import add_instance
from tests.test_instances import pem
from tests.test_investigator_tools import box

NOW = 1_792_224_000.0
KEY = 'airbyte-key'
URL = 'https://airbyte.example.com'
EAST_URL = 'https://airbyte-east.example.com'
BASE = URL + engine.API
EAST_BASE = EAST_URL + engine.API
WS = '0a1b2c3d-0000-4000-8000-00000000a001'
ORDERS = '1a2b3c4d-1111-4111-8111-000000000001'
EVENTS = '1a2b3c4d-1111-4111-8111-000000000002'
LEDGER = '1a2b3c4d-1111-4111-8111-000000000003'
SHOP = '11111111-2222-3333-4444-555555555555'
PAYMENTS = '22222222-3333-4444-5555-666666666666'
OTHER = '33333333-4444-5555-6666-777777777777'
CLICKS = '44444444-5555-6666-7777-888888888888'
GONE = '55555555-6666-7777-8888-999999999999'
SECRET = 'airbyte-client-secret-9f2c'
VALUES = {'url': URL, 'client-id': 'cid', 'client-secret': SECRET}
ALICE = 'alice@example.com'
DENIED = 'the application lacks the needed permission (it reads only what its owning user can access)'
INVALID = 'the application is invalid or lacks the needed permission (it reads only what its owning user can access)'
ACCESS_C = 'airbyte:access:connections'
ACCESS_J = 'airbyte:access:jobs'


def job(jid: int, status: str, ago_s: float, dur: str | None = 'PT10M', rows: int = 1000, conn: str = ORDERS) -> dict:
    return {'jobId': jid, 'status': status, 'jobType': 'sync', 'startTime': iso(NOW - ago_s), 'duration': dur, 'rowsSynced': rows, 'bytesSynced': rows * 300,
            'lastUpdatedAt': iso(NOW - ago_s + 600), 'connectionId': conn}


def conn(cid: str, name: str, schedule: str | None) -> dict:
    return {'id': cid, 'name': name, 'status': 'active', 'schedule': schedule, 'workspaceId': WS}


def data() -> list[dict]:
    return [
        {'conn': conn(ORDERS, 'Orders to Warehouse', 'cron 0 */2 * * *'),
         'jobs': [job(9, 'running', 5400, None, 400)] + [job(8 - i, 'succeeded', 7200 * (i + 1), 'PT12M', 900) for i in range(5)]},
        {'conn': conn(EVENTS, 'Events to Lake', None),
         'jobs': [job(20, 'succeeded', 3000, 'PT8M', 500), job(19, 'succeeded', 10200, 'PT9M', 450)]},
        {'conn': conn(SHOP, 'Postgres to S3 shop', 'basic 1h'),
         'jobs': [job(31, 'failed', 4000, 'PT40M', 0), job(30, 'succeeded', 7600, 'PT5M', 20000)]},
        {'conn': conn(LEDGER, 'Ledger Changes', None), 'jobs': []},
    ]


def listed(cid: str, name: str, status: str = 'active', schedule: dict | None = None) -> dict:
    return {'connectionId': cid, 'name': name, 'status': status, 'workspaceId': WS, **({'schedule': schedule} if schedule else {})}


def routes(base: str, conns: list[dict], jobs: dict, token_answer=None) -> dict:
    return {
        ('POST', f'{base}/applications/token'): token_answer if token_answer is not None else {'access_token': 'tok'},
        ('GET', f'{base}/connections'): {'data': conns},
        ('GET', f'{base}/jobs'): lambda q, b: {'data': jobs.get(q.get('connectionId', [''])[0], [])[:int(q['limit'][0])]},
    }


def fake_api(token_answer=None) -> FakeHttp:
    conns = [listed(ORDERS, 'Orders to Warehouse', schedule={'scheduleType': 'cron', 'cronExpression': '0 */2 * * *'}),
             listed(SHOP, 'Postgres to S3 shop', schedule={'scheduleType': 'basic', 'basicTiming': 'Every 1 HOURS'}),
             listed(OTHER, 'Some other sync')]
    jobs = {ORDERS: [job(9, 'running', 5400, None, 400)], SHOP: [job(31, 'failed', 4000, 'PT40M', 0, conn=SHOP)]}
    return FakeHttp(routes(BASE, conns, jobs, token_answer))


@dataclass
class Ctx:
    transport: object
    config: dict = field(default_factory=lambda: dict(VALUES))
    now: float = NOW
    name: str = 'main'
    rules_given: dict = field(default_factory=dict)
    cards: list = field(default_factory=list)

    @property
    def instance(self) -> InstanceInfo:
        return InstanceInfo(self.name, True, None, self.config)

    def http(self):
        return self.transport

    def rules(self, set_id: str) -> dict:
        assert set_id == 'rules'
        return {**engine.RULES, **self.rules_given}

    def open_cards(self, plugin_wide: bool = False) -> list[dict]:
        return list(self.cards)


def tool_runtime(store, http, instances: dict | None = None) -> Runtime:
    reg = Registry.of(airbyte.PLUGIN)
    runtime = Runtime(reg, PluginState(store.db), Instances(store.db, reg, lambda: Sealer(KEY)), http)
    for i, (name, values) in enumerate(({'main': VALUES} if instances is None else instances).items()):
        add_instance(store, airbyte.PLUGIN, name, sealer_key=KEY, primary=i == 0, **values)
    runtime.refresh()
    return runtime


def airbyte_box(store: Store, http: FakeHttp, instances: dict | None = None, **kw):
    return box(store, plugin_tools=PluginTools(tool_runtime(store, http, instances)), **kw)


def call(b, **args):
    text, err = b.call('airbyte', args)
    return (text if err else json.loads(text)), err


def app_with(store, tmp_path, http, instances: dict | None = None):
    sealing = master_key(store.db, settings())
    for i, (name, values) in enumerate(({'main': VALUES} if instances is None else instances).items()):
        add_instance(store, airbyte.PLUGIN, name, sealer_key=sealing, primary=i == 0, **values)
    return TestClient(create_app(settings(), store, registry=Registry.of(airbyte.PLUGIN), transport=http, kube=Kube(root=tmp_path)))


def by_key(state: dict) -> dict:
    return {a['key']: a for a in state['anomalies']}


def test_only_the_three_read_calls_are_allowed():
    # given
    http = fake_api()
    client = engine.Airbyte(URL, 'cid', 'secret', http)

    # then
    for method, path in (('DELETE', '/jobs'), ('POST', '/jobs'), ('PATCH', '/connections'), ('POST', '/connections')):
        with pytest.raises(ValueError, match='read-only allowlist'):
            client.call(method, path)
    assert http.calls == []


def test_the_pass_mints_a_token_lists_connections_and_reads_the_watched_jobs():
    # given
    http = fake_api()
    client = engine.Airbyte(URL, 'cid', 'secret', http)

    # when
    read = engine.airbyte_pass(client, engine.watched_of(f' orders to WAREHOUSE , {SHOP.upper()}, {GONE}'), 15)
    got, cut = read.data, read.cut
    every = engine.airbyte_pass(client, engine.watched_of(''), 15).data

    # then
    assert {d['conn']['id'] for d in got} == {ORDERS, SHOP, GONE} and cut is False
    assert [d for d in got if d['conn']['id'] == GONE] == [{'conn': {'id': GONE, 'name': GONE, 'status': 'not listed', 'schedule': None, 'workspaceId': None}, 'jobs': [], 'missing': True}]
    assert (read.access, read.listed) == ({'connections': None, 'jobs': None}, 3)
    assert not any(GONE in c[1] for c in http.hits(f'{BASE}/jobs'))
    assert {d['conn']['id'] for d in every} == {ORDERS, SHOP, OTHER}
    token_call = http.hits(f'{BASE}/applications/token')[0]
    assert token_call[3] == {'client_id': 'cid', 'client_secret': 'secret', 'grant_type': 'client_credentials'}
    assert 'Authorization' not in token_call[2]
    gets = [c for c in http.calls if c[0] == 'GET']
    assert gets and all(c[2]['Authorization'] == 'Bearer tok' for c in gets)
    assert {c[0] for c in http.calls} <= {'GET', 'POST'} and len(http.hits(f'{BASE}/applications/token')) == 2
    assert all('workspaceIds' not in c[1] for c in http.hits(f'{BASE}/connections'))
    assert [d['conn'] for d in got if d['conn']['id'] == ORDERS] == [{'id': ORDERS, 'name': 'Orders to Warehouse', 'status': 'active', 'schedule': 'cron 0 */2 * * *', 'workspaceId': WS}]


def test_a_refused_token_fails_with_a_short_reason_without_the_secret():
    # given
    ctx = Ctx(fake_api(HttpError(401, 'x')), dict(VALUES, **{'client-secret': 'very-secret'}))

    # when
    sweep = airbyte.run(ctx)

    # then
    result = sweep.result
    assert (result.name, result.ok, result.status, result.ran) == ('airbyte', True, 'ok \u00b7 no access to connections', (ACCESS_C,)), 'a refusal is never an error or no data (P5-C111)'
    assert [(a['ranKey'], a['sev'], a['label']) for a in result.found] == [(ACCESS_C, 'warn', 'Airbyte cannot read connections: the client id and secret are invalid or lack the needed permission')]
    assert 'HTTP 401' in result.found[0]['what']
    assert 'very-secret' not in json.dumps(result.found) and sweep.sections == {}


def test_long_runs_and_failed_syncs_are_flagged_as_warnings():
    # when
    found, rows = engine.evaluate(data(), NOW, URL, engine.RULES)

    # then
    by = {x['key']: x for x in found}
    assert by['airbyte|Orders to Warehouse|long-run']['sev'] == 'warn'
    assert by['airbyte|Postgres to S3 shop|failed']['sev'] == 'warn'
    assert 'airbyte|Events to Lake|long-run' not in by and len(found) == 2
    assert [r['name'] for r in rows] == ['Events to Lake', 'Ledger Changes', 'Orders to Warehouse', 'Postgres to S3 shop']
    assert not any('suspects' in x for x in found)


def test_a_long_run_names_its_usual_runs_and_the_failed_sync_its_end_in_the_zone_label():
    # when
    found, rows = engine.evaluate(data(), NOW, URL, engine.RULES)

    # then
    by = {x['key']: x for x in found}
    long_run, failed = by['airbyte|Orders to Warehouse|long-run'], by['airbyte|Postgres to S3 shop|failed']
    assert long_run['what'] == ('Orders to Warehouse: job 9 has been running for 90 min since 06:30 UTC, 400 rows so far; its 5 recent successful runs took 12 min and 900 rows (median). '
                                'Airbyte retries a failed attempt inside the same job and the public API has no attempt count, so a run this long is most likely a retry loop.')
    assert long_run['short'] == 'WARN Airbyte Orders to Warehouse running 90 min (usual 12 min)'
    assert failed['what'] == ('Postgres to S3 shop: job 31 failed at 07:33 UTC after 40 min (started 06:53 UTC, 0 rows); its 1 recent successful runs took 5 min and 20,000 rows (median). '
                              'Airbyte retries a failed attempt inside the same job and the public API has no attempt count, so a run this long is most likely a retry loop.')
    assert failed['short'] == 'WARN Airbyte Postgres to S3 shop sync failed after 40 min'
    assert (long_run['src'], long_run['sig'], long_run['ns'], long_run['svc'], long_run['label'], long_run['unit']) == ('airbyte', 'airbyte-long-run', '-', 'Orders to Warehouse', 'sync running long', 'dur')
    assert (long_run['connId'], long_run['jobId'], long_run['workspaceId']) == (ORDERS, 9, WS)
    assert all(ord(ch) < 128 for x in found for ch in x['what'] + x['short'])
    orders = next(r for r in rows if r['id'] == ORDERS)
    assert orders['url'] == f'{URL}/workspaces/{WS}/connections/{ORDERS}/timeline'
    assert orders['jobUrl'] == f'{URL}/workspaces/{WS}/connections/{ORDERS}/timeline?jobId=9&openLogs=true'
    assert (orders['status'], orders['durS'], orders['usualS'], orders['okRuns'], orders['rows'], orders['end']) == ('running', 5400, 720, 5, 400, None)
    assert 'kind' not in orders and 'cluster' not in orders and 'plugin' not in orders


def test_links_cover_airbyte_only():
    # given
    x = engine.evaluate(data(), NOW, URL, engine.RULES)[0][0]
    info = InstanceInfo('main', True, None, {'url': URL + '/'})

    # when
    found = airbyte.links(x, info)

    # then
    assert [link['label'] for link in found] == ['Airbyte connection timeline', 'Airbyte job 9 logs']
    assert found[0]['url'] == f'{URL}/workspaces/{WS}/connections/{ORDERS}/timeline'
    assert airbyte.links({'src': 'prom', 'sig': 'err'}, info) is None
    assert airbyte.links({'src': 'airbyte', 'connId': ORDERS}, info) == []


def test_the_app_runs_the_airbyte_check_once_an_instance_serves(store: Store, tmp_path):
    # given
    http = fake_api()

    # when
    with app_with(store, tmp_path, http) as c:
        jobs = {j.name: j for j in c.app.state.scheduler.jobs}
        tiles = {p['id']: p for p in c.get('/api/server').json()['plugins']}
        c.app.state.sweeper.run('airbyte', 'airbyte', NOW)
        usage = {t['name']: t for t in c.get('/api/tool-usage?days=1').json()['tools']}
    state = store.load()
    by = by_key(state)

    # then
    assert (jobs['airbyte.airbyte'].period, jobs['airbyte.airbyte'].offset) == (900, 300)
    assert (usage['airbyte']['calls'], usage['airbyte']['byTrigger'], usage['airbyte']['byCheck']) == (1, {'check': 1}, {'airbyte': {'runs': 1, 'errors': 0}})
    assert tiles['airbyte']['passes'] == ['airbyte']
    assert tiles['airbyte']['pages'] == [{'tab': 'airbyte', 'label': 'Airbyte', 'group': 'observe', 'icon': '/plugins/airbyte/icon.svg'}]
    assert state['sources']['airbyte'] == 'ok'
    assert by['airbyte|Orders to Warehouse|long-run']['links'][0] == {'label': 'Airbyte connection timeline', 'url': f'{URL}/workspaces/{WS}/connections/{ORDERS}/timeline'}
    assert by['airbyte|Postgres to S3 shop|failed']['sev'] == 'warn'
    assert {r['name'] for r in state['airbyte']} == {'Orders to Warehouse', 'Postgres to S3 shop', 'Some other sync'}
    assert all(r['plugin'] == 'airbyte' and 'cluster' not in r for r in state['airbyte'])
    assert [(r['name'], r['ok']) for r in store.check_runs_since('2000-01-01T00:00:00Z')] == [('airbyte', True)], 'the check run is kept for the Tools tab'


def test_without_an_instance_no_airbyte_job_is_registered(store: Store, tmp_path):
    # when
    with app_with(store, tmp_path, fake_api(), instances={}) as c:
        names = [j.name for j in c.app.state.scheduler.jobs]

    # then
    assert 'airbyte.airbyte' not in names


def test_an_incomplete_job_is_a_retry_in_progress_not_a_failed_sync():
    # given
    def watched(last: dict) -> list[dict]:
        return [{'conn': conn(ORDERS, 'Orders to Warehouse', None), 'jobs': [last] + [job(8 - i, 'succeeded', 7200 * (i + 1), 'PT12M', 900) for i in range(5)]}]

    # when
    retrying = engine.evaluate(watched(job(9, 'incomplete', 3 * 3600, 'PT2H50M', 400)), NOW, URL, engine.RULES)[0]
    fresh = engine.evaluate(watched(job(9, 'incomplete', 300, 'PT4M', 0)), NOW, URL, engine.RULES)[0]
    failed = engine.evaluate(watched(job(9, 'failed', 3 * 3600, 'PT2H50M', 400)), NOW, URL, engine.RULES)[0]

    # then
    assert [(x['key'], x['sev']) for x in retrying] == [('airbyte|Orders to Warehouse|long-run', 'warn')]
    assert 'status incomplete: its last attempt failed and Airbyte retries' in retrying[0]['what'] and 'has been running for 3 h' in retrying[0]['what']
    assert fresh == []
    assert [(x['key'], x['sev']) for x in failed] == [('airbyte|Orders to Warehouse|failed', 'warn')]


def test_a_connection_without_a_name_is_named_by_its_id():
    # given
    nameless = [{'conn': {'id': ORDERS, 'name': None, 'status': 'active', 'schedule': None, 'workspaceId': WS}, 'jobs': [job(9, 'failed', 600, 'PT5M', 0)]}]

    # when
    found, rows = engine.evaluate(nameless, NOW, URL, engine.RULES)

    # then
    assert [(x['key'], x['svc']) for x in found] == [(f'airbyte|{ORDERS}|failed', ORDERS)]
    assert rows[0]['name'] == ORDERS


def test_airbyte_identity_and_manifest():
    # given
    registry = Registry.load({}, entry_points=False, core_tools=CORE_TOOLS, core_knowledge=prompt.core_anchors())

    # when
    found = registry.get('airbyte')
    manifest = describe(airbyte.PLUGIN, icon=True)
    fields = {f.key: f for f in airbyte.PLUGIN.fields}
    check, tool, rules = airbyte.PLUGIN.checks[0], airbyte.PLUGIN.tools[0], airbyte.PLUGIN.rules[0]

    # then
    assert 'skopax.plugins.airbyte' in BUILTIN and found is not None and found.state == 'ok' and found.plugin is airbyte.PLUGIN
    assert json.loads(json.dumps(manifest, allow_nan=False)) == manifest
    assert (manifest['id'], manifest['name'], manifest['version'], manifest['category'], manifest['author'], manifest['licence'], manifest['core'], manifest['edition']) == (
        'airbyte', 'Airbyte', '0.1.0', 'infrastructure', 'Skopax', 'AGPL-3.0-only', '>=0.1,<0.8', 'community')
    assert manifest['capabilities'] == {'hosts': ['@url'], 'secrets': ['client-secret'], 'agentReads': False} and manifest['uses'] == []
    assert [(f.key, f.kind, f.required, f.ui) for f in fields.values()] == [('url', 'url', True, True), ('client-id', 'text', True, False), ('client-secret', 'secret', True, False),
                                                                             ('connections', 'text', False, False), ('connection-match', 'regex', False, False),
                                                                             ('severities', 'text', False, False), ('workspaces', 'text', False, False), ('ca', 'pem', False, False),
                                                                             ('ca-pinned', 'bool', False, False)]
    assert (check.id, check.period, check.offset, check.ran, check.sections, check.tool, check.once) == ('airbyte', 900, 300, ('airbyte',), ('airbyte',), 'airbyte', False)
    assert check.section_help['airbyte']
    assert (rules.id, rules.label, set(rules.help) == set(rules.defaults)) == ('rules', 'Airbyte syncs', True)
    assert (tool.name, tool.kind, tool.per_run, tool.scan, tool.instance_input, tool.reach) == ('airbyte', 'read', 6, True, 'instance', ())
    assert manifest['panels'] == [{'tab': 'airbyte', 'script': 'panel.js', 'fragment': None, 'label': 'Airbyte', 'group': 'observe'}]
    assert registry.icon('airbyte').startswith('<svg')
    assert [k.id for k in airbyte.PLUGIN.knowledge] == ['airbyte'] and len(airbyte.PLUGIN.knowledge[0].text.splitlines()) <= 40
    assert airbyte.PLUGIN.client is None and airbyte.PLUGIN.max_instances is None and airbyte.PLUGIN.agent_reads == ()


def test_airbyte_defaults_are_neutral():
    # then
    assert {f.key: f.default for f in airbyte.PLUGIN.fields} == {'url': '', 'client-id': '', 'client-secret': '', 'connections': '', 'connection-match': '', 'severities': '', 'workspaces': '', 'ca': '', 'ca-pinned': 'false'}
    assert {s.key: s.default for s in airbyte.PLUGIN.settings} == {}
    assert dict(airbyte.PLUGIN.rules[0].defaults) == {'jobs': 15, 'long_x': 2, 'long_slack_s': 1200, 'no_base_s': 3600, 'failed_keep_s': 93600}


def test_airbyte_connection_test_reports_ok_and_failures(store):
    # given
    unreachable = FakeHttp({('POST', f'{BASE}/applications/token'): remote.Unreachable('down')})
    no_list, no_jobs, broken, list_key, jobs_key = fake_api(), fake_api(), fake_api(), fake_api(), fake_api()
    no_list.routes[('GET', f'{BASE}/connections')] = HttpError(403, f'{BASE}/connections')
    no_jobs.routes[('GET', f'{BASE}/jobs')] = HttpError(403, f'{BASE}/jobs')
    broken.routes[('GET', f'{BASE}/connections')] = HttpError(500, f'{BASE}/connections')
    list_key.routes[('GET', f'{BASE}/connections')] = HttpError(401, f'{BASE}/connections')
    jobs_key.routes[('GET', f'{BASE}/jobs')] = HttpError(401, f'{BASE}/jobs')
    cases = {'ok': fake_api(), 'refused': fake_api(HttpError(401, 'x')), 'unreachable': unreachable, 'no-token': fake_api({}), 'no-list': no_list, 'no-jobs': no_jobs, 'broken': broken,
             'token-403': fake_api(HttpError(403, 'x')), 'list-401': list_key, 'jobs-401': jobs_key}

    # when
    got = {}
    for name, http in cases.items():
        runtime = tool_runtime(store, http, instances={})
        got[name] = runtime.connection_test(airbyte.PLUGIN, None, dict(VALUES), 5, NOW)

    # then
    assert (got['ok']['ok'], got['ok']['summary']) == (True, 'Airbyte \u00b7 3 connections')
    assert (got['refused']['ok'], got['refused']['summary']) == (False, msg('connection.rejected', status=401)), 'a 401 in Test keeps the credentials text (P5-C119)'
    assert [got[k]['summary'] for k in ('list-401', 'jobs-401')] == [f'Airbyte refused the {p} read (HTTP 401): {INVALID}' for p in ('connections', 'jobs')], 'the token mint worked, so a later 401 is two-sided (P5-C127)'
    assert got['token-403']['summary'] == 'Airbyte refused the token request (HTTP 403): the client id and secret were refused'
    assert (got['unreachable']['ok'], got['unreachable']['summary']) == (False, msg('connection.unreachable'))
    assert (got['no-token']['ok'], got['no-token']['summary']) == (False, 'POST /applications/token answered without an access token')
    assert (got['no-list']['ok'], got['no-list']['summary']) == (False, f'Airbyte refused the connections read (HTTP 403): {DENIED}')
    assert (got['no-jobs']['ok'], got['no-jobs']['summary']) == (False, f'Airbyte refused the jobs read (HTTP 403): {DENIED}')
    assert (got['broken']['ok'], got['broken']['summary']) == (False, msg('connection.status', status=500))
    asked = cases['ok'].hits(f'{BASE}/connections')
    assert len(asked) == 1 and 'limit=100' in asked[0][1] and asked[0][2]['Authorization'] == 'Bearer tok'
    jobs = cases['ok'].hits(f'{BASE}/jobs')
    assert len(jobs) == 1 and f'connectionId={ORDERS}' in jobs[0][1] and 'limit=1' in jobs[0][1], 'Test also reads the jobs of one connection'


def test_airbyte_connection_test_fits_inside_core_wait(store):
    # given
    http = fake_api()
    timeouts: list[float] = []

    def timed(method, url, headers, data, timeout):
        timeouts.append(timeout)
        return http(method, url, headers, data, timeout)

    # when
    got = tool_runtime(store, timed, instances={}).connection_test(airbyte.PLUGIN, None, dict(VALUES), 15, NOW)

    # then
    assert got['ok'] and len(timeouts) == 3 and all(0 < t <= airbyte.TEST_BUDGET_S < 15 for t in timeouts)


def test_airbyte_connection_test_says_when_the_first_page_is_full(store):
    # given
    many = [listed(f'{i:08d}-2222-3333-4444-555555555555', f'Sync {i}') for i in range(100)]
    http = FakeHttp(routes(BASE, many, {}))

    # when
    got = tool_runtime(store, http, instances={}).connection_test(airbyte.PLUGIN, None, dict(VALUES), 5, NOW)

    # then
    assert (got['ok'], got['summary']) == (True, 'Airbyte \u00b7 100+ connections')


def test_airbyte_second_instance_is_scoped(store: Store, tmp_path):
    # given
    http = fake_api()
    http.routes.update(routes(EAST_BASE, [listed(CLICKS, 'Clicks to Lake')], {CLICKS: [job(41, 'failed', 600, 'PT5M', 0, conn=CLICKS)]}))

    # when
    with app_with(store, tmp_path, http, instances={'main': VALUES, 'east': dict(VALUES, url=EAST_URL)}) as c:
        c.app.state.sweeper.run('airbyte', 'airbyte', NOW)
    state = store.load()
    east = by_key(state)['airbyte|Clicks to Lake|failed@east']

    # then
    assert (east['cluster'], east['ranKey']) == ('east', f'airbyte:{CLICKS}-east')
    assert state['sources']['airbyte-east'] == 'ok' and state['sources']['airbyte'] == 'ok'
    assert east['links'][0]['url'] == f'{EAST_URL}/workspaces/{WS}/connections/{CLICKS}/timeline'
    assert [r['cluster'] for r in state['airbyte'] if r['name'] == 'Clicks to Lake'] == ['east']
    assert 'cluster' not in by_key(state)['airbyte|Orders to Warehouse|long-run']


def test_airbyte_rules_come_from_the_rule_set(store: Store, tmp_path):
    # given
    jobs = {ORDERS: [job(9, 'running', 1000, None, 400)] + [job(8 - i, 'succeeded', 7200 * (i + 1), 'PT12M', 900) for i in range(5)]}
    http = FakeHttp(routes(BASE, [listed(ORDERS, 'Orders to Warehouse')], jobs))

    # when
    with app_with(store, tmp_path, http) as c:
        c.app.state.sweeper.run('airbyte', 'airbyte', NOW)
        before = set(by_key(store.load()))
        c.app.state.runtime.live.set('airbyte.rules', '{"long_x": 1, "long_slack_s": 60}', ALICE)
        c.app.state.sweeper.run('airbyte', 'airbyte', NOW)
        after = by_key(store.load())

    # then
    assert 'airbyte|Orders to Warehouse|long-run' not in before
    assert after['airbyte|Orders to Warehouse|long-run']['status'] == 'open'
    assert 'running for 16 min' in after['airbyte|Orders to Warehouse|long-run']['what']


def test_the_jobs_rule_sets_how_many_jobs_the_pass_reads():
    # given
    http = fake_api()

    none, huge = fake_api(), fake_api()

    # when
    airbyte.run(Ctx(http, rules_given={'jobs': 4.0}))
    airbyte.run(Ctx(none, rules_given={'jobs': 0}))
    airbyte.run(Ctx(huge, rules_given={'jobs': 5000}))

    # then
    queries = [c[1] for c in http.hits(f'{BASE}/jobs')]
    assert queries and all('limit=4&' in q for q in queries)
    assert all('limit=1&' in c[1] for c in none.hits(f'{BASE}/jobs')), 'a zero rule still reads the last job'
    assert all(f'limit={engine.JOBS_MAX}&' in c[1] for c in huge.hits(f'{BASE}/jobs')) and engine.JOBS_MAX == 100


def test_airbyte_probe_target_is_the_health_endpoint_of_each_instance(store):
    # given
    runtime = tool_runtime(store, fake_api(), instances={'main': VALUES, 'east': dict(VALUES, url=EAST_URL + '/', ca=pem())})

    # when
    targets = sorted(runtime.probe_targets(), key=lambda t: t['name'])

    # then
    assert [(t['name'], t['url'], t['instance'], 'pem' in t) for t in targets] == [('airbyte-east', f'{EAST_URL}/api/v1/health', 'east', True), ('airbyte-main', f'{URL}/api/v1/health', 'main', False)]


def test_airbyte_tool_status_is_off_unreachable_from_the_probe_and_on_otherwise(store):
    # given
    at = iso(NOW)
    target = {'name': 'airbyte-main', 'plugin': 'airbyte', 'instance': 'main', 'url': f'{URL}/api/v1/health'}
    unset = ToolSet(tool_runtime(store, fake_api(), instances={}))
    serving = ToolSet(tool_runtime(store, fake_api()))

    # when
    off = unset.status('airbyte', {'at': at, 'targets': [{**target, 'ok': False, 'error': 'dns: gaierror'}]})
    cut = serving.status('airbyte', {'at': at, 'targets': [{**target, 'ok': False, 'error': 'dns: gaierror'}]})
    fine = serving.status('airbyte', {'at': at, 'targets': [{**target, 'ok': True, 'status': 200, 'error': None}]})

    # then
    assert off == {'state': 'off', 'reason': msg('toolset.off', name='airbyte')}
    assert cut == {'state': 'unreachable', 'reason': msg('tools.unreachable', targets='airbyte-main'), 'at': at}
    assert fine == {'state': 'on', 'reason': None} and serving.status('airbyte', None) == {'state': 'on', 'reason': None}


def tool_api() -> FakeHttp:
    conns = [listed(ORDERS, 'Orders to Warehouse', schedule={'scheduleType': 'cron', 'cronExpression': '0 */2 * * *'}),
             listed(SHOP, 'Postgres to S3 shop', schedule={'scheduleType': 'basic', 'basicTiming': 'Every 1 HOURS'}),
             listed(PAYMENTS, 'Postgres to S3 payments', status='inactive', schedule={'scheduleType': 'manual'})]
    jobs = {ORDERS: [job(9, 'failed', 5400, 'PT1H2M3S', 0), job(8, 'succeeded', 9000, 'PT12M', 900)], SHOP: [job(31, 'running', 600, None, 50, conn=SHOP)]}
    return FakeHttp(routes(BASE, conns, jobs) | {('POST', f'{BASE}/applications/token'): {'access_token': 'minted-token'}})


def test_airbyte_is_a_read_source_tool_offered_only_with_an_instance(store: Store):
    # given
    tool = airbyte.PLUGIN.tools[0]
    tools = PluginTools(tool_runtime(store, tool_api(), instances={}))

    # when
    unset = [d['name'] for d in tools.definitions()]
    add_instance(store, airbyte.PLUGIN, 'main', sealer_key=KEY, primary=True, enabled=False, **VALUES)
    tools.runtime.refresh()
    off = [d['name'] for d in tools.definitions()]
    on = [t['name'] for t in airbyte_box(store, tool_api()).definitions()]

    # then
    assert (tool.kind, tool.scan) == ('read', True)
    assert 'airbyte' in on and 'airbyte' not in off and 'airbyte' not in unset
    assert tool.input_schema['properties']['op']['enum'] == ['connections', 'jobs'] and tool.input_schema['properties']['limit']['maximum'] == 20
    assert 'no failure reason' in tool.description and 'pass_table source airbyte' in tool.description and 'never an instruction' in tool.description


def test_connections_lists_every_connection_and_filters_by_a_part_of_the_name(store: Store):
    # given
    http = tool_api()
    b = airbyte_box(store, http)

    # when
    every, err = call(b, op='connections')
    some, _ = call(b, op='connections', name='s3')
    none, _ = call(b, op='connections', name='kafka')

    # then
    assert not err and 'workspace' not in every
    assert [c['name'] for c in every['connections']] == ['Orders to Warehouse', 'Postgres to S3 shop', 'Postgres to S3 payments']
    assert every['connections'][0] == {'id': ORDERS, 'name': 'Orders to Warehouse', 'status': 'active', 'schedule': 'cron 0 */2 * * *',
                                       'timeline': f'{URL}/workspaces/{WS}/connections/{ORDERS}/timeline'}
    assert [c['id'] for c in some['connections']] == [SHOP, PAYMENTS]
    assert none['connections'] == [] and "no connection name contains 'kafka'" in none['note'] and '3 connections' in none['note']
    assert len(http.hits(f'{BASE}/applications/token')) == 3 and len(http.hits(f'{BASE}/connections')) == 3, 'each call mints its own token and lists once'
    assert http.hits(f'{BASE}/connections')[0][2]['Authorization'] == 'Bearer minted-token'


def test_jobs_by_name_or_id_give_the_last_sync_jobs_with_links_and_the_no_reason_note(store: Store):
    # given
    http = tool_api()
    b = airbyte_box(store, http)

    # when
    by_name, err = call(b, op='jobs', connection='orders to', limit=2)
    by_id, _ = call(b, op='jobs', connection=SHOP.upper())

    # then
    assert not err and by_name['connection']['id'] == ORDERS and by_name['note'] == engine.NO_REASON
    assert by_name['timeline'] == f'{URL}/workspaces/{WS}/connections/{ORDERS}/timeline'
    assert by_name['jobs'][0] == {'id': 9, 'status': 'failed', 'start': iso(NOW - 5400), 'durationS': 3723, 'duration': by_name['jobs'][0]['duration'], 'rows': 0, 'bytes': 0,
                                  'link': f'{URL}/workspaces/{WS}/connections/{ORDERS}/timeline?jobId=9&openLogs=true'}
    assert by_name['jobs'][0]['duration'] and [j['id'] for j in by_name['jobs']] == [9, 8]
    assert by_id['connection']['id'] == SHOP and by_id['jobs'][0] == {'id': 31, 'status': 'running', 'start': iso(NOW - 600), 'durationS': None, 'duration': None,
                                                                       'rows': 50, 'bytes': 15000, 'link': by_id['jobs'][0]['link']}
    queries = [c[1] for c in http.hits(f'{BASE}/jobs')]
    assert 'jobType=sync' in queries[0] and 'orderBy=createdAt%7CDESC' in queries[0] and 'limit=2' in queries[0] and 'limit=10' in queries[1]


def test_an_ambiguous_name_lists_the_choices_and_an_unknown_or_missing_connection_is_refused(store: Store):
    # given
    b = airbyte_box(store, tool_api())

    # when
    ambiguous = call(b, op='jobs', connection='postgres')
    unknown = call(b, op='jobs', connection='kafka sync')
    no_id = call(b, op='jobs', connection='99999999-2222-3333-4444-555555555555')
    missing = call(b, op='jobs')

    # then
    assert not ambiguous[1]
    found = ambiguous[0]
    assert [m['id'] for m in found['matches']] == [SHOP, PAYMENTS]
    assert found['note'] == "2 connections match 'postgres', each with its last sync job: call jobs again with the id of the one you mean for more of its jobs"
    assert found['matches'][0]['lastJob']['status'] == 'running' and found['matches'][0]['status'] == 'active'
    assert 'lastJob' not in found['matches'][1] and found['matches'][1]['status'] == 'inactive'
    assert unknown[1] and 'op connections lists them' in unknown[0] and 'or the application cannot see it' in unknown[0]
    assert no_id[1] and 'no connection 99999999' in no_id[0] and 'or the application cannot see it' in no_id[0]
    assert missing[1] and missing[0] == 'connection is required'


def test_off_without_an_instance_and_capped_per_run(store: Store):
    # given
    off = airbyte_box(store, FakeHttp({}), instances={})

    # when
    refused = call(off, op='connections')
    b = airbyte_box(store, tool_api())
    answers = [call(b, op='connections') for _ in range(airbyte.PLUGIN.tools[0].per_run + 1)]

    # then
    assert refused == ('no tool named airbyte', True)
    assert [err for _, err in answers] == [False] * 6 + [True]
    assert 'made its 6 calls in this run' in answers[-1][0]


def test_an_airbyte_failure_is_a_tool_error_that_never_carries_the_secret_or_token(store: Store):
    # given
    http = FakeHttp({('POST', f'{BASE}/applications/token'): {'access_token': 'minted-token'}, ('GET', f'{BASE}/connections'): HttpError(401, f'{BASE}/connections')})
    b = airbyte_box(store, http, run_id='run-1')

    # when
    text, err = call(b, op='connections')

    # then
    assert err and text == f'GET /connections HTTP 401: {INVALID}'
    seen = json.dumps([b.calls, list(store.activity.find({}, {'_id': 0}))], default=str)
    assert SECRET not in seen and 'minted-token' not in seen


def test_the_app_hands_its_airbyte_instance_to_the_investigator(store: Store, tmp_path):
    # given
    http = tool_api()

    # when
    with app_with(store, tmp_path, http) as c:
        tools = c.app.state.investigator.sources.plugin_tools
        offered = [d['name'] for d in tools.definitions()]
        listed_now = tools.call('airbyte', {'op': 'connections'}, NOW)

    # then
    assert 'airbyte' in offered and tools.cap('airbyte') == 6 and tools.scan('airbyte')
    assert [x['id'] for x in listed_now['connections']] == [ORDERS, SHOP, PAYMENTS]


def test_a_name_seventeen_connections_share_reads_the_last_job_of_the_first_fifteen_only(store: Store):
    # given
    tracker = [listed(f'{i:08d}-2222-3333-4444-555555555555', f'Shop {i} Tracker Source to S3 Lake') for i in range(17)]
    http = FakeHttp({('POST', f'{BASE}/applications/token'): {'access_token': 'minted-token'}, ('GET', f'{BASE}/connections'): {'data': tracker},
                     ('GET', f'{BASE}/jobs'): lambda q, b: {'data': [job(int(q['connectionId'][0][:8]), 'succeeded', 3600)]}})

    # when
    out, err = call(airbyte_box(store, http), op='jobs', connection='Tracker')

    # then
    assert not err
    assert len(out['matches']) == 17 and all('lastJob' in m for m in out['matches'][:15]) and not any('lastJob' in m for m in out['matches'][15:])
    assert out['note'].startswith("17 connections match 'Tracker', each with its last sync job (the first 15): ")
    assert len(http.hits(f'{BASE}/jobs')) == 15


def test_watched_entries_keep_their_text_and_drop_repeats():
    # then
    assert engine.watched_of(' Orders to Warehouse , orders to warehouse,, Typo Name ') == ['Orders to Warehouse', 'Typo Name']
    assert engine.watched_of('') == []


def test_a_watched_entry_the_listing_lacks_gets_a_placeholder_row_and_a_status_note():
    # given
    typo = Ctx(fake_api(), dict(VALUES, connections='Orders to Warehouse, orders-to-warehouse'))
    only_typo = Ctx(fake_api(), dict(VALUES, connections='orders-to-warehouse'))

    # when
    mixed = airbyte.run(typo)
    alone = airbyte.run(only_typo)

    # then
    assert (mixed.result.ok, mixed.result.status, mixed.result.ran) == (True, 'ok \u00b7 no data for orders-to-warehouse', (f'airbyte:{ORDERS}', ACCESS_C, ACCESS_J))
    assert [x['key'] for x in mixed.result.found] == ['airbyte|Orders to Warehouse|long-run']
    placeholder = next(r for r in mixed.sections['airbyte'] if r['id'] == 'orders-to-warehouse')
    assert placeholder == {'id': 'orders-to-warehouse', 'name': 'orders-to-warehouse', 'workspaceId': None, 'connStatus': 'not listed', 'schedule': None, 'usualS': None,
                           'usualRows': None, 'okRuns': 0, 'lastOk': None, 'url': None, 'missing': True}
    assert next(r for r in mixed.sections['airbyte'] if r['id'] == ORDERS)['status'] == 'running'
    assert (alone.result.ok, alone.result.status, alone.result.ran, alone.result.found) == (True, 'ok \u00b7 no data for orders-to-warehouse', ('airbyte:no-data', ACCESS_C), [])
    assert [r['id'] for r in alone.sections['airbyte']] == ['orders-to-warehouse']


def test_a_full_listing_says_it_was_cut():
    # given
    many = [listed(f'{i:08d}-2222-3333-4444-555555555555', f'Sync {i:03d}') for i in range(engine.PAGE * engine.PAGES)]
    http = FakeHttp(routes(BASE, many, {}))
    http.routes[('GET', f'{BASE}/connections')] = lambda q, b: {'data': many[int(q['offset'][0]):int(q['offset'][0]) + int(q['limit'][0])]}

    # when
    sweep = airbyte.run(Ctx(http))

    # then
    assert sweep.result.status == 'ok \u00b7 read 500 connections, more not read' and len(sweep.sections['airbyte']) == 500
    assert len(http.hits(f'{BASE}/connections')) == engine.PAGES


def test_a_cut_listing_closes_no_card_of_a_connection_past_the_cap():
    # given
    many = [listed(f'{i:08d}-2222-3333-4444-555555555555', f'Sync {i:03d}') for i in range(engine.PAGE * engine.PAGES)]
    past = '99999999-2222-3333-4444-555555555555'
    held = {'key': 'airbyte|Sync past|failed', 'src': 'airbyte', 'connId': past, 'svc': 'Sync past', 'status': 'open', 'ranKey': f'airbyte:{past}'}
    http = FakeHttp(routes(BASE, many, {}))
    http.routes[('GET', f'{BASE}/connections')] = lambda q, b: {'data': many[int(q['offset'][0]):int(q['offset'][0]) + int(q['limit'][0])]}

    # when
    sweep = airbyte.run(Ctx(http, cards=[held]))

    # then
    assert sweep.result.status == 'ok \u00b7 read 500 connections, more not read'
    assert 'airbyte' not in sweep.result.ran and f'airbyte:{past}' not in sweep.result.ran
    assert f'airbyte:{many[0]["connectionId"]}' in sweep.result.ran


def test_renaming_one_watched_failing_connection_shows_it_as_not_listed_and_holds_its_card(store: Store, tmp_path):
    # given
    http = fake_api()
    watched = dict(VALUES, connections='Orders to Warehouse, Postgres to S3 shop')

    # when
    with app_with(store, tmp_path, http, instances={'main': watched}) as c:
        c.app.state.sweeper.run('airbyte', 'airbyte', NOW)
        before = by_key(store.load())
        http.routes[('GET', f'{BASE}/connections')] = {'data': [listed(ORDERS, 'Orders to Warehouse'), listed(SHOP, 'Shop to Lake')]}
        c.app.state.sweeper.run('airbyte', 'airbyte', NOW + 900)
    state = store.load()
    after = by_key(state)

    # then
    assert before['airbyte|Postgres to S3 shop|failed']['status'] == 'open'
    assert state['sources']['airbyte'] == 'ok \u00b7 no data for Postgres to S3 shop'
    assert [(r['name'], r.get('connStatus'), r.get('missing')) for r in state['airbyte'] if r['name'] == 'Postgres to S3 shop'] == [('Postgres to S3 shop', 'not listed', True)]
    assert after['airbyte|Postgres to S3 shop|failed']['status'] == 'open', 'a watched connection the listing lacks keeps its card, as its no-data status says (Lens A M9)'
    assert after['airbyte|Orders to Warehouse|long-run']['status'] == 'open'


def test_renaming_the_only_watched_failing_connection_keeps_its_card_open_as_no_data(store: Store, tmp_path):
    # given
    http = fake_api()

    # when
    with app_with(store, tmp_path, http, instances={'main': dict(VALUES, connections='Postgres to S3 shop')}) as c:
        c.app.state.sweeper.run('airbyte', 'airbyte', NOW)
        http.routes[('GET', f'{BASE}/connections')] = {'data': [listed(SHOP, 'Shop to Lake')]}
        c.app.state.sweeper.run('airbyte', 'airbyte', NOW + 900)
    state = store.load()

    # then
    assert state['sources']['airbyte'] == 'ok \u00b7 no data for Postgres to S3 shop'
    assert by_key(state)['airbyte|Postgres to S3 shop|failed']['status'] == 'open', 'a pass that read no watched connection closes no card (P5-C82)'
    assert [r['name'] for r in state['airbyte']] == ['Postgres to S3 shop']


def test_a_pass_that_outruns_its_budget_names_the_unread_count_and_leaves_no_reader_running(monkeypatch):
    # given
    release = threading.Event()

    def jobs(q, b):
        if q['connectionId'][0] == ORDERS:
            release.wait(10)
        return {'data': []}

    http = fake_api()
    http.routes[('GET', f'{BASE}/jobs')] = jobs
    monkeypatch.setattr(engine, 'BUDGET_S', 0.3)

    # when
    started = time.monotonic()
    sweep = airbyte.run(Ctx(http))
    took = time.monotonic() - started
    release.set()
    for t in [t for t in threading.enumerate() if t.name.startswith(engine.READERS)]:
        t.join(5)

    # then
    assert (sweep.result.ok, sweep.result.status) == (True, 'error: jobs: 2 of 3 connections read in 0.3 s'), 'the connections read in time are still judged (P5-C129)'
    assert f'airbyte:{ORDERS}' not in sweep.result.ran and 'airbyte' not in sweep.result.ran and len(sweep.sections['airbyte']) == 3
    assert took < 3
    assert not [t for t in threading.enumerate() if t.name.startswith(engine.READERS)]
    assert engine.BUDGET_S < 240


def test_two_connections_with_one_name_get_a_card_each():
    # given
    twins = [{'conn': conn(SHOP, 'Same', None), 'jobs': [job(31, 'failed', 600, 'PT5M', 0, conn=SHOP)]},
             {'conn': conn(PAYMENTS, 'Same', None), 'jobs': [job(41, 'failed', 900, 'PT6M', 0, conn=PAYMENTS)]},
             {'conn': conn(ORDERS, 'Orders to Warehouse', None), 'jobs': [job(9, 'failed', 600, 'PT5M', 0)]}]

    # when
    found, rows = engine.evaluate(twins, NOW, URL, engine.RULES)

    # then
    assert sorted((x['key'], x['svc'], x['connId']) for x in found) == [('airbyte|Orders to Warehouse|failed', 'Orders to Warehouse', ORDERS),
                                                                         ('airbyte|Same (11111111)|failed', 'Same (11111111)', SHOP),
                                                                         ('airbyte|Same (22222222)|failed', 'Same (22222222)', PAYMENTS)]
    assert [r['name'] for r in rows] == ['Orders to Warehouse', 'Same (11111111)', 'Same (22222222)']


def test_a_malformed_job_is_skipped_and_the_other_connections_still_report():
    # given
    bad = dict(job(9, 'failed', 600, None, 0), startTime='yesterday', lastUpdatedAt=5, rowsSynced='many')
    odd = dict(job(31, 'failed', 600, None, 0, conn=SHOP), lastUpdatedAt='soon', rowsSynced='lots')
    mixed = [{'conn': conn(ORDERS, 'Orders to Warehouse', None), 'jobs': [bad, dict(job(8, 'succeeded', 7200, 'PT12M', 900), rowsSynced='many')]},
             {'conn': conn(SHOP, 'Postgres to S3 shop', None), 'jobs': [odd]},
             {'conn': conn(EVENTS, 'Events to Lake', None), 'jobs': [job(20, 'failed', 600, 'PT8M', 500, conn=EVENTS)]}]

    # when
    found, rows = engine.evaluate(mixed, NOW, URL, engine.RULES)

    # then
    by = {r['id']: r for r in rows}
    assert sorted(x['key'] for x in found) == ['airbyte|Events to Lake|failed', 'airbyte|Postgres to S3 shop|failed']
    assert 'status' not in by[ORDERS] and by[ORDERS]['usualS'] == 720 and by[ORDERS]['usualRows'] == 0
    assert (by[SHOP]['durS'], by[SHOP]['rows']) == (0, None)
    assert ', 0 rows' in next(x for x in found if x['connId'] == SHOP)['what']


def test_an_ambiguous_name_says_when_the_last_jobs_could_not_be_read(store: Store):
    # given
    http = tool_api()
    http.routes[('GET', f'{BASE}/jobs')] = HttpError(503, f'{BASE}/jobs')
    refused = tool_api()
    refused.routes[('GET', f'{BASE}/jobs')] = HttpError(403, f'{BASE}/jobs')

    # when
    out, err = call(airbyte_box(store, http), op='jobs', connection='postgres')
    denied = call(airbyte_box(store, refused), op='jobs', connection='postgres')

    # then
    assert not err and not any('lastJob' in m for m in out['matches'])
    assert [m['lastJobError'] for m in out['matches']] == ['GET /jobs HTTP 503'] * 2, 'a failed last-job read says why, per row'
    assert out['note'].endswith('; last job unreadable for 2')
    assert denied == (f'GET /jobs HTTP 403: {DENIED}', True), 'a refused last-job read is a tool error naming the permission, never a quiet gap'


def test_pass_table_offers_the_airbyte_rows_while_an_instance_serves(store: Store):
    # given
    rows = [{'id': ORDERS, 'name': 'Orders to Warehouse', 'status': 'failed', 'durS': 300, 'plugin': 'airbyte'}]
    store.save_state({**state_of(), 'serverPasses': {'airbyte': {'at': '2026-10-17T07:55:00Z', 'status': 'ok'}}, 'airbyte': rows}, None)
    b = airbyte_box(store, tool_api())

    # when
    tool = next(t for t in b.definitions() if t['name'] == 'pass_table')
    out, err = b.call('pass_table', {'source': 'airbyte'})

    # then
    assert 'airbyte' in tool['input_schema']['properties']['source']['enum'] and 'the airbyte check of Airbyte' in tool['description']
    assert not err and json.loads(out) == {'source': 'airbyte', 'passes': {'airbyte': {'at': '2026-10-17T07:55:00Z', 'status': 'ok'}}, 'rows': rows}


def same_name_listing(*ids: str) -> list[dict]:
    return [listed(i, 'Orders') for i in ids]


def same_name_api() -> FakeHttp:
    return FakeHttp(routes(BASE, same_name_listing(SHOP), {SHOP: [job(31, 'failed', 600, 'PT5M', 0, conn=SHOP)], PAYMENTS: [job(41, 'succeeded', 900, 'PT6M', 10, conn=PAYMENTS)]}))


def open_keys(store: Store) -> set[str]:
    return {a['key'] for a in store.load()['anomalies'] if a['status'] == 'open'}


def test_a_failing_connection_keeps_its_card_when_a_same_name_connection_comes_and_goes(store: Store, tmp_path):
    # given
    http = same_name_api()
    seen = []

    # when
    with app_with(store, tmp_path, http) as c:
        for at, ids in ((NOW, (SHOP,)), (NOW + 900, (SHOP, PAYMENTS)), (NOW + 1800, (SHOP,))):
            http.routes[('GET', f'{BASE}/connections')] = {'data': same_name_listing(*ids)}
            c.app.state.sweeper.run('airbyte', 'airbyte', at)
            seen.append((open_keys(store), sorted(r['name'] for r in store.load()['airbyte'])))
    cards = by_key(store.load())

    # then
    assert [keys for keys, _ in seen] == [{'airbyte|Orders|failed'}] * 3, 'the card never moves while the duplicate comes and goes'
    assert seen[1][1] == ['Orders', 'Orders (22222222)'], 'only the connection without an open card takes the id suffix'
    assert 'airbyte|Orders (11111111)|failed' not in cards and cards['airbyte|Orders|failed']['firstSeen'] == iso(NOW)


def test_a_card_opened_with_the_id_suffix_keeps_it_after_the_duplicate_goes(store: Store, tmp_path):
    # given
    http = same_name_api()
    seen = []

    # when
    with app_with(store, tmp_path, http) as c:
        for at, ids in ((NOW, (SHOP, PAYMENTS)), (NOW + 900, (SHOP,)), (NOW + 1800, (SHOP, PAYMENTS))):
            http.routes[('GET', f'{BASE}/connections')] = {'data': same_name_listing(*ids)}
            c.app.state.sweeper.run('airbyte', 'airbyte', at)
            seen.append(open_keys(store))
    cards = by_key(store.load())

    # then
    assert seen == [{'airbyte|Orders (11111111)|failed'}] * 3
    assert 'airbyte|Orders|failed' not in cards and cards['airbyte|Orders (11111111)|failed']['firstSeen'] == iso(NOW)


def test_an_open_card_label_is_kept_only_while_it_still_names_the_connection():
    # given
    renamed = [{'conn': conn(SHOP, 'Ledger', None), 'jobs': [job(31, 'failed', 600, 'PT5M', 0, conn=SHOP)]}]
    plain = [{'conn': conn(SHOP, 'Orders', None), 'jobs': [job(31, 'failed', 600, 'PT5M', 0, conn=SHOP)]}]
    other = {'key': 'airbyte|Orders|failed', 'src': 'airbyte', 'svc': 'Orders', 'connId': PAYMENTS}

    # when
    moved = engine.evaluate(renamed, NOW, URL, engine.RULES, {SHOP: 'Orders'})[0]
    kept = engine.evaluate(plain, NOW, URL, engine.RULES, {SHOP: 'Orders (11111111)'})[0]
    sweep = airbyte.run(Ctx(same_name_api(), cards=[other, {'key': 'airbyte|Orders (11111111)|failed', 'src': 'airbyte', 'svc': 'Orders (11111111)', 'connId': SHOP}]))

    # then
    assert [x['key'] for x in moved] == ['airbyte|Ledger|failed'], 'a renamed connection takes its new name'
    assert [x['key'] for x in kept] == ['airbyte|Orders (11111111)|failed']
    assert [x['key'] for x in sweep.result.found] == ['airbyte|Orders (11111111)|failed'], 'the label comes from the open card of the same connId'


def test_a_refused_airbyte_read_keeps_its_code_and_part_and_names_the_permission():
    # given
    http = fake_api()
    http.routes[('GET', f'{BASE}/jobs')] = HttpError(403, f'{BASE}/jobs')
    http.routes[('GET', f'{BASE}/connections')] = HttpError(500, f'{BASE}/connections')
    client = engine.Airbyte(URL, 'cid', 'secret', http)

    # when
    with pytest.raises(engine.AirbyteError) as jobs:
        client.call('GET', '/jobs', 'tok', connectionId=ORDERS)
    with pytest.raises(engine.AirbyteError) as listing:
        client.call('GET', '/connections', 'tok')
    with pytest.raises(engine.AirbyteError) as token:
        engine.token_of(engine.Airbyte(URL, 'cid', 'secret', fake_api(HttpError(401, 'x'))))

    # then
    assert isinstance(jobs.value, ReadError) and (jobs.value.code, jobs.value.part, jobs.value.denied, jobs.value.status) == (403, 'jobs', True, 403)
    assert jobs.value.reason == jobs.value.message == f'GET /jobs HTTP 403: {DENIED}'
    assert (listing.value.code, listing.value.part, listing.value.denied, listing.value.reason) == (500, 'connections', False, 'GET /connections HTTP 500')
    assert isinstance(listing.value.cause, HttpError)
    assert (token.value.code, token.value.part, token.value.denied) == (401, 'connections', True)
    assert token.value.reason == 'POST /applications/token HTTP 401: the client id and secret were refused', 'bad credentials are named apart from a missing permission'


def test_a_refused_jobs_read_holds_only_that_connections_cards_names_the_part_and_raises_its_access_card():
    # given
    http = fake_api()
    jobs = {ORDERS: [job(9, 'running', 5400, None, 400)]}

    def some(q, b):
        if q['connectionId'][0] == SHOP:
            return HttpError(403, f'{BASE}/jobs')
        return {'data': jobs.get(q['connectionId'][0], [])}

    http.routes[('GET', f'{BASE}/jobs')] = some

    # when
    sweep = airbyte.run(Ctx(http, dict(VALUES, connections=f'{ORDERS}, {SHOP}')))
    result = sweep.result

    # then
    assert (result.ok, result.status, result.ran) == (True, 'ok \u00b7 no access to jobs', (f'airbyte:{ORDERS}', ACCESS_C, ACCESS_J)), \
        'the refused connection keeps its key out of ran; the bare airbyte key waits for a full read (P5-C120)'
    by = {a['key']: a for a in result.found}
    assert by['airbyte|Orders to Warehouse|long-run']['ranKey'] == f'airbyte:{ORDERS}', 'a readable connection is still judged, under its own key'
    access = next(a for a in result.found if a.get('ranKey') == ACCESS_J)
    assert (access['sev'], access['label'], access['svc']) == ('warn', 'Airbyte cannot read jobs: the application lacks the needed permission', 'airbyte')
    shop = next(r for r in sweep.sections['airbyte'] if r['id'] == SHOP)
    assert shop.get('jobsDenied') is True and 'job' not in shop


def test_an_empty_listing_says_no_connection_is_visible_to_the_application():
    # given
    http = FakeHttp(routes(BASE, [], {}))

    # when
    result = airbyte.run(Ctx(http)).result

    # then
    assert (result.status, result.ran, result.found) == ('ok \u00b7 no data', ('airbyte:no-data', ACCESS_C), [])


def test_an_open_jobs_access_card_is_probed_when_the_pass_reads_no_jobs():
    # given
    http = FakeHttp(routes(BASE, [], {}))
    held = [{'key': 'no-access|-/airbyte|jobs', 'src': 'airbyte', 'sig': 'no-access', 'sub': 'jobs', 'ranKey': ACCESS_J, 'status': 'open', 'svc': 'airbyte'}]

    # when
    probed = airbyte.run(Ctx(http, cards=held)).result
    refused = FakeHttp(routes(BASE, [], {}))
    refused.routes[('GET', f'{BASE}/jobs')] = HttpError(403, f'{BASE}/jobs')
    still = airbyte.run(Ctx(refused, cards=held)).result

    # then
    assert probed.ran == ('airbyte:no-data', ACCESS_C, ACCESS_J) and probed.found == []
    assert [c[1] for c in http.hits(f'{BASE}/jobs')] and 'limit=1' in http.hits(f'{BASE}/jobs')[0][1]
    assert (still.status, still.ran) == ('ok \u00b7 no data \u00b7 no access to jobs', (ACCESS_C, ACCESS_J))
    assert [a['ranKey'] for a in still.found] == [ACCESS_J]


def test_access_cards_open_while_a_part_is_refused_and_clear_at_the_first_readable_pass(store: Store, tmp_path):
    # given
    http = fake_api()
    readable = dict(http.routes)

    def run_with(c, change):
        http.routes.clear()
        http.routes.update(readable | change)
        c.app.state.sweeper.run('airbyte', 'airbyte', NOW)
        return store.load()

    # when
    with app_with(store, tmp_path, http) as c:
        first = run_with(c, {})
        no_list = run_with(c, {('GET', f'{BASE}/connections'): HttpError(403, f'{BASE}/connections')})
        back = run_with(c, {})
        no_jobs = run_with(c, {('GET', f'{BASE}/jobs'): HttpError(403, f'{BASE}/jobs')})
        again = run_with(c, {})
        later = run_with(c, {})

    # then
    failed = 'airbyte|Postgres to S3 shop|failed'
    assert by_key(first)[failed]['status'] == 'open'
    assert no_list['sources']['airbyte'] == 'ok \u00b7 no access to connections'
    assert by_key(no_list)['no-access|-/airbyte|connections']['status'] == 'open' and by_key(no_list)[failed]['status'] == 'open'
    assert by_key(back)['no-access|-/airbyte|connections']['status'] != 'open' and back['sources']['airbyte'] == 'ok'
    assert no_jobs['sources']['airbyte'] == 'ok \u00b7 no access to jobs'
    assert by_key(no_jobs)['no-access|-/airbyte|jobs']['status'] == 'open' and by_key(no_jobs)[failed]['status'] == 'open'
    assert by_key(again)['no-access|-/airbyte|jobs']['status'] != 'open' and by_key(again)[failed]['status'] == 'open'
    assert later['sources']['airbyte'] == 'ok' and not [a for a in later['anomalies'] if a['sig'] == 'no-access' and a['status'] == 'open']


def test_a_recovered_connection_card_closes_while_another_connection_is_refused(store: Store, tmp_path):
    # given
    http = fake_api()
    readable = dict(http.routes)
    healthy = {ORDERS: [job(10, 'succeeded', 900, 'PT12M', 900)], SHOP: [job(31, 'failed', 4000, 'PT40M', 0, conn=SHOP)]}

    def recovered(q, b):
        if q['connectionId'][0] == SHOP:
            return HttpError(403, f'{BASE}/jobs')
        return {'data': healthy.get(q['connectionId'][0], [])}

    # when
    with app_with(store, tmp_path, http, instances={'main': dict(VALUES, connections=f'{ORDERS}, {SHOP}')}) as c:
        c.app.state.sweeper.run('airbyte', 'airbyte', NOW)
        first = by_key(store.load())
        http.routes[('GET', f'{BASE}/jobs')] = recovered
        c.app.state.sweeper.run('airbyte', 'airbyte', NOW)
        held = store.load()
        http.routes.update(readable)
        http.routes[('GET', f'{BASE}/jobs')] = lambda q, b: {'data': healthy.get(q['connectionId'][0], [])}
        c.app.state.sweeper.run('airbyte', 'airbyte', NOW)
        back = by_key(store.load())

    # then
    long_run, failed = 'airbyte|Orders to Warehouse|long-run', 'airbyte|Postgres to S3 shop|failed'
    assert first[long_run]['status'] == 'open' and first[failed]['status'] == 'open'
    assert held['sources']['airbyte'] == 'ok \u00b7 no access to jobs'
    assert by_key(held)[long_run]['status'] != 'open', 'a recovered connection closes on the next pass while another is refused (P5-C120)'
    assert by_key(held)[failed]['status'] == 'open', 'only the refused connection keeps its cards'
    assert back[failed]['status'] == 'open' and back['no-access|-/airbyte|jobs']['status'] != 'open'


def test_the_bare_airbyte_key_is_listed_only_on_a_pass_that_read_every_part():
    # given
    refused = fake_api()
    refused.routes[('GET', f'{BASE}/jobs')] = lambda q, b: HttpError(403, f'{BASE}/jobs') if q['connectionId'][0] == SHOP else {'data': []}
    gone = [{'key': 'airbyte|Ledger Changes|failed', 'src': 'airbyte', 'sig': 'airbyte-failed', 'connId': LEDGER, 'svc': 'Ledger Changes', 'status': 'open'}]

    # when
    full = airbyte.run(Ctx(fake_api(), cards=gone)).result
    partial = airbyte.run(Ctx(refused, cards=gone)).result

    # then
    assert full.ran == ('airbyte', f'airbyte:{ORDERS}', f'airbyte:{SHOP}', f'airbyte:{OTHER}', f'airbyte:{LEDGER}', ACCESS_C, ACCESS_J), \
        'a full pass lists the bare key for cards stored before the split, each read connection, and each open card of a connection no longer chosen'
    assert 'airbyte' not in partial.ran and f'airbyte:{SHOP}' not in partial.ran and f'airbyte:{ORDERS}' in partial.ran and f'airbyte:{LEDGER}' in partial.ran


def test_one_failing_jobs_read_holds_only_that_connection_and_the_others_are_judged():
    # given
    http = fake_api()

    def some(q, b):
        if q['connectionId'][0] == SHOP:
            return HttpError(500, f'{BASE}/jobs')
        return {'data': [job(9, 'running', 5400, None, 400)] if q['connectionId'][0] == ORDERS else []}

    http.routes[('GET', f'{BASE}/jobs')] = some

    # when
    sweep = airbyte.run(Ctx(http, dict(VALUES, connections=f'{ORDERS}, {SHOP}')))
    result = sweep.result

    # then
    assert (result.ok, result.status) == (True, 'error: jobs: GET /jobs HTTP 500')
    assert result.ran == (f'airbyte:{ORDERS}', ACCESS_C, ACCESS_J), 'the failed connection and the bare key stay out of ran (P5-C129)'
    assert [a['key'] for a in result.found] == ['airbyte|Orders to Warehouse|long-run']
    assert next(r for r in sweep.sections['airbyte'] if r['id'] == SHOP).get('jobsFailed') is True


def test_a_watched_connection_the_listing_lacks_keeps_its_open_card():
    # given
    held = [{'key': 'airbyte|Postgres to S3 shop|failed', 'src': 'airbyte', 'sig': 'airbyte-failed', 'svc': 'Postgres to S3 shop', 'connId': SHOP, 'ranKey': f'airbyte:{SHOP}', 'status': 'open'}]
    http = fake_api()
    http.routes[('GET', f'{BASE}/connections')] = {'data': [listed(ORDERS, 'Orders to Warehouse'), listed(SHOP, 'Shop to Lake')]}

    # when
    result = airbyte.run(Ctx(http, dict(VALUES, connections='Orders to Warehouse, Postgres to S3 shop'), cards=held)).result

    # then
    assert f'airbyte:{SHOP}' not in result.ran and f'airbyte:{ORDERS}' in result.ran and 'airbyte' not in result.ran
    assert result.status == 'ok \u00b7 no data for Postgres to S3 shop'


def test_a_401_after_the_token_mint_is_two_sided_in_the_tool_too():
    # given
    http = fake_api()
    http.routes[('GET', f'{BASE}/jobs')] = HttpError(401, f'{BASE}/jobs')

    # when
    with pytest.raises(ReadError) as told:
        airbyte.airbyte(Ctx(http), {'op': 'jobs', 'connection': ORDERS})

    # then
    assert (told.value.status, told.value.message) == (401, f'GET /jobs HTTP 401: {INVALID}')
