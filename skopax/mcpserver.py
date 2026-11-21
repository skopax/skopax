import datetime as dt
import json
import logging
import time
from dataclasses import dataclass
from typing import Any, Callable
from urllib.parse import quote

import anyio
from fastapi import HTTPException
from mcp import types
from mcp.server.lowlevel import Server
from mcp.server.streamable_http_manager import DEFAULT_MAX_REQUEST_BODY_SIZE, StreamableHTTPSessionManager
from pydantic import ValidationError
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import Headers
from starlette.responses import JSONResponse
from starlette.routing import Route

from skopax import __version__, brand, information
from skopax.actors import shown
from skopax.assign import Invalid as AssignInvalid, slug
from skopax.errkind import Authored, kind as error_kind
from skopax.features import off_text
from skopax.cardlink import card_url, chat_url
from skopax.auth import AuthError, bearer_token
from skopax.engine import parse_iso
from skopax.mcpauth import Forbidden, McpAuth, McpUser, Unavailable
from skopax.messages import msg
from skopax.notices import LABELS as NOTICE_LABELS
from skopax.models import ActionIn
from skopax.sessions import role_at_least
from skopax.store import NotFound, Store, iso, utc_now

log = logging.getLogger('skopax.mcp')

MCP = 'mcp'
USER_KEY = 'mcp_user'
METHOD_NOT_FOUND = -32601
UNSUPPORTED = frozenset({'server/discover'})
POLL_S = 2.0
HOUR_S = 3600
LIST_MAX = 50
LIST_DEFAULT = 20
ABOUT_LAYERS = 5
ABOUT_PLUGINS = 40
ABOUT_REASON = 160
STATES = ('ok', 'off', 'incompatible', 'locked', 'failed')
NOTICE_LIST_DEFAULT = 20
ASK_CARDS = 3
TEXT_CLIP = 220
ANSWER_CLIP = 6000
RECAP_CLIP = 4000
QUEUE_SCAN = 500
OPEN_STATES = ('requested', 'processing')
DONE_STATES = ('done', 'failed', 'cancelled')
SEV_RANK = {'crit': 0, 'warn': 1, 'info': 2}
SEVS = ('crit', 'warn', 'info')
FILTERS = ('open', 'firing', 'waiting', 'progress', 'handled', 'muted', 'all', 'archived')
CARD_TOOL = {'investigate': 'investigate_card', 'resolve': 'resolve_card', 'take': 'take_card', 'mute': 'mute_card'}
HALTED = {'stopped': 'the investigator is stopped (Stop switch on the page)', 'capped': 'the investigator is over its daily limit until local midnight', 'off': off_text('investigator')}
VERB = {'take': 'take', 'resolve': 'resolve', 'mute': 'mute', 'known': 'known noise', 'reopen': 'reopen', 'link': 'link ticket'}

def layers_text(layers: list[dict]) -> str:
    shown = [f"{x['name']} {x['version']}".strip() for x in layers[:ABOUT_LAYERS]]
    more = len(layers) - len(shown)
    return ', '.join(shown) + (f' and {more} more' if more > 0 else '')


def identity(name: str, layers: list[dict] | None = None) -> str:
    if not brand.shown():
        lead = name
    elif name == brand.DEFAULT_NAME:
        lead = f'{brand.DEFAULT_NAME} {brand.display_version()}'
    else:
        lead = f'{name}, powered by {brand.DEFAULT_NAME} {brand.display_version()}'
    return lead + (f', extended by {layers_text(layers)}' if layers else '') + '.'


def instructions(base_url: str, name: str | None = None, layers: list[dict] | None = None) -> str:
    where = f' ({base_url})' if base_url else ''
    name = name if name is not None else brand.product()
    return (
        f'{identity(name, layers)} The about tool says which version, edition and plug-ins this server runs. '
        f'{name} is a production health board{where}. Every anomaly is a card with a number (#14) and a key. '
        'Start with list_cards (open cards by default), then get_card for one card with its latest investigation. '
        f'ask sends a private question to the {name} investigator (an AI that reads prod Prometheus, Kubernetes and more); '
        'investigate_card queues a full investigation of one card. Both cost money and have caps, so ask one clear question instead of many small ones. '
        'take_card, resolve_card, mute_card, reopen_card, assign_card and link_ticket change a card exactly like the buttons on the page (Known noise stays a page and Slack button, where a person confirms it), '
        'in your name; confirm with the user before changing a card they did not name. '
        'list_notifications shows the same notifications as Notifications in the sidebar of the page: cards assigned to the user and what happened on them. '
        f'Cards, chats, runs and deploy reports come with a {name} link; show it to the user next to what it names.'
    )


CARD = {'type': ['string', 'integer'], 'description': 'The card: its number (14 or "#14") or its full key, as list_cards shows them'}
REASON = {'type': 'string', 'maxLength': 300, 'description': 'Optional short reason, kept on the card'}
TICKET = {'type': 'string', 'maxLength': 100, 'description': 'Optional Jira key such as OPS-123'}
WAIT = {'type': 'integer', 'minimum': 0, 'maximum': 55, 'description': 'Seconds to wait for the result in this call (capped by the server setting); 0 returns at once'}


def schema(props: dict, required: tuple = ()) -> dict:
    return {'type': 'object', 'properties': props, 'required': list(required), 'additionalProperties': False}


def hints(read_only: bool, destructive: bool, idempotent: bool, open_world: bool) -> types.ToolAnnotations:
    return types.ToolAnnotations(readOnlyHint=read_only, destructiveHint=destructive, idempotentHint=idempotent, openWorldHint=open_world)


TOOLS = (
    types.Tool(name='list_cards', title='List cards', annotations=hints(True, False, True, False),
               description='List {product} cards like the Board. filter: open (default: firing plus stopped cards still waiting to be addressed), firing, waiting, progress (In progress), '
                           'handled (No action needed), muted, all, archived. Narrow with sev, service (part of the service name), type (card type such as page-typos, or a source such as logs, prom, apm) '
                           'and text (searched in key, title and summary). Critical first. Returns number, severity, service / signal, state, owner, ticket and a one-line summary per card.',
               inputSchema=schema({'filter': {'type': 'string', 'enum': list(FILTERS)}, 'sev': {'type': 'string', 'enum': list(SEVS)}, 'service': {'type': 'string', 'maxLength': 100},
                                   'type': {'type': 'string', 'maxLength': 60}, 'text': {'type': 'string', 'maxLength': 100},
                                   'limit': {'type': 'integer', 'minimum': 1, 'maximum': LIST_MAX, 'description': f'Rows to return, default {LIST_DEFAULT}'}})),
    types.Tool(name='get_card', title='Get card', annotations=hints(True, False, True, False),
               description='One card in full by number or key: severity, state, numbers now / yesterday / last week, summary, the investigator note, suspects and recommendation, owner, ticket, '
                           'merged cards, the last action on it and its latest investigation (id, outcome, recap). A number of a card that already closed returns its last saved state.',
               inputSchema=schema({'card': CARD}, ('card',))),
    types.Tool(name='get_investigation', title='Get investigation', annotations=hints(True, False, True, False),
               description='One investigator run by id (from get_card, investigate_card or investigator_status): trigger, cards, outcome, recap, tool calls, cost. A private chat run shows only its public fields; '
                           'read your own chat answers with get_ask.',
               inputSchema=schema({'run_id': {'type': 'string', 'maxLength': 64}}, ('run_id',))),
    types.Tool(name='investigator_status', title='Investigator status', annotations=hints(True, False, False, False),
               description='What the {product} investigator is doing now: rooms and running runs, the waiting queue, today\'s spend against the daily limit, your chat messages left today.',
               inputSchema=schema({})),
    types.Tool(name='list_deploy_reports', title='List deploy reports', annotations=hints(True, False, True, False),
               description='The newest post-deploy check reports, newest first: release, level, headline, services and which ones were flagged.',
               inputSchema=schema({'limit': {'type': 'integer', 'minimum': 1, 'maximum': 20, 'description': 'Reports to return, default 5'}})),
    types.Tool(name='list_my_asks', title='List my asks', annotations=hints(True, False, True, False),
               description='Your own private chats with the investigator, newest first (the ones asked here and the ones on the {product} Chat page), with their ids for get_ask or a follow-up ask.',
               inputSchema=schema({})),
    types.Tool(name='get_ask', title='Get ask', annotations=hints(True, False, True, False),
               description='One of your chats by id with every question, its answer or state, and any card action the investigator proposed.',
               inputSchema=schema({'chat_id': {'type': 'string', 'maxLength': 64}}, ('chat_id',))),
    types.Tool(name='list_notifications', title='List my notifications', annotations=hints(True, False, True, False),
               description='Your {product} notifications, newest first, as in Notifications in the sidebar of the page: cards assigned to you, status changes on cards you hold made by someone else or by the server, '
                           'finished investigations, Slack posts and reminders on them, and chat answers. Reading them here does not mark them read.',
               inputSchema=schema({'limit': {'type': 'integer', 'minimum': 1, 'maximum': 50, 'description': 'Notifications to return, default 20'},
                                   'unread_only': {'type': 'boolean', 'description': 'Only the unread ones'}})),
    types.Tool(name='about', title='About', annotations=hints(True, False, True, False),
               description='Which {product} this is: the version it runs, the edition, who extended it with their own plug-ins, and which plug-ins are loaded and in what state. Takes no arguments.',
               inputSchema=schema({})),
    types.Tool(name='investigate_card', title='Investigate card', annotations=hints(False, False, False, True),
               description='Queue a full investigation of one card, like the Investigate button: the investigator reads prod sources and writes its finding on the card (and may post it to Slack). '
                           'One open investigation per card; a few per person per hour. Returns the queued request; with wait_seconds it waits for the run and returns its recap, '
                           'otherwise follow it with get_card or investigator_status.',
               inputSchema=schema({'card': CARD, 'wait_seconds': WAIT}, ('card',))),
    types.Tool(name='ask', title='Ask the investigator', annotations=hints(False, False, False, True),
               description='Ask the {product} investigator a question about prod and {product} cards, privately (only you see the chat, like the Chat page). Optional cards (up to 3) give it context; '
                           'pass chat_id to follow up in the same chat. Counts against your chat messages per day. Waits for the answer up to wait_seconds (default: the server setting); '
                           'when it is not ready, read it later with get_ask.',
               inputSchema=schema({'question': {'type': 'string', 'minLength': 1, 'maxLength': 8000}, 'cards': {'type': 'array', 'items': CARD, 'maxItems': ASK_CARDS},
                                   'chat_id': {'type': 'string', 'maxLength': 64}, 'wait_seconds': WAIT}, ('question',))),
    types.Tool(name='take_card', title='Take card', annotations=hints(False, False, True, False),
               description='Move a card to In progress in your name, like Take on the page; optional Jira ticket and reason.',
               inputSchema=schema({'card': CARD, 'ticket': TICKET, 'reason': REASON}, ('card',))),
    types.Tool(name='resolve_card', title='Resolve card', annotations=hints(False, True, True, False),
               description='Resolve a card as No action needed, like Resolve on the page; optional reason (kept under the finding) and ticket.',
               inputSchema=schema({'card': CARD, 'reason': REASON, 'ticket': TICKET}, ('card',))),
    types.Tool(name='mute_card', title='Mute card', annotations=hints(False, True, False, False),
               description='Mute a card for some hours (default 24, at most 720): no pushes or posts while muted.',
               inputSchema=schema({'card': CARD, 'hours': {'type': 'number', 'exclusiveMinimum': 0, 'maximum': 720}, 'reason': REASON}, ('card',))),
    types.Tool(name='reopen_card', title='Reopen card', annotations=hints(False, False, True, False),
               description='Undo take, resolve, mute or known noise on a card: it goes back to open.',
               inputSchema=schema({'card': CARD}, ('card',))),
    types.Tool(name='assign_card', title='Assign card', annotations=hints(False, False, True, False),
               description='Set who looks at a card: assignee is a person\'s name, "me" for yourself, or "" to remove the assignee. Optional email lets {product} send that person one Slack message.',
               inputSchema=schema({'card': CARD, 'assignee': {'type': 'string', 'maxLength': 200}, 'email': {'type': 'string', 'maxLength': 200}}, ('card', 'assignee'))),
    types.Tool(name='link_ticket', title='Link Jira ticket', annotations=hints(False, False, True, False),
               description='Link a Jira ticket (OPS-123) to a card; the card moves to In progress unless it is already resolved.',
               inputSchema=schema({'card': CARD, 'ticket': {'type': 'string', 'minLength': 3, 'maxLength': 100}}, ('card', 'ticket'))),
)

TOOL_ROLES: dict[str, str] = {
    'list_cards': 'viewer',
    'get_card': 'viewer',
    'get_investigation': 'viewer',
    'investigator_status': 'viewer',
    'list_deploy_reports': 'viewer',
    'list_my_asks': 'viewer',
    'get_ask': 'viewer',
    'list_notifications': 'viewer',
    'about': 'viewer',
    'investigate_card': 'operator',
    'ask': 'operator',
    'take_card': 'operator',
    'resolve_card': 'operator',
    'mute_card': 'operator',
    'reopen_card': 'operator',
    'assign_card': 'operator',
    'link_ticket': 'operator',
}


def allowed(user: McpUser | None, name: str) -> bool:
    return user is not None and name in TOOL_ROLES and role_at_least(user.role, TOOL_ROLES[name])


class ToolError(Authored, Exception):
    pass


@dataclass(frozen=True)
class Bridge:
    store: Store
    settings: Any
    chat: Callable[[str, str, str | None, list[str], str], dict]
    chat_view: Callable[[dict], dict]
    act: Callable[[ActionIn, str], dict]
    assign: Callable[[str, str | None, str | None, str], dict]
    card_view: Callable[[dict, bool, list[dict]], dict]
    board: Callable[[float], dict]
    kick: Callable[[], None]
    chat_used: Callable[[str], int]
    base: str = ''
    feature_on: Callable[[str], bool] = lambda _: True
    registry: Any = None
    plugin_on: Callable[[str], bool] = lambda _: True


def clip(text: Any, n: int = TEXT_CLIP) -> str:
    flat = ' '.join(str(text or '').split())
    return flat if len(flat) <= n else flat[:n - 1] + '…'


def done(a: dict) -> bool:
    return a.get('triage') in ('progress', 'handled')


INVESTIGATION = {'investigating': 'being investigated', 'dived': 'investigated'}


def waiting(a: dict) -> bool:
    return a.get('status') == 'cleared' and not a.get('muted') and not done(a) and a.get('sev') in ('warn', 'crit')


def state_of(a: dict) -> str:
    if a.get('muted'):
        return 'muted'
    if a.get('triage') == 'progress':
        return 'in progress'
    if a.get('triage') == 'handled':
        return 'no action needed'
    if a.get('status') != 'open' and not waiting(a):
        return 'cleared'
    found = INVESTIGATION.get(a.get('triage'))
    unaddressed = waiting(a) or a.get('triage') == 'dived'
    return ', '.join(['open', 'firing' if a.get('status') == 'open' else 'stopped firing'] + ([found] if found else []) + (['waiting to be addressed'] if unaddressed else []))


def stamp(value: Any) -> float:
    try:
        return parse_iso(value) if value else 0.0
    except (TypeError, ValueError):
        return 0.0


def card_ref(a: dict) -> str:
    return f"#{a['num']}" if a.get('num') is not None else str(a.get('key'))


def title_of(a: dict) -> str:
    return f"{a.get('svc') or '-'} / {a.get('label') or '?'}"


class Tools:
    def __init__(self, bridge: Bridge, poll_s: float = POLL_S, clock: Callable[[], float] = time.time):
        self.b = bridge
        self.store = bridge.store
        self.poll_s = poll_s
        self.clock = clock
        self.calls = {'list_cards': self.list_cards, 'get_card': self.get_card, 'get_investigation': self.get_investigation, 'investigator_status': self.investigator_status,
                      'list_deploy_reports': self.list_deploy_reports, 'list_my_asks': self.list_my_asks, 'get_ask': self.get_ask, 'list_notifications': self.list_notifications, 'about': self.about, 'take_card': self.take_card,
                      'resolve_card': self.resolve_card, 'mute_card': self.mute_card, 'reopen_card': self.reopen_card,
                      'assign_card': self.assign_card, 'link_ticket': self.link_ticket}
        self.waits = {'investigate_card': (self.start_dive, self.dive_result), 'ask': (self.start_ask, self.ask_result)}

    async def run(self, name: str, args: dict, user: McpUser) -> str:
        log.info('mcp %s by %s', name, user.name)
        if name not in TOOL_ROLES:
            raise ToolError(msg('mcp.no_tool', name=name))
        if not allowed(user, name):
            raise ToolError(msg('mcp.needs_role', role=TOOL_ROLES[name]))
        try:
            if name in self.waits:
                start, result = self.waits[name]
                began = time.monotonic()
                handle, seconds = await anyio.to_thread.run_sync(start, user, args)
                return await self.wait(lambda: result(user, handle, False), lambda: result(user, handle, True), seconds - (time.monotonic() - began))
            return await anyio.to_thread.run_sync(self.calls[name], user, args)
        except ToolError:
            raise
        except Exception as e:
            log.error('mcp %s by %s failed: %s', name, user.name, error_kind(e))
            raise ToolError(msg('mcp.failed', error=type(e).__name__)) from None

    async def wait(self, ready: Callable[[], str | None], final: Callable[[], str], seconds: float) -> str:
        deadline = time.monotonic() + seconds
        while True:
            got = await anyio.to_thread.run_sync(ready)
            if got is not None:
                return got
            left = deadline - time.monotonic()
            if left <= 0:
                return await anyio.to_thread.run_sync(final)
            await anyio.sleep(min(self.poll_s, left))

    def site(self) -> str:
        return (self.b.base or '').rstrip('/')

    def link(self, a: dict) -> str:
        return card_url(self.site(), str(a.get('key')), a.get('num'))

    def num_of(self, key: str) -> int | None:
        return self.store.card_numbers([key]).get(key)

    def chat_link(self, chat_id: Any) -> str:
        return f"{self.site()}/#t=chat/thread/{quote(str(chat_id), safe='')}"

    def wait_of(self, args: dict, default: int) -> int:
        top = int(self.b.settings.mcp_wait_seconds or 0)
        wanted = args.get('wait_seconds')
        return max(0, min(top, default if wanted is None else int(wanted)))

    def key_of(self, ref: Any, live: bool = True) -> str:
        text = str(ref if ref is not None else '').strip().lstrip('#').strip()
        if not text:
            raise ToolError('name a card: its number or key')
        if text.isdigit():
            key = self.store.key_of_number(int(text))
            if key is None:
                raise ToolError(f'no card #{text}')
            if live:
                try:
                    self.card_only(self.store.anomaly(key))
                except NotFound:
                    raise ToolError(f'card #{text} is closed and no longer on the board') from None
            return key
        try:
            self.card_only(self.store.anomaly(text))
        except NotFound:
            raise ToolError(f'no card with key {text}; list_cards shows the numbers and keys') from None
        return text

    def card_only(self, card: dict) -> dict:
        if information.is_item(card):
            raise ToolError(msg('cards.information_item', key=card.get('key')))
        return card

    def card(self, key: str) -> dict:
        try:
            return self.store.anomaly(key)
        except NotFound:
            raise ToolError(f'no card with key {key}') from None

    def visible(self, state: dict) -> tuple[list[dict], dict[str, int], list[dict]]:
        cards = information.cards_only(state.get('anomalies'))
        by_key = {a.get('key'): a for a in cards}
        mutes = {m.get('pattern'): m.get('at') for m in state.get('muted') or [] if isinstance(m, dict)}

        def host(a: dict) -> dict | None:
            p = by_key.get(a.get('parent')) if a.get('parent') and a.get('parent') != a.get('key') else None
            return p if p and (p.get('status') == 'open' or a.get('status') == 'cleared') else None

        kids: dict[str, int] = {}
        top = []
        for a in cards:
            seen, root, p = set(), None, host(a)
            while p is not None and p.get('key') not in seen:
                seen.add(p.get('key'))
                root, p = p, host(p)
            if root is not None and root is not a:
                kids[root['key']] = kids.get(root['key'], 0) + 1
            else:
                top.append(a)
        limit = self.clock() - int(self.b.settings.archive_hours or 48) * HOUR_S

        def archived(a: dict) -> bool:
            if (a.get('status') == 'open' and not a.get('muted')) or waiting(a):
                return False
            last = max(stamp(a.get('clearedAt')), stamp(a.get('triagedAt')), stamp(mutes.get(a.get('key'))))
            return 0 < last < limit

        old = [a for a in top if archived(a)]
        return [a for a in top if a not in old], kids, old

    def list_cards(self, user: McpUser, args: dict) -> str:
        which = args.get('filter') or 'open'
        live, kids, old = self.visible(self.store.load())
        picks = {'open': lambda a: (a.get('status') == 'open' and not a.get('muted') and not done(a)) or waiting(a),
                 'firing': lambda a: a.get('status') == 'open' and not a.get('muted') and not done(a), 'waiting': waiting,
                 'progress': lambda a: a.get('triage') == 'progress', 'handled': lambda a: a.get('triage') == 'handled',
                 'muted': lambda a: bool(a.get('muted')) and not done(a), 'all': lambda a: True, 'archived': lambda a: True}
        rows = [a for a in (old if which == 'archived' else live) if picks[which](a)]
        sev, svc, kind, text = args.get('sev'), str(args.get('service') or '').lower().strip(), str(args.get('type') or '').lower().strip(), str(args.get('text') or '').lower().strip()
        if sev:
            rows = [a for a in rows if a.get('sev') == sev]
        if svc:
            rows = [a for a in rows if svc in str(a.get('svc') or '').lower()]
        if kind:
            rows = [a for a in rows if kind in (str(a.get('sig') or '').lower(), str(a.get('src') or '').lower())]
        if text:
            rows = [a for a in rows if any(text in str(a.get(f) or '').lower() for f in ('key', 'svc', 'label', 'what', 'title'))]
        rows.sort(key=lambda a: (SEV_RANK.get(a.get('sev'), 3), -stamp(a.get('lastSeen') or a.get('firstSeen'))))
        limit = int(args.get('limit') or LIST_DEFAULT)
        if not rows:
            return f'No {which} cards' + (' match those filters.' if any((sev, svc, kind, text)) else '.')
        lines = [f'{len(rows)} {which} card{"" if len(rows) == 1 else "s"}' + (f', showing the first {limit}' if len(rows) > limit else '') + ':']
        for a in rows[:limit]:
            extra = [x for x in (f"owner {a['owner']}" if a.get('owner') else '', a.get('ticket') or '', f"{kids[a['key']]} merged" if kids.get(a.get('key')) else '') if x]
            lines.append(f"- {card_ref(a)} · {a.get('sev') or '?'} · {title_of(a)} · {state_of(a)}" + (f" · {', '.join(extra)}" if extra else '') +
                         (f" · first seen {a['firstSeen']}" if a.get('firstSeen') else '') + f"\n  {clip(a.get('what') or a.get('label'))}\n  key: {a.get('key')}\n  link: {self.link(a)}")
        return '\n'.join(lines)

    def get_card(self, user: McpUser, args: dict) -> str:
        text = str(args.get('card') if args.get('card') is not None else '').strip().lstrip('#').strip()
        if text.isdigit() and (key := self.store.key_of_number(int(text))) is not None:
            try:
                card = self.card_only(self.store.anomaly(key))
            except NotFound:
                past = self.store.last_past_card(key)
                if not past:
                    raise ToolError(f'card #{text} is closed and has no saved state') from None
                return self.card_text(self.b.card_view(past | {'num': int(text)}, False, []), None, None)
        else:
            card = self.card(self.key_of(text))
        view = self.b.card_view(card, True, self.store.children(card['key'])) | {'last': self.store.last_action(card['key'])}
        runs = self.store.card_runs([card['key']]).get(card['key'])
        return self.card_text(view, runs, card)

    def card_text(self, v: dict, runs: dict | None, card: dict | None) -> str:
        lines = [f"Card {card_ref(v)} · {v.get('sev') or '?'} · {title_of(v)}" + ('' if v.get('live') else ' (closed, last saved state)'),
                 f"key: {v.get('key')}", f"link: {self.link(v)}", f"chat about it: {chat_url(self.site(), str(v.get('key')), v.get('num'))}", f"state: {state_of(v)}" + (f" · owner {v['owner']}" if v.get('owner') else '') + (f" · ticket {v['ticket']}" if v.get('ticket') else ''),
                 ' · '.join(f'{label} {v[k]}' for k, label in (('firstSeen', 'first seen'), ('lastSeen', 'last seen'), ('clearedAt', 'cleared')) if v.get(k)) or 'no times recorded']
        if any(v.get(k) is not None for k in ('now', 'd1', 'd7')):
            unit = f" {v['unit']}" if v.get('unit') else ''
            lines.append(f"now {v.get('now')}{unit} · yesterday {v.get('d1')}{unit} · last week {v.get('d7')}{unit}")
        if v.get('what'):
            lines.append(f"summary: {clip(v['what'], 800)}")
        note = v.get('note')
        if note:
            lines.append(f"investigator note: {clip(note.get('text') if isinstance(note, dict) else note, 2000)}")
        if v.get('suspects'):
            lines.append('suspects: ' + '; '.join(clip(s, 200) for s in v['suspects']))
        if v.get('recommend'):
            lines.append(f"next: {clip(v['recommend'], 800)}")
        if v.get('tags'):
            lines.append('tags: ' + ', '.join(str(t.get('id') if isinstance(t, dict) else t) for t in v['tags']))
        if v.get('url'):
            lines.append(f"page: {v['url']}")
        for m in v.get('merged') or []:
            lines.append(f"merged: {card_ref(m)} · {m.get('sev') or '?'} · {title_of(m)} · {m.get('status')}")
        last = v.get('last')
        if last:
            lines.append(f"last action: {last.get('type')} · {last.get('status')} · by {shown(last.get('requestedBy')) or '?'} at {last.get('requestedAt') or '?'}" + (f" · {clip(last['result'], 200)}" if last.get('result') else ''))
        if runs and runs.get('latest'):
            r = runs['latest']
            lines.append(f"latest investigation: {r.get('id')} · {r.get('trigger')} · {r.get('outcome')} · {r.get('startedAt')} · ${float(r.get('usd') or 0):.2f} ({runs.get('n')} runs on this card in 30 days)")
            if r.get('recap'):
                lines.append(f"recap: {clip(r['recap'], RECAP_CLIP)}")
        elif card is not None:
            lines.append('latest investigation: none in the last 30 days')
        return '\n'.join(lines)

    def get_investigation(self, user: McpUser, args: dict) -> str:
        try:
            r = self.store.investigation(str(args.get('run_id') or '').strip())
        except NotFound as e:
            raise ToolError(e.text) from None
        return self.run_text(r)

    def run_text(self, r: dict) -> str:
        private = (r.get('ask') or {}).get('private')
        calls = r.get('toolCalls') or []
        lines = [f"Run {r.get('id')} · {r.get('trigger')} · {r.get('outcome')} · started {r.get('startedAt')}" + (f" · ended {r['endedAt']}" if r.get('endedAt') else ''),
                 f"model {r.get('model')} · {r.get('turns') or 0} turns · {len(calls)} tool calls · ${float(r.get('usd') or 0):.2f}"]
        if not private:
            lines.append(f"record: {self.site()}/api/investigations/{quote(str(r.get('id')), safe='')} (JSON, needs the page login)")
        if private:
            lines.append('A private chat run: only its owner reads the question and answer, with get_ask.')
            return '\n'.join(lines)
        if r.get('keys'):
            lines.append('cards: ' + ', '.join(f'{k} ({card_url(self.site(), k, self.num_of(k))})' for k in r['keys']))
        ask = r.get('ask') or {}
        if ask.get('text'):
            lines.append(f"question by {shown(ask.get('by'))}: {clip(ask['text'], 600)}")
        if calls:
            names: dict[str, int] = {}
            for c in calls:
                names[c.get('name') or '?'] = names.get(c.get('name') or '?', 0) + 1
            errors = sum(1 for c in calls if c.get('error'))
            lines.append('tools: ' + ', '.join(f'{n}×{k}' if k > 1 else n for n, k in names.items()) + (f' · {errors} failed' if errors else ''))
        if r.get('error'):
            lines.append(f"error: {clip(r['error'], 400)}")
        if r.get('recap'):
            lines.append(f"recap:\n{str(r['recap'])[:RECAP_CLIP]}")
        if r.get('cardAction'):
            lines.append(self.proposal(r['cardAction']))
        return '\n'.join(lines)

    def investigator_status(self, user: McpUser, args: dict) -> str:
        b = self.b.board(self.clock())
        lim = b.get('limits') or {}
        s = self.b.settings
        lines = [f"Investigator: mode {b.get('mode')}" + ('' if b.get('enabled') else ' (off)') + (f" · halted: {b['halted']}" if b.get('halted') else ''),
                 f"spent today ${float(lim.get('spentToday') or 0):.2f} of ${float(lim.get('cap') or 0):.2f} · page asks this hour {lim.get('pageAsks', 0)}/{lim.get('pageAskCap', '?')}"
                 f" · your chat messages today {self.b.chat_used(user.name)}/{s.chat_daily_cap}"]
        for room in b.get('rooms') or []:
            run = room.get('run')
            what = ('private chat' if run.get('trigger') == 'chat' else ', '.join(run.get('keys') or []) or '-') if run else ''
            lines.append(f"- {room.get('name')}: " + (f"running {run.get('id')} · {run.get('trigger')} · {what} · since {run.get('startedAt')} · {run.get('toolCalls', 0)} tool calls" if run else 'free'))
        queue = b.get('waiting') or []
        lines.append(f"queue: {len(queue)} waiting" if queue else 'queue: empty')
        for w in queue[:10]:
            lines.append(f"- {w.get('type')} · {w.get('text') or ''} · by {shown(w.get('by')) or 'private'} · {w.get('why')}")
        for r in (b.get('recent') or [])[:5]:
            lines.append(f"recent: {r.get('id')} · {r.get('trigger')} · {r.get('outcome')} · {', '.join(r.get('keys') or []) or '-'} · {r.get('startedAt')}")
        return '\n'.join(lines)

    def layers(self) -> list[dict]:
        return self.b.registry.extensions() if self.b.registry is not None else []

    def about(self, user: McpUser, args: dict) -> str:
        name = brand.product()
        lines = [identity(name), f"edition: {self.b.registry.edition() if self.b.registry is not None else 'community'}"] + ([f'site: {brand.SITE}'] if brand.shown() else [])
        if self.site():
            lines.append(f'server: {self.site()}')
        layers = self.layers()
        if layers:
            lines.append('Extended by:')
            for x in layers:
                lines.append(f"- {x['name']} {x['version']}".rstrip() + (f" \u00b7 plug-ins: {', '.join(x['plugins'])}" if x['package'] else ''))
        rows = self.b.registry.standing(self.b.plugin_on) if self.b.registry is not None else []
        counts = {s: sum(1 for r in rows if r['state'] == s) for s in (*STATES, *sorted({r['state'] for r in rows} - set(STATES)))}
        lines.append(f'Plug-ins: {len(rows)} \u00b7 ' + ' \u00b7 '.join(f'{s} {n}' for s, n in counts.items() if n or s == 'ok'))
        admin = role_at_least(user.role, 'admin')
        for r in rows[:ABOUT_PLUGINS]:
            lines.append(f"- {r['id']} {r['version']}".rstrip() + f" \u00b7 {r['state']}" + (f" \u00b7 {clip(r['reason'], ABOUT_REASON)}" if admin and r['state'] != 'ok' and r['reason'] else ''))
        if len(rows) > ABOUT_PLUGINS:
            lines.append(f'and {len(rows) - ABOUT_PLUGINS} more')
        return '\n'.join(lines)

    def list_deploy_reports(self, user: McpUser, args: dict) -> str:
        reports = self.store.list_deploy_reports(int(args.get('limit') or 5))
        if not reports:
            return 'No deploy reports yet.'
        lines = []
        for r in reports:
            brief = r.get('brief') or {}
            rows = r.get('rows') or []
            flagged = [x.get('svc') for x in rows if x.get('flags')]
            lines.append(f"- {r.get('id')} · {r.get('createdAt')} · {brief.get('level') or ('warn' if flagged else 'ok')} · {clip(r.get('summary'), 200)}" +
                         (f"\n  {clip(brief['headline'], 300)}" if brief.get('headline') else '') +
                         f"\n  {len(rows)} services" + (f", flagged: {', '.join(str(f) for f in flagged[:10])}" if flagged else ', none flagged') +
                         f"\n  link: {self.site()}/#r={quote(str(r.get('id')), safe='')}")
        return '\n'.join(lines)

    def list_my_asks(self, user: McpUser, args: dict) -> str:
        chats = self.store.chats(user.name, LIST_DEFAULT)
        if not chats:
            return 'You have no chats yet. Start one with ask.'
        return '\n'.join(f"- {c['id']} · {clip(c.get('title'), 120)} · {c.get('messages')} message{'' if c.get('messages') == 1 else 's'}" +
                         (' · waiting for an answer' if c.get('pending') else '') + f" · {c.get('updatedAt')} · {self.chat_link(c['id'])}" for c in chats)

    def list_notifications(self, user: McpUser, args: dict) -> str:
        inbox = slug(user.name) if '@' in user.name else ''
        rows = self.store.notices_of(inbox, int(args.get('limit') or NOTICE_LIST_DEFAULT)) if inbox else []
        if args.get('unread_only'):
            rows = [r for r in rows if not r.get('read')]
        unread = self.store.unread_notices(inbox) if inbox else 0
        if not rows:
            return f'No notifications{" unread" if args.get("unread_only") else ""}. {unread} unread in all.'
        lines = [f'{unread} unread']
        for r in rows:
            where = f"{self.site()}/{r['link'].lstrip('/')}" if r.get('link') else self.link(r) if r.get('key') else ''
            lines.append(f"- {'unread · ' if not r.get('read') else ''}{NOTICE_LABELS.get(r.get('kind'), r.get('kind'))}" + (f" · #{r['num']}" if r.get('num') else '') +
                         (f" {clip(r.get('title'), 120)}" if r.get('title') else '') + (f" · {r['note']}" if r.get('note') else '') + f" · by {shown(r.get('by')) or '?'} · {r.get('t')}" +
                         (f" · {where}" if where else ''))
        return '\n'.join(lines)

    def get_ask(self, user: McpUser, args: dict) -> str:
        chat = self.store.chat_for(user.name, str(args.get('chat_id') or '').strip())
        if chat is None:
            raise ToolError('no such chat of yours, or it is not shared with you')
        return self.chat_text(self.b.chat_view(chat))

    def chat_text(self, chat: dict, only_last: bool = False) -> str:
        msgs = chat.get('messages') or []
        head = f"Chat {chat.get('id')} · {clip(chat.get('title'), 120)}" + (' (private to you)' if chat.get('mine', True) and not chat.get('shared') else ' (shared by link)' if chat.get('shared') else '') + \
               (f" · by {shown(chat['by'])}" if chat.get('by') and not chat.get('mine', True) else '')
        lines = [head] + ([f"link: {self.chat_link(chat.get('id'))}"] if chat.get('mine', True) else [])
        for i, m in enumerate(msgs[-1:] if only_last else msgs, start=len(msgs) if only_last else 1):
            lines.append(f"\n[{i}] question: {clip(m.get('text'), 600)}")
            if m.get('answer'):
                lines.append(f"answer:\n{str(m['answer'])[:ANSWER_CLIP]}")
            elif m.get('status') in OPEN_STATES:
                p = m.get('progress') or {}
                why = HALTED.get(chat.get('halted') or '')
                lines.append(f"answer: not ready yet ({m.get('status')}" + (f", {p.get('toolCalls')} tool calls so far" if p.get('toolCalls') else '') + (f', {why}' if why else '') +
                             f"); read it later with get_ask(chat_id='{chat.get('id')}')")
            else:
                lines.append(f"answer: none ({m.get('status')}" + (f": {clip(m['error'], 300)}" if m.get('error') else '') + ')')
            if m.get('cardAction'):
                lines.append(self.proposal(m['cardAction']))
        return '\n'.join(lines)

    def proposal(self, call: dict) -> str:
        action = str(call.get('action') or '')
        extra = ', '.join(f'{k} {call[k]}' for k in ('ticket', 'hours', 'owner') if call.get(k))
        tool = CARD_TOOL.get(action)
        return f"proposed: {action} on {call.get('key')}" + (f' ({extra})' if extra else '') + (f'; call {tool} to apply it' if tool else '')

    def start_dive(self, user: McpUser, args: dict) -> tuple[dict, int]:
        key = self.key_of(args.get('card'))
        card = self.card(key)
        if not self.b.feature_on('investigator'):
            raise ToolError(off_text('investigator'))
        cap = int(self.b.settings.mcp_dive_cap or 0)
        if cap <= 0:
            raise ToolError('investigations over MCP are switched off (Admin \u203a Investigator, MCP investigations per person per hour)')
        for st in OPEN_STATES:
            for a in self.store.list_actions(st, QUEUE_SCAN):
                if a.get('type') == 'dive' and a.get('key') == key:
                    return {'action': a, 'queued': False}, self.wait_of(args, 0)
        since = iso(utc_now() - dt.timedelta(seconds=HOUR_S))
        if self.store.count_actions('dive', since, source=MCP, by=user.name) >= cap:
            raise ToolError(f'{cap} investigations over MCP an hour is your cap (Admin \u203a Investigator); try again later or use the page')
        action = self.store.add_action({'type': 'dive', 'key': key, 'target': key, 'label': f'Investigate {title_of(card)}'[:300], 'requestedBy': user.name, 'source': MCP, 'via': MCP})
        self.store.add_activity(MCP, user.name, f'investigate queued · {key} · via MCP', key=key)
        self.b.kick()
        return {'action': action, 'queued': True}, self.wait_of(args, 0)

    def dive_result(self, user: McpUser, handle: dict, final: bool) -> str | None:
        action = handle['action']
        try:
            now = self.store.get_action(action['id'])
        except NotFound:
            now = action
        if not final and now.get('status') in OPEN_STATES:
            return None
        who = 'queued' if handle['queued'] else f"already queued by {action.get('requestedBy')}"
        head = f"Investigate {action.get('key')}: request {action['id']} {who}, now {now.get('status')}" + (f" · {clip(now['result'], 300)}" if now.get('result') else '')
        card = f"\ncard: {card_url(self.site(), str(action.get('key')), self.num_of(str(action.get('key'))))}"
        if now.get('status') in OPEN_STATES:
            why = next((w.get('why') for w in self.b.board(self.clock()).get('waiting') or [] if w.get('id') == action['id']), None) if now.get('status') == 'requested' else 'running now'
            return head + (f' ({why})' if why else '') + card + '\nThe run takes a few minutes; follow it with get_card (latest investigation) or investigator_status.'
        run = self.store.run_of_action(action['id'])
        return head + card + ('\n' + self.run_text(run) if run else '')

    def start_ask(self, user: McpUser, args: dict) -> tuple[dict, int]:
        keys = [self.key_of(c) for c in (args.get('cards') or [])[:ASK_CARDS]]
        chat_id = str(args.get('chat_id') or '').strip() or None
        try:
            chat = self.b.chat(user.name, str(args.get('question') or ''), chat_id, keys, user.role)
        except HTTPException as e:
            raise ToolError(str(e.detail)) from None
        return chat, self.wait_of(args, int(self.b.settings.mcp_wait_seconds or 0))

    def ask_result(self, user: McpUser, chat: dict, final: bool) -> str | None:
        fresh = self.store.chat_for(user.name, chat['id'])
        view = self.b.chat_view(fresh) if fresh else chat
        last = (view.get('messages') or [{}])[-1]
        if not final and last.get('status') in OPEN_STATES:
            return None
        return self.chat_text(view, only_last=True)

    def card_action(self, user: McpUser, kind: str, ref: Any, **fields) -> str:
        key = self.key_of(ref)
        try:
            body = ActionIn(type=kind, key=key, target=key, label=f'{VERB[kind]} via MCP', **{k: v for k, v in fields.items() if v not in (None, '')})
        except ValidationError:
            raise ToolError(msg('mcp.action_invalid', verb=VERB[kind])) from None
        try:
            out = self.b.act(body, user.name)
        except HTTPException as e:
            raise ToolError(str(e.detail)) from None
        self.store.set_action_fields(out['id'], {'via': MCP})
        if out.get('status') == 'failed':
            raise ToolError(str(out.get('result') or f'{VERB[kind]} was not applied'))
        self.store.add_activity(MCP, user.name, f'{VERB[kind]} · {key} · via MCP', key=key)
        card = self.card(key)
        return f"{card_ref(card)} {title_of(card)}: {out.get('result') or 'done'} · now {state_of(card)} · {self.link(card)}"

    def take_card(self, user: McpUser, args: dict) -> str:
        return self.card_action(user, 'take', args.get('card'), ticket=args.get('ticket'), reason=args.get('reason'))

    def resolve_card(self, user: McpUser, args: dict) -> str:
        return self.card_action(user, 'resolve', args.get('card'), ticket=args.get('ticket'), reason=args.get('reason'))

    def mute_card(self, user: McpUser, args: dict) -> str:
        return self.card_action(user, 'mute', args.get('card'), hours=args.get('hours'), reason=args.get('reason'))


    def reopen_card(self, user: McpUser, args: dict) -> str:
        return self.card_action(user, 'reopen', args.get('card'))

    def link_ticket(self, user: McpUser, args: dict) -> str:
        return self.card_action(user, 'link', args.get('card'), ticket=args.get('ticket'))

    def assign_card(self, user: McpUser, args: dict) -> str:
        key = self.key_of(args.get('card'))
        who = str(args.get('assignee') or '').strip()
        email = str(args.get('email') or '').strip() or None
        if who.lower() == 'me':
            who, email = user.name, email or user.email or (user.name if '@' in user.name else None)
        elif who.lower() in ('none', '-'):
            who = ''
        try:
            out = self.b.assign(key, who or None, email if who else None, user.name)
        except AssignInvalid as e:
            raise ToolError(e.text) from None
        except NotFound as e:
            raise ToolError(e.text) from None
        self.store.add_activity(MCP, user.name, f"{'assigned to ' + who if who else 'assignee removed'} · {key} · via MCP", key=key)
        card = self.card(key)
        if not who:
            return f'{card_ref(card)} {title_of(card)}: assignee removed'
        dm = out.get('dm')
        return f"{card_ref(card)} {title_of(card)}: assigned to {out.get('owner')}" + (f' · Slack message: {dm}' if isinstance(dm, str) else ' · Slack message sent' if dm else '')


def shown_tool(tool: types.Tool) -> types.Tool:
    return tool.model_copy(update={'description': brand.fill(tool.description or '')})


class Fresh(Server):
    def __init__(self, tools: Tools, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.tools = tools

    def create_initialization_options(self, *args, **kwargs):
        self.instructions = instructions(self.tools.site(), layers=self.tools.layers())
        self.website_url = brand.SITE if brand.shown() else None
        return super().create_initialization_options(*args, **kwargs)


def build_server(tools: Tools) -> Server:
    server = Fresh(tools, brand.mcp_slug(brand.product()), version=__version__)

    @server.list_tools()
    async def list_tools(asked: types.ListToolsRequest) -> types.ListToolsResult:
        if asked is None:
            return types.ListToolsResult(tools=[shown_tool(t) for t in TOOLS])
        request = server.request_context.request
        user = request.state.mcp_user if request is not None else None
        return types.ListToolsResult(tools=[shown_tool(t) for t in TOOLS if allowed(user, t.name)])

    @server.call_tool(validate_input=True)
    async def call_tool(name: str, arguments: dict) -> list[types.TextContent]:
        request = server.request_context.request
        user = request.state.mcp_user if request is not None else None
        if user is None:
            raise ToolError('not logged in')
        return [types.TextContent(type='text', text=await tools.run(name, arguments or {}, user))]

    return server


class Switched:
    def __init__(self, inner, on: Callable[[], bool], refused: Callable[[], str]):
        self.inner = inner
        self.on = on
        self.refused = refused

    async def __call__(self, scope, receive, send) -> None:
        if not await run_in_threadpool(self.on):
            await JSONResponse({'detail': self.refused()}, status_code=409)(scope, receive, send)
            return
        await self.inner(scope, receive, send)


class Endpoint:
    def __init__(self, auth: McpAuth, manager: StreamableHTTPSessionManager):
        self.auth = auth
        self.manager = manager

    async def __call__(self, scope, receive, send) -> None:
        if not await run_in_threadpool(lambda: self.auth.enabled):
            await JSONResponse({'detail': msg('mcp.not_set_up')}, status_code=404)(scope, receive, send)
            return
        token = bearer_token(Headers(scope=scope).get('authorization'))
        if token is None:
            await self.refuse(scope, receive, send, 401, msg('mcp.login_first'))
            return
        try:
            user = await anyio.to_thread.run_sync(self.auth.user, token)
        except Unavailable as e:
            log.warning('mcp user check failed: %s', error_kind(e))
            await JSONResponse({'detail': e.detail}, status_code=503)(scope, receive, send)
            return
        except Forbidden as e:
            log.info('mcp token refused: %s', error_kind(e))
            await self.refuse(scope, receive, send, 403, e.detail, 'insufficient_scope', e.description)
            return
        except AuthError as e:
            log.info('mcp token rejected: %s', error_kind(e))
            await self.refuse(scope, receive, send, 401, msg('mcp.token_invalid'), 'invalid_token', msg('mcp.token_invalid_header'))
            return
        if scope.get('method') != 'POST':
            await JSONResponse({'detail': msg('mcp.post_only')}, status_code=405, headers={'Allow': 'POST'})(scope, receive, send)
            return
        body = await read_body(receive, DEFAULT_MAX_REQUEST_BODY_SIZE)
        if body is None:
            await JSONResponse({'detail': msg('mcp.too_large')}, status_code=413)(scope, receive, send)
            return
        unknown = probe_id(body)
        if unknown is not None:
            await JSONResponse({'jsonrpc': '2.0', 'id': unknown, 'error': {'code': METHOD_NOT_FOUND, 'message': 'Method not found'}})(scope, receive, send)
            return
        scope.setdefault('state', {})[USER_KEY] = user
        await self.manager.handle_request(scope, replay(body, receive), send)

    async def refuse(self, scope, receive, send, code: int, detail: str, *why: str) -> None:
        challenge = await run_in_threadpool(self.auth.challenge, *why)
        await JSONResponse({'detail': detail}, status_code=code, headers={'WWW-Authenticate': challenge})(scope, receive, send)


async def read_body(receive, limit: int) -> bytes | None:
    chunks, size, more = [], 0, True
    while more:
        message = await receive()
        if message.get('type') != 'http.request':
            break
        chunk = message.get('body', b'')
        size += len(chunk)
        if size > limit:
            return None
        chunks.append(chunk)
        more = message.get('more_body', False)
    return b''.join(chunks)


def replay(body: bytes, receive):
    sent = False

    async def again() -> dict:
        nonlocal sent
        if not sent:
            sent = True
            return {'type': 'http.request', 'body': body, 'more_body': False}
        return await receive()

    return again


def probe_id(body: bytes):
    try:
        message = json.loads(body)
    except ValueError:
        return None
    if isinstance(message, dict) and message.get('method') in UNSUPPORTED and 'id' in message:
        return message['id']
    return None


def first_sentence(text: str, n: int = 180) -> str:
    text = ' '.join(str(text or '').split())
    cut_at = text.find('. ')
    return clip(text[:cut_at + 1] if cut_at >= 0 else text, n)


def info_of(auth: McpAuth, on: bool = True) -> dict:
    tools = [{'name': t.name, 'title': t.title or t.name, 'what': first_sentence(brand.fill(t.description or '')), 'readOnly': bool(t.annotations and t.annotations.readOnlyHint)} for t in TOOLS]
    s = auth.settings
    return {'on': on, 'enabled': auth.enabled, 'url': auth.resource(), 'clientId': auth.client_id(), 'waitSeconds': s.mcp_wait_seconds, 'diveCap': s.mcp_dive_cap, 'tools': tools}


def mount(app, bridge: Bridge, auth: McpAuth, on: Callable[[], bool] = lambda: True) -> StreamableHTTPSessionManager:
    manager = StreamableHTTPSessionManager(app=build_server(Tools(bridge)), stateless=True, json_response=True)
    app.router.routes.append(Route('/mcp', endpoint=Switched(Endpoint(auth, manager), on, lambda: off_text('mcp')), methods=['GET', 'POST', 'DELETE']))

    def metadata() -> JSONResponse:
        if not on():
            return JSONResponse({'detail': off_text('mcp')}, status_code=409)
        if not auth.enabled:
            return JSONResponse({'detail': msg('mcp.not_set_up')}, status_code=404)
        return JSONResponse(auth.metadata(), headers={'Cache-Control': 'max-age=300'})

    app.add_api_route('/.well-known/oauth-protected-resource', metadata, methods=['GET'], include_in_schema=False)
    app.add_api_route('/.well-known/oauth-protected-resource/mcp', metadata, methods=['GET'], include_in_schema=False)
    app.add_api_route('/api/mcp/info', lambda: JSONResponse(info_of(auth, bool(on()))), methods=['GET'], include_in_schema=False)
    return manager
