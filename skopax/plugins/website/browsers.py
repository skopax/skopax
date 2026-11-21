import threading

from skopax.contract import Context, ReadError, Transport
from skopax.kit.browser import Browser

PICK = 'browser'
LEGACY = 'browser-url'
BROWSERS: dict[tuple[str, object], Browser] = {}
BROWSERS_GUARD = threading.Lock()
BROWSERS_MAX = 16


def shared(url: str, transport: Transport) -> Browser:
    key = (url, transport)
    with BROWSERS_GUARD:
        found = BROWSERS.pop(key, None) or Browser(url, transport)
        BROWSERS[key] = found
        while len(BROWSERS) > BROWSERS_MAX:
            BROWSERS.pop(next(iter(BROWSERS)))
        return found


def legacy_url(config) -> str:
    return (config.get(LEGACY) or '').strip()


def peered(ctx: Context) -> tuple[str, Transport] | None:
    try:
        peer = ctx.peer(PICK)
    except ReadError:
        return None
    if peer is None:
        return None
    url = (peer.info.config.get('url') or '').strip()
    return (url, peer.http()) if url else None


def ready(ctx: Context) -> bool:
    return bool(legacy_url(ctx.instance.config)) or peered(ctx) is not None


def browser_of(ctx: Context) -> Browser | None:
    if legacy_url(ctx.instance.config):
        return ctx.client()
    found = peered(ctx)
    return None if found is None else shared(*found)


def has_browser(runtime, instance) -> bool:
    if str(instance.values.get(LEGACY) or '').strip():
        return True
    name = str(instance.values.get(PICK) or '').strip()
    return bool(name) and any(i.name == name for i in runtime.instances_of('browser'))
