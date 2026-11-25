import hashlib
import json
import logging
import re
import threading
import time
from collections import Counter
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass

from mcp import types

from skopax.actors import is_token
from skopax.contract import Knowledge
from skopax.errkind import kind
from skopax.investigator.prompt import quoted as prompt_quoted
from skopax.investigator.tools import ToolError
from skopax.investigator.toolset import Run
from skopax.messages import msg
from skopax.redact import redact_secrets, redact_text
from skopax.users import ROLES
from skopax.mcptools import (CALL_DEADLINE_S, CALL_THREAD_S, ERROR_TEXT_MAX, KNOWLEDGE_SECTION, LABEL_MAX, LIVE_CACHE_S, ON_MAX, OUTPUT_MAX, PER_TOOL_CALLS, PLUGIN_ID,
                             RUN_CALLS_HIGH, RUN_CALLS_KEY, RUN_CALLS_LOW, RUN_CALLS_MAX, RUN_DEFINITIONS_MAX, RUN_LIST_DEADLINE_S, RUN_OUTPUT_MAX, RUN_TOOLS_MAX, TEARDOWN_S, Moved, Reconnect, left, scrub, secrets_of,
                             within)
from skopax.mcptools.adapters import Adapter, ReadRefused, Scope, ScopeRefused, rendered
from skopax.mcptools.catalog import NAME_MAX, STALE, tool_hash
from skopax.mcptools.client import PERMISSION_WORDS, UNAUTHORIZED, McpFailure, Refused, Unauthorized, check_headers, failure_text
from skopax.mcptools.oauth import NoScope
from skopax.mcptools.routes import (DEFAULT_HEADER, LOCAL_HTTP, REFUSED, Deps, adapter_tools, allowed, client_of, credential, knowledge_on, moved_away, on_origin,
                                    permission_of, ready_adapter, server_words, values_of, watched)

log = logging.getLogger('skopax.mcptools.bridge')

RANK = {role: n for n, role in enumerate(ROLES)}
CHAT_ROLE = 'operator'
SLACK_OPERATOR = 'operator'
MASKED_MAX = 2 * OUTPUT_MAX
INERT = re.compile(r'(</?outside)-(data)', re.IGNORECASE)


def asker_role(action: dict | None, users) -> str | None:
    if not isinstance(action, dict) or action.get('type') != 'ask':
        return None
    by = action.get('requestedBy')
    if is_token(by):
        return ROLES[0]
    slack = action.get('slack')
    role = SLACK_OPERATOR if isinstance(slack, dict) and slack.get('operator') else ROLES[0]
    try:
        found = users.get(by) if isinstance(by, str) and by else None
    except Exception as e:
        log.warning('the role of an asker was not read, so the ask keeps %s: %s', role, kind(e))
        return role
    if found and found.get('disabled'):
        return role
    if action.get('mcpRole') in RANK:
        return action['mcpRole']
    if found and RANK.get(found.get('role'), -1) > RANK[role]:
        return found['role']
    return role


def header_ok(values: Mapping[str, str]) -> bool:
    value = values.get('header-value') or ''
    try:
        check_headers({values.get('header-name') or DEFAULT_HEADER: value})
    except Refused:
        return False
    return bool(value)


def joined(*groups: Iterable[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(s for group in groups for s in group))


def label_of(folder: Scope) -> str:
    flat = ' '.join(''.join(ch if ch.isprintable() else ' ' for ch in folder.label).split())
    return '"' + prompt_quoted(flat.replace('"', "'"), LABEL_MAX) + '"'


def folder_of(doc: dict) -> Scope | None:
    found = doc.get('scope')
    if not isinstance(found, dict) or not isinstance(found.get('id'), str) or not found['id']:
        return None
    return Scope(found['id'], str(found.get('label') or ''), str(found.get('url') or ''))


def masked(text: str, secrets: Iterable[str]) -> tuple[str, bool]:
    shown = redact_secrets(scrub(text, secrets)[:MASKED_MAX])
    if len(text) <= MASKED_MAX and len(shown) <= OUTPUT_MAX:
        return shown, False
    return shown[:OUTPUT_MAX] + '\n' + msg('mcptools.truncated', limit=f'{OUTPUT_MAX:,}'), True


@dataclass(frozen=True)
class Conn:
    name: str
    values: Mapping[str, str]
    doc: dict
    secrets: tuple[str, ...]
    scope: frozenset[str] | None
    adapter: Adapter | None = None
    folder: Scope | None = None

    @property
    def account(self) -> str:
        return str(self.doc.get('account') or '')

    @property
    def key(self) -> tuple[str, str, str]:
        stored = sorted((str(r.get('name')), str(r.get('hash'))) for r in self.doc.get('tools') or [])
        return self.name, self.account, hashlib.sha256(json.dumps(stored).encode()).hexdigest()


@dataclass(frozen=True)
class Bridged:
    conn: Conn
    name: str
    exposed: str
    hash: str
    schema: dict
    definition: dict


def bridged(conn: Conn, row: dict) -> Bridged:
    scoped = msg('mcptools.description_scope', noun=conn.adapter.noun, folder=label_of(conn.folder)) if conn.adapter is not None and conn.folder is not None else ''
    text = ' '.join(t for t in (msg('mcptools.description', instance=conn.name), row.get('description') or '', scoped) if t)
    return Bridged(conn, row['name'], row['exposed'], row.get('hash') or '', row['schema'], {'name': row['exposed'], 'description': text, 'input_schema': row['schema']})


def offerable(row: dict, conn: Conn) -> bool:
    understood = row.get('kind') == 'read' or (row.get('kind') == 'unknown' and bool(row.get('understood')))
    return (bool(row.get('on')) and understood and not row.get('blocked') and not row.get('changed') and bool(row.get('exposed')) and isinstance(row.get('schema'), dict)
            and (conn.scope is None or row.get('name') in conn.scope))


def framed(tool: Bridged, text: str) -> str:
    inner = INERT.sub(r'\1_\2', text)
    return f'<outside-data server="{tool.conn.name}" tool="{tool.exposed}">\n{inner}\n</outside-data>\n' + msg('mcptools.framed')


def framed_error(tool: Bridged, key: str, said: str, **values) -> str:
    def wrap(inner: str) -> str:
        kept = INERT.sub(r'\1_\2', inner)
        return f'<outside-data server="{tool.conn.name}" tool="{tool.exposed}">{kept}</outside-data>'

    room = ERROR_TEXT_MAX - len(msg(key, instance=tool.conn.name, tool=tool.exposed, text=wrap(''), **values))
    if room <= 0:
        return values['refusal'] if 'refusal' in values else msg(key, instance=tool.conn.name, tool=tool.exposed, text='')
    return msg(key, instance=tool.conn.name, tool=tool.exposed, text=wrap(said[:room]), **values)


def said_of(result: types.CallToolResult, secrets: Iterable[str]) -> str:
    return server_words(rendered(result), tuple(secrets))


def read_refused(tool: Bridged, error: ReadRefused, secrets: Iterable[str]) -> str:
    said = server_words(error.said, tuple(secrets))
    base = tool.conn.adapter.refusal(credential(tool.conn.values) if PERMISSION_WORDS.search(said) else None)
    return framed_error(tool, 'mcptools.access.adapter_words', said, refusal=base) if said else base


class ToolBridge:
    def __init__(self, deps: Deps, runtime, features, store, client: Callable[..., object] = client_of, clock: Callable[[], float] = time.monotonic):
        self.deps = deps
        self.runtime = runtime
        self.features = features
        self.store = store
        self.client = client
        self.clock = clock
        self._live: dict[tuple[str, str, str], tuple[float, dict[str, str]]] = {}
        self._guard = threading.Lock()

    def start(self, run: Run, run_id: str | None = None) -> 'RunTools':
        try:
            return RunTools(self, self._start(run, run_id), run, self.run_calls())
        except Exception as e:
            log.warning('MCP server tools were left out of run %s: %s', run_id, kind(e))
            return RunTools(self, (), run)

    def run_calls(self) -> int:
        try:
            found = self.deps.setting(RUN_CALLS_KEY)
        except KeyError:
            return RUN_CALLS_MAX
        if isinstance(found, bool) or not isinstance(found, (int, float)) or not RUN_CALLS_LOW <= found <= RUN_CALLS_HIGH:
            return RUN_CALLS_MAX
        return int(found)

    def _start(self, run: Run, run_id: str | None) -> list[Bridged]:
        found = sorted(self.runtime.instances_of(PLUGIN_ID), key=lambda i: i.name)
        if not found or not self.features.on('investigator') or (run.chat and not self.features.on('chat')):
            return []
        kept = [c for c in (self._kept(i, run) for i in found) if c is not None]
        listed = self._listings(kept, run_id)
        offered: list[Bridged] = []
        for conn in kept:
            if conn.name in listed:
                offered += self._offered(conn, listed[conn.name], run_id)
        return self._fit(offered, run_id)

    def _kept(self, inst, run: Run, call: bool = False) -> Conn | None:
        try:
            values = values_of(self.deps, inst)
            if moved_away(self.deps, inst.name, values):
                return None
            if (run.ask or run.role is not None) and RANK.get(run.role or ROLES[0], -1) < RANK.get(values.get('chat-role') or CHAT_ROLE, len(ROLES)):
                return None
            scope = adapter_tools(values)
            if knowledge_on(values) and (scope is None or not on_origin(values)):
                return None
            mode = values.get('auth') or 'none'
            if (mode == 'header' and not header_ok(values)) or (mode == 'oauth' and self.deps.tokens.state(inst.name) != 'connected'):
                return None
            doc = self.deps.catalog.get(inst.name) or {}
            if not self.deps.catalog.current(inst.name, knowledge_on(values), doc, values.get('url') or ''):
                return None
            adapter, folder = (ready_adapter(values), folder_of(doc)) if scope is not None else (None, None)
            if scope is not None and (adapter is None or folder is None):
                return None
            return Conn(inst.name, values, doc, secrets_of(values, self.deps.tokens.secrets(inst.name)), scope, adapter, folder)
        except Exception as e:
            if call:
                log.warning('MCP server %s: a tool call was refused because its switches could not be read: %s', inst.name, kind(e))
            else:
                log.warning('MCP server %s was left out of a run: %s', inst.name, kind(e))
            return None

    def _listings(self, conns: list[Conn], run_id: str | None) -> dict[str, dict[str, str]]:
        now = self.clock()
        out: dict[str, dict[str, str]] = {}
        jobs = []
        with self._guard:
            for conn in conns:
                hit = self._live.get(conn.key)
                if hit is not None and now - hit[0] < LIVE_CACHE_S:
                    out[conn.name] = hit[1]
                else:
                    jobs.append((conn, {}, threading.Event()))
        for conn, box, done in jobs:
            threading.Thread(target=self._list, args=(conn, box, done), name=f'mcp-list-{conn.name}', daemon=True).start()
        end = time.monotonic() + RUN_LIST_DEADLINE_S + TEARDOWN_S
        for conn, box, done in jobs:
            finished = done.wait(max(0.0, end - time.monotonic()))
            if not finished or 'hashes' not in box:
                self._note(run_id, msg('mcptools.run_skipped', instance=conn.name, reason=box.get('why') if finished else msg('connection.timeout')))
                if finished and box.get('refused'):
                    self.deps.catalog.refusal(conn.name, box.get('why'))
                continue
            if conn.doc.get('refused'):
                self.deps.catalog.refusal(conn.name, None)
            with self._guard:
                self._live[conn.key] = (now, box['hashes'])
            out[conn.name] = box['hashes']
        return out

    def _list(self, conn: Conn, box: dict, done: threading.Event) -> None:
        heard: dict = {}
        try:
            found = self._retried(conn, heard, RUN_LIST_DEADLINE_S, lambda client: client.listing(left(RUN_LIST_DEADLINE_S), joined(conn.secrets, self._secrets(conn))))
            hashes: dict[str, str] = {}
            for t in found.tools:
                hashes.setdefault(t.name, tool_hash(t))
            box['hashes'] = hashes
        except NoScope as e:
            box['why'] = scrub(e.text, joined(conn.secrets, self._secrets(conn)))
        except Moved:
            self._moved(conn)
            box['why'] = msg('mcptools.oauth.connect_first')
        except Reconnect:
            box['why'] = msg('mcptools.oauth.connect_first')
        except McpFailure as e:
            box['why'] = e.text
        except Exception as e:
            box['why'] = failure_text(e)
        finally:
            box['refused'] = heard.get('status') in REFUSED
            done.set()

    def _client(self, conn: Conn, heard: dict | None = None, renew: bool = False):
        http = self.deps.http if heard is None else (lambda ca: watched(self.deps.http(ca), heard, conn.secrets))
        allow = allowed(lambda: self.deps.setting(LOCAL_HTTP))
        if renew:
            return self.client(self.deps.tokens, http, conn.name, conn.values, allow, renew=True)
        return self.client(self.deps.tokens, http, conn.name, conn.values, allow)

    def _retried(self, conn: Conn, heard: dict, budget: float, work: Callable[[object], object]):
        with within(budget):
            try:
                return work(self._client(conn, heard))
            except Unauthorized:
                if (conn.values.get('auth') or 'none') != 'oauth':
                    raise
            heard.clear()
            return work(self._client(conn, heard, renew=True))

    def _moved(self, conn: Conn) -> None:
        if (conn.values.get('auth') or 'none') == 'oauth' and self.deps.tokens.state(conn.name) == 'connected':
            self.deps.tokens.park(conn.name, 'moved')

    def refused_call(self, name: str, text: str) -> None:
        try:
            self.deps.catalog.refusal(name, text, call=True)
        except Exception as e:
            log.warning('MCP server %s: a refused tool call was not recorded: %s', name, kind(e))

    def _secrets(self, conn: Conn) -> tuple[str, ...]:
        return secrets_of(conn.values, self.deps.tokens.secrets(conn.name))

    def refusal(self, tool: Bridged, run: Run) -> str | None:
        try:
            inst = next((i for i in self.runtime.instances_of(PLUGIN_ID) if i.name == tool.conn.name), None)
            values = values_of(self.deps, inst) if inst is not None else {}
            if (values.get('auth') or 'none') == 'oauth' and (moved_away(self.deps, inst.name, values) or self.deps.tokens.state(inst.name) == 'reconnect'):
                return msg('mcptools.oauth.reconnect', instance=tool.conn.name)
            fresh = None if inst is None else self._kept(inst, run, call=True)
            row = next((r for r in fresh.doc.get('tools') or [] if r.get('name') == tool.name), None) if fresh is not None else None
            same = fresh is not None and fresh.account == tool.conn.account and fresh.folder == tool.conn.folder and row is not None
            if same and row.get('exposed') == tool.exposed and row.get('hash') == tool.hash and offerable(row, fresh):
                return None
        except Exception as e:
            log.warning('MCP server %s: a tool call was refused because its switches could not be read: %s', tool.conn.name, kind(e))
        return msg('mcptools.tool_off', instance=tool.conn.name)

    def _offered(self, conn: Conn, hashes: dict[str, str], run_id: str | None) -> list[Bridged]:
        try:
            stale = [r['name'] for r in conn.doc.get('tools') or [] if r.get('on') and hashes.get(r['name']) != r.get('hash')]
            if stale:
                self.deps.catalog.turn_off(conn.name, stale, STALE)
                self._note(run_id, msg('mcptools.tools.changed', instance=conn.name, names=', '.join(redact_text(n, NAME_MAX) for n in stale)))
            rows = sorted((r for r in conn.doc.get('tools') or [] if r['name'] not in stale and offerable(r, conn)), key=lambda r: r['exposed'])
            return [bridged(conn, r) for r in rows[:ON_MAX]]
        except Exception as e:
            log.warning('MCP server %s was left out of a run: %s', conn.name, kind(e))
            return []

    def _fit(self, tools: list[Bridged], run_id: str | None) -> list[Bridged]:
        kept: list[Bridged] = []
        size = 0
        for n, tool in enumerate(tools):
            more = len(json.dumps(tool.definition))
            if len(kept) == RUN_TOOLS_MAX or size + more > RUN_DEFINITIONS_MAX:
                self._note(run_id, msg('mcptools.run_full', count=len(tools) - n, limit=RUN_TOOLS_MAX, chars=f'{RUN_DEFINITIONS_MAX:,}'))
                break
            kept.append(tool)
            size += more
        return kept

    def _note(self, run_id: str | None, text: str) -> None:
        self.store.add_activity('run', 'investigator', text, runId=run_id)

    def run(self, tool: Bridged, args: dict, spend: Callable[[], None] = lambda: None) -> tuple[str, dict]:
        box: dict = {'heard': {}, 'secrets': ()}
        done = threading.Event()
        gate, ended = threading.Lock(), []
        adapter, folder = tool.conn.adapter, tool.conn.folder

        def call(client) -> types.CallToolResult:
            box['secrets'] = self._secrets(tool.conn)
            if adapter is None:
                return client.call(tool.name, args, left(CALL_DEADLINE_S))

            def checked(name: str, given: dict) -> types.CallToolResult:
                with gate:
                    if ended:
                        raise ToolError(msg('connection.timeout'))
                    spend()
                return client.call(name, given, left(CALL_DEADLINE_S))

            return client.call(tool.name, adapter.before(tool.name, args, folder, checked), left(CALL_DEADLINE_S))

        def go() -> None:
            try:
                box['out'] = self._retried(tool.conn, box['heard'], CALL_DEADLINE_S, call)
            except BaseException as e:
                if isinstance(e, Moved):
                    self._moved(tool.conn)
                box['error'] = e
            finally:
                done.set()

        threading.Thread(target=go, name=f'mcp-call-{tool.conn.name}', daemon=True).start()
        if not done.wait(CALL_THREAD_S):
            with gate:
                ended.append(True)
            return 'timeout', box
        return ('error' if 'error' in box else 'done'), box


class RunTools:
    def __init__(self, bridge: ToolBridge, tools: Iterable[Bridged], run: Run, limit: int = RUN_CALLS_MAX):
        self.bridge = bridge
        self.tools = {t.exposed: t for t in tools}
        self.run = run
        self.limit = limit
        self.used: Counter = Counter()
        self.calls = 0
        self.read = 0

    def definitions(self) -> list[dict]:
        return [dict(t.definition) for t in self.tools.values()]

    def owns(self, name: object) -> bool:
        return isinstance(name, str) and name in self.tools

    def schema(self, name: str) -> dict:
        return self.tools[name].schema

    def instance(self, name: str) -> str:
        return self.tools[name].conn.name

    def knowledge(self) -> list[Knowledge]:
        found = [t for t in self.tools.values() if t.conn.adapter is not None and t.conn.folder is not None]
        if not found:
            return []
        lines = [msg('mcptools.knowledge.line', tool=t.exposed, noun=t.conn.adapter.noun, folder=label_of(t.conn.folder), instance=t.conn.name) for t in found]
        return [Knowledge(KNOWLEDGE_SECTION, msg('mcptools.knowledge.title'), '\n'.join([msg('mcptools.knowledge.intro'), *lines, msg('mcptools.knowledge.rules')]), slack=True)]

    def link_hosts(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(h for t in self.tools.values() if t.conn.adapter is not None for h in t.conn.adapter.link_hosts))

    def refused(self, name: str, run_id: str | None) -> None:
        self._note(self.tools[name], run_id, 0, 'refused', 0)

    def call(self, name: str, args: dict, run_id: str | None, chat: bool) -> tuple[str, int | None, bool]:
        tool = self.tools[name]
        began = time.monotonic()
        outcome, chars, secrets = 'refused', 0, tool.conn.secrets
        try:
            refused = self.bridge.refusal(tool, self.run)
            if refused is not None:
                raise ToolError(refused)
            outcome = 'capped'
            self._spend(tool)
            outcome = 'error'
            state, box = self.bridge.run(tool, args, lambda: self._spend(tool))
            if state == 'timeout':
                outcome = 'timeout'
                raise ToolError(msg('connection.timeout'))
            if state == 'error':
                text = self._failed(tool, box)
                outcome = 'timeout' if text == msg('connection.timeout') else self._outcome(box['error'])
                if box['heard'].get('status') in REFUSED:
                    self.bridge.refused_call(tool.conn.name, text)
                raise ToolError(text)
            found, secrets = box.get('out'), joined(tool.conn.secrets, box['secrets'])
            if tool.conn.adapter is not None:
                kept, rows = tool.conn.adapter.after(tool.name, found, tool.conn.folder)
                text, cut = masked(kept, secrets)
                self.read += len(text)
                outcome, chars = 'ok', len(text)
                return framed(tool, text), rows, cut
            if found.isError:
                said = said_of(found, secrets)
                key = 'mcptools.tool_error'
                if PERMISSION_WORDS.search(said):
                    key = 'mcptools.access.said' if credential(tool.conn.values) is not None else 'mcptools.access.said_open'
                raise ToolError(framed_error(tool, key, said))
            text, cut = masked(rendered(found), secrets)
            self.read += len(text)
            outcome, chars = 'ok', len(text)
            return framed(tool, text), None, cut
        except ToolError:
            raise
        except ReadRefused as e:
            raise ToolError(read_refused(tool, e, secrets)) from None
        except Exception as e:
            raise ToolError(failure_text(e)) from None
        finally:
            self._note(tool, run_id, round((time.monotonic() - began) * 1000), outcome, chars)

    def _spend(self, tool: Bridged) -> None:
        if self.used[tool.exposed] >= PER_TOOL_CALLS:
            raise ToolError(msg('mcptools.cap.tool', tool=tool.exposed, limit=PER_TOOL_CALLS))
        if self.calls >= self.limit:
            raise ToolError(msg('mcptools.cap.run', limit=self.limit))
        if self.read >= RUN_OUTPUT_MAX:
            raise ToolError(msg('mcptools.cap.output', limit=f'{RUN_OUTPUT_MAX:,}'))
        self.used[tool.exposed] += 1
        self.calls += 1

    @staticmethod
    def _outcome(error: BaseException) -> str:
        if isinstance(error, ScopeRefused):
            return 'refused'
        return 'capped' if isinstance(error, ToolError) else 'error'

    @staticmethod
    def _failed(tool: Bridged, box: dict) -> str:
        error, heard = box['error'], box['heard']
        if isinstance(error, ReadRefused) and tool.conn.adapter is not None:
            return read_refused(tool, error, joined(tool.conn.secrets, box['secrets']))
        if isinstance(error, (ScopeRefused, ReadRefused, ToolError)):
            return error.text
        if isinstance(error, NoScope):
            return scrub(error.text, joined(tool.conn.secrets, box['secrets']))
        if isinstance(error, Reconnect):
            return msg('mcptools.oauth.reconnect', instance=tool.conn.name)
        if heard.get('status') in REFUSED:
            held = credential(tool.conn.values)
            if held is None:
                return msg('mcptools.access.no_credential', instance=tool.conn.name, tool=tool.exposed)
            permission = permission_of(heard, joined(tool.conn.secrets, box['secrets']))
            if heard['status'] == UNAUTHORIZED:
                return msg('mcptools.access.invalid', instance=tool.conn.name, tool=tool.exposed, credential=held, permission=permission)
            return msg('mcptools.access.refused', instance=tool.conn.name, tool=tool.exposed, status=heard['status'], credential=held, permission=permission)
        if isinstance(error, McpFailure):
            return error.text
        return failure_text(error)

    def _note(self, tool: Bridged, run_id: str | None, ms: int, outcome: str, chars: int) -> None:
        text = msg('mcptools.activity', server=tool.conn.name, tool=redact_text(tool.name, NAME_MAX), ms=ms, outcome=outcome, chars=chars)
        self.bridge.store.add_activity('tool', 'investigator', text, runId=run_id, tool=tool.exposed, error=None)
