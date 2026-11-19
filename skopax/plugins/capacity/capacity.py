from collections.abc import Callable

from skopax.contract import PassResult, anomaly, iso
from skopax.errkind import kind
from skopax.kit import access
from skopax.kit.promql import Late, Prometheus, PrometheusError, prom_link

RULES = {'disk_warn': 80, 'disk_crit': 90, 'fill_days': 3, 'fill_floor': 60, 'heap_warn': 85, 'heap_crit': 95}
FS = ('fstype!~"tmpfs|devtmpfs|overlay|squashfs|nsfs|ramfs|iso9660|autofs",'
      'mountpoint!~"/boot.*|/run.*|/snap.*|/etc/.*|/var/lib/docker.*|/var/lib/kubelet.*"')
HPA = 'namespace, horizontalpodautoscaler'
ROWS = 15
KEEP = {'volumes': 25, 'hpa': ROWS, 'heap': ROWS, 'dbdisk': 40}
NAMES = {'volumes': 'volumes', 'hpa': 'autoscalers', 'heap': 'JVM heap', 'dbdisk': 'host disks'}
LIVE = 'jvm_gc_live_data_size_bytes / jvm_gc_max_data_size_bytes > 0'
GRAPH = 'Prometheus graph'
NAME = 'Capacity'
NO_DATA = 'capacity:no-data'
NO_DATA_STATUS = 'ok \u00b7 no data'

Vec = Callable[[str], list[tuple[dict, float]]]


def braces(selector: str) -> str:
    return f'{{{selector}}}' if selector else ''


def volume_expr(ns: str, pvc: str) -> str:
    sel = f'namespace="{ns}",persistentvolumeclaim="{pvc}"'
    return f'100 * kubelet_volume_stats_used_bytes{{{sel}}} / kubelet_volume_stats_capacity_bytes{{{sel}}}'


def hpa_expr(ns: str, name: str) -> str:
    return f'kube_horizontalpodautoscaler_status_current_replicas{{namespace="{ns}",horizontalpodautoscaler="{name}"}}'


def heap_expr(ns: str, job: str) -> str:
    sel = f'namespace="{ns}",job="{job}"'
    return f'100 * jvm_gc_live_data_size_bytes{{{sel}}} / jvm_gc_max_data_size_bytes{{{sel}}}'


def volume_rows(vec: Vec, selector: str) -> list[dict]:
    rows: dict[tuple, dict] = {}
    sel = braces(selector)

    def put(expr: str, key: str, digits: int):
        for m, v in vec(expr):
            if m.get('persistentvolumeclaim'):
                k = (m.get('namespace'), m['persistentvolumeclaim'])
                rows.setdefault(k, {'part': 'volumes', 'ns': m.get('namespace') or '-', 'pvc': m['persistentvolumeclaim']})[key] = round(v, digits)
    by = 'namespace, persistentvolumeclaim'
    put(f'max by ({by}) (100 * kubelet_volume_stats_used_bytes{sel} / kubelet_volume_stats_capacity_bytes{sel})', 'pct', 1)
    put(f'max by ({by}) (kubelet_volume_stats_capacity_bytes{sel}) / 1e9', 'capGb', 1)
    put(f'max by ({by}) (kubelet_volume_stats_available_bytes{sel}) / 1e9', 'freeGb', 1)
    put(f'max by ({by}) (deriv(kubelet_volume_stats_used_bytes{sel}[1d])) * 86400 / 1e9', 'growGbDay', 2)
    for r in rows.values():
        grow, free = r.get('growGbDay') or 0, r.get('freeGb')
        if grow > 0 and free is not None:
            r['daysToFull'] = round(free / grow, 1)
    return sorted(rows.values(), key=lambda x: -(x.get('pct') or 0))


def disk_rows(vec: Vec, selector: str) -> list[dict]:
    rows: dict[tuple, dict] = {}
    sel = braces(selector)
    by = 'instance, node_name, mountpoint'

    def put(expr: str, key: str, digits: int):
        for m, v in vec(expr):
            node = m.get('node_name') or m.get('instance')
            if node and m.get('mountpoint'):
                rows.setdefault((node, m['mountpoint']), {'part': 'dbdisk', 'node': node, 'mount': m['mountpoint']})[key] = round(v, digits)
    put(f'max by ({by}) (100 * (1 - node_filesystem_avail_bytes{sel} / node_filesystem_size_bytes{sel}))', 'pct', 1)
    put(f'max by ({by}) (node_filesystem_size_bytes{sel}) / 1e9', 'capGb', 1)
    put(f'max by ({by}) (node_filesystem_avail_bytes{sel}) / 1e9', 'freeGb', 1)
    put(f'-1 * max by ({by}) (deriv(node_filesystem_avail_bytes{sel}[1d])) * 86400 / 1e9', 'growGbDay', 2)
    for r in rows.values():
        grow, free = r.get('growGbDay') or 0, r.get('freeGb')
        if grow > 0 and free is not None:
            r['daysToFull'] = round(free / grow, 1)
    return sorted([r for r in rows.values() if r.get('capGb', 0) >= 1], key=lambda x: -(x.get('pct') or 0))


def hpa_rows(vec: Vec) -> list[dict]:
    rows: dict[tuple, dict] = {}

    def put(expr: str, key: str):
        for m, v in vec(expr):
            if m.get('horizontalpodautoscaler'):
                k = (m.get('namespace'), m['horizontalpodautoscaler'])
                rows.setdefault(k, {'part': 'hpa', 'ns': m.get('namespace') or '-', 'hpa': m['horizontalpodautoscaler']})[key] = int(round(v))
    put(f'max by ({HPA}) (kube_horizontalpodautoscaler_status_current_replicas)', 'current')
    put(f'max by ({HPA}) (kube_horizontalpodautoscaler_spec_min_replicas)', 'min')
    put(f'max by ({HPA}) (kube_horizontalpodautoscaler_spec_max_replicas)', 'max')
    put(f'min_over_time((max by ({HPA}) (kube_horizontalpodautoscaler_status_current_replicas) - on ({HPA}) max by ({HPA}) (kube_horizontalpodautoscaler_spec_max_replicas))[30m:1m])', 'gap30')
    put(f'max by ({HPA}) (kube_horizontalpodautoscaler_status_target_metric{{metric_name="cpu",metric_target_type="utilization"}})', 'cpu')
    put(f'max by ({HPA}) (kube_horizontalpodautoscaler_spec_target_metric{{metric_name="cpu",metric_target_type="utilization"}})', 'cpuTarget')
    out = [r for r in rows.values() if r.get('max') and r.get('min') is not None and r['max'] > r['min']]
    for r in out:
        r['atMax30'] = r.get('gap30') is not None and r['gap30'] >= 0
    return sorted(out, key=lambda x: (-int(x['atMax30']), -((x.get('current') or 0) / x['max']), -(x.get('cpu') or 0)))


def heap_rows(vec: Vec) -> list[dict]:
    rows: dict[tuple, dict] = {}

    def put(expr: str, key: str, digits: int = 1):
        for m, v in vec(expr):
            if m.get('job'):
                k = (m.get('namespace'), m['job'])
                rows.setdefault(k, {'part': 'heap', 'ns': m.get('namespace') or '-', 'job': m['job']})[key] = round(v, digits)
    put(f'100 * max by (namespace, job) ({LIVE})', 'pct')
    put(f'100 * max by (namespace, job) (min_over_time(({LIVE})[15m:1m]))', 'pct15')
    put('max by (namespace, job) (jvm_gc_max_data_size_bytes) / 1048576', 'maxMb', 0)
    put('1000 * max by (namespace, job) (rate(jvm_gc_pause_seconds_sum[30m]))', 'gcMsPerS', 1)
    return sorted(rows.values(), key=lambda x: -(x.get('pct') or 0))


def volume_cards(rows: list[dict], rules: dict) -> list[dict]:
    found = []
    for r in rows:
        pct, days = r.get('pct'), r.get('daysToFull')
        if pct is None:
            continue
        full = pct >= rules['disk_warn']
        soon = days is not None and days <= rules['fill_days'] and pct >= rules['fill_floor']
        if not (full or soon):
            continue
        sev = 'crit' if pct >= rules['disk_crit'] or (soon and days <= 1) else 'warn'
        trend = f", growing {r['growGbDay']:g} GB a day, so full in about {days:g} days" if days is not None else ''
        x = anomaly('capacity', 'pvc-disk', r['ns'], r['pvc'], 'disk', sev, pct, None, None, '%', 'Volume disk',
                    f"{r['pvc']} ({r['ns']}) is {pct:g} % full ({r.get('freeGb', '?')} GB free of {r.get('capGb', '?')} GB){trend}. "
                    'A full volume stops its pod writing: Elasticsearch turns indices read-only at 95 %, Prometheus and databases stop. '
                    'Check retention or grow the volume.')
        x.update(ranKey='capacity:volumes', capacity='volumes')
        found.append(x)
    return found


def disk_cards(rows: list[dict], rules: dict) -> list[dict]:
    found = []
    for r in rows:
        pct, days = r.get('pct'), r.get('daysToFull')
        if pct is None:
            continue
        soon = days is not None and days <= rules['fill_days'] and pct >= rules['fill_floor']
        if pct < rules['disk_warn'] and not soon:
            continue
        sev = 'crit' if pct >= rules['disk_crit'] or (soon and days <= 1) else 'warn'
        trend = f", growing {r['growGbDay']:g} GB a day, so full in about {days:g} days" if days is not None else ''
        x = anomaly('capacity', 'db-disk', '-', r['node'], r['mount'], sev, pct, None, None, '%', 'Host disk',
                    f"{r['mount']} on {r['node']} is {pct:g} % full ({r.get('freeGb', '?')} GB free of {r.get('capGb', '?')} GB){trend}. "
                    'A full data or log disk stops what runs on that host writing; check data and log growth, archives and old backups on that host.')
        x.update(ranKey='capacity:dbdisk', capacity='dbdisk')
        found.append(x)
    return found


def hpa_cards(rows: list[dict]) -> list[dict]:
    found = []
    for r in rows:
        if not r['atMax30']:
            continue
        cpu, target = r.get('cpu'), r.get('cpuTarget')
        busy = cpu is not None and target is not None and cpu >= target
        load = f' at {cpu} % CPU against its {target} % target' if cpu is not None and target is not None else ''
        x = anomaly('capacity', 'hpa-max', r['ns'], r['hpa'], 'replicas', 'warn' if busy else 'info', r.get('current') or r['max'], None, None, '', 'Autoscaler at max',
                    f"{r['hpa']} ({r['ns']}) has run at its maximum of {r['max']} replicas for 30 min{load}. "
                    + ('It cannot add pods while the load needs more, so latency grows; raise maxReplicas or find what drives the CPU.' if busy else
                       'Its CPU is under target, so it should scale down soon; a long stay at max means the target or the limits are off.'))
        x.update(ranKey='capacity:hpa', capacity='hpa')
        found.append(x)
    return found


def heap_cards(rows: list[dict], rules: dict) -> list[dict]:
    found = []
    for r in rows:
        held = r.get('pct15')
        if held is None or held < rules['heap_warn']:
            continue
        x = anomaly('capacity', 'jvm-heap', r['ns'], r['job'], 'heap', 'crit' if held >= rules['heap_crit'] else 'warn', held, None, None, '%', 'JVM heap after GC',
                    f"{r['job']} ({r['ns']}) kept at least {held:g} % of its old generation ({r.get('maxMb', '?')} MB) live after every garbage collection for 15 min. "
                    'Memory that survives GC is in use, so a leak or an oversized cache is filling the heap; next come long GC pauses, then OutOfMemoryError and a restart.')
        x.update(ranKey='capacity:heap', capacity='heap')
        found.append(x)
    return found


def card_urls(a: dict, ui: str) -> list[dict]:
    ns, svc = a.get('ns') or '', a.get('svc') or ''
    expr = {'pvc-disk': volume_expr, 'hpa-max': hpa_expr, 'jvm-heap': heap_expr}.get(a.get('sig'))
    return [dict(label=GRAPH, url=prom_link(expr(ns, svc), ui, '7d' if a.get('sig') == 'pvc-disk' else '2d'))] if expr and ui else []


def row_link(r: dict, ui: str) -> str | None:
    if not ui:
        return None
    if r['part'] == 'volumes':
        return prom_link(volume_expr(r['ns'], r['pvc']), ui)
    if r['part'] == 'heap':
        return prom_link(heap_expr(r['ns'], r['job']), ui)
    return None


def run_pass(client: Prometheus, now: float, rules: dict, *, source: str, ui: str, credential: str = access.CREDENTIAL, jvm: bool = True, host_disks: bool = True, volume_selector: str = '', fs_selector: str = FS,
             disk_client=None, disk_error: str = '') -> tuple[PassResult, list[dict]]:
    found, ran, read_ok, errors, empty, off, rows, refused, reached = [], [], [], [], [], [], [], [], []
    for part, read, cards in (('volumes', lambda vec: volume_rows(vec, volume_selector), lambda got: volume_cards(got, rules)),
                              ('hpa', hpa_rows, hpa_cards),
                              ('heap', heap_rows, lambda got: heap_cards(got, rules)),
                              ('dbdisk', lambda vec: disk_rows(vec, fs_selector), lambda got: disk_cards(got, rules))):
        if (part == 'heap' and not jvm) or (part == 'dbdisk' and not host_disks):
            off.append(part)
            ran.append(f'capacity:{part}')
            continue
        if part == 'dbdisk' and disk_error:
            errors.append(f'{NAMES[part]}: {disk_error}')
            continue
        seen = []
        reader = disk_client if part == 'dbdisk' and disk_client is not None else client

        def vec(expr: str, seen=seen, reader=reader) -> list[tuple[dict, float]]:
            got = reader.vec(expr, now)
            seen.append(bool(got))
            return got

        try:
            got = read(vec)
        except Late:
            errors.append(f'{NAMES[part]}: timed out')
            continue
        except PrometheusError as e:
            if access.denied(e.code):
                refused.append(part)
                reached.append(part)
                found.append(access.card('capacity', NAME, part, status=e.code, credential='token' if reader is not client else credential, name=NAMES[part]))
                continue
            errors.append(f'{NAMES[part]}: {e.reason}')
            continue
        except Exception as e:
            errors.append(f'{NAMES[part]}: {kind(e)}')
            continue
        reached.append(part)
        if not any(seen):
            empty.append(part)
            continue
        for r in got[:KEEP[part]]:
            link = row_link(r, ui)
            rows.append({**r, 'source': source, **({'link': link} if link else {})})
        for x in cards(got):
            urls = card_urls(x, ui)
            found.append({**x, **({'urls': urls} if urls else {})})
        ran.append(f'capacity:{part}')
        read_ok.append(part)
    ok = bool(read_ok) or bool(refused) or not errors
    if empty:
        ran.append(NO_DATA)
    ran += [access.ran_key('capacity', part) for part in reached + off]
    quiet = f"no data for {', '.join(NAMES[p] for p in empty)}" if empty else ''
    base = 'error: ' + '; '.join(errors) + (f' \u00b7 {quiet}' if quiet else '') if errors else NO_DATA_STATUS if len(empty) == len(KEEP) else f'ok \u00b7 {quiet}' if quiet else 'ok'
    status = access.status(base, [NAMES[p] for p in refused])
    rows.append({'part': 'summary', 'at': iso(now), 'source': source, 'off': off, 'noAccess': refused})
    return PassResult('capacity', ok, status, found, {}, tuple(ran)), rows
