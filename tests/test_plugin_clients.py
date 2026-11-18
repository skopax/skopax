import base64
import dataclasses
import hashlib
import logging
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import mongomock
import pytest
from cryptography.fernet import Fernet

from skopax.app import create_app
from skopax.contract import SourceData, SourceDown
from skopax.instances import SECRET_MAX, Instances, InvalidInstance, PluginState, SourceOwned
from skopax.messages import msg
from skopax.registry import Registry
from skopax.runtime import Runtime
from skopax.sealer import Sealer
from tests.conftest import settings
from tests.kube_kit import KEY as KUBE_KEY, FakeKube, echoer, kube_runtime
from tests.plugin_kit import add_instance
from tests.plugins import demo_plugin
from tests.test_instances import Box

HERE = Path(demo_plugin.__file__).parent
KEY = 'client-key'
DEMO = demo_plugin.PLUGIN
LIMITED = dataclasses.replace(DEMO, max_instances=1)


@pytest.fixture(autouse=True)
def clean_builds():
    demo_plugin.BUILT.clear()
    yield
    demo_plugin.BUILT.clear()


@pytest.fixture
def db():
    return mongomock.MongoClient(tz_aware=True)['skopax']


def runtime_of(store, *plugins) -> Runtime:
    reg = Registry.of(*plugins, directory=HERE)
    runtime = Runtime(reg, PluginState(store.db), Instances(store.db, reg, lambda: Sealer(KEY)))
    runtime.refresh()
    return runtime


def listed(*names) -> list[SourceData]:
    return [SourceData.of({'instances': [{'plugin': 'demo', 'name': n, 'url': f'https://{n}.example.com'} for n in names]}, 'demo source')]


def sealed_with(key: bytes, text: bytes) -> str:
    return 'enc:v1:' + Fernet(base64.urlsafe_b64encode(hashlib.sha256(key).digest())).encrypt(text).decode()


def test_runtime_client_is_built_once_per_config(store):
    # given
    add_instance(store, DEMO, 'main', sealer_key=KEY, primary=True, url='https://demo.example.com')
    runtime = runtime_of(store, DEMO)

    # when
    first, second = runtime.client('demo'), runtime.client('demo')
    runtime.instances.update('demo', 'main', {'url': 'https://other.example.com'}, 'alice')
    runtime.refresh()
    third = runtime.client('demo')
    named = runtime.client('demo', 'main')

    # then
    assert first is second and third is not first and named is third
    assert demo_plugin.BUILT == ['main', 'main']
    assert (first.url, third.url) == ('https://demo.example.com', 'https://other.example.com')


def test_client_overrides_win(store):
    # given
    fake = object()

    # when
    app = create_app(settings(), store, registry=Registry.of(DEMO, directory=HERE), clients={'demo': fake})
    runtime = app.state.runtime

    # then
    assert runtime.client('demo') is fake and runtime.instances.all('demo') == []
    assert runtime.proxy('demo').enabled is True
    assert demo_plugin.BUILT == []


def test_live_client_follows_the_switch(store):
    # given
    add_instance(store, DEMO, 'main', sealer_key=KEY, primary=True, url='https://demo.example.com')
    runtime = runtime_of(store, DEMO)
    live = runtime.proxy('demo')

    # when
    on, pinged = live.enabled, live.ping()
    runtime.state.set('demo', False, 'alice')
    off = live.enabled
    with pytest.raises(SourceDown) as down:
        live.anything()
    private = hasattr(live, '_hidden')

    # then
    assert (on, pinged, off, private) == (True, 'https://demo.example.com', False, False)
    assert down.value.text == msg('integrations.off', name='Demo')


def test_a_raising_client_factory_gives_none_and_logs_once(store, caplog):
    # given
    def boom(info, transport):
        raise RuntimeError('token=s3cret-value')

    broken = dataclasses.replace(DEMO, client=boom)
    add_instance(store, broken, 'main', sealer_key=KEY, primary=True, url='https://demo.example.com')
    runtime = runtime_of(store, broken)

    # when
    with caplog.at_level(logging.WARNING, logger='skopax.runtime'):
        got = [runtime.client('demo'), runtime.client('demo'), runtime.proxy('demo').enabled]
    warned = [r.getMessage() for r in caplog.records if r.name == 'skopax.runtime']

    # then
    assert got == [None, None, False]
    assert warned == [msg('runtime.client_failed', plugin='demo', error='RuntimeError')]
    assert all('s3cret' not in line for line in warned)


def test_a_hanging_client_factory_gives_none_within_the_hook_bound_and_logs_once(store, caplog):
    # given
    release, tries = threading.Event(), []

    def hangs(info, transport):
        tries.append(info.name)
        release.wait(3)
        return object()

    slow = dataclasses.replace(DEMO, client=hangs)
    add_instance(store, slow, 'main', sealer_key=KEY, primary=True, url='https://demo.example.com')
    runtime = runtime_of(store, slow)
    runtime.hook_timeout = 0.1

    # when
    began = time.monotonic()
    with caplog.at_level(logging.WARNING, logger='skopax.runtime'):
        got = [runtime.client('demo'), runtime.client('demo'), runtime.proxy('demo').enabled]
    took = time.monotonic() - began
    release.set()
    warned = [r.getMessage() for r in caplog.records if r.name == 'skopax.runtime']

    # then
    assert got == [None, None, False] and tries == ['main']
    assert took < 1
    assert warned == [msg('runtime.client_failed', plugin='demo', error='TimedOut')]


def test_concurrent_first_calls_share_one_healthy_slow_build(store, caplog):
    # given
    tries, got = [], []

    def slow(info, transport):
        tries.append(info.name)
        time.sleep(0.2)
        return object()

    plugin = dataclasses.replace(DEMO, client=slow)
    add_instance(store, plugin, 'main', sealer_key=KEY, primary=True, url='https://demo.example.com')
    runtime = runtime_of(store, plugin)
    workers = [threading.Thread(target=lambda: got.append(runtime.client('demo'))) for _ in range(2)]

    # when
    with caplog.at_level(logging.WARNING, logger='skopax.runtime'):
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(5)

    # then
    assert len(got) == 2 and got[0] is not None and got[0] is got[1]
    assert tries == ['main'], 'the second caller waits for the build in flight instead of failing'
    assert [r.getMessage() for r in caplog.records if r.name == 'skopax.runtime'] == []


def test_a_new_config_waiting_for_a_hung_build_shares_one_deadline_with_its_own_build(store):
    # given
    release, took = threading.Event(), {}

    def hangs(info, transport):
        release.wait(3)
        return object()

    plugin = dataclasses.replace(DEMO, client=hangs)
    add_instance(store, plugin, 'main', sealer_key=KEY, primary=True, url='https://demo.example.com')
    runtime = runtime_of(store, plugin)
    runtime.hook_timeout = 0.5

    def ask(name: str) -> None:
        began = time.monotonic()
        runtime.client('demo')
        took[name] = time.monotonic() - began

    first = threading.Thread(target=ask, args=('old',))

    # when
    first.start()
    time.sleep(0.05)
    runtime.instances.update('demo', 'main', {'url': 'https://other.example.com'}, 'alice')
    runtime.refresh()
    ask('new')
    first.join(3)
    release.set()

    # then
    assert took['old'] < 0.5 + 0.2
    assert took['new'] < 0.5 + 0.2, 'the wait for the gate and the build share one hook bound'


def test_a_failed_client_build_is_not_retried_until_the_values_change_or_a_minute_passes(store):
    # given
    tries, now = [], [100.0]

    def boom(info, transport):
        tries.append(info.config['url'])
        raise RuntimeError('down')

    broken = dataclasses.replace(DEMO, client=boom)
    add_instance(store, broken, 'main', sealer_key=KEY, primary=True, url='https://demo.example.com')
    runtime = runtime_of(store, broken)
    runtime.clock = lambda: now[0]

    # when
    runtime.client('demo')
    runtime.client('demo')
    runtime.proxy('demo').enabled
    held = len(tries)
    now[0] += 61
    runtime.client('demo')
    later = len(tries)
    runtime.instances.update('demo', 'main', {'url': 'https://other.example.com'}, 'alice')
    runtime.refresh()
    runtime.client('demo')

    # then
    assert (held, later) == (1, 2)
    assert tries == ['https://demo.example.com', 'https://demo.example.com', 'https://other.example.com']


def test_a_switched_off_proxy_answers_hasattr_and_getattr_for_private_names(store):
    # given
    add_instance(store, DEMO, 'main', sealer_key=KEY, primary=True, url='https://demo.example.com')
    runtime = runtime_of(store, DEMO)
    live = runtime.proxy('demo')

    # when
    private_on = live.transport is runtime.client('demo').transport and live._probe_private() == 'https://demo.example.com'
    runtime.state.set('demo', False, 'alice')
    shown = (hasattr(live, '_paths'), getattr(live, '__deepcopy__', None), getattr(live, '_paths', 'none'))
    with pytest.raises(SourceDown):
        live.ping

    # then
    assert private_on is True
    assert shown == (False, None, 'none')


def test_the_proxy_honours_the_clients_own_enabled_flag(store):
    # given
    off = SimpleNamespace(enabled=False)
    on = SimpleNamespace(enabled=True)
    bare = object()

    # when
    flags = [Runtime(Registry.of(), PluginState(store.db), Instances(store.db, Registry.of(), lambda: Sealer(KEY)), clients={'demo': c}).proxy('demo').enabled for c in (off, on, bare)]

    # then
    assert flags == [False, True, True]


def test_a_context_hands_its_plugin_the_one_cached_client(store):
    # given
    add_instance(store, DEMO, 'main', sealer_key=KEY, primary=True, url='https://demo.example.com')
    add_instance(store, DEMO, 'east', sealer_key=KEY, primary=False, url='https://east.example.com')
    runtime = runtime_of(store, DEMO)
    fake = object()
    overridden = Runtime(runtime.registry, runtime.state, runtime.instances, clients={'demo': fake})
    overridden.refresh()

    # when
    main = runtime.context(DEMO, runtime.instances.get('demo', 'main'), 0).client()
    east = runtime.context(DEMO, runtime.instances.get('demo', 'east'), 0).client()
    again = runtime.context(DEMO, runtime.instances.get('demo', 'east'), 0).client()
    faked = overridden.context(DEMO, overridden.instances.get('demo', 'east'), 0).client()

    # then
    assert main is runtime.client('demo') and east is runtime.client('demo', 'east') and again is east
    assert (main.url, east.url) == ('https://demo.example.com', 'https://east.example.com')
    assert faked is fake and demo_plugin.BUILT == ['main', 'east']


def test_max_instances_refuses_a_second_and_parks_extra_source_instances(db):
    # given
    box = Box(db, LIMITED, sources=listed('main', 'east'))
    box.instances.refresh()

    # when
    with pytest.raises(InvalidInstance) as refused:
        box.instances.create('demo', 'west', {'url': 'https://west.example.com'}, True, 'alice')
    parked = {i.name: i.problem for i in box.instances.all('demo')}

    # then
    assert (refused.value.field, refused.value.text) == ('name', msg('instances.max_one'))
    assert parked == {'main': None, 'east': msg('instances.max_one')}
    assert msg('instances.max_one') == 'This plug-in takes one instance'


def test_a_limit_above_one_names_its_count(db):
    # given
    box = Box(db, dataclasses.replace(DEMO, max_instances=2), sources=listed('main', 'east', 'west'))

    # when
    box.instances.refresh()
    parked = {i.name: i.problem for i in box.instances.all('demo')}

    # then
    assert parked == {'main': None, 'east': None, 'west': msg('instances.max', count=2)}
    assert msg('instances.max', count=2) == 'This plug-in takes at most 2 instances'


def test_a_source_instance_takes_the_slot_of_an_admin_instance_made_first(db):
    # given
    box = Box(db, LIMITED)
    box.runtime.refresh()
    box.instances.create('demo', 'solo', {'url': 'https://solo.example.com'}, True, 'alice')
    box.sources[:] = listed('main')

    # when
    box.runtime.refresh()
    found = {i.name: (i.primary, i.problem) for i in box.instances.all('demo')}
    served = box.runtime.client('demo')

    # then
    assert found == {'main': (True, None), 'solo': (False, msg('instances.max_one'))}
    assert served.url == 'https://main.example.com'
    assert [i.name for i in box.runtime.instances_of('demo')] == ['main']


def test_adopt_sealed_moves_an_old_secret_without_decrypting(db):
    # given
    sealed, foreign = sealed_with(b'k', b'pw'), sealed_with(b'k2', b'pw')
    box = Box(db, DEMO, key='k')
    box.instances.create('demo', 'main', {'url': 'https://demo.example.com'}, True, 'alice')

    # when
    refusals = []
    for field, value in (('token', foreign), ('url', sealed), ('token', 'pw')):
        with pytest.raises(InvalidInstance) as refused:
            box.instances.adopt_sealed('demo', 'main', field, value, 'system')
        refusals.append((refused.value.field, refused.value.text))
    untouched = db['instances'].find_one({'_id': 'demo/main'})
    adopted = box.instances.adopt_sealed('demo', 'main', 'token', sealed, 'system')
    stored = db['instances'].find_one({'_id': 'demo/main'})

    # then
    assert refusals == [('token', msg('instances.secret_unreadable', field='Token')), ('url', msg('instances.not_sealable', field='URL')),
                        ('token', msg('instances.secret_unreadable', field='Token'))]
    assert 'token' not in untouched['secrets'] and untouched['updatedBy'] == 'alice'
    assert stored['secrets']['token'] == sealed and stored['updatedBy'] == 'system'
    assert adopted.info().config['token'] == 'pw'


def test_adopt_sealed_opens_a_sealed_plain_text_value_and_stores_it_plain(db):
    # given
    from skopax.contract import Capabilities, Field
    from tests.plugins.base import minimal
    keys = minimal('keys', 'Keys', capabilities=Capabilities(secrets=('secret-key',)), fields=(
        Field('url', 'URL', 'url', required=True), Field('public-key', 'Public key', 'text'), Field('mode', 'Mode', 'choice', choices=('a', 'b')), Field('secret-key', 'Secret key', 'secret')))
    box = Box(db, keys, key='k')
    box.instances.create('keys', 'main', {'url': 'https://keys.example.com'}, True, 'alice')

    # when
    adopted = box.instances.adopt_sealed('keys', 'main', 'public-key', sealed_with(b'k', b'pk-lf-0001'), 'system')
    stored = db['instances'].find_one({'_id': 'keys/main'})
    refused = []
    for field, value in (('mode', sealed_with(b'k', b'a')), ('public-key', sealed_with(b'k2', b'pk-lf-0002')), ('nope', sealed_with(b'k', b'x'))):
        with pytest.raises(InvalidInstance) as e:
            box.instances.adopt_sealed('keys', 'main', field, value, 'system')
        refused.append((e.value.field, e.value.text))

    # then
    assert stored['values']['public-key'] == 'pk-lf-0001' and 'public-key' not in (stored.get('secrets') or {}) and stored['updatedBy'] == 'system'
    assert adopted.values['public-key'] == 'pk-lf-0001'
    assert refused == [('mode', msg('instances.not_sealable', field='Mode')), ('public-key', msg('instances.secret_unreadable', field='Public key')),
                       ('nope', msg('instances.invalid.unknown', field='nope'))]
    assert msg('instances.not_sealable', field='Mode') == 'Mode is not a secret or a text field, so it cannot take a sealed value: enter it in the form'


def test_adopt_sealed_refuses_an_oversized_secret_and_a_source_owned_field(db):
    # given
    box = Box(db, DEMO, key='k', sources=[SourceData.of({'instances': [{'plugin': 'demo', 'name': 'env', 'url': 'https://env.example.com', 'token': 't'}]}, 'demo source')])
    box.instances.refresh()
    box.instances.create('demo', 'main', {'url': 'https://demo.example.com'}, True, 'alice')

    # when
    with pytest.raises(InvalidInstance) as long:
        box.instances.adopt_sealed('demo', 'main', 'token', sealed_with(b'k', b'x' * (SECRET_MAX + 1)), 'system')
    untouched = db['instances'].find_one({'_id': 'demo/main'})
    with pytest.raises(SourceOwned) as owned:
        box.instances.adopt_sealed('demo', 'env', 'token', sealed_with(b'k', b'pw'), 'system')

    # then
    assert (long.value.field, long.value.text) == ('token', msg('instances.invalid.long', field='Token', max=SECRET_MAX))
    assert 'token' not in untouched['secrets']
    assert owned.value.field == 'token' and db['instances'].find_one({'_id': 'demo/env'}) is None


def test_adopt_sealed_writes_a_dotted_agent_field_into_the_secrets_map(store):
    # given
    runtime = kube_runtime(store, {'east': {'mode': 'agent', 'url': 'https://east.example.com', 'token': 'east-token-0002'}}, kube=FakeKube(), plugins=(echoer(),))
    sealed = sealed_with(KUBE_KEY.encode(), b'echo-secret')

    # when
    adopted = runtime.instances.adopt_sealed('kubernetes', 'east', 'echo.token', sealed, 'system')
    stored = runtime.instances.db['instances'].find_one({'_id': 'kubernetes/east'})['secrets']

    # then
    assert stored['echo.token'] == sealed and stored['token'] != sealed
    assert adopted.info().config['echo.token'] == 'echo-secret' and adopted.info().config['token'] == 'east-token-0002'


def test_adopt_sealed_takes_a_value_of_the_stored_key_and_seals_it_again(db):
    # given
    db['meta'].insert_one({'_id': 'secret-key', 'key': 'old'})
    sealed = sealed_with(b'old', b'pw')
    box = Box(db, DEMO, key='k')
    box.instances.create('demo', 'main', {'url': 'https://demo.example.com'}, True, 'alice')

    # when
    adopted = box.instances.adopt_sealed('demo', 'main', 'token', sealed, 'system')
    stored = db['instances'].find_one({'_id': 'demo/main'})['secrets']['token']

    # then
    assert adopted.info().config['token'] == 'pw'
    assert stored != sealed and Sealer('k').open(stored) == 'pw'
