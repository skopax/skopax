from collections.abc import Iterable
from dataclasses import dataclass

from skopax.ai import ADVISOR, ANTHROPIC, PROVIDER_CAPABILITIES, explicit
from skopax.investigator import pricing
from skopax.messages import msg

TOOL_TYPE = 'advisor_20260301'
TOOL_NAME = 'advisor'
RESULT = 'advisor_tool_result'
CALL = 'server_tool_use'
CACHE_FROM = 3
CACHE_TTL = '5m'
USES_LOW, USES_HIGH, USES_DEFAULT = 1, 10, 3
TOKENS_LOW, TOKENS_HIGH, TOKENS_DEFAULT = 1024, 4096, 2000
USE_SETTINGS = {'card': ('investigate.model', 'investigate_model'), 'critical': ('investigate.model-critical', 'investigate_model_critical'),
                'high': ('investigate.model-high', 'investigate_model_high'), 'slack': ('investigate.slack-model', 'investigate_slack_model'), 'chat': ('chat.model', 'chat_model')}
USES = tuple(USE_SETTINGS)
NUDGE = ('\n\n## Advisor\n\nYou have an advisor tool, a stronger model that reads this whole run. Consult it before you name the cause on a Critical or High card, '
         'and when two suspects remain. Its advice reaches people only through your answer, so check it against what the tools showed.\n')
TOP = ('claude-mythos-5-2', 'claude-fable-5-2', 'claude-mythos-5-1', 'claude-fable-5-1', 'claude-mythos-5', 'claude-fable-5', 'claude-opus-5-5', 'claude-opus-5')
LATEST = ('claude-mythos-5-2', 'claude-fable-5-2')
OLDER = TOP + ('claude-opus-4-8', 'claude-opus-4-7', 'claude-sonnet-5-5')
LOW = OLDER + ('claude-sonnet-5', 'claude-haiku-5-5')
PAIRINGS = {
    'claude-fable-5-2': LATEST,
    'claude-mythos-5-2': LATEST,
    'claude-opus-5-5': TOP,
    'claude-opus-5': TOP,
    'claude-opus-4-8': OLDER,
    'claude-sonnet-5-5': TOP + ('claude-sonnet-5-5',),
    'claude-sonnet-5': LOW,
    'claude-haiku-5-5': LOW,
}


def key_of(use: str, part: str = '') -> str:
    return USE_SETTINGS[use][0] + '-advisor' + (f'-{part}' if part else '')


def field_of(use: str, part: str = '') -> str:
    return USE_SETTINGS[use][1] + '_advisor' + (f'_{part}' if part else '')


def keys() -> dict[str, tuple[str, str, str]]:
    return {use: (key_of(use), key_of(use, 'uses'), key_of(use, 'tokens')) for use in USES}


def advisor_keys() -> frozenset[str]:
    return frozenset(k for found in keys().values() for k in found)


def use_of_key(key: str) -> str | None:
    return next((use for use, found in keys().items() if key == found[0]), None)


def choices(executor: str) -> tuple[str, ...]:
    return tuple(m for m in PAIRINGS.get(executor, ()) if m in pricing.PRICES)


def bounded(value: object, low: int, high: int, default: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return min(max(number, low), high)


@dataclass(frozen=True)
class Plan:
    model: str
    uses: int = USES_DEFAULT
    tokens: int = TOKENS_DEFAULT

    def tool(self) -> dict:
        found = {'type': TOOL_TYPE, 'name': TOOL_NAME, 'model': self.model, 'max_uses': self.uses, 'max_tokens': self.tokens}
        if self.uses >= CACHE_FROM:
            found['caching'] = {'type': 'ephemeral', 'ttl': CACHE_TTL}
        return found


def stored(settings, use: str) -> tuple[str, int, int]:
    model = str(getattr(settings, field_of(use), '') or '').strip()
    uses = bounded(getattr(settings, field_of(use, 'uses'), USES_DEFAULT), USES_LOW, USES_HIGH, USES_DEFAULT)
    tokens = bounded(getattr(settings, field_of(use, 'tokens'), TOKENS_DEFAULT), TOKENS_LOW, TOKENS_HIGH, TOKENS_DEFAULT)
    return model, uses, tokens


def problem_of(model: str, provider: str | None, caps: Iterable[str], executor: str) -> str | None:
    if not model:
        return None
    if provider != ANTHROPIC or ADVISOR not in caps:
        return msg('advisor.not_anthropic')
    if not choices(executor):
        return msg('advisor.executor_unsupported', executor=executor or '?')
    if model not in choices(executor):
        return msg('advisor.pair_invalid', advisor=model, executor=executor)
    return None


def resolve(settings, use: str, provider: str | None, caps: Iterable[str], executor: str) -> tuple[Plan | None, str | None]:
    model, uses, tokens = stored(settings, use)
    if not model:
        return None, None
    problem = problem_of(model, provider, caps, executor)
    return (None, problem) if problem else (Plan(model, uses, tokens), None)


def view(settings, use: str, provider: str | None, caps: Iterable[str], executor: str) -> dict:
    model, uses, tokens = stored(settings, use)
    capable = provider == ANTHROPIC and ADVISOR in caps
    return {'model': model, 'choices': list(choices(executor)) if capable else [], 'maxUses': uses, 'maxTokens': tokens, 'capable': capable, 'problem': problem_of(model, provider, caps, executor)}


def read_blocks(content) -> tuple[int, int]:
    results = errors = 0
    for b in content or []:
        if (b.get('type') if isinstance(b, dict) else getattr(b, 'type', None)) != RESULT:
            continue
        inner = b.get('content') if isinstance(b, dict) else getattr(b, 'content', None)
        kind = inner.get('type') if isinstance(inner, dict) else getattr(inner, 'type', None)
        results += 1
        errors += kind not in ('advisor_result', 'advisor_redacted_result')
    return results, errors


def executor(settings, use: str) -> tuple[str, str]:
    field = USE_SETTINGS[use][1]
    ref = getattr(settings, field)
    if use in ('critical', 'high') and not ref:
        field, ref = USE_SETTINGS['card'][1], settings.investigate_model
    role = 'slack' if use == 'slack' else 'chat' if use == 'chat' else 'card'
    provider, model, _ = pricing.resolve(ref, role, settings.ai_provider, None, explicit(settings, field))
    return provider, model


def save_problem(settings, key: str, value: str) -> str | None:
    use = use_of_key(key)
    if use is None or not value:
        return None
    provider, model = executor(settings, use)
    return problem_of(value, provider, PROVIDER_CAPABILITIES.get(provider, ()), model)
