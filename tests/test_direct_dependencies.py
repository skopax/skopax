import ast
import re
import sys
import tomllib
from importlib.metadata import packages_distributions
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def normal(name: str) -> str:
    return re.sub(r'[-_.]+', '-', name).lower()


def top_imports() -> dict[str, str]:
    found: dict[str, str] = {}
    for path in sorted((ROOT / 'skopax').rglob('*.py')):
        for node in ast.parse(path.read_text(encoding='utf-8')).body:
            names = [a.name for a in node.names] if isinstance(node, ast.Import) else [node.module] if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module else []
            for name in names:
                found.setdefault(name.split('.')[0], str(path.relative_to(ROOT)))
    return found


def test_every_top_level_third_party_import_in_skopax_is_a_direct_dependency():
    # given
    declared = {normal(re.split(r'[\[=<>!~; ]', d, maxsplit=1)[0]) for d in tomllib.loads((ROOT / 'pyproject.toml').read_text())['project']['dependencies']}
    owners = packages_distributions()

    # when
    third = {name: where for name, where in top_imports().items() if name != 'skopax' and name not in sys.stdlib_module_names and not name.startswith('_')}
    missing = sorted(f'{name} ({where})' for name, where in third.items() if not {normal(d) for d in owners.get(name, [name])} & declared)

    # then
    assert missing == []


def test_httpcore2_is_pinned_to_the_locked_version():
    # given
    deps = tomllib.loads((ROOT / 'pyproject.toml').read_text())['project']['dependencies']
    lock = (ROOT / 'uv.lock').read_text()

    # when
    locked = re.search(r'name = "httpcore2"\nversion = "([^"]+)"', lock).group(1)

    # then
    assert f'httpcore2=={locked}' in deps and locked == '2.16.0'
