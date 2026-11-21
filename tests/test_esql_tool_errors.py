import pytest

from skopax.contract import ReadError
from skopax.kit import elastic
from skopax.plugins.elasticsearch.esql import run_esql
from skopax.remote import HttpError
from tests.fake_http import FakeHttp
from tests.test_elasticsearch_plugin import ES_URL, QUERY
from tests.test_esql_narrow import ROWS, THREE, ctx, sent

RESOLVE = f'{ES_URL}/_resolve/index/*'
MISSING = HttpError(400, QUERY, '{"error": {"type": "verification_exception", "reason": "Found 1 problem\\nline 1:1: Unknown index [k8s-shop-prod-logs-*]"}}')
INDICES = {'indices': [{'name': f'k8s-checkout-prod-logs-2026.11.{d:02d}'} for d in range(1, 6)] + [{'name': 'k8s-shop-prod-app-logs-2026.11.01'}, {'name': 'metrics-system.cpu-default'}, {'name': 'zzz-unrelated'}],
           'aliases': [], 'data_streams': [{'name': 'k8s-cart-prod-logs-default'}]}


@pytest.fixture(autouse=True)
def fresh_cache():
    elastic.PATTERNS.clear()
    yield
    elastic.PATTERNS.clear()


def scripted(*answers, resolve=INDICES):
    left = list(answers)
    return FakeHttp({('POST', QUERY): lambda params, body: left.pop(0), ('GET', RESOLVE): resolve})


def test_an_index_pattern_that_matches_no_index_answers_with_the_closest_existing_patterns():
    # given
    http = scripted(MISSING)

    # when
    with pytest.raises(ReadError) as raised:
        run_esql(ctx(http), {'query': 'FROM k8s-shop-prod-logs-* | WHERE @timestamp >= NOW() - 1 hour | LIMIT 5'})

    # then
    message = raised.value.message
    assert 'closest existing' in message, message
    assert 'k8s-checkout-prod-logs-*' in message and 'k8s-cart-prod-logs-default' in message, message
    assert 'zzz-unrelated' not in message
    assert len(http.hits(QUERY)) == 1


def test_the_closest_patterns_are_capped_at_ten_and_the_index_list_is_read_once_per_cache_window():
    # given
    many = {'indices': [{'name': f'k8s-shop-prod-logs-{c}-x'} for c in 'abcdefghijklmnop'], 'aliases': [], 'data_streams': []}
    http = scripted(MISSING, MISSING, resolve=many)

    # when
    texts = []
    for _ in range(2):
        with pytest.raises(ReadError) as raised:
            run_esql(ctx(http), {'query': 'FROM k8s-shop-prod-logs-* | WHERE @timestamp >= NOW() - 1 hour | LIMIT 5'})
        texts.append(raised.value.message)

    # then
    assert len([x for x in texts[0].split('closest existing')[1].split(',') if x.strip()]) == 10
    assert len(http.hits(RESOLVE)) == 1


def test_a_failing_index_list_read_leaves_the_plain_error():
    # given
    http = scripted(MISSING, resolve=HttpError(500, RESOLVE, 'boom'))

    # when
    with pytest.raises(ReadError) as raised:
        run_esql(ctx(http), {'query': 'FROM k8s-shop-prod-logs-* | WHERE @timestamp >= NOW() - 1 hour | LIMIT 5'})

    # then
    assert 'Unknown index' in raised.value.message
    assert 'closest existing' not in raised.value.message


@pytest.mark.parametrize('failure', [HttpError(400, QUERY, '{"error": {"type": "search_phase_execution_exception", "reason": "all shards failed"}}'),
                                     HttpError(503, QUERY, '{"error": {"type": "search_phase_execution_exception", "reason": "all shards failed"}}'),
                                     HttpError(400, QUERY, '{"error": {"type": "too_many_buckets_exception", "reason": "Trying to create too many buckets"}}')])
def test_a_time_range_the_index_cannot_serve_retries_once_on_the_last_day_and_says_it_was_narrowed(failure):
    # given
    http = scripted(failure, ROWS)

    # when
    out = run_esql(ctx(http), {'query': THREE})

    # then
    assert len(sent(http)) == 2
    assert 'NOW() - 1 day' in sent(http)[1]
    assert 'last day' in out['note'] and 'full range failed' in out['note'] and out['rows'] == [{'n': 7}]


@pytest.mark.parametrize('failure', [HttpError(400, QUERY, '{"error": {"type": "parsing_exception", "reason": "line 1:40: mismatched input out of range near retention"}}'),
                                     HttpError(400, QUERY, '{"error": {"type": "verification_exception", "reason": "Found 1 problem\\nline 1:60: Unknown column [retention_days]"}}'),
                                     HttpError(400, QUERY, '{"error": {"type": "illegal_argument_exception", "reason": "time range is outside the retention of the index"}}')])
def test_a_parse_or_unknown_column_error_is_never_read_as_a_range_error(failure):
    # given
    http = scripted(failure, {'columns': [], 'values': []})

    # when
    with pytest.raises(ReadError):
        run_esql(ctx(http), {'query': THREE})

    # then
    assert sent(http).count(THREE) == 1 and all('NOW() - 1 day' not in q for q in sent(http))


def test_a_range_error_on_a_one_day_range_is_not_retried():
    # given
    http = scripted(HttpError(400, QUERY, '{"error": {"type": "search_phase_execution_exception", "reason": "all shards failed"}}'))

    # when
    with pytest.raises(ReadError):
        run_esql(ctx(http), {'query': 'FROM app-logs-* | WHERE @timestamp >= NOW() - 1 day | STATS n = COUNT(*)'})

    # then
    assert len(sent(http)) == 1


def test_a_failed_index_list_read_is_not_repeated_within_a_minute():
    # given
    http = scripted(MISSING, MISSING, resolve=HttpError(500, RESOLVE, 'boom'))

    # when
    for _ in range(2):
        with pytest.raises(ReadError):
            run_esql(ctx(http), {'query': 'FROM k8s-shop-prod-logs-* | WHERE @timestamp >= NOW() - 1 hour | LIMIT 5'})

    # then
    assert len(http.hits(RESOLVE)) == 1
