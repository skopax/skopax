import anthropic
import httpx2
import pytest
from anthropic.types.beta import BetaMessage

from tests.fake_anthropic import FakeAnthropic, reply, text, tool, usage
from skopax.investigator.pricing import PRICES
from skopax.investigator.runner import CUT_TEXT, FALLBACK_BETA, LIMIT_TEXT, RunConfig, Runner
from skopax.store import Store

DAY = '2026-10-21'


class FakeBox:
    def __init__(self, answers=None):
        self.answers = answers or {}
        self.seen = []

    def definitions(self):
        return [{'name': 'promql', 'description': 'x', 'input_schema': {'type': 'object', 'properties': {}, 'required': [], 'additionalProperties': False}}]

    def call(self, name, args):
        self.seen.append((name, args))
        return self.answers.get(name, ('ok', False))


def run(store, replies, halt=lambda: None, max_calls=30, clock=None):
    client = FakeAnthropic(replies)
    runner = Runner(client, store, RunConfig('claude-opus-5-5', 'medium', max_calls), clock or (lambda: 0.0))
    return runner.run(FakeBox(), 'SYSTEM', 'BRIEF', DAY, halt), client


def test_a_run_calls_tools_until_the_model_answers_and_counts_spend(store: Store):
    # when
    result, client = run(store, [reply(tool('promql', {'expr': 'up'})), reply(text('Recap line 1\nline 2'), stop='end_turn', u=usage(2000, 100, 1000))])

    # then
    assert (result.outcome, result.turns, result.recap) == ('done', 2, 'Recap line 1\nline 2')
    assert result.usage['input_tokens'] == 3000 and result.usage['cache_read_input_tokens'] == 1000
    assert store.spent(DAY) == pytest.approx(result.usd) and result.usd > 0
    first = client.requests[0]
    assert (first['model'], first['system'], first['thinking'], first['output_config'], first['betas']) == ('claude-opus-5-5', [{'type': 'text', 'text': 'SYSTEM', 'cache_control': {'type': 'ephemeral'}}], {'type': 'adaptive'}, {'effort': 'medium'}, [FALLBACK_BETA])
    assert first['extra_body'] == {'fallbacks': 'default'}
    assert first['messages'][0]['content'] == [{'type': 'text', 'text': 'BRIEF', 'cache_control': {'type': 'ephemeral'}}], 'the brief carries the moving breakpoint on the first turn'
    second = client.requests[1]['messages']
    assert second[0]['content'] == 'BRIEF', 'the breakpoint moves: older messages go back untouched'
    assert second[2]['content'][0] == {'type': 'tool_result', 'tool_use_id': 't1', 'content': 'ok', 'is_error': False, 'cache_control': {'type': 'ephemeral'}}


def test_the_halt_check_runs_before_every_turn(store: Store):
    # given
    calls = iter([None, 'stopped'])

    # when
    result, client = run(store, [reply(tool('promql', {})), reply(text('never'), stop='end_turn')], halt=lambda: next(calls))

    # then
    assert (result.outcome, result.turns, len(client.requests)) == ('stopped', 1, 1)


def test_the_tool_call_limit_gets_one_turn_to_recap_then_caps(store: Store):
    # when
    done, _ = run(store, [reply(tool('promql', {}, 'a'), tool('promql', {}, 'b')), reply(text('recap'), stop='end_turn')], max_calls=1)
    capped, client = run(store, [reply(tool('promql', {}, 'a'), tool('promql', {}, 'b')), reply(tool('promql', {}, 'c'))], max_calls=1)

    # then
    assert done.outcome == 'done'
    assert capped.outcome == 'capped' and 'after the limit' in capped.error
    assert client.requests[1]['messages'][2]['content'][1] == {'type': 'tool_result', 'tool_use_id': 'b', 'content': LIMIT_TEXT, 'is_error': True, 'cache_control': {'type': 'ephemeral'}}


@pytest.mark.parametrize('system', ['', ' \n\t'])
def test_a_blank_system_prompt_fails_the_run_before_any_call(store: Store, system: str):
    # given
    client = FakeAnthropic([reply(text('never'), stop='end_turn')])
    runner = Runner(client, store, RunConfig('claude-opus-5-5', 'medium', 30), lambda: 0.0)

    # when
    result = runner.run(FakeBox(), system, 'BRIEF', DAY, lambda: None)

    # then
    assert (result.outcome, result.error, result.turns) == ('failed', 'the system prompt is empty', 0)
    assert client.requests == [] and store.spent(DAY) == 0


def test_the_limit_text_asks_for_the_final_answer_not_a_recap():
    # then
    assert LIMIT_TEXT == 'Tool-call limit reached for this run: call no more tools and give your final answer now.'


def test_a_tool_call_cut_off_at_max_tokens_is_not_run_and_the_model_is_told_to_send_it_again(store: Store):
    # given
    box = FakeBox()
    client = FakeAnthropic([reply(tool('slack_draft', {'key': 'k', 'text': 'half'}, 'a'), tool('note', {'key': 'k'}, 'b'), stop='max_tokens'),
                            reply(tool('promql', {'expr': 'up'}, 'c')), reply(text('recap'), stop='end_turn')])
    runner = Runner(client, store, RunConfig('claude-opus-5-5', 'medium', 30), lambda: 0.0)

    # when
    result = runner.run(box, 'SYSTEM', 'BRIEF', DAY, lambda: None)

    # then
    assert (result.outcome, result.turns, result.error) == ('done', 3, None)
    assert box.seen == [('promql', {'expr': 'up'})]
    answered = client.requests[1]['messages'][2]['content']
    assert [(r['tool_use_id'], r['content'], r['is_error']) for r in answered] == [('a', CUT_TEXT, True), ('b', CUT_TEXT, True)]
    assert CUT_TEXT == 'cut off at max_tokens and not run; send it again shorter'


def test_a_second_tool_call_cut_off_at_max_tokens_fails_the_run(store: Store):
    # given
    box = FakeBox()
    client = FakeAnthropic([reply(tool('note', {'key': 'k'}, 'a'), stop='max_tokens'), reply(tool('note', {'key': 'k'}, 'b'), stop='max_tokens'),
                            reply(text('never'), stop='end_turn')])
    runner = Runner(client, store, RunConfig('claude-opus-5-5', 'medium', 30), lambda: 0.0)

    # when
    result = runner.run(box, 'SYSTEM', 'BRIEF', DAY, lambda: None)

    # then
    assert (result.outcome, result.error, result.turns) == ('failed', 'tool call cut off at max_tokens', 2)
    assert box.seen == [] and len(client.requests) == 2


def test_a_refusal_an_api_error_max_tokens_and_the_time_limit_end_the_run(store: Store):
    # given
    err = anthropic.APIConnectionError(request=httpx2.Request('POST', 'https://api.anthropic.com/v1/messages'))
    ticks = iter([0.0, 0.0, 1000.0])

    # when
    refused, _ = run(store, [reply(text(''), stop='refusal')])
    failed, _ = run(store, [err])
    cut, _ = run(store, [reply(text('half'), stop='max_tokens')])
    slow, _ = run(store, [reply(tool('promql', {})), reply(text('x'), stop='end_turn')], clock=lambda: next(ticks))

    # then
    assert refused.outcome == 'refused'
    assert failed.outcome == 'failed' and 'APIConnectionError' in failed.error
    assert cut.outcome == 'failed' and 'max_tokens' in cut.error
    assert slow.outcome == 'capped' and slow.error == 'time limit'


def test_an_unpriced_model_fails_before_any_request_and_spends_nothing(store: Store):
    # given
    client = FakeAnthropic([reply(text('never'), stop='end_turn')])
    runner = Runner(client, store, RunConfig('claude-unknown', 'medium', 5), lambda: 0.0)

    # when
    result = runner.run(FakeBox(), 'SYSTEM', 'BRIEF', DAY, lambda: None)

    # then
    assert result.outcome == 'failed' and 'claude-unknown' in result.error
    assert client.requests == [] and store.spent(DAY) == 0


def test_spend_is_still_counted_when_pricing_fails_mid_run(store: Store, monkeypatch):
    # given
    monkeypatch.setattr('skopax.investigator.runner.known', lambda model: True)
    client = FakeAnthropic([reply(text('x'), stop='end_turn')])
    runner = Runner(client, store, RunConfig('claude-unknown', 'medium', 5), lambda: 0.0)

    # when
    result = runner.run(FakeBox(), 'SYSTEM', 'BRIEF', DAY, lambda: None)

    # then
    assert result.usd > 0 and store.spent(DAY) == pytest.approx(result.usd)
    assert result.usage['input_tokens'] == 1000 and 'unpriced' in result.error


def served_by(model: str):
    return BetaMessage.model_validate({'id': 'm1', 'type': 'message', 'role': 'assistant', 'model': model, 'content': [{'type': 'text', 'text': 'recap'}],
                                       'stop_reason': 'end_turn', 'stop_sequence': None,
                                       'usage': {'input_tokens': 1000, 'output_tokens': 200, 'cache_read_input_tokens': 10000, 'cache_creation_input_tokens': None}})


def test_a_turn_served_by_a_fallback_model_is_priced_at_that_models_rates(store: Store):
    # when
    result, _ = run(store, [served_by('claude-opus-4-8')])

    # then
    assert result.usd == pytest.approx(1000 * 5 / 1e6 + 200 * 25 / 1e6 + 10000 * 0.5 / 1e6)
    assert store.spent(DAY) == pytest.approx(result.usd)
    assert result.models == ['claude-opus-4-8'] and result.error is None


def test_an_unpriced_served_model_is_charged_at_the_highest_rate_and_named(store: Store):
    # when
    result, _ = run(store, [reply(text('x'), stop='end_turn', model='claude-fable-9')])

    # then
    assert result.usd == pytest.approx(max(1000 * p['input_tokens'] / 1e6 + 200 * p['output_tokens'] / 1e6 for p in PRICES.values()))
    assert 'claude-fable-9' in result.error and result.models == ['claude-fable-9']


def test_every_turn_records_the_model_that_served_it(store: Store):
    # when
    result, _ = run(store, [reply(tool('promql', {})), reply(text('x'), stop='end_turn', model='claude-opus-5')])

    # then
    assert result.models == ['claude-opus-5-5', 'claude-opus-5']


def test_the_lease_is_renewed_before_every_tool_call_and_a_lost_lease_fails_the_run(store: Store):
    # given
    answers = iter([None, None, None, ('failed', 'lease lost to another pod')])
    box = FakeBox()
    client = FakeAnthropic([reply(tool('promql', {'n': 1}, 'a'), tool('promql', {'n': 2}, 'b'), tool('promql', {'n': 3}, 'c'))])
    runner = Runner(client, store, RunConfig('claude-opus-5-5', 'medium', 30), lambda: 0.0)

    # when
    result = runner.run(box, 'SYSTEM', 'BRIEF', DAY, lambda: next(answers))

    # then
    assert (result.outcome, result.error, result.turns) == ('failed', 'lease lost to another pod', 1)
    assert box.seen == [('promql', {'n': 1}), ('promql', {'n': 2})]


def test_a_two_turn_run_checks_halt_at_both_turns_and_before_each_tool(store: Store):
    # given
    seen = []

    # when
    result, _ = run(store, [reply(tool('promql', {}, 'a'), tool('promql', {}, 'b')), reply(text('recap'), stop='end_turn')], halt=lambda: seen.append(1))

    # then
    assert result.outcome == 'done' and len(seen) == 4


def test_the_fix_turn_shares_the_runs_time_budget(store: Store):
    # given
    now = [0.0]
    client = FakeAnthropic([reply(text('first'), stop='end_turn'), reply(text('second'), stop='end_turn')])
    runner = Runner(client, store, RunConfig('claude-opus-5-5', 'medium', 30, max_seconds=600), lambda: now[0])
    first = runner.run(FakeBox(), 'SYSTEM', 'BRIEF', DAY, lambda: None)

    # when
    now[0] = 700.0
    again = runner.again('fix it')

    # then
    assert first.outcome == 'done' and again.outcome == 'capped' and again.error == 'time limit'
