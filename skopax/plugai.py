import logging
import time
from collections.abc import Callable, Mapping

import anthropic

from skopax import clock, tracing
from skopax.ai import THINK_AUTO, THINK_OFF, capabilities_of, clients_for, wrap
from skopax.contract import Check, InstanceInfo, Planted, Plugin, ReadError
from skopax.errkind import kind
from skopax.investigator import flags, pricing, prompt
from skopax.messages import msg
from skopax.postdeploy import SERVER_BY
from skopax.store import PLUGIN_NO_INSTANCE

log = logging.getLogger('skopax.plugai')

TIMEOUT_S = 90.0
RETRIES = 1
MAX_TOKENS = 16000
EFFORTS = ('low', 'medium', 'high', 'xhigh', 'max')
SOURCES = {('website', 'pagescan'): 'pagescan'}


def source_of(plugin_id: str, check_id: str) -> str:
    return SOURCES.get((plugin_id, check_id), f'{plugin_id}.{check_id}')


def texts_of(messages: list[dict]) -> str:
    out = []
    for m in messages:
        content = m.get('content')
        blocks = [content] if isinstance(content, str) else content if isinstance(content, list) else []
        out += [b if isinstance(b, str) else str(b.get('text') or '') for b in blocks if isinstance(b, str) or (isinstance(b, dict) and b.get('type') == 'text')]
    return '\n'.join(out)


def problem(system: object, messages: object, max_tokens: object, schema: object, effort: object) -> str | None:
    if not isinstance(system, str):
        return 'system'
    if not isinstance(messages, list) or not messages or not all(isinstance(m, dict) for m in messages):
        return 'messages'
    if not isinstance(max_tokens, int) or isinstance(max_tokens, bool) or not 1 <= max_tokens <= MAX_TOKENS:
        return 'max_tokens'
    if schema is not None and not isinstance(schema, dict):
        return 'schema'
    if effort not in EFFORTS:
        return 'effort'
    return None


class PluginAi:
    def __init__(self, store, key: Callable[[], str] | None, halted: Callable[[float], str | None], client: object | None = None, clock: Callable[[], float] = time.time,
                 provider: Callable[[], str] | None = None, keys: Mapping[str, Callable[[], str]] | None = None, given: Callable[[str], bool] | None = None):
        self.store = store
        self.halted = halted
        self.provider = provider or (lambda: pricing.ANTHROPIC)
        self.given = given or (lambda plugin_id: True)
        self.clients = clients_for(None, key, lambda k: anthropic.Anthropic(api_key=k, timeout=TIMEOUT_S, max_retries=RETRIES), TIMEOUT_S, RETRIES, client, keys, self.provider)
        self.clock = clock

    @property
    def box(self):
        return self.clients.box()

    def route(self, model: object, plugin_id: str | None = None) -> tuple[str, object, str | None]:
        if not isinstance(model, str):
            return self.clients.default(), model, None
        return self.clients.resolve(model, 'plugin', plugin_id is None or self.given(plugin_id))

    def model_of(self, model: object, plugin_id: str | None = None) -> object:
        return self.route(model, plugin_id)[1]

    def key(self, model: object = None, plugin_id: str | None = None) -> str:
        return self.clients.key(self.route(model, plugin_id)[0] if model is not None else self.clients.default())

    def ready(self, model: object, plugin_id: str | None = None) -> bool:
        provider, model, _ = self.route(model, plugin_id)
        return isinstance(model, str) and (bool(self.clients.key(provider)) or self.clients.keyed(provider)) and pricing.known(model) and self.halted(self.clock()) is None

    def for_check(self, plugin: Plugin, check: Check, info: InstanceInfo | None, model: object) -> 'AiCall | None':
        if not self.ready(model, plugin.id):
            return None
        return AiCall(self, plugin.id, PLUGIN_NO_INSTANCE if info is None else info.name, str(self.model_of(model, plugin.id)), source_of(plugin.id, check.id), str(model))


class AiCall:
    def __init__(self, ai: PluginAi, plugin_id: str, instance: str, model: str, source: str, ref: str | None = None):
        self.ai = ai
        self.plugin_id = plugin_id
        self.instance = instance
        self.model = model
        self.source = source
        self.ref = ref or model
        self.fallback_from: str | None = None

    def complete(self, system: str, messages: list[dict], max_tokens: int, schema: dict | None = None, effort: str = 'medium') -> dict | None:
        wrong = problem(system, messages, max_tokens, schema, effort)
        if wrong is not None:
            raise ReadError(400, msg('sweeps.ai_input', what=wrong))
        found = flags.scan(self.source, texts_of(messages), skip=flags.REQUESTS)
        if found:
            raise Planted(tuple(dict.fromkeys(f['phrase'] for f in found)))
        now = self.ai.clock()
        provider, model, self.fallback_from = self.ai.route(self.ref, self.plugin_id)
        if self.ai.halted(now) is not None or not self.ai.clients.keyed(provider):
            return None
        ai = wrap(self.ai.clients.get(provider))
        if ai is None:
            return None
        self.model = pricing.in_hand(ai.provider, model, 'plugin')
        try:
            with tracing.opened('plug-in AI', provider=ai.provider, plugin=self.plugin_id, instance=self.instance, source=self.source):
                resp = ai.once(self.model, system + prompt.confidential_block(), messages, max_tokens, effort, THINK_AUTO if 'thinking' in capabilities_of(ai) else THINK_OFF, schema)
        except Exception as e:
            log.warning('a plug-in model call for %s failed (%s)', self.source, kind(e))
            raise ReadError(502, msg('sweeps.ai_failed', kind=type(e).__name__)) from None
        try:
            usd, priced_as = pricing.charge(self.model, ai.provider, resp.usage)
        except Exception as e:
            log.warning('a plug-in model call for %s could not be priced (%s)', self.source, kind(e))
            raise ReadError(502, msg('sweeps.ai_failed', kind=type(e).__name__)) from None
        day = clock.day(now)
        self.ai.store.add_spend(day, usd)
        try:
            self.ai.store.add_ledger({'source': self.source, 'use': 'plugin', 'day': day, 'provider': ai.provider, 'model': self.model, 'effort': effort, 'runId': None, 'actionId': None, 'keys': [], 'by': SERVER_BY,
                                      'plugin': self.plugin_id, 'instance': self.instance, **pricing.split(priced_as or self.model, resp.usage)}
                                     | ({'pricedAs': priced_as} if priced_as else {}) | ({'fallbackFrom': self.fallback_from} if self.fallback_from else {}))
        except Exception as e:
            log.warning('spend ledger row for a plug-in model call for %s not written: %s', self.source, kind(e))
        if resp.stop == 'refusal':
            raise ReadError(502, msg('sweeps.ai_refused'))
        said = ''.join(resp.texts)
        return {'text': said, 'usd': usd}
