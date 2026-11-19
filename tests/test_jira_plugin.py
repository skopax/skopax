import base64
import dataclasses
import datetime as dt
import json

from pathlib import Path
import pytest
from fastapi.testclient import TestClient

from skopax import actions as page_actions, chatui, contract, messages
from skopax.app import create_app
from skopax.config import ConfigurationError, load_settings, release_problem
from skopax.chatui.text import configure, linkify
from skopax.enrich import JiraRefused
from skopax.investigator.tools import Effects, Sources, Toolbox, allowed_link_hosts
from skopax.jiralinks import ticket_pattern, ticket_projects
from skopax.kube import Kube
from skopax.messages import MESSAGES, msg
from skopax.plugins import jira
from skopax.plugins.jira.client import CREATE_MISSING, JIRA_GATEWAY, Jira, denied_text
from skopax.plugintools import PluginTools
from skopax.postdeploy import RELEASE_NAME, PostDeploy, release_name
from skopax.registry import BUILTIN, Registry, describe
from skopax.sealer import master_key
from skopax.remote import HttpError, Unreachable
from skopax.runtime import Runtime
from skopax.settingsources import boot
from skopax.slack import release_pattern, report_message
from skopax.store import Store
from tests.conftest import AUTH, anomaly, settings, state
from tests.fake_http import FakeHttp
from tests.fake_slack import fake_slack
from tests.plugin_kit import JIRA_SITE, JIRA_VALUES, add_instance, jira_runtime, registry_with
from tests.test_plugin_hooks import DEMO, GOOD
from tests.test_auth import sso_settings
from tests.test_postdeploy import FakeJira, NOW as DEPLOY_NOW, at, only, prom, settled_action
from tests.test_slack_clicks import drafted_ask, ephemerals, payload
from tests.test_slackevents import KEY, card_with_post

NOW = 1_792_224_000.0
USER = 'alice@example.com'
SITE = 'https://example.atlassian.net'
TENANT = f'{SITE}/_edge/tenant_info'
GATEWAY = f'{JIRA_GATEWAY}/cid'
MYSELF = f'{GATEWAY}/rest/api/3/myself'
SEARCH = f'{GATEWAY}/rest/api/3/search/jql'
USERS = f'{GATEWAY}/rest/api/3/user/search'
ISSUE = f'{GATEWAY}/rest/api/3/issue/OPS-1'
CREATE = f'{GATEWAY}/rest/api/3/issue'
VALUES = {'url': SITE, 'email': 'bot@example.com', 'api-token': 'jt', 'cloud-id': ''}
OLD_FORM = '{project} {dd}.{mm}.{yyyy} Release'
JIRA_TOOLS = ['jira_issue', 'jira_search', 'propose_ticket']


def basic(call) -> str:
    return base64.b64decode(call[2]['Authorization'].split(' ', 1)[1]).decode()


def slack_ask(store: Store, runtime: Runtime) -> Toolbox:
    effects = Effects(lambda key, by: {}, lambda key, text, by, *_, notice=None: {})
    return Toolbox(Sources(gitlab=None, jira=runtime.proxy('jira'), plugin_tools=PluginTools(runtime)), store, effects, [KEY], 'live', NOW, 'https://skopax.example.com', ask=True,
                   read_only=True, settings=settings())


def jira_names(b: Toolbox) -> list[str]:
    return sorted(d['name'] for d in b.definitions() if d['name'] in JIRA_TOOLS)


def test_jira_identity_and_manifest():
    # given
    registry = Registry.of(jira.PLUGIN)

    # when
    loaded = registry.get('jira')
    manifest = describe(jira.PLUGIN, icon=True)

    # then
    assert json.loads(json.dumps(manifest)) == manifest
    assert (loaded.state, loaded.reason) == ('ok', None) and 'skopax.plugins.jira' in BUILTIN
    assert Registry.load({}, entry_points=False).get('jira').state == 'ok'
    assert (manifest['name'], manifest['version'], manifest['category'], manifest['author'], manifest['licence'], manifest['core'], manifest['edition'], manifest['maxInstances']) == (
        'Jira', '0.1.0', 'tickets', 'Skopax', 'Apache-2.0', '>=0.1,<0.4', 'community', 1)
    assert manifest['capabilities'] == {'hosts': ['@url', 'api.atlassian.com'], 'secrets': ['api-token'], 'agentReads': False} and manifest['uses'] == []
    assert [(f['key'], f['kind'], f['required'], f['ui']) for f in manifest['fields']] == [('url', 'url', True, True), ('email', 'text', True, False), ('api-token', 'secret', True, False),
                                                                                           ('cloud-id', 'text', False, False)]
    assert jira.PLUGIN.tools == () and [k.id for k in jira.PLUGIN.knowledge] == ['jira'] and manifest['checks'] == []
    assert registry.icon('jira').startswith('<svg')


def test_jira_connection_test_reports_ok_and_failures(store: Store):
    # given
    reads = {('POST', SEARCH): {'issues': []}, ('GET', USERS): []}
    good = FakeHttp({('GET', TENANT): {'cloudId': 'cid'}, ('GET', MYSELF): {'accountId': 'a1', 'displayName': 'Bot'}} | reads)
    denied = FakeHttp({('GET', TENANT): {'cloudId': 'cid'}, ('GET', MYSELF): HttpError(401, MYSELF)})
    down = FakeHttp({('GET', TENANT): Unreachable('x')})
    no_cloud = FakeHttp({('GET', TENANT): {'other': 1}})
    pinned = FakeHttp({('GET', MYSELF): {'accountId': 'a1'}} | reads)
    no_issues = FakeHttp({('GET', MYSELF): {'accountId': 'a1'}, ('POST', SEARCH): HttpError(403, SEARCH), ('GET', USERS): []})
    no_users = FakeHttp({('GET', MYSELF): {'accountId': 'a1'}, ('POST', SEARCH): {'issues': []}, ('GET', USERS): HttpError(401, USERS)})
    budgets = []

    def check(http, **values):
        def timed(method, url, headers, data, timeout):
            budgets.append(timeout)
            return http(method, url, headers, data, timeout)

        rt = jira_runtime(store, timed, **(VALUES | values))
        return jira.test(rt.context(jira.PLUGIN, rt.instances.get('jira', 'main'), NOW))

    # when
    ok, refused, unreachable, cloudless, known = check(good), check(denied), check(down), check(no_cloud), check(pinned, **{'cloud-id': 'cid'})
    issues_refused, users_refused = check(no_issues, **{'cloud-id': 'cid'}), check(no_users, **{'cloud-id': 'cid'})

    # then
    assert (ok.ok, ok.summary) == (True, msg('jira.test_ok', site='example.atlassian.net')) and ok.summary == 'Jira at example.atlassian.net: the token works'
    assert (refused.ok, refused.summary) == (False, msg('connection.rejected', status=401))
    assert (unreachable.ok, unreachable.summary) == (False, msg('connection.unreachable'))
    assert cloudless.ok is False and 'cloudId' in cloudless.summary and len(no_cloud.calls) == 1
    assert known.ok is True and [c[1].split('?')[0] for c in pinned.calls] == [MYSELF, SEARCH, USERS]
    assert [c[1].split('?')[0] for c in good.calls] == [TENANT, MYSELF, SEARCH, USERS] and basic(good.calls[1]) == 'bot@example.com:jt' and 'Authorization' not in good.calls[0][2]
    assert issues_refused.ok is False and issues_refused.summary == denied_text('issues', 403) and 'Browse projects' in issues_refused.summary
    assert users_refused.ok is False and users_refused.summary == denied_text('users', 401) and 'Browse users and groups' in users_refused.summary
    assert budgets and all(0 < b <= 14 for b in budgets), 'tenant_info and the gateway read share one budget inside the 15 s Test'


def test_jira_defaults_are_neutral():
    # when
    defaults = {f.key: f.default for f in jira.PLUGIN.fields}

    # then
    assert defaults == {'url': '', 'email': '', 'api-token': '', 'cloud-id': ''}
    assert jira.PLUGIN.settings == () and jira.PLUGIN.rules == () and jira.PLUGIN.checks == ()


def test_env_defines_the_main_instance(store: Store, tmp_path):
    # given
    env = {'JIRA_URL': SITE + '/', 'JIRA_EMAIL': 'bot@example.com', 'JIRA_API_TOKEN': 'jt-env', 'JIRA_CLOUD_ID': 'cid', 'SKOPAX_BASE_URL': 'https://skopax.example.com',
           'SKOPAX_TOKEN': AUTH['Authorization'].split(' ', 1)[1]}

    # when
    b = boot(env)
    bare = boot({'JIRA_URL': SITE, 'JIRA_EMAIL': 'bot@example.com', 'JIRA_API_TOKEN': 'jt-env'})
    no_token = boot({'JIRA_URL': SITE, 'JIRA_EMAIL': 'bot@example.com'})
    no_url = boot({'JIRA_EMAIL': 'bot@example.com', 'JIRA_API_TOKEN': 'jt-env'})
    no_email = boot({'JIRA_URL': SITE, 'JIRA_API_TOKEN': 'jt-env'})
    with TestClient(create_app(b.settings, store, registry=b.registry, sources=b.source_set(), kube=Kube(root=tmp_path))) as c:
        main = c.app.state.instances.get('jira', 'main')
        client = c.app.state.runtime.client('jira')
        jobs = [j.name for j in c.app.state.scheduler.jobs]
        me = c.get('/api/me', headers=AUTH).json()

    # then
    def rows(found):
        return [r for r in found.sources[0].instances if r['plugin'] == 'jira']

    assert rows(b) == [{'plugin': 'jira', 'name': 'main', 'primary': 'true', 'url': SITE, 'email': 'bot@example.com', 'api-token': 'jt-env', 'cloud-id': 'cid'}]
    assert rows(bare)[0]['cloud-id'] == ''
    assert rows(no_token) == [] and rows(no_url) == [] and rows(no_email) == [], 'Jira stays off, as before, until the site, the email and the token are all set'
    assert (main.primary, main.enabled, main.problem, main.values['url']) == (True, True, None, SITE)
    assert (client.site, client.email, client.token, client.cloud_id) == (SITE, 'bot@example.com', 'jt-env', 'cid')
    assert 'jira-links' in jobs
    assert (me['jira'], me['jiraUrl']) == (True, SITE)
    assert not any(hasattr(b.settings, f) for f in ('jira_url', 'jira_email', 'jira_api_token', 'jira_cloud_id'))


def test_jira_added_in_admin_starts_its_jobs_without_restart(store: Store):
    # given
    http = FakeHttp({('GET', TENANT): {'cloudId': 'cid'}, ('POST', SEARCH): {'issues': []}})

    # when
    with TestClient(create_app(settings(), store, transport=http)) as c:
        before = [j.name for j in c.app.state.scheduler.jobs]
        added = c.post('/api/admin/plugins/jira/instances', json={'name': 'main', 'values': VALUES, 'enabled': True}, headers=AUTH)
        ran = c.app.state.scheduler.run_due()

    # then
    assert 'jira-links' not in before
    assert added.status_code == 201, added.text
    assert 'jira-links' in ran
    assert http.hits(SEARCH) and 'skopax.example.com' in http.hits(SEARCH)[0][3]['jql']


def test_create_task_with_jira_off_is_refused_cleanly(store: Store):
    # given
    card_with_post(store)
    act = drafted_ask(store)
    slack, _, api = fake_slack(emails={'U1': 'adam.baker@example.com'})
    http = FakeHttp({('POST', CREATE): {'key': 'OPS-140'}})
    refusal = msg('tickets.jira_off')

    # when
    with TestClient(create_app(settings(), store, slack=slack, transport=http, operators_lookup=lambda email: email == 'adam.baker@example.com')) as c:
        c.app.state.instances.create('jira', 'main', VALUES | {'cloud-id': 'cid'}, True, USER)
        c.app.state.instances.set_enabled('jira', 'main', False, USER)
        c.app.state.clicks.on = lambda: True
        page = c.post('/api/actions', json={'type': 'ticket', 'key': KEY, 'title': 'checkout-service retry storm'}, headers=AUTH)
        clicked = c.app.state.clicks.handle(payload(act['id']))
        me = c.get('/api/me', headers=AUTH).json()

    # then
    assert page.status_code == 201 and (page.json()['status'], page.json()['result']) == ('failed', refusal)
    assert clicked == 'refused' and ephemerals(api) == [msg('slackevents.click.refused', reason=refusal)]
    assert 'Admin \u203a Integrations' in refusal and 'jira.url' not in refusal
    assert 'triage' not in store.anomaly(KEY) and 'ticket' not in store.get_action(act['id']) and http.calls == []
    assert (me['jira'], me['jiraUrl']) == (False, '')


def test_an_admin_set_token_is_used_without_restart(store: Store):
    # given
    http = FakeHttp({('POST', SEARCH): {'issues': [{'key': 'OPS-1', 'fields': {'summary': 'cart', 'status': {'name': 'Open'}}}]}})

    # when
    with TestClient(create_app(settings(), store, transport=http)) as c:
        c.app.state.instances.create('jira', 'main', VALUES | {'cloud-id': 'cid', 'api-token': 'old'}, True, USER)
        first = c.get('/api/jira/search', params={'q': 'cart'}, headers=AUTH)
        c.app.state.instances.update('jira', 'main', {'api-token': 'new'}, USER)
        c.app.state.runtime.refresh()
        second = c.get('/api/jira/search', params={'q': 'cart'}, headers=AUTH)

    # then
    assert first.status_code == second.status_code == 200 and first.json()[0]['key'] == 'OPS-1'
    assert [basic(call) for call in http.calls] == ['bot@example.com:old', 'bot@example.com:new']


def test_jira_tools_and_propose_ticket_follow_the_switch(store: Store):
    # given
    store.save_state(state(anomaly(KEY)), None)
    http = FakeHttp({('POST', SEARCH): {'issues': [{'key': 'OPS-1', 'fields': {'summary': 'cart'}}]}})
    runtime = jira_runtime(store, http, named=None)

    # when
    before = jira_names(slack_ask(store, runtime))
    runtime.instances.create('jira', 'main', VALUES | {'cloud-id': 'cid'}, True, USER)
    serving = slack_ask(store, runtime)
    shown = jira_names(serving)
    found = serving.call('jira_search', {'jql': 'project = OPS'})
    runtime.instances.set_enabled('jira', 'main', False, USER)
    off = slack_ask(store, runtime)
    after = jira_names(off)
    refused = off.call('jira_search', {'jql': 'project = OPS'})

    # then
    assert before == [] and after == []
    assert shown == JIRA_TOOLS
    assert found[1] is False and 'OPS-1' in found[0]
    assert refused == ('jira_search is off', True)


def test_release_names_follow_the_setting(store: Store):
    # given
    day = dt.datetime(2026, 11, 14, 9, 30)
    releases = {'issues': [{'key': 'SHOP-1', 'inRelease': True}], 'rows': []}
    store.add_deploys([{'ns': 'shop', 'svc': 'checkout-service', 'fromImage': 'aaaaaaaa', 'toImage': 'bbbbbbbb', 'at': at(1800)}])
    settled_action(store, 1800, 1800)
    named = {'template': RELEASE_NAME}
    check = PostDeploy(store, only(prom({})), FakeJira([]), clock=lambda: DEPLOY_NOW, projects=('SHOP',), release_template=lambda: named['template'])

    # when
    default, old = release_name(RELEASE_NAME, 'SHOP', day), release_name(OLD_FORM, 'SHOP', day)
    matched = [release_pattern(t).match(n) for t, n in ((RELEASE_NAME, default), (OLD_FORM, old))]
    first = check.run_once(DEPLOY_NOW)
    named['template'] = OLD_FORM
    settled_action(store, 1800, 1800)
    second = check.run_once(DEPLOY_NOW)
    short = [report_message(releases | {'releases': [release_name(t, p, day) for p in ('SHOP', 'WEB')]}, 'https://w/#r=1', 'token', release_template=t)['text'] for t in (RELEASE_NAME, OLD_FORM)]
    with TestClient(create_app(settings(), store)) as c:
        refused = [c.put('/api/admin/settings', json={'key': 'jira.release-name', 'value': v}, headers=AUTH)
                   for v in ('{yyyy}-{mm}-{dd}', '{project} {yyyy}-{mm}', '{project} {yyyy}-{dd}', '{project} {dd}.{mm}.{dd}', '{project} {day}.{dd}.{mm}')]
        saved = c.put('/api/admin/settings', json={'key': 'jira.release-name', 'value': OLD_FORM}, headers=AUTH)

    # then
    assert (default, old) == ('SHOP 2026-11-14', 'SHOP 14.11.2026 Release')
    assert all(m is not None and (m['project'], m['dd'], m['mm']) == ('SHOP', '14', '11') for m in matched)
    assert release_pattern(RELEASE_NAME).match('SHOP 14.11.2026 Release') is None and release_pattern(OLD_FORM).match('SHOP 2026-11-14') is None
    local = dt.datetime.fromtimestamp(DEPLOY_NOW - 1800, dt.timezone.utc)
    assert first['releases'] == [release_name(RELEASE_NAME, 'SHOP', local)] and second['releases'] == [release_name(OLD_FORM, 'SHOP', local)]
    assert short == [':large_green_circle: *SHOP + WEB 11-14 release* \u00b7 ALL CLEAR \u00b7 0 services \u00b7 clean', ':large_green_circle: *SHOP + WEB 14.11 release* \u00b7 ALL CLEAR \u00b7 0 services \u00b7 clean']
    assert [r.status_code for r in refused] == [422] * 5 and all(r.json()['detail'] for r in refused)
    assert saved.status_code == 200 and saved.json()['item']['value'] == OLD_FORM


def test_the_ticket_key_pattern_still_comes_from_the_configured_projects(store: Store):
    # given
    s = dataclasses.replace(sso_settings(), chat_ui=True)

    # when
    try:
        with TestClient(create_app(s, store, transport=FakeHttp()), base_url='https://testserver') as c:
            c.app.state.instances.create('jira', 'main', VALUES | {'cloud-id': 'cid'}, True, USER)
            projects = c.get('/api/me', headers=AUTH).json()['task']['projects']
            before = linkify('OPS-12 and ZZZ-9')
            c.put('/api/admin/settings', json={'key': 'jira.projects', 'value': 'OPS,ZZZ'}, headers=AUTH)
            after = linkify('OPS-12 and ZZZ-9')
    finally:
        configure('', ())

    # then
    assert projects == ['OPS', 'WEB', 'APP', 'MOB']
    assert ticket_projects(settings()) == ('OPS', 'WEB', 'APP', 'MOB', 'INFRA') and ticket_pattern(ticket_projects(settings())).fullmatch('ZZZ-9') is None
    assert before == f'[OPS-12]({SITE}/browse/OPS-12) and ZZZ-9'
    assert after == f'[OPS-12]({SITE}/browse/OPS-12) and [ZZZ-9]({SITE}/browse/ZZZ-9)'


def test_the_chat_links_jira_keys_from_the_instance_without_building_a_client(store: Store, monkeypatch):
    # given
    built = []
    client = Runtime.client

    def watched(self, plugin_id, name=None):
        if plugin_id == 'jira':
            built.append(plugin_id)
        return client(self, plugin_id, name)

    monkeypatch.setattr(Runtime, 'client', watched)
    s = dataclasses.replace(sso_settings(), chat_ui=True)

    # when
    try:
        with TestClient(create_app(s, store, transport=FakeHttp()), base_url='https://testserver') as c:
            none = linkify('see OPS-12')
            c.app.state.instances.create('jira', 'main', VALUES | {'url': JIRA_SITE + '/'}, True, USER)
            served = chatui.text.markdown_of('see OPS-12')
            c.app.state.instances.set_enabled('jira', 'main', False, USER)
            off = linkify('see OPS-12')
    finally:
        configure('', ())

    # then
    assert none == 'see OPS-12' and off == 'see OPS-12'
    assert served == f'see [OPS-12]({JIRA_SITE}/browse/OPS-12)'
    assert built == [], 'the chat renders links inside async handlers, where building a client would block the loop'


def test_a_refusal_from_the_plugin_client_fails_the_page_action_with_its_text(store: Store):
    # given
    store.save_state(state(anomaly('log|checkout-service|ERROR|boom')), None)
    http = FakeHttp({('POST', CREATE): HttpError(400, CREATE), ('GET', f'{GATEWAY}/rest/api/3/user/search'): [], ('POST', SEARCH): {'issues': []}})
    client = Jira('bot@example.com', 'jt', http, cloud_id='cid', site=SITE)

    # when
    with pytest.raises(contract.Refused) as raw:
        client.create_issue({'summary': 'x'})
    try:
        page_actions.apply(store, {'type': 'ticket', 'key': 'log|checkout-service|ERROR|boom', 'target': 'log|checkout-service|ERROR|boom', 'requestedBy': 'x'}, client, settings())
        raised = None
    except page_actions.Refused as e:
        raised = str(e)

    # then
    assert not isinstance(raw.value, JiraRefused) and raised == raw.value.text
    assert raised.startswith('Jira answered HTTP 400 to the create')
    assert 'triage' not in store.anomaly('log|checkout-service|ERROR|boom')


def test_a_jira_link_in_a_slack_draft_needs_the_serving_instance(store: Store):
    # given
    store.save_state(state(anomaly(KEY, svc='checkout-service'), anomaly('log|b|ERROR|y', svc='orders-service')), None)
    runtime = jira_runtime(store, FakeHttp(), url=SITE)
    effects = Effects(lambda key, by: {}, lambda key, text, by, *_, notice=None: {})

    def draft(key: str):
        b = Toolbox(Sources(gitlab=None, jira=runtime.proxy('jira'), plugin_tools=PluginTools(runtime), link_hosts=lambda: allowed_link_hosts(settings(), runtime.ui_hosts())),
                    store, effects, [key], 'live', NOW, 'https://skopax.example.com', settings=settings())
        return b.call('slack_draft', {'key': key, 'text': f'Tracked in {SITE}/browse/OPS-12'})

    # when
    served = draft(KEY)
    runtime.instances.set_enabled('jira', 'main', False, USER)
    off = draft('log|b|ERROR|y')

    # then
    assert served[1] is False, served[0]
    assert off[1] is True and 'example.atlassian.net is not one of them' in off[0]


def test_the_jira_off_refusal_is_stored_in_english_and_told_in_the_outbound_language(store: Store):
    # given
    card_with_post(store)
    act = drafted_ask(store)
    slack, _, api = fake_slack(emails={'U1': 'adam.baker@example.com'})
    german = 'Jira ist hier nicht eingerichtet: ein Admin legt es unter Admin ' + chr(0x203a) + ' Integrationen an'
    pack = DEMO.locales[0]
    speaking = dataclasses.replace(DEMO, locales=(dataclasses.replace(pack, messages={**pack.messages, 'tickets.jira_off': german}),))
    add_instance(store, 'demo', 'main', sealer_key=master_key(store.db, settings()), **GOOD)

    # when
    with TestClient(create_app(dataclasses.replace(settings(), language='de'), store, slack=slack, transport=FakeHttp(), registry=registry_with(speaking),
                               operators_lookup=lambda email: email == 'adam.baker@example.com')) as c:
        c.app.state.clicks.on = lambda: True
        page = c.post('/api/actions', json={'type': 'ticket', 'key': KEY, 'title': 'checkout-service retry storm'}, headers=AUTH).json()
        clicked = c.app.state.clicks.handle(payload(act['id']))
        told = ephemerals(api)
        said = msg('tickets.jira_off')
    stored = [a['result'] for a in store.list_actions('failed', 5) if a['type'] == 'ticket']

    # then
    english = msg('actions.jira_off')
    assert said == german and english == MESSAGES['tickets.jira_off'] and 'actions' not in messages.OUTBOUND
    assert (page['status'], page['result']) == ('failed', english)
    assert clicked == 'refused' and told == [msg('slackevents.click.refused', reason=german)]
    assert stored == [english, english], 'the page result and the failed Slack click are stored in English'


def test_a_stale_cloud_id_fails_create_task_with_a_readable_text_not_a_500(store: Store):
    # given
    card_with_post(store)
    act = drafted_ask(store)
    slack, _, api = fake_slack(emails={'U1': 'adam.baker@example.com'})
    http = FakeHttp()

    # when
    with TestClient(create_app(settings(), store, slack=slack, transport=http, operators_lookup=lambda email: email == 'adam.baker@example.com')) as c:
        c.app.state.instances.create('jira', 'main', VALUES | {'cloud-id': 'stale'}, True, USER)
        c.app.state.clicks.on = lambda: True
        page = c.post('/api/actions', json={'type': 'ticket', 'key': KEY, 'title': 'checkout-service retry storm'}, headers=AUTH)
        clicked = c.app.state.clicks.handle(payload(act['id']))

    # then
    assert page.status_code == 201 and (page.json()['status'], page.json()['result']) == ('failed', CREATE_MISSING)
    assert 'cloud id' in CREATE_MISSING
    assert clicked == 'refused' and ephemerals(api) == [msg('slackevents.click.refused', reason=CREATE_MISSING)]
    assert 'triage' not in store.anomaly(KEY) and any(c[1].split('?')[0].endswith('/search/jql') for c in http.calls)


def test_a_bad_release_name_from_the_environment_stops_the_boot_with_the_admin_text(store: Store):
    # when
    with pytest.raises(ConfigurationError) as refused:
        load_settings(env={'JIRA_RELEASE_NAME': '{project} {yyyy}'})
    with TestClient(create_app(settings(), store)) as c:
        admin = c.put('/api/admin/settings', json={'key': 'jira.release-name', 'value': '{project} {yyyy}'}, headers=AUTH)
    kept = load_settings(env={'JIRA_RELEASE_NAME': OLD_FORM}).jira_release_name

    # then
    assert str(refused.value) == admin.json()['detail'] == release_problem('{project} {yyyy}') == msg('jira.release_name.missing', placeholder='{dd}')
    assert admin.status_code == 422 and kept == OLD_FORM


def test_a_refused_issue_search_keeps_the_cards_and_opens_one_access_card(store: Store):
    # given
    tracked = 'log|orders-service|ERROR|tracked'
    store.save_state(state(anomaly(tracked, triage='progress', ticket='OPS-1', owner='nina.white', firstSeen='2026-10-15T08:00:00Z')), None)
    answer = {'search': HttpError(403, SEARCH)}
    http = FakeHttp({('POST', SEARCH): lambda query, body: answer['search']})

    # when
    with TestClient(create_app(settings(), store, transport=http)) as c:
        c.app.state.instances.create('jira', 'main', VALUES | {'cloud-id': 'cid'}, True, USER)
        job = next(j for j in c.app.state.scheduler.jobs if j.name == 'jira-links')
        job.run(NOW)
        refused = store.load()
        answer['search'] = {'issues': []}
        job.run(NOW + 900)
        healed = store.load()

    # then
    def access(found):
        return [a for a in found['anomalies'] if a.get('ranKey') == 'jira-links:access:issues']

    card = access(refused)[0]
    assert len(access(refused)) == 1 and (card['sev'], card['status'], card['src']) == ('warn', 'open', 'jira-links')
    assert card['label'] == msg('cards.no_access', plugin='Jira', part='issues', permission=jira.client.PERMISSIONS['issues']) and 'Browse projects' in card['label']
    assert refused['sources']['jira-links'] == 'ok \u00b7 no access to issues'
    kept = next(a for a in refused['anomalies'] if a['key'] == tracked)
    assert (kept['status'], kept['triage'], kept['ticket']) == ('open', 'progress', 'OPS-1'), 'a card the pass could not read stays as it was'
    assert access(healed)[0]['status'] == 'cleared' and healed['sources']['jira-links'] == 'ok'


def test_a_refused_jira_read_names_the_missing_permission(store: Store):
    # given
    store.save_state(state(anomaly(KEY)), None)
    http = FakeHttp({('POST', SEARCH): HttpError(403, SEARCH), ('GET', ISSUE): {'key': 'OPS-1', 'fields': {}}, ('GET', f'{ISSUE}/comment'): HttpError(403, ISSUE)})
    b = slack_ask(store, jira_runtime(store, http, url=SITE))

    # when
    searched = b.call('jira_search', {'jql': 'project = OPS'})
    read = b.call('jira_issue', {'key': 'OPS-1'})

    # then
    assert searched == (denied_text('issues', 403), True) and 'Browse projects' in searched[0]
    assert read[1] is False and denied_text('issues', 403) in read[0]


def test_create_task_and_the_page_searches_name_a_refused_read_instead_of_none(store: Store):
    # given
    store.save_state(state(anomaly(KEY)), None)
    http = FakeHttp({('GET', USERS): HttpError(403, USERS), ('POST', SEARCH): HttpError(403, SEARCH), ('POST', f'{GATEWAY}/rest/api/3/issue'): {'key': 'OPS-140'}})
    client = Jira('bot@example.com', 'jt', http, cloud_id='cid', site=SITE)

    # when
    result = page_actions.apply(store, {'type': 'ticket', 'key': KEY, 'target': KEY, 'requestedBy': 'oscar.green@example.com'}, client, settings())
    with TestClient(create_app(settings(), store, transport=http)) as c:
        c.app.state.instances.create('jira', 'main', VALUES | {'cloud-id': 'cid'}, True, USER)
        found = c.get('/api/jira/search', params={'q': 'cart'}, headers=AUTH)
        named = c.get('/api/people', params={'q': 'nina'}, headers=AUTH)

    # then
    assert result.startswith('OPS-140 created') and denied_text('users', 403) in result and denied_text('issues', 403) in result
    assert 'no Jira account found' not in result
    assert (found.status_code, found.json()['detail']) == (502, denied_text('issues', 403))
    assert (named.status_code, named.json()['detail']) == (502, denied_text('users', 403))


def test_a_release_name_with_a_quote_or_a_backslash_is_refused_so_the_jql_never_breaks():
    # when
    found = [release_problem(t) for t in ('{project} "{yyyy}-{mm}-{dd}"', '{project} {yyyy}\\{mm}\\{dd}', '{project} {dd}.{mm}.{yyyy}')]

    # then
    assert found[:2] == [msg('jira.release_name.quoted'), msg('jira.release_name.quoted')] and found[2] is None


def test_a_cloud_id_with_a_stray_slash_or_question_mark_stays_one_path_segment():
    # given
    http = FakeHttp({})
    client = Jira('bot@example.com', 'jt', http, cloud_id='c/../x?y', site='https://jira.example.com')

    # when
    client.issue('OPS-1', ['summary'])

    # then
    assert http.calls[0][1].startswith(f'{JIRA_GATEWAY}/c%2F..%2Fx%3Fy/rest/api/3/issue/OPS-1')


def jira_links_app(store: Store, answer: dict):
    http = FakeHttp({('POST', SEARCH): lambda query, body: answer['search']})
    return TestClient(create_app(settings(), store, transport=http))


def access_cards(store: Store) -> list[dict]:
    return [a for a in store.load()['anomalies'] if a.get('ranKey') == 'jira-links:access:issues']


def test_a_jira_5xx_in_the_link_pass_reports_an_error_status_and_holds_the_access_card(store: Store):
    # given
    answer = {'search': HttpError(403, SEARCH)}

    # when
    with jira_links_app(store, answer) as c:
        c.app.state.instances.create('jira', 'main', VALUES | {'cloud-id': 'cid'}, True, USER)
        job = next(j for j in c.app.state.scheduler.jobs if j.name == 'jira-links')
        job.run(NOW)
        answer['search'] = HttpError(503, SEARCH)
        job.run(NOW + 900)
        after = store.load()

    # then
    assert after['sources']['jira-links'] == 'error: issues: Jira HTTP 503'
    assert [a['status'] for a in access_cards(store)] == ['open'], 'a 5xx is not a reading, so the refusal card stays'


def test_switching_jira_off_closes_its_open_access_card(store: Store):
    # given
    answer = {'search': HttpError(403, SEARCH)}

    # when
    with jira_links_app(store, answer) as c:
        c.app.state.instances.create('jira', 'main', VALUES | {'cloud-id': 'cid'}, True, USER)
        next(j for j in c.app.state.scheduler.jobs if j.name == 'jira-links').run(NOW)
        opened = [a['status'] for a in access_cards(store)]
        c.put('/api/admin/plugins/jira', json={'enabled': False}, headers=AUTH)
        job = next((j for j in c.app.state.scheduler.jobs if j.name == 'jira-links'), None)
        job.run(NOW + 900)
        later = [j.name for j in c.app.state.scheduler.jobs]

    # then
    assert opened == ['open'] and [a['status'] for a in access_cards(store)] == ['cleared']
    assert 'jira-links' not in later, 'with the card closed the job goes away again'


def test_the_jira_tile_shows_the_link_pass_refusal_as_no_access_instead_of_connected(store: Store):
    # given
    answer = {'search': HttpError(403, SEARCH)}

    # when
    with jira_links_app(store, answer) as c:
        c.app.state.instances.create('jira', 'main', VALUES | {'cloud-id': 'cid'}, True, USER)
        next(j for j in c.app.state.scheduler.jobs if j.name == 'jira-links').run(NOW)
        row = next(p for p in c.get('/api/admin/plugins', headers=AUTH).json()['plugins'] if p['id'] == 'jira')

    # then
    shown = msg('integrations.health.no_access')
    assert (row['health']['state'], row['health']['text']) == ('warn', shown)
    assert {k: row['health']['instances'][0][k] for k in ('state', 'text')} == {'state': 'warn', 'text': shown}


def test_the_jira_tile_keeps_a_failed_link_pass_an_error_with_its_status(store: Store):
    # given
    answer = {'search': HttpError(502, SEARCH)}

    # when
    with jira_links_app(store, answer) as c:
        c.app.state.instances.create('jira', 'main', VALUES | {'cloud-id': 'cid'}, True, USER)
        next(j for j in c.app.state.scheduler.jobs if j.name == 'jira-links').run(NOW)
        row = next(p for p in c.get('/api/admin/plugins', headers=AUTH).json()['plugins'] if p['id'] == 'jira')

    # then
    assert row['health']['state'] == 'error' and row['health']['instances'][0]['text'].startswith('error: issues: ')


def test_a_403_on_the_account_read_names_the_permission_and_a_401_keeps_the_credentials_text(store: Store):
    # given
    def check(code: int):
        http = FakeHttp({('GET', MYSELF): HttpError(code, MYSELF)})
        rt = jira_runtime(store, http, **(VALUES | {'cloud-id': 'cid'}))
        return jira.test(rt.context(jira.PLUGIN, rt.instances.get('jira', 'main'), NOW))

    # when
    refused, rejected = check(403), check(401)

    # then
    assert (refused.ok, refused.summary) == (False, denied_text('users', 403))
    assert (rejected.ok, rejected.summary) == (False, msg('connection.rejected', status=401))


@pytest.mark.parametrize('code, key', [(403, 'jira.create_denied'), (401, 'jira.create_denied_invalid')])
def test_a_refused_create_names_the_create_issues_permission(code, key):
    # given
    client = Jira('bot@example.com', 'jt', FakeHttp({('POST', CREATE): HttpError(code, CREATE)}), cloud_id='cid', site=SITE)

    # when
    with pytest.raises(contract.Refused) as refused:
        client.create_issue({'summary': 'x'})

    # then
    assert refused.value.text == msg(key, status=code) and 'Create issues' in refused.value.text


def test_the_jira_icon_monogram_uses_the_same_case_as_the_other_word_monograms():
    # when
    text = (Path(jira.__file__).parent / 'icon.svg').read_text(encoding='utf-8')

    # then
    assert '>Ji</text>' in text


def test_the_jira_tile_forgets_the_link_pass_once_jira_links_is_switched_off(store: Store):
    # given
    answer = {'search': HttpError(403, SEARCH)}

    # when
    with jira_links_app(store, answer) as c:
        c.app.state.instances.create('jira', 'main', VALUES | {'cloud-id': 'cid'}, True, USER)
        next(j for j in c.app.state.scheduler.jobs if j.name == 'jira-links').run(NOW)
        c.app.state.jira_links.flag = False
        row = next(p for p in c.get('/api/admin/plugins', headers=AUTH).json()['plugins'] if p['id'] == 'jira')

    # then
    assert (row['health']['state'], row['health']['text']) == ('none', msg('integrations.health.no_checks')), row['health']
