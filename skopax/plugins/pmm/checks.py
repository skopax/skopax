import concurrent.futures
import json
import re
import urllib.parse

from skopax import clock
from skopax.contract import anomaly, fmt, iso
from skopax.kit.text import duration

W = '60m'
OFFS = (1, 7)
DASH = '\u2013'
PMM_RULE = dict(sat_warn=70, sat_crit=85, jump=1.5, jump_slack=200, idletx=20, longtx=600, lag=60, lock=5, lock_x=2, lock_slack=2)
QAN_RULE = dict(load=120, load_crit=1800, slow_ms=50, slow_calls=500, slow_min_s=30, calls=200000, total=900, top=12, rows_min_s=5, rows_min_calls=100000,
                stall_x=20, stall_ms=50, stall_s=30)
MONGO_RULE = dict(sat_warn=70, sat_crit=85, jump=1.5, jump_slack=200, lag=60, lat_x=2, lat_ms=5, lat_ops=1000, queue=20)
MONGO_STATES = {0: 'STARTUP', 1: 'PRIMARY', 2: 'SECONDARY', 3: 'RECOVERING', 5: 'STARTUP2', 6: 'UNKNOWN', 7: 'ARBITER', 8: 'DOWN', 9: 'ROLLBACK', 10: 'REMOVED'}
SET_CARDS = ('mongo-no-primary', 'mongo-election')
DEFAULT_SET = 'replica-set'
QAN_ROWS = 1000
QAN_TOTALS = 'qan:totals'
NOT_RE2 = re.compile(r'\(\?<?[=!]|\(\?P=|\(\?>|\\[1-9]|\\[kg]<')
DASHBOARDS = {'pg': 'postgresql-instance-summary/postgresql-instance-summary', 'repl': 'postgresql-replication-overview/postgresql-replication-overview',
              'qan': 'pmm-qan/pmm-query-analytics', 'mongo': 'mongodb-instance-summary/mongodb-instance-summary', 'set': 'mongodb-replicaset-summary/mongodb-replset-summary'}


def watched(pattern: str, name: str) -> bool:
    return re.fullmatch(pattern or '.*', name or '') is not None


def unsupported(pattern: str, label: str) -> str | None:
    return f'{label} uses a lookaround or a backreference, which PMM cannot run' if NOT_RE2.search(pattern or '') else None


def users(text: str) -> list[str]:
    return [u.strip() for u in (text or '').split(',') if u.strip()]


def literal(text: str) -> str:
    return "'" + text.replace('\\', '\\\\').replace("'", "\\'") + "'"


def pg_queries(services: str) -> dict[str, str]:
    sel = f'service_name=~{json.dumps(services or ".*")}'
    return dict(
        up=f'max by (service_name) (pg_up{{{sel}}})',
        maxconn=f'max by (service_name) (pg_settings_max_connections{{{sel}}})',
        conn=f'max_over_time(sum by (service_name) (pg_stat_activity_count{{{sel}}})[{W}:1m])',
        idletx=f'max_over_time(sum by (service_name) (pg_stat_activity_count{{{sel},state=~"idle in transaction.*"}})[{W}:1m])',
        longtx=f'max by (service_name, datname, usename, state) (pg_stat_activity_max_tx_duration{{{sel},state!="idle"}})',
        lag=f'max by (service_name) (min_over_time(pg_replication_lag_seconds{{{sel}}}[10m]) and on (service_name) (pg_replication_is_replica{{{sel}}} == 1))',
        deadlocks=f'sum by (service_name, datname) (increase(pg_stat_database_deadlocks{{{sel}}}[{W}]))',
        lockwait=f'max_over_time(sum by (service_name) (last_over_time(pg_lock_conflicts_count{{{sel}}}[1m]))[{W}:1m])',
        lockmins=f'count_over_time((sum by (service_name) (last_over_time(pg_lock_conflicts_count{{{sel}}}[1m])) > 0)[{W}:1m])',
    )


def mongo_queries(services: str) -> dict[str, str]:
    sel = f'service_name=~{json.dumps(services or ".*")}'
    return dict(
        up=f'max by (service_name) (mongodb_up{{{sel}}})',
        state=f'max by (service_name, set) (mongodb_mongod_replset_my_state{{{sel}}})',
        statechg=f'max by (service_name) (changes(mongodb_mongod_replset_my_state{{{sel}}}[{W}]))',
        conn=f'max by (service_name) (max_over_time(mongodb_connections{{{sel},state="current"}}[{W}]))',
        avail=f'max by (service_name) (mongodb_connections{{{sel},state="available"}})',
        lag=f'min by (name) (min_over_time(mongodb_mongod_replset_member_replication_lag{{{sel}}}[10m]))',
        names=f'max by (service_name, name, node_name) (mongodb_mongod_replset_my_name{{{sel}}})',
        ops=f'sum by (service_name) (increase(mongodb_op_counters_total{{{sel}}}[{W}]))',
        lat=f'sum by (service_name, type) (increase(mongodb_mongod_op_latencies_latency_total{{{sel}}}[{W}]))',
        latn=f'sum by (service_name, type) (increase(mongodb_mongod_op_latencies_ops_total{{{sel}}}[{W}]))',
        queue=f'max by (service_name) (max_over_time(sum by (service_name) (mongodb_mongod_global_lock_current_queue{{{sel}}})[{W}:1m]))',
    )


def metrics_pass(client, queries: dict, now: float, skip=lambda k, i: False) -> dict:
    ats = [now] + [now - o * 86400 for o in OFFS]
    with concurrent.futures.ThreadPoolExecutor(6) as ex:
        futs = {(k, i): ex.submit(client.vec, q, t) for k, q in queries.items() for i, t in enumerate(ats) if not skip(k, i)}
        return {k: f.result() for k, f in futs.items()}


def pmm_pass(client, now: float, services: str) -> dict:
    client.uid('metrics')
    return metrics_pass(client, pg_queries(services), now)


def mongo_pass(client, now: float, services: str) -> dict:
    client.uid('metrics')
    return metrics_pass(client, mongo_queries(services), now, lambda k, i: k == 'names' and i > 0)


def qan_pass(client, now: float, services: str, skip_users: list[str], rules: dict) -> tuple[list[dict], list[dict]]:
    client.uid('qan')
    hours = 24 * max(OFFS) + 2
    with concurrent.futures.ThreadPoolExecutor(2) as ex:
        q = ex.submit(client.qan, qan_sql(now, True, services, skip_users, rules), hours, now)
        t = ex.submit(client.qan, qan_sql(now, False, services, skip_users, rules), hours, now)
        return q.result(), t.result()


def _number(v: float) -> str:
    return f'{float(v):g}'


def pmm_item(svc, sig, sev, now, d1, d7, label, what, slack, short, unit=''):
    x = anomaly('pmm', 'pg-' + sig, 'postgres', svc, '', sev, now, d1, d7, unit, label, what, slack, short)
    x['key'] = f'pmm|{svc}|{sig}'
    return x


def evaluate_pmm(data: dict, rules: dict, services: str = '.*') -> tuple[list[dict], list[dict]] | None:
    n = 1 + len(OFFS)

    def per_svc(k, i):
        out = {}
        for m, v in data[(k, i)]:
            s = m.get('service_name', '')
            if watched(services, s):
                out[s] = max(out.get(s, v), v)
        return out

    def trio(k):
        return [per_svc(k, i) for i in range(n)]

    up, conn, idle, lag, longtx = trio('up'), trio('conn'), trio('idletx'), trio('lag'), trio('longtx')
    lockw, lockm = trio('lockwait'), trio('lockmins')
    expected = sorted(set().union(*up))
    if not expected:
        return None
    maxconn = per_svc('maxconn', 0)
    top_tx = {}
    for m, v in data[('longtx', 0)]:
        s = m.get('service_name', '')
        if v >= top_tx.get(s, ({}, -1))[1]:
            top_tx[s] = (m, v)
    dead = []
    for i in range(n):
        agg = {}
        for m, v in data[('deadlocks', i)]:
            s = agg.setdefault(m.get('service_name', ''), [0, {}])
            s[0] += v
            if v >= 0.5:
                s[1][m.get('datname', '?')] = v
        dead.append(agg)
    found, rows = [], []
    for svc in expected:
        r = dict(svc=svc, up=up[0].get(svc), maxconn=maxconn.get(svc), conn=conn[0].get(svc), connD1=conn[1].get(svc), connD7=conn[2].get(svc),
                 idletx=idle[0].get(svc), longtx=longtx[0].get(svc), lag=lag[0].get(svc), deadlocks=round(dead[0].get(svc, [0])[0]),
                 lockMax=lockw[0].get(svc, 0) if svc in up[0] else None, lockMins=round(lockm[0].get(svc, 0)),
                 lockMaxD1=lockw[1].get(svc, 0), lockMaxD7=lockw[2].get(svc, 0), lockMinsD1=round(lockm[1].get(svc, 0)), lockMinsD7=round(lockm[2].get(svc, 0)))
        rows.append(r)
        if svc not in up[0]:
            found.append(pmm_item(svc, 'down', 'crit', 0, up[1].get(svc), up[2].get(svc), 'not reported by PMM',
                                  f'PMM has no pg_up sample for {svc}: its postgres_exporter or pmm-agent stopped reporting, or the instance is gone', 1,
                                  f'CRIT {svc} not reported by PMM'))
            continue
        if up[0][svc] < 0.5:
            found.append(pmm_item(svc, 'down', 'crit', 0, up[1].get(svc), up[2].get(svc), 'Postgres down',
                                  f'{svc} is down: pg_up is 0 on every PMM collector', 1, f'CRIT {svc} Postgres down (pg_up 0)'))
            continue
        c, cap = conn[0].get(svc), maxconn.get(svc)
        d1, d7 = conn[1].get(svc), conn[2].get(svc)
        base_txt = f'the same window had {fmt(d1) if d1 is not None else "no data"} a day ago and {fmt(d7) if d7 is not None else "no data"} a week ago'
        if c is not None and cap:
            pct = 100 * c / cap
            r['pct'] = round(pct, 1)
            if pct >= rules['sat_warn']:
                sev = 'crit' if pct >= rules['sat_crit'] else 'warn'
                found.append(pmm_item(svc, 'saturation', sev, pct, None if d1 is None else 100 * d1 / cap, None if d7 is None else 100 * d7 / cap, 'connection saturation',
                                      f'{svc} peaked at {fmt(c)} of {fmt(cap)} connections in the last 60 min ({fmt(pct, "%")} of max_connections), {base_txt}', 5,
                                      f'{sev.upper()} {svc} connections {fmt(c)}/{fmt(cap)} ({fmt(pct, "%")})', '%'))
        bases = [b for b in (d1, d7) if b is not None]
        if c is not None and bases:
            base = max(bases)
            if c > max(rules['jump'] * base, base + rules['jump_slack']):
                found.append(pmm_item(svc, 'conn-jump', 'warn', c, d1, d7, 'connection jump',
                                      f'{svc} peaked at {fmt(c)} connections in the last 60 min (max_connections {fmt(cap or 0)}), {base_txt}', rules['jump_slack'],
                                      f'WARN {svc} connections jumped to {fmt(c)} (1d {fmt(d1 or 0)} / 7d {fmt(d7 or 0)})'))
        v = idle[0].get(svc) or 0
        if v > rules['idletx']:
            found.append(pmm_item(svc, 'idle-in-tx', 'warn', v, idle[1].get(svc), idle[2].get(svc), 'idle in transaction',
                                  f'{svc} had up to {fmt(v)} connections idle in transaction in the last 60 min; each holds its locks and a connection slot', rules['idletx'],
                                  f'WARN {svc} {fmt(v)} connections idle in transaction'))
        m, v = top_tx.get(svc, ({}, 0))
        tx_base = max([b for b in (longtx[1].get(svc), longtx[2].get(svc)) if b is not None] or [0])
        if v > rules['longtx'] and v > max(2 * tx_base, tx_base + rules['longtx']):
            who = ', '.join(f'{k} {m[k]}' for k in ('datname', 'usename') if m.get(k))
            found.append(pmm_item(svc, 'long-tx', 'warn', v, longtx[1].get(svc), longtx[2].get(svc), 'longest open transaction (s)',
                                  f'a transaction on {svc} has been open for {duration(v)} ({m.get("state", "?")}; {who}); the same moment had {duration(longtx[1].get(svc))} a day ago '
                                  f'and {duration(longtx[2].get(svc))} a week ago',
                                  rules['longtx'], f'WARN {svc} transaction open {duration(v)} ({m.get("datname", "?")})'))
        v = lag[0].get(svc)
        lag_base = max([b for b in (lag[1].get(svc), lag[2].get(svc)) if b is not None] or [0])
        if v is not None and v > rules['lag'] and v > lag_base + rules['lag']:
            found.append(pmm_item(svc, 'repl-lag', 'warn', v, lag[1].get(svc), lag[2].get(svc), 'replication lag (s)',
                                  f'{svc} has stayed at least {duration(v)} behind its primary for 10 min; the same window had {duration(lag[1].get(svc))} a day ago '
                                  f'and {duration(lag[2].get(svc))} a week ago',
                                  rules['lag'], f'WARN {svc} replication lag {duration(v)}'))
        w, wmin = lockw[0].get(svc, 0), round(lockm[0].get(svc, 0))
        w1, w7 = lockw[1].get(svc, 0), lockw[2].get(svc, 0)
        lock_base = max(w1, w7)
        if w >= rules['lock'] and w > max(rules['lock_x'] * lock_base, lock_base + rules['lock_slack']):
            found.append(pmm_item(svc, 'lock-waits', 'warn', w, w1, w7, 'sessions waiting on locks',
                                  f'up to {fmt(w)} sessions waited on a lock on {svc} in the last 60 min, in {wmin} of the sampled minutes '
                                  f'(pg_lock_conflicts_count is sampled at its collector\'s resolution, so a 60-min window holds only a few samples); the same window peaked at {fmt(w1)} a day ago '
                                  f'and {fmt(w7)} a week ago. The metric carries only the blocking pid, no database or query, so the blocked statement has to come from QAN or the app logs',
                                  rules['lock'], f'WARN {svc} {fmt(w)} sessions waiting on locks ({wmin} min)'))
        count, dbs = dead[0].get(svc, [0, {}])
        if count >= 0.5:
            top = ', '.join(f'{k} {fmt(x)}' for k, x in sorted(dbs.items(), key=lambda kv: -kv[1])[:3]) or '?'
            found.append(pmm_item(svc, 'deadlocks', 'info', round(count), round(dead[1].get(svc, [0])[0]), round(dead[2].get(svc, [0])[0]), 'deadlocks',
                                  f'{fmt(count)} deadlocks on {svc} in the last 60 min (databases: {top})', 1, f'INFO {svc} {fmt(count)} deadlocks in 60 min'))
    return found, rows


def qan_windows(now: float) -> list[tuple[str, str]]:
    t = int(now)
    return [(f'toDateTime({t - 3600 - o * 86400})', f'toDateTime({t - o * 86400})') for o in (0,) + OFFS]


def qan_sql(now: float, per_query: bool, services: str, skip_users: list[str], rules: dict) -> str:
    w = qan_windows(now)
    cols = ', '.join(f'sumIf(num_queries, period_start > {a} AND period_start <= {b}) AS c{i}, sumIf(m_query_time_sum, period_start > {a} AND period_start <= {b}) AS t{i}'
                     for i, (a, b) in enumerate(w))
    skipped = f" AND username NOT IN ({', '.join(literal(u) for u in skip_users)})" if skip_users else ''
    where = (f"service_type = 'postgresql' AND match(service_name, {literal('^(?:' + (services or '.*') + ')$')}){skipped} AND ("
             + ' OR '.join(f'(period_start > {a} AND period_start <= {b})' for a, b in w) + ')')
    if not per_query:
        return f'SELECT service_name, {cols} FROM pmm.metrics WHERE {where} GROUP BY service_name'
    inside = [f'period_start > {a} AND period_start <= {b}' for a, b in w]
    per_minute = ', '.join(f'sumIf(bc, {x}) AS c{i}, sumIf(bt, {x}) AS t{i}' for i, x in enumerate(inside))
    hot = f'{inside[0]} AND bc > 0'
    least_s, least_calls = _number(rules['rows_min_s']), _number(rules['rows_min_calls'])
    return (f'SELECT service_name, database, queryid, any(fq) AS q, {per_minute}, maxIf(bt / bc, {hot}) AS pk0, '
            f'toUnixTimestamp(argMaxIf(period_start, bt / bc, {hot})) AS pkat0, argMaxIf(bc, bt / bc, {hot}) AS pkc0 '
            f'FROM (SELECT service_name, database, queryid, period_start, any(substring(fingerprint, 1, 200)) AS fq, sum(num_queries) AS bc, sum(m_query_time_sum) AS bt '
            f'FROM pmm.metrics WHERE {where} GROUP BY service_name, database, queryid, period_start) '
            f'GROUP BY service_name, database, queryid '
            f'HAVING t0 >= {least_s} OR c0 >= {least_calls} OR t1 >= {least_s} OR t2 >= {least_s} '
            f'ORDER BY greatest(t0, t1, t2) DESC LIMIT {QAN_ROWS}')


def qan_app(db: str | None) -> str:
    return (db or '?').replace('_', '-')


def qan_avg(c, t):
    return None if not c else 1000 * t / c


def qan_item(server, kind, sig, sev, now, d1, d7, unit, label, what, slack, short, app, qid=None):
    x = anomaly('qan', sig, 'postgres', app, server, sev, now, d1, d7, unit, label, what, slack, short)
    x['key'] = f'qan|{server}|{kind}' + (f'|{qid}' if qid else '')
    x['ranKey'] = qan_key(server, qid) if qid else QAN_TOTALS
    return x


def qan_key(server: object, qid: object) -> str:
    return f'qan:{server}:{qid}'


def evaluate_qan(data: tuple[list[dict], list[dict]], rules: dict, services: str = '.*') -> tuple[list[dict], list[dict]] | None:
    rows, totals = ([r for r in part if watched(services, str(r.get('service_name') or ''))] for part in data)
    if not rows and not totals:
        return None

    def num(r, k):
        return float(r.get(k) or 0)

    found = []
    for r in totals:
        server = r['service_name']
        t0, t1, t7 = num(r, 't0'), num(r, 't1'), num(r, 't2')
        base = max(t1, t7)
        if t0 > max(2 * base, base + rules['total']):
            found.append(qan_item(server, 'total', 'qan-total', 'warn', t0, t1, t7, '', 'query DB time (s)',
                                  f'queries on {server} used {fmt(t0)} s of DB time in the last 60 min (PMM QAN, excluded users left out); '
                                  f'the same hour had {fmt(t1)} s a day ago and {fmt(t7)} s a week ago',
                                  rules['total'], f'WARN {server} DB time {fmt(t0)} s/h (1d {fmt(t1)} / 7d {fmt(t7)})', server))
    top = []
    for r in rows:
        server, db, qid, q = r['service_name'], r.get('database') or '', str(r.get('queryid') or ''), ' '.join(str(r.get('q') or '').split())
        app = qan_app(db)
        c0, t0, c1, t1, c7, t7 = num(r, 'c0'), num(r, 't0'), num(r, 'c1'), num(r, 't1'), num(r, 'c2'), num(r, 't2')
        a0, a1, a7 = qan_avg(c0, t0), qan_avg(c1, t1), qan_avg(c7, t7)
        pk = 1000 * num(r, 'pk0') if num(r, 'pkc0') else None
        top.append(dict(svc=server, db=db, app=app, queryid=qid, q=q, calls=round(c0), t=round(t0, 1), avgMs=None if a0 is None else round(a0, 2),
                        peakMs=None if pk is None else round(pk, 2), peakAt=None if pk is None else iso(num(r, 'pkat0')),
                        peakCalls=None if pk is None else round(num(r, 'pkc0')), callsD1=round(c1), tD1=round(t1, 1), avgD1=None if a1 is None else round(a1, 2),
                        callsD7=round(c7), tD7=round(t7, 1), avgD7=None if a7 is None else round(a7, 2)))
        where = f'{app} ({db}) on {server}'
        short_q = q[:90]
        base_t = max(t1, t7)
        if t0 >= rules['load'] and t0 > max(2 * base_t, base_t + rules['load']):
            sev = 'crit' if t0 >= rules['load_crit'] else 'warn'
            what = (f'query used {fmt(t0)} s of DB time in the last 60 min over {fmt(c0)} calls, against {fmt(t1)} s a day ago and {fmt(t7)} s a week ago'
                    + (' (new: no calls in either baseline)' if not (c1 or c7) else '') + f'; {where}: {short_q}')
            x = qan_item(server, 'load', 'qan-load', sev, t0, t1, t7, '', 'query DB time (s)', what, rules['load'],
                         f'{sev.upper()} {app} query DB time {fmt(t0)} s/h (1d {fmt(t1)} / 7d {fmt(t7)}): {short_q[:60]}', app, qid)
            found.append(dict(x, msg=q, queryid=qid, db=db))
        bases = [a for a, c in ((a1, c1), (a7, c7)) if a is not None and c >= 100]
        base_a = max(bases) if bases else None
        if (base_a is not None and c0 >= rules['slow_calls'] and t0 >= rules['slow_min_s'] and a0 >= rules['slow_ms']
                and a0 > max(2 * base_a, base_a + rules['slow_ms'])):
            what = (f'query average went to {fmt(a0, "ms")} over {fmt(c0)} calls in the last 60 min' + ('' if pk is None else f' (worst minute {fmt(pk, "ms")} avg)') + ', '
                    f'against {DASH if a1 is None else fmt(a1, "ms")} a day ago and {DASH if a7 is None else fmt(a7, "ms")} a week ago; {where}: {short_q}')
            x = qan_item(server, 'slow', 'qan-slow', 'warn', a0, a1, a7, 'ms', 'query average', what, rules['slow_ms'],
                         f'WARN {app} query slower: {fmt(a0, "ms")} avg (1d {DASH if a1 is None else fmt(a1, "ms")} / 7d {DASH if a7 is None else fmt(a7, "ms")}): {short_q[:60]}', app, qid)
            found.append(dict(x, msg=q, queryid=qid, db=db))
        peak_s = 0 if pk is None else pk * num(r, 'pkc0') / 1000
        stall_base = max(bases + [a0]) if a0 is not None else None
        if (pk is not None and stall_base and pk >= rules['stall_ms'] and peak_s >= rules['stall_s']
                and pk > rules['stall_x'] * stall_base):
            when = num(r, 'pkat0')
            at, zone, pc = clock.hm(when), clock.label(when), num(r, 'pkc0')
            what = (f'one minute stalled: {at} {zone} averaged {fmt(pk, "ms")} over {fmt(pc)} calls and burned {fmt(peak_s)} s of DB time, against {fmt(stall_base, "ms")} usual '
                    f'and about {fmt(c0 / 60)} calls a minute averaged over the hour, so sessions were waiting rather than working (a lock or I/O, not more load); {where}: {short_q}')
            x = qan_item(server, 'stall', 'qan-stall', 'warn', pk, a1, a7, 'ms', 'worst-minute average', what, rules['stall_ms'],
                         f'WARN {app} query stalled {at}: {fmt(pk, "ms")} avg over {fmt(pc)} calls, usual {fmt(stall_base, "ms")}: {short_q[:60]}', app, qid)
            found.append(dict(x, msg=q, queryid=qid, db=db))
        base_c = max(c1, c7)
        if c0 >= rules['calls'] and c0 > max(3 * base_c, base_c + rules['calls']):
            what = f'query ran {fmt(c0)} times in the last 60 min, against {fmt(c1)} a day ago and {fmt(c7)} a week ago ({fmt(t0)} s of DB time); {where}: {short_q}'
            x = qan_item(server, 'calls', 'qan-calls', 'info', c0, c1, c7, '', 'query calls', what, rules['calls'],
                         f'INFO {app} query calls {fmt(c0)}/h (1d {fmt(c1)} / 7d {fmt(c7)}): {short_q[:60]}', app, qid)
            found.append(dict(x, msg=q, queryid=qid, db=db))
    top.sort(key=lambda x: -x['t'])
    return found, top[:int(rules['top'])]


def mongo_item(svc, sig, sev, now, d1, d7, label, what, slack, short, unit=''):
    x = anomaly('mongo', 'mongo-' + sig, 'mongodb', svc, '', sev, now, d1, d7, unit, label, what, slack, short)
    x['key'] = f'mongo|{svc}|{sig}'
    return x


def evaluate_mongo(data: dict, rules: dict, services: str = '.*', replica_set: str = '') -> tuple[list[dict], list[dict]] | None:
    n = 1 + len(OFFS)

    def per_svc(k, i):
        out = {}
        for m, v in data[(k, i)]:
            s = m.get('service_name', '')
            if watched(services, s):
                out[s] = max(out.get(s, v), v)
        return out

    def trio(k):
        return [per_svc(k, i) for i in range(n)]

    def by_type(k, i):
        out = {}
        for m, v in data[(k, i)]:
            out[(m.get('service_name', ''), m.get('type', ''))] = v
        return out

    up, state, chg, conn, ops, queue = trio('up'), trio('state'), trio('statechg'), trio('conn'), trio('ops'), trio('queue')
    expected = sorted(set().union(*up))
    if not expected:
        return None
    reporting = {m.get('service_name') for i in range(n) for m, _ in data[('state', i)]}
    labels = {m.get('service_name'): m.get('set') for i in range(n) for m, _ in data[('state', i)] if isinstance(m.get('set'), str) and m.get('set')}
    named = {labels[svc] for svc in expected if svc in labels}
    fallback = next(iter(named)) if len(named) == 1 else DEFAULT_SET
    set_of = {svc: (replica_set or labels.get(svc) or fallback) if svc in reporting else '' for svc in expected}
    avail = per_svc('avail', 0)
    host = {m.get('service_name'): (m.get('name'), m.get('node_name')) for m, _ in data[('names', 0)]}
    lag = [{m.get('name'): v for m, v in data[('lag', i)]} for i in range(n)]
    lat = [by_type('lat', i) for i in range(n)]
    latn = [by_type('latn', i) for i in range(n)]
    found, rows = [], []
    for svc in expected:
        addr, node = host.get(svc, (None, None))
        st = state[0].get(svc)
        avg = {}
        for t in ('read', 'write', 'command'):
            avg[t] = [None if not latn[i].get((svc, t)) else lat[i].get((svc, t), 0) / latn[i][(svc, t)] / 1000 for i in range(n)]
        r = dict(svc=svc, set=set_of[svc], node=node, addr=addr, up=up[0].get(svc), state=MONGO_STATES.get(int(st), str(st)) if st is not None else None,
                 conn=conn[0].get(svc), connD1=conn[1].get(svc), connD7=conn[2].get(svc), avail=avail.get(svc),
                 lag=lag[0].get(addr) if addr else None, ops=ops[0].get(svc), opsD1=ops[1].get(svc), opsD7=ops[2].get(svc),
                 readMs=avg['read'][0], writeMs=avg['write'][0], cmdMs=avg['command'][0], queue=queue[0].get(svc))
        for k in ('readMs', 'writeMs', 'cmdMs'):
            if r[k] is not None:
                r[k] = round(r[k], 2)
        rows.append(r)
        if svc not in up[0]:
            found.append(mongo_item(svc, 'down', 'crit', 0, up[1].get(svc), up[2].get(svc), 'not reported by PMM',
                                    f'PMM has no mongodb_up sample for {svc} ({node or "?"}): its mongodb_exporter or pmm-agent stopped reporting, or the member is gone', 1,
                                    f'CRIT {svc} not reported by PMM'))
            continue
        if up[0][svc] < 0.5:
            found.append(mongo_item(svc, 'down', 'crit', 0, up[1].get(svc), up[2].get(svc), 'MongoDB down',
                                    f'{svc} ({node or "?"}) is down: mongodb_up is 0', 1, f'CRIT {svc} MongoDB down (mongodb_up 0)'))
            continue
        if st is not None and int(st) not in (1, 2, 7):
            found.append(mongo_item(svc, 'state', 'crit', st, state[1].get(svc), state[2].get(svc), 'replica set state',
                                    f'{svc} ({node or "?"}) is {r["state"]} in replica set {set_of[svc]}, not PRIMARY or SECONDARY', 1,
                                    f'CRIT {svc} is {r["state"]}'))
        c, cap = conn[0].get(svc), (conn[0].get(svc) or 0) + (avail.get(svc) or 0)
        d1, d7 = conn[1].get(svc), conn[2].get(svc)
        if c is not None and avail.get(svc):
            pct = 100 * c / cap
            r['pct'] = round(pct, 2)
            if pct >= rules['sat_warn']:
                sev = 'crit' if pct >= rules['sat_crit'] else 'warn'
                found.append(mongo_item(svc, 'saturation', sev, pct, None, None, 'connection saturation',
                                        f'{svc} peaked at {fmt(c)} connections in the last 60 min, {fmt(pct, "%")} of the {fmt(cap)} it accepts', 5,
                                        f'{sev.upper()} {svc} connections {fmt(c)}/{fmt(cap)} ({fmt(pct, "%")})', '%'))
        bases = [b for b in (d1, d7) if b is not None]
        if c is not None and bases:
            base = max(bases)
            if c > max(rules['jump'] * base, base + rules['jump_slack']):
                found.append(mongo_item(svc, 'conn-jump', 'warn', c, d1, d7, 'connection jump',
                                        f'{svc} peaked at {fmt(c)} connections in the last 60 min; the same window had {fmt(d1 or 0)} a day ago and {fmt(d7 or 0)} a week ago',
                                        rules['jump_slack'], f'WARN {svc} connections jumped to {fmt(c)} (1d {fmt(d1 or 0)} / 7d {fmt(d7 or 0)})'))
        v = r['lag']
        lb = max([b for b in (lag[1].get(addr), lag[2].get(addr)) if b is not None] or [0]) if addr else 0
        if v is not None and v > rules['lag'] and v > lb + rules['lag']:
            found.append(mongo_item(svc, 'repl-lag', 'warn', v, lag[1].get(addr), lag[2].get(addr), 'replication lag (s)',
                                    f'{svc} ({addr}) has stayed at least {duration(v)} behind the primary for 10 min (lowest value every member reports); '
                                    f'the same window had {duration(lag[1].get(addr))} a day ago and {duration(lag[2].get(addr))} a week ago',
                                    rules['lag'], f'WARN {svc} replication lag {duration(v)}'))
        for t, name in (('read', 'reads'), ('write', 'writes'), ('command', 'commands')):
            a0, a1, a7 = avg[t]
            nops = latn[0].get((svc, t)) or 0
            earlier = [b for b in (a1, a7) if b is not None]
            base_a = max(earlier) if earlier else None
            if a0 is not None and base_a is not None and nops >= rules['lat_ops'] and a0 > max(rules['lat_x'] * base_a, base_a + rules['lat_ms']):
                found.append(mongo_item(svc, f'latency-{t}', 'warn', a0, a1, a7, f'{name} average',
                                        f'{name} on {svc} averaged {fmt(a0, "ms")} over {fmt(nops)} operations in the last 60 min, '
                                        f'against {DASH if a1 is None else fmt(a1, "ms")} a day ago and {DASH if a7 is None else fmt(a7, "ms")} a week ago',
                                        rules['lat_ms'], f'WARN {svc} {name} {fmt(a0, "ms")} avg (1d {DASH if a1 is None else fmt(a1, "ms")} / 7d {DASH if a7 is None else fmt(a7, "ms")})', 'ms'))
        q0 = queue[0].get(svc) or 0
        qb = max([b for b in (queue[1].get(svc), queue[2].get(svc)) if b is not None] or [0])
        if q0 > rules['queue'] and q0 > qb + rules['queue']:
            found.append(mongo_item(svc, 'queue', 'warn', q0, queue[1].get(svc), queue[2].get(svc), 'operations queued on locks',
                                    f'up to {fmt(q0)} operations waited in the global lock queue on {svc} in the last 60 min', rules['queue'],
                                    f'WARN {svc} {fmt(q0)} operations queued'))
    for name in sorted({x for x in set_of.values() if x}):
        members = [s for s in expected if set_of[s] == name]
        primaries = [s for s in members if state[0].get(s) == 1]
        if not primaries:
            found.append(mongo_item(name, 'no-primary', 'crit', 0, None, None, 'no primary',
                                    f'no member of replica set {name} reports PRIMARY: writes fail until an election finishes', 1, f'CRIT {name} has no primary'))
        changed = [s for s in members if (chg[0].get(s) or 0) > 0]
        if changed and primaries:
            p = primaries[0]
            found.append(mongo_item(name, 'election', 'warn', len(changed), None, None, 'replica set roles changed',
                                    f'replica set {name} changed member roles in the last 60 min (members: {", ".join(changed)}); the primary is now {p} ({host.get(p, (None, "?"))[1]})', 1,
                                    f'WARN {name} election, primary now {p}'))
    return found, rows


def dashboard(url: str, which: str, params: dict) -> str:
    return f"{url.rstrip('/')}/graph/d/{DASHBOARDS[which]}?" + urllib.parse.urlencode({**params, 'from': 'now-6h', 'to': 'now'})


def links_for(card: dict, url: str) -> list[dict] | None:
    src, sig, svc = card.get('src'), str(card.get('sig') or ''), str(card.get('svc') or '')
    if src not in ('pmm', 'qan', 'mongo') or not url.startswith('https://'):
        return None
    if src == 'pmm':
        if not sig.startswith('pg-'):
            return None
        out = [dict(label='PMM instance summary', url=dashboard(url, 'pg', {'var-service_name': svc}))]
        if sig == 'pg-repl-lag':
            out.append(dict(label='PMM replication overview', url=dashboard(url, 'repl', {'var-service_name': svc})))
        return out
    if src == 'qan':
        if not sig.startswith('qan-'):
            return None
        server = str(card.get('sub') or svc)
        params = {'var-service_name': server}
        if card.get('queryid'):
            params.update(filter_by=str(card['queryid']), query_selected='true')
        return [dict(label='PMM Query Analytics', url=dashboard(url, 'qan', params)), dict(label='PMM instance summary', url=dashboard(url, 'pg', {'var-service_name': server}))]
    if not sig.startswith('mongo-'):
        return None
    if sig in SET_CARDS:
        return [dict(label='PMM replica set summary', url=dashboard(url, 'set', {'var-replset': svc}))]
    return [dict(label='PMM MongoDB instance summary', url=dashboard(url, 'mongo', {'var-service_name': svc}))]
