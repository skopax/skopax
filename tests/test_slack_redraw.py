import dataclasses
import datetime as dt
import threading
import time

from fastapi.testclient import TestClient

from tests.conftest import AUTH, anomaly, settings, state
from tests.fake_slack import FakePost, FakeSlackApi, fake_slack
from tests.test_slack_layout import BASE, CARD, LINK, Recorder, head_of, posted, posts_of, rewrites
from tests.test_slackevents import CARD_TS
from skopax import app as app_module
from skopax.app import create_app
from skopax.cardposts import REDRAW_CAP, REDRAW_MARK, REDRAW_STALE_S
from skopax.messages import msg
from skopax.slack import Slack, SlackError, anomaly_message, slowed
from skopax.store import Store, iso, utc_now

OLD = '2026-11-01T07:00:00Z'
KEYS = {'open': 'log|a|ERROR|x', 'cleared': 'log|b|ERROR|x', 'closed': 'log|c|ERROR|x', 'stale': 'log|d|ERROR|x', 'gone': 'log|e|ERROR|x'}


def seed(store: Store, **cards) -> None:
    store.save(state(*[anomaly(KEYS[name], num=index + 20, **extra) for index, (name, extra) in enumerate(cards.items())]), None)
    for index, name in enumerate(cards):
        posted(store, KEYS[name], f'1792224000.0002{index:02d}')


def recent() -> str:
    return iso(utc_now())


def test_a_cleared_open_card_wears_the_yellow_mark_and_words_but_a_warning_stays_orange_and_a_closed_card_green():
    # when
    cleared = anomaly_message(CARD | {'status': 'cleared'}, LINK, 'server', head={'kind': 'cleared'})
    warn = anomaly_message(CARD, LINK, 'server', head={'kind': 'reopened', 'by': 'system'})
    closed = anomaly_message(CARD | {'status': 'cleared', 'triage': 'handled'}, LINK, 'server', head={'kind': 'resolved', 'by': 'alice@example.com'})

    # then
    assert head_of(cleared) == ':large_yellow_circle: #14 stopped firing \u00b7 awaiting review'
    assert head_of(warn).startswith(':large_orange_circle:') and head_of(closed).startswith(':white_check_mark:')
    assert ':eyes:' not in head_of(cleared) and msg('slackevents.offer.take', name='alice').startswith('\U0001F440')


def test_every_pack_marks_a_cleared_note_yellow_and_never_with_the_eyes():
    # then
    import json
    from pathlib import Path
    for lang in ('de', 'es', 'fr', 'tr'):
        pack = json.loads((Path(__file__).parent.parent / 'skopax' / 'locale' / lang / 'messages.json').read_text(encoding='utf-8'))
        assert pack['clear.cleared'].startswith('\U0001F7E1 ') and pack['clear.plugin_note'].startswith('\U0001F7E1 ')
        assert pack['slackevents.offer.take'].startswith('\U0001F440')


def test_the_redraw_updates_only_posts_that_exist_through_chat_update_and_posts_nothing_new(store: Store):
    # given
    seed(store, open={}, cleared={'status': 'cleared'}, closed={'triage': 'handled'})
    store.slack_post_head(KEYS['closed'], '1792224000.000202', {'kind': 'resolved', 'by': 'alice@example.com', 'at': recent()})
    slack, post, api = fake_slack()
    posts = posts_of(store, slack, pause=lambda s: None)

    # when
    result = posts.redraw()

    # then
    assert (result['redrawn'], result['skipped'], result['result']) == (3, 0, 'done')
    marks = {u['ts']: head_of(u).split(' ')[0] for u in rewrites(api)}
    assert marks == {'1792224000.000200': ':large_orange_circle:', '1792224000.000201': ':large_yellow_circle:', '1792224000.000202': ':white_check_mark:'}
    assert post.calls == [] and not [m for m, _ in api.calls if m in ('chat.postMessage', 'conversations.replies')]


def test_the_redraw_is_idempotent_and_leaves_the_stored_head_alone(store: Store):
    # given
    seed(store, cleared={'status': 'cleared'})
    store.slack_post_head(KEYS['cleared'], '1792224000.000200', {'kind': 'cleared', 'by': 'server', 'at': OLD})
    slack, _, api = fake_slack()
    posts = posts_of(store, slack, pause=lambda s: None)

    # when
    posts.redraw()
    posts.redraw()

    # then
    sent = rewrites(api)
    assert len(sent) == 2 and sent[0] == sent[1] and head_of(sent[0]) == ':large_yellow_circle: #20 stopped firing \u00b7 awaiting review'
    assert store.anomaly(KEYS['cleared'])['slackPost']['head']['at'] == OLD


def test_the_redraw_covers_every_card_post_still_in_the_store_whatever_its_age(store: Store):
    # given
    seed(store, open={}, closed={'triage': 'handled'}, stale={'triage': 'handled'})
    store.slack_post_head(KEYS['closed'], '1792224000.000201', {'kind': 'resolved', 'by': 'alice@example.com', 'at': recent()})
    store.slack_post_head(KEYS['stale'], '1792224000.000202', {'kind': 'resolved', 'by': 'alice@example.com', 'at': OLD})
    slack, _, api = fake_slack()
    posts = posts_of(store, slack, pause=lambda s: None)

    # when
    result = posts.redraw()

    # then
    assert result['redrawn'] == 3 and sorted(u['ts'] for u in rewrites(api)) == ['1792224000.000200', '1792224000.000201', '1792224000.000202']


def test_a_card_resolved_while_the_redraw_runs_is_drawn_with_its_fresh_state(store: Store):
    # given
    seed(store, open={}, cleared={'status': 'cleared'})
    slack, _, api = fake_slack()
    waits = []

    def pause(seconds):
        waits.append(seconds)
        store.anomalies.update_one({'_id': KEYS['cleared']}, {'$set': {'triage': 'handled'}})

    posts = posts_of(store, slack, pause=pause)

    # when
    posts.redraw()

    # then
    last = rewrites(api)[-1]
    assert last['ts'] == '1792224000.000201' and head_of(last).startswith(':white_check_mark:'), head_of(last)


def test_a_card_reopened_while_the_redraw_runs_is_drawn_open_not_closed(store: Store):
    # given
    seed(store, open={}, closed={'triage': 'handled'})
    store.slack_post_head(KEYS['closed'], '1792224000.000201', {'kind': 'resolved', 'by': 'alice@example.com', 'at': recent()})
    slack, _, api = fake_slack()

    def pause(seconds):
        store.anomalies.update_one({'_id': KEYS['closed']}, {'$unset': {'triage': ''}})

    posts = posts_of(store, slack, pause=pause)

    # when
    posts.redraw()

    # then
    last = rewrites(api)[-1]
    assert last['ts'] == '1792224000.000201' and head_of(last).startswith(':large_orange_circle:') and 'reopened' in head_of(last), head_of(last)


def test_a_row_whose_post_changed_during_the_run_is_skipped(store: Store):
    # given
    seed(store, open={}, cleared={'status': 'cleared'})
    slack, _, api = fake_slack()

    def pause(seconds):
        store.anomalies.update_one({'_id': KEYS['cleared']}, {'$set': {'slackPost.ts': '1792224999.000999'}})

    posts = posts_of(store, slack, pause=pause)

    # when
    result = posts.redraw()

    # then
    assert [u['ts'] for u in rewrites(api)] == ['1792224000.000200'] and (result['redrawn'], result['skipped']) == (1, 1)


def test_a_headless_post_gets_a_head_that_keeps_the_time_of_the_post(store: Store):
    # given
    seed(store, open={})
    store.anomalies.update_one({'_id': KEYS['open']}, {'$unset': {'slackPost.head': ''}})
    slack, _, api = fake_slack()
    posts = posts_of(store, slack, pause=lambda s: None)

    # when
    posts.redraw()

    # then
    head = store.anomaly(KEYS['open'])['slackPost']['head']
    assert head['at'] == '2026-11-11T07:00:00Z' and head['kind'] == 'opened'


def test_the_redraw_stops_at_the_cap_and_throttles_between_posts(store: Store):
    # given
    seed(store, open={}, cleared={'status': 'cleared'}, closed={'triage': 'handled'})
    slack, _, api = fake_slack()
    waits: list[float] = []
    posts = posts_of(store, slack, pause=waits.append)

    # when
    result = posts.redraw(cap=2)

    # then
    assert result['redrawn'] == 2 and len(rewrites(api)) == 2 and len(waits) == 1 and waits[0] > 1 and REDRAW_CAP == 500


def test_a_post_that_is_gone_or_whose_channel_was_left_is_skipped_and_counted_not_retried(store: Store):
    # given
    seed(store, open={}, cleared={'status': 'cleared'})
    slack, _, api = fake_slack()
    codes = {'1792224000.000200': 'message_not_found', '1792224000.000201': 'not_in_channel'}
    real = slack.update

    def update(channel, ts, *a, **k):
        if ts in codes:
            raise SlackError(codes[ts], code=codes[ts])
        return real(channel, ts, *a, **k)

    slack.update = update
    posts = posts_of(store, slack, pause=lambda s: None)

    # when
    first = posts.redraw()
    second = posts.redraw()

    # then
    assert (first['redrawn'], first['skipped'], first['result']) == (0, 2, 'done')
    assert store.anomaly(KEYS['open'])['slackPost']['drawn']['gone'] == 'message_not_found'
    assert (second['redrawn'], second['skipped']) == (0, 2)


def test_a_slowed_slack_waits_its_time_and_the_redraw_goes_on(store: Store):
    # given
    seed(store, open={})
    slack, _, api = fake_slack()
    waits: list[float] = []
    real = slack.update
    calls = {'n': 0}

    def update(*a, **k):
        calls['n'] += 1
        if calls['n'] == 1:
            raise slowed(7)
        return real(*a, **k)

    slack.update = update
    posts = posts_of(store, slack, pause=waits.append)

    # when
    result = posts.redraw()

    # then
    assert result['redrawn'] == 1 and waits[0] == 7.0


def test_the_first_start_redraws_once_even_when_two_copies_start(store: Store, monkeypatch):
    monkeypatch.setattr(app_module, 'REDRAW_START_DELAY_S', 0)
    # given
    seed(store, open={}, cleared={'status': 'cleared'})
    store.anomalies.update_many({}, {'$set': {'slackPost.drawn.sig': 'old'}})
    slack, _, api = fake_slack()
    one = TestClient(create_app(settings(), store, slack=slack))
    two = TestClient(create_app(settings(), store, slack=slack))

    # when
    with one, two:
        for _ in range(100):
            if (store.counters.find_one({'_id': REDRAW_MARK}) or {}).get('done'):
                break
            time.sleep(0.1)
        time.sleep(0.5)

    # then
    marker = store.counters.find_one({'_id': REDRAW_MARK})
    assert marker['done'] is True
    first_pass = [u['ts'] for u in rewrites(api) if u['ts'] in ('1792224000.000200', '1792224000.000201')]
    assert sorted(first_pass) == ['1792224000.000200', '1792224000.000201']
    acts = [a for a in store.list_activity(None, 50) if a.get('text') == msg('slack.redrawn', count=2)]
    assert len(acts) == 1


def test_the_admin_redraw_runs_the_job_writes_the_activity_line_and_a_click_on_another_copy_does_nothing_while_it_runs(store: Store):
    # given
    seed(store, open={}, cleared={'status': 'cleared'})
    store.counters.insert_one({'_id': REDRAW_MARK, 'startedAt': recent(), 'done': True})
    gate, entered = threading.Event(), threading.Event()
    api = Recorder(before=lambda method, payload: (entered.set(), gate.wait(5)) if method == 'chat.update' else None)
    slack = Slack('xoxb-test', '#ops', FakePost(), api, upload=api.upload, sleep=api.sleeps.append)
    other_api = FakeSlackApi()
    other = Slack('xoxb-test', '#ops', FakePost(), other_api, upload=other_api.upload, sleep=other_api.sleeps.append)

    with TestClient(create_app(settings(), store, slack=slack)) as one, TestClient(create_app(settings(), store, slack=other)) as two:
        # when
        started = one.post('/api/admin/slack-redraw', headers=AUTH)
        assert entered.wait(5)
        again = two.post('/api/admin/slack-redraw', headers=AUTH)
        again_here = one.post('/api/admin/slack-redraw', headers=AUTH)
        running = two.get('/api/admin/slack-redraw', headers=AUTH).json()
        gate.set()
        for _ in range(100):
            if not two.get('/api/admin/slack-redraw', headers=AUTH).json().get('running'):
                break
            time.sleep(0.1)
        final = two.get('/api/admin/slack-redraw', headers=AUTH).json()

        # then
        assert started.status_code == 202 and started.json()['started'] is True
        assert again.json()['started'] is False and again_here.json()['started'] is False
        assert running['running'] is True and running['total'] == 2
        assert final['running'] is False and (final['done'], final['result']) == (2, 'done')
        lines = [a for a in one.get('/api/activity', headers=AUTH).json() if msg('slack.redrawn', count=2) in str(a.get('text') or a)]
        assert len(lines) == 1
    assert len([1 for m, _ in api.calls if m == 'chat.update']) == 2
    assert len([1 for m, _ in other_api.calls if m == 'chat.update']) == 0


def test_a_run_whose_lease_went_stale_is_taken_over_and_a_live_one_is_not(store: Store):
    # given
    stale = iso(utc_now() - dt.timedelta(seconds=REDRAW_STALE_S + 60))

    # when
    first = store.redraw_claim('a', stale)
    live_second = store.redraw_claim('b', stale)
    store.counters.update_one({'_id': 'slack-redraw-run'}, {'$set': {'heartbeat': iso(utc_now() - dt.timedelta(seconds=REDRAW_STALE_S + 120))}})
    taken = store.redraw_claim('b', iso(utc_now() - dt.timedelta(seconds=REDRAW_STALE_S)))
    lost = store.redraw_progress('a', done=1)
    kept = store.redraw_progress('b', done=1, total=5)

    # then
    assert (first, live_second, taken, lost, kept) == (True, False, True, False, True)
    assert store.redraw_status()['done'] == 1 and store.redraw_status()['running'] is True


def test_the_progress_heartbeat_keeps_a_long_run_from_being_taken_over(store: Store):
    # given
    seed(store, open={}, cleared={'status': 'cleared'})
    slack, _, _ = fake_slack()
    beats = []

    def pause(seconds):
        beats.append(store.redraw_status()['heartbeat'])

    posts = posts_of(store, slack, pause=pause)

    # when
    posts.redraw()

    # then
    assert len(beats) == 1 and beats[0] >= store.redraw_status()['startedAt']


def test_the_admin_redraw_is_for_admins_only(store: Store):
    # given
    s = dataclasses.replace(settings(), token='')
    slack, _, _ = fake_slack()

    with TestClient(create_app(s, store, slack=slack)) as client:
        # when
        anonymous = client.post('/api/admin/slack-redraw')
        status = client.get('/api/admin/slack-redraw')

        # then
        assert anonymous.status_code in (401, 403) and status.status_code in (401, 403)
