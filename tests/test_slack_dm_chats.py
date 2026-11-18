import dataclasses

import pytest

from tests.conftest import settings
from tests.fake_anthropic import reply, text, verdict
from tests.fake_slack import fake_slack
from tests.test_investigator import NOW as INV_NOW
from tests.test_investigator_slack import first_brief, slack_investigator
from tests.test_slackevents import NOW, intake
from skopax.engine import parse_iso
from skopax.messages import msg
from skopax.slackevents import Replier
from skopax.store import ChatPrivate, Store

OFF_TOPIC = msg('slackevents.off_topic')
DM = 'D0ALI'
TS = '1792224000.000700'
LATER = '1792224100.000800'
SECRET_QUESTION = 'why is checkout-service slow, client 5551234567?'


def dm_event(text: str = SECRET_QUESTION, ts: str = TS, **over) -> dict:
    return {'type': 'message', 'channel_type': 'im', 'channel': DM, 'user': 'U1', 'text': text, 'ts': ts, **over}


def answer(store: Store, action: dict, text: str) -> None:
    store.update_action(action['id'], 'processing', None, 'requested')
    store.update_action(action['id'], 'done', text, 'processing')


def test_a_direct_message_becomes_a_private_chat_message_answered_in_the_dm_without_a_thread(store: Store):
    # given
    box, f = intake(store, gate_replies=[verdict(addressed=False)])

    # when
    decision = box.handle(dm_event())

    # then
    assert decision == 'accepted'
    assert f.post.calls == [{'channel': DM, 'text': msg('slackevents.placeholder'), 'unfurl_links': False, 'unfurl_media': False}]
    action = store.list_actions('requested', 5)[0]
    assert (action['source'], action['text'], action['requestedBy'], action['cards']) == ('chat', SECRET_QUESTION, 'adam.baker@example.com', [])
    assert action['chat']['dm'] == DM and action['target'] == f"chat:{action['chat']['id']}"
    assert action['slack'] | {'permalink': None} == {'where': 'dm', 'channel': DM, 'threadTs': None, 'ts': TS, 'user': 'U1', 'language': 'nl', 'permalink': None,
                                                     'operator': True, 'placeholderTs': '1792224000.000001'}
    assert [c['id'] for c in store.chats('adam.baker@example.com', 10)] == [action['chat']['id']]
    record = store.list_intake(1)[0]
    assert (record['decision'], record['where'], record['tagged']) == ('accepted', 'dm', True)
    assert SECRET_QUESTION not in str(record) and 'question' not in record
    assert f.inv.kicks == [NOW]


def test_the_next_dm_continues_the_chat_with_its_earlier_turns_and_a_quiet_dm_starts_a_new_one(store: Store):
    # given
    box, _ = intake(store, gate_replies=[verdict(), verdict()])
    box.handle(dm_event('first question'))
    first = store.list_actions('requested', 5)[0]
    answer(store, first, 'first answer')
    later, _ = intake(store, gate_replies=[verdict()])
    later.clock = lambda: parse_iso(first['requestedAt']) + 7 * 3600

    # when
    box.handle(dm_event('second question', ts=LATER))
    second = store.list_actions('requested', 5)[0]
    answer(store, second, 'second answer')
    later.handle(dm_event('tomorrow question', ts='1792249300.000900'))
    third = store.list_actions('requested', 5)[0]

    # then
    assert second['chat']['id'] == first['chat']['id']
    assert second['thread'] == [{'who': 'person', 'text': 'first question'}, {'who': 'bot', 'text': 'first answer'}]
    assert third['chat']['id'] != first['chat']['id'] and third['thread'] == []


def test_a_dm_while_the_last_answer_is_still_coming_waits_and_the_shared_chat_cap_and_switch_hold(store: Store):
    # given
    box, f = intake(store, gate_replies=[verdict()])
    box.handle(dm_event('first question'))
    busy = box.handle(dm_event('and this one', ts=LATER))
    capped_store = Store(store.db.client['dm-capped'])
    for i in range(3):
        capped_store.add_action({'type': 'ask', 'source': 'chat', 'key': 'ask', 'target': f'chat:c{i}', 'text': 'q', 'requestedBy': 'adam.baker@example.com', 'chat': {'id': f'c{i}'}}, 'done', 'a')
    capped, fc = intake(capped_store)
    capped.settings = dataclasses.replace(capped.settings, chat_daily_cap=3)
    off, fo = intake(Store(store.db.client['dm-off']))
    off.settings = dataclasses.replace(off.settings, chat_daily_cap=0)
    ignored, fi = intake(Store(store.db.client['dm-ignored']))
    ignored.settings = dataclasses.replace(ignored.settings, slack_dm_chats=False)

    # when
    cap_decision = capped.handle(dm_event())
    off_decision = off.handle(dm_event())
    ignored_decision = ignored.handle(dm_event())

    # then
    assert busy == 'busy' and f.post.calls[-1]['text'].startswith(msg('slackevents.busy') + '\nYour chat history is on the site: ') and 'thread_ts' not in f.post.calls[-1]
    assert len(store.list_actions('requested', 5)) == 1
    assert cap_decision == 'dm-cap' and fc.post.calls[-1]['text'].startswith(msg('slackevents.dm.capped', cap=3) + '\n')
    assert off_decision == 'dm-off' and fo.post.calls[-1]['text'].startswith(msg('slackevents.dm.off') + '\n')
    assert ignored_decision is None and fi.post.calls == []


def test_small_talk_and_off_topic_dms_are_kept_in_the_chat_like_on_the_chat_tab(store: Store):
    # given
    box, f = intake(store, gate_replies=[verdict(on_topic=False, small_talk=True), reply(text('Hi, I am here 👋'), stop='end_turn'), verdict(on_topic=False)])

    # when
    talk = box.handle(dm_event('hi there'))
    refused = box.handle(dm_event('write me a poem', ts=LATER))
    chat = store.chat('adam.baker@example.com', store.chats('adam.baker@example.com', 5)[0]['id'])

    # then
    assert (talk, refused) == ('small-talk', 'off-topic')
    assert [p.get('thread_ts') for p in f.post.calls] == [None, None]
    assert [(m['text'], m['answer']) for m in chat['messages']] == [('hi there', 'Hi, I am here 👋'), ('write me a poem', OFF_TOPIC)]
    assert all(r.get('question') is None and r.get('smallTalk') is None for r in store.list_intake(5))


def test_a_dm_answer_goes_to_the_dm_without_a_mention_and_the_investigator_says_where_the_chat_is(store: Store):
    # given
    action = {'id': 'a1', 'slack': {'where': 'dm', 'channel': DM, 'threadTs': None, 'user': 'U1'}}
    act = store.add_action({'type': 'ask', 'source': 'chat', 'key': 'ask', 'target': 'chat:c9', 'text': 'Why is it slow?', 'cards': [], 'requestedBy': 'adam.baker@example.com',
                            'chat': {'id': 'c9', 'dm': DM}, 'thread': [], 'slack': {'where': 'dm', 'channel': DM, 'threadTs': None, 'ts': TS, 'user': 'U1', 'placeholderTs': '1.2'}})
    inv, seen = slack_investigator(store, [reply(text('*checkout-service* p95 1.2 s (promql, 10:05 UTC).'), stop='end_turn')])

    # when
    said = Replier.text_for(action, 'done', 'p95 *1.2 s*')
    record = inv.run_once(INV_NOW)

    # then
    assert said == 'p95 *1.2 s*'
    assert record['trigger'] == 'chat' and record['ask']['source'] == 'chat'
    assert 'in a Slack direct message to the Skopax bot' in first_brief(inv)
    assert [(a['id'], outcome, recap) for a, outcome, recap in seen] == [(act['id'], 'done', '*checkout-service* p95 1.2 s (promql, 10:05 UTC).')]
    assert store.chat('adam.baker@example.com', 'c9')['messages'][0]['answer'] == '*checkout-service* p95 1.2 s (promql, 10:05 UTC).'


def test_dm_info_lines_link_the_site_chat_and_every_dm_answer_links_its_chat_there(store: Store):
    # given
    capped_store = Store(store.db.client['dm-link-capped'])
    capped_store.add_action({'type': 'ask', 'source': 'chat', 'key': 'ask', 'target': 'chat:c0', 'text': 'q', 'requestedBy': 'adam.baker@example.com', 'chat': {'id': 'c0'}}, 'done', 'a')
    capped, fc = intake(capped_store)
    capped.settings = dataclasses.replace(capped.settings, chat_daily_cap=1)
    slack, post, _ = fake_slack()
    replier = Replier(store, slack, dataclasses.replace(settings(), slack_listen='live'), base_url='https://skopax.example.com')
    dm_act = store.add_action({'type': 'ask', 'source': 'chat', 'key': 'ask', 'target': 'chat:c7', 'text': 'q', 'requestedBy': 'adam.baker@example.com', 'chat': {'id': 'c7', 'dm': DM},
                               'slack': {'where': 'dm', 'channel': DM, 'threadTs': None, 'ts': TS, 'user': 'U1'}})
    channel_act = store.add_action({'type': 'ask', 'source': 'slack', 'key': 'ask', 'target': 'slack:C1:1', 'text': 'q', 'requestedBy': 'adam.baker@example.com',
                                    'slack': {'where': 'channel', 'channel': 'C1', 'threadTs': '1.1', 'ts': '1.1', 'user': 'U1'}})

    # when
    capped.handle(dm_event())
    replier.reply(dm_act, 'done', 'p95 1.2 s')
    replier.reply(channel_act, 'done', 'p95 1.2 s')

    # then
    assert fc.post.calls[-1]['text'].endswith('\nYour chat history is on the site: <https://skopax.example.com/chat/|Skopax Chat>')
    dm_blocks, channel_blocks = post.calls[0]['blocks'], post.calls[1]['blocks']
    assert dm_blocks[-1] == {'type': 'context', 'elements': [{'type': 'mrkdwn', 'text': "💬 <https://skopax.example.com/chat/thread/c7|Open this chat in Skopax Chat>"}]}
    assert 'thread_ts' not in post.calls[0] and all(b['type'] != 'context' for b in channel_blocks)


def test_a_dm_chat_shows_in_the_site_history_marked_slack_and_cannot_be_shared(store: Store):
    # given
    box, _ = intake(store, gate_replies=[verdict()])
    box.handle(dm_event('why did trino go down'))
    dm_id = store.chats('adam.baker@example.com', 5)[0]['id']
    store.add_action({'type': 'ask', 'source': 'chat', 'key': 'ask', 'target': 'chat:site1', 'text': 'site question', 'requestedBy': 'adam.baker@example.com', 'chat': {'id': 'site1'}}, 'done', 'a')

    # when
    listed = {c['id']: c for c in store.chats('adam.baker@example.com', 5)}
    refused = pytest.raises(ChatPrivate, store.share_chat, 'adam.baker@example.com', dm_id, True)
    shared = store.share_chat('adam.baker@example.com', 'site1', True)

    # then
    assert (listed[dm_id]['title'], listed[dm_id]['slack']) == ('Slack · why did trino go down', True)
    assert listed['site1']['title'] == 'site question' and 'slack' not in listed['site1']
    assert store.chat('adam.baker@example.com', dm_id)['slack'] is True
    assert 'stays private' in str(refused.value) and not store.chat_shared(dm_id)
    assert shared is True
    assert store.chat_for('nina.white@example.com', dm_id) is None


def test_sharing_a_dm_chat_from_the_chat_page_answers_403_with_the_reason_in_the_reader_s_language(store: Store, tmp_path):
    # given
    import asyncio
    from fastapi import HTTPException
    from skopax import chatui
    from skopax.app import create_app
    from skopax.chatui.layer import ChatDataLayer
    from skopax.kube import Kube
    box, _ = intake(store, gate_replies=[verdict()])
    box.handle(dm_event('why did trino go down'))
    dm_id = store.chats('adam.baker@example.com', 5)[0]['id']
    layer = ChatDataLayer(create_app(settings(), store, kube=Kube(root=tmp_path)).state.chat_bridge)

    def share(lang: str) -> HTTPException:
        token = chatui.REQUEST_LANG.set(lang)
        try:
            with pytest.raises(HTTPException) as e:
                asyncio.run(layer.update_thread(dm_id, metadata={'is_shared': True, 'shared_at': '2026-11-11T13:31:45Z'}))
            return e.value
        finally:
            chatui.REQUEST_LANG.reset(token)

    # when
    other, en = share('nl'), share('en')

    # then
    assert (other.status_code, other.detail) == (403, 'This chat started in a Slack direct message, so it stays private and cannot be shared.')
    assert en.detail == 'This chat started in a Slack direct message, so it stays private and cannot be shared.'
    assert not store.chat_shared(dm_id), 'Chainlit gets the refusal instead of a share link that can only fail'


def test_a_slack_dm_follows_the_admin_chat_message_length(store: Store):
    # given
    box, f = intake(store, gate_replies=[verdict()])
    box.settings = dataclasses.replace(box.settings, chat_max_chars=20)

    # when
    decision = box.handle(dm_event('this message will be longer than twenty characters'))

    # then
    assert decision == 'dm-long' and f.post.calls[-1]['text'].startswith(msg('slackevents.dm.long', limit=20))
