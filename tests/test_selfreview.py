import dataclasses
import datetime as dt
import functools
import json
import time

import pytest
import regex
from fastapi.testclient import TestClient

from tests.conftest import AUTH, settings
from tests.fake_anthropic import FakeAnthropic, reply, text, usage
from tests.test_investigator_tools import box
from tests.test_slack import FakeSlack, FakeSlackApi
from skopax import clock, redact, selfreview, toolusage
from skopax.app import create_app
from skopax.config import load_settings
from skopax.engine import iso
from skopax.investigator.tools import READ_NAMES, RUN_CALLS
from skopax.kube import Kube
from skopax.messages import msg
from skopax.selfreview import BY, SYSTEM, SelfReview, gather as gathered, log_query, review_offset, review_slot_offset, runs_facts, local_time, tidy
from skopax.slack import Slack
from skopax.store import Store
from skopax.tunables import default_of, registry

NOW = 1_794_207_600.0

gather = functools.partial(gathered, service='skopax', log_index='logs-platform-*')


class FakeLogs:
    enabled = True

    def __init__(self, rows=None, fail: str | None = None):
        self.rows = rows or []
        self.fail = fail
        self.asked: list[str] = []

    def query(self, esql: str) -> list[dict]:
        self.asked.append(esql)
        if self.fail:
            raise RuntimeError(self.fail)
        return self.rows


def seed(store: Store) -> None:
    store.save({'initialized': True, 'muted': [], 'anomalies': [
        {'key': 'alert|KubePodNotReady|cluster=east-data', 'src': 'alert', 'sev': 'warn', 'status': 'open', 'recurringCount': 6, 'firstSeen': iso(NOW - 3600)},
        {'key': 'unavailable|platform/metrics-service', 'src': 'kube', 'sev': 'warn', 'status': 'cleared', 'firstSeen': iso(NOW - 7200)},
        {'key': 'page-typos|shop.example.com/products', 'src': 'pagescan', 'sev': 'warn', 'status': 'open', 'firstSeen': iso(NOW - 7200),
         'slackPost': {'channel': 'C1', 'ts': '1', 'at': iso(NOW - 600), 'by': 'oscar.green@example.com'}},
        {'key': 'log|orders-service|ERROR|x', 'src': 'logs', 'sev': 'warn', 'status': 'open', 'recurringCount': 3, 'triage': 'progress', 'ticket': 'OPS-1', 'firstSeen': iso(NOW - 600)}]}, None)
    store.investigations.insert_many([
        {'_id': 'r1', 'trigger': 'cards', 'startedAt': iso(NOW - 3000), 'outcome': 'done', 'usd': 0.5, 'keys': ['alert|KubePodNotReady|cluster=east-data'],
         'toolCalls': [{'name': 'esql', 'error': 'ES|QL HTTP 400: verification_exception: Unknown column [log]'}, {'name': 'kube'}]},
        {'_id': 'r2', 'trigger': 'slack', 'startedAt': iso(NOW - 2000), 'outcome': 'done', 'usd': 0.25, 'keys': ['alert|KubePodNotReady|cluster=east-data'],
         'toolCalls': [{'name': 'esql'}] * 10, 'ask': {'login': 'someone@example.com'}},
        {'_id': 'old', 'trigger': 'cards', 'startedAt': iso(NOW - 3 * 86400), 'outcome': 'failed', 'usd': 9.0, 'keys': [], 'toolCalls': []}])
    store.slack_seen.insert_one({'_id': 's1', 'at': iso(NOW - 100), 'decision': 'no-access', 'user': 'U123', 'login': 'someone@example.com'})
    store.add_self_review({'at': iso(NOW - 86400), 'by': 'token', 'text': 'Healthy day.\n- esql reserved words → hint. Code.', 'dm': ['oscar.green@example.com']})


def test_the_facts_cover_the_last_day_with_numbers_and_no_person(store: Store):
    # given
    seed(store)
    info = {'passes': {'cloudflare': {'status': 'ok'}, 'umami': {'status': 'error: HTTP 502'}},
            'investigate': {'spentToday': 3.5, 'cap': 20.0, 'mode': 'live', 'maxToolCalls': 30, 'slackMaxToolCalls': 10}}
    logs = FakeLogs([{'level': 'WARN', 'n': 4, 'msg': 'page scan: no typo screenshot of https://shop.example.com/x', 'oldest': '2026-11-08T12:00:00Z', 'newest': '2026-11-08T12:06:00Z'}])

    # when
    facts = gather(store, info, logs, NOW)

    # then
    assert facts['passesNotOk'] == {'umami': 'error: HTTP 502'} and facts['spend'] == {'today': 3.5, 'cap': 20.0, 'mode': 'live', 'stopped': False}
    runs = facts['runs']
    assert (runs['count'], runs['usd'], runs['atToolCap'], runs['triggers']) == (2, 0.75, 1, {'cards': 1, 'slack': 1})
    assert runs['toolErrors'] == {'esql': {'count': 1, 'samples': ['ES|QL HTTP 400: verification_exception: Unknown column [log]'], 'last': local_time(iso(NOW - 3000)), 'runs': {'cards': 1}}}
    assert runs['cardsRunMoreThanOnce'] == [{'key': 'alert|KubePodNotReady|cluster=east-data', 'runs': 2, 'now': 'open'}]
    cards = facts['cards']
    assert cards['stoppedNotAddressed'] == ['unavailable|platform/metrics-service']
    assert cards['recurringWithoutTicket'] == [{'key': 'alert|KubePodNotReady|cluster=east-data', 'times': 6}]
    assert cards['openAlertsNotGrouped'] == ['alert|KubePodNotReady|cluster=east-data']
    assert cards['slackPosts'] == {'cards': 1, 'actedOn': 0, 'bySource': {'pagescan': 1}}
    assert facts['slackIntake'] == {'no-access': 1}
    assert facts['ownLogs'] == [{'level': 'WARN', 'count': 4, 'first': '2026-11-08T12:00:00Z', 'last': '2026-11-08T12:06:00Z', 'message': 'page scan: no typo screenshot of https://shop.example.com/x'}]
    assert 'oldest = MIN(@timestamp), newest = MAX(@timestamp)' in logs.asked[0] and ' last ' not in logs.asked[0]
    assert facts['previousReviews'][0]['text'].startswith('Healthy day.')
    dumped = json.dumps(facts)
    assert 'someone@example.com' not in dumped and 'U123' not in dumped and 'oscar.green@example.com' not in dumped
    assert '`service.name` == "skopax"' in logs.asked[0] and 'FROM logs-platform-* | WHERE' in logs.asked[0]


def test_every_allowed_ok_form_counts_as_a_passed_pass(store: Store):
    # given
    ok = {'prom': 'ok', 'capacity': 'ok \u00b7 no data', 'kafka': 'ok \u00b7 no data for groups, app', 'grafana': 'ok \u00b7 read 12 of 14 datasources',
          'argo': 'ok \u00b7 read 1000 applications, more not read', 'redis': 'ok \u00b7 no access to replication', 'kafka-east': 'ok \u00b7 broker disk needs the strimzi preset'}
    bad = {'umami': 'error: HTTP 502', 'okta': 'okay', 'pmm': None, 'cf': ''}
    info = {'passes': {k: {'status': v} for k, v in (ok | bad).items()}}

    # when
    facts = gather(store, info, None, NOW)

    # then
    assert facts['passesNotOk'] == bad


def test_refused_reads_are_a_named_fact_and_an_instance_tail_is_not_a_mention(store: Store):
    # given
    store.save({'initialized': True, 'muted': [], 'anomalies': [
        {'key': 'no-access|-/prom|metrics', 'src': 'prom', 'sig': 'no-access', 'ranKey': 'prom:access:metrics', 'sev': 'warn', 'status': 'open', 'label': 'Prometheus cannot read metrics: the token lacks the needed permission', 'firstSeen': iso(NOW - 600)},
        {'key': 'cf-access|health-checks', 'src': 'cf', 'sig': 'cf-access', 'ranKey': 'cf:access:health-checks', 'sev': 'warn', 'status': 'open', 'label': 'Cloudflare cannot read health checks: the token lacks Health Checks Read', 'firstSeen': iso(NOW - 600)},
        {'key': 'no-access|-/kafka|groups@east', 'src': 'kafka', 'sig': 'no-access', 'ranKey': 'kafka:access:groups-east', 'sev': 'warn', 'status': 'open', 'cluster': 'east', 'label': 'Kafka cannot read groups: the token lacks the needed permission', 'firstSeen': iso(NOW - 600)},
        {'key': 'no-access|-/redis|app', 'src': 'redis', 'sig': 'no-access', 'ranKey': 'redis:access:app', 'sev': 'warn', 'status': 'cleared', 'label': 'Redis cannot read app: the token lacks the needed permission', 'firstSeen': iso(NOW - 600)},
        {'key': 'err|shop/checkout-service', 'src': 'prom', 'sig': 'err', 'sev': 'warn', 'status': 'open', 'firstSeen': iso(NOW - 600)}]}, None)

    # when
    facts = gather(store, {'passes': {}}, None, NOW)
    said = tidy('err|shop/checkout-service@east is open; ping @alice')

    # then
    assert facts['cards']['noAccess'] == [{'label': 'Prometheus cannot read metrics: the token lacks the needed permission', 'instance': None},
                                         {'label': 'Cloudflare cannot read health checks: the token lacks Health Checks Read', 'instance': None},
                                         {'label': 'Kafka cannot read groups: the token lacks the needed permission', 'instance': 'east'}]
    assert said == 'err|shop/checkout-service@east is open; ping'


def test_a_logs_failure_is_a_fact_not_a_crash(store: Store):
    # given
    seed(store)

    # when
    facts = gather(store, {}, FakeLogs(fail='ES|QL HTTP 504 at https://user:SENTINEL-77@es.example.com'), NOW)

    # then
    assert facts['ownLogs'] == {'error': msg('selfreview.logs_unread', kind='RuntimeError')}
    assert 'SENTINEL' not in json.dumps(facts)


def test_the_review_is_written_by_claude_from_the_facts_saved_with_its_spend_and_mentions_removed(store: Store):
    # given
    seed(store)
    client = FakeAnthropic([], [reply(text('Mostly healthy: 2 runs for $0.75. <@U123>\n- esql Unknown column [log] x1 → hint. Code.'), stop='end_turn', u=usage(6000, 300))])
    saved = []
    s = dataclasses.replace(settings(), anthropic_api_key='sk-test')
    review = SelfReview(store, s, lambda now: gather(store, {}, None, now), lambda t: saved.append(t) or {'review': {'text': t}}, client=client)

    # when
    out = review.run(NOW)

    # then
    assert saved == ['Mostly healthy: 2 runs for $0.75. \n- esql Unknown column [log] x1 → hint. Code.']
    req = client.gate_requests[0]
    assert req['model'] == 'claude-sonnet-5-5' and 'Still open' in req['system'] and req['messages'][0]['content'].startswith('Facts (JSON):\n{')
    assert out['usd'] > 0 and store.spent(clock.day(NOW)) == out['usd']
    assert [r['source'] for r in store.ledger_since(clock.day(NOW))] == ['selfreview']


def test_a_spent_cap_skips_the_review_without_a_call(store: Store):
    # given
    store.add_spend(clock.day(NOW), 25.0)
    client = FakeAnthropic([], [])
    review = SelfReview(store, dataclasses.replace(settings(), anthropic_api_key='sk-test'), lambda now: {}, lambda t: {}, client=client)

    # when
    out = review.run(NOW)

    # then
    assert out is None and client.gate_requests == []


def test_the_app_runs_it_daily_at_the_local_hour_and_on_request_and_dms_the_push_users_by_default(store: Store):
    # given
    seed(store)
    t = FakeSlack()
    client = FakeAnthropic([], [reply(text('Healthy day: every pass ok.\n- nothing to fix → none. No code.'), stop='end_turn')])
    s = dataclasses.replace(settings(), anthropic_api_key='sk-test', slack_channel='C1', slack_push_user='U9', self_review_enabled=True)

    # when
    with TestClient(create_app(s, store, kube=Kube(root='/nonexistent'), slack=Slack('xoxb-test', 'C1', t, FakeSlackApi()), anthropic_client=client)) as c:
        jobs = {j.name: j for j in c.app.state.scheduler.jobs}
        res = c.post('/api/self-review/run', headers=AUTH)
        anonymous = c.post('/api/self-review/run')
        for _ in range(50):
            if store.self_reviews() and store.self_reviews()[0]['by'] == BY:
                break
            time.sleep(0.05)

    # then
    assert (res.status_code, res.json(), anonymous.status_code) == (202, {'started': True}, 401)
    assert jobs['self-review'].offset_at(NOW) == review_slot_offset(9, NOW) == 9 * 3600 and review_offset(9) == 9 * 3600
    assert (jobs['self-review'].background, jobs['self-review'].run.__name__) == (True, 'scheduled')
    latest = store.self_reviews()[0]
    assert (latest['by'], latest['text'], latest['dm']) == (BY, 'Healthy day: every pass ok.\n- nothing to fix → none. No code.', ['U9'])
    [dm] = [m for _, m in t.calls if m.get('channel') == 'U9']
    assert dm['text'].startswith('*Skopax self-review · ')


@pytest.mark.parametrize('push_on, named, told, channels', [(True, '', ['U9'], ['U9']), (False, '', [], []), (False, 'alice@example.com', ['alice@example.com'], ['U5'])],
                         ids=['push-users-while-push-is-on', 'nobody-while-push-is-off', 'a-named-reviewer-whatever-push-says'])
def test_the_self_review_falls_back_to_the_push_users_only_while_phone_push_is_on(store: Store, push_on, named, told, channels):
    # given
    seed(store)
    t = FakeSlack()
    client = FakeAnthropic([], [reply(text('Healthy day.'), stop='end_turn')])
    s = dataclasses.replace(settings(), anthropic_api_key='sk-test', slack_channel='C1', slack_push_user='U9', self_review_enabled=True,
                            push_on=push_on, selfreview_dm=named)

    # when
    with TestClient(create_app(s, store, kube=Kube(root='/nonexistent'), slack=Slack('xoxb-test', 'C1', t, FakeSlackApi(emails={'U5': 'alice@example.com'})), anthropic_client=client)) as c:
        c.post('/api/self-review/run', headers=AUTH)
        for _ in range(50):
            if store.self_reviews() and store.self_reviews()[0]['by'] == BY:
                break
            time.sleep(0.05)

    # then
    assert store.self_reviews()[0]['dm'] == told
    assert [m['channel'] for _, m in t.calls if str(m.get('channel', '')).startswith('U')] == channels


def test_the_scheduled_review_is_skipped_once_claude_saved_one_today_so_a_restart_never_dms_twice(store: Store):
    # given
    client = FakeAnthropic([], [reply(text('Healthy day.'), stop='end_turn')])
    save = lambda t: store.add_self_review({'at': iso(NOW), 'by': BY, 'text': t, 'dm': ['U9']}) or {}
    review = SelfReview(store, dataclasses.replace(settings(), anthropic_api_key='sk-test', self_review_enabled=True), lambda now: {}, save, client=client, clock=lambda: NOW)
    store.add_self_review({'at': iso(NOW - 86400), 'by': BY, 'text': 'yesterday', 'dm': ['U9']})
    store.add_self_review({'at': iso(NOW - 600), 'by': 'token', 'text': 'pasted', 'dm': []})

    # when
    first = review.scheduled(NOW)
    again = review.scheduled(NOW + 300)

    # then
    assert (first, again) == (True, False)
    assert len(client.gate_requests) == 1
    assert [r['text'] for r in store.self_reviews()] == ['Healthy day.', 'pasted', 'yesterday']


def test_self_review_runs_only_when_enabled(store: Store):
    # given
    seed(store)
    t = FakeSlack()
    client = FakeAnthropic([], [reply(text('Healthy day.'), stop='end_turn')])
    s = dataclasses.replace(settings(), anthropic_api_key='sk-test', slack_channel='C1', slack_push_user='U9')

    # when
    with TestClient(create_app(s, store, kube=Kube(root='/nonexistent'), slack=Slack('xoxb-test', 'C1', t, FakeSlackApi()), anthropic_client=client)) as c:
        job = {j.name: j for j in c.app.state.scheduler.jobs}['self-review']
        off = job.run(NOW)
        refused = c.post('/api/self-review/run', headers=AUTH)
        calls_while_off, dms_while_off = list(client.gate_requests), [m for _, m in t.calls if m.get('channel') == 'U9']
        switched = c.put('/api/admin/settings', json={'key': 'self-review.enabled', 'value': 'true'}, headers=AUTH)
        on = job.run(NOW)

    # then
    assert (off, calls_while_off, dms_while_off) == (False, [], [])
    assert (refused.status_code, refused.json()['detail']) == (409, msg('features.off', feature='Self-review'))
    assert switched.status_code == 200 and switched.json()['item']['live'] is True and switched.json()['item']['value'] == 'true'
    assert on is True and len(client.gate_requests) == 1
    assert [r['by'] for r in store.self_reviews()][0] == BY
    assert [m['text'].startswith('*Skopax self-review') for _, m in t.calls if m.get('channel') == 'U9'] == [True]


def test_the_self_review_switch_is_a_live_feature_that_starts_off():
    # given
    switch = {t.key: t for t in registry()}['self-review.enabled']

    # then
    assert (switch.field, switch.kind, switch.live, switch.group) == ('self_review_enabled', 'bool', True, 'Features')
    assert default_of(switch) == 'false' and 'next day' in switch.help and 'Run now works at once' in switch.help
    assert load_settings(env={}).self_review_enabled is False
    assert load_settings(env={'SKOPAX_SELF_REVIEW': 'true'}).self_review_enabled is True


class Watching(FakeSlack):
    def __init__(self, store: Store):
        super().__init__()
        self.store = store
        self.stored_at_dm: list[list[str]] = []

    def __call__(self, token: str, payload: dict) -> dict:
        if payload.get('channel') == 'U9':
            self.stored_at_dm.append([r['by'] for r in self.store.self_reviews()])
        return super().__call__(token, payload)


def test_the_review_is_stored_before_the_dm_goes_out_and_then_carries_who_got_it(store: Store):
    # given
    t = Watching(store)
    client = FakeAnthropic([], [reply(text('Healthy day.'), stop='end_turn')])
    s = dataclasses.replace(settings(), anthropic_api_key='sk-test', slack_channel='C1', slack_push_user='U9', self_review_enabled=True)

    # when
    with TestClient(create_app(s, store, kube=Kube(root='/nonexistent'), slack=Slack('xoxb-test', 'C1', t, FakeSlackApi()), anthropic_client=client)) as c:
        c.post('/api/self-review/run', headers=AUTH)
        for _ in range(50):
            if store.self_reviews() and store.self_reviews()[0].get('dm'):
                break
            time.sleep(0.05)

    # then
    assert t.stored_at_dm == [[BY]]
    assert (store.self_reviews()[0]['by'], store.self_reviews()[0]['dm']) == (BY, ['U9'])


def test_the_log_query_reads_the_configured_index_for_the_configured_service():
    # then
    q = log_query('logs-platform-*', 'skopax')
    assert q.startswith('FROM logs-platform-* | WHERE `service.name` == "skopax"')
    assert 'EVAL level = TO_UPPER(`log.level`), msg = SUBSTRING(`message`, 1, 140)' in q
    assert log_query('logs-*', 'skopax', {'service': 'app.name', 'level': 'severity', 'message': 'line'}).startswith('FROM logs-* | WHERE `app.name` == "skopax"')
    assert runs_facts([], 30, 10)['count'] == 0


def test_the_investigator_reads_the_reviews_and_the_live_facts_through_its_self_review_tool(store: Store):
    # given
    seed(store)
    store.add_self_review({'at': iso(NOW - 600), 'by': BY, 'text': 'Mostly healthy.', 'dm': ['U9']})
    asked = []
    on = box(store, selfreview=lambda now: asked.append(now) or {'runs': {'count': 2}, 'previousReviews': ['dropped']})
    off = box(store)

    # when
    plain, e1 = on.call('self_review', {})
    two, e2 = on.call('self_review', {'limit': 2, 'facts': True})
    missing, e3 = off.call('self_review', {'facts': True})

    # then
    assert 'self_review' in READ_NAMES and RUN_CALLS['self_review'] == 2 and (e1, e2, e3) == (False, False, True)
    assert json.loads(plain) == {'reviews': [{'at': iso(NOW - 600), 'by': BY, 'text': 'Mostly healthy.'}]}
    data = json.loads(two)
    assert [r['text'][:7] for r in data['reviews']] == ['Mostly ', 'Healthy'] and data['facts'] == {'runs': {'count': 2}} and len(asked) == 1
    assert 'U9' not in two and 'oscar' not in two and 'not available here' in missing


def test_the_services_own_deploys_since_the_last_review_are_facts_so_fixed_items_are_not_still_open(store: Store):
    # given
    seed(store)
    by = lambda title: {'title': title, 'commits': 1}
    store.add_deploys([
        {'ns': 'platform', 'svc': 'skopax', 'toImage': 'facade01', 'fromImage': 'beaded01', 'at': iso(NOW - 3000), 'imageBy': by('OPS-123 daily cart summary email')},
        {'ns': 'reports', 'svc': 'skopax', 'toImage': 'facade01', 'fromImage': 'beaded01', 'at': iso(NOW - 2900), 'imageBy': by('OPS-123 daily cart summary email')},
        {'ns': 'platform', 'svc': 'skopax', 'toImage': 'old00000', 'fromImage': 'x', 'at': iso(NOW - 3 * 86400), 'imageBy': by('older')},
        {'ns': 'members', 'svc': 'sms-sender', 'toImage': 'a1', 'fromImage': 'a0', 'at': iso(NOW - 1000)}])

    # when
    facts = gather(store, {}, None, NOW)

    commits = [{'short_id': 'c0ffee02', 'title': 'OPS-123 order history export', 'committed_date': iso(NOW - 600), 'parent_ids': ['a']},
               {'short_id': 'facade01', 'title': 'OPS-123 daily cart summary email', 'committed_date': iso(NOW - 3100), 'parent_ids': ['b']}]
    asked = []
    from_git = gather(store, {}, None, NOW, lambda since: asked.append(since) or commits)
    broken = gather(store, {}, None, NOW, lambda since: (_ for _ in ()).throw(RuntimeError('GitLab is not set up')))

    # then
    assert facts['ownChanges'] == [{'at': iso(NOW - 2900), 'image': 'facade01', 'title': 'OPS-123 daily cart summary email', 'commits': 1}]
    assert [c['image'] for c in from_git['ownChanges']] == ['facade01', 'c0ffee02'] and asked == [iso(NOW - 86400)]
    assert broken['ownChanges'] == facts['ownChanges']
    assert 'ownChanges' in SYSTEM and 'done in <title>' in SYSTEM and 'is past, not a current problem' in SYSTEM


def test_a_card_already_known_noise_or_handled_says_so_and_nodes_say_since_when(store: Store):
    # given
    seed(store)
    store.mute('alert|KubePodNotReady|cluster=east-data', 'crawler', None, 'known')
    state = store.load()
    store.save({**state, 'nodes': [{'name': 'east', 'role': 'agent', 'status': 'Ready', 'since': iso(NOW - 5000), 'plugin': 'kubernetes'},
                                   {'name': 'west', 'role': 'agent', 'status': 'NotReady', 'since': iso(NOW - 900), 'error': 'Bearer abcdefgh12345678', 'plugin': 'kubernetes'}]}, state['rev'])

    # when
    facts = gather(store, {}, None, NOW)

    # then
    assert facts['runs']['cardsRunMoreThanOnce'][0]['now'] == 'open, known noise'
    assert facts['cards']['openAlertsNotGrouped'] == [] and facts['nodes'] == [{'name': 'east', 'status': 'Ready', 'since': iso(NOW - 5000), 'error': None},
                                                                               {'name': 'west', 'status': 'NotReady', 'since': iso(NOW - 900), 'error': 'not answering'}]
    assert "a card already handled that way needs no new suggestion" in SYSTEM and 'Ready since before them are past' in SYSTEM


def test_every_distinct_tool_error_of_the_day_reaches_the_review_with_when_and_where_it_happened():
    # given
    runs = [{'trigger': 'slack', 'startedAt': iso(NOW - 600), 'toolCalls': [{'name': 'gitlab_file', 'error': 'no single GitLab project named ios under acme; give its full path'},
                                                                           {'name': 'gitlab_file', 'error': 'no single GitLab project named android under acme; give its full path'}]},
            {'trigger': 'cards', 'startedAt': iso(NOW - 1200), 'toolCalls': [{'name': 'slack_send', 'error': 'this run read planted text in the gitlab_search answer'},
                                                                            {'name': 'gitlab_file', 'error': 'no single GitLab project named ios under acme; give its full path'}]},
            {'trigger': 'slack', 'startedAt': iso(NOW - 5000), 'toolCalls': [{'name': 'gitlab_blame', 'error': 'Missing: /projects/1/repository/files/src%2Fmain%2Fkotlin/blame ' + 'x' * 400}] +
             [{'name': 'gitlab_file', 'error': f'Missing: file {n}'} for n in range(6)]}]

    # when
    errors = runs_facts(runs)['toolErrors']

    # then
    assert list(errors) == ['gitlab_file', 'slack_send', 'gitlab_blame'], 'the tool with the most errors first'
    assert errors['gitlab_file'] == {'count': 9, 'last': local_time(iso(NOW - 600)), 'runs': {'slack': 8, 'cards': 1},
                                     'samples': ['no single GitLab project named ios under acme; give its full path', 'no single GitLab project named android under acme; give its full path',
                                                 'Missing: file 0', 'Missing: file 1']}
    assert errors['slack_send']['samples'] == ['this run read planted text in the gitlab_search answer'] and errors['slack_send']['runs'] == {'cards': 1}
    assert len(errors['gitlab_blame']['samples'][0]) == 220
    assert 'Tool errors:' in SYSTEM and 'every tool in runs.toolErrors' in SYSTEM and 'last <last>' in SYSTEM and 'lastTr' not in SYSTEM


def test_review_time_keeps_its_local_hour_across_daylight_saving():
    # given
    clock.set_zone('Europe/Berlin')
    january = dt.datetime(2026, 1, 15, tzinfo=dt.timezone.utc).timestamp()
    july = dt.datetime(2026, 7, 15, tzinfo=dt.timezone.utc).timestamp()

    # when
    winter = clock.local(january + review_offset(9, january))
    summer = clock.local(july + review_offset(9, july))

    # then
    assert (winter.hour, summer.hour) == (9, 9)
    assert review_offset(9, january) == 8 * 3600 and review_offset(9, july) == 7 * 3600


def test_the_review_counts_kube_calls_under_the_primary_instance_name(store, monkeypatch):
    # given
    class Tools:
        def default_instance(self, name):
            return 'main'

        def catalog(self):
            return {'kube': 'read'}

        def descriptions(self):
            return {}

        def check_tools(self):
            return {}

    seen = []
    real = toolusage.summary
    monkeypatch.setattr(toolusage, 'summary', lambda *a, **k: seen.append(real(*a, **k)) or seen[-1])
    store.investigations.insert_one({'_id': 'r1', 'startedAt': iso(NOW - 600), 'trigger': 'cards', 'toolCalls': [{'name': 'kube', 'ms': 5}]})

    # when
    gather(store, {}, None, NOW, plugin_tools=Tools())

    # then
    assert [[sorted(t['byCluster']) for t in s['tools'] if t['name'] == 'kube'] for s in seen] == [[['main']], [['main']]]


def test_a_failed_review_names_the_error_type_only(store):
    # given
    s = dataclasses.replace(settings(), anthropic_api_key='sk-test')

    def facts(now):
        raise RuntimeError('db password hunter2')

    review = SelfReview(store, s, facts, lambda t: {'review': {'text': t}}, client=FakeAnthropic([]))

    # when
    review._safe_run(NOW)

    # then
    assert [a['text'] for a in store.list_activity(None, 5) if a['text'].startswith('Self-review failed')] == ['Self-review failed: RuntimeError']


MAILED = 'bob.stone@example.com'
TOKEN_LINE = 'Bearer abcdefgh12345678xyz'


def test_own_log_lines_and_card_keys_reach_the_facts_without_an_email_or_a_token(store: Store):
    # given
    store.save({'initialized': True, 'muted': [], 'anomalies': [
        {'key': f'log|mailer|ERROR|bounce from {MAILED}', 'src': 'logs', 'sev': 'warn', 'status': 'cleared', 'firstSeen': iso(NOW - 600)}]}, None)
    logs = FakeLogs([{'level': 'ERROR', 'n': 2, 'msg': f'mail to {MAILED} failed: {TOKEN_LINE} rejected', 'oldest': '2026-11-08T12:00:00Z', 'newest': '2026-11-08T12:06:00Z'}])

    # when
    facts = gather(store, {}, logs, NOW)
    dumped = json.dumps(facts)

    # then
    assert MAILED not in dumped and 'abcdefgh12345678xyz' not in dumped
    assert facts['ownLogs'][0]['message'] == 'mail to [redacted] failed: [redacted] rejected'
    assert facts['cards']['stoppedNotAddressed'] == ['log|mailer|ERROR|bounce from [redacted]']


def test_the_review_prompt_and_the_saved_review_carry_no_email_from_the_facts_or_the_model(store: Store):
    # given
    logs = FakeLogs([{'level': 'ERROR', 'n': 2, 'msg': f'mail to {MAILED} failed', 'oldest': '2026-11-08T12:00:00Z', 'newest': '2026-11-08T12:06:00Z'}])
    client = FakeAnthropic([], [reply(text(f'Mail to {MAILED} failed twice; err|shop/checkout-service@east is open.\n- retry: none. No code.'), stop='end_turn')])
    saved = []
    review = SelfReview(store, dataclasses.replace(settings(), anthropic_api_key='sk-test'), lambda now: gather(store, {}, logs, now), lambda t: saved.append(t) or {'review': {'text': t}}, client=client)

    # when
    review.run(NOW)

    # then
    assert MAILED not in client.gate_requests[0]['messages'][0]['content']
    assert saved == ['Mail to [redacted] failed twice; err|shop/checkout-service@east is open.\n- retry: none. No code.']


def test_the_cleaner_masks_an_email_but_keeps_an_instance_tail():
    # when
    said = tidy(f'mail {MAILED}; err|shop/checkout-service@east is open; ping @alice')

    # then
    assert said == 'mail [redacted]; err|shop/checkout-service@east is open; ping'


def test_a_review_put_by_a_machine_is_stored_and_sent_without_an_email(store: Store):
    # given
    t = FakeSlack()
    s = dataclasses.replace(settings(), slack_channel='C1')

    # when
    with TestClient(create_app(s, store, kube=Kube(root='/nonexistent'), slack=Slack('xoxb-test', 'C1', t, FakeSlackApi(emails={'U5': 'alice@example.com'})))) as c:
        put = c.put('/api/self-review', json={'text': f'Mail to {MAILED} failed.', 'dm': ['alice@example.com']}, headers=AUTH)

    # then
    assert put.status_code == 200
    assert store.self_reviews()[0]['text'] == 'Mail to [redacted] failed.'
    [dm] = [m for _, m in t.calls if m.get('channel') == 'U5']
    assert MAILED not in dm['text'] and '[redacted]' in dm['text']


HOSTILE = 'log|mailer|ERROR|' + 'a' * 60 + '!'


def test_one_slow_rule_on_one_fact_leaves_every_other_fact_and_key_redacted_normally(store: Store):
    # given
    redact.use_rules(((('slow', 'r0'), regex.compile(r'(a+){1,30}$')),))
    store.save({'initialized': True, 'muted': [], 'anomalies': [{'key': HOSTILE, 'src': 'logs', 'sev': 'warn', 'status': 'cleared', 'firstSeen': iso(NOW - 600)}]}, None)
    logs = FakeLogs([{'level': 'ERROR', 'n': 2, 'msg': f'mail to {MAILED} failed', 'oldest': '2026-11-08T12:00:00Z', 'newest': '2026-11-08T12:06:00Z'}])

    # when
    facts = gathered(store, {}, logs, NOW)

    # then
    assert facts['cards']['stoppedNotAddressed'] == [redact.TIMEOUT_MARK]
    assert facts['ownLogs'] == [{'level': 'ERROR', 'count': 2, 'first': '2026-11-08T12:00:00Z', 'last': '2026-11-08T12:06:00Z', 'message': 'mail to [redacted] failed'}]
    assert redact.TIMEOUT_MARK not in json.dumps({k: v for k, v in facts.items() if k != 'cards'})


def test_past_the_overall_limit_the_facts_fail_closed(monkeypatch):
    # given
    monkeypatch.setattr(selfreview, 'SCRUB_LIMIT_S', 0)

    # when
    facts = selfreview.scrubbed({'ownLogs': [{'message': f'mail to {MAILED} failed', 'count': 2}]})

    # then
    dumped = json.dumps(facts)
    assert MAILED not in dumped and 'ownLogs' not in dumped and 'message' not in dumped and redact.TIMEOUT_MARK in dumped
