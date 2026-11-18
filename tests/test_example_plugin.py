import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

from fastapi.testclient import TestClient

from skopax.app import create_app
from skopax.registry import NAV_GROUPS, Registry, problem
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
DISCOVER = ('import json, sys; from pathlib import Path; sys.path[:0] = [sys.argv[2], sys.argv[1]]; from skopax.registry import Registry; '
            'x = Registry.load({}, builtin=()).get("hello"); '
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


def test_the_example_panel_counts_open_cards_like_the_built_ins():
    # given
    pattern = re.compile(r"a\.status === 'open'[^;]*;")
    example = (EXAMPLE / 'skopax_hello' / 'panel.js').read_text(encoding='utf-8')
    builtin = (ROOT / 'skopax' / 'plugins' / 'capacity' / 'panel.js').read_text(encoding='utf-8')

    # when
    ours = pattern.search(example)
    theirs = pattern.search(builtin)

    # then
    assert ours and theirs
    assert ours.group(0) == theirs.group(0)
    assert '!a.muted' in ours.group(0) and "a.triage !== 'handled'" in ours.group(0)
