from pathlib import Path
from time import monotonic

from skopax.contract import (Capabilities, Check, Context, Field, HttpError, InstanceInfo, Knowledge, PassResult, Plugin, ReadError, SourceDown, Sweep, TestResult, Tool,
                             Unreachable, anomaly, call)
from skopax.kit import connection
from skopax.kube import NAMESPACE, Kube, KubeError
from skopax.messages import msg
from skopax.plugins.argocd import argo
from skopax.plugins.argocd.argo import (API, API_PERMISSION, KUBE_API, LISTED, NAMES, OFF, PAGE, PART, ApiSource, Argo, ArgoDenied, ArgoError, KubeSource, evaluate, items_of,
                                        kube_permission, links_for, name_of, not_granted, projects_of)

ID = 'argocd'
CHECK = 'argo'
OPS = ('apps', 'app')
DEFAULT_NAMESPACE = 'argocd'
TEST_BUDGET_S = 12
NO_DATA = 'ok \u00b7 no data'
ACCESS = f'{CHECK}:access:{PART}'
STR = {'type': 'string'}
SECTION_HELP = {'argo': 'Argo CD applications that are not Synced or not Healthy: sync, health, the last operation phase and since when'}
DESCRIPTION = ('Read-only Argo CD (Application objects only; no helm values, parameters or other config). '
               'op apps: the applications that are not Synced or not Healthy with sync, health and the last operation phase '
               '(problem_only false lists every application, problems first; name keeps the ones whose name contains it); '
               'an instance with a Projects list shows only the applications of those projects. '
               'op app (name): sync status and revision, health and its message, the last sync operation (phase, message, start, finish, revision), '
               'the last 10 deploys (id, revision, deployedAt), the current images, conditions and only the resources that are not Synced or not Healthy. '
               'Every answer carries the Argo CD UI link. A deployment\'s label argocd.argoproj.io/instance holds its application name '
               '(when the `kube` tool is offered, its op rollout shows it as argoApp), else apps with name=<service> finds it. '
               'instance: the Argo CD instance to read (default: the primary instance).')
SCHEMA = {'type': 'object', 'properties': {'op': {'type': 'string', 'enum': list(OPS)}, 'name': STR, 'problem_only': {'type': 'boolean'}},
          'required': ['op'], 'additionalProperties': False}


def in_cluster(info: InstanceInfo) -> bool:
    return (info.config.get('mode') or 'api') == 'in-cluster'


def namespace_of(info: InstanceInfo) -> str:
    return (info.config.get('namespace') or '').strip() or DEFAULT_NAMESPACE


def argo_of(ctx: Context) -> Argo:
    info = ctx.instance
    url = info.config.get('url') or ''
    namespace = namespace_of(info)
    source = KubeSource(argo.local_kube(), namespace) if in_cluster(info) else ApiSource(url, info.config.get('token') or '', ctx.http())
    return Argo(source, url, namespace, projects_of(info.config.get('projects')))


def left(deadline: float) -> float:
    remaining = deadline - monotonic()
    if remaining <= 0:
        raise TimeoutError()
    return remaining


def capped(kube: Kube, deadline: float) -> Kube:
    inner = kube.transport

    def send(url: str, headers: dict[str, str], timeout: float) -> bytes:
        return inner(url, headers, min(timeout, left(deadline)))

    kube.transport = send
    return kube


def first_page(namespace: str, deadline: float) -> str:
    kube = capped(argo.local_kube(), deadline)
    if not kube.enabled:
        raise SourceDown(OFF)
    if not NAMESPACE.fullmatch(namespace):
        raise SourceDown(f'namespace {namespace[:80]!r} is not a Kubernetes name')
    try:
        body = kube.get(KUBE_API.format(namespace=namespace), {'limit': PAGE})
    except KubeError as e:
        if e.code == 403:
            raise ArgoDenied(403, 'service account', kube_permission(namespace)) from None
        if e.code:
            raise HttpError(e.code, ID) from None
        raise Unreachable() from None
    body = body if isinstance(body, dict) else {}
    count = len(body.get('items') or [])
    more = '+' if (body.get('metadata') or {}).get('continue') else ''
    return f'Argo CD in this cluster: {count}{more} applications in namespace {namespace}'


def test(ctx: Context) -> TestResult:
    info = ctx.instance

    def ask() -> str:
        deadline = monotonic() + TEST_BUDGET_S
        if in_cluster(info):
            return first_page(namespace_of(info), deadline)
        token = info.config.get('token') or ''
        headers = {'Authorization': f'Bearer {token}'} if token else {}
        base, transport = (info.config.get('url') or '').rstrip('/'), ctx.http()
        body = call(transport, 'GET', base + '/api/version', headers, timeout=left(deadline)).body
        version = body.get('Version') if isinstance(body, dict) else None
        try:
            count = len(items_of(call(transport, 'GET', base + API + NAMES, headers, timeout=left(deadline)).body, LISTED))
        except HttpError as e:
            if e.code == 403:
                raise ArgoDenied(403, 'token', API_PERMISSION) from None
            raise
        head = f"Argo CD {version[:40] if isinstance(version, str) and version else 'unknown version'}: {count} applications"
        if count:
            return head
        granted = ApiSource(base, token, transport).granted(timeout=left(deadline))
        if granted is False:
            raise not_granted()
        return head if granted else f"{head}; {msg('argocd.blind', permission=API_PERMISSION)}"

    return connection.result(ask)


def access_card(e: ArgoDenied) -> dict:
    card = anomaly(CHECK, 'no-access', '-', CHECK, e.part, 'warn', 1, None, None, '', e.label, e.label, 1, f'WARN {e.label}')
    card['ranKey'] = ACCESS
    return card


def refused(e: ArgoDenied) -> Sweep:
    return Sweep(PassResult(CHECK, True, f'ok \u00b7 no access to {e.part}', [access_card(e)], {}, (ACCESS,)))


def run(ctx: Context) -> Sweep:
    info = ctx.instance
    try:
        reader = argo_of(ctx)
        raw, more, unread = reader.read(argo.PASS_PAGES, argo.PASS_BUDGET_S)
    except ArgoDenied as e:
        return refused(e)
    except ArgoError as e:
        return Sweep(PassResult(CHECK, False, f'error: {PART}: {e.reason}'))
    if not raw:
        granted = reader.granted()
        if granted is False:
            return refused(not_granted())
        return Sweep(PassResult(CHECK, True, NO_DATA, [], {}, (f'{CHECK}:no-data',) + ((ACCESS,) if granted else ())))
    seen = {n for n in (name_of(a) for a in raw) if n}
    names = sorted(seen)
    prev = [r for r in ctx.previous(CHECK) or [] if isinstance(r, dict)]
    found, rows = evaluate(reader.watched(raw), prev, ctx.now, reader.url, reader.namespace, (info.config.get('app-suffix') or '').strip())
    read_keys = tuple(f'{CHECK}:{n}' for n in names)
    if more:
        kept = [{k: v for k, v in r.items() if k not in ('plugin', 'cluster')} for r in prev if r.get('app') not in seen]
        status = f'ok \u00b7 read {len(raw)} of {len(raw) + unread} applications' if unread is not None else f'ok \u00b7 read {len(raw)} applications, more not read'
        return Sweep(PassResult(CHECK, True, status, found, {}, (*(read_keys or (f'{CHECK}:no-data',)), ACCESS)), sections={CHECK: rows + kept})
    opened = sorted({c.get('app') for c in ctx.open_cards() if isinstance(c.get('app'), str) and c.get('app')} - seen)
    if opened and reader.granted() is not True:
        opened = []
    return Sweep(PassResult(CHECK, True, 'ok', found, {}, (CHECK, *read_keys, *(f'{CHECK}:{a}' for a in opened), ACCESS)), sections={CHECK: rows})


def argo_tool(ctx: Context, args: dict) -> object:
    op = args.get('op')
    if op not in OPS:
        raise ReadError(400, f"op must be one of {', '.join(OPS)}")
    if op == 'app' and not args.get('name'):
        raise ReadError(400, 'name is required')
    reader = argo_of(ctx)
    if op == 'apps':
        return reader.apps(args.get('problem_only') is not False, args.get('name') or None)
    return reader.app(args['name'])


def links(card: dict, info: InstanceInfo) -> list[dict] | None:
    return links_for(card, info.config.get('url') or '', namespace_of(info))


def probe_targets(info: InstanceInfo) -> list[dict]:
    return [{'name': f'argocd-{info.name}', 'url': (info.config.get('url') or '').rstrip('/') + '/healthz'}]


PLUGIN = Plugin(
    id=ID, name='Argo CD', version='0.1.0', category='infrastructure', description='Argo CD applications that stay out of sync or unhealthy, read from the Argo CD API or the cluster.',
    icon='icon.svg', author='Skopax', licence='Apache-2.0', core='>=0.1,<0.3', edition='community',
    capabilities=Capabilities(hosts=('@url', 'kubernetes.default.svc'), secrets=('token',)),
    fields=(Field('mode', 'Mode', 'choice', required=True, default='api', choices=('api', 'in-cluster'), choice_labels={'api': 'Argo CD API', 'in-cluster': 'This cluster'},
                  help='Read through the Argo CD API with a token, or read the Application objects of the cluster this service runs in.'),
            Field('url', 'URL', 'url', required=True, ui=True, help='Address of the Argo CD server: its API in api mode, and the links people open in both modes.'),
            Field('token', 'Token', 'secret', show_if=('mode', 'api'), help='Argo CD API token of an account that can read applications.'),
            Field('ca', 'CA certificate', 'pem', group='advanced', help='PEM certificate to trust instead of the system roots.'),
            Field('ca-pinned', 'Trust this certificate as it is', 'bool', default='false', show_if=('ca', '*'), group='advanced', help="For a server whose own certificate fails the usual checks: paste the server's own certificate; with the switch on, only that exact certificate is accepted, even if it is expired, self-signed or names another host. A CA certificate does not work with the switch on."),
            Field('namespace', 'Namespace', 'text', default=DEFAULT_NAMESPACE,
                  help='Namespace of the Application objects (in-cluster mode) and of the application links.'),
            Field('projects', 'Projects', 'text', help='Comma list of Argo CD projects to watch. Empty means all projects.'),
            Field('app-suffix', 'Application suffix', 'text',
                  help='A suffix the application names carry after the service name, such as -live; it is left out of the service name on cards. Empty means none.')),
    test=test,
    checks=(Check(CHECK, 'Argo CD applications', 300, 105, (CHECK,), run, tool='argo', sections=(CHECK,), section_help=SECTION_HELP),),
    tools=(Tool('argo', DESCRIPTION, SCHEMA, argo_tool),),
    links=links, probe_targets=probe_targets,
    knowledge=(Knowledge('argocd', 'Argo CD', (Path(__file__).parent / 'argocd.md').read_text(encoding='utf-8'), slack=True),),
)
