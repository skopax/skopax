import datetime as dt

from pymongo.errors import PyMongoError

from tests.conftest import AUTH, anomaly, state
from tests.test_deploy_reports import report
from tests.test_investigator_tools import NOW, PLANTED, box, sources
from tests.test_slack import FakeSlack, client_with
from tests.test_tag_responsibles import ADMIN, signed_in
from skopax.autosend import AutoSend, Fallback, PostNow
from skopax.investigator.tools import Effects, Toolbox
from skopax.slack import AUTO_BY, SlackDown
from skopax.store import Store, iso, utc_now

KEY = 'log|checkout-service|ERROR|boom'
TEXT = 'checkout-service errors have been up since 14:05 UTC.'


def seeded(store: Store, sev: str = 'crit', **extra) -> None:
    store.save_state(state(anomaly(KEY, sev=sev, **extra)), None)


class Sender:
    def __init__(self, store: Store, fail: Exception | None = None):
        self.store = store
        self.fail = fail
        self.cards: list[str] = []
        self.reports: list[str] = []

    def card(self, key: str) -> dict:
        if self.fail:
            raise self.fail
        self.cards.append(key)
        self.store.slack_posted(key, {'channel': 'C1', 'ts': f'1.{len(self.cards)}', 'at': iso(utc_now()), 'by': AUTO_BY, 'draft': True})
        return {}

    def report(self, report_id: str) -> dict:
        self.reports.append(report_id)
        self.store.report_slack_posted(report_id, {'channel': 'C1', 'ts': '2.1', 'at': iso(utc_now()), 'by': AUTO_BY, 'draft': True})
        return {}


def worker(store: Store, sender: Sender, on: bool = True) -> AutoSend:
    return AutoSend(store, sender.card, sender.report, on=lambda: on)


def toolbox(store: Store, on: bool = True, woken: list | None = None, report_id: str | None = None, keys=(KEY,)) -> Toolbox:
    woken = [] if woken is None else woken
    effects = Effects(lambda key, by: {}, lambda key, text, by, *_, notice=None: {}, autopost=lambda: on, wake=lambda: woken.append(1))
    b = box(store, keys=keys, run_id='run-1') if report_id is None else Toolbox(sources(), store, effects, [], 'live', NOW, 'https://skopax.example.com', run_id='run-1', report_id=report_id)
    b.effects = effects
    return b


def lines(store: Store) -> list[tuple[str, str]]:
    return [(a['who'], a['text']) for a in store.list_activity(None, 50)]


def test_the_investigators_post_goes_out_at_once_through_the_worker_with_no_draft_left_waiting(store: Store):
    # given
    seeded(store)
    woken: list = []
    sender = Sender(store)

    # when
    out, err = toolbox(store, woken=woken).call('slack_draft', {'key': KEY, 'text': TEXT})
    sent = worker(store, sender).run_once()

    # then
    assert not err, out
    assert 'posting' in out.lower()
    assert woken == [1] and sent == 1 and sender.cards == [KEY]
    assert ('auto', f'Slack post sent · {KEY}') in lines(store)


def test_a_report_post_goes_out_at_once(store: Store):
    # given
    saved = store.add_deploy_report(report())
    woken: list = []
    sender = Sender(store)

    # when
    out, err = toolbox(store, woken=woken, report_id=saved['id']).call('report_slack_draft', {'text': 'OPS 15.10 release: no new errors.'})
    sent = worker(store, sender).run_once()

    # then
    assert not err, out
    assert woken == [1] and sent == 1 and sender.reports == [saved['id']]
    assert ('auto', f"Slack post sent · report {saved['id']}") in lines(store)


def test_auto_send_off_drops_the_post_with_an_activity_line_and_a_note_on_the_card(store: Store):
    # given
    seeded(store)
    sender = Sender(store)

    # when
    out, err = toolbox(store, on=False).call('slack_draft', {'key': KEY, 'text': TEXT})
    sent = worker(store, sender, on=False).run_once()

    # then
    assert not err, out
    assert 'auto-send is off' in out
    card = store.anomaly(KEY)
    assert 'slackDraft' not in card and 'autoSend' not in card and card['slackSkipped']['at']
    assert sent == 0 and sender.cards == []
    assert ('auto', f'Slack post skipped: auto-send is off · {KEY}') in lines(store)


def test_a_post_armed_while_auto_send_is_off_is_skipped_by_the_worker_and_never_sent_later(store: Store):
    # given
    seeded(store)
    store.pod_slack_draft(KEY, TEXT, 'Claude (pod)')
    sender = Sender(store)

    # when
    off = worker(store, sender, on=False).run_once()
    on = worker(store, sender).run_once()

    # then
    assert (off, on) == (0, 0) and sender.cards == []
    assert store.anomaly(KEY)['slackSkipped']['at']


def test_a_run_that_read_planted_text_does_not_post_and_the_card_keeps_the_text(store: Store):
    # given
    seeded(store)
    woken: list = []
    b = toolbox(store, woken=woken)
    b.flags += PLANTED
    sender = Sender(store)

    # when
    out, err = b.call('slack_draft', {'key': KEY, 'text': TEXT})
    sent = worker(store, sender).run_once()

    # then
    assert not err, out
    assert 'not posted' in out.lower()
    card = store.anomaly(KEY)
    assert card['slackDraft']['text'] == TEXT and card['slackDraft']['notAutoSent'] and 'autoSend' not in card
    assert woken == [] and sent == 0 and sender.cards == []


def test_a_later_clean_run_replaces_a_kept_or_failed_text_but_not_a_post_on_its_way(store: Store):
    # given
    seeded(store)
    b = toolbox(store)
    b.flags += PLANTED
    b.call('slack_draft', {'key': KEY, 'text': TEXT})
    sender = Sender(store)

    # when
    on_its_way_out, on_its_way_err = toolbox(store).call('slack_draft', {'key': KEY, 'text': 'A newer text.'})
    blocked_out, blocked_err = toolbox(store).call('slack_draft', {'key': KEY, 'text': 'A third text.'})
    sent = worker(store, sender).run_once()

    # then
    assert not on_its_way_err, on_its_way_out
    assert blocked_err and 'on its way' in blocked_out
    assert sent == 1 and store.anomaly(KEY)['slackDraft']['text'] == 'A newer text.'


def test_a_failed_post_is_retried_then_left_failed_with_its_code(store: Store):
    # given
    seeded(store)
    store.pod_slack_draft(KEY, TEXT, 'Claude (pod)')
    sender = Sender(store, fail=SlackDown('Slack did not answer'))
    w = worker(store, sender)

    # when
    for _ in range(4):
        w.run_once()
        store.anomalies.update_one({'_id': KEY, 'autoSend.claimedAt': {'$exists': False}}, {'$set': {'autoSend.at': iso(utc_now() - dt.timedelta(minutes=1))}})
    auto = store.anomaly(KEY)['autoSend']

    # then
    assert auto['retries'] == 3 and auto['failedAt'] and auto['claimedAt'] and auto['error']
    assert w.run_once() == 0


def test_the_upgrade_sweep_sends_a_young_waiting_draft_once_across_two_copies_and_clears_the_rest(store: Store):
    # given
    young, old, held, planted, manual = (f'log|svc-{n}|ERROR|x' for n in 'abcde')
    store.save_state(state(*(anomaly(k, sev='crit') for k in (young, old, held, planted, manual))), None)
    for k in (young, old, held):
        store.pod_slack_draft(k, TEXT, 'Claude (pod)')
    store.pod_slack_draft(planted, TEXT, 'Claude (pod)', hold='planted text in the esql answer (ignore previous)')
    store.anomalies.update_one({'_id': manual}, {'$set': {'slackDraft': {'text': TEXT, 'by': 'alice@example.com', 'at': iso(utc_now())}}})
    two_hours = iso(utc_now() - dt.timedelta(hours=2))
    store.anomalies.update_one({'_id': old}, {'$set': {'slackDraft.at': two_hours, 'autoSend.draftAt': two_hours, 'autoSend.at': iso(utc_now() + dt.timedelta(minutes=3))}})
    store.anomalies.update_one({'_id': young}, {'$set': {'autoSend.at': iso(utc_now() + dt.timedelta(minutes=4))}})
    store.anomalies.update_one({'_id': held}, {'$set': {'autoSend.holdBy': 'alice@example.com'}})
    sender = Sender(store)
    one, two = worker(store, sender), worker(store, sender)

    # when
    one.sweep()
    two.sweep()
    sent = one.run_once() + two.run_once()
    one.sweep()
    again = two.run_once()

    # then
    assert sent == 1 and again == 0 and sender.cards == [young]
    for k in (old, held, manual):
        card = store.anomaly(k)
        assert 'slackDraft' not in card and 'autoSend' not in card, k
    assert store.anomaly(planted)['slackDraft']['notAutoSent']


def test_the_upgrade_sweep_sends_a_young_report_draft_and_clears_an_old_one(store: Store):
    # given
    young, old = store.add_deploy_report(report()), store.add_deploy_report(report())
    for r in (young, old):
        store.report_slack_draft(r['id'], 'OPS release: no new errors.', 'Claude (pod)')
    store.deploy_reports.update_one({'_id': old['id']}, {'$set': {'slackDraft.at': iso(utc_now() - dt.timedelta(hours=2))}})
    sender = Sender(store)
    w = worker(store, sender)

    # when
    w.sweep()
    sent = w.run_once()

    # then
    assert sent == 1 and sender.reports == [young['id']]
    assert 'slackDraft' not in store.deploy_report(old['id'])


def test_the_sweep_with_auto_send_off_clears_every_waiting_draft_and_posts_nothing(store: Store):
    # given
    seeded(store)
    store.pod_slack_draft(KEY, TEXT, 'Claude (pod)')
    sender = Sender(store)
    w = worker(store, sender, on=False)

    # when
    w.sweep()
    w.run_once()

    # then
    assert sender.cards == [] and 'slackDraft' not in store.anomaly(KEY)


def test_post_at_once_and_the_worker_never_both_post_one_card(store: Store):
    # given
    seeded(store)
    sender = Sender(store)
    w = worker(store, sender)

    def racing(key: str) -> dict:
        w.run_once()
        return sender.card(key)

    now = utc_now().timestamp()
    post_now = PostNow(store, racing, lambda: 'crit', lambda: True)

    # when
    posted = post_now.run([store.anomaly(KEY)], store.load()['anomalies'], now)

    # then
    assert len(sender.cards) == 1
    assert posted in ([KEY], [])


def test_a_person_can_no_longer_write_or_hold_a_draft_and_a_token_post_goes_out(store: Store):
    # given
    seeded(store)
    transport = FakeSlack()

    # when
    with client_with(store, transport) as client:
        hold = client.post('/api/slack-holds', json={'key': KEY}, headers=AUTH)
        report_hold = client.post('/api/deploy-reports/r1/slack-hold', headers=AUTH)
        token = client.put('/api/slack-drafts', json={'key': KEY, 'text': TEXT}, headers=AUTH)
        sent = client.app.state.auto_send.run_once()

    # then
    assert (hold.status_code, report_hold.status_code) == (410, 410)
    assert token.status_code == 200 and sent == 1 and len(transport.calls) == 1


def test_a_person_gets_gone_for_the_draft_edit_of_a_card_and_of_a_report(store: Store):
    # given
    seeded(store)
    person = signed_in(store, (ADMIN,))

    # when
    card = person.put('/api/slack-drafts', json={'key': KEY, 'text': TEXT})
    rep = person.put('/api/deploy-reports/r1/slack-draft', json={'text': TEXT})

    # then
    assert (card.status_code, rep.status_code) == (410, 410)
    assert 'Send to Slack' in card.json()['detail']
    assert 'slackDraft' not in store.anomaly(KEY)


def triaged_with_text(store: Store, triage: str = 'handled') -> None:
    seeded(store)
    store.pod_slack_draft(KEY, TEXT, 'Claude (pod)')
    store.stop_anomaly_auto_send(KEY)
    store.anomalies.update_one({'_id': KEY}, {'$set': {'triage': triage}})


def test_slack_send_requeues_for_the_worker_which_posts_a_triaged_card_and_retries_a_failure(store: Store):
    # given
    triaged_with_text(store)
    failing = Sender(store, fail=SlackDown('Slack did not answer'))
    w = worker(store, failing)

    # when
    said = w.send_soon(KEY)
    first = w.run_once()
    auto = store.anomaly(KEY)['autoSend']
    store.anomalies.update_one({'_id': KEY}, {'$set': {'autoSend.at': iso(utc_now() - dt.timedelta(minutes=1))}})
    failing.fail = None
    second = w.run_once()

    # then
    assert said == 'queued' and first == 0
    assert auto['retries'] == 1 and 'claimedAt' not in auto
    assert second == 1 and failing.cards == [KEY]


def test_slack_send_with_auto_send_off_is_skipped_with_a_note_and_the_tool_says_so(store: Store):
    # given
    triaged_with_text(store)
    b = toolbox(store, on=False)
    b.drafted[KEY] = __import__('skopax.store', fromlist=['draft_hash']).draft_hash(TEXT)
    b.writes.append({'tool': 'triage', 'key': KEY, 'summary': 'handled', 'proposed': False})

    # when
    out, err = b.call('slack_send', {'key': KEY})
    said = worker(store, Sender(store), on=False).send_soon(KEY)

    # then
    assert not err, out
    assert 'auto-send is off' in out
    card = store.anomaly(KEY)
    assert card['slackSkipped']['at'] and 'slackDraft' not in card
    assert said == 'nothing to post'
    assert ('auto', f'Slack post skipped: auto-send is off · {KEY}') in lines(store)


def test_a_claim_left_by_a_dead_send_goes_back_to_the_queue_after_15_minutes(store: Store):
    # given
    seeded(store)
    store.pod_slack_draft(KEY, TEXT, 'Claude (pod)')
    old = iso(utc_now() - dt.timedelta(minutes=20))
    store.anomalies.update_one({'_id': KEY}, {'$set': {'autoSend.claimedAt': old}})
    sender = Sender(store)

    # when
    sent = worker(store, sender).run_once()

    # then
    assert sent == 1 and sender.cards == [KEY]


def test_a_fresh_claim_is_left_alone(store: Store):
    # given
    seeded(store)
    store.pod_slack_draft(KEY, TEXT, 'Claude (pod)')
    store.anomalies.update_one({'_id': KEY}, {'$set': {'autoSend.claimedAt': iso(utc_now() - dt.timedelta(minutes=2))}})
    sender = Sender(store)

    # when
    sent = worker(store, sender).run_once()

    # then
    assert sent == 0 and sender.cards == []


def test_switch_off_skips_even_while_slack_is_not_ready(store: Store):
    # given
    seeded(store)
    store.pod_slack_draft(KEY, TEXT, 'Claude (pod)')
    w = AutoSend(store, Sender(store).card, Sender(store).report, on=lambda: False, ready=lambda: False)

    # when
    w.run_once()

    # then
    assert store.anomaly(KEY)['slackSkipped']['at']


def test_a_kept_planted_text_never_keeps_a_critical_card_off_slack(store: Store):
    # given
    seeded(store, sev='warn')
    store.pod_slack_draft(KEY, TEXT, 'Claude (pod)', hold='planted text in the esql answer (ignore previous)')
    store.anomalies.update_one({'_id': KEY}, {'$set': {'sev': 'crit'}})
    sender = Sender(store)
    post_now = PostNow(store, sender.card, lambda: 'crit', lambda: True)

    # when
    posted = post_now.run([store.anomaly(KEY)], store.load()['anomalies'], utc_now().timestamp())

    # then
    assert posted == [KEY] and sender.cards == [KEY]
    assert store.anomaly(KEY)['slackDraft']['by'] == 'server' and TEXT not in store.anomaly(KEY)['slackDraft']['text']


def test_a_failed_text_is_replaced_by_the_server_fallback(store: Store):
    # given
    seeded(store)
    store.pod_slack_draft(KEY, TEXT, 'Claude (pod)')
    store.anomalies.update_one({'_id': KEY}, {'$set': {'autoSend.claimedAt': iso(utc_now()), 'autoSend.failedAt': iso(utc_now()), 'autoSend.retries': 3}})

    # when
    saved = store.fallback_slack_draft(KEY, 'numbers only', 'server', iso(utc_now()), utc_now(), ())

    # then
    assert saved is not None and store.anomaly(KEY)['slackDraft']['text'] == 'numbers only'


def test_the_fallback_queues_nothing_while_auto_send_is_off(store: Store):
    # given
    fired = iso(utc_now() - dt.timedelta(minutes=20))
    store.save_state(state(anomaly(KEY, sev='crit', firstSeen=fired, pushedAt=fired)) | {'history': [{'key': KEY, 'at': fired, 't': fired, 'event': 'fired'}]}, None)
    fb = Fallback(store, 15, on=lambda: False)

    # when
    count = fb.run_once(utc_now().timestamp())

    # then
    card = store.anomaly(KEY)
    assert count == 0 and 'slackDraft' not in card and 'slackFallback' not in card and 'slackSkipped' not in card


def test_the_upgrade_sweep_runs_once_and_a_later_start_keeps_waiting_posts(store: Store):
    # given
    seeded(store)
    store.pod_slack_draft(KEY, TEXT, 'Claude (pod)')
    store.anomalies.update_one({'_id': KEY}, {'$set': {'slackDraft.at': iso(utc_now() - dt.timedelta(hours=2))}})
    w = worker(store, Sender(store))
    w.sweep()
    store.pod_slack_draft(KEY, TEXT, 'Claude (pod)')
    store.anomalies.update_one({'_id': KEY}, {'$set': {'slackDraft.at': iso(utc_now() - dt.timedelta(hours=2)), 'autoSend.draftAt': iso(utc_now() - dt.timedelta(hours=2))}})

    # when
    again = worker(store, Sender(store)).sweep()

    # then
    assert again == 0 and store.anomaly(KEY)['slackDraft']['text'] == TEXT


def test_the_upgrade_sweep_with_auto_send_off_notes_the_young_posts_it_skips(store: Store):
    # given
    seeded(store)
    store.pod_slack_draft(KEY, TEXT, 'Claude (pod)')

    # when
    worker(store, Sender(store), on=False).sweep()

    # then
    assert store.anomaly(KEY)['slackSkipped']['at']
    assert ('auto', f'Slack post skipped: auto-send is off · {KEY}') in lines(store)


def test_a_token_post_with_a_link_to_an_unknown_host_is_refused_with_the_reason(store: Store):
    # given
    seeded(store)

    # when
    with client_with(store, FakeSlack()) as client:
        bad = client.put('/api/slack-drafts', json={'key': KEY, 'text': 'See https://evil.example.net/x'}, headers=AUTH)
        rep = client.post('/api/deploy-reports', json=report(), headers=AUTH).json()
        bad_rep = client.put(f"/api/deploy-reports/{rep['id']}/slack-draft", json={'text': 'Ping <!channel>'}, headers=AUTH)

    # then
    assert bad.status_code == 400 and 'evil.example.net' in bad.json()['detail']
    assert bad_rep.status_code == 400 and 'mention' in bad_rep.json()['detail']
    assert 'slackDraft' not in store.anomaly(KEY)


def test_a_post_that_went_out_but_was_not_recorded_is_never_posted_again(store: Store, monkeypatch):
    # given
    seeded(store)
    transport = FakeSlack()
    with client_with(store, transport) as client:
        store.pod_slack_draft(KEY, TEXT, 'Claude (pod)')
        real = store.slack_posted

        def down(key, post):
            raise PyMongoError('down')

        monkeypatch.setattr(store, 'slack_posted', down)

        # when
        client.app.state.auto_send.run_once()
        monkeypatch.setattr(store, 'slack_posted', real)
        store.anomalies.update_one({'_id': KEY}, {'$set': {'autoSend.claimedAt': iso(utc_now() - dt.timedelta(minutes=20))}})
        again = client.app.state.auto_send.run_once()

    # then
    auto = store.anomaly(KEY)['autoSend']
    assert len(transport.calls) == 1 and again == 0
    assert auto['failedAt'] and 'not recorded' in auto['error']


def test_an_old_stale_claim_is_never_posted_and_reads_failed(store: Store):
    # given
    seeded(store)
    store.pod_slack_draft(KEY, TEXT, 'Claude (pod)')
    two_hours = iso(utc_now() - dt.timedelta(hours=2))
    store.anomalies.update_one({'_id': KEY}, {'$set': {'slackDraft.at': two_hours, 'autoSend.draftAt': two_hours, 'autoSend.claimedAt': two_hours}})
    sender = Sender(store)

    # when
    sent = worker(store, sender).run_once()

    # then
    auto = store.anomaly(KEY)['autoSend']
    assert sent == 0 and sender.cards == []
    assert auto['failedAt'] and auto['claimedAt'] and auto['error']


def test_the_worker_never_posts_a_queued_text_older_than_an_hour_on_its_own(store: Store):
    # given
    seeded(store)
    store.pod_slack_draft(KEY, TEXT, 'Claude (pod)')
    two_hours = iso(utc_now() - dt.timedelta(hours=2))
    store.anomalies.update_one({'_id': KEY}, {'$set': {'slackDraft.at': two_hours, 'autoSend.draftAt': two_hours}})
    sender = Sender(store)

    # when
    sent = worker(store, sender).run_once()

    # then
    assert sent == 0 and sender.cards == [] and 'autoSend' not in store.anomaly(KEY)


def test_a_sweep_a_dead_copy_started_is_finished_by_the_next_start(store: Store):
    # given
    seeded(store)
    store.pod_slack_draft(KEY, TEXT, 'Claude (pod)')
    store.anomalies.update_one({'_id': KEY}, {'$set': {'autoSend.holdBy': 'alice@example.com'}})
    store.counters.insert_one({'_id': 'slack-direct-sweep', 'startedAt': iso(utc_now() - dt.timedelta(minutes=20)), 'done': False})

    # when
    worker(store, Sender(store)).sweep()

    # then
    assert 'slackDraft' not in store.anomaly(KEY)
    assert store.counters.find_one({'_id': 'slack-direct-sweep'})['done'] is True


def test_a_sweep_another_copy_is_running_now_is_left_to_it(store: Store):
    # given
    seeded(store)
    store.pod_slack_draft(KEY, TEXT, 'Claude (pod)')
    store.anomalies.update_one({'_id': KEY}, {'$set': {'autoSend.holdBy': 'alice@example.com'}})
    store.counters.insert_one({'_id': 'slack-direct-sweep', 'startedAt': iso(utc_now() - dt.timedelta(minutes=2)), 'done': False})

    # when
    worker(store, Sender(store)).sweep()

    # then
    assert store.anomaly(KEY)['slackDraft']['text'] == TEXT


def test_a_report_post_that_went_out_but_was_not_recorded_is_never_posted_again(store: Store, monkeypatch):
    # given
    transport = FakeSlack()
    with client_with(store, transport) as client:
        saved = store.add_deploy_report(report())
        store.report_slack_draft(saved['id'], 'OPS release: no new errors.', 'Claude (pod)')

        def down(report_id, post):
            raise PyMongoError('down')

        monkeypatch.setattr(store, 'report_slack_posted', down)

        # when
        client.app.state.auto_send.run_once()
        monkeypatch.undo()
        store.deploy_reports.update_one({'_id': saved['id']}, {'$set': {'autoSend.claimedAt': iso(utc_now() - dt.timedelta(minutes=20))}})
        again = client.app.state.auto_send.run_once()

    # then
    auto = store.deploy_report(saved['id'])['autoSend']
    assert len(transport.calls) == 1 and again == 0
    assert auto['failedAt'] and 'not recorded' in auto['error']


def test_a_text_dropped_for_age_leaves_a_note_and_an_activity_line(store: Store):
    # given
    seeded(store)
    store.pod_slack_draft(KEY, TEXT, 'Claude (pod)')
    two_hours = iso(utc_now() - dt.timedelta(hours=2))
    store.anomalies.update_one({'_id': KEY}, {'$set': {'slackDraft.at': two_hours, 'autoSend.draftAt': two_hours}})

    # when
    worker(store, Sender(store)).run_once()

    # then
    card = store.anomaly(KEY)
    assert card['slackSkipped']['why'] == 'old' and 'slackDraft' not in card
    assert ('auto', f'Slack post skipped: the text was over an hour old · {KEY}') in lines(store)
