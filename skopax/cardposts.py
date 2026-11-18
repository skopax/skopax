import hashlib
import json
import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

from skopax import tags
from skopax.actors import SYSTEM
from skopax.cardlink import card_url, chat_url
from skopax.engine import mute_for, parse_iso
from skopax.errkind import kind as error_kind
from skopax.slack import CALL_TIMEOUT_S, POST_ACTIONS, Slack, SlackDown, SlackError, anomaly_message, menu_kinds, status_of
from skopax.store import NotFound, Store, glob_match, iso, utc_now

log = logging.getLogger('skopax.cardposts')

GLOB_KINDS = ('known', 'mute', 'unmute')
ACTION_HEADS = {'take': 'took', 'ticket': 'took', 'resolve': 'resolved', 'known': 'known', 'mute': 'muted', 'reopen': 'reopened', 'unmute': 'reopened'}
SWEPT_HEADS = {'cleared': 'cleared', 'reopened': 'reopened', 'escalated': None}
TRIAGE_HEADS = {'progress': 'took', 'handled': 'resolved'}
BATCH_DEADLINE_S = 15
RETRY_AFTER_S = 30
QUEUE_LIMIT = 500
RETRIES = 1
GONE = ('message_not_found', 'cant_update_message', 'channel_not_found', 'not_in_channel', 'is_archived')
CHANNEL_GONE = ('channel_not_found', 'not_in_channel', 'is_archived')
GONE_RETRY_S = 86400
WORKER = 'slack-card-posts'
MENU_LABEL = 'Take, Resolve and Known noise on the Slack post'


def mute_kind(mute: dict | None) -> str | None:
    if not mute:
        return None
    return 'known' if mute.get('kind') == 'known' else 'mute'


def drawn_sig(card: dict, mute: str | None) -> str:
    shown = [status_of(card, mute), card.get('sev'), card.get('owner') or '', sorted(t for t in tags.ids(card) if t in tags.all_tags()), card.get('firstSeen') or '']
    return hashlib.sha1(json.dumps(shown, default=str).encode()).hexdigest()[:16]


def ask_of(post: dict) -> str | None:
    return (post.get('drawn') or {}).get('menu') or post.get('menu')


def gone_for_good(drawn: dict, now: float) -> bool:
    code = drawn.get('gone')
    if not code:
        return False
    if code not in CHANNEL_GONE:
        return True
    try:
        return now - parse_iso(str(drawn.get('at') or '')) < GONE_RETRY_S
    except (TypeError, ValueError):
        return True


@dataclass
class Job:
    match: Callable[[dict, frozenset, str | None], bool]
    head_of: Callable[[dict], dict | None]
    tries: int = 0
    due: float = 0.0


class CardPosts:
    def __init__(self, store: Store, slack: Slack, base: str, menu_on: Callable[[], bool] = lambda: False, clock: Callable[[], float] = time.time,
                 timer: Callable[[], float] = time.monotonic, autostart: bool = True, deadline_s: float = BATCH_DEADLINE_S, retry_after_s: float = RETRY_AFTER_S):
        self.store = store
        self.slack = slack
        self.base = base
        self.menu_on = menu_on
        self.clock = clock
        self.timer = timer
        self.autostart = autostart
        self.deadline_s = deadline_s
        self.retry_after_s = retry_after_s
        self._jobs: list[Job] = []
        self._later: list[Job] = []
        self._lock = threading.Lock()
        self._idle = threading.Condition(self._lock)
        self._wake = threading.Event()
        self._running = False
        self._busy = False
        self._dropped = False

    def after(self, action: dict) -> None:
        kind, pattern = action.get('type'), str(action.get('key') or '')
        head = {'kind': ACTION_HEADS[kind], 'by': action.get('requestedBy')} if kind in ACTION_HEADS else None

        def match(card: dict, keys: frozenset, mute: str | None) -> bool:
            if kind in GLOB_KINDS and pattern not in keys:
                return glob_match(pattern, card['key'])
            return card['key'] == pattern
        self._add(Job(match, lambda card: head))

    def triaged(self, key: str, before: dict) -> None:
        def head(card: dict) -> dict | None:
            now = card.get('triage')
            return {'kind': TRIAGE_HEADS[now], 'by': SYSTEM} if now in TRIAGE_HEADS and now != (before or {}).get('triage') else None
        self._add(Job(lambda card, keys, mute: card['key'] == key, head))

    def assigned(self, key: str, name: str | None, by: str) -> None:
        head = {'kind': 'assigned', 'by': by, 'name': name} if name else {'kind': 'unassigned', 'by': by}
        self._add(Job(lambda card, keys, mute: card['key'] == key, lambda card: head))

    def swept(self, applied) -> None:
        if not applied:
            return
        heads = {e['key']: SWEPT_HEADS[e['ev']] for e in applied.events or [] if e.get('ev') in SWEPT_HEADS and e.get('key')}
        heads |= {r['key']: 'resolved' for r in applied.resolved or [] if r.get('key')}
        if heads:
            self._add(Job(lambda card, keys, mute: card['key'] in heads, lambda card: {'kind': heads[card['key']], 'by': SYSTEM} if heads.get(card['key']) else None))

    def jira(self, rows) -> None:
        cards = {r['card'] for r in rows or [] if isinstance(r, dict) and r.get('card')}
        if cards:
            self._add(Job(lambda card, keys, mute: card['key'] in cards, lambda card: {'kind': TRIAGE_HEADS.get(card.get('triage'), 'reopened'), 'by': SYSTEM}))

    def refresh(self, key: str) -> None:
        self._add(Job(lambda card, keys, mute: card['key'] == key, lambda card: None))

    def reconcile(self) -> None:
        def stale(card: dict, keys: frozenset, mute: str | None) -> bool:
            post = card['slackPost']
            drawn = post.get('drawn') or {}
            return drawn.get('sig') != drawn_sig(card, mute) or bool(drawn.get('gone')) or (not ask_of(post) and bool(menu_kinds(card, mute)) and self.menu_on())
        self._add(Job(stale, lambda card: None))

    def mute_of(self, key: str) -> str | None:
        try:
            return mute_kind(mute_for(self.store.load(), key, self.clock()))
        except Exception as e:
            log.warning('the mute of %s could not be read for its Slack post: %s', key, error_kind(e))
            return None

    def new_menu(self, key: str, by: str) -> dict:
        return self.store.add_action({'type': 'post-buttons', 'source': 'slack', 'key': key, 'target': key, 'label': f'Triage buttons on the Slack post: {key}'[:300], 'requestedBy': by,
                                      'cardActions': {kind: {'key': key, 'action': kind} for kind in POST_ACTIONS}}, 'done', MENU_LABEL)

    def drain(self, timeout: float = 5.0) -> bool:
        with self._idle:
            return self._idle.wait_for(lambda: not self._jobs and not self._later and not self._busy, timeout)

    def run_batch(self) -> int:
        with self._lock:
            now = self.timer()
            due = [j for j in self._later if j.due <= now]
            self._later = [j for j in self._later if j.due > now]
            jobs, self._jobs = due + self._jobs, []
        if not jobs:
            return 0
        try:
            if not self.slack.enabled:
                return 0
            state = self.store.load()
        except Exception as e:
            log.warning('card posts not rewritten, the cards could not be read: %s', error_kind(e))
            return 0
        plan = self._plan(state, jobs)
        started, done = self.timer(), 0
        for index, (card, head, tries, mute) in enumerate(plan):
            if index and self.timer() - started > self.deadline_s - CALL_TIMEOUT_S:
                log.warning('card posts: %d rewrite(s) wait for the next batch, this one ran out of its %d s', len(plan) - index, self.deadline_s)
                self._defer(plan[index:], counted=False)
                break
            got = self._rewrite(card, card['slackPost'], head, mute)
            if got is None:
                self._defer(plan[index:])
                break
            done += got
        return done

    def _plan(self, state: dict, jobs: list[Job]) -> list[tuple[dict, dict | None, int, str | None]]:
        cards = state.get('anomalies') or []
        keys = frozenset(c.get('key') for c in cards if c.get('key'))
        now = self.clock()
        posted = [(c, mute_kind(mute_for(state, c['key'], now))) for c in cards
                  if c.get('key') and (c.get('slackPost') or {}).get('head') and c['slackPost'].get('channel') and c['slackPost'].get('ts')
                  and not gone_for_good(c['slackPost'].get('drawn') or {}, now)]
        plan: dict[str, list] = {}
        for job in jobs:
            for card, mute in posted:
                try:
                    if not job.match(card, keys, mute):
                        continue
                    head = job.head_of(card)
                except Exception as e:
                    log.warning('card post of %s skipped, its change could not be read: %s', card['key'], error_kind(e))
                    continue
                entry = plan.setdefault(card['key'], [card, None, 0, mute])
                entry[1] = head if head is not None else entry[1]
                entry[2] = max(entry[2], job.tries)
        return [tuple(entry) for entry in plan.values()]

    def _defer(self, rest: list, counted: bool = True) -> None:
        due = self.timer() + self.retry_after_s
        step = 1 if counted else 0
        retried = [Job(lambda c, keys, mute, k=card['key']: c['key'] == k, lambda c, h=head: h, tries + step, due) for card, head, tries, _ in rest if not counted or tries < RETRIES]
        with self._lock:
            self._later += retried

    def _add(self, job: Job) -> None:
        with self._lock:
            if len(self._jobs) >= QUEUE_LIMIT:
                self._jobs.pop(0)
                if not self._dropped:
                    self._dropped = True
                    log.warning('card posts: the rewrite queue is full, the oldest change is dropped; the next reconcile redraws it')
            self._jobs.append(job)
            start = self.autostart and not self._running
            if start:
                self._running = True
        self._wake.set()
        if start:
            threading.Thread(target=self._work, name=WORKER, daemon=True).start()

    def _work(self) -> None:
        while True:
            with self._lock:
                now = self.timer()
                ready = bool(self._jobs) or any(j.due <= now for j in self._later)
                if not ready and not self._later:
                    self._running = False
                    self._idle.notify_all()
                    return
                self._busy = ready
                wait = None if ready else min(j.due for j in self._later) - now
                self._wake.clear()
            if wait is not None:
                self._wake.wait(max(wait, 0.0))
                continue
            try:
                self.run_batch()
            except Exception as e:
                log.warning('card posts: a batch of rewrites failed: %s', error_kind(e))
            finally:
                with self._lock:
                    self._busy = False
                    self._dropped = False
                    self._idle.notify_all()

    def _claimed(self, key: str, menu: str) -> bool:
        try:
            return bool(self.store.get_action(menu).get('cardActionApplied'))
        except NotFound:
            return True
        except Exception as e:
            log.warning('the menu of the Slack post of %s could not be read: %s', key, error_kind(e))
            return True

    def _menu(self, card: dict, menu: str | None, mute: str | None) -> tuple[str | None, tuple[str, ...]]:
        kinds = menu_kinds(card, mute)
        if not menu:
            return (self.new_menu(card['key'], SYSTEM)['id'], kinds) if kinds and self.menu_on() else (None, kinds)
        if not self._claimed(card['key'], menu):
            return menu, kinds
        if kinds and self.menu_on():
            return self.new_menu(card['key'], SYSTEM)['id'], kinds
        return menu, ()

    def _rewrite(self, card: dict, post: dict, head: dict | None, mute: str | None) -> int | None:
        key = card['key']
        if head is not None:
            head = head | {'at': iso(utc_now())}
            try:
                self.store.slack_post_head(key, post['ts'], head)
            except Exception as e:
                log.warning('the headline of the Slack post of %s was not stored: %s', key, error_kind(e))
        threaded = bool(post.get('threadOf'))
        ts = post.get('replyTs') if threaded else post['ts']
        if not ts:
            return 0
        body = post.get('body') if isinstance(post.get('body'), dict) else {}
        draft = str(body.get('text') or '') or None
        shown = card | {'slackDraft': {k: v for k, v in body.items() if k in ('text', 'headline', 'by')}} if draft else card
        try:
            menu, kinds = self._menu(card, ask_of(post), mute)
            message = anomaly_message(shown, card_url(self.base, key, card.get('num')), post.get('by') or SYSTEM, draft, menu, chat_url(self.base, key, card.get('num')),
                                      head=head or post['head'], attribution=None if threaded else self.slack.signature(), mute=mute, kinds=kinds, shots=post.get('screenshots'), base=self.base)
            self.slack.update(post['channel'], ts, message['text'], message['blocks'])
        except SlackDown as e:
            log.warning('the Slack post of %s was not rewritten and the rest of this batch waits, Slack did not answer: %s', key, error_kind(e))
            return None
        except SlackError as e:
            if e.code not in GONE:
                log.warning('the Slack post of %s was not rewritten: %s', key, error_kind(e))
                return 0
            log.info('the Slack post of %s can no longer be rewritten (%s), it is left as it is', key, e.code)
            try:
                self.store.slack_post_drawn(key, post['ts'], {'sig': drawn_sig(card, mute), 'at': iso(utc_now()), 'gone': e.code})
            except Exception as e2:
                log.warning('the Slack post of %s was not marked as gone: %s', key, error_kind(e2))
            return 0
        except Exception as e:
            log.warning('the Slack post of %s was not rewritten: %s', key, error_kind(e))
            return 0
        try:
            self.store.slack_post_drawn(key, post['ts'], {'sig': drawn_sig(card, mute), 'at': iso(utc_now()), **({'menu': menu} if menu else {})})
        except Exception as e:
            log.warning('the Slack post of %s was rewritten but not recorded: %s', key, error_kind(e))
        log.info('Slack post of %s rewritten in place: %s', key, (head or post['head']).get('kind'))
        return 1
