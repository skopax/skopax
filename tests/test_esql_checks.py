import json
from urllib.parse import unquote

import pytest

from skopax import engine, promqlchecks
from skopax.contract import HttpError, Unreachable
from skopax.instances import Instances, PluginState
from skopax.kit import elastic, promql
from skopax.kit.elastic import epoch
from skopax.plugins import elasticsearch, prometheus
from skopax.promqlchecks import CARDS_MAX, PASS, ROWS_MAX, CustomChecks, Invalid, clean
from skopax.registry import Registry
from skopax.remote import JSON_READ, Reply, TimedOut, TooLarge
from skopax.runtime import Runtime
from skopax.sealer import Sealer
from tests.plugin_kit import KIT_KEY, PROM_URL, add_instance

NOW = 1_792_224_000.0
ES_URL = 'https://es.example.com'
KIBANA = 'https://kibana.example.com'
API_KEY = 'test-api-key-1234'
Q = 'FROM logs-*\n| WHERE @timestamp >= ?start AND @timestamp < ?end AND service.name IS NOT NULL\n| STATS value = COUNT(*) BY service = service.name'
DENIED = json.dumps({'error': {'type': 'security_exception', 'reason': 'action [indices:data/read/esql] is unauthorized for API key'}})
BAD = json.dumps({'error': {'type': 'verification_exception', 'reason': 'Found 1 problem\nline 1:1: Unknown column [nope]'}})


def answer(rows: list[dict], columns: list[str] | None = None) -> dict:
    names = columns or list(dict.fromkeys(k for r in rows for k in r)) or ['value', 'service']
    return {'columns': [{'name': n, 'type': 'keyword'} for n in names], 'values': [[r.get(n) for n in names] for r in rows]}


class Source:
    def __init__(self):
        self.now = NOW
        self.answers: dict = {}
        self.columns: dict = {}
        self.asked: list[dict] = []
        self.timeouts: list[float] = []
        self.clock = 0.0
        self.took = 0.0

    def set(self, query: str, rows, back: int = 0, columns: list[str] | None = None) -> None:
        self.answers[(query, back)] = rows
        self.columns[(query, back)] = columns

    def __call__(self, method, url, headers, data, timeout):
        self.clock += self.took
        self.timeouts.append(timeout)
        if (method, url) != ('POST', f'{ES_URL}/_query'):
            raise HttpError(404, url)
        body = json.loads(data)
        params = {k: v for p in body.get('params') or [] for k, v in p.items()}
        self.asked.append({'body': body, 'params': params, 'auth': headers.get('Authorization'), 'read': headers.get(JSON_READ)})
        back = round(self.now - epoch(params['end'])) if 'end' in params else 0
        got = self.answers.get((body['query'], back), [])
        if isinstance(got, BaseException):
            raise got
        return Reply(answer(got, self.columns.get((body['query'], back))), {})


def checks_with(store, source: Source, monkeypatch=None, **config) -> CustomChecks:
    registry = Registry.of(prometheus.PLUGIN, elasticsearch.PLUGIN)
    runtime = Runtime(registry, PluginState(store.db), Instances(store.db, registry, lambda: Sealer(KIT_KEY)), source)
    add_instance(store, prometheus.PLUGIN, 'main', sealer_key=KIT_KEY, primary=True, url=PROM_URL)
    add_instance(store, elasticsearch.PLUGIN, 'central', sealer_key=KIT_KEY, primary=True, **({'url': ES_URL, 'api-key': API_KEY, 'ui-url': KIBANA} | config))
    runtime.refresh()
    if monkeypatch is not None:
        monkeypatch.setattr(promql, 'clock', lambda: source.clock)
    return CustomChecks(store, runtime, lambda results, now, links: engine.apply(store, results, now, links), monotonic=lambda: source.clock)


def check(**over) -> dict:
    return {'id': 'busy-services', 'name': 'Busy services', 'source': 'elasticsearch', 'instance': 'central', 'query': Q, 'op': '>', 'threshold': 100, 'severity': 'warn',
            'by': ['service'], 'title': '{service} logs {value} lines', 'text': 'Service {service} wrote {value} log lines in the hour.'} | over


def added(checks: CustomChecks, **over) -> dict:
    return checks.defs.create(clean(check(**over)), 'admin@example.com')


def sweep(checks: CustomChecks, source: Source, now: float = NOW):
    source.now = now
    return checks.run_once(now)


def cards(store) -> dict:
    return {a['key']: a for a in store.load().get('anomalies') or [] if a.get('src') == PASS and a['status'] == 'open'}


def key(check_id: str, *values: str) -> str:
    return promqlchecks.card_key(check_id, list(values))


def test_an_elasticsearch_check_is_plain_json_with_a_window_of_60_minutes_by_default():
    # when
    got = clean({'name': 'Busy services', 'source': 'elasticsearch', 'instance': 'central', 'query': Q, 'op': '>', 'threshold': '100'})
    prom = clean({'name': 'Up', 'source': 'prometheus', 'instance': 'main', 'query': 'up', 'op': '<', 'threshold': 1, 'window': 30})

    # then
    assert got == {'id': 'busy-services', 'name': 'Busy services', 'enabled': True, 'source': 'elasticsearch', 'instance': 'central', 'datasource': '', 'query': Q, 'window': 60,
                   'op': '>', 'threshold': 100.0, 'baselines': [], 'baselineMode': 'ratio', 'severity': 'warn', 'by': [], 'for': 1, 'title': '', 'text': ''}
    assert 'window' not in prom, 'a PromQL check has no window'
    assert clean(check(window='1440'))['window'] == 1440 and clean(check(window=5))['window'] == 5


@pytest.mark.parametrize('over, said', [
    ({'query': 'ROW value = 1'}, 'FROM'),
    ({'query': 'SHOW INFO'}, 'FROM'),
    ({'query': '| FROM logs-*'}, 'FROM'),
    ({'query': 'FROM logs-* | ' + 'x' * 2000}, 'query is too long'),
    ({'window': 4}, 'window'),
    ({'window': 1441}, 'window'),
    ({'window': 'an hour'}, 'window'),
    ({'window': True}, 'window'),
    ({'window': 7.5}, 'window'),
])
def test_an_elasticsearch_query_must_start_with_from_fit_2000_characters_and_have_a_window_of_5_to_1440_minutes(over, said):
    # when
    with pytest.raises(Invalid) as e:
        clean(check(**over))

    # then
    assert said in e.value.text


def test_firing_rows_open_one_card_per_by_value_with_the_window_as_params_and_a_kibana_link(store):
    # given
    source = Source()
    checks = checks_with(store, source)
    added(checks)
    source.set(Q, [{'value': 250, 'service': 'checkout-service', 'level': None}, {'value': 40, 'service': 'shop'}])

    # when
    result = sweep(checks, source)
    found = cards(store)

    # then
    assert result.ok and result.status == 'ok' and result.ran == (f'{PASS}:busy-services',)
    assert list(found) == [key('busy-services', 'checkout-service')]
    card = found[key('busy-services', 'checkout-service')]
    assert (card['sev'], card['svc'], card['label'], card['now']) == ('warn', 'Busy services', 'checkout-service logs 250 lines', 250)
    assert card['what'].startswith('Service checkout-service wrote 250 log lines in the hour.') and '250 > 100' in card['what']
    assert card['checkLabels'] == {'service': 'checkout-service'}
    asked = source.asked[0]
    assert asked['params'] == {'start': engine.iso(NOW - 3600), 'end': engine.iso(NOW)}
    assert asked['body']['query'] == Q and asked['auth'] == f'ApiKey {API_KEY}' and asked['read'] == str(promqlchecks.BODY_MAX)
    assert max(source.timeouts) <= promqlchecks.QUERY_TIME_S
    link = card['links'][0]
    assert link['label'] == 'Query in Kibana' and link['url'].startswith(f'{KIBANA}/app/discover#/?_g=')
    opened = unquote(link['url'])
    assert 'time:(from:now-60m,to:now)' in opened and 'dataSource:(type:esql)' in opened
    assert '@timestamp >= ?_tstart AND @timestamp < ?_tend' in opened, 'Kibana fills its own time range into the query'
    state = checks.defs.state('busy-services')
    assert (state['state'], state['series'], state['read'], state['firing'], state['cards']) == ('ok', 2, 2, 1, 1)


def test_a_longer_window_moves_start_and_a_query_without_params_gets_none(store):
    # given
    source = Source()
    checks = checks_with(store, source)
    added(checks, window=240)
    plain = 'FROM logs-* | STATS value = COUNT(*)'
    added(checks, id='all-time', name='All time', query=plain, by=[])

    # when
    sweep(checks, source)

    # then
    by_query = {a['body']['query']: a for a in source.asked}
    assert by_query[Q]['params'] == {'start': engine.iso(NOW - 240 * 60), 'end': engine.iso(NOW)}
    assert 'params' not in by_query[plain]['body']


def test_baselines_rerun_the_query_with_both_params_shifted_and_fire_only_against_every_baseline(store):
    # given
    source = Source()
    checks = checks_with(store, source)
    added(checks, threshold=3, baselines=['1d', '1w'])
    source.set(Q, [{'value': 600, 'service': 'shop'}, {'value': 600, 'service': 'checkout-service'}, {'value': 90, 'service': 'search'}])
    source.set(Q, [{'value': 100, 'service': 'shop'}, {'value': 100, 'service': 'checkout-service'}, {'value': 10, 'service': 'search'}], 86400)
    source.set(Q, [{'value': 100, 'service': 'shop'}, {'value': 300, 'service': 'checkout-service'}], 604800)

    # when
    sweep(checks, source)
    found = cards(store)

    # then
    shifted = sorted((a['params']['start'], a['params']['end']) for a in source.asked)
    assert shifted == sorted((engine.iso(NOW - back - 3600), engine.iso(NOW - back)) for back in (0, 86400, 604800))
    assert list(found) == [key('busy-services', 'shop')], 'checkout-service is only twice the week before, search has no week'
    assert (found[key('busy-services', 'shop')]['d1'], found[key('busy-services', 'shop')]['d7']) == (100, 100)
    assert checks.defs.state('busy-services')['noBaseline'] == 1


def test_a_long_or_double_value_counts_and_grouping_columns_fill_the_texts(store):
    # given
    source = Source()
    checks = checks_with(store, source)
    added(checks, by=['service', 'level'], title='{service} {level} at {value}', text='{service} wrote {value} {level} lines, {note}.')
    source.set(Q, [{'value': 120.5, 'service': 'shop', 'level': 'ERROR', 'note': ['a', 'b']}, {'value': 101, 'service': None, 'level': 'WARN'}])

    # when
    sweep(checks, source)
    found = cards(store)

    # then
    assert set(found) == {key('busy-services', 'shop', 'ERROR'), key('busy-services', '', 'WARN')}
    assert found[key('busy-services', 'shop', 'ERROR')]['what'].startswith('shop wrote 120.5 ERROR lines, a, b.')
    assert found[key('busy-services', '', 'WARN')]['label'] == 'WARN at 101', 'a null by value is shown as empty'


@pytest.mark.parametrize('columns, reason', [
    (['count', 'service'], 'the query returns no column named value: name the number value, such as STATS value = COUNT(*)'),
    (['value', 'svc'], 'the query returns no column named service, which by names'),
])
def test_a_missing_column_fails_the_check_also_when_no_row_comes_back(store, columns, reason):
    # given
    source = Source()
    checks = checks_with(store, source)
    added(checks)
    source.set(Q, [], columns=columns)

    # when
    sweep(checks, source)

    # then
    assert checks.defs.state('busy-services')['reason'] == reason


@pytest.mark.parametrize('rows, reason', [
    ([{'value': float('inf'), 'service': 'shop'}], 'the value column holds a number that is not finite'),
    ([{'value': '250', 'service': 'shop'}], 'the value column must hold numbers, not text'),
    ([{'value': True, 'service': 'shop'}], 'the value column must hold numbers, not true or false'),
    ([{'value': [1, 2], 'service': 'shop'}], 'the value column must hold numbers, not a list'),
    ([{'value': None, 'service': 'shop'}], 'the value column is null in a row: drop such rows with WHERE value IS NOT NULL'),
    ([{'count': 250, 'service': 'shop'}], 'the query returns no column named value: name the number value, such as STATS value = COUNT(*)'),
    ([{'value': 250, 'svc': 'shop'}], 'the query returns no column named service, which by names'),
])
def test_a_value_that_is_not_a_number_or_a_missing_column_fails_the_check_with_a_clear_reason(store, rows, reason):
    # given
    source = Source()
    checks = checks_with(store, source)
    added(checks)
    source.set(Q, rows)

    # when
    result = sweep(checks, source)

    # then
    assert result.ok and result.ran == () and cards(store) == {}
    assert checks.defs.state('busy-services')['reason'] == reason


def test_an_empty_result_is_a_quiet_pass(store):
    # given
    source = Source()
    checks = checks_with(store, source)
    added(checks)

    # when
    result = sweep(checks, source)

    # then
    assert result.status == 'ok' and result.ran == (f'{PASS}:busy-services',) and cards(store) == {}
    assert checks.defs.state('busy-services')['state'] == 'ok'


def test_at_most_the_row_cap_is_read_and_at_most_the_card_cap_is_raised_the_rest_on_one_card(store):
    # given
    source = Source()
    checks = checks_with(store, source)
    added(checks)
    source.set(Q, [{'value': 101 + n % 7, 'service': f'svc-{n:05d}'} for n in range(ROWS_MAX + 200)])

    # when
    sweep(checks, source)
    found = cards(store)
    preview = checks.preview(check(), NOW)

    # then
    assert len(found) == CARDS_MAX
    more = found[key('busy-services') + '|more']
    assert more['now'] == ROWS_MAX - (CARDS_MAX - 1)
    state = checks.defs.state('busy-services')
    assert (state['series'], state['read'], state['firing']) == (ROWS_MAX + 200, ROWS_MAX, ROWS_MAX)
    assert (preview['series'], preview['read']) == (ROWS_MAX + 200, ROWS_MAX)
    assert any('rows' in w for w in preview['warnings'])


@pytest.mark.parametrize('error, reason', [
    (HttpError(401, f'{ES_URL}/_query', '{}'), 'Elasticsearch central answered HTTP 401: the API key was not accepted by Elasticsearch'),
    (HttpError(403, f'{ES_URL}/_query', DENIED), 'Elasticsearch central answered HTTP 403: the API key lacks the read privilege on logs-*'),
    (HttpError(400, f'{ES_URL}/_query', BAD), 'Elasticsearch central answered HTTP 400: verification_exception: Found 1 problem line 1:1: Unknown column [nope]'),
    (Unreachable('down'), 'Elasticsearch central is unreachable'),
])
def test_a_refused_bad_or_unreachable_query_fails_the_check_with_the_reason(store, error, reason):
    # given
    source = Source()
    checks = checks_with(store, source)
    added(checks)
    source.set(Q, error)

    # when
    result = sweep(checks, source)

    # then
    assert result.ok and result.status == 'ok \u00b7 1 of 1 custom checks failing: Busy services' and cards(store) == {}
    assert checks.defs.state('busy-services')['reason'] == reason


def test_an_unreachable_instance_holds_back_its_other_checks_in_that_pass(store):
    # given
    source = Source()
    checks = checks_with(store, source)
    added(checks)
    added(checks, id='second', name='Second')
    source.set(Q, Unreachable('down'))

    # when
    sweep(checks, source)

    # then
    reasons = sorted(checks.defs.state(i)['reason'] for i in ('busy-services', 'second'))
    assert reasons == ['Elasticsearch central is unreachable', 'not run: elasticsearch central did not answer earlier in this pass']


def test_a_second_timeout_on_the_same_instance_holds_back_its_remaining_checks_in_that_pass(store):
    # given
    source = Source()
    checks = checks_with(store, source)
    slow_one, slow_two, last = 'FROM logs-a | STATS value = COUNT(*)', 'FROM logs-b | STATS value = COUNT(*)', 'FROM logs-c | STATS value = COUNT(*)'
    for check_id, query in (('a-first', slow_one), ('b-second', slow_two), ('c-third', last)):
        added(checks, id=check_id, name=check_id, query=query, by=[])
    source.set(slow_one, TimedOut('slow'))
    source.set(slow_two, TimedOut('slow'))
    source.set(last, [{'value': 500}])

    # when
    sweep(checks, source)

    # then
    late = f'the queries took longer than {promqlchecks.QUERY_TIME_S} s'
    assert [checks.defs.state(i)['reason'] for i in ('a-first', 'b-second', 'c-third')] == [late, late, 'not run: elasticsearch central did not answer earlier in this pass']
    assert [a['body']['query'] for a in source.asked] == [slow_one, slow_two]


def test_a_slow_query_hits_the_time_limit_and_fails_the_check(store, monkeypatch):
    # given
    source = Source()
    checks = checks_with(store, source, monkeypatch)
    added(checks, baselines=['1d'])
    source.took = promqlchecks.QUERY_TIME_S + 1
    source.set(Q, [{'value': 500, 'service': 'shop'}])

    # when
    sweep(checks, source)

    # then
    assert checks.defs.state('busy-services')['reason'] == f'the queries took longer than {promqlchecks.QUERY_TIME_S} s'
    assert len(source.asked) == 1


@pytest.mark.parametrize('error, reason', [
    (TimedOut('slow'), f'the queries took longer than {promqlchecks.QUERY_TIME_S} s'),
    (TooLarge(f'{ES_URL}/_query answered more than 8 MB'), 'Elasticsearch central answered more than 8 MB: aggregate with STATS or keep fewer columns'),
])
def test_a_timed_out_or_oversized_query_fails_only_its_own_check(store, error, reason):
    # given
    source = Source()
    checks = checks_with(store, source)
    added(checks)
    other = 'FROM logs-* | STATS value = COUNT(*)'
    added(checks, id='second', name='Second', query=other, by=[])
    source.set(Q, error)
    source.set(other, [{'value': 500}])

    # when
    sweep(checks, source)

    # then
    assert checks.defs.state('busy-services')['reason'] == reason
    assert checks.defs.state('second')['state'] == 'ok' and set(cards(store)) == {key('second')}, 'the other check on the instance still runs'


def test_a_deleted_or_switched_off_instance_fails_its_checks_with_a_clear_reason(store):
    # given
    source = Source()
    checks = checks_with(store, source)
    added(checks)
    added(checks, id='gone', name='Gone', instance='old-logs')

    # when
    store.db['instances'].delete_one({'_id': 'elasticsearch/central'})
    checks.runtime.refresh()
    sweep(checks, source)

    # then
    assert checks.defs.state('busy-services')['reason'] == 'no elasticsearch instance named central'
    assert checks.defs.state('gone')['reason'] == 'no elasticsearch instance named old-logs'
    assert source.asked == []


def test_a_switched_off_instance_fails_its_checks_with_a_clear_reason(store):
    # given
    source = Source()
    checks = checks_with(store, source)
    added(checks)

    # when
    store.db['instances'].update_one({'_id': 'elasticsearch/central'}, {'$set': {'enabled': False}})
    checks.runtime.refresh()
    sweep(checks, source)

    # then
    assert checks.defs.state('busy-services')['reason'] == 'no elasticsearch instance named central'
    assert source.asked == [] and cards(store) == {}


def test_a_long_list_value_is_cut_like_any_label_value(store):
    # given
    source = Source()
    checks = checks_with(store, source)
    added(checks)
    source.set(Q, [{'value': 250, 'service': ['x' * 150, 'y' * 150]}])

    # when
    sweep(checks, source)

    # then
    (card,) = cards(store).values()
    assert len(card['checkLabels']['service']) == promqlchecks.LABEL_MAX


@pytest.mark.parametrize('config', [{'ui-url': ''}, {'ui-url': 'http://kibana.example.com'}])
def test_no_link_without_an_https_kibana_address(store, config):
    # given
    source = Source()
    checks = checks_with(store, source, **config)
    added(checks)
    source.set(Q, [{'value': 250, 'service': 'shop'}])

    # when
    sweep(checks, source)

    # then
    assert 'links' not in cards(store)[key('busy-services', 'shop')]


def test_preview_warns_when_the_query_has_no_time_params_and_does_not_fail(store):
    # given
    source = Source()
    checks = checks_with(store, source)
    plain = 'FROM logs-* | STATS value = COUNT(*) BY service = service.name'
    source.set(plain, [{'value': 250, 'service': 'shop'}])
    source.set(Q, [{'value': 250, 'service': 'shop'}])

    # when
    unbounded = checks.preview(check(query=plain), NOW)
    bounded = checks.preview(check(), NOW)
    prom = checks.preview({'name': 'Up', 'source': 'prometheus', 'instance': 'main', 'query': 'up', 'op': '<', 'threshold': 1}, NOW)

    # then
    assert unbounded['ok'] and unbounded['warnings'] == ['query has no ?start/?end: reads every document']
    assert [c['title'] for c in unbounded['cards']] == ['shop logs 250 lines']
    assert bounded['ok'] and bounded['warnings'] == []
    assert bounded['cards'][0]['links'][0]['label'] == 'Query in Kibana'
    assert prom['warnings'] == []


def test_preview_shows_why_a_check_would_fail(store):
    # given
    source = Source()
    checks = checks_with(store, source)
    source.set(Q, HttpError(403, f'{ES_URL}/_query', DENIED))

    # when
    got = checks.preview(check(), NOW)

    # then
    assert not got['ok'] and got['reason'] == 'Elasticsearch central answered HTTP 403: the API key lacks the read privilege on logs-*'
    assert got['cards'] == [] and got['warnings'] == []


def test_elasticsearch_instances_are_offered_as_a_source(store):
    # given
    checks = checks_with(store, Source())

    # when
    got = checks.sources()

    # then
    assert got == {'prometheus': ['main'], 'grafana': [], 'elasticsearch': ['central']}


def test_changing_the_window_starts_the_for_count_again_and_a_title_change_does_not(store):
    # given
    source = Source()
    checks = checks_with(store, source)
    added(checks, **{'for': 3})
    source.set(Q, [{'value': 250, 'service': 'shop'}])
    sweep(checks, source)
    waiting = checks.defs.state('busy-services')['pending']

    # when
    checks.defs.update('busy-services', clean(check(**{'for': 3, 'title': 'renamed {service}'})), 'admin@example.com')
    kept = checks.defs.state('busy-services')['pending']
    checks.defs.update('busy-services', clean(check(**{'for': 3, 'window': 120})), 'admin@example.com')

    # then
    assert waiting == kept == {key('busy-services', 'shop'): NOW}
    assert checks.defs.state('busy-services')['pending'] == {}


def test_the_kibana_link_keeps_rison_quotes_inside_the_query_safe():
    # when
    url = elastic.esql_discover(KIBANA, 'FROM logs-* | WHERE message == "it\'s!" AND @timestamp >= ?start', 30)

    # then
    assert "esql:'FROM logs-* | WHERE message == \"it!'s!!\" AND @timestamp >= ?_tstart'" in unquote(url)
    assert 'from:now-30m' in unquote(url)
    assert elastic.esql_discover('', 'FROM x', 30) == '' and elastic.esql_discover('http://kibana.example.com', 'FROM x', 30) == ''
