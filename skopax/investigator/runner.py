import logging
import time
from dataclasses import dataclass, field, replace
from typing import Callable

from skopax import advisor, tracing
from skopax.ai import ADVISOR, ANTHROPIC, THINK_OFF, Usage, capabilities_of, provider_of_ref, wrap
from skopax.ai.anthropic_ai import CACHE, FALLBACK_BETA, answer_text, cache_marked
from skopax.errkind import kind as error_kind
from skopax.investigator.pricing import charge, default_model, known, split
from skopax.investigator.web import is_web_tool
from skopax.messages import msg

log = logging.getLogger('skopax.investigator.runner')

MAX_TOKENS = 16000
RECAP_LIMIT = 3000
ERROR_LIMIT = 300
MAX_PAUSES = 5
USAGE_FIELDS = ('input_tokens', 'output_tokens', 'cache_read_input_tokens', 'cache_creation_input_tokens')
LIMIT_TEXT = 'Tool-call limit reached for this run: call no more tools and give your final answer now.'
CUT_TEXT = 'cut off at max_tokens and not run; send it again shorter'
CUT_ERROR = 'tool call cut off at max_tokens'
BROKEN_TEXT = 'the tool input was not valid JSON and the tool did not run; send it again'
PAUSE_ERROR = 'the server tools kept pausing the turn'
ADVISOR_OVER = 'the advisor was consulted more often than the run allows'
WEB_REFUSED = 'web tools refused by the API, the run went on without them'
NO_SYSTEM = 'the system prompt is empty'
NO_ANSWER = 'the run has no answer to rewrite'


@dataclass(frozen=True)
class RunConfig:
    model: str
    effort: str
    max_tool_calls: int
    max_seconds: float = 900
    cache_ttl: str | None = None
    role: str = 'card'
    capabilities: frozenset[str] | None = None
    advisor: advisor.Plan | None = None


@dataclass
class Result:
    outcome: str = 'done'
    recap: str = ''
    turns: int = 0
    usage: dict = field(default_factory=lambda: dict.fromkeys(USAGE_FIELDS, 0))
    usd: float = 0.0
    error: str | None = None
    models: list = field(default_factory=list)
    advisor_calls: int = 0
    advisor_errors: int = 0


def note(result, text: str) -> None:
    held = result.error or ''
    if text not in held:
        result.error = (held + '; ' + text if held else text)[:ERROR_LIMIT]


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
        self.ai = wrap(client)
        self.store = store
        self.config = config
        self.clock = clock
        self.tag = tag
        self.plan: advisor.Plan | None = None
        self.messages: list[dict] = []
        self.tools: list[dict] = []
        self.held_run: tuple | None = None
        self.calls = 0

    def advising(self) -> advisor.Plan | None:
        plan = self.config.advisor
        if plan is None or self.ai.provider != ANTHROPIC:
            return None
        held = self.config.capabilities if self.config.capabilities is not None else capabilities_of(self.ai)
        return plan if ADVISOR in held and plan.model in advisor.choices(self.config.model) else None

    def request(self, system: str, tools: list[dict], messages: list[dict]):
        caps = self.config.capabilities
        extra = {} if caps is None else ({} if 'thinking' in caps else {'thinking': THINK_OFF}) | ({} if 'fallbacks' in caps else {'fallbacks': False})
        if self.plan is not None:
            extra['advisor'] = self.plan.tool()
            extra['fallbacks'] = False
        return self.ai.turn(self.config.model, self.config.effort, system, tools, messages, MAX_TOKENS, self.config.cache_ttl, **extra)

    def count(self, result: Result, turn, day: str) -> None:
        usage = turn.usage
        served = turn.model or self.config.model
        result.models.append(served)
        for f, n in usage.fields().items():
            result.usage[f] += n
        cost, priced_as = charge(served, self.ai.provider, usage)
        priced = priced_as or served
        if priced_as is not None:
            note(result, f'unpriced model {served}, charged at the highest known rate')
        result.usd += cost
        self.store.add_spend(day, cost)
        self.ledger(served, priced, usage, day)
        for model, used in turn.advisor:
            self.count_advisor(result, model, used, day)

    def count_advisor(self, result: Result, model: str, usage, day: str, estimated: bool = False) -> None:
        cost, priced_as = charge(model, self.ai.provider, usage)
        if priced_as is not None:
            note(result, f'unpriced advisor model {model}, charged at the highest known rate')
        result.usd += cost
        self.store.add_spend(day, cost)
        self.ledger(model, priced_as or model, usage, day, True, estimated)

    def estimate_advisor(self, result: Result, turn, missing: int, plan: advisor.Plan, day: str) -> None:
        used = turn.usage
        guess = Usage(input=used.input + used.cache_read + used.cache_write + used.cache_write_long, output=plan.tokens)
        note(result, msg('advisor.usage_missing'))
        for _ in range(missing):
            self.count_advisor(result, plan.model, guess, day, True)

    def ledger(self, served: str, priced: str, usage, day: str, as_advisor: bool = False, estimated: bool = False) -> None:
        if self.tag is None:
            return
        try:
            row = {**self.tag, 'day': day, 'provider': self.ai.provider, 'model': served, 'effort': self.config.effort, **split(priced, usage)}
            if as_advisor:
                row['role'] = 'advisor'
            if estimated:
                row['estimated'] = True
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
        if provider_of_ref(self.config.model) not in (self.ai.provider, None):
            instead = default_model(self.ai.provider, self.config.role)
            log.info('run %s: %s is a model of another AI provider than the client in hand, %s runs instead', (self.tag or {}).get('runId'), self.config.model, instead)
            self.config = replace(self.config, model=instead)
        if not str(system or '').strip():
            result.outcome, result.error = 'failed', NO_SYSTEM
            return result
        self.plan = plan = self.advising()
        if self.config.advisor is not None and plan is None:
            dropped = msg('advisor.dropped', advisor=self.config.advisor.model, executor=self.config.model)
            log.warning('run %s: %s', (self.tag or {}).get('runId'), dropped)
            note(result, dropped)
        self.messages = [{'role': 'user', 'content': brief}]
        self.tools = toolbox.definitions()
        self.held_run = (toolbox, system, day, halt)
        self.calls = 0
        return self._loop(result, plan)

    def again(self, text: str, halt: Callable[[], str | tuple[str, str] | None] | None = None) -> Result:
        result = Result()
        if self.held_run is None or not self.messages or self.messages[-1].get('role') != 'assistant':
            result.outcome, result.error = 'failed', NO_ANSWER
            return result
        toolbox, system, day, before = self.held_run
        self.held_run = (toolbox, system, day, halt or before)
        self.messages.append({'role': 'user', 'content': text})
        return self._loop(result, self.plan)

    def _loop(self, result: Result, plan) -> Result:
        toolbox, system, day, halt = self.held_run
        messages, tools = self.messages, self.tools
        start = self.clock()
        served = getattr(toolbox, 'server_calls', None)
        limited, cut, pauses = False, False, 0
        while True:
            if stop(result, halt()):
                break
            if self.clock() - start > self.config.max_seconds:
                result.outcome, result.error = 'capped', 'time limit'
                break
            try:
                resp = self.request(system, tools, messages)
            except self.ai.bad_request as e:
                if result.turns == 0 and plan is not None:
                    said = msg('advisor.refused', why=error_kind(e))
                    log.warning('run %s: %s', (self.tag or {}).get('runId'), said)
                    self.plan = plan = None
                    note(result, said)
                    continue
                if result.turns == 0 and any(is_web_tool(t) for t in tools):
                    log.warning('run %s: %s: %s', (self.tag or {}).get('runId'), WEB_REFUSED, error_kind(e))
                    tools = self.tools = [t for t in tools if not is_web_tool(t)]
                    note(result, f'{WEB_REFUSED}: {error_kind(e)}')
                    continue
                result.outcome, result.error = 'failed', error_kind(e)[:ERROR_LIMIT]
                break
            except self.ai.api_error as e:
                result.outcome, result.error = 'failed', error_kind(e)[:ERROR_LIMIT]
                break
            result.turns += 1
            self.count(result, resp, day)
            if plan is not None:
                found, failed = advisor.read_blocks(resp.native)
                result.advisor_calls += found - failed
                result.advisor_errors += failed
                if found - failed > len(resp.advisor):
                    self.estimate_advisor(result, resp, found - failed - len(resp.advisor), plan, day)
                if result.advisor_calls > plan.uses:
                    result.outcome, result.error = 'capped', ADVISOR_OVER
                    break
            if resp.stop == 'refusal':
                result.outcome = 'refused'
                break
            messages.append({'role': 'assistant', 'content': resp.native})
            if served is not None:
                served(resp.server)
            if resp.stop == 'pause_turn':
                pauses += 1
                if pauses > MAX_PAUSES + (plan.uses if plan is not None else 0):
                    result.outcome, result.error = 'failed', PAUSE_ERROR
                    break
                continue
            uses = resp.calls
            if not uses:
                result.recap = resp.text[:RECAP_LIMIT]
                if resp.stop == 'max_tokens':
                    result.outcome, result.error = 'failed', 'the answer hit max_tokens'
                break
            if limited:
                result.outcome, result.error = 'capped', 'the model kept calling tools after the limit'
                break
            if resp.stop == 'max_tokens':
                if cut:
                    result.outcome, result.error = 'failed', CUT_ERROR
                    break
                cut = True
                messages.append({'role': 'user', 'content': [{'type': 'tool_result', 'tool_use_id': b.id, 'content': CUT_TEXT, 'is_error': True} for b in uses]})
                continue
            results, halted = [], False
            for b in uses:
                self.calls += 1
                if self.calls > self.config.max_tool_calls:
                    limited = True
                    content, is_error = LIMIT_TEXT, True
                elif halted := stop(result, halt()):
                    break
                elif b.broken:
                    content, is_error = BROKEN_TEXT, True
                else:
                    began = tracing.now()
                    content, is_error = toolbox.call(b.name, b.input or {})
                    tracing.tool(b.name, began, not is_error, b.input or {}, content)
                results.append({'type': 'tool_result', 'tool_use_id': b.id, 'content': content, 'is_error': is_error})
            if halted:
                break
            messages.append({'role': 'user', 'content': results})
        result.usd = round(result.usd, 6)
        return result
