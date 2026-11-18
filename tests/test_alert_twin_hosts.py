import dataclasses

from fastapi.testclient import TestClient

from tests.conftest import settings
from tests.test_engine import NOW
from skopax.app import create_app
from skopax.engine import PassResult, anomaly, group_alert_twins
from skopax.store import Store

TWINS = ('HighErrorRate',)
TWIN = 'alert|HighErrorRate|cluster=prod,namespace=members'


def test_a_grafana_alert_card_never_hosts_a_prod_alert_twin():
    # given
    state = {'anomalies': [
        {'key': 'alert|KubeDeploymentReplicasMismatch|cluster=dr-generic-prometheus,namespace=members', 'src': 'grafana', 'status': 'open', 'ns': 'members', 'sev': 'warn',
         'firstSeen': '2026-11-10T00:00:00Z'},
        {'key': TWIN, 'src': 'alert', 'status': 'open', 'ns': 'members', 'sev': 'crit', 'firstSeen': '2026-11-10T01:00:00Z'}]}

    # when
    twins = group_alert_twins(state, TWINS)

    # then
    assert twins == [] and 'parent' not in state['anomalies'][1]


def test_a_native_card_of_the_namespace_still_hosts_the_twin_next_to_a_grafana_card():
    # given
    state = {'anomalies': [
        {'key': 'alert|KubeDeploymentReplicasMismatch|cluster=dr-generic-prometheus,namespace=members', 'src': 'grafana', 'status': 'open', 'ns': 'members', 'sev': 'crit',
         'firstSeen': '2026-11-10T00:00:00Z'},
        {'key': 'kafka-consume-err|members/email-service|in', 'src': 'kafka', 'status': 'open', 'ns': 'members', 'sev': 'warn', 'firstSeen': '2026-11-10T00:30:00Z'},
        {'key': TWIN, 'src': 'alert', 'status': 'open', 'ns': 'members', 'sev': 'crit', 'firstSeen': '2026-11-10T01:00:00Z'}]}

    # when
    twins = group_alert_twins(state, TWINS)

    # then
    assert twins == [(TWIN, 'kafka-consume-err|members/email-service|in')]


def test_the_app_groups_the_alert_twins_named_in_settings(store: Store):
    # given
    host = anomaly('prom', 'err', 'shop', 'checkout-service', '', 'warn', 50, 0, 0, '', 'ERROR logs', 'errors')
    twin = anomaly('alert', 'alert', 'shop', '-', 'HighErrorRate', 'warn', 1, None, None, '', 'HighErrorRate', 'firing') | {'key': 'alert|HighErrorRate|namespace=shop'}
    other = anomaly('alert', 'alert', 'shop', '-', 'SlowRequests', 'warn', 1, None, None, '', 'SlowRequests', 'firing') | {'key': 'alert|SlowRequests|namespace=shop'}
    store.save_state({'anomalies': [], 'initialized': True}, None)

    # when
    with TestClient(create_app(dataclasses.replace(settings(), alert_twins=TWINS), store)) as c:
        c.app.state.server_apply([PassResult('prom', True, 'ok', [host, twin, other], {}, ('prom', 'alert'))], NOW, None)

    # then
    assert store.anomaly(twin['key'])['parent'] == host['key']
    assert 'parent' not in store.anomaly(other['key'])
