import logging
import time
from typing import Callable

from skopax.cardlink import card_url
from skopax.clearnotes import recurs
from skopax.engine import iso, mute_for, parse_iso
from skopax.errkind import kind as error_kind
from skopax.messages import msg
from skopax.slack import CARD_ACTION, SECTION_LIMIT, Slack, card_action_label, card_button, cut, esc, reason_of, take_offer_blocks
from skopax.statenotes import card_name, merged, posted, shared_thread
from skopax.store import Store

log = logging.getLogger('skopax.reminders')

ADDRESSED = ('progress', 'handled')
WAITING_SEVS = ('warn', 'crit')
REMINDER_TYPE = 'reminder'
DAY_H = 24
GROUP_MAX = 10


def waiting(card: dict, by_key: dict, state: dict, now: float) -> bool:
    if not posted(card) or merged(card, by_key) or card.get('muted') or card.get('triage') in ADDRESSED or mute_for(state, card['key'], now):
        return False
    return card.get('status') == 'open' or (card.get('status') == 'cleared' and card.get('sev') in WAITING_SEVS)


def last_nudge(card: dict) -> float:
    post = card['slackPost']
    stamps = [post.get('at'), (post.get('reminder') or {}).get('at'), (post.get('clearNote') or {}).get('at'), card.get('firstSeen')]
    return max(parse_iso(s) for s in stamps if s)


def waiting_since(card: dict) -> float:
    return max(parse_iso(s) for s in (card['slackPost'].get('at'), card.get('firstSeen')) if s)


def who_text(card: dict) -> str:
    owner = str(card.get('owner') or '').strip()
    return msg('reminder.owned', owner=esc(owner)) if owner else msg('reminder.nobody')


def state_text(card: dict) -> str:
    return msg('reminder.still' if card.get('status') == 'open' else 'reminder.quiet' if recurs(card) else 'reminder.calmed')


def age_text(hours: float) -> str:
    return msg('reminder.age.hours', n=int(hours)) if hours < 2 * DAY_H else msg('reminder.age.days', n=int(hours // DAY_H))


class Reminders:
    def __init__(self, store: Store, slack: Slack, hours: float, base_url: str, clock: Callable[[], float] = time.time):
        self.store = store
        self.slack = slack
        self.hours = hours
        self.base_url = base_url.rstrip('/')
        self.clock = clock

    @property
    def enabled(self) -> bool:
        return self.hours > 0 and self.slack.enabled

    def run_once(self, now: float | None = None) -> list[str]:
        now = self.clock() if now is None else now
        state = self.store.load()
        cards = state.get('anomalies') or []
        by_key = {c['key']: c for c in cards}
        due = [c for c in cards if waiting(c, by_key, state, now) and now - last_nudge(c) >= self.hours * 3600]
        threads: dict[tuple, list[dict]] = {}
        for card in due:
            threads.setdefault((card['slackPost']['channel'], card['slackPost']['ts']), []).append(card)
        sent = []
        for group in threads.values():
            if len(group) == 1 and not shared_thread(group[0], cards):
                sent += [group[0]['key']] if self._remind(group[0], now) else []
                continue
            for i in range(0, len(group), GROUP_MAX):
                sent += self._remind_many(group[i:i + GROUP_MAX], now)
        return sent

    def _link(self, card: dict) -> str:
        return card_url(self.base_url, card['key'], card.get('num'))

    def _remind_many(self, cards: list[dict], now: float) -> list[str]:
        post = cards[0]['slackPost']
        head = msg('reminder.group.head', n=len(cards), age=age_text((now - min(waiting_since(c) for c in cards)) / 3600),
                   who=msg('reminder.group.started' if any(c.get('owner') for c in cards) else 'reminder.group.nobody'))
        lines, blocks, offers = [], [{'type': 'section', 'text': {'type': 'mrkdwn', 'text': head}}], []
        for card in cards:
            is_open = card.get('status') == 'open'
            owner = msg('reminder.group.owner', owner=esc(str(card['owner']).strip())) if str(card.get('owner') or '').strip() else ''
            line = msg('reminder.group.line', name=esc(card_name(card)), owner=owner, state=state_text(card), link=self._link(card))
            call = {'key': card['key'], 'action': 'take' if is_open else 'resolve'}
            offer = self.store.add_action({'type': REMINDER_TYPE, 'source': 'slack', 'key': card['key'], 'target': card['key'], 'label': f"Slack reminder: {card['key']}"[:300],
                                           'requestedBy': 'server', 'cardAction': call, 'slack': {'where': 'card', 'channel': post['channel'], 'threadTs': post['ts']}})
            offers.append((card, offer, line))
            lines.append(line)
            blocks += [{'type': 'section', 'text': {'type': 'mrkdwn', 'text': cut(line, SECTION_LIMIT)}},
                       {'type': 'actions', 'elements': [card_button(CARD_ACTION, offer['id'], card_action_label(call), call['action'], primary=True)]}]
        text = '\n'.join([head] + lines)
        try:
            reply = self.slack.post({'channel': post['channel'], 'thread_ts': post['ts'], 'text': text, 'blocks': blocks})
        except Exception as e:
            log.warning('reminder for %d cards in one thread not sent: %s', len(cards), error_kind(e))
            for _, offer, _ in offers:
                self.store.update_action(offer['id'], 'failed', f'not posted: {reason_of(e)}'[:300], 'requested')
            return []
        for card, offer, line in offers:
            self.store.update_action(offer['id'], 'done', line, 'requested')
            n = (card['slackPost'].get('reminder') or {}).get('n', 0) + 1
            self.store.slack_reminded(card['key'], post['ts'], {'at': iso(now), 'ts': reply.get('ts'), 'n': n})
        log.info('one reminder for %d cards in thread %s', len(cards), post['ts'])
        return [c['key'] for c in cards]

    def _remind(self, card: dict, now: float) -> bool:
        key, post = card['key'], card['slackPost']
        is_open = card.get('status') == 'open'
        text = msg('reminder.card', age=age_text((now - waiting_since(card)) / 3600), who=who_text(card), state=state_text(card), link=self._link(card))
        call = {'key': key, 'action': 'take' if is_open else 'resolve'}
        offer = self.store.add_action({'type': REMINDER_TYPE, 'source': 'slack', 'key': key, 'target': key, 'label': f'Slack reminder: {key}'[:300], 'requestedBy': 'server',
                                       'cardAction': call, 'slack': {'where': 'card', 'channel': post['channel'], 'threadTs': post['ts']}})
        try:
            reply = self.slack.post({'channel': post['channel'], 'thread_ts': post['ts'], 'text': text, 'blocks': take_offer_blocks(text, offer['id'], call)})
        except Exception as e:
            log.warning('reminder for %s not sent: %s', key, error_kind(e))
            self.store.update_action(offer['id'], 'failed', f'not posted: {reason_of(e)}'[:300], 'requested')
            return False
        self.store.update_action(offer['id'], 'done', text, 'requested')
        n = (post.get('reminder') or {}).get('n', 0) + 1
        self.store.slack_reminded(key, post['ts'], {'at': iso(now), 'ts': reply.get('ts'), 'n': n})
        log.info('reminder %d for %s', n, key)
        return True
