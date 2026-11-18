import datetime as dt
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import mongomock

from skopax.contract import Plugin
from skopax.instances import Instances, PluginState
from skopax.plugins import gitlab, jira, prometheus
from skopax.plugins.gitlab.client import GITLAB_URL
from skopax.plugintools import PluginTools
from skopax.registry import Registry
from skopax.remote import HttpError, Reply
from skopax.runtime import Runtime
from skopax.sealer import Sealer, master_key
from tests.plugins import demo_plugin

FIXTURES = {'demo': demo_plugin.PLUGIN, 'prometheus': prometheus.PLUGIN, 'gitlab': gitlab.PLUGIN, 'jira': jira.PLUGIN}
KIT_KEY = 'kit-key'
PROM_URL = 'https://prom.example.com'
GITLAB_VALUES = {'url': GITLAB_URL, 'token': 'glpat', 'group': 'acme'}
JIRA_SITE = 'https://jira.example.com'
JIRA_VALUES = {'url': JIRA_SITE, 'email': 'bot@example.com', 'api-token': 'jt', 'cloud-id': 'cid'}


async def no_loop(self, every=0):
    return None


def registry_with(*modules) -> Registry:
    return Registry.of(*[m.PLUGIN if hasattr(m, 'PLUGIN') else m for m in modules])


def rule_patterns(runtime: Runtime) -> tuple:
    return tuple(pattern for _, pattern in runtime.redaction_rules())


def add_instance(store, plugin: Plugin | str, name: str, sealer_key: str = '', primary: bool | None = None, enabled: bool | None = None, **values) -> dict:
    found = FIXTURES.get(plugin) if isinstance(plugin, str) else plugin
    plugin_id = plugin if isinstance(plugin, str) else plugin.id
    secret_keys = {f.key for f in found.fields if f.kind == 'secret'} if found is not None else {'token'}
    sealer = Sealer(sealer_key)
    plain = {k: v for k, v in values.items() if k not in secret_keys}
    sealed = {k: sealer.seal(v) for k, v in values.items() if k in secret_keys}
    doc = {'_id': f'{plugin_id}/{name}', 'plugin': plugin_id, 'name': name, 'values': plain, 'secrets': sealed, 'createdBy': 'test', 'createdAt': dt.datetime.now(dt.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')}
    if primary is not None:
        doc['primary'] = primary
    if enabled is not None:
        doc['enabled'] = enabled
    store.db['instances'].replace_one({'_id': doc['_id']}, doc, upsert=True)
    return doc


def prom_runtime(store, instances: dict[str, dict] | None = None, transport=None) -> Runtime:
    registry = Registry.of(prometheus.PLUGIN)
    runtime = Runtime(registry, PluginState(store.db), Instances(store.db, registry, lambda: Sealer(KIT_KEY)), transport)
    for index, (name, values) in enumerate((instances or {}).items()):
        add_instance(store, prometheus.PLUGIN, name, sealer_key=KIT_KEY, primary=index == 0, **({'url': PROM_URL} | values))
    runtime.refresh()
    return runtime


def fake_transport(fake):
    def send(method, url, headers, data, timeout):
        path = urlsplit(url).path
        params = parse_qs(urlsplit(url).query)
        if path.endswith('/api/v1/query'):
            rows = fake.instant(params['query'][0], float(params['time'][0]))
        elif path.endswith('/api/v1/query_range'):
            rows = fake.range_query(params['query'][0], float(params['start'][0]), float(params['end'][0]), int(params['step'][0]))
        else:
            raise HttpError(404, path)
        return Reply({'status': 'success', 'data': {'result': rows}}, {})

    return send


def prom_tools(fake) -> PluginTools:
    store = SimpleNamespace(db=mongomock.MongoClient(tz_aware=True)['kit'])
    return PluginTools(prom_runtime(store, {'main': {}}, fake_transport(fake)))


def gitlab_runtime(store, transport=None, clients: dict | None = None, named: str | None = 'main', prom=None, with_jira: bool = False, **values) -> Runtime:
    plugins = (gitlab.PLUGIN,) + ((prometheus.PLUGIN,) if prom is not None else ()) + ((jira.PLUGIN,) if with_jira else ())
    registry = Registry.of(*plugins)
    runtime = Runtime(registry, PluginState(store.db), Instances(store.db, registry, lambda: Sealer(KIT_KEY)), fake_transport(prom) if prom is not None else transport, clients)
    if named:
        add_instance(store, gitlab.PLUGIN, named, sealer_key=KIT_KEY, primary=True, **(GITLAB_VALUES | values))
    if prom is not None:
        add_instance(store, prometheus.PLUGIN, 'main', sealer_key=KIT_KEY, primary=True, url=PROM_URL)
    if with_jira:
        add_instance(store, jira.PLUGIN, 'main', sealer_key=KIT_KEY, primary=True, **JIRA_VALUES)
    runtime.refresh()
    return runtime


def gitlab_tools(fake, prom=None, runtime: Runtime | None = None, with_jira: bool = False) -> PluginTools:
    if runtime is None:
        store = SimpleNamespace(db=mongomock.MongoClient(tz_aware=True)['kit'])
        runtime = gitlab_runtime(store, clients=None if fake is None else {'gitlab': fake}, prom=prom, with_jira=with_jira)
    elif fake is not None:
        runtime.clients['gitlab'] = fake
    return PluginTools(runtime)


def jira_runtime(store, transport=None, clients: dict | None = None, named: str | None = 'main', plugins=(), **values) -> Runtime:
    registry = Registry.of(jira.PLUGIN, *plugins)
    runtime = Runtime(registry, PluginState(store.db), Instances(store.db, registry, lambda: Sealer(KIT_KEY)), transport, clients)
    if named:
        add_instance(store, jira.PLUGIN, named, sealer_key=KIT_KEY, primary=True, **(JIRA_VALUES | values))
    runtime.refresh()
    return runtime


def jira_tools(fake=None, store=None, plugins=(), **values) -> PluginTools:
    store = store or SimpleNamespace(db=mongomock.MongoClient(tz_aware=True)['kit'])
    return PluginTools(jira_runtime(store, clients=None if fake is None else {'jira': fake}, plugins=plugins, **values))


def serve_jira(store, settings, **values) -> dict:
    return add_instance(store, jira.PLUGIN, 'main', sealer_key=master_key(store.db, settings), primary=True, **(JIRA_VALUES | values))


def idle_jira() -> SimpleNamespace:
    return SimpleNamespace(enabled=False)
