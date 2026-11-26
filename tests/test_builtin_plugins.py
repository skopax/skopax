import importlib
import json
import shutil
from pathlib import Path

import pytest

from skopax.mcptools import presets
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


def fields_of(path: str) -> dict:
    return {f.key: f for f in plugin_of(path).fields}


def test_the_mcp_server_form_is_grouped_and_its_choices_read_as_words():
    # given
    fields = fields_of('skopax.plugins.mcp_tools')
    names = {p.id: p.name for p in presets.load() if not p.soon}

    # then
    assert {k: f.group for k, f in fields.items()} == {'url': 'connection', 'preset': 'connection', 'auth': 'sign-in', 'header-name': 'sign-in', 'header-value': 'sign-in', 'oauth-scopes': 'sign-in',
                                                     'oauth-client-id': 'sign-in', 'oauth-client-secret': 'sign-in', 'chat-role': 'access', 'knowledge': 'access', 'ca': 'advanced'}
    assert fields['auth'].choice_labels == {'none': 'No sign-in', 'header': 'Token in a header', 'oauth': 'OAuth'}
    assert fields['chat-role'].choice_labels == {'viewer': "Card runs + everyone's questions", 'operator': 'Card runs + operator and admin questions', 'admin': 'Card runs + admin questions', 'cards': 'Card runs only'}
    assert fields['auth'].label == 'Method'
    assert dict(fields['preset'].choice_labels) == {k: v for k, v in names.items() if k in fields['preset'].choices} and set(fields['preset'].choices) <= set(names)


def test_the_prometheus_form_is_grouped_and_its_sign_in_reads_as_words():
    # given
    fields = fields_of('skopax.plugins.prometheus')

    # then
    assert fields['auth'].choice_labels == {'none': 'None', 'bearer': 'Bearer token', 'basic': 'Username and password'}
    assert [(k, f.group) for k, f in fields.items()] == [('url', 'connection'), ('ui-url', 'connection'), ('auth', 'sign-in'), ('token', 'sign-in'), ('username', 'sign-in'), ('password', 'sign-in'),
                                                         ('ca', 'advanced'), ('ca-pinned', 'advanced'), ('namespaces', 'scope'), ('ingress-exclude', 'scope'), ('profiles', 'scope')]


@pytest.mark.parametrize('path', BUILTIN)
def test_certificate_and_relay_fields_of_a_built_in_plugin_sit_under_advanced(path):
    # when
    keys = {f.key: f.group for f in plugin_of(path).fields if f.kind == 'pem' or f.key in ('ca-pinned', 'via')}

    # then
    assert all(g == 'advanced' for g in keys.values()), keys


@pytest.mark.parametrize('path, key, labels', [
    ('skopax.plugins.kubernetes', 'mode', {'in-cluster': 'This cluster', 'agent': 'Through an agent'}),
    ('skopax.plugins.argocd', 'mode', {'api': 'Argo CD API', 'in-cluster': 'This cluster'}),
    ('skopax.plugins.teams', 'min-sev', {'warn': 'Warning', 'high': 'High', 'crit': 'Critical'}),
])
def test_mode_and_severity_choices_read_as_words(path, key, labels):
    # when
    found = fields_of(path)[key].choice_labels

    # then
    assert dict(found) == labels


@pytest.mark.parametrize('path', BUILTIN)
def test_every_built_in_plugin_icon_is_a_static_64_square_svg_with_a_drawn_glyph(path):
    # given
    svg = (package_of(path) / 'icon.svg').read_text(encoding='utf-8')
    lowered = svg.lower()

    # then
    assert 'viewbox="0 0 64 64"' in lowered
    assert '<text' not in lowered
    assert '<script' not in lowered
    assert 'http://' not in lowered.replace('xmlns="http://www.w3.org/2000/svg"', '')
    assert 'https://' not in lowered
    assert 'href' not in lowered
    assert any(shape in lowered.split('/>', 1)[1] for shape in ('<path', '<circle', '<rect', '<line', '<polyline'))
    assert len(svg.encode('utf-8')) < 2048
