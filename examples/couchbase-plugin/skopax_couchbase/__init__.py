import concurrent.futures
import json
import re

from skopax.contract import CardType, Check, Context, Field, HttpError, InstanceInfo, Panel, PassResult, Plugin, ReadError, RuleSet, Sweep, TestResult, Tool, anomaly, fmt, iso
from skopax.kit import access, connection
from skopax.kit.promql import Late, PrometheusError, bounded, client_for, credential, instant, number, prom_link, ui_of
from skopax.kit.text import duration

ID = 'couchbase'
NAME = 'Couchbase'
TOOL = 'couchbase_nodes'
PART = 'metrics'
PART_NAME = 'Couchbase metrics'
BUDGET_S = 200
NO_PEER = 'no Prometheus instance is set'
DEFAULT_SELECTOR = 'job=~".*couchbase.*"'
SELECTOR_MAX = 300
MATCHER = r' *[a-zA-Z_][a-zA-Z0-9_]* *(?:=~|!~|!=|=) *"(?:[^"\\\n]|\\.)*" *'
SELECTOR = re.compile(f'{MATCHER}(?:,{MATCHER})*')
BAD_SELECTOR = f'the selector must be label matchers such as {DEFAULT_SELECTOR}'
W = '60m'
OFFSETS = (1, 7)
MAX_ROWS = 50
NODE_KEYS = ('node', 'up', 'uptime', 'conn', 'connD1', 'connD7', 'imem', 'iquota', 'imemPct')
SUMMARY_KEYS = ('req', 'reqD1', 'reqD7', 'errors', 'errorsD1', 'errorsD7', 'share', 'timeouts', 'timeoutsD1', 'timeoutsD7', 'avgMs', 'avgD1', 'avgD7', 'queue', 'failover',
                'rebalance', 'stuck', 'auth')
QUERIES = dict(
    up='max by (instance) (up{{{sel}}})',
    uptime='max by (instance) (kv_uptime_seconds{{{sel}}})',
    conn='max by (instance) (max_over_time(kv_curr_connections{{{sel}}}[{w}]))',
    imem='max by (instance) (index_memory_used_total{{{sel}}})',
    iquota='max by (instance) (index_memory_quota{{{sel}}})',
    failover='max(increase(cm_auto_failover_count{{{sel}}}[{w}]))',
    stuck='max(cm_rebalance_stuck{{{sel}}})',
    rebalance='max(cm_rebalance_in_progress{{{sel}}})',
    req='sum(increase(n1ql_requests{{{sel}}}[{w}]))',
    errors='sum(increase(n1ql_errors{{{sel}}}[{w}]))',
    timeouts='sum(increase(n1ql_timeouts{{{sel}}}[{w}]))',
    rtime='sum(increase(n1ql_request_time{{{sel}}}[{w}]))',
    queue='max(max_over_time(n1ql_queued_requests{{{sel}}}[{w}]))',
    auth='sum(increase(cm_rest_request_auth_failure_total{{{sel}}}[{w}]))',
)
RULES = dict(uptime_s=3600, err_min=20, err_share=1, err_x=2, err_pp=0.5, err_crit=5, tmo_min=5, tmo_x=2, lat_req=300, lat_x=2, lat_ms=50, lat_floor=200,
             queue=20, imem=90, jump=1.5, jump_slack=200, auth=20)
RULE_HELP = {
    'uptime_s': 'Warning when a node has been up for less than this many seconds.',
    'err_min': 'N1QL errors are checked only with at least this many errors in 60 min.',
    'err_share': 'Error share in percent that warns while no baseline exists.',
    'err_x': 'With a baseline, warn at this many times the share of 1 or 7 days earlier,',
    'err_pp': 'and at least this many percentage points above it.',
    'err_crit': 'Critical at this error share in percent.',
    'tmo_min': 'Warning at this many N1QL timeouts in 60 min,',
    'tmo_x': 'and this many times the timeouts of 1 or 7 days earlier.',
    'lat_req': 'N1QL average time is checked only with at least this many queries in 60 min.',
    'lat_x': 'With a baseline, warn at this many times the average of 1 or 7 days earlier,',
    'lat_ms': 'and at least this many milliseconds above it.',
    'lat_floor': 'Average time in milliseconds that warns while no baseline exists.',
    'queue': 'Warning when more N1QL queries than this waited in the queue.',
    'imem': 'Info when the index service uses this percent of its memory quota.',
    'jump': 'Warning when a node peaks at this many times its connections of 1 or 7 days earlier,',
    'jump_slack': 'and at least this many connections more.',
    'auth': 'Info at this many failed REST authentications in 60 min.',
}
SECTION_HELP = {ID: 'one row per Couchbase node (uptime, connection peak, index memory) and one summary row per cluster (N1QL queries, errors, timeouts, time)'}
LINK = {
    'couchbase-down': 'up{{{sel}}}',
    'couchbase-restart': 'kv_uptime_seconds{{{sel}}}',
    'couchbase-conn-jump': 'kv_curr_connections{{{sel}}}',
    'couchbase-index-memory': '100 * index_memory_used_total{{{sel}}} / index_memory_quota{{{sel}}}',
    'couchbase-failover': 'increase(cm_auto_failover_count{{{sel}}}[60m])',
    'couchbase-rebalance': 'cm_rebalance_progress{{{sel}}}',
    'couchbase-n1ql-errors': '100 * sum(increase(n1ql_errors{{{sel}}}[60m])) / sum(increase(n1ql_requests{{{sel}}}[60m]))',
    'couchbase-n1ql-timeouts': 'sum(increase(n1ql_timeouts{{{sel}}}[60m]))',
    'couchbase-n1ql-latency': 'sum(increase(n1ql_request_time{{{sel}}}[60m])) / sum(increase(n1ql_requests{{{sel}}}[60m])) / 1e6',
    'couchbase-n1ql-queue': 'max(n1ql_queued_requests{{{sel}}})',
    'couchbase-auth-fail': 'sum(increase(cm_rest_request_auth_failure_total{{{sel}}}[60m]))',
}
TOOL_NOTE = ('one row per node as the last pass saw it: up (1 or 0), uptime in seconds, conn (the peak of client connections in 60 min) with connD1 and connD7 '
             '(the same window 1 and 7 days earlier), index memory used and its quota in bytes, imemPct; summary holds the N1QL numbers of the same 60 min')
DESCRIPTION = ('The Couchbase cluster {field:cluster}, read only, as the last Couchbase pass saw it through Prometheus {field:prometheus}: one row per node, down nodes first, '
               'with up, uptime, the connection peak of the last 60 min against the same window 1 and 7 days earlier, and index memory against its quota, and a summary '
               f'of N1QL queries, errors, timeouts, average time, queue, rebalance and auto-failover. node keeps the nodes whose name holds that text; limit caps the rows (1-{MAX_ROWS}). '
               'Use it on Couchbase cards. At most 4 calls per run.')
SCHEMA = {'type': 'object', 'properties': {'node': {'type': 'string'}, 'limit': {'type': 'integer', 'minimum': 1, 'maximum': MAX_ROWS}}, 'additionalProperties': False}


class NoSeries(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def selector_of(ctx: Context) -> str:
    return (ctx.instance.config.get('selector') or '').strip()


def cluster_of(ctx: Context) -> str:
    return (ctx.instance.config.get('cluster') or '').strip() or ctx.instance.name


def selector_problem(selector: str) -> str | None:
    return None if len(selector) <= SELECTOR_MAX and SELECTOR.fullmatch(selector) else BAD_SELECTOR


def queries(selector: str) -> dict[str, str]:
    return {k: q.format(sel=selector.strip(), w=W) for k, q in QUERIES.items()}


def fetch(client, q: dict[str, str], now: float) -> dict:
    ats = [now] + [now - o * 86400 for o in OFFSETS]
    with concurrent.futures.ThreadPoolExecutor(6) as ex:
        futs = {(k, i): ex.submit(client.vec, expr, t) for k, expr in q.items() for i, t in enumerate(ats)}
        return {k: f.result() for k, f in futs.items()}


def dash(v, unit: str = '') -> str:
    return '-' if v is None else fmt(v, unit)


class Reading:
    def __init__(self, data: dict):
        self.data = data
        self.n = 1 + len(OFFSETS)

    def per_node(self, k: str, i: int) -> dict:
        return {m.get('instance', ''): v for m, v in self.data[(k, i)]}

    def one(self, k: str, i: int):
        got = [v for _, v in self.data[(k, i)]]
        return got[0] if got else None

    def trio(self, k: str) -> list:
        return [self.one(k, i) for i in range(self.n)]


class Cards:
    def __init__(self, instance: str, cluster: str, rules: dict):
        self.instance = instance
        self.cluster = cluster
        self.rules = rules
        self.found: list[dict] = []

    def add(self, subject: str, sig: str, sev: str, now_v, d1, d7, label: str, what: str, slack, short: str, unit: str = '') -> None:
        x = anomaly(ID, f'{ID}-{sig}', ID, subject, '', sev, now_v, d1, d7, unit, label, what, slack, short)
        x.update(key=f'{ID}|{self.instance}|{subject}|{sig}', ranKey=ID)
        self.found.append(x)

    def node(self, node: str, up: list, uptime: dict, conn: list, pct, iquota: dict) -> None:
        rules, cluster = self.rules, self.cluster
        if node not in up[0]:
            self.add(node, 'down', 'warn', 0, up[1].get(node), up[2].get(node), 'not scraped',
                     f'Prometheus had an up sample for Couchbase node {node} in {cluster} a day ago and has none now: its scrape target is gone or the node left the cluster', 1,
                     f'WARN Couchbase {node} not scraped')
            return
        if up[0][node] < 0.5:
            self.add(node, 'down', 'crit', 0, up[1].get(node), up[2].get(node), 'node down',
                     f'Couchbase node {node} in {cluster} is down: up is 0, Prometheus cannot reach its metrics endpoint', 1, f'CRIT Couchbase {node} down (up 0)')
            return
        u = uptime.get(node)
        if u is not None and u < rules['uptime_s']:
            self.add(node, 'restart', 'warn', u, None, None, 'node restarted',
                     f'Couchbase node {node} in {cluster} has been up for only {duration(u)}: its data service restarted in the last hour', 1,
                     f'WARN Couchbase {node} restarted {duration(u)} ago', 'dur')
        c, d1, d7 = conn[0].get(node), conn[1].get(node), conn[2].get(node)
        bases = [b for b in (d1, d7) if b is not None]
        if c is not None and bases and c > max(rules['jump'] * max(bases), max(bases) + rules['jump_slack']):
            self.add(node, 'conn-jump', 'warn', c, d1, d7, 'connection jump',
                     f'Couchbase node {node} peaked at {fmt(c)} client connections in the last 60 min; the same window had {dash(d1)} a day ago and {dash(d7)} a week ago',
                     rules['jump_slack'], f'WARN Couchbase {node} connections jumped to {fmt(c)} (1d {dash(d1)} / 7d {dash(d7)})')
        if pct is not None and pct >= rules['imem']:
            self.add(node, 'index-memory', 'info', pct, None, None, 'index memory used',
                     f'the index service on {node} uses {fmt(pct, "%")} of its {iquota[node] / 2 ** 30:.1f} GiB memory quota; this stays info, because a full index quota slows '
                     'index builds and scans before it fails them', 5, f'INFO Couchbase {node} index memory {fmt(pct, "%")} of quota', '%')

    def state(self, failover, stuck, rebalance) -> None:
        cluster = self.cluster
        if failover:
            self.add(cluster, 'failover', 'crit', failover, None, None, 'auto-failover',
                     f'Couchbase cluster {cluster} failed over a node automatically in the last 60 min: a node stopped answering and its data moved to the replicas', 1,
                     f'CRIT Couchbase {cluster} auto-failover')
        if stuck:
            self.add(cluster, 'rebalance', 'crit', stuck, None, None, 'rebalance stuck',
                     f'Couchbase reports a stuck rebalance in {cluster}: moving data between the nodes stopped making progress', 1, f'CRIT Couchbase {cluster} rebalance stuck')
        elif rebalance:
            self.add(cluster, 'rebalance', 'info', rebalance, None, None, 'rebalance running',
                     f'a rebalance is running in Couchbase cluster {cluster}; expect slower queries until it finishes', 1, f'INFO Couchbase {cluster} rebalance running')

    def errors(self, req: list, err: list, share: list) -> None:
        rules, cluster = self.rules, self.cluster
        base = [b for b in share[1:] if b is not None]
        limit = max(rules['err_x'] * max(base), max(base) + rules['err_pp']) if base else rules['err_share']
        if share[0] is None or (err[0] or 0) < rules['err_min'] or share[0] <= limit:
            return
        sev = 'crit' if share[0] >= rules['err_crit'] else 'warn'
        self.add(cluster, 'n1ql-errors', sev, share[0], share[1], share[2], 'N1QL error share',
                 f'{fmt(err[0])} of {fmt(req[0])} N1QL queries on {cluster} failed in the last 60 min ({fmt(share[0], "%")}), against {dash(share[1], "%")} a day ago and '
                 f'{dash(share[2], "%")} a week ago', rules['err_pp'], f'{sev.upper()} Couchbase N1QL errors {fmt(share[0], "%")} ({fmt(err[0])} of {fmt(req[0])})', '%')

    def timeouts(self, tmo: list) -> None:
        rules, cluster = self.rules, self.cluster
        base = [b for b in tmo[1:] if b is not None]
        if (tmo[0] or 0) < rules['tmo_min'] or (base and tmo[0] <= rules['tmo_x'] * max(base)):
            return
        self.add(cluster, 'n1ql-timeouts', 'warn', tmo[0], tmo[1], tmo[2], 'N1QL timeouts',
                 f'{fmt(tmo[0])} N1QL queries on {cluster} timed out in the last 60 min, against {dash(tmo[1])} a day ago and {dash(tmo[2])} a week ago; '
                 'each one is an error for its caller', rules['tmo_min'], f'WARN Couchbase N1QL timeouts {fmt(tmo[0])} in 60 min (1d {dash(tmo[1])} / 7d {dash(tmo[2])})')

    def latency(self, req: list, avg: list) -> None:
        rules, cluster = self.rules, self.cluster
        base = [b for b in avg[1:] if b is not None]
        limit = max(rules['lat_x'] * max(base), max(base) + rules['lat_ms']) if base else rules['lat_floor']
        if avg[0] is None or (req[0] or 0) < rules['lat_req'] or avg[0] <= limit:
            return
        self.add(cluster, 'n1ql-latency', 'warn', avg[0], avg[1], avg[2], 'N1QL average',
                 f'N1QL queries on {cluster} averaged {fmt(avg[0], "ms")} over {fmt(req[0])} queries in the last 60 min, against {dash(avg[1], "ms")} a day ago and '
                 f'{dash(avg[2], "ms")} a week ago', rules['lat_ms'], f'WARN Couchbase N1QL {fmt(avg[0], "ms")} avg (1d {dash(avg[1], "ms")} / 7d {dash(avg[2], "ms")})', 'ms')

    def load(self, queue, auth) -> None:
        rules, cluster = self.rules, self.cluster
        if (queue or 0) > rules['queue']:
            self.add(cluster, 'n1ql-queue', 'warn', queue, None, None, 'N1QL queries queued',
                     f'up to {fmt(queue)} N1QL queries waited in the query service queue on {cluster} in the last 60 min: the query service is short of workers',
                     rules['queue'], f'WARN Couchbase {fmt(queue)} N1QL queries queued')
        if (auth or 0) >= rules['auth']:
            self.add(cluster, 'auth-fail', 'info', auth, None, None, 'REST auth failures',
                     f'{fmt(auth)} Couchbase REST requests failed authentication on {cluster} in the last 60 min: a client with a wrong or rotated password, '
                     'or someone probing the admin port', rules['auth'], f'INFO Couchbase {fmt(auth)} REST auth failures')


def evaluate(data: dict, instance: str, cluster: str, rules: dict = RULES) -> tuple[list[dict], list[dict], dict]:
    rd = Reading(data)
    up = [rd.per_node('up', i) for i in range(rd.n)]
    if not up[0]:
        raise NoSeries(f'Prometheus holds no Couchbase up series for {cluster}')
    uptime, conn, imem, iquota = rd.per_node('uptime', 0), [rd.per_node('conn', i) for i in range(rd.n)], rd.per_node('imem', 0), rd.per_node('iquota', 0)
    req, err, tmo, rtime = rd.trio('req'), rd.trio('errors'), rd.trio('timeouts'), rd.trio('rtime')
    queue, failover, stuck, rebalance, auth = rd.one('queue', 0), rd.one('failover', 0), rd.one('stuck', 0), rd.one('rebalance', 0), rd.one('auth', 0)
    avg = [None if not req[i] or rtime[i] is None else round(rtime[i] / req[i] / 1e6, 2) for i in range(rd.n)]
    share = [None if not req[i] or err[i] is None else round(100 * err[i] / req[i], 3) for i in range(rd.n)]
    cards, rows = Cards(instance, cluster, rules), []
    for node in sorted(set(up[0]) | set(up[1])):
        pct = None if not iquota.get(node) or imem.get(node) is None else round(100 * imem[node] / iquota[node], 2)
        rows.append(dict(part='node', name=cluster, node=node, up=up[0].get(node), uptime=uptime.get(node), conn=conn[0].get(node), connD1=conn[1].get(node),
                         connD7=conn[2].get(node), imem=imem.get(node), iquota=iquota.get(node), imemPct=pct))
        cards.node(node, up, uptime, conn, pct, iquota)
    cards.state(failover, stuck, rebalance)
    cards.errors(req, err, share)
    cards.timeouts(tmo)
    cards.latency(req, avg)
    cards.load(queue, auth)
    summary = dict(req=req[0], reqD1=req[1], reqD7=req[2], errors=err[0], errorsD1=err[1], errorsD7=err[2], share=share[0], timeouts=tmo[0], timeoutsD1=tmo[1],
                   timeoutsD7=tmo[2], avgMs=avg[0], avgD1=avg[1], avgD7=avg[2], queue=queue, failover=failover, rebalance=rebalance, stuck=stuck, auth=auth)
    return cards.found, rows, summary


def card_urls(card: dict, ui: str, selector: str, cluster: str) -> list[dict]:
    if card.get('sig') not in LINK or not ui.startswith('https://'):
        return []
    node = card.get('svc', '')
    sel = selector if node == cluster else f'{selector},instance={json.dumps(node)}'
    return [dict(label='Prometheus graph', url=prom_link(LINK[card['sig']].format(sel=sel), ui))]


def refused(e: PrometheusError, config) -> Sweep:
    card = access.card(ID, NAME, PART, status=e.code, credential=credential(config), name=PART_NAME)
    return Sweep(PassResult(ID, True, access.status('ok', [PART_NAME]), [card], {}, (access.ran_key(ID, PART),)))


def run(ctx: Context) -> Sweep:
    selector, cluster = selector_of(ctx), cluster_of(ctx)
    bad = selector_problem(selector)
    if bad:
        return Sweep(PassResult(ID, False, f'error: {bad}'))
    peer = ctx.peer('prometheus')
    if peer is None:
        return Sweep(PassResult(ID, False, f'error: {NO_PEER}'))
    rules = {**RULES, **ctx.rules('rules')}
    try:
        data = fetch(client_for(peer.info, bounded(peer.http(), BUDGET_S)), queries(selector), ctx.now)
        found, rows, summary = evaluate(data, ctx.instance.name, cluster, rules)
    except Late:
        return Sweep(PassResult(ID, False, 'error: timed out'))
    except NoSeries as e:
        return Sweep(PassResult(ID, False, f'error: {e.reason}'))
    except PrometheusError as e:
        if access.denied(e.code):
            return refused(e, peer.info.config)
        return Sweep(PassResult(ID, False, f'error: {e.reason}'))
    ui = ui_of(peer.info.config)
    for card in found:
        card['urls'] = card_urls(card, ui, selector, cluster)
    head = {'part': 'summary', 'name': cluster, **summary, 'selector': selector, 'at': iso(ctx.now), 'source': peer.info.name, 'ui': ui}
    return Sweep(PassResult(ID, True, 'ok', found, {}, (ID, access.ran_key(ID, PART))), sections={ID: rows + [head]})


def test(ctx: Context) -> TestResult:
    def ask() -> str:
        selector, cluster = selector_of(ctx), cluster_of(ctx)
        bad = selector_problem(selector)
        if bad:
            raise ReadError(400, bad)
        peer = ctx.peer('prometheus')
        if peer is None:
            raise ReadError(400, NO_PEER)
        try:
            found = instant(peer, queries(selector)['up'])
        except HttpError as e:
            said = access.tested(NAME, PART_NAME, e.code, credential=credential(peer.info.config))
            if said:
                raise ReadError(e.code, said) from None
            raise
        if not found:
            return f'Prometheus {peer.info.name} answered, but it holds no Couchbase up series for {cluster}'
        up = sum(1 for r in found if number(r) >= 0.5)
        return f'{up} of {len(found)} Couchbase nodes of {cluster} up through Prometheus {peer.info.name}'

    return connection.result(ask)


def wanted(args: dict) -> tuple[str, int]:
    raw = args.get('limit')
    try:
        limit = MAX_ROWS if raw is None else int(raw)
    except (TypeError, ValueError):
        raise ReadError(400, f'limit must be 1-{MAX_ROWS}') from None
    if not 1 <= limit <= MAX_ROWS:
        raise ReadError(400, f'limit must be 1-{MAX_ROWS}')
    return str(args.get('node') or '').strip(), limit


def nodes(ctx: Context, args: dict) -> dict:
    want, limit = wanted(args)
    rows = [r for r in ctx.previous(ID) or [] if isinstance(r, dict)]
    if not rows:
        raise ReadError(404, 'no Couchbase pass has written a node table for this instance yet')
    head = next((r for r in rows if r.get('part') == 'summary'), {})
    found = [r for r in rows if r.get('part') == 'node' and want in str(r.get('node') or '')]
    found.sort(key=lambda r: (isinstance(r.get('up'), (int, float)) and r['up'] >= 0.5, str(r.get('node') or '')))
    kept = [{k: r.get(k) for k in NODE_KEYS} for r in found[:limit]]
    more = f'first {len(kept)} of {len(found)} nodes, down nodes first; ' if len(kept) < len(found) else ''
    return {'cluster': head.get('name'), 'at': head.get('at'), 'nodes': kept, 'summary': {k: head.get(k) for k in SUMMARY_KEYS}, 'note': more + TOOL_NOTE}


def links(card: dict, info: InstanceInfo) -> list[dict] | None:
    if card.get('src') != ID:
        return None
    return [{'label': u['label'], 'url': u['url']} for u in card.get('urls') or []
            if isinstance(u, dict) and isinstance(u.get('label'), str) and isinstance(u.get('url'), str) and u['url'].startswith('https://')]


CARD_TYPES = (CardType('couchbase-down', 'Couchbase node down'), CardType('couchbase-restart', 'Couchbase restart'), CardType('couchbase-conn-jump', 'Couchbase connections'),
              CardType('couchbase-index-memory', 'Couchbase index memory'), CardType('couchbase-failover', 'Couchbase failover'),
              CardType('couchbase-rebalance', 'Couchbase rebalance'), CardType('couchbase-n1ql-errors', 'N1QL errors'), CardType('couchbase-n1ql-timeouts', 'N1QL timeouts'),
              CardType('couchbase-n1ql-latency', 'N1QL latency'), CardType('couchbase-n1ql-queue', 'N1QL queue'), CardType('couchbase-auth-fail', 'Couchbase auth failures'))

PLUGIN = Plugin(
    id=ID, name=NAME, version='0.1.0', category='infrastructure',
    description='Example plug-in: a Couchbase cluster read through a Prometheus instance: nodes, connections, index memory, rebalance, failover and N1QL errors, timeouts and time.',
    icon='icon.svg', author='Skopax', licence='Apache-2.0', core='>=0.1,<0.2', uses=('prometheus',),
    fields=(Field('prometheus', 'Prometheus', 'instance', required=True, choices=('prometheus',), help='The Prometheus instance that scrapes the Couchbase exporters.'),
            Field('cluster', 'Cluster', 'text', required=True, help='The name the cards and the page show for this cluster, such as orders-prod.'),
            Field('selector', 'Selector', 'text', default=DEFAULT_SELECTOR,
                  help='The label matchers that pick this cluster in every query, such as job=~".*couchbase.*",env="prod". Each one is label, operator, quoted value.')),
    test=test,
    checks=(Check(ID, 'Couchbase cluster', 900, 240, (ID,), run, tool=TOOL, sections=(ID,), section_help=SECTION_HELP),),
    rules=(RuleSet('rules', 'Couchbase thresholds', dict(RULES), RULE_HELP),),
    tools=(Tool(TOOL, DESCRIPTION, SCHEMA, nodes, per_run=4),),
    links=links,
    card_types=CARD_TYPES,
    panels=(Panel(ID, 'panel.js', label='Couchbase', group='observe'),),
)
