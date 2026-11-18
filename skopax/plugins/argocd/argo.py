import json
import re
from time import monotonic
from typing import NamedTuple, Protocol
from urllib.parse import quote

from skopax import clock
from skopax.contract import HttpError, ReadError, Transport, Unreachable, anomaly, call, iso
from skopax.errkind import NoAccess
from skopax.kit.text import duration
from skopax.kube import NAME, NAMESPACE, TIMEOUT, Kube, KubeError, checked, clean, compact, when
from skopax.messages import msg

API = '/api/v1/applications'
NAMES = '?fields=items.metadata.name'
CAN_I = '/api/v1/account/can-i/applications/get/*/*'
KUBE_API = '/apis/argoproj.io/v1alpha1/namespaces/{namespace}/applications'
LISTED = 'Argo CD applications'
PAGE = 50
PAGES = 20
PASS_PAGES = 200
PASS_BUDGET_S = 180
KUBE_TIMEOUT_S = TIMEOUT
API_TIMEOUT_S = 30
HISTORY = 10
RESOURCE_LIMIT = 50
REVISION_LIMIT = 100
SERVER_LIMIT = 200
OFF = 'Argo CD in-cluster mode is off: this service is not running in a cluster (no service account token)'
FILTER = re.compile(r'[a-z0-9.-]{1,253}')
PERSIST_S = 900
WARN_HEALTH = ('Degraded', 'Missing', 'Progressing')
CALM_HEALTH = ('Healthy', 'Suspended')
FAILED = ('Failed', 'Error')
OP_LIMIT = 300
RESOURCE_MESSAGE_LIMIT = 120
CARD_RESOURCES = 3
PART = 'applications'
DENIED = (401, 403)
API_PERMISSION = 'the RBAC permission applications, get'


def kube_permission(namespace: str) -> str:
    return f'get and list on applications.argoproj.io in the namespace {namespace}'


class ArgoError(ReadError):
    def __init__(self, status: int, reason: str, message: str | None = None):
        super().__init__(status, message or reason)
        self.reason = reason


class ArgoDenied(ArgoError, NoAccess):
    def __init__(self, status: int, credential: str, permission: str, text: str | None = None):
        said = text or msg('argocd.denied_invalid' if status == 401 else 'argocd.denied', part=PART, status=status, credential=credential, permission=permission)
        super().__init__(status, said)
        self.part = PART
        self.credential = credential
        self.permission = permission

    @property
    def label(self) -> str:
        return msg('argocd.no_access_invalid' if self.status == 401 else 'argocd.no_access', part=self.part, credential=self.credential, permission=self.permission)


def not_granted() -> ArgoDenied:
    return ArgoDenied(403, 'token', API_PERMISSION, msg('argocd.no_access', part=PART, credential='token', permission=API_PERMISSION))


def verdict(body: object) -> bool | None:
    value = body.get('value') if isinstance(body, dict) else None
    return True if value == 'yes' else False if value == 'no' else None


class Listing(NamedTuple):
    items: list[dict]
    more: bool
    left: int | None


class Source(Protocol):
    def list(self, pages: int = PAGES, budget: float | None = None) -> Listing: ...

    def granted(self, timeout: float = API_TIMEOUT_S) -> bool | None: ...

    def get(self, name: str) -> dict: ...


def local_kube() -> Kube:
    return Kube()


def status_of(code: object) -> int:
    return code if isinstance(code, int) and not isinstance(code, bool) and 400 <= code <= 599 else 502


def server_message(body: str, token: str) -> str:
    try:
        parsed = json.loads(body or '')
    except ValueError:
        return ''
    said = next((parsed[k] for k in ('message', 'error') if isinstance(parsed, dict) and isinstance(parsed.get(k), str) and parsed[k].strip()), '')
    if token:
        said = said.replace(token, '***')
    return ' '.join(said.split())[:SERVER_LIMIT]


def slim(app: dict) -> dict:
    meta, spec, st = app.get('metadata') or {}, app.get('spec') or {}, app.get('status') or {}
    op, health = st.get('operationState') or {}, st.get('health') or {}
    resources = [{k: r.get(k) for k in ('kind', 'name', 'namespace', 'status', 'health') if k in r} for r in st.get('resources') or [] if isinstance(r, dict) and resource_problem(r)]
    return {'metadata': {'name': meta.get('name')},
            'spec': {'project': spec.get('project'), 'destination': {'namespace': (spec.get('destination') or {}).get('namespace')}, 'syncPolicy': spec.get('syncPolicy')},
            'status': {'sync': {'status': (st.get('sync') or {}).get('status')}, 'health': {k: health.get(k) for k in ('status', 'message') if k in health},
                       'operationState': {k: op.get(k) for k in ('phase', 'message', 'startedAt', 'finishedAt') if k in op}, 'resources': resources}}


def items_of(body: object, what: str) -> list[dict]:
    if not isinstance(body, dict):
        raise ArgoError(502, f'{what} answered without an object')
    found = body.get('items')
    if found is None:
        return []
    if not isinstance(found, list):
        raise ArgoError(502, f'{what} answered without an items list')
    return [a for a in found if isinstance(a, dict)]


class ApiSource:
    def __init__(self, url: str, token: str, transport: Transport):
        self.url = url.rstrip('/')
        self.token = token
        self.transport = transport

    def _get(self, path: str, what: str, name: str | None = None, timeout: float = API_TIMEOUT_S) -> object:
        headers = {'Authorization': f'Bearer {self.token}'} if self.token else {}
        try:
            return call(self.transport, 'GET', self.url + path, headers, timeout=timeout).body
        except HttpError as e:
            if e.code in DENIED and name is not None:
                raise ArgoError(e.code, msg('argocd.no_app_invalid' if e.code == 401 else 'argocd.no_app', name=name)) from None
            if e.code in DENIED:
                raise ArgoDenied(e.code, 'token', API_PERMISSION) from None
            reason = f'HTTP {e.code} on {what}'
            said = server_message(e.body, self.token)
            raise ArgoError(status_of(e.code), reason, f'{reason}: {said}' if said else reason) from None
        except Unreachable:
            raise ArgoError(502, f'{what} unreachable') from None

    def list(self, pages: int = PAGES, budget: float | None = None) -> Listing:
        return Listing([slim(a) for a in items_of(self._get(API, LISTED), LISTED)], False, 0)

    def granted(self, timeout: float = API_TIMEOUT_S) -> bool | None:
        try:
            return verdict(self._get(CAN_I, 'Argo CD permissions', timeout=timeout))
        except ArgoError:
            return None

    def get(self, name: str) -> dict:
        what = f'Argo CD application {name}'
        body = self._get(f'{API}/{quote(name)}', what, name)
        if not isinstance(body, dict):
            raise ArgoError(502, f'{what} answered without an object')
        return body


class KubeSource:
    def __init__(self, kube: Kube, namespace: str):
        self.kube = kube
        self.namespace = namespace

    def _path(self) -> str:
        try:
            return KUBE_API.format(namespace=checked(NAMESPACE, self.namespace, 'namespace'))
        except KubeError as e:
            raise ArgoError(400, e.message) from None

    def _get(self, path: str, params: dict | None, what: str) -> object:
        if not self.kube.enabled:
            raise ArgoError(503, OFF)
        try:
            return self.kube.get(path, params)
        except KubeError as e:
            if e.code in DENIED:
                raise ArgoDenied(e.code, 'service account', kube_permission(self.namespace)) from None
            if e.code:
                raise ArgoError(status_of(e.code), f'HTTP {e.code} on {what}', e.message) from None
            raise ArgoError(502, e.message) from None

    def list(self, pages: int = PAGES, budget: float | None = None) -> Listing:
        path = self._path()
        deadline = None if budget is None else monotonic() + budget
        read, token, left = [], None, None
        for _ in range(pages):
            if token and deadline is not None and deadline - monotonic() < KUBE_TIMEOUT_S:
                break
            body = self._get(path, {'limit': PAGE, 'continue': token}, LISTED)
            read += [slim(a) for a in items_of(body, LISTED)]
            meta = body.get('metadata') or {}
            token = meta.get('continue')
            count = meta.get('remainingItemCount')
            left = count if isinstance(count, int) and not isinstance(count, bool) and count >= 0 else None
            if not token:
                break
        return Listing(read, bool(token), left if token else 0)

    def granted(self, timeout: float = API_TIMEOUT_S) -> bool | None:
        return True

    def get(self, name: str) -> dict:
        what = f'Argo CD application {name}'
        body = self._get(f'{self._path()}/{quote(name)}', None, what)
        if not isinstance(body, dict):
            raise ArgoError(502, f'{what} answered without an object')
        return body


def ui_link(url: str, namespace: str, name: str) -> str:
    return f"{url.rstrip('/')}/applications/{quote(namespace)}/{quote(name)}"


def revision_of(d: dict):
    if d.get('revisions'):
        return [str(r)[:REVISION_LIMIT] for r in d['revisions']]
    return str(d['revision'])[:REVISION_LIMIT] if d.get('revision') else None


def problem(app: dict) -> bool:
    st = app.get('status') or {}
    return (st.get('sync') or {}).get('status') != 'Synced' or (st.get('health') or {}).get('status') != 'Healthy'


def resource_problem(r: dict) -> bool:
    health = (r.get('health') or {}).get('status')
    return (r.get('status') is not None and r['status'] != 'Synced') or (health is not None and health != 'Healthy')


def app_row(app: dict, url: str, namespace: str) -> dict:
    meta, spec, st = app.get('metadata') or {}, app.get('spec') or {}, app.get('status') or {}
    return compact({'name': meta.get('name'), 'namespace': (spec.get('destination') or {}).get('namespace'), 'sync': (st.get('sync') or {}).get('status'),
                    'health': (st.get('health') or {}).get('status'), 'operation': (st.get('operationState') or {}).get('phase'),
                    'link': ui_link(url, namespace, meta.get('name') or '')})


def source_row(s: dict) -> dict:
    return compact({'repo': clean(s.get('repoURL'), 300), 'path': s.get('path'), 'chart': s.get('chart'), 'targetRevision': s.get('targetRevision')})


def resource_row(r: dict) -> dict:
    health = r.get('health') or {}
    return compact({'kind': r.get('kind'), 'namespace': r.get('namespace'), 'name': r.get('name'), 'status': r.get('status'), 'health': health.get('status'),
                    'message': clean(health.get('message'))})


def projects_of(text: str | None) -> tuple[str, ...]:
    return tuple(p for p in (x.strip() for x in str(text or '').split(',')) if p)


class Argo:
    def __init__(self, source: Source, url: str, namespace: str, projects: tuple[str, ...] = ()):
        self.source = source
        self.url = url
        self.namespace = namespace
        self.projects = projects

    def read(self, pages: int = PAGES, budget: float | None = None) -> Listing:
        return self.source.list(pages, budget)

    def granted(self) -> bool | None:
        return self.source.granted()

    def watched(self, items: list[dict]) -> list[dict]:
        return [a for a in items if not self.projects or (a.get('spec') or {}).get('project') in self.projects]

    def apps(self, problem_only: bool = True, name: str | None = None) -> list[dict]:
        part = str(name).lower() if name else None
        if part and not FILTER.fullmatch(part):
            raise ArgoError(400, 'name takes part of an Argo CD application name, like checkout-service')
        raw, more, _ = self.read()
        if not raw:
            granted = self.granted()
            if granted is False:
                raise not_granted()
            return [{'note': 'Argo CD listed no applications' + ('' if granted else f"; {msg('argocd.blind', permission=API_PERMISSION)}")}]
        read = self.watched(raw)
        if not read:
            return [{'note': f'no application is in the watched projects ({len(raw)} read)' + (f'; Argo CD holds more than the {len(raw)} read' if more else '')}]
        matched = [a for a in read if not part or part in str((a.get('metadata') or {}).get('name') or '')]
        bad = [a for a in matched if problem(a)]
        rows = sorted((app_row(a, self.url, self.namespace) for a in bad), key=lambda r: r.get('name') or '')
        if not problem_only:
            rows += sorted((app_row(a, self.url, self.namespace) for a in matched if not problem(a)), key=lambda r: r.get('name') or '')
        scope = f" with '{part}' in the name" if part else ''
        tail = f' ({len(read)} read)' if part else ''
        if part and not matched:
            note = f"no application has '{part}' in its name ({len(read)} read)"
        elif bad:
            note = f'{len(bad)} of {len(matched)} applications{scope} are not Synced or not Healthy{tail}'
        else:
            note = f'all {len(matched)} applications{scope} are Synced and Healthy{tail}'
        if more:
            note += f'; Argo CD holds more than the {len(raw)} read'
        return rows + [{'note': note}]

    def app(self, name: str) -> dict:
        try:
            app_name = checked(NAME, name, 'application')
        except KubeError as e:
            raise ArgoError(400, e.message) from None
        a = self.source.get(app_name)
        if not self.watched([a]):
            raise ArgoError(404, f'Argo CD application {app_name} is not in the watched projects')
        meta, spec, st = a.get('metadata') or {}, a.get('spec') or {}, a.get('status') or {}
        sync, health, op = st.get('sync') or {}, st.get('health') or {}, st.get('operationState') or {}
        sources = spec.get('sources') or ([spec['source']] if spec.get('source') else [])
        history = sorted(st.get('history') or [], key=lambda h: h.get('id') or 0, reverse=True)[:HISTORY]
        resources = st.get('resources') or []
        bad = [resource_row(r) for r in resources if resource_problem(r)]
        out = compact({
            'name': meta.get('name'), 'project': spec.get('project'), 'namespace': (spec.get('destination') or {}).get('namespace'),
            'link': ui_link(self.url, self.namespace, meta.get('name') or app_name),
            'sync': compact({'status': sync.get('status'), 'revision': revision_of(sync)}),
            'health': compact({'status': health.get('status'), 'message': clean(health.get('message'))}),
            'operation': compact({'phase': op.get('phase'), 'message': clean(op.get('message')), 'startedAt': op.get('startedAt'), 'finishedAt': op.get('finishedAt'),
                                  'revision': revision_of(op.get('syncResult') or {}) or revision_of(((op.get('operation') or {}).get('sync')) or {}),
                                  'retries': op.get('retryCount')}) or None,
            'images': [str(i) for i in (st.get('summary') or {}).get('images') or []],
            'source': [source_row(s) for s in sources],
            'history': [compact({'id': h.get('id'), 'revision': revision_of(h), 'deployedAt': h.get('deployedAt')}) for h in history],
            'conditions': [compact({'type': c.get('type'), 'message': clean(c.get('message')), 'at': c.get('lastTransitionTime')}) for c in st.get('conditions') or []],
            'resourceCount': len(resources), 'resources': bad[:RESOURCE_LIMIT], 'reconciledAt': st.get('reconciledAt'),
        })
        if len(bad) > RESOURCE_LIMIT:
            out['resourcesNote'] = f'{len(bad)} resources are not Synced or not Healthy; the first {RESOURCE_LIMIT} are shown'
        return out


def service_of(app: str, ns: str | None, suffix: str = '') -> str:
    name = app[len(ns) + 1:] if ns and app.startswith(ns + '-') else app
    return name[:-len(suffix)] if suffix and name.endswith(suffix) and len(name) > len(suffix) else name


def at(t: float) -> str:
    return f'{clock.hm(t)} {clock.label(t)}'


def card_text(app: dict, name: str, sync: str | None, health: str | None, failed: bool, since: float, now: float, url: str, namespace: str) -> str:
    spec, st = app.get('spec') or {}, app.get('status') or {}
    op = st.get('operationState') or {}
    automated = (spec.get('syncPolicy') or {}).get('automated')
    auto = isinstance(automated, dict) and automated.get('enabled') is not False
    parts = [f'Argo CD app {name}: not Synced/Healthy since {at(since)} ({duration(now - since)})',
             f'sync {sync}' + ((' (auto-sync on)' if auto else ' (auto-sync off: the change waits for a manual Sync)') if sync == 'OutOfSync' else '')]
    message = clean((st.get('health') or {}).get('message'), OP_LIMIT)
    parts.append(f'health {health}' + (f': {message}' if message else ''))
    if op.get('phase'):
        finished = when(op.get('finishedAt') or op.get('startedAt'))
        text = clean(op.get('message'), OP_LIMIT) if op.get('phase') != 'Succeeded' or failed else None
        parts.append(f"last sync {op['phase']}" + (f' at {at(finished)}' if finished is not None else '') + (f': {text}' if text else ''))
    bad = [r for r in st.get('resources') or [] if resource_problem(r)]
    if bad:
        shown = []
        for r in bad[:CARD_RESOURCES]:
            h = r.get('health') or {}
            said = clean(h.get('message'), RESOURCE_MESSAGE_LIMIT)
            shown.append(f"{r.get('kind')}/{r.get('name')} {'/'.join(x for x in (r.get('status'), h.get('status')) if x)}" + (f' ({said})' if said else ''))
        parts.append('bad resources: ' + ', '.join(shown) + (f', +{len(bad) - CARD_RESOURCES} more' if len(bad) > CARD_RESOURCES else ''))
    return '; '.join(parts) + f'. {ui_link(url, namespace, name)}'


def evaluate(items: list[dict], prev: list[dict], now: float, url: str, namespace: str, suffix: str = '') -> tuple[list[dict], list[dict]]:
    before = {r.get('app'): r for r in prev or []}
    found, rows = [], []
    for a in sorted(items, key=lambda x: (x.get('metadata') or {}).get('name') or ''):
        name = (a.get('metadata') or {}).get('name')
        st = a.get('status') or {}
        sync, health = (st.get('sync') or {}).get('status'), (st.get('health') or {}).get('status')
        phase = (st.get('operationState') or {}).get('phase')
        if not name or (sync == 'Synced' and health in CALM_HEALTH):
            continue
        ns = ((a.get('spec') or {}).get('destination') or {}).get('namespace')
        svc = service_of(name, ns, suffix)
        old = before.get(name)
        kept = when(old.get('since')) if old else None
        since = now if kept is None else kept
        rows.append({'app': name, 'ns': ns, 'svc': svc, 'sync': sync, 'health': health, 'phase': phase, 'since': iso(since)})
        failed = phase in FAILED and sync != 'Synced'
        if not failed and now - since < PERSIST_S:
            continue
        sev = 'warn' if failed or health in WARN_HEALTH else 'info'
        label = 'Argo CD ' + ' \u00b7 '.join(x for x in ('sync failed' if failed else sync if sync != 'Synced' else None, health if health not in CALM_HEALTH else None) if x)
        x = anomaly('argo', 'argo', ns or '-', svc, '', sev, now - since, None, None, 'dur', label, card_text(a, name, sync, health, failed, since, now, url, namespace), 86400,
                    f'{sev.upper()} {svc} {label} for {duration(now - since)}')
        x['app'] = name
        x['ranKey'] = f'argo:{name}'
        found.append(x)
    return found, rows


def name_of(app: dict) -> str:
    name = (app.get('metadata') or {}).get('name')
    return name if isinstance(name, str) else ''


def links_for(card: dict, url: str, namespace: str) -> list[dict] | None:
    if card.get('src') != 'argo':
        return None
    return [{'label': 'Argo CD app', 'url': ui_link(url, namespace, card['app'])}] if card.get('app') else []
