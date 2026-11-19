from pathlib import Path

from skopax.contract import Capabilities, Check, Context, Field, HttpError, InstanceInfo, Knowledge, PassResult, Plugin, RuleSet, Setup, Sweep, TestResult, Tool, iso
from skopax.kit import connection
from skopax.plugins.crux import crux as engine
from skopax.plugins.crux.crux import (ACCESS, DOT, ID, MAX_PERIODS, METRICS, NAME, NO_DATA, PART, PASS, REFUSED, RULE_HELP, RULES, SECTION, TOOL_BUDGET_S, TOOL_FORMS,
                                      Crux, CruxError, Denied, access_card, links_for, malformed, origin_list, origins_of, query, read, wanted)

TEST_TIMEOUT_S = 10
PERMISSION_CODE = 403
PER_RUN = 4
SECTION_HELP = {SECTION: 'p75 INP, LCP and CLS of real Chrome users per origin and device, one value per 28-day period, the periods a week apart'}
DESCRIPTION = ('Chrome UX Report: page speed real Chrome users had on a configured origin ({field:origins}) or one URL on it, read only. '
               'Each value is the p75 over the 28 days ending on a date, the dates a week apart: INP (good up to 200 ms, poor above 500), '
               'LCP (good up to 2500 ms, poor above 4000) and CLS (good up to 0.10, poor above 0.25), with the latest band. '
               'origin (the first configured origin by default) or url (a full URL on a configured origin, with its scheme; a rarely visited page has no data); '
               f'form PHONE (default), DESKTOP or ALL; periods 1-{MAX_PERIODS} (default 8). '
               'It lags a release by days to weeks, so use it on crux cards and for questions about page speed, not to clear a fresh deploy. '
               f'At most {PER_RUN} calls per run.')
STR = {'type': 'string'}
SCHEMA = {'type': 'object',
          'properties': {'origin': STR, 'url': STR, 'form': {'type': 'string', 'enum': list(TOOL_FORMS)}, 'periods': {'type': 'integer', 'minimum': 1, 'maximum': MAX_PERIODS}},
          'required': [], 'additionalProperties': False}


def client_of(ctx: Context, budget: float = engine.BUDGET_S) -> Crux:
    config = ctx.instance.config
    return Crux(config.get('api-key') or '', origin_list(config.get('origins')), ctx.http(), budget)


def test(ctx: Context) -> TestResult:
    config = ctx.instance.config

    def ask() -> str:
        origins, bad = origins_of(config.get('origins'))
        if bad:
            raise CruxError(malformed(bad[0]), 400)
        if not origins:
            raise CruxError('no origin is set on this instance', 400)
        first = origins[0]
        try:
            query(ctx.http(), config.get('api-key') or '', {'origin': first, 'metrics': list(METRICS), 'collectionPeriodCount': 1}, TEST_TIMEOUT_S)
        except HttpError as e:
            if e.code == 404:
                return f'CrUX{DOT}{first}{DOT}no data for this origin'
            if e.code == PERMISSION_CODE:
                raise Denied(e.code) from None
            if e.code == 400:
                raise CruxError(f'CrUX HTTP 400{REFUSED}: {first}', 400) from None
            raise
        return f'CrUX{DOT}{first}'

    return connection.result(ask)


def run(ctx: Context) -> Sweep:
    bad = origins_of(ctx.instance.config.get('origins'))[1]
    if bad:
        return Sweep(PassResult(PASS, False, f'error: {malformed(bad[0])}'))
    c = client_of(ctx)
    try:
        found, rows, empty, read_count, denied = engine.run_pass(c, ctx.rules('rules'))
    except CruxError as e:
        return Sweep(PassResult(PASS, False, f'error: {e.reason}'))
    if denied is not None:
        return Sweep(PassResult(PASS, True, f'ok{DOT}no access to {PART}', found + [access_card(denied)], {}, (ACCESS,)))
    sections = {SECTION: rows + [{'part': 'summary', 'at': iso(ctx.now)}]}
    total = len(c.origins)
    if read_count < total:
        return Sweep(PassResult(PASS, True, f'ok{DOT}read {read_count} of {total} origins', found, {}, (NO_DATA, ACCESS)), sections=sections)
    if len(empty) == total:
        return Sweep(PassResult(PASS, True, f'ok{DOT}no data', found, {}, (NO_DATA, ACCESS)), sections=sections)
    status = f"ok{DOT}no data for {', '.join(empty)}" if empty else 'ok'
    return Sweep(PassResult(PASS, True, status, found, {}, (PASS, ACCESS)), sections=sections)


def crux_tool(ctx: Context, args: dict) -> object:
    c = client_of(ctx, TOOL_BUDGET_S)
    asked = wanted(args, c.origins)
    return read(c, asked)


def links(card: dict, info: InstanceInfo) -> list[dict] | None:
    return links_for(card)


PLUGIN = Plugin(
    id=ID, name=NAME, version='0.1.0', category='web', description='Page speed (INP, LCP and CLS) real Chrome users had on your origins, read from the Chrome UX Report API.',
    icon='icon.svg', author='Skopax', licence='Apache-2.0', core='>=0.1,<0.3', edition='community',
    capabilities=Capabilities(hosts=('chromeuxreport.googleapis.com',), secrets=('api-key',)),
    fields=(Field('api-key', 'API key', 'secret', required=True, help='A Google API key with the Chrome UX Report API enabled.'),
            Field('origins', 'Origins', 'text', required=True, help='Comma list of the origins to watch, for example https://www.example.com, https://shop.example.com. '
                                                                  'A bare host name means https; a path is ignored.')),
    test=test,
    checks=(Check(PASS, 'Page speed of real Chrome users', 86400, 28800, (PASS,), run, tool='crux', sections=(SECTION,), section_help=SECTION_HELP),),
    rules=(RuleSet('rules', 'Page speed (CrUX)', dict(RULES), RULE_HELP),),
    tools=(Tool('crux', DESCRIPTION, SCHEMA, crux_tool, per_run=PER_RUN, scan=True),),
    links=links,
    setup=Setup(steps=(
                   'In Google Cloud console, open APIs & Services > Library, search Chrome UX Report API and click Enable.',
                   'Open APIs & Services > Credentials > Create credentials > API key. Under API restrictions choose Restrict key, select only Chrome UX Report API, and save.',
                   'Paste the key into API key.'),
                docs='https://developer.chrome.com/docs/crux/api', docs_label='Chrome UX Report API'),
    knowledge=(Knowledge('crux', 'Chrome UX Report', (Path(__file__).parent / 'crux.md').read_text(encoding='utf-8'), slack=True),),
)
