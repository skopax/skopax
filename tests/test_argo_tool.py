from fastapi.testclient import TestClient

from skopax.app import create_app
from skopax.investigator import prompt
from skopax.investigator.tools import RUN_CALLS, SOURCE_TOOLS, WRITE_NAMES, Effects, Toolbox, allowed_link_hosts, draft_problem
from skopax.investigator.toolset import CORE
from skopax.kube import Kube
from skopax.plugins import argocd
from skopax.plugins.argocd.argo import ArgoError
from skopax.plugintools import PluginTools
from skopax.store import Store
from tests.conftest import settings
from tests.fake_http import FakeHttp
from tests.test_argo import APPS, application
from tests.test_argo_pass import CHECKOUT, NOW, URL, argo_runtime, in_cluster, with_instance
from tests.test_investigator_tools import box, sources
from tests.test_kube import BASE, FakeApi


class FakeArgo:
    def __init__(self, answer=None, fail: ArgoError | None = None):
        self.answer = answer
        self.fail = fail
        self.asked: list[tuple] = []

    def _reply(self, *call):
        self.asked.append(call)
        if self.fail:
            raise self.fail
        return self.answer

    def apps(self, problem_only=True, name=None):
        return self._reply('apps', problem_only, name)

    def app(self, name):
        return self._reply('app', name)


def argo_tools(http=None, **values) -> PluginTools:
    return PluginTools(argo_runtime(http if http is not None else FakeHttp(), **values))


def faked(monkeypatch, fake: FakeArgo) -> FakeArgo:
    monkeypatch.setattr(argocd, 'argo_of', lambda ctx: fake)
    return fake


def test_argo_is_a_read_source_tool_that_slack_read_only_runs_get_too(store: Store):
    # given
    tool = next(t for t in argocd.PLUGIN.tools if t.name == 'argo')

    # when
    slack = Toolbox(sources(plugin_tools=argo_tools()), store, Effects(lambda key, by: {}, lambda key, text, by, *_, notice=None: {}), [], 'live', NOW,
                    'https://skopax.example.com', ask=True, read_only=True)
    offered = {t['name']: t for t in slack.definitions()}

    # then
    assert (tool.kind, tool.scan, tool.per_run, tool.instance_input) == ('read', True, None, 'instance')
    assert 'argo' in offered and slack.sources.plugin_tools.scan('argo') and 'argo' not in WRITE_NAMES
    assert 'argo' not in SOURCE_TOOLS and 'argo' not in RUN_CALLS and 'argo' not in CORE
    schema = offered['argo']['input_schema']
    assert schema['properties']['op']['enum'] == ['apps', 'app']
    assert schema['properties']['problem_only'] == {'type': 'boolean'}
    assert schema['properties']['instance']['enum'] == ['main']
    assert schema['required'] == ['op']
    assert schema['additionalProperties'] is False
    assert 'argocd.argoproj.io/instance' in tool.description
    assert '://' not in tool.description and 'when the `kube` tool is offered' in tool.description


def test_each_op_goes_to_its_fixed_argo_operation(store: Store, monkeypatch):
    # given
    fake = faked(monkeypatch, FakeArgo([{'name': 'a'}]))
    b = box(store, plugin_tools=argo_tools())

    # when
    results = [b.call('argo', a) for a in (
        {'op': 'apps'},
        {'op': 'apps', 'problem_only': False},
        {'op': 'apps', 'problem_only': True, 'name': 'ledger-service'},
        {'op': 'app', 'name': CHECKOUT},
    )]

    # then
    assert [err for _, err in results] == [False] * 4
    assert fake.asked == [
        ('apps', True, None),
        ('apps', False, None),
        ('apps', True, 'ledger-service'),
        ('app', CHECKOUT),
    ]


def test_without_a_cluster_the_argo_tool_answers_a_clean_error_and_calls_nothing(store: Store, monkeypatch, tmp_path):
    # given
    api = in_cluster(monkeypatch, tmp_path, {APPS: {'items': []}}, token=False)

    # when
    missing, missing_err = box(store).call('argo', {'op': 'apps'})
    disabled, disabled_err = box(store, plugin_tools=argo_tools(mode='in-cluster')).call('argo', {'op': 'apps'})

    # then
    assert (missing_err, disabled_err) == (True, True)
    assert missing == 'no tool named argo'
    assert 'not running in a cluster' in disabled
    assert api.calls == []


def test_a_missing_name_an_unknown_op_and_an_api_error_come_back_as_tool_errors(store: Store, monkeypatch):
    # given
    fail = ArgoError(403, 'HTTP 403 on Argo CD applications', 'HTTP 403 on /apis/argoproj.io/v1alpha1/namespaces/argocd/applications: forbidden')
    faked(monkeypatch, FakeArgo())
    plain = box(store, plugin_tools=argo_tools())

    # when
    no_name, e1 = plain.call('argo', {'op': 'app'})
    bad_op, e2 = plain.call('argo', {'op': 'sync', 'name': 'x'})
    faked(monkeypatch, FakeArgo(fail=fail))
    b = box(store, plugin_tools=argo_tools())
    forbidden, e3 = b.call('argo', {'op': 'apps'})

    # then
    assert (e1, e2, e3) == (True, True, True)
    assert no_name == 'name is required'
    assert bad_op == 'op must be one of apps, app, got "sync"'
    assert forbidden.startswith('HTTP 403') and 'forbidden' in forbidden and b.calls[-1]['error'] == forbidden


def test_in_cluster_mode_builds_its_own_kube_client_and_not_the_apps(store: Store, monkeypatch, tmp_path):
    # given
    own = in_cluster(monkeypatch, tmp_path, {APPS: {'items': [application(CHECKOUT, 'OutOfSync')]}})
    inside = FakeApi({})
    with_instance(store, mode='in-cluster')

    # when
    with TestClient(create_app(settings(), store, kube=Kube(root=tmp_path, base=BASE, transport=inside))) as c:
        rows = c.app.state.investigator.sources.plugin_tools.call('argo', {'op': 'apps'}, NOW)

    # then
    assert [r.get('name') for r in rows] == [CHECKOUT, None]
    assert len(own.calls) == 1 and inside.calls == []


def test_a_slack_draft_may_carry_the_argo_cd_link():
    # given
    hosts = allowed_link_hosts(settings(), argo_runtime(FakeHttp()).ui_hosts())

    # when
    ok = draft_problem(f'checkout-service OutOfSync: {URL}/applications/argocd/{CHECKOUT}', hosts)
    other = draft_problem('https://argocd.other.example.net/applications/x', hosts)

    # then
    assert ok is None
    assert 'argocd.other.example.net is not one of them' in other


def test_card_runs_and_slack_asks_both_learn_when_to_use_argo():
    # given
    serving = argo_runtime(FakeHttp())
    idle = prompt.system_prompt()

    # when
    full, slim = prompt.system_prompt(serving.knowledge()), prompt.slack_system_prompt(serving.knowledge())

    # then
    assert '## Argo CD' not in idle
    for text in (full, slim):
        section = text.split('## Argo CD')[1].split('\n## ')[0]
        assert '`argo`' in section and 'argocd.argoproj.io/instance' in section and 'when the `kube` tool is offered' in section
