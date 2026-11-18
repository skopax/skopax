import importlib
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from skopax.app import create_app
from skopax.contract import InstanceInfo
from skopax.investigator.tools import CORE_TOOLS
from skopax.registry import CORE_SIGS, NAV_GROUPS, Registry, problem
from skopax.settingsources import core_knowledge_ids, core_setting_ids
from skopax.sweeps import allowed, scope
from skopax.remote import Reply
from skopax.sealer import master_key
from tests.conftest import settings
from tests.test_brand import article_hits
from tests.test_exception_text import leaks as exception_leaks, tree_index
from tests.fake_http import FakePages
from tests.plugin_kit import add_instance
from tests.test_log_hygiene import leaks as log_leaks
from tests.test_no_cdn import outside_fetches
from tests.test_plugin_surface import network_uses, refused

ROOT = Path(__file__).resolve().parent.parent
EXAMPLE = ROOT / 'examples' / 'hello-plugin'
PACKAGE = EXAMPLE / 'skopax_hello'
NOW = 1_792_224_000.0
SECRET = 's3cr3t-key'
URL = f'https://shop.example.com/health?key={SECRET}'
SHOWN = 'https://shop.example.com/health'
KEY = 'hello|shop.example.com'
FULLER = {'couchbase': ('couchbase-plugin', 'skopax_couchbase', ('icon.svg', 'panel.js')), 'managed-redis': ('managed-redis-plugin', 'skopax_managed_redis', ('icon.svg',))}
DISCOVER = ('import json, sys; from pathlib import Path; sys.path[:0] = [sys.argv[2], sys.argv[1]]; from skopax.registry import Registry; '
            'x = Registry.load({}, builtin=()).get(sys.argv[3] if len(sys.argv) > 3 else "hello"); '
            'print(json.dumps(None if x is None else {"id": x.id, "origin": x.origin, "state": x.state, "reason": x.reason, '
            '"home": x.directory is not None and Path(x.directory).resolve().is_relative_to(Path(sys.argv[2]).resolve())}))')


def hello():
    import skopax_hello
    return skopax_hello


def test_the_example_installs_to_a_target_dir_and_is_discovered(tmp_path):
    # given
    source = tmp_path / 'source'
    target = tmp_path / 'site-packages'
    shutil.copytree(EXAMPLE, source, ignore=shutil.ignore_patterns('__pycache__', 'tests', 'Dockerfile'))
    uv = shutil.which('uv')
    assert uv, 'the example is built with uv (uv_build), so this test needs uv on PATH'
    installed = subprocess.run([uv, 'pip', 'install', '--no-deps', '--target', str(target), '--python', sys.executable, str(source)],
                               capture_output=True, text=True, timeout=300, env={**os.environ, 'UV_CACHE_DIR': str(tmp_path / 'uv-cache')})

    # when
    found = subprocess.run([sys.executable, '-I', '-c', DISCOVER, str(ROOT), str(target)], capture_output=True, text=True, timeout=120)

    # then
    assert installed.returncode == 0, installed.stderr
    assert (target / 'skopax_hello' / 'icon.svg').is_file() and (target / 'skopax_hello' / 'panel.js').is_file()
    assert (target / 'skopax_hello-0.1.0.dist-info' / 'licenses' / 'LICENSE').read_bytes() == (ROOT / 'LICENSE').read_bytes()
    assert found.returncode == 0, found.stderr
    assert json.loads(found.stdout) == {'id': 'hello', 'origin': 'entry-point', 'state': 'ok', 'reason': None, 'home': True}


def test_the_example_follows_the_plugin_rules():
    # given
    files = sorted(PACKAGE.rglob('*.py'))
    plugin = hello().PLUGIN

    # when
    sockets = {path.name: network_uses(path) for path in files}
    imports = refused(files, EXAMPLE)
    fetches = {path.name: outside_fetches(path.read_text(encoding='utf-8')) for path in sorted(PACKAGE.glob('*')) if path.suffix in ('.svg', '.js', '.html')}
    logged = [hit for path in files for hit in log_leaks(path, EXAMPLE)]
    raised = [hit for path in files for hit in exception_leaks(path, EXAMPLE, index=tree_index(), stable=True)]
    articles = {path.name: article_hits(path.read_text(encoding='utf-8')) for path in sorted(EXAMPLE.rglob('*')) if path.is_file() and path.suffix in ('.py', '.md', '.js')}
    loaded = Registry.of(plugin).loaded()[0]
    panels = [problem('panels', panel) for panel in plugin.panels]

    # then
    assert files and {name: hits for name, hits in sockets.items() if hits} == {}
    assert imports == []
    assert set(fetches) == {'icon.svg', 'panel.js'} and {name: hits for name, hits in fetches.items() if hits} == {}
    assert logged == [] and raised == []
    assert 'README.md' in articles and {name: hits for name, hits in articles.items() if hits} == {}
    assert (loaded.id, loaded.state, loaded.reason) == ('hello', 'ok', None)
    assert panels == [None] and [(p.tab, p.group, p.label) for p in plugin.panels] == [('hello', 'more', 'Hello')] and 'more' in NAV_GROUPS
    assert all((PACKAGE / p.script).is_file() for p in plugin.panels) and (PACKAGE / plugin.icon).is_file()
    assert (plugin.licence, plugin.version) == ('Apache-2.0', '0.1.0')


def test_the_example_check_opens_a_card_on_a_non_200(store):
    # given
    plugin = hello().PLUGIN
    pages = FakePages({URL: Reply(b'busy', {}, 503)})
    add_instance(store, plugin, 'main', sealer_key=master_key(store.db, settings()), primary=True, url=URL)
    with TestClient(create_app(settings(), store, registry=Registry.of(plugin), transport=pages)) as c:
        c.app.state.scheduler.nudge = lambda name: None
        c.app.state.runtime.refresh()

        # when
        c.app.state.sweeper.run('hello', 'hello', NOW)
        down = {a['key']: (a['status'], a['src'], a['sev'], a['what'], a['short']) for a in store.load()['anomalies']}
        rows = store.load().get('hello')
        pages.routes[URL] = Reply(b'ok', {}, 200)
        c.app.state.sweeper.run('hello', 'hello', NOW + 300)
        up = {a['key']: a['status'] for a in store.load()['anomalies']}

    # then
    assert down == {KEY: ('open', 'hello', 'warn', f'{SHOWN} answered HTTP 503', 'WARN shop.example.com not answering 200')}
    assert [(r['url'], r['ok'], r['said'], r['plugin']) for r in rows] == [(SHOWN, False, f'{SHOWN} answered HTTP 503', 'hello')]
    assert SECRET not in json.dumps([store.load()['anomalies'], rows], default=str)
    assert up == {KEY: 'cleared'}
    assert pages.urls() == [URL, URL]


@pytest.mark.parametrize('script', [EXAMPLE / 'skopax_hello' / 'panel.js', ROOT / 'examples' / 'couchbase-plugin' / 'skopax_couchbase' / 'panel.js'], ids=['hello', 'couchbase'])
def test_the_example_panel_counts_open_cards_like_the_built_ins(script):
    # given
    pattern = re.compile(r"a\.status === 'open'[^;]*;")
    example = script.read_text(encoding='utf-8')
    builtin = (ROOT / 'skopax' / 'plugins' / 'capacity' / 'panel.js').read_text(encoding='utf-8')

    # when
    ours = pattern.search(example)
    theirs = pattern.search(builtin)

    # then
    assert ours and theirs
    assert ours.group(0) == theirs.group(0)
    assert '!a.muted' in ours.group(0) and "a.triage !== 'handled'" in ours.group(0)


@pytest.mark.parametrize('plugin_id', sorted(FULLER))
def test_a_fuller_example_installs_to_a_target_dir_and_is_discovered(tmp_path, plugin_id):
    # given
    folder, package, data = FULLER[plugin_id]
    source = tmp_path / 'source'
    target = tmp_path / 'site-packages'
    shutil.copytree(ROOT / 'examples' / folder, source, ignore=shutil.ignore_patterns('__pycache__', 'tests', 'Dockerfile'))
    uv = shutil.which('uv')
    assert uv, 'the examples are built with uv (uv_build), so this test needs uv on PATH'
    installed = subprocess.run([uv, 'pip', 'install', '--no-deps', '--target', str(target), '--python', sys.executable, str(source)],
                               capture_output=True, text=True, timeout=300, env={**os.environ, 'UV_CACHE_DIR': str(tmp_path / 'uv-cache')})

    # when
    found = subprocess.run([sys.executable, '-I', '-c', DISCOVER, str(ROOT), str(target), plugin_id], capture_output=True, text=True, timeout=120)

    # then
    assert installed.returncode == 0, installed.stderr
    assert all((target / package / name).is_file() for name in data)
    assert (target / f'{package}-0.1.0.dist-info' / 'licenses' / 'LICENSE').read_bytes() == (ROOT / 'LICENSE').read_bytes()
    assert found.returncode == 0, found.stderr
    assert json.loads(found.stdout) == {'id': plugin_id, 'origin': 'entry-point', 'state': 'ok', 'reason': None, 'home': True}


@pytest.mark.parametrize('plugin_id', sorted(FULLER))
def test_a_fuller_example_follows_the_plugin_rules(plugin_id):
    # given
    folder, package, data = FULLER[plugin_id]
    example = ROOT / 'examples' / folder
    files = sorted((example / package).rglob('*.py'))
    plugin = importlib.import_module(package).PLUGIN

    # when
    sockets = {path.name: network_uses(path) for path in files}
    imports = refused(files, example)
    fetches = {path.name: outside_fetches(path.read_text(encoding='utf-8')) for path in sorted((example / package).glob('*')) if path.suffix in ('.svg', '.js', '.html')}
    logged = [hit for path in files for hit in log_leaks(path, example)]
    raised = [hit for path in files for hit in exception_leaks(path, example, index=tree_index(), stable=True)]
    articles = {path.name: article_hits(path.read_text(encoding='utf-8')) for path in sorted(example.rglob('*')) if path.is_file() and path.suffix in ('.py', '.md', '.js')}
    loaded = Registry.of(plugin).loaded()[0]
    panels = [problem('panels', panel) for panel in plugin.panels]
    tools = [problem('tools', tool) for tool in plugin.tools]

    # then
    assert files and {name: hits for name, hits in sockets.items() if hits} == {}
    assert imports == []
    assert set(fetches) == set(data) and {name: hits for name, hits in fetches.items() if hits} == {}
    assert logged == [] and raised == []
    assert 'README.md' in articles and {name: hits for name, hits in articles.items() if hits} == {}
    assert (loaded.id, loaded.state, loaded.reason) == (plugin_id, 'ok', None)
    assert panels == [None] * len(plugin.panels) and all(p.group in NAV_GROUPS and (example / package / p.script).is_file() for p in plugin.panels)
    assert tools == [None] and (example / package / plugin.icon).is_file()
    assert (plugin.licence, plugin.version, plugin.max_instances) == ('Apache-2.0', '0.1.0', None)
    assert {t.sig for t in plugin.card_types}.isdisjoint(CORE_SIGS)


def test_the_fuller_examples_load_beside_the_built_in_plugins_without_a_clash():
    # when
    registry = Registry.load({'SKOPAX_PLUGINS': 'skopax_couchbase, skopax_managed_redis'}, entry_points=False, core_tools=CORE_TOOLS, core_knowledge=core_knowledge_ids(),
                             core_settings=core_setting_ids())

    # then
    assert {p: (registry.get(p).state, registry.get(p).reason) for p in FULLER} == {p: ('ok', None) for p in FULLER}
    assert {x.id: x.state for x in registry.loaded() if x.state != 'ok'} == {}


def example_tests(plugin_id: str):
    folder, package, _ = FULLER[plugin_id]
    path = ROOT / 'examples' / folder / 'tests' / ('test_' + package.removeprefix('skopax_') + '.py')
    spec = importlib.util.spec_from_file_location(f'example_tests_{package}', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize('plugin_id', sorted(FULLER))
def test_a_second_instance_of_a_fuller_example_keeps_its_own_keys_and_clears_only_its_own_cards(plugin_id):
    # given
    package = importlib.import_module(FULLER[plugin_id][1])
    tests = example_tests(plugin_id)
    [check] = package.PLUGIN.checks
    if plugin_id == 'couchbase':
        data = tests.put(tests.node_put(tests.healthy(), 'uptime', 0, tests.N1, 60), 'queue', 0, 50)
        first, second = tests.Ctx(tests.FakeProm(data), name='orders'), tests.Ctx(tests.FakeProm(data), name='search')
    else:
        data = tests.put(tests.put(tests.healthy(), 'evicted', tests.MAIN, 5), 'uptime', tests.MAIN, 60)
        first, second = tests.Ctx(tests.FakeGrafana(data=data), name='cloud'), tests.Ctx(tests.FakeGrafana(data=data), name='backup')

    # when
    primary = scope(package.run(first), check, InstanceInfo(first.name, True, None, first.config))
    other = scope(package.run(second), check, InstanceInfo(second.name, False, second.name, second.config))

    # then
    assert len(primary.result.found) == len(other.result.found) == 2
    assert {c['key'] for c in primary.result.found}.isdisjoint({c['key'] for c in other.result.found})
    assert all(c['key'].startswith(f'{plugin_id}|{first.name}|') and '@' not in c['key'] for c in primary.result.found)
    assert all(c['key'].startswith(f'{plugin_id}|{second.name}|') and c['key'].endswith(f'@{second.name}') for c in other.result.found)
    assert primary.result.ran == (plugin_id, f'{plugin_id}:access:metrics') and other.result.ran == (f'{plugin_id}-{second.name}', f'{plugin_id}:access:metrics-{second.name}')
    assert all(allowed(c['ranKey'], check) for c in primary.result.found) and {c['ranKey'] for c in other.result.found} == {f'{plugin_id}-{second.name}'}
