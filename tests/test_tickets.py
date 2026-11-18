import dataclasses
import pytest
from fastapi.testclient import TestClient

from tests.conftest import AUTH, anomaly, settings, state
from tests.fake_slack import FakeSlackApi
from tests.plugin_kit import serve_jira
from tests.test_slack import FakeSlack
from skopax import actions as page_actions
from skopax.app import create_app
from skopax.enrich import JiraRefused
from skopax.messages import msg
from skopax.slack import Slack
from skopax.store import Store

KEY = 'log|checkout-service|ERROR|boom'


class FakeJira:
    enabled = True

    def __init__(self, fail: str | None = None, sprint: int | None = 2604):
        self.created: list[dict] = []
        self.fail = fail
        self.sprint = sprint

    def find_user(self, query: str) -> str | None:
        return '712020:abc' if query == 'oscar.green@example.com' else None

    def active_sprint(self, project: str, field: str = '') -> int | None:
        return self.sprint

    def create_issue(self, fields: dict) -> str:
        if self.fail:
            raise JiraRefused(self.fail)
        self.created.append(fields)
        return f'OPS-{120 + len(self.created)}'


def text_of(adf: dict) -> list[str]:
    return [''.join(n['text'] for n in c['content']) for c in adf['content']]


def hrefs(adf: dict) -> list[tuple[str, str]]:
    return [(n['text'], n['marks'][0]['attrs']['href']) for c in adf['content'] for n in c['content'] if n.get('marks')]


def test_create_task_files_a_task_with_the_card_facts_and_takes_the_card(store: Store):
    # given
    store.save_state(state(anomaly(KEY, now=40, d1=5, d7=4, firstSeen='2026-10-22T05:00:00Z', short='ERROR burst', suspects=['one client retried', 'gateway 502'], recommend='ask the client team',
                                   note={'text': 'an old finding', 'at': '2026-10-22T05:30:00Z'}, links=[{'label': 'Kibana · ERROR lines', 'url': 'https://kibana.example.com/app/discover#/?x'}, {'label': 'bad', 'url': 'javascript:alert(1)'}],
                                   code=[{'label': 'OrderService.java:42', 'url': 'https://gitlab.example.com/x/-/blob/master/OrderService.java#L42'}])), None)
    jira = FakeJira()
    action = {'type': 'ticket', 'key': KEY, 'target': KEY, 'title': '  OPS: checkout-service retry storm  ', 'reason': 'the client side keeps retrying', 'requestedBy': 'oscar.green@example.com'}

    # when
    result = page_actions.apply(store, action, jira, dataclasses.replace(settings(), jira_dev_scope='Web'), 'https://skopax.example.com')
    fields = jira.created[0]
    card = store.anomaly(KEY)

    # then
    assert result == 'OPS-121 created · In progress · oscar.green@example.com'
    assert (fields['project'], fields['issuetype'], fields['summary']) == ({'key': 'OPS'}, {'name': 'Task'}, 'OPS: checkout-service retry storm') and 'parent' not in fields, 'no epic unless one is chosen'
    assert (fields['customfield_1'], fields['customfield_2'], fields['customfield_3']) == ([{'value': 'Web'}], 1.0, 2604)
    assert fields['assignee'] == fields['customfield_4'] == {'accountId': '712020:abc'}
    assert text_of(fields['description']) == ['warn · checkout-service · ERROR pattern · now 40 vs 1 d 5 / 7 d 4 · since 22 Oct 05:00 UTC', 'ERROR burst', 'the client side keeps retrying', 'Findings: an old finding',
                                               'Suspects: one client retried; gateway 502', 'Recommended: ask the client team', 'Card: Skopax', 'Link: Kibana · ERROR lines', 'Link: OrderService.java:42']
    assert hrefs(fields['description']) == [('Skopax', 'https://skopax.example.com/#a=log%7Ccheckout-service%7CERROR%7Cboom'), ('Kibana · ERROR lines', 'https://kibana.example.com/app/discover#/?x'),
                                            ('OrderService.java:42', 'https://gitlab.example.com/x/-/blob/master/OrderService.java#L42')], 'card links and code links become real links; a non-https one is dropped'
    assert (card['triage'], card['ticket'], card['owner'], card['note']['text']) == ('progress', 'OPS-121', 'oscar.green@example.com', 'the client side keeps retrying')
    assert [r['text'] for r in store.list_activity(None, 5)] == ['OPS-121 created for ' + KEY]


def test_an_unknown_operator_and_no_sprint_leave_those_fields_out_and_the_title_defaults(store: Store):
    # given
    store.save_state(state(anomaly(KEY)), None)
    jira = FakeJira(sprint=None)

    # when
    result = page_actions.apply(store, {'type': 'ticket', 'key': KEY, 'target': KEY, 'requestedBy': 'someone'}, jira, settings())
    fields = jira.created[0]
    assert result.endswith('· no Jira account found for someone: assignee and Developer are empty, fill them in Jira (or map the login in jira.accounts)'), result

    # then
    assert fields['summary'] == 'checkout-service ERROR pattern', 'no title prefix unless one is set'
    assert 'assignee' not in fields and 'customfield_4' not in fields and 'customfield_3' not in fields
    assert store.list_actions(None, 1) == [], 'apply alone queues no action'
    assert hrefs(fields['description']) == [('Skopax', 'https://skopax.example.com/#a=log%7Ccheckout-service%7CERROR%7Cboom')]


def test_a_scope_field_without_a_scope_value_sends_no_scope(store: Store):
    # given
    store.save_state(state(anomaly(KEY)), None)
    jira = FakeJira()

    # when
    page_actions.apply(store, {'type': 'ticket', 'key': KEY, 'target': KEY, 'requestedBy': 'oscar.green@example.com'}, jira, settings())

    # then
    assert settings().jira_scope_field == 'customfield_1' and 'customfield_1' not in jira.created[0]


@pytest.mark.parametrize('project', ['OPS', 'INFRA'])
def test_a_default_title_starts_with_the_configured_prefix(store: Store, project: str):
    # given
    store.save_state(state(anomaly(KEY)), None)
    jira = FakeJira()
    prefixed = dataclasses.replace(settings(), jira_title_prefix='OPS: ')

    # when
    page_actions.apply(store, {'type': 'ticket', 'key': KEY, 'target': KEY, 'project': project, 'requestedBy': 'oscar.green@example.com'}, jira, prefixed)

    # then
    assert jira.created[0]['summary'] == 'OPS: checkout-service ERROR pattern'


def test_a_jira_refusal_or_a_missing_jira_fails_the_action_and_leaves_the_card(store: Store, client: TestClient):
    # given
    client.put('/api/state', json=state(anomaly(KEY)), headers=AUTH)

    # when
    refused = client.post('/api/actions', json={'type': 'ticket', 'key': KEY, 'label': 'Create task'}, headers=AUTH)
    try:
        page_actions.apply(store, {'type': 'ticket', 'key': KEY, 'target': KEY, 'requestedBy': 'x'}, FakeJira(fail='Jira answered HTTP 400 to the create'), settings())
        raised = None
    except page_actions.Refused as e:
        raised = str(e)

    # then
    assert (refused.json()['status'], refused.json()['result']) == ('failed', msg('tickets.jira_off'))
    assert raised == 'Jira answered HTTP 400 to the create'
    assert 'triage' not in store.anomaly(KEY)


def test_a_typed_description_and_points_from_the_dialog_replace_the_defaults(store: Store):
    # given
    store.save_state(state(anomaly(KEY, note={'text': 'an old finding', 'at': '2026-10-22T05:30:00Z'})), None)
    jira = FakeJira()

    # when
    page_actions.apply(store, {'type': 'ticket', 'key': KEY, 'target': KEY, 'title': 'OPS: retry storm', 'description': 'Line 1\n\nLine 2  \n', 'points': 3, 'requestedBy': 'oscar.green@example.com'}, jira, settings())
    fields = jira.created[0]

    # then
    assert text_of(fields['description']) == ['Line 1', 'Line 2', 'Card: Skopax']
    assert fields['customfield_2'] == 3
    assert store.anomaly(KEY)['note']['text'] == 'an old finding', 'the dialog description goes to Jira only, the card note stays'


def test_points_must_be_fibonacci_and_the_notice_goes_out_after_the_create(store: Store):
    # given
    store.save_state(state(anomaly(KEY)), None)
    jira = FakeJira()
    told = []

    def notify(key: str, new_key: str, by: str) -> list[str]:
        told.append((key, new_key, by))
        return ['thread', 'DM']

    # when
    try:
        page_actions.apply(store, {'type': 'ticket', 'key': KEY, 'target': KEY, 'points': 4, 'requestedBy': 'x'}, jira, settings())
        refused = None
    except page_actions.Refused as e:
        refused = str(e)
    result = page_actions.apply(store, {'type': 'ticket', 'key': KEY, 'target': KEY, 'points': 5, 'requestedBy': 'oscar.green@example.com'}, jira, settings(), notify=notify)

    # then
    assert refused == 'story points must be one of 1, 2, 3, 5, 8, 13, 21' and jira.created == [] or jira.created[0]['customfield_2'] == 5
    assert len(jira.created) == 1 and jira.created[0]['customfield_2'] == 5
    assert result == 'OPS-121 created · In progress · oscar.green@example.com · notified: thread, DM'
    assert told == [(KEY, 'OPS-121', 'oscar.green@example.com')]


def test_a_mapped_login_in_jira_accounts_beats_the_user_search(store: Store):
    # given
    import dataclasses
    store.save_state(state(anomaly(KEY)), None)
    jira = FakeJira()
    cfg = dataclasses.replace(settings(), jira_accounts='Someone@example.com=712020:mapped, other=712020:x')

    # when
    result = page_actions.apply(store, {'type': 'ticket', 'key': KEY, 'target': KEY, 'requestedBy': 'someone@example.com'}, jira, cfg)

    # then
    assert jira.created[0]['assignee'] == jira.created[0]['customfield_4'] == {'accountId': '712020:mapped'}
    assert 'no Jira account' not in result


def test_a_tech_task_goes_to_the_infra_project_without_the_project_fields_and_still_takes_the_card(store: Store):
    # given
    store.save_state(state(anomaly(KEY, short='Varnish misses')), None)
    jira = FakeJira()
    action = {'type': 'ticket', 'key': KEY, 'target': KEY, 'title': 'Varnish: /gift-cards cacheable list', 'description': 'Add it to the list.', 'points': 5, 'project': 'INFRA',
              'requestedBy': 'oscar.green@example.com'}

    # when
    result = page_actions.apply(store, action, jira, settings(), 'https://skopax.example.com')
    fields = jira.created[0]

    # then
    assert result.startswith('OPS-121 created · In progress')
    assert set(fields) == {'project', 'issuetype', 'summary', 'description', 'assignee'}
    assert (fields['project'], fields['summary'], fields['assignee']) == ({'key': 'INFRA'}, 'Varnish: /gift-cards cacheable list', {'accountId': '712020:tech'})
    assert text_of(fields['description'])[0] == 'Add it to the list.' and store.anomaly(KEY)['triage'] == 'progress'


def test_an_unknown_project_is_refused(store: Store):
    # given
    store.save_state(state(anomaly(KEY)), None)
    jira = FakeJira()

    # when
    with pytest.raises(page_actions.Refused) as refused:
        page_actions.apply(store, {'type': 'ticket', 'key': KEY, 'target': KEY, 'project': 'XYZ', 'requestedBy': 'oscar.green@example.com'}, jira, settings())

    # then
    assert str(refused.value) == 'project must be one of OPS, WEB, APP, MOB, INFRA' and jira.created == []


def test_a_task_goes_to_the_project_and_epic_picked_in_the_dialog(store: Store):
    # given
    store.save_state(state(anomaly(KEY, short='ERROR burst')), None)
    jira = FakeJira()
    asked = []
    jira.active_sprint = lambda project, field='': asked.append(project) or 2771
    base = {'type': 'ticket', 'key': KEY, 'target': KEY, 'title': 'OPS: redirect error', 'requestedBy': 'oscar.green@example.com'}

    # when
    typed = dataclasses.replace(settings(), jira_issue_types=(('WEB', 'Story'),))
    page_actions.apply(store, base | {'project': 'WEB', 'epic': ' web-201 '}, jira, typed, 'https://skopax.example.com')
    page_actions.apply(store, base | {'project': 'APP'}, jira, typed, 'https://skopax.example.com')
    with pytest.raises(page_actions.Refused) as bad:
        page_actions.apply(store, base | {'project': 'OPS', 'epic': 'not a key'}, jira, settings())

    # then
    web, other = jira.created
    assert (web['project'], web['issuetype'], web['parent'], web['customfield_3']) == ({'key': 'WEB'}, {'name': 'Story'}, {'key': 'WEB-201'}, 2771)
    assert (other['project'], other['issuetype']) == ({'key': 'APP'}, {'name': 'Task'}) and 'parent' not in other and asked == ['WEB', 'APP']
    assert str(bad.value) == 'the epic is a Jira key such as OPS-123'


def test_a_default_epic_set_in_settings_applies_to_its_own_project_only(store: Store):
    # given
    import dataclasses
    store.save_state(state(anomaly(KEY)), None)
    jira = FakeJira()
    s = dataclasses.replace(settings(), jira_parent='OPS-103', jira_projects=('OPS', 'APP'))
    base = {'type': 'ticket', 'key': KEY, 'target': KEY, 'requestedBy': 'oscar.green@example.com'}

    # when
    page_actions.apply(store, dict(base), jira, s)
    page_actions.apply(store, base | {'project': 'APP'}, jira, s)
    with pytest.raises(page_actions.Refused) as refused:
        page_actions.apply(store, base | {'project': 'WEB'}, jira, s)

    # then
    assert jira.created[0]['parent'] == {'key': 'OPS-103'} and 'parent' not in jira.created[1]
    assert str(refused.value) == 'project must be one of OPS, APP, INFRA'


def test_issue_types_and_custom_field_ids_come_from_the_settings(store: Store):
    # given
    store.save_state(state(anomaly(KEY, short='ERROR burst')), None)
    jira = FakeJira()
    typed = dataclasses.replace(settings(), jira_issue_types=(('OPS', 'Story'),), jira_projects=('OPS', 'WEB'))
    bare = dataclasses.replace(typed, jira_scope_field='', jira_points_field='', jira_sprint_field='')
    base = {'type': 'ticket', 'key': KEY, 'target': KEY, 'requestedBy': 'x'}

    # when
    page_actions.apply(store, base | {'project': 'OPS'}, jira, typed, 'https://skopax.example.com')
    page_actions.apply(store, base | {'project': 'WEB'}, jira, typed, 'https://skopax.example.com')
    page_actions.apply(store, base | {'project': 'WEB'}, jira, bare, 'https://skopax.example.com')

    # then
    ops, web, plain = jira.created
    assert (ops['issuetype'], web['issuetype']) == ({'name': 'Story'}, {'name': 'Task'})
    assert not {'customfield_1', 'customfield_2', 'customfield_3'} & set(plain)


@pytest.mark.parametrize('push_on, told', [(True, 'notified: thread, DM'), (False, 'notified: thread')])
def test_phone_push_off_also_silences_the_task_notice_dm_to_the_push_users(store: Store, push_on, told):
    # given
    store.save_state(state(anomaly(KEY, slackPost={'channel': 'C123', 'ts': '1792224000.000100'})), None)
    transport = FakeSlack()
    s = dataclasses.replace(settings(), slack_push_user='U0PUSH', push_on=push_on)
    serve_jira(store, s)

    # when
    with TestClient(create_app(s, store, slack=Slack('xoxb-test', '#ops', transport, FakeSlackApi()), jira=FakeJira())) as c:
        done = c.post('/api/actions', json={'type': 'ticket', 'key': KEY, 'title': 'checkout-service retry storm'}, headers=AUTH).json()

    # then
    assert done['status'] == 'done' and f'{told} \u00b7' in done['result']
    assert [p['text'].split(' \u00b7 ')[0] for _, p in transport.calls if p.get('channel') == 'U0PUSH'] == (['OPS-121 created for checkout-service ERROR pattern by token:bootstrap'] if push_on else [])
