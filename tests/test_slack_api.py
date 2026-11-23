import json
import time
import urllib.error
import urllib.request

import pytest

from tests.fake_slack import fake_slack
from skopax.redact import REDACTED, redact
from skopax.slack import CARD_ACTION, CREATE_TASK, Slack, SlackDown, SlackError, answer_blocks, api, card_action_label, reason_of


class Resp:
    def __init__(self, body: dict):
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self) -> bytes:
        return json.dumps(self.body).encode()


def test_api_reads_with_get_and_writes_with_post(monkeypatch):
    # given
    seen = []
    monkeypatch.setattr(urllib.request, 'urlopen', lambda req, timeout: seen.append(req) or Resp({'ok': True}))

    # when
    api('xoxb-t', 'users.info', {'user': 'U1'})
    api('xoxb-t', 'chat.update', {'channel': 'C1', 'ts': '1.2', 'text': 'x'})

    # then
    assert seen[0].get_method() == 'GET' and seen[0].full_url == 'https://slack.com/api/users.info?user=U1'
    assert seen[0].get_header('Authorization') == 'Bearer xoxb-t'
    assert seen[1].get_method() == 'POST' and seen[1].full_url == 'https://slack.com/api/chat.update'
    assert json.loads(seen[1].data) == {'channel': 'C1', 'ts': '1.2', 'text': 'x'}


def test_api_raises_when_slack_says_not_ok(monkeypatch):
    # given
    monkeypatch.setattr(urllib.request, 'urlopen', lambda req, timeout: Resp({'ok': False, 'error': 'message_not_found'}))

    # when
    with pytest.raises(SlackError) as e:
        api('xoxb-t', 'chat.update', {'channel': 'C1', 'ts': '1.2', 'text': 'x'})

    # then
    assert str(e.value) == 'message_not_found' and e.value.code == 'message_not_found'


def test_update_ephemeral_and_permalink_drop_empty_fields():
    # given
    slack, _, calls = fake_slack()

    # when
    slack.update('C1', '1.2', 'hi')
    slack.ephemeral('C1', 'U1', 'only you', None)
    link = slack.permalink('C1', '1792224000.000200')

    # then
    assert calls.calls[0] == ('chat.update', {'channel': 'C1', 'ts': '1.2', 'text': 'hi'})
    assert calls.calls[1] == ('chat.postEphemeral', {'channel': 'C1', 'user': 'U1', 'text': 'only you'})
    assert link == 'https://example.slack.com/archives/C1/p1792224000000200'


def test_permalink_is_none_when_slack_fails():
    # given
    slack, _, _ = fake_slack(fail={'chat.getPermalink'})

    # then
    assert slack.permalink('C1', '1.2') is None


def test_user_email_is_trimmed_and_none_without_one():
    # given
    slack, _, _ = fake_slack(emails={'U1': ' Adam.Baker@Example.com '})

    # then
    assert slack.user_email('U1') == 'Adam.Baker@Example.com'
    assert slack.user_email('U2') is None


@pytest.mark.parametrize('user', [{'id': 'U3', 'is_bot': True, 'profile': {'email': 'bot@example.com'}}, {'id': 'U4', 'deleted': True, 'profile': {'email': 'x@example.com'}},
                                  {'id': 'USLACKBOT', 'profile': {'email': 'slackbot@example.com'}}])
def test_bots_deleted_users_and_slackbot_have_no_email(user):
    # given
    slack = Slack('xoxb-test', '#ops', api=lambda token, method, payload: {'ok': True, 'user': user})

    # then
    assert slack.user_email(user['id']) is None


def test_user_email_raises_when_users_info_fails():
    # given
    slack, _, _ = fake_slack(fail={'users.info'})

    # then
    with pytest.raises(SlackError):
        slack.user_email('U1')


def test_replies_keep_the_last_messages_and_ask_slack_for_a_full_page():
    # given
    slack, _, calls = fake_slack(replies=[{'ts': str(i), 'text': f'm{i}'} for i in range(15)])

    # when
    last = slack.replies('C1', '0', 10)

    # then
    assert [m['text'] for m in last] == [f'm{i}' for i in range(5, 15)]
    assert calls.calls[0] == ('conversations.replies', {'channel': 'C1', 'ts': '0', 'limit': 200})


def test_bot_user_is_the_user_id_and_is_read_once():
    # given
    slack, _, calls = fake_slack()

    # when
    first, second = slack.bot_user(), slack.bot_user()

    # then
    assert first == second == 'UBOT'
    assert calls.methods() == ['auth.test']


@pytest.mark.parametrize('raw', ['mail adam.baker@example.com now', 'tel +44 7700 900123', 'tel +1-202-555-0143', 'tel +4915112345678',
                                 'iban DE89 3704 0044 0532 0130 00', 'iban GB82WEST12345698765432', 'iban fr14 2004 1010 0505 0001 3m02 606',
                                 'card 4111 1111 1111 1111', 'auth Bearer abcdefghijklmnop', 'jwt eyJhbGciOi.eyJzdWIiOi.c2lnbmF0dXJl',
                                 'id 5 4111111111111111', '2026-10-24 4111111111111111', 'no 12 4111 1111 1111 1111',
                                 'id 10000000146', 'tel 0532 123 45 67'])
def test_redact_hides_personal_data_and_tokens(raw):
    # then
    assert REDACTED in redact(raw)


@pytest.mark.parametrize('raw', ['p95 1240 ms at 14:05', 'epoch 1792224000000', 'trace 4bf92f3577b34da6a3ce929d0e0e4736', 'order 12345678901', 'OPS-125 is live',
                                 'span ab12cdef34567890ab12', 'offset +01:00 at 2026-10-24T10:00:00+01:00', 'p99 +250 ms', 'id 10000000147'])
def test_redact_keeps_numbers_that_are_not_personal(raw):
    # then
    assert redact(raw) == raw


@pytest.mark.parametrize('raw, expected', [
    ('AT61 1904 3002 3457 3201 EUR', '[redacted] EUR'),
    ('ES91 2100 0418 4502 0005 1332 EUR', '[redacted] EUR'),
    ('BE68 5390 0754 7034 2026 rent', '[redacted] 2026 rent'),
    ('pay DE89 3704 0044 0532 0130 00 to us, then BE68 5390 0754 7034 next week', 'pay [redacted] to us, then [redacted] next week'),
    ('AB12 CDEF GHIJ KLMN stays', 'AB12 CDEF GHIJ KLMN stays'),
    ('BE68 5390 0754 7034 DE89 3704 0044 0532 0130 00', '[redacted] [redacted]'),
    ('ref AB12 DE89 3704 0044 0532 0130 00', 'ref AB12 [redacted]'),
    ('code XY99 GB82 WEST 1234 5698 7654 32 ok', 'code XY99 [redacted] ok'),
    ('tel +1 (202) 555-0143 today', 'tel [redacted] today'),
    ('call +44 (0)20 7946 0958 or +49 151 1234 5678', 'call [redacted] or [redacted]'),
    ('up +12 (3 h) since 14:05, +250 ms', 'up +12 (3 h) since 14:05, +250 ms'),
])
def test_redact_cuts_an_iban_or_a_phone_number_out_of_the_text_around_it(raw, expected):
    # then
    assert redact(raw) == expected


def test_redact_stays_linear_on_a_long_run_of_iban_shaped_groups():
    # given
    raw = ' '.join(['AB12'] + ['CDEF'] * 20000)

    # when
    started = time.perf_counter()
    out = redact(raw)

    # then
    assert time.perf_counter() - started < 1.0 and out == raw


def test_answer_blocks_carry_the_draft_and_the_button_only_with_a_proposal():
    # given
    proposal = {'key': 'k', 'title': 'Fix <null> guard', 'description': 'line one\n\nline two\nline three\nline four', 'points': 3}

    # when
    plain = answer_blocks('answer', None, 'a1')
    drafted = answer_blocks('answer', proposal, 'a1')

    # then
    assert plain == [{'type': 'section', 'text': {'type': 'mrkdwn', 'text': 'answer'}}]
    assert drafted[1]['text']['text'] == '*Task draft:* Fix &lt;null&gt; guard\n> line one\n> line two\n> line three'
    assert drafted[2] == {'type': 'actions', 'elements': [{'type': 'button', 'action_id': CREATE_TASK, 'value': 'a1', 'style': 'primary', 'text': {'type': 'plain_text', 'text': 'Create task'}}]}


def test_answer_blocks_carry_the_card_action_line_and_button_beside_the_create_task_button():
    # given
    proposal = {'key': 'k', 'title': 'Fix', 'description': 'd', 'points': 3}
    card = {'svc': 'cart<service>', 'label': 'ERROR pattern'}

    # when
    only_action = answer_blocks('answer', None, 'a1', {'key': 'k', 'action': 'mute', 'hours': 48}, card)
    both = answer_blocks('answer', proposal, 'a1', {'key': 'k', 'action': 'known'}, card)

    # then
    assert only_action[1]['text']['text'] == '*Suggested action:* Mute 48 h · cart&lt;service&gt; ERROR pattern'
    assert only_action[2] == {'type': 'actions', 'elements': [{'type': 'button', 'action_id': CARD_ACTION, 'value': 'a1', 'text': {'type': 'plain_text', 'text': 'Mute 48 h'}, 'style': 'primary'}]}, 'the suggested action is green like the other offers'
    assert [b['type'] for b in both] == ['section', 'section', 'section', 'actions']
    assert [e['action_id'] for e in both[3]['elements']] == [CREATE_TASK, CARD_ACTION]
    assert both[3]['elements'][1]['text']['text'] == 'Known noise'


@pytest.mark.parametrize('action, label', [('resolve', 'Resolve'), ('take', 'Take'), ('known', 'Known noise'), ('mute', 'Mute 24 h')])
def test_card_action_labels(action, label):
    # then
    assert card_action_label({'key': 'k', 'action': action}) == label


def test_the_suggested_action_names_an_alert_card_by_number_without_its_dash_service_and_known_noise_stays_plain():
    # given
    alert = {'svc': '-', 'label': 'KubePodNotReady', 'num': 153}

    # when
    resolve = answer_blocks('answer', None, 'a1', {'key': 'k', 'action': 'resolve'}, alert)
    known = answer_blocks('answer', None, 'a1', {'key': 'k', 'action': 'known'}, alert)

    # then
    assert resolve[1]['text']['text'] == '*Suggested action:* Resolve · #153 KubePodNotReady'
    assert resolve[2]['elements'][0]['style'] == 'primary'
    assert 'style' not in known[2]['elements'][0] and known[2]['elements'][0]['confirm'], 'Known noise keeps its confirm and no green'


def test_a_status_keeps_the_slack_error_code_and_otherwise_only_the_error_type():
    # then
    assert reason_of(SlackError('channel_not_found', code='channel_not_found')) == 'channel_not_found'
    assert reason_of(SlackError('Slack did not answer (refused by 10.0.0.7)')) == 'SlackError'
    assert reason_of(RuntimeError('db password hunter2')) == 'RuntimeError'


def test_an_http_429_is_a_slack_that_did_not_answer_carrying_how_long_to_wait(monkeypatch):
    # given
    def limited(req, timeout):
        raise urllib.error.HTTPError(req.full_url, 429, 'Too Many Requests', {'Retry-After': '7'}, None)

    monkeypatch.setattr(urllib.request, 'urlopen', limited)

    # when
    with pytest.raises(SlackDown) as caught:
        api('xoxb-test', 'users.list', {'limit': 200})

    # then
    assert caught.value.code == 'ratelimited' and caught.value.wait == 7.0
