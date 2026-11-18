import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOCK = tomllib.loads((ROOT / 'uv.lock').read_text(encoding='utf-8'))
PROJECT = next(p for p in LOCK['package'] if 'virtual' in p['source'])
PYPROJECT = tomllib.loads((ROOT / 'pyproject.toml').read_text(encoding='utf-8'))


def normal(name: str) -> str:
    return re.sub(r'[-_.]+', '-', name).lower()


def declared(specs: list[str]) -> set[tuple[str, frozenset[str], str]]:
    found = set()
    for spec in specs:
        match = re.fullmatch(r'([A-Za-z0-9_.-]+)(?:\[([^\]]*)\])?(.*)', spec.replace(' ', ''))
        extras = frozenset(e for e in (match[2] or '').split(',') if e)
        found.add((normal(match[1]), extras, match[3]))
    return found


def required(entries: list[dict]) -> set[tuple[str, frozenset[str], str]]:
    return {(normal(e['name']), frozenset(e.get('extras', [])), e.get('specifier', '')) for e in entries}


def test_no_requirements_exports_are_committed():
    # then
    assert sorted(p.name for p in ROOT.glob('requirements*.txt')) == []


def test_lock_holds_one_version_per_package_name():
    # given
    names = [p['name'] for p in LOCK['package']]

    # when
    repeated = {n for n in names if names.count(n) > 1}

    # then
    assert repeated == set()


def test_pyproject_dependencies_match_the_lock():
    # given
    metadata = PROJECT['metadata']

    # when
    runtime = declared(PYPROJECT['project']['dependencies'])
    dev = declared(PYPROJECT['dependency-groups']['dev'])

    # then
    assert runtime == required(metadata['requires-dist'])
    assert dev == required(metadata['requires-dev']['dev'])


def test_pyproject_version_matches_the_package_and_the_lock():
    # given
    init = (ROOT / 'skopax' / '__init__.py').read_text(encoding='utf-8')

    # when
    version = PYPROJECT['project']['version']

    # then
    assert re.search(r"^__version__ = '([^']+)'", init, re.M)[1] == version
    assert PROJECT['version'] == version
