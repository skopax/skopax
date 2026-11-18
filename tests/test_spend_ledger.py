import datetime as dt
from types import SimpleNamespace

import pytest
from pymongo.errors import PyMongoError

from tests.conftest import anomaly, state
from tests.fake_anthropic import FakeAnthropic, reply, text, tool, usage, verdict
from tests.test_gate import gate
from tests.test_investigator import KEY, NOW, assess_action, investigator, note_and_recap, seed, seeded_report
from tests.test_investigator_runner import DAY, FakeBox, run as plain_run
from tests.test_investigator_slack import RECAP, slack_action, slack_investigator
from tests.test_page_ask import app_with_gate, ask
from tests.test_slackevents import card_with_post, intake, reply_event
from skopax import clock
from skopax.engine import iso
from skopax.investigator import pricing
from skopax.investigator.gate import GateDown
from skopax.investigator.runner import RunConfig, Runner
from skopax.store import INVESTIGATION_KEEP_DAYS, SPEND_KEEP_DAYS, Store

AT = dt.datetime(2026, 10, 24, 9, 30, tzinfo=dt.timezone.utc)


def row(**over) -> dict:
    return {'source': 'pod', 'day': '2026-10-24', 'model': 'claude-opus-5-5', 'effort': 'medium', 'runId': 'r1', 'actionId': None, 'keys': ['k1'], 'by': 'pod',
            **pricing.split('claude-opus-5-5', {'input_tokens': 1000, 'output_tokens': 100}), **over}


def test_split_prices_each_token_kind_and_sums_to_usd():
    # given
    used = SimpleNamespace(input_tokens=10_000, output_tokens=2_000, cache_read_input_tokens=100_000, cache_creation_input_tokens=30_000,
                           cache_creation=SimpleNamespace(ephemeral_1h_input_tokens=20_000))

    # when
    parts = pricing.split('claude-opus-5-5', used)

    # then
    assert parts['tokens'] == {'cacheRead': 100_000, 'input': 10_000, 'cacheWrite5m': 10_000, 'cacheWrite1h': 20_000, 'output': 2_000, 'webSearch': 0}
    assert [parts['usd'][p] for p in pricing.PARTS] == pytest.approx([0.02, 0.04, 0.05, 0.16, 0.04, 0.0])
    assert parts['usd']['total'] == pytest.approx(0.31)
    assert parts['usd']['total'] == pricing.usd('claude-opus-5-5', used)


def test_split_reads_a_dict_usage_and_an_unknown_model_cannot_be_split():
    # when
    parts = pricing.split('claude-sonnet-5-5', {'input_tokens': 1_000_000, 'cache_creation_input_tokens': 1_000_000, 'cache_creation': {'ephemeral_1h_input_tokens': 1_000_000}})

    # then
    assert parts['tokens'] == {'cacheRead': 0, 'input': 1_000_000, 'cacheWrite5m': 0, 'cacheWrite1h': 1_000_000, 'output': 0, 'webSearch': 0}
    assert parts['usd']['total'] == pytest.approx(6.0)
    with pytest.raises(pricing.UnknownModel):
        pricing.split('gpt-x', {'input_tokens': 1})


def test_the_parts_run_from_the_cheapest_token_to_the_dearest_on_every_model():
    # given
    million = {'input_tokens': 1_000_000, 'output_tokens': 1_000_000, 'cache_read_input_tokens': 1_000_000, 'cache_creation_input_tokens': 2_000_000,
               'cache_creation': {'ephemeral_1h_input_tokens': 1_000_000}}

    # then
    for model in pricing.PRICES:
        rates = [pricing.split(model, million)['usd'][p] for p in pricing.TOKEN_PARTS]
        assert rates == sorted(rates), model


def test_a_ledger_row_is_stamped_and_read_back_by_day(store: Store):
    # given
    store.add_ledger(row(), AT)
    store.add_ledger(row(day='2026-10-20', runId='r0'), AT - dt.timedelta(days=4))

    # when
    rows = store.ledger_since('2026-10-21')
    raw = store.ledger.find_one({'runId': 'r1'})

    # then
    assert [r['runId'] for r in rows] == ['r1']
    assert rows[0]['at'] == '2026-10-24T09:30:00Z' and '_id' not in rows[0] and 'atDt' not in rows[0]
    assert raw['atDt'] == AT and raw['usd']['total'] == pytest.approx(0.006)


def test_the_ledger_lives_in_spend_expires_with_the_run_records_and_is_indexed_by_day(store: Store):
    # when
    store.ensure_indexes()
    info = store.ledger.index_information()

    # then
    assert store.ledger.name == 'spend' and store.spend.name == 'investigator_spend'
    assert info['atDt_1']['expireAfterSeconds'] == max(SPEND_KEEP_DAYS, INVESTIGATION_KEEP_DAYS) * 86400 == 365 * 86400
    assert info['day_1_at_-1']['key'] == [('day', 1), ('at', -1)]


def test_run_costs_meta_reads_only_what_the_costs_tab_needs(store: Store):
    # given
    store.add_investigation({'id': 'r1', 'mode': 'live', 'trigger': 'ask', 'actionId': 'a1', 'keys': [], 'startedAt': '2026-10-24T09:30:00Z', 'model': 'claude-opus-5-5',
                             'effort': 'medium', 'outcome': 'running', 'ask': {'text': 'Why?', 'by': 'adam'}})
    store.update_investigation('r1', {'outcome': 'done', 'endedAt': '2026-10-24T09:32:05Z', 'toolCalls': [{'name': 'esql', 'input': '{}', 'ms': 5}], 'recap': 'long'})

    # when
    meta = store.run_costs_meta(['r1', 'nope'])

    # then
    assert list(meta) == ['r1']
    assert meta['r1']['ask'] == {'text': 'Why?'} and meta['r1']['toolCalls'] == [{'name': 'esql'}] and 'recap' not in meta['r1']


TAG = {'source': 'slack', 'runId': 'r9', 'actionId': 'a9', 'keys': ['k1'], 'by': 'adam.baker@example.com'}


def boom(row, now=None):
    raise PyMongoError('spend collection down')


def ledger_of(store: Store, run_id: str) -> list[dict]:
    return [r for r in store.ledger_since('2000-01-01') if r['runId'] == run_id]


def test_the_runner_writes_one_row_per_response_with_the_hour_and_five_minute_split(store: Store):
    # given
    hour = SimpleNamespace(input_tokens=500, output_tokens=300, cache_read_input_tokens=0, cache_creation_input_tokens=30_000,
                           cache_creation=SimpleNamespace(ephemeral_1h_input_tokens=20_000))
    client = FakeAnthropic([reply(tool('promql', {}), u=hour), reply(text('recap'), stop='end_turn', u=usage(200, 100, 30_000, 1_000))])
    runner = Runner(client, store, RunConfig('claude-sonnet-5-5', 'low', 10, cache_ttl='1h'), lambda: 0.0, tag=TAG)

    # when
    result = runner.run(FakeBox(), 'SYSTEM', 'BRIEF', DAY, lambda: None)
    first, second = sorted(store.ledger_since(DAY), key=lambda r: r['tokens']['cacheRead'])

    # then
    assert result.turns == 2
    assert {k: first[k] for k in TAG} == TAG and (first['day'], first['model'], first['effort']) == (DAY, 'claude-sonnet-5-5', 'low')
    assert first['tokens'] == {'cacheRead': 0, 'input': 500, 'cacheWrite5m': 10_000, 'cacheWrite1h': 20_000, 'output': 300, 'webSearch': 0}
    assert (first['usd']['cacheWrite1h'], first['usd']['cacheWrite5m']) == (pytest.approx(0.08), pytest.approx(0.025))
    assert (second['tokens']['cacheWrite5m'], second['tokens']['cacheWrite1h']) == (1_000, 0)
    assert first['usd']['total'] + second['usd']['total'] == pytest.approx(result.usd)
    assert store.spent(DAY) == pytest.approx(result.usd)
    assert 'pricedAs' not in first


def test_an_unpriced_served_model_gets_a_row_priced_at_the_rate_it_was_charged(store: Store):
    # given
    client = FakeAnthropic([reply(text('x'), stop='end_turn', model='claude-fable-9')])
    runner = Runner(client, store, RunConfig('claude-opus-5-5', 'medium', 5), lambda: 0.0, tag=TAG)

    # when
    result = runner.run(FakeBox(), 'SYSTEM', 'BRIEF', DAY, lambda: None)
    row = store.ledger_since(DAY)[0]

    # then
    assert (row['model'], row['pricedAs']) == ('claude-fable-9', 'claude-opus-5')
    assert row['usd']['total'] == pytest.approx(result.usd)


def test_a_failed_ledger_write_never_fails_the_run_or_charges_the_cap_twice(store: Store, monkeypatch, caplog):
    # given
    monkeypatch.setattr(store, 'add_ledger', boom)
    client = FakeAnthropic([reply(tool('promql', {})), reply(text('recap'), stop='end_turn')])
    runner = Runner(client, store, RunConfig('claude-opus-5-5', 'medium', 5), lambda: 0.0, tag=TAG)

    # when
    result = runner.run(FakeBox(), 'SYSTEM', 'BRIEF', DAY, lambda: None)

    # then
    assert (result.outcome, result.turns, result.error) == ('done', 2, None)
    assert store.spent(DAY) == pytest.approx(result.usd)
    assert caplog.text.count('spend ledger row for run r9 not written') == 2


def test_a_runner_without_a_tag_writes_no_row(store: Store):
    # when
    plain_run(store, [reply(text('x'), stop='end_turn')])

    # then
    assert store.ledger_since(DAY) == []


def test_a_card_run_is_pod_spend_by_the_pod_and_a_dive_is_pod_spend_by_its_operator(store: Store):
    # given
    seed(store, anomaly(KEY, firstSeen=iso(NOW - 600)))
    card_run = investigator(store, note_and_recap()).run_once(NOW)
    store.add_action({'type': 'dive', 'key': KEY, 'target': KEY, 'label': 'Investigate', 'requestedBy': 'oscar.green'})

    # when
    dive = investigator(store, note_and_recap()).run_once(NOW)

    # then
    rows = ledger_of(store, card_run['id'])
    assert len(rows) == 2 and {(r['source'], r['by'], r['actionId'], tuple(r['keys']), r['day']) for r in rows} == {('pod', 'pod', None, (KEY,), clock.day(NOW))}
    assert sum(r['usd']['total'] for r in rows) == pytest.approx(card_run['usd'], abs=1e-4)
    assert {(r['source'], r['by'], r['actionId']) for r in ledger_of(store, dive['id'])} == {('pod', 'oscar.green', dive['actionId'])}


def test_page_asks_slack_asks_and_assessments_are_their_own_sources(store: Store):
    # given
    seed(store, anomaly(KEY), history=[])
    store.add_action({'type': 'ask', 'source': 'page', 'key': 'ask', 'target': 'ask', 'label': 'q', 'text': 'Why?', 'cards': [], 'requestedBy': 'oscar.green'})
    page = investigator(store, [reply(text('One client.'), stop='end_turn')]).run_once(NOW)
    slack_action(store)
    slack = slack_investigator(store, [reply(text(RECAP), stop='end_turn')])[0].run_once(NOW)
    assess_action(store, seeded_report(store)['id'])

    # when
    assessed = investigator(store, [reply(text('ok'), stop='end_turn')]).run_once(NOW)

    # then
    assert [(r['source'], r['by'], r['model'], r['effort']) for r in ledger_of(store, page['id'])] == [('page', 'oscar.green', 'claude-opus-5-5', 'medium')]
    assert [(r['source'], r['by'], r['model'], r['effort'], r['keys']) for r in ledger_of(store, slack['id'])] == [('slack', 'adam.baker', 'claude-sonnet-5-5', 'medium', [KEY])]
    assert [(r['source'], r['by'], r['actionId']) for r in ledger_of(store, assessed['id'])] == [('assess', 'server', assessed['actionId'])]


def test_a_gate_call_writes_a_gate_row_for_its_caller(store: Store):
    # given
    g, _ = gate(store, [verdict()])

    # when
    v = g.check('why did it go up?', 'slack', 'adam.baker@example.com', [KEY])
    row = store.ledger_since('2000-01-01')[0]

    # then
    assert (row['source'], row['gateFor'], row['by'], row['keys'], row['runId'], row['actionId']) == ('gate', 'slack', 'adam.baker@example.com', [KEY], None, None)
    assert (row['day'], row['model'], row['effort']) == (clock.day(NOW), 'claude-haiku-5-5', 'low')
    assert row['tokens']['input'] == 1500 and row['usd']['total'] == pytest.approx(v.usd)


def test_a_refused_gate_call_keeps_its_row_and_a_call_without_an_answer_has_none(store: Store):
    # given
    refused, _ = gate(store, [reply(text(''), stop='refusal', u=usage(1500, 0))])
    down, _ = gate(store, [])

    # when
    with pytest.raises(GateDown):
        refused.check('x')
    with pytest.raises(GateDown):
        down.check('x')

    # then
    assert [(r['source'], r['gateFor'], r['by'], r['keys']) for r in store.ledger_since('2000-01-01')] == [('gate', 'page', None, [])]


def test_a_failed_gate_ledger_write_keeps_the_verdict_and_charges_the_cap_once(store: Store, monkeypatch, caplog):
    # given
    g, _ = gate(store, [verdict()])
    monkeypatch.setattr(store, 'add_ledger', boom)

    # when
    v = g.check('why?', 'page', 'oscar.green')

    # then
    assert v.on_topic and store.spent(clock.day(NOW)) == pytest.approx(v.usd)
    assert 'spend ledger row for a page gate call not written' in caplog.text


def test_the_page_ask_gate_row_names_the_caller_and_the_cards(store: Store):
    # given
    store.save(state(anomaly('k1')), None)
    client, _ = app_with_gate(store, [verdict()])

    # when
    resp = ask(client, 'Why did checkout-service spike?', ['k1'])

    # then
    assert resp.status_code == 201
    assert [(r['source'], r['gateFor'], r['by'], r['keys']) for r in store.ledger_since('2000-01-01')] == [('gate', 'page', 'token:bootstrap', ['k1'])]


def test_the_slack_gate_row_names_the_operator_login_and_the_card(store: Store):
    # given
    card_with_post(store)
    box, _ = intake(store, gate_replies=[verdict()])

    # when
    box.handle(reply_event())

    # then
    assert [(r['source'], r['gateFor'], r['by'], r['keys']) for r in store.ledger_since('2000-01-01')] == [('gate', 'slack', 'adam.baker@example.com', [KEY])]
