import logging
import threading
import time

from pymongo.errors import ConnectionFailure, DuplicateKeyError, PyMongoError

from skopax.contract import InstanceWriter, Migration
from skopax.engine import iso
from skopax.errkind import Authored, kind
from skopax.messages import msg
from skopax.registry import Registry

log = logging.getLogger('skopax.migrations')

CORE: tuple[Migration, ...] = ()
LOCK_S = 300
WAIT_S = LOCK_S
POLL_S = 5
LOCK_ID = '_lock'
CORE_OWNER = 'core'


class MigrationFailed(Authored, Exception):
    def __init__(self, text: str, migration: str = '', error: str = ''):
        super().__init__(text)
        self.migration = migration
        self.error = error


class LeaseLost(MigrationFailed):
    pass


class Interrupted(MigrationFailed):
    pass


class RequiredFailed(MigrationFailed):
    pass


def _lease(col, boot_id: str, now: float) -> bool:
    try:
        col.update_one({'_id': LOCK_ID, '$or': [{'until': {'$lte': now}}, {'owner': boot_id}]}, {'$set': {'owner': boot_id, 'until': now + LOCK_S}}, upsert=True)
    except DuplicateKeyError:
        return False
    return True


def _renew(col, boot_id: str, clock) -> None:
    if col.update_one({'_id': LOCK_ID, 'owner': boot_id}, {'$set': {'until': clock() + LOCK_S}}).matched_count == 0:
        raise LeaseLost(msg('migrations.lease_lost'))


class _Heartbeat:
    def __init__(self, col, boot_id: str, clock):
        self._col = col
        self._boot_id = boot_id
        self._clock = clock
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._beat, name='skopax-migration-heartbeat', daemon=True)

    def __enter__(self) -> '_Heartbeat':
        self._thread.start()
        return self

    def __exit__(self, *_exc) -> None:
        self._stop.set()
        self._thread.join()

    def _beat(self) -> None:
        while not self._stop.wait(LOCK_S / 3):
            try:
                _renew(self._col, self._boot_id, self._clock)
            except LeaseLost:
                return
            except Exception as e:
                log.warning('%s', msg('migrations.heartbeat_failed', error=kind(e)))


def _repeated(items: tuple[Migration, ...]) -> int | None:
    seen: set[int] = set()
    for m in items:
        if m.version in seen:
            return m.version
        seen.add(m.version)
    return None


def _failed(owner: str, m: Migration, e: Exception, probe=None) -> MigrationFailed:
    if owner != CORE_OWNER and isinstance(e, ConnectionFailure):
        interrupted = Interrupted(msg('migrations.interrupted', owner=owner, error=kind(e)), m.name, kind(e))
        if probe is None:
            return interrupted
        try:
            probe()
        except LeaseLost as lost:
            return lost
        except Exception:
            return interrupted
    if owner != CORE_OWNER and m.required:
        return RequiredFailed(msg('migrations.required_failed', version=m.version, name=m.name, owner=owner, error=kind(e)), m.name, kind(e))
    return MigrationFailed(msg('migrations.failed', version=m.version, name=m.name, error=kind(e)), m.name, kind(e))


def _stored(col, owner: str) -> int:
    return int((col.find_one({'_id': owner}) or {}).get('version', 0))


def _required(plugin: object) -> list[Migration]:
    items = getattr(plugin, 'migrations', ())
    if not isinstance(items, (tuple, list)):
        return []
    return sorted((m for m in items if isinstance(m, Migration) and m.required and isinstance(m.version, int) and not isinstance(m.version, bool)), key=lambda m: m.version)


def _pending_required(db, registry: Registry) -> None:
    col = db['schema_version']
    for found in _governing(registry):
        needs = _required(found.plugin)
        if not needs:
            continue
        owner = str(found.id)
        try:
            done = _stored(col, owner)
        except Exception as e:
            raise RequiredFailed(msg('migrations.required_unknown', owner=owner, error=kind(e)), owner, kind(e)) from None
        late = next((m for m in needs if m.version > done), None)
        if late is not None:
            raise RequiredFailed(msg('migrations.required_pending', version=late.version, name=late.name, owner=owner), str(late.name), 'pending')


def _governing(registry: Registry) -> list:
    out = []
    for owner in sorted({str(x.id) for x in registry.loaded()}):
        found = registry.get(owner)
        if found is not None and found.plugin is not None:
            out.append(found)
    return out


def required_owners(registry: Registry) -> list[str]:
    return [str(x.id) for x in _governing(registry) if _required(x.plugin)]


def stop_if_required(registry: Registry, e: Exception) -> None:
    owners = required_owners(registry)
    if owners:
        raise RequiredFailed(msg('migrations.required_unreachable', owners=', '.join(owners), error=kind(e)), ', '.join(owners), kind(e)) from None


def _apply(db, col, owner: str, items: tuple[Migration, ...], boot_id: str, clock, applied: list[str], writer: InstanceWriter | None = None) -> None:
    done = _stored(col, owner)
    ordered = sorted(items, key=lambda x: x.version)
    if ordered and done > ordered[-1].version:
        log.warning('%s', msg('migrations.ahead', owner=owner, stored=done, code=ordered[-1].version))
    for m in ordered:
        if m.version <= done:
            continue
        if m.needs_instances and writer is None:
            return
        try:
            _renew(col, boot_id, clock)
            try:
                m.run(db, writer) if m.needs_instances else m.run(db)
            except Exception as e:
                raise _failed(owner, m, e, lambda: _renew(col, boot_id, clock)) from None
            _renew(col, boot_id, clock)
            col.update_one({'_id': owner}, {'$max': {'version': m.version}, '$push': {'applied': {'version': m.version, 'name': m.name, 'at': iso(clock())}}}, upsert=True)
        except MigrationFailed:
            raise
        except Exception as e:
            raise _failed(owner, m, e) from None
        applied.append(f'{owner}:{m.version}')


def _release(col, boot_id: str) -> None:
    try:
        col.delete_one({'_id': LOCK_ID, 'owner': boot_id})
    except PyMongoError as e:
        log.warning('%s', msg('migrations.release_failed', error=kind(e)))


def _plugin_run(db, col, plugin, boot_id: str, clock, applied: list[str], registry: Registry, writer: InstanceWriter | None = None) -> None:
    repeated = _repeated(plugin.migrations)
    try:
        if repeated is not None:
            raise MigrationFailed(msg('migrations.repeated', version=repeated), str(repeated))
        _apply(db, col, plugin.id, plugin.migrations, boot_id, clock, applied, writer)
    except (LeaseLost, Interrupted, RequiredFailed):
        raise
    except ConnectionFailure as e:
        raise Interrupted(msg('migrations.interrupted', owner=plugin.id, error=kind(e)), plugin.id, kind(e)) from None
    except Exception as e:
        reason = e.text if isinstance(e, MigrationFailed) else msg('migrations.bookkeeping', owner=plugin.id, error=kind(e))
        log.warning('plug-in %s: %s', plugin.id, reason)
        try:
            registry.clear_forced([plugin.id])
            registry.mark_broken(plugin.id, reason)
        except KeyError:
            pass
    else:
        registry.clear_forced([plugin.id])


def _plugins(db, col, registry: Registry, boot_id: str, clock, applied: list[str], writer: InstanceWriter | None = None) -> None:
    done: set[str] = set()
    while True:
        todo = [p for p in registry.runnable() if p.id not in done and p.migrations]
        if not todo:
            return
        for plugin in todo:
            if plugin.id not in {p.id for p in registry.runnable()}:
                continue
            done.add(plugin.id)
            _plugin_run(db, col, plugin, boot_id, clock, applied, registry, writer)


_interrupted: dict[str, str] = {}


def _try(db, registry: Registry, boot_id: str, clock, writer: InstanceWriter | None = None) -> tuple[list[str] | None, bool]:
    col = db['schema_version']
    if not _lease(col, boot_id, clock()):
        return None, False
    applied: list[str] = []
    try:
        with _Heartbeat(col, boot_id, clock):
            try:
                _apply(db, col, CORE_OWNER, CORE, boot_id, clock, applied, writer)
            except MigrationFailed:
                raise
            except Exception as e:
                raise MigrationFailed(msg('migrations.core_failed', error=kind(e)), CORE_OWNER, kind(e)) from None
            _plugins(db, col, registry, boot_id, clock, applied, writer)
            _pending_required(db, registry)
    except LeaseLost:
        log.warning('%s', msg('migrations.lease_lost'))
        return None, False
    except Interrupted as e:
        text = e.text
        if _interrupted.get(boot_id) != text:
            _interrupted[boot_id] = text
            log.warning('%s', text)
        return None, True
    finally:
        _release(col, boot_id)
    _interrupted.pop(boot_id, None)
    return applied, False


def attempt(db, registry: Registry, boot_id: str, clock=time.time, writer: InstanceWriter | None = None) -> list[str] | None:
    return _try(db, registry, boot_id, clock, writer)[0]


def run(db, registry: Registry, boot_id: str, clock=time.time, writer: InstanceWriter | None = None) -> list[str]:
    found = attempt(db, registry, boot_id, clock, writer)
    if found is None:
        log.info('migrations are being applied by another instance; skipping')
        return []
    return found


def wait_and_run(db, registry: Registry, boot_id: str, clock=time.time, sleep=time.sleep, wait_s: float | None = None, poll_s: float | None = None,
                 writer: InstanceWriter | None = None) -> list[str] | None:
    deadline = clock() + (WAIT_S if wait_s is None else wait_s)
    while True:
        try:
            found, interrupted = _try(db, registry, boot_id, clock, writer)
        except PyMongoError:
            _pending_required(db, registry)
            raise
        if found is not None:
            return found
        if interrupted:
            _pending_required(db, registry)
            return None
        if clock() >= deadline:
            _pending_required(db, registry)
            log.warning('%s', msg('migrations.still_held'))
            return None
        log.info('migrations are being applied by another instance; waiting')
        sleep(POLL_S if poll_s is None else poll_s)
