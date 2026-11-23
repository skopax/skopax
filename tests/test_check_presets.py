import json
import logging
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml
from pymongo.errors import PyMongoError

from skopax import checkpresets, promqlchecks
from skopax.checkpresets import PresetError, add, load, presets
from skopax.promqlchecks import Definitions, Exists, Full, Invalid, clean, evaluate
from tests import presets_promtool
from tests.presets_promtool import CB, RD, couchbase_cases, couchbase_expected, redis_cases, redis_expected

ROOT = Path(__file__).resolve().parent.parent
NOW = 1_792_224_000.0
COUCHBASE = {'instance': 'main', 'cluster': 'orders-prod', 'selector': 'job=~".*couchbase.*",env="prod"'}
REDIS = {'instance': 'ops', 'datasource': 'cloud-metrics', 'name': 'cache-prod'}
BY = 'admin@example.com'


def write(folder: Path, preset: dict) -> None:
    (folder / f"{preset['id']}.json").write_text(json.dumps(preset))


def shipped(preset_id: str) -> dict:
    return json.loads((ROOT / 'skopax' / 'presets' / f'{preset_id}.json').read_text())


def test_every_shipped_preset_loads_and_its_checks_pass_clean_with_the_sample_placeholders():
    # when
    found = load()

    # then
    assert sorted(found) == ['couchbase', 'csp', 'managed-redis']
    for preset in found.values():
        samples = {p['key']: p['example'] for p in preset.placeholders}
        values, checks = preset.fill(samples)
        assert values == {p['key']: p['example'] for p in preset.placeholders}
        assert checks and all(clean(c) == c for c in checks)
        assert len({c['id'] for c in checks}) == len(checks) <= promqlchecks.CHECKS_MAX
        assert all(c['source'] == preset.source and c['instance'] == samples['instance'] for c in checks)
        assert all('${' not in json.dumps(c) for c in checks)
    assert presets() == found


def test_the_longest_allowed_placeholder_values_still_fit_every_check():
    # given
    for preset in load().values():
        longest = {}
        for p in preset.placeholders:
            most = p['max']
            longest[p['key']] = {'matchers': 'a="' + 'x' * (most - 4) + '"', 'prefix': 'p' * most, 'label': 'l' * most, 'name': 'n' * most,
                                 'instance': 'i' * most, 'datasource': 'd' * most, 'index': 'x' * most, 'condition': 'x' * most}[p['kind']]

        # when
        values, checks = preset.fill(longest)

        # then
        assert all(len(c['query']) <= promqlchecks.QUERY_MAX for c in checks)


def test_a_preset_is_listed_with_what_it_needs_and_its_placeholders():
    # when
    view = presets()['couchbase'].view()

    # then
    assert (view['id'], view['name'], view['source']) == ('couchbase', 'Couchbase', 'prometheus')
    assert view['summary'] and 'Couchbase exporter' in view['needs']
    assert [(p['key'], p['kind']) for p in view['placeholders']] == [('instance', 'instance'), ('cluster', 'name'), ('selector', 'matchers')]
    assert view['placeholders'][2]['default'] == 'job=~".*couchbase.*"'
    assert [c['key'] for c in view['checks']][:3] == ['down', 'not-scraped', 'restart']
    assert view['checks'][0] == {'key': 'down', 'name': 'Couchbase <cluster> node down', 'severity': 'crit'}


def test_defaults_fill_the_placeholders_left_empty_and_an_optional_selector_may_stay_empty():
    # when
    values, checks = presets()['managed-redis'].fill(REDIS)
    _, cluster = presets()['couchbase'].fill({'instance': 'main', 'cluster': 'orders-prod', 'selector': ' '})

    # then
    assert values == REDIS | {'selector': '', 'prefix': 'redis_', 'label': 'instance'}
    down = next(c for c in checks if c['id'] == 'redis-cache-prod-down')
    assert (down['query'], down['by'], down['datasource'], down['title']) == ('max by (instance) (redis_up{})', ['instance'], 'cloud-metrics', '{instance} down')
    assert 'up{job=~".*couchbase.*"}' in cluster[0]['query']


@pytest.mark.parametrize('selector', ['job="a"}', 'job="a"} or up{x="1"', 'job="a"} or vector(1)', 'job="a" or up', 'job="a",}', '}', 'job', 'job=couchbase', 'job="a',
                                      'job="a",', ',job="a"', 'job="a" env="b"', 'job="a\nb"', 'job="a",\nenv="b"', '9job="a"', 'job=="a"', 'x="' + 'y' * 300 + '"', '{job="a"}'])
def test_a_selector_that_could_break_out_of_the_braces_is_refused(selector):
    # when
    with pytest.raises(Invalid) as e:
        presets()['couchbase'].fill(COUCHBASE | {'selector': selector})

    # then
    assert 'Selector' in e.value.text


@pytest.mark.parametrize('over, said', [
    ({'prefix': 'redis_up or vector(1) or redis_'}, 'Metric prefix'),
    ({'prefix': 'redis_{x="1"}'}, 'Metric prefix'),
    ({'label': 'instance) or (up'}, 'Instance label'),
    ({'label': 'in-stance'}, 'Instance label'),
    ({'label': 'value'}, 'Instance label'),
    ({'label': 'base'}, 'Instance label'),
    ({'label': '__name__'}, 'Instance label'),
    ({'name': 'Cache Prod'}, 'Name'),
    ({'name': 'x' * 21}, 'Name'),
    ({'name': '{instance}'}, 'Name'),
    ({'name': ''}, 'Name'),
    ({'instance': ''}, 'Grafana'),
    ({'instance': 'ops\nother'}, 'Grafana'),
    ({'datasource': 7}, 'Datasource'),
    ({'selector': 'env="prod"} or up{'}, 'Selector'),
    ({'unknown': 'x'}, 'unknown'),
])
def test_a_placeholder_value_is_checked_by_its_kind(over, said):
    # when
    with pytest.raises(Invalid) as e:
        presets()['managed-redis'].fill(REDIS | over)

    # then
    assert said in e.value.text


def test_the_values_must_be_an_object_and_a_value_is_filled_only_once():
    # given
    preset = presets()['couchbase']

    # when
    with pytest.raises(Invalid):
        preset.fill(['main'])
    _, checks = preset.fill(COUCHBASE | {'selector': 'job="${cluster}"'})

    # then
    assert 'up{job="${cluster}"}' in checks[0]['query'], 'a value is never filled a second time'


def test_placeholders_in_quoted_matcher_values_stay_inside_the_quotes():
    # when
    _, checks = presets()['couchbase'].fill(COUCHBASE | {'selector': 'job="a} or up{x=\\"1\\""'})

    # then
    assert checks[0]['query'] == 'max by (instance) (up{job="a} or up{x=\\"1\\""})'


@pytest.mark.parametrize('change, said', [
    (lambda p: p.update(id='Bad Id'), 'id'),
    (lambda p: p.update(source='influx'), 'source'),
    (lambda p: p.update(extra=1), 'extra'),
    (lambda p: p['checks'][0].update(query='up{${nothing}}'), 'nothing'),
    (lambda p: p['checks'][0].update(instance='main'), 'instance'),
    (lambda p: p['checks'][0].update(text='node ${selector}'), 'selector'),
    (lambda p: p['checks'][0].update(op='~'), 'comparison'),
    (lambda p: p['checks'][1].update(key='down'), 'down'),
    (lambda p: p['checks'][1].update(id=p['checks'][0]['id']), 'id'),
    (lambda p: p['placeholders'].append({'key': 'other', 'kind': 'name', 'label': 'Other', 'help': '', 'example': 'x'}), 'name'),
    (lambda p: p['placeholders'][2].update(kind='expression'), 'kind'),
    (lambda p: p['placeholders'][2].update(max=300), 'query'),
    (lambda p: p['placeholders'][2].update(max=1000), 'max'),
    (lambda p: p['placeholders'][2].update(example='job="a"} or up'), 'Selector'),
    (lambda p: p.update(checks=[]), 'checks'),
    (lambda p: p['placeholders'][2].update(default='job="a"} or up'), 'default'),
    (lambda p: p['placeholders'][1].update(default='Bad Name'), 'default'),
])
def test_a_preset_that_breaks_the_rules_does_not_load(tmp_path, change, said):
    # given
    preset = shipped('couchbase')
    change(preset)
    write(tmp_path, preset)

    # when
    with pytest.raises(PresetError) as e:
        load(tmp_path)

    # then
    assert said in str(e.value)


def test_a_preset_file_must_be_named_after_its_id(tmp_path):
    # given
    (tmp_path / 'other.json').write_text(json.dumps(shipped('couchbase')))

    # when
    with pytest.raises(PresetError) as e:
        load(tmp_path)

    # then
    assert 'other' in str(e.value)


def test_adding_a_preset_makes_normal_checks_tagged_with_the_preset_that_stay_editable(store):
    # given
    defs = Definitions(store.db)
    preset = presets()['couchbase']

    # when
    made = add(defs, preset, COUCHBASE, BY)
    first = made[0]
    edited = defs.update(first['id'], clean({**first, 'threshold': 0.9, 'name': 'Renamed'}), 'bob@example.com')
    defs.delete(made[1]['id'])

    # then
    assert [d['id'] for d in made] == [f"cb-orders-prod-{c['key']}" for c in preset.checks]
    assert all((d['preset'], d['presetValues'], d['createdBy']) == ('couchbase', COUCHBASE, BY) for d in made)
    assert (edited['preset'], edited['presetValues'], edited['threshold'], edited['name']) == ('couchbase', COUCHBASE, 0.9, 'Renamed')
    assert defs.get(first['id'])['preset'] == 'couchbase'
    assert len(defs.list()) == len(made) - 1


def test_a_check_made_by_hand_has_no_preset_tag(store):
    # given
    defs = Definitions(store.db)

    # when
    made = defs.create(clean({'name': 'Up', 'source': 'prometheus', 'instance': 'main', 'query': 'up', 'op': '<', 'threshold': 1}), BY)

    # then
    assert (made['preset'], made['presetValues']) == (None, None)


def test_the_same_preset_with_the_same_values_is_refused_naming_its_checks_and_other_values_are_added(store):
    # given
    defs = Definitions(store.db)
    preset = presets()['couchbase']
    first = add(defs, preset, COUCHBASE, BY)
    defs.delete(first[0]['id'])

    # when
    with pytest.raises(Exists) as again:
        add(defs, preset, {**COUCHBASE, 'selector': ' job=~".*couchbase.*",env="prod" '}, BY)
    other = add(defs, preset, COUCHBASE | {'cluster': 'search-prod', 'selector': 'job="search"'}, BY)

    # then
    assert 'Couchbase orders-prod node not scraped' in again.value.text and 'Couchbase orders-prod node down' not in again.value.text
    assert len(other) == len(first) and len(defs.list()) == 2 * len(first) - 1


def test_an_id_taken_by_another_check_refuses_the_whole_preset(store):
    # given
    defs = Definitions(store.db)
    defs.create(clean({'id': 'cb-orders-prod-restart', 'name': 'Mine', 'source': 'prometheus', 'instance': 'main', 'query': 'up', 'op': '<', 'threshold': 1}), BY)

    # when
    with pytest.raises(Exists) as e:
        add(defs, presets()['couchbase'], COUCHBASE | {'selector': 'job="other"'}, BY)

    # then
    assert 'cb-orders-prod-restart' in e.value.text
    assert [d['id'] for d in defs.list()] == ['cb-orders-prod-restart']


def test_the_cap_refuses_a_whole_preset_that_would_not_fit_and_leaves_no_partial_checks(store, monkeypatch):
    # given
    preset = presets()['couchbase']
    monkeypatch.setattr(promqlchecks, 'CHECKS_MAX', len(preset.checks) + 1)
    defs = Definitions(store.db)
    for n in range(2):
        defs.create(clean({'id': f'mine-{n}', 'name': f'Mine {n}', 'source': 'prometheus', 'instance': 'main', 'query': 'up', 'op': '<', 'threshold': 1}), BY)

    # when
    with pytest.raises(Full) as e:
        add(defs, preset, COUCHBASE, BY)

    # then
    assert f'{len(preset.checks)} checks' in e.value.text and 'at most' in e.value.text
    assert [d['id'] for d in defs.list()] == ['mine-0', 'mine-1']


def test_a_failure_half_way_takes_back_the_checks_this_add_made(store, monkeypatch):
    # given
    defs = Definitions(store.db)
    real = defs.create
    calls = []

    def flaky(d, by, tags=None):
        calls.append(d['id'])
        if len(calls) == 3:
            raise Full('at most 50 custom checks can be set up')
        return real(d, by, tags)

    monkeypatch.setattr(defs, 'create', flaky)

    # when
    with pytest.raises(Full):
        add(defs, presets()['couchbase'], COUCHBASE, BY)

    # then
    assert len(calls) == 3 and defs.list() == []


def ask(answers: dict):
    class Asker(promqlchecks.Asker):
        def data(self, expr, at):
            assert at == NOW
            rows = answers.get(expr, [])
            return {'resultType': 'vector', 'result': [{'metric': m, 'value': [at, str(v)]} for m, v in rows]}

    return Asker()


def fired(checks: list[dict], answers: dict, subject: str, kinds: dict) -> list[tuple[str, str, str]]:
    out = []
    for d in checks:
        for g in evaluate(d, ask(answers), NOW).firing():
            out.append((g.labels.get(d['by'][0], '') if d['by'] else subject, kinds[d['id'].rsplit('-' + subject + '-', 1)[-1]], d['severity']))
    return sorted(out)


def per(data: dict, k: str, i: int) -> dict:
    return {m.get('instance', ''): v for m, v in data[(k, i)]}


def one(data: dict, k: str, i: int):
    rows = data[(k, i)]
    return rows[0][1] if rows else None


def rows(values: dict) -> list:
    return [({'instance': n}, v) for n, v in values.items() if v is not None]


def scalar(v) -> list:
    return [] if v is None else [({}, v)]


def ratio(a, b):
    if a is None or b is None:
        return None
    return a / b if b else (float('nan') if a == 0 else float('inf'))


def baseline(*values):
    got = [v for v in values if v is not None]
    return max(got) if got else None


def live(data: dict, values: dict) -> dict:
    up = per(data, 'up', 0)
    return {n: v for n, v in values.items() if up.get(n, 0) >= 0.5}


def couchbase_answers(data: dict, checks: list[dict]) -> dict:
    r = CB.RULES
    up = per(data, 'up', 0)
    req = [one(data, 'req', i) for i in range(3)]
    err = [one(data, 'errors', i) for i in range(3)]
    rtime = [one(data, 'rtime', i) for i in range(3)]
    tmo = [one(data, 'timeouts', i) for i in range(3)]
    share = [None if err[i] is None or not req[i] else 100 * err[i] / req[i] for i in range(3)]
    avg = [None if rtime[i] is None or not req[i] else rtime[i] / req[i] / 1e6 for i in range(3)]
    sb, ab, tb = baseline(*share[1:]), baseline(*avg[1:]), baseline(*tmo[1:])
    share_limit = r['err_share'] if sb is None else max(r['err_x'] * sb, sb + r['err_pp'])
    avg_limit = r['lat_floor'] if ab is None else max(r['lat_x'] * ab, ab + r['lat_ms'])
    conn = [per(data, 'conn', i) for i in range(3)]
    jumps = {}
    for n, c in conn[0].items():
        b = baseline(conn[1].get(n), conn[2].get(n))
        if b is not None and c > max(r['jump'] * b, b + r['jump_slack']):
            jumps[n] = c
    iquota = per(data, 'iquota', 0)
    imem = {n: ratio(100 * v, iquota.get(n)) for n, v in per(data, 'imem', 0).items()}
    stuck, running = one(data, 'stuck', 0), one(data, 'rebalance', 0)
    answer = {
        'down': rows(up),
        'not-scraped': rows({n: v for n, v in per(data, 'up', 1).items() if n not in up}) if up else [],
        'restart': rows(live(data, per(data, 'uptime', 0))),
        'conn-jump': rows(live(data, jumps)),
        'index-memory': rows(live(data, imem)),
        'failover': scalar(one(data, 'failover', 0)),
        'rebalance-stuck': scalar(stuck),
        'rebalance': [] if stuck else scalar(running),
        'n1ql-errors': scalar(share[0] if share[0] is not None and share[0] > share_limit and (err[0] or 0) >= r['err_min'] else None),
        'n1ql-timeouts': scalar(None if tmo[0] is None or (tb is not None and tmo[0] <= r['tmo_x'] * tb) else tmo[0]),
        'n1ql-latency': scalar(avg[0] if avg[0] is not None and avg[0] > avg_limit and (req[0] or 0) >= r['lat_req'] else None),
        'n1ql-queue': scalar(one(data, 'queue', 0)),
        'auth-fail': scalar(one(data, 'auth', 0)),
    }
    answer['n1ql-errors-crit'] = answer['n1ql-errors']
    answer['no-series'] = [] if up else scalar(1)
    return {c['query']: answer[c['id'].removeprefix('cb-orders-prod-')] for c in checks}


CB_KINDS = {'down': 'down', 'not-scraped': 'down', 'restart': 'restart', 'conn-jump': 'conn-jump', 'index-memory': 'index-memory', 'failover': 'failover',
            'rebalance-stuck': 'rebalance', 'rebalance': 'rebalance', 'n1ql-errors': 'n1ql-errors', 'n1ql-errors-crit': 'n1ql-errors', 'n1ql-timeouts': 'n1ql-timeouts',
            'n1ql-latency': 'n1ql-latency', 'n1ql-queue': 'n1ql-queue', 'auth-fail': 'auth-fail', 'no-series': 'no-series'}


@pytest.mark.parametrize('name, data', couchbase_cases(), ids=[c[0] for c in couchbase_cases()])
def test_the_couchbase_preset_gives_the_cards_the_example_plugin_gives_on_its_own_test_data(name, data):
    # given
    _, checks = presets()['couchbase'].fill({'instance': 'main', 'cluster': CB.CLUSTER, 'selector': CB.SELECTOR})
    example = couchbase_expected(data)

    # when
    got = fired(checks, couchbase_answers(data, checks), CB.CLUSTER, CB_KINDS)

    # then
    assert got == example


def redis_answers(data: dict, checks: list[dict]) -> dict:
    def of(k: str) -> dict:
        return {m.get('instance', ''): v for m, v in data.get(k, [])}

    up = of('up')

    def alive(values: dict) -> list:
        return rows({n: v for n, v in values.items() if up.get(n, 0) >= 0.5})

    memory = {n: ratio(100 * v, of('maxMemory').get(n)) for n, v in of('memory').items()}
    clients = {n: ratio(100 * v, of('maxClients').get(n)) for n, v in of('clients').items()}
    slow = {}
    for n, v in of('avgUs').items():
        b = baseline(of('avgD1').get(n), of('avgD7').get(n))
        if b is not None and v >= RD.RULES['rt_times'] * b:
            slow[n] = v
    answer = {'no-series': [] if up else scalar(1), 'down': rows(up), 'memory': alive({n: v for n, v in memory.items() if v < RD.RULES['mem_crit']}), 'memory-crit': alive(memory), 'clients': alive(clients),
              'latency': alive(slow), 'evicted': alive(of('evicted')), 'rejected': alive(of('rejected')), 'restart': alive(of('uptime'))}
    return {c['query']: answer[c['id'].removeprefix('redis-cloud-')] for c in checks}


RD_KINDS = {'no-series': 'no-series', 'down': 'down', 'memory': 'memory', 'memory-crit': 'memory', 'clients': 'clients', 'latency': 'latency', 'evicted': 'evicted', 'rejected': 'rejected',
            'restart': 'restart'}


@pytest.mark.parametrize('name, data', redis_cases(), ids=[c[0] for c in redis_cases()])
def test_the_managed_redis_preset_gives_the_cards_the_example_plugin_gives_on_its_own_test_data(name, data):
    # given
    _, checks = presets()['managed-redis'].fill({'instance': 'ops', 'datasource': RD.SOURCE, 'name': 'cloud'})
    example = redis_expected(data)

    # when
    got = fired(checks, redis_answers(data, checks), 'cloud', RD_KINDS)

    # then
    assert got == example


def test_the_preset_queries_read_the_series_the_example_plugins_read():
    # given
    _, couchbase = presets()['couchbase'].fill({'instance': 'main', 'cluster': CB.CLUSTER, 'selector': CB.SELECTOR})
    _, redis = presets()['managed-redis'].fill({'instance': 'ops', 'datasource': RD.SOURCE, 'name': 'cloud'})
    cb_text, rd_text = ' '.join(c['query'] for c in couchbase), ' '.join(c['query'] for c in redis)

    # then
    for expr in CB.Q.values():
        assert expr.split('(')[-1].split('{')[0].split('[')[0] in cb_text
    assert all(f'{{{CB.SELECTOR}}}' in c['query'] for c in couchbase)
    for metric in ('redis_up', 'redis_connected_clients', 'redis_config_maxclients', 'redis_memory_used_bytes', 'redis_memory_max_bytes', 'redis_evicted_keys_total',
                   'redis_rejected_connections_total', 'redis_uptime_in_seconds', 'redis_commands_duration_seconds_total', 'redis_commands_processed_total'):
        assert metric + '{}' in rd_text
    assert all(q.count('(') == q.count(')') and q.count('{') == q.count('}') for q in (c['query'] for c in couchbase + redis))
    assert all(' offset 1d' in c['query'] and ' offset 7d' in c['query'] for c in couchbase if c['id'].endswith(('conn-jump', 'n1ql-errors', 'n1ql-timeouts', 'n1ql-latency')))


def test_the_constants_inside_the_queries_and_the_thresholds_are_the_example_rules():
    # given
    r, m = CB.RULES, RD.RULES
    _, couchbase = presets()['couchbase'].fill({'instance': 'main', 'cluster': CB.CLUSTER, 'selector': CB.SELECTOR})
    _, redis = presets()['managed-redis'].fill({'instance': 'ops', 'datasource': RD.SOURCE, 'name': 'cloud'})
    cb = {c['id'].removeprefix('cb-orders-prod-'): c for c in couchbase}
    rd = {c['id'].removeprefix('redis-cloud-'): c for c in redis}

    # then
    assert {k: (c['op'], c['threshold']) for k, c in cb.items() if k in ('restart', 'index-memory', 'n1ql-queue', 'auth-fail', 'n1ql-timeouts', 'n1ql-errors', 'n1ql-errors-crit')} == {
        'restart': ('<', r['uptime_s']), 'index-memory': ('>=', r['imem']), 'n1ql-queue': ('>', r['queue']), 'auth-fail': ('>=', r['auth']),
        'n1ql-timeouts': ('>=', r['tmo_min']), 'n1ql-errors': ('<', r['err_crit']), 'n1ql-errors-crit': ('>=', r['err_crit'])}
    assert f"> {r['jump']} * " in cb['conn-jump']['query'] and f"+ {r['jump_slack']})" in cb['conn-jump']['query']
    errors = cb['n1ql-errors']['query']
    assert r['err_x'] == 2 and f"clamp_min(" in errors and f", {r['err_pp']})) or on () vector({r['err_share']})" in errors and f">= {r['err_min']})" in errors
    latency = cb['n1ql-latency']['query']
    assert r['lat_x'] == 2 and f", {r['lat_ms']})) or on () vector({r['lat_floor']})" in latency and f">= {r['lat_req']})" in latency
    assert f"<= {r['tmo_x']} * max(" in cb['n1ql-timeouts']['query']
    assert {k: (c['op'], c['threshold']) for k, c in rd.items() if k not in ('down', 'no-series')} == {
        'memory': ('>=', m['mem_warn']), 'memory-crit': ('>=', m['mem_crit']), 'clients': ('>=', m['conn_warn']), 'latency': ('>=', m['rt_floor_us']),
        'evicted': ('>', 0), 'rejected': ('>', 0), 'restart': ('<', m['uptime_s'])}
    assert f"< {m['mem_crit']} and" in rd['memory']['query'] and f">= {m['rt_times']} * max by" in rd['latency']['query']


def test_a_preset_whose_selector_matches_nothing_says_so_with_one_check():
    # given
    checks = {p.id: p.fill({'couchbase': COUCHBASE, 'managed-redis': REDIS}[p.id])[1] for p in presets().values() if p.source != 'elasticsearch'}

    # when
    found = {k: next(d for d in v if d['id'].endswith('-no-series')) for k, v in checks.items()}

    # then
    assert found['couchbase']['query'] == 'absent(up{job=~".*couchbase.*",env="prod"})'
    assert found['managed-redis']['query'] == 'absent(redis_up{})'
    assert all((d['op'], d['threshold'], d['severity'], d['by']) == ('>', 0, 'crit', []) for d in found.values())
    assert all('matches no' in d['text'] and 'selector' in d['text'].lower() for d in found.values())


def test_the_promtool_file_covers_every_check_of_every_preset_in_every_case(tmp_path):
    # when
    path = presets_promtool.write(tmp_path)
    doc = yaml.safe_load(path.read_text())

    # then
    assert doc['rule_files'] == [presets_promtool.RULES_FILE] and (tmp_path / presets_promtool.RULES_FILE).read_text() == 'groups: []\n'
    promql = [p for p, preset in presets().items() if preset.source != 'elasticsearch']
    groups = {p: [t for t in doc['tests'] if t['name'].startswith(p + ' ')] for p in promql}
    assert [len(groups[p]) for p in promql] == [len(couchbase_cases()), len(redis_cases())]
    for preset_id, tests in groups.items():
        _, checks = presets()[preset_id].fill({'couchbase': presets_promtool.COUCHBASE_VALUES, 'managed-redis': presets_promtool.REDIS_VALUES}[preset_id])
        for t in tests:
            assert [e['expr'] for e in t['promql_expr_test']] == [presets_promtool.expression(d) for d in checks]
            assert all(e['eval_time'] == '8d' for e in t['promql_expr_test'])
    every = next(t for t in groups['couchbase'] if t['name'] == 'couchbase every kind')
    firing = {e['expr'].split('(', 2)[2][:40]: [x['labels'] for x in e['exp_samples']] for e in every['promql_expr_test'] if e['exp_samples']}
    assert sum(len(v) for v in firing.values()) == len(couchbase_expected(dict(couchbase_cases())['every kind']))
    none = next(t for t in groups['couchbase'] if t['name'] == 'couchbase no up series')
    assert [(e['expr'], e['exp_samples']) for e in none['promql_expr_test'] if e['exp_samples']] == [
        ('count by () ((absent(up{job=~".*couchbase.*",env="prod"})) > 0.0 unless abs((absent(up{job=~".*couchbase.*",env="prod"}))) == Inf)', [{'labels': '{}', 'value': 1}])]


def test_the_promtool_series_read_back_the_values_of_the_example_data():
    # given
    data = dict(couchbase_cases())['every kind']

    # when
    series = {s['series']: s['values'].split(' ') for s in presets_promtool.couchbase_series(data)}

    # then
    up = series['up{job="couchbase-exporter",env="prod",instance="node-1.example.com:8091"}']
    assert len(up) == 385 and up[-2:] == ['0.0', '0.0'] and set(up[:-2]) == {'_'}
    errors = series['n1ql_errors{job="couchbase-exporter",env="prod"}']
    assert errors[-2:] == [repr(presets_promtool.BASE), repr(presets_promtool.BASE + 50)]
    gone = series['up{job="couchbase-exporter",env="prod",instance="node-9.example.com:8091"}']
    assert gone[384 - 48 - 1:384 - 48 + 1] == ['1.0', '1.0'] and gone[-1] == '_'


@pytest.mark.skipif(shutil.which('promtool') is None, reason='promtool is not installed; CI runs it from the Prometheus image')
def test_promtool_runs_every_preset_check_on_the_example_data(tmp_path):
    # given
    path = presets_promtool.write(tmp_path)

    # when
    done = subprocess.run(['promtool', 'test', 'rules', path.name], cwd=tmp_path, capture_output=True, text=True, timeout=300)

    # then
    assert done.returncode == 0, done.stdout + done.stderr


def test_a_rollback_that_cannot_delete_names_the_checks_left_behind(store, monkeypatch, caplog):
    # given
    defs = Definitions(store.db)
    real_create, real_delete = defs.create, defs.col.delete_one
    calls = []

    def flaky(d, by, tags=None):
        calls.append(d['id'])
        if len(calls) == 3:
            raise Full('at most 50 custom checks can be set up')
        return real_create(d, by, tags)

    def stuck(query, *a, **k):
        if query.get('_id') == 'cb-orders-prod-not-scraped':
            raise PyMongoError('down')
        return real_delete(query, *a, **k)

    monkeypatch.setattr(defs, 'create', flaky)
    monkeypatch.setattr(defs.col, 'delete_one', stuck)

    # when
    with caplog.at_level(logging.ERROR, logger='skopax.promqlchecks'), pytest.raises(Full) as e:
        add(defs, presets()['couchbase'], COUCHBASE, BY)

    # then
    assert 'cb-orders-prod-not-scraped' in e.value.text and 'at most 50' in e.value.text
    assert [d['id'] for d in defs.list()] == ['cb-orders-prod-not-scraped']
    assert 'cb-orders-prod-not-scraped' in caplog.text


def test_a_store_error_whose_rollback_fails_is_a_leftover_error_naming_the_checks(store, monkeypatch):
    # given
    defs = Definitions(store.db)
    real_create = defs.create
    calls = []

    def broken(d, by, tags=None):
        calls.append(d['id'])
        if len(calls) == 2:
            raise PyMongoError('lost')
        return real_create(d, by, tags)

    monkeypatch.setattr(defs, 'create', broken)
    monkeypatch.setattr(defs.col, 'delete_one', lambda *a, **k: (_ for _ in ()).throw(PyMongoError('down')))

    # when
    with pytest.raises(promqlchecks.Leftover) as e:
        add(defs, presets()['couchbase'], COUCHBASE, BY)

    # then
    assert 'cb-orders-prod-down' in e.value.text


def test_the_real_recount_after_an_insert_takes_back_the_whole_preset(store, monkeypatch):
    # given
    defs = Definitions(store.db)
    real = defs.col.count_documents
    monkeypatch.setattr(defs.col, 'count_documents', lambda query: real(query) + (promqlchecks.CHECKS_MAX if real(query) >= 3 else 0))

    # when
    with pytest.raises(Full):
        add(defs, presets()['couchbase'], COUCHBASE, BY)

    # then
    assert store.db['custom_checks'].count_documents({}) == 0


def test_the_duplicate_refusal_names_a_few_checks_and_counts_the_rest(store):
    # given
    defs = Definitions(store.db)
    made = add(defs, presets()['couchbase'], COUCHBASE, BY)

    # when
    with pytest.raises(Exists) as e:
        add(defs, presets()['couchbase'], COUCHBASE, BY)

    # then
    assert e.value.text.count('Couchbase orders-prod') == checkpresets.NAMED and f'and {len(made) - checkpresets.NAMED} more' in e.value.text
