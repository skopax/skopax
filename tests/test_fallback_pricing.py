import pytest

from tests.fake_anthropic import FakeAnthropic, reply, text, usage
from tests.test_investigator_runner import FakeBox
from skopax.investigator import pricing
from skopax.investigator.runner import RunConfig, Runner
from skopax.store import Store
from skopax.tunables import MODELS

DAY = '2026-11-10'


def test_sonnet_5_5_cache_reads_cost_half_of_sonnet_5s():
    # given
    read = {'input_tokens': 0, 'output_tokens': 0, 'cache_read_input_tokens': 1_000_000, 'cache_creation_input_tokens': 0}

    # then
    assert pricing.usd('claude-sonnet-5-5', read) == pytest.approx(0.10)
    assert pricing.usd('claude-sonnet-5', read) == pytest.approx(0.20)


def test_a_slack_turn_that_falls_back_to_sonnet_5_is_priced_at_sonnet_5_and_not_at_the_highest_rate(store: Store):
    # given
    client = FakeAnthropic([reply(text('x'), stop='end_turn', u=usage(1000, 200, 10_000), model='claude-sonnet-5')])
    runner = Runner(client, store, RunConfig('claude-sonnet-5-5', 'medium', 5), lambda: 0.0)

    # when
    result = runner.run(FakeBox(), 'SYSTEM', 'BRIEF', DAY, lambda: None)

    # then
    assert result.usd == pytest.approx(1000 * 2 / 1e6 + 200 * 10 / 1e6 + 10_000 * 0.2 / 1e6)
    assert result.error is None and result.models == ['claude-sonnet-5']


def test_the_fallback_model_is_priced_but_not_offered_as_a_model_choice():
    # then
    assert 'claude-sonnet-5' not in MODELS and not pricing.known('claude-sonnet-5')
    assert pricing.split('claude-sonnet-5', {'input_tokens': 1_000_000})['usd']['total'] == pytest.approx(2.0)
