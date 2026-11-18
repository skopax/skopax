import asyncio
import dataclasses
import copy
import hashlib
import json
import logging
import re
import threading
import time
from collections.abc import Callable, Iterable, Mapping
from urllib.parse import urlsplit

import regex

from skopax import remote, tags
from skopax.agent import AgentError, RemoteAgent, declared, http_link, rev_of
from skopax.bounded import HOOK_TIMEOUT_S, Bound, TimedOut
from skopax.contract import Context, Field, InstanceInfo, Knowledge, Locale, OrgPack, Panel, Peer, Plugin, ReadError, SourceDown, TestResult
from skopax.engine import parse_iso
from skopax.errkind import kind
from skopax.kube import Kube
from skopax.messages import LANG_TAG, msg
from skopax.migrations import MigrationFailed
from skopax.redact import SlowRules
from skopax.instances import (ADMIN, AGENT_PLUGIN, REFRESH_S, Instance, InstanceMissing, Instances, InvalidInstance, PluginState, address_owners, agent_fields, checks_off, effective_fields,
                              hub_problem, merged_values, moved_addresses, validate, visible)
from skopax.registry import Registry, field_view, problem
from skopax.routes import Route, route_of
from skopax.sections import plugin_of, stored_rows
from skopax.remote import Transport

log = logging.getLogger('skopax.runtime')

KNOWLEDGE_TOTAL = 24000

ORG_KEYS = ('link_hosts', 'email_domains', 'ticket_projects')
ASSET_MAX_BYTES = 2 * 1024 * 1024
TEST_SUMMARY_MAX = 300
CLIENT_RETRY_S = 60
READ_TIMEOUT_MIN = 1
READ_TIMEOUT_MAX = 75
PROBE_HOOK_S = 5
ICON_FILE = 'icon.svg'
LINK_NAME = re.compile(r'[a-z0-9-]{1,40}')
LINK_URL = re.compile(r'https://\S{1,3990}')
LINKS_PER_ROW = 20
LINKS_MAX = 8
RELEASE_TEXT = ('ns', 'svc', 'at', 'toImage')
RELEASE_KEYS = ('id', 'ns', 'svc', 'at', 'fromImage', 'toImage', 'title')
RELEASE_CHARS = 200
RELEASE_ITEMS = 20
RELEASE_DEPTH = 4
ASSET_TYPES = {'.js': 'text/javascript; charset=utf-8', '.html': 'text/html; charset=utf-8'}
PAGE_PACK = '(function (all, lang, pack) { var held = all[lang] = all[lang] || {}; Object.keys(pack).forEach(function (k) { var v = pack[k], was = held[k]; held[k] = k === "@rules" || k === "@tails" ? (Array.isArray(was) ? was : []).concat(v) : k === "@months" || k === "@days" || k === "@th" ? Object.assign({}, was, v) : v; }); })(window.SKOPAX_I18N = window.SKOPAX_I18N || {}, %s, %s);'
PROTECTED_HEADERS = ('accept', 'content-type', 'host', 'content-length', 'transfer-encoding', 'connection')
HEADER_NAME = re.compile(r"[!#$%&'*+\-.^_`|~0-9A-Za-z]+")
HEADER_VALUE = re.compile(r'[^\x00-\x08\x0a-\x1f\x7f]*')
I18N_FILE = re.compile(r'i18n-(' + LANG_TAG.pattern + r')\.js')

_contexts: dict[str, Transport] = {}
_contexts_guard = threading.Lock()


def on_page(locale: Locale) -> bool:
    return any((locale.page, locale.rules, locale.months, locale.days, locale.tails, locale.headers))


def page_pack(locales: list[Locale]) -> dict:
    page: dict[str, str] = {}
    months: dict[str, str] = {}
    days: dict[str, str] = {}
    headers: dict[str, str] = {}
    for locale in locales:
        page.update(locale.page)
        months.update(locale.months)
        days.update(locale.days)
        headers.update(locale.headers)
    extra = {'@rules': [list(r) for locale in locales for r in locale.rules], '@months': months, '@days': days, '@tails': [list(t) for locale in locales for t in locale.tails], '@th': headers}
    return page | {key: value for key, value in extra.items() if value}


def pem_transport(pem: str, pinned: bool = False) -> Transport:
    with _contexts_guard:
        if (pem, pinned) not in _contexts:
            _contexts[(pem, pinned)] = remote.sender(remote.tls_context(pem, pinned))
        return _contexts[(pem, pinned)]


def ca_of(plugin: Plugin, config: Mapping[str, str]) -> tuple[str, bool]:
    pem = config.get('ca') if any(f.key == 'ca' and f.kind == 'pem' for f in plugin.fields) else ''
    pem = pem if isinstance(pem, str) else ''
    return pem, bool(pem) and config.get('ca-pinned') == 'true'


def capped(value: object, depth: int = 0) -> object:
    if isinstance(value, str):
        return value[:RELEASE_CHARS]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if depth >= RELEASE_DEPTH:
        return None
    if isinstance(value, (list, tuple)):
        return [capped(v, depth + 1) for v in value[:RELEASE_ITEMS]]
    if isinstance(value, dict):
        return {str(k)[:RELEASE_CHARS]: capped(v, depth + 1) for k, v in list(value.items())[:RELEASE_ITEMS]}
    return None


def _stem(card: dict) -> str | None:
    key, named = card.get('ranKey'), card.get('cluster')
    if not isinstance(key, str) or not key:
        return None
    if isinstance(named, str) and named and key.endswith(f'-{named}'):
        key = key[:-len(named) - 1]
    return key.split(':', 1)[0]


class RunContext:
    def __init__(self, runtime: 'Runtime', plugin: Plugin, instance: Instance | None, now: float, reads=None):
        self.runtime = runtime
        self.plugin = plugin
        self.now = now
        self.instance = None if instance is None else instance.info()
        self.testing = False
        self._reads = reads
        self._tool_reads: list = []

    def http(self) -> Transport:
        return self.runtime.transport_for(self.plugin, self.instance)

    def peer(self, field: str) -> Peer | None:
        found = next((f for f in self.plugin.fields if f.key == field and f.kind == 'instance'), None)
        if found is None:
            raise ReadError(400, msg('peers.not_instance', field=str(field)[:40]))
        value = (self.instance.config.get(field) if self.instance is not None else '') or ''
        if not value:
            return None
        used = found.choices[0]
        plugin = next((p for p in self.runtime.active() if p.id == used), None)
        chosen = next((i for i in self.runtime.instances_of(used) if i.name == value), None) if plugin is not None else None
        if chosen is None:
            raise ReadError(503, msg('peers.unavailable', plugin=self.runtime.plugin_name(used), instance=value))
        return self.runtime.peer_of(plugin, chosen)

    def peers(self, plugin_id: str) -> list[Peer]:
        if plugin_id != self.plugin.id and plugin_id not in self.plugin.uses:
            raise ReadError(403, msg('peers.not_used', plugin=str(plugin_id)[:40]))
        plugin = next((p for p in self.runtime.active() if p.id == plugin_id), None)
        found = sorted(self.runtime.instances_of(plugin_id), key=lambda i: (not i.primary, i.name)) if plugin is not None else []
        return [self.runtime.peer_of(plugin, i) for i in found]

    def previous(self, section: str) -> list | None:
        reads = self._reads if self._reads is not None else self._stored()
        return None if reads is None else reads.previous(self.instance, section)

    def deploys(self, since: float) -> list[dict]:
        return [] if self._reads is None else self._reads.deploys(since)

    def open_cards(self, plugin_wide: bool = False) -> list[dict]:
        return [] if self._reads is None else self._reads.open_cards(self.instance, plugin_wide)

    def client(self) -> object | None:
        return self.runtime.client(self.plugin.id, None if self.instance is None else self.instance.name)

    def ai(self, model: str) -> object | None:
        check = getattr(self._reads, 'check', None)
        if check is None or self.runtime.ai is None:
            return None
        return self.runtime.ai(self.plugin, check, self.instance, model)

    def memo(self, key: str) -> dict | None:
        if self.runtime.memos is None or not isinstance(key, str) or not key:
            return None
        return self.runtime.memos(self.plugin.id, None if self.instance is None else self.instance.name, key)

    def _stored(self):
        if not self._tool_reads:
            self._tool_reads.append(self.runtime.reader(self.plugin) if self.runtime.reader is not None else None)
        return self._tool_reads[0]

    def rules(self, set_id: str) -> dict[str, float]:
        return self.runtime.rules(self.plugin.id, set_id)

    def setting(self, key: str) -> object:
        return self.runtime.setting(self.plugin.id, key)

    def agent_read(self, agent: str, op: str, args: dict) -> object:
        return self.runtime.agent_read(agent, op, args)

    def _host_only(self) -> None:
        if self.plugin.id != AGENT_PLUGIN or self.instance is None:
            raise ReadError(403, msg('agent.host_only'))

    def agent(self) -> RemoteAgent:
        self._host_only()
        return self.runtime.agent_from(self.instance, self.testing)

    def local_kube(self) -> object:
        self._host_only()
        return self.runtime.kube if self.runtime.kube is not None else Kube()

    def agent_of(self, peer: Peer) -> RemoteAgent:
        self._host_only()
        return self.runtime.agent_from(self.runtime._agent(peer.info.name).info(), self.testing)


class LiveClient:
    def __init__(self, runtime: 'Runtime', plugin_id: str):
        self._runtime = runtime
        self._plugin_id = plugin_id

    @property
    def enabled(self) -> bool:
        found = self._runtime.client(self._plugin_id)
        return found is not None and bool(getattr(found, 'enabled', True))

    def __getattr__(self, name: str) -> object:
        if name.startswith('__') and name.endswith('__'):
            raise AttributeError(name)
        found = self._runtime.client(self._plugin_id)
        if found is None:
            if name.startswith('_'):
                raise AttributeError(name)
            raise SourceDown(msg('integrations.off', name=self._runtime.plugin_name(self._plugin_id)))
        return getattr(found, name)


def field_set(instance, field: str) -> bool:
    return bool(str(instance.values.get(field) or instance.secrets.get(field) or '').strip())


def held_knowledge(plugin: Plugin, found: list) -> bool:
    if plugin.checks:
        return False
    gated = [t.needs_field for t in plugin.tools if t.needs_field]
    return bool(gated) and len(gated) == len(plugin.tools) and not any(field_set(i, f) for f in gated for i in found or [])


class Runtime:
    def __init__(self, registry: Registry, state: PluginState, instances: Instances, transport: Transport | None = None, clients: Mapping[str, object] | None = None,
                 hook_timeout: float = HOOK_TIMEOUT_S):
        self.registry = registry
        self.state = state
        self.instances = instances
        self.transport = transport
        self.clients = dict(clients or {})
        self.reader: Callable[[Plugin], object] | None = None
        self.ai: Callable[[Plugin, object, InstanceInfo | None, str], object | None] | None = None
        self.memos: Callable[[str, str | None, str], dict | None] | None = None
        self.clock: Callable[[], float] = time.monotonic
        self.hook_timeout = hook_timeout
        self._calls = Bound('plugin-hook')
        self._builds = Bound('plugin-client')
        self._link_calls = Bound('plugin-links')
        self._linking = threading.Lock()
        self._refreshing = threading.Lock()
        self._clients: dict[tuple[str, str, str], object] = {}
        self._clients_failed: dict[tuple[str, str, str], float] = {}
        self._clients_warned: set[tuple[str, str, str]] = set()
        self._building: dict[tuple[str, str], threading.Lock] = {}
        self.kube = None
        self.live = None
        self.sources = None
        self.migrate: Callable[[], list[str] | None] | None = None
        self.languages: Callable[[], None] | None = None
        self.after_refresh: Callable[[], None] | None = None
        self._failing: set[tuple[str, str]] = set()
        self._guard = threading.Lock()
        self._catching = threading.Lock()
        self._good: dict[str, dict[str, tuple[str, ...]]] = {}
        self._org_failing: set[str] = set()
        self._crowded: set[str] = set()
        self._links_failed: set[str] = set()
        self._release_runs: dict[tuple[str, str | None], tuple[dict, threading.Event]] = {}
        self._releasing = threading.Lock()
        self._releases_failed: set[str] = set()
        self._files: dict[tuple[str, str], tuple[bytes, str] | None] = {}
        self._checked: dict[tuple[str, str], list] = {}
        self._compiled: dict[tuple[str, str, str], regex.Pattern] = {}
        self._slow = SlowRules(log)
        self._warned: set[tuple] = set()
        self._pushed: dict[str, tuple[tuple, str]] = {}
        self._tests: dict[tuple[str, str | None], threading.Thread] = {}
        self._probing: dict[tuple[str, str], tuple[dict, threading.Event]] = {}

    def refresh(self) -> None:
        with self._refreshing:
            self._refresh()

    def _refresh(self) -> None:
        with self._guard:
            self._files = {}
            self._checked = {}
            self._compiled = {}
        steps = [('migrate', self._catch_up)] if self.migrate is not None else []
        after = [(name, part) for name, part in (('languages', self.languages), ('heal', self.after_refresh)) if part is not None]
        for name, part in steps + [('registry', self.registry.refresh), ('state', self.state.refresh), ('instances', self.instances.refresh), ('org', self._refresh_org)] + after:
            if name in ('registry', 'instances', 'org'):
                self._step(name, part)
                continue
            try:
                self._calls.call(('step', name), lambda name=name, part=part: self._step(name, part), self.hook_timeout)
            except TimedOut as e:
                self._step_failed(name, e)

    def _step(self, name: str, part: Callable[[], None]) -> None:
        try:
            part()
        except Exception as e:
            self._step_failed(name, e)
        else:
            with self._guard:
                self._failing = {k for k in self._failing if k[0] != name}

    def _step_failed(self, name: str, e: Exception) -> None:
        streak = (name, 'migration' if isinstance(e, MigrationFailed) else 'timeout' if isinstance(e, TimedOut) else 'error')
        with self._guard:
            first = streak not in self._failing
            self._failing.add(streak)
        if not first:
            return
        if isinstance(e, MigrationFailed):
            log.error('%s', msg('runtime.migration_failed', step=name, migration=e.migration or '-', error=e.error or kind(e)))
        else:
            log.warning('%s', msg('runtime.step_failed', step=name, error=kind(e)))

    def _catch_up(self) -> None:
        if not self._catching.acquire(blocking=False):
            return
        try:
            migrate = self.migrate
            if migrate is not None and migrate() is not None:
                self.migrate = None
        finally:
            self._catching.release()

    def set_enabled(self, plugin_id: str, on: bool, by: str) -> threading.Thread:
        self.state.set(plugin_id, on, by)
        thread = threading.Thread(target=self.refresh, name='plugin-switch-refresh', daemon=True)
        thread.start()
        return thread

    async def loop(self, every: float = REFRESH_S) -> None:
        while True:
            await asyncio.to_thread(self.refresh)
            await asyncio.sleep(every)

    def setting(self, plugin_id: str, key: str) -> object:
        return self.live.plugin_value(f'{plugin_id}.{key}')

    def rules(self, plugin_id: str, set_id: str) -> dict:
        return self.live.rules_of(f'{plugin_id}.{set_id}')

    def agent_fields(self, plugin_id: str) -> tuple[Field, ...]:
        return agent_fields(self.registry, plugin_id)

    def fields_of(self, plugin_id: str) -> list[dict]:
        found = self.registry.get(plugin_id)
        if found is None or found.plugin is None or found.state == 'broken':
            return []
        return [field_view(f) for f in effective_fields(found.plugin, self.instances.extra_fields(plugin_id))]

    def _agent(self, name: str) -> Instance:
        found = self.instances.get(AGENT_PLUGIN, name)
        if found is None:
            raise ReadError(404, msg('agent.unknown', label=name))
        if found.values.get('mode', 'agent') != 'agent':
            raise ReadError(400, msg('agent.not_agent', label=name))
        if not any(p.id == AGENT_PLUGIN for p in self.active()):
            raise ReadError(503, msg('agent.plugin_off', label=name))
        if not found.enabled:
            raise ReadError(503, msg('agent.disabled', label=name))
        if found.problem:
            raise ReadError(503, msg('agent.parked', label=name))
        return found

    def agent_from(self, info: InstanceInfo, testing: bool = False) -> RemoteAgent:
        config = info.config
        via = config.get('via') or ''
        relay = None
        if via:
            hub = next((i for i in self.instances.all(AGENT_PLUGIN) if i.name == via and i.values.get('mode', 'agent') == 'agent' and not i.problem and (testing or i.enabled)), None)
            if hub is None:
                raise AgentError(msg('agent.relay_off', label=info.name, relay=via))
            relay = (hub.values.get('url', ''), hub.secrets.get('token', ''))
        link = http_link(config.get('url', ''), config.get('token', ''), self.transport or remote.send, via=relay, via_label=via)
        return RemoteAgent(link, info.name if info.name and info.name != '-' else msg('agent.unsaved'))

    def agent_for(self, name: str) -> RemoteAgent:
        return self.agent_from(self._agent(name).info())

    def _agent_config(self, found: Instance) -> dict[str, str]:
        keys = {f.key for f in self.agent_fields(AGENT_PLUGIN)}
        patterns = tuple(k for p in self.active() for read in p.agent_reads for k in read.keys)
        given = {**found.values, **found.secrets}
        return {k: given[k] for k in sorted(keys) if given.get(k) and declared(k, patterns)}

    def agent_read(self, agent: str, op: str, args: dict) -> object:
        reads = {read.op: read for plugin in self.active() for read in plugin.agent_reads}
        read = reads.get(op) if isinstance(op, str) else None
        if read is None:
            raise ReadError(404, msg('agent.op_unknown', ops=', '.join(reads)) if reads else msg('agent.no_reads'))
        found = self._agent(agent)
        remote_agent = self.agent_for(agent)
        config = self._agent_config(found)
        rev = rev_of(config)
        target = (found.values.get('url'), found.values.get('via') or '')
        with self._guard:
            last = self._pushed.get(agent)
        if (last is None and rev) or (last is not None and last != (target, rev)):
            self._push(agent, remote_agent, target, rev, config)
        timeout = min(max(read.timeout, READ_TIMEOUT_MIN), READ_TIMEOUT_MAX)
        try:
            return remote_agent.read(op, args or {}, timeout)
        except AgentError as e:
            if e.code != 503 or not config:
                raise
        with self._guard:
            self._pushed.pop(agent, None)
        self._push(agent, remote_agent, target, rev, config)
        return remote_agent.read(op, args or {}, timeout)

    def _push(self, agent: str, remote_agent: RemoteAgent, target: tuple, rev: str, config: dict[str, str]) -> None:
        remote_agent.push_sources(rev, config)
        with self._guard:
            self._pushed[agent] = (target, rev)

    def active(self) -> list[Plugin]:
        return [p for p in self.registry.usable() if self.state.enabled(p.id)]

    def instances_of(self, plugin_id: str) -> list[Instance]:
        if not any(p.id == plugin_id for p in self.active()):
            return []
        return [i for i in self.instances.all(plugin_id) if i.enabled and not i.problem]

    def connection_test(self, plugin: Plugin, name: str | None, given: Mapping[str, str | None], timeout: float, now: float, reused: Callable[[], None] | None = None,
                        off_checks: Iterable[str] | None = None, tester: Callable[[Context], TestResult] | None = None) -> dict:
        run = tester or plugin.test
        if run is None:
            return {'ok': False, 'summary': msg('integrations.test_none'), 'ms': 0}
        stored = self.instances.get(plugin.id, name) if name else None
        if name and stored is None:
            raise InstanceMissing(msg('instances.unknown', name=name))
        extra = self.instances.extra_fields(plugin.id)
        every = effective_fields(plugin, extra)
        fields = {f.key: f for f in every}
        base = {f.key: f.default for f in every if f.default and f.kind != 'secret'}
        if stored is not None:
            base.update(stored.values)
        owner = address_owners(self.registry, plugin.id, extra)
        moved = moved_addresses(plugin, fields, owner, base, given)
        addressed = moved_addresses(plugin, fields, owner, base, given, trust=False)
        stored_secrets = dict(stored.secrets) if stored is not None else {}
        kept = {k: v for k, v in stored_secrets.items() if plugin.id not in moved and owner.get(k, plugin.id) not in moved}
        values = merged_values({**base, **kept}, given)
        validate(plugin, merged_values({}, given), partial=True, extra=extra)
        for key in stored_secrets:
            if key not in kept and key in fields and given.get(key) is None and visible(fields[key], values, fields):
                if key in stored.owned:
                    raise InvalidInstance(key, msg('integrations.reenter_owned', field=fields[key].label, source=stored.source))
                raise InvalidInstance(key, msg('integrations.reenter' if plugin.id in addressed or owner.get(key, plugin.id) in addressed else 'integrations.reenter_trust_test', field=fields[key].label))
        clean = validate(plugin, values, extra=extra)
        if plugin.id == AGENT_PLUGIN and kept and stored is not None:
            found = hub_problem(stored, clean.get('via'), self.instances.get(AGENT_PLUGIN, clean.get('via') or ''))
            if found is not None:
                raise InvalidInstance('via', found)
        plain = {k: v for k, v in clean.items() if fields[k].kind != 'secret'}
        secrets = {k: v for k, v in clean.items() if fields[k].kind == 'secret'}
        if any(given.get(k) is None for k in kept) and reused is not None:
            reused()
        off = checks_off(plugin, off_checks) if off_checks is not None else stored.off_checks if stored is not None else frozenset()
        temp = Instance(plugin.id, name or '-', bool(stored and stored.primary), True, plain, secrets, ADMIN, frozenset(), None, tuple(f.key for f in every if f.kind == 'secret'), off)
        box: dict = {}
        done = threading.Event()
        key = (plugin.id, name)

        def go() -> None:
            try:
                ctx = self.context(plugin, temp, now)
                ctx.testing = True
                box['value'] = run(ctx)
            except BaseException as e:
                box['error'] = e
            finally:
                with self._guard:
                    if self._tests.get(key) is threading.current_thread():
                        del self._tests[key]
                done.set()

        with self._guard:
            running = self._tests.get(key)
            if running is not None and running.is_alive():
                return {'ok': False, 'summary': msg('integrations.test_busy'), 'ms': 0}
            worker = self._tests[key] = threading.Thread(target=go, name=f'test-{plugin.id}', daemon=True)
        worker.start()
        began = time.monotonic()
        if not done.wait(timeout):
            return {'ok': False, 'summary': msg('integrations.test_timeout', seconds=f'{timeout:g}'), 'ms': int(timeout * 1000)}
        elapsed = int((time.monotonic() - began) * 1000)
        if 'error' in box:
            log.warning('%s', msg('integrations.test_failed_log', plugin=plugin.id, error=kind(box['error'])))
            return {'ok': False, 'summary': msg('integrations.test_failed', error=kind(box['error'])), 'ms': elapsed}
        found = box.get('value')
        if not isinstance(found, TestResult) or not isinstance(found.ok, bool) or not isinstance(found.summary, str):
            return {'ok': False, 'summary': msg('integrations.test_bad'), 'ms': elapsed}
        text = found.summary
        for secret in secrets.values():
            text = text.replace(secret, '***')
        text = ''.join(ch for ch in text if ch.isprintable())
        for secret in secrets.values():
            shown = ''.join(ch for ch in secret if ch.isprintable())
            if shown:
                text = text.replace(shown, '***')
        ms = found.ms if isinstance(found.ms, int) and not isinstance(found.ms, bool) and found.ms >= 0 else elapsed
        return {'ok': found.ok, 'summary': text[:TEST_SUMMARY_MAX], 'ms': ms}

    def serving(self) -> list[tuple[Plugin, list[Instance]]]:
        out = []
        for plugin in self.active():
            found = [i for i in self.instances.all(plugin.id) if i.enabled and not i.problem]
            if found or not plugin.fields:
                out.append((plugin, found))
        return out

    def client(self, plugin_id: str, name: str | None = None) -> object | None:
        if plugin_id in self.clients:
            return self.clients[plugin_id]
        plugin = next((p for p in self.active() if p.id == plugin_id), None)
        if plugin is None or plugin.client is None:
            return None
        found = self.instances_of(plugin_id)
        if name is None:
            primary = self.instances.primary(plugin_id)
            chosen = next((i for i in found if primary is not None and i.name == primary.name), None)
        else:
            chosen = next((i for i in found if i.name == name), None)
        if chosen is None:
            return None
        info = chosen.info()
        key = (plugin_id, chosen.name, hashlib.sha256(json.dumps(dict(info.config), sort_keys=True).encode()).hexdigest()[:16])
        known, found = self._cached_client(key)
        if known:
            return found
        deadline = time.monotonic() + self.hook_timeout
        with self._guard:
            gate = self._building.setdefault(key[:2], threading.Lock())
        if not gate.acquire(timeout=self.hook_timeout):
            return None
        try:
            known, found = self._cached_client(key)
            left = deadline - time.monotonic()
            return found if known or left <= 0 else self._build_client(plugin, info, key, left)
        finally:
            gate.release()

    def _cached_client(self, key: tuple[str, str, str]) -> tuple[bool, object | None]:
        with self._guard:
            held = self._clients.get(key)
            failed_at = self._clients_failed.get(key)
        if held is not None:
            return True, held
        if failed_at is not None and self.clock() - failed_at < CLIENT_RETRY_S:
            return True, None
        return False, None

    def _build_client(self, plugin: Plugin, info: InstanceInfo, key: tuple[str, str, str], timeout: float) -> object | None:
        try:
            built = self._builds.call(key, lambda: plugin.client(info, self.transport_for(plugin, info)), timeout)
        except Exception as e:
            with self._guard:
                first = key not in self._clients_warned
                self._clients_warned.add(key)
                for old in [k for k in self._clients_failed if k[:2] == key[:2] and k != key]:
                    del self._clients_failed[old]
                self._clients_failed[key] = self.clock()
            if first:
                log.warning('%s', msg('runtime.client_failed', plugin=plugin.id, error=kind(e)))
            return None
        with self._guard:
            for old in [k for k in self._clients if k[:2] == key[:2] and k != key]:
                del self._clients[old]
            self._clients_failed.pop(key, None)
            return self._clients.setdefault(key, built)

    def proxy(self, plugin_id: str) -> LiveClient:
        return LiveClient(self, plugin_id)

    def plugin_name(self, plugin_id: str) -> str:
        found = self.registry.get(plugin_id)
        name = found.plugin.name if found is not None and found.plugin is not None else None
        return name if isinstance(name, str) and name.strip() else plugin_id

    def transport_for(self, plugin: Plugin, info: InstanceInfo | None) -> Transport:
        if self.transport is not None:
            return self.transport
        pem, pinned = ca_of(plugin, info.config if info is not None else {})
        return pem_transport(pem, pinned) if pem else remote.send

    def peer_of(self, plugin: Plugin, instance: Instance) -> Peer:
        info = instance.info()
        transport = self.transport_for(plugin, info)
        return Peer(info, lambda: transport, frozenset(instance.off_checks))

    def knowledge(self) -> list[Knowledge]:
        out: list[Knowledge] = []
        used = 0
        full = False
        for plugin, found in self.serving():
            if held_knowledge(plugin, found):
                continue
            for k in plugin.knowledge:
                shown = dataclasses.replace(k, text=f'From plug-in {plugin.name}.\n\n{k.text.strip()}')
                size = len(f'\n## {shown.title} {{#{shown.id}}}\n\n{shown.text.strip()}\n')
                if full or used + size > KNOWLEDGE_TOTAL:
                    full = True
                    if plugin.id not in self._crowded:
                        self._crowded.add(plugin.id)
                        log.warning('%s', msg('plugins.knowledge.skipped', plugin=plugin.name, limit=KNOWLEDGE_TOTAL))
                    continue
                used += size
                out.append(shown)
        return out

    def context(self, plugin: Plugin, instance: Instance | None, now: float, reads=None) -> RunContext:
        return RunContext(self, plugin, instance, now, reads)

    def linker(self) -> Callable[[dict], list[dict] | None]:
        deadline = time.monotonic() + self.hook_timeout
        return lambda card: self.links(card, deadline)

    def links(self, card: dict, deadline: float | None = None) -> list[dict] | None:
        end = time.monotonic() + self.hook_timeout if deadline is None else deadline
        if not self._linking.acquire(timeout=max(0.0, end - time.monotonic())):
            return None
        try:
            return self._links(card, end)
        finally:
            self._linking.release()

    def _links(self, card: dict, end: float) -> list[dict] | None:
        named = card.get('cluster') if isinstance(card.get('cluster'), str) else None
        active = self.active()
        owner = self._owner(card, active)
        out: list[dict] = []
        seen: set[str] = set()
        for plugin in sorted((p for p in active if p.links is not None), key=lambda p: p is not owner):
            info = self._link_instance(plugin, named if plugin is owner else None)
            if info is None:
                continue
            left = end - time.monotonic()
            if left <= 0:
                continue
            try:
                got = self._link_calls.call(plugin.id, lambda plugin=plugin, info=info: plugin.links(card, info), left)
            except Exception as e:
                with self._guard:
                    first = plugin.id not in self._links_failed
                    self._links_failed.add(plugin.id)
                if first:
                    log.warning('%s', msg('runtime.links_failed', plugin=plugin.id, error=kind(e)))
                continue
            with self._guard:
                self._links_failed.discard(plugin.id)
            for link in got if isinstance(got, list) else []:
                label, url = (link.get('label'), link.get('url')) if isinstance(link, dict) else (None, None)
                if isinstance(label, str) and isinstance(url, str) and LINK_URL.fullmatch(url) and url not in seen:
                    seen.add(url)
                    out.append({'label': label, 'url': url})
        return out[:LINKS_MAX] or None

    def _owner(self, card: dict, active: list[Plugin]) -> Plugin | None:
        for key in (_stem(card), card.get('src')):
            if isinstance(key, str) and key:
                found = next((p for p in active if any(key in c.ran for c in p.checks)), None)
                if found is not None:
                    return found
        return None

    def _link_instance(self, plugin: Plugin, named: str | None) -> InstanceInfo | None:
        found = self.instances.get(plugin.id, named) if named else self.instances.primary(plugin.id)
        if found is not None:
            return None if not found.enabled or found.problem else found.info()
        return None if plugin.fields else InstanceInfo(plugin.id, True, None, {})

    def _row_instance(self, plugin: Plugin, found: list[Instance], row: dict) -> InstanceInfo | None:
        named = row.get('cluster')
        if isinstance(named, str) and named:
            chosen = next((i for i in found if i.name == named), None)
        else:
            primary = self.instances.primary(plugin.id)
            chosen = next((i for i in found if primary is not None and i.name == primary.name), None)
        return chosen.info() if chosen is not None else None

    def row_links(self, report: dict) -> dict:
        offered = [(p, found) for p, found in self.serving() if p.row_links is not None and found]
        rows = report.get('rows') if isinstance(report, dict) else None
        if not offered or not isinstance(rows, list):
            return report
        window = {'from': report.get('windowFrom'), 'to': report.get('windowTo')}
        for row in rows:
            if not isinstance(row, dict):
                continue
            for plugin, found in offered:
                info = self._row_instance(plugin, found, row)
                if info is None:
                    continue
                try:
                    got = plugin.row_links(copy.deepcopy(row), dict(window), info)
                except Exception as e:
                    self._warn(('row-links', plugin.id), 'plug-in %s: row_links failed, skipping it (%s)', plugin.id, kind(e))
                    continue
                if not isinstance(got, dict):
                    continue
                links = dict(row.get('links') or {})
                for name, url in got.items():
                    if len(links) < LINKS_PER_ROW and isinstance(name, str) and isinstance(url, str) and LINK_NAME.fullmatch(name) and LINK_URL.fullmatch(url):
                        links.setdefault(name, url)
                if links:
                    row['links'] = links
        return report

    def releases(self, read: Callable[[str], object], since: float) -> list[dict]:
        hooked = [(plugin, found) for plugin, found in self.serving() if plugin.releases is not None]
        if not hooked:
            return []
        if not self._releasing.acquire(timeout=self.hook_timeout):
            self._warn(('releases-busy',), 'release rows left out: an earlier read still held them after %s s', f'{self.hook_timeout:g}')
            return []
        try:
            calls = [(plugin, segment, self._release_run(plugin, segment, rows, since)) for plugin, found in hooked
                     for segment, rows in self._release_groups(plugin, found, read).items()]
            deadline = time.monotonic() + self.hook_timeout
            kept: dict[str, list[dict]] = {}
            failed: dict[str, str] = {}
            for plugin, segment, run in calls:
                why = self._release_wait(run, deadline)
                if why is not None:
                    failed.setdefault(plugin.id, why)
                    continue
                got = run[0]['value']
                kept.setdefault(plugin.id, []).extend(r for r in (self._release_row(plugin, segment, item, since) for item in got) if r is not None)
        finally:
            self._releasing.release()
        out: list[dict] = []
        for plugin, _ in hooked:
            if plugin.id in failed:
                self._release_failed(plugin, failed[plugin.id])
                continue
            with self._guard:
                self._releases_failed.discard(plugin.id)
            out += kept.get(plugin.id, [])
        return out

    def _release_groups(self, plugin: Plugin, found: list[Instance], read: Callable[[str], object]) -> dict[str | None, list[dict]]:
        segments = {None if i.primary else i.name for i in found} if plugin.fields else {None}
        groups: dict[str | None, list[dict]] = {}
        for section in dict.fromkeys(s for c in plugin.checks for s in c.sections):
            value = read(section)
            for row in stored_rows({} if value is None else {section: value}, section):
                if isinstance(row, dict) and plugin_of(row) == plugin.id and (row.get('cluster') or None) in segments:
                    groups.setdefault(row.get('cluster') or None, []).append(copy.deepcopy(row))
        return groups

    def _release_run(self, plugin: Plugin, segment: str | None, rows: list[dict], since: float) -> tuple[dict, threading.Event] | None:
        key = (plugin.id, segment)
        box: dict = {}
        done = threading.Event()
        with self._guard:
            held = self._release_runs.get(key)
            if held is not None and not held[1].is_set():
                return None
            self._release_runs[key] = (box, done)

        def go() -> None:
            try:
                box['value'] = plugin.releases(rows, since)
            except BaseException as e:
                box['error'] = kind(e)
            finally:
                done.set()

        threading.Thread(target=go, name=f'plugin-releases-{plugin.id}', daemon=True).start()
        return box, done

    def _release_wait(self, run: tuple[dict, threading.Event] | None, deadline: float) -> str | None:
        if run is None or not run[1].wait(max(0.0, deadline - time.monotonic())):
            return TimedOut.__name__
        box = run[0]
        if 'error' in box:
            return box['error']
        return None if isinstance(box.get('value'), (list, tuple)) else type(box.get('value')).__name__

    def _release_failed(self, plugin: Plugin, why: str) -> None:
        with self._guard:
            first = plugin.id not in self._releases_failed
            self._releases_failed.add(plugin.id)
        if first:
            log.warning('plug-in %s: releases failed, its rows are left out (%s)', plugin.id, why)

    def _release_row(self, plugin: Plugin, segment: str | None, row: object, since: float) -> dict | None:
        if not isinstance(row, dict) or not all(isinstance(row.get(k), str) and row[k] for k in RELEASE_TEXT):
            return None
        out = {k: row[k][:RELEASE_CHARS] for k in RELEASE_KEYS if isinstance(row.get(k), str)}
        if plugin.id not in RELEASE_KEYS and isinstance(row.get(plugin.id), dict):
            out[plugin.id] = capped(row[plugin.id])
        try:
            at = parse_iso(row['at'])
            json.dumps(out, allow_nan=False)
        except (TypeError, ValueError, OverflowError, RecursionError):
            return None
        if at < since:
            return None
        out['kind'] = plugin.id
        if segment is not None:
            out['cluster'] = segment
            if 'id' in out:
                out['id'] = f"{out['id']}-{segment}"
        return out

    def _refresh_org(self) -> None:
        hooks = self._hooks('org_validators')
        names = {p.id for p, _ in hooks}
        with self._guard:
            self._good = {k: v for k, v in self._good.items() if k in names}
            self._org_failing &= names
        for plugin, hook in hooks:
            try:
                self._calls.call(('org', plugin.id), lambda plugin=plugin, hook=hook: self._org_of(plugin, hook), self.hook_timeout)
            except Exception as e:
                with self._guard:
                    first = plugin.id not in self._org_failing
                    self._org_failing.add(plugin.id)
                if first:
                    log.warning('plug-in %s: org_validators gave no usable answer, keeping its last good values (%s)', plugin.id, type(e).__name__)

    def _org_of(self, plugin: Plugin, hook: Callable[[], dict]) -> None:
        raw = hook()
        if not isinstance(raw, dict):
            raise ValueError('not a dict')
        domains = raw.get('email_domains')
        if domains is not None and (not isinstance(domains, (list, tuple)) or not all(isinstance(v, str) and v.strip() for v in domains)):
            raise ValueError('email_domains must be a list of text')
        found: dict[str, tuple[str, ...]] = {}
        for name in ORG_KEYS:
            value = raw.get(name)
            if value is None:
                continue
            if not isinstance(value, (list, tuple)):
                self._warn(('org-key', plugin.id, name), 'plug-in %s: org_validators %s is not a list; skipping it', plugin.id, name)
                continue
            found[name] = tuple(dict.fromkeys(v.strip() for v in value if isinstance(v, str) and v.strip()))
        with self._guard:
            self._good[plugin.id] = found
            self._org_failing.discard(plugin.id)

    def org(self) -> dict:
        with self._guard:
            good = dict(self._good)
        found: dict[str, list[str]] = {k: [] for k in ORG_KEYS}
        closed = False
        for plugin, _ in self._hooks('org_validators'):
            given = good.get(plugin.id)
            if given is None:
                closed = True
                continue
            for name, values in given.items():
                found[name].extend(values)
        return {name: tuple(dict.fromkeys(values)) for name, values in found.items()} | {'email_closed': closed}

    def org_packs(self) -> list[Plugin]:
        return [p for p in self.active() if p.org_pack is not None]

    def org_pack(self) -> tuple[str, OrgPack] | None:
        found = self.org_packs()
        if not found:
            return None
        for other in found[1:]:
            self._warn(('org-pack', other.id), '%s', msg('orgpack.ignored', name=other.name, used=found[0].name))
        return found[0].id, found[0].org_pack

    def _warn(self, key: tuple, text: str, *args) -> None:
        with self._guard:
            if key in self._warned:
                return
            self._warned.add(key)
        log.warning(text, *args)

    def _plugins(self) -> list[Plugin]:
        return [plugin for plugin, _ in self.serving()]

    def _hooks(self, name: str) -> list[tuple[Plugin, Callable]]:
        return [(p, getattr(p.hooks, name)) for p in self._plugins() if getattr(p.hooks, name) is not None]

    def _valid(self, plugin: Plugin, kind: str) -> list:
        key = (plugin.id, kind)
        with self._guard:
            held = self._checked.get(key)
        if held is not None:
            return held
        out = []
        for index, item in enumerate(getattr(plugin, kind)):
            found = problem(kind, item)
            if found:
                self._warn((kind, plugin.id, index), 'plug-in %s: skipping %s item %d: %s', plugin.id, kind, index, found)
            else:
                out.append(item)
        with self._guard:
            self._checked[key] = out
        return out

    def redaction_rules(self) -> tuple[tuple[tuple[str, str], regex.Pattern], ...]:
        found = []
        for plugin in self._plugins():
            for rule in self._valid(plugin, 'redactions'):
                key = (plugin.id, rule.id, rule.pattern)
                with self._guard:
                    pattern = self._compiled.get(key)
                if pattern is None:
                    pattern = regex.compile(rule.pattern)
                    with self._guard:
                        self._compiled[key] = pattern
                found.append(((plugin.id, rule.id), pattern))
        return tuple(found)

    def redaction_timed_out(self, owner: tuple[str, str] | None) -> None:
        self._slow(owner)

    def tag_defs(self) -> dict[str, str]:
        out: dict[str, str] = {}
        owner: dict[str, str] = {}
        for plugin in self._plugins():
            for tag in self._valid(plugin, 'tags'):
                if tag.id in tags.TAGS:
                    self._warn(('tag-core', plugin.id, tag.id), 'plug-in %s: its tag %s is ignored, core already has a tag of that name', plugin.id, tag.id)
                elif tag.id in owner and owner[tag.id] != plugin.id:
                    self._warn(('tag-taken', plugin.id, tag.id), 'plug-in %s: its tag %s is ignored, plug-in %s already has it', plugin.id, tag.id, owner[tag.id])
                else:
                    owner[tag.id] = plugin.id
                    out[tag.id] = tags.clean_label(tag.label)
        return out

    def tag_rules(self) -> dict[str, tuple[str, ...]]:
        out: dict[str, tuple[str, ...]] = {}
        known = {**tags.TAGS, **self.tag_defs()}
        for plugin in self._plugins():
            for rule in self._valid(plugin, 'tag_rules'):
                if rule.tag not in known:
                    self._warn(('tag', plugin.id, rule.tag), 'plug-in %s: its tag rule for %s is ignored, there is no tag of that name', plugin.id, rule.tag)
                out[rule.tag] = tuple(dict.fromkeys((*out.get(rule.tag, ()), *rule.terms)))
        return out

    def ui_hosts(self) -> tuple[str, ...]:
        hosts = set()
        for plugin, items in self.serving():
            keys = [f.key for f in plugin.fields if f.ui]
            fallback = [f.key for f in plugin.fields if f.kind == 'url' and not f.ui][:1]
            for item in items:
                shown = [k for k in keys if item.values.get(k)] or (fallback if keys else [])
                for field_key in shown:
                    try:
                        host = urlsplit(str(item.values.get(field_key) or '')).hostname
                    except ValueError:
                        host = None
                    if host:
                        hosts.add(host)
        return tuple(sorted(hosts))

    def role_resolvers(self) -> list[Callable]:
        return [hook for _, hook in self._hooks('role_resolver')]

    def role_resolvers_named(self) -> list[tuple[str, Callable]]:
        return [(plugin.id, hook) for plugin, hook in self._hooks('role_resolver')]

    def _probe_hook(self, plugin: Plugin, item: Instance) -> tuple[dict, threading.Event]:
        box: dict = {}
        done = threading.Event()
        with self._guard:
            held = self._probing.get((plugin.id, item.name))
            if held is not None and not held[1].is_set():
                return held
            self._probing[(plugin.id, item.name)] = (box, done)

        def go() -> None:
            try:
                box['value'] = plugin.probe_targets(item.info())
            except BaseException as e:
                box['error'] = type(e).__name__
            finally:
                done.set()

        threading.Thread(target=go, name=f'probe-targets-{plugin.id}', daemon=True).start()
        return box, done

    def probe_targets(self) -> list[dict]:
        out = []
        calls = [(plugin, item, self._probe_hook(plugin, item)) for plugin, items in self.serving() if plugin.probe_targets is not None for item in items]
        deadline = time.monotonic() + PROBE_HOOK_S
        for plugin, item, (box, done) in calls:
            if not done.wait(max(0.0, deadline - time.monotonic())):
                log.warning('plug-in %s: probe targets of instance %s did not come within %s s, skipping them', plugin.id, item.name, f'{PROBE_HOOK_S:g}')
                continue
            if 'error' in box:
                log.warning('plug-in %s: probe targets of instance %s failed, skipping them (%s)', plugin.id, item.name, box['error'])
                continue
            listed = box.get('value')
            if not isinstance(listed, (list, tuple)):
                log.warning('plug-in %s: probe targets of instance %s are not a list, skipping them', plugin.id, item.name)
                continue
            pem, pinned = ca_of(plugin, item.values)
            out.extend({'name': t['name'], 'url': t['url'], 'plugin': plugin.id, 'instance': item.name, **({'pem': pem} if pem else {}), **({'pinned': True} if pinned else {})}
                       for t in listed if isinstance(t, dict) and isinstance(t.get('name'), str) and t['name'] and isinstance(t.get('url'), str) and t['url'])
        return out

    def routes(self) -> list[Route]:
        out = []
        for plugin, items in self.serving():
            if plugin.outbound is None:
                continue
            for item in items:
                parsed = route_of(plugin, item)
                if parsed.unknown:
                    self._warn(('route-tags', plugin.id, item.name, parsed.unknown), 'plug-in %s: route %s takes %s, which no card can carry; the tags are %s',
                               plugin.id, item.name, ', '.join(parsed.unknown), ', '.join(tags.all_tags()))
                if parsed.dropped:
                    self._warn(('route-mentions', plugin.id, item.name, parsed.dropped), 'plug-in %s: route %s mentions %d %s not an email address; left out',
                               plugin.id, item.name, parsed.dropped, 'entry that is' if parsed.dropped == 1 else 'entries that are')
                if parsed.route is None:
                    self._warn(('route', plugin.id, item.name, parsed.problem), 'plug-in %s: route %s is skipped: %s', plugin.id, item.name, parsed.problem)
                    continue
                out.append(parsed.route)
        return sorted(out, key=lambda r: (r.plugin, r.instance))

    def route_problem(self, plugin: Plugin, item: Instance) -> str | None:
        return None if plugin.outbound is None else route_of(plugin, item).problem

    def locales(self) -> list[tuple[str, Locale]]:
        return [(plugin.id, locale) for plugin in self._plugins() for locale in self._valid(plugin, 'locales')]

    def panels(self) -> list[tuple[str, Panel]]:
        return [(plugin.id, panel) for plugin in self._plugins() for panel in self._valid(plugin, 'panels')]

    def _packs(self, plugin: Plugin) -> list[str]:
        return [f'i18n-{lang}.js' for lang in dict.fromkeys(locale.lang for locale in self._valid(plugin, 'locales') if on_page(locale))]

    def page_packs(self) -> list[str]:
        return [f'/plugins/{plugin.id}/{file}' for plugin in self._plugins() for file in self._packs(plugin)]

    def page_scripts(self) -> list[str]:
        out: list[str] = []
        for plugin in self._plugins():
            files = self._packs(plugin) + [panel.script for panel in self._valid(plugin, 'panels') if panel.script]
            out.extend(f'/plugins/{plugin.id}/{file}' for file in dict.fromkeys(files))
        return out

    def token_request(self, request: dict) -> dict:
        out: dict[str, dict[str, str]] = {'headers': {}, 'params': {}}
        for plugin, hook in self._hooks('oidc_token_request'):
            try:
                raw = hook(copy.deepcopy(request))
            except Exception as e:
                log.warning('plug-in %s: oidc_token_request failed, so the login stops (%s)', plugin.id, type(e).__name__)
                raise
            if not isinstance(raw, dict):
                log.warning('plug-in %s: oidc_token_request returned %s, not a dict; skipping it', plugin.id, type(raw).__name__)
                continue
            for name in out:
                given = raw.get(name)
                if not isinstance(given, dict):
                    continue
                for k, v in given.items():
                    if not isinstance(k, str) or not isinstance(v, (str, int)) or isinstance(v, bool):
                        continue
                    if name == 'headers' and not self._header_ok(plugin.id, k, str(v)):
                        continue
                    out[name][k] = str(v)
        return out

    def _header_ok(self, plugin_id: str, name: str, value: str) -> bool:
        named = bool(HEADER_NAME.fullmatch(name))
        ok = named and name.lower() not in PROTECTED_HEADERS and not name.lower().startswith(remote.MARKER) and bool(HEADER_VALUE.fullmatch(value))
        if ok:
            try:
                value.encode('latin-1')
            except UnicodeEncodeError:
                ok = False
        if not ok:
            shown = name if named else 'a header with an invalid name'
            self._warn(('header', plugin_id, shown.lower()), 'plug-in %s: oidc_token_request header %s refused', plugin_id, shown)
        return ok

    def _file(self, found, plugin_id: str, file: str) -> tuple[bytes, str] | None:
        key = (plugin_id, file)
        with self._guard:
            if key in self._files:
                return self._files[key]
        suffix = file[file.rfind('.'):] if '.' in file else ''
        result = None
        if suffix in ASSET_TYPES and found.directory is not None:
            try:
                root = found.directory.resolve()
                path = (root / file).resolve()
                if path.is_relative_to(root) and path.is_file() and path.stat().st_size <= ASSET_MAX_BYTES:
                    result = (path.read_bytes(), ASSET_TYPES[suffix])
            except (OSError, ValueError):
                result = None
        with self._guard:
            self._files[key] = result
        return result

    def asset(self, plugin_id: str, file: str) -> tuple[bytes, str] | None:
        if not isinstance(file, str) or not file or file in ('.', '..') or any(c in file for c in '/\\\x00'):
            return None
        found = self.registry.get(plugin_id)
        if found is None or found.plugin is None or found.state == 'broken':
            return None
        if file == ICON_FILE:
            icon = self.registry.icon(plugin_id)
            return None if icon is None else (icon.encode('utf-8'), 'image/svg+xml')
        plugin = next((p for p in self._plugins() if p.id == plugin_id), None)
        if plugin is None:
            return None
        lang = I18N_FILE.fullmatch(file)
        if lang:
            mine = [locale for locale in self._valid(plugin, 'locales') if locale.lang == lang[1]]
            if not mine:
                return None
            return (PAGE_PACK % (json.dumps(lang[1]), json.dumps(page_pack(mine)))).encode('utf-8'), ASSET_TYPES['.js']
        declared = {name for panel in self._valid(plugin, 'panels') for name in (panel.script, panel.fragment) if name}
        if file not in declared:
            return None
        return self._file(found, plugin_id, file)
