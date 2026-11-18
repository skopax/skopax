import pytest

from skopax.kit import promql
from skopax.plugins.prometheus import engine

UI = 'https://prom.example.com'
RESTARTS = 'sum by (pod) (kube_pod_container_status_restarts_total{namespace="shop",pod=~"checkout-service-.*"})'


@pytest.mark.parametrize('args, url', [
    (('up', UI), f'{UI}/query?g0.expr=up&g0.tab=graph&g0.range_input=2d'),
    ((RESTARTS, UI, '6h'), f'{UI}/query?g0.expr=sum+by+%28pod%29+%28kube_pod_container_status_restarts_total%7Bnamespace%3D%22shop%22%2Cpod%3D~%22checkout-service-.%2A%22%7D%29'
                           '&g0.tab=graph&g0.range_input=6h'),
    (('rate(x[5m])', UI, '1h', 1792224000.0), f'{UI}/query?g0.expr=rate%28x%5B5m%5D%29&g0.tab=graph&g0.range_input=1h&g0.end_input=2026-10-17+08%3A00%3A00'),
], ids=['default-range', 'braces-and-quotes', 'with-end'])
def test_the_kit_builds_the_same_prometheus_graph_link_as_before(args, url):
    # when
    built = promql.prom_link(*args)

    # then
    assert built == url


def test_the_prometheus_engine_uses_the_kit_link():
    # then
    assert engine.prom_link is promql.prom_link
