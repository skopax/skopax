import re
from pathlib import Path

from skopax.contract import (Capabilities, Check, Context, Field, HttpError, InstanceInfo, Knowledge, Panel, PassResult, Plugin, ReadError, RuleSet, Sweep, TestResult, Tool,
                             Unreachable)
from skopax.kit import access, connection
from skopax.kit.promql import ranged
from skopax.plugins.pmm.checks import (MONGO_RULE, PMM_RULE, QAN_ROWS, QAN_RULE, QAN_TOTALS, dashboard, evaluate_mongo, evaluate_pmm, evaluate_qan, links_for, mongo_pass,
                                       pmm_pass, qan_key, qan_pass, unsupported, users)
from skopax.plugins.pmm.client import KINDS, PERMISSIONS, SLUGS, Pmm, PmmError, client_for, lacks, pick

ID = 'pmm'
NO_DATA = 'ok \u00b7 no data'
CAPPED = f'ok \u00b7 read {QAN_ROWS} queries, more not read'
REFUSED = (401, 403)
ACCESS_PARTS = {'pmm': ('data sources', 'metrics'), 'qan': ('data sources', 'QAN'), 'mongo': ('data sources', 'metrics')}
NS = {'pmm': 'postgres', 'qan': 'postgres', 'mongo': 'mongodb'}
PATTERNS = {'postgres-services': 'Postgres services', 'mongo-services': 'MongoDB services'}
RANGE_MINUTES = 1440
TEST_BUDGET_S = 12
NEEDS = {'pmm': 'metrics', 'qan': 'qan', 'mongo': 'metrics'}
QAN_HOURS = 168
SELECT_SQL = re.compile(r'\s*(select|with)\b', re.I)
QAN_FUNCTIONS = ('format', 'input', 'executable', 'dictionary', 'generateRandom', 'loop', 'merge')
QAN_FAMILIES = ('url', 'remote', 'cluster', 's3', 'gcs', 'oss', 'cosn', 'azure', 'file', 'hdfs', 'mysql', 'postgresql', 'mongodb', 'redis', 'sqlite', 'odbc', 'jdbc',
                'hive', 'iceberg', 'deltaLake', 'hudi', 'paimon', 'arrowflight', 'ytsaurus')
QAN_DENY = re.compile(r'\b(?:' + '|'.join(QAN_FUNCTIONS) + r')\s*\(|\b(?:' + '|'.join(QAN_FAMILIES) + r')\w*\s*\(|\b(?:from|join)\s+[\w.]+\s*\('
                      r'|\b(?:system|information_schema)\s*\.|\binto\s+outfile\b|\bsettings\b|--|/\*|#', re.I)
NOT_SELECT = 'qan takes one ClickHouse SELECT (or WITH \u2026 SELECT) without semicolons'
NOT_METRICS = 'qan reads only pmm.metrics: no table functions, system tables, SETTINGS, INTO OUTFILE or comments'
SECTION_HELP = {
    'pmm': 'one row per watched Postgres service: up, connections against max_connections, idle in transaction, longest transaction, replication lag, deadlocks, lock waits',
    'qan': 'the Postgres queries with the most DB time in the last 60 minutes, from PMM Query Analytics, with their calls and averages a day and a week earlier',
    'mongo': 'one row per watched MongoDB member: up, replica set role, connections, replication lag, operations, read, write and command averages, lock queue',
}
PG_HELP = {
    'sat_warn': 'Warning when a server peaks at this share of max_connections in the last 60 minutes (70 = 70 %).',
    'sat_crit': 'Critical at this share of max_connections.',
    'jump': 'Warning when the peak connections pass this many times the higher of the same window 1 day and 7 days earlier.',
    'jump_slack': 'And at least this many connections more than that earlier window.',
    'idletx': 'Warning when more than this many connections sit idle in transaction.',
    'longtx': 'Warning when a transaction has been open for more than this many seconds and more than twice, and this much above, its earlier level.',
    'lag': 'Warning when a replica stays more than this many seconds behind for 10 minutes, and this much above its earlier level.',
    'lock': 'Warning when at least this many sessions wait on locks at once.',
    'lock_x': 'And more than this many times the earlier peak of waiting sessions.',
    'lock_slack': 'And at least this many sessions more than the earlier peak.',
}
QAN_HELP = {
    'load': 'Warning when one query uses at least this many seconds of DB time in 60 minutes and more than twice, and this much above, its earlier hours.',
    'load_crit': 'Critical when that query uses at least this many seconds of DB time.',
    'slow_ms': 'Warning when a query average reaches this many milliseconds and passes twice, and this much above, its earlier average.',
    'slow_calls': 'The average is judged only for a query with at least this many calls in the hour.',
    'slow_min_s': 'And only when the query used at least this many seconds of DB time in the hour.',
    'calls': 'Info when a query runs at least this many times in the hour and three times, and this much above, its earlier hours.',
    'total': 'Warning when a server spends this many seconds of query time more than twice its earlier hours.',
    'top': 'How many queries the Postgres page lists, the ones with the most DB time first.',
    'rows_min_s': 'A query is read from Query Analytics only when one of the three hours used at least this many seconds of DB time.',
    'rows_min_calls': 'Or when the last hour had at least this many calls.',
    'stall_x': 'Warning when the worst one-minute average of a query is this many times its usual average.',
    'stall_ms': 'And that minute averaged at least this many milliseconds.',
    'stall_s': 'And that minute burned at least this many seconds of DB time.',
}
MONGO_HELP = {
    'sat_warn': 'Warning when a member peaks at this share of the connections it accepts (70 = 70 %).',
    'sat_crit': 'Critical at this share of the connections it accepts.',
    'jump': 'Warning when the peak connections pass this many times the higher of the same window 1 day and 7 days earlier.',
    'jump_slack': 'And at least this many connections more than that earlier window.',
    'lag': 'Warning when a member stays more than this many seconds behind the primary for 10 minutes, and this much above its earlier level.',
    'lat_x': 'Warning when the read, write or command average passes this many times its earlier average.',
    'lat_ms': 'And at least this many milliseconds above that earlier average.',
    'lat_ops': 'Averages are judged only with at least this many operations of that kind in the hour.',
    'queue': 'Warning when more than this many operations wait in the global lock queue, and this many more than before.',
}
PMM_QUERY = ('PromQL on the PMM metrics data source (Postgres pg_* and MongoDB mongodb_* series). Instant query at `at` (unix seconds, default now): each row is the '
             f"metric's labels and its value. With range_minutes (1-{RANGE_MINUTES}), a range query over the minutes up to `at` with about 200 points: "
             'each row is the labels and their [unix seconds, value] pairs.')
QAN = 'ClickHouse SELECT on PMM Query Analytics over the last `hours`: table pmm.metrics only, no table functions, system tables, SETTINGS or comments.'
PMM_QUERY_SCHEMA = {'type': 'object', 'properties': {'expr': {'type': 'string'}, 'at': {'type': 'number'}, 'range_minutes': {'type': 'integer', 'minimum': 1, 'maximum': RANGE_MINUTES}},
                    'required': ['expr'], 'additionalProperties': False}
QAN_SCHEMA = {'type': 'object', 'properties': {'sql': {'type': 'string'}, 'hours': {'type': 'number', 'minimum': 0.25, 'maximum': QAN_HOURS}},
              'required': ['sql', 'hours'], 'additionalProperties': False}


def _url(info: InstanceInfo) -> str:
    return (info.config.get('url') or '').rstrip('/')


def _shown(found: dict | None, pinned: str) -> str:
    if found is not None:
        return f"{found.get('name') or found['uid']} ({found['uid']})"
    return f'{pinned} (pinned)' if pinned else 'none found'


def _pattern(info: InstanceInfo, key: str) -> tuple[str, str | None]:
    pattern = info.config.get(key) or '.*'
    return pattern, unsupported(pattern, PATTERNS[key])


def _answers(client, kind: str, uid: str, now: float) -> None:
    try:
        if kind == 'metrics':
            client.vec('vector(1)', now)
        else:
            client.qan('SELECT 1', 0.25, now)
    except PmmError as e:
        if not e.answered or e.denied or e.code in REFUSED:
            raise
        raise ReadError(400, f'the {KINDS[kind][2]} data source {uid} does not answer ({e.reason})') from None


def _tested(e: PmmError) -> ReadError:
    if e.code == 401 and isinstance(e.cause, HttpError):
        return ReadError(401, connection.failure(e.cause))
    if e.refused():
        return ReadError(403, f'PMM cannot read {e.part}: {lacks(e.part)}')
    if isinstance(e.cause, (HttpError, Unreachable)):
        return ReadError(e.status, connection.failure(e.cause))
    return e


def test(ctx: Context) -> TestResult:
    info = ctx.instance

    def ask() -> str:
        refused = next((why for why in (_pattern(info, key)[1] for key in PATTERNS) if why), None)
        if refused:
            raise ReadError(400, refused)
        client = client_for(info, ctx.http(), TEST_BUDGET_S)
        version = client.version()
        wanted = {NEEDS[check.id] for check in PLUGIN.checks if check.id not in info.off_checks}
        pinned = {kind: (info.config.get(KINDS[kind][0]) or '').strip() for kind in KINDS}
        sources = None if all(pinned[kind] for kind in wanted) else client.datasources()
        parts = []
        for kind in [k for k in ('metrics', 'qan') if k in wanted]:
            _, types, label = KINDS[kind]
            found = pick(sources or [], pinned[kind], types)
            uid = pinned[kind] or (found['uid'] if found else '')
            if uid:
                client.use(kind, uid)
                _answers(client, kind, uid, ctx.now)
            parts.append(f'{label}: {_shown(found, pinned[kind])}')
        return ' \u00b7 '.join([f"PMM {version or 'unknown version'}", *parts])

    def checked() -> str:
        try:
            return ask()
        except PmmError as e:
            raise _tested(e) from None

    return connection.result(checked)


def _linked(rows: list[dict], info: InstanceInfo, which: str, params) -> list[dict]:
    url = _url(info)
    if not url.startswith('https://'):
        return rows
    return [dict(r, link=dashboard(url, which, params(r))) for r in rows]


def access_keys(check: str, client: Pmm, refused: str = '') -> tuple[str, ...]:
    def seen(part: str) -> bool:
        return part == refused or part in client.reached or (part == 'data sources' and client.pinned(NEEDS[check]))

    return tuple(f'{check}:access:{SLUGS[part]}' for part in ACCESS_PARTS[check] if seen(part))


def access_card(check: str, part: str, why: str, status: int) -> dict:
    what = (f'PMM refused to read the {part} ({why}). Until the token has {PERMISSIONS[part]}, '
            f'the {check} check reads nothing, and its cards stay as they are and are not checked again.')
    return access.card(check, 'PMM', SLUGS[part], PERMISSIONS[part], status, name=part, what=what)


def _passed(check: str, status: str, found: list[dict], ran: tuple[str, ...], section: str, rows: list[dict], client: Pmm) -> Sweep:
    return Sweep(PassResult(check, True, status, found, {}, ran + access_keys(check, client)), sections={section: rows})


def _failed(check: str, section: str, e: PmmError, client: Pmm) -> Sweep:
    if e.refused():
        status = e.code if e.code in REFUSED else access.FORBIDDEN
        return Sweep(PassResult(check, True, access.status('ok', [e.part]), [access_card(check, e.part, e.reason, status)], {}, access_keys(check, client, e.part)),
                     sections={section: []})
    return Sweep(PassResult(check, False, f'error: {e.reason}'))


def _quiet(check: str, section: str, client: Pmm) -> Sweep:
    return _passed(check, NO_DATA, [], (f'{check}:no-data',), section, [], client)


def _gone(ctx: Context, read: set[str]) -> list[str]:
    info = ctx.instance
    tail = '' if info is None or info.primary else f'-{info.name}'
    out = []
    for card in ctx.open_cards():
        key = card.get('ranKey')
        if not isinstance(key, str) or not key.endswith(tail):
            continue
        key = key[:len(key) - len(tail)] if tail else key
        if key.startswith('qan:') and not key.startswith('qan:access:') and key not in (QAN_TOTALS, 'qan:no-data') and key not in read:
            out.append(key)
    return out


def run_pmm(ctx: Context) -> Sweep:
    info = ctx.instance
    services, refused = _pattern(info, 'postgres-services')
    if refused:
        return Sweep(PassResult('pmm', False, f'error: {refused}'))
    client = client_for(info, ctx.http())
    try:
        got = evaluate_pmm(pmm_pass(client, ctx.now, services), ctx.rules('postgres'), services)
    except PmmError as e:
        return _failed('pmm', 'pmm', e, client)
    if got is None:
        return _quiet('pmm', 'pmm', client)
    found, rows = got
    return _passed('pmm', 'ok', found, ('pmm',), 'pmm', _linked(rows, info, 'pg', lambda r: {'var-service_name': r['svc']}), client)


def run_qan(ctx: Context) -> Sweep:
    info = ctx.instance
    services, refused = _pattern(info, 'postgres-services')
    if refused:
        return Sweep(PassResult('qan', False, f'error: {refused}'))
    rules = ctx.rules('qan')
    client = client_for(info, ctx.http())
    try:
        data = qan_pass(client, ctx.now, services, users(info.config.get('exclude-users') or ''), rules)
        got = evaluate_qan(data, rules, services)
    except PmmError as e:
        return _failed('qan', 'qan', e, client)
    if got is None:
        return _quiet('qan', 'qan', client)
    found, top = got

    def params(r: dict) -> dict:
        return {'var-service_name': r['svc'], **({'filter_by': r['queryid'], 'query_selected': 'true'} if r['queryid'] else {})}

    capped = len(data[0]) >= QAN_ROWS
    read = {qan_key(r.get('service_name'), r.get('queryid')) for r in data[0] if r.get('queryid')}
    ran = (QAN_TOTALS, *sorted(read)) + (() if capped else ('qan', *_gone(ctx, read)))
    return _passed('qan', CAPPED if capped else 'ok', found, ran, 'qan', _linked(top, info, 'qan', params), client)


def run_mongo(ctx: Context) -> Sweep:
    info = ctx.instance
    services, refused = _pattern(info, 'mongo-services')
    if refused:
        return Sweep(PassResult('mongo', False, f'error: {refused}'))
    client = client_for(info, ctx.http())
    try:
        got = evaluate_mongo(mongo_pass(client, ctx.now, services), ctx.rules('mongo'), services, (info.config.get('replica-set') or '').strip())
    except PmmError as e:
        return _failed('mongo', 'mongo', e, client)
    if got is None:
        return _quiet('mongo', 'mongo', client)
    found, rows = got
    return _passed('mongo', 'ok', found, ('mongo',), 'mongo', _linked(rows, info, 'mongo', lambda r: {'var-service_name': r['svc']}), client)


def _tool_error(client, e: PmmError) -> ReadError:
    if e.refused():
        return ReadError(e.status, f'{e.shown(client.token)}; {lacks(e.part, e.code == 401)}')
    return ReadError(502, e.shown(client.token))


def pmm_query(ctx: Context, args: dict) -> object:
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
            return ranged(client, expr, at, min(minutes, RANGE_MINUTES))
        return [{'metric': m, 'value': v} for m, v in client.vec(expr, at)]
    except PmmError as e:
        raise _tool_error(client, e) from None


def qan(ctx: Context, args: dict) -> object:
    sql = args.get('sql')
    if not isinstance(sql, str) or not SELECT_SQL.match(sql) or ';' in sql:
        raise ReadError(400, NOT_SELECT)
    if QAN_DENY.search(re.sub(r'[`"]', ' ', sql)):
        raise ReadError(400, NOT_METRICS)
    try:
        hours = min(float(args.get('hours')), QAN_HOURS)
    except (TypeError, ValueError, OverflowError):
        raise ReadError(400, 'hours must be a number') from None
    client = client_for(ctx.instance, ctx.http())
    try:
        return client.qan(sql, hours, ctx.now)
    except PmmError as e:
        raise _tool_error(client, e) from None


def links(card: dict, info: InstanceInfo) -> list[dict] | None:
    return links_for(card, _url(info))


def probe_targets(info: InstanceInfo) -> list[dict]:
    return [{'name': f'pmm-{info.name}', 'url': _url(info) + '/v1/readyz'}]


PLUGIN = Plugin(
    id=ID, name='PMM (Postgres, MongoDB)', version='0.1.0', category='infrastructure',
    description='Postgres servers, slow queries and MongoDB members read from Percona Monitoring and Management.', icon='icon.svg',
    author='Skopax', licence='Apache-2.0', core='>=0.1,<0.2', edition='community', capabilities=Capabilities(hosts=('@url',), secrets=('token',)),
    fields=(Field('url', 'URL', 'url', required=True, ui=True, help='Address of the PMM server, also used for dashboard links.'),
            Field('token', 'Token', 'secret', required=True, help='A PMM service-account token that can query the data sources; finding them when a uid is empty also needs the right to list data sources.'),
            Field('ca', 'CA certificate', 'pem', help='PEM certificate to trust instead of the system roots.'),
            Field('ca-pinned', 'Trust this certificate as it is', 'bool', default='false', show_if=('ca', '*'), help="For a server whose own certificate fails the usual checks: paste the server's own certificate; with the switch on, only that exact certificate is accepted, even if it is expired, self-signed or names another host. A CA certificate does not work with the switch on."),
            Field('metrics-datasource', 'Metrics data source', 'text', help='Uid of the metrics data source. Empty: the first Prometheus data source PMM lists.'),
            Field('qan-datasource', 'QAN data source', 'text', help='Uid of the Query Analytics data source. Empty: the first ClickHouse data source PMM lists.'),
            Field('postgres-services', 'Postgres services', 'regex', default='.*', help='Only Postgres service names that match this pattern are watched. RE2 syntax: no lookarounds or backreferences.'),
            Field('mongo-services', 'MongoDB services', 'regex', default='.*', help='Only MongoDB service names that match this pattern are watched. RE2 syntax: no lookarounds or backreferences.'),
            Field('replica-set', 'Replica set', 'text', help='Name of the MongoDB replica set on its cards. Empty: the set label of the series.'),
            Field('exclude-users', 'Users Query Analytics ignores', 'text', default='pmm', help='Comma-separated database users whose queries are left out.')),
    test=test,
    checks=(Check('pmm', 'Postgres servers', 900, 360, ('pmm',), run_pmm, tool='pmm_query', sections=('pmm',), section_help={'pmm': SECTION_HELP['pmm']}),
            Check('qan', 'Postgres queries', 900, 360, ('qan',), run_qan, tool='qan', sections=('qan',), section_help={'qan': SECTION_HELP['qan']}),
            Check('mongo', 'MongoDB members', 900, 360, ('mongo',), run_mongo, tool='pmm_query', sections=('mongo',), section_help={'mongo': SECTION_HELP['mongo']})),
    rules=(RuleSet('postgres', 'PMM Postgres', dict(PMM_RULE), PG_HELP),
           RuleSet('qan', 'PMM Query Analytics', dict(QAN_RULE), QAN_HELP),
           RuleSet('mongo', 'PMM MongoDB', dict(MONGO_RULE), MONGO_HELP)),
    tools=(Tool('pmm_query', PMM_QUERY, PMM_QUERY_SCHEMA, pmm_query), Tool('qan', QAN, QAN_SCHEMA, qan)),
    links=links, probe_targets=probe_targets,
    panels=(Panel('pg', 'panel.js', label='Postgres', group='observe'), Panel('mongo', 'panel.js', label='MongoDB', group='observe')),
    knowledge=(Knowledge('pmm', 'PMM', (Path(__file__).parent / 'pmm.md').read_text(encoding='utf-8'), slack=True),),
)
