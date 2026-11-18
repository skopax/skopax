from pathlib import Path

from skopax.contract import Capabilities, Check, Context, Field, HttpError, InstanceInfo, Knowledge, Panel, PassResult, Plugin, ReadError, RuleSet, Sweep, TestResult, Tool
from skopax.kit import connection
from skopax.plugins.cloudflare import client as cf
from skopax.plugins.cloudflare.checks import EDGE_HELP, EDGE_RULE, MID, Pass, cf_pass, components_of, edge_pass, evaluate_edge, outcome, probe_analytics
from skopax.plugins.cloudflare.client import (DENIED, STATUS_PAGE, VERIFY, ZONE_NAME, Cloudflare, CloudflareError, Zone, account_id, client_for, lacks, traffic,
                                              zone_names)
from skopax.plugins.cloudflare.tools import (EDGE_DESCRIPTION, EDGE_SCHEMA, GQL_DESCRIPTION, GQL_LACKS, GQL_RUN_CALLS, GQL_SCHEMA, edge_request, edge_status, edge_top,
                                             graphql_check, graphql_read, zone_hint)

ID = 'cloudflare'
EDGE_SIGS = ('edge-conn', 'edge-5xx')
STATUS_SIGS = ('cf-status', 'cf-incident')
STATUS_LINK = dict(label='Cloudflare status page', url=STATUS_PAGE)
NONE_FOUND = 'no watched zone found'
SECTION_HELP = {
    'cloudflare': 'health checks (part checks), certificate packs (part certs) and watched status components (part components) of the zones, plus a summary row',
    'edge': 'one row per watched zone: requests in the last hour, cached share, 502 / 52x and other 5xx shares, and the same shares 1 and 7 days earlier',
}


def _zones(client: Cloudflare, names: list[str], out: Pass) -> tuple[list[Zone], list[str]]:
    try:
        found, missing = client.zones(names)
    except CloudflareError as e:
        if e.denied:
            out.deny('zones', e.reason, invalid=e.invalid)
        else:
            out.fail('zones', e.reason)
        return [], []
    if not found:
        out.fail('zones', NONE_FOUND)
        return [], []
    out.reach('zones')
    return found, missing


def _dropped(ctx: Context, check: str, names: list[str]) -> list[str]:
    info = ctx.instance
    tail = '' if info is None or info.primary else f'-{info.name}'
    out = []
    for card in ctx.open_cards():
        key = card.get('ranKey')
        if not isinstance(key, str) or not key.endswith(tail):
            continue
        key = key[:len(key) - len(tail)]
        head, _, rest = key.partition(':')
        zone = rest.split(':', 1)[0]
        if head == check and ZONE_NAME.fullmatch(zone) and zone not in names:
            out.append(key)
    return out


def _refused(part: str, zones: list[str], invalid: bool = False) -> ReadError:
    where = f" of {', '.join(zones)}" if zones else ''
    return ReadError(400, f'Cloudflare cannot read {part}{where}: {lacks(part, invalid)}')


def _probe(client: Cloudflare, zones: list[Zone], now: float, off: frozenset[str]) -> None:
    zone = zones[0]
    parts = () if 'cf' in off else (('health checks', f'/zones/{zone.id}/healthchecks'), ('certificates', f'/zones/{zone.id}/ssl/certificate_packs'))
    for part, path in parts:
        try:
            client.listing(path, pages=1)
        except CloudflareError as e:
            if e.denied:
                raise _refused(part, [zone.name], e.invalid) from None
            if part == 'health checks' and e.unavailable():
                continue
            if e.answer():
                raise ReadError(400, e.reason) from None
            raise
    if 'edge' in off:
        return
    try:
        probe_analytics(client, zones, now)
    except CloudflareError as e:
        if e.denied:
            raise _refused('analytics', [z.name for z in zones], e.invalid) from None
        if e.answer():
            raise ReadError(400, e.reason) from None
        raise


def test(ctx: Context) -> TestResult:
    info = ctx.instance

    def ask() -> str:
        names = zone_names(info.config.get('zones'))
        if not names:
            raise ReadError(400, 'Zones is empty: name at least one zone')
        client = client_for(info, ctx.http(), cf.TEST_BUDGET_S, cf.TEST_CALL_S)
        try:
            status, refused = client.verify(), None
        except CloudflareError as e:
            if e.code not in DENIED:
                raise
            status, refused = 'unknown', e.code
        try:
            found, missing = client.zones(names)
        except CloudflareError as e:
            if refused is not None:
                raise HttpError(refused, VERIFY) from None
            if e.denied:
                raise _refused('zones', [], e.invalid) from None
            if e.answer():
                raise ReadError(400, e.reason) from None
            raise
        if refused is not None:
            owner = account_id(info.config.get('account')) or next((z.account for z in found if z.account), '')
            if not owner:
                raise HttpError(refused, VERIFY)
            try:
                status = client.verify(owner)
            except CloudflareError:
                raise HttpError(refused, VERIFY) from None
        if status != 'active':
            raise ReadError(400, f'Cloudflare token is {status}')
        if missing:
            raise ReadError(400, f"Cloudflare token active, but it cannot see the zone{'s' if len(missing) > 1 else ''} {', '.join(missing)}")
        _probe(client, found, ctx.now, info.off_checks)
        return f"Cloudflare token active{MID}zones: {', '.join(z.name for z in found)}"

    return connection.result(ask)


def _blind(got: Pass) -> bool:
    return bool(got.errors) and not got.reached


def run_cf(ctx: Context) -> Sweep:
    info = ctx.instance
    names = zone_names(info.config.get('zones'))
    components = components_of(info.config.get('status-components'))
    client = client_for(info, ctx.http(), cf.BUDGET_S)
    got = Pass()
    zones, missing = _zones(client, names, got)
    cf_pass(client, zones, components, ctx.now, got)
    if components and 'status' not in got.errors:
        got.reach('status')
    status, ran = outcome('cf', got, got.ran + _dropped(ctx, 'cf', names), missing + got.absent)
    if _blind(got):
        return Sweep(PassResult('cf', False, status))
    summary = dict(part='summary', zones=[z.name for z in zones], missing=missing, noHealthChecks=got.bare, statusOff=not components, noAccess=got.no_access())
    return Sweep(PassResult('cf', True, status, got.found + got.access('cf'), {}, ran), sections={'cloudflare': got.rows + [summary]})


def run_edge(ctx: Context) -> Sweep:
    info = ctx.instance
    names = zone_names(info.config.get('zones'))
    client = client_for(info, ctx.http(), cf.BUDGET_S)
    got = Pass()
    zones, missing = _zones(client, names, got)
    data = None
    if zones:
        try:
            data = edge_pass(client, zones, ctx.now)
            got.reach('analytics')
        except CloudflareError as e:
            if e.denied:
                for zone in zones:
                    got.deny('analytics', e.reason, zone.name, e.invalid)
            else:
                got.fail('analytics', e.reason)
    found, rows = evaluate_edge(data, zones, ctx.now, ctx.rules('edge'), account_id(info.config.get('account'))) if data else ([], [])
    seen = {r['zone'] for r in rows if r.get('req') is not None}
    got.read = {z.name: z.name in seen for z in zones} if data else {}
    status, ran = outcome('edge', got, [f'edge:{z.name}' for z in zones if z.name in seen] + _dropped(ctx, 'edge', names), missing)
    if _blind(got):
        return Sweep(PassResult('edge', False, status))
    return Sweep(PassResult('edge', True, status, found + got.access('edge'), {}, ran), sections={'edge': rows})


def graphql(ctx: Context, args: dict) -> object:
    info = ctx.instance
    client = client_for(info, ctx.http(), cf.TOOL_BUDGET_S)
    names = zone_names(info.config.get('zones'))

    def hint() -> str:
        return zone_hint(client, names, account_id(info.config.get('account')))

    query, variables = graphql_check(args.get('query'), args.get('variables'), hint)
    try:
        return graphql_read(client, query, variables, hint)
    except CloudflareError as e:
        raise ReadError(502, e.shown(client.token) + (f'; {GQL_LACKS}' if e.denied else '')) from None


def _failed(client: Cloudflare, e: CloudflareError, part: str) -> ReadError:
    return ReadError(502, e.shown(client.token) + (f'; {lacks(part, e.invalid)}' if e.denied else ''))


def edge(ctx: Context, args: dict) -> object:
    info = ctx.instance
    op, name, host, path, start, (label, codes) = edge_request(args, zone_names(info.config.get('zones')), ctx.now)
    client = client_for(info, ctx.http(), cf.TOOL_BUDGET_S)
    try:
        found, _ = client.zones([name])
    except CloudflareError as e:
        raise _failed(client, e, 'zones') from None
    if not found:
        raise ReadError(400, f'the token cannot see the zone {name}')
    try:
        if op == 'status':
            return edge_status(client, found[0], host, path, start, ctx.now)
        return edge_top(client, found[0], host, path, codes, label, start, ctx.now)
    except CloudflareError as e:
        raise _failed(client, e, 'analytics') from None


def _https(links: object) -> list[dict]:
    return [dict(label=x['label'], url=x['url']) for x in links if isinstance(x, dict) and isinstance(x.get('label'), str) and str(x.get('url') or '').startswith('https://')] \
        if isinstance(links, list) else []


def links(card: dict, info: InstanceInfo) -> list[dict] | None:
    if card.get('src') != 'cf':
        return None
    sig = card.get('sig')
    if sig in EDGE_SIGS:
        pinned = account_id(info.config.get('account'))
        if pinned:
            zone = str(card.get('svc') or '')
            url = traffic(pinned, zone)
            own = [dict(label=f'Cloudflare traffic analytics for {zone}', url=url)] if url else []
        else:
            own = _https(card.get('urls'))
        return own + [dict(STATUS_LINK)]
    if sig in STATUS_SIGS:
        return [dict(STATUS_LINK)]
    return []


PLUGIN = Plugin(
    id=ID, name='Cloudflare', version='0.1.0', category='web',
    description='Health checks, certificate packs, status-page components and edge 5xx shares of Cloudflare zones.', icon='icon.svg',
    author='Skopax', licence='Apache-2.0', core='>=0.1,<0.2', edition='community',
    capabilities=Capabilities(hosts=('api.cloudflare.com', 'www.cloudflarestatus.com'), secrets=('token',)),
    fields=(Field('token', 'API token', 'secret', required=True, help='A read-only API token with Zone Read, Analytics Read, SSL and Certificates Read and Health Checks Read on the watched zones.'),
            Field('zones', 'Zones', 'text', required=True, help='Comma-separated zone names to watch, for example example.com, example.org. Their ids are looked up with the token.'),
            Field('account', 'Account id', 'text', help='Account id for account-level GraphQL and dashboard links. Empty: the account of the first zone.'),
            Field('status-components', 'Status components', 'text',
                  help='Semicolon-separated Cloudflare status-page components to watch, written as the status page shows them, for example CDN/Cache; WAF; Tokyo, Japan - (NRT). '
                       'Empty: no status check.')),
    test=test,
    checks=(Check('cf', 'Health checks, certificates and status', 900, 60, ('cf',), run_cf, sections=('cloudflare',), section_help={'cloudflare': SECTION_HELP['cloudflare']}),
            Check('edge', 'Edge 5xx', 900, 60, ('edge',), run_edge, tool='edge', sections=('edge',), section_help={'edge': SECTION_HELP['edge']})),
    rules=(RuleSet('edge', 'Cloudflare edge', dict(EDGE_RULE), EDGE_HELP),),
    tools=(Tool('cloudflare_graphql', GQL_DESCRIPTION, GQL_SCHEMA, graphql, per_run=GQL_RUN_CALLS),
           Tool('edge', EDGE_DESCRIPTION, EDGE_SCHEMA, edge)),
    links=links,
    panels=(Panel('cf', 'panel.js', label='Cloudflare', group='observe'),),
    knowledge=(Knowledge('cloudflare', 'Cloudflare', (Path(__file__).parent / 'cloudflare.md').read_text(encoding='utf-8'), slack=True),),
)
