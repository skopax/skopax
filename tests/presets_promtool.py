import importlib.util
import sys
from pathlib import Path

import yaml

from skopax.checkpresets import presets

ROOT = Path(__file__).resolve().parent.parent
EXAMPLES = ROOT / 'examples'
RULES_FILE = 'presets-rules.yml'
TEST_FILE = 'presets-test.yml'
DAY_S = 86400
NOW_S = 8 * DAY_S
BASE = float(2 ** 40)
COUCHBASE_VALUES = {'instance': 'main', 'cluster': 'orders-prod', 'selector': 'job=~".*couchbase.*",env="prod"'}
REDIS_VALUES = {'instance': 'ops', 'datasource': 'cloud-metrics', 'name': 'cloud'}
COUCHBASE_LABELS = {'job': 'couchbase-exporter', 'env': 'prod'}
COUCHBASE_SERIES = {'up': ('up', 'gauge'), 'uptime': ('kv_uptime_seconds', 'gauge'), 'conn': ('kv_curr_connections', 'gauge'), 'imem': ('index_memory_used_total', 'gauge'),
                    'iquota': ('index_memory_quota', 'gauge'), 'failover': ('cm_auto_failover_count', 'counter'), 'stuck': ('cm_rebalance_stuck', 'gauge'),
                    'rebalance': ('cm_rebalance_in_progress', 'gauge'), 'req': ('n1ql_requests', 'counter'), 'errors': ('n1ql_errors', 'counter'),
                    'timeouts': ('n1ql_timeouts', 'counter'), 'rtime': ('n1ql_request_time', 'counter'), 'queue': ('n1ql_queued_requests', 'gauge'),
                    'auth': ('cm_rest_request_auth_failure_total', 'counter')}
REDIS_SERIES = {'up': ('redis_up', 'gauge'), 'clients': ('redis_connected_clients', 'gauge'), 'maxClients': ('redis_config_maxclients', 'gauge'),
                'memory': ('redis_memory_used_bytes', 'gauge'), 'maxMemory': ('redis_memory_max_bytes', 'gauge'), 'evicted': ('redis_evicted_keys_total', 'counter'),
                'rejected': ('redis_rejected_connections_total', 'counter'), 'uptime': ('redis_uptime_in_seconds', 'gauge')}
REDIS_AVERAGES = ('avgUs', 'avgD1', 'avgD7')
PROCESSED = 1e6
CB_CHECK = {('down', 'crit'): 'down', ('down', 'warn'): 'not-scraped', ('rebalance', 'crit'): 'rebalance-stuck', ('rebalance', 'info'): 'rebalance',
            ('n1ql-errors', 'warn'): 'n1ql-errors', ('n1ql-errors', 'crit'): 'n1ql-errors-crit'}
RD_CHECK = {('memory', 'warn'): 'memory', ('memory', 'crit'): 'memory-crit'}


def example_tests(name: str, path: str):
    for folder in ('couchbase-plugin', 'managed-redis-plugin'):
        if str(EXAMPLES / folder) not in sys.path:
            sys.path.append(str(EXAMPLES / folder))
    spec = importlib.util.spec_from_file_location(name, EXAMPLES / path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


CB = example_tests('couchbase_example_tests', 'couchbase-plugin/tests/test_couchbase.py')
RD = example_tests('managed_redis_example_tests', 'managed-redis-plugin/tests/test_managed_redis.py')


def couchbase_cases() -> list:
    h, put, node_put, GIB = CB.healthy, CB.put, CB.node_put, CB.GIB
    N1, N2, N3 = CB.NODES
    cases = [('healthy', h())]
    every = node_put(node_put(h(), 'up', 0, N1, 0), 'uptime', 0, N2, 60)
    node_put(node_put(every, 'conn', 0, N3, 1000), 'conn', 1, N3, 100)
    node_put(every, 'imem', 0, N2, 3.9 * GIB)
    node_put(every, 'up', 1, 'node-9.example.com:8091', 1)
    for k, v in dict(failover=1, stuck=1, errors=100, timeouts=50, rtime=2000 * 900e6, queue=50, auth=40).items():
        put(every, k, 0, v)
    cases.append(('every kind', every))
    down = node_put(h(), 'up', 0, N3, 0)
    node_put(down, 'uptime', 0, N3, 10)
    node_put(down, 'up', 1, 'node-4.example.com:8091', 1)
    cases.append(('down and gone', down))
    none = h()
    none[('up', 0)] = []
    node_put(none, 'up', 1, N1, 1)
    cases.append(('no up series', none))
    cases += [(f'uptime {u}', node_put(h(), 'uptime', 0, N1, u)) for u in (3599, 3600)]
    cases.append(('stuck', put(put(put(h(), 'failover', 0, 1), 'stuck', 0, 1), 'rebalance', 0, 1)))
    cases.append(('running', put(h(), 'rebalance', 0, 1)))
    for errors, base in ((19, None), (20, None), (22, None), (100, None), (40, 20), (42, 20)):
        data = put(h(), 'errors', 0, errors)
        if base is not None:
            put(put(data, 'req', 1, 2000), 'errors', 1, base)
        cases.append((f'errors {errors}/{base}', data))
    for timeouts, base in ((4, None), (5, None), (6, 3), (7, 3), (7, None)):
        data = put(h(), 'timeouts', 0, timeouts)
        if base is not None:
            put(data, 'timeouts', 2, base)
        cases.append((f'timeouts {timeouts}/{base}', data))
    for req, avg, base in ((300, 200, None), (300, 201, None), (299, 500, None), (300, 200, 100), (300, 201, 100), (300, 260, 140), (300, 281, 140)):
        data = put(put(h(), 'req', 0, req), 'rtime', 0, req * avg * 1e6)
        if base is not None:
            put(put(data, 'req', 1, 1000), 'rtime', 1, 1000 * base * 1e6)
        cases.append((f'latency {req}/{avg}/{base}', data))
    for k, v in (('queue', 20), ('queue', 21), ('auth', 19), ('auth', 20)):
        cases.append((f'{k} {v}', put(h(), k, 0, v)))
    cases += [(f'index {u}', node_put(h(), 'imem', 0, N2, u * GIB)) for u in (3.5996, 3.6)]
    cases.append(('no quota', node_put(node_put(h(), 'imem', 0, N2, 3.9 * GIB), 'iquota', 0, N2, 0)))
    for conn, d1, d7 in ((1000, None, None), (300, 100, None), (301, 100, None), (450, 300, None), (501, 300, None), (301, None, 100), (301, 100, 300)):
        data = node_put(h(), 'conn', 0, N3, conn)
        cases.append((f'conn {conn}/{d1}/{d7}', node_put(node_put(data, 'conn', 1, N3, d1), 'conn', 2, N3, d7)))
    return cases


def redis_cases() -> list:
    h, put, GIB, MAIN, CACHE = RD.healthy, RD.put, RD.GIB, RD.MAIN, RD.CACHE
    every = put(h(), 'up', CACHE, 0)
    for k, v in dict(memory=7.8 * GIB, clients=9000, avgUs=900, evicted=5, rejected=2, uptime=60).items():
        put(every, k, MAIN, v)
    cases = [('healthy', h()), ('every kind', every)]
    cases += [(f'memory {u}/{m}', put(put(h(), 'memory', MAIN, u * GIB), 'maxMemory', MAIN, m * GIB)) for u, m in ((6.7, 8), (6.8, 8), (7.6, 8), (7.9, 0))]
    for avg, d1, d7 in ((900, None, None), (150, 40, 50), (269, 90, None), (300, 100, 90), (300, None, 100), (300, 90, 110)):
        cases.append((f'latency {avg}/{d1}/{d7}', put(put(put(h(), 'avgUs', MAIN, avg), 'avgD1', MAIN, d1), 'avgD7', MAIN, d7)))
    cases += [('restart', put(h(), 'uptime', MAIN, 5000)), ('down skips the rest', put(put(h(), 'up', MAIN, 0), 'uptime', MAIN, 10)), ('empty', {})]
    return cases


def couchbase_expected(data: dict) -> list[tuple[str, str, str]]:
    out = CB.run(CB.Ctx(CB.FakeProm(data))).result
    found = [(c['svc'], c['sig'].removeprefix('couchbase-'), c['sev']) for c in out.found]
    if not out.ok and 'holds no Couchbase up series' in out.status:
        found.append((CB.CLUSTER, 'no-series', 'crit'))
    return sorted(found)


def redis_expected(data: dict) -> list[tuple[str, str, str]]:
    out = RD.run(RD.Ctx(RD.FakeGrafana(data=data))).result
    found = [(c['svc'], c['sig'].removeprefix('managed-redis-'), c['sev']) for c in out.found]
    if not out.ok and 'holds no redis_up series' in out.status:
        found.append(('cloud', 'no-series', 'crit'))
    return sorted(found)


def labels_text(labels: dict) -> str:
    return '{' + ','.join(f'{k}="{v}"' for k, v in labels.items()) + '}'


class Timeline:
    def __init__(self, step_s: int):
        self.step_s = step_s
        self.now = NOW_S // step_s
        self.series: dict[str, dict[int, float]] = {}

    def point(self, i: int) -> int:
        return self.now - (0, DAY_S, 7 * DAY_S)[i] // self.step_s

    def put(self, name: str, labels: dict, kind: str, i: int, value: float) -> None:
        got = self.series.setdefault(f'{name}{labels_text(labels)}', {})
        p = self.point(i)
        got[p - 1], got[p] = (float(value), float(value)) if kind == 'gauge' else (BASE, BASE + float(value) / 2)

    def input_series(self) -> list[dict]:
        return [{'series': name, 'values': ' '.join(repr(points[n]) if n in points else '_' for n in range(self.now + 1))} for name, points in sorted(self.series.items())]


def couchbase_series(data: dict) -> list[dict]:
    t = Timeline(1800)
    for (key, i), found in data.items():
        name, kind = COUCHBASE_SERIES[key]
        for labels, value in found:
            t.put(name, COUCHBASE_LABELS | ({'instance': labels['instance']} if 'instance' in labels else {}), kind, i, value)
    return t.input_series()


def redis_series(data: dict) -> list[dict]:
    t = Timeline(900)
    for key, found in data.items():
        for labels, value in found:
            if key in REDIS_SERIES:
                name, kind = REDIS_SERIES[key]
                t.put(name, labels, kind, 0, value)
            elif key in REDIS_AVERAGES and value is not None:
                i = REDIS_AVERAGES.index(key)
                t.put('redis_commands_duration_seconds_total', labels, 'counter', i, value)
                t.put('redis_commands_processed_total', labels, 'counter', i, PROCESSED)
    return t.input_series()


def expression(d: dict) -> str:
    q = f"({d['query']})"
    return f"count by ({', '.join(d['by'])}) ({q} {d['op']} {d['threshold']!r} unless abs({q}) == Inf)"


def expr_tests(checks: list[dict], prefix: str, expected: list[tuple[str, str, str]], check_of: dict) -> list[dict]:
    want: dict[str, list[dict]] = {}
    for subject, kind, sev in expected:
        want.setdefault(check_of.get((kind, sev), kind), []).append(subject)
    out = []
    for d in checks:
        key = d['id'].removeprefix(prefix)
        samples = [{'labels': labels_text({d['by'][0]: s} if d['by'] else {}), 'value': 1} for s in sorted(want.get(key, []))]
        out.append({'expr': expression(d), 'eval_time': f'{NOW_S // DAY_S}d', 'exp_samples': samples})
    return out


def document() -> dict:
    _, couchbase = presets()['couchbase'].fill(COUCHBASE_VALUES)
    _, redis = presets()['managed-redis'].fill(REDIS_VALUES)
    tests = []
    for name, data in couchbase_cases():
        tests.append({'name': f'couchbase {name}', 'interval': '30m', 'input_series': couchbase_series(data),
                      'promql_expr_test': expr_tests(couchbase, 'cb-orders-prod-', couchbase_expected(data), CB_CHECK)})
    for name, data in redis_cases():
        tests.append({'name': f'managed-redis {name}', 'interval': '15m', 'input_series': redis_series(data),
                      'promql_expr_test': expr_tests(redis, 'redis-cloud-', redis_expected(data), RD_CHECK)})
    return {'rule_files': [RULES_FILE], 'evaluation_interval': '1m', 'tests': tests}


def write(folder: Path) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    (folder / RULES_FILE).write_text('groups: []\n', encoding='ascii')
    path = folder / TEST_FILE
    path.write_text(yaml.safe_dump(document(), sort_keys=False, width=100000), encoding='ascii')
    return path


if __name__ == '__main__':
    print(write(Path(sys.argv[1])))
