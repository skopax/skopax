from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from skopax.app import create_app
from skopax.codehosts import CodeHosts
from skopax.contract import Missing, SourceDown
from skopax.enrich import Enricher
from skopax.errkind import NoAccess
from skopax.plugins.github.client import GitHub
from skopax.plugins.gitlab.client import GitLab
from skopax.remote import HttpError
from skopax.store import Store
from tests.conftest import AUTH, settings
from tests.fake_http import FakeHttp
from tests.github_kit import CART_WEB, FULL, TOKEN, VALUES, github_http, rate_limited
from tests.test_deploys import deploy
from tests.test_enrich import fake_gitlab

GITLAB_VALUES = {'url': 'https://gitlab.com', 'token': 'glpat', 'group': 'acme'}


class Off:
    enabled = False

    def __getattr__(self, name):
        raise SourceDown('GitLab is not set up or is switched off')


def gitlab_host(http: FakeHttp | None = None) -> GitLab:
    return GitLab('glpat', http or fake_gitlab(), group='acme')


def github_host(http: FakeHttp | None = None) -> GitHub:
    return GitHub(TOKEN, http or github_http(), owner='acme')


def test_one_serving_host_is_passed_through_untouched():
    # given
    gl = gitlab_host()
    hosts = CodeHosts((gl, Off()))

    # when
    found = hosts.repo('checkout-service')

    # then
    assert hosts.enabled and found is gl.repo('checkout-service')
    assert hosts.commit_url(found, 'abc') == gl.commit_url(found, 'abc')
    assert hosts.similar('checkout') == gl.similar('checkout')
    assert hosts.link_problem('https://gitlab.com/other/x') == gl.link_problem('https://gitlab.com/other/x')


def test_no_serving_host_keeps_the_first_hosts_refusal():
    # given
    hosts = CodeHosts((Off(), Off()))

    # then
    assert not hosts.enabled
    with pytest.raises(SourceDown, match='GitLab is not set up'):
        hosts.repo('checkout-service')


def test_a_repo_only_github_knows_resolves_and_reads_there():
    # given
    gl = gitlab_host(FakeHttp({('GET', 'https://gitlab.com/api/v4/groups/acme/projects'): []}))
    gh = github_host()
    hosts = CodeHosts((gl, gh))

    # when
    found = hosts.repo('checkout-service')
    head = hosts.commit(found, '2b2b2b2b')
    by_name = hosts.commits('acme/checkout-service', '1a1a1a1a', FULL)

    # then
    assert found['full'] == 'acme/checkout-service' and head['id'] == FULL
    assert hosts.commit_url(found, FULL) == f'{CART_WEB}/commit/{FULL}'
    assert hosts.compare_url(found, 'a', 'b') == f'{CART_WEB}/compare/a...b'
    assert [c['author_name'] for c in by_name] == ['Nina', 'Adam']


def test_a_repo_both_hosts_know_resolves_on_gitlab_first():
    # given
    gl, gh = gitlab_host(), github_host()
    hosts = CodeHosts((gl, gh))

    # when
    found = hosts.repo('checkout-service')

    # then
    assert found['full'] == 'acme/shop/checkout-service'
    assert hosts.commit_url(found, 'abc') == 'https://gitlab.com/acme/shop/checkout-service/-/commit/abc'
    assert set(hosts.similar('checkout')) >= {'shop/checkout-service', 'acme/checkout-service'}


def sweep_both(store: Store, gh_http: FakeHttp, gl_http: FakeHttp | None = None, svcs: tuple[str, ...] = ('redis', 'checkout-service')) -> tuple[dict, dict]:
    store.add_deploys([deploy(svc, to='3c3c3c3c' if svc != 'checkout-service' else '2b2b2b2b') for svc in svcs])
    hosts = CodeHosts((gitlab_host(gl_http), github_host(gh_http)))
    done = Enricher(store, hosts, SimpleNamespace(enabled=False)).sweep()
    return done, {d['svc']: d for d in store.deploys_since('2000-01-01T00:00:00Z')}


def test_a_paused_github_never_stops_gitlab_enrichment(store: Store):
    # given
    gh = FakeHttp({('GET', 'https://api.github.com/user/repos'): rate_limited('https://api.github.com/user/repos')})

    # when
    done, rows = sweep_both(store, gh)

    # then
    assert rows['checkout-service']['imageBy']['url'].startswith('https://gitlab.com/acme/shop/checkout-service/-/commit/')
    assert not (rows['redis'].get('enriched') or {}) and 'imageBy' not in rows['redis']
    assert done['gitlab'] == 1


def test_an_expired_github_token_never_marks_a_gitlab_deploy(store: Store):
    # given
    gh = FakeHttp({('GET', 'https://api.github.com/user/repos'): HttpError(401, 'https://api.github.com/user/repos')})

    # when
    done, rows = sweep_both(store, gh)

    # then
    assert rows['checkout-service']['imageBy']['url'].startswith('https://gitlab.com/')
    assert all('refused' not in (d.get('enriched') or {}) for d in rows.values())
    assert not (rows['redis'].get('enriched') or {})


def test_a_repo_never_resolves_on_github_while_gitlab_could_not_answer(store: Store):
    # given
    down = FakeHttp({('GET', 'https://gitlab.com/api/v4/groups/acme/projects'): HttpError(502, 'x')})
    gh = github_http()
    hosts = CodeHosts((gitlab_host(down), github_host(gh)))

    # when
    with pytest.raises(SourceDown):
        hosts.repo('checkout-service')
    done, rows = sweep_both(store, github_http(), down, ('checkout-service',))

    # then
    d = rows['checkout-service']
    assert 'imageBy' not in d and not (d.get('enriched') or {})
    assert not gh.calls and done['gitlab'] == 0


def test_a_host_that_failed_is_not_asked_again_for_a_minute(store: Store):
    # given
    clock = {'now': 100.0}
    down = FakeHttp({('GET', 'https://gitlab.com/api/v4/groups/acme/projects'): HttpError(502, 'x')})
    hosts = CodeHosts((gitlab_host(down), github_host()), clock=lambda: clock['now'])

    # when
    for _ in range(3):
        with pytest.raises(SourceDown):
            hosts.repo('checkout-service')
    asked = len(down.calls)
    clock['now'] += 61
    with pytest.raises(SourceDown):
        hosts.repo('checkout-service')

    # then
    assert asked == 1 and len(down.calls) == 2


def test_a_tagged_repo_stays_on_its_host_after_the_other_goes_off():
    # given
    gl, gh = gitlab_host(FakeHttp({('GET', 'https://gitlab.com/api/v4/groups/acme/projects'): []})), github_host()
    hosts = CodeHosts((gl, gh))
    found = hosts.repo('checkout-service')
    gh.token = ''

    # then
    with pytest.raises(SourceDown):
        hosts.commit_url(found, 'abc')


def test_a_refusal_in_similar_is_not_swallowed():
    # given
    denied = FakeHttp({('GET', 'https://gitlab.com/api/v4/groups/acme/projects'): HttpError(403, 'x')})
    hosts = CodeHosts((gitlab_host(denied), github_host()))

    # then
    with pytest.raises(NoAccess):
        hosts.similar('checkout')


def test_each_host_rules_on_links_of_its_own_host():
    # given
    hosts = CodeHosts((gitlab_host(), github_host()))

    # when
    lab, hub, fine = hosts.link_problem('https://gitlab.com/other/x'), hosts.link_problem('https://github.com/other/x'), hosts.link_problem(f'{CART_WEB}/blob/x/a.py')

    # then
    assert 'gitlab.com/acme/' in lab and 'github.com/acme/' in hub and fine is None


def test_a_string_repo_the_hosts_do_not_know_reads_on_the_first_serving_host():
    # given
    gh = github_host(github_http({('GET', 'https://api.github.com/repos/acme/odd'): Missing('x')}))
    hosts = CodeHosts((Off(), gh))
    calls = []
    gh.commits_since = lambda repo, ref, since: calls.append(repo) or []

    # when
    hosts.commits_since('acme/odd', 'main', '2026-10-01T00:00:00Z')

    # then
    assert calls == ['acme/odd']


def test_both_hosts_serve_and_each_deploy_is_read_where_its_repo_lives(store: Store):
    # given
    store.add_deploys([deploy(), deploy('billing', to='3c3c3c3c')])
    gl = fake_gitlab()
    gh = github_http({('GET', 'https://api.github.com/user/repos'): [{'id': 7, 'name': 'billing', 'full_name': 'acme/billing', 'owner': {'login': 'acme'}, 'html_url': 'https://github.com/acme/billing',
                                                                        'default_branch': 'main', 'archived': False}],
                      ('GET', 'https://api.github.com/repos/acme/billing/commits/3c3c3c3c'): {'sha': '3c3c3c3c' + 'c' * 32, 'commit': {'message': 'OPS-7 bill', 'author': {'name': 'Bea', 'date': '2026-10-15T07:00:00Z'}},
                                                                                           'parents': [{'sha': 'p' * 40}]},
                      ('GET', 'https://api.github.com/repos/acme/billing/compare/1a1a1a1a...' + '3c3c3c3c' + 'c' * 32): {'commits': [], 'files': []},
                      ('GET', 'https://api.github.com/repos/acme/billing/compare/1a1a1a1a...3c3c3c3c'): {'commits': [], 'files': []}})

    def both(method, url, headers, data, timeout):
        return (gh if 'github.com' in url else gl)(method, url, headers, data, timeout)

    # when
    with TestClient(create_app(settings(), store, transport=both)) as c:
        c.post('/api/admin/plugins/gitlab/instances', json={'name': 'main', 'values': GITLAB_VALUES, 'enabled': True}, headers=AUTH)
        c.post('/api/admin/plugins/github/instances', json={'name': 'main', 'values': VALUES, 'enabled': True}, headers=AUTH)
        done = c.app.state.enricher.sweep()
    rows = {d['svc']: d for d in store.deploys_since('2000-01-01T00:00:00Z')}

    # then
    assert done['gitlab'] == 2
    assert rows['checkout-service']['imageBy']['url'].startswith('https://gitlab.com/acme/shop/checkout-service/-/commit/')
    assert rows['billing']['imageBy']['url'] == 'https://github.com/acme/billing/commit/' + '3c3c3c3c' + 'c' * 32


def test_a_code_link_of_the_investigator_points_at_github_when_only_github_serves(store: Store):
    # given
    from skopax.investigator.tools import code_link
    from tests.github_kit import CART_API, content_json, blame_json, GRAPHQL
    gh = github_host(github_http({('GET', f'{CART_API}/commits/main'): {'sha': FULL, 'commit': {'message': 'x', 'author': {'name': 'A', 'date': '2026-10-15T07:00:00Z'}}, 'parents': []},
                                  ('GET', f'{CART_API}/contents/src/a.py'): content_json('src/a.py', 'a\nb\nc\n'),
                                  ('POST', GRAPHQL): blame_json([(1, 3, 'd' * 40, 'Adam', 'OPS-2 a')])}))
    hosts = CodeHosts((Off(), gh))

    # when
    link = code_link(hosts, {'repo': 'checkout-service', 'path': 'src/a.py', 'lines': '2-3', 'what': 'here'}, '')

    # then
    assert link['url'] == f'{CART_WEB}/blob/{FULL}/src/a.py#L2-L3'
    assert link['by'][0]['url'] == f'{CART_WEB}/commit/{"d" * 40}' and link['by'][0]['name'] == 'Adam'
