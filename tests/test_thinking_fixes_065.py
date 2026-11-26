import dataclasses
import json
import logging

import anthropic
import httpx2
import pytest

from tests.conftest import settings
from tests.fake_anthropic import FakeAnthropic, reply, text, usage, verdict
from tests.test_vertex_ai import Maker, connection_of
from skopax.ai import THINK_AUTO, THINK_OFF
from skopax.ai import vertex_ai
from skopax.ai.anthropic_ai import THINKING_ROOM, AnthropicAi
from skopax.investigator import pricing
from skopax.investigator.gate import Gate
from skopax.investigator.guard import REASON_LIMIT, Guard
from skopax.outward import outward
from skopax.store import Store

NOW = 1_792_224_000.0
REQUEST = httpx2.Request('POST', 'https://api.anthropic.com/v1/messages')
CLAUDE = sorted(m for m in (*pricing.PRICES, *pricing.FALLBACK_PRICES) if m.startswith('claude-'))
HAIKU = 'claude-haiku'
ALWAYS = ('claude-fable', 'claude-mythos')
REFUSED_TEXT = 'thinking.type: disabled is not supported for this model. Use adaptive or between_tools.'


def expected(model: str) -> str:
    if model.startswith(ALWAYS):
        return 'adaptive'
    return 'disabled' if model.startswith(HAIKU) else 'between_tools'


def bad_request(message: str) -> anthropic.BadRequestError:
    return anthropic.BadRequestError(message, response=httpx2.Response(400, request=REQUEST), body={'type': 'error', 'error': {'type': 'invalid_request_error', 'message': message}})


class StrictAnthropic(FakeAnthropic):
    def _gate(self, **kw):
        if kw.get('thinking', {}).get('type') == 'disabled' and not kw['model'].startswith(HAIKU):
            self.gate_requests.append(kw)
            raise bad_request(REFUSED_TEXT)
        return super()._gate(**kw)


def guard_reply() -> object:
    return reply(text(json.dumps({'verdict': 'pass', 'category': 'none', 'reason': 'an on-topic question', 'spans': []})), stop='end_turn', u=usage(1500, 40))


def test_the_claude_model_list_covers_every_family():
    # then
    assert {'claude-opus-5-5', 'claude-sonnet-5-5', 'claude-haiku-5-5', 'claude-fable-5-2', 'claude-sonnet-5'} <= set(CLAUDE)


@pytest.mark.parametrize('model', CLAUDE)
@pytest.mark.parametrize('thinking', [THINK_OFF, THINK_AUTO])
def test_once_args_thinking_matches_model(model, thinking):
    # when
    args = AnthropicAi(None).once_args(model, 'sys', [{'role': 'user', 'content': 'hi'}], 300, 'low', thinking, None)

    # then
    assert args['thinking'] == {'type': expected(model)}
    assert args['max_tokens'] == (300 + THINKING_ROOM if expected(model) == 'adaptive' else 300)


@pytest.mark.parametrize('model', CLAUDE)
@pytest.mark.parametrize('thinking', [THINK_OFF, THINK_AUTO])
def test_a_vertex_once_call_never_sends_disabled_to_a_model_that_refuses_it(model, thinking):
    # given
    maker = Maker()
    held = vertex_ai.VertexAi(maker(connection_of(), '/var/run/secrets/tokens', 30, 0), 'vertex', {'m': model})

    # when
    held.once('vertex/m', 'S', [{'role': 'user', 'content': 'Q'}], 300, 'low', thinking)

    # then
    body = maker.requests[0]['body']
    wanted = 'disabled' if model.startswith(HAIKU) else 'adaptive'
    assert body['thinking'] == {'type': wanted}
    assert body['max_tokens'] == (300 + THINKING_ROOM if wanted == 'adaptive' else 300)


@pytest.mark.parametrize('model', ['claude-opus-5-5', 'claude-sonnet-5-5'])
def test_guard_and_gate_requests_valid_for_opus_and_sonnet(store: Store, model):
    # given
    guard_fake = StrictAnthropic([], [guard_reply(), guard_reply()])
    found = dataclasses.replace(settings(), anthropic_api_key='sk-test', investigate_guard_model=model, guard_check_questions=True, guard_check_answers=True)
    guard = Guard(store, found, client=guard_fake, clock=lambda: NOW)
    gate_fake = StrictAnthropic([], [verdict()])
    gate = Gate(store, dataclasses.replace(settings(), anthropic_api_key='sk-test', investigate_gate_model=model), client=gate_fake, clock=lambda: NOW)

    # when
    asked = guard.question('why is checkout-service slow?', 'operator', 'run-1')
    judged = gate.check('why did it go up?')

    # then
    assert (asked.verdict, asked.failure, guard.problem()) == ('pass', None, None)
    assert judged.on_topic is True
    assert [r['thinking'] for r in guard_fake.gate_requests + gate_fake.gate_requests] == [{'type': 'between_tools'}, {'type': 'between_tools'}]


def test_guard_problem_shows_provider_message(store: Store, caplog):
    # given
    long_tail = ' then more words follow' * 20
    message = 'thinking.type: disabled is not supported, key sk-ant-api03-' + 'A' * 40 + long_tail
    fake = FakeAnthropic([], [bad_request(message), guard_reply()])
    found = dataclasses.replace(settings(), anthropic_api_key='sk-test', investigate_guard_model='claude-haiku-5-5', guard_check_questions=True, guard_check_answers=True)
    guard = Guard(store, found, client=fake, clock=lambda: NOW)

    # when
    with caplog.at_level(logging.DEBUG):
        checked = guard.question('why is checkout-service slow for alice@example.com?', 'operator', 'run-1')
    code, shown = guard.last_failure, guard.problem()
    guard.question('why is checkout-service slow?', 'operator', 'run-2')

    # then
    cut = outward(message).strip()
    assert checked.failure == 'api:BadRequestError' == code
    assert 'api:BadRequestError' in shown and cut[:REASON_LIMIT] in shown and cut[:REASON_LIMIT + 1] not in shown
    assert 'A' * 40 not in shown and 'thinking.type: disabled is not supported' in shown
    assert guard.problem() is None
    logged = '\n'.join(r.getMessage() for r in caplog.records)
    assert 'not supported' not in logged and 'alice@example.com' not in logged


def test_a_later_failure_without_a_provider_message_drops_the_earlier_message(store: Store):
    # given
    fake = FakeAnthropic([], [bad_request(REFUSED_TEXT), anthropic.APITimeoutError(request=REQUEST)])
    found = dataclasses.replace(settings(), anthropic_api_key='sk-test', investigate_guard_model='claude-haiku-5-5', guard_check_questions=True, guard_check_answers=True)
    guard = Guard(store, found, client=fake, clock=lambda: NOW)

    # when
    guard.question('first', 'operator', 'run-1')
    first = guard.problem()
    guard.question('second', 'operator', 'run-2')

    # then
    assert 'not supported' in first
    assert 'timeout' in guard.problem() and 'not supported' not in guard.problem()


@pytest.mark.parametrize('body', [{'type': 'error', 'error': {'type': 'invalid_request_error', 'message': REFUSED_TEXT}, 'request_id': 'req_1'},
                                  {'error': {'code': 400, 'message': REFUSED_TEXT, 'status': 'INVALID_ARGUMENT'}}])
def test_the_guard_reads_the_message_of_a_real_client_400(store: Store, body):
    # given
    client = anthropic.Anthropic(api_key='sk-test', max_retries=0, http_client=httpx2.Client(transport=httpx2.MockTransport(lambda request: httpx2.Response(400, json=body))))
    found = dataclasses.replace(settings(), anthropic_api_key='sk-test', investigate_guard_model='claude-haiku-5-5', guard_check_questions=True, guard_check_answers=True)
    guard = Guard(store, found, client=client, clock=lambda: NOW)

    # when
    checked = guard.question('why is checkout-service slow?', 'operator', 'run-1')

    # then
    assert checked.failure == 'api:BadRequestError'
    assert REFUSED_TEXT in guard.problem()
