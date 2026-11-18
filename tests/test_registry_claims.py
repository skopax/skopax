import dataclasses

from skopax.registry import Registry
from tests.plugins import demo_plugin
from tests.plugins.base import minimal


def tool(name):
    return dataclasses.replace(demo_plugin.PLUGIN.tools[0], name=name)


def test_claims_go_in_id_order_even_when_after_reorders_the_load():
    # given
    first = minimal('aaa', 'Aaa', after=('zzz',), tools=(tool('shared_tool'),))
    last = minimal('zzz', 'Zzz', tools=(tool('shared_tool'),))

    # when
    reg = Registry.of(last, first)
    states = {x.id: x for x in reg.loaded()}

    # then
    assert [x.id for x in reg.loaded()] == ['zzz', 'aaa']
    assert (states['aaa'].state, states['zzz'].state) == ('ok', 'broken')
    assert 'shared_tool' in states['zzz'].reason and 'aaa' in states['zzz'].reason


def test_accessors_return_copies_without_a_refresh():
    # given
    reg = Registry.of(minimal('copy-one', 'Copy one'))

    # when
    reg.loaded()[0].state = 'broken'
    reg.get('copy-one').state = 'broken'
    reg.get('copy-one').reason = 'tampered'

    # then
    assert reg.loaded()[0].state == 'ok'
    assert (reg.get('copy-one').state, reg.get('copy-one').reason) == ('ok', None)
    assert [p.id for p in reg.usable()] == ['copy-one']


def test_a_plugin_repeating_its_own_tool_name_is_broken_naming_itself():
    # given
    twice = minimal('twice', 'Twice', tools=(tool('echo_one'), tool('echo_one')))

    # when
    loaded = Registry.of(twice).get('twice')

    # then
    assert loaded.state == 'broken'
    assert 'echo_one' in loaded.reason and 'twice' in loaded.reason


def test_a_plugin_repeating_its_own_knowledge_id_is_broken_naming_itself():
    # given
    note = demo_plugin.PLUGIN.knowledge[0]
    twice = minimal('notes-twice', 'Notes twice', knowledge=(dataclasses.replace(note, id='note-one'), dataclasses.replace(note, id='note-one')))

    # when
    loaded = Registry.of(twice).get('notes-twice')

    # then
    assert loaded.state == 'broken'
    assert 'note-one' in loaded.reason


def test_a_module_path_with_an_attribute_is_refused_with_the_path_reason():
    # given
    env = {'SKOPAX_PLUGINS': 'tests.plugins.demo_plugin:PLUGIN'}

    # when
    reg = Registry.load(env, builtin=(), entry_points=False)
    found = reg.get('tests.plugins.demo_plugin:PLUGIN')

    # then
    assert found.state == 'broken'
    assert 'module path tests.plugins.demo_plugin:PLUGIN must not name an attribute' in found.reason


def test_fields_given_as_a_list_are_checked_like_a_tuple():
    # given
    fine = dataclasses.replace(demo_plugin.PLUGIN, fields=list(demo_plugin.PLUGIN.fields))
    bad = dataclasses.replace(demo_plugin.PLUGIN, fields=list(demo_plugin.PLUGIN.fields) + [dataclasses.replace(demo_plugin.PLUGIN.fields[0], key='Bad_Key')])

    # when
    loaded = Registry.of(fine).get('demo')
    refused = Registry.of(bad).get('demo')

    # then
    assert loaded.state == 'ok'
    assert refused.state == 'broken' and 'field key Bad_Key is invalid or reserved' in refused.reason and 'could not be checked' not in refused.reason


def test_a_forced_broken_plugin_is_still_locked_by_the_edition_and_an_incompatible_one_stays_incompatible():
    # given
    locked = minimal('aaa-ee', 'Aaa EE', edition='enterprise')
    old = minimal('aaa-old', 'Aaa old', core='>=9')
    reg = Registry.of(locked, old, minimal('zzz', 'Zzz'))

    # when
    reg.mark_broken('aaa-ee', 'a migration failed')
    reg.mark_broken('aaa-old', 'a migration failed')

    # then
    assert [(x.id, x.state) for x in reg.loaded()] == [('aaa-ee', 'locked'), ('aaa-old', 'incompatible'), ('zzz', 'ok')]
    assert [p.id for p in reg.runnable()] == ['zzz']
