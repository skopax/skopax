import runpy
import threading

from fastapi.testclient import TestClient

from skopax import remote
from skopax.agent import create_agent
from skopax.contract import Hooks
from skopax.kube import KubeError
from skopax.registry import Registry
from skopax.settingsources import Boot, light_boot
from tests.plugin_kit import registry_with
from tests.plugins import demo_plugin
from tests.plugins.base import minimal

TOKEN = 'agent-token-0001'
AUTH = {'Authorization': f'Bearer {TOKEN}'}
RELAY_HOSTS = 'agent.example.com,*.example.org'
NOW = 1_700_000_000.0
ROUTES = {
    ('GET', '/actuator/health/liveness'), ('GET', '/actuator/health/readiness'), ('GET', '/actuator/health'), ('GET', '/api/agent'), ('POST', '/api/kube'), ('POST', '/api/read'), ('PUT', '/api/agent/sources'),
    ('GET', '/api/relay/{rest:path}'), ('POST', '/api/relay/{rest:path}'), ('PUT', '/api/relay/{rest:path}'),
}


class FakeKube:
    def __init__(self, enabled: bool = True):
        self.enabled = enabled
        self.calls: list[tuple] = []

    def nodes(self):
        self.calls.append(('nodes',))
        if not self.enabled:
            raise KubeError('kube is off')
        return [{'name': 'n1', 'ready': 'True'}, {'name': 'n2', 'ready': 'False'}]

    def pods(self, namespace, selector=None):
        self.calls.append(('pods', namespace, selector))
        return [{'name': 'checkout-service-1'}]

    def pod(self, namespace, name):
        self.calls.append(('pod', namespace, name))
        if name == 'forbidden':
            raise KubeError('pods is forbidden', 403)
        return {'name': name}

    def events(self, namespace, obj, since_minutes, now):
        self.calls.append(('events', namespace, obj, since_minutes, now))
        return []

    def rollout(self, namespace, deployment):
        self.calls.append(('rollout', namespace, deployment))
        return {'name': deployment}

    def previous_logs(self, namespace, pod, container, tail):
        self.calls.append(('previous_logs', namespace, pod, container, tail))
        return 'line'

    def node(self, name, now):
        self.calls.append(('node', name, now))
        return {'name': name}

    def daemonset(self, namespace, name):
        self.calls.append(('daemonset', namespace, name))
        return {'name': name}


class Feed:
    def __init__(self, values: dict[str, str] | None = None):
        self.values = dict(values or {})
        self.calls = 0
        self.failure: Exception | None = None
        self.hold: threading.Event | None = None
        self.held = threading.Event()
        self.released = threading.Event()

    def __call__(self, env):
        self.calls += 1
        hold = self.hold
        if hold is not None:
            self.held.set()
            hold.wait(5)
            self.released.set()
        if self.failure is not None:
            raise self.failure
        return {'values': dict(self.values)}


def feed_plugin(feed: Feed):
    return minimal('agent-feed', 'Agent feed', hooks=Hooks(settings_source=feed))


def agent_boot(env: dict[str, str] | None = None, values: dict[str, str] | None = None, plugins=(demo_plugin,), feed: Feed | None = None) -> Boot:
    env = dict(env) if env is not None else {'SKOPAX_AGENT_TOKEN': TOKEN, 'SKOPAX_AGENT_RELAY_HOSTS': RELAY_HOSTS}
    return light_boot(env, registry_with(*plugins, feed_plugin(feed if feed is not None else Feed(values))))


class Transport:
    def __init__(self, answer=None):
        self.answer = answer if answer is not None else {'ok': True}
        self.calls: list[tuple] = []

    def __call__(self, method, url, headers, data, timeout):
        self.calls.append((method, url, headers, data, timeout))
        if isinstance(self.answer, Exception):
            raise self.answer
        return remote.Reply(self.answer, {})


def agent_client(boot: Boot | None = None, kube: FakeKube | None = None, transport=None) -> TestClient:
    return TestClient(create_agent(boot or agent_boot(), kube=kube or FakeKube(), transport=transport or Transport(), clock=lambda: NOW, refresh_every=None))


def relay_headers(to: str = 'https://agent.example.com', inner: str = 'Bearer inner-token-0002') -> dict:
    return {**AUTH, 'X-Relay-To': to, 'X-Relay-Authorization': inner}


def run_main(monkeypatch, registry: Registry) -> dict:
    monkeypatch.setattr('skopax.logs.setup', lambda *a, **k: None)
    monkeypatch.setattr(Registry, 'load', lambda *a, **k: registry)
    return runpy.run_module('skopax.main', run_name='skopax_main_under_test')


def run_asgi(app, method: str, path: str, headers: dict, chunks: list[bytes]) -> tuple[int, int, bytes]:
    import asyncio

    pending = list(chunks)
    received = [0]
    sent: list[dict] = []

    async def receive():
        received[0] += 1
        if pending:
            piece = pending.pop(0)
            return {'type': 'http.request', 'body': piece, 'more_body': bool(pending)}
        return {'type': 'http.disconnect'}

    async def send(message):
        sent.append(message)

    scope = {'type': 'http', 'asgi': {'version': '3.0'}, 'http_version': '1.1', 'method': method, 'scheme': 'http', 'path': path, 'raw_path': path.encode(), 'query_string': b'', 'root_path': '',
             'headers': [(k.lower().encode(), v.encode()) for k, v in headers.items()], 'client': ('127.0.0.1', 1), 'server': ('test', 80)}
    asyncio.run(app(scope, receive, send))
    status = next(m['status'] for m in sent if m['type'] == 'http.response.start')
    return status, received[0], b''.join(m.get('body', b'') for m in sent if m['type'] == 'http.response.body')
