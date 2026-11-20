import dataclasses
from types import SimpleNamespace

import mongomock
import pytest

from tests.conftest import settings
from skopax.app import create_app, public_base
from skopax.cardlink import card_link_pattern, card_url, linked
from skopax.config import load_settings
from skopax.chatui.text import configure, linkify
from skopax.enrich import SourceDown, ticket_keys
from skopax.plugins.jira import env_instances as jira_env
from skopax.plugins.jira.client import Jira
from skopax.investigator.tools import allowed_link_hosts, link_problem
from skopax.jiralinks import ticket_finder, ticket_pattern, ticket_projects
from skopax.mcpserver import instructions
from skopax.models import email_allowed
from skopax.postdeploy import PostDeploy
from skopax.selfreview import SelfReview, self_service
from skopax.settle import ignore_doc
from skopax.slackevents import new_ticket
from skopax.slack import local_hm, local_time, report_message
from tests.plugin_kit import gitlab_runtime, jira_runtime
from tests.sso_kit import redeem, sign_on


def settings_with(**kw):
    return dataclasses.replace(settings(), **kw)


def test_card_link_base_url_forms():
    assert card_url('https://w.example.com/', 'a|b') == 'https://w.example.com/#a=a%7Cb'
    assert card_url('https://w.example.com', 'a|b') == 'https://w.example.com/#a=a%7Cb'
    assert card_url('', 'a|b') == '/#a=a%7Cb'


def test_ticket_pattern_from_projects():
    assert ticket_pattern(('OPS', 'WEB')).findall('OPS-1 WEB-22 ABC-3') == ['OPS-1', 'WEB-22']
    assert ticket_pattern(()).findall('see OPS-1 and ab-2') == ['OPS-1']


def test_draft_links_follow_link_hosts():
    hosts = allowed_link_hosts(settings_with(base_url='https://w.example.com', link_hosts=('grafana.example.org',)))
    assert link_problem('see https://grafana.example.org/d/x', hosts) is None
    assert link_problem('see https://w.example.com/#a=k', allowed_link_hosts(settings_with(base_url='https://w.example.com'))) is None
    assert link_problem('see https://evil.example.net/x', hosts) is not None


def test_draft_links_follow_the_configured_integrations(store):
    # given
    jira_link = 'see https://jira.example.com/browse/OPS-12'
    mr_link = 'see https://gitlab.com/acme/shop/-/merge_requests/4'
    bare = settings_with(base_url='https://w.example.com')
    tickets = SimpleNamespace(db=mongomock.MongoClient(tz_aware=True)['jira'])

    # when
    no_gitlab = gitlab_runtime(store, named=None).ui_hosts()
    with_gitlab = gitlab_runtime(store).ui_hosts()
    no_jira = jira_runtime(tickets, named=None).ui_hosts()
    with_jira = jira_runtime(tickets).ui_hosts()

    # then
    assert link_problem(jira_link, allowed_link_hosts(bare, with_jira)) is None
    assert link_problem(mr_link, allowed_link_hosts(bare, with_gitlab)) is None
    assert link_problem(jira_link, allowed_link_hosts(bare, no_jira)) is not None
    assert link_problem(mr_link, allowed_link_hosts(bare, no_gitlab)) is not None


def test_draft_links_count_the_sso_redirect_host_as_the_public_base():
    # given
    s = settings_with(base_url='', sso_redirect_uri='https://login.example.com/authorized')

    # then
    assert allowed_link_hosts(s) == ('login.example.com',)


def test_email_domains_empty_means_any():
    assert email_allowed('bob@anything.example', ()) is True
    assert email_allowed('bob@other.example', ('example.com',)) is False


def test_jira_is_off_without_a_site_url():
    # then
    assert Jira('bot@example.com', 'tok', site='').enabled is False
    assert Jira('bot@example.com', 'tok', site='https://jira.example.com/').enabled is True
    assert Jira('bot@example.com', 'tok', site='https://jira.example.com/').site == 'https://jira.example.com'


def test_jira_url_is_read_from_its_env_name_without_a_trailing_slash():
    # when
    found = jira_env({'JIRA_URL': 'https://jira.example.com/', 'JIRA_EMAIL': 'bot@example.com', 'JIRA_API_TOKEN': 't'})

    # then
    assert found[0]['url'] == 'https://jira.example.com' and jira_env({}) == []


def test_the_public_base_prefers_the_base_url_then_the_login_host_then_nothing():
    # then
    assert public_base(settings_with(base_url='https://w.example.com/')) == 'https://w.example.com'
    assert public_base(settings_with(base_url='', sso_redirect_uri='https://login.example.com/authorized')) == 'https://login.example.com'
    assert public_base(settings_with(base_url='', sso_redirect_uri='')) == ''


def test_card_links_are_found_only_under_the_base_url():
    # given
    text = 'see https://w.example.com/c/7 and https://w.example.com/#a=log%7Ca%7Cb and https://other.example.net/c/9'

    # then
    assert linked(text, 'https://w.example.com/') == ({'log|a|b'}, {7})
    assert card_link_pattern('https://w.example.com').search('https://w.example.com/c/12').group('num') == '12'


def test_card_links_match_on_the_host_with_either_scheme():
    # then
    assert linked('see https://w.example.com/c/7', 'https://w.example.com') == (set(), {7})
    assert linked('see http://w.example.com/c/8', 'https://w.example.com') == (set(), {8})
    assert linked('see https://w.example.com/c/7 and http://w.example.com/#a=k', 'http://w.example.com') == ({'k'}, {7})
    assert linked('see w.example.com/c/9', 'https://w.example.com') == (set(), {9})
    assert linked('see https://x.w.example.com/c/9 and https://w.example.com.evil.test/c/5', 'https://w.example.com') == (set(), set())


def test_the_ticket_projects_cover_the_default_listed_and_tech_projects():
    # then
    assert ticket_projects(settings_with(jira_project='OPS', jira_projects=('WEB', 'OPS'), jira_tech_project='INFRA')) == ('OPS', 'WEB', 'INFRA')
    assert ticket_projects(settings_with(jira_project='', jira_projects=(), jira_tech_project='')) == ()


def test_the_own_deploy_is_ignored_and_the_release_ignore_list_comes_from_the_settings(store):
    # given
    store.self_deploy, store.release_ignore = 'platform/skopax', ('docs',)
    at = '2026-10-10T09:00:00Z'

    # when
    out = store.add_deploys([{'ns': 'platform', 'svc': 'skopax', 'fromImage': 'a', 'toImage': 'b', 'at': at}, {'ns': 'shop', 'svc': 'checkout-service', 'fromImage': 'a', 'toImage': 'b', 'at': at}])

    # then
    assert (out['inserted'], out['ignored']) == (1, 1)
    assert ignore_doc(store)['services'] == ['docs']


def test_the_self_review_needs_a_project_only(store):
    # given
    on = settings_with(anthropic_api_key='sk-test', self_review_enabled=True)

    # then
    assert SelfReview(store, on, lambda now: {}, lambda t: {}).enabled is True
    assert SelfReview(store, dataclasses.replace(on, self_review_enabled=False), lambda now: {}, lambda t: {}).configured is True
    assert SelfReview(store, dataclasses.replace(on, self_project=''), lambda now: {}, lambda t: {}).enabled is False
    assert SelfReview(store, dataclasses.replace(on, self_log_index=''), lambda now: {}, lambda t: {}).enabled is True
    assert self_service(on) == 'skopax' and self_service(dataclasses.replace(on, self_deploy='')) == 'skopax'


def test_the_token_request_carries_the_configured_headers(monkeypatch):
    # given
    seen = []
    monkeypatch.setattr('skopax.sso.post_form', lambda url, data, headers: seen.append((url, headers)) or {})
    signon = sign_on(settings_with(sso_token_url='https://id.example.com/token', sso_token_headers='X-Client: web'))

    # when
    redeem(signon)

    # then
    assert seen == [('https://id.example.com/token', {'X-Client': 'web'})]
    assert settings_with().sso_scope == 'openid email profile'


def test_the_mcp_instructions_name_the_board_and_its_address():
    # then
    assert 'Skopax is a production health board (https://w.example.com).' in instructions('https://w.example.com')
    assert 'Skopax is a production health board. ' in instructions('')


def test_the_time_helpers_follow_the_local_clock():
    # then
    assert local_hm('2026-10-10T09:04:00+00:00') == '09:04' and local_hm('nope') == ''
    assert local_time('2026-10-10T09:04:00+00:00').endswith('UTC') and local_time(None) == ''


def test_no_logs_link_is_built_for_an_unmatched_flag():
    # given
    row = {'svc': 'checkout-service', 'ns': 'shop', 'flags': ['restarts=2'], 'at': '2026-10-10T09:04:00Z'}
    report = {'summary': 's', 'rows': [row]}

    # when
    text = report_message(report, 'https://w.example.com/#r=1', 'token')['attachments'][0]['blocks'][1]['text']['text']

    # then
    assert 'restarts 2' in text and 'http' not in text


def test_jira_off_without_a_url_raises_before_any_request():
    # given
    calls = []
    jira = Jira('bot@example.com', 'tok', lambda *a, **k: calls.append(a), cloud_id='cid', site='')

    # then
    for call in (lambda: jira.search('x', ['summary']), lambda: jira.issue('OPS-1', ['summary']), lambda: jira.versions(['OPS-1']), lambda: jira.create_issue({})):
        with pytest.raises(SourceDown):
            call()
    assert calls == []
    with pytest.raises(SourceDown):
        Jira('bot@example.com', 'tok', lambda *a, **k: calls.append(a), site='').base
    assert calls == []


def test_the_app_wires_postdeploy_with_the_release_projects_or_the_jira_projects(store, monkeypatch):
    # given
    seen = []

    class Spy(PostDeploy):
        def __init__(self, *a, **kw):
            seen.append(kw['projects'])
            super().__init__(*a, **kw)
    monkeypatch.setattr('skopax.app.PostDeploy', Spy)

    # when
    create_app(settings_with(jira_projects=('OPS', 'WEB'), jira_release_projects=('REL',)), store)
    create_app(settings_with(jira_projects=('OPS', 'WEB'), jira_release_projects=()), store)

    # then
    assert seen == [('REL',), ('OPS', 'WEB')]


def test_release_projects_and_upper_cased_issue_types_are_read_from_env():
    # when
    s = load_settings(env={'JIRA_RELEASE_PROJECTS': 'REL, OPS', 'JIRA_ISSUE_TYPES': 'ops=Story'})

    # then
    assert s.jira_release_projects == ('REL', 'OPS') and s.jira_issue_types == (('OPS', 'Story'),)


def test_free_text_ticket_scanning_needs_projects():
    # given
    text = 'fix CVE-2024 UTF-8 SHA-256 ISO-8601 OPS-12'

    # then
    assert ticket_finder(('OPS',)).findall(text) == ['OPS-12']
    assert ticket_keys([{'title': text}], ()) == [] and ticket_keys([{'title': text}], ('OPS',)) == ['OPS-12']
    assert new_ticket({'status': 'open'}, text, ()) is None and new_ticket({'status': 'open'}, text, ('OPS',)) == 'OPS-12'
    configure('https://jira.example.com', ())
    assert linkify(text) == text
    configure('https://jira.example.com', ('OPS',))
    assert linkify(text).count('](') == 1 and '[OPS-12](https://jira.example.com/browse/OPS-12)' in linkify(text)
    configure('', ())


def test_link_host_entries_are_normalised():
    # when
    hosts = allowed_link_hosts(settings_with(base_url='',
                                            link_hosts=('Example.com', 'https://grafana.example.org/', 'host.example.com:3000', 'dotted.example.com.')))

    # then
    assert hosts == ('example.com', 'grafana.example.org', 'host.example.com', 'dotted.example.com')
