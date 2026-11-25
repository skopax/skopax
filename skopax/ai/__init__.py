from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field

ANTHROPIC = 'anthropic'
OPENAI = 'openai'
PROVIDERS = (ANTHROPIC, OPENAI)
LABELS = {ANTHROPIC: 'Anthropic', OPENAI: 'OpenAI'}
SEARCH_TYPE = 'web_search_20250305'
FETCH_TYPE = 'web_fetch_20250910'
THINK_OFF = 'off'
THINK_AUTO = 'auto'
COMPAT = 'compat'
CAPABILITIES = frozenset({'tools', 'images', 'web_search', 'web_fetch', 'thinking', 'cache', 'fallbacks'})
PROVIDER_CAPABILITIES = {ANTHROPIC: CAPABILITIES, OPENAI: frozenset({'tools', 'images', 'web_search', 'thinking', 'cache'})}
IMAGES_OFF = 'images not sent: this model does not read images'
FACTORIES: dict[str, 'Provider'] = {}


@dataclass(frozen=True)
class Provider:
    factory: Callable[[str, float, int], object | None]
    capabilities: object
    models: Callable[[], Iterable[str]] | None = None
    company: Callable[[str], str] | None = None
    owns: Callable[[str], bool] | None = None


@dataclass(frozen=True)
class Usage:
    input: int = 0
    cache_read: int = 0
    cache_write: int = 0
    cache_write_long: int = 0
    output: int = 0
    web_searches: int = 0

    def fields(self) -> dict:
        return {'input_tokens': self.input, 'output_tokens': self.output, 'cache_read_input_tokens': self.cache_read, 'cache_creation_input_tokens': self.cache_write + self.cache_write_long}


def _count(usage, name: str) -> int:
    value = usage.get(name) if isinstance(usage, dict) else getattr(usage, name, None)
    return int(value or 0)


def _inner(usage, name: str):
    return usage.get(name) if isinstance(usage, dict) else getattr(usage, name, None)


def neutral(usage) -> Usage:
    if isinstance(usage, Usage):
        return usage
    split = _inner(usage, 'cache_creation')
    hour = _count(split, 'ephemeral_1h_input_tokens') if split is not None else 0
    used = _inner(usage, 'server_tool_use')
    return Usage(input=_count(usage, 'input_tokens'), cache_read=_count(usage, 'cache_read_input_tokens'), cache_write=_count(usage, 'cache_creation_input_tokens') - hour,
                 cache_write_long=hour, output=_count(usage, 'output_tokens'), web_searches=_count(used, 'web_search_requests') if used is not None else 0)


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    input: dict
    broken: bool = False


@dataclass
class Turn:
    stop: str
    model: str
    usage: Usage
    texts: list[str] = field(default_factory=list)
    text: str = ''
    calls: list[ToolCall] = field(default_factory=list)
    native: list = field(default_factory=list)
    server: list = field(default_factory=list)


def provider_named(value: object) -> str:
    return value if value in PROVIDERS else ANTHROPIC


def is_openai(client: object) -> bool:
    if getattr(client, 'provider', None) == OPENAI:
        return True
    try:
        import openai
    except ImportError:
        return False
    return isinstance(client, openai.OpenAI)


def wrap(client: object):
    if client is None:
        return None
    if hasattr(client, 'turn') and hasattr(client, 'once'):
        return client
    if is_openai(client):
        from skopax.ai.openai_ai import OpenAiAi
        return OpenAiAi(client)
    from skopax.ai.anthropic_ai import AnthropicAi
    return AnthropicAi(client)


def maker(provider: Callable[[], str], claude: Callable[[str], object], timeout: float, retries: int) -> Callable[..., object]:
    def make(key: str, chosen: str | None = None) -> object:
        if provider_named(provider() if chosen is None else chosen) == OPENAI:
            import openai
            return openai.OpenAI(api_key=key, timeout=timeout, max_retries=retries)
        return claude(key)
    make.scoped = True
    return make


def register_provider(prefix: str, factory: Callable[[str, float, int], object | None], capabilities: Iterable[str] | Callable[[str], Iterable[str]],
                      models: Callable[[], Iterable[str]] | None = None, company: Callable[[str], str] | None = None, owns: Callable[[str], bool] | None = None) -> None:
    FACTORIES[prefix] = Provider(factory, capabilities, models, company, owns)


def extra_models() -> tuple[str, ...]:
    found: list[str] = []
    for held in list(FACTORIES.values()):
        if held.models is None:
            continue
        try:
            found += [str(m) for m in held.models() if isinstance(m, str) and m]
        except Exception:
            continue
    return tuple(dict.fromkeys(found))


def company_of(ref: str, provider: str | None = None) -> str:
    provider = provider or provider_of_ref(ref)
    held = FACTORIES.get(str(provider or '').partition(':')[0])
    if held is not None and held.company is not None:
        try:
            return str(held.company(ref) or '') or label_of(provider)
        except Exception:
            return label_of(provider)
    return label_of(provider)


def unregister_provider(prefix: str) -> None:
    FACTORIES.pop(prefix, None)


def owned(held: Provider, name: str) -> bool:
    try:
        return bool(held.owns(name))
    except Exception:
        return False


def provider_of_ref(ref: object) -> str | None:
    if not isinstance(ref, str) or not ref:
        return None
    if '/' in ref:
        name = ref.split('/', 1)[0]
        if not name:
            return None
        for prefix, held in list(FACTORIES.items()):
            if held.owns is not None and owned(held, name):
                return f'{prefix}:{name}'
        return f'{COMPAT}:{name}'
    from skopax.investigator.pricing import provider_of
    return provider_of(ref)


def label_of(provider: str | None) -> str:
    if provider in LABELS:
        return LABELS[provider]
    return str(provider or '').split(':', 1)[-1] or LABELS[ANTHROPIC]


def capabilities_of(client: object) -> frozenset[str]:
    found = getattr(client, 'capabilities', None)
    if found is not None and not callable(found):
        return frozenset(found) & CAPABILITIES
    adapter = wrap(client)
    found = getattr(adapter, 'capabilities', None) if adapter is not None else None
    return CAPABILITIES if found is None or callable(found) else frozenset(found) & CAPABILITIES


def explicit(settings, field: str) -> bool:
    if field in (getattr(settings, 'ai_models_held', None) or ()):
        return False
    if field in (getattr(settings, 'ai_models_set', None) or ()):
        return True
    from skopax.config import Settings
    held = Settings.__dataclass_fields__.get(field)
    return held is None or getattr(settings, field, '') != held.default


class Clients:
    def __init__(self, default: Callable[[], str], keys: Mapping[str, Callable[[], str]], claude: Callable[[str], object], timeout: float, retries: int, client: object | None = None):
        self.default = lambda: provider_named(default())
        self.keys = dict(keys)
        self.claude = claude
        self.timeout = timeout
        self.retries = retries
        self.client = client
        self._boxes: dict[str, object] = {}

    def set_client(self, client: object | None) -> None:
        self.client = client
        for provider, box in self._boxes.items():
            box.client = self.given(provider)

    @property
    def injected(self) -> bool:
        return self.client is not None

    def given(self, provider: str) -> object | None:
        if isinstance(self.client, Mapping):
            return self.client.get(provider)
        return self.client

    def key(self, provider: str | None) -> str:
        found = self.keys.get(provider or '')
        return (found() or '') if found is not None else ''

    def keyed(self, provider: str | None) -> bool:
        if not provider:
            return False
        if provider in PROVIDERS:
            return bool(self.key(provider))
        return self.compat(provider) is not None

    def box(self, provider: str | None = None):
        from skopax.aikey import ClientBox
        chosen = provider_named(provider or self.default())
        held = self._boxes.get(chosen)
        if held is None:
            held = self._boxes.setdefault(chosen, ClientBox(lambda: self.key(chosen), maker(lambda: chosen, self.claude, self.timeout, self.retries), self.given(chosen), lambda: chosen))
        return held

    def compat(self, provider: str) -> object | None:
        prefix, _, name = provider.partition(':')
        found = FACTORIES.get(prefix)
        if found is None or not name:
            return None
        return found.factory(name, self.timeout, self.retries)

    def get(self, provider: str | None = None) -> object | None:
        chosen = provider or self.default()
        held = self.given(chosen)
        if held is not None:
            if isinstance(self.client, Mapping) or chosen == self.default() or self.keyed(chosen) or getattr(wrap(held), 'provider', chosen) == chosen:
                return held
            return None
        if ':' not in chosen:
            return self.box(chosen).get()
        return self.compat(chosen)

    def capabilities(self, provider: str | None, client: object | None = None) -> frozenset[str]:
        prefix = str(provider or '').partition(':')[0]
        if client is not None and (getattr(client, 'capabilities', None) is not None or prefix not in FACTORIES):
            return capabilities_of(client)
        if prefix in FACTORIES:
            caps = FACTORIES[prefix].capabilities
            return frozenset(caps(provider) if callable(caps) else caps) & CAPABILITIES
        return PROVIDER_CAPABILITIES.get(provider or '', CAPABILITIES)

    def resolve(self, ref: object, role: str, chosen: bool = True) -> tuple[str, str, str | None]:
        from skopax.investigator.pricing import resolve
        return resolve(ref, role, self.default(), self.keyed, chosen)


def clients_for(settings, key: Callable[[], str] | None, claude: Callable[[str], object], timeout: float, retries: int, client: object | None = None,
                keys: Mapping[str, Callable[[], str]] | None = None, default: Callable[[], str] | None = None) -> Clients:
    chosen = default or (lambda: getattr(settings, 'ai_provider', ANTHROPIC))
    found = dict(keys) if keys is not None else {ANTHROPIC: lambda: getattr(settings, 'anthropic_api_key', '') or '', OPENAI: lambda: getattr(settings, 'openai_api_key', '') or ''}
    if key is not None and keys is None:
        given = dict(found)
        found = {p: (lambda p=p: key() if p == provider_named(chosen()) else given[p]()) for p in PROVIDERS}
    return Clients(chosen, found, claude, timeout, retries, client)


def route(obj, field: str, role: str) -> tuple[str, str, str | None]:
    settings = obj.settings
    clients = getattr(obj, 'clients', None)
    ref = getattr(settings, field)
    if clients is None:
        from skopax.investigator.pricing import resolve
        return resolve(ref, role, provider_named(getattr(settings, 'ai_provider', ANTHROPIC)), None, explicit(settings, field))
    return clients.resolve(ref, role, explicit(settings, field))


class ModelSetting:
    def __init__(self, field: str, role: str):
        self.field = field
        self.role = role

    def __set_name__(self, owner, name: str) -> None:
        self.name = name

    def __get__(self, obj, owner=None):
        if obj is None:
            return self
        if self.name in obj.__dict__:
            return obj.__dict__[self.name]
        return route(obj, self.field, self.role)[1]

    def __set__(self, obj, value) -> None:
        obj.__dict__[self.name] = value


def api_errors() -> tuple[type[BaseException], ...]:
    import openai
    return (openai.APIError,)
