import re
from collections.abc import Mapping
from dataclasses import dataclass, field

from collections.abc import Callable

from skopax.contract import Context, InstanceInfo, PassResult, Peer, Sweep, anomaly, iso
from skopax.kit.elastic import (Budget, Elastic, ElasticError, access_card, card_urls, carried, client_for, discover_for, fields_card, index_of, indices_of, no_access, refusal,
                                refused_status)
from skopax.monitor import below_both

OFFS = (1, 7)
RULES = {'drop': 0.75, 'floor': 20, 'min_req': 3000, 'crit_share': 0.5, 'candidate_misses': 2000, 'candidate_share': 0.5}
BOTH = 'the same window 1 day and 7 days earlier'
HELP = {'drop': f'Warning when the share of requests served from cache falls below this share of {BOTH}.',
        'floor': 'The cache share is checked only when both earlier windows were at least this many percent.',
        'min_req': 'The cache share is checked only when the last hour had at least this many requests.',
        'crit_share': 'Critical when the cache share is below this share of the lower earlier window.',
        'candidate_misses': 'A page is listed as a caching candidate only with at least this many cache misses in a day.',
        'candidate_share': 'A page is listed as a caching candidate only when at least this share of its requests missed the cache.'}
SECTION_HELP = {'varnish': 'Share of GET requests Varnish served from cache in the last hour, against the same hour 1 and 7 days earlier'}
CACHE_HELP = {'varnishCache': 'Paths Varnish missed most in the last 24 hours, with a hint why each one misses'}
STATIC = re.compile(r'\.(?:js|mjs|css|json|txt|xml|webmanifest|ico|png|jpe?g|gif|svg|webp|avif|woff2?)$', re.I)
RSC, QUERY_STRING, RESPONSE = 'varnish.query_params._rsc.keyword', 'varnish.query_string', 'varnish.response_code.keyword'
REQUIRED = ('varnish.method.keyword', 'varnish.cache_status.keyword', 'varnish.path.keyword')
RATIO = REQUIRED[:2]
OPTIONAL = (RSC, QUERY_STRING, RESPONSE)
FIELDS = REQUIRED + OPTIONAL
TERMS = {RSC: ('rsc = CASE(varnish.query_params._rsc.keyword IS NOT NULL, 1, 0)', 'rsc = 0'),
         QUERY_STRING: ('q = CASE(varnish.query_string IS NOT NULL, 1, 0)', 'q = 0'),
         RESPONSE: ('ok = CASE(varnish.response_code.keyword == "200", 1, 0)', 'ok = 1')}
PART = 'logs'
TOP = 8
HOURLY_TOP = 3
GET = 'varnish.method.keyword == "GET" AND varnish.cache_status.keyword IS NOT NULL'
MISSES_KQL = 'varnish.cache_status:"miss" and varnish.method:"GET"'
COLUMNS = ('varnish.path', 'varnish.query_string', 'varnish.response_code', 'varnish.content_type')
LINK_LABEL = 'Varnish misses in Kibana'
NO_PEER = 'error: no Elasticsearch instance is chosen'
QUIET = 'ok \u00b7 no data'


@dataclass(frozen=True)
class Target:
    index: str
    ns: str
    svc: str
    personal: re.Pattern | None
    ui: Mapping[str, str] = field(default_factory=dict)


def target_of(config: Mapping[str, str], peer_config: Mapping[str, str]) -> Target:
    raw = str(config.get('personal-paths') or '').strip()
    return Target(str(config.get('index') or '').strip() or index_of(peer_config, 'logs'), str(config.get('namespace') or '').strip() or '-',
                  str(config.get('service') or '').strip() or 'varnish', re.compile(raw, re.I) if raw else None, peer_config)


def window() -> str:
    return ' OR '.join(['@timestamp >= NOW() - 60 minutes'] + [f'(@timestamp >= NOW() - {o} days - 60 minutes AND @timestamp < NOW() - {o} days)' for o in OFFS])


def ratio_query(index: str) -> str:
    return (f'FROM {index}\n| WHERE {GET} AND ({window()})\n'
            '| EVAL w = CASE(@timestamp >= NOW() - 60 minutes, "now", @timestamp >= NOW() - 2 days, "d1", "d7"), '
            'hit = CASE(varnish.cache_status.keyword == "hit", 1, 0)\n| STATS req = COUNT(*), hits = SUM(hit) BY w')


def paths_query(index: str, hours: int, min_misses: int, without: tuple[str, ...] = ()) -> str:
    terms = ', '.join(['miss = CASE(varnish.cache_status.keyword == "hit", 0, 1)'] + [TERMS[f][1] if f in without else TERMS[f][0] for f in OPTIONAL])
    return (f'FROM {index}\n| WHERE {GET} AND @timestamp >= NOW() - {hours} hours\n| EVAL {terms}\n'
            '| STATS req = COUNT(*), misses = SUM(miss), rsc = SUM(rsc), q = SUM(q), ok = SUM(ok) BY path = varnish.path.keyword\n'
            f'| WHERE misses >= {min_misses}\n| SORT misses DESC\n| LIMIT {TOP * 3}')


def probe_query(index: str, without: tuple[str, ...] = ()) -> str:
    seen = ' OR '.join(f'{f} IS NOT NULL' for f in FIELDS[2:] if f not in without)
    return (f'FROM {index}\n| WHERE {GET} AND @timestamp >= NOW() - 60 minutes\n'
            f'| EVAL hit = CASE(varnish.cache_status.keyword == "hit", 1, 0), seen = {seen}\n| STATS req = COUNT(*), hits = SUM(hit)')


def needed(unknown: tuple[str, ...], among: tuple[str, ...] = REQUIRED) -> tuple[str, ...]:
    return tuple(f for f in among if f in unknown)


def tolerant(es: Elastic, build: Callable[[tuple[str, ...]], str], timeout: Callable[[], float]) -> tuple[list[dict], tuple[str, ...]]:
    without: tuple[str, ...] = ()
    while True:
        try:
            return es.query(build(without), timeout=timeout()), without
        except ElasticError as e:
            gaps = tuple(f for f in OPTIONAL if f in e.unknown and f not in without)
            if not gaps or needed(e.unknown):
                raise
            without += gaps


def number(value: object) -> float:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else 0.0


def share(rows: dict, w: str) -> tuple[float | None, int]:
    r = rows.get(w) or {}
    req = int(number(r.get('req')))
    return (100.0 * number(r.get('hits')) / req if req else None), req


def hint(r: dict, personal: re.Pattern | None) -> str:
    req = r['req'] or 1
    if personal is not None and personal.search(r['path'] or ''):
        return 'checkout or personal flow: misses are expected'
    if r['ok'] / req < 0.5:
        return 'mostly non-200 answers (redirects or errors), which are not cached'
    if STATIC.search(r['path'] or ''):
        return 'static file: the VCL does not cache it; a VCL entry or a CDN cache rule would serve it from cache'
    if r['rsc'] / req >= 0.5:
        return 'Next.js RSC requests: every _rsc value is a new cache key'
    if r['q'] / req >= 0.5:
        return 'unique query values (form inputs or tracking values) split the cache'
    return 'a page without query values that rarely hits: most likely the VCL does not cache it; make it cacheable if the page is the same for everyone'


def candidates(rows: list[dict], rules: Mapping[str, float], personal: re.Pattern | None, without: tuple[str, ...] = ()) -> list[dict]:
    out = []
    for r in rows:
        if not isinstance(r, dict):
            continue
        req, misses = int(number(r.get('req'))), int(number(r.get('misses')))
        if not isinstance(r.get('path'), str) or not r['path'] or not req or misses / req < rules['candidate_share']:
            continue
        c = {'path': r['path'], 'req': req, 'misses': misses, 'missPct': round(100 * misses / req, 1),
             'rsc': 0 if RSC in without else int(number(r.get('rsc'))), 'q': 0 if QUERY_STRING in without else int(number(r.get('q'))),
             'ok': req if RESPONSE in without else int(number(r.get('ok')))}
        c['hint'] = hint(c, personal)
        out.append(c)
    return out[:TOP]


def line(c: dict) -> str:
    return f"{c['path']} {c['misses']:,} of {c['req']:,} missed ({round(c['missPct'])} %): {c['hint']}"


def misses_link(target: Target, since: str) -> list[dict]:
    return card_urls(LINK_LABEL, discover_for(target.ui, MISSES_KQL, since, columns=COLUMNS))


def evaluate_ratio(rows: dict, rules: Mapping[str, float], target: Target) -> tuple[dict | None, dict]:
    now, req = share(rows, 'now')
    d1, _ = share(rows, 'd1')
    d7, _ = share(rows, 'd7')
    section = {'hitPct': None if now is None else round(now, 1), 'req': req, 'd1': None if d1 is None else round(d1, 1), 'd7': None if d7 is None else round(d7, 1)}
    if now is None or d1 is None or d7 is None or req < rules['min_req'] or not below_both(now, d1, d7, rules['drop'], rules['floor']):
        return None, section
    sev = 'crit' if now < rules['crit_share'] * min(d1, d7) else 'warn'
    x = anomaly('varnish', 'varnish-hit', target.ns, target.svc, 'ratio', sev, round(now, 1), round(d1, 1), round(d7, 1), '%', 'Varnish hit ratio',
                f'Varnish served {round(now)} % of {req:,} GET requests from cache in the last 60 min, against {round(d1)} % a day and {round(d7)} % a week earlier. '
                'More misses send more traffic to the backend. A VCL change, a new query value in links (query values are part of the cache key), or a cache purge does this.',
                5, f'{sev.upper()} Varnish hit ratio {round(now)} % (1d {round(d1)} % / 7d {round(d7)} %)')
    return x, section


def listed(columns: tuple[str, ...]) -> str:
    return ', '.join(columns)


def ran(check: str) -> tuple[str, ...]:
    return (check, f'{check}:fields', f'{check}:access:{PART}')


def quiet(check: str, section: list[dict] | None = None, found: list[dict] | None = None, status: str = QUIET) -> Sweep:
    name = 'varnish' if check == 'varnish' else 'varnishCache'
    return Sweep(PassResult(check, True, status, found or [], {}, (f'{check}:no-data',) + ran(check)[1:]), sections={name: section or []})


def fault(check: str, es: Elastic, e: ElasticError, target: Target, budget: Budget) -> Sweep:
    if refusal(es, e, indices_of(target.index), budget):
        return no_access(check, access_card(check, 'Varnish', PART, target.index, refused_status(e), bool(es.api_key)))
    if e.missing:
        return quiet(check)
    names = needed(e.unknown, RATIO if check == 'varnish' else REQUIRED)
    if names:
        return quiet(check, found=[fields_card(check, 'varnish', 'Varnish', target.ns, target.svc, names, target.index)])
    return Sweep(PassResult(check, False, f'error: {e.reason}'))


def run_ratio(es: Elastic, now: float, rules: Mapping[str, float], target: Target, budget: Budget | None = None) -> Sweep:
    budget = budget or Budget()
    status = 'ok'
    try:
        rows = {r.get('w'): r for r in es.query(ratio_query(target.index), timeout=budget.left()) if isinstance(r, dict)}
        if not rows:
            return quiet('varnish')
        card, section = evaluate_ratio(rows, rules, target)
        summary = [{'part': 'summary', 'at': iso(now), **section}]
        if section['hitPct'] is None:
            return quiet('varnish', summary, status=f'{QUIET} for the last hour')
        found = []
        if card:
            try:
                got, without = tolerant(es, lambda gone: paths_query(target.index, 1, 50, gone), budget.left)
                top = candidates(got, rules, target.personal, without)[:HOURLY_TOP]
            except ElasticError as e:
                if e.denied or not needed(e.unknown):
                    raise
                top, status = [], f'{QUIET} for path list'
            if top:
                card['what'] += ' Most missed in the last hour: ' + '; '.join(line(c) for c in top) + '.'
            urls = misses_link(target, 'now-2h')
            if urls:
                card['urls'] = urls
            found.append(card)
    except ElasticError as e:
        return fault('varnish', es, e, target, budget)
    return Sweep(PassResult('varnish', True, status, found, {}, ran('varnish')), sections={'varnish': summary})


def run_daily(es: Elastic, now: float, rules: Mapping[str, float], target: Target, budget: Budget | None = None) -> Sweep:
    budget = budget or Budget()
    try:
        got, without = tolerant(es, lambda gone: paths_query(target.index, 24, int(rules['candidate_misses']), gone), budget.left)
    except ElasticError as e:
        return fault('varnish-cache', es, e, target, budget)
    top = candidates(got, rules, target.personal, without)
    found = []
    if top:
        them = 'it' if len(without) == 1 else 'them'
        note = f' The logs have no {listed(without)}, so the hints that need {them} are left out.' if without else ''
        x = anomaly('varnish', 'varnish-cache', target.ns, target.svc, 'candidates', 'info', len(top), None, None, '', 'Varnish cache candidates',
                    f'Paths Varnish missed most in the last 24 h (GET, at least {int(rules["candidate_misses"]):,} misses and half of their requests): '
                    + '; '.join(line(c) for c in top) + '. Each hint says why it misses; static files and pages the VCL does not cache are the ones a VCL or CDN change can win.' + note,
                    1, f'INFO Varnish cache candidates: {len(top)} paths, most missed {top[0]["path"]}')
        x.update(ranKey='varnish-cache', candidates=top)
        urls = misses_link(target, 'now-24h')
        if urls:
            x['urls'] = urls
        found.append(x)
    rows = [{'part': 'path', **c} for c in top] + [{'part': 'summary', 'at': iso(now)}]
    return Sweep(PassResult('varnish-cache', True, 'ok', found, {}, ran('varnish-cache')), sections={'varnishCache': rows})


def reach(ctx: Context) -> tuple[Peer | None, Target | None]:
    peer = ctx.peer('elasticsearch')
    return (peer, target_of(ctx.instance.config, peer.info.config)) if peer is not None else (None, None)


def run_varnish(ctx: Context) -> Sweep:
    peer, target = reach(ctx)
    if peer is None:
        return Sweep(PassResult('varnish', False, NO_PEER))
    return run_ratio(client_for(peer.info, peer.http()), ctx.now, ctx.rules('rules'), target)


def run_cache(ctx: Context) -> Sweep:
    peer, target = reach(ctx)
    if peer is None:
        return Sweep(PassResult('varnish-cache', False, NO_PEER))
    return run_daily(client_for(peer.info, peer.http()), ctx.now, ctx.rules('rules'), target)


def links(card: dict, info: InstanceInfo) -> list[dict] | None:
    return carried(card, 'varnish')
