import asyncio
import datetime as dt
import logging
from typing import Callable

from pymongo.errors import PyMongoError

from skopax import clock
from skopax.engine import PUSH_SVC_QUIET_S, SEV_RANK, Applied, fmt, iso as epoch_iso, parse_iso, push_group
from skopax.errkind import Authored, kind as error_kind
from skopax.investigator.select import EVENT_WINDOW_S, announced_twin, merged, own_findings, stamps
from skopax.messages import msg
from skopax.postdeploy import SERVER_BY
from skopax.slack import SlackError, cut
from skopax.store import NotFound, Store, draft_hash, iso, utc_now

log = logging.getLogger('skopax.autosend')

INTERVAL_SECONDS = 30
STOPPED_TRIAGE = ('progress', 'handled')
LIVE_WHEN_CLEARED = ('crit', 'warn')
SEND_ERRORS = (SlackError, PyMongoError, NotFound)
FALLBACK_SEV = 'crit'
FALLBACK_SKIP_TRIAGE = ('investigating',) + STOPPED_TRIAGE
FALLBACK_TRIGGERS = ('cards', 'action')
FALLBACK_COVERED = ('done', 'running')
POST_NOW_BURST = 3
SEV_WORDS = {'crit': 'critical', 'warn': 'warning', 'info': 'info'}
NO_INVESTIGATION = ('info',)
SUB_LIMIT = 80


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


def waiting(x: dict) -> bool:
    auto = x.get('autoSend') or {}
    return bool((x.get('slackDraft') or {}).get('text')) and not sent(x) or bool(auto.get('holdBy')) or bool(auto.get('claimedAt'))


class AutoSend:
    def __init__(self, store: Store, send_anomaly: Callable[[str], dict], send_report: Callable[[str], dict], interval: float = INTERVAL_SECONDS, on: Callable[[], bool] = lambda: True):
        self.store = store
        self.send_anomaly = send_anomaly
        self.send_report = send_report
        self.interval = interval
        self.on = on
        self.told_off = False

    def merged(self, a: dict) -> bool:
        parent = a.get('parent')
        if not parent or parent == a.get('key'):
            return False
        try:
            host = self.store.anomaly(parent)
        except NotFound:
            return False
        return host.get('status') == 'open' or a.get('status') == 'cleared'

    def live(self, a: dict) -> bool:
        if a.get('muted') or a.get('triage') in STOPPED_TRIAGE or self.merged(a):
            return False
        return a.get('status') == 'open' or (a.get('status') == 'cleared' and a.get('sev') in LIVE_WHEN_CLEARED)

    def _anomaly(self, a: dict, now: str) -> int:
        key, draft = a.get('key'), armed_for(a)
        if not draft or not self.live(a):
            log.info('auto-send dropped on %s: the card or its draft moved on', key)
            self.store.stop_anomaly_auto_send(key)
            return 0
        if self.store.claim_anomaly_auto_send(key, draft, now) is None:
            return 0
        try:
            self.send_anomaly(key)
        except SEND_ERRORS as e:
            log.warning('auto-send of the Slack draft on %s failed: %s', key, error_kind(e))
            self.store.anomaly_auto_send_failed(key, failure_of(e))
            return 0
        log.info('auto-sent the Slack draft on %s', key)
        self.store.add_activity('slack', 'auto', f'Slack draft posted · {key}', key=key)
        return 1

    def _report(self, r: dict, now: str) -> int:
        report_id, draft = r.get('id'), armed_for(r)
        if not draft:
            log.info('auto-send dropped on report %s: its draft moved on', report_id)
            self.store.stop_report_auto_send(report_id)
            return 0
        if self.store.claim_report_auto_send(report_id, draft, now) is None:
            return 0
        try:
            self.send_report(report_id)
        except SEND_ERRORS as e:
            log.warning('auto-send of the Slack draft on report %s failed: %s', report_id, error_kind(e))
            self.store.report_auto_send_failed(report_id, failure_of(e))
            return 0
        log.info('auto-sent the Slack draft on report %s', report_id)
        self.store.add_activity('slack', 'auto', f'Slack draft posted · report {report_id}', reportId=report_id)
        return 1

    def run_once(self) -> int:
        if not self.on():
            if not self.told_off:
                self.told_off = True
                log.info('Slack drafts wait for a person: auto-send is off')
            return 0
        self.told_off = False
        now = iso(utc_now())
        count = sum(self._anomaly(a, now) for a in self.store.due_anomaly_auto_sends(now))
        return count + sum(self._report(r, now) for r in self.store.due_report_auto_sends(now))

    async def loop(self) -> None:
        while True:
            await asyncio.sleep(self.interval)
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
    def __init__(self, store: Store, minutes: int):
        self.store = store
        self.minutes = minutes

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
            if a.get('slackDraft') or (a.get('slackPost') or {}).get('at') or (a.get('autoSend') or {}).get('holdBy') or a.get('triage') in FALLBACK_SKIP_TRIAGE:
                continue
            age = now - parse_iso(fired_at)
            if age < self.minutes * 60 or age > EVENT_WINDOW_S or (a.get('slackFallback') or {}).get('firedAt') == fired_at or investigated(runs, key, fired_at):
                continue
            picked.append((a, fired_at))
        picked.sort(key=lambda p: (p[0].get('firstSeen') or p[1], p[0]['key']))
        return picked

    def run_once(self, now: float) -> int:
        if not self.enabled:
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
            log.info('fallback Slack draft armed on %s: no finished investigation %d min after it fired', a['key'], minutes)
            self.store.add_activity('slack', SERVER_BY, f"Slack fallback draft armed · {a['key']} · no finished investigation {minutes} min after it fired", key=a['key'])
            count += 1
        return count


def quiet(a: dict, cards: list[dict], now: float) -> bool:
    group = push_group(a)
    if group is None:
        return False
    for c in cards:
        if c is a or push_group(c) != group:
            continue
        at = c.get('pushedAt') if c.get('pushedSev') in SEV_RANK else None
        at = at or ((c.get('slackPost') or {}).get('at') if c.get('sev') in NO_INVESTIGATION else None)
        if at and now - parse_iso(at) < PUSH_SVC_QUIET_S:
            return True
    return False


def fresh(applied: Applied, now: float) -> list[dict]:
    if applied.first:
        return []
    by_key = {c['key']: c for c in applied.cards}
    pushed = {a['key'] for a in applied.push}
    new = [by_key[e['key']] for e in applied.events if e.get('ev') == 'new' and e.get('key') in by_key and e['key'] not in pushed]
    taken, info = set(), []
    for a in new:
        group = push_group(a)
        if a.get('sev') not in NO_INVESTIGATION or (group is not None and group in taken) or quiet(a, applied.cards, now):
            continue
        if group is not None:
            taken.add(group)
        info.append(a)
    return list(applied.push) + info


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
        if a.get('sev') in NO_INVESTIGATION:
            return 'autosend.post_now.info_last'
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
            if SEV_RANK.get(a.get('sev'), -1) < SEV_RANK[floor] or a.get('status') != 'open' or a.get('muted') or merged(a, by_key) or a.get('src') in own:
                continue
            if a.get('slackDraft') or (a.get('slackPost') or {}).get('at') or (a.get('autoSend') or {}).get('holdBy') or a.get('triage') in STOPPED_TRIAGE:
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
            grouped = investigating and any(a.get('sev') not in NO_INVESTIGATION for a in due)
            self.store.add_activity('slack', SERVER_BY, f'{len(due)} cards opened together ({severities(due)}); not posted at once' + (', the investigator groups them first' if grouped else ''))
            log.info('%d cards opened together (%s), not posted at once', len(due), severities(due))
            return []
        posted = []
        for a in due:
            fired_at = a.get('pushedAt') or a.get('firstSeen') or epoch_iso(now)
            last = self.last_line(a, investigating)
            saved = self.store.fallback_slack_draft(a['key'], fallback_text(a, fired_at, last), SERVER_BY, fired_at, dt.datetime.fromtimestamp(now, dt.timezone.utc), STOPPED_TRIAGE)
            if saved is None:
                continue
            try:
                self.send(a['key'])
            except SEND_ERRORS as e:
                log.warning('card %s not posted to Slack at once, its auto-send retries: %s', a['key'], error_kind(e))
                continue
            posted.append(a['key'])
            self.store.add_activity('slack', SERVER_BY, f"Card posted to Slack at once with its numbers \u00b7 {a['key']}", key=a['key'])
            log.info('card %s posted to Slack at once', a['key'])
        return posted
