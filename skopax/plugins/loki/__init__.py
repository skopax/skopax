from pathlib import Path

from skopax.contract import Capabilities, Check, Context, Field, HttpError, InstanceInfo, Knowledge, Plugin, ReadError, Setup, SourceDown, Sweep, TestResult, Tool
from skopax.kit import access, connection
from skopax.kit.promql import credential
from skopax.plugins.loki.loki import (DEFAULT_SERVICE, DOT, MIN_LABELS, MIN_LEVELS, NAME, PART, PASS, SECTION, WINDOW_S, Budget, LokiError, client_for, instance_matchers,
                                      levels_of, name_of, option_problem, render, selector_problem, sweep, tenant_problem)
from skopax.plugins.loki.tools import (COUNT_DESCRIPTION, COUNT_SCHEMA, LABELS_DESCRIPTION, LABELS_SCHEMA, PERMISSION, QUERY_DESCRIPTION, QUERY_SCHEMA,
                                       loki_count as read_count, loki_labels as read_labels, loki_query as read_query)

PER_RUN = 5
MANY = -1
MANY_STREAMS = 'more streams match than one 8 MB answer lists'
TEST_CALL_S = 7
TEST_BUDGET_S = 15
SECTION_HELP = {SECTION: 'error (and, with a level label, warning) log patterns per service of the last hour, with their counts 1 and 7 days earlier'}


def client_of(ctx: Context, timeout: float | None = None, budget: Budget | None = None):
    return client_for(ctx.instance.config, ctx.http(), *([timeout] if timeout else []), budget=budget)


def refused(e: LokiError, config) -> Exception:
    if e.spent:
        return SourceDown(f'Test connection ran out of its {TEST_BUDGET_S} s')
    if e.tenant:
        return ReadError(401, e.reason)
    if access.denied(e.code):
        said = access.tested(NAME, PART, e.code, PERMISSION, credential(config))
        return ReadError(e.code, said) if said else HttpError(e.code, NAME)
    if e.answered:
        return ReadError(502, e.reason)
    return e


def test(ctx: Context) -> TestResult:
    config = ctx.instance.config

    def ask() -> str:
        problem = selector_problem(config.get('selector')) or option_problem(config)
        if problem:
            raise SourceDown(problem)
        selector = render(instance_matchers(config))
        client = client_of(ctx, TEST_CALL_S, Budget(TEST_BUDGET_S))
        try:
            version = client.version()
            ready = client.ready()
            try:
                streams = len(client.series(selector, ctx.now - WINDOW_S, ctx.now))
            except LokiError as e:
                if not e.large:
                    raise
                streams = MANY
        except LokiError as e:
            raise refused(e, config) from None
        if not streams:
            raise SourceDown('Loki answered, but no stream matches the selector in the last hour; check Selector (and Tenant)')
        parts = [f"{NAME} {version or '(version not shown)'}", MANY_STREAMS if streams == MANY else f"{streams} {'stream matches' if streams == 1 else 'streams match'}"]
        if ready is False:
            parts.append('Loki says it is not ready yet')
        if str(config.get('min-severity') or '') == 'warn' and levels_of(config) == ('ERROR',):
            parts.append('Lowest severity Warning needs a level label, so only errors are watched')
        return DOT.join(parts)

    return connection.result(ask)


def run(ctx: Context) -> Sweep:
    return sweep(client_of(ctx, budget=Budget()), ctx.instance.config, ctx.now, ctx.last_status(), ctx.previous(SECTION))


def loki_query(ctx: Context, args: dict) -> object:
    return read_query(client_of(ctx), ctx.instance.config, args, ctx.now)


def loki_count(ctx: Context, args: dict) -> object:
    return read_count(client_of(ctx), ctx.instance.config, args, ctx.now)


def loki_labels(ctx: Context, args: dict) -> object:
    return read_labels(client_of(ctx), ctx.instance.config, args, ctx.now)


def probe_targets(info: InstanceInfo) -> list[dict]:
    return [{'name': f'loki-{info.name}', 'url': info.config['url'].rstrip('/') + '/'}]


def label_check(value: object) -> str | None:
    text = str(value or '').strip()
    return None if not text or name_of({'x': text}, 'x') else 'A label name holds letters, digits and _, and does not start with a digit'


PLUGIN = Plugin(
    setup=Setup(
        steps=(
            'Self-hosted Loki: enter its base address in URL, such as https://loki.example.com (the API is <URL>/loki/api/v1). Loki has no sign-in of its own; pick the one of the proxy in front of it.',
            'Multi-tenant Loki (auth_enabled: true): enter the tenant id in Tenant; it is sent as X-Scope-OrgID. A single-tenant Loki needs no tenant.',
            'Grafana Cloud: open your stack, Loki, Details. The URL is shown there and the user is the number shown as User. Create an access policy token with the logs:read scope and use it as Password, with Username and password as Authentication. No tenant is needed.',
            'Enter a Selector, the LogQL stream selector to watch, such as {namespace=~"prod-.*"}. Every query the check and the tools send stays inside it.',
            'Optionally set Service label (default app, then service_name), a Level label such as level or detected_level, and Lowest severity, then press Test connection.',
        ),
        docs='https://grafana.com/docs/loki/latest/reference/loki-http-api/',
        docs_label='Grafana Loki: HTTP API',
    ),
    id=PASS, name=NAME, version='0.1.0', category='logs',
    description='New and growing error log patterns per service, read from Grafana Loki with LogQL, and read tools for log lines, counts and labels.',
    icon='icon.svg', author='Skopax', licence='Apache-2.0', core='>=0.5,<0.6', edition='community', capabilities=Capabilities(hosts=('@url',), secrets=('token', 'password')),
    fields=(Field('url', 'URL', 'url', required=True, group='connection', help='Base address of Loki, without /loki/api/v1, such as https://loki.example.com.'),
            Field('tenant', 'Tenant', 'text', default='', group='connection', check=tenant_problem,
                  help='Sent as X-Scope-OrgID for a multi-tenant Loki. Empty sends no tenant. One tenant only: a | (several tenants) is refused.'),
            Field('auth', 'Authentication', 'choice', default='none', choices=('none', 'bearer', 'basic'),
                  choice_labels={'none': 'None', 'bearer': 'Bearer token', 'basic': 'Username and password'}, group='sign-in',
                  help='How Skopax signs in to Loki or the proxy in front of it: none, a bearer token, or a username and password (Grafana Cloud).'),
            Field('token', 'Token', 'secret', show_if=('auth', 'bearer'), group='sign-in', help='Sent as Authorization: Bearer with every query.'),
            Field('username', 'Username', 'text', show_if=('auth', 'basic'), group='sign-in', help='User for HTTP basic authentication; on Grafana Cloud the number shown as User.'),
            Field('password', 'Password', 'secret', show_if=('auth', 'basic'), group='sign-in', help='Password of that user; on Grafana Cloud an access policy token with logs:read.'),
            Field('selector', 'Selector', 'text', required=True, group='scope', check=selector_problem,
                  help='LogQL stream selector of the logs to watch, such as {namespace=~"prod-.*"}. The check and every tool query stay inside it.'),
            Field('service-label', 'Service label', 'text', default=DEFAULT_SERVICE, group='scope', check=label_check,
                  help='Label that names the service of a line; lines without it use service_name.'),
            Field('level-label', 'Level label', 'text', default='', group='scope', check=label_check,
                  help='Label that holds the log level, such as level or detected_level. When set, error and warning lines are taken from it and Error pattern is not used.'),
            Field('error-pattern', 'Error pattern', 'regex', default='', group='scope',
                  help='Line filter for error lines when no level label is set. Empty means (?i)(error|exception|fatal|panic). Loki runs it (RE2 syntax).'),
            Field('min-severity', 'Lowest severity', 'choice', default='error', choices=MIN_LEVELS, choice_labels=MIN_LABELS, group='scope',
                  help='Error watches error lines only; Warning also watches warning lines, which needs a level label.'),
            Field('ca', 'CA certificate', 'pem', group='advanced', help='PEM certificate to trust instead of the system roots.'),
            Field('ca-pinned', 'Trust this certificate as it is', 'bool', default='false', show_if=('ca', '*'), group='advanced', help="For a server whose own certificate fails the usual checks: paste the server's own certificate; with the switch on, only that exact certificate is accepted, even if it is expired, self-signed or names another host. A CA certificate does not work with the switch on.")),
    test=test,
    checks=(Check(PASS, 'Error log patterns', 900, 435, (PASS,), run, tool='loki_query', sections=(SECTION,), section_help=SECTION_HELP),),
    tools=(Tool('loki_query', QUERY_DESCRIPTION, QUERY_SCHEMA, loki_query, per_run=PER_RUN),
           Tool('loki_count', COUNT_DESCRIPTION, COUNT_SCHEMA, loki_count, per_run=PER_RUN),
           Tool('loki_labels', LABELS_DESCRIPTION, LABELS_SCHEMA, loki_labels, per_run=PER_RUN)),
    probe_targets=probe_targets,
    knowledge=(Knowledge('loki', NAME, (Path(__file__).parent / 'loki.md').read_text(encoding='utf-8'), slack=True),),
)
