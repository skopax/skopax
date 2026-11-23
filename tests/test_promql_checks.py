import dataclasses
import json
from urllib.parse import parse_qs, urlsplit

import pytest

from skopax import engine, messages, promqlchecks
from skopax.contract import Check, HttpError, Unreachable
from skopax.instances import Instances, PluginState
from skopax.kit import promql
from skopax.plugins import grafana, prometheus
from skopax.promqlchecks import CARDS_MAX, PASS, SERIES_MAX, CustomChecks, Invalid, clean, render
from skopax.registry import CORE_RAN, Registry
from skopax.remote import JSON_READ, Reply, TimedOut
from skopax.runtime import Runtime
from skopax.sealer import Sealer
from tests.plugin_kit import KIT_KEY, PROM_URL, add_instance
from tests.plugins import demo_plugin

NOW = 1_792_224_000.0
GRAFANA = 'https://grafana.example.com'
Q = '100 * redis_memory_used_bytes / redis_memory_max_bytes'
A, B = 'cache-a.example.com:6379', 'cache-b.example.com:6379'


def vector(rows) -> dict:
    return {'status': 'success', 'data': {'resultType': 'vector', 'result': [{'metric': m, 'value': [NOW, v if isinstance(v, str) else str(v)]} for m, v in rows]}}


class Source:
    def __init__(self):
        self.now = NOW
        self.answers: dict = {}
        self.asked: list[dict] = []
        self.listed = [{'uid': 'ds-1', 'name': 'cloud-metrics', 'type': 'prometheus'}]
        self.lists = 0
        self.timeouts: list[float] = []
        self.took = 0.0
        self.clock = 0.0

    def set(self, query: str, rows, back: int = 0) -> None:
        self.answers[(query, back)] = rows

    def __call__(self, method, url, headers, data, timeout):
        parts = urlsplit(url)
        params = {k: v[0] for k, v in parse_qs(parts.query).items()}
        self.timeouts.append(timeout)
        self.clock += self.took
        if parts.path == '/api/datasources':
            self.lists += 1
            if isinstance(self.listed, Exception):
                raise self.listed
            return Reply(self.listed, {})
        if parts.path not in (f'{urlsplit(PROM_URL).path}/api/v1/query', '/api/datasources/proxy/uid/ds-1/api/v1/query'):
            raise HttpError(404, url)
        self.asked.append(params | {'path': parts.path, 'read': headers.get(JSON_READ)})
        back = round(self.now - float(params['time']))
        got = self.answers.get((params['query'], back), [])
        if callable(got):
            got = got()
        if isinstance(got, Exception):
            raise got
        if isinstance(got, dict):
            return Reply(got, {})
        return Reply(vector(got), {})


def checks_with(store, source: Source, monkeypatch=None) -> CustomChecks:
    registry = Registry.of(prometheus.PLUGIN, grafana.PLUGIN)
    runtime = Runtime(registry, PluginState(store.db), Instances(store.db, registry, lambda: Sealer(KIT_KEY)), source)
    add_instance(store, prometheus.PLUGIN, 'main', sealer_key=KIT_KEY, primary=True, url=PROM_URL, **{'ui-url': 'https://prom-ui.example.com'})
    add_instance(store, grafana.PLUGIN, 'ops', sealer_key=KIT_KEY, primary=True, url=GRAFANA, token='test-token-1')
    runtime.refresh()
    if monkeypatch is not None:
        monkeypatch.setattr(promql, 'clock', lambda: source.clock)
    return CustomChecks(store, runtime, lambda results, now, links: engine.apply(store, results, now, links), monotonic=lambda: source.clock)


def check(**over) -> dict:
    return {'id': 'redis-memory', 'name': 'Redis memory', 'source': 'prometheus', 'instance': 'main', 'query': Q, 'op': '>', 'threshold': 90, 'severity': 'warn',
            'by': ['instance'], 'title': '{instance} memory at {value} %', 'text': 'Redis {instance} uses {value} % of its memory.'} | over


def added(checks: CustomChecks, **over) -> dict:
    return checks.defs.create(clean(check(**over)), 'admin@example.com')


def sweep(checks: CustomChecks, source: Source, now: float = NOW):
    source.now = now
    return checks.run_once(now)


def cards(store, status: str | None = 'open') -> dict:
    return {a['key']: a for a in store.load().get('anomalies') or [] if a.get('src') == PASS and (status is None or a['status'] == status)}


def key(check_id: str, *values: str) -> str:
    return promqlchecks.card_key(check_id, list(values))


def test_a_definition_is_plain_json_with_the_defaults_filled_in():
    # when
    got = clean({'name': ' Redis memory ', 'source': 'grafana', 'instance': 'ops', 'datasource': 'cloud-metrics', 'query': Q, 'op': '>=', 'threshold': '90'})

    # then
    assert got == {'id': 'redis-memory', 'name': 'Redis memory', 'enabled': True, 'source': 'grafana', 'instance': 'ops', 'datasource': 'cloud-metrics', 'query': Q,
                   'op': '>=', 'threshold': 90.0, 'baselines': [], 'baselineMode': 'ratio', 'severity': 'warn', 'by': [], 'for': 1, 'title': '', 'text': ''}
    assert json.loads(json.dumps(got)) == got


@pytest.mark.parametrize('over, said', [
    ({'id': 'Bad Id'}, 'id'),
    ({'name': ''}, 'name'),
    ({'source': 'influx'}, 'source'),
    ({'instance': ''}, 'instance'),
    ({'source': 'grafana', 'instance': 'ops', 'datasource': ''}, 'datasource'),
    ({'query': ''}, 'query'),
    ({'query': 'x' * 2001}, 'query'),
    ({'query': 'up\x00'}, 'query'),
    ({'op': '=~'}, 'comparison'),
    ({'threshold': 'NaN'}, 'threshold'),
    ({'threshold': 'inf'}, 'threshold'),
    ({'threshold': True}, 'threshold'),
    ({'baselines': ['2d']}, 'baseline'),
    ({'baselines': ['1d'], 'baselineMode': 'percent'}, 'baseline'),
    ({'severity': 'fatal'}, 'severity'),
    ({'by': ['instance', 'bad-label']}, 'label'),
    ({'by': ['a', 'b', 'c', 'd', 'e', 'f']}, 'label'),
    ({'for': 0}, 'for'),
    ({'for': 13}, 'for'),
    ({'title': '{value:>10}'}, 'title'),
    ({'title': '{instance.__class__}'}, 'title'),
    ({'text': 'see {instance[0]}'}, 'text'),
    ({'text': 'a { b'}, 'text'),
    ({'text': 'x' * 1001}, 'text'),
    ({'enabled': 'yes'}, 'enabled'),
])
def test_a_bad_field_is_refused_with_a_text_that_names_it(over, said):
    # when
    with pytest.raises(Invalid) as e:
        clean(check(**over))

    # then
    assert said in e.value.text


def test_templates_fill_placeholders_only_once_and_never_run_an_expression():
    # given
    labels = {'instance': 'a {value} b {instance}', 'job': 'redis'}

    # when
    plain = render('{instance} at {value} % ({job}, {missing})', labels, 93.456789)
    whole = render('{value}', {}, 12.0)
    huge = render('{instance}', {'instance': 'x' * 5000}, 1.0)
    marked = render('{instance} *{value}*', {'instance': 'a*b`c*'}, 2.0)

    # then
    assert plain == 'a {value} b {instance} at 93.457 % (redis, )'
    assert whole == '12'
    assert len(huge) <= promqlchecks.TITLE_MAX
    assert marked == "a\u2217b'c\u2217 *2*", 'a label value cannot bring Slack bold or code marks; the template keeps its own'


def test_a_firing_series_opens_one_card_per_label_set_from_the_templates_with_a_graph_link(store):
    # given
    source = Source()
    checks = checks_with(store, source)
    added(checks)
    source.set(Q, [({'instance': A, 'job': 'redis'}, 93.5), ({'instance': B, 'job': 'redis'}, 40)])

    # when
    result = sweep(checks, source)
    found = cards(store)

    # then
    assert result.ok and result.status == 'ok' and result.ran == (f'{PASS}:redis-memory',)
    assert list(found) == [key('redis-memory', A)]
    card = found[key('redis-memory', A)]
    assert (card['sev'], card['svc'], card['label'], card['now'], card['ranKey']) == ('warn', 'Redis memory', f'{A} memory at 93.5 %', 93.5, f'{PASS}:redis-memory')
    assert card['what'].startswith(f'Redis {A} uses 93.5 % of its memory.') and '93.5 > 90' in card['what']
    assert card['checkLabels'] == {'instance': A}
    assert card['links'][0]['label'] == 'Query in Prometheus' and card['links'][0]['url'].startswith('https://prom-ui.example.com/query?')
    assert source.asked[0]['time'] == f'{NOW:.3f}' and source.asked[0]['timeout'] == '20s'
    assert max(source.timeouts) <= promqlchecks.QUERY_TIME_S
    state = checks.defs.state('redis-memory')
    assert (state['state'], state['series'], state['firing'], state['cards']) == ('ok', 2, 1, 1)


def test_for_waits_that_many_firing_passes_and_a_quiet_pass_starts_it_again(store):
    # given
    source = Source()
    checks = checks_with(store, source)
    added(checks, **{'for': 3})
    source.set(Q, [({'instance': A}, 95)])

    # when
    seen = []
    for n, firing in enumerate((True, True, False, True, True, True)):
        source.set(Q, [({'instance': A}, 95 if firing else 10)])
        sweep(checks, source, NOW + 300 * n)
        seen.append(bool(cards(store)))

    # then
    assert seen == [False, False, False, False, False, True]


def test_a_card_clears_the_existing_way_once_its_series_stops_matching(store):
    # given
    source = Source()
    checks = checks_with(store, source)
    added(checks)
    source.set(Q, [({'instance': A}, 95)])
    sweep(checks, source)

    # when
    source.set(Q, [({'instance': A}, 50)])
    sweep(checks, source, NOW + 300)

    # then
    assert cards(store) == {} and cards(store, 'cleared')[key('redis-memory', A)]['clearedAt'] == engine.iso(NOW + 300)


def test_a_bad_query_is_one_failing_state_on_that_check_that_keeps_its_cards_and_the_other_checks(store):
    # given
    source = Source()
    checks = checks_with(store, source)
    added(checks)
    added(checks, id='other', name='Other', query='up == 0', op='==', threshold=0, by=[])
    source.set(Q, [({'instance': A}, 95)])
    sweep(checks, source)
    body = json.dumps({'status': 'error', 'errorType': 'bad_data', 'error': '1:9: parse error: unexpected <EOF>'})

    # when
    source.set(Q, HttpError(400, PROM_URL, body))
    source.set('up == 0', [({'job': 'x'}, 0)])
    result = sweep(checks, source, NOW + 300)

    # then
    assert result.ok and result.ran == (f'{PASS}:other',)
    assert result.status == 'ok \u00b7 1 of 2 custom checks failing: Redis memory'
    state = checks.defs.state('redis-memory')
    assert state['state'] == 'failing' and state['reason'] == 'Prometheus main answered HTTP 400: 1:9: parse error: unexpected <EOF>'
    assert set(cards(store)) == {key('redis-memory', A), key('other')}


@pytest.mark.parametrize('over, answer, reason', [
    ({}, Unreachable('down'), 'Prometheus main is unreachable'),
    ({'instance': 'gone'}, [], 'no prometheus instance named gone'),
    ({'query': 'up[5m]'}, {'status': 'success', 'data': {'resultType': 'matrix', 'result': []}}, 'the query must give an instant vector or a scalar, not a matrix'),
    ({}, {'status': 'error'}, 'Prometheus main did not answer with a query result'),
])
def test_an_unreachable_source_a_missing_instance_or_an_odd_answer_fails_the_check_and_raises_nothing(store, over, answer, reason):
    # given
    source = Source()
    checks = checks_with(store, source)
    d = added(checks, **over)
    source.set(d['query'], answer)

    # when
    result = sweep(checks, source)

    # then
    assert result.ok and result.ran == () and cards(store) == {}
    assert checks.defs.state('redis-memory')['reason'] == reason


def test_a_slow_query_hits_the_time_limit_and_fails_the_check(store, monkeypatch):
    # given
    source = Source()
    checks = checks_with(store, source, monkeypatch)
    added(checks, baselines=['1d', '1w'])
    source.took = promqlchecks.QUERY_TIME_S + 1
    source.set(Q, [({'instance': A}, 95)])

    # when
    sweep(checks, source)

    # then
    assert checks.defs.state('redis-memory')['reason'] == f'the queries took longer than {promqlchecks.QUERY_TIME_S} s'
    assert len(source.asked) == 1 and cards(store) == {}


def test_checks_left_when_the_pass_runs_out_of_time_wait_for_the_next_pass(store, monkeypatch):
    # given
    source = Source()
    checks = checks_with(store, source, monkeypatch)
    added(checks)
    added(checks, id='second', name='Second')
    source.took = promqlchecks.PASS_TIME_S

    # when
    result = sweep(checks, source)

    # then
    assert result.ran == (f'{PASS}:redis-memory',)
    assert checks.defs.state('second')['reason'] == 'not run: the pass ran out of time'


def test_at_most_the_series_cap_is_read_and_at_most_the_card_cap_is_raised_the_rest_on_one_card(store):
    # given
    source = Source()
    checks = checks_with(store, source)
    added(checks)
    source.set(Q, [({'instance': f'cache-{n:04d}'}, 91 + n % 7) for n in range(SERIES_MAX + 100)])

    # when
    sweep(checks, source)
    found = cards(store)

    # then
    assert len(found) == CARDS_MAX
    more = found[key('redis-memory') + '|more']
    assert more['now'] == SERIES_MAX - (CARDS_MAX - 1) and more['sev'] == 'warn'
    assert f'{SERIES_MAX - CARDS_MAX + 1} more label sets' in more['what']
    shown = sorted(a['now'] for k, a in found.items() if not k.endswith('|more'))
    assert shown[0] == 97, 'the cards go to the largest values first'
    state = checks.defs.state('redis-memory')
    assert (state['series'], state['read'], state['firing']) == (SERIES_MAX + 100, SERIES_MAX, SERIES_MAX)


def test_nan_and_infinite_values_never_reach_a_card_and_an_empty_result_is_quiet(store):
    # given
    source = Source()
    checks = checks_with(store, source)
    added(checks)
    added(checks, id='empty', name='Empty', query='absent_metric > 1')
    source.set(Q, [({'instance': A}, 'NaN'), ({'instance': B}, '+Inf'), ({'instance': 'c'}, '-Inf'), ({'instance': 'd'}, 'junk')])

    # when
    result = sweep(checks, source)

    # then
    assert cards(store) == {} and result.ran == (f'{PASS}:empty', f'{PASS}:redis-memory')
    assert checks.defs.state('redis-memory')['skipped'] == 4
    assert checks.defs.state('empty')['state'] == 'ok'


def test_a_baseline_is_the_same_query_at_an_earlier_time_and_fires_only_against_every_baseline(store):
    # given
    source = Source()
    checks = checks_with(store, source)
    ratio = 'sum by (instance) (rate(redis_commands_duration_seconds_total[5m])) / sum by (instance) (rate(redis_commands_processed_total[5m]))'
    added(checks, id='latency', name='Latency', query=ratio, threshold=2, baselines=['1d', '1w'], title='{instance} slower', text='')
    source.set(ratio, [({'instance': A}, 30), ({'instance': B}, 30), ({'instance': 'c'}, 30), ({'instance': 'd'}, 30)])
    source.set(ratio, [({'instance': A}, 10), ({'instance': B}, 10), ({'instance': 'c'}, 0)], back=86400)
    source.set(ratio, [({'instance': A}, 12), ({'instance': B}, 20), ({'instance': 'c'}, 10), ({'instance': 'd'}, 10)], back=604800)

    # when
    sweep(checks, source)
    found = cards(store)

    # then
    assert {a['query'] for a in source.asked} == {ratio}
    assert sorted(round(NOW - float(a['time'])) for a in source.asked) == [0, 86400, 604800]
    assert list(found) == [key('latency', A)]
    card = found[key('latency', A)]
    assert (card['now'], card['d1'], card['d7']) == (30, 10, 12)
    assert 'ratio 3 to 1 day ago and 2.5 to 1 week ago' in card['what']


def test_a_difference_baseline_compares_the_change(store):
    # given
    source = Source()
    checks = checks_with(store, source)
    added(checks, id='growth', name='Growth', threshold=20, baselines=['1d'], baselineMode='difference')
    source.set(Q, [({'instance': A}, 50), ({'instance': B}, 50)])
    source.set(Q, [({'instance': A}, 20), ({'instance': B}, 40)], back=86400)

    # when
    sweep(checks, source)

    # then
    assert list(cards(store)) == [key('growth', A)]
    assert 'difference 30 to 1 day ago' in cards(store)[key('growth', A)]['what']


def test_a_changed_label_set_closes_the_old_card_and_opens_a_new_one(store):
    # given
    source = Source()
    checks = checks_with(store, source)
    added(checks)
    source.set(Q, [({'instance': A}, 95)])
    sweep(checks, source)

    # when
    source.set(Q, [({'instance': B}, 95)])
    sweep(checks, source, NOW + 300)

    # then
    assert list(cards(store)) == [key('redis-memory', B)] and list(cards(store, 'cleared')) == [key('redis-memory', A)]


def test_several_series_of_one_label_set_make_one_card_with_the_extreme_value(store):
    # given
    source = Source()
    checks = checks_with(store, source)
    added(checks, op='<', threshold=10, title='{instance} {pod}')
    source.set(Q, [({'instance': A, 'pod': 'p1'}, 8), ({'instance': A, 'pod': 'p2'}, 3), ({'instance': A, 'pod': 'p3'}, 50)])

    # when
    sweep(checks, source)

    # then
    card = cards(store)[key('redis-memory', A)]
    assert (card['now'], card['label']) == (3, f'{A} p2')


def test_a_renamed_check_keeps_its_open_card_and_updates_the_text(store):
    # given
    source = Source()
    checks = checks_with(store, source)
    added(checks)
    source.set(Q, [({'instance': A}, 95)])
    sweep(checks, source)
    first = cards(store)[key('redis-memory', A)]['firstSeen']

    # when
    checks.defs.update('redis-memory', clean(check(name='Cache memory', title='{instance} is full')), 'admin@example.com')
    sweep(checks, source, NOW + 300)

    # then
    card = cards(store)[key('redis-memory', A)]
    assert (card['svc'], card['label'], card['firstSeen']) == ('Cache memory', f'{A} is full', first)


def test_a_disabled_or_deleted_check_clears_its_cards(store):
    # given
    source = Source()
    checks = checks_with(store, source)
    added(checks)
    added(checks, id='other', name='Other')
    source.set(Q, [({'instance': A}, 95)])
    sweep(checks, source)

    # when
    checks.defs.update('redis-memory', clean(check(enabled=False)), 'admin@example.com')
    checks.defs.delete('other')
    result = sweep(checks, source, NOW + 300)

    # then
    assert cards(store) == {} and set(cards(store, 'cleared')) == {key('redis-memory', A), key('other', A)}
    assert set(result.ran) == {f'{PASS}:redis-memory', f'{PASS}:other'}
    assert checks.defs.state('redis-memory')['state'] == 'off'


def test_clear_closes_the_cards_of_one_check_at_once_and_keeps_the_pass_status(store):
    # given
    source = Source()
    checks = checks_with(store, source)
    added(checks)
    added(checks, id='other', name='Other')
    source.set(Q, [({'instance': A}, 95)])
    sweep(checks, source)

    # when
    checks.clear('redis-memory', NOW + 10)

    # then
    assert list(cards(store)) == [key('other', A)]
    assert store.load()['sources'][PASS] == 'ok'


def test_a_huge_label_value_is_cut_and_the_card_key_stays_short(store):
    # given
    source = Source()
    checks = checks_with(store, source)
    added(checks)
    source.set(Q, [({'instance': '<b>' + 'x' * 10000}, 95)])

    # when
    sweep(checks, source)

    # then
    (k, card), = cards(store).items()
    assert len(k) < 80 and len(card['label']) <= promqlchecks.TITLE_MAX and len(card['what']) <= promqlchecks.TEXT_MAX + 200
    assert len(card['checkLabels']['instance']) <= promqlchecks.LABEL_MAX


def test_a_scalar_result_is_one_card_without_labels(store):
    # given
    source = Source()
    checks = checks_with(store, source)
    added(checks, query='scalar(sum(up))', op='<', threshold=3, by=[], title='')
    source.set('scalar(sum(up))', {'status': 'success', 'data': {'resultType': 'scalar', 'result': [NOW, '2']}})

    # when
    sweep(checks, source)

    # then
    card = cards(store)[key('redis-memory')]
    assert (card['now'], card['svc'], card['label']) == (2, 'Redis memory', 'Redis memory')


def test_a_grafana_datasource_is_looked_up_once_per_pass_and_a_renamed_one_fails_the_check(store):
    # given
    source = Source()
    checks = checks_with(store, source)
    added(checks, source='grafana', instance='ops', datasource='cloud-metrics')
    added(checks, id='by-uid', name='By uid', source='grafana', instance='ops', datasource='ds-1')
    source.set(Q, [({'instance': A}, 95)])
    sweep(checks, source)
    opened = set(cards(store))

    # when
    source.listed = [{'uid': 'ds-1', 'name': 'renamed-metrics', 'type': 'prometheus'}]
    result = sweep(checks, source, NOW + 300)

    # then
    assert opened == {key('redis-memory', A), key('by-uid', A)}
    assert source.lists == 2
    assert {a['path'] for a in source.asked} == {'/api/datasources/proxy/uid/ds-1/api/v1/query'}
    assert result.ran == (f'{PASS}:by-uid',)
    assert checks.defs.state('redis-memory')['reason'] == 'no Grafana datasource named cloud-metrics'
    assert set(cards(store)) == opened
    link = cards(store)[key('by-uid', A)]['links'][0]
    assert link['label'] == 'Query in Grafana' and link['url'].startswith(f'{GRAFANA}/explore?')


def test_preview_shows_the_cards_it_would_make_under_the_same_bounds_and_saves_nothing(store):
    # given
    source = Source()
    checks = checks_with(store, source)
    source.set(Q, [({'instance': f'cache-{n:04d}'}, 95) for n in range(CARDS_MAX + 5)])
    before = store.load()

    # when
    got = checks.preview(check(**{'for': 4}), NOW)

    # then
    assert got['ok'] and got['for'] == 4 and got['series'] == CARDS_MAX + 5 and got['firing'] == CARDS_MAX + 5
    assert len(got['cards']) == CARDS_MAX and got['cards'][-1]['more'] == 6
    first = got['cards'][0]
    assert set(first) == {'key', 'sev', 'title', 'text', 'value', 'd1', 'd7', 'labels', 'links', 'more'}
    assert checks.defs.list() == [] and checks.defs.states() == {} and store.load() == before


def test_preview_of_a_failing_query_says_why_and_of_a_bad_definition_refuses(store):
    # given
    source = Source()
    checks = checks_with(store, source)
    source.set(Q, Unreachable('down'))

    # when
    failing = checks.preview(check(), NOW)
    with pytest.raises(Invalid):
        checks.preview(check(op='~'), NOW)

    # then
    assert failing == {'ok': False, 'reason': 'Prometheus main is unreachable', 'cards': [], 'series': 0, 'read': 0, 'skipped': 0, 'noBaseline': 0, 'firing': 0, 'for': 1, 'warnings': []}


def test_the_job_is_on_while_a_check_or_one_of_its_cards_exists(store):
    # given
    source = Source()
    checks = checks_with(store, source)

    # when
    idle = checks.enabled()
    added(checks)
    on = checks.enabled()

    # then
    assert (idle, on) == (False, True)


def test_the_core_pass_name_is_reserved_so_no_plugin_can_claim_its_cards():
    # given
    claimer = dataclasses.replace(demo_plugin.PLUGIN, checks=(Check('claim', 'Claim', 900, 0, (PASS,), demo_plugin.pulse),))

    # when
    found = Registry.of(claimer).loaded()[0]

    # then
    assert PASS == 'custom-checks' and PASS in CORE_RAN
    assert found.state == 'broken'
    assert found.reason.endswith(messages.msg('plugins.why.ran_taken', key=PASS, owner='core')), found.reason


def test_an_unexpected_error_inside_one_check_fails_only_that_check(store, monkeypatch):
    # given
    source = Source()
    checks = checks_with(store, source)
    added(checks)
    added(checks, id='other', name='Other', title='', text='')
    source.set(Q, [({'instance': A}, 95)])

    def broken(template, labels, value, limit=promqlchecks.TITLE_MAX):
        raise KeyError(template)

    monkeypatch.setattr(promqlchecks, 'render', broken)

    # when
    result = sweep(checks, source)

    # then
    assert result.ran == (f'{PASS}:other',) and list(cards(store)) == [key('other', A)]
    assert checks.defs.state('redis-memory')['reason'] == 'the check failed (KeyError)'


def test_deleting_the_last_failing_check_leaves_an_ok_pass_status(store):
    # given
    source = Source()
    checks = checks_with(store, source)
    added(checks)
    source.set(Q, Unreachable('down'))
    sweep(checks, source)
    failing = store.load()['sources'][PASS]

    # when
    checks.defs.delete('redis-memory')
    checks.clear('redis-memory', NOW + 10)

    # then
    assert failing == 'ok \u00b7 1 of 1 custom checks failing: Redis memory'
    assert store.load()['sources'][PASS] == 'ok'


def test_editing_a_check_with_for_keeps_its_open_cards(store):
    # given
    source = Source()
    checks = checks_with(store, source)
    added(checks, **{'for': 2})
    source.set(Q, [({'instance': A}, 95)])
    sweep(checks, source)
    sweep(checks, source, NOW + 300)
    opened = cards(store)

    # when
    checks.defs.update('redis-memory', clean(check(name='Cache memory', threshold=80, **{'for': 2})), 'admin@example.com')
    sweep(checks, source, NOW + 600)

    # then
    assert list(opened) == [key('redis-memory', A)]
    assert list(cards(store)) == [key('redis-memory', A)] and cards(store, 'cleared') == {}
    assert cards(store)[key('redis-memory', A)]['firstSeen'] == opened[key('redis-memory', A)]['firstSeen']


@pytest.mark.parametrize('change', ['delete', 'disable'])
def test_a_check_deleted_during_a_pass_does_not_reopen_its_cards(store, change):
    # given
    source = Source()
    checks = checks_with(store, source)
    added(checks)
    source.set(Q, [({'instance': A}, 95)])
    sweep(checks, source)

    def meanwhile():
        if change == 'delete':
            checks.defs.delete('redis-memory')
        else:
            checks.defs.update('redis-memory', clean(check(enabled=False)), 'admin@example.com')
        checks.clear('redis-memory', NOW + 200)
        return [({'instance': A}, 95)]

    source.set(Q, meanwhile)

    # when
    result = sweep(checks, source, NOW + 300)

    # then
    assert cards(store) == {} and result.found == []
    assert checks.defs.state('redis-memory') == (None if change == 'delete' else {'state': 'off', 'at': engine.iso(NOW + 300), 'pending': {}})


def test_a_timing_out_source_does_not_starve_checks_on_other_sources(store, monkeypatch):
    # given
    source = Source()
    checks = checks_with(store, source, monkeypatch)
    for n in range(13):
        added(checks, id=f'slow-{n:02d}', name=f'Slow {n}')
    added(checks, id='zz-ok', name='Fine', source='grafana', instance='ops', datasource='cloud-metrics', query='up == 0', op='==', threshold=0, by=['job'])

    def slow():
        source.clock += promqlchecks.QUERY_TIME_S
        return TimedOut('slow')

    source.set(Q, slow)
    source.set('up == 0', [({'job': 'redis'}, 0)])

    # when
    sweep(checks, source)

    # then
    assert checks.defs.state('zz-ok')['state'] == 'ok' and list(cards(store)) == [key('zz-ok', 'redis')]
    assert checks.defs.state('slow-00')['reason'] == f'the queries took longer than {promqlchecks.QUERY_TIME_S} s'
    assert checks.defs.state('slow-01')['reason'] == 'not run: prometheus main did not answer earlier in this pass'
    assert len([a for a in source.asked if a['query'] == Q]) == 1


def test_checks_that_waited_longest_run_first(store, monkeypatch):
    # given
    source = Source()
    checks = checks_with(store, source, monkeypatch)
    added(checks, id='aa-first', name='First')
    added(checks, id='bb-second', name='Second', query='up')
    source.set(Q, [({'instance': A}, 95)])
    source.took = promqlchecks.PASS_TIME_S
    sweep(checks, source)

    # when
    source.asked.clear()
    sweep(checks, source, NOW + 300)

    # then
    assert [a['query'] for a in source.asked] == ['up']
    assert checks.defs.state('aa-first')['reason'] == 'not run: the pass ran out of time'


def test_custom_check_card_and_status_texts_come_from_the_catalog(store, monkeypatch):
    # given
    for k in ('fact', 'short', 'link_prometheus', 'failing', 'more', 'more_label', 'more_short'):
        monkeypatch.setitem(messages.MESSAGES, f'custom_checks.{k}', f'<{k}>')
    source = Source()
    checks = checks_with(store, source)
    added(checks, text='', by=['instance'])
    added(checks, id='broken', name='Broken', query='up')
    source.set(Q, [({'instance': f'c{n:02d}'}, 95) for n in range(promqlchecks.CARDS_MAX + 1)])
    source.set('up', HttpError(400, PROM_URL))

    # when
    result = sweep(checks, source)
    found = cards(store)

    # then
    assert result.status == '<failing>'
    card = found[key('redis-memory', 'c00')]
    assert (card['what'], card['short'], card['links'][0]['label']) == ('<fact>', '<short>', '<link_prometheus>')
    more = found[key('redis-memory') + '|more']
    assert (more['what'], more['label'], more['short']) == ('<more>', '<more_label>', '<more_short>')
    assert {k.split('.', 1)[0] for k in messages.MESSAGES if k.startswith('custom_checks.')} == {'custom_checks'}
    assert len([k for k in messages.MESSAGES if k.startswith('custom_checks.')]) >= 15


@pytest.mark.parametrize('over', [{'baselines': [[1]]}, {'baselines': [{}]}, {'baselines': '1d'}, {'for': '\u00b2'}, {'for': 2.5}, {'by': [1]}, {'by': 'instance'},
                                  {'op': ['>']}, {'source': ['prometheus']}, {'severity': {}}, {'baselineMode': []}, {'threshold': [1]}, {'name': 5}, {'id': ['x']}])
def test_a_field_of_the_wrong_type_is_refused_not_a_crash(over):
    # when
    with pytest.raises(Invalid):
        clean(check(**over))


@pytest.mark.parametrize('name, said', [('!!!', 'set an id'), ('5xx rate', None), ('a', 'set an id')])
def test_an_id_made_from_the_name_is_usable_or_the_error_asks_for_one(name, said):
    # when
    if said is None:
        got = clean(check(name=name) | {'id': None})['id']
    else:
        with pytest.raises(Invalid) as e:
            clean(check(name=name) | {'id': None})

    # then
    if said is None:
        assert got == 'check-5xx-rate'
    else:
        assert said in e.value.text


@pytest.mark.parametrize('over', [{}, {'source': 'grafana', 'instance': 'ops', 'datasource': 'cloud-metrics'}])
def test_both_sources_cap_the_answer_size(store, over):
    # given
    source = Source()
    checks = checks_with(store, source)
    added(checks, **over)

    # when
    sweep(checks, source)

    # then
    assert [a['read'] for a in source.asked] == [str(promqlchecks.BODY_MAX)]


def test_a_slow_grafana_query_hits_the_time_limit_and_says_so(store, monkeypatch):
    # given
    source = Source()
    checks = checks_with(store, source, monkeypatch)
    added(checks, source='grafana', instance='ops', datasource='cloud-metrics', baselines=['1d'])
    added(checks, id='timed-out', name='Timed out', source='grafana', instance='ops', datasource='cloud-metrics', query='up')
    source.set(Q, [({'instance': A}, 95)])
    source.took = promqlchecks.QUERY_TIME_S / 2 + 1

    # when
    sweep(checks, source)
    slow = checks.defs.state('redis-memory')['reason']
    source.took = 0
    source.set('up', TimedOut('slow'))
    sweep(checks, source, NOW + 300)

    # then
    assert slow == f'the queries took longer than {promqlchecks.QUERY_TIME_S} s'
    assert checks.defs.state('timed-out')['reason'] == f'the queries took longer than {promqlchecks.QUERY_TIME_S} s'


def test_preview_obeys_the_time_limit(store, monkeypatch):
    # given
    source = Source()
    checks = checks_with(store, source, monkeypatch)
    source.set(Q, [({'instance': A}, 95)])
    source.took = promqlchecks.QUERY_TIME_S + 1

    # when
    got = checks.preview(check(baselines=['1w']), NOW)

    # then
    assert got['ok'] is False and got['reason'] == f'the queries took longer than {promqlchecks.QUERY_TIME_S} s'


def test_a_refused_grafana_read_names_the_right_the_token_lacks(store):
    # given
    source = Source()
    checks = checks_with(store, source)
    added(checks, source='grafana', instance='ops', datasource='cloud-metrics')
    source.listed = HttpError(403, GRAFANA)

    # when
    sweep(checks, source)

    # then
    assert checks.defs.state('redis-memory')['reason'] == 'Grafana datasources: HTTP 403 (the token needs datasources:read)'


def test_open_cards_keep_their_slots_under_the_card_cap(store):
    # given
    source = Source()
    checks = checks_with(store, source)
    added(checks)
    first = [({'instance': f'a{n:02d}'}, 95) for n in range(promqlchecks.CARDS_MAX - 1)]
    source.set(Q, first)
    sweep(checks, source)
    kept = set(cards(store))

    # when
    source.set(Q, first + [({'instance': f'b{n:02d}'}, 99) for n in range(5)])
    sweep(checks, source, NOW + 300)

    # then
    assert kept <= set(cards(store)) and cards(store, 'cleared') == {}
    assert cards(store)[key('redis-memory') + '|more']['now'] == 5


def test_an_extra_pass_soon_after_does_not_count_towards_for(store):
    # given
    source = Source()
    checks = checks_with(store, source)
    added(checks, **{'for': 2})
    source.set(Q, [({'instance': A}, 95)])

    # when
    sweep(checks, source)
    sweep(checks, source, NOW + 5)
    early = bool(cards(store))
    sweep(checks, source, NOW + 300)

    # then
    assert (early, bool(cards(store))) == (False, True)


def test_groups_without_a_baseline_value_are_counted_in_the_state_and_the_preview(store):
    # given
    source = Source()
    checks = checks_with(store, source)
    added(checks, baselines=['1d'], threshold=2)
    source.set(Q, [({'instance': A}, 30), ({'instance': B}, 30)])
    source.set(Q, [({'instance': A}, 10)], back=86400)

    # when
    sweep(checks, source)
    shown = checks.preview(check(baselines=['1d'], threshold=2), NOW)

    # then
    assert checks.defs.state('redis-memory')['noBaseline'] == 1 and shown['noBaseline'] == 1


def test_a_create_past_the_cap_leaves_no_extra_check(store, monkeypatch):
    # given
    monkeypatch.setattr(promqlchecks, 'CHECKS_MAX', 2)
    defs = promqlchecks.Definitions(store.db)
    defs.create(clean(check(id='one')), 'a')
    defs.create(clean(check(id='two')), 'a')

    # when
    with pytest.raises(promqlchecks.Full):
        defs.create(clean(check(id='three')), 'a')

    # then
    assert [d['id'] for d in defs.list()] == ['one', 'two']


def test_a_check_edited_during_a_pass_does_not_open_cards_of_the_old_definition(store):
    # given
    source = Source()
    checks = checks_with(store, source)
    added(checks)

    def meanwhile():
        checks.defs.update('redis-memory', clean(check(threshold=99)), 'admin@example.com')
        return [({'instance': A}, 95)]

    source.set(Q, meanwhile)

    # when
    result = sweep(checks, source)
    during = (cards(store), checks.defs.state('redis-memory'))
    source.set(Q, [({'instance': A}, 95)])
    sweep(checks, source, NOW + 300)

    # then
    assert during == ({}, {'pending': {}}) and result.found == [] and f'{PASS}:redis-memory' not in result.ran
    assert cards(store) == {}


def test_an_edit_of_the_texts_only_during_a_pass_still_applies_its_cards(store, monkeypatch):
    # given
    source = Source()
    checks = checks_with(store, source)
    added(checks)

    def meanwhile():
        monkeypatch.setattr(promqlchecks.time, 'time', lambda: NOW + 3600)
        checks.defs.update('redis-memory', clean(check(title='{instance} is full', severity='crit')), 'admin@example.com')
        return [({'instance': A}, 95)]

    source.set(Q, meanwhile)

    # when
    sweep(checks, source)

    # then
    assert checks.defs.get('redis-memory')['updatedAt'] == engine.iso(NOW + 3600)
    assert list(cards(store)) == [key('redis-memory', A)] and checks.defs.state('redis-memory')['state'] == 'ok'


def test_a_failed_pass_starts_the_for_count_again(store):
    # given
    source = Source()
    checks = checks_with(store, source)
    added(checks, **{'for': 3})

    # when
    source.set(Q, [({'instance': A}, 95)])
    sweep(checks, source)
    source.set(Q, Unreachable('down'))
    sweep(checks, source, NOW + 300)
    failed = checks.defs.state('redis-memory')['pending']
    source.set(Q, [({'instance': A}, 95)])
    sweep(checks, source, NOW + 600)

    # then
    assert failed == {} and cards(store) == {}


def test_a_create_that_loses_the_count_race_is_taken_back(store, monkeypatch):
    # given
    defs = promqlchecks.Definitions(store.db)
    counts = iter([0, promqlchecks.CHECKS_MAX + 1])
    monkeypatch.setattr(defs.col, 'count_documents', lambda query: next(counts))

    # when
    with pytest.raises(promqlchecks.Full):
        defs.create(clean(check()), 'admin@example.com')

    # then
    assert store.db['custom_checks'].find_one({}) is None


def test_a_start_or_end_inside_an_esql_string_is_not_a_time_parameter():
    # given
    quoted = 'FROM logs | WHERE url LIKE "*?start=1*" OR message == """?end""" | STATS n = COUNT(*)'
    real = 'FROM logs | WHERE @timestamp >= TO_DATETIME(?start) AND url LIKE "*?end*" | STATS n = COUNT(*)'

    # when
    found = promqlchecks.time_params(quoted), promqlchecks.time_params(real)

    # then
    assert found == (set(), {'start'})
