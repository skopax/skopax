import pytest

from tests.fake_anthropic import FakeAnthropic, reply, text as claude_text
from skopax.ai import THINK_OFF
from skopax.ai.anthropic_ai import AnthropicAi
from skopax.investigator import pricing


@pytest.mark.parametrize('model', ['claude-fable-5-2', 'claude-mythos-5-2'])
def test_fable_and_mythos_are_priced_and_offered(model):
    # then
    assert pricing.PRICES[model] == {'input_tokens': 8.00, 'output_tokens': 40.00, 'cache_read_input_tokens': 0.20, 'cache_creation_input_tokens': 10.00}
    assert model in pricing.ALL_MODELS


@pytest.mark.parametrize('model', ['claude-fable-5-2', 'claude-mythos-5-2'])
@pytest.mark.parametrize('thinking', [THINK_OFF, 'low'])
def test_a_one_shot_call_on_fable_or_mythos_never_switches_thinking_off(model, thinking):
    # given
    client = FakeAnthropic([], gate_replies=[reply(claude_text('ok'), stop='end_turn')])

    # when
    AnthropicAi(client).once(model, 'sys', [{'role': 'user', 'content': 'hi'}], 200, 'low', thinking)

    # then
    assert client.gate_requests[0]['thinking'] == {'type': 'adaptive'}


def test_other_models_keep_their_thinking_off_shape():
    # given
    client = FakeAnthropic([], gate_replies=[reply(claude_text('ok'), stop='end_turn'), reply(claude_text('ok'), stop='end_turn')])
    ai = AnthropicAi(client)

    # when
    ai.once('claude-haiku-5-5', 'sys', [{'role': 'user', 'content': 'hi'}], 200, 'low', THINK_OFF)
    ai.once('claude-sonnet-5-5', 'sys', [{'role': 'user', 'content': 'hi'}], 200, 'low', 'low')

    # then
    assert [r['thinking'] for r in client.gate_requests] == [{'type': 'disabled'}, {'type': 'between_tools'}]
