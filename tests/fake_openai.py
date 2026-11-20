import json

import httpx2
import openai
from openai.types.responses import Response

MODEL = 'gpt-6.2-astra'


def usage(i: int = 1000, o: int = 200, cached: int = 0, written: int = 0, reasoning: int = 0) -> dict:
    return {'input_tokens': i, 'input_tokens_details': {'cached_tokens': cached, 'cache_write_tokens': written}, 'output_tokens': o,
            'output_tokens_details': {'reasoning_tokens': reasoning}, 'total_tokens': i + o}


def text(t: str, mid: str = 'msg_1') -> dict:
    return {'type': 'message', 'id': mid, 'role': 'assistant', 'status': 'completed', 'content': [{'type': 'output_text', 'text': t, 'annotations': []}]}


def refusal(t: str = 'no') -> dict:
    return {'type': 'message', 'id': 'msg_r', 'role': 'assistant', 'status': 'completed', 'content': [{'type': 'refusal', 'refusal': t}]}


def call(name: str, args, cid: str = 'call_1', iid: str = 'fc_1') -> dict:
    return {'type': 'function_call', 'id': iid, 'call_id': cid, 'name': name, 'arguments': args if isinstance(args, str) else json.dumps(args), 'status': 'completed'}


def reasoning(rid: str = 'rs_1') -> dict:
    return {'type': 'reasoning', 'id': rid, 'summary': [], 'encrypted_content': 'sealed-thoughts'}


def search(query: str = 'kafka lag', sid: str = 'ws_1', sources: tuple = ('https://kafka.apache.org/doc',)) -> dict:
    return {'type': 'web_search_call', 'id': sid, 'status': 'completed', 'action': {'type': 'search', 'query': query, 'sources': [{'type': 'url', 'url': u} for u in sources]}}


def reply(*items: dict, u: dict | None = None, model: str = MODEL, status: str = 'completed', reason: str | None = None) -> Response:
    return Response.model_validate({'id': 'resp_1', 'object': 'response', 'created_at': 1, 'model': model, 'output': list(items), 'parallel_tool_calls': True, 'tool_choice': 'auto',
                                    'tools': [], 'status': status, 'incomplete_details': {'reason': reason} if reason else None, 'usage': u or usage()})


def error(cls, status: int | None = None):
    request = httpx2.Request('POST', 'https://api.openai.com/v1/responses')
    if status is None:
        return cls(request=request) if cls is not openai.APITimeoutError else cls(request)
    return cls('refused', response=httpx2.Response(status, request=request), body=None)


class FakeOpenAI:
    provider = 'openai'

    def __init__(self, replies: list):
        self.replies = list(replies)
        self.requests: list[dict] = []
        self.responses = self
        self.models = self

    def create(self, **kw):
        self.requests.append({**kw, 'input': json.loads(json.dumps(kw['input'], default=str))})
        r = self.replies.pop(0)
        if isinstance(r, Exception):
            raise r
        return r

    def retrieve(self, model: str):
        return {'id': model}
