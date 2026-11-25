import logging
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass

import anthropic

from skopax.ai import THINK_OFF, ModelSetting, clients_for, route, wrap
from skopax.config import Setting
from skopax.engine import iso as epoch_iso
from skopax import clock, tracing
from skopax.contract import OrgPack
from skopax.errkind import kind as error_kind
from skopax.investigator import pricing, prompt
from skopax.investigator.gate import GATE_TIMEOUT
from skopax.outward import outward

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
    provider = Setting('ai_provider')
    model = ModelSetting('investigate_gate_model', 'gate')
    org = Setting('org_about')

    def __init__(self, store, settings, client=None, clock=time.time, key: Callable[[], str] | None = None, pack: Callable[[], OrgPack | None] | None = None,
                 keys: Mapping[str, Callable[[], str]] | None = None):
        self.store = store
        self.settings = settings
        self.clients = clients_for(settings, key, lambda k: anthropic.Anthropic(api_key=k, timeout=GATE_TIMEOUT, max_retries=0), GATE_TIMEOUT, 0, client, keys)
        self.key = lambda: self.clients.key(route(self, 'investigate_gate_model', 'gate')[0])
        self.keyed = lambda: self.clients.keyed(route(self, 'investigate_gate_model', 'gate')[0])
        self.clock = clock
        self.pack = pack or (lambda: None)

    @property
    def enabled(self) -> bool:
        return self.keyed() and pricing.known(self.model)

    @property
    def box(self):
        return self.clients.box()

    def _client(self):
        return self.clients.get(route(self, 'investigate_gate_model', 'gate')[0])

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
        system = prompt.small_talk_prompt(self.org, self.pack(), self.rules()['rules'], name)
        try:
            ai = wrap(self._client())
            if ai is None:
                return None
            model = pricing.in_hand(ai.provider, self.model, 'gate')
            with tracing.opened('small talk', provider=ai.provider, talkFor=talk_for, cards=', '.join(keys)):
                resp = ai.once(model, system, [{'role': 'user', 'content': text}], REPLY_MAX_TOKENS, 'low', THINK_OFF)
        except Exception as e:
            log.warning('small talk call failed: %s', error_kind(e))
            return None
        try:
            usd, priced_as = pricing.charge(model, ai.provider, resp.usage)
        except Exception as e:
            log.warning('small talk usage could not be priced: %s', error_kind(e))
            return None
        day = clock.day(self.clock())
        self.store.add_spend(day, usd)
        self._ledger(day, ai.provider, model, resp.usage, talk_for, by, keys, priced_as)
        if resp.stop == 'refusal':
            return None
        answer = outward('\n'.join(resp.texts).strip())
        return Reply(answer[:REPLY_LIMIT], usd) if answer else None

    def _ledger(self, day: str, provider: str, model: str, usage, talk_for: str, by: str | None, keys, priced_as: str | None = None) -> None:
        try:
            self.store.add_ledger({'source': 'smalltalk', 'use': 'gate', 'talkFor': talk_for, 'day': day, 'provider': provider, 'model': model, 'effort': None if provider == pricing.OPENAI else 'low', 'runId': None, 'actionId': None,
                                   'keys': list(keys), 'by': by, **pricing.split(priced_as or model, usage)} | ({'pricedAs': priced_as} if priced_as else {}))
        except Exception as e:
            log.warning('spend ledger row for a %s small talk call not written: %s', talk_for, error_kind(e))
