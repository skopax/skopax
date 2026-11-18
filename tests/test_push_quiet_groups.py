from tests.test_engine import NOW, ago, item, stored
from skopax.engine import merge


def test_service_less_alerts_do_not_silence_each_other(store):
    # given
    st = {'initialized': True, 'muted': [], 'history': [], 'anomalies': [
        {**stored('alert|KubeQuotaAlmostFull|cluster=prod', src='alert', pushedAt=ago(600)), 'svc': '-', 'ns': '-'},
    ]}
    found = [item('alert|KubeQuotaAlmostFull|cluster=prod', src='alert', svc='-', ns='-'),
             item('alert|TargetDown|cluster=prod', src='alert', svc='-', ns='-')]

    # when
    events, push, _ = merge(st, found, {'alert': True}, NOW)

    # then
    assert [a['key'] for a in push] == ['alert|TargetDown|cluster=prod']
    assert {e['key'] for e in events if e['ev'] == 'new'} == {'alert|TargetDown|cluster=prod'}
