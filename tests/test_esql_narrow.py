import json
from types import SimpleNamespace

import pytest

from skopax.contract import ReadError
from skopax.plugins.elasticsearch.esql import run_esql
from skopax.plugintools import TOOL_TIMEOUT_S, PluginTools
from skopax.remote import HttpError, Reply, TimedOut
from tests.fake_http import FakeHttp
from tests.test_elasticsearch_plugin import ES_URL, NOW, QUERY, es_runtime

ROWS = {'columns': [{'name': 'n'}], 'values': [[7]]}
THREE = 'FROM app-logs-* | WHERE @timestamp >= NOW() - 3 days AND level == "ERROR" | STATS n = COUNT(*)'
ONE = 'FROM app-logs-* | WHERE @timestamp >= NOW() - 1 day AND level == "ERROR" | STATS n = COUNT(*)'
BARE = 'FROM app-logs-2026.11.04 | STATS n = COUNT(*)'


def ctx(http: FakeHttp):
    return SimpleNamespace(instance=SimpleNamespace(config={'url': ES_URL, 'api-key': 'esql-test-key-0123456789'}), http=lambda: http)


def scripted(*answers):
    left = list(answers)

    def answer(params, body):
        return left.pop(0)

    return FakeHttp({('POST', QUERY): answer})


def sent(http: FakeHttp) -> list[str]:
    return [c[3]['query'] for c in http.hits(QUERY)]


def test_an_esql_504_over_several_days_retries_once_on_the_last_day_and_says_so():
    # given
    http = scripted(HttpError(504, QUERY, 'gateway timeout'), ROWS)

    # when
    out = run_esql(ctx(http), {'query': THREE})

    # then
    assert len(sent(http)) == 2
    assert 'last day' in out['note'] and 'timed out' in out['note']
    assert out['rows'] == [{'n': 7}]


def test_a_client_timeout_over_several_days_also_narrows_and_retries_once():
    # given
    http = scripted(TimedOut(f'{ES_URL}/_query gave no full answer within 75 s'), ROWS)

    # when
    out = run_esql(ctx(http), {'query': THREE})

    # then
    assert len(sent(http)) == 2
    assert 'last day' in out['note'] and out['rows'] == [{'n': 7}]


@pytest.mark.parametrize('query', [ONE.replace('1 day', '1 hour'), BARE])
def test_an_esql_504_on_a_one_hour_range_or_no_range_is_reported_with_the_narrowing_hint_and_no_retry(query: str):
    # given
    http = scripted(HttpError(504, QUERY, 'gateway timeout'))

    # when
    with pytest.raises(ReadError) as raised:
        run_esql(ctx(http), {'query': query})

    # then
    assert len(sent(http)) == 1
    assert raised.value.status == 504
    assert 'narrow the time range or the index' in raised.value.message


def test_a_retry_that_fails_again_reports_the_first_error_and_the_retry_error():
    # given
    http = scripted(HttpError(504, QUERY, 'gateway timeout'), HttpError(400, QUERY, '{"error": {"type": "verification_exception", "reason": "Unknown column [zzz]"}}'))

    # when
    with pytest.raises(ReadError) as raised:
        run_esql(ctx(http), {'query': THREE})

    # then
    assert len(sent(http)) == 2
    message = raised.value.message
    assert '\n' not in message
    assert message.index('504') < message.index('retry') < message.index('Unknown column')


def test_the_retry_keeps_the_query_except_its_time_range():
    # given
    http = scripted(HttpError(504, QUERY, 'gateway timeout'), ROWS)

    # when
    run_esql(ctx(http), {'query': THREE})

    # then
    assert sent(http) == [THREE, ONE]


def test_a_timestamp_literal_older_than_a_day_is_narrowed_and_a_query_with_an_upper_bound_is_not():
    # given
    old = 'FROM app-logs-* | WHERE @timestamp > "2020-01-01T00:00:00Z" | STATS n = COUNT(*)'
    bounded = 'FROM app-logs-* | WHERE @timestamp >= NOW() - 3 days AND @timestamp < NOW() - 1 day | STATS n = COUNT(*)'
    first = scripted(HttpError(504, QUERY, 'gateway timeout'), ROWS)
    second = scripted(HttpError(504, QUERY, 'gateway timeout'))

    # when
    run_esql(ctx(first), {'query': old})
    with pytest.raises(ReadError):
        run_esql(ctx(second), {'query': bounded})

    # then
    assert sent(first)[1] == 'FROM app-logs-* | WHERE @timestamp > NOW() - 1 day | STATS n = COUNT(*)'
    assert len(sent(second)) == 1


def test_a_slow_first_call_and_a_quick_retry_fit_the_tool_limit_and_render_like_any_answer(store):
    # given
    timeouts: list[float] = []
    rows = {'columns': [{'name': 'n'}], 'values': [[i] for i in range(250)]}

    def transport(method, url, headers, data, timeout):
        timeouts.append(timeout)
        if len(timeouts) == 1:
            raise TimedOut(f'{QUERY} gave no full answer within {timeout:g} s')
        return Reply(rows, {})

    tools = PluginTools(es_runtime(store, {'main': {}}, transport))

    # when
    text, count, cut = tools.call('esql', {'query': THREE}, NOW, rendered=True)

    # then
    assert len(timeouts) == 2 and sum(timeouts) < TOOL_TIMEOUT_S - 5, timeouts
    assert count == 250 and cut is True
    assert 'last day' in text and 'showing the first 200 of 250 rows' in text
    assert json.loads(text.split('\n', 1)[1].rsplit('\n[truncated', 1)[0]) == [{'n': i} for i in range(200)]
