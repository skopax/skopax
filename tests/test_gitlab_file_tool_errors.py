from skopax.plugins.gitlab.client import GITLAB_API, GitLab
from skopax.remote import HttpError
from tests.fake_http import FakeHttp
from tests.plugin_kit import gitlab_tools
from tests.test_gitlab_plugin import Sources, Store, toolbox

PROJECT = f'{GITLAB_API}/projects/11'
TREE = f'{PROJECT}/repository/tree'
FILE = f'{PROJECT}/repository/files'


def entries(names: list[str], kind: str = 'blob') -> list[dict]:
    return [{'name': n, 'type': kind, 'path': n} for n in names]


def box(store: Store, http: FakeHttp):
    gitlab = GitLab('glpat', http, group='acme')
    return toolbox(store, Sources(gitlab=None, jira=None, plugin_tools=gitlab_tools(gitlab)))


def routes(extra: dict) -> dict:
    base = {('GET', f'{GITLAB_API}/groups/acme/projects'): [{'id': 11, 'path_with_namespace': 'acme/checkout-service', 'web_url': 'https://gitlab.com/acme/checkout-service', 'default_branch': 'main'}]}
    return base | extra


def test_a_path_that_is_a_folder_lists_up_to_20_of_its_entries(store: Store):
    # given
    names = [f'f{i:02d}.yaml' for i in range(30)]
    http = FakeHttp(routes({('GET', f'{FILE}/deploy'): HttpError(404, 'x'), ('GET', TREE): lambda q, _b: entries(names) if q.get('path') == ['deploy'] else HttpError(404, 'x')}))

    # when
    text, err = box(store, http).call('gitlab_file', {'repo': 'checkout-service', 'path': 'deploy'})

    # then
    assert err and 'is not a file' in text, text
    assert 'f00.yaml' in text and 'f19.yaml' in text and 'f20.yaml' not in text, text
    assert 'deploy' in text and 'main' in text


def test_a_wrong_path_lists_the_nearest_existing_parent_directory(store: Store):
    # given
    http = FakeHttp(routes({('GET', f'{FILE}/src%2Fmain%2Fnope%2FA.kt'): HttpError(404, 'x'),
                              ('GET', TREE): lambda q, _b: entries(['A.kt', 'B.kt']) if q.get('path') == ['src/main'] else HttpError(404, 'x')}))

    # when
    text, err = box(store, http).call('gitlab_file', {'repo': 'checkout-service', 'path': 'src/main/nope/A.kt'})

    # then
    assert err and 'src/main' in text and 'A.kt' in text and 'B.kt' in text, text


def test_a_missing_top_level_path_lists_the_repository_root(store: Store):
    # given
    http = FakeHttp(routes({('GET', f'{FILE}/nope.txt'): HttpError(404, 'x'),
                              ('GET', TREE): lambda q, _b: entries(['README.md', 'src'], 'tree') if not q.get('path') or q.get('path') == [''] else HttpError(404, 'x')}))

    # when
    text, err = box(store, http).call('gitlab_file', {'repo': 'checkout-service', 'path': 'nope.txt'})

    # then
    assert err and 'README.md' in text and 'src' in text, text


def test_a_failing_listing_leaves_the_plain_refusal(store: Store):
    # given
    http = FakeHttp(routes({('GET', f'{FILE}/nope.txt'): HttpError(404, 'x'), ('GET', TREE): HttpError(500, 'x')}))

    # when
    text, err = box(store, http).call('gitlab_file', {'repo': 'checkout-service', 'path': 'nope.txt'})

    # then
    assert err and 'is not a file' in text


def test_a_failing_listing_is_attempted_once_for_a_deep_path(store: Store):
    # given
    http = FakeHttp(routes({('GET', f'{FILE}/a%2Fb%2Fc%2Fd.txt'): HttpError(404, 'x'), ('GET', TREE): HttpError(500, 'x')}))

    # when
    text, err = box(store, http).call('gitlab_file', {'repo': 'checkout-service', 'path': 'a/b/c/d.txt'})

    # then
    assert err and 'is not a file' in text
    assert len(http.hits(TREE)) == 1


def test_a_denied_listing_is_attempted_once_for_a_deep_path(store: Store):
    # given
    http = FakeHttp(routes({('GET', f'{FILE}/a%2Fb%2Fd.txt'): HttpError(404, 'x'), ('GET', TREE): HttpError(403, 'x')}))

    # when
    box(store, http).call('gitlab_file', {'repo': 'checkout-service', 'path': 'a/b/d.txt'})

    # then
    assert len(http.hits(TREE)) == 1
