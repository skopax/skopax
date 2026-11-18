import dataclasses
import logging

import mongomock
import pytest
from fastapi.testclient import TestClient
from pymongo.errors import ServerSelectionTimeoutError

from skopax import migrations
from skopax.app import create_app
from skopax.contract import CardType, Migration
from skopax.instances import Instances, PluginState
from skopax.runtime import Runtime
from skopax.sealer import Sealer
from tests.conftest import settings
from tests.plugin_kit import no_loop, registry_with
from tests.plugins import demo_plugin, enterprise_plugin
from tests.plugins.base import minimal

DEMO = demo_plugin.PLUGIN


def test_a_failed_required_plugin_migration_fails_the_start_and_a_rerun_applies_it_once(store):
    # given
    calls = []
    fixed = {'on': False}

    def first(db):
        db['shop_state'].insert_one({'step': 'first'})

    def must(db):
        calls.append('must')
        if not fixed['on']:
            raise RuntimeError('schema not ready 7f3a')
        db['shop_state'].insert_one({'step': 'must'})

    plugin = minimal('shop', 'Shop', migrations=(Migration(1, 'first', first), Migration(2, 'must', must, required=True)))

    # when
    with pytest.raises(migrations.MigrationFailed) as stopped:
        with TestClient(create_app(settings(), store, registry=registry_with(DEMO, plugin))):
            pass
    after_failure = store.db['schema_version'].find_one({'_id': 'shop'})['version']
    lock_after_failure = store.db['schema_version'].find_one({'_id': '_lock'})
    fixed['on'] = True
    with TestClient(create_app(settings(), store, registry=registry_with(DEMO, plugin))) as client:
        state = client.app.state.runtime.registry.get('shop').state
        health = client.get('/actuator/health').status_code
    with TestClient(create_app(settings(), store, registry=registry_with(DEMO, plugin))):
        pass

    # then
    assert 'required migration 2 (must) of shop failed: RuntimeError' in stopped.value.text and '7f3a' not in str(stopped.value)
    assert stopped.value.migration == 'must' and stopped.value.error == 'RuntimeError'
    assert after_failure == 1 and lock_after_failure is None
    assert calls == ['must', 'must'] and state == 'ok' and health == 200
    assert [d['step'] for d in store.db['shop_state'].find()] == ['first', 'must']
    assert [a['version'] for a in store.db['schema_version'].find_one({'_id': 'shop'})['applied']] == [1, 2]


def test_a_failed_optional_migration_still_only_breaks_its_plugin(store):
    # given
    def must(db):
        db['shop_state'].insert_one({'step': 'must'})

    def maybe(_db):
        raise RuntimeError('optional broke 9c1d')

    plugin = minimal('shop', 'Shop', migrations=(Migration(1, 'must', must, required=True), Migration(2, 'maybe', maybe, required=False)))

    # when
    with TestClient(create_app(settings(), store, registry=registry_with(DEMO, plugin))) as client:
        shop = client.app.state.runtime.registry.get('shop')
        demo = client.app.state.runtime.registry.get('demo')
        health = client.get('/actuator/health').status_code

    # then
    assert health == 200 and demo.state == 'ok'
    assert shop.state == 'broken' and 'RuntimeError' in shop.reason and '9c1d' not in shop.reason
    assert store.db['schema_version'].find_one({'_id': 'shop'})['version'] == 1
    assert store.db['schema_version'].find_one({'_id': 'demo'})['version'] == 2
    assert Migration(1, 'x', must).required is False


def boot(store, *plugins):
    with TestClient(create_app(settings(), store, registry=registry_with(DEMO, *plugins))) as client:
        return client.get('/actuator/health').status_code


def stopped_by(store, *plugins) -> migrations.RequiredFailed:
    with pytest.raises(migrations.RequiredFailed) as stopped:
        boot(store, *plugins)
    return stopped.value


def needed(db):
    db['shop_state'].insert_one({'step': 'needed'})


def test_an_optional_migration_that_fails_before_a_pending_required_one_stops_the_start(store):
    # given
    def maybe(_db):
        raise RuntimeError('optional broke 9c1d')

    plugin = minimal('shop', 'Shop', migrations=(Migration(1, 'maybe', maybe), Migration(2, 'needed', needed, required=True)))

    # when
    stopped = stopped_by(store, plugin)

    # then
    assert stopped.text == 'the required migration 2 (needed) of shop is not applied'
    assert '9c1d' not in str(stopped) and stopped.migration == 'needed'
    assert store.db['schema_version'].find_one({'_id': 'shop'}) is None and store.db['schema_version'].find_one({'_id': '_lock'}) is None


def test_a_repeated_version_with_a_required_migration_stops_the_start(store):
    # given
    plugin = minimal('shop', 'Shop', migrations=(Migration(1, 'needed', needed, required=True), Migration(1, 'again', needed)))

    # when
    stopped = stopped_by(store, plugin)

    # then
    assert stopped.text == 'the required migration 1 (needed) of shop is not applied'
    assert list(store.db['shop_state'].find()) == []


def test_a_plugin_broken_at_load_with_a_pending_required_migration_stops_the_start(store):
    # given
    plugin = minimal('shop', 'Shop', card_types=(CardType('log', 'Shop log'),), migrations=(Migration(1, 'needed', needed, required=True),))

    # when
    stopped = stopped_by(store, plugin)

    # then
    assert registry_with(DEMO, plugin).get('shop').state == 'broken'
    assert stopped.text == 'the required migration 1 (needed) of shop is not applied'


def test_a_locked_plugin_with_a_pending_required_migration_stops_the_start(store):
    # given
    plugin = dataclasses.replace(enterprise_plugin.PLUGIN, migrations=(Migration(3, 'needed', needed, required=True),))

    # when
    stopped = stopped_by(store, plugin)

    # then
    assert registry_with(DEMO, plugin).get('enterprise-demo').state == 'locked'
    assert stopped.text == 'the required migration 3 (needed) of enterprise-demo is not applied'


def test_a_disabled_plugin_with_a_failing_required_migration_stops_the_start(store):
    # given
    def must(_db):
        raise RuntimeError('schema not ready 7f3a')

    PluginState(store.db).set('shop', False, 'alice@example.com')
    plugin = minimal('shop', 'Shop', migrations=(Migration(1, 'must', must, required=True),))

    # when
    stopped = stopped_by(store, plugin)

    # then
    assert 'required migration 1 (must) of shop' in stopped.text and '7f3a' not in str(stopped)


def test_a_required_instances_migration_without_a_writer_is_not_caught_up():
    # given
    db = mongomock.MongoClient(tz_aware=True)['skopax']
    seen = []
    plugin = minimal('shop', 'Shop', migrations=(Migration(1, 'seed', lambda _db, writer: seen.append(writer), needs_instances=True, required=True),))

    # when
    with pytest.raises(migrations.RequiredFailed) as stopped:
        migrations.run(db, registry_with(DEMO, plugin), 'boot-1')

    # then
    assert stopped.value.text == 'the required migration 1 (seed) of shop is not applied'
    assert seen == [] and db['schema_version'].find_one({'_id': '_lock'}) is None


def test_a_lease_held_by_another_pod_at_boot_with_a_pending_required_migration_stops_the_start(store, monkeypatch):
    # given
    monkeypatch.setattr(migrations, 'WAIT_S', 0)
    store.db['schema_version'].insert_one({'_id': '_lock', 'owner': 'other-pod', 'until': 10 ** 12})
    plugin = minimal('shop', 'Shop', migrations=(Migration(1, 'needed', needed, required=True),))

    # when
    stopped = stopped_by(store, plugin)
    served = boot(store)

    # then
    assert stopped.text == 'the required migration 1 (needed) of shop is not applied'
    assert served == 200, 'with nothing required pending a held lease still serves'
    assert store.db['schema_version'].find_one({'_id': '_lock'})['owner'] == 'other-pod'


def test_an_interrupted_boot_with_a_pending_required_migration_stops_the_start(monkeypatch):
    # given
    db = mongomock.MongoClient(tz_aware=True)['skopax']
    plugin = minimal('shop', 'Shop', migrations=(Migration(1, 'needed', needed, required=True),))
    real = migrations._renew
    calls = {'n': 0}

    def renew(col, boot_id, clock):
        calls['n'] += 1
        if calls['n'] > 1:
            raise ServerSelectionTimeoutError('mongo gone 5e2b')
        return real(col, boot_id, clock)

    monkeypatch.setattr(migrations, '_renew', renew)

    # when
    with pytest.raises(migrations.RequiredFailed) as stopped:
        migrations.wait_and_run(db, registry_with(DEMO, plugin), 'boot-1')

    # then
    assert stopped.value.text == 'the required migration 1 (needed) of shop is not applied'
    assert '5e2b' not in str(stopped.value)


def test_a_boot_that_cannot_read_the_stored_versions_counts_a_required_migration_as_pending(monkeypatch):
    # given
    db = mongomock.MongoClient(tz_aware=True)['skopax']
    db['schema_version'].insert_one({'_id': '_lock', 'owner': 'other-pod', 'until': 10 ** 12})
    plugin = minimal('shop', 'Shop', migrations=(Migration(1, 'needed', needed, required=True),))
    real = migrations._stored

    def stored(col, owner):
        if owner == 'shop':
            raise ServerSelectionTimeoutError('mongo gone 5e2b')
        return real(col, owner)

    monkeypatch.setattr(migrations, '_stored', stored)

    # when
    with pytest.raises(migrations.RequiredFailed) as stopped:
        migrations.wait_and_run(db, registry_with(DEMO, plugin), 'boot-1', wait_s=0)

    # then
    assert stopped.value.text == 'the required migrations of shop could not be checked (ServerSelectionTimeoutError)'
    assert '5e2b' not in str(stopped.value)


def test_after_the_start_a_pending_required_migration_is_logged_once_at_error_level_and_retried(caplog):
    # given
    db = mongomock.MongoClient(tz_aware=True)['skopax']
    plugin = dataclasses.replace(enterprise_plugin.PLUGIN, migrations=(Migration(3, 'needed', needed, required=True),))
    reg = registry_with(DEMO, plugin)
    runtime = Runtime(reg, PluginState(db), Instances(db, reg, lambda: Sealer('k')))
    runtime.migrate = lambda: migrations.attempt(db, reg, 'B')

    # when
    with caplog.at_level(logging.ERROR, logger='skopax.runtime'):
        runtime.refresh()
        runtime.refresh()

    # then
    errors = [r for r in caplog.records if r.levelno == logging.ERROR and 'migration needed' in r.getMessage()]
    assert len(errors) == 1 and runtime.migrate is not None


def mongo_down(store, monkeypatch):
    def down(self):
        raise ServerSelectionTimeoutError('mongo gone 5e2b')

    monkeypatch.setattr(type(store), 'ensure_indexes', down)
    monkeypatch.setattr(Runtime, 'loop', no_loop)


def test_a_boot_that_cannot_reach_mongo_stops_when_a_plugin_declares_a_required_migration(store, monkeypatch):
    # given
    mongo_down(store, monkeypatch)
    shop = minimal('shop', 'Shop', migrations=(Migration(1, 'needed', needed, required=True),))
    cart = minimal('cart', 'Cart', migrations=(Migration(2, 'carts', needed, required=True),))

    # when
    stopped = stopped_by(store, shop, cart)

    # then
    assert stopped.text == 'the required migrations of cart, shop could not be checked: the database could not be reached (ServerSelectionTimeoutError)'
    assert '5e2b' not in str(stopped)


def test_a_boot_that_cannot_reach_mongo_still_serves_when_no_plugin_declares_a_required_migration(store, monkeypatch, caplog):
    # given
    mongo_down(store, monkeypatch)
    plugin = minimal('shop', 'Shop', migrations=(Migration(1, 'maybe', needed),))

    # when
    with caplog.at_level(logging.WARNING):
        with TestClient(create_app(settings(), store, registry=registry_with(DEMO, plugin))) as client:
            armed = client.app.state.runtime.migrate is not None
            health = client.get('/actuator/health').status_code

    # then
    assert armed and health == 200
    assert 'index setup skipped, Mongo not reachable (ServerSelectionTimeoutError)' in caplog.text


def test_a_plugin_that_lost_an_id_clash_does_not_govern_the_required_migrations_of_that_id(store):
    # given
    winner = minimal('shop', 'Shop', migrations=(Migration(1, 'plain', needed),))
    loser = minimal('shop', 'Shop copy', migrations=(Migration(5, 'needed', needed, required=True),))
    registry = registry_with(DEMO, winner, loser)

    # when
    with TestClient(create_app(settings(), store, registry=registry)) as client:
        health = client.get('/actuator/health').status_code

    # then
    assert [x.state for x in registry.loaded() if x.id == 'shop'] == ['ok', 'broken']
    assert health == 200 and migrations.required_owners(registry) == []
