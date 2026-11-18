import dataclasses
import logging
import threading
import time
import traceback

import mongomock
import pytest

from fastapi.testclient import TestClient
from pymongo.errors import AutoReconnect, NetworkTimeout, OperationFailure, ServerSelectionTimeoutError

from skopax import migrations
from skopax.app import create_app
from skopax.runtime import Runtime
from skopax.instances import Instances, PluginState
from skopax.sealer import Sealer
from tests.conftest import settings
from skopax.contract import Migration
from tests.plugin_kit import no_loop, registry_with
from tests.plugins import demo_plugin, enterprise_plugin
from tests.plugins.base import minimal

DEMO = demo_plugin.PLUGIN


def fresh():
    return mongomock.MongoClient(tz_aware=True)['skopax']


def noting(log, label):
    return lambda db: log.append(label)


@pytest.fixture
def seen(monkeypatch):
    log = []
    monkeypatch.setattr(migrations, 'CORE', (Migration(1, 'core-one', noting(log, 'core:1')), Migration(2, 'core-two', noting(log, 'core:2'))))
    return log


def test_migrations_run_once_in_version_order_core_first(seen):
    # given
    db = fresh()
    late = minimal('alpha', 'Alpha', after=('zed',), migrations=(Migration(2, 'b', noting(seen, 'alpha:2')), Migration(1, 'a', noting(seen, 'alpha:1'))))
    early = minimal('zed', 'Zed', migrations=(Migration(1, 'z', noting(seen, 'zed:1')),))
    reg = registry_with(DEMO, late, early)

    # when
    first = migrations.run(db, reg, 'boot-1')
    second = migrations.run(db, reg, 'boot-2')

    # then
    assert first == ['core:1', 'core:2', 'demo:1', 'demo:2', 'zed:1', 'alpha:1', 'alpha:2']
    assert seen == ['core:1', 'core:2', 'zed:1', 'alpha:1', 'alpha:2']
    assert second == []
    assert db['demo_state'].find_one({'_id': 'seed'})['v'] == 2
    record = db['schema_version'].find_one({'_id': 'alpha'})
    assert record['version'] == 2 and [a['version'] for a in record['applied']] == [1, 2] and record['applied'][0]['name'] == 'a' and record['applied'][0]['at'].endswith('Z')
    assert db['schema_version'].find_one({'_id': '_lock'}) is None


def test_two_runners_apply_each_migration_once(seen):
    # given
    db = fresh()
    reg = registry_with(DEMO)
    now = 1_000_000.0
    db['schema_version'].insert_one({'_id': '_lock', 'owner': 'boot-1', 'until': now + migrations.LOCK_S})

    # when
    blocked = migrations.run(db, reg, 'boot-2', clock=lambda: now + 10)
    expired = migrations.run(db, reg, 'boot-2', clock=lambda: now + migrations.LOCK_S + 1)
    again = migrations.run(db, reg, 'boot-1', clock=lambda: now + migrations.LOCK_S + 2)

    # then
    assert blocked == []
    assert expired == ['core:1', 'core:2', 'demo:1', 'demo:2']
    assert again == []
    assert seen == ['core:1', 'core:2']


def test_a_failing_plugin_migration_breaks_only_that_plugin(seen):
    # given
    db = fresh()

    def boom(_db):
        raise RuntimeError('disk full')

    shaky = minimal('shaky', 'Shaky', migrations=(Migration(1, 'ok', noting(seen, 'shaky:1')), Migration(2, 'bad', boom), Migration(3, 'never', noting(seen, 'shaky:3'))))
    reg = registry_with(DEMO, shaky)

    # when
    applied = migrations.run(db, reg, 'boot-1')

    # then
    assert applied == ['core:1', 'core:2', 'demo:1', 'demo:2', 'shaky:1']
    assert 'shaky:3' not in seen
    assert reg.get('shaky').state == 'broken' and 'RuntimeError' in reg.get('shaky').reason and 'disk full' not in reg.get('shaky').reason and '2' in reg.get('shaky').reason
    assert reg.get('demo').state == 'ok'
    assert db['schema_version'].find_one({'_id': 'shaky'})['version'] == 1
    assert db['schema_version'].find_one({'_id': '_lock'}) is None


def test_a_failing_core_migration_raises_and_frees_the_lease(monkeypatch):
    # given
    db = fresh()

    def boom(_db):
        raise RuntimeError('core broke')

    monkeypatch.setattr(migrations, 'CORE', (Migration(1, 'bad', boom),))

    # when
    with pytest.raises(migrations.MigrationFailed, match=r'migration 1 \(bad\) failed: RuntimeError'):
        migrations.run(db, registry_with(DEMO), 'boot-1')

    # then
    assert db['schema_version'].find_one({'_id': '_lock'}) is None
    assert db['schema_version'].find_one({'_id': 'core'}) is None


def test_a_repeated_version_breaks_the_plugin_before_anything_runs(seen):
    # given
    db = fresh()
    twice = minimal('twice', 'Twice', migrations=(Migration(1, 'a', noting(seen, 'twice:1')), Migration(1, 'b', noting(seen, 'twice:1b'))))
    reg = registry_with(twice)

    # when
    applied = migrations.run(db, reg, 'boot-1')

    # then
    assert applied == ['core:1', 'core:2'] and 'twice:1' not in seen
    assert reg.get('twice').state == 'broken'


def test_locked_plugin_migrations_do_not_run(seen):
    # given
    db = fresh()
    locked = dataclasses.replace(enterprise_plugin.PLUGIN, migrations=(Migration(1, 'locked', noting(seen, 'enterprise-demo:1')),))
    reg = registry_with(DEMO, locked)

    # when
    applied = migrations.run(db, reg, 'boot-1')

    # then
    assert reg.get('enterprise-demo').state == 'locked'
    assert 'enterprise-demo:1' not in seen and not any(a.startswith('enterprise-demo') for a in applied)
    assert db['schema_version'].find_one({'_id': 'enterprise-demo'}) is None


def lease_state(now=0.0):
    box = {'now': now}
    return box, (lambda: box['now'])


def test_a_lost_lease_stops_the_runner_and_never_moves_the_version_back(seen):
    # given
    db = fresh()
    box, clock = lease_state()
    ran = []
    regs = []

    def slow(d):
        ran.append('A:2')
        if ran.count('A:2') > 1:
            return
        box['now'] = migrations.LOCK_S + 10
        ran.append(('B', migrations.run(d, regs[0], 'B', clock=clock)))

    shaky = minimal('alpha', 'Alpha', migrations=(Migration(1, 'one', noting(ran, 'v1')), Migration(2, 'two', slow), Migration(3, 'three', noting(ran, 'v3'))))
    regs.append(registry_with(shaky))

    # when
    applied = migrations.attempt(db, regs[0], 'A', clock=clock)

    # then
    record = db['schema_version'].find_one({'_id': 'alpha'})
    assert ran.count('v3') == 1 and ran.count('v1') == 1
    assert [r for r in ran if isinstance(r, tuple)] == [('B', ['alpha:2', 'alpha:3'])]
    assert applied is None
    assert record['version'] == 3 and [a['version'] for a in record['applied']] == [1, 2, 3]
    assert regs[0].get('alpha').state == 'ok'


def test_a_lease_held_by_another_runner_is_never_released_by_this_one(seen):
    # given
    db = fresh()
    box, clock = lease_state()

    def steal(d):
        d['schema_version'].replace_one({'_id': '_lock'}, {'_id': '_lock', 'owner': 'B', 'until': 10 ** 9})

    stolen = minimal('alpha', 'Alpha', migrations=(Migration(1, 'one', steal), Migration(2, 'two', noting(seen, 'alpha:2'))))
    reg = registry_with(stolen)

    # when
    applied = migrations.attempt(db, reg, 'A', clock=clock)

    # then
    assert db['schema_version'].find_one({'_id': '_lock'}) == {'_id': '_lock', 'owner': 'B', 'until': 10 ** 9}
    assert 'alpha:2' not in seen and applied is None
    assert reg.get('alpha').state == 'ok'


def test_the_lease_is_renewed_after_every_migration(seen):
    # given
    db = fresh()
    box, clock = lease_state(100.0)
    untils = []

    def watch(d):
        untils.append(d['schema_version'].find_one({'_id': '_lock'})['until'])
        box['now'] += 50

    reg = registry_with(minimal('alpha', 'Alpha', migrations=(Migration(1, 'a', watch), Migration(2, 'b', watch))))

    # when
    migrations.run(db, reg, 'A', clock=clock)

    # then
    assert untils == [100.0 + migrations.LOCK_S, 150.0 + migrations.LOCK_S]


def test_a_core_failure_with_a_database_error_raises_migration_failed(monkeypatch):
    # given
    def down(_db):
        raise AutoReconnect('mongo down')

    monkeypatch.setattr(migrations, 'CORE', (Migration(1, 'bad', down),))

    # when
    db = fresh()
    with pytest.raises(migrations.MigrationFailed, match=r'migration 1 \(bad\) failed: AutoReconnect'):
        migrations.run(db, registry_with(DEMO), 'A')

    # then
    assert db['schema_version'].find_one({'_id': '_lock'}) is None


def test_a_stored_version_ahead_of_the_code_warns_once_and_changes_nothing(seen, caplog):
    # given
    db = fresh()
    db['schema_version'].insert_one({'_id': 'alpha', 'version': 9, 'applied': []})
    reg = registry_with(minimal('alpha', 'Alpha', migrations=(Migration(1, 'one', noting(seen, 'alpha:1')),)))

    # when
    with caplog.at_level(logging.WARNING, logger='skopax.migrations'):
        applied = migrations.run(db, reg, 'A')

    # then
    assert 'alpha:1' not in applied and db['schema_version'].find_one({'_id': 'alpha'})['version'] == 9
    assert len([r for r in caplog.records if 'ahead' in r.getMessage()]) == 1


def clocked_sleep(box, sleeps):
    def sleep(seconds):
        sleeps.append(seconds)
        box['now'] += seconds
    return sleep


def test_boot_waits_for_a_held_lease_polling_every_five_seconds(seen):
    # given
    db = fresh()
    box, clock = lease_state(1000.0)
    sleeps = []
    db['schema_version'].insert_one({'_id': '_lock', 'owner': 'other', 'until': 1030.0})

    # when
    applied = migrations.wait_and_run(db, registry_with(DEMO), 'B', clock=clock, sleep=clocked_sleep(box, sleeps))

    # then
    assert applied == ['core:1', 'core:2', 'demo:1', 'demo:2'] and sleeps == [5] * 6


def test_boot_gives_up_waiting_after_the_lease_time_and_reports_it(seen):
    # given
    db = fresh()
    box, clock = lease_state(1000.0)
    sleeps = []
    db['schema_version'].insert_one({'_id': '_lock', 'owner': 'other', 'until': 10 ** 9})

    # when
    applied = migrations.wait_and_run(db, registry_with(DEMO), 'B', clock=clock, sleep=clocked_sleep(box, sleeps))

    # then
    assert applied is None and len(sleeps) == migrations.LOCK_S // 5 and seen == []


def test_the_runtime_tick_retries_migrations_until_caught_up(seen):
    # given
    db = fresh()
    reg = registry_with(DEMO)
    db['schema_version'].insert_one({'_id': '_lock', 'owner': 'other', 'until': 10 ** 12})
    runtime = Runtime(reg, PluginState(db), Instances(db, reg, lambda: Sealer('k')))
    runtime.migrate = lambda: migrations.attempt(db, reg, 'B')

    # when
    runtime.refresh()
    held = runtime.migrate is not None and seen == []
    db['schema_version'].delete_one({'_id': '_lock'})
    runtime.refresh()
    runtime.refresh()

    # then
    assert held and runtime.migrate is None and seen == ['core:1', 'core:2']


def test_boot_stops_when_a_core_migration_fails(store, monkeypatch):
    # given
    def boom(_db):
        raise RuntimeError('core broke')

    monkeypatch.setattr(migrations, 'CORE', (Migration(1, 'bad', boom),))

    # when
    with pytest.raises(migrations.MigrationFailed, match=r'migration 1 \(bad\) failed: RuntimeError'):
        with TestClient(create_app(settings(), store, registry=registry_with(DEMO))):
            pass

    # then
    assert store.db['schema_version'].find_one({'_id': '_lock'}) is None


def test_boot_serves_with_a_held_lease_and_catches_up_on_the_tick(store, monkeypatch, seen):
    # given
    monkeypatch.setattr(migrations, 'WAIT_S', 0.05)
    monkeypatch.setattr(migrations, 'POLL_S', 0.01)
    store.db['schema_version'].insert_one({'_id': '_lock', 'owner': 'other', 'until': 10 ** 12})

    # when
    with TestClient(create_app(settings(), store, registry=registry_with(DEMO))) as client:
        runtime = client.app.state.runtime
        waiting = runtime.migrate is not None and seen == []
        store.db['schema_version'].delete_one({'_id': '_lock'})
        runtime.refresh()

    # then
    assert waiting and runtime.migrate is None and seen == ['core:1', 'core:2']
    assert store.db['schema_version'].find_one({'_id': 'demo'})['version'] == 2


SENT = 'zq9-sentinel-secret'


def on_schema(monkeypatch, db, name, wrap):
    col = db['schema_version']
    real = getattr(type(col), name)

    def patched(self, *a, **k):
        if self.name == 'schema_version':
            return wrap(real, self, *a, **k)
        return real(self, *a, **k)

    monkeypatch.setattr(type(col), name, patched)


def test_boot_stops_with_the_core_failure_when_the_lease_release_also_fails(store, monkeypatch):
    # given
    down = {'on': False}

    def bad(_db):
        down['on'] = True
        raise AutoReconnect('connection reset')

    def delete_one(real, self, *a, **k):
        if down['on']:
            raise AutoReconnect('still down')
        return real(self, *a, **k)

    monkeypatch.setattr(migrations, 'CORE', (Migration(1, 'remap', bad),))
    on_schema(monkeypatch, store.db, 'delete_one', delete_one)

    # when
    with pytest.raises(migrations.MigrationFailed, match=r'migration 1 \(remap\) failed: AutoReconnect'):
        with TestClient(create_app(settings(), store, registry=registry_with(DEMO))):
            pass

    # then
    assert down['on']


def test_a_lease_error_at_boot_serves_and_leaves_the_retry_armed(store, monkeypatch, seen):
    # given
    real = migrations._lease
    state = {'down': True}

    def flaky(col, boot_id, now):
        if state['down']:
            raise AutoReconnect('mongo blip')
        return real(col, boot_id, now)

    monkeypatch.setattr(migrations, '_lease', flaky)

    # when
    with TestClient(create_app(settings(), store, registry=registry_with(DEMO))) as client:
        runtime = client.app.state.runtime
        armed = runtime.migrate is not None and seen == []
        state['down'] = False
        runtime.refresh()

    # then
    assert armed and runtime.migrate is None and seen == ['core:1', 'core:2']


def test_a_release_error_after_a_clean_run_does_not_raise(monkeypatch, seen):
    # given
    db = fresh()

    def delete_one(real, self, *a, **k):
        raise AutoReconnect('still down')

    on_schema(monkeypatch, db, 'delete_one', delete_one)

    # when
    applied = migrations.run(db, registry_with(DEMO), 'A')

    # then
    assert applied == ['core:1', 'core:2', 'demo:1', 'demo:2']


def test_the_heartbeat_keeps_the_lease_through_a_migration_longer_than_the_lease_time(monkeypatch):
    # given
    monkeypatch.setattr(migrations, 'CORE', ())
    monkeypatch.setattr(migrations, 'LOCK_S', 0.3)
    db = fresh()
    rival = []
    entered = []

    def long_one(d):
        entered.append(1)
        if len(entered) > 1:
            return
        time.sleep(0.8)
        rival.append(migrations.attempt(d, reg, 'B'))

    reg = registry_with(minimal('alpha', 'Alpha', migrations=(Migration(1, 'long', long_one),)))

    # when
    applied = migrations.attempt(db, reg, 'A')

    # then
    assert rival == [None] and applied == ['alpha:1']
    assert db['schema_version'].find_one({'_id': 'alpha'})['version'] == 1
    assert db['schema_version'].find_one({'_id': '_lock'}) is None


def test_the_heartbeat_stops_after_the_run(monkeypatch):
    # given
    monkeypatch.setattr(migrations, 'CORE', ())
    monkeypatch.setattr(migrations, 'LOCK_S', 0.3)
    db = fresh()
    reg = registry_with(minimal('alpha', 'Alpha', migrations=(Migration(1, 'one', lambda d: None),)))

    # when
    migrations.attempt(db, reg, 'A')
    time.sleep(0.4)

    # then
    assert db['schema_version'].find_one({'_id': '_lock'}) is None
    assert not [t for t in threading.enumerate() if t.name == 'skopax-migration-heartbeat']


def test_a_lost_lease_never_breaks_the_plugin_or_the_ones_after_it(seen):
    # given
    db = fresh()
    box, clock = lease_state()
    ran = []
    regs = []

    def slow(d):
        ran.append('alpha')
        if ran.count('alpha') == 1:
            box['now'] = migrations.LOCK_S + 10
            migrations.run(d, regs[0], 'B', clock=clock)

    alpha = minimal('alpha', 'Alpha', migrations=(Migration(1, 'one', slow),))
    beta = minimal('beta', 'Beta', migrations=(Migration(1, 'one', noting(ran, 'beta')),))
    regs.append(registry_with(alpha, beta))

    # when
    result = migrations.attempt(db, regs[0], 'A', clock=clock)
    regs[0].refresh()

    # then
    assert result is None
    assert ran.count('beta') == 1
    assert [regs[0].get(i).state for i in ('alpha', 'beta')] == ['ok', 'ok']


def test_a_lost_lease_arms_the_retry_on_the_runtime_tick(seen):
    # given
    db = fresh()
    ran = []
    steal = {'on': True}

    def take(d):
        ran.append('x')
        if steal['on']:
            steal['on'] = False
            d['schema_version'].replace_one({'_id': '_lock'}, {'_id': '_lock', 'owner': 'B', 'until': 10 ** 9})

    reg = registry_with(minimal('alpha', 'Alpha', migrations=(Migration(1, 'one', take),)))
    runtime = Runtime(reg, PluginState(db), Instances(db, reg, lambda: Sealer('k')))
    runtime.migrate = lambda: migrations.attempt(db, reg, 'A')

    # when
    runtime.refresh()
    first = runtime.migrate is not None and reg.get('alpha').state == 'ok'
    db['schema_version'].delete_one({'_id': '_lock'})
    runtime.refresh()

    # then
    assert first and runtime.migrate is None and reg.get('alpha').state == 'ok'
    assert db['schema_version'].find_one({'_id': 'alpha'})['version'] == 1


def test_a_later_successful_run_clears_the_broken_mark_the_runner_set(seen):
    # given
    db = fresh()
    fail = [True]

    def flaky(d):
        if fail[0]:
            raise RuntimeError(SENT)

    reg = registry_with(minimal('alpha', 'Alpha', migrations=(Migration(1, 'one', flaky),)))

    # when
    migrations.attempt(db, reg, 'A')
    first = reg.get('alpha').state
    fail[0] = False
    migrations.attempt(db, reg, 'B')

    # then
    assert first == 'broken' and reg.get('alpha').state == 'ok'
    assert db['schema_version'].find_one({'_id': 'alpha'})['version'] == 1


def test_the_same_boot_takes_its_own_unexpired_lease_again_but_another_does_not():
    # given
    col = fresh()['schema_version']

    # when
    first = migrations._lease(col, 'A', 0.0)
    again = migrations._lease(col, 'A', 10.0)
    other = migrations._lease(col, 'B', 20.0)
    later = migrations._lease(col, 'B', migrations.LOCK_S + 20.0)

    # then
    assert [first, again, other, later] == [True, True, False, True]


def test_a_lower_version_write_never_lowers_the_stored_version(monkeypatch):
    # given
    monkeypatch.setattr(migrations, 'CORE', ())
    db = fresh()

    def bump(d):
        d['schema_version'].update_one({'_id': 'alpha'}, {'$set': {'version': 9}}, upsert=True)

    reg = registry_with(minimal('alpha', 'Alpha', migrations=(Migration(1, 'one', bump),)))

    # when
    applied = migrations.attempt(db, reg, 'A')

    # then
    assert applied == ['alpha:1'] and db['schema_version'].find_one({'_id': 'alpha'})['version'] == 9


def test_every_migration_is_bracketed_by_its_own_two_renewals(monkeypatch):
    # given
    monkeypatch.setattr(migrations, 'CORE', ())
    events = []
    real = migrations._renew

    def spy(col, boot_id, clock):
        events.append('renew')
        return real(col, boot_id, clock)

    monkeypatch.setattr(migrations, '_renew', spy)
    reg = registry_with(minimal('alpha', 'Alpha', migrations=(Migration(1, 'a', lambda d: events.append('run')), Migration(2, 'b', lambda d: events.append('run')))))

    # when
    migrations.attempt(fresh(), reg, 'A')

    # then
    assert events == ['renew', 'run', 'renew', 'renew', 'run', 'renew']


def test_a_core_bookkeeping_database_error_is_a_core_failure_with_the_type_only(monkeypatch):
    # given
    db = fresh()
    monkeypatch.setattr(migrations, 'CORE', (Migration(1, 'ok', lambda d: None),))

    def find_one(real, self, *a, **k):
        if a and a[0] == {'_id': 'core'}:
            raise AutoReconnect(SENT)
        return real(self, *a, **k)

    on_schema(monkeypatch, db, 'find_one', find_one)

    # when
    with pytest.raises(migrations.MigrationFailed) as caught:
        migrations.attempt(db, registry_with(DEMO), 'A')

    # then
    assert str(caught.value) == 'a core migration failed: AutoReconnect'
    assert db['schema_version'].find_one({'_id': '_lock'}) is None


def test_a_plugin_bookkeeping_error_names_the_migration(monkeypatch):
    # given
    monkeypatch.setattr(migrations, 'CORE', ())
    db = fresh()

    def update_one(real, self, *a, **k):
        if a and a[0] == {'_id': 'alpha'}:
            raise OperationFailure(SENT)
        return real(self, *a, **k)

    on_schema(monkeypatch, db, 'update_one', update_one)
    reg = registry_with(minimal('alpha', 'Alpha', migrations=(Migration(1, 'one', lambda d: None),)))

    # when
    migrations.attempt(db, reg, 'A')

    # then
    reason = reg.get('alpha').reason
    assert 'migration 1 (one) failed: OperationFailure' in reason and 'migration 0' not in reason and SENT not in reason


def test_a_migration_error_text_never_carries_the_exception_message(monkeypatch, caplog):
    # given
    def boom(_d):
        raise RuntimeError(SENT)

    reg = registry_with(minimal('alpha', 'Alpha', migrations=(Migration(1, 'one', boom),)))
    monkeypatch.setattr(migrations, 'CORE', (Migration(1, 'core-bad', boom),))
    db = fresh()

    # when
    with caplog.at_level(logging.DEBUG):
        with pytest.raises(migrations.MigrationFailed) as core:
            migrations.attempt(db, reg, 'A')
        monkeypatch.setattr(migrations, 'CORE', ())
        migrations.attempt(db, reg, 'A')
    shown = str(core.value) + reg.get('alpha').reason + caplog.text

    # then
    assert SENT not in shown
    assert 'migration 1 (core-bad) failed: RuntimeError' in str(core.value)
    assert 'migration 1 (one) failed: RuntimeError' in reg.get('alpha').reason


def test_the_runtime_tick_logs_a_failing_core_migration_once_at_error_level_and_keeps_retrying(monkeypatch, caplog):
    # given
    runs = []

    def bad(_d):
        runs.append(1)
        raise RuntimeError(SENT)

    monkeypatch.setattr(migrations, 'CORE', (Migration(1, 'bad', bad),))
    db = fresh()
    reg = registry_with(DEMO)
    runtime = Runtime(reg, PluginState(db), Instances(db, reg, lambda: Sealer('k')))
    runtime.migrate = lambda: migrations.attempt(db, reg, 'B')

    # when
    with caplog.at_level(logging.WARNING, logger='skopax.runtime'):
        for _ in range(4):
            runtime.refresh()

    # then
    assert len(runs) == 4 and runtime.migrate is not None
    assert [r.levelname for r in caplog.records] == ['ERROR']
    assert SENT not in caplog.text and 'RuntimeError' in caplog.text


def beats():
    return [t for t in threading.enumerate() if t.name == 'skopax-migration-heartbeat']


def test_a_mongo_error_before_the_migrations_leaves_the_retry_armed_and_the_next_tick_runs_them(store, monkeypatch, seen, caplog):
    # given
    state = {'down': True}
    real = type(store).ensure_indexes

    def flaky(self):
        if state['down']:
            raise AutoReconnect(SENT)
        return real(self)

    monkeypatch.setattr(type(store), 'ensure_indexes', flaky)
    monkeypatch.setattr(Runtime, 'loop', no_loop)

    # when
    with caplog.at_level(logging.WARNING):
        with TestClient(create_app(settings(), store, registry=registry_with(DEMO))) as client:
            runtime = client.app.state.runtime
            armed = runtime.migrate is not None and seen == []
            state['down'] = False
            runtime.refresh()

    # then
    assert armed and runtime.migrate is None and seen == ['core:1', 'core:2']
    assert SENT not in caplog.text and 'index setup skipped, Mongo not reachable (AutoReconnect)' in caplog.text


def test_a_core_failure_after_a_lease_error_in_the_same_streak_still_logs_at_error_level(monkeypatch, caplog):
    # given
    runs = []

    def bad(_d):
        runs.append(1)
        raise RuntimeError(SENT)

    monkeypatch.setattr(migrations, 'CORE', (Migration(1, 'bad', bad),))
    db = fresh()
    reg = registry_with(DEMO)
    runtime = Runtime(reg, PluginState(db), Instances(db, reg, lambda: Sealer('k')))
    real = migrations._lease
    tick = {'n': 0}

    def flaky(col, boot_id, now):
        if tick['n'] == 1:
            raise AutoReconnect(SENT)
        return real(col, boot_id, now)

    monkeypatch.setattr(migrations, '_lease', flaky)
    runtime.migrate = lambda: migrations.attempt(db, reg, 'B')

    # when
    with caplog.at_level(logging.WARNING, logger='skopax.runtime'):
        for n in range(1, 5):
            tick['n'] = n
            runtime.refresh()

    # then
    assert [r.levelname for r in caplog.records] == ['WARNING', 'ERROR']
    assert SENT not in caplog.text and 'migration bad' in caplog.records[1].getMessage() and 'RuntimeError' in caplog.records[1].getMessage()


def test_the_boot_stop_exception_never_carries_the_message_in_its_traceback(store, monkeypatch):
    # given
    def boom(_db):
        raise RuntimeError(SENT)

    monkeypatch.setattr(migrations, 'CORE', (Migration(1, 'bad', boom),))

    # when
    with pytest.raises(migrations.MigrationFailed) as caught:
        with TestClient(create_app(settings(), store, registry=registry_with(DEMO))):
            pass

    # then
    assert SENT not in ''.join(traceback.format_exception(caught.value))


def test_a_plugin_phase_outage_stops_the_run_marks_nothing_broken_and_keeps_the_retry(monkeypatch, seen):
    # given
    db = fresh()
    alpha = minimal('alpha', 'Alpha', migrations=(Migration(1, 'one', noting(seen, 'alpha:1')),))
    reg = registry_with(DEMO, alpha)
    real = migrations._renew
    calls = {'n': 0}

    def renew(col, boot_id, clock):
        calls['n'] += 1
        if calls['n'] > 2:
            raise ServerSelectionTimeoutError(SENT)
        return real(col, boot_id, clock)

    monkeypatch.setattr(migrations, 'CORE', (Migration(1, 'c', noting(seen, 'core')),))
    monkeypatch.setattr(migrations, '_renew', renew)

    # when
    first = migrations.attempt(db, reg, 'A')
    states = [reg.get(i).state for i in ('demo', 'alpha')]
    calls['n'] = -100
    second = migrations.attempt(db, reg, 'A')

    # then
    assert first is None and states == ['ok', 'ok']
    assert second is not None and 'alpha:1' in second and reg.get('alpha').state == 'ok'
    assert db['schema_version'].find_one({'_id': '_lock'}) is None


def test_a_plugin_migration_that_loses_its_connection_while_mongo_is_up_breaks_that_plugin_like_any_other_error(monkeypatch):
    # given
    monkeypatch.setattr(migrations, 'CORE', ())
    db = fresh()

    def drops(_d):
        raise NetworkTimeout(SENT)

    def other(_d):
        raise OperationFailure(SENT)

    a = minimal('alpha', 'Alpha', migrations=(Migration(1, 'one', drops),))
    b = minimal('beta', 'Beta', migrations=(Migration(1, 'one', other),))
    reg = registry_with(a, b)

    # when
    result = migrations.attempt(db, reg, 'A')

    # then
    assert result == []
    assert [reg.get(i).state for i in ('alpha', 'beta')] == ['broken', 'broken']
    assert 'migration 1 (one) failed: NetworkTimeout' in reg.get('alpha').reason
    assert 'migration 1 (one) failed: OperationFailure' in reg.get('beta').reason


def test_a_plugin_migration_that_loses_its_connection_during_an_outage_is_retried_not_broken(monkeypatch):
    # given
    monkeypatch.setattr(migrations, 'CORE', ())
    db = fresh()
    real = migrations._renew
    down = {'on': False}

    def drops(_d):
        down['on'] = True
        raise NetworkTimeout(SENT)

    def renew(col, boot_id, clock):
        if down['on']:
            raise ServerSelectionTimeoutError(SENT)
        return real(col, boot_id, clock)

    monkeypatch.setattr(migrations, '_renew', renew)
    reg = registry_with(minimal('alpha', 'Alpha', migrations=(Migration(1, 'one', drops),)))

    # when
    result = migrations.attempt(db, reg, 'A')

    # then
    assert result is None and reg.get('alpha').state == 'ok'


def test_a_failed_release_leaves_the_lock_and_no_heartbeat_keeps_renewing_it(monkeypatch):
    # given
    monkeypatch.setattr(migrations, 'CORE', ())
    monkeypatch.setattr(migrations, 'LOCK_S', 0.3)
    db = fresh()

    def delete_one(real, self, *a, **k):
        raise AutoReconnect('still down')

    on_schema(monkeypatch, db, 'delete_one', delete_one)
    reg = registry_with(minimal('alpha', 'Alpha', migrations=(Migration(1, 'one', lambda d: None),)))

    # when
    migrations.attempt(db, reg, 'A')
    first = db['schema_version'].find_one({'_id': '_lock'})['until']
    time.sleep(0.5)
    second = db['schema_version'].find_one({'_id': '_lock'})['until']

    # then
    assert not beats() and second == first


def test_a_heartbeat_error_is_logged_by_type_and_the_beating_goes_on(monkeypatch, caplog):
    # given
    monkeypatch.setattr(migrations, 'CORE', ())
    monkeypatch.setattr(migrations, 'LOCK_S', 0.3)
    db = fresh()
    flaky = {'on': False}

    def update_one(real, self, *a, **k):
        if flaky['on'] and threading.current_thread().name == 'skopax-migration-heartbeat':
            raise AutoReconnect(SENT)
        return real(self, *a, **k)

    def long_one(_d):
        flaky['on'] = True
        time.sleep(0.45)
        flaky['on'] = False

    on_schema(monkeypatch, db, 'update_one', update_one)
    reg = registry_with(minimal('alpha', 'Alpha', migrations=(Migration(1, 'long', long_one),)))

    # when
    with caplog.at_level(logging.WARNING, logger='skopax.migrations'):
        applied = migrations.attempt(db, reg, 'A')

    # then
    failures = [r for r in caplog.records if 'could not be renewed' in r.getMessage()]
    assert applied == ['alpha:1'] and len(failures) >= 2
    assert SENT not in caplog.text and all('AutoReconnect' in r.getMessage() for r in failures)


def test_the_heartbeat_renews_every_third_of_the_lease_time(monkeypatch):
    # given
    monkeypatch.setattr(migrations, 'CORE', ())
    monkeypatch.setattr(migrations, 'LOCK_S', 0.9)
    db = fresh()
    count = []

    def update_one(real, self, *a, **k):
        if threading.current_thread().name == 'skopax-migration-heartbeat':
            count.append(1)
        return real(self, *a, **k)

    on_schema(monkeypatch, db, 'update_one', update_one)
    reg = registry_with(minimal('alpha', 'Alpha', migrations=(Migration(1, 'long', lambda d: time.sleep(1.0)),)))

    # when
    migrations.attempt(db, reg, 'A')

    # then
    assert 2 <= len(count) <= 4


def test_a_core_failure_carries_the_class_name_and_the_migration_for_the_runtime_log(monkeypatch):
    # given
    class _Private(AutoReconnect):
        pass

    def boom(_d):
        raise _Private(SENT)

    monkeypatch.setattr(migrations, 'CORE', (Migration(1, 'bad', boom),))

    # when
    with pytest.raises(migrations.MigrationFailed) as caught:
        migrations.attempt(fresh(), registry_with(DEMO), 'A')

    # then
    assert str(caught.value) == 'migration 1 (bad) failed: AutoReconnect'
    assert caught.value.migration == 'bad' and caught.value.error == 'AutoReconnect'


def test_the_log_and_reason_texts_never_carry_the_exception_message(store, monkeypatch, caplog):
    # given
    def release_fails(real, self, *a, **k):
        raise AutoReconnect(SENT)

    db = store.db
    on_schema(monkeypatch, db, 'delete_one', release_fails)
    monkeypatch.setattr(migrations, 'CORE', ())
    real = migrations._lease
    state = {'down': True}

    def flaky(col, boot_id, now):
        if state['down']:
            raise AutoReconnect(SENT)
        return real(col, boot_id, now)

    monkeypatch.setattr(migrations, '_lease', flaky)
    broken = minimal('alpha', 'Alpha', migrations=(Migration(1, 'one', lambda d: None),))
    links_plugin = minimal('lnk', 'Lnk', links=lambda card, info: (_ for _ in ()).throw(RuntimeError(SENT)))

    # when
    with caplog.at_level(logging.DEBUG):
        with TestClient(create_app(settings(), store, registry=registry_with(DEMO, broken, links_plugin))) as client:
            state['down'] = False
            client.app.state.runtime.links({'cluster': None})
            client.app.state.runtime.refresh()

    # then
    text = caplog.text
    assert SENT not in text
    assert 'could not be taken (AutoReconnect)' in text and 'could not be released (AutoReconnect)' in text and 'links of plug-in lnk failed (RuntimeError)' in text


def test_a_plugin_bookkeeping_error_reads_could_not_be_read_or_recorded_with_the_type_only(monkeypatch):
    # given
    monkeypatch.setattr(migrations, 'CORE', ())
    db = fresh()

    def find_one(real, self, *a, **k):
        if a and a[0] == {'_id': 'alpha'}:
            raise OperationFailure(SENT)
        return real(self, *a, **k)

    on_schema(monkeypatch, db, 'find_one', find_one)
    reg = registry_with(minimal('alpha', 'Alpha', migrations=(Migration(1, 'one', lambda d: None),)))

    # when
    migrations.attempt(db, reg, 'A')

    # then
    assert 'the migrations of alpha could not be read or recorded: OperationFailure' in reg.get('alpha').reason and SENT not in reg.get('alpha').reason


def test_a_plugin_read_that_loses_the_connection_is_retried_not_broken(monkeypatch):
    # given
    monkeypatch.setattr(migrations, 'CORE', ())
    db = fresh()

    def find_one(real, self, *a, **k):
        if a and a[0] == {'_id': 'alpha'}:
            raise AutoReconnect(SENT)
        return real(self, *a, **k)

    on_schema(monkeypatch, db, 'find_one', find_one)
    reg = registry_with(minimal('alpha', 'Alpha', migrations=(Migration(1, 'one', lambda d: None),)))

    # when
    result = migrations.attempt(db, reg, 'A')

    # then
    assert result is None and reg.get('alpha').state == 'ok'
    assert db['schema_version'].find_one({'_id': '_lock'}) is None


def test_the_runtime_log_of_a_migration_failure_uses_the_name_and_type_never_the_text(caplog):
    # given
    db = fresh()
    reg = registry_with(DEMO)
    runtime = Runtime(reg, PluginState(db), Instances(db, reg, lambda: Sealer('k')))

    def failing():
        raise migrations.MigrationFailed(SENT, 'remap', 'RuntimeError')

    runtime.migrate = failing

    # when
    with caplog.at_level(logging.WARNING, logger='skopax.runtime'):
        runtime.refresh()

    # then
    assert SENT not in caplog.text and 'migration remap (RuntimeError)' in caplog.text


def test_a_second_catch_up_while_one_runs_returns_at_once():
    # given
    db = fresh()
    reg = registry_with(DEMO)
    runtime = Runtime(reg, PluginState(db), Instances(db, reg, lambda: Sealer('k')))
    inside, release, calls = threading.Event(), threading.Event(), []

    def slow():
        calls.append('run')
        inside.set()
        release.wait(5)
        return ['core:1']

    runtime.migrate = slow
    first = threading.Thread(target=runtime._catch_up)
    first.start()
    assert inside.wait(5)

    # when
    second = threading.Thread(target=runtime._catch_up)
    second.start()
    second.join(1)
    waited = second.is_alive()
    during = list(calls)
    release.set()
    first.join(5)
    second.join(5)

    # then
    assert not waited and during == ['run'] and runtime.migrate is None


def test_a_catch_up_after_another_thread_finished_the_migrations_is_a_no_op(caplog):
    # given
    db = fresh()
    reg = registry_with(DEMO)
    runtime = Runtime(reg, PluginState(db), Instances(db, reg, lambda: Sealer('k')))
    runtime.migrate = None

    # when
    with caplog.at_level(logging.WARNING):
        runtime._catch_up()

    # then
    assert runtime.migrate is None and 'TypeError' not in caplog.text
