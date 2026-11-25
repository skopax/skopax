import json

import openai

from skopax import tracing
from skopax.ai import FETCH_TYPE, OPENAI, PROVIDER_CAPABILITIES, SEARCH_TYPE, THINK_AUTO, THINK_OFF, ToolCall, Turn, Usage

EFFORTS = {'low': 'low', 'medium': 'medium', 'high': 'high', 'xhigh': 'xhigh', 'max': 'max'}
NONE_MODELS = ('gpt-6.1-terra', 'gpt-6-luna')
REASONING_ROOM = 4000
REASONING = 'reasoning.encrypted_content'
SOURCES = 'web_search_call.action.sources'
ERROR_MARK = 'Error: '
SEARCH_CALL = 'web_search_call'
NATIVE = ('message', 'reasoning', 'function_call', SEARCH_CALL)


class ResponseFailed(openai.OpenAIError):
    def __init__(self, status: str):
        super().__init__(f'the response ended {status}')
        self.status = status


def reasoning_effort(model: str, effort: str, thinking: str) -> str:
    if thinking == THINK_OFF:
        return 'none' if model in NONE_MODELS else 'low'
    return EFFORTS.get(effort, 'medium')


def part(obj, name: str):
    return obj.get(name) if isinstance(obj, dict) else getattr(obj, name, None)


def plain(item) -> dict:
    if isinstance(item, dict):
        return item
    return item.model_dump(mode='json', exclude_none=True)


def piece(block) -> dict | None:
    kind = part(block, 'type')
    if kind == 'text':
        return {'type': 'input_text', 'text': part(block, 'text') or ''}
    if kind == 'image':
        source = part(block, 'source') or {}
        return {'type': 'input_image', 'image_url': f"data:{part(source, 'media_type')};base64,{part(source, 'data')}"}
    return None


def output_of(block) -> str | list:
    content = part(block, 'content')
    error = bool(part(block, 'is_error'))
    if isinstance(content, list):
        pieces = [p for p in (piece(b) for b in content) if p is not None]
        return ([{'type': 'input_text', 'text': ERROR_MARK.strip()}] if error else []) + pieces
    text = '' if content is None else str(content)
    return ERROR_MARK + text if error else text


def user_items(content) -> list[dict]:
    if isinstance(content, str):
        return [{'role': 'user', 'content': content}]
    outputs = [{'type': 'function_call_output', 'call_id': part(b, 'tool_use_id'), 'output': output_of(b)} for b in content if part(b, 'type') == 'tool_result']
    pieces = [p for p in (piece(b) for b in content if part(b, 'type') != 'tool_result') if p is not None]
    return outputs + ([{'role': 'user', 'content': pieces}] if pieces else [])


def assistant_items(content) -> list[dict]:
    if isinstance(content, str):
        return [{'role': 'assistant', 'content': content}]
    native = [plain(b) for b in content if part(b, 'type') in NATIVE]
    said = ''.join(str(part(b, 'text') or '') for b in content if part(b, 'type') == 'text')
    return native + ([{'role': 'assistant', 'content': said}] if said else [])


def input_of(messages: list[dict]) -> list[dict]:
    out: list[dict] = []
    for m in messages:
        out += assistant_items(m.get('content')) if m.get('role') == 'assistant' else user_items(m.get('content'))
    return out


def searched(messages: list[dict]) -> int:
    return sum(1 for m in messages if m.get('role') == 'assistant' and isinstance(m.get('content'), list) for b in m['content'] if part(b, 'type') == SEARCH_CALL)


def function_of(tool: dict) -> dict:
    return {'type': 'function', 'name': tool['name'], 'description': tool.get('description', ''), 'parameters': tool.get('input_schema') or {'type': 'object', 'properties': {}},
            'strict': bool(tool.get('strict')) and closed(tool.get('input_schema') or {})}


def tools_of(tools: list[dict], messages: list[dict]) -> tuple[list[dict], int]:
    out, left = [], 0
    for t in tools:
        kind = t.get('type')
        if kind == FETCH_TYPE:
            continue
        if kind == SEARCH_TYPE:
            left = max(0, int(t.get('max_uses') or 0) - searched(messages))
            if left:
                out.append({'type': 'web_search'} | ({'filters': {'allowed_domains': list(t['allowed_domains'])}} if t.get('allowed_domains') else {}))
            continue
        out.append(function_of(t))
    return out, left


def closed(schema: dict) -> bool:
    props = schema.get('properties')
    if not (schema.get('additionalProperties') is False and isinstance(props, dict) and set(schema.get('required') or ()) == set(props)):
        return False
    return all(closed(v) if v.get('type') == 'object' else closed(v['items']) if v.get('type') == 'array' and isinstance(v.get('items'), dict) and v['items'].get('type') == 'object' else True
               for v in props.values() if isinstance(v, dict))


def arguments(raw) -> dict | None:
    try:
        found = json.loads(raw) if isinstance(raw, str) else raw
    except ValueError:
        return None
    return found if isinstance(found, dict) else None


def usage_of(resp, searches: int) -> Usage:
    u = part(resp, 'usage')
    details = part(u, 'input_tokens_details')
    cached = int(part(details, 'cached_tokens') or 0)
    written = int(part(details, 'cache_write_tokens') or 0)
    long = int(part(part(details, 'cache_write_tokens_details'), 'ttl_12h_tokens') or 0)
    return Usage(input=max(0, int(part(u, 'input_tokens') or 0) - cached - written), cache_read=cached, cache_write=max(0, written - long), cache_write_long=long,
                 output=int(part(u, 'output_tokens') or 0), web_searches=searches)


def server_of(item) -> list[dict]:
    action = part(item, 'action')
    query = part(action, 'query') or ' '.join(part(action, 'queries') or [])
    sid = part(item, 'id')
    started = {'type': 'server_tool_use', 'id': sid, 'name': 'web_search', 'input': {'query': query} if query else {}}
    if part(item, 'status') == 'failed':
        return [started, {'type': 'web_search_tool_result', 'tool_use_id': sid, 'content': {'error_code': 'failed'}}]
    found = [{'title': part(s, 'url')} for s in part(action, 'sources') or []]
    return [started, {'type': 'web_search_tool_result', 'tool_use_id': sid, 'content': found}]


def served_model(served, model: str) -> str:
    if not isinstance(served, str) or not served or served == model or served.startswith(model + '-'):
        return model
    return served


def turn_of(resp, model: str) -> Turn:
    status = part(resp, 'status')
    if status in ('failed', 'cancelled'):
        raise ResponseFailed(status)
    items = list(part(resp, 'output') or [])
    texts, calls, server, refused = [], [], [], False
    for item in items:
        kind = part(item, 'type')
        if kind == 'message':
            for c in part(item, 'content') or []:
                if part(c, 'type') == 'output_text' and isinstance(part(c, 'text'), str):
                    texts.append(part(c, 'text'))
                elif part(c, 'type') == 'refusal':
                    refused = True
        elif kind == 'function_call':
            found = arguments(part(item, 'arguments'))
            calls.append(ToolCall(part(item, 'call_id'), part(item, 'name'), found or {}, found is None))
        elif kind == SEARCH_CALL:
            server += server_of(item)
    reason = part(part(resp, 'incomplete_details'), 'reason')
    if refused or reason == 'content_filter':
        stop = 'refusal'
    elif status == 'incomplete':
        stop = 'max_tokens'
    else:
        stop = 'tool_use' if calls else 'end_turn'
    searches = sum(1 for i in items if part(i, 'type') == SEARCH_CALL)
    return Turn(stop=stop, model=served_model(part(resp, 'model'), model), usage=usage_of(resp, searches), texts=texts, text='\n'.join(texts).strip(), calls=calls,
                native=[plain(i) for i in items], server=server)


class OpenAiAi:
    provider = OPENAI
    capabilities = PROVIDER_CAPABILITIES[OPENAI]
    bad_request = openai.BadRequestError
    api_error = (openai.APIError, ResponseFailed)

    def __init__(self, client):
        self.client = client

    def turn(self, model: str, effort: str, system: str, tools: list[dict], messages: list[dict], max_tokens: int, cache_ttl: str | None = None, thinking: str = THINK_AUTO,
             fallbacks: bool = True) -> Turn:
        offered, left = tools_of(tools, messages)
        extra = {'max_tool_calls': left} if left else {}
        return tracing.generation(self.provider, 'turn', model, system, messages, lambda: turn_of(self.client.responses.create(
            model=model, instructions=system, input=input_of(messages), tools=offered, max_output_tokens=max_tokens, reasoning={'effort': reasoning_effort(model, effort, thinking)},
            store=False, include=[REASONING] + ([SOURCES] if left else []), **extra), model), tools)

    def once(self, model: str, system: str, messages: list[dict], max_tokens: int, effort: str, thinking: str = THINK_OFF, schema: dict | None = None) -> Turn:
        level = reasoning_effort(model, effort, thinking)
        extra = {'text': {'format': {'type': 'json_schema', 'name': 'answer', 'schema': schema, 'strict': closed(schema)}}} if schema is not None else {}
        return tracing.generation(self.provider, 'once', model, system, messages, lambda: turn_of(self.client.responses.create(
            model=model, instructions=system, input=input_of(messages), max_output_tokens=max_tokens + (0 if level == 'none' else REASONING_ROOM), reasoning={'effort': level},
            store=False, **extra), model))
