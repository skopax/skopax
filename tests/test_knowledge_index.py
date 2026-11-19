import dataclasses
import json
import logging

import pytest

from skopax.contract import KNOWLEDGE_INLINE_MAX, KNOWLEDGE_INLINE_TOTAL, KNOWLEDGE_OWNER_TOTAL, Field, Knowledge, OrgPack, Tool
from skopax.messages import msg
from skopax.registry import Registry
from tests.test_admin_plugins import HERE
from skopax.engine import iso
from skopax.investigator import Investigator, prompt
from skopax.investigator.tools import Effects
from tests.conftest import anomaly, settings, state
from tests.fake_anthropic import FakeAnthropic, reply, text, tool
from tests.plugins.base import minimal
from tests.test_investigator_tools import sources
from tests.plugin_kit import jira_runtime
from skopax.plugintools import PluginTools
from skopax.plugins import github, gitlab, jira
from tests.test_plugin_tools import NOW, SCHEMA, Rig

EARLY = minimal('early', 'Early', knowledge=(Knowledge('early-note', 'Early note', 'early words ' * 20), Knowledge('early-chat', 'Early chat note', 'chat words', slack=True)))
OWNER = minimal('owner', 'Owner', knowledge=(Knowledge('owner-note', 'Owner note', 'owner words', slack=True),))
LATE = minimal('late', 'Late `ticks`', knowledge=(Knowledge('late-note', 'Late `note`', 'late words'),))
HELD = minimal('held', 'Held', fields=(Field('url', 'URL', 'url', required=True), Field('spaces', 'Spaces', 'text')),
               tools=(Tool('held_read', 'Needs spaces', SCHEMA, lambda ctx, a: {}, instance_input=None, needs_field='spaces'),), checks=(),
               knowledge=(Knowledge('held-note', 'Held note', 'private held words'),))


def seeded(store, src: str) -> str:
    key = f'log|{src}|ERROR|x'
    st = state(anomaly(key, src=src, firstSeen=iso(NOW - 600))) | {'initialized': True, 'history': [{'t': iso(NOW - 300), 'key': key, 'ev': 'new', 'sev': 'warn', 'text': key, 'muted': False}]}
    store.save_state(st, None)
    return key


def investigate(store, rig, replies, pack=None):
    s = dataclasses.replace(settings(), anthropic_api_key='sk-test', investigate_mode='shadow')
    client = FakeAnthropic(replies)
    inv = Investigator(store, sources(plugin_tools=rig.tools), Effects(lambda k, b: {}, lambda k, t, b, notice=None: {}), s, client=client, clock=lambda: NOW)
    if pack is not None:
        inv.pack = lambda: pack
    inv.run_once(NOW)
    return client


def index_lines(system: str) -> list[str]:
    part = system.split('## Plug-in knowledge index', 1)
    return [line for line in part[1].split('\n') if line.startswith('- ')] if len(part) == 2 else []


@pytest.fixture
def rig(store):
    found = Rig(store, EARLY, OWNER, LATE, HELD)
    found.add(HELD, 'main', primary=True)
    return found


def test_a_card_run_inlines_its_owner_s_knowledge_and_lists_every_other_entry_on_one_line_in_load_order(store, rig):
    # given
    seeded(store, 'owner')

    # when
    client = investigate(store, rig, [reply(text('recap'), stop='end_turn')])
    system = client.requests[0]['system'][0]['text']
    tools = {t['name']: t for t in client.requests[0]['tools']}

    # then
    assert '## Owner note {#owner-note}\n\nFrom plug-in Owner.\n\nowner words' in system
    assert 'early words' not in system and 'late words' not in system and 'private held words' not in system
    assert index_lines(system) == ["- `early-note`: Early note (plug-in Early)", "- `early-chat`: Early chat note (plug-in Early)", "- `late-note`: Late 'note' (plug-in Late 'ticks')"]
    assert tools['plugin_knowledge']['input_schema']['properties']['id']['enum'] == ['early-note', 'early-chat', 'late-note'], 'the index entries, in load order'


def test_the_index_keeps_load_order_and_leaves_out_only_the_card_owner_s_entries(store, rig):
    # given
    seeded(store, 'late')

    # when
    system = investigate(store, rig, [reply(text('recap'), stop='end_turn')]).requests[0]['system'][0]['text']
    plain = prompt.system_prompt(*rig.runtime.knowledge_parts()[:1], index=rig.runtime.knowledge_parts()[1])

    # then
    assert index_lines(system) == ["- `early-note`: Early note (plug-in Early)", "- `early-chat`: Early chat note (plug-in Early)", "- `owner-note`: Owner note (plug-in Owner)"]
    assert index_lines(plain) == ["- `early-note`: Early note (plug-in Early)", "- `early-chat`: Early chat note (plug-in Early)", "- `late-note`: Late 'note' (plug-in Late 'ticks')",
                                  "- `owner-note`: Owner note (plug-in Owner)"]


def test_the_investigator_reads_one_entry_in_full_and_an_unknown_or_held_one_is_refused(store, rig):
    # given
    seeded(store, 'owner')
    replies = [reply(tool('plugin_knowledge', {'id': 'early-note'}, 't1'), tool('plugin_knowledge', {'id': 'held-note'}, 't2'), tool('plugin_knowledge', {'id': 'nope'}, 't3')),
               reply(text('recap'), stop='end_turn')]

    # when
    client = investigate(store, rig, replies)
    answers = {b['tool_use_id']: b for b in client.requests[1]['messages'][-1]['content'] if b.get('type') == 'tool_result'}

    # then
    early = answers['t1']['content'] if isinstance(answers['t1']['content'], str) else answers['t1']['content'][0]['text']
    assert early.startswith('## Early note\n\nFrom plug-in Early.\n\nearly words') and not answers['t1'].get('is_error')
    held, nope = json.dumps(answers['t2']), json.dumps(answers['t3'])
    assert answers['t2'].get('is_error') and 'private held words' not in held and 'one of early-note, early-chat, late-note' in held
    assert answers['t3'].get('is_error') and 'one of early-note, early-chat, late-note' in nope


def test_a_quick_run_reads_and_lists_only_the_entries_meant_for_slack(store, rig):
    # given
    inline, index = rig.runtime.knowledge_parts()
    box = rig.box(read_only=True, knowledge=[k for _, k in index])
    full = rig.box(knowledge=[k for _, k in index])

    # when
    chat = prompt.slack_system_prompt(inline, None, (), index)
    early, refused = box.call('plugin_knowledge', {'id': 'early-chat'}), box.call('plugin_knowledge', {'id': 'early-note'})
    schema = next(t for t in box.definitions() if t['name'] == 'plugin_knowledge')['input_schema']

    # then
    assert index_lines(chat) == ["- `early-chat`: Early chat note (plug-in Early)", "- `owner-note`: Owner note (plug-in Owner)"]
    assert not early[1] and 'chat words' in early[0]
    assert refused[1] and 'early words' not in refused[0]
    assert schema['properties']['id']['enum'] == ['early-chat', 'owner-note']
    assert 'plugin_knowledge' in [t['name'] for t in full.definitions()]


def test_a_run_with_no_readable_entry_is_not_offered_the_tool_and_an_org_pack_without_plugin_sections_hides_the_index(store, rig):
    # given
    seeded(store, 'owner')
    pack = OrgPack(process='# Process\n\n## Steps {#steps}\n\nLook first.\n', plugin_sections=False)

    # when
    client = investigate(store, rig, [reply(text('recap'), stop='end_turn')], pack=pack)
    system = client.requests[0]['system'][0]['text']
    names = [t['name'] for t in client.requests[0]['tools']]
    bare = rig.box()

    # then
    assert 'owner words' not in system and index_lines(system) == [] and 'Plug-in knowledge index' not in system
    assert 'plugin_knowledge' not in names and 'plugin_knowledge' not in [t['name'] for t in bare.definitions()]
    assert bare.call('plugin_knowledge', {'id': 'early-note'})[1]


def test_every_entry_is_offered_whatever_its_size_and_nothing_is_logged_as_left_out(store, caplog):
    # given
    big = minimal('crowded', 'Crowded', knowledge=tuple(Knowledge(f'part-{i}', f'Part {i}', 'x' * 8000) for i in range(30)))
    rig = Rig(store, big)

    # when
    with caplog.at_level(logging.INFO):
        inline, index = rig.runtime.knowledge_parts()
        system = prompt.system_prompt(inline, index=index)

    # then
    assert inline == [] and [k.id for _, k in index] == [f'part-{i}' for i in range(30)]
    assert len(system) - len(prompt.system_prompt()) < 3000, 'the prompt grows by one line per entry, not by its text'
    assert [r for r in caplog.records if r.name == 'skopax.runtime'] == []


def test_cross_cutting_notes_stay_inline_on_every_card_run(store):
    # given
    tools = PluginTools(jira_runtime(store, plugins=(OWNER, EARLY)))
    seeded(store, 'owner')
    client = FakeAnthropic([reply(text('recap'), stop='end_turn')])
    s = dataclasses.replace(settings(), anthropic_api_key='sk-test', investigate_mode='shadow')
    inv = Investigator(store, sources(plugin_tools=tools), Effects(lambda k, b: {}, lambda k, t, b, notice=None: {}), s, client=client, clock=lambda: NOW)

    # when
    inv.run_once(NOW)
    system = client.requests[0]['system'][0]['text']

    # then
    assert '## Jira {#jira}\n\nFrom plug-in Jira.' in system and '{#owner-note}' in system
    assert jira.PLUGIN.knowledge[0].inline and gitlab.PLUGIN.knowledge[0].inline


def test_the_github_working_note_is_inline_like_jira_and_gitlab_and_all_three_fit_the_inline_total():
    # given
    notes = [p.PLUGIN.knowledge[0] for p in (jira, gitlab, github)]

    # when
    sizes = [len(k.text) for k in notes]

    # then
    assert all(k.inline for k in notes)
    assert max(sizes) <= KNOWLEDGE_INLINE_MAX and sum(sizes) <= KNOWLEDGE_INLINE_TOTAL

def test_an_inline_entry_is_not_repeated_in_the_index_or_the_tool(store):
    # given
    tools = PluginTools(jira_runtime(store, plugins=(EARLY,)))
    inline, index = tools.knowledge_parts()
    box = Rig(store).box()
    box = type(box)(sources(plugin_tools=tools), store, box.effects, [], 'live', NOW, 'https://skopax.example.com', knowledge=[k for _, k in index])

    # when
    system = prompt.system_prompt(inline, index=index)
    enum = next(t for t in box.definitions() if t['name'] == 'plugin_knowledge')['input_schema']['properties']['id']['enum']

    # then
    assert [k.id for k in inline] == ['jira'] and system.count('{#jira}') == 1
    assert 'jira' not in [k.id for _, k in index] and '`jira`' not in system and enum == ['early-note', 'early-chat']


def test_an_inline_entry_over_its_cap_breaks_the_plugin():
    # when
    found = Registry.of(minimal('wordy', 'Wordy', knowledge=(Knowledge('wordy-note', 'Wordy', 'x' * (KNOWLEDGE_INLINE_MAX + 1), inline=True),)), directory=HERE).get('wordy')
    fits = Registry.of(minimal('fits', 'Fits', knowledge=(Knowledge('fits-note', 'Fits', 'x' * KNOWLEDGE_INLINE_MAX, inline=True),)), directory=HERE).get('fits')

    # then
    assert found.state == 'broken' and msg('plugins.why.knowledge_inline_long', id='wordy-note', limit=KNOWLEDGE_INLINE_MAX) in found.reason
    assert fits.state == 'ok'
    assert len(jira.PLUGIN.knowledge[0].text) <= KNOWLEDGE_INLINE_MAX and len(gitlab.PLUGIN.knowledge[0].text) <= KNOWLEDGE_INLINE_MAX


WIDE_A = minimal('wide-a', 'Wide A', knowledge=tuple(Knowledge(f'a-{i:02d}', f'A {i}', 'a' * KNOWLEDGE_INLINE_MAX, inline=True) for i in range(15)))
WIDE_B = minimal('wide-b', 'Wide B', knowledge=tuple(Knowledge(f'b-{i:02d}', f'B {i}', 'b' * KNOWLEDGE_INLINE_MAX, inline=True) for i in range(15)))


def test_inline_entries_stop_at_their_total_and_the_rest_are_listed_and_readable(store):
    # given
    rig = Rig(store, WIDE_A, WIDE_B)

    # when
    inline, index = rig.runtime.knowledge_parts()
    owned_inline, owned_index = rig.runtime.knowledge_parts([{'key': 'x|-/y', 'src': 'wide-b', 'ranKey': 'wide-b'}])
    system = prompt.system_prompt(inline, index=index)
    box = rig.box(knowledge=[k for _, k in index])
    enum = next(t for t in box.definitions() if t['name'] == 'plugin_knowledge')['input_schema']['properties']['id']['enum']

    # then
    assert [k.id for k in inline] == ['a-00', 'a-01', 'a-02'] and sum(len(k.text.split('\n\n', 1)[1]) for k in inline) <= KNOWLEDGE_INLINE_TOTAL
    assert system.count('a' * KNOWLEDGE_INLINE_MAX) == 3 and 'b' * KNOWLEDGE_INLINE_MAX not in system
    assert [k.id for _, k in index] == [f'a-{i:02d}' for i in range(3, 15)] + [f'b-{i:02d}' for i in range(15)] and enum == [k.id for _, k in index]
    assert [k.id for k in owned_inline] == ['a-00', 'a-01', 'a-02'] + [f'b-{i:02d}' for i in range(8)], "the card owner's entries have their own total and do not count against the shared one"
    assert [k.id for _, k in owned_index] == [f'a-{i:02d}' for i in range(3, 15)] + [f'b-{i:02d}' for i in range(8, 15)]


BIG_OWNER = minimal('big-owner', 'Big owner', knowledge=tuple(Knowledge(f'o-{i:02d}', f'O {i}', 'o' * 8000) for i in range(20)))


def test_the_card_owner_s_entries_stop_at_their_own_total_and_the_rest_are_listed_and_readable(store):
    # given
    rig = Rig(store, BIG_OWNER, WIDE_A)
    card = {'key': 'x|-/y', 'src': 'big-owner', 'ranKey': 'big-owner'}

    # when
    inline, index = rig.runtime.knowledge_parts([card])
    owned = [k for k in inline if k.id.startswith('o-')]
    box = rig.box(knowledge=[k for _, k in index])
    enum = next(t for t in box.definitions() if t['name'] == 'plugin_knowledge')['input_schema']['properties']['id']['enum']

    # then
    assert [k.id for k in owned] == ['o-00', 'o-01'] and sum(len(k.text.split('\n\n', 1)[1]) for k in owned) <= KNOWLEDGE_OWNER_TOTAL
    assert [k.id for k in inline if k.id.startswith('a-')] == ['a-00', 'a-01', 'a-02'], 'the shared inline total is untouched by the owner'
    assert [k.id for _, k in index] == [f'o-{i:02d}' for i in range(2, 20)] + [f'a-{i:02d}' for i in range(3, 15)]
    assert enum == [k.id for _, k in index]
