import asyncio
import logging

from chainlit.data.base import BaseDataLayer
from chainlit.types import PageInfo, PaginatedResponse
from chainlit.user import PersistedUser
from chainlit.utils import utc_now
from fastapi import HTTPException

from skopax import brand, chatui
from skopax.chatui.text import OPEN, about_line, answer_of, continue_line, said
from skopax.errkind import kind as error_kind
from skopax.store import ChatPrivate

log = logging.getLogger('skopax.chatui.layer')

SHARE_KEYS = {'is_shared', 'shared_at'}


def steps_of(chat: dict, lang: str = 'en') -> list[dict]:
    steps = []
    cards = [c for c in chat.get('cards') or [] if isinstance(c, dict)]
    first = (chat.get('messages') or [{}])[0].get('at')
    if cards:
        steps.append({'id': f"{chat['id']}-about", 'threadId': chat['id'], 'parentId': None, 'name': brand.product(), 'type': 'assistant_message', 'output': about_line(cards),
                      'createdAt': first, 'start': first, 'end': first, 'streaming': False, 'metadata': {}})
    for m in chat.get('messages') or []:
        steps.append({'id': f"{m['id']}-q", 'threadId': chat['id'], 'parentId': None, 'name': chat.get('by') or 'user', 'type': 'user_message', 'output': m.get('text') or '',
                      'createdAt': m.get('at'), 'start': m.get('at'), 'end': m.get('at'), 'streaming': False, 'metadata': {}})
        if m.get('status') in OPEN:
            continue
        at = m.get('answeredAt') or m.get('at')
        steps.append({'id': f"{m['id']}-a", 'threadId': chat['id'], 'parentId': None, 'name': brand.product(), 'type': 'assistant_message', 'output': answer_of(m, lang),
                      'createdAt': at, 'start': at, 'end': at, 'streaming': False, 'metadata': {}})
    return steps


def thread_of(chat: dict, owner: str, shared: bool, steps: bool = True, lang: str = 'en') -> dict:
    return {'id': chat['id'], 'createdAt': chat.get('startedAt') or ((chat.get('messages') or [{}])[0].get('at') if steps else None), 'name': chat.get('title') or said(lang, 'untitled'),
            'userId': owner, 'userIdentifier': owner, 'tags': [], 'metadata': {'is_shared': shared}, 'steps': steps_of(chat | {'by': owner}, lang) if steps else [], 'elements': []}


class ChatDataLayer(BaseDataLayer):
    def __init__(self, bridge: chatui.Bridge):
        self.bridge = bridge

    async def get_user(self, identifier: str):
        return None

    async def create_user(self, user):
        return PersistedUser(id=user.identifier, createdAt=utc_now(), identifier=user.identifier, display_name=user.display_name, metadata=dict(user.metadata or {}))

    async def list_threads(self, pagination, filters):
        return await asyncio.to_thread(self.threads, pagination, filters)

    def threads(self, pagination, filters) -> PaginatedResponse:
        rows = self.bridge.chats(filters.userId) if filters.userId else []
        wanted = (filters.search or '').strip().lower()
        rows = [c for c in rows if not wanted or wanted in str(c.get('title') or '').lower()]
        lang = chatui.REQUEST_LANG.get()
        data = [thread_of(c, filters.userId, self.bridge.shared(c['id']), steps=False, lang=lang) for c in rows[:pagination.first]]
        return PaginatedResponse(pageInfo=PageInfo(hasNextPage=False, startCursor=None, endCursor=None), data=data)

    async def get_thread(self, thread_id: str):
        return await asyncio.to_thread(self.thread, thread_id)

    def thread(self, thread_id: str) -> dict | None:
        owner, view = self.bridge.owner(thread_id), chatui.SHARED_VIEW.get()
        chat = self.bridge.chat('' if view else owner, thread_id) if owner else None
        if not chat:
            return None
        lang = view if isinstance(view, str) else chatui.REQUEST_LANG.get()
        out = thread_of(chat, owner, self.bridge.shared(thread_id), lang=lang)
        if view:
            at = (chat.get('messages') or [{}])[-1].get('answeredAt') or chat.get('snapshotAt')
            out['steps'].append({'id': f'{thread_id}-continue', 'threadId': thread_id, 'parentId': None, 'name': brand.product(), 'type': 'assistant_message',
                                 'output': continue_line(lang, thread_id), 'createdAt': at, 'start': at, 'end': at, 'streaming': False, 'metadata': {}})
        return out

    async def get_thread_author(self, thread_id: str) -> str:
        return await asyncio.to_thread(self.bridge.owner, thread_id) or ''

    async def update_thread(self, thread_id: str, name=None, user_id=None, metadata=None, tags=None):
        if not isinstance(metadata, dict) or 'is_shared' not in metadata or set(metadata) - SHARE_KEYS:
            return
        await asyncio.to_thread(self.share, thread_id, bool(metadata['is_shared']))

    def share(self, thread_id: str, on: bool) -> None:
        owner = self.bridge.owner(thread_id)
        if owner and on != self.bridge.shared(thread_id):
            try:
                self.bridge.share(owner, thread_id, on)
            except ChatPrivate as e:
                log.info('share of chat %s refused: %s', thread_id, error_kind(e))
                raise HTTPException(status_code=403, detail=said(chatui.REQUEST_LANG.get(), 'private_share')) from None

    async def delete_thread(self, thread_id: str):
        await asyncio.to_thread(self.hide, thread_id)

    def hide(self, thread_id: str) -> None:
        owner = self.bridge.owner(thread_id)
        if owner:
            self.bridge.hide(owner, thread_id)

    async def create_step(self, step_dict):
        return None

    async def update_step(self, step_dict):
        return None

    async def delete_step(self, step_id: str):
        return None

    async def create_element(self, element):
        return None

    async def get_element(self, thread_id: str, element_id: str):
        return None

    async def delete_element(self, element_id: str, thread_id: str | None = None):
        return None

    async def upsert_feedback(self, feedback) -> str:
        return ''

    async def delete_feedback(self, feedback_id: str) -> bool:
        return True

    async def get_favorite_steps(self, user_id: str):
        return []

    async def build_debug_url(self) -> str:
        return ''

    async def close(self) -> None:
        return None
