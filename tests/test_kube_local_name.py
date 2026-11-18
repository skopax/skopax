import pytest

from skopax.config import ConfigurationError, settings_env_names
from skopax.instances import Instances
from skopax.plugins import kubernetes
from skopax.registry import Registry
from skopax.sealer import Sealer
from skopax.settingsources import boot

NAME_ENV = 'SKOPAX_KUBE_LOCAL_NAME'


def test_skopax_kube_local_name_names_the_in_cluster_instance_and_a_reserved_name_stops_the_start(store, monkeypatch, tmp_path):
    # given
    inside = tmp_path / 'sa'
    inside.mkdir()
    (inside / 'token').write_text('t')
    monkeypatch.setattr(kubernetes, 'SA_ROOT', tmp_path / 'none')
    outside = kubernetes.env_instances({NAME_ENV: 'prod'})
    monkeypatch.setattr(kubernetes, 'SA_ROOT', inside)
    registry = Registry.of(kubernetes.PLUGIN)

    # when
    named = kubernetes.env_instances({NAME_ENV: ' prod '})
    unset = kubernetes.env_instances({})
    blank = kubernetes.env_instances({NAME_ENV: '  '})
    booted = boot({NAME_ENV: 'prod'}, registry)
    stored = Instances(store.db, registry, lambda: Sealer('kube-key'), sources=booted.source_set().current)
    stored.refresh()
    with pytest.raises(ConfigurationError) as reserved:
        boot({NAME_ENV: 'nodes'}, Registry.of(kubernetes.PLUGIN))
    with pytest.raises(ConfigurationError) as malformed:
        boot({NAME_ENV: 'Prod_1'}, Registry.of(kubernetes.PLUGIN))

    # then
    assert outside == []
    assert named == [{'name': 'prod', 'primary': 'true', 'mode': 'in-cluster'}]
    assert unset == blank == [{'name': 'local', 'primary': 'true', 'mode': 'in-cluster'}]
    assert [(i.name, i.primary, i.source, i.problem) for i in stored.all('kubernetes')] == [('prod', True, 'environment', None)]
    assert 'nodes' in str(reserved.value) and 'reserved' in str(reserved.value)
    assert 'Prod_1' in str(malformed.value)
    assert NAME_ENV in settings_env_names()
