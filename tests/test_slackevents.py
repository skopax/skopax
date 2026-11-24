import dataclasses
import datetime as dt
import json
import logging
from types import SimpleNamespace

import pytest

from tests.conftest import anomaly, settings, state
from tests.fake_anthropic import FakeAnthropic, reply, text, usage, verdict
from tests.fake_slack import fake_slack
from tests.gate_examples import GROUPS
from tests.plugin_kit import JIRA_SITE
from skopax import brand
from skopax.engine import iso
from skopax.investigator import prompt
from skopax.investigator.gate import Gate
from skopax.investigator.smalltalk import SmallTalk
from skopax.operators import Operators
from skopax.messages import msg
from skopax.redact import REDACTED
from skopax.slack import CARD_ACTION, CREATE_TASK, SlackError
from skopax.slackevents import GATE_HOURLY_CAP, Intake, Replier, bot_tagged, decode, named, other_mentions, question_text, thread_view
from skopax.store import NotFound, Store

OFF_TOPIC = msg('slackevents.off_topic')
STOPPED = msg('slackevents.stopped')
CAPPED = msg('slackevents.capped')
ASK_CAPPED = msg('slackevents.ask_capped')
BUSY = msg('slackevents.busy')
EMPTY_TAG = msg('slackevents.empty_tag')
SMALL_TALK_CAPPED = msg('slackevents.small_talk_capped')
PLACEHOLDER = msg('slackevents.placeholder')
REPLY_CAPPED = msg('slackevents.reply.capped')
REPLY_STOPPED = msg('slackevents.reply.stopped')
REPLY_FAILED = msg('slackevents.reply.failed')
NOW = 1_792_224_000.0
AT = dt.datetime.fromtimestamp(NOW, dt.timezone.utc)
KEY = 'log|a|ERROR|x'
CARD_TS = '1792224000.000100'
ASK_TS = '1792224000.000200'
TOP_TS = '1792224000.000500'
THREAD = [{'ts': CARD_TS, 'bot_id': 'B0BOT', 'text': 'checkout-service ERROR 40'}, {'ts': '1792224000.000150', 'user': 'U2', 'text': 'looking into it'},
          {'ts': ASK_TS, 'user': 'U1', 'text': '<@UBOT> why did it go up?'}]


class FakeInvestigator:
    def __init__(self, enabled: bool = True, halted: str | None = None):
        self.enabled = enabled
        self.halted = halted
        self.kicks: list[float] = []

    def why_halted(self, now: float) -> str | None:
        return self.halted

    def kick(self, now: float) -> None:
        self.kicks.append(now)


def intake(store: Store, listen: str = 'live', emails=None, gate_replies=None, fail=None, investigator=None, ask_cap: int = 10, members=(), ask_channels: str = 'all', user_cap: int = 4):
    slack, post, api = fake_slack(emails={'U1': 'adam.baker@example.com'} if emails is None else emails, replies=THREAD, fail=fail)
    s = dataclasses.replace(settings(), anthropic_api_key='sk-test', slack_listen=listen, slack_ask_cap=ask_cap, slack_ask_channels=ask_channels, slack_user_ask_cap=user_cap)
    fake = FakeAnthropic([], gate_replies or [])
    inv = investigator or FakeInvestigator()
    roles = lambda email: email in {'adam.baker@example.com', *members}
    box = Intake(store, slack, Operators(roles, slack, s, lambda: NOW), Gate(store, s, client=fake, clock=lambda: NOW), SmallTalk(store, s, client=fake, clock=lambda: NOW), inv, s,
                 clock=lambda: NOW, base_url='https://skopax.example.com', jira_site=lambda: JIRA_SITE)
    return box, SimpleNamespace(post=post, api=api, gate=fake, inv=inv)


def card_with_post(store: Store, at: str | None = None) -> None:
    store.save(state(anomaly(KEY, firstSeen=iso(NOW - 900))), None)
    store.slack_posted(KEY, {'channel': 'C1', 'ts': CARD_TS, 'at': at or iso(NOW - 600), 'by': 'server'})


def reply_event(text: str = '<@UBOT> why did it go up?', ts: str = ASK_TS, user: str = 'U1', kind: str = 'message', **over) -> dict:
    return {'type': kind, 'channel': 'C1', 'user': user, 'text': text, 'ts': ts, 'thread_ts': CARD_TS, **over}


def top_event(text: str = '<@UBOT> why is checkout-service slow?', ts: str = TOP_TS, channel: str = 'C1', **over) -> dict:
    return {'type': 'app_mention', 'channel': channel, 'user': 'U1', 'text': text, 'ts': ts, **over}


def slack_ask(store: Store, channel: str = 'C1', thread_ts: str = CARD_TS, status: str = 'requested') -> dict:
    return store.add_action({'type': 'ask', 'source': 'slack', 'key': 'ask', 'target': f'slack:{channel}:{thread_ts}', 'text': 'q', 'cards': [],
                             'slack': {'where': 'card', 'channel': channel, 'threadTs': thread_ts, 'ts': thread_ts, 'user': 'U1'}}, status)


def test_mentions_and_slack_encoding_are_read_from_the_text():
    # given
    raw = '<@UBOT> <@U2> look &lt;b&gt; &amp; <https://x.example.com/a|panel> <https://y.example.com> <#C123|skopax>'

    # then
    assert bot_tagged(raw, 'UBOT') and not bot_tagged('<@U2> hi', 'UBOT')
    assert other_mentions(raw, 'UBOT') and not other_mentions('<@UBOT> hi', 'UBOT')
    assert decode(raw, 'UBOT') == '@U2 look <b> & panel (https://x.example.com/a) https://y.example.com #skopax'
    assert question_text('<@UBOT>  ', 'UBOT') == ''
    assert len(question_text('<@UBOT> ' + 'x' * 3000, 'UBOT')) == 2000


def test_the_thread_view_marks_bots_and_drops_the_asked_message():
    # when
    view = thread_view(THREAD, 'UBOT', ASK_TS)

    # then
    assert view == [{'who': 'bot', 'text': 'checkout-service ERROR 40'}, {'who': 'person', 'text': 'looking into it'}]


def test_a_tagged_card_thread_reply_from_an_operator_becomes_an_ask(store: Store):
    # given
    card_with_post(store)
    box, f = intake(store, gate_replies=[verdict()])

    # when
    decision = box.handle(reply_event())

    # then
    assert decision == 'accepted'
    assert f.post.calls == [{'channel': 'C1', 'thread_ts': CARD_TS, 'text': PLACEHOLDER, 'unfurl_links': False, 'unfurl_media': False}]
    action = store.list_actions('requested', 5)[0]
    assert (action['source'], action['cards'], action['text'], action['requestedBy'], action['label']) == ('slack', [KEY], 'why did it go up?', 'adam.baker@example.com', 'Slack: why did it go up?')
    assert action['slack'] == {'where': 'card', 'channel': 'C1', 'threadTs': CARD_TS, 'ts': ASK_TS, 'user': 'U1', 'language': 'nl',
                               'permalink': 'https://example.slack.com/archives/C1/p1792224000000200', 'operator': True, 'placeholderTs': '1792224000.000001'}
    assert action['thread'] == [{'who': 'bot', 'text': 'checkout-service ERROR 40'}, {'who': 'person', 'text': 'looking into it'}]
    assert f.inv.kicks == [NOW]
    assert f.gate.gate_requests[0]['messages'][0]['content'].startswith('Card: checkout-service / ERROR pattern')
    record = store.list_intake(1)[0]
    assert (record['decision'], record['actionId'], record['tagged'], record['gate']['onTopic']) == ('accepted', action['id'], True, True)


def test_a_tagged_resend_request_in_a_card_thread_tells_the_gate_it_tags_the_bot_and_becomes_an_ask(store: Store):
    # given
    card_with_post(store)
    box, f = intake(store, gate_replies=[verdict(addressed=True, on_topic=True, reason='asks the bot to send its image again')])

    # when
    got = box.handle(reply_event('<@UBOT> the image did not come through, send it again'))

    # then
    assert got == 'accepted'
    content = f.gate.gate_requests[0]['messages'][0]['content']
    assert content.endswith(f'person: looking into it\n</thread>\n\n{brand.fill(prompt.TAGGED)}\n\n<message>\nthe image did not come through, send it again\n</message>')
    assert store.list_actions('requested', 5)[0]['text'] == 'the image did not come through, send it again'
    record = store.list_intake(1)[0]
    assert (record['tagged'], record['decision'], record['gate']['addressed']) == (True, 'accepted', True)


def test_an_untagged_reply_is_an_ask_only_when_the_gate_says_it_is_addressed(store: Store):
    # given
    card_with_post(store)
    box, f = intake(store, gate_replies=[verdict(addressed=True), verdict(addressed=False)])

    # when
    asked = box.handle(reply_event('why did it go up?'))
    store.update_action(store.list_actions('requested', 1)[0]['id'], 'done', 'x', None)
    chatter = box.handle(reply_event('ok', ts='1792224000.000300'))

    # then
    assert (asked, chatter) == ('accepted', 'not-addressed')
    assert len(f.post.calls) == 1


def test_untagged_talk_between_people_never_reaches_slack_or_the_gate(store: Store):
    # given
    card_with_post(store)
    box, f = intake(store)

    # when
    decision = box.handle(reply_event('<@U2> can you take a look?'))

    # then
    assert decision is None
    assert f.gate.gate_requests == [] and f.api.methods() == ['auth.test'] and store.list_intake(5) == []


@pytest.mark.parametrize('event', [
    reply_event(subtype='message_changed'),
    reply_event(bot_id='B0BOT'),
    reply_event(user='UBOT'),
    {**top_event('why is checkout-service slow?'), 'type': 'message'},
    reply_event('something', thread_ts='1792224000.000999'),
    {'type': 'reaction_added', 'channel': 'C1', 'user': 'U1', 'ts': ASK_TS},
])
def test_messages_that_are_not_ours_end_without_a_trace(store: Store, event):
    # given
    card_with_post(store)
    box, f = intake(store)

    # when
    decision = box.handle(event)

    # then
    assert decision is None
    assert f.post.calls == [] and f.gate.gate_requests == [] and store.list_intake(5) == []


def test_a_duplicate_and_an_untagged_empty_message_end_early(store: Store):
    # given
    card_with_post(store)
    box, f = intake(store, gate_replies=[verdict()])

    # when
    empty = box.handle(reply_event('', ts='1792224000.000300'))
    first = box.handle(reply_event(kind='app_mention'))
    again = box.handle(reply_event())

    # then
    assert (empty, first, again) == ('empty', 'accepted', None)
    assert len(store.list_actions(None, 5)) == 1


def test_the_age_limit_stops_untagged_replies_under_an_old_card_post_but_not_tagged_asks(store: Store):
    # given
    card_with_post(store, at=iso(NOW - 8 * 86400))
    box, f = intake(store, gate_replies=[verdict()])

    # when
    untagged = box.handle(reply_event('why did it go up?'))
    tagged = box.handle(reply_event(ts='1792224000.000300'))

    # then
    assert (untagged, tagged) == (None, 'accepted')
    assert len(store.list_actions(None, 5)) == 1


def test_anyone_in_the_workspace_can_ask_and_is_named_by_email_but_gets_no_card_buttons(store: Store):
    # given
    card_with_post(store)
    box, f = intake(store, emails={'U9': 'x@example.com'}, gate_replies=[verdict()])

    # when
    tagged = box.handle(reply_event(user='U9'))

    # then
    assert tagged == 'accepted' and 'chat.postEphemeral' not in f.api.methods()
    action = store.list_actions('requested', 1)[0]
    assert (action['requestedBy'], action['slack']['operator']) == ('x@example.com', False)
    assert store.list_intake(1)[0]['operator'] is False


def test_a_person_slack_cannot_name_still_asks_under_their_slack_id(store: Store):
    # given
    card_with_post(store)
    box, _ = intake(store, fail={'users.info'}, gate_replies=[verdict()])

    # when
    decision = box.handle(reply_event())

    # then
    assert decision == 'accepted' and store.list_actions('requested', 1)[0]['requestedBy'] == 'slack:U1'


def test_someone_from_another_company_in_a_shared_channel_is_ignored(store: Store):
    # given
    card_with_post(store)
    box, f = intake(store)

    # when
    decision = box.handle(top_event(channel='C2', team='T1', user_team='T9'))

    # then
    assert decision == 'external' and f.post.calls == [] and f.gate.gate_requests == []


@pytest.mark.parametrize('setup, decision, line', [
    ('stopped', 'stopped', STOPPED),
    ('capped', 'capped', CAPPED),
    ('ask-cap', 'ask-cap', ASK_CAPPED),
    ('off', 'not-ready', msg('chat.not_ready')),
])
@pytest.mark.parametrize('tagged', [True, False])
def test_the_decision_table_around_the_gate(store: Store, setup, decision, line, tagged):
    # given
    card_with_post(store)
    inv = FakeInvestigator(enabled=setup != 'off', halted=setup if setup in ('stopped', 'capped') else None)
    held = setup == 'ask-cap'
    box, f = intake(store, investigator=inv, gate_replies=[verdict()] if held else None)
    if setup == 'ask-cap':
        for i in range(10):
            slack_ask(store, thread_ts=f'1792224000.0009{i:02d}')

    # when
    got = box.handle(reply_event() if tagged else reply_event('why did it go up?'))

    # then
    assert got == (decision if tagged or setup != 'off' else 'off')
    assert [p['text'] for p in f.post.calls] == ([line] if tagged and line else [])
    assert len(f.gate.gate_requests) == (1 if held else 0) and f.inv.kicks == []


@pytest.mark.parametrize('tagged, decision', [(True, 'accepted'), (False, 'gate-cap')])
def test_the_gate_cap_skips_the_gate_and_lets_only_tagged_asks_through(store: Store, tagged, decision):
    # given
    card_with_post(store)
    for i in range(GATE_HOURLY_CAP):
        store.seen('C9', str(i), AT)
        store.record_intake('C9', str(i), {'gate': {'onTopic': True}})
    box, f = intake(store)

    # when
    got = box.handle(reply_event() if tagged else reply_event('why did it go up?'))

    # then
    assert got == decision and f.gate.gate_requests == []


@pytest.mark.parametrize('tagged, decision', [(True, 'accepted'), (False, 'gate-down')])
def test_a_gate_down_runs_tagged_asks_and_drops_untagged_ones(store: Store, tagged, decision):
    # given
    card_with_post(store)
    box, f = intake(store, gate_replies=[])

    # when
    got = box.handle(reply_event() if tagged else reply_event('why did it go up?'))

    # then
    assert got == decision
    assert 'down' in store.list_intake(1)[0]['gate']


def test_a_gate_down_is_recorded_as_its_reason_code_and_a_fixed_text_never_the_error_text(store: Store):
    # given
    card_with_post(store)
    secret = 'sk-ant-fake-0042-secret'
    box, f = intake(store, gate_replies=[RuntimeError(f'upstream refused the key {secret}')])

    # when
    got = box.handle(reply_event('why did it go up?'))

    # then
    record = store.list_intake(1)[0]
    assert got == 'gate-down'
    assert record['gate'] == {'down': msg('page.gate_down'), 'error': 'api:RuntimeError'}
    assert secret not in json.dumps(record)


@pytest.mark.parametrize('language, line', [('en', OFF_TOPIC), ('nl', OFF_TOPIC), ('other', OFF_TOPIC)])
def test_an_off_topic_ask_gets_the_english_line_when_no_pack_has_its_language(store: Store, language, line):
    # given
    card_with_post(store)
    box, f = intake(store, gate_replies=[verdict(on_topic=False, language=language)])

    # when
    got = box.handle(reply_event('<@UBOT> write me a poem'))

    # then
    assert got == 'off-topic'
    assert [(p['thread_ts'], p['text']) for p in f.post.calls] == [(CARD_TS, line)]
    assert store.list_actions(None, 5) == []


def test_an_untagged_addressed_but_off_topic_reply_gets_the_off_topic_line(store: Store):
    # given
    card_with_post(store)
    box, f = intake(store, gate_replies=[verdict(addressed=True, on_topic=False, language='en')])

    # when
    got = box.handle(reply_event('could you write me a poem?'))

    # then
    assert got == 'off-topic'
    assert [(p['thread_ts'], p['text']) for p in f.post.calls] == [(CARD_TS, OFF_TOPIC)]
    assert store.list_actions(None, 5) == []


def test_shadow_mode_posts_nothing_and_stores_what_it_would_have_said(store: Store):
    # given
    card_with_post(store)
    box, f = intake(store, listen='shadow', emails={'U1': 'adam.baker@example.com', 'U9': 'x@example.com'}, gate_replies=[verdict(), verdict(on_topic=False), verdict()])

    # when
    accepted = box.handle(reply_event())
    store.update_action(store.list_actions('requested', 1)[0]['id'], 'done', 'x', None)
    off = box.handle(reply_event('<@UBOT> write a poem', ts='1792224000.000300'))
    outsider = box.handle(reply_event(ts='1792224000.000400', user='U9'))

    # then
    assert (accepted, off, outsider) == ('accepted', 'off-topic', 'accepted')
    assert f.post.calls == [] and 'chat.postEphemeral' not in f.api.methods()
    assert 'placeholderTs' not in store.list_actions(None, 5)[-1]['slack']
    by_ts = {r['id'].split(':')[1]: r for r in store.list_intake(5)}
    assert by_ts['1792224000.000300']['wouldPost'] == OFF_TOPIC
    assert 'wouldPost' not in by_ts['1792224000.000400']


def test_a_tagged_top_level_message_in_the_card_channel_is_a_channel_ask(store: Store):
    # given
    card_with_post(store)
    box, f = intake(store, gate_replies=[verdict()])

    # when
    decision = box.handle(top_event())

    # then
    assert decision == 'accepted'
    action = store.list_actions('requested', 1)[0]
    assert action['cards'] == [] and action['thread'] == []
    assert (action['slack']['where'], action['slack']['threadTs']) == ('channel', TOP_TS)
    assert f.post.calls[0]['thread_ts'] == TOP_TS
    assert f.gate.gate_requests[0]['messages'][0]['content'].startswith(prompt.NO_CARD)
    assert [p['ts'] for m, p in f.api.calls if m == 'conversations.replies'] == [TOP_TS], 'only the asked message itself is read, for its files'


def test_untagged_or_foreign_top_level_messages_are_dropped_when_asks_stay_in_the_skopax_channel(store: Store, caplog):
    # given
    box, f = intake(store, ask_channels='main')

    # when
    with caplog.at_level(logging.INFO, logger='skopax.slackevents'):
        nothing_posted_yet = box.handle(top_event())
    card_with_post(store)
    untagged = box.handle({**top_event('why is checkout-service slow?'), 'type': 'message'})
    other_channel = box.handle(top_event(channel='C2'))

    # then
    assert (nothing_posted_yet, untagged, other_channel) == (None, None, None)
    assert 'channel ask in C1 dropped: no card post seen yet' in caplog.text
    assert f.post.calls == [] and store.list_intake(5) == []


def test_a_tagged_question_in_any_channel_the_bot_is_in_becomes_a_request_answered_there(store: Store):
    # given
    card_with_post(store)
    box, f = intake(store, gate_replies=[verdict(), verdict()])

    # when
    top = box.handle(top_event(channel='C2'))
    untagged = box.handle({**top_event('why is checkout-service slow?', ts='1792224000.000600', channel='C2'), 'type': 'message'})
    named_only = box.handle({**top_event('skopax why is checkout-service slow?', ts='1792224000.000700', channel='C2'), 'type': 'message'})
    home = box.handle(top_event(ts='1792224000.000900'))

    # then
    assert (top, untagged, named_only, home) == ('accepted', None, None, 'accepted')
    asked = {a['slack']['channel']: a for a in store.list_actions('requested', 5)}
    assert (asked['C2']['slack']['outside'], asked['C2']['slack']['channelName'], asked['C2']['slack']['threadTs']) == (True, 'dev-backend', TOP_TS)
    assert 'outside' not in asked['C1']['slack']
    assert f.post.calls[0]['channel'] == 'C2' and f.post.calls[0]['thread_ts'] == TOP_TS


def test_a_listed_channel_takes_asks_and_any_other_one_is_ignored(store: Store):
    # given
    card_with_post(store)
    box, f = intake(store, gate_replies=[verdict(), verdict()], ask_channels='C3')

    # when
    listed = box.handle(top_event(channel='C3'))
    other = box.handle(top_event(ts='1792224000.000600', channel='C2'))
    home = box.handle(top_event(ts='1792224000.000700'))

    # then
    assert (listed, other, home) == ('accepted', None, 'accepted')


def test_one_person_gets_a_few_asks_an_hour_and_then_a_wait_line(store: Store):
    # given
    card_with_post(store)
    box, f = intake(store, gate_replies=[verdict(), verdict(), verdict()], user_cap=2)

    # when
    first = box.handle(top_event(channel='C2'))
    second = box.handle(top_event(ts='1792224000.000600', channel='C2'))
    third = box.handle(top_event(ts='1792224000.000700', channel='C2'))

    # then
    assert (first, second, third) == ('accepted', 'accepted', 'user-cap')
    assert (f.post.calls[-1]['channel'], f.post.calls[-1]['thread_ts'], f.post.calls[-1]['text']) == ('C2', '1792224000.000700', msg('slackevents.user_capped', cap=2))


FOREIGN_TS = '1792224000.000800'


def thread_event(text: str, ts: str, root: str = TOP_TS, channel: str = 'C1', **over) -> dict:
    return {'type': 'message', 'channel': channel, 'user': 'U1', 'text': text, 'ts': ts, 'thread_ts': root, **over}


def channel_ask_thread(store: Store, box, f) -> None:
    box.handle(top_event())
    store.update_action(store.list_actions('requested', 1)[0]['id'], 'done', 'x', None)


def test_follow_ups_in_a_channel_ask_thread_go_through_the_gate_tagged_or_not(store: Store):
    # given
    card_with_post(store)
    box, f = intake(store, gate_replies=[verdict(), verdict(), verdict(addressed=True), verdict(addressed=False)])
    channel_ask_thread(store, box, f)

    # when
    tagged = box.handle(thread_event('<@UBOT> and yesterday?', '1792224000.000600'))
    store.update_action(store.list_actions('requested', 1)[0]['id'], 'done', 'x', None)
    addressed = box.handle(thread_event('and yesterday?', '1792224000.000700'))
    follow = store.list_actions('requested', 1)[0]
    store.update_action(follow['id'], 'done', 'x', None)
    chatter = box.handle(thread_event('ok', '1792224000.000710'))

    # then
    assert (tagged, addressed, chatter) == ('accepted', 'accepted', 'not-addressed')
    assert (follow['slack']['where'], follow['slack']['threadTs'], follow['cards']) == ('channel', TOP_TS, [])
    assert len(f.gate.gate_requests) == 4


def test_an_untagged_follow_up_that_mentions_someone_else_is_skipped_with_no_record(store: Store):
    # given
    card_with_post(store)
    box, f = intake(store, gate_replies=[verdict()])
    channel_ask_thread(store, box, f)
    before = len(store.list_intake(10))

    # when
    skipped = box.handle(thread_event('<@U2> can you take a look?', '1792224000.000600'))

    # then
    assert skipped is None and len(f.gate.gate_requests) == 1 and len(store.list_intake(10)) == before


def test_a_tagged_reply_in_a_foreign_thread_of_the_skopax_channel_is_a_channel_ask_read_against_the_root(store: Store):
    # given
    card_with_post(store)
    box, f = intake(store, gate_replies=[verdict()])
    f.api.thread = [{'ts': FOREIGN_TS, 'user': 'U2', 'text': 'this gave 500s after the deploy'}, {'ts': ASK_TS, 'user': 'U1', 'text': '<@UBOT> what is this saying'}]

    # when
    decision = box.handle(thread_event('<@UBOT> what is this saying', ASK_TS, root=FOREIGN_TS))

    # then
    assert decision == 'accepted'
    assert f.post.calls[0]['thread_ts'] == FOREIGN_TS
    action = store.list_actions('requested', 1)[0]
    assert (action['slack']['where'], action['slack']['threadTs'], action['cards']) == ('channel', FOREIGN_TS, [])
    assert action['thread'] == [{'who': 'person', 'text': 'this gave 500s after the deploy'}]
    assert 'this gave 500s after the deploy' in f.gate.gate_requests[0]['messages'][0]['content']


def test_a_tagged_reply_in_a_foreign_thread_outside_the_skopax_channel_is_dropped_when_asks_stay_there(store: Store):
    # given
    card_with_post(store)
    box, f = intake(store, ask_channels='main')

    # when
    other = box.handle(thread_event('<@UBOT> what is this?', ASK_TS, root=FOREIGN_TS, channel='C2'))

    # then
    assert other is None and f.post.calls == [] and store.list_intake(5) == []


def test_a_tagged_reply_before_any_skopax_post_is_dropped_when_asks_stay_in_the_skopax_channel(store: Store, caplog):
    # given
    box, f = intake(store, ask_channels='main')

    # when
    with caplog.at_level(logging.INFO, logger='skopax.slackevents'):
        dropped = box.handle(thread_event('<@UBOT> what is this?', ASK_TS, root=FOREIGN_TS))

    # then
    assert dropped is None and 'dropped: no card post seen yet' in caplog.text


def test_untagged_replies_in_a_foreign_thread_are_unread_until_a_tagged_ask_started_it(store: Store):
    # given
    card_with_post(store)
    box, f = intake(store, gate_replies=[verdict(), verdict(addressed=True)])

    # when
    before = box.handle(thread_event('what does this say?', ASK_TS, root=FOREIGN_TS))
    started = box.handle(thread_event('<@UBOT> what does this say?', '1792224000.000300', root=FOREIGN_TS))
    store.update_action(store.list_actions('requested', 1)[0]['id'], 'done', 'x', None)
    after = box.handle(thread_event('and yesterday?', '1792224000.000400', root=FOREIGN_TS))

    # then
    assert (before, started, after) == (None, 'accepted', 'accepted')
    assert len(f.gate.gate_requests) == 2
    assert store.list_actions('requested', 1)[0]['slack']['threadTs'] == FOREIGN_TS


def test_a_tagged_reply_under_a_card_that_left_the_state_is_a_no_card_ask_without_an_age_limit(store: Store):
    # given
    card_with_post(store, at=iso(NOW - 30 * 86400))
    store.save(state(anomaly('log|b|ERROR|y')), None)
    store.slack_posted('log|b|ERROR|y', {'channel': 'C1', 'ts': '1792224000.000050', 'at': iso(NOW - 600), 'by': 'server'})
    store.save(state(anomaly('log|b|ERROR|y')), None)
    assert store.anomaly_by_post('C1', CARD_TS) is None
    box, f = intake(store, gate_replies=[verdict()])

    # when
    decision = box.handle(reply_event())

    # then
    assert decision == 'accepted'
    action = store.list_actions('requested', 1)[0]
    assert (action['slack']['where'], action['slack']['threadTs'], action['cards']) == ('channel', CARD_TS, [])
    assert f.gate.gate_requests[0]['messages'][0]['content'].startswith(prompt.NO_CARD)


def replier(store: Store, listen: str = 'live', fail=None, post_fail: bool = False):
    slack, post, api = fake_slack(fail=fail, post_fail=post_fail)
    return Replier(store, slack, dataclasses.replace(settings(), slack_listen=listen), clock=lambda: NOW), post, api


CARD_CALL = {'key': KEY, 'action': 'resolve'}


def answered(store: Store, where: str = 'card', placeholder: bool = True, proposal: bool = False, card_action: dict | None = None) -> dict:
    s = {'where': where, 'channel': 'C1', 'threadTs': CARD_TS, 'ts': ASK_TS, 'user': 'U1'} | ({'placeholderTs': '1792224000.000300'} if placeholder else {})
    act = store.add_action({'type': 'ask', 'source': 'slack', 'key': 'ask', 'target': 'slack:C1:x', 'text': 'q', 'cards': [KEY] if where == 'card' else [], 'slack': s})
    return act | ({'ticketProposal': {'key': KEY, 'title': 'null guard', 'description': 'NPE at line 69', 'points': 3}} if proposal else {}) | ({'cardAction': card_action} if card_action else {})


@pytest.mark.parametrize('outcome, recap, expected', [
    ('done', 'One client <b> & adam@example.com', f'<@U1> One client &lt;b&gt; &amp; {REDACTED}'),
    ('capped', 'half an answer', f'{REPLY_CAPPED}\nhalf an answer'),
    ('capped', '', REPLY_CAPPED),
    ('stopped', 'x', REPLY_STOPPED),
    ('failed', 'x', REPLY_FAILED),
    ('refused', '', REPLY_FAILED),
    ('done', '  ', REPLY_FAILED),
])
def test_the_reply_text_follows_the_outcome(outcome, recap, expected):
    # then
    assert Replier.text_for({'slack': {'user': 'U1'}}, outcome, recap) == expected


def test_a_live_reply_is_a_new_message_at_the_end_of_the_thread_and_the_placeholder_goes(store: Store):
    # given
    r, post, api = replier(store)
    act = answered(store, proposal=True)

    # when
    sent = r.reply(act, 'done', 'Draft ready.')

    # then
    [payload] = post.calls
    assert (payload['channel'], payload['thread_ts'], payload['text']) == ('C1', CARD_TS, '<@U1> Draft ready.')
    assert payload['blocks'][-1]['elements'][0]['action_id'] == CREATE_TASK and payload['blocks'][-1]['elements'][0]['value'] == act['id']
    assert api.calls == [('chat.delete', {'channel': 'C1', 'ts': '1792224000.000300'})]
    assert sent == {'ts': '1792224000.000001', 'text': '<@U1> Draft ready.', 'at': iso(NOW), 'button': True}
    assert store.get_action(act['id'])['slackReply'] == sent


@pytest.mark.parametrize('where, outcome', [('channel', 'done'), ('card', 'failed')])
def test_no_button_without_a_card_or_on_a_failed_run(store: Store, where, outcome):
    # given
    r, post, _ = replier(store)

    # when
    r.reply(answered(store, where=where, proposal=True), outcome, 'x')

    # then
    assert all(b['type'] != 'actions' for b in post.calls[0]['blocks'])


def test_a_live_reply_carries_the_card_action_for_a_channel_ask_and_both_buttons_with_a_draft(store: Store):
    # given
    store.save(state(anomaly(KEY)), None)
    r, post, _ = replier(store)
    channel_ask = answered(store, where='channel', card_action=CARD_CALL)
    card_ask = answered(store, proposal=True, card_action={'key': KEY, 'action': 'mute', 'hours': 6})

    # when
    sent = r.reply(channel_ask, 'done', 'The Resolve button is below.')
    r.reply(card_ask, 'done', 'Draft and action.')

    # then
    first, second = post.calls[0]['blocks'], post.calls[1]['blocks']
    assert first[1]['text']['text'] == '*Suggested action:* Resolve · checkout-service ERROR pattern'
    assert [(e['action_id'], e['value']) for e in first[-1]['elements']] == [(CARD_ACTION, channel_ask['id'])]
    assert [e['action_id'] for e in second[-1]['elements']] == [CREATE_TASK, CARD_ACTION]
    assert sent['button'] is True and sent['cardAction'] is True


def test_a_card_action_for_a_card_that_is_gone_carries_no_button(store: Store):
    # given
    r, post, _ = replier(store)

    # when
    sent = r.reply(answered(store, where='channel', card_action=CARD_CALL), 'done', 'answer')

    # then
    assert all(b['type'] != 'actions' for b in post.calls[0]['blocks']) and 'cardAction' not in sent


def test_a_shadow_reply_stores_that_a_card_action_was_proposed(store: Store):
    # given
    r, post, api = replier(store, listen='shadow')

    # when
    sent = r.reply(answered(store, card_action=CARD_CALL), 'done', 'answer')

    # then
    assert post.calls == [] and api.calls == []
    assert sent == {'shadow': '<@U1> answer', 'at': iso(NOW), 'cardAction': True}


@pytest.mark.parametrize('listen', ['live', 'shadow'])
def test_a_failed_or_blank_answer_carries_no_card_action(store: Store, listen):
    # given
    store.save(state(anomaly(KEY)), None)
    r, post, _ = replier(store, listen=listen)

    # when
    blank = r.reply(answered(store, card_action=CARD_CALL), 'done', '  ')
    failed = r.reply(answered(store, card_action=CARD_CALL), 'failed', 'x')

    # then
    assert 'cardAction' not in blank and 'cardAction' not in failed
    assert all(b['type'] != 'actions' for p in post.calls for b in p['blocks'])


def test_a_placeholder_slack_will_not_delete_or_none_at_all_still_means_a_new_thread_reply(store: Store):
    # given
    r, post, _ = replier(store, fail={'chat.delete'})

    # when
    first = r.reply(answered(store), 'done', 'answer')
    second = r.reply(answered(store, placeholder=False), 'done', 'answer')

    # then
    assert [(p['thread_ts'], p['text']) for p in post.calls] == [(CARD_TS, '<@U1> answer')] * 2
    assert first['ts'] and second['ts']


def test_when_the_new_reply_cannot_be_posted_the_answer_goes_into_the_placeholder(store: Store):
    # given
    r, _, api = replier(store, post_fail=True)

    # when
    sent = r.reply(answered(store), 'done', 'answer')
    none = r.reply(answered(store, placeholder=False), 'done', 'answer')

    # then
    assert [(m, p['ts'], p['text']) for m, p in api.calls if m == 'chat.update'] == [('chat.update', '1792224000.000300', '<@U1> answer')]
    assert sent['text'] == '<@U1> answer' and none is None


def test_a_shadow_reply_calls_no_slack_and_stores_the_text(store: Store):
    # given
    r, post, api = replier(store, listen='shadow')
    act = answered(store, proposal=True)

    # when
    sent = r.reply(act, 'done', 'answer')

    # then
    assert post.calls == [] and api.calls == []
    assert sent == {'shadow': '<@U1> answer', 'at': iso(NOW), 'proposal': True}
    assert store.get_action(act['id'])['slackReply'] == sent


@pytest.mark.parametrize('setup', ['stopped', 'off'])
def test_an_untagged_message_stopped_before_the_gate_keeps_no_question_text(store: Store, setup):
    # given
    card_with_post(store)
    box, _ = intake(store, investigator=FakeInvestigator(enabled=setup != 'off', halted='stopped' if setup == 'stopped' else None))

    # when
    box.handle(reply_event('why did it go up?'))
    box.handle(reply_event(ts='1792224000.000300'))

    # then
    by_ts = {r['id'].split(':')[1]: r for r in store.list_intake(5)}
    assert 'question' not in by_ts[ASK_TS] and by_ts[ASK_TS]['login'] == 'adam.baker@example.com'
    assert by_ts['1792224000.000300']['question'] == 'why did it go up?'


def test_an_untagged_message_judged_not_addressed_keeps_its_question(store: Store):
    # given
    card_with_post(store)
    box, _ = intake(store, gate_replies=[verdict(addressed=False)])

    # when
    box.handle(reply_event('ok'))

    # then
    record = store.list_intake(1)[0]
    assert (record['decision'], record['question']) == ('not-addressed', 'ok')


@pytest.mark.parametrize('listen', ['live', 'shadow'])
def test_a_blank_answer_carries_neither_the_button_nor_the_draft(store: Store, listen):
    # given
    r, post, _ = replier(store, listen=listen)

    # when
    sent = r.reply(answered(store, proposal=True), 'done', '  ')

    # then
    if listen == 'live':
        assert sent['text'] == REPLY_FAILED and 'button' not in sent
        assert all(b['type'] != 'actions' for b in post.calls[0]['blocks'])
    else:
        assert sent == {'shadow': REPLY_FAILED, 'at': iso(NOW)}


def test_a_reply_for_a_deleted_action_is_logged_not_raised(store: Store, caplog):
    # given
    r, _, _ = replier(store)
    act = answered(store)
    store.set_action_fields = lambda *a, **k: (_ for _ in ()).throw(NotFound())

    # when
    with caplog.at_level(logging.WARNING, logger='skopax.slackevents'):
        r.reply(act, 'done', 'answer')

    # then
    assert act['id'] in caplog.text


def test_an_unexpected_error_after_the_dedupe_is_recorded_as_error(store: Store):
    # given
    card_with_post(store)
    box, env = intake(store)
    box.gate.check = lambda text, *rest: (_ for _ in ()).throw(RuntimeError('boom'))

    # when
    result = box.handle(reply_event())

    # then
    assert result == 'error'
    assert [p for m, p in env.api.calls if m == 'chat.postMessage'] == [] and env.post.calls == []
    assert store.list_intake(10)[0]['decision'] == 'error'


@pytest.mark.parametrize('text, expected', [
    ('skopax any errors in the last hour', True), ("Skopax's answer", True), ('SKOPAX’s reply', True), ('look, the skopax page is slow', True),
    ('https://skopax.example.com/#a=x look at this', False), ('<#C123|skopax> I wrote in that channel', False), ('<https://x.example.com/skopax|panel> look', False),
    ('skopax.example.com is slow', False), ('skopaxs are running', False), ('any errors', False),
    ('any problem skopax.', True), ('skopax. look', True), ('skopax-api is slow', False), ('prod-skopax card', False), ('platform/skopax deploy', False),
    ('skopax/api look', False),
])
def test_the_word_skopax_without_a_tag_counts_as_a_soft_tag_only_outside_links_and_hosts(text, expected):
    # then
    assert named(text) is expected


def named_event(text: str, ts: str = TOP_TS, **over) -> dict:
    return {'type': 'message', 'channel': 'C1', 'user': 'U1', 'text': text, 'ts': ts, **over}


def test_a_named_top_level_message_goes_through_the_gate_like_an_untagged_reply(store: Store):
    # given
    card_with_post(store)
    box, f = intake(store, gate_replies=[verdict(addressed=True), verdict(addressed=False)])

    # when
    asked = box.handle(named_event('skopax any errors in the last hour'))
    chatter = box.handle(named_event('the skopax page opens slowly', ts='1792224000.000900'))

    # then
    assert (asked, chatter) == ('accepted', 'not-addressed')
    assert [c['thread_ts'] for c in f.post.calls] == [TOP_TS]
    action = store.list_actions('requested', 1)[0]
    assert (action['slack']['where'], action['slack']['threadTs'], action['cards']) == ('channel', TOP_TS, [])
    records = {r['id'].split(':')[1]: r for r in store.list_intake(5)}
    assert records[TOP_TS]['named'] is True and records[TOP_TS]['tagged'] is False
    assert records['1792224000.000900']['decision'] == 'not-addressed'


def test_a_named_off_topic_message_gets_the_off_topic_line_and_a_refusal_before_the_gate_is_silent(store: Store):
    # given
    card_with_post(store)
    box, f = intake(store, gate_replies=[verdict(on_topic=False)])
    stopped, g = intake(store, investigator=FakeInvestigator(halted='stopped'))

    # when
    off = box.handle(named_event('skopax write me a poem'))
    silent = stopped.handle(named_event('skopax did it stop?', ts='1792224000.000900'))

    # then
    assert (off, silent) == ('off-topic', 'stopped')
    assert [c['text'] for c in f.post.calls] == [OFF_TOPIC] and g.post.calls == []


@pytest.mark.parametrize('text', ['https://skopax.example.com/#a=x look at this', '<#C123|skopax> I wrote in that channel', '<@U2> skopax can you take a look'])
def test_a_message_that_only_looks_named_or_mentions_someone_else_is_dropped_with_no_record(store: Store, text):
    # given
    card_with_post(store)
    box, f = intake(store, gate_replies=[verdict()])

    # when
    decision = box.handle(named_event(text))

    # then
    assert decision is None and f.gate.gate_requests == [] and store.list_intake(5) == []


def test_a_named_reply_in_a_foreign_thread_is_dropped_before_any_record_read_or_gate(store: Store):
    # given
    card_with_post(store)
    box, f = intake(store, gate_replies=[verdict()])

    # when
    decision = box.handle(thread_event('skopax what does this say?', ASK_TS, root=FOREIGN_TS))

    # then
    assert decision is None
    assert store.list_actions('requested', 1) == []
    assert 'conversations.replies' not in f.api.methods() and f.gate.gate_requests == [] and f.post.calls == []
    assert store.seen('C1', ASK_TS, dt.datetime.fromtimestamp(NOW, dt.timezone.utc)) is False


def test_a_named_message_from_anyone_goes_through_the_gate(store: Store):
    # given
    card_with_post(store)
    box, f = intake(store, emails={'U1': 'x@example.com'}, gate_replies=[verdict()])

    # when
    decision = box.handle(named_event('skopax any errors'))

    # then
    assert decision == 'accepted' and len(f.gate.gate_requests) == 1 and 'chat.postEphemeral' not in f.api.methods()


def test_a_tagged_reply_under_a_deploy_report_post_is_a_no_card_ask(store: Store):
    # given
    store.deploy_reports.insert_one({'_id': 'r1', 'createdAt': '2026-10-24T09:00:00Z', 'slackPost': {'channel': 'C1', 'ts': CARD_TS, 'at': iso(NOW - 600)}})
    box, f = intake(store, gate_replies=[verdict()])

    # when
    decision = box.handle(reply_event())

    # then
    assert decision == 'accepted'
    action = store.list_actions('requested', 1)[0]
    assert (action['slack']['where'], action['slack']['threadTs'], action['cards']) == ('channel', CARD_TS, [])


def chat(t: str = 'Fine, you? <!channel> <@U2> & bye', u=None):
    return reply(text(t), stop='end_turn', u=u or usage(800, 30))


SMALL_TALK = verdict(addressed=True, on_topic=False, small_talk=True, reason='greets the bot')
ESCAPED = 'Fine, you? &lt;!channel&gt; &lt;@U2&gt; &amp; bye'


def test_tagged_small_talk_gets_a_haiku_reply_in_the_thread_that_can_never_ping(store: Store):
    # given
    card_with_post(store)
    box, f = intake(store, gate_replies=[SMALL_TALK, chat()])

    # when
    got = box.handle(reply_event('<@UBOT> hey, how are you?'))

    # then
    assert got == 'small-talk'
    assert [(p['thread_ts'], p['text']) for p in f.post.calls] == [(CARD_TS, ESCAPED)]
    assert store.list_actions(None, 5) == [] and f.inv.kicks == []
    talk_call = f.gate.gate_requests[1]
    assert talk_call['model'] == 'claude-haiku-5-5' and 'tools' not in talk_call and talk_call['thinking'] == {'type': 'disabled'}
    assert talk_call['system'].startswith(prompt.small_talk_system(''))
    assert talk_call['messages'][0]['content'] == f.gate.gate_requests[0]['messages'][0]['content']
    assert talk_call['messages'][0]['content'].endswith(f'</thread>\n\n{brand.fill(prompt.TAGGED)}\n\n<message>\nhey, how are you?\n</message>')
    record = store.list_intake(1)[0]
    assert record['decision'] == 'small-talk' and record['gate']['smallTalk'] is True
    assert record['smallTalk'] == {'text': 'Fine, you? <!channel> <@U2> & bye', 'usd': 0.000095}
    ledger = [(r['source'], r.get('talkFor'), r['by'], r['keys']) for r in store.ledger_since('2000-01-01')]
    assert ledger == [('gate', None, 'adam.baker@example.com', [KEY]), ('smalltalk', 'slack', 'adam.baker@example.com', [KEY])]


def test_small_talk_in_draft_mode_posts_nothing_and_keeps_the_escaped_reply(store: Store):
    # given
    card_with_post(store)
    box, f = intake(store, listen='shadow', gate_replies=[SMALL_TALK, chat()])

    # when
    got = box.handle(reply_event('<@UBOT> hey there?'))

    # then
    assert got == 'small-talk' and f.post.calls == []
    assert store.list_intake(1)[0]['wouldPost'] == ESCAPED


def test_untagged_small_talk_is_answered_only_when_the_gate_says_it_is_addressed(store: Store):
    # given
    card_with_post(store)
    box, f = intake(store, gate_replies=[SMALL_TALK, chat('Hi!'), verdict(addressed=False, on_topic=False, small_talk=True)])

    # when
    asked = box.handle(reply_event('hey there skopax?'))
    between = box.handle(reply_event('hey, coffee?', ts='1792224000.000300'))

    # then
    assert (asked, between) == ('small-talk', 'not-addressed')
    assert [p['text'] for p in f.post.calls] == ['Hi!'] and len(f.gate.gate_requests) == 3


@pytest.mark.parametrize('setup', ['ask-cap', 'busy'])
def test_small_talk_goes_through_an_exhausted_slack_ask_cap_and_a_busy_thread(store: Store, setup):
    # given
    card_with_post(store)
    if setup == 'ask-cap':
        for i in range(10):
            slack_ask(store, thread_ts=f'1792224000.0009{i:02d}')
    else:
        slack_ask(store)
    box, f = intake(store, gate_replies=[SMALL_TALK, chat('Hi!'), verdict()])

    # when
    talked = box.handle(reply_event('<@UBOT> hey there?'))
    asked = box.handle(reply_event('<@UBOT> why did it go up?', ts='1792224000.000300'))

    # then
    assert (talked, asked) == ('small-talk', 'ask-cap' if setup == 'ask-cap' else 'queued')
    assert [p['text'] for p in f.post.calls] == ['Hi!', ASK_CAPPED if setup == 'ask-cap' else BUSY]
    assert len(f.inv.kicks) == (0 if setup == 'ask-cap' else 1)


@pytest.mark.parametrize('rules, skipped, decision, line', [({'enabled': False}, 'disabled', 'off-topic', OFF_TOPIC),
                                                            ({'hourlyCap': 1}, 'cap', 'small-talk-cap', SMALL_TALK_CAPPED)])
def test_tagged_small_talk_with_the_rules_off_gets_the_off_topic_line_and_over_its_cap_the_limit_line(store: Store, rules, skipped, decision, line):
    # given
    card_with_post(store)
    store.set_small_talk_rules({'enabled': True, 'rules': 'Answer briefly.', 'hourlyCap': 10} | rules)
    store.seen('C9', 'x', AT)
    store.record_intake('C9', 'x', {'decision': 'small-talk'})
    box, f = intake(store, gate_replies=[verdict(addressed=True, on_topic=False, small_talk=True, language='en'), chat()])

    # when
    got = box.handle(reply_event('<@UBOT> how are you?'))

    # then
    assert got == decision
    assert [p['text'] for p in f.post.calls] == [line]
    assert len(f.gate.gate_requests) == 1
    assert {r['id']: r for r in store.list_intake(5)}[f'C1:{ASK_TS}']['smallTalkSkipped'] == skipped


def small_talk_capped(store: Store, cap: int = 1) -> None:
    store.set_small_talk_rules({'enabled': True, 'rules': 'Answer briefly.', 'hourlyCap': cap})
    store.seen('C9', 'x', AT)
    store.record_intake('C9', 'x', {'decision': 'small-talk'})


@pytest.mark.parametrize('language', ['nl', 'en', 'other'])
def test_untagged_addressed_small_talk_over_its_cap_gets_the_limit_line_in_its_language(store: Store, language):
    # given
    card_with_post(store)
    small_talk_capped(store)
    box, f = intake(store, gate_replies=[verdict(addressed=True, on_topic=False, small_talk=True, language=language)])

    # when
    got = box.handle(reply_event('you rock skopax'))

    # then
    assert got == 'small-talk-cap'
    assert [p['text'] for p in f.post.calls] == [SMALL_TALK_CAPPED]
    assert SMALL_TALK_CAPPED == 'My small talk limit for this hour is full 😄 Prod questions still work.'


@pytest.mark.parametrize('setup', ['ask-cap', 'busy'])
def test_small_talk_over_its_cap_on_a_capped_or_busy_thread_gets_the_limit_line_not_the_held_line(store: Store, setup):
    # given
    card_with_post(store)
    small_talk_capped(store)
    if setup == 'ask-cap':
        for i in range(10):
            slack_ask(store, thread_ts=f'1792224000.0009{i:02d}')
    else:
        slack_ask(store)
    box, f = intake(store, gate_replies=[SMALL_TALK])

    # when
    got = box.handle(reply_event('<@UBOT> hey there?'))

    # then
    assert got == 'small-talk-cap'
    assert [p['text'] for p in f.post.calls] == [SMALL_TALK_CAPPED]


def test_small_talk_over_its_cap_in_draft_mode_keeps_the_limit_line(store: Store):
    # given
    card_with_post(store)
    small_talk_capped(store)
    box, f = intake(store, listen='shadow', gate_replies=[SMALL_TALK])

    # when
    got = box.handle(reply_event('<@UBOT> hey there?'))

    # then
    assert got == 'small-talk-cap'
    assert f.post.calls == []
    assert {r['id']: r for r in store.list_intake(5)}[f'C1:{ASK_TS}']['wouldPost'] == SMALL_TALK_CAPPED


@pytest.mark.parametrize('setup', [None, 'busy'])
def test_untagged_small_talk_with_the_rules_off_stays_silent(store: Store, setup):
    # given
    card_with_post(store)
    store.set_small_talk_rules({'enabled': False, 'rules': 'Answer briefly.', 'hourlyCap': 10})
    if setup == 'busy':
        slack_ask(store)
    box, f = intake(store, gate_replies=[SMALL_TALK])

    # when
    got = box.handle(reply_event('you rock skopax'))

    # then
    assert got == 'small-talk-off'
    assert f.post.calls == []
    record = {r['id']: r for r in store.list_intake(5)}[f'C1:{ASK_TS}']
    assert record['smallTalkSkipped'] == 'disabled' and 'wouldPost' not in record


def test_slack_and_page_small_talk_share_one_hourly_cap(store: Store):
    # given
    card_with_post(store)
    store.set_small_talk_rules({'enabled': True, 'rules': 'Answer briefly.', 'hourlyCap': 2})
    store.add_action({'type': 'ask', 'source': 'page', 'key': 'ask', 'target': 'ask', 'text': 'hey', 'smallTalk': True}, 'done', 'fine')
    box, f = intake(store, gate_replies=[SMALL_TALK, chat('Hi!'), SMALL_TALK])

    # when
    first = box.handle(reply_event('<@UBOT> hey there?'))
    second = box.handle(reply_event('<@UBOT> how are you?', ts='1792224000.000300'))

    # then
    assert (first, second) == ('small-talk', 'small-talk-cap')
    assert [p['text'] for p in f.post.calls] == ['Hi!', SMALL_TALK_CAPPED]


@pytest.mark.parametrize('answer', [reply(text(''), stop='refusal'), chat('  '), None])
def test_a_refused_empty_or_failed_small_talk_reply_falls_back_to_the_off_topic_line(store: Store, answer):
    # given
    card_with_post(store)
    box, f = intake(store, gate_replies=[SMALL_TALK] + ([answer] if answer is not None else []))

    # when
    got = box.handle(reply_event('<@UBOT> hey there?'))

    # then
    assert got == 'off-topic'
    assert [p['text'] for p in f.post.calls] == [OFF_TOPIC]
    record = store.list_intake(1)[0]
    assert record['smallTalkSkipped'] == 'failed' and 'smallTalk' not in record


@pytest.mark.parametrize('halted, line', [('stopped', STOPPED), ('capped', CAPPED)])
def test_small_talk_stays_behind_the_stop_switch_and_the_daily_cap(store: Store, halted, line):
    # given
    card_with_post(store)
    box, f = intake(store, investigator=FakeInvestigator(halted=halted), gate_replies=[SMALL_TALK, chat()])

    # when
    got = box.handle(reply_event('<@UBOT> hey there?'))

    # then
    assert got == halted
    assert [p['text'] for p in f.post.calls] == [line] and f.gate.gate_requests == []


def test_small_talk_while_the_gate_is_down_keeps_todays_behavior(store: Store):
    # given
    card_with_post(store)
    box, f = intake(store, gate_replies=[])

    # when
    tagged = box.handle(reply_event('<@UBOT> hey there?'))
    store.update_action(store.list_actions('requested', 1)[0]['id'], 'done', 'x', None)
    untagged = box.handle(reply_event('hey there skopax?', ts='1792224000.000300'))

    # then
    assert (tagged, untagged) == ('accepted', 'gate-down')
    assert [p['text'] for p in f.post.calls] == [PLACEHOLDER]


def test_tagged_asks_in_a_busy_thread_with_the_gate_down_or_capped_are_queued(store: Store):
    # given
    card_with_post(store)
    first = slack_ask(store)
    down, f = intake(store, gate_replies=[])
    capped, g = intake(store, gate_replies=[SMALL_TALK])

    # when
    a = down.handle(reply_event('<@UBOT> hey there?'))
    for i in range(GATE_HOURLY_CAP):
        store.seen('C9', str(i), AT)
        store.record_intake('C9', str(i), {'gate': {'onTopic': True}})
    b = capped.handle(reply_event('<@UBOT> hey there?', ts='1792224000.000300'))

    # then
    assert (a, b) == ('queued', 'queued')
    assert [p['text'] for p in f.post.calls + g.post.calls] == [BUSY, BUSY]
    assert g.gate.gate_requests == []
    assert len([x for x in store.list_actions('requested', 10) if x['id'] != first['id']]) == 2


def test_a_question_in_a_busy_thread_is_queued_behind_the_open_ask_with_the_busy_line_as_its_placeholder(store: Store):
    # given
    card_with_post(store)
    first = slack_ask(store)
    box, f = intake(store, gate_replies=[verdict()])

    # when
    got = box.handle(reply_event())

    # then
    queued = store.list_actions('requested', 1)[0]
    assert got == 'queued' and queued['id'] != first['id'] and queued['text'] == 'why did it go up?'
    assert [p['text'] for p in f.post.calls] == [BUSY] and queued['slack']['placeholderTs'] == '1792224000.000001'
    assert queued['slack']['threadTs'] == CARD_TS and len(f.inv.kicks) == 1
    assert store.list_intake(1)[0]['decision'] == 'queued'


@pytest.mark.parametrize('addressed, decision, posted', [(True, 'queued', [BUSY]), (False, 'not-addressed', [])])
def test_an_untagged_message_in_a_busy_thread_is_queued_only_when_the_gate_says_it_is_addressed(store: Store, addressed, decision, posted):
    # given
    card_with_post(store)
    slack_ask(store)
    box, f = intake(store, gate_replies=[verdict(addressed=addressed)])

    # when
    got = box.handle(reply_event('why did it go up?'))

    # then
    record = store.list_intake(1)[0]
    assert (got, record['question'], record['gate']['onTopic']) == (decision, 'why did it go up?', True)
    assert [p['text'] for p in f.post.calls] == posted and len(f.gate.gate_requests) == 1


ANSWERED = [{'ts': CARD_TS, 'bot_id': 'B0BOT', 'text': 'checkout-service ERROR 40'}, {'ts': ASK_TS, 'user': 'U1', 'text': '<@UBOT> is there a problem right now?'},
            {'ts': '1792224000.000300', 'user': 'UBOT', 'bot_id': 'B0BOT', 'text': 'No problem right now.'}]
ANSWERED_VIEW = [{'who': 'bot', 'text': 'checkout-service ERROR 40'}, {'who': 'person', 'text': 'is there a problem right now?'}, {'who': 'bot', 'text': 'No problem right now.'}]
POSTS = {'small-talk': ['Hi!'], 'accepted': [PLACEHOLDER], 'not-addressed': [], 'off-topic': [OFF_TOPIC], 'take-offered': [msg('slackevents.offer.take', name='adam.baker')],
         'resolve-offered': [msg('slackevents.offer.resolve', name='adam.baker')], 'known-offered': [msg('slackevents.offer.known', name='adam.baker')], 'mute-offered': [msg('slackevents.offer.mute', name='adam.baker', hours=24)]}


@pytest.mark.parametrize('group, message', [(g, m) for g, examples in GROUPS.items() for m in examples[0]])
def test_each_gate_example_group_routes_an_untagged_reply_after_the_bots_answer(store: Store, group, message):
    # given
    _, judged, decision = GROUPS[group]
    card_with_post(store)
    box, f = intake(store, gate_replies=[verdict(**judged), chat('Hi!')])
    f.api.thread = ANSWERED + [{'ts': '1792224000.000400', 'user': 'U1', 'text': message}]

    # when
    got = box.handle(reply_event(message, ts='1792224000.000400'))

    # then
    assert got == decision
    assert [p['text'] for p in f.post.calls] == POSTS[decision]
    sent = f.gate.gate_requests[0]
    assert sent['system'] == prompt.gate_system() and f'"{message}"' in sent['system']
    assert sent['messages'][0]['content'].endswith(f'bot: No problem right now.\n</thread>\n\n<message>\n{message}\n</message>')
    assert brand.fill(prompt.TAGGED) not in sent['messages'][0]['content']
    assert len(f.gate.gate_requests) == (2 if decision == 'small-talk' else 1)


def test_a_named_top_level_remark_to_the_bot_gets_a_small_talk_reply_under_it(store: Store):
    # given
    card_with_post(store)
    box, f = intake(store, gate_replies=[SMALL_TALK, chat('Thanks!')])

    # when
    got = box.handle(named_event('you rock skopax'))

    # then
    assert got == 'small-talk'
    assert [(p['thread_ts'], p['text']) for p in f.post.calls] == [(TOP_TS, 'Thanks!')]
    record = store.list_intake(1)[0]
    assert (record['named'], record['tagged'], record['gate']['addressed'], record['gate']['smallTalk']) == (True, False, True, True)


BARE_TS = '1792224000.000600'


def said(ts: str, text: str, user: str = 'U1') -> dict:
    return {'ts': ts, 'user': user, 'text': text}


def bare_thread(*between: dict) -> list[dict]:
    return ANSWERED + list(between) + [said(BARE_TS, '<@UBOT>')]


def test_a_bare_tag_answers_the_same_users_last_message_after_the_bots_reply(store: Store):
    # given
    card_with_post(store)
    box, f = intake(store, gate_replies=[SMALL_TALK, chat('I am always here.')])
    f.api.thread = bare_thread(said('1792224000.000400', 'skopax you too?'), said('1792224000.000500', 'ignoring us', user='U2'))

    # when
    got = box.handle(reply_event('<@UBOT>', ts=BARE_TS))

    # then
    assert got == 'small-talk'
    assert [(p['thread_ts'], p['text']) for p in f.post.calls] == [(CARD_TS, 'I am always here.')]
    content = f.gate.gate_requests[0]['messages'][0]['content']
    assert content.endswith(f'person: ignoring us\n</thread>\n\n{brand.fill(prompt.TAGGED)}\n\n<message>\nskopax you too?\n</message>') and content.count('skopax you too?') == 1
    record = store.list_intake(1)[0]
    assert (record['tagged'], record['questionFrom'], record['question'], record['decision']) == (True, '1792224000.000400', 'skopax you too?', 'small-talk')
    assert f.api.methods().count('conversations.replies') == 1


def test_a_bare_tag_turns_the_picked_message_into_a_tagged_ask_with_the_thread_around_it(store: Store):
    # given
    card_with_post(store)
    box, f = intake(store, gate_replies=[verdict()])
    f.api.thread = bare_thread(said('1792224000.000400', 'and why did it go up yesterday?'))

    # when
    got = box.handle(reply_event('<@UBOT>', ts=BARE_TS))

    # then
    assert got == 'accepted'
    action = store.list_actions('requested', 1)[0]
    assert (action['text'], action['slack']['ts'], action['slack']['threadTs'], action['cards']) == ('and why did it go up yesterday?', BARE_TS, CARD_TS, [KEY])
    assert action['thread'] == ANSWERED_VIEW
    assert store.list_intake(1)[0]['questionFrom'] == '1792224000.000400'


def test_a_bare_tag_picks_a_message_up_to_15_minutes_old(store: Store):
    # given
    card_with_post(store)
    box, f = intake(store, gate_replies=[verdict()])
    f.api.thread = [{'ts': '1792222000.000100', 'bot_id': 'B0BOT', 'text': 'checkout-service ERROR 40'}, said('1792223200.000100', 'why did this go up?'), said(BARE_TS, '<@UBOT>')]

    # when
    got = box.handle(reply_event('<@UBOT>', ts=BARE_TS))

    # then
    assert got == 'accepted' and store.list_intake(1)[0]['questionFrom'] == '1792223200.000100'


@pytest.mark.parametrize('thread', [
    bare_thread(said('1792224000.000400', 'skopax you too?', user='U2')),
    bare_thread(),
    bare_thread(said('1792224000.000400', '<@UBOT>')),
    [{'ts': '1792222000.000100', 'bot_id': 'B0BOT', 'text': 'checkout-service ERROR 40'}, said('1792222900.000100', 'skopax you too?'), said(BARE_TS, '<@UBOT>')],
], ids=['another user', 'before the bot reply', 'another bare tag', 'older than 15 minutes'])
def test_a_bare_tag_with_nothing_to_pick_gets_the_fixed_line_and_no_model_call(store: Store, thread):
    # given
    card_with_post(store)
    box, f = intake(store, gate_replies=[verdict()])
    f.api.thread = thread

    # when
    got = box.handle(reply_event('<@UBOT>', ts=BARE_TS))

    # then
    assert got == 'empty-tag'
    assert [(p['thread_ts'], p['text']) for p in f.post.calls] == [(CARD_TS, EMPTY_TAG)]
    assert f.gate.gate_requests == [] and store.list_actions(None, 5) == [] and f.inv.kicks == []
    record = store.list_intake(1)[0]
    assert (record['decision'], record['login'], record['tagged']) == ('empty-tag', 'adam.baker@example.com', True) and 'questionFrom' not in record


def test_a_bare_tag_with_nothing_to_pick_in_draft_mode_posts_nothing_and_keeps_the_line(store: Store):
    # given
    card_with_post(store)
    box, f = intake(store, listen='shadow')
    f.api.thread = bare_thread()

    # when
    got = box.handle(reply_event('<@UBOT>', ts=BARE_TS))

    # then
    assert got == 'empty-tag' and f.post.calls == []
    assert store.list_intake(1)[0]['wouldPost'] == EMPTY_TAG


def test_a_bare_tag_at_the_top_level_gets_the_fixed_line_under_it_without_a_thread_read(store: Store):
    # given
    card_with_post(store)
    box, f = intake(store)

    # when
    got = box.handle(top_event('<@UBOT>'))

    # then
    assert got == 'empty-tag'
    assert [(p['thread_ts'], p['text']) for p in f.post.calls] == [(TOP_TS, EMPTY_TAG)]
    assert [p['ts'] for m, p in f.api.calls if m == 'conversations.replies'] == [TOP_TS] and f.gate.gate_requests == [], 'only the tagged message itself is read, for its files'


def test_a_bare_tag_from_anyone_picks_their_earlier_question(store: Store):
    # given
    card_with_post(store)
    box, f = intake(store, emails={'U9': 'x@example.com'}, gate_replies=[verdict()])
    f.api.thread = bare_thread(said('1792224000.000400', 'skopax you too?', user='U9'))

    # when
    got = box.handle(reply_event('<@UBOT>', ts=BARE_TS, user='U9'))

    # then
    assert got == 'accepted' and 'chat.postEphemeral' not in f.api.methods()
    assert store.list_actions('requested', 1)[0]['text'] == 'skopax you too?'


def test_a_bare_tag_in_a_thread_the_bot_never_spoke_in_picks_from_the_whole_window(store: Store):
    # given
    card_with_post(store)
    box, f = intake(store, gate_replies=[verdict()])
    f.api.thread = [said(FOREIGN_TS, 'this gave 500s after the deploy', user='U2'), said('1792224000.000850', 'what is this saying'), said('1792224000.000900', '<@UBOT>')]

    # when
    got = box.handle(thread_event('<@UBOT>', '1792224000.000900', root=FOREIGN_TS))

    # then
    assert got == 'accepted'
    action = store.list_actions('requested', 1)[0]
    assert (action['text'], action['slack']['threadTs'], action['thread']) == ('what is this saying', FOREIGN_TS, [{'who': 'person', 'text': 'this gave 500s after the deploy'}])


def test_a_bare_tag_with_a_pick_stays_behind_the_stop_switch(store: Store):
    # given
    card_with_post(store)
    box, f = intake(store, investigator=FakeInvestigator(halted='stopped'))
    f.api.thread = bare_thread(said('1792224000.000400', 'skopax you too?'))

    # when
    got = box.handle(reply_event('<@UBOT>', ts=BARE_TS))

    # then
    assert got == 'stopped'
    assert [p['text'] for p in f.post.calls] == [STOPPED] and f.gate.gate_requests == []
    assert store.list_intake(1)[0]['questionFrom'] == '1792224000.000400'


TAKES = verdict(addressed=False, on_topic=True, card_intent='take', reason='the writer is checking the card')
OFFER_LINE = '👀 adam.baker seems to be looking at this. Shall we mark the card In progress?'


def offers(store: Store) -> list[dict]:
    return [a for a in store.list_actions(None, 20) if a['type'] == 'take-offer']


def test_someone_checking_the_card_gets_one_in_progress_offer_with_a_button_and_no_run(store: Store):
    # given
    card_with_post(store)
    box, f = intake(store, gate_replies=[TAKES, TAKES])

    # when
    first = box.handle(reply_event('looking into it'))
    second = box.handle(reply_event('I am looking into it', ts='1792224000.000300'))

    # then
    assert (first, second) == ('take-offered', 'not-addressed')
    [offer] = offers(store)
    assert (offer['status'], offer['source'], offer['key'], offer['requestedBy'], offer['result']) == ('done', 'slack', KEY, 'adam.baker@example.com', OFFER_LINE)
    assert offer['cardAction'] == {'key': KEY, 'action': 'take', 'owner': 'adam.baker@example.com'}
    assert [(p['thread_ts'], p['text']) for p in f.post.calls] == [(CARD_TS, OFFER_LINE)]
    assert f.post.calls[0]['blocks'] == [{'type': 'section', 'text': {'type': 'mrkdwn', 'text': OFFER_LINE}},
                                         {'type': 'actions', 'elements': [{'type': 'button', 'action_id': CARD_ACTION, 'value': offer['id'], 'style': 'primary',
                                                                           'text': {'type': 'plain_text', 'text': 'In progress · adam.baker'}}]}]
    assert store.anomaly(KEY)['takeOffered'] == {'ts': ASK_TS, 'by': 'adam.baker@example.com', 'at': iso(NOW)}
    assert store.anomaly(KEY).get('triage') is None
    record = {r['id']: r for r in store.list_intake(5)}[f'C1:{ASK_TS}']
    assert (record['decision'], record['actionId'], record['gate']['cardIntent'], record['gate']['addressed']) == ('take-offered', offer['id'], 'take', False)
    assert f.inv.kicks == [] and len(f.gate.gate_requests) == 2 and store.list_actions('requested', 5) == []


@pytest.mark.parametrize('setup', ['progress', 'handled', 'known', 'muted', 'cleared'])
def test_no_offer_on_a_card_already_in_progress_handled_known_muted_or_cleared(store: Store, setup):
    # given
    card_with_post(store)
    if setup in ('progress', 'handled'):
        store.triage(KEY, setup, owner='baker.adam@example.com')
    elif setup == 'known':
        store.mute(KEY, 'known noise', None, 'known')
    elif setup == 'muted':
        store.mute(KEY, 'muted for 6 h', iso(NOW + 6 * 3600), 'mute')
    else:
        store.anomalies.update_one({'_id': KEY}, {'$set': {'status': 'cleared'}})
    box, f = intake(store, gate_replies=[TAKES])

    # when
    got = box.handle(reply_event('looking into it'))

    # then
    assert got == 'not-addressed'
    assert f.post.calls == [] and offers(store) == []
    assert 'takeOffered' not in store.anomaly(KEY)


def test_no_offer_in_a_channel_thread_without_a_card(store: Store):
    # given
    card_with_post(store)
    box, f = intake(store, gate_replies=[verdict(), TAKES])
    channel_ask_thread(store, box, f)

    # when
    got = box.handle(thread_event('looking into it', '1792224000.000600'))

    # then
    assert got == 'not-addressed'
    assert [p['text'] for p in f.post.calls] == [PLACEHOLDER] and offers(store) == []


def test_no_offer_when_the_gate_says_someone_else_is_checking(store: Store):
    # given
    card_with_post(store)
    box, f = intake(store, gate_replies=[verdict(addressed=False, on_topic=True, card_intent='none', reason='names someone else')])

    # when
    got = box.handle(reply_event('Gary is looking into it'))

    # then
    assert got == 'not-addressed'
    assert f.post.calls == [] and offers(store) == [] and 'takeOffered' not in store.anomaly(KEY)


@pytest.mark.parametrize('event, judged', [
    (reply_event('<@UBOT> looking into it, why did it go up?'), TAKES),
    (reply_event('skopax I am looking into it, why did it go up?'), verdict(addressed=True, on_topic=True, card_intent='take')),
])
def test_an_addressed_message_that_also_takes_the_card_keeps_the_addressed_flow(store: Store, event, judged):
    # given
    card_with_post(store)
    box, f = intake(store, gate_replies=[judged])

    # when
    got = box.handle(event)

    # then
    assert got == 'accepted'
    assert [p['text'] for p in f.post.calls] == [PLACEHOLDER] and offers(store) == []
    assert 'takeOffered' not in store.anomaly(KEY)


def test_draft_mode_keeps_the_offer_line_and_neither_posts_nor_marks_the_card(store: Store):
    # given
    card_with_post(store)
    box, f = intake(store, listen='shadow', gate_replies=[TAKES])

    # when
    got = box.handle(reply_event('looking into it'))

    # then
    assert got == 'take-offered'
    assert f.post.calls == [] and offers(store) == []
    assert 'takeOffered' not in store.anomaly(KEY)
    assert store.list_intake(1)[0]['wouldPost'] == OFFER_LINE


@pytest.mark.parametrize('setup', ['ask-cap', 'busy'])
def test_the_offer_goes_out_on_a_busy_or_ask_capped_thread_since_it_is_no_ask(store: Store, setup):
    # given
    card_with_post(store)
    if setup == 'ask-cap':
        for i in range(10):
            slack_ask(store, thread_ts=f'1792224000.0009{i:02d}')
    else:
        slack_ask(store)
    box, f = intake(store, gate_replies=[TAKES])

    # when
    got = box.handle(reply_event('looking into it'))

    # then
    assert got == 'take-offered'
    assert [p['text'] for p in f.post.calls] == [OFFER_LINE] and len(offers(store)) == 1


def test_a_failed_offer_post_without_a_slack_code_names_the_error_type_only(store: Store):
    # given
    card_with_post(store)
    box, f = intake(store, gate_replies=[TAKES])
    f.post.fail = SlackError('Slack did not answer (refused by 10.0.0.7)')

    # when
    box.handle(reply_event('looking into it'))

    # then
    [offer] = offers(store)
    assert (offer['status'], offer['result']) == ('failed', 'not posted: SlackError')


def test_a_failed_offer_post_unmarks_the_card_and_fails_the_offer(store: Store):
    # given
    card_with_post(store)
    box, f = intake(store, gate_replies=[TAKES])
    f.post.fail = True

    # when
    got = box.handle(reply_event('looking into it'))

    # then
    assert got == 'take-offer-failed'
    [offer] = offers(store)
    assert offer['status'] == 'failed' and 'channel_not_found' in offer['result']
    assert 'takeOffered' not in store.anomaly(KEY)


def test_the_offer_mark_stays_through_a_pass_while_the_card_stays_open(store: Store):
    # given
    card_with_post(store)
    box, f = intake(store, gate_replies=[TAKES, TAKES])
    box.handle(reply_event('looking into it'))

    # when
    store.save(store.load(), None)
    again = box.handle(reply_event('I am looking into it', ts='1792224000.000300'))

    # then
    assert again == 'not-addressed'
    assert len(f.post.calls) == 1 and store.anomaly(KEY)['takeOffered']['ts'] == ASK_TS


@pytest.mark.parametrize('intent, said, status, call, line', [
    ('resolve', 'fix deployed, it is fixed', 'cleared', {'action': 'resolve'}, msg('slackevents.offer.resolve', name='adam.baker')),
    ('known', 'this always happens, the night batch', 'open', {'action': 'known', 'reason': 'adam.baker in Slack: this always happens, the night batch'}, msg('slackevents.offer.known', name='adam.baker')),
    ('mute', 'let us mute it for now', 'open', {'action': 'mute', 'hours': 24}, msg('slackevents.offer.mute', name='adam.baker', hours=24)),
])
def test_a_reply_saying_fixed_known_or_silence_it_gets_the_matching_offer_once(store: Store, intent, said, status, call, line):
    # given
    card_with_post(store)
    store.anomalies.update_one({'_id': KEY}, {'$set': {'status': status}})
    judged = verdict(addressed=False, on_topic=True, card_intent=intent)
    box, f = intake(store, gate_replies=[judged, judged])

    # when
    first = box.handle(reply_event(said))
    again = box.handle(reply_event(said, ts='1792224000.000300'))

    # then
    assert (first, again) == (f'{intent}-offered', 'not-addressed')
    [offer] = [a for a in store.list_actions(None, 20) if a['type'] == f'{intent}-offer']
    assert offer['cardAction'] == {'key': KEY, **call} and offer['status'] == 'done' and offer['result'] == line
    assert [p['text'] for p in f.post.calls] == [line]
    assert f.post.calls[0]['blocks'][1]['elements'][0]['value'] == offer['id']
    assert store.anomaly(KEY)[f'{intent}Offered']['ts'] == ASK_TS and store.anomaly(KEY).get('triage') is None
    assert store.list_intake(5)[-1]['gate']['cardIntent'] == intent


@pytest.mark.parametrize('intent, setup', [('resolve', 'handled'), ('known', 'muted'), ('mute', 'cleared'), ('take', 'cleared')])
def test_no_offer_that_does_not_fit_the_card(store: Store, intent, setup):
    # given
    card_with_post(store)
    if setup == 'handled':
        store.triage(KEY, 'handled')
    elif setup == 'muted':
        store.mute(KEY, 'muted for 6 h', iso(NOW + 6 * 3600), 'mute')
    else:
        store.anomalies.update_one({'_id': KEY}, {'$set': {'status': 'cleared'}})
    box, f = intake(store, gate_replies=[verdict(addressed=False, on_topic=True, card_intent=intent)])

    # when
    got = box.handle(reply_event('x'))

    # then
    assert got == 'not-addressed' and f.post.calls == []


def test_a_card_already_in_progress_can_still_get_a_resolve_offer(store: Store):
    # given
    card_with_post(store)
    store.triage(KEY, 'progress', owner='baker.adam@example.com')
    box, f = intake(store, gate_replies=[verdict(addressed=False, on_topic=True, card_intent='resolve')])

    # when
    got = box.handle(reply_event('fixed now'))

    # then
    assert got == 'resolve-offered' and [p['text'] for p in f.post.calls] == [msg('slackevents.offer.resolve', name='adam.baker')]


def reactions(f) -> list[dict]:
    return [p for m, p in f.api.calls if m == 'reactions.add']


def test_a_reply_the_skopax_agrees_with_gets_a_thumbs_up_and_nothing_else(store: Store):
    # given
    card_with_post(store)
    box, f = intake(store, gate_replies=[verdict(addressed=False, on_topic=True, agrees=True)])
    f.api.scopes = 'chat:write,reactions:write'

    # when
    got = box.handle(reply_event('it is on the PayCo side, nothing changed on ours'))

    # then
    assert got == 'not-addressed'
    assert reactions(f) == [{'channel': 'C1', 'timestamp': ASK_TS, 'name': '+1'}] and f.post.calls == []
    record = store.list_intake(1)[0]
    assert (record['reacted'], record['gate']['agrees']) == (['+1'], True)


def test_an_agreed_fix_gets_the_thumbs_up_and_the_resolve_offer(store: Store):
    # given
    card_with_post(store)
    store.anomalies.update_one({'_id': KEY}, {'$set': {'status': 'cleared'}})
    box, f = intake(store, gate_replies=[verdict(addressed=False, on_topic=True, agrees=True, card_intent='resolve')])
    f.api.scopes = 'chat:write,reactions:write'

    # when
    got = box.handle(reply_event('it recovered after the fix'))

    # then
    assert got == 'resolve-offered' and len(reactions(f)) == 1 and [p['text'] for p in f.post.calls] == [msg('slackevents.offer.resolve', name='adam.baker')]


@pytest.mark.parametrize('setup', ['no-scope', 'addressed', 'tagged', 'disagrees', 'shadow'])
def test_no_thumbs_up_without_the_scope_on_an_answered_message_when_it_does_not_agree_or_in_draft_mode(store: Store, setup):
    # given
    card_with_post(store)
    judged = verdict(addressed=setup == 'addressed', on_topic=True, agrees=setup != 'disagrees')
    box, f = intake(store, listen='shadow' if setup == 'shadow' else 'live', gate_replies=[judged])
    if setup != 'no-scope':
        f.api.scopes = 'chat:write,reactions:write'

    # when
    box.handle(reply_event(('<@UBOT> ' if setup == 'tagged' else '') + 'it is on the PayCo side, nothing changed on ours'))

    # then
    assert reactions(f) == []
    record = store.list_intake(1)[0]
    expected = {'no-scope': ('reactSkipped', 'no reactions:write scope'), 'shadow': ('wouldReact', ['+1'])}.get(setup)
    assert (record.get(expected[0]) == expected[1]) if expected else ('reacted' not in record and 'reactSkipped' not in record)


def said_rows(store: Store) -> list[dict]:
    doc = store.card_history.find_one({'key': KEY}) or {}
    return doc.get('said') or []


@pytest.mark.parametrize('judged, words, kind, agreed', [
    (verdict(addressed=False, on_topic=True, card_intent='resolve', agrees=True), 'it recovered after the fix', 'resolve', True),
    (verdict(addressed=False, on_topic=True, agrees=True), 'it is on the PayCo side, nothing changed on ours', 'agree', True),
    (verdict(addressed=False, on_topic=True, card_intent='known'), 'this always happens, the night batch', 'known', None),
])
def test_what_people_say_about_the_card_is_kept_in_its_history(store: Store, judged, words, kind, agreed):
    # given
    card_with_post(store)
    box, f = intake(store, gate_replies=[judged])

    # when
    box.handle(reply_event(words))

    # then
    [entry] = said_rows(store)
    assert {k: v for k, v in entry.items() if k != 'link'} == {'at': iso(NOW), 'by': 'adam.baker@example.com', 'text': words, 'kind': kind, **({'agreed': True} if agreed else {})}
    assert entry['link'] == 'https://example.slack.com/archives/C1/p1792224000000200'


def test_a_ticket_posted_in_the_thread_is_kept_and_plain_chat_is_not(store: Store):
    # given
    card_with_post(store)
    box, f = intake(store, gate_replies=[verdict(addressed=False, on_topic=True)])

    # when
    box.handle(reply_event('INFRA-402 opened'))
    box.handle(reply_event('ok', ts='1792224000.000300'))

    # then
    assert [(e['kind'], e['text']) for e in said_rows(store)] == [('ticket', 'INFRA-402 opened')]


def test_an_absolutely_wild_message_gets_an_exploding_head_also_when_the_bot_answers_it(store: Store):
    # given
    card_with_post(store)
    box, f = intake(store, gate_replies=[verdict(addressed=False, on_topic=True, agrees=True, mind_blown=True), verdict(mind_blown=True)])
    f.api.scopes = 'chat:write,reactions:write'

    # when
    quiet = box.handle(reply_event('all traffic comes from one IP, 200k requests'))
    asked = box.handle(reply_event('<@UBOT> has a test service been writing the prod table for 3 years?', ts='1792224000.000300'))

    # then
    assert (quiet, asked) == ('not-addressed', 'accepted')
    assert [(p['timestamp'], p['name']) for p in reactions(f)] == [(ASK_TS, '+1'), (ASK_TS, 'exploding_head'), ('1792224000.000300', 'exploding_head')]
    records = {r['id'].split(':')[1]: r for r in store.list_intake(5)}
    assert records[ASK_TS]['reacted'] == ['+1', 'exploding_head'] and records['1792224000.000300']['gate']['mindBlown'] is True


def test_a_good_message_gets_a_snake_but_not_with_mind_blown_and_not_when_the_emoji_is_set_empty(store: Store):
    # given
    card_with_post(store)
    box, f = intake(store, gate_replies=[verdict(addressed=False, on_topic=True, great=True), verdict(addressed=False, on_topic=True, great=True, mind_blown=True)])
    off, g = intake(store, gate_replies=[verdict(addressed=False, on_topic=True, great=True)])
    off.great_emoji = ''
    f.api.scopes = g.api.scopes = 'chat:write,reactions:write'

    # when
    box.handle(reply_event('the redis pool was stuck at 8, I made it 32, latency dropped'))
    box.handle(reply_event('all traffic from one IP', ts='1792224000.000300'))
    off.handle(reply_event('root cause found', ts='1792224000.000400'))

    # then
    assert [(p['timestamp'], p['name']) for p in reactions(f)] == [(ASK_TS, 'snake'), ('1792224000.000300', 'exploding_head')]
    assert reactions(g) == []
    records = {r['id'].split(':')[1]: r for r in store.list_intake(5)}
    assert records[ASK_TS]['reacted'] == ['snake'] and records[ASK_TS]['gate']['great'] is True


JIRA_LINK = '<https://jira.example.com/browse/INFRA-402|DEVOPS - Varnish cache>'
TICKET_LINE = '🎫 <https://jira.example.com/browse/INFRA-402|INFRA-402> looks like it was opened for this card. Shall we mark the card In progress with INFRA-402?'


def test_a_jira_link_in_a_card_thread_gets_one_in_progress_offer_with_the_ticket_and_no_gate_or_run(store: Store):
    # given
    card_with_post(store)
    box, f = intake(store, gate_replies=[verdict(addressed=False, on_topic=True)])

    # when
    first = box.handle(reply_event(f'task opened {JIRA_LINK} let us move on'))
    again = box.handle(reply_event('INFRA-402 done', ts='1792224000.000300'))

    # then
    assert (first, again) == ('ticket-offered', 'not-addressed')
    [offer] = offers(store)
    assert (offer['status'], offer['key'], offer['requestedBy'], offer['result'], offer['label']) == ('done', KEY, 'adam.baker@example.com', TICKET_LINE, 'In progress offer in Slack: INFRA-402')
    assert offer['cardAction'] == {'key': KEY, 'action': 'take', 'ticket': 'INFRA-402'}
    assert [(p['thread_ts'], p['text']) for p in f.post.calls] == [(CARD_TS, TICKET_LINE)]
    assert f.post.calls[0]['blocks'][1]['elements'][0]['text']['text'] == 'In progress · INFRA-402'
    assert store.anomaly(KEY)['ticketOffered'] == {'ticket': 'INFRA-402', 'ts': ASK_TS, 'by': 'adam.baker@example.com', 'at': iso(NOW)}
    assert store.anomaly(KEY).get('triage') is None and store.anomaly(KEY).get('ticket') is None
    record = {r['id']: r for r in store.list_intake(5)}[f'C1:{ASK_TS}']
    assert (record['decision'], record['ticket'], record['actionId']) == ('ticket-offered', 'INFRA-402', offer['id']) and 'gate' not in record
    assert f.inv.kicks == [] and len(f.gate.gate_requests) == 1


def test_a_card_already_in_progress_keeps_its_owner_in_the_ticket_offer_and_another_ticket_gets_its_own_offer(store: Store):
    # given
    card_with_post(store)
    store.triage(KEY, 'progress', owner='oscar.green@example.com')
    box, f = intake(store)

    # when
    first = box.handle(reply_event('INFRA-402 opened'))
    second = box.handle(reply_event('OPS-142 too', ts='1792224000.000300'))

    # then
    assert (first, second) == ('ticket-offered', 'ticket-offered')
    assert [o['cardAction'] for o in offers(store)] == [{'key': KEY, 'action': 'take', 'ticket': 'OPS-142', 'owner': 'oscar.green@example.com'},
                                                        {'key': KEY, 'action': 'take', 'ticket': 'INFRA-402', 'owner': 'oscar.green@example.com'}]


def test_a_ticket_from_someone_who_is_not_an_operator_gets_no_offer_they_could_not_click(store: Store):
    # given
    card_with_post(store)
    box, f = intake(store, emails={'U9': 'x@example.com'}, gate_replies=[verdict(addressed=False)])

    # when
    decision = box.handle(reply_event('INFRA-402 opened', user='U9'))

    # then
    assert decision == 'not-addressed' and offers(store) == []


@pytest.mark.parametrize('setup', ['linked', 'handled', 'known', 'muted', 'cleared', 'no-project'])
def test_no_ticket_offer_for_the_linked_ticket_a_handled_known_muted_or_cleared_card_or_a_key_of_another_project(store: Store, setup):
    # given
    card_with_post(store)
    words = 'it came as UTF-8' if setup == 'no-project' else 'INFRA-402 opened'
    if setup in ('linked', 'handled'):
        store.triage(KEY, 'progress' if setup == 'linked' else 'handled', ticket='INFRA-402' if setup == 'linked' else None)
    elif setup == 'known':
        store.mute(KEY, 'known noise', None, 'known')
    elif setup == 'muted':
        store.mute(KEY, 'muted for 6 h', iso(NOW + 6 * 3600), 'mute')
    elif setup == 'cleared':
        store.anomalies.update_one({'_id': KEY}, {'$set': {'status': 'cleared'}})
    box, f = intake(store, gate_replies=[verdict(addressed=False, on_topic=True)])

    # when
    got = box.handle(reply_event(words))

    # then
    assert got == 'not-addressed'
    assert f.post.calls == [] and offers(store) == [] and 'ticketOffered' not in store.anomaly(KEY)


def test_a_tagged_message_with_a_ticket_becomes_an_ask_that_carries_the_ticket(store: Store):
    # given
    card_with_post(store)
    box, f = intake(store, gate_replies=[verdict(addressed=True, on_topic=True)])

    # when
    got = box.handle(reply_event(f'<@UBOT> task opened {JIRA_LINK} move it to in progress'))

    # then
    assert got == 'accepted'
    assert [p['text'] for p in f.post.calls] == [PLACEHOLDER] and offers(store) == []
    action = store.list_actions('requested', 1)[0]
    assert action['mentionedTicket'] == 'INFRA-402' and 'ticket' not in action and 'ticketOffered' not in store.anomaly(KEY)
    assert store.list_intake(1)[0]['ticket'] == 'INFRA-402'


def test_draft_mode_keeps_the_ticket_offer_line_and_a_failed_post_unmarks_the_card(store: Store):
    # given
    card_with_post(store)
    shadow, _ = intake(store, listen='shadow')
    live, f = intake(store)
    f.post.fail = True

    # when
    drafted = shadow.handle(reply_event('INFRA-402 opened'))
    failed = live.handle(reply_event('INFRA-402 opened', ts='1792224000.000300'))

    # then
    assert (drafted, failed) == ('ticket-offered', 'ticket-offer-failed')
    records = {r['id'].split(':')[1]: r for r in store.list_intake(5)}
    assert records[ASK_TS]['wouldPost'] == TICKET_LINE
    [offer] = offers(store)
    assert offer['status'] == 'failed' and 'ticketOffered' not in store.anomaly(KEY)


def test_the_ticket_offer_mark_stays_through_a_pass_while_the_card_stays_open(store: Store):
    # given
    card_with_post(store)
    box, f = intake(store, gate_replies=[verdict(addressed=False, on_topic=True)])
    box.handle(reply_event('INFRA-402 opened'))

    # when
    store.save(store.load(), None)
    again = box.handle(reply_event('INFRA-402 opened', ts='1792224000.000300'))

    # then
    assert again == 'not-addressed'
    assert len(f.post.calls) == 1 and store.anomaly(KEY)['ticketOffered']['ticket'] == 'INFRA-402'


def test_a_skopax_user_without_any_role_gets_answers_and_the_in_progress_offer_too(store: Store):
    # given
    card_with_post(store)
    box, f = intake(store, emails={'U9': 'mark.young@example.com'}, members={'mark.young@example.com'}, gate_replies=[TAKES, verdict()])

    # when
    checking = box.handle(reply_event('looking into it', ts='1792224000.000300', user='U9'))
    asked = box.handle(reply_event('<@UBOT> why did it go up?', user='U9'))

    # then
    assert (checking, asked) == ('take-offered', 'accepted'), 'every account is treated as an operator now'
    action = store.list_actions('requested', 1)[0]
    assert action['requestedBy'] == 'mark.young@example.com'
    records = {r['id'].split(':')[1]: r for r in store.list_intake(5)}
    assert records[ASK_TS]['operator'] is True


def test_a_named_top_level_message_shows_the_gate_the_lines_just_before_it(store: Store):
    # given
    card_with_post(store)
    box, f = intake(store, gate_replies=[verdict(addressed=True, on_topic=False, small_talk=True)])
    f.api.history = [said('1792223990.000100', 'hello waffle'), said('1792220000.000100', 'two hours ago')]

    # when
    box.handle(named_event('pardon skopax'))

    # then
    [call] = [c for c in f.api.calls if c[0] == 'conversations.history']
    assert (call[1]['channel'], call[1]['latest']) == ('C1', TOP_TS)
    sent = json.dumps(f.gate.gate_requests[0]['messages'], ensure_ascii=False)
    assert 'hello waffle' in sent and 'two hours ago' not in sent


def test_a_failed_ticket_offer_post_without_a_slack_code_names_the_error_type_only(store: Store):
    # given
    card_with_post(store)
    live, f = intake(store)
    f.post.fail = SlackError('Slack did not answer (refused by 10.0.0.7)')

    # when
    failed = live.handle(reply_event('INFRA-402 opened', ts='1792224000.000300'))

    # then
    [offer] = offers(store)
    assert failed == 'ticket-offer-failed' and (offer['status'], offer['result']) == ('failed', 'not posted: SlackError')


def test_a_bare_tag_in_a_thread_slack_will_not_let_us_read_says_so_and_names_the_scope(store: Store):
    # given
    card_with_post(store)
    box, f = intake(store, gate_replies=[verdict()])

    def refused(channel: str, thread_ts: str, limit: int = 10) -> list[dict]:
        raise SlackError('Slack refused: the bot token lacks the scope groups:history', code='missing_scope', needed='groups:history')
    box.slack.replies = refused

    # when
    got = box.handle(reply_event('<@UBOT>', ts=BARE_TS))

    # then
    assert got == 'read-refused'
    assert [(p['thread_ts'], p['text']) for p in f.post.calls] == [(CARD_TS, msg('slackevents.read_refused', scope='groups:history'))]
    assert 'groups:history' in msg('slackevents.read_refused', scope='groups:history') and f.gate.gate_requests == []
    record = store.list_intake(1)[0]
    assert (record['decision'], record['readRefused']) == ('read-refused', 'missing_scope')


def test_a_refused_thread_read_for_a_question_with_text_is_marked_on_the_intake_record(store: Store):
    # given
    card_with_post(store)
    box, f = intake(store, gate_replies=[verdict()])

    def refused(channel: str, thread_ts: str, limit: int = 10) -> list[dict]:
        raise SlackError('Slack refused: this call needs a bot token with more scopes (not_allowed_token_type)', code='not_allowed_token_type')
    box.slack.replies = refused

    # when
    decision = box.handle(reply_event())

    # then
    record = store.list_intake(1)[0]
    assert decision == 'accepted' and (record['readRefused'], record['missingScope']) == ('not_allowed_token_type', 'channels:history, groups:history or im:history')


def test_a_bare_tag_whose_thread_read_names_no_scope_says_which_scopes_read_a_thread(store: Store):
    # given
    card_with_post(store)
    box, f = intake(store, gate_replies=[verdict()])

    def refused(channel: str, thread_ts: str, limit: int = 10) -> list[dict]:
        raise SlackError('Slack refused: this call needs a bot token with more scopes (not_allowed_token_type)', code='not_allowed_token_type')
    box.slack.replies = refused

    # when
    box.handle(reply_event('<@UBOT>', ts=BARE_TS))

    # then
    assert [p['text'] for p in f.post.calls] == [msg('slackevents.read_refused', scope='channels:history, groups:history or im:history')]


def person(n: int, text: str, user: str = 'U2') -> dict:
    return {'ts': f'1792224000.{n:06d}', 'user': user, 'text': text}


def test_the_last_person_messages_before_a_tag_keep_their_full_text():
    # given
    posts = [person(10, 'x' * 3000), {'ts': ASK_TS, 'user': 'U1', 'text': '<@UBOT> please look into it'}]

    # when
    view = thread_view(posts, 'UBOT', ASK_TS)

    # then
    assert view == [{'who': 'person', 'text': 'x' * 3000}]


def test_an_older_message_is_still_cut_and_says_so():
    # given
    posts = [person(n, chr(96 + n) * 900) for n in range(1, 6)]

    # when
    view = thread_view(posts, 'UBOT', ASK_TS)

    # then
    assert [t['text'] for t in view[:2]] == [chr(96 + n) * 500 + ' [cut]' for n in (1, 2)]
    assert [t['text'] for t in view[2:]] == [chr(96 + n) * 900 for n in (3, 4, 5)]


def test_bot_messages_do_not_use_up_the_full_slots():
    # given
    posts = [person(1, 'a' * 900), person(2, 'b' * 900), {'ts': '1792224000.000003', 'bot_id': 'B0BOT', 'text': 'z' * 900}, person(4, 'c' * 900),
             {'ts': '1792224000.000005', 'bot_id': 'B0BOT', 'text': 'y' * 900}, person(6, 'd' * 900)]

    # when
    view = thread_view(posts, 'UBOT', ASK_TS)

    # then
    assert [len(t['text']) for t in view] == [506, 900, 506, 900, 506, 900]
    assert [t['who'] for t in view if t['text'].endswith(' [cut]')] == ['person', 'bot', 'bot']


def test_a_message_newer_than_the_tag_does_not_take_a_full_slot():
    # given
    posts = [person(1, 'a' * 900), person(2, 'b' * 900), person(3, 'c' * 900), {'ts': '1792224000.000300', 'user': 'U2', 'text': 'd' * 900}]

    # when
    view = thread_view(posts, 'UBOT', '1792224000.000250')

    # then
    assert [len(t['text']) for t in view] == [900, 900, 900, 506]
