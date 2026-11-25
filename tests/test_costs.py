import datetime as dt
import time

from fastapi.testclient import TestClient

from tests.conftest import AUTH
from skopax import costs
from skopax import clock
from skopax.store import Store

TODAY = '2026-10-24'
K1 = 'log|checkout-service|ERROR|boom'
K2 = 'cf-status|cloudflare/CDN/Cache'


def row(source: str, usd: dict, toks: dict, day: str = TODAY, at: str = '2026-10-24T06:00:00Z', run: str | None = None, by: str = 'pod', keys: tuple = (),
        model: str = 'claude-opus-5-5', effort: str = 'medium', **extra) -> dict:
    parts = dict.fromkeys(('cacheRead', 'input', 'cacheWrite5m', 'cacheWrite1h', 'output'), 0.0) | usd
    return {'source': source, 'day': day, 'at': at, 'model': model, 'effort': effort, 'runId': run, 'actionId': None, 'keys': list(keys), 'by': by,
            'tokens': dict.fromkeys(parts, 0) | toks, 'usd': parts | {'total': sum(parts.values())}, **extra}


def rows() -> list[dict]:
    return [
        row('pod', {'cacheRead': 0.1, 'input': 0.2, 'cacheWrite5m': 0.3, 'output': 0.4}, {'cacheRead': 500_000, 'input': 50_000, 'cacheWrite5m': 60_000, 'output': 20_000}, run='r1', keys=(K1,)),
        row('pod', {'cacheRead': 0.5}, {'cacheRead': 2_500_000}, at='2026-10-24T06:01:00Z', run='r1', keys=(K1,)),
        row('slack', {'cacheWrite1h': 0.2, 'input': 0.05}, {'cacheWrite1h': 50_000, 'input': 25_000}, day='2026-10-23', at='2026-10-23T10:00:00Z', run='r2',
            by='adam.baker@example.com', keys=(K1,), model='claude-sonnet-5-5', effort='low'),
        row('gate', {'input': 0.0002, 'output': 0.0001}, {'input': 2000, 'output': 200}, day='2026-10-23', at='2026-10-23T09:59:58Z', by='adam.baker@example.com', keys=(K1,),
            model='claude-haiku-5-5', effort='low', gateFor='slack'),
        row('page', {'input': 0.4}, {'input': 100_000}, day='2026-10-20', at='2026-10-20T08:00:00Z', run='r3', by='oscar.green', keys=(K1, K2)),
        row('assess', {'output': 0.6}, {'output': 30_000}, at='2026-10-24T05:00:00Z', run='r4', by='server'),
    ]


META = {
    'r1': {'_id': 'r1', 'trigger': 'cards', 'outcome': 'done', 'startedAt': '2026-10-24T05:59:58Z', 'endedAt': '2026-10-24T06:02:00Z', 'keys': [K1], 'toolCalls': [{'name': 'esql'}, {'name': 'card'}]},
    'r3': {'_id': 'r3', 'trigger': 'ask', 'outcome': 'done', 'startedAt': '2026-10-20T08:00:00Z', 'endedAt': '2026-10-20T08:00:40Z', 'keys': [K1, K2], 'ask': {'text': 'Why did it go up?'}, 'toolCalls': []},
    'r4': {'_id': 'r4', 'trigger': 'assess', 'outcome': 'failed', 'startedAt': '2026-10-24T05:00:00Z', 'endedAt': '2026-10-24T05:03:00Z', 'keys': [], 'report': {'summary': '2 deploys, 1 flagged'}, 'toolCalls': [{'name': 'promql'}]},
}


def test_the_window_is_whole_local_days_ending_today_and_one_day_is_cut_into_hours():
    # when
    week = costs.window(7, TODAY)
    day = costs.window(1, TODAY)

    # then
    assert week[:2] == ('2026-10-18', 'day') and week[2] == ['2026-10-18', '2026-10-19', '2026-10-20', '2026-10-21', '2026-10-22', '2026-10-23', '2026-10-24']
    assert day[:2] == (TODAY, 'hour') and day[2][0] == '2026-10-24T00' and day[2][-1] == '2026-10-24T23' and len(day[2]) == 24


def test_totals_today_and_the_averages():
    # when
    s = costs.summary(rows(), META, TODAY, 7, 3.41, 20.0)

    # then
    assert (s['days'], s['from'], s['to'], s['bucket']) == (7, '2026-10-18', TODAY, 'day')
    assert s['today'] == {'day': TODAY, 'spent': 3.41, 'cap': 20.0}
    assert (s['total']['usd'], s['total']['calls'], s['total']['runs']) == (2.7503, 6, 4)
    assert s['total']['cacheHit'] == round(3_000_000 / (3_000_000 + 177_000 + 60_000 + 50_000), 3)
    assert s['avg'] == {'slackAsk': 0.25, 'podRun': 1.5}


def test_spend_per_day_has_every_day_of_the_window_split_by_source():
    # when
    per = costs.summary(rows(), META, TODAY, 7, 0, 20.0)['perBucket']

    # then
    assert [b['t'] for b in per] == ['2026-10-18', '2026-10-19', '2026-10-20', '2026-10-21', '2026-10-22', '2026-10-23', '2026-10-24']
    assert per[-1] == {'t': TODAY, 'pod': 1.5, 'page': 0.0, 'slack': 0.0, 'chat': 0.0, 'assess': 0.6, 'gate': 0.0, 'smalltalk': 0.0, 'pagescan': 0.0, 'selfreview': 0.0, 'threadsummary': 0.0, 'mcp': 0.0, 'total': 2.1}
    assert (per[-2]['slack'], per[-2]['gate'], per[-2]['total']) == (0.25, 0.0003, 0.2503)
    assert per[2]['page'] == 0.4 and per[0]['total'] == 0.0


def test_one_day_is_bucketed_by_local_hour():
    # when
    s = costs.summary([r for r in rows() if r['day'] == TODAY], META, TODAY, 1, 0, 20.0)
    hours = {b['t']: b for b in s['perBucket']}

    # then
    assert s['bucket'] == 'hour' and len(s['perBucket']) == 24
    assert (hours['2026-10-24T06']['pod'], hours['2026-10-24T05']['assess'], hours['2026-10-24T07']['total']) == (1.5, 0.6, 0.0)


def test_every_source_has_its_calls_runs_split_hit_share_average_and_model_mix():
    # when
    by = {x['source']: x for x in costs.summary(rows(), META, TODAY, 7, 0, 20.0)['sources']}

    # then
    assert list(by) == ['pod', 'page', 'slack', 'chat', 'assess', 'gate', 'smalltalk', 'pagescan', 'selfreview', 'threadsummary', 'mcp']
    assert (by['pod']['calls'], by['pod']['runs'], by['pod']['avgRun'], by['pod']['models']) == (2, 1, 1.5, {'claude-opus-5-5': 2})
    assert by['pod']['usd'] == {'cacheRead': 0.6, 'input': 0.2, 'cacheWrite5m': 0.3, 'cacheWrite1h': 0.0, 'output': 0.4, 'webSearch': 0.0, 'total': 1.5}
    assert by['pod']['cacheHit'] == round(3_000_000 / 3_110_000, 3)
    assert (by['slack']['usd']['cacheWrite1h'], by['slack']['cacheHit'], by['slack']['models']) == (0.2, 0.0, {'claude-sonnet-5-5': 1})
    assert (by['gate']['calls'], by['gate']['runs'], by['gate']['avgRun']) == (1, 0, None)
    assert by['assess']['cacheHit'] is None


def test_a_small_talk_reply_is_its_own_source_without_runs():
    # given
    talk = row('smalltalk', {'input': 0.0001, 'output': 0.00002}, {'input': 1000, 'output': 40}, by='adam.baker@example.com', model='claude-haiku-5-5', effort='low', talkFor='slack')

    # when
    s = costs.summary(rows() + [talk], META, TODAY, 7, 0, 20.0)
    by = {x['source']: x for x in s['sources']}

    # then
    assert (by['smalltalk']['calls'], by['smalltalk']['runs'], by['smalltalk']['avgRun'], by['smalltalk']['usd']['total']) == (1, 0, None, 0.0001)
    assert s['perBucket'][-1]['smalltalk'] == 0.0001 and s['total']['runs'] == 4


def test_models_cache_per_day_top_cards_and_top_askers():
    # when
    s = costs.summary(rows(), META, TODAY, 7, 0, 20.0)
    cache = {c['t']: c for c in s['cache']}

    # then
    assert s['models'] == [{'model': 'claude-opus-5-5', 'calls': 4, 'usd': 2.5}, {'model': 'claude-sonnet-5-5', 'calls': 1, 'usd': 0.25}, {'model': 'claude-haiku-5-5', 'calls': 1, 'usd': 0.0003}]
    assert (cache[TODAY]['hit']['pod'], cache[TODAY]['hit']['assess'], cache[TODAY]['writeUsd']['pod']) == (round(3_000_000 / 3_110_000, 3), None, 0.3)
    assert (cache['2026-10-23']['writeUsd']['slack'], cache['2026-10-23']['hit']['slack'], cache['2026-10-18']['hit']['pod']) == (0.2, 0.0, None)
    assert s['topCards'] == [{'key': K1, 'runs': 3, 'usd': 1.9503}, {'key': K2, 'runs': 1, 'usd': 0.2}]
    assert s['topAskers'] == [{'by': 'oscar.green', 'runs': 1, 'calls': 1, 'usd': 0.4}, {'by': 'adam.baker@example.com', 'runs': 1, 'calls': 2, 'usd': 0.2503}]


def test_the_run_list_is_newest_first_and_joins_the_run_records():
    # when
    listed = costs.summary(rows(), META, TODAY, 7, 0, 20.0)['runs']
    r1, r4, r2, r3 = listed

    # then
    assert [r['id'] for r in listed] == ['r1', 'r4', 'r2', 'r3']
    assert (r1['at'], r1['source'], r1['by'], r1['trigger'], r1['outcome'], r1['what'], r1['model'], r1['effort']) == ('2026-10-24T05:59:58Z', 'pod', 'pod', 'cards', 'done', K1, 'claude-opus-5-5', 'medium')
    assert (r1['calls'], r1['toolCalls'], r1['seconds'], r1['usd']['total'], r1['usd']['cacheRead']) == (2, 2, 122, 1.5, 0.6)
    assert (r3['what'], r3['keys'], r4['what'], r4['outcome'], r4['seconds']) == ('Why did it go up?', [K1, K2], '2 deploys, 1 flagged', 'failed', 180)
    assert (r2['at'], r2['what'], r2['trigger'], r2['toolCalls'], r2['seconds'], r2['model']) == ('2026-10-23T10:00:00Z', K1, None, None, None, 'claude-sonnet-5-5')


def test_an_empty_ledger_gives_zeros_and_no_average():
    # when
    s = costs.summary([], {}, TODAY, 30, 0.0, 20.0)

    # then
    assert (s['total'], s['avg'], len(s['perBucket']), s['runs'], s['topCards'], s['topAskers']) == ({'usd': 0.0, 'calls': 0, 'runs': 0, 'cacheHit': None}, {'slackAsk': None, 'podRun': None}, 30, [], [], [])


def test_the_costs_endpoint_is_for_operators_and_reads_the_window(client: TestClient, store: Store):
    # given
    today = clock.day(time.time())
    old = (dt.date.fromisoformat(today) - dt.timedelta(days=8)).isoformat()
    store.add_ledger(row('pod', {'input': 0.5}, {'input': 125_000}, day=today, run='r1', keys=(K1,)))
    store.add_ledger(row('pod', {'input': 9.0}, {'input': 1}, day=old, run='r0'))
    store.add_spend(today, 3.4)

    # when
    resp = client.get('/api/costs', params={'days': 7}, headers=AUTH)
    guest = client.get('/api/costs')
    too_long = client.get('/api/costs', params={'days': 31}, headers=AUTH)

    # then
    assert (resp.status_code, guest.status_code, too_long.status_code) == (200, 401, 422)
    body = resp.json()
    assert body['today'] == {'day': today, 'spent': 3.4, 'cap': 25.0}
    assert (body['total']['usd'], body['total']['calls'], len(body['perBucket']), [r['id'] for r in body['runs']]) == (0.5, 1, 7, ['r1'])
