import datetime as dt

from fastapi.testclient import TestClient

from skopax.investigator.pricing import known, usd
from skopax.store import Store, iso, utc_now

K1 = 'log|a|ERROR|x'
K2 = 'cf-status|cloudflare/CDN/Cache'


def run(store: Store, rid: str, key: str, cost: float, calls: list[dict], usage: dict, model: str = 'claude-opus-5-5', days_ago: float = 0) -> None:
    started = iso(utc_now() - dt.timedelta(days=days_ago))
    store.add_investigation({'id': rid, 'mode': 'shadow', 'trigger': 'cards', 'actionId': None, 'keys': [key], 'startedAt': started, 'model': model, 'effort': 'medium', 'outcome': 'running'})
    store.update_investigation(rid, {'outcome': 'done', 'endedAt': started, 'turns': len(calls) + 1, 'models': [model] * (len(calls) + 1), 'usage': usage, 'usd': cost,
                                     'toolCalls': calls, 'writes': [], 'flags': [], 'recap': 'r', 'error': None})


def seed(store: Store) -> None:
    run(store, 'r1', K1, 0.4, [{'name': 'esql', 'input': '{}', 'ms': 5000, 'rows': 40, 'truncated': False, 'error': None},
                             {'name': 'esql', 'input': '{}', 'ms': 59000, 'rows': 200, 'truncated': True, 'error': None},
                             {'name': 'card', 'input': '{}', 'ms': 1, 'rows': None, 'truncated': False, 'error': None}],
        {'input_tokens': 1000, 'output_tokens': 200, 'cache_read_input_tokens': 50000, 'cache_creation_input_tokens': 29000})
    run(store, 'r2', K2, 0.6, [{'name': 'gitlab_search', 'input': '{}', 'ms': 3000, 'rows': None, 'truncated': False, 'error': 'HTTP 500'}],
        {'input_tokens': 500, 'output_tokens': 100, 'cache_read_input_tokens': 20000, 'cache_creation_input_tokens': 0}, model='claude-sonnet-5-5')
    run(store, 'r0', K1, 9.0, [{'name': 'esql', 'input': '{}', 'ms': 1, 'rows': 1, 'truncated': False, 'error': None}],
        {'input_tokens': 1, 'output_tokens': 1, 'cache_read_input_tokens': 0, 'cache_creation_input_tokens': 0}, days_ago=10)


def test_usage_aggregates_the_runs_since_the_cut(store: Store):
    # given
    seed(store)

    # when
    u = store.investigation_usage(iso(utc_now() - dt.timedelta(days=7)))

    # then
    assert (u['runs'], u['usd'], u['turns']) == (2, 1.0, 6)
    assert u['usage'] == {'input_tokens': 1500, 'output_tokens': 300, 'cache_read_input_tokens': 70000, 'cache_creation_input_tokens': 29000}
    assert u['cacheShare'] == round(70000 / 100500, 3)
    assert u['models'] == {'claude-opus-5-5': 4, 'claude-sonnet-5-5': 2} and u['outcomes'] == {'done': 2}
    assert [(t['name'], t['calls'], t['avgMs'], t['maxMs'], t['truncated'], t['errors']) for t in u['tools']] == [('esql', 2, 32000, 59000, 1, 0), ('card', 1, 1, 1, 0, 0), ('gitlab_search', 1, 3000, 3000, 0, 1)]
    assert [(k['key'], k['runs'], k['usd']) for k in u['keys']] == [(K2, 1, 0.6), (K1, 1, 0.4)]
    assert [(s['tool'], s['ms'], s['run']) for s in u['slowest'][:2]] == [('esql', 59000, 'r1'), ('esql', 5000, 'r1')]


def test_the_usage_and_full_run_endpoints(client: TestClient, store: Store):
    # given
    seed(store)

    # when
    usage = client.get('/api/investigator/usage', params={'days': 7}).json()
    full = client.get('/api/investigations/r1').json()
    page = client.get('/api/investigations', params={'key': K1}).json()[0]
    missing = client.get('/api/investigations/nope')
    too_long = client.get('/api/investigator/usage', params={'days': 31})

    # then
    assert usage['runs'] == 2 and usage['usd'] == 1.0
    assert full['id'] == 'r1' and len(full['toolCalls']) == 3 and full['toolCalls'][1]['truncated'] is True and full['usage']['cache_read_input_tokens'] == 50000
    assert page['toolCalls'] == 3 and 'usage' not in page
    assert missing.status_code == 404
    assert too_long.status_code == 422


def test_the_run_list_names_the_action_so_the_card_can_find_its_ask(client: TestClient, store: Store):
    # given
    store.add_investigation({'id': 'r9', 'mode': 'live', 'trigger': 'ask', 'actionId': 'act-9', 'keys': [K1], 'startedAt': iso(utc_now()), 'outcome': 'running',
                             'ask': {'text': 'Why?', 'by': 'op@example.com'}})

    # when
    page = client.get('/api/investigations', params={'key': K1}).json()[0]

    # then
    assert (page['id'], page['actionId'], page['ask']) == ('r9', 'act-9', {'text': 'Why?', 'by': 'op@example.com'})


def test_sonnet_5_5_is_priced():
    # then
    assert known('claude-sonnet-5-5')
    assert usd('claude-sonnet-5-5', {'input_tokens': 1_000_000, 'output_tokens': 100_000, 'cache_read_input_tokens': 1_000_000, 'cache_creation_input_tokens': 100_000}) == 2.0 + 1.0 + 0.1 + 0.25
