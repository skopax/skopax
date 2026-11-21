import dataclasses
import datetime as dt

import pytest
from fastapi.testclient import TestClient
from pymongo.errors import PyMongoError

from tests.conftest import AUTH, anomaly, settings, state
from tests.test_deploy_reports import report
from tests.test_slack import KEY, FakeSlack, client_with
from skopax.app import create_app
from skopax.autosend import PostNow, Fallback, fallback_text, fresh
from skopax.engine import Applied, parse_iso
from skopax.messages import msg
from skopax.slack import AUTO_BY, Slack, SlackDown
from skopax.store import NotFound, Store, iso, utc_now

DRAFT = 'web-bff showed 116 WARN, 13:05 UTC.\nA logo record is missing, the product team is on it.\nhttps://skopax.example.com/#a=x'
OTHER = 'The text changed, this is another draft now.'
REPORT_DRAFT = 'OPS 15.10 release: 14 services deployed, no new errors.\nhttps://skopax.example.com/#r=x'
FB_KEY = '5xx|platform/audit-service'
TWIN_KEY = 'restarts|platform/audit-service'
FIRED = '2026-10-26T06:05:00Z'
FB_NOW = parse_iso('2026-10-26T06:21:00Z')
FB_TEXT = ("audit-service · 5xx: now 42 (1 d ago 3 / 7 d ago 2)\n"
           "Ongoing since 26 Oct 06:05 UTC.\n"
           "The automatic investigation could not run; someone needs to look at this card.")


def job(client: TestClient):
    return client.app.state.auto_send


def seed(store: Store, sev: str = 'crit', **extra) -> Store:
    store.save(state(anomaly(KEY, sev=sev, **extra)), None)
    return store


def due_now(store: Store, key: str = KEY) -> None:
    store.anomalies.update_one({'_id': key}, {'$set': {'autoSend.at': iso(utc_now() - dt.timedelta(minutes=1))}})


def due_report(store: Store, report_id: str) -> None:
    store.deploy_reports.update_one({'_id': report_id}, {'$set': {'autoSend.at': iso(utc_now() - dt.timedelta(minutes=1))}})


def gap(auto: dict, draft: dict) -> dt.timedelta:
    return dt.datetime.fromisoformat(auto['at']) - dt.datetime.fromisoformat(draft['at'])


def drafted(client: TestClient, text: str = DRAFT) -> dict:
    return client.put('/api/slack-drafts', json={'key': KEY, 'text': text}, headers=AUTH).json()


def test_claudes_crit_draft_arms_at_once_and_the_job_posts_it_on_its_next_run(store: Store):
    # given
    seed(store, sev='crit')
    transport = FakeSlack()

    # when
    with client_with(store, transport) as client:
        saved = drafted(client)
        first = job(client).run_once()
        again = job(client).run_once()
        listed = client.get('/api/actions').json()

    # then
    assert gap(saved['autoSend'], saved['slackDraft']) == dt.timedelta(0)
    assert saved['autoSend']['draftAt'] == saved['slackDraft']['at']
    assert (first, again) == (1, 0)
    assert len(transport.calls) == 1
    payload = transport.calls[0][1]
    assert payload['text'].startswith('web-bff showed')
    assert [b for b in payload['blocks'] if b['type'] == 'context'][-1]['elements'][0]['text'].endswith(' \u00b7 Investigator \u00b7 auto-sent'), 'a draft by the core token reads as the investigator'
    assert [(a['type'], a['status'], a['requestedBy'], a['draft']) for a in listed] == [('slack', 'done', AUTO_BY, True)]
    assert store.anomaly(KEY)['slackPost']['by'] == AUTO_BY


def test_a_warn_draft_arms_and_information_takes_no_draft(store: Store):
    # given
    warn = seed(Store(store.db), sev='warn')

    # when
    with client_with(warn, FakeSlack()) as client:
        armed = drafted(client)
    warn.anomalies.update_one({'_id': KEY}, {'$set': {'sev': 'info'}, '$unset': {'slackDraft': '', 'autoSend': ''}})
    with client_with(warn, FakeSlack()) as client:
        info = client.put('/api/slack-drafts', json={'key': KEY, 'text': 'For information, no rush.'}, headers=AUTH)

    # then
    assert armed['autoSend']['draftAt'] == armed['slackDraft']['at']
    assert info.status_code == 409
    assert 'autoSend' not in warn.anomaly(KEY) and 'slackDraft' not in warn.anomaly(KEY)


def test_an_operators_edit_leaves_the_draft_manual(store: Store):
    # given
    seed(store)
    with client_with(store, FakeSlack()) as client:
        drafted(client)

    # when
    _, draft, auto = store.slack_draft(KEY, 'The operator wrote their own text.', 'oscar.green@example.com')

    # then
    assert auto is None
    assert draft['by'] == 'oscar.green@example.com'
    assert 'autoSend' not in store.anomaly(KEY)


def test_a_stopped_card_is_skipped_and_disarmed(store: Store):
    # given
    seed(store)
    transport = FakeSlack()

    # when
    with client_with(store, transport) as client:
        drafted(client)
        store.anomalies.update_one({'_id': KEY}, {'$set': {'triage': 'handled'}})
        due_now(store)
        sent = job(client).run_once()
        store.anomalies.update_one({'_id': KEY}, {'$unset': {'triage': ''}})
        later = job(client).run_once()

    # then
    assert (sent, later) == (0, 0)
    assert transport.calls == []
    assert 'autoSend' not in store.anomaly(KEY)
    assert store.anomaly(KEY)['slackDraft']['text'] == DRAFT


def test_a_muted_a_merged_and_a_cleared_info_card_are_skipped(store: Store):
    # given
    child = 'log|web-bff|WARN|child'
    store.save(state(anomaly(KEY, sev='crit'), anomaly('muted|x', sev='crit', muted='24 h'), anomaly(child, sev='crit', parent=KEY), anomaly('old|x', sev='info', status='cleared')), None)
    transport = FakeSlack()

    # when
    with client_with(store, transport) as client:
        for key in ('muted|x', child, 'old|x'):
            client.put('/api/slack-drafts', json={'key': key, 'text': DRAFT}, headers=AUTH)
        for key in ('muted|x', child):
            store.anomalies.update_one({'_id': key}, {'$set': {'autoSend.at': iso(utc_now() - dt.timedelta(minutes=1))}})
        sent = job(client).run_once()

    # then
    assert sent == 0
    assert transport.calls == []
    assert [k for k in ('muted|x', child) if 'autoSend' in store.anomaly(k)] == []
    assert store.anomaly('old|x').get('autoSend') is None


def test_a_draft_edited_after_it_was_armed_is_not_sent(store: Store):
    # given
    seed(store)
    transport = FakeSlack()

    # when
    with client_with(store, transport) as client:
        drafted(client)
        due_now(store)
        store.anomalies.update_one({'_id': KEY}, {'$set': {'slackDraft.text': OTHER, 'slackDraft.at': iso(utc_now())}})
        sent = job(client).run_once()

    # then
    assert sent == 0
    assert transport.calls == []
    assert 'autoSend' not in store.anomaly(KEY)


def test_a_draft_already_posted_is_not_sent_again(store: Store):
    # given
    seed(store)
    transport = FakeSlack()

    # when
    with client_with(store, transport) as client:
        client.put('/api/slack-drafts', json={'key': KEY, 'text': DRAFT}, headers=AUTH)
        client.post('/api/slack-posts', json={'key': KEY}, headers=AUTH)
        due_now(store)
        sent = job(client).run_once()

    # then
    assert sent == 0
    assert len(transport.calls) == 1
    assert store.anomaly(KEY)['slackPost']['by'] == 'token:bootstrap'


def test_only_one_replica_claims_a_due_draft(store: Store):
    # given
    seed(store)
    one, two = FakeSlack(), FakeSlack()

    # when
    with client_with(store, one) as pod_a, client_with(store, two) as pod_b:
        draft = drafted(pod_a)['slackDraft']
        due_now(store)
        now = iso(utc_now())
        first = store.claim_anomaly_auto_send(KEY, draft, now)
        second = store.claim_anomaly_auto_send(KEY, draft, now)
        store.anomalies.update_one({'_id': KEY}, {'$unset': {'autoSend.claimedAt': ''}})
        sent = [job(pod_a).run_once(), job(pod_b).run_once()]

    # then
    assert first is not None
    assert second is None
    assert sent == [1, 0]
    assert len(one.calls) == 1
    assert two.calls == []


def test_a_tick_that_read_before_the_send_cannot_wipe_the_post_or_the_claim(store: Store):
    # given
    seed(store)
    transport = FakeSlack()

    # when
    with client_with(store, transport) as client:
        drafted(client)
        stale = store.load()
        due_now(store)
        sent = job(client).run_once()
        store.save(stale, None)
        after = job(client).run_once()

    # then
    assert (sent, after) == (1, 0)
    assert len(transport.calls) == 1
    kept = store.anomaly(KEY)
    assert kept['slackPost']['by'] == AUTO_BY
    assert kept['autoSend']['claimedAt']


def test_a_slack_failure_retries_three_times_ten_minutes_apart_then_leaves_the_draft_manual(store: Store):
    # given
    seed(store)
    transport = FakeSlack('channel_not_found')

    # when
    with client_with(store, transport) as client:
        drafted(client)
        due_now(store)
        first = job(client).run_once()
        retry = store.anomaly(KEY)['autoSend']
        early = job(client).run_once()
        for _ in range(3):
            due_now(store)
            job(client).run_once()
        due_now(store)
        late = job(client).run_once()
        listed = client.get('/api/actions').json()

    # then
    assert (first, early, late) == (0, 0, 0)
    assert 'claimedAt' not in retry and retry['retries'] == 1 and parse_iso(retry['at']) - parse_iso(retry['failedAt']) == 600, 'the next try is 10 min later'
    assert len(transport.calls) == 4, 'the first send and three retries, then no more'
    assert all(a['result'] == 'not sent: channel_not_found' for a in listed if a['status'] == 'failed')
    auto = store.anomaly(KEY)['autoSend']
    assert auto['claimedAt'] and auto['failedAt'] and auto['retries'] == 3 and auto['error'] == 'channel_not_found'


@pytest.mark.parametrize('error, shown', [
    (PyMongoError('not authorized on db at mongodb://user:SENTINEL-77@db.example.com:27017'), msg('cards.send_failed', kind='PyMongoError')),
    (SlackDown('Slack did not answer (URLError)'), 'Slack did not answer (URLError)'),
    (NotFound('no card with key x'), 'no card with key x'),
], ids=['foreign', 'slack-down', 'own'])
def test_a_failed_auto_send_shows_skopax_own_text_or_a_fixed_text_never_a_foreign_one(store: Store, error, shown):
    # given
    seed(store)

    class Down(FakeSlack):
        def __call__(self, token: str, payload: dict) -> dict:
            raise error

    # when
    with client_with(store, Down()) as client:
        drafted(client)
        due_now(store)
        sent = job(client).run_once()

    # then
    assert sent == 0 and store.anomaly(KEY)['autoSend']['error'] == shown


def test_claudes_report_draft_always_arms_and_the_job_posts_it(store: Store):
    # given
    transport = FakeSlack()

    # when
    with client_with(store, transport) as client:
        saved = client.post('/api/deploy-reports', json=report(), headers=AUTH).json()
        armed = client.put(f"/api/deploy-reports/{saved['id']}/slack-draft", json={'text': REPORT_DRAFT}, headers=AUTH).json()
        first = job(client).run_once()
        listed = client.get('/api/deploy-reports').json()

    # then
    assert gap(armed['autoSend'], armed['slackDraft']) == dt.timedelta(0)
    assert first == 1
    assert transport.calls[0][1]['text'].startswith('OPS 15.10 release')
    assert listed[0]['slackPost']['by'] == AUTO_BY
    assert listed[0]['slackPost']['draft'] is True


@pytest.mark.parametrize('by, footer', [('server', 'Skopax \u00b7 auto-sent'), ('Claude (pod)', 'Investigator \u00b7 auto-sent'), ('token', 'Investigator \u00b7 auto-sent'),
                                        ('token:bootstrap', 'Investigator \u00b7 auto-sent'), ('token:release-bot', 'release-bot \u00b7 auto-sent')])
def test_a_report_draft_posts_with_the_footer_of_who_drafted_it(store: Store, by: str, footer: str):
    # given
    transport = FakeSlack()

    # when
    with client_with(store, transport) as client:
        saved = client.post('/api/deploy-reports', json=report(), headers=AUTH).json()
        store.report_slack_draft(saved['id'], REPORT_DRAFT, by)
        due_report(store, saved['id'])
        sent = job(client).run_once()

    # then
    assert sent == 1
    assert transport.calls[0][1]['attachments'][0]['blocks'][-2]['elements'][0]['text'].endswith(f'Sent from Skopax by {footer}')


def test_an_operators_report_edit_leaves_it_manual(store: Store):
    # given
    with client_with(store, FakeSlack()) as client:
        saved = client.post('/api/deploy-reports', json=report(), headers=AUTH).json()
        client.put(f"/api/deploy-reports/{saved['id']}/slack-draft", json={'text': REPORT_DRAFT}, headers=AUTH)

    # when
    edited = store.report_slack_draft(saved['id'], 'Operator text.', 'oscar.green@example.com')

    # then
    assert edited['autoSend'] is None
    assert 'autoSend' not in store.deploy_report(saved['id'])


def crit(key: str = FB_KEY, **extra) -> dict:
    return {**anomaly(key, sev='crit', svc='audit-service', label='5xx', ns='platform', now=42, d1=3, d7=2, unit='', firstSeen=FIRED), **extra}


def fired(store: Store, *cards: dict) -> Store:
    events = [{'t': c.get('firstSeen') or FIRED, 'key': c['key'], 'ev': 'new', 'sev': c['sev']} for c in cards]
    store.save({**state(*cards), 'history': events}, None)
    return store


def run(store: Store, outcome: str = 'failed', started: str = '2026-10-26T06:07:00Z', mode: str = 'live', trigger: str = 'cards', key: str = FB_KEY) -> None:
    store.add_investigation({'mode': mode, 'trigger': trigger, 'keys': [key], 'startedAt': started, 'outcome': outcome})


def drafted_keys(store: Store) -> list[str]:
    return [a['key'] for a in store.load()['anomalies'] if a.get('slackDraft')]


def test_a_critical_card_no_investigation_finished_on_gets_one_server_draft_that_posts_at_once(store: Store):
    # given
    fired(store, crit())
    run(store, outcome='failed')
    transport = FakeSlack()

    # when
    with client_with(store, transport) as client:
        first = client.app.state.slack_fallback.run_once(FB_NOW)
        again = client.app.state.slack_fallback.run_once(FB_NOW)
        armed = store.anomaly(FB_KEY)
        now = iso(utc_now())
        store.anomalies.update_one({'_id': FB_KEY}, {'$set': {'slackDraft.at': now, 'autoSend.draftAt': now, 'autoSend.at': now}})
        job(client).run_once()

    # then
    assert (first, again, len(transport.calls)) == (1, 0, 1)
    assert armed['slackDraft']['text'] == FB_TEXT and armed['slackDraft']['by'] == 'server'
    assert gap(armed['autoSend'], armed['slackDraft']) == dt.timedelta(0)
    assert armed['slackFallback']['firedAt'] == FIRED
    assert transport.calls[0][1]['text'].startswith('audit-service · 5xx: now 42')
    assert [b for b in transport.calls[0][1]['blocks'] if b['type'] == 'context'][-1]['elements'][0]['text'].endswith(' · Skopax · auto-sent')
    assert store.anomaly(FB_KEY)['slackPost']['by'] == AUTO_BY
    lines = [a['text'] for a in store.list_activity(None, 20) if a['who'] == 'server']
    assert lines == [f'Slack fallback post queued · {FB_KEY} · no finished investigation 16 min after it fired']


def test_the_fallback_waits_its_minutes_and_zero_turns_it_off(store: Store):
    # given
    fired(store, crit())

    # when
    early = Fallback(store, 15).run_once(parse_iso('2026-10-26T06:19:00Z'))
    off = Fallback(store, 0)
    stopped = off.run_once(FB_NOW)
    late = Fallback(store, 15).run_once(FB_NOW)

    # then
    assert (early, stopped, late) == (0, 0, 1)
    assert Fallback(store, 15).enabled and not off.enabled


@pytest.mark.parametrize('runs, drafted', [
    ([], 1),
    ([{'outcome': o} for o in ('failed', 'skipped', 'stopped', 'abandoned')], 1),
    ([{'outcome': 'done', 'mode': 'shadow'}], 1),
    ([{'outcome': 'done', 'trigger': 'slack'}, {'outcome': 'done', 'trigger': 'ask'}], 1),
    ([{'outcome': 'done', 'started': '2026-10-26T06:00:00Z'}], 1),
    ([{'outcome': 'done', 'key': TWIN_KEY}], 1),
    ([{'outcome': 'done'}], 0),
    ([{'outcome': 'done', 'trigger': 'action'}], 0),
    ([{'outcome': 'running'}], 0),
    ([{'outcome': 'running', 'trigger': 'action'}], 0),
])
def test_only_a_finished_or_running_live_card_investigation_since_the_card_fired_holds_the_fallback(store: Store, runs: list[dict], drafted: int):
    # given
    fired(store, crit())
    for r in runs:
        run(store, **r)

    # when
    count = Fallback(store, 15).run_once(FB_NOW)

    # then
    assert count == drafted
    assert drafted_keys(store) == [FB_KEY] * drafted


@pytest.mark.parametrize('extra, drafted', [
    ({'sev': 'warn'}, 0),
    ({'status': 'cleared'}, 0),
    ({'muted': '24 h'}, 0),
    ({'slackDraft': {'text': 'Operator draft.', 'by': 'op@example.com', 'at': FIRED}}, 1),
    ({'slackPost': {'at': FIRED, 'ts': '1.2', 'channel': 'C1', 'by': 'token'}}, 0),
    ({'autoSend': {'holdBy': 'op@example.com', 'holdAt': FIRED}}, 0),
    ({'triage': 'investigating'}, 0),
    ({'triage': 'progress'}, 0),
    ({'triage': 'handled'}, 0),
    ({'firstSeen': '2026-10-25T23:00:00Z'}, 0),
    ({'triage': 'dived'}, 1),
    ({'slackDraft': None, 'slackPost': None, 'autoSend': None, 'slackFallback': None, 'triage': None}, 1),
])
def test_only_an_open_unmuted_critical_card_with_no_draft_post_hold_or_owner_fired_in_the_last_6_h_gets_one(store: Store, extra: dict, drafted: int):
    # given
    fired(store, crit(**extra))

    # when
    count = Fallback(store, 15).run_once(FB_NOW)

    # then
    assert count == drafted
    assert len([k for k in drafted_keys(store) if store.anomaly(k)['slackDraft']['by'] == 'server']) == drafted


def test_the_draft_write_refuses_a_card_the_pod_claimed_after_the_fallback_read_it(store: Store):
    # given
    fired(store, crit())
    store.anomalies.update_one({'_id': FB_KEY}, {'$set': {'triage': 'investigating'}})

    # when
    saved = store.fallback_slack_draft(FB_KEY, FB_TEXT, 'server', FIRED, utc_now(), ('investigating', 'progress', 'handled'))

    # then
    assert saved is None
    assert 'slackDraft' not in store.anomaly(FB_KEY)


def test_a_merged_child_gets_no_fallback_and_its_open_parent_does(store: Store):
    # given
    child = crit('5xx|identity/api-gateway-admin', svc='api-gateway-admin', parent=FB_KEY)
    fired(store, crit(), child)

    # when
    count = Fallback(store, 15).run_once(FB_NOW)

    # then
    assert count == 1
    assert drafted_keys(store) == [FB_KEY]


@pytest.mark.parametrize('twin, drafted', [
    ({'slackPost': {'at': '2026-10-26T03:00:00Z', 'ts': '1.2', 'channel': 'C1', 'by': AUTO_BY}}, 0),
    ({'slackDraft': {'text': 'Pod post.', 'by': 'Claude (pod)', 'at': '2026-10-26T05:00:00Z'}, 'autoSend': {'at': '2026-10-26T05:00:00Z', 'draftAt': '2026-10-26T05:00:00Z', 'draftHash': 'h'}}, 0),
    ({'slackPost': {'at': '2026-10-25T23:00:00Z', 'ts': '1.2', 'channel': 'C1', 'by': AUTO_BY}}, 1),
    ({'sev': 'warn', 'slackPost': {'at': '2026-10-26T05:00:00Z', 'ts': '1.2', 'channel': 'C1', 'by': AUTO_BY}}, 1),
    ({'status': 'cleared', 'slackPost': {'at': '2026-10-26T05:00:00Z', 'ts': '1.2', 'channel': 'C1', 'by': AUTO_BY}}, 1),
])
def test_one_post_per_service_in_6_h_holds_back_the_fallback_unless_the_twin_is_older_lower_or_cleared(store: Store, twin: dict, drafted: int):
    # given
    fired(store, crit(), crit(TWIN_KEY, firstSeen='2026-10-26T02:50:00Z', **twin))

    # when
    count = Fallback(store, 15).run_once(FB_NOW)

    # then
    assert count == drafted
    assert [k for k in drafted_keys(store) if store.anomaly(k)['slackDraft']['by'] == 'server'] == [FB_KEY] * drafted


def test_a_twin_with_text_kept_for_planted_text_never_holds_back_the_service(store: Store):
    # given
    kept = {'text': 'Kept for a person.', 'by': 'Claude (pod)', 'at': '2026-10-26T05:00:00Z', 'notAutoSent': 'planted text in the esql answer (ignore previous)'}
    fired(store, crit(), crit(TWIN_KEY, firstSeen='2026-10-26T02:50:00Z', slackDraft=kept))

    # when
    count = Fallback(store, 15).run_once(FB_NOW)

    # then
    assert count == 1
    assert [k for k in drafted_keys(store) if store.anomaly(k)['slackDraft']['by'] == 'server'] == [TWIN_KEY], 'the older card gets the numbers post in place of the kept text'


def test_two_fresh_critical_cards_of_one_service_get_one_fallback_on_the_older_card(store: Store):
    # given
    fired(store, crit(), crit(TWIN_KEY, firstSeen='2026-10-26T06:04:00Z'))

    # when
    count = Fallback(store, 15).run_once(FB_NOW)

    # then
    assert count == 1
    assert drafted_keys(store) == [TWIN_KEY]


def test_a_discarded_fallback_draft_comes_back_only_when_the_card_fires_again(store: Store):
    # given
    fired(store, crit())
    fallback = Fallback(store, 15)
    first = fallback.run_once(FB_NOW)
    store.slack_draft(FB_KEY, '', 'op@example.com')

    # when
    same_fire = fallback.run_once(FB_NOW + 600)
    current = store.load()
    current['history'].insert(0, {'t': '2026-10-26T06:40:00Z', 'key': FB_KEY, 'ev': 'escalated', 'sev': 'crit'})
    store.save(current, None)
    new_fire = fallback.run_once(parse_iso('2026-10-26T06:56:00Z'))

    # then
    assert (first, same_fire, new_fire) == (1, 0, 1)
    assert store.anomaly(FB_KEY)['slackFallback']['firedAt'] == '2026-10-26T06:40:00Z'


def test_the_fallback_text_carries_the_unit_and_no_mention_or_line_break_from_the_signal():
    # given
    ms = crit(label='p95', sub='<@U0123ABCD> GET /x\nnext line', unit='ms', now=1840, d1=420, d7=None)
    dur = crit(label='argo', unit='dur', now=1500, d1=None, d7=None, firstSeen=None)
    pct = crit(label='fail', unit='%', now=12.5, d1=0.4, d7=0)

    # when
    lines = [fallback_text(a, FIRED).splitlines() for a in (ms, dur, pct)]

    # then
    assert lines[0][0] == 'audit-service · p95 · ‹@U0123ABCD› GET /x next line: now 1.8 s (1 d ago 420 ms)'
    assert lines[1][0] == 'audit-service · argo: now 25 min'
    assert lines[1][1] == "Ongoing since 26 Oct 06:05 UTC."
    assert lines[2][0] == 'audit-service · fail: now 12.5% (1 d ago 0.40% / 7 d ago 0.0%)'
    assert all(len(x) == 3 and '<@' not in '\n'.join(x) and 'skopax.example.com' not in '\n'.join(x) for x in lines)


def test_the_server_runs_the_fallback_job_only_with_slack_and_a_positive_fallback_minutes(store: Store):
    # given
    off = dataclasses.replace(settings(), slack_fallback_minutes=0)

    # when
    with client_with(store, FakeSlack()) as on:
        with_slack = [j.name for j in on.app.state.scheduler.jobs]
    with TestClient(create_app(settings(), store)) as bare:
        without_slack = [j.name for j in bare.app.state.scheduler.jobs]
    with TestClient(create_app(off, store, slack=Slack('xoxb-test', '#ops', FakeSlack()))) as zero:
        zero_minutes = [j.name for j in zero.app.state.scheduler.jobs]

    # then
    assert 'slack-fallback' in with_slack
    assert 'slack-fallback' not in without_slack and 'slack-fallback' not in zero_minutes


def crit_card(key: str, **extra) -> dict:
    return anomaly(key, sev='crit', **({'pushedAt': '2026-11-12T05:17:11Z'} | extra))


def test_a_critical_card_is_posted_at_once_with_its_numbers_and_the_investigation_line(store: Store):
    # given
    store.save(state(crit_card(KEY), crit_card('log|a|ERROR|muted', svc='a', muted=True), crit_card('log|b|ERROR|taken', svc='b', triage='progress'),
                     anomaly('log|c|WARN|warn', svc='c', sev='warn'), crit_card('log|d|ERROR|posted', svc='d', slackPost={'at': '2026-11-12T05:00:00Z', 'ts': '1.0', 'channel': 'C1'})), None)
    sent = []
    now = parse_iso('2026-11-12T05:17:20Z')

    # when
    cards = store.load()['anomalies']
    posted = PostNow(store, sent.append, lambda: 'crit').run(cards, cards, now)
    again = PostNow(store, sent.append, lambda: 'crit').run(cards, store.load()['anomalies'], now)

    # then
    assert posted == [KEY] and sent == [KEY], 'only the open, unmuted, untaken, unposted critical card'
    draft = store.anomaly(KEY)['slackDraft']
    assert draft['by'] == 'server' and draft['text'].endswith(msg('autosend.crit_now.last'))
    assert again == [], 'a card with its numbers already drafted is not posted twice'
    assert any('posted to Slack at once' in r['text'] for r in store.list_activity(None, 10))


def test_many_critical_cards_at_once_are_left_to_the_investigator_and_the_switch_turns_it_off(store: Store):
    # given
    keys = [f'restarts|ns{i}/svc{i}' for i in range(4)]
    store.save(state(*[crit_card(k, svc=f'svc{i}', ns=f'ns{i}') for i, k in enumerate(keys)]), None)
    cards = store.load()['anomalies']
    sent = []

    # when
    burst = PostNow(store, sent.append, lambda: 'crit').run(cards, cards, parse_iso('2026-11-12T05:17:20Z'))
    off = PostNow(store, sent.append, lambda: 'off').run(cards[:1], cards, parse_iso('2026-11-12T05:17:20Z'))

    # then
    assert (burst, off, sent) == ([], [], []), 'four critical cards in one pass look like a release or a cluster event; the investigator groups them first'
    assert any('4 cards opened together (4 critical)' in r['text'] for r in store.list_activity(None, 10))


AT_ONCE = parse_iso('2026-11-12T05:17:20Z')


def sev_card(key: str, sev: str, **extra) -> dict:
    return anomaly(key, sev=sev, svc=key.split('|')[1], **({'pushedAt': '2026-11-12T05:17:11Z'} | extra))


@pytest.mark.parametrize('floor, expected', [
    ('crit', ['log|a|ERROR|x']),
    ('warn', ['log|a|ERROR|x', 'log|b|WARN|x']),
    ('info', ['log|a|ERROR|x', 'log|b|WARN|x']),
    ('off', []),
])
def test_cards_at_or_above_the_chosen_severity_post_at_once(store: Store, floor, expected):
    # given
    store.save(state(sev_card('log|a|ERROR|x', 'crit'), sev_card('log|b|WARN|x', 'warn'), sev_card('log|c|INFO|x', 'info')), None)
    cards = store.load()['anomalies']
    sent = []

    # when
    posted = PostNow(store, sent.append, lambda: floor).run(cards, cards, AT_ONCE)

    # then
    assert posted == expected and sent == expected
    assert all(store.anomaly(k)['slackDraft']['by'] == 'server' for k in expected)
    assert all(not store.anomaly(k).get('slackDraft') for k in {c['key'] for c in cards} - set(expected)), 'cards below the line wait for the investigator or a person'


@pytest.mark.parametrize('investigating, crit_line, warn_line', [
    (True, 'autosend.crit_now.last', 'autosend.post_now.warn_last'),
    (False, None, None),
])
def test_each_post_at_once_ends_with_a_line_that_is_true_for_its_severity(store: Store, investigating, crit_line, warn_line):
    # given
    store.save(state(sev_card('log|a|ERROR|x', 'crit'), sev_card('log|b|WARN|x', 'warn'), sev_card('log|c|INFO|x', 'info')), None)
    cards = store.load()['anomalies']

    # when
    PostNow(store, lambda key: None, lambda: 'info', lambda: investigating).run(cards, cards, AT_ONCE)
    text = {k: store.anomaly(k)['slackDraft']['text'] for k in ('log|a|ERROR|x', 'log|b|WARN|x')}

    # then
    lines = {msg('autosend.crit_now.last'), msg('autosend.post_now.warn_last')}
    for key, line in (('log|a|ERROR|x', crit_line), ('log|b|WARN|x', warn_line)):
        if line:
            assert text[key].endswith('\n' + msg(line))
        else:
            assert not any(x in text[key] for x in lines) and text[key].endswith('.'), 'no investigation is promised while the investigator does not run on cards'
    assert not store.anomaly('log|c|INFO|x').get('slackDraft'), 'information is never posted'
    assert msg('autosend.post_now.warn_last') == 'The investigator looks at this card on its next run; findings will arrive in this thread.'
    assert any(r['text'] == 'Card posted to Slack at once with its numbers \u00b7 log|b|WARN|x' for r in store.list_activity(None, 10))


def test_an_info_only_burst_posts_nothing_and_says_nothing(store: Store):
    # given
    keys = [f'log|s{i}|INFO|x' for i in range(4)]
    store.save(state(*[sev_card(k, 'info') for k in keys]), None)
    cards = store.load()['anomalies']

    # when
    burst = PostNow(store, lambda key: None, lambda: 'info').run(cards, cards, AT_ONCE)

    # then
    assert burst == []
    assert [r['text'] for r in store.list_activity(None, 10) if 'opened together' in r['text']] == []


def test_the_burst_counts_only_the_cards_due_and_names_their_severities(store: Store):
    # given
    keys = ['log|a|ERROR|1', 'log|b|ERROR|2', 'log|c|WARN|3', 'log|d|WARN|4']
    store.save(state(*[sev_card(k, 'crit' if 'ERROR' in k else 'warn') for k in keys]), None)
    cards = store.load()['anomalies']
    sent = []

    # when
    burst = PostNow(store, sent.append, lambda: 'warn').run(cards, cards, AT_ONCE)
    crit_only = PostNow(store, sent.append, lambda: 'crit').run(cards, store.load()['anomalies'], AT_ONCE)

    # then
    assert burst == [], 'four cards due in one pass look like a release or a cluster event'
    assert any(r['text'] == '4 cards opened together (2 critical, 2 warning); not posted at once, the investigator groups them first' for r in store.list_activity(None, 10))
    assert crit_only == ['log|a|ERROR|1', 'log|b|ERROR|2'] and sent == crit_only, 'with Critical only two are due, under the burst'


def test_a_card_posted_at_once_gets_no_fallback_draft_even_when_the_post_failed(store: Store):
    # given
    store.save(state(sev_card('log|a|ERROR|x', 'crit', firstSeen=FIRED), sev_card('log|b|WARN|x', 'warn', firstSeen=FIRED)), None)
    store.anomalies.update_many({}, {'$set': {'pushedAt': FIRED}})
    loaded = store.load()
    loaded['history'] = [{'t': FIRED, 'key': k, 'ev': 'new'} for k in ('log|a|ERROR|x', 'log|b|WARN|x')]
    cards = loaded['anomalies']

    def down(key):
        raise SlackDown('down')

    # when
    before = [a['key'] for a, _ in Fallback(store, 15).due(loaded, [], FB_NOW)]
    posted = PostNow(store, down, lambda: 'warn').run(cards, cards, AT_ONCE)
    after = store.load()
    after['history'] = loaded['history']
    picked = Fallback(store, 15).due(after, [], FB_NOW)

    # then
    assert before == ['log|a|ERROR|x'], 'without the post at once the fallback would draft the critical card'
    assert posted == [], 'Slack was down, the armed draft retries through auto-send'
    assert picked == [], 'the numbers are drafted once; the fallback does not draft them again'
    assert {store.anomaly(k)['slackDraft']['by'] for k in ('log|a|ERROR|x', 'log|b|WARN|x')} == {'server'}


def test_fresh_cards_are_the_pushed_ones_after_the_first_pass_and_never_information():
    # given
    now = parse_iso('2026-11-12T05:17:20Z')
    crit = anomaly('log|a|ERROR|x', sev='crit', svc='a')
    info, old_info = anomaly('log|c|INFO|x', sev='info', svc='c'), anomaly('log|d|INFO|x', sev='info', svc='d')
    events = [{'key': 'log|a|ERROR|x', 'ev': 'new'}, {'key': 'log|c|INFO|x', 'ev': 'new'}, {'key': 'log|d|INFO|x', 'ev': 'reopened'}]
    cards = [crit, info, old_info]

    # when
    later = fresh(Applied(rev=2, events=events, push=[crit], cards=cards), now)
    first = fresh(Applied(rev=1, events=events, push=[], cards=cards, first=True), now)

    # then
    assert [a['key'] for a in later] == ['log|a|ERROR|x']
    assert first == [], 'the first pass after a fresh start posts nothing'


@pytest.mark.parametrize('mode, investigated', [('live', True), ('shadow', False)])
def test_the_app_promises_an_investigation_only_while_the_investigator_runs_live(store: Store, mode, investigated):
    # given
    from tests.fake_anthropic import FakeAnthropic
    from tests.test_engine import item
    from tests.test_slack import FakeSlackApi
    from skopax.engine import PassResult
    import time
    s = dataclasses.replace(settings(), anthropic_api_key='sk-test', investigate_mode=mode)
    store.save_state({'anomalies': [], 'initialized': True}, None)
    found = [item('cf-health|shop', sev='crit'), item('cf-health|cart', sev='warn')]

    # when
    with TestClient(create_app(s, store, slack=Slack('xoxb-test', '#ops', FakeSlack(), FakeSlackApi()), anthropic_client=FakeAnthropic([]))) as c:
        c.app.state.server_apply([PassResult('cf', True, 'ok', found, {}, ('cf',))], time.time(), None)
        texts = {a['key']: (a.get('slackDraft') or {}).get('text', '') for a in store.load()['anomalies']}

    # then
    crit, warn = texts['cf-health|shop'], texts['cf-health|cart']
    assert 'Ongoing since' in crit and 'Ongoing since' in warn, 'both post at once with their numbers'
    assert crit.endswith(msg('autosend.crit_now.last')) is investigated
    assert warn.endswith(msg('autosend.post_now.warn_last')) is investigated
    if not investigated:
        assert crit.endswith('.') and msg('autosend.crit_now.last') not in crit and msg('autosend.post_now.warn_last') not in warn
