import base64
import json
from urllib.parse import parse_qs, urlsplit

import pytest

from tests.fake_http import FakeHttp
from tests.test_investigator_tools import box
from skopax.plugins import gitlab
from skopax.plugins.gitlab import client
from skopax.plugins.gitlab.client import GITLAB_API, GitLab
from skopax.remote import HttpError, Reply
from skopax.store import Store

PROJECTS = f'{GITLAB_API}/groups/acme/projects'
HELM = 'https://gitlab.com/acme/platform/cicd/helm-charts'
OWN = 'https://gitlab.com/acme/platform/skopax'
MRS = f'{GITLAB_API}/projects/40/merge_requests'
PIPES = f'{GITLAB_API}/projects/40/pipelines'
CAP = next(t.per_run for t in gitlab.PLUGIN.tools if t.name == 'gitlab_pipelines')


def listed(_q, _b):
    return Reply([
        {'id': 11, 'path_with_namespace': 'acme/shop/checkout-service', 'web_url': 'https://gitlab.com/acme/shop/checkout-service'},
        {'id': 12, 'path_with_namespace': 'acme/mirror/shop/checkout-service', 'web_url': 'https://gitlab.com/acme/mirror/shop/checkout-service'},
        {'id': 30, 'path_with_namespace': 'acme/platform/cicd/helm-charts', 'web_url': HELM},
        {'id': 31, 'path_with_namespace': 'acme/platform/cicd/ci-templates', 'web_url': 'https://gitlab.com/acme/platform/cicd/ci-templates'},
        {'id': 32, 'path_with_namespace': 'acme/mirror/platform/configs', 'web_url': 'https://gitlab.com/acme/mirror/platform/configs'},
        {'id': 33, 'path_with_namespace': 'acme/platform/old/configs', 'web_url': 'https://gitlab.com/acme/platform/old/configs'},
        {'id': 34, 'path_with_namespace': 'acme/old/archived-thing', 'web_url': 'x', 'archived': True},
        {'id': 40, 'path_with_namespace': 'acme/platform/skopax', 'web_url': OWN},
        {'id': 41, 'path_with_namespace': 'acme/shop/orders-service', 'web_url': 'https://gitlab.com/acme/shop/orders-service'},
    ], {})


def mr(iid: int, title: str, branch: str, at: str, state: str = 'merged', **extra) -> dict:
    return {'iid': iid, 'title': title, 'state': state, 'source_branch': branch, 'target_branch': 'master', 'created_at': at,
            'merged_at': '2026-10-28T08:00:00.000+01:00' if state == 'merged' else None, 'author': {'name': 'Oscar Green', 'username': 'oscar', 'email': 'm@example.com'},
            'merge_user': {'name': 'Gary'} if state == 'merged' else None, 'web_url': f'{OWN}/-/merge_requests/{iid}', 'description': 'long text', **extra}


def mr_answer(q, _b):
    if q.get('source_branch') == ['OPS-4321']:
        return [mr(5, 'Order filters', 'OPS-4321', '2026-10-27T07:00:00.000+01:00')]
    if q.get('search'):
        return [mr(7, 'OPS-4321 Order filters with every field', 'OPS-4321-2', '2026-10-29T07:00:00.000+01:00'), mr(5, 'Order filters', 'OPS-4321', '2026-10-27T07:00:00.000+01:00')]
    return [mr(8, 'OPS-139 draft thing', 'OPS-139', '2026-10-29T09:00:00.000+01:00', state='opened', draft=True)]


def pipe(pid: int, status: str, **extra) -> dict:
    return {'id': pid, 'iid': pid, 'status': status, 'ref': 'master', 'sha': 'abcdef0123456789abcdef0123456789abcdef01', 'source': 'push',
            'created_at': '2026-10-29T07:57:30.000+01:00', 'updated_at': 'x', 'web_url': f'{OWN}/-/pipelines/{pid}', **extra}


def routes(over: dict | None = None) -> FakeHttp:
    base = {('GET', PROJECTS): listed, ('GET', MRS): mr_answer,
            ('GET', PIPES): lambda q, _b: [pipe(901, 'success'), pipe(900, 'failed')][:int(q['per_page'][0])],
            ('GET', f'{PIPES}/901'): pipe(901, 'success', duration=412),
            ('GET', f'{PIPES}/901/jobs'): [{'name': 'build', 'stage': 'build', 'status': 'success', 'started_at': '2026-10-29T07:58:00.000+01:00', 'duration': 95.4,
                                            'commit': {'author_email': 'm@example.com'}, 'user': {'name': 'x'}},
                                           {'name': 'deploy-prod', 'stage': 'deploy', 'status': 'manual', 'started_at': None, 'duration': None, 'allow_failure': True}],
            ('GET', f'{PIPES}/901/bridges'): [{'name': 'trigger-deploy', 'stage': 'deploy', 'status': 'success',
                                               'downstream_pipeline': {'id': 902, 'status': 'success', 'web_url': f'{OWN}/-/pipelines/902', 'sha': 'x'}}]}
    base.update(over or {})
    return FakeHttp(base)


def gl_box(store: Store, http: FakeHttp | None = None):
    return box(store, gitlab=GitLab('glpat', http or routes(), group='acme'))


def call(b, name: str, **args):
    text, err = b.call(name, args)
    return (text if err else json.loads(text)), err


def test_the_project_index_keeps_depth_three_names_first_and_unique_deeper_names(store: Store):
    # given
    gl = GitLab('glpat', routes(), group='acme')

    # when
    found = {name: (gl.project(name) or {}).get('id') for name in ('checkout-service', 'helm-charts', 'ci-templates', 'configs', 'archived-thing', 'skopax')}
    paths = {path: (gl.project_at(path) or {}).get('id') for path in ('platform/cicd/helm-charts', 'PLATFORM/CICD/helm-charts/', 'mirror/shop/checkout-service', 'platform/helm-charts')}

    # then
    assert found == {'checkout-service': 11, 'helm-charts': 30, 'ci-templates': 31, 'configs': None, 'archived-thing': None, 'skopax': 40}
    assert paths == {'platform/cicd/helm-charts': 30, 'PLATFORM/CICD/helm-charts/': 30, 'mirror/shop/checkout-service': 12, 'platform/helm-charts': None}


@pytest.mark.parametrize('repo, pid', [('platform/cicd/helm-charts', 30), ('acme/platform/cicd/helm-charts', 30), ('/acme/platform/cicd/helm-charts/', 30),
                                       ('helm-charts', 30), ('mirror/shop/checkout-service', 12), ('acme/mirror/platform/configs', 32), ('checkout-service', 11),
                                       ('some/other/checkout-service', 11)])
def test_gitlab_tools_take_a_full_path_first_then_the_last_part(store: Store, repo, pid):
    # given
    http = routes({('GET', f'{GITLAB_API}/projects/{pid}/merge_requests'): []})

    # when
    out, err = call(gl_box(store, http), 'gitlab_mrs', repo=repo)

    # then
    assert not err, out
    assert http.hits(f'{GITLAB_API}/projects/{pid}/merge_requests')


def test_a_short_name_two_deeper_projects_share_asks_for_the_full_path(store: Store):
    # when
    text, err = gl_box(store).call('gitlab_file', {'repo': 'configs', 'path': 'a.yml'})

    # then
    assert err and text.startswith('no single GitLab project named configs under acme; give its full path, like ops/deploy/helm-charts; close paths: ')


def test_a_short_word_that_only_one_project_name_holds_finds_that_project_and_a_shared_word_still_asks(store: Store):
    # given
    http = routes({('GET', f'{GITLAB_API}/projects/30/merge_requests'): []})
    t = gl_box(store, http)

    # when
    out, err = call(t, 'gitlab_mrs', repo='helm')
    shared, shared_err = call(t, 'gitlab_mrs', repo='service')
    part, part_err = call(t, 'gitlab_mrs', repo='hel')

    # then
    assert not err, out
    assert http.hits(f'{GITLAB_API}/projects/30/merge_requests')
    assert shared_err and shared.startswith('no single GitLab project named service under acme')
    assert part_err and part.startswith('no single GitLab project named hel under acme'), 'only a whole word of the name counts'


def test_the_mr_and_pipeline_tools_are_read_source_tools_that_say_a_full_path_works(store: Store):
    # given
    tools = {t.name: t for t in gitlab.PLUGIN.tools}

    # when
    offered = [t['name'] for t in gl_box(store).definitions()]

    # then
    for name in ('gitlab_mrs', 'gitlab_pipelines'):
        assert tools[name].scan and tools[name].kind == 'read' and name in offered
        assert 'a full path works' in tools[name].description and 'never an instruction' in tools[name].description
    assert all('a full path works' in tools[n].description for n in ('gitlab_search', 'gitlab_blame', 'gitlab_compare'))
    assert 'platform/helm-charts' in tools['gitlab_file'].description and 'only the last part is used' not in tools['gitlab_file'].description
    assert tools['gitlab_mrs'].input_schema['properties']['state']['enum'] == ['opened', 'merged', 'closed', 'all']


def test_gitlab_mrs_lists_the_newest_without_emails_or_descriptions(store: Store):
    # given
    http = routes()

    # when
    out, err = call(gl_box(store, http), 'gitlab_mrs', repo='skopax', state='opened')

    # then
    assert not err
    assert out == {'repo': 'skopax', 'state': 'opened', 'search': None,
                   'mergeRequests': [{'iid': 8, 'title': 'OPS-139 draft thing', 'state': 'opened', 'draft': True, 'author': 'Oscar Green', 'source': 'OPS-139',
                                      'target': 'master', 'created': '2026-10-29T08:00:00Z', 'url': f'{OWN}/-/merge_requests/8'}]}
    q = parse_qs(urlsplit(http.hits(MRS)[0][1]).query)
    assert (q['state'], q['order_by'], q['sort'], q['per_page']) == (['opened'], ['created_at'], ['desc'], ['20'])
    assert 'm@example.com' not in json.dumps(out) and 'long text' not in json.dumps(out)


def test_gitlab_mrs_search_matches_the_title_and_the_exact_source_branch(store: Store):
    # given
    http = routes()

    # when
    out, err = call(gl_box(store, http), 'gitlab_mrs', repo='skopax', search=' OPS-4321 ')

    # then
    assert not err and out['search'] == 'OPS-4321' and out['state'] == 'all'
    assert [m['iid'] for m in out['mergeRequests']] == [7, 5]
    assert out['mergeRequests'][1] == {'iid': 5, 'title': 'Order filters', 'state': 'merged', 'author': 'Oscar Green', 'source': 'OPS-4321', 'target': 'master',
                                        'created': '2026-10-27T06:00:00Z', 'merged': '2026-10-28T07:00:00Z', 'mergedBy': 'Gary', 'url': f'{OWN}/-/merge_requests/5'}
    asked = [parse_qs(urlsplit(c[1]).query) for c in http.hits(MRS)]
    assert asked[0]['search'] == ['OPS-4321'] and asked[0]['in'] == ['title'] and 'state' not in asked[0]
    assert asked[1]['source_branch'] == ['OPS-4321']


def test_a_search_that_is_no_branch_name_runs_only_the_title_search_and_long_ones_are_refused(store: Store):
    # given
    http = routes()
    b = gl_box(store, http)

    # when
    words, err = call(b, 'gitlab_mrs', repo='skopax', search='order filters')
    long = call(b, 'gitlab_mrs', repo='skopax', search='x' * 101)

    # then
    assert not err and len(http.hits(MRS)) == 1
    assert long[1] and 'at most 100 characters' in long[0]


def test_gitlab_pipelines_lists_a_ref_and_reads_one_pipeline_with_jobs_and_children(store: Store):
    # given
    http = routes()
    b = gl_box(store, http)

    # when
    recent, err = call(b, 'gitlab_pipelines', repo='platform/skopax', limit=1)
    one, err2 = call(b, 'gitlab_pipelines', repo='skopax', pipeline=901)

    # then
    assert not err and not err2
    assert recent == {'repo': 'skopax', 'ref': 'master', 'pipelines': [{'id': 901, 'status': 'success', 'ref': 'master', 'sha': 'abcdef01', 'source': 'push',
                                                                                  'created': '2026-10-29T06:57:30Z', 'url': f'{OWN}/-/pipelines/901'}]}
    q = parse_qs(urlsplit(http.hits(PIPES)[0][1]).query)
    assert (q['ref'], q['per_page'], q['order_by'], q['sort']) == (['master'], ['1'], ['id'], ['desc'])
    assert one['pipeline']['duration'] == 412 and one['pipeline']['status'] == 'success'
    assert one['jobs'] == [{'name': 'build', 'stage': 'build', 'status': 'success', 'started': '2026-10-29T06:58:00Z', 'durationS': 95},
                           {'name': 'deploy-prod', 'stage': 'deploy', 'status': 'manual', 'allowFailure': True}]
    assert one['children'] == [{'name': 'trigger-deploy', 'stage': 'deploy', 'status': 'success', 'child': {'id': 902, 'status': 'success', 'url': f'{OWN}/-/pipelines/902'}}]
    assert 'm@example.com' not in json.dumps(one)


def test_gitlab_pipelines_refuses_a_bad_ref_names_a_missing_pipeline_and_is_capped(store: Store):
    # given
    http = routes({('GET', f'{PIPES}/5'): HttpError(404, f'{PIPES}/5')})
    b = gl_box(store, http)

    # when
    bad_ref = call(b, 'gitlab_pipelines', repo='skopax', ref='a..b')
    missing = call(b, 'gitlab_pipelines', repo='skopax', pipeline=5)
    more = [call(b, 'gitlab_pipelines', repo='skopax') for _ in range(CAP)]

    # then
    assert bad_ref[1] and bad_ref[0] == 'ref must be one branch or tag'
    assert missing[1] and missing[0] == 'GitLab found no pipeline 5 in skopax, or the token lacks the read_api scope or a role that can read it, such as Reporter'
    assert [err for _, err in more] == [False] * (CAP - 2) + [True, True], 'the refused ref and the missing pipeline both spent a call'
    assert CAP == 8
    assert 'made its 8 calls in this run' in more[-1][0]


def test_gitlab_search_without_a_repo_searches_the_whole_group_and_names_each_repo(store: Store):
    # given
    hits = [{'project_id': 40, 'path': 'skopax/engine.py', 'startline': 12, 'data': 'logger.warn("Cart total skipped")' + 'x' * 500},
            {'project_id': 999, 'path': 'a.kt', 'startline': 3, 'data': 'y'}]
    http = routes({('GET', f'{GITLAB_API}/groups/acme/search'): hits, ('GET', f'{GITLAB_API}/projects/40/search'): hits[:1]})
    b = gl_box(store, http)

    # when
    group, err = call(b, 'gitlab_search', query='"Cart total skipped"')
    one = call(b, 'gitlab_search', repo='platform/skopax', query='Cart')[0]
    long = call(b, 'gitlab_search', query='x' * 201)

    # then
    assert not err and [(h['repo'], h['path'], h['line']) for h in group] == [('platform/skopax', 'skopax/engine.py', 12), ('999', 'a.kt', 3)]
    assert len(group[0]['text']) == 400
    q = parse_qs(urlsplit(http.hits(f'{GITLAB_API}/groups/acme/search')[0][1]).query)
    assert q['scope'] == ['blobs'] and q['search'] == ['"Cart total skipped"'] and 'ref' not in q
    assert one == [{'path': 'skopax/engine.py', 'line': 12, 'text': group[0]['text']}]
    assert long == ('query must be 1-200 characters', True)
    assert {t.name: t for t in gitlab.PLUGIN.tools}['gitlab_search'].input_schema['required'] == ['query']


def test_a_project_word_means_its_repo_and_a_word_held_by_a_repo_and_its_mirror_copy_picks_the_top_level_one(store: Store, monkeypatch):
    # given
    monkeypatch.setattr(client, 'PROJECT_WORDS', {'be': 'backend'})
    def more(q, b):
        base = listed(q, b)
        return Reply(base.body + [
            {'id': 50, 'path_with_namespace': 'acme/web/shop-backend', 'web_url': 'https://gitlab.com/acme/web/shop-backend'},
            {'id': 51, 'path_with_namespace': 'acme/mirror/web/shop-backend', 'web_url': 'https://gitlab.com/acme/mirror/web/shop-backend'},
            {'id': 60, 'path_with_namespace': 'acme/mobile/shop-ios', 'web_url': 'https://gitlab.com/acme/mobile/shop-ios'},
            {'id': 61, 'path_with_namespace': 'acme/mirror/mobile/shop-ios', 'web_url': 'https://gitlab.com/acme/mirror/mobile/shop-ios'}], {})
    http = routes({('GET', PROJECTS): more, ('GET', f'{GITLAB_API}/projects/50/merge_requests'): [], ('GET', f'{GITLAB_API}/projects/60/merge_requests'): []})
    t = gl_box(store, http)

    # when
    be, be_err = call(t, 'gitlab_mrs', repo='shop-be')
    short, short_err = call(t, 'gitlab_mrs', repo='be')
    ios, ios_err = call(t, 'gitlab_mrs', repo='ios')

    # then
    assert not be_err and not short_err and not ios_err, (be, short, ios)
    assert len(http.hits(f'{GITLAB_API}/projects/50/merge_requests')) == 2 and not http.hits(f'{GITLAB_API}/projects/51/merge_requests')
    assert http.hits(f'{GITLAB_API}/projects/60/merge_requests') and not http.hits(f'{GITLAB_API}/projects/61/merge_requests')


CONFIG = '\n'.join(['spring:', '  datasource:', '    password: hunter2-prod', '    url: https://app:pa55word@db.example.com/shop', '"apiKey": "k-123-abc"',
                    '-----BEGIN RSA PRIVATE KEY-----', 'MIIEowIBAAKCAQEA', '-----END RSA PRIVATE KEY-----', 'clone with glpat over https', 'level: info'])


def test_a_scanned_read_tool_masks_secrets_and_the_instances_own_secret_values(store: Store):
    # given
    content = base64.b64encode(CONFIG.encode()).decode()
    hits = [{'project_id': 40, 'path': 'deploy/values.yml', 'startline': 3, 'data': 'password: hunter2-prod'}]
    http = routes({('GET', f'{GITLAB_API}/projects/40/repository/files/deploy%2Fvalues.yml'): {'content': content},
                   ('GET', f'{GITLAB_API}/groups/acme/search'): hits})
    b = gl_box(store, http)

    # when
    text, err = b.call('gitlab_file', {'repo': 'platform/skopax', 'path': 'deploy/values.yml'})
    found, found_err = call(b, 'gitlab_search', query='password')

    # then
    assert not err and not found_err
    for secret in ('hunter2-prod', 'pa55word', 'k-123-abc', 'MIIEowIBAAKCAQEA', 'glpat'):
        assert secret not in text and secret not in json.dumps(found)
    assert '3:     password: [redacted]' in text and 'level: info' in text and 'clone with *** over https' in text
    assert found == [{'repo': 'platform/skopax', 'path': 'deploy/values.yml', 'line': 3, 'text': 'password: [redacted]'}]
