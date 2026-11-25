import dataclasses
import hashlib
import html
import importlib.util
import json
import logging
import os
import re
import shutil
import string
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import skopax
from skopax import actors, brand, chatui, engine, history, mcpserver, notices, statenotes
from skopax.app import create_app
from skopax.config import Settings, load_settings
from skopax.features import Features
from skopax.messages import MESSAGES, msg
from skopax.settingsources import FILE_ENV, boot
from skopax.tunables import Live, TunableStore
from tools import release_version
from tests.conftest import AUTH, TOKEN, settings
from tests.plugin_kit import registry_with
from tests.plugins import demo_plugin, switchable_entitlement
from tests.test_api_tokens import admin_client
from tests.test_mcp import mcp_client, mcp_settings, rpc, token
from tests.test_setup import app_client, fresh_store

MARKUP = '<b>&"x'
HTML = {'Accept': 'text/html'}
INIT = {'protocolVersion': '2025-06-18', 'capabilities': {}, 'clientInfo': {'name': 'claude-code', 'version': '2'}}
BARE = {'mongo_uri': 'm', 'mongo_db': 'd', 'token': 't', 'commit_id': 'c'}


def test_package_and_version():
    # given
    import skopax

    # when
    version = skopax.__version__
    old = importlib.util.find_spec('watch' + 'er')

    # then
    assert version == '0.6.1'
    assert old is None


def test_display_name_defaults_to_skopax_and_is_trimmed():
    # given
    named = {text: brand.display_name(Settings(**BARE, display_name=text)) for text in ('', '   ', '  Acme Ops  ', 'x' * 60)}

    # when
    loaded = load_settings(env={'SKOPAX_DISPLAY_NAME': '  Acme Ops  '})
    unset = load_settings(env={})

    # then
    assert (named[''], named['   ']) == ('Skopax', 'Skopax')
    assert named['  Acme Ops  '] == 'Acme Ops'
    assert named['x' * 60] == 'x' * 40
    assert (loaded.display_name, unset.display_name) == ('Acme Ops', 'Skopax')


def test_mcp_server_name_is_a_slug_of_the_display_name():
    # then
    assert brand.mcp_slug('Acme Ops') == 'acme-ops'
    assert brand.mcp_slug('Acme Ops!') == 'acme-ops'
    assert brand.mcp_slug('!!!') == 'skopax'
    assert brand.mcp_slug('') == 'skopax'


def test_attribution():
    # given
    named, default = dataclasses.replace(settings(), display_name='Acme Ops'), dataclasses.replace(settings(), display_name='Skopax')

    # then
    assert brand.attribution(named) == ('Acme Ops \u00b7 powered by Skopax 0.6.1', 'https://skopax.io')
    assert brand.attribution(default) == ('Skopax 0.6.1 \u00b7 skopax.io', 'https://skopax.io')


def test_the_shown_version_is_the_release_tag_form_of_the_package_version():
    # given
    versions = ('0.1.0rc1', '0.1.0', '1.20.3rc12', '0.1.0.post1', '01.0.0', 'dev')

    # when
    shown = {v: brand.display_version(v) for v in versions}

    # then
    assert brand.display_version() == '0.6.1' == brand.display_version(skopax.__version__)
    assert shown == {v: release_version.release_tag(v).removeprefix('v') if release_version.release_tag(v) else v for v in versions}
    assert shown['0.1.0rc1'] == '0.1.0-rc.1' and shown['0.1.0'] == '0.1.0' and shown['0.1.0.post1'] == '0.1.0.post1'
    assert brand.page_info()['version'] == '0.6.1' and skopax.__version__ == '0.6.1'


def test_display_name_is_escaped_everywhere(store):
    # given
    quoted = "O'Brien Ops"
    escaped = html.escape(quoted)
    client = admin_client(store, display_name=quoted)

    # when
    page = client.get('/', headers=HTML).text
    login = client.get('/login', headers=HTML).text
    manifest = client.get('/manifest.webmanifest')
    with app_client(fresh_store(), display_name=quoted) as c:
        setup = c.get('/setup', headers=HTML).text
    chat = tomllib.loads(chatui.config_text('https://example.com', name=MARKUP))['UI']
    instructions = mcpserver.instructions('')

    # then
    for text in (page, login, setup):
        assert escaped in text
    assert manifest.json()['name'] == quoted and manifest.json()['short_name'] == quoted
    assert instructions.startswith(f'{brand.product()}, powered by Skopax ') and f'{brand.product()} is a production health board' in instructions
    assert chat['name'] == MARKUP and chat['description'] == f'Chat with {MARKUP} about production'


def test_clean_name_strips_markup_characters_and_controls_but_keeps_letters():
    # then
    assert brand.clean_name('<b>&"x') == 'bx'
    assert brand.clean_name('Ops & SRE') == 'Ops SRE'
    assert brand.clean_name('Caf\u00e9 Ops') == 'Caf\u00e9 Ops'
    assert brand.clean_name('\x07') == brand.DEFAULT_NAME
    assert brand.clean_name('a\x00b\x7fc') == 'abc'
    assert brand.clean_name('a\x85b\x9fc') == 'abc'
    assert brand.clean_name('a\udc80b\ud800c') == 'abc'
    assert brand.clean_name('\x85\udc80') == brand.DEFAULT_NAME
    assert brand.clean_name('  Ops   Team  ') == 'Ops Team'
    assert brand.clean_name('x' * 60) == 'x' * 40
    assert brand.clean_name('<>&"') == brand.DEFAULT_NAME
    assert Settings(**BARE).display_name == brand.DEFAULT_NAME
    assert load_settings(env={'SKOPAX_DISPLAY_NAME': '<i>Ops</i> & "Co"'}).display_name == 'iOps/i Co'


CHAT_SCRIPT = """
import json, os, sys
os.environ.pop('CHAINLIT_APP_ROOT', None)
from skopax import brand, chatui
from skopax.config import load_settings
brand.use(brand.display_name(load_settings(env={'SKOPAX_DISPLAY_NAME': sys.argv[1]})))
from fastapi import FastAPI
from fastapi.testclient import TestClient
bridge = chatui.Bridge(user=lambda c: None, post=lambda *a: {}, chat=lambda *a: None, chats=lambda by: [], owner=lambda i: None, shared=lambda i: False,
                       share=lambda *a: True, hide=lambda *a: None, apply=lambda *a: '', card=lambda r: None)
app = FastAPI()
chatui.mount(app, bridge, b'seed', 'http://testserver')
with TestClient(app) as c:
    r = c.get('/chat/')
    sys.stdout.write(json.dumps({'status': r.status_code, 'headers': dict(r.headers), 'text': r.text}))
"""


def test_the_mounted_chat_page_takes_a_hostile_display_name_as_plain_text(tmp_path):
    # given
    root = str(Path(__file__).resolve().parent.parent)
    env = {k: v for k, v in os.environ.items() if k != 'CHAINLIT_APP_ROOT'} | {'PYTHONPATH': root}

    # when
    run = subprocess.run([sys.executable, '-c', CHAT_SCRIPT, '</title><script>x</script>'], cwd=tmp_path, env=env, capture_output=True, text=True, timeout=120)

    # then
    assert run.returncode == 0, run.stderr[-800:]
    page = json.loads(run.stdout)['text']
    assert '<title>' in page
    assert '<script>x' not in page and '</title><script' not in page
    assert len(re.findall(r'<title>[^<>]*</title>', page)) == 1
    for tag in re.findall(r'<meta[^>]*og:title[^>]*>', page):
        assert re.fullmatch(r'<meta property="og:title" content="[^"<>]*"\s*/?>', tag), tag


def test_slack_text_with_a_hostile_display_name_has_no_mention_or_broken_link():
    # given
    brand.use('<!here> <https://evil.example|click> Ops')

    # when
    texts = [msg('review.dm', when='1 Nov 10:00 UTC', text='t', link='https://h/#t=review'), msg('slackevents.dm.site', url='https://h/chat/'),
             msg('push.task', key='OPS-123', name='n', by='b', url='https://j.example.com/browse/OPS-123', card='https://h/c/1'), msg('slack.sent_from', sender='alice'),
             msg('slackevents.off_topic'), statenotes.progress_text(statenotes.shown('system'), None)]

    # then
    for text in texts:
        assert '<!' not in text and 'evil.example|click>' not in text
        assert all(re.fullmatch(r'<https?://[^<>|\s]+(\|[^<>]*)?>', link) for link in re.findall(r'<[^>]*>?', text)), text


def test_the_mcp_403_header_is_neutral_ascii_whatever_the_display_name(store):
    # given
    from skopax.mcpauth import McpAuth

    # when
    heads = {}
    for name in ('Ops "Q" Team', 'Ops \u0141\u00f3d\u017a'):
        with mcp_client(store, mcp_settings(display_name=name, mcp_required_role='SKOPAX_MCP')) as c:
            res = rpc(c, 'tools/list', bearer=token(roles=('PUBLIC',)))
        heads[name] = (res.status_code, res.headers['www-authenticate'])
    challenge = McpAuth(mcp_settings(), None, None, 'https://h').challenge('insufficient_scope', 'say "hi" \\ \u0141')

    # then
    for name, (status, header) in heads.items():
        assert status == 403
        header.encode('ascii')
        assert re.fullmatch(r'Bearer error="insufficient_scope", error_description="[^"\\]*", resource_metadata="[^"]+", scope="[^"]+"', header), header
        assert msg('mcp.no_role_header') in header and 'Ops' not in header
    challenge.encode('ascii')
    assert re.fullmatch(r'Bearer error="insufficient_scope", error_description="(?:[^"\\]|\\.)*", resource_metadata="[^"]+", scope="[^"]+"', challenge), challenge
    assert 'say \\"hi\\" \\\\ ?' in challenge


def test_mcp_instructions_carry_the_display_name_as_plain_text():
    # given
    brand.use('Ops SRE')

    # then
    assert mcpserver.instructions('').startswith('Ops SRE, powered by Skopax ') and 'Ops SRE is a production health board' in mcpserver.instructions('')
    assert 'the Ops SRE investigator' in mcpserver.instructions('')
    assert 'Ops &' not in mcpserver.instructions('') and '&amp;' not in mcpserver.instructions('')


def test_version_is_the_same_everywhere(store):
    # given
    version = skopax.__version__
    client = admin_client(store, display_name='Acme Ops')
    mcp = mcp_client(store, mcp_settings(display_name='Acme Ops'))
    powered = f'Acme Ops \u00b7 powered by Skopax {brand.display_version()}'

    # when
    server = client.get('/api/server').json()
    page = client.get('/', headers=HTML).text
    login = client.get('/login', headers=HTML).text
    with app_client(fresh_store(), display_name='Acme Ops') as c:
        setup = c.get('/setup', headers=HTML).text
    with mcp as c:
        info = rpc(c, 'initialize', INIT, token()).json()['result']['serverInfo']

    # then
    assert server['version'] == version and server['displayName'] == 'Acme Ops'
    assert powered in page and powered in login and powered in setup
    assert '<a href="https://skopax.io"' in login and '<a href="https://skopax.io"' in setup
    for text in (login, setup, page):
        assert re.findall(r'<a [^>]*href="https://skopax.io"[^>]*>', text) and all('rel="noopener noreferrer"' in a for a in re.findall(r'<a [^>]*href="https://skopax.io"[^>]*>', text))
    assert info['version'] == version and info['name'] == 'acme-ops'
    assert server['product'] == 'Skopax' and info['websiteUrl'] == 'https://skopax.io'


def test_default_name_attribution_is_the_short_line(store):
    # given
    client = admin_client(store)

    # when
    login = client.get('/login', headers=HTML).text

    # then
    assert f'Skopax {brand.display_version()} \u00b7 skopax.io' in login


def test_messages_use_the_display_name(store, caplog):
    # given
    caplog.set_level(logging.INFO, logger='skopax')
    client = admin_client(store, display_name='Acme Ops')

    # when
    last = client.patch('/api/admin/users/admin@example.com', json={'role': 'operator'})
    with app_client(fresh_store(), display_name='Acme Ops') as c:
        c.get('/setup')
        detail = c.get('/api/state').json()['detail']
    line = next(r.getMessage() for r in caplog.records if 'setup: open' in r.getMessage())

    # then
    assert (last.status_code, last.json()['detail']) == (409, 'Acme Ops needs at least one admin.')
    assert re.fullmatch(r'Acme Ops setup: open \S+/setup and enter the code [A-Z0-9]{4}-[A-Z0-9]{4}', line)
    assert detail == 'Acme Ops is not set up yet: open /setup'


def fields_of(text: str) -> dict[str, str]:
    return {name: 1 if spec else 'x' for _, name, spec, _ in string.Formatter().parse(text) if name and name != 'product'}


def test_no_message_names_the_product_by_hand():
    # then
    bare = {key: re.sub(r'SKOPAX_[A-Z_]+', '', text) for key, text in MESSAGES.items()}
    assert [key for key, text in bare.items() if 'skopax' in text.lower()] == []


def test_every_message_that_names_the_product_follows_the_display_name():
    # given
    brand.use('Acme Ops')

    # when
    shown = {key: msg(key, **fields_of(text)) for key, text in MESSAGES.items()}

    # then
    assert [key for key, text in MESSAGES.items() if '{product}' in text and 'Acme Ops' not in shown[key]] == []
    assert [key for key, text in shown.items() if 'Skopax' in text] == []
    assert len([key for key, text in MESSAGES.items() if '{product}' in text]) >= 25


def test_system_actor_is_system_and_displays_the_display_name():
    # given
    brand.use('Acme Ops')

    # then
    assert actors.SYSTEM == 'system' and engine.SYSTEM_VIA == 'system' and history.RESOLVE_VIAS[-1] == 'system'
    assert [notices.who_text(by) for by in ('system', 'server', 'auto', 'auto-resolve')] == ['Acme Ops'] * 4
    assert notices.who_text('bob') == 'bob' and notices.who_text('pod') == 'Investigator'
    assert statenotes.who_of('system') == 'Acme Ops' and statenotes.shown('system') == 'Acme Ops'
    assert history.resolution_text({'kind': 'handled', 'via': 'system', 'by': 'auto'}) == 'No action needed · via Acme Ops · auto-resolve'
    assert statenotes.via_label('system') == 'from Acme Ops' and statenotes.via_label('bob') == ''
    assert engine.auto_resolution('2026-11-01T00:00:00Z')['via'] == 'system'


def test_product_defaults():
    # when
    defaults = load_settings(env={})
    declared = Settings(**BARE)

    # then
    assert (defaults.mcp_client_id, defaults.sso_client_id, defaults.slack_channel) == ('skopax-mcp', 'skopax', '#skopax')
    assert (declared.mcp_client_id, declared.sso_client_id, declared.slack_channel) == ('skopax-mcp', 'skopax', '#skopax')


def test_mcp_client_id_is_read_like_the_other_settings():
    # when
    loaded = load_settings(env={'MCP_CLIENT_ID': 'acme-mcp'})

    # then
    assert loaded.mcp_client_id == 'acme-mcp'


def test_session_and_chat_secrets_use_the_new_prefixes():
    # given
    from skopax.sessions import derive_secret

    # then
    assert derive_secret('k') == hashlib.sha256(b'skopax-session:k').digest()
    assert chatui.secret_of(b'k') == hashlib.sha256(b'skopax-chat:k').hexdigest()


def test_the_name_in_text_sent_to_the_model_follows_the_display_name():
    # given
    from skopax.investigator import prompt
    from skopax.investigator.tools import READ_TOOLS, WRITE_TOOLS, filled

    brand.use('Acme Ops')

    # when
    texts = [prompt.gate_system(), prompt.small_talk_system(''), prompt.process_text(), prompt.page_gate_context([])]
    texts += [filled(t)['description'] for t in READ_TOOLS + WRITE_TOOLS]

    # then
    assert all('{product}' not in t for t in texts)
    assert 'Acme Ops' in texts[0] and 'Acme Ops' in texts[1] and 'Acme Ops' in texts[2] and texts[3] == 'No card: asked on the Acme Ops page.'
    assert json.dumps(texts).count('Acme Ops') > 20


def test_the_wordmark_is_the_svg_for_the_default_name_and_escaped_text_for_any_other():
    # given
    default_auto = brand.wordmark_html()
    default_dark = brand.wordmark_html('dark')
    brand.use("O'Brien Ops")
    named = brand.wordmark_html()

    # then
    assert default_auto.startswith('<span class="wordmark" role="img" aria-label="Skopax">')
    assert default_auto.count('<svg') == 2 and 'class="wm-light"' in default_auto and 'class="wm-dark"' in default_auto
    assert default_dark.count('<svg') == 1 and 'class="wm-light"' not in default_dark and '#e8eef5' in default_dark
    assert 'aria-label="Skopax"' not in default_auto.split('<svg', 1)[1], 'the inner svg is hidden from readers; the span carries the label'
    assert named == '<span class="wordmark wordmark-text" role="img" aria-label="O&#x27;Brien Ops">O&#x27;Brien Ops</span>'


def test_favicon_ico_is_open_and_holds_the_mark_at_16_32_and_48_pixels():
    # given
    with app_client(fresh_store()) as c:

        # when
        got = c.get('/favicon.ico')

    # then
    body = got.content
    sizes = [body[6 + 16 * i] for i in range(int.from_bytes(body[4:6], 'little'))]
    assert got.status_code == 200 and got.headers['content-type'] == 'image/x-icon'
    assert body[:4] == bytes([0, 0, 1, 0]) and sizes == [16, 32, 48]
    assert all(body[int.from_bytes(body[6 + 16 * i + 12:6 + 16 * i + 16], 'little'):][:8] == b'\x89PNG\r\n\x1a\n' for i in range(3))


def test_login_setup_and_the_page_header_show_the_wordmark_and_the_favicon_is_the_mark(store):
    # given
    client = admin_client(store)

    # when
    login = client.get('/login', headers=HTML).text
    page = client.get('/', headers=HTML).text
    with app_client(fresh_store()) as c:
        setup = c.get('/setup', headers=HTML).text
    mark = client.get('/icons/mark.svg')
    with app_client(store, display_name='Acme Ops') as c:
        named = c.get('/login', headers=HTML).text

    # then
    for text in (login, setup):
        assert '<span class="wordmark" role="img" aria-label="Skopax">' in text and 'wm-light' in text and 'wm-dark' in text
    assert '<a class="home" href="/" title="Main page"><span class="wordmark" role="img" aria-label="Skopax"><span class="wm-dark"><svg' in page
    for text in (page,):
        assert 'rel="icon"' in text and 'href="/icons/mark.svg"' in text and 'data:image/svg+xml' not in text.split('rel="icon"', 1)[1].split('>', 1)[0]
    assert mark.status_code == 200 and mark.headers['content-type'].startswith('image/svg+xml') and mark.text.startswith('<svg')
    assert '<span class="wordmark wordmark-text" role="img" aria-label="Acme Ops">Acme Ops</span>' in named and '<svg aria-hidden' not in named


def test_the_header_tile_is_the_beam_mark_only_for_a_custom_name(store):
    # given
    brand.use('Acme Ops')
    tile = brand.mark_tile_html()
    brand.use('Skopax')

    # then
    assert brand.mark_tile_html() == ''
    assert tile.startswith('<a class="mark home-mark" href="/" tabindex="-1" aria-hidden="true"><svg aria-hidden') and '#3cc8f0' in tile
    with app_client(store, display_name='Acme Ops') as c:
        assert 'class="mark home-mark"' in c.get('/', headers=HTML).text
    with app_client(store) as c:
        assert 'home-mark"' not in c.get('/', headers=HTML).text.split('</style>')[-1]


ASTRAL_NAMES = ['Caf\u00e9 Ops', 'Back\\slash Ops', "O'Brien Ops", 'Rocket \U0001F680 Ops']


def test_config_text_parses_for_any_allowed_display_name():
    # given
    names = ASTRAL_NAMES + [brand.clean_name(n) for n in ASTRAL_NAMES]

    # when
    parsed = {n: tomllib.loads(chatui.config_text('https://example.com', name=n))['UI'] for n in names}

    # then
    for name, ui in parsed.items():
        assert ui['name'] == name and name in ui['description']


def test_the_chat_mounts_with_an_astral_display_name(tmp_path):
    # given
    root = str(Path(__file__).resolve().parent.parent)
    env = {k: v for k, v in os.environ.items() if k != 'CHAINLIT_APP_ROOT'} | {'PYTHONPATH': root, 'PYTHONIOENCODING': 'utf-8'}

    # when
    run = subprocess.run([sys.executable, '-c', CHAT_SCRIPT, 'Rocket \U0001F680 Ops'], cwd=tmp_path, env=env, capture_output=True, text=True, encoding='utf-8', timeout=120)

    # then
    assert run.returncode == 0, run.stderr[-800:]
    assert '<title>Rocket \U0001F680 Ops</title>' in json.loads(run.stdout)['text']


def test_a_display_name_that_looks_like_a_data_placeholder_gets_no_data_blob(store):
    # given
    from skopax.app import BRAND_PLACEHOLDER, PLACEHOLDER

    # when
    pages = {}
    for name in (PLACEHOLDER, BRAND_PLACEHOLDER):
        client = admin_client(store, display_name=name)
        pages[name] = client.get('/', headers=HTML).text

    # then
    for name, page in pages.items():
        assert f'<title>{name}</title>' in page
        assert page.count('"anomalies"') == 1
        assert re.findall(r'<meta[^>]*content="([^"]*)"', page).count(name) >= 1
        assert all('"anomalies"' not in m for m in re.findall(r'<meta[^>]*>', page))


ADJECTIVAL = ('page', 'bot', 'card', 'investigator', 'state', 'service', 'log', 'nodes', 'chat', 'tab')
ARTICLED = re.compile(r"\b(?:the|this|a|an)\s+(?:\{\{product\}\}|\{product\})(?:'s\b|(?!\w)(?!\s+(?:" + '|'.join(ADJECTIVAL) + r")\b))", re.IGNORECASE)


def article_hits(text: str) -> list[str]:
    return [m[0] for m in ARTICLED.finditer(' '.join(text.split()))]


def test_no_text_shown_to_people_or_models_puts_an_article_before_the_name_as_a_noun():
    # given
    root = Path(__file__).resolve().parent.parent / 'skopax'
    sources = [p for p in root.rglob('*') if p.is_file() and p.suffix in ('.py', '.md', '.html', '.js')]

    # when
    hits = [f'{p.relative_to(root)}: {h}' for p in sources for h in article_hits(p.read_text(encoding='utf-8'))]

    # then
    assert len(sources) > 20
    assert hits == []


def test_the_article_guard_catches_double_braces_and_line_splits_and_spares_adjectival_uses():
    # then
    assert article_hits('no mentions (the {{product}} adds the one mention)') == ['the {{product}}']
    assert article_hits('lets the\n        {product} send') == ['the {product}']
    assert article_hits("'the '\n  'x' and A {product}\nposts") == ['A {product}']
    assert article_hits('the {product} page, the {{product}} bot and this {product}\n card') == []


def test_a_name_with_c1_controls_or_lone_surrogates_still_writes_the_chat_config():
    # given
    name = brand.clean_name('Ops \x85 \udc80 Team')

    # when
    parsed = tomllib.loads(chatui.config_text('https://example.com', name=name))['UI']

    # then
    assert name == 'Ops Team' and parsed['name'] == 'Ops Team'
    assert load_settings(env={'SKOPAX_DISPLAY_NAME': 'a\udc80b'}).display_name == 'ab'


NAMED = 'Acme Ops'
LINE = f'{NAMED} \u00b7 powered by Skopax {brand.display_version()}'
SITE = 'https://skopax.io'
CHAT_JSON = '/public/skopax-brand.json'
BRAND_DATA = re.compile(r'<script type="application/json" id="brand-data">\s*(.*?)\s*</script>', re.S)
SURFACES = ('/', '/login')


def hide_client(store, **over) -> TestClient:
    app = create_app(dataclasses.replace(settings(), display_name=NAMED, **over), store, registry=registry_with(switchable_entitlement))
    return TestClient(app, base_url='https://testserver', follow_redirects=False)


def carries(text: str) -> bool:
    return 'skopax.io' in text and 'powered by' in text


def bare(text: str) -> bool:
    return 'skopax.io' not in text and 'powered by' not in text


def brand_data(page: str) -> dict:
    return json.loads(BRAND_DATA.search(page)[1])


def chat_line() -> dict:
    return json.loads(chatui.brand_answer(CHAT_JSON, b'').body)


def surfaces(c: TestClient) -> dict[str, str]:
    return {path: c.get(path, headers=AUTH | HTML).text for path in SURFACES}


def hide_item(c: TestClient) -> dict:
    listed = c.get('/api/admin/features', headers=AUTH).json()
    return next(i for i in listed['items'] if i['id'] == 'hide-attribution') | {'running': listed['edition']}


def hide(c: TestClient, on: bool):
    return c.put('/api/admin/features/hide-attribution', json={'on': on}, headers=AUTH)


def shown_with(store, edition: str, stored: bool) -> tuple[bool, bool, bool, bool]:
    features = Features(Live(dataclasses.replace(settings(), hide_attribution=stored), TunableStore(store.db)), lambda: edition)
    brand.use_hidden(lambda: features.on('hide-attribution'))
    return brand.shown(), bool(brand.footer_html()), brand.page_info()['attribution'] is not None, brand.chat_brand()['attribution'] is not None


def test_shown_needs_enterprise_and_the_switch(store):
    # given
    cases = [(edition, stored) for edition in ('community', 'enterprise', 'gold') for stored in (False, True)]

    # when
    table = {case: shown_with(store, *case) for case in cases}
    brand.use_hidden(None)
    unset = (brand.shown(), bool(brand.footer_html()))

    # then
    assert {case for case, seen in table.items() if seen != (True, True, True, True)} == {('enterprise', True)}
    assert table[('enterprise', True)] == (False, False, False, False)
    assert unset == (True, True)


def test_a_failing_hide_check_keeps_the_line_and_warns_once(caplog):
    # given
    caplog.set_level(logging.WARNING, logger='skopax.brand')

    def broken() -> bool:
        raise RuntimeError('store down at mongodb://secret@example.com')

    brand.use_hidden(broken)

    # when
    seen = (brand.shown(), bool(brand.footer_html()), brand.page_info()['attribution'], brand.chat_brand()['attribution'])
    warned = [r.getMessage() for r in caplog.records if r.name == 'skopax.brand']

    # then
    assert seen == (True, True, {'text': 'Skopax 0.6.1 \u00b7 skopax.io', 'href': SITE}, {'text': 'Skopax 0.6.1 \u00b7 skopax.io', 'href': SITE})
    assert len(warned) == 1 and 'RuntimeError' in warned[0] and 'secret' not in warned[0]


def test_a_good_read_rearms_the_warning_for_the_next_outage(caplog):
    # given
    caplog.set_level(logging.WARNING, logger='skopax.brand')
    down = {'now': True}

    def flaky() -> bool:
        if down['now']:
            raise ConnectionError('store down')
        return False

    brand.use_hidden(flaky)

    # when
    first = [brand.shown(), brand.shown()]
    down['now'] = False
    good = brand.shown()
    down['now'] = True
    second = [brand.shown(), brand.shown()]
    warned = [r.getMessage() for r in caplog.records if r.name == 'skopax.brand']

    # then
    assert first + [good] + second == [True] * 5
    assert len(warned) == 2 and all('ConnectionError' in text for text in warned)


FOOTER_SOURCE = re.compile(r"^FOOTER = '(.+)'$", re.M)
HARNESS = Path(__file__).resolve().parent / 'page' / 'skopax-page.js'


def test_the_page_footers_carry_the_id_the_scripts_read_and_the_page_harness_renders_the_same_markup(store):
    # given
    client = admin_client(store, display_name="O'Brien Ops")
    text, href = brand.attribution_of("O'Brien Ops")
    harness_fill = brand.FOOTER.format(href=html.escape(href, quote=True), text=html.escape(text, quote=True))

    # when
    feet = [re.findall(r'<footer class="attribution">.*?</footer>', client.get(path, headers=HTML).text) for path in ('/',)]

    # then
    assert feet == [[brand.footer_html(), brand.footer_html(element_id=None)]]
    assert '<a href="https://skopax.io" id="attribution" target="_blank" rel="noopener noreferrer">' in feet[0][0] and 'O&#x27;Brien Ops' in feet[0][0]
    assert brand.footer_html() == harness_fill
    assert brand.footer_html(element_id=None) == harness_fill.replace(' id="attribution"', '')
    assert FOOTER_SOURCE.search(Path(brand.__file__).read_text(encoding='utf-8'))[1] == brand.FOOTER
    harness = HARNESS.read_text(encoding='utf-8')
    assert "/^FOOTER = '(.+)'$/m" in harness and '<footer' not in harness, 'the page harness fills the footer from brand.py, never from its own copy'


def harness_feet(view: dict) -> list[str]:
    node = shutil.which('node')
    if node is None:
        pytest.skip('node is not installed, so the page harness cannot run')
    script = 'process.stdout.write(require(process.argv[1])(null, {brand: JSON.parse(process.argv[2])}))'
    run = subprocess.run([node, '-e', script, str(HARNESS), json.dumps(view)], capture_output=True, text=True, encoding='utf-8', timeout=60, check=True)
    return re.findall(r'<footer class="attribution">.*?</footer>', run.stdout)


def test_the_page_harness_renders_the_side_and_phone_footers_the_server_renders(store):
    # given
    client = admin_client(store, display_name="O'Brien Ops")
    served = re.findall(r'<footer class="attribution">.*?</footer>', client.get('/', headers=HTML).text)

    # when
    rendered = harness_feet(brand.page_info())

    # then
    assert rendered == served == [brand.footer_html(), brand.footer_html(element_id=None)]
    assert 'O&#x27;Brien Ops' in rendered[1] and 'id=' not in rendered[1]


@pytest.mark.parametrize('bad', ['', 'attr-{n}', '{', 'two words', 'a"b', 0])
def test_the_footer_refuses_an_empty_or_invalid_id_whether_the_line_shows_or_not(bad):
    # when
    with pytest.raises(ValueError):
        brand.footer_html(element_id=bad)
    brand.use_hidden(lambda: True)

    # then
    with pytest.raises(ValueError):
        brand.footer_html(element_id=bad)


def test_the_footer_puts_a_plain_id_on_the_formatted_markup_and_none_leaves_it_out():
    # given
    text, href = brand.attribution_of(brand.product())
    filled = brand.FOOTER.format(href=html.escape(href, quote=True), text=html.escape(text, quote=True))

    # then
    assert brand.footer_html(element_id='side-attr_2') == filled.replace(' id="attribution"', ' id="side-attr_2"')
    assert brand.footer_html(element_id=None) == filled.replace(' id="attribution"', '')
    assert brand.footer_html() == filled


SIDE_ATTR = re.compile(r'<div class="side-attr" id="side-attr">(.*?)</div>', re.S)
END_ATTR = re.compile(r'<div class="end-attr" id="end-attr">(.*?)</div>', re.S)


def test_the_board_carries_one_attribution_id_and_its_phone_copy_carries_none(store):
    # given
    client = admin_client(store, display_name='Acme Ops')

    # when
    board = client.get('/', headers=HTML).text

    # then
    assert board.count('id="attribution"') == 1
    assert SIDE_ATTR.search(board)[1] == brand.footer_html()
    assert END_ATTR.search(board)[1] == brand.footer_html(element_id=None) == brand.footer_html().replace(' id="attribution"', '')
    assert brand.attribution_of('Acme Ops')[0] in END_ATTR.search(board)[1]
    assert '[[attribution' not in board


def test_with_the_line_hidden_the_foot_the_phone_copy_and_the_rail_mark_render_nothing(store):
    # given
    with hide_client(store) as c:
        before = c.get('/', headers=AUTH | HTML).text

        # when
        hide(c, True)
        board = c.get('/', headers=AUTH | HTML).text

    # then
    assert SIDE_ATTR.search(before)[1] and END_ATTR.search(before)[1] and brand_data(before)['attribution'] == {'text': LINE, 'href': SITE}
    assert (SIDE_ATTR.search(board)[1], END_ATTR.search(board)[1]) == ('', ''), 'the foot and the phone copy slots render nothing'
    assert brand_data(board)['attribution'] is None, 'the rail mark is drawn from this gated line, so it has nothing to draw'
    assert 'id="attribution"' not in board and bare(board)


def test_the_line_disappears_from_every_surface_with_enterprise(store):
    # given
    with hide_client(store) as c:
        shown = surfaces(c)
        shown_chat = chat_line()

        # when
        turned = hide(c, True)
        hidden = surfaces(c)
        hidden_chat = chat_line()
        item = hide_item(c)
    with hide_client(fresh_store(), hide_attribution=True) as s:
        setup = s.get('/setup', headers=HTML)
        setup_state = (s.app.state.features.edition(), s.app.state.features.stored('hide-attribution'))

    # then
    assert turned.status_code == 200 and (item['running'], item['stored'], item['locked'], item['on']) == ('enterprise', True, False, True)
    assert all(carries(text) for text in shown.values()) and shown_chat == {'attribution': {'text': LINE, 'href': SITE}}
    assert {path: bare(text) for path, text in hidden.items()} == {path: True for path in SURFACES}
    assert brand_data(hidden['/'])['attribution'] is None
    assert hidden_chat == {'attribution': None}
    assert setup.status_code == 200 and setup_state == ('enterprise', True) and bare(setup.text)


def test_the_line_returns_when_the_entitlement_drops_back(store, monkeypatch):
    # given
    with hide_client(store) as c:
        hide(c, True)
        hidden = surfaces(c)
        hidden_chat = chat_line()
        monkeypatch.setattr(switchable_entitlement, 'EDITION', 'community')

        # when
        c.app.state.registry.refresh()
        back = surfaces(c)
        back_chat = chat_line()
        item = hide_item(c)
    monkeypatch.setattr(switchable_entitlement, 'EDITION', 'enterprise')
    with hide_client(fresh_store(), hide_attribution=True) as s:
        setup_hidden = s.get('/setup', headers=HTML).text
        monkeypatch.setattr(switchable_entitlement, 'EDITION', 'community')
        s.app.state.registry.refresh()
        setup_back = s.get('/setup', headers=HTML).text

    # then
    assert all(bare(text) for text in hidden.values()) and hidden_chat == {'attribution': None} and bare(setup_hidden)
    assert all(carries(text) and LINE in text for text in back.values()) and carries(setup_back) and LINE in setup_back
    assert brand_data(back['/'])['attribution'] == {'text': LINE, 'href': SITE} and back_chat == {'attribution': {'text': LINE, 'href': SITE}}
    assert (item['running'], item['locked'], item['stored'], item['on']) == ('community', True, True, False)


def test_env_yaml_or_source_cannot_hide_the_line_on_community(store, tmp_path, monkeypatch):
    # given
    absent, file = str(tmp_path / 'absent.yaml'), tmp_path / 'skopax.yaml'
    file.write_text('settings:\n  core.hide-attribution: true\n', encoding='utf-8')
    cases = {'env': ({'SKOPAX_FEATURE_HIDE_ATTRIBUTION': 'true', FILE_ENV: absent}, {}),
             'yaml': ({FILE_ENV: str(file)}, {}),
             'source': ({FILE_ENV: absent}, {'values': {'core.hide-attribution': 'true'}})}
    seen = {}

    # when
    for name, (env, source) in cases.items():
        monkeypatch.setattr(demo_plugin, 'SOURCE', source)
        b = boot(env | {'SKOPAX_DISPLAY_NAME': NAMED}, registry_with(demo_plugin))
        with TestClient(create_app(dataclasses.replace(b.settings, token=TOKEN), store, registry=b.registry, sources=b.source_set())) as c:
            item = hide_item(c)
            page, login = c.get('/', headers=AUTH | HTML).text, c.get('/login', headers=HTML).text
            seen[name] = (b.settings.hide_attribution, item['running'], item['stored'], item['locked'], item['on'], carries(page) and LINE in page, carries(login) and LINE in login)

    # then
    assert seen == {name: (True, 'community', True, True, False, True, True) for name in cases}


def test_the_hide_switch_is_live_without_restart(store):
    # given
    with hide_client(store) as c:
        before = c.get('/', headers=HTML).text

        # when
        on = hide(c, True)
        hidden = c.get('/', headers=HTML).text
        off = hide(c, False)
        back = c.get('/', headers=HTML).text

    # then
    assert (on.status_code, on.json()['on'], off.status_code, off.json()['on']) == (200, True, 200, False)
    assert carries(before) and bare(hidden) and carries(back) and LINE in back


def test_the_sign_in_and_setup_pages_centre_the_attribution_line():
    # given
    pages = {name: (Path(brand.__file__).parent / 'web' / name).read_text(encoding='utf-8') for name in ('login.html', 'setup.html')}

    # then
    assert {name: '.attribution{margin-top:40px;font-size:.8rem;text-align:center}' in text for name, text in pages.items()} == {'login.html': True, 'setup.html': True}
