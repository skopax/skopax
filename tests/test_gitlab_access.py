import logging
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest

from skopax.contract import Denied
from skopax.errkind import NoAccess
from skopax.messages import msg
from skopax.plugins import gitlab
from skopax.contract import ReadError
from skopax.plugins.gitlab import tools as gitlab_tools
from skopax.plugins.gitlab.client import GITLAB_API, PERMISSIONS, GitLab, denied_text, hidden_text, similar_projects, worded_project
from skopax.remote import HttpError
from skopax.store import Store
from tests.fake_http import FakeHttp
from tests.plugin_kit import gitlab_runtime
from tests.test_compare_tool import COMMITS, fake as compare_fake, mrs_of
from tests.test_enrich import CART, PROJECTS, commit, enricher, fake_gitlab
from tests.test_deploys import deploy
from tests.test_gitlab_ci_tools import PIPES, call, gl_box, routes

NOW = 1_792_224_000.0
FILE = f'{GITLAB_API}/projects/11/repository/files/src%2FA.kt'
VERSION = f'{GITLAB_API}/version'


def test_a_refused_read_names_the_part_and_the_scope_and_a_401_says_the_token_may_be_invalid():
    # given
    forbidden = FakeHttp({('GET', PROJECTS): HttpError(403, PROJECTS), ('GET', FILE): HttpError(403, FILE)})
    unauthorized = FakeHttp({('GET', f'{PIPES}/5'): HttpError(401, PIPES)})

    # when
    raised = []
    for read in (lambda: GitLab('glpat', forbidden, group='acme').projects(), lambda: GitLab('glpat', forbidden, group='acme').file(11, 'src/A.kt', 'master'),
                 lambda: GitLab('glpat', unauthorized, group='acme').pipeline(40, 5)):
        with pytest.raises(Denied) as e:
            read()
        raised.append(e.value)

    # then
    assert [(e.part, e.text) for e in raised] == [('projects', denied_text('projects', 403)), ('code', denied_text('code', 403)), ('pipelines', denied_text('pipelines', 401))]
    assert all(isinstance(e, NoAccess) and 'read_api' in e.permission and e.permission in e.text for e in raised)
    assert raised[0].text == 'GitLab refused to read projects (HTTP 403): the token lacks the read_api scope or access to the group'
    assert raised[2].text == 'GitLab refused to read pipelines (HTTP 401): the token is invalid or lacks the read_api scope or a role that can read it, such as Reporter'


def test_a_hidden_group_is_named_as_a_possible_refusal_and_not_as_not_found():
    # given
    hidden = FakeHttp({('GET', PROJECTS): HttpError(404, PROJECTS)})

    # when
    with pytest.raises(Denied) as e:
        GitLab('glpat', hidden, group='acme').projects()

    # then
    assert (e.value.part, e.value.text) == ('projects', hidden_text('acme'))
    assert e.value.text == 'GitLab answered HTTP 404 for the group acme: it does not exist, or the token lacks the read_api scope or access to the group'


def test_a_refused_project_listing_is_never_read_as_no_such_project():
    # given
    refused = FakeHttp({('GET', PROJECTS): HttpError(403, PROJECTS)})

    # when
    raised = []
    for read in (lambda: similar_projects(GitLab('glpat', refused, group='acme'), 'checkout'), lambda: worded_project(GitLab('glpat', refused, group='acme'), 'checkout'),
                 lambda: GitLab('glpat', refused, group='acme').similar('checkout')):
        with pytest.raises(Denied) as e:
            read()
        raised.append(e.value.text)

    # then
    assert raised == [denied_text('projects', 403)] * 3


def test_compare_names_refused_merge_requests_and_words_a_404_as_a_possible_refusal(store: Store):
    # given
    refused = compare_fake()
    refused.routes[('GET', mrs_of(COMMITS[1]['id']))] = HttpError(403, 'x')
    gone = compare_fake()
    for c in COMMITS:
        gone.routes[('GET', mrs_of(c['id']))] = HttpError(404, 'x')

    # when
    denied, denied_err = call(gl_box(store, refused), 'gitlab_compare', repo='checkout-service', **{'from': 'a', 'to': 'b'})
    hidden, hidden_err = call(gl_box(store, gone), 'gitlab_compare', repo='checkout-service', **{'from': 'a', 'to': 'b'})

    # then
    assert not denied_err and denied['mergeRequestsError'] == denied_text('merge requests', 403)
    assert not hidden_err and hidden['mergeRequests'] == []
    assert hidden['mergeRequestsError'] == 'GitLab answered HTTP 404 to the merge requests of 3 commits: merge requests may be off for the project, or the token lacks the read_api scope or a role that can read it, such as Reporter'


def test_a_refused_tool_call_is_a_tool_error_naming_the_scope(store: Store):
    # given
    listing = routes({('GET', f'{GITLAB_API}/groups/acme/projects'): HttpError(403, 'x')})
    pipelines = routes({('GET', PIPES): HttpError(403, PIPES)})
    blank = routes({('GET', f'{GITLAB_API}/projects/40/merge_requests'): HttpError(404, 'x')})

    # when
    listed = call(gl_box(store, listing), 'gitlab_file', repo='checkout-service', path='a.kt')
    piped = call(gl_box(store, pipelines), 'gitlab_pipelines', repo='skopax')
    missing = call(gl_box(store, blank), 'gitlab_mrs', repo='skopax')

    # then
    assert listed == (denied_text('projects', 403), True)
    assert piped == (denied_text('pipelines', 403), True) and 'read_api' in piped[0]
    assert missing == ('GitLab answered HTTP 404: no such item, or the token lacks the read_api scope or a role that can read it, such as Reporter', True)


def test_a_404_on_a_file_blame_compare_or_pipeline_is_worded_as_a_possible_refusal(store: Store):
    # given
    http = routes({('GET', f'{GITLAB_API}/projects/40/repository/files/a.kt'): HttpError(404, 'x'), ('GET', f'{GITLAB_API}/projects/40/repository/files/a.kt/blame'): HttpError(404, 'x'),
                   ('GET', f'{PIPES}/5'): HttpError(404, 'x'), ('GET', f'{GITLAB_API}/projects/40/repository/compare'): HttpError(404, 'x')})
    b = gl_box(store, http)

    # when
    read = call(b, 'gitlab_file', repo='skopax', path='a.kt')
    blamed = call(b, 'gitlab_blame', repo='skopax', path='a.kt', lines='1-2')
    piped = call(b, 'gitlab_pipelines', repo='skopax', pipeline=5)
    compared = call(b, 'gitlab_compare', repo='skopax', **{'from': 'a', 'to': 'b'})

    # then
    assert read == blamed == ('a.kt is not a file in skopax at master, or the token lacks the read_api scope or a role that can read it, such as Reporter: give the path of one file, not a directory '
                              '(gitlab_search with the class, function or text finds where it lives)', True)
    assert piped == ('GitLab found no pipeline 5 in skopax, or the token lacks the read_api scope or a role that can read it, such as Reporter', True)
    assert compared == ('GitLab found no a or b in skopax, or the token lacks the read_api scope or a role that can read it, such as Reporter', True)


@pytest.mark.parametrize('answer, summary', [
    (HttpError(403, PROJECTS), denied_text('projects', 403)),
    (HttpError(404, PROJECTS), hidden_text('acme')),
])
def test_the_connection_test_reads_the_group_and_fails_naming_the_scope(store: Store, answer, summary):
    # given
    http = FakeHttp({('GET', VERSION): {'version': '17.5.0'}, ('GET', PROJECTS): answer})
    rt = gitlab_runtime(store, http)

    # when
    result = gitlab.test(rt.context(gitlab.PLUGIN, rt.instances.get('gitlab', 'main'), NOW))

    # then
    assert (result.ok, result.summary) == (False, summary)
    asked = http.hits(PROJECTS)[0][1]
    assert parse_qs(urlsplit(asked).query)['per_page'] == ['1']


def test_the_connection_test_passes_when_the_group_lists_and_keeps_the_credentials_text_for_a_401(store: Store):
    # given
    good = FakeHttp({('GET', VERSION): {'version': '17.5.0'}, ('GET', PROJECTS): [{'id': 11, 'path_with_namespace': 'acme/shop/checkout-service'}]})
    revoked = FakeHttp({('GET', VERSION): {'version': '17.5.0'}, ('GET', PROJECTS): HttpError(401, PROJECTS)})

    def check(http):
        rt = gitlab_runtime(store, http)
        return gitlab.test(rt.context(gitlab.PLUGIN, rt.instances.get('gitlab', 'main'), NOW))

    # when
    ok, rejected = check(good), check(revoked)

    # then
    assert (ok.ok, ok.summary) == (True, 'GitLab 17.5.0') and [c[1].split('?')[0] for c in good.calls] == [VERSION, PROJECTS]
    assert (rejected.ok, rejected.summary) == (False, msg('connection.rejected', status=401))


def test_a_refused_gitlab_read_is_stored_on_every_unread_deploy_logged_with_the_permission_and_cleared_by_a_later_read(store: Store, caplog):
    # given
    store.add_deploys([deploy(), deploy('search-service', to='7a7a7a7a')])
    refused = fake_gitlab({('GET', PROJECTS): HttpError(403, PROJECTS)})
    good = fake_gitlab({('GET', CART): [commit('c0', 'fix test', 'Nina')]})

    # when
    with caplog.at_level(logging.WARNING, logger='skopax.enrich'):
        done = enricher(store, refused, jira_token='').sweep()
    before = {d['svc']: d for d in store.deploys_since('2000-01-01T00:00:00Z')}
    enricher(store, good, jira_token='').sweep()
    after = {d['svc']: d for d in store.deploys_since('2000-01-01T00:00:00Z')}

    # then
    assert done['errors'] == [denied_text('projects', 403)] and done['gitlab'] == 0
    assert {svc: d.get('enriched') for svc, d in before.items()} == {'checkout-service': {'refused': denied_text('projects', 403)}, 'search-service': {'refused': denied_text('projects', 403)}}
    assert all('ticketsNote' not in d and 'imageBy' not in d for d in before.values()), 'a refusal is never stored as a finding'
    assert [r.getMessage() for r in caplog.records if r.name == 'skopax.enrich'] == ['deploy enrichment through GitLab refused for projects: the token lacks the read_api scope or access to the group']
    assert all('refused' not in d['enriched'] and d['enriched']['gitlab'] for d in after.values())
    assert 'tickets' not in after['checkout-service'] and after['search-service']['ticketsNote'] == 'no GitLab project with the service name in a subgroup of the group'


def test_a_404_before_a_refusal_keeps_its_count_in_the_merge_request_error(store: Store):
    # given
    http = compare_fake()
    http.routes[('GET', mrs_of(COMMITS[1]['id']))] = HttpError(404, 'x')
    http.routes[('GET', mrs_of(COMMITS[0]['id']))] = HttpError(403, 'x')

    # when
    out, err = call(gl_box(store, http), 'gitlab_compare', repo='checkout-service', **{'from': 'a', 'to': 'b'})

    # then
    assert not err and out['mergeRequestsError'] == denied_text('merge requests', 403) + '; ' + msg('gitlab.mrs_hidden', count=1, plural='', permission=PERMISSIONS['merge requests'])
    assert 'of 1 commit:' in out['mergeRequestsError']


def test_served_turns_a_refusal_into_a_403_tool_error_and_a_404_into_a_named_text():
    # given
    refused = SimpleNamespace(client=lambda: GitLab('glpat', FakeHttp({('GET', PROJECTS): HttpError(403, PROJECTS)}), group='acme'))
    blank = SimpleNamespace(client=lambda: GitLab('glpat', routes({('GET', f'{GITLAB_API}/projects/40/merge_requests'): HttpError(404, 'x')}), group='acme'))

    # when
    with pytest.raises(ReadError) as denied:
        gitlab_tools.served(gitlab_tools.read_file)(refused, {'repo': 'checkout-service', 'path': 'a.kt'})
    with pytest.raises(ReadError) as missing:
        gitlab_tools.served(gitlab_tools.merge_requests)(blank, {'repo': 'skopax'})

    # then
    assert (denied.value.status, denied.value.message) == (403, denied_text('projects', 403))
    assert (missing.value.status, missing.value.message) == (404, msg('gitlab.missing', permission=PERMISSIONS['code']))


def test_the_404_texts_are_message_keys():
    # given
    http = routes({('GET', f'{GITLAB_API}/projects/40/repository/files/a.kt'): HttpError(404, 'x'), ('GET', f'{PIPES}/5'): HttpError(404, 'x'),
                   ('GET', f'{GITLAB_API}/projects/40/repository/compare'): HttpError(404, 'x')})
    gl = GitLab('glpat', http, group='acme')
    code = PERMISSIONS['code']

    # when
    texts = []
    for run, args in ((gitlab_tools.read_file, {'repo': 'skopax', 'path': 'a.kt'}), (gitlab_tools.pipelines, {'repo': 'skopax', 'pipeline': 5}),
                      (gitlab_tools.compare, {'repo': 'skopax', 'from': 'a', 'to': 'b'})):
        with pytest.raises(ReadError) as e:
            run(gl, args)
        texts.append(e.value.message)

    # then
    assert texts == [msg('gitlab.no_file', path='a.kt', repo='skopax', ref='master', permission=code), msg('gitlab.no_pipeline', pipeline=5, repo='skopax', permission=PERMISSIONS['pipelines']),
                     msg('gitlab.no_refs', old='a', new='b', repo='skopax', permission=code)]


def test_a_refused_project_stamps_only_its_own_deploys_naming_the_project_and_the_sweep_goes_on(store: Store, caplog):
    # given
    orders = f'{GITLAB_API}/projects/31/repository/commits'
    store.add_deploys([deploy(), deploy('orders-service', to='4d4d4d4d')])
    http = fake_gitlab({('GET', f'{CART}/2b2b2b2b'): HttpError(403, CART), ('GET', f'{orders}/4d4d4d4d'): commit('4d4d4d4d' + 'b' * 32, 'OPS-120 fix', 'Erin'),
                        ('GET', orders): [commit('c9', 'OPS-120 fix', 'Erin')]})

    # when
    with caplog.at_level(logging.WARNING, logger='skopax.enrich'):
        done = enricher(store, http, jira_token='').sweep()
    by = {d['svc']: d for d in store.deploys_since('2000-01-01T00:00:00Z')}

    # then
    assert by['checkout-service']['enriched'] == {'refused': 'acme/shop/checkout-service: ' + denied_text('code', 403)}
    assert by['orders-service']['imageBy']['names'] == ['Erin'] and by['orders-service']['enriched']['gitlab'], 'one refused project does not stop the others'
    assert done['gitlab'] == 1 and done['errors'] == ['acme/shop/checkout-service: ' + denied_text('code', 403)]


def test_a_service_with_no_project_in_a_subgroup_gets_a_neutral_note_not_a_token_one(store: Store):
    # given
    store.add_deploys([deploy('ghost-service')])

    # when
    enricher(store, fake_gitlab(), jira_token='').sweep()
    note = store.deploys_since('2000-01-01T00:00:00Z')[0]['ticketsNote']

    # then
    assert 'token' not in note and note == 'no GitLab project with the service name in a subgroup of the group'
