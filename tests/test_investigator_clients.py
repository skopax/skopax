import base64

from tests.fake_http import FakeHttp
from skopax.plugins.jira.client import JIRA_GATEWAY, Jira
from skopax.plugins.gitlab.client import GITLAB_API, GitLab

JIRA_API = f'{JIRA_GATEWAY}/cid'
from skopax.kit.promql import Prometheus

FILE = f'{GITLAB_API}/projects/11/repository/files/src%2Fmain%2FFoo.kt'


def test_gitlab_reads_master_a_file_its_blame_and_a_code_search():
    # given
    http = FakeHttp({
        ('GET', f'{GITLAB_API}/projects/11/repository/branches/master'): {'commit': {'id': 'a' * 40}},
        ('GET', FILE): {'content': base64.b64encode(b'line 1\nline 2\n').decode()},
        ('GET', f'{FILE}/blame'): [{'commit': {'id': 'b' * 40, 'author_name': 'Nina'}, 'lines': ['line 1']}],
        ('GET', f'{GITLAB_API}/projects/11/search'): [{'path': 'src/main/Foo.kt', 'startline': 1, 'data': 'line 1'}],
    })
    gl = GitLab('glpat', http, group='acme')

    # when
    sha, text = gl.branch_sha(11, 'master'), gl.file(11, 'src/main/Foo.kt', 'master')
    blame, hits = gl.blame(11, 'src/main/Foo.kt', 'a' * 40, 1, 2), gl.search(11, 'Foo')

    # then
    assert sha == 'a' * 40 and text == 'line 1\nline 2\n'
    assert blame[0]['commit']['author_name'] == 'Nina' and hits[0]['path'] == 'src/main/Foo.kt'
    assert 'range%5Bstart%5D=1' in http.hits(f'{FILE}/blame')[0][1]
    assert 'scope=blobs' in http.hits(f'{GITLAB_API}/projects/11/search')[0][1]


def test_jira_searches_with_a_field_list_and_a_missing_issue_is_none():
    # given
    http = FakeHttp({
        ('POST', f'{JIRA_API}/rest/api/3/search/jql'): {'issues': [{'key': 'OPS-1', 'fields': {'summary': 's'}}]},
        ('GET', f'{JIRA_API}/rest/api/3/issue/OPS-1'): {'key': 'OPS-1', 'fields': {'summary': 's'}},
    })
    jira = Jira('bot@example.com', 'jt', http, cloud_id='cid', site='https://jira.example.com')

    # when
    found = jira.search('text ~ "x"', ['summary'], 5)
    one, none = jira.issue('OPS-1', ['summary']), jira.issue('OPS-404', ['summary'])

    # then
    assert found[0]['key'] == 'OPS-1' and one['key'] == 'OPS-1' and none is None
    assert http.hits(f'{JIRA_API}/rest/api/3/search/jql')[0][3] == {'jql': 'text ~ "x"', 'fields': ['summary'], 'maxResults': 5}


def test_prometheus_instant_and_range_return_the_raw_result():
    # given
    http = FakeHttp({
        ('GET', 'https://prom.test/api/v1/query'): {'status': 'success', 'data': {'result': [{'metric': {'job': 'x'}, 'value': [1, '2']}]}},
        ('GET', 'https://prom.test/api/v1/query_range'): {'status': 'success', 'data': {'result': [{'metric': {'job': 'x'}, 'values': [[1, '2']]}]}},
    })
    p = Prometheus('https://prom.test', http)

    # then
    assert p.instant('up', 100.0) == [{'metric': {'job': 'x'}, 'value': [1, '2']}]
    assert p.range_query('up', 0.0, 600.0, 60)[0]['values'] == [[1, '2']]
    assert 'step=60' in http.hits('https://prom.test/api/v1/query_range')[0][1]
