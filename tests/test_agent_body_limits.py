import http.server
import json
import threading

import pytest
from fastapi.testclient import TestClient

from skopax import agent, remote
from skopax.agent import AgentCore, create_agent
from tests.agent_kit import NOW, TOKEN, FakeKube, Transport, agent_boot, relay_headers, run_asgi
from tests.plugin_kit import registry_with
from tests.plugins import demo_plugin

BODY_ROUTES = [('POST', '/api/kube'), ('POST', '/api/read'), ('PUT', '/api/agent/sources'), ('POST', '/api/relay/api/kube'), ('PUT', '/api/relay/api/agent/sources')]


def keyed_app():
    return create_agent(agent_boot(), kube=FakeKube(), transport=Transport(), clock=lambda: NOW, refresh_every=None)


def live_threads():
    return [t for t in threading.enumerate() if t.name == 'agent-sources' and t.is_alive()]


@pytest.mark.parametrize('method,path', BODY_ROUTES)
def test_a_wrong_token_with_a_declared_length_over_the_cap_gets_401_and_nothing_is_received(method, path):
    # given
    extra = {k: v for k, v in relay_headers().items() if k != 'Authorization'} if path.startswith('/api/relay') else {}
    head = {'authorization': 'Bearer nope', 'content-length': str(2 * 1024 * 1024), **extra}

    # when
    status, received, _ = run_asgi(keyed_app(), method, path, head, [b'x' * 65536] * 4)

    # then
    assert (status, received) == (401, 0)


@pytest.mark.parametrize('declared', [chr(0x662) + chr(0x663), '9' * 5000, chr(0xb2), '12345678901234', '2097152'])
def test_a_malformed_or_huge_content_length_behind_a_valid_token_is_a_413_or_is_streamed_never_a_crash(declared):
    # given
    head = {'authorization': 'Bearer ' + TOKEN, 'content-length': declared}

    # when
    status, _, body = run_asgi(keyed_app(), 'POST', '/api/kube', head, [b'{"op": "nodes"}'])

    # then
    assert status in (200, 413)
    assert declared.isascii() is False or status == 413
    assert json.loads(body)


def test_the_relay_413_says_it_came_from_the_relay_and_the_plain_one_does_not():
    # given
    head = {'authorization': 'Bearer ' + TOKEN, 'content-length': str(2 * 1024 * 1024)}
    inner = {k: v for k, v in relay_headers().items() if k != 'Authorization'}

    # when
    plain = run_asgi(keyed_app(), 'POST', '/api/kube', head, [])
    relayed = run_asgi(keyed_app(), 'POST', '/api/relay/api/kube', head | inner, [])

    # then
    assert json.loads(plain[2]) == {'error': 'the body is larger than 1 MiB'}
    assert json.loads(relayed[2]) == {'error': 'the body is larger than 1 MiB', 'relay': True}


def test_a_second_lifespan_gets_its_own_refresher_thread():
    # given
    app = create_agent(agent_boot(), kube=FakeKube(), clock=lambda: NOW, refresh_every=0.01)

    # when
    with TestClient(app) as client:
        client.get('/actuator/health')
        first = len(live_threads())
    between = len(live_threads())
    with TestClient(app) as client:
        client.get('/actuator/health')
        second = len(live_threads())
    after = len(live_threads())

    # then
    assert (first, between, second, after) == (1, 0, 1, 0)


def test_no_refresher_thread_exists_before_the_app_is_served():
    # given
    app = create_agent(agent_boot(), kube=FakeKube(), clock=lambda: NOW, refresh_every=0.01)

    # then
    assert live_threads() == []
    assert app.state.refresher._thread is None


def test_a_relayed_body_the_encoder_cannot_nest_is_a_400_whatever_the_interpreter_limit(monkeypatch):
    # given
    def too_deep(*args, **kwargs):
        raise RecursionError

    monkeypatch.setattr(remote, 'call', too_deep)
    core = AgentCore(registry_with(demo_plugin), FakeKube(), lambda: TOKEN, lambda: ('agent.example.com',), lambda: {}, transport=Transport(), clock=lambda: NOW)

    # when
    status, out = core.dispatch('POST', '/api/relay/api/kube', f'Bearer {TOKEN}', {'op': 'nodes'}, 'https://agent.example.com', 'Bearer inner')

    # then
    assert status == 400 and set(out) == {'error'}


class Big(http.server.BaseHTTPRequestHandler):
    size = 0

    def do_GET(self):
        body = b'"' + b'x' * (self.size - 2) + b'"'
        self.send_response(200)
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def serve(size: int):
    handler = type('Sized', (Big,), {'size': size})
    server = http.server.HTTPServer(('127.0.0.1', 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def test_a_relay_answer_over_one_mebibyte_is_refused_and_one_at_the_cap_is_read():
    # given
    exact, over = serve(remote.ANSWER_LIMIT), serve(remote.ANSWER_LIMIT + 1)

    try:
        # when
        taken = remote.relay_send('GET', f'http://127.0.0.1:{exact.server_port}/x', {}, None, 5)
        with pytest.raises(remote.Unreachable) as refused:
            remote.relay_send('GET', f'http://127.0.0.1:{over.server_port}/x', {}, None, 5)
    finally:
        exact.shutdown()
        exact.server_close()
        over.shutdown()
        over.server_close()

    # then
    assert len(taken.body) == remote.ANSWER_LIMIT - 2 and remote.ANSWER_LIMIT == agent.BODY_LIMIT
    assert 'xxxx' not in str(refused.value)


def test_a_non_relay_call_keeps_reading_an_answer_over_one_mebibyte():
    # given
    big = serve(remote.ANSWER_LIMIT * 2)

    try:
        # when
        plain = remote.send('GET', f'http://127.0.0.1:{big.server_port}/x', {}, None, 10)
        capped = remote.sender(limit=100)
        with pytest.raises(remote.Unreachable):
            capped('GET', f'http://127.0.0.1:{big.server_port}/x', {}, None, 10)
    finally:
        big.shutdown()
        big.server_close()

    # then
    assert len(plain.body) == remote.ANSWER_LIMIT * 2 - 2
