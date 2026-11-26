import dataclasses
import json
from pathlib import Path

import pytest

from skopax import locales
from skopax.actors import TOKEN_PREFIX
from skopax.contract import ASKED_ACCESS, QUESTION_CORE_OWNERS, QUESTIONS_FIELD, Capabilities, Check, Field, Knowledge, PassResult, Sweep, Tool
from skopax.instances import Instances, PluginState
from skopax.instances import effective_fields
from skopax.mcptools.bridge import asker_role
from skopax.investigator import Investigator
from skopax.investigator.tools import Effects, Toolbox
from skopax.investigator.toolset import CORE
from skopax.plugins import jira, mcp_tools
from skopax.plugintools import PluginTools
from skopax.registry import Registry
from skopax.runtime import Runtime
from skopax.sealer import Sealer
from skopax.store import STATE_ID
from tests.conftest import settings
from tests.fake_anthropic import FakeAnthropic, reply, text
from tests.test_investigator_tools import sources
from tests.test_plugin_tools import NOW, Rig
from tests.plugin_kit import JIRA_VALUES, KIT_KEY, add_instance
from tests.plugins import demo_plugin
from tests.plugins.base import minimal

SCHEMA = {'type': 'object', 'properties': {'what': {'type': 'string'}}, 'additionalProperties': False}
HELP = 'Card runs always read this connection. This picks whose questions (Slack asks, chat messages, page asks) may read it too.'

MCP_HELP = 'Card runs always may use this server. This picks whose chat messages and asks may use it too.'


def where(ctx, args):
    return {'instance': ctx.instance.name if ctx.instance else None}


SHOP = minimal('shop-ads', 'Shop ads', capabilities=Capabilities(), fields=(Field('url', 'URL', 'url', required=True),),
               tools=(Tool('shop_ads', 'Read the shop ads of {field:url}', SCHEMA, where, instance_input='instance'),))
SOLO = minimal('solo-docs', 'Solo docs', capabilities=Capabilities(), fields=(Field('url', 'URL', 'url', required=True),), max_instances=1,
               tools=(Tool('solo_docs', 'Read the solo docs', SCHEMA, where, instance_input=None),))
QUIET = minimal('quiet', 'Quiet', capabilities=Capabilities())


@pytest.fixture
def rig(store):
    return Rig(store, SHOP, SOLO)


def offered(box) -> dict[str, dict]:
    return {d['name']: d for d in box.definitions()}


def test_a_connection_set_to_card_investigations_is_not_offered_in_a_question(rig):
    # given
    rig.add(SHOP, 'main', primary=True, questions='none')
    rig.add(SOLO, 'docs', primary=True, questions='none')
    box = rig.box(ask=True, role='admin')

    # when
    names = offered(box)
    out, error = box.call('shop_ads', {'what': 'spend'})

    # then
    assert 'shop_ads' not in names and 'solo_docs' not in names
    assert error and 'not available in questions' in out


def test_operators_only_hides_the_tool_from_a_viewers_question_and_shows_it_to_an_operator(rig):
    # given
    rig.add(SHOP, 'main', primary=True, questions='operators')
    rig.add(SOLO, 'docs', primary=True, questions='operators')

    # when
    seen = {role: sorted(n for n in offered(rig.box(ask=True, role=role)) if n in ('shop_ads', 'solo_docs')) for role in ('viewer', None, 'operator', 'admin')}
    out, error = rig.box(ask=True, role='operator').call('shop_ads', {'what': 'spend'})

    # then
    assert seen == {'viewer': [], None: [], 'operator': ['shop_ads', 'solo_docs'], 'admin': ['shop_ads', 'solo_docs']}
    assert not error and 'main' in out


def test_card_runs_keep_every_connection_whatever_the_setting(rig):
    # given
    rig.add(SHOP, 'main', primary=True, questions='none')
    rig.add(SHOP, 'east', questions='operators')
    rig.add(SOLO, 'docs', primary=True, questions='none')
    box = rig.box()

    # when
    names = offered(box)
    out, error = box.call('shop_ads', {'instance': 'east'})

    # then
    assert names['shop_ads']['input_schema']['properties']['instance']['enum'] == ['main', 'east'] and 'solo_docs' in names
    assert not error and 'east' in out


def test_a_shared_tool_drops_only_the_refused_instance_from_its_enum_and_refuses_a_call_to_it(rig):
    # given
    rig.add(SHOP, 'main', primary=True, questions='none', url='https://main-secret.example.com')
    rig.add(SHOP, 'east', url='https://east.example.com')
    box = rig.box(ask=True, role='viewer')

    # when
    tool = offered(box)['shop_ads']
    pick = tool['input_schema']['properties']['instance']
    named, refused = box.call('shop_ads', {'instance': 'main'})
    kept, error = box.call('shop_ads', {'instance': 'east'})

    # then
    assert pick['enum'] == ['east']
    assert 'main' not in pick['description'] and 'main' not in tool['description']
    assert refused and 'main is not available in questions' in named and 'not configured' not in named
    assert not error and 'east' in kept


def test_the_brief_names_the_connections_not_available_in_questions(store, rig):
    # given
    rig.add(SHOP, 'main', primary=True)
    rig.add(SHOP, 'east', questions='operators')
    rig.add(SOLO, 'docs', primary=True, questions='none')
    s = dataclasses.replace(settings(), anthropic_api_key='sk-test', investigate_mode='shadow')
    client = FakeAnthropic([reply(text('answer'), stop='end_turn')])
    inv = Investigator(store, sources(plugin_tools=rig.tools), Effects(lambda k, b: {}, lambda k, t, b, notice=None: {}), s, client=client, clock=lambda: NOW)
    inv.role_of = lambda action: 'viewer'
    action = store.add_action({'type': 'ask', 'source': 'chat', 'key': 'chat', 'target': 'chat:c1', 'text': 'How do the ads do?', 'cards': [], 'requestedBy': 'alice@example.com',
                               'chat': {'id': 'c1'}}, 'processing')

    # when
    inv.investigate([], action, NOW)
    first = client.requests[0]['messages'][0]['content']
    brief = first if isinstance(first, str) else ''.join(b.get('text', '') for b in first)

    # then
    lines = [line for line in brief.splitlines() if 'not available in questions' in line]
    assert lines == ['Connections not available in questions: Shop ads east, Solo docs docs. If the question needs one of them, say that this connection is not available in questions; do not guess its answer.']
    assert 'shop_ads' in [t['name'] for t in client.requests[0]['tools']] and 'solo_docs' not in [t['name'] for t in client.requests[0]['tools']]


def test_the_default_keeps_todays_behaviour(rig):
    # given
    rig.add(SHOP, 'main', primary=True)
    rig.add(SOLO, 'docs', primary=True)

    # when
    asked = offered(rig.box(ask=True, role=None))
    field = next(f for f in effective_fields(SHOP) if f.key == QUESTIONS_FIELD)

    # then
    assert 'shop_ads' in asked and 'solo_docs' in asked
    assert asked['shop_ads']['input_schema']['properties']['instance']['enum'] == ['main']
    assert (field.label, field.kind, field.default, field.choices, field.group, field.help) == ('Available in questions', 'choice', 'everyone', ('everyone', 'operators', 'none'), 'advanced', HELP)
    assert dict(field.choice_labels) == {'everyone': "Card runs + everyone's questions", 'operators': 'Card runs + operator and admin questions', 'none': 'Card runs only'}
    assert [f.key for f in effective_fields(SOLO)].count(QUESTIONS_FIELD) == 1 and not effective_fields(QUIET)
    assert QUESTIONS_FIELD not in [f['key'] for f in rig.runtime.fields_of('quiet')] and QUESTIONS_FIELD in [f['key'] for f in rig.runtime.fields_of('shop-ads')]


def test_labels_say_cards_always_included():
    # given
    labels = ASKED_ACCESS.choice_labels

    # when
    starts = [label.startswith('Card runs') for label in labels.values()]

    # then
    assert list(labels) == ['everyone', 'operators', 'none'] and all(starts)


def test_the_mcp_chat_role_labels_say_cards_always_included():
    # given
    field = next(f for f in mcp_tools.PLUGIN.fields if f.key == 'chat-role')

    # when
    starts = [label.startswith('Card runs') for label in field.choice_labels.values()]

    # then
    assert list(field.choice_labels) == ['viewer', 'operator', 'admin', 'cards'] and all(starts) and field.help == MCP_HELP


@pytest.mark.parametrize('lang', ['de', 'es', 'fr', 'tr'])
def test_each_core_pack_translates_the_available_in_questions_texts(lang):
    # given
    page = json.loads((locales.ROOT / lang / 'page.json').read_text(encoding='utf-8'))['page']
    texts = ('Available in questions', "Card runs + everyone's questions", 'Card runs + operator and admin questions', 'Card runs + admin questions', 'Card runs only', HELP, MCP_HELP)

    # when
    missing = [t for t in texts if not page.get(t) or page[t] == t]

    # then
    assert missing == []


def test_an_mcp_ask_goes_by_the_role_of_its_token(rig):
    # given
    rig.add(SHOP, 'main', primary=True, questions='operators')
    users = type('Users', (), {'get': lambda self, by: {'role': 'viewer'}})()
    action = {'type': 'ask', 'source': 'chat', 'text': 'How do the ads do?', 'requestedBy': 'alice@example.com', 'mcpRole': 'operator'}

    # when
    role = asker_role(action, users)
    names = offered(rig.box(ask=True, chat=True, read_only=True, role=role))
    plain = offered(rig.box(ask=True, chat=True, read_only=True, role=asker_role(action | {'mcpRole': 'viewer'}, users)))

    # then
    assert role == 'operator' and 'shop_ads' in names and 'shop_ads' not in plain



def quiet_pass(ctx):
    return Sweep(PassResult('shelf', True, 'ok'))


def users_of(ctx, args):
    return {'users': sorted(u.name for u in ctx.users())}


SHELF = minimal('shelf', 'Shelf', capabilities=Capabilities(), fields=(Field('url', 'URL', 'url', required=True),),
                checks=(Check('shelf', 'Shelf', 900, 0, ('shelf',), quiet_pass, sections=('shelfRows',)),),
                knowledge=(Knowledge('shelf-notes', 'Shelf notes', 'The shelf secret recipe is cinnamon.', slack=True),),
                tools=(Tool('shelf_read', 'Read the shelf', SCHEMA, users_of, instance_input='instance'),))
PICKER = minimal('picker', 'Picker', capabilities=Capabilities(), fields=(Field('url', 'URL', 'url', required=True), Field('shelf', 'Shelf instance', 'instance', choices=('shelf',))),
                 uses=('shelf',), tools=(Tool('picker_read', 'Read the picker', SCHEMA, where, instance_input='instance'),))


def shelf_rig(store, *plugins) -> Rig:
    rig = Rig(store)
    rig.registry = Registry.of(*plugins, directory=Path(demo_plugin.__file__).parent)
    rig.instances = Instances(store.db, rig.registry, lambda: Sealer(rig.key))
    rig.runtime = Runtime(rig.registry, rig.state, rig.instances)
    rig.tools = PluginTools(rig.runtime)
    return rig


def question(rig, role='viewer', **over):
    return rig.box(ask=True, role=role, **over)


def test_jira_set_to_card_investigations_is_not_offered_in_a_question(store):
    # given
    registry = Registry.of(jira.PLUGIN)
    runtime = Runtime(registry, PluginState(store.db), Instances(store.db, registry, lambda: Sealer(KIT_KEY)))
    add_instance(store, jira.PLUGIN, 'main', sealer_key=KIT_KEY, primary=True, **(JIRA_VALUES | {'questions': 'none'}))
    runtime.refresh()
    tools = PluginTools(runtime)

    def built(**over):
        return Toolbox(sources(plugin_tools=tools), store, Effects(lambda key, by: {}, lambda key, text, by, *_, notice=None: {}), ['log|a|ERROR|x'], 'live', NOW,
                       'https://skopax.example.com', settings=settings(), **over)

    # when
    asked = built(ask=True, role='admin')
    card = built()
    out, error = asked.call('jira_search', {'jql': 'project = OPS'})

    # then
    assert QUESTIONS_FIELD in [f.key for f in effective_fields(jira.PLUGIN)]
    assert set(QUESTION_CORE_OWNERS) == {s.needs for s in CORE.values() if s.needs}
    assert 'jira_search' not in offered(asked) and 'jira_issue' not in offered(asked)
    assert 'jira_search' in offered(card) and 'jira_issue' in offered(card)
    assert error and 'not available in questions' in out
    assert asked.not_in_questions() == ['Jira main']


def test_reviews_and_deploy_assessments_keep_a_card_only_connection(rig):
    # given
    rig.add(SHOP, 'main', primary=True, questions='none')

    # when
    verify = offered(rig.box(review='verify', read_only=True))
    settle = offered(rig.box(review='settle', read_only=True))
    report = offered(rig.box(report_id='r1'))

    # then
    assert 'shop_ads' in verify and 'shop_ads' in settle and 'shop_ads' in report


def test_an_api_token_ask_counts_as_a_viewer(rig):
    # given
    rig.add(SHOP, 'main', primary=True, questions='operators')
    users = type('Users', (), {'get': lambda self, by: None})()
    action = {'type': 'ask', 'source': 'page', 'text': 'How do the ads do?', 'requestedBy': TOKEN_PREFIX + 'ci'}

    # when
    role = asker_role(action, users)
    names = offered(question(rig, role=role))

    # then
    assert role == 'viewer' and 'shop_ads' not in names


def test_an_mcp_ask_end_to_end_reads_with_the_token_role(store, rig):
    # given
    rig.add(SHOP, 'main', primary=True, questions='operators')
    s = dataclasses.replace(settings(), anthropic_api_key='sk-test', investigate_mode='shadow')
    users = type('Users', (), {'get': lambda self, by: {'role': 'viewer'}})()

    inv = Investigator(store, sources(plugin_tools=rig.tools), Effects(lambda k, b: {}, lambda k, t, b, notice=None: {}), s, client=FakeAnthropic([]), clock=lambda: NOW)
    inv.role_of = lambda action: asker_role(action, users)

    def asked(role):
        client = FakeAnthropic([reply(text('answer'), stop='end_turn')])
        inv.client = client
        action = store.add_action({'type': 'ask', 'source': 'chat', 'key': 'chat', 'target': f'chat:{role}', 'text': 'How do the ads do?', 'cards': [], 'requestedBy': 'alice@example.com',
                                   'chat': {'id': role}, 'via': 'mcp', 'mcpRole': role}, 'processing')
        inv.investigate([], action, NOW)
        return [t['name'] for t in client.requests[0]['tools']]

    # when
    operator = asked('operator')
    viewer = asked('viewer')

    # then
    assert 'shop_ads' in operator and 'shop_ads' not in viewer


def test_a_question_does_not_read_a_refused_instances_pass_table_or_knowledge(store):
    # given
    rig = shelf_rig(store, SHELF)
    rig.add(SHELF, 'main', primary=True, questions='none')
    rows = [{'plugin': 'shelf', 'name': 'main row'}, {'plugin': 'shelf', 'cluster': 'east', 'name': 'east row'}]
    store.meta.update_one({'_id': STATE_ID}, {'$set': {'shelfRows': rows}}, upsert=True)
    rig.add(SHELF, 'east')

    # when
    shared_out, shared_err = question(rig).call('pass_table', {'source': 'shelfRows'})
    card_out, card_err = rig.box().call('pass_table', {'source': 'shelfRows'})
    rig.add(SHELF, 'east', questions='none')
    gone_out, gone_err = question(rig).call('pass_table', {'source': 'shelfRows'})
    inline, index = rig.tools.knowledge_parts((), allowed=question(rig).gate)
    card_inline, card_index = rig.tools.knowledge_parts(())
    asked_knowledge = 'plugin_knowledge' in offered(question(rig))

    # then
    assert not shared_err and 'east row' in shared_out and 'main row' not in shared_out
    assert not card_err and 'east row' in card_out and 'main row' in card_out
    assert gone_err and 'row' not in gone_out
    assert inline == [] and index == [] and not asked_knowledge
    assert [k.id for k in card_inline] + [k.id for _, k in card_index] == ['shelf-notes']


def test_a_question_leaves_the_indices_of_a_refused_instance_out_of_user_reads(store):
    # given
    rig = shelf_rig(store, SHELF, PICKER)
    rig.add(SHELF, 'main', primary=True)
    rig.add(PICKER, 'open', primary=True, shelf='main')
    rig.add(PICKER, 'shut', shelf='main', questions='none')

    # when
    asked, asked_err = question(rig).call('shelf_read', {})
    card, card_err = rig.box().call('shelf_read', {})

    # then
    assert not asked_err and not card_err
    assert json.loads(asked)['users'] == ['open'] and json.loads(card)['users'] == ['open', 'shut']


def peer_read(ctx, args):
    seen = ctx.peer('shelf')
    return {'peer': seen.info.name if seen is not None else None, 'peers': [p.info.name for p in ctx.peers('shelf')]}


READER = minimal('reader', 'Reader', capabilities=Capabilities(), fields=(Field('url', 'URL', 'url', required=True), Field('shelf', 'Shelf instance', 'instance', choices=('shelf',))),
                 uses=('shelf',), tools=(Tool('reader_read', 'Read through the shelf', SCHEMA, peer_read, instance_input='instance'),))


def test_a_question_cannot_read_a_refused_peer_through_another_connection(store):
    # given
    rig = shelf_rig(store, SHELF, READER)
    rig.add(SHELF, 'main', primary=True, questions='none')
    rig.add(SHELF, 'east')
    rig.add(READER, 'kafka', primary=True, shelf='main')

    # when
    asked, asked_err = question(rig).call('reader_read', {})
    card, card_err = rig.box().call('reader_read', {})
    rig.add(READER, 'kafka', primary=True, shelf='east')
    other, other_err = question(rig).call('reader_read', {})

    # then
    assert asked_err and 'main is not available in questions' in asked
    assert not card_err and json.loads(card) == {'peer': 'main', 'peers': ['main', 'east']}
    assert not other_err and json.loads(other) == {'peer': 'east', 'peers': ['east']}


LOGS = minimal('elasticsearch', 'Elasticsearch', capabilities=Capabilities(), fields=(Field('url', 'URL', 'url', required=True),),
               tools=(Tool('logs_read', 'Read the logs', SCHEMA, where, instance_input='instance'),))


def test_the_self_review_facts_are_refused_in_a_question_when_the_log_connection_is_refused(store):
    # given
    rig = shelf_rig(store, LOGS)
    rig.add(LOGS, 'main', primary=True, questions='none')
    read = []

    def box(**over):
        built = rig.box(**over)
        built.sources = dataclasses.replace(built.sources, selfreview=lambda now: read.append(now) or {'runs': 3})
        return built

    # when
    asked, asked_err = box(ask=True, role='admin').call('self_review', {'facts': True})
    card, card_err = box().call('self_review', {'facts': True})

    # then
    assert asked_err and 'not available in questions' in asked
    assert not card_err and json.loads(card)['facts'] == {'runs': 3} and len(read) == 1
