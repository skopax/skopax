import logging
import time
from collections.abc import Callable
from dataclasses import dataclass

import anthropic

from skopax.aikey import ClientBox
from skopax.config import Setting
from skopax.engine import iso as epoch_iso
from skopax import clock
from skopax.contract import OrgPack
from skopax.errkind import kind as error_kind
from skopax.investigator import pricing, prompt
from skopax.investigator.gate import GATE_TIMEOUT

log = logging.getLogger('skopax.smalltalk')
REPLY_MAX_TOKENS = 300
REPLY_LIMIT = 1000
WINDOW_S = 3600
DEFAULTS = {'enabled': True, 'rules': prompt.SMALL_TALK_RULES, 'hourlyCap': 10, 'updatedAt': None, 'updatedBy': None}


def asker(by: str | None) -> str | None:
    by = str(by or '').strip()
    return by.split('@')[0] if '@' in by else None


@dataclass(frozen=True)
class Reply:
    text: str
    usd: float


class SmallTalk:
    model = Setting('investigate_gate_model')
    org = Setting('org_about')

    def __init__(self, store, settings, client=None, clock=time.time, key: Callable[[], str] | None = None, pack: Callable[[], OrgPack | None] | None = None):
        self.store = store
        self.settings = settings
        self.key = key or (lambda: settings.anthropic_api_key)
        self.box = ClientBox(self.key, lambda k: anthropic.Anthropic(api_key=k, timeout=GATE_TIMEOUT, max_retries=0), client)
        self.clock = clock
        self.pack = pack or (lambda: None)

    @property
    def enabled(self) -> bool:
        return bool(self.key()) and pricing.known(self.model)

    def _client(self):
        return self.box.get()

    def rules(self) -> dict:
        return {**DEFAULTS, **(self.store.small_talk_rules() or {})}

    def blocked(self, now: float) -> str | None:
        rules = self.rules()
        if not self.enabled or not rules['enabled']:
            return 'disabled'
        if self.store.count_small_talk(epoch_iso(now - WINDOW_S)) >= rules['hourlyCap']:
            return 'cap'
        return None

    def reply(self, text: str, talk_for: str, by: str | None = None, keys: list[str] | tuple = ()) -> Reply | None:
        if not self.enabled:
            return None
        name = asker(by)
        system = prompt.small_talk_system(self.org, self.pack()) + self.rules()['rules'] + (prompt.SMALL_TALK_ASKER.format(name=name) if name else '')
        try:
            client = self._client()
            if client is None:
                return None
            resp = client.messages.create(model=self.model, max_tokens=REPLY_MAX_TOKENS, system=system,
                                                  messages=[{'role': 'user', 'content': text}], thinking={'type': 'disabled'}, output_config={'effort': 'low'})
        except Exception as e:
            log.warning('small talk call failed: %s', error_kind(e))
            return None
        try:
            usd = pricing.usd(self.model, resp.usage)
        except Exception as e:
            log.warning('small talk usage could not be priced: %s', error_kind(e))
            return None
        day = clock.day(self.clock())
        self.store.add_spend(day, usd)
        self._ledger(day, resp.usage, talk_for, by, keys)
        if resp.stop_reason == 'refusal':
            return None
        answer = '\n'.join(b.text for b in resp.content if b.type == 'text').strip()
        return Reply(answer[:REPLY_LIMIT], usd) if answer else None

    def _ledger(self, day: str, usage, talk_for: str, by: str | None, keys) -> None:
        try:
            self.store.add_ledger({'source': 'smalltalk', 'talkFor': talk_for, 'day': day, 'model': self.model, 'effort': 'low', 'runId': None, 'actionId': None,
                                   'keys': list(keys), 'by': by, **pricing.split(self.model, usage)})
        except Exception as e:
            log.warning('spend ledger row for a %s small talk call not written: %s', talk_for, error_kind(e))
