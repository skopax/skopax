import dataclasses
import datetime as dt
import json

import pytest

from tests.conftest import anomaly, settings
from tests.fake_anthropic import FakeAnthropic, reply, text, tool
from tests.test_investigator import KEY, NOW, seed
from tests.test_investigator_tools import sources
from skopax import clock
from skopax.engine import iso
from skopax.investigator import STALE_AFTER_S, Investigator, prompt
from skopax.investigator.smalltalk import SmallTalk
from skopax.investigator.tools import WRITE_NAMES, Effects
from skopax.store import Store

PERMALINK = 'https://example.slack.com/archives/C1/p1792224000000200'
RECAP = '*checkout-service*: one client, 40 lines (esql, 14:05 UTC).'


def slack_action(store: Store, cards=(KEY,), where: str = 'card', **extra) -> dict:
    return store.add_action({'type': 'ask', 'source': 'slack', 'key': 'ask', 'target': 'slack:C1:1792224000.000200', 'text': 'Why did it go up?', 'cards': list(cards),
                             'label': 'Slack: Why did it go up?', 'requestedBy': 'adam.baker',
                             'slack': {'where': where, 'channel': 'C1', 'threadTs': '1792224000.000100', 'ts': '1792224000.000200', 'user': 'U1',
                                       'placeholderTs': '1792224000.000300', 'language': 'nl', 'permalink': PERMALINK},
                             'thread': [{'who': 'bot', 'text': 'checkout-service ERROR 40'}], **extra})


def slack_investigator(store: Store, replies: list, mode: str = 'live', talk: bool = False):
    seen = []

    def slack_reply(action, outcome, recap):
        seen.append((action, outcome, recap))
        return {'ts': action['slack']['placeholderTs'], 'text': recap or outcome}

    s = dataclasses.replace(settings(), anthropic_api_key='sk-test', investigate_mode=mode)
    inv = Investigator(store, sources(), Effects(lambda key, by: {}, lambda key, text, by, *_, notice=None: {}, slack_reply), s, client=FakeAnthropic(replies), clock=lambda: NOW,
                       talk=SmallTalk(store, s, clock=lambda: NOW) if talk else None)
    return inv, seen


def first_brief(inv) -> str:
    first = inv.client.requests[0]['messages'][0]['content']
    return first if isinstance(first, str) else first[0]['text']


def test_a_slack_ask_runs_read_only_with_the_slack_brief_and_replies_with_the_recap(store: Store):
    # given
    seed(store, anomaly(KEY), history=[])
    act = slack_action(store)
    inv, seen = slack_investigator(store, [reply(tool('note', {'key': KEY, 'text': 'x'})), reply(text(RECAP), stop='end_turn')])

    # when
    record = inv.run_once(NOW)

    # then
    offered = {t['name'] for t in inv.client.requests[0]['tools']}
    assert not offered & WRITE_NAMES and 'propose_ticket' in offered
    assert 'this run can only read' in json.dumps(inv.client.requests[1]['messages'][-1])
    assert record['trigger'] == 'slack' and record['ask'] == {'text': 'Why did it go up?', 'by': 'adam.baker', 'source': 'slack', 'thread': PERMALINK}
    assert first_brief(inv).startswith('Slack ask by operator adam.baker (<@U1>)') and first_brief(inv).endswith(prompt.SLACK_CLOSE.format(calls=10))
    assert [(a['id'], outcome, recap) for a, outcome, recap in seen] == [(act['id'], 'done', RECAP)]
    assert store.get_action(act['id'])['status'] == 'done'
    assert store.investigation(record['id'])['slackReply'] == {'ts': '1792224000.000300', 'text': RECAP}
    assert store.anomaly(KEY).get('note') is None


def test_a_slack_ask_brief_says_how_much_of_each_limit_is_used(store: Store):
    # given
    seed(store, anomaly(KEY), history=[])
    slack_action(store)
    slack_action(store)
    store.add_action({'type': 'ask', 'source': 'page', 'key': 'ask', 'target': 'ask', 'text': 'page ask', 'cards': [], 'requestedBy': 'op'})
    recent, old = dt.datetime.fromtimestamp(NOW - 600, dt.timezone.utc), dt.datetime.fromtimestamp(NOW - 7200, dt.timezone.utc)
    for i, at in enumerate([recent] * 12 + [old]):
        store.seen('C9', str(i), at)
        store.record_intake('C9', str(i), {'gate': {'onTopic': True}})
    store.add_spend(clock.day(NOW), 4.1)
    inv, _ = slack_investigator(store, [reply(text(RECAP), stop='end_turn')])

    # when
    inv.run_once(NOW)

    # then
    assert '\n\nSkopax limits now: Slack asks 2 of 10 this hour, gate checks 12 of 60 this hour, spend today $4.10 of $25.\n\nQuestion:\n' in first_brief(inv)


@pytest.mark.parametrize('enabled, part', [(True, 'small talk 3 of 4 this hour'), (False, 'small talk off')])
def test_a_slack_ask_brief_says_how_much_small_talk_is_left_or_that_it_is_off(store: Store, enabled, part):
    # given
    seed(store, anomaly(KEY), history=[])
    slack_action(store)
    store.set_small_talk_rules({'enabled': enabled, 'rules': 'Answer briefly.', 'hourlyCap': 4})
    recent, old = dt.datetime.fromtimestamp(NOW - 600, dt.timezone.utc), dt.datetime.fromtimestamp(NOW - 7200, dt.timezone.utc)
    for i, at in enumerate([recent] * 3 + [old]):
        store.seen('C8', f't{i}', at)
        store.record_intake('C8', f't{i}', {'decision': 'small-talk'})
    inv, _ = slack_investigator(store, [reply(text(RECAP), stop='end_turn')], talk=True)

    # when
    inv.run_once(NOW)

    # then
    assert f'Skopax limits now: Slack asks 1 of 10 this hour, gate checks 0 of 60 this hour, {part}, spend today $0.00 of $25.' in first_brief(inv)


def test_a_shadow_slack_ask_replies_with_the_plain_recap(store: Store):
    # given
    seed(store, anomaly(KEY), history=[])
    act = slack_action(store)
    inv, seen = slack_investigator(store, [reply(text(RECAP), stop='end_turn')], mode='shadow')

    # when
    inv.run_once(NOW)

    # then
    assert store.get_action(act['id'])['result'].startswith('shadow run, nothing written:')
    assert seen[0][2] == RECAP


def test_a_ticket_draft_lands_on_the_run_the_action_and_the_reply(store: Store):
    # given
    seed(store, anomaly(KEY), history=[])
    act = slack_action(store)
    draft = {'title': 'checkout-service null guard', 'description': 'NPE at line 69', 'points': 3}
    inv, seen = slack_investigator(store, [reply(tool('propose_ticket', draft)), reply(tool('propose_ticket', draft, 't2')), reply(text('Draft ready.'), stop='end_turn')])

    # when
    record = inv.run_once(NOW)

    # then
    expected = {'key': KEY, **draft, 'project': None}
    assert record['ticketProposal'] == expected
    assert store.get_action(act['id'])['ticketProposal'] == expected
    assert seen[0][0]['ticketProposal'] == expected
    assert 'one ticket draft per run' in json.dumps(inv.client.requests[2]['messages'][-1])


def test_a_card_action_lands_on_the_run_the_action_and_the_reply(store: Store):
    # given
    seed(store, anomaly(KEY), history=[])
    act = slack_action(store, cards=())
    call = {'key': KEY, 'action': 'resolve'}
    inv, seen = slack_investigator(store, [reply(tool('propose_card_action', call)), reply(tool('propose_card_action', call, 't2')), reply(text('The Resolve button is below.'), stop='end_turn')])

    # when
    record = inv.run_once(NOW)

    # then
    assert 'propose_card_action' in {t['name'] for t in inv.client.requests[0]['tools']}
    assert record['cardAction'] == call
    assert store.get_action(act['id'])['cardAction'] == call
    assert seen[0][0]['cardAction'] == call
    assert 'one card action per run' in json.dumps(inv.client.requests[2]['messages'][-1])
    assert store.anomaly(KEY).get('triage') != 'handled'


def test_a_resolve_proposal_keeps_its_kind_and_ref_on_the_run_the_action_and_the_button(store: Store):
    # given
    seed(store, anomaly(KEY), history=[])
    store.card_ids.insert_many([{'_id': KEY, 'n': 11}, {'_id': 'log|other|ERROR|x', 'n': 12}])
    act = slack_action(store, cards=())
    inv, seen = slack_investigator(store, [reply(tool('propose_card_action', {'key': KEY, 'action': 'resolve', 'kind': 'duplicate', 'ref': '12'})), reply(text('Duplicate, the button is below.'), stop='end_turn')])

    # when
    record = inv.run_once(NOW)

    # then
    call = {'key': KEY, 'action': 'resolve', 'kind': 'duplicate', 'ref': '#12'}
    assert record['cardAction'] == call and store.get_action(act['id'])['cardAction'] == call and seen[0][0]['cardAction'] == call
    assert store.anomaly(KEY).get('triage') != 'handled', 'a proposal writes nothing'


@pytest.mark.parametrize('proposed, expected', [
    (None, {'key': KEY, 'action': 'take', 'ticket': 'INFRA-402'}),
    ({'key': KEY, 'action': 'take'}, {'key': KEY, 'action': 'take', 'ticket': 'INFRA-402'}),
    ({'key': KEY, 'action': 'take', 'ticket': 'OPS-142'}, {'key': KEY, 'action': 'take', 'ticket': 'OPS-142'}),
    ({'key': KEY, 'action': 'resolve'}, {'key': KEY, 'action': 'resolve'}),
])
def test_a_ticket_named_in_a_card_thread_ask_goes_on_the_take_button(store: Store, proposed, expected):
    # given
    seed(store, anomaly(KEY), history=[])
    act = slack_action(store, mentionedTicket='INFRA-402')
    replies = ([reply(tool('propose_card_action', proposed))] if proposed else []) + [reply(text('The button is below.'), stop='end_turn')]
    inv, seen = slack_investigator(store, replies)

    # when
    record = inv.run_once(NOW)

    # then
    assert record['cardAction'] == expected
    assert store.get_action(act['id'])['cardAction'] == expected and seen[0][0]['cardAction'] == expected


@pytest.mark.parametrize('setup', ['linked', 'handled', 'muted'])
def test_no_take_button_from_the_ticket_when_the_card_has_it_is_handled_or_muted(store: Store, setup):
    # given
    card = anomaly(KEY, ticket='INFRA-402', triage='progress') if setup == 'linked' else anomaly(KEY, triage='handled') if setup == 'handled' else anomaly(KEY, muted={'kind': 'mute'})
    seed(store, card, history=[])
    slack_action(store, mentionedTicket='INFRA-402')
    inv, _ = slack_investigator(store, [reply(text('Ok.'), stop='end_turn')])

    # when
    record = inv.run_once(NOW)

    # then
    assert 'cardAction' not in record


@pytest.mark.parametrize('call, error', [
    ({'key': KEY, 'action': 'resolve', 'ticket': 'INFRA-402'}, 'ticket goes only with take'),
    ({'key': KEY, 'action': 'take', 'ticket': 'see the jira'}, 'ticket must be a Jira key such as OPS-123'),
])
def test_propose_card_action_refuses_a_ticket_on_another_action_or_a_free_text_ticket(store: Store, call, error):
    # given
    seed(store, anomaly(KEY), history=[])
    slack_action(store, cards=())
    inv, _ = slack_investigator(store, [reply(tool('propose_card_action', call)), reply(text('Did not work.'), stop='end_turn')])

    # when
    record = inv.run_once(NOW)

    # then
    assert error in json.dumps(inv.client.requests[1]['messages'][-1]) and 'cardAction' not in record


def test_a_crash_before_the_run_record_still_replaces_the_placeholder(store: Store, monkeypatch):
    # given
    seed(store, anomaly(KEY), history=[])
    act = slack_action(store)
    inv, seen = slack_investigator(store, [])
    monkeypatch.setattr(store, 'add_investigation', lambda doc: (_ for _ in ()).throw(RuntimeError('mongo down')))

    # when
    with pytest.raises(RuntimeError):
        inv.run_once(NOW)

    # then
    assert [(a['id'], outcome) for a, outcome, _ in seen] == [(act['id'], 'failed')]
    assert store.get_action(act['id'])['status'] == 'failed'


def test_a_slack_ask_whose_card_is_gone_fails_with_a_reply(store: Store):
    # given
    seed(store, anomaly(KEY), history=[])
    act = slack_action(store, cards=('gone',))
    inv, seen = slack_investigator(store, [])

    # when
    inv.run_once(NOW)

    # then
    assert store.get_action(act['id'])['status'] == 'failed'
    assert [(a['id'], outcome, recap) for a, outcome, recap in seen] == [(act['id'], 'failed', '')]


def test_a_reaped_slack_ask_gets_its_placeholder_replaced_and_counts_in_the_reap(store: Store):
    # given
    act = slack_action(store)
    store.actions.update_one({'_id': act['id']}, {'$set': {'requestedAt': iso(NOW - STALE_AFTER_S - 60)}})
    inv, seen = slack_investigator(store, [])

    # when
    counts = inv.reap(NOW)

    # then
    assert counts == (0, 0, 1)
    assert [(a['id'], outcome) for a, outcome, _ in seen] == [(act['id'], 'failed')]


def test_a_slack_ask_reaped_mid_run_is_not_answered_twice(store: Store, monkeypatch):
    # given
    seed(store, anomaly(KEY), history=[])
    slack_action(store)
    inv, seen = slack_investigator(store, [reply(text(RECAP), stop='end_turn')])
    original = store.update_action

    def reaped_first(action_id, status, result, expect, report=None):
        if expect == 'processing' and status in ('done', 'failed'):
            store.actions.update_one({'_id': action_id}, {'$set': {'status': 'failed'}})
        return original(action_id, status, result, expect, report)

    monkeypatch.setattr(store, 'update_action', reaped_first)

    # when
    record = inv.run_once(NOW)

    # then
    assert record['outcome'] == 'done'
    assert seen == []


def test_a_slack_ask_runs_on_the_slack_model_effort_cache_and_slim_prompt(store: Store):
    # given
    seed(store, anomaly(KEY), history=[])
    slack_action(store)
    inv, _ = slack_investigator(store, [reply(text(RECAP), stop='end_turn')])

    # when
    record = inv.run_once(NOW)

    # then
    request = inv.client.requests[0]
    system = request['system'][0]
    assert request['model'] == 'claude-sonnet-5-5' and request['output_config'] == {'effort': 'medium'}
    assert system['cache_control'] == {'type': 'ephemeral', 'ttl': '1h'}
    assert '## Slack asks' in system['text'] and '## Lock stalls' not in system['text']
    assert (record['model'], record['effort']) == ('claude-sonnet-5-5', 'medium')


def test_a_slack_ask_stops_at_the_slack_tool_call_cap(store: Store):
    # given
    seed(store, anomaly(KEY), history=[])
    slack_action(store)
    calls = [reply(tool('open_cards', {}, f't{i}')) for i in range(11)]
    inv, _ = slack_investigator(store, calls + [reply(text(RECAP), stop='end_turn')])

    # when
    record = inv.run_once(NOW)

    # then
    assert len(record['toolCalls']) == 10
    assert 'Tool-call limit reached' in json.dumps(inv.client.requests[11]['messages'][-1])


def test_a_page_ask_never_calls_the_slack_reply(store: Store):
    # given
    seed(store, anomaly(KEY), history=[])
    store.add_action({'type': 'ask', 'source': 'page', 'key': 'ask', 'target': 'ask', 'text': 'Why?', 'cards': [], 'requestedBy': 'oscar.green'})
    inv, seen = slack_investigator(store, [reply(text('Because.'), stop='end_turn')])

    # when
    record = inv.run_once(NOW)

    # then
    assert record['trigger'] == 'ask' and 'source' not in record['ask']
    request = inv.client.requests[0]
    assert (request['model'], request['output_config']) == (inv.model, {'effort': inv.effort})
    assert request['system'][0]['cache_control'] == {'type': 'ephemeral'}
    assert request['system'][0]['text'] == prompt.system_prompt(inv.sources.plugin_tools.knowledge())
    assert seen == []


def test_why_halted_names_the_stop_switch_and_the_cap(store: Store):
    # given
    inv, _ = slack_investigator(store, [])

    # when
    free = inv.why_halted(NOW)
    store.set_investigator_stop(True, 'oscar.green')
    stopped = inv.why_halted(NOW)

    # then
    assert (free, stopped) == (None, 'stopped')


def later_ask(store: Store, ts: str = '1792224000.000400', thread_ts: str = '1792224000.000100') -> dict:
    return slack_action(store, slack={'where': 'card', 'channel': 'C1', 'threadTs': thread_ts, 'ts': ts, 'user': 'U1', 'placeholderTs': '1792224000.000500', 'language': 'nl',
                                      'permalink': PERMALINK}, text='And when did it start?')


def test_a_queued_slack_ask_waits_while_an_earlier_ask_in_its_thread_is_open(store: Store):
    # given
    seed(store, anomaly(KEY), history=[])
    first = slack_action(store)
    store.update_action(first['id'], 'processing', None, 'requested')
    queued = later_ask(store)
    elsewhere = later_ask(store, ts='1792224000.000700', thread_ts='1792224000.000600')
    inv, _ = slack_investigator(store, [])

    # when
    picked = inv.pick_action()

    # then
    assert picked[1]['id'] == elsewhere['id']
    assert store.get_action(queued['id'])['status'] == 'requested'


def test_a_queued_slack_ask_runs_after_the_earlier_one_with_its_answer_in_the_thread(store: Store):
    # given
    seed(store, anomaly(KEY), history=[])
    first = slack_action(store)
    queued = later_ask(store)
    store.actions.update_one({'_id': first['id']}, {'$set': {'requestedAt': iso(NOW - 120)}})
    store.actions.update_one({'_id': queued['id']}, {'$set': {'requestedAt': iso(NOW - 60)}})
    store.update_action(first['id'], 'processing', None, 'requested')
    store.update_action(first['id'], 'done', RECAP, 'processing')
    store.set_action_fields(first['id'], {'slackReply': {'ts': '1792224000.000300', 'text': '<@U1> ' + RECAP, 'at': iso(NOW - 30)}})
    inv, seen = slack_investigator(store, [reply(text('Yesterday 14:00 UTC.'), stop='end_turn')])

    # when
    inv.run_once(NOW)

    # then
    assert [(a['id'], outcome) for a, outcome, _ in seen] == [(queued['id'], 'done')]
    assert f'> bot: ‹@U1› {RECAP}' in first_brief(inv)
    assert first_brief(inv).index('checkout-service ERROR 40') < first_brief(inv).index(RECAP)
