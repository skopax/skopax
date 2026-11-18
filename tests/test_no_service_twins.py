from tests.conftest import anomaly, state
from tests.test_autosend import FB_NOW, crit, drafted_keys, fired
from tests.test_investigator_tools import NOW, box
from skopax.autosend import Fallback
from skopax.engine import iso as epoch_iso
from skopax.investigator import select
from skopax.slack import AUTO_BY
from skopax.store import Store

POSTED = 'alert|TargetDown|namespace=monitoring'
FRESH = 'alert|AirbyteSyncFailed|namespace=airbyte'


def seeded(store: Store) -> Store:
    store.save_state(state(anomaly(POSTED, svc='-', ns='monitoring', slackPost={'ts': '1.2', 'at': epoch_iso(NOW - 3600), 'channel': 'C1', 'by': 'auto'}),
                           anomaly(FRESH, svc='-', ns='airbyte')), None)
    return store


def test_a_card_with_no_service_gets_its_own_draft_while_another_card_with_no_service_is_announced(store: Store):
    # given
    b = box(seeded(store), keys=(FRESH,))

    # when
    out, failed = b.call('slack_draft', {'key': FRESH, 'text': 'The Airbyte sync failed 3 times in a row.'})

    # then
    assert not failed, out
    assert store.anomaly(FRESH)['slackDraft']['text'] == 'The Airbyte sync failed 3 times in a row.'


def test_a_run_cannot_reply_in_the_thread_of_an_unrelated_card_that_also_has_no_service(store: Store):
    # given
    b = box(seeded(store), keys=(FRESH,))

    # when
    out, failed = b.call('slack_update', {'key': POSTED, 'text': 'Update: the airbyte sync is fixed.'})

    # then
    assert failed and 'not one of the cards this run investigates' in out, out
    assert b.writes == []


def test_the_announce_rule_names_no_twin_for_a_card_with_no_service_and_still_one_for_a_service():
    # given
    post = {'ts': '1.2', 'at': epoch_iso(NOW - 3600), 'channel': 'C1', 'by': 'auto'}
    cards = [anomaly(POSTED, svc='-', ns='monitoring', slackPost=post), anomaly(FRESH, svc='-', ns='airbyte'),
             anomaly('log|checkout-service|ERROR|x', slackPost=post), anomaly('log|checkout-service|ERROR|y')]

    # when
    no_service = select.announced(cards[1], cards, NOW)
    same_service = select.announced(cards[3], cards, NOW)

    # then
    assert no_service is None
    assert same_service == {'key': 'log|checkout-service|ERROR|x', 'svc': 'checkout-service', 'posted': True}


def test_the_critical_fallback_drafts_a_card_with_no_service_while_another_card_with_no_service_is_announced(store: Store):
    # given
    fired(store, crit('alert|KubeNodeNotReady|namespace=monitoring', svc='-', ns='monitoring', slackPost={'at': '2026-10-26T05:30:00Z', 'ts': '1.2', 'channel': 'C1', 'by': AUTO_BY}),
          crit('alert|AirbyteDown|namespace=airbyte', svc='-', ns='airbyte'))

    # when
    count = Fallback(store, 15).run_once(FB_NOW)

    # then
    assert count == 1
    assert drafted_keys(store) == ['alert|AirbyteDown|namespace=airbyte']
