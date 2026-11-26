import logging
import re
import time
from typing import Callable

from skopax import clock, history, information, severity, summary
from skopax.actors import POD_BY, SYSTEM
from skopax.businesstime import business_add, elapsed
from skopax.engine import has_owner, iso, mute_for, parse_iso, wait_start, window_for
from skopax.errkind import kind as error_kind
from skopax.messages import msg
from skopax.notices import holder
from skopax.reminders import person
from skopax.slack import esc, take_offer_blocks
from skopax.store import Store

log = logging.getLogger('skopax.followup')

REVIEW = 'review'
VERIFY = 'verify'
SETTLE = 'settle'
FLAVORS = (VERIFY, SETTLE)
FIELD = 'followUp'
DONE_WHEN_LIMIT = 300
TOOLS_MAX = 5
TOOL_NAME_LIMIT = 64
EVIDENCE_LIMIT = 300
OWNER_LIMIT = 120
NOT_DONE = 'not_done'
CLOSING = ('fixed', 'no_action')
SETTLE_KINDS = ('fixed', 'no_action', 'keep', 'ticket')
CLOSE_KIND = {'fixed': 'fixed', 'no_action': 'handled'}
CLOSE_NAME = {'fixed': 'Fixed', 'handled': 'No action needed'}
REF_HEAD = {VERIFY: 'Verified: ', SETTLE: 'Auto-closed: '}
KEEP = 'keep'
OFFER_TYPE = 'follow-up'
RUN = 'follow-ups'
PERIOD = 600
OFFSET = 210
DAY_S = 86400
HOUR_S = 3600
WEEKEND = 5
OPEN_STATES = ('requested', 'processing')
SEVERITIES = severity.CARD_LEVELS
WATCHED = ('progress', 'handled')
ACTIVITY = 'followup'
CHECKED_CLOSE = {'fixed': 'looks fixed', 'no_action': 'looks over'}
CHECKED = {NOT_DONE: 'Follow-up checked: not done yet', 'keep': 'Settle review: keep open', 'ticket': 'Settle review: the linked ticket is still open'}
CANCELLED = {'fired': 'Auto-close cancelled: the card fired again', 'person': 'Auto-close cancelled: a person acted on the card', 'closed': 'Auto-close cancelled: the card was resolved'}
CLOSE_FIELDS = ('closedAt', 'closeAt', 'closeKind', 'closeRef', 'announcedAt', 'announced', 'basis', 'cancelled', 'lastResult', 'lastEvidence', 'lastRun', 'untold')
PENDING_CLOSE = ('closeAt', 'closeKind', 'closeRef', 'announcedAt', 'announced', 'basis', 'cancelled', 'lastResult', 'lastEvidence', 'lastRun')
PERSON_NOTE = ('', 'person')
KEEP_WORDS = re.compile(r"\b(?:keep|leave)\s+(?:it\s+|this\s+|the\s+card\s+)?open\b|\b(?:do\s+not|don[\u2019']?t|never)\s+(?:auto[\s-]?)?close\b|\bnot\s+(?:yet\s+)?(?:done|fixed)\b|\bpending\b|\bwaiting\s+(?:on|for)\b", re.I)
WEEKDAYS = ('Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun')
PERSON_ACTIONS = ('dive', 'ask')


def zone_of(business: bool):
    return clock.zone() if business else None


def stamp(value) -> float | None:
    try:
        return parse_iso(str(value)) if value else None
    except ValueError:
        return None


def flat(text, limit: int) -> str:
    return ' '.join(str(text or '').split())[:limit]


def done_when(text, tools, at: str, flagged: bool = False) -> dict:
    condition = flat(text, DONE_WHEN_LIMIT)
    if not condition:
        return dict.fromkeys(('doneWhen', 'tools', 'setAt', 'by', 'flagged', 'gaveUp', 'gaveUpAt'))
    names = list(dict.fromkeys(flat(t, TOOL_NAME_LIMIT) for t in tools or [] if flat(t, TOOL_NAME_LIMIT)))[:TOOLS_MAX]
    return {'doneWhen': condition, 'tools': names, 'setAt': at, 'by': POD_BY, 'flagged': True if flagged else None, 'gaveUp': None, 'gaveUpAt': None, 'checkedAt': None} | dict.fromkeys(CLOSE_FIELDS)


def close_at_text(close_at: float, now: float) -> str:
    day = clock.local(close_at)
    if day.date() == clock.local(now).date():
        return f'{clock.hm(close_at)} {clock.label(close_at)}'
    return f'{WEEKDAYS[day.weekday()]} {clock.date(close_at)} {clock.label(close_at)}'


def person_note(card: dict) -> dict | None:
    note = card.get('note') if isinstance(card.get('note'), dict) else {}
    if not str(note.get('text') or '').strip() or str(card.get('noteBy') or '') not in PERSON_NOTE or (not card.get('noteBy') and card.get('expired')):
        return None
    return note


def kept_by_note(card: dict) -> bool:
    note = person_note(card)
    if note is None:
        return False
    if KEEP_WORDS.search(str(note.get('text'))):
        return True
    fu = card.get(FIELD) if isinstance(card.get(FIELD), dict) else {}
    marks = [stamp(fu.get('setAt'))] if fu.get('doneWhen') else [stamp(card.get('assessedAt')), stamp(card.get('clearedAt'))]
    since, written = max((m for m in marks if m is not None), default=None), stamp(note.get('at'))
    return since is not None and written is not None and written >= since


def kept(card: dict) -> bool:
    fu = card.get(FIELD) if isinstance(card.get(FIELD), dict) else {}
    at, seen = stamp(fu.get('keptAt')), stamp(card.get('lastSeen'))
    return at is not None and not (seen is not None and seen > at)


def basis(card: dict) -> dict:
    triage = card.get('triage') if card.get('triage') in WATCHED else None
    note = card.get('note') if isinstance(card.get('note'), dict) else {}
    held = card.get('assigned') if isinstance(card.get('assigned'), dict) else {}
    hand = card.get('sevSet') if isinstance(card.get('sevSet'), dict) else {}
    return {'triage': triage, 'triagedAt': card.get('triagedAt') if triage else None, 'owner': card.get('owner') or None, 'assigned': held.get('at'), 'ticket': card.get('ticket') or None,
            'sev': hand.get('at'), 'note': note.get('at') if note.get('text') and card.get('noteBy') != 'pod' else None, 'reopened': card.get('reopenedAt'), 'muted': bool(card.get('muted')),
            'parent': card.get('parent') or None}


def cancel_reason(card: dict, fu: dict) -> str | None:
    announced, seen = stamp(fu.get('announcedAt')), stamp(card.get('lastSeen'))
    if card.get('status') != 'cleared' or (announced is not None and seen is not None and seen > announced):
        return 'fired'
    if card.get('triage') == 'handled':
        return 'closed'
    if card.get('muted') or basis(card) != fu.get('basis'):
        return 'person'
    return None


def merged(card: dict, by_key: dict) -> bool:
    return bool(card.get('parent')) and card.get('parent') != card.get('key') and card.get('parent') in by_key


def verify_state(card: dict, now: float, hours: int, days: int) -> str | None:
    fu = card.get(FIELD)
    if not isinstance(fu, dict) or not fu.get('doneWhen') or fu.get('flagged') or fu.get('gaveUp') or fu.get('untold') or fu.get('closeAt') or fu.get('closedAt') or kept(card):
        return None
    set_at = stamp(fu.get('setAt')) or now
    if now - set_at >= days * DAY_S:
        return 'give-up'
    last = stamp(fu.get('checkedAt'))
    return 'due' if (now - last >= hours * HOUR_S if last is not None else now - set_at >= HOUR_S) else None


def severity_hours(s) -> dict[str, int]:
    return {'crit': s.auto_resolve_hours_crit, 'high': s.auto_resolve_hours_high, 'warn': s.auto_resolve_hours_warn}


def settle_due(card: dict, by_key: dict, state: dict, now: float, s, windows: tuple, zone) -> bool:
    if card.get('status') != 'cleared' or card.get('sev') not in SEVERITIES or card.get('muted') or information.is_item(card) or merged(card, by_key) or mute_for(state, card['key'], now):
        return False
    if card.get('triage') in ('handled', 'investigating') or card.get('autoResolvedAt') or not card.get('clearedAt') or window_for(card, windows):
        return False
    if card.get('triage') != 'progress' and not has_owner(card):
        return False
    window = severity_hours(s).get(card.get('sev'), s.auto_resolve_hours)
    cleared = stamp(card['clearedAt'])
    if window <= 0 or cleared is None or elapsed(wait_start(card, cleared), now, zone) <= window * HOUR_S:
        return False
    fu = card.get(FIELD) if isinstance(card.get(FIELD), dict) else {}
    if fu.get('closeAt') or kept(card):
        return False
    last = stamp(fu.get('checkedAt'))
    return last is None or now - last >= s.investigate_follow_up_hours * HOUR_S


class FollowUps:
    def __init__(self, store: Store, settings: Callable[[], object], investigator, slack, feature_on: Callable[[str], bool], notices, clock: Callable[[], float] = time.time,
                 base_url: str = '', windows: Callable[[], tuple] = lambda: (), mention: Callable[[str], str | None] | None = None, name_of: Callable[[str], str] | None = None,
                 after_close: Callable[[str, dict], None] | None = None):
        self.store = store
        self.settings = settings
        self.investigator = investigator
        self.slack = slack
        self.feature_on = feature_on
        self.notices = notices
        self.clock = clock
        self.base_url = base_url.rstrip('/')
        self.windows = windows
        self.mention = mention
        self.name_of = name_of
        self.after_close = after_close
        self._said: set[str] = set()

    @property
    def enabled(self) -> bool:
        return bool(self.investigator.enabled) and self.investigator.mode == 'live'

    def run_once(self, now: float | None = None) -> dict:
        now = self.clock() if now is None else now
        if not self.enabled or self.investigator.why_halted(now) in ('off', 'stopped'):
            return {}
        s = self.settings()
        state = self.store.load()
        cards = state.get('anomalies') or []
        closed = self._closes(cards, now)
        queued = self._queue(state, cards, now, s)
        if queued:
            self.investigator.kick(now)
        return {'closed': closed, 'queued': queued}

    def _once(self, mark: str, text: str, *args) -> None:
        if mark in self._said:
            return
        self._said.add(mark)
        log.info(text, *args)

    def _queue(self, state: dict, cards: list[dict], now: float, s) -> list[str]:
        zone = zone_of(s.business_days)
        if zone is not None and clock.local(now).weekday() >= WEEKEND:
            return []
        by_key = {c['key']: c for c in cards}
        waiting = {c['key'] for c in summary.waiting_cards(cards) if not mute_for(state, c['key'], now)}
        windows = tuple(self.windows() or ())
        settle_on = self.feature_on('settle')
        busy = {a.get('key') for st in OPEN_STATES for a in self.store.list_actions(st, 500) if a.get('type') == REVIEW} | self.store.held_review_keys()
        due = []
        for c in cards:
            if c['key'] in busy:
                continue
            if settle_on and settle_due(c, by_key, state, now, s, windows, zone):
                due.append((c, SETTLE))
                continue
            if c['key'] not in waiting:
                continue
            found = verify_state(c, now, s.investigate_follow_up_hours, s.investigate_follow_up_days)
            if found == 'give-up':
                self._give_up(c, now, s.investigate_follow_up_days)
            elif found == 'due':
                due.append((c, VERIFY))
        if not due:
            return []
        halted = self.investigator.why_halted(now)
        if halted:
            self._once(f'halt:{clock.day(now)}:{halted}', 'follow-up checks of %d cards wait: the investigator is %s', len(due), halted)
            return []
        used = self.store.count_actions(REVIEW, iso(clock.midnight(now)))
        room = max(s.investigate_settle_max_per_day - used, 0)
        if room < len(due):
            self._once(f'cap:{clock.day(now)}', 'follow-up checks: the cap of %d a day is reached, %d cards wait for tomorrow', s.investigate_settle_max_per_day, len(due) - room)
        due.sort(key=lambda pair: (str(((pair[0].get(FIELD) if isinstance(pair[0].get(FIELD), dict) else {}) or {}).get('checkedAt') or ''), str(pair[0].get('clearedAt') or ''), pair[0]['key']))
        queued = []
        for c, flavor in due[:room]:
            key = c['key']
            if not self.store.claim_run(f'{RUN}:{key}:{int(now // HOUR_S)}'):
                continue
            self.store.follow_up_set(key, {'checkedAt': iso(now)})
            self.store.add_action({'type': REVIEW, 'flavor': flavor, 'key': key, 'target': f'{RUN}:{key}', 'requestedBy': 'server', 'source': 'server',
                                   'label': f"{'Follow-up check' if flavor == VERIFY else 'Settle review'}: {key}"[:300]})
            queued.append(key)
        if queued:
            log.info('follow-up checks queued for %d cards', len(queued))
        return queued

    def _give_up(self, card: dict, now: float, days: int) -> None:
        self.store.follow_up_set(card['key'], {'gaveUp': True, 'gaveUpAt': iso(now)})
        self.store.add_activity(ACTIVITY, 'server', f'Follow-up check gave up after {days} days: the fix was not confirmed, the card is left to people', key=card['key'])

    def _closes(self, cards: list[dict], now: float) -> list[str]:
        closed = []
        for c in cards:
            fu = c.get(FIELD)
            if not isinstance(fu, dict) or not fu.get('closeAt'):
                continue
            why = cancel_reason(c, fu) or ('person' if self.store.person_asked_since(c['key'], fu.get('announcedAt') or '') else None)
            if why:
                self._cancel(c, fu, why, now)
                continue
            if not fu.get('announced'):
                self._announce_again(c, fu, now)
                continue
            at = stamp(fu['closeAt'])
            if at is None or now < at:
                continue
            how = {'kind': fu.get('closeKind') or 'fixed', 'ref': fu.get('closeRef'), 'by': POD_BY, 'via': SYSTEM}
            if not self.store.close_follow_up(c['key'], fu['closeAt'], how, fu.get('basis')):
                continue
            self.store.add_activity(ACTIVITY, POD_BY, f"Closed as {CLOSE_NAME.get(how['kind'], 'Fixed')} after the follow-up check: {how['ref'] or ''}"[:300], key=c['key'])
            self._closed(c)
            closed.append(c['key'])
        return closed

    def _closed(self, before: dict) -> None:
        try:
            self.notices.triaged(before['key'], before, POD_BY)
            if self.after_close is not None:
                self.after_close(before['key'], before)
        except Exception as e:
            log.warning('the close of %s after its follow-up check was not announced: %s', before['key'], error_kind(e))

    def _cancel(self, card: dict, fu: dict, why: str, now: float) -> None:
        if self.store.follow_up_if(card['key'], fu['closeAt'], {f'{FIELD}.closeAt': None, f'{FIELD}.announced': None, f'{FIELD}.cancelled': {'at': iso(now), 'why': why}}):
            self.store.add_activity(ACTIVITY, 'server', CANCELLED[why], key=card['key'])

    def verdict(self, key: str, flavor: str, kind: str, evidence: str, owner: str | None, run_id: str | None) -> str:
        now = self.clock()
        s = self.settings()
        card = self.store.anomaly(key)
        at = iso(now)
        evidence = flat(evidence, EVIDENCE_LIMIT)
        fields = {'checkedAt': at, 'lastResult': kind, 'lastEvidence': evidence, 'lastRun': run_id, 'flavor': flavor}
        if kind not in CLOSING:
            fields['owner'] = flat(owner, OWNER_LIMIT) or None if kind == KEEP else None
            self.store.follow_up_set(key, fields)
            self.store.add_activity(ACTIVITY, POD_BY, f'{CHECKED[kind]} \u00b7 {evidence}'[:300], key=key, runId=run_id)
            return f'recorded on {key}: {CHECKED[kind].lower()}'
        fu = card.get(FIELD) if isinstance(card.get(FIELD), dict) else {}
        if fu.get('closeAt'):
            self.store.follow_up_set(key, {k: v for k, v in fields.items() if k != 'lastResult'})
            return 'already announced: the close of this card is pending, nothing new was posted'
        if kept(card):
            self.store.follow_up_set(key, fields)
            return 'not closed: a person pressed Keep open on this card, it stays with people'
        if fu.get('flagged'):
            self.store.follow_up_set(key, fields | {'lastResult': KEEP})
            self.store.add_activity(ACTIVITY, POD_BY, 'Not closed: the done-condition came from a run that read planted text, the card stays with people', key=key, runId=run_id)
            return 'not closed: the done-condition of this card was written by a run that read planted text, so it never closes the card'
        if kept_by_note(card):
            self.store.follow_up_set(key, fields | {'lastResult': KEEP, 'owner': None})
            self.store.add_activity(ACTIVITY, POD_BY, 'Not closed: a person wrote a note on this card that keeps it with people', key=key, runId=run_id)
            return 'not closed: a person wrote a note on this card while it waited or asked to keep it open; it stays with people'
        if card.get('status') != 'cleared' or card.get('triage') == 'handled' or card.get('muted'):
            self.store.follow_up_set(key, fields)
            return 'not closed: the card fired again, is resolved or is muted'
        if not self._audience(card):
            self.store.follow_up_set(key, fields | {'untold': True})
            self.store.add_activity(ACTIVITY, POD_BY, f'Follow-up check: {CHECKED_CLOSE[kind]}, but nobody could be told (no Slack post, no assignee), so the auto-resolve rule decides \u00b7 {evidence}'[:300], key=key, runId=run_id)
            return 'recorded: nobody can be told about a close of this card (no Slack post, no assignee), so it is not announced and the auto-resolve rule decides'
        close_at = business_add(now, s.investigate_follow_up_close_hours * HOUR_S, zone_of(s.business_days))
        close_kind = CLOSE_KIND[kind]
        when = close_at_text(close_at, now)
        fields |= {'closeAt': iso(close_at), 'closeKind': close_kind, 'closeRef': (REF_HEAD.get(flavor, REF_HEAD[SETTLE]) + evidence)[:history.REF_LIMIT],
                   'announcedAt': at, 'announced': None, 'basis': basis(card), 'cancelled': None}
        self.store.follow_up_set(key, fields)
        name = CLOSE_NAME[close_kind]
        self.store.add_activity(ACTIVITY, POD_BY, f'Follow-up check: closes as {name} at {when} unless it fires again or a person acts \u00b7 {evidence}'[:300], key=key, runId=run_id)
        told = self._tell(card, close_kind, evidence, close_at, now, iso(close_at))
        return f'recorded: {key} closes as {name} at {when} unless it fires again or a person acts on it' + ('' if told else '; nobody could be told yet, so it waits and the server tries to announce it again')

    def _audience(self, card: dict) -> bool:
        post = card.get('slackPost') if isinstance(card.get('slackPost'), dict) else {}
        thread = self.slack is not None and bool(self.slack.enabled) and bool(post.get('ts')) and bool(post.get('channel')) and not post.get('threadOf')
        return thread or bool(holder(card))

    def _tell(self, card: dict, close_kind: str, evidence: str, close_at: float, now: float, close_iso: str, current: str | None = None, extra: dict | None = None) -> bool:
        key = card['key']
        posted = self._announce(card, close_kind, evidence, close_at, now)
        noticed = 0
        try:
            noticed = self.notices.add({holder(card)}, 'closing', POD_BY, card, f'{CLOSE_NAME[close_kind]} at {close_at_text(close_at, now)}')
        except Exception as e:
            log.warning('the closing notice of %s was not saved: %s', key, error_kind(e))
        if posted is False and noticed:
            self.store.add_activity(ACTIVITY, 'server', 'The Slack thread post failed; the close was announced to the assignee by a notice', key=key)
        if posted or noticed:
            self.store.follow_up_if(key, current or close_iso, {f'{FIELD}.announced': {'at': iso(now), 'thread': bool(posted), 'notice': bool(noticed)}} | (extra or {}))
            return True
        return False

    def _announce_again(self, card: dict, fu: dict, now: float) -> None:
        s = self.settings()
        close_at = max(stamp(fu['closeAt']) or now, business_add(now, s.investigate_follow_up_close_hours * HOUR_S, zone_of(s.business_days)))
        if not self._audience(card):
            if self.store.follow_up_if(card['key'], fu['closeAt'], {f'{FIELD}.closeAt': None, f'{FIELD}.untold': True}):
                self.store.add_activity(ACTIVITY, 'server', 'Auto-close dropped: nobody could be told (no Slack post, no assignee), so the auto-resolve rule decides', key=card['key'])
            return
        self._tell(card, fu.get('closeKind') or 'fixed', str(fu.get('lastEvidence') or ''), close_at, now, iso(close_at), fu['closeAt'], {f'{FIELD}.closeAt': iso(close_at), f'{FIELD}.announcedAt': iso(now)})

    def _who(self, card: dict) -> str:
        owner = str(card.get('owner') or '').strip()
        held = card.get('assigned') if isinstance(card.get('assigned'), dict) else {}
        if not owner:
            return ''
        return ' ' + person(owner, self.mention, self.name_of, str(held.get('email') or ''))

    def _announce(self, card: dict, close_kind: str, evidence: str, close_at: float, now: float) -> bool | None:
        post = card.get('slackPost') if isinstance(card.get('slackPost'), dict) else {}
        if self.slack is None or not self.slack.enabled or not post.get('ts') or not post.get('channel') or post.get('threadOf'):
            return None
        key = card['key']
        text = msg('slack.followup.fixed' if close_kind == 'fixed' else 'slack.followup.handled', evidence=esc(evidence), at=close_at_text(close_at, now), who=self._who(card))
        call = {'key': key, 'action': KEEP}
        try:
            offer = self.store.add_action({'type': OFFER_TYPE, 'source': 'slack', 'key': key, 'target': f'{OFFER_TYPE}:{key}', 'label': f'Slack keep-open offer: {key}'[:300], 'requestedBy': 'server',
                                           'cardAction': call, 'slack': {'where': 'card', 'channel': post['channel'], 'threadTs': post['ts']}}, 'done', text[:300])
            self.slack.post({'channel': post['channel'], 'thread_ts': post['ts'], 'text': text, 'blocks': take_offer_blocks(text, offer['id'], call)})
            return True
        except Exception as e:
            log.warning('the close announcement of %s was not posted: %s', key, error_kind(e))
            return False


def line(fu: dict) -> str:
    parts = []
    if fu.get('doneWhen'):
        parts.append(f"done when {flat(fu['doneWhen'], DONE_WHEN_LIMIT)}")
    if fu.get('checkedAt'):
        parts.append(f"last check {fu['checkedAt']}" + (f": {fu['lastResult']}" if fu.get('lastResult') else '') + (f" ({flat(fu['lastEvidence'], EVIDENCE_LIMIT)})" if fu.get('lastEvidence') else ''))
    if fu.get('closeAt'):
        parts.append(f"closes as {CLOSE_NAME.get(fu.get('closeKind'), 'Fixed')} at {fu['closeAt']}")
    if fu.get('owner'):
        parts.append(f"pending with {flat(fu['owner'], OWNER_LIMIT)}")
    if fu.get('keptBy'):
        parts.append(f"kept open by {fu['keptBy']}")
    if fu.get('gaveUp'):
        parts.append('gave up')
    return ' \u00b7 '.join(parts)
