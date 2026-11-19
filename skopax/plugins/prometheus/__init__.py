from collections.abc import Mapping
from pathlib import Path

from skopax.contract import Capabilities, Check, Context, Field, HttpError, InstanceInfo, Knowledge, PassResult, Panel, Plugin, ReadError, RuleSet, Sweep, TestResult, Tool, call
from skopax.kit import access, connection
from skopax.kit.promql import CHECK, ID, PrometheusError, auth_headers, client_for, credential, ranged, with_headers
from skopax.plugins.prometheus.engine import FLOORS, PROFILES, deploy_docs, evaluate_prom, links_for, profiles_of, prom_pass
from skopax.plugins.prometheus.links import row_links as signal_row_links

RAN = ('prom', 'kube', 'alert')
NAME = 'Prometheus'
PART = 'metrics'
SECTION_HELP = {'services': 'per-service rows of the Prometheus pass', 'routes': 'top gateway routes of the Prometheus pass',
                'deploys': 'image changes the Prometheus pass saw: service, old and new image, when, and whether it is still warming up'}
TEST_TIMEOUT_S = 15
RATE_HELP = {'err': 'ERROR log lines in 60 minutes above the baseline', 'warn': 'WARN log lines in 60 minutes above the baseline', '5xx': '5xx responses in 60 minutes above the baseline',
             'out-err': 'failed outbound calls in 60 minutes above the baseline', 'gw-err': 'gateway errors in 60 minutes above the baseline', 'ing-5xx': 'ingress 5xx in 60 minutes above the baseline',
             'hikari-tmo': 'database pool timeouts in 60 minutes above the baseline', 'kafka-lag': 'consumer lag that stayed above the baseline for 10 minutes'}
PROMQL = ('PromQL on Prometheus. Instant query at `at` (unix seconds, default now); give range_minutes for a range query with about 200 points.')
PROMQL_SCHEMA = {'type': 'object', 'properties': {'expr': {'type': 'string'}, 'at': {'type': 'number'}, 'range_minutes': {'type': 'integer', 'minimum': 1, 'maximum': 10080}},
                 'required': ['expr'], 'additionalProperties': False}


def env_instances(env: Mapping[str, str]) -> list[dict]:
    url = (env.get('PROMETHEUS_URL') or '').strip()
    if not url:
        return []
    return [{'name': 'main', 'primary': 'true', 'url': url, 'ui-url': (env.get('PROMETHEUS_UI_URL') or '').strip(), 'namespaces': (env.get('PROMETHEUS_NAMESPACES') or '').strip() or '.*'}]


def test(ctx: Context) -> TestResult:
    info = ctx.instance

    def ask() -> str:
        _, bad = profiles_of(info.config.get('profiles'))
        if bad:
            raise ReadError(400, bad)
        extra = auth_headers(info.config)
        transport = with_headers(ctx.http(), extra) if extra else ctx.http()
        try:
            body = call(transport, 'GET', info.config['url'].rstrip('/') + '/api/v1/status/buildinfo', timeout=TEST_TIMEOUT_S).body
        except HttpError as e:
            said = access.tested(NAME, PART, e.code, credential=credential(info.config))
            if said:
                raise ReadError(e.code, said) from None
            raise
        data = body.get('data') if isinstance(body, dict) else None
        version = data.get('version') if isinstance(data, dict) else None
        return f"Prometheus {version if isinstance(version, str) and version else 'unknown version'}"

    return connection.result(ask)


def run(ctx: Context) -> Sweep:
    info = ctx.instance
    profiles, bad = profiles_of(info.config.get('profiles'))
    if bad:
        return Sweep(PassResult('prom', False, f'error: {bad}'))
    try:
        client = client_for(info, ctx.http())
        data, hard, alerts, deploys = prom_pass(client, ctx.now, profiles, info.config.get('ingress-exclude') or '')
        found, rows, routes, dep_rows, _ = evaluate_prom(ctx.now, data, hard, alerts, deploys, None, ctx.rules('rates'), info.segment)
    except PrometheusError as e:
        if access.denied(e.code):
            return Sweep(PassResult('prom', True, access.status('ok', [PART]), [access.card('prom', NAME, PART, status=e.code, credential=credential(info.config))], {}, (access.ran_key('prom', PART),)))
        return Sweep(PassResult('prom', False, f'error: {e.reason}'))
    return Sweep(PassResult('prom', True, 'ok', found, {}, RAN + (access.ran_key('prom', PART),)), sections={'services': list(rows.values()), 'routes': routes, 'deploys': dep_rows},
                 deploys=deploy_docs(deploys))


def promql(ctx: Context, args: dict) -> object:
    expr = args.get('expr')
    if not isinstance(expr, str) or not expr.strip():
        raise ReadError(400, 'expr is required')
    try:
        at = float(args.get('at') or ctx.now)
        minutes = int(args['range_minutes']) if args.get('range_minutes') else None
    except (TypeError, ValueError, OverflowError):
        raise ReadError(400, 'at must be unix seconds and range_minutes a whole number of minutes') from None
    client = client_for(ctx.instance, ctx.http())
    try:
        if minutes:
            return ranged(client, expr, at, min(minutes, 10080))
        return client.instant(expr, at)
    except PrometheusError as e:
        if access.denied(e.code):
            raise ReadError(e.code, access.text(NAME, PART, credential=credential(ctx.instance.config), status=e.code)) from None
        raise ReadError(502, e.reason) from None


def _ui(info: InstanceInfo) -> str:
    return (info.config.get('ui-url') or info.config.get('url') or '').rstrip('/')


def links(card: dict, info: InstanceInfo) -> list[dict] | None:
    return links_for(card, _ui(info), info.config.get('namespaces') or '.*')


def row_links(row: dict, window: dict, info: InstanceInfo) -> dict[str, str]:
    return signal_row_links(row, window, _ui(info))


def probe_targets(info: InstanceInfo) -> list[dict]:
    return [{'name': f'prometheus-{info.name}', 'url': info.config['url'].rstrip('/') + '/-/ready'}]


PLUGIN = Plugin(
    id=ID, name='Prometheus', version='0.1.0', category='metrics', description='Errors, latency, restarts, alerts and image changes read from Prometheus.', icon='icon.svg',
    author='Skopax', licence='Apache-2.0', core='>=0.1,<0.3', edition='community', capabilities=Capabilities(hosts=('@url', '@ui-url'), secrets=('token', 'password')),
    fields=(Field('url', 'URL', 'url', required=True, group='connection', help='Base URL of the Prometheus API.'),
            Field('ui-url', 'UI URL', 'url', ui=True, group='connection', help='Address people open for graph links. Defaults to the URL.'),
            Field('auth', 'Authentication', 'choice', default='none', choices=('none', 'bearer', 'basic'),
                  choice_labels={'none': 'None', 'bearer': 'Bearer token', 'basic': 'Username and password'}, group='sign-in', help='How Skopax signs in to Prometheus: none, a bearer token, or a username and password.'),
            Field('token', 'Token', 'secret', show_if=('auth', 'bearer'), group='sign-in', help='Sent as Authorization: Bearer with every query.'),
            Field('username', 'Username', 'text', show_if=('auth', 'basic'), group='sign-in', help='User for HTTP basic authentication.'),
            Field('password', 'Password', 'secret', show_if=('auth', 'basic'), group='sign-in', help='Password of that user.'),
            Field('ca', 'CA certificate', 'pem', group='advanced', help='PEM certificate to trust instead of the system roots.'),
            Field('ca-pinned', 'Trust this certificate as it is', 'bool', default='false', show_if=('ca', '*'), group='advanced', help="For a server whose own certificate fails the usual checks: paste the server's own certificate; with the switch on, only that exact certificate is accepted, even if it is expired, self-signed or names another host. A CA certificate does not work with the switch on."),
            Field('namespaces', 'Namespaces', 'regex', default='.*', group='scope', help='Only namespaces that match this pattern are watched.'),
            Field('ingress-exclude', 'Ingress exclusion pattern', 'regex', group='scope', help='Ingresses whose name matches this pattern are left out of the ingress profile. Empty leaves none out.'),
            Field('profiles', 'Profiles', 'text', default=','.join(PROFILES), group='scope',
                  help='Comma list of the metric families to read: spring (Micrometer HTTP, log and Hikari metrics), krakend (KrakenD gateway), ingress (ingress-nginx), '
                       'kube (kube-state-metrics restarts, pod states, nodes and image changes, which also feed Releases) and kafka (consumer lag). Alerts always run. Empty means all five.')),
    env_instances=env_instances, test=test,
    checks=(Check(CHECK, 'Errors, latency, restarts and alerts', 900, 120, RAN, run, tool='promql', legacy_alias=True, sections=('services', 'deploys', 'routes'), section_help=SECTION_HELP),),
    rules=(RuleSet('rates', 'Rate floors', dict(FLOORS), RATE_HELP),),
    tools=(Tool('promql', PROMQL, PROMQL_SCHEMA, promql, instance_input='cluster'),),
    links=links, row_links=row_links, probe_targets=probe_targets, panels=(Panel('svcs'),),
    knowledge=(Knowledge('promql', 'Prometheus', (Path(__file__).parent / 'promql.md').read_text(encoding='utf-8'), slack=True),),
)
