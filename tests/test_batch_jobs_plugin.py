import json
import re
from dataclasses import dataclass, field
from urllib.parse import parse_qs, unquote, urlsplit

import pytest

from skopax.contract import HttpError, InstanceInfo, Peer
from skopax.kit import access, promql
from skopax.remote import Reply

from skopax import registry
from skopax.plugins import batch_jobs
from skopax.plugins.batch_jobs import CARD_TYPES, LINK, PLUGIN, RULES, links, queries, run

NOW = 1_792_224_000.0
DAY = 86400
PROM = 'http://prometheus.example.com:9090'
UI = 'https://prometheus.example.com'
CONFIG = {'prometheus': 'metrics'}
Q = queries(CONFIG)
EXPORT, INVOICES, SEARCH, CLEANUP = 'export-orders', 'send-invoices', 'rebuild-search-index', 'cleanup-sessions'
JOBS = {EXPORT: dict(runs=12, day=288, avg=30, max=40), INVOICES: dict(runs=1, day=24, avg=120, max=130),
        SEARCH: dict(runs=4, day=96, avg=300, max=320), CLEANUP: dict(runs=0, day=1, avg=None, max=None)}
DRIFT = RULES['drift_min'] * 60


def key(subject: str, sig: str, instance: str = 'shop-jobs') -> str:
    return f'batch-jobs|{instance}|{subject}|{sig}'


def labels(job: str, group: str = 'shop') -> dict:
    return {'namespace': group, 'job_name': job}


def healthy() -> dict:
    data = {(k, i): [] for k in Q for i in range(3)}
    for job, x in JOBS.items():
        for i in range(3):
            data[('runs', i)].append((labels(job), x['runs']))
            if i:
                data[('expected', i)].append((labels(job), x['runs']))
            data[('fails', i)].append((labels(job), 0))
            if x['avg'] is not None:
                data[('total', i)].append((labels(job), x['runs'] * x['avg']))
                data[('max', i)].append((labels(job), x['max']))
        data[('day', 0)].append((labels(job), x['day']))
        data[('last', 0)].append((labels(job), NOW - (36000 if job == CLEANUP else 120)))
    return data


def put(data: dict, k: str, i: int, job: str, v, group: str = 'shop') -> dict:
    rows = [(m, x) for m, x in data[(k, i)] if m != labels(job, group)]
    data[(k, i)] = rows + ([(labels(job, group), v)] if v is not None else [])
    return data


def failing(data: dict, job: str, runs, fails) -> dict:
    avg = JOBS.get(job, {}).get('avg')
    total = None if avg is None else runs * avg
    return put(put(put(data, 'runs', 0, job, runs), 'fails', 0, job, fails), 'total', 0, job, total)


def missed(data: dict, job: str, group: str = 'shop', often: bool = False) -> dict:
    for i in range(3):
        put(data, 'runs', i, job, 0 if i == 0 else 6 if often else 1, group)
        if i:
            put(data, 'expected', i, job, 6 if often else 1, group)
    return put(data, 'day', 0, job, 30, group)


def slow(data: dict, job: str, avg: float) -> dict:
    runs = JOBS[job]['runs']
    return put(data, 'total', 0, job, runs * avg)


class FakeProm:
    def __init__(self, data: dict | None = None, error: Exception | None = None, queries: dict | None = None):
        self.data = data or {}
        self.by_query = {q: k for k, q in (queries or Q).items()}
        self.error = error
        self.calls: list[tuple[str, float]] = []

    def __call__(self, method, url, headers, body, timeout):
        params = parse_qs(urlsplit(url).query)
        q = params['query'][0]
        at = float(params['time'][0]) if 'time' in params else NOW
        self.calls.append((q, at))
        if self.error is not None:
            raise self.error
        i = {0: 0, 1: 1, 7: 2}[round((NOW - at) / DAY)]
        rows = self.data.get((self.by_query.get(q), i), [])
        return Reply({'status': 'success', 'data': {'resultType': 'vector', 'result': [{'metric': m, 'value': [at, str(v)]} for m, v in rows]}}, {})


@dataclass
class Ctx:
    prom: object = None
    name: str = 'shop-jobs'
    config: dict = field(default_factory=lambda: dict(CONFIG))
    overrides: dict = field(default_factory=dict)
    peered: bool = True
    ui: str = UI
    now: float = NOW

    @property
    def instance(self) -> InstanceInfo:
        return InstanceInfo(self.name, True, None, self.config)

    def peer(self, name: str) -> Peer | None:
        assert name == 'prometheus'
        if not self.peered:
            return None
        return Peer(InstanceInfo('metrics', False, 'metrics', {'url': PROM, 'ui-url': self.ui}), lambda: self.prom)

    def rules(self, set_id: str) -> dict:
        assert set_id == 'rules'
        return dict(self.overrides)


def raised(data: dict, **given) -> set[tuple[str, str]]:
    return {(c['key'], c['sev']) for c in run(Ctx(FakeProm(data), **given)).result.found}


def test_the_plugin_reads_batch_jobs_through_a_prometheus_peer_with_names_as_fields():
    # when
    fields = {f.key: f for f in PLUGIN.fields}
    [check] = PLUGIN.checks
    [knowledge] = PLUGIN.knowledge

    # then
    assert PLUGIN.max_instances is None and PLUGIN.uses == ('prometheus',) and PLUGIN.tools == ()
    assert (fields['prometheus'].kind, fields['prometheus'].choices, fields['prometheus'].required) == ('instance', ('prometheus',), True)
    assert {k: fields[k].default for k in ('metric', 'group', 'job', 'result', 'failure', 'selector')} == {
        'metric': 'batch_job_duration_seconds', 'group': 'namespace', 'job': 'job_name', 'result': 'result', 'failure': 'failure', 'selector': ''}
    assert (check.id, check.ran, check.sections, check.period, check.offset) == ('batch-jobs', ('batch-jobs',), ('batch-jobs',), 600, 290)
    assert [(r.id, dict(r.defaults)) for r in PLUGIN.rules] == [('rules', RULES)] and set(PLUGIN.rules[0].help) == set(RULES)
    assert all(PLUGIN.rules[0].help[k].strip() for k in RULES)
    assert {t.sig for t in CARD_TYPES} == set(LINK) == {'batch-jobs-failing', 'batch-jobs-missed', 'batch-jobs-slow'}
    assert [(p.tab, p.script, p.group) for p in PLUGIN.panels] == [('batch-jobs', 'panel.js', 'observe')]
    assert (knowledge.id, knowledge.title, knowledge.slack) == ('batch-jobs', 'Batch jobs', True)
    assert len(knowledge.text) <= 8000 and knowledge.text.isascii()
    assert all(s in knowledge.text for s in ('batch-jobs-failing', 'batch-jobs-missed', 'batch-jobs-slow', 'promql', 'batch_job_duration_seconds_count'))


def test_a_healthy_pass_raises_nothing_and_fills_one_row_per_job_with_shifted_baselines():
    # given
    prom = FakeProm(healthy())

    # when
    sweep = run(Ctx(prom))

    # then
    out = sweep.result
    assert (out.name, out.ok, out.status, out.found, out.ran) == ('batch-jobs', True, 'ok', [], ('batch-jobs', 'batch-jobs:access:metrics'))
    assert len(prom.calls) == 4 * 3 + 2 + 2
    assert {at for _, at in prom.calls} == {NOW, NOW - DAY, NOW - 7 * DAY, NOW - DAY - DRIFT, NOW - 7 * DAY - DRIFT}
    assert sorted(at for q, at in prom.calls if q == Q['expected']) == [NOW - 7 * DAY - DRIFT, NOW - DAY - DRIFT] and f'[{3600 - DRIFT}s]' in Q['expected']
    rows = sweep.sections['batch-jobs']
    assert [r['job'] for r in rows if r['part'] == 'job'] == [CLEANUP, EXPORT, SEARCH, INVOICES]
    [summary] = [r for r in rows if r['part'] == 'summary']
    assert (summary['name'], summary['jobs'], summary['groups'], summary['source'], summary['ui'], summary['at']) == ('shop-jobs', 4, 1, 'metrics', UI, '2026-10-17T08:00:00Z')


def test_the_panel_rows_hold_runs_failures_and_times_against_one_and_seven_days_earlier():
    # given
    data = put(put(healthy(), 'fails', 1, EXPORT, 1), 'total', 2, EXPORT, 12 * 25)

    # when
    rows = run(Ctx(FakeProm(data))).sections['batch-jobs']

    # then
    row = next(r for r in rows if r.get('job') == EXPORT)
    assert row == {'part': 'job', 'name': 'shop-jobs', 'group': 'shop', 'job': EXPORT, 'runs': 12, 'runsD1': 12, 'runsD7': 12, 'fails': 0, 'failsD1': 1, 'failsD7': 0,
                   'avg': 30.0, 'avgD1': 30.0, 'avgD7': 25.0, 'max': 40, 'maxD1': 40, 'maxD7': 40, 'day': 288, 'interval': 300.0, 'last': NOW - 120}
    rare = next(r for r in rows if r.get('job') == CLEANUP)
    assert (rare['runs'], rare['avg'], rare['interval'], rare['last']) == (0, None, 86400.0, NOW - 36000)


@pytest.mark.parametrize('runs, fails, job, expect', [
    (3, 3, SEARCH, 'crit'), (12, 12, EXPORT, 'crit'), (2, 2, SEARCH, 'warn'), (10, 2, EXPORT, 'warn'), (11, 2, EXPORT, None),
    (12, 1, EXPORT, None), (1, 1, INVOICES, None), (1, 1, CLEANUP, 'info'), (12, 0, EXPORT, None), (2.6, 2.4, SEARCH, 'warn'),
])
def test_a_failing_job_is_critical_when_every_run_failed_warns_on_a_share_and_one_failure_is_info_only_for_rare_jobs(runs, fails, job, expect):
    # then
    assert raised(failing(healthy(), job, runs, fails)) == ({(key(f'shop/{job}', 'failing'), expect)} if expect else set())


@pytest.mark.parametrize('now, d1, d7, expect', [
    (0, 1, 1, 'warn'), (0, 6, 4, 'crit'), (0, 6, 3, 'warn'), (0.3, 1, 1, 'warn'), (0.6, 1, 1, None), (0, 1, 0, None), (0, None, 2, None), (None, 1, 1, 'warn'),
])
def test_a_job_that_ran_in_the_same_window_a_day_and_a_week_ago_but_not_now_is_missed(now, d1, d7, expect):
    # given
    data = healthy()
    for i, v in enumerate((now, d1, d7)):
        put(data, 'expected' if i else 'runs', i, INVOICES, v)
        if i:
            put(data, 'runs', i, INVOICES, v)

    # then
    assert raised(data) == ({(key(f'shop/{INVOICES}', 'missed'), expect)} if expect else set())


def test_many_missed_jobs_in_one_group_roll_up_into_one_card_for_the_group():
    # given
    three = missed(missed(missed(healthy(), 'export-orders-eu'), 'export-orders-us'), 'send-reminders', often=True)
    two = missed(missed(healthy(), 'export-orders-eu'), 'export-orders-us')
    other = missed(missed(missed(healthy(), 'export-orders-eu'), 'export-orders-us'), 'send-reminders', group='billing')

    # when
    [card] = run(Ctx(FakeProm(three))).result.found

    # then
    assert (card['key'], card['sev'], card['svc'], card['now']) == (key('shop', 'missed'), 'crit', 'shop', 3)
    assert all(j in card['what'] for j in ('export-orders-eu', 'export-orders-us', 'send-reminders'))
    assert raised(two) == {(key('shop/export-orders-eu', 'missed'), 'warn'), (key('shop/export-orders-us', 'missed'), 'warn')}
    assert raised(other) == {(key('shop/export-orders-eu', 'missed'), 'warn'), (key('shop/export-orders-us', 'missed'), 'warn'),
                             (key('billing/send-reminders', 'missed'), 'warn')}


@pytest.mark.parametrize('job, avg, expect', [
    (EXPORT, 200, 'warn'), (EXPORT, 100, 'info'), (EXPORT, 59, None), (EXPORT, 89, None),
    (INVOICES, 400, 'info'), (INVOICES, 300, None), (SEARCH, 650, 'warn'), (SEARCH, 450, None),
])
def test_a_slow_job_takes_x_times_its_usual_and_s_seconds_longer_and_a_share_of_its_interval(job, avg, expect):
    # then
    assert raised(slow(healthy(), job, avg)) == ({(key(f'shop/{job}', 'slow'), expect)} if expect else set())


def test_a_slow_job_needs_a_baseline_and_compares_with_the_slower_of_the_two_days():
    # given
    none = put(put(slow(healthy(), EXPORT, 200), 'total', 1, EXPORT, None), 'total', 2, EXPORT, None)
    slower = put(slow(healthy(), EXPORT, 200), 'total', 2, EXPORT, 12 * 110)

    # then
    assert raised(none) == set()
    assert raised(slower) == set()


def test_every_card_kind_is_a_declared_card_type_with_a_graph():
    # given
    data = slow(failing(missed(healthy(), 'export-orders-eu'), SEARCH, 4, 4), EXPORT, 200)

    # when
    found = run(Ctx(FakeProm(data))).result.found

    # then
    assert {c['sig'] for c in found} == {t.sig for t in CARD_TYPES}
    assert all(c['src'] == 'batch-jobs' and c['ns'] == 'shop' and c['ranKey'] == 'batch-jobs' for c in found)
    assert all(c['key'] == key(f"shop/{c['svc']}", c['sig'].removeprefix('batch-jobs-')) for c in found)
    assert all(len(c['urls']) == 1 and c['urls'][0]['url'].startswith(UI + '/query?') for c in found)
    assert all(c['what'].isascii() and c['short'].isascii() for c in found)


def test_the_rules_come_from_the_rule_set_of_the_instance():
    # given
    data = failing(healthy(), EXPORT, 12, 1)

    # then
    assert raised(data) == set()
    assert raised(data, overrides={'rare_runs': 300}) == {(key(f'shop/{EXPORT}', 'failing'), 'info')}
    assert raised(failing(healthy(), SEARCH, 4, 2), overrides={'fail_min': 3}) == set()


def test_no_series_at_all_is_an_error_pass_not_a_missed_card_per_job():
    # when
    out = run(Ctx(FakeProm({}))).result

    # then
    assert (out.ok, out.status, out.found, out.ran) == (False, 'error: Prometheus holds no batch_job_duration_seconds_count series', [], ())


def test_a_pass_that_runs_out_of_its_time_budget_fails_and_clears_nothing(monkeypatch):
    # given
    ticks = iter([0.0] + [1000.0] * 100)
    monkeypatch.setattr(promql, 'clock', lambda: next(ticks))
    prom = FakeProm(healthy())

    # when
    out = run(Ctx(prom)).result

    # then
    assert (out.ok, out.status, out.found, out.ran) == (False, 'error: timed out', [], ())
    assert prom.calls == []


@pytest.mark.parametrize('code, status', [(401, 401), (403, 403)])
def test_a_refused_read_raises_one_access_card_and_keeps_the_other_cards_open(code, status):
    # when
    out = run(Ctx(FakeProm(error=HttpError(code, PROM)))).result

    # then
    [card] = out.found
    assert (out.ok, out.ran, out.status) == (True, ('batch-jobs:access:metrics',), access.status('ok', ['batch job metrics']))
    assert (card['sig'], card['ranKey'], card['sev']) == ('no-access', 'batch-jobs:access:metrics', 'warn')
    assert card['label'] == access.text('Batch jobs', 'batch job metrics', credential='', status=status)


def test_no_peer_or_a_failing_prometheus_is_an_error_pass_that_clears_nothing():
    # when
    unset = run(Ctx(FakeProm(healthy()), peered=False)).result
    down = run(Ctx(FakeProm(error=HttpError(503, PROM)))).result

    # then
    assert (unset.ok, unset.status, unset.ran) == (False, 'error: no Prometheus instance is set', ())
    assert (down.ok, down.status, down.ran) == (False, 'error: Prometheus query HTTP 503', ())


def test_other_names_and_a_selector_reach_every_query():
    # given
    config = {'prometheus': 'metrics', 'metric': 'cron_run_seconds', 'group': 'team', 'job': 'task', 'result': 'status', 'failure': 'error|failed', 'selector': 'env="prod"'}
    q = queries(config)
    data = {}
    for (k, i), rows in healthy().items():
        data[(k, i)] = [({'team': m['namespace'], 'task': m['job_name']}, v) for m, v in rows]
    prom = FakeProm(data, queries=q)

    # when
    out = run(Ctx(prom, config=config)).result

    # then
    assert out.ok and out.status == 'ok'
    assert all('cron_run_seconds_' in s and 'env="prod"' in s and 'by (team, task)' in s for s, _ in prom.calls)
    assert 'status=~"error|failed"' in q['fails'] and 'cron_run_seconds_max' in q['max'] and 'cron_run_seconds_sum' in q['total']


@pytest.mark.parametrize('config, problem', [
    ({'metric': 'batch-job'}, 'the metric must be a Prometheus metric name such as batch_job_duration_seconds'),
    ({'metric': 'x{a="1"} or up'}, 'the metric must be a Prometheus metric name such as batch_job_duration_seconds'),
    ({'group': 'name space'}, 'the group, job and result labels must be label names such as namespace, job_name and result'),
    ({'job': 'job"}'}, 'the group, job and result labels must be label names such as namespace, job_name and result'),
    ({'result': 'job_name'}, 'the group, job and result labels must be three different labels'),
    ({'failure': 'a"} or up{x="'}, 'the failure values must be words joined by | such as failure|error'),
    ({'failure': 'failure||error'}, 'the failure values must be words joined by | such as failure|error'),
    ({'selector': 'job="a"} or up{x="1"'}, 'the selector must be empty or label matchers such as env="prod"'),
    ({'selector': 'env'}, 'the selector must be empty or label matchers such as env="prod"'),
])
def test_a_field_that_could_break_a_query_fails_the_pass_and_the_test_without_a_query(config, problem):
    # given
    prom = FakeProm(healthy())
    ctx = Ctx(prom, config={'prometheus': 'metrics', **config})

    # when
    out = run(ctx).result
    said = batch_jobs.test(ctx)

    # then
    assert (out.ok, out.found, out.ran, out.status) == (False, [], (), f'error: {problem}')
    assert (said.ok, said.summary) == (False, problem)
    assert prom.calls == []


def test_card_links_are_prometheus_graphs_on_an_https_ui_only():
    # given
    data = failing(healthy(), SEARCH, 4, 4)

    # when
    [card] = run(Ctx(FakeProm(data))).result.found
    [plain] = run(Ctx(FakeProm(data), ui='http://prometheus.example.com:9090')).result.found

    # then
    [link] = links(card, InstanceInfo('shop-jobs', True, None, {}))
    assert link['label'] == 'Prometheus graph'
    expr = unquote(parse_qs(urlsplit(link['url']).query)['g0.expr'][0])
    assert 'namespace=' + json.dumps('shop') in expr and 'job_name=' + json.dumps(SEARCH) in expr and 'result=~"failure"' in expr
    assert plain['urls'] == [] and links(plain, InstanceInfo('shop-jobs', True, None, {})) == []
    assert links({'src': 'other', 'urls': [{'label': 'x', 'url': UI}]}, InstanceInfo('shop-jobs', True, None, {})) is None
    assert links({'src': 'batch-jobs', 'urls': [{'label': 'x', 'url': 'javascript:alert(1)'}, 'x', {'label': 'y', 'url': UI}]}, InstanceInfo('shop-jobs', True, None, {})) == [
        {'label': 'y', 'url': UI}]


def test_a_label_value_with_a_quote_cannot_change_a_link_query():
    # given
    odd = 'export"} or up{x="1'
    data = failing(put(put(healthy(), 'runs', 0, odd, 3), 'day', 0, odd, 72), odd, 3, 3)

    # when
    card = next(c for c in run(Ctx(FakeProm(data))).result.found if c['svc'] == odd)

    # then
    expr = parse_qs(urlsplit(card['urls'][0]['url']).query)['g0.expr'][0]
    assert 'namespace="shop",job_name=' + json.dumps(odd) + ',result=~"failure"}' in expr


def test_two_instances_keep_distinct_card_keys():
    # given
    data = failing(slow(healthy(), EXPORT, 200), SEARCH, 4, 4)

    # when
    first = run(Ctx(FakeProm(data), name='shop-jobs')).result.found
    second = run(Ctx(FakeProm(data), name='shop-jobs-eu')).result.found

    # then
    assert len(first) == len(second) == 2
    assert {c['key'] for c in first}.isdisjoint({c['key'] for c in second})


def test_test_connection_counts_the_jobs_it_sees():
    # given
    prom = FakeProm({('jobs', 0): [(labels(j), 3) for j in JOBS]}, queries={'jobs': batch_jobs.jobs_query(CONFIG)})

    # when
    said = batch_jobs.test(Ctx(prom))
    empty = batch_jobs.test(Ctx(FakeProm({})))
    unset = batch_jobs.test(Ctx(prom, peered=False))
    refused = batch_jobs.test(Ctx(FakeProm(error=HttpError(403, PROM))))

    # then
    assert (said.ok, said.summary) == (True, '4 batch jobs in 1 group through Prometheus metrics')
    assert [q for q, _ in prom.calls] == ['count by (namespace, job_name) (batch_job_duration_seconds_count)']
    assert (empty.ok, empty.summary) == (True, 'Prometheus metrics answered, but it holds no batch_job_duration_seconds_count series')
    assert (unset.ok, unset.summary) == (False, 'no Prometheus instance is set')
    assert (refused.ok, refused.summary) == (False, access.text('Batch jobs', 'batch job metrics', credential='', status=403))


def test_the_plugin_ships_built_in_for_the_community_edition_with_a_setup_guide():
    # then
    assert 'skopax.plugins.batch_jobs' in registry.BUILTIN
    assert batch_jobs.PLUGIN is PLUGIN and PLUGIN.id == 'batch-jobs' and PLUGIN.version == '0.1.0' and PLUGIN.edition == 'community'
    assert PLUGIN.setup.docs == 'https://prometheus.io/docs/concepts/metric_types/' and PLUGIN.setup.docs_label == 'Prometheus: metric types'
    assert all(s.isascii() for s in PLUGIN.setup.steps) and 'Example' not in PLUGIN.description


def test_current_runs_missing_while_baselines_exist_is_an_error_pass_that_clears_nothing():
    # given
    data = healthy()
    data[('runs', 0)] = []

    # when
    out = run(Ctx(FakeProm(data))).result

    # then
    assert (out.ok, out.status, out.found, out.ran) == (False, 'error: Prometheus holds no batch_job_duration_seconds_count series', [], ())


class EventProm:
    def __init__(self, runs: dict[str, list[float]]):
        self.runs = runs
        self.counted = {Q['runs'], Q['expected'], Q['day']}

    def __call__(self, method, url, headers, body, timeout):
        params = parse_qs(urlsplit(url).query)
        q, at = params['query'][0], float(params['time'][0])
        rows = []
        if q in self.counted:
            amount, unit = re.search(r'\[(\d+)([smd])\]', q).groups()
            span = int(amount) * {'s': 1, 'm': 60, 'd': DAY}[unit]
            rows = [{'metric': labels(job), 'value': [at, str(sum(1 for t in ts if at - span < t <= at))]} for job, ts in self.runs.items()]
        return Reply({'status': 'success', 'data': {'resultType': 'vector', 'result': rows}}, {})


@pytest.mark.parametrize('after, today, expect', [(3900, True, set()), (1800, True, set()), (1800, False, {(key('shop/' + CLEANUP, 'missed'), 'warn')})])
def test_a_daily_job_that_ran_on_time_an_hour_ago_is_not_missed_and_one_that_did_not_run_is(after, today, expect):
    # given
    eight = NOW
    runs = {CLEANUP: [eight - k * DAY for k in range(0 if today else 1, 9)], EXPORT: [NOW + after - k * 300 for k in range(1, 300)]}

    # when
    found = run(Ctx(EventProm(runs), now=NOW + after)).result.found

    # then
    assert {(c['key'], c['sev']) for c in found} == expect


def test_a_slash_or_bar_in_a_group_or_job_is_escaped_in_the_card_key():
    # given
    odd = 'export/orders|eu'
    data = failing(put(put(healthy(), 'runs', 0, odd, 3, 'shop|eu'), 'day', 0, odd, 72, 'shop|eu'), SEARCH, 4, 4)
    put(data, 'fails', 0, odd, 3, 'shop|eu')

    # when
    keys = {c['key'] for c in run(Ctx(FakeProm(data))).result.found}

    # then
    assert keys == {key('shop/' + SEARCH, 'failing'), key('shop%7Ceu/export%2Forders%7Ceu', 'failing')}


@pytest.mark.parametrize('after', range(0, 3600, 300))
def test_a_missed_fifteen_minute_job_keeps_its_level_whatever_the_minute_of_the_pass(after):
    # given
    now = NOW + after
    runs = {SEARCH: [NOW - k * DAY + j * 900 for k in range(1, 9) for j in range(-8, 16)], EXPORT: [now - k * 300 for k in range(1, 300)]}

    # when
    found = run(Ctx(EventProm(runs), now=now)).result.found

    # then
    assert {(c['key'], c['sev'], c['d1'], c['d7']) for c in found} == {(key('shop/' + SEARCH, 'missed'), 'crit', 4, 4)}


def test_a_missed_card_names_its_window_and_shows_the_same_hour_counts_as_the_page():
    # given
    data = missed(healthy(), 'export-orders-eu', often=True)
    put(put(data, 'expected', 1, 'export-orders-eu', 5), 'expected', 2, 'export-orders-eu', 4)

    # when
    sweep = run(Ctx(FakeProm(data)))
    [card] = sweep.result.found
    row = next(r for r in sweep.sections['batch-jobs'] if r.get('job') == 'export-orders-eu')

    # then
    assert (card['d1'], card['d7']) == (row['runsD1'], row['runsD7']) == (6, 6)
    assert 'ran 5 times in the 50 minutes that ended 10 minutes before this time a day ago and 4 times a week ago' in card['what']
    assert 'in the whole same hour it ran 6 times a day ago and 6 times a week ago' in card['what']
    assert 'same 60 min' not in card['what'] and card['what'].isascii()


def test_a_group_missed_card_names_the_shorter_window():
    # given
    data = missed(missed(missed(healthy(), 'export-orders-eu'), 'export-orders-us'), 'send-reminders')

    # when
    [card] = run(Ctx(FakeProm(data, queries=queries(CONFIG, 900)), overrides={'drift_min': 15})).result.found

    # then
    assert 'ran in the 45 minutes that ended 15 minutes before this time both a day ago and a week ago, but not in the last 60 min' in card['what']


def test_a_percent_sign_is_escaped_so_names_that_look_escaped_never_share_a_key():
    # given
    data = healthy()
    for job in ('50%', 'a/b', 'a%2Fb'):
        failing(put(put(data, 'runs', 0, job, 3), 'day', 0, job, 72), job, 3, 3)

    # when
    keys = {c['key'] for c in run(Ctx(FakeProm(data))).result.found}

    # then
    assert keys == {key('shop/50%25', 'failing'), key('shop/a%2Fb', 'failing'), key('shop/a%252Fb', 'failing')}


def test_a_fractional_drift_is_floored_once_so_the_card_text_and_the_queried_window_agree():
    # given
    q = queries(CONFIG, 120)
    prom = FakeProm(missed(healthy(), 'export-orders-eu'), queries=q)

    # when
    [card] = run(Ctx(prom, overrides={'drift_min': 2.5})).result.found

    # then
    assert '[3480s]' in q['expected']
    assert sorted(at for s, at in prom.calls if s == q['expected']) == [NOW - 7 * DAY - 120, NOW - DAY - 120]
    assert 'in the 58 minutes that ended 2 minutes before this time a day ago' in card['what']


GRAPH = 'https://grafana.example.com/d/jobs?var-ns={namespace}&var-job={job}'
LOGS = 'http://logs.example.com/search?q=job:{job}'


def test_the_job_link_and_known_cause_fields_are_optional_text_fields_checked_on_save():
    # when
    fields = {f.key: f for f in PLUGIN.fields}

    # then
    assert [k for k in fields][-3:] == ['graph-url', 'logs-url', 'known-causes']
    assert all((fields[k].kind, fields[k].required, fields[k].default) == ('text', False, '') and fields[k].check is not None for k in ('graph-url', 'logs-url', 'known-causes'))
    assert fields['graph-url'].ui and fields['logs-url'].ui and not fields['known-causes'].ui
    assert fields['graph-url'].check(GRAPH) is None and fields['logs-url'].check(LOGS) is None and fields['known-causes'].check('nightly-export*: usually the warehouse lock') is None
    assert all(fields[k].help.isascii() and '{job}' in fields[k].help for k in ('graph-url', 'logs-url'))


@pytest.mark.parametrize('value', [
    'ftp://logs.example.com/{job}', 'logs.example.com/{job}', 'https://{namespace}.logs.example.com/{job}', 'https://alice@logs.example.com/{job}',
    'https:///{job}', 'https://logs.example.com/a b', 'javascript:alert(1)', 'https://a.example.com\\.evil.com/{job}', 'https://a.example.com%2F.evil.com/{job}',
    'https://a.exa\tmple.com/{job}', 'https://a.example.com:99999/{job}', 'https://a_b.example.com/{job}',
])
def test_a_job_link_that_is_not_an_http_address_with_placeholders_after_the_host_is_refused(value):
    # given
    prom = FakeProm(healthy())
    ctx = Ctx(prom, config={**CONFIG, 'logs-url': value})

    # when
    said = {f.key: f for f in PLUGIN.fields}['logs-url'].check(value)
    out = run(ctx).result
    tested = batch_jobs.test(ctx)

    # then
    assert said == batch_jobs.BAD_LINK
    assert (out.ok, out.status, out.found) == (False, f'error: {batch_jobs.BAD_LINK}', [])
    assert (tested.ok, tested.summary) == (False, batch_jobs.BAD_LINK)
    assert prom.calls == []


@pytest.mark.parametrize('value', ['nightly-export*', ': a note', 'nightly-export*:', 'a: ok; b', 'x' * 201 + ': note', 'a: ' + 'n' * 301])
def test_a_known_cause_entry_without_a_pattern_and_a_note_is_refused(value):
    # given
    ctx = Ctx(FakeProm(healthy()), config={**CONFIG, 'known-causes': value})

    # when
    said = {f.key: f for f in PLUGIN.fields}['known-causes'].check(value)
    out = run(ctx).result

    # then
    assert said == batch_jobs.BAD_KNOWN
    assert (out.ok, out.status) == (False, f'error: {batch_jobs.BAD_KNOWN}')


def test_job_links_fill_the_group_and_job_escaped_show_on_the_card_and_reach_the_investigator_in_its_text():
    # given
    odd = 'export orders/eu&x'
    data = failing(put(put(healthy(), 'runs', 0, odd, 3), 'day', 0, odd, 72), odd, 3, 3)
    config = {**CONFIG, 'graph-url': GRAPH, 'logs-url': LOGS}

    # when
    card = next(c for c in run(Ctx(FakeProm(data), config=config)).result.found if c['svc'] == odd)
    shown = links(card, InstanceInfo('shop-jobs', True, None, config))

    # then
    graph = 'https://grafana.example.com/d/jobs?var-ns=shop&var-job=export%20orders%2Feu%26x'
    logs = 'http://logs.example.com/search?q=job:export%20orders%2Feu%26x'
    assert [u['label'] for u in shown] == ['Prometheus graph', 'Job graph']
    assert shown[1]['url'] == graph
    assert card['what'].endswith(f' Job graph: {graph} Job logs: {logs}')
    assert card['what'].isascii()


def test_a_group_card_keeps_only_the_links_without_a_job_placeholder():
    # given
    data = missed(missed(missed(healthy(), 'export-orders-eu'), 'export-orders-us'), 'send-reminders')
    config = {**CONFIG, 'graph-url': 'https://grafana.example.com/d/jobs?var-ns={namespace}', 'logs-url': LOGS}

    # when
    [card] = run(Ctx(FakeProm(data), config=config)).result.found

    # then
    assert [u['label'] for u in card['urls']] == ['Prometheus graph', 'Job graph']
    assert card['what'].endswith(' Job graph: https://grafana.example.com/d/jobs?var-ns=shop') and 'Job logs' not in card['what']


def test_no_job_link_fields_leave_the_card_text_and_links_as_before():
    # given
    data = failing(healthy(), SEARCH, 4, 4)

    # when
    [card] = run(Ctx(FakeProm(data))).result.found

    # then
    assert [u['label'] for u in card['urls']] == ['Prometheus graph'] and 'Job graph' not in card['what'] and 'knownCause' not in card


def test_a_known_cause_note_goes_on_the_cards_of_the_first_matching_job_pattern():
    # given
    data = slow(failing(failing(healthy(), SEARCH, 4, 4), INVOICES, 1, 1), EXPORT, 200)
    put(data, 'day', 0, INVOICES, 2)
    known = 'rebuild-*: usually the warehouse lock, retry after 02:00; *: never this one; shop/export-*: the export host is slow at night'
    config = {**CONFIG, 'known-causes': known}

    # when
    found = {c['svc']: c for c in run(Ctx(FakeProm(data), config=config)).result.found}

    # then
    assert found[SEARCH]['knownCause'] == 'usually the warehouse lock, retry after 02:00'
    assert found[INVOICES]['knownCause'] == 'never this one'
    assert found[EXPORT]['knownCause'] == 'never this one'
    assert 'warehouse lock' not in found[SEARCH]['what']


def test_a_known_cause_pattern_with_a_slash_matches_the_group_and_job():
    # given
    data = slow(healthy(), EXPORT, 200)
    config = {**CONFIG, 'known-causes': 'billing/*: not this group; shop/export-*: the export host is slow at night'}

    # when
    [card] = run(Ctx(FakeProm(data), config=config)).result.found

    # then
    assert card['knownCause'] == 'the export host is slow at night'


def test_a_group_card_takes_no_known_cause_of_a_job():
    # given
    data = missed(missed(missed(healthy(), 'export-orders-eu'), 'export-orders-us'), 'send-reminders')

    # when
    [card] = run(Ctx(FakeProm(data), config={**CONFIG, 'known-causes': '*: a job note'})).result.found

    # then
    assert 'knownCause' not in card


@pytest.mark.parametrize('key, value, said', [('graph-url', 'https://{namespace}.grafana.example.com/{job}', batch_jobs.BAD_LINK),
                                              ('logs-url', 'ftp://logs.example.com/{job}', batch_jobs.BAD_LINK),
                                              ('known-causes', 'nightly-export* usually the warehouse lock', batch_jobs.BAD_KNOWN)])
def test_saving_a_bad_job_link_or_known_cause_is_refused_with_its_own_sentence(key, value, said):
    # given
    import mongomock
    from skopax.instances import InvalidInstance
    from skopax.plugins import prometheus
    from tests.test_instances import Box
    box = Box(mongomock.MongoClient(tz_aware=True)['bj'], prometheus.PLUGIN, PLUGIN)
    box.instances.create('prometheus', 'metrics', {'url': PROM}, True, 'alice')

    # when
    with pytest.raises(InvalidInstance) as e:
        box.instances.create('batch-jobs', 'shop-jobs', {'prometheus': 'metrics', key: value}, True, 'alice')
    made = box.instances.create('batch-jobs', 'shop-jobs', {'prometheus': 'metrics', 'graph-url': GRAPH, 'logs-url': LOGS, 'known-causes': 'nightly-*: the lock'}, True, 'alice')

    # then
    assert (e.value.field, e.value.args[0]) == (key, said)
    assert made.values['graph-url'] == GRAPH and made.values['known-causes'] == 'nightly-*: the lock'


def test_a_known_cause_stays_while_the_pass_raises_it_and_goes_when_the_setting_drops_it():
    # given
    from skopax.engine import merge
    data = failing(healthy(), SEARCH, 4, 4)
    config = {**CONFIG, 'known-causes': 'rebuild-*: usually the warehouse lock'}
    state = {'anomalies': []}

    # when
    first = run(Ctx(FakeProm(data), config=config)).result.found
    merge(state, first, {'batch-jobs': True}, NOW)
    kept = dict(state['anomalies'][0])
    merge(state, run(Ctx(FakeProm(data))).result.found, {'batch-jobs': True}, NOW + 600)

    # then
    assert kept['knownCause'] == 'usually the warehouse lock' and kept['what'] == first[0]['what']
    assert state['anomalies'][0]['key'] == kept['key'] and 'knownCause' not in state['anomalies'][0]
