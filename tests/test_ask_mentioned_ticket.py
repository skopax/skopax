from tests.conftest import anomaly
from tests.fake_anthropic import reply, text
from tests.test_investigator import KEY, NOW, seed
from tests.test_investigator_slack import slack_action, slack_investigator
from skopax.store import Store


def test_the_jira_key_a_card_thread_ask_names_still_goes_on_the_take_button(store: Store):
    # given
    seed(store, anomaly(KEY), history=[])
    act = slack_action(store, mentionedTicket='INFRA-402')
    inv, _ = slack_investigator(store, [reply(text('The button is below.'), stop='end_turn')])

    # when
    record = inv.run_once(NOW)

    # then
    assert record['cardAction'] == {'key': KEY, 'action': 'take', 'ticket': 'INFRA-402'}
    assert store.get_action(act['id'])['cardAction'] == record['cardAction'] and 'ticket' not in store.get_action(act['id'])
