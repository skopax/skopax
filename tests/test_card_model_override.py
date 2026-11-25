import dataclasses

from fastapi.testclient import TestClient

from tests.conftest import AUTH, anomaly, settings
from tests.fake_anthropic import FakeAnthropic, reply, text
from tests.test_ai_provider import on_openai
from tests.test_chat import chat_action
from tests.test_investigator import KEY, NOW, assess_action, note_and_recap, seed, seeded_report
from tests.test_investigator_slack import slack_action
from tests.test_investigator_tools import sources
from skopax import clock
from skopax.app import create_app
from skopax.config import load_settings
from skopax.investigator import Investigator
from skopax.investigator.tools import Effects
from skopax.messages import msg
from skopax.store import Store

OTHER = 'log|b|ERROR|y'
CARD = 'claude-opus-5-5'
CRITICAL = 'claude-opus-5'
HIGH = 'claude-sonnet-5-5'


def investigator(store: Store, replies: list, **over) -> Investigator:
    s = dataclasses.replace(settings(), anthropic_api_key='sk-test', investigate_mode='live', investigate_model=CARD, **over)
    return Investigator(store, sources(), Effects(lambda key, by: {}, lambda key, text, by, *_, notice=None: {}, lambda *a: None), s, client=FakeAnthropic(replies), clock=lambda: NOW)


def overrides(**over) -> dict:
    return {'investigate_model_critical': CRITICAL, 'investigate_model_high': HIGH} | over


def asked(inv: Investigator) -> list[str]:
    return [r['model'] for r in inv.client.requests]


def dive(store: Store, key: str = KEY) -> dict:
    return store.add_action({'type': 'dive', 'key': key, 'target': key, 'label': 'Investigate', 'requestedBy': 'alice@example.com'})


def setting(c: TestClient, key: str) -> dict:
    return next(i for i in c.get('/api/admin/settings', headers=AUTH).json()['items'] if i['key'] == key)


def test_a_critical_card_runs_on_the_critical_model(store: Store):
    # given
    seed(store, anomaly(KEY, sev='crit', firstSeen='2026-10-22T05:00:00Z'))
    inv = investigator(store, note_and_recap(), **overrides())

    # when
    record = inv.run_once(NOW)

    # then
    assert record['outcome'] == 'done' and record['model'] == CRITICAL
    assert set(asked(inv)) == {CRITICAL}


def test_a_high_card_runs_on_the_high_model_and_a_warning_card_on_the_card_model(store: Store):
    # given
    seed(store, anomaly(KEY, sev='high', firstSeen='2026-10-22T05:00:00Z'), anomaly(OTHER, sev='warn', firstSeen='2026-10-22T05:00:00Z'))
    dive(store, KEY)
    dive(store, OTHER)
    inv = investigator(store, note_and_recap() + note_and_recap(), **overrides())

    # when
    high = inv.run_once(NOW)
    warn = inv.run_once(NOW)

    # then
    assert (high['keys'], high['model']) == ([KEY], HIGH)
    assert (warn['keys'], warn['model']) == ([OTHER], CARD)
    assert asked(inv) == [HIGH, HIGH, CARD, CARD]


def test_an_empty_override_keeps_the_card_model(store: Store):
    # given
    seed(store, anomaly(KEY, sev='crit', firstSeen='2026-10-22T05:00:00Z'))
    inv = investigator(store, note_and_recap())

    # when
    record = inv.run_once(NOW)

    # then
    assert (settings().investigate_model_critical, settings().investigate_model_high) == ('', '')
    assert record['model'] == CARD and set(asked(inv)) == {CARD}
    assert 'modelCritical' not in inv.info() and 'modelOverrides' not in inv.board(NOW)


def test_a_grouped_run_uses_the_highest_severity(store: Store):
    # given
    seed(store, anomaly(KEY, sev='warn', firstSeen='2026-10-22T05:00:00Z'), anomaly(OTHER, sev='crit', firstSeen='2026-10-22T05:00:00Z'))
    inv = investigator(store, note_and_recap(), **overrides())

    # when
    record = inv.run_once(NOW)

    # then
    assert sorted(record['keys']) == sorted([KEY, OTHER])
    assert record['model'] == CRITICAL and set(asked(inv)) == {CRITICAL}


def test_an_escalated_card_uses_the_new_severity_on_its_next_run(store: Store):
    # given
    seed(store, anomaly(KEY, sev='warn', firstSeen='2026-10-22T05:00:00Z'))
    inv = investigator(store, note_and_recap() + note_and_recap(), **overrides())
    first = inv.run_once(NOW)

    # when
    seed(store, anomaly(KEY, sev='crit', firstSeen='2026-10-22T05:00:00Z'), history=[])
    dive(store, KEY)
    second = inv.run_once(NOW)

    # then
    assert (first['model'], second['model']) == (CARD, CRITICAL)
    assert asked(inv) == [CARD, CARD, CRITICAL, CRITICAL]


def test_the_run_records_the_override_model_for_spend(store: Store):
    # given
    seed(store, anomaly(KEY, sev='crit', firstSeen='2026-10-22T05:00:00Z'))
    inv = investigator(store, note_and_recap(), **overrides())

    # when
    record = inv.run_once(NOW)
    run = store.list_investigations(KEY, 1)[0]
    rows = [r for r in store.ledger_since(clock.day(NOW)) if r.get('runId') == record['id']]

    # then
    assert (run['model'], set(run['models']), run['effort']) == (CRITICAL, {CRITICAL}, settings().investigate_effort)
    assert rows and {r['model'] for r in rows} == {CRITICAL}
    assert set(store.investigation_usage('2000-01-01T00:00:00Z')['models']) == {CRITICAL}


def test_an_override_of_the_other_provider_falls_back_and_admin_says_so(store: Store):
    # given
    s = on_openai(investigate_mode='live', investigate_model='gpt-6.2-sol', investigate_model_critical='claude-opus-5', investigate_model_high='gpt-6.1-terra')
    inv = Investigator(store, sources(), Effects(lambda key, by: {}, lambda key, text, by, *_, notice=None: {}), s)
    c = TestClient(create_app(s, store))

    # when
    critical, high = setting(c, 'investigate.model-critical'), setting(c, 'investigate.model-high')
    refused = c.put('/api/admin/settings', json={'key': 'investigate.model-high', 'value': 'claude-opus-5'}, headers=AUTH)

    # then
    assert (inv.model, inv.card_model({'crit'}), inv.card_model({'high'}), inv.card_model({'warn'})) == ('gpt-6.2-sol', 'gpt-6.2-sol', 'gpt-6.1-terra', 'gpt-6.2-sol')
    assert inv.enabled and inv.board(NOW)['problem'] is None
    assert critical['problem'] == 'claude-opus-5 is a model of Anthropic; with OpenAI as the AI provider, gpt-6.2-sol is used until you pick one of its models'
    assert critical['choiceLabels']['claude-opus-5'] == 'claude-opus-5 (Anthropic, not used)' and critical['choices'][0] == ''
    assert 'problem' not in high and high['value'] == 'gpt-6.1-terra' and 'claude-opus-5' not in high['choices']
    assert (refused.status_code, refused.json()['detail']) == (422, msg('ai.model_refused', model='claude-opus-5', owner='Anthropic', provider='OpenAI'))
    assert setting(c, 'investigate.model-high')['value'] == 'gpt-6.1-terra'
    assert inv.info()['modelCritical'] == 'gpt-6.2-sol' and inv.info()['modelHigh'] == 'gpt-6.1-terra'


def test_an_unknown_override_falls_back_to_the_card_model_and_admin_says_so(store: Store):
    # given
    s = dataclasses.replace(settings(), investigate_model_critical='claude-x')
    inv = Investigator(store, sources(), Effects(lambda key, by: {}, lambda key, text, by, *_, notice=None: {}), s)
    c = TestClient(create_app(s, store))

    # when
    critical = setting(c, 'investigate.model-critical')

    # then
    assert inv.card_model({'crit'}) == CARD and inv.board(NOW)['problem'] != 'model'
    assert critical['problem'] == 'claude-x is not a known model, so Critical cards use the Card model claude-opus-5-5 until you pick another'


def test_slack_answers_and_chat_ignore_the_override(store: Store):
    # given
    seed(store, anomaly(KEY, sev='crit', num=14), history=[])
    slack_action(store)
    chat_action(store)
    replies = [reply(text('one client'), stop='end_turn'), reply(text('one client'), stop='end_turn')]
    inv = investigator(store, replies, **overrides())

    # when
    first = inv.run_once(NOW)
    second = inv.run_once(NOW)

    # then
    assert {(first['trigger'], first['model']), (second['trigger'], second['model'])} == {('slack', inv.slack_model), ('chat', inv.chat_model)}
    assert CRITICAL not in asked(inv)


def test_page_asks_on_a_critical_card_and_deploy_assessments_keep_the_card_model(store: Store):
    # given
    seed(store, anomaly(KEY, sev='crit', num=14), history=[])
    store.add_action({'type': 'ask', 'source': 'page', 'key': 'ask', 'target': 'ask', 'text': 'Why did it go up?', 'cards': [KEY], 'requestedBy': 'alice@example.com'})
    assess_action(store, seeded_report(store)['id'])
    replies = [reply(text('one client'), stop='end_turn'), reply(text('nothing to add'), stop='end_turn')]
    inv = investigator(store, replies, **overrides())

    # when
    first = inv.run_once(NOW)
    second = inv.run_once(NOW)

    # then
    assert {(first['trigger'], first['model']), (second['trigger'], second['model'])} == {('ask', CARD), ('assess', CARD)}
    assert set(asked(inv)) == {CARD}


def test_a_known_fallback_model_as_override_falls_back_to_the_card_model_and_admin_says_so(store: Store):
    # given
    s = dataclasses.replace(settings(), investigate_model_high='claude-sonnet-5')
    inv = Investigator(store, sources(), Effects(lambda key, by: {}, lambda key, text, by, *_, notice=None: {}), s)
    c = TestClient(create_app(s, store))

    # when
    high = setting(c, 'investigate.model-high')

    # then
    assert inv.card_model({'high'}) == CARD
    assert high['problem'] == 'claude-sonnet-5 is not a known model, so High cards use the Card model claude-opus-5-5 until you pick another' and 'claude-sonnet-5' in high['choices']


def test_admin_lists_the_two_overrides_next_to_the_card_model_with_their_choices(store: Store):
    # given
    c = TestClient(create_app(settings(), store))

    # when
    items = {i['key']: i for i in c.get('/api/admin/settings', headers=AUTH).json()['items']}
    keys = list(items)

    # then
    models = ['claude-opus-5-5', 'claude-opus-5', 'claude-opus-4-8', 'claude-sonnet-5-5', 'claude-haiku-5-5', 'claude-fable-5-2', 'claude-mythos-5-2']
    for key, label, sev in (('investigate.model-critical', 'Model for Critical cards', 'Critical'), ('investigate.model-high', 'Model for High cards', 'High')):
        item = items[key]
        assert (item['label'], item['help'], item['kind'], item['card'], item['group']) == (label, f'Card model for {sev} cards; empty uses the Card model', 'choice', 'Models', 'Investigator')
        assert (item['value'], item['source'], item['choices']) == ('', 'default', [''] + models)
        assert item['choiceLabels'] == {'': 'Same as Card model'}
    assert keys.index('investigate.model') < keys.index('investigate.model-critical') < keys.index('investigate.model-high')
    assert items['investigate.model-critical']['order'] > items['investigate.model']['order']


def test_the_overrides_read_their_environment_names():
    # given
    env = {'INVESTIGATE_MODEL_CRITICAL': ' claude-opus-5 ', 'INVESTIGATE_MODEL_HIGH': 'claude-sonnet-5-5'}

    # when
    s = load_settings(env=env)

    # then
    assert (s.investigate_model_critical, s.investigate_model_high) == ('claude-opus-5', 'claude-sonnet-5-5')
