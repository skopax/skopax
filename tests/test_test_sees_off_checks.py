import dataclasses
from pathlib import Path

from fastapi.testclient import TestClient

from skopax.app import create_app
from skopax.contract import InstanceInfo, TestResult
from skopax.kube import Kube
from skopax.messages import msg
from skopax.registry import Registry
from tests.conftest import AUTH, settings
from tests.plugins import demo_plugin, two_checks_plugin

HERE = Path(demo_plugin.__file__).parent
NOW = 1_792_224_000.0
URL = {'url': 'https://demo.example.com'}
SEEN: list = []


def probe(ctx) -> TestResult:
    SEEN.append(ctx.instance.off_checks)
    return TestResult(True, 'seen', 1)


PROBED = dataclasses.replace(two_checks_plugin.PLUGIN, test=probe)


def test_a_test_sees_the_saved_switches_the_form_ticks_or_none(store, tmp_path):
    # given
    SEEN.clear()
    with TestClient(create_app(settings(), store, registry=Registry.of(PROBED, directory=HERE), kube=Kube(root=tmp_path))) as c:
        c.post('/api/admin/plugins/demo/instances', json={'name': 'main', 'values': URL, 'enabled': False, 'offChecks': ['beat']}, headers=AUTH)

        # when
        saved = c.post('/api/admin/plugins/demo/test', json={'name': 'main', 'values': {}}, headers=AUTH)
        ticked = c.post('/api/admin/plugins/demo/test', json={'name': 'main', 'values': {}, 'offChecks': ['pulse']}, headers=AUTH)
        fresh = c.post('/api/admin/plugins/demo/test', json={'values': URL}, headers=AUTH)
        fresh_ticked = c.post('/api/admin/plugins/demo/test', json={'values': URL, 'offChecks': ['pulse', 'beat']}, headers=AUTH)
        unknown = c.post('/api/admin/plugins/demo/test', json={'name': 'main', 'values': {}, 'offChecks': ['nope']}, headers=AUTH)
        runtime = c.app.state.runtime
        in_pass = runtime.context(PROBED, runtime.instances.get('demo', 'main'), NOW).instance.off_checks

    # then
    assert [r.status_code for r in (saved, ticked, fresh, fresh_ticked)] == [200, 200, 200, 200]
    assert SEEN == [frozenset({'beat'}), frozenset({'pulse'}), frozenset(), frozenset({'pulse', 'beat'})]
    assert unknown.status_code == 422 and unknown.json() == {'detail': msg('instances.invalid.checks', check='nope'), 'field': 'offChecks'}
    assert in_pass == frozenset({'beat'})


def test_an_instance_info_has_no_off_checks_by_default():
    # when
    info = InstanceInfo('main', True, None, {})

    # then
    assert info.off_checks == frozenset()
