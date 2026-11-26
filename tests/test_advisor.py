import dataclasses
import json
import logging
from types import SimpleNamespace as NS

import anthropic
import httpx2
import pytest
from fastapi.testclient import TestClient

from tests.conftest import AUTH, anomaly, settings
from tests.fake_anthropic import FakeAnthropic, reply, text, tool, usage
from tests.test_card_model_override import CRITICAL, dive, investigator
from tests.test_investigator import KEY, NOW, note_and_recap, seed
from tests.test_investigator_slack import slack_action, slack_investigator
from skopax import advisor, clock, tracing
from skopax.ai import ANTHROPIC, OPENAI, Clients, PROVIDER_CAPABILITIES, Turn, Usage, capabilities_of
from skopax.ai.anthropic_ai import ADVISOR_BETA, FALLBACK_BETA, AnthropicAi, turn_of
from skopax.ai.vertex_ai import VERTEX_CAPABILITIES
from skopax.app import create_app
from skopax.config import load_settings
from skopax.investigator import pricing
from skopax.investigator.runner import RunConfig, Runner
from skopax.messages import msg
from skopax.store import Store
from skopax.tunables import is_model_setting, registry

DAY = '2026-10-21'
SENTINEL = 'ZQX-advice-sentinel-91'
EXECUTOR = 'claude-haiku-5-5'
PLAIN = 'claude-opus-4-8'
STRONG = 'claude-opus-5-5'
PLAN = advisor.Plan(PLAIN, 3, 2000)
TAG = {'source': 'pod', 'runId': 'run-1', 'actionId': None, 'keys': [], 'by': 'pod', 'use': 'critical'}
PATH = '/api/admin/ai-uses'


class Box:
    def definitions(self):
        return [{'name': 'promql', 'description': 'x', 'input_schema': {'type': 'object', 'properties': {}, 'required': [], 'additionalProperties': False}}]

    def call(self, name, args):
        return 'ok', False


def call(sid: str = 'srvtoolu_1') -> NS:
    return NS(type='server_tool_use', id=sid, name='advisor', input={})


def plain(sid: str = 'srvtoolu_1', words: str = SENTINEL) -> NS:
    return NS(type='advisor_tool_result', tool_use_id=sid, content=NS(type='advisor_result', text=words, stop_reason='end_turn'))


def redacted(sid: str = 'srvtoolu_1') -> NS:
    return NS(type='advisor_tool_result', tool_use_id=sid, content=NS(type='advisor_redacted_result', encrypted_content=SENTINEL + '-blob', stop_reason='end_turn'))


def failed(code: str, sid: str = 'srvtoolu_1') -> NS:
    return NS(type='advisor_tool_result', tool_use_id=sid, content=NS(type='advisor_tool_result_error', error_code=code))


def consulted(model: str = PLAIN, i: int = 800, o: int = 1500) -> NS:
    return NS(type='advisor_message', model=model, input_tokens=i, output_tokens=o, cache_read_input_tokens=0, cache_creation_input_tokens=0)


def billed(*iterations: NS, i: int = 1000, o: int = 200) -> NS:
    found = usage(i, o)
    found.iterations = list(iterations)
    return found


def go(store: Store, replies: list, plan=PLAN, model: str = EXECUTOR, halt=lambda: None, caps=None, tag=TAG, client=None):
    client = client or FakeAnthropic(replies)
    runner = Runner(client, store, RunConfig(model, 'medium', 30, advisor=plan, capabilities=caps), lambda: 0.0, tag=tag)
    return runner.run(Box(), 'SYSTEM', 'BRIEF', DAY, halt), client


def everything(store: Store, caplog, result=None) -> str:
    docs = [store.ledger_since('0000'), list(store.investigations.find({})), list(store.activity.find({})) if hasattr(store, 'activity') else []]
    return json.dumps(docs, default=str) + caplog.text + (json.dumps(result.__dict__, default=str) if result is not None else '')


def test_the_pairing_choices_follow_the_docs_table_and_keep_to_priced_models():
    # then
    assert advisor.choices('claude-opus-5-5') == ('claude-mythos-5-2', 'claude-fable-5-2', 'claude-opus-5-5', 'claude-opus-5')
    assert advisor.choices('claude-sonnet-5-5') == ('claude-mythos-5-2', 'claude-fable-5-2', 'claude-opus-5-5', 'claude-opus-5', 'claude-sonnet-5-5')
    assert advisor.choices('claude-haiku-5-5') == ('claude-mythos-5-2', 'claude-fable-5-2', 'claude-opus-5-5', 'claude-opus-5', 'claude-opus-4-8', 'claude-sonnet-5-5', 'claude-haiku-5-5')
    assert advisor.choices('claude-fable-5-2') == ('claude-mythos-5-2', 'claude-fable-5-2')
    assert advisor.choices('gpt-6.2-astra') == () and advisor.choices('') == () and advisor.choices('claude-unreleased-9') == ()
    assert all(m in pricing.PRICES for found in advisor.PAIRINGS for m in advisor.choices(found))


def test_only_the_anthropic_provider_holds_the_advisor_capability():
    # given
    bare = NS(turn=lambda *a: None, once=lambda *a: None)
    registered = NS(capabilities=frozenset({'tools', 'advisor'}))

    # then
    assert 'advisor' in PROVIDER_CAPABILITIES[ANTHROPIC]
    assert 'advisor' not in PROVIDER_CAPABILITIES[OPENAI] and 'advisor' not in VERTEX_CAPABILITIES
    assert 'advisor' not in capabilities_of(bare)
    assert 'advisor' not in Clients(lambda: ANTHROPIC, {}, lambda key: None, 1.0, 0).capabilities('somewhere')
    assert 'advisor' in AnthropicAi(object()).capabilities
    assert advisor.problem_of(PLAIN, 'vertex:eu', capabilities_of(registered), EXECUTOR) == msg('advisor.not_anthropic')
    assert advisor.problem_of(PLAIN, 'compat:local', {'advisor'}, EXECUTOR) == msg('advisor.not_anthropic')


@pytest.mark.parametrize('provider, caps, executor, model, problem', [
    (ANTHROPIC, {'advisor'}, EXECUTOR, PLAIN, None),
    (ANTHROPIC, {'advisor'}, EXECUTOR, '', None),
    (OPENAI, PROVIDER_CAPABILITIES[OPENAI], 'gpt-6.2-astra', STRONG, msg('advisor.not_anthropic')),
    ('vertex:eu', VERTEX_CAPABILITIES, 'eu/claude-opus-5-5', STRONG, msg('advisor.not_anthropic')),
    (ANTHROPIC, {'tools'}, EXECUTOR, STRONG, msg('advisor.not_anthropic')),
    (ANTHROPIC, {'advisor'}, 'claude-opus-5-5', 'claude-sonnet-5-5', msg('advisor.pair_invalid', advisor='claude-sonnet-5-5', executor='claude-opus-5-5')),
    (ANTHROPIC, {'advisor'}, 'claude-unreleased-9', STRONG, msg('advisor.executor_unsupported', executor='claude-unreleased-9')),
])
def test_the_one_resolver_gives_a_plan_or_a_plain_problem(provider: str, caps, executor: str, model: str, problem):
    # given
    held = dataclasses.replace(settings(), investigate_model_critical_advisor=model, investigate_model_critical_advisor_uses=4, investigate_model_critical_advisor_tokens=1500)

    # when
    plan, said = advisor.resolve(held, 'critical', provider, caps, executor)

    # then
    assert said == problem
    assert plan == (advisor.Plan(model, 4, 1500) if model and problem is None else None)


def test_the_tool_definition_is_the_pinned_shape_and_caches_only_long_loops():
    # then
    assert advisor.Plan(STRONG, 3, 2000).tool() == {'type': 'advisor_20260301', 'name': 'advisor', 'model': STRONG, 'max_uses': 3, 'max_tokens': 2000, 'caching': {'type': 'ephemeral', 'ttl': '5m'}}
    assert advisor.Plan(STRONG, 2, 1024).tool() == {'type': 'advisor_20260301', 'name': 'advisor', 'model': STRONG, 'max_uses': 2, 'max_tokens': 1024}
    assert ADVISOR_BETA == 'advisor-tool-2026-03-01'


def test_the_settings_load_with_the_pinned_names_defaults_and_bounds():
    # given
    env = {'INVESTIGATE_MODEL_CRITICAL_ADVISOR': ' claude-fable-5-2 ', 'INVESTIGATE_MODEL_CRITICAL_ADVISOR_USES': '99', 'INVESTIGATE_MODEL_CRITICAL_ADVISOR_TOKENS': '5',
           'CHAT_MODEL_ADVISOR': STRONG, 'INVESTIGATE_SLACK_MODEL_ADVISOR_USES': 'x'}

    # when
    base = load_settings(env={})
    got = load_settings(env=env)

    # then
    assert [(base.__dict__[f'{n}_advisor'], base.__dict__[f'{n}_advisor_uses'], base.__dict__[f'{n}_advisor_tokens']) for n in
            ('investigate_model', 'investigate_model_critical', 'investigate_model_high', 'investigate_slack_model', 'chat_model')] == [('', 3, 2000)] * 5
    assert (got.investigate_model_critical_advisor, got.investigate_model_critical_advisor_uses, got.investigate_model_critical_advisor_tokens) == ('claude-fable-5-2', 10, 1024)
    assert (got.chat_model_advisor, got.investigate_slack_model_advisor_uses) == (STRONG, 3)
    assert {k for k in advisor.advisor_keys()} >= {'investigate.model-critical-advisor', 'investigate.model-critical-advisor-uses', 'investigate.model-critical-advisor-tokens',
                                                    'investigate.model-advisor', 'investigate.model-high-advisor', 'investigate.slack-model-advisor', 'chat.model-advisor'}
    assert len(advisor.advisor_keys()) == 15


def test_the_advisor_settings_are_registered_but_are_not_model_settings():
    # given
    found = {t.key: t for t in registry() if t.key in advisor.advisor_keys()}

    # then
    assert len(found) == 15
    assert not any(is_model_setting(t) for t in found.values())
    assert found['investigate.model-critical-advisor-uses'].low == 1 and found['investigate.model-critical-advisor-uses'].high == 10
    assert found['chat.model-advisor-tokens'].low == 1024 and found['chat.model-advisor-tokens'].high == 4096


def put(c: TestClient, key: str, value):
    return c.put('/api/admin/settings', json={'key': key, 'value': value}, headers=AUTH)


def client_for(store: Store, **over) -> TestClient:
    return TestClient(create_app(dataclasses.replace(settings(), anthropic_api_key='test-anthropic-key', **over), store, clock=lambda: NOW))


def test_a_valid_pair_saves_and_an_invalid_pair_or_a_foreign_model_is_refused_with_a_plain_reason(store: Store):
    # given
    c = client_for(store, investigate_model_high='claude-sonnet-5-5')

    # when
    good = put(c, 'investigate.model-critical-advisor', 'claude-fable-5-2')
    off_table = put(c, 'investigate.model-high-advisor', 'claude-haiku-5-5')
    foreign = put(c, 'investigate.model-advisor', 'gpt-6.2-astra')
    ref = put(c, 'investigate.model-advisor', 'eu/claude-opus-5-5')
    cleared = put(c, 'investigate.model-critical-advisor', '')

    # then
    assert good.status_code == 200 and good.json()['item']['value'] == 'claude-fable-5-2'
    assert off_table.status_code == 422 and off_table.json()['detail'] == msg('advisor.pair_invalid', advisor='claude-haiku-5-5', executor='claude-sonnet-5-5')
    assert foreign.status_code == 422 and 'must be one of' in foreign.json()['detail']
    assert ref.status_code == 422
    assert cleared.status_code == 200 and cleared.json()['item']['value'] == ''


@pytest.mark.parametrize('key, value', [('investigate.model-critical-advisor-uses', '0'), ('investigate.model-critical-advisor-uses', '11'), ('chat.model-advisor-tokens', '1023'),
                                         ('chat.model-advisor-tokens', '4097'), ('chat.model-advisor-uses', 'many')])
def test_the_consultation_count_and_answer_size_are_bounded_on_save(store: Store, key: str, value: str):
    # given
    c = client_for(store)

    # then
    assert put(c, key, value).status_code == 422
    assert put(c, key, '5' if key.endswith('uses') else '2048').status_code == 200


def test_a_use_on_a_foreign_provider_cannot_save_an_advisor(store: Store):
    # given
    c = client_for(store, ai_provider='openai', openai_api_key='test-openai-key', investigate_model='gpt-6.2-astra')

    # when
    refused = put(c, 'investigate.model-advisor', STRONG)

    # then
    assert refused.status_code == 422 and refused.json()['detail'] == msg('advisor.not_anthropic')


def rows(c: TestClient) -> dict:
    return {r['use']: r for r in c.get(PATH, headers=AUTH).json()['uses']}


def test_the_use_rows_carry_the_pinned_advisor_object_for_the_five_tool_loop_uses_and_null_for_the_rest(store: Store):
    # given
    c = client_for(store, investigate_model_critical='claude-opus-5', investigate_model_critical_advisor='claude-fable-5-2', investigate_model_critical_advisor_uses=5,
                   investigate_model_critical_advisor_tokens=3000)

    # when
    got = rows(c)

    # then
    assert [got[u]['advisor'] for u in ('gate', 'review', 'plugin')] == [None, None, None]
    assert set(got['critical']['advisor']) == {'model', 'choices', 'maxUses', 'maxTokens', 'capable', 'problem'}
    assert got['critical']['advisor'] == {'model': 'claude-fable-5-2', 'choices': list(advisor.choices('claude-opus-5')), 'maxUses': 5, 'maxTokens': 3000, 'capable': True, 'problem': None}
    assert got['card']['advisor'] == {'model': '', 'choices': list(advisor.choices('claude-opus-5-5')), 'maxUses': 3, 'maxTokens': 2000, 'capable': True, 'problem': None}
    assert all(got[u]['advisor'] is not None for u in ('card', 'critical', 'high', 'slack', 'chat'))


def test_a_stored_advisor_that_no_longer_fits_shows_a_problem_and_nothing_is_moved(store: Store):
    # given
    c = client_for(store, ai_provider='anthropic', openai_api_key='test-openai-key', chat_model='gpt-6.1-terra', chat_model_advisor=STRONG, investigate_model_advisor='claude-sonnet-5-5')

    # when
    got = rows(c)

    # then
    assert got['chat']['advisor'] == {'model': STRONG, 'choices': [], 'maxUses': 3, 'maxTokens': 2000, 'capable': False, 'problem': msg('advisor.not_anthropic')}
    assert (got['chat']['provider'], got['chat']['model']) == ('openai', 'gpt-6.1-terra')
    assert got['card']['advisor']['problem'] == msg('advisor.pair_invalid', advisor='claude-sonnet-5-5', executor='claude-opus-5-5')
    assert (got['card']['model'], got['card']['advisor']['model']) == ('claude-opus-5-5', 'claude-sonnet-5-5')


def test_the_request_carries_the_tool_and_the_beta_only_when_the_run_has_an_advisor(store: Store):
    # when
    _, off = go(store, [reply(text('done'), stop='end_turn')], plan=None)
    _, on = go(store, [reply(text('done'), stop='end_turn')])
    _, no_fallbacks = go(store, [reply(text('done'), stop='end_turn')], caps=frozenset({'tools', 'advisor', 'thinking'}))

    # then
    assert off.requests[0]['betas'] == [FALLBACK_BETA] and all(t.get('name') != 'advisor' for t in off.requests[0]['tools'])
    assert on.requests[0]['betas'] == [ADVISOR_BETA] and 'extra_body' not in on.requests[0]
    assert on.requests[0]['tools'][-1] == PLAN.tool() and on.requests[0]['tools'][0]['name'] == 'promql'
    assert no_fallbacks.requests[0]['betas'] == [ADVISOR_BETA] and 'extra_body' not in no_fallbacks.requests[0]


def test_every_request_of_a_run_carries_the_identical_tool_definition_and_history_replays_the_advisor_blocks(store: Store):
    # given
    first = [text('look'), call(), plain(), tool('promql', {'expr': 'up'})]
    replies = [reply(*first, u=billed(consulted())), reply(call('srvtoolu_2'), redacted('srvtoolu_2'), tool('promql', {}, 't2'), u=billed(consulted(STRONG))), reply(text('Recap'), stop='end_turn')]

    # when
    result, client = go(store, replies)

    # then
    assert result.outcome == 'done' and result.advisor_calls == 2
    assert len({json.dumps(r['tools'][-1], sort_keys=True) for r in client.requests}) == 1 and len(client.requests) == 3
    replayed = client.requests[1]['messages'][1]['content']
    assert replayed is replies[0].content and [b.type for b in replayed] == ['text', 'server_tool_use', 'advisor_tool_result', 'tool_use']
    assert client.requests[2]['messages'][3]['content'][1].type == 'advisor_tool_result'


def test_a_pause_turn_resumes_with_the_same_advisor_tool_and_the_pause_cap_allows_the_consultations(store: Store):
    # given
    pauses = [reply(call(f'srvtoolu_{n}'), stop='pause_turn') for n in range(3)]

    # when
    result, client = go(store, pauses + [reply(text('Recap'), stop='end_turn')], plan=advisor.Plan(PLAIN, 10, 2000))
    web, _ = go(store, [reply(call(), stop='pause_turn')] * 9, plan=None)

    # then
    assert (result.outcome, result.turns) == ('done', 4)
    assert all(r['tools'][-1]['type'] == 'advisor_20260301' and r['betas'] == [ADVISOR_BETA] for r in client.requests)
    assert client.requests[1]['messages'][1]['content'] == pauses[0].content
    assert web.outcome == 'failed' and web.error == 'the server tools kept pausing the turn'


@pytest.mark.parametrize('code', ['max_uses_exceeded', 'overloaded', 'too_many_requests', 'prompt_too_long', 'execution_time_exceeded', 'model_not_found', 'unavailable', 'brand_new_code'])
def test_an_advisor_error_result_lets_the_run_go_on_without_advice(store: Store, code: str):
    # given
    replies = [reply(call(), failed(code), tool('promql', {}), u=billed()), reply(text('Recap without advice'), stop='end_turn')]

    # when
    result, client = go(store, replies)

    # then
    assert (result.outcome, result.recap, result.error, result.advisor_calls, result.advisor_errors) == ('done', 'Recap without advice', None, 0, 1)
    assert len(client.requests) == 2


def test_an_unknown_result_shape_counts_as_no_advice_and_never_crashes(store: Store):
    # given
    odd = NS(type='advisor_tool_result', tool_use_id='srvtoolu_1', content=NS(type='advisor_new_kind', words=SENTINEL))
    replies = [reply(call(), odd, stop='end_turn', u=billed())]

    # when
    result, _ = go(store, replies)

    # then
    assert (result.outcome, result.advisor_calls, result.advisor_errors) == ('done', 0, 1)


def test_advice_text_and_encrypted_blobs_are_never_read_logged_stored_or_traced(store: Store, caplog):
    # given
    caplog.set_level(logging.DEBUG)
    replies = [reply(text('checking'), call(), plain(), call('srvtoolu_2'), redacted('srvtoolu_2'), tool('promql', {}), u=billed(consulted(), consulted(STRONG))), reply(text('Recap'), stop='end_turn')]

    # when
    result, client = go(store, replies)
    turn = turn_of(replies[0], EXECUTOR)

    # then
    assert result.outcome == 'done' and result.advisor_calls == 2
    assert SENTINEL not in everything(store, caplog, result) and SENTINEL not in json.dumps(Box().call('promql', {}))
    assert SENTINEL not in json.dumps(tracing.answer(turn), default=str) and SENTINEL not in tracing.plain_text(replies[0].content) and turn.text == 'checking'
    assert SENTINEL not in tracing.asked('SYSTEM', [{'role': 'assistant', 'content': replies[0].content}])['last']['text']


def test_advisor_spend_is_priced_by_the_advisor_model_ledgered_apart_and_counted_in_the_day(store: Store):
    # given
    replies = [reply(call(), plain(), stop='end_turn', u=billed(consulted(PLAIN, 800, 1500), i=1000, o=200))]
    rates = pricing.PRICES[PLAIN]

    # when
    result, _ = go(store, replies)

    # then
    rows = store.ledger_since('0000')
    mine = next(r for r in rows if r.get('role') == 'advisor')
    executor = next(r for r in rows if r.get('role') != 'advisor')
    expected = (800 * rates['input_tokens'] + 1500 * rates['output_tokens']) / 1_000_000
    assert (mine['model'], mine['use'], mine['runId'], mine['provider']) == (PLAIN, 'critical', 'run-1', 'anthropic')
    assert mine['usd']['total'] == pytest.approx(expected) and mine['tokens']['input'] == 800 and mine['tokens']['output'] == 1500
    assert executor['model'] == EXECUTOR and 'role' not in executor and executor['tokens']['input'] == 1000
    assert result.usd == pytest.approx(executor['usd']['total'] + expected, abs=1e-6) and store.spent(DAY) == pytest.approx(result.usd)


def test_advisor_spend_counts_against_the_daily_cap_even_without_a_ledger_tag(store: Store):
    # given
    cap = 0.05
    replies = [reply(call(), plain(), tool('promql', {}), u=billed(consulted(STRONG, 5000, 3000))), reply(text('never'), stop='end_turn')]

    # when
    result, client = go(store, replies, tag=None, halt=lambda: 'capped' if store.spent(DAY) >= cap else None)

    # then
    assert store.ledger_since('0000') == []
    assert store.spent(DAY) > cap and (result.outcome, len(client.requests)) == ('capped', 1)


def test_an_unpriced_advisor_is_charged_at_the_highest_known_rate_and_said_so(store: Store):
    # given
    replies = [reply(call(), plain(), stop='end_turn', u=billed(consulted('claude-unreleased-9', 1000, 1000)))]

    # when
    result, _ = go(store, replies)

    # then
    row = next(r for r in store.ledger_since('0000') if r.get('role') == 'advisor')
    assert row['model'] == 'claude-unreleased-9' and row['pricedAs'] in pricing.PRICES and row['usd']['total'] > 0
    assert result.error.startswith('unpriced advisor model claude-unreleased-9')


class Foreign:
    provider = 'openai'
    capabilities = frozenset({'tools'})
    bad_request = anthropic.BadRequestError
    api_error = anthropic.APIError

    def __init__(self):
        self.asked = []

    def turn(self, model, effort, system, tools, messages, max_tokens, cache_ttl=None, thinking='auto', fallbacks=True):
        self.asked.append(tools)
        return Turn(stop='end_turn', model=model, usage=Usage(input=10, output=2), texts=['fine'], text='fine', native=[], server=[])

    def once(self, *a, **k):
        raise AssertionError('not used')


def test_a_run_on_another_provider_never_sends_the_advisor_even_when_one_is_in_the_config(store: Store):
    # given
    foreign = Foreign()

    # when
    result, _ = go(store, [], plan=advisor.Plan(STRONG, 3, 2000), model='gpt-6.2-astra', client=foreign, caps=frozenset({'tools', 'advisor'}))

    # then
    assert result.outcome == 'done' and foreign.asked and all(t.get('name') != 'advisor' for t in foreign.asked[0])
    assert result.error == msg('advisor.dropped', advisor=STRONG, executor='gpt-6.2-astra')


def test_a_plan_that_no_longer_pairs_with_the_model_in_hand_is_dropped_before_the_first_request(store: Store):
    # when
    result, client = go(store, [reply(text('done'), stop='end_turn')], plan=advisor.Plan('claude-sonnet-5-5', 3, 2000), model='claude-opus-5-5')

    # then
    assert result.outcome == 'done' and all(t.get('name') != 'advisor' for t in client.requests[0]['tools']) and client.requests[0]['betas'] == [FALLBACK_BETA]
    assert result.error == msg('advisor.dropped', advisor='claude-sonnet-5-5', executor='claude-opus-5-5')


def refused(code: int = 400):
    return anthropic.BadRequestError('invalid pair', response=httpx2.Response(code, request=httpx2.Request('POST', 'https://api.anthropic.com/v1/messages')), body=None)


def test_a_refused_advisor_on_the_first_turn_drops_it_once_and_says_so_while_a_later_refusal_fails_the_run(store: Store):
    # when
    first, client = go(store, [refused(), reply(text('Recap'), stop='end_turn')])
    later, kept = go(store, [reply(call(), stop='pause_turn'), refused()])

    # then
    assert (first.outcome, first.recap) == ('done', 'Recap') and first.error.startswith('the API refused the advisor, the run went on without it')
    assert client.requests[0]['tools'][-1]['name'] == 'advisor' and client.requests[1]['tools'][-1]['name'] == 'promql'
    assert client.requests[1]['betas'] == [FALLBACK_BETA]
    assert later.outcome == 'failed' and kept.requests[1]['tools'][-1]['name'] == 'advisor'


def wire_message(extra_usage: dict | None = None, blocks=None, stop: str = 'end_turn') -> dict:
    body = {'id': 'msg_1', 'type': 'message', 'role': 'assistant', 'model': EXECUTOR, 'stop_reason': stop, 'stop_sequence': None,
            'content': blocks if blocks is not None else [{'type': 'text', 'text': 'ok'}],
            'usage': {'input_tokens': 10, 'output_tokens': 4, 'cache_read_input_tokens': 0, 'cache_creation_input_tokens': 0} | (extra_usage or {})}
    return body


def test_through_the_real_sdk_the_request_has_the_beta_header_and_tool_body_and_usage_iterations_and_blocks_round_trip(store: Store):
    # given
    first = wire_message({'iterations': [{'type': 'message', 'input_tokens': 10, 'output_tokens': 2, 'cache_read_input_tokens': 0, 'cache_creation_input_tokens': 0},
                                         {'type': 'advisor_message', 'model': PLAIN, 'input_tokens': 800, 'output_tokens': 1500, 'cache_read_input_tokens': 0, 'cache_creation_input_tokens': 0}]},
                         [{'type': 'server_tool_use', 'id': 'srvtoolu_1', 'name': 'advisor', 'input': {}},
                          {'type': 'advisor_tool_result', 'tool_use_id': 'srvtoolu_1', 'content': {'type': 'advisor_result', 'text': SENTINEL}},
                          {'type': 'tool_use', 'id': 'toolu_1', 'name': 'promql', 'input': {'expr': 'up'}}], 'tool_use')
    second = wire_message(blocks=[{'type': 'text', 'text': 'Recap'}])
    answers = iter([first, second])
    seen: list[httpx2.Request] = []

    def handle(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return httpx2.Response(200, json=next(answers))

    client = anthropic.Anthropic(api_key='sk-wire-' + 'w' * 32, max_retries=0, http_client=anthropic.DefaultHttpxClient(transport=httpx2.MockTransport(handle)))
    tag = dict(TAG)

    # when
    result, _ = go(store, [], client=client, tag=tag)

    # then
    sent = [json.loads(r.content) for r in seen]
    assert result.outcome == 'done' and result.advisor_calls == 1
    assert seen[0].headers['anthropic-beta'] == ADVISOR_BETA and seen[1].headers['anthropic-beta'] == seen[0].headers['anthropic-beta']
    assert sent[0]['tools'][-1] == {'type': 'advisor_20260301', 'name': 'advisor', 'model': PLAIN, 'max_uses': 3, 'max_tokens': 2000, 'caching': {'type': 'ephemeral', 'ttl': '5m'}}
    assert sent[1]['tools'][-1] == sent[0]['tools'][-1]
    echoed = sent[1]['messages'][1]['content']
    assert [b['type'] for b in echoed] == ['server_tool_use', 'advisor_tool_result', 'tool_use'] and echoed[1]['content'] == {'type': 'advisor_result', 'text': SENTINEL}
    mine = next(r for r in store.ledger_since('0000') if r.get('role') == 'advisor')
    assert (mine['model'], mine['tokens']['input'], mine['tokens']['output']) == (PLAIN, 800, 1500)


def test_through_the_real_sdk_an_encrypted_result_round_trips_without_being_read(store: Store):
    # given
    blocks = [{'type': 'server_tool_use', 'id': 'srvtoolu_1', 'name': 'advisor', 'input': {}},
              {'type': 'advisor_tool_result', 'tool_use_id': 'srvtoolu_1', 'content': {'type': 'advisor_redacted_result', 'encrypted_content': 'opaque-blob', 'stop_reason': 'end_turn'}},
              {'type': 'tool_use', 'id': 'toolu_1', 'name': 'promql', 'input': {}}]
    answers = iter([wire_message(blocks=blocks, stop='tool_use'), wire_message(blocks=[{'type': 'text', 'text': 'Recap'}])])
    seen: list[httpx2.Request] = []

    def handle(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return httpx2.Response(200, json=next(answers))

    client = anthropic.Anthropic(api_key='sk-wire-' + 'w' * 32, max_retries=0, http_client=anthropic.DefaultHttpxClient(transport=httpx2.MockTransport(handle)))

    # when
    result, _ = go(store, [], client=client)

    # then
    echoed = json.loads(seen[1].content)['messages'][1]['content']
    assert result.outcome == 'done' and result.advisor_calls == 1
    assert echoed[1]['content'] == {'type': 'advisor_redacted_result', 'encrypted_content': 'opaque-blob', 'stop_reason': 'end_turn'}


def advised(store: Store, replies: list, **over):
    return investigator(store, replies, **over)


def advisor_asks(inv) -> list[dict | None]:
    return [next((t for t in r['tools'] if t.get('name') == 'advisor'), None) for r in inv.client.requests]


def system_of(inv) -> str:
    return inv.client.requests[0]['system'][0]['text']


def test_a_critical_card_uses_the_critical_advisor_and_the_prompt_nudge_follows(store: Store):
    # given
    seed(store, anomaly(KEY, sev='crit', firstSeen='2026-10-22T05:00:00Z'))
    inv = advised(store, note_and_recap(), investigate_model_critical=CRITICAL, investigate_model_critical_advisor='claude-fable-5-2', investigate_model_critical_advisor_uses=4,
                  investigate_model_critical_advisor_tokens=1500)

    # when
    record = inv.run_once(NOW)

    # then
    assert record['outcome'] == 'done' and record['model'] == CRITICAL
    assert all(a == {'type': 'advisor_20260301', 'name': 'advisor', 'model': 'claude-fable-5-2', 'max_uses': 4, 'max_tokens': 1500, 'caching': {'type': 'ephemeral', 'ttl': '5m'}} for a in advisor_asks(inv))
    assert advisor.NUDGE in system_of(inv) and all(ADVISOR_BETA in r['betas'] for r in inv.client.requests)


def test_a_card_run_without_an_advisor_has_no_tool_no_beta_and_no_nudge(store: Store):
    # given
    seed(store, anomaly(KEY, sev='crit', firstSeen='2026-10-22T05:00:00Z'))
    inv = advised(store, note_and_recap(), investigate_model_critical=CRITICAL)

    # when
    record = inv.run_once(NOW)

    # then
    assert record['outcome'] == 'done' and advisor_asks(inv) == [None, None]
    assert advisor.NUDGE not in system_of(inv) and all(r['betas'] == [FALLBACK_BETA] for r in inv.client.requests) and record['advisorCalls'] == 0


def test_the_advisor_follows_the_severity_even_when_the_critical_card_runs_on_the_card_model(store: Store):
    # given
    seed(store, anomaly(KEY, sev='crit', firstSeen='2026-10-22T05:00:00Z'))
    inv = advised(store, note_and_recap(), investigate_model_critical_advisor=STRONG, investigate_model_advisor='claude-mythos-5-2')

    # when
    record = inv.run_once(NOW)

    # then
    assert record['model'] == 'claude-opus-5-5' and {a['model'] for a in advisor_asks(inv)} == {STRONG}


def test_a_high_card_with_no_high_advisor_gets_none_whatever_the_other_uses_hold(store: Store):
    # given
    seed(store, anomaly(KEY, sev='high', firstSeen='2026-10-22T05:00:00Z'))
    inv = advised(store, note_and_recap(), investigate_model_advisor='claude-fable-5-2', investigate_model_critical_advisor=STRONG)

    # when
    inv.run_once(NOW)

    # then
    assert advisor_asks(inv) == [None, None]


def test_a_warning_card_uses_the_card_advisor(store: Store):
    # given
    seed(store, anomaly(KEY, sev='warn', firstSeen='2026-10-22T05:00:00Z'))
    dive(store, KEY)
    inv = advised(store, note_and_recap(), investigate_model_advisor='claude-fable-5-2', investigate_model_critical_advisor=STRONG)

    # when
    inv.run_once(NOW)

    # then
    assert {a['model'] for a in advisor_asks(inv)} == {'claude-fable-5-2'}


def test_the_run_record_counts_advisor_calls_and_stores_no_advice(store: Store, caplog):
    # given
    caplog.set_level(logging.DEBUG)
    seed(store, anomaly(KEY, sev='crit', firstSeen='2026-10-22T05:00:00Z'))
    replies = [reply(call(), plain(), tool('note', {'key': KEY, 'text': 'one client'}), u=billed(consulted(STRONG, 700, 1200))), reply(text('Checked: one client.'), stop='end_turn')]
    inv = advised(store, replies, investigate_model_critical_advisor=STRONG)

    # when
    record = inv.run_once(NOW)

    # then
    run = store.investigation(record['id'])
    rows = store.ledger_since('0000')
    assert (run['advisorCalls'], run['advisorErrors'], run['outcome']) == (1, 0, 'done')
    assert {(r['model'], r.get('role'), r['use']) for r in rows} == {('claude-opus-5-5', None, 'card'), (STRONG, 'advisor', 'card')}
    assert SENTINEL not in everything(store, caplog)
    assert run['usd'] == pytest.approx(sum(r['usd']['total'] for r in rows), abs=1e-4)


def test_a_slack_answer_uses_the_slack_advisor_and_ignores_the_card_ones(store: Store):
    # given
    seed(store, anomaly(KEY), history=[])
    slack_action(store)
    inv, _ = slack_investigator(store, [reply(text('Because of one client.'), stop='end_turn')])
    inv.settings = dataclasses.replace(inv.settings, investigate_slack_model_advisor='claude-fable-5-2', investigate_model_advisor=STRONG)

    # when
    record = inv.run_once(NOW)

    # then
    assert record['outcome'] == 'done'
    assert {a['model'] for a in advisor_asks(inv)} == {'claude-fable-5-2'} and advisor.NUDGE in system_of(inv)


def test_the_costs_summary_shows_advisor_spend_apart_and_keeps_the_executor_as_the_run_model(store: Store):
    # given
    from skopax import costs
    base = {'source': 'pod', 'use': 'critical', 'day': DAY, 'provider': 'anthropic', 'effort': 'medium', 'runId': 'r1', 'actionId': None, 'keys': ['k'], 'by': 'pod', 'at': '2026-10-21T10:00:00Z'}
    rows = [base | {'model': EXECUTOR} | pricing.split(EXECUTOR, Usage(input=1000, output=200)),
            base | {'model': STRONG, 'role': 'advisor'} | pricing.split(STRONG, Usage(input=2000, output=1000))]

    # when
    summary = costs.summary(rows, {'r1': {'advisorCalls': 1, 'trigger': 'cards', 'outcome': 'done'}}, DAY, 1, 0.0, 10.0)

    # then
    run = summary['runs'][0]
    pod = next(s for s in summary['sources'] if s['source'] == 'pod')
    assert (run['model'], run['calls'], run['advisorModel'], run['advisorCalls']) == (EXECUTOR, 1, STRONG, 1)
    assert run['advisorUsd'] == pytest.approx(summary['total']['advisorUsd']) == pytest.approx(pod['advisor']['usd'], abs=1e-4) and run['advisorUsd'] > 0
    assert (pod['advisor']['calls'], pod['advisor']['runs'], pod['advisor']['models']) == (1, 1, [STRONG])
    assert next(s for s in summary['sources'] if s['source'] == 'slack')['advisor'] is None
    assert summary['total']['usd'] == pytest.approx(run['usd']['total']) and pod['models'] == {EXECUTOR: 1}


def test_missing_usage_iterations_are_estimated_ledgered_and_said_so(store: Store):
    # given
    whole = [reply(call(), plain(), stop='end_turn', u=usage(1000, 200))]
    nameless = [reply(call(), plain(), stop='end_turn', u=billed(NS(type='advisor_message', input_tokens=800, output_tokens=1500, cache_read_input_tokens=0, cache_creation_input_tokens=0)))]
    one_of_two = [reply(call(), plain(), call('srvtoolu_2'), plain('srvtoolu_2'), stop='end_turn', u=billed(consulted(PLAIN, 800, 1500)))]
    rates = pricing.PRICES[PLAIN]

    # when
    first, _ = go(store, whole)
    rows_first = [r for r in store.ledger_since('0000') if r.get('role') == 'advisor']
    second, _ = go(store, nameless, tag=dict(TAG, runId='run-2'))
    third, _ = go(store, one_of_two, tag=dict(TAG, runId='run-3'))

    # then
    expected = (1000 * rates['input_tokens'] + 2000 * rates['output_tokens']) / 1_000_000
    assert len(rows_first) == 1 and rows_first[0]['estimated'] is True and rows_first[0]['model'] == PLAIN and rows_first[0]['use'] == 'critical'
    assert rows_first[0]['tokens']['input'] == 1000 and rows_first[0]['tokens']['output'] == 2000 and rows_first[0]['usd']['total'] == pytest.approx(expected)
    assert first.error == msg('advisor.usage_missing') and first.advisor_calls == 1 and first.usd > expected
    assert second.error == msg('advisor.usage_missing') and second.advisor_calls == 1
    mine = [r for r in store.ledger_since('0000') if r.get('role') == 'advisor' and r['runId'] == 'run-3']
    assert sorted(r.get('estimated', False) for r in mine) == [False, True] and third.error == msg('advisor.usage_missing')
    assert store.spent(DAY) == pytest.approx(first.usd + second.usd + third.usd)


def test_a_complete_usage_report_is_not_estimated(store: Store):
    # when
    result, _ = go(store, [reply(call(), plain(), stop='end_turn', u=billed(consulted()))])

    # then
    assert result.error is None and all('estimated' not in r for r in store.ledger_since('0000'))


def test_an_advisor_run_never_asks_for_server_side_fallbacks(store: Store):
    # when
    _, on = go(store, [reply(text('done'), stop='end_turn')], caps=frozenset({'tools', 'thinking', 'fallbacks', 'advisor'}))
    _, off = go(store, [reply(text('done'), stop='end_turn')], plan=None, caps=frozenset({'tools', 'thinking', 'fallbacks', 'advisor'}))

    # then
    assert 'extra_body' not in on.requests[0] and FALLBACK_BETA not in on.requests[0]['betas']
    assert off.requests[0]['extra_body'] == {'fallbacks': 'default'} and off.requests[0]['betas'] == [FALLBACK_BETA]


class WebBox(Box):
    def definitions(self):
        return super().definitions() + [{'type': 'web_search_20250305', 'name': 'web_search'}]


def test_a_refused_web_tool_adds_to_an_advisor_error_and_the_log_uses_the_one_text(store: Store, caplog):
    # given
    caplog.set_level(logging.WARNING)
    client = FakeAnthropic([refused(), refused(), reply(text('Recap'), stop='end_turn')])
    runner = Runner(client, store, RunConfig(EXECUTOR, 'medium', 30, advisor=PLAN), lambda: 0.0, tag=TAG)

    # when
    result = runner.run(WebBox(), 'SYSTEM', 'BRIEF', DAY, lambda: None)

    # then
    assert result.outcome == 'done' and result.error.startswith('the API refused the advisor, the run went on without it')
    assert 'web tools refused by the API' in result.error and len(result.error) <= 300
    assert any(msg('advisor.refused', why='BadRequestError') in r.getMessage() or 'the API refused the advisor' in r.getMessage() for r in caplog.records)


def test_the_token_count_setting_shows_its_number_in_changes_and_the_activity_feed(store: Store):
    # given
    c = client_for(store)

    # when
    put(c, 'chat.model-advisor-tokens', '2048')
    history = c.get('/api/admin/history', headers=AUTH).json()
    feed = c.get('/api/activity', headers=AUTH).json()

    # then
    row = next(h for h in history if h.get('key') == 'chat.model-advisor-tokens')
    assert (row['old'], row['new']) == (None, '2048')
    assert any('(chat.model-advisor-tokens): 2000 \u2192 2048' in a['text'] for a in feed)


def test_the_cache_hit_share_ignores_advisor_rows_and_the_run_names_its_most_used_advisor():
    # given
    from skopax import costs
    base = {'source': 'pod', 'use': 'critical', 'day': DAY, 'provider': 'anthropic', 'effort': 'medium', 'runId': 'r1', 'actionId': None, 'keys': ['k'], 'by': 'pod', 'at': '2026-10-21T10:00:00Z'}
    rows = [base | {'model': EXECUTOR} | pricing.split(EXECUTOR, Usage(input=500, cache_read=500, output=100)),
            base | {'model': STRONG, 'role': 'advisor'} | pricing.split(STRONG, Usage(input=9000, output=1000)),
            base | {'model': PLAIN, 'role': 'advisor'} | pricing.split(PLAIN, Usage(input=9000, output=1000)),
            base | {'model': PLAIN, 'role': 'advisor'} | pricing.split(PLAIN, Usage(input=9000, output=1000))]

    # when
    summary = costs.summary(rows, {'r1': {}}, DAY, 1, 0.0, 10.0)

    # then
    pod = next(s for s in summary['sources'] if s['source'] == 'pod')
    assert pod['cacheHit'] == 0.5 and summary['total']['cacheHit'] == 0.5 and summary['runs'][0]['cacheHit'] == 0.5
    assert summary['runs'][0]['advisorModel'] == PLAIN


def test_a_run_at_the_real_daily_cap_stops_consulting(store: Store):
    # given
    seed(store, anomaly(KEY, sev='crit', firstSeen='2026-10-22T05:00:00Z'))
    replies = [reply(call(), plain(), tool('note', {'key': KEY, 'text': 'one client'}), u=billed(consulted(STRONG, 20000, 4000))), reply(text('never asked'), stop='end_turn')]
    inv = advised(store, replies, investigate_model_critical_advisor=STRONG, investigate_daily_usd=0.1)

    # when
    record = inv.run_once(NOW)

    # then
    assert record['outcome'] == 'capped' and len(inv.client.requests) == 1
    assert store.spent(clock.day(NOW)) > 0.1 and record['advisorCalls'] == 1


def test_a_use_on_another_provider_with_a_stored_advisor_sends_no_tool_and_stays_on_its_provider(store: Store):
    # given
    from tests.fake_openai import FakeOpenAI, reply as gpt_reply, text as gpt_text
    from skopax.investigator import Investigator
    from skopax.investigator.tools import Effects
    from tests.test_investigator_tools import sources
    seed(store, anomaly(KEY, firstSeen='2026-10-22T05:00:00Z'))
    dive(store, KEY)
    s = dataclasses.replace(settings(), anthropic_api_key='test-anthropic-key', openai_api_key='test-openai-key', investigate_mode='live', investigate_model='gpt-6.2-astra',
                            investigate_model_advisor=STRONG)
    claude = FakeAnthropic([])
    gpt = FakeOpenAI([gpt_reply(gpt_text('Checked: one client.'), model='gpt-6.2-astra')])
    inv = Investigator(store, sources(), Effects(lambda key, by: {}, lambda key, text, by, *_, notice=None: {}, lambda *a: None), s, client={'anthropic': claude, 'openai': gpt}, clock=lambda: NOW)

    # when
    record = inv.run_once(NOW)

    # then
    assert record['outcome'] == 'done' and claude.requests == [] and len(gpt.requests) == 1
    assert all('advisor' not in json.dumps(t, default=str) for t in gpt.requests[0].get('tools') or [])
    assert advisor.NUDGE not in json.dumps(gpt.requests[0], default=str)


def test_a_server_that_ignores_the_per_turn_cap_cannot_push_a_run_past_the_consultations_it_allows(store: Store):
    # given
    plan = advisor.Plan(PLAIN, 2, 2000)
    replies = [reply(call(f'srvtoolu_{n}'), plain(f'srvtoolu_{n}'), tool('promql', {}, f't{n}'), u=billed(consulted())) for n in range(6)]

    # when
    result, client = go(store, replies, plan=plan)

    # then
    assert result.outcome == 'capped' and result.error == 'the advisor was consulted more often than the run allows'
    assert len(client.requests) == 3 and result.advisor_calls == 3
    assert {r['tools'][-1]['max_uses'] for r in client.requests} == {2}


def test_a_run_that_stays_inside_its_consultations_is_not_capped(store: Store):
    # given
    plan = advisor.Plan(PLAIN, 2, 2000)
    replies = [reply(call('srvtoolu_1'), plain('srvtoolu_1'), tool('promql', {}, 't1'), u=billed(consulted())),
               reply(call('srvtoolu_2'), plain('srvtoolu_2'), tool('promql', {}, 't2'), u=billed(consulted())),
               reply(call('srvtoolu_3'), failed('max_uses_exceeded', 'srvtoolu_3'), tool('promql', {}, 't3')),
               reply(text('Recap'), stop='end_turn')]

    # when
    result, client = go(store, replies, plan=plan)

    # then
    assert (result.outcome, result.advisor_calls, result.advisor_errors, len(client.requests)) == ('done', 2, 1, 4)


def shaped(value):
    if isinstance(value, dict):
        return {k: shaped(v) for k, v in value.items()}
    if isinstance(value, list):
        return [sorted({type(v).__name__ for v in value})]
    return type(value).__name__


def test_the_costs_advisor_shapes_the_server_serves_are_the_ones_the_page_test_feeds_the_page(store: Store):
    # given
    from pathlib import Path
    from skopax import costs
    shared = json.loads((Path(__file__).parent / 'page' / 'skopax-costs-advisor.json').read_text(encoding='utf-8'))
    base = {'source': 'pod', 'use': 'critical', 'day': DAY, 'provider': 'anthropic', 'effort': 'medium', 'runId': 'r1', 'actionId': None, 'keys': ['k'], 'by': 'pod', 'at': '2026-10-21T10:00:00Z'}
    rows = [base | {'model': EXECUTOR} | pricing.split(EXECUTOR, Usage(input=1000, output=200)),
            base | {'model': STRONG, 'role': 'advisor'} | pricing.split(STRONG, Usage(input=2000, output=1000)),
            base | {'model': STRONG, 'role': 'advisor', 'estimated': True} | pricing.split(STRONG, Usage(input=1000, output=500))]

    # when
    summary = costs.summary(rows, {'r1': {'advisorCalls': 2, 'trigger': 'cards', 'outcome': 'done'}}, DAY, 1, 0.0, 10.0)

    # then
    pod = next(s for s in summary['sources'] if s['source'] == 'pod')
    run = summary['runs'][0]
    assert shaped(pod['advisor']) == shaped(shared['source'])
    assert shaped({k: run[k] for k in shared['run']}) == shaped(shared['run'])
    assert shaped({k: summary['total'][k] for k in shared['total']}) == shaped(shared['total'])
    assert 0 < pod['advisor']['estimatedUsd'] < pod['advisor']['usd']


def test_the_trace_text_never_carries_an_advisor_result(store: Store):
    # given
    blocks = [NS(type='text', text='visible'), call(), plain(), redacted(), {'type': 'advisor_tool_result', 'tool_use_id': 'x', 'content': {'type': 'advisor_result', 'text': SENTINEL}}]

    # when
    found = tracing.plain_text(blocks)

    # then
    assert SENTINEL not in found and 'visible' in found
