import re
from collections.abc import Mapping

from skopax.contract import Context, Field, ReadError, send
from skopax.kit.elastic import CLUSTERS, FALLBACK, REJECTED, Elastic, ElasticError, client_for, index_of, log_fields, refused_text, sources_of

FROM = re.compile(r'^\s*FROM\s+([^|]+)', re.I)
TIME_BOUND = re.compile(r'@timestamp`?\s*(>=|>|<=|<)', re.I)
MAX_INDICES = 3
ANSWER_MAX = 300
ROWS = 1000
QUERY_LIMIT = 8000
TIMEOUT_S = 75
AGENT_QUERY_S = 70
KEYS = (r'elastic\.(logging|apm)-(url|api-key)',)
AGENT_CLUSTERS = {'logs': 'logging', 'apm': 'apm'}
AGENT_REFUSED = 502
SLOW = ('ES|QL HTTP 504: the query ran past the time limit of the gateway in front of Elasticsearch. Name fewer indices without a star, shorten the @timestamp range, '
        'filter on a keyword field before any LIKE "*...*", or count one day at a time')
DESCRIPTION = ('ES|QL query on Elasticsearch, read only. cluster logs (the default) reads the log cluster, whose logs are in {field:log-index}; '
               'cluster apm reads the APM cluster: {field:apm-index}, logs-apm.error-* and metrics-apm.*. '
               'FROM comes first and names one to three indices; a wildcard or more indices is refused unless WHERE bounds @timestamp. '
               'Long answers are cut, so aggregate with STATS. A WHERE on a field that does not exist returns zero rows, not an error. '
               'first and last are reserved words, so never name a column first or last (use oldest / newest). '
               'Log fields: the line is in {field:message-field}, the level in {field:level-field}, the service in {field:service-field}.')
SCHEMA = {'type': 'object',
          'properties': {'cluster': {'type': 'string', 'enum': list(CLUSTERS), 'description': 'logs (default) or apm'}, 'query': {'type': 'string', 'description': 'the ES|QL query'}},
          'required': ['query'], 'additionalProperties': False}
AGENT_FIELDS = (Field('elastic.logging-url', 'Logs Elasticsearch URL', 'url', help='Log cluster this agent reads for the esql read.'),
                Field('elastic.logging-api-key', 'Logs Elasticsearch API key', 'secret', help='API key of that log cluster, kept on the agent side and sent only to it.'),
                Field('elastic.apm-url', 'APM Elasticsearch URL', 'url', help='APM cluster this agent reads for the esql read.'),
                Field('elastic.apm-api-key', 'APM Elasticsearch API key', 'secret', help='API key of that APM cluster, kept on the agent side and sent only to it.'))


def hints(config: Mapping[str, str]) -> tuple[tuple[re.Pattern, str], ...]:
    named = log_fields(config)
    fields = f"the level in {named['level']}, the service in {named['service']}"
    return ((re.compile(r"mismatched input '(first|last)'", re.I), 'first and last are reserved words: name the column oldest / newest'),
            (re.compile(r'No matches found for pattern'), f"a KEEP or DROP pattern matched no column: name only columns the index has (the log line is {named['message']}, {fields})"),
            (re.compile(r'Unknown index'), f"that index does not exist, or the API key cannot read it: logs are in {index_of(config, 'logs')}, APM in {index_of(config, 'apm')}"),
            (re.compile(r'is \[text\] so second argument must'), 'that field is text: compare it with a quoted string ("404") or use its .keyword field'),
            (re.compile(r'Unknown column \[(log|log\.message|msg|text|line|log_message)\]'), f"the log line is in {named['message']} ({fields})"))


def answer(text: str, status: int, config: Mapping[str, str]) -> str:
    if status == 504:
        return SLOW
    tail = ''.join(f' - {hint}' for pattern, hint in hints(config) if pattern.search(text))
    return text[:max(0, ANSWER_MAX - len(tail))] + tail


def check_scope(query: str, index: str) -> None:
    m = FROM.match(query)
    if not m:
        raise ReadError(400, 'the query must start with FROM <index>')
    sources = [x.strip() for x in m.group(1).split(',') if x.strip()]
    if (any('*' in x for x in sources) or len(sources) > MAX_INDICES) and not TIME_BOUND.search(query):
        raise ReadError(400, f"{', '.join(sources[:MAX_INDICES])} scans more than {MAX_INDICES} daily indices with no time bound: name 1-{MAX_INDICES} indices without a star "
                             f'or add a @timestamp range to WHERE, like FROM {index} | WHERE @timestamp >= NOW() - 1 hour')


def run_esql(ctx: Context, args: dict) -> object:
    cluster = args.get('cluster') or 'logs'
    if cluster not in CLUSTERS:
        raise ReadError(400, 'cluster must be logs or apm')
    query = args.get('query')
    if not isinstance(query, str) or not query.strip():
        raise ReadError(400, 'query is required')
    config = ctx.instance.config
    check_scope(query, index_of(config, cluster))
    try:
        return client_for(ctx.instance, ctx.http(), cluster).query(query)
    except ElasticError as e:
        if e.denied:
            raise ReadError(e.status if e.status in REJECTED else 403, refused_text(e.status, sources_of(query))) from None
        raise ReadError(e.status if 400 <= e.status <= 599 else 502, answer(e.text, e.status, config)) from None


def agent_sources(config: Mapping[str, str]) -> list[str]:
    return [name for name, key in AGENT_CLUSTERS.items() if str(config.get(f'elastic.{key}-url') or '').strip() and str(config.get(f'elastic.{key}-api-key') or '').strip()]


def agent_esql(config: Mapping[str, str], args: dict, transport=None) -> object:
    cluster = args.get('cluster')
    if cluster not in AGENT_CLUSTERS:
        raise ReadError(422, f"cluster is one of {', '.join(AGENT_CLUSTERS)}")
    query = args.get('query')
    if not isinstance(query, str) or not query.strip() or len(query) > QUERY_LIMIT:
        raise ReadError(422, f'query must be a text of at most {QUERY_LIMIT} characters')
    key = AGENT_CLUSTERS[cluster]
    url, api_key = str(config.get(f'elastic.{key}-url') or '').strip(), str(config.get(f'elastic.{key}-api-key') or '').strip()
    if not url or not api_key:
        raise ReadError(503, f'esql {cluster} is off on this agent: elastic.{key}-url and elastic.{key}-api-key are not both set in its config')
    try:
        check_scope(query, FALLBACK['apm-index' if cluster == 'apm' else 'log-index'])
    except ReadError as e:
        raise ReadError(422, e.message) from None
    try:
        return Elastic(url, api_key, transport or send).query(query.strip(), timeout=AGENT_QUERY_S)[:ROWS]
    except ElasticError as e:
        if e.denied:
            raise ReadError(AGENT_REFUSED if e.status == 401 else 403, refused_text(e.status, sources_of(query))) from None
        raise ReadError(400, e.text) from None
