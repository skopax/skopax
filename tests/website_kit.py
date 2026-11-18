import dataclasses

from skopax import remote
from skopax.instances import Instances, PluginState
from skopax.investigator.tools import Effects, Toolbox
from skopax.kit import browser as browser_kit, web
from skopax.plugins import website
from skopax.plugintools import PluginTools
from skopax.registry import Registry
from skopax.remote import Reply
from skopax.runtime import Runtime
from skopax.sealer import Sealer
from tests.conftest import settings
from tests.plugin_kit import KIT_KEY, add_instance
from tests.test_investigator_tools import NOW, sources

SITE = 'https://shop.example.com'
HOME = 'https://shop.example.com/'
CHAIRS = 'https://shop.example.com/garden/chairs'
BASE = 'https://skopax.example.com'
FIELDS = {'hosts': 'shop.example.net', 'login-hosts': 'login.shop.example.com', 'login-paths': 'login, auth, /account/sign-in', 'refused-paths': 'out, banner-click'}
SHOP = web.Site.of({'url': SITE} | FIELDS)


class Clock:
    def __init__(self, now: float = 1000.0):
        self.now = now
        self.slept: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(round(seconds, 3))
        self.now += seconds


def page(status: int = 200, body: bytes = b'', **headers) -> Reply:
    return Reply(body, {k.replace('_', '-'): v for k, v in headers.items()}, status)


def through(pages):
    def send(method, url, headers, data, timeout):
        if url.startswith('http://127.0.0.1:'):
            return remote.send(method, url, headers, data, timeout)
        return pages(method, url, headers, data, timeout)

    return send


def quiet(monkeypatch) -> None:
    monkeypatch.setattr(web, 'SPACING_S', 0)
    monkeypatch.setattr(web, 'resolve', lambda host: ['93.184.216.34'])
    monkeypatch.setattr(browser_kit, 'GAP_S', 0)
    monkeypatch.setattr(browser_kit, 'SETTLE_S', 0.05)
    web._pacers.clear()
    website.BROWSERS.clear()


def website_runtime(store, transport, instances: dict[str, dict] | None = None) -> Runtime:
    registry = Registry.of(website.PLUGIN)
    runtime = Runtime(registry, PluginState(store.db), Instances(store.db, registry, lambda: Sealer(KIT_KEY)), transport)
    for index, (name, values) in enumerate((instances or {'main': {}}).items()):
        add_instance(store, website.PLUGIN, name, sealer_key=KIT_KEY, primary=index == 0, **({'url': SITE} | FIELDS | values))
    runtime.refresh()
    return runtime


def effects() -> Effects:
    return Effects(lambda key, by: {}, lambda key, text, by, *_, **__: {})


def site_box(store, transport, instances: dict[str, dict] | None = None, mode: str = 'live', keys=('log|a|ERROR|x',), run_id: str | None = None, fx: Effects | None = None,
             runtime: Runtime | None = None, **kw) -> Toolbox:
    tools = PluginTools(runtime or website_runtime(store, transport, instances))
    return Toolbox(sources(plugin_tools=tools), store, fx or effects(), list(keys), mode, NOW, BASE, run_id=run_id,
                   settings=dataclasses.replace(settings(), link_hosts=('example.com', 'gitlab.com')), **kw)
