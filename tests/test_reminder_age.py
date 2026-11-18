from tests.test_reminders import BASE, H, POSTED, line, reminders
from tests.test_state_notes import KEY, POST_TS, QUIET, Transport, card, seed
from skopax.engine import iso
from skopax.messages import msg
from skopax.store import Store

DAY = 24 * H
REOPENED = POSTED + 15 * DAY


def test_a_card_that_fired_again_long_after_its_post_is_said_to_wait_since_it_fired_again(store: Store):
    # given
    seed(store, card(KEY, firstSeen=iso(REOPENED)))
    t = Transport()

    # when
    early = reminders(store, t).run_once(REOPENED + 23 * H)
    sent = reminders(store, t).run_once(REOPENED + 25 * H)

    # then
    assert (early, sent) == ([], [KEY])
    assert t.replies() == [(POST_TS, line('for 25 hours', msg('reminder.still')))]


def test_a_shared_thread_reminder_counts_each_card_from_when_it_fired_this_time(store: Store):
    # given
    seed(store, card(KEY, firstSeen=iso(REOPENED)), card(QUIET, firstSeen=iso(REOPENED + 2 * H), svc='payments-api'))
    t = Transport()

    # when
    sent = reminders(store, t).run_once(REOPENED + 26 * H)

    # then
    assert sorted(sent) == sorted([KEY, QUIET])
    assert t.calls[0]['text'].split('\n')[0] == '⏰ Reminder: the 2 cards on this topic have been waiting for 26 hours, nobody has taken them yet.'
    assert BASE in t.calls[0]['text']
