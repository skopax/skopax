import dataclasses

import pytest
from fastapi.testclient import TestClient

from skopax import actions as page_actions
from skopax.app import create_app
from skopax.contract import SourceDown
from skopax.enrich import JiraRefused
from skopax.jirafields import JiraFields, asked, create_meta, dialog_view, field_choices, given_fields
from skopax.plugins.jira.client import JIRA_GATEWAY, Jira
from skopax.remote import HttpError
from skopax.store import Store
from skopax.tunables import Invalid, parse, registry
from tests.conftest import AUTH, anomaly, settings, state
from tests.fake_http import FakeHttp
from tests.test_tickets import FakeJira

KEY = 'log|checkout-service|ERROR|boom'
SITE = 'https://example.atlassian.net'
GATEWAY = f'{JIRA_GATEWAY}/cid'
TYPES = f'{GATEWAY}/rest/api/3/issue/createmeta/OPS/issuetypes'
TASK_FIELDS = f'{TYPES}/10002'
ALL_FIELDS = f'{GATEWAY}/rest/api/3/field'
VALUES = {'url': SITE, 'email': 'bot@example.com', 'api-token': 'jt', 'cloud-id': 'cid'}
SCOPES = ['Server', 'Browser', 'Admin console', 'Phone', 'Tablet']


def option_field(fid: str, name: str, values: list[str], required: bool = True, multi: bool = True, default: bool = False) -> dict:
    schema = {'type': 'array', 'items': 'option', 'custom': 'com.atlassian.jira.plugin.system.customfieldtypes:multiselect'} if multi else {'type': 'option', 'custom': 'com.atlassian.jira.plugin.system.customfieldtypes:select'}
    return {'fieldId': fid, 'key': fid, 'name': name, 'required': required, 'hasDefaultValue': default, 'schema': schema,
            'allowedValues': [{'id': str(10000 + i), 'value': v} for i, v in enumerate(values)] + [{'id': '19999', 'value': 'Retired', 'disabled': True}]}


SCREEN = [
    {'fieldId': 'summary', 'name': 'Summary', 'required': True, 'hasDefaultValue': False, 'schema': {'type': 'string', 'system': 'summary'}},
    {'fieldId': 'reporter', 'name': 'Reporter', 'required': True, 'hasDefaultValue': False, 'schema': {'type': 'user', 'system': 'reporter'}},
    option_field('customfield_1', 'Scope', SCOPES),
    option_field('customfield_7', 'Team', ['Payments', 'Search'], multi=False),
    {'fieldId': 'customfield_8', 'name': 'Release note', 'required': True, 'hasDefaultValue': False, 'schema': {'type': 'string', 'custom': 'com.atlassian.jira.plugin.system.customfieldtypes:textfield'}},
    {'fieldId': 'customfield_9', 'name': 'Risk', 'required': True, 'hasDefaultValue': False, 'schema': {'type': 'number'}},
    {'fieldId': 'customfield_10', 'name': 'Reviewer', 'required': True, 'hasDefaultValue': False, 'schema': {'type': 'user', 'custom': 'com.atlassian.jira.plugin.system.customfieldtypes:userpicker'}},
    {'fieldId': 'customfield_11', 'name': 'Due window', 'required': True, 'hasDefaultValue': False, 'schema': {'type': 'date'}},
    {'fieldId': 'customfield_12', 'name': 'Labels kept', 'required': True, 'hasDefaultValue': True, 'schema': {'type': 'string'}},
    option_field('customfield_13', 'Optional pick', ['A', 'B'], required=False, multi=False),
]

JIRA_LIST = [
    {'id': 'summary', 'name': 'Summary', 'custom': False, 'schema': {'type': 'string', 'system': 'summary'}},
    {'id': 'customfield_10020', 'name': 'Sprint', 'custom': True, 'schema': {'type': 'array', 'items': 'json', 'custom': 'com.pyxis.greenhopper.jira:gh-sprint'}},
    {'id': 'customfield_10016', 'name': 'Story Points', 'custom': True, 'schema': {'type': 'number', 'custom': 'com.atlassian.jira.plugin.system.customfieldtypes:float'}},
    {'id': 'customfield_10200', 'name': 'Story point estimate', 'custom': True, 'schema': {'type': 'number', 'custom': 'com.pyxis.greenhopper.jira:jsw-story-points'}},
    {'id': 'customfield_10100', 'name': 'Scope', 'custom': True, 'schema': {'type': 'array', 'items': 'option', 'custom': 'com.atlassian.jira.plugin.system.customfieldtypes:multiselect'}},
    {'id': 'customfield_10060', 'name': 'Developer', 'custom': True, 'schema': {'type': 'user', 'custom': 'com.atlassian.jira.plugin.system.customfieldtypes:userpicker'}},
    {'id': 'assignee', 'name': 'Assignee', 'custom': False, 'schema': {'type': 'user', 'system': 'assignee'}},
]


def paged(items: list[dict], key: str, size: int):
    def answer(query: dict, body):
        start = int(query.get('startAt', ['0'])[0])
        return {key: items[start:start + size], 'startAt': start, 'maxResults': size, 'total': len(items)}
    return answer


def jira_http(screen: list[dict] | None = None, page: int = 50) -> FakeHttp:
    return FakeHttp({('GET', TYPES): {'issueTypes': [{'id': '10001', 'name': 'Bug'}, {'id': '10002', 'name': 'Task'}], 'startAt': 0, 'maxResults': 50, 'total': 2},
                     ('GET', TASK_FIELDS): paged(SCREEN if screen is None else screen, 'fields', page), ('GET', ALL_FIELDS): JIRA_LIST})


def client(http: FakeHttp) -> Jira:
    return Jira('bot@example.com', 'jt', http, cloud_id='cid', site=SITE)


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


class MetaJira(FakeJira):
    def __init__(self, screen: list[dict] | None = None, **kw):
        super().__init__(**kw)
        self.screen = SCREEN if screen is None else screen
        self.reads = 0

    def find_user(self, query: str) -> str | None:
        return {'oscar.green@example.com': '712020:abc', 'ruth.hale@example.com': '712020:ruth'}.get(query)

    def create_fields(self, project: str, issue_type: str) -> list[dict] | None:
        self.reads += 1
        return self.screen


def meta_of(jira: MetaJira):
    return JiraFields(jira).create_meta


def ticket(**over) -> dict:
    return {'type': 'ticket', 'key': KEY, 'target': KEY, 'requestedBy': 'oscar.green@example.com'} | over


def test_the_client_finds_the_task_type_and_pages_its_create_fields():
    # given
    http = jira_http(page=4)

    # when
    found = client(http).create_fields('OPS', 'task')

    # then
    assert [f['fieldId'] for f in found] == [f['fieldId'] for f in SCREEN]
    assert [c[1].split('?')[1] for c in http.hits(TASK_FIELDS)] == ['startAt=0&maxResults=50', 'startAt=4&maxResults=50', 'startAt=8&maxResults=50'], 'pages by what Jira gave until total is reached'
    assert client(http).create_fields('OPS', 'Epic') is None, 'an issue type the project lacks gives no screen'


def test_the_client_lists_every_jira_field():
    # given
    http = jira_http()

    # when
    found = client(http).fields()

    # then
    assert [f['id'] for f in found] == [f['id'] for f in JIRA_LIST] and len(http.hits(ALL_FIELDS)) == 1


def test_the_cache_reads_once_per_project_for_a_few_minutes_and_retries_a_failure_sooner():
    # given
    http, clock = jira_http(), Clock()
    cache = JiraFields(client(http), now=clock)

    # when
    first = cache.create_meta('OPS', 'Task')
    again = cache.create_meta('OPS', 'Task')
    clock.t += 301
    later = cache.create_meta('OPS', 'Task')
    http.routes[('GET', ALL_FIELDS)] = HttpError(500, ALL_FIELDS)
    down = cache.all()
    http.routes[('GET', ALL_FIELDS)] = JIRA_LIST
    still = cache.all()
    clock.t += 31
    back = cache.all()

    # then
    assert first == again == later and first['customfield_1']['options'] == SCOPES, 'a disabled option is not offered'
    assert len(http.hits(TASK_FIELDS)) == 2, 'one read, then one after the cache time'
    assert down is None and still is None and back == JIRA_LIST, 'a failure is held only a short time'


def test_the_dialog_view_lists_the_scope_and_the_required_fields_skopax_does_not_fill():
    # given
    meta = create_meta(SCREEN)

    # when
    view = dialog_view(meta, settings())

    # then
    assert view['readable'] and view['scope'] == {'name': 'Scope', 'options': SCOPES, 'required': True, 'multi': True}
    assert [(f['id'], f['kind']) for f in view['asked']] == [('customfield_7', 'option'), ('customfield_8', 'string'), ('customfield_9', 'number'), ('customfield_10', 'user')], \
        'summary, reporter and the scope are filled by Skopax; a field with a default or not required is not asked'
    assert view['asked'][0]['options'] == ['Payments', 'Search']
    assert view['unsupported'] == ['Due window']
    assert dialog_view(None, settings()) == {'readable': False, 'scope': None, 'asked': [], 'unsupported': []}


def test_the_configured_fields_are_never_asked_again():
    # given
    meta = create_meta(SCREEN)
    s = dataclasses.replace(settings(), jira_developer_field='customfield_10', jira_points_field='customfield_9')

    # when
    ids = [f['id'] for f in asked(meta, s)]

    # then
    assert ids == ['customfield_7', 'customfield_8', 'customfield_11']


@pytest.mark.parametrize('given, reason', [
    ({'customfield_99': 'x'}, 'customfield_99 is not a field of the OPS create screen that the dialog fills'),
    ({'summary': 'x'}, 'summary is not a field of the OPS create screen that the dialog fills'),
    ({'customfield_11': '2026-12-01'}, 'customfield_11 is not a field of the OPS create screen that the dialog fills'),
    ({'customfield_7': 'Billing', 'customfield_8': 'n', 'customfield_9': 1, 'customfield_10': '712020:ruth'}, 'Team: Billing is not an allowed value; it is one of Payments, Search'),
    ({'customfield_7': 'Search', 'customfield_8': 'x' * 256, 'customfield_9': 1, 'customfield_10': '712020:ruth'}, 'Release note is at most 255 characters'),
    ({'customfield_7': 'Search', 'customfield_8': 'n', 'customfield_9': 'high', 'customfield_10': '712020:ruth'}, 'Risk is a number'),
    ({'customfield_7': 'Search', 'customfield_9': 2, 'customfield_10': '712020:ruth'}, 'Jira needs Release note'),
    ({'customfield_7': 'Search', 'customfield_8': 'n', 'customfield_9': 2, 'customfield_10': 'nobody@example.com'}, 'no Jira account found for nobody@example.com in Reviewer'),
])
def test_extra_fields_are_checked_against_the_create_screen(given, reason):
    # given
    meta = create_meta(SCREEN)

    # when
    with pytest.raises(JiraRefused) as refused:
        given_fields(given, meta, settings(), 'OPS', MetaJira().find_user)

    # then
    assert str(refused.value) == reason


def test_valid_extra_fields_get_the_jira_shape():
    # given
    meta = create_meta(SCREEN)

    # when
    sent = given_fields({'customfield_7': 'Search', 'customfield_8': ' rollout ', 'customfield_9': 3, 'customfield_10': 'ruth.hale@example.com', 'customfield_13': ''}, meta, settings(), 'OPS', MetaJira().find_user)

    # then
    assert sent == {'customfield_7': {'value': 'Search'}, 'customfield_8': 'rollout', 'customfield_9': 3, 'customfield_10': {'accountId': '712020:ruth'}}


def test_extra_fields_without_a_readable_screen_are_refused():
    # when
    with pytest.raises(JiraRefused) as refused:
        given_fields({'customfield_7': 'Search'}, None, settings(), 'OPS', lambda who: None)

    # then
    assert str(refused.value).startswith('the OPS create screen could not be read from Jira')


def test_create_task_sends_the_picked_scope_and_no_sprint_when_unticked(store: Store):
    # given
    store.save_state(state(anomaly(KEY)), None)
    jira = MetaJira(screen=[option_field('customfield_1', 'Scope', SCOPES)])

    # when
    result = page_actions.apply(store, ticket(scope=['Browser', 'Phone'], sprint=False), jira, dataclasses.replace(settings(), jira_dev_scope='Server'), 'https://skopax.example.com', meta=meta_of(jira))
    fields = jira.created[0]

    # then
    assert fields['customfield_1'] == [{'value': 'Browser'}, {'value': 'Phone'}]
    assert 'customfield_3' not in fields, 'an unticked box sends no sprint'
    assert result == 'OPS-121 created · In progress · oscar.green@example.com · scope Browser, Phone'


def test_absent_scope_and_sprint_follow_the_settings_and_read_no_screen(store: Store):
    # given
    store.save_state(state(anomaly(KEY)), None)
    jira = MetaJira()
    off = dataclasses.replace(settings(), jira_dev_scope='Server', jira_sprint_default=False)

    # when
    page_actions.apply(store, ticket(), jira, dataclasses.replace(off, jira_sprint_default=True), 'https://skopax.example.com', meta=meta_of(jira))
    page_actions.apply(store, ticket(), jira, off, 'https://skopax.example.com', meta=meta_of(jira))

    # then
    on, plain = jira.created
    assert on['customfield_1'] == plain['customfield_1'] == [{'value': 'Server'}]
    assert on['customfield_3'] == 2604 and 'customfield_3' not in plain, 'jira.sprint-default decides when the request says nothing'
    assert jira.reads == 0, 'Slack and MCP tasks make no create-screen read'


def test_a_scope_outside_the_jira_options_is_refused_and_files_nothing(store: Store):
    # given
    store.save_state(state(anomaly(KEY)), None)
    jira = MetaJira(screen=[option_field('customfield_1', 'Scope', SCOPES)])

    # when
    with pytest.raises(page_actions.Refused) as refused:
        page_actions.apply(store, ticket(scope=['Mainframe']), jira, settings(), 'https://skopax.example.com', meta=meta_of(jira))

    # then
    assert str(refused.value) == 'Scope: Mainframe is not an allowed value; it is one of Server, Browser, Admin console, Phone, Tablet'
    assert jira.created == []


def test_an_empty_required_scope_is_refused_and_a_scope_without_options_goes_as_given(store: Store):
    # given
    store.save_state(state(anomaly(KEY)), None)
    known = MetaJira(screen=[option_field('customfield_1', 'Scope', SCOPES)])
    unread = MetaJira(screen=[])
    unread.create_fields = lambda project, issue_type: (_ for _ in ()).throw(SourceDown('Jira HTTP 503'))

    # when
    with pytest.raises(page_actions.Refused) as refused:
        page_actions.apply(store, ticket(scope=[]), known, settings(), 'https://skopax.example.com', meta=meta_of(known))
    page_actions.apply(store, ticket(scope=['Mainframe']), unread, settings(), 'https://skopax.example.com', meta=meta_of(unread))

    # then
    assert str(refused.value) == 'Scope is required: pick at least one'
    assert unread.created[0]['customfield_1'] == [{'value': 'Mainframe'}], 'without options the value goes as given and Jira judges it'


def test_create_task_sends_the_extra_required_fields(store: Store):
    # given
    store.save_state(state(anomaly(KEY)), None)
    jira = MetaJira()

    # when
    page_actions.apply(store, ticket(scope=['Browser'], fields={'customfield_7': 'Payments', 'customfield_8': 'note', 'customfield_9': 2, 'customfield_10': 'ruth.hale@example.com'}), jira, settings(),
                       'https://skopax.example.com', meta=meta_of(jira))

    # then
    fields = jira.created[0]
    assert (fields['customfield_7'], fields['customfield_8'], fields['customfield_9'], fields['customfield_10']) == ({'value': 'Payments'}, 'note', 2, {'accountId': '712020:ruth'})


@pytest.mark.parametrize('role, suggest', [('sprint', 'customfield_10020'), ('points', 'customfield_10200'), ('developer', 'customfield_10060'), ('scope', 'customfield_10100')])
def test_the_field_picker_lists_fields_by_name_and_suggests_one(role, suggest):
    # when
    found = field_choices(JIRA_LIST, role)

    # then
    assert found['suggest']['id'] == suggest
    assert found['fields'][0]['fits'] and [r['label'] for r in found['fields'] if r['id'] == 'customfield_10200'] == ['Story point estimate (customfield_10200)']
    assert {r['id'] for r in found['fields']} == {f['id'] for f in JIRA_LIST}, 'every field stays listed; fitting ones come first'


def test_no_developer_or_scope_suggestion_when_several_fields_fit():
    # given
    more = JIRA_LIST + [{'id': 'customfield_10070', 'name': 'Tester', 'custom': True, 'schema': {'type': 'user', 'custom': 'com.atlassian.jira.plugin.system.customfieldtypes:userpicker'}}]

    # when
    found = field_choices(more, 'developer')

    # then
    assert found['suggest'] is None and [r['id'] for r in found['fields'] if r['fits']] == ['customfield_10060', 'customfield_10070']


@pytest.mark.parametrize('key', ['jira.scope-field', 'jira.points-field', 'jira.developer-field', 'jira.sprint-field'])
def test_the_field_settings_take_a_field_id_only(key):
    # given
    t = next(t for t in registry() if t.key == key)

    # when
    kept = parse(t, 'customfield_10200', settings())
    with pytest.raises(Invalid):
        parse(t, 'Story points', settings())

    # then
    assert kept == 'customfield_10200' and parse(t, '', settings()) == ''


def test_the_sprint_default_is_a_bool_setting_on_by_default():
    # given
    t = next(t for t in registry() if t.key == 'jira.sprint-default')

    # then
    assert t.kind == 'bool' and settings().jira_sprint_default is True


def app_client(store: Store, http: FakeHttp, s=None) -> TestClient:
    c = TestClient(create_app(s or settings(), store, transport=http))
    c.app.state.instances.create('jira', 'main', VALUES, True, 'admin@example.com')
    return c


def test_the_create_fields_endpoint_serves_the_dialog_view_and_refuses_other_projects(store: Store):
    # given
    http = jira_http()

    # when
    with app_client(store, http) as c:
        got = c.get('/api/jira/create-fields?project=OPS', headers=AUTH)
        again = c.get('/api/jira/create-fields?project=ops', headers=AUTH)
        infra = c.get('/api/jira/create-fields?project=INFRA', headers=AUTH)
        anon = c.get('/api/jira/create-fields?project=OPS')

    # then
    assert got.status_code == 200 and got.json()['scope']['options'] == SCOPES and again.json() == got.json()
    assert len(http.hits(TASK_FIELDS)) == 1, 'cached per project'
    assert infra.status_code == 422 and anon.status_code in (401, 403)


def test_the_create_fields_endpoint_falls_back_when_jira_cannot_say(store: Store):
    # given
    http = jira_http()
    http.routes[('GET', TYPES)] = HttpError(403, TYPES)

    # when
    with app_client(store, http) as c:
        got = c.get('/api/jira/create-fields?project=WEB', headers=AUTH)

    # then
    assert got.status_code == 200 and got.json() == {'readable': False, 'scope': None, 'asked': [], 'unsupported': []}


def test_the_admin_field_list_is_admin_only_and_cached(store: Store):
    # given
    http = jira_http()

    # when
    with app_client(store, http) as c:
        got = c.get('/api/admin/jira/fields?role=points', headers=AUTH)
        c.get('/api/admin/jira/fields?role=sprint', headers=AUTH)
        bad = c.get('/api/admin/jira/fields?role=epic', headers=AUTH)

    # then
    assert got.status_code == 200 and got.json()['suggest'] == {'id': 'customfield_10200', 'label': 'Story point estimate (customfield_10200)'}
    assert len(http.hits(ALL_FIELDS)) == 1 and bad.status_code == 422


def test_the_page_action_carries_scope_sprint_and_fields_to_jira(store: Store):
    # given
    http = jira_http()
    http.routes[('POST', f'{GATEWAY}/rest/api/3/issue')] = {'key': 'OPS-300'}
    http.routes[('GET', f'{GATEWAY}/rest/api/3/user/search')] = []
    store.save_state(state(anomaly(KEY)), None)
    body = {'type': 'ticket', 'key': KEY, 'project': 'OPS', 'scope': ['Browser'], 'sprint': False,
            'fields': {'customfield_7': 'Search', 'customfield_8': 'note', 'customfield_9': 1, 'customfield_10': '712020:ruth'}}

    # when
    with app_client(store, http) as c:
        done = c.post('/api/actions', json=body, headers=AUTH).json()
        me = c.get('/api/me', headers=AUTH).json()['task']

    # then
    sent = http.hits(f'{GATEWAY}/rest/api/3/issue')[0][3]['fields']
    assert done['status'] == 'done' and done['result'].startswith('OPS-300 created') and ' · scope Browser' in done['result']
    assert sent['customfield_1'] == [{'value': 'Browser'}] and sent['customfield_7'] == {'value': 'Search'} and 'customfield_3' not in sent
    assert (me['scopeField'], me['scope'], me['sprintField'], me['sprint']) == (True, '', True, True)


@pytest.mark.parametrize('kind, sent', [('option', {'value': 'Server'}), ('options', [{'value': 'Server'}]), (None, [{'value': 'Server'}])])
def test_a_slack_or_mcp_task_sends_the_scope_in_the_shape_of_the_field(store: Store, kind, sent):
    # given
    store.save_state(state(anomaly(KEY)), None)
    jira = MetaJira()

    # when
    page_actions.apply(store, ticket(source='slack'), jira, dataclasses.replace(settings(), jira_dev_scope='Server'), 'https://skopax.example.com', meta=meta_of(jira), field_kind=lambda fid: kind)

    # then
    assert jira.created[0]['customfield_1'] == sent
    assert jira.reads == 0, 'the shape comes from the field list, not a create-screen read'


def test_the_field_kind_comes_from_the_cached_field_list_and_is_unknown_when_jira_cannot_say():
    # given
    http = jira_http()
    single = [dict(f, schema={'type': 'option'}) if f['id'] == 'customfield_10100' else f for f in JIRA_LIST]
    http.routes[('GET', ALL_FIELDS)] = single
    cache = JiraFields(client(http))
    down = JiraFields(client(FakeHttp({('GET', ALL_FIELDS): HttpError(403, ALL_FIELDS)})))

    # when
    kinds = (cache.field_kind('customfield_10100'), cache.field_kind('customfield_10060'), cache.field_kind('customfield_404'), down.field_kind('customfield_10100'))

    # then
    assert kinds == ('option', 'user', None, None)
    assert len(http.hits(ALL_FIELDS)) == 1


def test_a_screen_without_the_scope_field_sends_no_scope(store: Store):
    # given
    store.save_state(state(anomaly(KEY)), None)
    jira = MetaJira(screen=[{'fieldId': 'summary', 'name': 'Summary', 'required': True, 'schema': {'type': 'string'}}])

    # when
    result = page_actions.apply(store, ticket(scope=['Server']), jira, settings(), 'https://skopax.example.com', meta=meta_of(jira))

    # then
    assert 'customfield_1' not in jira.created[0] and not result.endswith('scope Server')
    assert dialog_view(create_meta(jira.screen), settings())['scope'] is None


def test_long_spaced_and_late_options_are_accepted_and_sent_as_jira_wrote_them():
    # given
    many = [f'Area {n}' for n in range(300)] + ['Tablet ', 'x' * 400]
    meta = create_meta([option_field('customfield_7', 'Team', many, multi=False)])

    # when
    sent = [given_fields({'customfield_7': v}, meta, settings(), 'OPS', lambda who: None)['customfield_7'] for v in ('Area 250', 'Tablet', 'x' * 400)]

    # then
    assert sent == [{'value': 'Area 250'}, {'value': 'Tablet '}, {'value': 'x' * 400}]


def test_a_plain_word_is_no_account_id_but_a_real_one_is():
    # given
    meta = create_meta(SCREEN)
    base = {'customfield_7': 'Search', 'customfield_8': 'n', 'customfield_9': 1}

    # when
    with pytest.raises(JiraRefused) as plain:
        given_fields(base | {'customfield_10': 'oscargreen'}, meta, settings(), 'OPS', lambda who: None)

    # then
    assert str(plain.value) == 'Reviewer takes an e-mail or a Jira account id'
    assert given_fields(base | {'customfield_10': '5b10a2844c20165700ede21f'}, meta, settings(), 'OPS', lambda who: None)['customfield_10'] == {'accountId': '5b10a2844c20165700ede21f'}


def test_a_refused_people_read_in_create_task_says_so(store: Store):
    # given
    from skopax.contract import Denied
    store.save_state(state(anomaly(KEY)), None)
    jira = MetaJira()
    known = jira.find_user

    def find_user(query):
        if query == 'ruth.hale@example.com':
            raise Denied('HTTP 403, the token lacks the Browse users and groups permission', 'users', 'Browse users')
        return known(query)
    jira.find_user = find_user

    # when
    with pytest.raises(page_actions.Refused) as refused:
        page_actions.apply(store, ticket(scope=['Server'], fields={'customfield_7': 'Search', 'customfield_8': 'n', 'customfield_9': 1, 'customfield_10': 'ruth.hale@example.com'}),
                           jira, settings(), 'https://skopax.example.com', meta=meta_of(jira))

    # then
    assert str(refused.value) == 'HTTP 403, the token lacks the Browse users and groups permission' and jira.created == []


@pytest.mark.parametrize('value', [True, ['a'] * 51, 'x' * 4001, ['y' * 1001]])
def test_the_ticket_action_refuses_a_bool_or_an_oversized_field_value(store: Store, value):
    # given
    http = jira_http()

    # when
    with app_client(store, http) as c:
        got = c.post('/api/actions', json={'type': 'ticket', 'key': KEY, 'project': 'OPS', 'fields': {'customfield_9': value}}, headers=AUTH)

    # then
    assert got.status_code == 422


@pytest.mark.parametrize('key, value', [('jira.points-field', 'summary'), ('jira.developer-field', 'reporter'), ('jira.sprint-field', 'parent')])
def test_a_role_field_cannot_be_a_field_skopax_fills(key, value):
    # given
    t = next(t for t in registry() if t.key == key)

    # when
    with pytest.raises(Invalid):
        parse(t, value, settings())


def test_a_missing_issue_type_is_retried_soon_like_a_failure():
    # given
    clock = Clock()
    jira = MetaJira()
    jira.create_fields = lambda project, issue_type: (setattr(jira, 'reads', jira.reads + 1), None)[1]
    cache = JiraFields(jira, now=clock)

    # when
    cache.create_meta('OPS', 'Task')
    clock.t += 31
    cache.create_meta('OPS', 'Task')

    # then
    assert jira.reads == 2


@pytest.mark.parametrize('lang', ['de', 'es', 'fr', 'tr'])
def test_the_new_dialog_and_admin_texts_are_translated(lang):
    # given
    import json
    from pathlib import Path
    pack = json.loads((Path(__file__).parent.parent / 'skopax' / 'locale' / lang / 'page.json').read_text(encoding='utf-8'))['page']
    texts = ['Add to the current sprint', 'Values separated by commas', 'Jira needs {0}, which the dialog cannot fill; set a default in Jira', 'Pick the {0} of this task.', 'Jira needs {0}.',
             'E-mail of the person', 'Jira search failed; type the e-mail', 'List Jira fields', 'List again', 'Suggested: {0}', 'Use it', 'Not a field Jira lists: check the id',
             'The Jira fields could not be read; type the field id', 'Scope field', 'Story points field', 'Sprint field', 'Developer field', 'Fields']

    # when
    missing = [t for t in texts if not pack.get(t) or pack[t] == t]

    # then
    assert missing == []


def test_an_empty_scope_from_the_dialog_sends_no_scope_even_with_a_default_set(store: Store):
    # given
    store.save_state(state(anomaly(KEY)), None)
    jira = MetaJira(screen=[{'fieldId': 'summary', 'name': 'Summary', 'required': True, 'schema': {'type': 'string'}}])

    # when
    result = page_actions.apply(store, ticket(scope=[]), jira, dataclasses.replace(settings(), jira_dev_scope='Server'), 'https://skopax.example.com', meta=meta_of(jira))

    # then
    assert 'customfield_1' not in jira.created[0] and not result.endswith('scope Server')
