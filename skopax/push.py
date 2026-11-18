import logging
from collections.abc import Callable

from skopax.errkind import kind as error_kind
from skopax.messages import msg
from skopax.cardlink import card_url, chat_url
from skopax.engine import push_line
from skopax.slack import Slack, SlackError, esc

log = logging.getLogger('skopax.push')


def push_users(value: str) -> list[str]:
    return [u.strip() for u in value.split(',') if u.strip()]


def page_link(base: str, items: list[dict]) -> str:
    base = base.rstrip('/')
    return card_url(base, items[0]['key'], items[0].get('num')) if len(items) == 1 else f'{base}/#t=anoms'


class Pusher:
    def __init__(self, slack: Slack, users: str, base: str, on: Callable[[], bool] = lambda: True):
        self.slack = slack
        self.users = push_users(users)
        self.base = base
        self.on = on

    @property
    def enabled(self) -> bool:
        return bool(self.slack.token and self.users) and self.on()

    def text(self, text: str) -> list[str]:
        sent = []
        if not self.enabled:
            return sent
        for user in self.users:
            try:
                self.slack.post({'channel': user, 'text': text})
            except SlackError as e:
                log.warning('phone push DM to %s failed: %s', user, error_kind(e))
                continue
            sent.append(user)
        return sent

    def send(self, items: list[dict]) -> str | None:
        line = push_line(items)
        if not line or not self.enabled:
            return None
        chat = f" · <{chat_url(self.base, items[0]['key'], items[0].get('num'))}|Chat about this>" if len(items) == 1 else ''
        sent = self.text(f'{esc(line)}\n<{page_link(self.base, items)}|{msg('slack.open')}>{chat}')
        if not sent:
            return None
        log.info('phone push DM sent to %s for %s', ', '.join(sent), ', '.join(a['key'] for a in items))
        return line
