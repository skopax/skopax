import anthropic

from skopax import tracing
from skopax.ai import ANTHROPIC, PROVIDER_CAPABILITIES, THINK_AUTO, THINK_OFF, ToolCall, Turn, neutral

FALLBACK_BETA = 'server-side-fallback-2026-07-01'
ADVISOR_BETA = 'advisor-tool-2026-03-01'
ADVISOR_ITERATION = 'advisor_message'
CACHE = {'type': 'ephemeral'}


def cache_marked(messages: list[dict]) -> list[dict]:
    out = list(messages)
    at = max(i for i, m in enumerate(out) if m['role'] == 'user')
    last = dict(out[at])
    content = last['content']
    if isinstance(content, str):
        last['content'] = [{'type': 'text', 'text': content, 'cache_control': CACHE}]
    else:
        blocks = list(content)
        blocks[-1] = {**blocks[-1], 'cache_control': CACHE}
        last['content'] = blocks
    out[at] = last
    return out


QUIET_BLOCKS = frozenset({'thinking', 'redacted_thinking'})


def answer_text(content) -> str:
    parts: list[str] = []
    last: list[str] = []
    for b in content:
        kind = getattr(b, 'type', None)
        if kind == 'text':
            parts.append(b.text)
        elif kind not in QUIET_BLOCKS and parts:
            last, parts = parts, []
    return ''.join(parts or last).strip()


ALWAYS_THINKING = ('claude-fable', 'claude-mythos')
THINKING_ROOM = 8000


def thinking_off(model: str) -> dict:
    if model.startswith(ALWAYS_THINKING):
        return {'type': 'adaptive'}
    return {'type': 'disabled'} if model.startswith('claude-haiku') else {'type': 'between_tools'}


def part(value, name: str):
    return value.get(name) if isinstance(value, dict) else getattr(value, name, None)


def advisor_usage(resp) -> list[tuple[str, object]]:
    found = []
    for item in part(getattr(resp, 'usage', None), 'iterations') or []:
        if part(item, 'type') == ADVISOR_ITERATION and isinstance(part(item, 'model'), str):
            found.append((part(item, 'model'), neutral(item)))
    return found


def turn_of(resp, model: str) -> Turn:
    content = list(resp.content or [])
    texts = [b.text for b in content if getattr(b, 'type', None) == 'text' and isinstance(getattr(b, 'text', None), str)]
    calls = [ToolCall(b.id, b.name, b.input or {}) for b in content if getattr(b, 'type', None) == 'tool_use']
    return Turn(stop=getattr(resp, 'stop_reason', None) or 'end_turn', model=getattr(resp, 'model', None) or model, usage=neutral(resp.usage), texts=texts,
                text=answer_text(content), calls=calls, native=resp.content, server=resp.content, advisor=advisor_usage(resp))


class AnthropicAi:
    provider = ANTHROPIC
    capabilities = PROVIDER_CAPABILITIES[ANTHROPIC]
    bad_request = anthropic.BadRequestError
    api_error = anthropic.APIError

    def __init__(self, client):
        self.client = client

    def turn_args(self, model: str, effort: str, system: str, tools: list[dict], messages: list[dict], max_tokens: int, cache_ttl: str | None, thinking: str) -> dict:
        cache = {**CACHE, 'ttl': cache_ttl} if cache_ttl else CACHE
        think = {'thinking': {'type': 'adaptive'}, 'output_config': {'effort': effort}} if thinking != THINK_OFF else {}
        return {'model': model, 'max_tokens': max_tokens, 'system': [{'type': 'text', 'text': system, 'cache_control': cache}], 'tools': tools, 'messages': cache_marked(messages)} | think

    def once_args(self, model: str, system: str, messages: list[dict], max_tokens: int, effort: str, thinking: str, schema: dict | None) -> dict:
        output = {'effort': effort} | ({'format': {'type': 'json_schema', 'schema': schema}} if schema is not None else {})
        think = thinking_off(model)
        return {'model': model, 'max_tokens': max_tokens + (THINKING_ROOM if think['type'] == 'adaptive' else 0), 'system': system, 'messages': messages, 'thinking': think, 'output_config': output}

    def turn(self, model: str, effort: str, system: str, tools: list[dict], messages: list[dict], max_tokens: int, cache_ttl: str | None = None, thinking: str = THINK_AUTO,
             fallbacks: bool = True, advisor: dict | None = None) -> Turn:
        betas = ([FALLBACK_BETA] if fallbacks else []) + ([ADVISOR_BETA] if advisor else [])
        extra = ({'betas': betas} if betas else {}) | ({'extra_body': {'fallbacks': 'default'}} if fallbacks else {})
        offered = tools + [advisor] if advisor else tools
        return tracing.generation(self.provider, 'turn', model, system, messages, lambda: turn_of(self.client.beta.messages.create(
            **self.turn_args(model, effort, system, offered, messages, max_tokens, cache_ttl, thinking), **extra), model), offered)

    def once(self, model: str, system: str, messages: list[dict], max_tokens: int, effort: str, thinking: str = THINK_OFF, schema: dict | None = None) -> Turn:
        return tracing.generation(self.provider, 'once', model, system, messages,
                                  lambda: turn_of(self.client.messages.create(**self.once_args(model, system, messages, max_tokens, effort, thinking, schema)), model))
