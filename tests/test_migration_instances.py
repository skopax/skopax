import dataclasses

import mongomock
import pytest
from fastapi.testclient import TestClient

from skopax import contract, migrations
from skopax.app import create_app
from skopax.contract import Capabilities, Field, Migration, SourceData
from skopax.instances import Instances, InvalidInstance, SourceOwned, Writer
from skopax.messages import msg
from skopax.sealer import SEALED, Sealer, master_key
from tests.conftest import settings
from tests.plugin_kit import add_instance, registry_with
from tests.plugins import demo_plugin
from tests.plugins.base import minimal

DEMO = demo_plugin.PLUGIN
KEY = 'writer-key'
KEYS = minimal('keys', 'Keys', capabilities=Capabilities(hosts=('@url',), secrets=('token', 'backup')),
               fields=(Field('url', 'URL', 'url', required=True), Field('token', 'Token', 'secret'), Field('backup', 'Backup token', 'secret')))


def fresh():
    return mongomock.MongoClient(tz_aware=True)['skopax']


def test_a_plugin_migration_adopts_a_sealed_admin_secret_into_an_instance_field_unopened(store):
    # given
    sealed = Sealer(master_key(store.db, settings())).seal('adopted-token-0001')
    store.db['admin_secrets'].insert_one({'_id': 'demo-token', 'value': sealed, 'updatedBy': 'admin@example.com'})
    add_instance(store, 'demo', 'main', url='https://demo.example.com')
    handed = []

    def adopt(db, writer):
        handed.append(writer)
        writer.adopt_sealed('demo', 'main', 'token', db['admin_secrets'].find_one({'_id': 'demo-token'})['value'], 'migration')

    plugin = dataclasses.replace(DEMO, migrations=(Migration(1, 'adopt-token', adopt, needs_instances=True),))

    # when
    with TestClient(create_app(settings(), store, registry=registry_with(plugin))) as client:
        shown = client.app.state.instances.get('demo', 'main')
        state = client.app.state.runtime.registry.get('demo').state

    # then
    doc = store.db['instances'].find_one({'_id': 'demo/main'})
    assert doc['secrets']['token'] == sealed and doc['updatedBy'] == 'migration'
    assert shown.secrets['token'] == 'adopted-token-0001' and state == 'ok'
    assert store.db['schema_version'].find_one({'_id': 'demo'})['version'] == 1
    assert [n for n in dir(handed[0]) if not n.startswith('_')] == ['adopt_sealed', 'get', 'put']


def test_put_seals_plain_secret_fields_and_refuses_an_unknown_field():
    # given
    db = fresh()

    def seed(_db, writer):
        writer.put('demo', 'main', {'url': 'https://demo.example.com', 'mode': 'slow'}, {'token': 'plain-token-0002'}, 'migration', primary=True)
        writer.put('demo', 'main', {'url': 'https://demo.example.com', 'mode': 'slow'}, {'token': 'plain-token-0002'}, 'migration', primary=True)

    def stray(_db, writer):
        writer.put('demo', 'side', {'url': 'https://side.example.com', 'colour': 'red'}, {}, 'migration')

    plugin = dataclasses.replace(DEMO, migrations=(Migration(1, 'seed', seed, needs_instances=True), Migration(2, 'stray', stray, needs_instances=True)))
    reg = registry_with(plugin)
    instances = Instances(db, reg, lambda: Sealer(KEY))
    writer = Writer(instances)

    # when
    applied = migrations.run(db, reg, 'boot-1', writer=writer)
    with pytest.raises(InvalidInstance) as refused:
        writer.put('demo', 'side', {'url': 'https://side.example.com'}, {'colour': 'red'}, 'migration')
    with pytest.raises(InvalidInstance) as misfiled:
        writer.put('demo', 'side', {'url': 'https://side.example.com'}, {'mode': 'fast'}, 'migration')

    # then
    doc = db['instances'].find_one({'_id': 'demo/main'})
    assert applied == ['demo:1']
    assert doc['secrets']['token'].startswith(SEALED) and Sealer(KEY).open(doc['secrets']['token']) == 'plain-token-0002'
    assert doc['values'] == {'url': 'https://demo.example.com', 'mode': 'slow'} and doc['primary'] is True and doc['createdBy'] == 'migration'
    assert db['instances'].find_one({'_id': 'demo/side'}) is None
    assert reg.get('demo').state == 'broken' and 'InvalidInstance' in reg.get('demo').reason
    assert db['schema_version'].find_one({'_id': 'demo'})['version'] == 1
    assert refused.value.field == 'colour' and misfiled.value.field == 'mode'


def test_adopt_into_a_source_owned_field_is_refused_and_into_a_free_field_of_a_source_instance_works():
    # given
    db = fresh()
    sealed = Sealer(KEY).seal('backup-token-0003')
    listed = SourceData('vault source', {}, ({'plugin': 'keys', 'name': 'main', 'url': 'https://keys.example.com', 'token': 'source-token-0004'},))
    reg = registry_with(KEYS)
    instances = Instances(db, reg, lambda: Sealer(KEY), sources=lambda: [listed])
    writer = Writer(instances)
    cold = instances.age() is None

    # when
    with pytest.raises(SourceOwned) as owned:
        writer.adopt_sealed('keys', 'main', 'token', sealed, 'migration')
    writer.adopt_sealed('keys', 'main', 'backup', sealed, 'migration')

    # then
    doc = db['instances'].find_one({'_id': 'keys/main'})
    found = instances.get('keys', 'main')
    assert cold and owned.value.field == 'token' and owned.value.source == 'vault source'
    assert doc['secrets'] == {'backup': sealed}
    assert found.source == 'vault source' and found.secrets == {'token': 'source-token-0004', 'backup': 'backup-token-0003'}


def test_a_writer_less_run_stops_a_plugin_before_its_instances_migration_and_a_run_with_the_writer_applies_it():
    # given
    db = fresh()
    seen = []

    def plain(_db):
        seen.append('plain')

    def seed(_db, writer):
        seen.append('seed')
        writer.put('demo', 'main', {'url': 'https://demo.example.com'}, {}, 'migration')

    plugin = dataclasses.replace(DEMO, migrations=(Migration(1, 'plain', plain), Migration(2, 'seed', seed, needs_instances=True), Migration(3, 'after', plain)))
    reg = registry_with(plugin)

    # when
    first = migrations.run(db, reg, 'boot-1')
    second = migrations.run(db, reg, 'boot-2', writer=Writer(Instances(db, reg, lambda: Sealer(KEY))))

    # then
    assert first == ['demo:1'] and second == ['demo:2', 'demo:3']
    assert seen == ['plain', 'seed', 'plain'] and reg.get('demo').state == 'ok'
    assert db['instances'].find_one({'_id': 'demo/main'})['values'] == {'url': 'https://demo.example.com'}


def test_put_into_a_source_owned_field_is_refused_and_a_free_field_is_written():
    # given
    db = fresh()
    listed = SourceData('vault source', {}, ({'plugin': 'keys', 'name': 'main', 'url': 'https://keys.example.com', 'token': 'source-token-0004'},))
    reg = registry_with(KEYS)
    instances = Instances(db, reg, lambda: Sealer(KEY), sources=lambda: [listed])
    writer = Writer(instances)
    instances.age()

    # when
    with pytest.raises(contract.SourceOwned) as owned:
        writer.put('keys', 'main', {}, {'token': 'plain-token-0005'}, 'migration')
    writer.put('keys', 'main', {}, {'backup': 'plain-token-0006'}, 'migration')

    # then
    found = instances.get('keys', 'main')
    assert owned.value.field == 'token' and owned.value.source == 'vault source'
    assert found.secrets == {'token': 'source-token-0004', 'backup': 'plain-token-0006'}


def test_put_refuses_to_make_a_switched_off_instance_the_primary_and_writes_nothing():
    # given
    db = fresh()
    reg = registry_with(DEMO)
    instances = Instances(db, reg, lambda: Sealer(KEY))
    writer = Writer(instances)
    writer.put('demo', 'main', {'url': 'https://demo.example.com'}, {}, 'migration', primary=True)
    writer.put('demo', 'side', {'url': 'https://side.example.com'}, {}, 'migration')
    instances.set_enabled('demo', 'side', False, 'alice@example.com')

    # when
    with pytest.raises(contract.InvalidInstance) as refused:
        writer.put('demo', 'side', {'url': 'https://other.example.com'}, {}, 'migration', primary=True)

    # then
    assert refused.value.field == 'primary' and 'Switch side on' in refused.value.text
    assert db['instances'].find_one({'_id': 'demo/side'})['values'] == {'url': 'https://side.example.com'}
    assert instances.primary('demo').name == 'main'


def test_the_writer_errors_are_part_of_the_plugin_contract():
    # then
    assert contract.InvalidInstance is InvalidInstance and contract.SourceOwned is SourceOwned
    assert {'InvalidInstance', 'SourceOwned'} <= set(contract.__all__)


SEALS = minimal('seals', 'Seals', capabilities=Capabilities(hosts=('@url',), secrets=('token', 'backup')),
                fields=(Field('url', 'URL', 'url', required=True), Field('token', 'Token', 'secret', required=True), Field('backup', 'Backup token', 'secret'),
                        Field('mode', 'Mode', 'choice', default='fast', choices=('fast', 'slow')), Field('public-key', 'Public key', 'text')))


def seals_writer(db):
    reg = registry_with(SEALS)
    instances = Instances(db, reg, lambda: Sealer(KEY))
    return instances, Writer(instances)


def test_put_creates_an_instance_whose_required_secret_is_only_given_sealed_and_keeps_it_byte_for_byte():
    # given
    db = fresh()
    instances, writer = seals_writer(db)
    sealed = Sealer(KEY).seal('old-admin-token-0011')
    backup = Sealer(KEY).seal('old-backup-token-0012')

    # when
    writer.put('seals', 'main', {'url': 'https://seals.example.com'}, {}, 'migration', sealed={'token': sealed})
    writer.put('seals', 'side', {'url': 'https://side.example.com'}, {'token': 'plain-token-0013'}, 'migration', sealed={'backup': backup})

    # then
    main, side = (db['instances'].find_one({'_id': f'seals/{n}'}) for n in ('main', 'side'))
    assert main['secrets'] == {'token': sealed} and main['values'] == {'url': 'https://seals.example.com'}
    assert side['secrets']['backup'] == backup and Sealer(KEY).open(side['secrets']['token']) == 'plain-token-0013'
    assert instances.get('seals', 'main').secrets == {'token': 'old-admin-token-0011'} and instances.get('seals', 'main').problem is None


def test_put_refuses_a_sealed_value_that_does_not_open_or_a_field_given_both_ways_and_writes_nothing():
    # given
    db = fresh()
    _, writer = seals_writer(db)
    foreign = Sealer('another-key').seal('old-admin-token-0011')

    # when
    with pytest.raises(contract.InvalidInstance) as unreadable:
        writer.put('seals', 'main', {'url': 'https://seals.example.com'}, {}, 'migration', sealed={'token': foreign})
    with pytest.raises(contract.InvalidInstance) as twice:
        writer.put('seals', 'main', {'url': 'https://seals.example.com'}, {'token': 'plain-token-0013'}, 'migration', sealed={'token': Sealer(KEY).seal('x-0014')})
    with pytest.raises(contract.InvalidInstance) as plain:
        writer.put('seals', 'main', {'url': 'https://seals.example.com'}, {}, 'migration', sealed={'token': 'not-sealed'})

    # then
    assert db['instances'].find_one({'_id': 'seals/main'}) is None
    assert unreadable.value.field == 'token' and unreadable.value.text == msg('instances.secret_unreadable', field='Token')
    assert twice.value.field == 'token' and twice.value.text == msg('instances.invalid.twice', field='Token')
    assert plain.value.field == 'token'
    assert 'old-admin-token-0011' not in unreadable.value.text


def test_put_can_store_an_instance_switched_off_and_refuses_off_with_primary():
    # given
    db = fresh()
    instances, writer = seals_writer(db)

    # when
    writer.put('seals', 'main', {'url': 'https://seals.example.com'}, {'token': 'plain-token-0013'}, 'migration')
    writer.put('seals', 'side', {'url': 'https://side.example.com'}, {'token': 'plain-token-0015'}, 'migration', enabled=False)
    with pytest.raises(contract.InvalidInstance) as refused:
        writer.put('seals', 'third', {'url': 'https://third.example.com'}, {'token': 'plain-token-0016'}, 'migration', primary=True, enabled=False)

    # then
    assert instances.get('seals', 'side').enabled is False and instances.get('seals', 'main').enabled is True
    assert refused.value.field == 'primary' and db['instances'].find_one({'_id': 'seals/third'}) is None


def test_put_switches_named_checks_off_and_refuses_an_unknown_check():
    # given
    db = fresh()
    instances, writer = seals_writer(db)

    # when
    writer.put('seals', 'main', {'url': 'https://seals.example.com'}, {'token': 'plain-token-0013'}, 'migration', off_checks=('seals',))
    with pytest.raises(contract.InvalidInstance) as refused:
        writer.put('seals', 'side', {'url': 'https://side.example.com'}, {'token': 'plain-token-0015'}, 'migration', off_checks=('nope',))

    # then
    assert instances.get('seals', 'main').off_checks == frozenset({'seals'})
    assert refused.value.field == 'offChecks' and refused.value.text == msg('instances.invalid.checks', check='nope')
    assert db['instances'].find_one({'_id': 'seals/side'}) is None


def test_the_writer_reads_an_instances_plain_values_and_never_its_secrets():
    # given
    db = fresh()
    _, writer = seals_writer(db)
    writer.put('seals', 'main', {'url': 'https://seals.example.com', 'mode': 'slow'}, {'token': 'plain-token-0013'}, 'migration', sealed={'backup': Sealer(KEY).seal('old-backup-0017')})

    # when
    got = writer.get('seals', 'main')
    missing = writer.get('seals', 'nope')

    # then
    assert dict(got) == {'url': 'https://seals.example.com', 'mode': 'slow'}
    assert 'plain-token-0013' not in repr(got) and 'old-backup-0017' not in repr(got) and 'enc:' not in repr(got)
    assert missing is None
    assert callable(getattr(contract.InstanceWriter, 'get', None))


def test_put_opens_a_sealed_text_field_and_stores_it_plain_while_a_sealed_secret_stays_byte_for_byte():
    # given
    db = fresh()
    instances, writer = seals_writer(db)
    token = Sealer(KEY).seal('old-admin-token-0018')
    public = Sealer(KEY).seal('ssh-ed25519 AAAA-public-0019')

    # when
    writer.put('seals', 'main', {'url': 'https://seals.example.com'}, {}, 'migration', sealed={'token': token, 'public-key': public})
    with pytest.raises(contract.InvalidInstance) as twice:
        writer.put('seals', 'side', {'url': 'https://side.example.com', 'public-key': 'x'}, {}, 'migration', sealed={'token': token, 'public-key': public})

    # then
    doc = db['instances'].find_one({'_id': 'seals/main'})
    assert doc['secrets'] == {'token': token}
    assert doc['values'] == {'url': 'https://seals.example.com', 'public-key': 'ssh-ed25519 AAAA-public-0019'}
    assert twice.value.field == 'public-key' and db['instances'].find_one({'_id': 'seals/side'}) is None


def test_a_switched_off_first_instance_still_becomes_the_primary():
    # given
    db = fresh()
    instances, writer = seals_writer(db)

    # when
    writer.put('seals', 'main', {'url': 'https://seals.example.com'}, {'token': 'plain-token-0020'}, 'migration', enabled=False)
    writer.put('seals', 'side', {'url': 'https://side.example.com'}, {'token': 'plain-token-0021'}, 'migration', enabled=False)

    # then
    main, side = instances.get('seals', 'main'), instances.get('seals', 'side')
    assert (main.enabled, main.primary) == (False, True)
    assert (side.enabled, side.primary) == (False, False)


def test_put_with_a_sealed_secret_changes_an_existing_instance_byte_for_byte():
    # given
    db = fresh()
    _, writer = seals_writer(db)
    writer.put('seals', 'main', {'url': 'https://seals.example.com'}, {'token': 'plain-token-0024'}, 'migration')
    sealed = Sealer(KEY).seal('old-admin-token-0025')

    # when
    writer.put('seals', 'main', {}, {}, 'migration', sealed={'token': sealed})

    # then
    assert db['instances'].find_one({'_id': 'seals/main'})['secrets'] == {'token': sealed}


def test_put_with_a_sealed_value_into_a_source_owned_field_is_refused_and_writes_nothing():
    # given
    db = fresh()
    listed = SourceData('vault source', {}, ({'plugin': 'keys', 'name': 'main', 'url': 'https://keys.example.com', 'token': 'source-token-0004'},))
    reg = registry_with(KEYS)
    instances = Instances(db, reg, lambda: Sealer(KEY), sources=lambda: [listed])
    writer = Writer(instances)
    instances.age()
    before = db['instances'].find_one({'_id': 'keys/main'})

    # when
    with pytest.raises(contract.SourceOwned) as owned:
        writer.put('keys', 'main', {}, {}, 'migration', sealed={'token': Sealer(KEY).seal('sealed-token-0026')})

    # then
    assert owned.value.field == 'token'
    assert db['instances'].find_one({'_id': 'keys/main'}) == before
    assert instances.get('keys', 'main').secrets == {'token': 'source-token-0004'}
