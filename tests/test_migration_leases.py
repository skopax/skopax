import dataclasses
import logging

import mongomock
import pytest
from pymongo.errors import NetworkTimeout, ServerSelectionTimeoutError

from skopax import migrations
from skopax.contract import Check, Hooks, Migration
from tests.plugin_kit import registry_with
from tests.plugins import enterprise_plugin
from tests.plugins.base import minimal, run

SENT = 'zq9-sentinel-secret'


def fresh():
    return mongomock.MongoClient(tz_aware=True)['skopax']


def noting(log, label):
    return lambda db: log.append(label)


@pytest.fixture(autouse=True)
def no_core(monkeypatch):
    monkeypatch.setattr(migrations, 'CORE', ())
    migrations._interrupted.clear()


def outage(monkeypatch):
    real = migrations._renew
    state = {'down': False}

    def renew(col, boot_id, clock):
        if state['down']:
            raise ServerSelectionTimeoutError(SENT)
        return real(col, boot_id, clock)

    monkeypatch.setattr(migrations, '_renew', renew)
    return state


def dropping(state):
    def run_it(_d):
        state['down'] = True
        raise NetworkTimeout(SENT)

    return run_it


def interrupted_logs(caplog):
    return len([r for r in caplog.records if 'interrupted' in r.getMessage()])


def test_an_interrupted_run_warns_once_until_it_recovers_then_warns_again(monkeypatch, caplog):
    # given
    state = outage(monkeypatch)
    db = fresh()
    seen = []
    state['down'] = True
    reg = registry_with(minimal('alpha', 'Alpha', migrations=(Migration(1, 'one', noting(seen, 'one')),)))

    # when
    with caplog.at_level(logging.WARNING, logger='skopax.migrations'):
        first = [migrations.attempt(db, reg, 'A') for _ in range(3)]
        logged_first = interrupted_logs(caplog)
        state['down'] = False
        recovered = migrations.attempt(db, reg, 'A')
        later = registry_with(minimal('alpha', 'Alpha', migrations=(Migration(1, 'one', noting(seen, 'one')), Migration(2, 'two', noting(seen, 'two')))))
        state['down'] = True
        again = [migrations.attempt(db, later, 'A') for _ in range(2)]
        logged_total = interrupted_logs(caplog)

    # then
    assert first == [None] * 3 and recovered == ['alpha:1'] and again == [None, None]
    assert (logged_first, logged_total) == (1, 2)


def test_an_interrupted_boot_serves_at_once_with_the_real_reason_and_never_waits(monkeypatch, caplog):
    # given
    state = outage(monkeypatch)
    db = fresh()
    reg = registry_with(minimal('alpha', 'Alpha', migrations=(Migration(1, 'one', dropping(state)),)))
    sleeps = []
    clock = {'now': 1000.0}

    def sleep(seconds):
        sleeps.append(seconds)
        clock['now'] += seconds

    # when
    with caplog.at_level(logging.INFO, logger='skopax.migrations'):
        found = migrations.wait_and_run(db, reg, 'B', clock=lambda: clock['now'], sleep=sleep)

    # then
    assert found is None and sleeps == []
    assert 'another instance' not in caplog.text and 'interrupted' in caplog.text and 'NetworkTimeout' in caplog.text and SENT not in caplog.text


def test_a_connection_error_whose_probe_finds_the_lease_lost_is_a_lost_lease_not_an_interruption(monkeypatch, caplog):
    # given
    db = fresh()
    real = migrations._renew
    state = {'lost': False}

    def drops(_d):
        state['lost'] = True
        raise NetworkTimeout(SENT)

    def renew(col, boot_id, clock):
        if state['lost']:
            raise migrations.LeaseLost(SENT)
        return real(col, boot_id, clock)

    monkeypatch.setattr(migrations, '_renew', renew)
    reg = registry_with(minimal('alpha', 'Alpha', migrations=(Migration(1, 'one', drops),)))

    # when
    with caplog.at_level(logging.WARNING, logger='skopax.migrations'):
        result = migrations.attempt(db, reg, 'A')

    # then
    assert result is None and reg.get('alpha').state == 'ok'
    assert 'lease' in caplog.text and 'interrupted' not in caplog.text and SENT not in caplog.text


def test_a_held_lease_at_boot_still_waits(monkeypatch):
    # given
    db = fresh()
    db['schema_version'].insert_one({'_id': '_lock', 'owner': 'other', 'until': 10 ** 9})
    clock = {'now': 1000.0}
    sleeps = []

    def sleep(seconds):
        sleeps.append(seconds)
        clock['now'] += seconds

    # when
    found = migrations.wait_and_run(db, registry_with(minimal('alpha', 'Alpha')), 'B', clock=lambda: clock['now'], sleep=sleep)

    # then
    assert found is None and len(sleeps) == migrations.LOCK_S // 5


def test_a_forced_broken_plugin_that_fails_again_shows_the_new_reason():
    # given
    db = fresh()
    error = [RuntimeError]

    def failing(_d):
        raise error[0](SENT)

    reg = registry_with(minimal('alpha', 'Alpha', migrations=(Migration(1, 'one', failing),)))

    # when
    migrations.attempt(db, reg, 'A')
    first = reg.get('alpha').reason
    error[0] = ValueError
    migrations.attempt(db, reg, 'A')
    second = reg.get('alpha').reason

    # then
    assert 'RuntimeError' in first and 'ValueError' not in first
    assert 'ValueError' in second and 'RuntimeError' not in second
    assert reg.get('alpha').state == 'broken'


def test_a_plugin_that_stops_being_runnable_during_the_pass_is_skipped():
    # given
    db = fresh()
    seen = []

    def failing(_d):
        raise RuntimeError(SENT)

    licence = minimal('aaa-licence', 'Licence', hooks=Hooks(entitlement=lambda: 'enterprise'), migrations=(Migration(1, 'one', failing),))
    locked = dataclasses.replace(enterprise_plugin.PLUGIN, id='zzz-ee', name='Zzz', checks=(Check('zzz-ee', 'zzz', 900, 0, ('zzz-ee',), run),), migrations=(Migration(1, 'ee', noting(seen, 'zzz-ee:1')),))
    reg = registry_with(licence, locked)

    # when
    applied = migrations.attempt(db, reg, 'A')

    # then
    assert reg.get('aaa-licence').state == 'broken' and reg.get('zzz-ee').state == 'locked'
    assert seen == [] and applied == []
    assert db['schema_version'].find_one({'_id': 'zzz-ee'}) is None


def test_a_plugin_freed_by_an_earlier_failure_runs_in_a_second_pass_of_the_same_attempt():
    # given
    db = fresh()
    seen = []

    def failing(_d):
        raise RuntimeError(SENT)

    first = minimal('aaa', 'Aaa', migrations=(Migration(1, 'one', failing),))
    second = minimal('bbb', 'Bbb', checks=(Check('bbb', 'bbb', 900, 0, ('bbb', 'aaa'), run),), migrations=(Migration(1, 'two', noting(seen, 'bbb:1')),))
    reg = registry_with(first, second)

    # when
    before = reg.get('bbb').state
    applied = migrations.attempt(db, reg, 'A')

    # then
    assert before == 'broken'
    assert reg.get('aaa').state == 'broken' and reg.get('bbb').state == 'ok'
    assert seen == ['bbb:1'] and applied == ['bbb:1']


def test_a_core_bookkeeping_failure_hides_the_database_error_as_its_context(monkeypatch):
    # given
    db = fresh()
    real = type(db['schema_version']).find_one

    def find_one(self, *a, **k):
        if self.name == 'schema_version' and a and a[0] == {'_id': migrations.CORE_OWNER}:
            raise RuntimeError(SENT)
        return real(self, *a, **k)

    monkeypatch.setattr(type(db['schema_version']), 'find_one', find_one)
    monkeypatch.setattr(migrations, 'CORE', (Migration(1, 'core-one', lambda d: None),))

    # when
    with pytest.raises(migrations.MigrationFailed) as e:
        migrations.attempt(db, registry_with(minimal('alpha', 'Alpha')), 'A')

    # then
    assert e.value.__cause__ is None and e.value.__suppress_context__ is True and SENT not in str(e.value)


def test_a_failing_migration_and_a_failing_bookkeeping_step_hide_their_errors_as_context():
    # given
    db = fresh()
    col = db['schema_version']

    def boom(_d):
        raise RuntimeError(SENT)

    class Wrapped:
        def __init__(self, inner):
            self.inner = inner

        def find_one(self, *a, **k):
            return self.inner.find_one(*a, **k)

        def update_one(self, *a, **k):
            if a and a[0] == {'_id': 'alpha'}:
                raise RuntimeError(SENT)
            return self.inner.update_one(*a, **k)

    col.update_one({'_id': migrations.LOCK_ID}, {'$set': {'owner': 'A', 'until': 10 ** 12}}, upsert=True)

    # when
    with pytest.raises(migrations.MigrationFailed) as run_failed:
        migrations._apply(db, col, 'alpha', (Migration(1, 'one', boom),), 'A', lambda: 1.0, [])
    with pytest.raises(migrations.MigrationFailed) as book_failed:
        migrations._apply(db, Wrapped(col), 'alpha', (Migration(1, 'one', lambda d: None),), 'A', lambda: 1.0, [])

    # then
    for caught in (run_failed, book_failed):
        assert caught.value.__cause__ is None and caught.value.__suppress_context__ is True
        assert SENT not in str(caught.value)


class Waits:
    def __init__(self, answers):
        self.answers = list(answers)
        self.args = []

    def wait(self, seconds):
        self.args.append(seconds)
        return self.answers.pop(0)

    def set(self):
        pass


def test_the_heartbeat_waits_exactly_a_third_of_the_lease_between_renewals(monkeypatch):
    # given
    monkeypatch.setattr(migrations, 'LOCK_S', 300)
    beat = migrations._Heartbeat(fresh()['schema_version'], 'A', lambda: 1.0)
    beat._stop = Waits([False, False, True])
    renewals = []
    monkeypatch.setattr(migrations, '_renew', lambda col, boot_id, clock: renewals.append(1))

    # when
    beat._beat()

    # then
    assert beat._stop.args == [100.0, 100.0, 100.0] and len(renewals) == 2


def test_the_heartbeat_stops_for_good_when_the_lease_is_lost(monkeypatch):
    # given
    beat = migrations._Heartbeat(fresh()['schema_version'], 'A', lambda: 1.0)
    beat._stop = Waits([False, False, False])
    calls = []

    def renew(col, boot_id, clock):
        calls.append(1)
        raise migrations.LeaseLost(SENT)

    monkeypatch.setattr(migrations, '_renew', renew)

    # when
    beat._beat()

    # then
    assert len(calls) == 1 and len(beat._stop.args) == 1
