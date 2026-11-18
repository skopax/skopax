import json

from skopax.engine import iso
from skopax.kit.elastic import PASS_BUDGET, Budget, Elastic
from skopax.plugins.elasticsearch import logflow
from skopax.remote import HttpError
from skopax.store import Store
from skopax.sweeps import CHECK_TIMEOUT_S
from tests.fake_http import FakeHttp
from tests.test_elasticsearch_plugin import ES_URL, KIBANA, NOW, NS_PATTERN, PRIVILEGES, QUERY, USER, app_with, card_map, es_error, info, link_params, missing_index

FLOW = {'index-namespace': NS_PATTERN}
EAST = 'https://east.example.com'


def flow_answer(stalled: bool = False):
    def answer(params, body):
        text = body['query']
        if 'days' not in text:
            if stalled:
                return {'columns': [{'name': 'n'}, {'name': 'newest'}, {'name': '_index'}], 'values': [[40, iso(NOW - 1200), 'app-auth-logs-2026.11.04']]}
            return {'columns': [{'name': 'n'}, {'name': 'newest'}, {'name': '_index'}],
                    'values': [[9000, iso(NOW - 30), 'app-auth-logs-2026.11.04'], [800, iso(NOW - 40), '.ds-app-payments-logs-2026.11.04-000001'], [3, iso(NOW - 900), 'app-tools-logs-2026.11.04']]}
        return {'columns': [{'name': 'n'}, {'name': '_index'}],
                'values': [[8800, 'app-auth-logs-2026.11.03'], [900, 'app-payments-logs-2026.11.03'], [700, 'app-orders-logs-2026.11.03'], [2, 'app-tools-logs-2026.11.03']]}
    return answer


def field_answer(params, body):
    text = body['query']
    if 'days' not in text:
        return {'columns': [{'name': 'n'}, {'name': 'newest'}, {'name': 'kubernetes.namespace'}],
                'values': [[9000, iso(NOW - 30), 'auth'], [800, iso(NOW - 40), 'payments'], [5, iso(NOW - 60), None]]}
    return {'columns': [{'name': 'n'}, {'name': 'kubernetes.namespace'}], 'values': [[8800, 'auth'], [900, 'payments'], [700, 'orders'], [9, None]]}


def flow_of(sweep) -> tuple[dict, list[dict]]:
    rows = sweep.sections['logflow']
    [summary] = [r for r in rows if r['part'] == 'summary']
    return summary, [r for r in rows if r['part'] == 'namespace']


def open_flow_card(**extra) -> dict:
    return {'key': 'logs-stopped|orders/logs|flow', 'src': 'logflow', 'sig': 'logs-stopped', 'ns': 'orders', 'svc': 'logs', 'sub': 'flow', 'sev': 'warn', 'status': 'open', 'now': 0,
            'label': 'Logs stopped arriving', 'what': 'x', 'short': 'WARN orders logs', 'ranKey': 'logflow', 'firstSeen': iso(NOW - 3600), 'lastSeen': iso(NOW - 600), **extra}


def test_the_log_flow_check_cards_a_namespace_that_went_quiet_and_reads_the_newest_line():
    # given
    es = Elastic(ES_URL, 'k', FakeHttp({('POST', QUERY): flow_answer()}))

    # when
    out = logflow.sweep(es, info(**FLOW).config, NOW, dict(logflow.RULES))

    # then
    assert out.result.ok and out.result.ran == ('logflow', 'logflow:fields', 'logflow:access:logs')
    assert [c['key'] for c in out.result.found] == ['logs-stopped|orders/logs|flow']
    summary, rows = flow_of(out)
    assert summary['lagMin'] == 0.5 and summary['total'] == {'n': 9803, 'd1': 10402, 'd7': 10402}
    assert {r['ns']: r['n'] for r in rows} == {'auth': 9000, 'orders': 0, 'payments': 800, 'tools': 3}


def test_a_stalled_pipeline_is_one_critical_card():
    # given
    es = Elastic(ES_URL, 'k', FakeHttp({('POST', QUERY): flow_answer(stalled=True)}))

    # when
    out = logflow.sweep(es, info(**FLOW).config, NOW, dict(logflow.RULES))

    # then
    lag = [c for c in out.result.found if c['sig'] == 'logs-lag']
    assert len(lag) == 1 and lag[0]['sev'] == 'crit' and lag[0]['short'].startswith('CRIT log pipeline: 40 lines in 20 min')
    assert (lag[0]['ns'], lag[0]['svc'], lag[0]['ranKey']) == ('-', 'pipeline', 'logflow')
    assert not any(word in lag[0]['what'].lower() for word in ('kafka', 'logstash', 'tab'))


def test_the_log_flow_queries_read_the_configured_index_over_each_window():
    # when
    q = logflow.window_query(info(**FLOW, **{'log-index': 'app-*'}).config, 1, False)
    now = logflow.window_query(info(**FLOW, **{'log-index': 'app-*'}).config, 0, True)

    # then
    assert q.startswith('FROM app-* METADATA _index\n')
    assert 'NOW() - 1 days - 20 minutes AND @timestamp < NOW() - 1 days' in q and 'newest' not in q
    assert '@timestamp >= NOW() - 20 minutes\n' in now and 'n = COUNT(*), newest = MAX(@timestamp) BY _index' in now


def test_logflow_groups_by_the_namespace_field_or_the_index_name():
    # given
    by_index = Elastic(ES_URL, 'k', FakeHttp({('POST', QUERY): flow_answer()}))
    by_field_http = FakeHttp({('POST', QUERY): field_answer})
    by_field = Elastic(ES_URL, 'k', by_field_http)

    # when
    from_index = logflow.sweep(by_index, info(**FLOW).config, NOW, dict(logflow.RULES))
    from_field = logflow.sweep(by_field, info(**{'index-namespace': '', 'namespace-field': 'kubernetes.namespace'}).config, NOW, dict(logflow.RULES))
    asked = [c[3]['query'] for c in by_field_http.calls if 'query' in (c[3] or {})]

    # then
    assert {r['ns'] for r in flow_of(from_index)[1]} == {'auth', 'orders', 'payments', 'tools'}
    assert {r['ns']: (r['n'], r['d1']) for r in flow_of(from_field)[1]} == {'auth': (9000, 8800), 'orders': (0, 700), 'payments': (800, 900)}
    assert [c['key'] for c in from_field.result.found] == ['logs-stopped|orders/logs|flow']
    assert all('METADATA' not in q and q.endswith('BY `kubernetes.namespace`') for q in asked) and len(asked) == 3


def test_log_flow_needs_a_namespace_source_and_a_pattern_with_ns():
    # given
    http = FakeHttp({('POST', QUERY): flow_answer()})
    es = Elastic(ES_URL, 'k', http)

    # when
    none = logflow.sweep(es, info(**{'index-namespace': '', 'namespace-field': ''}).config, NOW, dict(logflow.RULES))
    bad = logflow.sweep(es, info(**{'index-namespace': r'app-(.+)-logs-.*'}).config, NOW, dict(logflow.RULES))

    # then
    assert (none.result.ok, none.result.status) == (False, 'error: log flow needs index-namespace or namespace-field')
    assert not bad.result.ok and bad.result.status.startswith('error: index-namespace needs a group named ns')
    assert http.calls == []


def test_logflow_without_logs_is_ok_and_quiet(store: Store):
    # given
    empty = {'columns': [{'name': 'n'}, {'name': '_index'}], 'values': []}
    unknown = HttpError(400, QUERY, es_error('verification_exception', 'Found 1 problem\nline 4:20: Unknown column [kubernetes.namespace]'))
    cases = {'missing': (FakeHttp({('POST', QUERY): missing_index('logs-*')}), FLOW, set()),
             'empty': (FakeHttp({('POST', QUERY): empty}), FLOW, set()),
             'unknown': (FakeHttp({('POST', QUERY): unknown}), {}, {'logflow-fields|-/logs|fields'})}

    for name, (http, config, extra) in cases.items():
        store.anomalies.delete_many({})
        store.save_state({'anomalies': [open_flow_card()], 'initialized': True}, None)

        # when
        with app_with(store, http, main=config) as c:
            c.app.state.sweeper.run('elasticsearch', 'logflow', NOW)
            state = store.load()
        cards = card_map(store)

        # then
        assert state['serverPasses']['logflow']['status'] == 'ok \u00b7 no data', name
        assert cards['logs-stopped|orders/logs|flow']['status'] == 'open', name
        assert {k for k in cards if k != 'logs-stopped|orders/logs|flow'} == extra, name
        assert [r for r in state['logflow'] if r.get('part') == 'namespace'] == [], name


def test_a_missing_namespace_field_raises_a_card_that_clears_when_the_field_arrives(store: Store):
    # given
    unknown = HttpError(400, QUERY, es_error('verification_exception', 'Found 1 problem\nline 4:20: Unknown column [kubernetes.namespace]'))
    http = FakeHttp({('POST', QUERY): unknown})

    # when
    with app_with(store, http, main={'ui-url': KIBANA}) as c:
        c.app.state.sweeper.run('elasticsearch', 'logflow', NOW)
        first = card_map(store)
        http.routes[('POST', QUERY)] = field_answer
        c.app.state.sweeper.run('elasticsearch', 'logflow', NOW + 600)
        second = card_map(store)

    # then
    card = first['logflow-fields|-/logs|fields']
    assert (card['sev'], card['ranKey'], card['status']) == ('warn', 'logflow:fields', 'open')
    assert card['label'] == 'Elasticsearch logs lack the field kubernetes.namespace'
    assert 'or the API key cannot read it' in card['what'] and 'logs-*' in card['what']
    assert second['logflow-fields|-/logs|fields']['status'] == 'cleared'


def test_log_flow_refused_for_permission_is_never_no_data(store: Store):
    # given
    denied = HttpError(403, QUERY, es_error('security_exception', 'action [indices:data/read/esql] is unauthorized for API key id [key-0042]'))
    cases = {'refused': FakeHttp({('POST', QUERY): denied}),
             'hidden': FakeHttp({('POST', QUERY): missing_index('logs-*'), ('POST', PRIVILEGES): {'has_all_requested': False, 'index': {'logs-*': {'read': False}}}})}

    for name, http in cases.items():
        store.anomalies.delete_many({})
        store.save_state({'anomalies': [open_flow_card()], 'initialized': True}, None)

        # when
        with app_with(store, http, main={**FLOW, 'api-key': 'key-0042'}) as c:
            c.app.state.sweeper.run('elasticsearch', 'logflow', NOW)
            state = store.load()
            cards = card_map(store)
            http.routes[('POST', QUERY)] = flow_answer()
            http.routes[('POST', PRIVILEGES)] = {'has_all_requested': True, 'index': {'logs-*': {'read': True}}}
            c.app.state.sweeper.run('elasticsearch', 'logflow', NOW + 600)
        after = card_map(store)

        # then
        assert state['serverPasses']['logflow']['status'] == 'ok \u00b7 no access to logs', name
        assert cards['logs-stopped|orders/logs|flow']['status'] == 'open', name
        [access] = [a for a in cards.values() if a['sig'] == 'no-access']
        assert (access['ranKey'], access['sev']) == ('logflow:access:logs', 'warn'), name
        assert access['label'] == 'Elasticsearch cannot read logs: the API key lacks the read privilege on logs-*', name
        assert 'key-0042' not in access['what'] + access['label'], name
        assert after[access['key']]['status'] == 'cleared', name


def test_the_three_log_flow_reads_share_one_pass_budget():
    # given
    class Slow:
        def __init__(self, step: float):
            self.t, self.step, self.timeouts = 0.0, step, []

        def clock(self) -> float:
            return self.t

        def query(self, esql: str, timeout: float) -> list[dict]:
            self.timeouts.append(timeout)
            self.t += self.step
            answer = flow_answer()({}, {'query': esql})
            return [dict(zip([c['name'] for c in answer['columns']], v)) for v in answer['values']]

        def unreadable(self, indices, timeout):
            return []

    fast, slow = Slow(60), Slow(110)

    # when
    ok = logflow.sweep(fast, info(**FLOW).config, NOW, dict(logflow.RULES), Budget(200, clock=fast.clock))
    late = logflow.sweep(slow, info(**FLOW).config, NOW, dict(logflow.RULES), Budget(200, clock=slow.clock))

    # then
    assert ok.result.ok and fast.timeouts == [120, 120, 80]
    assert (late.result.ok, late.result.status) == (False, 'error: ES|QL pass budget spent') and slow.timeouts == [120, 90]
    assert PASS_BUDGET == 200 and PASS_BUDGET + 30 <= CHECK_TIMEOUT_S


def test_an_empty_current_window_is_not_no_data():
    # given
    def answer(params, body):
        if 'days' not in body['query']:
            return {'columns': [{'name': 'n'}, {'name': 'newest'}, {'name': '_index'}], 'values': []}
        return {'columns': [{'name': 'n'}, {'name': '_index'}], 'values': [[8800, 'app-auth-logs-2026.11.03']]}

    # when
    out = logflow.sweep(Elastic(ES_URL, 'k', FakeHttp({('POST', QUERY): answer})), info(**FLOW).config, NOW, dict(logflow.RULES))

    # then
    assert out.result.ran == ('logflow', 'logflow:fields', 'logflow:access:logs') and out.result.status == 'ok'
    assert {c['sig'] for c in out.result.found} == {'logs-stopped', 'logs-lag'}


def test_a_failed_log_flow_query_fails_the_pass_and_keeps_its_cards(store: Store):
    # given
    http = FakeHttp({('POST', QUERY): HttpError(504, QUERY, 'gateway timeout')})
    store.save_state({'anomalies': [open_flow_card()], 'initialized': True}, None)

    # when
    with app_with(store, http, main=FLOW) as c:
        c.app.state.sweeper.run('elasticsearch', 'logflow', NOW)
        state = store.load()

    # then
    assert state['serverPasses']['logflow']['status'] == 'error: ES|QL HTTP 504'
    assert card_map(store)['logs-stopped|orders/logs|flow']['status'] == 'open'


def test_the_logflow_check_runs_every_ten_minutes_and_links_its_cards(store: Store):
    # given
    http = FakeHttp({('POST', QUERY): flow_answer(stalled=True)})

    # when
    with app_with(store, http, main={**FLOW, 'ui-url': KIBANA}) as c:
        job = next(j for j in c.app.state.scheduler.jobs if j.name == 'elasticsearch.logflow')
        c.app.state.sweeper.run('elasticsearch', 'logflow', NOW)
        state = store.load()
    cards = card_map(store)

    # then
    assert (job.period, job.offset) == (600, 270)
    assert state['serverPasses']['logflow']['status'] == 'ok'
    assert {a['sig'] for a in cards.values()} == {'logs-stopped', 'logs-lag'}
    assert all(a['ranKey'] == 'logflow' and 'cluster' not in a for a in cards.values())
    for a in cards.values():
        assert [x['label'] for x in a['links']] == ['Log lines in Kibana'], a['key']
        assert a['links'][0]['url'].startswith(f'{KIBANA}/app/discover#/') and "from:'now-2h'" in link_params(a['links'][0]['url'])
    assert {r['part'] for r in state['logflow']} == {'summary', 'namespace'}
    assert all(r['plugin'] == 'elasticsearch' and 'cluster' not in r for r in state['logflow'])


def test_logflow_of_a_second_instance_is_scoped(store: Store):
    # given
    quiet = {'columns': [{'name': 'n'}, {'name': '_index'}], 'values': []}
    http = FakeHttp({('POST', QUERY): quiet, ('POST', f'{EAST}/_query'): flow_answer()})

    # when
    with app_with(store, http, main=FLOW, east={'url': EAST, **FLOW}) as c:
        c.app.state.sweeper.run('elasticsearch', 'logflow', NOW)
        state = store.load()
    card = card_map(store)['logs-stopped|orders/logs|flow@east']

    # then
    assert (card['cluster'], card['ranKey']) == ('east', 'logflow-east')
    assert set(state['serverPasses']) >= {'logflow', 'logflow-east'}
    assert {r.get('cluster') for r in state['logflow']} == {'east'}


def test_logflow_rules_come_from_the_rule_set(store: Store):
    # given
    http = FakeHttp({('POST', QUERY): flow_answer()})

    # when
    with app_with(store, http, main=FLOW) as c:
        c.app.state.sweeper.run('elasticsearch', 'logflow', NOW)
        default = set(card_map(store))
        c.app.state.runtime.live.set('elasticsearch.logflow', json.dumps({'floor': 1000}), USER)
        store.anomalies.delete_many({})
        store.save_state({'anomalies': [], 'initialized': True}, None)
        c.app.state.sweeper.run('elasticsearch', 'logflow', NOW)
        stricter = set(card_map(store))

    # then
    assert default == {'logs-stopped|orders/logs|flow'} and stricter == set()


def test_a_keyless_instance_still_tells_a_refusal_from_a_missing_index(store: Store):
    # given
    http = FakeHttp({('POST', QUERY): missing_index('logs-*'), ('POST', PRIVILEGES): {'has_all_requested': False, 'index': {'logs-*': {'read': False}}}})
    store.save_state({'anomalies': [open_flow_card()], 'initialized': True}, None)

    # when
    with app_with(store, http, main=FLOW) as c:
        c.app.state.sweeper.run('elasticsearch', 'logflow', NOW)
        state = store.load()
    cards = card_map(store)

    # then
    assert state['serverPasses']['logflow']['status'] == 'ok \u00b7 no access to logs'
    assert cards['logs-stopped|orders/logs|flow']['status'] == 'open'
    assert [a['ranKey'] for a in cards.values() if a['sig'] == 'no-access'] == ['logflow:access:logs']
    assert 'Authorization' not in http.hits(PRIVILEGES)[0][2]


def test_a_refused_log_flow_read_empties_the_table_and_raises_the_access_card():
    # given
    denied = FakeHttp({('POST', QUERY): HttpError(403, QUERY, es_error('security_exception', 'unauthorized'))})

    # when
    refused = logflow.sweep(Elastic(ES_URL, 'key-0042', denied), info(**FLOW).config, NOW, logflow.RULES)

    # then
    assert refused.result.status == 'ok \u00b7 no access to logs'
    assert refused.sections == {'logflow': []}
    assert refused.result.ran == ('logflow:access:logs',)


def test_a_log_flow_read_that_works_is_never_second_guessed_by_the_privileges_api():
    # given
    http = FakeHttp({('POST', QUERY): flow_answer(), ('POST', PRIVILEGES): {'index': {'logs-*': {'read': False}}}})

    # when
    read = logflow.sweep(Elastic(ES_URL, 'key-0042', http), info(**FLOW).config, NOW, logflow.RULES)

    # then
    assert read.result.status == 'ok' and 'logflow' in read.result.ran
    assert http.hits(PRIVILEGES) == []
