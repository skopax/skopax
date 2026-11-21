import datetime as dt
import json
from urllib.parse import parse_qs, urlsplit

import pytest

from skopax.kube import LOG_TAIL_MAX, HttpFailed, Kube, KubeError

NOW = dt.datetime(2026, 10, 25, 8, tzinfo=dt.timezone.utc).timestamp()
BASE = 'https://api.test:6443'
TOKEN = 'eyJhbGciOiJSUzI1NiJ9.sa-token-body.signature-part'
SECRETS = ('SECRET-ENV-VALUE', 'SECRET-ENVFROM-NAME', 'SECRET-ARG-VALUE', 'SECRET-CMD-VALUE', 'SECRET-LAST-APPLIED', 'SECRET-MANAGED-FIELD',
           'SECRET-LABEL-VALUE', 'SECRET-VOLUME-NAME', 'SECRET-OTHER-ANNOTATION')


class FakeApi:
    def __init__(self, routes: dict):
        self.routes = routes
        self.calls: list[tuple[str, dict, dict]] = []

    def __call__(self, url: str, headers: dict, timeout: float) -> bytes:
        parts = urlsplit(url)
        query = parse_qs(parts.query)
        self.calls.append((parts.path, query, headers))
        assert url.startswith(BASE + '/')
        answer = self.routes.get(parts.path)
        if answer is None:
            raise HttpFailed(404, f'{parts.path} not found')
        if callable(answer):
            answer = answer(query)
        if isinstance(answer, Exception):
            raise answer
        return answer if isinstance(answer, bytes) else json.dumps(answer).encode()


def kube(tmp_path, routes: dict) -> tuple[Kube, FakeApi]:
    (tmp_path / 'token').write_text(TOKEN + '\n')
    api = FakeApi(routes)
    return Kube(root=tmp_path, base=BASE, transport=api), api


def leaky_meta(name: str, **extra) -> dict:
    return {'name': name, 'namespace': 'store', 'uid': f'uid-{name}', 'labels': {'app': 'catalog-service', 'leak': 'SECRET-LABEL-VALUE'},
            'annotations': {'kubectl.kubernetes.io/last-applied-configuration': '{"env":"SECRET-LAST-APPLIED"}', 'other': 'SECRET-OTHER-ANNOTATION'},
            'managedFields': [{'manager': 'helm', 'fieldsV1': {'f:spec': {'f:env': 'SECRET-MANAGED-FIELD'}}}], **extra}


def leaky_container(name: str = 'java', image: str = 'images/catalog-service/master:abc12345') -> dict:
    return {'name': name, 'image': image, 'env': [{'name': 'DB_PASSWORD', 'value': 'SECRET-ENV-VALUE'}],
            'envFrom': [{'secretRef': {'name': 'SECRET-ENVFROM-NAME'}}], 'command': ['sh', '-c', 'SECRET-CMD-VALUE'], 'args': ['--password=SECRET-ARG-VALUE'],
            'volumeMounts': [{'name': 'SECRET-VOLUME-NAME', 'mountPath': '/app/shop/certs'}]}


def leaky_spec() -> dict:
    return {'containers': [leaky_container()], 'volumes': [{'name': 'certs', 'secret': {'secretName': 'SECRET-VOLUME-NAME'}}], 'nodeName': 'node-1'}


def pod_json(name: str = 'catalog-service-7d9f8b6c5-x2b4k', restarts: int = 3) -> dict:
    return {
        'metadata': leaky_meta(name, ownerReferences=[{'kind': 'ReplicaSet', 'name': 'catalog-service-7d9f8b6c5', 'controller': True, 'uid': 'uid-rs'}]),
        'spec': leaky_spec(),
        'status': {
            'phase': 'Running', 'startTime': '2026-10-25T06:00:00Z', 'podIP': '10.1.2.3',
            'conditions': [{'type': 'Ready', 'status': 'False', 'reason': 'ContainersNotReady', 'message': 'containers with unready status: [java]', 'lastTransitionTime': '2026-10-25T07:50:00Z'}],
            'containerStatuses': [{
                'name': 'java', 'image': 'images/catalog-service/master:abc12345', 'ready': False, 'restartCount': restarts,
                'state': {'waiting': {'reason': 'CrashLoopBackOff', 'message': 'back-off 5m0s restarting failed container=java'}},
                'lastState': {'terminated': {'exitCode': 137, 'reason': 'OOMKilled', 'startedAt': '2026-10-25T07:40:00Z', 'finishedAt': '2026-10-25T07:49:30Z'}},
                'env': 'SECRET-ENV-VALUE'}],
        },
    }


def deployment_json() -> dict:
    return {
        'metadata': leaky_meta('catalog-service', generation=7),
        'spec': {'replicas': 2, 'selector': {'matchLabels': {'app.kubernetes.io/name': 'catalog-service', 'app.kubernetes.io/instance': 'store-catalog-service'}},
                 'template': {'metadata': leaky_meta('x'), 'spec': leaky_spec()}},
        'status': {'replicas': 3, 'updatedReplicas': 1, 'readyReplicas': 2, 'availableReplicas': 2, 'unavailableReplicas': 1, 'observedGeneration': 7,
                   'conditions': [{'type': 'Progressing', 'status': 'True', 'reason': 'ReplicaSetUpdated', 'message': 'ReplicaSet "catalog-service-7d9f8b6c5" is progressing.',
                                   'lastTransitionTime': '2026-10-25T07:45:00Z'}]},
    }


def replicaset_json(name: str, revision: str, image: str, created: str, replicas: int, owner: str = 'uid-catalog-service', cause: str | None = None) -> dict:
    meta = leaky_meta(name, creationTimestamp=created, ownerReferences=[{'kind': 'Deployment', 'name': 'catalog-service', 'uid': owner, 'controller': True}])
    meta['annotations']['deployment.kubernetes.io/revision'] = revision
    if cause:
        meta['annotations']['kubernetes.io/change-cause'] = cause
    return {'metadata': meta, 'spec': {'replicas': replicas, 'template': {'metadata': leaky_meta('t'), 'spec': {**leaky_spec(), 'containers': [leaky_container(image=image)]}}},
            'status': {'replicas': replicas, 'readyReplicas': replicas}}


PODS = '/api/v1/namespaces/store/pods'
POD = PODS + '/catalog-service-7d9f8b6c5-x2b4k'
EVENTS = '/api/v1/namespaces/store/events'
DEPLOY = '/apis/apps/v1/namespaces/store/deployments/catalog-service'
RSETS = '/apis/apps/v1/namespaces/store/replicasets'
LOG = POD + '/log'


def rollout_routes() -> dict:
    return {DEPLOY: deployment_json(), RSETS: {'items': [
        replicaset_json('catalog-service-5b6c7d8f9', '5', 'images/catalog-service/master:old11111', '2026-10-20T05:00:00Z', 0),
        replicaset_json('catalog-service-7d9f8b6c5', '6', 'images/catalog-service/master:abc12345', '2026-10-25T07:44:00Z', 1, cause='x' * 500),
        replicaset_json('catalog-service-other', '9', 'images/other:1', '2026-10-25T07:44:00Z', 1, owner='uid-someone-else'),
    ]}}


def test_without_a_token_file_kube_is_off_and_no_call_goes_out(tmp_path):
    # given
    api = FakeApi({PODS: {'items': []}})
    k = Kube(root=tmp_path, base=BASE, transport=api)

    # when
    with pytest.raises(KubeError) as e:
        k.pods('store')

    # then
    assert k.enabled is False
    assert 'not running in a cluster' in str(e.value)
    assert api.calls == []


def test_pods_lists_the_namespace_with_the_service_account_token_and_the_selector(tmp_path):
    # given
    k, api = kube(tmp_path, {PODS: {'items': [pod_json()]}})

    # when
    rows = k.pods('store', 'app.kubernetes.io/name=catalog-service')

    # then
    path, query, headers = api.calls[0]
    assert path == PODS
    assert query['labelSelector'] == ['app.kubernetes.io/name=catalog-service']
    assert headers['Authorization'] == f'Bearer {TOKEN}'
    assert k.enabled is True
    assert rows == [{'name': 'catalog-service-7d9f8b6c5-x2b4k', 'phase': 'Running', 'ready': '0/1', 'restarts': 3, 'node': 'node-1', 'started': '2026-10-25T06:00:00Z',
                     'owner': 'ReplicaSet/catalog-service-7d9f8b6c5', 'images': ['images/catalog-service/master:abc12345'], 'waiting': 'CrashLoopBackOff',
                     'lastTermination': {'container': 'java', 'reason': 'OOMKilled', 'exitCode': 137, 'finishedAt': '2026-10-25T07:49:30Z'}}]


def test_the_token_is_read_again_on_every_call_because_it_rotates(tmp_path):
    # given
    k, api = kube(tmp_path, {PODS: {'items': []}})

    # when
    k.pods('store')
    (tmp_path / 'token').write_text('rotated-token')
    k.pods('store')

    # then
    assert [c[2]['Authorization'] for c in api.calls] == [f'Bearer {TOKEN}', 'Bearer rotated-token']


def test_pod_returns_its_status_conditions_and_each_containers_state_and_last_state(tmp_path):
    # given
    k, api = kube(tmp_path, {POD: pod_json()})

    # when
    out = k.pod('store', 'catalog-service-7d9f8b6c5-x2b4k')

    # then
    assert api.calls[0][0] == POD
    assert out == {
        'name': 'catalog-service-7d9f8b6c5-x2b4k', 'namespace': 'store', 'phase': 'Running', 'node': 'node-1', 'started': '2026-10-25T06:00:00Z',
        'owner': {'kind': 'ReplicaSet', 'name': 'catalog-service-7d9f8b6c5'},
        'conditions': [{'type': 'Ready', 'status': 'False', 'reason': 'ContainersNotReady', 'message': 'containers with unready status: [java]', 'at': '2026-10-25T07:50:00Z'}],
        'containers': [{'name': 'java', 'image': 'images/catalog-service/master:abc12345', 'ready': False, 'restartCount': 3,
                        'state': {'state': 'waiting', 'reason': 'CrashLoopBackOff', 'message': 'back-off 5m0s restarting failed container=java'},
                        'lastState': {'state': 'terminated', 'reason': 'OOMKilled', 'exitCode': 137, 'startedAt': '2026-10-25T07:40:00Z', 'finishedAt': '2026-10-25T07:49:30Z'}}],
    }


def test_an_evicted_pod_keeps_its_reason_and_message(tmp_path):
    # given
    evicted = pod_json()
    evicted['status'] = {'phase': 'Failed', 'reason': 'Evicted', 'message': 'The node was low on resource: memory.'}
    k, _ = kube(tmp_path, {POD: evicted})

    # when
    out = k.pod('store', 'catalog-service-7d9f8b6c5-x2b4k')

    # then
    assert (out['phase'], out['reason'], out['message']) == ('Failed', 'Evicted', 'The node was low on resource: memory.')
    assert out['containers'] == []


def event(name: str, kind: str, reason: str, last: str | None, message: str = 'm', count: int = 1, kind_type: str = 'Warning', event_time: str | None = None) -> dict:
    return {'metadata': leaky_meta(f'{name}.{reason}', creationTimestamp='2026-10-25T00:00:00Z'), 'involvedObject': {'kind': kind, 'name': name, 'namespace': 'store', 'uid': 'u'},
            'type': kind_type, 'reason': reason, 'message': message, 'count': count, 'firstTimestamp': last, 'lastTimestamp': last, 'eventTime': event_time,
            'source': {'component': 'kubelet', 'host': 'node-1'}, 'reportingComponent': 'kubelet'}


def test_events_of_the_last_minutes_come_newest_first_for_an_object_and_its_children(tmp_path):
    # given
    items = [
        event('catalog-service-7d9f8b6c5-x2b4k', 'Pod', 'Unhealthy', '2026-10-25T07:55:00Z', 'Liveness probe failed: HTTP probe failed with statuscode: 503', count=4),
        event('catalog-service', 'Deployment', 'ScalingReplicaSet', '2026-10-25T07:44:00Z', 'Scaled up replica set catalog-service-7d9f8b6c5 to 1', kind_type='Normal'),
        event('catalog-service-7d9f8b6c5-x2b4k', 'Pod', 'BackOff', None, 'Back-off restarting failed container', event_time='2026-10-25T07:58:00.123456Z'),
        event('catalog-service-7d9f8b6c5-x2b4k', 'Pod', 'Killing', '2026-10-25T05:00:00Z', 'too old'),
        event('catalog-service-api', 'Pod', 'Pulled', '2026-10-25T07:50:00Z', 'another app'),
        event('checkout-service-1', 'Pod', 'FailedScheduling', '2026-10-25T07:59:00Z', '0/9 nodes are available'),
    ]
    k, api = kube(tmp_path, {EVENTS: {'items': items}})

    # when
    rows = k.events('store', 'catalog-service', 60, NOW)

    # then
    assert api.calls[0][0] == EVENTS
    assert rows == [
        {'last': '2026-10-25T07:58:00.123456Z', 'first': None, 'count': 1, 'type': 'Warning', 'reason': 'BackOff', 'object': 'Pod/catalog-service-7d9f8b6c5-x2b4k', 'message': 'Back-off restarting failed container'},
        {'last': '2026-10-25T07:55:00Z', 'first': '2026-10-25T07:55:00Z', 'count': 4, 'type': 'Warning', 'reason': 'Unhealthy', 'object': 'Pod/catalog-service-7d9f8b6c5-x2b4k',
         'message': 'Liveness probe failed: HTTP probe failed with statuscode: 503'},
        {'last': '2026-10-25T07:44:00Z', 'first': '2026-10-25T07:44:00Z', 'count': 1, 'type': 'Normal', 'reason': 'ScalingReplicaSet', 'object': 'Deployment/catalog-service',
         'message': 'Scaled up replica set catalog-service-7d9f8b6c5 to 1'},
    ]


def test_events_without_an_object_cover_the_namespace_and_cut_long_messages(tmp_path):
    # given
    items = [event('checkout-service-1', 'Pod', 'FailedScheduling', '2026-10-25T07:59:00Z', 'x' * 900), event('a', 'Pod', 'Pulled', '2026-10-25T07:30:00Z')]
    k, _ = kube(tmp_path, {EVENTS: {'items': items}})

    # when
    rows = k.events('store', None, 10, NOW)

    # then
    assert [r['reason'] for r in rows] == ['FailedScheduling']
    assert len(rows[0]['message']) <= 500


def test_rollout_lists_the_deployments_replicaset_revisions_newest_first(tmp_path):
    # given
    k, api = kube(tmp_path, rollout_routes())

    # when
    out = k.rollout('store', 'catalog-service')

    # then
    assert [c[0] for c in api.calls] == [DEPLOY, RSETS]
    assert api.calls[1][1]['labelSelector'] == ['app.kubernetes.io/instance=store-catalog-service,app.kubernetes.io/name=catalog-service']
    assert out['name'] == 'catalog-service'
    assert out['replicas'] == {'desired': 2, 'current': 3, 'updated': 1, 'ready': 2, 'available': 2, 'unavailable': 1}
    assert out['conditions'] == [{'type': 'Progressing', 'status': 'True', 'reason': 'ReplicaSetUpdated', 'message': 'ReplicaSet "catalog-service-7d9f8b6c5" is progressing.', 'at': '2026-10-25T07:45:00Z'}]
    assert out['images'] == [{'container': 'java', 'image': 'images/catalog-service/master:abc12345'}]
    assert out['selector'] == {'app.kubernetes.io/name': 'catalog-service', 'app.kubernetes.io/instance': 'store-catalog-service'}
    assert [(r['revision'], r['name'], r['images'], r['created'], r['replicas'], r['ready']) for r in out['revisions']] == [
        (6, 'catalog-service-7d9f8b6c5', ['images/catalog-service/master:abc12345'], '2026-10-25T07:44:00Z', 1, 1),
        (5, 'catalog-service-5b6c7d8f9', ['images/catalog-service/master:old11111'], '2026-10-20T05:00:00Z', 0, 0),
    ]
    assert len(out['revisions'][0]['changeCause']) == 200 and 'changeCause' not in out['revisions'][1]


def test_rollout_names_the_argo_cd_app_from_the_deployments_tracking_label_only(tmp_path):
    # given
    tracked = rollout_routes()
    tracked[DEPLOY]['metadata']['labels']['argocd.argoproj.io/instance'] = 'store-catalog-service-prod'
    k, _ = kube(tmp_path, tracked)
    plain, _ = kube(tmp_path, rollout_routes())

    # when
    out, untracked = k.rollout('store', 'catalog-service'), plain.rollout('store', 'catalog-service')

    # then
    assert out['argoApp'] == 'store-catalog-service-prod'
    assert 'argoApp' not in untracked
    assert 'SECRET-LABEL-VALUE' not in json.dumps(out)


def log_lines(n: int) -> bytes:
    return '\n'.join(f'line {i}' for i in range(1, n + 1)).encode() + b'\n'


def test_previous_logs_ask_for_the_previous_container_and_cap_the_tail_at_200(tmp_path):
    # given
    k, api = kube(tmp_path, {LOG: log_lines(300)})

    # when
    text = k.previous_logs('store', 'catalog-service-7d9f8b6c5-x2b4k', 'java', 500)

    # then
    path, query, headers = api.calls[0]
    assert path == LOG
    assert query == {'previous': ['true'], 'tailLines': [str(LOG_TAIL_MAX)], 'container': ['java']}
    assert headers['Accept'] == 'text/plain, application/json'
    lines = text.splitlines()
    assert LOG_TAIL_MAX == 200
    assert len(lines) == 200 and lines[0] == 'line 101' and lines[-1] == 'line 300'


def test_previous_logs_keep_the_crash_end_mask_credentials_and_go_through_redact(tmp_path):
    # given
    body = '\n'.join(['start ' + 'y' * 5000] * 8 + [
        'Exception in monitor: mongodb://usr_app:Pa55word@mongo-0.svc:27017/db',
        'login failed for adam.baker@example.com with Bearer abcdefghijklmnop',
        'Application run failed: Vault is sealed',
    ]).encode()
    k, _ = kube(tmp_path, {LOG: body})

    # when
    text = k.previous_logs('store', 'catalog-service-7d9f8b6c5-x2b4k', None, 50)

    # then
    assert text.rstrip().endswith('Application run failed: Vault is sealed')
    assert 'Pa55word' not in text and 'mongodb://***@mongo-0.svc' in text
    assert 'adam.baker@example.com' not in text and 'abcdefghijklmnop' not in text and '[redacted]' in text
    assert len(text) < 20_000
    assert all(len(line) <= 2_100 for line in text.splitlines())


NEVER = HttpFailed(400, 'previous terminated container "java" in pod "catalog-service-7d9f8b6c5-x2b4k" not found')


def previous_or_current(current: bytes, previous=NEVER):
    return lambda query: previous if query.get('previous') == ['true'] else current


def test_a_pod_that_never_restarted_falls_back_once_to_its_current_logs_and_says_so(tmp_path):
    # given
    k, api = kube(tmp_path, {LOG: previous_or_current(b'boot ok\nlistening on 8080')})

    # when
    named = k.previous_logs('store', 'catalog-service-7d9f8b6c5-x2b4k', 'java', 50)
    unnamed = k.previous_logs('store', 'catalog-service-7d9f8b6c5-x2b4k', None, 50)

    # then
    assert named.splitlines()[0].startswith('no previous run: container java never restarted, so these are its current logs')
    assert 'its container never restarted' in unnamed.splitlines()[0]
    assert named.endswith('boot ok\nlistening on 8080')
    assert [c[1].get('previous') for c in api.calls] == [['true'], None, ['true'], None]
    assert api.calls[1][1]['tailLines'] == ['50'] and api.calls[1][1]['container'] == ['java']


def test_the_current_log_fallback_is_masked_like_any_log(tmp_path):
    # given
    k, _ = kube(tmp_path, {LOG: previous_or_current(b'login failed for adam.baker@example.com with Bearer abcdefghijklmnop')})

    # when
    text = k.previous_logs('store', 'catalog-service-7d9f8b6c5-x2b4k', 'java', 50)

    # then
    assert 'adam.baker@example.com' not in text and 'abcdefghijklmnop' not in text


def test_when_the_current_log_cannot_be_read_either_the_never_restarted_error_stays(tmp_path):
    # given
    k, api = kube(tmp_path, {LOG: previous_or_current(HttpFailed(400, 'container "java" in pod "p" is waiting to start: ContainerCreating'))})

    # when
    with pytest.raises(KubeError) as named:
        k.previous_logs('store', 'catalog-service-7d9f8b6c5-x2b4k', 'java', 50)

    # then
    assert str(named.value) == ('catalog-service-7d9f8b6c5-x2b4k has no previous logs: container java never restarted, so there is no earlier run to read, '
                                'and its current logs could not be read either')
    assert len(api.calls) == 2


def test_a_json_status_answer_to_a_log_request_is_read_as_the_api_message(tmp_path):
    # given
    calls = []

    def strict_server(url, headers, timeout):
        calls.append(headers['Accept'])
        if 'application/json' not in headers['Accept']:
            raise HttpFailed(406, 'only the following media types are accepted: application/json, application/yaml, application/vnd.kubernetes.protobuf')
        raise HttpFailed(400, 'previous terminated container "app" in pod "cloudflare-exporter-5d8c7b9f6-k4m2p" not found')

    (tmp_path / 'token').write_text(TOKEN + '\n')
    k = Kube(root=tmp_path, base=BASE, transport=strict_server)

    # when
    with pytest.raises(KubeError) as e:
        k.previous_logs('observability', 'cloudflare-exporter-5d8c7b9f6-k4m2p', 'app', 10)

    # then
    assert calls == ['text/plain, application/json', 'text/plain, application/json']
    assert 'never restarted' in str(e.value) and '406' not in str(e.value)


def test_an_api_error_names_the_status_and_message_and_never_the_token(tmp_path):
    # given
    k, _ = kube(tmp_path, {
        PODS: HttpFailed(403, 'pods is forbidden: User "system:serviceaccount:platform:skopax" cannot list resource "pods"'),
        LOG: HttpFailed(400, f'a container name must be specified for pod x, choose one of: [java sidecar] (token {TOKEN})'),
        EVENTS: OSError(f'connection reset, Authorization: Bearer {TOKEN}'),
    })

    # when
    errors = []
    for op in (lambda: k.pods('store'), lambda: k.previous_logs('store', 'catalog-service-7d9f8b6c5-x2b4k', None, 10),
               lambda: k.events('store', None, 60, NOW)):
        with pytest.raises(KubeError) as e:
            op()
        errors.append(str(e.value))

    # then
    assert errors[0].startswith('HTTP 403') and 'cannot list resource "pods"' in errors[0]
    assert errors[1].startswith('HTTP 400') and 'a container name must be specified' in errors[1]
    assert 'unreachable' in errors[2]
    assert all(TOKEN not in x and 'sa-token-body' not in x for x in errors)


def test_names_that_are_not_kubernetes_names_are_refused_before_any_call(tmp_path):
    # given
    k, api = kube(tmp_path, {})

    # when
    refused = []
    for op in (lambda: k.pod('store', '../secrets/db'), lambda: k.pods('Store_Prod'), lambda: k.rollout('store', 'a/b'),
               lambda: k.previous_logs('store', 'p', 'java?x=1', 10), lambda: k.pods('store', 'app=a;rm')):
        with pytest.raises(KubeError):
            op()
        refused.append(True)

    # then
    assert len(refused) == 5
    assert api.calls == []


def test_pod_deployment_and_event_answers_carry_no_env_args_command_annotations_managed_fields_or_volumes(tmp_path):
    # given
    routes = {PODS: {'items': [pod_json()]}, POD: pod_json(), EVENTS: {'items': [event('catalog-service-7d9f8b6c5-x2b4k', 'Pod', 'Unhealthy', '2026-10-25T07:55:00Z')]}, **rollout_routes()}
    k, _ = kube(tmp_path, routes)

    # when
    rendered = json.dumps([k.pods('store'), k.pod('store', 'catalog-service-7d9f8b6c5-x2b4k'), k.rollout('store', 'catalog-service'),
                           k.events('store', None, 60, NOW)])

    # then
    assert [s for s in SECRETS if s in rendered] == []
    assert all(word not in rendered for word in ('env', 'args', 'command', 'annotations', 'managedFields', 'volumes', 'uid', 'podIP', 'kubelet'))


def node_json(name: str, ready: str = 'True', pressure: str | None = None, cordoned: bool = False) -> dict:
    conditions = [{'type': 'Ready', 'status': ready, 'reason': 'KubeletReady' if ready == 'True' else 'NodeStatusUnknown',
                   'message': 'Kubelet stopped posting node status.' if ready != 'True' else 'kubelet is posting ready status', 'lastTransitionTime': '2026-11-09T06:18:00Z'}]
    for t in ('MemoryPressure', 'DiskPressure', 'PIDPressure'):
        conditions.append({'type': t, 'status': 'True' if t == pressure else 'False', 'lastTransitionTime': '2026-11-01T00:00:00Z'})
    return {'metadata': {**leaky_meta(name, creationTimestamp='2026-09-01T00:00:00Z'), 'namespace': None,
                         'labels': {'node-role.kubernetes.io/worker': '', 'nodegroup': 'general', 'leak': 'SECRET-LABEL-VALUE'}},
            'spec': {'unschedulable': cordoned, 'taints': [{'key': 'node.kubernetes.io/unreachable', 'effect': 'NoExecute'}] if ready != 'True' else []},
            'status': {'conditions': conditions, 'nodeInfo': {'kubeletVersion': 'v1.33.5', 'osImage': 'Ubuntu 24.04', 'kernelVersion': '6.8', 'containerRuntimeVersion': 'containerd://2.0'},
                       'allocatable': {'cpu': '7800m', 'memory': '30Gi', 'pods': '110', 'hugepages-2Mi': '0'}, 'capacity': {'cpu': '8', 'memory': '32Gi', 'pods': '110'}}}


def node_pod(name: str, ns: str, ready: bool, node: str = 'east-worker-3', owner_uid: str = 'uid-rs') -> dict:
    p = pod_json(name)
    p['metadata']['namespace'] = ns
    p['metadata']['ownerReferences'] = [{'kind': 'DaemonSet', 'name': 'fluentbit-fluent-bit', 'controller': True, 'uid': owner_uid}]
    p['spec']['nodeName'] = node
    p['status']['containerStatuses'][0]['ready'] = ready
    if ready:
        p['status']['containerStatuses'][0]['state'] = {'running': {'startedAt': '2026-11-09T05:00:00Z'}}
    return p


NODES = '/api/v1/nodes'
EAST_NOW = dt.datetime(2026, 11, 9, 6, 40, tzinfo=dt.timezone.utc).timestamp()


def test_nodes_lists_problem_nodes_first_with_pressure_cordon_and_taints_and_no_other_labels(tmp_path):
    # given
    k, api = kube(tmp_path, {NODES: {'items': [node_json('east-worker-1'), node_json('east-worker-3', ready='Unknown'),
                                                node_json('east-worker-2', pressure='DiskPressure'), node_json('east-worker-4', cordoned=True)]}})

    # when
    rows = k.nodes()

    # then
    assert [r['name'] for r in rows] == ['east-worker-2', 'east-worker-3', 'east-worker-4', 'east-worker-1']
    down = rows[1]
    assert down['ready'] == 'Unknown' and down['readySince'] == '2026-11-09T06:18:00Z' and down['taints'] == ['node.kubernetes.io/unreachable:NoExecute']
    assert rows[0]['pressure'] == ['DiskPressure'] and rows[2]['unschedulable'] is True
    assert rows[3]['roles'] == ['worker'] and rows[3]['labels'] == {'nodegroup': 'general'} and rows[3]['allocatable'] == {'cpu': '7800m', 'memory': '30Gi', 'pods': '110'}
    assert 'SECRET' not in json.dumps(rows)
    assert api.calls[0][1] == {'limit': ['200']}


def test_node_shows_conditions_the_pods_on_it_and_its_last_hour_of_events(tmp_path):
    # given
    pods = {'items': [node_pod('fluentbit-fluent-bit-abcde', 'cluster-logging', False), node_pod('replication-job-1', 'airbyte', False),
                      node_pod('reports-service-7d9f8b6c5-x2b4k', 'reports', True)]}
    events = {'items': [
        {'lastTimestamp': '2026-11-09T06:18:30Z', 'type': 'Normal', 'reason': 'NodeNotReady', 'message': 'Node east-worker-3 status is now: NodeNotReady', 'count': 1},
        {'lastTimestamp': '2026-11-09T04:00:00Z', 'type': 'Normal', 'reason': 'Starting', 'message': 'old', 'count': 1}]}
    k, api = kube(tmp_path, {NODES + '/east-worker-3': node_json('east-worker-3', ready='Unknown'), '/api/v1/pods': pods, '/api/v1/events': events})

    # when
    out = k.node('east-worker-3', EAST_NOW)

    # then
    assert out['ready'] == 'Unknown' and out['conditions'][0]['message'] == 'Kubelet stopped posting node status.'
    assert out['info']['kubelet'] == 'v1.33.5' and out['capacity'] == {'cpu': '8', 'memory': '32Gi', 'pods': '110'}
    assert out['pods']['count'] == 3 and out['pods']['byNamespace'] == {'cluster-logging': 1, 'airbyte': 1, 'reports': 1}
    assert [p['name'] for p in out['pods']['notReady']] == ['fluentbit-fluent-bit-abcde', 'replication-job-1']
    assert [e['reason'] for e in out['events']] == ['NodeNotReady']
    asked = {path: query for path, query, _ in api.calls}
    assert asked['/api/v1/pods']['fieldSelector'] == ['spec.nodeName=east-worker-3']
    assert asked['/api/v1/events']['fieldSelector'] == ['involvedObject.kind=Node,involvedObject.name=east-worker-3']
    assert 'SECRET' not in json.dumps(out)


def test_daemonset_shows_misscheduled_counts_its_scheduling_rules_and_each_pod_with_its_node(tmp_path):
    # given
    ds = {'metadata': leaky_meta('fluentbit-fluent-bit', generation=4, uid='uid-ds'),
          'spec': {'selector': {'matchLabels': {'app.kubernetes.io/name': 'fluent-bit'}}, 'updateStrategy': {'type': 'RollingUpdate', 'rollingUpdate': {'maxUnavailable': 1}},
                   'template': {'metadata': leaky_meta('t'), 'spec': {**leaky_spec(), 'nodeSelector': {'nodegroup': 'general'},
                                                                    'tolerations': [{'operator': 'Exists', 'effect': 'NoSchedule'}, {'key': 'dedicated', 'operator': 'Equal', 'value': 'data', 'effect': 'NoExecute'}]}}},
          'status': {'desiredNumberScheduled': 6, 'currentNumberScheduled': 6, 'numberReady': 5, 'updatedNumberScheduled': 6, 'numberAvailable': 5,
                     'numberUnavailable': 1, 'numberMisscheduled': 1, 'observedGeneration': 4}}
    pods = {'items': [node_pod('fluentbit-fluent-bit-aaaaa', 'cluster-logging', True, 'east-worker-1', 'uid-ds'),
                      node_pod('fluentbit-fluent-bit-bbbbb', 'cluster-logging', False, 'east-worker-3', 'uid-ds'),
                      node_pod('stranger-ccccc', 'cluster-logging', False, 'east-worker-2', 'uid-other')]}
    k, api = kube(tmp_path, {'/apis/apps/v1/namespaces/cluster-logging/daemonsets/fluentbit-fluent-bit': ds, '/api/v1/namespaces/cluster-logging/pods': pods})

    # when
    out = k.daemonset('cluster-logging', 'fluentbit-fluent-bit')

    # then
    assert out['pods'] == {'desired': 6, 'current': 6, 'ready': 5, 'updated': 6, 'available': 5, 'unavailable': 1, 'misscheduled': 1}
    assert out['generation'] == {'wanted': 4, 'observed': 4} and out['updateStrategy'] == {'type': 'RollingUpdate', 'maxUnavailable': 1}
    assert out['nodeSelector'] == {'nodegroup': 'general'} and out['tolerations'] == ['* Exists:NoSchedule', 'dedicated Equal=data:NoExecute']
    assert [(p['name'], p['node']) for p in out['podsOnNodes']] == [('fluentbit-fluent-bit-bbbbb', 'east-worker-3'), ('fluentbit-fluent-bit-aaaaa', 'east-worker-1')]
    assert api.calls[1][1]['labelSelector'] == ['app.kubernetes.io/name=fluent-bit']
    assert 'SECRET' not in json.dumps(out)


@pytest.mark.parametrize('call', [lambda k: k.node('Bad Name', EAST_NOW), lambda k: k.daemonset('cluster-logging', '../x')])
def test_node_and_daemonset_names_are_checked_before_any_call(tmp_path, call):
    # given
    k, api = kube(tmp_path, {})

    # when
    with pytest.raises(KubeError) as e:
        call(k)

    # then
    assert 'is not a Kubernetes name' in str(e.value) and api.calls == []


def test_a_repeating_event_series_is_dated_by_its_last_observation_not_its_first(tmp_path):
    # given
    pending = event('checkout-service-7d9f8b6c5-x2b4k', 'Pod', 'FailedScheduling', None, '0/9 nodes are available: 9 Insufficient memory.', count=0,
                    event_time='2026-10-25T05:00:00.000000Z')
    pending['series'] = {'count': 180, 'lastObservedTime': '2026-10-25T07:59:20.000000Z'}
    k, _ = kube(tmp_path, {EVENTS: {'items': [pending]}})

    # when
    rows = k.events('store', 'checkout-service', 60, NOW)

    # then
    assert [(r['reason'], r['last'], r['count']) for r in rows] == [('FailedScheduling', '2026-10-25T07:59:20.000000Z', 180)]


def test_a_failed_call_names_the_error_type_never_its_text(tmp_path):
    # given
    k, _ = kube(tmp_path, {'/api/v1/nodes': OSError('connection reset by 10.0.0.7 while sending secret-value')})

    # when
    with pytest.raises(KubeError) as e:
        k.nodes()

    # then
    assert str(e.value) == '/api/v1/nodes unreachable (OSError)'
