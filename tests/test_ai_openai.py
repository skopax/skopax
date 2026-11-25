import json

import openai
import pytest

from tests.fake_openai import FakeOpenAI, call, error, reasoning, refusal, reply, search, text, usage
from tests.test_investigator_runner import FakeBox
from skopax.ai import THINK_AUTO, THINK_OFF, wrap
from skopax.ai.openai_ai import EFFORTS, REASONING_ROOM, OpenAiAi, reasoning_effort
from skopax.investigator import pricing
from skopax.investigator.runner import BROKEN_TEXT, CUT_TEXT, LIMIT_TEXT, MAX_TOKENS, RunConfig, Runner
from skopax.investigator.web import FETCH_TYPE, SEARCH_TYPE
from skopax.store import Store

DAY = '2026-11-20'
MODEL = 'gpt-6.2-astra'
TOOL = {'name': 'promql', 'description': 'Runs a query', 'input_schema': {'type': 'object', 'properties': {'expr': {'type': 'string'}}, 'required': ['expr'], 'additionalProperties': False}}
SEARCH = {'type': SEARCH_TYPE, 'name': 'web_search', 'max_uses': 2, 'allowed_domains': ['kafka.apache.org']}
FETCH = {'type': FETCH_TYPE, 'name': 'web_fetch', 'max_uses': 4, 'max_content_tokens': 20000}


def run(store, replies, max_calls=30, effort='medium', model=MODEL, box=None, tag=None):
    client = FakeOpenAI(replies)
    runner = Runner(client, store, RunConfig(model, effort, max_calls), lambda: 0.0, tag=tag)
    return runner.run(box or FakeBox(), 'SYSTEM', 'BRIEF', DAY, lambda: None), client


def test_a_fake_with_the_openai_flag_and_a_real_sdk_client_are_both_wrapped_by_the_openai_adapter():
    # when
    fake, real = wrap(FakeOpenAI([])), wrap(openai.OpenAI(api_key='test-key'))

    # then
    assert isinstance(fake, OpenAiAi) and isinstance(real, OpenAiAi) and fake.provider == real.provider == 'openai'


def test_one_turn_sends_the_responses_request_shape():
    # given
    client = FakeOpenAI([reply(text('done'))])
    strict = {**TOOL, 'name': 'triage', 'strict': True}

    # when
    turn = wrap(client).turn(MODEL, 'high', 'SYSTEM', [TOOL, strict], [{'role': 'user', 'content': 'BRIEF'}], MAX_TOKENS, '1h')

    # then
    sent = client.requests[0]
    assert (sent['model'], sent['instructions'], sent['max_output_tokens'], sent['reasoning'], sent['store']) == (MODEL, 'SYSTEM', MAX_TOKENS, {'effort': 'high'}, False)
    assert sent['include'] == ['reasoning.encrypted_content']
    assert sent['input'] == [{'role': 'user', 'content': 'BRIEF'}]
    assert sent['tools'] == [{'type': 'function', 'name': 'promql', 'description': 'Runs a query', 'parameters': TOOL['input_schema'], 'strict': False},
                             {'type': 'function', 'name': 'triage', 'description': 'Runs a query', 'parameters': TOOL['input_schema'], 'strict': True}]
    assert 'max_tool_calls' not in sent
    assert (turn.stop, turn.text, turn.calls, turn.model) == ('end_turn', 'done', [], MODEL)


def test_the_history_replays_reasoning_and_calls_and_answers_each_call_by_its_call_id():
    # given
    client = FakeOpenAI([reply(text('done'))])
    native = [reasoning(), call('promql', {'expr': 'up'}, 'call_a', 'fc_a'), call('promql', {'expr': 'down'}, 'call_b', 'fc_b')]
    history = [{'role': 'user', 'content': 'BRIEF'}, {'role': 'assistant', 'content': native},
               {'role': 'user', 'content': [{'type': 'tool_result', 'tool_use_id': 'call_a', 'content': 'rows', 'is_error': False},
                                            {'type': 'tool_result', 'tool_use_id': 'call_b', 'content': 'bad expr', 'is_error': True}]}]

    # when
    wrap(client).turn(MODEL, 'medium', 'SYSTEM', [], history, MAX_TOKENS)

    # then
    sent = client.requests[0]['input']
    assert sent[0] == {'role': 'user', 'content': 'BRIEF'}
    assert sent[1:4] == native
    assert sent[4:] == [{'type': 'function_call_output', 'call_id': 'call_a', 'output': 'rows'}, {'type': 'function_call_output', 'call_id': 'call_b', 'output': 'Error: bad expr'}]


def test_images_in_the_brief_and_in_tool_results_become_input_images():
    # given
    client = FakeOpenAI([reply(text('done'))])
    image = {'type': 'image', 'source': {'type': 'base64', 'media_type': 'image/png', 'data': 'AAAA'}}
    history = [{'role': 'user', 'content': [{'type': 'text', 'text': 'look'}, image]}, {'role': 'assistant', 'content': [call('shot', {}, 'call_s')]},
               {'role': 'user', 'content': [{'type': 'tool_result', 'tool_use_id': 'call_s', 'content': [{'type': 'text', 'text': 'a chart'}, image], 'is_error': False}]}]

    # when
    wrap(client).turn(MODEL, 'medium', 'SYSTEM', [], history, MAX_TOKENS)

    # then
    sent = client.requests[0]['input']
    picture = {'type': 'input_image', 'image_url': 'data:image/png;base64,AAAA'}
    assert sent[0] == {'role': 'user', 'content': [{'type': 'input_text', 'text': 'look'}, picture]}
    assert sent[2] == {'type': 'function_call_output', 'call_id': 'call_s', 'output': [{'type': 'input_text', 'text': 'a chart'}, picture]}


def test_several_tool_calls_in_one_turn_keep_their_call_ids_and_parsed_inputs():
    # given
    client = FakeOpenAI([reply(reasoning(), call('promql', {'expr': 'up'}, 'call_a', 'fc_a'), call('logs', '{"q": "error"}', 'call_b', 'fc_b'))])

    # when
    turn = wrap(client).turn(MODEL, 'medium', 'SYSTEM', [TOOL], [{'role': 'user', 'content': 'BRIEF'}], MAX_TOKENS)

    # then
    assert turn.stop == 'tool_use'
    assert [(c.id, c.name, c.input) for c in turn.calls] == [('call_a', 'promql', {'expr': 'up'}), ('call_b', 'logs', {'q': 'error'})]
    assert [i['type'] for i in turn.native] == ['reasoning', 'function_call', 'function_call']
    assert turn.native[0]['encrypted_content'] == 'sealed-thoughts'


@pytest.mark.parametrize('items, status, reason, stop', [
    ((text('half'),), 'incomplete', 'max_output_tokens', 'max_tokens'),
    ((call('promql', '{"expr": "u'),), 'incomplete', 'max_output_tokens', 'max_tokens'),
    ((refusal(),), 'completed', None, 'refusal'),
    ((text('x'),), 'incomplete', 'content_filter', 'refusal'),
], ids=['answer-cut', 'call-cut', 'refusal', 'filtered'])
def test_the_stop_reason_maps_to_the_neutral_one(items, status, reason, stop):
    # given
    client = FakeOpenAI([reply(*items, status=status, reason=reason)])

    # when
    turn = wrap(client).turn(MODEL, 'medium', 'SYSTEM', [], [{'role': 'user', 'content': 'BRIEF'}], MAX_TOKENS)

    # then
    assert turn.stop == stop


def test_a_call_cut_at_max_output_tokens_still_reaches_the_runner_with_an_empty_input():
    # given
    client = FakeOpenAI([reply(call('promql', '{"expr": "u'), status='incomplete', reason='max_output_tokens')])

    # when
    turn = wrap(client).turn(MODEL, 'medium', 'SYSTEM', [], [{'role': 'user', 'content': 'BRIEF'}], MAX_TOKENS)

    # then
    assert [(c.id, c.input) for c in turn.calls] == [('call_1', {})]


def test_usage_takes_cached_and_written_tokens_out_of_input_and_counts_searches():
    # given
    client = FakeOpenAI([reply(search(), search(sid='ws_2'), text('done'), u=usage(10_000, 500, cached=6_000, written=1_000, reasoning=300))])

    # when
    turn = wrap(client).turn(MODEL, 'medium', 'SYSTEM', [SEARCH], [{'role': 'user', 'content': 'BRIEF'}], MAX_TOKENS)

    # then
    assert turn.usage == pricing.Usage(input=3_000, cache_read=6_000, cache_write=1_000, cache_write_long=0, output=500, web_searches=2)


def test_the_web_search_tool_is_offered_with_the_domains_and_capped_by_the_searches_left_in_the_run():
    # given
    client = FakeOpenAI([reply(text('a')), reply(text('b'))])
    ai = wrap(client)
    history = [{'role': 'user', 'content': 'BRIEF'}]

    # when
    ai.turn(MODEL, 'medium', 'SYSTEM', [TOOL, SEARCH, FETCH], history, MAX_TOKENS)
    ai.turn(MODEL, 'medium', 'SYSTEM', [TOOL, SEARCH, FETCH], history + [{'role': 'assistant', 'content': [search(), search(sid='ws_2')]}, {'role': 'user', 'content': 'more'}], MAX_TOKENS)

    # then
    first, second = client.requests
    assert first['tools'][1:] == [{'type': 'web_search', 'filters': {'allowed_domains': ['kafka.apache.org']}}]
    assert first['max_tool_calls'] == 2 and 'web_search_call.action.sources' in first['include']
    assert [t['type'] for t in second['tools']] == ['function'] and 'max_tool_calls' not in second


def test_web_searches_are_reported_to_the_toolbox_as_search_calls_with_their_sources():
    # given
    client = FakeOpenAI([reply(search(), text('done'))])

    # when
    turn = wrap(client).turn(MODEL, 'medium', 'SYSTEM', [SEARCH], [{'role': 'user', 'content': 'BRIEF'}], MAX_TOKENS)

    # then
    assert turn.server == [{'type': 'server_tool_use', 'id': 'ws_1', 'name': 'web_search', 'input': {'query': 'kafka lag'}},
                           {'type': 'web_search_tool_result', 'tool_use_id': 'ws_1', 'content': [{'title': 'https://kafka.apache.org/doc'}]}]


@pytest.mark.parametrize('effort', ['low', 'medium', 'high', 'xhigh', 'max'])
def test_every_effort_maps_to_the_same_reasoning_effort(effort: str):
    # then
    assert reasoning_effort('gpt-6.2-sol', effort, THINK_AUTO) == EFFORTS[effort] == effort


def test_thinking_off_maps_to_none_where_the_model_has_it_and_to_low_elsewhere():
    # then
    assert [reasoning_effort(m, 'high', THINK_OFF) for m in ('gpt-6-luna', 'gpt-6.1-terra', 'gpt-6.2-sol', 'gpt-6.2-astra')] == ['none', 'none', 'low', 'low']


def test_a_single_call_with_a_schema_asks_for_strict_json_and_leaves_room_for_reasoning():
    # given
    client = FakeOpenAI([reply(text('{"ok": true}')), reply(text('hi'))])
    schema = {'type': 'object', 'properties': {'ok': {'type': 'boolean'}}, 'required': ['ok'], 'additionalProperties': False}
    ai = wrap(client)

    # when
    first = ai.once('gpt-6-luna', 'SYSTEM', [{'role': 'user', 'content': 'q'}], 300, 'low', THINK_OFF, schema)
    ai.once('gpt-6.2-sol', 'SYSTEM', [{'role': 'user', 'content': 'q'}], 300, 'medium', THINK_AUTO)

    # then
    gate, other = client.requests
    assert (gate['reasoning'], gate['max_output_tokens'], gate['store']) == ({'effort': 'none'}, 300, False)
    assert gate['text'] == {'format': {'type': 'json_schema', 'name': 'answer', 'schema': schema, 'strict': True}}
    assert (other['reasoning'], other['max_output_tokens']) == ({'effort': 'medium'}, 300 + REASONING_ROOM) and 'text' not in other
    assert first.texts == ['{"ok": true}'] and first.stop == 'end_turn'


def test_a_schema_that_is_not_closed_is_sent_without_strict():
    # given
    client = FakeOpenAI([reply(text('{}'))])
    schema = {'type': 'object', 'properties': {'ok': {'type': 'boolean'}}}

    # when
    wrap(client).once('gpt-6-luna', 'SYSTEM', [{'role': 'user', 'content': 'q'}], 300, 'low', THINK_OFF, schema)

    # then
    assert client.requests[0]['text']['format']['strict'] is False


def test_a_run_on_openai_calls_tools_answers_and_writes_ledger_rows_with_the_provider(store: Store):
    # given
    tag = {'source': 'pod', 'runId': 'r1', 'actionId': None, 'keys': ['k'], 'by': 'pod'}
    first = reply(reasoning(), call('promql', {'expr': 'up'}), u=usage(2_000, 100, cached=1_500))
    second = reply(text('Recap'), u=usage(3_000, 200, cached=2_000))

    # when
    result, client = run(store, [first, second], tag=tag)

    # then
    assert (result.outcome, result.turns, result.recap, result.error) == ('done', 2, 'Recap', None)
    assert result.usage == {'input_tokens': 1_500, 'output_tokens': 300, 'cache_read_input_tokens': 3_500, 'cache_creation_input_tokens': 0}
    expected = (1_500 * 8 + 300 * 40 + 3_500 * 0.4) / 1e6
    assert result.usd == pytest.approx(expected) and store.spent(DAY) == pytest.approx(expected)
    rows = list(store.ledger.find({'runId': 'r1'}))
    assert [(r['provider'], r['model'], r['tokens']['cacheRead'], r['tokens']['input']) for r in rows] == [('openai', MODEL, 1_500, 500), ('openai', MODEL, 2_000, 1_000)]
    assert client.requests[1]['input'][-1] == {'type': 'function_call_output', 'call_id': 'call_1', 'output': 'ok'}


def test_a_turn_cut_at_max_output_tokens_with_two_calls_answers_both_with_the_cut_text(store: Store):
    # given
    cut = reply(call('promql', '{"expr": "up"}', 'call_a', 'fc_a'), call('promql', '{"ex', 'call_b', 'fc_b'), status='incomplete', reason='max_output_tokens')

    # when
    result, client = run(store, [cut, reply(text('short answer'))])

    # then
    outputs = [i for i in client.requests[1]['input'] if i.get('type') == 'function_call_output']
    assert outputs == [{'type': 'function_call_output', 'call_id': 'call_a', 'output': 'Error: ' + CUT_TEXT}, {'type': 'function_call_output', 'call_id': 'call_b', 'output': 'Error: ' + CUT_TEXT}]
    assert (result.outcome, result.recap) == ('done', 'short answer')


def test_the_tool_limit_answers_extra_calls_with_the_limit_text(store: Store):
    # given
    many = reply(call('promql', {'expr': 'a'}, 'call_a', 'fc_a'), call('promql', {'expr': 'b'}, 'call_b', 'fc_b'))

    # when
    result, client = run(store, [many, reply(text('done'))], max_calls=1)

    # then
    outputs = [i['output'] for i in client.requests[1]['input'] if i.get('type') == 'function_call_output']
    assert outputs == ['ok', 'Error: ' + LIMIT_TEXT]


@pytest.mark.parametrize('cls, status', [(openai.AuthenticationError, 401), (openai.PermissionDeniedError, 403), (openai.NotFoundError, 404), (openai.RateLimitError, 429),
                                         (openai.APITimeoutError, None), (openai.APIConnectionError, None)],
                         ids=['auth', 'permission', 'not-found', 'rate-limit', 'timeout', 'connection'])
def test_an_openai_error_fails_the_run_with_the_error_kind(store: Store, cls, status):
    # when
    result, _ = run(store, [error(cls, status)])

    # then
    assert (result.outcome, result.error) == ('failed', cls.__name__)


def test_a_bad_request_on_the_first_turn_drops_the_web_search_and_goes_on(store: Store):
    # given
    class WebBox(FakeBox):
        def definitions(self):
            return super().definitions() + [SEARCH]

    # when
    result, client = run(store, [error(openai.BadRequestError, 400), reply(text('done'))], box=WebBox())

    # then
    assert result.outcome == 'done' and result.error.startswith('web tools refused by the API')
    assert [t['type'] for t in client.requests[1]['tools']] == ['function']


def test_a_claude_model_on_an_openai_client_falls_back_to_the_openai_default_of_its_role(store: Store):
    # given
    client = FakeOpenAI([reply(text('x')), reply(text('y'), model='gpt-6.2-sol')])

    # when
    card = Runner(client, store, RunConfig('claude-opus-5-5', 'medium', 5), lambda: 0.0).run(FakeBox(), 'SYSTEM', 'BRIEF', DAY, lambda: None)
    slack = Runner(client, store, RunConfig('claude-sonnet-5-5', 'medium', 5, role='slack'), lambda: 0.0).run(FakeBox(), 'SYSTEM', 'BRIEF', DAY, lambda: None)

    # then
    assert (card.outcome, card.models, slack.outcome, slack.models) == ('done', [MODEL], 'done', ['gpt-6.2-sol'])
    assert [r['model'] for r in client.requests] == [MODEL, 'gpt-6.2-sol']


def test_a_completed_call_with_arguments_that_are_not_json_is_answered_as_an_error_and_not_run(store: Store):
    # given
    box = FakeBox()
    first = reply(call('promql', '{"expr": "up"', 'call_a', 'fc_a'), call('promql', {'expr': 'down'}, 'call_b', 'fc_b'))

    # when
    result, client = run(store, [first, reply(text('done'))], box=box)

    # then
    outputs = [(i['call_id'], i['output']) for i in client.requests[1]['input'] if i.get('type') == 'function_call_output']
    assert outputs == [('call_a', 'Error: ' + BROKEN_TEXT), ('call_b', 'ok')]
    assert box.seen == [('promql', {'expr': 'down'})] and result.outcome == 'done'


def test_an_error_result_with_pictures_keeps_the_error_mark():
    # given
    client = FakeOpenAI([reply(text('done'))])
    image = {'type': 'image', 'source': {'type': 'base64', 'media_type': 'image/png', 'data': 'AAAA'}}
    history = [{'role': 'user', 'content': 'BRIEF'}, {'role': 'assistant', 'content': [call('shot', {}, 'call_s')]},
               {'role': 'user', 'content': [{'type': 'tool_result', 'tool_use_id': 'call_s', 'content': [{'type': 'text', 'text': 'blank page'}, image], 'is_error': True}]}]

    # when
    wrap(client).turn(MODEL, 'medium', 'SYSTEM', [], history, MAX_TOKENS)

    # then
    assert client.requests[0]['input'][2]['output'][0] == {'type': 'input_text', 'text': 'Error:'}


@pytest.mark.parametrize('status', ['failed', 'cancelled'])
def test_a_failed_or_cancelled_response_fails_the_run_and_raises_from_a_single_call(store: Store, status: str):
    # given
    from skopax.ai.openai_ai import ResponseFailed
    single = FakeOpenAI([reply(text('x'), status=status)])

    # when
    result, _ = run(store, [reply(text('x'), status=status)])
    with pytest.raises(ResponseFailed) as raised:
        wrap(single).once('gpt-6-luna', 'SYSTEM', [{'role': 'user', 'content': 'q'}], 300, 'low', THINK_OFF)

    # then
    assert (result.outcome, result.error) == ('failed', 'ResponseFailed') and raised.value.status == status


def test_openai_prices_split_cached_written_and_long_context_tokens():
    # given
    short = pricing.Usage(input=50_000, cache_read=50_000, cache_write=50_000, cache_write_long=0, output=1_000_000, web_searches=3)
    long = pricing.Usage(input=300_000)

    # when
    parts = pricing.split('gpt-6.2-sol', short)

    # then
    assert parts['usd'] == pytest.approx({'cacheRead': 0.005, 'input': 0.10, 'cacheWrite5m': 0.125, 'cacheWrite1h': 0.0, 'output': 10.00, 'webSearch': 0.03, 'total': 10.26})
    assert pricing.usd('gpt-6.2-sol', long) == pytest.approx(0.3 * 4.00)
    assert pricing.provider_of('gpt-6-luna') == 'openai' and pricing.provider_of('claude-haiku-5-5') == 'anthropic' and pricing.provider_of('gpt-x') is None


def test_a_chosen_model_of_another_provider_stays_on_its_provider_and_an_unset_one_follows_the_default_provider():
    # given
    keyed = {'anthropic': True, 'openai': False}.get

    # then
    assert pricing.resolve('claude-opus-5-5', 'card', 'openai', keyed, chosen=False) == ('openai', 'gpt-6.2-astra', None)
    assert pricing.resolve('claude-haiku-5-5', 'gate', 'openai', keyed) == ('anthropic', 'claude-haiku-5-5', None)
    assert pricing.resolve('gpt-6.2-sol', 'review', 'anthropic', keyed) == ('openai', 'gpt-6.2-sol', None)
    assert pricing.resolve('gpt-6.1-terra', 'card', 'openai', keyed) == ('openai', 'gpt-6.1-terra', None)
    assert pricing.resolve('', 'chat', 'openai', keyed, chosen=False) == ('openai', 'gpt-6.2-sol', None) and pricing.resolve('', 'chat', 'openai', keyed) == ('openai', '', None)


def test_the_openai_request_body_sent_by_the_real_sdk_has_no_key_in_it():
    # given
    import httpx2
    seen = []
    body = reply(text('ok')).model_dump(mode='json')

    def handle(request):
        seen.append(request)
        return httpx2.Response(200, json=body)

    client = openai.OpenAI(api_key='test-key', http_client=openai.DefaultHttpxClient(transport=httpx2.MockTransport(handle)), max_retries=0)

    # when
    turn = wrap(client).turn(MODEL, 'high', 'SYSTEM', [TOOL], [{'role': 'user', 'content': 'BRIEF'}], MAX_TOKENS)

    # then
    sent = json.loads(seen[0].content)
    assert (seen[0].method, seen[0].url.path) == ('POST', '/v1/responses')
    assert (sent['model'], sent['instructions'], sent['reasoning'], sent['store']) == (MODEL, 'SYSTEM', {'effort': 'high'}, False)
    assert 'test-key' not in seen[0].content.decode()
    assert (turn.text, turn.stop, turn.usage.input) == ('ok', 'end_turn', 1000)


def test_a_dated_snapshot_of_the_requested_model_is_priced_as_the_requested_model(store: Store):
    # given
    client = FakeOpenAI([reply(text('done'), model=MODEL + '-2026-10-30')])

    # when
    turn = wrap(client).turn(MODEL, 'medium', 'SYSTEM', [], [{'role': 'user', 'content': 'BRIEF'}], MAX_TOKENS)
    result, _ = run(store, [reply(text('done'), model=MODEL + '-2026-10-30')])

    # then
    assert turn.model == MODEL
    assert result.error is None and result.models == [MODEL]


def test_a_strict_tool_whose_schema_is_not_closed_is_sent_without_strict():
    # given
    client = FakeOpenAI([reply(text('done'))])
    loose = {**TOOL, 'name': 'note', 'strict': True, 'input_schema': {'type': 'object', 'properties': {'key': {'type': 'string'}, 'text': {'type': 'string'}}, 'required': ['key'], 'additionalProperties': False}}
    nested = {**TOOL, 'name': 'triage', 'strict': True, 'input_schema': {'type': 'object', 'properties': {'item': {'type': 'object', 'properties': {'a': {'type': 'string'}}}}, 'required': ['item'], 'additionalProperties': False}}

    # when
    wrap(client).turn(MODEL, 'medium', 'SYSTEM', [{**TOOL, 'strict': True}, loose, nested], [{'role': 'user', 'content': 'BRIEF'}], MAX_TOKENS)

    # then
    assert [t['strict'] for t in client.requests[0]['tools']] == [True, False, False]


def test_the_client_box_builds_with_the_provider_it_read_with_the_key():
    # given
    from skopax.ai import maker
    from skopax.aikey import ClientBox
    reads = iter(['anthropic', 'openai', 'openai', 'openai'])
    built = []
    box = ClientBox(lambda: 'test-key', maker(lambda: next(reads), lambda k: built.append(('anthropic', k)) or 'claude', 10.0, 0), scope=lambda: next(reads))

    # when
    made = box.get()

    # then
    assert isinstance(made, openai.OpenAI) and built == []


def test_the_openai_search_price_comes_from_its_own_entry(monkeypatch):
    # given
    monkeypatch.setitem(pricing.SEARCH_USD, 'openai', 0.025)
    used = pricing.Usage(web_searches=4)

    # when
    openai_side = pricing.split(MODEL, used)['usd']['webSearch']
    anthropic_side = pricing.split('claude-opus-5-5', used)['usd']['webSearch']

    # then
    assert (openai_side, anthropic_side) == (pytest.approx(0.10), pytest.approx(0.04))
