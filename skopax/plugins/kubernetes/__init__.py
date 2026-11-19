from collections.abc import Mapping
from pathlib import Path
from typing import Protocol

from skopax import kube as cluster
from skopax.agent import OPS, REQUIRED, RemoteAgent, run_kube, status_of
from skopax.contract import Capabilities, Check, Context, Field, HttpError, Knowledge, Panel, Plugin, ReadError, SourceDown, TestResult, Tool
from skopax.kit import access, connection
from skopax.kube import LOG_TAIL_MAX, OFF, Kube, KubeError
from skopax.messages import msg
from skopax.plugins.kubernetes.nodes import ID, NAME, NODES_RIGHT, PASS, PERIOD, local_nodes, run as nodes_run

SA_ROOT = cluster.SA_ROOT
LOCAL_NAME_ENV = 'SKOPAX_KUBE_LOCAL_NAME'
LOCAL_NAME = 'local'
SINCE_MINUTES = 60
TAIL = 100
SECTION_HELP = {'nodes': 'one row per Kubernetes instance from the heartbeat: status and since when, last answer, version and a summary of its cluster nodes'}
STR = {'type': 'string'}
DESCRIPTION = ('Read-only Kubernetes API of a cluster, for what Prometheus cannot show (fixed operations, no env or config). '
               'op pods: pods of a namespace (selector = label selector, optional) with phase, ready, restarts, images and last termination; '
               'op pod (name): status, conditions and each container\'s state and last state (exit code, reason, message); '
               f'op events: the namespace\'s events of the last since_minutes (default {SINCE_MINUTES}; Kubernetes keeps about 1 h), only for object when given '
               '(a deployment name also matches its ReplicaSets and pods): FailedScheduling, Unhealthy probes, BackOff, Killing; '
               'op rollout (deployment): replicas, conditions and ReplicaSet revisions with images and age; '
               f'op previous_logs (pod, container): the last tail lines (default {TAIL}, max {LOG_TAIL_MAX}) of the container\'s previous, crashed run (a container that never restarted has none). '
               'op nodes (no namespace): every node with Ready and since when, pressure (memory, disk, PID, network), cordoned, taints, roles and kubelet version, problem nodes first; '
               'op node (name, no namespace): its conditions with reasons, capacity, the pods on it by namespace with the ones not ready, and its events of the last hour (NodeNotReady, eviction, OOM); '
               'op daemonset (name): desired, current, ready, updated, unavailable and misscheduled pods, conditions, node selector and tolerations, and each pod with its node. '
               'cluster: the instance to read (default: the primary instance); a failed call says so, then fall back to a metrics query, when a metrics tool is offered, for what it counts. Times are UTC.')
SCHEMA = {'type': 'object',
          'properties': {'op': {'type': 'string', 'enum': list(OPS)}, 'namespace': STR, 'selector': STR, 'name': STR, 'object': STR,
                         'since_minutes': {'type': 'integer', 'minimum': 1, 'maximum': 1440}, 'deployment': STR, 'pod': STR, 'container': STR,
                         'tail': {'type': 'integer', 'minimum': 1, 'maximum': LOG_TAIL_MAX}},
          'required': ['op'], 'additionalProperties': False}


class HostContext(Context, Protocol):
    def agent(self) -> RemoteAgent: ...

    def local_kube(self) -> Kube: ...


def env_instances(env: Mapping[str, str]) -> list[dict]:
    try:
        inside = (SA_ROOT / 'token').is_file()
    except OSError:
        inside = False
    if not inside:
        return []
    return [{'name': (env.get(LOCAL_NAME_ENV) or '').strip() or LOCAL_NAME, 'primary': 'true', 'mode': 'in-cluster'}]


def in_cluster(ctx: Context) -> bool:
    return (ctx.instance.config.get('mode') or 'agent') == 'in-cluster'


def kube(ctx: HostContext, args: dict) -> object:
    op = args.get('op')
    if op not in OPS:
        raise ReadError(400, f"op must be one of {', '.join(OPS)}")
    wanted = {k: args[k] for k in OPS[op] if args.get(k) not in (None, '')}
    for key in REQUIRED[op]:
        if key not in wanted:
            raise ReadError(400, f'{key} is required')
    if 'tail' in wanted:
        wanted['tail'] = min(int(wanted['tail']), LOG_TAIL_MAX)
    try:
        if not in_cluster(ctx):
            return ctx.agent().kube(op, **wanted)
        client = ctx.local_kube()
        if not client.enabled:
            raise ReadError(503, OFF)
        return run_kube(client, op, wanted, ctx.now)
    except KubeError as e:
        raise ReadError(status_of(e.code), e.message) from None


def nodes_of(client: Kube) -> int:
    return len(local_nodes(client))


def test(ctx: HostContext) -> TestResult:
    def ask() -> str:
        if in_cluster(ctx):
            try:
                return msg('kubernetes.test.cluster', nodes=nodes_of(ctx.local_kube()))
            except HttpError as e:
                if e.code == access.FORBIDDEN:
                    raise ReadError(e.code, access.text(NAME, f'nodes of {ctx.instance.name}', NODES_RIGHT)) from None
                raise
        answer = ctx.agent().info()
        release = str(answer.get('release') or '')[:20]
        found = answer.get('cluster')
        if not isinstance(found, dict):
            raise SourceDown(msg('kubernetes.test.agent_cluster', release=release))
        return msg('kubernetes.test.agent', release=release, nodes=found.get('total', 0))

    return connection.result(ask)


PLUGIN = Plugin(
    id=ID, name='Kubernetes', version='0.1.0', category='infrastructure', description='Read-only access to the Kubernetes API of this cluster, or of another cluster through a remote agent.', icon='icon.svg',
    author='Skopax', licence='Apache-2.0', core='>=0.1,<0.3', edition='community', capabilities=Capabilities(hosts=('@url', 'kubernetes.default.svc'), secrets=('token',)),
    fields=(Field('mode', 'Mode', 'choice', required=True, default='agent', choices=('in-cluster', 'agent'), choice_labels={'in-cluster': 'This cluster', 'agent': 'Through an agent'}, help='Read the cluster this service runs in, or a cluster through a remote agent.'),
            Field('url', 'URL', 'url', required=True, show_if=('mode', 'agent'), help='Address of the remote agent.'),
            Field('token', 'Token', 'secret', required=True, show_if=('mode', 'agent'), help='Bearer token the remote agent expects.'),
            Field('via', 'Relay', 'text', show_if=('mode', 'agent'), group='advanced', help='Name of another agent instance that relays the calls to this agent.')),
    env_instances=env_instances, test=test,
    checks=(Check(PASS, 'Node and agent heartbeat', PERIOD, 165, (PASS,), nodes_run, sections=(PASS,), section_help=SECTION_HELP, once=True),),
    tools=(Tool('kube', DESCRIPTION, SCHEMA, kube, instance_input='cluster'),),
    panels=(Panel(PASS, 'panel.js', label='Nodes', group='observe'),),
    knowledge=(Knowledge('kubernetes', 'Kubernetes', (Path(__file__).parent / 'kubernetes.md').read_text(encoding='utf-8'), slack=True),),
)
