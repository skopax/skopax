from tests.conftest import state
from tests.test_investigator_tools import box
from tests.test_tags import open_card
from skopax.store import Store

LONG = 'log|web-bff|WARN|Rest error occurred. Url: http://orders-api.orders.svc:8080/v1/orders/garden/sofa/detailed?car'
CART = 'log|web-bff|WARN|Rest error occurred. Url: http://checkout-service.shop.svc:8080/v1/checkout/cart/totals/detailed'
OTHER = 'log|web-bff|WARN|Store not found.(Store not found.)'


def seeded(store: Store) -> Store:
    store.save({**state(open_card(LONG, svc='web-bff'), open_card(CART, svc='web-bff'), open_card(OTHER, svc='web-bff')), 'initialized': True}, None)
    return store


def test_a_card_key_given_cut_short_or_with_the_rest_of_the_log_line_still_finds_its_card(store: Store):
    # given
    b = box(seeded(store), keys=(LONG, OTHER))

    # when
    cut, cut_error = b.call('note', {'key': LONG[:100], 'text': 'orders-api 404, the client has no order.'})
    longer, longer_error = b.call('note', {'key': LONG + 'dId=123&channel=WEB', 'text': 'same root cause.'})
    grouped, group_error = b.call('group', {'parent': LONG[:100], 'children': [CART[:104]]})

    # then
    assert not cut_error and cut == f'note saved on {LONG}'
    assert not longer_error and store.anomaly(LONG)['note']['text'] == 'same root cause.'
    assert not group_error and store.anomaly(CART)['parent'] == LONG, 'a child outside the run is found among the stored cards by its cut key'


def test_a_cut_key_that_fits_two_cards_or_is_too_short_is_left_as_it_was_given(store: Store):
    # given
    b = box(seeded(store), keys=(LONG, CART))

    # when
    both, both_error = b.call('note', {'key': LONG[:48], 'text': 'x'})
    short, short_error = b.call('note', {'key': 'log|web-bff|WARN', 'text': 'x'})

    # then
    assert both_error and 'is not one of the cards this run investigates' in both
    assert short_error and store.anomaly(LONG).get('note') is None
