import json
from types import SimpleNamespace as NS

import anthropic
import httpx2


def usage(i: int = 1000, o: int = 200, cr: int = 0, cw: int = 0) -> NS:
    return NS(input_tokens=i, output_tokens=o, cache_read_input_tokens=cr, cache_creation_input_tokens=cw)


def text(t: str) -> NS:
    return NS(type='text', text=t)


def tool(name: str, args: dict, tid: str = 't1') -> NS:
    return NS(type='tool_use', id=tid, name=name, input=args)


def reply(*blocks: NS, stop: str = 'tool_use', u: NS | None = None, model: str | None = None) -> NS:
    return NS(content=list(blocks), stop_reason=stop, usage=u or usage(), model=model)


class FakeAnthropic:
    def __init__(self, replies: list, gate_replies: list | None = None):
        self.replies = list(replies)
        self.gate_replies = list(gate_replies or [])
        self.requests: list[dict] = []
        self.gate_requests: list[dict] = []
        self.beta = NS(messages=NS(create=self._create))
        self.messages = NS(create=self._gate)

    def _create(self, **kw):
        self.requests.append({**kw, 'messages': list(kw['messages'])})
        r = self.replies.pop(0)
        if isinstance(r, Exception):
            raise r
        if getattr(r, 'model', None) is None:
            r.model = kw['model']
        return r

    def _gate(self, **kw):
        self.gate_requests.append(kw)
        if not self.gate_replies:
            raise anthropic.APIConnectionError(request=httpx2.Request('POST', 'https://api.anthropic.com/v1/messages'))
        r = self.gate_replies.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


class FakeModelsClient:
    def __init__(self, owner: 'FakeModels'):
        self.owner = owner
        self.models = NS(retrieve=owner.retrieve)

    def close(self) -> None:
        self.owner.closed += 1

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> None:
        self.close()


class FakeModels:
    def __init__(self, outcome=None):
        self.outcome = outcome
        self.built: list[dict] = []
        self.asked: list[str] = []
        self.closed = 0

    def __call__(self, **kw):
        self.built.append(kw)
        return FakeModelsClient(self)

    def retrieve(self, model: str):
        self.asked.append(model)
        if isinstance(self.outcome, BaseException):
            raise self.outcome
        if callable(self.outcome):
            return self.outcome()
        return NS(id=model) if self.outcome is None else self.outcome


def verdict(addressed: bool = True, on_topic: bool = True, language: str = 'nl', reason: str = 'asks about the card', u: NS | None = None, small_talk: bool = False,
            card_intent: str = 'none', agrees: bool = False, mind_blown: bool = False, great: bool = False) -> NS:
    body = {'addressed': addressed, 'onTopic': on_topic, 'smallTalk': small_talk, 'cardIntent': card_intent, 'agrees': agrees, 'mindBlown': mind_blown, 'great': great, 'language': language, 'reason': reason}
    return reply(text(json.dumps(body)), stop='end_turn', u=u or usage(1500, 40))
