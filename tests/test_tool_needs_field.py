import dataclasses
from pathlib import Path

import pytest

from skopax import toolusage
from skopax.contract import Field, Tool
from skopax.instances import Instances, PluginState
from skopax.investigator.tools import ToolError
from skopax.investigator.toolset import ToolSet
from skopax.messages import msg
from skopax.plugintools import PluginTools
from skopax.registry import Registry
from skopax.runtime import Runtime
from skopax.sealer import Sealer
from skopax.store import Store
from tests.plugin_kit import add_instance
from tests.plugins import demo_plugin
from tests.plugins.base import minimal

DEMO_DIR = Path(demo_plugin.__file__).parent
URL = 'https://scoped.example.com'
SEAL = 'gate-key'
NOW = 1_792_224_000.0
SINCE = '2026-10-28T00:00:00Z'
SCHEMA = {'type': 'object', 'properties': {'text': {'type': 'string'}}, 'additionalProperties': False}


def scoped(calls: list, instance_input: str | None = 'instance'):
    def echo(ctx, args):
        calls.append(ctx.instance.name)
        return {'from': ctx.instance.name}

    return minimal('scoped', 'Scoped', checks=(), fields=(Field('url', 'URL', 'url', required=True), Field('scope', 'Scope', 'text')),
                   tools=(Tool('scoped_echo', 'Echo from an instance that has a scope', SCHEMA, echo, instance_input=instance_input, needs_field='scope'),
                          Tool('scoped_plain', 'Echo from any instance', SCHEMA, echo, instance_input=instance_input)))


def rig(store: Store, plugin, **instances) -> Runtime:
    registry = Registry.of(plugin, directory=DEMO_DIR)
    runtime = Runtime(registry, PluginState(store.db), Instances(store.db, registry, lambda: Sealer(SEAL)))
    for index, (name, values) in enumerate(instances.items()):
        add_instance(store, plugin, name, sealer_key=SEAL, primary=index == 0, **({'url': URL} | values))
    runtime.refresh()
    return runtime


def rows(runtime: Runtime) -> dict[str, dict]:
    usage = toolusage.with_status(toolusage.summary([], 1, SINCE, None, PluginTools(runtime)), ToolSet(runtime), None)
    return {t['name']: t for t in usage['tools']}


def test_a_tool_that_needs_a_field_is_offered_only_through_instances_that_have_it_set(store: Store):
    # given
    calls: list = []
    runtime = rig(store, scoped(calls), a={}, b={'scope': 'shop'})
    tools = PluginTools(runtime)

    # when
    defs = {d['name']: d for d in tools.definitions()}
    chosen = tools.call('scoped_echo', {'instance': 'b'}, NOW)
    refused = pytest.raises(ToolError, tools.call, 'scoped_echo', {'instance': 'a'}, NOW).value

    # then
    assert defs['scoped_echo']['input_schema']['properties']['instance']['enum'] == ['b']
    assert sorted(defs['scoped_plain']['input_schema']['properties']['instance']['enum']) == ['a', 'b']
    assert chosen == {'from': 'b'} and calls == ['b']
    assert refused.text.startswith('instance must be one of b')
    assert ToolSet(runtime).status('scoped_echo', None) == {'state': 'on', 'reason': None}


def test_with_no_instance_that_has_the_field_the_tool_is_held_and_its_status_says_why(store: Store):
    # given
    calls: list = []
    runtime = rig(store, scoped(calls), a={'scope': '   '})
    tools = PluginTools(runtime)

    # when
    offered = [d['name'] for d in tools.definitions()]
    listed = rows(runtime)

    # then
    assert 'scoped_echo' not in offered and 'scoped_plain' in offered and tools.get('scoped_echo') is None
    assert ToolSet(runtime).status('scoped_echo', None) == {'state': 'off', 'reason': msg('tools.off.needs_field', field='Scope')}
    assert listed['scoped_echo']['kind'] == 'read' and listed['scoped_echo']['status'] == {'state': 'off', 'reason': 'No instance has Scope set'}
    assert listed['scoped_echo']['description'] == 'Echo from an instance that has a scope'
    assert listed['scoped_plain']['status'] == {'state': 'on', 'reason': None}
    assert calls == []


def test_a_tool_without_an_instance_input_needs_the_field_on_the_primary(store: Store):
    # given
    calls: list = []
    plugin = scoped(calls, instance_input=None)
    runtime = rig(store, plugin, a={}, b={'scope': 'shop'})

    # when
    held = [d['name'] for d in PluginTools(runtime).definitions()]
    add_instance(store, plugin, 'a', sealer_key=SEAL, primary=True, url=URL, scope='shop')
    runtime.refresh()
    offered = [d['name'] for d in PluginTools(runtime).definitions()]
    answer = PluginTools(runtime).call('scoped_echo', {}, NOW)

    # then
    assert 'scoped_echo' not in held and 'scoped_plain' in held
    assert 'scoped_echo' in offered and answer == {'from': 'a'} and calls == ['a']


def test_a_tool_whose_needs_field_names_no_field_breaks_its_plugin():
    # given
    bad = dataclasses.replace(scoped([]), tools=(Tool('scoped_echo', 'Echo from an instance that has a scope', SCHEMA, lambda ctx, args: {}, needs_field='scopes'),))

    # when
    found = Registry.of(bad, directory=DEMO_DIR).get('scoped')
    fine = Registry.of(scoped([]), directory=DEMO_DIR).get('scoped')

    # then
    assert found.state == 'broken'
    assert found.reason == msg('plugins.broken', name='Scoped', reason=msg('plugins.why.tool_needs_field', name='scoped_echo', field='scopes'))
    assert fine.state == 'ok'
