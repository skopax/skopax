import dataclasses

from tests.conftest import settings
from tests.fake_slack import fake_slack
from tests.test_investigator_tools import sources
from tests.test_slack_clicks import drafted_ask, payload
from tests.test_slackevents import KEY, NOW, card_with_post
from tests.test_tickets import FakeJira
from skopax import drafts
from skopax.app import create_app
from skopax.investigator.tools import Effects, Toolbox
from skopax.operators import Operators
from skopax.slackevents import Clicks
from skopax.store import Store


def filed(store: Store, s, proposal: dict) -> tuple[str, FakeJira]:
    jira = FakeJira()
    app = create_app(s, store, jira=jira)
    slack, _, _ = fake_slack(emails={'U1': 'alice@example.com'})
    clicks = Clicks(store, slack, Operators(lambda email: email in {'alice@example.com'}, slack, s, lambda: NOW), app.state.clicks.apply)
    act = drafted_ask(store, proposal)
    return clicks.handle(payload(act['id'])), jira


def clicked(store: Store, s, draft: dict) -> tuple[str, FakeJira, dict]:
    b = Toolbox(sources(), store, Effects(lambda key, by: {}, lambda key, text, by, *_, notice=None: {}), [KEY], 'live', NOW, 'https://skopax.example.com', ask=True, read_only=True, settings=s)
    b.call('propose_ticket', draft)
    outcome, jira = filed(store, s, b.proposal)
    return outcome, jira, b.proposal


def test_the_slack_create_task_button_files_a_drafted_ticket_in_the_default_project_from_settings(store: Store):
    # given
    card_with_post(store)
    web = dataclasses.replace(settings(), jira_project='WEB', jira_projects=('WEB',))

    # when
    outcome, jira, proposal = clicked(store, web, {'title': 'Checkout null guard', 'description': 'NPE at line 69', 'points': 3})

    # then
    assert outcome == 'created' and proposal['project'] is None
    assert [f['project'] for f in jira.created] == [{'key': 'WEB'}]


def test_the_slack_create_task_button_files_a_tech_draft_in_the_infra_project_from_settings(store: Store):
    # given
    card_with_post(store)
    sre = dataclasses.replace(settings(), jira_tech_project='SRE')

    # when
    outcome, jira, proposal = clicked(store, sre, {'title': 'Cache: /checkout', 'description': 'Add it to the list.', 'points': 5, 'project': 'SRE'})

    # then
    assert outcome == 'created' and proposal == {'key': KEY, 'title': 'Cache: /checkout', 'description': 'Add it to the list.', 'points': None, 'project': 'SRE', drafts.TECH: True}
    assert [f['project'] for f in jira.created] == [{'key': 'SRE'}]
    assert 'customfield_2' not in jira.created[0], 'an Infra task carries no story points'


def test_the_slack_create_task_button_files_a_draft_in_another_configured_project(store: Store):
    # given
    card_with_post(store)
    shop = dataclasses.replace(settings(), jira_project='OPS', jira_projects=('OPS', 'WEB'), jira_tech_project='INFRA')

    # when
    outcome, jira, proposal = clicked(store, shop, {'title': 'Checkout null guard', 'description': 'NPE at line 69', 'points': 3, 'project': 'WEB'})

    # then
    assert outcome == 'created' and (proposal['project'], proposal['points']) == ('WEB', 3)
    assert [f['project'] for f in jira.created] == [{'key': 'WEB'}]


def test_a_draft_into_a_real_project_spelled_like_the_old_tech_marker_stays_in_that_project(store: Store):
    # given
    card_with_post(store)
    shop = dataclasses.replace(settings(), jira_project='OPS', jira_projects=('OPS', 'TECH'), jira_tech_project='INFRA')

    # when
    outcome, jira, proposal = clicked(store, shop, {'title': 'Checkout null guard', 'description': 'NPE at line 69', 'points': 3, 'project': 'TECH'})

    # then
    assert outcome == 'created' and (proposal['project'], proposal['points'], drafts.is_tech(proposal)) == ('TECH', 3, False)
    assert [f['project'] for f in jira.created] == [{'key': 'TECH'}]


def test_a_real_tech_draft_goes_to_the_tech_project_even_next_to_a_project_named_tech(store: Store):
    # given
    card_with_post(store)
    shop = dataclasses.replace(settings(), jira_project='OPS', jira_projects=('OPS', 'TECH'), jira_tech_project='INFRA')

    # when
    outcome, jira, proposal = clicked(store, shop, {'title': 'Cache: /checkout', 'description': 'Add it to the list.', 'points': 5, 'project': 'INFRA'})

    # then
    assert outcome == 'created' and (proposal['project'], proposal['points'], drafts.is_tech(proposal)) == ('INFRA', None, True)
    assert [f['project'] for f in jira.created] == [{'key': 'INFRA'}]


def test_a_clear_note_tech_draft_with_no_project_key_goes_to_the_configured_tech_project(store: Store):
    # given
    card_with_post(store)
    shop = dataclasses.replace(settings(), jira_project='OPS', jira_projects=('OPS', 'TECH'), jira_tech_project='INFRA')
    proposal = {'key': KEY, 'title': 'data-0 Volume disk', 'description': '', 'points': None, 'project': None, drafts.TECH: True}

    # when
    outcome, jira = filed(store, shop, proposal)

    # then
    assert outcome == 'created'
    assert [f['project'] for f in jira.created] == [{'key': 'INFRA'}]
