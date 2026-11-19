import dataclasses
import io
import json
import re
import ssl
import time
import urllib.error
import urllib.parse

import pytest
from fastapi.testclient import TestClient

from skopax import remote, selfreview
from skopax.app import create_app
from skopax.agent import RemoteAgent
from skopax.contract import InstanceInfo, ReadError
from skopax.engine import iso
from skopax.instances import Instances, PluginState
from skopax.investigator.tools import ToolError
from skopax.kit import elastic
from skopax.kit.elastic import REASON_LIMIT, Elastic, ElasticError, access_card
from skopax.messages import msg
from skopax.plugins import elasticsearch
from skopax.plugins.elasticsearch import checks, esql as reads
from skopax.plugintools import PluginTools
from skopax.registry import BUILTIN, Registry, describe
from skopax.remote import BODY_LIMIT, HttpError, Unreachable
from skopax.runtime import Runtime
from skopax.sealer import Sealer, master_key
from skopax.store import Store
from tests.agent_kit import AUTH, agent_boot, agent_client
from tests.conftest import settings
from tests.fake_http import FakeHttp
from tests.plugin_kit import KIT_KEY, add_instance

NOW = 1_794_207_600.0
ES_URL = 'https://es.example.com'
APM_URL = 'https://apm.example.com'
KIBANA = 'https://kibana.example.com'
QUERY = f'{ES_URL}/_query'
PRIVILEGES = f'{ES_URL}/_security/user/_has_privileges'
APM_QUERY = f'{APM_URL}/_query'
USER = 'alice@example.com'
V1_KEYS = ('elastic.logging-url', 'elastic.logging-api-key', 'elastic.apm-url', 'elastic.apm-api-key')
NS_PATTERN = r'(?:\.ds-)?app-(?P<ns>.+)-logs-.*'

LOG_ROWS = [
    {'app': 'orders-service', 'level': 'ERROR', 'msg': 'Connection refused: payments gateway', 'now': 60, 'd1': 0, 'd7': 0},
    {'app': 'checkout-service', 'level': 'ERROR', 'msg': 'Timeout on 3f2a9c1d-1111-2222 call', 'now': 40, 'd1': 10, 'd7': 5},
    {'app': 'cart-service', 'level': 'WARN', 'msg': 'slow cart list', 'now': 35, 'd1': 10, 'd7': 2},
    {'app': 'auth-service', 'level': 'WARN', 'msg': 'retrying role lookup', 'now': 60, 'd1': 0, 'd7': 0},
]
APM_ROWS = [
    {'svc': 'orders_service', 'n_now': 500, 'r_now': 450, 'p95_now': 900.0, 'p95_base': 300.0, 'f_now': 0, 'fr_now': 0.0, 'fr_base': 0.0, 'slow': True, 'failing': False},
    {'svc': 'cart-service', 'n_now': 400, 'r_now': 380, 'p95_now': 120.0, 'p95_base': 100.0, 'f_now': 30, 'fr_now': 0.06, 'fr_base': 0.01, 'slow': False, 'failing': True},
    {'svc': 'checkout-service', 'n_now': 300, 'r_now': 280, 'p95_now': 800.0, 'p95_base': 200.0, 'f_now': 20, 'fr_now': 0.05, 'fr_base': 0.0, 'slow': True, 'failing': True},
]
SLOW_ROWS = [
    {'svc': 'payments-service', 'n_now': 266, 'r_now': 250, 'p95_now': 900.0, 'p95_base': 300.0, 'f_now': 0, 'fr_now': 0.0, 'fr_base': 0.0, 'slow': True, 'failing': False},
    {'svc': 'payments-service', 'n_now': 270, 'r_now': 260, 'p95_now': 1300.0, 'p95_base': 400.0, 'f_now': 0, 'fr_now': 0.0, 'fr_base': 0.0, 'slow': True, 'failing': False},
]
WARM = {('shop', 'checkout-service')}
DEFAULT_RULES = {'p95_times': 2, 'p95_floor_ms': 200}
FLOW_RULES = {'floor': 200, 'lag_warn_min': 5, 'lag_crit_min': 15, 'all_drop': 0.2}
OWN = {'elastic.logging-url': 'https://es.example.com/', 'elastic.logging-api-key': 'own-es-key'}


def esql(rows: list[dict]) -> dict:
    names = list(rows[0]) if rows else []
    return {'took': 41, 'is_partial': False, 'columns': [{'name': n, 'type': 'keyword'} for n in names], 'values': [[r[n] for n in names] for r in rows]}


def es_error(kind: str, reason: str, root: list | None = None) -> str:
    return json.dumps({'error': {'root_cause': root if root is not None else [{'type': kind, 'reason': reason}], 'type': kind, 'reason': reason}, 'status': 400})


def missing_index(index: str = 'traces-apm*') -> HttpError:
    return HttpError(400, QUERY, es_error('verification_exception', f'Found 1 problem\nline 1:1: Unknown index [{index}]'))


def by_query(logs=None, apm=None):
    def answer(params, body):
        apm_asked = 'traces-apm' in body['query']
        got = (apm if apm is not None else esql(APM_ROWS)) if apm_asked else (logs if logs is not None else esql(LOG_ROWS))
        return got

    return answer


def searched(url: str, *patterns: str, shards: int = 3) -> dict:
    return {('POST', f'{url}/{p}/_search'): {'_shards': {'total': shards}, 'hits': {'hits': []}} for p in patterns}


def es_http(logs=None, apm=None, url: str = QUERY) -> FakeHttp:
    return FakeHttp({('POST', url): by_query(logs, apm)})


def info(**config) -> InstanceInfo:
    return InstanceInfo('main', True, None, {'url': ES_URL, 'log-index': 'logs-*', 'apm-index': 'traces-apm*', 'service-field': 'service.name', 'level-field': 'log.level',
                                              'message-field': 'message', 'error-field': 'error.message', 'namespace-field': 'kubernetes.namespace', 'ui-url': KIBANA, **config})


def es_runtime(store: Store, instances: dict[str, dict], transport) -> Runtime:
    registry = Registry.of(elasticsearch.PLUGIN)
    runtime = Runtime(registry, PluginState(store.db), Instances(store.db, registry, lambda: Sealer(KIT_KEY)), transport)
    for index, (name, values) in enumerate(instances.items()):
        add_instance(store, elasticsearch.PLUGIN, name, sealer_key=KIT_KEY, primary=index == 0, **({'url': ES_URL} | values))
    runtime.refresh()
    return runtime


def app_with(store: Store, http, config=None, **instances: dict) -> TestClient:
    key = master_key(store.db, settings())
    for index, (name, values) in enumerate(instances.items()):
        add_instance(store, elasticsearch.PLUGIN, name, sealer_key=key, primary=index == 0, **({'url': ES_URL} | values))
    return TestClient(create_app(config or settings(), store, transport=http))


def card_map(store: Store) -> dict[str, dict]:
    return {a['key']: a for a in store.load()['anomalies']}


def tool(tools: PluginTools, args: dict) -> tuple[str, bool]:
    try:
        out = tools.call('esql', args, NOW)
    except ToolError as e:
        return e.text, True
    return json.dumps(out), False


def queries_of(http: FakeHttp) -> list[str]:
    return [c[3]['query'] for c in http.calls if isinstance(c[3], dict) and 'query' in c[3]]


def sent_query(http: FakeHttp, index: int = -1) -> str:
    return queries_of(http)[index]


def link_params(url: str) -> str:
    return urllib.parse.unquote(url)


def test_elasticsearch_identity_and_manifest():
    # given
    registry = Registry.of(elasticsearch.PLUGIN)

    # when
    loaded = registry.get('elasticsearch')
    manifest = describe(elasticsearch.PLUGIN, icon=True)

    # then
    assert (loaded.state, loaded.reason) == ('ok', None) and 'skopax.plugins.elasticsearch' in BUILTIN
    assert Registry.load({}, entry_points=False).get('elasticsearch').state == 'ok'
    assert json.loads(json.dumps(manifest)) == manifest
    assert (manifest['name'], manifest['version'], manifest['category'], manifest['author'], manifest['licence'], manifest['core'], manifest['edition']) == (
        'Elasticsearch', '0.1.0', 'logs', 'Skopax', 'Apache-2.0', '>=0.1,<0.3', 'community')
    assert manifest['capabilities'] == {'hosts': ['@url', '@apm-url', '@ui-url'], 'secrets': ['api-key', 'apm-api-key', 'elastic.logging-api-key', 'elastic.apm-api-key'], 'agentReads': True}
    assert manifest['uses'] == [] and manifest['description'].endswith('.')
    assert manifest['checks'] == [{'id': 'logs', 'label': manifest['checks'][0]['label'], 'period': 900, 'offset': 180, 'ran': ['logs']},
                                  {'id': 'apm', 'label': manifest['checks'][1]['label'], 'period': 900, 'offset': 180, 'ran': ['apm']},
                                  {'id': 'logflow', 'label': manifest['checks'][2]['label'], 'period': 600, 'offset': 270, 'ran': ['logflow']}]
    assert [(c.sections, c.tool) for c in elasticsearch.PLUGIN.checks] == [(('logTop',), 'esql'), ((), 'esql'), (('logflow',), 'esql')]
    assert [(t['name'], t['kind']) for t in manifest['tools']] == [('esql', 'read')] and manifest['knowledge'] == ['esql']
    found = elasticsearch.PLUGIN.tools[0]
    assert (found.scan, found.per_run, found.instance_input) == (True, None, 'instance')
    assert found.input_schema['properties']['cluster']['enum'] == ['logs', 'apm'] and found.input_schema['required'] == ['query']
    assert [(r['id'], r['defaults']) for r in manifest['rules']] == [('apm', DEFAULT_RULES), ('logflow', FLOW_RULES)]
    assert manifest['panels'] == [{'tab': 'logs', 'script': 'panel.js', 'fragment': None, 'label': 'Logs', 'group': 'observe'}]
    assert manifest['agentReads'] == [{'op': 'esql', 'args': ['cluster', 'query'], 'keys': [r'elastic\.(logging|apm)-(url|api-key)']}]
    assert registry.icon('elasticsearch').startswith('<svg') and len(elasticsearch.PLUGIN.knowledge[0].text) < 8000
    assert len(elasticsearch.PLUGIN.knowledge[0].text.splitlines()) <= 40


def test_elasticsearch_defaults_are_neutral():
    # when
    defaults = {f.key: f.default for f in elasticsearch.PLUGIN.fields}
    kinds = {f.key: f.kind for f in elasticsearch.PLUGIN.fields}

    # then
    assert defaults == {'url': '', 'api-key': '', 'apm-url': '', 'apm-api-key': '', 'ca': '', 'ca-pinned': 'false', 'ui-url': '', 'data-view': '', 'log-index': 'logs-*', 'apm-index': 'traces-apm*',
                        'index-namespace': '', 'namespace-field': 'kubernetes.namespace', 'service-field': 'service.name', 'level-field': 'log.level', 'message-field': 'message',
                        'error-field': 'error.message', 'logflow-index': '', 'message-mask': '', 'apm-p95-overrides': '', 'discover-columns': ''}
    assert kinds == {'url': 'url', 'api-key': 'secret', 'apm-url': 'url', 'apm-api-key': 'secret', 'ca': 'pem', 'ca-pinned': 'bool', 'ui-url': 'url', 'data-view': 'text', 'log-index': 'text', 'apm-index': 'text',
                     'index-namespace': 'regex', 'namespace-field': 'text', 'service-field': 'text', 'level-field': 'text', 'message-field': 'text', 'error-field': 'text',
                     'logflow-index': 'text', 'message-mask': 'regex', 'apm-p95-overrides': 'text', 'discover-columns': 'text'}
    assert [f.key for f in elasticsearch.PLUGIN.fields if f.required] == ['url'] and [f.key for f in elasticsearch.PLUGIN.fields if f.ui] == ['ui-url']
    assert elasticsearch.PLUGIN.settings == ()
    error_help = {f.key: f.help for f in elasticsearch.PLUGIN.fields}['error-field']
    assert 'not mapped' in error_help and 'reads the message only' in error_help and 'Test connection says so' in error_help and 'status' not in error_help
    assert {r.id: dict(r.defaults) for r in elasticsearch.PLUGIN.rules} == {'apm': DEFAULT_RULES, 'logflow': FLOW_RULES}


def test_elasticsearch_connection_test_reports_ok_and_failures(store: Store):
    # given
    root, apm_root = f'{ES_URL}/', f'{APM_URL}/'
    good = FakeHttp({('GET', root): {'version': {'number': '8.15.0'}}, ('GET', apm_root): {'version': {'number': '8.14.1'}}, **searched(ES_URL, 'logs-*', 'traces-apm*'), **searched(APM_URL, 'traces-apm*')})
    denied, down = FakeHttp({('GET', root): HttpError(401, root)}), FakeHttp({('GET', root): Unreachable(f'{root} unreachable (TimeoutError)')})

    def wrong_ca(params, body):
        raise Unreachable(f'{root} unreachable (SSLError)') from ssl.SSLError('certificate verify failed')

    untrusted = FakeHttp({('GET', root): wrong_ca})

    def check(http, **values):
        rt = es_runtime(store, {'main': {'api-key': 'secret-key', **values}}, http)
        return elasticsearch.test(rt.context(elasticsearch.PLUGIN, rt.instances.get('elasticsearch', 'main'), NOW))

    # when
    ok = check(good)
    both = check(good, **{'apm-url': APM_URL, 'apm-api-key': 'apm-key'})
    refused, unreachable, tls = check(denied), check(down), check(untrusted)
    bad_pattern = check(good, **{'index-namespace': r'app-(.+)-logs-.*'})

    # then
    assert (ok.ok, ok.summary) == (True, 'Elasticsearch 8.15.0') and isinstance(ok.ms, int) and ok.ms >= 0
    assert (both.ok, both.summary) == (True, 'Elasticsearch 8.15.0, APM Elasticsearch 8.14.1')
    assert (refused.ok, refused.summary) == (False, msg('connection.rejected', status=401))
    assert (unreachable.ok, unreachable.summary) == (False, msg('connection.unreachable'))
    assert (tls.ok, tls.summary) == (False, msg('connection.tls'))
    assert (bad_pattern.ok, bad_pattern.summary) == (False, elastic.NS_MISSING)
    assert good.calls[0][1] == root and good.calls[0][2]['Authorization'] == 'ApiKey secret-key'
    assert [c[2]['Authorization'] for c in good.hits(apm_root)] == ['ApiKey apm-key']
    assert not any('secret-key' in r.summary for r in (ok, both, refused, unreachable, bad_pattern))


def test_a_keyless_elasticsearch_test_says_the_cluster_wants_credentials_rather_than_that_they_were_refused(store: Store):
    # given
    root = f'{ES_URL}/'
    rt = es_runtime(store, {'main': {}}, FakeHttp({('GET', root): HttpError(401, root)}))

    # when
    tested = elasticsearch.test(rt.context(elasticsearch.PLUGIN, rt.instances.get('elasticsearch', 'main'), NOW))

    # then
    assert (tested.ok, tested.summary) == (False, 'The service requires credentials and none are set (HTTP 401)')


def test_the_elasticsearch_test_reads_each_index_and_names_a_missing_read_privilege(store: Store):
    # given
    root = f'{ES_URL}/'
    denied_body = es_error('security_exception', 'action [indices:data/read/search] is unauthorized for API key id [secret-key] on indices [logs-*]')
    no_read = FakeHttp({('GET', root): {'version': {'number': '8.15.0'}}, ('POST', f'{ES_URL}/logs-*/_search'): HttpError(403, f'{ES_URL}/logs-*/_search', denied_body),
                        **searched(ES_URL, 'traces-apm*')})
    no_monitor = FakeHttp({('GET', root): HttpError(403, root, es_error('security_exception', 'action [cluster:monitor/main] is unauthorized')), **searched(ES_URL, 'logs-*', 'traces-apm*')})
    hidden = FakeHttp({('GET', root): {'version': {'number': '8.15.0'}}, **searched(ES_URL, 'traces-apm*'), ('POST', f'{ES_URL}/logs-*/_search'): {'_shards': {'total': 0}},
                       ('POST', PRIVILEGES): {'has_all_requested': False, 'index': {'logs-*': {'read': False}}}})
    fresh = FakeHttp({('GET', root): {'version': {'number': '8.15.0'}}, **searched(ES_URL, 'logs-*'), ('POST', f'{ES_URL}/traces-apm*/_search'): {'_shards': {'total': 0}},
                      ('POST', PRIVILEGES): {'has_all_requested': True, 'index': {'traces-apm*': {'read': True}}}})

    def check(http, **values):
        rt = es_runtime(store, {'main': {'api-key': 'secret-key', **values}}, http)
        return elasticsearch.test(rt.context(elasticsearch.PLUGIN, rt.instances.get('elasticsearch', 'main'), NOW))

    # when
    refused, monitor, unseen, empty = check(no_read), check(no_monitor), check(hidden), check(fresh)

    # then
    said = 'The API key lacks the read privilege on logs-*, which the logs and log flow checks read.'
    assert (refused.ok, refused.summary) == (False, said)
    assert (unseen.ok, unseen.summary) == (False, said)
    assert (monitor.ok, monitor.summary) == (True, 'Elasticsearch (no version without the monitor privilege)')
    assert (empty.ok, empty.summary) == (True, 'Elasticsearch 8.15.0, no index matches traces-apm* yet')
    assert no_read.hits(f'{ES_URL}/logs-*/_search')[0][3] == {'size': 0}
    assert hidden.hits(PRIVILEGES)[0][3] == {'index': [{'names': ['logs-*'], 'privileges': ['read']}]}
    assert not any('secret-key' in r.summary for r in (refused, monitor, unseen, empty))


def test_logs_and_apm_refused_for_permission_are_never_no_data(store: Store):
    # given
    denied = HttpError(403, QUERY, es_error('security_exception', 'action [indices:data/read/esql] is unauthorized for API key id [key-0042]'))
    unseen = FakeHttp({('POST', QUERY): by_query(logs=denied, apm=missing_index()), ('POST', PRIVILEGES): {'index': {'traces-apm*': {'read': False}}}})
    log_card = {'key': 'log|orders-service|ERROR|boom', 'src': 'logs', 'sig': 'log', 'ns': '-', 'svc': 'orders-service', 'sev': 'warn', 'status': 'open', 'now': 30, 'label': 'ERROR pattern',
                'what': 'x', 'short': 'WARN orders-service ERROR pattern', 'firstSeen': iso(NOW - 3600), 'lastSeen': iso(NOW - 900)}
    apm_card = dict(log_card, key='apm|orders-service|fail', src='apm', sig='apm-fail', label='failed transactions', short='WARN orders-service failed transactions')
    store.save_state({'anomalies': [log_card, apm_card], 'initialized': True}, None)

    # when
    with app_with(store, unseen, main={'api-key': 'key-0042'}) as c:
        c.app.state.sweeper.run('elasticsearch', 'logs', NOW)
        c.app.state.sweeper.run('elasticsearch', 'apm', NOW)
        refused = store.load()
        cards = card_map(store)
        unseen.routes[('POST', QUERY)] = by_query(apm=esql([]))
        unseen.routes[('POST', PRIVILEGES)] = {'index': {'logs-*': {'read': True}, 'traces-apm*': {'read': True}}}
        c.app.state.sweeper.run('elasticsearch', 'logs', NOW + 900)
        c.app.state.sweeper.run('elasticsearch', 'apm', NOW + 900)
    after = card_map(store)

    # then
    assert {k: refused['serverPasses'][k]['status'] for k in ('logs', 'apm')} == {'logs': 'ok \u00b7 no access to logs', 'apm': 'ok \u00b7 no access to apm'}
    assert cards['log|orders-service|ERROR|boom']['status'] == 'open' and cards['apm|orders-service|fail']['status'] == 'open'
    access = {a['ranKey']: a for a in cards.values() if a['sig'] == 'no-access'}
    assert set(access) == {'logs:access:logs', 'apm:access:apm'}
    assert access['logs:access:logs']['label'] == 'Elasticsearch cannot read logs: the API key lacks the read privilege on logs-*'
    assert access['apm:access:apm']['label'] == 'Elasticsearch cannot read apm: the API key lacks the read privilege on traces-apm*'
    assert all(a['sev'] == 'warn' and 'key-0042' not in a['what'] + a['label'] for a in access.values())
    assert {k for k, a in after.items() if a['status'] == 'open' and a['sig'] == 'no-access'} == set()
    assert access['logs:access:logs']['key'] == 'no-access|-/logs|logs'


def test_an_elasticsearch_refusal_names_the_api_key_both_ways_and_a_missing_key_by_what_was_sent():
    # when
    keyed = access_card('logs', 'Elasticsearch', 'logs', 'logs-*', 401)
    open_ = access_card('logs', 'Elasticsearch', 'logs', 'logs-*', 401, keyed=False)

    # then
    assert keyed['label'] == 'Elasticsearch cannot read logs: the API key is invalid or lacks the read privilege on logs-*'
    assert open_['label'] == 'Elasticsearch cannot read logs: the server requires credentials and none are set'
    assert 'API key' not in open_['label'] and (keyed['sig'], open_['sig']) == ('no-access', 'no-access')

def test_query_posts_esql_with_the_api_key_and_returns_rows():
    # given
    http = es_http()

    # when
    rows = Elastic(ES_URL, 'lk', http).query('FROM x | LIMIT 1')

    # then
    assert rows == LOG_ROWS
    method, url, headers, body = http.calls[0]
    assert (method, url, body) == ('POST', QUERY, {'query': 'FROM x | LIMIT 1'})
    assert headers['Authorization'] == 'ApiKey lk'


def test_a_refused_or_unreachable_cluster_raises_a_short_reason_without_the_key():
    # given
    refused = FakeHttp({})
    down = FakeHttp({('POST', QUERY): Unreachable(f'{QUERY} unreachable (TimeoutError)')})

    # then
    for http, text in ((refused, 'ES|QL HTTP 404'), (down, 'ES|QL unreachable')):
        with pytest.raises(ElasticError) as e:
            Elastic(ES_URL, 'secret-key', http).query('FROM x')
        assert text in e.value.text and e.value.reason == text
        assert 'secret-key' not in e.value.text


@pytest.mark.parametrize('code, body, text', [
    (404, es_error('index_not_found_exception', 'no such index [logs-shop-*]'), 'ES|QL HTTP 404: index_not_found_exception: no such index [logs-shop-*]'),
    (400, es_error('verification_exception', 'Found 1 problem\nline 2:9: Unknown column [app.name]'), 'ES|QL HTTP 400: verification_exception: Found 1 problem line 2:9: Unknown column [app.name]'),
    (400, es_error('parsing_exception', "line 1:12: mismatched input 'WHER' expecting <EOF>"), "ES|QL HTTP 400: parsing_exception: line 1:12: mismatched input 'WHER' expecting <EOF>"),
    (500, es_error('search_phase_execution_exception', 'all shards failed', [{'type': 'circuit_breaking_exception', 'reason': '[parent] Data too large'}]),
     'ES|QL HTTP 500: circuit_breaking_exception: [parent] Data too large'),
    (502, '<html><body>Bad Gateway</body></html>', 'ES|QL HTTP 502: <html><body>Bad Gateway</body></html>'),
    (503, '', 'ES|QL HTTP 503'),
])
def test_an_es_error_keeps_its_type_and_reason(code, body, text):
    # given
    http = FakeHttp({('POST', QUERY): HttpError(code, QUERY, body)})

    # when
    with pytest.raises(ElasticError) as e:
        Elastic(ES_URL, 'secret-key', http).query('FROM logs-shop-* | LIMIT 1')

    # then
    assert e.value.text == text and (e.value.reason, e.value.status) == (f'ES|QL HTTP {code}', code)


def test_an_es_error_reason_is_capped_never_carries_the_key_and_core_masks_it_on_the_way_out(store: Store):
    # given
    reason = 'Unknown column [x] near alice@example.com key secret-key ' + 'y' * 1000
    http = FakeHttp({('POST', QUERY): HttpError(400, QUERY, es_error('verification_exception', reason))})
    tools = PluginTools(es_runtime(store, {'main': {'api-key': 'secret-key'}}, http))

    # when
    with pytest.raises(ElasticError) as e:
        Elastic(ES_URL, 'secret-key', http).query('FROM logs-shop-2026.11.14')
    shown, failed = tool(tools, {'query': 'FROM logs-shop-2026.11.14 | LIMIT 1'})

    # then
    text = e.value.text
    assert text.startswith('ES|QL HTTP 400: verification_exception: Unknown column [x]') and len(text) == REASON_LIMIT
    assert 'secret-key' not in text and 'key ***' in text and e.value.reason == 'ES|QL HTTP 400'
    assert failed and shown.startswith('ES|QL HTTP 400: verification_exception: Unknown column [x]')
    assert 'secret-key' not in shown and 'alice@example.com' not in shown and '[redacted]' in shown


def test_the_http_sender_keeps_the_error_body(monkeypatch):
    # given
    def refuse(req, timeout, context):
        raise urllib.error.HTTPError(req.full_url, 400, 'Bad Request', {}, io.BytesIO(es_error('verification_exception', 'Unknown column [foo]').encode() + b' ' * 9000))

    monkeypatch.setattr(remote, 'open_url', refuse)

    # when
    with pytest.raises(HttpError) as e:
        remote.send('POST', QUERY, {}, b'{}', 5)

    # then
    assert e.value.code == 400 and e.value.args[0] == f'HTTP 400 on {QUERY}'
    assert '"verification_exception"' in e.value.body and len(e.value.body) == BODY_LIMIT


def test_a_cluster_needs_a_url_and_sends_the_key_only_when_one_is_set():
    # given
    http = es_http()

    # when
    Elastic(ES_URL, '', http).query('FROM x')

    # then
    assert Elastic(ES_URL, 'k').enabled and Elastic(ES_URL, '').enabled
    assert not Elastic('', 'k').enabled
    assert 'Authorization' not in http.calls[0][2]


def test_logs_flag_new_and_growing_errors_and_loud_warns():
    # when
    found, top = checks.evaluate_logs(LOG_ROWS)

    # then
    by_app = {x['svc']: x for x in found}
    assert set(by_app) == {'orders-service', 'checkout-service', 'auth-service'}
    assert by_app['orders-service']['sev'] == 'crit'
    assert by_app['checkout-service']['key'] == 'log|checkout-service|ERROR|Timeout on <id> call'
    assert by_app['auth-service']['sev'] == 'warn'
    assert all(x['src'] == 'logs' and x['sig'] == 'log' and x['ns'] == '-' for x in found)
    assert [t.get('flag', False) for t in top] == [True, True, False, True]


def test_the_log_query_reads_the_configured_index_and_fields_and_reads_levels_in_any_case():
    # when
    default = checks.logs_query(info().config)
    custom = checks.logs_query(info(**{'log-index': 'app-*-logs-*', 'service-field': 'labels.app', 'level-field': 'severity', 'message-field': 'line', 'error-field': ''}).config)

    # then
    assert default.startswith('FROM logs-*\n') and 'EVAL level = TO_UPPER(`log.level`)' in default
    assert 'msg = SUBSTRING(COALESCE(`error.message`, `message`), 1, 90)' in default and 'BY app = `service.name`, level, msg' in default
    assert custom.startswith('FROM app-*-logs-*\n') and 'TO_UPPER(`severity`)' in custom and 'SUBSTRING(`line`, 1, 90)' in custom and 'BY app = `labels.app`, level, msg' in custom
    assert 'NOT_FOUND' not in default and 'Request data' not in default
    levels = '`log.level` IN ("ERROR", "error", "Error", "WARN", "warn", "Warn")'
    assert f'| WHERE ({checks.window()}) AND {levels}\n| EVAL level = TO_UPPER(`log.level`)' in default, default
    assert default.index(levels) < default.index('TO_UPPER') and 'WHERE level IN' not in default
    assert '`severity` IN ("ERROR", "error", "Error", "WARN", "warn", "Warn")' in custom


def test_the_message_only_query_reads_no_error_field():
    # when
    plain = checks.logs_query(info().config, with_error=False)

    # then
    assert 'COALESCE' not in plain and 'msg = SUBSTRING(`message`, 1, 90)' in plain


def test_an_unknown_column_is_read_from_the_parsed_error():
    # given
    body = es_error('verification_exception', 'Found 2 problems\nline 5:39: Unknown column [error.message]\nline 6:2: Unknown column [labels.x]')
    http = FakeHttp({('POST', QUERY): HttpError(400, QUERY, body)})

    # when
    with pytest.raises(ElasticError) as e:
        Elastic(ES_URL, 'test-api-key', http).query('FROM logs-*')

    # then
    assert e.value.unknown == ('error.message', 'labels.x') and not e.value.missing


def unmapped_error_field(params, body):
    if 'COALESCE' in body['query']:
        return HttpError(400, QUERY, es_error('verification_exception', 'Found 1 problem\nline 5:39: Unknown column [error.message]'))
    return esql(LOG_ROWS)


def test_an_unmapped_error_field_falls_back_to_the_message_in_the_same_pass(store: Store):
    # given
    http = FakeHttp({('POST', QUERY): unmapped_error_field})

    # when
    with app_with(store, http, main={'ui-url': KIBANA}) as c:
        c.app.state.sweeper.run('elasticsearch', 'logs', NOW)
        state = store.load()
    cards = card_map(store)

    # then
    assert state['serverPasses']['logs']['status'] == 'ok'
    assert [('COALESCE' in q) for q in queries_of(http)] == [True, False]
    assert {k for k, a in cards.items() if a['src'] == 'logs'} == {x['key'] for x in checks.evaluate_logs(LOG_ROWS)[0]}
    assert len(state['logTop']) == len(LOG_ROWS)


def test_another_unknown_column_still_fails_the_logs_pass_with_one_query(store: Store):
    # given
    http = FakeHttp({('POST', QUERY): HttpError(400, QUERY, es_error('verification_exception', 'Found 1 problem\nline 7:9: Unknown column [service.name]'))})

    # when
    with app_with(store, http, main={}) as c:
        c.app.state.sweeper.run('elasticsearch', 'logs', NOW)
        state = store.load()

    # then
    assert state['serverPasses']['logs']['status'] == 'error: ES|QL HTTP 400' and len(queries_of(http)) == 1


def test_apm_flags_slow_and_failing_services_except_freshly_deployed_ones():
    # when
    found = checks.evaluate_apm(APM_ROWS, WARM, DEFAULT_RULES)

    # then
    assert [x['key'] for x in found] == ['apm|orders_service|p95', 'apm|cart-service|fail']


def test_apm_p95_cards_are_info_while_failure_cards_stay_warn():
    # when
    found = {x['key']: x for x in checks.evaluate_apm(APM_ROWS, set(), DEFAULT_RULES)}

    # then
    assert found['apm|orders_service|p95']['sev'] == 'info' and found['apm|checkout-service|p95']['sev'] == 'info'
    assert found['apm|cart-service|fail']['sev'] == 'warn' and found['apm|checkout-service|fail']['sev'] == 'warn'
    assert 'HTTP requests' in found['apm|orders_service|p95']['what']


def test_apm_p95_is_computed_over_request_transactions_only():
    # when
    apm = checks.apm_query(info().config, DEFAULT_RULES)

    # then
    assert apm.startswith('FROM traces-apm*\n')
    assert 'rq = transaction.type == "request"' in apm
    assert all(f'PERCENTILE(CASE(w == "{w}" AND rq, ms, null), 95)' in apm for w in ('now', 'd1', 'd7'))
    assert 'slow = r_now >= 100 AND r_d1 >= 50 AND r_d7 >= 50 AND p95_now > 2 * p95_base AND p95_now - p95_base >= 200' in apm
    assert 'f_now = SUM(CASE(w == "now", f, 0))' in apm


def test_apm_p95_with_stricter_rules_needs_three_times_the_baseline_and_800_ms_more():
    # when
    found = checks.evaluate_apm(SLOW_ROWS, set(), {'p95_times': 3, 'p95_floor_ms': 800})
    loose = checks.evaluate_apm(SLOW_ROWS, set(), DEFAULT_RULES)

    # then
    assert [(x['key'], x['now']) for x in found] == [('apm|payments-service|p95', 1300.0)]
    assert [x['now'] for x in loose] == [900.0, 1300.0]
    assert 'p95_now > 3 * p95_base AND p95_now - p95_base >= 800' in checks.apm_query(info().config, {'p95_times': 3, 'p95_floor_ms': 800})


def test_warm_services_come_from_the_deploys_the_context_hands_over():
    # given
    deploys = [{'ns': 'shop', 'svc': 'checkout-service', 'at': iso(NOW - 600)}, {'ns': 'payments', 'svc': 'orders-service', 'at': iso(NOW - 300)}, {'svc': 'no-namespace'}, 'junk']

    # then
    assert checks.warm_services(deploys) == {('shop', 'checkout-service'), ('payments', 'orders-service')}


def test_links_cover_log_and_apm_cards_and_any_card_with_a_service():
    # given
    log_item = checks.evaluate_logs(LOG_ROWS)[0][0]
    apm_item = checks.evaluate_apm(APM_ROWS, set(), DEFAULT_RULES)[0]
    found = info()

    # when
    log_links = elasticsearch.links(log_item, found)
    apm_links = elasticsearch.links(apm_item, found)

    # then
    assert [x['label'] for x in log_links] == ['Log lines in Kibana', 'All orders-service errors in Kibana']
    assert [x['label'] for x in apm_links] == ['orders_service errors in Kibana']
    assert 'orders_service' in link_params(apm_links[0]['url'])
    assert all(x['url'].startswith(f'{KIBANA}/app/discover#/?_g=') for x in log_links + apm_links)
    assert elasticsearch.links({'src': 'cf', 'sig': 'cf-health'}, found) is None
    assert elasticsearch.links({'src': 'pagescan', 'sig': 'typos', 'ns': '-', 'svc': 'shop.example.com'}, found) is None
    assert elasticsearch.links({'src': 'prom', 'sig': 'err', 'ns': 'shop', 'svc': 'ingress/web'}, found) is None
    assert elasticsearch.links(log_item, info(**{'ui-url': ''})) is None


def test_any_card_with_a_service_gets_a_logs_link(store: Store):
    # given
    rt = es_runtime(store, {'main': {'ui-url': KIBANA, 'data-view': 'view-1', 'service-field': 'app.name'}}, FakeHttp({}))
    card = {'key': 'err|shop/checkout-service', 'src': 'prom', 'sig': 'err', 'ns': 'shop', 'svc': 'checkout-service'}

    # when
    found = rt.links(card)
    quiet = rt.links({'key': 'x', 'src': 'cf', 'sig': 'cf-health', 'ns': '-', 'svc': 'zone'})

    # then
    assert [x['label'] for x in found] == ['Logs']
    shown = link_params(found[0]['url'])
    assert found[0]['url'].startswith(f'{KIBANA}/app/discover#/?_g=') and 'app.name:"checkout-service"' in shown and "dataViewId:'view-1'" in shown
    assert "from:'now-2h'" in shown and 'log.level:("ERROR" or "error" or "Error" or "WARN" or "warn" or "Warn")' in shown
    assert quiet is None


def test_deploy_report_rows_get_logs_links(store: Store):
    # given
    rt = es_runtime(store, {'main': {'ui-url': KIBANA}}, FakeHttp({}))
    report = {'windowFrom': '2026-11-14T10:00:00Z', 'windowTo': '2026-11-14T10:10:00Z', 'rows': [
        {'ns': 'shop', 'svc': 'checkout-service', 'metrics': 'err 12'}, {'ns': 'shop', 'svc': 'ingress/web'}, {'ns': 'shop'}]}

    # when
    out = rt.row_links(report)

    # then
    first, ingress, bare = out['rows']
    url = first['links']['logs']
    shown = link_params(url)
    assert url.startswith(f'{KIBANA}/app/discover#/?_g=') and 'service.name:"checkout-service"' in shown
    assert "from:'2026-11-14T09:55:00Z',to:'2026-11-14T10:35:00Z'" in shown
    assert 'links' not in ingress and 'links' not in bare


def test_index_namespace_takes_the_namespace_from_the_index():
    # given
    by_index = {'index-namespace': NS_PATTERN, 'namespace-field': 'kubernetes.namespace'}
    by_field = {'index-namespace': '', 'namespace-field': 'kubernetes.namespace'}

    # then
    assert elastic.namespace_of({'_index': 'app-shop-logs-2026.11.14'}, by_index) == 'shop'
    assert elastic.namespace_of({'_index': '.ds-app-payments-logs-2026.11.14-000001'}, by_index) == 'payments'
    assert elastic.namespace_of({'_index': 'other-2026.11.14'}, by_index) is None
    assert elastic.namespace_of({'_index': 'app-shop-logs-x', 'kubernetes.namespace': 'cart'}, by_field) == 'cart'
    assert elastic.namespace_problem({'index-namespace': r'app-(.+)-logs-.*'}) == elastic.NS_MISSING
    assert elastic.namespace_problem(by_index) is None and elastic.namespace_problem(by_field) is None
    with pytest.raises(ElasticError) as e:
        elastic.namespace_of({'_index': 'app-shop-logs-1'}, {'index-namespace': r'app-(.+)-logs-.*'})
    assert e.value.reason == elastic.NS_MISSING


def test_a_pattern_without_ns_fails_the_logs_pass_at_run_time(store: Store):
    # given
    http = es_http()

    # when
    with app_with(store, http, main={'index-namespace': r'app-(.+)-logs-.*'}) as c:
        c.app.state.sweeper.run('elasticsearch', 'logs', NOW)
        state = store.load()

    # then
    assert state['serverPasses']['logs']['status'] == f'error: {elastic.NS_MISSING}'
    assert http.calls == []


def test_one_instance_runs_both_checks_with_old_pass_names(store: Store):
    # given
    http = es_http()

    # when
    with app_with(store, http, main={'api-key': 'lk', 'ui-url': KIBANA}) as c:
        jobs = {j.name: j for j in c.app.state.scheduler.jobs}
        c.app.state.sweeper.run('elasticsearch', 'logs', NOW)
        c.app.state.sweeper.run('elasticsearch', 'apm', NOW)
        state = store.load()
    cards = card_map(store)

    # then
    assert (jobs['elasticsearch.logs'].period, jobs['elasticsearch.logs'].offset) == (900, 180)
    assert (jobs['elasticsearch.apm'].period, jobs['elasticsearch.apm'].offset) == (900, 180)
    assert {k: v['status'] for k, v in state['serverPasses'].items()} == {'logs': 'ok', 'apm': 'ok'}
    assert {k for k, a in cards.items() if a['src'] == 'logs'} == {x['key'] for x in checks.evaluate_logs(LOG_ROWS)[0]}
    assert {k for k, a in cards.items() if a['src'] == 'apm'} == {x['key'] for x in checks.evaluate_apm(APM_ROWS, set(), DEFAULT_RULES)}
    assert not any('cluster' in a or 'ranKey' in a for a in cards.values())
    assert len(state['logTop']) == len(LOG_ROWS) and all(t['link'].startswith(f'{KIBANA}/app/discover#/') for t in state['logTop'])
    assert all(a['links'] for a in cards.values())
    assert all(c[2]['Authorization'] == 'ApiKey lk' for c in http.calls)


def test_each_pass_reports_on_its_own_and_links_the_top_rows(store: Store):
    # given
    http = FakeHttp({('POST', QUERY): by_query(), ('POST', APM_QUERY): Unreachable(f'{APM_QUERY} unreachable (TimeoutError)')})

    # when
    with app_with(store, http, main={'ui-url': KIBANA, 'apm-url': APM_URL}) as c:
        c.app.state.sweeper.run('elasticsearch', 'logs', NOW)
        c.app.state.sweeper.run('elasticsearch', 'apm', NOW)
        state = store.load()

    # then
    passes = state['serverPasses']
    assert passes['logs']['status'] == 'ok' and len(state['logTop']) == len(LOG_ROWS)
    assert passes['apm']['status'] == 'error: ES|QL unreachable'


def test_an_empty_apm_url_and_key_fall_back_to_the_log_cluster(store: Store):
    # given
    http = es_http()
    tools = PluginTools(es_runtime(store, {'main': {'api-key': 'lk'}}, http))

    # when
    shown, failed = tool(tools, {'cluster': 'apm', 'query': 'FROM traces-apm* | WHERE @timestamp >= NOW() - 1 hour | LIMIT 1'})

    # then
    assert not failed and http.calls[-1][1] == QUERY and http.calls[-1][2]['Authorization'] == 'ApiKey lk'


def test_apm_without_indices_is_ok_and_quiet(store: Store):
    # given
    http = FakeHttp({('POST', QUERY): missing_index(), ('POST', 'https://east.example.com/_query'): missing_index()})
    open_main = {'key': 'apm|orders-service|fail', 'src': 'apm', 'sig': 'apm-fail', 'ns': '-', 'svc': 'orders-service', 'sev': 'warn', 'status': 'open', 'now': 30, 'label': 'failed transactions',
                 'what': 'x', 'short': 'WARN orders-service failed transactions', 'firstSeen': iso(NOW - 3600), 'lastSeen': iso(NOW - 900)}
    open_east = dict(open_main, key='apm|cart-service|fail', svc='cart-service', cluster='east', ranKey='apm-east')
    store.save_state({'anomalies': [open_main, open_east], 'initialized': True}, None)

    # when
    with app_with(store, http, main={}, east={'url': 'https://east.example.com'}) as c:
        c.app.state.sweeper.run('elasticsearch', 'apm', NOW)
        state = store.load()
    cards = card_map(store)

    # then
    for name in ('apm', 'apm-east'):
        status = state['serverPasses'][name]['status']
        assert status == 'ok \u00b7 no data'
    assert set(cards) == {'apm|orders-service|fail', 'apm|cart-service|fail'}
    assert all(a['status'] == 'open' for a in cards.values())


def test_elasticsearch_second_instance_is_scoped(store: Store):
    # given
    east = 'https://east.example.com'
    http = FakeHttp({('POST', QUERY): by_query(logs=esql([])), ('POST', f'{east}/_query'): by_query(logs=esql(LOG_ROWS[:1]))})

    # when
    with app_with(store, http, main={}, east={'url': east}) as c:
        c.app.state.sweeper.run('elasticsearch', 'logs', NOW)
        state = store.load()
    cards = card_map(store)

    # then
    card = cards['log|orders-service|ERROR|Connection refused: payments gateway@east']
    assert (card['cluster'], card['ranKey']) == ('east', 'logs-east')
    assert set(state['serverPasses']) >= {'logs', 'logs-east'}
    assert [r.get('cluster') for r in state['logTop']] == ['east']


def test_the_app_skips_freshly_deployed_services_in_apm(store: Store):
    # given
    now = time.time()
    store.save_state({'anomalies': [], 'initialized': True, 'deploys': [{'ns': 'shop', 'svc': 'checkout-service', 'at': iso(now - 300)},
                                                                       {'ns': 'shop', 'svc': 'cart-service', 'at': iso(now - 4000)}]}, None)
    store.add_deploys([{'ns': 'payments', 'svc': 'orders-service', 'toImage': 'abc12345', 'at': iso(now - 120)}])
    http = es_http()

    # when
    with app_with(store, http, main={}) as c:
        c.app.state.sweeper.run('elasticsearch', 'apm', now)
    keys = {a['key'] for a in store.load()['anomalies']}

    # then
    assert keys == {'apm|cart-service|fail'}


def test_without_an_instance_no_elasticsearch_job_is_registered(store: Store):
    # when
    with TestClient(create_app(settings(), store)) as c:
        names = [j.name for j in c.app.state.scheduler.jobs]

    # then
    assert not any(n.startswith('elasticsearch.') for n in names)


def test_elasticsearch_rules_come_from_the_rule_set(store: Store):
    # given
    http = es_http(apm=esql(SLOW_ROWS[:1]))

    # when
    with app_with(store, http, main={}) as c:
        c.app.state.sweeper.run('elasticsearch', 'apm', NOW)
        default = set(card_map(store))
        c.app.state.runtime.live.set('elasticsearch.apm', '{"p95_times": 3, "p95_floor_ms": 800}', USER)
        store.anomalies.delete_many({})
        store.save_state({'anomalies': [], 'initialized': True}, None)
        c.app.state.sweeper.run('elasticsearch', 'apm', NOW)
        stricter = set(card_map(store))
    asked = queries_of(http)

    # then
    assert default == {'apm|payments-service|p95'} and stricter == set()
    assert 'p95_now > 2 * p95_base AND p95_now - p95_base >= 200' in asked[0] and 'p95_now > 3 * p95_base AND p95_now - p95_base >= 800' in asked[-1]


def test_esql_cluster_input_picks_the_cluster_inside_the_instance(store: Store):
    # given
    http = FakeHttp({('POST', QUERY): esql([{'n': 1}]), ('POST', APM_QUERY): esql([{'n': 2}])})
    tools = PluginTools(es_runtime(store, {'main': {'api-key': 'lk', 'apm-url': APM_URL, 'apm-api-key': 'ak'}}, http))

    # when
    logs = tool(tools, {'query': 'FROM logs-shop-2026.11.14 | LIMIT 1'})
    apm = tool(tools, {'cluster': 'apm', 'query': 'FROM traces-apm* | WHERE @timestamp >= NOW() - 1 hour | LIMIT 1'})
    wrong = tool(tools, {'cluster': 'prod', 'query': 'FROM x'})

    # then
    assert [(c[1], c[2]['Authorization']) for c in http.calls] == [(QUERY, 'ApiKey lk'), (APM_QUERY, 'ApiKey ak')]
    assert json.loads(logs[0]) == [{'n': 1}] and json.loads(apm[0]) == [{'n': 2}]
    assert wrong[1] and 'logs or apm' in wrong[0]


def test_the_esql_tool_shows_the_es_reason_without_the_class_name(store: Store):
    # given
    body = es_error('index_not_found_exception', 'no such index [logs-shop-2026.10.29]')
    http = FakeHttp({('POST', QUERY): HttpError(404, QUERY, body)})
    tools = PluginTools(es_runtime(store, {'main': {'api-key': 'secret-key'}}, http))

    # when
    text, err = tool(tools, {'cluster': 'logs', 'query': 'FROM logs-shop-2026.10.29 | LIMIT 1'})

    # then
    assert err and text.startswith('ES|QL HTTP 404: index_not_found_exception: no such index [logs-shop-2026.10.29]')
    assert 'ElasticError' not in text and 'ReadError' not in text


@pytest.mark.parametrize('kind,reason,hint', [
    ('parsing_exception', "line 1:535: mismatched input 'last' expecting {QUOTED_STRING, INTEGER_LITERAL}", 'first and last are reserved words: name the column oldest / newest'),
    ('verification_exception', 'Found 1 problem line 1:191: No matches found for pattern [kubernetes*]', 'a KEEP or DROP pattern matched no column'),
    ('verification_exception', 'Found 1 problem line 1:6: Unknown index [logs-shop-2026.11.10*]', 'that index does not exist, or the API key cannot read it: logs are in logs-*, APM in traces-apm*'),
    ('verification_exception', 'Found 1 problem line 1:178: first argument of [http.response.status_code == 404] is [text] so second argument must also be [text]',
     'compare it with a quoted string ("404") or use its .keyword field'),
    ('verification_exception', 'Found 1 problem line 1:259: Unknown column [log]', 'the log line is in message'),
])
def test_the_esql_tool_adds_a_hint_to_a_reserved_word_or_a_missing_index(store: Store, kind, reason, hint):
    # given
    http = FakeHttp({('POST', QUERY): HttpError(400, QUERY, es_error(kind, reason))})
    tools = PluginTools(es_runtime(store, {'main': {'api-key': 'secret-key'}}, http))

    # when
    text, err = tool(tools, {'cluster': 'logs', 'query': 'FROM logs-shop-2026.11.08 | LIMIT 1'})

    # then
    assert err and text.startswith(f'ES|QL HTTP 400: {kind}: ') and hint in text, text


def test_a_long_es_reason_keeps_its_hint_inside_the_tool_answer(store: Store):
    # given
    reason = 'Found 1 problem line 1:259: Unknown column [log] ' + 'z' * 600
    http = FakeHttp({('POST', QUERY): HttpError(400, QUERY, es_error('verification_exception', reason))})
    tools = PluginTools(es_runtime(store, {'main': {}}, http))

    # when
    text, err = tool(tools, {'query': 'FROM logs-shop-2026.11.08 | LIMIT 1'})

    # then
    assert err and text.endswith('the log line is in message (the level in log.level, the service in service.name)'), text


def test_a_gateway_timeout_tells_the_model_how_to_narrow_the_query(store: Store):
    # given
    http = FakeHttp({('POST', QUERY): HttpError(504, QUERY, '<html><title>504 Gateway Time-out</title></html>')})
    tools = PluginTools(es_runtime(store, {'main': {}}, http))

    # when
    text, err = tool(tools, {'cluster': 'logs', 'query': 'FROM logs-* | WHERE @timestamp >= NOW() - 2 days AND message LIKE "*x*" | LIMIT 1'})

    # then
    assert err and text.startswith('ES|QL HTTP 504: ') and 'one day at a time' in text and 'the retry on the last day failed too' in text and '<html>' not in text and '60 s' not in text


@pytest.mark.parametrize('query,ok', [
    ('FROM logs-shop-2026.10.* | WHERE message LIKE "promo*" | STATS c = COUNT(*) BY level', False),
    ('FROM logs-shop-* | STATS c = COUNT(*)', False),
    ('FROM a-2026.10.18, a-2026.10.19, a-2026.10.20, a-2026.10.21 | STATS c = COUNT(*)', False),
    ('FROM logs-shop-2026.10.* | WHERE @timestamp >= "2026-10-20T00:00:00Z" AND message LIKE "promo*" | STATS c = COUNT(*)', True),
    ('FROM traces-apm* | WHERE `@timestamp` > NOW() - 1 hour | STATS c = COUNT(*)', True),
    ('FROM logs-shop-2026.10.21 | WHERE message LIKE "promo*" | STATS c = COUNT(*)', True),
    ('FROM a-2026.10.19, a-2026.10.20, a-2026.10.21 | STATS c = COUNT(*)', True),
    ('SHOW INFO', False),
])
def test_esql_refuses_an_unbounded_scan_over_many_daily_indices(store: Store, query, ok):
    # given
    http = FakeHttp({('POST', QUERY): esql([{'c': 1}])})
    tools = PluginTools(es_runtime(store, {'main': {}}, http))

    # when
    text, err = tool(tools, {'cluster': 'logs', 'query': query})

    # then
    assert err is (not ok), text
    if not ok:
        assert ('daily indices' in text or 'must start with FROM' in text) and http.calls == []


def test_the_esql_refusal_suggests_a_bounded_query_the_check_accepts(store: Store):
    # given
    tools = PluginTools(es_runtime(store, {'main': {'log-index': 'app-*-logs-*'}}, FakeHttp({})))
    unbounded = 'FROM app-shop-logs-* | STATS c = COUNT(*)'

    # when
    text, err = tool(tools, {'cluster': 'logs', 'query': unbounded})
    suggested = re.search(r'like (FROM .+)$', text).group(1)

    # then
    assert err and suggested.startswith('FROM app-*-logs-* | WHERE @timestamp')
    reads.check_scope(suggested + ' | STATS c = COUNT(*)', 'app-*-logs-*')
    assert '*' not in reads.SLOW.split(':', 1)[1].replace('"*...*"', '')


def test_read_tools_take_markup_like_text_as_it_is(store: Store):
    # given
    http = FakeHttp({('POST', QUERY): esql([])})
    tools = PluginTools(es_runtime(store, {'main': {}}, http))
    query = 'FROM logs-shop-2026.10.26 | WHERE message LIKE "*</parameter>*" | LIMIT 5'

    # when
    out, err = tool(tools, {'cluster': 'logs', 'query': query})

    # then
    assert not err and queries_of(http) == [query], 'the markup in WHERE passes as it is'


def test_self_review_reads_its_own_logs_through_elasticsearch(store: Store):
    # given
    rows = [{'level': 'WARN', 'n': 4, 'msg': 'page scan: no screenshot of https://shop.example.com/x', 'oldest': '2026-11-08T12:00:00Z', 'newest': '2026-11-08T12:06:00Z'}]
    http = FakeHttp({('POST', QUERY): esql(rows)})
    quiet_http = FakeHttp({('POST', QUERY): esql(rows)})

    # when
    with TestClient(create_app(settings(), store, transport=quiet_http)) as c:
        none = c.app.state.self_review.facts(NOW)
    with app_with(store, http, main={'api-key': 'lk', 'service-field': 'app.name'}) as c:
        facts = c.app.state.self_review.facts(NOW)
        asked = c.app.state.investigator.sources.selfreview(NOW)
    unset = dataclasses.replace(settings(), self_log_index='')
    with app_with(store, quiet_http, config=unset) as c:
        no_index = c.app.state.self_review.facts(NOW)

    # then
    expected = [{'level': 'WARN', 'count': 4, 'first': '2026-11-08T12:00:00Z', 'last': '2026-11-08T12:06:00Z', 'message': 'page scan: no screenshot of https://shop.example.com/x'}]
    assert facts['ownLogs'] == expected and asked['ownLogs'] == expected
    assert sent_query(http, 0).startswith('FROM logs-platform-* | WHERE `app.name` == "skopax"') and 'TO_UPPER(`log.level`)' in sent_query(http, 0)
    levels = '`log.level` IN ("ERROR", "error", "Error", "WARN", "warn", "Warn")'
    assert sent_query(http, 0).index(levels) < sent_query(http, 0).index('| EVAL') and 'WHERE level IN' not in sent_query(http, 0)
    assert selfreview.quoted is elastic.quoted and not hasattr(selfreview, 'LOG_FIELDS')
    assert http.calls[0][2]['Authorization'] == 'ApiKey lk'
    assert none['ownLogs'] == {'error': 'the logs source is off'} and no_index['ownLogs'] == {'error': 'the logs source is off'}
    assert quiet_http.calls == []


def test_agent_read_keys_match_v1():
    # given
    read = elasticsearch.PLUGIN.agent_reads[0]
    client = agent_client(agent_boot(plugins=(elasticsearch,)))

    # when
    pushed = client.put('/api/agent/sources', json={'rev': 'v1', 'config': {'elastic.logging-url': ES_URL, 'elastic.logging-api-key': 'lk', 'elastic.apm-url': APM_URL,
                                                                            'elastic.apm-api-key': 'ak'}}, headers=AUTH)

    # then
    assert tuple(f.key for f in read.fields) == V1_KEYS
    assert all(any(re.fullmatch(k, key) for k in read.keys) for key in V1_KEYS)
    assert not any(re.fullmatch(k, 'elastic.other-url') for k in read.keys)
    assert (read.op, read.args, read.timeout) == ('esql', ('cluster', 'query'), 75)
    assert [f.kind for f in read.fields] == ['url', 'secret', 'url', 'secret']
    assert pushed.status_code == 200 and pushed.json()['reads'] == {'esql': ['apm', 'logs']}


def test_the_agent_builds_its_log_readers_from_its_own_config_keys():
    # then
    assert reads.agent_sources({'elastic.logging-url': 'https://es.example.com/', 'elastic.logging-api-key': 'es-key'}) == ['logs']
    assert reads.agent_sources({'elastic.logging-url': 'https://es.example.com/', 'elastic.apm-url': APM_URL, 'elastic.apm-api-key': 'ak'}) == ['apm']
    assert reads.agent_sources({'elastic.logging-url': ' ', 'elastic.logging-api-key': 'k'}) == []


def test_the_agent_runs_esql_only_with_the_bearer_token(monkeypatch):
    # given
    http = FakeHttp({('POST', QUERY): esql([{'message': 'boom'}])})
    monkeypatch.setattr(reads, 'send', http)
    c = agent_client(agent_boot(values=OWN, plugins=(elasticsearch,)))
    body = {'op': 'esql', 'args': {'cluster': 'logs', 'query': 'FROM logs-shop-2026.11.10 | LIMIT 1'}}

    # when
    anonymous = c.post('/api/read', json=body)
    rows = c.post('/api/read', json=body, headers=AUTH)
    found = c.get('/api/agent', headers=AUTH)

    # then
    assert anonymous.status_code == 401 and queries_of(http) == ['FROM logs-shop-2026.11.10 | LIMIT 1']
    assert http.calls[0][1] == QUERY and http.calls[0][2]['Authorization'] == 'ApiKey own-es-key'
    assert rows.json() == {'data': [{'message': 'boom'}]}
    assert found.json()['reads'] == {'esql': ['logs']}


@pytest.mark.parametrize('body, status, said', [
    ({'op': 'drop', 'args': {}}, 404, 'esql'),
    ({'op': 'esql', 'args': {'cluster': 'prod', 'query': 'FROM x'}}, 422, 'cluster is one of logs, apm'),
    ({'op': 'esql', 'args': {'cluster': 'logs'}}, 422, 'query must be a text'),
    ({'op': 'esql', 'args': {'cluster': 'logs', 'query': 'FROM x', 'index': 'y'}}, 422, 'cluster, query'),
    ({'op': 'esql', 'args': {'cluster': 'apm', 'query': 'FROM traces-apm*'}}, 503, 'elastic.apm-url and elastic.apm-api-key'),
])
def test_the_agent_refuses_reads_it_does_not_know_or_has_no_keys_for(monkeypatch, body, status, said):
    # given
    monkeypatch.setattr(reads, 'send', FakeHttp({('POST', QUERY): esql([{'message': 'boom'}])}))

    # when
    r = agent_client(agent_boot(values=OWN, plugins=(elasticsearch,))).post('/api/read', json=body, headers=AUTH)

    # then
    assert r.status_code == status and said in r.json()['error'], r.json()


def test_source_errors_come_back_as_the_source_said_and_long_answers_are_cut(monkeypatch):
    # given
    http = FakeHttp({('POST', QUERY): HttpError(400, QUERY, es_error('verification_exception', 'Unknown column [log] near own-es-key')),
                     ('POST', APM_QUERY): esql([{'n': i} for i in range(reads.ROWS + 5)])})
    monkeypatch.setattr(reads, 'send', http)
    c = agent_client(agent_boot(values={**OWN, 'elastic.apm-url': APM_URL, 'elastic.apm-api-key': 'ak'}, plugins=(elasticsearch,)))

    # when
    bad = c.post('/api/read', json={'op': 'esql', 'args': {'cluster': 'logs', 'query': 'FROM x'}}, headers=AUTH)
    many = c.post('/api/read', json={'op': 'esql', 'args': {'cluster': 'apm', 'query': 'FROM traces-apm* | WHERE @timestamp >= NOW() - 1 hour'}}, headers=AUTH)

    # then
    assert (bad.status_code, bad.json()['error']) == (400, 'ES|QL HTTP 400: verification_exception: Unknown column [log] near ***')
    assert len(many.json()['data']) == reads.ROWS


def test_a_pushed_url_never_takes_the_agents_own_key_along(monkeypatch):
    # given
    http = FakeHttp({('POST', 'https://es.elsewhere.example/_query'): esql([{'message': 'x'}]), ('POST', QUERY): esql([{'message': 'own'}])})
    monkeypatch.setattr(reads, 'send', http)
    c = agent_client(agent_boot(values=OWN, plugins=(elasticsearch,)))
    body = {'op': 'esql', 'args': {'cluster': 'logs', 'query': 'FROM x'}}

    # when
    c.put('/api/agent/sources', json={'rev': 'r1', 'config': {'elastic.logging-url': 'https://es.elsewhere.example'}}, headers=AUTH)
    kept = c.post('/api/read', json=body, headers=AUTH)
    c.put('/api/agent/sources', json={'rev': 'r2', 'config': {'elastic.logging-url': 'https://es.elsewhere.example', 'elastic.logging-api-key': 'pushed-key'}}, headers=AUTH)
    taken = c.post('/api/read', json=body, headers=AUTH)

    # then
    assert kept.status_code == 503
    assert taken.status_code == 200 and taken.json() == {'data': [{'message': 'x'}]}
    assert [(call[1], call[2]['Authorization']) for call in http.calls] == [('https://es.elsewhere.example/_query', 'ApiKey pushed-key')]


def test_the_esql_tool_and_the_agent_name_the_read_privilege_on_a_refusal(store: Store, monkeypatch):
    # given
    denied = HttpError(403, QUERY, es_error('security_exception', 'action [indices:data/read/esql] is unauthorized for API key id [secret-key] and own-es-key'))
    tools = PluginTools(es_runtime(store, {'main': {'api-key': 'secret-key'}}, FakeHttp({('POST', QUERY): denied})))
    monkeypatch.setattr(reads, 'send', FakeHttp({('POST', QUERY): denied}))
    c = agent_client(agent_boot(values=OWN, plugins=(elasticsearch,)))

    # when
    text, err = tool(tools, {'cluster': 'logs', 'query': 'FROM logs-shop-*, logs-cart-* | WHERE @timestamp >= NOW() - 1 hour | LIMIT 1'})
    agent = c.post('/api/read', json={'op': 'esql', 'args': {'cluster': 'logs', 'query': 'FROM logs-shop-* | WHERE @timestamp >= NOW() - 1 hour | LIMIT 1'}}, headers=AUTH)

    # then
    assert err and 'the API key lacks the read privilege on logs-shop-*, logs-cart-*' in text and 'secret-key' not in text
    assert agent.status_code == 403 and 'the API key lacks the read privilege on logs-shop-*' in agent.json()['error'] and 'own-es-key' not in agent.json()['error']


def test_the_elasticsearch_test_notes_an_unmapped_error_field(store: Store):
    # given
    root = f'{ES_URL}/'
    base = {('GET', root): {'version': {'number': '8.15.0'}}, **searched(ES_URL, 'logs-*', 'traces-apm*')}
    unmapped = FakeHttp({**base, ('POST', QUERY): HttpError(400, QUERY, es_error('verification_exception', 'Found 1 problem\nline 1:17: Unknown column [error.message]'))})
    mapped = FakeHttp({**base, ('POST', QUERY): esql([])})

    def check(http, **values):
        rt = es_runtime(store, {'main': {'api-key': 'secret-key', **values}}, http)
        return elasticsearch.test(rt.context(elasticsearch.PLUGIN, rt.instances.get('elasticsearch', 'main'), NOW))

    # when
    noted, plain = check(unmapped), check(mapped)

    # then
    assert (noted.ok, noted.summary) == (True, 'Elasticsearch 8.15.0, error-field error.message is not mapped, so the logs check reads the message')
    assert (plain.ok, plain.summary) == (True, 'Elasticsearch 8.15.0')
    assert 'FROM logs-* | KEEP `error.message` | LIMIT 1' in queries_of(mapped)
    assert [q for q in queries_of(unmapped) if 'error.message' in q] == ['FROM logs-* | KEEP `error.message` | LIMIT 1'], 'the log flow namespace read is the only other ES|QL call'


def test_an_es_401_in_the_agent_read_never_reads_as_the_agent_token(monkeypatch):
    # given
    monkeypatch.setattr(reads, 'send', FakeHttp({('POST', QUERY): HttpError(401, QUERY, es_error('security_exception', 'unable to authenticate with provided credentials'))}))
    c = agent_client(agent_boot(values=OWN, plugins=(elasticsearch,)))
    answer = c.post('/api/read', json={'op': 'esql', 'args': {'cluster': 'logs', 'query': 'FROM logs-* | WHERE @timestamp >= NOW() - 1 hour | LIMIT 1'}}, headers=AUTH)

    def link(method, path, body, timeout):
        raise HttpError(answer.status_code, f'https://agent.example.com{path}', answer.text)

    # when
    with pytest.raises(ReadError) as e:
        RemoteAgent(link, 'east', relay='').read('esql', {'cluster': 'logs', 'query': 'FROM logs-* | WHERE @timestamp >= NOW() - 1 hour | LIMIT 1'})

    # then
    assert answer.status_code == 502 and answer.json()['error'] == 'the API key was not accepted by Elasticsearch (HTTP 401)'
    assert e.value.message == msg('agent.answered_with', label='east', code=502, detail='the API key was not accepted by Elasticsearch (HTTP 401)')
    assert e.value.message != msg('agent.refused', label='east')


def open_apm_card() -> dict:
    return {'key': 'apm|checkout-service|p95', 'src': 'apm', 'sig': 'apm-p95', 'ns': '-', 'svc': 'checkout-service', 'sev': 'info', 'status': 'open', 'now': 900, 'label': 'APM p95',
            'what': 'x', 'short': 'INFO checkout-service APM p95', 'firstSeen': iso(NOW - 3600), 'lastSeen': iso(NOW - 900)}


def test_an_apm_stream_never_written_or_now_empty_is_no_data_and_closes_no_card(store: Store):
    # given
    unwritten = HttpError(400, QUERY, es_error('verification_exception', 'Found 1 problem\nline 3:200: Unknown column [event.outcome]'))

    def emptied(params, body):
        return esql([{'n': 0}]) if 'COUNT(*)' in body['query'] else esql([])

    def busy(params, body):
        return esql([{'n': 4200}]) if 'COUNT(*)' in body['query'] else esql([])

    def run(answer):
        store.save_state({'anomalies': [open_apm_card()], 'initialized': True}, None)
        with app_with(store, FakeHttp({('POST', QUERY): answer}), main={}) as c:
            c.app.state.sweeper.run('elasticsearch', 'apm', NOW)
        store.db['instances'].delete_many({})
        return store.load()['serverPasses']['apm']['status'], card_map(store)['apm|checkout-service|p95']['status']

    # when
    never, empty, quiet = run(unwritten), run(emptied), run(busy)

    # then
    assert never == ('ok \u00b7 no data', 'open')
    assert empty == ('ok \u00b7 no data', 'open')
    assert quiet == ('ok', 'cleared'), 'transactions this hour with none slow or failing is a real all-clear'


def test_a_missing_log_index_is_no_data_for_the_logs_check_too(store: Store):
    # given
    http = FakeHttp({('POST', QUERY): missing_index('logs-*')})
    runtime = es_runtime(store, {'main': {}}, http)

    # when
    sweep = checks.run_logs(runtime.context(elasticsearch.PLUGIN, runtime.instances.get('elasticsearch', 'main'), NOW))

    # then
    assert (sweep.result.ok, sweep.result.status, sweep.result.ran) == (True, 'ok \u00b7 no data', ('logs:no-data', 'logs:access:logs'))
    assert sweep.sections == {'logTop': []}


def test_a_refused_logs_read_empties_the_log_table_instead_of_showing_old_rows(store: Store):
    # given
    denied = HttpError(403, QUERY, es_error('security_exception', 'action [indices:data/read/esql] is unauthorized'))
    runtime = es_runtime(store, {'main': {'api-key': 'key-0042'}}, FakeHttp({('POST', QUERY): denied}))

    # when
    sweep = checks.run_logs(runtime.context(elasticsearch.PLUGIN, runtime.instances.get('elasticsearch', 'main'), NOW))

    # then
    assert sweep.result.status == 'ok \u00b7 no access to logs' and sweep.sections == {'logTop': []}


def test_a_read_that_works_is_never_second_guessed_by_the_privileges_api(store: Store):
    # given
    http = FakeHttp({('POST', QUERY): by_query(), ('POST', PRIVILEGES): {'index': {'logs-*': {'read': False}, 'traces-apm*': {'read': False}}}})

    # when
    with app_with(store, http, main={'api-key': 'key-0042'}) as c:
        c.app.state.sweeper.run('elasticsearch', 'logs', NOW)
        c.app.state.sweeper.run('elasticsearch', 'apm', NOW)
    passes = store.load()['serverPasses']

    # then
    assert (passes['logs']['status'], passes['apm']['status']) == ('ok', 'ok')
    assert not any(a['sig'] == 'no-access' for a in card_map(store).values())
    assert http.hits(PRIVILEGES) == [], 'the privileges API answers for the pattern as typed, not for the indices a read reached'


def test_access_and_fields_cards_get_no_kibana_links():
    # given
    config = info().config
    refused = elastic.access_card('apm', 'Elasticsearch', 'apm', 'traces-apm*', 403)
    fields = elastic.fields_card('logflow', 'logflow', 'Elasticsearch', '-', 'logs', ('kubernetes.namespace',), 'logs-*')

    # then
    assert checks.links(refused, InstanceInfo('main', True, None, config)) is None
    assert checks.links(fields, InstanceInfo('main', True, None, config)) is None


def test_the_elasticsearch_test_reads_only_what_the_switched_on_checks_need(store: Store):
    # given
    root = f'{ES_URL}/'
    denied = HttpError(403, f'{ES_URL}/traces-apm*/_search', es_error('security_exception', 'unauthorized'))
    http = FakeHttp({('GET', root): {'version': {'number': '8.15.0'}}, **searched(ES_URL, 'logs-*'), ('POST', f'{ES_URL}/traces-apm*/_search'): denied, ('POST', QUERY): esql([])})

    def tested(off: set[str], **values):
        runtime = es_runtime(store, {'main': {'api-key': 'secret-key', **values}}, http)
        ctx = runtime.context(elasticsearch.PLUGIN, runtime.instances.get('elasticsearch', 'main'), NOW)
        ctx.instance = dataclasses.replace(ctx.instance, off_checks=frozenset(off))
        return elasticsearch.test(ctx)

    # when
    with_apm, without_apm = tested(set()), tested({'apm'})

    # then
    assert not with_apm.ok and 'traces-apm*' in with_apm.summary
    assert (without_apm.ok, without_apm.summary) == (True, 'Elasticsearch 8.15.0')


def test_the_elasticsearch_test_names_a_namespace_field_the_log_flow_check_needs(store: Store):
    # given
    root = f'{ES_URL}/'
    unknown = HttpError(400, QUERY, es_error('verification_exception', 'Found 1 problem\nline 1:20: Unknown column [kubernetes.namespace]'))

    def answer(params, body):
        return unknown if 'kubernetes.namespace' in body['query'] else esql([])

    http = FakeHttp({('GET', root): {'version': {'number': '8.15.0'}}, **searched(ES_URL, 'logs-*', 'traces-apm*'), ('POST', QUERY): answer})

    def tested(off: set[str]):
        runtime = es_runtime(store, {'main': {'api-key': 'secret-key'}}, http)
        ctx = runtime.context(elasticsearch.PLUGIN, runtime.instances.get('elasticsearch', 'main'), NOW)
        ctx.instance = dataclasses.replace(ctx.instance, off_checks=frozenset(off))
        return elasticsearch.test(ctx)

    # when
    flow_on, flow_off = tested(set()), tested({'logflow'})

    # then
    assert (flow_on.ok, flow_on.summary) == (False, 'logs-* lacks the field kubernetes.namespace, which the log flow check needs')
    assert flow_off.ok


def test_the_agent_esql_read_refuses_an_unbounded_scan_and_stops_within_the_agent_wait():
    # given
    seen = []

    def transport(method, url, headers, data, timeout):
        seen.append(timeout)
        return remote.Reply(esql([{'n': 1}]), {})

    config = {'elastic.logging-url': ES_URL, 'elastic.logging-api-key': 'own-es-key'}

    # when
    with pytest.raises(ReadError) as unbounded:
        reads.agent_esql(config, {'cluster': 'logs', 'query': 'FROM logs-* | STATS n = COUNT(*)'}, transport)
    reads.agent_esql(config, {'cluster': 'logs', 'query': 'FROM logs-* | WHERE @timestamp >= NOW() - 1 hour | STATS n = COUNT(*)'}, transport)

    # then
    assert unbounded.value.status == 422 and 'no time bound' in unbounded.value.message
    assert seen and max(seen) < reads.TIMEOUT_S


def test_the_small_elastic_notes_of_the_log_flow_review_hold():
    # given
    short = Elastic(ES_URL, 'k', FakeHttp({('POST', QUERY): HttpError(400, QUERY, es_error('verification_exception', 'Unknown column [kubernetes.namespace]'))}))
    spaced = FakeHttp({('POST', f'{ES_URL}/logs-*,app-*/_search'): {'_shards': {'total': 2}}})
    proxied = FakeHttp({('POST', PRIVILEGES): HttpError(403, PRIVILEGES)})
    spent = elastic.Budget(0, clock=lambda: 0.0)

    # when
    with pytest.raises(ElasticError) as unknown:
        short.query('FROM logs-* | KEEP `kubernetes.namespace`')
    shards = Elastic(ES_URL, '', spaced).shards('logs-*, app-*')
    keyless = elastic.refusal(Elastic(ES_URL, '', proxied), ElasticError('ES|QL HTTP 400', missing=True), ('logs-*',), elastic.Budget())
    keyed = elastic.refusal(Elastic(ES_URL, 'key-0042', proxied), ElasticError('ES|QL HTTP 400', missing=True), ('logs-*',), elastic.Budget())
    with pytest.raises(ElasticError) as late:
        elastic.refusal(Elastic(ES_URL, '', proxied), ElasticError('ES|QL HTTP 400', missing=True), ('logs-*',), spent)

    # then
    assert unknown.value.unknown == ('kubernetes.namespace',), 'a short key masks after the parse, so the column name survives'
    assert shards == 2, 'a spaced index list is joined with commas'
    assert (keyless, keyed) == (False, True), 'a keyless instance behind a proxy that refuses the security API reads a missing index as missing'
    assert late.value.reason == 'ES|QL pass budget spent', 'a spent budget is reported, never read as a missing index'


def test_every_agent_read_field_explains_itself():
    # then
    assert all(f.help for f in reads.AGENT_FIELDS)
