import json
import logging
import re
import time
from collections.abc import Callable
from dataclasses import dataclass

import anthropic

from skopax.ai import THINK_OFF, ModelSetting, api_errors, key_for, maker, wrap
from skopax.aikey import ClientBox
from skopax.config import Setting
from skopax import clock
from skopax.contract import OrgPack
from skopax.errkind import kind as error_kind
from skopax.investigator import pricing, prompt

log = logging.getLogger('skopax.gate')
GATE_TIMEOUT = 10.0
GATE_MAX_TOKENS = 300
REASON_LIMIT = 200
CARD_INTENTS = ('none', 'take', 'resolve', 'known', 'mute')
DEFAULT_LANGUAGE = 'en'
LANGUAGE = re.compile(r'[a-z]{2}')
SUBTAG = re.compile(r'[-_]')
GATE_SCHEMA = {'type': 'object', 'additionalProperties': False, 'required': ['reason', 'addressed', 'onTopic', 'smallTalk', 'cardIntent', 'agrees', 'mindBlown', 'great', 'language'],
               'properties': {'reason': {'type': 'string'}, 'addressed': {'type': 'boolean'}, 'onTopic': {'type': 'boolean'}, 'smallTalk': {'type': 'boolean'},
                              'cardIntent': {'type': 'string', 'enum': list(CARD_INTENTS)}, 'agrees': {'type': 'boolean'}, 'mindBlown': {'type': 'boolean'}, 'great': {'type': 'boolean'}, 'language': {'type': 'string', 'description': 'ISO 639-1 two-letter language code of the question, e.g. en'}}}
FLAGS = ('addressed', 'onTopic', 'smallTalk', 'agrees', 'mindBlown', 'great')
NO_KEY = 'no-key'
NO_PRICE = 'no-price'
REFUSED = 'refused'
SCHEMA = 'schema'
API = 'api:'


def language_of(value) -> str:
    code = SUBTAG.split(value.strip().lower(), maxsplit=1)[0] if isinstance(value, str) else ''
    return code if LANGUAGE.fullmatch(code) else DEFAULT_LANGUAGE


class GateDown(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class Verdict:
    addressed: bool
    on_topic: bool
    language: str
    reason: str
    usd: float = 0.0
    small_talk: bool = False
    card_intent: str = 'none'
    agrees: bool = False
    mind_blown: bool = False
    great: bool = False


EXAMPLES_DEFAULT = {'examples': '', 'updatedAt': None, 'updatedBy': None}


class Gate:
    provider = Setting('ai_provider')
    model = ModelSetting('investigate_gate_model', 'gate')
    effort = Setting('investigate_gate_effort')
    org = Setting('org_about')

    def __init__(self, store, settings, client=None, clock=time.time, key: Callable[[], str] | None = None, pack: Callable[[], OrgPack | None] | None = None):
        self.store = store
        self.settings = settings
        self.key = key or key_for(settings, lambda: self.provider)
        self.box = ClientBox(self.key, maker(lambda: self.provider, lambda k: anthropic.Anthropic(api_key=k, timeout=GATE_TIMEOUT, max_retries=0), GATE_TIMEOUT, 0), client,
                             lambda: self.provider)
        self.clock = clock
        self.pack = pack or (lambda: None)

    @property
    def enabled(self) -> bool:
        return bool(self.key()) and pricing.known(self.model)

    def examples(self) -> dict:
        return {**EXAMPLES_DEFAULT, **(self.store.gate_examples() or {})}

    def _client(self):
        return self.box.get()

    def check(self, text: str, gate_for: str = 'page', by: str | None = None, keys: list[str] | tuple = ()) -> Verdict:
        if not self.key():
            raise GateDown(NO_KEY)
        if not pricing.known(self.model):
            raise GateDown(NO_PRICE)
        try:
            ai = wrap(self._client())
            if ai is None:
                raise GateDown(NO_KEY)
            model = pricing.effective(ai.provider, self.model, 'gate')
            resp = ai.once(model, prompt.gate_system(self.examples()['examples'], self.org, self.pack()), [{'role': 'user', 'content': text}], GATE_MAX_TOKENS, self.effort, THINK_OFF, GATE_SCHEMA)
        except GateDown:
            raise
        except (anthropic.APIError, *api_errors()) as e:
            log.warning('gate call failed: %s', error_kind(e))
            raise GateDown(API + type(e).__name__) from None
        except Exception as e:
            log.warning('gate call failed unexpectedly: %s', error_kind(e))
            raise GateDown(API + type(e).__name__) from None
        try:
            usd = pricing.usd(model, resp.usage)
        except Exception as e:
            log.warning('gate usage could not be priced: %s', error_kind(e))
            raise GateDown(NO_PRICE) from None
        day = clock.day(self.clock())
        self.store.add_spend(day, usd)
        self._ledger(day, ai.provider, model, resp.usage, gate_for, by, keys)
        if resp.stop == 'refusal':
            raise GateDown(REFUSED)
        try:
            body = json.loads(next(iter(resp.texts)))
            if not all(isinstance(body[k], bool) for k in FLAGS) or body['cardIntent'] not in CARD_INTENTS:
                raise ValueError('addressed, onTopic, smallTalk, agrees, mindBlown and great must be booleans, cardIntent one of its enum')
            return Verdict(body['addressed'], body['onTopic'], language_of(body.get('language')), str(body['reason'])[:REASON_LIMIT], usd, body['smallTalk'], body['cardIntent'], body['agrees'],
                           body['mindBlown'], body['great'])
        except (StopIteration, ValueError, KeyError, TypeError):
            raise GateDown(SCHEMA) from None

    def _ledger(self, day: str, provider: str, model: str, usage, gate_for: str, by: str | None, keys) -> None:
        try:
            self.store.add_ledger({'source': 'gate', 'gateFor': gate_for, 'day': day, 'provider': provider, 'model': model, 'effort': None if provider == pricing.OPENAI else self.effort, 'runId': None, 'actionId': None,
                                   'keys': list(keys), 'by': by, **pricing.split(model, usage)})
        except Exception as e:
            log.warning('spend ledger row for a %s gate call not written: %s', gate_for, error_kind(e))
