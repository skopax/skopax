from tests.test_incident_group import OTHER, PARENT, SOAP, TRACKHUB, box, seeded
from skopax.store import Store

MISS = 'log|checkout-service|ERROR|*NullPointerException*'


def test_a_group_absorb_pattern_that_matches_no_card_names_the_group_card_keys(store: Store):
    # given
    b = box(seeded(store), keys=(TRACKHUB, SOAP))

    # when
    out, failed = b.call('group', {'parent': PARENT, 'children': [TRACKHUB, SOAP], 'absorb': [MISS]})

    # then
    assert failed and 'matches no card of this group' in out, out
    assert all(k in out for k in (PARENT, TRACKHUB, SOAP)), out
    assert OTHER not in out
    assert store.anomaly(TRACKHUB).get('parent') is None


def test_the_named_keys_are_capped_at_20_and_cut_to_120_characters_with_a_count_of_the_rest(store: Store):
    # given
    from tests.test_engine import stored
    from tests.conftest import state
    kids = [f'log|reports-service|ERROR|child number {i:02d} ' + 'x' * 150 for i in range(25)]
    store.save({**state(stored(PARENT, src='prom', svc='reports-service', ns='reports', triage='dived', num=144), *[stored(k, src='logs') for k in kids]), 'initialized': True}, None)
    b = box(store, keys=tuple(kids))

    # when
    out, failed = b.call('group', {'parent': PARENT, 'children': kids, 'absorb': [MISS]})

    # then
    assert failed, out
    shown = [k for k in kids if k[:120] in out]
    assert len(shown) == 19
    assert all(k not in out for k in kids)
    assert 'and 6 more' in out, out


def test_a_matching_absorb_pattern_still_groups_as_before(store: Store):
    # given
    b = box(seeded(store), keys=(TRACKHUB,))

    # when
    out, failed = b.call('group', {'parent': PARENT, 'children': [TRACKHUB], 'absorb': ['log|reports-service|ERROR|*TrackHub*']})

    # then
    assert not failed, out
    assert store.anomaly(TRACKHUB)['parent'] == PARENT
