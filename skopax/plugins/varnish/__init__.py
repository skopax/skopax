from pathlib import Path

from skopax.contract import Check, Context, Field, Knowledge, Plugin, ReadError, RuleSet, SourceDown, TestResult, Setup
from skopax.kit import connection
from skopax.kit.elastic import REJECTED, TEST_TIMEOUT, Budget, ElasticError, client_for, indices_of, native, no_read, refusal
from skopax.plugins.varnish.checks import CACHE_HELP, HELP, RULES, SECTION_HELP, links, listed, needed, number, probe_query, run_cache, run_varnish, target_of, tolerant

ID = 'varnish'


def test(ctx: Context) -> TestResult:
    def ask() -> str:
        peer = ctx.peer('elasticsearch')
        if peer is None:
            raise SourceDown('Choose the Elasticsearch instance that holds the Varnish logs.')
        target = target_of(ctx.instance.config, peer.info.config)
        budget = Budget(TEST_TIMEOUT)
        client = client_for(peer.info, peer.http())
        try:
            rows, without = tolerant(client, lambda gone: probe_query(target.index, gone), lambda: budget.left(TEST_TIMEOUT))
        except ElasticError as e:
            if e.status != 401 and refusal(client, e, indices_of(target.index), budget):
                raise SourceDown(no_read(target.index, 'Varnish checks read')) from None
            if e.missing:
                raise SourceDown(f'No index matches {target.index}.') from None
            if needed(e.unknown):
                raise ReadError(400, f'{target.index} lacks {listed(needed(e.unknown))}, which the Varnish checks need.') from None
            if not e.status or e.status in REJECTED:
                raise native(e)
            raise ReadError(e.status, e.text) from None
        first = rows[0] if rows and isinstance(rows[0], dict) else {}
        req, hits = int(number(first.get('req'))), number(first.get('hits'))
        cached = f', {round(100 * hits / req)} % from cache' if req else ''
        them = 'it' if len(without) == 1 else 'them'
        note = f'; the logs have no {listed(without)}, so the hints that need {them} are left out' if without else ''
        return f'Varnish: {req:,} GET requests in the last hour in {target.index} through Elasticsearch {peer.info.name}{cached}{note}'

    return connection.result(ask)


PLUGIN = Plugin(
    setup=Setup(
        steps=(
            'No credential of its own: Varnish reads logs through an Elasticsearch instance. Add that instance first and pick it in Elasticsearch; its API key needs read on the Varnish index.',
            "Set Index if the Varnish logs are not in that instance's log index. The log lines need the fields varnish.method, varnish.cache_status and varnish.path (keyword); Test connection names any that are missing.",
        ),
        docs='https://www.elastic.co/docs/deploy-manage/api-keys/elasticsearch-api-keys',
        docs_label='Elastic: API keys',
    ),
    id=ID, name='Varnish', version='0.1.0', category='web',
    description='Varnish cache hit ratio against the day and the week before, and a daily list of the paths that miss the cache most, read from Varnish logs in Elasticsearch.',
    icon='icon.svg', author='Skopax', licence='Apache-2.0', core='>=0.1,<0.5', edition='community',
    uses=('elasticsearch',),
    fields=(Field('elasticsearch', 'Elasticsearch', 'instance', required=True, choices=('elasticsearch',), help='The Elasticsearch instance that holds the Varnish logs.'),
            Field('index', 'Index', 'text', help='Index pattern of the Varnish logs. Empty means the log index of that Elasticsearch instance.'),
            Field('namespace', 'Namespace', 'text', help='Namespace shown on the Varnish cards. Empty means none.'),
            Field('service', 'Service', 'text', default='varnish', help='Service shown on the Varnish cards.'),
            Field('personal-paths', 'Personal paths', 'regex', help='Pattern of the paths of personal pages, such as sign-in or checkout flows. Their misses are expected, and the cache candidates say so.')),
    test=test,
    checks=(Check('varnish', 'Varnish hit ratio', 900, 720, ('varnish',), run_varnish, tool='esql', sections=('varnish',), section_help=SECTION_HELP),
            Check('varnish-cache', 'Varnish cache candidates', 86400, 25200, ('varnish-cache',), run_cache, tool='esql', sections=('varnishCache',), section_help=CACHE_HELP)),
    rules=(RuleSet('rules', 'Varnish', dict(RULES), HELP),),
    links=links,
    knowledge=(Knowledge('varnish', 'Varnish logs', (Path(__file__).parent / 'varnish.md').read_text(encoding='utf-8')),),
)
