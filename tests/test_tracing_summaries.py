import json

import pytest

from tests.fake_anthropic import FakeAnthropic, reply, text, tool, usage
from tests.fake_openai import FakeOpenAI, call as openai_call, reply as openai_reply, text as openai_text
from tests.test_tracing import URL, attrs, make, spans
from skopax import tracing
from skopax.ai import THINK_OFF, ToolCall, Turn, Usage, wrap
from skopax.investigator.runner import RunConfig, Runner
from skopax.store import Store
from skopax.tracing import FULL, METADATA, OTLP

PROMPT = 'canary-prompt-7f3a customer alice@example.com asked'
ANSWER = 'canary-answer-91bc the checkout failed'
RESULT = 'canary-result-55de rows for alice'
SYSTEM = 'canary-system-0d2e you are the investigator'
DAY = '2026-11-23'
STR = {'type': 'string'}
HOSTILE = 'customer-alice@example.com'


class Box:
    def definitions(self):
        return [{'name': 'promql', 'description': 'x', 'input_schema': {'type': 'object', 'properties': {'expr': STR, 'window': STR}, 'required': [], 'additionalProperties': False}},
                {'name': 'esql', 'description': 'x', 'input_schema': {'type': 'object', 'properties': {'query': STR}, 'required': [], 'additionalProperties': False}}]

    def call(self, name, args):
        return (RESULT, False) if name == 'promql' else ('index missing ' + RESULT, True)


def payload(http) -> str:
    return json.dumps([body for _, _, _, body in http.hits(URL + OTLP)])


def run_once(found, http, store: Store, content: str):
    client = FakeAnthropic([reply(tool('promql', {'expr': 'up', 'window': '5m'}, 'a'), tool('esql', {'query': PROMPT}, 'b')), reply(text(ANSWER), stop='end_turn', u=usage(1200, 80, cr=300))])
    runner = Runner(client, store, RunConfig('claude-opus-5-5', 'medium', 30), lambda: 0.0)
    handle = tracing.begin('card run', runId='run-1')
    handle.trace.note_brief(trigger='cards', cards=2)
    handle.trace.given(PROMPT)
    result = runner.run(Box(), SYSTEM, PROMPT, DAY, lambda: None)
    handle.trace.finished(result.outcome)
    handle.trace.answered(ANSWER)
    tracing.end(handle)
    found.drain()
    return result


def by_type(http, kind: str) -> list[dict]:
    return [attrs(s) for s in spans(http) if attrs(s)['langfuse.observation.type'] == kind]


def test_metadata_mode_sends_safe_summaries_and_never_the_prompt_answer_or_tool_text(installed, store: Store):
    # given
    found, http = make(content=METADATA)
    installed(found)

    # when
    run_once(found, http, store, METADATA)

    # then
    sent = payload(http)
    for canary in ('canary-prompt', 'canary-answer', 'canary-result', 'canary-system', 'alice@example.com', '"up"', '5m'):
        assert canary not in sent, canary
    first, second = by_type(http, 'generation')
    given = json.loads(first['langfuse.observation.input'])
    assert given == {'messages': 1, 'system_chars': len(SYSTEM), 'prompt_chars': len(PROMPT), 'input_tokens': 1000, 'tools_offered': ['promql', 'esql']}
    assert json.loads(first['langfuse.observation.output']) == {'stop_reason': 'tool_use', 'tool_calls': ['promql', 'esql'], 'answer_chars': 0}
    assert json.loads(second['langfuse.observation.input'])['messages'] == 3 and json.loads(second['langfuse.observation.input'])['input_tokens'] == 1500
    assert json.loads(second['langfuse.observation.output']) == {'stop_reason': 'end_turn', 'tool_calls': [], 'answer_chars': len(ANSWER)}
    promql, esql = by_type(http, 'tool')
    assert json.loads(promql['langfuse.observation.input']) == {'tool': 'promql', 'args': ['expr', 'window']}
    assert json.loads(promql['langfuse.observation.output']) == {'ok': True, 'result_chars': len(RESULT)}
    assert json.loads(esql['langfuse.observation.input']) == {'tool': 'esql', 'args': ['query']}
    assert json.loads(esql['langfuse.observation.output']) == {'ok': False, 'error': tracing.TOOL_FAILED, 'result_chars': len('index missing ' + RESULT)}
    root = next(attrs(s) for s in spans(http) if 'parentSpanId' not in s)
    assert json.loads(root['langfuse.observation.input']) == {'kind': 'card run', 'trigger': 'cards', 'cards': 2}
    assert json.loads(root['langfuse.observation.output']) == {'outcome': 'done', 'model_calls': 2, 'tool_calls': 2}


def test_full_mode_still_sends_the_redacted_text_and_no_summary(installed, store: Store):
    # given
    found, http = make(content=FULL)
    installed(found)

    # when
    run_once(found, http, store, FULL)

    # then
    first, second = by_type(http, 'generation')
    assert 'canary-system' in first['langfuse.observation.input'] and 'tools_offered' not in first['langfuse.observation.input']
    assert 'canary-answer' in second['langfuse.observation.output'] and 'answer_chars' not in second['langfuse.observation.output']
    promql = by_type(http, 'tool')[0]
    assert promql['langfuse.observation.output'] == RESULT and '"up"' in promql['langfuse.observation.input']
    root = next(attrs(s) for s in spans(http) if 'parentSpanId' not in s)
    assert root['langfuse.observation.input'].startswith('canary-prompt') and '[redacted]' in root['langfuse.observation.input']


def test_an_openai_call_summarises_its_tool_calls_and_offered_tools(installed):
    # given
    found, http = make()
    installed(found)
    client = FakeOpenAI([openai_reply(openai_call('promql', {'expr': PROMPT}))])
    tools = [{'name': 'promql', 'description': 'x', 'input_schema': {'type': 'object', 'properties': {}}}, {'type': 'web_search_20250305', 'name': 'web_search', 'max_uses': 2}]

    # when
    wrap(client).turn('gpt-6.2-astra', 'low', SYSTEM, tools, [{'role': 'user', 'content': PROMPT}], 100)
    found.drain()

    # then
    gen = by_type(http, 'generation')[0]
    assert json.loads(gen['langfuse.observation.input'])['tools_offered'] == ['promql', 'web_search']
    assert json.loads(gen['langfuse.observation.output']) == {'stop_reason': 'tool_use', 'tool_calls': ['promql'], 'answer_chars': 0}
    assert 'canary' not in payload(http)


def test_a_failed_model_call_summarises_its_input_and_names_only_the_error_kind(installed):
    # given
    found, http = make()
    installed(found)

    # when
    with pytest.raises(KeyError):
        with tracing.opened('topic check'):
            tracing.generation('anthropic', 'once', 'claude-haiku-5-5', SYSTEM, [{'role': 'user', 'content': PROMPT}], lambda: {}['missing'])
    found.drain()

    # then
    gen = by_type(http, 'generation')[0]
    assert json.loads(gen['langfuse.observation.input']) == {'messages': 1, 'system_chars': len(SYSTEM), 'prompt_chars': len(PROMPT), 'tools_offered': []}
    assert json.loads(gen['langfuse.observation.output']) == {'error': 'KeyError'}
    root = next(attrs(s) for s in spans(http) if 'parentSpanId' not in s)
    assert json.loads(root['langfuse.observation.input']) == {'kind': 'topic check'}
    assert json.loads(root['langfuse.observation.output']) == {'outcome': 'failed', 'model_calls': 1, 'tool_calls': 0}
    assert 'canary' not in payload(http)


@pytest.mark.parametrize('system, messages, tools', [
    (None, None, None),
    ('', [], [{}]),
    ('S', [None, 'plain', {'role': 'user'}, {'role': 'user', 'content': [None, {'type': 'image', 'source': {'data': 'AAAA'}}, {'type': 'tool_result', 'content': None}]}], ['x', None, {'type': 3}]),
    (42, {'role': 'user'}, 'promql'),
])
def test_the_input_summary_never_raises_on_odd_shapes(system, messages, tools):
    # when
    found = tracing.asked_brief(system, messages, tools, None)

    # then
    assert set(found) == {'messages', 'system_chars', 'prompt_chars', 'tools_offered'}
    assert all(isinstance(found[k], int) for k in ('messages', 'system_chars', 'prompt_chars')) and isinstance(found['tools_offered'], list)
    assert 'AAAA' not in json.dumps(found)


@pytest.mark.parametrize('turn', [None, object(), Turn(stop=None, model='m', usage=None), Turn(stop='end_turn', model='m', usage=Usage(), text=None, calls=[None, ToolCall('1', None, {})])])
def test_the_output_summary_never_raises_on_odd_shapes(turn):
    # when
    found = tracing.answer_brief(turn)
    tokens = tracing.asked_brief('S', [], [], getattr(turn, 'usage', None))

    # then
    assert set(found) == {'stop_reason', 'tool_calls', 'answer_chars'} and isinstance(found['tool_calls'], list) and isinstance(found['answer_chars'], int)
    assert 'messages' in tokens


@pytest.mark.parametrize('args, said', [(None, None), ('not a dict', 42), ({1: 'x', 'b': 2}, [{'type': 'image', 'source': {'data': 'AAAA'}}, {'type': 'text', 'text': 'abc'}])])
def test_the_tool_summaries_never_raise_on_odd_shapes(args, said):
    # when
    given = tracing.tool_input_brief('promql', args, {'promql': frozenset({'b'})})
    out = tracing.tool_output_brief(True, said)

    # then
    assert given['tool'] == 'promql' and isinstance(given['args'], list) and all(isinstance(a, str) for a in given['args'])
    assert out['ok'] is True and isinstance(out['result_chars'], int) and 'AAAA' not in json.dumps(out)


def test_model_chosen_tool_names_and_argument_keys_outside_the_offered_tools_are_never_named(installed, store: Store):
    # given
    found, http = make(content=METADATA)
    installed(found)
    client = FakeAnthropic([reply(tool('promql', {'expr': 'up', 'alice@example.com': 'x', HOSTILE: 1}, 'a'), tool(HOSTILE, {'expr': 'up'}, 'b')),
                            reply(text(ANSWER), stop='end_turn')])
    runner = Runner(client, store, RunConfig('claude-opus-5-5', 'medium', 30), lambda: 0.0)

    class Hostile(Box):
        def call(self, name, args):
            return ('ok', False)

    # when
    with tracing.opened('card run'):
        runner.run(Hostile(), SYSTEM, PROMPT, DAY, lambda: None)
    found.drain()

    # then
    assert 'alice@example.com' not in payload(http) and HOSTILE not in payload(http)
    first = by_type(http, 'generation')[0]
    assert json.loads(first['langfuse.observation.output'])['tool_calls'] == ['promql', 'unknown']
    promql, other = by_type(http, 'tool')
    assert json.loads(promql['langfuse.observation.input']) == {'tool': 'promql', 'args': ['expr'], 'other': 2}
    assert json.loads(other['langfuse.observation.input']) == {'tool': 'unknown', 'args': [], 'other': 1}
    assert [s['name'] for s in spans(http) if attrs(s)['langfuse.observation.type'] == 'tool'] == ['promql', 'unknown']


def test_long_tool_lists_and_odd_stops_are_capped_and_named_from_a_fixed_set():
    # given
    tools = [{'name': f't{n}'} for n in range(60)]
    turn = Turn(stop='weird-' + HOSTILE, model='m', usage=None, calls=[ToolCall(str(n), f't{n}', {}) for n in range(25)])

    # when
    given = tracing.asked_brief('S', [], tools, None)
    said = tracing.answer_brief(turn, tools)

    # then
    assert len(given['tools_offered']) == 51 and given['tools_offered'][-1] == '+10'
    assert len(said['tool_calls']) == 21 and said['tool_calls'][-1] == '+5' and said['stop_reason'] == 'other'


def test_an_unknown_outcome_is_reported_as_other_and_a_huge_brief_is_cut():
    # given
    trace = tracing.Trace('card run', {}, True, False, 1.0)

    # when
    trace.finished('weird ' + HOSTILE)
    trace.note_brief(trigger='x' * 50_000)
    shown = tracing.brief_text(trace.root_brief_in())

    # then
    assert trace.root_brief_out()['outcome'] == 'other' and len(shown) <= tracing.CONTENT_MAX


def test_a_tool_brief_that_fails_never_reaches_the_runner(monkeypatch):
    # given
    trace = tracing.Trace('card run', {}, True, False, 1.0)
    monkeypatch.setattr(tracing, 'tool_input_brief', lambda *a: (_ for _ in ()).throw(RuntimeError('bug')))

    # when
    trace.tool('promql', 1.0, 2.0, True, {'expr': 'up'}, 'ok')

    # then
    assert trace.spans[0].brief_in == {'tool': 'unknown', 'args': []}


def test_openai_errors_and_once_calls_send_no_customer_text(installed):
    # given
    import openai
    from tests.fake_openai import error as openai_error
    found, http = make(content=METADATA)
    installed(found)
    failing = FakeOpenAI([openai_error(openai.APIConnectionError)])
    answering = FakeOpenAI([openai_reply(openai_text(ANSWER))])

    # when
    with pytest.raises(openai.APIConnectionError):
        wrap(failing).turn('gpt-6.2-astra', 'low', SYSTEM, [{'name': 'promql', 'input_schema': {'type': 'object', 'properties': {}}}], [{'role': 'user', 'content': PROMPT}], 100)
    wrap(answering).once('gpt-6.2-astra', SYSTEM, [{'role': 'user', 'content': PROMPT}], 100, 'low', THINK_OFF, {'type': 'object', 'properties': {'answer': STR}, 'required': ['answer'], 'additionalProperties': False})
    found.drain()

    # then
    assert 'canary' not in payload(http) and 'alice' not in payload(http)
    failed, once = by_type(http, 'generation')
    assert json.loads(failed['langfuse.observation.output']) == {'error': 'APIConnectionError'} and json.loads(failed['langfuse.observation.input'])['tools_offered'] == ['promql']
    assert json.loads(once['langfuse.observation.input'])['tools_offered'] == [] and json.loads(once['langfuse.observation.output'])['answer_chars'] == len(ANSWER)
