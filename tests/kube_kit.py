from urllib.parse import urlsplit

import mongomock
from fastapi.testclient import TestClient

from skopax import remote
from skopax.agent import create_agent
from skopax.contract import AgentRead, Capabilities, Field
from skopax.kube import KubeError
from skopax.instances import Instances, PluginState
from skopax.plugins import kubernetes
from skopax.registry import Registry
from skopax.runtime import Runtime
from skopax.sealer import Sealer
from tests.agent_kit import NOW, FakeKube as AgentKube, agent_boot
from tests.plugins.base import minimal

KEY = 'kube-key'
MAIN_NOW = 1_792_224_000.0
ECHO_FIELDS = (Field('echo.url', 'Echo URL', 'url'), Field('echo.token', 'Echo token', 'secret'))
ECHO_KEYS = (r'echo\.url', r'echo\.token')


def echo_sources(config):
    return ['echo'] if config.get('echo.url') else []


def echo_run(config, args):
    return {'echo': args.get('text', ''), 'url': config.get('echo.url'), 'token': config.get('echo.token')}


def echoer(timeout: int = 75):
    read = AgentRead('echoer-read', ('text',), ECHO_KEYS, echo_sources, echo_run, fields=ECHO_FIELDS, timeout=timeout)
    return minimal('echoer', 'Echoer', capabilities=Capabilities(secrets=('echo.token',), agent_reads=True), agent_reads=(read,))


class FakeKube:
    enabled = True

    def __init__(self, answer=None, fail: str | None = None, code: int | None = None):
        self.answer = answer
        self.fail = fail
        self.code = code
        self.asked: list[tuple] = []

    def _reply(self, *call):
        self.asked.append(call)
        if self.fail:
            raise KubeError(self.fail, self.code)
        return self.answer

    def pods(self, namespace, selector=None):
        return self._reply('pods', namespace, selector)

    def pod(self, namespace, name):
        return self._reply('pod', namespace, name)

    def events(self, namespace, obj, since_minutes, now):
        return self._reply('events', namespace, obj, since_minutes, now)

    def rollout(self, namespace, deployment):
        return self._reply('rollout', namespace, deployment)

    def previous_logs(self, namespace, pod, container, tail):
        return self._reply('previous_logs', namespace, pod, container, tail)

    def nodes(self):
        return self._reply('nodes')

    def node(self, name, now):
        return self._reply('node', name, now)

    def daemonset(self, namespace, name):
        return self._reply('daemonset', namespace, name)


class Seen:
    def __init__(self):
        self.calls: list[tuple] = []

    def paths(self) -> list[tuple[str, str]]:
        return [(c[0], urlsplit(c[1]).path) for c in self.calls]


def app_transport(routes: dict[str, TestClient], seen: Seen | None = None):
    def send(method, url, headers, data, timeout):
        parts = urlsplit(url)
        if seen is not None:
            seen.calls.append((method, url, timeout, headers, data))
        client = routes.get(parts.hostname)
        if client is None:
            raise remote.Unreachable(f'{url} unreachable (no route)')
        resp = client.request(method, parts.path, headers=headers, content=data)
        if resp.status_code >= 400:
            raise remote.HttpError(resp.status_code, parts.path, resp.text)
        return remote.Reply(resp.json(), {})

    return send


def agent_app(token: str, plugins=(), relay_hosts: str = '', kube=None, transport=None) -> TestClient:
    env = {'SKOPAX_AGENT_TOKEN': token, 'SKOPAX_AGENT_RELAY_HOSTS': relay_hosts}
    return TestClient(create_agent(agent_boot(env=env, plugins=plugins), kube=kube or AgentKube(), transport=transport or app_transport({}), clock=lambda: NOW, refresh_every=None))


def kube_runtime(store, instances: dict[str, dict] | None = None, kube=None, transport=None, plugins=()) -> Runtime:
    registry = Registry.of(kubernetes.PLUGIN, *plugins)
    box: list[Runtime] = []
    db = memory_db()
    stored = Instances(db, registry, lambda: Sealer(KEY), extra_fields=lambda plugin_id: box[0].agent_fields(plugin_id))
    runtime = Runtime(registry, PluginState(db), stored, transport)
    box.append(runtime)
    runtime.kube = kube
    for name, values in (instances or {}).items():
        stored.create('kubernetes', name, values, True, 'test')
    runtime.refresh()
    return runtime


def memory_db():
    return mongomock.MongoClient(tz_aware=True)['kit']
