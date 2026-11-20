import dataclasses
import json
import logging
import re
import shutil
from pathlib import Path

import pytest

import skopax
from skopax import contract, engine, jiraaccess, probe, promqlchecks, registry, selfreview, slackhealth
from skopax.contract import AgentRead, Capabilities, Check, Field, Hooks, Knowledge, PassResult, ReadError, Setup, Sweep, Tool
from skopax.registry import Registry
from tests.plugins import demo_plugin, enterprise_plugin, entitled_plugin, gold_plugin, old_core_plugin, order_a, order_b, order_c
from tests.plugins.base import minimal

DEMO = demo_plugin.PLUGIN
HERE = Path(__file__).parent / 'plugins'

OBJECT = {'type': 'object', 'properties': {}}


def variant(**over):
    return dataclasses.replace(DEMO, **over)


def solo(plugin, **kw):
    return Registry.of(plugin, **kw).loaded()[0]


def other(id: str, ran: tuple[str, ...] | None = None, **over):
    return minimal(id, id.title(), checks=(Check(id, id, 900, 0, ran or (id,), demo_plugin.pulse),), **over)


def test_demo_plugin_loads_with_its_identity():
    # given
    reg = Registry.of(DEMO)

    # when
    loaded = reg.get('demo')
    manifest = registry.describe(loaded.plugin)

    # then
    assert (loaded.state, loaded.reason, loaded.id) == ('ok', None, 'demo')
    assert (manifest['author'], manifest['licence'], manifest['core'], manifest['edition']) == ('Skopax contributors', 'Apache-2.0', '>=0.1,<1', 'community')
    assert manifest['capabilities'] == {'hosts': ['@url'], 'secrets': ['token'], 'agentReads': True}
    assert json.loads(json.dumps(manifest)) == manifest
    assert reg.usable() == [DEMO]


def test_describe_lists_every_contribution_and_no_callable():
    # given
    manifest = registry.describe(DEMO)

    # when
    text = json.dumps(manifest)

    # then
    assert [f['key'] for f in manifest['fields']] == ['url', 'token', 'ca', 'mode']
    assert manifest['checks'] == [{'id': 'pulse', 'label': 'Demo pulse', 'period': 900, 'offset': 0, 'ran': ['demo']}]
    assert [r['id'] for r in manifest['rules']] == ['limits']
    assert [s['key'] for s in manifest['settings']] == ['depth']
    assert manifest['tools'] == [{'name': 'demo_echo', 'kind': 'read', 'description': 'Echo text back'}, {'name': 'demo_write', 'kind': 'write', 'description': 'Write text somewhere'},
                                 {'name': 'demo_propose', 'kind': 'propose', 'description': 'Propose a demo change'}]
    assert manifest['knowledge'] == ['demo-notes']
    assert manifest['panels'] == [{'tab': 'demo', 'script': 'demo-panel.js', 'fragment': None, 'label': '', 'group': 'observe'}]
    assert manifest['locales'] == ['de']
    assert manifest['hooks'] == ['role_resolver', 'settings_source', 'oidc_token_request', 'org_validators', 'entitlement']
    assert manifest['migrations'] == [1, 2]
    assert manifest['agentReads'] == [{'op': 'demo-echo', 'args': ['text'], 'keys': ['url', 'token']}]
    assert 'function' not in text and 'object at' not in text


def test_entry_points_and_env_list_are_discovered(tmp_path, monkeypatch):
    # given
    (tmp_path / 'demo_ep.py').write_text("from tests.plugins.base import minimal\n\nPLUGIN = minimal('demo-ep', 'Demo EP')\n", encoding='utf-8')
    shutil.copy(HERE / 'demo-icon.svg', tmp_path / 'demo-icon.svg')
    info = tmp_path / 'demo_ep-0.1.dist-info'
    info.mkdir()
    (info / 'METADATA').write_text('Metadata-Version: 2.1\nName: demo-ep\nVersion: 0.1\n', encoding='utf-8')
    (info / 'entry_points.txt').write_text('[skopax.plugins]\ndemo-ep = demo_ep:PLUGIN\n', encoding='utf-8')
    monkeypatch.syspath_prepend(str(tmp_path))

    # when
    by_entry_point = Registry.load({}, builtin=())
    by_env = Registry.load({'SKOPAX_PLUGINS': 'tests.plugins.demo_plugin'}, builtin=(), entry_points=False)
    both = Registry.load({'SKOPAX_PLUGINS': ' tests.plugins.demo_plugin , '}, builtin=())

    # then
    assert [(x.id, x.origin, x.state) for x in by_entry_point.loaded() if x.id == 'demo-ep'] == [('demo-ep', 'entry-point', 'ok')]
    assert [(x.id, x.origin, x.state) for x in by_env.loaded()] == [('demo', 'env', 'ok')]
    assert [(x.id, x.origin) for x in both.loaded() if x.id in ('demo', 'demo-ep')] == [('demo', 'env'), ('demo-ep', 'entry-point')]
    assert by_entry_point.icon('demo-ep').startswith('<svg')


def test_builtin_modules_load_first_and_a_module_listed_twice_loads_once():
    # given
    env = {'SKOPAX_PLUGINS': 'tests.plugins.demo_plugin'}

    # when
    reg = Registry.load(env, builtin=('tests.plugins.demo_plugin',), entry_points=False)

    # then
    assert [(x.id, x.origin, x.state) for x in reg.loaded()] == [('demo', 'builtin', 'ok')]


def test_load_order_is_name_order_with_after():
    # given
    plain = Registry.of(order_c.PLUGIN, order_b.PLUGIN, dataclasses.replace(order_a.PLUGIN, after=()))

    # when
    after = Registry.of(order_c.PLUGIN, order_b.PLUGIN, order_a.PLUGIN)
    absent = Registry.of(order_b.PLUGIN, order_a.PLUGIN)

    # then
    assert [x.id for x in plain.loaded()] == ['order-a', 'order-b', 'order-c']
    assert [x.id for x in after.loaded()] == ['order-b', 'order-c', 'order-a']
    assert [x.id for x in absent.loaded()] == ['order-a', 'order-b']


def test_an_after_cycle_breaks_every_plugin_in_it_and_names_the_cycle():
    # given
    b = dataclasses.replace(order_b.PLUGIN, after=('order-c',))
    c = dataclasses.replace(order_c.PLUGIN, after=('order-b',))
    tail = minimal('order-d', 'Order D', after=('order-b',))

    # when
    reg = Registry.of(b, c, tail, order_a.PLUGIN)
    states = {x.id: x for x in reg.loaded()}

    # then
    assert (states['order-b'].state, states['order-c'].state) == ('broken', 'broken')
    assert 'order-b -> order-c -> order-b' in states['order-b'].reason and 'order-c -> order-b -> order-c' in states['order-c'].reason
    assert (states['order-a'].state, states['order-d'].state) == ('ok', 'ok')
    assert [x.id for x in reg.usable()] == ['order-a', 'order-d']


def test_import_failure_is_listed_not_fatal():
    # given
    env = {'SKOPAX_PLUGINS': 'tests.plugins.broken_plugin,tests.plugins.demo_plugin,tests.plugins.missing_module'}

    # when
    reg = Registry.load(env, builtin=(), entry_points=False)
    states = {x.id: x for x in reg.loaded()}

    # then
    assert states['tests.plugins.broken_plugin'].state == 'broken'
    assert 'ImportError' in states['tests.plugins.broken_plugin'].reason and 'example-sdk' not in states['tests.plugins.broken_plugin'].reason
    assert states['tests.plugins.missing_module'].state == 'broken' and 'ModuleNotFoundError' in states['tests.plugins.missing_module'].reason
    assert states['demo'].state == 'ok'
    assert [p.id for p in reg.usable()] == ['demo']


def test_a_module_without_a_plugin_object_is_broken(tmp_path, monkeypatch):
    # given
    (tmp_path / 'no_plugin_here.py').write_text('X = 1\n', encoding='utf-8')
    (tmp_path / 'wrong_object.py').write_text('PLUGIN = 5\n', encoding='utf-8')
    monkeypatch.syspath_prepend(str(tmp_path))

    # when
    reg = Registry.load({'SKOPAX_PLUGINS': 'no_plugin_here,wrong_object'}, builtin=(), entry_points=False)

    # then
    assert [(x.id, x.state) for x in reg.loaded()] == [('no_plugin_here', 'broken'), ('wrong_object', 'broken')]
    assert reg.get('no_plugin_here').reason == 'no_plugin_here was not loaded: module no_plugin_here has no PLUGIN object'


def test_core_version_range_is_checked():
    # given
    reg = Registry.of(old_core_plugin.PLUGIN, DEMO)

    # when
    loaded = reg.get('old-core-demo')

    # then
    assert loaded.state == 'incompatible'
    assert skopax.__version__ == '0.4.2' and '0.4.2' in loaded.reason and '>=9' in loaded.reason
    assert [p.id for p in reg.usable()] == ['demo']


@pytest.mark.parametrize('core, state', [('>=0.1,<0.2', 'ok'), ('<0.1', 'incompatible'), ('>=0.2', 'incompatible'), ('==0.1.0rc1', 'ok'), ('>=0.1.0rc2', 'incompatible'),
                                         ('!=0.1.0rc1', 'incompatible'), ('==0.1.*', 'ok'), ('==0.2.*', 'incompatible'), ('~=0.1.0rc1', 'ok')])
def test_a_release_candidate_core_is_checked_as_its_release(monkeypatch, core, state):
    # given
    monkeypatch.setattr(skopax, '__version__', '0.1.0rc1')
    reg = Registry.of(minimal('rc-core-demo', 'Rc core demo', core=core))

    # when
    loaded = reg.get('rc-core-demo')

    # then
    assert loaded.state == state
    assert loaded.reason == (None if state == 'ok' else f'Rc core demo needs Skopax {core}, installed: 0.1.0rc1')


@pytest.mark.parametrize('core, state', [('>=0.1.0.post1', 'ok'), ('==0.1.0.post1', 'ok'), ('==0.1.0', 'incompatible'), ('>=0.1,<0.2', 'ok')])
def test_a_post_release_core_is_checked_as_itself(monkeypatch, core, state):
    # given
    monkeypatch.setattr(skopax, '__version__', '0.1.0.post1')
    reg = Registry.of(minimal('post-core-demo', 'Post core demo', core=core))

    # when
    loaded = reg.get('post-core-demo')

    # then
    assert loaded.state == state


def test_enterprise_plugin_is_listed_but_locked_until_an_entitlement_says_enterprise():
    # given
    ee = enterprise_plugin.PLUGIN

    # when
    bare = Registry.of(ee)
    entitled = Registry.of(ee, entitled_plugin.PLUGIN)
    gold = Registry.of(ee, gold_plugin.PLUGIN)

    # then
    assert (bare.get('enterprise-demo').state, bare.edition(), bare.usable()) == ('locked', 'community', [])
    assert 'Enterprise' in bare.get('enterprise-demo').reason
    assert (entitled.get('enterprise-demo').state, entitled.get('enterprise-demo').reason, entitled.edition()) == ('ok', None, 'enterprise')
    assert [p.id for p in entitled.usable()] == ['enterprise-demo', 'entitled-demo']
    assert (gold.get('enterprise-demo').state, gold.edition()) == ('locked', 'community')


def test_a_failing_or_second_entitlement_provider_is_survived_with_one_warning(caplog):
    # given
    def boom():
        raise RuntimeError('licence server down')

    failing = dataclasses.replace(entitled_plugin.PLUGIN, hooks=Hooks(entitlement=boom))
    reg = Registry.of(enterprise_plugin.PLUGIN, failing)
    twin = Registry.of(entitled_plugin.PLUGIN, gold_plugin.PLUGIN)

    # when
    with caplog.at_level(logging.WARNING, logger='skopax.registry'):
        first, second = reg.edition(), reg.edition()
        winner = twin.edition()
        twin.edition()

    # then
    assert (first, second, winner) == ('community', 'community', 'enterprise')
    assert reg.get('enterprise-demo').state == 'locked'
    assert len([r for r in caplog.records if 'RuntimeError' in r.getMessage()]) == 1
    assert not [r for r in caplog.records if 'licence server down' in r.getMessage()]
    assert len([r for r in caplog.records if 'gold-demo' in r.getMessage()]) == 1


def test_a_plugin_with_an_entitlement_hook_must_be_community():
    # given
    greedy = dataclasses.replace(entitled_plugin.PLUGIN, edition='enterprise')

    # when
    reg = Registry.of(greedy)

    # then
    assert reg.get('entitled-demo').state == 'broken'
    assert reg.edition() == 'community'


def test_capabilities_must_cover_secret_fields():
    # given
    bare = variant(capabilities=Capabilities(hosts=('@url',), secrets=(), agent_reads=True))
    agent = variant(agent_reads=(AgentRead('demo-echo', ('text',), ('url',), demo_plugin.agent_sources, demo_plugin.agent_echo, fields=(Field('api-key', 'API key', 'secret'),)),))

    # then
    assert solo(bare).state == 'broken' and 'token' in solo(bare).reason
    assert solo(agent).state == 'broken' and 'api-key' in solo(agent).reason


@pytest.mark.parametrize('over,fragment', [
    ({'id': 'Demo'}, 'valid id'),
    ({'id': 'x'}, 'valid id'),
    ({'id': 'core'}, 'reserved'),
    ({'id': 'small-talk'}, 'reserved'),
    ({'version': '1.0'}, 'major.minor.patch'),
    ({'category': 'weather'}, 'category'),
    ({'edition': 'gold'}, 'edition'),
    ({'author': ''}, 'author'),
    ({'licence': ''}, 'licence'),
    ({'icon': ''}, 'icon'),
    ({'icon': 'nothing.svg'}, 'icon'),
    ({'icon': 'demo-panel.js'}, 'icon'),
    ({'core': 'nonsense'}, 'not valid'),
    ({'fields': (Field('url', 'URL', 'url'), Field('name', 'Name', 'text'))}, 'field key'),
    ({'fields': (Field('url', 'URL', 'url'), Field('primary', 'Primary', 'bool'))}, 'field key'),
    ({'fields': (Field('url', 'URL', 'url'), Field('Bad_Key', 'Bad', 'text'))}, 'field key'),
    ({'fields': (Field('url', 'URL', 'url'), Field('url', 'URL again', 'url'))}, 'repeated'),
    ({'fields': (Field('url', 'URL', 'url'), Field('mode', 'Mode', 'colour'))}, 'unknown kind'),
    ({'fields': (Field('url', 'URL', 'url'), Field('ca', 'CA', 'pem', show_if=('mode', 'slow')))}, 'depends on'),
    ({'fields': (Field('url', 'URL', 'url'), Field('auth', 'Auth', 'choice', choices=('none', 'basic'), choice_labels={'bearer': 'Bearer token'}))}, 'choice labels'),
    ({'fields': (Field('url', 'URL', 'url'), Field('auth', 'Auth', 'choice', choices=('none', 'basic'), choice_labels={'none': ''}))}, 'choice labels'),
    ({'fields': (Field('url', 'URL', 'url'), Field('auth', 'Auth', 'choice', choices=('none', 'basic'), choice_labels={'none': 'n' * 41}))}, 'choice labels'),
    ({'fields': (Field('url', 'URL', 'url'), Field('auth', 'Auth', 'choice', choices=('none', 'basic'), choice_labels={'none': 'No' + chr(10) + 'sign-in'}))}, 'choice labels'),
    ({'fields': (Field('url', 'URL', 'url'), Field('auth', 'Auth', 'choice', choices=('none', 'basic'), choice_labels={'none': '   '}))}, 'choice labels'),
    ({'fields': (Field('url', 'URL', 'url'), Field('auth', 'Auth', 'choice', choices=('none', 'basic'), choice_labels={'none': 5}))}, 'choice labels'),
    ({'fields': (Field('url', 'URL', 'url'), Field('auth', 'Auth', 'choice', choices=('none', 'basic'), choice_labels=(('none', 'None'),)))}, 'choice labels'),
    ({'fields': (Field('url', 'URL', 'url'), Field('on', 'On', 'bool', choice_labels={'true': 'Yes'}))}, 'choice labels'),
    ({'fields': (Field('url', 'URL', 'url', group='Connection'),)}, 'group'),
    ({'fields': (Field('url', 'URL', 'url', group='sign in'),)}, 'group'),
    ({'fields': (Field('url', 'URL', 'url', group='g' * 25),)}, 'group'),
    ({'fields': (Field('url', 'URL', 'url', group='-x'),)}, 'group'),
    ({'fields': (Field('url', 'URL', 'url', group=None),)}, 'group'),
    ({'capabilities': Capabilities(hosts=('@nope',), secrets=('token',), agent_reads=True)}, 'URL field'),
    ({'capabilities': Capabilities(hosts=('@token',), secrets=('token',), agent_reads=True)}, 'URL field'),
    ({'capabilities': Capabilities(hosts=('@url',), secrets=('token',), agent_reads=False)}, 'agent reads'),
    ({'agent_reads': ()}, 'agent reads'),
    ({'checks': (Check('pulse', 'A', 900, 0, ('demo',), demo_plugin.pulse), Check('pulse', 'B', 900, 0, ('demo-b',), demo_plugin.pulse))}, 'repeated'),
    ({'checks': (Check('pulse', 'A', 900, 0, (), demo_plugin.pulse),)}, 'no ran keys'),
    ({'tools': (Tool('demo_echo', 'x', OBJECT, demo_plugin.echo, kind='destroy'),)}, 'unknown kind'),
    ({'tools': (Tool('demo_echo', 'x', OBJECT, demo_plugin.echo), Tool('demo_echo', 'y', OBJECT, demo_plugin.echo))}, 'already taken'),
    ({'tools': (Tool('bad name', 'x', OBJECT, demo_plugin.echo),)}, 'tool name'),
    ({'tools': (Tool('x' * 65, 'x', OBJECT, demo_plugin.echo),)}, 'tool name'),
    ({'tools': (Tool('demo_echo', '  ', OBJECT, demo_plugin.echo),)}, 'needs a description'),
    ({'tools': (Tool('demo_echo', None, OBJECT, demo_plugin.echo),)}, 'needs a description'),
    ({'tools': (Tool('demo_echo', 'x', {'type': 'string'}, demo_plugin.echo),)}, 'input schema'),
    ({'tools': (Tool('demo_echo', 'x', {'properties': {}}, demo_plugin.echo),)}, 'input schema'),
    ({'tools': (Tool('demo_echo', 'x', {'type': 'object', 'properties': {'a': {'anyOf': [{'type': 'string'}]}}}, demo_plugin.echo),)}, 'input schema'),
    ({'tools': (Tool('demo_echo', 'x', {'type': 'object', 'properties': {'a': {'type': 'string', 'pattern': 'x+'}}}, demo_plugin.echo),)}, 'input schema'),
    ({'tools': (Tool('demo_echo', 'x', {'type': 'object', 'properties': {'a': {'type': ['string', 'blob']}}}, demo_plugin.echo),)}, 'input schema'),
    ({'tools': (Tool('demo_echo', 'x', {'type': 'object', 'properties': 'nope'}, demo_plugin.echo),)}, 'input schema'),
    ({'knowledge': (Knowledge('Bad Id', 'T', 'x'),)}, 'knowledge id'),
    ({'knowledge': (Knowledge('a' * 41, 'T', 'x'),)}, 'knowledge id'),
    ({'knowledge': (Knowledge('notes', 'two\nlines', 'x'),)}, 'one-line title'),
    ({'knowledge': (Knowledge('notes', 'T' * 81, 'x'),)}, 'one-line title'),
    ({'knowledge': (Knowledge('notes', ' ', 'x'),)}, 'one-line title'),
    ({'knowledge': (Knowledge('notes', 'T', ['x']),)}, 'needs text'),
    ({'knowledge': (Knowledge('notes', 'T', 'x' * 8001),)}, 'longer than 8000'),
    ({'checks': (Check('manual', 'A', 900, 0, ('demo',), demo_plugin.pulse),)}, 'built-in'),
    ({'checks': (Check('slack', 'A', 900, 0, ('demo',), demo_plugin.pulse),)}, 'built-in'),
    ({'checks': (Check('selfreview-x', 'A', 900, 0, ('demo',), demo_plugin.pulse),)}, 'built-in'),
    ({'checks': (Check('history', 'A', 900, 0, ('demo',), demo_plugin.pulse),)}, 'built-in'),
    ({'checks': (Check('pulse', 'A', 900, 0, ('demo',), demo_plugin.pulse, sections=('anomalies',)),)}, 'built-in state key'),
    ({'checks': (Check('pulse', 'A', 900, 0, ('demo',), demo_plugin.pulse, sections=('services', 'serverPasses')),)}, 'built-in state key'),
    ({'checks': (Check('pulse', 'A', 900, 0, ('demo',), demo_plugin.pulse, sections=('services', 'selfreview')),)}, 'built-in state key'),
    ({'checks': (Check('pulse', 'A', 900, 0, ('demo',), demo_plugin.pulse, sections=('services', 5)),)}, 'sections has the wrong type'),
    ({'tools': (Tool('demo_echo', 'x', {'type': 'object', 'properties': {'a': {'type': 'number', 'minimum': float('nan')}}}, demo_plugin.echo),)}, 'input schema'),
    ({'tools': (Tool('demo_echo', 'x', {'type': 'object', 'properties': {'a': {'type': 'integer', 'maximum': float('inf')}}}, demo_plugin.echo),)}, 'input schema'),
    ({'tools': (Tool('demo_echo', 'x', {'type': 'object', 'properties': {'a': {'type': 'string', 'enum': ['x', {'y'}]}}}, demo_plugin.echo),)}, 'input schema'),
    ({'tools': (Tool('demo_echo', 'x', {'type': 'object', 'properties': {'a': {'type': 'string', 'minimum': 1}}}, demo_plugin.echo),)}, 'input schema'),
    ({'tools': (Tool('demo_echo', 'x', {'type': 'object', 'properties': {'a': {'type': ['integer', 'string'], 'maximum': 9}}}, demo_plugin.echo),)}, 'input schema'),
    ({'tools': (Tool('demo_echo', 'x', {'type': 'object', 'properties': {'a': {'minimum': 1}}}, demo_plugin.echo),)}, 'input schema'),
    ({'tools': (Tool('demo_echo', 'x', {'type': 'object', 'properties': {'a': {'type': 'integer', 'minLength': 1}}}, demo_plugin.echo),)}, 'input schema'),
    ({'tools': (Tool('demo_echo', 'x', {'type': 'object', 'properties': {'a': {'type': 'array', 'maxLength': 3}}}, demo_plugin.echo),)}, 'input schema'),
    ({'checks': (Check('pulse', 'A', 900, 0, ('demo',), demo_plugin.pulse, sections=('services', 'Routes')),)}, 'not a valid section name'),
    ({'checks': (Check('pulse', 'A', 900, 0, ('demo',), demo_plugin.pulse, sections=('bad name',)),)}, 'not a valid section name'),
    ({'checks': (Check('pulse', 'A', 900, 0, ('demo',), demo_plugin.pulse, sections=('s' * 32,)),)}, 'not a valid section name'),
    ({'checks': (Check('pulse', 'A', 900, 0, ('demo',), demo_plugin.pulse, sections=('',)),)}, 'not a valid section name'),
    ({'checks': (Check('pulse', 'A', 900, 0, ('demo',), demo_plugin.pulse, sections=('9lives',)),)}, 'not a valid section name'),
])
def test_a_plugin_that_breaks_a_rule_is_broken_with_its_reason(over, fragment):
    # given
    plugin = variant(**over)

    # when
    loaded = solo(plugin)

    # then
    assert loaded.state == 'broken'
    assert fragment in loaded.reason
    assert loaded.reason.startswith('Demo ')


@pytest.mark.parametrize('name', ['Demo.\n\n## Process', 'D' * 61, 'Demo' + chr(9) + 'tab'], ids=['two-lines', 'long', 'tab'])
def test_a_plugin_name_must_be_one_short_printable_line(name):
    # given
    plugin = variant(name=name)

    # when
    loaded = solo(plugin)

    # then
    assert loaded.state == 'broken'
    assert loaded.reason == 'demo was not loaded: the name must be one line of at most 60 printable characters'


GOOD_DOCS = 'https://docs.example.com/tokens'


@pytest.mark.parametrize('setup', [
    Setup(steps=('Open Settings',)),
    Setup(steps=tuple(f'Step {n}' for n in range(10)), docs=GOOD_DOCS, docs_label='Example: tokens'),
    Setup(steps=('x' * 400,)),
    Setup(steps=('Open',), docs='https://' + 'a' * 492),
    Setup(steps=('Open',), docs=GOOD_DOCS, docs_label='L' * 60),
    None,
], ids=['one-step', 'ten-steps-with-link', 'long-step', 'long-docs', 'long-label', 'none'])
def test_a_valid_setup_guide_loads(setup):
    # then
    assert solo(variant(setup=setup)).state == 'ok'


@pytest.mark.parametrize('setup', [
    Setup(),
    Setup(steps=tuple(f'Step {n}' for n in range(11))),
    Setup(steps=('',)),
    Setup(steps=('   ',)),
    Setup(steps=('Open' + chr(9) + 'Settings',)),
    Setup(steps=('Open\nSettings',)),
    Setup(steps=('x' * 401,)),
    Setup(steps=(5,)),
    Setup(steps=['Open']),
    Setup(steps=('Open',), docs='http://docs.example.com/tokens'),
    Setup(steps=('Open',), docs='javascript:alert(1)'),
    Setup(steps=('Open',), docs='https://'),
    Setup(steps=('Open',), docs='https://docs.example.com/a b'),
    Setup(steps=('Open',), docs='https://' + 'a' * 493),
    Setup(steps=('Open',), docs=GOOD_DOCS, docs_label='L' * 61),
    Setup(steps=('Open',), docs=GOOD_DOCS, docs_label='Two' + chr(10) + 'lines'),
    Setup(steps=('Open',), docs_label='Guide'),
    Setup(steps=('Open',), docs=None),
    Setup(steps=('Open',), docs=GOOD_DOCS, docs_label='   '),
    'steps',
], ids=['empty', 'eleven-steps', 'blank-step', 'space-step', 'tab-step', 'newline-step', 'long-step', 'not-text', 'not-tuple', 'http', 'script', 'bare-scheme', 'whitespace', 'long-docs',
        'long-label', 'label-newline', 'label-without-docs', 'docs-none', 'blank-label', 'not-a-setup'])
def test_an_invalid_setup_guide_breaks_the_plugin(setup):
    # when
    loaded = solo(variant(setup=setup))

    # then
    assert loaded.state == 'broken'
    assert loaded.reason.startswith('Demo was not loaded: the setup guide must be 1-10 printable steps')


def test_describe_serves_the_setup_guide_or_none():
    # given
    guide = Setup(steps=('Open Settings', 'Copy the token'), docs=GOOD_DOCS, docs_label='Example: tokens')

    # then
    assert registry.describe(variant(setup=guide))['setup'] == {'steps': ['Open Settings', 'Copy the token'], 'docs': GOOD_DOCS, 'docsLabel': 'Example: tokens'}
    assert registry.describe(DEMO)['setup'] is None
    assert json.dumps(registry.describe(variant(setup=guide)))


@pytest.mark.parametrize('value', ['', 'always', 'url', 'mode'])
def test_a_company_knowledge_marker_of_empty_always_or_an_own_field_loads(value):
    # then
    assert solo(variant(company_knowledge=value)).state == 'ok'


@pytest.mark.parametrize('value', ['docs', 'Always', 'token ', None, 3])
def test_a_company_knowledge_marker_naming_no_own_field_breaks_the_plugin(value):
    # when
    loaded = solo(variant(company_knowledge=value))

    # then
    assert loaded.state == 'broken'
    assert loaded.reason.startswith('Demo was not loaded: company_knowledge ')
    assert 'must be empty, always, or the key of one of the plug-in' in loaded.reason


def test_describe_serves_the_company_knowledge_marker():
    assert registry.describe(DEMO)['companyKnowledge'] == ''
    assert registry.describe(variant(company_knowledge='always'))['companyKnowledge'] == 'always'
    assert registry.describe(variant(company_knowledge='mode'))['companyKnowledge'] == 'mode'


@pytest.mark.parametrize('marker', ['always', 'mode'])
def test_a_secondary_knowledge_source_loads_and_is_described(marker):
    # when
    plugin = variant(company_knowledge=marker, knowledge_also=True)

    # then
    assert solo(plugin).state == 'ok'
    assert registry.describe(plugin)['knowledgeAlso'] is True
    assert registry.describe(DEMO)['knowledgeAlso'] is False


@pytest.mark.parametrize('over', [{'knowledge_also': True}, {'knowledge_also': 'yes', 'company_knowledge': 'always'}, {'knowledge_also': 1, 'company_knowledge': 'always'}])
def test_knowledge_also_needs_a_bool_and_a_company_knowledge_marker(over):
    # when
    loaded = solo(variant(**over))

    # then
    assert loaded.state == 'broken'
    assert loaded.reason.startswith('Demo was not loaded: knowledge_also ')


def test_jira_and_github_are_secondary_knowledge_sources_and_the_pure_ones_are_not():
    # given
    from skopax.plugins import confluence, github, jira, sharepoint

    # when
    shown = {p.id: (registry.describe(p)['companyKnowledge'], registry.describe(p)['knowledgeAlso']) for p in (jira.PLUGIN, github.PLUGIN, confluence.PLUGIN, sharepoint.PLUGIN)}

    # then
    assert shown == {'jira': ('always', True), 'github': ('docs', True), 'confluence': ('always', False), 'sharepoint': ('always', False)}
    assert all(solo(p).state == 'ok' for p in (jira.PLUGIN, github.PLUGIN))


def test_a_sixty_character_name_loads():
    # then
    assert solo(variant(name='D' * 60)).state == 'ok'


def test_an_icon_must_be_a_small_svg_inside_the_plugin_directory(tmp_path):
    # given
    head, tail = '<svg xmlns="http://www.w3.org/2000/svg">', '</svg>'
    inner = tmp_path / 'inner'
    inner.mkdir()
    (inner / 'edge.svg').write_text(head + ' ' * (20480 - len(head) - len(tail)) + tail, encoding='utf-8')
    (inner / 'big.svg').write_text(head + ' ' * (20481 - len(head) - len(tail)) + tail, encoding='utf-8')
    (inner / 'fake.svg').write_text('not markup at all', encoding='utf-8')
    (tmp_path / 'outside.svg').write_text(head + tail, encoding='utf-8')

    # when
    states = {name: solo(variant(icon=name), directory=inner).state for name in ('edge.svg', 'big.svg', 'fake.svg', '../outside.svg', 'absent.svg')}

    # then
    assert states == {'edge.svg': 'ok', 'big.svg': 'broken', 'fake.svg': 'broken', '../outside.svg': 'broken', 'absent.svg': 'broken'}
    assert 'icon' in solo(variant(icon='big.svg'), directory=inner).reason


def test_the_icon_is_read_from_the_plugin_directory():
    # given
    reg = Registry.of(DEMO, other('plain', icon='demo-icon.svg'), other('lost', icon='lost.svg'))

    # when
    text = reg.icon('demo')

    # then
    assert text == (HERE / 'demo-icon.svg').read_text(encoding='utf-8')
    assert reg.icon('plain') == text
    assert (reg.icon('lost'), reg.icon('absent')) == (None, None)


def test_duplicate_ids_tools_ran_keys_and_ops_break_the_later_plugin():
    # given
    keep = variant(id='aaa', name='Aaa', checks=(Check('aaa', 'A', 900, 0, ('aaa',), demo_plugin.pulse),), tools=(), knowledge=(), agent_reads=(), capabilities=Capabilities(hosts=('@url',), secrets=('token',)))
    twin = dataclasses.replace(DEMO)
    ran = other('ran-clash', ran=('demo',))
    tool = other('tool-clash', tools=(Tool('demo_echo', 'again', OBJECT, demo_plugin.echo),))
    note = other('note-clash', knowledge=(dataclasses.replace(DEMO.knowledge[0]),))
    op = other('op-clash', capabilities=Capabilities(agent_reads=True), agent_reads=DEMO.agent_reads)
    reg = Registry.of(DEMO, twin, ran, tool, note, op, keep)

    # when
    states = [(x.id, x.state, x.reason) for x in reg.loaded()]

    # then
    assert [s[:2] for s in states] == [('aaa', 'ok'), ('demo', 'ok'), ('demo', 'broken'), ('note-clash', 'broken'), ('op-clash', 'broken'), ('ran-clash', 'broken'), ('tool-clash', 'broken')]
    assert 'already loaded' in states[2][2]
    assert 'demo-notes' in states[3][2] and 'demo' in states[3][2]
    assert 'demo-echo' in states[4][2]
    assert 'ran key demo' in states[5][2]
    assert 'demo_echo' in states[6][2]
    assert [p.id for p in reg.usable()] == ['aaa', 'demo']


def test_a_tool_cannot_shadow_a_core_tool():
    # given
    shadow = other('shadow', tools=(Tool('card', 'Pretend card reader', OBJECT, demo_plugin.echo),))
    note = other('noter', knowledge=(dataclasses.replace(DEMO.knowledge[0], id='triage'),))

    # when
    reg = Registry.of(shadow, note, core_tools=frozenset({'card', 'cards'}), core_knowledge=frozenset({'triage'}))
    free = Registry.of(shadow, note)

    # then
    assert (reg.get('shadow').state, reg.get('noter').state) == ('broken', 'broken')
    assert 'card' in reg.get('shadow').reason and 'core' in reg.get('shadow').reason
    assert 'triage' in reg.get('noter').reason
    assert (free.get('shadow').state, free.get('noter').state) == ('ok', 'ok')
    assert Registry.load({}, builtin=('tests.plugins.demo_plugin',), entry_points=False, core_tools=frozenset({'demo_echo'})).get('demo').state == 'broken'


def test_ran_keys_may_not_use_a_core_ran_key():
    # given
    thief = other('thief', ran=('manual',))
    thief_two = other('thief-two', ran=('slack-posts',))

    # when
    reg = Registry.of(thief, thief_two)

    # then
    assert [x.state for x in reg.loaded()] == ['broken', 'broken']
    assert 'core' in reg.get('thief').reason


def test_reserved_names_cover_core_and_plugin_ran_keys():
    # given
    reg = Registry.of(DEMO)

    # when
    names = reg.reserved_names()
    prefixes = reg.reserved_prefixes()

    # then
    assert {'manual', 'slack-posts', 'selfreview', 'demo', 'pulse', 'primary', 'all'} <= names
    assert prefixes == frozenset(f'{n}-' for n in names)
    assert 'demo-' in prefixes and 'pulse-' in prefixes


def test_reserved_names_leave_out_broken_plugins():
    # given
    reg = Registry.of(variant(id='Demo'))

    # then
    assert 'pulse' not in reg.reserved_names()
    assert reg.reserved_names() == frozenset(registry.CORE_RAN) | {'primary', 'all', 'no-data'}


def test_core_ran_literals_match_the_core_passes():
    # given
    expected = ('manual', slackhealth.PASS, selfreview.PASS, probe.PASS, jiraaccess.PASS, promqlchecks.PASS)

    # then
    assert registry.CORE_RAN == expected


def test_core_prefixes_hold_the_integrations_that_are_still_core():
    # given
    prefixes = set(registry.CORE_PREFIXES)

    # then
    assert {'core', 'general', 'small-talk', 'self-review'} <= prefixes and 'gitlab' not in prefixes and 'jira' not in prefixes and 'slack' not in prefixes
    assert len(prefixes) == len(registry.CORE_PREFIXES)


def test_registry_is_per_call():
    # given
    first = Registry.load({'SKOPAX_PLUGINS': 'tests.plugins.demo_plugin'}, builtin=(), entry_points=False)
    second = Registry.load({'SKOPAX_PLUGINS': 'tests.plugins.enterprise_plugin'}, builtin=(), entry_points=False)

    # when
    ids = ([x.id for x in first.loaded()], [x.id for x in second.loaded()])

    # then
    assert ids == (['demo'], ['enterprise-demo'])
    assert first.get('enterprise-demo') is None and second.get('demo') is None
    assert 'demo' in first.reserved_names() and 'demo' not in second.reserved_names()


def test_mark_broken_removes_a_plugin_from_use_and_its_claims():
    # given
    plain = other('plain')
    reg = Registry.of(plain, entitled_plugin.PLUGIN, enterprise_plugin.PLUGIN)

    # when
    before = (reg.edition(), reg.get('enterprise-demo').state, 'plain' in reg.reserved_names())
    reg.mark_broken('plain', 'migration 2 failed')
    reg.mark_broken('entitled-demo', 'migration 1 failed')

    # then
    assert before == ('enterprise', 'ok', True)
    assert 'migration 2 failed' in reg.get('plain').reason and reg.get('plain').state == 'broken'
    assert (reg.edition(), reg.get('enterprise-demo').state) == ('community', 'locked')
    assert reg.usable() == []
    assert 'plain' not in reg.reserved_names()
    with pytest.raises(KeyError):
        reg.mark_broken('absent', 'x')


def test_the_contract_reexports_the_engine_helpers_and_keeps_its_values_apart():
    # given
    a = Sweep(PassResult('x', True, 'ok'))
    b = Sweep(PassResult('y', True, 'ok'))

    # when
    a.sections['k'] = [{}]
    a.deploys.append({})

    # then
    assert (contract.PassResult, contract.anomaly, contract.fmt, contract.iso) == (engine.PassResult, engine.anomaly, engine.fmt, engine.iso)
    assert (b.sections, b.deploys) == ({}, [])
    with pytest.raises(dataclasses.FrozenInstanceError):
        DEMO.checks[0].id = 'x'
    assert contract.EDITIONS == ('community', 'enterprise')
    assert contract.ID.fullmatch('order-a') and not contract.ID.fullmatch('a')


def test_read_error_carries_status_and_message():
    # given
    err = ReadError(404, 'no such thing')

    # then
    assert (err.status, err.message, str(err)) == (404, 'no such thing', 'no such thing')


def test_source_data_accepts_the_documented_shape_and_refuses_the_rest():
    # given
    good = {'label': 'file', 'values': {'a': 'b', 'n': 3, 'flag': True, 'none': None}, 'instances': [{'name': 'main', 'port': 9}]}

    # when
    data = contract.SourceData.of(good, 'fallback')
    bare = contract.SourceData.of({}, 'fallback')

    # then
    assert data == contract.SourceData('file', {'a': 'b', 'n': '3', 'flag': 'true', 'none': ''}, ({'name': 'main', 'port': '9'},))
    assert bare == contract.SourceData('fallback', {}, ())
    for bad in ([], {'label': 5}, {'values': []}, {'values': {1: 'x'}}, {'values': {'a': [1]}}, {'instances': {'name': 'x'}}, {'instances': ['x']}, {'instances': [{'name': {'x': 1}}]}):
        with pytest.raises(ValueError):
            contract.SourceData.of(bad, 'fallback')


def echo_tool(name):
    return Tool(name, 'x', OBJECT, demo_plugin.echo)


def test_a_locked_or_incompatible_plugin_never_claims_what_a_runnable_one_uses():
    # given
    locked = minimal('aaa-ee', 'Aaa EE', edition='enterprise', tools=(echo_tool('shared'),))
    old = minimal('aaa-old', 'Aaa old', core='>=9', tools=(echo_tool('shared'),))
    runner = minimal('zzz', 'Zzz', tools=(echo_tool('shared'),))

    # when
    first = Registry.of(locked, runner)
    second = Registry.of(old, runner)

    # then
    assert [(x.id, x.state) for x in first.loaded()] == [('aaa-ee', 'locked'), ('zzz', 'ok')]
    assert [(x.id, x.state) for x in second.loaded()] == [('aaa-old', 'incompatible'), ('zzz', 'ok')]
    assert [p.id for p in first.usable()] == ['zzz'] and [p.id for p in second.usable()] == ['zzz']


def test_claims_are_resolved_again_in_id_order_when_the_edition_changes():
    # given
    state = {'edition': 'community'}
    provider = minimal('mmm-licence', 'Licence', hooks=Hooks(entitlement=lambda: state['edition']))
    locked = minimal('aaa-ee', 'Aaa EE', edition='enterprise', tools=(echo_tool('shared'),))
    runner = minimal('zzz', 'Zzz', tools=(echo_tool('shared'),))
    reg = Registry.of(locked, provider, runner)

    # when
    before = [(x.id, x.state) for x in reg.loaded()]
    state['edition'] = 'enterprise'
    stale = reg.edition()
    reg.refresh()
    after = {x.id: x for x in reg.loaded()}

    # then
    assert before == [('aaa-ee', 'locked'), ('mmm-licence', 'ok'), ('zzz', 'ok')]
    assert stale == 'community'
    assert (after['aaa-ee'].state, after['zzz'].state) == ('ok', 'broken')
    assert 'shared' in after['zzz'].reason and 'aaa-ee' in after['zzz'].reason
    assert reg.edition() == 'enterprise'


def test_the_entitlement_hook_runs_once_per_settle_and_accessors_return_copies():
    # given
    calls = []

    def hook():
        calls.append(1)
        return 'community'

    reg = Registry.of(minimal('mmm-licence', 'Licence', hooks=Hooks(entitlement=hook)), enterprise_plugin.PLUGIN)

    # when
    for _ in range(3):
        reg.loaded(), reg.get('mmm-licence'), reg.usable(), reg.edition()
    reg.loaded()[0].state = 'broken'
    during = len(calls)
    reg.refresh()

    # then
    assert (during, len(calls)) == (1, 2)
    assert reg.loaded()[0].state != 'broken'


def test_an_incompatible_provider_leaves_the_edition_community():
    # given
    old = dataclasses.replace(entitled_plugin.PLUGIN, core='>=9')

    # when
    reg = Registry.of(old, enterprise_plugin.PLUGIN)

    # then
    assert (reg.get('entitled-demo').state, reg.edition(), reg.get('enterprise-demo').state) == ('incompatible', 'community', 'locked')


def test_a_check_id_claims_its_prefix_for_other_plugins():
    # given
    prom = other('prom')
    east = other('prom-east')
    odd = other('zeta', ran=('prom-x',))

    # when
    reg = Registry.of(east, prom, odd)
    states = {x.id: x for x in reg.loaded()}

    # then
    assert (states['prom'].state, states['prom-east'].state, states['zeta'].state) == ('ok', 'broken', 'broken')
    assert 'clashes with prom' in states['prom-east'].reason and 'clashes with prom' in states['zeta'].reason
    assert [p.id for p in reg.usable()] == ['prom']


def test_a_later_check_id_cannot_cover_a_key_another_plugin_already_uses():
    # given
    early = other('bbb', ran=('aaa-x',))
    late = minimal('zzz', 'Zzz', checks=(Check('aaa', 'A', 900, 0, ('zzz',), demo_plugin.pulse),))

    # when
    reg = Registry.of(early, late)

    # then
    assert [(x.id, x.state) for x in reg.loaded()] == [('bbb', 'ok'), ('zzz', 'broken')]
    assert 'clashes with bbb' in reg.get('zzz').reason


def test_the_registry_import_closure_is_the_allowlist():
    # given
    import ast
    import subprocess
    import sys

    root = Path(__file__).resolve().parent.parent
    code = 'import sys; sys.path.insert(0, sys.argv[1]); import skopax.registry; print(sorted(m for m in sys.modules if m.startswith("skopax")))'
    allowed = {'skopax', 'skopax.actors', 'skopax.bounded', 'skopax.brand', 'skopax.businesstime', 'skopax.cardlink', 'skopax.causes', 'skopax.chattext', 'skopax.clock', 'skopax.contract', 'skopax.engine', 'skopax.errkind', 'skopax.history', 'skopax.information', 'skopax.jiralinks', 'skopax.messages', 'skopax.redact', 'skopax.registry', 'skopax.remote',
               'skopax.settle', 'skopax.store', 'skopax.summary', 'skopax.tags'}

    # when
    out = subprocess.run([sys.executable, '-I', '-c', code, str(root)], capture_output=True, text=True, check=True).stdout
    closure = set(eval(out))
    tree = ast.parse((root / 'skopax' / 'contract.py').read_text(encoding='utf-8'))
    contract_imports = {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module.startswith('skopax')}

    # then
    assert closure == allowed
    assert not closure & {'skopax.selfreview', 'skopax.slackhealth', 'skopax.toolusage', 'skopax.postdeploy', 'skopax.plugins.prometheus', 'skopax.plugins.prometheus.engine', 'skopax.investigator.tools'}
    assert contract_imports == {'skopax.engine', 'skopax.errkind', 'skopax.remote'}


@pytest.mark.parametrize('over', [{'knowledge': None}, {'after': None}, {'id': 5}, {'id': None}, {'after': 'order-c'}, {'agent_reads': None}, {'tools': (Tool(['t'], 'x', OBJECT, demo_plugin.echo),)},
                                  {'checks': (Check('bad', 'Bad', 900, 0, (['k'],), demo_plugin.pulse),)}, {'knowledge': (dataclasses.replace(DEMO.knowledge[0], id=['k']),)}, {'core': ''}])
def test_a_malformed_plugin_is_broken_and_never_stops_the_others(over):
    # given
    bad = dataclasses.replace(minimal('bad-one', 'Bad one'), **over)

    # when
    reg = Registry.of(DEMO, bad)
    states = {x.id: x for x in reg.loaded()}

    # then
    assert states['demo'].state == 'ok'
    broken = [x for x in reg.loaded() if x.id != 'demo']
    assert len(broken) == 1 and broken[0].state == 'broken' and broken[0].reason
    assert reg.reserved_names() and [p.id for p in reg.usable()] == ['demo']


def test_a_malformed_module_next_to_demo_does_not_stop_load(tmp_path, monkeypatch):
    # given
    (tmp_path / 'tp_none.py').write_text("from tests.plugins.base import minimal\n\nPLUGIN = minimal('tp-none', 'TP None', knowledge=None)\n", encoding='utf-8')
    (tmp_path / 'tp_exit.py').write_text("raise SystemExit(3)\n", encoding='utf-8')
    monkeypatch.syspath_prepend(str(tmp_path))

    # when
    reg = Registry.load({'SKOPAX_PLUGINS': 'tests.plugins.demo_plugin,tp_none,tp_exit,tests.plugins.demo_plugin:PLUGIN'}, builtin=(), entry_points=False)
    states = {x.id: x for x in reg.loaded()}

    # then
    assert states['demo'].state == 'ok'
    assert states['tp-none'].state == 'broken' and states['tp_exit'].state == 'broken' and 'SystemExit' in states['tp_exit'].reason
    assert states['tests.plugins.demo_plugin:PLUGIN'].reason == 'tests.plugins.demo_plugin:PLUGIN was not loaded: module path tests.plugins.demo_plugin:PLUGIN must not name an attribute'


def test_get_prefers_a_non_broken_entry_and_extra_duplicates_break():
    # given
    bad_first = minimal('dup', 'Dup first', icon='nothing.svg')
    good = minimal('dup', 'Dup second')
    third = minimal('dup', 'Dup third')

    # when
    reg = Registry.of(bad_first, good, third)
    found = reg.get('dup')

    # then
    assert (found.state, found.plugin.name) == ('ok', 'Dup second')
    assert [(x.state, x.plugin.name) for x in reg.loaded()] == [('broken', 'Dup first'), ('ok', 'Dup second'), ('broken', 'Dup third')]
    assert 'already loaded' in reg.loaded()[2].reason


def test_an_empty_core_is_missing_and_describe_carries_the_icon_flag():
    # given
    empty = solo(variant(core=''))

    # when
    flag = (registry.describe(DEMO)['icon'], registry.describe(variant(icon=''))['icon'], registry.describe(DEMO, icon=False)['icon'])

    # then
    assert empty.state == 'broken' and 'core is missing' in empty.reason
    assert flag == (True, False, False)


def test_a_field_named_plugin_is_reserved():
    # given
    bad = dataclasses.replace(minimal('plugfield', 'Plug field'), fields=(Field('plugin', 'Plugin', 'text'),))

    # when
    loaded = solo(bad)

    # then
    assert loaded.state == 'broken' and 'plugin' in loaded.reason


def test_a_schema_with_a_type_list_and_the_supported_limits_loads():
    # given
    schema = {'type': 'object', 'properties': {'a': {'type': ['string', 'null'], 'maxLength': 5}, 'b': {'type': 'array', 'items': {'type': 'integer', 'minimum': 1}, 'maxItems': 3}, 'c': {'type': 'string', 'enum': ['x', 'y']}},
              'required': ['b'], 'additionalProperties': False}

    # when
    loaded = solo(variant(tools=(Tool('demo_echo', 'x', schema, demo_plugin.echo),), knowledge=(Knowledge('notes', 'Notes', 'x' * 8000),)))

    # then
    assert loaded.state == 'ok'


def agent_field_plugin(plugin_id, *keys, fields=()):
    secrets = tuple(k for k in keys if k.endswith('token'))
    read = AgentRead(f'{plugin_id}-read', ('q',), tuple(re.escape(k) for k in keys), lambda c: [], lambda c, a: {}, fields=tuple(Field(k, k, 'secret' if k in secrets else 'text') for k in keys))
    return dataclasses.replace(minimal(plugin_id, plugin_id.capitalize()), fields=fields, capabilities=Capabilities(secrets=secrets, agent_reads=True), agent_reads=(read,))


def test_agent_read_field_keys_may_hold_dots_and_follow_one_shape():
    # given
    good = ['a', 'a.b-c', 'bo.dev.url', 'x' + 'y' * 60]
    bad = ['Bad', '1x', 'a b', 'x' + 'y' * 61, 'a_b', '.a', '']

    # when
    fine = [Registry.of(agent_field_plugin('reader', k)).get('reader').state for k in good]
    refused = [Registry.of(agent_field_plugin('reader', k)).get('reader') for k in bad]

    # then
    assert fine == ['ok'] * 4
    assert [x.state for x in refused] == ['broken'] * len(bad)
    assert all('agent read field' in x.reason for x in refused)


def test_agent_read_field_keys_never_clash_with_the_agent_instance_fields_and_never_change_meaning():
    # given
    same = agent_field_plugin('reader', 'k.one', 'k.two')
    same = dataclasses.replace(same, agent_reads=same.agent_reads + (dataclasses.replace(same.agent_reads[0], op='reader-again'),))
    twice = dataclasses.replace(same, agent_reads=(same.agent_reads[0], dataclasses.replace(same.agent_reads[1], fields=(Field('k.one', 'Other label', 'text'),))))
    shared = agent_field_plugin('reader', 'k-one', fields=(Field('k-one', 'K', 'text'),))
    equal = agent_field_plugin('reader', 'k-one', fields=(Field('k-one', 'k-one', 'text'),))

    # when
    repeated = Registry.of(same).get('reader')
    conflicting = Registry.of(twice).get('reader')
    with_field = Registry.of(shared).get('reader')
    with_equal = Registry.of(equal).get('reader')
    host = [Registry.of(agent_field_plugin('reader', key)).get('reader') for key in registry.AGENT_HOST_FIELDS]
    both = Registry.of(agent_field_plugin('aaa', 'shared.key'), agent_field_plugin('bbb', 'shared.key'))
    first, second = both.get('aaa'), both.get('bbb')

    # then
    assert repeated.state == 'ok' and with_equal.state == 'ok'
    assert conflicting.state == 'broken' and 'k.one' in conflicting.reason and 'different settings' in conflicting.reason
    assert with_field.state == 'broken' and 'k-one' in with_field.reason
    assert [x.state for x in host] == ['broken'] * 4
    assert (first.state, second.state) == ('ok', 'broken') and 'shared.key' in second.reason and 'aaa' in second.reason


def test_a_field_may_label_its_choices_and_name_its_section():
    # given
    field = Field('auth', 'Sign-in', 'choice', default='none', choices=('none', 'header', 'oauth'), choice_labels={'none': 'No sign-in', 'header': 'Token in a header'}, group='sign-in')
    plugin = variant(fields=DEMO.fields + (field, Field('cert', 'CA certificate', 'pem', group='advanced'), Field('pinned', 'Pinned', 'bool', default='false', group='advanced')))

    # when
    loaded = solo(plugin)
    view = registry.field_view(field)

    # then
    assert loaded.state == 'ok', loaded.reason
    assert (view['choiceLabels'], view['group']) == ({'none': 'No sign-in', 'header': 'Token in a header'}, 'sign-in')
    assert [(f['key'], f['choiceLabels'], f['group']) for f in registry.describe(plugin)['fields']][-3:] == [('auth', {'none': 'No sign-in', 'header': 'Token in a header'}, 'sign-in'), ('cert', {}, 'advanced'), ('pinned', {}, 'advanced')]
    assert (Field('x', 'X', 'text').choice_labels, Field('x', 'X', 'text').group) == ({}, '')


@pytest.mark.parametrize('over', [{'group': 'Advanced'}, {'group': 'a b'}, {'kind': 'choice', 'choices': ('a',), 'choice_labels': {'b': 'B'}}], ids=['caps', 'space', 'unknown-choice'])
def test_an_agent_read_field_follows_the_same_label_and_group_rules(over):
    # given
    plugin = agent_field_plugin('reader', 'reader.mode')
    read = plugin.agent_reads[0]
    bad = dataclasses.replace(plugin, agent_reads=(dataclasses.replace(read, fields=(dataclasses.replace(read.fields[0], **over),)),))

    # when
    fine = Registry.of(plugin).get('reader')
    refused = Registry.of(bad).get('reader')

    # then
    assert fine.state == 'ok'
    assert refused.state == 'broken' and 'reader.mode' in refused.reason


def test_the_field_view_is_public_and_describe_uses_it():
    # given
    field = Field('url', 'URL', 'url', required=True, show_if=('mode', 'agent'), ui=True)

    # then
    assert registry.field_view(field) == {'key': 'url', 'label': 'URL', 'kind': 'url', 'required': True, 'default': '', 'help': '', 'choices': [], 'low': None, 'high': None, 'showIf': ['mode', 'agent'], 'ui': True, 'choiceLabels': {}, 'group': ''}
    assert registry.describe(demo_plugin.PLUGIN)['fields'][0] == registry.field_view(demo_plugin.PLUGIN.fields[0])
    assert not hasattr(registry, '_field')


class Exploding(str):
    def __hash__(self):
        raise RuntimeError('db password hunter2')

    def strip(self, *args):
        raise RuntimeError('db password hunter2')


class OnceOnly(list):
    def __init__(self, *items):
        super().__init__(items)
        self.read = 0

    def __iter__(self):
        self.read += 1
        if self.read > 1:
            raise RuntimeError('db password hunter2')
        return super().__iter__()


@pytest.mark.parametrize('over, label', [
    ({'name': Exploding('Demo')}, 'Demo'),
    ({'tools': (Tool(Exploding('demo_echo'), 'x', OBJECT, demo_plugin.echo),)}, 'Demo'),
    ({'after': OnceOnly('zzz')}, 'Demo'),
], ids=['check', 'claim', 'order'])
def test_an_unexpected_failure_while_checking_a_plugin_names_only_its_type(over, label):
    # given
    plugin = variant(**over)

    # when
    loaded = Registry.of(plugin, minimal('zzz', 'Zzz')).get('demo')

    # then
    assert (loaded.state, loaded.reason) == ('broken', f'{label} was not loaded: it could not be checked (RuntimeError)')


def test_an_agent_read_field_must_match_one_of_its_keys():
    # given
    read = AgentRead('demo-echo', ('text',), ('url', 'token'), demo_plugin.agent_sources, demo_plugin.agent_echo, fields=(Field('ns', 'Namespace', 'text'),))
    plugin = variant(agent_reads=(read,))

    # when
    loaded = solo(plugin)

    # then
    assert (loaded.state, loaded.reason) == ('broken', 'Demo was not loaded: agent read demo-echo asks for field ns, which none of its keys match')


def test_a_section_name_of_thirty_one_characters_loads():
    # given
    plugin = variant(checks=(Check('pulse', 'A', 900, 0, ('demo',), demo_plugin.pulse, sections=('s' + 'a-9' * 10,)),))

    # when
    loaded = solo(plugin)

    # then
    assert len(plugin.checks[0].sections[0]) == 31 and loaded.state == 'ok'


def test_no_test_pins_the_order_of_the_builtin_list():
    # given
    tests = Path(__file__).parent

    # when
    found = [p.name for p in sorted(tests.rglob('test_*.py')) if 'BUILTIN.index(' in p.read_text(encoding='utf-8') and p.name != Path(__file__).name]

    # then
    assert found == []


def test_built_in_checks_alone_roll_over_at_most_four_times_in_any_minute_core_jobs_not_counted():
    # given
    import collections
    import importlib
    from skopax.scheduler import MAX_PARALLEL
    marks = collections.Counter()

    # when
    for name in registry.BUILTIN:
        plugin = importlib.import_module(name).PLUGIN
        for c in plugin.checks:
            if c.background or callable(c.offset):
                continue
            period, offset = (3600, c.offset % 3600) if c.period > 3600 else (c.period, c.offset % c.period)
            for t in range(offset, 3600, period):
                marks[t] += 1
    windows = {t: sum(marks[(t + k) % 3600] for k in range(60)) for t in range(3600)}

    # then
    assert MAX_PARALLEL == 4
    assert {t: n for t, n in windows.items() if n > MAX_PARALLEL} == {}, 'more than 4 built-in foreground checks fall due in one minute'


def test_no_two_foreground_builtin_checks_start_in_the_same_second_of_an_hour():
    # given
    import collections
    import importlib
    together = {'pmm': ('pmm', 'qan', 'mongo'), 'cloudflare': ('cf', 'edge'), 'elasticsearch': ('logs', 'apm')}
    starts = collections.defaultdict(list)

    # when
    for name in registry.BUILTIN:
        plugin = importlib.import_module(name).PLUGIN
        for c in plugin.checks:
            if c.background or callable(c.offset) or c.period > 3600:
                continue
            for t in range(c.offset % c.period, 3600, c.period):
                starts[t].append(plugin.id if c.id in together.get(plugin.id, ()) else f'{plugin.id}.{c.id}')

    # then
    assert {t: found for t, found in starts.items() if len(set(found)) > 1} == {}


def test_every_builtin_plugin_says_how_to_set_it_up():
    # given
    import importlib
    loaded = [importlib.import_module(name).PLUGIN for name in registry.BUILTIN]

    # when
    missing = [p.id for p in loaded if not isinstance(p.setup, Setup) or not p.setup.steps]
    unlinked = [p.id for p in loaded if p.setup and p.setup.docs and not p.setup.docs.startswith('https://')]

    # then
    assert missing == []
    assert unlinked == []
