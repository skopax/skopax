import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent / 'skopax'
STATIC = ROOT / 'web' / 'static'
NAMES = ('board inbox chat spark server timer tag report hex bell pulse history coin wrench plug gear shield dots check book search collapse expand updown menu x sun moon monitor key out chevR chevD tick '
         'send ear eye phone badge lock plus alert toggle').split()
FORBIDDEN = ('fonts.googleapis.com', 'fonts.gstatic.com', 'cdn.', 'unpkg.com', 'jsdelivr')
OUT = r'''(?:https?:|wss?:)?//'''
TAG = re.compile(r'<([a-zA-Z][\w:-]*)\b([^>]*)>')
ATTR = re.compile(r'''\b(src|srcset|poster|href|xlink:href)\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>]+))''', re.I)
SRC_TAGS_ONLY_HREF = ('link', 'image', 'use')
PATTERNS = (
    r'''url\(\s*["']?\s*''' + OUT,
    r'''@import\s+(?:url\(\s*)?["']?\s*''' + OUT,
    r'''\b(?:fetch|import|importScripts|EventSource|WebSocket|sendBeacon)\s*\(\s*["'`]\s*''' + OUT,
    r'''\bnew\s+(?:URL|Request|WebSocket|EventSource|Worker|SharedWorker)\s*\(\s*["'`]\s*''' + OUT,
    r'''\.open\(\s*["'][A-Za-z]+["']\s*,\s*["'`]\s*''' + OUT,
    r'''\.(?:src|href|srcset|poster)\s*=\s*["'`]\s*''' + OUT,
)
OUT_AT_START = re.compile(r'^\s*' + OUT, re.I)
OUT_IN_SET = re.compile(r'(?:^|,)\s*' + OUT, re.I)


def outside_fetches(text):
    found = []
    for tag, attrs in TAG.findall(text):
        for name, dq, sq, bare in ATTR.findall(attrs):
            value = dq or sq or bare
            name = name.lower()
            if name == 'href' or name == 'xlink:href':
                if tag.lower() not in SRC_TAGS_ONLY_HREF:
                    continue
            hit = OUT_IN_SET.search(value) if name == 'srcset' else OUT_AT_START.match(value)
            if hit:
                found.append(f'<{tag} {name}>')
    for pattern in PATTERNS:
        found += [m.group(0) for m in re.finditer(pattern, text, re.I)]
    return found


BLOCKED = [
    '<img src="https://example.com/p.gif" alt="">',
    "<img src='http://example.com/p.gif'>",
    '<script src="//example.com/a.js"></script>',
    "<script src='//example.com/a.js'></script>",
    "<link rel='stylesheet' href='https://example.com/a.css'>",
    '<link rel="stylesheet" href="//example.com/a.css">',
    '<img srcset="a.png 1x, https://example.com/b.png 2x">',
    '<video poster="//example.com/p.png"></video>',
    '<iframe src=https://example.com/f></iframe>',
    '<image href="https://example.com/p.png"/>',
    '.a { background: url(https://example.com/p.png); }',
    ".a { background: url( '//example.com/p.png' ); }",
    '@import "https://example.com/a.css";',
    "@import url('//example.com/a.css');",
    "fetch('https://example.com/x')",
    'fetch("//example.com/x")',
    'import("https://example.com/m.js")',
    "new URL('https://example.com/x')",
    'new URL("//example.com/x")',
    "importScripts('https://example.com/w.js')",
    "x.open('GET', 'https://example.com/x')",
    'x.open("POST", "//example.com/x")',
    "el.src = 'https://example.com/x.js'",
    'new WebSocket("wss://example.com/s")',
    '<link rel="stylesheet" href="HTTPS://example.com/f.woff2">',
    '<img srcset="a.png 1x, Https://example.com/b.png 2x">',
    "fetch('HTTPS://example.com/x')",
    '.a { background: URL(HTTP://example.com/p.png); }',
    'new WebSocket("WSS://example.com/s")',
]

ALLOWED = [
    '<a href="https://skopax.io" rel="noopener noreferrer">Skopax 0.1.0</a>',
    "<a href='https://skopax.io'>x</a>",
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24"></svg>',
    '<svg xmlns:xlink="http://www.w3.org/1999/xlink"></svg>',
    '<script src="/static/core.js?v=abc"></script>',
    '<link rel="icon" href="/icons/mark.svg">',
    '<img src="/plugins/prometheus/icon.svg" alt="">',
    '<img src="data:image/svg+xml;base64,AAAA" alt="">',
    '.a { background: url(/icons/mark.svg); }',
    "fetch('/api/anomalies')",
    "x.open('GET', '/api/x')",
    "new URL('/chat/', location.href)",
]


@pytest.mark.parametrize('sample', BLOCKED)
def test_the_matcher_blocks_every_outside_fetch_form(sample):
    # then
    assert outside_fetches(sample), sample


@pytest.mark.parametrize('sample', ALLOWED)
def test_the_matcher_allows_navigation_links_namespaces_and_same_origin_forms(sample):
    # then
    assert outside_fetches(sample) == [], sample


def walk_roots():
    return {
        'web': (ROOT / 'web', ('.html', '.js', '.svg')),
        'chat': (ROOT / 'chatui' / 'public', None),
        'plugins': (ROOT / 'plugins', ('.svg', '.js', '.html')),
    }


def served_files(root):
    base, suffixes = walk_roots()[root]
    return [p for p in sorted(base.rglob('*')) if p.is_file() and (suffixes is None or p.suffix in suffixes) and p.suffix not in ('.png', '.woff2')]


def test_every_walk_root_yields_files_including_one_known_file_per_root():
    # then
    names = {root: {p.relative_to(ROOT).as_posix() for p in served_files(root)} for root in walk_roots()}
    assert all(names[root] for root in names)
    assert 'web/template.html' in names['web'] and 'web/static/icons.js' in names['web']
    assert 'chatui/public/theme.json' in names['chat'] and 'chatui/public/skopax-chat.js' in names['chat']
    assert 'plugins/prometheus/icon.svg' in names['plugins'] and 'plugins/kubernetes/icon.svg' in names['plugins']


def test_no_page_asset_loads_from_another_origin():
    # given
    files = [p for root in walk_roots() for p in served_files(root)]

    # then
    assert files
    for path in files:
        text = path.read_text(encoding='utf-8')
        for word in FORBIDDEN:
            assert word not in text, f'{path} names {word}'
        assert outside_fetches(text) == [], f'{path} fetches from another origin'
    assert json.loads((ROOT / 'chatui' / 'public' / 'theme.json').read_text(encoding='utf-8'))['custom_fonts'] == []


def icon_table():
    return re.findall(r"^  (\w+): '(.*)',?$", (STATIC / 'icons.js').read_text(encoding='utf-8'), re.M)


def test_icons_js_defines_every_icon_the_page_names():
    # given
    defined = icon_table()
    keys = [k for k, _ in defined]

    # then
    assert set(NAMES) <= set(keys) and len(keys) == len(set(keys))
    for key, body in defined:
        shapes = re.findall(r'<(?:path|rect|circle)\b[^>]*/>', body)
        assert shapes, key
        assert all(re.search(r'\bd="[^"]+"|\b(?:cx|x)="-?\d', shape) for shape in shapes), key
    for path in sorted(STATIC.glob('*.js')):
        for name in re.findall(r'''navIcon\(\s*['"](\w+)['"]''', path.read_text(encoding='utf-8')):
            assert name in keys, f'{path.name} names the missing icon {name}'


def test_the_icon_checks_go_red_on_an_empty_path_and_a_double_quoted_call():
    # given
    empty = re.findall(r"^  (\w+): '(.*)',?$", "  toggle: '<path d=\"\"/>',\n", re.M)
    call = re.findall(r'''navIcon\(\s*['"](\w+)['"]''', 'navIcon("nope") + navIcon(\'tick\')')

    # then
    assert not re.search(r'\bd="[^"]+"', empty[0][1]) and call == ['nope', 'tick']


def test_the_font_tokens_are_the_exact_system_stacks_in_both_pages_and_both_chat_themes():
    # given
    ui = '--font-ui: system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;'
    data = '--font-data: ui-monospace, "SF Mono", Menlo, Consolas, monospace;'
    themes = json.loads((ROOT / 'chatui' / 'public' / 'theme.json').read_text(encoding='utf-8'))['variables']

    # then
    for page in ('template.html', 'card.html'):
        lines = [line.strip() for line in (ROOT / 'web' / page).read_text(encoding='utf-8').splitlines()]
        assert lines.count(ui) == 1 and lines.count(data) == 1, page
    for mode in ('light', 'dark'):
        assert themes[mode]['--font-sans'] == 'system-ui, -apple-system, "Segoe UI", Roboto, sans-serif'
