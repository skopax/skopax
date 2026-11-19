import json
import logging
import urllib.error
from types import SimpleNamespace

import mongomock
import pytest
from fastapi.testclient import TestClient

from skopax import remote
from skopax.app import create_app
from skopax.contract import Denied, Missing, ReadError, SourceDown
from skopax.enrich import Enricher
from skopax.instances import Instances, PluginState
from skopax.messages import msg
from skopax.plugins import github
from skopax.plugins.github import track
from skopax.plugins.github.client import GITHUB_API, GITHUB_URL, GitHub
from skopax.plugintools import PluginTools
from skopax.registry import BUILTIN, Registry, describe
from skopax.remote import HttpError, Reply, Unreachable
from skopax.runtime import Runtime
from skopax.sealer import Sealer
from skopax.store import Store
from tests.conftest import AUTH, settings
from tests.fake_http import FakeHttp
from tests.github_kit import (API, CART, CART_API, CART_WEB, FULL, GHES, GHES_API, GHES_GRAPHQL, GRAPHQL, OLD, TOKEN, VALUES, WEB_BASE, blame_json, commit_json, compare_json,
                              content_json, deployment_json, deployment_status_json, github_http, listing, paged, pull_json, rate_limited, release_json, repo_json)
from tests.plugin_kit import KIT_KEY, add_instance
from tests.test_deploys import deploy

NOW = 1_792_224_000.0
USER = 'alice@example.com'
TOOLS = {'github_file': None, 'github_search': None, 'github_blame': None, 'github_compare': None, 'github_prs': 6, 'github_checks': 8, 'github_docs': 8}


class Clock:
    def __init__(self, now: float = 1000.0):
        self.now = now

    def __call__(self) -> float:
        return self.now


def client(http: FakeHttp, clock: Clock | None = None, **kw) -> GitHub:
    return GitHub(TOKEN, http, owner=kw.pop('owner', 'acme'), clock=clock or Clock(), wall=lambda: NOW, **kw)


def github_runtime(store, transport=None, named: str | None = 'main', **values) -> Runtime:
    registry = Registry.of(github.PLUGIN)
    runtime = Runtime(registry, PluginState(store.db), Instances(store.db, registry, lambda: Sealer(KIT_KEY)), transport)
    if named:
        add_instance(store, github.PLUGIN, named, sealer_key=KIT_KEY, primary=True, **(VALUES | values))
    runtime.refresh()
    return runtime


def kit_store() -> SimpleNamespace:
    return SimpleNamespace(db=mongomock.MongoClient(tz_aware=True)['kit'])


def tool(name: str):
    return next(t for t in github.PLUGIN.tools if t.name == name)


def run_tool(gh: GitHub, name: str, **args):
    return tool(name).run(SimpleNamespace(client=lambda: gh, now=NOW, instance=None), args)


def refused(gh: GitHub, name: str, **args) -> ReadError:
    with pytest.raises(ReadError) as e:
        run_tool(gh, name, **args)
    return e.value


def test_github_identity_and_manifest():
    # given
    registry = Registry.of(github.PLUGIN)

    # when
    loaded = registry.get('github')
    manifest = describe(github.PLUGIN, icon=True)

    # then
    assert json.loads(json.dumps(manifest)) == manifest
    assert (loaded.state, loaded.reason) == ('ok', None) and 'skopax.plugins.github' in BUILTIN
    assert Registry.load({}, entry_points=False).get('github').state == 'ok'
    assert (manifest['name'], manifest['category'], manifest['edition'], manifest['maxInstances']) == ('GitHub', 'code', 'community', 1)
    assert manifest['capabilities'] == {'hosts': ['@url', 'api.github.com'], 'secrets': ['token'], 'agentReads': False}
    assert [(f['key'], f['kind'], f['required']) for f in manifest['fields']] == [('url', 'url', True), ('token', 'secret', True), ('owner', 'text', True), ('repos', 'text', False),
                                                                                  ('docs', 'text', False), ('ca', 'pem', False), ('ca-pinned', 'bool', False)]
    assert {t.name: t.per_run for t in github.PLUGIN.tools} == TOOLS
    assert all(t.kind == 'read' and t.instance_input is None for t in github.PLUGIN.tools)
    assert tool('github_docs').needs_field == 'docs'
    assert [k.id for k in github.PLUGIN.knowledge] == ['github'] and github.PLUGIN.env_instances is None


def test_github_defaults_are_neutral():
    # when
    defaults = {f.key: f.default for f in github.PLUGIN.fields}

    # then
    assert defaults == {'url': GITHUB_URL, 'token': '', 'owner': '', 'repos': '', 'docs': '', 'ca': '', 'ca-pinned': 'false'}
    assert GITHUB_URL == 'https://github.com' and GITHUB_API == 'https://api.github.com'
    assert [(c.id, c.period, c.sections) for c in github.PLUGIN.checks] == [('ghdeploy', 300, ('github',))]


def test_the_connection_test_names_the_owner_and_each_failure():
    # given
    owner = {'login': 'acme', 'id': 9, 'type': 'Organization'}
    good = FakeHttp({('GET', f'{API}/user'): {'login': 'skopax-bot'}, ('GET', f'{API}/users/acme'): owner, ('GET', f'{API}/user/repos'): listing('checkout-service')})
    invalid = FakeHttp({('GET', f'{API}/user'): HttpError(401, f'{API}/user')})
    hidden = FakeHttp({('GET', f'{API}/user'): {'login': 'skopax-bot'}})
    down = FakeHttp({('GET', f'{API}/user'): Unreachable('x')})
    limited = FakeHttp({('GET', f'{API}/user'): rate_limited(f'{API}/user', retry='12')})
    ghes = FakeHttp({('GET', f'{GHES_API}/user'): {'login': 'bot'}, ('GET', f'{GHES_API}/users/acme'): owner | {'type': 'User'}, ('GET', f'{GHES_API}/user/repos'): [],
                     ('GET', f'{GHES_API}/meta'): {'installed_version': '3.14.2'}})

    def check(http, **values):
        rt = github_runtime(kit_store(), http, **values)
        return github.test(rt.context(github.PLUGIN, rt.instances.get('github', 'main'), NOW))

    # when
    ok, bad, gone, unreachable, slow, enterprise = check(good), check(invalid), check(hidden), check(down), check(limited), check(ghes, url=GHES)

    # then
    assert (ok.ok, ok.summary) == (True, msg('github.test.ok', owner='acme', kind='organization', count='1 repository'))
    assert (bad.ok, bad.summary) == (False, msg('github.token_invalid', status=401))
    assert (gone.ok, gone.summary) == (False, msg('github.owner_hidden', owner='acme'))
    assert (unreachable.ok, unreachable.summary) == (False, msg('enrich.unreachable', source='GitHub'))
    assert (slow.ok, slow.summary) == (False, msg('github.rate_limited', seconds=12))
    assert (enterprise.ok, enterprise.summary) == (True, msg('github.test.ghes', version='3.14.2', owner='acme', kind='user', count='0 repositories'))
    sent = good.calls[0][2]
    assert sent['Authorization'] == f'Bearer {TOKEN}' and sent['Accept'] == 'application/vnd.github+json' and sent['X-GitHub-Api-Version'] == '2022-11-28'


def test_a_repo_is_found_by_name_or_full_name_under_the_owner_only():
    # given
    http = github_http({('GET', f'{API}/repos/acme/billing'): repo_json('billing', 300)})
    gh = client(http)

    # when
    by_name, by_full, other, direct, case = gh.repo('checkout-service'), gh.repo('acme/checkout-service'), gh.repo('evil/checkout-service'), gh.repo('billing'), gh.repo('ACME/Checkout-Service')

    # then
    assert by_name == {'id': 100, 'web': CART_WEB, 'path': CART, 'full': CART, 'branch': 'main', 'name': 'checkout-service'}
    assert by_full == by_name and case == by_name and other is None
    assert direct['full'] == 'acme/billing'
    assert not http.hits(f'{API}/repos/evil/checkout-service')
    assert gh.similar('checkout') == ['acme/checkout-service']


def test_the_repo_listing_follows_link_pages_on_the_api_host_only():
    # given
    url = f'{API}/user/repos'
    http = FakeHttp({('GET', url): paged(listing('a-service'), listing('b-service', 'c-service'), url)})
    foreign = FakeHttp({('GET', url): lambda q, _: Reply(listing('a-service'), {'link': '<https://evil.example.com/user/repos?page=2>; rel="next"'})})

    # when
    names = sorted(r['name'] for r in client(http).repos())
    one = sorted(r['name'] for r in client(foreign).repos())

    # then
    assert names == ['a-service', 'b-service', 'c-service'] and len(http.calls) == 2
    assert one == ['a-service'] and len(foreign.calls) == 1


def test_a_renamed_repo_is_followed_on_the_same_host_and_never_to_another():
    # given
    old = f'{API}/repos/acme/old-cart'
    moved = HttpError(301, old, '{"message": "Moved Permanently"}', {'location': f'{API}/repositories/100'})
    away = HttpError(301, old, '', {'location': 'https://evil.example.com/repositories/100'})
    http = github_http({('GET', old): moved, ('GET', f'{API}/repositories/100'): repo_json('checkout-service', 100)})
    other = github_http({('GET', old): away})

    # when
    found = client(http).repo('old-cart')
    lost = client(other).repo('old-cart')

    # then
    assert found['full'] == CART and found['path'] == CART
    assert lost is None and not [c for c in other.calls if 'evil' in c[1]]


def test_a_private_repo_the_token_cannot_see_is_a_clear_message():
    # given
    gh = client(github_http())

    # when
    error = refused(gh, 'github_file', repo='secret-repo', path='a.py')

    # then
    assert error.status == 404
    assert error.message.startswith(msg('github.no_repo', repo='secret-repo', owner='acme', permission=github.client.PERMISSIONS['repositories']))
    assert 'cannot see it' in error.message


def test_a_rate_limit_pauses_every_read_until_retry_after_and_is_never_a_permission_refusal():
    # given
    clock = Clock()
    url = f'{CART_API}/commits/abc12345'
    state = {'n': 0}

    def answer(_q, _b):
        state['n'] += 1
        return rate_limited(url) if state['n'] == 1 else commit_json('abc12345' * 5, 'x', 'Nina')

    gh = client(github_http({('GET', url): answer}), clock)
    repo = gh.repo('checkout-service')

    # when
    with pytest.raises(SourceDown) as first:
        gh.commit(repo, 'abc12345')
    with pytest.raises(SourceDown) as held:
        gh.commit(repo, 'abc12345')
    clock.now += 31
    later = gh.commit(repo, 'abc12345')

    # then
    assert not isinstance(first.value, Denied) and first.value.text == msg('github.rate_limited', seconds=30)
    assert held.value.text == msg('github.rate_limited', seconds=30) and state['n'] == 2
    assert later['id'] == 'abc12345' * 5


@pytest.mark.parametrize('error, wait', [
    pytest.param(rate_limited(f'{CART_API}/commits/x', code=403, retry=None, remaining='0', reset=str(int(NOW) + 600)), 600, id='primary-403-reset'),
    pytest.param(rate_limited(f'{CART_API}/commits/x', code=403, retry=None, body='{"message": "You have exceeded a secondary rate limit."}'), 60, id='secondary-403'),
    pytest.param(rate_limited(f'{CART_API}/commits/x', code=403, retry='45'), 45, id='retry-after-403'),
    pytest.param(rate_limited(f'{CART_API}/commits/x', code=429, retry='abc'), 60, id='odd-retry-after'),
    pytest.param(rate_limited(f'{CART_API}/commits/x', code=429, retry='99999'), 3600, id='capped'),
])
def test_both_github_rate_limits_read_as_a_pause(error, wait):
    # given
    gh = client(github_http({('GET', f'{CART_API}/commits/x'): error}))

    # when
    with pytest.raises(SourceDown) as e:
        gh.commit(gh.repo('checkout-service'), 'x')

    # then
    assert not isinstance(e.value, Denied) and e.value.text == msg('github.rate_limited', seconds=wait)


def test_a_plain_403_is_a_permission_refusal_that_names_the_part():
    # given
    url = f'{CART_API}/pulls'
    gh = client(github_http({('GET', url): HttpError(403, url, '{"message": "Resource not accessible by personal access token"}')}))

    # when
    error = refused(gh, 'github_prs', repo='checkout-service')

    # then
    assert error.status == 403
    assert error.message == msg('github.denied', part='pull requests', status=403, permission=github.client.PERMISSIONS['pull requests'])


def test_the_send_keeps_the_answer_headers_of_a_refusal(monkeypatch):
    # given
    def urlopen(req, timeout, context=None):
        raise urllib.error.HTTPError('https://api.example.test/x', 429, 'Too Many', {'Retry-After': '30', 'X-RateLimit-Remaining': '0'}, None)

    monkeypatch.setattr(remote, 'open_url', urlopen)

    # when
    with pytest.raises(HttpError) as e:
        remote.send('GET', 'https://api.example.test/x?q=1', {}, None, 5)

    # then
    assert e.value.code == 429 and e.value.headers == {'retry-after': '30', 'x-ratelimit-remaining': '0'}
    assert HttpError(404, 'x').headers == {}


def test_the_token_never_reaches_a_log_line_or_an_error_text(caplog):
    # given
    url = f'{CART_API}/commits/2b2b2b2b'
    texts = []
    gh = client(github_http({('GET', url): HttpError(403, url, f'{{"message": "bad credentials {TOKEN}"}}'), ('GET', f'{CART_API}/pulls'): rate_limited(f'{CART_API}/pulls')}))
    store = Store(mongomock.MongoClient(tz_aware=True)['s'])
    store.add_deploys([deploy()])

    # when
    with caplog.at_level(logging.DEBUG):
        texts.append(str(Enricher(store, gh, SimpleNamespace(enabled=False)).sweep()))
        for name, args in (('github_file', {'repo': 'nope', 'path': 'a'}), ('github_prs', {'repo': 'checkout-service'}), ('github_compare', {'repo': 'checkout-service', 'from': 'a', 'to': 'b'})):
            try:
                run_tool(gh, name, **args)
            except Exception as e:
                texts.append(str(e) + getattr(e, 'message', ''))

    # then
    assert texts and all(TOKEN not in t for t in texts)
    assert all(TOKEN not in r.getMessage() for r in caplog.records)


def test_enterprise_server_reads_its_own_api_and_links_under_its_own_host():
    # given
    web = f'{GHES}/acme/checkout-service'
    http = FakeHttp({('GET', f'{GHES_API}/user/repos'): [repo_json('checkout-service', 100, base=GHES)],
                     ('GET', f'{GHES_API}/repos/acme/checkout-service/commits/2b2b2b2b'): commit_json(FULL, 'OPS-1 fix', 'Nina', web=web),
                     ('POST', GHES_GRAPHQL): blame_json([(1, 4, 'd' * 40, 'Adam', 'OPS-2 a')])})
    gh = client(http, base=f'{GHES}/')

    # when
    repo = gh.repo('checkout-service')
    sha = gh.commit(repo, '2b2b2b2b')['id']
    links = (gh.commit_url(repo, sha), gh.compare_url(repo, OLD, sha), gh.file_url(repo, sha, 'src/a.py', 3, 3), gh.file_url(repo, sha, 'src/a.py', 3, 9))
    chunks = gh.blame(repo, 'src/a.py', sha, 1, 2)

    # then
    assert (gh.api, gh.graphql, gh.host) == (GHES_API, GHES_GRAPHQL, 'git.example.com')
    assert links == (f'{web}/commit/{FULL}', f'{web}/compare/{OLD}...{FULL}', f'{web}/blob/{FULL}/src/a.py#L3', f'{web}/blob/{FULL}/src/a.py#L3-L9')
    assert chunks[0]['lines'] == [1, 2]
    assert gh.link_problem(f'{GHES}/acme/checkout-service/blob/x/a.py') is None
    assert gh.link_problem(f'{GHES}/other/repo/blob/x/a.py') == msg('github.link_outside', host='git.example.com', prefix='/acme/', url=f'{GHES}/other/repo/blob/x/a.py')
    assert gh.link_problem('https://github.com/other/repo') is None


def test_github_com_links_stay_under_the_owner():
    # given
    gh = client(github_http())

    # when
    inside, outside, dotted, foreign = (gh.link_problem(u) for u in (f'{CART_WEB}/blob/x/a.py', 'https://github.com/evil/repo', 'https://github.com/acme/../evil/x', 'https://gitlab.com/x'))

    # then
    assert inside is None and foreign is None
    assert outside and dotted and 'github.com/acme/' in outside


def test_commits_and_the_compare_range_take_the_neutral_shape_without_merges():
    # given
    gh = client(github_http())
    repo = gh.repo('checkout-service')

    # when
    head = gh.commit(repo, '2b2b2b2b')
    between = gh.commits(repo, '1a1a1a1a', FULL)

    # then
    assert head == {'id': FULL, 'short_id': FULL[:8], 'title': 'Merge pull request #12 from acme/OPS-115', 'message': 'Merge pull request #12 from acme/OPS-115\n\nOPS-115 bundle calc',
                    'author_name': 'Oscar', 'committed_date': '2026-10-15T07:01:02Z', 'authored_date': '2026-10-15T07:01:02Z', 'parent_ids': ['0' * 40, '1' * 40],
                    'web_url': f'{CART_WEB}/commit/{FULL}'}
    assert [c['title'] for c in between] == ['OPS-115 bundle calc', 'WEB-203 bot session, see OPS-106']


def test_blame_reads_graphql_and_keeps_the_asked_lines_only():
    # given
    http = github_http({('POST', GRAPHQL): blame_json([(1, 3, 'a' * 40, 'Adam', 'OPS-1 one\nbody'), (4, 9, 'b' * 40, 'Nina', 'OPS-2 two'), (10, 12, 'c' * 40, 'Oscar', 'x')])})
    gh = client(http)
    repo = gh.repo('checkout-service')

    # when
    chunks = gh.blame(repo, 'src/a.py', FULL, 3, 5)
    rows = run_tool(gh, 'github_blame', repo='checkout-service', path='src/a.py', lines='3-5', ref=FULL)

    # then
    assert [(c['commit']['id'], c['lines']) for c in chunks] == [('a' * 40, [3]), ('b' * 40, [4, 5])]
    assert chunks[0]['commit'] == {'id': 'a' * 40, 'author_name': 'Adam', 'committed_date': '2026-10-01T10:00:00Z', 'authored_date': '2026-10-01T09:00:00Z', 'message': 'OPS-1 one\nbody'}
    sent = http.hits(GRAPHQL)[0]
    assert sent[3]['variables'] == {'owner': 'acme', 'name': 'checkout-service', 'ref': FULL, 'path': 'src/a.py'}
    assert rows == [{'sha': 'aaaaaaaa', 'author': 'Adam', 'date': '2026-10-01', 'title': 'OPS-1 one', 'lines': 1}, {'sha': 'bbbbbbbb', 'author': 'Nina', 'date': '2026-10-01', 'title': 'OPS-2 two', 'lines': 2}]


@pytest.mark.parametrize('answer, raised', [
    pytest.param({'data': {'repository': {'object': None}}}, Missing, id='no-ref'),
    pytest.param({'data': {'repository': None}, 'errors': [{'type': 'NOT_FOUND', 'message': 'Could not resolve'}]}, Missing, id='not-found'),
    pytest.param({'data': None, 'errors': [{'type': 'FORBIDDEN', 'message': 'Resource not accessible'}]}, Denied, id='forbidden'),
    pytest.param({'data': None, 'errors': [{'type': 'RATE_LIMITED', 'message': 'API rate limit exceeded'}]}, SourceDown, id='rate'),
])
def test_a_graphql_blame_error_maps_like_a_rest_one(answer, raised):
    # given
    gh = client(github_http({('POST', GRAPHQL): answer}))

    # then
    with pytest.raises(raised):
        gh.blame(gh.repo('checkout-service'), 'a.py', FULL, 1, 2)


def test_the_file_tool_reads_the_deployed_ref_with_line_numbers():
    # given
    url = f'{CART_API}/contents/src/app/a.py'
    http = github_http({('GET', url): content_json('src/app/a.py', 'one\ntwo\nthree\n'), ('GET', f'{CART_API}/contents/src'): [content_json('src/x', 'x')]})
    gh = client(http)

    # when
    text = run_tool(gh, 'github_file', repo='checkout-service', path='src/app/a.py', lines='2-3', ref=FULL[:8])
    folder = refused(gh, 'github_file', repo='checkout-service', path='src')

    # then
    assert text == '2: two\n3: three'
    assert http.hits(url)[0][1].endswith('?ref=2b2b2b2b')
    assert folder.status == 404 and 'not a file' in folder.message


def test_the_search_tool_stays_inside_the_owner():
    # given
    url = f'{API}/search/code'
    hits = {'total_count': 2, 'items': [
        {'name': 'a.py', 'path': 'src/a.py', 'repository': {'full_name': CART}, 'html_url': f'{CART_WEB}/blob/x/src/a.py', 'text_matches': [{'fragment': 'raise CartError("total skipped")'}]},
        {'name': 'b.py', 'path': 'b.py', 'repository': {'full_name': 'evil/thing'}, 'html_url': 'https://github.com/evil/thing/blob/x/b.py', 'text_matches': [{'fragment': 'x'}]}]}
    http = github_http({('GET', url): hits, ('GET', f'{API}/users/acme'): {'login': 'acme', 'type': 'Organization'}})
    gh = client(http)

    # when
    wide = run_tool(gh, 'github_search', query='"total skipped"')
    narrow = run_tool(gh, 'github_search', query='CartError', repo='checkout-service')
    widened = [refused(gh, 'github_search', query=q) for q in ('x repo:evil/thing', 'x ORG:evil', 'x user:evil', '"open')]

    # then
    assert wide == [{'repo': CART, 'path': 'src/a.py', 'line': None, 'text': 'raise CartError("total skipped")'}]
    assert narrow == [{'path': 'src/a.py', 'line': None, 'text': 'raise CartError("total skipped")'}]
    queries = [c[1] for c in http.hits(url)]
    assert 'org%3Aacme' in queries[0] and 'repo%3Aacme%2Fcheckout-service' in queries[1]
    assert http.hits(url)[0][2]['Accept'] == 'application/vnd.github.text-match+json'
    assert all(e.status == 400 for e in widened)


def test_the_compare_tool_lists_commits_files_and_pull_requests():
    # given
    url = f'{CART_API}/compare/1a1a1a1a...2b2b2b2b'
    files = [{'filename': 'src/a.py', 'status': 'modified', 'additions': 2, 'deletions': 1, 'patch': '@@ -1 +1,2 @@\n-a\n+b\n+c'},
             {'filename': 'src/new.py', 'status': 'renamed', 'previous_filename': 'src/old.py', 'additions': 0, 'deletions': 0},
             {'filename': 'img.png', 'status': 'added', 'additions': 0, 'deletions': 0}]
    body = compare_json([commit_json('c1' * 20, 'OPS-1 one', 'Adam', date='2026-10-15T06:00:00Z'), commit_json('c3' * 20, 'Merge pull request #12', 'Oscar', 2, date='2026-10-15T08:00:00Z')], files)
    http = github_http({('GET', url): body, ('GET', f'{CART_API}/commits/{"c3" * 20}/pulls'): [pull_json(12, 'OPS-115 bundle calc', 'OPS-115')],
                        ('GET', f'{CART_API}/commits/{"c1" * 20}/pulls'): [pull_json(12, 'OPS-115 bundle calc', 'OPS-115')]})
    gh = client(http)

    # when
    out = run_tool(gh, 'github_compare', repo='checkout-service', **{'from': 'registry.example.com/shop/checkout-service:1a1a1a1a', 'to': '2b2b2b2b'})
    one = run_tool(gh, 'github_compare', repo='checkout-service', path='src/a.py', **{'from': '1a1a1a1a', 'to': '2b2b2b2b'})
    none = run_tool(gh, 'github_compare', repo='checkout-service', path='README.md', **{'from': '1a1a1a1a', 'to': '2b2b2b2b'})

    # then
    assert out['url'] == body['html_url'] and [c['title'] for c in out['commits']] == ['Merge pull request #12', 'OPS-1 one']
    assert out['files'] == [{'path': 'src/a.py', 'status': 'modified', 'added': 2, 'deleted': 1}, {'path': 'src/new.py', 'status': 'renamed', 'from': 'src/old.py', 'added': 0, 'deleted': 0},
                            {'path': 'img.png', 'status': 'added', 'added': 0, 'deleted': 0}]
    assert out['pullRequests'] == [{'number': 12, 'title': 'OPS-115 bundle calc', 'url': f'{CART_WEB}/pull/12', 'author': 'nina'}]
    assert one['file']['diff'] == '@@ -1 +1,2 @@\n-a\n+b\n+c'
    assert none['file'] is None and none['changed'] == ['src/a.py', 'src/new.py', 'img.png']


def test_the_pull_request_tool_finds_a_ticket_by_title_or_branch_and_tells_merged_from_closed():
    # given
    pulls = f'{CART_API}/pulls'
    search = f'{API}/search/issues'

    def listed(q, _):
        if q.get('head') == ['acme:OPS-4321']:
            return [pull_json(5, 'Order filters', 'OPS-4321')]
        return [pull_json(8, 'OPS-139 draft', 'OPS-139', state='open', merged=None), pull_json(7, 'OPS-9 closed', 'OPS-9', merged=None), pull_json(6, 'OPS-8 merged', 'OPS-8')]

    found = {'total_count': 1, 'items': [{'number': 9, 'title': 'OPS-4321 filters', 'state': 'closed', 'draft': False, 'user': {'login': 'oscar'}, 'created_at': '2026-10-16T09:00:00Z',
                                          'html_url': f'{CART_WEB}/pull/9', 'repository_url': f'{API}/repos/{CART}',
                                          'pull_request': {'merged_at': '2026-10-16T10:00:00Z', 'html_url': f'{CART_WEB}/pull/9'}}]}
    http = github_http({('GET', pulls): listed, ('GET', search): found})
    gh = client(http)

    # when
    merged = run_tool(gh, 'github_prs', repo='checkout-service', state='merged')
    closed = run_tool(gh, 'github_prs', repo='checkout-service', state='closed')
    ticket = run_tool(gh, 'github_prs', repo='checkout-service', search='OPS-4321')

    # then
    assert [r['number'] for r in merged['pullRequests']] == [6] and merged['pullRequests'][0]['state'] == 'merged'
    assert [r['number'] for r in closed['pullRequests']] == [7]
    assert [r['number'] for r in ticket['pullRequests']] == [9, 5]
    assert 'is%3Apr' in http.hits(search)[0][1] and 'repo%3Aacme%2Fcheckout-service' in http.hits(search)[0][1]
    assert 'secret plan' not in json.dumps(ticket)


def test_the_checks_tool_reads_check_runs_and_statuses_of_one_commit():
    # given
    runs = {'total_count': 2, 'check_runs': [
        {'id': 1, 'name': 'build', 'status': 'completed', 'conclusion': 'success', 'started_at': '2026-10-15T07:00:00Z', 'completed_at': '2026-10-15T07:04:00Z',
         'html_url': f'{CART_WEB}/runs/1', 'app': {'slug': 'github-actions', 'name': 'GitHub Actions'}, 'output': {'text': 'secret log'}},
        {'id': 2, 'name': 'deploy prod', 'status': 'waiting', 'conclusion': None, 'started_at': None, 'completed_at': None, 'html_url': f'{CART_WEB}/runs/2', 'app': {'name': 'GitHub Actions'}}]}
    status = {'state': 'failure', 'sha': FULL, 'total_count': 1, 'statuses': [{'context': 'ci/legacy', 'state': 'failure', 'description': 'tests failed', 'target_url': 'https://ci.example.com/7',
                                                                              'updated_at': '2026-10-15T07:05:00Z'}]}
    http = github_http({('GET', f'{CART_API}/commits/main/check-runs'): runs, ('GET', f'{CART_API}/commits/main/status'): status})
    gh = client(http)

    # when
    out = run_tool(gh, 'github_checks', repo='checkout-service')
    bad = refused(gh, 'github_checks', repo='checkout-service', ref='a..b')

    # then
    assert out['repo'] == 'checkout-service' and out['ref'] == 'main' and out['state'] == 'failure'
    assert out['checks'] == [{'name': 'build', 'status': 'completed', 'conclusion': 'success', 'app': 'GitHub Actions', 'started': '2026-10-15T07:00:00Z', 'durationS': 240, 'url': f'{CART_WEB}/runs/1'},
                             {'name': 'deploy prod', 'status': 'waiting', 'app': 'GitHub Actions', 'url': f'{CART_WEB}/runs/2'}]
    assert out['statuses'] == [{'context': 'ci/legacy', 'state': 'failure', 'description': 'tests failed', 'updated': '2026-10-15T07:05:00Z', 'url': 'https://ci.example.com/7'}]
    assert 'secret log' not in json.dumps(out) and bad.status == 400


def test_the_docs_tool_reads_only_the_docs_folder_and_masks_secrets():
    # given
    url = f'{API}/search/code'
    hits = {'items': [{'path': 'runbooks/cart.md', 'repository': {'full_name': 'acme/handbook'}, 'html_url': 'https://github.com/acme/handbook/blob/main/runbooks/cart.md',
                       'text_matches': [{'fragment': 'restart the cart'}]},
                      {'path': 'src/x.py', 'repository': {'full_name': 'acme/handbook'}, 'html_url': 'x', 'text_matches': [{'fragment': 'y'}]}]}
    page = f'{API}/repos/acme/handbook/contents/runbooks/cart.md'
    http = FakeHttp({('GET', f'{API}/user/repos'): [repo_json('handbook', 5)], ('GET', url): hits,
                     ('GET', page): content_json('runbooks/cart.md', '# Cart\npassword=hunter2hunter2\nRestart it.')})
    gh = client(http, docs='acme/handbook/runbooks')
    none = client(http)

    # when
    found = run_tool(gh, 'github_docs', op='search', text='cart restart')
    read = run_tool(gh, 'github_docs', op='read', path='runbooks/cart.md')
    outside = [refused(gh, 'github_docs', op='read', path=p) for p in ('src/x.py', 'runbooks/../src/x.md', 'other/a.md')]
    off = refused(none, 'github_docs', op='search', text='cart')

    # then
    assert found == [{'path': 'runbooks/cart.md', 'url': 'https://github.com/acme/handbook/blob/main/runbooks/cart.md', 'text': 'restart the cart'}]
    assert 'path%3Arunbooks' in http.hits(url)[0][1] and 'repo%3Aacme%2Fhandbook' in http.hits(url)[0][1]
    assert read['path'] == 'runbooks/cart.md' and 'Restart it.' in read['text'] and 'hunter2hunter2' not in read['text']
    assert all(e.status == 403 for e in outside) and off.status == 403


def test_the_github_tools_are_offered_only_while_github_serves_and_name_the_owner():
    # given
    store = kit_store()
    runtime = github_runtime(store, FakeHttp(), named=None)
    tools = PluginTools(runtime)

    # when
    before = [n for n in tools.catalog() if n.startswith('github_') and tools.cap(n) is not None]
    runtime.instances.create('github', 'main', VALUES, True, USER)
    shown = {d['name']: d['description'] for d in tools.definitions() if d['name'].startswith('github_')}
    runtime.instances.set_enabled('github', 'main', False, USER)
    after = [d['name'] for d in tools.definitions() if d['name'].startswith('github_')]

    # then
    assert before == [] and after == []
    assert sorted(shown) == sorted(n for n in TOOLS if n != 'github_docs')
    assert all('acme' in text and '{field:' not in text for text in shown.values())


def tracked_http(state: str = 'failure') -> FakeHttp:
    deployments = [deployment_json(42, FULL, at='2026-10-15T08:00:00Z'), deployment_json(41, OLD, at='2026-10-14T08:00:00Z'),
                   deployment_json(40, OLD, env='staging', at='2026-10-14T07:00:00Z')]
    return github_http({
        ('GET', f'{CART_API}/deployments'): deployments,
        ('GET', f'{CART_API}/deployments/42/statuses'): [deployment_status_json(state)],
        ('GET', f'{CART_API}/deployments/41/statuses'): [deployment_status_json('success', at='2026-10-14T08:02:00Z')],
        ('GET', f'{CART_API}/deployments/40/statuses'): [deployment_status_json('success', at='2026-10-14T07:02:00Z')],
        ('GET', f'{CART_API}/releases'): [release_json(8, 'v1.4.0', 'Spring'), release_json(7, 'v1.3.0', 'Winter', at='2026-10-01T09:00:00Z'),
                                          release_json(9, 'v1.5.0', 'Draft', draft=True)],
        ('GET', f'{CART_API}/commits/{FULL}/pulls'): [pull_json(12, 'OPS-115 bundle calc', 'OPS-115')],
    })


def ctx_of(gh: GitHub, previous: list | None = None, now: float = NOW, repos: str = 'checkout-service') -> SimpleNamespace:
    info = SimpleNamespace(name='main', primary=True, segment=None, config={'url': WEB_BASE, 'owner': 'acme', 'repos': repos})
    return SimpleNamespace(client=lambda: gh, now=now, instance=info, previous=lambda section: previous)


def test_the_check_tracks_deployments_and_releases_and_raises_a_failed_deploy_with_its_links():
    # given
    http = tracked_http()
    gh = client(http)
    now = 1_792_065_600.0

    # when
    sweep = track.run(ctx_of(gh, now=now))
    again = track.run(ctx_of(gh, sweep.sections['github'], now=now))
    card = sweep.result.found[0]
    links = github.PLUGIN.links(card, SimpleNamespace(config={'url': WEB_BASE, 'owner': 'acme'}, name='main'))
    rows = github.PLUGIN.releases(sweep.sections['github'], now - 30 * 86400)

    # then
    row = sweep.sections['github'][0]
    assert row['repo'] == CART and [d['id'] for d in row['deployments']] == [42, 41, 40]
    assert [(d['state'], d['from']) for d in row['deployments']] == [('failure', OLD), ('success', None), ('success', None)]
    assert [r['tag'] for r in row['releases']] == ['v1.4.0', 'v1.3.0']
    assert [c['key'] for c in sweep.result.found] == ['github-deploy|github/checkout-service|production']
    assert card['sev'] == 'warn' and card['github'] == {'repo': CART, 'sha': FULL, 'pull': 12, 'deployment': 42}
    assert links == [{'label': 'Commit 2b2b2b2b', 'url': f'{CART_WEB}/commit/{FULL}'}, {'label': 'Pull request #12', 'url': f'{CART_WEB}/pull/12'},
                     {'label': 'Deployments', 'url': f'{CART_WEB}/deployments'}]
    assert len(http.hits(f'{CART_API}/deployments/41/statuses')) == 1 and len(http.hits(f'{CART_API}/deployments/42/statuses')) == 1
    assert len(http.hits(f'{CART_API}/commits/{FULL}/pulls')) == 1
    assert {(r['id'], r['ns'], r['svc'], r['fromImage'], r['toImage']) for r in rows} == {
        ('github-acme/checkout-service-d42', 'github', 'checkout-service', '1a1a1a1a', '2b2b2b2b'),
        ('github-acme/checkout-service-d41', 'github', 'checkout-service', '?', '1a1a1a1a'),
        ('github-acme/checkout-service-d40', 'github', 'checkout-service', '?', '1a1a1a1a'),
        ('github-acme/checkout-service-r8', 'github', 'checkout-service', 'v1.3.0', 'v1.4.0'),
        ('github-acme/checkout-service-r7', 'github', 'checkout-service', '?', 'v1.3.0')}
    assert again.result.ok and again.result.found[0]['key'] == card['key']


def test_the_check_reads_nothing_without_repos_and_refuses_links_outside_the_owner():
    # given
    http = tracked_http('success')
    gh = client(http)

    # when
    idle = track.run(ctx_of(gh, repos=''))
    idle_calls = len(http.calls)
    fine = track.run(ctx_of(gh))
    odd = github.PLUGIN.links({'src': 'ghdeploy', 'github': {'repo': 'evil/x', 'sha': FULL, 'pull': 1}}, SimpleNamespace(config={'url': WEB_BASE, 'owner': 'acme'}, name='main'))
    foreign = github.PLUGIN.links({'src': 'prom'}, SimpleNamespace(config={'url': WEB_BASE, 'owner': 'acme'}, name='main'))

    # then
    assert idle.result.ok and idle.sections == {} and idle_calls == 0
    assert fine.result.found == [] and odd == [] and foreign is None


def test_the_check_names_a_refused_part_on_an_access_card_and_a_rate_limit_as_an_error():
    # given
    url = f'{CART_API}/deployments'
    denied = client(github_http({('GET', url): HttpError(403, url)}))
    limited = client(github_http({('GET', url): rate_limited(url)}))

    # when
    refused_pass = track.run(ctx_of(denied))
    limited_pass = track.run(ctx_of(limited))

    # then
    assert refused_pass.result.ok and refused_pass.result.found[0]['sig'] == 'no-access'
    assert not limited_pass.result.ok and msg('github.rate_limited', seconds=30) in limited_pass.result.status


def test_enrichment_reads_a_deploy_through_github_alone(store: Store):
    # given
    store.add_deploys([deploy()])
    http = github_http()

    # when
    with TestClient(create_app(settings(), store, transport=http)) as c:
        added = c.post('/api/admin/plugins/github/instances', json={'name': 'main', 'values': VALUES, 'enabled': True}, headers=AUTH)
        done = c.app.state.enricher.sweep()
        me = c.get('/api/me', headers=AUTH).json()
    d = store.deploys_since('2000-01-01T00:00:00Z')[0]

    # then
    assert added.status_code == 201, added.text
    assert done['gitlab'] == 1 and me['gitlab'] is True
    assert d['imageBy']['url'] == f'{CART_WEB}/commit/{FULL}' and d['imageBy']['compare'] == f'{CART_WEB}/compare/1a1a1a1a...{FULL}'
    assert d['imageBy']['names'] == ['Nina', 'Adam'] and d['imageBy']['commits'] == 2


def test_a_string_repo_of_another_owner_is_never_read():
    # given
    http = github_http()
    gh = client(http)

    # when
    raised = []
    for read in (lambda: gh.commits_since('evil/thing', 'main', '2026-10-01T00:00:00Z'), lambda: gh.commit('evil/thing', 'abc'), lambda: gh.blame('evil/thing', 'a.py', 'main', 1, 2),
                 lambda: gh.file('../x', 'a.py', 'main')):
        with pytest.raises(Missing):
            read()
        raised.append(True)
    by_name = gh.commit('checkout-service', '2b2b2b2b')

    # then
    assert len(raised) == 4 and not [c for c in http.calls if 'evil' in c[1] or GRAPHQL in c[1]]
    assert by_name['id'] == FULL


def test_the_checks_tool_keeps_the_readable_half_when_one_is_refused():
    # given
    runs_url, status_url = f'{CART_API}/commits/main/check-runs', f'{CART_API}/commits/main/status'
    status = {'state': 'success', 'sha': FULL, 'statuses': [{'context': 'ci/legacy', 'state': 'success', 'updated_at': '2026-10-15T07:05:00Z'}]}
    gh = client(github_http({('GET', runs_url): HttpError(403, runs_url), ('GET', status_url): status}))

    # when
    out = run_tool(gh, 'github_checks', repo='checkout-service')

    # then
    assert out['statuses'][0]['context'] == 'ci/legacy' and out['checks'] == []
    assert msg('github.denied', part='checks', status=403, permission=github.client.PERMISSIONS['checks']) in out['note']


def test_the_compare_tool_reads_every_page_so_the_newest_commits_come_first():
    # given
    url = f'{CART_API}/compare/1a1a1a1a...2b2b2b2b'
    old = [commit_json(f'{i:02d}' * 20, f'old {i}', 'Adam', date=f'2026-10-01T{i % 24:02d}:00:00Z') for i in range(3)]
    new = [commit_json('ff' * 20, 'newest', 'Nina', date='2026-10-15T08:00:00Z')]

    def answer(q, _):
        if q.get('page', ['1']) == ['1']:
            return Reply(compare_json(old) | {'total_commits': 4}, {'link': f'<{url}?per_page=100&page=2>; rel="next"'})
        return Reply(compare_json(new) | {'total_commits': 4}, {})

    gh = client(github_http({('GET', url): answer, ('GET', f'{CART_API}/commits/{"ff" * 20}/pulls'): []}))

    # when
    out = run_tool(gh, 'github_compare', repo='checkout-service', **{'from': '1a1a1a1a', 'to': '2b2b2b2b'})

    # then
    assert out['commits'][0]['title'] == 'newest' and len(out['commits']) == 4 and 'commitCount' not in out


def test_a_card_whose_pull_request_read_failed_reads_it_again_next_pass():
    # given
    url = f'{CART_API}/commits/{FULL}/pulls'
    state = {'n': 0}

    def pulls(_q, _b):
        state['n'] += 1
        return HttpError(502, url) if state['n'] == 1 else [pull_json(12, 'OPS-115', 'OPS-115')]

    http = tracked_http()
    http.routes[('GET', url)] = pulls
    gh = client(http)
    now = 1_792_065_600.0

    # when
    first = track.run(ctx_of(gh, now=now))
    second = track.run(ctx_of(gh, first.sections['github'], now=now))

    # then
    assert first.result.found[0]['github']['pull'] is None
    assert second.result.found[0]['github']['pull'] == 12 and state['n'] == 2


def test_an_mcp_instance_named_github_still_serves_with_the_github_plugin_loaded():
    # given
    from pathlib import Path
    from skopax.instances import check_name
    from skopax.plugins import mcp_tools
    registry = Registry.of(github.PLUGIN, mcp_tools.PLUGIN)
    docs = (Path(__file__).parent.parent / 'docs' / 'plugins.md').read_text(encoding='utf-8')
    reserved = docs.split('### Reserved instance names', 1)[1].split('###', 1)[0]

    # when
    for name in ('github', 'github-tools', 'main'):
        check_name(registry, name, 'mcp-tools')

    # then
    assert [c.id for c in github.PLUGIN.checks] == ['ghdeploy'] and github.PLUGIN.checks[0].ran == ('ghdeploy',)
    assert '`ghdeploy`' in reserved
    with pytest.raises(Exception):
        check_name(registry, 'ghdeploy', 'mcp-tools')


def test_a_backslash_anywhere_in_a_search_is_refused():
    # given
    gh = client(github_http())

    # when
    errors = [refused(gh, 'github_search', query=q) for q in ('"a\\" repo:evil/x "', 'x \\ y', '"x\\y"')]
    errors.append(refused(gh, 'github_prs', repo='checkout-service', search='"OPS-1\\" repo:evil/x "'))

    # then
    assert [e.status for e in errors] == [400, 400, 400, 400]


def test_pull_request_search_keeps_only_the_asked_repo():
    # given
    def item(number: int, repo: str) -> dict:
        return {'number': number, 'title': f'OPS-1 n{number}', 'state': 'open', 'user': {'login': 'x'}, 'created_at': '2026-10-16T09:00:00Z',
                'repository_url': f'{API}/repos/{repo}', 'html_url': f'https://github.com/{repo}/pull/{number}', 'pull_request': {'merged_at': None}}

    found = {'items': [item(1, CART), item(2, 'evil/thing'), item(3, 'acme/other'), item(4, 'ACME/Checkout-Service')]}
    gh = client(github_http({('GET', f'{API}/search/issues'): found, ('GET', f'{CART_API}/pulls'): []}))

    # when
    out = run_tool(gh, 'github_prs', repo='checkout-service', search='OPS-1 filters')

    # then
    assert [r['number'] for r in out['pullRequests']] == [1, 4]


def test_a_missing_repo_mid_pass_is_named_and_the_pass_goes_on():
    # given
    http = tracked_http('success')
    http.routes[('GET', f'{API}/repos/acme/billing')] = repo_json('billing', 300)
    http.routes[('GET', f'{API}/repos/acme/billing/deployments')] = HttpError(404, 'x')
    gh = client(http)

    # when
    sweep = track.run(ctx_of(gh, repos='billing, checkout-service'))

    # then
    assert sweep.result.ok and 'billing' in sweep.result.status
    assert [r['repo'] for r in sweep.sections['github']] == [CART]


def test_each_repo_gets_its_own_state_read_budget():
    # given
    many = [deployment_json(i, FULL, env=f'e{i}') for i in range(1, 9)]
    http = github_http({('GET', f'{CART_API}/deployments'): many, ('GET', f'{API}/repos/acme/billing'): repo_json('billing', 300),
                        ('GET', f'{API}/repos/acme/billing/deployments'): [], ('GET', f'{CART_API}/releases'): [], ('GET', f'{API}/repos/acme/billing/releases'): []})
    for i in range(1, 9):
        http.routes[('GET', f'{CART_API}/deployments/{i}/statuses')] = [deployment_status_json('success')]
    gh = client(http)

    # when
    track.run(ctx_of(gh, repos='checkout-service, billing'))

    # then
    assert len([c for c in http.calls if c[1].split('?')[0].endswith('/statuses')]) == track.STATE_CALLS


@pytest.mark.parametrize('base, api, graphql', [
    ('https://www.github.com', 'https://api.github.com', 'https://api.github.com/graphql'),
    ('https://acme.ghe.com', 'https://api.acme.ghe.com', 'https://api.acme.ghe.com/graphql'),
    ('https://git.example.com', 'https://git.example.com/api/v3', 'https://git.example.com/api/graphql'),
])
def test_the_api_address_follows_the_kind_of_github(base, api, graphql):
    # when
    gh = client(FakeHttp(), base=base)

    # then
    assert (gh.api, gh.graphql) == (api, graphql)


def test_the_connection_test_reads_one_page_of_repositories():
    # given
    url = f'{API}/user/repos'
    full = [repo_json(f'r{i}', i) for i in range(100)]
    http = FakeHttp({('GET', f'{API}/user'): {'login': 'bot'}, ('GET', f'{API}/users/acme'): {'login': 'acme', 'type': 'Organization'},
                     ('GET', url): lambda q, _: Reply(full, {'link': f'<{url}?per_page=100&page=2>; rel="next"'})})
    rt = github_runtime(kit_store(), http)

    # when
    result = github.test(rt.context(github.PLUGIN, rt.instances.get('github', 'main'), NOW))

    # then
    assert result.ok and len(http.hits(url)) == 1
    assert result.summary == msg('github.test.ok', owner='acme', kind='organization', count='100 or more repositories')
