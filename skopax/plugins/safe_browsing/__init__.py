from pathlib import Path

from skopax.contract import Capabilities, Check, Context, Field, HttpError, InstanceInfo, Knowledge, Plugin, ReadError, Sweep, TestResult, Tool
from skopax.kit import connection
from skopax.plugins.safe_browsing import safebrowsing as engine
from skopax.plugins.safe_browsing.safebrowsing import (CLEAN, DOT, ID, NAME, PASS, REFUSED, SECTION, TOOL_BUDGET_S, TOOL_URLS, Denied, NotChecked, SafeBrowsing,
                                                       links_for, read, wanted)

TEST_BUDGET_S = 12
PERMISSION_CODE = 403
PER_RUN = 3
NEEDS_KEY = 'the safebrowsing tool needs an API key on this Safe Browsing instance; without one only the check runs, through the public site status page'
SECTION_HELP = {SECTION: 'Safe Browsing status of each host from the last pass, and whether it came from the API or the public status page'}
DESCRIPTION = (f'Google Safe Browsing, read only: whether up to {TOOL_URLS} given http(s) addresses are listed as malware, social engineering '
               '(Chrome\'s "Deceptive site" page), unwanted software or harmful apps. It answers only for the addresses given (and their parent paths and host), '
               'so check the page a card or a report names, and a suspicious referrer host in the logs. Search Console > Security issues, which no tool reads, '
               f'has the full picture. It needs the instance\'s API key. At most {PER_RUN} calls per run.')
SCHEMA = {'type': 'object', 'properties': {'urls': {'type': 'array', 'items': {'type': 'string'}, 'minItems': 1, 'maxItems': TOOL_URLS}},
          'required': ['urls'], 'additionalProperties': False}


def test(ctx: Context) -> TestResult:
    def ask() -> str:
        sb = SafeBrowsing.of(ctx.instance.config, ctx.http(), TEST_BUDGET_S)
        if not sb.hosts:
            raise NotChecked('no host is set on this instance', 400)
        host = sb.hosts[0]
        if sb.api_key:
            try:
                flagged = bool(sb.find([f'https://{host}/']))
            except HttpError as e:
                if e.code == PERMISSION_CODE:
                    raise Denied(e.code) from None
                if e.code == 400:
                    raise NotChecked(f'Safe Browsing API HTTP 400{REFUSED}', 400) from None
                raise
        else:
            r = sb.lookup(host)
            flagged = not (r['status'] == CLEAN and not r['flags'])
        return f"{NAME}{DOT}{host} {'flagged' if flagged else 'clean'}"

    return connection.result(ask)


def run(ctx: Context) -> Sweep:
    return engine.run_pass(SafeBrowsing.of(ctx.instance.config, ctx.http()), ctx.now, ctx.open_cards())


def safebrowsing_tool(ctx: Context, args: dict) -> object:
    sb = SafeBrowsing.of(ctx.instance.config, ctx.http(), TOOL_BUDGET_S)
    if not sb.api_key:
        raise ReadError(503, NEEDS_KEY)
    return read(sb, wanted(args))


def links(card: dict, info: InstanceInfo) -> list[dict] | None:
    return links_for(card)


PLUGIN = Plugin(
    id=ID, name=NAME, version='0.1.0', category='web', description='Whether Google Safe Browsing lists your hosts as unsafe, read from the Safe Browsing API or the public site status page.',
    icon='icon.svg', author='Skopax', licence='Apache-2.0', core='>=0.1,<0.2', edition='community',
    capabilities=Capabilities(hosts=('safebrowsing.googleapis.com', 'transparencyreport.google.com'), secrets=('api-key',)),
    fields=(Field('api-key', 'API key', 'secret', help='A Google API key with the Safe Browsing API enabled. Empty reads the public site status page of each host instead, '
                                                       'one host every few seconds, and the safebrowsing tool stays hidden from the investigator.'),
            Field('hosts', 'Hosts', 'text', required=True, help='Comma list of the host names to check, for example www.example.com, shop.example.com.')),
    test=test,
    checks=(Check(PASS, 'Safe Browsing listings of the hosts', 900, 675, (PASS,), run, tool='safebrowsing', sections=(SECTION,), section_help=SECTION_HELP),),
    tools=(Tool('safebrowsing', DESCRIPTION, SCHEMA, safebrowsing_tool, per_run=PER_RUN, scan=True, needs_field='api-key'),),
    links=links,
    knowledge=(Knowledge('safe-browsing', 'Safe Browsing', (Path(__file__).parent / 'safe_browsing.md').read_text(encoding='utf-8'), slack=True),),
)
