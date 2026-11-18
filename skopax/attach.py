import logging
from collections.abc import Callable

from pymongo.errors import PyMongoError

from skopax.errkind import Authored, kind, text_of
from skopax.messages import msg
from skopax.slack import SlackError, shot_links
from skopax.store import NotFound

log = logging.getLogger('skopax.attach')

MAX_SHOTS = 3
TITLE_LIMIT = 200


class Skip(Authored, Exception):
    pass


def unique(ids) -> list[str]:
    return list(dict.fromkeys(str(i) for i in ids if i))[:MAX_SHOTS]


def before_actions(blocks: list[dict], extra: list[dict]) -> list[dict]:
    at = next((i for i, b in enumerate(blocks) if b.get('type') == 'actions'), len(blocks))
    return blocks[:at] + extra + blocks[at:]


def with_blocks(message: dict, extra: list[dict]) -> dict:
    if message.get('attachments'):
        first, *rest = message['attachments']
        return message | {'attachments': [first | {'blocks': before_actions(first.get('blocks') or [], extra)}, *rest]}
    if message.get('blocks'):
        return message | {'blocks': before_actions(message['blocks'], extra)}
    return message | {'attachments': [{'blocks': extra}]}


class Attacher:
    def __init__(self, store, slack, enabled: bool | Callable[[], bool], base_url: str):
        self.store = store
        self.slack = slack
        self.enabled = enabled
        self.base_url = base_url.rstrip('/')

    def on(self) -> bool:
        return bool(self.enabled() if callable(self.enabled) else self.enabled)

    def __call__(self, channel: str, thread_ts: str, ids: list[str], where: str, run_id: str | None = None) -> str:
        ids = unique(ids)
        if not ids:
            return msg('screenshot.attach.none')
        return self._log(where, self._thread(channel, thread_ts, ids), run_id)

    def post(self, message: dict, shots: list[dict], where: str, run_id: str | None = None, sign: bool = True) -> tuple[dict, str | None]:
        picked = [s for s in shots if isinstance(s, dict) and s.get('id')]
        ids = unique(s['id'] for s in picked)
        if not ids:
            return self.slack.post(message, sign=sign), None
        line = shot_links([next(s for s in picked if s['id'] == i) for i in ids], self.base_url)
        sent = self.slack.post(with_blocks(message, [{'type': 'context', 'elements': [{'type': 'mrkdwn', 'text': line}]}]), sign=sign)
        return sent, self._log(where, self._thread(sent['channel'], message.get('thread_ts') or sent['ts'], ids), run_id)

    def _log(self, where: str, line: str, run_id: str | None) -> str:
        try:
            self.store.add_activity('slack', 'investigator', f'{where} \u00b7 {line}', runId=run_id)
        except PyMongoError as e:
            log.warning('the screenshot attach line of %s was not stored (%s)', where, kind(e))
        return line

    def _docs(self, ids: list[str]) -> list[dict]:
        if not self.on():
            raise Skip(msg('screenshot.attach.off'))
        if not self.slack.enabled:
            raise Skip(msg('screenshot.attach.no_slack'))
        if 'files:write' not in self.slack.scopes():
            raise Skip(msg('screenshot.attach.no_scope'))
        docs = []
        for shot_id in ids:
            try:
                docs.append(self.store.screenshot(shot_id))
            except NotFound:
                continue
        if not docs:
            raise Skip(msg('screenshot.attach.gone', days=self.store.screenshot_days))
        return docs

    @staticmethod
    def _file(d: dict) -> tuple[str, bytes, str]:
        return f"screenshot-{d.get('viewport') or 'desktop'}-{d['_id']}.jpg", bytes(d['data']), str(d.get('title') or d.get('finalUrl') or d['_id'])[:TITLE_LIMIT]

    def _thread(self, channel: str, thread_ts: str, ids: list[str]) -> str:
        try:
            docs = self._docs(ids)
            self.slack.attach(channel, thread_ts, [self._file(d) for d in docs])
        except (Skip, SlackError) as e:
            return msg('screenshot.attach.skipped', why=text_of(e))
        except PyMongoError as e:
            return msg('screenshot.attach.skipped', why=kind(e))
        return msg('screenshot.attach.done', count=len(docs), plural='' if len(docs) == 1 else 's', channel=self.slack.channel)
