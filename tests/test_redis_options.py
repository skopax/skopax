from skopax.plugins import redis
from skopax.plugins.redis import engine
from tests.fake_http import FakeHttp
from tests.test_redis_plugin import GET, MAIN, MAIN_ONLY, READER, answer, app_of, run_once, runtime_of


def addressed(address: str):
    def go(q, body):
        got = answer()(q, body)
        for r in got['data']['result']:
            if 'net_sock_peer_addr' in r['metric']:
                r['metric']['net_sock_peer_addr'] = address
        return got

    return go


def test_a_friendly_name_replaces_the_address_on_rows_and_cards_and_the_address_stays_without_a_map(store):
    # given
    http = FakeHttp({(GET, f'{MAIN}/api/v1/query'): addressed('10.0.4.12:6379')})

    # when
    with app_of(store, http, MAIN_ONLY, {'main': {'prometheus': 'main', 'peer-names': '10.0.4.12:6379=sessions-cache, 10.0.4.13=queue'}}) as c:
        named = run_once(c)

    # then
    rows = [r for r in named.sections['redis'] if r['part'] == 'app']
    assert {r['redis'] for r in rows} == {'sessions-cache'}
    assert 'sessions-cache' in next(x for x in named.result.found if x['sig'] == 'redis-app')['what']


def test_without_a_map_the_address_is_shown_as_it_is(store):
    # given
    http = FakeHttp({(GET, f'{MAIN}/api/v1/query'): addressed('10.0.4.12:6379')})

    # when
    with app_of(store, http, MAIN_ONLY, READER) as c:
        plain = run_once(c)

    # then
    assert {r['redis'] for r in plain.sections['redis'] if r['part'] == 'app'} == {'10.0.4.12:6379'}
    assert '10.0.4.12:6379' in next(x for x in plain.result.found if x['sig'] == 'redis-app')['what']


def test_a_map_entry_without_a_port_matches_any_port_and_an_unlisted_address_is_shown_as_it_is(store):
    # then
    names = engine.peer_names('10.0.4.13=queue, 10.0.4.12:6379=sessions, Cache.Example.com=shared')[0]
    assert engine.friendly('10.0.4.13:6380', names) == 'queue' and engine.friendly('10.0.4.12:6379', names) == 'sessions'
    assert engine.friendly('10.0.4.12:6380', names) == '10.0.4.12:6380' and engine.friendly('cache.example.com', names) == 'shared'
    assert engine.friendly('10.9.9.9:6379', names) == '10.9.9.9:6379' and engine.friendly(None, names) is None and engine.friendly('', names) == ''
    assert engine.friendly('10.0.4.13:6379', {}) == '10.0.4.13:6379'


def test_lines_of_the_map_that_cannot_be_read_are_named_and_the_others_still_count():
    # when
    found, bad = engine.peer_names('a=b, c, =d, e=, a=z')

    # then
    assert found == {'a': 'b'} and bad == ['c', '=d', 'e=', 'a=z']


def test_the_redis_tool_and_the_test_use_the_map_too(store):
    # given
    http = FakeHttp({(GET, f'{MAIN}/api/v1/query'): addressed('10.0.4.12:6379')})
    runtime = runtime_of(store, http, MAIN_ONLY, {'main': {'prometheus': 'main', 'peer-names': '10.0.4.12=sessions-cache'}})
    bad = runtime_of(store, http, MAIN_ONLY, {'main': {'prometheus': 'main', 'peer-names': 'oops'}})

    # when
    ctx = runtime.context(redis.PLUGIN, runtime.instances.get('redis', 'main'), 1_792_224_000.0)
    shown = redis.read(ctx, {'op': 'apps'})
    refused = redis.test(bad.context(redis.PLUGIN, bad.instances.get('redis', 'main'), 1_792_224_000.0))

    # then
    assert {r['redis'] for r in shown['apps']} == {'sessions-cache'}
    assert refused.ok is False and 'oops' in refused.summary
