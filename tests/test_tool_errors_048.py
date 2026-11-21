import datetime as dt
import json
from types import SimpleNamespace

import pytest

from skopax import history
from skopax.contract import Capabilities, Field, ReadError, Tool
from skopax.kit import elastic
from skopax.plugins import prometheus
from skopax.plugins.elasticsearch.esql import run_esql
from skopax.plugins.pmm import client as pmm_client
from skopax.remote import HttpError
from skopax.investigator.tools import ToolError
from skopax.store import Store
from tests.conftest import anomaly, state
from tests.fake_http import FakeHttp
from tests.plugins.base import minimal
from tests.test_crashlytics_plugin import ANDROID, KEY as GOOGLE_KEY, call as crash_call, crash_box, google
from tests.test_elasticsearch_plugin import ES_URL, QUERY
from tests.test_incident_group import OTHER, PARENT, SOAP, TRACKHUB, seeded
from tests.test_investigator_tools import box
from tests.test_pmm_plugin import pmm_routes, qan_path, tool_box
from tests.test_plugin_tools import SCHEMA, Rig, where

NODES = minimal('nodes', 'Nodes', capabilities=Capabilities(), fields=(Field('url', 'URL', 'url', required=True),),
                tools=(Tool('nodes_read', 'Read the nodes of a cluster', SCHEMA, where, instance_input='cluster'),))
WORKERS = minimal('workers', 'Workers', capabilities=Capabilities(), fields=(Field('url', 'URL', 'url', required=True),),
                  tools=(Tool('workers_restart', 'Restart a worker', SCHEMA, where, kind='write', instance_input='cluster'),))
TODAY = dt.datetime(2026, 11, 21, 10, 0, tzinfo=dt.timezone.utc).timestamp()
ROWS = {'columns': [{'name': 'n'}], 'values': [[7]]}


def es_ctx(http: FakeHttp, now: float = TODAY):
    return SimpleNamespace(instance=SimpleNamespace(config={'url': ES_URL, 'api-key': 'esql-test-key-0123456789'}), http=lambda: http, now=now)


RESOLVE = f'{ES_URL}/_resolve/index/*'
DAILY = {'indices': [{'name': f'app-logs-2026.11.{d:02d}'} for d in range(17, 21)], 'aliases': [], 'data_streams': []}


def es_scripted(*answers):
    left = list(answers)
    return FakeHttp({('POST', QUERY): lambda params, body: left.pop(0), ('GET', RESOLVE): DAILY})


def not_found(index: str) -> HttpError:
    return HttpError(404, QUERY, '{"error": {"type": "index_not_found_exception", "reason": "no such index [%s]"}}' % index)


def es_sent(http: FakeHttp) -> list[str]:
    return [c[3]['query'] for c in http.hits(QUERY)]


@pytest.fixture(autouse=True)
def fresh_caches():
    elastic.PATTERNS.clear()
    getattr(pmm_client, 'COLUMNS', {}).clear()
    yield
    elastic.PATTERNS.clear()
    getattr(pmm_client, 'COLUMNS', {}).clear()


def clusters(store: Store) -> Rig:
    rig = Rig(store, prometheus.PLUGIN, NODES, WORKERS)
    rig.add(prometheus.PLUGIN, 'prod', primary=True, url='https://prom.example.com')
    rig.add(prometheus.PLUGIN, 'data-eu', url='https://prom-eu.example.com')
    rig.add(NODES, 'edge', primary=True)
    rig.add(NODES, 'data')
    rig.add(NODES, 'eu-west')
    rig.add(WORKERS, 'jobs', primary=True)
    return rig


def test_a_promql_cluster_that_names_another_tool_s_instance_is_refused_with_the_promql_names_and_a_close_match(store: Store):
    # given
    rig = clusters(store)
    b = rig.box()

    # when
    related, failed = b.call('promql', {'expr': 'up', 'cluster': 'data'})
    unrelated, unrelated_failed = b.call('promql', {'expr': 'up', 'cluster': 'edge'})
    segment, _ = b.call('promql', {'expr': 'up', 'cluster': 'eu-west'})
    schema = next(d for d in b.definitions() if d['name'] == 'promql')['input_schema']['properties']['cluster']

    # then
    assert failed and related.startswith('cluster must be one of prod, data-eu'), related
    assert '"data" is an instance of nodes_read, not of promql' in related and 'did you mean data-eu' in related, related
    assert 'is not configured' not in related, related
    assert unrelated_failed and '"edge" is an instance of nodes_read' in unrelated and 'did you mean' not in unrelated, unrelated
    assert 'did you mean' not in segment and 'do not use another cluster in its place' in segment, segment
    assert 'prod, data-eu' in schema['description'] and 'nodes_read' in schema['description'], schema


def test_the_other_tool_notes_name_only_tools_offered_in_the_run_kind(store: Store):
    # given
    rig = clusters(store)
    live, reading = rig.box(), rig.box(read_only=True)

    # when
    live_note = next(d for d in live.definitions() if d['name'] == 'promql')['input_schema']['properties']['cluster']['description']
    read_note = next(d for d in reading.definitions() if d['name'] == 'promql')['input_schema']['properties']['cluster']['description']
    live_refusal, _ = live.call('promql', {'expr': 'up', 'cluster': 'jobs'})
    read_refusal, _ = reading.call('promql', {'expr': 'up', 'cluster': 'jobs'})

    # then
    assert 'workers_restart' in live_note and 'workers_restart' not in read_note and 'nodes_read' in read_note, (live_note, read_note)
    assert 'is an instance of workers_restart' in live_refusal, live_refusal
    assert 'workers_restart' not in read_refusal and 'is not configured' in read_refusal, read_refusal


def test_the_direct_call_path_names_only_the_tools_it_is_given(store: Store):
    # given
    rig = clusters(store)

    # when
    with pytest.raises(ToolError) as offered:
        rig.tools.call('promql', {'expr': 'up', 'cluster': 'jobs'}, TODAY, among={'promql', 'nodes_read', 'workers_restart'})
    with pytest.raises(ToolError) as reading:
        rig.tools.call('promql', {'expr': 'up', 'cluster': 'jobs'}, TODAY, among={'promql', 'nodes_read'})

    # then
    assert 'is an instance of workers_restart' in str(offered.value), str(offered.value)
    assert 'workers_restart' not in str(reading.value) and 'is not configured' in str(reading.value), str(reading.value)


def test_an_esql_unknown_column_lists_the_closest_columns_of_the_index():
    # given
    unknown = HttpError(400, QUERY, '{"error": {"type": "verification_exception", "reason": "Found 1 problem\\nline 1:40: Unknown column [servce.name]"}}')
    columns = {'columns': [{'name': n} for n in ('@timestamp', 'service.name', 'service.version', 'service.node.name', 'service.environment', 'service.id', 'message', 'host.name')],
               'values': []}
    http = es_scripted(unknown, columns)

    # when
    with pytest.raises(ReadError) as raised:
        run_esql(es_ctx(http), {'query': 'FROM app-logs-* | WHERE @timestamp >= NOW() - 1 hour | STATS n = COUNT(*) BY servce.name'})

    # then
    message = raised.value.message
    assert 'closest columns of app-logs-*: service.name' in message, message
    named = message.split('closest columns of app-logs-*: ')[1].split(' - ')[0].split(', ')
    assert 1 <= len(named) <= 5 and 'message' not in named and '@timestamp' not in named, named
    assert es_sent(http)[1] == 'FROM app-logs-* | LIMIT 0'


def test_an_esql_unknown_column_with_nothing_close_lists_no_columns():
    # given
    unknown = HttpError(400, QUERY, '{"error": {"type": "verification_exception", "reason": "Found 1 problem\\nline 1:40: Unknown column [zzqx]"}}')
    http = es_scripted(unknown, {'columns': [{'name': n} for n in ('@timestamp', 'service.name', 'message')], 'values': []})

    # when
    with pytest.raises(ReadError) as raised:
        run_esql(es_ctx(http), {'query': 'FROM app-logs-* | WHERE @timestamp >= NOW() - 1 hour | KEEP zzqx'})

    # then
    assert 'closest columns' not in raised.value.message, raised.value.message


def test_today_s_daily_index_that_is_not_created_yet_is_read_once_for_yesterday_and_the_answer_says_so():
    # given
    http = es_scripted(not_found('app-logs-2026.11.21'), ROWS)

    # when
    out = run_esql(es_ctx(http), {'query': 'FROM app-logs-2026.11.21 | STATS n = COUNT(*)'})

    # then
    assert es_sent(http) == ['FROM app-logs-2026.11.21 | STATS n = COUNT(*)', 'FROM app-logs-2026.11.20 | STATS n = COUNT(*)']
    assert out['rows'] == [{'n': 7}] and 'app-logs-2026.11.21 does not exist yet' in out['note'] and 'app-logs-2026.11.20' in out['note'], out


def test_a_failed_retry_on_yesterday_keeps_the_closest_index_hint():
    # given
    http = es_scripted(not_found('app-logs-2026.11.21'), not_found('app-logs-2026.11.20'))

    # when
    with pytest.raises(ReadError) as raised:
        run_esql(es_ctx(http), {'query': 'FROM app-logs-2026.11.21 | STATS n = COUNT(*)'})

    # then
    assert len(es_sent(http)) == 2
    assert 'retry on the day before failed too' in raised.value.message and 'closest existing patterns: app-logs-*' in raised.value.message, raised.value.message


def test_a_typo_in_an_index_with_today_s_date_gets_the_hint_without_a_retry():
    # given
    http = es_scripted(not_found('app-lgos-2026.11.21'), ROWS)

    # when
    with pytest.raises(ReadError) as raised:
        run_esql(es_ctx(http), {'query': 'FROM app-lgos-2026.11.21 | STATS n = COUNT(*)'})

    # then
    assert len(es_sent(http)) == 1
    assert 'closest existing patterns: app-logs-*' in raised.value.message, raised.value.message


def test_a_missing_other_index_next_to_today_s_does_not_retry_on_yesterday():
    # given
    http = es_scripted(not_found('app-lgos-*'), ROWS)

    # when
    with pytest.raises(ReadError):
        run_esql(es_ctx(http), {'query': 'FROM app-logs-2026.11.21, app-lgos-* | WHERE @timestamp >= NOW() - 1 hour | STATS n = COUNT(*)'})

    # then
    assert len(es_sent(http)) == 1


def test_an_older_missing_daily_index_is_not_retried():
    # given
    http = es_scripted(not_found('app-logs-2026.11.04'), ROWS)

    # when
    with pytest.raises(ReadError):
        run_esql(es_ctx(http), {'query': 'FROM app-logs-2026.11.04 | STATS n = COUNT(*)'})

    # then
    assert len(es_sent(http)) == 1


def test_a_group_parent_outside_the_run_or_unknown_is_refused_with_the_parents_this_run_can_use(store: Store):
    # given
    b = box(seeded(store), keys=(TRACKHUB, SOAP))

    # when
    outside, outside_failed = b.call('group', {'parent': OTHER, 'children': [TRACKHUB]})
    unknown, unknown_failed = b.call('group', {'parent': 'log|nowhere-service|ERROR|gone', 'children': [TRACKHUB]})
    child, child_failed = b.call('group', {'parent': TRACKHUB, 'children': ['log|nowhere-service|ERROR|gone']})

    # then
    assert outside_failed and unknown_failed and child_failed
    for out in (outside, unknown):
        assert 'parents this run can use' in out and TRACKHUB in out.split('parents this run can use')[1] and SOAP in out.split('parents this run can use')[1], out
    assert 'no anomaly with key' not in child and 'no card `log|nowhere-service|ERROR|gone`' in child, child
    assert PARENT not in outside.split('parents this run can use')[1]


def test_the_card_tool_answers_a_closed_or_merged_card_by_key_or_number(store: Store):
    # given
    live, gone = 'out-err|shop/checkout-service', 'log|checkout-service|ERROR|payment timeout'
    store.save_state(state(anomaly(live, num=12)), None)
    store.card_ids.insert_many([{'_id': live, 'n': 12}, {'_id': gone, 'n': 391}, {'_id': 'log|cart-service|ERROR|left', 'n': 392}])
    store.card_history.insert_one({'_id': history.doc_id('log|cart-service|ERROR|left', '2026-11-19T08:00:00Z'), 'key': 'log|cart-service|ERROR|left', 'num': 392,
                                   'firstSeen': '2026-11-19T08:00:00Z', 'lastSeen': '2026-11-19T09:00:00Z', 'outcome': 'muted'})
    store.card_history.insert_one({'_id': history.doc_id(gone, '2026-11-20T08:00:00Z'), 'key': gone, 'num': 391, 'firstSeen': '2026-11-20T08:00:00Z',
                                   'lastSeen': '2026-11-20T09:00:00Z', 'clearedAt': '2026-11-20T09:30:00Z', 'outcome': 'handled', 'parent': live})
    b = box(store, keys=(live,))

    # when
    by_number, number_failed = b.call('card', {'key': '#391'})
    by_key, key_failed = b.call('card', {'key': gone})
    alive, alive_failed = b.call('card', {'key': '#12'})
    nothing, nothing_failed = b.call('card', {'key': '#999'})

    # then
    assert not number_failed and not key_failed and not alive_failed, (by_number, by_key, alive)
    closed = json.loads(by_number)
    assert closed['key'] == gone and closed['num'] == 391 and closed['closed'] is True and closed['closedAt'] == '2026-11-20T09:30:00Z', closed
    assert 'closedAt' not in json.loads(b.call('card', {'key': '#392'})[0]), 'no clearedAt, no closedAt'
    assert closed['outcome'] == 'handled' and closed['mergedInto'] == {'key': live, 'num': 12}, closed
    assert json.loads(by_key) == closed
    assert json.loads(alive)['key'] == live
    assert nothing_failed and 'no card #999' in nothing and 'no anomaly with key' not in nothing, nothing


def test_slack_update_is_offered_only_for_a_run_with_a_main_channel_post_and_refused_clearly_otherwise(store: Store):
    # given
    unposted, posted = 'log|checkout-service|ERROR|x', 'log|cart-service|ERROR|y'
    post = {'ts': '1.2', 'at': '2026-10-10T08:00:00Z', 'channel': 'C1', 'by': 'auto'}
    store.save_state(state(anomaly(unposted), anomaly(posted, svc='cart-service', slackPost=post), anomaly('log|checkout-service|ERROR|old', status='cleared', slackPost=post)), None)

    # when
    quiet = box(store, keys=(unposted,))
    loud = box(store, keys=(posted,))
    refused, failed = quiet.call('slack_update', {'key': unposted, 'text': 'Update: fixed.'})

    # then
    assert 'slack_update' not in quiet.offered() and 'slack_update' not in [d['name'] for d in quiet.definitions()]
    assert 'slack_update' in loud.offered()
    assert 'slack_update' in box(store, keys=('log|checkout-service|ERROR|old',)).offered(), 'a cleared, posted run card still takes thread replies'
    assert failed and 'has no main-channel message to reply to yet' in refused, refused


def test_crashes_refuses_an_app_that_is_not_set_up_with_the_apps_it_takes(store: Store):
    # given
    http = google()
    b = crash_box(store, http, instances={'main': {'credentials': GOOGLE_KEY, 'android-app-id': ANDROID}})

    # when
    out, failed = crash_call(b, app='ios')

    # then
    assert failed and out.startswith('app must be one of android'), out
    assert http.calls == []


def columns_answer(error_for: dict[str, str]):
    seen = []

    def answer(params, body):
        raw = body['queries'][0]['rawSql']
        seen.append(raw)
        if 'system.columns' in raw:
            return {'results': {'A': {'frames': [{'schema': {'fields': [{'name': 'name'}]}, 'data': {'values': [['queryid', 'num_queries', 'm_rows_sum', 'm_query_time_sum', 'period_start']]}}]}}}
        for needle, error in error_for.items():
            if needle in raw:
                return {'results': {'A': {'error': error}}}
        return {'results': {'A': {'frames': []}}}

    return answer, seen


@pytest.mark.parametrize('sql, error', [
    ('SELECT queryid, sum(m.m_rows_sent_sum) AS rows FROM pmm.metrics AS m GROUP BY queryid',
     "Code: 47. DB::Exception: Missing columns: 'm.m_rows_sent_sum' while processing query"),
    ('SELECT queryid, sum(m_rows_sent_sum) AS sent FROM pmm.metrics GROUP BY queryid',
     'Code: 47. DB::Exception: Unknown expression or function identifier `m_rows_sent_sum` in scope SELECT queryid'),
], ids=['table-alias', 'column-alias'])
def test_a_qan_missing_column_keeps_the_error_and_names_the_column_with_close_ones_and_no_retry(store: Store, sql, error):
    # given
    answer, seen = columns_answer({'m_rows_sent_sum': error})
    http = FakeHttp(pmm_routes(qan=answer))
    b = tool_box(store, http)

    # when
    out, failed = b.call('qan', {'sql': sql, 'hours': 1})
    again, _ = b.call('qan', {'sql': sql, 'hours': 1})

    # then
    assert failed and out.startswith('PMM QAN query answered an error'), out
    assert 'pmm.metrics has no column m_rows_sent_sum' in out and 'm.m_rows_sent_sum;' not in out and 'close columns: m_rows_sum' in out, out
    assert again == out
    assert [s for s in seen if 'system.columns' not in s] == [sql, sql], 'no rewrite and no retry'
    assert sum('system.columns' in s for s in seen) == 1, 'the column list is read once and cached'
