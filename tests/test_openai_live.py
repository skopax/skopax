import os

import pytest

from tests.test_investigator_runner import FakeBox
from skopax.ai import THINK_AUTO, THINK_OFF, wrap
from skopax.ai.openai_ai import reasoning_effort
from skopax.errkind import kind as error_kind
from skopax.investigator import pricing
from skopax.investigator.runner import RunConfig, Runner
from skopax.investigator.web import SEARCH_TYPE
from skopax.store import Store

ENV = 'SKOPAX_TEST_OPENAI_KEY'
DAY = '2026-11-20'
CHEAP = 'gpt-6-luna'
pytestmark = pytest.mark.skipif(not os.environ.get(ENV), reason=f'live OpenAI test: set {ENV} to run it')
LOOKUP = {'name': 'lookup', 'description': 'Returns the status of one service by name.',
          'input_schema': {'type': 'object', 'properties': {'service': {'type': 'string'}}, 'required': ['service'], 'additionalProperties': False}}
PICK = {'name': 'pick', 'description': 'Records the verdict for one service.', 'strict': True,
        'input_schema': {'type': 'object', 'properties': {'service': {'type': 'string', 'maxLength': 40}, 'state': {'type': 'string', 'enum': ['ok', 'down']}},
                         'required': ['service', 'state'], 'additionalProperties': False}}
BOTH = ('Call lookup for checkout-service and for payments-api, both in this one turn, before you answer. '
        'Then answer with one line naming both services and their status.')


class LiveFailed(Exception):
    pass


class Recorder:
    provider = 'openai'

    def __init__(self):
        import openai
        self.raw = openai.OpenAI(api_key=os.environ[ENV], timeout=120, max_retries=1)
        self.responses = self
        self.seen = []

    def create(self, **kw):
        import openai
        try:
            resp = self.raw.responses.create(**kw)
        except openai.APIError as e:
            raise LiveFailed(error_kind(e)) from None
        self.seen.append(resp)
        return resp


def client() -> Recorder:
    return Recorder()


class Box(FakeBox):
    def __init__(self, tools):
        super().__init__({'lookup': ('ok, 12 pods ready', False), 'pick': ('recorded', False)})
        self.tools = tools

    def definitions(self):
        return list(self.tools)


def test_a_live_card_run_makes_two_calls_in_one_turn_and_answers_after_the_replay(store: Store):
    # given
    box = Box([LOOKUP])
    live = client()
    runner = Runner(live, store, RunConfig(CHEAP, 'low', 6), tag={'source': 'pod', 'runId': 'live-1', 'actionId': None, 'keys': [], 'by': 'pod'})

    # when
    result = runner.run(box, 'You check services with the tools given.', BOTH, DAY, lambda: None)

    # then
    assert result.outcome == 'done' and result.turns >= 2 and result.recap
    assert [i.type for i in live.seen[0].output].count('function_call') == 2, 'both calls came in the first response'
    assert sorted(a['service'] for name, a in box.seen if name == 'lookup') == ['checkout-service', 'payments-api']
    rows = list(store.ledger.find({'runId': 'live-1'}))
    assert len(rows) == result.turns and all(r['provider'] == 'openai' and r['usd']['total'] > 0 for r in rows)


def test_a_live_turn_cut_at_max_output_tokens_is_replayed_and_the_run_goes_on():
    # given
    ai = wrap(client())
    history = [{'role': 'user', 'content': BOTH}]

    # when
    cut = ai.turn(CHEAP, 'low', 'You check services with the tools given.', [LOOKUP], history, 16)
    history += [{'role': 'assistant', 'content': cut.native},
                {'role': 'user', 'content': [{'type': 'tool_result', 'tool_use_id': c.id, 'content': 'cut off; send it again shorter', 'is_error': True} for c in cut.calls]
                 or 'go on'}]
    after = ai.turn(CHEAP, 'low', 'You check services with the tools given.', [LOOKUP], history, 2000)

    # then
    assert cut.stop == 'max_tokens'
    assert after.stop in ('tool_use', 'end_turn')


def test_a_live_turn_with_web_search_counts_its_searches():
    # given
    ai = wrap(client())
    search = {'type': SEARCH_TYPE, 'name': 'web_search', 'max_uses': 2, 'allowed_domains': ['kafka.apache.org']}

    # when
    turn = ai.turn(CHEAP, 'low', 'Answer from the Kafka documentation.', [search], [{'role': 'user', 'content': 'Search the web: what is the default of log.retention.hours in Kafka?'}], 4000)

    # then
    assert turn.stop == 'end_turn' and turn.text
    assert turn.usage.web_searches >= 1 and pricing.split(CHEAP, turn.usage)['usd']['webSearch'] > 0


@pytest.mark.parametrize('model', list(pricing.OPENAI_PRICES))
def test_every_offered_model_takes_its_lowest_and_highest_effort(model: str):
    # given
    live = client()
    ai = wrap(live)
    ask = [{'role': 'user', 'content': 'Say ok.'}]

    # when
    off = ai.once(model, 'Answer in one word.', ask, 50, 'low', THINK_OFF)
    most = ai.once(model, 'Answer in one word.', ask, 50, 'max', THINK_AUTO)

    # then
    assert off.stop in ('end_turn', 'max_tokens') and most.stop in ('end_turn', 'max_tokens'), 'both efforts are accepted'
    assert [r.reasoning.effort for r in live.seen] == [reasoning_effort(model, 'low', THINK_OFF), 'max']


def test_a_live_strict_tool_with_max_length_and_enum_is_accepted():
    # given
    ai = wrap(client())

    # when
    turn = ai.turn(CHEAP, 'low', 'Record verdicts with pick.', [PICK], [{'role': 'user', 'content': 'checkout-service is down. Record it with pick.'}], 2000)

    # then
    assert turn.stop == 'tool_use'
    assert [(c.name, c.input['state'], c.broken) for c in turn.calls] == [('pick', 'down', False)]


def test_live_usage_sums_to_the_reported_input_tokens():
    # given
    live = client()
    long = 'Facts: ' + ' '.join(f'service-{i} is ok.' for i in range(1500))
    ai = wrap(live)

    # when
    turns = [ai.once(CHEAP, long, [{'role': 'user', 'content': 'Say ok.'}], 50, 'low', THINK_OFF) for _ in range(2)]

    # then
    for turn, usage in zip(turns, [r.usage for r in live.seen]):
        details = usage.input_tokens_details
        assert turn.usage.input + turn.usage.cache_read + turn.usage.cache_write + turn.usage.cache_write_long == usage.input_tokens
        assert turn.usage.cache_read == details.cached_tokens and turn.usage.output == usage.output_tokens
