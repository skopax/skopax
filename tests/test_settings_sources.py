import dataclasses
import logging
import subprocess
import sys
import textwrap
import threading
import time
import traceback
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from skopax import config
from skopax.app import create_app
from skopax.config import ConfigurationError, settings_env_names
from skopax.contract import Hooks, Setting, SourceData
from skopax.instances import Instances
from skopax.kube import Kube
from skopax.messages import msg
from skopax.registry import Registry
from skopax.sealer import Sealer
from skopax.tunables import Invalid, Live, TunableStore
from skopax.settingsources import DEFAULT_FILE, FILE_ENV, Follower, SourceSet, boot, env_source, file_source, hook_sources, light_boot, merged
from tests.conftest import settings
from tests.plugin_kit import registry_with
from tests.plugins import demo_plugin, enterprise_plugin

ROOT = Path(__file__).resolve().parent.parent
DEMO = demo_plugin.PLUGIN
HERE = Path(demo_plugin.__file__).parent
MAIN = {'DEMO_URL': 'https://demo.example.com'}
ALICE = 'alice@example.com'


def write(tmp_path, text: str) -> str:
    path = tmp_path / f'skopax-{len(list(tmp_path.glob("skopax-*.yaml")))}.yaml'
    path.write_text(textwrap.dedent(text), encoding='utf-8')
    return str(path)


@pytest.fixture(autouse=True)
def clean_source(monkeypatch):
    monkeypatch.setattr(demo_plugin, 'SOURCE', {})


def entries_of(live) -> dict:
    return {e['key']: e for e in live.entries()}


def test_boot_order_env_then_file_then_hooks_then_admin(store, tmp_path, monkeypatch):
    # given
    path = write(tmp_path, 'settings:\n  core.timezone: Europe/Berlin\n')
    monkeypatch.setattr(demo_plugin, 'SOURCE', {'values': {'core.timezone': 'Asia/Tokyo'}})
    env = {'SKOPAX_TIMEZONE': 'UTC', FILE_ENV: path}
    only_env = boot({'SKOPAX_TIMEZONE': 'UTC'}, Registry.of())
    only_file = boot({'SKOPAX_TIMEZONE': 'UTC', FILE_ENV: path}, Registry.of())

    # when
    b = boot(env, registry_with(demo_plugin))
    with TestClient(create_app(b.settings, store, registry=b.registry, sources=b.source_set(), kube=Kube(root=tmp_path))) as c:
        live = c.app.state.runtime.live
        before = entries_of(live)['general.timezone']
        live.set('general.timezone', 'America/New_York', ALICE)
        admin = entries_of(live)['general.timezone']
        live.set('general.timezone', None, ALICE)
        reset = entries_of(live)['general.timezone']

    # then
    assert (only_env.settings.timezone, only_file.settings.timezone, b.settings.timezone) == ('UTC', 'Europe/Berlin', 'Asia/Tokyo')
    assert (before['source'], before['value'], before['from']) == ('config', 'Asia/Tokyo', 'demo source')
    assert (admin['source'], admin['value'], admin['base']) == ('admin', 'America/New_York', 'Asia/Tokyo')
    assert (reset['source'], reset['from'], reset['value']) == ('config', 'demo source', 'Asia/Tokyo')


def test_mongo_uri_and_secret_key_can_come_from_a_hook():
    # given
    seen = []

    def spy(env):
        seen.append(dict(env))
        return {'label': 'vault', 'values': {'mongo.uri': 'mongodb://db.example.com:27017/shop', 'core.secret-key': 'k'}}

    plugin = dataclasses.replace(DEMO, hooks=Hooks(settings_source=spy))
    env = {'CONFIG_URL': 'https://config.example.com'}

    # when
    b = boot(env, Registry.of(plugin, directory=HERE))

    # then
    assert (b.settings.mongo_uri, b.settings.mongo_db, b.settings.secret_key) == ('mongodb://db.example.com:27017/shop', 'shop', 'k')
    assert seen == [env], 'the hook received the environment and nothing else'


def test_file_source_reads_yaml_with_placeholders(tmp_path):
    # given
    path = write(tmp_path, '''
        settings:
          core.timezone: Europe/Berlin
          investigate.max-cards: 7
          core.postdeploy: false
        plugins:
          demo:
            instances:
              - name: main
                primary: true
                url: ${DEMO_HOST}
                token: ${DEMO_TOKEN}
                mode: ${DEMO_MODE:slow}
    ''')

    # when
    data = file_source(path, {'DEMO_TOKEN': 'abc', 'DEMO_HOST': 'https://demo.example.com'})
    missing = write(tmp_path, 'plugins:\n  demo:\n    instances:\n      - name: main\n        token: ${NOT_SET}\n')

    # then
    assert data.label == 'config file'
    assert dict(data.values) == {'core.timezone': 'Europe/Berlin', 'investigate.max-cards': '7', 'core.postdeploy': 'false'}
    assert [dict(i) for i in data.instances] == [{'plugin': 'demo', 'name': 'main', 'primary': 'true', 'url': 'https://demo.example.com', 'token': 'abc', 'mode': 'slow'}]
    with pytest.raises(ConfigurationError, match='NOT_SET'):
        file_source(missing, {})


def test_missing_file_is_no_layer_and_a_broken_one_stops_boot(tmp_path):
    # given
    absent = str(tmp_path / 'none.yaml')
    broken = write(tmp_path, 'settings: [unclosed\n')
    unknown = write(tmp_path, 'surprise: 1\n')
    listy = write(tmp_path, '- a\n- b\n')
    nested = write(tmp_path, 'settings:\n  core.timezone:\n    deep: 1\n')

    # when
    b = boot({FILE_ENV: absent}, Registry.of())

    # then
    assert file_source(absent, {}) is None
    assert [s.label for s in b.sources] == ['environment']
    assert DEFAULT_FILE == '/etc/skopax/skopax.yaml'
    for path, word in ((broken, 'YAML'), (unknown, 'surprise'), (listy, 'mapping'), (nested, 'core.timezone')):
        with pytest.raises(ConfigurationError, match=word) as e:
            boot({FILE_ENV: path}, Registry.of())
        assert path in str(e.value)


def test_a_broken_file_error_never_quotes_the_file(tmp_path):
    # given
    path = write(tmp_path, 'settings:\n  core.token: hunter2-value\n  bad: [oops\n')

    # when
    with pytest.raises(ConfigurationError) as e:
        file_source(path, {})

    # then
    assert 'hunter2-value' not in str(e.value)


def test_the_config_file_name_is_a_known_env_name():
    # then
    assert FILE_ENV == 'SKOPAX_CONFIG_FILE'
    assert FILE_ENV in settings_env_names()
    assert FILE_ENV in config.BOOT_ENV


def test_env_source_lists_the_instances_of_usable_plugins():
    # when
    data = env_source(MAIN, registry_with(demo_plugin))
    none = env_source({}, registry_with(demo_plugin))

    # then
    assert data.label == 'environment' and dict(data.values) == {}
    assert [dict(i) for i in data.instances] == [{'plugin': 'demo', 'name': 'main', 'url': 'https://demo.example.com'}]
    assert none.instances == ()


def test_hook_sources_run_usable_plugins_in_load_order_and_name_a_failing_one():
    # given
    def boom(env):
        raise RuntimeError('offline')

    failing = dataclasses.replace(DEMO, hooks=Hooks(settings_source=boom))

    # when
    found = hook_sources(registry_with(demo_plugin), {})

    # then
    assert [s.label for s in found] == ['demo source']
    assert merged([SourceData('a', {'x': '1', 'y': '1'}), SourceData('b', {'x': '2'})]) == {'x': '2', 'y': '1'}
    with pytest.raises(ConfigurationError, match='Demo') as e:
        hook_sources(Registry.of(failing, directory=HERE), {})
    assert 'RuntimeError' in str(e.value) and 'offline' not in str(e.value)


def feed(*steps):
    queue = list(steps)

    def load():
        step = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(step, Exception):
            raise step
        return step

    return load


def test_sources_are_live_and_keep_the_last_good(store, caplog):
    # given
    v1 = SourceData('feed', {}, ({'plugin': 'demo', 'name': 'main', 'url': 'https://one.example.com'},))
    v2 = SourceData('feed', {}, ({'plugin': 'demo', 'name': 'main', 'url': 'https://two.example.com'},))
    sources = SourceSet([('feed', feed(RuntimeError('config service down'), RuntimeError('config service down'), v2))], {'feed': v1})
    registry = registry_with(demo_plugin)
    instances = Instances(store.db, registry, lambda: Sealer('k'), sources=sources.current)
    instances.refresh()
    seen = [instances.get('demo', 'main').values['url']]

    # when
    with caplog.at_level(logging.WARNING, logger='skopax'):
        first = sources.refresh()
        second = sources.refresh()
        instances.refresh()
        seen.append(instances.get('demo', 'main').values['url'])
        failing = sources.status()['feed']
        third = sources.refresh()
        recovered = sources.status()['feed']
        instances.refresh()
        seen.append(instances.get('demo', 'main').values['url'])

    # then
    assert seen == ['https://one.example.com'] * 2 + ['https://two.example.com']
    assert first is False and second is False and third is True
    assert sources.current() == [v2]
    assert len([r for r in caplog.records if 'feed' in r.getMessage() and r.levelno == logging.WARNING]) == 1, 'a failing source is logged once until it recovers'
    assert failing['ok'] is False and failing['error'] == 'RuntimeError' and failing['since']
    assert recovered['ok'] is True and recovered['error'] is None


def test_a_loader_that_returns_none_has_no_layer():
    # given
    sources = SourceSet([('file', feed(SourceData('file', {'a': '1'}))), ('env', feed(SourceData('env', {'b': '2'})))], {'file': None, 'env': SourceData('env', {'b': '2'})})

    # when
    before = sources.current()
    changed = sources.refresh()

    # then
    assert [s.label for s in before] == ['env']
    assert changed is True and [s.label for s in sources.current()] == ['file', 'env']


def test_a_bad_instance_from_a_hook_is_parked_not_fatal(store, tmp_path, monkeypatch):
    # given
    monkeypatch.setattr(demo_plugin, 'SOURCE', {'instances': [{'plugin': 'demo', 'name': 'east', 'url': 'ftp://x'}]})

    # when
    b = boot(MAIN, registry_with(demo_plugin))
    with TestClient(create_app(b.settings, store, registry=b.registry, sources=b.source_set(), kube=Kube(root=tmp_path))) as c:
        runtime = c.app.state.runtime
        east = runtime.instances.get('demo', 'east')
        running = [i.name for i in runtime.instances_of('demo')]

    # then
    assert east is not None and 'URL' in east.problem
    assert running == ['main']


def test_env_and_file_instances_with_bad_fields_stop_boot(tmp_path):
    # given
    bad_file = write(tmp_path, 'plugins:\n  demo:\n    instances:\n      - name: east\n        url: ftp://x\n')
    bad_name = write(tmp_path, 'plugins:\n  demo:\n    instances:\n      - name: Bad_Name\n        url: https://demo.example.com\n')
    unknown_field = write(tmp_path, 'plugins:\n  demo:\n    instances:\n      - name: east\n        url: https://demo.example.com\n        color: red\n')
    registry = registry_with(demo_plugin)

    # when
    with pytest.raises(ConfigurationError) as env_error:
        boot({'DEMO_URL': 'ftp://x'}, registry)

    # then
    assert all(word in str(env_error.value) for word in ('demo', 'main', 'url', 'environment'))
    for path, name, field in ((bad_file, 'east', 'url'), (bad_name, 'Bad_Name', 'name'), (unknown_field, 'east', 'color')):
        with pytest.raises(ConfigurationError) as e:
            boot({FILE_ENV: path}, registry)
        assert all(word in str(e.value) for word in ('demo', name, field, 'config file'))


def test_plugin_settings_and_rules_join_the_admin_settings(store, tmp_path, monkeypatch):
    # given
    monkeypatch.setattr(demo_plugin, 'SOURCE', {'values': {'demo.depth': '7'}})
    b = boot(MAIN, registry_with(demo_plugin))

    # when
    with TestClient(create_app(b.settings, store, registry=b.registry, sources=b.source_set(), kube=Kube(root=tmp_path))) as c:
        runtime = c.app.state.runtime
        live = runtime.live
        listed = entries_of(live)
        from_source = (live.plugin_value('demo.depth'), listed['demo.depth']['source'], listed['demo.depth']['from'])
        live.set('demo.depth', '5', ALICE)
        by_admin = (live.plugin_value('demo.depth'), runtime.setting('demo', 'depth'), entries_of(live)['demo.depth']['source'])
        live.set('demo.depth', None, ALICE)
        live.set('demo.limits', '{"warn": 7}', ALICE)
        rules = (live.rules_of('demo.limits'), runtime.rules('demo', 'limits'), entries_of(live)['demo.limits'])
        runtime.state.set('demo', False, ALICE)
        hidden = entries_of(live)
        runtime.state.set('demo', True, ALICE)
        shown = entries_of(live)

    # then
    assert {'demo.depth', 'demo.limits'} <= set(listed) and 'general.timezone' in listed
    assert (listed['demo.depth']['group'], listed['demo.depth']['kind'], listed['demo.depth']['low'], listed['demo.depth']['high']) == ('Demo', 'int', 1, 10)
    assert from_source == (7, 'config', 'demo source')
    assert by_admin == (5, 5, 'admin')
    assert rules[0] == rules[1] == {'warn': 7, 'crit': 10.0}
    assert rules[2]['kind'] == 'rules' and rules[2]['defaults'] == {'warn': 5.0, 'crit': 10.0} and rules[2]['ruleHelp'] == {'warn': 'Warn at this level'}
    assert not {'demo.depth', 'demo.limits'} & set(hidden) and 'general.timezone' in hidden
    assert {'demo.depth', 'demo.limits'} <= set(shown)


def test_a_plugin_setting_out_of_range_is_refused_and_a_bad_source_value_falls_back(store, tmp_path, monkeypatch):
    # given
    monkeypatch.setattr(demo_plugin, 'SOURCE', {'values': {'demo.depth': '99'}})
    b = boot(MAIN, registry_with(demo_plugin))

    # when
    with TestClient(create_app(b.settings, store, registry=b.registry, sources=b.source_set(), kube=Kube(root=tmp_path))) as c:
        live = c.app.state.runtime.live
        fallback = live.plugin_value('demo.depth')
        with pytest.raises(Invalid):
            live.set('demo.depth', '11', ALICE)
        with pytest.raises(KeyError):
            live.plugin_value('demo.nothing')

    # then
    assert fallback == 3


def test_an_over_long_org_about_from_a_settings_source_is_refused_for_that_field(store, tmp_path, monkeypatch, caplog):
    # given
    monkeypatch.setattr(demo_plugin, 'SOURCE', {'values': {'core.org-about': 'y' * 2001, 'core.timezone': 'Asia/Tokyo'}})
    first = SourceData('feed', {'core.org-about': 'We run a shop.'})
    second = SourceData('feed', {'core.org-about': 'z' * 2001, 'investigate.max-cards': '7'})
    box = {'data': first}
    sources = SourceSet([('feed', lambda: box['data'])], {'feed': first})

    # when
    with caplog.at_level(logging.WARNING, logger='skopax.config'):
        b = boot({**MAIN, 'SKOPAX_ORG_ABOUT': 'We sell books.'}, registry_with(demo_plugin))
        with TestClient(create_app(settings(), store, sources=sources, registry=Registry.of(), kube=Kube(root=tmp_path))) as c:
            runtime = c.app.state.runtime
            follower = Follower(sources, runtime.live, runtime.instances, {})
            box['data'] = second
            follower.tick()
            followed = runtime.live.current()
    warned = [r.getMessage() for r in caplog.records if r.name == 'skopax.config']

    # then
    assert (b.settings.org_about, b.settings.timezone) == ('We sell books.', 'Asia/Tokyo')
    assert (followed.org_about, followed.investigate_max_cards) == ('', 7)
    assert warned == [msg('settings.source_too_long', key='core.org-about', limit=2000)] * 2
    assert 'yyy' not in caplog.text and 'zzz' not in caplog.text


def test_admin_names_where_the_org_about_in_use_comes_from_when_a_source_value_is_refused(store, tmp_path, monkeypatch):
    # given
    monkeypatch.setattr(demo_plugin, 'SOURCE', {'values': {'core.org-about': 'y' * 2001, 'core.timezone': 'Asia/Tokyo'}})
    b = boot({**MAIN, 'SKOPAX_ORG_ABOUT': 'We sell books.'}, registry_with(demo_plugin))

    # when
    with TestClient(create_app(b.settings, store, registry=b.registry, sources=b.source_set(), kube=Kube(root=tmp_path))) as c:
        rows = entries_of(c.app.state.runtime.live)

    # then
    assert (rows['core.org-about']['value'], rows['core.org-about']['source'], rows['core.org-about']['from']) == ('We sell books.', 'config', 'environment')
    assert (rows['general.timezone']['value'], rows['general.timezone']['from']) == ('Asia/Tokyo', 'demo source'), 'a value the source still sets keeps its label'


def test_admin_measures_the_resolved_org_about_of_a_source_when_it_names_where_the_value_comes_from(store, tmp_path, monkeypatch):
    # given
    monkeypatch.setenv('ORG_TEXT', 'y' * 2001)
    monkeypatch.setattr(demo_plugin, 'SOURCE', {'values': {'core.org-about': '${ORG_TEXT}'}})
    b = boot({**MAIN, 'SKOPAX_ORG_ABOUT': 'We sell books.'}, registry_with(demo_plugin))

    # when
    with TestClient(create_app(b.settings, store, registry=b.registry, sources=b.source_set(), kube=Kube(root=tmp_path))) as c:
        row = entries_of(c.app.state.runtime.live)['core.org-about']

    # then
    assert b.settings.org_about == 'We sell books.'
    assert (row['value'], row['source'], row['from']) == ('We sell books.', 'config', 'environment')


def test_tunables_import_has_no_side_effects():
    # given
    code = ('import sys; sys.path.insert(0, sys.argv[1])\n'
            'import skopax.config as c\n'
            'def boom(*a, **k): raise RuntimeError("load_settings ran at import")\n'
            'c.load_settings = boom\n'
            'import skopax.tunables as t\n'
            'print("ok")\n')

    # when
    done = subprocess.run([sys.executable, '-I', '-c', code, str(ROOT)], capture_output=True, text=True, timeout=120)

    # then
    assert done.returncode == 0, done.stderr[-800:]
    assert done.stdout.strip() == 'ok'


def test_the_fallback_and_defaults_are_built_on_first_use():
    # given
    from skopax import tunables

    # when
    first, again = tunables.fallback(), tunables.fallback()

    # then
    assert first is again
    assert tunables.defaults()['timezone'] == 'UTC' and set(tunables.defaults()) == {f.name for f in dataclasses.fields(type(first))}


def test_non_live_value_change_logs_restart_once(store, tmp_path, caplog):
    # given
    first = SourceData('feed', {'investigate.max-cards': '4'})
    second = SourceData('feed', {'investigate.max-cards': '9', 'investigate.mode': 'live', 'slack.listen': 'live', 'mongo.uri': 'mongodb://secret-host.example.com/db'})
    box = {'data': first}
    sources = SourceSet([('feed', lambda: box['data'])], {'feed': first})
    with TestClient(create_app(settings(), store, sources=sources, registry=Registry.of(), kube=Kube(root=tmp_path))) as c:
        runtime = c.app.state.runtime
        follower = Follower(sources, runtime.live, runtime.instances, {})
        box['data'] = second

        # when
        with caplog.at_level(logging.INFO, logger='skopax'):
            follower.tick()
            follower.tick()
            box['data'] = dataclasses.replace(second, values={**second.values, 'investigate.max-cards': '8'})
            follower.tick()
        entry = entries_of(runtime.live)['investigate.max-cards']
        notes = [r.getMessage() for r in caplog.records if 'restarts' in r.getMessage()]

    # then
    assert sorted(n.split(' changed')[0] for n in notes) == ['mongo.uri', 'slack.listen']
    assert all('feed' in n and 'secret-host' not in n for n in notes)
    assert runtime.live.current().investigate_max_cards == 8 and runtime.live.current().slack_listen == 'off'
    assert runtime.live.current().investigate_mode == 'live', 'the mode is live since P4-R23, so a source change applies without a restart'
    assert (entry['source'], entry['from'], entry['value']) == ('config', 'feed', '8')


def test_a_changed_instance_reaches_the_cache_through_the_follower(store, tmp_path):
    # given
    one = SourceData('feed', {}, ({'plugin': 'demo', 'name': 'main', 'url': 'https://one.example.com'},))
    two = SourceData('feed', {}, ({'plugin': 'demo', 'name': 'main', 'url': 'https://two.example.com'},))
    box = {'data': one}
    sources = SourceSet([('feed', lambda: box['data'])], {'feed': one})
    with TestClient(create_app(settings(), store, sources=sources, registry=registry_with(demo_plugin), kube=Kube(root=tmp_path))) as c:
        runtime = c.app.state.runtime
        before = runtime.instances.get('demo', 'main').values['url']
        box['data'] = two

        # when
        Follower(sources, runtime.live, runtime.instances, {}).tick()
        after = runtime.instances.get('demo', 'main').values['url']

    # then
    assert (before, after) == ('https://one.example.com', 'https://two.example.com')


SENTINEL = 'https://user:SENTINEL-4471@vault.example.com/x'


def leaky(env):
    raise RuntimeError(SENTINEL)


def shown(error: BaseException) -> str:
    return ''.join(traceback.format_exception(error))


def test_a_failing_source_never_leaks_its_exception_text(store, caplog):
    # given
    plugin = dataclasses.replace(DEMO, hooks=Hooks(settings_source=leaky), env_instances=leaky)
    registry = Registry.of(plugin, directory=HERE)
    bad = SourceSet([('feed', lambda: leaky({}))], {'feed': None})

    # when
    with caplog.at_level(logging.DEBUG):
        with pytest.raises(ConfigurationError) as hook_error:
            hook_sources(registry, {})
        with pytest.raises(ConfigurationError) as env_error:
            env_source({}, registry)
        with pytest.raises(ConfigurationError) as boot_error:
            boot({}, registry)
        bad.refresh()
        broken = Instances(store.db, registry, lambda: Sealer('k'), sources=lambda: leaky({}))
        broken.refresh()

    # then
    for text in (shown(hook_error.value), shown(env_error.value), shown(boot_error.value), str(bad.status()), caplog.text):
        assert 'SENTINEL' not in text
    assert 'RuntimeError' in str(hook_error.value) and 'Demo' in str(hook_error.value)
    assert bad.status()['feed']['error'] == 'RuntimeError'
    assert 'feed' in caplog.text


def test_a_raising_hook_stops_boot():
    # given
    def boom(env):
        raise RuntimeError('offline')

    plugin = dataclasses.replace(DEMO, hooks=Hooks(settings_source=boom))

    # when
    with pytest.raises(ConfigurationError, match='Demo'):
        boot({}, Registry.of(plugin, directory=HERE))


def test_a_hanging_hook_stops_boot_within_its_bound_naming_the_plugin():
    # given
    release = threading.Event()

    def hangs(env):
        release.wait(3)
        return {}

    plugin = dataclasses.replace(DEMO, hooks=Hooks(settings_source=hangs))

    # when
    began = time.monotonic()
    with pytest.raises(ConfigurationError) as stopped:
        boot({}, Registry.of(plugin, directory=HERE), timeout=0.1)
    took = time.monotonic() - began
    release.set()

    # then
    assert str(stopped.value) == msg('settings.hook.failed', plugin='Demo', error='TimedOut')
    assert took < 1


def test_a_hanging_env_instances_hook_stops_boot_within_its_bound_naming_the_plugin():
    # given
    release = threading.Event()

    def hangs(env):
        release.wait(3)
        return []

    plugin = dataclasses.replace(DEMO, env_instances=hangs)

    # when
    began = time.monotonic()
    with pytest.raises(ConfigurationError) as stopped:
        boot({}, Registry.of(plugin, directory=HERE), timeout=0.1)
    took = time.monotonic() - began
    release.set()

    # then
    assert str(stopped.value) == msg('settings.hook.failed', plugin='Demo', error='TimedOut')
    assert took < 1


def test_a_hanging_hook_leaves_the_light_boot_within_its_bound_and_is_tried_again_later():
    # given
    release = threading.Event()

    def hangs(env):
        release.wait(3)
        return {}

    plugin = dataclasses.replace(DEMO, hooks=Hooks(settings_source=hangs))

    # when
    began = time.monotonic()
    light = light_boot({}, Registry.of(plugin, directory=HERE), timeout=0.1)
    took = time.monotonic() - began
    release.set()

    # then
    assert light.failed == {'Demo': 'TimedOut'} and light.loaded['Demo'] is None
    assert took < 1


def test_source_owned_secrets_stay_out_of_errors_and_logs(store, tmp_path, caplog):
    # given
    token = 'tok-SECRET-9921'
    bad = write(tmp_path, f'plugins:\n  demo:\n    instances:\n      - name: east\n        url: ftp://x\n        token: {token}\n')
    good = write(tmp_path, f'plugins:\n  demo:\n    instances:\n      - name: east\n        url: https://demo.example.com\n        token: {token}\n')

    # when
    with caplog.at_level(logging.DEBUG):
        with pytest.raises(ConfigurationError) as e:
            boot({FILE_ENV: bad}, registry_with(demo_plugin))
        b = boot({FILE_ENV: good}, registry_with(demo_plugin))
        with TestClient(create_app(b.settings, store, registry=b.registry, sources=b.source_set(), kube=Kube(root=tmp_path))) as c:
            found = c.app.state.runtime.instances.get('demo', 'east')

    # then
    assert token not in shown(e.value) and token not in caplog.text
    assert found.secrets == {'token': token} and token not in repr(found)


def test_a_loader_label_mismatch_is_refused():
    # given
    data = SourceData('feed', {})
    loaders = [('feed', lambda: data)]

    # then
    with pytest.raises(ValueError):
        SourceSet(loaders, {'other': data})
    with pytest.raises(ValueError):
        SourceSet(loaders, {})
    with pytest.raises(ValueError):
        SourceSet(loaders + [('feed', lambda: data)], {'feed': data})


def test_the_follower_keeps_no_record_of_secret_values(store, tmp_path):
    # given
    sources = SourceSet([], {})
    with TestClient(create_app(settings(), store, sources=sources, registry=Registry.of(), kube=Kube(root=tmp_path))) as c:
        runtime = c.app.state.runtime

        # then
        assert not hasattr(Follower(sources, runtime.live, runtime.instances, {}), '_noted')


def test_an_unregistered_plugin_in_the_file_stops_boot_and_a_locked_one_is_skipped(tmp_path, caplog):
    # given
    ghost = write(tmp_path, 'plugins:\n  ghost:\n    instances:\n      - name: main\n        url: https://x.example.com\n')
    locked = write(tmp_path, f'plugins:\n  {enterprise_plugin.PLUGIN.id}:\n    instances:\n      - name: main\n        url: https://x.example.com\n')
    registry = registry_with(demo_plugin, enterprise_plugin)

    # when
    with pytest.raises(ConfigurationError) as e:
        boot({FILE_ENV: ghost}, registry)
    with caplog.at_level(logging.WARNING):
        b = boot({FILE_ENV: locked}, registry)

    # then
    assert 'ghost' in str(e.value) and 'config file' in str(e.value)
    assert b.settings is not None
    assert len([r for r in caplog.records if enterprise_plugin.PLUGIN.id in r.getMessage()]) == 1


def test_create_app_boots_from_the_registry_it_was_given(store, tmp_path, monkeypatch):
    # given
    monkeypatch.setenv('DEMO_URL', 'https://demo.example.com')

    # when
    with TestClient(create_app(None, store, registry=registry_with(demo_plugin), kube=Kube(root=tmp_path))) as c:
        found = c.app.state.runtime.instances.get('demo', 'main')

    # then
    assert found is not None and found.values['url'] == 'https://demo.example.com'


def test_rebase_recomputes_the_values_even_when_the_revision_cannot_be_read(store):
    # given
    tunables = TunableStore(store.db)
    live = Live(settings(), tunables)
    live.current()

    def down():
        raise RuntimeError('mongo down')

    tunables.rev = down

    # when
    live.rebase(dataclasses.replace(live.base, investigate_max_cards=9), {}, {})

    # then
    assert live.current().investigate_max_cards == 9


def test_a_bad_source_value_warns_once_per_value(store, tmp_path, monkeypatch, caplog):
    # given
    monkeypatch.setattr(demo_plugin, 'SOURCE', {'values': {'demo.depth': '99'}})
    b = boot(MAIN, registry_with(demo_plugin))

    # when
    with TestClient(create_app(b.settings, store, registry=b.registry, sources=b.source_set(), kube=Kube(root=tmp_path))) as c:
        live = c.app.state.runtime.live
        with caplog.at_level(logging.WARNING):
            caplog.clear()
            for _ in range(4):
                live.plugin_value('demo.depth')
            live.rebase(live.base, {'demo.depth': '98'}, {})
            live.plugin_value('demo.depth')

    # then
    assert len([r for r in caplog.records if 'demo.depth' in r.getMessage() and 'ignored' in r.getMessage()]) == 2


def test_other_plugin_setting_kinds_round_trip_through_sources_and_admin(store, tmp_path, monkeypatch):
    # given
    extra = (Setting('fast', 'Fast', 'Go fast', 'bool', 'false'), Setting('pace', 'Pace', 'How quick', 'choice', 'slow', choices=('slow', 'quick')),
             Setting('ratio', 'Ratio', 'A share', 'float', '0.5', low=0, high=1), Setting('names', 'Names', 'A list', 'list', 'a, b'))
    plugin = dataclasses.replace(DEMO, settings=DEMO.settings + extra)
    monkeypatch.setattr(demo_plugin, 'SOURCE', {'values': {'demo.fast': 'true', 'demo.pace': 'quick', 'demo.ratio': '0.25', 'demo.names': 'x, y'}})
    b = boot(MAIN, Registry.of(plugin, directory=HERE))

    # when
    with TestClient(create_app(b.settings, store, registry=b.registry, sources=b.source_set(), kube=Kube(root=tmp_path))) as c:
        live = c.app.state.runtime.live
        from_source = [live.plugin_value(f'demo.{k}') for k in ('fast', 'pace', 'ratio', 'names')]
        live.set('demo.fast', 'false', ALICE)
        live.set('demo.pace', 'slow', ALICE)
        live.set('demo.ratio', '0.75', ALICE)
        live.set('demo.names', 'p, q, r', ALICE)
        by_admin = [live.plugin_value(f'demo.{k}') for k in ('fast', 'pace', 'ratio', 'names')]
        entry = entries_of(live)['demo.names']
        with pytest.raises(Invalid):
            live.set('demo.ratio', '2', ALICE)
        with pytest.raises(Invalid):
            live.set('demo.pace', 'sideways', ALICE)

    # then
    assert from_source == [True, 'quick', 0.25, ('x', 'y')]
    assert by_admin == [False, 'slow', 0.75, ('p', 'q', 'r')]
    assert (entry['value'], entry['base'], entry['source']) == ('p, q, r', 'x, y', 'admin')


def test_two_instances_of_a_locked_plugin_in_one_file_warn_once(tmp_path, caplog):
    # given
    locked_id = enterprise_plugin.PLUGIN.id
    path = write(tmp_path, f'plugins:\n  {locked_id}:\n    instances:\n      - name: one\n        url: https://x.example.com\n      - name: two\n        url: https://y.example.com\n')

    # when
    with caplog.at_level(logging.WARNING):
        boot({FILE_ENV: path}, registry_with(demo_plugin, enterprise_plugin))

    # then
    assert len([r for r in caplog.records if locked_id in r.getMessage()]) == 1


def test_a_failing_follow_logs_the_exception_type_and_never_its_text(store, tmp_path, caplog):
    # given
    first = SourceData('feed', {'investigate.max-cards': '4'})
    second = SourceData('feed', {'investigate.max-cards': '5'})
    box = {'data': first}
    sources = SourceSet([('feed', lambda: box['data'])], {'feed': first})
    with TestClient(create_app(settings(), store, sources=sources, registry=Registry.of(), kube=Kube(root=tmp_path))) as c:
        runtime = c.app.state.runtime

        class Broken:
            def refresh(self):
                raise RuntimeError(SENTINEL)

        follower = Follower(sources, runtime.live, Broken(), {})
        box['data'] = second

        # when
        with caplog.at_level(logging.WARNING, logger='skopax'):
            follower.tick()

    # then
    text = ' '.join(r.getMessage() for r in caplog.records)
    assert 'not followed' in text and 'RuntimeError' in text and 'SENTINEL' not in text


def test_settings_that_fail_to_apply_log_the_exception_type_and_never_its_text(store, tmp_path, caplog, monkeypatch):
    # given
    first = SourceData('feed', {'investigate.max-cards': '4'})
    second = SourceData('feed', {'investigate.max-cards': '5'})
    box = {'data': first}
    sources = SourceSet([('feed', lambda: box['data'])], {'feed': first})

    def refuse(fetch=None, env=None):
        raise ConfigurationError(SENTINEL)

    with TestClient(create_app(settings(), store, sources=sources, registry=Registry.of(), kube=Kube(root=tmp_path))) as c:
        runtime = c.app.state.runtime
        follower = Follower(sources, runtime.live, runtime.instances, {})
        box['data'] = second
        monkeypatch.setattr('skopax.settingsources.load_settings', refuse)

        # when
        with caplog.at_level(logging.WARNING, logger='skopax'):
            follower.tick()

    # then
    text = ' '.join(r.getMessage() for r in caplog.records)
    assert 'not applied' in text and 'ConfigurationError' in text and 'SENTINEL' not in text


def test_only_a_source_that_failed_at_boot_counts_as_unanswered_until_it_first_loads():
    # given
    answers = {'config file': [None, RuntimeError('broken file'), None], 'feed': [RuntimeError('down'), None, RuntimeError('down again')]}

    def loader(label):
        def load():
            given = answers[label].pop(0)
            if isinstance(given, Exception):
                raise given
            return given
        return load

    sources = SourceSet([('config file', loader('config file')), ('feed', loader('feed'))], {'config file': answers['config file'].pop(0), 'feed': None}, failed={'feed': 'RuntimeError'})
    answers['feed'].pop(0)

    # when
    at_boot = sources.unanswered()
    sources.refresh()
    after_first = sources.unanswered()
    sources.refresh()
    after_second = sources.unanswered()

    # then
    assert at_boot == ['feed']
    assert after_first == []
    assert after_second == []


def test_bad_source_values_keep_one_warning_mark_per_setting(store, tmp_path, monkeypatch, caplog):
    # given
    monkeypatch.setattr(demo_plugin, 'SOURCE', {'values': {'demo.depth': '99'}})
    b = boot(MAIN, registry_with(demo_plugin))

    # when
    with TestClient(create_app(b.settings, store, registry=b.registry, sources=b.source_set(), kube=Kube(root=tmp_path))) as c:
        live = c.app.state.runtime.live
        with caplog.at_level(logging.WARNING):
            caplog.clear()
            for n in range(200):
                live.rebase(live.base, {'demo.depth': str(100 + n)}, {})
                live.plugin_value('demo.depth')
            marks = len(live._warned)

    # then
    warned = [r.getMessage() for r in caplog.records if 'demo.depth' in r.getMessage() and 'ignored' in r.getMessage()]
    assert len(warned) == 200 and marks == 1
    assert all('100' not in m and '299' not in m for m in warned)


def test_old_connection_keys_left_under_settings_get_one_boot_warning_naming_them_and_the_instance_form(tmp_path, caplog):
    # given
    path = write(tmp_path, '''
        settings:
          mongo.uri: mongodb://db.example.com:27017/shop
          jira.url: https://jira.example.com
          jira.api-token: hunter2-value
          slack.bot-token: xoxb-value
          gitlab.token: glpat-value
          slack.channel: '#ops'
    ''')
    clean = write(tmp_path, '''
        settings:
          mongo.uri: mongodb://db.example.com:27017/shop
          slack.channel: '#ops'
    ''')

    # when
    with caplog.at_level(logging.WARNING, logger='skopax'):
        boot({FILE_ENV: path}, Registry.of())
        warned = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
        caplog.clear()
        boot({FILE_ENV: clean}, Registry.of())
        quiet = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]

    # then
    assert warned == [msg('settings.old_connection', keys='gitlab.token, jira.api-token, jira.url, slack.bot-token', sources='config file')]
    assert 'plugins:' in warned[0] and 'instances:' in warned[0] and '- name: main' in warned[0]
    assert not any(value in warned[0] for value in ('hunter2-value', 'xoxb-value', 'glpat-value', 'jira.example.com'))
    assert quiet == []


def test_post_at_once_from_set_and_removed_in_a_source_moves_with_its_set_mark(store, tmp_path):
    # given
    start = SourceData('feed', {'investigate.max-cards': '4'})
    box = {'data': start}
    sources = SourceSet([('feed', lambda: box['data'])], {'feed': start})
    with TestClient(create_app(settings(), store, sources=sources, registry=Registry.of(), kube=Kube(root=tmp_path))) as c:
        live = c.app.state.runtime.live
        live.tunables.write('slack.crit-now', 'false', 'alice@example.com', None)
        follower = Follower(sources, live, c.app.state.runtime.instances, {})

        # when
        live.refresh(force=True)
        before = (live.current().slack_post_sev, live.current().slack_post_sev_set)
        box['data'] = SourceData('feed', {'investigate.max-cards': '4', 'slack.post-sev': 'crit'})
        follower.tick()
        live.refresh(force=True)
        added = (live.current().slack_post_sev, live.current().slack_post_sev_set)
        box['data'] = SourceData('feed', {'investigate.max-cards': '4'})
        follower.tick()
        live.refresh(force=True)
        removed = (live.current().slack_post_sev, live.current().slack_post_sev_set)

    # then
    assert before == ('off', False), 'the stored old switch counts while the new setting is not set'
    assert added == ('crit', True), 'a source that sets the new setting wins over the stored old switch at once'
    assert removed == ('off', False), 'and once it is gone the stored old switch counts again'


def test_a_source_hook_without_the_field_kind_argument_still_loads_and_one_with_it_is_told_the_kinds():
    # given
    asked = []

    def told(env, field_kind=None):
        asked.append((field_kind('demo', 'token'), field_kind('demo', 'url'), field_kind('demo', 'nope'), field_kind('nosuch', 'token')))
        return {'label': 'told source', 'values': {'investigate.max-cards': '4'}}

    def keyword_only(env, *, field_kind):
        asked.append(field_kind('demo', 'token'))
        return {'label': 'keyword source'}

    from tests.plugins.base import minimal
    registry = registry_with(demo_plugin, minimal('told', 'Told', hooks=Hooks(settings_source=told)), minimal('keyword', 'Keyword', hooks=Hooks(settings_source=keyword_only)))

    # when
    found = hook_sources(registry, {})

    # then
    assert sorted(s.label for s in found) == ['demo source', 'keyword source', 'told source']
    assert sorted(asked, key=str) == sorted([('secret', 'url', None, None), 'secret'], key=str)
