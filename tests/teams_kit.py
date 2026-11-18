import json

from skopax.auth import SESSION_COOKIE
from skopax.instances import Instances, PluginState
from skopax.plugins import teams
from skopax.registry import Registry
from skopax.remote import HttpError, Reply
from skopax.routes import Routes
from skopax.runtime import Runtime
from skopax.sealer import Sealer
from skopax.sessions import Sessions, session_secret
from skopax.tunables import Live, TunableStore, plugin_tunables
from tests.conftest import ADMIN_EMAIL, AUTH, settings
from tests.plugin_kit import KIT_KEY, add_instance

BASE = 'https://skopax.example.com'
AT = '2026-11-09T16:00:00Z'
SEO = 'https://hooks.example.com/workflows/seo/invoke?sig=SEO'
TECH = 'https://hooks.example.com/workflows/tech/invoke?sig=TECH'
BIZDEV = 'https://hooks.example.com/workflows/bizdev/invoke?sig=BIZ'
MARKETING = 'https://hooks.example.com/workflows/abc/invoke?api-version=1&sig=SECRET'
DATA = 'https://relay.example.com/workflows/def/invoke?sig=SECRET2'
OLD_DEFAULTS = {'repeat-days': '0', 'fixed-posts': 'true', 'min-sev': 'info'}
ALICE = 'alice.archer@example.com'
BOB = 'bob.stone@example.com'


class Transport:
    def __init__(self, fail: str | None = None):
        self.fail = fail
        self.calls: list[tuple[str, dict]] = []

    def __call__(self, method, url, headers, data, timeout):
        self.calls.append((url, json.loads(data)))
        if self.fail and self.fail in url:
            raise HttpError(400, url, 'bad card for ' + url)
        return Reply(None, {})


def texts(body: dict) -> list[str]:
    return [b['text'] for b in body['attachments'][0]['content']['body']]


def route(name: str, webhook: str, tags: str = '', keys: str = '', sev: str = '', mentions: str = '') -> tuple[str, dict]:
    values = {'webhook': webhook, 'tags': tags, 'keys': keys, 'min-sev': sev, 'mentions': mentions}
    return name, {k: v for k, v in values.items() if v}


def tag(*ids: str, by: str = 'pod') -> list[dict]:
    return [{'id': i, 'by': by, 'at': AT} for i in ids]


def open_card(key: str, sev: str = 'warn', **extra) -> dict:
    return {'key': key, 'sev': sev, 'svc': 'checkout-service', 'label': 'ERROR lines', 'what': 'NullPointerException in OrderService', 'status': 'open',
            'firstSeen': '2026-11-09T10:00:00Z', **extra}


def live_of(store, plugins, values: dict[str, str]) -> Live:
    return Live(settings(), TunableStore(store.db), extra=lambda: tuple(t for p in plugins for t in plugin_tunables(p)), values=values)


def runtime_of(store, transport, *specs, plugins=(teams.PLUGIN,), values: dict[str, str] | None = None) -> Runtime:
    registry = Registry.of(*plugins)
    runtime = Runtime(registry, PluginState(store.db), Instances(store.db, registry, lambda: Sealer(KIT_KEY)), transport)
    runtime.live = live_of(store, plugins, {f'teams.{k}': v for k, v in (values or {}).items()})
    for index, spec in enumerate(specs):
        plugin, name, given = (spec if len(spec) == 3 else (teams.PLUGIN, *spec))
        add_instance(store, plugin, name, sealer_key=KIT_KEY, primary=index == 0, **given)
    runtime.refresh()
    return runtime


def routes_of(store, transport, *specs, plugins=(teams.PLUGIN,), old: bool = True, attribution=None, timeout: float | None = None, **values) -> Routes:
    given = (OLD_DEFAULTS if old else {}) | {k.replace('_', '-'): str(v).lower() if isinstance(v, bool) else str(v) for k, v in values.items()}
    runtime = runtime_of(store, transport, *specs, plugins=plugins, values=given)
    extra = {} if timeout is None else {'timeout': timeout}
    return Routes(store, runtime, settings(), attribution=attribution or (lambda: None), **extra)


def add_route(client, name: str, webhook: str, **values) -> dict:
    sent = client.post('/api/admin/plugins/teams/instances', json={'name': name, 'values': {'webhook': webhook} | {k.replace('_', '-'): v for k, v in values.items()}, 'enabled': True}, headers=AUTH)
    assert sent.status_code == 201, sent.text
    return sent.json()


def operator_me(client, store) -> dict:
    cookie = Sessions(session_secret(store.db, settings()), 3600, 7200).issue(ADMIN_EMAIL, 'admin', 'local')
    client.cookies.set(SESSION_COOKIE, cookie)
    try:
        return client.get('/api/me').json()
    finally:
        client.cookies.clear()
