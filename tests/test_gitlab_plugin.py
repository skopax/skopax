import base64
import dataclasses
import json
import logging
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from skopax import actions as page_actions, contract
from skopax.app import create_app
from skopax.contract import CodeHost
from skopax.enrich import Enricher, JiraRefused
from skopax.investigator.tools import Effects, Sources, ToolError, Toolbox, code_link
from skopax.kube import Kube
from skopax.messages import msg
from skopax.plugins import gitlab
from skopax.plugins.gitlab.client import GITLAB_API, GITLAB_URL, GitLab
from skopax.registry import BUILTIN, Registry, describe
from skopax.remote import HttpError, Reply, Unreachable
from skopax.settingsources import boot
from skopax.store import Store
from tests.conftest import AUTH, anomaly, settings, state
from tests.fake_http import FakeHttp
from tests.plugin_kit import gitlab_runtime, gitlab_tools
from tests.test_deploys import deploy
from tests.test_enrich import fake_gitlab
from tests.test_tickets import FakeJira

NOW = 1_792_224_000.0
USER = 'alice@example.com'
KEY = 'log|a|ERROR|x'
CARDS = (KEY, 'log|b|ERROR|y', 'log|c|ERROR|z')
SELF_HOSTED = 'https://git.example.com'
SELF_API = f'{SELF_HOSTED}/api/v4'
SELF_WEB = f'{SELF_HOSTED}/shop/team/checkout-service'
FULL = '2b2b2b2b' + 'a' * 32
TOOLS = {'gitlab_file': (None, True), 'gitlab_search': (None, True), 'gitlab_blame': (None, True), 'gitlab_compare': (None, True), 'gitlab_mrs': (6, True), 'gitlab_pipelines': (8, True)}
MAIN = {'url': GITLAB_URL, 'token': 'glpat', 'group': 'acme'}


def self_hosted(branch: str | None = None) -> FakeHttp:
    project = {'id': 11, 'path_with_namespace': 'shop/team/checkout-service', 'web_url': SELF_WEB} | ({'default_branch': branch} if branch else {})
    text = base64.b64encode(b'a\nb\nc\nd\n').decode()
    commits = f'{SELF_API}/projects/11/repository/commits'
    return FakeHttp({
        ('GET', f'{SELF_API}/groups/shop/projects'): [project],
        ('GET', f'{commits}/2b2b2b2b'): {'id': FULL, 'title': 'OPS-1 fix', 'author_name': 'Nina', 'committed_date': '2026-10-15T08:01:02Z', 'parent_ids': ['p']},
        ('GET', commits): HttpError(404, commits),
        ('GET', f'{SELF_API}/projects/11/repository/branches/{branch or "master"}'): {'commit': {'id': 'c' * 40}},
        ('GET', f'{SELF_API}/projects/11/repository/files/src%2FA.kt'): {'content': text},
        ('GET', f'{SELF_API}/projects/11/repository/files/src%2FA.kt/blame'): [{'commit': {'id': 'd' * 40, 'author_name': 'Adam', 'committed_date': '2026-10-01T10:00:00Z', 'message': 'OPS-2 a'}, 'lines': ['a', 'b']}],
    })


def toolbox(store: Store, sources: Sources, hosts: tuple[str, ...] = ('skopax.example.com', 'git.example.com')) -> Toolbox:
    effects = Effects(lambda key, by: {}, lambda key, text, by, *_, notice=None: {})
    return Toolbox(Sources(gitlab=sources.gitlab, jira=sources.jira, plugin_tools=sources.plugin_tools, link_hosts=lambda: hosts), store, effects, list(CARDS), 'live', NOW, 'https://skopax.example.com')


def offered(store: Store, tools) -> list[str]:
    return sorted(n for n in (d['name'] for d in toolbox(store, Sources(gitlab=None, jira=None, plugin_tools=tools)).definitions()) if n.startswith('gitlab_'))


def test_gitlab_identity_and_manifest():
    # given
    registry = Registry.of(gitlab.PLUGIN)

    # when
    loaded = registry.get('gitlab')
    manifest = describe(gitlab.PLUGIN, icon=True)

    # then
    assert json.loads(json.dumps(manifest)) == manifest
    assert (loaded.state, loaded.reason) == ('ok', None) and 'skopax.plugins.gitlab' in BUILTIN
    assert Registry.load({}, entry_points=False).get('gitlab').state == 'ok'
    assert (manifest['name'], manifest['version'], manifest['category'], manifest['author'], manifest['licence'], manifest['core'], manifest['edition'], manifest['maxInstances']) == (
        'GitLab', '0.1.0', 'code', 'Skopax', 'AGPL-3.0-only', '>=0.1,<0.7', 'community', 1)
    assert manifest['capabilities'] == {'hosts': ['@url'], 'secrets': ['token'], 'agentReads': False} and manifest['uses'] == []
    assert [(f['key'], f['kind'], f['required'], f['ui']) for f in manifest['fields']] == [('url', 'url', True, True), ('token', 'secret', True, False), ('group', 'text', True, False), ('ca', 'pem', False, False), ('ca-pinned', 'bool', False, False)]
    assert {t.name: (t.per_run, t.scan) for t in gitlab.PLUGIN.tools} == TOOLS
    assert all(t.kind == 'read' and t.instance_input is None and '{field:group}' in t.description for t in gitlab.PLUGIN.tools)
    assert manifest['checks'] == [] and manifest['knowledge'] == ['gitlab'] and len(gitlab.PLUGIN.knowledge[0].text.splitlines()) <= 40
    assert registry.icon('gitlab').startswith('<svg')
    assert {n for n in vars(CodeHost) if not n.startswith('_')} <= set(dir(GitLab))


def test_gitlab_connection_test_reports_ok_and_failures(store: Store):
    # given
    path = f'{GITLAB_API}/version'
    listing = {('GET', f'{GITLAB_API}/groups/acme/projects'): [{'id': 11, 'path_with_namespace': 'acme/shop/checkout-service'}]}
    good, denied, down = (FakeHttp({('GET', path): answer, **listing}) for answer in ({'version': '17.5.0', 'revision': 'abc'}, HttpError(401, path), Unreachable('x')))

    def check(http):
        rt = gitlab_runtime(store, http)
        return gitlab.test(rt.context(gitlab.PLUGIN, rt.instances.get('gitlab', 'main'), NOW))

    # when
    ok, refused, unreachable = check(good), check(denied), check(down)

    # then
    assert (ok.ok, ok.summary) == (True, msg('gitlab.test.version', version='17.5.0')) and ok.summary == 'GitLab 17.5.0'
    assert (refused.ok, refused.summary) == (False, msg('connection.rejected', status=401))
    assert (unreachable.ok, unreachable.summary) == (False, msg('connection.unreachable'))
    assert good.calls[0][1] == path and good.calls[0][2]['PRIVATE-TOKEN'] == 'glpat'


def test_gitlab_defaults_are_neutral():
    # when
    defaults = {f.key: f.default for f in gitlab.PLUGIN.fields}

    # then
    assert defaults == {'url': 'https://gitlab.com', 'token': '', 'group': '', 'ca': '', 'ca-pinned': 'false'}
    assert gitlab.PLUGIN.settings == () and gitlab.PLUGIN.rules == () and gitlab.PLUGIN.checks == ()


def test_env_defines_the_main_instance(store: Store, tmp_path):
    # given
    env = {'GITLAB_TOKEN': 'glpat-env', 'GITLAB_URL': SELF_HOSTED, 'GITLAB_GROUP': 'shop'}

    # when
    b = boot(env)
    bare = boot({'GITLAB_TOKEN': 'glpat-env', 'GITLAB_GROUP': 'shop'})
    no_token = boot({'GITLAB_URL': SELF_HOSTED, 'GITLAB_GROUP': 'shop'})
    no_group = boot({'GITLAB_TOKEN': 'glpat-env'})
    with TestClient(create_app(b.settings, store, registry=b.registry, sources=b.source_set(), kube=Kube(root=tmp_path))) as c:
        main = c.app.state.instances.get('gitlab', 'main')
        client = c.app.state.runtime.client('gitlab')
        jobs = [j.name for j in c.app.state.scheduler.jobs]

    # then
    def rows(found):
        return [r for r in found.sources[0].instances if r['plugin'] == 'gitlab']

    assert rows(b) == [{'plugin': 'gitlab', 'name': 'main', 'primary': 'true', 'url': SELF_HOSTED, 'token': 'glpat-env', 'group': 'shop'}]
    assert rows(bare)[0]['url'] == GITLAB_URL
    assert rows(no_token) == [] and rows(no_group) == []
    assert (main.primary, main.enabled, main.problem, main.values['url'], main.values['group']) == (True, True, None, SELF_HOSTED, 'shop')
    assert (client.api, client.group, client.token) == (SELF_API, 'shop', 'glpat-env')
    assert 'enrich' in jobs
    assert not hasattr(b.settings, 'gitlab_token') and not hasattr(b.settings, 'gitlab_group')


def test_gitlab_tools_are_offered_only_while_gitlab_serves(store: Store):
    # given
    runtime = gitlab_runtime(store, FakeHttp(), named=None)
    tools = gitlab_tools(GitLab('glpat', FakeHttp(), group='acme'), runtime=runtime)

    # when
    before = offered(store, tools)
    runtime.instances.create('gitlab', 'main', MAIN, True, USER)
    serving = toolbox(store, Sources(gitlab=None, jira=None, plugin_tools=tools)).definitions()
    runtime.instances.set_enabled('gitlab', 'main', False, USER)
    after = offered(store, tools)

    # then
    shown = {d['name']: d['description'] for d in serving if d['name'].startswith('gitlab_')}
    assert before == [] and after == []
    assert sorted(shown) == sorted(TOOLS)
    assert all('the GitLab group acme' in text and '{field:' not in text for text in shown.values())


def test_gitlab_added_in_admin_starts_enrichment_without_restart(store: Store):
    # given
    store.add_deploys([deploy()])
    http = fake_gitlab()

    # when
    with TestClient(create_app(settings(), store, transport=http)) as c:
        before = [j.name for j in c.app.state.scheduler.jobs]
        added = c.post('/api/admin/plugins/gitlab/instances', json={'name': 'main', 'values': MAIN, 'enabled': True}, headers=AUTH)
        ran = c.app.state.scheduler.run_due()
    d = store.deploys_since('2000-01-01T00:00:00Z')[0]

    # then
    assert 'enrich' not in before
    assert added.status_code == 201, added.text
    assert 'enrich' in ran
    assert d['tickets'] == ['OPS-106', 'OPS-115', 'WEB-203'] and d['imageBy']['commits'] == 3


def test_enrichment_skips_while_gitlab_is_off(store: Store, caplog):
    # given
    store.add_deploys([deploy()])
    http = fake_gitlab()

    # when
    with TestClient(create_app(settings(), store, transport=http)) as c:
        c.app.state.instances.create('gitlab', 'main', MAIN, True, USER)
        c.app.state.instances.set_enabled('gitlab', 'main', False, USER)
        with caplog.at_level(logging.INFO, logger='skopax.enrich'):
            off = [c.app.state.enricher.sweep(), c.app.state.enricher.sweep()]
        unenriched = store.deploys_since('2000-01-01T00:00:00Z')[0]
        asked_off = len(http.calls)
        c.app.state.instances.set_enabled('gitlab', 'main', True, USER)
        on = c.app.state.enricher.sweep()
    d = store.deploys_since('2000-01-01T00:00:00Z')[0]

    # then
    assert off == [{'gitlab': 0, 'jira': 0, 'errors': []}] * 2
    assert 'imageBy' not in unenriched and 'tickets' not in unenriched and asked_off == 0
    assert [r.getMessage() for r in caplog.records if r.name == 'skopax.enrich'] == ['deploy enrichment through GitLab is skipped while GitLab is off']
    assert on['gitlab'] == 1 and d['tickets'] == ['OPS-106', 'OPS-115', 'WEB-203']


def test_an_admin_set_token_is_used_without_restart(store: Store):
    # given
    store.add_deploys([deploy()])
    http = fake_gitlab()

    # when
    with TestClient(create_app(settings(), store, transport=http)) as c:
        c.app.state.instances.create('gitlab', 'main', {**MAIN, 'token': 'old'}, True, USER)
        c.app.state.enricher.sweep()
        store.add_deploys([deploy('orders-service', to='3c3c3c3c')])
        c.app.state.instances.update('gitlab', 'main', {'token': 'new'}, USER)
        c.app.state.runtime.refresh()
        seen = len(http.calls)
        c.app.state.enricher.sweep()

    # then
    tokens = [call[2]['PRIVATE-TOKEN'] for call in http.calls]
    assert tokens[:seen] and set(tokens[:seen]) == {'old'}
    assert tokens[seen:] and set(tokens[seen:]) == {'new'}


def test_self_hosted_url_is_used_for_api_and_code_links(store: Store):
    # given
    http = self_hosted()
    runtime = gitlab_runtime(store, http, url=SELF_HOSTED, group='shop')
    host = runtime.proxy('gitlab')
    store.add_deploys([deploy()])

    # when
    Enricher(store, host, SimpleNamespace(enabled=False)).sweep()
    by = store.deploys_since('2000-01-01T00:00:00Z')[0]['imageBy']
    link = code_link(host, {'repo': 'team/checkout-service', 'path': 'src/A.kt', 'lines': '2-3', 'what': 'x'}, '')

    # then
    assert http.calls and all(call[1].startswith(SELF_API + '/') for call in http.calls)
    assert by['url'] == f'{SELF_WEB}/-/commit/{FULL}'
    assert link['url'] == f'{SELF_WEB}/-/blob/{"c" * 40}/src/A.kt#L2-3' and link['ref'] == 'master'
    assert link['by'][0]['url'] == f'{SELF_WEB}/-/commit/{"d" * 40}'


def test_reads_and_links_follow_the_default_branch(store: Store):
    # given
    http = self_hosted(branch='main')
    runtime = gitlab_runtime(store, http, url=SELF_HOSTED, group='shop')
    b = toolbox(store, Sources(gitlab=runtime.proxy('gitlab'), jira=None, plugin_tools=gitlab_tools(None, runtime=runtime)))

    # when
    text, err = b.call('gitlab_file', {'repo': 'checkout-service', 'path': 'src/A.kt', 'lines': '1-2'})
    link = code_link(runtime.proxy('gitlab'), {'repo': 'checkout-service', 'path': 'src/A.kt', 'lines': '1', 'what': 'x'}, '')

    # then
    assert (text, err) == ('1: a\n2: b', False)
    assert 'ref=main' in http.hits(f'{SELF_API}/projects/11/repository/files/src%2FA.kt')[0][1]
    assert link['ref'] == 'main' and http.hits(f'{SELF_API}/projects/11/repository/branches/main')


def test_the_link_check_reads_the_group_from_the_instance(store: Store):
    # given
    store.save_state(state(*(anomaly(k, svc=svc) for k, svc in zip(CARDS, ('checkout-service', 'orders-service', 'payments-api')))), None)
    runtime = gitlab_runtime(store, FakeHttp(), url=SELF_HOSTED, group='shop')
    b = toolbox(store, Sources(gitlab=runtime.proxy('gitlab'), jira=None))

    # when
    outside = b.call('slack_draft', {'key': CARDS[0], 'text': f'See {SELF_HOSTED}/other/x'})
    inside = b.call('slack_draft', {'key': CARDS[1], 'text': f'See {SELF_HOSTED}/shop/team/x/-/merge_requests/4'})
    runtime.instances.set_enabled('gitlab', 'main', False, USER)
    off = toolbox(store, Sources(gitlab=runtime.proxy('gitlab'), jira=None)).call('slack_draft', {'key': CARDS[2], 'text': f'See {SELF_HOSTED}/other/y'})
    apex = GitLab('glpat', FakeHttp(), group='acme', base='https://example.com')
    rooted = GitLab('glpat', FakeHttp(), group='acme', base='https://example.com/gitlab/')
    hosted = GitLab('glpat', FakeHttp(), group='acme')

    # then
    assert outside[1] and outside[0] == f'gitlab links may only point under git.example.com/shop/; {SELF_HOSTED}/other/x is not'
    assert not inside[1], inside[0]
    assert not off[1], off[0]
    assert apex.link_problem('https://shop.example.com/cart') is None
    assert apex.link_problem('https://example.com/other/x') == 'gitlab links may only point under example.com/acme/; https://example.com/other/x is not'
    assert rooted.link_problem('https://example.com/gitlab/acme/shop/x/-/merge_requests/4') is None
    assert rooted.link_problem('https://example.com/gitlab/other/x') == 'gitlab links may only point under example.com/gitlab/acme/; https://example.com/gitlab/other/x is not'
    assert rooted.link_problem('https://example.com/acme/x') is not None
    assert hosted.link_problem('https://www.gitlab.com/other/x') is not None and hosted.link_problem('https://gitlab.com/acme/x') is None


def test_a_gitlab_tool_error_keeps_its_text_and_an_outage_names_gitlab(store: Store):
    # given
    down = FakeHttp({('GET', f'{GITLAB_API}/groups/acme/projects'): HttpError(500, 'x')})
    b = toolbox(store, Sources(gitlab=None, jira=None, plugin_tools=gitlab_tools(GitLab('glpat', down, group='acme'))))

    # when
    text, err = b.call('gitlab_file', {'repo': 'checkout-service', 'path': 'x.kt'})

    # then
    assert err and text == 'GitLab HTTP 500'


def test_the_client_reads_a_reply_body_and_its_paged_listing():
    # given
    page = FakeHttp({('GET', f'{GITLAB_API}/groups/acme/projects'): lambda q, _b: Reply([{'id': 1, 'path_with_namespace': 'acme/shop/a', 'web_url': 'https://gitlab.com/acme/shop/a', 'default_branch': 'main'}], {})})

    # when
    found = GitLab('glpat', page, group='acme').repo('a')

    # then
    assert found == {'id': 1, 'web': 'https://gitlab.com/acme/shop/a', 'path': 'shop/a', 'full': 'acme/shop/a', 'branch': 'main'}


def test_own_commits_are_read_through_the_instance_on_the_default_branch(store: Store):
    # given
    listing = [{'id': 40, 'path_with_namespace': 'acme/platform/skopax', 'web_url': 'https://gitlab.com/acme/platform/skopax', 'default_branch': 'main'}]
    commits = [{'id': 'c0ffee02' + 'a' * 32, 'short_id': 'c0ffee02', 'title': 'OPS-123 order history export', 'committed_date': '2026-10-15T08:01:02Z', 'parent_ids': ['a']}]
    http = FakeHttp({('GET', f'{GITLAB_API}/groups/acme/projects'): listing, ('GET', f'{GITLAB_API}/projects/40/repository/commits'): commits})

    # when
    with TestClient(create_app(settings(), store, transport=http)) as c:
        c.app.state.instances.create('gitlab', 'main', MAIN, True, USER)
        facts = c.app.state.self_review.facts(NOW)

    # then
    asked = http.hits(f'{GITLAB_API}/projects/40/repository/commits')
    assert facts['ownChanges'] == [{'at': '2026-10-15T08:01:02Z', 'image': 'c0ffee02', 'title': 'OPS-123 order history export'}]
    assert asked and 'ref_name=main' in asked[0][1]


def test_a_client_refusal_from_the_contract_fails_a_page_action_like_a_jira_refusal(store: Store):
    # given
    key = 'log|checkout-service|ERROR|boom'
    store.save_state(state(anomaly(key)), None)

    class Refusing(FakeJira):
        def create_issue(self, fields: dict) -> str:
            raise contract.Refused('the tracker refused the create')

    # when
    try:
        page_actions.apply(store, {'type': 'ticket', 'key': key, 'target': key, 'requestedBy': 'x'}, Refusing(), settings())
        raised = None
    except page_actions.Refused as e:
        raised = str(e)

    # then
    assert raised == 'the tracker refused the create'
    assert issubclass(JiraRefused, contract.Refused) and page_actions.Refused is not contract.Refused


def test_an_instance_saved_without_a_url_gets_the_default_and_its_link_host(store: Store):
    # given
    values = {'token': 'glpat', 'group': 'acme'}

    # when
    with TestClient(create_app(settings(), store, transport=FakeHttp())) as c:
        added = c.post('/api/admin/plugins/gitlab/instances', json={'name': 'main', 'values': values, 'enabled': True}, headers=AUTH)
        hosts = c.app.state.runtime.ui_hosts()
        stored = c.app.state.instances.get('gitlab', 'main').values

    # then
    assert added.status_code == 201, added.text
    assert stored['url'] == GITLAB_URL and hosts == ('gitlab.com',)


def test_own_commits_of_a_project_outside_the_listing_keep_the_raw_path(store: Store):
    # given
    listing = [{'id': 41, 'path_with_namespace': 'acme/tools/skopax', 'web_url': 'https://gitlab.com/acme/tools/skopax', 'default_branch': 'main'}]
    raw = f'{GITLAB_API}/projects/platform%2Fskopax/repository/commits'
    http = FakeHttp({('GET', f'{GITLAB_API}/groups/acme/projects'): listing, ('GET', raw): []})

    # when
    with TestClient(create_app(settings(), store, transport=http)) as c:
        c.app.state.instances.create('gitlab', 'main', MAIN, True, USER)
        c.app.state.self_review.facts(NOW)

    # then
    assert http.hits(raw) and 'ref_name=master' in http.hits(raw)[0][1]
    assert not http.hits(f'{GITLAB_API}/projects/41/repository/commits')


@pytest.mark.parametrize('own, raw', [('other/platform/skopax', True), ('acme/platform/skopax', False), ('platform/skopax', False)])
def test_own_commits_take_the_listed_project_only_on_an_exact_path(store: Store, own, raw):
    # given
    listing = [{'id': 40, 'path_with_namespace': 'acme/platform/skopax', 'web_url': 'https://gitlab.com/acme/platform/skopax', 'default_branch': 'main'}]
    outside = f'{GITLAB_API}/projects/other%2Fplatform%2Fskopax/repository/commits'
    listed = f'{GITLAB_API}/projects/40/repository/commits'
    http = FakeHttp({('GET', f'{GITLAB_API}/groups/acme/projects'): listing, ('GET', outside): [], ('GET', listed): []})

    # when
    with TestClient(create_app(dataclasses.replace(settings(), self_project=own), store, transport=http)) as c:
        c.app.state.instances.create('gitlab', 'main', MAIN, True, USER)
        c.app.state.self_review.facts(NOW)

    # then
    assert bool(http.hits(outside)) is raw and bool(http.hits(listed)) is not raw
    assert 'ref_name=master' in http.hits(outside)[0][1] if raw else 'ref_name=main' in http.hits(listed)[0][1]


def test_a_deploy_never_resolves_to_a_project_right_under_the_group(store: Store):
    # given
    listing = [{'id': 7, 'path_with_namespace': 'acme/cart', 'web_url': 'https://gitlab.com/acme/cart'}]
    http = FakeHttp({('GET', f'{GITLAB_API}/groups/acme/projects'): listing})
    store.add_deploys([deploy('cart')])

    # when
    Enricher(store, GitLab('glpat', http, group='acme'), SimpleNamespace(enabled=False)).sweep()
    d = store.deploys_since('2000-01-01T00:00:00Z')[0]

    # then
    assert d['ticketsNote'] == 'no GitLab project with the service name in a subgroup of the group' and 'imageBy' not in d
    assert not http.hits(f'{GITLAB_API}/projects/7/repository/commits/2b2b2b2b')


def test_a_code_link_to_no_known_repo_says_it_may_be_a_folder():
    # given
    http = FakeHttp({('GET', f'{GITLAB_API}/groups/acme/projects'): [{'id': 7, 'path_with_namespace': 'acme/shop/orders-service', 'web_url': 'https://gitlab.com/acme/shop/orders-service'}]})

    # when
    with pytest.raises(ToolError) as refused:
        code_link(GitLab('glpat', http, group='acme'), {'repo': 'manifests', 'path': 'a.yml', 'lines': '1', 'what': 'x'}, '')

    # then
    assert refused.value.text == ('no single GitLab project named manifests; give its full path, like ops/deploy/helm-charts; '
                                  'it may be a folder inside a repo, such as ops/deploy/manifests or ops/deploy/helm-charts, so search the code first')


def test_a_code_host_without_its_link_rule_refuses_the_draft_instead_of_allowing_any_link(store: Store):
    # given
    store.save_state(state(anomaly(KEY)), None)
    b = toolbox(store, Sources(gitlab=SimpleNamespace(enabled=True), jira=None))

    # when
    text, err = b.call('slack_draft', {'key': KEY, 'text': f'See {SELF_HOSTED}/other/x'})

    # then
    assert err and 'AttributeError' in text
    assert store.anomaly(KEY).get('slackDraft') is None


def layout(group: str, *paths: str) -> FakeHttp:
    encoded = group.replace('/', '%2F')
    listing = [{'id': i, 'path_with_namespace': f'{group}/{p}', 'web_url': f'https://gitlab.com/{group}/{p}'} for i, p in enumerate(paths, 1)]
    return FakeHttp({('GET', f'{GITLAB_API}/groups/{encoded}/projects'): listing})


@pytest.mark.parametrize('asked', ['checkout-service', 'acme/checkout-service', 'web', 'acme/web', 'platform/helm-charts', 'acme/platform/helm-charts', 'team/deep/charts',
                                   'acme/team/deep/charts'])
def test_the_gitlab_tools_find_a_project_by_bare_name_or_full_path_at_any_depth(asked):
    # given
    from skopax.plugins.gitlab.tools import project_of
    http = layout('acme', 'checkout-service', 'web', 'platform/helm-charts', 'team/deep/charts')
    want = {'checkout-service': 'checkout-service', 'web': 'web', 'helm-charts': 'platform/helm-charts', 'charts': 'team/deep/charts'}[asked.split('/')[-1]]

    # when
    found = project_of(GitLab('glpat', http, group='acme'), asked)

    # then
    assert found['path'] == want


def test_a_nested_group_is_sent_encoded_as_one_id_and_its_projects_are_found(store: Store):
    # given
    http = layout('acme/platform', 'checkout-service', 'payments/api')
    client = GitLab('glpat', http, group='acme/platform')

    # when
    top = client.repo('checkout-service')
    deep = client.repo('acme/platform/payments/api')
    with pytest.raises(contract.Missing):
        client.group_search('needle')

    # then
    assert top['full'] == 'acme/platform/checkout-service' and deep['path'] == 'payments/api'
    assert [c[1].split('?')[0] for c in http.calls] == [f'{GITLAB_API}/groups/acme%2Fplatform/projects', f'{GITLAB_API}/groups/acme%2Fplatform/search']


def test_the_connection_test_sends_a_nested_group_encoded(store: Store):
    # given
    http = FakeHttp({('GET', f'{GITLAB_API}/version'): {'version': '17.5.0'}, ('GET', f'{GITLAB_API}/groups/acme%2Fplatform/projects'): []})
    runtime = gitlab_runtime(store, http, group='acme/platform')

    # when
    result = gitlab.test(runtime.context(gitlab.PLUGIN, runtime.instances.get('gitlab', 'main'), NOW))

    # then
    assert result.ok is True, result.summary
