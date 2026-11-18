import asyncio
from types import SimpleNamespace as NS

import chainlit as cl
from chainlit.context import init_ws_context
from chainlit.session import WebsocketSession
from chainlit.user import PersistedUser
from chainlit.user_session import user_sessions

from tests.conftest import anomaly, ready_app, settings, state
from skopax import chatui
from skopax.app import create_app
from skopax.auth import SESSION_COOKIE
from skopax.chatui import handlers
from skopax.chatui.text import TEXTS
from skopax.kube import Kube
from skopax.sessions import Sessions, session_secret
from skopax.store import Store
from skopax.users import Users

ALICE = 'alice@example.com'
BOB = 'bob@example.com'
CAROL = 'carol@example.com'
THREAD = 'aaaabbbb-cccc-4ddd-8eee-ffff00002222'
KEY = 'log|checkout-service|ERROR|boom'
LOGIN = TEXTS['en']['login'].format(product='Skopax')


async def emit(*args, **kwargs):
    return None


def chat_app(store: Store, tmp_path, monkeypatch) -> list[str]:
    store.save({**state(anomaly(KEY, num=14)), 'initialized': True}, None)
    store.card_ids.insert_one({'_id': KEY, 'n': 14})
    users = Users(store.db)
    users.create(ALICE, 'Alice', 'operator')
    users.create(BOB, 'Bob', 'operator')
    users.create(CAROL, 'Carol', 'viewer')
    app = ready_app(settings(), store, kube=Kube(root=tmp_path))
    monkeypatch.setattr(chatui, 'BRIDGE', app.state.chat_bridge)
    monkeypatch.setattr('skopax.app.USER_CACHE_SECONDS', 0)
    monkeypatch.setattr(handlers, 'WAIT_LIMIT_S', 0)
    sent = []

    async def send(self):
        sent.append(self.content)
        return self

    monkeypatch.setattr(cl.Message, 'send', send)
    return sent


def cookie_of(store: Store, email: str, role: str) -> str:
    return Sessions(session_secret(store.db, settings()), 3600, 7200).issue(email, role, 'local')


def in_chat(store: Store, email: str, role: str, call) -> None:
    async def scenario():
        user = PersistedUser(id=email, createdAt='2026-11-10T00:00:00Z', identifier=email, metadata={})
        session = WebsocketSession(id='tab-access', socket_id='tab-access', emit=emit, emit_call=emit, user_env={}, client_type='webapp', user=user, thread_id=THREAD,
                                   environ={'HTTP_COOKIE': f'{SESSION_COOKIE}={cookie_of(store, email, role)}'})
        init_ws_context(session)
        try:
            await call()
        finally:
            user_sessions.pop('tab-access', None)
            await session.delete()

    asyncio.run(scenario())


async def removed():
    return None


def asks(store: Store) -> list[str]:
    return [a['requestedBy'] for a in store.list_actions(None, 50)]


def test_an_open_chat_refuses_a_user_disabled_after_it_opened(store: Store, tmp_path, monkeypatch):
    # given
    sent = chat_app(store, tmp_path, monkeypatch)
    in_chat(store, ALICE, 'operator', lambda: handlers.answer(NS(content='Why is #14 open?')))
    before = asks(store)
    Users(store.db).update(ALICE, {'disabled': True})
    sent.clear()

    # when
    in_chat(store, ALICE, 'operator', lambda: handlers.answer(NS(content='And now?')))
    in_chat(store, ALICE, 'operator', lambda: handlers.apply(NS(payload={'key': KEY, 'action': 'investigate'}, remove=removed)))

    # then
    assert before == [ALICE], 'an active operator still asks from the chat'
    assert sent == [LOGIN, LOGIN]
    assert asks(store) == [ALICE], 'nothing new was queued or applied for the disabled user'


def test_the_chat_refuses_a_viewer_and_an_unknown_user(store: Store, tmp_path, monkeypatch):
    # given
    sent = chat_app(store, tmp_path, monkeypatch)

    # when
    in_chat(store, CAROL, 'viewer', lambda: handlers.answer(NS(content='Why is #14 open?')))
    in_chat(store, CAROL, 'viewer', lambda: handlers.apply(NS(payload={'key': KEY, 'action': 'investigate'}, remove=removed)))
    in_chat(store, 'nobody@example.com', 'operator', lambda: handlers.answer(NS(content='Why is #14 open?')))

    # then
    assert sent == [LOGIN] * 3
    assert asks(store) == []


def test_the_chat_acts_as_the_cookie_s_user_and_refuses_when_it_is_not_the_chat_s_user(store: Store, tmp_path, monkeypatch):
    # given
    sent = chat_app(store, tmp_path, monkeypatch)

    async def mixed():
        cl.context.session.environ['HTTP_COOKIE'] = f'{SESSION_COOKIE}={cookie_of(store, BOB, "operator")}'
        await handlers.answer(NS(content='Why is #14 open?'))

    # when
    in_chat(store, ALICE, 'operator', mixed)

    # then
    assert sent == [LOGIN]
    assert asks(store) == []
