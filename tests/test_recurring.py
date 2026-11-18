from tests.conftest import AUTH, anomaly, state
from tests.test_investigator import NOW
from tests.test_slack import FakeSlack, client_with
from skopax import history
from skopax.config import load_settings
from skopax.engine import apply, iso
from skopax.investigator import prompt
from skopax.slack import anomaly_message
from skopax.store import Store

KEY = 'log|reports-service|WARN|ShipCorp 456 invalid client ip'
DAY = 86400


def ago(seconds: float) -> str:
    return iso(NOW - seconds)


def live(**extra) -> dict:
    return anomaly(KEY, svc='reports-service', label='WARN pattern', firstSeen=ago(1800), lastSeen=ago(60), now=12, d1=2, d7=3, unit='', src='logs', ns='-', **extra)


def occurred(store: Store, *days_ago: float, **extra) -> None:
    for d in days_ago:
        first = ago(d * DAY)
        store.card_history.insert_one({'_id': f'{KEY}|{first}', 'key': KEY, 'firstSeen': first, 'lastSeen': first, 'clearedAt': first, 'outcome': 'cleared', **extra})


def stamped(store: Store, card: dict, **opts) -> dict:
    store.save_state({'initialized': True, 'anomalies': [card], 'muted': [], 'history': []}, None)
    apply(store, [], NOW, **opts)
    return store.anomaly(KEY)


def test_two_in_seven_days_is_not_recurring(store: Store):
    # given
    occurred(store, 3)

    # when
    card = stamped(store, live())

    # then
    assert 'recurring' not in card and 'recurringCount' not in card


def test_three_in_seven_days_is_recurring_with_the_count(store: Store):
    # given
    occurred(store, 2, 5)

    # when
    card = stamped(store, live())

    # then
    assert (card['recurring'], card['recurringCount'], card['recurringDays']) == (True, 3, 7)


def test_an_old_occurrence_outside_the_window_does_not_count(store: Store):
    # given
    occurred(store, 2, 8)

    # when
    card = stamped(store, live())

    # then
    assert 'recurring' not in card


def test_the_live_occurrence_counts_once_even_with_its_own_history_doc(store: Store):
    # given
    card = live()
    occurred(store, 4)
    store.card_history.insert_one({'_id': f"{KEY}|{card['firstSeen']}", 'key': KEY, 'firstSeen': card['firstSeen'], 'outcome': 'open', 'lastAsk': {'at': ago(60), 'line': 'x'}})

    # when
    stamped_card = stamped(store, card)

    # then
    assert 'recurring' not in stamped_card


def test_in_progress_with_a_jira_ticket_is_not_flagged_but_free_text_is(store: Store):
    # given
    occurred(store, 1, 2, 3)

    # when
    ticketed = stamped(store, live(triage='progress', ticket='OPS-120'))
    free = stamped(store, live(triage='progress', ticket='Android nonce fix, no Jira task yet'))

    # then
    assert 'recurring' not in ticketed
    assert (free['recurring'], free['recurringCount']) == (True, 4)


def test_a_card_that_stops_recurring_loses_the_flag(store: Store):
    # given
    card = live(recurring=True, recurringCount=3, recurringDays=7)

    # when
    stamped_card = stamped(store, card)

    # then
    assert not {'recurring', 'recurringCount', 'recurringDays'} & set(stamped_card)


def test_the_window_and_the_threshold_are_tunable(store: Store):
    # given
    occurred(store, 9)

    # when
    card = stamped(store, live(), recurring_count=2, recurring_days=14)

    # then
    assert (card['recurring'], card['recurringCount'], card['recurringDays']) == (True, 2, 14)


def test_recurring_keys_come_from_the_config_with_defaults(monkeypatch):
    # given

    # when
    default = load_settings(lambda: {})
    set_ = load_settings(lambda: {'recurring.count': '4', 'recurring.days': '14'})

    # then
    assert (default.recurring_count, default.recurring_days) == (history.RECURRING_COUNT, history.RECURRING_DAYS) == (3, 7)
    assert (set_.recurring_count, set_.recurring_days) == (4, 14)


def test_the_brief_asks_to_say_so_and_recommend_a_ticket():
    # given
    card = live(recurring=True, recurringCount=4, recurringDays=7)

    # when
    text = prompt.card_brief(card, 'https://skopax.example.com')
    plain = prompt.card_brief(live(), 'https://skopax.example.com')

    # then
    assert ('  recurring: fired 4 times in the last 7 days (this one included) and no Jira ticket owns it; say so in your note and recommend a ticket '
            '(you cannot create one: operators open it with Create task on the card)') in text
    assert 'recurring' not in plain


def test_the_slack_post_carries_the_count():
    # when
    msg = anomaly_message(live(recurring=True, recurringCount=4, recurringDays=7), 'https://skopax.example.com/#a=x', 'op@example.com')
    plain = anomaly_message(live(), 'https://skopax.example.com/#a=x', 'op@example.com')

    # then
    facts = ' \u00b7 '.join(e['text'] for e in [b for b in msg['blocks'] if b['type'] == 'context'][-1]['elements'])
    assert '4 times in 7 days' in facts
    assert 'times in' not in str(plain)


def test_a_thread_reply_on_a_recurring_card_says_how_often(store: Store):
    # given
    store.save(state(live(recurring=True, recurringCount=3, recurringDays=7)), None)
    transport = FakeSlack()
    with client_with(store, transport) as client:
        client.post('/api/slack-posts', json={'key': KEY}, headers=AUTH)

        # when
        client.post('/api/slack-updates', json={'key': KEY, 'text': 'Waiting for the ShipCorp IP allow-list.'}, headers=AUTH)

    # then
    _, reply = transport.calls[1]
    assert reply['text'] == 'Waiting for the ShipCorp IP allow-list.\n_Recurring ×3: 3 times in the last 7 days_'


def test_a_card_with_a_broken_first_seen_never_breaks_the_pass(store: Store):
    # given
    occurred(store, 1, 2)
    store.save_state({'initialized': True, 'anomalies': [{**live(), 'firstSeen': 'garbage'}], 'muted': [], 'history': []}, None)

    # when
    applied = apply(store, [], NOW)

    # then
    assert applied is not None and store.anomaly(KEY)['firstSeen'] == 'garbage'
