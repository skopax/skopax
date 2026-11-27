import time
from pathlib import Path

from skopax.contract import Capabilities, Check, Context, Field, InstanceInfo, Knowledge, Panel, Plugin, ReadError, Setup, Sweep, TestResult, Tool
from skopax.kit import access, connection
from skopax.plugins.zabbix import page
from skopax.plugins.zabbix.tools import (HISTORY_DESCRIPTION, HISTORY_SCHEMA, HOST_DESCRIPTION, HOST_SCHEMA, PROBLEM_DESCRIPTION, PROBLEM_SCHEMA, zabbix_history as read_history,
                                         zabbix_host as read_host, zabbix_problem as read_problem)
from skopax.plugins.zabbix.zabbix import (DEFAULT_MIN, NAME, PASS, SECTION, SEVERITY_LABELS, SEVERITY_NAMES, ZabbixError, client_for, link_for,
                                          scope_of, sweep)

PER_RUN = 5
TEST_CALL_S = 7
DOT = ' \u00b7 '
TOKEN_REFUSED = ('Zabbix refused the token: check that it is valid, not expired or disabled, and that its user has API access '
                 'and read access to the host groups')
SECTION_HELP = {SECTION: 'when the Zabbix problem check last ran, the Zabbix version, how many active problems it read and whether it hit the cap', page.PAGE: page.PAGE_HELP}


def client_of(ctx: Context, timeout: float | None = None):
    return client_for(ctx.instance.config, ctx.http(), *([timeout] if timeout else []))


def test(ctx: Context) -> TestResult:
    def ask() -> str:
        z = client_of(ctx, TEST_CALL_S)
        try:
            version = z.connect()
        except ZabbixError as e:
            if e.answered:
                raise ReadError(502, e.reason) from None
            raise
        try:
            z.listed('hostgroup.get', {'output': ['groupid'], 'limit': 1})
            scope = scope_of(z, ctx.instance.config)
            count = z.count('problem.get', scope.params())
        except ZabbixError as e:
            if access.denied(e.code):
                raise ReadError(e.code, f'{TOKEN_REFUSED} ({e.reason})') from None
            raise ReadError(502, e.reason) from None
        missing = f"{DOT}no host group named {', '.join(scope.missing)}" if scope.missing else ''
        return f"{NAME} {version}{DOT}{count} {'problem' if count == 1 else 'problems'}{missing}"

    return connection.result(ask)


def run(ctx: Context) -> Sweep:
    info = ctx.instance
    z = client_of(ctx)
    started = time.monotonic()
    return page.extend(sweep(z, info.config, ctx.now, ctx.last_status(), info.name), z, info.config, ctx.now, info.name, started, ctx.previous(page.PAGE))


def zabbix_problem(ctx: Context, args: dict) -> object:
    return read_problem(client_of(ctx), args)


def zabbix_host(ctx: Context, args: dict) -> object:
    return read_host(client_of(ctx), args)


def zabbix_history(ctx: Context, args: dict) -> object:
    return read_history(client_of(ctx), args, ctx.now)


def links(card: dict, info: InstanceInfo) -> list[dict] | None:
    return link_for(card, info.config.get('url') or '')


def probe_targets(info: InstanceInfo) -> list[dict]:
    return [{'name': f'zabbix-{info.name}', 'url': info.config['url'].rstrip('/') + '/'}]


PLUGIN = Plugin(
    setup=Setup(
        steps=(
            'In Zabbix create a user (or pick one) whose user group has Read access to the host groups to watch, and whose role allows the API (Users > User roles > API access).',
            'Open Users > API tokens > Create API token, pick that user, set an expiry if you like, and choose Add. Copy the token at once: Zabbix shows it once.',
            'Paste the token into Token and the Zabbix frontend address into URL (the address you open in the browser; the API is <URL>/api_jsonrpc.php).',
            'Zabbix 6.0 works too: Skopax reads the version first and, below 6.4, also sends the token in the request body as that version expects.',
            'Optionally narrow it with Host groups, Tags and Lowest severity, then press Test connection.',
        ),
        docs='https://www.zabbix.com/documentation/current/en/manual/web_interface/frontend_sections/users/api_tokens',
        docs_label='Zabbix: API tokens',
    ),
    id='zabbix', name=NAME, version='0.1.0', category='alerts', description='A card for each active Zabbix problem, by trigger, and read tools for problems, hosts and item history.',
    icon='icon.svg', author='Skopax', licence='AGPL-3.0-only', core='>=0.5,<0.8', edition='community', capabilities=Capabilities(hosts=('@url',), secrets=('token',)),
    fields=(Field('url', 'URL', 'url', required=True, ui=True, help='Address of the Zabbix frontend, for example https://zabbix.example.com. The API is <URL>/api_jsonrpc.php.'),
            Field('token', 'Token', 'secret', required=True, help='A Zabbix API token of a user with read access to the host groups to watch.'),
            Field('host-groups', 'Host groups', 'text', default='', help='Comma-separated host group names to watch. Empty watches every group the user can read.'),
            Field('tags', 'Tags', 'text', default='', help='Comma-separated problem tags as name or name:value; a problem with any of them is watched. Empty watches all.'),
            Field('min-severity', 'Lowest severity', 'choice', default=DEFAULT_MIN, choices=SEVERITY_NAMES, choice_labels=SEVERITY_LABELS,
                  help='Problems below this Zabbix severity open no card.'),
            Field('ca', 'CA certificate', 'pem', group='advanced', help='PEM certificate to trust instead of the system roots.'),
            Field('ca-pinned', 'Trust this certificate as it is', 'bool', default='false', show_if=('ca', '*'), group='advanced', help="For a server whose own certificate fails the usual checks: paste the server's own certificate; with the switch on, only that exact certificate is accepted, even if it is expired, self-signed or names another host. A CA certificate does not work with the switch on.")),
    test=test,
    checks=(Check(PASS, 'Active Zabbix problems', 900, 510, (PASS,), run, tool='zabbix_problem', sections=(SECTION, page.PAGE), section_help=SECTION_HELP),),
    tools=(Tool('zabbix_problem', PROBLEM_DESCRIPTION, PROBLEM_SCHEMA, zabbix_problem, per_run=PER_RUN),
           Tool('zabbix_host', HOST_DESCRIPTION, HOST_SCHEMA, zabbix_host, per_run=PER_RUN),
           Tool('zabbix_history', HISTORY_DESCRIPTION, HISTORY_SCHEMA, zabbix_history, per_run=PER_RUN)),
    links=links, probe_targets=probe_targets, panels=(Panel(PASS, 'panel.js', label=NAME, group='observe'),),
    knowledge=(Knowledge('zabbix', NAME, (Path(__file__).parent / 'zabbix.md').read_text(encoding='utf-8'), slack=True),),
)
