from tests.conftest import anomaly, state
from tests.test_investigator_tools import box
from skopax.investigator.tools import Effects
from skopax.slack import AUTO_BY
from skopax.store import Store, iso, utc_now

KEY = 'log|checkout-service|ERROR|boom'


def drafted(store: Store, posted: bool):
    store.save_state(state(anomaly(KEY)), None)
    sent = []
    b = box(store, keys=(KEY,))
    b.effects = Effects(lambda key, by: sent.append(key) or {}, lambda key, text, by, *_, notice=None: {})
    b.call('slack_draft', {'key': KEY, 'text': 'checkout-service errors have been up since 14:05 UTC.'})
    if posted:
        store.slack_posted(KEY, {'channel': 'C1', 'ts': '1.2', 'at': iso(utc_now()), 'by': AUTO_BY, 'draft': True})
    b.call('note', {'key': KEY, 'text': 'A one-off retry spike.'})
    b.call('triage', {'key': KEY, 'state': 'handled'})
    return b, sent


def test_slack_send_after_the_draft_already_posted_itself_answers_with_the_post_time_and_sends_nothing(store: Store):
    # given
    b, sent = drafted(store, posted=True)
    at = store.anomaly(KEY)['slackPost']['at']

    # when
    out, failed = b.call('slack_send', {'key': KEY})

    # then
    assert not failed, out
    assert at in out and 'already' in out
    assert sent == [] and [w['tool'] for w in b.writes] == ['slack_draft', 'note', 'triage']


def test_slack_send_still_posts_a_draft_the_auto_send_skipped(store: Store):
    # given
    b, sent = drafted(store, posted=False)

    # when
    out, failed = b.call('slack_send', {'key': KEY})

    # then
    assert not failed, out
    assert sent == [KEY] and b.writes[-1]['tool'] == 'slack_send'
