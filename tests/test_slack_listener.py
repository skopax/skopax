import dataclasses
import datetime as dt
import logging
import threading
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from tests.conftest import AUTH, anomaly, settings, state
from tests.fake_anthropic import FakeAnthropic
from tests.fake_slack import fake_slack, slack_instance
from skopax.app import create_app
from skopax.messages import msg
from skopax.slackevents import DOWN_ALERT_S, SDK_LOGGER, Listener, TicketRefused, quiet_self_healing
from skopax.store import Store


class FakeSocket:
    def __init__(self, fail: bool = False):
        self.fail = fail
        self.socket_mode_request_listeners = []
        self.acks: list[str] = []
        self.connected = threading.Event()
        self.closed = 0

    def connect(self) -> None:
        if self.fail:
            raise ConnectionError('no route to wss-primary.slack.com')
        self.connected.set()

    def send_socket_mode_response(self, resp) -> None:
        self.acks.append(resp.envelope_id)

    def close(self) -> None:
        self.closed += 1

    def is_connected(self) -> bool:
        return self.connected.is_set()


class Factory:
    def __init__(self, failures: int = 0):
        self.failures = failures
        self.tokens: list[str] = []
        self.made: list[FakeSocket] = []

    def __call__(self, token: str) -> FakeSocket:
        self.tokens.append(token)
        self.made.append(FakeSocket(fail=len(self.made) < self.failures))
        return self.made[-1]


class Recorder:
    def __init__(self):
        self.seen: list[dict] = []

    def handle(self, payload: dict) -> str:
        self.seen.append(payload)
        return 'seen'


def req(kind: str, payload: dict, envelope: str = 'e1') -> SimpleNamespace:
    return SimpleNamespace(type=kind, payload=payload, envelope_id=envelope)


def wait_for(ready, seconds: float = 2.0) -> None:
    for _ in range(int(seconds / 0.01)):
        if ready():
            return
        threading.Event().wait(0.01)


def test_an_envelope_is_acked_first_and_queued_and_a_full_queue_drops_with_a_log(caplog):
    # given
    sock = FakeSocket()
    listener = Listener('xapp-test', Recorder(), Recorder(), lambda token: sock, queue_size=1)

    # when
    with caplog.at_level(logging.WARNING, logger='skopax.slackevents'):
        listener.on_request(sock, req('events_api', {'event': {'type': 'message'}}, 'e1'))
        listener.on_request(sock, req('events_api', {'event': {'type': 'message'}}, 'e2'))
        listener.on_request(sock, req('hello', {}, 'e3'))

    # then
    assert sock.acks == ['e1', 'e2', 'e3']
    assert listener.queue.qsize() == 1
    assert 'slack queue full, dropped a events_api envelope' in caplog.text


def test_events_go_to_the_intake_and_clicks_to_the_click_handler():
    # given
    intake, clicks = Recorder(), Recorder()
    listener = Listener('xapp-test', intake, clicks, lambda token: FakeSocket())

    # when
    listener.process(('events_api', {'event': {'type': 'app_mention', 'ts': '1.1'}}))
    listener.process(('interactive', {'type': 'block_actions'}))

    # then
    assert intake.seen == [{'type': 'app_mention', 'ts': '1.1'}]
    assert clicks.seen == [{'type': 'block_actions'}]


def test_the_listener_retries_a_failed_connect_and_closes_on_stop():
    # given
    factory = Factory(failures=1)
    listener = Listener('xapp-test', Recorder(), Recorder(), factory, retry_s=0.01)

    # when
    listener.start()
    wait_for(lambda: len(factory.made) == 2 and factory.made[1].connected.is_set())
    listener.stop()

    # then
    assert factory.tokens == ['xapp-test', 'xapp-test']
    assert factory.made[1].connected.is_set()
    assert factory.made[1].socket_mode_request_listeners == [listener.on_request]
    assert factory.made[1].closed == 1


def test_a_stop_during_connect_leaves_no_open_socket():
    # given
    holder = {}
    sock = FakeSocket()
    sock.connect = lambda: holder['listener'].stop()
    listener = Listener('xapp-test', Recorder(), Recorder(), lambda token: sock)
    holder['listener'] = listener

    # when
    listener._connect()

    # then
    assert sock.closed >= 1
    assert listener.client is None


def test_a_raising_factory_is_retried():
    # given
    made = []

    def factory(token):
        if not made:
            made.append(None)
            raise RuntimeError('boom')
        made.append(FakeSocket())
        return made[-1]

    listener = Listener('xapp-test', Recorder(), Recorder(), factory, retry_s=0.01)

    # when
    listener._connect()

    # then
    assert len(made) == 2 and made[1].connected.is_set()


def listening_app(store: Store, factory: Factory, listen: str = 'shadow', app_token: str = 'xapp-test'):
    s = dataclasses.replace(settings(), anthropic_api_key='sk-test', investigate_mode='shadow', slack_listen=listen)
    slack, _, _ = fake_slack()
    app = create_app(s, store, slack=slack, anthropic_client=FakeAnthropic([]), operators_lookup=lambda email: email in set(), slack_socket=factory)
    slack_instance(app, app_token=app_token)
    return app


def test_the_app_opens_the_socket_in_its_lifespan_and_reports_it(store: Store):
    # given
    factory = Factory()

    # when
    with TestClient(listening_app(store, factory)) as c:
        wait_for(lambda: bool(factory.made) and factory.made[0].connected.is_set())
        info = c.get('/api/server').json()['slack']
        same_reply = c.app.state.investigator.effects.slack_reply == c.app.state.replier.reply

    # then
    assert info == {'listen': 'shadow', 'socket': True, 'link': {'connected': True, 'downSince': None, 'drops': 0}}
    assert factory.tokens == ['xapp-test'] and factory.made[0].closed == 1
    assert same_reply


def test_listen_off_opens_no_socket(store: Store):
    # given
    factory = Factory()

    # when
    with TestClient(listening_app(store, factory, listen='off')) as c:
        info = c.get('/api/server').json()['slack']

    # then
    assert info == {'listen': 'off', 'socket': False}
    assert factory.tokens == [] and c.app.state.listener is None


def test_no_app_token_means_no_socket_and_a_warning(store: Store, caplog):
    # given
    factory = Factory()

    # when
    with caplog.at_level(logging.WARNING, logger='skopax'):
        with TestClient(listening_app(store, factory, app_token='')) as c:
            info = c.get('/api/server').json()['slack']

    # then
    assert info == {'listen': 'shadow', 'socket': False} and factory.tokens == []
    assert 'the Slack listener stays off' in caplog.text


def test_a_slack_click_runs_the_pages_create_task_path(store: Store):
    # given
    store.save(state(anomaly('k1')), None)
    app = listening_app(store, Factory(), listen='off')

    # then
    with pytest.raises(TicketRefused, match='Jira is not set up'):
        app.state.clicks.apply({'type': 'ticket', 'key': 'k1', 'target': 'k1', 'title': 't', 'description': 'd', 'requestedBy': 'adam.baker'})


def test_a_slack_card_action_click_runs_the_pages_action_path(store: Store):
    # given
    store.save(state(anomaly('k1')), None)
    app = listening_app(store, Factory(), listen='off')

    # when
    result = app.state.clicks.apply({'type': 'mute', 'key': 'k1', 'target': 'k1', 'hours': 6, 'requestedBy': 'adam.baker'})

    # then
    assert result.startswith('muted until') and store.anomaly('k1')['muted']
    with pytest.raises(TicketRefused):
        app.state.clicks.apply({'type': 'mute', 'key': 'gone', 'target': 'gone', 'hours': 6, 'requestedBy': 'adam.baker'})


def test_slack_intake_records_are_listed_for_operators(store: Store):
    # given
    store.seen('C1', '1.1', dt.datetime(2026, 10, 24, 12, 0, tzinfo=dt.timezone.utc))
    store.record_intake('C1', '1.1', {'decision': 'not-operator', 'user': 'U9'})
    client = TestClient(listening_app(store, Factory(), listen='off'))

    # when
    listed = client.get('/api/slack-intake?limit=5', headers=AUTH)
    guest = client.get('/api/slack-intake')

    # then
    assert [(r['id'], r['decision']) for r in listed.json()] == [('C1:1.1', 'not-operator')]
    assert guest.status_code == 401


def cancel_app(store: Store):
    s = dataclasses.replace(settings(), anthropic_api_key='sk-test', investigate_mode='shadow', slack_listen='live')
    slack, post, api = fake_slack()
    app = create_app(s, store, slack=slack, anthropic_client=FakeAnthropic([]), operators_lookup=lambda email: email in set(), slack_socket=Factory())
    slack_instance(app, app_token='xapp-test')
    return app, post, api


def test_cancelling_a_slack_ask_from_the_page_says_so_in_its_thread_and_drops_the_placeholder(store: Store):
    # given
    app, post, api = cancel_app(store)
    act = store.add_action({'type': 'ask', 'source': 'slack', 'key': 'ask', 'target': 'slack:C1:x', 'text': 'q', 'cards': [],
                            'slack': {'where': 'card', 'channel': 'C1', 'threadTs': '1792224000.000100', 'ts': '1792224000.000200', 'user': 'U1', 'placeholderTs': '1792224000.000300'}})

    # when
    with TestClient(app) as c:
        r = c.patch(f"/api/actions/{act['id']}", json={'status': 'cancelled', 'expect': 'requested'}, headers=AUTH)

    # then
    assert r.status_code == 200
    assert [(p['thread_ts'], p['text']) for p in post.calls] == [('1792224000.000100', msg('slackevents.reply.stopped'))]
    assert [p['ts'] for m, p in api.calls if m == 'chat.delete'] == ['1792224000.000300']
    assert store.get_action(act['id'])['slackReply']['text'] == msg('slackevents.reply.stopped')


def test_cancelling_a_page_ask_makes_no_slack_call(store: Store):
    # given
    app, post, api = cancel_app(store)
    act = store.add_action({'type': 'ask', 'key': 'ask', 'target': 'page', 'text': 'q', 'cards': []})

    # when
    with TestClient(app) as c:
        r = c.patch(f"/api/actions/{act['id']}", json={'status': 'cancelled', 'expect': 'requested'}, headers=AUTH)

    # then
    assert r.status_code == 200
    assert [m for m, _ in api.calls if m in ('chat.update', 'chat.postMessage')] == [] and post.calls == []


def test_the_sdk_s_self_healing_socket_check_logs_at_info_and_other_sdk_errors_stay_errors(caplog):
    # given
    quiet_self_healing()
    quiet_self_healing()
    sdk = logging.getLogger(SDK_LOGGER)

    # when
    with caplog.at_level(logging.INFO, logger=SDK_LOGGER):
        sdk.error('Failed to check the state of sock (session id: s1, error: ConnectionResetError, message: [Errno 104] Connection reset by peer)')
        sdk.error('Failed to connect (error: no route)')

    # then
    levels = [(r.levelname, r.getMessage()[:20]) for r in caplog.records if r.name == SDK_LOGGER]
    assert levels == [('INFO', 'Failed to check the '), ('ERROR', 'Failed to connect (e')]
    assert len(sdk.filters) == 1, 'the filter is added once'


def test_the_watchdog_logs_one_error_when_the_socket_stays_down_and_a_warning_when_it_is_back(caplog):
    # given
    clock = [1000.0]
    listener = Listener('xapp', None, None, Factory(), clock=lambda: clock[0])
    sock = FakeSocket()
    listener.client = sock

    # when
    with caplog.at_level(logging.INFO, logger='skopax.slackevents'):
        listener.check()
        clock[0] += 60
        listener.check()
        clock[0] += 70
        listener.check()
        clock[0] += 60
        listener.check()
        down = listener.status()
        sock.connected.set()
        clock[0] += 30
        listener.check()

    # then
    errors = [r.getMessage() for r in caplog.records if r.levelname == 'ERROR']
    assert errors == ['slack socket has been down for 130 s: Slack asks, button clicks and thread replies are not reaching this pod'], 'one ERROR after 2 min, not every check'
    assert down == {'connected': False, 'downSince': 1000.0, 'drops': 1}
    assert [r.getMessage() for r in caplog.records if r.levelname == 'WARNING'] == ['slack socket is back after 220 s down']
    assert listener.status() == {'connected': True, 'downSince': None, 'drops': 1} and DOWN_ALERT_S == 120


def test_a_short_drop_the_sdk_heals_logs_nothing():
    # given
    clock = [0.0]
    listener = Listener('xapp', None, None, Factory(), clock=lambda: clock[0])
    sock = FakeSocket()
    listener.client = sock

    # when
    listener.check()
    clock[0] += 30
    listener.check()
    sock.connected.set()
    clock[0] += 30
    listener.check()

    # then
    assert listener.status() == {'connected': True, 'downSince': None, 'drops': 1} and listener.alerted_at is None


def test_a_click_is_handled_while_a_slow_question_is_still_being_screened():
    # given
    held = threading.Event()

    class SlowIntake(Recorder):
        def handle(self, payload: dict) -> str:
            held.wait(5)
            return super().handle(payload)
    intake, clicks = SlowIntake(), Recorder()
    sock = FakeSocket()
    listener = Listener('xapp-test', intake, clicks, lambda token: sock)

    # when
    listener.start()
    listener.on_request(sock, req('events_api', {'event': {'type': 'app_mention', 'ts': '1.1'}}, 'e1'))
    listener.on_request(sock, req('interactive', {'type': 'block_actions', 'trigger_id': 'T1'}, 'e2'))
    wait_for(lambda: clicks.seen)
    clicked_first = (list(clicks.seen), list(intake.seen))
    held.set()
    wait_for(lambda: intake.seen)
    listener.stop()

    # then
    assert clicked_first == ([{'type': 'block_actions', 'trigger_id': 'T1'}], []), 'the Known noise window opens within the 3 s trigger'
    assert intake.seen == [{'event': {'type': 'app_mention', 'ts': '1.1'}}['event']]
