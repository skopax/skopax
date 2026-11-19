from pathlib import Path

from skopax.contract import Check, Context, Field, Knowledge, Plugin, ReadError, RuleSet, SourceDown, TestResult, Setup
from skopax.kit import connection
from skopax.kit.elastic import REJECTED, TEST_TIMEOUT, Budget, ElasticError, client_for, native, no_read, refusal, sources_of
from skopax.plugins.csp.checks import HELP, NO_MESSAGE, RULES, SECTION_HELP, grouped, links, query, run_csp, shown_sources, target_of

ID = 'csp'


def test(ctx: Context) -> TestResult:
    def ask() -> str:
        peer = ctx.peer('elasticsearch')
        if peer is None:
            raise SourceDown('Choose the Elasticsearch instance that holds the CSP reports.')
        target = target_of(ctx.instance.config, peer.info.config)
        if not target.query:
            raise SourceDown('Enter the ES|QL query that selects the CSP reports.')
        budget = Budget(TEST_TIMEOUT)
        client = client_for(peer.info, peer.http())
        try:
            rows = client.query(query(target.query, 1), timeout=budget.left(TEST_TIMEOUT))
        except ElasticError as e:
            if e.status != 401 and refusal(client, e, sources_of(target.query), budget):
                raise SourceDown(no_read(shown_sources(target), 'CSP check reads')) from None
            if e.missing:
                raise SourceDown(f'No index matches {shown_sources(target)}.') from None
            if 'message' in e.unknown:
                raise ReadError(400, NO_MESSAGE) from None
            if not e.status or e.status in REJECTED:
                raise native(e)
            raise ReadError(e.status, e.text) from None
        hour = grouped(rows)
        return f"CSP reports: {sum(g['c'] for g in hour.values()):,} in the last hour from {len(hour)} blocked hosts, through Elasticsearch {peer.info.name}"

    return connection.result(ask)


PLUGIN = Plugin(
    setup=Setup(
        steps=(
            'No credential of its own: CSP reports reads logs through an Elasticsearch instance. Add that instance first and pick it in Elasticsearch; its API key needs read on the indices the query names.',
            'Write Query as ES|QL that selects the report lines, such as FROM logs-* | WHERE message LIKE "*csp-report*". Each report is read from the message column. Press Test connection.',
        ),
        docs='https://www.elastic.co/docs/reference/query-languages/esql',
        docs_label='Elastic: ES|QL',
    ),
    id=ID, name='CSP reports', version='0.1.0', category='web',
    description='Deprecated: use the CSP preset under custom checks; this plug-in leaves in 0.4. Content Security Policy violation reports from the logs in Elasticsearch: hosts the browsers block for the first time, and jumps in the hourly report count.',
    icon='icon.svg', author='Skopax', licence='Apache-2.0', core='>=0.1,<0.3', edition='community',
    uses=('elasticsearch',),
    fields=(Field('elasticsearch', 'Elasticsearch', 'instance', required=True, choices=('elasticsearch',), help='The Elasticsearch instance that holds the CSP reports.'),
            Field('query', 'Query', 'text', required=True,
                  help='ES|QL source and filter that select the CSP report log lines, such as FROM logs-* | WHERE message LIKE "*csp-report*". '
                       'The check adds the time window and reads each report from the message column.'),
            Field('namespace', 'Namespace', 'text', help='Namespace shown on the CSP cards. Empty means none.'),
            Field('service', 'Service', 'text', default='csp', help='Service shown on the CSP cards.')),
    test=test,
    checks=(Check('csp', 'CSP reports', 900, 0, ('csp',), run_csp, tool='esql', sections=('csp',), section_help=SECTION_HELP),),
    rules=(RuleSet('rules', 'CSP reports', dict(RULES), HELP),),
    links=links,
    knowledge=(Knowledge('csp', 'CSP reports', (Path(__file__).parent / 'csp.md').read_text(encoding='utf-8')),),
)
