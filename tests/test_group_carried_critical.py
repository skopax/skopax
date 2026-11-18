from tests.conftest import anomaly, state
from tests.test_investigator_tools import box
from skopax.store import Store

PARENT = 'log|checkout-service|WARN|retry'
HOLDER = 'log|checkout-service|WARN|timeout'
CRITICAL = 'log|checkout-service|ERROR|down'


def test_a_card_that_holds_a_critical_card_cannot_go_under_a_handled_parent(store: Store):
    # given
    store.save_state(state(anomaly(PARENT), anomaly(HOLDER), anomaly(CRITICAL, sev='crit', parent=HOLDER)), None)
    b = box(store, keys=(PARENT,))
    b.call('note', {'key': PARENT, 'text': 'A one-off retry spike, it dropped by itself.'})
    handled = b.call('triage', {'key': PARENT, 'state': 'handled'})

    # when
    out, failed = b.call('group', {'parent': PARENT, 'children': [HOLDER]})

    # then
    assert not handled[1], handled[0]
    assert failed and CRITICAL in out and HOLDER in out and 'leave' in out, out
    assert store.anomaly(CRITICAL)['parent'] == HOLDER and store.anomaly(HOLDER).get('parent') is None


def test_a_card_whose_merged_cards_are_warnings_still_goes_under_a_handled_parent(store: Store):
    # given
    store.save_state(state(anomaly(PARENT), anomaly(HOLDER), anomaly(CRITICAL, parent=HOLDER)), None)
    b = box(store, keys=(PARENT,))
    b.call('note', {'key': PARENT, 'text': 'A one-off retry spike, it dropped by itself.'})
    b.call('triage', {'key': PARENT, 'state': 'handled'})

    # when
    out, failed = b.call('group', {'parent': PARENT, 'children': [HOLDER]})

    # then
    assert not failed, out
    assert store.anomaly(HOLDER)['parent'] == PARENT and store.anomaly(CRITICAL)['parent'] == PARENT
