import anthropic

from skopax import tracing
from skopax.ai import ANTHROPIC, THINK_OFF, ToolCall, Turn, neutral

FALLBACK_BETA = 'server-side-fallback-2026-07-01'
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


def answer_text(content) -> str:
    parts, gap = [], False
    for b in content:
        if getattr(b, 'type', None) == 'text':
            if parts and gap:
                parts.append('\n')
            parts.append(b.text)
            gap = False
        elif parts:
            gap = True
    return ''.join(parts).strip()


ALWAYS_THINKING = ('claude-fable', 'claude-mythos')
THINKING_ROOM = 8000


def thinking_off(model: str) -> dict:
    if model.startswith(ALWAYS_THINKING):
        return {'type': 'adaptive'}
    return {'type': 'disabled'} if model.startswith('claude-haiku') else {'type': 'between_tools'}


def turn_of(resp, model: str) -> Turn:
    content = list(resp.content or [])
    texts = [b.text for b in content if getattr(b, 'type', None) == 'text' and isinstance(getattr(b, 'text', None), str)]
    calls = [ToolCall(b.id, b.name, b.input or {}) for b in content if getattr(b, 'type', None) == 'tool_use']
    return Turn(stop=getattr(resp, 'stop_reason', None) or 'end_turn', model=getattr(resp, 'model', None) or model, usage=neutral(resp.usage), texts=texts,
                text=answer_text(content), calls=calls, native=resp.content, server=resp.content)


class AnthropicAi:
    provider = ANTHROPIC
    bad_request = anthropic.BadRequestError
    api_error = anthropic.APIError

    def __init__(self, client):
        self.client = client

    def turn(self, model: str, effort: str, system: str, tools: list[dict], messages: list[dict], max_tokens: int, cache_ttl: str | None = None) -> Turn:
        cache = {**CACHE, 'ttl': cache_ttl} if cache_ttl else CACHE
        return tracing.generation(self.provider, 'turn', model, system, messages, lambda: turn_of(self.client.beta.messages.create(
            model=model, max_tokens=max_tokens, system=[{'type': 'text', 'text': system, 'cache_control': cache}], tools=tools, messages=cache_marked(messages),
            thinking={'type': 'adaptive'}, output_config={'effort': effort}, betas=[FALLBACK_BETA],
            extra_body={'fallbacks': 'default'}), model), tools)

    def once(self, model: str, system: str, messages: list[dict], max_tokens: int, effort: str, thinking: str = THINK_OFF, schema: dict | None = None) -> Turn:
        output = {'effort': effort} | ({'format': {'type': 'json_schema', 'schema': schema}} if schema is not None else {})
        think = {'type': 'adaptive'} if model.startswith(ALWAYS_THINKING) else {'type': 'disabled'} if thinking == THINK_OFF else thinking_off(model)
        return tracing.generation(self.provider, 'once', model, system, messages,
                                  lambda: turn_of(self.client.messages.create(model=model, max_tokens=max_tokens + (THINKING_ROOM if model.startswith(ALWAYS_THINKING) else 0), system=system, messages=messages,
                                                                              thinking=think, output_config=output), model))
