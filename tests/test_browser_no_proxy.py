import pytest

from skopax import remote
from skopax.kit.browser import Browser, Connection
from skopax.plugins import website
from tests.fake_cdp import WS_PATH, FakeChrome, Site
from tests.test_remote import Proxy, clear_proxies
from tests.website_kit import CHAIRS, NOW, SHOP, Clock, quiet, website_runtime


@pytest.fixture
def chrome():
    c = FakeChrome({CHAIRS: Site()})
    yield c
    c.close()


@pytest.fixture
def proxy(monkeypatch):
    p = Proxy('refuse')
    clear_proxies(monkeypatch)
    remote.opener.cache_clear()
    yield p
    p.close()
    remote.opener.cache_clear()


def proxied(monkeypatch, proxy: Proxy, *names: str) -> None:
    for name in names:
        monkeypatch.setenv(name, f'http://127.0.0.1:{proxy.port}')
    monkeypatch.setenv('NO_PROXY', 'browser.test')


@pytest.mark.parametrize('names', [('HTTP_PROXY', 'HTTPS_PROXY'), ('HTTPS_PROXY',)], ids=['both', 'https-only'])
def test_the_browser_version_read_never_goes_through_the_proxy(chrome, proxy, monkeypatch, names):
    # given
    proxied(monkeypatch, proxy, *names)

    # when
    version = Browser(chrome.url, remote.send, resolve=lambda host: '127.0.0.1').version()

    # then
    assert version == 'Chrome/157.0.8103.15' and proxy.asked == []


@pytest.mark.parametrize('names', [('HTTP_PROXY', 'HTTPS_PROXY'), ('HTTPS_PROXY',)], ids=['both', 'https-only'])
def test_the_devtools_socket_never_goes_through_the_proxy(chrome, proxy, monkeypatch, names):
    # given
    proxied(monkeypatch, proxy, *names)

    # when
    conn = Connection.open(f'ws://127.0.0.1:{chrome.port}{WS_PATH}', 3)
    try:
        answer = conn.call('Target.createBrowserContext', {'disposeOnDetach': True})
    finally:
        conn.close()

    # then
    assert answer['browserContextId'] and proxy.asked == []


def test_a_shot_under_a_proxy_reaches_the_browser_directly(chrome, proxy, monkeypatch):
    # given
    quiet(monkeypatch)
    proxied(monkeypatch, proxy, 'HTTP_PROXY', 'HTTPS_PROXY')
    clock = Clock(500.0)
    b = Browser(chrome.url, remote.send, resolve=lambda host: '127.0.0.1', clock=clock, sleep=clock.sleep, settle=0.05, gap=0)

    # when
    pic = b.shoot(CHAIRS, SHOP)

    # then
    assert pic.data and proxy.asked == []


def test_the_website_browser_check_on_the_production_transport_never_goes_through_the_proxy(store, chrome, proxy, monkeypatch):
    # given
    proxied(monkeypatch, proxy, 'HTTP_PROXY', 'HTTPS_PROXY')
    runtime = website_runtime(store, None, {'main': {'browser-url': f'http://127.0.0.1:{chrome.port}'}})
    ctx = runtime.context(website.PLUGIN, runtime.instances.get('website', 'main'), NOW)

    # when
    swept = website.browser_check(ctx)

    # then
    assert runtime.transport is None and ctx.http() is remote.send
    assert swept.result.status == 'ok' and swept.sections['browser'][0]['version'] == 'Chrome/157.0.8103.15'
    assert proxy.asked == []
