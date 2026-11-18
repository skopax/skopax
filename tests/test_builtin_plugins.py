import importlib
import json
import shutil
from pathlib import Path

import pytest

from skopax.registry import ATTR, BUILTIN, describe

FAULTY = 'tests.plugins.walker_faults'


def plugin_of(path: str):
    return getattr(importlib.import_module(path), ATTR)


def package_of(path: str) -> Path:
    return Path(importlib.import_module(path).__file__).parent


def help_gaps(plugin) -> list[tuple[str, list[str], list[str]]]:
    return [(r.id, sorted(set(r.defaults) - set(r.help)), sorted(set(r.help) - set(r.defaults))) for r in plugin.rules if set(r.help) != set(r.defaults)]


def json_fault(plugin) -> str | None:
    manifest = describe(plugin, icon=True)
    try:
        same = json.loads(json.dumps(manifest, allow_nan=False)) == manifest
    except (TypeError, ValueError) as e:
        return type(e).__name__
    return None if same else 'changed'


def knowledge_files(directory: Path) -> list[str]:
    return sorted(p.relative_to(directory).as_posix() for p in directory.rglob('*') if p.name.lower() == 'knowledge.md')


def test_the_walk_covers_every_built_in_plugin():
    # then
    assert BUILTIN and len(set(BUILTIN)) == len(BUILTIN)


@pytest.mark.parametrize('path', BUILTIN)
def test_every_rule_threshold_of_a_built_in_plugin_has_help_and_no_help_is_left_over(path):
    # when
    gaps = help_gaps(plugin_of(path))

    # then
    assert gaps == []


@pytest.mark.parametrize('path', BUILTIN)
def test_every_built_in_plugin_describes_itself_as_json(path):
    # when
    fault = json_fault(plugin_of(path))

    # then
    assert fault is None


@pytest.mark.parametrize('path', BUILTIN)
def test_no_built_in_plugin_package_holds_a_knowledge_md(path):
    # when
    found = knowledge_files(package_of(path))

    # then
    assert found == []


def test_the_walkers_report_a_help_key_without_a_default_a_manifest_that_is_not_json_and_a_knowledge_md(tmp_path):
    # given
    plugin = plugin_of(FAULTY)
    package = tmp_path / 'walker_faults'
    shutil.copytree(package_of(FAULTY), package)
    (package / 'knowledge.md').write_text('# Walker faults\n', encoding='utf-8')

    # when
    found = (help_gaps(plugin), json_fault(plugin), knowledge_files(package))

    # then
    assert found == ([('rates', [], ['gone'])], 'ValueError', ['knowledge.md'])
