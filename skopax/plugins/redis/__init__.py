from pathlib import Path

from skopax.contract import Check, Context, Field, HttpError, Knowledge, Panel, PassResult, Plugin, ReadError, RuleSet, Sweep, TestResult, Tool, iso, Setup
from skopax.kit import access, connection
from skopax.kit.promql import Late, PrometheusError, bounded, client_for, credential, graph, instant, number, ui_of
from skopax.monitor import failed
from skopax.plugins.redis.engine import (APP_ROWS, BUDGET_S, ID, OPS, RULES, app_cards, app_rows, call_graph, capped, card_urls, exporter_cards, exporter_rows, links, matchers, peer_names, status_of)

NAME = 'Redis'
NO_PEER = 'no Prometheus instance is set'
PARTS = ('exporter', 'app')
NAMES = {'exporter': 'instances', 'app': 'services'}
RULE_HELP = {
    'app_times': "Warning when a service's Redis calls take this many times as long as 1 or 7 days earlier, measured in the service.",
    'app_floor_ms': "A service's Redis call time is checked only when it is at least this many milliseconds.",
    'app_min_calls': "A service's Redis call time is checked only when it made at least this many calls in 30 minutes.",
    'app_errors': 'Warning when a service has this many failed Redis calls in 30 minutes.',
}
SECTION_HELP = {'redis': 'rows of the Redis check: redis_exporter instances (part exporter), services by Lettuce call time (part app) and one summary row'}
INSTANCES_NOTE = ('one row per redis_exporter instance (the pod, else the instance label): up, role, connected replicas on a master, memory in bytes, clients, '
                  'ops/s over 30 min, rejected connections and evicted keys in 30 min')
APPS_NOTE = ('average Redis call time per service in ms over 30 min, measured by the Lettuce client in the service; d1 / d7 are the same window 1 and 7 days earlier; '
             'redis is the address the service calls')
DESCRIPTION = ('Redis, read only, through Prometheus instance {field:prometheus}. op instances (default): each redis_exporter instance with up, role, connected replicas, '
               'memory, clients, ops/s over 30 min, rejected connections and evicted keys in 30 min. op apps: average Redis call time in ms per service over 30 min '
               'as the service measures it (Lettuce), with the same window 1 and 7 days earlier, calls and errors; job filters one service. Use it on redis cards, '
               'on a cache question, and when a service slows down and its Redis calls might be why. At most 6 calls per run.')
SCHEMA = {'type': 'object', 'properties': {'op': {'type': 'string', 'enum': list(OPS)}, 'job': {'type': 'string'}}, 'additionalProperties': False}


def selector_of(ctx: Context) -> str:
    return ctx.instance.config.get('selector') or ''


def names_of(ctx: Context) -> dict[str, str]:
    return peer_names(ctx.instance.config.get('peer-names'))[0]


def test(ctx: Context) -> TestResult:
    def ask() -> str:
        bad = peer_names(ctx.instance.config.get('peer-names'))[1]
        if bad:
            raise ReadError(400, f"Peer names needs address=name entries; {', '.join(bad)} cannot be read")
        peer = ctx.peer('prometheus')
        if peer is None:
            raise ReadError(400, NO_PEER)
        try:
            found = instant(peer, f'count by (redis_version) (redis_instance_info{matchers(selector_of(ctx))})')
        except HttpError as e:
            said = access.tested(NAME, NAMES['exporter'], e.code, credential=credential(peer.info.config))
            if said:
                raise ReadError(e.code, said) from None
            raise
        count = int(sum(number(r) for r in found))
        if not count:
            return f'Prometheus {peer.info.name} answered, but it holds no redis_exporter series yet'
        versions = sorted({str((r.get('metric') or {}).get('redis_version') or '') for r in found} - {''})
        return f"Redis {', '.join(versions) or 'of an unknown version'} on {count} instance{'' if count == 1 else 's'} through Prometheus {peer.info.name}"

    return connection.result(ask)


def run(ctx: Context) -> Sweep:
    peer = ctx.peer('prometheus')
    if peer is None:
        return Sweep(PassResult(ID, False, f'error: {NO_PEER}'))
    client = client_for(peer.info, bounded(peer.http(), BUDGET_S))
    selector, rules, ui = selector_of(ctx), ctx.rules('rules'), ui_of(peer.info.config)
    reads = {'exporter': lambda: exporter_rows(client, selector, ctx.now), 'app': lambda: app_rows(client, selector, ctx.now, names_of(ctx))}
    found, ran, errors, empty, rows, answered, refused, reached = [], [], [], [], [], 0, [], []
    for part in PARTS:
        try:
            got = reads[part]()
        except Late:
            errors.append(f'{NAMES[part]}: timed out')
            continue
        except PrometheusError as e:
            if access.denied(e.code):
                refused.append(part)
                reached.append(part)
                found.append(access.card(ID, NAME, part, status=e.code, credential=credential(peer.info.config), name=NAMES[part]))
                continue
            errors.append(f'{NAMES[part]}: {e.reason}')
            continue
        except Exception as e:
            why = failed(e).removeprefix('error: ')
            errors.append(f'{NAMES[part]}: {why}')
            continue
        answered += 1
        reached.append(part)
        if not got:
            empty.append(part)
            continue
        cards = exporter_cards(got) if part == 'exporter' else app_cards(got, rules)
        for card in cards:
            urls = card_urls(card, ui, selector)
            if urls:
                card['urls'] = urls
        found += cards
        rows += [{'part': part, **r} for r in capped(got)] if part == 'exporter' else [app_row(r, peer.info.name, ui, selector) for r in got[:APP_ROWS]]
        ran.append(f'{ID}:{part}')
    if not answered and not refused:
        return Sweep(PassResult(ID, False, 'error: ' + '; '.join(errors)))
    summary = {'part': 'summary', 'at': iso(ctx.now), 'source': peer.info.name}
    keys = tuple(ran) + tuple(access.ran_key(ID, part) for part in reached) + (() if ran else (f'{ID}:no-data',))
    return Sweep(PassResult(ID, True, status_of(errors, [NAMES[p] for p in empty], ran, [NAMES[p] for p in refused]), found, {}, keys), sections={ID: rows + [summary]})


def app_row(row: dict, source: str, ui: str, selector: str) -> dict:
    out = {'part': 'app', **row, 'source': source}
    link = graph(call_graph(row['ns'], row['job'], selector), ui)
    if link:
        out['link'] = link
    return out


def read(ctx: Context, args: dict) -> object:
    op = args.get('op') or 'instances'
    if op not in OPS:
        raise ReadError(400, f"op must be one of {', '.join(OPS)}")
    job = str(args.get('job') or '').strip()
    peer = ctx.peer('prometheus')
    if peer is None:
        raise ReadError(503, NO_PEER)
    client = client_for(peer.info, peer.http())
    try:
        if op == 'instances':
            found = exporter_rows(client, selector_of(ctx), ctx.now)
            kept = capped(found)
            more = f'first {len(kept)} of {len(found)} instances, down and role-less ones first; ' if len(kept) < len(found) else ''
            return {'instances': kept, 'note': more + INSTANCES_NOTE}
        rows = [dict(r, source=peer.info.name) for r in app_rows(client, selector_of(ctx), ctx.now, names_of(ctx)) if not job or r['job'] == job]
        return {'apps': rows[:APP_ROWS], 'note': APPS_NOTE}
    except PrometheusError as e:
        if access.denied(e.code):
            raise ReadError(e.code, access.text(NAME, NAMES['exporter' if op == 'instances' else 'app'], credential=credential(peer.info.config), status=e.code)) from None
        raise ReadError(502, e.reason) from None


PLUGIN = Plugin(
    setup=Setup(
        steps=(
            'No credential of its own: Redis reads through a Prometheus instance. Add that Prometheus instance first and pick it in Prometheus; it signs in as set there.',
            'That Prometheus must hold the redis_exporter series (redis_up, redis_memory_used_bytes and others) and, for the Redis calls of each service, the lettuce_seconds_* series of the Lettuce client. Use Selector to narrow what is read.',
        ),
        docs='https://github.com/oliver006/redis_exporter',
        docs_label='redis_exporter',
    ),
    id=ID, name='Redis', version='0.1.0', category='infrastructure', description='Redis instances and the Redis calls of each service, read from redis_exporter and Lettuce series in Prometheus.',
    icon='icon.svg', author='Skopax', licence='AGPL-3.0-only', core='>=0.1,<0.8', edition='community', uses=('prometheus',),
    fields=(Field('prometheus', 'Prometheus', 'instance', required=True, choices=('prometheus',), help='The Prometheus instance that holds the redis_exporter and Lettuce client series.'),
            Field('selector', 'Selector', 'text', help='Extra label matchers added to every Redis query, such as env="prod". Empty reads every series.'),
            Field('peer-names', 'Redis names', 'text', help='Comma-separated address=name entries that show a friendly name for the Redis address a service calls, for example 10.0.4.12:6379=sessions-cache, 10.0.4.13=queue. An address without a port matches any port. Empty shows the address.')),
    test=test,
    checks=(Check(ID, 'Redis instances and service calls', 900, 780, (ID,), run, tool=ID, sections=(ID,), section_help=SECTION_HELP),),
    rules=(RuleSet('rules', 'Redis thresholds', dict(RULES), RULE_HELP),),
    tools=(Tool(ID, DESCRIPTION, SCHEMA, read, per_run=6, scan=True),),
    links=links, panels=(Panel(ID, 'panel.js', label='Redis', group='observe'),),
    knowledge=(Knowledge(ID, 'Redis', (Path(__file__).parent / 'redis.md').read_text(encoding='utf-8'), slack=True),),
)
