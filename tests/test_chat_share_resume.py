import asyncio

import pytest

import chainlit.socket as socket
from chainlit.session import WebsocketSession
from chainlit.user import PersistedUser
from chainlit.user_session import user_sessions
from fastapi.testclient import TestClient

from tests.conftest import anomaly, ready_app, settings, state
from skopax import chatui
from skopax.app import create_app
from skopax.chatui.layer import ChatDataLayer
from skopax.kube import Kube
from skopax.store import NotFound, Store

KEY = 'log|checkout-service|ERROR|boom'
NINA = 'nina.white@example.com'
THREAD = 'aaaabbbb-cccc-4ddd-8eee-ffff00001111'


async def emit(*args, **kwargs):
    return None


def layer_of(store: Store, tmp_path) -> ChatDataLayer:
    store.save({**state(anomaly(KEY, num=14)), 'initialized': True}, None)
    bridge = ready_app(settings(), store, kube=Kube(root=tmp_path)).state.chat_bridge
    bridge.post(NINA, 'Why is this card open?', THREAD, [KEY])
    store.update_action(store.list_actions('requested', 10)[0]['id'], 'done', 'private answer', 'requested')
    return ChatDataLayer(bridge)


def reopen_share_and_close(store: Store, layer: ChatDataLayer, tab: str, on: bool) -> tuple[bool, bool]:
    async def scenario():
        user = PersistedUser(id=NINA, createdAt='2026-11-10T00:00:00Z', identifier=NINA, metadata={})
        session = WebsocketSession(id=tab, socket_id=tab, emit=emit, emit_call=emit, user_env={}, client_type='webapp', user=user, thread_id=THREAD)
        try:
            await socket.resume_thread(session)
            thread = await layer.get_thread(THREAD)
            await layer.update_thread(thread_id=THREAD, metadata=thread['metadata'] | {'is_shared': on} | ({'shared_at': '2026-11-10T12:00:00Z'} if on else {}))
            clicked = store.chat_shared(THREAD)
            await socket.persist_user_session(THREAD, session.to_persistable())
            return clicked, store.chat_shared(THREAD)
        finally:
            user_sessions.pop(tab, None)
            await session.delete()

    return asyncio.run(scenario())


def test_closing_a_reopened_chat_keeps_the_sharing_its_owner_set_while_it_was_open(store: Store, tmp_path, monkeypatch):
    # given
    layer = layer_of(store, tmp_path)
    monkeypatch.setattr(socket, 'get_data_layer', lambda: layer)
    store.share_chat(NINA, THREAD, True)

    # when
    stopped = reopen_share_and_close(store, layer, 'tab-1', False)
    started = reopen_share_and_close(store, layer, 'tab-2', True)

    # then
    assert stopped == (False, False), 'stop sharing, then close the tab: the chat stays private'
    assert started == (True, True), 'share, then close the tab: the chat stays shared'


def test_only_the_share_route_s_metadata_changes_the_sharing(store: Store, tmp_path):
    # given
    layer = layer_of(store, tmp_path)

    async def scenario():
        await layer.update_thread(thread_id=THREAD, metadata={'is_shared': True, 'client_type': 'webapp', 'chat_settings': {}, 'chat_profile': None, 'env': {}})
        saved = store.chat_shared(THREAD)
        await layer.update_thread(thread_id=THREAD, metadata={'is_shared': True, 'shared_at': '2026-11-10T12:00:00Z'})
        shared = store.chat_shared(THREAD)
        await layer.update_thread(thread_id=THREAD, metadata={'is_shared': False})
        return saved, shared, store.chat_shared(THREAD)

    # when
    saved, shared, stopped = asyncio.run(scenario())

    # then
    assert (saved, shared, stopped) == (False, True, False)


def test_a_shared_chat_is_a_snapshot_so_messages_after_sharing_stay_private_until_it_is_shared_again(store: Store, tmp_path):
    # given
    layer = layer_of(store, tmp_path)
    store.share_chat(NINA, THREAD, True)
    layer.bridge.post(NINA, 'And what should I tell my own team?', THREAD, None)
    store.update_action(store.list_actions('requested', 10)[0]['id'], 'done', 'the answer after sharing', 'requested')

    # when
    other = store.chat_for('paul.stone@example.com', THREAD)
    mine = store.chat_for(NINA, THREAD)
    token = chatui.SHARED_VIEW.set(True)
    try:
        link = layer.thread(THREAD)
    finally:
        chatui.SHARED_VIEW.reset(token)
    own_view = layer.thread(THREAD)
    store.share_chat(NINA, THREAD, False)
    store.share_chat(NINA, THREAD, True)
    again = store.chat_for('paul.stone@example.com', THREAD)

    # then
    assert [m['text'] for m in other['messages']] == ['Why is this card open?'] and other['snapshotAt'] and not other['mine']
    assert [m['text'] for m in mine['messages']] == ['Why is this card open?', 'And what should I tell my own team?']
    shown = ' '.join(str(s.get('output')) for s in link['steps'])
    assert 'private answer' in shown and 'the answer after sharing' not in shown and 'And my own team' not in shown
    assert 'the answer after sharing' in ' '.join(str(s.get('output')) for s in own_view['steps'])
    assert len(again['messages']) == 2


def test_a_reader_continues_a_shared_snapshot_in_a_new_private_chat_of_their_own(store: Store, tmp_path):
    # given
    layer = layer_of(store, tmp_path)
    store.share_chat(NINA, THREAD, True)
    layer.bridge.post(NINA, 'The next private question', THREAD, None)
    store.update_action(store.list_actions('requested', 10)[0]['id'], 'done', 'the next private answer', 'requested')
    reader = 'paul.stone@example.com'

    # when
    token = chatui.SHARED_VIEW.set('nl')
    try:
        link = layer.thread(THREAD)['steps'][-1]['output']
    finally:
        chatui.SHARED_VIEW.reset(token)
    fresh = store.continue_chat(reader, THREAD)
    mine = store.chat(reader, fresh)
    layer.bridge.post(reader, 'And how do we fix it?', fresh, None)
    asked = store.list_actions('requested', 10)[0]
    private = pytest.raises(NotFound, store.continue_chat, reader, 'not-shared-chat-0001')

    # then
    assert link == f'This is a snapshot of a shared chat. [Continue it in your own private chat ↗](/chat-continue/{THREAD})'
    assert fresh != THREAD and store.chat_owner(fresh) == reader
    assert [(m['text'], m['answer']) for m in mine['messages']] == [('Why is this card open?', 'private answer')], 'only the snapshot, never what the owner wrote after sharing'
    assert asked['chat']['id'] == fresh and asked['thread'] == [{'who': 'person', 'text': 'Why is this card open?'}, {'who': 'bot', 'text': 'private answer'}]
    assert store.count_actions('ask', '2000-01-01T00:00:00Z', source='chat', by=reader) == 1, 'copied messages do not use the daily chat limit'
    assert [m['text'] for m in store.chat(NINA, THREAD)['messages']] == ['Why is this card open?', 'The next private question'] and not store.chat_shared(fresh)
    assert 'not shared' in str(private.value)


def test_the_continue_link_sends_someone_without_a_session_to_log_in_first(store: Store, tmp_path):
    # given
    client = TestClient(create_app(settings(), store, kube=Kube(root=tmp_path)))

    # when
    r = client.get(f'/chat-continue/{THREAD}', follow_redirects=False)

    # then
    assert r.status_code == 303 and r.headers['location'] == f'/login?next=/chat-continue/{THREAD}'


def test_the_share_link_chainlit_copies_without_the_chat_prefix_lands_on_the_chat_share_page_and_a_logged_out_reader_logs_in_first(store: Store, tmp_path):
    # given
    client = TestClient(create_app(settings(), store, kube=Kube(root=tmp_path)))
    html = {'Accept': 'text/html,application/xhtml+xml'}

    # when
    copied = client.get(f'/share/{THREAD}', follow_redirects=False)
    odd = client.get('/share/..%2Fapi', follow_redirects=False)
    page = client.get(f'/chat/share/{THREAD}', headers=html, follow_redirects=False)
    thread = client.get(f'/chat/thread/{THREAD}', headers=html, follow_redirects=False)
    api = client.get(f'/chat/project/share/{THREAD}', follow_redirects=False)

    # then
    assert copied.status_code == 303 and copied.headers['location'] == f'/chat/share/{THREAD}'
    assert odd.status_code == 404
    assert page.status_code == 303 and page.headers['location'] == f'/login?next=/chat/share/{THREAD}', 'a browser without a session goes to log in and comes back'
    assert thread.status_code == 303 and thread.headers['location'] == f'/login?next=/chat/thread/{THREAD}'
    assert api.status_code == 401, 'the chat API itself still answers 401'
