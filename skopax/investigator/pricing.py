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

SEARCH_USD = 0.01
TOKEN_PARTS = ('cacheRead', 'input', 'cacheWrite5m', 'cacheWrite1h', 'output')
PARTS = TOKEN_PARTS + ('webSearch',)
SOURCES = ('pod', 'page', 'slack', 'chat', 'assess', 'gate', 'smalltalk', 'pagescan', 'selfreview', 'mcp')
POD_BY = 'pod'


class UnknownModel(Exception):
    pass


def known(model: str) -> bool:
    return model in PRICES


def _count(usage, name: str) -> int:
    value = usage.get(name) if isinstance(usage, dict) else getattr(usage, name, None)
    return int(value or 0)


def _hour_writes(usage) -> int:
    split = usage.get('cache_creation') if isinstance(usage, dict) else getattr(usage, 'cache_creation', None)
    return _count(split, 'ephemeral_1h_input_tokens') if split is not None else 0


def _searches(usage) -> int:
    used = usage.get('server_tool_use') if isinstance(usage, dict) else getattr(usage, 'server_tool_use', None)
    return _count(used, 'web_search_requests') if used is not None else 0


def split(model: str, usage) -> dict:
    price = PRICES.get(model) or FALLBACK_PRICES.get(model)
    if price is None:
        raise UnknownModel(model)
    hour = _hour_writes(usage)
    tokens = {'cacheRead': _count(usage, 'cache_read_input_tokens'), 'input': _count(usage, 'input_tokens'),
              'cacheWrite5m': _count(usage, 'cache_creation_input_tokens') - hour, 'cacheWrite1h': hour, 'output': _count(usage, 'output_tokens'), 'webSearch': _searches(usage)}
    per_unit = {'cacheRead': price['cache_read_input_tokens'] / 1_000_000, 'input': price['input_tokens'] / 1_000_000, 'cacheWrite5m': price['cache_creation_input_tokens'] / 1_000_000,
                'cacheWrite1h': 2 * price['input_tokens'] / 1_000_000, 'output': price['output_tokens'] / 1_000_000, 'webSearch': SEARCH_USD}
    usd = {p: tokens[p] * per_unit[p] for p in PARTS}
    return {'tokens': tokens, 'usd': usd | {'total': sum(usd[p] for p in PARTS)}}


def usd(model: str, usage) -> float:
    return split(model, usage)['usd']['total']
