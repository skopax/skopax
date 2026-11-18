import concurrent.futures
import json
import re
import urllib.parse

from collections.abc import Mapping

from skopax.contract import anomaly, fmt, iso
from skopax.kit.alerts import ALERT_KEEP, QUIET_ALERTS
from skopax.kit.promql import POD, WATCHED, Prometheus, out_err, prom_link, scoped

KAFKA_NS = 'kafka-.*'
W = '60m'
OFFS = (1, 7)
TICK_MIN = 30
WARM_S = 1800
PROM_LINK = {
    'err': 'sum by (pod) (increase(log4j2_events_total{{{sel},level="error"}}[60m]))',
    'warn': 'sum by (pod) (increase(log4j2_events_total{{{sel},level="warn"}}[60m]))',
    '5xx': 'sum by (uri, status) (increase(http_server_requests_seconds_count{{{sel},status=~"5.."}}[60m]))',
    'out-err': lambda sel, sub: out_err(sel, '60m', 'clientName, uri, status, exception'),
    'gw-err': 'sum by (http_route) (increase(krakend_proxy_duration_count{{{sel},error="true"}}[60m]))',
    'hikari-tmo': 'sum by (pod) (increase(hikaricp_connections_timeout_total{{{sel}}}[60m]))',
    'traffic-drop': 'sum(increase(http_server_requests_seconds_count{{{sel}}}[60m]))',
    'traffic-spike': 'sum by (uri) (increase(http_server_requests_seconds_count{{{sel}}}[60m]))',
    'avg-latency': 'sum by (uri) (increase(http_server_requests_seconds_sum{{{sel}}}[60m])) / sum by (uri) (increase(http_server_requests_seconds_count{{{sel}}}[60m]))',
    'p95-route': 'histogram_quantile(0.95, sum by (le) (rate(krakend_proxy_duration_bucket{{{sel},http_route="{sub}"}}[60m])))',
}
DETECT_HOURS = 24


def job_id(m):
    return m.get('namespace', ''), m.get('job', ''), ''


def ing_id(m):
    return m.get('exported_namespace', ''), 'ingress/' + m.get('ingress', ''), ''


def route_id(m):
    return m.get('namespace', ''), m.get('job', ''), m.get('http_route', '')


def kafka_id(m):
    return m.get('namespace', ''), 'kafka/' + m.get('consumergroup', ''), ''


def inc(metric, sel, by='namespace, job'):
    return f'sum by ({by}) (increase({metric}{{{sel}}}[{W}]))'


APP = f'namespace=~{WATCHED}'
INGRESS_EXCLUDE = '<ingress-exclude>'
ING = f'exported_namespace=~{WATCHED}{INGRESS_EXCLUDE}'
RATES = [
    dict(sig='err', label='ERROR logs', q=inc('log4j2_events_total', APP + ',level="error"'), slack=10, sev='warn'),
    dict(sig='warn', label='WARN logs', q=inc('log4j2_events_total', APP + ',level="warn"'), slack=100, sev='info'),
    dict(sig='5xx', label='5xx responses', q=inc('http_server_requests_seconds_count', APP + ',status=~"5.."'), slack=5, sev='warn', req='req'),
    dict(sig='out-err', label='failed outbound calls', q=out_err(APP, W, 'namespace, job'), slack=10, sev='warn'),
    dict(sig='gw-err', label='gateway errors', q=inc('krakend_proxy_duration_count', APP + ',error="true"'), slack=20, sev='warn', req='gw-req'),
    dict(sig='ing-5xx', label='ingress 5xx', q=inc('nginx_ingress_controller_requests', ING + ',status=~"5.."', 'exported_namespace, ingress'), slack=10, sev='warn', ident=ing_id, req='ing-req'),
    dict(sig='hikari-tmo', label='DB pool timeouts', q=inc('hikaricp_connections_timeout_total', APP), slack=1, sev='warn'),
]
TRAFFIC = [
    dict(sig='req', label='requests', q=inc('http_server_requests_seconds_count', APP)),
    dict(sig='gw-req', label='gateway requests', q=inc('krakend_proxy_duration_count', APP)),
    dict(sig='ing-req', label='ingress requests', q=inc('nginx_ingress_controller_requests', ING, 'exported_namespace, ingress'), ident=ing_id),
]
LATENCY = [
    dict(sig='p95-route', label='gateway route p95',
         q=f'histogram_quantile(0.95, sum by (le, namespace, job, http_route) (rate(krakend_proxy_duration_bucket{{{APP}}}[{W}])))',
         n=inc('krakend_proxy_duration_count', APP, 'namespace, job, http_route'), ident=route_id, min=300, delta=0.25),
    dict(sig='p95-ingress', label='ingress p95',
         q=f'histogram_quantile(0.95, sum by (le, exported_namespace, ingress) (rate(nginx_ingress_controller_request_duration_seconds_bucket{{{ING}}}[{W}])))',
         n=inc('nginx_ingress_controller_request_duration_seconds_count', ING, 'exported_namespace, ingress'), ident=ing_id, min=300, delta=0.25),
    dict(sig='avg-latency', label='average response time',
         q=f'{inc("http_server_requests_seconds_sum", APP)} / {inc("http_server_requests_seconds_count", APP)}',
         n=inc('http_server_requests_seconds_count', APP), min=300, delta=0.1),
]
LAG = dict(sig='kafka-lag', label='consumer lag',
           q=f'min_over_time(sum by (namespace, consumergroup) (kafka_consumergroup_lag_sum{{namespace=~"{KAFKA_NS}"}})[10m:1m])', ident=kafka_id, slack=1000)
HARD = dict(
    restarts=f'sum by (namespace, pod) (increase(kube_pod_container_status_restarts_total{{{APP}}}[{TICK_MIN}m])) > 0.5',
    reason=f'kube_pod_container_status_last_terminated_reason{{{APP},reason!="Completed"}} == 1',
    waiting=f'kube_pod_container_status_waiting_reason{{{APP},reason!~"ContainerCreating|PodInitializing"}} == 1',
    unavail=f'min_over_time(kube_deployment_status_replicas_unavailable{{{APP}}}[10m]) > 0',
    noready=f'(kube_deployment_status_replicas_available{{{APP}}} == 0) and on (namespace, deployment) (kube_deployment_spec_replicas{{{APP}}} > 0)',
    notready='kube_node_status_condition{condition="Ready",status="true"} == 0',
    pressure='kube_node_status_condition{condition=~"MemoryPressure|DiskPressure|PIDPressure",status="true"} == 1',
)
PROFILES = ('spring', 'krakend', 'ingress', 'kube', 'kafka')
PROFILE_OF = {
    'err': 'spring', 'warn': 'spring', '5xx': 'spring', 'out-err': 'spring', 'hikari-tmo': 'spring', 'req': 'spring', 'avg-latency': 'spring',
    'gw-err': 'krakend', 'gw-req': 'krakend', 'p95-route': 'krakend',
    'ing-5xx': 'ingress', 'ing-req': 'ingress', 'p95-ingress': 'ingress',
    'kafka-lag': 'kafka',
    'restarts': 'kube', 'reason': 'kube', 'waiting': 'kube', 'unavail': 'kube', 'noready': 'kube', 'notready': 'kube', 'pressure': 'kube', 'detect': 'kube',
}
PROFILE_SHOWN = 40


def profiles_of(text: str | None) -> tuple[frozenset[str], str | None]:
    names = [n.strip().lower() for n in (text or '').split(',') if n.strip()]
    bad = next((n for n in names if n not in PROFILES), None)
    if bad is not None:
        return frozenset(), f'unknown profile "{bad[:PROFILE_SHOWN]}" (use {", ".join(PROFILES[:-1])} or {PROFILES[-1]})'
    return frozenset(names or PROFILES), None


def excluding(query: str, pattern: str = '') -> str:
    return query.replace(INGRESS_EXCLUDE, f',ingress!~{json.dumps(pattern)}' if pattern else '')


def prom_pass(client: Prometheus, now: float, profiles: frozenset[str] = frozenset(PROFILES), ingress_exclude: str = '') -> tuple[dict, dict, list[dict], list[tuple]]:
    ats = [now] + [now - o * 86400 for o in OFFS]
    plan = {}
    for spec in RATES + TRAFFIC + [LAG]:
        for i, at in enumerate(ats):
            plan[(spec['sig'], i)] = (spec['q'], at, spec.get('ident', job_id))
    for spec in LATENCY:
        for i, at in enumerate(ats):
            plan[(spec['sig'], i)] = (spec['q'], at, spec.get('ident', job_id))
            plan[(spec['sig'] + '#n', i)] = (spec['n'], at, spec.get('ident', job_id))

    def on(key: str) -> bool:
        return PROFILE_OF[key.removesuffix('#n')] in profiles

    with concurrent.futures.ThreadPoolExecutor(10) as ex:
        futs = {k: ex.submit(client.series, scoped(excluding(q, ingress_exclude), client.namespaces), at, ident) for k, (q, at, ident) in plan.items() if on(k[0])}
        hard_f = {k: ex.submit(client.vec, scoped(q, client.namespaces), now) for k, q in HARD.items() if on(k)}
        alerts_f = ex.submit(client.call, 'alerts')
        deploys_f = ex.submit(client.detect, client.namespaces, DETECT_HOURS, now) if on('detect') else None
        data = {k: {s: v for s, v in futs[k].result().items() if WATCHED not in plan[k][0] or client.watches(s[0])} if k in futs else {} for k in plan}
        hard = {k: [(m, v) for m, v in hard_f[k].result() if WATCHED not in HARD[k] or client.watches(m.get('namespace', ''))] if k in hard_f else [] for k in HARD}
        alerts = alerts_f.result().get('alerts', [])
        deploys = deploys_f.result() if deploys_f is not None else []
    return data, hard, alerts, deploys


FLOORS = {s['sig']: s['slack'] for s in RATES + [LAG]}


def evaluate_prom(now, data, hard, alerts, deploys, image_by=None, floors: Mapping[str, float] | None = None, segment: str | None = None):
    floors = {**FLOORS, **(floors or {})}
    found, svc_rows, routes = [], {}, []
    warm = {(ns, svc) for ns, svc, _, _, t in deploys if t > now - WARM_S}

    def row(ns, svc):
        kind = 'gateway' if svc.startswith('api-gateway') else 'ingress' if svc.startswith('ingress/') else 'kafka' if svc.startswith('kafka/') else 'app'
        return svc_rows.setdefault((ns, svc), dict(ns=ns, svc=svc, kind=kind))

    def trio(sig):
        return data[(sig, 0)], data[(sig, 1)], data[(sig, 2)]

    for spec in TRAFFIC:
        cur, b1, b7 = trio(spec['sig'])
        for k, v in cur.items():
            row(k[0], k[1])[spec['sig']] = round(v)
            lo = min(b1.get(k, 0), b7.get(k, 0))
            hi = max(b1.get(k, 0), b7.get(k, 0))
            if lo >= 600 and v < 0.3 * lo:
                found.append(anomaly('prom', 'traffic-drop', k[0], k[1], '', 'warn', v, b1.get(k, 0), b7.get(k, 0), '', spec['label'],
                                     f'{spec["label"]} in the last 60 min fell to {fmt(v)}, the same window had {fmt(b1.get(k, 0))} a day ago and {fmt(b7.get(k, 0))} a week ago', hi))
            elif v > 3 * hi + 3000:
                found.append(anomaly('prom', 'traffic-spike', k[0], k[1], '', 'info', v, b1.get(k, 0), b7.get(k, 0), '', spec['label'],
                                     f'{spec["label"]} in the last 60 min rose to {fmt(v)}, more than three times the baseline', 3000))
        for k, v in b1.items():
            if k not in cur and min(v, b7.get(k, 0)) >= 600:
                found.append(anomaly('prom', 'traffic-drop', k[0], k[1], '', 'warn', 0, v, b7.get(k, 0), '', spec['label'],
                                     f'no {spec["label"]} in the last 60 min, the same window had {fmt(v)} a day ago', max(v, b7.get(k, 0))))
    for spec in RATES:
        cur, b1, b7 = trio(spec['sig'])
        for k, v in cur.items():
            r = row(k[0], k[1])
            r[spec['sig']] = round(v)
            d1, d7 = b1.get(k, 0), b7.get(k, 0)
            base = max(d1, d7)
            slack = floors[spec['sig']]
            if v > max(2 * base, base + slack):
                sev = spec['sev']
                req = r.get(spec.get('req', ''), 0) if spec.get('req') else 0
                share = f', {v / req:.1%} of {fmt(req)} requests' if req else ''
                if spec.get('req') and v >= 20 and req and v / req > 0.02:
                    sev = 'crit'
                found.append(anomaly('prom', spec['sig'], k[0], k[1], '', sev, v, d1, d7, '', spec['label'],
                                     f'{fmt(v)} {spec["label"]} in the last 60 min{share}, the same window had {fmt(d1)} a day ago and {fmt(d7)} a week ago', slack))
    for spec in LATENCY:
        cur, b1, b7 = trio(spec['sig'])
        n0, n1, n7 = trio(spec['sig'] + '#n')
        for k, v in cur.items():
            ms, n = v * 1000, n0.get(k, 0)
            if spec['sig'] == 'avg-latency':
                row(k[0], k[1])['avgMs'] = round(ms)
            elif spec['sig'] == 'p95-ingress':
                row(k[0], k[1])['p95Ms'] = round(ms)
            if spec['sig'] == 'p95-route':
                routes.append(dict(ns=k[0], gw=k[1], route=k[2], req=round(n), p95=round(ms), d1=round(b1.get(k, 0) * 1000), d7=round(b7.get(k, 0) * 1000)))
            if n < spec['min'] or (k[0], k[1]) in warm:
                continue
            bases = [b.get(k) for b, bn in ((b1, n1), (b7, n7)) if b.get(k) is not None and bn.get(k, 0) >= spec['min'] / 3]
            if not bases:
                continue
            base = max(bases)
            if v > 2 * base and v - base >= spec['delta']:
                d1, d7 = b1.get(k), b7.get(k)
                found.append(anomaly('prom', spec['sig'], k[0], k[1], k[2], 'warn', ms, None if d1 is None else d1 * 1000, None if d7 is None else d7 * 1000, 'ms', spec['label'],
                                     f'{spec["label"]} {fmt(ms, "ms")} over {fmt(n)} requests in the last 60 min, the baseline is {fmt(base * 1000, "ms")}', spec['delta'] * 1000))
    cur, b1, b7 = trio(LAG['sig'])
    for k, v in cur.items():
        r = row(k[0], k[1])
        r['lag'] = round(v)
        base = max(b1.get(k, 0), b7.get(k, 0))
        if v > max(2 * base, base + floors[LAG['sig']]):
            note = ', so the logs pass may be behind' if 'logstash' in k[1] else ''
            found.append(anomaly('prom', 'kafka-lag', k[0], k[1], '', 'warn', v, b1.get(k, 0), b7.get(k, 0), '', LAG['label'],
                                 f'consumer lag has stayed at or above {fmt(v)} for 10 min{note}', floors[LAG['sig']]))
    reasons = {}
    for m, _ in hard['reason']:
        reasons.setdefault((m['namespace'], m['pod']), m.get('reason', ''))
    per_dep = {}
    for m, v in hard['restarts']:
        p = POD.match(m['pod'])
        dep = p.group(1) if p else m['pod']
        agg = per_dep.setdefault((m['namespace'], dep), [0, set()])
        agg[0] += round(v)
        if reasons.get((m['namespace'], m['pod'])):
            agg[1].add(reasons[(m['namespace'], m['pod'])])
    for (ns, dep), (n, why) in per_dep.items():
        row(ns, dep)['restarts'] = n
        sev = 'crit' if n >= 3 or 'OOMKilled' in why else 'warn'
        why_s = f' (last exit: {", ".join(sorted(why))})' if why else ''
        found.append(anomaly('kube', 'restarts', ns, dep, '', sev, n, None, None, '', 'container restarts', f'{n} container restarts in the last {TICK_MIN} min{why_s}', 1,
                             f'{sev.upper()} {dep} {n} restarts in {TICK_MIN} min{why_s}'))
    for m, _ in hard['waiting']:
        p = POD.match(m['pod'])
        dep = p.group(1) if p else m['pod']
        found.append(anomaly('kube', 'waiting', m['namespace'], dep, m.get('reason', ''), 'crit', 1, None, None, '', 'pod stuck',
                             f'pod {m["pod"]} is stuck in {m.get("reason", "?")}'))
    for m, v in hard['unavail']:
        if (m['namespace'], m['deployment']) in warm:
            continue
        found.append(anomaly('kube', 'unavailable', m['namespace'], m['deployment'], '', 'warn', v, None, None, '', 'unavailable replicas',
                             f'{fmt(v)} replicas have been unavailable for the whole last 10 min', 1, f'WARN {m["deployment"]} {fmt(v)} replicas unavailable for 10 min'))
    for m, _ in hard['noready']:
        found.append(anomaly('kube', 'no-ready-pods', m['namespace'], m['deployment'], '', 'crit', 0, None, None, '', 'ready pods',
                             'the deployment has no ready pods'))
    node_ns = f'cluster-{segment}' if segment else 'cluster'
    for m, _ in hard['notready']:
        found.append(anomaly('kube', 'node-not-ready', node_ns, m.get('node', '?'), '', 'crit', 1, None, None, '', 'node not ready', f'node {m.get("node")} is NotReady'))
    for m, _ in hard['pressure']:
        found.append(anomaly('kube', 'node-pressure', node_ns, m.get('node', '?'), m.get('condition', ''), 'warn', 1, None, None, '', 'node pressure',
                             f'node {m.get("node")} reports {m.get("condition")}'))
    for a in alerts:
        lab = a.get('labels', {})
        name = lab.get('alertname', '?')
        if a.get('state') != 'firing' or name in QUIET_ALERTS:
            continue
        sev = {'critical': 'crit', 'warning': 'warn'}.get(lab.get('severity', ''), 'info')
        ident = ','.join([f'cluster={segment}'] * bool(segment) + [f'{k}={lab[k]}' for k in ALERT_KEEP if k in lab])
        svc = lab.get('job') or lab.get('service') or lab.get('deployment') or lab.get('ingress') or lab.get('consumergroup') or lab.get('domain') or '-'
        ann = a.get('annotations', {})
        text = (ann.get('summary') or ann.get('description') or ann.get('message') or name).replace('\n', ' ')[:200]
        x = anomaly('alert', 'alert', lab.get('namespace', '-'), svc, name, sev, 1, None, None, '', name, f'Prometheus alert {name} firing since {a.get("activeAt", "?")[:16].replace("T", " ")}Z: {text}')
        x['key'] = f'alert|{name}|{ident}'
        x['short'] = f'{sev.upper()} alert {name}' + (f' {svc}' if svc != '-' else '')
        found.append(x)
    dep_rows = []
    for ns, svc, old, new, t in deploys:
        dep_rows.append(dict(ns=ns, svc=svc, old=old, new=new, at=iso(t), warm=t > now - WARM_S))
        r = row(ns, svc)
        r['deployedAt'] = iso(t)
        r['image'] = new
        r['imageBy'] = image_by(ns, svc, new) if image_by else None
    routes.sort(key=lambda x: -x['req'])
    return found, svc_rows, routes[:30], dep_rows, warm


def links_for(a: dict, ui: str, namespaces: str = '.*') -> list[dict] | None:
    if a.get('src') not in ('prom', 'kube', 'alert'):
        return None
    out = []
    sig, ns, svc = a.get('sig'), a.get('ns', '-'), a.get('svc', '')
    if a.get('src') == 'prom' and sig in PROM_LINK and re.fullmatch(namespaces, ns) and not svc.startswith(('ingress/', 'kafka/')):
        expr, sel = PROM_LINK[sig], f'namespace="{ns}",job="{svc}"'
        out.append(dict(label='Prometheus graph', url=prom_link(expr(sel, a.get('sub', '')) if callable(expr) else expr.format(sel=sel, sub=a.get('sub', '')), ui)))
    elif sig in ('ing-5xx', 'p95-ingress', 'traffic-drop', 'traffic-spike') and svc.startswith('ingress/'):
        ing = svc.split('/', 1)[1]
        expr = (f'sum by (status) (increase(nginx_ingress_controller_requests{{exported_namespace="{ns}",ingress="{ing}"}}[60m]))' if sig != 'p95-ingress' else
                f'histogram_quantile(0.95, sum by (le) (rate(nginx_ingress_controller_request_duration_seconds_bucket{{exported_namespace="{ns}",ingress="{ing}"}}[60m])))')
        out.append(dict(label='Prometheus graph', url=prom_link(expr, ui)))
    elif sig == 'kafka-lag':
        out.append(dict(label='Prometheus graph', url=prom_link(f'sum by (topic) (kafka_consumergroup_lag_sum{{namespace="{ns}",consumergroup="{svc.split("/", 1)[1]}"}})', ui)))
    elif sig in ('restarts', 'unavailable', 'no-ready-pods', 'waiting'):
        out.append(dict(label='Prometheus graph', url=prom_link(f'sum by (pod) (kube_pod_container_status_restarts_total{{namespace="{ns}",pod=~"{svc}-.*"}})', ui)))
    elif sig == 'alert':
        out.append(dict(label='Alert in Prometheus', url=f'{ui}/alerts?search=' + urllib.parse.quote(a.get('sub') or a.get('label', ''))))
    return out


def deploy_docs(found: list[tuple[str, str, str, str, float]]) -> list[dict]:
    return [{k: v for k, v in dict(ns=ns, svc=svc, fromImage=old, toImage=new, at=iso(t)).items() if v} for ns, svc, old, new, t in found if ns and svc and new]
