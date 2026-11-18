import json

import pytest

from skopax.kube import MESSAGE_LIMIT, HttpFailed, Kube
from skopax.plugins.argocd.argo import HISTORY, PAGE, PAGES, RESOURCE_LIMIT, Argo, ArgoError, KubeSource, ui_link
from tests.test_kube import BASE, TOKEN, FakeApi

APPS = '/apis/argoproj.io/v1alpha1/namespaces/argocd/applications'
UI = 'https://argocd.example.com'
CHECKOUT = 'shop-checkout-service'
ALLOWED_KEYS = {'name', 'namespace', 'project', 'link', 'sync', 'health', 'operation', 'note', 'status', 'revision', 'message', 'phase', 'startedAt', 'finishedAt',
                'retries', 'images', 'source', 'repo', 'path', 'targetRevision', 'chart', 'history', 'id', 'deployedAt', 'conditions', 'type', 'at', 'resourceCount',
                'resources', 'resourcesNote', 'kind', 'reconciledAt'}


def argo(tmp_path, routes: dict) -> tuple[Argo, FakeApi]:
    (tmp_path / 'token').write_text(TOKEN + '\n')
    api = FakeApi(routes)
    return Argo(KubeSource(Kube(root=tmp_path, base=BASE, transport=api), 'argocd'), UI, 'argocd'), api


def leaky_source(tag: str) -> dict:
    return {'repoURL': 'https://gitlab.com/acme/platform/helm-charts.git', 'path': 'charts/shop/checkout-service', 'targetRevision': 'main',
            'helm': {'releaseName': 'checkout-service', 'valueFiles': ['values.yaml'], 'values': f'db:\n  password: {tag}-VALUES',
                     'valuesObject': {'db': {'password': f'{tag}-VALUES-OBJECT'}}, 'parameters': [{'name': 'secret', 'value': f'{tag}-PARAMETER'}],
                     'fileParameters': [{'name': 'cert', 'path': f'{tag}-FILE-PARAMETER'}]},
            'plugin': {'name': 'vault', 'env': [{'name': 'TOKEN', 'value': f'{tag}-PLUGIN-ENV'}]},
            'kustomize': {'images': [f'{tag}-KUSTOMIZE']}, 'directory': {'jsonnet': {'extVars': [{'name': 'a', 'value': f'{tag}-JSONNET'}]}}}


def leaky_meta(name: str) -> dict:
    return {'name': name, 'namespace': 'argocd', 'uid': f'uid-{name}', 'labels': {'leak': 'SECRET-LABEL'},
            'annotations': {'kubectl.kubernetes.io/last-applied-configuration': '{"helm":{"values":"SECRET-LAST-APPLIED"}}'},
            'managedFields': [{'manager': 'argocd-server', 'fieldsV1': {'f:spec': {'f:source': 'SECRET-MANAGED-FIELD'}}}], 'finalizers': ['resources-finalizer.argocd.argoproj.io']}


def resource(kind: str, name: str, status: str | None, health: str | None = None, message: str | None = None) -> dict:
    r = {'group': 'apps' if kind == 'Deployment' else '', 'version': 'v1', 'kind': kind, 'namespace': 'shop', 'name': name}
    if status:
        r['status'] = status
    if health:
        r['health'] = {'status': health, **({'message': message} if message else {})}
    return r


def application(name: str = CHECKOUT, sync: str = 'Synced', health: str = 'Healthy', phase: str = 'Succeeded', multi: bool = False, history: int = 12) -> dict:
    spec = {'project': 'default', 'destination': {'server': 'https://kubernetes.default.svc', 'namespace': 'shop'},
            'syncPolicy': {'automated': {'prune': True, 'selfHeal': True}}, 'info': [{'name': 'owner', 'value': 'SECRET-INFO'}]}
    if multi:
        spec['sources'] = [leaky_source('SECRET-SOURCES'), {**leaky_source('SECRET-SOURCES-2'), 'path': 'values-only'}]
    else:
        spec['source'] = leaky_source('SECRET-SPEC')
    revision = {'revisions': ['abc123def456', 'fff000aaa111']} if multi else {'revision': 'abc123def456'}
    copies = (lambda tag: {'sources': [leaky_source(tag)]}) if multi else (lambda tag: {'source': leaky_source(tag)})
    rows = [{'id': i, **({'revisions': [f'rev{i}', 'fff000aaa111']} if multi else {'revision': f'rev{i}'}), 'deployedAt': f'2026-10-{i + 10:02d}T08:00:00Z',
             'deployStartedAt': f'2026-10-{i + 10:02d}T07:59:00Z', 'initiatedBy': {'username': 'SECRET-USERNAME@example.com'}, **copies('SECRET-HISTORY')} for i in range(1, history + 1)]
    return {
        'apiVersion': 'argoproj.io/v1alpha1', 'kind': 'Application', 'metadata': leaky_meta(name), 'spec': spec,
        'status': {
            'sync': {'status': sync, **revision, 'comparedTo': {**copies('SECRET-COMPARED'), 'destination': spec['destination']}},
            'health': {'status': health, **({'message': 'Deployment "checkout-service" exceeded its progress deadline'} if health != 'Healthy' else {})},
            'history': rows,
            'operationState': {
                'operation': {'sync': {**revision, **copies('SECRET-OPERATION'), 'syncOptions': ['CreateNamespace=true']}, 'initiatedBy': {'automated': True}, 'retry': {'limit': 5}},
                'phase': phase, 'message': 'one or more objects failed to apply, reason: ' + 'x' * 900 if phase == 'Failed' else 'successfully synced (all tasks run)',
                'startedAt': '2026-10-25T07:44:00Z', 'finishedAt': '2026-10-25T07:46:00Z', 'retryCount': 2,
                'syncResult': {**revision, **copies('SECRET-RESULT'), 'resources': [{'kind': 'Deployment', 'name': 'x', 'message': 'SECRET-RESULT-MESSAGE'}]},
            },
            'conditions': [{'type': 'SyncError', 'message': 'Failed sync attempt to abc123def456: ' + 'y' * 900, 'lastTransitionTime': '2026-10-25T07:46:00Z'}],
            'resources': [
                resource('Deployment', 'checkout-service', 'Synced', 'Degraded', 'Deployment "checkout-service" exceeded its progress deadline'),
                resource('Service', 'checkout-service', 'Synced', 'Healthy'),
                resource('ConfigMap', 'checkout-service-config', 'OutOfSync'),
                resource('ServiceAccount', 'checkout-service', 'Synced'),
                resource('Ingress', 'checkout-service', None, 'Healthy'),
            ],
            'summary': {'images': ['registry.example.com/checkout-service:abc12345'], 'externalURLs': ['https://SECRET-EXTERNAL-URL']},
            'reconciledAt': '2026-10-25T07:58:00Z', 'observedAt': '2026-10-25T07:58:00Z', 'sourceType': 'Helm',
        },
    }


def every_app() -> list[dict]:
    return [application('web-storefront'), application(CHECKOUT, 'OutOfSync', 'Degraded', 'Failed'), application('payments-ledger-service', 'Synced', 'Progressing', 'Running'),
            application('accounts-profile-service', 'Unknown', 'Healthy')]


def keys_of(value) -> set[str]:
    if isinstance(value, dict):
        return set(value) | set().union(*(keys_of(v) for v in value.values()))
    if isinstance(value, list):
        return set().union(set(), *(keys_of(v) for v in value))
    return set()


def link(name: str) -> str:
    return f'{UI}/applications/argocd/{name}'


def test_without_a_token_file_argo_is_off_and_no_call_goes_out(tmp_path):
    # given
    api = FakeApi({APPS: {'items': []}})
    a = Argo(KubeSource(Kube(root=tmp_path, base=BASE, transport=api), 'argocd'), UI, 'argocd')

    # when
    with pytest.raises(ArgoError) as e:
        a.apps()

    # then
    assert e.value.status == 503 and 'not running in a cluster' in e.value.message and e.value.reason == e.value.message
    assert api.calls == []


def test_apps_lists_only_the_apps_that_are_not_synced_or_not_healthy_with_their_link(tmp_path):
    # given
    a, api = argo(tmp_path, {APPS: {'items': every_app()}})

    # when
    rows = a.apps()

    # then
    path, query, headers = api.calls[0]
    assert path == APPS
    assert query == {'limit': [str(PAGE)]}
    assert headers['Authorization'] == f'Bearer {TOKEN}'
    assert rows == [
        {'name': 'accounts-profile-service', 'namespace': 'shop', 'sync': 'Unknown', 'health': 'Healthy', 'operation': 'Succeeded', 'link': link('accounts-profile-service')},
        {'name': 'payments-ledger-service', 'namespace': 'shop', 'sync': 'Synced', 'health': 'Progressing', 'operation': 'Running', 'link': link('payments-ledger-service')},
        {'name': CHECKOUT, 'namespace': 'shop', 'sync': 'OutOfSync', 'health': 'Degraded', 'operation': 'Failed', 'link': link(CHECKOUT)},
        {'note': '3 of 4 applications are not Synced or not Healthy'},
    ]
    assert ui_link(UI + '/', 'argocd', CHECKOUT) == link(CHECKOUT)


def test_apps_reads_every_page_through_the_continue_token(tmp_path):
    # given
    pages = {None: {'metadata': {'continue': 'page-2'}, 'items': every_app()[:2]}, 'page-2': {'metadata': {'continue': ''}, 'items': every_app()[2:]}}
    a, api = argo(tmp_path, {APPS: lambda query: pages[(query.get('continue') or [None])[0]]})

    # when
    rows = a.apps()

    # then
    assert [c[1] for c in api.calls] == [{'limit': [str(PAGE)]}, {'limit': [str(PAGE)], 'continue': ['page-2']}]
    assert [r.get('name') for r in rows] == ['accounts-profile-service', 'payments-ledger-service', CHECKOUT, None]


def test_apps_stop_after_the_page_cap_and_say_the_list_is_partial(tmp_path):
    # given
    a, api = argo(tmp_path, {APPS: {'metadata': {'continue': 'more'}, 'items': [application(sync='OutOfSync')]}})

    # when
    rows = a.apps()

    # then
    assert len(api.calls) == PAGES
    assert rows[-1] == {'note': f'{PAGES} of {PAGES} applications are not Synced or not Healthy; Argo CD holds more than the {PAGES} read'}


def test_apps_with_problem_only_off_list_every_matching_app_problems_first(tmp_path):
    # given
    a, _ = argo(tmp_path, {APPS: {'items': every_app()}})

    # when
    every = a.apps(problem_only=False)
    named = a.apps(problem_only=False, name='Ledger-Service')

    # then
    assert [r.get('name') for r in every] == ['accounts-profile-service', 'payments-ledger-service', CHECKOUT, 'web-storefront', None]
    assert every[-1] == {'note': '3 of 4 applications are not Synced or not Healthy'}
    assert [r.get('name') for r in named] == ['payments-ledger-service', None]
    assert named[-1] == {'note': "1 of 1 applications with 'ledger-service' in the name are not Synced or not Healthy (4 read)"}


def test_an_all_clear_or_an_unknown_name_still_answers_with_a_note(tmp_path):
    # given
    a, _ = argo(tmp_path, {APPS: {'items': [application('web-storefront'), application('web-blog')]}})

    # when
    clear = a.apps()
    clear_named = a.apps(name='blog')
    unknown = a.apps(name='nothing-like-it')

    # then
    assert clear == [{'note': 'all 2 applications are Synced and Healthy'}]
    assert clear_named == [{'note': "all 1 applications with 'blog' in the name are Synced and Healthy (2 read)"}]
    assert unknown == [{'note': "no application has 'nothing-like-it' in its name (2 read)"}]


def test_an_empty_or_filtered_read_says_so_instead_of_an_all_clear(tmp_path):
    # given
    (tmp_path / 'token').write_text(TOKEN)
    empty = Argo(KubeSource(Kube(root=tmp_path, base=BASE, transport=FakeApi({APPS: {'items': []}})), 'argocd'), UI, 'argocd')
    listed = FakeApi({APPS: {'items': every_app()}})
    other = Argo(KubeSource(Kube(root=tmp_path, base=BASE, transport=listed), 'argocd'), UI, 'argocd', ('payments-team',))

    # when
    nothing = empty.apps(problem_only=False)
    filtered = other.apps()

    # then
    assert nothing == [{'note': 'Argo CD listed no applications'}]
    assert filtered == [{'note': 'no application is in the watched projects (4 read)'}]


def test_app_returns_sync_health_operation_history_conditions_and_only_the_bad_resources(tmp_path):
    # given
    a, api = argo(tmp_path, {f'{APPS}/{CHECKOUT}': application(CHECKOUT, 'OutOfSync', 'Degraded', 'Failed')})

    # when
    out = a.app(CHECKOUT)

    # then
    assert api.calls[0][0] == f'{APPS}/{CHECKOUT}'
    assert out['name'] == CHECKOUT and out['project'] == 'default' and out['namespace'] == 'shop'
    assert out['link'] == link(CHECKOUT)
    assert out['sync'] == {'status': 'OutOfSync', 'revision': 'abc123def456'}
    assert out['health'] == {'status': 'Degraded', 'message': 'Deployment "checkout-service" exceeded its progress deadline'}
    op = out['operation']
    assert (op['phase'], op['startedAt'], op['finishedAt'], op['revision'], op['retries']) == ('Failed', '2026-10-25T07:44:00Z', '2026-10-25T07:46:00Z', 'abc123def456', 2)
    assert op['message'].startswith('one or more objects failed to apply') and len(op['message']) == MESSAGE_LIMIT == 500
    assert out['images'] == ['registry.example.com/checkout-service:abc12345']
    assert out['source'] == [{'repo': 'https://gitlab.com/acme/platform/helm-charts.git', 'path': 'charts/shop/checkout-service', 'targetRevision': 'main'}]
    assert HISTORY == 10 and len(out['history']) == 10
    assert out['history'][0] == {'id': 12, 'revision': 'rev12', 'deployedAt': '2026-10-22T08:00:00Z'}
    assert [h['id'] for h in out['history']] == list(range(12, 2, -1))
    assert out['conditions'][0]['type'] == 'SyncError' and len(out['conditions'][0]['message']) == 500 and out['conditions'][0]['at'] == '2026-10-25T07:46:00Z'
    assert out['resourceCount'] == 5
    assert out['resources'] == [
        {'kind': 'Deployment', 'namespace': 'shop', 'name': 'checkout-service', 'status': 'Synced', 'health': 'Degraded',
         'message': 'Deployment "checkout-service" exceeded its progress deadline'},
        {'kind': 'ConfigMap', 'namespace': 'shop', 'name': 'checkout-service-config', 'status': 'OutOfSync'},
    ]
    assert out['reconciledAt'] == '2026-10-25T07:58:00Z'


def test_a_multi_source_app_gives_its_revisions_as_lists(tmp_path):
    # given
    a, _ = argo(tmp_path, {f'{APPS}/{CHECKOUT}': application(CHECKOUT, multi=True, history=3)})

    # when
    out = a.app(CHECKOUT)

    # then
    assert out['sync'] == {'status': 'Synced', 'revision': ['abc123def456', 'fff000aaa111']}
    assert out['operation']['revision'] == ['abc123def456', 'fff000aaa111']
    assert out['history'][0] == {'id': 3, 'revision': ['rev3', 'fff000aaa111'], 'deployedAt': '2026-10-13T08:00:00Z'}
    assert [s['path'] for s in out['source']] == ['charts/shop/checkout-service', 'values-only']
    assert out['health'] == {'status': 'Healthy'}
    assert 'resourcesNote' not in out


def test_a_degraded_app_with_many_bad_resources_keeps_the_first_ones_and_says_so(tmp_path):
    # given
    app = application(CHECKOUT, 'OutOfSync')
    app['status']['resources'] = [resource('Deployment', f'd-{i}', 'OutOfSync', 'Missing') for i in range(RESOURCE_LIMIT + 7)]
    a, _ = argo(tmp_path, {f'{APPS}/{CHECKOUT}': app})

    # when
    out = a.app(CHECKOUT)

    # then
    assert len(out['resources']) == RESOURCE_LIMIT
    assert out['resourcesNote'] == f'{RESOURCE_LIMIT + 7} resources are not Synced or not Healthy; the first {RESOURCE_LIMIT} are shown'


def test_no_answer_carries_helm_values_parameters_plugin_env_or_any_source_copy(tmp_path):
    # given
    apps = every_app() + [application('payments-refund-service', 'OutOfSync', multi=True)]
    routes = {APPS: {'items': apps}, **{f'{APPS}/{x["metadata"]["name"]}': x for x in apps}}
    a, _ = argo(tmp_path, routes)

    # when
    answers = [a.apps(), a.apps(problem_only=False)] + [a.app(x['metadata']['name']) for x in apps]
    rendered = json.dumps(answers)

    # then
    assert 'SECRET' not in rendered
    assert keys_of(answers) <= ALLOWED_KEYS
    assert not keys_of(answers) & {'helm', 'values', 'valuesObject', 'parameters', 'fileParameters', 'plugin', 'env', 'comparedTo', 'syncResult', 'initiatedBy',
                                   'annotations', 'labels', 'managedFields', 'spec', 'kustomize', 'directory', 'info', 'externalURLs'}


def test_an_api_error_names_the_status_and_message_and_never_the_token(tmp_path):
    # given
    a, _ = argo(tmp_path, {
        APPS: HttpFailed(403, 'applications.argoproj.io is forbidden: User "system:serviceaccount:ops:skopax" cannot list resource "applications"'),
        f'{APPS}/{CHECKOUT}': OSError(f'connection reset, Authorization: Bearer {TOKEN}'),
    })

    # when
    with pytest.raises(ArgoError) as listed:
        a.apps()
    with pytest.raises(ArgoError) as missing:
        a.app('nope')
    with pytest.raises(ArgoError) as reset:
        a.app(CHECKOUT)

    # then
    assert (listed.value.status, listed.value.message) == (403, 'Argo CD refused to read applications (HTTP 403): the service account lacks get and list on applications.argoproj.io in the namespace argocd')
    assert missing.value.message.startswith('HTTP 404') and missing.value.status == 404
    assert 'unreachable' in reset.value.message and TOKEN not in reset.value.message and 'sa-token-body' not in reset.value.message
    assert reset.value.status == 502 and reset.value.reason == reset.value.message


def test_names_that_are_not_application_names_are_refused_before_any_call(tmp_path):
    # given
    a, api = argo(tmp_path, {})

    # when
    refused = []
    for op in (lambda: a.app('../../secrets/x'), lambda: a.app('Shop_Checkout'), lambda: a.app(''), lambda: a.apps(name='a;b'), lambda: a.apps(name='x' * 300)):
        with pytest.raises(ArgoError) as e:
            op()
        refused.append(e.value.status)

    # then
    assert refused == [400] * 5
    assert api.calls == []
