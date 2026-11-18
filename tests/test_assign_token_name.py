from tests.conftest import anomaly, state
from tests.test_assign import NINA, KEY, assigner
from tests.test_slack import FakeSlack
from skopax.store import Store


def test_an_assignment_made_with_the_api_token_is_sent_in_the_name_of_claude(store: Store):
    # given
    t = FakeSlack()
    store.save({**state(anomaly(KEY, num=14, svc='checkout-service', label='ERROR lines')), 'initialized': True}, None)
    a = assigner(store, t, {'U1': NINA})

    # when
    a.assign(KEY, 'Alice Smith', NINA, 'token')

    # then
    [(_, post)] = t.calls
    assert post['text'].startswith('Claude assigned you a Skopax card: *#14 · checkout-service / ERROR lines*')
