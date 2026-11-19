import dataclasses
import urllib.parse

from skopax.kit import elastic
from skopax.plugins import elasticsearch
from skopax.plugins.elasticsearch import checks, logflow
from skopax.remote import HttpError
from tests.fake_http import FakeHttp
from tests.test_elasticsearch_plugin import APM_ROWS, DEFAULT_RULES, ES_URL, NOW, QUERY, es_error, es_runtime, esql, info, searched

ROOT = f'{ES_URL}/'


def connected(store, http, off=(), **values):
    runtime = es_runtime(store, {'main': {'api-key': 'secret-key', **values}}, http)
    ctx = runtime.context(elasticsearch.PLUGIN, runtime.instances.get('elasticsearch', 'main'), NOW)
    ctx.instance = dataclasses.replace(ctx.instance, off_checks=frozenset(off))
    return elasticsearch.test(ctx)


def test_the_log_flow_check_reads_its_own_index_when_one_is_set_and_the_log_index_when_not():
    # given
    own = info(**{'logflow-index': 'flow-*'}).config

    # when
    flow = logflow.window_query(own, 0, True)
    shared = logflow.window_query(info().config, 0, True)
    logs = checks.logs_query(own)

    # then
    assert flow.startswith('FROM flow-*\n') and shared.startswith('FROM logs-*\n')
    assert logs.startswith('FROM logs-*\n')
    assert elastic.index_of(own, 'logflow') == 'flow-*' and elastic.index_of(own, 'logs') == 'logs-*'
    assert elastic.index_of(info(**{'logflow-index': '  '}).config, 'logflow') == 'logs-*'


def test_the_test_reads_a_separate_log_flow_index_and_names_it_when_the_key_cannot_read_it(store):
    # given
    denied = HttpError(403, f'{ES_URL}/flow-*/_search', es_error('security_exception', 'unauthorized'))
    routes = {('GET', ROOT): {'version': {'number': '8.15.0'}}, **searched(ES_URL, 'logs-*', 'traces-apm*'), ('POST', QUERY): esql([])}

    # when
    ok = connected(store, FakeHttp({**routes, **searched(ES_URL, 'flow-*')}), **{'logflow-index': 'flow-*'})
    refused = connected(store, FakeHttp({**routes, ('POST', f'{ES_URL}/flow-*/_search'): denied}), **{'logflow-index': 'flow-*'})
    same = connected(store, FakeHttp({**routes}))

    # then
    assert ok.ok and refused.ok is False and 'flow-*' in refused.summary and 'log flow check' in refused.summary
    assert same.ok


def test_an_override_does_not_loosen_the_query_for_other_services():
    # when
    plain = checks.apm_query(info().config, DEFAULT_RULES)
    over = checks.apm_query(info(**{'apm-p95-overrides': 'checkout=3:500, batch=:100, search=1.5'}).config, DEFAULT_RULES)

    # then
    assert 'p95_now > 2 * p95_base AND p95_now - p95_base >= 200' in plain
    assert 'p95_now > CASE(svc == "checkout", 3.0, svc == "search", 1.5, 2.0) * p95_base' in over
    assert 'p95_now - p95_base >= CASE(svc == "checkout", 500.0, svc == "batch", 100.0, 200.0)' in over
    assert 'p95_now > 1.5 * p95_base' not in over and 'p95_now - p95_base >= 100 ' not in over


def test_an_override_name_is_written_as_a_safe_literal_in_the_query():
    # when
    query = checks.apm_query(info(**{'apm-p95-overrides': 'we"ird=3'}).config, DEFAULT_RULES)

    # then
    assert 'svc == "we\\"ird"' in query


def test_sixty_slow_services_are_each_judged_by_their_own_rule_whatever_the_overrides():
    # given
    rows = [dict(APM_ROWS[0], svc=f's{n}', p95_now=900.0, p95_base=300.0) for n in range(60)]
    overrides = checks.p95_overrides(info(**{'apm-p95-overrides': 's7=4, s9=:1000'}).config)[0]

    # when
    found = {x['svc'] for x in checks.evaluate_apm(rows, set(), DEFAULT_RULES, overrides)}

    # then
    assert len(found) == 58 and 's7' not in found and 's9' not in found and 's0' in found and 's59' in found


def test_a_service_with_an_override_is_judged_by_its_own_factor_and_floor():
    # given
    rows = [dict(APM_ROWS[0], svc='checkout', p95_now=900.0, p95_base=300.0), dict(APM_ROWS[0], svc='search', p95_now=900.0, p95_base=300.0),
            dict(APM_ROWS[0], svc='batch', p95_now=450.0, p95_base=300.0)]
    overrides = checks.p95_overrides(info(**{'apm-p95-overrides': 'checkout=4, batch=1.4:100'}).config)[0]

    # when
    plain = {x['svc'] for x in checks.evaluate_apm(rows, set(), DEFAULT_RULES)}
    own = {x['svc'] for x in checks.evaluate_apm(rows, set(), DEFAULT_RULES, overrides)}

    # then
    assert plain == {'checkout', 'search'}
    assert own == {'search', 'batch'}


def test_override_lines_that_cannot_be_read_are_named_and_the_others_still_count():
    # when
    found, bad = checks.p95_overrides(info(**{'apm-p95-overrides': 'a=3:500, b=abc, c, d=0, e=:x, f=:'}).config)

    # then
    assert found == {'a': (3.0, 500.0)}
    assert bad == ['c', 'b=abc', 'd=0', 'e=:x', 'f=:']


def test_the_test_names_override_lines_it_ignores(store):
    # given
    http = FakeHttp({('GET', ROOT): {'version': {'number': '8.15.0'}}, **searched(ES_URL, 'logs-*', 'traces-apm*'), ('POST', QUERY): esql([])})

    # when
    said = connected(store, http, **{'apm-p95-overrides': 'a=3, b=oops'})

    # then
    assert said.ok and 'apm-p95-overrides ignores b=oops' in said.summary


def test_a_message_mask_is_replaced_inside_the_first_characters_before_lines_are_grouped():
    # given
    config = info(**{'message-mask': r'body=\{.*\}'}).config

    # when
    masked = checks.logs_query(config)
    plain = checks.logs_query(info().config)

    # then
    assert 'msg = SUBSTRING(REPLACE(SUBSTRING(COALESCE(`error.message`, `message`), 1, 400), "body=\\\\{.*\\\\}", "<masked>"), 1, 90)' in masked
    assert 'REPLACE' not in plain and 'msg = SUBSTRING(COALESCE(`error.message`, `message`), 1, 90)' in plain


def test_a_mask_with_quotes_is_written_as_a_safe_string_literal():
    # when
    query = checks.logs_query(info(**{'message-mask': 'say "hi"'}).config)

    # then
    assert '"say \\"hi\\""' in query


def test_a_mask_that_cannot_be_read_or_matches_nothing_is_left_out_so_the_check_still_runs():
    # then
    for mask in ('(', 'x*', 'a' * 201):
        assert 'REPLACE' not in checks.logs_query(info(**{'message-mask': mask}).config)


def test_the_test_tries_the_mask_on_elasticsearch_and_refuses_one_it_rejects(store):
    # given
    def answer(params, body):
        if 'REPLACE' in body['query'] and 'java-only' in body['query']:
            return HttpError(400, QUERY, es_error('illegal_argument_exception', 'Dangling meta character'))
        return esql([])

    routes = {('GET', ROOT): {'version': {'number': '8.15.0'}}, **searched(ES_URL, 'logs-*', 'traces-apm*'), ('POST', QUERY): answer}

    # when
    fine = connected(store, FakeHttp(routes), **{'message-mask': 'body=.*'})
    rejected = connected(store, FakeHttp(routes), **{'message-mask': 'java-only.*'})
    switched_off = connected(store, FakeHttp(routes), off={'logs'}, **{'message-mask': 'java-only.*'})

    # then
    assert fine.ok
    assert rejected.ok is False and 'does not accept message-mask' in rejected.summary
    assert checks.mask_problem({'message-mask': '('}) == 'message-mask is not a valid pattern'
    assert 'empty text' in checks.mask_problem({'message-mask': 'x*'}) and checks.mask_problem({'message-mask': 'body=.*'}) is None and checks.mask_problem({}) is None
    assert switched_off.ok



def test_a_refused_mask_names_the_elasticsearch_error_type_only_when_it_is_a_plain_name(store):
    # given
    def answer(kind):
        def reply(params, body):
            if 'REPLACE' in body['query']:
                return HttpError(400, QUERY, es_error(kind, 'line 1:40: mismatched input <!channel> ' + kind))
            return esql([])
        return {('GET', ROOT): {'version': {'number': '8.15.0'}}, **searched(ES_URL, 'logs-*', 'traces-apm*'), ('POST', QUERY): reply}

    # when
    plain = connected(store, FakeHttp(answer('parsing_exception')), **{'message-mask': 'body=.*'})
    odd = connected(store, FakeHttp(answer('Parsing-Exception <!channel>')), **{'message-mask': 'body=.*'})

    # then
    assert plain.ok is False and 'does not accept message-mask' in plain.summary and '(parsing_exception)' in plain.summary
    assert 'mismatched' not in plain.summary
    assert odd.ok is False and 'does not accept message-mask' in odd.summary
    assert 'Parsing' not in odd.summary and 'channel' not in odd.summary and 'mismatched' not in odd.summary

def test_a_masked_text_ends_the_text_searched_for_in_kibana():
    # then
    assert checks.needle('login failed for <masked> at gateway', True) == 'login failed for'
    assert checks.needle('plain message') == 'plain message'


def test_without_a_mask_a_message_that_says_masked_is_searched_for_whole():
    # then
    assert checks.needle('token <masked> rejected') == 'token <masked> rejected'
    assert 'masked' in urllib.parse.unquote(checks.log_lines_link(info().config, 'cart', 'ERROR', 'token <masked> rejected', 'now-2h'))


def test_a_mask_that_backtracks_badly_is_refused():
    # then
    config = info(**{'message-mask': '(a|aa)+$'}).config
    assert 'backtracks' in checks.mask_problem(config)
    assert 'REPLACE' not in checks.logs_query(config)
    assert checks.mask_problem(info(**{'message-mask': r'body=\{.*\}'}).config) is None


def test_the_mask_literal_escapes_line_breaks_and_tabs():
    # then
    assert checks.literal('a\nb\rc\td') == '"a\\nb\\rc\\td"'
    assert checks.literal('say "hi"\\') == '"say \\"hi\\"\\\\"'


def test_extra_discover_columns_follow_the_usual_ones_without_repeats_and_are_capped():
    # given
    names = ', '.join(['kubernetes.pod.name', 'host.name'] + [f'f{n}' for n in range(12)])
    config = info(**{'discover-columns': names}).config

    # when
    columns = elastic.columns_of(config)
    link = urllib.parse.unquote(checks.log_lines_link(config, 'cart-service', 'ERROR', 'boom', 'now-2h'))

    # then
    assert columns[:4] == ('log.level', 'service.name', 'message', 'error.message')
    assert columns[4:6] == ('kubernetes.pod.name', 'host.name') and len(columns) == 4 + 10
    assert "'kubernetes.pod.name','host.name'" in link
    assert elastic.columns_of(info().config) == ('log.level', 'service.name', 'message', 'error.message')
    assert elastic.columns_of(info(**{'discover-columns': 'message, host.name, host.name'}).config) == ('log.level', 'service.name', 'message', 'error.message', 'host.name')


def test_a_rejected_mask_is_named_in_the_status_of_the_failed_logs_pass(store):
    # given
    def answer(params, body):
        if 'REPLACE' in body['query']:
            return HttpError(400, QUERY, es_error('illegal_argument_exception', 'Dangling meta character'))
        return esql([])

    def failed(**values):
        runtime = es_runtime(store, {'main': values}, FakeHttp({('POST', QUERY): answer}))
        return checks.run_logs(runtime.context(elasticsearch.PLUGIN, runtime.instances.get('elasticsearch', 'main'), NOW)).result

    # when
    masked = failed(**{'message-mask': 'java-only.*'})
    plain = failed()

    # then
    assert masked.ok is False and masked.status == 'error: ES|QL HTTP 400 (check message-mask: Elasticsearch may not accept it)'
    assert plain.ok is True
