import datetime as dt
import hashlib
import json
import logging
import re
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass

from skopax import tags
from skopax.actors import POD_BY, SYSTEM
from skopax.cardlink import card_url, chat_url
from skopax.engine import mute_for, parse_iso, window_text
from skopax.errkind import kind as error_kind
from skopax.investigator.tools import DRAFT_LIMIT, POST_HEADLINE_LIMIT, draft_problem, post_headline
from skopax.messages import language, msg
from skopax.postdeploy import NUMBERS_BY, SERVER_BY
from skopax.slack import CALL_TIMEOUT_S, POST_ACTIONS, Slack, SlackDown, SlackError, SlackSlowed, anomaly_message, cut_words, menu_kinds, status_of, stopped_ending, stopped_who, thread_why
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
REDRAW_MARK = 'slack-redraw-0-4-6'
DRAW_VERSION = 3
REDRAW_CAP = 500
REDRAW_GAP_S = 1.2
REDRAW_TRIES = 3
REDRAW_STALE_S = 300
LEASE_WARN_S = 300
CLOSED_KINDS = ('resolved', 'known', 'auto', 'auto_first')
SERVER_BODIES = (SERVER_BY, NUMBERS_BY)
SUMMARY_SENTENCES = 2
SUMMARY_LINE_LIMIT = 300
SENTENCE_GAP = re.compile(r'(?<=[.!?])\s+')
NOT_AN_END = re.compile(r'(?:\b(?:e\.g|i\.e|etc|vs|incl|approx)|(?:^|\s)\d{1,2})\.$', re.IGNORECASE)
NO_THREAD = [None, '', False]
STAMP_SETTLED_S = 600


def auto_head(row: dict):
    hours = row.get('hours')
    if not isinstance(hours, (int, float)) or isinstance(hours, bool) or hours <= 0:
        return 'resolved'
    span = window_text(int(hours), words='notes.window.business_') if row.get('businessTime') else window_text(int(hours))
    return {'kind': 'auto_first' if row.get('since') == 'first' else 'auto', 'span': span}


def heads_of(head) -> dict | None:
    if not head:
        return None
    return {'kind': head, 'by': SYSTEM} if isinstance(head, str) else head | {'by': SYSTEM}


def mute_kind(mute: dict | None) -> str | None:
    if not mute:
        return None
    return 'known' if mute.get('kind') == 'known' else 'mute'


def drawn_sig(card: dict, mute: str | None, who: str = '') -> str:
    shown = [status_of(card, mute), card.get('sev'), card.get('owner') or '', sorted(t for t in tags.ids(card) if t in tags.all_tags()), card.get('firstSeen') or '', DRAW_VERSION, language(), stopped_ending(card), who]
    if shown[0] == 'cleared':
        shown.append('bar-short')
    return hashlib.sha1(json.dumps(shown, default=str).encode()).hexdigest()[:16]


def card_closed(card: dict, mute: str | None) -> bool:
    return mute == 'known' or card.get('triage') == 'handled' or bool(card.get('autoResolvedAt'))


def plain_head(card: dict, mute: str | None, post: dict) -> dict:
    kind = ('known' if mute == 'known' else 'resolved' if card_closed(card, mute) else 'muted' if mute else 'cleared' if card.get('status') == 'cleared'
            else 'took' if card.get('triage') == 'progress' else 'reopened' if (post.get('head') or {}).get('kind') in CLOSED_KINDS else 'opened')
    return {'kind': kind, 'by': owner_by(card, kind), 'at': post.get('at') or iso(utc_now())}


def owner_by(card: dict, kind: str | None) -> str:
    return (card.get('owner') if kind == 'took' else None) or SYSTEM


def fit_head(card: dict, mute: str | None, post: dict, head: dict | None) -> dict | None:
    if not head:
        return plain_head(card, mute, post)
    kind = head.get('kind')
    closed = card_closed(card, mute)
    stopped = card.get('status') == 'cleared' and not closed and not mute
    stale = (closed != (kind in CLOSED_KINDS) or (kind == 'took' and card.get('triage') != 'progress') or (kind == 'muted' and not mute)
             or (kind == 'cleared' and card.get('status') != 'cleared') or (stopped and kind != 'cleared'))
    return plain_head(card, mute, post) if stale else None


def last_drawn(post: dict) -> float:
    for value in ((post.get('head') or {}).get('at'), (post.get('drawn') or {}).get('at'), post.get('at')):
        try:
            return parse_iso(str(value))
        except (TypeError, ValueError):
            continue
    return 0.0


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


def tame(text) -> str:
    return ' '.join(str(text or '').replace('<', '\u2039').replace('>', '\u203a').split())


def sentences(text: str) -> list[str]:
    out: list[str] = []
    for piece in SENTENCE_GAP.split(text):
        if out and (NOT_AN_END.search(out[-1]) or not piece[:1].isupper()):
            out[-1] += ' ' + piece
        elif piece:
            out.append(piece)
    return out


def passes(text: str, check: Callable[[str], str | None]) -> bool:
    try:
        return not check(text)
    except Exception:
        return False


def recommended_line(card: dict, check: Callable[[str], str | None]) -> str:
    if card.get('recommendBy') != 'pod' or card.get('recommendFlagged'):
        return ''
    first = next((line.strip() for line in str(card.get('recommend') or '').splitlines() if line.strip()), '')
    return msg('slack.summary.recommended', text=cut_words(tame(first), SUMMARY_LINE_LIMIT)) if first and passes(first, check) else ''


def stamp_delivered(p: dict, now: float) -> bool:
    if 'ok' in p:
        return bool(p['ok'])
    try:
        return parse_iso(str(p.get('at') or '')) <= now - STAMP_SETTLED_S
    except (TypeError, ValueError):
        return False


def notified_line(card: dict, plugin_name: Callable[[str], str], now: float) -> str:
    groups: dict[str, list[str]] = {}
    for p in card.get('teamsPosts') or []:
        name = tame(p.get('route') if isinstance(p, dict) else '')
        if name and not p.get('fixed') and stamp_delivered(p, now):
            routes = groups.setdefault(str(p.get('plugin') or ''), [])
            if name not in routes:
                routes.append(name)
    parts = [', '.join(routes) + (f' ({tame(plugin_name(plugin))})' if plugin else '') for plugin, routes in groups.items()]
    return msg('slack.summary.notified', routes=cut_words('; '.join(parts), SUMMARY_LINE_LIMIT)) if parts else ''


def note_parts(card: dict, check: Callable[[str], str | None]) -> tuple[str, str]:
    if card.get('noteBy') != 'pod' or card.get('noteFlagged'):
        return '', ''
    found = sentences(' '.join(str((card.get('note') or {}).get('text') or '').split()))
    picked = found[:1 + SUMMARY_SENTENCES] if len(found) > 1 else found
    if not picked or not passes(' '.join(picked), check):
        return '', ''
    if len(picked) == 1:
        return '', tame(picked[0])
    return post_headline(cut_words(picked[0], POST_HEADLINE_LIMIT)), tame(' '.join(picked[1:]))


def summary_body(card: dict, check: Callable[[str], str | None]) -> dict | None:
    draft = card.get('slackDraft') or {}
    if draft.get('notAutoSent') or (card.get('autoSend') or {}).get('holdBy'):
        return None
    if draft.get('by') == POD_BY and str(draft.get('text') or '').strip():
        headline, text = post_headline(draft.get('headline')), str(draft['text']).strip()
    else:
        headline, text = note_parts(card, check)
    if not text:
        return None
    line = recommended_line(card, check)
    room = DRAFT_LIMIT - (len(line) + 1 if line else 0) - SUMMARY_LINE_LIMIT - 40
    return {'text': '\n'.join([cut_words(text, room)] + ([line] if line else [])), **({'headline': headline} if headline else {}), 'by': POD_BY, 'filled': True}


def has_text(body) -> bool:
    return isinstance(body, dict) and bool(str(body.get('text') or '').strip())


def fillable(post: dict) -> bool:
    if post.get('threadOf') or (post.get('drawn') or {}).get('filled'):
        return False
    body = post.get('body') if isinstance(post.get('body'), dict) else {}
    return not has_text(body) or body.get('by') in SERVER_BODIES


def filled_body(post: dict) -> bool:
    return not post.get('threadOf') and isinstance(post.get('body'), dict) and bool(post['body'].get('filled'))

@dataclass
class Job:
    match: Callable[[dict, frozenset, str | None], bool]
    head_of: Callable[[dict], dict | None]
    tries: int = 0
    due: float = 0.0


class CardPosts:
    def __init__(self, store: Store, slack: Slack, base: str, menu_on: Callable[[], bool] = lambda: False, clock: Callable[[], float] = time.time,
                 timer: Callable[[], float] = time.monotonic, autostart: bool = True, deadline_s: float = BATCH_DEADLINE_S, retry_after_s: float = RETRY_AFTER_S,
                 pause: Callable[[float], None] = time.sleep, name_of: Callable[[str], str] = lambda email: '', plugin_name: Callable[[str], str] = lambda plugin: plugin,
                 check: Callable[[], Callable[[str], str | None]] = lambda: draft_problem,
                 hosts: Callable[[], tuple[str, ...]] = lambda: ()):
        self.store = store
        self.hosts = hosts
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
        self._tl = threading.local()
        self.pause = pause
        self.name_of = name_of
        self._lease_warned: float | None = None
        self.plugin_name = plugin_name
        self.check = check

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
            return {'kind': TRIAGE_HEADS[now], 'by': owner_by(card, TRIAGE_HEADS[now])} if now in TRIAGE_HEADS and now != (before or {}).get('triage') else None
        self._add(Job(lambda card, keys, mute: card['key'] == key, head))

    def assigned(self, key: str, name: str | None, by: str) -> None:
        head = {'kind': 'assigned', 'by': by, 'name': name} if name else {'kind': 'unassigned', 'by': by}
        self._add(Job(lambda card, keys, mute: card['key'] == key, lambda card: head))

    def swept(self, applied) -> None:
        if not applied:
            return
        heads = {e['key']: SWEPT_HEADS[e['ev']] for e in applied.events or [] if e.get('ev') in SWEPT_HEADS and e.get('key')}
        heads |= {r['key']: auto_head(r) for r in applied.resolved or [] if r.get('key')}
        if heads:
            self._add(Job(lambda card, keys, mute: card['key'] in heads, lambda card: heads_of(heads[card['key']])))

    def jira(self, rows) -> None:
        cards = {r['card'] for r in rows or [] if isinstance(r, dict) and r.get('card')}
        if cards:
            self._add(Job(lambda card, keys, mute: card['key'] in cards, lambda card: {'kind': TRIAGE_HEADS.get(card.get('triage'), 'reopened'), 'by': owner_by(card, TRIAGE_HEADS.get(card.get('triage')))}))

    def refresh(self, key: str) -> None:
        self._add(Job(lambda card, keys, mute: card['key'] == key, lambda card: None))

    def redrawing(self) -> bool:
        try:
            run = self.store.redraw_status()
        except Exception as e:
            now = self.timer()
            if self._lease_warned is None or now - self._lease_warned >= LEASE_WARN_S:
                self._lease_warned = now
                log.warning('card posts not checked this pass, the Slack redraw lease could not be read: %s', error_kind(e))
            return True
        try:
            return bool(run.get('running')) and parse_iso(str(run.get('heartbeat') or '')) > self.clock() - REDRAW_STALE_S
        except (TypeError, ValueError):
            return False

    def reconcile(self) -> None:
        if self.redrawing():
            return

        def stale(card: dict, keys: frozenset, mute: str | None) -> bool:
            post = card['slackPost']
            drawn = post.get('drawn') or {}
            return drawn.get('sig') != drawn_sig(card, mute, stopped_who(card, mute, self.name_of)) or fit_head(card, mute, post, post.get('head')) is not None or bool(drawn.get('gone')) or (not ask_of(post) and bool(menu_kinds(card, mute)) and self.menu_on()) or self._fill(card, post) is not None or self._renotify(card, post)
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
        self._checker()
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

    def _parent(self, card: dict) -> dict | None:
        if not card.get('parent') or card['parent'] == card.get('key'):
            return None
        return self._card(card['parent'])

    def _card(self, key: str) -> dict | None:
        try:
            return self.store.anomaly(key)
        except NotFound:
            return None

    def _rewrite(self, card: dict, post: dict, head: dict | None, mute: str | None) -> int | None:
        key = card['key']
        if head is None or fit_head(card, mute, post, head) is not None:
            head = fit_head(card, mute, post, post.get('head'))
        who = stopped_who(card, mute, self.name_of)
        if head is not None:
            head = head if head.get('at') else head | {'at': iso(utc_now())}
            try:
                self.store.slack_post_head(key, post['ts'], head)
            except Exception as e:
                log.warning('the headline of the Slack post of %s was not stored: %s', key, error_kind(e))
        threaded = bool(post.get('threadOf'))
        ts = post.get('replyTs') if threaded else post['ts']
        if not ts:
            return 0
        fill = self._fill(card, post)
        body = fill or (post.get('body') if isinstance(post.get('body'), dict) else {})
        filled = bool(fill) or filled_body(post)
        notified = notified_line(card, self.plugin_name, self.clock()) if filled else ''
        draft = '\n'.join(t for t in (str(body.get('text') or '').strip() if filled else str(body.get('text') or ''), notified) if t) or None
        shown = card | {'slackDraft': {k: v for k, v in body.items() if k in ('text', 'headline', 'by')}} if draft else card
        try:
            menu, kinds = self._menu(card, ask_of(post), mute)
            placed = thread_why(card, self._card(post['threadOf']), post['threadOf'], post.get('threadWhy')) if threaded else ''
            message = anomaly_message(shown, card_url(self.base, key, card.get('num')), post.get('by') or SYSTEM, draft, menu, chat_url(self.base, key, card.get('num')),
                                      head=head or post['head'], attribution=None if threaded else self.slack.signature(), mute=mute, kinds=kinds, shots=post.get('screenshots'), base=self.base,
                                      parent=self._parent(card), who=who, hosts=self.hosts(), placed=placed)
            self.slack.update(post['channel'], ts, message['text'], message['blocks'])
        except SlackDown as e:
            self._tl.slow = e.wait if isinstance(e, SlackSlowed) else 0.0
            log.warning('the Slack post of %s was not rewritten and the rest of this batch waits, Slack did not answer: %s', key, error_kind(e))
            return None
        except SlackError as e:
            if e.code not in GONE:
                log.warning('the Slack post of %s was not rewritten: %s', key, error_kind(e))
                return 0
            log.info('the Slack post of %s can no longer be rewritten (%s), it is left as it is', key, e.code)
            try:
                self.store.slack_post_drawn(key, post['ts'], {'sig': drawn_sig(card, mute, who), 'at': iso(utc_now()), 'gone': e.code})
            except Exception as e2:
                log.warning('the Slack post of %s was not marked as gone: %s', key, error_kind(e2))
            return 0
        except Exception as e:
            log.warning('the Slack post of %s was not rewritten: %s', key, error_kind(e))
            return 0
        stored = False
        if fill:
            try:
                stored = self.store.slack_post_body(key, post['ts'], fill, SERVER_BODIES, NO_THREAD)
            except Exception as e:
                log.warning('the summary on the Slack post of %s was not stored: %s', key, error_kind(e))
        try:
            marks = ({'filled': True} if stored or (post.get('drawn') or {}).get('filled') else {}) | ({'notified': notified} if filled else {})
            self.store.slack_post_drawn(key, post['ts'], {'sig': drawn_sig(card, mute, who), 'at': iso(utc_now()), **({'menu': menu} if menu else {}), **marks})
        except Exception as e:
            log.warning('the Slack post of %s was rewritten but not recorded: %s', key, error_kind(e))
        log.info('Slack post of %s rewritten in place: %s', key, (head or post['head']).get('kind'))
        return 1

    def _checker(self) -> Callable[[str], str | None]:
        try:
            self._tl.check = self.check()
        except Exception as e:
            log.warning('the link rules for Slack post summaries could not be read: %s', error_kind(e))
            self._tl.check = lambda text: 'the link rules could not be read'
        return self._tl.check

    def _fill(self, card: dict, post: dict) -> dict | None:
        if not fillable(post):
            return None
        return summary_body(card, getattr(self._tl, 'check', None) or self._checker())

    def _renotify(self, card: dict, post: dict) -> bool:
        return filled_body(post) and notified_line(card, self.plugin_name, self.clock()) != (post.get('drawn') or {}).get('notified', '')


    def redraw_status(self) -> dict:
        return self.store.redraw_status()

    def redraw_start(self, cap: int = REDRAW_CAP, on_done: Callable[[dict], None] | None = None, stale_only: bool = False) -> bool:
        holder = uuid.uuid4().hex
        try:
            if not self.store.redraw_claim(holder, iso(utc_now() - dt.timedelta(seconds=REDRAW_STALE_S))):
                return False
        except Exception as e:
            log.warning('the Slack redraw lease could not be taken: %s', error_kind(e))
            return False

        def work() -> None:
            result = self.redraw(cap, holder, stale_only)
            if on_done is not None:
                try:
                    on_done(result)
                except Exception as e:
                    log.warning('the Slack redraw finished but its follow-up failed: %s', error_kind(e))

        threading.Thread(target=work, name='slack-redraw', daemon=True).start()
        return True

    def redraw(self, cap: int = REDRAW_CAP, holder: str | None = None, stale_only: bool = False) -> dict:
        own = holder is None
        holder = holder or uuid.uuid4().hex
        if own:
            self.store.redraw_claim(holder, iso(utc_now() - dt.timedelta(seconds=REDRAW_STALE_S)))
        redrawn = skipped = 0
        try:
            if not self.slack.enabled:
                return self._redraw_end(holder, 0, 0, 'off')
            self._checker()
            state = self.store.load()
            now = self.clock()
            rows = []
            for card in state.get('anomalies') or []:
                post = card.get('slackPost') or {}
                if not (card.get('key') and post.get('channel') and post.get('ts')):
                    continue
                rows.append((card_closed(card, mute_kind(mute_for(state, card['key'], now))), -last_drawn(post), card['key'], post['ts']))
            rows.sort(key=lambda r: (r[0], r[1]))
            rows = rows[:max(int(cap), 0)]
            self.store.redraw_progress(holder, total=len(rows))
            for index, (_, _, key, ts) in enumerate(rows):
                got = self._redraw_row(key, ts, stale_only)
                if got is None:
                    return self._redraw_end(holder, redrawn, skipped + len(rows) - index, 'slow')
                redrawn += got
                skipped += 1 - got
                if not self.store.redraw_progress(holder, done=redrawn, skipped=skipped):
                    log.warning('the Slack redraw lost its lease and stops')
                    return {'redrawn': redrawn, 'skipped': skipped, 'result': 'lost'}
                if index + 1 < len(rows):
                    self.pause(REDRAW_GAP_S)
            return self._redraw_end(holder, redrawn, skipped, 'done')
        except Exception as e:
            log.warning('the Slack redraw stopped: %s', error_kind(e))
            return self._redraw_end(holder, redrawn, skipped, 'failed')

    def _redraw_row(self, key: str, ts: str, stale_only: bool = False) -> int | None:
        now = self.clock()
        try:
            card = self.store.anomaly(key)
        except NotFound:
            return 0
        state = {'muted': self.store.muted_rules()}
        post = card.get('slackPost') or {}
        if post.get('ts') != ts or not post.get('channel') or gone_for_good(post.get('drawn') or {}, now):
            return 0
        mute = mute_kind(mute_for(state, key, now))
        head = fit_head(card, mute, post, post.get('head'))
        if stale_only and head is None and (post.get('drawn') or {}).get('sig') == drawn_sig(card, mute, stopped_who(card, mute, self.name_of)) and self._fill(card, post) is None and not self._renotify(card, post):
            return 0
        for attempt in range(REDRAW_TRIES):
            self._tl.slow = 0.0
            got = self._rewrite(card, post, head, mute)
            if got is not None:
                return got
            if attempt + 1 < REDRAW_TRIES:
                self.pause(getattr(self._tl, 'slow', 0.0) or self.retry_after_s)
        return None

    def _redraw_end(self, holder: str, redrawn: int, skipped: int, why: str) -> dict:
        result = {'redrawn': redrawn, 'skipped': skipped, 'result': why}
        try:
            self.store.redraw_finish(holder, done=redrawn, skipped=skipped, result=why)
        except Exception as e:
            log.warning('the Slack redraw result was not stored: %s', error_kind(e))
        log.info('Slack posts redrawn: %d, skipped: %d (%s)', redrawn, skipped, why)
        return result
