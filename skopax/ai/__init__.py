from collections.abc import Callable
from dataclasses import dataclass, field

ANTHROPIC = 'anthropic'
OPENAI = 'openai'
PROVIDERS = (ANTHROPIC, OPENAI)
LABELS = {ANTHROPIC: 'Anthropic', OPENAI: 'OpenAI'}
SEARCH_TYPE = 'web_search_20250305'
FETCH_TYPE = 'web_fetch_20250910'
THINK_OFF = 'off'
THINK_AUTO = 'auto'


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


def key_for(settings, provider: Callable[[], str]) -> Callable[[], str]:
    return lambda: settings.openai_api_key if provider_named(provider()) == OPENAI else settings.anthropic_api_key


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
        from skopax.investigator.pricing import effective
        return effective(provider_named(getattr(obj.settings, 'ai_provider', ANTHROPIC)), getattr(obj.settings, self.field), self.role)

    def __set__(self, obj, value) -> None:
        obj.__dict__[self.name] = value


def api_errors() -> tuple[type[BaseException], ...]:
    import openai
    return (openai.APIError,)
