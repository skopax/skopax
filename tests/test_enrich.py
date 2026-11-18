from fastapi.testclient import TestClient

from tests.conftest import AUTH, settings
from tests.fake_http import FakeHttp
from tests.test_deploys import deploy
from skopax.app import create_app
from skopax.enrich import Enricher
from skopax.plugins.jira.client import JIRA_GATEWAY, Jira, denied_text
from skopax.plugins.gitlab.client import GITLAB_API, GitLab, denied_text as gitlab_denied
from skopax.remote import HttpError, Reply, Unreachable
from skopax.store import Store

PROJECTS = f'{GITLAB_API}/groups/acme/projects'
CART = f'{GITLAB_API}/projects/11/repository/commits'
JIRA_SITE = 'https://jira.example.com'
TENANT_INFO = f'{JIRA_SITE}/_edge/tenant_info'
JIRA_API = f'{JIRA_GATEWAY}/cid'
BULK = f'{JIRA_API}/rest/api/3/issue/bulkfetch'
WEB = 'https://gitlab.com/acme/shop/checkout-service'
FULL = '2b2b2b2b' + 'a' * 32


def projects(_q, _b):
    return Reply([
        {'id': 11, 'path_with_namespace': 'acme/shop/checkout-service', 'web_url': WEB},
        {'id': 12, 'path_with_namespace': 'acme/mirror/shop/checkout-service', 'web_url': 'https://gitlab.com/acme/mirror/shop/checkout-service'},
        {'id': 21, 'path_with_namespace': 'acme/members/users-service', 'web_url': 'https://gitlab.com/acme/members/users-service'},
        {'id': 22, 'path_with_namespace': 'acme/identity/users-service', 'web_url': 'https://gitlab.com/acme/identity/users-service'},
    ] if _q.get('page') == ['1'] else [{'id': 31, 'path_with_namespace': 'acme/payments/orders-service', 'web_url': 'https://gitlab.com/acme/payments/orders-service'}],
        {'x-next-page': '2'} if _q.get('page') == ['1'] else {})


def commit(sha: str, title: str, author: str, parents: int = 1) -> dict:
    return {'id': sha, 'title': title, 'author_name': author, 'committed_date': '2026-10-15T08:01:02.000+01:00', 'parent_ids': ['p'] * parents}


def commits(q, _):
    if q['ref_name'] == ['1a1a1a1a..' + FULL] or q['ref_name'] == ['1a1a1a1a..2b2b2b2b']:
        return [commit('c3', "Merge branch 'OPS-115' into 'master'", 'Oscar', 2), commit('c2', 'OPS-115 bundle calc', 'Nina'),
                commit('c1', 'WEB-203 bot session, see OPS-106', 'Adam'), commit('c0', 'fix test', 'Nina')]
    return HttpError(404, CART)


def fake_gitlab(over: dict | None = None) -> FakeHttp:
    routes = {('GET', PROJECTS): projects, ('GET', f'{CART}/2b2b2b2b'): commit(FULL, "Merge branch 'OPS-115' into 'master'", 'Oscar', 2), ('GET', CART): commits}
    routes.update(over or {})
    return FakeHttp(routes)


def fake_jira(calls: list | None = None) -> FakeHttp:
    def bulk(_, body):
        if calls is not None:
            calls.append(body['issueIdsOrKeys'])
        known = {'OPS-115': ['OPS 15.10.2026 Release'], 'WEB-203': ['WEB 15.10.2026 Release', 'OPS 15.10.2026 Release']}
        return {'issues': [{'key': k, 'fields': {'fixVersions': [{'name': v} for v in known[k]]}} for k in body['issueIdsOrKeys'] if k in known],
                'issueErrors': [{'key': k} for k in body['issueIdsOrKeys'] if k not in known]}
    return FakeHttp({('POST', BULK): bulk})


def enricher(store: Store, gitlab: FakeHttp | None = None, jira: FakeHttp | None = None, gl_token: str = 'glpat', jira_token: str = 'jt') -> Enricher:
    return Enricher(store, GitLab(gl_token, gitlab or fake_gitlab(), group='acme'), Jira('bot@example.com', jira_token, jira or fake_jira(), cloud_id='cid', site='https://jira.example.com'), ('OPS', 'WEB'))


def only(store: Store) -> dict:
    return store.deploys_since('2000-01-01T00:00:00Z')[0]


def test_a_bare_deploy_gets_authors_tickets_and_releases(store: Store):
    # given
    store.add_deploys([deploy()])
    gl = fake_gitlab()

    # when
    done = enricher(store, gl).sweep()
    d = only(store)

    # then
    assert done == {'gitlab': 1, 'jira': 1, 'errors': []}
    assert d['imageBy'] == {'title': "Merge branch 'OPS-115' into 'master'", 'date': '2026-10-15', 'sha': '2b2b2b2b', 'url': f'{WEB}/-/commit/{FULL}',
                            'names': ['Nina', 'Adam'], 'commits': 3, 'compare': f'{WEB}/-/compare/1a1a1a1a...{FULL}'}
    assert d['tickets'] == ['OPS-106', 'OPS-115', 'WEB-203']
    assert d['releases'] == ['OPS 15.10.2026 Release', 'WEB 15.10.2026 Release']
    assert d['enriched']['gitlab']
    assert gl.hits(PROJECTS)[0][2]['PRIVATE-TOKEN'] == 'glpat'
    assert 'archived=false' in gl.hits(PROJECTS)[0][1] and 'include_subgroups=true' in gl.hits(PROJECTS)[0][1]


def test_fields_a_post_already_carries_are_never_overwritten(store: Store):
    # given
    store.add_deploys([deploy(imageBy={'names': ['Posted'], 'sha': '2b2b2b2b'}, tickets=['OPS-1'], releases=[])])

    # when
    enricher(store).sweep()
    d = only(store)

    # then
    assert d['imageBy'] == {'names': ['Posted'], 'sha': '2b2b2b2b'}
    assert d['tickets'] == ['OPS-1']
    assert d['releases'] == []


def test_a_later_post_still_wins_over_what_the_server_filled(client: TestClient, store: Store):
    # given
    store.add_deploys([deploy()])
    enricher(store).sweep()

    # when
    client.post('/api/deploys', json=[deploy(tickets=['OPS-9'], releases=['OPS 16.10.2026 Release'])], headers=AUTH)
    d = only(store)

    # then
    assert d['tickets'] == ['OPS-9']
    assert d['releases'] == ['OPS 16.10.2026 Release']


def test_a_name_two_projects_share_or_no_project_has_is_refused_with_a_note(store: Store):
    # given
    store.add_deploys([deploy('users-service', to='aaaabbbb'), deploy('ghost-service', to='ccccdddd'), deploy('orders-service', to='v1.2.3', ticketsNote='no local clone')])
    gl = fake_gitlab({('GET', f'{GITLAB_API}/projects/31/repository/commits/v1.2.3'): HttpError(404, 'x')})

    # when
    enricher(store, gl).sweep()
    by = {d['svc']: d for d in store.deploys_since('2000-01-01T00:00:00Z')}

    # then
    assert by['users-service']['ticketsNote'] == 'no GitLab project with the service name in a subgroup of the group'
    assert by['ghost-service']['ticketsNote'] == 'no GitLab project with the service name in a subgroup of the group'
    assert by['orders-service']['ticketsNote'] == 'tag is not a commit'
    assert all('imageBy' not in d and d['enriched']['gitlab'] for d in by.values())
    assert [c for c in gl.calls if '/projects/2' in c[1]] == []


def test_commits_missing_in_gitlab_leave_a_note_and_a_local_note_is_replaced(store: Store):
    # given
    store.add_deploys([deploy(fromImage='deadbeef', ticketsNote='no local clone')])
    gl = fake_gitlab({('GET', f'{CART}/2b2b2b2b'): HttpError(404, 'x')})

    # when
    enricher(store, gl).sweep()
    d = only(store)

    # then
    assert d['ticketsNote'] == 'image commits not in GitLab, or not visible to the token'
    assert 'imageBy' not in d


def test_a_range_without_ticket_keys_clears_a_local_could_not_check_note(store: Store):
    # given
    store.add_deploys([deploy(ticketsNote='image commits not in the clone')])
    gl = fake_gitlab({('GET', CART): [commit('c0', 'fix test', 'Nina')]})

    # when
    enricher(store, gl).sweep()
    d = only(store)

    # then
    assert 'ticketsNote' not in d
    assert 'tickets' not in d
    assert d['imageBy']['names'] == ['Nina']
    assert 'releases' not in d


def test_a_rollback_falls_back_to_the_merge_authors(store: Store):
    # given
    store.add_deploys([deploy(fromImage=None)])
    gl = fake_gitlab({('GET', CART): lambda q, _: [commit('c2', 'OPS-115 bundle calc', 'Nina'), commit('c1', 'x', 'Adam'), commit('c9', 'y', 'Nina')] if q['ref_name'] == [f'p..{FULL}'] else HttpError(404, CART)})

    # when
    enricher(store, gl).sweep()
    d = only(store)

    # then
    assert d['imageBy']['names'] == ['Nina', 'Adam']
    assert 'commits' not in d['imageBy']
    assert d['ticketsNote'] == 'tag is not a commit'


def test_gitlab_down_stops_the_sweep_and_retries_later(store: Store):
    # given
    store.add_deploys([deploy(), deploy('search-service', to='7a7a7a7a')])
    down = fake_gitlab({('GET', PROJECTS): HttpError(401, PROJECTS)})

    # when
    first = enricher(store, down).sweep()
    second = enricher(store).sweep()

    # then
    assert first['errors'] == [gitlab_denied('projects', 401)] and first['gitlab'] == 0
    assert second['gitlab'] == 2
    assert only(store)['tickets'] == ['OPS-106', 'OPS-115', 'WEB-203']


def test_a_checked_deploy_is_not_asked_again(store: Store):
    # given
    store.add_deploys([deploy()])
    gl = fake_gitlab({('GET', CART): [commit('c0', 'fix test', 'Nina')]})
    enricher(store, gl).sweep()

    # when
    again = fake_gitlab()
    done = enricher(store, again).sweep()

    # then
    assert done['gitlab'] == 0
    assert again.calls == []


def test_jira_runs_alone_when_only_its_credentials_are_set_and_batches_the_keys(store: Store):
    # given
    store.add_deploys([deploy(tickets=['OPS-115', 'OPS-404']), deploy('search-service', to='7a7a7a7a', tickets=['WEB-203'])])
    asked: list = []

    # when
    done = enricher(store, FakeHttp(), fake_jira(asked), gl_token='').sweep()
    by = {d['svc']: d for d in store.deploys_since('2000-01-01T00:00:00Z')}

    # then
    assert done['jira'] == 2
    assert asked == [['OPS-115', 'OPS-404', 'WEB-203']]
    assert by['checkout-service']['releases'] == ['OPS 15.10.2026 Release']
    assert by['search-service']['releases'] == ['OPS 15.10.2026 Release', 'WEB 15.10.2026 Release']


def test_jira_down_leaves_releases_unset(store: Store):
    # given
    store.add_deploys([deploy(tickets=['OPS-115'])])

    # when
    done = enricher(store, jira=FakeHttp({('POST', BULK): HttpError(401, BULK)}), gl_token='').sweep()

    # then
    assert done['errors'] == [denied_text('issues', 401)]
    assert 'releases' not in only(store)


def test_an_unreachable_source_is_a_fixed_text_never_the_url_it_tried(store: Store):
    # given
    store.add_deploys([deploy(tickets=['OPS-115'])])
    hidden = 'https://user:SENTINEL-77@gitlab.example.com/api/v4/projects unreachable (gaierror)'
    gitlab_down = fake_gitlab({('GET', PROJECTS): Unreachable(hidden)})
    jira_down = FakeHttp({('POST', BULK): Unreachable(hidden)})

    # when
    jira = enricher(store, jira=jira_down, gl_token='').sweep()
    git = enricher(store, gitlab_down, jira_token='').sweep()

    # then
    assert (git['errors'], jira['errors']) == (['GitLab could not be reached'], ['Jira could not be reached'])


def test_without_tokens_nothing_is_enriched(store: Store):
    # given
    store.add_deploys([deploy()])
    gl, jira = fake_gitlab(), fake_jira()

    # when
    e = enricher(store, gl, jira, gl_token='', jira_token='')
    done = e.sweep()

    # then
    assert e.enabled is False
    assert done == {'gitlab': 0, 'jira': 0, 'errors': []}
    assert gl.calls == [] and jira.calls == []


def test_a_post_enriches_the_posted_deploys_in_the_background(store: Store):
    # given
    gl, jira = fake_gitlab(), fake_jira()

    # when
    with TestClient(create_app(settings(), store, gitlab=GitLab('glpat', gl, group='acme'), jira=Jira('bot@example.com', 'jt', jira, cloud_id='cid', site='https://jira.example.com'))) as c:
        res = c.post('/api/deploys', json=[deploy()], headers=AUTH)
        listed = c.get('/api/deploys').json()

    # then
    assert res.json() == {'received': 1, 'inserted': 1, 'existing': 0}
    assert listed[0]['tickets'] == ['OPS-106', 'OPS-115', 'WEB-203']
    assert listed[0]['releases'] == ['OPS 15.10.2026 Release', 'WEB 15.10.2026 Release']
    assert listed[0]['imageBy']['commits'] == 3


def test_find_user_asks_myself_for_the_token_owner_and_searches_for_others():
    # given
    http = FakeHttp({('GET', TENANT_INFO): {'cloudId': 'cid'}, ('GET', JIRA_API + '/rest/api/3/myself'): {'accountId': '712020:me', 'emailAddress': 'oscar.green@example.com'},
                     ('GET', JIRA_API + '/rest/api/3/user/search'): lambda q, _: [{'accountId': '712020:other'}] if q.get('query') == ['other@example.com'] else []})
    jira = Jira('Oscar.Green@example.com', 'jt', http, site='https://jira.example.com')

    # when
    me = jira.find_user('oscar.green@example.com')
    other = jira.find_user('other@example.com')
    nobody = jira.find_user('ghost@example.com')

    # then
    assert (me, other, nobody) == ('712020:me', '712020:other', None)
    assert [c[1].split('?')[0] for c in http.calls] == [TENANT_INFO, JIRA_API + '/rest/api/3/myself', JIRA_API + '/rest/api/3/user/search', JIRA_API + '/rest/api/3/user/search'], 'the cloud id is read once from tenant_info, then every call goes through the Atlassian API gateway'
    assert jira.cloud_id == 'cid'


def test_a_missing_cloud_id_is_a_source_outage_not_a_crash():
    # given
    from skopax.enrich import SourceDown
    http = FakeHttp({('GET', TENANT_INFO): {'nope': 1}})
    jira = Jira('x@example.com', 'jt', http, site='https://jira.example.com')

    # when
    found = jira.find_user('x@example.com')
    try:
        jira.search('project = OPS', ['summary'])
        raised = None
    except SourceDown as e:
        raised = str(e)

    # then
    assert found is None and raised == 'Jira cloud id: tenant_info answered without a cloudId'


def test_the_service_reads_its_master_pipelines_since_a_day_for_the_build_numbers():
    # given
    url = f'{GITLAB_API}/projects/4242/pipelines'
    http = FakeHttp({('GET', url): lambda q, b: Reply([{'id': 1, 'iid': 632, 'sha': 'a' * 40, 'status': 'success'}], {})})

    # when
    got = GitLab('glpat', http, group='acme').pipelines_since(4242, 'master', '2026-10-12T00:00:00Z')

    # then
    sent = http.hits(url)[0][1]
    assert got == [{'id': 1, 'iid': 632, 'sha': 'a' * 40, 'status': 'success'}]
    assert 'ref=master' in sent and 'updated_after=2026-10-12T00%3A00%3A00Z' in sent


def test_jira_answering_5xx_leaves_releases_unset_with_its_status(store: Store):
    # given
    store.add_deploys([deploy(tickets=['OPS-115'])])

    # when
    done = enricher(store, jira=FakeHttp({('POST', BULK): HttpError(503, BULK)}), gl_token='').sweep()

    # then
    assert done['errors'] == ['Jira HTTP 503'] and 'releases' not in only(store) and 'releasesNote' not in only(store)


def test_a_refused_release_read_is_stamped_on_the_deploy_and_a_later_read_clears_it(store: Store):
    # given
    store.add_deploys([deploy(tickets=['OPS-115'])])

    # when
    enricher(store, jira=FakeHttp({('POST', BULK): HttpError(403, BULK)}), gl_token='').sweep()
    refused = only(store)
    enricher(store, gl_token='').sweep()
    read = only(store)

    # then
    assert refused['releasesNote'] == denied_text('issues', 403) and 'releases' not in refused
    assert read['releases'] == ['OPS 15.10.2026 Release'] and 'releasesNote' not in read


def test_an_issue_jira_does_not_return_is_named_as_missing_or_hidden_and_read_again(store: Store):
    # given
    calls: list = []
    store.add_deploys([deploy(tickets=['OPS-115', 'OPS-999'])])

    # when
    enricher(store, jira=fake_jira(calls), gl_token='').sweep()
    enricher(store, jira=fake_jira(calls), gl_token='').sweep()
    d = only(store)

    # then
    assert d['releases'] == ['OPS 15.10.2026 Release']
    assert d['releasesNote'] == 'Jira did not return OPS-999: it does not exist or the token cannot see it'
    assert calls == [['OPS-115', 'OPS-999'], ['OPS-115', 'OPS-999']], 'a deploy with a hidden issue is read again while it is in the window'
