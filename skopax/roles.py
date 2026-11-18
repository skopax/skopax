import copy
import json
import logging
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout

from skopax.messages import msg
from skopax.tunables import Invalid
from skopax.users import RESOLVED_AT, ROLES

log = logging.getLogger('skopax.roles')

RESOLVER_TIMEOUT_S = 5
MAP_MAX = 50
VALUE_MAX = 200
RAW_MAX = 64 * 1024
WORKERS = 4
EXECUTOR = ThreadPoolExecutor(max_workers=WORKERS, thread_name_prefix='skopax-resolver')
POOL = {'busy': 0, 'warned': False}
POOL_GUARD = threading.Lock()

Hook = Callable[[dict, str], str | None]


def claim_values(claims: Mapping, path: str) -> tuple[str, ...]:
    if not path or not isinstance(claims, Mapping):
        return ()
    if path in claims:
        value = claims[path]
    else:
        value = claims
        for part in path.split('.'):
            if not isinstance(value, Mapping) or part not in value:
                return ()
            value = value[part]
    if isinstance(value, str):
        return (value,)
    if isinstance(value, list):
        return tuple(v for v in value if isinstance(v, str))
    return ()


def refused(reason: str) -> Invalid:
    return Invalid(msg('sso.role_map_invalid', reason=reason))


def parse_role_map(raw: str, limit: bool = True) -> tuple[tuple[str, str], ...]:
    text = str(raw if raw is not None else '')
    if limit and len(text) > RAW_MAX:
        raise refused(msg('sso.role_map.size', max=RAW_MAX))
    try:
        given = json.loads(text)
    except (ValueError, RecursionError):
        raise refused(msg('sso.role_map.json')) from None
    if not isinstance(given, list):
        raise refused(msg('sso.role_map.json'))
    if len(given) > MAP_MAX:
        raise refused(msg('sso.role_map.count', max=MAP_MAX))
    out: list[tuple[str, str]] = []
    for entry, item in enumerate(given, 1):
        if not isinstance(item, dict) or set(item) != {'value', 'role'}:
            raise refused(msg('sso.role_map.pair', entry=entry))
        value, role = item['value'], item['role']
        if not isinstance(value, str) or not 1 <= len(value) <= VALUE_MAX:
            raise refused(msg('sso.role_map.value', entry=entry, max=VALUE_MAX))
        if role not in ROLES:
            raise refused(msg('sso.role_map.role', entry=entry))
        if any(value == known for known, _ in out):
            raise refused(msg('sso.role_map.repeat', value=value))
        out.append((value, role))
    return tuple(out)


def map_text(pairs: Sequence[tuple[str, str]]) -> str:
    return json.dumps([{'value': value, 'role': role} for value, role in pairs], separators=(',', ':'), ensure_ascii=False)


def role_map(settings) -> tuple[tuple[str, str], ...]:
    raw = (getattr(settings, 'sso_role_map', '') or '').strip()
    if raw:
        return parse_role_map(raw, limit=False)
    given = ((settings.admin_role, 'admin'), (settings.operator_role, 'operator'))
    return tuple((value.strip(), role) for value, role in given if value and value.strip())


def mapped_role(values: Sequence[str], pairs: Sequence[tuple[str, str]]) -> str | None:
    found = {role for value, role in pairs if value in values}
    return max(found, key=ROLES.index) if found else None


class ResolverError(Exception):
    def __init__(self, plugin: str, why: str):
        self.plugin = plugin
        self.why = why
        self.line = f'the role resolver of plug-in {plugin} failed: {why}'
        super().__init__(self.line)


class Resolvers:
    def __init__(self, hooks: Callable[[], list[tuple[str, Hook]]], timeout: float = RESOLVER_TIMEOUT_S, clock: Callable[[], float] = time.monotonic):
        self.hooks = hooks
        self.timeout = timeout
        self.clock = clock

    def resolve(self, user: dict, channel: str) -> str | None:
        try:
            hooks = list(self.hooks())
        except Exception as e:
            raise ResolverError('runtime', type(e).__name__) from None
        for plugin, hook in hooks:
            value = self.asked(plugin, hook, user, channel)
            if value is None:
                continue
            if value not in ROLES:
                raise ResolverError(plugin, 'it answered a value that is not a role')
            return value
        return None

    def asked(self, plugin: str, hook: Hook, user: dict, channel: str) -> str | None:
        end = self.clock() + self.timeout
        for attempt in range(2):
            future = submit(hook, copy.deepcopy(user), channel)
            try:
                return future.result(timeout=max(0.0, end - self.clock()))
            except Exception as e:
                if isinstance(e, FutureTimeout) and not future.done():
                    future.cancel()
                    raise ResolverError(plugin, f'no answer within {self.timeout:g} s') from None
                if attempt == 0 and end - self.clock() >= self.timeout / 2:
                    continue
                raise ResolverError(plugin, type(e).__name__) from None


def said_down(error: ResolverError, minutes: int | None = None) -> None:
    log.warning('%s', error.line if minutes is None else msg('roles.last_known', error=error.line, minutes=minutes))


def last_known(doc: dict | None, now: float, limit_minutes: int) -> tuple[str, int] | None:
    at = (doc or {}).get(RESOLVED_AT)
    if not limit_minutes or not doc or doc.get('roleFrom') != 'resolver' or doc.get('role') not in ROLES or isinstance(at, bool) or not isinstance(at, (int, float)):
        return None
    age = now - at
    return (doc['role'], int(age // 60)) if 0 <= age <= limit_minutes * 60 else None


def busy() -> int:
    with POOL_GUARD:
        return POOL['busy']


def finished(_future) -> None:
    with POOL_GUARD:
        POOL['busy'] -= 1


def submit(hook: Hook, user: dict, channel: str):
    with POOL_GUARD:
        full = POOL['busy'] >= WORKERS
        warn = full and not POOL['warned']
        POOL['warned'] = full
    if warn:
        log.warning('%s', msg('roles.pool_full', workers=WORKERS))
    future = EXECUTOR.submit(hook, user, channel)
    with POOL_GUARD:
        POOL['busy'] += 1
    future.add_done_callback(finished)
    return future


def choose(*, resolved: str | None, mapped: str | None, admin_set: str | None, default: str) -> tuple[str, str]:
    for role, source in ((resolved, 'resolver'), (mapped, 'claim'), (admin_set, 'admin')):
        if role is not None:
            return role, source
    return default, 'default'
