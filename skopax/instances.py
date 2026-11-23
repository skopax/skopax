import dataclasses
import hashlib
import logging
import re
import ssl
import threading
import time
import uuid
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from pymongo import ASCENDING
from pymongo.errors import DuplicateKeyError

from skopax.actors import SYSTEM
from skopax.agentproto import KEYS_MAX, VALUE_LIMIT
from skopax.bounded import HOOK_TIMEOUT_S, Bound, TimedOut
from skopax.config import retired_info
from skopax.contract import Field, InstanceInfo, Plugin, SourceData
from skopax.errkind import InvalidInstance, SourceOwned, kind
from skopax.messages import msg
from skopax.registry import ALONE_ADDRESS, HOSTS_ADDRESS, Registry
from skopax.sealer import SEALED, Sealer, stored_key
from skopax.store import iso, utc_now

log = logging.getLogger('skopax.instances')

NAME = re.compile(r'[a-z0-9](?:[-a-z0-9]{0,18}[a-z0-9])?')
REFRESH_S = 15
REREAD_S = 2
ADMIN = 'admin'
TEXT_MAX = 1000
SECRET_MAX = 4000
PEM_MAX = 20000
REGEX_MAX = 200
SOURCE_ORDER_LAST = 1 << 30
AGENT_PLUGIN = 'kubernetes'
AGENT_VALUE_MAX = VALUE_LIMIT
AGENT_KEYS_MAX = KEYS_MAX
OWN_ADDRESS_RULE = ('mcp-tools',)
ANY_VALUE = '*'
TRUST_FIELD = 'ca'
SEALABLE = ('secret', 'text')
PINNED_FIELD = 'ca-pinned'
FIELD_CHECK_S = 5
FIELD_CHECK_TEXT_MAX = 200
SECRET_RUN = 8
FIELD_CHECK_READ = 4


class InstanceExists(Exception):
    pass


class InstanceMissing(KeyError):
    def __str__(self) -> str:
        return str(self.args[0])


@dataclass(frozen=True)
class Instance:
    plugin: str
    name: str
    primary: bool
    enabled: bool
    values: Mapping[str, str]
    secrets: Mapping[str, str] = field(repr=False)
    source: str
    owned: frozenset[str]
    problem: str | None
    secret_keys: tuple[str, ...] = ()
    off_checks: frozenset[str] = frozenset()

    def info(self) -> InstanceInfo:
        return InstanceInfo(self.name, self.primary, None if self.primary else self.name, {**self.values, **self.secrets}, frozenset(self.off_checks))

    def view(self) -> dict:
        return {'name': self.name, 'primary': self.primary, 'enabled': self.enabled, 'source': self.source, 'problem': self.problem, 'values': dict(self.values),
                'secrets': {k: {'set': k in self.secrets, 'from': self.source if k in self.owned else None} for k in self.secret_keys}, 'owned': sorted(self.owned),
                'offChecks': sorted(self.off_checks)}


def merge_plan(stored: Mapping[str, str], given: Mapping[str, str | None]) -> tuple[dict[str, str], dict[str, str], list[str]]:
    out = dict(stored)
    put: dict[str, str] = {}
    drop: list[str] = []
    for key, value in given.items():
        if value is None:
            continue
        if value == '':
            out.pop(key, None)
            drop.append(key)
        else:
            out[key] = value
            put[key] = value
    return out, put, drop


def merged_values(stored: Mapping[str, str], given: Mapping[str, str | None]) -> dict[str, str]:
    return merge_plan(stored, given)[0]


def _text(f, raw: object) -> str:
    if raw is None:
        return ''
    if isinstance(raw, bool):
        return 'true' if raw else 'false'
    if isinstance(raw, (str, int, float)):
        return str(raw)
    raise InvalidInstance(f.key, msg('instances.invalid.scalar', field=f.label))


PEM_BLOCK = re.compile(r'-----BEGIN ([A-Z0-9 ]+)-----')


def _pem_ok(text: str) -> bool:
    kinds = PEM_BLOCK.findall(text)
    if not kinds or any(k != 'CERTIFICATE' for k in kinds):
        return False
    try:
        ssl.create_default_context(cadata=text)
    except (ssl.SSLError, ValueError, TypeError):
        return False
    return True


def retired_route_sev(plugin: Plugin, values: dict) -> dict:
    sev_field = plugin.outbound.sev_field if plugin.outbound is not None else None
    if not sev_field or values.get(sev_field) != 'info':
        return values
    return {**values, sev_field: retired_info(f'{plugin.id} {sev_field}', 'info')}


def _check(f, text: str) -> str:
    limit = SECRET_MAX if f.kind == 'secret' else PEM_MAX if f.kind == 'pem' else TEXT_MAX
    if len(text) > limit:
        raise InvalidInstance(f.key, msg('instances.invalid.long', field=f.label, max=limit))
    if text == '':
        return ''
    if f.kind == 'url':
        try:
            parts = urlsplit(text)
            parts.port
        except ValueError:
            parts = None
        if parts is None or parts.scheme not in ('http', 'https') or not parts.hostname or '@' in parts.netloc:
            raise InvalidInstance(f.key, msg('instances.invalid.url', field=f.label))
    elif f.kind == 'regex':
        try:
            if len(text) > REGEX_MAX:
                raise re.error('too long')
            re.compile(text)
        except (re.error, OverflowError, RecursionError):
            raise InvalidInstance(f.key, msg('instances.invalid.regex', field=f.label, max=REGEX_MAX)) from None
    elif f.kind == 'pem':
        if not _pem_ok(text):
            raise InvalidInstance(f.key, msg('instances.invalid.pem', field=f.label))
    elif f.kind == 'instance':
        if not NAME.fullmatch(text):
            raise InvalidInstance(f.key, msg('instances.invalid.instance', field=f.label))
    elif f.kind == 'choice':
        if text not in f.choices:
            raise InvalidInstance(f.key, msg('instances.invalid.choice', field=f.label, choices=', '.join(f.choices)))
    elif f.kind == 'bool':
        if text not in ('true', 'false'):
            raise InvalidInstance(f.key, msg('instances.invalid.bool', field=f.label))
    elif f.kind == 'int':
        try:
            number = int(text)
        except ValueError:
            raise InvalidInstance(f.key, msg('instances.invalid.int', field=f.label)) from None
        if f.low is not None and f.high is not None and not f.low <= number <= f.high:
            raise InvalidInstance(f.key, msg('instances.invalid.range', field=f.label, low=f.low, high=f.high))
        if f.low is not None and f.high is None and number < f.low:
            raise InvalidInstance(f.key, msg('instances.invalid.min', field=f.label, low=f.low))
        if f.high is not None and f.low is None and number > f.high:
            raise InvalidInstance(f.key, msg('instances.invalid.max', field=f.label, high=f.high))
        return str(number)
    return text


def limit_text(limit: int) -> str:
    return msg('instances.max_one') if limit == 1 else msg('instances.max', count=limit)


def effective_fields(plugin: Plugin, extra: Iterable[Field] = ()) -> tuple[Field, ...]:
    seen: set[str] = set()
    out: list[Field] = []
    for f in (*plugin.fields, *extra):
        if f.key not in seen:
            seen.add(f.key)
            out.append(f)
    return tuple(out)


def agent_fields(registry: Registry, plugin_id: str) -> tuple[Field, ...]:
    if plugin_id != AGENT_PLUGIN:
        return ()
    return tuple(dataclasses.replace(f, required=False, show_if=('mode', 'agent')) for p in registry.usable() if p.id != plugin_id for read in p.agent_reads for f in read.fields)


def address_owners(registry: Registry, plugin_id: str, extra: Iterable[Field]) -> dict[str, str]:
    keys = {f.key for f in extra}
    return {f.key: p.id for p in registry.usable() if p.id != plugin_id for read in p.agent_reads for f in read.fields if f.key in keys}


def moved_addresses(plugin: Plugin, fields: Mapping[str, Field], owners: Mapping[str, str], before: Mapping[str, str], given: Mapping[str, str | None], trust: bool = True) -> set[str]:
    after = merged_values(before, {k: v for k, v in given.items() if k in fields and fields[k].kind != 'secret'})
    own = {f.key for f in plugin.fields}

    def side(key: str) -> tuple[str, bool]:
        return owners.get(key, plugin.id), key in own

    def address(key: str) -> bool:
        return key in fields and fields[key].kind == 'url'

    def host_list(key: str) -> bool:
        return key in fields and fields[key].address == HOSTS_ADDRESS

    def added_host(key: str) -> bool:
        return bool(listed_hosts(given.get(key)) - listed_hosts(before.get(key))) or bool(honoured(fields[key], after.get(key), after) - honoured(fields[key], before.get(key), before))

    def trusted_otherwise(key: str) -> bool:
        if key not in fields or given.get(key) is None:
            return False
        if fields[key].kind == 'pem':
            return pem_text(given[key]) != pem_text(before.get(key))
        return key == PINNED_FIELD and fields.get(TRUST_FIELD) is not None and before.get(key) == 'true' and given[key] != 'true'

    def sent_to(key: str) -> bool:
        return address(key) and not (fields[key].ui and not fields[key].required and any(address(k) and not fields[k].ui and side(k) == side(key) for k in fields))

    def taken_over(key: str) -> bool:
        if key not in own or fields[key].address == ALONE_ADDRESS:
            return False
        if fields[key].default:
            return fields[key].default != before.get(key)
        return any(sent_to(k) and k != key and side(k) == side(key) and after.get(k) for k in fields)

    changed = [k for k, v in given.items() if (sent_to(k) or (k == 'via' and k in fields and plugin.id == AGENT_PLUGIN)) and v is not None and v != before.get(k, '')]
    hosts = [k for k in fields if host_list(k) and added_host(k)]
    trusted = [k for k in given if trust and trusted_otherwise(k)]
    return {side(k)[0] for k in changed if given[k] != '' or taken_over(k)} | {side(k)[0] for k in hosts + trusted}


def pem_text(value: object) -> str:
    return ''.join(value.split()) if isinstance(value, str) else ''


def honoured(f: Field, value: object, config: Mapping[str, str]) -> set[str]:
    value = '' if value is None else value
    if f.hosts_of is None or not isinstance(value, str):
        return set()
    try:
        return {h for h in f.hosts_of(value, config) if isinstance(h, str)}
    except Exception:
        return {value}


def listed_hosts(value: object) -> set[str]:
    return {h.strip().lower().rstrip('.') for h in re.split(r'[,;\s]+', value) if h.strip()} if isinstance(value, str) else set()


def visible(f: Field, out: Mapping[str, str], fields: Mapping[str, Field]) -> bool:
    if f.show_if is None:
        return True
    other = fields.get(f.show_if[0])
    value = out.get(f.show_if[0]) or (other.default if other is not None else '')
    return bool(value) if f.show_if[1] == ANY_VALUE else value == f.show_if[1]


def orphaned(values: Mapping[str, str], fields: Mapping[str, Field]) -> list[str]:
    return [k for k in values if k in fields and fields[k].show_if is not None and fields[k].show_if[1] == ANY_VALUE and not visible(fields[k], values, fields)]


def via_problem(name: str, values: Mapping[str, str], others: Mapping[str, Mapping[str, str]]) -> str | None:
    via = values.get('via')
    if not via or values.get('mode', 'agent') != 'agent':
        return None
    target = others.get(via)
    if via == name or target is None or target.get('mode', 'agent') != 'agent':
        return msg('instances.invalid.via')
    if target.get('via'):
        return msg('instances.invalid.via_chain', via=via)
    user = next((n for n, v in others.items() if v.get('via') == name and v.get('mode', 'agent') == 'agent'), None)
    if user is not None:
        return msg('instances.invalid.via_relays', other=user)
    return None


def hub_problem(owner: 'Instance', via: str | None, hub: 'Instance | None') -> str | None:
    if not owner.owned or not via:
        return None
    if hub is None or hub.source != owner.source or 'url' not in hub.owned:
        return msg('instances.via_not_owned', via=via, source=owner.source)
    return None


def _checked(plugin: Plugin, values: Mapping, partial: bool, required: bool = True, extra: Iterable[Field] = ()) -> tuple[dict[str, str], InvalidInstance | None]:
    extra = tuple(extra)
    all_fields = effective_fields(plugin, extra)
    fields = {f.key: f for f in all_fields}
    own = {f.key for f in plugin.fields}
    agent_keys = {f.key for f in extra} - own
    out: dict[str, str] = {}
    error: InvalidInstance | None = None
    for key, raw in values.items():
        f = fields.get(key)
        if f is None:
            error = error or InvalidInstance(str(key), msg('instances.invalid.unknown', field=key))
            continue
        try:
            text = _check(f, _text(f, raw))
            if key in agent_keys and len(text) > AGENT_VALUE_MAX:
                raise InvalidInstance(key, msg('instances.invalid.long', field=f.label, max=AGENT_VALUE_MAX))
        except InvalidInstance as e:
            error = error or e
            continue
        except Exception:
            error = error or InvalidInstance(key, msg('instances.invalid.unexpected', field=f.label))
            continue
        if text != '':
            out[key] = text
    held = [k for k in out if k in agent_keys]
    if len(held) > AGENT_KEYS_MAX:
        error = error or InvalidInstance(sorted(held)[AGENT_KEYS_MAX], msg('instances.invalid.agent_keys', max=AGENT_KEYS_MAX))
    if not partial and required:
        for f in all_fields:
            if f.required and visible(f, out, fields) and not (out.get(f.key) or f.default):
                error = error or InvalidInstance(f.key, msg('instances.invalid.required', field=f.label))
    return out, error


def validate(plugin: Plugin, values: Mapping, partial: bool = False, extra: Iterable[Field] = ()) -> dict[str, str]:
    out, error = _checked(plugin, values, partial, extra=extra)
    if error is not None:
        raise error
    return out


def leaked(value: str, text: str) -> bool:
    return any(value[i:i + SECRET_RUN] in text for i in range(len(value) - SECRET_RUN + 1))


def check_text(f: Field, value: str, said: str) -> str | None:
    secret = f.kind == 'secret' and bool(value)
    text = said[:FIELD_CHECK_READ * FIELD_CHECK_TEXT_MAX + (len(value) if secret else 0)]
    flat = ' '.join(value.split()) if secret else ''
    text = ' '.join((text.replace(value, '***') if secret else text).split())
    text = text.replace(flat, '***') if flat else text
    text = text if len(text) <= FIELD_CHECK_TEXT_MAX else text[:FIELD_CHECK_TEXT_MAX - 1] + '\u2026'
    return None if secret and (leaked(value, text) or leaked(flat, text)) else text


def checks_off(plugin: Plugin, off: Iterable[str]) -> frozenset[str]:
    known = {c.id for c in plugin.checks}
    chosen = frozenset(off)
    unknown = sorted(chosen - known)
    if unknown:
        raise InvalidInstance('offChecks', msg('instances.invalid.checks', check=unknown[0]))
    return chosen


def check_name(registry: Registry, name: object, plugin: str) -> None:
    if not isinstance(name, str) or not NAME.fullmatch(name):
        raise InvalidInstance('name', msg('instances.invalid.name'))
    if name in registry.reserved_names() or any(name.startswith(p) for p in registry.reserved_prefixes()) or registry.shadowed(plugin, name):
        raise InvalidInstance('name', msg('instances.invalid.reserved', name=name))


def _stamp() -> str:
    return iso(utc_now())


@dataclass
class _Row:
    order: tuple
    flag: bool | None
    instance: Instance


class Instances:
    def __init__(self, db, registry: Registry, sealer: Callable[[], Sealer], sources: Callable[[], list[SourceData]] = lambda: [], clock=time.monotonic, every: float = REFRESH_S,
                 extra_fields: Callable[[str], tuple[Field, ...]] = lambda _: (), source_timeout: float = HOOK_TIMEOUT_S):
        self.db = db
        self.source_timeout = source_timeout
        self._bound = Bound('settings-source')
        self._field_calls = Bound('field-check')
        self.col = db['instances']
        self.moves = db['primary_moves']
        self.registry = registry
        self._sealer = sealer
        self._sources = sources
        self.extra_fields = extra_fields
        self.clock = clock
        self.every = every
        self._items: list[Instance] = []
        self._checked: float | None = None
        self._asked: float | None = None
        self._last_sources: list[SourceData] = []
        self._warned: set = set()
        self._guard = threading.Lock()
        self._write = threading.RLock()
        self._flags: dict[tuple[str, str], bool | None] = {}
        self._noted_generated = False
        self.on_primary_moved: Callable[[str, InstanceInfo, InstanceInfo], None] | None = None
        self._seen: dict[str, str] = {}
        self._retry: set[str] = set()
        self._held: dict[str, tuple[str, str]] = {}
        self._move_warned: set[tuple[str, str, str]] = set()
        self._moving = threading.Lock()

    def ensure_indexes(self) -> None:
        self.col.create_index([('plugin', ASCENDING)])

    def age(self) -> float | None:
        return None if self._checked is None else self.clock() - self._checked

    def live_age(self) -> float | None:
        return None if self._asked is None else self.clock() - self._asked

    def all(self, plugin: str | None = None) -> list[Instance]:
        with self._guard:
            items = self._items
        return [i for i in items if plugin is None or i.plugin == plugin]

    def get(self, plugin: str, name: str) -> Instance | None:
        return next((i for i in self.all(plugin) if i.name == name), None)

    def fresh(self, plugin: str, name: str) -> Instance | None:
        with self._write:
            self._reload(live=False)
            return self.get(plugin, name)

    def reread(self, within: float | None = None) -> None:
        held = self.age()
        if held is not None and held < (REREAD_S if within is None else within):
            return
        try:
            self._reload(live=False)
        except Exception as e:
            self._warn(('refresh', type(e).__name__), msg('instances.refresh_failed', error=kind(e)))

    def primary(self, plugin: str) -> Instance | None:
        return next((i for i in self.all(plugin) if i.primary), None)

    def refresh(self) -> None:
        try:
            self._reload()
        except Exception as e:
            self._warn(('refresh', type(e).__name__), msg('instances.refresh_failed', error=kind(e)))
            return
        if not self._moving.acquire(blocking=False):
            return
        try:
            self._follow_primaries()
        finally:
            self._moving.release()

    def _follow_primaries(self) -> None:
        current = {i.plugin: i for i in self.all() if i.primary}
        changed = [plugin for plugin, i in current.items() if self._seen.get(plugin) != i.name or plugin in self._retry]
        self._seen = {plugin: i.name for plugin, i in current.items()}
        if not changed:
            return
        try:
            held = {d['_id']: d.get('primary') for d in self.moves.find({'_id': {'$in': changed}})}
        except Exception as e:
            self._retry.update(changed)
            self._move_failed('', kind(e), 'instances.primary_records_unread')
            return
        self._move_done('')
        for plugin in changed:
            old = held.get(plugin)
            if plugin in self._held and self._held[plugin][0] == current[plugin].name:
                done = self._moved_back(plugin)
            elif plugin in self._held:
                done = self._primary_moved(plugin, *self._held[plugin], current[plugin])
            elif not isinstance(old, str):
                done = self._first_record(plugin, current[plugin].name)
            else:
                done = old == current[plugin].name or self._primary_moved(plugin, old, uuid.uuid4().hex, current[plugin])
            if done:
                self._retry.discard(plugin)
                self._move_done(plugin)
            else:
                self._retry.add(plugin)

    def _first_record(self, plugin: str, name: str) -> bool:
        try:
            self.moves.insert_one({'_id': plugin, 'primary': name, 'at': _stamp()})
        except DuplicateKeyError:
            pass
        except Exception as e:
            self._move_failed(plugin, kind(e))
            return False
        return True

    def _record_primary(self, plugin: str, name: str | None) -> None:
        if name is None:
            self.moves.delete_one({'_id': plugin})
        else:
            self.moves.update_one({'_id': plugin}, {'$set': {'primary': name, 'at': _stamp()}, '$unset': {'claim': ''}}, upsert=True)

    def _primary_moved(self, plugin: str, old: str, token: str, new: Instance) -> bool:
        claimed = False
        try:
            with self._write:
                self._reload(live=False)
                current = self.get(plugin, new.name)
                if current is None or not current.primary:
                    self._held.pop(plugin, None)
                    return True
                if not self._claim_move(plugin, old, new.name, token):
                    self._held.pop(plugin, None)
                    return True
                claimed = True
                before = self.get(plugin, old)
                handed = self._once_choices(plugin, before, current)
                if handed:
                    self._patch(plugin, new.name, SYSTEM, handed)
                    self._reload(live=False)
                promoted = self.get(plugin, new.name)
            hook = self.on_primary_moved
            if hook is not None and promoted is not None:
                hook(plugin, InstanceInfo(old, True, None, before.info().config if before is not None else {}), promoted.info())
            self._held.pop(plugin, None)
            return True
        except Exception as e:
            self._move_failed(plugin, kind(e))
            if claimed:
                self._held[plugin] = (old, token)
                self._release_move(plugin, old, token)
            return False

    def _claim_move(self, plugin: str, old: str, new: str, token: str) -> bool:
        return self.moves.update_one({'_id': plugin, '$or': [{'primary': old}, {'claim': token}]}, {'$set': {'primary': new, 'claim': token, 'at': _stamp()}}).matched_count == 1

    def _moved_back(self, plugin: str) -> bool:
        if not self._release_move(plugin, *self._held[plugin], key='instances.primary_record_not_reset'):
            return False
        self._held.pop(plugin, None)
        return True

    def _release_move(self, plugin: str, old: str, token: str, key: str = 'instances.primary_move_failed') -> bool:
        try:
            self.moves.update_one({'_id': plugin, 'claim': token}, {'$set': {'primary': old}, '$unset': {'claim': ''}})
        except Exception as e:
            self._move_failed(plugin, kind(e), key)
            return False
        return True

    def _move_failed(self, plugin: str, error: str, key: str = 'instances.primary_move_failed') -> None:
        with self._guard:
            first = (plugin, error, key) not in self._move_warned
            self._move_warned.add((plugin, error, key))
        if first:
            log.warning('%s', msg(key, plugin=plugin, error=error))

    def _move_done(self, plugin: str) -> None:
        with self._guard:
            self._move_warned = {k for k in self._move_warned if k[0] != plugin}

    def _field_checks(self, plugin: str, values: Mapping) -> None:
        found = self.registry.get(plugin)
        if found is None or found.state != 'ok':
            return
        extra = self.extra_fields(plugin)
        fields = {f.key: f for f in effective_fields(found.plugin, extra)}
        given = {k: v for k, v in values.items() if k in fields and fields[k].check is not None and v is not None}
        if not given:
            return
        try:
            clean = validate(found.plugin, given, partial=True, extra=extra)
        except InvalidInstance:
            return
        for key, value in clean.items():
            f = fields[key]
            try:
                said = self._field_calls.call((plugin, key), lambda f=f, value=value: f.check(value), FIELD_CHECK_S)
            except TimedOut:
                raise InvalidInstance(key, msg('instances.invalid.check_slow', field=f.label)) from None
            except Exception as e:
                log.warning('the check of field %s of plug-in %s failed: %s', key, plugin, kind(e))
                raise InvalidInstance(key, msg('instances.invalid.check_failed', field=f.label)) from None
            if said is None or said == '':
                continue
            if not isinstance(said, str) or not check_text(f, value, said):
                raise InvalidInstance(key, msg('instances.invalid.check_failed', field=f.label))
            raise InvalidInstance(key, check_text(f, value, said))

    def create(self, plugin: str, name: str, values: Mapping, enabled: bool, by: str, off_checks: Iterable[str] = (), kept: Mapping[str, str] | None = None) -> Instance:
        self._field_checks(plugin, values)
        with self._write:
            found = self.registry.get(plugin)
            if found is None or found.state != 'ok':
                raise InvalidInstance('plugin', msg('instances.invalid.plugin', plugin=plugin))
            self._name_ok(plugin, name)
            extra = self.extra_fields(plugin)
            clean = validate(found.plugin, values, extra=extra)
            clean = {k: v for k, v in clean.items() if k not in orphaned(clean, {f.key: f for f in effective_fields(found.plugin, extra)})}
            off = checks_off(found.plugin, off_checks)
            self._reload(live=False)
            if self.get(plugin, name) is not None:
                raise InstanceExists(msg('instances.exists', name=name))
            limit = found.plugin.max_instances
            if limit is not None and len(self.all(plugin)) >= limit:
                raise InvalidInstance('name', limit_text(limit))
            self._peers_ok(found.plugin, clean, extra)
            self._via_ok(plugin, name, {**{f.key: f.default for f in found.plugin.fields if f.default}, **clean})
            kinds = {f.key: f.kind for f in effective_fields(found.plugin, extra)}
            sealer = self._sealer()
            now = _stamp()
            doc = {'_id': f'{plugin}/{name}', 'plugin': plugin, 'name': name, 'enabled': bool(enabled), 'values': {k: v for k, v in clean.items() if kinds[k] != 'secret'},
                   'secrets': {k: (kept or {}).get(k) or self._seal(sealer, v) for k, v in clean.items() if kinds[k] == 'secret'}, 'createdBy': by, 'createdAt': now, 'updatedBy': by, 'updatedAt': now}
            if off:
                doc['offChecks'] = sorted(off)
            if not self.all(plugin):
                doc['primary'] = True
            try:
                self.col.insert_one(doc)
            except DuplicateKeyError:
                raise InstanceExists(msg('instances.exists', name=name)) from None
            self._reload(live=False)
            return self.get(plugin, name)

    def update(self, plugin: str, name: str, values: Mapping, by: str, kept: Mapping[str, str] | None = None) -> Instance:
        self._field_checks(plugin, values)
        with self._write:
            self._reload(live=False)
            current = self._need(plugin, name)
            found = self.registry.get(plugin)
            extra = self.extra_fields(plugin)
            fields = {f.key: f for f in effective_fields(found.plugin, extra)}
            stored = self.col.find_one({'_id': f'{plugin}/{name}'}) or {}
            held = {**(stored.get('values') or {}), **(stored.get('secrets') or {})}
            stale = {k for k, v in values.items() if k not in fields and v == '' and k in held}
            live = {k: v for k, v in values.items() if k not in stale}
            for key in live:
                if key not in fields:
                    raise InvalidInstance(str(key), msg('instances.invalid.unknown', field=key))
                if live[key] is not None and key in current.owned:
                    raise SourceOwned(key, current.source, fields[key].label)
            clean = validate(found.plugin, {k: v for k, v in live.items() if v is not None}, partial=True, extra=extra)
            self._peers_ok(found.plugin, clean, extra)
            live, unsent = self._no_secret_follows(found.plugin, fields, extra, stored, live) if current.source == ADMIN and plugin not in OWN_ADDRESS_RULE else (live, None)
            sealer = self._sealer()
            plain = {k: v if v in (None, '') else clean[k] for k, v in live.items() if fields[k].kind != 'secret'} | dict.fromkeys(stale, '')
            sealed = {k: v if v in (None, '') else (kept or {}).get(k) or self._seal(sealer, clean[k]) for k, v in live.items() if fields[k].kind == 'secret'} | dict.fromkeys(stale, '')
            row = self._source_row(plugin, name)
            after = {**{k: v for k, v in (row[3] if row else {}).items() if v != ''}, **merged_values(stored.get('values') or {}, plain)}
            plain |= dict.fromkeys([k for k in orphaned(after, fields) if k in (stored.get('values') or {}) or plain.get(k)], '')
            new_values, put_values, drop_values = merge_plan(stored.get('values') or {}, plain)
            new_secrets, put_secrets, drop_secrets = merge_plan(stored.get('secrets') or {}, sealed)
            doc = {**stored, 'values': new_values, 'secrets': new_secrets}
            made, error = self._make(found.plugin, name, row, doc, sealer, self._alt(), [])
            if error is not None:
                raise error
            self._via_ok(plugin, name, made.instance.values)
            if unsent is not None:
                raise InvalidInstance(unsent[0], msg('integrations.reenter_trust' if unsent[1] else 'integrations.reenter_secret'))
            put: dict = {}
            drop: dict = {}
            for part, whole, puts, drops in (('values', new_values, put_values, drop_values), ('secrets', new_secrets, put_secrets, drop_secrets)):
                if any('.' in k for k in (*puts, *drops)):
                    put[part] = whole
                else:
                    put.update({f'{part}.{k}': v for k, v in puts.items()})
                    drop.update({f'{part}.{k}': '' for k in drops})
            self._patch(plugin, name, by, put, drop)
            self._reload(live=False)
            return self.get(plugin, name)

    def _no_secret_follows(self, plugin: Plugin, fields: Mapping[str, Field], extra: tuple[Field, ...], stored: Mapping, live: dict) -> tuple[dict, tuple[str, bool] | None]:
        before = {**{f.key: f.default for f in fields.values() if f.default and f.kind != 'secret'}, **(stored.get('values') or {})}
        owners = address_owners(self.registry, plugin.id, extra)
        moved = moved_addresses(plugin, fields, owners, before, live)
        if not moved:
            return live, None
        held = stored.get('secrets') or {}
        followed = [k for k in fields if k in held and live.get(k) is None and (plugin.id in moved or owners.get(k, plugin.id) in moved)]
        after = merged_values(before, {k: v for k, v in live.items() if fields[k].kind != 'secret'})
        shown = [k for k in followed if visible(fields[k], after, fields)]
        if not shown:
            return {**live, **dict.fromkeys(followed, '')}, None
        addressed = moved_addresses(plugin, fields, owners, before, live, trust=False)
        return {**live, **dict.fromkeys([k for k in followed if k not in shown], '')}, (shown[0], plugin.id not in addressed and owners.get(shown[0], plugin.id) not in addressed)

    def set_enabled(self, plugin: str, name: str, on: bool, by: str) -> Instance:
        with self._write:
            self._reload(live=False)
            self._need(plugin, name)
            self._patch(plugin, name, by, {'enabled': bool(on)})
            self._reload(live=False)
            return self.get(plugin, name)

    def set_checks(self, plugin: str, name: str, off: Iterable[str], by: str) -> Instance:
        with self._write:
            self._reload(live=False)
            self._need(plugin, name)
            chosen = checks_off(self.registry.get(plugin).plugin, off)
            self._patch(plugin, name, by, {'offChecks': sorted(chosen)})
            self._reload(live=False)
            return self.get(plugin, name)

    def make_primary(self, plugin: str, name: str, by: str) -> tuple[str, ...]:
        with self._write:
            self._reload(live=False)
            target = self._need(plugin, name)
            before = {i.name: i.primary for i in self.all(plugin)}
            if target.primary:
                return ()
            handed = self._once_choices(plugin, self.primary(plugin), target)
            self._record_primary(plugin, name)
            for i in self.all(plugin):
                if i.name == name:
                    self._patch(plugin, name, by, {'primary': True, **handed})
                elif i.primary or self._flags.get((plugin, i.name)):
                    self._patch(plugin, i.name, by, {'primary': False})
            self.col.update_many({'plugin': plugin, '_id': {'$ne': f'{plugin}/{name}'}, 'primary': True}, {'$set': {'primary': False}})
            self._reload(live=False)
            after = {i.name: i.primary for i in self.all(plugin)}
            changed = [n for n, was in before.items() if was != after.get(n) and not after.get(n)] + [n for n, now in after.items() if now and not before.get(n)]
            return tuple(changed)

    def delete(self, plugin: str, name: str, by: str) -> None:
        with self._write:
            self._reload(live=False)
            current = self._need(plugin, name)
            if current.source != ADMIN:
                raise SourceOwned('name', current.source, delete=True)
            self.col.delete_one({'_id': f'{plugin}/{name}'})
            self._reload(live=False)
            chosen = self.primary(plugin)
            put = self._once_choices(plugin, current, chosen) if current.primary and chosen is not None else {}
            if chosen is not None and chosen.source == ADMIN and not (self.col.find_one({'_id': f'{plugin}/{chosen.name}'}) or {}).get('primary'):
                put['primary'] = True
            if put:
                self._patch(plugin, chosen.name, by, put)
                self._reload(live=False)
            if current.primary:
                self._record_primary(plugin, None if chosen is None else chosen.name)

    def _once_choices(self, plugin: str, old: Instance | None, new: Instance) -> dict:
        found = self.registry.get(plugin)
        if old is None or found is None or found.plugin is None:
            return {}
        once = {c.id for c in found.plugin.checks if c.once}
        off = (new.off_checks - once) | (old.off_checks & once)
        return {'offChecks': sorted(off)} if off != new.off_checks else {}

    def _need(self, plugin: str, name: str) -> Instance:
        found = self.get(plugin, name)
        if found is None:
            raise InstanceMissing(msg('instances.unknown', name=name))
        return found

    def adopt_sealed(self, plugin: str, name: str, field: str, sealed: str, by: str) -> Instance:
        with self._write:
            self._reload(live=False)
            current = self._need(plugin, name)
            found = self.registry.get(plugin)
            fields = {f.key: f for f in effective_fields(found.plugin, self.extra_fields(plugin))}
            chosen = fields.get(field)
            if chosen is None:
                raise InvalidInstance(field, msg('instances.invalid.unknown', field=field))
            if chosen.kind not in SEALABLE:
                raise InvalidInstance(field, msg('instances.not_sealable', field=chosen.label))
            if field in current.owned:
                raise SourceOwned(field, current.source, chosen.label)
            opened = self._open(sealed, self._sealer(), self._alt()) if isinstance(sealed, str) and sealed.startswith(SEALED) else None
            if opened is None:
                raise InvalidInstance(field, msg('instances.secret_unreadable', field=chosen.label))
            text = _check(chosen, opened[0])
            secret = chosen.kind == 'secret'
            part = 'secrets' if secret else 'values'
            kept = sealed if secret else text
            if '.' in field:
                stored = (self.col.find_one({'_id': f'{plugin}/{name}'}) or {}).get(part) or {}
                self._patch(plugin, name, by, {part: {**stored, field: kept}})
            else:
                self._patch(plugin, name, by, {f'{part}.{field}': kept})
            self._reload(live=False)
            return self.get(plugin, name)

    def put(self, plugin: str, name: str, values: Mapping[str, str], secrets: Mapping[str, str], by: str, primary: bool = False, sealed: Mapping[str, str] | None = None,
            enabled: bool = True, off_checks: Iterable[str] = ()) -> Instance:
        with self._write:
            found = self.registry.get(plugin)
            if found is None or found.plugin is None:
                raise InvalidInstance('plugin', msg('instances.invalid.plugin', plugin=plugin))
            fields = {f.key: f for f in effective_fields(found.plugin, self.extra_fields(plugin))}
            given_sealed = dict(sealed or {})
            for part, secret in ((values, False), (secrets, True)):
                for key in part:
                    if key not in fields:
                        raise InvalidInstance(str(key), msg('instances.invalid.unknown', field=key))
                    if (fields[key].kind == 'secret') != secret:
                        raise InvalidInstance(key, msg('instances.invalid.misfiled', field=fields[key].label))
            opened: dict[str, str] = {}
            for key, value in given_sealed.items():
                if key not in fields:
                    raise InvalidInstance(str(key), msg('instances.invalid.unknown', field=key))
                if fields[key].kind not in SEALABLE:
                    raise InvalidInstance(key, msg('instances.not_sealable', field=fields[key].label))
                if key in secrets or key in values:
                    raise InvalidInstance(key, msg('instances.invalid.twice', field=fields[key].label))
                plain = self._sealer().open(value) if isinstance(value, str) and value.startswith(SEALED) else None
                if plain is None:
                    raise InvalidInstance(key, msg('instances.secret_unreadable', field=fields[key].label))
                opened[key] = plain
            kept = {k: v for k, v in given_sealed.items() if fields[k].kind == 'secret'}
            off = checks_off(found.plugin, off_checks)
            if primary and not enabled:
                raise InvalidInstance('primary', msg('instances.primary_disabled', name=name))
            given = {**values, **secrets, **opened}
            self._reload(live=False)
            current = self.get(plugin, name)
            if primary and current is not None and not current.enabled:
                raise InvalidInstance('primary', msg('instances.primary_disabled', name=name))
            if current is None:
                self.create(plugin, name, given, enabled, by, off, kept)
            else:
                self.update(plugin, name, given, by, kept)
                if not enabled and current.enabled:
                    self.set_enabled(plugin, name, False, by)
                if off - current.off_checks:
                    self.set_checks(plugin, name, current.off_checks | off, by)
            if primary:
                self.make_primary(plugin, name, by)
            return self.get(plugin, name)

    def values_of(self, plugin: str, name: str) -> dict[str, str] | None:
        self._reload(live=False)
        found = self.get(plugin, name)
        return None if found is None else dict(found.values)

    def _peers_ok(self, plugin: Plugin, values: Mapping[str, str], extra: Iterable[Field]) -> None:
        for f in effective_fields(plugin, extra):
            value = values.get(f.key)
            if f.kind != 'instance' or not value or len(f.choices) != 1:
                continue
            used = f.choices[0]
            if not any(i.name == value for i in self.all(used)):
                loaded = self.registry.get(used)
                shown = loaded.plugin.name if loaded is not None and loaded.plugin is not None and isinstance(loaded.plugin.name, str) and loaded.plugin.name.strip() else used
                raise InvalidInstance(f.key, msg('peers.unknown', plugin=shown, name=value))

    def _name_ok(self, plugin: str, name: object) -> None:
        check_name(self.registry, name, plugin)

    def _via_ok(self, plugin: str, name: str, values: Mapping[str, str]) -> None:
        if plugin != AGENT_PLUGIN:
            return
        found = via_problem(name, values, {i.name: i.values for i in self.all(plugin) if i.name != name})
        if found is not None:
            raise InvalidInstance('via', found)

    def _patch(self, plugin: str, name: str, by: str, put: dict, drop: dict | None = None) -> None:
        now = _stamp()
        update = {'$set': {**put, 'updatedBy': by, 'updatedAt': now}, '$setOnInsert': {'plugin': plugin, 'name': name, 'createdBy': by, 'createdAt': now}}
        if drop:
            update['$unset'] = drop
        self.col.update_one({'_id': f'{plugin}/{name}'}, update, upsert=True)

    def _seal(self, sealer: Sealer, text: str) -> str:
        if sealer.generated and not self._noted_generated:
            self._noted_generated = True
            log.warning('secrets are sealed with a key that was generated and is kept in the database; set SKOPAX_SECRET_KEY to manage the key yourself')
        return sealer.seal(text)

    def _warn(self, key: tuple, text: str) -> None:
        digest = hashlib.sha256(repr(key).encode()).hexdigest()
        if digest not in self._warned:
            self._warned.add(digest)
            log.warning(text)

    def _source_data(self, live: bool = True) -> list[SourceData]:
        if not live and self._checked is not None:
            return self._last_sources
        try:
            data = self._bound.call('sources', lambda: list(self._sources()), self.source_timeout)
        except Exception as e:
            self._warn(('source', type(e).__name__), msg('instances.sources_failed', error=kind(e)))
            return self._last_sources
        self._last_sources = data
        return data

    def _alt(self) -> Callable[[], Sealer | None]:
        box: list = []

        def previous() -> Sealer | None:
            if not box:
                key = stored_key(self.db)
                box.append(Sealer(key) if key else None)
            return box[0]

        return previous

    def _source_row(self, plugin: str, name: str) -> tuple | None:
        for index, data in enumerate(self._last_sources):
            for pos, raw in enumerate(data.instances):
                if raw.get('plugin') == plugin and raw.get('name') == name:
                    return index, pos, data.label, raw
        return None

    def _reload(self, live: bool = True) -> None:
        with self._write:
            asked = live or self._checked is None
            docs = list(self.col.find())
            sources = self._source_data(live)
            sealer = self._sealer()
            built, reseal = self._merge(docs, sources, sealer)
            with self._guard:
                self._items = built
                self._checked = self.clock()
                if asked:
                    self._asked = self._checked
            self._reseal(reseal, sealer)

    def _reseal_one(self, doc_id: str, key: str, old: str, new: str) -> bool:
        if '.' not in key:
            return bool(self.col.update_one({'_id': doc_id, f'secrets.{key}': old}, {'$set': {f'secrets.{key}': new}}).modified_count)
        held = (self.col.find_one({'_id': doc_id}) or {}).get('secrets') or {}
        if held.get(key) != old:
            return False
        return bool(self.col.update_one({'_id': doc_id, 'secrets': held}, {'$set': {'secrets': {**held, key: new}}}).modified_count)

    def _reseal(self, reseal: list[tuple], sealer: Sealer) -> None:
        if reseal and sealer.enabled:
            done = 0
            for doc_id, key, old, plain in reseal:
                if self._reseal_one(doc_id, key, old, sealer.seal(plain)):
                    done += 1
            if done:
                log.info('%d secret(s) were sealed again with the current secret key', done)

    def _merge(self, docs: list[dict], sources: list[SourceData], sealer: Sealer) -> tuple[list[Instance], list[tuple]]:
        entries: dict[tuple[str, str], dict] = {}
        for index, data in enumerate(sources):
            for pos, raw in enumerate(data.instances):
                key = self._source_key(raw, data.label)
                if key is None:
                    continue
                if 'src' in entries.get(key, {}):
                    self._warn(('dup', key), f'instance {key[1]} of plug-in {key[0]} is defined by more than one settings source; using the first')
                    continue
                entries.setdefault(key, {})['src'] = (index, pos, data.label, raw)
        for doc in docs:
            if isinstance(doc.get('plugin'), str) and isinstance(doc.get('name'), str):
                entries.setdefault((doc['plugin'], doc['name']), {})['doc'] = doc
        alt = self._alt()
        reseal: list[tuple] = []
        rows: dict[str, list[_Row]] = {}
        flags: dict[tuple[str, str], bool | None] = {}
        for (plugin, name), parts in entries.items():
            found = self.registry.get(plugin)
            if found is None or found.plugin is None or found.state == 'broken':
                continue
            src = parts.get('src')
            try:
                built, _ = self._make(found.plugin, name, src, parts.get('doc'), sealer, alt, reseal)
            except Exception as e:
                self._warn(('make', plugin, name, type(e).__name__), f'an instance of plug-in {plugin} could not be read: {kind(e)}')
                built = _Made(Instance(plugin, name, False, False, {}, {}, src[2] if src else ADMIN, frozenset(), msg('instances.unexpected'), ()), None)
            if src is None and not built.instance.problem:
                try:
                    check_name(self.registry, name, plugin)
                except InvalidInstance as e:
                    built = _Made(dataclasses.replace(built.instance, problem=msg('instances.reserved_now', reason=e.args[0])), built.primary_flag)
            flags[(plugin, name)] = built.primary_flag
            order = (src[0], src[1], name) if src else (SOURCE_ORDER_LAST, 0, name)
            rows.setdefault(plugin, []).append(_Row(order, built.primary_flag, built.instance))
        out: list[Instance] = []
        for plugin, group in rows.items():
            group.sort(key=lambda r: r.order)
            limit = self.registry.get(plugin).plugin.max_instances
            kept = group if limit is None else group[:limit]
            for r in group[len(kept):]:
                if not r.instance.problem:
                    r.instance = dataclasses.replace(r.instance, problem=limit_text(limit))
            if plugin == AGENT_PLUGIN:
                byname = {r.instance.name: r.instance.values for r in group}
                hubs = {r.instance.name: r.instance for r in group}
                for r in group:
                    found = None if r.instance.problem else via_problem(r.instance.name, r.instance.values, {n: v for n, v in byname.items() if n != r.instance.name})
                    found = found or (None if r.instance.problem else hub_problem(r.instance, r.instance.values.get('via'), hubs.get(r.instance.values.get('via') or '')))
                    if found is not None:
                        r.instance = dataclasses.replace(r.instance, problem=found)
            flagged = [r for r in kept if r.flag]
            chosen = (flagged or kept)[0]
            if len(flagged) > 1:
                self._warn(('primaries', plugin, tuple(r.instance.name for r in flagged)), f'plug-in {plugin} has several primary instances ({", ".join(r.instance.name for r in flagged)}); using {chosen.instance.name}')
            out.extend(dataclasses.replace(r.instance, primary=r is chosen) for r in group)
        out.sort(key=lambda i: (i.plugin, not i.primary, i.name))
        self._flags = flags
        return out, reseal

    def _source_key(self, raw: Mapping, label: str) -> tuple[str, str] | None:
        plugin, name = raw.get('plugin'), raw.get('name')
        found = self.registry.get(plugin) if isinstance(plugin, str) else None
        if found is None or found.plugin is None or found.state == 'broken':
            self._warn(('unknown', str(plugin)), msg('instances.source_unknown_plugin', label=label))
            return None
        try:
            self._name_ok(plugin, name)
        except InvalidInstance as e:
            self._warn(('name', str(plugin), str(name)), msg('instances.source_instance_skipped', label=label, plugin=plugin, error=kind(e)))
            return None
        return plugin, name

    def _make(self, plugin: Plugin, name: str, src: tuple | None, doc: dict | None, sealer: Sealer, alt: Callable[[], Sealer | None], reseal: list) -> tuple['_Made', InvalidInstance | None]:
        extra = self.extra_fields(plugin.id)
        all_fields = effective_fields(plugin, extra)
        fields = {f.key: f for f in all_fields}
        values = {f.key: f.default for f in all_fields if f.default and f.kind != 'secret'}
        secrets: dict[str, str] = {}
        owned: set[str] = set()
        addresses: set[str] = set()
        problem = None
        error = None
        flag = None
        enabled = True
        off: frozenset[str] = frozenset()
        label = ADMIN
        if src is not None:
            label = src[2]
            for key, value in src[3].items():
                if key in ('plugin', 'name'):
                    continue
                if key == 'primary':
                    flag = str(value).strip().lower() == 'true'
                elif key == 'enabled':
                    enabled = str(value).strip().lower() != 'false'
                elif key not in fields:
                    error = error or InvalidInstance(key, msg('instances.invalid.unknown', field=key))
                elif fields[key].kind == 'secret':
                    if value != '':
                        secrets[key] = value
                        owned.add(key)
                elif value != '':
                    values[key] = value
                    if fields[key].kind == 'url':
                        addresses.add(key)
        if owned:
            owners = address_owners(self.registry, plugin.id, extra)
            sides = {owners.get(k, plugin.id) for k in owned} | {plugin.id}
            owned |= addresses | {k for k, f in fields.items() if (f.kind == 'url' and owners.get(k, plugin.id) in sides) or (plugin.id == AGENT_PLUGIN and k == 'via' and plugin.id in sides)}
        unreadable = None
        if doc is not None:
            for key, value in (doc.get('values') or {}).items():
                if key in fields and fields[key].kind != 'secret' and key not in owned and isinstance(value, str) and value != '':
                    values[key] = value
            for key, sealed in (doc.get('secrets') or {}).items():
                f = fields.get(key)
                if f is None or f.kind != 'secret' or key in owned:
                    continue
                opened = self._open(sealed, sealer, alt)
                if opened is None:
                    unreadable = unreadable or f.label
                else:
                    secrets[key] = opened[0]
                    if opened[1]:
                        reseal.append((doc['_id'], key, sealed, opened[0]))
            if 'enabled' in doc:
                enabled = bool(doc['enabled'])
            if isinstance(doc.get('offChecks'), list):
                off = frozenset(c for c in doc['offChecks'] if isinstance(c, str)) & {c.id for c in plugin.checks}
            if 'primary' in doc:
                flag = bool(doc['primary'])
        clean, found_error = _checked(plugin, retired_route_sev(plugin, {**values, **secrets}), False, required=unreadable is None, extra=extra)
        error = error or found_error
        values = {k: v for k, v in clean.items() if fields[k].kind != 'secret'}
        secrets = {k: v for k, v in clean.items() if fields[k].kind == 'secret'}
        if unreadable is not None:
            problem = msg('instances.secret_unreadable', field=unreadable)
        elif error is not None:
            problem = str(error)
        secret_keys = tuple(f.key for f in all_fields if f.kind == 'secret')
        instance = Instance(plugin.id, name, False, enabled, values, secrets, label, frozenset(owned), problem, secret_keys, off)
        return _Made(instance, flag), error

    def _open(self, sealed: object, sealer: Sealer, alt: Callable[[], Sealer | None]) -> tuple[str, bool] | None:
        plain = sealer.open(sealed)
        if plain is not None:
            return plain, False
        other = alt()
        plain = other.open(sealed) if other is not None else None
        return None if plain is None else (plain, True)


class Writer:
    def __init__(self, instances: Instances):
        self._instances = instances

    def adopt_sealed(self, plugin: str, name: str, field: str, sealed: str, by: str) -> None:
        self._instances.adopt_sealed(plugin, name, field, sealed, by)

    def put(self, plugin: str, name: str, values: Mapping[str, str], secrets: Mapping[str, str], by: str, primary: bool = False, sealed: Mapping[str, str] | None = None,
            enabled: bool = True, off_checks: Iterable[str] = ()) -> None:
        self._instances.put(plugin, name, values, secrets, by, primary, sealed, enabled, off_checks)

    def get(self, plugin: str, name: str) -> Mapping[str, str] | None:
        return self._instances.values_of(plugin, name)


@dataclass
class _Made:
    instance: Instance
    primary_flag: bool | None


class PluginState:
    def __init__(self, db):
        self.col = db['plugin_state']
        self._on: dict[str, bool] = {}
        self._guard = threading.Lock()
        self._write = threading.RLock()

    def refresh(self) -> None:
        with self._write:
            found = {d['_id']: bool(d.get('enabled', True)) for d in self.col.find()}
            with self._guard:
                self._on = found

    def enabled(self, plugin_id: str) -> bool:
        with self._guard:
            return self._on.get(plugin_id, True)

    def set(self, plugin_id: str, on: bool, by: str) -> None:
        with self._write:
            self.col.replace_one({'_id': plugin_id}, {'_id': plugin_id, 'enabled': bool(on), 'updatedBy': by, 'updatedAt': _stamp()}, upsert=True)
            with self._guard:
                self._on = {**self._on, plugin_id: bool(on)}
