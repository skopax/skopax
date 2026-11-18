from tests.conftest import state
from tests.test_investigator_tools import NOW, box, ro_box
from tests.test_tags import open_card
from skopax.engine import iso as epoch_iso
from skopax.store import Store

RUN = 'log|web-bff|ERROR|Connection refused: payments-api.shop'
TWIN = RUN + '.svc:8080 (retry 3)'


def seeded(store: Store) -> Store:
    store.save({**state(open_card(RUN, svc='web-bff'),
                        open_card(TWIN, svc='web-bff', triage='dived', slackPost={'ts': '1.2', 'at': epoch_iso(NOW - 1800), 'channel': 'C1', 'by': 'auto'})),
                'initialized': True}, None)
    return store


def test_the_exact_key_of_a_stored_card_is_not_bent_to_a_run_card_whose_key_it_starts_with(store: Store):
    # given
    b = box(seeded(store), keys=(RUN,))

    # when
    note, note_failed = b.call('note', {'key': TWIN, 'text': 'same root cause.'})
    grouped, group_failed = b.call('group', {'parent': TWIN, 'children': [RUN]})
    update, update_failed = b.call('slack_update', {'key': TWIN, 'text': 'the payments-api connection is back.'})

    # then
    assert note_failed and 'not one of the cards this run investigates' in note, note
    assert store.anomaly(RUN).get('note') is None
    assert not group_failed and store.anomaly(RUN)['parent'] == TWIN, grouped
    assert not update_failed and update == f'slack_update saved on {TWIN}', update


def test_a_cut_key_in_a_proposal_still_finds_the_run_card(store: Store):
    # given
    b = ro_box(seeded(store), keys=(RUN,))

    # when
    out, failed = b.call('propose_card_action', {'key': RUN[:60], 'action': 'resolve'})

    # then
    assert not failed, out
    assert b.card_action == {'key': RUN, 'action': 'resolve'}
