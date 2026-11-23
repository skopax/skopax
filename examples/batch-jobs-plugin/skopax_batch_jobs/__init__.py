import concurrent.futures
import json
import re
from collections.abc import Mapping
from pathlib import Path

from skopax.contract import CardType, Check, Context, Field, HttpError, Knowledge, Panel, PassResult, Plugin, ReadError, RuleSet, Sweep, TestResult, anomaly, fmt, iso
from skopax.kit import access, connection
from skopax.kit.promql import Late, PrometheusError, bounded, client_for, credential, instant, prom_link, ui_of
from skopax.kit.text import duration

ID = 'example-batch-jobs'
NAME = 'Example batch jobs'
PART = 'metrics'
PART_NAME = 'batch job metrics'
BUDGET_S = 120
NO_PEER = 'no Prometheus instance is set'
W = '60m'
OFFSETS = (1, 7)
DEFAULTS = dict(metric='batch_job_duration_seconds', group='namespace', job='job_name', result='result', failure='failure', selector='')
NAME_MAX = 200
SELECTOR_MAX = 300
METRIC = re.compile(r'[a-zA-Z_:][a-zA-Z0-9_:]*')
LABEL = re.compile(r'[a-zA-Z_][a-zA-Z0-9_]*')
FAILURE = re.compile(r'[A-Za-z0-9_.-]+(?:\|[A-Za-z0-9_.-]+)*')
MATCHER = r' *[a-zA-Z_][a-zA-Z0-9_]* *(?:=~|!~|!=|=) *"(?:[^"\\\n]|\\.)*" *'
SELECTOR = re.compile(f'{MATCHER}(?:,{MATCHER})*')
BAD_METRIC = f"the metric must be a Prometheus metric name such as {DEFAULTS['metric']}"
BAD_LABEL = 'the group, job and result labels must be label names such as namespace, job_name and result'
SAME_LABEL = 'the group, job and result labels must be three different labels'
BAD_FAILURE = 'the failure values must be words joined by | such as failure|error'
BAD_SELECTOR = 'the selector must be empty or label matchers such as env="prod"'
QUERIES = dict(
    runs='sum by ({by}) (increase({m}_count{sel}[{w}]))',
    fails='sum by ({by}) (increase({m}_count{failed}[{w}]))',
    total='sum by ({by}) (increase({m}_sum{sel}[{w}]))',
    max='max by ({by}) (max_over_time({m}_max{sel}[{w}]))',
)
EXPECTED = 'sum by ({by}) (increase({m}_count{sel}[{e}]))'
NOW_ONLY = dict(
    last='max by ({by}) (max_over_time(timestamp(increase({m}_count{sel}[10m]) > 0)[1d:5m]))',
    day='sum by ({by}) (increase({m}_count{sel}[1d]))',
)
LINK = {
    'example-batch-jobs-failing': 'sum by ({by}) (increase({m}_count{failed}[60m]))',
    'example-batch-jobs-missed': 'sum by ({by}) (increase({m}_count{sel}[60m]))',
    'example-batch-jobs-slow': 'sum by ({by}) (increase({m}_sum{sel}[60m])) / sum by ({by}) (increase({m}_count{sel}[60m]))',
}
RULES = dict(fail_crit=3, fail_min=2, fail_share=20, rare_runs=4, miss_runs=1, miss_often=4, drift_min=10, miss_group=3, slow_x=2, slow_s=60, slow_warn=50,
             slow_info=10)
RULE_HELP = {
    'fail_crit': 'Critical when every run in the last 60 min failed and at least this many did.',
    'fail_min': 'Warning when at least this many runs failed in the last 60 min,',
    'fail_share': 'and they are at least this percent of the runs.',
    'rare_runs': 'A job with at most this many runs in 24 h is rare: one failed run of it is an info card. Other jobs need the warning rule.',
    'miss_runs': 'A job is missed when it ran at least this many times in the same 60 min a day ago and a week ago (less the drift minutes at their end), and not at all in the last 60 min.',
    'miss_often': 'A missed job is critical when it ran at least this many times in the whole same hour a day ago and a week ago.',
    'drift_min': 'For missed jobs, the day and week windows leave out their last this many minutes, so a job that starts a little later than usual is not missed.',
    'miss_group': 'At this many missed jobs in one group, one card for the group replaces the cards per job.',
    'slow_x': 'Slow when the average run takes this many times its usual (the slower of the same 60 min a day and a week ago),',
    'slow_s': 'and at least this many seconds longer.',
    'slow_warn': 'A slow job warns when its average run takes at least this percent of its interval (24 h divided by its runs in 24 h),',
    'slow_info': 'and is info from this percent; below it there is no card.',
}
SECTION_HELP = {ID: 'one row per batch job (runs, failures, average and longest run against the same 60 min 1 and 7 days earlier, last run, interval) and one summary row'}
KNOWLEDGE = (Path(__file__).parent / 'batch_jobs.md').read_text(encoding='utf-8')


class NoSeries(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def names_of(config: Mapping[str, str]) -> dict[str, str]:
    return {k: (config.get(k) or '').strip() or v for k, v in DEFAULTS.items()}


def problem(n: dict[str, str]) -> str | None:
    if len(n['metric']) > NAME_MAX or not METRIC.fullmatch(n['metric']):
        return BAD_METRIC
    labels = (n['group'], n['job'], n['result'])
    if any(len(x) > NAME_MAX or not LABEL.fullmatch(x) for x in labels):
        return BAD_LABEL
    if len(set(labels)) < 3:
        return SAME_LABEL
    if len(n['failure']) > NAME_MAX or not FAILURE.fullmatch(n['failure']):
        return BAD_FAILURE
    if n['selector'] and (len(n['selector']) > SELECTOR_MAX or not SELECTOR.fullmatch(n['selector'])):
        return BAD_SELECTOR
    return None


def braces(*parts: str) -> str:
    kept = [p for p in parts if p]
    return '{' + ','.join(kept) + '}' if kept else ''


def parts(n: dict[str, str], extra: tuple[str, ...] = ()) -> dict[str, str]:
    failed = f"{n['result']}=~{json.dumps(n['failure'])}"
    return dict(by=f"{n['group']}, {n['job']}", m=n['metric'], w=W, sel=braces(n['selector'], *extra), failed=braces(n['selector'], *extra, failed))


def drift_of(rules: Mapping[str, float]) -> int:
    return min(59, max(0, int(rules['drift_min'])))


def window_s(drift_s: float) -> int:
    return max(60, 3600 - round(drift_s))


def queries(config: Mapping[str, str], drift_s: float = RULES['drift_min'] * 60) -> dict[str, str]:
    p = parts(names_of(config))
    return {**{k: q.format(**p) for k, q in {**QUERIES, **NOW_ONLY}.items()}, 'expected': EXPECTED.format(**p, e=f'{window_s(drift_s)}s')}


def jobs_query(config: Mapping[str, str]) -> str:
    p = parts(names_of(config))
    return f"count by ({p['by']}) ({p['m']}_count{p['sel']})"


def fetch(client, q: dict[str, str], now: float, drift_s: float) -> dict:
    ats = [now] + [now - o * 86400 for o in OFFSETS]
    with concurrent.futures.ThreadPoolExecutor(6) as ex:
        futs = {(k, i): ex.submit(client.vec, q[k], t) for k in QUERIES for i, t in enumerate(ats)}
        futs.update({('expected', i + 1): ex.submit(client.vec, q['expected'], now - o * 86400 - drift_s) for i, o in enumerate(OFFSETS)})
        futs.update({(k, 0): ex.submit(client.vec, q[k], now) for k in NOW_ONLY})
        return {k: f.result() for k, f in futs.items()}


def escape(v: str) -> str:
    return v.replace('%', '%25').replace('/', '%2F').replace('|', '%7C')


def dash(v, unit: str = '') -> str:
    return '-' if v is None else fmt(v, unit)


def secs(v) -> str:
    return '-' if v is None else duration(v)


def count(v) -> int | None:
    return None if v is None else round(v)


class Reading:
    def __init__(self, data: dict, n: dict[str, str]):
        self.data = data
        self.n = n

    def per_job(self, k: str, i: int) -> dict:
        return {(m.get(self.n['group'], ''), m.get(self.n['job'], '')): v for m, v in self.data.get((k, i), [])}

    def trio(self, k: str) -> list[dict]:
        return [self.per_job(k, i) for i in range(1 + len(OFFSETS))]


class Cards:
    def __init__(self, instance: str, rules: dict, n: dict[str, str], ui: str):
        self.instance = instance
        self.rules = rules
        self.n = n
        self.ui = ui
        self.found: list[dict] = []
        self.missed: dict[str, list[tuple[str, str, list]]] = {}

    def link(self, sig: str, group: str, job: str | None) -> list[dict]:
        if not self.ui.startswith('https://'):
            return []
        extra = (f"{self.n['group']}={json.dumps(group)}",) + (() if job is None else (f"{self.n['job']}={json.dumps(job)}",))
        return [dict(label='Prometheus graph', url=prom_link(LINK[sig].format(**parts(self.n, extra)), self.ui))]

    def add(self, group: str, job: str | None, sig: str, sev: str, now_v, d1, d7, label: str, what: str, slack, short: str, unit: str = '') -> None:
        subject = escape(group) if job is None else f'{escape(group)}/{escape(job)}'
        x = anomaly(ID, f'{ID}-{sig}', group, group if job is None else job, '', sev, now_v, d1, d7, unit, label, what, slack, short)
        x.update(key=f'{ID}|{self.instance}|{subject}|{sig}', ranKey=ID, urls=self.link(f'{ID}-{sig}', group, job))
        self.found.append(x)

    def failing(self, group: str, job: str, runs: list, fails: list, day) -> None:
        rules = self.rules
        r, f = runs[0] or 0, fails[0] or 0
        if f < 1:
            return
        share = 100 * f / max(r, f)
        if f >= r and f >= rules['fail_crit']:
            sev = 'crit'
        elif f >= rules['fail_min'] and share >= rules['fail_share']:
            sev = 'warn'
        elif (day if day is not None else r) <= rules['rare_runs']:
            sev = 'info'
        else:
            return
        self.add(group, job, 'failing', sev, f, fails[1], fails[2], 'failed runs',
                 f'{fmt(f)} of {fmt(max(r, f))} runs of batch job {job} in {group or "-"} failed in the last 60 min ({fmt(share, "%")}); the same window had '
                 f'{dash(fails[1])} failed a day ago and {dash(fails[2])} a week ago', 1, f'{sev.upper()} batch job {job} {fmt(f)} of {fmt(max(r, f))} runs failed')

    def check_missed(self, group: str, job: str, now, expected: list, hour: list) -> None:
        rules = self.rules
        if (now or 0) >= 1 or None in expected or min(expected) < max(rules['miss_runs'], 1):
            return
        sev = 'crit' if None not in hour and min(hour) >= rules['miss_often'] else 'warn'
        self.missed.setdefault(group, []).append((job, sev, expected, hour))

    def window(self) -> str:
        drift = drift_of(self.rules)
        return f'{window_s(drift * 60) // 60} minutes that ended {drift} minutes before this time'

    def roll_missed(self) -> None:
        for group, jobs in sorted(self.missed.items()):
            if len(jobs) >= self.rules['miss_group']:
                sev = 'crit' if any(x[1] == 'crit' for x in jobs) else 'warn'
                listed = ', '.join(x[0] for x in jobs)
                self.add(group, None, 'missed', sev, len(jobs), None, None, 'jobs missed',
                         f'{len(jobs)} batch jobs in {group or "-"} ran in the {self.window()} both a day ago and a week ago, but not in the last 60 min: {listed}. '
                         'Many jobs of one group missing at once points at their scheduler or at the metrics, not at one job', 1,
                         f'{sev.upper()} {len(jobs)} batch jobs missed in {group or "-"}')
                continue
            for job, sev, expected, hour in jobs:
                self.add(group, job, 'missed', sev, 0, hour[0], hour[1], 'job missed',
                         f'batch job {job} in {group or "-"} did not run in the last 60 min. It ran {fmt(expected[0])} times in the {self.window()} a day ago '
                         f'and {fmt(expected[1])} times a week ago; in the whole same hour it ran {dash(hour[0])} times a day ago and {dash(hour[1])} times a week ago', 1,
                         f'{sev.upper()} batch job {job} missed (same hour 1d {dash(hour[0])} / 7d {dash(hour[1])} runs)')

    def slow(self, group: str, job: str, avg: list, interval) -> None:
        rules = self.rules
        bases = [b for b in avg[1:] if b is not None]
        if avg[0] is None or not bases or not interval:
            return
        usual = max(bases)
        if avg[0] < rules['slow_x'] * usual or avg[0] - usual < rules['slow_s']:
            return
        share = 100 * avg[0] / interval
        sev = 'warn' if share >= rules['slow_warn'] else 'info' if share >= rules['slow_info'] else None
        if sev is None:
            return
        self.add(group, job, 'slow', sev, avg[0], avg[1], avg[2], 'slow runs',
                 f'runs of batch job {job} in {group or "-"} took {secs(avg[0])} on average in the last 60 min, against {secs(avg[1])} a day ago and {secs(avg[2])} '
                 f'a week ago; that is {fmt(share, "%")} of its interval of {secs(interval)}', rules['slow_s'],
                 f'{sev.upper()} batch job {job} runs take {secs(avg[0])} (1d {secs(avg[1])} / 7d {secs(avg[2])})')


def average(total, runs) -> float | None:
    return None if total is None or runs is None or runs < 0.5 else round(total / runs, 2)


def evaluate(data: dict, instance: str, n: dict[str, str], rules: dict, ui: str) -> tuple[list[dict], list[dict]]:
    rd = Reading(data, n)
    runs, fails, total, mx = rd.trio('runs'), rd.trio('fails'), rd.trio('total'), rd.trio('max')
    last, day = rd.per_job('last', 0), rd.per_job('day', 0)
    expected = rd.trio('expected')
    jobs = sorted(set().union(*runs, *fails, *total, *mx, *expected, last, day))
    if not runs[0]:
        raise NoSeries(f"Prometheus holds no {n['metric']}_count series")
    cards, rows = Cards(instance, rules, n, ui), []
    for job in jobs:
        group, name = job
        r = [count(x.get(job)) for x in runs]
        f = [count(x.get(job)) for x in fails]
        avg = [average(total[i].get(job), runs[i].get(job)) for i in range(len(runs))]
        d = count(day.get(job))
        interval = round(86400 / d, 1) if d else None
        rows.append(dict(part='job', name=instance, group=group, job=name, runs=r[0], runsD1=r[1], runsD7=r[2], fails=f[0], failsD1=f[1], failsD7=f[2],
                         avg=avg[0], avgD1=avg[1], avgD7=avg[2], max=mx[0].get(job), maxD1=mx[1].get(job), maxD7=mx[2].get(job), day=d, interval=interval,
                         last=last.get(job)))
        cards.failing(group, name, r, f, d)
        cards.check_missed(group, name, r[0], [count(x.get(job)) for x in expected[1:]], r[1:])
        cards.slow(group, name, avg, interval)
    cards.roll_missed()
    return cards.found, rows


def refused(e: PrometheusError, config) -> Sweep:
    card = access.card(ID, NAME, PART, status=e.code, credential=credential(config), name=PART_NAME)
    return Sweep(PassResult(ID, True, access.status('ok', [PART_NAME]), [card], {}, (access.ran_key(ID, PART),)))


def run(ctx: Context) -> Sweep:
    n = names_of(ctx.instance.config)
    bad = problem(n)
    if bad:
        return Sweep(PassResult(ID, False, f'error: {bad}'))
    peer = ctx.peer('prometheus')
    if peer is None:
        return Sweep(PassResult(ID, False, f'error: {NO_PEER}'))
    rules = {**RULES, **ctx.rules('rules')}
    rules['drift_min'] = drift_of(rules)
    ui = ui_of(peer.info.config)
    try:
        data = fetch(client_for(peer.info, bounded(peer.http(), BUDGET_S)), queries(n, rules['drift_min'] * 60), ctx.now, rules['drift_min'] * 60)
        found, rows = evaluate(data, ctx.instance.name, n, rules, ui)
    except Late:
        return Sweep(PassResult(ID, False, 'error: timed out'))
    except NoSeries as e:
        return Sweep(PassResult(ID, False, f'error: {e.reason}'))
    except PrometheusError as e:
        if access.denied(e.code):
            return refused(e, peer.info.config)
        return Sweep(PassResult(ID, False, f'error: {e.reason}'))
    head = {'part': 'summary', 'name': ctx.instance.name, 'jobs': len(rows), 'groups': len({r['group'] for r in rows}), 'metric': n['metric'],
            'selector': n['selector'], 'at': iso(ctx.now), 'source': peer.info.name, 'ui': ui}
    return Sweep(PassResult(ID, True, 'ok', found, {}, (ID, access.ran_key(ID, PART))), sections={ID: rows + [head]})


def plural(k: int, word: str) -> str:
    return f'{k} {word}' + ('' if k == 1 else 's')


def test(ctx: Context) -> TestResult:
    def ask() -> str:
        n = names_of(ctx.instance.config)
        bad = problem(n)
        if bad:
            raise ReadError(400, bad)
        peer = ctx.peer('prometheus')
        if peer is None:
            raise ReadError(400, NO_PEER)
        try:
            found = instant(peer, jobs_query(n))
        except HttpError as e:
            said = access.tested(NAME, PART_NAME, e.code, credential=credential(peer.info.config))
            if said:
                raise ReadError(e.code, said) from None
            raise
        if not found:
            return f"Prometheus {peer.info.name} answered, but it holds no {n['metric']}_count series"
        groups = {str((r.get('metric') or {}).get(n['group'], '')) for r in found}
        return f"{plural(len(found), 'batch job')} in {plural(len(groups), 'group')} through Prometheus {peer.info.name}"

    return connection.result(ask)


def links(card: dict, info) -> list[dict] | None:
    if card.get('src') != ID:
        return None
    return [{'label': u['label'], 'url': u['url']} for u in card.get('urls') or []
            if isinstance(u, dict) and isinstance(u.get('label'), str) and isinstance(u.get('url'), str) and u['url'].startswith('https://')]


CARD_TYPES = (CardType('example-batch-jobs-failing', 'Example batch job failing'), CardType('example-batch-jobs-missed', 'Example batch job missed'), CardType('example-batch-jobs-slow', 'Example batch job slow'))

PLUGIN = Plugin(
    id=ID, name=NAME, version='0.1.0', category='infrastructure',
    description='Example plug-in, a simpler copy of the built-in Batch jobs: scheduled batch jobs read through a Prometheus instance: failing, missed and slow jobs against the same hour 1 and 7 days earlier.',
    icon='icon.svg', author='Skopax', licence='Apache-2.0', core='>=0.2,<0.6', uses=('prometheus',),
    fields=(Field('prometheus', 'Prometheus', 'instance', required=True, choices=('prometheus',), help='The Prometheus instance that holds the batch job timers.'),
            Field('metric', 'Metric', 'text', default=DEFAULTS['metric'],
                  help='The timer of each run, without its suffix: the plug-in reads its _count, _sum and _max series.'),
            Field('group', 'Group label', 'text', default=DEFAULTS['group'], help='The label that names the group of a job, such as its namespace or team.'),
            Field('job', 'Job label', 'text', default=DEFAULTS['job'], help='The label that names the job. Prometheus usually renames a label called job or namespace on scraped series (to exported_job, or by relabelling) unless the scrape sets honor_labels, so the default is job_name.'),
            Field('result', 'Result label', 'text', default=DEFAULTS['result'], help='The label that holds the outcome of a run, such as success or failure.'),
            Field('failure', 'Failure values', 'text', default=DEFAULTS['failure'], help='The values of the result label that mean a failed run, joined by |, such as failure|error.'),
            Field('selector', 'Selector', 'text', default=DEFAULTS['selector'],
                  help='Optional label matchers that every query adds, such as env="prod". Each one is label, operator, quoted value; empty reads every job.')),
    test=test,
    checks=(Check(ID, 'Example batch jobs', 300, 90, (ID,), run, sections=(ID,), section_help=SECTION_HELP),),
    rules=(RuleSet('rules', 'Batch job thresholds', dict(RULES), RULE_HELP),),
    links=links,
    card_types=CARD_TYPES,
    knowledge=(Knowledge(ID, NAME, KNOWLEDGE, slack=True),),
    panels=(Panel(ID, 'panel.js', label='Example batch jobs', group='observe'),),
)
