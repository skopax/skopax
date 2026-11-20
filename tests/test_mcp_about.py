import shutil
from pathlib import Path

from skopax import brand, mcpserver
from skopax.mcpauth import McpUser
from skopax.registry import Registry, standing_of
from skopax.store import Store
from tests.conftest import AUTH
from tests.plugins.base import minimal
from tests.test_api_tokens import admin_client
from tests.test_mcp import NINA, board, call, cards, mcp_client, mcp_settings, rpc, token

VERSION = brand.display_version()
ICON = Path(__file__).parent / 'plugins' / 'demo-icon.svg'
DOT = chr(183)


def user(role: str) -> McpUser:
    return McpUser(name=NINA, sub=NINA, email=NINA, role=role)


def installed(tmp_path, monkeypatch, dist: str = 'acme-layer', version: str = '2.3', ids: tuple[str, ...] = ('acme-pager', 'acme-audit'), broken: tuple[str, ...] = ()):
    module = dist.replace('-', '_')
    shutil.copy(ICON, tmp_path / 'demo-icon.svg')
    lines = ['from tests.plugins.base import minimal\n'] + [f"P{i} = minimal('{pid}', '{pid}')\n" for i, pid in enumerate(ids)]
    (tmp_path / f'{module}.py').write_text(''.join(lines), encoding='utf-8')
    info = tmp_path / f'{module}-{version}.dist-info'
    info.mkdir()
    (info / 'METADATA').write_text(f'Metadata-Version: 2.1\nName: {dist}\nVersion: {version}\n', encoding='utf-8')
    points = ''.join(f'{pid} = {module}:P{i}\n' for i, pid in enumerate(ids)) + ''.join(f'{pid} = {module}_missing:PLUGIN\n' for pid in broken)
    (info / 'entry_points.txt').write_text(f'[skopax.plugins]\n{points}', encoding='utf-8')
    monkeypatch.syspath_prepend(str(tmp_path))


def dist_info(tmp_path, dist: str, version: str, top: str, files: tuple[str, ...] = ()) -> None:
    info = tmp_path / f'{dist.replace("-", "_")}-{version}.dist-info'
    info.mkdir()
    (info / 'METADATA').write_text(f'Metadata-Version: 2.1\nName: {dist}\nVersion: {version}\n', encoding='utf-8')
    (info / 'top_level.txt').write_text(top + '\n', encoding='utf-8')
    (info / 'RECORD').write_text(''.join(f'{f},,\n' for f in files), encoding='utf-8')


def tools_with(store: Store, registry, plugin_on=lambda _: True) -> mcpserver.Tools:
    bridge = mcpserver.Bridge(store=store, settings=mcp_settings(), chat=lambda *a: {}, chat_view=lambda chat: chat, act=lambda *a: {}, assign=lambda *a: {},
                              card_view=lambda *a: {}, board=lambda now: {}, kick=lambda: None, chat_used=lambda by: 0, base='https://skopax.example.com/', registry=registry, plugin_on=plugin_on)
    return mcpserver.Tools(bridge, poll_s=0.01)


class Moving:
    def __init__(self):
        self.layers = []

    def extensions(self):
        return list(self.layers)

    def edition(self):
        return 'community'

    def standing(self, enabled):
        return []


class Odd(Moving):
    def standing(self, enabled):
        return [{'id': 'x', 'version': '1', 'state': 'weird', 'reason': ''}, {'id': 'y', 'version': '1', 'state': 'ok', 'reason': ''}]


def test_the_instructions_open_with_who_runs_the_server():
    # given
    layers = [{'name': 'acme-layer', 'version': '2.3', 'package': True, 'plugins': ['acme-pager']}]

    # when
    plain = mcpserver.instructions('', 'Skopax')
    renamed = mcpserver.instructions('', 'Acme Ops')
    extended = mcpserver.instructions('', 'Acme Ops', layers)
    extended_plain = mcpserver.instructions('', 'Skopax', layers)

    # then
    assert plain.startswith(f'Skopax {VERSION}. ') and 'powered by' not in plain
    assert renamed.startswith(f'Acme Ops, powered by Skopax {VERSION}. ') and 'extended by' not in renamed
    assert extended.startswith(f'Acme Ops, powered by Skopax {VERSION}, extended by acme-layer 2.3. ')
    assert extended_plain.startswith(f'Skopax {VERSION}, extended by acme-layer 2.3. ')
    assert 'The about tool' in plain and 'Start with list_cards' in plain


def test_the_instructions_cap_the_extending_layers():
    # given
    layers = [{'name': f'layer-{i}', 'version': '1', 'package': True, 'plugins': []} for i in range(8)]

    # when
    text = mcpserver.instructions('', 'Skopax', layers)

    # then
    assert 'layer-4 1 and 3 more.' in text and 'layer-5' not in text


def test_a_hidden_attribution_leaves_no_skopax_name_version_or_site_in_the_instructions_and_about(store: Store):
    # given
    brand.use('Acme Ops')
    brand.use_hidden(lambda: True)
    tools = tools_with(store, Registry.of(minimal('fine', 'Fine')))

    # when
    try:
        text = mcpserver.instructions('', 'Acme Ops', [{'name': 'acme-layer', 'version': '2.3', 'package': True, 'plugins': []}])
        about = tools.about(user('viewer'), {})
        options = mcpserver.build_server(tools).create_initialization_options()
    finally:
        brand.use_hidden(None)
        brand.use('Skopax')

    # then
    assert text.startswith('Acme Ops, extended by acme-layer 2.3. The about tool')
    assert 'Skopax' not in text and 'skopax.io' not in text
    assert about.splitlines()[:3] == ['Acme Ops.', 'edition: community', 'server: https://skopax.example.com']
    assert 'Skopax' not in about and 'skopax.io' not in about and VERSION not in about
    assert f'- fine 0.1.0 {DOT} ok' in about.splitlines()
    assert options.website_url is None and options.server_version


def test_the_identity_sentence_is_read_again_for_every_session(store: Store):
    # given
    registry = Moving()
    server = mcpserver.build_server(tools_with(store, registry))
    before = server.create_initialization_options().instructions

    # when
    registry.layers = [{'name': 'acme-layer', 'version': '2.3', 'package': True, 'plugins': ['acme-pager']}]
    after = server.create_initialization_options().instructions

    # then
    assert before.startswith(f'Skopax {VERSION}. ') and after.startswith(f'Skopax {VERSION}, extended by acme-layer 2.3. ')


def test_a_plugin_from_another_package_is_an_extension_and_built_ins_are_not(tmp_path, monkeypatch, store: Store):
    # given
    installed(tmp_path, monkeypatch)
    registry = Registry.load({}, builtin=('tests.plugins.demo_plugin',))

    # when
    layers = registry.extensions()
    options = mcpserver.build_server(tools_with(store, registry)).create_initialization_options()

    # then
    assert layers == [{'name': 'acme-layer', 'version': '2.3', 'package': True, 'plugins': ['acme-audit', 'acme-pager']}]
    assert 'extended by acme-layer 2.3.' in options.instructions


def test_a_broken_external_plugin_stays_under_its_package_and_a_broken_built_in_never_shows(tmp_path, monkeypatch):
    # given
    installed(tmp_path, monkeypatch, ids=('acme-pager',), broken=('acme-gone',))
    registry = Registry.load({}, builtin=('tests.plugins.demo_plugin', 'tests.plugins.no_such_module'))

    # when
    layers = registry.extensions()
    states = {r['id']: r['state'] for r in registry.standing(lambda _: True)}

    # then
    assert layers == [{'name': 'acme-layer', 'version': '2.3', 'package': True, 'plugins': ['acme-gone', 'acme-pager']}]
    assert states['acme-gone'] == 'failed' and states['tests.plugins.no_such_module'] == 'failed'


def test_an_entry_point_of_the_skopax_package_itself_is_not_an_extension(tmp_path, monkeypatch):
    # given
    installed(tmp_path, monkeypatch, dist='skopax', version='0.4.3', ids=('own-plugin',))
    registry = Registry.load({}, builtin=())

    # then
    assert registry.extensions() == []


def test_a_plugin_from_the_env_list_without_a_package_shows_its_id_and_version():
    # given
    registry = Registry.load({'SKOPAX_PLUGINS': 'tests.plugins.demo_plugin'}, builtin=(), entry_points=False)

    # when
    layers = registry.extensions()

    # then
    assert layers == [{'name': 'demo', 'version': '0.1.0', 'package': False, 'plugins': ['demo']}]


def test_an_env_plugin_is_credited_to_the_package_whose_file_list_holds_its_module(tmp_path, monkeypatch):
    # given
    shutil.copy(ICON, tmp_path / 'demo-icon.svg')
    (tmp_path / 'envlayer.py').write_text("from tests.plugins.base import minimal\n\nPLUGIN = minimal('env-layer', 'Env layer')\n", encoding='utf-8')
    dist_info(tmp_path, 'env-layer-dist', '1.4', 'envlayer', ('envlayer.py',))
    monkeypatch.syspath_prepend(str(tmp_path))

    # when
    registry = Registry.load({'SKOPAX_PLUGINS': 'envlayer'}, builtin=(), entry_points=False)

    # then
    assert registry.extensions() == [{'name': 'env-layer-dist', 'version': '1.4', 'package': True, 'plugins': ['env-layer']}]


def test_an_env_plugin_is_not_credited_to_a_package_that_only_shares_the_top_level_name(tmp_path, monkeypatch):
    # given
    shutil.copy(ICON, tmp_path / 'demo-icon.svg')
    (tmp_path / 'sharedname.py').write_text("from tests.plugins.base import minimal\n\nPLUGIN = minimal('shared', 'Shared')\n", encoding='utf-8')
    dist_info(tmp_path, 'other-dist', '9.9', 'sharedname', ('othermodule.py',))
    monkeypatch.syspath_prepend(str(tmp_path))

    # when
    registry = Registry.load({'SKOPAX_PLUGINS': 'sharedname'}, builtin=(), entry_points=False)

    # then
    assert registry.extensions() == [{'name': 'shared', 'version': '0.1.0', 'package': False, 'plugins': ['shared']}]


def test_about_for_a_viewer_lists_the_facts_without_reasons_and_paths(tmp_path, monkeypatch, store: Store):
    # given
    installed(tmp_path, monkeypatch)
    registry = Registry.load({}, builtin=('tests.plugins.demo_plugin',))
    registry.mark_broken('acme-pager', 'pager broke at ' + str(tmp_path))
    tools = tools_with(store, registry, plugin_on=lambda pid: pid != 'acme-audit')

    # when
    text = tools.about(user('viewer'), {})

    # then
    assert text.splitlines() == [
        f'Skopax {VERSION}.', 'edition: community', 'site: https://skopax.io', 'server: https://skopax.example.com',
        'Extended by:', f'- acme-layer 2.3 {DOT} plug-ins: acme-audit, acme-pager',
        f'Plug-ins: 3 {DOT} ok 1 {DOT} off 1 {DOT} failed 1',
        f'- acme-audit 0.1.0 {DOT} off', f'- acme-pager 0.1.0 {DOT} failed', f'- demo 0.1.0 {DOT} ok']
    assert str(tmp_path) not in text


def test_about_for_a_renamed_install_says_powered_by_skopax(store: Store):
    # given
    brand.use('Acme Ops')

    # when
    try:
        text = tools_with(store, Registry.of(minimal('fine', 'Fine'))).about(user('viewer'), {})
    finally:
        brand.use('Skopax')

    # then
    assert text.splitlines()[:3] == [f'Acme Ops, powered by Skopax {VERSION}.', 'edition: community', 'site: https://skopax.io']


def test_about_for_an_admin_adds_the_reason_a_plugin_is_not_ok_and_a_viewer_does_not(store: Store):
    # given
    registry = Registry.of(minimal('fine', 'Fine'), minimal('late', 'Late', core='>=9,<10'), minimal('gated', 'Gated', edition='enterprise'), minimal('hurt', 'Hurt'))
    registry.mark_broken('hurt', 'bad field')
    reasons = {x.id: x.reason for x in registry.loaded()}
    tools = tools_with(store, registry)

    # when
    admin = tools.about(user('admin'), {}).splitlines()
    operator = tools.about(user('operator'), {}).splitlines()

    # then
    assert reasons['gated'] and reasons['hurt'] and reasons['late']
    assert f'Plug-ins: 4 {DOT} ok 1 {DOT} incompatible 1 {DOT} locked 1 {DOT} failed 1' in admin
    assert admin[-4:] == [f"- gated 0.1.0 {DOT} locked {DOT} {reasons['gated']}", f"- hurt 0.1.0 {DOT} failed {DOT} {reasons['hurt']}",
                          f"- late 0.1.0 {DOT} incompatible {DOT} {reasons['late']}", f'- fine 0.1.0 {DOT} ok']
    assert operator[-4:] == [f'- gated 0.1.0 {DOT} locked', f'- hurt 0.1.0 {DOT} failed', f'- late 0.1.0 {DOT} incompatible', f'- fine 0.1.0 {DOT} ok']


def test_a_state_outside_the_known_ones_is_counted_under_its_own_name(store: Store):
    # when
    text = tools_with(store, Odd()).about(user('viewer'), {})

    # then
    assert f'Plug-ins: 2 {DOT} ok 1 {DOT} weird 1' in text.splitlines()


def test_about_caps_the_plugin_list_and_says_how_many_more(store: Store):
    # given
    registry = Registry.of(*[minimal(f'p{i:02d}', f'P{i}') for i in range(mcpserver.ABOUT_PLUGINS + 7)])

    # when
    text = tools_with(store, registry).about(user('viewer'), {})

    # then
    assert f'Plug-ins: {mcpserver.ABOUT_PLUGINS + 7} {DOT} ok {mcpserver.ABOUT_PLUGINS + 7}' in text
    assert len([line for line in text.splitlines() if line.startswith('- p')]) == mcpserver.ABOUT_PLUGINS
    assert text.splitlines()[-1] == 'and 7 more'


def test_about_without_a_registry_still_names_the_version_and_edition(store: Store):
    # when
    text = tools_with(store, None).about(user('viewer'), {})

    # then
    assert f'Skopax {VERSION}.' in text and 'edition: community' in text and f'Plug-ins: 0 {DOT} ok 0' in text


def test_the_admin_plugin_list_and_about_derive_the_state_the_same_way(store: Store):
    # given
    client = admin_client(store)
    registry = Registry.of(minimal('aa', 'Aa'), minimal('bb', 'Bb'))

    # when
    rows = client.get('/api/admin/plugins', headers=AUTH).json()['plugins']

    # then
    assert rows and all(r['standing'] in ('ok', 'off', 'incompatible', 'locked', 'failed') for r in rows)
    assert [standing_of(x, lambda pid: pid != 'bb') for x in registry.loaded()] == ['ok', 'off']


def test_about_is_a_read_only_viewer_tool():
    # given
    tool = next(t for t in mcpserver.TOOLS if t.name == 'about')

    # then
    assert mcpserver.TOOL_ROLES['about'] == 'viewer' and tool.title == 'About'
    assert (tool.annotations.readOnlyHint, tool.annotations.destructiveHint, tool.annotations.idempotentHint) == (True, False, True)
    assert tool.inputSchema['properties'] == {} and '{product}' in tool.description


def test_a_viewer_reaches_about_over_mcp_and_the_handshake_carries_the_sentence(store: Store):
    # given
    board(store, *cards())

    # when
    with mcp_client(store) as c:
        init = rpc(c, 'initialize', {'protocolVersion': '2025-06-18', 'capabilities': {}, 'clientInfo': {'name': 'x', 'version': '1'}}, token(roles=('SKOPAX_MCP',)))
        text, failed = call(c, token(roles=('SKOPAX_MCP',)), 'about')

    # then
    assert init.json()['result']['instructions'].startswith(f'Skopax {VERSION}. ')
    assert failed is False and text.startswith(f'Skopax {VERSION}.\nedition: ') and 'Plug-ins: ' in text and '- jira ' in text
