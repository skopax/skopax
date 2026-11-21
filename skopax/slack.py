import datetime as dt
import hashlib
import json
import logging
import re
import time
import urllib.error
import urllib.request
from typing import Callable
from urllib.parse import quote, urlencode, urlsplit

from skopax import brand, causes, clock
from skopax.cardlink import card_url
from skopax.errkind import Authored, kind as error_kind
from skopax.actors import BOOTSTRAP_TOKEN, POD_BY, SYSTEM, investigator_label, is_token, token_label
from skopax.messages import fold, msg
from skopax.actions import DEFAULT_MUTE_HOURS
from skopax.config import RELEASE_NAME, RELEASE_PLACEHOLDER, release_problem
from skopax.postdeploy import SERVER_BY
from skopax.redact import redact, redact_lines
from skopax import drafts, history, information, tags
from skopax.kit.text import duration

log = logging.getLogger('skopax.slack')

POST_URL = 'https://slack.com/api/chat.postMessage'
API_URL = 'https://slack.com/api/'
READ_METHODS = frozenset({'auth.test', 'chat.getPermalink', 'conversations.history', 'conversations.info', 'conversations.replies', 'users.info', 'users.list', 'users.lookupByEmail'})
FORM_METHODS = frozenset({'files.getUploadURLExternal', 'files.completeUploadExternal'})
SCOPES_KEY = '_scopes'
CALL_TIMEOUT_S = 10
ACCESS_SIG = 'no-access'
CHANNEL_SCOPES = 'channels:read or groups:read'
SCOPES_TTL_S = 3600
UPLOAD_TIMEOUT_S = 30
FILE_HOST = 'files.slack.com'
IMAGE_BYTES_MAX = 3_750_000
IMAGE_MIMES = ('image/png', 'image/jpeg', 'image/gif', 'image/webp')
REPLIES_PAGE = 200
MEMBERS_PAGE = 200
MEMBERS_PAGES_MAX = 100
RATE_LIMITED = 'ratelimited'
RATE_TRIES = 3
RATE_WAIT_MAX_S = 30.0
SLACKBOT = 'USLACKBOT'
REACTIONS_SCOPE = 'reactions:write'
CREATE_TASK = 'create_task'
CARD_ACTION = 'card_action'
PROPOSAL_CONFIRM = 'proposal_confirm'
BUTTON_LIMIT = 75
POST_ACTIONS = ('take', 'resolve', 'known')
SLACK_MARKS = str.maketrans({'*': '\u2217', '_': '\u02cd', '~': '\u223c', '`': '\u02cb'})
DRAFT_LINES = 3
EMOJI = {'crit': ':red_circle:', 'warn': ':large_orange_circle:', 'info': ':large_blue_circle:', 'ok': ':large_green_circle:'}
SEVS = ('crit', 'warn', 'info')
LEVELS = ('ok',) + SEVS
COLOR = {'crit': '#c4231a', 'warn': '#e08a00', 'info': '#2f6fe0', 'cleared': '#0b7287', 'ok': '#1f8a4c'}
MAX_LINK_BUTTONS = 2
MAX_POST_LINKS = 5
MENU_LIMIT = 5
LINK_URL_LIMIT = 1000
BLOCK_LIMIT = 50
FIX_NOTE_LIMIT = 400
FIX_DIFF_LIMIT = 2600
FENCE = '`' * 3
ENTITY_CUT = re.compile(r'&[a-z]{0,3}…$')
CARD_MENU = 'card_menu'
KNOWN_VIEW = 'known_confirm'
VIEW_TEXT_LIMIT = 24
DONE = ':white_check_mark:'
SHORT_LIMIT = 160
MAX_REPORT_ROWS = 8
MAX_REPORT_POINTS = 6
MAX_REPORT_TICKETS = 12
NOTE_LIMIT = 90
SECTION_LIMIT = 2900
SHOTS_SHOWN = 3
SEMI = re.compile(r';(?![^()]*\))')
NOTE_LEAD = re.compile(r'^(\S+)(?: \S+)? =(?:\s+|$)')
RELEASE_GROUPS = {'project': r'(?P<project>[A-Z][A-Z0-9]+)', 'dd': r'(?P<dd>\d\d)', 'mm': r'(?P<mm>\d\d)', 'yyyy': r'\d{4}'}
RELEASE_GAP_MAX = 3
USER_MENTION = re.compile(r'&lt;(@[UW][A-Z0-9]{2,})&gt;')
FLAG_SIG = {'unavail': 'unavailable'}
AUTO_BY = 'auto · Claude draft'
CHANNEL_NOT_FOUND = 'channel_not_found'
CHANNEL_ID = re.compile(r'[CG][A-Z0-9]{8,12}')
SCOPE_ERRORS = ('missing_scope', 'not_allowed_token_type')
SCOPE_NAME = re.compile(r'[a-z][a-z0-9.:_,-]{0,200}')
MACHINE_BY = frozenset({SYSTEM, SERVER_BY, 'pod', 'auto', AUTO_BY, ''})
HEAD_KINDS = ('took', 'resolved', 'cleared', 'reopened', 'known', 'muted', 'unassigned')
OPEN_MARK = ':eyes:'


class SlackError(Authored, Exception):
    def __init__(self, message: str = '', code: str | None = None, needed: str | None = None):
        super().__init__(message)
        self.code = code
        self.needed = needed


def reason_of(e: BaseException) -> str:
    code = getattr(e, 'code', None) if isinstance(e, SlackError) else None
    return code if isinstance(code, str) and code else error_kind(e)


class SlackDown(SlackError):
    pass


class NoThread(Authored, Exception):
    pass


def scope_needed(body: dict) -> str | None:
    needed = str(body.get('needed') or '')
    return needed if SCOPE_NAME.fullmatch(needed) else None


def refusal_of(body: dict) -> str:
    code = str(body.get('error') or 'unknown error')
    if code == 'missing_scope':
        return f"Slack refused: the bot token lacks the scope {scope_needed(body) or 'this call needs'}"
    if code == 'not_allowed_token_type':
        return 'Slack refused: this call needs a bot token with more scopes (not_allowed_token_type)'
    return code


class SlackSlowed(SlackDown):
    def __init__(self, wait: float):
        super().__init__('Slack asked to slow down', code=RATE_LIMITED)
        self.wait = wait


def slowed(after) -> SlackSlowed:
    try:
        return SlackSlowed(min(max(float(after), 1.0), RATE_WAIT_MAX_S))
    except (TypeError, ValueError):
        return SlackSlowed(1.0)


def _answer(req: urllib.request.Request) -> dict:
    try:
        with urllib.request.urlopen(req, timeout=CALL_TIMEOUT_S) as resp:
            body = json.loads(resp.read())
            scopes = (getattr(resp, 'headers', None) or {}).get('x-oauth-scopes')
    except urllib.error.HTTPError as e:
        if e.code == 429:
            raise slowed(e.headers.get('Retry-After') if e.headers else None) from None
        raise SlackDown(f'Slack did not answer ({error_kind(e)})') from e
    except Exception as e:
        raise SlackDown(f'Slack did not answer ({error_kind(e)})') from e
    if not body.get('ok'):
        raise SlackError(refusal_of(body), code=str(body.get('error') or 'unknown_error'), needed=scope_needed(body))
    if scopes is not None:
        body[SCOPES_KEY] = scopes
    return body


def member(user: dict) -> dict | None:
    if not isinstance(user, dict) or user.get('deleted') or user.get('is_bot') or user.get('is_app_user') or user.get('is_restricted') or user.get('is_ultra_restricted') or user.get('id') == SLACKBOT:
        return None
    profile = user.get('profile') or {}
    email = str(profile.get('email') or '').strip().lower()
    if not email:
        return None
    display = str(profile.get('display_name') or '').strip()
    name = str(profile.get('real_name') or user.get('real_name') or display or user.get('name') or '').strip()
    return {'email': email, 'name': name or email.split('@')[0], 'display': display}


def send(token: str, payload: dict) -> dict:
    return _answer(urllib.request.Request(POST_URL, data=json.dumps(payload).encode(), headers={'Authorization': f'Bearer {token}', 'Content-Type': 'application/json; charset=utf-8'}))


def api(token: str, method: str, payload: dict) -> dict:
    headers = {'Authorization': f'Bearer {token}'}
    if method in READ_METHODS:
        return _answer(urllib.request.Request(f'{API_URL}{method}?{urlencode(payload)}', headers=headers))
    if method in FORM_METHODS:
        return _answer(urllib.request.Request(f'{API_URL}{method}', data=urlencode(payload).encode(), headers=headers | {'Content-Type': 'application/x-www-form-urlencoded'}))
    return _answer(urllib.request.Request(f'{API_URL}{method}', data=json.dumps(payload).encode(), headers=headers | {'Content-Type': 'application/json; charset=utf-8'}))


def scopes_of(body: dict) -> frozenset[str]:
    return frozenset(s.strip() for s in str(body.get(SCOPES_KEY) or '').split(',') if s.strip())


def name_words(name: str) -> list[str]:
    return [w for w in re.split(r'[^a-z0-9]+', fold(str(name or ''))) if w]


def same_person(asked: str, slack_name: str) -> bool:
    wanted, found = name_words(asked), set(name_words(slack_name))
    return len(wanted) >= 2 and all(w in found for w in wanted)


def upload(url: str, data: bytes) -> None:
    req = urllib.request.Request(url, data=data, headers={'Content-Type': 'application/octet-stream'}, method='POST')
    try:
        with urllib.request.urlopen(req, timeout=UPLOAD_TIMEOUT_S) as resp:
            resp.read()
    except Exception as e:
        raise SlackError(f'the file upload failed ({error_kind(e)})') from e


def download(token: str, url: str, limit: int) -> tuple[bytes, str]:
    req = urllib.request.Request(url, headers={'Authorization': f'Bearer {token}'})
    try:
        with urllib.request.urlopen(req, timeout=UPLOAD_TIMEOUT_S) as resp:
            return resp.read(limit + 1), str((getattr(resp, 'headers', None) or {}).get('content-type') or '')
    except Exception as e:
        raise SlackError(f'the file download failed ({type(e).__name__})') from None


class Slack:
    def __init__(self, token: str | Callable[[], str], channel: str | Callable[[], str], transport: Callable[[str, dict], dict] = send, api: Callable[[str, str, dict], dict] = api,
                 upload: Callable[[str, bytes], None] = upload, clock: Callable[[], float] = time.monotonic, sleep: Callable[[float], None] = time.sleep,
                 fetch: Callable[[str, str, int], tuple[bytes, str]] = download, known_channel: Callable[[], str | None] = lambda: None,
                 attribution: Callable[[], dict | None] = lambda: None):
        self._token = token
        self.attribution = attribution
        self._token_for: str | None = None
        self._channel = channel
        self._for: str | None = None
        self.known_channel = known_channel
        self._channel_id: str | None = None
        self._name_gone = False
        self.transport = transport
        self.api = api
        self.upload = upload
        self.fetch = fetch
        self.clock = clock
        self.sleep = sleep
        self._bot_user: str | None = None
        self._scopes: tuple[float, frozenset[str]] | None = None
        self._workspace: str | None = None
        self._channel_names: dict[str, str | None] = {}

    def _renew(self) -> str:
        now = self._token() if callable(self._token) else self._token
        if now != self._token_for:
            self._token_for, self._bot_user, self._scopes, self._workspace, self._channel_names = now, None, None, None, {}
            self._channel_id, self._name_gone = None, False
        return now

    @property
    def token(self) -> str:
        return self._renew()

    @property
    def channel(self) -> str:
        self._renew()
        now = self._channel() if callable(self._channel) else self._channel
        if now != self._for:
            self._for, self._channel_id, self._name_gone = now, None, False
        return now

    @property
    def enabled(self) -> bool:
        return bool(self.token and self.channel)

    def check_channel(self, channel: str) -> str | None:
        if not CHANNEL_ID.fullmatch(channel or ''):
            return 'give the channel id, like C0123ABCD (Slack › the channel › About, at the bottom)'
        if not self.token:
            return None
        try:
            info = self.call('conversations.info', {'channel': channel}).get('channel') or {}
        except SlackDown:
            return 'Slack did not answer, try again'
        except SlackError as e:
            return e.text if e.code in SCOPE_ERRORS else f'Slack does not know that channel ({e.text})'
        if info.get('is_archived'):
            return f"#{info.get('name') or channel} is archived"
        if info.get('is_member') is False:
            return msg('settings.slack_bot_missing', channel=info.get('name') or channel)
        return None

    def signature(self) -> dict | None:
        try:
            line = self.attribution()
        except Exception as e:
            log.warning('the powered-by line could not be read, so the post goes out without it: %s', error_kind(e))
            return None
        return line if isinstance(line, dict) and line.get('text') and line.get('href') else None

    def post(self, message: dict, sign: bool = True) -> dict:
        payload = {'channel': self.channel, **{k: v for k, v in message.items() if v is not None}, 'unfurl_links': False, 'unfurl_media': False}
        if sign and 'thread_ts' not in payload:
            payload = signed(payload, self.signature())
        if self._name_gone and self._channel_id and payload['channel'] == self.channel:
            payload['channel'] = self._channel_id
        try:
            body = self.transport(self.token, payload)
        except SlackError as e:
            fallback = self._channel_id or self.known_channel()
            if str(e) != CHANNEL_NOT_FOUND or payload['channel'] != self.channel or not fallback or fallback == self.channel:
                raise
            log.warning('Slack has no channel %s any more, posting to %s, the channel of the last post; set the channel id on Admin \u203a General \u203a Slack', self.channel, fallback)
            body = self.transport(self.token, payload | {'channel': fallback})
            self._name_gone = True
        if payload['channel'] in (self.channel, self._channel_id) and body.get('channel') and 'thread_ts' not in payload:
            self._channel_id = body['channel']
        return {'channel': body.get('channel'), 'ts': body.get('ts')}

    def call(self, method: str, payload: dict) -> dict:
        token = self.token
        body = self.api(token, method, {k: v for k, v in payload.items() if v is not None})
        if SCOPES_KEY in body and token == self._token_for:
            self._scopes = (self.clock(), scopes_of(body))
        return body

    def missing_scopes(self, listening: bool, dm_chats: bool) -> list[str]:
        if not self.scopes():
            return []
        missing, family = [], None
        try:
            info = self.call('conversations.info', {'channel': self._channel_id or self.channel}).get('channel') or {}
            family = 'groups' if info.get('is_private') else 'channels'
        except SlackDown:
            raise
        except SlackError as e:
            if e.code in SCOPE_ERRORS:
                missing.append(e.needed or CHANNEL_SCOPES)
        granted = self.scopes()
        needed = ([f'{family}:read'] + ([f'{family}:history'] if listening else []) if family else []) + (['im:history'] if listening and dm_chats else [])
        return missing + [s for s in needed if s not in granted]

    def update(self, channel: str, ts: str, text: str, blocks: list | None = None, attachments: list | None = None) -> dict:
        body = self.call('chat.update', {'channel': channel, 'ts': ts, 'text': text, 'blocks': blocks, 'attachments': attachments})
        return {'channel': body.get('channel'), 'ts': body.get('ts')}

    def delete(self, channel: str, ts: str) -> None:
        self.call('chat.delete', {'channel': channel, 'ts': ts})

    def ephemeral(self, channel: str, user: str, text: str, thread_ts: str | None = None) -> None:
        self.call('chat.postEphemeral', {'channel': channel, 'user': user, 'text': text, 'thread_ts': thread_ts})

    def react(self, channel: str, ts: str, name: str) -> bool:
        if REACTIONS_SCOPE not in self.scopes():
            return False
        try:
            self.call('reactions.add', {'channel': channel, 'timestamp': ts, 'name': name})
        except SlackError as e:
            if str(e) != 'already_reacted':
                raise
        return True

    def file(self, url: str) -> tuple[bytes, str]:
        parts = urlsplit(str(url or ''))
        if parts.scheme != 'https' or parts.hostname != FILE_HOST:
            raise SlackError('not a Slack file link')
        data, kind = self.fetch(self.token, url, IMAGE_BYTES_MAX)
        mime = kind.split(';')[0].strip().lower()
        if mime not in IMAGE_MIMES:
            raise SlackError('Slack did not return an image: the Slack app may lack the files:read scope')
        if len(data) > IMAGE_BYTES_MAX:
            raise SlackError(f'the image is larger than {IMAGE_BYTES_MAX // 1_000_000} MB')
        return data, mime

    def history(self, channel: str, latest: str, limit: int = 5) -> list[dict]:
        return self.call('conversations.history', {'channel': channel, 'latest': latest, 'limit': limit}).get('messages') or []

    def replies(self, channel: str, thread_ts: str, limit: int = 10) -> list[dict]:
        return (self.call('conversations.replies', {'channel': channel, 'ts': thread_ts, 'limit': REPLIES_PAGE}).get('messages') or [])[-limit:]

    def channel_name(self, channel: str) -> str | None:
        self._renew()
        if channel not in self._channel_names:
            try:
                self._channel_names[channel] = (self.call('conversations.info', {'channel': channel}).get('channel') or {}).get('name')
            except SlackError:
                return None
        return self._channel_names[channel]

    def permalink(self, channel: str, ts: str) -> str | None:
        try:
            return self.call('chat.getPermalink', {'channel': channel, 'message_ts': ts}).get('permalink')
        except SlackError:
            return None

    def workspace(self) -> str | None:
        self._renew()
        if self._workspace is None:
            try:
                self._auth()
            except SlackError:
                return None
        return self._workspace or None

    def linked(self, sent: dict, thread_ts: str | None = None) -> dict:
        channel, ts = sent.get('channel'), sent.get('ts')
        if not channel or not ts:
            return sent
        for find in (lambda: self.permalink(channel, ts), lambda: built_link(self.workspace(), channel, ts, thread_ts)):
            try:
                link = find()
            except Exception:
                link = None
            if link:
                return sent | {'permalink': link}
        return sent

    def user_by_email(self, email: str) -> dict | None:
        try:
            user = self.call('users.lookupByEmail', {'email': email}).get('user') or {}
        except SlackError as e:
            if str(e) == 'users_not_found':
                return None
            raise
        if user.get('is_bot') or user.get('deleted') or not user.get('id'):
            return None
        profile = user.get('profile') or {}
        return {'id': user['id'], 'name': str(profile.get('real_name') or user.get('real_name') or profile.get('display_name') or user.get('name') or '').strip()}

    def members(self) -> list[dict]:
        found, cursor = [], None
        for _ in range(MEMBERS_PAGES_MAX):
            body = self.page_of_members(cursor)
            found.extend(p for p in map(member, body.get('members') or []) if p)
            cursor = str((body.get('response_metadata') or {}).get('next_cursor') or '')
            if not cursor:
                break
        return found

    def page_of_members(self, cursor: str | None) -> dict:
        for attempt in range(RATE_TRIES):
            try:
                return self.call('users.list', {'limit': MEMBERS_PAGE, 'cursor': cursor})
            except SlackError as e:
                if e.code != RATE_LIMITED or attempt == RATE_TRIES - 1:
                    raise
                self.sleep(min(float(getattr(e, 'wait', 1.0) or 1.0), RATE_WAIT_MAX_S))
        raise slowed(None)

    def user_email(self, user_id: str) -> str | None:
        user = self.call('users.info', {'user': user_id}).get('user') or {}
        if user.get('is_bot') or user.get('deleted') or user.get('id') == SLACKBOT:
            return None
        return ((user.get('profile') or {}).get('email') or '').strip() or None

    def _auth(self) -> dict:
        token = self.token
        body = self.api(token, 'auth.test', {})
        if token == self._token_for:
            self._scopes = (self.clock(), scopes_of(body))
            if self._bot_user is None:
                self._bot_user = body.get('user_id') or ''
            self._workspace = str(body.get('url') or '')
        return body

    def bot_user(self) -> str:
        self._renew()
        if self._bot_user is None:
            found = self._auth().get('user_id') or ''
            return found if self._bot_user is None else self._bot_user
        return self._bot_user

    def scopes(self) -> frozenset[str]:
        self._renew()
        held = self._scopes
        if held is None or self.clock() - held[0] > SCOPES_TTL_S:
            return scopes_of(self._auth())
        return held[1]

    def attach(self, channel: str, thread_ts: str, files: list[tuple[str, bytes, str]]) -> list[str]:
        return self.upload_files(files, channel, thread_ts)

    def upload_files(self, files: list[tuple[str, bytes, str]], channel: str | None = None, thread_ts: str | None = None) -> list[str]:
        shared = []
        for name, data, title in files:
            body = self.call('files.getUploadURLExternal', {'filename': name, 'length': len(data)})
            self.upload(body['upload_url'], data)
            shared.append({'id': body['file_id'], 'title': title})
        self.call('files.completeUploadExternal', {'files': json.dumps(shared), 'channel_id': channel, 'thread_ts': thread_ts})
        return [f['id'] for f in shared]


def built_link(workspace: str | None, channel: str, ts: str, thread_ts: str | None = None) -> str | None:
    if not workspace or not workspace.startswith('https://') or not channel or not ts:
        return None
    link = f"{workspace.rstrip('/')}/archives/{quote(channel, safe='')}/p{ts.replace('.', '')}"
    if thread_ts and thread_ts != ts:
        link += '?' + urlencode({'thread_ts': thread_ts, 'cid': channel})
    return link


def esc(text) -> str:
    return str(text).replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')


def esc_keep_mentions(text) -> str:
    return USER_MENTION.sub(r'<\1>', esc(text))


def cut(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit - 1].rstrip() + '…'


def esc_cut(text, limit: int) -> str:
    return ENTITY_CUT.sub('…', cut(esc(text), limit))


def cut_words(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    head = text[:limit - 1]
    space = head.rfind(' ')
    if space >= limit // 2:
        head = head[:space]
    return head.rstrip(' ,;:(/-') + '…'


def row_note(x: dict) -> str:
    note = (x.get('note') or '').strip()
    found = NOTE_LEAD.match(note)
    if found and found[1] in {re.split(r'[\s=]', f)[0] for f in x.get('flags') or []}:
        note = note[found.end():]
    return cut_words(note, NOTE_LIMIT)


def num(v, unit: str | None) -> str:
    if v is None:
        return '–'
    if unit == 'dur':
        return duration(v)
    if unit == 'ms':
        return f'{v / 1000:.1f} s' if v >= 1000 else f'{round(v)} ms'
    if v == 0:
        return '0'
    return f'{round(v):,}' if abs(v) >= 1 else f'{v:.2f}'


def local_time(value: str | None) -> str:
    try:
        parsed = dt.datetime.fromisoformat(value)
        return f'{clock.date(parsed)} {clock.label(parsed)}'
    except (TypeError, ValueError):
        return ''


def local_hm(value: str | None) -> str:
    try:
        return clock.hm(dt.datetime.fromisoformat(value))
    except (TypeError, ValueError):
        return ''


def sender(by: str, draft_by: str | None = None) -> str:
    if by == AUTO_BY:
        if draft_by == SERVER_BY:
            return msg('slack.sender.server')
        label = token_label(draft_by, '') if is_token(draft_by) and draft_by != BOOTSTRAP_TOKEN else ''
        return msg('slack.sender.token', label=label) if label else msg('slack.sender.auto')
    if by == POD_BY:
        return investigator_label()
    return token_label(by) if is_token(by) else by.split('@')[0]


CONFIRM_LIMIT = 300


def sev_label(sev: str, other: str) -> str:
    return msg(f'slack.sev.{sev}') if sev in SEVS else other


def level_label(level) -> str:
    return msg(f'slack.level.{level}') if level in LEVELS else str(level).upper()


def known_confirm() -> dict:
    return {'title': {'type': 'plain_text', 'text': msg('slack.known.title')},
            'text': {'type': 'mrkdwn', 'text': msg('slack.known.confirm')},
            'confirm': {'type': 'plain_text', 'text': msg('slack.known.ok')}, 'deny': {'type': 'plain_text', 'text': msg('slack.cancel')}, 'style': 'danger'}


def card_button(action_id: str, value: str, label: str, kind: str, primary: bool = False) -> dict:
    out = {'type': 'button', 'action_id': action_id, 'value': value, 'text': {'type': 'plain_text', 'text': label}}
    if primary:
        out['style'] = 'primary'
    if kind == 'known':
        out['confirm'] = known_confirm()
    return out


def button(text: str, url: str, primary: bool = False) -> dict:
    out = {'type': 'button', 'text': {'type': 'plain_text', 'text': cut(text, 75)}, 'url': url}
    if primary:
        out['style'] = 'primary'
    return out


def recurring_line(a: dict) -> str:
    if not a.get('recurring') or not a.get('recurringCount'):
        return ''
    return msg('slack.recurring', count=a['recurringCount'], days=a.get('recurringDays') or 7)


def plain(text: str, limit: int = 75) -> dict:
    return {'type': 'plain_text', 'text': cut(text, limit)}


def actor(by) -> str:
    text = str(by or '').strip()
    if text == POD_BY:
        return investigator_label()
    if is_token(text):
        return token_label(text)
    if text in MACHINE_BY:
        return brand.product()
    return text.split('@')[0] if '@' in text else text


def opening(by: str) -> dict:
    return {'kind': 'opened' if str(by or '').strip() in MACHINE_BY else 'shared', 'by': by}


def card_ref(a: dict) -> str:
    return f"#{a['num']}" if a.get('num') else f"{a.get('svc') or '-'} / {a.get('label') or '?'}"


def head_text(a: dict, head: dict, mute: str | None = None) -> str:
    sev = a.get('sev', 'info')
    kind = head.get('kind')
    who, ref, level = esc(actor(head.get('by'))), esc(card_ref(a)), sev_label(sev, esc(sev))
    if kind == 'shared':
        text = msg('slack.head.shared', who=who, sev=level)
    elif kind == 'assigned':
        text = msg('slack.head.assigned', who=who, card=ref, name=esc(actor(head.get('name'))))
    elif kind in ('auto', 'auto_first'):
        text = msg(f'slack.head.{kind}', card=ref, span=esc(str(head.get('span') or '')))
    elif kind in HEAD_KINDS:
        text = msg(f'slack.head.{kind}', who=who, card=ref)
    else:
        text = msg('slack.head.opened', sev=level)
    if a.get('triage') == 'handled' or mute == 'known':
        mark = DONE
    elif a.get('status') == 'cleared':
        mark = OPEN_MARK
    else:
        mark = EMOJI.get(sev, ':white_circle:')
    return f"{mark} {text}"


def status_of(a: dict, mute: str | None) -> str:
    if mute == 'known':
        return 'known'
    if mute:
        return 'muted'
    if a.get('triage') == 'handled':
        return 'handled'
    if a.get('status') == 'cleared':
        return 'cleared'
    return 'progress' if a.get('triage') == 'progress' else 'open'


def menu_kinds(a: dict, mute: str | None) -> tuple[str, ...]:
    if mute == 'known' or a.get('triage') == 'handled':
        return ()
    return tuple(k for k in POST_ACTIONS if not (k == 'take' and a.get('triage') == 'progress'))


def card_menu(buttons: str | None, kinds: tuple[str, ...], chat: str | None) -> dict | None:
    options = [{'text': plain(msg(f'slack.label.{k}')), 'value': f'{k}:{buttons}'} for k in POST_ACTIONS if k in kinds] if buttons else []
    if chat:
        options.append({'text': plain(msg('slack.menu.chat')), 'value': 'chat', 'url': chat})
    return {'type': 'overflow', 'action_id': CARD_MENU, 'options': options[:MENU_LIMIT]} if options else None


def parents(parent: dict | None) -> dict:
    return {parent['key']: parent} if isinstance(parent, dict) and parent.get('key') else {}


def facts_line(a: dict, mute: str | None, drafted_by: str = '', parent: dict | None = None) -> str:
    sev = a.get('sev', 'info')
    status = status_of(a, mute)
    parts = [msg('slack.status.event' if status == 'cleared' and causes.event(a, parents(parent)) else f'slack.status.{status}'), sev_label(sev, esc(sev))]
    if a.get('owner'):
        parts.append(msg('slack.bar.assignee', name=esc(actor(a['owner']))))
    named = [tags.label(t) for t in tags.ids(a) if t in tags.all_tags()]
    if named:
        parts.append(msg('slack.facts.tags', names=esc(', '.join(named))))
    since = local_time(a.get('firstSeen'))
    if since:
        parts.append(msg('slack.bar.since', at=since))
    if drafted_by:
        parts.append(esc(drafted_by))
    return ' \u00b7 '.join(parts)


def numbers_line(a: dict) -> str:
    if a.get('sig') == ACCESS_SIG:
        return ''
    unit = a.get('unit')
    parts = [msg('slack.facts.now', value=num(a.get('now'), unit))] + [msg(f'slack.facts.{k}', value=num(a.get(k), unit)) for k in ('d1', 'd7') if a.get(k) is not None]
    if a.get('recurring') and a.get('recurringCount'):
        parts.append(msg('slack.facts.recurring', count=a['recurringCount'], days=a.get('recurringDays') or 7))
    return ' \u00b7 '.join(parts)


def safe_url(url) -> bool:
    text = str(url or '')
    return text.startswith('https://') and len(text) <= LINK_URL_LIMIT and text.isprintable() and not any(c in text for c in ' |<>')


def links_line(links) -> str:
    out, size = [], 0
    for item in links if isinstance(links, list) else []:
        label = str(item.get('label') or '').strip() if isinstance(item, dict) else ''
        if not label or not safe_url(item.get('url')):
            continue
        link = f"<{esc(item['url'])}|{esc(cut(label, 75))}>"
        if size + len(link) + 3 > SECTION_LIMIT:
            break
        out.append(link)
        size += len(link) + 3
        if len(out) == MAX_POST_LINKS:
            break
    return ' \u00b7 '.join(out)


def shot_links(shots, base: str) -> str:
    found = [s for s in shots or [] if isinstance(s, dict) and s.get('id')][:SHOTS_SHOWN]
    return ' \u00b7 '.join(f"<{base.rstrip('/')}/api/screenshots/{quote(str(s['id']), safe='')}|{esc(msg('slack.screenshot.link', viewport=s.get('viewport') or 'desktop'))}>" for s in found)


def attribution_mark(line: dict) -> str:
    return f"<{line['href']}|{esc(line['text'])}>"


def anomaly_message(a: dict, link: str, by: str, draft: str | None = None, buttons: str | None = None, chat: str | None = None, *, head: dict | None = None,
                    attribution: dict | None = None, mute: str | None = None, kinds: tuple[str, ...] = POST_ACTIONS, shots: list | None = None, base: str = '',
                    parent: dict | None = None) -> dict:
    sev = a.get('sev', 'info')
    cleared = a.get('status') == 'cleared'
    top = {'type': 'section', 'text': {'type': 'mrkdwn', 'text': head_text(a, head or opening(by), mute)}}
    menu = card_menu(buttons, kinds, chat)
    if menu:
        top['accessory'] = menu
    title = (f"#{a['num']} " if a.get('num') else '') + f"{a.get('svc', '?')} / {a.get('label', '?')}" + (f" \u00b7 {a['sub']}" if a.get('sub') else '')
    blocks = [top, {'type': 'section', 'text': {'type': 'mrkdwn', 'text': f'*<{link}|{esc(title)}>*'}}]
    summary = str((a.get('slackDraft') or {}).get('headline') or '').strip() if draft else ''
    if draft:
        blocks.append({'type': 'section', 'text': {'type': 'mrkdwn', 'text': (f'*{esc(summary)}*\n' if summary else '') + esc_keep_mentions(draft)}})
    if a.get('msg'):
        blocks.append({'type': 'context', 'elements': [{'type': 'mrkdwn', 'text': f"`{esc(cut(a['msg'], SHORT_LIMIT).replace('`', "'"))}`"}]})
    drafted_by = sender(by, (a.get('slackDraft') or {}).get('by')) if draft and by == AUTO_BY else ''
    bar = [facts_line(a, mute, drafted_by, parent), numbers_line(a), links_line(a.get('links')), shot_links(shots, base) if base else ''] + ([attribution_mark(attribution)] if attribution else [])
    blocks.append({'type': 'context', 'elements': [{'type': 'mrkdwn', 'text': t} for t in bar if t]})
    fallback = f"{(msg('slack.status.event') if causes.event(a, parents(parent)) else msg('slack.cleared')) if cleared else sev_label(sev, sev)} \u00b7 {esc(a.get('svc', '?'))} / {esc(a.get('label', '?'))}" + (f" \u00b7 {esc(a['sub'])}" if a.get('sub') else '')
    return {'text': (f'{esc(summary)}\n' if summary else '') + esc_keep_mentions(draft) if draft else fallback, 'blocks': blocks}


def known_view(ask_id: str, channel: str, ts: str) -> dict:
    return {'type': 'modal', 'callback_id': KNOWN_VIEW, 'private_metadata': json.dumps({'ask': ask_id, 'channel': channel, 'ts': ts}),
            'title': plain(msg('slack.known.title'), VIEW_TEXT_LIMIT), 'submit': plain(msg('slack.known.ok'), VIEW_TEXT_LIMIT), 'close': plain(msg('slack.cancel'), VIEW_TEXT_LIMIT),
            'blocks': [{'type': 'section', 'text': {'type': 'mrkdwn', 'text': msg('slack.known.confirm')}}]}


def carries(blocks: list, mark: str) -> bool:
    return any(e.get('text') == mark for b in blocks if isinstance(b, dict) and b.get('type') == 'context' for e in b.get('elements') or [] if isinstance(e, dict))


def text_blocks(text: str) -> list[dict]:
    packed: list[str] = []
    for line in text.split('\n'):
        for piece in [line[i:i + SECTION_LIMIT] for i in range(0, len(line), SECTION_LIMIT)] or ['']:
            if packed and len(packed[-1]) + 1 + len(piece) <= SECTION_LIMIT:
                packed[-1] += '\n' + piece
            else:
                packed.append(piece)
    return [{'type': 'section', 'text': {'type': 'mrkdwn', 'text': t}} for t in packed if t.strip()]


def signed(message: dict, line: dict | None) -> dict:
    if not line or message.get('thread_ts'):
        return message
    mark = attribution_mark(line)
    note = {'type': 'context', 'elements': [{'type': 'mrkdwn', 'text': mark}]}
    if message.get('blocks'):
        blocks = list(message['blocks'])
        return message if carries(blocks, mark) or len(blocks) >= BLOCK_LIMIT else message | {'blocks': blocks + [note]}
    if message.get('attachments'):
        *rest, last = message['attachments']
        inner = list(last.get('blocks') or []) if isinstance(last, dict) else []
        if not inner or carries(inner, mark) or len(inner) >= BLOCK_LIMIT:
            return message
        return message | {'attachments': rest + [last | {'blocks': inner + [note]}]}
    sections = text_blocks(str(message.get('text') or ''))
    return message | {'blocks': sections + [note]} if sections and len(sections) < BLOCK_LIMIT else message


def fix_hash(ts: str, c: dict) -> str:
    return hashlib.sha1('|'.join(str(x) for x in (ts, c.get('repo'), c.get('path'), c.get('lines'), c.get('suggestion'))).encode()).hexdigest()[:16]


def fix_message(fixes: list[dict], link: str) -> dict:
    blocks = []
    for c in fixes:
        where = f"{c.get('repo') or ''} · {c.get('path') or ''}:{c.get('lines') or ''}"
        head = msg('slack.fix.head') + (f"<{c['url']}|{esc(where)}>" if str(c.get('url', '')).startswith('https://') else esc(where)) + (f" @ `{esc(c['sha'])}`" if c.get('sha') else '')
        blocks.append({'type': 'section', 'text': {'type': 'mrkdwn', 'text': head}})
        if c.get('note'):
            blocks.append({'type': 'context', 'elements': [{'type': 'mrkdwn', 'text': esc(cut(c['note'], FIX_NOTE_LIMIT))}]})
        diff = '\n'.join(['- ' + line for line in str(c.get('current') or '').splitlines()] + ['+ ' + line for line in str(c.get('suggestion') or '').splitlines()])
        blocks.append({'type': 'section', 'text': {'type': 'mrkdwn', 'text': FENCE + esc_cut(diff.replace(FENCE, "'''"), FIX_DIFF_LIMIT) + FENCE}})
    blocks.append({'type': 'actions', 'elements': [button(msg('slack.open'), link)]})
    return {'text': msg('slack.fix.text') + ', '.join(f"{c.get('path')}:{c.get('lines')}" for c in fixes), 'blocks': blocks}


def release_pattern(template: str) -> re.Pattern:
    out, at = [], 0
    for m in RELEASE_PLACEHOLDER.finditer(template):
        out += [re.escape(template[at:m.start()]), RELEASE_GROUPS.get(m.group(1), re.escape(m.group(0)))]
        at = m.end()
    return re.compile('^' + ''.join(out) + re.escape(template[at:]) + '$')


def release_day(template: str, m: re.Match) -> str:
    spots = {p.group(1): p for p in RELEASE_PLACEHOLDER.finditer(template) if p.group(1) in ('dd', 'mm')}
    first, second = sorted(spots.values(), key=lambda p: p.start())
    gap = template[first.end():second.start()]
    joint = gap if 0 < len(gap) <= RELEASE_GAP_MAX and not any(c.isalnum() or c in '{}' for c in gap) else '.'
    return m[first.group(1)] + joint + m[second.group(1)]


def report_release(r: dict, template: str = RELEASE_NAME) -> str:
    releases = r.get('releases') or []
    if not releases or not any(i.get('inRelease') for i in r.get('issues') or []):
        return ''
    found = [release_pattern(template).match(v) for v in releases] if release_problem(template) is None else [None]
    if all(found) and len({(m['dd'], m['mm']) for m in found}) == 1:
        prefixes = list(dict.fromkeys(m['project'] for m in found))
        return f"{' + '.join(prefixes)} {release_day(template, found[0])} release"
    return ' + '.join(releases)


def when(value) -> float | None:
    try:
        return dt.datetime.fromisoformat(str(value)).timestamp()
    except (TypeError, ValueError):
        return None


def flag_sig(flag: str) -> str:
    first = re.split(r'[\s=]', flag)[0]
    return FLAG_SIG.get(first, first)


def row_time(x: dict, r: dict) -> float:
    return when(x.get('at')) or when(r.get('windowFrom')) or when(r.get('createdAt')) or dt.datetime.now(dt.timezone.utc).timestamp()


def flag_card(x: dict, r: dict, sig: str, anomalies: list[dict]) -> dict | None:
    at = row_time(x, r)
    end = max(when(r.get('windowTo') or r.get('createdAt')) or 0, at) + 3 * 3600
    near = [a for a in anomalies if (when(a.get('lastSeen') or a.get('firstSeen')) or 0) >= at - 3600 and (when(a.get('firstSeen')) or float('inf')) <= end]
    key, cluster = f"{sig}|{x.get('ns')}/{x.get('svc')}", x.get('cluster') or ''
    tail = f'@{cluster}' if cluster else ''
    own = [a for a in near if (a.get('cluster') or '') == cluster]
    hit = next((a for a in own if a.get('key') == key + tail or str(a.get('key')).startswith(key + '|')), None)
    if hit or sig not in ('err', 'warn'):
        return hit
    lead = f"log|{x.get('svc')}|{'ERROR' if sig == 'err' else 'WARN'}|"
    return next((a for a in near if str(a.get('key')).startswith(lead)), None)


def flag_links(x: dict, r: dict, page: str, anomalies: list[dict]) -> str:
    out = []
    for f in x.get('flags') or []:
        sig, text = flag_sig(f), esc(f.replace('=', ' '))
        card = flag_card(x, r, sig, anomalies)
        out.append(f"<{card_url(page, card['key'], card.get('num'))}|{text} \u203a>" if card else text)
    return ', '.join(out)


def shipped_tickets(r: dict) -> list[str]:
    deployed = list(dict.fromkeys(k for x in r.get('rows') or [] for k in x.get('issues') or []))
    known = [i['key'] for i in r.get('issues') or [] if i.get('key') in deployed]
    return known if r.get('issues') else deployed


def report_message(r: dict, link: str, by: str, draft: str | None = None, anomalies: list[dict] | None = None, jira_site: str = '', release_template: str = RELEASE_NAME) -> dict:
    rows = r.get('rows') or []
    flagged = [x for x in rows if x.get('flags')]
    brief = r.get('brief') or {}
    level = brief.get('level') or ('warn' if flagged else 'ok')
    release = report_release(r, release_template)
    state = msg('slack.report.flagged', n=len(flagged)) if flagged else msg('slack.report.clean')
    services = msg('slack.report.service' if len(rows) == 1 else 'slack.report.services', n=len(rows))
    name = f'*{esc(release)}*' if release else f"*{msg('slack.report.name')}* \u00b7 {esc(local_time(r.get('windowFrom') or r.get('createdAt')) or msg('slack.report.check'))}"
    label = level_label(level)
    headline = f"{EMOJI.get(level, ':white_circle:')} {name} · {esc(label)} · {services} · {state}"
    blocks = []
    if brief.get('headline'):
        parts = [p.strip() for p in SEMI.split(brief['headline']) if p.strip()]
        points = brief.get('points') or []
        lines = [f'*{esc(label)}* · *{esc(parts[0])}*'] + [esc(p) for p in parts[1:]] + [f'• {esc(cut_words(p, 200))}' for p in points[:MAX_REPORT_POINTS]]
        if len(points) > MAX_REPORT_POINTS:
            lines.append(f"_{msg('slack.report.more', n=len(points) - MAX_REPORT_POINTS)}_")
        blocks.append({'type': 'section', 'text': {'type': 'mrkdwn', 'text': cut('\n'.join(lines), SECTION_LIMIT)}})
    elif r.get('summary'):
        blocks.append({'type': 'section', 'text': {'type': 'mrkdwn', 'text': f'*{esc(label)}* · ' + esc(cut(r['summary'], SECTION_LIMIT - 40))}})
    if flagged:
        twice = {x.get('svc') for x in flagged if sum(y.get('svc') == x.get('svc') for y in flagged) > 1}
        page = link.split('#')[0]
        lines = []
        for x in flagged[:MAX_REPORT_ROWS]:
            where = f" ({esc(x['ns'])})" if x.get('svc') in twice and x.get('ns') else ''
            note = row_note(x)
            note = f' · {esc(note)}' if note else ''
            line = f"• *{esc(x.get('svc', '?'))}*{where} · {flag_links(x, r, page, information.cards_only(anomalies))}{note}"
            lines.append(line if len(line) < SECTION_LIMIT - 20 else f"• *{esc(x.get('svc', '?'))}*{where} · {esc(', '.join(f.replace('=', ' ') for f in x['flags']))}{note}")
        if len(flagged) > MAX_REPORT_ROWS:
            lines.append(f"_{msg('slack.report.more', n=len(flagged) - MAX_REPORT_ROWS)}_")
        packed = [f"*{msg('slack.report.flagged_head')}*"]
        for line in lines:
            if len(packed[-1]) + 1 + len(line) > SECTION_LIMIT:
                packed.append(line)
            else:
                packed[-1] += '\n' + line
        blocks += [{'type': 'section', 'text': {'type': 'mrkdwn', 'text': t}} for t in packed]
    tickets = shipped_tickets(r)
    if tickets:
        keys = ', '.join(f'<{jira_site}/browse/{k}|{k}>' if jira_site else k for k in tickets[:MAX_REPORT_TICKETS]) + (' ' + msg('slack.report.more_tickets', n=len(tickets) - MAX_REPORT_TICKETS) if len(tickets) > MAX_REPORT_TICKETS else '')
        count = msg('slack.report.ticket' if len(tickets) == 1 else 'slack.report.tickets', n=len(tickets))
        blocks.append({'type': 'section', 'text': {'type': 'mrkdwn', 'text': f"*{msg('slack.report.shipped')}* \u00b7 {count}\n{keys}"}})
    blocks.append({'type': 'actions', 'elements': [button(msg('slack.report.open'), link, primary=True)]})
    window = local_hm(r.get('windowFrom'))
    span = msg('slack.report.window', start=window, end=local_hm(r.get('windowTo')) or msg('slack.report.now'), zone=clock.label(dt.datetime.fromisoformat(r['windowFrom']))) + ' \u00b7 ' if window else ''
    blocks.append({'type': 'context', 'elements': [{'type': 'mrkdwn', 'text': f"{span}{msg('slack.sent_from', sender=esc(sender(by, (r.get('slackDraft') or {}).get('by'))))}"}]})
    return {'text': esc_keep_mentions(draft) if draft else headline, 'attachments': [{'color': COLOR.get(level, '#687385'), 'blocks': blocks}]}


def card_action_label(call: dict) -> str:
    if call.get('action') == 'mute':
        return msg('slack.label.mute', hours=call.get('hours') or DEFAULT_MUTE_HOURS)
    if call.get('action') == 'take' and call.get('ticket'):
        return msg('slack.label.progress', who=call['ticket'])
    if call.get('action') == 'take' and call.get('owner'):
        return msg('slack.label.progress', who=str(call['owner']).split('@')[0])
    if call.get('action') == 'resolve' and call.get('kind') in ('fixed', 'duplicate'):
        return cut(msg('slack.label.resolve_as', kind=history.kind_text({'kind': call['kind'], 'ref': call.get('ref') if call['kind'] == 'duplicate' else None})), 75)
    action = call.get('action')
    return msg(f'slack.label.{action}') if action in POST_ACTIONS else str(action)


def task_label(draft: dict | None) -> str:
    name = drafts.target(draft)
    return msg('slack.task.create_named', name=name) if name else msg('slack.task.create')


def task_button(action_id: str, draft: dict | None = None, primary: bool = True, confirm: dict | None = None) -> dict:
    return ({'type': 'button', 'action_id': CREATE_TASK, 'value': action_id, 'text': {'type': 'plain_text', 'text': task_label(draft)}}
            | ({'style': 'primary'} if primary else {}) | ({'confirm': confirm} if confirm else {}))


def task_confirm(title: str, draft: dict | None) -> dict:
    who = msg('slack.task.who.tech' if drafts.is_tech(draft) else 'slack.task.who.sprint')
    return {'title': {'type': 'plain_text', 'text': task_label(draft) + '?'},
            'text': {'type': 'mrkdwn', 'text': cut(msg('slack.task.confirm', title=esc(title), who=who), CONFIRM_LIMIT)},
            'confirm': {'type': 'plain_text', 'text': msg('slack.task.create_ok')}, 'deny': {'type': 'plain_text', 'text': msg('slack.cancel')}}


def take_offer_blocks(text: str, action_id: str, call: dict) -> list[dict]:
    return [{'type': 'section', 'text': {'type': 'mrkdwn', 'text': cut(text, SECTION_LIMIT)}},
            {'type': 'actions', 'elements': [card_button(CARD_ACTION, action_id, card_action_label(call), call.get('action'), primary=True)]}]


def confirm_button(run_id: str, title: str) -> dict:
    return {'type': 'button', 'action_id': PROPOSAL_CONFIRM, 'value': run_id, 'style': 'primary',
            'text': {'type': 'plain_text', 'text': cut(msg('proposals.button', title=redact(' '.join(str(title or '').split()))), BUTTON_LIMIT)}}


def answer_blocks(text: str, proposal: dict | None, action_id: str | None, card_action: dict | None = None, card: dict | None = None, confirm: dict | None = None) -> list[dict]:
    blocks = [{'type': 'section', 'text': {'type': 'mrkdwn', 'text': cut(text, SECTION_LIMIT)}}]
    buttons = []
    if proposal and action_id:
        lines = [x for x in str(proposal.get('description') or '').splitlines() if x.strip()][:DRAFT_LINES]
        name = drafts.target(proposal)
        title, *masked = redact_lines([esc(proposal.get('title') or '')] + [esc(x) for x in lines])
        draft = (msg('slack.draft.named', name=name[:1].upper() + name[1:], title=title) if name else msg('slack.draft.task', title=title)) + ''.join(f'\n> {x}' for x in masked)
        blocks.append({'type': 'section', 'text': {'type': 'mrkdwn', 'text': cut(draft, SECTION_LIMIT)}})
        buttons.append(task_button(action_id, proposal))
    if card_action and card and action_id:
        label = card_action_label(card_action)
        name = ' '.join(x for x in (str(card.get('svc') or '').strip(), str(card.get('label') or '').strip()) if x and x != '-')
        num = f"#{card['num']} " if card.get('num') else ''
        line = msg('slack.action', label=label, num=num, name=redact(esc(name))).rstrip(' ·')
        blocks.append({'type': 'section', 'text': {'type': 'mrkdwn', 'text': cut(line, SECTION_LIMIT)}})
        buttons.append(card_button(CARD_ACTION, action_id, label, card_action.get('action'), primary=card_action.get('action') != 'known'))
    if confirm and confirm.get('run'):
        buttons.append(confirm_button(str(confirm['run']), str(confirm.get('title') or '')))
    if buttons:
        blocks.append({'type': 'actions', 'elements': buttons})
    return blocks
