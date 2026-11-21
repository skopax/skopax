from skopax.plugintools import PluginTools
from skopax.store import Store
from tests.fake_http import FakeHttp
from tests.plugin_kit import prom_runtime
from tests.test_investigator_tools import box
from tests.test_prometheus import EAST, GET, PROD, vector


def served(store: Store, names: dict) -> tuple:
    http = FakeHttp({(GET, f'{PROD}/api/v1/query'): vector([({'job': 'prod'}, 1)]), (GET, f'{EAST}/api/v1/query'): vector([({'job': 'ngn'}, 2)])})
    return box(store, plugin_tools=PluginTools(prom_runtime(store, names, http))), http


def test_a_name_that_only_starts_with_a_valid_one_is_refused_not_mapped(store: Store):
    # given
    b, http = served(store, {'prod': {'url': PROD}, 'ngn': {'url': EAST}})

    # when
    text, failed = b.call('promql', {'expr': 'up', 'cluster': 'ngn-data'})

    # then
    assert failed, text
    assert 'prod' in text and 'ngn' in text and '"ngn-data" is not configured' in text, text
    assert 'do not use another one in its place without saying so' in text
    assert not http.calls


def test_a_valid_cluster_is_used_as_given(store: Store):
    # given
    b, http = served(store, {'prod': {'url': PROD}, 'ngn': {'url': EAST}})

    # when
    text, failed = b.call('promql', {'expr': 'up', 'cluster': 'ngn'})

    # then
    assert not failed, text
    assert len(http.hits(f'{EAST}/api/v1/query')) == 1
