import threading

from skopax.contract import Capabilities, Field, Outbound, Plugin
from tests.plugins.base import minimal

FIELDS = (Field('url', 'URL', 'url', required=True), Field('tags', 'Tags', 'text'), Field('keys', 'Card keys', 'text'), Field('mentions', 'Mentions', 'text'))


class Hook:
    def __init__(self, answer: object = None, hold: threading.Event | None = None):
        self.posts: list[tuple[str, dict]] = []
        self.answer = {'ok': True, 'status': 200} if answer is None else answer
        self.hold = hold

    def post(self, ctx, message: dict) -> object:
        self.posts.append((ctx.instance.name, message))
        if self.hold is not None:
            self.hold.wait(5)
        if isinstance(self.answer, BaseException):
            raise self.answer
        return self.answer


def hook_plugin(hook: Hook, **over) -> Plugin:
    values = dict(category='chat', fields=FIELDS, capabilities=Capabilities(hosts=('@url',)), outbound=Outbound(hook.post, 'tags', 'mentions', link_field='url', keys_field='keys'))
    return minimal('hook', 'Hook', **(values | over))
