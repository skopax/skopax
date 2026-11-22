import json
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

from fastapi.testclient import TestClient

from skopax import messages
from skopax.app import create_app
from skopax.instances import Instances, PluginState
from skopax.kit.promql import prom_link
from skopax.messages import msg
from skopax.plugins import capacity, prometheus
from skopax.registry import BUILTIN, Registry, describe
from skopax.remote import HttpError, Unreachable
from skopax.runtime import Runtime
from skopax.sealer import Sealer
from skopax.store import Store
from tests.conftest import settings
from tests.fake_http import FakeHttp
from tests.plugin_kit import KIT_KEY, add_instance
from tests.test_engine import NOW

GET = 'GET'
PROD = 'https://prom.example.com'
EAST = 'https://prom-east.example.com'
UI = 'https://prometheus.example.com'
USER = 'alice@example.com'
SELECTOR = 'namespace!="scratch"'
SEARCH = {'namespace': 'search', 'persistentvolumeclaim': 'es-data-0'}
METRICS = {'namespace': 'monitoring', 'persistentvolumeclaim': 'prometheus-db'}
LOKI = {'namespace': 'monitoring', 'persistentvolumeclaim': 'loki-data'}
FS_DEFAULT = ('fstype!~"tmpfs|devtmpfs|overlay|squashfs|nsfs|ramfs|iso9660|autofs",'
              'mountpoint!~"/boot.*|/run.*|/snap.*|/etc/.*|/var/lib/docker.*|/var/lib/kubelet.*"')


def vec(rows: list[tuple[dict, float]]) -> dict:
    return {'status': 'success', 'data': {'resultType': 'vector', 'result': [{'metric': m, 'value': [NOW, str(v)]} for m, v in rows]}}


def hpa(name: str) -> dict:
    return {'namespace': 'shop', 'horizontalpodautoscaler': name}


def job(name: str) -> dict:
    return {'namespace': 'shop', 'job': name}


def disk(node: str, mount: str, label: str = 'node_name') -> dict:
    return {label: node, 'mountpoint': mount}


def cap_main(q, body):
    e = q['query'][0]
    if 'kubelet_volume_stats' in e:
        if e.startswith('max by (namespace, persistentvolumeclaim) (100 *'):
            return vec([(SEARCH, 91.2), (METRICS, 65.0), (LOKI, 61.0)])
        if 'capacity_bytes' in e:
            return vec([(SEARCH, 337.6), (METRICS, 316.9)])
        if 'available_bytes' in e:
            return vec([(SEARCH, 29.7), (METRICS, 110.9), (LOKI, 2.0)])
        return vec([(SEARCH, 3.9), (METRICS, 6.6), (LOKI, 1.0)])
    if 'min_over_time((max by (namespace, horizontalpodautoscaler)' in e:
        return vec([(hpa('checkout-service'), 0), (hpa('cart-service'), -3), (hpa('static-site'), 0)])
    if 'status_current_replicas' in e:
        return vec([(hpa('checkout-service'), 15), (hpa('cart-service'), 2), (hpa('static-site'), 2)])
    if 'spec_min_replicas' in e:
        return vec([(hpa('checkout-service'), 10), (hpa('cart-service'), 2), (hpa('static-site'), 2)])
    if 'spec_max_replicas' in e:
        return vec([(hpa('checkout-service'), 15), (hpa('cart-service'), 5), (hpa('static-site'), 2)])
    if 'status_target_metric' in e:
        return vec([(hpa('checkout-service'), 640), (hpa('cart-service'), 40)])
    if 'spec_target_metric' in e:
        return vec([(hpa('checkout-service'), 500), (hpa('cart-service'), 500)])
    if 'min_over_time' in e and 'jvm_gc_live' in e:
        return vec([(job('cart-service'), 96.1), (job('payments-api'), 40.0)])
    if 'jvm_gc_live' in e:
        return vec([(job('cart-service'), 97.0), (job('payments-api'), 41.0)])
    if 'jvm_gc_max_data_size_bytes' in e:
        return vec([(job('cart-service'), 564), (job('payments-api'), 770)])
    if 'jvm_gc_pause' in e:
        return vec([(job('cart-service'), 12.5)])
    return vec([])


def cap_east(q, body):
    e = q['query'][0]
    if 'kubelet_volume_stats' in e and e.startswith('max by (namespace, persistentvolumeclaim) (100 *'):
        return vec([({'namespace': 'data', 'persistentvolumeclaim': 'pg-data-0'}, 93.0)])
    return vec([])


def host_disks(label: str, host: str = 'db-1'):
    def answer(q, body):
        e = q['query'][0]
        if 'node_filesystem' not in e:
            return vec([])
        n = lambda node, mount: disk(node, mount, label)
        if e.startswith('max by (instance, node_name, mountpoint) (100 *'):
            return vec([(n(host, '/data'), 88.0), (n(host, '/'), 40.0), (n('tiny', '/x'), 99.0)])
        if 'size_bytes' in e and 'avail' not in e:
            return vec([(n(host, '/data'), 1000.0), (n(host, '/'), 50.0), (n('tiny', '/x'), 0.2)])
        if 'deriv' in e:
            return vec([(n(host, '/data'), 10.0)])
        return vec([(n(host, '/data'), 120.0), (n(host, '/'), 30.0)])

    return answer


def everything(q, body):
    return (host_disks('node_name') if 'node_filesystem' in q['query'][0] else cap_main)(q, body)


def no_volumes(q, body):
    return vec([]) if 'kubelet_volume_stats' in q['query'][0] else everything(q, body)


def nothing(q, body):
    return vec([])


def routes(**answers) -> FakeHttp:
    base = {'main': PROD, 'east': EAST}
    return FakeHttp({(GET, f'{base[name]}/api/v1/query'): answer for name, answer in answers.items()})


def runtime_with(store: Store, http, proms: dict[str, dict], caps: dict[str, dict]) -> Runtime:
    registry = Registry.of(prometheus.PLUGIN, capacity.PLUGIN)
    runtime = Runtime(registry, PluginState(store.db), Instances(store.db, registry, lambda: Sealer(KIT_KEY)), http)
    for index, (name, values) in enumerate(proms.items()):
        add_instance(store, prometheus.PLUGIN, name, sealer_key=KIT_KEY, primary=index == 0, **values)
    for index, (name, values) in enumerate(caps.items()):
        add_instance(store, capacity.PLUGIN, name, sealer_key=KIT_KEY, primary=index == 0, **values)
    runtime.refresh()
    runtime.live = SimpleNamespace(rules_of=lambda key: {})
    return runtime


def run_once(store: Store, http, caps: dict | None = None, proms: dict | None = None, name: str = 'main'):
    runtime = runtime_with(store, http, proms or {'main': {'url': PROD, 'ui-url': UI}}, caps or {'main': {'prometheus': 'main', 'volume-selector': SELECTOR}})
    return capacity.run(runtime.context(capacity.PLUGIN, runtime.instances.get('capacity', name), NOW)), runtime


def asked(http: FakeHttp) -> list[str]:
    return [parse_qs(urlsplit(c[1]).query)['query'][0] for c in http.calls]


def part(rows: list[dict], name: str) -> list[dict]:
    return [r for r in rows if r['part'] == name]


def app_with(store: Store, http, proms: dict[str, dict], caps: dict[str, dict]) -> TestClient:
    for index, (name, values) in enumerate(proms.items()):
        add_instance(store, prometheus.PLUGIN, name, primary=index == 0, **values)
    for index, (name, values) in enumerate(caps.items()):
        add_instance(store, capacity.PLUGIN, name, primary=index == 0, **values)
    return TestClient(create_app(settings(), store, transport=http))


def cards(store: Store) -> dict[str, dict]:
    return {a['key']: a for a in store.load()['anomalies']}


def test_the_capacity_check_cards_full_and_filling_volumes_busy_autoscalers_at_max_and_heap_after_gc(store):
    # given
    http = routes(main=cap_main)

    # when
    sweep, runtime = run_once(store, http)
    out = sweep.result

    # then
    assert out.ok and out.status == 'ok \u00b7 no data for host disks' and out.ran == ('capacity:volumes', 'capacity:hpa', 'capacity:heap', 'capacity:no-data', 'capacity:access:volumes', 'capacity:access:hpa', 'capacity:access:heap', 'capacity:access:dbdisk')
    found = {c['key']: c for c in out.found}
    assert found['pvc-disk|search/es-data-0|disk']['sev'] == 'crit' and found['pvc-disk|search/es-data-0|disk']['now'] == 91.2
    filling = found['pvc-disk|monitoring/loki-data|disk']
    assert filling['sev'] == 'warn' and 'cluster' not in filling and 'full in about 2 days' in filling['what'], 'a volume that fills within 3 days warns before 80 %'
    assert 'pvc-disk|monitoring/prometheus-db|disk' not in found
    scaler = found['hpa-max|shop/checkout-service|replicas']
    assert scaler['sev'] == 'warn' and '640 % CPU against its 500 % target' in scaler['what']
    assert not [k for k in found if 'static-site' in k or 'cart-service|replicas' in k], 'a fixed-size autoscaler (min = max) or one below max is never a card'
    heap = found['jvm-heap|shop/cart-service|heap']
    assert heap['sev'] == 'crit' and heap['now'] == 96.1 and heap['ranKey'] == 'capacity:heap'
    rows = sweep.sections['capacity']
    assert [v['pvc'] for v in part(rows, 'volumes')][:2] == ['es-data-0', 'prometheus-db'] and part(rows, 'volumes')[0]['daysToFull'] == 7.6
    assert [h['hpa'] for h in part(rows, 'hpa')] == ['checkout-service', 'cart-service'] and part(rows, 'hpa')[0]['atMax30']
    graph = prom_link('100 * jvm_gc_live_data_size_bytes{namespace="shop",job="cart-service"} / jvm_gc_max_data_size_bytes{namespace="shop",job="cart-service"}', UI)
    assert part(rows, 'heap')[0] == {'part': 'heap', 'source': 'main', 'ns': 'shop', 'job': 'cart-service', 'pct': 97.0, 'pct15': 96.1, 'maxMb': 564, 'gcMsPerS': 12.5, 'link': graph}
    assert part(rows, 'summary') == [{'part': 'summary', 'at': '2026-10-17T08:00:00Z', 'source': 'main', 'off': [], 'noAccess': []}] and part(rows, 'dbdisk') == []
    assert not [r for r in rows if 'cluster' in r or 'plugin' in r]
    assert capacity.links(filling, runtime.instances.get('capacity', 'main').info())[0]['url'].startswith(f'{UI}/query?')
    assert all(SELECTOR in q for q in asked(http) if 'kubelet_volume_stats' in q)


def test_a_failed_capacity_part_keeps_the_others(store):
    # given
    def broken(q, body):
        if 'jvm_' in q['query'][0]:
            raise HttpError(502, PROD)
        return cap_main(q, body)

    # when
    sweep, _ = run_once(store, routes(main=broken))
    out = sweep.result

    # then
    assert out.ok and out.ran == ('capacity:volumes', 'capacity:hpa', 'capacity:no-data', 'capacity:access:volumes', 'capacity:access:hpa', 'capacity:access:dbdisk') and out.status.startswith('error: JVM heap: ')
    assert out.status == 'error: JVM heap: Prometheus query HTTP 502 \u00b7 no data for host disks'
    assert part(sweep.sections['capacity'], 'heap') == [] and part(sweep.sections['capacity'], 'volumes')


def test_host_disks_come_from_node_filesystem_series_and_card_a_full_data_disk(store):
    # given
    pmm_style = routes(main=host_disks('node_name'))

    # when
    sweep, _ = run_once(store, pmm_style)
    out = sweep.result
    plain, _ = run_once(store, routes(main=host_disks('instance', '10.0.0.5:9100')))
    with app_with(store, routes(main=host_disks('instance', '10.0.0.5:9100')), {'main': {'url': PROD}}, {'main': {'prometheus': 'main'}}) as c:
        c.app.state.sweeper.run('capacity', 'capacity', NOW)
    stored = cards(store)

    # then
    assert all('/etc/.*' in q and 'node_name!~' not in q for q in asked(pmm_style) if 'node_filesystem' in q), 'the default selector keeps bind mounts out and names no host'
    assert out.ran == ('capacity:dbdisk', 'capacity:no-data', 'capacity:access:volumes', 'capacity:access:hpa', 'capacity:access:heap', 'capacity:access:dbdisk')
    [card] = out.found
    assert card['key'] == 'db-disk|-/db-1|/data' and card['sev'] == 'warn' and 'full in about 12 days' in card['what'] and card['ranKey'] == 'capacity:dbdisk'
    assert card['label'] == 'Host disk' and 'PMM' not in card['what'] and 'urls' not in card
    assert [r['mount'] for r in part(sweep.sections['capacity'], 'dbdisk')] == ['/data', '/'], 'disks under 1 GB are left out'
    assert [c['key'] for c in plain.result.found] == ['db-disk|-/10.0.0.5:9100|/data'], 'a plain node_exporter names its hosts by instance'
    assert stored['db-disk|-/10.0.0.5:9100|/data']['status'] == 'open', 'the Sweeper keeps a host:port service name'


def test_capacity_identity_and_manifest():
    # given
    registry = Registry.of(prometheus.PLUGIN, capacity.PLUGIN)

    # when
    loaded = registry.get('capacity')
    manifest = describe(capacity.PLUGIN, icon=True)

    # then
    assert (loaded.state, loaded.reason) == ('ok', None) and 'skopax.plugins.capacity' in BUILTIN
    assert Registry.load({}, entry_points=False).get('capacity').state == 'ok'
    assert json.loads(json.dumps(manifest, allow_nan=False)) == manifest
    assert (manifest['id'], manifest['name'], manifest['version'], manifest['category'], manifest['author'], manifest['licence'], manifest['core'], manifest['edition']) == (
        'capacity', 'Capacity', '0.1.0', 'infrastructure', 'Skopax', 'Apache-2.0', '>=0.1,<0.6', 'community')
    assert manifest['uses'] == ['prometheus', 'pmm'] and manifest['capabilities'] == {'hosts': [], 'secrets': [], 'agentReads': False}
    assert [(f['key'], f['kind'], f['required']) for f in manifest['fields']] == [('prometheus', 'instance', True), ('jvm', 'bool', False), ('host-disks', 'bool', False), ('disks-pmm', 'instance', False), ('volume-selector', 'text', False),
                                                                                                    ('fs-selector', 'text', False)]
    assert manifest['fields'][0]['choices'] == ['prometheus']
    assert manifest['checks'] == [{'id': 'capacity', 'label': 'Volumes, autoscalers, JVM heap and host disks', 'period': 900, 'offset': 600, 'ran': ['capacity']}]
    assert capacity.PLUGIN.checks[0].sections == ('capacity',) and capacity.PLUGIN.checks[0].tool is None
    assert manifest['rules'] == [{'id': 'rules', 'label': 'Capacity', 'defaults': capacity.RULES, 'help': manifest['rules'][0]['help']}]
    assert capacity.RULES == {'disk_warn': 80, 'disk_crit': 90, 'fill_days': 3, 'fill_floor': 60, 'heap_warn': 85, 'heap_crit': 95}
    assert [(p['tab'], p['label'], p['script'], p['group']) for p in manifest['panels']] == [('disk', 'Disk', 'panel.js', 'observe'), ('jvm', 'JVM', 'panel.js', 'observe'), ('cap', 'Capacity', 'panel.js', 'observe')]
    assert manifest['tools'] == [] and manifest['knowledge'] == [] and capacity.PLUGIN.probe_targets is None
    assert registry.icon('capacity').startswith('<svg')


def test_capacity_connection_test_reports_ok_and_failures(store):
    # given
    path = f'{PROD}/api/v1/query'

    def check(answer, enabled: bool = True):
        http = FakeHttp({(GET, path): answer})
        runtime = runtime_with(store, http, {'main': {'url': PROD}}, {'main': {'prometheus': 'main'}})
        if not enabled:
            add_instance(store, prometheus.PLUGIN, 'main', sealer_key=KIT_KEY, primary=True, enabled=False, url=PROD)
            runtime.refresh()
        return capacity.test(runtime.context(capacity.PLUGIN, runtime.instances.get('capacity', 'main'), NOW)), http

    # when
    ok, http = check(vec([({'job': 'prometheus'}, 1)]))
    refused, _ = check(HttpError(401, path))
    down, _ = check(Unreachable('x'))
    off, quiet = check(vec([]), enabled=False)

    # then
    assert (ok.ok, ok.summary) == (True, 'Prometheus main answers') and isinstance(ok.ms, int)
    assert [parse_qs(urlsplit(c[1]).query)['query'] for c in http.calls] == [['up']]
    assert (refused.ok, refused.summary) == (False, messages.msg('connection.no_credentials', status=401))
    assert (down.ok, down.summary) == (False, messages.msg('connection.unreachable'))
    assert (off.ok, off.summary) == (False, messages.msg('peers.unavailable', plugin='Prometheus', instance='main')) and quiet.calls == []


def test_capacity_defaults_are_neutral():
    # when
    defaults = {f.key: f.default for f in capacity.PLUGIN.fields}

    # then
    assert defaults == {'prometheus': '', 'jvm': 'true', 'host-disks': 'true', 'disks-pmm': '', 'volume-selector': '', 'fs-selector': FS_DEFAULT}
    assert 'DiskPressure' in next(f for f in capacity.PLUGIN.fields if f.key == 'host-disks').help
    assert {s.key: s.default for s in capacity.PLUGIN.settings} == {}


def test_capacity_second_instance_is_scoped(store):
    # given
    http = routes(main=cap_main, east=cap_east)
    proms = {'main': {'url': PROD, 'ui-url': UI}, 'east': {'url': EAST}}

    # when
    with app_with(store, http, proms, {'main': {'prometheus': 'main'}, 'east': {'prometheus': 'east'}}) as c:
        c.app.state.sweeper.run('capacity', 'capacity', NOW)
    state = store.load()
    east = cards(store)['pvc-disk|data/pg-data-0|disk@east']

    # then
    assert (east['cluster'], east['ranKey'], east['short']) == ('east', 'capacity:volumes-east', 'CRIT EAST pg-data-0 disk Volume disk 93.0%')
    assert 'cluster' not in cards(store)['pvc-disk|search/es-data-0|disk']
    assert state['serverPasses']['capacity']['status'] == 'ok \u00b7 no data for host disks'
    assert state['serverPasses']['capacity-east']['status'] == 'ok \u00b7 no data for autoscalers, JVM heap, host disks'
    assert [r['cluster'] for r in state['capacity'] if r['part'] == 'volumes' and r['pvc'] == 'pg-data-0'] == ['east']
    assert east['links'] == [{'label': 'Prometheus graph', 'url': prom_link(
        '100 * kubelet_volume_stats_used_bytes{namespace="data",persistentvolumeclaim="pg-data-0"} / kubelet_volume_stats_capacity_bytes{namespace="data",persistentvolumeclaim="pg-data-0"}',
        EAST, '7d')}]


def test_capacity_serves_one_panel_script_for_its_three_pages_and_its_tile_opens_disk(store):
    # given
    http = routes(main=cap_main)

    # when
    with app_with(store, http, {'main': {'url': PROD}}, {'main': {'prometheus': 'main'}}) as c:
        c.app.state.sweeper.run('capacity', 'capacity', NOW)
        info = c.get('/api/server').json()
        scripts = c.app.state.runtime.page_scripts()
        served = c.app.state.runtime.asset('capacity', 'panel.js')
    tile = next(p for p in info['plugins'] if p['id'] == 'capacity')

    # then
    assert tile['tab'] == 'disk' and [(p['tab'], p['label']) for p in tile['pages']] == [('disk', 'Disk'), ('jvm', 'JVM'), ('cap', 'Capacity')]
    assert tile['instances'] == [{'name': 'main', 'primary': True, 'pass': 'capacity', 'status': 'ok \u00b7 no data for host disks', 'state': 'ok', 'text': msg('integrations.health.ok')}]
    assert scripts.count('/plugins/capacity/panel.js') == 1
    assert served is not None and served[1].startswith('text/javascript') and b'skopax:state' in served[0]


def test_an_emptied_selector_or_profile_list_keeps_its_default(store):
    # given
    runtime = runtime_with(store, routes(main=nothing), {'main': {'url': PROD, 'profiles': ''}}, {'main': {'prometheus': 'main', 'fs-selector': '', 'volume-selector': ''}})

    # when
    cap = runtime.instances.get('capacity', 'main').info().config
    prom = runtime.instances.get('prometheus', 'main').info().config

    # then
    assert (cap['fs-selector'], cap['jvm'], cap.get('volume-selector', '')) == (FS_DEFAULT, 'true', '')
    assert prom['profiles'] == 'spring,krakend,ingress,kube,kafka'


def test_capacity_rules_come_from_the_rule_set(store):
    # given
    http = routes(main=cap_main)

    # when
    with app_with(store, http, {'main': {'url': PROD}}, {'main': {'prometheus': 'main'}}) as c:
        c.app.state.sweeper.run('capacity', 'capacity', NOW)
        default = set(cards(store))
        c.app.state.runtime.live.set('capacity.rules', '{"disk_warn": 60}', USER)
        c.app.state.sweeper.run('capacity', 'capacity', NOW + 900)
        stricter = set(cards(store))

    # then
    assert 'pvc-disk|monitoring/prometheus-db|disk' not in default and 'pvc-disk|monitoring/prometheus-db|disk' in stricter


def test_capacity_without_series_is_ok_and_quiet(store):
    # given
    answers = {'now': cap_main}
    http = FakeHttp({(GET, f'{PROD}/api/v1/query'): lambda q, body: answers['now'](q, body)})

    # when
    with app_with(store, http, {'main': {'url': PROD}}, {'main': {'prometheus': 'main'}}) as c:
        c.app.state.sweeper.run('capacity', 'capacity', NOW)
        before = {k for k, a in cards(store).items() if a['status'] == 'open'}
        answers['now'] = nothing
        c.app.state.sweeper.run('capacity', 'capacity', NOW + 900)
    state = store.load()
    after = {k for k, a in cards(store).items() if a['status'] == 'open'}

    # then
    assert 'pvc-disk|search/es-data-0|disk' in before
    assert state['serverPasses']['capacity']['status'] == 'ok \u00b7 no data'
    assert after == before, 'a source without the series opens no card and closes none'
    assert [r['part'] for r in state['capacity']] == ['summary']
    quiet, _ = run_once(store, routes(main=nothing))
    assert (quiet.result.ok, quiet.result.status, quiet.result.ran, quiet.result.found) == (True, 'ok \u00b7 no data', ('capacity:no-data', 'capacity:access:volumes', 'capacity:access:hpa', 'capacity:access:heap', 'capacity:access:dbdisk'), [])


def test_capacity_reads_through_the_chosen_prometheus_instance(store):
    # given
    http = routes(main=nothing, east=cap_east)
    proms = {'main': {'url': PROD, 'ui-url': UI}, 'east': {'url': EAST}}

    # when
    sweep, _ = run_once(store, http, {'main': {'prometheus': 'east'}}, proms)

    # then
    assert http.hits(f'{EAST}/api/v1/query') and http.hits(f'{PROD}/api/v1/query') == []
    assert [c['key'] for c in sweep.result.found] == ['pvc-disk|data/pg-data-0|disk']
    assert {r['source'] for r in sweep.sections['capacity']} == {'east'}
    assert sweep.result.found[0]['urls'][0]['url'].startswith(f'{EAST}/query?'), 'graph links open the Prometheus the check read'


def test_capacity_without_jvm_reads_no_heap(store):
    # given
    http = routes(main=cap_main)

    # when
    sweep, _ = run_once(store, http, {'main': {'prometheus': 'main', 'jvm': 'false'}})

    # then
    assert not [q for q in asked(http) if 'jvm_' in q]
    assert sweep.result.ok, 'a pass that read one part fine stays ok with another part switched off'
    assert sweep.result.ran == ('capacity:volumes', 'capacity:hpa', 'capacity:heap', 'capacity:no-data', 'capacity:access:volumes', 'capacity:access:hpa', 'capacity:access:dbdisk', 'capacity:access:heap') and part(sweep.sections['capacity'], 'heap') == []
    assert sweep.result.status == 'ok \u00b7 no data for host disks', 'a switched-off part is not a part without data'
    assert not [c for c in sweep.result.found if c['sig'] == 'jvm-heap']
    assert part(sweep.sections['capacity'], 'summary')[0]['off'] == ['heap']


def failing(q, body):
    raise HttpError(502, PROD)


def test_a_pass_whose_reads_all_fail_fails_even_with_a_part_switched_off(store):
    # given
    switches = ({'jvm': 'false'}, {'host-disks': 'false'}, {'jvm': 'false', 'host-disks': 'false'})

    # when
    results = [run_once(store, routes(main=failing), {'main': {'prometheus': 'main', **off}})[0].result for off in switches]

    # then
    assert [(r.ok, r.status.split(';')[0]) for r in results] == [(False, 'error: volumes: Prometheus query HTTP 502')] * 3
    assert all(r.found == [] for r in results)


def test_a_failed_pass_keeps_the_rows_of_the_last_good_one_when_a_part_is_switched_off(store):
    # given
    answers = {'now': cap_main}
    http = FakeHttp({(GET, f'{PROD}/api/v1/query'): lambda q, body: answers['now'](q, body)})

    # when
    with app_with(store, http, {'main': {'url': PROD}}, {'main': {'prometheus': 'main', 'host-disks': 'false'}}) as c:
        c.app.state.sweeper.run('capacity', 'capacity', NOW)
        before = [r for r in store.load()['capacity'] if r['part'] != 'summary']
        answers['now'] = failing
        c.app.state.sweeper.run('capacity', 'capacity', NOW + 900)
        state = store.load()

    # then
    assert before and [r for r in state['capacity'] if r['part'] != 'summary'] == before
    assert state['serverPasses']['capacity']['status'].startswith('error: volumes: Prometheus query HTTP 502')


def test_capacity_without_host_disks_reads_no_node_filesystem(store):
    # given
    http = routes(main=everything)

    # when
    on, _ = run_once(store, http)
    asked_on = asked(http)
    http.calls.clear()
    off, _ = run_once(store, http, {'main': {'prometheus': 'main', 'host-disks': 'false'}})

    # then
    assert any('node_filesystem' in q for q in asked_on) and 'db-disk|-/db-1|/data' in {c['key'] for c in on.result.found}
    assert not [q for q in asked(http) if 'node_filesystem' in q]
    assert (off.result.status, off.result.ran) == ('ok', ('capacity:volumes', 'capacity:hpa', 'capacity:heap', 'capacity:dbdisk', 'capacity:access:volumes', 'capacity:access:hpa', 'capacity:access:heap', 'capacity:access:dbdisk'))
    assert not [c for c in off.result.found if c['sig'] == 'db-disk'] and part(off.sections['capacity'], 'dbdisk') == []
    assert part(off.sections['capacity'], 'summary')[0]['off'] == ['dbdisk']


def test_a_switched_off_part_closes_its_cards_and_a_part_without_series_keeps_them(store):
    # given
    answers = {'now': everything}
    http = FakeHttp({(GET, f'{PROD}/api/v1/query'): lambda q, body: answers['now'](q, body)})
    watched = ('jvm-heap|shop/cart-service|heap', 'db-disk|-/db-1|/data', 'pvc-disk|search/es-data-0|disk', 'hpa-max|shop/checkout-service|replicas')

    # when
    with app_with(store, http, {'main': {'url': PROD}}, {'main': {'prometheus': 'main'}}) as c:
        c.app.state.sweeper.run('capacity', 'capacity', NOW)
        before = {k: cards(store)[k]['status'] for k in watched}
        c.app.state.instances.update('capacity', 'main', {'jvm': 'false', 'host-disks': 'false'}, USER)
        answers['now'] = no_volumes
        c.app.state.sweeper.run('capacity', 'capacity', NOW + 900)
        after = {k: cards(store)[k]['status'] for k in watched}
        status = store.load()['serverPasses']['capacity']['status']

    # then
    assert before == dict.fromkeys(watched, 'open')
    assert after == {'jvm-heap|shop/cart-service|heap': 'cleared', 'db-disk|-/db-1|/data': 'cleared', 'pvc-disk|search/es-data-0|disk': 'open', 'hpa-max|shop/checkout-service|replicas': 'open'}
    assert status == 'ok \u00b7 no data for volumes'


def test_capacity_links_are_its_own_https_card_links(store):
    # given
    info = runtime_with(store, routes(main=nothing), {'main': {'url': PROD}}, {'main': {'prometheus': 'main'}}).instances.get('capacity', 'main').info()
    good = {'src': 'capacity', 'urls': [{'label': 'Prometheus graph', 'url': f'{UI}/query?g0.expr=up'}, {'label': 'plain', 'url': 'http://prom.local/query'}, 'junk']}

    # when
    mine = capacity.links(good, info)
    other = capacity.links({'src': 'kafka', 'urls': good['urls']}, info)

    # then
    assert mine == [{'label': 'Prometheus graph', 'url': f'{UI}/query?g0.expr=up'}]
    assert other is None


def test_a_refused_capacity_part_is_a_warn_card_and_its_own_cards_stay_open(store):
    # given
    path = f'{PROD}/api/v1/query'
    http = FakeHttp({(GET, path): cap_main})
    card = 'no-access|-/capacity|volumes'
    disk = 'pvc-disk|search/es-data-0|disk'

    def refusing(q, body):
        return HttpError(403, PROD) if 'kubelet_volume_stats' in q['query'][0] else cap_main(q, body)

    # when
    with app_with(store, http, {'main': {'url': PROD, 'ui-url': UI}}, {'main': {'prometheus': 'main'}}) as c:
        c.app.state.sweeper.run('capacity', 'capacity', NOW)
        before = {k for k, a in cards(store).items() if a['status'] == 'open'}
        http.routes[(GET, path)] = refusing
        c.app.state.sweeper.run('capacity', 'capacity', NOW + 900)
        refused = {k for k, a in cards(store).items() if a['status'] == 'open'}
        state = store.load()
        http.routes[(GET, path)] = cap_main
        c.app.state.sweeper.run('capacity', 'capacity', NOW + 1800)
        back = {k: a['status'] for k, a in cards(store).items()}
    runtime = runtime_with(store, FakeHttp({(GET, path): HttpError(403, path)}), {'main': {'url': PROD}}, {'main': {'prometheus': 'main'}})
    lacking = capacity.test(runtime.context(capacity.PLUGIN, runtime.instances.get('capacity', 'main'), NOW))

    # then
    assert disk in before and card not in before
    assert state['serverPasses']['capacity']['status'] == 'ok \u00b7 no data for host disks \u00b7 no access to volumes'
    warn = cards(store)[card]
    assert (warn['sev'], warn['ranKey'], warn['label']) == ('warn', 'capacity:access:volumes', 'Capacity cannot read volumes: the server requires credentials and none are set')
    assert refused == before | {card}
    assert back[card] == 'cleared' and back[disk] == 'open'
    assert (lacking.ok, lacking.summary) == (False, 'Capacity cannot read metrics: the server requires credentials and none are set')


def test_capacity_names_its_parts_as_the_page_does_and_keeps_the_part_ids_in_its_keys(store):
    # given
    def refusing(q, body):
        if 'horizontalpodautoscaler' in q['query'][0] or 'jvm_' in q['query'][0]:
            raise HttpError(403, PROD)
        return cap_main(q, body)

    # when
    sweep, _ = run_once(store, routes(main=refusing))
    out = sweep.result

    # then
    assert out.status == 'ok \u00b7 no data for host disks \u00b7 no access to autoscalers, JVM heap'
    labels = {c['ranKey']: c['label'] for c in out.found if c['sig'] == 'no-access'}
    assert labels == {'capacity:access:hpa': 'Capacity cannot read autoscalers: the server requires credentials and none are set',
                      'capacity:access:heap': 'Capacity cannot read JVM heap: the server requires credentials and none are set'}
    assert {c['key'] for c in out.found if c['sig'] == 'no-access'} == {'no-access|-/capacity|hpa', 'no-access|-/capacity|heap'}
    assert part(sweep.sections['capacity'], 'summary')[0]['noAccess'] == ['hpa', 'heap']


def test_a_failed_capacity_part_names_the_part_and_keeps_the_no_data_tail(store):
    # given
    def broken(q, body):
        if 'jvm_' in q['query'][0]:
            raise HttpError(502, PROD)
        return cap_main(q, body)

    # when
    sweep, _ = run_once(store, routes(main=broken))

    # then
    assert sweep.result.status == 'error: JVM heap: Prometheus query HTTP 502 \u00b7 no data for host disks'


def test_a_slow_prometheus_keeps_the_capacity_parts_that_answered_in_time(store, monkeypatch):
    # given
    from skopax.kit import promql
    http = routes(main=cap_main)
    ticks = iter([0.0] * 6 + [1000.0] * 200)

    # when
    monkeypatch.setattr(promql, 'clock', lambda: next(ticks))
    sweep, _ = run_once(store, http)

    # then
    out = sweep.result
    assert out.ok and out.status.startswith('error: ') and 'timed out' in out.status and 'capacity:volumes' in out.ran
    assert all(name in out.status for name in ('autoscalers: timed out', 'JVM heap: timed out', 'host disks: timed out'))
    assert part(sweep.sections['capacity'], 'volumes')
