import asyncio
import datetime as dt
import logging
import threading
from typing import Callable

from pymongo.errors import PyMongoError

from skopax import clock, information, severity
from skopax.engine import SEV_RANK, Applied, fmt, iso as epoch_iso, parse_iso
from skopax.errkind import Authored, kind as error_kind
from skopax.investigator.select import EVENT_WINDOW_S, announced_twin, merged, own_findings, stamps
from skopax.messages import msg
from skopax.postdeploy import NUMBERS_BY, SERVER_BY
from skopax.slack import SlackError, cut
from skopax.store import NotFound, Store, draft_hash, iso, utc_now

log = logging.getLogger('skopax.autosend')

INTERVAL_SECONDS = 30
STOPPED_TRIAGE = ('progress', 'handled')
LIVE_WHEN_CLEARED = severity.CARD_LEVELS
SEND_ERRORS = (SlackError, PyMongoError, NotFound)
FALLBACK_SEV = 'crit'
FALLBACK_SKIP_TRIAGE = ('investigating',) + STOPPED_TRIAGE
FALLBACK_TRIGGERS = ('cards', 'action')
FALLBACK_COVERED = ('done', 'running')
POST_NOW_BURST = 3
SEV_WORDS = severity.WORDS
SUB_LIMIT = 80
SWEEP_AGE = dt.timedelta(hours=1)
STALE_CLAIM = dt.timedelta(minutes=15)
SWEEP_STALE = dt.timedelta(minutes=10)
OLD = 'old'
SWEEP_MARK = 'slack-direct-sweep'


def failure_of(e: BaseException) -> str:
    return e.text if isinstance(e, Authored) else msg('cards.send_failed', kind=error_kind(e))


def sent(x: dict) -> bool:
    draft, post = x.get('slackDraft') or {}, x.get('slackPost') or {}
    return bool(draft.get('at') and post.get('at') and post['at'] >= draft['at'])


def armed_for(x: dict) -> dict | None:
    draft, auto = x.get('slackDraft') or {}, x.get('autoSend') or {}
    if not draft.get('at') or not draft.get('text') or sent(x):
        return None
    same = auto.get('draftAt') == draft['at'] and auto.get('draftHash') == draft_hash(draft['text'])
    return draft if same else None


def final_failure(x: dict) -> bool:
    auto = x.get('autoSend') or {}
    return bool(auto.get('failedAt') and auto.get('claimedAt'))


def on_its_way(x: dict) -> bool:
    auto = x.get('autoSend') or {}
    if bool(auto.get('claimedAt')) and not auto.get('failedAt') and not sent(x):
        return True
    return bool((x.get('slackDraft') or {}).get('text')) and not sent(x) and bool(auto.get('at')) and not auto.get('holdBy') and not final_failure(x)


def too_old(x: dict) -> bool:
    auto = x.get('autoSend') or {}
    if auto.get('force') or auto.get('retries'):
        return False
    return str((x.get('slackDraft') or {}).get('at') or '') < iso(utc_now() - SWEEP_AGE)


def kept(x: dict) -> bool:
    return bool((x.get('slackDraft') or {}).get('text')) and not sent(x) and not on_its_way(x)


def waiting(x: dict) -> bool:
    return on_its_way(x)


def said_at_once(store: Store, key: str) -> None:
    store.add_activity('slack', SERVER_BY, f'Card posted to Slack at once with its numbers \u00b7 {key}', key=key)
    log.info('card %s posted to Slack at once', key)


class AutoSend:
    def __init__(self, store: Store, send_anomaly: Callable[[str], dict], send_report: Callable[[str], dict], interval: float = INTERVAL_SECONDS, on: Callable[[], bool] = lambda: True,
                 ready: Callable[[], bool] = lambda: True):
        self.store = store
        self.send_anomaly = send_anomaly
        self.send_report = send_report
        self.interval = interval
        self.on = on
        self.ready = ready
        self.told_off = False
        self.woken = threading.Event()

    def wake(self) -> None:
        self.woken.set()

    def merged(self, a: dict) -> bool:
        parent = a.get('parent')
        if not parent or parent == a.get('key'):
            return False
        try:
            host = self.store.anomaly(parent)
        except NotFound:
            return False
        return information.is_card(host) and (host.get('status') == 'open' or a.get('status') == 'cleared')

    def live(self, a: dict, force: bool = False) -> bool:
        if not information.is_card(a) or a.get('muted') or (a.get('triage') in STOPPED_TRIAGE and not force) or self.merged(a):
            return False
        return a.get('status') == 'open' or (a.get('status') == 'cleared' and a.get('sev') in LIVE_WHEN_CLEARED)

    def _anomaly(self, a: dict, now: str) -> int:
        key, draft = a.get('key'), armed_for(a)
        if draft and too_old(a):
            self.drop_old('card', key, now)
            return 0
        if not draft or not self.live(a, bool((a.get('autoSend') or {}).get('force'))):
            log.info('Slack post dropped on %s: the card or its text moved on', key)
            self.store.stop_anomaly_auto_send(key)
            return 0
        if self.store.claim_anomaly_auto_send(key, draft, now) is None:
            return 0
        try:
            self.send_anomaly(key)
        except SEND_ERRORS as e:
            log.warning('Slack post on %s failed: %s', key, error_kind(e))
            self.store.anomaly_auto_send_failed(key, failure_of(e))
            return 0
        fallback = a.get('slackFallback') or {}
        if fallback.get('atOnce') and fallback.get('at') == draft.get('at') and not (a.get('autoSend') or {}).get('retries'):
            said_at_once(self.store, key)
            return 1
        log.info('Slack post sent on %s', key)
        self.store.add_activity('slack', 'auto', f'Slack post sent \u00b7 {key}', key=key)
        return 1

    def _report(self, r: dict, now: str) -> int:
        report_id, draft = r.get('id'), armed_for(r)
        if draft and too_old(r):
            self.drop_old('report', report_id, now)
            return 0
        if not draft:
            log.info('Slack post dropped on report %s: its text moved on', report_id)
            self.store.stop_report_auto_send(report_id)
            return 0
        if self.store.claim_report_auto_send(report_id, draft, now) is None:
            return 0
        try:
            self.send_report(report_id)
        except SEND_ERRORS as e:
            log.warning('Slack post on report %s failed: %s', report_id, error_kind(e))
            self.store.report_auto_send_failed(report_id, failure_of(e))
            return 0
        log.info('Slack post sent on report %s', report_id)
        self.store.add_activity('slack', 'auto', f'Slack post sent \u00b7 report {report_id}', reportId=report_id)
        return 1

    def skip(self, kind: str, doc_id: str, now: str) -> bool:
        if not self.store.skip_auto_send(kind, doc_id, now):
            return False
        where = f'report {doc_id}' if kind == 'report' else doc_id
        log.info('Slack post skipped on %s: auto-send is off', where)
        self.store.add_activity('slack', 'auto', f'Slack post skipped: auto-send is off \u00b7 {where}', **({'reportId': doc_id} if kind == 'report' else {'key': doc_id}))
        return True

    def drop_old(self, kind: str, doc_id: str, now: str) -> None:
        if not self.store.skip_auto_send(kind, doc_id, now, OLD):
            return
        where = f'report {doc_id}' if kind == 'report' else doc_id
        log.info('Slack post dropped on %s: its text is over an hour old and was never sent', where)
        self.store.add_activity('slack', 'auto', f'Slack post skipped: the text was over an hour old \u00b7 {where}', **({'reportId': doc_id} if kind == 'report' else {'key': doc_id}))

    def send_soon(self, key: str) -> str:
        now = iso(utc_now())
        if not self.on():
            return 'skipped' if self.skip('card', key, now) else 'nothing to post'
        if not self.store.requeue_anomaly_post(key, now):
            return 'busy'
        self.wake()
        return 'queued'

    def run_once(self) -> int:
        now = iso(utc_now())
        if not self.on():
            if not self.told_off:
                self.told_off = True
                log.info('Slack posts are skipped: auto-send is off')
            for a in self.store.due_anomaly_auto_sends(now):
                self.skip('card', a['key'], now)
            for r in self.store.due_report_auto_sends(now):
                self.skip('report', r['id'], now)
            return 0
        if not self.ready():
            return 0
        self.told_off = False
        if self.store.release_stale_post_claims(iso(utc_now() - STALE_CLAIM), now, iso(utc_now() - SWEEP_AGE)):
            log.warning('Slack posts claimed more than %d min ago were not sent; they go back to the queue', STALE_CLAIM.seconds // 60)
        count = sum(self._anomaly(a, now) for a in self.store.due_anomaly_auto_sends(now))
        return count + sum(self._report(r, now) for r in self.store.due_report_auto_sends(now))

    def sweep(self) -> int:
        if not self.store.begin_once(SWEEP_MARK, iso(utc_now() - SWEEP_STALE)):
            return 0
        sent_now = self._sweep()
        self.store.finish_once(SWEEP_MARK)
        return sent_now

    def _sweep(self) -> int:
        now = utc_now()
        young, on, sent_now = iso(now - SWEEP_AGE), self.on(), 0
        for kind, docs in (('card', self.store.waiting_slack_drafts('card')), ('report', self.store.waiting_slack_drafts('report'))):
            for x in docs:
                draft, auto = x.get('slackDraft') or {}, x.get('autoSend') or {}
                doc_id = x['id'] if kind == 'report' else x['key']
                if sent(x) or auto.get('claimedAt') or draft.get('notAutoSent') or (kind == 'report' and draft.get('by') == NUMBERS_BY and str(draft.get('at') or '') >= young):
                    continue
                fresh = armed_for(x) and not auto.get('holdBy') and str(draft.get('at') or '') >= young and (kind == 'report' or self.live(x))
                if fresh and not on:
                    self.skip(kind, doc_id, iso(now))
                    continue
                if fresh and self.store.rearm_auto_send(kind, doc_id, draft['at'], iso(now)):
                    sent_now += 1
                    continue
                if not fresh:
                    self.store.drop_slack_draft(kind, doc_id, str(draft.get('at') or ''))
        if sent_now:
            log.info('%d waiting Slack post%s from before the upgrade go%s out now', sent_now, '' if sent_now == 1 else 's', 'es' if sent_now == 1 else '')
        return sent_now

    async def loop(self) -> None:
        try:
            await asyncio.to_thread(self.sweep)
        except Exception as e:
            log.warning('Slack draft sweep at start failed: %s', error_kind(e))
        while True:
            try:
                await asyncio.to_thread(self.woken.wait, self.interval)
            except asyncio.CancelledError:
                self.woken.set()
                raise
            self.woken.clear()
            try:
                await asyncio.to_thread(self.run_once)
            except Exception as e:
                log.warning('auto-send pass failed: %s', error_kind(e))


def shown(v: float | None, unit: str) -> str:
    if v is None:
        return '–'
    if unit == 'dur':
        return msg('autosend.dur.min', n=round(v / 60)) if v < 3600 else msg('autosend.dur.hour', n=f'{v / 3600:.1f}')
    return fmt(v, unit)


def plain(text) -> str:
    return ' '.join(str(text).split()).replace('<', '‹').replace('>', '›')


def fallback_text(a: dict, fired_at: str, last_key: str | None = 'autosend.fallback.last') -> str:
    unit = a.get('unit') or ''
    what = ' · '.join([plain(a.get('svc') or '?'), plain(a.get('label') or '?')] + ([cut(plain(a['sub']), SUB_LIMIT)] if a.get('sub') else []))
    base = '' if a.get('d1') is None else msg('autosend.base.d1', d1=shown(a['d1'], unit)) + ('' if a.get('d7') is None else msg('autosend.base.d7', d7=shown(a['d7'], unit))) + ')'
    since_at = parse_iso(a.get('firstSeen') or fired_at)
    since = clock.date(since_at)
    text = msg('autosend.fallback.body', what=what, now=shown(a.get('now'), unit), base=base, since=since, zone=clock.label(since_at), last=msg(last_key) if last_key else '')
    return text if last_key else text.rstrip()


def investigated(runs: list[dict], key: str, fired_at: str) -> bool:
    return any(r.get('outcome') in FALLBACK_COVERED and r.get('trigger') in FALLBACK_TRIGGERS and key in (r.get('keys') or []) and (r.get('startedAt') or '') >= fired_at for r in runs)


class Fallback:
    def __init__(self, store: Store, minutes: int, wake: Callable[[], None] = lambda: None, on: Callable[[], bool] = lambda: True):
        self.store = store
        self.minutes = minutes
        self.wake = wake
        self.on = on

    @property
    def enabled(self) -> bool:
        return self.minutes > 0

    def due(self, state: dict, runs: list[dict], now: float) -> list[tuple[dict, str]]:
        by_key = {a['key']: a for a in state.get('anomalies') or []}
        picked = []
        own = own_findings()
        for key, fired_at in stamps(state.get('history') or []).items():
            a = by_key.get(key)
            if not a or a.get('status') != 'open' or a.get('muted') or a.get('sev') != FALLBACK_SEV or merged(a, by_key) or a.get('src') in own:
                continue
            if on_its_way(a) or (a.get('slackPost') or {}).get('at') or (a.get('autoSend') or {}).get('holdBy') or a.get('triage') in FALLBACK_SKIP_TRIAGE:
                continue
            age = now - parse_iso(fired_at)
            if age < self.minutes * 60 or age > EVENT_WINDOW_S or (a.get('slackFallback') or {}).get('firedAt') == fired_at or investigated(runs, key, fired_at):
                continue
            picked.append((a, fired_at))
        picked.sort(key=lambda p: (p[0].get('firstSeen') or p[1], p[0]['key']))
        return picked

    def run_once(self, now: float) -> int:
        if not self.enabled or not self.on():
            return 0
        state = self.store.load()
        cards = state.get('anomalies') or []
        runs = self.store.recent_investigations('live', epoch_iso(now - EVENT_WINDOW_S))
        count = 0
        for a, fired_at in self.due(state, runs, now):
            if announced_twin(a, cards, now):
                continue
            saved = self.store.fallback_slack_draft(a['key'], fallback_text(a, fired_at), SERVER_BY, fired_at, dt.datetime.fromtimestamp(now, dt.timezone.utc), FALLBACK_SKIP_TRIAGE)
            if saved is None:
                continue
            a.update(saved)
            minutes = int((now - parse_iso(fired_at)) // 60)
            log.info('fallback Slack post queued on %s: no finished investigation %d min after it fired', a['key'], minutes)
            self.store.add_activity('slack', SERVER_BY, f"Slack fallback post queued · {a['key']} · no finished investigation {minutes} min after it fired", key=a['key'])
            count += 1
        if count:
            self.wake()
        return count


def fresh(applied: Applied, now: float) -> list[dict]:
    if applied.first:
        return []
    return [a for a in applied.push if information.is_card(a)]


def severities(cards: list[dict]) -> str:
    counts = {sev: sum(1 for a in cards if a.get('sev') == sev) for sev in SEV_WORDS}
    return ', '.join(f'{n} {word}' for sev, word in SEV_WORDS.items() if (n := counts[sev]))


class PostNow:
    def __init__(self, store: Store, send: Callable[[str], object], floor: Callable[[], str], investigating: Callable[[], bool] = lambda: True):
        self.store = store
        self.send = send
        self.floor = floor
        self.investigating = investigating

    def last_line(self, a: dict, investigating: bool) -> str | None:
        if not investigating:
            return None
        return 'autosend.crit_now.last' if a.get('sev') == FALLBACK_SEV else 'autosend.post_now.warn_last'

    def due(self, pushed: list[dict], cards: list[dict], now: float, floor: str | None = None) -> list[dict]:
        floor = floor or self.floor()
        if floor not in SEV_RANK:
            return []
        by_key = {c['key']: c for c in cards}
        picked = []
        own = own_findings()
        for p in pushed:
            a = by_key.get(p['key'], p)
            if a.get('sev') not in information.CARD_SEVS or SEV_RANK.get(a.get('sev'), -1) < SEV_RANK[floor] or a.get('status') != 'open' or a.get('muted') or merged(a, by_key) or a.get('src') in own:
                continue
            if on_its_way(a) or (a.get('slackPost') or {}).get('at') or (a.get('autoSend') or {}).get('holdBy') or a.get('triage') in STOPPED_TRIAGE:
                continue
            if announced_twin(a, cards, now):
                continue
            picked.append(a)
        return picked

    def run(self, pushed: list[dict], cards: list[dict], now: float) -> list[str]:
        if not pushed:
            return []
        floor = self.floor()
        if floor not in SEV_RANK:
            return []
        due = self.due(pushed, cards, now, floor)
        investigating = self.investigating()
        if len(due) > POST_NOW_BURST:
            grouped = investigating
            self.store.add_activity('slack', SERVER_BY, f'{len(due)} cards opened together ({severities(due)}); not posted at once' + (', the investigator groups them first' if grouped else ''))
            log.info('%d cards opened together (%s), not posted at once', len(due), severities(due))
            return []
        posted = []
        for a in due:
            fired_at = a.get('pushedAt') or a.get('firstSeen') or epoch_iso(now)
            last = self.last_line(a, investigating)
            saved = self.store.fallback_slack_draft(a['key'], fallback_text(a, fired_at, last), SERVER_BY, fired_at, dt.datetime.fromtimestamp(now, dt.timezone.utc), STOPPED_TRIAGE, at_once=True)
            if saved is None or self.store.claim_anomaly_auto_send(a['key'], saved['slackDraft'], iso(utc_now())) is None:
                continue
            try:
                self.send(a['key'])
            except SEND_ERRORS as e:
                log.warning('card %s not posted to Slack at once, its auto-send retries: %s', a['key'], error_kind(e))
                self.store.anomaly_auto_send_failed(a['key'], failure_of(e))
                continue
            posted.append(a['key'])
            said_at_once(self.store, a['key'])
        return posted
