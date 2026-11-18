import json
import re
from collections.abc import Mapping
from dataclasses import dataclass

from skopax.contract import ReadError, anomaly
from skopax.kit import access
from skopax.kit.promql import Prometheus, graph

ID = 'kafka'
WINDOW = '30m'
RULES = {'disk_warn': 80, 'disk_crit': 90, 'gone_lag': 100, 'app_failures': 10, 'app_times': 3}
GROUP_ROWS = 40
APP_ROWS = 30
CLUSTER_ROWS = 100
BUDGET_S = 200
TOPIC_ROWS = 15
OPS = ('clusters', 'groups', 'group', 'topics', 'apps', 'history')
HISTORY_HOURS = 48
NAME = re.compile(r'[A-Za-z0-9._-]{1,120}')
PRESETS = ('strimzi', 'kafka-exporter')
DEFAULT_PRESET = 'kafka-exporter'
LABELS = {'strimzi': 'namespace', 'kafka-exporter': 'job'}
GRAPH = 'Prometheus graph'


@dataclass(frozen=True)
class Preset:
    name: str
    by: str
    sel: str
    skip: str = ''

    @property
    def strimzi(self) -> bool:
        return self.name == 'strimzi'


def kube_watched(peer: Mapping[str, str]) -> bool:
    names = [n.strip().lower() for n in (peer.get('profiles') or '').split(',') if n.strip()]
    return not names or 'kube' in names


def preset_of(config: Mapping[str, str], peer: Mapping[str, str] | None = None) -> Preset:
    name = config.get('preset') or DEFAULT_PRESET
    name = name if name in LABELS else DEFAULT_PRESET
    skip = f",namespace!~{json.dumps(peer.get('namespaces') or '.*')}" if peer is not None and kube_watched(peer) else ''
    return Preset(name, LABELS[name], f"namespace=~{json.dumps(config.get('namespaces') or '.*')}", skip)


def label_is(label: str, value: str) -> str:
    return f'{label}={json.dumps(value)}'


def service(ns: str, job: str) -> str:
    return ','.join(([label_is('namespace', ns)] if ns != '-' else []) + [label_is('job', job)])


def by_label(p: Prometheus, expr: str, now: float, label: str) -> dict[str, float]:
    return {m[label]: v for m, v in p.vec(expr, now) if m.get(label)}


def cluster_rows(p: Prometheus, k: Preset, now: float) -> list[dict]:
    by, sel = k.by, k.sel
    user = f'{sel},topic!~"__.*"'
    expected = by_label(p, f'count by ({by}) (count by ({by}, id) (max_over_time(kafka_broker_info{{{sel}}}[1d])))', now, by)
    brokers = by_label(p, f'max by ({by}) (kafka_brokers{{{sel}}})', now, by)
    held = by_label(p, f'max_over_time(max by ({by}) (kafka_brokers{{{sel}}})[10m:1m])', now, by)
    rows = {c: {'ns': c, 'expected': int(n)} for c, n in expected.items()}
    for c, v in brokers.items():
        rows.setdefault(c, {'ns': c, 'expected': int(v)})['brokers'] = int(v)
    for c, v in held.items():
        if c in rows:
            rows[c]['brokers10'] = int(v)
    for key, expr, digits in (
            ('topics', f'count by ({by}) (kafka_topic_partitions{{{user}}})', 0),
            ('partitions', f'sum by ({by}) (kafka_topic_partitions{{{user}}})', 0),
            ('urp', f'sum by ({by}) (kafka_topic_partition_under_replicated_partition{{{sel}}})', 0),
            ('urp10', f'min_over_time(sum by ({by}) (kafka_topic_partition_under_replicated_partition{{{sel}}})[10m:1m])', 0),
            ('offline', f'count by ({by}) (kafka_topic_partition_leader{{{sel}}} == -1)', 0),
            ('groups', f'count by ({by}) (kafka_consumergroup_members{{{sel}}})', 0),
            ('msgs', f'sum by ({by}) (rate(kafka_topic_partition_current_offset{{{user}}}[{WINDOW}]))', 2),
            ('msgsD1', f'sum by ({by}) (rate(kafka_topic_partition_current_offset{{{user}}}[{WINDOW}] offset 1d))', 2),
            ('msgsD7', f'sum by ({by}) (rate(kafka_topic_partition_current_offset{{{user}}}[{WINDOW}] offset 7d))', 2)):
        for c, v in by_label(p, expr, now, by).items():
            if c in rows:
                rows[c][key] = round(v, digits) if digits else int(v)
    if k.strimzi:
        volumes = f'{sel},persistentvolumeclaim=~"data-.*"'
        disk = f'100 * kubelet_volume_stats_used_bytes{{{volumes}}} / kubelet_volume_stats_capacity_bytes{{{volumes}}}'
        for m, v in p.vec(f'max by (namespace, persistentvolumeclaim) ({disk})', now):
            row = rows.get(m.get('namespace'))
            if row is not None and v > (row.get('diskPct') or -1):
                row.update(diskPct=round(v, 1), diskPvc=m.get('persistentvolumeclaim'))
        for m, v in p.vec(f'sum by (namespace, pod) (increase(kube_pod_container_status_restarts_total{{{sel}{k.skip}}}[{WINDOW}])) > 0.5', now):
            row = rows.get(m.get('namespace'))
            if row is not None and m.get('pod'):
                row.setdefault('restarts', {})[m['pod']] = int(round(v))
    return sorted(rows.values(), key=lambda x: x['ns'])


def troubled(row: dict) -> bool:
    held = row.get('brokers10')
    return held is None or held < (row.get('expected') or 0) or (row.get('offline') or 0) > 0 or (row.get('urp10') or 0) > 0 or bool(row.get('restarts'))


def capped(rows: list[dict]) -> list[dict]:
    if len(rows) <= CLUSTER_ROWS:
        return rows
    first = sorted(rows, key=lambda r: not troubled(r))[:CLUSTER_ROWS]
    return sorted(first, key=lambda x: x['ns'])


def group_rows(p: Prometheus, k: Preset, now: float) -> list[dict]:
    by = f'{k.by}, consumergroup' if k.strimzi else f'{k.by}, namespace, consumergroup'
    sel = k.sel
    rows: dict[tuple, dict] = {}

    def put(expr: str, key: str) -> None:
        for m, v in p.vec(expr, now):
            if m.get(k.by) and m.get('consumergroup'):
                where = {} if k.strimzi or not m.get('namespace') else {'namespace': m['namespace']}
                rows.setdefault((m[k.by], where.get('namespace'), m['consumergroup']), {'ns': m[k.by], **where, 'group': m['consumergroup']})[key] = int(v)

    put(f'max by ({by}) (kafka_consumergroup_members{{{sel}}})', 'members')
    for key, offset in (('lag', ''), ('d1', ' offset 1d'), ('d7', ' offset 7d')):
        put(f'sum by ({by}) (kafka_consumergroup_lag_sum{{{sel}}}{offset})', key)
    put(f'min_over_time(sum by ({by}) (kafka_consumergroup_lag_sum{{{sel}}})[15m:1m])', 'lagMin15')
    put(f'max_over_time(max by ({by}) (kafka_consumergroup_members{{{sel}}})[15m:1m])', 'members15')
    put(f'max_over_time(max by ({by}) (kafka_consumergroup_members{{{sel}}})[1d:10m])', 'members1d')
    return sorted(rows.values(), key=lambda x: (-(x.get('lag') or 0), x['ns'], x['group']))


def app_rows(p: Prometheus, now: float) -> list[dict]:
    by = 'namespace, job'
    rows: dict[tuple, dict] = {}

    def put(expr: str, key: str, digits: int = 0) -> None:
        for m, v in p.vec(expr, now):
            if m.get('job'):
                k = (m.get('namespace') or '-', m['job'])
                rows.setdefault(k, {'ns': k[0], 'job': k[1]})[key] = round(v, digits) if digits else int(round(v))

    for side, metric in (('in', 'spring_kafka_listener_seconds'), ('out', 'spring_kafka_template_seconds')):
        put(f'sum by ({by}) (increase({metric}_count[{WINDOW}]))', side)
        for key, offset in (('Fail', ''), ('FailD1', ' offset 1d'), ('FailD7', ' offset 7d')):
            put(f'sum by ({by}) (increase({metric}_count{{result="failure"}}[{WINDOW}]{offset}))', side + key)
        put(f'1000 * sum by ({by}) (rate({metric}_sum[{WINDOW}])) / sum by ({by}) (rate({metric}_count[{WINDOW}]))', side + 'Ms', 2)
    return sorted(rows.values(), key=lambda x: (-((x.get('inFail') or 0) + (x.get('outFail') or 0)), -((x.get('in') or 0) + (x.get('out') or 0))))


def cluster_cards(rows: list[dict], rules: Mapping[str, float]) -> list[dict]:
    found = []

    def card(row: dict, sig: str, svc: str, sub: str, sev: str, value: float, unit: str, label: str, what: str) -> None:
        x = anomaly(ID, sig, row['ns'], svc, sub, sev, value, None, None, unit, label, what)
        x.update(ranKey='kafka:clusters', kafka='clusters')
        found.append(x)

    for row in rows:
        ns, up, held, want = row['ns'], row.get('brokers'), row.get('brokers10'), row.get('expected') or 0
        for pod, n in sorted((row.get('restarts') or {}).items()):
            card(row, 'kafka-restart', pod, 'restarts', 'warn', n, '', 'Kafka pod restarts', f'{pod} ({ns}) restarted {n} times in the last 30 min.')
        if held is None:
            card(row, 'kafka-exporter', ns, 'exporter', 'warn', 0, '', 'Kafka exporter silent',
                 f'The kafka_exporter of {ns} reported {want} brokers in the last day but nothing for 10 min, so lag, partitions and brokers of {ns} are not watched until it is back.')
            continue
        if held < want:
            card(row, 'kafka-broker', ns, 'brokers', 'crit', up if up is not None else held, '', 'Kafka brokers down',
                 f"{ns} has had at most {held} of its {want} brokers in the cluster metadata for 10 min ({up if up is not None else '?'} now). Partitions led by the missing broker move to the others, "
                 'and topics whose in-sync replicas fall below their minimum stop taking writes.')
        if (row.get('offline') or 0) > 0:
            card(row, 'kafka-offline', ns, 'offline', 'crit', row['offline'], '', 'Kafka partitions without a leader',
                 f"{ns} has {row['offline']} partitions with no leader: producers and consumers of those partitions are stuck until a broker that holds them is back.")
        if (row.get('urp10') or 0) > 0:
            card(row, 'kafka-urp', ns, 'urp', 'warn', row.get('urp') or row['urp10'], '', 'Kafka under-replicated partitions',
                 f"{ns} has had under-replicated partitions for at least 10 min ({row.get('urp', row['urp10'])} now): a replica is behind or a broker is gone, so one more broker problem can make data unavailable.")
        pct = row.get('diskPct')
        if pct is not None and pct >= rules['disk_warn']:
            card(row, 'kafka-disk', row.get('diskPvc') or ns, 'disk', 'crit' if pct >= rules['disk_crit'] else 'warn', pct, '%', 'Kafka broker disk',
                 f"{ns} broker volume {row.get('diskPvc')} is {pct:g} % full. A full log volume takes the broker down; check topic retention (retention.ms / retention.bytes) or grow the volume.")
    return found


def group_cards(rows: list[dict], rules: Mapping[str, float]) -> list[dict]:
    found = []
    for row in rows:
        if (row.get('members15') or 0) == 0 and (row.get('members1d') or 0) > 0 and (row.get('lagMin15') or 0) >= rules['gone_lag']:
            x = anomaly(ID, 'kafka-gone', row['ns'], 'kafka/' + row['group'], 'members', 'warn', row.get('lag') or row['lagMin15'], None, None, '', 'Kafka consumer gone',
                        f"Consumer group {row['group']} ({row['ns']}) has had no members for 15 min while {row.get('lag', row['lagMin15']):,} messages wait; "
                        f"it had {row['members1d']} in the last day. Its service is down, scaled to zero, or cannot reach Kafka.")
            x.update(ranKey='kafka:groups', kafka='groups')
            found.append(x)
    return found


def app_cards(rows: list[dict], rules: Mapping[str, float]) -> list[dict]:
    found = []
    for row in rows:
        for side, sig, label, verb in (('in', 'kafka-consume-err', 'Kafka listener failures', 'listener calls'), ('out', 'kafka-send-err', 'Kafka send failures', 'sends')):
            n, total = row.get(side + 'Fail') or 0, row.get(side) or 0
            base = max(row.get(side + 'FailD1') or 0, row.get(side + 'FailD7') or 0)
            if n >= rules['app_failures'] and n >= rules['app_times'] * base:
                x = anomaly(ID, sig, row['ns'], row['job'], side, 'warn', n, row.get(side + 'FailD1'), row.get(side + 'FailD7'), '', label,
                            f"{row['job']} ({row['ns']}): {n:,} of {total:,} Kafka {verb} failed in the last 30 min, against {row.get(side + 'FailD1', 0)} a day and {row.get(side + 'FailD7', 0)} a week earlier "
                            '(Spring Kafka timers in the service; the exception label names the cause).')
                x.update(ranKey='kafka:app', kafka='app')
                found.append(x)
    return found


def card_urls(card: dict, k: Preset, ui: str) -> list[dict]:
    sig, ns, svc = card.get('sig'), card.get('ns') or '', card.get('svc') or ''
    if sig == 'kafka-gone':
        g = f"{label_is(k.by, ns)},consumergroup={json.dumps(svc.removeprefix('kafka/'))}"
        exprs = (('Lag graph', f'sum by (topic) (kafka_consumergroup_lag_sum{{{g}}})'), ('Members graph', f'kafka_consumergroup_members{{{g}}}'))
    elif card.get('kafka') == 'app':
        metric = 'spring_kafka_listener_seconds_count' if sig == 'kafka-consume-err' else 'spring_kafka_template_seconds_count'
        exprs = ((GRAPH, f'sum by (result, exception) (rate({metric}{{{service(ns, svc)}}}[5m]))'),)
    else:
        sel = label_is(k.by, ns)
        exprs = ((GRAPH, {'kafka-urp': f'sum by (topic) (kafka_topic_partition_under_replicated_partition{{{sel}}}) > 0',
                          'kafka-offline': f'kafka_topic_partition_leader{{{sel}}} == -1',
                          'kafka-disk': f'100 * kubelet_volume_stats_used_bytes{{{sel}}} / kubelet_volume_stats_capacity_bytes{{{sel}}}'}.get(sig, f'kafka_brokers{{{sel}}}')),)
    out = [{'label': label, 'url': graph(expr, ui)} for label, expr in exprs]
    return [u for u in out if u['url']]


def row_links(part: str, row: dict, k: Preset, ui: str) -> dict[str, str]:
    if part == 'clusters':
        found = {'link': graph(f'sum(rate(kafka_topic_partition_current_offset{{{label_is(k.by, row["ns"])},topic!~"__.*"}}[5m]))', ui)}
    elif part == 'groups':
        found = {'link': graph(f"sum by (topic) (kafka_consumergroup_lag_sum{{{label_is(k.by, row['ns'])},consumergroup={json.dumps(row['group'])}}})", ui)}
    else:
        sel = service(row['ns'], row['job'])
        found = {'inLink': graph(f'sum by (result) (rate(spring_kafka_listener_seconds_count{{{sel}}}[5m]))', ui),
                 'outLink': graph(f'sum by (result) (rate(spring_kafka_template_seconds_count{{{sel}}}[5m]))', ui)}
    return {key: url for key, url in found.items() if url}


def status_of(errors: list[str], empty: list[str], ran: list[str], refused: list[str] = ()) -> str:
    if errors:
        return access.status('error: ' + '; '.join(errors) + (f" \u00b7 no data for {', '.join(empty)}" if empty else ''), refused)
    if not ran and not refused:
        return 'ok \u00b7 no data'
    return access.status(f"ok \u00b7 no data for {', '.join(empty)}" if empty else 'ok', refused)


def links(card: dict, info: object) -> list[dict] | None:
    if card.get('src') != ID:
        return None
    return [{'label': u['label'], 'url': u['url']} for u in card.get('urls') or []
            if isinstance(u, dict) and isinstance(u.get('label'), str) and isinstance(u.get('url'), str) and u['url'].startswith('https://')]


def name_of(args: dict, key: str) -> str:
    v = args.get(key)
    v = '' if v is None else str(v).strip()
    if v and not NAME.fullmatch(v):
        raise ReadError(400, f'{key} may hold only letters, digits, dot, dash and underscore')
    return v


def wanted(args: dict) -> dict:
    op = args.get('op') or 'clusters'
    if op not in OPS:
        raise ReadError(400, f"op must be one of {', '.join(OPS)}")
    try:
        hours = int(args.get('hours') or 24)
    except (TypeError, ValueError, OverflowError):
        hours = 0
    if not 1 <= hours <= HISTORY_HOURS:
        raise ReadError(400, f'hours must be 1-{HISTORY_HOURS}')
    w = {'op': op, 'hours': hours, 'cluster': name_of(args, 'cluster'), 'group': name_of(args, 'group'), 'job': name_of(args, 'job')}
    if op in ('group', 'history') and not w['group']:
        raise ReadError(400, f'op {op} needs group (a consumer group such as cart-service)')
    return w
