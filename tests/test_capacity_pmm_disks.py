from types import SimpleNamespace

from skopax.contract import HttpError
from skopax.instances import Instances, PluginState
from skopax.plugins import capacity, pmm, prometheus
from skopax.registry import Registry
from skopax.runtime import Runtime
from skopax.sealer import Sealer
from skopax.store import Store
from tests.fake_http import FakeHttp
from tests.plugin_kit import KIT_KEY, add_instance
from tests.test_capacity_plugin import GET, NOW, PROD, UI, cap_main, host_disks, vec
from tests.test_pmm_plugin import DATASOURCES, METRICS_UID, TOKEN
from tests.test_pmm_plugin import URL as PMM

PROXY = f'{PMM}/graph/api/datasources/proxy/uid/{METRICS_UID}/api/v1/query'
LISTING = f'{PMM}/graph/api/datasources'


def without_disks(q, body):
    return vec([]) if 'node_filesystem' in q['query'][0] else cap_main(q, body)


def routes(prom=without_disks, pmm_answer=None, listing=None) -> FakeHttp:
    return FakeHttp({(GET, f'{PROD}/api/v1/query'): prom, (GET, PROXY): pmm_answer or host_disks('node_name'), (GET, LISTING): DATASOURCES if listing is None else listing})


def run_with(store: Store, http, pmm_values: dict | None = None, **capacity_values):
    registry = Registry.of(prometheus.PLUGIN, pmm.PLUGIN, capacity.PLUGIN)
    runtime = Runtime(registry, PluginState(store.db), Instances(store.db, registry, lambda: Sealer(KIT_KEY)), http)
    add_instance(store, prometheus.PLUGIN, 'main', sealer_key=KIT_KEY, primary=True, url=PROD, **{'ui-url': UI})
    add_instance(store, pmm.PLUGIN, 'edge', sealer_key=KIT_KEY, primary=True, **({'url': PMM, 'token': TOKEN} | (pmm_values or {})))
    add_instance(store, capacity.PLUGIN, 'main', sealer_key=KIT_KEY, primary=True, prometheus='main', **capacity_values)
    runtime.refresh()
    runtime.live = SimpleNamespace(rules_of=lambda key: {})
    ctx = runtime.context(capacity.PLUGIN, runtime.instances.get('capacity', 'main'), NOW)
    return capacity.run(ctx), capacity.test(ctx), runtime


def disk_queries(http: FakeHttp, url: str) -> list[str]:
    return [c[1] for c in http.hits(url) if 'node_filesystem' in c[1].replace('%28', '(')]


def test_host_disks_come_from_the_pmm_metrics_data_source_when_one_is_set(store: Store):
    # given
    http = routes()

    # when
    sweep, tested, _ = run_with(store, http, **{'disks-pmm': 'edge'})

    # then
    keys = {c['key'] for c in sweep.result.found}
    assert 'db-disk|-/db-1|/data' in keys
    assert http.hits(PROXY) and all('node_filesystem' not in str(c) for c in http.hits(f'{PROD}/api/v1/query'))
    assert [c[2].get('Authorization') for c in http.hits(PROXY)][0] == f'Bearer {TOKEN}'
    assert tested.ok and tested.summary == 'Prometheus main answers, host disks from PMM edge'
    assert sweep.result.status == 'ok'


def test_without_the_option_the_host_disks_still_come_from_prometheus_and_pmm_is_never_asked(store: Store):
    # given
    http = routes(prom=lambda q, body: host_disks('node_name')(q, body) if 'node_filesystem' in q['query'][0] else cap_main(q, body))

    # when
    sweep, tested, _ = run_with(store, http)

    # then
    assert 'db-disk|-/db-1|/data' in {c['key'] for c in sweep.result.found}
    assert http.hits(PROXY) == [] and http.hits(LISTING) == []
    assert tested.summary == 'Prometheus main answers'


def test_a_pinned_pmm_data_source_is_used_without_listing_them(store: Store):
    # given
    http = routes()

    # when
    sweep, _, _ = run_with(store, http, {'metrics-datasource': METRICS_UID}, **{'disks-pmm': 'edge'})

    # then
    assert http.hits(LISTING) == [] and http.hits(PROXY) and 'db-disk|-/db-1|/data' in {c['key'] for c in sweep.result.found}


def test_the_host_header_of_the_pmm_instance_goes_with_the_disk_reads(store: Store):
    # given
    http = routes()

    # when
    run_with(store, http, {'host-header': 'pmm.internal:8443'}, **{'disks-pmm': 'edge'})

    # then
    assert http.hits(PROXY) and all(c[2].get('Host') == 'pmm.internal:8443' for c in http.hits(PROXY) + http.hits(LISTING))


def test_switching_host_disks_off_ignores_the_pmm_instance(store: Store):
    # given
    http = routes()

    # when
    sweep, _, _ = run_with(store, http, **{'disks-pmm': 'edge', 'host-disks': 'false'})

    # then
    assert http.hits(PROXY) == [] and not [c for c in sweep.result.found if c['sig'] == 'db-disk']


def test_a_failing_pmm_read_fails_only_the_host_disks_part(store: Store):
    # given
    http = routes(pmm_answer=lambda q, body: HttpError(502, PROXY))

    # when
    sweep, _, _ = run_with(store, http, **{'disks-pmm': 'edge'})

    # then
    assert sweep.result.ok and sweep.result.status.startswith('error: host disks: PMM metrics query HTTP 502')
    assert 'pvc-disk' in {c['sig'] for c in sweep.result.found}
    assert 'capacity:dbdisk' not in sweep.result.ran


def test_a_refused_pmm_read_is_an_access_card_that_names_the_token(store: Store):
    # given
    http = routes(pmm_answer=lambda q, body: HttpError(403, PROXY))

    # when
    sweep, tested, _ = run_with(store, http, **{'disks-pmm': 'edge'})

    # then
    card = next(c for c in sweep.result.found if c['sig'] == 'no-access')
    assert card['ranKey'] == 'capacity:access:dbdisk' and 'token' in card['label']
    assert sweep.result.status.endswith('no access to host disks') or 'host disks' in sweep.result.status
    assert tested.ok is False


def test_a_pmm_instance_that_is_not_there_is_named_by_the_host_disks_part(store: Store):
    # given
    http = routes()

    # when
    sweep, _, _ = run_with(store, http, **{'disks-pmm': 'ghost'})

    # then
    assert sweep.result.status.startswith('error: host disks:') and 'ghost' in sweep.result.status
    assert http.hits(PROXY) == []


def test_pmm_lists_no_metrics_data_source_is_a_failed_part_not_a_silent_one(store: Store):
    # given
    http = routes(listing=[{'uid': 'logs', 'type': 'loki'}])

    # when
    sweep, _, _ = run_with(store, http, **{'disks-pmm': 'edge'})

    # then
    assert 'error: host disks: PMM lists no metrics data source' in sweep.result.status
