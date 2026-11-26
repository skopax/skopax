import subprocess
import sys
import tomllib
from pathlib import Path

import pytest
from packaging.version import Version

import skopax
from tools import release_version

ROOT = Path(__file__).resolve().parent.parent


def project(tmp_path: Path, version: str) -> Path:
    path = tmp_path / 'pyproject.toml'
    path.write_text(f'[project]\nname = "skopax"\nversion = "{version}"\n', encoding='utf-8')
    return path


@pytest.mark.parametrize('tag, version, ok', [
    ('v0.1.0-rc.1', '0.1.0rc1', True),
    ('v0.1.0', '0.1.0rc1', False),
    ('v0.1.0-rc.2', '0.1.0rc1', False),
    ('0.1.0-rc.1', '0.1.0rc1', False),
    ('v0.1.0', '0.1.0', True),
    ('v0.1.0-rc.1', '0.1.0', False),
    ('v0.1', '0.1', False),
    ('v0.1.0-rc1', '0.1.0rc1', False),
    ('v01.0.0', '01.0.0', False),
    ('v1.00.0', '1.00.0', False),
    ('v0.1.0-rc.01', '0.1.0rc01', False),
    ('v\u0661.0.0', '\u0661.0.0', False),
    ('v1\u0661.0.0', '1\u0661.0.0', False),
    ('v1.0.1\u0661', '1.0.1\u0661', False),
])
def test_tag_and_package_version_must_match(tmp_path, capsys, tag, version, ok):
    # given
    path = project(tmp_path, version)

    # when
    code = release_version.main(['check', tag, '--pyproject', str(path)])

    # then
    out = capsys.readouterr().out
    assert code == (0 if ok else 1)
    assert out == ('' if ok else f'tag {tag} does not match package version {version}\n')


def test_image_tags_for_rc_and_release(capsys):
    # when
    rc = release_version.main(['tags', 'v0.1.0-rc.1'])
    rc_out = capsys.readouterr().out
    release = release_version.main(['tags', 'v0.1.0'])
    release_out = capsys.readouterr().out
    later = release_version.main(['tags', 'v2.13.4'])
    later_out = capsys.readouterr().out
    bad = release_version.main(['tags', '0.1.0'])
    bad_out = capsys.readouterr().out
    padded = release_version.main(['tags', 'v01.0.0'])
    padded_out = capsys.readouterr().out
    foreign = release_version.main(['tags', 'v\u0661.0.0'])
    foreign_out = capsys.readouterr().out

    # then
    assert (rc, rc_out) == (0, '0.1.0-rc.1\n')
    assert (release, release_out) == (0, '0.1.0\n0.1\nlatest\n')
    assert (later, later_out) == (0, '2.13.4\n2.13\nlatest\n')
    assert (bad, bad_out) == (1, 'tag 0.1.0 is not vX.Y.Z or vX.Y.Z-rc.N\n')
    assert (padded, padded_out) == (1, 'tag v01.0.0 is not vX.Y.Z or vX.Y.Z-rc.N\n')
    assert (foreign, foreign_out) == (1, 'tag v\u0661.0.0 is not vX.Y.Z or vX.Y.Z-rc.N\n')


def test_the_tree_version_is_the_current_release():
    # given
    declared = tomllib.loads((ROOT / 'pyproject.toml').read_text(encoding='utf-8'))['project']['version']

    # then
    assert release_version.package_version() == declared == skopax.__version__ == '0.6.3'
    assert release_version.main(['check', 'v0.6.3']) == 0


def test_release_tag_turns_a_package_version_into_its_git_tag():
    # when
    found = {version: release_version.release_tag(version) for version in ('0.1.0rc1', '0.1.0', '2.13.4rc12', '0.1.0.post1', '0.1', '01.0.0', '0.1.0rc01', '0.1.0a1')}

    # then
    assert found == {'0.1.0rc1': 'v0.1.0-rc.1', '0.1.0': 'v0.1.0', '2.13.4rc12': 'v2.13.4-rc.12', '0.1.0.post1': None, '0.1': None, '01.0.0': None, '0.1.0rc01': None, '0.1.0a1': None}
    assert all(release_version.expected(tag) == version for version, tag in found.items() if tag)


def test_the_tag_list_of_the_current_version_and_its_release_starts_with_the_exact_tag():
    # given
    tag = release_version.release_tag(skopax.__version__)
    release = release_version.release_tag(Version(skopax.__version__).base_version)

    # when
    done = [subprocess.run([sys.executable, '-m', 'tools.release_version', 'tags', t], cwd=ROOT, capture_output=True, text=True, check=False) for t in (tag, release)]

    # then
    assert [(d.returncode, d.stderr) for d in done] == [(0, ''), (0, '')]
    assert done[0].stdout.split()[0] == tag.removeprefix('v')
    major, minor, _ = release.removeprefix('v').split('.')
    assert done[1].stdout.split() == [release.removeprefix('v'), f'{major}.{minor}', 'latest']
