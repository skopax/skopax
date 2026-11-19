import pytest

from tests.conftest import settings
from tests.fake_slack import fake_slack
from tests.test_slackevents import CARD_TS, KEY, NOW, card_with_post
from skopax import actions
from skopax.messages import msg
from skopax.operators import Operators
from skopax.slack import CARD_ACTION, CREATE_TASK, POST_ACTIONS, anomaly_message, answer_blocks, take_offer_blocks
from skopax.slackevents import Clicks, TicketRefused
from skopax.store import Store

ANSWER_TS = '1792224000.000300'
PROPOSAL = {'key': KEY, 'title': 'checkout-service null guard', 'description': 'NPE at line 69', 'points': 3}


class Applier:
    def __init__(self, refuse: str | None = None):
        self.refuse = refuse
        self.calls: list[dict] = []

    def __call__(self, ticket: dict) -> str:
        self.calls.append(ticket)
        if self.refuse:
            raise TicketRefused(self.refuse)
        if ticket['type'] != 'ticket':
            return 'No action needed'
        return 'OPS-140 created · In progress · adam.baker · notified: thread'


def clicks(store: Store, refuse: str | None = None, emails=None):
    slack, post, api = fake_slack(emails={'U1': 'adam.baker@example.com'} if emails is None else emails)
    applier = Applier(refuse)
    return Clicks(store, slack, Operators(lambda email: email in {'adam.baker@example.com'}, slack, settings(), lambda: NOW), applier, jira_site='https://jira.example.com'), applier, api


def drafted_ask(store: Store, proposal: dict | None = PROPOSAL) -> dict:
    act = store.add_action({'type': 'ask', 'source': 'slack', 'key': 'ask', 'target': 'slack:C1:x', 'text': 'open a task', 'cards': [KEY],
                            'slack': {'where': 'card', 'channel': 'C1', 'threadTs': CARD_TS, 'ts': '1792224000.000200', 'user': 'U1'}}, 'done', 'answered')
    if proposal:
        store.set_action_fields(act['id'], {'ticketProposal': proposal})
    return act


def payload(action_id: str, user: str = 'U1') -> dict:
    return {'type': 'block_actions', 'user': {'id': user}, 'channel': {'id': 'C1'},
            'message': {'ts': ANSWER_TS, 'thread_ts': CARD_TS, 'text': 'Draft ready.', 'blocks': answer_blocks('Draft ready.', PROPOSAL, action_id)},
            'actions': [{'action_id': CREATE_TASK, 'value': action_id}]}


def ephemerals(api) -> list[str]:
    return [p['text'] for m, p in api.calls if m == 'chat.postEphemeral']


def test_an_operator_click_opens_the_task_once_and_swaps_the_button(store: Store):
    # given
    card_with_post(store)
    act = drafted_ask(store)
    box, applier, api = clicks(store)

    # when
    first = box.handle(payload(act['id']))
    second = box.handle(payload(act['id']))

    # then
    assert (first, second) == ('created', 'claimed')
    assert applier.calls == [{'type': 'ticket', 'key': KEY, 'target': KEY, 'title': 'checkout-service null guard', 'description': 'NPE at line 69', 'points': 3, 'project': None,
                             'label': 'Create task from Slack: checkout-service null guard', 'requestedBy': 'adam.baker@example.com', 'source': 'slack', 'askId': act['id']}]
    update = next(p for m, p in api.calls if m == 'chat.update')
    assert update['ts'] == ANSWER_TS and all(b['type'] != 'actions' for b in update['blocks'])
    assert update['blocks'][-1] == {'type': 'context', 'elements': [{'type': 'mrkdwn', 'text': '<https://jira.example.com/browse/OPS-140|OPS-140> opened · adam.baker@example.com'}]}
    assert store.get_action(act['id'])['ticket']['key'] == 'OPS-140'
    assert ephemerals(api) == ['Already opened: OPS-140']
    tickets = [a for a in store.list_actions('done', 10) if a['type'] == 'ticket']
    assert len(tickets) == 1 and tickets[0]['result'].startswith('OPS-140 created')


def test_a_click_while_another_one_is_opening_says_so(store: Store):
    # given
    card_with_post(store)
    act = drafted_ask(store)
    store.claim_ticket(act['id'], 'nina')
    box, applier, api = clicks(store)

    # when
    got = box.handle(payload(act['id']))

    # then
    assert got == 'claimed' and applier.calls == [] and ephemerals(api) == [msg('slackevents.click.busy')]


def test_a_non_operator_click_gets_the_ephemeral_line(store: Store):
    # given
    card_with_post(store)
    act = drafted_ask(store)
    box, applier, api = clicks(store, emails={'U9': 'x@example.com'})

    # when
    got = box.handle(payload(act['id'], user='U9'))

    # then
    assert got == 'not-operator' and applier.calls == [] and ephemerals(api) == [msg('slackevents.click.non_operator')]


def test_a_jira_refusal_releases_the_claim_and_keeps_the_button(store: Store):
    # given
    card_with_post(store)
    act = drafted_ask(store)
    box, applier, api = clicks(store, refuse='Jira is not set up on this service')

    # when
    got = box.handle(payload(act['id']))

    # then
    assert got == 'refused'
    assert 'ticket' not in store.get_action(act['id'])
    assert ephemerals(api) == ['Could not open the task: Jira is not set up on this service']
    assert 'chat.update' not in api.methods()
    assert [a['result'] for a in store.list_actions('failed', 5) if a['type'] == 'ticket'] == ['Jira is not set up on this service']


def test_a_click_on_a_gone_draft_or_card_says_so(store: Store):
    # given
    card_with_post(store)
    no_draft = drafted_ask(store, proposal=None)
    gone_card = drafted_ask(store, proposal={**PROPOSAL, 'key': 'log|gone|ERROR|y'})
    box, applier, api = clicks(store)

    # when
    got = [box.handle(payload(i)) for i in ('nope', no_draft['id'], gone_card['id'])]

    # then
    assert got == ['gone'] * 3 and applier.calls == [] and ephemerals(api) == [msg('slackevents.click.gone')] * 3


def test_other_interactive_payloads_are_ignored(store: Store):
    # given
    box, applier, api = clicks(store)

    # then
    assert box.handle({'type': 'view_submission'}) is None
    assert box.handle({'type': 'block_actions', 'actions': [{'action_id': 'other', 'value': 'x'}]}) is None
    assert api.calls == []


class Failing:
    def __init__(self, error: Exception):
        self.error = error
        self.calls: list[dict] = []

    def __call__(self, ticket: dict) -> str:
        self.calls.append(ticket)
        raise self.error


class Plain:
    def __init__(self, result: str):
        self.result = result
        self.calls: list[dict] = []

    def __call__(self, ticket: dict) -> str:
        self.calls.append(ticket)
        return self.result


def with_applier(store: Store, applier):
    slack, post, api = fake_slack(emails={'U1': 'adam.baker@example.com'})
    return Clicks(store, slack, Operators(lambda email: email in {'adam.baker@example.com'}, slack, settings(), lambda: NOW), applier, jira_site='https://jira.example.com'), api


def test_an_unexpected_create_error_releases_the_claim_and_says_so(store: Store):
    # given
    card_with_post(store)
    act = drafted_ask(store)
    applier = Failing(RuntimeError('boom'))
    box, api = with_applier(store, applier)

    # when
    got = box.handle(payload(act['id']))

    # then
    assert got == 'refused' and len(applier.calls) == 1
    assert 'ticket' not in store.get_action(act['id'])
    assert ephemerals(api) == ['Could not open the task: RuntimeError']
    assert [a['result'] for a in store.list_actions('failed', 5) if a['type'] == 'ticket'] == ['RuntimeError']


def test_a_result_with_another_project_key_still_swaps_the_button(store: Store):
    # given
    card_with_post(store)
    act = drafted_ask(store)
    box, api = with_applier(store, Plain('ABC-12 created · In progress · adam.baker'))

    # when
    got = box.handle(payload(act['id']))

    # then
    assert got == 'created'
    assert store.get_action(act['id'])['ticket']['key'] == 'ABC-12'
    update = next(p for m, p in api.calls if m == 'chat.update')
    assert update['blocks'][-1]['elements'][0]['text'] == '<https://jira.example.com/browse/ABC-12|ABC-12> opened · adam.baker@example.com'


def test_a_click_during_a_lookup_outage_is_silent_and_claims_nothing(store: Store):
    # given
    card_with_post(store)
    act = drafted_ask(store)
    box, applier, api = clicks(store)
    api.fail.add('users.info')

    # when
    result = box.handle(payload(act['id']))

    # then
    assert result == 'operator-unknown'
    assert ephemerals(api) == [] and applier.calls == []
    assert 'ticket' not in store.get_action(act['id'])


CALL = {'key': KEY, 'action': 'resolve'}


def action_ask(store: Store, call: dict | None = CALL) -> dict:
    act = drafted_ask(store, proposal=None)
    if call:
        store.set_action_fields(act['id'], {'cardAction': call})
    return act


def action_payload(action_id: str, user: str = 'U1', call: dict = CALL, with_draft: bool = False) -> dict:
    blocks = answer_blocks('Ready.', PROPOSAL if with_draft else None, action_id, call, {'svc': 'checkout-service', 'label': 'ERROR pattern'})
    return {'type': 'block_actions', 'user': {'id': user}, 'channel': {'id': 'C1'}, 'message': {'ts': ANSWER_TS, 'thread_ts': CARD_TS, 'text': 'Ready.', 'blocks': blocks},
            'actions': [{'action_id': CARD_ACTION, 'value': action_id}]}


def test_an_operator_click_applies_the_card_action_once_and_swaps_the_button(store: Store):
    # given
    card_with_post(store)
    act = action_ask(store, {'key': KEY, 'action': 'mute', 'hours': 6})
    box, applier, api = clicks(store)

    # when
    first = box.handle(action_payload(act['id'], call={'key': KEY, 'action': 'mute', 'hours': 6}))
    second = box.handle(action_payload(act['id']))

    # then
    assert (first, second) == ('applied', 'claimed')
    assert applier.calls == [{'type': 'mute', 'key': KEY, 'target': KEY, 'hours': 6, 'label': f'Mute 6 h from Slack: {KEY}', 'requestedBy': 'adam.baker@example.com', 'source': 'slack', 'askId': act['id'], 'reason': 'Mute 6 h from Slack by adam.baker@example.com', 'presetReason': True}]
    update = next(p for m, p in api.calls if m == 'chat.update')
    assert update['ts'] == ANSWER_TS and all(b['type'] != 'actions' for b in update['blocks'])
    assert update['blocks'][-1] == {'type': 'context', 'elements': [{'type': 'mrkdwn', 'text': 'Mute 6 h · adam.baker@example.com'}]}
    assert store.get_action(act['id'])['cardActionApplied']['at']
    assert ephemerals(api) == ['Already applied: Mute 6 h · adam.baker@example.com']
    done = [a for a in store.list_actions('done', 10) if a['type'] == 'mute']
    assert len(done) == 1 and done[0]['result'] == 'No action needed'


def test_known_noise_click_carries_the_slack_reason_and_take_carries_none(store: Store):
    # given
    card_with_post(store)
    known = action_ask(store, {'key': KEY, 'action': 'known'})
    take = action_ask(store, {'key': KEY, 'action': 'take'})
    box, applier, _ = clicks(store)

    # when
    box.handle(action_payload(known['id'], call={'key': KEY, 'action': 'known'}))
    box.handle(action_payload(take['id'], call={'key': KEY, 'action': 'take'}))

    # then
    assert applier.calls[0]['reason'] == 'Known noise from Slack by adam.baker@example.com'
    assert 'reason' not in applier.calls[1]


def test_resolve_and_take_carry_no_hours(store: Store):
    # given
    card_with_post(store)
    act = action_ask(store, {'key': KEY, 'action': 'take'})
    box, applier, _ = clicks(store)

    # when
    box.handle(action_payload(act['id']))

    # then
    assert applier.calls[0]['type'] == 'take' and 'hours' not in applier.calls[0] and applier.calls[0]['label'] == f'Take from Slack: {KEY}'


def test_applying_the_card_action_keeps_the_create_task_button(store: Store):
    # given
    card_with_post(store)
    act = action_ask(store)
    store.set_action_fields(act['id'], {'ticketProposal': PROPOSAL})
    box, applier, api = clicks(store)

    # when
    box.handle(action_payload(act['id'], with_draft=True))

    # then
    update = next(p for m, p in api.calls if m == 'chat.update')
    actions = [b for b in update['blocks'] if b['type'] == 'actions']
    assert [e['action_id'] for e in actions[0]['elements']] == [CREATE_TASK]
    assert update['blocks'][-1]['type'] == 'context'


def test_creating_the_task_keeps_the_card_action_button(store: Store):
    # given
    card_with_post(store)
    act = action_ask(store)
    store.set_action_fields(act['id'], {'ticketProposal': PROPOSAL})
    box, applier, api = clicks(store)
    both = payload(act['id'])
    both['message']['blocks'] = answer_blocks('Ready.', PROPOSAL, act['id'], CALL, {'svc': 'checkout-service', 'label': 'ERROR pattern'})

    # when
    box.handle(both)

    # then
    update = next(p for m, p in api.calls if m == 'chat.update')
    actions = [b for b in update['blocks'] if b['type'] == 'actions']
    assert [e['action_id'] for e in actions[0]['elements']] == [CARD_ACTION]


def test_a_non_operator_card_action_click_gets_the_ephemeral_line(store: Store):
    # given
    card_with_post(store)
    act = action_ask(store)
    box, applier, api = clicks(store, emails={'U9': 'x@example.com'})

    # when
    got = box.handle(action_payload(act['id'], user='U9'))

    # then
    assert got == 'not-operator' and applier.calls == [] and ephemerals(api) == [msg('slackevents.click.action_non_operator')]
    assert 'cardActionApplied' not in store.get_action(act['id'])


def test_a_card_action_click_during_a_lookup_outage_is_silent(store: Store):
    # given
    card_with_post(store)
    act = action_ask(store)
    box, applier, api = clicks(store)
    api.fail.add('users.info')

    # when
    got = box.handle(action_payload(act['id']))

    # then
    assert got == 'operator-unknown' and ephemerals(api) == [] and applier.calls == []
    assert 'cardActionApplied' not in store.get_action(act['id'])


def test_a_click_while_the_card_action_is_being_applied_says_so(store: Store):
    # given
    card_with_post(store)
    act = action_ask(store)
    store.claim_card_action(act['id'], 'nina')
    box, applier, api = clicks(store)

    # when
    got = box.handle(action_payload(act['id']))

    # then
    assert got == 'claimed' and applier.calls == [] and ephemerals(api) == [msg('slackevents.click.applying')]


@pytest.mark.parametrize('error, reason, failed', [(TicketRefused('card is gone'), 'card is gone', 'card is gone'), (RuntimeError('boom'), 'RuntimeError', 'RuntimeError')])
def test_a_failing_card_action_releases_the_claim_and_says_so(store: Store, error, reason, failed):
    # given
    card_with_post(store)
    act = action_ask(store)
    applier = Failing(error)
    box, api = with_applier(store, applier)

    # when
    got = box.handle(action_payload(act['id']))

    # then
    assert got == 'refused' and len(applier.calls) == 1
    assert 'cardActionApplied' not in store.get_action(act['id'])
    assert ephemerals(api) == [f'Could not act on the card: {reason}']
    assert [a['result'] for a in store.list_actions('failed', 5) if a['type'] == 'resolve'] == [failed]
    assert 'chat.update' not in api.methods()


def test_a_card_action_click_on_a_gone_action_or_card_says_so(store: Store):
    # given
    card_with_post(store)
    no_action = action_ask(store, call=None)
    gone_card = action_ask(store, call={'key': 'log|gone|ERROR|y', 'action': 'resolve'})
    box, applier, api = clicks(store)

    # when
    got = [box.handle(action_payload(i)) for i in ('nope', no_action['id'], gone_card['id'])]

    # then
    assert got == ['gone'] * 3 and applier.calls == [] and ephemerals(api) == [msg('slackevents.click.gone')] * 3


def test_a_card_action_click_on_a_page_ask_a_ticket_only_ask_or_an_unknown_action_applies_nothing(store: Store):
    # given
    card_with_post(store)
    page_ask = store.add_action({'type': 'ask', 'key': 'ask', 'target': 'page', 'text': 'q', 'cards': [KEY], 'cardAction': CALL}, 'done', 'answered')
    ticket_only = drafted_ask(store)
    bad_action = action_ask(store, call={'key': KEY, 'action': 'delete'})
    box, applier, api = clicks(store)

    # when
    got = [box.handle(action_payload(i)) for i in (page_ask['id'], ticket_only['id'], bad_action['id'])]

    # then
    assert got == ['gone'] * 3 and applier.calls == [] and ephemerals(api) == [msg('slackevents.click.gone')] * 3
    assert all('cardActionApplied' not in store.get_action(a['id']) for a in (page_ask, ticket_only))


OFFER = {'key': KEY, 'action': 'take', 'owner': 'gary.cole@example.com'}
OFFER_TEXT = '👀 gary.cole seems to be looking at this. Shall we mark the card In progress?'


def offer_payload(action_id: str, user: str = 'U1', call: dict = OFFER, said: str = OFFER_TEXT) -> dict:
    return {'type': 'block_actions', 'user': {'id': user}, 'channel': {'id': 'C1'},
            'message': {'ts': ANSWER_TS, 'thread_ts': CARD_TS, 'text': said, 'blocks': take_offer_blocks(said, action_id, call)},
            'actions': [{'action_id': CARD_ACTION, 'value': action_id}]}


def test_an_in_progress_offer_click_takes_the_card_for_the_speaker_whoever_clicks(store: Store):
    # given
    card_with_post(store)
    offer = store.add_action({'type': 'take-offer', 'source': 'slack', 'key': KEY, 'target': KEY, 'requestedBy': 'gary.cole@example.com', 'cardAction': OFFER}, 'done', OFFER_TEXT)
    box, applier, api = clicks(store)

    # when
    first = box.handle(offer_payload(offer['id']))
    second = box.handle(offer_payload(offer['id']))

    # then
    assert (first, second) == ('applied', 'claimed')
    assert applier.calls == [{'type': 'take', 'key': KEY, 'target': KEY, 'label': f'In progress · gary.cole from Slack: {KEY}', 'requestedBy': 'adam.baker@example.com',
                              'source': 'slack', 'askId': offer['id'], 'owner': 'gary.cole@example.com'}]
    update = next(p for m, p in api.calls if m == 'chat.update')
    assert update['blocks'] == [{'type': 'section', 'text': {'type': 'mrkdwn', 'text': OFFER_TEXT}},
                                {'type': 'context', 'elements': [{'type': 'mrkdwn', 'text': 'In progress · gary.cole · adam.baker@example.com'}]}]
    assert ephemerals(api) == ['Already applied: In progress · gary.cole · adam.baker@example.com']


def test_a_ticket_offer_click_takes_the_card_with_the_ticket_and_the_clicker_as_owner(store: Store):
    # given
    card_with_post(store)
    call = {'key': KEY, 'action': 'take', 'ticket': 'INFRA-402'}
    said = 'INFRA-402 looks opened for this card.'
    offer = store.add_action({'type': 'take-offer', 'source': 'slack', 'key': KEY, 'target': KEY, 'requestedBy': 'gary.cole@example.com', 'cardAction': call}, 'done', said)
    box, applier, api = clicks(store)

    # when
    got = box.handle(offer_payload(offer['id'], call=call, said=said))

    # then
    assert got == 'applied'
    assert applier.calls == [{'type': 'take', 'key': KEY, 'target': KEY, 'label': f'In progress · INFRA-402 from Slack: {KEY}', 'requestedBy': 'adam.baker@example.com',
                              'source': 'slack', 'askId': offer['id'], 'ticket': 'INFRA-402'}]
    update = next(p for m, p in api.calls if m == 'chat.update')
    assert update['blocks'][1] == {'type': 'context', 'elements': [{'type': 'mrkdwn', 'text': 'In progress · INFRA-402 · adam.baker@example.com'}]}


def test_a_resolve_offer_click_resolves_with_what_was_said_in_the_thread_as_the_reason(store: Store):
    # given
    card_with_post(store)
    call = {'key': KEY, 'action': 'resolve', 'reason': 'gary.cole in Slack: fixed now'}
    said = 'gary.cole says the problem is solved.'
    offer = store.add_action({'type': 'resolve-offer', 'source': 'slack', 'key': KEY, 'target': KEY, 'requestedBy': 'gary.cole@example.com', 'cardAction': call}, 'done', said)
    box, applier, api = clicks(store)

    # when
    got = box.handle(offer_payload(offer['id'], call=call, said=said))

    # then
    assert got == 'applied'
    assert applier.calls == [{'type': 'resolve', 'key': KEY, 'target': KEY, 'label': f'Resolve from Slack: {KEY}', 'requestedBy': 'adam.baker@example.com',
                              'source': 'slack', 'askId': offer['id'], 'reason': 'gary.cole in Slack: fixed now'}]


def test_a_resolve_proposal_with_a_kind_says_it_on_the_button_and_passes_kind_and_ref(store: Store):
    # given
    card_with_post(store)
    call = {'key': KEY, 'action': 'resolve', 'kind': 'duplicate', 'ref': '#12'}
    act = action_ask(store, call)
    box, applier, api = clicks(store)

    # when
    blocks = action_payload(act['id'], call=call)['message']['blocks']
    got = box.handle(action_payload(act['id'], call=call))

    # then
    button = next(e for b in blocks if b['type'] == 'actions' for e in b['elements'] if e.get('action_id') == CARD_ACTION)
    assert button['text']['text'] == 'Resolve · Duplicate of #12' and 'confirm' not in button
    assert take_offer_blocks('x', 'id', {'key': KEY, 'action': 'resolve', 'kind': 'fixed', 'ref': 'https://gitlab.example.com/x/-/merge_requests/1'})[1]['elements'][0]['text']['text'] == 'Resolve · Fixed'
    assert got == 'applied'
    assert applier.calls == [{'type': 'resolve', 'key': KEY, 'target': KEY, 'label': f'Resolve · Duplicate of #12 from Slack: {KEY}', 'requestedBy': 'adam.baker@example.com',
                              'source': 'slack', 'askId': act['id'], 'kind': 'duplicate', 'ref': '#12'}]


def test_a_slack_resolve_is_stored_as_via_slack_by_the_clicker(store: Store):
    # given
    card_with_post(store)
    act = action_ask(store, {'key': KEY, 'action': 'resolve', 'kind': 'fixed', 'ref': 'OPS-140'})
    slack, _, _ = fake_slack(emails={'U1': 'adam.baker@example.com'})
    box = Clicks(store, slack, Operators(lambda email: email in {'adam.baker@example.com'}, slack, settings(), lambda: NOW), lambda action: actions.apply(store, action))

    # when
    got = box.handle(action_payload(act['id'], call={'key': KEY, 'action': 'resolve', 'kind': 'fixed', 'ref': 'OPS-140'}))

    # then
    how = store.anomaly(KEY)['resolution']
    assert got == 'applied'
    assert {k: how[k] for k in ('kind', 'ref', 'by', 'via')} == {'kind': 'fixed', 'ref': 'OPS-140', 'by': 'adam.baker@example.com', 'via': 'slack'}


def test_a_non_operator_click_on_an_offer_applies_nothing(store: Store):
    # given
    card_with_post(store)
    offer = store.add_action({'type': 'take-offer', 'source': 'slack', 'key': KEY, 'target': KEY, 'cardAction': OFFER}, 'done', OFFER_TEXT)
    box, applier, api = clicks(store, emails={'U9': 'x@example.com'})

    # when
    got = box.handle(offer_payload(offer['id'], user='U9'))

    # then
    assert got == 'not-operator'
    assert applier.calls == [] and ephemerals(api) == [msg('slackevents.click.action_non_operator')]


POST_CARD = {'key': KEY, 'sev': 'warn', 'status': 'open', 'svc': 'checkout-service', 'label': 'ERROR pattern'}


def old_post_blocks(action_id: str) -> list[dict]:
    return [{'type': 'section', 'text': {'type': 'mrkdwn', 'text': ':large_orange_circle: *checkout-service* / ERROR pattern \u00b7 Warning'}},
            {'type': 'context', 'elements': [{'type': 'mrkdwn', 'text': 'Now *1* \u00b7 auto'}]},
            {'type': 'actions', 'elements': [{'type': 'button', 'text': {'type': 'plain_text', 'text': 'Open in Skopax'}, 'url': 'https://skopax.example.com/#a=x', 'style': 'primary'}]
             + [{'type': 'button', 'action_id': f'{CARD_ACTION}_{k}', 'value': action_id, 'text': {'type': 'plain_text', 'text': k}} for k in POST_ACTIONS]}]


def post_payload(action_id: str, kind: str, user: str = 'U1') -> dict:
    return {'type': 'block_actions', 'user': {'id': user}, 'channel': {'id': 'C1'},
            'message': {'ts': CARD_TS, 'text': 'Warning \u00b7 checkout-service / ERROR pattern', 'blocks': old_post_blocks(action_id)},
            'actions': [{'action_id': f'{CARD_ACTION}_{kind}', 'value': action_id}]}


def test_a_card_post_menu_offers_take_resolve_and_known_noise_only_when_asked():
    # when
    with_buttons = anomaly_message(POST_CARD, 'https://skopax.example.com/#a=x', 'auto', None, 'abc')
    without = anomaly_message(POST_CARD, 'https://skopax.example.com/#a=x', 'auto')

    # then
    menu = with_buttons['blocks'][0]['accessory']
    assert [(menu['action_id'], o['value'], o['text']['text']) for o in menu['options']] == [
        ('card_menu', 'take:abc', 'Take'), ('card_menu', 'resolve:abc', 'Resolve'), ('card_menu', 'known:abc', 'Known noise')]
    assert 'accessory' not in without['blocks'][0] and not any(b['type'] == 'actions' for m in (with_buttons, without) for b in m['blocks'])
    assert not any('confirm' in o for o in menu['options']), 'Known noise asks in a window instead, see test_slack_layout'
    assert 'confirm' in take_offer_blocks('x', 'id', {'key': KEY, 'action': 'known'})[1]['elements'][0]
    assert 'confirm' not in take_offer_blocks('x', 'id', {'key': KEY, 'action': 'take'})[1]['elements'][0]


def test_the_triage_buttons_on_a_card_post_apply_once_and_leave_a_line_in_the_post(store: Store):
    # given
    card_with_post(store)
    doc = store.add_action({'type': 'post-buttons', 'source': 'slack', 'key': KEY, 'target': KEY, 'cardActions': {k: {'key': KEY, 'action': k} for k in POST_ACTIONS}}, 'done', 'buttons')
    box, applier, api = clicks(store)

    # when
    first = box.handle(post_payload(doc['id'], 'resolve'))
    second = box.handle(post_payload(doc['id'], 'take'))

    # then
    assert (first, second) == ('applied', 'claimed')
    assert applier.calls == [{'type': 'resolve', 'key': KEY, 'target': KEY, 'label': f'Resolve from Slack: {KEY}', 'requestedBy': 'adam.baker@example.com', 'source': 'slack', 'askId': doc['id']}]
    update = next(p for m, p in api.calls if m == 'chat.update')
    assert 'attachments' not in update
    ids = [e.get('action_id') for b in update['blocks'] if b['type'] == 'actions' for e in b['elements']]
    labels = [e['text']['text'] for b in update['blocks'] if b['type'] == 'actions' for e in b['elements'] if e['type'] == 'button']
    assert not any(str(i).startswith(CARD_ACTION) for i in ids) and labels == ['Open in Skopax']
    assert update['blocks'][-1] == {'type': 'context', 'elements': [{'type': 'mrkdwn', 'text': 'Resolve · adam.baker@example.com'}]}
    assert ephemerals(api) == ['Already applied: Resolve · adam.baker@example.com']


def test_a_post_button_for_an_unknown_kind_or_a_missing_card_applies_nothing(store: Store):
    # given
    card_with_post(store)
    doc = store.add_action({'type': 'post-buttons', 'source': 'slack', 'key': KEY, 'target': KEY, 'cardActions': {'take': {'key': 'log|gone|ERROR|g', 'action': 'take'}}}, 'done', 'buttons')
    box, applier, api = clicks(store)

    # when
    got = [box.handle(post_payload(doc['id'], 'resolve')), box.handle(post_payload(doc['id'], 'take'))]

    # then
    assert got == ['gone', 'gone'] and applier.calls == []


def test_a_click_in_a_reminder_for_several_cards_takes_its_own_card_and_leaves_the_other_buttons(store: Store):
    # given
    card_with_post(store)
    mine = store.add_action({'type': 'reminder', 'source': 'slack', 'key': KEY, 'target': KEY, 'requestedBy': 'server', 'cardAction': {'key': KEY, 'action': 'take'}}, 'done', 'r')
    blocks = [{'type': 'section', 'text': {'type': 'mrkdwn', 'text': '⏰ Reminder: 2 cards in this thread have waited 24 hours, nobody has taken them yet.'}},
              {'type': 'section', 'text': {'type': 'mrkdwn', 'text': '• *a*'}},
              {'type': 'actions', 'elements': [{'type': 'button', 'action_id': CARD_ACTION, 'value': mine['id'], 'text': {'type': 'plain_text', 'text': 'Take'}}]},
              {'type': 'section', 'text': {'type': 'mrkdwn', 'text': '• *b*'}},
              {'type': 'actions', 'elements': [{'type': 'button', 'action_id': CARD_ACTION, 'value': 'other-offer', 'text': {'type': 'plain_text', 'text': 'Take'}}]}]
    box, applier, api = clicks(store)

    # when
    out = box.handle({'type': 'block_actions', 'user': {'id': 'U1'}, 'channel': {'id': 'C1'}, 'message': {'ts': ANSWER_TS, 'thread_ts': CARD_TS, 'text': 'r', 'blocks': blocks},
                      'actions': [{'action_id': CARD_ACTION, 'value': mine['id']}]})

    # then
    assert out == 'applied' and [c['key'] for c in applier.calls] == [KEY]
    update = next(p for m, p in api.calls if m == 'chat.update')
    assert [b['elements'][0]['value'] for b in update['blocks'] if b['type'] == 'actions'] == ['other-offer']
    assert update['blocks'][-1]['elements'][0]['text'] == f"Take · adam.baker@example.com · {store.anomaly(KEY)['svc']} / {store.anomaly(KEY)['label']}"


def test_a_post_from_before_the_new_layout_keeps_its_attachment_when_a_button_is_clicked(store: Store):
    # given
    card_with_post(store)
    doc = store.add_action({'type': 'post-buttons', 'source': 'slack', 'key': KEY, 'target': KEY, 'cardActions': {k: {'key': KEY, 'action': k} for k in POST_ACTIONS}}, 'done', 'buttons')
    box, applier, api = clicks(store)
    old = [{'type': 'section', 'fields': [{'type': 'mrkdwn', 'text': '*Now*\n5'}]},
           {'type': 'actions', 'elements': [{'type': 'button', 'text': {'type': 'plain_text', 'text': 'Open in Skopax'}, 'url': 'https://skopax.example.com/#a=x'}]},
           {'type': 'actions', 'elements': [{'type': 'button', 'action_id': f'{CARD_ACTION}_{k}', 'value': doc['id'], 'text': {'type': 'plain_text', 'text': k}} for k in POST_ACTIONS]}]
    payload = {'type': 'block_actions', 'user': {'id': 'U1'}, 'channel': {'id': 'C1'},
               'message': {'ts': CARD_TS, 'text': 'x', 'attachments': [{'id': 1, 'color': 'e08a00', 'fallback': 'x', 'blocks': old}]},
               'actions': [{'action_id': f'{CARD_ACTION}_resolve', 'value': doc['id']}]}

    # when
    got = box.handle(payload)

    # then
    assert got == 'applied'
    update = next(p for m, p in api.calls if m == 'chat.update')
    [att] = update['attachments']
    assert att['color'] == '#e08a00' and 'blocks' not in update
    assert not any(str(e.get('action_id')).startswith(CARD_ACTION) for b in att['blocks'] if b['type'] == 'actions' for e in b['elements'])
    assert att['blocks'][-1]['elements'][0]['text'] == 'Resolve · adam.baker@example.com'
