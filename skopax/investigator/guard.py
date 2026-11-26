import json
import logging
import secrets
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

import anthropic

from skopax import clock as days
from skopax import tracing
from skopax.ai import THINK_OFF, ModelSetting, clients_for, label_of, route, wrap
from skopax.errkind import kind as error_kind
from skopax.investigator import pricing
from skopax.messages import msg
from skopax.outward import outward

log = logging.getLogger('skopax.guard')
MASK_TEXT = '[hidden by the guard]'
PROMPT_VERSION = '1'
PROMPT = Path(__file__).with_name('guard.md').read_text()
TIMEOUT = 20.0
MAX_TOKENS = 500
EFFORT = 'low'
REASON_LIMIT = 200
SPANS_MAX = 20
DIGEST_MAX = 20_000
MARKER_BYTES = 8
USE = 'guard'
FIELD = 'investigate_guard_model'
PASS, REFUSE, MASK, HOLD = 'pass', 'refuse', 'mask', 'hold'
QUESTION, ANSWER = 'question', 'answer'
VERDICTS = {QUESTION: (PASS, REFUSE), ANSWER: (PASS, MASK, HOLD)}
CLOSED = {QUESTION: REFUSE, ANSWER: HOLD}
NO_KEY, NO_PRICE, CAP, TIMEOUT_FAILURE, SCHEMA, API = 'no-key', 'no-price', 'cap', 'timeout', 'schema', 'api:'
KEYS = ('verdict', 'reason', 'spans')
NORMAL_END = frozenset({'end_turn'})


def schema_for(check: str) -> dict:
    return {'type': 'object', 'additionalProperties': False, 'required': list(KEYS),
            'properties': {'verdict': {'type': 'string', 'enum': list(VERDICTS[check])}, 'reason': {'type': 'string', 'description': f'one short sentence, at most {REASON_LIMIT} characters'},
                           'spans': {'type': 'array', 'items': {'type': 'string'}, 'description': f'exact substrings of the answer to hide, at most {SPANS_MAX}'}}}


QUESTION_SCHEMA = schema_for(QUESTION)
ANSWER_SCHEMA = schema_for(ANSWER)
SCHEMAS = {QUESTION: QUESTION_SCHEMA, ANSWER: ANSWER_SCHEMA}


class Failed(Exception):
    def __init__(self, code: str, usd: float = 0.0):
        super().__init__(code)
        self.code = code
        self.usd = usd


@dataclass(frozen=True)
class Check:
    verdict: str
    reason: str
    spans: tuple[str, ...]
    usd: float
    ms: int
    model: str
    failure: str | None


def hidden(answer: str, spans: tuple[str, ...]) -> list[tuple[int, int]]:
    found = []
    for span in {s for s in spans if isinstance(s, str) and s}:
        at = answer.find(span)
        while at >= 0:
            found.append((at, at + len(span)))
            at = answer.find(span, at + 1)
    merged: list[tuple[int, int]] = []
    for start, end in sorted(found):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def apply_mask(answer: str, spans: tuple[str, ...]) -> tuple[str, int] | None:
    parts = hidden(answer, spans)
    if sum(end - start for start, end in parts) * 2 > len(answer):
        return None
    out, at = [], 0
    for start, end in parts:
        out += [answer[at:start], MASK_TEXT]
        at = end
    return ''.join(out + [answer[at:]]), len(parts)


def marker_for(inputs: tuple[str, ...]) -> str:
    while True:
        found = secrets.token_hex(MARKER_BYTES)
        if not any(found in text for text in inputs):
            return found


def block(marker: str, name: str, text: str) -> str:
    return f'[{marker}:{name}]\n{text}\n[/{marker}:{name}]'


def masked(answer: str, spans: tuple[str, ...]) -> tuple[str, tuple[str, ...]]:
    found = tuple(dict.fromkeys(s for s in spans if isinstance(s, str) and s and s in answer))
    if not found or apply_mask(answer, found) is None:
        return HOLD, ()
    return MASK, found


def priced(model: str) -> bool:
    return pricing.known(model) and pricing.price_of(model) is not None


def timed_out(e: BaseException) -> bool:
    return isinstance(e, (TimeoutError, anthropic.APITimeoutError)) or 'Timeout' in type(e).__name__


class Guard:
    model = ModelSetting(FIELD, USE)

    def __init__(self, store, settings, client=None, clock=time.time, keys: Mapping[str, Callable[[], str]] | None = None):
        self.store = store
        self.settings = settings
        self.clients = clients_for(settings, None, lambda k: anthropic.Anthropic(api_key=k, timeout=TIMEOUT, max_retries=0), TIMEOUT, 0, client, keys)
        self.clock = clock
        self.last_failure: str | None = None

    @property
    def chosen(self) -> str:
        return str(getattr(self.settings, FIELD, '') or '').strip()

    @property
    def checks_questions(self) -> bool:
        return bool(self.settings.guard_check_questions) and bool(self.chosen)

    @property
    def checks_answers(self) -> bool:
        return bool(self.settings.guard_check_answers) and bool(self.chosen)

    def question(self, text: str, role: str, run_id: str) -> Check:
        return self._check(QUESTION, run_id, (('role', str(role)), ('question', str(text))))

    def answer(self, question: str, answer: str, digest: str, run_id: str) -> Check:
        if not isinstance(answer, str):
            self.last_failure = SCHEMA
            log.warning('guard %s check failed: %s', ANSWER, SCHEMA)
            return Check(HOLD, '', (), 0.0, 0, self.chosen, SCHEMA)
        return self._check(ANSWER, run_id, (('question', str(question)), ('answer', answer), ('digest', str(digest)[:DIGEST_MAX])), answer)

    def problem(self) -> str | None:
        on = bool(self.settings.guard_check_questions) or bool(self.settings.guard_check_answers)
        if not on:
            return None
        if not self.chosen:
            return msg('guard.no_model')
        try:
            provider, model, fallback = route(self, FIELD, USE)
            if fallback or not self.clients.keyed(provider):
                return msg('guard.no_key', owner=label_of(provider), model=model)
            if not priced(model):
                return msg('guard.no_price', model=model)
        except Exception as e:
            log.warning('guard route could not be read: %s', error_kind(e))
            return msg('guard.failed', failure=API + type(e).__name__)
        return msg('guard.failed', failure=self.last_failure) if self.last_failure else None

    def _check(self, check: str, run_id: str, inputs: tuple[tuple[str, str], ...], answer: str = '') -> Check:
        started = time.monotonic()
        model = self.chosen
        try:
            model, usd, verdict, reason, spans = self._ask(check, run_id, inputs)
            if verdict == MASK:
                verdict, spans = masked(answer, spans)
        except Failed as e:
            self.last_failure = e.code
            log.warning('guard %s check failed: %s', check, e.code)
            return Check(CLOSED[check], '', (), e.usd, int((time.monotonic() - started) * 1000), model, e.code)
        except Exception as e:
            self.last_failure = API + type(e).__name__
            log.warning('guard %s check failed unexpectedly: %s', check, error_kind(e))
            return Check(CLOSED[check], '', (), 0.0, int((time.monotonic() - started) * 1000), model, self.last_failure)
        self.last_failure = None
        return Check(verdict, reason, spans, usd, int((time.monotonic() - started) * 1000), model, None)

    def _ask(self, check: str, run_id: str, inputs: tuple[tuple[str, str], ...]) -> tuple[str, float, str, str, tuple[str, ...]]:
        provider, model, fallback = route(self, FIELD, USE)
        if not model or fallback:
            raise Failed(NO_KEY)
        if not self.clients.keyed(provider):
            raise Failed(NO_KEY)
        if not priced(model):
            raise Failed(NO_PRICE)
        day = days.day(self.clock())
        if self.store.spent(day) >= self.settings.investigate_daily_usd:
            raise Failed(CAP)
        ai = wrap(self.clients.get(provider))
        if ai is None or pricing.in_hand(ai.provider, model, USE) != model:
            raise Failed(NO_KEY)
        marker = marker_for(tuple(text for _, text in inputs))
        system = PROMPT.replace('{{marker}}', marker).replace('{{check}}', check)
        content = '\n\n'.join(block(marker, name, text) for name, text in inputs)
        try:
            with tracing.untraced():
                resp = ai.once(model, system, [{'role': 'user', 'content': content}], MAX_TOKENS, EFFORT, THINK_OFF, SCHEMAS[check])
        except Exception as e:
            raise Failed(TIMEOUT_FAILURE if timed_out(e) else API + type(e).__name__) from None
        try:
            usd, priced_as = pricing.charge(model, ai.provider, resp.usage)
        except Exception:
            raise Failed(NO_PRICE) from None
        self._spend(day, usd, ai.provider, model, resp.usage, priced_as, check, run_id)
        if resp.stop not in NORMAL_END:
            raise Failed(SCHEMA, usd)
        verdict, reason, spans = self._read(check, resp.texts, marker, usd)
        return model, usd, verdict, reason, spans

    def _read(self, check: str, texts, marker: str, usd: float) -> tuple[str, str, tuple[str, ...]]:
        try:
            body = json.loads(next(iter(texts)))
        except (StopIteration, ValueError, TypeError):
            raise Failed(SCHEMA, usd) from None
        if not isinstance(body, dict) or set(body) != set(KEYS):
            raise Failed(SCHEMA, usd)
        verdict, reason, spans = body['verdict'], body['reason'], body['spans']
        if verdict not in VERDICTS[check] or not isinstance(reason, str) or not isinstance(spans, list) or len(spans) > SPANS_MAX or not all(isinstance(s, str) for s in spans):
            raise Failed(SCHEMA, usd)
        said = outward(reason.replace(marker, '')).strip()[:REASON_LIMIT]
        return verdict, said, tuple(spans) if verdict == MASK else ()

    def _spend(self, day: str, usd: float, provider: str, model: str, usage, priced_as: str | None, check: str, run_id: str) -> None:
        try:
            self.store.add_spend(day, usd)
        except Exception as e:
            log.warning('guard spend not added: %s', error_kind(e))
        try:
            self.store.add_ledger({'source': USE, 'use': USE, 'guardFor': check, 'guardRun': run_id, 'day': day, 'provider': provider, 'model': model,
                                   'effort': None if provider == pricing.OPENAI else EFFORT, 'runId': None, 'actionId': None, 'keys': [], 'by': None,
                                   **pricing.split(priced_as or model, usage)} | ({'pricedAs': priced_as} if priced_as else {}))
        except Exception as e:
            log.warning('spend ledger row for a guard call not written: %s', error_kind(e))
