from collections.abc import Callable

from skopax.ai import ANTHROPIC, LABELS, OPENAI, PROVIDERS, Usage, neutral, provider_of_ref

PRICES = {
    'claude-opus-5-5': {'input_tokens': 4.00, 'output_tokens': 20.00, 'cache_read_input_tokens': 0.20, 'cache_creation_input_tokens': 5.00},
    'claude-opus-5': {'input_tokens': 5.00, 'output_tokens': 25.00, 'cache_read_input_tokens': 0.50, 'cache_creation_input_tokens': 6.25},
    'claude-opus-4-8': {'input_tokens': 5.00, 'output_tokens': 25.00, 'cache_read_input_tokens': 0.50, 'cache_creation_input_tokens': 6.25},
    'claude-sonnet-5-5': {'input_tokens': 2.00, 'output_tokens': 10.00, 'cache_read_input_tokens': 0.10, 'cache_creation_input_tokens': 2.50},
    'claude-haiku-5-5': {'input_tokens': 0.10, 'output_tokens': 0.50, 'cache_read_input_tokens': 0.01, 'cache_creation_input_tokens': 0.125},
    'claude-fable-5-2': {'input_tokens': 8.00, 'output_tokens': 40.00, 'cache_read_input_tokens': 0.20, 'cache_creation_input_tokens': 10.00},
    'claude-mythos-5-2': {'input_tokens': 8.00, 'output_tokens': 40.00, 'cache_read_input_tokens': 0.20, 'cache_creation_input_tokens': 10.00},
}
FALLBACK_PRICES = {
    'claude-sonnet-5': {'input_tokens': 2.00, 'output_tokens': 10.00, 'cache_read_input_tokens': 0.20, 'cache_creation_input_tokens': 2.50},
}
LONG_INPUT = 272_000
OPENAI_PRICES = {
    'gpt-6.2-astra': {'input_tokens': 8.00, 'output_tokens': 40.00, 'cache_read_input_tokens': 0.40, 'cache_creation_input_tokens': 10.00, 'cache_write_long': 20.00,
                      'long': {'input_tokens': 16.00, 'output_tokens': 60.00, 'cache_read_input_tokens': 0.80, 'cache_creation_input_tokens': 20.00, 'cache_write_long': 40.00}},
    'gpt-6.2-sol': {'input_tokens': 2.00, 'output_tokens': 10.00, 'cache_read_input_tokens': 0.10, 'cache_creation_input_tokens': 2.50, 'cache_write_long': 5.00,
                    'long': {'input_tokens': 4.00, 'output_tokens': 15.00, 'cache_read_input_tokens': 0.20, 'cache_creation_input_tokens': 5.00, 'cache_write_long': 10.00}},
    'gpt-6.1-terra': {'input_tokens': 1.00, 'output_tokens': 5.00, 'cache_read_input_tokens': 0.05, 'cache_creation_input_tokens': 1.25, 'cache_write_long': 2.50,
                      'long': {'input_tokens': 2.00, 'output_tokens': 7.50, 'cache_read_input_tokens': 0.10, 'cache_creation_input_tokens': 2.50, 'cache_write_long': 5.00}},
    'gpt-6-luna': {'input_tokens': 0.10, 'output_tokens': 0.50, 'cache_read_input_tokens': 0.01, 'cache_creation_input_tokens': 0.125, 'cache_write_long': 0.25,
                   'long': {'input_tokens': 0.20, 'output_tokens': 0.75, 'cache_read_input_tokens': 0.02, 'cache_creation_input_tokens': 0.25, 'cache_write_long': 0.50}},
}
TABLES = {ANTHROPIC: PRICES, OPENAI: OPENAI_PRICES}
ALL_MODELS = tuple(PRICES) + tuple(OPENAI_PRICES)
DEFAULTS = {
    ANTHROPIC: {'card': 'claude-opus-5-5', 'slack': 'claude-sonnet-5-5', 'chat': 'claude-sonnet-5-5', 'gate': 'claude-haiku-5-5', 'review': 'claude-sonnet-5-5', 'plugin': 'claude-opus-5-5'},
    OPENAI: {'card': 'gpt-6.2-astra', 'slack': 'gpt-6.2-sol', 'chat': 'gpt-6.2-sol', 'gate': 'gpt-6-luna', 'review': 'gpt-6.2-sol', 'plugin': 'gpt-6.2-astra'},
}

SEARCH_USD = {ANTHROPIC: 0.01, OPENAI: 0.01, 'vertex': 0.01}
VERTEX_CACHE_READ, VERTEX_CACHE_WRITE_5M, VERTEX_CACHE_WRITE_1H = 0.1, 1.25, 2.0
TOKEN_PARTS = ('cacheRead', 'input', 'cacheWrite5m', 'cacheWrite1h', 'output')
PARTS = TOKEN_PARTS + ('webSearch',)
SOURCES = ('pod', 'page', 'slack', 'chat', 'assess', 'gate', 'smalltalk', 'pagescan', 'selfreview', 'threadsummary', 'mcp')
POD_BY = 'pod'


COMPAT: list = []
VERTEX: list = []


class UnknownModel(Exception):
    pass


def vertex_prices() -> dict:
    return VERTEX[0]() if VERTEX else {}


def compat_prices() -> dict:
    return (COMPAT[0]() if COMPAT else {}) | vertex_prices()


def known(model: str) -> bool:
    return model in PRICES or model in OPENAI_PRICES or model in compat_prices()


def provider_of(model: object) -> str | None:
    found = next((p for p, table in TABLES.items() if model in table), ANTHROPIC if model in FALLBACK_PRICES else None)
    if found is None and isinstance(model, str) and model in vertex_prices():
        return 'vertex:' + model.split('/', 1)[0]
    if found is None and isinstance(model, str) and model in compat_prices():
        return 'compat:' + model.split('/', 1)[0]
    return found


def models_of(provider: str) -> tuple[str, ...]:
    if provider.startswith(('compat:', 'vertex:')):
        return tuple(m for m in compat_prices() if provider_of(m) == provider)
    return tuple(TABLES.get(provider) or PRICES)


def priced_models(provider: str) -> tuple[str, ...]:
    return tuple(TABLES.get(provider) or ()) or ALL_MODELS + tuple(m for m, p in compat_prices().items() if p is not None)


def default_model(provider: str, role: str) -> str:
    return (DEFAULTS.get(provider) or DEFAULTS[ANTHROPIC])[role]


def resolve(ref: object, role: str, default: str = ANTHROPIC, keyed: Callable[[str], bool] | None = None, chosen: bool = True) -> tuple[str, str, str | None]:
    if not isinstance(ref, str) or not ref:
        return default, '' if chosen else default_model(default, role), None
    owner = provider_of_ref(ref)
    if owner in (default, None):
        return default, ref, None
    if not chosen:
        return default, default_model(default, role), None
    return owner, ref, None


def in_hand(provider: str, model: str, role: str) -> str:
    return model if provider_of_ref(model) in (provider, None) else default_model(provider, role)


def price_of(model: str) -> dict | None:
    price = PRICES.get(model) or FALLBACK_PRICES.get(model) or OPENAI_PRICES.get(model) or compat_prices().get(model)
    return None if price is None else {'input': price['input_tokens'], 'output': price['output_tokens']}


def _rates(model: str, used: Usage) -> dict:
    price = PRICES.get(model) or FALLBACK_PRICES.get(model)
    if price is not None:
        return {'cacheRead': price['cache_read_input_tokens'], 'input': price['input_tokens'], 'cacheWrite5m': price['cache_creation_input_tokens'],
                'cacheWrite1h': 2 * price['input_tokens'], 'output': price['output_tokens']}
    vertex = vertex_prices()
    if model in vertex:
        price = vertex[model]
        if price is None:
            raise UnknownModel(model)
        return {'cacheRead': price['input_tokens'] * VERTEX_CACHE_READ, 'input': price['input_tokens'], 'cacheWrite5m': price['input_tokens'] * VERTEX_CACHE_WRITE_5M,
                'cacheWrite1h': price['input_tokens'] * VERTEX_CACHE_WRITE_1H, 'output': price['output_tokens']}
    compat = compat_prices()
    if model in compat:
        price = compat[model]
        if price is None:
            raise UnknownModel(model)
        return {'cacheRead': price['input_tokens'], 'input': price['input_tokens'], 'cacheWrite5m': price['input_tokens'], 'cacheWrite1h': price['input_tokens'], 'output': price['output_tokens']}
    price = OPENAI_PRICES.get(model)
    if price is None:
        raise UnknownModel(model)
    if used.input + used.cache_read + used.cache_write + used.cache_write_long > LONG_INPUT:
        price = price['long']
    return {'cacheRead': price['cache_read_input_tokens'], 'input': price['input_tokens'], 'cacheWrite5m': price['cache_creation_input_tokens'],
            'cacheWrite1h': price['cache_write_long'], 'output': price['output_tokens']}


def split(model: str, usage) -> dict:
    used = neutral(usage)
    rates = _rates(model, used)
    tokens = {'cacheRead': used.cache_read, 'input': used.input, 'cacheWrite5m': used.cache_write, 'cacheWrite1h': used.cache_write_long, 'output': used.output, 'webSearch': used.web_searches}
    per_unit = {p: rates[p] / 1_000_000 for p in TOKEN_PARTS} | {'webSearch': SEARCH_USD.get((provider_of(model) or ANTHROPIC).partition(':')[0], 0.0)}
    usd = {p: tokens[p] * per_unit[p] for p in PARTS}
    return {'tokens': tokens, 'usd': usd | {'total': sum(usd[p] for p in PARTS)}}


def usd(model: str, usage) -> float:
    return split(model, usage)['usd']['total']


def charge(model: str, provider: str | None, usage) -> tuple[float, str | None]:
    try:
        return usd(model, usage), None
    except UnknownModel:
        priced = max(priced_models(provider or ''), key=lambda m: usd(m, usage))
        return usd(priced, usage), priced
