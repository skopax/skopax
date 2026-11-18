import asyncio
import json
import logging
from types import SimpleNamespace

import pytest

from skopax.assign import Assigner
from skopax.auth import AuthError
from skopax.autosend import AutoSend
from skopax.enrich import Enricher, SourceDown
from skopax.mcpauth import Forbidden
from skopax.mcpserver import Endpoint
from skopax.operators import OperatorUnknown, Operators
from skopax.push import Pusher
from skopax.selfreview import gather
from skopax.slack import Slack, SlackDown, SlackError
from skopax.slackevents import Intake
from skopax.statenotes import StateNotes
from tests import test_autosend, test_clear_notes, test_reminders
from tests.conftest import settings
from tests.fake_slack import FakeSlackApi
from tests.test_selfreview import FakeLogs
from tests.test_slack import FakeSlack, client_with
from tests.test_state_notes import KEY, Transport, card, seed

SENTINEL = 'zq9-sentinel-secret-4471'
NOW = 1_792_224_000.0


def clean(caplog, *needles):
    assert SENTINEL not in caplog.text
    for needle in needles:
        assert needle in caplog.text


def raising(error):
    def run(*a, **kw):
        raise error

    return run


def slack_on(transport=None):
    return Slack('xoxb-test', '#ops', transport or Transport(), FakeSlackApi())


def test_the_status_note_slack_logs_carry_the_kind_only(store, caplog, monkeypatch):
    # given
    seed(store)
    notes = StateNotes(store, slack_on(Transport(fail=SlackDown(SENTINEL))))
    monkeypatch.setattr(store, 'load', raising(RuntimeError(SENTINEL)))

    # when
    with caplog.at_level(logging.WARNING):
        shared = notes._shared(card(KEY))
    monkeypatch.undo()
    with caplog.at_level(logging.WARNING):
        notes._send(card(KEY), 'progress', 'text', 'alice')
        working = StateNotes(store, slack_on())
        monkeypatch.setattr(store, 'slack_state_noted', raising(RuntimeError(SENTINEL)))
        working._send(card(KEY), 'progress', 'text', 'alice')

    # then
    assert shared is False
    clean(caplog, 'thread could not be checked: RuntimeError', 'not sent: SlackDown', 'not recorded on the card: RuntimeError')


def test_the_assignee_slack_logs_carry_the_kind_only(store, caplog):
    # given
    api = FakeSlackApi({'U1': 'alice@example.com'})
    slack = Slack('xoxb-test', '#ops', FakeSlack(), api)
    slack.post = raising(SlackError(SENTINEL))

    class Broken:
        def assigned(self, *a, **kw):
            raise RuntimeError(SENTINEL)

    assigner = Assigner(store, slack, settings(), 'https://skopax.example.com', notes=Broken())
    assigner.settings = settings()

    # when
    with caplog.at_level(logging.WARNING):
        assigner.tell({'key': 'k1', 'num': 1, 'svc': 's', 'label': 'l'}, 'Alice', 'alice@example.com', 'bob@example.com')
        assigner.noted('k1', 'Alice', 'bob@example.com')

    # then
    clean(caplog, 'assignee message for k1 not sent: SlackError', 'not sent to its thread: RuntimeError')


def test_the_auto_send_logs_carry_the_kind_only(store, caplog, monkeypatch):
    # given
    test_autosend.seed(store)
    transport = FakeSlack(SENTINEL)

    # when

    with client_with(store, transport) as client:
        test_autosend.drafted(client)
        test_autosend.due_now(store)
        with caplog.at_level(logging.WARNING):
            test_autosend.job(client).run_once()
    box = AutoSend(store, lambda k: {}, lambda k: {}, interval=0.01)
    monkeypatch.setattr(store, 'due_anomaly_auto_sends', raising(RuntimeError(SENTINEL)))

    async def scenario():
        task = asyncio.create_task(box.loop())
        await asyncio.sleep(0.1)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    with caplog.at_level(logging.WARNING):
        asyncio.run(scenario())

    # then
    text = ' '.join(r.getMessage() for r in caplog.records if r.name == 'skopax.autosend')
    assert SENTINEL not in text and 'failed: SlackError' in text and 'auto-send pass failed: RuntimeError' in text


def test_the_clear_note_log_carries_the_kind_only(store, caplog):
    # given
    test_clear_notes.seed(store, card(KEY, status='cleared', clearedAt=test_clear_notes.CLEARED))
    box = test_clear_notes.notes(store, Transport(fail=SlackDown(SENTINEL)))

    # when
    with caplog.at_level(logging.WARNING):
        box.run_once(test_clear_notes.T + 16 * test_clear_notes.M)

    # then
    clean(caplog, 'not sent: SlackDown')


def test_the_reminder_log_carries_the_kind_only(store, caplog):
    # given
    seed(store, card(KEY))
    box = test_reminders.reminders(store, Transport(fail=SlackDown(SENTINEL)))

    # when
    with caplog.at_level(logging.WARNING):
        box.run_once(test_reminders.POSTED + 25 * test_reminders.H)

    # then
    clean(caplog, 'not sent: SlackDown')


def test_the_push_log_carries_the_kind_only(caplog):
    # given
    slack = slack_on()
    slack.post = raising(SlackError(SENTINEL))

    # when
    with caplog.at_level(logging.WARNING):
        Pusher(slack, 'U1', 'https://skopax.example.com').text('hello')

    # then
    clean(caplog, 'failed: SlackError')


def test_the_operator_logs_carry_the_kind_only(caplog):
    # given
    slack = slack_on()
    slack.user_email = raising(SlackError(SENTINEL))
    ops = Operators(raising(RuntimeError(SENTINEL)), slack, settings())

    # when
    with caplog.at_level(logging.WARNING):
        ops.email_of('U1')
        with pytest.raises(OperatorUnknown):
            ops.access_of('alice@example.com')

    # then
    clean(caplog, 'users.info for U1 failed: SlackError', 'role lookup failed: RuntimeError')


def test_the_enrichment_logs_carry_the_kind_only(store, caplog, monkeypatch):
    # given
    enricher = Enricher(store, SimpleNamespace(enabled=True), SimpleNamespace(enabled=True, versions=raising(SourceDown(SENTINEL))), ('OPS',))
    monkeypatch.setattr(enricher, 'needs_gitlab', lambda d: True)
    monkeypatch.setattr(enricher, '_git', raising(SourceDown(SENTINEL)))
    monkeypatch.setattr(enricher, 'needs_jira', lambda d: True)
    monkeypatch.setattr(store, 'deploys_by_keys', lambda keys: [{'ns': 'n', 'svc': 's', 'toImage': 'i', 'tickets': ['OPS-1']}])

    # when
    with caplog.at_level(logging.WARNING):
        enricher.run([{'ns': 'n', 'svc': 's', 'toImage': 'i'}])

    # then
    clean(caplog, 'GitLab stopped: SourceDown', 'Jira failed: SourceDown')


def test_the_self_review_commit_log_carries_the_kind_only(store, caplog):
    # given
    when = 1_792_224_000.0

    # when
    with caplog.at_level(logging.WARNING):
        gather(store, {}, FakeLogs(), when, commits=raising(RuntimeError(SENTINEL)))

    # then
    clean(caplog, 'own commits not read, using the deploy rows: RuntimeError')


def run_endpoint(auth):
    sent = []

    async def send(message):
        sent.append(message)

    async def receive():
        return {'type': 'http.disconnect'}

    scope = {'type': 'http', 'method': 'POST', 'path': '/mcp', 'headers': [(b'authorization', b'Bearer tok')]}
    asyncio.run(Endpoint(auth, None)(scope, receive, send))
    return sent


def test_the_mcp_token_logs_carry_the_kind_only(caplog):
    # given
    class Auth:
        enabled = True

        def __init__(self, error):
            self.error = error

        def challenge(self, *a):
            return 'Bearer'

        def user(self, token):
            raise self.error

    # when
    with caplog.at_level(logging.INFO):
        first = run_endpoint(Auth(Forbidden(SENTINEL, 'no role')))
        second = run_endpoint(Auth(AuthError(SENTINEL)))

    # then
    assert (first[0]['status'], second[0]['status']) == (403, 401)
    clean(caplog, 'token refused: Forbidden', 'token rejected: AuthError')


def test_a_refused_mcp_token_is_answered_with_the_reason_and_never_the_exception_text():
    # given
    class Wrapped(Forbidden):
        def __str__(self):
            return SENTINEL

    class Auth:
        enabled = True

        def challenge(self, *a):
            return 'Bearer'

        def user(self, token):
            raise Wrapped('the token has no viewer role', 'no role')

    # when
    sent = run_endpoint(Auth())

    # then
    assert sent[0]['status'] == 403
    assert json.loads(sent[1]['body']) == {'detail': 'the token has no viewer role'}


def test_the_intake_reaction_log_carries_the_kind_only(caplog):
    # given
    slack = SimpleNamespace(react=raising(SlackError(SENTINEL)))
    intake = SimpleNamespace(listen='live', slack=slack)
    record = {}

    # when
    with caplog.at_level(logging.WARNING):
        Intake._react(intake, 'C1', '1.1', record, ['+1'])

    # then
    clean(caplog, 'reaction in C1 failed: SlackError')
