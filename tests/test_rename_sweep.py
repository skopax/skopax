import re
import struct
import subprocess
from pathlib import Path

import pytest

import skopax
from skopax import brand

ROOT = Path(__file__).resolve().parent.parent
WEB = ROOT / 'skopax' / 'web'
PUBLIC = ROOT / 'skopax' / 'chatui' / 'public'
OLD = 'watch' + 'er'
ALLOWED_OLD_ENV_FILES: set[str] = set()
SHORT = OLD[:5]
SHORT_NEEDLES = [re.compile(r'(?<![A-Za-z])' + SHORT + '-'), re.compile(r'(?<![A-Za-z0-9])__' + SHORT.upper() + '_'), re.compile(r'(?<![A-Za-z])' + SHORT[:3] + '_'),
                 re.compile('__' + SHORT + '(?=[A-Z])'), re.compile(r'(?<![A-Za-z0-9_])' + SHORT.upper() + '_')]
PULSE_POINTS = '2 12 6 12 9 5 13 19 16 12 22 12'
OLD_ENV_NAME = re.compile(r'(?<![A-Za-z0-9_])' + OLD.upper() + r'_[A-Z0-9_]*')
BROWSER_FILES = sorted(p for d in (WEB, PUBLIC) for p in d.rglob('*') if p.is_file() and p.suffix in ('.js', '.css', '.html', '.svg'))
OLD_BROWSER_NAMES = [OLD + '-', OLD + '_', OLD.upper() + '_I18N']
ASSET_REF = re.compile(r'''(?:src|href)=["']/(?:static|icons)/([\w./-]+)|(?:/chat/public|/public)/([\w.-]+\.(?:css|js|svg))''')


def test_browser_names():
    # given
    hits = [f'{p.relative_to(ROOT)}: {n}' for p in BROWSER_FILES for n in OLD_BROWSER_NAMES if n in p.read_text(encoding='utf-8')]
    chatui = (ROOT / 'skopax' / 'chatui' / '__init__.py').read_text(encoding='utf-8')
    pages = [WEB / 'template.html', WEB / 'card.html', WEB / 'login.html', WEB / 'setup.html']

    # when
    missing = []
    for page in pages:
        for m in ASSET_REF.finditer(page.read_text(encoding='utf-8')):
            rel = m[1] or m[2]
            base = WEB / ('icons' if '/icons/' in m[0] else 'static')
            if not (base / rel).exists():
                missing.append(f'{page.name}: {rel}')
    for m in re.finditer(r'"/(?:chat/)?public/([\w.-]+)"', chatui):
        if not (PUBLIC / m[1]).exists():
            missing.append(f'chatui: {m[1]}')

    # then
    assert hits == []
    assert sorted(p.name for p in PUBLIC.iterdir() if p.name.startswith('skopax-')) == ['skopax-back.svg', 'skopax-chat.css', 'skopax-chat.js']
    assert missing == []


def test_actors_fallback_version_matches_the_package_version():
    # given
    text = (WEB / 'static' / 'actors.js').read_text(encoding='utf-8')

    # when
    found = set(re.findall(r'''['"`](\d+\.\d+\.\d+(?:(?:a|b|rc)\d+|-rc\.\d+)?)['"`]''', text))

    # then
    assert found, 'no version literal found in actors.js'
    assert found == {brand.display_version(skopax.__version__)}


def test_the_browser_files_and_page_tests_carry_no_code_or_claims_for_an_integration_that_does_not_exist():
    # given
    word = 'couch' + 'base'
    dead = ("'cb|", 'cb-index-memory', '.cb-sum', '#cb-tags', 'cbOpen', 'cbUp')
    page_tests = sorted((ROOT / 'tests' / 'page').glob('*.test.js'))

    # when
    code = [f'{p.relative_to(ROOT)}: {needle}' for p in BROWSER_FILES for needle in (word, *dead) if needle in p.read_text(encoding='utf-8').lower() or needle in p.read_text(encoding='utf-8')]
    claims = [p.name for p in page_tests if word in p.read_text(encoding='utf-8').lower()]

    # then
    assert page_tests
    assert code == []
    assert claims == []


def tracked_and_untracked(root: Path) -> list[str]:
    if not (root / '.git').exists():
        pytest.skip('the tree is not a git work tree, so there is no file list to sweep')
    out = subprocess.run(['git', 'ls-files', '--cached', '--others', '--exclude-standard', '-z'], cwd=root, check=True, capture_output=True).stdout
    return sorted({p for p in out.decode('utf-8').split('\0') if p and (root / p).is_file()})


def old_name_hits(root: Path, paths: list[str], allowed: frozenset[str] | set[str] = ALLOWED_OLD_ENV_FILES) -> list[str]:
    hits = []
    for rel in paths:
        if OLD in rel.lower() or any(n.search(rel) for n in SHORT_NEEDLES):
            hits.append(f'{rel}: path')
        raw = (root / rel).read_bytes()
        if b'\0' in raw:
            continue
        try:
            text = raw.decode('utf-8')
        except UnicodeDecodeError:
            continue
        if rel in allowed:
            text = OLD_ENV_NAME.sub('', text)
        hits += [f'{rel}:{n}: {line.strip()[:80]}' for n, line in enumerate(text.splitlines(), 1) if OLD in line.lower() or any(n.search(line) for n in SHORT_NEEDLES)]
    return hits


def test_old_name_is_gone():
    # given
    assert ALLOWED_OLD_ENV_FILES == set()
    paths = tracked_and_untracked(ROOT)

    # when
    hits = old_name_hits(ROOT, paths)

    # then
    assert paths, 'the tree listing is empty'
    assert hits == []


def test_an_allowlist_given_to_the_sweep_covers_only_uppercase_env_names_in_its_files(tmp_path):
    # given
    allowed = ['skopax/first.py', 'skopax/second.py']
    for rel in allowed + ['skopax/other.py']:
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
    (tmp_path / allowed[0]).write_text(f"print('{OLD.upper()}_TOKEN is renamed')\n", encoding='utf-8')
    (tmp_path / allowed[1]).write_text(f"x = '{OLD}'\n", encoding='utf-8')
    (tmp_path / 'skopax/other.py').write_text(f"x = '{OLD.upper()}_TOKEN'\n", encoding='utf-8')

    # when
    hits = old_name_hits(tmp_path, allowed + ['skopax/other.py'], set(allowed))

    # then
    assert [h.split(':')[0] for h in hits] == [allowed[1], 'skopax/other.py']


def test_the_old_name_in_a_path_is_a_hit(tmp_path):
    # given
    (tmp_path / f'{OLD}.svg').write_bytes(b'\x89PNG\0')

    # when
    hits = old_name_hits(tmp_path, [f'{OLD}.svg'])

    # then
    assert hits == [f'{OLD}.svg: path']


ICONS = {'icon-192.png': 192, 'icon-512.png': 512, 'icon-maskable-512.png': 512, 'apple-touch-icon.png': 180}


def test_icons_exist_and_are_png():
    # given
    folder = WEB / 'icons'

    # when
    sizes = {}
    for name in ICONS:
        head = (folder / name).read_bytes()[:24]
        assert head[:8] == b'\x89PNG\r\n\x1a\n' and head[12:16] == b'IHDR', name
        sizes[name] = struct.unpack('>II', head[16:24])

    # then
    assert sizes == {name: (px, px) for name, px in ICONS.items()}
    assert (folder / 'mark.svg').read_text(encoding='utf-8').startswith('<svg')


def test_the_old_short_names_are_a_hit_and_look_alikes_are_not(tmp_path):
    # given
    bad = [SHORT + '-tab', '/*__' + SHORT.upper() + '_DATA__*/', SHORT[:3] + '_abcd1234_secret', '__' + SHORT + 'Poll', SHORT.upper() + '_LANG']
    good = ['stop' + SHORT + '-', 'wrist' + SHORT + '-', 'chat' + SHORT.capitalize(), SHORT + 'ed', SHORT + 'dog', SHORT.capitalize() + 'edDb', 'swat_team', 'my' + SHORT + 'ed_x', '__' + SHORT + 'dog', 'STOP' + SHORT.upper() + '_MS', 'X__' + SHORT.upper() + '_Y']
    for n, name in enumerate(bad):
        (tmp_path / f'bad{n}.js').write_text(f"load('{name}')\n", encoding='utf-8')
    for n, name in enumerate(good):
        (tmp_path / f'good{n}.js').write_text(f"load('{name}')\n", encoding='utf-8')
    (tmp_path / 'ok.md').write_text('the watch passes still run\n', encoding='utf-8')
    (tmp_path / 'consts.py').write_text('LISTENER_WAIT_S = 30\n', encoding='utf-8')
    (tmp_path / 'bad_py.py').write_text(f'{SHORT.upper()}_S = 30\n', encoding='utf-8')

    # when
    hits = old_name_hits(tmp_path, [f'bad{n}.js' for n in range(len(bad))] + [f'good{n}.js' for n in range(len(good))] + ['ok.md', 'consts.py', 'bad_py.py'])

    # then
    assert sorted({h.split(':')[0] for h in hits}) == sorted([f'bad{n}.js' for n in range(len(bad))] + ['bad_py.py'])


def test_the_old_pulse_glyph_is_nowhere_in_the_package():
    # given
    texts = {p: p.read_text(encoding='utf-8') for p in (ROOT / 'skopax').rglob('*') if p.is_file() and p.suffix in ('.html', '.js', '.css', '.svg', '.py', '.md')}

    # then
    assert [str(p.relative_to(ROOT)) for p, t in texts.items() if PULSE_POINTS in t] == []
    assert texts


def test_no_browser_file_clears_a_storage_key_nothing_writes():
    # given
    removers = [f'{p.relative_to(ROOT)}: {m[0]}' for p in BROWSER_FILES if p.suffix == '.js' for m in re.finditer(r"removeItem\('skopax-token'\)", p.read_text(encoding='utf-8'))]

    # then
    assert removers == []
