import concurrent.futures
import datetime as dt
import logging
import time
from typing import Callable

from skopax import clock
from skopax.config import RELEASE_NAME, RELEASE_PLACEHOLDER
from skopax.contract import SourceDown
from skopax.errkind import NoAccess, kind as error_kind, text_of
from skopax.kit.promql import Prometheus, signal_queries
from skopax.messages import msg
from skopax.settle import RELEASE_GAP_HOURS, ignored, when
from skopax.store import ActionMoved, Store, iso

log = logging.getLogger('skopax.postdeploy')

JOB_PERIOD = 120
JOB_OFFSET = 45
ACTION_SCAN = 50
LOOKBACK_HOURS = 6
BASELINES = (1, 7)
WORKERS = 12
MIN_WINDOW_S = 300
RATE_SIGNALS = {'err': 10, 'warn': 50, '5xx': 5, 'out-err': 5, 'gw-err': 20, 'ing-5xx': 10, 'hikari-tmo': 1, 'kafka-lag': 1000}
NOW_ONLY = ('restarts', 'unavail')
RESULT_LIMIT = 300
POINTS = 10
SLACK_ROWS = 3
SERVER_BY = 'server'
COST_NOTE = 'Prometheus and Jira queries only, no model'
NUMBERS_BY = 'server · numbers'
WAIT_WINDOW_S = 1800


def utc(t: float) -> dt.datetime:
    return dt.datetime.fromtimestamp(t, dt.timezone.utc)


def queries(ns: str, svc: str, window: str) -> dict[str, str]:
    sel = f'namespace="{ns}",job="{svc}"'
    return {'err': f'sum(increase(log4j2_events_total{{{sel},level="error"}}[{window}]))',
            'warn': f'sum(increase(log4j2_events_total{{{sel},level="warn"}}[{window}]))', **signal_queries(ns, svc, window)}


def scalar(client: Prometheus, expr: str, at: float) -> float:
    return sum(v for _, v in client.vec(expr, at))


def issue_order(key: str) -> tuple[str, int]:
    project, _, number = key.partition('-')
    return project, int(number) if number.isdigit() else 0


def rows_text(rows: list[dict]) -> str:
    return '\n'.join(f"[{'WARN' if r['flags'] else 'ok'}] {r['ns']}/{r['svc']}  {r['metrics']}" + (f"  << {'; '.join(r['flags'])}" if r['flags'] else '') for r in rows)


def report_text(rows: list[dict]) -> str:
    return rows_text(rows)[:20000]


def release_name(template: str, project: str, day: dt.datetime) -> str:
    parts = {'project': project, 'yyyy': f'{day.year:04d}', 'mm': f'{day.month:02d}', 'dd': f'{day.day:02d}'}
    return RELEASE_PLACEHOLDER.sub(lambda m: parts.get(m.group(1), m.group(0)), template)


def slack_text(rows: list[dict], frm: float, now: float, flags: list[str] | None = None) -> str:
    flagged = [r for r in rows if r['flags']]
    first = min(when(r['at']) for r in rows if r.get('at')) if any(r.get('at') for r in rows) else utc(frm)
    span = f"{clock.hm(first)}–{clock.hm(utc(frm))}" if clock.hm(first) != clock.hm(utc(frm)) else clock.hm(utc(frm))
    head = msg('postdeploy.head', date=clock.date(utc(frm), hm=False), count=len(rows), span=span, zone=clock.label(utc(frm)), clean=len(rows) - len(flagged))
    if flagged:
        head += msg('postdeploy.flagged', n=len(flagged), list='; '.join(f"{r['svc']} ({', '.join(r['flags'])})" for r in flagged[:SLACK_ROWS])) + (msg('postdeploy.more') if len(flagged) > SLACK_ROWS else '')
    if flags:
        head += msg('postdeploy.plugin_flags', list='; '.join(flags[:SLACK_ROWS])) + (msg('postdeploy.more') if len(flags) > SLACK_ROWS else '')
    return head + msg('postdeploy.window', minutes=max(MIN_WINDOW_S, int(now - frm)) // 60)


class PostDeploy:
    def __init__(self, store: Store, prom_for: Callable[[str | None], Prometheus | None], jira: object, flag: bool | Callable[[], bool] = True, base_url: str = '', clock=time.time,
                 assess: Callable[[], bool] | None = None, projects: tuple[str, ...] = (), serving: Callable[[], bool] | None = None,
                 primary_name: Callable[[], str | None] = lambda: None, release_template: Callable[[], str] = lambda: RELEASE_NAME,
                 plugin_reports: Callable[[list[dict], float, float], list[dict]] | None = None, autopost: Callable[[], bool] = lambda: True):
        self.plugin_reports = plugin_reports
        self.autopost = autopost
        self.projects = projects
        self.release_template = release_template
        self.primary_name = primary_name
        self.store = store
        self.assess = assess
        self.prom_for = prom_for
        self.serving = serving if serving is not None else (lambda: prom_for(None) is not None)
        self.jira = jira
        self.flag = flag
        self.base_url = base_url
        self.clock = clock

    @property
    def enabled(self) -> bool:
        return bool(self.flag() if callable(self.flag) else self.flag) and self.serving()

    def run_once(self, now: float | None = None) -> dict | None:
        now = self.clock() if now is None else now
        for act in reversed(self.store.list_actions('requested', ACTION_SCAN)):
            if act.get('type') != 'postdeploy':
                continue
            try:
                self.store.update_action(act['id'], 'processing', None, 'requested')
            except ActionMoved:
                continue
            answers = self.plugin_answers(act, now)
            if self.waiting(act, answers, now) and self.handed_back(act):
                continue
            try:
                return self.check(act, now, answers)
            except Exception as e:
                log.error('post-deploy check for action %s failed: %s', act['id'], error_kind(e))
                self.store.update_action(act['id'], 'failed', error_kind(e)[:RESULT_LIMIT], 'processing')
                self.store.add_activity('postdeploy', SERVER_BY, f'post-deploy check failed \u00b7 {error_kind(e)}', error=error_kind(e))
                return None
        return None

    def targets(self, act: dict, now: float) -> list[dict]:
        skip = ignored(self.store)
        first = when((act.get('release') or {}).get('firstAt'))
        if not first:
            return [d for d in self.store.deploys_since(iso(utc(now) - dt.timedelta(hours=LOOKBACK_HOURS))) if d.get('svc') not in skip]
        wave: list[dict] = []
        for d in self.store.deploys_since(iso(first - dt.timedelta(minutes=5))):
            t = when(d.get('at'))
            if t is None or (wave and t - when(wave[-1]['at']) > dt.timedelta(hours=RELEASE_GAP_HOURS)):
                break
            wave.append(d)
        return [d for d in wave if d.get('svc') not in skip]

    def plugin_answers(self, act: dict, now: float) -> list[dict]:
        if self.plugin_reports is None:
            return []
        try:
            rows = [d for d in self.targets(act, now) if when(d.get('at'))]
            if not rows:
                return []
            started = min(when(d['at']).timestamp() for d in rows)
            return list(self.plugin_reports(rows, started, now))
        except Exception as e:
            log.warning('post-deploy plug-in answers left out: %s', error_kind(e))
            return []

    def waiting(self, act: dict, answers: list[dict], now: float) -> bool:
        until = max((a['wait_until'] for a in answers if a.get('wait_until') is not None), default=None)
        since = when(act.get('requestedAt'))
        if until is None or since is None:
            return False
        return now < min(until, since.timestamp() + WAIT_WINDOW_S)

    def handed_back(self, act: dict) -> bool:
        try:
            self.store.hand_back_action(act['id'])
        except Exception as e:
            log.warning('post-deploy check for action %s could not wait, its report is written now: %s', act['id'], error_kind(e))
            return False
        return True

    def check(self, act: dict, now: float, answers: list[dict] | None = None) -> dict | None:
        rows = [d for d in self.targets(act, now) if when(d.get('at'))]
        if not rows:
            self.store.update_action(act['id'], 'done', f'no image change in the last {LOOKBACK_HOURS} h', 'processing')
            self.store.add_activity('postdeploy', SERVER_BY, f'post-deploy check: no image change in the last {LOOKBACK_HOURS} h')
            return None
        frm = max(when(d['at']).timestamp() for d in rows)
        window = f'{max(MIN_WINDOW_S, int(now - frm))}s'
        clients = {c: self.prom_for(c) for c in {d.get('cluster') or None for d in rows}}
        plan: dict[tuple[str | None, str, float], tuple[Prometheus, str, float]] = {}
        for d in rows:
            cluster = d.get('cluster') or None
            if clients[cluster] is None:
                continue
            for name, expr in queries(d['ns'], d['svc'], window).items():
                for t in ([now] if name in NOW_ONLY else [now] + [now - o * 86400 for o in BASELINES]):
                    plan[(cluster, expr, t)] = (clients[cluster], expr, t)
        keys = list(plan)
        with concurrent.futures.ThreadPoolExecutor(min(WORKERS, max(1, len(keys)))) as ex:
            values = list(ex.map(lambda k: scalar(*plan[k]), keys))
        got = dict(zip(keys, values))
        out = []
        for d in rows:
            metrics, flags = [], []
            cluster = d.get('cluster') or None
            if clients[cluster] is None:
                flags.append(msg('reports.no_prometheus', instance=cluster or self.primary_name() or 'primary'))
            for name, expr in (queries(d['ns'], d['svc'], window).items() if clients[cluster] is not None else []):
                cur = got[(cluster, expr, now)]
                if name in NOW_ONLY:
                    if cur > 0:
                        flags.append(f'{name}={cur:.0f}')
                    continue
                base = [got[(cluster, expr, now - o * 86400)] for o in BASELINES]
                if cur == 0 and not any(base):
                    continue
                metrics.append(f"{name} {cur:.0f} ({'/'.join(f'{b:.0f}' for b in base)})")
                if name in RATE_SIGNALS and cur > max(2 * max(base), max(base) + RATE_SIGNALS[name]):
                    flags.append(f'{name} up')
            row = {'ns': d['ns'], 'svc': d['svc'], 'at': d['at'], 'flags': flags, 'metrics': ', '.join(metrics), 'issues': sorted(d.get('tickets') or [])}
            for field in ('fromImage', 'toImage', 'cluster'):
                if d.get(field):
                    row[field] = d[field]
            out.append(row)
        flagged = [r for r in out if r['flags']]
        summary = f"{len(out)} deploys since {clock.hm(utc(frm))} {clock.label(utc(frm))}: {len(out) - len(flagged)} clean" + (f', {len(flagged)} flagged' if flagged else '')
        releases = [release_name(self.release_template(), p, clock.local(utc(frm))) for p in self.projects]
        assessed = bool(self.assess and self.assess())
        issues, refused = self.released(out, releases)
        report = {'summary': summary, 'brief': {'level': 'warn' if flagged else 'ok', 'headline': summary, 'points': [f"{r['svc']}: {', '.join(r['flags'])}" for r in flagged][:POINTS]},
                  'windowFrom': iso(utc(frm)), 'windowTo': iso(utc(now)), 'rows': out, 'releases': releases, 'issues': issues, 'text': report_text(out),
                  'actionId': act['id'], 'usd': 0.0, 'costNote': COST_NOTE} | ({'issuesNote': refused} if refused else {})
        report = self.with_plugins(report, answers or [])
        saved = self.store.add_deploy_report(report)
        posting = self.autopost()
        if posting:
            self.store.report_slack_draft(saved['id'], slack_text(out, frm, now, report.get('flags')), NUMBERS_BY if assessed else SERVER_BY)
        self.store.update_action(act['id'], 'done', summary[:RESULT_LIMIT], 'processing')
        if assessed:
            self.queue_assessment(saved)
        line = f'post-deploy check · {summary} · ' + ('Slack auto-send is off, not posted' if not posting else 'assessment queued for the investigator, its post waits' if assessed else 'Slack post queued')
        self.store.add_activity('postdeploy', SERVER_BY, line, reportId=saved['id'])
        log.info('post-deploy report %s for action %s: %s', saved['id'], act['id'], summary)
        return saved

    def with_plugins(self, report: dict, answers: list[dict]) -> dict:
        if not answers:
            return report
        flags = [f"{a['plugin']}: {f}" for a in answers for f in a['flags']]
        text = report['text'] + ''.join(f"\n[{a['plugin']}] {a['text']}" for a in answers)
        brief = dict(report['brief'])
        if flags:
            brief = {**brief, 'level': 'warn', 'points': (brief['points'] + flags)[:POINTS]}
        return {**report, 'plugins': {a['id']: a['text'] for a in answers}, 'flags': flags, 'brief': brief, 'text': text[:20000]}

    def queue_assessment(self, report: dict) -> None:
        self.store.add_action({'type': 'assess', 'key': f"report:{report['id']}", 'target': f"report:{report['id']}", 'reportId': report['id'],
                               'label': f"Assess deploy report: {report.get('summary')}"[:300], 'requestedBy': SERVER_BY})

    def released(self, rows: list[dict], releases: list[str]) -> tuple[list[dict], str | None]:
        keys = sorted({k for r in rows for k in r.get('issues') or []}, key=issue_order)
        if not self.jira.enabled:
            return [{'key': k} for k in keys], None
        names = ', '.join(f'"{r}"' for r in releases)
        clauses = ([f'fixVersion in ({names})'] if releases else []) + ([f'key in ({", ".join(keys)})'] if keys else [])
        if not clauses:
            return [], None
        jql = ' OR '.join(clauses) + ' ORDER BY key'
        try:
            found = self.jira.search(jql, ['summary', 'status', 'fixVersions'], 200)
        except NoAccess as e:
            log.warning('released issues refused: %s', error_kind(e))
            return [{'key': k} for k in keys], text_of(e)
        except SourceDown as e:
            log.warning('released issues not read: %s', error_kind(e))
            return [{'key': k} for k in keys], None
        fields = {i['key']: i.get('fields') or {} for i in found if i.get('key')}
        wanted = set(releases)
        out = []
        for k in sorted(set(keys) | set(fields), key=issue_order):
            item: dict = {'key': k}
            f = fields.get(k)
            if f is not None:
                if f.get('summary'):
                    item['summary'] = str(f['summary'])[:300]
                if (f.get('status') or {}).get('name'):
                    item['status'] = str(f['status']['name'])[:60]
                item['inRelease'] = bool(wanted & {v.get('name') for v in f.get('fixVersions') or []})
            out.append(item)
        return out, None
