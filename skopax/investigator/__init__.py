import base64
import logging
import re
import socket
import threading
import time
import uuid
from collections.abc import Callable, Mapping

import anthropic

from skopax import followup, history, information, severity, tracing
from skopax.ai import IMAGES_OFF, ModelSetting, clients_for, label_of, provider_of_ref, route, wrap
from skopax.config import Setting, Settings
from skopax.contract import OrgPack
from skopax.engine import iso as epoch_iso
from skopax.errkind import Refusal, kind as error_kind, text_of
from skopax.features import off_text
from skopax.investigator import pricing, prompt, select
from skopax.investigator.flags import REQUESTS, SOFT, asked, scan
from skopax.investigator.runner import Result, RunConfig, Runner
from skopax.investigator.tools import Effects, Sources, Toolbox
from skopax.investigator.toolset import Run, asked_gate
from skopax.investigator.web import own_domains as own_web_domains, web_from
from skopax.postdeploy import NUMBERS_BY
from skopax import clock
from skopax.messages import msg
from skopax.outward import outward
from skopax.store import LEASE_ID, ActionMoved, NotFound, Store, new_id

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
USE_FIELDS = {'card': ('investigate_model', 'card'), 'slack': ('investigate_slack_model', 'slack'), 'chat': ('chat_model', 'chat')}


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
        return self.chat_effort_set or self.slack_effort

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
            if keys or ask or report:
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
                    result = runner.run(toolbox, system, first, day, lambda: self.halt(day, lease))
        except BaseException as e:
            crash = e
            log.error('investigation %s crashed: %s', run_id, error_kind(e))
            raise
        finally:
            try:
                with self._lock:
                    self._runs.pop(run_id, None)
                done = self._finish(run_id, claimed, action, record is not None, toolbox, result, crash, mode, trace.trace)
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

    def _finish(self, run_id: str, claimed: list[str], action: dict | None, recorded: bool, toolbox, result, crash: BaseException | None, mode: str,
                trace: tracing.Trace | None = None) -> dict:
        ended = epoch_iso(self.clock())
        if result is None:
            outcome, error = ('failed', error_kind(crash)) if crash is not None else ('skipped', None)
            result = Result(outcome=outcome, error=error)
        result.recap = outward(result.recap or '')
        outcome, error = result.outcome, result.error
        done = {'outcome': outcome, 'endedAt': ended, 'turns': result.turns, 'models': result.models, 'usage': result.usage, 'usd': round(result.usd, 4),
                'toolCalls': toolbox.calls if toolbox else [], 'writes': toolbox.writes if toolbox else [], 'flags': toolbox.flags if toolbox else [],
                'recap': result.recap, 'error': error}
        if trace is not None:
            self._traced(trace, outcome, error, result.recap)
        if toolbox is not None and toolbox.proposal:
            done['ticketProposal'] = toolbox.proposal
        if toolbox is not None and toolbox.plugin_proposal and outcome == 'done':
            done['proposal'] = toolbox.plugin_proposal
        call = toolbox.card_action if toolbox is not None else None
        if outcome == 'done':
            call = self._with_ticket(call, action)
        if call:
            done['cardAction'] = call
        if toolbox is not None and toolbox.shots:
            done['screenshots'] = toolbox.shots
        try:
            if recorded:
                self.store.update_investigation(run_id, done)
                self.store.add_activity('run', 'investigator', f"run {outcome} · {done['turns']} turns · {len(done['toolCalls'])} tool calls · ${done['usd']:.2f}" + (f" · {error}" if error else ''), runId=run_id, outcome=outcome,
                                        **({'screenshots': done['screenshots']} if done.get('screenshots') else {}))
        finally:
            try:
                for k in claimed:
                    self.store.release_card(k, run_id)
            finally:
                if action:
                    ok = outcome == 'done'
                    line = (result.recap.splitlines() or ['done'])[0] if ok else f"{outcome}: {error or 'see the run record'}"
                    if ok and mode == 'shadow':
                        line = 'shadow run, nothing written: ' + line
                    if not self._close_action(action, 'done' if ok else 'failed', line[:RESULT_LIMIT]):
                        self._answer_slack(run_id, recorded, action, outcome, result.recap, {k: done[k] for k in ('ticketProposal', 'cardAction') if k in done}, done.get('proposal'),
                                           [s['id'] for s in done.get('screenshots') or []])
                    if ok and recorded:
                        self._remember_ask(run_id, action, done)
                    if action.get('type') == 'ask' and action.get('source') == 'chat':
                        self.store.add_activity('ask', 'investigator', f"chat {'answer' if ok else outcome} · private" + (' · via MCP' if via_mcp(action) else ''), runId=run_id, outcome=outcome)
                    elif action.get('type') == 'ask':
                        self.store.add_activity('ask', 'investigator', f"{'answer' if ok else outcome} for {action.get('requestedBy')} · {clip(prompt.flat(action.get('text') or ''), ASK_LINE)} → {clip(line, ASK_LINE)}", runId=run_id, outcome=outcome)
                    if action.get('type') == 'assess' and action.get('reportId'):
                        self._close_assessment(str(action['reportId']), run_id, ok, done, toolbox)
                if recorded:
                    self._notify(claimed, action, outcome, result.recap)
        return done

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

    def _close_assessment(self, report_id: str, run_id: str, ok: bool, done: dict, toolbox) -> None:
        try:
            report = self.store.deploy_report(report_id)
        except NotFound:
            return
        drafted = bool(toolbox) and any(w['tool'] == 'report_slack_draft' and not w.get('proposed') for w in toolbox.writes)
        self.store.set_report_fields(report_id, {'investigationId': run_id, 'usd': round(done['usd'], 4),
                                                 'costNote': f"investigator assessment, {done['turns']} turns, {len(done['toolCalls'])} tool calls" + ('' if ok else f", {done['outcome']}")})
        text = (report.get('slackDraft') or {}).get('text')
        armed = bool((report.get('autoSend') or {}).get('at'))
        posting = self.effects.autopost is None or self.effects.autopost()
        if posting and not drafted and text and not armed and (report.get('slackDraft') or {}).get('by') == NUMBERS_BY and not (report.get('slackPost') or {}).get('ts'):
            self.store.report_slack_draft(report_id, text, 'server')
        self.store.add_activity('assess', 'investigator', f"deploy report {'assessed' if ok else done['outcome']} · {report.get('summary')} · ${done['usd']:.2f}" + ('' if drafted or (report.get('slackPost') or {}).get('ts') else ' · numbers post queued'),
                                runId=run_id, reportId=report_id, outcome=done['outcome'])
