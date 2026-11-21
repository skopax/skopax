import datetime as dt
import functools
import json
import logging
import queue
import re
import threading
import time
from typing import Callable
from urllib.parse import quote

from slack_sdk.socket_mode import SocketModeClient
from slack_sdk.socket_mode.response import SocketModeResponse

from skopax.actions import PRESET
from skopax.config import Setting
from skopax import brand, drafts, history
from skopax.engine import iso as epoch_iso, parse_iso
from skopax.errkind import Authored, kind as error_kind
from skopax.features import off_text
from skopax.jiralinks import listed, site_of, ticket_finder, ticket_projects
from skopax import clock
from skopax.investigator import GATE_HOURLY_CAP, prompt
from skopax.investigator.gate import GateDown
from skopax.investigator.tools import CARD_ACTIONS, DEFAULT_MUTE_HOURS
from skopax import messages
from skopax.investigator.toolset import NoProposal, ProposalClosed, ProposalUnavailable
from skopax.messages import msg
from skopax.operators import OperatorUnknown
from skopax.slack import CARD_ACTION, CARD_MENU, CREATE_TASK, FILE_HOST, IMAGE_BYTES_MAX, IMAGE_MIMES, KNOWN_VIEW, POST_ACTIONS, PROPOSAL_CONFIRM, SCOPE_ERRORS, SECTION_LIMIT, SlackError, answer_blocks, card_action_label, cut, esc, known_view, cut_clear, guard_addresses, link_addresses, reason_of, redact, take_offer_blocks
from skopax.statenotes import card_name, who_of
from skopax.store import NotFound, new_id
from skopax.tickets import slack_reason

log = logging.getLogger('skopax.slackevents')

HISTORY_SCOPES = 'channels:history, groups:history or im:history'
MENTION = re.compile(r'<@([UW][A-Z0-9]+)(?:\|[^>]*)?>')
LINK = re.compile(r'<(https?://[^|>]+)\|([^>]+)>')
BARE_LINK = re.compile(r'<(https?://[^|>]+)>')
CHANNEL = re.compile(r'<#C[A-Z0-9]+\|([^>]*)>')
TICKET_KEY = re.compile(r'\b([A-Z][A-Z0-9]+-\d+)\b')
SLACK_TOKEN = re.compile(r'<[^>]*>')
NAME_AFTER = r"(?:['\u2019]\w+)?(?![\w/-])(?!\.\w)"
SPACES = re.compile(r'[ \t]+')
QUESTION_LIMIT = 2000
THREAD_KEEP = 10
MAX_AGE_S = 7 * 86400
ASK_WINDOW_S = 3600
NOT_READY = 'not-ready'
NOT_READY_HELD = 'not-ready-held'
PICK_WINDOW_S = 900
CONTEXT_KEEP = 5
LABEL_LIMIT = 300
REASON_LIMIT = 200
QUEUE_SIZE = 50
CONNECT_RETRY_S = 60.0
LISTENER_WAIT_S = 30.0
DOWN_ALERT_S = 120.0
DOWN_REPEAT_S = 1800.0
SDK_LOGGER = 'slack_sdk.socket_mode.builtin.client'
SELF_HEALING = 'Failed to check the state of sock'

DM_SESSION_S = 6 * 3600
DM_THREAD = 6
DM_HIDDEN = ('question', 'questionFrom', 'smallTalk', 'wouldPost', 'ticket')
IMAGE_ONLY = '(no text: the question is the attached image)'
IMAGES_MAX = 3
FILE_SUBTYPE = 'file_share'
CARD_OFFERS = {'take': 'slackevents.offer.take', 'resolve': 'slackevents.offer.resolve', 'known': 'slackevents.offer.known', 'mute': 'slackevents.offer.mute'}
OFFER_NAMES = {'take': 'In progress', 'resolve': 'Resolve', 'known': 'Known noise', 'mute': 'Mute'}
OFFER_REASON = 150
AGREE = '+1'
MIND_BLOWN = 'exploding_head'
TAKE_OFFER_TYPE = 'take-offer'
TAKEN = ('progress', 'handled')


def listening(handler, what: str) -> bool:
    if handler.on():
        handler.off_logged = False
        return True
    if not handler.off_logged:
        handler.off_logged = True
        log.info('Slack %s are ignored: %s', what, off_text('slack-listener'))
    return False


def bot_tagged(text: str, bot_id: str) -> bool:
    return bool(bot_id) and any(m.group(1) == bot_id for m in MENTION.finditer(text or ''))


@functools.lru_cache(maxsize=8)
def name_pattern(name: str) -> re.Pattern:
    return re.compile(r'(?<![\w/-])' + re.escape(name) + NAME_AFTER, re.I)


def named(text: str) -> bool:
    return bool(name_pattern(brand.product()).search(SLACK_TOKEN.sub(' ', text or '')))


def other_mentions(text: str, bot_id: str) -> bool:
    return any(m.group(1) != bot_id for m in MENTION.finditer(text or ''))


def decode(text: str, bot_id: str = '') -> str:
    out = MENTION.sub(lambda m: '' if m.group(1) == bot_id else f'@{m.group(1)}', text or '')
    out = LINK.sub(lambda m: f'{m.group(2)} ({m.group(1)})', out)
    out = BARE_LINK.sub(lambda m: m.group(1), out)
    out = CHANNEL.sub(lambda m: f'#{m.group(1)}', out)
    out = out.replace('&lt;', '<').replace('&gt;', '>').replace('&amp;', '&')
    return '\n'.join(SPACES.sub(' ', line).strip() for line in out.splitlines()).strip()


def question_text(text: str, bot_id: str) -> str:
    return decode(text, bot_id)[:QUESTION_LIMIT]


def from_bot(message: dict, bot_id: str) -> bool:
    return bool(message.get('bot_id')) or message.get('user') == bot_id


def thread_view(posts: list[dict], bot_id: str, skip_ts: str) -> list[dict]:
    out = [{'who': 'bot' if from_bot(m, bot_id) else 'person', 'text': decode(m.get('text') or '', bot_id)[:prompt.SLACK_THREAD_LINE]}
           for m in posts if m.get('ts') != skip_ts]
    return out[-THREAD_KEEP:]


def earlier_question(posts: list[dict], bot_id: str, user: str, ts: str, now: float) -> tuple[str, str] | None:
    start = max((i + 1 for i, m in enumerate(posts) if from_bot(m, bot_id)), default=0)
    for m in reversed(posts[start:]):
        at = float(m.get('ts') or 0)
        if m.get('user') != user or not at < float(ts) or now - at > PICK_WINDOW_S:
            continue
        question = question_text(m.get('text') or '', bot_id)
        if question:
            return m['ts'], question
    return None


def images_of(message: dict) -> tuple[list[dict], int]:
    kept, skipped = [], 0
    for f in message.get('files') or []:
        url = str((f or {}).get('url_private') or '') if isinstance(f, dict) else ''
        size = f.get('size') if isinstance(f, dict) else None
        usable = (isinstance(f, dict) and f.get('mimetype') in IMAGE_MIMES and url.startswith(f'https://{FILE_HOST}/') and isinstance(size, int) and 0 < size <= IMAGE_BYTES_MAX)
        if usable and len(kept) < IMAGES_MAX:
            kept.append({'name': str(f.get('name') or 'image')[:120], 'mime': f['mimetype'], 'size': size, 'url': url})
        else:
            skipped += 1
    return kept, skipped


def offerable(card: dict | None, kind: str) -> bool:
    if not card or card.get('muted') or card.get('triage') == 'handled' or card.get(f'{kind}Offered'):
        return False
    if kind in ('take', 'mute'):
        return card.get('status') == 'open' and (kind == 'mute' or card.get('triage') not in TAKEN)
    return card.get('status') in ('open', 'cleared')


def new_ticket(card: dict | None, text: str, projects=()) -> str | None:
    projects = listed(projects)
    if not projects:
        return None
    if not card or card.get('status') != 'open' or card.get('muted') or card.get('triage') == 'handled':
        return None
    offered = (card.get('ticketOffered') or {}).get('ticket')
    return next((k for k in ticket_finder(projects).findall(text or '') if k not in (card.get('ticket'), offered)), None)


def refused_read(record: dict | None, e: SlackError) -> None:
    if record is not None and e.code in SCOPE_ERRORS:
        record['readRefused'] = e.code
        record['missingScope'] = getattr(e, 'needed', None) or HISTORY_SCOPES


class Intake:
    great_emoji = Setting('slack_great_emoji', lambda e: str(e).strip(': '))
    ask_cap = Setting('slack_ask_cap')
    ask_channels = Setting('slack_ask_channels')
    user_cap = Setting('slack_user_ask_cap')
    dm_chats = Setting('slack_dm_chats')
    chat_cap = Setting('chat_daily_cap')
    dm_text = Setting('chat_max_chars')

    def __init__(self, store, slack, operators, gate, talk, investigator, settings, clock=time.time, base_url: str = '', ticket_extra=lambda: (),
                 on: Callable[[], bool] = lambda: True, chat: Callable[[], bool] = lambda: True, not_ready: Callable[[], str] = lambda: msg('slackevents.not_ready'),
                 jira_site: Callable[[], str] = lambda: ''):
        self.jira_site = jira_site
        self.on = on
        self.chat = chat
        self.not_ready = not_ready
        self.off_logged = False
        self.base_url = base_url.rstrip('/')
        self.ticket_extra = ticket_extra
        self.store = store
        self.slack = slack
        self.operators = operators
        self.gate = gate
        self.talk = talk
        self.investigator = investigator
        self.settings = settings
        self.listen = settings.slack_listen
        self.clock = clock

    def handle(self, event: dict) -> str | None:
        if not listening(self, 'events'):
            return None
        if event.get('type') not in ('app_mention', 'message') or event.get('subtype') not in (None, 'thread_broadcast', FILE_SUBTYPE) or event.get('bot_id'):
            return None
        bot = self.slack.bot_user()
        user, channel, ts = event.get('user'), event.get('channel'), event.get('ts')
        if not user or not channel or not ts or user == bot:
            return None
        text = event.get('text') or ''
        tagged = bot_tagged(text, bot)
        thread_ts = event.get('thread_ts')
        soft = not tagged and named(text)
        is_reply = bool(thread_ts) and thread_ts != ts
        dm = event.get('channel_type') == 'im'
        card = self.store.anomaly_by_post(channel, thread_ts) if is_reply and not dm else None
        if dm:
            if event.get('type') != 'message' or not self.dm_chats:
                return None
            where, root, born, tagged, soft = 'dm', thread_ts if is_reply else ts, None, True, False
        elif card:
            where, root, born = 'card', thread_ts, (card.get('slackPost') or {}).get('at')
        elif (tagged or soft) and not is_reply:
            if not (self._ask_channel(channel) if tagged else self._main_channel(channel)):
                return None
            where, root, born = 'channel', ts, None
        elif is_reply and (first := self.store.channel_ask_root(channel, thread_ts)):
            where, root, born = 'channel', thread_ts, first.get('requestedAt')
        elif tagged and is_reply:
            if not self._ask_channel(channel):
                return None
            where, root, born = 'channel', thread_ts, None
        else:
            return None
        now = self.clock()
        if born and not tagged and now - parse_iso(born) > MAX_AGE_S:
            return None
        if not tagged and other_mentions(text, bot):
            return None
        if self.store.seen(channel, ts, dt.datetime.fromtimestamp(now, dt.timezone.utc)):
            return None
        record = {'tagged': tagged, 'where': where, 'user': user, 'threadTs': root} | ({'named': True} if soft else {})
        try:
            return self._process(event, bot, text, tagged, where, root, card, now, record)
        except Exception as e:
            log.error('slack message %s in %s failed after the dedupe: %s', ts, channel, error_kind(e))
            try:
                self._record(channel, ts, record, 'error')
            except Exception as inner:
                log.error('slack intake record for %s in %s failed: %s', ts, channel, error_kind(inner))
            return 'error'

    def _ask_channel(self, channel: str) -> bool:
        if self.ask_channels == 'main':
            return self._main_channel(channel)
        if self.ask_channels == 'all' or channel in self.store.post_channels():
            return True
        return channel in {c.strip() for c in self.ask_channels.split(',') if c.strip()}

    def _main_channel(self, channel: str) -> bool:
        channels = self.store.post_channels()
        if not channels:
            log.info('channel ask in %s dropped: no card post seen yet', channel)
            return False
        return channel in channels

    def _process(self, event, bot, text, tagged, where, root, card, now, record) -> str:
        user, channel, ts = event['user'], event['channel'], event['ts']
        if event.get('user_team') and event.get('team') and event['user_team'] != event['team']:
            return self._record(channel, ts, record, 'external')
        try:
            login, operator = self.operators.access_for(user)
        except OperatorUnknown:
            login, operator = None, False
        if login is None:
            login = self.operators.email_of(user) or f'slack:{user}'
        record['operator'] = operator
        question = question_text(text, bot)
        if not question and not tagged:
            return self._record(channel, ts, record, 'empty')
        record['login'] = login
        thread = None
        images, skipped = self._images(event, channel, root, ts)
        if images or skipped:
            record['images'] = len(images)
        if not question and images:
            question = IMAGE_ONLY
        if not question:
            posts = self._replies(channel, root, record) if root != ts else []
            picked = earlier_question(posts, bot, user, ts, now)
            if picked is None and record.get('readRefused'):
                return self._say(channel, ts, root, record, 'read-refused', msg('slackevents.read_refused', scope=record.get('missingScope') or HISTORY_SCOPES))
            if picked is None:
                return self._say(channel, ts, root, record, 'empty-tag', msg('slackevents.empty_tag'))
            record['questionFrom'], question = picked
            thread = thread_view([m for m in posts if m.get('ts') != picked[0]], bot, ts)
        if tagged:
            record['question'] = question[:LABEL_LIMIT]
        return self._decide(channel, ts, root, where, user, login, question, card, tagged, record, now, thread, (images, skipped))

    def _images(self, event: dict, channel: str, root: str, ts: str) -> tuple[list[dict], int]:
        found = images_of(event)
        if found != ([], 0) or event.get('type') != 'app_mention':
            return found
        return images_of(next((m for m in self._replies(channel, root) if m.get('ts') == ts), {}))

    def _decide(self, channel, ts, root, where, user, login, question, card, tagged, record, now, thread=None, attached=((), 0)) -> str:
        if self.listen == 'off' or not (self.investigator.enabled or tagged):
            return self._record(channel, ts, record, 'off')
        if not self.investigator.enabled:
            if self.store.count_intake(NOT_READY, epoch_iso(now - ASK_WINDOW_S), user):
                return self._record(channel, ts, record, NOT_READY_HELD)
            return self._say(channel, ts, root, record, NOT_READY, self.not_ready())
        if where == 'dm':
            refused = self._dm_open(channel, ts, root, login, question, record, now)
            if refused is not None:
                return refused
            thread = record['_dm']['thread']
        ticket = new_ticket(card, question, lambda: ticket_projects(self.settings, self.ticket_extra())) if where == 'card' and site_of(self.jira_site) else None
        if ticket and not tagged and record.get('operator'):
            offered = self._offer_ticket(channel, ts, root, user, login, card, ticket, record, now)
            if offered is not None:
                self._remember_said(card, login, question, 'ticket', False, channel, ts, now)
                return offered
        elif ticket:
            record['ticket'] = ticket
        halted = self.investigator.why_halted(now)
        if halted == 'off':
            return self._say(channel, ts, root, record, 'investigator-off', off_text('investigator', 'slackevents.feature_off') if tagged else None)
        if halted in ('stopped', 'capped'):
            return self._say(channel, ts, root, record, halted, msg('slackevents.stopped' if halted == 'stopped' else 'slackevents.capped') if tagged else None)
        if where == 'dm':
            held = None
        elif self.user_cap > 0 and self.store.count_actions('ask', epoch_iso(now - ASK_WINDOW_S), source='slack', by=login) >= self.user_cap:
            held = ('user-cap', 'slackevents.user_capped' if tagged else None, {'cap': self.user_cap})
        elif self.store.count_actions('ask', epoch_iso(now - ASK_WINDOW_S), source='slack') >= self.ask_cap:
            held = ('ask-cap', 'slackevents.ask_capped' if tagged else None, {})
        else:
            held = None
        queued = held is None and where != 'dm' and self.store.open_thread_ask(channel, root) is not None
        gate_capped = self.store.count_gate_calls(epoch_iso(now - ASK_WINDOW_S)) >= GATE_HOURLY_CAP
        if held and gate_capped:
            return self._hold(channel, ts, root, record, held)
        if thread is None:
            thread = self._thread(channel, root, ts, record) if root != ts else self._recent(channel, ts, record)
        verdict = None
        keys = [card['key']] if card else []
        if gate_capped:
            if not tagged:
                return self._record(channel, ts, record, 'gate-cap')
            record['gateSkipped'] = 'cap'
        else:
            try:
                shown, other = len(attached[0]), attached[1]
                asked = question + (f" [{shown} image{'' if shown == 1 else 's'} attached]" if shown else f" [{other} file{'' if other == 1 else 's'} attached]" if other else '')
                verdict = self.gate.check(prompt.gate_input(prompt.gate_context(card), thread, asked, tagged), 'slack', login, keys)
                record['gate'] = {'addressed': verdict.addressed, 'onTopic': verdict.on_topic, 'smallTalk': verdict.small_talk, 'cardIntent': verdict.card_intent, 'agrees': verdict.agrees, 'mindBlown': verdict.mind_blown,
                                  'great': verdict.great,
                                  'language': verdict.language, 'reason': verdict.reason, 'usd': round(verdict.usd, 6)}
                record.setdefault('question', question[:LABEL_LIMIT])
            except GateDown as e:
                record['gate'] = {'down': msg('page.gate_down'), 'error': e.code}
                record.setdefault('question', question[:LABEL_LIMIT])
                if held:
                    return self._hold(channel, ts, root, record, held)
                if not tagged:
                    return self._record(channel, ts, record, 'gate-down')
        if verdict is not None:
            self._react(channel, ts, record, [name for name, on in ((AGREE, verdict.agrees and not tagged and not verdict.addressed), (MIND_BLOWN, verdict.mind_blown),
                                                                    (self.great_emoji, verdict.great and not verdict.mind_blown and bool(self.great_emoji))) if on])
        if verdict is not None and verdict.small_talk and not verdict.on_topic and (tagged or verdict.addressed):
            said = self._small_talk(prompt.gate_input(prompt.gate_context(card), thread, question, tagged), login, keys, record, now)
            if said is not None:
                return self._say(channel, ts, root, record, 'small-talk', said)
            if record['smallTalkSkipped'] == 'cap':
                with messages.speaking(verdict.language):
                    capped = msg('slackevents.small_talk_capped')
                return self._say(channel, ts, root, record, 'small-talk-cap', capped)
            if record['smallTalkSkipped'] == 'disabled' and not tagged:
                return self._record(channel, ts, record, 'small-talk-off')
        if verdict is not None and card and not tagged and not verdict.addressed and (verdict.agrees or verdict.card_intent in CARD_OFFERS):
            self._remember_said(card, login, question, verdict.card_intent if verdict.card_intent in CARD_OFFERS else 'agree', verdict.agrees, channel, ts, now)
        if verdict is not None and verdict.card_intent in CARD_OFFERS and not tagged and not verdict.addressed and record.get('operator'):
            offered = self._offer_card(channel, ts, root, user, login, card, verdict.card_intent, question, record, now)
            if offered is not None:
                return offered
        if held:
            return self._hold(channel, ts, root, record, held, verdict)
        if verdict is not None:
            if not tagged and not verdict.addressed:
                return self._record(channel, ts, record, 'not-addressed')
            if not verdict.on_topic:
                with messages.speaking(verdict.language):
                    refused = msg('slackevents.off_topic')
                return self._say(channel, ts, root, record, 'off-topic', refused)
        return self._accept(channel, ts, root, where, user, login, question, card, thread, verdict.language if verdict else 'en', record, queued, attached)

    def _dm_open(self, channel: str, ts: str, root: str, login: str, question: str, record: dict, now: float) -> str | None:
        site = '\n' + msg('slackevents.dm.site', url=f'{self.base_url}/chat/') if self.base_url else ''
        if not self.chat():
            return self._say(channel, ts, root, record, 'chat-off', off_text('chat', 'slackevents.feature_off') + site)
        if len(question) > self.dm_text:
            return self._say(channel, ts, root, record, 'dm-long', msg('slackevents.dm.long', limit=self.dm_text))
        chat_id = self.store.dm_chat_id(login, channel, epoch_iso(now - DM_SESSION_S))
        chat = self.store.chat(login, chat_id) if chat_id else None
        if chat and chat.get('pending'):
            return self._say(channel, ts, root, record, 'busy', msg('slackevents.busy') + site)
        if self.chat_cap <= 0:
            return self._say(channel, ts, root, record, 'dm-off', msg('slackevents.dm.off') + site)
        if self.store.count_actions('ask', epoch_iso(clock.midnight(now)), source='chat', by=login) >= self.chat_cap:
            return self._say(channel, ts, root, record, 'dm-cap', msg('slackevents.dm.capped', cap=self.chat_cap) + site)
        thread = []
        for m in ((chat or {}).get('messages') or [])[-DM_THREAD:]:
            thread += [{'who': 'person', 'text': m.get('text') or ''}] + ([{'who': 'bot', 'text': m['answer']}] if m.get('answer') else [])
        record['_dm'] = {'chatId': chat_id or new_id(), 'text': question, 'thread': thread, 'channel': channel}
        return None

    def _dm_keep(self, record: dict, login: str, text: str, extra: dict) -> None:
        dm = record['_dm']
        self.store.add_action({'type': 'ask', 'source': 'chat', 'key': 'ask', 'target': f"chat:{dm['chatId']}", 'text': dm['text'], 'label': dm['text'][:LABEL_LIMIT], 'cards': [],
                               'requestedBy': login, 'chat': {'id': dm['chatId'], 'dm': dm['channel']}, 'thread': dm['thread']} | extra, 'done', text)

    def _remember_said(self, card: dict, login: str, text: str, kind: str, agreed: bool, channel: str, ts: str, now: float) -> None:
        self.store.record_card(card, history.SAID_EVENT, said=history.said_entry(text, login, kind, agreed, epoch_iso(now), self.slack.permalink(channel, ts)))

    def _react(self, channel: str, ts: str, record: dict, names: list[str]) -> None:
        if not names:
            return
        if self.listen != 'live':
            record['wouldReact'] = names
            return
        try:
            for name in names:
                if not self.slack.react(channel, ts, name):
                    record['reactSkipped'] = 'no reactions:write scope'
                    return
                record.setdefault('reacted', []).append(name)
        except SlackError as e:
            log.warning('reaction in %s failed: %s', channel, error_kind(e))
            record['reactSkipped'] = reason_of(e)[:REASON_LIMIT]

    def _offer_card(self, channel: str, ts: str, root: str, user: str, login: str, card: dict | None, kind: str, said: str, record: dict, now: float) -> str | None:
        if not offerable(card, kind):
            return None
        name = who_of(login)
        text = msg(CARD_OFFERS[kind], name=esc(name), hours=DEFAULT_MUTE_HOURS)
        if self.listen != 'live':
            return self._say(channel, ts, root, record, f'{kind}-offered', text)
        if not self.store.offer_card(card['key'], kind, {'ts': ts, 'by': login, 'at': epoch_iso(now)}):
            return None
        call = {'key': card['key'], 'action': kind} | ({'owner': login} if kind == 'take' else {}) | ({'hours': DEFAULT_MUTE_HOURS} if kind == 'mute' else {})
        if kind == 'known':
            call['reason'] = f'{name} in Slack: {said}'[:OFFER_REASON]
        offer = self.store.add_action({'type': f'{kind}-offer', 'source': 'slack', 'key': card['key'], 'target': card['key'], 'label': f'{OFFER_NAMES[kind]} offer in Slack: {name}'[:LABEL_LIMIT],
                                       'requestedBy': login, 'cardAction': call, 'slack': {'where': 'card', 'channel': channel, 'threadTs': root, 'ts': ts, 'user': user}})
        record['actionId'] = offer['id']
        try:
            self.slack.post({'channel': channel, 'thread_ts': root, 'text': text, 'blocks': take_offer_blocks(text, offer['id'], call)})
        except SlackError as e:
            log.warning('%s offer in %s failed: %s', kind, channel, error_kind(e))
            self.store.release_card_offer(card['key'], kind, ts)
            self.store.update_action(offer['id'], 'failed', f'not posted: {reason_of(e)}'[:LABEL_LIMIT], 'requested')
            return self._record(channel, ts, record, f'{kind}-offer-failed')
        self.store.update_action(offer['id'], 'done', text, 'requested')
        return self._record(channel, ts, record, f'{kind}-offered')

    def _offer_ticket(self, channel: str, ts: str, root: str, user: str, login: str, card: dict, ticket: str, record: dict, now: float) -> str | None:
        text = msg('slackevents.offer.ticket', url=f'{site_of(self.jira_site)}/browse/{ticket}', key=ticket)
        record['ticket'] = ticket
        if self.listen != 'live':
            return self._say(channel, ts, root, record, 'ticket-offered', text)
        if not self.store.offer_ticket(card['key'], {'ticket': ticket, 'ts': ts, 'by': login, 'at': epoch_iso(now)}):
            return None
        call = {'key': card['key'], 'action': 'take', 'ticket': ticket} | ({'owner': card['owner']} if card.get('triage') == 'progress' and card.get('owner') else {})
        offer = self.store.add_action({'type': TAKE_OFFER_TYPE, 'source': 'slack', 'key': card['key'], 'target': card['key'], 'label': f'In progress offer in Slack: {ticket}'[:LABEL_LIMIT],
                                       'requestedBy': login, 'cardAction': call, 'slack': {'where': 'card', 'channel': channel, 'threadTs': root, 'ts': ts, 'user': user}})
        record['actionId'] = offer['id']
        try:
            self.slack.post({'channel': channel, 'thread_ts': root, 'text': text, 'blocks': take_offer_blocks(text, offer['id'], call)})
        except SlackError as e:
            log.warning('ticket offer in %s failed: %s', channel, error_kind(e))
            self.store.release_ticket_offer(card['key'], ts)
            self.store.update_action(offer['id'], 'failed', f'not posted: {reason_of(e)}'[:LABEL_LIMIT], 'requested')
            return self._record(channel, ts, record, 'ticket-offer-failed')
        self.store.update_action(offer['id'], 'done', text, 'requested')
        return self._record(channel, ts, record, 'ticket-offered')

    def _small_talk(self, text: str, login: str, keys: list[str], record: dict, now: float) -> str | None:
        skipped = self.talk.blocked(now)
        said = self.talk.reply(text, 'slack', login, keys) if skipped is None else None
        if said is None:
            record['smallTalkSkipped'] = skipped or 'failed'
            return None
        record['smallTalk'] = {'text': said.text, 'usd': round(said.usd, 6)}
        return cut(redact(esc(said.text)), SECTION_LIMIT)

    def _thread(self, channel: str, root: str, ts: str, record: dict | None = None) -> list[dict]:
        return thread_view(self._replies(channel, root, record), self.slack.bot_user(), ts)

    def _recent(self, channel: str, ts: str, record: dict | None = None) -> list[dict]:
        try:
            posts = self.slack.history(channel, ts, CONTEXT_KEEP)
        except SlackError as e:
            log.warning('channel read in %s failed: %s', channel, error_kind(e))
            refused_read(record, e)
            return []
        kept = [m for m in posts if 0 <= float(ts) - float(m.get('ts') or 0) <= PICK_WINDOW_S]
        return thread_view(sorted(kept, key=lambda m: float(m.get('ts') or 0)), self.slack.bot_user(), ts)

    def _replies(self, channel: str, root: str, record: dict | None = None) -> list[dict]:
        try:
            return self.slack.replies(channel, root, THREAD_KEEP + 1)
        except SlackError as e:
            log.warning('thread read in %s failed: %s', channel, error_kind(e))
            refused_read(record, e)
            return []

    def _accept(self, channel, ts, root, where, user, login, question, card, thread, language, record, queued=False, attached=((), 0)) -> str:
        dm = record.get('_dm') if where == 'dm' else None
        slack = {'where': where, 'channel': channel, 'threadTs': None if dm and root == ts else root, 'ts': ts, 'user': user, 'language': language,
                 'permalink': self.slack.permalink(channel, ts), 'operator': bool(record.get('operator'))}
        if where == 'channel' and channel not in self.store.post_channels():
            slack['outside'] = True
            name = self.slack.channel_name(channel)
            if name:
                slack['channelName'] = name
        if self.listen == 'live':
            try:
                slack['placeholderTs'] = self.slack.post({'channel': channel, 'thread_ts': slack['threadTs'], 'text': msg('slackevents.busy' if queued else 'slackevents.placeholder')}, sign=False)['ts']
            except SlackError as e:
                log.warning('placeholder in %s failed: %s', channel, error_kind(e))
        images = {'images': list(attached[0]), 'imagesSkipped': attached[1]} if attached[0] or attached[1] else {}
        if dm:
            action = self.store.add_action({'type': 'ask', 'source': 'chat', 'key': 'ask', 'target': f"chat:{dm['chatId']}", 'text': question, 'cards': [], 'label': question[:LABEL_LIMIT],
                                            'requestedBy': login, 'chat': {'id': dm['chatId'], 'dm': channel}, 'slack': slack, 'thread': thread} | images)
        else:
            action = self.store.add_action({'type': 'ask', 'source': 'slack', 'key': 'ask', 'target': f'slack:{channel}:{ts}', 'text': question,
                                            'cards': [card['key']] if card else [], 'label': f'Slack: {question}'[:LABEL_LIMIT], 'requestedBy': login,
                                            'slack': slack, 'thread': thread} | ({'mentionedTicket': record['ticket']} if record.get('ticket') else {}) | images)
        record['actionId'] = action['id']
        decision = 'queued' if queued else 'accepted'
        self._record(channel, ts, record, decision)
        self.investigator.kick(self.clock())
        return decision

    def _hold(self, channel: str, ts: str, root: str, record: dict, held: tuple, verdict=None) -> str:
        decision, key, values = held
        with messages.speaking(verdict.language if verdict is not None else None):
            text = msg(key, **values) if key else None
        return self._say(channel, ts, root, record, decision, text)

    def _say(self, channel: str, ts: str, root: str, record: dict, decision: str, text: str | None) -> str:
        dm = record.get('where') == 'dm'
        if text and dm and record.get('_dm') and decision in ('small-talk', 'off-topic'):
            self._dm_keep(record, record['login'], text, {'smallTalk': True, 'usd': (record.get('smallTalk') or {}).get('usd', 0)} if decision == 'small-talk' else {'refused': True})
        if text:
            if self.listen == 'live':
                try:
                    self.slack.post({'channel': channel, 'thread_ts': None if dm and root == ts else root, 'text': text}, sign=False)
                except SlackError as e:
                    log.warning('slack line in %s failed: %s', channel, error_kind(e))
            else:
                record['wouldPost'] = text
        return self._record(channel, ts, record, decision)

    def _ephemeral(self, channel: str, user: str, text: str, thread_ts: str | None, record: dict) -> None:
        if self.listen != 'live':
            record['wouldPost'] = text
            return
        try:
            self.slack.ephemeral(channel, user, text, thread_ts)
        except SlackError as e:
            log.warning('ephemeral line in %s failed: %s', channel, error_kind(e))

    def _record(self, channel: str, ts: str, record: dict, decision: str) -> str:
        kept = {k: v for k, v in record.items() if k != '_dm' and not (record.get('where') == 'dm' and k in DM_HIDDEN)}
        self.store.record_intake(channel, ts, {**kept, 'decision': decision})
        return decision


class Replier:
    def __init__(self, store, slack, settings, clock=time.time, attach=None, base_url: str = '', hosts: Callable[[], tuple[str, ...]] = lambda: ()):
        self.base_url = base_url.rstrip('/')
        self.hosts = hosts
        self.store = store
        self.slack = slack
        self.listen = settings.slack_listen
        self.clock = clock
        self.attach = attach

    @staticmethod
    def text_for(action: dict, outcome: str, recap: str, hosts: tuple[str, ...] = ()) -> str:
        body = redact(esc((recap or '').strip()))
        if outcome == 'done' and body:
            user = (action.get('slack') or {}).get('user') if (action.get('slack') or {}).get('where') != 'dm' else None
            return Replier.linked((f'<@{user}> ' if user else '') + body, hosts)
        if outcome == 'capped':
            return Replier.linked(msg('slackevents.reply.capped') + (f'\n{body}' if body else ''), hosts)
        if outcome == 'stopped':
            return msg('slackevents.reply.stopped')
        return msg('slackevents.reply.failed')

    @staticmethod
    def linked(text: str, hosts: tuple[str, ...]) -> str:
        return link_addresses(cut_clear(guard_addresses(cut_clear(text, SECTION_LIMIT), hosts), SECTION_LIMIT), hosts, SECTION_LIMIT)

    def reply(self, action: dict, outcome: str, recap: str) -> dict | None:
        s = action.get('slack') or {}
        if not s.get('channel'):
            return None
        text = self.text_for(action, outcome, recap, self.hosts())
        answered = outcome == 'done' and text != msg('slackevents.reply.failed')
        proposal = action.get('ticketProposal') if answered and s.get('where') == 'card' else None
        card_action = action.get('cardAction') if answered else None
        run_proposal = action.get('runProposal') if answered else None
        at = epoch_iso(self.clock())
        if self.listen != 'live':
            return self._keep(action, {'shadow': text, 'at': at} | ({'proposal': True} if proposal else {}) | ({'cardAction': True} if card_action else {}) | ({'confirm': True} if run_proposal else {}))
        card = self._card(card_action)
        card_action = card_action if card else None
        blocks = answer_blocks(text, proposal, action['id'] if proposal or card_action else None, card_action, card, run_proposal)
        chat_id = (action.get('chat') or {}).get('id') if s.get('where') == 'dm' else None
        if chat_id and self.base_url:
            blocks.append({'type': 'context', 'elements': [{'type': 'mrkdwn', 'text': msg('slackevents.dm.open', url=f'{self.base_url}/chat/thread/{chat_id}')}]})
        new_reply = {'channel': s['channel'], 'thread_ts': s.get('threadTs'), 'text': text, 'blocks': blocks}
        try:
            sent = self.slack.post(new_reply, sign=False)
        except SlackError as e:
            if not s.get('placeholderTs'):
                log.warning('slack reply for %s failed: %s', action.get('id'), error_kind(e))
                return None
            log.warning('slack reply for %s failed, writing it into the placeholder: %s', action.get('id'), error_kind(e))
            try:
                sent = self.slack.update(s['channel'], s['placeholderTs'], text, blocks)
            except SlackError as e2:
                log.warning('slack reply for %s failed: %s', action.get('id'), error_kind(e2))
                return None
        else:
            if s.get('placeholderTs'):
                try:
                    self.slack.delete(s['channel'], s['placeholderTs'])
                except SlackError as e:
                    log.info('placeholder of %s not deleted: %s', action.get('id'), error_kind(e))
        shots = action.get('screenshots') or []
        if answered and shots and self.attach is not None:
            try:
                self.attach(s['channel'], s.get('threadTs') or s.get('ts'), shots, 'slack ask', action.get('runId'))
            except Exception as e:
                log.warning('screenshots for the slack reply of %s were not attached: %s', action.get('id'), error_kind(e))
        return self._keep(action, {'ts': sent.get('ts'), 'text': text, 'at': at} | ({'button': True} if proposal or card_action or run_proposal else {}) | ({'cardAction': True} if card_action else {}))

    def _card(self, call: dict | None) -> dict | None:
        if not call:
            return None
        try:
            return self.store.anomaly(call['key'])
        except NotFound:
            return None

    def _keep(self, action: dict, stored: dict) -> dict:
        try:
            self.store.set_action_fields(action['id'], {'slackReply': stored})
        except NotFound:
            log.warning('action %s was deleted before its slack reply was stored', action.get('id'))
        return stored


class TicketRefused(Authored, Exception):
    pass


def without_buttons(blocks: list | None, action_id: str, value: str | None = None) -> list[dict]:
    out = []
    for b in blocks or []:
        if b.get('type') == 'actions':
            b = {**b, 'elements': [e for e in b.get('elements') or [] if not (str(e.get('action_id') or '').startswith(action_id) and (value is None or e.get('value') == value))]}
            if not b['elements']:
                continue
        out.append(b)
    return out


def card_buttons(message: dict, value: str) -> bool:
    blocks = list(message.get('blocks') or []) + [b for a in message.get('attachments') or [] for b in a.get('blocks') or []]
    return any(str(e.get('action_id') or '').startswith(CARD_ACTION) and e.get('value') != value for b in blocks if b.get('type') == 'actions' for e in b.get('elements') or [])


def colour(value) -> str | None:
    text = str(value or '').strip()
    return None if not text else text if text.startswith('#') else '#' + text


def way_in(sso: bool, account: bool, base: str, added_only: bool = False, viewer_default: bool = False, mapped: bool = False, closed: bool = False) -> str:
    if closed:
        return ''
    if account:
        return msg('slackevents.click.ask_access' if mapped else 'slackevents.click.ask_admin')
    if not sso:
        return ''
    if mapped:
        return msg('slackevents.click.ask_access')
    if added_only:
        return msg('slackevents.click.ask_add')
    key = 'slackevents.click.sign_in_then_ask' if viewer_default else 'slackevents.click.sign_in'
    return msg(key, url=base) if base else msg(key + '_bare')


class Clicks:
    def __init__(self, store, slack, operators, apply: Callable[[dict], str], jira_site: str | Callable[[], str] = '', on: Callable[[], bool] = lambda: True,
                 confirm: Callable[[str, str], dict] | None = None, background: Callable[[Callable[[], None]], None] | None = None, way_in: Callable[[str], str] | None = None,
                 hosts: Callable[[], tuple[str, ...]] = lambda: ()):
        self.on = on
        self.hosts = hosts
        self.way_in = way_in
        self.confirm = confirm
        self.background = background or (lambda job: threading.Thread(target=job, name='proposal-confirm', daemon=True).start())
        self.off_logged = False
        self.jira_site = jira_site
        self.store = store
        self.slack = slack
        self.operators = operators
        self.apply = apply

    def handle(self, payload: dict) -> str | None:
        if not listening(self, 'clicks'):
            return None
        if payload.get('type') == 'view_submission':
            return self._submitted(payload)
        if payload.get('type') != 'block_actions':
            return None
        menu = next((a for a in payload.get('actions') or [] if a.get('action_id') == CARD_MENU), None)
        if menu is not None:
            return self._menu(payload, menu)
        button = next((a for a in payload.get('actions') or [] if a.get('action_id') in (CREATE_TASK, CARD_ACTION, PROPOSAL_CONFIRM) or str(a.get('action_id') or '').startswith(CARD_ACTION + '_')), None)
        if button is None:
            return None
        card_click = button['action_id'] != CREATE_TASK
        kind = button['action_id'][len(CARD_ACTION) + 1:] if button['action_id'].startswith(CARD_ACTION + '_') else None
        user = (payload.get('user') or {}).get('id') or ''
        channel = (payload.get('channel') or {}).get('id') or ''
        message = payload.get('message') or {}
        thread_ts = message.get('thread_ts') or message.get('ts')
        try:
            login = self.operators.login_for(user)
        except OperatorUnknown:
            log.warning('click from %s: operator lookup is down, ignoring the click', user)
            return 'operator-unknown'
        if login is None:
            refused = 'proposals.non_operator' if button['action_id'] == PROPOSAL_CONFIRM else 'slackevents.click.action_non_operator' if card_click else 'slackevents.click.non_operator'
            return self._tell(channel, user, self._refused(refused, user), thread_ts, 'not-operator')
        if button['action_id'] == PROPOSAL_CONFIRM:
            if self.confirm is None:
                return self._tell(channel, user, msg('proposals.none'), thread_ts, 'gone')
            run_id = str(button.get('value') or '')
            self.background(lambda: self._confirm_job(channel, user, login, run_id, message, thread_ts))
            return 'confirming'
        found = self._ask(str(button.get('value') or ''), 'cardAction' if card_click else 'ticketProposal', kind)
        if found is None:
            return self._tell(channel, user, msg('slackevents.click.gone'), thread_ts, 'gone')
        ask, call = found
        if card_click:
            return self._card_action(channel, user, login, ask, call, message, thread_ts)
        return self._create_task(channel, user, login, ask, message, thread_ts)

    def _create_task(self, channel: str, user: str, login: str, ask: dict, message: dict, thread_ts: str | None) -> str:
        if self.store.claim_ticket(ask['id'], login) is None:
            try:
                key = (self.store.get_action(ask['id']).get('ticket') or {}).get('key')
            except NotFound:
                return self._tell(channel, user, msg('slackevents.click.gone'), thread_ts, 'gone')
            return self._tell(channel, user, msg('slackevents.click.done', key=key) if key else msg('slackevents.click.busy'), thread_ts, 'claimed')
        p = ask['ticketProposal']
        ticket = {'type': 'ticket', 'key': p['key'], 'target': p['key'], 'title': p['title'], 'description': p['description'], 'points': p.get('points'), 'project': p.get('project'),
                  'label': f"Create task from Slack: {p['title']}"[:LABEL_LIMIT], 'requestedBy': login, 'source': 'slack', 'askId': ask['id']} | ({drafts.TECH: True} if drafts.is_tech(p) else {})
        try:
            result = self.apply(ticket)
        except TicketRefused as e:
            self.store.release_ticket(ask['id'])
            self.store.add_action(ticket, 'failed', e.text[:LABEL_LIMIT])
            return self._tell(channel, user, msg('slackevents.click.refused', reason=slack_reason(e.text)[:REASON_LIMIT]), thread_ts, 'refused')
        except Exception as e:
            log.error('create task for ask %s failed: %s', ask['id'], error_kind(e))
            self.store.release_ticket(ask['id'])
            self.store.add_action(ticket, 'failed', error_kind(e)[:LABEL_LIMIT])
            return self._tell(channel, user, msg('slackevents.click.refused', reason=type(e).__name__), thread_ts, 'refused')
        self.store.add_action(ticket, 'done', result[:LABEL_LIMIT])
        m = TICKET_KEY.search(result)
        if m:
            self.store.ticket_created(ask['id'], m.group(1))
            line = msg('slackevents.click.opened', url=f"{site_of(self.jira_site)}/browse/{quote(m.group(1), safe='')}", key=esc(m.group(1)), login=esc(login))
            self._swap_button(channel, message, CREATE_TASK, line)
        else:
            log.warning('no ticket key in create result for ask %s: %s', ask['id'], result[:LABEL_LIMIT])
        return 'created'

    def _refused(self, key: str, user: str) -> str:
        text = msg(key)
        if self.way_in is None:
            return text
        try:
            hint = self.way_in(user)
        except Exception as e:
            log.warning('the way in for a refused click could not be read: %s', error_kind(e))
            return text
        return f'{text} {hint}' if hint else text

    def _operator(self, channel: str, user: str, thread_ts: str | None) -> tuple[str | None, str | None]:
        try:
            login = self.operators.login_for(user)
        except OperatorUnknown:
            log.warning('click from %s: operator lookup is down, ignoring the click', user)
            return None, 'operator-unknown'
        if login is None:
            return None, self._tell(channel, user, self._refused('slackevents.click.action_non_operator', user), thread_ts, 'not-operator')
        return login, None

    def _menu(self, payload: dict, menu: dict) -> str | None:
        kind, _, ask_id = str((menu.get('selected_option') or {}).get('value') or '').partition(':')
        if kind not in POST_ACTIONS or not ask_id:
            return None
        user = (payload.get('user') or {}).get('id') or ''
        channel = (payload.get('channel') or {}).get('id') or ''
        message = payload.get('message') or {}
        thread_ts = message.get('thread_ts') or message.get('ts')
        login, refused = self._operator(channel, user, thread_ts)
        if login is None:
            return refused
        found = self._ask(ask_id, 'cardAction', kind)
        if found is None:
            return self._tell(channel, user, msg('slackevents.click.gone'), thread_ts, 'gone')
        ask, call = found
        if kind != 'known':
            return self._card_action(channel, user, login, ask, call, message, thread_ts, swap=False)
        try:
            self.slack.call('views.open', {'trigger_id': payload.get('trigger_id'), 'view': known_view(ask['id'], channel, str(message.get('ts') or ''))})
        except SlackError as e:
            log.warning('the known noise window in %s was not opened: %s', channel, error_kind(e))
            return self._tell(channel, user, msg('slackevents.click.window_failed'), thread_ts, 'modal-failed')
        return 'confirm-open'

    def _submitted(self, payload: dict) -> str | None:
        view = payload.get('view') or {}
        if view.get('callback_id') != KNOWN_VIEW:
            return None
        try:
            meta = json.loads(view.get('private_metadata') or '{}')
        except ValueError:
            return None
        if not isinstance(meta, dict) or not meta.get('ask'):
            return None
        channel, ts = str(meta.get('channel') or ''), str(meta.get('ts') or '') or None
        user = (payload.get('user') or {}).get('id') or ''
        login, refused = self._operator(channel, user, ts)
        if login is None:
            return refused
        found = self._ask(str(meta['ask']), 'cardAction', 'known')
        if found is None:
            return self._tell(channel, user, msg('slackevents.click.gone'), ts, 'gone')
        ask, call = found
        return self._card_action(channel, user, login, ask, call, {'ts': ts}, ts, swap=False)

    def _card_action(self, channel: str, user: str, login: str, ask: dict, call: dict, message: dict, thread_ts: str | None, swap: bool = True) -> str:
        if call.get('action') not in CARD_ACTIONS:
            return self._tell(channel, user, msg('slackevents.click.gone'), thread_ts, 'gone')
        label = card_action_label(call)
        with messages.speaking(messages.ENGLISH):
            stored = card_action_label(call)
        if self.store.claim_card_action(ask['id'], login, stored, call) is None:
            try:
                claim = self.store.get_action(ask['id']).get('cardActionApplied') or {}
            except NotFound:
                return self._tell(channel, user, msg('slackevents.click.gone'), thread_ts, 'gone')
            applied = card_action_label(claim['call']) if isinstance(claim.get('call'), dict) else claim.get('label') or label
            text = msg('slackevents.click.applied', label=applied, login=claim.get('claimedBy')) if claim.get('at') else msg('slackevents.click.applying')
            return self._tell(channel, user, text, thread_ts, 'claimed')
        action = {'type': call['action'], 'key': call['key'], 'target': call['key'], 'label': f"{stored} from Slack: {call['key']}"[:LABEL_LIMIT],
                  'requestedBy': login, 'source': 'slack', 'askId': ask['id']} | ({'hours': call.get('hours')} if call['action'] == 'mute' else {}) | ({'reason': f'{stored} from Slack by {login}', PRESET: True} if call['action'] in ('mute', 'known') else {})
        if call['action'] == 'take' and call.get('owner'):
            action['owner'] = call['owner']
        if call['action'] == 'take' and call.get('ticket'):
            action['ticket'] = call['ticket']
        if call['action'] == 'resolve':
            action.update({f: call[f] for f in ('kind', 'ref') if call.get(f)})
        if call.get('reason'):
            action['reason'] = call['reason']
            action.pop(PRESET, None)
        try:
            result = self.apply(action)
        except TicketRefused as e:
            self.store.release_card_action(ask['id'])
            self.store.add_action(action, 'failed', e.text[:LABEL_LIMIT])
            return self._tell(channel, user, msg('slackevents.click.action_refused', reason=e.text[:REASON_LIMIT]), thread_ts, 'refused')
        except Exception as e:
            log.error('card action for ask %s failed: %s', ask['id'], error_kind(e))
            self.store.release_card_action(ask['id'])
            self.store.add_action(action, 'failed', error_kind(e)[:LABEL_LIMIT])
            return self._tell(channel, user, msg('slackevents.click.action_refused', reason=type(e).__name__), thread_ts, 'refused')
        self.store.add_action(action, 'done', result[:LABEL_LIMIT])
        self.store.card_action_applied(ask['id'])
        if not swap:
            return 'applied'
        line = msg('slackevents.click.applied_line', label=esc(label), login=esc(login))
        if card_buttons(message, ask['id']):
            line += ' · ' + esc(self._name(call['key']))
        self._swap_button(channel, message, CARD_ACTION, line, ask['id'])
        return 'applied'

    def _confirm_job(self, channel: str, user: str, login: str, run_id: str, message: dict, thread_ts: str | None) -> None:
        try:
            self._confirm(channel, user, login, run_id, message, thread_ts)
        except Exception as e:
            log.error('proposal of run %s: the Slack side of the confirm failed: %s', run_id, error_kind(e))

    def _confirm(self, channel: str, user: str, login: str, run_id: str, message: dict, thread_ts: str | None) -> str:
        try:
            result = self.confirm(run_id, login)
        except NoProposal:
            return self._tell(channel, user, msg('proposals.none'), thread_ts, 'gone')
        except (ProposalClosed, ProposalUnavailable) as e:
            return self._tell(channel, user, msg(f'proposals.{e.code}'), thread_ts, 'closed')
        except Exception as e:
            log.error('proposal of run %s not confirmed: %s', run_id, error_kind(e))
            return self._tell(channel, user, msg('proposals.failed'), thread_ts, 'refused')
        summary = msg('proposals.timed_out') if result.get('outcome') == 'timeout' else msg('proposals.failed') if result.get('outcome') == 'failed' else link_addresses(redact(esc(result['summary'])), self.hosts(), SECTION_LIMIT)
        try:
            self.slack.post({'channel': channel, 'thread_ts': thread_ts, 'text': summary})
        except SlackError as e:
            log.warning('proposal summary in %s failed: %s', channel, error_kind(e))
        title = (self.store.run_proposal(run_id) or {}).get('title') or ''
        self._swap_button(channel, message, PROPOSAL_CONFIRM, msg('proposals.confirmed', title=redact(esc(title)), by=esc(login)), run_id)
        return 'confirmed' if result.get('ok') else 'timeout' if result.get('outcome') == 'timeout' else 'failed'

    def _ask(self, action_id: str, field: str, kind: str | None = None) -> tuple[dict, dict] | None:
        try:
            ask = self.store.get_action(action_id)
            if ask.get('source') != 'slack':
                return None
            proposed = (ask.get('cardActions') or {}).get(kind) if kind else ask.get(field)
            if not proposed:
                return None
            self.store.anomaly(proposed['key'])
            return ask, proposed
        except NotFound:
            return None

    def _name(self, key: str) -> str:
        try:
            return card_name(self.store.anomaly(key))
        except NotFound:
            return key

    def _swap_button(self, channel: str, message: dict, action_id: str, line: str, value: str | None = None) -> None:
        note = {'type': 'context', 'elements': [{'type': 'mrkdwn', 'text': line}]}
        try:
            if message.get('attachments') and not message.get('blocks'):
                kept = [{'color': colour(a.get('color')), 'blocks': without_buttons(a.get('blocks'), action_id, value)} for a in message['attachments']]
                kept[-1]['blocks'].append(note)
                self.slack.update(channel, message.get('ts'), message.get('text') or '', attachments=kept)
            else:
                self.slack.update(channel, message.get('ts'), message.get('text') or '', without_buttons(message.get('blocks'), action_id, value) + [note])
        except SlackError as e:
            log.warning('button swap in %s failed: %s', channel, error_kind(e))

    def _tell(self, channel: str, user: str, text: str, thread_ts: str | None, decision: str) -> str:
        try:
            self.slack.ephemeral(channel, user, text, thread_ts)
        except SlackError as e:
            log.warning('ephemeral line in %s failed: %s', channel, error_kind(e))
        return decision


class SelfHealing(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if record.levelno >= logging.ERROR and str(record.msg).startswith(SELF_HEALING):
            record.levelno, record.levelname = logging.INFO, 'INFO'
        return True


def quiet_self_healing() -> None:
    sdk = logging.getLogger(SDK_LOGGER)
    if not any(isinstance(f, SelfHealing) for f in sdk.filters):
        sdk.addFilter(SelfHealing())


def socket_client(app_token: str):
    quiet_self_healing()
    return SocketModeClient(app_token=app_token)


class Listener:
    def __init__(self, app_token: str, intake, clicks, client_factory: Callable[[str], object], queue_size: int = QUEUE_SIZE, retry_s: float = CONNECT_RETRY_S,
                 clock: Callable[[], float] = time.time):
        self.app_token = app_token
        self.intake = intake
        self.clicks = clicks
        self.client_factory = client_factory
        self.queue: queue.Queue = queue.Queue(maxsize=queue_size)
        self.click_queue: queue.Queue = queue.Queue(maxsize=queue_size)
        self.retry_s = retry_s
        self.clock = clock
        self.client = None
        self.down_since: float | None = None
        self.alerted_at: float | None = None
        self.resets = 0
        self._stop = threading.Event()

    def start(self) -> None:
        for name, target in (('slack-intake', lambda: self._work(self.queue)), ('slack-clicks', lambda: self._work(self.click_queue)), ('slack-connect', self._connect),
                             ('slack-watch', self._watch)):
            threading.Thread(target=target, name=name, daemon=True).start()

    def connected(self) -> bool:
        try:
            return bool(self.client is not None and self.client.is_connected())
        except Exception:
            return False

    def check(self) -> None:
        now = self.clock()
        if self.connected():
            if self.down_since is not None and self.alerted_at is not None:
                log.warning('slack socket is back after %.0f s down', now - self.down_since)
            self.down_since = self.alerted_at = None
            return
        if self.down_since is None:
            self.down_since = now
            self.resets += 1
            return
        down = now - self.down_since
        if down >= DOWN_ALERT_S and (self.alerted_at is None or now - self.alerted_at >= DOWN_REPEAT_S):
            self.alerted_at = now
            log.error('slack socket has been down for %.0f s: Slack asks, button clicks and thread replies are not reaching this pod', down)

    def status(self) -> dict:
        return {'connected': self.connected(), 'downSince': self.down_since, 'drops': self.resets}

    def _watch(self) -> None:
        while not self._stop.wait(LISTENER_WAIT_S):
            try:
                self.check()
            except Exception as e:
                log.warning('slack socket check failed: %s', error_kind(e))

    def _connect(self) -> None:
        while not self._stop.is_set():
            client = None
            try:
                client = self.client_factory(self.app_token)
                self.client = client
                client.socket_mode_request_listeners.append(self.on_request)
                client.connect()
                if self._stop.is_set():
                    self._close(client)
                    self.client = None
                    return
                log.info('slack socket connected')
                return
            except Exception as e:
                self.client = None
                log.warning('slack socket did not connect, retrying in %.0f s: %s', self.retry_s, error_kind(e))
                if client is not None:
                    self._close(client)
                self._stop.wait(self.retry_s)

    def on_request(self, client, req) -> None:
        client.send_socket_mode_response(SocketModeResponse(envelope_id=req.envelope_id))
        if req.type not in ('events_api', 'interactive'):
            return
        try:
            (self.click_queue if req.type == 'interactive' else self.queue).put_nowait((req.type, req.payload))
        except queue.Full:
            log.warning('slack queue full, dropped a %s envelope', req.type)

    def process(self, item: tuple[str, dict]) -> str | None:
        kind, payload = item
        if kind == 'events_api':
            return self.intake.handle((payload or {}).get('event') or {})
        return self.clicks.handle(payload or {})

    def _work(self, source: queue.Queue) -> None:
        while not self._stop.is_set():
            try:
                item = source.get(timeout=1)
            except queue.Empty:
                continue
            try:
                self.process(item)
            except Exception as e:
                log.error('slack envelope failed: %s', error_kind(e))

    def stop(self) -> None:
        self._stop.set()
        if self.client is not None:
            self._close(self.client)

    def _close(self, client) -> None:
        try:
            client.close()
        except Exception as e:
            log.warning('slack socket close failed: %s', error_kind(e))
