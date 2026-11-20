from collections.abc import Mapping
from pathlib import Path

from skopax.contract import AgentRead, Capabilities, Check, Context, Field, HttpError, InstanceInfo, Knowledge, Panel, Plugin, ReadError, RuleSet, SourceDown, TestResult, Tool, Transport, call, Setup
from skopax.kit import connection
from skopax.kit.elastic import TEST_TIMEOUT, Budget, Elastic, ElasticError, client_for, cluster_of, index_of, indices_of, namespace_problem, native, no_read, quoted
from skopax.messages import msg
from skopax.plugins.elasticsearch.checks import APM_HELP, APM_RULES, LOG_HELP, MASKED, links, literal, mask_of, mask_problem, p95_overrides, row_links, run_apm, run_logs
from skopax.plugins.elasticsearch.esql import AGENT_FIELDS, DESCRIPTION, KEYS, SCHEMA, TIMEOUT_S, agent_esql, agent_sources, run_esql
from skopax.plugins.elasticsearch.logflow import HELP as FLOW_HELP, RULES as FLOW_RULES, SECTION_HELP as FLOW_SECTION_HELP, run_logflow

ID = 'elasticsearch'
NO_MONITOR = 'Elasticsearch (no version without the monitor privilege)'
TEST_SPENT = 'Test connection ran out of its 15 s'


def client(info: InstanceInfo, transport: Transport) -> Elastic:
    return client_for(info, transport)


def version(transport: Transport, url: str, key: str, budget: Budget) -> str:
    try:
        body = call(transport, 'GET', url.rstrip('/') + '/', Elastic(url, key, transport).headers(), timeout=budget.left(TEST_TIMEOUT)).body
    except HttpError as e:
        if e.code == 401 and not key:
            raise ReadError(401, msg('connection.no_credentials', status=401)) from None
        if e.code != 403:
            raise
        return NO_MONITOR
    found = body.get('version') if isinstance(body, dict) else None
    number = found.get('number') if isinstance(found, dict) else None
    return f"Elasticsearch {number if isinstance(number, str) and number else 'unknown version'}"


def readable(client: Elastic, index: str, who: str, budget: Budget) -> str | None:
    try:
        shards = client.shards(index, budget.left(TEST_TIMEOUT))
    except ElasticError as e:
        if e.denied and e.status != 401:
            raise SourceDown(no_read(index, who)) from None
        if not e.missing:
            raise native(e)
        shards = 0
    if shards:
        return None
    try:
        hidden = client.unreadable(indices_of(index), budget.left(TEST_TIMEOUT))
    except ElasticError as e:
        if not e.denied:
            raise
        hidden = []
    if hidden:
        raise SourceDown(no_read(index, who))
    return f'no index matches {index} yet'


def error_field(client: Elastic, config: Mapping[str, str], budget: Budget) -> str | None:
    error = str(config.get('error-field') or '').strip()
    if not error:
        return None
    try:
        client.query(f"FROM {index_of(config, 'logs')} | KEEP {quoted(error)} | LIMIT 1", timeout=budget.left(TEST_TIMEOUT))
    except ElasticError as e:
        if error in e.unknown:
            return f'error-field {error} is not mapped, so the logs check reads the message'
        if not e.missing and e.status != 404:
            return f'error-field {error} was not checked ({e.reason})'
    return None


def mask_accepted(client: Elastic, config: Mapping[str, str], budget: Budget) -> None:
    problem = mask_problem(config)
    if problem:
        raise SourceDown(problem)
    mask = mask_of(config)
    if not mask:
        return
    try:
        client.query(f'ROW m = "x" | EVAL r = REPLACE(m, {literal(mask)}, "{MASKED}") | KEEP r', timeout=budget.left(TEST_TIMEOUT))
    except ElasticError as e:
        if e.denied or e.missing or e.status in (401, 404):
            return
        raise SourceDown(f"Elasticsearch does not accept message-mask as a REPLACE pattern{f' ({e.code})' if e.code else ''}; check the pattern in ES|QL") from None


def option_notes(config: Mapping[str, str]) -> list[str]:
    bad = p95_overrides(config)[1]
    return [f"apm-p95-overrides ignores {', '.join(bad)}"] if bad else []


def namespace_field(client: Elastic, config: Mapping[str, str], budget: Budget) -> None:
    field = str(config.get('namespace-field') or '').strip()
    if str(config.get('index-namespace') or '').strip() or not field:
        return
    index = index_of(config, 'logflow')
    try:
        client.query(f'FROM {index} | KEEP {quoted(field)} | LIMIT 1', timeout=budget.left(TEST_TIMEOUT))
    except ElasticError as e:
        if field in e.unknown:
            raise SourceDown(f'{index} lacks the field {field}, which the log flow check needs') from None


def test(ctx: Context) -> TestResult:
    config = ctx.instance.config
    off = ctx.instance.off_checks

    def ask() -> str:
        problem = namespace_problem(config)
        if problem:
            raise SourceDown(problem)
        budget = Budget(TEST_TIMEOUT, spent=TEST_SPENT)
        logs, flow, apm = 'logs' not in off, 'logflow' not in off, 'apm' not in off
        parts = [version(ctx.http(), *cluster_of(config, 'logs'), budget)]
        if str(config.get('apm-url') or '') and apm:
            parts.append('APM ' + version(ctx.http(), *cluster_of(config, 'apm'), budget))
        logs_index, flow_index = index_of(config, 'logs'), index_of(config, 'logflow')
        shared = logs_index == flow_index
        notes = [readable(client_for(ctx.instance, ctx.http()), logs_index, 'logs and log flow checks read' if shared else 'logs check reads', budget) if (logs or flow) and (shared or logs) else None,
                 readable(client_for(ctx.instance, ctx.http()), flow_index, 'log flow check reads', budget) if flow and not shared else None,
                 readable(client_for(ctx.instance, ctx.http(), 'apm'), index_of(config, 'apm'), 'APM check reads', budget) if apm else None,
                 error_field(client_for(ctx.instance, ctx.http()), config, budget) if logs else None]
        if logs:
            mask_accepted(client_for(ctx.instance, ctx.http()), config, budget)
        if flow:
            namespace_field(client_for(ctx.instance, ctx.http()), config, budget)
        notes += option_notes(config) if apm else []
        return ', '.join(parts + [n for n in notes if n])

    return connection.result(ask)


def probe_targets(info: InstanceInfo) -> list[dict]:
    out = [{'name': f'elasticsearch-{info.name}', 'url': info.config['url']}]
    if str(info.config.get('apm-url') or ''):
        out.append({'name': f'elasticsearch-{info.name}-apm', 'url': info.config['apm-url']})
    return out


PLUGIN = Plugin(
    setup=Setup(
        steps=(
            'In Kibana open the API keys page, choose Create API key and pick a user key. Creating it needs the manage_own_api_key or manage_api_key privilege.',
            'Under access configuration use custom privileges: read on the Log index (default logs-*), the APM index (default traces-apm*) and the Log flow index if you set one. Add the cluster privilege monitor if you want Test connection to show the version.',
            'Copy the encoded key and paste it into API key. If the APM data is in another cluster, enter its address in APM URL and its own key in APM API key; empty means the log cluster and its key.',
            'Enter the cluster address in URL. A cluster without security needs no key. Press Test connection.',
        ),
        docs='https://www.elastic.co/docs/deploy-manage/api-keys/elasticsearch-api-keys',
        docs_label='Elastic: API keys',
    ),
    id=ID, name='Elasticsearch', version='0.1.0', category='logs', description='New and growing error and warning log patterns, APM latency and failures, and the log flow per namespace, read from Elasticsearch with ES|QL.',
    icon='icon.svg', author='Skopax', licence='Apache-2.0', core='>=0.1,<0.5', edition='community',
    capabilities=Capabilities(hosts=('@url', '@apm-url', '@ui-url'), secrets=('api-key', 'apm-api-key', 'elastic.logging-api-key', 'elastic.apm-api-key'), agent_reads=True),
    client=client,
    fields=(Field('url', 'URL', 'url', required=True, help='Base URL of the Elasticsearch cluster that holds the logs.'),
            Field('api-key', 'API key', 'secret', help='Sent as Authorization: ApiKey. Leave it empty for a cluster without security.'),
            Field('apm-url', 'APM URL', 'url', help='Base URL of the cluster that holds the APM data. Empty means the log cluster.'),
            Field('apm-api-key', 'APM API key', 'secret', help='API key of the APM cluster. Empty means the API key above.'),
            Field('ca', 'CA certificate', 'pem', group='advanced', help='PEM certificate to trust instead of the system roots.'),
            Field('ca-pinned', 'Trust this certificate as it is', 'bool', default='false', show_if=('ca', '*'), group='advanced', help="For a server whose own certificate fails the usual checks: paste the server's own certificate; with the switch on, only that exact certificate is accepted, even if it is expired, self-signed or names another host. A CA certificate does not work with the switch on."),
            Field('ui-url', 'Kibana URL', 'url', ui=True, help='Address of Kibana, for the Discover links on cards and rows.'),
            Field('data-view', 'Data view', 'text', help='Id of the Kibana data view the Discover links open.'),
            Field('log-index', 'Log index', 'text', default='logs-*', help='Index pattern that holds the logs.'),
            Field('apm-index', 'APM index', 'text', default='traces-apm*', help='Index pattern that holds the APM transactions.'),
            Field('index-namespace', 'Namespace from index', 'regex', help='Pattern with a group named ns that takes the namespace from the index name. Empty means the namespace field.'),
            Field('namespace-field', 'Namespace field', 'text', default='kubernetes.namespace', help='Log field that holds the namespace.'),
            Field('service-field', 'Service field', 'text', default='service.name', help='Log field that holds the service name.'),
            Field('level-field', 'Level field', 'text', default='log.level', help='Log field that holds the level, such as ERROR or WARN.'),
            Field('message-field', 'Message field', 'text', default='message', help='Log field that holds the log line.'),
            Field('error-field', 'Error field', 'text', default='error.message', help='Log field read before the message. When the log index has not mapped it, the logs check reads the message only, and Test connection says so.'),
            Field('logflow-index', 'Log flow index', 'text', help='Index pattern the log flow check reads. Empty means the log index.'),
            Field('message-mask', 'Message mask', 'regex', help='Pattern whose matches are replaced by <masked> in the first 400 characters of a log line before the logs check groups lines, for example a request body: body=.*. Elasticsearch runs it (Java syntax), so Test connection tries it. Empty masks nothing.'),
            Field('apm-p95-overrides', 'APM p95 overrides', 'text', help='Comma-separated service=times:floor_ms entries that replace the APM rule set for one service, for example checkout=3:500, search=4 or batch=:1000. A missing part keeps the rule set value.'),
            Field('discover-columns', 'Extra Discover columns', 'text', help='Comma-separated log fields added to the columns of every Discover link, after the level, service, message and error fields. At most 10.')),
    test=test,
    checks=(Check('logs', 'Error and warning log patterns', 900, 180, ('logs',), run_logs, tool='esql', sections=('logTop',), section_help=LOG_HELP),
            Check('apm', 'APM latency and failures', 900, 180, ('apm',), run_apm, tool='esql'),
            Check('logflow', 'Log flow per namespace', 600, 270, ('logflow',), run_logflow, tool='esql', sections=('logflow',), section_help=FLOW_SECTION_HELP)),
    rules=(RuleSet('apm', 'APM latency', dict(APM_RULES), APM_HELP), RuleSet('logflow', 'Log flow', dict(FLOW_RULES), FLOW_HELP)),
    tools=(Tool('esql', DESCRIPTION, SCHEMA, run_esql, instance_input='instance', scan=True),),
    links=links, row_links=row_links, probe_targets=probe_targets,
    panels=(Panel('logs', 'panel.js', label='Logs', group='observe'),),
    knowledge=(Knowledge('esql', 'Elasticsearch', (Path(__file__).parent / 'esql.md').read_text(encoding='utf-8'), slack=True),),
    agent_reads=(AgentRead('esql', ('cluster', 'query'), KEYS, agent_sources, agent_esql, fields=AGENT_FIELDS, timeout=TIMEOUT_S),),
)
