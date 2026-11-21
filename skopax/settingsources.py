import dataclasses
import logging
import os
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from skopax.bounded import HOOK_TIMEOUT_S, Bound, TimedOut
from skopax.config import ConfigurationError, Settings, load_settings, resolve
from skopax.contract import Plugin, SourceData
from skopax.instances import AGENT_PLUGIN, InvalidInstance, agent_fields, check_name, validate, via_problem
from skopax.investigator.prompt import core_anchors
from skopax.investigator.tools import CORE_TOOLS
from skopax.messages import msg
from skopax.mcptools import KNOWLEDGE_SECTION
from skopax.registry import Registry
from skopax.store import iso, utc_now
from skopax.tunables import CONFIG_KEY, LINKS_KEY, OLD_LINKS_KEY, STATIC

log = logging.getLogger('skopax.settingsources')

FILE_ENV = 'SKOPAX_CONFIG_FILE'
DEFAULT_FILE = '/etc/skopax/skopax.yaml'
REFRESH_S = 60
LABEL_ENV = 'environment'
LABEL_FILE = 'config file'
FILE_KEYS = ('settings', 'plugins')
OLD_CONNECTION_KEYS = ('gitlab.token', 'gitlab.group', 'jira.url', 'jira.email', 'jira.api-token', 'jira.cloud-id', 'slack.bot-token', 'slack.app-token')
INSTANCE_META = ('plugin', 'name', 'primary', 'enabled')


def env_source(env: Mapping[str, str], registry: Registry, timeout: float | None = None) -> SourceData:
    found: list[dict] = []
    bound = Bound('settings-source')
    for plugin in registry.usable():
        if plugin.env_instances is None:
            continue
        try:
            listed = list(plugin.env_instances(env)) if timeout is None else bound.call(plugin.id, lambda plugin=plugin: list(plugin.env_instances(env)), timeout)
            for raw in listed:
                found.append({**raw, 'plugin': plugin.id})
        except TimedOut:
            raise ConfigurationError(msg('settings.hook.failed', plugin=plugin.name, error='TimedOut')) from None
        except Exception as e:
            raise ConfigurationError(msg('settings.env.failed', plugin=plugin.name, error=type(e).__name__)) from None
    try:
        return SourceData.of({'label': LABEL_ENV, 'instances': found}, LABEL_ENV)
    except ValueError as e:
        raise ConfigurationError(msg('settings.env.failed', plugin=LABEL_ENV, error=type(e).__name__)) from None


def _read(path: str) -> object:
    try:
        text = Path(path).read_text(encoding='utf-8')
    except FileNotFoundError:
        return None
    except (OSError, UnicodeError) as e:
        raise ConfigurationError(msg('settings.file.unreadable', path=path, error=type(e).__name__)) from None
    try:
        return {} if (raw := yaml.safe_load(text)) is None else raw
    except yaml.YAMLError as e:
        mark = getattr(e, 'problem_mark', None)
        where = f'line {mark.line + 1}' if mark is not None else 'unknown position'
        raise ConfigurationError(msg('settings.file.parse', path=path, error=where)) from None


def _fill(path: str, where: str, raw: object, env: Mapping[str, str]) -> object:
    if isinstance(raw, str):
        return resolve(raw, dict(env))
    if raw is None or isinstance(raw, (bool, int, float)):
        return raw
    raise ConfigurationError(msg('settings.file.value', path=path, where=where, expected='a single value'))


def file_source(path: str, env: Mapping[str, str]) -> SourceData | None:
    raw = _read(path)
    if raw is None:
        return None
    keys = ', '.join(FILE_KEYS)
    if not isinstance(raw, dict):
        raise ConfigurationError(msg('settings.file.shape', path=path, keys=keys))
    for key in raw:
        if key not in FILE_KEYS:
            raise ConfigurationError(msg('settings.file.unknown', path=path, key=key, keys=keys))
    values = {} if raw.get('settings') is None else raw['settings']
    plugins = {} if raw.get('plugins') is None else raw['plugins']
    if not isinstance(values, dict) or not all(isinstance(k, str) for k in values):
        raise ConfigurationError(msg('settings.file.value', path=path, where='settings', expected='a mapping of keys to single values'))
    if not isinstance(plugins, dict):
        raise ConfigurationError(msg('settings.file.value', path=path, where='plugins', expected='a mapping of plug-in ids'))
    filled = {k: _fill(path, f'settings.{k}', v, env) for k, v in values.items()}
    instances: list[dict] = []
    for plugin_id, body in plugins.items():
        where = f'plugins.{plugin_id}'
        listed = body.get('instances') if isinstance(body, dict) and set(body) <= {'instances'} else None
        if not isinstance(plugin_id, str) or not isinstance(listed, list):
            raise ConfigurationError(msg('settings.file.value', path=path, where=where, expected='a mapping with a list named instances'))
        for index, item in enumerate(listed):
            if not isinstance(item, dict) or not all(isinstance(k, str) for k in item):
                raise ConfigurationError(msg('settings.file.value', path=path, where=f'{where}.instances[{index}]', expected='a mapping'))
            instances.append({**{k: _fill(path, f'{where}.instances[{index}].{k}', v, env) for k, v in item.items()}, 'plugin': plugin_id})
    try:
        return SourceData.of({'label': LABEL_FILE, 'values': filled, 'instances': instances}, LABEL_FILE)
    except ValueError as e:
        raise ConfigurationError(msg('settings.file.value', path=path, where='file', expected='a mapping of single values')) from None


def _hook_data(plugin: Plugin, env: Mapping[str, str]) -> SourceData | None:
    try:
        raw = plugin.hooks.settings_source(dict(env))
        return None if raw is None else SourceData.of(raw, plugin.name)
    except Exception as e:
        raise ConfigurationError(msg('settings.hook.failed', plugin=plugin.name, error=type(e).__name__)) from None


def hook_sources(registry: Registry, env: Mapping[str, str]) -> list[SourceData]:
    return [d for d in (_hook_data(p, env) for p in registry.usable() if p.hooks.settings_source is not None) if d is not None]


def merged(sources: list[SourceData]) -> dict[str, str]:
    out: dict[str, str] = {}
    for data in sources:
        out.update(data.values)
    return out


def labelled(sources: list[SourceData]) -> dict[str, str]:
    out: dict[str, str] = {}
    for data in sources:
        out.update({key: data.label for key in data.values})
    return out


def _check_instances(registry: Registry, data: SourceData, skipped: set[str]) -> None:
    for raw in data.instances:
        plugin_id = raw.get('plugin', '')
        found = registry.get(plugin_id)
        if found is None:
            raise ConfigurationError(msg('settings.instance.unregistered', plugin=plugin_id, source=data.label))
        if found.plugin is None or found.state != 'ok':
            if plugin_id not in skipped:
                skipped.add(plugin_id)
                log.warning('%s', msg('settings.instance.skipped', plugin=plugin_id, source=data.label))
            continue
        name = raw.get('name', '')
        try:
            check_name(registry, name, found.id)
            validate(found.plugin, {k: v for k, v in raw.items() if k not in INSTANCE_META}, extra=agent_fields(registry, found.id))
        except InvalidInstance as e:
            raise ConfigurationError(msg('settings.instance.invalid', plugin=found.id, name=name, source=data.label, field=e.field, problem=e.text)) from None
        via = raw.get('via')
        others = {r.get('name'): r for r in data.instances if r.get('plugin') == AGENT_PLUGIN and r.get('name') != name}
        if found.id == AGENT_PLUGIN and via and (via == name or via in others):
            problem = via_problem(name, raw, others)
            if problem:
                raise ConfigurationError(msg('settings.instance.invalid', plugin=found.id, name=name, source=data.label, field='via', problem=problem))


class SourceSet:
    def __init__(self, loaders: list[tuple[str, Callable[[], SourceData | None]]], initial: Mapping[str, SourceData | None], every: float = REFRESH_S, clock=time.monotonic,
                 failed: Mapping[str, str] | None = None, timeout: float = HOOK_TIMEOUT_S):
        self.loaders = list(loaders)
        self.timeout = timeout
        self._bound = Bound('settings-source')
        self.every = every
        self.clock = clock
        self._lock = threading.Lock()
        failed = dict(failed or {})
        labels = [label for label, _ in self.loaders]
        if len(set(labels)) != len(labels) or set(initial) != set(labels) or not set(failed) <= set(labels):
            raise ValueError(msg('settings.source.mismatch', loaders=', '.join(labels), initial=', '.join(sorted(initial))))
        self._data: list[SourceData | None] = [initial[label] for label in labels]
        stamp = iso(utc_now())
        self._status: dict[str, dict] = {label: {'ok': label not in failed, 'since': stamp, 'error': failed.get(label)} for label, _ in self.loaders}
        self._warned: set[str] = set(failed)
        self._waiting: set[str] = set(failed)

    def current(self) -> list[SourceData]:
        with self._lock:
            return [d for d in self._data if d is not None]

    def status(self) -> dict[str, dict]:
        with self._lock:
            return {label: dict(item) for label, item in self._status.items()}

    def unanswered(self) -> list[str]:
        with self._lock:
            return [label for label, _ in self.loaders if label in self._waiting]

    def refresh(self) -> bool:
        changed = False
        for index, (label, load) in enumerate(self.loaders):
            try:
                data = self._bound.call(label, load, self.timeout)
            except Exception as e:
                self._failed(label, type(e).__name__)
                continue
            with self._lock:
                changed = changed or self._data[index] != data
                self._data[index] = data
                if not self._status[label]['ok']:
                    self._status[label] = {'ok': True, 'since': iso(utc_now()), 'error': None}
                self._warned.discard(label)
                self._waiting.discard(label)
        return changed

    def _failed(self, label: str, error: str) -> None:
        with self._lock:
            first = label not in self._warned
            self._warned.add(label)
            if self._status[label]['ok']:
                self._status[label] = {'ok': False, 'since': iso(utc_now()), 'error': error}
            else:
                self._status[label]['error'] = error
        if first:
            log.warning('%s', msg('settings.source.kept', source=label, error=error))


@dataclass
class Boot:
    env: Mapping[str, str]
    registry: Registry
    sources: list[SourceData]
    settings: Settings | None
    loaders: list[tuple[str, Callable[[], SourceData | None]]] = field(default_factory=list)
    loaded: dict[str, SourceData | None] = field(default_factory=dict)
    failed: dict[str, str] = field(default_factory=dict)

    def source_set(self) -> SourceSet:
        return SourceSet(self.loaders, self.loaded, REFRESH_S, failed=self.failed)


def core_setting_ids() -> frozenset[str]:
    return frozenset(t.key for t in STATIC)


def core_knowledge_ids() -> frozenset[str]:
    return core_anchors() | {KNOWLEDGE_SECTION}


def _registry(env: Mapping[str, str], registry: Registry | None) -> Registry:
    return registry if registry is not None else Registry.load(env, core_tools=CORE_TOOLS, core_knowledge=core_knowledge_ids(), core_settings=core_setting_ids())


def _loaders(env: Mapping[str, str], registry: Registry, instances: bool = True) -> list[tuple[str, Callable[[], SourceData | None]]]:
    path = env.get(FILE_ENV) or DEFAULT_FILE
    loaders: list[tuple[str, Callable[[], SourceData | None]]] = [(LABEL_ENV, lambda: env_source(env, registry))] if instances else []
    loaders.append((LABEL_FILE, lambda: file_source(path, env)))
    for plugin in registry.usable():
        if plugin.hooks.settings_source is not None:
            loaders.append((plugin.name, lambda plugin=plugin: _hook_data(plugin, env) if registry.get(plugin.id).state == 'ok' else None))
    return loaders


def _bounded_hook(bound: Bound, label: str, load: Callable[[], SourceData | None], timeout: float) -> SourceData | None:
    try:
        return bound.call(label, load, timeout)
    except TimedOut:
        raise ConfigurationError(msg('settings.hook.failed', plugin=label, error='TimedOut')) from None


def boot(env: Mapping[str, str] | None = None, registry: Registry | None = None, timeout: float = HOOK_TIMEOUT_S) -> Boot:
    env = os.environ if env is None else env
    registry = _registry(env, registry)
    loaders = _loaders(env, registry)
    loaded: dict[str, SourceData | None] = {}
    skipped: set[str] = set()
    bound = Bound('settings-source')
    for index, (label, load) in enumerate(loaders):
        data = env_source(env, registry, timeout) if index == 0 else load() if index == 1 else _bounded_hook(bound, label, load, timeout)
        if data is not None and index < 2:
            _check_instances(registry, data, skipped)
        loaded[label] = data
    sources = [d for d in loaded.values() if d is not None]
    values = merged(sources)
    old = sorted(k for k in values if k in OLD_CONNECTION_KEYS)
    if old:
        places = sorted({d.label for d in sources if any(k in OLD_CONNECTION_KEYS for k in d.values)})
        log.warning('%s', msg('settings.old_connection', keys=', '.join(old), sources=', '.join(places)))
    return Boot(env, registry, sources, load_settings(fetch=lambda: values, env=env), loaders, loaded)


def light_boot(env: Mapping[str, str] | None = None, registry: Registry | None = None, timeout: float = HOOK_TIMEOUT_S) -> Boot:
    env = os.environ if env is None else env
    registry = _registry(env, registry)
    loaders = _loaders(env, registry, instances=False)
    loaded: dict[str, SourceData | None] = {}
    failed: dict[str, str] = {}
    bound = Bound('settings-source')
    for index, (label, load) in enumerate(loaders):
        try:
            loaded[label] = load() if index == 0 else bound.call(label, load, timeout)
        except Exception as e:
            loaded[label] = None
            failed[label] = type(e).__name__
    return Boot(env, registry, [d for d in loaded.values() if d is not None], None, loaders, loaded, failed)


AUTO_RESOLVE_FIELDS = frozenset({'auto_resolve_hours', 'auto_resolve_days', 'auto_resolve_hours_crit', 'auto_resolve_hours_warn', 'auto_resolve_hours_set', 'auto_resolve_sev_set',
                                 'info_auto_close_days', 'info_auto_close_days_set'})


class Follower:
    def __init__(self, sources: SourceSet, live, instances, env: Mapping[str, str]):
        self.sources = sources
        self.live = live
        self.instances = instances
        self.env = env
        self._seen = sources.current()

    def tick(self) -> None:
        try:
            if self.sources.refresh():
                self._follow()
        except Exception as e:
            log.warning('settings sources not followed, keeping the last values: %s', type(e).__name__)

    def _follow(self) -> None:
        before, after = merged(self._seen), merged(self.sources.current())
        labels = labelled(self.sources.current())
        old_labels = labelled(self._seen)
        self._seen = self.sources.current()
        changed = sorted(k for k in set(before) | set(after) if before.get(k) != after.get(k))
        known = self.live.known().values()
        core = {CONFIG_KEY.get(t.key, t.key): t for t in known if t.plugin is None}
        core[OLD_LINKS_KEY] = core[LINKS_KEY]
        own = {t.key: t for t in known if t.plugin is not None}
        fields: set[str] = set()
        restart: list[str] = []
        for key in changed:
            t = core.get(key) or own.get(key)
            if t is not None and t.live:
                if t.plugin is None:
                    fields.add(t.field)
            elif t is None and key.startswith('tags.'):
                fields.add('tag_rules')
            else:
                restart.append(key)
        base = self.live.base
        if 'slack_post_sev' in fields:
            fields.add('slack_post_sev_set')
        if fields & AUTO_RESOLVE_FIELDS:
            fields |= AUTO_RESOLVE_FIELDS
        if fields:
            try:
                fresh = load_settings(fetch=lambda: after, env=self.env)
                base = dataclasses.replace(base, **{f: getattr(fresh, f) for f in fields})
            except Exception as e:
                log.warning('settings from the sources not applied, keeping the last values: %s', type(e).__name__)
        self.live.rebase(base, after, labels)
        self.instances.refresh()
        for key in restart:
            log.info('%s', msg('settings.restart_to_apply', key=key, source=labels.get(key) or old_labels.get(key, '')))
