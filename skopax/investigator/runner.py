import logging
import time
from dataclasses import dataclass, field
from typing import Callable

import anthropic

from skopax.errkind import kind as error_kind
from skopax.investigator.pricing import PRICES, UnknownModel, known, split, usd
from skopax.investigator.web import is_web_tool

log = logging.getLogger('skopax.investigator.runner')

FALLBACK_BETA = 'server-side-fallback-2026-07-01'
MAX_TOKENS = 16000
RECAP_LIMIT = 3000
ERROR_LIMIT = 300
MAX_PAUSES = 5
USAGE_FIELDS = ('input_tokens', 'output_tokens', 'cache_read_input_tokens', 'cache_creation_input_tokens')
LIMIT_TEXT = 'Tool-call limit reached for this run: call no more tools and give your final answer now.'
CUT_TEXT = 'cut off at max_tokens and not run; send it again shorter'
CUT_ERROR = 'tool call cut off at max_tokens'
PAUSE_ERROR = 'the web tools kept pausing the turn'
WEB_REFUSED = 'web tools refused by the API, the run went on without them'
NO_SYSTEM = 'the system prompt is empty'
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


@dataclass(frozen=True)
class RunConfig:
    model: str
    effort: str
    max_tool_calls: int
    max_seconds: float = 900
    cache_ttl: str | None = None


@dataclass
class Result:
    outcome: str = 'done'
    recap: str = ''
    turns: int = 0
    usage: dict = field(default_factory=lambda: dict.fromkeys(USAGE_FIELDS, 0))
    usd: float = 0.0
    error: str | None = None
    models: list = field(default_factory=list)


def stop(result: Result, why) -> bool:
    if not why:
        return False
    outcome, error = (why, None) if isinstance(why, str) else why
    result.outcome = outcome
    if error:
        result.error = error[:ERROR_LIMIT]
    return True


class Runner:
    def __init__(self, client, store, config: RunConfig, clock: Callable[[], float] = time.monotonic, tag: dict | None = None):
        self.client = client
        self.store = store
        self.config = config
        self.clock = clock
        self.tag = tag

    def request(self, system: str, tools: list[dict], messages: list[dict]):
        cache = {**CACHE, 'ttl': self.config.cache_ttl} if self.config.cache_ttl else CACHE
        return self.client.beta.messages.create(
            model=self.config.model, max_tokens=MAX_TOKENS, system=[{'type': 'text', 'text': system, 'cache_control': cache}], tools=tools, messages=cache_marked(messages),
            thinking={'type': 'adaptive'}, output_config={'effort': self.config.effort}, betas=[FALLBACK_BETA],
            extra_body={'fallbacks': 'default'})

    def count(self, result: Result, resp, day: str) -> None:
        usage = resp.usage
        served = getattr(resp, 'model', None) or self.config.model
        result.models.append(served)
        for f in USAGE_FIELDS:
            result.usage[f] += int(getattr(usage, f, 0) or 0)
        try:
            priced, cost = served, usd(served, usage)
        except UnknownModel:
            priced = max(PRICES, key=lambda m: usd(m, usage))
            cost = usd(priced, usage)
            result.error = f'unpriced model {served}, charged at the highest known rate'[:ERROR_LIMIT]
        result.usd += cost
        self.store.add_spend(day, cost)
        self.ledger(served, priced, usage, day)

    def ledger(self, served: str, priced: str, usage, day: str) -> None:
        if self.tag is None:
            return
        try:
            row = {**self.tag, 'day': day, 'model': served, 'effort': self.config.effort, **split(priced, usage)}
            if priced != served:
                row['pricedAs'] = priced
            self.store.add_ledger(row)
        except Exception as e:
            log.warning('spend ledger row for run %s not written: %s', self.tag.get('runId'), error_kind(e))

    def run(self, toolbox, system: str, brief: str, day: str, halt: Callable[[], str | tuple[str, str] | None]) -> Result:
        result = Result()
        if not known(self.config.model):
            result.outcome, result.error = 'failed', f'unknown model {self.config.model}'[:ERROR_LIMIT]
            return result
        if not str(system or '').strip():
            result.outcome, result.error = 'failed', NO_SYSTEM
            return result
        start = self.clock()
        messages = [{'role': 'user', 'content': brief}]
        tools = toolbox.definitions()
        served = getattr(toolbox, 'server_calls', None)
        calls, limited, cut, pauses = 0, False, False, 0
        while True:
            if stop(result, halt()):
                break
            if self.clock() - start > self.config.max_seconds:
                result.outcome, result.error = 'capped', 'time limit'
                break
            try:
                resp = self.request(system, tools, messages)
            except anthropic.BadRequestError as e:
                if result.turns == 0 and any(is_web_tool(t) for t in tools):
                    log.warning('run %s: %s: %s', (self.tag or {}).get('runId'), WEB_REFUSED, error_kind(e))
                    tools = [t for t in tools if not is_web_tool(t)]
                    result.error = f'{WEB_REFUSED}: {error_kind(e)}'[:ERROR_LIMIT]
                    continue
                result.outcome, result.error = 'failed', error_kind(e)[:ERROR_LIMIT]
                break
            except anthropic.APIError as e:
                result.outcome, result.error = 'failed', error_kind(e)[:ERROR_LIMIT]
                break
            result.turns += 1
            self.count(result, resp, day)
            if resp.stop_reason == 'refusal':
                result.outcome = 'refused'
                break
            messages.append({'role': 'assistant', 'content': resp.content})
            if served is not None:
                served(resp.content)
            if resp.stop_reason == 'pause_turn':
                pauses += 1
                if pauses > MAX_PAUSES:
                    result.outcome, result.error = 'failed', PAUSE_ERROR
                    break
                continue
            uses = [b for b in resp.content if getattr(b, 'type', None) == 'tool_use']
            if not uses:
                result.recap = answer_text(resp.content)[:RECAP_LIMIT]
                if resp.stop_reason == 'max_tokens':
                    result.outcome, result.error = 'failed', 'the answer hit max_tokens'
                break
            if limited:
                result.outcome, result.error = 'capped', 'the model kept calling tools after the limit'
                break
            if resp.stop_reason == 'max_tokens':
                if cut:
                    result.outcome, result.error = 'failed', CUT_ERROR
                    break
                cut = True
                messages.append({'role': 'user', 'content': [{'type': 'tool_result', 'tool_use_id': b.id, 'content': CUT_TEXT, 'is_error': True} for b in uses]})
                continue
            results, halted = [], False
            for b in uses:
                calls += 1
                if calls > self.config.max_tool_calls:
                    limited = True
                    content, is_error = LIMIT_TEXT, True
                elif halted := stop(result, halt()):
                    break
                else:
                    content, is_error = toolbox.call(b.name, b.input or {})
                results.append({'type': 'tool_result', 'tool_use_id': b.id, 'content': content, 'is_error': is_error})
            if halted:
                break
            messages.append({'role': 'user', 'content': results})
        result.usd = round(result.usd, 6)
        return result
