import json
from collections.abc import Mapping
from pathlib import Path

from skopax.contract import Check, Context, Field, HttpError, Knowledge, Panel, PassResult, Peer, Plugin, ReadError, RuleSet, Sweep, TestResult, Tool, iso, Setup
from skopax.kit import access, connection
from skopax.kit.promql import Late, PrometheusError, bounded, client_for, credential, instant, number, ui_of
from skopax.monitor import failed
from skopax.plugins.kafka.engine import (APP_ROWS, BUDGET_S, DEFAULT_PRESET, GROUP_ROWS, HISTORY_HOURS, ID, OPS, PRESETS, RULES, TOPIC_ROWS, WINDOW, app_cards, app_rows, capped, card_urls, cluster_cards, cluster_rows, group_cards, group_rows, label_is, links, preset_of, row_links, status_of, wanted)

NAME = 'Kafka'
NO_PEER = 'no Prometheus instance is set'
PEER_CHECK = 'prom'
NAMES = {'clusters': 'clusters', 'groups': 'groups', 'app': 'services'}
PARTS = (('clusters', capped), ('groups', lambda rows: rows[:GROUP_ROWS]), ('app', lambda rows: rows[:APP_ROWS]))
RULE_HELP = {
    'disk_warn': "Warning when a Kafka broker's disk is this percent full (strimzi preset).",
    'disk_crit': 'Critical at this percent full.',
    'gone_lag': 'Warning when a consumer group has had no members for 15 minutes while at least this many messages wait.',
    'app_failures': 'Warning when a service has this many failed Kafka listener calls or sends in 30 minutes.',
    'app_times': 'Those failures must also be this many times the same window 1 or 7 days earlier.',
}
SECTION_HELP = {'kafka': 'rows of the Kafka check: clusters (part clusters), consumer groups (part groups), services by Spring Kafka timers (part app) and one summary row'}
CLUSTERS_NOTE = ('ns names the cluster: the namespace under the strimzi preset, the exporter job under kafka-exporter. expected = brokers seen in the last day, '
                 'brokers10 = the most brokers of the last 10 min; topics, partitions and msgs leave out internal topics (__consumer_offsets); '
                 'urp = under-replicated partitions now, urp10 = the lowest count of the last 10 min; msgs = messages written per second over 30 min '
                 '(msgsD1 / msgsD7 the same window 1 and 7 days earlier); with the strimzi preset, diskPct = the fullest broker volume and restarts = pod restarts in 30 min '
                 'in the namespaces the Prometheus instance does not already watch for restarts')
GROUPS_NOTE = 'lag = messages waiting now (d1 / d7 the same moment 1 and 7 days earlier); lagMin15 = the lowest lag of the last 15 min; members15 / members1d = the most members in 15 min / a day'
GROUP_NOTE = 'lag per topic now, 1 and 7 days earlier; readPerS = offsets the group committed per second over 30 min'
TOPICS_NOTE = 'busiest topics by messages written per second over 30 min, with the same window 1 and 7 days earlier'
APPS_NOTE = 'per service over 30 min: in = Spring Kafka listener calls, out = KafkaTemplate sends, *Fail = failed ones (FailD1 / FailD7 the same window 1 and 7 days earlier), *Ms = average time in ms'
DESCRIPTION = ('Kafka, read only, from the kafka_exporter and Spring Kafka series on Prometheus instance {field:prometheus} (preset {field:preset}). '
               'cluster names a Kafka cluster the way the rows name it in ns: the namespace under the strimzi preset, the exporter job under kafka-exporter. '
               'op clusters (default): brokers up of those seen in a day, topics, partitions, under-replicated and leaderless partitions, consumer groups, messages/s with 1 d / 7 d, '
               'and with the strimzi preset the fullest broker volume and the pod restarts Prometheus does not already card. op groups: every consumer group with lag now, 1 d and 7 d earlier and members '
               "(cluster and group filter). op group (group, cluster optional): that group's lag per topic and offsets read per second. op topics: the busiest topics by "
               'messages/s with 1 d / 7 d (cluster optional). op apps: per service, Spring Kafka listener calls and KafkaTemplate sends over 30 min, failures with 1 d / 7 d '
               f"and average time (job filter). op history (group, hours 1-{HISTORY_HOURS}): a group's lag over time. Use it on kafka and kafka-lag cards and when a flow "
               'that runs on events stalls. At most 6 calls per run.')
TEXT = {'type': 'string'}
SCHEMA = {'type': 'object', 'properties': {'op': {'type': 'string', 'enum': list(OPS)}, 'cluster': TEXT, 'group': TEXT, 'job': TEXT,
                                           'hours': {'type': 'integer', 'minimum': 1, 'maximum': HISTORY_HOURS}}, 'additionalProperties': False}


def watching(peer: Peer) -> Mapping[str, str] | None:
    return None if PEER_CHECK in peer.off_checks else peer.info.config


def plural(n: int, one: str) -> str:
    return f"{n} {one}{'' if n == 1 else 's'}"


def test(ctx: Context) -> TestResult:
    def ask() -> str:
        peer = ctx.peer('prometheus')
        if peer is None:
            raise ReadError(400, NO_PEER)
        k = preset_of(ctx.instance.config)
        try:
            found = instant(peer, f'max by ({k.by}) (kafka_brokers{{{k.sel}}})')
        except HttpError as e:
            said = access.tested(NAME, 'clusters', e.code, credential=credential(peer.info.config))
            if said:
                raise ReadError(e.code, said) from None
            raise
        if not found:
            return f'Prometheus {peer.info.name} answered, but it holds no kafka_exporter series yet'
        brokers = int(sum(number(r) for r in found))
        return f'Kafka with {plural(brokers, "broker")} in {plural(len(found), "cluster")} through Prometheus {peer.info.name}'

    return connection.result(ask)


def run(ctx: Context) -> Sweep:
    peer = ctx.peer('prometheus')
    if peer is None:
        return Sweep(PassResult(ID, False, f'error: {NO_PEER}'))
    client = client_for(peer.info, bounded(peer.http(), BUDGET_S))
    k, rules, ui = preset_of(ctx.instance.config, watching(peer)), ctx.rules('rules'), ui_of(peer.info.config)
    reads = {'clusters': (lambda: cluster_rows(client, k, ctx.now), cluster_cards), 'groups': (lambda: group_rows(client, k, ctx.now), group_cards),
             'app': (lambda: app_rows(client, ctx.now), app_cards)}
    found, ran, errors, empty, rows, answered, refused, reached = [], [], [], [], [], 0, [], []
    for part, keep in PARTS:
        read_part, cards_of = reads[part]
        try:
            got = read_part()
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
        cards = cards_of(got, rules)
        for card in cards:
            urls = card_urls(card, k, ui)
            if urls:
                card['urls'] = urls
        found += cards
        rows += [{'part': part, **r, **({'source': peer.info.name} if part == 'app' else {}), **row_links(part, r, k, ui)} for r in keep(got)]
        ran.append(f'{ID}:{part}')
    if not answered and not refused:
        return Sweep(PassResult(ID, False, 'error: ' + '; '.join(errors)))
    status = status_of(errors, [NAMES[p] for p in empty], ran, [NAMES[p] for p in refused])
    summary = {'part': 'summary', 'at': iso(ctx.now), 'source': peer.info.name, 'sourcePrimary': peer.info.primary, 'preset': k.name}
    keys = tuple(ran) + tuple(access.ran_key(ID, part) for part in reached) + (() if ran else (f'{ID}:no-data',))
    return Sweep(PassResult(ID, True, status, found, {}, keys), sections={ID: rows + [summary]})


def read(ctx: Context, args: dict) -> object:
    w = wanted(args)
    peer = ctx.peer('prometheus')
    if peer is None:
        raise ReadError(503, NO_PEER)
    p, k, now = client_for(peer.info, peer.http()), preset_of(ctx.instance.config, watching(peer)), ctx.now
    try:
        if w['op'] == 'clusters':
            found = cluster_rows(p, k, now)
            kept = capped(found)
            more = f'first {len(kept)} of {len(found)} clusters, troubled ones first; ' if len(kept) < len(found) else ''
            return {'clusters': kept, 'note': more + CLUSTERS_NOTE}
        if w['op'] == 'groups':
            rows = [x for x in group_rows(p, k, now) if (not w['cluster'] or x['ns'] == w['cluster']) and (not w['group'] or x['group'] == w['group'])]
            return {'groups': rows[:GROUP_ROWS], 'note': GROUPS_NOTE}
        if w['op'] == 'topics':
            sel = k.sel + (f",{label_is(k.by, w['cluster'])}" if w['cluster'] else '')
            rows: dict[tuple, dict] = {}
            for key, offset in (('msgs', ''), ('msgsD1', ' offset 1d'), ('msgsD7', ' offset 7d')):
                expr = f'topk({TOPIC_ROWS}, sum by ({k.by}, topic) (rate(kafka_topic_partition_current_offset{{{sel},topic!~"__.*"}}[{WINDOW}]{offset})))'
                for m, v in p.vec(expr, now):
                    rows.setdefault((m.get(k.by), m.get('topic')), {'ns': m.get(k.by), 'topic': m.get('topic')})[key] = round(v, 2)
            return {'topics': sorted(rows.values(), key=lambda x: -(x.get('msgs') or 0))[:TOPIC_ROWS], 'note': TOPICS_NOTE}
        if w['op'] == 'apps':
            rows = [dict(x, source=peer.info.name) for x in app_rows(p, now) if not w['job'] or x['job'] == w['job']]
            return {'apps': rows[:APP_ROWS], 'note': APPS_NOTE}
        g = f"{k.sel},consumergroup={json.dumps(w['group'])}" + (f",{label_is(k.by, w['cluster'])}" if w['cluster'] else '')
        if w['op'] == 'group':
            topics: dict[tuple, dict] = {}
            for key, offset in (('lag', ''), ('d1', ' offset 1d'), ('d7', ' offset 7d')):
                for m, v in p.vec(f'sum by ({k.by}, topic) (kafka_consumergroup_lag_sum{{{g}}}{offset})', now):
                    topics.setdefault((m.get(k.by), m.get('topic')), {'ns': m.get(k.by), 'topic': m.get('topic')})[key] = int(v)
            for m, v in p.vec(f'sum by ({k.by}, topic) (rate(kafka_consumergroup_current_offset{{{g}}}[{WINDOW}]))', now):
                if (m.get(k.by), m.get('topic')) in topics:
                    topics[(m.get(k.by), m.get('topic'))]['readPerS'] = round(v, 2)
            members = {m[k.by]: int(v) for m, v in p.vec(f'max by ({k.by}) (kafka_consumergroup_members{{{g}}})', now) if m.get(k.by)}
            return {'group': w['group'], 'members': members, 'topics': sorted(topics.values(), key=lambda x: -(x.get('lag') or 0)), 'note': GROUP_NOTE}
        step = max(60, w['hours'] * 3600 // 120)
        series = p.range_query(f'sum by ({k.by}) (kafka_consumergroup_lag_sum{{{g}}})', now - w['hours'] * 3600, now, step)
        points = [[iso(float(t)), float(v)] for s in series for t, v in (s.get('values') or [])]
        return {'group': w['group'], 'metric': 'lag', 'stepSeconds': step, 'points': points[-120:]}
    except PrometheusError as e:
        if access.denied(e.code):
            raise ReadError(e.code, access.text(NAME, w['op'], credential=credential(peer.info.config), status=e.code)) from None
        raise ReadError(502, e.reason) from None


PLUGIN = Plugin(
    setup=Setup(
        steps=(
            'No credential of its own: Kafka reads through a Prometheus instance. Add that Prometheus instance first and pick it in Prometheus; it signs in as set there.',
            'That Prometheus must hold the kafka_exporter series (kafka_consumergroup_lag_sum, kafka_brokers and others) and, for the calls of each service, the spring_kafka_* timers. Use Preset strimzi for Strimzi clusters, otherwise kafka-exporter.',
        ),
        docs='https://github.com/danielqsj/kafka_exporter',
        docs_label='kafka_exporter',
    ),
    id=ID, name='Kafka', version='0.1.0', category='infrastructure', description='Kafka brokers, partitions, consumer groups and the Kafka calls of each service, read from kafka_exporter and Spring Kafka series in Prometheus.',
    icon='icon.svg', author='Skopax', licence='AGPL-3.0-only', core='>=0.1,<0.8', edition='community', uses=('prometheus',),
    fields=(Field('prometheus', 'Prometheus', 'instance', required=True, choices=('prometheus',), help='The Prometheus instance that holds the kafka_exporter and Spring Kafka series.'),
            Field('preset', 'Preset', 'choice', default=DEFAULT_PRESET, choices=PRESETS,
                  help='strimzi groups the clusters by namespace and adds broker disks and pod restarts; kafka-exporter groups them by the exporter job.'),
            Field('namespaces', 'Namespaces', 'regex', default='.*', help='Namespaces of the Kafka clusters: only kafka_exporter series in namespaces that match are read.')),
    test=test,
    checks=(Check(ID, 'Kafka brokers, consumer groups and service calls', 900, 840, (ID,), run, tool=ID, sections=(ID,), section_help=SECTION_HELP),),
    rules=(RuleSet('rules', 'Kafka thresholds', dict(RULES), RULE_HELP),),
    tools=(Tool(ID, DESCRIPTION, SCHEMA, read, per_run=6, scan=True),),
    links=links, panels=(Panel(ID, 'panel.js', label='Kafka', group='observe'),),
    knowledge=(Knowledge(ID, 'Kafka', (Path(__file__).parent / 'kafka.md').read_text(encoding='utf-8'), slack=True),),
)
