import asyncio
import logging

import pytest
from pymongo.errors import OperationFailure

from skopax import backfill, engine
from skopax.assign import Assigner
from skopax.mcpauth import Unavailable
from skopax.mcpserver import Endpoint
from skopax.notices import Notices
from skopax.scheduler import Job, Scheduler
from skopax.slack import Slack
from skopax.statenotes import StateNotes
from skopax.users import LastAdmin, Users
from tests.conftest import settings
from tests.fake_slack import FakeSlackApi
from tests.test_slack import FakeSlack

SENTINEL = 'zq9-sentinel-secret-4471'
SLOT = 1_792_224_000.0


def refused(*a, **kw):
    raise OperationFailure(SENTINEL, code=13)


def plain(*a, **kw):
    raise RuntimeError(SENTINEL)


def clean(caplog, *needles):
    assert SENTINEL not in caplog.text
    for needle in needles:
        assert needle in caplog.text


def test_the_last_admin_restore_failure_logs_type_and_code_only(store, caplog, monkeypatch):
    # given
    users = Users(store.db)
    users.create('boss@example.com', 'Boss', 'admin')
    monkeypatch.setattr(users, 'active_admins', lambda: 0)
    monkeypatch.setattr(users.col, 'update_one', refused)

    # when
    with caplog.at_level(logging.ERROR):
        with pytest.raises(LastAdmin):
            users.guarded_update('boss@example.com', {'role': 'viewer'})

    # then
    clean(caplog, 'OperationFailure code 13')


def test_the_notices_logs_carry_type_and_code_only(store, caplog, monkeypatch):
    # given
    notices = Notices(store)
    monkeypatch.setattr(notices, '_cards', refused)

    # when
    with caplog.at_level(logging.WARNING):
        watched = notices.watch({'key': 'k1', 'type': 'mute'})
        applied = notices.applied({'key': 'k1'}, {'k1': {}})
        triaged = notices.triaged('k1', {})

    # then
    assert (watched, applied, triaged) == (None, 0, 0)
    assert caplog.text.count('OperationFailure code 13') == 3
    clean(caplog)


def test_the_card_number_log_carries_the_type_only(store, caplog, monkeypatch):
    # given
    monkeypatch.setattr(store, 'card_numbers', refused)

    # when
    with caplog.at_level(logging.WARNING):
        engine.number_cards(store, [{'key': 'k1', 'sev': 'warn'}])

    # then
    clean(caplog, 'OperationFailure code 13')


def test_the_seen_before_marks_log_carries_the_type_and_code_only(store, caplog, monkeypatch):
    # given
    store.save_state({'anomalies': [], 'initialized': True}, None)
    monkeypatch.setattr(engine.history, 'annotate', refused)

    # when
    with caplog.at_level(logging.WARNING):
        engine.apply(store, [engine.PassResult('pulse', True, 'ok', [], {}, ('pulse',))], SLOT)

    # then
    assert 'seen-before and recurring marks skipped this pass: OperationFailure code 13' in caplog.text
    clean(caplog, 'OperationFailure code 13')


def slack_on():
    return Slack('xoxb-test', '#ops', FakeSlack(), FakeSlackApi({}))


def test_the_state_note_logs_carry_type_and_code_only(store, caplog, monkeypatch):
    # given
    notes = StateNotes(store, slack_on())
    monkeypatch.setattr(notes, '_cards', refused)
    monkeypatch.setattr(store, 'load', refused)

    # when
    with caplog.at_level(logging.WARNING):
        watched = notes.watch({'key': 'k1', 'type': 'mute'})
        notes.applied({'key': 'k1', 'type': 'mute'}, {})
        card = notes._card('k1')

    # then
    assert watched is None and card is None
    assert caplog.text.count('OperationFailure code 13') == 3
    clean(caplog)


def test_the_assignee_notification_log_carries_type_and_code_only(store, caplog):
    # given
    class Broken:
        def assigned(self, *a, **kw):
            raise OperationFailure(SENTINEL, code=13)

    assigner = Assigner(store, slack_on(), settings(), 'https://skopax.example.com', notices=Broken())

    # when
    with caplog.at_level(logging.WARNING):
        assigner.notify({'key': 'k1'}, 'Alice', 'alice@example.com', 'bob@example.com')

    # then
    clean(caplog, 'OperationFailure code 13')


def test_the_backfill_logs_carry_the_type_only(store, caplog, monkeypatch):
    # given
    monkeypatch.setattr(backfill, 'row_of', lambda *a, **kw: (_ for _ in ()).throw(KeyError(SENTINEL)))

    # when
    with caplog.at_level(logging.WARNING):
        skipped = backfill.safe_row(store, {'_id': 'r1'})
        monkeypatch.setattr(store, 'backfill_candidates', plain)
        written = backfill.run(store)

    # then
    assert skipped is None and written == 0
    clean(caplog, 'KeyError', 'RuntimeError')


def test_the_mcp_database_log_carries_the_type_only(caplog):
    # given
    class Auth:
        enabled = True

        def challenge(self):
            return {}

        def user(self, token):
            raise Unavailable(SENTINEL)

    sent = []

    async def send(message):
        sent.append(message)

    async def receive():
        return {'type': 'http.disconnect'}

    scope = {'type': 'http', 'method': 'POST', 'path': '/mcp', 'headers': [(b'authorization', b'Bearer tok')]}

    # when
    with caplog.at_level(logging.WARNING):
        asyncio.run(Endpoint(Auth(), None)(scope, receive, send))

    # then
    assert sent[0]['status'] == 503
    clean(caplog, 'Unavailable')


def scheduler_for(store, job=None):
    return Scheduler(store, [job] if job else [], poll=0.01, clock=lambda: SLOT)


def test_a_run_that_cannot_be_claimed_logs_type_and_code_only(store, caplog, monkeypatch):
    # given
    s = scheduler_for(store, Job('j', 300, 0, lambda now: None))
    monkeypatch.setattr(store, 'claim_run', refused)

    # when
    with caplog.at_level(logging.WARNING):
        s.run_due(join=True)

    # then
    clean(caplog, 'j run not claimed: OperationFailure code 13')


def test_a_failing_run_logs_the_type_only(store, caplog):
    # given
    s = scheduler_for(store, Job('j', 300, 0, plain))

    # when
    with caplog.at_level(logging.WARNING):
        s.run_due(join=True)

    # then
    clean(caplog, 'j run failed: RuntimeError')


def test_a_run_that_cannot_be_marked_done_logs_type_and_code_only(store, caplog, monkeypatch):
    # given
    s = scheduler_for(store, Job('j', 300, 0, lambda now: None))
    monkeypatch.setattr(store, 'finish_run', refused)

    # when
    with caplog.at_level(logging.WARNING):
        s.run_due(join=True)

    # then
    clean(caplog, 'j run not marked done: OperationFailure code 13')


def test_a_failing_beat_and_a_failing_pass_log_the_type_only(store, caplog, monkeypatch):
    # given
    s = scheduler_for(store)
    monkeypatch.setattr(store, 'beat_run', plain)
    monkeypatch.setattr(s, 'run_due', plain)

    async def scenario():
        task = asyncio.create_task(s.loop())
        await asyncio.sleep(0.15)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    # when
    with caplog.at_level(logging.WARNING):
        asyncio.run(scenario())

    # then
    clean(caplog, 'scheduler beat failed: RuntimeError', 'scheduler pass failed: RuntimeError')
