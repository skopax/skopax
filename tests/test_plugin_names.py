import dataclasses
import logging
import shutil
from pathlib import Path

import mongomock
import pytest
from fastapi.testclient import TestClient

from skopax import settingsources
from skopax.app import create_app
from skopax.config import ConfigurationError
from skopax.contract import Check, PassResult, RuleSet, Setting, SourceData, Sweep
from skopax.instances import Instances, InvalidInstance, check_name
from skopax.kube import Kube
from skopax.messages import msg
from skopax.plugins import slack as slack_plugin
from skopax.registry import Registry
from skopax.sealer import Sealer
from skopax.settingsources import FILE_ENV, boot
from skopax.sweeps import scope
from tests.conftest import settings
from tests.plugins import cache_plugin, demo_plugin, demo_twin
from tests.plugins.base import minimal

HERE = Path(demo_plugin.__file__).parent
CACHE = cache_plugin.PLUGIN
DEMO = demo_plugin.PLUGIN
NOW = 1_792_224_000.0
LOG_ROWS = [{'app': 'checkout-service', 'level': 'ERROR', 'msg': 'pool exhausted', 'now': 40, 'd1': 2, 'd7': 1},
            {'app': 'payments', 'level': 'WARN', 'msg': 'slow reply', 'now': 3, 'd1': 1, 'd7': 0}]


@pytest.fixture
def db():
    return mongomock.MongoClient(tz_aware=True)['skopax']


def instances_of(db, *plugins) -> Instances:
    return Instances(db, Registry.of(*plugins, directory=HERE), lambda: Sealer('k'))


def write_log_top(ctx) -> Sweep:
    return Sweep(PassResult('logs', True, 'ok', [], {}, ('logs',)), {'logTop': [dict(r) for r in LOG_ROWS]})


def sectioned(*names: str):
    return minimal('logger', 'Logger', checks=(Check('logs', 'Logs', 900, 0, ('logs',), write_log_top, sections=names),))


def slack_like(**over):
    return minimal('slack', 'Slack', checks=(), **over)


def test_twin_checks_of_one_plugin_load_and_prefix_claims_across_plugins_stay():
    # given
    stranger = minimal('stranger', 'Stranger', checks=(Check('tick-x', 'Tick x', 900, 0, ('tick-x',), cache_plugin.tick),))

    # when
    loaded = Registry.load({'SKOPAX_PLUGINS': 'tests.plugins.cache_plugin'}, builtin=(), entry_points=False).get('cache-demo')
    both = Registry.of(CACHE, stranger, directory=HERE)

    # then
    assert (loaded.state, loaded.reason) == ('ok', None)
    assert [c.id for c in loaded.plugin.checks] == ['tick', 'tick-cache']
    assert both.get('cache-demo').state == 'ok'
    assert both.get('stranger').state == 'broken' and 'clashes with cache-demo' in both.get('stranger').reason


def test_an_instance_name_that_would_shadow_a_check_pass_is_refused(db):
    # given
    instances = instances_of(db, CACHE, DEMO)
    refused = {}

    # when
    for name in ('cache', 'cache-east'):
        with pytest.raises(InvalidInstance) as caught:
            instances.create('cache-demo', name, {}, True, 'alice')
        refused[name] = (caught.value.field, caught.value.text)
    cached = instances.create('cache-demo', 'cached', {}, True, 'alice')
    elsewhere = instances.create('demo', 'cache', {'url': 'https://demo.example.com'}, True, 'alice')

    # then
    assert instances.registry.get('cache-demo').state == 'ok'
    assert refused == {name: ('name', msg('instances.invalid.reserved', name=name)) for name in ('cache', 'cache-east')}
    assert (cached.name, elsewhere.name) == ('cached', 'cache')
    shadowed = instances.registry.shadowed
    assert (shadowed('cache-demo', 'cache'), shadowed('cache-demo', 'cache-east'), shadowed('cache-demo', 'cached'), shadowed('demo', 'cache')) == (True, True, False, False)


def test_the_name_check_always_knows_its_plugin():
    # given
    reg = Registry.of(CACHE, directory=HERE)

    # when
    with pytest.raises(TypeError):
        check_name(reg, 'cache-east')
    with pytest.raises(InvalidInstance) as caught:
        check_name(reg, 'cache-east', 'cache-demo')

    # then
    assert (caught.value.field, caught.value.text) == ('name', msg('instances.invalid.reserved', name='cache-east'))


@pytest.mark.parametrize('ran_checks', [
    (Check('pulse', 'Pulse', 900, 0, ('demo',), demo_plugin.pulse), Check('beat', 'Beat', 900, 0, ('demo-b',), demo_plugin.pulse)),
    (Check('pulse', 'Pulse', 900, 0, ('demo', 'demo-b'), demo_plugin.pulse),),
], ids=['two-checks', 'one-check'])
def test_ran_key_twins_refuse_their_suffix_and_names_under_it(db, ran_checks):
    # given
    instances = instances_of(db, dataclasses.replace(DEMO, checks=ran_checks))
    refused = {}

    # when
    for name in ('b', 'b-east'):
        with pytest.raises(InvalidInstance) as caught:
            instances.create('demo', name, {'url': 'https://demo.example.com'}, True, 'alice')
        refused[name] = (caught.value.field, caught.value.text)
    made = [instances.create('demo', name, {'url': 'https://demo.example.com'}, True, 'alice').name for name in ('east', 'bee', 'east-two')]

    # then
    assert refused == {name: ('name', msg('instances.invalid.reserved', name=name)) for name in ('b', 'b-east')}
    assert made == ['east', 'bee', 'east-two']


def test_a_scoped_ran_key_comes_from_one_instance_only(db):
    # given
    beat = Check('beat', 'Beat', 900, 0, ('demo-b',), demo_plugin.pulse)
    plugin = dataclasses.replace(DEMO, checks=(DEMO.checks[0], beat))
    instances = instances_of(db, plugin)
    for name in ('main', 'east', 'b', 'b-east', 'west', 'b-west', 'east-two'):
        try:
            instances.create('demo', name, {'url': 'https://demo.example.com'}, True, 'alice')
        except InvalidInstance:
            pass

    # when
    made = instances.all('demo')
    origins = [(key, check.id, inst.name) for inst in made for check in plugin.checks for key in scope(Sweep(PassResult(check.id, True, 'ok')), check, inst.info()).result.ran]

    # then
    assert sorted(i.name for i in made) == ['east', 'east-two', 'main', 'west']
    assert [o for o in origins if o[0] == 'demo-b-east'] == [('demo-b-east', 'beat', 'east')]
    assert len({o[0] for o in origins}) == len(origins)


def test_a_stored_shadow_name_is_parked_and_a_configured_one_stops_boot(db, tmp_path):
    # given
    db['instances'].insert_one({'_id': 'cache-demo/cache', 'plugin': 'cache-demo', 'name': 'cache', 'values': {}, 'secrets': {}, 'createdBy': 'alice', 'createdAt': '2026-11-01T00:00:00Z'})
    path = tmp_path / 'skopax.yaml'
    path.write_text('plugins:\n  cache-demo:\n    instances:\n      - name: cache-east\n', encoding='utf-8')
    instances = instances_of(db, CACHE)

    # when
    instances.refresh()
    stored = instances.get('cache-demo', 'cache')
    with pytest.raises(ConfigurationError) as stopped:
        boot({FILE_ENV: str(path)}, Registry.of(CACHE, directory=HERE))

    # then
    assert stored.problem == msg('instances.reserved_now', reason=msg('instances.invalid.reserved', name='cache'))
    assert str(stopped.value) == msg('settings.instance.invalid', plugin='cache-demo', name='cache-east', source='config file', field='name',
                                     problem=msg('instances.invalid.reserved', name='cache-east'))


def test_a_source_instance_with_a_shadow_name_is_skipped_with_one_warning(db, caplog):
    # given
    source = SourceData('file', {}, ({'plugin': 'cache-demo', 'name': 'cache-east'}, {'plugin': 'cache-demo', 'name': 'cached'}))
    instances = Instances(db, Registry.of(CACHE, directory=HERE), lambda: Sealer('k'), sources=lambda: [source])

    # when
    with caplog.at_level(logging.WARNING, logger='skopax.instances'):
        instances.refresh()
        instances.refresh()

    # then
    assert [i.name for i in instances.all('cache-demo')] == ['cached']
    assert [r.getMessage() for r in caplog.records] == [msg('instances.source_instance_skipped', label='file', plugin='cache-demo', error='InvalidInstance')]
    assert 'cache-east' not in caplog.text


def test_a_camel_case_section_loads_and_other_shapes_are_refused():
    # when
    found = {name: Registry.of(sectioned(name), directory=HERE).get('logger') for name in ('logTop', 'grafanaAlerts', 'LogTop', 'log_top', 'serverPasses', 'archiveHours')}

    # then
    assert [(found[n].state, found[n].reason) for n in ('logTop', 'grafanaAlerts')] == [('ok', None), ('ok', None)]
    for name in ('LogTop', 'log_top'):
        assert found[name].reason == 'Logger was not loaded: ' + msg('plugins.why.section_name', id='logs', name=name)
    for name in ('serverPasses', 'archiveHours'):
        assert found[name].reason == 'Logger was not loaded: ' + msg('plugins.why.section_core', id='logs', name=name)


def test_a_camel_case_section_runs_through_a_sweep_and_reads_back_unchanged(store, tmp_path):
    # given
    with TestClient(create_app(settings(), store, registry=Registry.of(sectioned('logTop'), directory=HERE), kube=Kube(root=tmp_path))) as c:

        # when
        c.app.state.sweeper.run('logger', 'logs', NOW)
        stored = store.load()
        served = c.get('/api/state').json()

    # then
    assert stored['sources']['logs'] == 'ok'
    assert stored['logTop'] == [dict(row, plugin='logger') for row in LOG_ROWS]
    assert served['logTop'] == stored['logTop']


def test_plugin_settings_cannot_take_a_core_tunable_id():
    # given
    core = settingsources.core_setting_ids()
    taken = slack_like(settings=(Setting('remind-hours', 'Remind', 'Hours', 'float', '4'),))
    free = slack_like(settings=(Setting('other-key', 'Other', 'Text', 'text', ''),))

    # when
    broken = Registry.of(taken, directory=HERE, core_settings=core).get('slack')
    fine = Registry.of(free, directory=HERE, core_settings=core).get('slack')

    # then
    assert 'slack.remind-hours' in core and 'slack.other-key' not in core
    assert (broken.state, broken.reason) == ('broken', 'Slack was not loaded: ' + msg('plugins.why.setting_taken', key='slack.remind-hours'))
    assert 'slack.remind-hours' in broken.reason
    assert (fine.state, fine.reason) == ('ok', None)


def test_a_rule_set_cannot_take_a_core_tunable_id():
    # given
    ruled = slack_like(rules=(RuleSet('ask-cap', 'Asks', {'warn': 1.0}),))

    # when
    broken = Registry.of(ruled, directory=HERE, core_settings=settingsources.core_setting_ids()).get('slack')
    demo = Registry.of(DEMO, core_settings=frozenset({'demo.limits'})).get('demo')

    # then
    assert broken.reason == 'Slack was not loaded: ' + msg('plugins.why.setting_taken', key='slack.ask-cap')
    assert (demo.state, demo.reason) == ('broken', 'Demo was not loaded: ' + msg('plugins.why.setting_taken', key='demo.limits'))


def test_both_load_sites_pass_the_core_setting_ids(store, tmp_path, monkeypatch):
    # given
    monkeypatch.setattr(slack_plugin, 'PLUGIN', dataclasses.replace(slack_plugin.PLUGIN, settings=(Setting('remind-hours', 'Remind', 'Hours', 'float', '4'),)))
    expected = ('broken', 'Slack was not loaded: ' + msg('plugins.why.setting_taken', key='slack.remind-hours'))

    # when
    booted = boot({FILE_ENV: str(tmp_path / 'absent.yaml')}).registry.get('slack')
    served = create_app(settings(), store).state.registry.get('slack')

    # then
    assert (booted.state, booted.reason) == expected
    assert (served.state, served.reason) == expected


def test_a_builtin_wins_a_duplicate_id():
    # when
    reg = Registry.load({'SKOPAX_PLUGINS': 'tests.plugins.demo_plugin'}, builtin=('tests.plugins.demo_twin',))
    copies = [(x.origin, x.plugin.name, x.state, x.reason) for x in reg.loaded() if x.id == 'demo']

    # then
    assert copies == [('builtin', 'Demo twin', 'ok', None), ('env', 'Demo', 'broken', 'Demo was not loaded: id demo is already loaded (the copy that wins is built in)')]
    assert reg.get('demo').plugin is demo_twin.PLUGIN


def test_the_duplicate_reason_names_the_winning_copy_in_plain_words(tmp_path, monkeypatch):
    # given
    shutil.copy(HERE / 'demo-icon.svg', tmp_path / 'demo-icon.svg')
    (tmp_path / 'twin_package.py').write_text("import dataclasses\nfrom tests.plugins import demo_plugin\n\nPLUGIN = dataclasses.replace(demo_plugin.PLUGIN, name='Demo package')\n", encoding='utf-8')
    info = tmp_path / 'twin_package-0.1.dist-info'
    info.mkdir()
    (info / 'METADATA').write_text('Metadata-Version: 2.1\nName: twin-package\nVersion: 0.1\n', encoding='utf-8')
    (info / 'entry_points.txt').write_text('[skopax.plugins]\ntwin-package = twin_package:PLUGIN\n', encoding='utf-8')
    monkeypatch.syspath_prepend(str(tmp_path))

    # when
    packaged = Registry.load({'SKOPAX_PLUGINS': 'tests.plugins.demo_plugin'}, builtin=())
    listed = Registry.load({'SKOPAX_PLUGINS': 'tests.plugins.demo_twin,tests.plugins.demo_plugin'}, builtin=(), entry_points=False)

    # then
    assert [(x.origin, x.state, x.reason) for x in packaged.loaded() if x.id == 'demo'] == [
        ('entry-point', 'ok', None), ('env', 'broken', 'Demo was not loaded: id demo is already loaded (the copy that wins is an installed package)')]
    assert [(x.plugin.name, x.state, x.reason) for x in listed.loaded()] == [
        ('Demo twin', 'ok', None), ('Demo', 'broken', 'Demo was not loaded: id demo is already loaded (the copy that wins is from SKOPAX_PLUGINS)')]


def test_no_data_is_a_reserved_instance_name_for_every_plugin(db):
    # given
    instances = instances_of(db, CACHE, DEMO)
    refused = {}

    # when
    for plugin, values in (('demo', {'url': 'https://demo.example.com'}), ('cache-demo', {})):
        for name in ('no-data', 'no-data-east'):
            with pytest.raises(InvalidInstance) as caught:
                instances.create(plugin, name, values, True, 'alice')
            refused[(plugin, name)] = (caught.value.field, caught.value.text)
    kept = instances.create('demo', 'no-datas', {'url': 'https://demo.example.com'}, True, 'alice')

    # then
    assert 'no-data' in instances.registry.reserved_names()
    assert refused == {(p, n): ('name', msg('instances.invalid.reserved', name=n)) for p in ('demo', 'cache-demo') for n in ('no-data', 'no-data-east')}
    assert kept.name == 'no-datas'
