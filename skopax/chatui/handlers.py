import asyncio
import time

import chainlit as cl

from skopax import brand, chatui
from skopax.auth import SESSION_COOKIE
from skopax.chatui.layer import ChatDataLayer
from skopax.chatui.text import OPEN, about_line, action_label, answer_of, card_ref, cookie_of, done_line, lang_of, pending_line, refusal, said, said_md, texts_of

LIST_AGAIN = '\u2060'
POLL_S = 1.5
WAIT_LIMIT_S = 900
RESUME_SETTLE_S = 1.0
FOLLOWING: set[asyncio.Task] = set()


def bridge() -> chatui.Bridge:
    return chatui.BRIDGE


def lang() -> str:
    environ = cl.context.session.environ
    return lang_of(environ.get('HTTP_COOKIE'), environ.get('HTTP_ACCEPT_LANGUAGE'))


@cl.header_auth_callback
async def login(headers) -> cl.User | None:
    who = await cl.make_async(bridge().user)(cookie_of(headers.get('cookie'), SESSION_COOKIE))
    if not who:
        return None
    return cl.User(identifier=who['login'], display_name=who['login'].split('@')[0], metadata={'operator': bool(who.get('operator'))})


@cl.data_layer
def data_layer() -> ChatDataLayer:
    return ChatDataLayer(bridge())


@cl.set_starters
async def starters(user=None, language=None):
    return [cl.Starter(label=label, message=message) for label, message in texts_of(chatui.REQUEST_LANG.get())['starters']]


@cl.on_chat_start
async def start() -> None:
    environ = cl.context.session.environ
    ref = cookie_of(environ.get('HTTP_COOKIE'), chatui.CARD_COOKIE) or card_ref(environ.get('HTTP_REFERER'))
    card = await cl.make_async(bridge().card)(ref) if ref else None
    if card:
        cl.user_session.set('card', card['key'])
        await cl.Message(content=about_line([card])).send()


@cl.on_chat_resume
async def resume(thread) -> asyncio.Task | None:
    user = cl.user_session.get('user')
    chat_id = (thread or {}).get('id')
    steps = (thread or {}).get('steps') or []
    if not user or not chat_id or not steps or steps[-1].get('type') != 'user_message':
        return None
    task = asyncio.create_task(follow_after_resume(user.identifier, chat_id, lang()))
    FOLLOWING.add(task)
    task.add_done_callback(FOLLOWING.discard)
    return task


async def follow_after_resume(by: str, chat_id: str, tongue: str) -> None:
    await asyncio.sleep(RESUME_SETTLE_S)
    chat = await cl.make_async(bridge().chat)(by, chat_id)
    if chat and chat.get('messages'):
        await follow(by, chat_id, chat, tongue)


@cl.on_shared_thread_view
async def shared_view(thread, viewer) -> bool:
    return viewer is not None and await cl.make_async(bridge().shared)(thread['id'])


async def operator() -> dict | None:
    who = await cl.make_async(bridge().user)(cookie_of(cl.context.session.environ.get('HTTP_COOKIE'), SESSION_COOKIE))
    user = cl.user_session.get('user')
    if not who or not who.get('operator') or user is None or user.identifier != who['login']:
        await cl.Message(content=said_md(lang(), 'login', product=brand.product())).send()
        return None
    return who


@cl.on_message
async def answer(message: cl.Message) -> None:
    who = await operator()
    if who is None:
        return
    by, chat_id, tongue = who['login'], cl.context.session.thread_id, lang()
    card = cl.user_session.get('card')
    try:
        chat = await cl.make_async(bridge().post)(by, message.content, chat_id, [card] if card else None)
    except chatui.Refused as e:
        await cl.Message(content=refusal(e, tongue)).send()
        return
    if len(chat.get('messages') or []) == 1:
        await cl.context.emitter.emit('first_interaction', {'interaction': message.content + LIST_AGAIN, 'thread_id': chat_id})
    await follow(by, chat_id, chat, tongue)


async def follow(by: str, chat_id: str, chat: dict, tongue: str) -> None:
    last, until = chat['messages'][-1], time.monotonic() + WAIT_LIMIT_S
    if last.get('status') in OPEN:
        async with cl.Step(name=brand.product(), type='tool') as step:
            while last.get('status') in OPEN and time.monotonic() < until:
                step.output = pending_line(last, chat.get('halted'), tongue)
                await step.update()
                await asyncio.sleep(POLL_S)
                chat = await cl.make_async(bridge().chat)(by, chat_id) or chat
                last = chat['messages'][-1]
            step.output = said_md(tongue, 'still') if last.get('status') in OPEN else done_line(last, tongue)
    if last.get('status') in OPEN:
        return
    proposed = last.get('cardAction')
    target = await cl.make_async(bridge().card)(proposed.get('key')) if proposed else None
    actions = [cl.Action(name='card_action', payload=proposed, label=action_label(proposed, target, tongue), tooltip=said(tongue, 'role_tip'))] if proposed else []
    await cl.Message(content=answer_of(last, tongue), actions=actions).send()


@cl.action_callback('card_action')
async def apply(action: cl.Action) -> None:
    who = await operator()
    if who is None:
        return
    tongue = lang()
    try:
        outcome, detail = await cl.make_async(bridge().apply)(who['login'], bool(who.get('operator')), dict(action.payload))
    except chatui.Refused as e:
        await cl.Message(content=refusal(e, tongue)).send()
        return
    await action.remove()
    await cl.Message(content=said_md(tongue, outcome, what=detail)).send()
