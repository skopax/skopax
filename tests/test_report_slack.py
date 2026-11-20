import dataclasses
import datetime as dt
import re
import time
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from tests.conftest import AUTH, anomaly, settings, state
from tests.test_deploy_reports import report
from tests.fake_http import FakeHttp
from tests.fake_slack import FakeSlackApi
from tests.plugin_kit import serve_jira
from tests.test_slack import FakeSlack, client_with
from skopax import clock
from skopax.app import create_app
from skopax.slack import Slack, flag_card, report_message
from skopax.store import Store, iso

BRIEF = {'level': 'warn', 'headline': 'Release 05:06–05:28: 2 services; orders-service restarted 3 times at startup', 'points': ['orders-service healthy since 05:14', 'checkout-service clean']}
OLD_RELEASE = '{project} {dd}.{mm}.{yyyy} Release'
DRAFT = 'OPS 13.10 release: 2 services deployed, orders-service restarted 3 times at startup, healthy since 05:14.\nhttps://skopax.dev.example.com/#r=x'


def plain(text: str) -> str:
    return re.sub(r'<[^|>]+\|([^>]+)>', r'\1', text)


def flagged_lines(blocks: list[dict]) -> list[str]:
    texts = [b['text']['text'] for b in blocks if b['type'] == 'section' and 'text' in b]
    start = next(i for i, t in enumerate(texts) if t.startswith('*Flagged*'))
    out = []
    for t in texts[start:]:
        if t.startswith('*Shipped*'):
            break
        out += t.split('\n')
    return out


@pytest.fixture
def saved(store: Store) -> dict:
    with client_with(store, FakeSlack()) as client:
        body = report(brief=BRIEF, releases=['OPS 13.10.2026 Release'], issues=[{'key': 'OPS-106', 'inRelease': False}, {'key': 'OPS-115', 'inRelease': True}, {'key': 'OPS-116', 'inRelease': True}])
        body['rows'][0]['issues'] = ['OPS-115']
        body['rows'][1]['issues'] = ['OPS-106', 'WEB-0000']
        return client.post('/api/deploy-reports', json=body, headers=AUTH).json()


def test_report_keeps_claudes_summary_with_its_time(saved: dict, store: Store):
    # when
    with client_with(store, FakeSlack()) as client:
        listed = client.get('/api/deploy-reports').json()

    # then
    assert listed[0]['brief'] == {**BRIEF, 'at': saved['createdAt']}


def test_report_summary_is_validated(client: TestClient):
    # when
    blank = client.post('/api/deploy-reports', json=report(brief={'headline': ''}), headers=AUTH)
    bad_level = client.post('/api/deploy-reports', json=report(brief={'headline': 'h', 'level': 'fine'}), headers=AUTH)
    too_many = client.post('/api/deploy-reports', json=report(brief={'headline': 'h', 'points': ['p'] * 11}), headers=AUTH)
    default = client.post('/api/deploy-reports', json=report(brief={'headline': 'h'}), headers=AUTH)

    # then
    assert [blank.status_code, bad_level.status_code, too_many.status_code] == [422, 422, 422]
    assert default.json()['brief']['level'] == 'info'
    assert default.json()['brief']['points'] == []


def test_send_posts_the_report_and_records_it(saved: dict, store: Store):
    # given
    transport = FakeSlack()
    serve_jira(store, settings())
    s = dataclasses.replace(settings(), jira_release_name=OLD_RELEASE)

    # when
    with TestClient(create_app(s, store, slack=Slack('xoxb-test', '#ops', transport, FakeSlackApi()), transport=FakeHttp()), base_url='https://skopax.dev.example.com') as client:
        res = client.post(f"/api/deploy-reports/{saved['id']}/slack-posts", headers=AUTH)
        listed = client.get('/api/deploy-reports').json()
        actions = client.get('/api/actions').json()

    # then
    assert res.status_code == 201
    assert res.json()['type'] == 'slack'
    assert res.json()['status'] == 'done'
    assert res.json()['key'] == f"report:{saved['id']}"
    assert res.json()['target'] == f"slack:report:{saved['id']}"
    assert res.json()['label'] == 'Send to Slack: deploy report 2 deploys since 05:06 UTC: 1 clean, orders-service restarts'
    assert res.json()['result'] == 'sent to #ops'
    assert res.json()['draft'] is False
    assert [a['id'] for a in actions] == [res.json()['id']]
    post = listed[0]['slackPost']
    assert (post['channel'], post['ts'], post['by'], post['draft']) == ('C123', '1791900000.000100', 'token:bootstrap', False)
    assert post['at']
    _, payload = transport.calls[0]
    assert payload['channel'] == '#ops'
    assert payload['text'] == ':large_orange_circle: *OPS 13.10 release* · WARNING · 2 services · 1 flagged'
    card = payload['attachments'][0]
    assert card['color'] == '#e08a00'
    blocks = card['blocks']
    assert [b['type'] for b in blocks] == ['section', 'section', 'section', 'actions', 'context', 'context']
    assert blocks[0]['text']['text'] == '*WARNING* · *Release 05:06–05:28: 2 services*\norders-service restarted 3 times at startup\n• orders-service healthy since 05:14\n• checkout-service clean'
    assert plain(blocks[1]['text']['text']) == '*Flagged*\n• *orders-service* · restarts 3 · config binding failed until 05:14', 'no card on the page: the flag is plain text'
    assert blocks[2]['text']['text'] == '*Shipped* · 2 tickets\n<https://jira.example.com/browse/OPS-106|OPS-106>, <https://jira.example.com/browse/OPS-115|OPS-115>'
    assert blocks[3]['elements'] == [{'type': 'button', 'text': {'type': 'plain_text', 'text': 'Open report'}, 'url': f"https://skopax.example.com/#r={saved['id']}", 'style': 'primary'}]
    assert blocks[4]['elements'][0]['text'] == 'Window 05:06–05:35 UTC · Sent from Skopax by bootstrap'
    assert 'skopax.io' in blocks[5]['elements'][0]['text'], 'the powered-by line ends the attachment'


def test_draft_is_the_message_and_the_report_goes_under_it(saved: dict, store: Store):
    # given
    transport = FakeSlack()

    # when
    with client_with(store, transport) as client:
        put = client.put(f"/api/deploy-reports/{saved['id']}/slack-draft", json={'text': f'  {DRAFT} <b> <@U0FAKE00001>  '}, headers=AUTH)
        res = client.post(f"/api/deploy-reports/{saved['id']}/slack-posts", headers=AUTH)
        listed = client.get('/api/deploy-reports').json()

    # then
    assert put.status_code == 200
    assert put.json()['slackDraft']['text'] == f'{DRAFT} <b> <@U0FAKE00001>'
    assert put.json()['slackDraft']['by'] == 'token:bootstrap'
    assert put.json()['slackDraft']['at']
    assert listed[0]['slackDraft'] == put.json()['slackDraft']
    assert res.json()['draft'] is True
    assert res.json()['result'] == 'sent to #ops with the draft'
    assert listed[0]['slackPost']['draft'] is True
    _, payload = transport.calls[0]
    assert payload['text'] == DRAFT + ' &lt;b&gt; <@U0FAKE00001>'
    assert payload['attachments'][0]['blocks'][3]['elements'][0]['text']['text'] == 'Open report'


def test_empty_draft_clears_it_and_the_post_falls_back_to_the_headline(saved: dict, store: Store):
    # given
    transport = FakeSlack()

    # when
    with client_with(store, transport, jira_release_name=OLD_RELEASE) as client:
        client.put(f"/api/deploy-reports/{saved['id']}/slack-draft", json={'text': DRAFT}, headers=AUTH)
        cleared = client.put(f"/api/deploy-reports/{saved['id']}/slack-draft", json={'text': '   '}, headers=AUTH)
        client.post(f"/api/deploy-reports/{saved['id']}/slack-posts", headers=AUTH)
        listed = client.get('/api/deploy-reports').json()

    # then
    assert cleared.json() == {'slackDraft': None, 'autoSend': None}
    assert 'slackDraft' not in listed[0]
    assert transport.calls[0][1]['text'].startswith(':large_orange_circle: *OPS 13.10 release*')


def test_draft_and_send_need_an_operator_and_a_known_report(saved: dict, store: Store):
    # given
    transport = FakeSlack()

    # when
    with client_with(store, transport) as client:
        anonymous_draft = client.put(f"/api/deploy-reports/{saved['id']}/slack-draft", json={'text': 'draft'})
        anonymous_send = client.post(f"/api/deploy-reports/{saved['id']}/slack-posts")
        unknown_draft = client.put('/api/deploy-reports/nope/slack-draft', json={'text': 'draft'}, headers=AUTH)
        unknown_send = client.post('/api/deploy-reports/nope/slack-posts', headers=AUTH)
        too_long = client.put(f"/api/deploy-reports/{saved['id']}/slack-draft", json={'text': 'x' * 1501}, headers=AUTH)
        listed = client.get('/api/deploy-reports').json()

    # then
    assert [anonymous_draft.status_code, anonymous_send.status_code] == [401, 401]
    assert [unknown_draft.status_code, unknown_send.status_code] == [404, 404]
    assert too_long.status_code == 422
    assert transport.calls == []
    assert 'slackDraft' not in listed[0] and 'slackPost' not in listed[0]


def test_refused_send_is_recorded_as_failed_and_leaves_no_post(saved: dict, store: Store):
    # given
    transport = FakeSlack(error='not_in_channel')

    # when
    with client_with(store, transport) as client:
        res = client.post(f"/api/deploy-reports/{saved['id']}/slack-posts", headers=AUTH)
        actions = client.get('/api/actions').json()
        listed = client.get('/api/deploy-reports').json()

    # then
    assert res.status_code == 502
    assert 'not_in_channel' in res.json()['detail']
    assert (actions[0]['status'], actions[0]['result'], actions[0]['target']) == ('failed', 'not sent: not_in_channel', f"slack:report:{saved['id']}")
    assert 'slackPost' not in listed[0]


def test_send_without_a_slack_token_is_refused(saved: dict, store: Store):
    # given
    transport = FakeSlack()

    # when
    with client_with(store, transport, token='') as client:
        res = client.post(f"/api/deploy-reports/{saved['id']}/slack-posts", headers=AUTH)

    # then
    assert res.status_code == 503
    assert transport.calls == []


def test_message_without_a_summary_or_release_for_a_clean_report():
    # given
    clean = {'summary': '1 deploy since 06:00 UTC: all clean', 'windowFrom': '2026-10-15T06:00:00Z', 'createdAt': '2026-10-15T06:40:00.000Z',
             'rows': [{'svc': 'checkout-service', 'flags': [], 'issues': ['OPS-1', 'OPS-2']}], 'releases': ['OPS 15.10.2026 Release'], 'issues': [{'key': 'OPS-1', 'inRelease': False}]}

    # when
    message = report_message(clean, 'https://skopax.dev.example.com/#r=r1', 'oscar.green@example.com', jira_site='https://jira.example.com')
    unchecked = report_message({**clean, 'issues': []}, 'https://skopax.dev.example.com/#r=r1', 'token')

    # then
    assert message['text'] == ':large_green_circle: *Deploy report* · 15 Oct 06:00 UTC · ALL CLEAR · 1 service · clean'
    card = message['attachments'][0]
    assert card['color'] == '#1f8a4c'
    assert [b['type'] for b in card['blocks']] == ['section', 'section', 'actions', 'context']
    assert card['blocks'][0]['text']['text'] == '*ALL CLEAR* · 1 deploy since 06:00 UTC: all clean'
    assert card['blocks'][1]['text']['text'] == '*Shipped* · 1 ticket\n<https://jira.example.com/browse/OPS-1|OPS-1>'
    assert card['blocks'][3]['elements'][0]['text'] == 'Window 06:00–now UTC · Sent from Skopax by oscar.green'
    assert unchecked['attachments'][0]['blocks'][1]['text']['text'].startswith('*Shipped* · 2 tickets\n'), 'without Jira details the commit keys stand in'


def test_message_caps_long_lists():
    # given
    rows = [{'svc': f's{i}', 'ns': 'a-prod', 'flags': ['restarts=1'], 'note': 'x' * 200, 'issues': [f'OPS-{i}', f'OPS-{i + 100}']} for i in range(10)]
    rows[1]['svc'] = 's0'
    rows[1]['ns'] = 'b-prod'
    big = {'summary': 's', 'rows': rows, 'brief': {'level': 'crit', 'headline': 'h', 'points': [f'p{i}' for i in range(8)]}}

    # when
    message = report_message(big, 'https://w/#r=r1', 'token', jira_site='https://jira.example.com')
    blocks = message['attachments'][0]['blocks']

    # then
    assert message['text'] == ':red_circle: *Deploy report* · post-deploy check · CRITICAL · 10 services · 10 flagged'
    assert blocks[0]['text']['text'].endswith('• p5\n_+2 more on the report_')
    lines = flagged_lines(blocks)
    flagged = plain('\n'.join(lines))
    assert len([x for x in lines if x.startswith('• ')]) == 8
    assert lines[-1] == '_+2 more on the report_'
    assert '• *s0* (a-prod) · restarts 1 · ' in flagged and '• *s0* (b-prod) · ' in flagged and '• *s2* · ' in flagged, 'namespace only where a service name repeats'
    assert '·  ' not in flagged and 'x' * 89 + '…' in flagged and 'x' * 90 not in flagged, 'notes cut to 90 characters'
    sections = [b['text']['text'] for b in blocks if b['type'] == 'section' and 'text' in b]
    assert all(len(t) <= 3000 for t in sections), 'every section stays under Slack\'s 3000 characters'
    assert len([t for t in sections if not t.startswith(('*CRITICAL* · *h*', '*Shipped*'))]) >= 1
    shipped = next(t for t in sections if t.startswith('*Shipped*'))
    assert shipped.startswith('*Shipped* · 20 tickets\n') and shipped.endswith('|OPS-105> +8 more')


def test_release_names_shorten_only_when_they_share_a_day():
    # given
    issues = [{'key': 'OPS-1', 'inRelease': True}]

    # when
    same = report_message({'rows': [], 'releases': ['OPS 15.10.2026 Release', 'WEB 15.10.2026 Release'], 'issues': issues}, 'https://w/#r=1', 'token', release_template=OLD_RELEASE)
    mixed = report_message({'rows': [], 'releases': ['OPS 15.10.2026 Release', 'WEB 14.10.2026 Release'], 'issues': issues}, 'https://w/#r=1', 'token', release_template=OLD_RELEASE)
    odd = report_message({'rows': [], 'releases': ['Hotfix 1'], 'issues': issues}, 'https://w/#r=1', 'token', release_template=OLD_RELEASE)

    # then
    assert same['text'] == ':large_green_circle: *OPS + WEB 15.10 release* · ALL CLEAR · 0 services · clean'
    assert mixed['text'] == ':large_green_circle: *OPS 15.10.2026 Release + WEB 14.10.2026 Release* · ALL CLEAR · 0 services · clean'
    assert odd['text'] == ':large_green_circle: *Hotfix 1* · ALL CLEAR · 0 services · clean'


def test_flagged_notes_drop_the_repeated_flag_and_end_on_a_whole_word():
    # given
    rows = [
        {'svc': 'search-service', 'flags': ['warn up'], 'note': "warn up = checkout-service cart-totals/calculate 400 'Order total is far too low or too high' (calculator input); 35 at 08:50"},
        {'svc': 'web-bff', 'flags': ['out-err up'], 'note': 'out-err 7 = cancelled calls to catalog-service /storefront-product-price-summaries (UNKNOWN, no 5xx) at 08:50–08:53 during the rollout'},
        {'svc': 'orders-service', 'flags': ['restarts=3'], 'note': "05:11-05:13 APPLICATION FAILED TO START, bind 'encryption'; healthy since 05:14"},
        {'svc': 'checkout-service', 'flags': ['err up'], 'note': 'warn up = a note about another signal'},
        {'svc': 'payments-api', 'flags': ['5xx'], 'note': '5xx = '},
    ]

    # when
    lines = [plain(x) for x in flagged_lines(report_message({'summary': 's', 'rows': rows}, 'https://w/#r=1', 'token')['attachments'][0]['blocks'])]

    # then
    assert lines[1] == "• *search-service* · warn up · checkout-service cart-totals/calculate 400 'Order total is far too low or too high'…"
    assert lines[2] == '• *web-bff* · out-err up · cancelled calls to catalog-service /storefront-product-price-summaries (UNKNOWN, no 5xx)…'
    assert lines[3] == "• *orders-service* · restarts 3 · 05:11-05:13 APPLICATION FAILED TO START, bind 'encryption'; healthy since 05:14"
    assert lines[4] == '• *checkout-service* · err up · warn up = a note about another signal', 'a lead naming another signal stays'
    assert lines[5] == '• *payments-api* · 5xx', 'a note that only repeats the flag leaves nothing behind'


def test_send_links_a_flag_to_its_anomaly_card_when_it_is_on_the_page(saved: dict, store: Store):
    # given
    transport = FakeSlack()
    store.save(state(anomaly('restarts|payments/orders-service', svc='orders-service', ns='payments', label='restarts', firstSeen='2026-10-13T05:15:00Z', lastSeen='2026-10-13T05:40:00Z')), None)

    # when
    with client_with(store, transport) as client:
        client.post(f"/api/deploy-reports/{saved['id']}/slack-posts", headers=AUTH)

    # then
    flagged = transport.calls[0][1]['attachments'][0]['blocks'][1]['text']['text']
    assert '\u2022 *orders-service* \u00b7 <https://skopax.example.com/#a=restarts%7Cpayments%2Forders-service|restarts 3 \u203a> \u00b7 config binding failed until 05:14' in flagged


def test_flag_links_pick_a_card_from_around_the_deploy_else_the_logs():
    # given
    rows = [
        {'svc': 'orders-service', 'ns': 'payments', 'at': '2026-10-15T05:12:00Z', 'flags': ['err up', 'unavail=1']},
        {'svc': 'old-service', 'ns': 'shop', 'at': '2026-10-15T05:12:00Z', 'flags': ['5xx up']},
    ]
    anomalies = [
        {'key': 'log|orders-service|ERROR|Could not bind encryption', 'firstSeen': '2026-10-15T05:14:00Z', 'lastSeen': '2026-10-15T05:20:00Z', 'status': 'cleared'},
        {'key': 'unavailable|payments/orders-service', 'firstSeen': '2026-10-15T05:13:00Z', 'lastSeen': '2026-10-15T05:30:00Z'},
        {'key': '5xx|shop/old-service', 'firstSeen': '2026-10-12T05:00:00Z', 'lastSeen': '2026-10-12T06:00:00Z'},
    ]
    r = {'summary': 's', 'rows': rows, 'windowFrom': '2026-10-15T05:06:00Z', 'windowTo': '2026-10-15T05:40:00Z'}

    # when
    lines = flagged_lines(report_message(r, 'https://skopax.dev.example.com/#r=r1', 'token', anomalies=anomalies)['attachments'][0]['blocks'])

    # then
    assert lines[1].startswith('• *orders-service* · <https://skopax.dev.example.com/#a=log%7Corders-service%7CERROR%7CCould%20not%20bind%20encryption|err up ›>, '
                               '<https://skopax.dev.example.com/#a=unavailable%7Cpayments%2Forders-service|unavail 1 ›>'), 'err falls back to the log pattern card, unavail maps to unavailable'
    assert lines[2] == '• *old-service* · 5xx up', 'a card from days before the deploy is not its card, so the flag stays plain text'


@pytest.mark.parametrize('level,name', [('ok', 'ALL CLEAR'), ('warn', 'WARNING'), ('crit', 'CRITICAL'), ('info', 'INFO')])
def test_the_post_names_the_level_like_the_page_also_under_a_draft(level, name):
    # given
    r = {'summary': 's', 'rows': [], 'brief': {'level': level, 'headline': 'Release 09:00–09:20: 3 services; nothing new since the deploy', 'points': ['p']}}

    # when
    drafted = report_message(r, 'https://w/#r=r1', 'token', draft='Deploy clean.')
    old = report_message({'summary': '1 deploy since 06:00 UTC: all clean', 'rows': [{'svc': 'a', 'flags': ['restarts=1']}]}, 'https://w/#r=r1', 'token', draft='Draft.')

    # then
    assert drafted['text'] == 'Deploy clean.', 'the draft stays the message text'
    assert drafted['attachments'][0]['blocks'][0]['text']['text'].startswith(f'*{name}* · *Release 09:00–09:20: 3 services*\nnothing new since the deploy')
    assert old['attachments'][0]['blocks'][0]['text']['text'] == '*WARNING* · 1 deploy since 06:00 UTC: all clean', 'a report without a summary takes its level from the flags'


def second_report(client: TestClient) -> dict:
    return client.post('/api/deploy-reports', json=report(brief={'level': 'ok', 'headline': 'Later deploy: 1 service, clean'}), headers=AUTH).json()


def test_a_later_report_the_same_local_day_goes_into_the_first_reports_thread(saved: dict, store: Store, monkeypatch):
    # given
    now = dt.datetime(2026, 11, 15, 0, 2, tzinfo=dt.timezone.utc)
    monkeypatch.setattr('skopax.app.time', SimpleNamespace(time=now.timestamp))
    transport = FakeSlack()
    store.report_slack_posted(saved['id'], {'channel': 'C777', 'ts': '1792000000.000100', 'at': iso(now - dt.timedelta(seconds=90)), 'by': 'auto'})

    # when
    with client_with(store, transport) as client:
        later = second_report(client)
        res = client.post(f"/api/deploy-reports/{later['id']}/slack-posts", headers=AUTH)
        third = second_report(client)
        again = client.post(f"/api/deploy-reports/{third['id']}/slack-posts", headers=AUTH)
        posts = {r['id']: r.get('slackPost') for r in client.get('/api/deploy-reports').json()}

    # then
    assert res.status_code == 201 and res.json()['result'] == "sent to today's deploy thread in #ops"
    assert [(p['channel'], p['thread_ts']) for _, p in transport.calls] == [('C777', '1792000000.000100'), ('C777', '1792000000.000100')]
    assert 'reply_broadcast' not in transport.calls[0][1], 'a later report of the day stays in the thread only'
    assert posts[later['id']]['threadTs'] == '1792000000.000100' and posts[third['id']]['threadTs'] == '1792000000.000100'
    assert again.json()['status'] == 'done' and 'threadTs' not in posts[saved['id']]


def test_a_report_from_an_earlier_local_day_or_a_reply_does_not_start_todays_thread(saved: dict, store: Store):
    # given
    transport = FakeSlack()
    start = clock.local(clock.midnight(time.time()))
    store.report_slack_posted(saved['id'], {'channel': 'C777', 'ts': '1791000000.000100', 'at': iso((start - dt.timedelta(minutes=5)).astimezone(dt.timezone.utc)), 'by': 'auto'})

    # when
    with client_with(store, transport) as client:
        today = second_report(client)
        first = client.post(f"/api/deploy-reports/{today['id']}/slack-posts", headers=AUTH)

    # then
    assert first.json()['result'] == 'sent to #ops'
    _, payload = transport.calls[0]
    assert payload['channel'] == '#ops' and 'thread_ts' not in payload
    assert store.report_day_thread(iso(start.astimezone(dt.timezone.utc)), iso((start + dt.timedelta(days=1)).astimezone(dt.timezone.utc)), 'none')['ts'] == '1791900000.000100'


def test_a_flag_on_a_second_instance_row_links_that_instances_card():
    # given
    rows = [{'svc': 'orders-service', 'ns': 'payments', 'at': '2026-10-15T05:12:00Z', 'flags': ['out-err up']},
            {'svc': 'orders-service', 'ns': 'payments', 'cluster': 'east', 'at': '2026-10-15T05:12:00Z', 'flags': ['out-err up']}]
    main = {'key': 'out-err|payments/orders-service', 'firstSeen': '2026-10-15T05:13:00Z', 'lastSeen': '2026-10-15T05:30:00Z'}
    east = {'key': 'out-err|payments/orders-service@east', 'cluster': 'east', 'firstSeen': '2026-10-15T05:13:00Z', 'lastSeen': '2026-10-15T05:30:00Z'}
    sub = {'key': 'out-err|payments/orders-service|db@east', 'cluster': 'east', 'firstSeen': '2026-10-15T05:13:00Z', 'lastSeen': '2026-10-15T05:30:00Z'}
    r = {'summary': 's', 'rows': rows, 'windowFrom': '2026-10-15T05:06:00Z', 'windowTo': '2026-10-15T05:40:00Z'}

    # when
    both = [flag_card(x, r, 'out-err', [main, east]) for x in rows]
    east_only = [flag_card(x, r, 'out-err', [east]) for x in rows]
    sub_only = [flag_card(x, r, 'out-err', [sub]) for x in rows]

    # then
    assert [c['key'] if c else None for c in both] == [main['key'], east['key']]
    assert [c['key'] if c else None for c in east_only] == [None, east['key']]
    assert [c['key'] if c else None for c in sub_only] == [None, sub['key']]
