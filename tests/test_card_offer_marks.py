from tests.conftest import anomaly, state
from tests.test_slackevents import CARD_TS, KEY, NOW, TAKES, intake, offers, reply_event
from skopax.engine import OFFER_MARKS, PassResult, apply, iso
from skopax.store import Store


def seen_again() -> PassResult:
    found = {'key': KEY, 'src': 'logs', 'sig': 'log', 'ns': 'a', 'svc': 'checkout-service', 'sub': 'ERROR|x', 'sev': 'warn', 'now': 40, 'd1': None, 'd7': None, 'unit': '',
             'label': 'ERROR pattern', 'what': 'x', 'short': 'WARN checkout-service ERROR pattern', 'slack': 1}
    return PassResult('logs', True, 'ok', [found])


def test_a_card_seen_again_by_its_pass_still_gets_its_in_progress_offer_in_slack(store: Store):
    # given
    store.save({**state(anomaly(KEY, src='logs', firstSeen=iso(NOW - 900), lastSeen=iso(NOW - 900), peak=40, pushedAt=iso(NOW - 900), pushedSev='warn')), 'initialized': True}, None)
    store.slack_posted(KEY, {'channel': 'C1', 'ts': CARD_TS, 'at': iso(NOW - 600), 'by': 'server'})
    apply(store, [seen_again()], NOW - 60)
    box, f = intake(store, gate_replies=[TAKES])

    # when
    got = box.handle(reply_event('looking into it'))

    # then
    stored = store.anomalies.find_one({'_id': KEY})
    assert got == 'take-offered' and len(offers(store)) == 1 and len(f.post.calls) == 1
    assert stored['takeOffered']['by'] == 'adam.baker@example.com'
    assert not any(m in stored for m in OFFER_MARKS if m != 'takeOffered'), 'a pass leaves no empty offer marks behind'


def test_an_offer_mark_stored_empty_by_an_earlier_pass_does_not_block_the_offer(store: Store):
    # given
    store.save({**state(anomaly(KEY, takeOffered=None, resolveOffered=None)), 'initialized': True}, None)

    # when
    took = store.offer_card(KEY, 'take', {'ts': '1.2', 'by': 'nina.white@example.com', 'at': iso(NOW)})
    again = store.offer_card(KEY, 'take', {'ts': '1.3', 'by': 'paul.stone@example.com', 'at': iso(NOW)})

    # then
    assert (took, again) == (True, False)
    assert store.anomaly(KEY)['takeOffered']['by'] == 'nina.white@example.com'
