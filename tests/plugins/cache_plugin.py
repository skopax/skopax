from skopax.contract import Check, Field, PassResult, Sweep
from tests.plugins.base import minimal


def tick(ctx) -> Sweep:
    return Sweep(PassResult('tick', True, 'ok', [], {}, ('tick',)))


def tick_cache(ctx) -> Sweep:
    return Sweep(PassResult('tick-cache', True, 'ok', [], {}, ('tick-cache',)))


TICK = Check('tick', 'Tick', 900, 0, ('tick',), tick)
TICK_CACHE = Check('tick-cache', 'Tick cache', 900, 0, ('tick-cache',), tick_cache)
PLUGIN = minimal('cache-demo', 'Cache demo', fields=(Field('url', 'URL', 'url'),), checks=(TICK, TICK_CACHE))
