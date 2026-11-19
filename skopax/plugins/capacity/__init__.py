from urllib.parse import urlencode

from skopax.contract import Capabilities, Check, Context, Field, HttpError, InstanceInfo, PassResult, Panel, Peer, Plugin, ReadError, RuleSet, Sweep, TestResult, call
from skopax.kit import access, connection
from skopax.kit.promql import PrometheusError, bounded, client_for, credential
from skopax.plugins.capacity.capacity import FS, NAME, RULES, run_pass
from skopax.plugins.capacity.pmm import PmmMetrics

ID = 'capacity'
CHECK = 'capacity'
TEST_TIMEOUT_S = 15
BUDGET_S = 200
NO_PEER = 'no Prometheus instance is set'
SECTION_HELP = {'capacity': 'rows of the capacity check: volumes, autoscalers, JVM heap and host disks, each tagged by part, plus one summary row'}
RULE_HELP = {
    'disk_warn': 'Warning when a volume or host disk is this percent full.',
    'disk_crit': 'Critical at this percent full, or when it would fill within a day.',
    'fill_days': 'Warning when a disk would be full within this many days at its current growth.',
    'fill_floor': 'The days-to-full warning is raised only when the disk is already at least this percent full.',
    'heap_warn': 'Warning when a service keeps this percent of its Java memory in use after every clean-up for 15 minutes.',
    'heap_crit': 'Critical when it keeps this percent of its Java memory in use.',
}


def _ui(peer: Peer) -> str:
    return (peer.info.config.get('ui-url') or peer.info.config.get('url') or '').rstrip('/')


def test(ctx: Context) -> TestResult:
    def ask() -> str:
        peer = ctx.peer('prometheus')
        if peer is None:
            raise ReadError(400, NO_PEER)
        client = client_for(peer.info, peer.http())
        try:
            body = call(client.transport, 'GET', f'{client.url}/api/v1/query?' + urlencode({'query': 'up'}), timeout=TEST_TIMEOUT_S).body
        except HttpError as e:
            said = access.tested(NAME, 'metrics', e.code, credential=credential(peer.info.config))
            if said:
                raise ReadError(e.code, said) from None
            raise
        if not isinstance(body, dict) or body.get('status') != 'success':
            raise ReadError(502, 'Prometheus answered an error')
        found = f'Prometheus {peer.info.name} answers'
        disks = ctx.peer('disks-pmm') if ctx.instance.config.get('host-disks') != 'false' else None
        if disks is None:
            return found
        try:
            PmmMetrics(disks.info.config, disks.http()).vec('vector(1)', ctx.now)
        except PrometheusError as e:
            said = access.tested(NAME, 'host disks', e.code, credential='token')
            raise ReadError(e.code or 502, said or e.reason) from None
        return f'{found}, host disks from PMM {disks.info.name}'

    return connection.result(ask)


def run(ctx: Context) -> Sweep:
    peer = ctx.peer('prometheus')
    if peer is None:
        return Sweep(PassResult(CHECK, False, f'error: {NO_PEER}'))
    config = ctx.instance.config
    disk_client, disk_error = None, ''
    if config.get('host-disks') != 'false' and config.get('disks-pmm'):
        try:
            found = ctx.peer('disks-pmm')
            disk_client = None if found is None else PmmMetrics(found.info.config, bounded(found.http(), BUDGET_S))
        except ReadError as e:
            disk_error = e.message
    result, rows = run_pass(client_for(peer.info, bounded(peer.http(), BUDGET_S)), ctx.now, {**RULES, **ctx.rules('rules')}, source=peer.info.name, ui=_ui(peer),
                            credential=credential(peer.info.config), jvm=config.get('jvm') != 'false', host_disks=config.get('host-disks') != 'false', volume_selector=(config.get('volume-selector') or '').strip(), fs_selector=(config.get('fs-selector') or FS).strip(),
                            disk_client=disk_client, disk_error=disk_error)
    return Sweep(result, sections={'capacity': rows} if result.ok else {})


def links(card: dict, info: InstanceInfo) -> list[dict] | None:
    if card.get('src') != 'capacity':
        return None
    return [{'label': u['label'], 'url': u['url']} for u in card.get('urls') or []
            if isinstance(u, dict) and isinstance(u.get('label'), str) and isinstance(u.get('url'), str) and u['url'].startswith('https://')]


PLUGIN = Plugin(
    id=ID, name='Capacity', version='0.1.0', category='infrastructure', description='Volume, host disk, autoscaler and JVM heap headroom read through a Prometheus instance.', icon='icon.svg',
    author='Skopax', licence='Apache-2.0', core='>=0.1,<0.3', edition='community', capabilities=Capabilities(), uses=('prometheus', 'pmm'),
    fields=(Field('prometheus', 'Prometheus', 'instance', required=True, choices=('prometheus',), help='The Prometheus instance to read.'),
            Field('jvm', 'JVM heap', 'bool', default='true', help='Read the JVM heap of Micrometer services. Switched off, its open cards close at the next check.'),
            Field('host-disks', 'Host disks', 'bool', default='true',
                  help='Read the disks node_exporter reports (node_filesystem_* series). On a Kubernetes Prometheus, node root disks are usually covered by the '
                       "cluster's own DiskPressure checks, so you may switch this off there. Switched off, its open cards close at the next check."),
            Field('disks-pmm', 'Host disks from PMM', 'instance', choices=('pmm',),
                  help='A PMM instance whose metrics data source holds the node_filesystem_* series of the hosts, for hosts that Prometheus does not scrape. Empty reads the host disks from Prometheus.'),
            Field('volume-selector', 'Volume selector', 'text', help='Extra label matchers for kubelet_volume_stats_* series, for example namespace!="scratch".'),
            Field('fs-selector', 'Host disk selector', 'text', default=FS, help='Label matchers for node_filesystem_* series; the default leaves out pseudo file systems and container mounts.')),
    test=test,
    checks=(Check(CHECK, 'Volumes, autoscalers, JVM heap and host disks', 900, 600, (CHECK,), run, sections=('capacity',), section_help=SECTION_HELP),),
    rules=(RuleSet('rules', 'Capacity', dict(RULES), RULE_HELP),),
    links=links,
    panels=(Panel('disk', 'panel.js', label='Disk', group='observe'), Panel('jvm', 'panel.js', label='JVM', group='observe'), Panel('cap', 'panel.js', label='Capacity', group='observe')),
)
