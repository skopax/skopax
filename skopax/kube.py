import datetime as dt
import json
import os
import re
import ssl
import urllib.error
import urllib.request
from pathlib import Path
from typing import Callable
from urllib.parse import quote, urlencode

from skopax.errkind import kind
from skopax.redact import redact, redact_lines
from skopax.remote import NoRedirect

SA_ROOT = Path('/var/run/secrets/kubernetes.io/serviceaccount')
DEFAULT_HOST = 'kubernetes.default.svc'
TIMEOUT = 15
MAX_BYTES = 8 * 1024 * 1024
LIST_LIMIT = 200
EVENT_LIMIT = 500
LOG_TAIL_MAX = 200
LOG_LINE_LIMIT = 2_000
LOG_CHARS = 18_000
MESSAGE_LIMIT = 500
CAUSE_LIMIT = 200
ERROR_LIMIT = 300
REVISIONS = 10
OFF = 'kube is off: the service is not running in a cluster (no service account token)'
NAMESPACE = re.compile(r'[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?')
NAME = re.compile(r'[a-z0-9]([-a-z0-9.]{0,251}[a-z0-9])?')
CONTAINER = re.compile(r'[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?')
SELECTOR = re.compile(r'[A-Za-z0-9_./=!, ()-]{1,200}')
SUFFIX = '[bcdfghjklmnpqrstvwxz2456789]'
CHILD = re.compile(rf'(?:-{SUFFIX}{{6,10}}(?:-{SUFFIX}{{5}})?|-{SUFFIX}{{5}}|-\d+)')
CREDENTIALS = re.compile(r'://[^@/\s]+@')
REVISION = 'deployment.kubernetes.io/revision'
CHANGE_CAUSE = 'kubernetes.io/change-cause'
ARGO_APP = 'argocd.argoproj.io/instance'
TEXT_ACCEPT = 'text/plain, application/json'
NO_PREVIOUS = 'previous terminated container'
NODE_ROLE = 'node-role.kubernetes.io/'
NODE_LABELS = ('nodegroup', 'node.kubernetes.io/instance-type', 'topology.kubernetes.io/zone')
PRESSURES = ('MemoryPressure', 'DiskPressure', 'PIDPressure', 'NetworkUnavailable')
RESOURCES = ('cpu', 'memory', 'pods', 'ephemeral-storage')
NODE_EVENT_MINUTES = 60

Transport = Callable[[str, dict[str, str], float], bytes]


class KubeError(Exception):
    def __init__(self, message: str, code: int | None = None, reason: str = ''):
        super().__init__(message)
        self.message = message
        self.code = code
        self.reason = reason


class HttpFailed(Exception):
    def __init__(self, code: int, message: str):
        super().__init__(f'HTTP {code}: {message}')
        self.code = code
        self.message = message


def status_message(body: bytes) -> str:
    try:
        return str(json.loads(body).get('message') or '')
    except (ValueError, AttributeError):
        return body.decode('utf-8', 'replace')


def sender(ca_file: Path) -> Transport:
    opener = None

    def send(url: str, headers: dict[str, str], timeout: float) -> bytes:
        nonlocal opener
        if opener is None:
            context = ssl.create_default_context(cafile=str(ca_file))
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), urllib.request.HTTPSHandler(context=context), NoRedirect)
        try:
            with opener.open(urllib.request.Request(url, headers=headers, method='GET'), timeout=timeout) as resp:
                body = resp.read(MAX_BYTES + 1)
        except urllib.error.HTTPError as e:
            raise HttpFailed(e.code, status_message(e.read(4096))) from None
        if len(body) > MAX_BYTES:
            raise HttpFailed(413, 'the answer is over 8 MB; narrow it with a selector or an object')
        return body

    return send


def default_base() -> str:
    host = os.environ.get('KUBERNETES_SERVICE_HOST') or DEFAULT_HOST
    port = os.environ.get('KUBERNETES_SERVICE_PORT_HTTPS') or os.environ.get('KUBERNETES_SERVICE_PORT') or '443'
    return f"https://{f'[{host}]' if ':' in host else host}:{port}"


def checked(pattern: re.Pattern, value, what: str) -> str:
    text = str(value or '')
    if not pattern.fullmatch(text):
        raise KubeError(f'{what} {text[:80]!r} is not a Kubernetes name')
    return text


def clean(text, limit: int = MESSAGE_LIMIT) -> str | None:
    if text is None:
        return None
    return redact(CREDENTIALS.sub('://***@', str(text)))[:limit]


def clean_lines(texts: list[str], limit: int) -> list[str]:
    return [t[:limit] for t in redact_lines([CREDENTIALS.sub('://***@', str(t)) for t in texts])]


def when(value) -> float | None:
    try:
        return dt.datetime.fromisoformat(str(value)).timestamp() if value else None
    except ValueError:
        return None


def compact(d: dict) -> dict:
    return {k: v for k, v in d.items() if v is not None}


def last_seen(e: dict):
    return (e.get('series') or {}).get('lastObservedTime') or e.get('lastTimestamp') or e.get('eventTime') or (e.get('metadata') or {}).get('creationTimestamp')


def owner_of(meta: dict) -> dict | None:
    refs = meta.get('ownerReferences') or []
    ref = next((r for r in refs if r.get('controller')), refs[0] if refs else None)
    return {'kind': ref.get('kind'), 'name': ref.get('name')} if ref else None


def container_state(state: dict | None) -> dict | None:
    for name in ('running', 'waiting', 'terminated'):
        s = (state or {}).get(name)
        if s is not None:
            return compact({'state': name, 'reason': s.get('reason'), 'exitCode': s.get('exitCode'), 'signal': s.get('signal'), 'message': clean(s.get('message')),
                            'startedAt': s.get('startedAt'), 'finishedAt': s.get('finishedAt')})
    return None


def container(c: dict) -> dict:
    return compact({'name': c.get('name'), 'image': c.get('image'), 'ready': c.get('ready'), 'restartCount': c.get('restartCount'),
                    'state': container_state(c.get('state')), 'lastState': container_state(c.get('lastState'))})


def condition(c: dict) -> dict:
    return compact({'type': c.get('type'), 'status': c.get('status'), 'reason': c.get('reason'), 'message': clean(c.get('message')), 'at': c.get('lastTransitionTime')})


def images_of(spec: dict) -> list[dict]:
    return [{'container': c.get('name'), 'image': c.get('image')} for c in (spec.get('containers') or [])]


def pod_row(p: dict) -> dict:
    meta, spec, st = p.get('metadata') or {}, p.get('spec') or {}, p.get('status') or {}
    cs = st.get('containerStatuses') or []
    owner = owner_of(meta)
    waiting = next((c['state']['waiting'].get('reason') for c in cs if (c.get('state') or {}).get('waiting')), None)
    last = [(c, c['lastState']['terminated']) for c in cs if (c.get('lastState') or {}).get('terminated')]
    term = None
    if last:
        c, t = max(last, key=lambda x: x[1].get('finishedAt') or '')
        term = compact({'container': c.get('name'), 'reason': t.get('reason'), 'exitCode': t.get('exitCode'), 'finishedAt': t.get('finishedAt')})
    images = sorted({c.get('image') for c in cs if c.get('image')} or {c.get('image') for c in spec.get('containers') or [] if c.get('image')})
    return compact({'name': meta.get('name'), 'phase': st.get('phase'), 'reason': st.get('reason'),
                    'ready': f"{sum(1 for c in cs if c.get('ready'))}/{len(cs) or len(spec.get('containers') or [])}",
                    'restarts': sum(int(c.get('restartCount') or 0) for c in cs), 'node': spec.get('nodeName'), 'started': st.get('startTime'),
                    'owner': f"{owner['kind']}/{owner['name']}" if owner else None, 'images': images, 'waiting': waiting, 'lastTermination': term})


def taints_of(spec: dict) -> list[str]:
    return [f"{t.get('key')}{'=' + str(t['value']) if t.get('value') else ''}:{t.get('effect')}" for t in spec.get('taints') or []]


def resources_of(values: dict | None) -> dict:
    return {k: values[k] for k in RESOURCES if k in (values or {})}


def node_row(n: dict) -> dict:
    meta, spec, st = n.get('metadata') or {}, n.get('spec') or {}, n.get('status') or {}
    conditions = {c.get('type'): c for c in st.get('conditions') or []}
    ready = conditions.get('Ready') or {}
    labels = meta.get('labels') or {}
    return compact({'name': meta.get('name'), 'ready': ready.get('status') or 'Unknown', 'readySince': ready.get('lastTransitionTime'),
                    'pressure': [t for t in PRESSURES if (conditions.get(t) or {}).get('status') == 'True'] or None,
                    'unschedulable': True if spec.get('unschedulable') else None, 'taints': taints_of(spec) or None,
                    'roles': sorted(k[len(NODE_ROLE):] for k in labels if k.startswith(NODE_ROLE)) or None,
                    'labels': {k: clean(labels[k], CAUSE_LIMIT) for k in NODE_LABELS if k in labels} or None,
                    'kubelet': (st.get('nodeInfo') or {}).get('kubeletVersion'), 'created': meta.get('creationTimestamp'),
                    'allocatable': resources_of(st.get('allocatable')) or None})


def short_pod(p: dict) -> dict:
    row = pod_row(p)
    meta = p.get('metadata') or {}
    return compact({'namespace': meta.get('namespace'), 'name': row.get('name'), 'phase': row.get('phase'), 'ready': row.get('ready'), 'restarts': row.get('restarts'),
                    'node': row.get('node'), 'owner': row.get('owner'), 'waiting': row.get('waiting'), 'lastTermination': row.get('lastTermination')})


def settled(p: dict) -> bool:
    st = p.get('status') or {}
    if st.get('phase') == 'Succeeded':
        return True
    cs = st.get('containerStatuses') or []
    return st.get('phase') == 'Running' and bool(cs) and all(c.get('ready') for c in cs)


def cluster_summary(rows: list[dict]) -> dict:
    nodes = [r for r in rows if r.get('name')]
    return {'total': len(nodes), 'ready': sum(1 for r in nodes if r.get('ready') == 'True'),
            'notReady': [r['name'] for r in nodes if r.get('ready') != 'True'],
            'pressure': [f"{r['name']}: {', '.join(r['pressure'])}" for r in nodes if r.get('pressure')],
            'unschedulable': [r['name'] for r in nodes if r.get('unschedulable')],
            'nodes': [{k: r[k] for k in ('name', 'ready', 'readySince', 'pressure', 'unschedulable', 'taints', 'roles', 'kubelet', 'created') if k in r} for r in nodes]}


def toleration(t: dict) -> str:
    return f"{t.get('key') or '*'}{' ' + t['operator'] if t.get('operator') else ''}{'=' + str(t['value']) if t.get('value') else ''}:{t.get('effect') or '*'}"


class Kube:
    def __init__(self, root: Path | str = SA_ROOT, base: str | None = None, transport: Transport | None = None):
        self.root = Path(root)
        self.base = (base or default_base()).rstrip('/')
        self.transport = transport or sender(self.root / 'ca.crt')

    @property
    def enabled(self) -> bool:
        return (self.root / 'token').is_file()

    def get(self, path: str, params: dict | None = None, text: bool = False):
        if not self.enabled:
            raise KubeError(OFF)
        token = (self.root / 'token').read_text().strip()
        query = {k: v for k, v in (params or {}).items() if v is not None}
        url = f'{self.base}{path}' + (f'?{urlencode(query)}' if query else '')
        headers = {'Authorization': f'Bearer {token}', 'Accept': TEXT_ACCEPT if text else 'application/json'}
        try:
            body = self.transport(url, headers, TIMEOUT)
        except HttpFailed as e:
            raise KubeError(self._scrub(f'HTTP {e.code} on {path}: {e.message}', token), e.code, e.message) from None
        except Exception as e:
            raise KubeError(f'{path} unreachable ({kind(e)})') from None
        if text:
            return body.decode('utf-8', 'replace')
        try:
            return json.loads(body)
        except ValueError:
            raise KubeError(f'{path} answered without JSON') from None

    @staticmethod
    def _scrub(message: str, token: str) -> str:
        if token:
            message = message.replace(token, '***')
        return redact(message)[:ERROR_LIMIT]

    def pods(self, namespace: str, selector: str | None = None) -> list[dict]:
        ns = checked(NAMESPACE, namespace, 'namespace')
        if selector and not SELECTOR.fullmatch(str(selector)):
            raise KubeError('selector takes labels only, like app.kubernetes.io/name=checkout-service')
        body = self.get(f'/api/v1/namespaces/{ns}/pods', {'labelSelector': selector or None, 'limit': LIST_LIMIT})
        rows = [pod_row(p) for p in body.get('items') or []]
        if (body.get('metadata') or {}).get('continue'):
            rows.append({'note': f'more than {LIST_LIMIT} pods; narrow it with a selector'})
        return rows

    def pod(self, namespace: str, name: str) -> dict:
        ns, pod = checked(NAMESPACE, namespace, 'namespace'), checked(NAME, name, 'pod')
        p = self.get(f'/api/v1/namespaces/{ns}/pods/{quote(pod)}')
        meta, spec, st = p.get('metadata') or {}, p.get('spec') or {}, p.get('status') or {}
        out = compact({'name': meta.get('name'), 'namespace': meta.get('namespace'), 'phase': st.get('phase'), 'reason': st.get('reason'), 'message': clean(st.get('message')),
                       'node': spec.get('nodeName'), 'started': st.get('startTime'), 'owner': owner_of(meta),
                       'conditions': [condition(c) for c in st.get('conditions') or []], 'containers': [container(c) for c in st.get('containerStatuses') or []]})
        if st.get('initContainerStatuses'):
            out['initContainers'] = [container(c) for c in st['initContainerStatuses']]
        return out

    def events(self, namespace: str, obj: str | None, since_minutes: int, now: float) -> list[dict]:
        ns = checked(NAMESPACE, namespace, 'namespace')
        name = checked(NAME, obj, 'object') if obj else None
        body = self.get(f'/api/v1/namespaces/{ns}/events', {'limit': EVENT_LIMIT})
        start = now - int(since_minutes) * 60
        rows = []
        for e in body.get('items') or []:
            involved = e.get('involvedObject') or {}
            if name and not (involved.get('name') == name or (str(involved.get('name') or '').startswith(name) and CHILD.fullmatch(str(involved['name'])[len(name):]))):
                continue
            last = last_seen(e)
            if (when(last) or 0) < start:
                continue
            rows.append({'last': last, 'first': e.get('firstTimestamp'), 'count': e.get('count') or (e.get('series') or {}).get('count') or 1, 'type': e.get('type'),
                         'reason': e.get('reason'), 'object': f"{involved.get('kind')}/{involved.get('name')}", 'message': clean(e.get('message'))})
        rows.sort(key=lambda r: when(r['last']) or 0, reverse=True)
        if (body.get('metadata') or {}).get('continue'):
            rows.append({'note': f'the namespace has more than {EVENT_LIMIT} events; only the first {EVENT_LIMIT} were read'})
        return rows

    def rollout(self, namespace: str, deployment: str) -> dict:
        ns, name = checked(NAMESPACE, namespace, 'namespace'), checked(NAME, deployment, 'deployment')
        d = self.get(f'/apis/apps/v1/namespaces/{ns}/deployments/{quote(name)}')
        meta, spec, st = d.get('metadata') or {}, d.get('spec') or {}, d.get('status') or {}
        labels = (spec.get('selector') or {}).get('matchLabels') or {}
        selector = ','.join(f'{k}={v}' for k, v in sorted(labels.items())) or None
        sets = self.get(f'/apis/apps/v1/namespaces/{ns}/replicasets', {'labelSelector': selector, 'limit': LIST_LIMIT})
        revisions = []
        for rs in sets.get('items') or []:
            rm = rs.get('metadata') or {}
            if not any(r.get('uid') == meta.get('uid') for r in rm.get('ownerReferences') or []):
                continue
            notes = rm.get('annotations') or {}
            try:
                revision = int(notes.get(REVISION))
            except (TypeError, ValueError):
                revision = None
            template = ((rs.get('spec') or {}).get('template') or {}).get('spec') or {}
            revisions.append(compact({'revision': revision, 'name': rm.get('name'), 'images': [c.get('image') for c in template.get('containers') or []],
                                      'created': rm.get('creationTimestamp'), 'replicas': (rs.get('status') or {}).get('replicas') or 0,
                                      'ready': (rs.get('status') or {}).get('readyReplicas') or 0, 'changeCause': clean(notes.get(CHANGE_CAUSE), CAUSE_LIMIT)}))
        revisions.sort(key=lambda r: r.get('revision') or 0, reverse=True)
        out = {'name': meta.get('name'), 'namespace': meta.get('namespace'),
               'replicas': {'desired': spec.get('replicas'), 'current': st.get('replicas') or 0, 'updated': st.get('updatedReplicas') or 0, 'ready': st.get('readyReplicas') or 0,
                            'available': st.get('availableReplicas') or 0, 'unavailable': st.get('unavailableReplicas') or 0},
               'conditions': [condition(c) for c in st.get('conditions') or []], 'images': images_of((spec.get('template') or {}).get('spec') or {}),
               'selector': labels, 'revisions': revisions[:REVISIONS]}
        if (meta.get('labels') or {}).get(ARGO_APP):
            out['argoApp'] = clean(meta['labels'][ARGO_APP], CAUSE_LIMIT)
        return out

    def previous_logs(self, namespace: str, pod: str, container_name: str | None, tail: int) -> str:
        ns, name = checked(NAMESPACE, namespace, 'namespace'), checked(NAME, pod, 'pod')
        box = checked(CONTAINER, container_name, 'container') if container_name else None
        lines_wanted = max(1, min(int(tail or LOG_TAIL_MAX), LOG_TAIL_MAX))
        try:
            text = self.get(f'/api/v1/namespaces/{ns}/pods/{quote(name)}/log', {'previous': 'true', 'tailLines': lines_wanted, 'container': box}, text=True)
        except KubeError as e:
            if e.code == 400 and NO_PREVIOUS in e.reason:
                raise KubeError(f"{name} has no previous logs: {f'container {box}' if box else 'its container'} never restarted, so there is no earlier run to read; "
                                'read its current logs with esql', 400) from None
            raise
        lines = clean_lines(text.splitlines()[-lines_wanted:], LOG_LINE_LIMIT)
        kept, size = [], 0
        for line in reversed(lines):
            size += len(line) + 1
            if size > LOG_CHARS:
                break
            kept.append(line)
        kept.reverse()
        cut = len(lines) - len(kept)
        return (f'[{cut} earlier lines cut to stay under {LOG_CHARS} characters]\n' if cut else '') + '\n'.join(kept)

    def nodes(self) -> list[dict]:
        body = self.get('/api/v1/nodes', {'limit': LIST_LIMIT})
        rows = [node_row(n) for n in body.get('items') or []]
        rows.sort(key=lambda r: (r.get('ready') == 'True' and not r.get('pressure') and not r.get('unschedulable'), r.get('name') or ''))
        if (body.get('metadata') or {}).get('continue'):
            rows.append({'note': f'more than {LIST_LIMIT} nodes; only the first {LIST_LIMIT} were read'})
        return rows

    def node(self, name: str, now: float) -> dict:
        node = checked(NAME, name, 'node')
        n = self.get(f'/api/v1/nodes/{quote(node)}')
        st = n.get('status') or {}
        info = st.get('nodeInfo') or {}
        out = node_row(n) | compact({'conditions': [condition(c) for c in st.get('conditions') or []],
                                     'info': compact({'kubelet': info.get('kubeletVersion'), 'os': info.get('osImage'), 'kernel': info.get('kernelVersion'),
                                                      'runtime': info.get('containerRuntimeVersion')}),
                                     'capacity': resources_of(st.get('capacity')) or None})
        pods = self.get('/api/v1/pods', {'fieldSelector': f'spec.nodeName={node}', 'limit': LIST_LIMIT})
        items = pods.get('items') or []
        spread: dict[str, int] = {}
        for p in items:
            ns = (p.get('metadata') or {}).get('namespace') or '?'
            spread[ns] = spread.get(ns, 0) + 1
        out['pods'] = compact({'count': len(items), 'byNamespace': dict(sorted(spread.items(), key=lambda kv: -kv[1])),
                               'notReady': [short_pod(p) for p in items if not settled(p)] or None,
                               'note': f'more than {LIST_LIMIT} pods on the node; only the first {LIST_LIMIT} were read' if (pods.get('metadata') or {}).get('continue') else None})
        events = self.get('/api/v1/events', {'fieldSelector': f'involvedObject.kind=Node,involvedObject.name={node}', 'limit': EVENT_LIMIT})
        start = now - NODE_EVENT_MINUTES * 60
        rows = []
        for e in events.get('items') or []:
            last = last_seen(e)
            if (when(last) or 0) >= start:
                rows.append({'last': last, 'count': e.get('count') or (e.get('series') or {}).get('count') or 1, 'type': e.get('type'), 'reason': e.get('reason'),
                             'message': clean(e.get('message'))})
        rows.sort(key=lambda r: when(r['last']) or 0, reverse=True)
        out['events'] = rows
        return out

    def daemonset(self, namespace: str, name: str) -> dict:
        ns, ds = checked(NAMESPACE, namespace, 'namespace'), checked(NAME, name, 'daemonset')
        d = self.get(f'/apis/apps/v1/namespaces/{ns}/daemonsets/{quote(ds)}')
        meta, spec, st = d.get('metadata') or {}, d.get('spec') or {}, d.get('status') or {}
        template = (spec.get('template') or {}).get('spec') or {}
        labels = (spec.get('selector') or {}).get('matchLabels') or {}
        strategy = spec.get('updateStrategy') or {}
        out = compact({'name': meta.get('name'), 'namespace': meta.get('namespace'),
                       'pods': {'desired': st.get('desiredNumberScheduled') or 0, 'current': st.get('currentNumberScheduled') or 0, 'ready': st.get('numberReady') or 0,
                                'updated': st.get('updatedNumberScheduled') or 0, 'available': st.get('numberAvailable') or 0,
                                'unavailable': st.get('numberUnavailable') or 0, 'misscheduled': st.get('numberMisscheduled') or 0},
                       'generation': {'wanted': meta.get('generation'), 'observed': st.get('observedGeneration')},
                       'updateStrategy': compact({'type': strategy.get('type'), 'maxUnavailable': (strategy.get('rollingUpdate') or {}).get('maxUnavailable')}),
                       'conditions': [condition(c) for c in st.get('conditions') or []] or None, 'images': images_of(template),
                       'nodeSelector': {k: clean(v, CAUSE_LIMIT) for k, v in (template.get('nodeSelector') or {}).items()} or None,
                       'tolerations': [toleration(t) for t in template.get('tolerations') or []] or None, 'selector': labels})
        if labels:
            selector = ','.join(f'{k}={v}' for k, v in sorted(labels.items()))
            pods = self.get(f'/api/v1/namespaces/{ns}/pods', {'labelSelector': selector, 'limit': LIST_LIMIT})
            items = [p for p in pods.get('items') or [] if any(r.get('uid') == meta.get('uid') for r in (p.get('metadata') or {}).get('ownerReferences') or [])]
            out['podsOnNodes'] = sorted((short_pod(p) for p in items), key=lambda r: (settled_row(r), r.get('node') or ''))
        return out


def settled_row(row: dict) -> bool:
    ready = str(row.get('ready') or '0/1').split('/')
    return row.get('phase') in ('Running', 'Succeeded') and ready[0] == ready[-1] and not row.get('waiting')
