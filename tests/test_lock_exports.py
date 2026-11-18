import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOCK = tomllib.loads((ROOT / 'uv.lock').read_text(encoding='utf-8'))
PACKAGES = {p['name']: p for p in LOCK['package']}
PROJECT = next(p for p in LOCK['package'] if 'virtual' in p['source'])
HEADER = '# Generated from uv.lock by: {}\n'
RUNTIME_EXPORT = 'uv export --frozen --no-dev --no-header --no-annotate'
DEV_EXPORT = 'uv export --frozen --group dev --no-header --no-annotate'
PYPROJECT = tomllib.loads((ROOT / 'pyproject.toml').read_text(encoding='utf-8'))


def normal(name: str) -> str:
    return re.sub(r'[-_.]+', '-', name).lower()


def closure(roots: list[dict]) -> set[str]:
    seen, names, todo = set(), set(), list(roots)
    while todo:
        dep = todo.pop()
        package = PACKAGES[dep['name']]
        names.add(dep['name'])
        for part in [None, *dep.get('extra', [])]:
            if (dep['name'], part) in seen:
                continue
            seen.add((dep['name'], part))
            todo.extend(package.get('dependencies', []) if part is None else package['optional-dependencies'][part])
    return names


def hashes(package: dict) -> frozenset[str]:
    files = [*package.get('wheels', []), *([package['sdist']] if 'sdist' in package else [])]
    return frozenset(f['hash'] for f in files)


def locked(names: set[str]) -> dict[str, tuple[str, frozenset[str]]]:
    return {normal(n): (PACKAGES[n]['version'], hashes(PACKAGES[n])) for n in names}


def declared(specs: list[str]) -> set[tuple[str, frozenset[str], str]]:
    found = set()
    for spec in specs:
        match = re.fullmatch(r'([A-Za-z0-9_.-]+)(?:\[([^\]]*)\])?(.*)', spec.replace(' ', ''))
        extras = frozenset(e for e in (match[2] or '').split(',') if e)
        found.add((normal(match[1]), extras, match[3]))
    return found


def required(entries: list[dict]) -> set[tuple[str, frozenset[str], str]]:
    return {(normal(e['name']), frozenset(e.get('extras', [])), e.get('specifier', '')) for e in entries}


def exported(path: str) -> dict[str, tuple[str, frozenset[str]]]:
    lines = (ROOT / path).read_text(encoding='utf-8').replace('\\\n', ' ').splitlines()
    entries = {}
    for line in lines:
        if not line.strip() or line.lstrip().startswith('#'):
            continue
        spec = line.split('--hash=')[0].split(';')[0].strip()
        name, version = spec.split('==')
        entries[normal(name)] = (version, frozenset(re.findall(r'--hash=(\S+)', line)))
    return entries


def test_requirements_txt_is_the_hashed_runtime_export_of_the_lock():
    # given
    expected = locked(closure(PROJECT['dependencies']))

    # when
    entries = exported('requirements.txt')

    # then
    assert (ROOT / 'requirements.txt').read_text(encoding='utf-8').startswith(HEADER.format(RUNTIME_EXPORT))
    assert entries == expected
    assert all(found for _, found in entries.values())


def test_requirements_dev_txt_is_the_hashed_runtime_and_dev_export_of_the_lock():
    # given
    expected = locked(closure(PROJECT['dependencies'] + PROJECT['dev-dependencies']['dev']))

    # when
    entries = exported('requirements-dev.txt')

    # then
    assert (ROOT / 'requirements-dev.txt').read_text(encoding='utf-8').startswith(HEADER.format(DEV_EXPORT))
    assert entries == expected
    assert all(found for _, found in entries.values())


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
