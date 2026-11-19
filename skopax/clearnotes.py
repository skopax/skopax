import logging
import re
import time
from typing import Callable

from skopax import causes, drafts
from skopax.contract import CLEAR_OUTCOMES
from skopax.engine import iso, mute_for, parse_iso
from skopax.errkind import kind as error_kind
from skopax.messages import msg
from skopax.slack import SLACK_MARKS, Slack, esc, local_time, reason_of, take_offer_blocks, task_button, task_confirm
from skopax.statenotes import merged, posted
from skopax.store import Store
from skopax.tickets import TITLE_LIMIT, default_title

log = logging.getLogger('skopax.clearnotes')

CLEAR_TYPE = 'clear-note'
RECURRING = re.compile(r'OOM|OutOfMemory|CrashLoop|Evict')
INFRA_SOURCES = ('capacity', 'grafana', 'alert', 'kube', 'varnish', 'cloudflare', 'cf', 'edge', 'pmm', 'mongo', 'argo', 'airbyte', 'redis', 'logflow')
NO_SERVICE = (None, '', '-')
HOUR_S = 3600
DAY_S = 24 * HOUR_S
LATE_S = 12 * HOUR_S
WAIT_TRIAGE = ('handled', 'investigating')


def took_text(seconds: float) -> str:
    minutes = max(1, int(seconds // 60))
    if seconds >= 2 * DAY_S:
        return msg('clear.took.days', n=int(seconds // DAY_S))
    if minutes < 60:
        return msg('clear.took.min', n=minutes)
    h, m = divmod(minutes, 60)
    return msg('clear.took.hourmin', h=h, m=m) if m else msg('clear.took.hour', h=h)


def recurs(card: dict) -> bool:
    return bool(RECURRING.search(' '.join(str(card.get(f) or '') for f in ('key', 'label', 'what'))))


def inert(text) -> str:
    return esc(' '.join(str(text).split())[:causes.WHAT_LIMIT]).translate(SLACK_MARKS)


def task_for(card: dict, prefix: str = '') -> dict | None:
    if card.get('ticket'):
        return None
    tech = card.get('src') in INFRA_SOURCES or card.get('svc') in NO_SERVICE
    title = f"{card.get('svc') or '-'} {card.get('label') or card['key']}"[:TITLE_LIMIT] if tech else default_title(card, prefix)
    return {'key': card['key'], 'title': title, 'description': '', 'points': None, 'project': None} | ({drafts.TECH: True} if tech else {})


def announced_at(card: dict) -> str:
    post = card['slackPost']
    return max(post.get('at') or '', (post.get('update') or {}).get('at') or '')


def plugin_said(card: dict) -> dict | None:
    note = card.get('clearNote')
    return note if isinstance(note, dict) and note.get('at') and note.get('at') == card.get('clearedAt') else None


def plugin_resolved(card: dict) -> str | None:
    outcome = (plugin_said(card) or {}).get('outcome')
    return outcome if outcome in CLEAR_OUTCOMES and card.get('triage') == 'handled' else None


def due(card: dict, by_key: dict, state: dict, now: float, settle_s: float) -> bool:
    if not posted(card) or card.get('status') != 'cleared' or card.get('expired') or merged(card, by_key) or card.get('muted') or (card.get('triage') in WAIT_TRIAGE and not plugin_resolved(card)):
        return False
    first, cleared = card.get('firstSeen'), card.get('clearedAt')
    if not first or not cleared or announced_at(card) < first or not settle_s <= now - parse_iso(cleared) <= LATE_S:
        return False
    if (card['slackPost'].get('clearNote') or {}).get('clearedAt', '') >= first:
        return False
    return not mute_for(state, card['key'], now)


class ClearNotes:
    def __init__(self, store: Store, slack: Slack, minutes: float, clock: Callable[[], float] = time.time, title_prefix: str = ''):
        self.store = store
        self.slack = slack
        self.minutes = minutes
        self.clock = clock
        self.title_prefix = title_prefix

    @property
    def enabled(self) -> bool:
        return self.minutes > 0 and self.slack.enabled

    def run_once(self, now: float | None = None) -> list[str]:
        now = self.clock() if now is None else now
        state = self.store.load()
        cards = state.get('anomalies') or []
        by_key = {c['key']: c for c in cards}
        return [c['key'] for c in cards if due(c, by_key, state, now, self.minutes * 60) and self._note(c, now)]

    def _note(self, card: dict, now: float) -> bool:
        key, post = card['key'], card['slackPost']
        if plugin_resolved(card):
            return self._resolved_note(card, now)
        took = parse_iso(card['clearedAt']) - parse_iso(card['firstSeen'])
        task = task_for(card, self.title_prefix)
        cause = causes.own(card) or {}
        event = cause.get('kind') == causes.EVENT
        again = event or cause.get('kind') == causes.RECURRING or recurs(card)
        tails = ('clear.recur_taken', 'clear.recur_free_task', 'clear.recur_free') if again else ('clear.taken', 'clear.free_task', 'clear.free')
        said = plugin_said(card) or {}
        words = {'what': inert(cause['what'])} if event else {}
        sentence = ('clear.plugin_event' if said.get('text') else 'clear.event') if event else ('clear.plugin_recurs' if again else 'clear.plugin_note') if said.get('text') else 'clear.recurs' if again else 'clear.cleared'
        text = msg(sentence, name=esc(f"{card.get('svc') or '-'} / {card.get('label') or '?'}"), at=local_time(card['clearedAt']), took=took_text(took),
                   tail=msg(tails[0] if card.get('triage') == 'progress' else tails[1] if task else tails[2]), plugin=esc(said.get('by') or ''), note=esc(said.get('text') or ''), **words)
        call = {'key': key, 'action': 'resolve'}
        offer = self.store.add_action({'type': CLEAR_TYPE, 'source': 'slack', 'key': key, 'target': key, 'label': f'Slack clear note: {key}'[:300], 'requestedBy': 'server',
                                       'cardAction': call, 'slack': {'where': 'card', 'channel': post['channel'], 'threadTs': post['ts']}} | ({'ticketProposal': task} if task else {}))
        blocks = take_offer_blocks(text, offer['id'], call)
        blocks[0]['text']['verbatim'] = True
        if task:
            blocks[-1]['elements'].append(task_button(offer['id'], task, primary=False, confirm=task_confirm(task['title'], task)))
        try:
            reply = self.slack.post({'channel': post['channel'], 'thread_ts': post['ts'], 'text': text, 'blocks': blocks})
        except Exception as e:
            log.warning('clear note for %s not sent: %s', key, error_kind(e))
            self.store.update_action(offer['id'], 'failed', f'not posted: {reason_of(e)}'[:300], 'requested')
            return False
        self.store.update_action(offer['id'], 'done', text, 'requested')
        self.store.slack_clear_noted(key, post['ts'], {'at': iso(now), 'ts': reply.get('ts'), 'clearedAt': card['clearedAt']})
        log.info('clear note for %s', key)
        return True

    def _resolved_note(self, card: dict, now: float) -> bool:
        key, post, said = card['key'], card['slackPost'], plugin_said(card)
        head = msg(f"clear.plugin_{said['outcome']}", plugin=esc(said.get('by') or ''), name=esc(f"{card.get('svc') or '-'} / {card.get('label') or '?'}"))
        text = head + (f" \u00b7 {esc(said['text'])}" if said.get('text') else '')
        try:
            reply = self.slack.post({'channel': post['channel'], 'thread_ts': post['ts'], 'text': text})
        except Exception as e:
            log.warning('clear note for %s not sent: %s', key, error_kind(e))
            return False
        self.store.slack_clear_noted(key, post['ts'], {'at': iso(now), 'ts': reply.get('ts'), 'clearedAt': card['clearedAt'], 'by': said.get('plugin')})
        log.info('clear note for %s, resolved by %s', key, said.get('plugin'))
        return True
