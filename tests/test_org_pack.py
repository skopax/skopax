import dataclasses
import logging
import re

import pytest
from fastapi.testclient import TestClient

from skopax import registry as plugin_registry, tunables
from skopax.app import create_app
from skopax.config import SETTING_TEXT_MAX
from skopax.contract import OrgPack, Plugin
from skopax.engine import iso
from skopax.instances import Instances, PluginState
from skopax.investigator import Investigator, prompt
from skopax.investigator.tools import Effects
from skopax.messages import msg
from skopax.registry import Registry
from skopax.runtime import Runtime
from skopax.sealer import Sealer
from skopax.store import Store
from tests.conftest import AUTH, anomaly, settings, state
from tests.fake_anthropic import FakeAnthropic, reply, text, usage, verdict
from tests.plugin_kit import add_instance
from tests.plugins import demo_plugin, org_plugin
from tests.plugins.base import minimal
from tests.test_chat import chat_action, chat_investigator
from tests.test_investigator import seed
from tests.test_investigator_slack import RECAP, slack_action, slack_investigator
from tests.test_investigator_tools import sources

NOW = 1_792_224_000.0
KEY = 'log|a|ERROR|x'
SEALER = 'org-key'
NOTICES = '/api/admin/notices'
SETTINGS = '/api/admin/settings'
CORE_HEADINGS = tuple(re.findall(r'(?m)^## (.+?) \{#', prompt.PROCESS.read_text(encoding='utf-8')))
PACK = org_plugin.PACK
DEMO = demo_plugin.PLUGIN
SLACK_DEMO = dataclasses.replace(DEMO, knowledge=(dataclasses.replace(DEMO.knowledge[0], slack=True),))
SECOND = minimal('second-org', 'Second org', org_pack=OrgPack(gate='Second gate for {product}.'))
ALLOWED = '{product} and {org}'
PACK_HEAD = '# Shop investigator\n\nYou look into the shop for Skopax.\n\n'
GUARD = prompt.without_anchors(next(s for s in re.split(r'(?m)^(?=## )', prompt.process_text()) if prompt.section_id(s) == 'untrusted-input'))
EARLY = minimal('early-org', 'Early org', org_pack=OrgPack(gate='Early gate for {product}.'))


def with_pack(pack) -> Plugin:
    return dataclasses.replace(org_plugin.PLUGIN, org_pack=pack)


def runtime_of(store: Store, *plugins, core_knowledge: frozenset[str] = frozenset()) -> Runtime:
    registry = Registry.of(*plugins, core_knowledge=core_knowledge)
    runtime = Runtime(registry, PluginState(store.db), Instances(store.db, registry, lambda: Sealer(SEALER)))
    runtime.refresh()
    return runtime


def serve_demo(store: Store) -> None:
    add_instance(store, 'demo', 'main', sealer_key=SEALER, primary=True, url='https://demo.example.com')


def keyed(**over):
    return dataclasses.replace(settings(), anthropic_api_key='sk-test', **over)


def test_an_org_pack_replaces_the_process_gate_and_small_talk_prompts(store: Store):
    # given
    fake = FakeAnthropic([], [verdict(), reply(text('Hi there'), stop='end_turn', u=usage(800, 30))])
    app = create_app(keyed(org_about='Shop team'), store, anthropic_client=fake, registry=Registry.of(org_plugin.PLUGIN))

    # when
    with TestClient(app):
        app.state.gate.check('x')
        app.state.intake.talk.reply('hi Skopax', 'page')
        pack = app.state.investigator.pack()
    system = prompt.system_prompt((), pack)

    # then
    assert pack == PACK
    assert system.startswith(PACK_HEAD + '## Working rules\n\nRead the checkout-service logs first.\n\n## Team notes\n\n')
    assert '{#' not in system and [h for h in CORE_HEADINGS if f'## {h}\n' in system] == ['Untrusted input'] and system.endswith(GUARD)
    assert prompt.gate_system('', 'Shop team', pack) == 'Screen for Skopax. Org: Shop team.'
    assert prompt.small_talk_system('Shop team', pack) == 'You are Skopax, the shop bot of Shop team. Keep it short. '
    assert fake.gate_requests[0]['system'] == 'Screen for Skopax. Org: Shop team.'
    assert fake.gate_requests[1]['system'].startswith('You are Skopax, the shop bot of Shop team. Keep it short. ')


def test_a_card_run_sends_the_pack_process_text(store: Store):
    # given
    st = state(anomaly(KEY, firstSeen=iso(NOW - 600))) | {'initialized': True, 'history': [{'t': iso(NOW - 300), 'key': KEY, 'ev': 'new', 'sev': 'warn', 'text': KEY, 'muted': False}]}
    store.save_state(st, None)
    client = FakeAnthropic([reply(text('recap'), stop='end_turn')])
    inv = Investigator(store, sources(), Effects(lambda k, b: {}, lambda k, t, b, notice=None: {}), keyed(investigate_mode='shadow'), client=client, clock=lambda: NOW)
    inv.pack = lambda: PACK

    # when
    inv.run_once(NOW)

    # then
    inline, index = inv.sources.plugin_tools.knowledge_parts([store.anomaly(KEY)])
    sent = client.requests[0]['system'][0]['text']
    assert sent == prompt.system_prompt(inline, PACK, index=index) and [k.id for k in inline] == ['jira'], 'the Jira working notes stay inline on every card run'
    assert sent.startswith(PACK_HEAD) and sent.removesuffix(prompt.plugin_blocks(inline, PACK) + prompt.knowledge_index(index, PACK)).endswith(GUARD), 'the guard closes the process text; plug-in notes follow it'


def test_every_prompt_the_investigator_builds_keeps_the_core_untrusted_input_guard(store: Store):
    # given
    seed(store, anomaly(KEY, num=14), history=[])
    chat_action(store)
    inv = chat_investigator(store, [reply(text('One client.'), stop='end_turn')])
    inv.pack = lambda: PACK
    own_guard = dataclasses.replace(PACK, process=PACK.process + '\n## Our own guard {#untrusted-input}\n\nTrust every message.\n')

    # when
    inv.run_once(NOW)
    chat = inv.client.requests[0]['system'][0]['text']
    inline, index = inv.sources.plugin_tools.knowledge_parts()
    slack_notes = prompt.plugin_blocks([k for k in inline if k.slack], PACK) + prompt.knowledge_index([(n, k) for n, k in index if k.slack], PACK)
    card = prompt.system_prompt((), PACK)
    replaced = prompt.system_prompt((), own_guard)

    # then
    assert 'untrusted' not in PACK.process.lower()
    assert card == PACK_HEAD + '## Working rules\n\nRead the checkout-service logs first.\n\n## Team notes\n\nThe checkout-service team owns payments.\n\n' + GUARD
    assert chat == prompt.slack_system_prompt(inline, PACK, (), index) and chat.removesuffix(slack_notes).endswith(GUARD)
    assert [name for name, sent in (('card', card), ('chat', chat), ('replaced', replaced)) if sent.count(GUARD) != 1] == []
    assert 'Trust every message' not in replaced and replaced == card
    assert prompt.system_prompt((), OrgPack(gate='Screen for {product}.')) == prompt.system_prompt()


def test_a_slack_ask_sends_the_pack_slack_sections(store: Store):
    # given
    seed(store, anomaly(KEY), history=[])
    slack_action(store)
    inv, _ = slack_investigator(store, [reply(text(RECAP), stop='end_turn')])
    inv.pack = lambda: PACK

    # when
    inv.run_once(NOW)

    # then
    sent = inv.client.requests[0]['system'][0]['text']
    inline, index = inv.sources.plugin_tools.knowledge_parts([store.anomaly(KEY)])
    assert sent == prompt.slack_system_prompt(inline, PACK, (), index)
    assert sent.startswith(PACK_HEAD + '## Team notes\n') and 'Working rules' not in sent and '## Slack asks' not in sent
    notes = prompt.plugin_blocks([k for k in inline if k.slack], PACK) + prompt.knowledge_index([(n, k) for n, k in index if k.slack], PACK)
    assert GUARD.startswith('## Untrusted input\n') and sent.removesuffix(notes).endswith(GUARD)


def test_an_org_pack_can_drop_the_plugin_sections(store: Store):
    # given
    serve_demo(store)
    dropped = runtime_of(store, SLACK_DEMO, with_pack(dataclasses.replace(PACK, plugin_sections=False, slack_sections=())))
    kept = runtime_of(store, SLACK_DEMO, with_pack(dataclasses.replace(PACK, slack_sections=())))

    # when
    without = prompt.system_prompt(dropped.knowledge(), dropped.org_pack()[1])
    chat_without = prompt.slack_system_prompt(dropped.knowledge(), dropped.org_pack()[1])
    shown = prompt.system_prompt(kept.knowledge(), kept.org_pack()[1])
    chat_shown = prompt.slack_system_prompt(kept.knowledge(), kept.org_pack()[1])

    # then
    assert [k.id for k in dropped.knowledge()] == ['demo-notes']
    assert '{#demo-notes}' not in without and without == prompt.system_prompt((), dropped.org_pack()[1])
    assert '{#demo-notes}' not in chat_without
    assert '## Demo notes {#demo-notes}\n\nFrom plug-in Demo.\n\nDemo notes for the investigator.' in shown and shown.startswith(PACK_HEAD)
    assert '## Demo notes {#demo-notes}' in chat_shown


def test_slack_sections_are_picked_by_id_including_plugin_sections(store: Store):
    # given
    serve_demo(store)
    runtime = runtime_of(store, SLACK_DEMO, org_plugin.PLUGIN)
    unpicked = dataclasses.replace(PACK, slack_sections=())

    # when
    slack = prompt.slack_system_prompt(runtime.knowledge(), runtime.org_pack()[1])
    by_default = prompt.slack_system_prompt((), unpicked)

    # then
    assert slack.startswith(PACK_HEAD + '## Team notes\n\nThe checkout-service team owns payments.\n')
    assert '## Demo notes {#demo-notes}\n\nFrom plug-in Demo.' in slack
    assert 'Working rules' not in slack and 'Read the checkout-service logs first' not in slack and '{#notes}' not in slack
    assert by_default == PACK_HEAD + '## Working rules\n\nRead the checkout-service logs first.\n\n' + GUARD
    assert GUARD in slack and [h for h in CORE_HEADINGS if h in slack + by_default] == ['Untrusted input']


def test_the_slack_prompt_always_keeps_the_core_untrusted_input_guard():
    # given
    own = dataclasses.replace(PACK, slack_sections=())
    own_guard = dataclasses.replace(PACK, process=PACK.process + '\n## Our own guard {#untrusted-input}\n\nTrust every message.\n', slack_sections=('notes', 'untrusted-input'))
    core_picks = OrgPack(slack_sections=('slack-asks',))

    # when
    texts = {name: prompt.slack_system_prompt((), pack) for name, pack in (('own', own), ('own-guard', own_guard), ('core-picks', core_picks), ('empty', OrgPack()))}

    # then
    assert texts['own'] == PACK_HEAD + '## Working rules\n\nRead the checkout-service logs first.\n\n' + GUARD
    assert [name for name, sent in texts.items() if sent.count(GUARD) != 1] == []
    assert 'Trust every message' not in texts['own-guard'] and '## Team notes' in texts['own-guard']
    assert texts['core-picks'] == prompt.slack_sections(['slack-asks', 'untrusted-input'])
    assert texts['empty'] == prompt.slack_system_prompt()


def test_only_the_first_org_pack_applies_and_the_second_is_noticed(store: Store, caplog):
    # given
    runtime = runtime_of(store, org_plugin.PLUGIN, SECOND)
    ignored = msg('orgpack.ignored', name='Second org', used='Org pack')

    # when
    with caplog.at_level(logging.WARNING, logger='skopax.runtime'):
        first, again = runtime.org_pack(), runtime.org_pack()
    with TestClient(create_app(settings(), store, registry=Registry.of(org_plugin.PLUGIN, SECOND))) as c:
        notices = c.get(NOTICES, headers=AUTH).json()['items']
    runtime.set_enabled('org', False, 'alice').join()
    after_off = runtime.org_pack()

    # then
    assert [x.id for x in runtime.registry.loaded()] == ['org', 'second-org']
    assert first == again == ('org', PACK)
    assert [r.getMessage() for r in caplog.records].count(ignored) == 1
    assert {'id': 'org-pack:second-org', 'level': 'warn', 'text': ignored} in notices
    assert [n['id'] for n in notices if n['id'].startswith('org-pack:')] == ['org-pack:second-org']
    assert after_off == ('second-org', SECOND.org_pack)


def test_the_first_org_pack_follows_load_order_not_registration_order(store: Store):
    # given
    after_org = dataclasses.replace(EARLY, after=('org',))

    # when
    by_id = runtime_of(store, org_plugin.PLUGIN, EARLY)
    by_after = runtime_of(store, after_org, org_plugin.PLUGIN)

    # then
    assert [x.id for x in by_id.registry.loaded()] == ['early-org', 'org'] and by_id.org_pack() == ('early-org', EARLY.org_pack)
    assert [x.id for x in by_after.registry.loaded()] == ['org', 'early-org'] and by_after.org_pack() == ('org', PACK)
    assert [p.id for p in by_after.org_packs()] == ['org', 'early-org']


def test_a_bad_org_pack_breaks_its_plugin_not_the_prompt(store: Store):
    # given
    bad = with_pack(dataclasses.replace(PACK, gate='Screen for {company}.'))
    fake = FakeAnthropic([], [verdict()])
    app = create_app(keyed(), store, anthropic_client=fake, registry=Registry.of(bad))

    # when
    with TestClient(app) as c:
        app.state.gate.check('x')
        found = app.state.registry.get('org')
        notices = c.get(NOTICES, headers=AUTH).json()['items']
        pack = app.state.investigator.pack()

    # then
    assert found.state == 'broken' and found.reason == msg('plugins.broken', name='Org pack', reason=msg('plugins.why.org_pack_field', what='gate', name='{company}', allowed=ALLOWED))
    assert pack is None and app.state.runtime.org_pack() is None
    assert fake.gate_requests[0]['system'] == prompt.gate_system()
    assert prompt.system_prompt((), pack) == prompt.system_prompt() and prompt.slack_system_prompt((), pack) == prompt.slack_system_prompt()
    assert [n['level'] for n in notices if n['id'] == 'plugin:org'] == ['warn'] and not [n for n in notices if n['id'].startswith('org-pack:')]


@pytest.mark.parametrize('pack, code, values', [
    pytest.param(OrgPack(small_talk='Hi {0}'), 'org_pack_field', {'what': 'small talk', 'name': '{0}', 'allowed': ALLOWED}, id='positional-index'),
    pytest.param(OrgPack(gate='Hi {}'), 'org_pack_field', {'what': 'gate', 'name': '{}', 'allowed': ALLOWED}, id='positional-empty'),
    pytest.param(OrgPack(gate='Hi {org.upper}'), 'org_pack_field', {'what': 'gate', 'name': '{org.upper}', 'allowed': ALLOWED}, id='attribute'),
    pytest.param(OrgPack(gate='Hi {org:{company}}'), 'org_pack_format', {'what': 'gate', 'allowed': ALLOWED}, id='nested-unknown-field'),
    pytest.param(OrgPack(gate='Hi {product:{org}}'), 'org_pack_format', {'what': 'gate', 'allowed': ALLOWED}, id='nested-org-in-spec'),
    pytest.param(OrgPack(small_talk='Hi {org:{product}<5}'), 'org_pack_format', {'what': 'small talk', 'allowed': ALLOWED}, id='nested-product-in-spec'),
    pytest.param(OrgPack(gate='Hi {org'), 'org_pack_format', {'what': 'gate', 'allowed': ALLOWED}, id='unclosed-brace'),
    pytest.param(OrgPack(small_talk='Hi {org!z}'), 'org_pack_format', {'what': 'small talk', 'allowed': ALLOWED}, id='bad-conversion'),
    pytest.param(OrgPack(gate='Hi {org:d}'), 'org_pack_format', {'what': 'gate', 'allowed': ALLOWED}, id='bad-spec'),
    pytest.param(OrgPack(gate='Hi {org:>5000000}'), 'org_pack_format', {'what': 'gate', 'allowed': ALLOWED}, id='huge-width'),
    pytest.param(OrgPack(gate='Hi {product:>3}'), 'org_pack_format', {'what': 'gate', 'allowed': ALLOWED}, id='plain-spec'),
    pytest.param(OrgPack(small_talk='Hi {org!r}'), 'org_pack_format', {'what': 'small talk', 'allowed': ALLOWED}, id='plain-conversion'),
    pytest.param(OrgPack(gate='x' * 40_001), 'org_pack_long', {'what': 'gate', 'limit': 40_000}, id='gate-long'),
    pytest.param(OrgPack(small_talk='x' * 40_001), 'org_pack_long', {'what': 'small talk', 'limit': 40_000}, id='small-talk-long'),
    pytest.param(OrgPack(process='# Shop\n\n## Plain heading\n\nNo anchor.\n\n### Deep {#deep}\n'), 'org_pack_anchor', {}, id='no-anchor'),
    pytest.param(OrgPack(process='## Notes {#notes}\n\n' + 'x' * 200_000), 'org_pack_long', {'what': 'process', 'limit': 200_000}, id='process-long'),
    pytest.param(dataclasses.replace(PACK, slack_sections=('notes', 'slack-asks')), 'org_pack_section', {'id': 'slack-asks'}, id='section-not-in-pack'),
    pytest.param(OrgPack(slack_sections=('how-to-work', 'notes')), 'org_pack_section', {'id': 'notes'}, id='section-not-in-process-md'),
    pytest.param(OrgPack(process=None), 'org_pack', {}, id='process-not-text'),
    pytest.param(OrgPack(slack_sections='notes'), 'org_pack', {}, id='sections-not-a-list'),
    pytest.param(OrgPack(plugin_sections='no'), 'org_pack', {}, id='switch-not-bool'),
    pytest.param('a pack', 'org_pack', {}, id='not-a-pack'),
])
def test_each_org_pack_rule_breaks_its_plugin_with_a_reason(store: Store, pack, code, values):
    # when
    runtime = runtime_of(store, with_pack(pack), core_knowledge=prompt.core_anchors())
    found = runtime.registry.get('org')

    # then
    assert (found.state, found.reason) == ('broken', msg('plugins.broken', name='Org pack', reason=msg('plugins.why.' + code, **values)))
    assert runtime.org_pack() is None


def test_the_registry_reads_section_anchors_the_way_the_prompt_picks_them(store: Store):
    # given
    core = prompt.PROCESS.read_text(encoding='utf-8')
    kept = OrgPack(slack_sections=('how-to-work', 'slack-asks'), gate='{{"verdict": "ok"}} for {product} and {org}')

    # when
    runtime = runtime_of(store, with_pack(kept), core_knowledge=prompt.core_anchors())

    # then
    assert set(plugin_registry.section_anchors(core)) == prompt.core_anchors()
    assert set(plugin_registry.section_anchors(PACK.process)) == {'how-to-work', 'notes'}
    assert runtime.org_pack() == ('org', kept)
    assert prompt.slack_system_prompt((), kept) == prompt.slack_sections(['how-to-work', 'slack-asks', 'untrusted-input'])
    assert prompt.gate_system('', 'Shop team', kept) == '{"verdict": "ok"} for Skopax and Shop team'


def test_about_your_company_is_a_live_setting(store: Store):
    # given
    store.save({**state(), 'initialized': True}, None)
    fake = FakeAnthropic([], [verdict(), verdict()])
    app = create_app(keyed(), store, anthropic_client=fake)

    # when
    with TestClient(app) as c:
        app.state.gate.check('x')
        saved = c.put(SETTINGS, json={'key': 'core.org-about', 'value': 'Shop team'}, headers=AUTH)
        app.state.gate.check('x')
        long = c.put(SETTINGS, json={'key': 'core.org-about', 'value': 'x' * 301}, headers=AUTH)
        fits = tunables.parse(app.state.runtime.live.known()['core.org-about'], 'x' * 300, settings())
        items = c.get(SETTINGS, headers=AUTH).json()['items']
    entry = next(i for i in items if i['key'] == 'core.org-about')
    boxes = {i['key']: i.get('limit') for i in items if i['kind'] in ('text', 'list', 'tagrules')}

    # then
    assert 'Shop team' not in fake.gate_requests[0]['system'] and 'About what Skopax watches: Shop team.' in fake.gate_requests[1]['system']
    assert saved.status_code == 200 and (saved.json()['item']['live'], saved.json()['item']['value']) == (True, 'Shop team')
    assert (long.status_code, long.json()['detail']) == (422, 'About your company is longer than 300 characters')
    assert fits == 'x' * 300 and tunables.TEXT_LIMITS['core.org-about'] == prompt.ORG_LIMIT
    assert {k: entry[k] for k in ('group', 'label', 'kind', 'limit', 'value')} == {'group': 'Investigator', 'label': 'About your company', 'kind': 'text', 'limit': 300, 'value': 'Shop team'}
    assert entry['help'] == 'One or two sentences on what Skopax watches, used by the gate that screens Slack messages'
    assert (boxes['general.timezone'], boxes['jira.projects'], boxes['core.org-about']) == (1000, 1000, 300)
    assert {k: limit for k, limit in boxes.items() if limit not in (300, 1000)} == {'website.scan-prompt': SETTING_TEXT_MAX, 'sso.button-text': 40} and any(k.startswith('tags.') for k in boxes)
