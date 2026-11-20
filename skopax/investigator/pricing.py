from skopax.ai import ANTHROPIC, LABELS, OPENAI, PROVIDERS, Usage, neutral

PRICES = {
    'claude-opus-5-5': {'input_tokens': 4.00, 'output_tokens': 20.00, 'cache_read_input_tokens': 0.20, 'cache_creation_input_tokens': 5.00},
    'claude-opus-5': {'input_tokens': 5.00, 'output_tokens': 25.00, 'cache_read_input_tokens': 0.50, 'cache_creation_input_tokens': 6.25},
    'claude-opus-4-8': {'input_tokens': 5.00, 'output_tokens': 25.00, 'cache_read_input_tokens': 0.50, 'cache_creation_input_tokens': 6.25},
    'claude-sonnet-5-5': {'input_tokens': 2.00, 'output_tokens': 10.00, 'cache_read_input_tokens': 0.10, 'cache_creation_input_tokens': 2.50},
    'claude-haiku-5-5': {'input_tokens': 0.10, 'output_tokens': 0.50, 'cache_read_input_tokens': 0.01, 'cache_creation_input_tokens': 0.125},
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

SEARCH_USD = {ANTHROPIC: 0.01, OPENAI: 0.01}
TOKEN_PARTS = ('cacheRead', 'input', 'cacheWrite5m', 'cacheWrite1h', 'output')
PARTS = TOKEN_PARTS + ('webSearch',)
SOURCES = ('pod', 'page', 'slack', 'chat', 'assess', 'gate', 'smalltalk', 'pagescan', 'selfreview', 'mcp')
POD_BY = 'pod'


class UnknownModel(Exception):
    pass


def known(model: str) -> bool:
    return model in PRICES or model in OPENAI_PRICES


def provider_of(model: object) -> str | None:
    return next((p for p, table in TABLES.items() if model in table), ANTHROPIC if model in FALLBACK_PRICES else None)


def models_of(provider: str) -> tuple[str, ...]:
    return tuple(TABLES.get(provider) or PRICES)


def default_model(provider: str, role: str) -> str:
    return (DEFAULTS.get(provider) or DEFAULTS[ANTHROPIC])[role]


def effective(provider: str, model: str, role: str) -> str:
    return model if provider_of(model) in (provider, None) else default_model(provider, role)


def _rates(model: str, used: Usage) -> dict:
    price = PRICES.get(model) or FALLBACK_PRICES.get(model)
    if price is not None:
        return {'cacheRead': price['cache_read_input_tokens'], 'input': price['input_tokens'], 'cacheWrite5m': price['cache_creation_input_tokens'],
                'cacheWrite1h': 2 * price['input_tokens'], 'output': price['output_tokens']}
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
    per_unit = {p: rates[p] / 1_000_000 for p in TOKEN_PARTS} | {'webSearch': SEARCH_USD[provider_of(model) or ANTHROPIC]}
    usd = {p: tokens[p] * per_unit[p] for p in PARTS}
    return {'tokens': tokens, 'usd': usd | {'total': sum(usd[p] for p in PARTS)}}


def usd(model: str, usage) -> float:
    return split(model, usage)['usd']['total']
