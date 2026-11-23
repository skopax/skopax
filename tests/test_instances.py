import base64
import dataclasses
import datetime as dt
import hashlib
import json
import logging
import threading
from types import SimpleNamespace

import mongomock
import pytest
from cryptography import x509
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
from fastapi.testclient import TestClient
from pymongo.errors import AutoReconnect

from skopax import instances as inst
from skopax.app import create_app
from skopax.contract import AgentRead, Capabilities, Check, Field, Hooks, InstanceInfo, SourceData
from skopax.instances import Instances, InstanceExists, InstanceMissing, InvalidInstance, PluginState, SourceOwned, merged_values, validate
from skopax.messages import msg
from skopax.runtime import Runtime
from skopax.sealer import SEALED, Sealer, master_key, stored_key
from skopax.sessions import derive_secret, session_secret
from tests.conftest import settings
from tests.plugin_kit import add_instance, no_loop, registry_with
from tests.plugins import demo_plugin, enterprise_plugin, two_checks_plugin
from tests.plugins.base import minimal

DEMO = demo_plugin.PLUGIN
LEAK = 'zq9-plaintext-token'
GOOD = {'url': 'https://demo.example.com', 'token': 'pw'}
KINDS = minimal('kinds', 'Kinds', capabilities=Capabilities(secrets=('key',)), fields=(
    Field('url', 'URL', 'url'), Field('rx', 'Pattern', 'regex'), Field('ca', 'CA', 'pem'), Field('mode', 'Mode', 'choice', choices=('fast', 'slow')),
    Field('n', 'Count', 'int', low=1, high=5), Field('flag', 'Flag', 'bool'), Field('note', 'Note', 'text'), Field('key', 'Key', 'secret')))


def pem() -> str:
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'example.com')])
    now = dt.datetime.now(dt.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now).not_valid_after(now + dt.timedelta(days=2)).sign(key, hashes.SHA256()))
    return cert.public_bytes(serialization.Encoding.PEM).decode()


class Box:
    def __init__(self, db, *plugins, key='k', generated=False, sources=None, **kw):
        self.db = SimpleNamespace(db=db)
        self.key = key
        self.generated = generated
        self.sources: list[SourceData] = sources if sources is not None else []
        self.registry = registry_with(*plugins)
        self.state = PluginState(db)
        self.instances = Instances(db, self.registry, lambda: Sealer(self.key, generated=self.generated), sources=lambda: self.sources, **kw)
        self.runtime = Runtime(self.registry, self.state, self.instances)


@pytest.fixture
def db():
    return mongomock.MongoClient(tz_aware=True)['skopax']


def test_sealer_matches_the_stored_secret_format():
    # given
    raw = Fernet(base64.urlsafe_b64encode(hashlib.sha256(b'k').digest()))
    stored = 'enc:v1:' + raw.encrypt(b'pw').decode()

    # when
    sealed = Sealer('k').seal('pw')

    # then
    assert SEALED == 'enc:v1:'
    assert Sealer('k').open(stored) == 'pw'
    assert sealed.startswith(SEALED) and raw.decrypt(sealed[len(SEALED):].encode()) == b'pw'
    assert Sealer('other').open(stored) is None
    assert Sealer('k').open('plain') is None and Sealer('k').open(SEALED + 'garbage') is None
    assert Sealer('').enabled is False and Sealer('k').enabled is True
    assert Sealer('').open(stored) is None


def test_master_key_is_shared_with_sessions(db):
    # given
    keyless = settings()

    # when
    key = master_key(db, keyless)
    secret = session_secret(db, keyless)

    # then
    stored = db['meta'].find_one({'_id': 'secret-key'})['key']
    assert key == stored == stored_key(db)
    assert secret == hashlib.sha256(b'skopax-session:' + stored.encode()).digest() == derive_secret(stored)
    assert master_key(db, dataclasses.replace(keyless, secret_key='from-env')) == 'from-env'
    assert session_secret(db, dataclasses.replace(keyless, secret_key='from-env')) == derive_secret('from-env')
    assert db['meta'].count_documents({'_id': 'secret-key'}) == 1


def test_stored_key_does_not_create_one(db):
    # then
    assert stored_key(db) is None
    assert db['meta'].count_documents({}) == 0


def test_create_validates_fields_by_kind(db):
    # given
    box = Box(db, KINDS)
    bad = [('url', 'ftp://x.example.com'), ('url', 'https://u:p@x.example.com'), ('url', 'not a url'), ('rx', '('), ('rx', 'a' * 201), ('ca', 'not a pem'), ('mode', 'sideways'), ('n', '9'), ('n', 'many'), ('flag', 'maybe'),
           ('note', 'x' * 1001), ('key', 'x' * 4001), ('nope', 'x')]

    # when
    failures = []
    for field, value in bad:
        with pytest.raises(InvalidInstance) as caught:
            box.instances.create('kinds', 'main', {field: value}, True, 'alice')
        failures.append((field, caught.value.field))

    # then
    assert all(sent == found for sent, found in failures) and len(failures) == len(bad)
    assert box.instances.all() == []
    made = box.instances.create('kinds', 'main', {'url': 'https://x.example.com:8443/p', 'rx': '^a+$', 'ca': pem(), 'mode': 'slow', 'n': '5', 'flag': 'true', 'note': 'x' * 1000}, True, 'alice')
    assert made.values['mode'] == 'slow' and made.values['n'] == '5'


def test_required_fields_and_scalar_coercion(db):
    # given
    box = Box(db, DEMO)

    # when
    with pytest.raises(InvalidInstance) as missing:
        box.instances.create('demo', 'main', {'token': 'pw'}, True, 'alice')
    with pytest.raises(InvalidInstance) as nested:
        box.instances.create('demo', 'main', {'url': ['https://demo.example.com']}, True, 'alice')
    made = validate(DEMO, {'url': 'https://demo.example.com', 'mode': ''})

    # then
    assert missing.value.field == 'url' and nested.value.field == 'url'
    assert made == {'url': 'https://demo.example.com'}
    assert validate(DEMO, {'mode': 'slow'}, partial=True) == {'mode': 'slow'}
    assert validate(KINDS, {'flag': True, 'n': 3}) == {'flag': 'true', 'n': '3'}


def test_instance_names_are_unique_and_not_reserved(db):
    # given
    box = Box(db, DEMO)

    # when
    refused = {}
    for name in ('East', 'manual', 'demo', 'pulse', 'demo-east', 'pulse-x', 'primary', 'all-x', '', '-a', 'a' * 21, 'a_b'):
        with pytest.raises(InvalidInstance) as caught:
            box.instances.create('demo', name, GOOD, True, 'alice')
        refused[name] = caught.value.field
    box.instances.create('demo', 'east', GOOD, True, 'alice')
    box.instances.create('demo', 'eastern', GOOD, True, 'alice')
    with pytest.raises(InstanceExists):
        box.instances.create('demo', 'east', GOOD, True, 'alice')

    # then
    assert set(refused.values()) == {'name'}
    assert [i.name for i in box.instances.all('demo')] == ['east', 'eastern']


def test_the_same_name_may_exist_under_two_plugins(db):
    # given
    other = minimal('other', 'Other', fields=(Field('url', 'URL', 'url'),))
    box = Box(db, DEMO, other)

    # when
    box.instances.create('demo', 'east', GOOD, True, 'alice')
    box.instances.create('other', 'east', {'url': 'https://o.example.com'}, True, 'alice')

    # then
    assert [(i.plugin, i.name, i.primary) for i in box.instances.all()] == [('demo', 'east', True), ('other', 'east', True)]


def test_creating_for_an_unknown_or_locked_plugin_is_refused(db):
    # given
    locked = dataclasses.replace(enterprise_plugin.PLUGIN, fields=(Field('url', 'URL', 'url'),), capabilities=Capabilities(hosts=('@url',)))
    box = Box(db, DEMO, locked)

    # when
    with pytest.raises(InvalidInstance) as unknown:
        box.instances.create('missing', 'main', {}, True, 'alice')
    with pytest.raises(InvalidInstance) as refused:
        box.instances.create('enterprise-demo', 'main', {'url': 'https://x.example.com'}, True, 'alice')

    # then
    assert unknown.value.field == 'plugin' and refused.value.field == 'plugin'
    assert db['instances'].count_documents({}) == 0


def test_first_instance_is_primary_and_primary_is_exactly_one(db):
    # given
    box = Box(db, DEMO)
    box.instances.create('demo', 'main', GOOD, True, 'alice')
    box.instances.create('demo', 'east', GOOD, True, 'alice')

    # when
    first = box.instances.make_primary('demo', 'east', 'alice')
    again = box.instances.make_primary('demo', 'east', 'alice')

    # then
    assert first == ('main', 'east') and again == ()
    assert [(i.name, i.primary) for i in box.instances.all('demo')] == [('east', True), ('main', False)]
    assert box.instances.primary('demo').name == 'east'
    assert box.instances.get('demo', 'east').info().segment is None
    assert box.instances.get('demo', 'main').info().segment == 'main'


def test_deleting_the_primary_promotes_the_next_by_name_and_it_sticks(db):
    # given
    box = Box(db, DEMO)
    for name in ('main', 'beta', 'delta'):
        box.instances.create('demo', name, GOOD, True, 'alice')

    # when
    box.instances.delete('demo', 'main', 'alice')
    box.instances.create('demo', 'alpha', GOOD, True, 'alice')

    # then
    assert box.instances.primary('demo').name == 'beta'
    assert [i.name for i in box.instances.all('demo') if i.primary] == ['beta']
    assert db['instances'].find_one({'_id': 'demo/beta'})['primary'] is True


def test_secrets_are_sealed_never_returned_and_kept_on_blank_update(db):
    # given
    box = Box(db, DEMO)

    # when
    made = box.instances.create('demo', 'main', {**GOOD, 'token': LEAK}, True, 'alice')
    view = made.view()
    stored = db['instances'].find_one({'_id': 'demo/main'})

    # then
    assert stored['secrets']['token'].startswith(SEALED) and LEAK not in json.dumps(stored)
    assert view['secrets'] == {'token': {'set': True, 'from': None}}
    assert LEAK not in json.dumps(view) and LEAK not in repr(made) and LEAK not in str(box.instances.all())
    assert made.info().config['token'] == LEAK and made.info().config['url'] == 'https://demo.example.com' and made.info().config['mode'] == 'fast'
    assert view['name'] == 'main' and view['primary'] is True and view['enabled'] is True and view['problem'] is None and view['source'] == 'admin' and view['owned'] == []
    kept = box.instances.update('demo', 'main', {'mode': 'slow', 'token': None}, 'bob')
    assert kept.info().config['token'] == LEAK and kept.values['mode'] == 'slow'
    assert db['instances'].find_one({'_id': 'demo/main'})['updatedBy'] == 'bob'
    cleared = box.instances.update('demo', 'main', {'token': ''}, 'bob')
    assert cleared.view()['secrets'] == {'token': {'set': False, 'from': None}} and 'token' not in cleared.info().config
    replaced = box.instances.update('demo', 'main', {'token': 'new'}, 'bob')
    assert replaced.info().config['token'] == 'new'


def test_merged_values_keeps_on_none_and_removes_on_blank():
    # then
    assert merged_values({'a': '1', 'b': '2'}, {'a': None, 'b': '', 'c': '3', 'd': None}) == {'a': '1', 'c': '3'}
    assert merged_values({}, {}) == {}


def test_a_required_field_cannot_be_cleared(db):
    # given
    box = Box(db, DEMO)
    box.instances.create('demo', 'main', GOOD, True, 'alice')

    # when
    with pytest.raises(InvalidInstance) as caught:
        box.instances.update('demo', 'main', {'url': ''}, 'alice')

    # then
    assert caught.value.field == 'url' and box.instances.get('demo', 'main').values['url'] == 'https://demo.example.com'


def test_generated_key_seals_and_warns_once(db, caplog):
    # given
    box = Box(db, DEMO, key='generated-key', generated=True)

    # when
    with caplog.at_level(logging.WARNING, logger='skopax.instances'):
        box.instances.create('demo', 'main', GOOD, True, 'alice')
        box.instances.create('demo', 'east', GOOD, True, 'alice')
        box.instances.refresh()

    # then
    warned = [r for r in caplog.records if 'generated' in r.getMessage()]
    assert len(warned) == 1
    assert Sealer('generated-key').open(db['instances'].find_one({'_id': 'demo/main'})['secrets']['token']) == 'pw'
    assert 'pw' not in caplog.text and 'generated-key' not in caplog.text


def test_secrets_survive_setting_the_secret_key_later_or_park_the_instance(db, caplog):
    # given
    generated = master_key(db, settings())
    box = Box(db, DEMO, key=generated, generated=True)
    box.instances.create('demo', 'main', GOOD, True, 'alice')
    box.key, box.generated = 'k', False

    # when
    with caplog.at_level(logging.INFO, logger='skopax.instances'):
        box.instances.refresh()
    resealed = db['instances'].find_one({'_id': 'demo/main'})['secrets']['token']
    after = box.instances.get('demo', 'main')

    # then
    assert after.problem is None and after.info().config['token'] == 'pw'
    assert Sealer('k').open(resealed) == 'pw'
    assert len([r for r in caplog.records if 'sealed again' in r.getMessage()]) == 1 and 'pw' not in caplog.text
    assert [i.name for i in box.runtime.instances_of('demo')] == ['main']

    # given
    add_instance(box.db, 'demo', 'east', sealer_key=generated, url='https://e.example.com', token='pw2')
    db['meta'].delete_one({'_id': 'secret-key'})

    # when
    box.instances.refresh()
    parked = box.instances.get('demo', 'east')

    # then
    assert parked.problem and 'Token' in parked.problem and 'again' in parked.problem
    assert 'token' not in parked.secrets and parked.view()['secrets'] == {'token': {'set': False, 'from': None}}
    assert [i.name for i in box.runtime.instances_of('demo')] == ['main']


def test_an_unreadable_secret_is_fixed_by_entering_it_again(db):
    # given
    box = Box(db, DEMO)
    add_instance(box.db, 'demo', 'main', sealer_key='lost', url='https://e.example.com', token='pw')
    box.instances.refresh()

    # when
    before = box.instances.get('demo', 'main')
    fixed = box.instances.update('demo', 'main', {'token': 'pw'}, 'alice')

    # then
    assert before.problem and fixed.problem is None and fixed.info().config['token'] == 'pw'


def test_an_address_saved_over_a_source_instance_never_carries_its_secret_elsewhere(db):
    # given
    source = SourceData('config service', {}, ({'plugin': 'demo', 'name': 'main', 'url': 'https://src.example.com', 'token': 'src-secret'},
                                              {'plugin': 'demo', 'name': 'east', 'url': 'https://east.example.com'}))
    box = Box(db, DEMO, sources=[source])
    box.instances.refresh()
    box.instances.update('demo', 'main', {'mode': 'slow'}, 'alice')
    box.instances.update('demo', 'east', {'mode': 'slow'}, 'alice')
    db['instances'].update_many({}, {'$set': {'values.url': 'https://elsewhere.example.net'}})

    # when
    box.instances.refresh()
    main = box.instances.get('demo', 'main')
    east = box.instances.get('demo', 'east')

    # then
    assert main.info().config['url'] == 'https://src.example.com' and main.info().config['token'] == 'src-secret' and main.values['mode'] == 'slow'
    assert east.info().config['url'] == 'https://elsewhere.example.net' and east.owned == frozenset()


def test_source_instance_is_overridable_but_its_secret_and_existence_are_not(db):
    # given
    source = SourceData('config service', {}, ({'plugin': 'demo', 'name': 'main', 'url': 'https://src.example.com', 'token': 'src-secret', 'mode': 'slow'},
                                              {'plugin': 'demo', 'name': 'east', 'url': 'https://east.example.com', 'primary': 'true'}))
    box = Box(db, DEMO, sources=[source])
    box.instances.refresh()

    # when
    main = box.instances.get('demo', 'main')
    overridden = box.instances.update('demo', 'main', {'mode': 'fast'}, 'alice')
    with pytest.raises(SourceOwned) as address:
        box.instances.update('demo', 'main', {'url': 'https://override.example.com'}, 'alice')
    off = box.instances.set_enabled('demo', 'main', False, 'alice')
    with pytest.raises(SourceOwned) as secret:
        box.instances.update('demo', 'main', {'token': 'mine'}, 'alice')
    with pytest.raises(SourceOwned) as blank:
        box.instances.update('demo', 'main', {'token': ''}, 'alice')
    with pytest.raises(SourceOwned) as gone:
        box.instances.delete('demo', 'main', 'alice')
    kept = box.instances.update('demo', 'main', {'token': None, 'mode': ''}, 'alice')

    # then
    assert (main.source, main.primary, main.owned) == ('config service', False, frozenset({'token', 'url'}))
    assert main.view()['secrets'] == {'token': {'set': True, 'from': 'config service'}} and main.view()['owned'] == ['token', 'url']
    assert 'src-secret' not in json.dumps(main.view()) and 'src-secret' not in repr(main) and 'src-secret' not in str(box.instances.all())
    assert overridden.values['mode'] == 'fast' and overridden.info().config['token'] == 'src-secret'
    assert (address.value.field, str(address.value)) == ('url', 'URL comes from config service and cannot be changed here')
    assert off.enabled is False
    assert (secret.value.field, secret.value.source) == ('token', 'config service') and blank.value.field == 'token' and gone.value.source == 'config service'
    assert kept.values['url'] == 'https://src.example.com' and kept.values['mode'] == 'slow'
    assert box.instances.primary('demo').name == 'east'
    assert db['instances'].find_one({'_id': 'demo/main'}).get('secrets', {}) == {}


def test_a_source_instance_that_names_no_known_plugin_or_a_bad_name_is_skipped_and_a_bad_field_is_parked(db):
    # given
    source = SourceData('file', {}, ({'plugin': 'missing', 'name': 'main'}, {'plugin': 'demo', 'name': 'Bad Name', 'url': 'https://x.example.com'}, {'plugin': 'demo', 'name': 'ok', 'url': 'ftp://x.example.com'},
                                    {'plugin': 'demo', 'name': 'typo', 'url': 'https://x.example.com', 'tokne': 'x'}, {'plugin': 'demo', 'name': 'fine', 'url': 'https://x.example.com'}))
    box = Box(db, DEMO, sources=[source])

    # when
    box.instances.refresh()

    # then
    assert sorted(i.name for i in box.instances.all()) == ['fine', 'ok', 'typo']
    assert box.instances.get('demo', 'ok').problem and 'URL' in box.instances.get('demo', 'ok').problem
    assert box.instances.get('demo', 'typo').problem
    assert box.instances.get('demo', 'fine').problem is None and box.instances.primary('demo').name == 'ok'
    assert [i.name for i in box.runtime.instances_of('demo')] == ['fine']


def test_admin_overrides_enabled_and_primary_of_a_source_instance(db):
    # given
    source = SourceData('file', {}, ({'plugin': 'demo', 'name': 'main', 'url': 'https://a.example.com'}, {'plugin': 'demo', 'name': 'east', 'url': 'https://b.example.com'}))
    box = Box(db, DEMO, sources=[source])
    box.instances.refresh()

    # when
    changed = box.instances.make_primary('demo', 'east', 'alice')
    box.instances.refresh()

    # then
    assert changed == ('main', 'east')
    assert [(i.name, i.primary) for i in box.instances.all('demo')] == [('east', True), ('main', False)]


def test_two_primaries_resolve_to_the_first_in_source_order_with_one_warning(db, caplog):
    # given
    source = SourceData('file', {}, ({'plugin': 'demo', 'name': 'zeta', 'url': 'https://a.example.com', 'primary': 'true'}, {'plugin': 'demo', 'name': 'alpha', 'url': 'https://b.example.com', 'primary': 'true'}))
    box = Box(db, DEMO, sources=[source])

    # when
    with caplog.at_level(logging.WARNING, logger='skopax.instances'):
        box.instances.refresh()
        box.instances.refresh()

    # then
    assert box.instances.primary('demo').name == 'zeta' and [i.name for i in box.instances.all('demo') if i.primary] == ['zeta']
    assert len([r for r in caplog.records if 'primary' in r.getMessage()]) == 1


def test_a_failing_source_keeps_the_last_good_data(db, caplog):
    # given
    box = Box(db, DEMO, sources=[SourceData('file', {}, ({'plugin': 'demo', 'name': 'main', 'url': 'https://a.example.com'},))])
    box.instances.refresh()
    good = box.instances.all()

    def down():
        raise OSError('config service down')

    box.instances._sources = down

    # when
    with caplog.at_level(logging.WARNING, logger='skopax.instances'):
        box.instances.refresh()
        box.instances.refresh()

    # then
    assert box.instances.all() == good
    assert len([r for r in caplog.records if 'OSError' in r.getMessage()]) == 1
    assert not [r for r in caplog.records if 'config service down' in r.getMessage()], 'the exception text can carry a secret'


def test_all_is_a_cache_that_refresh_replaces(db):
    # given
    ticks = [0.0]
    box = Box(db, DEMO, clock=lambda: ticks[0])
    add_instance(box.db, 'demo', 'main', sealer_key='k', url='https://a.example.com', token='pw')

    # when
    before = box.instances.all()
    box.instances.refresh()
    ticks[0] = 7.0

    # then
    assert before == [] and [i.name for i in box.instances.all()] == ['main']
    assert box.instances.age() == 7.0 and box.instances.every == inst.REFRESH_S == 15


def test_reread_skips_a_cache_younger_than_two_seconds(db):
    # given
    ticks = [0.0]
    box = Box(db, DEMO, clock=lambda: ticks[0])
    box.instances.reread()
    add_instance(box.db, 'demo', 'main', sealer_key='k', url='https://a.example.com', token='pw')

    # when
    ticks[0] = 1.5
    box.instances.reread()
    young = [i.name for i in box.instances.all()]
    ticks[0] = 2.5
    box.instances.reread()

    # then
    assert young == [] and [i.name for i in box.instances.all()] == ['main']
    assert inst.REREAD_S == 2


def test_reread_with_no_wait_always_reads_again(db):
    # given
    ticks = [0.0]
    box = Box(db, DEMO, clock=lambda: ticks[0])
    box.instances.reread()
    add_instance(box.db, 'demo', 'main', sealer_key='k', url='https://a.example.com', token='pw')

    # when
    box.instances.reread(within=0)

    # then
    assert [i.name for i in box.instances.all()] == ['main']


def test_a_database_failure_keeps_the_cache_and_logs_once(db, monkeypatch, caplog):
    # given
    box = Box(db, DEMO)
    box.instances.create('demo', 'main', GOOD, True, 'alice')

    def down(*a, **k):
        raise AutoReconnect('mongo down')

    monkeypatch.setattr(box.instances.col, 'find', down)

    # when
    with caplog.at_level(logging.WARNING, logger='skopax.instances'):
        box.instances.refresh()
        box.instances.refresh()

    # then
    assert [i.name for i in box.instances.all()] == ['main']
    assert len([r for r in caplog.records if 'AutoReconnect' in r.getMessage()]) == 1


def test_plugin_state_defaults_on_and_switches(db):
    # given
    state = PluginState(db)
    other = PluginState(db)

    # when
    default = state.enabled('demo')
    state.set('demo', False, 'alice')
    off = state.enabled('demo')
    other.refresh()
    elsewhere = other.enabled('demo')
    state.set('demo', True, 'alice')

    # then
    assert default is True and off is False and elsewhere is False and state.enabled('demo') is True
    assert db['plugin_state'].find_one({'_id': 'demo'})['enabled'] is True and db['plugin_state'].find_one({'_id': 'demo'})['updatedBy'] == 'alice'


def test_locked_plugin_contributes_nothing_even_if_stored_on(db):
    # given
    locked = dataclasses.replace(enterprise_plugin.PLUGIN, fields=(Field('url', 'URL', 'url'),), capabilities=Capabilities(hosts=('@url',)))
    box = Box(db, DEMO, locked)
    box.state.set('enterprise-demo', True, 'alice')
    add_instance(box.db, locked, 'main', url='https://e.example.com', enabled=True)
    box.runtime.refresh()

    # when
    listed = box.instances.all('enterprise-demo')

    # then
    assert box.registry.get('enterprise-demo').state == 'locked'
    assert [i.name for i in listed] == ['main']
    assert [p.id for p in box.runtime.active()] == ['demo']
    assert 'enterprise-demo' not in [p.id for p, _ in box.runtime.serving()]
    assert box.runtime.instances_of('enterprise-demo') == []


def test_runtime_serves_only_active_plugins_with_runnable_instances(db):
    # given
    bare = minimal('bare', 'Bare')
    box = Box(db, DEMO, bare)
    box.runtime.refresh()

    # when
    idle = [(p.id, [i.name for i in found]) for p, found in box.runtime.serving()]
    box.instances.create('demo', 'main', GOOD, True, 'alice')
    box.instances.create('demo', 'east', GOOD, False, 'alice')
    running = [(p.id, [i.name for i in found]) for p, found in box.runtime.serving()]
    box.instances.set_enabled('demo', 'main', False, 'alice')
    parked = [p.id for p, _ in box.runtime.serving()]
    box.instances.set_enabled('demo', 'main', True, 'alice')
    box.state.set('demo', False, 'alice')
    switched = [p.id for p, _ in box.runtime.serving()]
    silent = box.runtime.instances_of('demo')
    box.state.set('demo', True, 'alice')

    # then
    assert idle == [('bare', [])]
    assert running == [('bare', []), ('demo', ['main'])]
    assert parked == ['bare']
    assert switched == ['bare'] and silent == []
    assert [i.name for i in box.runtime.instances_of('demo')] == ['main']


def test_runtime_refresh_runs_the_entitlement_hook_and_accessors_do_not(db):
    # given
    calls = []
    edition = ['community']

    def entitlement():
        calls.append(1)
        return edition[0]

    gate = minimal('gate', 'Gate', hooks=Hooks(entitlement=entitlement))
    box = Box(db, gate, enterprise_plugin.PLUGIN)
    settled = len(calls)

    # when
    box.runtime.active()
    box.runtime.serving()
    box.instances.all()
    during = len(calls) - settled
    edition[0] = 'enterprise'
    stale = [p.id for p in box.runtime.active()]
    box.runtime.refresh()
    fresh = [p.id for p in box.runtime.active()]

    # then
    assert during == 0
    assert stale == ['gate'] and fresh == ['enterprise-demo', 'gate']


def test_runtime_refresh_survives_a_failing_part(db, monkeypatch, caplog):
    # given
    box = Box(db, DEMO)
    box.instances.create('demo', 'main', GOOD, True, 'alice')

    def boom():
        raise RuntimeError('state down')

    monkeypatch.setattr(box.state, 'refresh', boom)

    # when
    with caplog.at_level(logging.WARNING):
        box.runtime.refresh()

    # then
    assert 'RuntimeError' in caplog.text and 'state down' not in caplog.text and [i.name for i in box.runtime.instances_of('demo')] == ['main']


def test_ensure_indexes_builds_the_plugin_index(db):
    # given
    box = Box(db, DEMO)

    # when
    box.instances.ensure_indexes()

    # then
    assert any(i['key'] == [('plugin', 1)] for i in db['instances'].index_information().values())


def test_the_app_migrates_and_loads_instances_at_startup(store):
    # given
    add_instance(store, 'demo', 'main', sealer_key=master_key(store.db, settings()), url='https://demo.example.com', token='pw')

    # when
    with TestClient(create_app(settings(), store, registry=registry_with(DEMO))) as client:
        runtime = client.app.state.runtime
        found = client.app.state.instances.get('demo', 'main')
        serving = [(p.id, [i.name for i in items]) for p, items in runtime.serving()]

    # then
    assert store.db['schema_version'].find_one({'_id': 'demo'})['version'] == 2
    assert found.problem is None and found.info().config['token'] == 'pw' and found.primary
    assert serving == [('demo', ['main'])]
    assert store.db['instances'].find_one({'_id': 'demo/main'})['secrets']['token'].startswith(SEALED)


def test_a_broken_plugin_with_a_stored_instance_does_not_freeze_the_cache(db):
    # given
    shaky = dataclasses.replace(minimal('shaky', 'Shaky'), fields=None)
    box = Box(db, DEMO, shaky)
    assert box.registry.get('shaky').state == 'broken'
    add_instance(box.db, 'shaky', 'main', url='https://x.example.com')
    add_instance(box.db, 'demo', 'main', sealer_key='k', url='https://a.example.com', token='pw')
    box.instances._source_data = lambda live=True: [SourceData('file', {}, ({'plugin': 'shaky', 'name': 'main', 'url': 'https://x.example.com'},))]

    # when
    box.instances.refresh()
    made = box.instances.create('demo', 'east', GOOD, True, 'alice')

    # then
    assert sorted((i.plugin, i.name) for i in box.instances.all()) == [('demo', 'east'), ('demo', 'main')]
    assert made.name == 'east' and box.runtime.instances_of('shaky') == []


def test_a_slow_refresh_cannot_overwrite_a_newer_write(db):
    # given
    box = Box(db, DEMO)
    reading, release = threading.Event(), threading.Event()
    find = box.instances.col.find
    first = []

    def slow(*a, **k):
        old = list(find(*a, **k))
        if not first:
            first.append(1)
            reading.set()
            release.wait(5)
        return old

    box.instances.col.find = slow
    made = []
    refresher = threading.Thread(target=box.instances.refresh)
    creator = threading.Thread(target=lambda: made.append(box.instances.create('demo', 'main', GOOD, True, 'alice')))

    # when
    refresher.start()
    assert reading.wait(5)
    creator.start()
    creator.join(0.5)
    release.set()
    refresher.join(5)
    creator.join(5)

    # then
    assert [i.name for i in made] == ['main']
    assert [i.name for i in box.instances.all()] == ['main']


SENT = 'zq9-sentinel-secret'
DEMO_ONLY_BAD_URL = f'https://alice:{SENT}@prom.example.com'


def test_a_secret_never_appears_in_repr_str_view_json_or_logs(db, caplog):
    # given
    source = SourceData('file', {'token': SENT}, ({'plugin': 'demo', 'name': 'main', 'url': 'https://m.example.com', 'token': SENT}, {'plugin': 'demo', 'name': 'bad', 'url': DEMO_ONLY_BAD_URL, 'token': SENT}))
    box = Box(db, DEMO, sources=[source])
    box.instances.create('demo', 'east', {**GOOD, 'token': SENT}, True, 'alice')
    add_instance(box.db, 'demo', 'west', sealer_key='k', url=DEMO_ONLY_BAD_URL, token=SENT)

    # when
    with caplog.at_level(logging.DEBUG):
        box.instances.refresh()
        box.runtime.refresh()
        found = box.instances.all()
        shown = [repr(i) + str(i) + repr(i.info()) + str(i.info()) + json.dumps(i.view()) for i in found]
    shown.append(repr(source) + str(source) + repr(InstanceInfo('main', True, None, {'token': SENT})))

    # then
    assert len(found) == 4
    assert all(SENT not in text for text in shown)
    assert SENT not in caplog.text
    assert [i.name for i in found if i.problem] == ['bad', 'west']


def test_a_parked_instance_keeps_only_the_values_that_validated(db):
    # given
    source = SourceData('file', {}, ({'plugin': 'demo', 'name': 'bad', 'url': DEMO_ONLY_BAD_URL, 'mode': 'slow', 'token': SENT},))
    box = Box(db, DEMO, sources=[source])
    add_instance(box.db, 'demo', 'stored', sealer_key='lost', url=DEMO_ONLY_BAD_URL, token=SENT, mode='slow')
    db['instances'].update_one({'_id': 'demo/stored'}, {'$set': {'values.token': SENT}})

    # when
    box.instances.refresh()
    bad = box.instances.get('demo', 'bad')
    stored = box.instances.get('demo', 'stored')

    # then
    assert bad.values == {'mode': 'slow'} and 'URL' in bad.problem and SENT not in bad.problem
    assert stored.values == {'mode': 'slow'} and 'token' not in stored.values and stored.problem and SENT not in repr(stored) + json.dumps(stored.view())
    assert [i.name for i in box.runtime.instances_of('demo')] == []


def test_merge_plan_yields_the_merged_values_and_the_patch_from_one_rule():
    # given
    stored = {'a': '1', 'b': '2', 'd': '4'}
    given = {'a': None, 'b': '', 'c': '3', 'd': '5'}

    # when
    merged, put, drop = inst.merge_plan(stored, given)

    # then
    assert merged == {'a': '1', 'c': '3', 'd': '5'} == merged_values(stored, given)
    assert put == {'c': '3', 'd': '5'} and drop == ['b']


def test_update_keeps_clears_and_sets_per_field_in_the_stored_document(db):
    # given
    box = Box(db, DEMO)
    box.instances.create('demo', 'main', {**GOOD, 'mode': 'fast'}, True, 'alice')
    before = db['instances'].find_one({'_id': 'demo/main'})

    # when
    box.instances.update('demo', 'main', {'ca': None, 'token': None, 'mode': ''}, 'bob')
    after = db['instances'].find_one({'_id': 'demo/main'})
    box.instances.update('demo', 'main', {'url': 'https://b.example.com', 'token': 'new-token'}, 'bob')
    resealed = db['instances'].find_one({'_id': 'demo/main'})

    # then
    assert after['values'] == {'url': 'https://demo.example.com'} and 'mode' in before['values']
    assert after['secrets'] == before['secrets']
    assert resealed['secrets']['token'] != before['secrets']['token'] and resealed['values'] == {'url': 'https://b.example.com'}
    assert Sealer('k').open(resealed['secrets']['token']) == 'new-token'


def test_a_huge_regex_repeat_is_refused_not_raised(db):
    # given
    box = Box(db, KINDS)

    # when
    with pytest.raises(InvalidInstance) as caught:
        box.instances.create('kinds', 'main', {'rx': 'a{99999999999999999999}'}, True, 'alice')

    # then
    assert caught.value.field == 'rx'
    assert str(caught.value) == msg('instances.invalid.regex', field='Pattern', max=inst.REGEX_MAX)


def test_a_regex_that_recurses_too_deep_is_refused_with_the_same_reason(db, monkeypatch):
    # given
    box = Box(db, KINDS)

    def deep(_text):
        raise RecursionError('too deep')

    monkeypatch.setattr(inst.re, 'compile', deep)

    # when
    with pytest.raises(InvalidInstance) as caught:
        box.instances.create('kinds', 'main', {'rx': '(a)'}, True, 'alice')

    # then
    assert caught.value.field == 'rx'
    assert str(caught.value) == msg('instances.invalid.regex', field='Pattern', max=inst.REGEX_MAX)


def test_an_unexpected_error_while_reading_parks_the_instance_instead_of_dropping_it(db, monkeypatch):
    # given
    box = Box(db, DEMO, sources=[SourceData('file', {}, ({'plugin': 'demo', 'name': 'main', 'url': 'https://m.example.com'},))])
    add_instance(box.db, 'demo', 'east', sealer_key='k', url='https://e.example.com', token='pw')
    real = inst._check

    def boom(f, text):
        if text == 'https://e.example.com':
            raise ZeroDivisionError('boom')
        return real(f, text)

    monkeypatch.setattr(inst, '_check', boom)

    # when
    box.instances.refresh()

    # then
    east = box.instances.get('demo', 'east')
    assert east is not None and east.problem and east.values == {'mode': 'fast'}
    assert box.instances.get('demo', 'main').problem is None


def test_a_failure_outside_validation_still_parks_the_instance(db, monkeypatch):
    # given
    box = Box(db, DEMO)
    add_instance(box.db, 'demo', 'east', sealer_key='k', url='https://e.example.com', token='pw')
    monkeypatch.setattr(box.instances, '_open', lambda *a: (_ for _ in ()).throw(ZeroDivisionError('boom')))

    # when
    box.instances.refresh()

    # then
    east = box.instances.get('demo', 'east')
    assert east is not None and east.problem and east.enabled is False and east.values == {} and box.runtime.instances_of('demo') == []


def test_a_pem_that_is_not_a_certificate_is_refused(db):
    # given
    certificate = pem()
    key_block = '-----BEGIN PRIVATE KEY-----\nMIIBVQIBADANBgkqhkiG9w0BAQEFAASCAT8wggE7AgEAAkEA\n-----END PRIVATE KEY-----\n'

    # then
    assert validate(KINDS, {'ca': certificate})['ca'] == certificate
    for bad in (key_block, certificate + key_block, 'plain text'):
        with pytest.raises(InvalidInstance) as caught:
            validate(KINDS, {'ca': bad})
        assert caught.value.field == 'ca'


def test_an_update_does_not_overwrite_a_secret_another_writer_saved_meanwhile(db, monkeypatch):
    # given
    box = Box(db, DEMO)
    box.instances.create('demo', 'main', GOOD, True, 'alice')
    other = Sealer('k').seal('theirs')
    real = box.instances._make
    fired = []

    def racing(*a, **k):
        fired.append(1)
        if len(fired) == 2:
            db['instances'].update_one({'_id': 'demo/main'}, {'$set': {'secrets.token': other}})
        return real(*a, **k)

    monkeypatch.setattr(box.instances, '_make', racing)

    # when
    box.instances.update('demo', 'main', {'mode': 'slow'}, 'bob')

    # then
    stored = db['instances'].find_one({'_id': 'demo/main'})
    assert stored['secrets']['token'] == other and stored['values']['mode'] == 'slow'
    assert box.instances.get('demo', 'main').info().config['token'] == 'theirs'


def test_make_primary_returns_the_effective_change_with_several_source_primaries(db):
    # given
    source = SourceData('file', {}, ({'plugin': 'demo', 'name': 'west', 'url': 'https://w.example.com', 'primary': 'true'}, {'plugin': 'demo', 'name': 'main', 'url': 'https://m.example.com', 'primary': 'true'},
                                    {'plugin': 'demo', 'name': 'east', 'url': 'https://e.example.com'}))
    box = Box(db, DEMO, sources=[source])
    box.instances.refresh()

    # when
    changed = box.instances.make_primary('demo', 'east', 'alice')
    box.instances.refresh()

    # then
    assert changed == ('west', 'east')
    assert box.instances.primary('demo').name == 'east' and [i.name for i in box.instances.all('demo') if i.primary] == ['east']


def test_plugin_state_set_waits_for_a_slow_refresh_and_wins(db):
    # given
    state = PluginState(db)
    reading, release = threading.Event(), threading.Event()
    find = state.col.find
    first = []

    def slow(*a, **k):
        old = list(find(*a, **k))
        if not first:
            first.append(1)
            reading.set()
            release.wait(5)
        return old

    state.col.find = slow
    refresher = threading.Thread(target=state.refresh)
    setter = threading.Thread(target=lambda: state.set('demo', False, 'alice'))

    # when
    refresher.start()
    assert reading.wait(5)
    setter.start()
    setter.join(0.3)
    release.set()
    refresher.join(5)
    setter.join(5)

    # then
    assert state.enabled('demo') is False


def test_runtime_refresh_warns_once_per_failing_part_until_it_recovers(db, monkeypatch, caplog):
    # given
    box = Box(db, DEMO)
    broken = [True]

    def flaky():
        if broken[0]:
            raise RuntimeError('state down')

    monkeypatch.setattr(box.state, 'refresh', flaky)

    # when
    with caplog.at_level(logging.WARNING, logger='skopax.runtime'):
        box.runtime.refresh()
        box.runtime.refresh()
        box.runtime.refresh()
        first = len(caplog.records)
        broken[0] = False
        box.runtime.refresh()
        broken[0] = True
        box.runtime.refresh()

    # then
    assert first == 1 and len(caplog.records) == 2


def test_writes_reuse_the_last_sources_instead_of_calling_them(db):
    # given
    calls = []
    box = Box(db, DEMO)
    box.instances._sources = lambda: calls.append(1) or []
    box.instances.refresh()
    base = len(calls)

    # when
    box.instances.create('demo', 'main', GOOD, True, 'alice')
    box.instances.update('demo', 'main', {'mode': 'slow'}, 'alice')
    box.instances.set_enabled('demo', 'main', False, 'alice')
    box.instances.make_primary('demo', 'main', 'alice')
    box.instances.delete('demo', 'main', 'alice')
    box.instances.refresh()

    # then
    assert len(calls) == base + 1


def test_one_sided_int_ranges_and_missing_names_read_well(db):
    # given
    low = minimal('low', 'Low', fields=(Field('n', 'Count', 'int', low=3), Field('m', 'Limit', 'int', high=7)))
    box = Box(db, low)

    # when
    with pytest.raises(InvalidInstance) as under:
        box.instances.create('low', 'main', {'n': '1'}, True, 'alice')
    with pytest.raises(InvalidInstance) as over:
        box.instances.create('low', 'main', {'m': '9'}, True, 'alice')
    with pytest.raises(InstanceMissing) as missing:
        box.instances.update('low', 'nope', {}, 'alice')

    # then
    assert str(under.value) == 'Count must be at least 3' and str(over.value) == 'Limit must be at most 7'
    assert str(missing.value) == 'No instance named nope' and isinstance(missing.value, KeyError)


def test_the_sealer_returns_none_for_non_text_plaintext_and_non_string_input():
    # given
    raw = Fernet(base64.urlsafe_b64encode(hashlib.sha256(b'k').digest()))
    binary = 'enc:v1:' + raw.encrypt(b'\xff\xfe').decode()

    # then
    assert Sealer('k').open(binary) is None
    for odd in (None, 5, b'enc:v1:abc', ['enc:v1:x']):
        assert Sealer('k').open(odd) is None


def test_a_source_owned_secret_beats_a_stored_admin_secret(db):
    # given
    box = Box(db, DEMO, sources=[SourceData('file', {}, ({'plugin': 'demo', 'name': 'main', 'url': 'https://m.example.com', 'token': 'from-source'},))])
    add_instance(box.db, 'demo', 'main', sealer_key='k', token='from-admin')
    before = db['instances'].find_one({'_id': 'demo/main'})['secrets']['token']

    # when
    box.instances.refresh()

    # then
    assert box.instances.get('demo', 'main').info().config['token'] == 'from-source'
    assert db['instances'].find_one({'_id': 'demo/main'})['secrets']['token'] == before


def test_resealing_only_replaces_the_value_it_read(db):
    # given
    box = Box(db, DEMO)
    add_instance(box.db, 'demo', 'main', sealer_key='old', url='https://m.example.com', token='pw')
    read = db['instances'].find_one({'_id': 'demo/main'})['secrets']['token']
    newer = Sealer('old').seal('pw-newer')
    db['instances'].update_one({'_id': 'demo/main'}, {'$set': {'secrets.token': newer}})

    # when
    box.instances._reseal([('demo/main', 'token', read, 'pw')], Sealer('k'))

    # then
    assert db['instances'].find_one({'_id': 'demo/main'})['secrets']['token'] == newer
    box.instances._reseal([('demo/main', 'token', newer, 'pw-newer')], Sealer('k'))
    assert Sealer('k').open(db['instances'].find_one({'_id': 'demo/main'})['secrets']['token']) == 'pw-newer'


def test_a_broken_plugin_is_skipped_for_source_instances_too(db):
    # given
    shaky = dataclasses.replace(minimal('shaky', 'Shaky'), fields=None)
    box = Box(db, DEMO, shaky, sources=[SourceData('file', {}, ({'plugin': 'shaky', 'name': 'main', 'url': 'https://x.example.com'},))])

    # when
    box.instances.refresh()

    # then
    assert box.instances.all() == []


def test_refresh_and_source_failures_log_the_type_only_and_keep_no_message(db, monkeypatch, caplog):
    # given
    box = Box(db, DEMO)
    box.instances.create('demo', 'main', GOOD, True, 'alice')

    def down(*a, **k):
        raise AutoReconnect(SENT)

    def broken_source():
        raise RuntimeError(SENT)

    monkeypatch.setattr(box.instances.col, 'find', down)
    box.instances._sources = broken_source

    # when
    with caplog.at_level(logging.DEBUG):
        box.instances.refresh()
        box.instances.refresh()
        monkeypatch.undo()
        box.instances._sources = broken_source
        box.instances.refresh()

    # then
    assert SENT not in caplog.text
    assert all(SENT not in repr(k) for k in box.instances._warned)
    assert 'AutoReconnect' in caplog.text and 'RuntimeError' in caplog.text
    assert [i.name for i in box.instances.all()] == ['main']


def test_the_first_refresh_runs_before_the_app_serves(store, monkeypatch):
    # given
    calls = []
    real = Runtime.refresh
    monkeypatch.setattr(Runtime, 'loop', no_loop)
    monkeypatch.setattr(Runtime, 'refresh', lambda self: calls.append(1) or real(self))

    # when
    with TestClient(create_app(settings(), store, registry=registry_with(DEMO))):
        seen = len(calls)

    # then
    assert seen == 1


def test_source_logs_name_the_plugin_and_the_reason_type_never_the_source_supplied_names(db, caplog):
    # given
    source = SourceData('file', {}, (
        {'plugin': 'nope-' + SENT, 'name': 'main'},
        {'plugin': 'demo', 'name': 'Bad Name ' + SENT, 'url': 'https://m.example.com'},
    ))
    box = Box(db, DEMO, sources=[source])

    # when
    with caplog.at_level(logging.DEBUG):
        box.instances.refresh()

    # then
    assert SENT not in caplog.text and all(SENT not in repr(k) for k in box.instances._warned)
    assert 'plug-in demo' in caplog.text and 'InvalidInstance' in caplog.text
    assert all(isinstance(k, str) and len(k) == 64 for k in box.instances._warned)


def test_no_update_sends_a_path_in_both_set_and_unset(db, monkeypatch):
    # given
    box = Box(db, DEMO)
    sent = []
    real = type(box.instances.col).update_one

    def spy(self, flt, update, *a, **k):
        sent.append(update)
        return real(self, flt, update, *a, **k)

    monkeypatch.setattr(type(box.instances.col), 'update_one', spy)

    # when
    box.instances.create('demo', 'main', {**GOOD, 'mode': 'fast'}, True, 'alice')
    box.instances.update('demo', 'main', {'url': 'https://b.example.com', 'token': '', 'mode': ''}, 'bob')
    box.instances.update('demo', 'main', {'mode': 'slow', 'token': 'again'}, 'bob')

    # then
    updates = [u for u in sent if '$unset' in u]
    assert updates and all(not set(u['$set']) & set(u['$unset']) for u in updates)


EXTRA = (Field('echo.url', 'Echo URL', 'url'), Field('echo.token', 'Echo token', 'secret'))
AGENTISH = minimal('agentish', 'Agentish', capabilities=Capabilities(secrets=('token', 'echo.token')), fields=(
    Field('mode', 'Mode', 'choice', default='agent', choices=('agent', 'local')), Field('url', 'URL', 'url', required=True, show_if=('mode', 'agent')),
    Field('token', 'Token', 'secret', required=True, show_if=('mode', 'agent'))))


def extra_for(plugin_id):
    return EXTRA if plugin_id == 'agentish' else ()


def test_instances_take_extra_agent_read_fields_and_keep_dotted_keys_whole(db):
    # given
    box = Box(db, AGENTISH, extra_fields=extra_for)
    values = {'url': 'https://east.example.com', 'token': 'pw', 'echo.url': 'https://echo.example.com', 'echo.token': LEAK}

    # when
    made = box.instances.create('agentish', 'east', values, True, 'alice')
    with pytest.raises(InvalidInstance) as bare:
        Box(db, AGENTISH).instances.create('agentish', 'west', values, True, 'alice')
    changed = box.instances.update('agentish', 'east', {'echo.url': 'https://other.example.com', 'url': 'https://east2.example.com', 'token': 'pw', 'echo.token': LEAK}, 'alice')
    doc = db['instances'].find_one({'_id': 'agentish/east'})
    cleared = box.instances.update('agentish', 'east', {'echo.url': ''}, 'alice')
    flat = db['instances'].find_one({'_id': 'agentish/east'})
    with pytest.raises(InvalidInstance) as unknown:
        box.instances.update('agentish', 'east', {'echo.nope': 'x'}, 'alice')
    with pytest.raises(InvalidInstance) as invalid:
        box.instances.update('agentish', 'east', {'echo.url': 'ftp://x'}, 'alice')

    # then
    assert made.problem is None and made.values['echo.url'] == 'https://echo.example.com' and made.secrets['echo.token'] == LEAK
    assert bare.value.field == 'echo.url'
    assert changed.values['echo.url'] == 'https://other.example.com' and changed.values['url'] == 'https://east2.example.com'
    assert set(doc['values']) == {'url', 'echo.url'} and set(doc['secrets']) == {'token', 'echo.token'} and doc['secrets']['echo.token'].startswith(SEALED) and LEAK not in json.dumps(doc, default=str)
    assert set(flat['values']) == {'url'} and 'echo.url' not in cleared.values and cleared.secrets['echo.token'] == LEAK
    assert unknown.value.field == 'echo.nope' and invalid.value.field == 'echo.url'
    assert made.view()['secrets']['echo.token'] == {'set': True, 'from': None} and LEAK not in json.dumps(made.view())


def test_a_plugin_does_not_accept_the_fields_of_its_own_agent_reads_on_its_own_instances(db):
    # given
    own = dataclasses.replace(AGENTISH, id='owner', name='Owner', agent_reads=(AgentRead('owner-read', ('q',), (r'own\.k',), lambda c: [], lambda c, a: {}, fields=(Field('own.k', 'Own key', 'secret'),)),),
                              capabilities=Capabilities(secrets=('token', 'own.k'), agent_reads=True))
    box = Box(db, own)

    # when
    with pytest.raises(InvalidInstance) as e:
        box.instances.create('owner', 'east', {'url': 'https://east.example.com', 'token': 'pw', 'own.k': LEAK}, True, 'alice')
    with pytest.raises(InvalidInstance) as checked:
        validate(own, {'url': 'https://east.example.com', 'token': 'pw', 'own.k': LEAK})

    # then
    assert e.value.field == 'own.k' and checked.value.field == 'own.k'


def test_validate_takes_extra_fields_and_refuses_them_without(db):
    # given
    values = {'url': 'https://east.example.com', 'token': 'pw', 'echo.url': 'https://echo.example.com'}

    # when
    ok = validate(AGENTISH, values, extra=EXTRA)
    with pytest.raises(InvalidInstance) as e:
        validate(AGENTISH, values)

    # then
    assert ok['echo.url'] == 'https://echo.example.com' and e.value.field == 'echo.url'


def test_a_required_field_is_only_required_while_it_is_shown(db):
    # when
    local = validate(AGENTISH, {'mode': 'local'})
    with pytest.raises(InvalidInstance) as no_url:
        validate(AGENTISH, {'mode': 'agent', 'token': 'pw'})
    with pytest.raises(InvalidInstance) as no_token:
        validate(AGENTISH, {'url': 'https://east.example.com'})
    with pytest.raises(InvalidInstance) as by_default:
        validate(AGENTISH, {})
    full = validate(AGENTISH, {'url': 'https://east.example.com', 'token': 'pw'})

    # then
    assert local == {'mode': 'local'} and full['url'] == 'https://east.example.com'
    assert (no_url.value.field, no_token.value.field, by_default.value.field) == ('url', 'token', 'url')
    assert Box(db, AGENTISH).instances.create('agentish', 'here', {'mode': 'local'}, True, 'alice').problem is None


def test_a_dotted_secret_is_sealed_again_as_a_whole_map_and_only_when_it_is_still_the_one_read(db):
    # given
    generated = master_key(db, settings())
    box = Box(db, AGENTISH, key=generated, generated=True, extra_fields=extra_for)
    box.instances.create('agentish', 'east', {'url': 'https://east.example.com', 'token': 'pw', 'echo.token': LEAK}, True, 'alice')
    read = db['instances'].find_one({'_id': 'agentish/east'})['secrets']['echo.token']

    # when
    box.key, box.generated = 'k', False
    box.instances.refresh()
    doc = db['instances'].find_one({'_id': 'agentish/east'})
    held = doc['secrets']
    newer = Sealer('k').seal('newer')
    db['instances'].update_one({'_id': 'agentish/east'}, {'$set': {'secrets': {**held, 'echo.token': newer}}})
    box.instances._reseal([('agentish/east', 'echo.token', held['echo.token'], 'stale')], Sealer('k2'))
    after = db['instances'].find_one({'_id': 'agentish/east'})['secrets']

    # then
    assert read != held['echo.token'] and Sealer('k').open(held['echo.token']) == LEAK and Sealer('k').open(held['token']) == 'pw'
    assert set(held) == {'token', 'echo.token'} and after['echo.token'] == newer
    box.instances.refresh()
    assert box.instances.get('agentish', 'east').secrets['echo.token'] == 'newer'


def test_an_unreadable_instance_document_is_logged_without_its_name(db, caplog):
    # given
    box = Box(db, DEMO)
    db['instances'].insert_one({'_id': 'demo/odd', 'plugin': 'demo', 'name': 'odd' + chr(10) + 'FORGED line', 'values': ['not', 'a', 'map']})

    # when
    with caplog.at_level(logging.WARNING):
        box.instances.refresh()

    # then
    assert 'an instance of plug-in demo could not be read: AttributeError' in caplog.text
    assert 'FORGED' not in caplog.text


def test_an_admin_instance_whose_name_a_later_plugin_reserves_is_parked(db):
    # given
    before = Box(db, DEMO)
    before.instances.create('demo', 'east', {'url': 'https://east.example.com'}, True, 'alice')
    later = minimal('later', 'Later', checks=(Check('east', 'East', 900, 0, ('east',), DEMO.checks[0].run),))

    # when
    after = Box(db, DEMO, later)
    after.instances.refresh()
    east = after.instances.get('demo', 'east')

    # then
    assert east.problem == msg('instances.reserved_now', reason=msg('instances.invalid.reserved', name='east'))
    assert 'east' not in [i.name for i in after.runtime.instances_of('demo')]


def test_off_checks_come_from_the_document_and_stale_or_malformed_ids_are_dropped(db):
    # given
    box = Box(db, two_checks_plugin.PLUGIN)
    for i, name in enumerate(('main', 'east', 'west', 'north')):
        add_instance(box.db, two_checks_plugin.PLUGIN, name, sealer_key='k', primary=i == 0, url='https://demo.example.com')
    db['instances'].update_one({'_id': 'demo/main'}, {'$set': {'offChecks': ['pulse', 'gone', 7, 'pulse']}})
    db['instances'].update_one({'_id': 'demo/east'}, {'$set': {'offChecks': 'pulse'}})
    db['instances'].update_one({'_id': 'demo/west'}, {'$set': {'offChecks': None}})

    # when
    box.instances.refresh()
    found = {i.name: i for i in box.instances.all('demo')}

    # then
    assert {n: i.off_checks for n, i in found.items()} == {'main': frozenset({'pulse'}), 'east': frozenset(), 'west': frozenset(), 'north': frozenset()}
    assert all(i.problem is None for i in found.values())
    assert found['main'].view()['offChecks'] == ['pulse'] and found['north'].view()['offChecks'] == []
    assert [i.name for i in box.runtime.instances_of('demo')] == ['main', 'east', 'north', 'west']


def test_set_checks_stores_a_sorted_set_and_unknown_ids_are_refused_on_every_write(db):
    # given
    box = Box(db, two_checks_plugin.PLUGIN)
    made = box.instances.create('demo', 'main', GOOD, True, 'alice', off_checks=['beat'])

    # when
    changed = box.instances.set_checks('demo', 'main', ['pulse', 'beat', 'pulse'], 'bob')
    with pytest.raises(InvalidInstance) as bad:
        box.instances.set_checks('demo', 'main', ['nope'], 'bob')
    with pytest.raises(InvalidInstance) as bad_new:
        box.instances.create('demo', 'east', GOOD, True, 'alice', off_checks=['pulse', 'nope'])
    with pytest.raises(InstanceMissing):
        box.instances.set_checks('demo', 'ghost', [], 'bob')
    cleared = box.instances.set_checks('demo', 'main', [], 'carol')
    doc = db['instances'].find_one({'_id': 'demo/main'})

    # then
    assert made.off_checks == frozenset({'beat'}) and changed.off_checks == frozenset({'beat', 'pulse'}) and cleared.off_checks == frozenset()
    assert (bad.value.field, str(bad.value)) == ('offChecks', msg('instances.invalid.checks', check='nope')) and bad_new.value.field == 'offChecks'
    assert doc['offChecks'] == [] and doc['updatedBy'] == 'carol' and box.instances.get('demo', 'east') is None
