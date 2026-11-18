import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
INNER = '''
import threading

from skopax import agent, tags
from skopax.settingsources import SourceSet

RAW = threading.Event()


def alive():
    return [t for t in threading.enumerate() if t.name == 'agent-sources' and t.is_alive()]


def test_before():
    assert alive() == []


def test_leaks_a_refresher():
    agent.Refresher(SourceSet([], {}), 60).start()


def test_after_the_refresher():
    assert alive() == []


def test_leaks_a_thread_it_cannot_stop():
    threading.Thread(target=RAW.wait, args=(30,), name='agent-sources', daemon=True).start()


def test_after_the_thread_it_cannot_stop():
    assert len(alive()) == 1


def test_sets_plugin_tags():
    tags.use_tags({'zz-tag': 'Zz'})


def test_plugin_tags_are_reset():
    assert tags.plugin_tags() == {}


def test_releases_the_thread_it_could_not_stop():
    RAW.set()
'''


def test_a_leaking_test_fails_alone_and_its_neighbours_pass(tmp_path):
    # given
    (tmp_path / 'test_inner.py').write_text(INNER, encoding='utf-8')
    env = {**os.environ, 'PYTHONPATH': str(ROOT), 'PYTHONDONTWRITEBYTECODE': '1'}

    # when
    done = subprocess.run([sys.executable, '-m', 'pytest', '-q', '--color=no', '-n', '0', '-p', 'no:cacheprovider', '-p', 'tests.conftest', '--rootdir', str(tmp_path), str(tmp_path / 'test_inner.py')],
                          cwd=tmp_path, env=env, capture_output=True, text=True, timeout=120)
    errors = sorted(re.findall(r'^ERROR test_inner\.py::(\w+)', done.stdout, re.MULTILINE))
    summary = done.stdout.strip().splitlines()[-1]

    # then
    assert re.search(r'\b8 passed\b', summary) and re.search(r'\b2 errors\b', summary) and 'failed' not in summary, done.stdout[-3000:]
    assert errors == ['test_leaks_a_refresher', 'test_leaks_a_thread_it_cannot_stop']
    assert 'a test left an agent-sources thread running' in done.stdout
