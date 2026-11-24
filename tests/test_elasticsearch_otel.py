from skopax.contract import ReadError
from skopax.engine import iso
from skopax.plugins import elasticsearch
from skopax.plugins.elasticsearch import checks, esql as reads
from skopax.remote import HttpError
from skopax.store import Store
from tests.fake_http import FakeHttp
from tests.test_elasticsearch_plugin import (APM_ROWS, DEFAULT_APM, DEFAULT_RULES, ES_URL, NOW, es_error, OTEL_INDEX, PRIVILEGES, QUERY, app_with, card_map, es_runtime, esql, info,
                                             missing_index, searched)

OTEL_ROWS = [
    {'svc': 'basket-api', 'n_now': 600, 'r_now': 550, 'p95_now': 1500.0, 'p95_base': 400.0, 'f_now': 0, 'fr_now': 0.0, 'fr_base': 0.0, 'slow': True, 'failing': False},
    {'svc': 'ledger-api', 'n_now': 300, 'r_now': 290, 'p95_now': 90.0, 'p95_base': 80.0, 'f_now': 40, 'fr_now': 0.13, 'fr_base': 0.01, 'slow': False, 'failing': True},
]
SHARED_ROWS = [
    {'svc': 'checkout-service', 'n_now': 900, 'r_now': 850, 'p95_now': 2000.0, 'p95_base': 500.0, 'f_now': 0, 'fr_now': 0.0, 'fr_base': 0.0, 'slow': True, 'failing': False},
]
CLASSIC_QUERY = '''FROM traces-apm*
| WHERE processor.event == "transaction" AND ({window})
| EVAL w = CASE(@timestamp >= NOW() - 60 minutes, "now", @timestamp >= NOW() - 2 days, "d1", "d7"), ms = transaction.duration.us / 1000.0, f = CASE(event.outcome == "failure", 1, 0), rq = transaction.type == "request"
| STATS n_now = COUNT(CASE(w == "now", 1, null)), n_d1 = COUNT(CASE(w == "d1", 1, null)), n_d7 = COUNT(CASE(w == "d7", 1, null)), r_now = COUNT(CASE(w == "now" AND rq, 1, null)), r_d1 = COUNT(CASE(w == "d1" AND rq, 1, null)), r_d7 = COUNT(CASE(w == "d7" AND rq, 1, null)), p95_now = PERCENTILE(CASE(w == "now" AND rq, ms, null), 95), p95_d1 = PERCENTILE(CASE(w == "d1" AND rq, ms, null), 95), p95_d7 = PERCENTILE(CASE(w == "d7" AND rq, ms, null), 95), f_now = SUM(CASE(w == "now", f, 0)), f_d1 = SUM(CASE(w == "d1", f, 0)), f_d7 = SUM(CASE(w == "d7", f, 0)) BY svc = service.name
| WHERE n_now >= 100 AND n_d1 >= 50 AND n_d7 >= 50
| EVAL p95_base = GREATEST(p95_d1, p95_d7), fr_now = f_now * 1.0 / n_now, fr_base = GREATEST(f_d1 * 1.0 / n_d1, f_d7 * 1.0 / n_d7)
| EVAL slow = r_now >= 100 AND r_d1 >= 50 AND r_d7 >= 50 AND p95_now > 2 * p95_base AND p95_now - p95_base >= 200, failing = f_now >= 10 AND fr_now > 2 * fr_base + 0.005
| WHERE slow OR failing
| KEEP svc, n_now, r_now, p95_now, p95_base, f_now, fr_now, fr_base, slow, failing
| LIMIT 50'''


def shaped(classic, otel):
    def answer(params, body):
        query = body['query']
        if query.startswith(f'FROM {OTEL_INDEX}\n'):
            return otel
        if query.startswith('FROM traces-apm*\n'):
            return classic
        return esql([])

    return answer


def swept(store: Store, http: FakeHttp, **values) -> tuple[dict, dict]:
    with app_with(store, http, main={'api-key': 'lk', **values}) as c:
        c.app.state.sweeper.run('elasticsearch', 'apm', NOW)
        state = store.load()
    return state['serverPasses']['apm'], {k: a for k, a in card_map(store).items() if a['src'] == 'apm'}


def queries(http: FakeHttp) -> list[str]:
    return [c[3]['query'] for c in http.hits(QUERY)]


def test_the_default_apm_index_names_classic_and_otel_native_traces_and_splits_them_by_shape():
    # when
    default = checks.families({})
    custom = checks.families({'apm-index': 'traces-apm*, traces-generic.otel-default,apm-legacy-*,traces-*.otel*'})

    # then
    assert {f.key: f.default for f in elasticsearch.PLUGIN.fields}['apm-index'] == DEFAULT_APM == 'traces-apm*,traces-*.otel-*'
    assert default == [('classic', 'traces-apm*'), ('otel', 'traces-*.otel-*')]
    assert custom == [('classic', 'traces-apm*,apm-legacy-*'), ('otel', 'traces-generic.otel-default,traces-*.otel*')]
    assert checks.families({'apm-index': 'traces-apm*'}) == [('classic', 'traces-apm*')]


def test_the_otel_native_query_reads_semantic_convention_fields_and_turns_nanoseconds_into_milliseconds():
    # when
    otel = checks.apm_query(info(**{'apm-index': DEFAULT_APM}).config, DEFAULT_RULES, 'otel', OTEL_INDEX)
    count = checks.apm_count(info(**{'apm-index': DEFAULT_APM}).config, 'otel', OTEL_INDEX)

    # then
    assert otel.startswith('FROM traces-*.otel-*\n| WHERE (kind IN ("Server", "Consumer") OR parent_span_id IS NULL OR parent_span_id == "") AND (')
    assert otel.find('ms = duration / 1000000.0') >= 0 and otel.find('f = CASE(status.code == "Error", 1, 0)') >= 0 and otel.find('rq = kind == "Server"') >= 0
    assert otel.find('BY svc = resource.attributes.service.name\n') >= 0
    assert otel.find('processor.event') < 0 and otel.find('transaction.') < 0 and otel.find('event.outcome') < 0
    assert otel.split('\n')[4:] == checks.apm_query(info().config, DEFAULT_RULES).split('\n')[4:]
    assert count == 'FROM traces-*.otel-*\n| WHERE (kind IN ("Server", "Consumer") OR parent_span_id IS NULL OR parent_span_id == "") AND @timestamp >= NOW() - 60 minutes\n| STATS n = COUNT(*)'


def test_an_otel_native_install_gets_failure_and_p95_cards_in_milliseconds(store: Store):
    # given
    http = FakeHttp({('POST', QUERY): shaped(missing_index(), esql(OTEL_ROWS))})

    # when
    status, cards = swept(store, http)

    # then
    assert status['status'] == 'ok'
    assert set(cards) == {'apm|basket-api|p95', 'apm|ledger-api|fail'}
    p95 = cards['apm|basket-api|p95']
    assert (p95['sig'], p95['sev'], p95['now'], p95['unit']) == ('apm-p95', 'info', 1500.0, 'ms')
    assert cards['apm|ledger-api|fail']['sig'] == 'apm-fail' and cards['apm|ledger-api|fail']['sev'] == 'high'
    assert [q.split('\n')[0] for q in queries(http)] == ['FROM traces-apm*', 'FROM traces-*.otel-*']


def test_a_mixed_install_keeps_each_signal_of_a_service_from_the_shape_that_raises_it(store: Store):
    # given
    http = FakeHttp({('POST', QUERY): shaped(esql(APM_ROWS), esql(OTEL_ROWS + SHARED_ROWS))})

    # when
    status, cards = swept(store, http)

    # then
    classic_keys = {x['key'] for x in checks.evaluate_apm(APM_ROWS, set(), DEFAULT_RULES)}
    assert status['status'] == 'ok'
    assert set(cards) == classic_keys | {'apm|basket-api|p95', 'apm|ledger-api|fail'}
    assert cards['apm|checkout-service|p95']['now'] == 2000.0 and cards['apm|checkout-service|fail']['now'] == 20
    keys = [x['key'] for x in checks.evaluate_apm(checks.merged([APM_ROWS, SHARED_ROWS]), set(), DEFAULT_RULES)]
    assert sorted(keys) == sorted(set(keys)) and set(keys) == classic_keys


def test_a_classic_only_install_sends_the_same_query_and_gets_the_same_cards(store: Store):
    # given
    pinned = FakeHttp({('POST', QUERY): shaped(esql(APM_ROWS), esql([]))})
    defaulted = FakeHttp({('POST', QUERY): shaped(esql(APM_ROWS), missing_index(OTEL_INDEX)), ('POST', PRIVILEGES): {'index': {OTEL_INDEX: {'read': False}}}})

    # when
    pinned_status, pinned_cards = swept(store, pinned, **{'apm-index': 'traces-apm*'})
    _, defaulted_cards = swept(store, defaulted)

    # then
    expected = {x['key'] for x in checks.evaluate_apm(APM_ROWS, set(), DEFAULT_RULES)}
    assert pinned_status['status'] == 'ok' and set(pinned_cards) == expected
    assert queries(pinned) == [CLASSIC_QUERY.format(window=checks.window())]
    assert set(defaulted_cards) == expected and not any(a['sig'] == 'no-access' for a in card_map(store).values())
    assert queries(defaulted)[0] == CLASSIC_QUERY.format(window=checks.window())
    assert defaulted.hits(PRIVILEGES) == []


def test_apm_without_either_shape_is_no_data_unless_the_key_can_read_neither(store: Store):
    # given
    def http(classic: bool, otel: bool) -> FakeHttp:
        return FakeHttp({('POST', QUERY): shaped(missing_index(), missing_index(OTEL_INDEX)),
                         ('POST', PRIVILEGES): {'index': {'traces-apm*': {'read': classic}, OTEL_INDEX: {'read': otel}}}})

    # when
    nothing, _ = swept(store, http(True, True))
    old_key, _ = swept(store, http(True, False))
    quiet_access = [a for a in card_map(store).values() if a['sig'] == 'no-access']
    no_key, _ = swept(store, http(False, False))
    access = [a for a in card_map(store).values() if a['sig'] == 'no-access']

    # then
    assert (nothing['status'], old_key['status']) == ('ok \u00b7 no data', 'ok \u00b7 no data') and quiet_access == []
    assert no_key['status'] == 'ok \u00b7 no access to apm'
    assert [a['label'] for a in access] == [f'Elasticsearch cannot read apm: the API key lacks the read privilege on {DEFAULT_APM}']


def test_the_connection_test_names_the_trace_shapes_it_found(store: Store):
    # given
    root = f'{ES_URL}/'
    base = {('GET', root): {'version': {'number': '8.15.0'}}, **searched(ES_URL, 'logs-*')}
    both = FakeHttp({**base, **searched(ES_URL, 'traces-apm*', OTEL_INDEX)})
    otel = FakeHttp({**base, **searched(ES_URL, 'traces-apm*', shards=0), **searched(ES_URL, OTEL_INDEX)})
    classic = FakeHttp({**base, **searched(ES_URL, 'traces-apm*'), **searched(ES_URL, OTEL_INDEX, shards=0)})
    denied = FakeHttp({**base, **searched(ES_URL, 'traces-apm*'), ('POST', f'{ES_URL}/{OTEL_INDEX}/_search'): {'_shards': {'total': 0}},
                       ('POST', PRIVILEGES): {'index': {OTEL_INDEX: {'read': False}}}})
    forbidden = HttpError(403, f'{ES_URL}/{OTEL_INDEX}/_search', es_error('security_exception', 'unauthorized'))
    otel_403 = FakeHttp({**base, **searched(ES_URL, 'traces-apm*'), ('POST', f'{ES_URL}/{OTEL_INDEX}/_search'): forbidden})
    fresh = FakeHttp({**base, **searched(ES_URL, 'traces-apm*', OTEL_INDEX, shards=0), ('POST', PRIVILEGES): {'index': {'traces-apm*': {'read': True}, OTEL_INDEX: {'read': False}}}})
    unread = FakeHttp({**base, **searched(ES_URL, 'traces-apm*', OTEL_INDEX, shards=0), ('POST', PRIVILEGES): {'index': {'traces-apm*': {'read': False}, OTEL_INDEX: {'read': False}}}})

    def check(http):
        rt = es_runtime(store, {'main': {'api-key': 'lk'}}, http)
        return elasticsearch.test(rt.context(elasticsearch.PLUGIN, rt.instances.get('elasticsearch', 'main'), NOW))

    # when
    found = {name: check(http) for name, http in (('both', both), ('otel', otel), ('classic', classic), ('denied', denied), ('otel_403', otel_403), ('fresh', fresh), ('unread', unread))}

    # then
    assert {k: (r.ok, r.summary) for k, r in found.items()} == {'both': (True, 'Elasticsearch 8.15.0, APM data: classic APM and OTel-native'),
                                                                'otel': (True, 'Elasticsearch 8.15.0, APM data: OTel-native'),
                                                                'classic': (True, 'Elasticsearch 8.15.0'),
                                                                'denied': (True, 'Elasticsearch 8.15.0'),
                                                                'otel_403': (True, 'Elasticsearch 8.15.0'),
                                                                'fresh': (True, f'Elasticsearch 8.15.0, no index matches {DEFAULT_APM} yet (the API key would also need read on {OTEL_INDEX})'),
                                                                'unread': (False, f'The API key lacks the read privilege on {DEFAULT_APM}, which the APM check reads.')}
    assert [c[3] for c in both.hits(f'{ES_URL}/{OTEL_INDEX}/_search')] == [{'size': 0}]


def test_the_esql_scope_check_takes_the_comma_list_of_both_shapes():
    # when
    reads.check_scope(f'FROM {DEFAULT_APM} | WHERE @timestamp >= NOW() - 1 hour | STATS c = COUNT(*)', DEFAULT_APM)
    try:
        reads.check_scope(f'FROM {DEFAULT_APM} | STATS c = COUNT(*)', DEFAULT_APM)
        refused = ''
    except ReadError as e:
        refused = e.message

    # then
    assert refused.endswith(f'like FROM {DEFAULT_APM} | WHERE @timestamp >= NOW() - 1 hour')
    assert refused.startswith('traces-apm*, traces-*.otel-* scans more than 3 daily indices')


def test_a_refused_otel_native_read_is_an_absent_shape_while_classic_apm_answers(store: Store):
    # given
    refused = HttpError(403, QUERY, es_error('security_exception', 'action [indices:data/read/esql] is unauthorized'))
    http = FakeHttp({('POST', QUERY): shaped(esql(APM_ROWS), refused), ('POST', PRIVILEGES): {'index': {OTEL_INDEX: {'read': False}}}})

    # when
    status, cards = swept(store, http)

    # then
    assert status['status'] == 'ok'
    assert set(cards) == {x['key'] for x in checks.evaluate_apm(APM_ROWS, set(), DEFAULT_RULES)}
    assert not any(a['sig'] == 'no-access' for a in card_map(store).values()) and http.hits(PRIVILEGES) == []


def test_a_refusal_opens_the_access_card_only_when_no_shape_answers(store: Store):
    # given
    refused = HttpError(403, QUERY, es_error('security_exception', 'action [indices:data/read/esql] is unauthorized'))
    both = FakeHttp({('POST', QUERY): shaped(refused, refused)})
    classic = FakeHttp({('POST', QUERY): shaped(refused, missing_index(OTEL_INDEX)), ('POST', PRIVILEGES): {'index': {OTEL_INDEX: {'read': True}}}})
    empty_otel = FakeHttp({('POST', QUERY): shaped(refused, esql([]))})

    # when
    both_status, _ = swept(store, both)
    both_labels = [a['label'] for a in card_map(store).values() if a['sig'] == 'no-access']
    classic_status, _ = swept(store, classic)
    classic_labels = [a['label'] for a in card_map(store).values() if a['sig'] == 'no-access']
    empty_status, _ = swept(store, empty_otel)
    empty_labels = [a['label'] for a in card_map(store).values() if a['sig'] == 'no-access']

    # then
    assert (both_status['status'], classic_status['status']) == ('ok \u00b7 no access to apm', 'ok \u00b7 no access to apm')
    assert both_labels == [f'Elasticsearch cannot read apm: the API key lacks the read privilege on {DEFAULT_APM}']
    assert classic_labels == ['Elasticsearch cannot read apm: the API key lacks the read privilege on traces-apm*']
    assert empty_status['status'] == 'ok \u00b7 no access to apm' and empty_labels == classic_labels


def test_a_broken_otel_native_read_fails_the_pass_and_keeps_an_open_classic_card(store: Store):
    # given
    broken = HttpError(500, QUERY, es_error('illegal_state_exception', 'shard failure'))
    card = {'key': 'apm|orders-service|fail', 'src': 'apm', 'sig': 'apm-fail', 'ns': '-', 'svc': 'orders-service', 'sev': 'high', 'status': 'open', 'now': 30, 'label': 'failed transactions',
            'what': 'x', 'short': 'HIGH orders-service failed transactions', 'firstSeen': iso(NOW - 3600), 'lastSeen': iso(NOW - 900)}
    store.save_state({'anomalies': [card], 'initialized': True}, None)

    # when
    status, cards = swept(store, FakeHttp({('POST', QUERY): shaped(esql(APM_ROWS), broken)}))

    # then
    assert status['status'].startswith('error: ES|QL HTTP 500')
    assert set(cards) == {'apm|orders-service|fail'} and cards['apm|orders-service|fail']['status'] == 'open'
