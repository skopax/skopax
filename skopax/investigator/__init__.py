import base64
import dataclasses
import json
import logging
import re
import socket
import threading
import time
import uuid
from collections.abc import Callable, Mapping

import anthropic

from skopax import advisor, followup, history, information, messages, severity, tracing
from skopax.ai import IMAGES_OFF, ModelSetting, clients_for, label_of, provider_of_ref, route, wrap
from skopax.config import Setting, Settings
from skopax.contract import OrgPack
from skopax.engine import iso as epoch_iso
from skopax.errkind import Refusal, kind as error_kind, text_of
from skopax.features import off_text
from skopax.investigator import pricing, prompt, select
from skopax.investigator.flags import REQUESTS, SOFT, asked, scan
from skopax.investigator.runner import Result, RunConfig, Runner
from skopax.investigator.guard import CATEGORIES, MASK_TEXT, apply_mask
from skopax.investigator.tools import Effects, Sources, Toolbox, applied_writes, leaves, masked_leaves
from skopax.investigator.toolset import Run, asked_gate
from skopax.investigator.web import own_domains as own_web_domains, web_from
from skopax.postdeploy import NUMBERS_BY
from skopax import clock
from skopax.messages import msg
from skopax.outward import outward
from skopax.store import HELD_OUTCOMES, LEASE_ID, ActionMoved, NotFound, Store, new_id

log = logging.getLogger('skopax.investigator')

LEASE_SECONDS = 1200
STALE_AFTER_S = 3 * LEASE_SECONDS
LEASE_LOST = 'lease lost to another pod'
ROOM = 'investigator-room-'
BOARD_TEXT = 300
BOARD_RECENT = 10
SHUTDOWN = 'the pod stopped (a deploy or a restart) before the run ended'
DEAD_POD_S = 300
DEAD_POD = 'the pod running it stopped checking in'
JOB_PERIOD = 120
JOB_OFFSET = 30
REQUEST_TIMEOUT = 240.0
MAX_RETRIES = 2
ACTION_SCAN = 50
RESULT_LIMIT = 300
ASK_LINE = 100
URL_TAIL = re.compile(r'https://\S*$')
LIMIT_WINDOW_S = 3600
GATE_HOURLY_CAP = 60
SOURCE_OF = {'cards': 'pod', 'action': 'pod', 'ask': 'page', 'slack': 'slack', 'chat': 'chat', 'assess': 'assess', 'review': 'pod'}
MCP = 'mcp'
TRACE_NAMES = {'cards': 'card run', 'action': 'investigate click', 'ask': 'page question', 'slack': 'slack question', 'chat': 'chat answer', 'assess': 'deploy assessment', 'review': 'follow-up check'}
TRACE_OK = ('done', 'skipped')
HELD_S = 24 * 3600
HELD_PERIOD = 300
HELD_OFFSET = 45
STUCK_RELEASE_S = 600
REASON_LIMIT = 200
REFUSED_BY = 'Refused by the guard'
HELD_PUBLIC = ('until', 'released')
GUARD_PRIVATE = ('reason', 'category', 'first', 'fix', 'noted')
FIX_TEXT = 'A safety reviewer objected: {category}: {reason}. Rewrite the final answer to fix only that; do not call tools unless needed.'
CONTEXT_LINE = 200
DROPPED = 'dropped'
FIXED = 'fixed'
PROPOSALS = ('ticketProposal', 'cardAction', 'proposal')
USE_FIELDS = {'card': ('investigate_model', 'card'), 'slack': ('investigate_slack_model', 'slack'), 'chat': ('chat_model', 'chat')}


class ReleaseFailed(Exception):
    pass


class ReleaseLost(Exception):
    pass


class ReleaseTaken(ReleaseFailed):
    pass


def guard_reason(reason) -> str:
    return outward(str(reason or ''))[:REASON_LIMIT]


def merged(result: Result, again: Result) -> None:
    result.turns += again.turns
    result.usd += again.usd
    result.models += list(again.models)
    result.advisor_calls += again.advisor_calls
    result.advisor_errors += again.advisor_errors
    for k, n in (again.usage or {}).items():
        result.usage[k] = result.usage.get(k, 0) + n
    if again.error:
        result.error = ((result.error + '; ') if result.error else '') + again.error


def guard_category(category, verdict: str = '', failure=None) -> str:
    if failure:
        return 'other'
    if verdict == 'pass':
        return category if category in CATEGORIES else 'none'
    return category if category in CATEGORIES and category != 'none' else 'other'


MASK_TAIL_MIN = 4
HELD_CALL_ERROR = 'hidden while the answer is held'


def hidden(text: str, forms: tuple[str, ...]) -> str:
    if not isinstance(text, str) or not text:
        return text
    found = tuple(f for f in forms if f in text)
    if found:
        got = apply_mask(text, found)
        text = got[0] if got is not None else MASK_TEXT
    for f in forms:
        tail = next((n for n in range(len(f) - 1, MASK_TAIL_MIN - 1, -1) if text.endswith(f[:n])), 0)
        if tail:
            text = text[:-tail] + MASK_TEXT
    return text


def mask_record(toolbox, spans: tuple[str, ...]) -> None:
    forms = tuple(dict.fromkeys(f for s in spans for f in (s, json.dumps(s, ensure_ascii=False)[1:-1]) if f))
    for c in toolbox.calls:
        for field in ('input', 'error'):
            if isinstance(c.get(field), str):
                c[field] = hidden(c[field], forms)
    for w in toolbox.writes:
        if isinstance(w.get('summary'), str):
            w['summary'] = hidden(w['summary'], forms)
    for f in toolbox.flags:
        if isinstance(f, dict) and isinstance(f.get('excerpt'), str):
            f['excerpt'] = hidden(f['excerpt'], forms)


def stored_calls(calls: list[dict]) -> list[dict]:
    return [c | {'input': outward(c['input'])} if isinstance(c.get('input'), str) else c for c in calls]


def public_run(run: dict) -> dict:
    out = dict(run)
    if isinstance(out.get('held'), dict):
        out['held'] = {k: v for k, v in out['held'].items() if k in HELD_PUBLIC}
    if out.get('outcome') in HELD_OUTCOMES:
        out.pop('screenshots', None)
    if isinstance(out.get('guard'), dict):
        out['guard'] = {k: v for k, v in out['guard'].items() if k not in GUARD_PRIVATE}
    return out


def said(action: dict | None, key: str) -> str:
    with messages.speaking(((action or {}).get('slack') or {}).get('language')):
        return msg(key)


def via_mcp(action: dict | None) -> bool:
    return (action or {}).get('via') == MCP


def without_outcome(card: dict) -> dict:
    seen = card.get('seenBefore')
    return card | {'seenBefore': {k: v for k, v in seen.items() if k not in ('outcome', 'resolution', 'verdicts')}} if isinstance(seen, dict) else card


def scan_parts(cards: list[dict], base: str) -> tuple[str, str]:
    strict, soft = [], []
    for c in cards:
        if isinstance(c.get('seenBefore'), dict):
            soft.append(prompt.verdict_text(c['seenBefore']))
        c = without_outcome(c)
        seen = c.get('seenBefore')
        line = (seen.get('slack') or {}).get('line') if isinstance(seen, dict) else None
        pod = c.get('noteBy') == 'pod' and bool(c.get('note'))
        if isinstance(seen, dict) and 'slack' in seen:
            c = c | {'seenBefore': {k: v for k, v in seen.items() if k != 'slack'}}
        if pod:
            soft.append(str((c['note'].get('text') if isinstance(c['note'], dict) else c['note']) or ''))
            c = {k: v for k, v in c.items() if k != 'note'}
        if prompt.KNOWN_CAUSE in c:
            soft.append(str(c[prompt.KNOWN_CAUSE] or ''))
            c = {k: v for k, v in c.items() if k != prompt.KNOWN_CAUSE}
        soft.append(str(line or ''))
        if c.get('history'):
            soft.append('\n'.join(prompt.history_lines(c)[1:]))
            c = {k: v for k, v in c.items() if k != 'history'}
        strict.append(c)
    return prompt.cards_text(strict, base), '\n'.join(t for t in soft if t)


def clip(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    head = text[:limit]
    if not text[limit].isspace():
        head = URL_TAIL.sub('', head)
    return head.rstrip()


def with_verdicts(cards: list[dict], ruled: dict[str, list[dict]]) -> list[dict]:
    out = []
    for c in cards:
        seen = c.get('seenBefore')
        if isinstance(seen, dict):
            c = c | {'seenBefore': {k: v for k, v in seen.items() if k != 'verdicts'} | ({'verdicts': ruled[c['key']]} if ruled.get(c.get('key')) else {})}
        out.append(c)
    return out


class Investigator:
    mode = Setting('investigate_mode')
    provider = Setting('ai_provider')
    model = ModelSetting('investigate_model', 'card')
    effort = Setting('investigate_effort')
    cap = Setting('investigate_daily_usd')
    max_cards = Setting('investigate_max_cards')
    refire_hours = Setting('investigate_refire_hours', lambda h: max(1.0, float(h)))
    max_tool_calls = Setting('investigate_max_tool_calls')
    assess_max_tool_calls = Setting('investigate_assess_max_tool_calls')
    slack_model = ModelSetting('investigate_slack_model', 'slack')
    chat_model = ModelSetting('chat_model', 'chat')
    chat_effort_set = Setting('chat_effort')
    chat_effort_follows = Setting('chat_effort_follows')
    chat_max_tool_calls_set = Setting('chat_max_tool_calls')
    language = Setting('investigate_language')
    slack_effort = Setting('investigate_slack_effort')
    slack_max_tool_calls = Setting('investigate_slack_max_tool_calls')
    slack_ask_cap = Setting('slack_ask_cap')
    strict_tools = Setting('investigate_strict_tools')
    web_domains = Setting('investigate_web_domains')
    org_about = Setting('org_about')
    web_searches = Setting('investigate_web_searches')
    web_fetches = Setting('investigate_web_fetches')
    rooms = Setting('investigate_rooms', lambda n: max(1, int(n)))

    def __init__(self, store: Store, sources: Sources, effects: Effects, settings: Settings, client=None,
                 clock=time.time, base_url: str | None = None, talk=None, key: Callable[[], str] | None = None, keys: Mapping[str, Callable[[], str]] | None = None):
        self.store = store
        self.talk = talk
        self.sources = sources
        self.effects = effects
        self.notices = None
        self.settings = settings
        self.on: Callable[[], bool] = lambda: True
        self.pack: Callable[[], OrgPack | None] = lambda: None
        self.role_of: Callable[[dict | None], str | None] = lambda action: None
        self.guard = None
        self.admins: Callable[[], list[str]] = lambda: []
        self.clients = clients_for(settings, key, lambda k: anthropic.Anthropic(api_key=k, timeout=REQUEST_TIMEOUT, max_retries=MAX_RETRIES), REQUEST_TIMEOUT, MAX_RETRIES, client, keys)
        self.key = lambda: self.clients.key(route(self, 'investigate_model', 'card')[0])
        self.keyed = lambda: self.clients.keyed(route(self, 'investigate_model', 'card')[0])
        self.clock = clock
        self.base_url = settings.base_url if base_url is None else base_url
        self.owner = f'{socket.gethostname()}:{uuid.uuid4().hex[:8]}'
        self._thread: threading.Thread | None = None
        self._threads: list[threading.Thread] = []
        self._runs: dict[str, dict] = {}
        self._lock = threading.Lock()
        self._card_turn = threading.Lock()

    @property
    def box(self):
        return self.clients.box()

    @property
    def client(self):
        return self.clients.client

    @client.setter
    def client(self, value) -> None:
        self.clients.set_client(value)

    @property
    def enabled(self) -> bool:
        return self.mode != 'off' and self.keyed() and pricing.known(self.model)

    def _client(self, provider: str | None = None):
        return self.clients.get(provider or route(self, 'investigate_model', 'card')[0])

    def own_domains(self) -> tuple[str, ...]:
        if self.sources.own_sites is not None:
            return own_web_domains(self.sources.own_sites())
        return own_web_domains(self.settings.own_sites)

    @property
    def chat_effort(self) -> str:
        return self.slack_effort if self.chat_effort_follows else self.chat_effort_set

    @property
    def chat_max_tool_calls(self) -> int:
        return self.chat_max_tool_calls_set or self.slack_max_tool_calls

    def card_model(self, sevs) -> str:
        return self.card_route(sevs)[1]

    def card_route(self, sevs) -> tuple[str, str, str | None, str, str | None]:
        worst = severity.worst(sevs)
        use = 'critical' if worst == severity.CRIT else 'high' if worst == severity.HIGH else 'card'
        chosen = self.settings.investigate_model_critical if use == 'critical' else self.settings.investigate_model_high if use == 'high' else ''
        provider, model, fallback = route(self, 'investigate_model', 'card')
        if not chosen:
            return provider, model, fallback, 'card', None
        owner = provider_of_ref(chosen) or self.clients.default()
        if pricing.known(chosen) or '/' in chosen:
            return owner, chosen, None, use, None
        return provider, model, chosen, 'card', fallback

    def run_route(self, quick: bool, chat: bool, asked: bool, cards: list[dict]) -> tuple[str, str, str | None, str, str | None]:
        if quick:
            use = 'chat' if chat else 'slack'
            return (*self.use_route(use), use, None)
        if asked:
            return (*self.use_route('card'), 'card', None)
        return self.card_route(c.get('sev') for c in cards)

    def advisor_use(self, use: str, quick: bool, review: bool, asked: bool, cards: list[dict]) -> str | None:
        if review:
            return None
        if quick or asked:
            return use
        worst = severity.worst(c.get('sev') for c in cards)
        return 'critical' if worst == severity.CRIT else 'high' if worst == severity.HIGH else 'card'

    def use_route(self, use: str) -> tuple[str, str, str | None]:
        field, role = USE_FIELDS[use]
        return route(self, field, role)

    def overrides(self) -> dict:
        return {name: self.card_model({sev}) for name, sev, field in (('modelCritical', severity.CRIT, 'investigate_model_critical'), ('modelHigh', severity.HIGH, 'investigate_model_high'))
                if getattr(self.settings, field, '')}

    def info(self) -> dict:
        last = self.store.list_investigations(None, 1)
        return {'mode': self.mode, 'enabled': self.enabled, 'model': self.model, **self.overrides(), 'effort': self.effort, 'maxToolCalls': self.max_tool_calls, 'rooms': self.rooms,
                'slackModel': self.slack_model, 'slackEffort': self.slack_effort, 'slackMaxToolCalls': self.slack_max_tool_calls, 'chatEffort': self.chat_effort, 'chatMaxToolCalls': self.chat_max_tool_calls, 'stopped': self.store.investigator_stop(),
                'spentToday': round(self.store.spent(clock.day(self.clock())), 2), 'cap': self.cap, 'lastRun': last[0]['startedAt'] if last else None}

    def board(self, now: float) -> dict:
        halted = self._halted(clock.day(now))
        leases = self.store.leases_now()
        runs = self.store.running_investigations()
        shown = [r for r in runs if r.get('trigger') != 'chat']
        progress = self.store.tool_progress([r['id'] for r in shown])
        spend = self.store.run_spend([r['id'] for r in shown], min((str(r.get('startedAt') or '') for r in shown), default=''))
        runs = [r | {'toolCalls': 0, 'usd': spend.get(r['id'], 0.0)} | progress.get(r['id'], {}) if r.get('trigger') != 'chat' else r | {'toolCalls': 0} for r in runs]
        rooms = [{'id': f'{ROOM}{i}', 'name': f'Room {i + 1}', 'takes': 'questions, Investigate clicks and deploy assessments'} for i in range(self.rooms)]
        rooms.append({'id': LEASE_ID, 'name': 'Cards', 'takes': 'new and escalated cards, and the queue when the rooms are full'})
        placed = set()
        for room in rooms:
            lease = leases.get(room['id'])
            run = next((r for r in runs if r.get('room') == room['id']), None)
            room.update(pod=(lease['owner'].split(':')[0] if lease else run['pod'].split(':')[0] if run else None), until=lease['until'] if lease else None, run=run)
            if run:
                placed.add(run['id'])
        waiting = []
        for act in reversed(self.store.list_actions('requested', ACTION_SCAN)):
            if act.get('type') not in ('dive', 'ask', 'assess'):
                continue
            s = act.get('slack') or {}
            private = act.get('source') == 'chat'
            why = (off_text('investigator') if halted == 'off' else f'the investigator is {"stopped" if halted == "stopped" else "over its daily limit"}' if halted else 'the investigator is off' if not self.enabled
                   else 'behind an earlier question in the same Slack thread' if self._waits(act) else 'next for a free room')
            waiting.append({'id': act['id'], 'type': act['type'], 'source': act.get('source'), 'by': None if private else act.get('requestedBy'), 'at': act.get('requestedAt'),
                            'text': 'Chat message (private)' if private else clip(prompt.flat(str(act.get('text') or act.get('label') or '')), BOARD_TEXT),
                            'cards': [] if private else (act.get('cards') or ([act['key']] if act['type'] == 'dive' else []))[:5],
                            'thread': s.get('permalink'), 'why': why} | ({'via': MCP} if via_mcp(act) else {}))
        recent = [r for r in self.store.list_investigations(None, BOARD_RECENT + len(runs), page=True) if r.get('outcome') != 'running'][:BOARD_RECENT]
        errors = self.store.run_errors([r['id'] for r in recent if r.get('outcome') != 'done'])
        recent = [r | ({'error': errors[r['id']]} if r['id'] in errors else {}) for r in recent]
        recent = [r | ({'traceUrl': link} if (link := tracing.trace_link(r.get('trace'))) else {}) for r in recent]
        since = epoch_iso(now - LIMIT_WINDOW_S)
        limits = {'slackAsks': self.store.count_actions('ask', since, source='slack'), 'slackAskCap': self.slack_ask_cap, 'gateCalls': self.store.count_gate_calls(since), 'gateCap': GATE_HOURLY_CAP,
                  'spentToday': round(self.store.spent(clock.day(now)), 2), 'cap': self.cap, 'askers': self.store.askers(since)}
        overrides = self.overrides()
        return {'at': epoch_iso(now), 'mode': self.mode, 'enabled': self.enabled, 'halted': halted, 'problem': self._problem(halted), 'rooms': rooms, 'unplaced': [r for r in runs if r['id'] not in placed],
                'waiting': waiting, 'recent': recent, 'limits': limits} | ({'model': self.model, 'modelOverrides': overrides} if overrides else {})

    def _problem(self, halted: str | None) -> str | None:
        if self.mode == 'off' or halted == 'off':
            return None
        if not self.keyed():
            return 'key'
        return None if pricing.known(self.model) else 'model'

    def available(self) -> bool:
        return self.enabled and self._halted(clock.day(self.clock())) is None

    def halt(self, day: str, lease: tuple[str, str] | None = None) -> str | tuple[str, str] | None:
        lease_id, owner = lease or (LEASE_ID, self.owner)
        if not self.store.take_lease(owner, LEASE_SECONDS, lease_id):
            return 'failed', LEASE_LOST
        return self._limit(day)

    def limits(self, now: float) -> str:
        since = epoch_iso(now - LIMIT_WINDOW_S)
        talk = ''
        if self.talk is not None:
            rules = self.talk.rules()
            talk = prompt.small_talk_use(self.store.count_small_talk(since), rules['hourlyCap'], self.talk.enabled and rules['enabled'])
        return prompt.limits_line(self.store.count_actions('ask', since, source='slack'), self.slack_ask_cap,
                                  self.store.count_gate_calls(since), GATE_HOURLY_CAP, self.store.spent(clock.day(now)), self.cap, talk)

    def _halted(self, day: str) -> str | None:
        if not self.on():
            return 'off'
        return self._limit(day)

    def _limit(self, day: str) -> str | None:
        if self.store.investigator_stop():
            return 'stopped'
        if self.store.spent(day) >= self.cap:
            return 'capped'
        return None

    def why_halted(self, now: float) -> str | None:
        return self._halted(clock.day(now))

    def _slack_reply(self, action: dict, outcome: str, recap: str) -> dict | None:
        if self.effects.slack_reply is None or action.get('type') != 'ask' or not (action.get('slack') or {}).get('channel'):
            return None
        try:
            return self.effects.slack_reply(action, outcome, recap)
        except Exception as e:
            log.warning('slack reply for action %s failed: %s', action.get('id'), error_kind(e))
            return None

    def _images(self, action: dict) -> tuple[list[dict], str]:
        blocks, shown, failed = [], [], []
        for image in action.get('images') or []:
            name = str(image.get('name') or 'image')
            try:
                if self.effects.slack_file is None:
                    raise Refusal('this server has no Slack file reader')
                data, mime = self.effects.slack_file(str(image.get('url') or ''))
            except Exception as e:
                failed.append(f'{name}: {text_of(e)[:160]}')
                continue
            blocks.append({'type': 'image', 'source': {'type': 'base64', 'media_type': mime, 'data': base64.b64encode(data).decode()}})
            shown.append(name)
        return blocks, prompt.images_note(shown, failed, int(action.get('imagesSkipped') or 0))

    def kick(self, now: float) -> None:
        with self._lock:
            self._threads = [t for t in self._threads if t.is_alive()]
            if len(self._threads) > self.rooms:
                return
            thread = threading.Thread(target=self._safe_run, args=(now,), name='investigator', daemon=True)
            thread.start()
            self._thread = thread
            self._threads.append(thread)

    def _safe_run(self, now: float) -> None:
        try:
            if self.run_once(now) is not None:
                self.kick(self.clock())
        except Exception as e:
            log.error('investigator run failed: %s', error_kind(e))

    def _room(self) -> tuple[str, str] | None:
        owner = f'{self.owner}:{uuid.uuid4().hex[:6]}'
        for i in range(self.rooms):
            if self.store.take_lease(owner, LEASE_SECONDS, f'{ROOM}{i}'):
                return f'{ROOM}{i}', owner
        return None

    def reap_dead(self, now: float) -> int:
        pods = self.store.dead_pods(now - DEAD_POD_S) - {self.owner}
        if not pods:
            return 0
        runs = self.store.abandon_runs_of(pods, DEAD_POD)
        leases = self.store.drop_leases_of(pods)
        if runs or leases:
            log.warning('freed %d runs and %d leases of stopped pods %s', len(runs), leases, ', '.join(sorted(pods)))
            self.store.add_activity('reap', 'investigator', f"reaped {len(runs)} run{'' if len(runs) == 1 else 's'} of a stopped pod · {leases} lease{'' if leases == 1 else 's'} freed")
        return len(runs)

    def run_once(self, now: float) -> dict | None:
        if not self.enabled:
            return None
        asks = self._fail_stale_asks(epoch_iso(now - STALE_AFTER_S))
        if asks:
            log.warning('failed %d stale slack asks', asks)
            self.store.add_activity('reap', 'investigator', f"reaped {asks} stale Slack ask{'' if asks == 1 else 's'}")
        if self._halted(clock.day(now)):
            return None
        try:
            self.reap_dead(now)
        except Exception as e:
            log.warning('dead pod check failed: %s', error_kind(e))
        room = self._room()
        if room is not None:
            try:
                work = self.pick_action()
                if work:
                    return self.investigate(*work, now, room)
            finally:
                self.store.drop_lease(room[1], room[0])
        if not self._card_turn.acquire(blocking=False):
            return None
        try:
            return self._card_run(now)
        finally:
            self._card_turn.release()

    def _card_run(self, now: float) -> dict | None:
        if not self.store.take_lease(self.owner, LEASE_SECONDS):
            return None
        try:
            self.reap(now)
            work = self.pick(now)
            return self.investigate(*work, now) if work else None
        finally:
            self.store.drop_lease(self.owner)

    def reap(self, now: float) -> tuple[int, int, int]:
        before = epoch_iso(now - STALE_AFTER_S)
        claims = self.store.release_stale_claims(before)
        runs = self.store.abandon_stale_runs(before)
        acts = self.store.fail_stale_actions(before, ('dive', 'ask', followup.REVIEW)) + self._fail_stale_asks(before)
        if claims or runs or acts:
            log.warning('reaped %d stale claims, %d runs, %d actions', claims, runs, acts)
            self.store.add_activity('reap', 'investigator', f'reaped {claims} stale claims · {runs} runs · {acts} actions')
        return claims, runs, acts

    def _fail_stale_asks(self, before: str) -> int:
        failed = self.store.fail_stale_slack_asks(before)
        for act in failed:
            self._slack_reply(act, 'failed', '')
        return len(failed)

    def pick(self, now: float) -> tuple[list[dict], dict | None] | None:
        work = self.pick_action()
        if work:
            return work
        cooldown, mode = self.refire_hours * 3600, self.mode
        runs = self.store.recent_investigations(mode, epoch_iso(now - select.run_window(cooldown)))
        cards = select.eligible(self.store.load(), mode, runs, now, self.max_cards, cooldown)
        return (cards, None) if cards else None

    def pick_action(self) -> tuple[list[dict], dict] | None:
        if self.mode in ('live', 'shadow'):
            for act in reversed(self.store.list_actions('requested', ACTION_SCAN)):
                if act.get('type') not in ('dive', 'ask', 'assess', followup.REVIEW) or self._waits(act):
                    continue
                try:
                    self.store.update_action(act['id'], 'processing', None, 'requested')
                except ActionMoved:
                    continue
                try:
                    if act['type'] == 'ask':
                        cards = [self.store.anomaly(k) for k in act.get('cards') or []]
                        item = next((c for c in cards if information.is_item(c)), None)
                        if item is not None:
                            log.info('ask %s dropped: %s is information, not a card', act['id'], item['key'])
                            self._slack_reply(self.store.update_action(act['id'], 'failed', msg('cards.information_item', key=item['key']), 'processing'), 'failed', '')
                            continue
                        return cards, self._caught_up(act)
                    if act['type'] == 'assess':
                        return [], act
                    card = self.store.anomaly(act['key'])
                    if information.is_item(card):
                        log.info('investigate on %s dropped: it is information, not a card', act['key'])
                        self._slack_reply(self.store.update_action(act['id'], 'failed', msg('cards.information_item', key=act['key']), 'processing'), 'failed', '')
                        continue
                    return [card], act
                except NotFound:
                    self._slack_reply(self.store.update_action(act['id'], 'failed', 'no card with that key any more', 'processing'), 'failed', '')
                except Exception as e:
                    self._slack_reply(self.store.update_action(act['id'], 'failed', error_kind(e)[:RESULT_LIMIT], 'processing'), 'failed', '')
                    raise
        return None

    @staticmethod
    def _thread_of(act: dict) -> tuple[str, str] | None:
        s = act.get('slack') or {}
        if act.get('type') != 'ask' or act.get('source') != 'slack' or not s.get('channel') or not s.get('threadTs'):
            return None
        return s['channel'], s['threadTs']

    def _waits(self, act: dict) -> bool:
        where = self._thread_of(act)
        if where is None:
            return False
        first = self.store.open_thread_ask(*where)
        return first is not None and first['id'] != act['id']

    def _caught_up(self, act: dict) -> dict:
        where = self._thread_of(act)
        said = self.store.thread_answers(*where, act.get('requestedAt') or '') if where else []
        return act | {'thread': (act.get('thread') or []) + [{'who': 'bot', 'text': t} for t in said]} if said else act

    def shutdown(self) -> None:
        with self._lock:
            runs = list(self._runs.items())
        for run_id, cur in runs:
            try:
                self.store.update_investigation(run_id, {'outcome': 'abandoned', 'endedAt': epoch_iso(self.clock()), 'error': SHUTDOWN})
                for k in list(cur['claimed']):
                    self.store.release_card(k, run_id)
                action = cur['action']
                again = action is not None and self.store.requeue_action(action['id'])
                self.store.add_activity('run', 'investigator', f"run abandoned · {SHUTDOWN}" + (' · the request goes back to the queue' if again else ''), runId=run_id, outcome='abandoned')
            except Exception as e:
                log.warning('run %s not closed at shutdown: %s', run_id, error_kind(e))
            finally:
                self.store.drop_lease(cur['lease'][1], cur['lease'][0])
        self.store.drop_lease(self.owner)

    def investigate(self, cards: list[dict], action: dict | None, now: float, lease: tuple[str, str] | None = None) -> dict | None:
        if not (self.clients.injected or self.keyed()):
            if action is not None:
                self.store.requeue_action(action['id'])
            return None
        day, run_id, mode = clock.day(now), new_id(), self.mode
        keys = [c['key'] for c in cards]
        ask = str(action.get('text') or '') if action and action.get('type') == 'ask' else ''
        slack_ask = bool(ask) and action.get('source') == 'slack'
        chat_ask = bool(ask) and action.get('source') == 'chat'
        quick = slack_ask or chat_ask
        report_id = str(action.get('reportId') or '') if action and action.get('type') == 'assess' else ''
        review = str(action.get('flavor') or '') if action and action.get('type') == followup.REVIEW else ''
        claimed: list[str] = []
        record: dict | None = None
        toolbox = None
        result = None
        crash = None
        guarded = None
        trigger = 'review' if review else 'assess' if report_id else 'slack' if slack_ask else 'chat' if chat_ask else 'ask' if ask else 'action' if action else 'cards'
        trace = tracing.Handle(None, None, None)
        with self._lock:
            self._runs[run_id] = {'claimed': claimed, 'action': action, 'lease': lease or (LEASE_ID, self.owner)}
        try:
            trace = self._trace(run_id, trigger, keys, mode, action, ask, self.run_route(quick or bool(review), chat_ask, bool(ask or report_id), cards)[0])
            if mode == 'live' and action is None:
                for k in keys:
                    if self.store.claim_card(k, run_id):
                        claimed.append(k)
                keys = list(claimed)
                cards = [c for c in cards if c['key'] in keys]
            report = self.store.deploy_report(report_id) if report_id else None
            cards = self._with_history(self._with_verdicts(cards))
            provider, used_model, fallback, use, card_fallback = self.run_route(quick or bool(review), chat_ask, bool(ask or report), cards)
            quick_model = model = used_model
            record = {'id': run_id, 'mode': mode, 'trigger': trigger, 'actionId': action['id'] if action else None,
                      'keys': keys, 'pod': self.owner, 'room': (lease or (LEASE_ID, self.owner))[0], 'startedAt': epoch_iso(now), 'model': used_model, 'effort': (self.chat_effort if chat_ask else self.slack_effort) if quick else self.slack_effort if review else self.effort, 'outcome': 'running'}
            if fallback:
                record['fallbackFrom'] = fallback
            if card_fallback:
                record['cardFallbackFrom'] = card_fallback
            if ask:
                record['ask'] = {'text': ask, 'by': action.get('requestedBy')}
                if slack_ask:
                    record['ask'] |= {'source': 'slack', 'thread': (action.get('slack') or {}).get('permalink')}
                if chat_ask:
                    record['ask'] |= {'source': 'chat', 'chat': (action.get('chat') or {}).get('id')}
            if report:
                record['report'] = {'id': report_id, 'summary': report.get('summary')}
            if review:
                record['review'] = review
            mcp = via_mcp(action)
            if mcp:
                record['source'] = MCP
            self.store.add_investigation(record)
            self.store.add_activity('run', 'investigator', f"{'follow-up check' if review == followup.VERIFY else 'settle review'} \u00b7 {', '.join(keys)}" if review else f"assessing deploy report · {report.get('summary')}" if report else 'chat message · private' + (' · via MCP' if mcp else '') if chat_ask else f"{'slack ' if slack_ask else ''}ask by {action.get('requestedBy')} · {prompt.flat(ask)[:ASK_LINE]}" if ask else f"run started · {mode} · {', '.join(keys)}" + (f" · via MCP by {action.get('requestedBy')}" if mcp else ''), runId=run_id)
            guarded = self._guard_start(run_id, action, ask, review, report, cards)
            if guarded is not None and guarded['refused']:
                result = Result(outcome='done', recap=guarded['refused'], error=REFUSED_BY)
            elif keys or ask or report:
                client, result = self._built(provider)
                caps = self.clients.capabilities(provider, client) if client is not None else self.clients.capabilities(provider)
                if client is not None and 'tools' not in caps:
                    client, result = None, Result(outcome='failed', error=msg('ai.no_tools'))
                searching = 'web_search' in caps
                role = self.role_of(action)
                gate = asked_gate(Run(read_only=quick, ask=bool(ask), report=bool(report_id), chat=chat_ask, keys=bool(keys), asker=False, shadow=mode == 'shadow', role=role, review=review or None))
                parts = self.sources.plugin_tools.knowledge_parts if self.sources.plugin_tools is not None else None
                sections, index = (parts(() if chat_ask else cards, allowed=gate) if gate is not None else parts(() if chat_ask else cards)) if parts is not None else ([], [])
                pack = self.pack()
                readable = [k for _, k in index] if pack is None or pack.plugin_sections else []
                toolbox = Toolbox(self.sources, self.store, self.effects, keys, mode, now, self.base_url, run_id=run_id, ask=bool(ask), report_id=report_id or None, read_only=quick,
                                  strict=self.strict_tools, asker=action.get('requestedBy') if ask and not quick else None, chat=chat_ask, language=self.language,
                                  web=web_from(self.web_domains, self.web_searches if searching else 0, self.web_fetches if searching and 'web_fetch' in caps and getattr(wrap(client), 'provider', provider) != pricing.OPENAI else 0, self.own_domains()), settings=self.settings, role=role, knowledge=readable,
                                  review=review or None)
                toolbox.hold_writes = bool(guarded and guarded['answers'])
                if review:
                    config = RunConfig(quick_model, self.slack_effort, self.slack_max_tool_calls, capabilities=caps)
                    brief = prompt.review_brief(cards[0], review, self.base_url, config.max_tool_calls) + prompt.language_note(self.language)
                    toolbox.flags += scan('brief', prompt.review_soft(cards[0]), skip=SOFT + REQUESTS)
                elif quick:
                    config = RunConfig(quick_model, self.chat_effort if chat_ask else self.slack_effort, self.chat_max_tool_calls if chat_ask else self.slack_max_tool_calls, cache_ttl='1h', role='chat' if chat_ask else 'slack', capabilities=caps)
                    blocks, images = (self._images(action) if 'images' in caps else ([], IMAGES_OFF)) if action.get('images') else ([], '')
                    if images == IMAGES_OFF:
                        self.store.add_activity('run', 'investigator', IMAGES_OFF, runId=run_id)
                    brief = (prompt.slack_brief(cards, action, self.base_url, config.max_tool_calls, self.limits(now), images) if slack_ask
                             else prompt.chat_brief(cards, action, self.base_url, config.max_tool_calls, images))
                    if blocks:
                        record['images'] = len(blocks)
                    asker = 'slack' if slack_ask else 'chat'
                    toolbox.flags += scan(asker, prompt.flat(ask) + '\n' + prompt.thread_text(action.get('thread') or []), skip=REQUESTS)
                    toolbox.flags += asked(asker, '\n'.join([prompt.flat(ask)] + [str(t.get('text') or '') for t in action.get('thread') or [] if t.get('who', 'person') != 'bot']))
                else:
                    config = RunConfig(model, self.effort, self.assess_max_tool_calls if report else self.max_tool_calls, capabilities=caps)
                    brief = prompt.brief(cards, {**action, 'report': report} if report else action, self.base_url, config.max_tool_calls, None if ask or report else self._announced(cards, now)) + ('' if ask else prompt.language_note(self.language))
                plan = None
                if client is not None and (wanted := self.advisor_use(use, quick, bool(review), bool(ask or report), cards)) is not None:
                    plan = advisor.resolve(self.settings, wanted, provider, caps, config.model)[0]
                    config = dataclasses.replace(config, advisor=plan)
                if ask and not review:
                    brief += prompt.questions_note(toolbox.not_in_questions())
                if report:
                    toolbox.flags += scan('brief', brief, skip=REQUESTS)
                else:
                    strict, soft = scan_parts(cards, self.base_url)
                    toolbox.flags += scan('brief', strict, skip=REQUESTS) + scan('brief', soft, skip=SOFT + REQUESTS)
                if ask and not quick:
                    toolbox.flags += asked('page', prompt.flat(ask))
                listed = {(f.get('tool'), f.get('phrase')) for f in toolbox.flags}
                toolbox.flags += [f for f in (action or {}).get('askFlags') or [] if isinstance(f, dict) and (f.get('tool'), f.get('phrase')) not in listed]
                tag = {'source': MCP if mcp else SOURCE_OF[record['trigger']], 'runId': run_id, 'actionId': action['id'] if action else None,
                       'keys': [] if mcp and chat_ask else keys, 'by': (action or {}).get('requestedBy') or pricing.POD_BY, 'use': use} | ({'fallbackFrom': fallback} if fallback else {})
                if client is not None:
                    extra = toolbox.start_bridge().knowledge()
                    runner = Runner(client, self.store, config, tag=tag)
                    first = [{'type': 'text', 'text': brief}] + blocks if quick and blocks else brief
                    system = prompt.slack_system_prompt(sections, pack, extra, index, self.org_about) if quick else prompt.system_prompt(sections, pack, extra, index, self.org_about)
                    system += advisor.NUDGE if plan is not None else ''
                    result = runner.run(toolbox, system, first, day, lambda: self.halt(day, lease))
                    if guarded is not None and guarded.get('fixing') and not guarded.get('watch') and result.outcome == 'done':
                        guarded['retry'] = lambda text: runner.again(text, lambda: self.halt(day, lease))
        except BaseException as e:
            crash = e
            log.error('investigation %s crashed: %s', run_id, error_kind(e))
            raise
        finally:
            try:
                with self._lock:
                    self._runs.pop(run_id, None)
                done = self._finish(run_id, claimed, action, record is not None, toolbox, result, crash, mode, trace.trace, guarded)
            finally:
                tracing.end(trace)
        log.info('investigation %s (%s, %s): %s, %d turns, %d tool calls, $%.2f', run_id, mode, ', '.join(keys), done['outcome'], done['turns'],
                 len(toolbox.calls) if toolbox else 0, done['usd'])
        return record | done

    def _built(self, provider: str | None = None) -> tuple[object | None, Result | None]:
        label = label_of(provider or route(self, 'investigate_model', 'card')[0])
        try:
            client = self._client(provider)
        except Exception as e:
            log.warning('the %s client was not built: %s', label, error_kind(e))
            return None, Result(outcome='failed', error=f'the {label} client was not built: {error_kind(e)}')
        if client is not None:
            return client, None
        chosen = provider or route(self, 'investigate_model', 'card')[0]
        if chosen != self.clients.default() and not self.clients.keyed(chosen):
            return None, Result(outcome='failed', error=msg('ai.use_held', owner=label))
        return None, Result(outcome='failed', error=f'no {label} API key is set')

    @property
    def label(self) -> str:
        return label_of(route(self, 'investigate_model', 'card')[0])

    def _with_verdicts(self, cards: list[dict]) -> list[dict]:
        seen = [c for c in cards if isinstance(c.get('seenBefore'), dict)]
        return with_verdicts(cards, self.store.history_verdicts(seen) if seen else {})

    def _with_history(self, cards: list[dict]) -> list[dict]:
        out = []
        for c in cards:
            rows = self.store.card_history_tail(c, (c.get('seenBefore') or {}).get('verdicts')) if c.get('key') else []
            out.append(c | {'history': rows} if rows else c)
        return out

    def _announced(self, cards: list[dict], now: float) -> dict[str, dict]:
        others = self.store.load().get('anomalies') or []
        return {c['key']: twin for c in cards if (twin := select.announced(c, others, now))}

    def _guard_start(self, run_id: str, action: dict | None, ask: str, review: str, report: dict | None, cards: list[dict]) -> dict | None:
        watch = self.guard
        if watch is None:
            return None
        questions = bool(ask) and bool(watch.checks_questions)
        answers = bool(watch.checks_answers)
        if not (questions or answers):
            return None
        watching = getattr(watch, 'watching', False) is True
        out = {'questions': None, 'answers': answers, 'question': self._guard_question(ask, report, cards), 'context': self._guard_context(cards), 'ms': 0, 'model': '', 'reason': '',
               'category': 'none', 'refused': None, 'watch': watching, 'fixing': getattr(watch, 'fixing', False) is True, 'retry': None}
        if not questions:
            return out
        try:
            got = watch.question(ask, self.role_of(action) or '', run_id)
            verdict, failure, reason, ms, model, category = got.verdict, got.failure, got.reason, got.ms, got.model, getattr(got, 'category', 'other')
        except Exception as e:
            log.warning('the question check of run %s failed: %s', run_id, error_kind(e))
            verdict, failure, reason, ms, model, category = 'refuse', 'api', '', 0, '', 'other'
        out.update(ms=int(ms or 0), model=str(model or ''), reason=guard_reason(reason), category=guard_category(category, verdict, failure))
        if failure:
            out.update(questions='failed', refused=None if watching else said(action, 'slackevents.guard.unavailable'))
        elif verdict == 'pass':
            out['questions'] = 'pass'
        else:
            out.update(questions='refuse', refused=None if watching else said(action, 'slackevents.guard.refused'))
        return out

    @staticmethod
    def _guard_context(cards: list[dict]) -> str:
        lines = []
        for c in cards:
            num = f"#{c['num']} " if c.get('num') else ''
            line = outward(f"{num}{prompt.flat(c.get('svc') or '-')} / {prompt.flat(c.get('label') or '?')}, {prompt.flat(c.get('sev') or '?')}, {prompt.flat(c.get('status') or '?')}")[:CONTEXT_LINE]
            lines.append(line)
        return '\n'.join(lines)

    @staticmethod
    def _guard_question(ask: str, report: dict | None, cards: list[dict]) -> str:
        if ask:
            return ask
        if report:
            return str(report.get('summary') or '')
        return '\n'.join(f"{c.get('svc') or '-'} / {c.get('label') or '?'}: {c.get('what') or c.get('sig') or c.get('key') or ''}" for c in cards)

    def _check_answer(self, run_id: str, toolbox, result: Result, guarded: dict | None, proposed: dict,
                      renew: Callable[[], dict] | None = None) -> tuple[dict | None, dict]:
        if guarded is None:
            return None, proposed
        out = {'questions': guarded['questions'], 'answers': None, 'masked': 0, 'ms': guarded['ms'], 'model': guarded['model'], 'reason': guarded['reason'],
               'category': guarded.get('category') or 'none'} | ({'watch': True} if guarded.get('watch') else {})
        if guarded['refused']:
            return out | {'answers': 'not run'}, proposed
        queued = toolbox is not None and bool(toolbox.held)
        if not guarded['answers'] or not (result.recap or queued or proposed):
            return out, proposed
        if guarded.get('watch'):
            verdict, _, _, _ = self._judged(run_id, toolbox, result, guarded, proposed, out)
            return out | {'answers': verdict}, proposed
        verdict, proposed = self._settle(run_id, toolbox, result, guarded, proposed, out)
        if verdict in ('pass', 'mask') or not guarded.get('fixing'):
            return out | {'answers': verdict}, proposed
        first = {'answers': verdict, 'category': out['category'], 'reason': out['reason']}
        retry = guarded.get('retry')
        if verdict == 'failed' or retry is None:
            return out | {'answers': DROPPED, 'first': first, 'outcome': DROPPED}, {}
        self._clear_attempt(toolbox)
        try:
            again = retry(FIX_TEXT.format(category=first['category'], reason=first['reason'] or 'no reason given'))
        except Exception as e:
            log.warning('the fix turn of run %s failed: %s', run_id, error_kind(e))
            again = None
        if again is not None:
            merged(result, again)
        if again is None or again.outcome != 'done' or not str(again.recap or '').strip():
            return out | {'answers': DROPPED, 'first': first, 'outcome': DROPPED}, {}
        result.recap = outward(again.recap)
        proposed = renew() if renew is not None else {}
        out.update(reason='', category='none')
        verdict, proposed = self._settle(run_id, toolbox, result, guarded, proposed, out)
        fix = {'answers': verdict, 'category': out['category'], 'reason': out['reason']}
        out.update(reason=first['reason'], category=first['category'])
        if verdict in ('pass', 'mask'):
            return out | {'answers': verdict, 'first': first, 'fix': fix, 'outcome': FIXED}, proposed
        return out | {'answers': DROPPED, 'first': first, 'fix': fix, 'outcome': DROPPED}, {}

    @staticmethod
    def _clear_attempt(toolbox) -> None:
        if toolbox is None:
            return
        toolbox.held.clear()
        toolbox.writes[:] = [w for w in toolbox.writes if not w.get('held')]
        toolbox.proposal = toolbox.plugin_proposal = toolbox.card_action = None

    def _judged(self, run_id: str, toolbox, result: Result, guarded: dict, proposed: dict, out: dict) -> tuple[str, tuple[str, ...], str, bool]:
        queued = toolbox is not None and bool(toolbox.held)
        offers = '\n'.join(f'{k}: {t}' for k, v in proposed.items() for t in leaves(v))
        whole = result.recap + (f'\n\nProposals:\n{offers}' if offers else '') + (f'\n\nCard writes waiting for this check:\n{toolbox.held_text()}' if queued else '')
        try:
            digest = toolbox.digest() if toolbox is not None else ''
            got = self.guard.answer(guarded['question'], whole, digest, run_id, context=guarded.get('context') or '')
            verdict, failure, spans, reason, ms, model = got.verdict, got.failure, tuple(s for s in got.spans or () if isinstance(s, str) and s), got.reason, got.ms, got.model
            category = getattr(got, 'category', 'other')
        except Exception as e:
            log.warning('the answer check of run %s failed: %s', run_id, error_kind(e))
            verdict, failure, spans, reason, ms, model, category = 'hold', 'api', (), '', 0, '', 'other'
        out.update(ms=out['ms'] + int(ms or 0), model=str(model or out['model'] or ''), reason=guard_reason(reason) or out['reason'], category=guard_category(category, verdict, failure))
        if failure:
            return 'failed', (), whole, queued
        return (verdict if verdict in ('pass', 'mask') else 'hold'), spans, whole, queued

    def _settle(self, run_id: str, toolbox, result: Result, guarded: dict, proposed: dict, out: dict) -> tuple[str, dict]:
        verdict, spans, whole, queued = self._judged(run_id, toolbox, result, guarded, proposed, out)
        if verdict != 'mask':
            return verdict, proposed
        found = [s for s in spans if s in whole]
        parts = [result.recap] + leaves(proposed) + ([t for w in toolbox.held for t in leaves(w.get('args')) + [str(w.get('summary') or '')]] if queued else [])
        total = apply_mask(whole, tuple(found))
        if total is None or any(not any(s in p for p in parts) for s in found) or any(apply_mask(p, tuple(found)) is None for p in parts if p):
            return 'hold', proposed

        def mask(text: str) -> str:
            return apply_mask(text, tuple(found))[0] if text else text

        result.recap = mask(result.recap)
        if toolbox is not None:
            mask_record(toolbox, tuple(found))
        if queued:
            toolbox.mask_held(mask)
        out['masked'] = total[1]
        return 'mask', masked_leaves(proposed, mask)

    def _proposals(self, toolbox, action: dict | None, outcome: str) -> dict:
        proposed: dict = {}
        if toolbox is not None and toolbox.proposal:
            proposed['ticketProposal'] = toolbox.proposal
        if toolbox is not None and toolbox.plugin_proposal and outcome == 'done':
            proposed['proposal'] = toolbox.plugin_proposal
        call = toolbox.card_action if toolbox is not None else None
        if outcome == 'done':
            call = self._with_ticket(call, action)
        if call:
            proposed['cardAction'] = call
        return proposed

    def _finish(self, run_id: str, claimed: list[str], action: dict | None, recorded: bool, toolbox, result, crash: BaseException | None, mode: str,
                trace: tracing.Trace | None = None, guarded: dict | None = None) -> dict:
        ended = epoch_iso(self.clock())
        if result is None:
            outcome, error = ('failed', error_kind(crash)) if crash is not None else ('skipped', None)
            result = Result(outcome=outcome, error=error)
        result.recap = outward(result.recap or '')
        proposed = self._proposals(toolbox, action, result.outcome)
        checked, proposed = self._check_answer(run_id, toolbox, result, guarded, proposed, lambda: self._proposals(toolbox, action, result.outcome))
        outcome, error = result.outcome, result.error
        held = checked is not None and not checked.get('watch') and checked['answers'] in ('hold', 'failed')
        dropped = checked is not None and checked['answers'] == DROPPED
        if dropped:
            self._clear_attempt(toolbox)
        if checked is not None and not held and toolbox is not None and toolbox.held:
            toolbox.replay(toolbox.held)
            toolbox.writes[:] = [w for w in toolbox.writes if not w.get('held')]
        done = {'outcome': outcome, 'endedAt': ended, 'turns': result.turns, 'models': result.models, 'usage': result.usage, 'usd': round(result.usd, 4),
                'advisorCalls': result.advisor_calls, 'advisorErrors': result.advisor_errors, 'toolCalls': stored_calls(toolbox.calls) if toolbox else [], 'writes': toolbox.writes if toolbox else [], 'flags': toolbox.flags if toolbox else [],
                'recap': result.recap, 'error': error}
        if checked is not None:
            done['guard'] = {k: v for k, v in checked.items() if not (k == 'category' and v == 'none')} | ({'noted': True} if checked.get('category') not in (None, 'none') else {})
        done |= {k: proposed[k] for k in PROPOSALS if k in proposed}
        if toolbox is not None and toolbox.shots and not (checked is not None and checked.get('outcome') == FIXED):
            done['screenshots'] = toolbox.shots
        if held:
            done = self._held(done, action, toolbox, result.recap)
            outcome = done['outcome']
        if dropped:
            done = self._dropped(done, action)
            result.recap = done['recap']
        if trace is not None:
            self._traced(trace, outcome, error, done['recap'])
        try:
            if recorded:
                self.store.update_investigation(run_id, done)
                self.store.add_activity('run', 'investigator', f"run {outcome} · {done['turns']} turns · {len(done['toolCalls'])} tool calls · ${done['usd']:.2f}" + (f" · {error}" if error else ''), runId=run_id, outcome=outcome,
                                        **({'screenshots': done['screenshots']} if done.get('screenshots') and not held else {}))
        finally:
            try:
                for k in claimed:
                    self.store.release_card(k, run_id)
            finally:
                if action and held:
                    self._hold_action(run_id, recorded, action, done['recap'])
                elif action:
                    ok = outcome == 'done'
                    line = (result.recap.splitlines() or ['done'])[0] if ok else f"{outcome}: {error or 'see the run record'}"
                    if ok and mode == 'shadow':
                        line = 'shadow run, nothing written: ' + line
                    if not self._close_action(action, 'done' if ok else 'failed', line[:RESULT_LIMIT]):
                        self._answer_slack(run_id, recorded, action, outcome, result.recap, {k: done[k] for k in ('ticketProposal', 'cardAction') if k in done}, done.get('proposal'),
                                           [s['id'] for s in done.get('screenshots') or []])
                    if ok and recorded and not (guarded and guarded['refused']):
                        self._remember_ask(run_id, action, done)
                    if action.get('type') == 'ask' and action.get('source') == 'chat':
                        self.store.add_activity('ask', 'investigator', f"chat {'answer' if ok else outcome} · private" + (' · via MCP' if via_mcp(action) else ''), runId=run_id, outcome=outcome)
                    elif action.get('type') == 'ask':
                        self.store.add_activity('ask', 'investigator', f"{'answer' if ok else outcome} for {action.get('requestedBy')} · {clip(prompt.flat(action.get('text') or ''), ASK_LINE)} → {clip(line, ASK_LINE)}", runId=run_id, outcome=outcome)
                if action and action.get('type') == 'assess' and action.get('reportId'):
                    self._close_assessment(str(action['reportId']), run_id, outcome == 'done', done, toolbox, held)
                if recorded:
                    self._notify(claimed, action, outcome, done['recap'])
                if recorded and held:
                    self._notify_held(run_id)
        return done

    def _held(self, done: dict, action: dict | None, toolbox, text: str) -> dict:
        asked = bool(action) and action.get('type') == 'ask'
        kept = {'text': text, 'writes': list(toolbox.held) if toolbox is not None else [], 'until': epoch_iso(self.clock() + HELD_S)}
        proposed = {k: done.pop(k) for k in PROPOSALS if k in done}
        if proposed:
            kept['proposed'] = proposed
        calls = [c | {'input': ''} | ({'error': HELD_CALL_ERROR} if c.get('error') else {}) for c in done['toolCalls']]
        writes = [{k: v for k, v in w.items() if k != 'summary'} if w.get('held') else w for w in done['writes']]
        return done | {'outcome': 'held', 'recap': said(action, 'slackevents.guard.waiting' if asked else 'slackevents.guard.card_held'), 'held': kept, 'toolCalls': calls, 'writes': writes}

    @staticmethod
    def _dropped(done: dict, action: dict | None) -> dict:
        asked = bool(action) and action.get('type') == 'ask'
        calls = [c | {'input': ''} | ({'error': HELD_CALL_ERROR} if c.get('error') else {}) for c in done['toolCalls']]
        out = {k: v for k, v in done.items() if k not in PROPOSALS and k != 'screenshots'} | {'flags': [{k: v for k, v in f.items() if k != 'excerpt'} for f in done.get('flags') or [] if isinstance(f, dict)]}
        outage = ((done.get('guard') or {}).get('first') or {}).get('answers') == 'failed'
        key = 'slackevents.guard.unavailable' if outage else 'slackevents.guard.dropped' if asked else 'slackevents.guard.card_dropped'
        return out | {'recap': said(action, key), 'toolCalls': calls}

    def _hold_action(self, run_id: str, recorded: bool, action: dict, waiting: str) -> None:
        if not self._close_action(action, 'done', waiting[:RESULT_LIMIT]):
            self._answer_slack(run_id, recorded, action, 'done', waiting, {})
        if action.get('type') == 'ask':
            who = 'chat \u00b7 private' if action.get('source') == 'chat' else f"for {action.get('requestedBy')}"
            self.store.add_activity('ask', 'investigator', f'answer held for review {who}', runId=run_id, outcome='held')

    def _notify_held(self, run_id: str) -> None:
        if self.notices is None:
            return
        try:
            self.notices.held([e for e in self.admins() if e], run_id, f"{self.base_url.rstrip('/')}/api/investigations/{run_id}")
        except Exception as e:
            log.warning('held answer notifications not saved: %s', error_kind(e))

    def _action_of(self, run: dict) -> dict | None:
        if not run.get('actionId'):
            return None
        try:
            return self.store.get_action(run['actionId'])
        except NotFound:
            return None

    def _replay_box(self, run: dict, now: float) -> Toolbox:
        trigger = run.get('trigger')
        ask = trigger in ('ask', 'slack', 'chat')
        quick = trigger in ('slack', 'chat')
        return Toolbox(self.sources, self.store, self.effects, list(run.get('keys') or []), run.get('mode') or self.mode, now, self.base_url, run_id=run['_id'], ask=ask,
                       report_id=(run.get('report') or {}).get('id'), read_only=quick, strict=self.strict_tools, asker=(run.get('ask') or {}).get('by') if ask and not quick else None,
                       chat=trigger == 'chat', language=self.language, settings=self.settings, review=run.get('review'))

    def _post_released(self, run_id: str, action: dict, text: str, proposed: dict, shots: list[str]) -> None:
        if action.get('type') != 'ask' or not (action.get('slack') or {}).get('channel') or self.effects.slack_reply is None:
            return
        offered = {k: proposed[k] for k in ('ticketProposal', 'cardAction') if k in proposed}
        if offered:
            self.store.set_action_fields(action['id'], offered)
        confirm = {'runProposal': {'run': run_id, 'title': proposed['proposal']['title']}} if proposed.get('proposal') else {}
        sent = self.effects.slack_reply({**action, **offered, **confirm, **({'screenshots': shots, 'runId': run_id} if shots else {})}, 'done', text)
        if not sent:
            raise ReleaseFailed('the Slack reply was not posted')
        self.store.update_investigation(run_id, {'slackReply': sent})

    def release(self, run_id: str, by: str) -> dict | None:
        now = self.clock()
        token = uuid.uuid4().hex
        run = self.store.claim_held(run_id, epoch_iso(now), token)
        if run is None:
            self.expire_held(now, run_id)
            return None
        return self._complete(run, token, by, now)

    def _complete(self, run: dict, token: str, by: str, now: float) -> dict | None:
        run_id = run['_id']
        kept = run.get('held') or {}
        text = outward(str(kept.get('text') or ''))
        proposed = kept.get('proposed') or {}
        action = self._action_of(run)
        if not kept.get('posted'):
            try:
                if action:
                    self._post_released(run_id, action, text, proposed, [s['id'] for s in run.get('screenshots') or []])
            except Exception as e:
                self.store.unclaim_held(run_id, token)
                log.warning('held answer of run %s not released: %s', run_id, error_kind(e))
                raise ReleaseFailed(error_kind(e)) from None
            self.store.mark_posted(run_id)
            if not self.store.stamp_release(run_id, token, {'held.posted': True}):
                return self._lost_claim(run_id, by)
        box = self._replay_box(run, now)
        done = {int(i): str(r) for i, r in (kept.get('applied') or {}).items()}
        lost: list[bool] = []

        def stamp(i: int, result: str) -> None:
            if not self.store.stamp_release(run_id, token, {f'held.applied.{i}': result}):
                lost.append(True)
                raise ReleaseLost(run_id)

        try:
            said = box.replay(list(kept.get('writes') or []), done, stamp)
        except ReleaseLost:
            return self._lost_claim(run_id, by)
        applied = applied_writes(said[i] for i in range(len(said)) if i not in done) if said else 0
        writes = [w for w in run.get('writes') or [] if not w.get('held')] + box.writes
        if not self.store.finish_release(run_id, token, {'recap': text, 'writes': writes, 'held.released': {'by': by, 'at': epoch_iso(now)}} | proposed):
            return self._lost_claim(run_id, by)
        report = (run.get('report') or {}).get('id')
        if run.get('trigger') == 'assess' and report:
            try:
                self._numbers_post(report, self.store.deploy_report(report), any(w['tool'] == 'report_slack_draft' for w in box.writes))
            except NotFound:
                pass
        if action:
            try:
                self.store.set_action_fields(action['id'], {'result': (text.splitlines() or ['done'])[0][:RESULT_LIMIT]})
            except NotFound:
                pass
            self._remember_ask(run_id, action, {'recap': text, 'endedAt': run.get('endedAt') or epoch_iso(now), 'usd': run.get('usd')})
        missed = sum(1 for i in range(len(said)) if i not in done) - applied
        self.store.add_activity('run', by, f"held answer released by {by}" + (f" \u00b7 {applied} held write{'' if applied == 1 else 's'} applied" if applied else '')
                                + (f" \u00b7 {missed} not applied" if missed else ''), runId=run_id, keys=list(run.get('keys') or []) or None, outcome='done')
        self._notify(list(run.get('keys') or []), action, 'done', text)
        return self.store.investigation(run_id)

    def _lost_claim(self, run_id: str, by: str) -> None:
        log.warning('release of run %s stopped: another release or the recovery job took the run over', run_id)
        self.store.add_activity('admin', by, f'held answer release stopped \u00b7 run {run_id} \u00b7 taken over by another release; nothing posted again', runId=run_id)
        raise ReleaseTaken('the release was taken over')

    def discard(self, run_id: str, by: str, expired: bool = False, at: float | None = None) -> dict | None:
        now = self.clock() if at is None else at
        action = self._action_of(self.store.held_run(run_id) or {})
        gone = said(action, 'slackevents.guard.not_released')
        run = self.store.settle_held(run_id, 'discarded', {'held.discarded': {'by': by, 'at': epoch_iso(now)}, 'recap': gone}, epoch_iso(now), expired)
        if run is None:
            if not expired:
                self.expire_held(now, run_id)
            return None
        if action:
            try:
                self.store.set_action_fields(action['id'], {'result': gone[:RESULT_LIMIT]})
            except NotFound:
                pass
            self._slack_reply(action, 'done', gone)
        why = 'after 24 h without a review' if expired else f'by {by}'
        self.store.add_activity('run', by, f'held answer discarded {why}', runId=run_id, keys=list(run.get('keys') or []) or None, outcome='discarded')
        return self.store.investigation(run_id)

    def expire_held(self, now: float, only: str | None = None) -> int:
        gone = 0
        if only is None:
            self._unstick(now)
        for run_id in self.store.expired_held(epoch_iso(now), only):
            try:
                if self.discard(run_id, 'expiry', expired=True, at=now) is not None:
                    gone += 1
                    continue
                token = uuid.uuid4().hex
                run = self.store.claim_held(run_id, epoch_iso(now), token, expired=True)
                if run is not None:
                    self._complete(run, token, 'expiry', now)
            except Exception as e:
                log.warning('held answer of run %s not discarded: %s', run_id, error_kind(e))
        return gone

    def _unstick(self, now: float) -> None:
        before = epoch_iso(now - STUCK_RELEASE_S)
        for run_id in self.store.stuck_releasing(before):
            try:
                if self.store.unstick_release(run_id, before):
                    log.warning('release of run %s stopped half way; the answer is held again', run_id)
                    self.store.add_activity('admin', 'server', f'held answer release stopped half way \u00b7 run {run_id} \u00b7 held again', runId=run_id)
                    continue
                token = uuid.uuid4().hex
                run = self.store.retake_release(run_id, before, epoch_iso(now), token)
                if run is not None:
                    log.warning('release of run %s stopped after its post; finishing it without posting again', run_id)
                    self.store.add_activity('admin', 'server', f'held answer release stopped after its post \u00b7 run {run_id} \u00b7 finished without posting again', runId=run_id)
                    self._complete(run, token, 'server', now)
            except Exception as e:
                log.warning('stuck release of run %s not recovered: %s', run_id, error_kind(e))

    def _trace(self, run_id: str, trigger: str, keys: list[str], mode: str, action: dict | None, ask: str, provider: str | None = None) -> tracing.Handle:
        handle = tracing.begin(TRACE_NAMES[trigger], runId=run_id, cards=', '.join(keys), provider=provider or self.provider, trigger=trigger, mode=mode, source=MCP if via_mcp(action) else None)
        if handle.trace is not None:
            handle.trace.on_sent = lambda sent: self.store.update_investigation(run_id, {'trace': sent})
            try:
                handle.trace.note_brief(trigger=trigger, cards=len(keys))
                handle.trace.given(ask or ', '.join(keys))
            except Exception as e:
                log.warning('run trace input not kept: %s', error_kind(e))
        return handle

    @staticmethod
    def _traced(trace: tracing.Trace, outcome: str, error: str | None, recap: str) -> None:
        try:
            if outcome not in TRACE_OK:
                trace.fail(f'{outcome}: {error}' if error else outcome)
            trace.finished(outcome)
            trace.answered(recap)
        except Exception as e:
            log.warning('run trace not finished: %s', error_kind(e))

    def _notify(self, keys: list[str], action: dict | None, outcome: str, recap: str) -> None:
        if self.notices is None:
            return
        try:
            self.notices.run_done(keys, action, outcome, recap)
        except Exception as e:
            log.warning('run notifications not saved: %s', error_kind(e))

    def _with_ticket(self, call: dict | None, action: dict | None) -> dict | None:
        ticket = (action or {}).get('mentionedTicket')
        if not ticket:
            return call
        if call is not None:
            return call | {'ticket': ticket} if call.get('action') == 'take' and not call.get('ticket') else call
        keys = action.get('cards') or []
        try:
            card = self.store.anomaly(keys[0]) if keys else None
        except NotFound:
            card = None
        if not card or card.get('status') != 'open' or card.get('muted') or card.get('triage') == 'handled' or card.get('ticket') == ticket:
            return None
        return {'key': card['key'], 'action': 'take', 'ticket': ticket} | ({'owner': card['owner']} if card.get('triage') == 'progress' and card.get('owner') else {})

    def _remember_ask(self, run_id: str, action: dict, done: dict) -> None:
        if action.get('type') != 'ask' or action.get('source') != 'slack':
            return
        for key in (action.get('cards') or [])[:1]:
            try:
                card = self.store.anomaly(key)
            except NotFound:
                continue
            entry = history.ask_entry(action.get('text'), action.get('requestedBy'), done.get('recap'), (action.get('slack') or {}).get('permalink'), done['endedAt'], done.get('usd'), run_id)
            self.store.record_card(card, history.ASK_EVENT, ask=entry)

    def _close_action(self, action: dict, status: str, line: str) -> bool:
        try:
            self.store.update_action(action['id'], status, line, 'processing')
            return False
        except ActionMoved:
            if not (action.get('slack') or {}).get('channel'):
                raise
            log.warning('slack ask %s was reaped before its run ended; the placeholder already says so', action['id'])
            return True

    def _answer_slack(self, run_id: str, recorded: bool, action: dict, outcome: str, recap: str, proposed: dict, plugin_proposal: dict | None = None, shots: list[str] = ()) -> None:
        if action.get('type') != 'ask' or not (action.get('slack') or {}).get('channel'):
            return
        if proposed:
            try:
                self.store.set_action_fields(action['id'], proposed)
            except NotFound:
                pass
        confirm = {'runProposal': {'run': run_id, 'title': plugin_proposal['title']}} if plugin_proposal and recorded else {}
        sent = self._slack_reply({**action, **proposed, **confirm, **({'screenshots': list(shots), 'runId': run_id} if shots else {})}, outcome, recap or '')
        if sent and recorded:
            self.store.update_investigation(run_id, {'slackReply': sent})

    def _close_assessment(self, report_id: str, run_id: str, ok: bool, done: dict, toolbox, held: bool = False) -> None:
        try:
            report = self.store.deploy_report(report_id)
        except NotFound:
            return
        drafted = bool(toolbox) and any(w['tool'] == 'report_slack_draft' and not w.get('proposed') for w in toolbox.writes)
        self.store.set_report_fields(report_id, {'investigationId': run_id, 'usd': round(done['usd'], 4),
                                                 'costNote': f"investigator assessment, {done['turns']} turns, {len(done['toolCalls'])} tool calls" + ('' if ok else f", {done['outcome']}")})
        if not held:
            self._numbers_post(report_id, report, drafted)
        tail = ' \u00b7 numbers post held with the answer' if held else '' if drafted or (report.get('slackPost') or {}).get('ts') else ' \u00b7 numbers post queued'
        self.store.add_activity('assess', 'investigator', f"deploy report {'assessed' if ok else done['outcome']} · {report.get('summary')} · ${done['usd']:.2f}" + tail,
                                runId=run_id, reportId=report_id, outcome=done['outcome'])

    def _numbers_post(self, report_id: str, report: dict, drafted: bool) -> bool:
        text = (report.get('slackDraft') or {}).get('text')
        armed = bool((report.get('autoSend') or {}).get('at'))
        posting = self.effects.autopost is None or self.effects.autopost()
        if posting and not drafted and text and not armed and (report.get('slackDraft') or {}).get('by') == NUMBERS_BY and not (report.get('slackPost') or {}).get('ts'):
            self.store.report_slack_draft(report_id, text, 'server')
            return True
        return False
