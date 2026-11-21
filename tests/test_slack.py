import dataclasses
import urllib.request

import pytest
from fastapi.testclient import TestClient

from tests.conftest import AUTH, anomaly, settings, state
from tests.fake_slack import FakeSlackApi
from skopax.app import create_app
from skopax.slack import Slack, SlackDown, SlackError, anomaly_message, api, same_person, upload
from skopax.store import Store

KEY = 'log|web-bff|WARN|Define logo for BRAND'


class FakeSlack:
    def __init__(self, error: str | None = None):
        self.error = error
        self.calls: list[tuple[str, dict]] = []

    def __call__(self, token: str, payload: dict) -> dict:
        self.calls.append((token, payload))
        if self.error:
            raise SlackError(self.error, code=self.error)
        return {'ok': True, 'channel': 'C123', 'ts': '1791900000.000100'}


def client_with(store: Store, transport: FakeSlack, token: str = 'xoxb-test', api: FakeSlackApi | None = None, **over) -> TestClient:
    s = dataclasses.replace(settings(), **over) if over else settings()
    return TestClient(create_app(s, store, slack=Slack(token, '#ops', transport, api or FakeSlackApi())), base_url='https://skopax.dev.example.com')


@pytest.fixture
def seeded(store: Store) -> Store:
    store.save(state(anomaly(KEY, sev='crit', svc='web-bff', label='WARN pattern', now=116, d1=0, d7=0, unit='', firstSeen='2026-10-13T09:22:06Z',
                             msg='Define logo for BRAND', src='logs', ns='-', links=[{'label': 'Log lines in Kibana', 'url': 'https://kibana.prod.example.com/x'}, {'label': 'All errors', 'url': 'https://kibana.prod.example.com/y'}, {'label': 'Graph', 'url': 'https://prometheus.prod.example.com/z'}, {'label': 'bad', 'url': 'javascript:alert(1)'}], what='new WARN pattern: Define logo for BRAND', suspects=['BRAND.svg rejected by asset-service'], recommend='fix the icon source ' + 'x' * 300,
                             note={'text': 'checked <asset-service> logs & catalog-service', 'at': '2026-10-13T10:00:00Z'})), None)
    return store


def test_send_posts_the_anomaly_and_records_who_sent_it(seeded: Store):
    # given
    transport = FakeSlack()

    # when
    with client_with(seeded, transport) as client:
        res = client.post('/api/slack-posts', json={'key': KEY}, headers=AUTH)
        listed = client.get('/api/actions').json()

    # then
    assert res.status_code == 201
    assert res.json()['type'] == 'slack'
    assert res.json()['status'] == 'done'
    assert res.json()['target'] == f'slack:{KEY}'
    assert res.json()['requestedBy'] == 'token:bootstrap'
    assert res.json()['result'] == 'sent to #ops'
    assert res.json()['slack'] == {'channel': 'C123', 'ts': '1791900000.000100', 'permalink': 'https://example.slack.com/archives/C123/p1791900000000100'}
    assert res.json()['doneAt']
    assert [a['id'] for a in listed] == [res.json()['id']]
    token, payload = transport.calls[0]
    assert token == 'xoxb-test'
    assert payload['channel'] == '#ops'
    assert payload['text'] == 'Critical · web-bff / WARN pattern'
    assert 'attachments' not in payload
    blocks = payload['blocks']
    assert [b['type'] for b in blocks] == ['section', 'section', 'context', 'context']
    assert blocks[0]['text']['text'] == ':red_circle: bootstrap shared a card \u00b7 Critical'
    assert blocks[1]['text']['text'] == '*<https://skopax.example.com/#a=log%7Cweb-bff%7CWARN%7CDefine%20logo%20for%20BRAND|web-bff / WARN pattern>*'
    assert blocks[2]['elements'][0]['text'] == '`Define logo for BRAND`'
    assert [e['text'] for e in blocks[3]['elements']][:-1] == ['Open \u00b7 Critical \u00b7 since 13 Oct 09:22 UTC', 'Now *116* \u00b7 yesterday 0 \u00b7 last week 0',
                                                          '<https://kibana.prod.example.com/x|Log lines in Kibana> \u00b7 <https://kibana.prod.example.com/y|All errors> \u00b7 <https://prometheus.prod.example.com/z|Graph>']
    assert 'skopax.io' in blocks[3]['elements'][-1]['text'], 'the powered-by line ends the bar'
    assert 'fix the icon source' not in str(blocks), 'the next step stays on the card'
    menu = blocks[0]['accessory']
    assert menu['type'] == 'overflow' and menu['action_id'] == 'card_menu'
    assert [(o['text']['text'], o['value']) for o in menu['options']] == [('Ask in chat', 'chat')], 'no triage options while the listener is off'
    assert res.json()['draft'] is False


DRAFT = 'BFF 116 WARN for the BRAND logo since 12:22 UTC <asset-service> & catalog-service\nOperations should upload the logo.\nhttps://skopax.example.com/#a=log%7Cweb-bff%7CWARN%7CDefine%20logo%20for%20BRAND'


def test_send_uses_the_draft_as_the_message_and_keeps_the_card(seeded: Store):
    # given
    transport = FakeSlack()
    with client_with(seeded, transport) as client:
        client.put('/api/slack-drafts', json={'key': KEY, 'text': DRAFT}, headers=AUTH)

        # when
        res = client.post('/api/slack-posts', json={'key': KEY}, headers=AUTH)

    # then
    assert res.status_code == 201
    assert res.json()['draft'] is True
    assert res.json()['result'] == 'sent to #ops with the draft'
    _, payload = transport.calls[0]
    assert payload['text'] == DRAFT.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
    assert '\n' in payload['text']
    blocks = payload['blocks']
    assert [b['type'] for b in blocks] == ['section', 'section', 'section', 'context', 'context']
    assert blocks[2]['text']['text'] == payload['text']
    assert blocks[1]['text']['text'].startswith('*<https://skopax.example.com/#a=log%7Cweb-bff%7CWARN%7CDefine%20logo%20for%20BRAND|')


def test_draft_is_saved_on_the_anomaly_and_moves_the_rev(seeded: Store):
    # given
    before = seeded.rev()['rev']

    # when
    with client_with(seeded, FakeSlack()) as client:
        res = client.put('/api/slack-drafts', json={'key': KEY, 'text': '  first line\nsecond line  '}, headers=AUTH)
        saved = client.get('/api/state').json()

    # then
    assert res.status_code == 200
    assert res.json()['rev'] == before + 1
    draft = saved['anomalies'][0]['slackDraft']
    assert draft['text'] == 'first line\nsecond line'
    assert draft['by'] == 'token:bootstrap'
    assert draft['at'].endswith('Z')
    assert res.json()['slackDraft'] == draft
    assert saved['rev'] == before + 1


def test_a_draft_saved_after_the_check_read_the_state_makes_its_write_conflict(seeded: Store):
    # given
    with client_with(seeded, FakeSlack()) as client:
        read = client.get('/api/state').json()
        client.put('/api/slack-drafts', json={'key': KEY, 'text': 'draft'}, headers=AUTH)

        # when
        res = client.put('/api/state', json=read, headers=AUTH | {'X-Base-Rev': str(read['rev'])})
        kept = client.get('/api/state').json()

    # then
    assert res.status_code == 409
    assert kept['anomalies'][0]['slackDraft']['text'] == 'draft'


def test_empty_draft_clears_it_and_the_post_falls_back_to_the_headline(seeded: Store):
    # given
    transport = FakeSlack()
    with client_with(seeded, transport) as client:
        client.put('/api/slack-drafts', json={'key': KEY, 'text': 'draft'}, headers=AUTH)

        # when
        cleared = client.put('/api/slack-drafts', json={'key': KEY, 'text': '   '}, headers=AUTH)
        saved = client.get('/api/state').json()
        res = client.post('/api/slack-posts', json={'key': KEY}, headers=AUTH)

    # then
    assert cleared.status_code == 200
    assert cleared.json()['slackDraft'] is None
    assert 'slackDraft' not in saved['anomalies'][0]
    assert res.json()['draft'] is False
    assert transport.calls[0][1]['text'] == 'Critical · web-bff / WARN pattern'


def test_draft_needs_an_operator_and_a_known_key(seeded: Store):
    # when
    with client_with(seeded, FakeSlack()) as client:
        anonymous = client.put('/api/slack-drafts', json={'key': KEY, 'text': 'draft'})
        unknown = client.put('/api/slack-drafts', json={'key': 'nope', 'text': 'draft'}, headers=AUTH)
        too_long = client.put('/api/slack-drafts', json={'key': KEY, 'text': 'x' * 1501}, headers=AUTH)
        saved = client.get('/api/state').json()

    # then
    assert anonymous.status_code == 401
    assert unknown.status_code == 404
    assert too_long.status_code == 422
    assert 'slackDraft' not in saved['anomalies'][0]


def test_failed_send_is_recorded_and_reported(seeded: Store):
    # given
    transport = FakeSlack(error='not_in_channel')

    # when
    with client_with(seeded, transport) as client:
        res = client.post('/api/slack-posts', json={'key': KEY}, headers=AUTH)
        listed = client.get('/api/actions').json()

    # then
    assert res.status_code == 502
    assert 'not_in_channel' in res.json()['detail']
    assert listed[0]['status'] == 'failed'
    assert listed[0]['result'] == 'not sent: not_in_channel'
    assert listed[0]['failedAt']


def test_send_without_a_token_is_refused_and_posts_nothing(seeded: Store):
    # given
    transport = FakeSlack()

    # when
    with client_with(seeded, transport, token='') as client:
        res = client.post('/api/slack-posts', json={'key': KEY}, headers=AUTH)
        me = client.get('/api/me').json()

    # then
    assert res.status_code == 503
    assert transport.calls == []
    assert me['slack'] is False
    assert me['slackChannel'] == '#ops'


def test_send_needs_an_operator(seeded: Store):
    # given
    transport = FakeSlack()

    # when
    with client_with(seeded, transport) as client:
        res = client.post('/api/slack-posts', json={'key': KEY})

    # then
    assert res.status_code == 401
    assert transport.calls == []


def test_send_for_an_unknown_anomaly_is_not_found(seeded: Store):
    # given
    transport = FakeSlack()

    # when
    with client_with(seeded, transport) as client:
        res = client.post('/api/slack-posts', json={'key': 'nope'}, headers=AUTH)

    # then
    assert res.status_code == 404
    assert transport.calls == []


def test_slack_cannot_be_queued_as_a_claude_action(seeded: Store):
    # when
    with client_with(seeded, FakeSlack()) as client:
        res = client.post('/api/actions', json={'type': 'slack', 'key': KEY}, headers=AUTH)

    # then
    assert res.status_code == 422


def test_message_for_a_cleared_latency_item_by_a_person():
    # given
    item = anomaly('apm|rpt|p95', sev='info', status='cleared', svc='reports-service', label='p95', sub='GET /x', now=1695, d1=326, d7=None, unit='ms', firstSeen='2026-10-13T08:18:00Z')

    # when
    message = anomaly_message(item, 'https://w/#a=x', 'oscar.green@example.com')

    # then
    assert message['text'] == 'Stopped firing \u00b7 unassigned · reports-service / p95 · GET /x'
    blocks = message['blocks']
    assert [b['type'] for b in blocks] == ['section', 'section', 'context']
    assert blocks[0]['text']['text'] == ':large_yellow_circle: oscar.green shared a card \u00b7 Info'
    assert blocks[1]['text']['text'] == '*<https://w/#a=x|reports-service / p95 \u00b7 GET /x>*'
    assert [e['text'] for e in blocks[2]['elements']] == ['Stopped firing \u00b7 Info \u00b7 since 13 Oct 08:18 UTC', 'Now *1.7 s* \u00b7 yesterday 326 ms']


def test_message_with_a_draft_puts_it_under_the_title_and_keeps_the_rest_of_the_plain_message():
    # given
    item = anomaly('apm|rpt|p95', sev='warn', svc='reports-service', label='p95', now=1695, d1=326, d7=None, unit='ms', firstSeen='2026-10-13T08:18:00Z')

    # when
    plain = anomaly_message(item, 'https://w/#a=x', 'token')
    drafted = anomaly_message(item, 'https://w/#a=x', 'token', 'p95 <2 s> & rising')

    # then
    assert drafted['text'] == 'p95 &lt;2 s&gt; &amp; rising'
    assert drafted['blocks'] == plain['blocks'][:2] + [{'type': 'section', 'text': {'type': 'mrkdwn', 'text': 'p95 &lt;2 s&gt; &amp; rising'}}] + plain['blocks'][2:]


def test_draft_keeps_user_mentions_and_escapes_broadcasts():
    # given
    item = anomaly('alert|CloudCostDailyAnomaly|', sev='warn', svc='-', label='CloudCostDailyAnomaly', now=1, d1=None, d7=None, unit='', firstSeen='2026-10-19T05:51:03Z')

    # when
    drafted = anomaly_message(item, 'https://w/#a=x', 'token', '<@U0FAKE00001> <@W012AB3CD> can you look? <!channel> <!subteam^S123> <@here> <b>')

    # then
    assert drafted['text'] == '<@U0FAKE00001> <@W012AB3CD> can you look? &lt;!channel&gt; &lt;!subteam^S123&gt; &lt;@here&gt; &lt;b&gt;'


def test_send_records_the_post_on_the_anomaly(seeded: Store):
    # given
    before = seeded.rev()['rev']

    # when
    with client_with(seeded, FakeSlack()) as client:
        client.post('/api/slack-posts', json={'key': KEY}, headers=AUTH)
        saved = client.get('/api/state').json()

    # then
    post = saved['anomalies'][0]['slackPost']
    assert {k: post[k] for k in ('channel', 'ts', 'by', 'draft')} == {'channel': 'C123', 'ts': '1791900000.000100', 'by': 'token:bootstrap', 'draft': False}
    assert post['at'].endswith('Z')
    assert saved['rev'] == before + 1


def test_failed_send_records_no_post(seeded: Store):
    # when
    with client_with(seeded, FakeSlack(error='not_in_channel')) as client:
        client.post('/api/slack-posts', json={'key': KEY}, headers=AUTH)
        saved = client.get('/api/state').json()

    # then
    assert 'slackPost' not in saved['anomalies'][0]


def test_thread_update_replies_under_the_posted_message(seeded: Store):
    # given
    transport = FakeSlack()
    with client_with(seeded, transport) as client:
        client.post('/api/slack-posts', json={'key': KEY}, headers=AUTH)

        # when
        res = client.post('/api/slack-updates', json={'key': KEY, 'text': '  Solved <OPS-102> & closed <@U0FAKE00001>  '}, headers=AUTH)
        listed = client.get('/api/actions').json()

    # then
    assert res.status_code == 201
    assert res.json()['type'] == 'slack-update'
    assert res.json()['target'] == f'slack-update:{KEY}'
    assert res.json()['status'] == 'done'
    assert res.json()['result'] == 'replied in the Slack thread'
    _, payload = transport.calls[1]
    assert payload['channel'] == 'C123'
    assert payload['thread_ts'] == '1791900000.000100'
    assert payload['text'] == 'Solved &lt;OPS-102&gt; &amp; closed <@U0FAKE00001>'
    assert 'attachments' not in payload
    assert [a['type'] for a in listed] == ['slack-update', 'slack']
    assert seeded.anomaly(KEY)['slackPost']['update']['at'] >= seeded.anomaly(KEY)['slackPost']['at']


def test_thread_update_needs_an_earlier_post_and_text(seeded: Store):
    # given
    transport = FakeSlack()

    # when
    with client_with(seeded, transport) as client:
        unposted = client.post('/api/slack-updates', json={'key': KEY, 'text': 'update'}, headers=AUTH)
        client.post('/api/slack-posts', json={'key': KEY}, headers=AUTH)
        empty = client.post('/api/slack-updates', json={'key': KEY, 'text': '   '}, headers=AUTH)
        anonymous = client.post('/api/slack-updates', json={'key': KEY, 'text': 'update'})
        unknown = client.post('/api/slack-updates', json={'key': 'nope', 'text': 'update'}, headers=AUTH)

    # then
    assert unposted.status_code == 409
    assert empty.status_code == 422
    assert anonymous.status_code == 401
    assert unknown.status_code == 404
    assert len(transport.calls) == 1


def test_refused_thread_update_is_recorded_and_reported(seeded: Store):
    # given
    transport = FakeSlack()
    with client_with(seeded, transport) as client:
        client.post('/api/slack-posts', json={'key': KEY}, headers=AUTH)
        transport.error = 'thread_not_found'

        # when
        res = client.post('/api/slack-updates', json={'key': KEY, 'text': 'update'}, headers=AUTH)
        listed = client.get('/api/actions').json()

    # then
    assert res.status_code == 502
    assert listed[0]['type'] == 'slack-update'
    assert listed[0]['status'] == 'failed'
    assert listed[0]['result'] == 'not sent: thread_not_found'


def test_thread_update_without_a_token_is_refused(seeded: Store):
    # when
    with client_with(seeded, FakeSlack(), token='') as client:
        res = client.post('/api/slack-updates', json={'key': KEY, 'text': 'update'}, headers=AUTH)

    # then
    assert res.status_code == 503


def test_thread_update_cannot_be_queued_as_a_claude_action(seeded: Store):
    # when
    with client_with(seeded, FakeSlack()) as client:
        res = client.post('/api/actions', json={'type': 'slack-update', 'key': KEY}, headers=AUTH)

    # then
    assert res.status_code == 422


def test_a_self_review_is_kept_newest_first_and_sent_as_a_dm_to_the_named_people(store: Store):
    # given
    api = FakeSlackApi(emails={'U7': 'oscar.green@example.com'})
    t = FakeSlack()
    with client_with(store, t, api=api, email_domains=('example.com',)) as client:

        # when
        first = client.put('/api/self-review', json={'text': 'Yesterday was fine.', 'dm': []}, headers=AUTH)
        second = client.put('/api/self-review', json={'text': '- 3 cards closed with nobody looking\n- **integrity** again', 'dm': ['oscar.green@example.com', 'no.person@example.com']}, headers=AUTH)
        anonymous = client.put('/api/self-review', json={'text': 'x'})
        outside = client.put('/api/self-review', json={'text': 'x', 'dm': ['someone@example.org']}, headers=AUTH)
        listed = client.get('/api/self-review').json()

    # then
    assert (first.status_code, second.status_code, anonymous.status_code, outside.status_code) == (200, 200, 401, 422)
    assert second.json()['review']['dm'] == ['oscar.green@example.com'] and second.json()['failed'] == ['no.person@example.com']
    assert [r['text'] for r in listed['reviews']] == ['- 3 cards closed with nobody looking\n- **integrity** again', 'Yesterday was fine.']
    [dm] = [c for _, c in t.calls if c.get('channel') == 'U7']
    assert dm['text'].startswith('*Skopax self-review · ') and '- 3 cards closed with nobody looking' in dm['text'] and dm['text'].endswith('#t=review|Open in Skopax>')


def test_a_new_post_whose_channel_name_no_longer_resolves_goes_to_the_channel_of_the_last_post():
    # given
    calls = []

    def transport(token, payload):
        calls.append(payload['channel'])
        if payload['channel'] == '#ops':
            raise SlackError('channel_not_found')
        return {'ok': True, 'channel': payload['channel'], 'ts': '9.9'}

    s = Slack('xoxb-test', '#ops', transport, known_channel=lambda: 'C0LAST')

    # when
    first = s.post({'text': 'x'})
    second = s.post({'text': 'y'})
    reply = s.post({'channel': 'C0OTHER', 'thread_ts': '1.1', 'text': 'z'})

    # then
    assert first == {'channel': 'C0LAST', 'ts': '9.9'}
    assert calls == ['#ops', 'C0LAST', 'C0LAST', 'C0OTHER'], 'after one miss new posts go straight to the id; a thread reply keeps its own channel'


def test_other_slack_errors_and_a_missing_last_channel_still_fail():
    # given
    def transport(token, payload):
        raise SlackError('not_in_channel' if payload['channel'] == '#a' else 'channel_not_found')

    # then
    with pytest.raises(SlackError, match='not_in_channel'):
        Slack('xoxb-test', '#a', transport, known_channel=lambda: 'C0LAST').post({'text': 'x'})
    with pytest.raises(SlackError, match='channel_not_found'):
        Slack('xoxb-test', '#b', transport).post({'text': 'x'})


def test_a_draft_headline_leads_the_body_and_the_notification():
    # given
    card = {'key': 'restarts|store/catalog-service', 'svc': 'catalog-service', 'label': 'container restarts', 'sev': 'crit', 'status': 'cleared', 'num': 172,
            'slackDraft': {'text': 'The new pod went down 5 times.', 'headline': 'After the release: catalog-service failed to start'}}

    # when
    with_headline = anomaly_message(card, 'https://skopax.example.com/c/172', 'server', 'The new pod went down 5 times.')
    plain = anomaly_message(card | {'slackDraft': {'text': 'x'}}, 'https://skopax.example.com/c/172', 'server', 'x')

    # then
    assert with_headline['blocks'][2]['text']['text'] == '*After the release: catalog-service failed to start*\nThe new pod went down 5 times.'
    assert with_headline['text'] == 'After the release: catalog-service failed to start\nThe new pod went down 5 times.', 'the notification opens with the headline'
    assert [m['blocks'][0]['text']['text'] for m in (with_headline, plain)] == [':large_yellow_circle: Skopax opened a card \u00b7 Critical'] * 2, 'the who-did-what line stays the header'
    assert plain['blocks'][1]['text']['text'] == '*<https://skopax.example.com/c/172|#172 catalog-service / container restarts>*' and plain['blocks'][2]['text']['text'] == 'x'


def test_the_channel_is_read_on_every_post_and_a_new_one_drops_the_old_fallback():
    # given
    calls, now = [], {'channel': '#ops'}

    def transport(token, payload):
        calls.append(payload['channel'])
        if payload['channel'] == '#ops':
            raise SlackError('channel_not_found')
        return {'ok': True, 'channel': payload['channel'], 'ts': '9.9'}

    s = Slack('xoxb-test', lambda: now['channel'], transport, known_channel=lambda: 'C0LAST')

    # when
    s.post({'text': 'x'})
    now['channel'] = 'C0NEW00001'
    moved = s.post({'text': 'y'})

    # then
    assert calls == ['#ops', 'C0LAST', 'C0NEW00001'], 'a channel set on Admin is used from the next post on'
    assert moved == {'channel': 'C0NEW00001', 'ts': '9.9'} and s.channel == 'C0NEW00001'


def test_a_channel_is_checked_with_slack_before_it_is_saved():
    # given
    answers = {'C0OK000001': {'name': 'ops', 'is_member': True}, 'C0OLD00001': {'name': 'old-ops', 'is_archived': True},
               'C0OUT00001': {'name': 'general', 'is_member': False}}

    def api(token, method, payload):
        assert method == 'conversations.info'
        if payload['channel'] not in answers:
            raise SlackError('channel_not_found')
        return {'ok': True, 'channel': {'id': payload['channel'], **answers[payload['channel']]}}

    s = Slack('xoxb-test', '#ops', api=api)

    # when
    checks = {c: s.check_channel(c) for c in ('C0OK000001', '#ops', '', 'C0GONE0001', 'C0OLD00001', 'C0OUT00001')}

    # then
    assert checks['C0OK000001'] is None
    assert checks['#ops'].startswith('give the channel id') and checks[''].startswith('give the channel id'), 'a name can stop resolving, so only an id is taken'
    assert checks['C0GONE0001'] == 'Slack does not know that channel (channel_not_found)'
    assert checks['C0OLD00001'] == '#old-ops is archived'
    assert checks['C0OUT00001'] == 'the Skopax bot is not in #general: invite it there first'
    assert Slack('', '#ops', api=api).check_channel('C0ANY00001') is None, 'without a bot token there is nothing to ask'


def test_an_admin_sets_the_channel_on_the_page_only_after_slack_knows_it(store: Store, tmp_path):
    # given
    from skopax.kube import Kube

    def api(token, method, payload):
        if payload.get('channel') != 'C0CHAN0001':
            raise SlackError('channel_not_found')
        return {'ok': True, 'channel': {'id': payload['channel'], 'name': 'ops', 'is_member': True}}

    store.save({**state(), 'initialized': True}, None)

    # when
    with TestClient(create_app(settings(), store, slack=Slack('xoxb-test', '#ops', api=api), kube=Kube(root=tmp_path))) as c:
        wrong = c.put('/api/admin/settings', json={'key': 'slack.channel', 'value': 'C0NOPE0001'}, headers=AUTH)
        named = c.put('/api/admin/settings', json={'key': 'slack.channel', 'value': '#ops'}, headers=AUTH)
        saved = c.put('/api/admin/settings', json={'key': 'slack.channel', 'value': ' C0CHAN0001 '}, headers=AUTH)
        items = {i['key']: i for i in c.get('/api/admin/settings', headers=AUTH).json()['items']}
        reset = c.delete('/api/admin/settings', params={'key': 'slack.channel'}, headers=AUTH)

    # then
    assert (wrong.status_code, wrong.json()['detail']) == (422, 'Channel: Slack does not know that channel (channel_not_found)')
    assert named.status_code == 422
    assert saved.status_code == 200 and saved.json()['change']['after'] == 'C0CHAN0001'
    assert items['slack.channel']['live'] is True
    assert reset.status_code == 200 and reset.json()['change']['after'] == '#skopax', 'Reset goes back to the config value'


def test_same_person_matches_names_whose_letters_fold_to_plain_spellings():
    # then
    assert same_person('Lukasz Sorensen', '\u0141ukasz S\u00f8rensen') is True
    assert same_person('\u00de\u00f3ra Stra\u00dfer', 'Thora Strasser') is True
    assert same_person('Hakon Aeberg', 'Ha\u0301kon \u00c6berg') is True


class SlackGone(FakeSlack):
    def __call__(self, token: str, payload: dict) -> dict:
        self.calls.append((token, payload))
        raise SlackDown('Slack did not answer (refused by 10.0.0.7)')


def test_a_failed_send_without_a_slack_code_records_and_reports_the_error_type_only(seeded: Store):
    # given
    transport = SlackGone()

    # when
    with client_with(seeded, transport) as client:
        res = client.post('/api/slack-posts', json={'key': KEY}, headers=AUTH)
        listed = client.get('/api/actions').json()

    # then
    assert (res.status_code, res.json()['detail']) == (502, 'Slack refused the message: SlackDown')
    assert listed[0]['result'] == 'not sent: SlackDown' and '10.0.0.7' not in str(listed)


def test_slack_transport_failures_name_the_error_type_only(monkeypatch):
    # given
    def refuse(req, timeout=None):
        raise OSError('connection reset by 10.0.0.7')

    monkeypatch.setattr(urllib.request, 'urlopen', refuse)

    # when
    with pytest.raises(SlackDown) as down:
        api('xoxb-t', 'chat.postMessage', {'channel': 'C1', 'text': 'x'})
    with pytest.raises(SlackError) as upload_failed:
        upload('https://files.example.com/upload', b'x')

    # then
    assert str(down.value) == 'Slack did not answer (OSError)'
    assert str(upload_failed.value) == 'the file upload failed (OSError)'


@pytest.mark.parametrize('configured, shown', [('https://skopax.example.com', 'https://skopax.example.com'), ('', 'https://skopax.dev.example.com')],
                         ids=['configured base', 'no base set'])
def test_a_card_link_uses_the_configured_base_and_the_request_host_only_without_one(seeded: Store, configured, shown):
    # given
    transport = FakeSlack()

    # when
    with client_with(seeded, transport, base_url=configured, sso_redirect_uri='') as client:
        res = client.post('/api/slack-posts', json={'key': KEY}, headers={**AUTH, 'Host': 'localhost:8080'} if configured else AUTH)

    # then
    assert res.status_code == 201
    text = transport.calls[0][1]['blocks'][1]['text']['text']
    assert text.startswith(f'*<{shown}/#a=log%7Cweb-bff%7CWARN')
