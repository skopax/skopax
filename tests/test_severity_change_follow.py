import pytest

from tests.conftest import anomaly
from tests.test_head_state import KEY, seed
from tests.test_slack_layout import head_of, rewrites


def test_a_severity_change_by_hand_redraws_the_card_post(store):
    # given
    posts = seed(store, {'kind': 'opened', 'by': 'server', 'at': '2026-11-01T07:00:00Z'}, sev='warn')

    # when
    store.set_severity(KEY, 'crit', 'alice@example.com')
    posts.after({'type': 'severity', 'key': KEY, 'sev': 'crit', 'requestedBy': 'alice@example.com'})
    posts.run_batch()

    # then
    [update] = rewrites(posts.api)
    assert head_of(update).startswith(':red_circle:'), head_of(update)


def test_a_severity_change_over_the_actions_api_tells_the_holder(store):
    from tests.test_notifications import CARD, NINA, PAUL, board, inbox, page_client

    # given
    board(store, anomaly(CARD, num=14, owner=NINA, sev='warn'))

    # when
    with page_client(store, PAUL) as c:
        from tests.test_auth import log_in
        log_in(c)
        got = c.post('/api/actions', json={'type': 'severity', 'key': CARD, 'target': CARD, 'sev': 'crit'})

    # then
    assert got.status_code == 201 and got.json()['status'] == 'done', got.text
    assert [n['kind'] for n in inbox(store, 'nina.white')] == ['severity']


def test_a_move_to_information_and_back_tells_the_holder_and_redraws(store):
    from tests.test_notifications import CARD, NINA, PAUL, board, inbox, page_client
    from tests.test_auth import log_in

    # given
    board(store, anomaly(CARD, num=14, owner=NINA, sev='warn'))

    # when
    with page_client(store, PAUL) as c:
        log_in(c)
        down = c.post('/api/actions', json={'type': 'severity', 'key': CARD, 'target': CARD, 'sev': 'info'})
        back = c.post('/api/actions', json={'type': 'severity', 'key': CARD, 'target': CARD, 'sev': 'auto'})

    # then
    assert (down.json()['status'], back.json()['status']) == ('done', 'done'), (down.text, back.text)
    assert [n['kind'] for n in inbox(store, 'nina.white')] == ['severity', 'severity']


@pytest.mark.parametrize('sev,mark', [('crit', ':red_circle:'), ('high', ':large_orange_circle:'), ('info', ':large_blue_circle:')])
def test_a_severity_change_over_the_actions_api_redraws_the_post_in_place(store, sev, mark):
    from tests.test_slack_layout import AUTH, FakeSlack, FakeSlackApi, client_with, settled, state

    # given
    store.save(state(anomaly(KEY, num=14, now=5, firstSeen='2026-11-11T07:00:00Z')), None)
    transport, api = FakeSlack(), FakeSlackApi()

    # when
    with client_with(store, transport, api=api) as client:
        client.post('/api/slack-posts', json={'key': KEY}, headers=AUTH)
        client.post('/api/actions', json={'type': 'severity', 'key': KEY, 'sev': sev, 'label': 'Severity'}, headers=AUTH)
        settled(client)

    # then
    updates = [p for m, p in api.calls if m == 'chat.update']
    assert len(updates) == 1 and head_of(updates[0]).startswith(mark), [head_of(u) for u in updates]


def test_a_hand_set_severity_shows_in_the_head_of_a_stopped_or_resolved_post(store):
    from tests.test_head_state import seed

    # given
    stopped = seed(store, {'kind': 'cleared', 'by': 'server', 'at': '2026-11-01T07:00:00Z'}, status='cleared', sev='crit', sevSet={'sev': 'crit', 'by': 'alice@example.com', 'at': '2026-11-02T07:00:00Z', 'auto': 'warn'})

    # when
    stopped.after({'type': 'severity', 'key': KEY, 'sev': 'crit', 'requestedBy': 'alice@example.com'})
    stopped.run_batch()
    store.triage(KEY, 'handled', resolution={'kind': 'fixed', 'by': 'alice@example.com', 'via': 'page'})
    stopped.after({'type': 'severity', 'key': KEY, 'sev': 'crit', 'requestedBy': 'alice@example.com'})
    stopped.run_batch()

    # then
    heads = [head_of(u) for u in rewrites(stopped.api)]
    assert len(heads) == 2 and all(h.endswith('· Critical') for h in heads), heads


def test_a_holder_changing_their_own_card_and_an_unassigned_card_tell_nobody(store):
    from tests.test_notifications import CARD, NINA, OTHER, board, inbox
    from skopax.notices import Notices

    # given
    board(store, anomaly(CARD, num=14, owner=NINA, sev='warn'), anomaly(OTHER, num=15, sev='warn'))
    notices = Notices(store)

    # when
    for key, who in ((CARD, NINA), (OTHER, 'paul.stone@example.com')):
        seen = notices.watch({'type': 'severity', 'key': key})
        store.set_severity(key, 'crit', who)
        notices.applied({'type': 'severity', 'key': key, 'requestedBy': who}, seen)

    # then
    assert inbox(store, 'nina.white') == [] and inbox(store, 'paul.stone') == []


def test_a_redraw_that_cannot_happen_is_written_to_the_card_history(store):
    from tests.test_head_state import seed

    # given
    posts = seed(store, None)
    store.anomalies.update_one({'_id': KEY}, {'$unset': {'slackPost.head': ''}})

    # when
    store.set_severity(KEY, 'crit', 'alice@example.com')
    posts.after({'type': 'severity', 'key': KEY, 'sev': 'crit', 'requestedBy': 'alice@example.com'})
    posts.run_batch()

    # then
    texts = [r['text'] for r in store.card_activity(KEY, 20)]
    assert texts == ['Slack post not updated: the post has no stored headline to redraw'] and rewrites(posts.api) == [], texts


def test_a_flapping_severity_notifies_every_time_but_mails_once_a_minute(store):
    from tests.test_notifications import CARD, NINA, PAUL, board, inbox
    from skopax.notices import Notices

    # given
    board(store, anomaly(CARD, num=14, owner=NINA, sev='warn'))

    class Mail:
        def __init__(self):
            self.rows = []

        def queue(self, rows):
            self.rows += list(rows)

    mail = Mail()
    notices = Notices(store, mail=mail)

    # when
    for sev in ('crit', 'warn', 'high'):
        seen = notices.watch({'type': 'severity', 'key': CARD})
        store.set_severity(CARD, sev, PAUL)
        notices.applied({'type': 'severity', 'key': CARD, 'requestedBy': PAUL}, seen)

    # then
    assert len(inbox(store, 'nina.white')) == 3 and len(mail.rows) == 1


def history_texts(store):
    return [r['text'] for r in store.card_activity(KEY, 20)]


def sev_job(posts):
    store = posts.store
    store.set_severity(KEY, 'crit', 'alice@example.com')
    posts.after({'type': 'severity', 'key': KEY, 'sev': 'crit', 'requestedBy': 'alice@example.com'})


def test_a_redraw_retried_after_slack_was_down_still_writes_the_history_row_when_it_fails_again(store):
    from tests.test_head_state import seed
    from skopax.slack import SlackDown, SlackError

    # given
    posts = seed(store, {'kind': 'opened', 'by': 'server', 'at': '2026-11-01T07:00:00Z'})
    clock = {'t': 0.0}
    posts.timer = lambda: clock['t']
    posts.retry_after_s = 0
    answers = [SlackDown('down'), SlackError('refused', code='invalid_blocks')]

    def update(*args, **kwargs):
        raise answers.pop(0)

    posts.slack.update = update

    # when
    sev_job(posts)
    posts.run_batch()
    first = history_texts(store)
    posts.run_batch()

    # then
    assert first == [] and history_texts(store) == ['Slack post not updated: Slack did not accept the change']


def test_a_deleted_post_is_recorded_as_deleted_both_when_known_and_when_the_rewrite_finds_it_gone(store):
    from tests.test_head_state import seed
    from skopax.slack import SlackError

    # given
    known = seed(store, {'kind': 'opened', 'by': 'server', 'at': '2026-11-01T07:00:00Z'})
    store.slack_post_drawn(KEY, '1792224000.000200', {'sig': 'x', 'at': '2026-11-11T07:00:00Z', 'gone': 'message_not_found'})

    # when
    sev_job(known)
    known.run_batch()

    # then
    assert history_texts(store) == ['Slack post not updated: the Slack post was deleted']

    # given
    fresh = seed(store, {'kind': 'opened', 'by': 'server', 'at': '2026-11-01T07:00:00Z'})

    def update(*args, **kwargs):
        raise SlackError('gone', code='message_not_found')

    fresh.slack.update = update

    # when
    sev_job(fresh)
    fresh.run_batch()

    # then
    assert history_texts(store)[0] == 'Slack post not updated: the Slack post was deleted' and len(history_texts(store)) == 2


def test_a_slack_error_that_is_not_an_outage_is_written_to_the_card_history(store):
    from tests.test_head_state import seed
    from tests.fake_slack import fake_slack
    from skopax.cardposts import CardPosts
    from tests.test_slack_layout import BASE

    # given
    posts = seed(store, {'kind': 'opened', 'by': 'server', 'at': '2026-11-01T07:00:00Z'})
    slack, _, api = fake_slack(fail={'chat.update'})
    posts = CardPosts(store, slack, BASE, autostart=False)

    # when
    sev_job(posts)
    posts.run_batch()

    # then
    assert history_texts(store) == ['Slack post not updated: Slack did not accept the change']


def test_a_redraw_while_slack_is_not_set_up_is_written_to_the_card_history(store):
    from tests.test_head_state import seed

    # given
    posts = seed(store, {'kind': 'opened', 'by': 'server', 'at': '2026-11-01T07:00:00Z'})
    posts.slack = type('Off', (), {'enabled': False})()

    # when
    sev_job(posts)
    posts.run_batch()

    # then
    assert history_texts(store) == ['Slack post not updated: Slack is not set up on this service']


def test_an_empty_chat_effort_saved_after_the_upgrade_shows_one_admin_notice(store):
    import dataclasses
    from fastapi.testclient import TestClient
    from tests.conftest import AUTH, settings
    from skopax.app import create_app

    # given
    with TestClient(create_app(settings(), store)) as c:
        store.db['tunables'].insert_one({'_id': 'chat.effort', 'value': '', 'by': 'old-pod', 'at': '2026-11-01T00:00:00Z'})
        c.app.state.live.refresh(force=True)

        # when
        items = c.get('/api/admin/notices', headers=AUTH).json()['items']

    # then
    assert [n['id'] for n in items].count('chat-effort-empty') == 1
