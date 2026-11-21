import datetime as dt
import difflib
import re
from collections.abc import Mapping

from skopax.contract import Context, Field, ReadError, send
from skopax.kit.elastic import CLUSTERS, DATED, FALLBACK, REJECTED, Elastic, ElasticError, client_for, closest_patterns, index_of, log_fields, refused_text, sources_of

FROM = re.compile(r'^\s*FROM\s+([^|]+)', re.I)
TIME_BOUND = re.compile(r'@timestamp`?\s*(>=|>|<=|<)', re.I)
UPPER_BOUND = re.compile(r'@timestamp`?\s*(<=|<)', re.I)
LOWER_BOUND = re.compile(r'(@timestamp`?\s*(?:>=|>)\s*)(?:NOW\(\)\s*-\s*(\d+)\s*(minutes?|hours?|days?|weeks?)\b|"([^"]+)")', re.I)
UNIT_HOURS = {'minute': 1 / 60, 'hour': 1, 'day': 24, 'week': 168}
LAST_DAY = 'NOW() - 1 day'
NARROW_HINT = 'narrow the time range or the index'
MAX_INDICES = 3
COLUMNS_SHOWN = 5
COLUMNS_CLOSE = 0.5
MISSING_NAME = re.compile(r'(?:no such index|Unknown index) \[([^\]]+)\]')
COLUMNS_S = 15
DAY_STAMP = re.compile(r'(\d{4})([.\-])(\d{2})\2(\d{2})')
ANSWER_MAX = 300
HINT_ROOM = 1600
ROWS = 1000
QUERY_LIMIT = 8000
TIMEOUT_S = 75
AGENT_QUERY_S = 70
QUERY_S = 55
KEYS = (r'elastic\.(logging|apm)-(url|api-key)',)
AGENT_CLUSTERS = {'logs': 'logging', 'apm': 'apm'}
AGENT_REFUSED = 502
SLOW = ('ES|QL HTTP 504: the query ran past the time limit of the gateway in front of Elasticsearch. Name fewer indices without a star, shorten the @timestamp range, '
        'filter on a keyword field before any LIKE "*...*", or count one day at a time')
RANGE_KINDS = ('search_phase_execution_exception', 'too_many_buckets_exception', 'no_shard_available_action_exception')
RANGE_NOTE = 'the full range failed; this answer covers the last day (NOW() - 1 day to now); ask for the earlier days one at a time'
LAST_DAY_NOTE = ('the full time range timed out, so this answer covers only the last day (NOW() - 1 day to now); ask for the earlier days one at a time')
DESCRIPTION = ('ES|QL query on Elasticsearch, read only. cluster logs (the default) reads the log cluster, whose logs are in {field:log-index}; '
               'cluster apm reads the APM cluster: {field:apm-index}, logs-apm.error-* and metrics-apm.*. '
               'Daily index names follow UTC dates, so never write a dated index name: use the pattern above with a wildcard and a WHERE on @timestamp. '
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
            (re.compile(r'index_not_found_exception|no such index', re.I),
             f"daily index names follow UTC dates and today's may not exist yet: use the pattern with a * and a @timestamp bound, logs {index_of(config, 'logs')}, APM {index_of(config, 'apm')}"),
            (re.compile(r'Unknown index'), f"that index does not exist, or the API key cannot read it: logs are in {index_of(config, 'logs')}, APM in {index_of(config, 'apm')}"),
            (re.compile(r'is \[text\] so second argument must'), 'that field is text: compare it with a quoted string ("404") or use its .keyword field'),
            (re.compile(r'Unknown column \[(log|log\.message|msg|text|line|log_message)\]'), f"the log line is in {named['message']} ({fields})"))


def answer(text: str, status: int, config: Mapping[str, str]) -> str:
    if status == 504:
        return SLOW
    tail = ''.join(f' - {hint}' for pattern, hint in hints(config) if pattern.search(text))
    return text[:max(0, ANSWER_MAX - len(tail))] + tail


def hours_of(count: str, unit: str) -> float:
    return int(count) * UNIT_HOURS[unit.lower().rstrip('s')]


def literal_hours(text: str) -> float | None:
    try:
        stamp = dt.datetime.fromisoformat(text.strip().replace('Z', '+00:00'))
    except ValueError:
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=dt.timezone.utc)
    return (dt.datetime.now(dt.timezone.utc) - stamp).total_seconds() / 3600


def last_day(query: str) -> str | None:
    if UPPER_BOUND.search(query):
        return None
    wide = False

    def narrow(m: re.Match) -> str:
        nonlocal wide
        hours = hours_of(m.group(2), m.group(3)) if m.group(2) else literal_hours(m.group(4))
        if hours is None or hours <= 24:
            return m.group(0)
        wide = True
        return m.group(1) + LAST_DAY

    narrowed = LOWER_BOUND.sub(narrow, query)
    return narrowed if wide else None


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
    es = client_for(ctx.instance, ctx.http(), cluster)
    try:
        return es.query(query, timeout=QUERY_S)
    except ElasticError as e:
        if e.denied:
            raise ReadError(e.status if e.status in REJECTED else 403, refused_text(e.status, sources_of(query))) from None
        first = answer(e.text, e.status, config)
        status = e.status if 400 <= e.status <= 599 else 502
        if e.missing:
            earlier = yesterday(query, ctx.now, missing_names(e.detail), es) if DAY_STAMP.search(query) else None
            if earlier is not None:
                return day_before(es, query, earlier, status, first, config)
            raise ReadError(status, first + missing_hint(es, query), HINT_ROOM) from None
        if e.unknown:
            raise ReadError(status, first + column_hint(es, query, e.unknown), HINT_ROOM) from None
        slow = e.status == 504 or e.timed_out
        unserved = not slow and unserved_range(e)
        narrowed = last_day(query) if slow or unserved else None
        if narrowed is None:
            raise ReadError(status, first + (f' - {NARROW_HINT}' if slow or unserved else '')) from None
    try:
        rows = es.query(narrowed, timeout=QUERY_S)
    except ElasticError as again:
        raise ReadError(status, f'{first} - the retry on the last day failed too: {answer(again.text, again.status, config)}') from None
    return {'note': RANGE_NOTE if unserved else LAST_DAY_NOTE, 'rows': rows}


def missing_names(detail: str) -> set[str]:
    return {x.strip() for x in MISSING_NAME.findall(detail or '')}


def yesterday(query: str, now: float, missing: set[str], es: Elastic) -> str | None:
    m = FROM.match(query)
    if not m:
        return None
    today = dt.datetime.fromtimestamp(now, dt.timezone.utc).date()
    stamped = [x for x in sources_of(query) if x in missing and any(found_day(d) == today for d in DAY_STAMP.finditer(x))]
    if not stamped or len(stamped) != len(missing) or any(re.sub(r'\*{2,}', '*', DATED.sub('*', x)) not in es.index_patterns() for x in stamped):
        return None

    def back(found: re.Match) -> str:
        day = found_day(found)
        if day != today:
            return found.group(0)
        sep = found.group(2)
        return (day - dt.timedelta(days=1)).strftime(f'%Y{sep}%m{sep}%d')

    sources = m.group(1)
    for x in stamped:
        sources = sources.replace(x, DAY_STAMP.sub(back, x))
    return query[:m.start(1)] + sources + query[m.end(1):]


def found_day(found: re.Match) -> dt.date | None:
    try:
        return dt.date(int(found.group(1)), int(found.group(3)), int(found.group(4)))
    except ValueError:
        return None


def day_before(es: Elastic, query: str, earlier: str, status: int, first: str, config: Mapping[str, str]) -> object:
    try:
        rows = es.query(earlier, timeout=QUERY_S)
    except ElasticError as again:
        raise ReadError(status, f'{first} - the retry on the day before failed too: {answer(again.text, again.status, config)}{missing_hint(es, query)}', HINT_ROOM) from None
    named, used = ', '.join(sources_of(query)), ', '.join(sources_of(earlier))
    return {'note': f"{named} does not exist yet (today's daily index is made with its first log line), so this answer reads the day before: {used}", 'rows': rows}


def column_hint(es: Elastic, query: str, unknown: tuple[str, ...]) -> str:
    sources = sources_of(query)
    if not sources:
        return ''
    try:
        names, _ = es.answer(f"FROM {', '.join(sources)} | LIMIT 0", timeout=COLUMNS_S)
    except ElasticError:
        return ''
    scored = sorted(((max(difflib.SequenceMatcher(None, u, n).ratio() for u in unknown), n) for n in names if n not in unknown), key=lambda x: (-x[0], x[1]))
    close = [n for ratio, n in scored if ratio >= COLUMNS_CLOSE][:COLUMNS_SHOWN]
    return f" - closest columns of {', '.join(sources)}: {', '.join(close)}" if close else ''


def unserved_range(e: ElasticError) -> bool:
    return e.status in (400, 503) and not e.unknown and e.code in RANGE_KINDS


def missing_hint(es: Elastic, query: str) -> str:
    sources = sources_of(query)
    close = closest_patterns(sources, es.index_patterns()) if sources else []
    if not close:
        return ''
    return f" - no index matches {', '.join(sources[:MAX_INDICES])}; closest existing patterns: {', '.join(close)}"


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
