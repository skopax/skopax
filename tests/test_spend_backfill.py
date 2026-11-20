import datetime as dt
import logging

import pytest
from fastapi.testclient import TestClient

from tests.conftest import settings
from skopax import backfill, costs
from skopax.app import create_app
from skopax.investigator import pricing
from skopax.store import Store, iso, utc_now

USAGE = {'input_tokens': 1000, 'output_tokens': 200, 'cache_read_input_tokens': 50_000, 'cache_creation_input_tokens': 10_000}
RUN_DAY = (utc_now() - dt.timedelta(days=2)).replace(hour=21, minute=30, second=0, microsecond=0)
STARTED = iso(RUN_DAY)
ENDED = iso(RUN_DAY + dt.timedelta(minutes=12, seconds=10))


def record(run_id: str = 'r1', **over) -> dict:
    return {'id': run_id, 'trigger': 'cards', 'mode': 'live', 'outcome': 'done', 'startedAt': STARTED, 'endedAt': ENDED, 'turns': 7, 'models': ['claude-opus-5-5'] * 6 + ['claude-sonnet-5-5'],
            'model': 'claude-opus-5-5', 'effort': 'medium', 'usage': USAGE, 'usd': 1.5, 'keys': ['k1', 'k2'], 'toolCalls': [], **over}


def test_a_pre_ledger_run_gets_one_backfill_row_that_sums_to_its_recorded_usd(store: Store):
    # given
    store.add_investigation(record())

    # when
    written = backfill.run(store)

    # then
    rows = list(store.ledger.find({}))
    assert written == 1 and len(rows) == 1
    r = rows[0]
    assert r['_id'] == 'backfill-r1' and r['backfill'] is True and r['calls'] == 7
    assert r['runId'] == 'r1' and r['actionId'] is None and r['keys'] == ['k1', 'k2'] and r['by'] == pricing.POD_BY
    assert r['source'] == 'pod' and r['model'] == 'claude-opus-5-5' and r['effort'] == 'medium' and 'pricedAs' not in r
    assert r['at'] == ENDED and r['atDt'] == RUN_DAY + dt.timedelta(minutes=12, seconds=10)
    assert r['day'] == RUN_DAY.date().isoformat()
    assert r['tokens'] == {'cacheRead': 50_000, 'input': 1000, 'cacheWrite5m': 10_000, 'cacheWrite1h': 0, 'output': 200, 'webSearch': 0}
    assert r['usd']['total'] == pytest.approx(1.5)
    assert sum(r['usd'][p] for p in pricing.PARTS) == pytest.approx(1.5)
    plain = pricing.split('claude-opus-5-5', USAGE)['usd']
    assert r['usd']['cacheRead'] / r['usd']['input'] == pytest.approx(plain['cacheRead'] / plain['input'])


def test_a_run_that_already_has_ledger_rows_gets_none(store: Store):
    # given
    store.add_investigation(record())
    store.add_ledger({'source': 'pod', 'day': '2026-10-21', 'runId': 'r1', 'model': 'claude-opus-5-5', 'tokens': {}, 'usd': {'total': 0.1}})

    # when
    written = backfill.run(store)

    # then
    assert written == 0 and store.ledger.count_documents({}) == 1


def test_a_second_backfill_writes_nothing(store: Store):
    # given
    store.add_investigation(record())
    backfill.run(store)

    # when
    again = backfill.run(store)

    # then
    assert again == 0 and store.ledger.count_documents({}) == 1


def test_two_pods_starting_together_cannot_double_write(store: Store):
    # given
    store.add_investigation(record())
    store.ledger.insert_one({'_id': 'backfill-r1', 'runId': 'r1'})
    store.add_investigation(record('r2'))

    # when
    written = backfill.run(store)

    # then
    assert written == 1
    assert sorted(d['_id'] for d in store.ledger.find({})) == ['backfill-r1', 'backfill-r2']


def test_a_run_without_cost_is_skipped(store: Store):
    # given
    store.add_investigation(record(usd=0))
    store.add_investigation(record('r2', usd=None))
    store.add_investigation({k: v for k, v in record('r3').items() if k != 'usd'})

    # when
    written = backfill.run(store)

    # then
    assert written == 0 and store.ledger.count_documents({}) == 0


@pytest.mark.parametrize('trigger,source', [('cards', 'pod'), ('action', 'pod'), ('ask', 'page'), ('slack', 'slack'), ('assess', 'assess')])
def test_the_trigger_maps_to_the_source(store: Store, trigger: str, source: str):
    # given
    store.add_investigation(record(trigger=trigger))

    # when
    backfill.run(store)

    # then
    assert store.ledger.find_one({'runId': 'r1'})['source'] == source


def test_an_unknown_trigger_is_skipped(store: Store):
    # given
    store.add_investigation(record(trigger='mystery'))
    store.add_investigation(record('r2', trigger=None))

    # when
    written = backfill.run(store)

    # then
    assert written == 0


def test_by_comes_from_the_ask_first(store: Store):
    # given
    act = store.add_action({'type': 'ask', 'key': 'ask', 'target': 'page', 'text': 'q', 'cards': [], 'requestedBy': 'someone.else'})
    store.add_investigation(record(trigger='ask', actionId=act['id'], ask={'text': 'why', 'by': 'adam.baker'}))

    # when
    backfill.run(store)

    # then
    row = store.ledger.find_one({'runId': 'r1'})
    assert row['by'] == 'adam.baker' and row['actionId'] == act['id']


def test_by_falls_back_to_the_action_requester(store: Store):
    # given
    act = store.add_action({'type': 'investigate', 'key': 'k1', 'target': 'k1', 'requestedBy': 'oscar.green'})
    store.add_investigation(record(trigger='action', actionId=act['id']))

    # when
    backfill.run(store)

    # then
    assert store.ledger.find_one({'runId': 'r1'})['by'] == 'oscar.green'


def test_by_falls_back_to_pod_when_the_action_is_gone(store: Store):
    # given
    store.add_investigation(record(trigger='action', actionId='gone'))

    # when
    backfill.run(store)

    # then
    assert store.ledger.find_one({'runId': 'r1'})['by'] == pricing.POD_BY


def test_an_unknown_model_is_priced_at_the_highest_row_and_marked(store: Store):
    # given
    store.add_investigation(record(model='claude-future-9', models=['claude-future-9'], turns=None))

    # when
    backfill.run(store)

    # then
    row = store.ledger.find_one({'runId': 'r1'})
    assert row['model'] == 'claude-future-9' and row['calls'] == 1
    assert row['pricedAs'] == max(pricing.PRICES, key=lambda m: pricing.usd(m, USAGE))
    assert row['usd']['total'] == pytest.approx(1.5)


def test_model_falls_back_to_the_most_common_served_model_and_calls_to_the_models(store: Store):
    # given
    old = record(models=['claude-sonnet-5-5', 'claude-opus-5-5', 'claude-sonnet-5-5'])
    old.pop('model')
    old.pop('effort')
    old.pop('turns')
    store.add_investigation(old)

    # when
    backfill.run(store)

    # then
    row = store.ledger.find_one({'runId': 'r1'})
    assert row['model'] == 'claude-sonnet-5-5' and row['effort'] is None and row['calls'] == 3


def test_a_run_with_no_priced_tokens_puts_the_whole_usd_in_input(store: Store):
    # given
    store.add_investigation(record(usage={}))

    # when
    backfill.run(store)

    # then
    row = store.ledger.find_one({'runId': 'r1'})
    assert row['usd']['input'] == pytest.approx(1.5) and row['usd']['total'] == pytest.approx(1.5)


def test_a_failure_inside_the_backfill_is_one_warning_and_nothing_raised(store: Store, monkeypatch, caplog):
    # given
    store.add_investigation(record())
    monkeypatch.setattr(store, 'backfill_candidates', lambda: (_ for _ in ()).throw(RuntimeError('boom')))

    # when
    with caplog.at_level(logging.WARNING):
        written = backfill.run(store)

    # then
    assert written == 0
    assert [r.levelname for r in caplog.records if 'backfill' in r.getMessage()] == ['WARNING']


def test_the_info_line_is_logged_only_when_rows_were_written(store: Store, caplog):
    # given
    store.add_investigation(record())

    # when
    with caplog.at_level(logging.INFO):
        backfill.run(store)
        backfill.run(store)

    # then
    assert [r.getMessage() for r in caplog.records if 'backfilled' in r.getMessage()] == ['spend ledger backfilled 1 runs']


def test_startup_backfills_and_survives_a_failing_backfill(store: Store, monkeypatch):
    # given
    store.add_investigation(record())

    # when
    with TestClient(create_app(settings(), store)):
        pass

    # then
    assert store.ledger.count_documents({'backfill': True}) == 1

    # given
    monkeypatch.setattr(store, 'backfill_candidates', lambda: (_ for _ in ()).throw(RuntimeError('boom')))

    # when
    with TestClient(create_app(settings(), store)):
        pass


def test_a_backfill_row_counts_its_calls_in_the_costs_aggregation():
    # given
    rows = [{'source': 'pod', 'day': '2026-10-24', 'at': '2026-10-24T06:00:00Z', 'model': 'claude-opus-5-5', 'runId': 'r1', 'keys': ['k1'], 'by': 'oscar.green', 'calls': 7,
             'tokens': dict.fromkeys(pricing.PARTS, 0), 'usd': dict.fromkeys(pricing.PARTS, 0.0) | {'total': 1.0}},
            {'source': 'pod', 'day': '2026-10-24', 'at': '2026-10-24T06:05:00Z', 'model': 'claude-opus-5-5', 'runId': 'r2', 'keys': ['k1'], 'by': 'oscar.green',
             'tokens': dict.fromkeys(pricing.PARTS, 0), 'usd': dict.fromkeys(pricing.PARTS, 0.0) | {'total': 1.0}}]

    # when
    out = costs.summary(rows, {}, '2026-10-24', 7, 0.0, 20.0)

    # then
    assert out['total']['calls'] == 8
    assert out['sources'][0]['calls'] == 8 and out['sources'][0]['models'] == {'claude-opus-5-5': 8}
    assert out['models'][0]['calls'] == 8
    assert {r['id']: r['calls'] for r in out['runs']} == {'r1': 7, 'r2': 1}
    assert out['topAskers'][0]['calls'] == 8
