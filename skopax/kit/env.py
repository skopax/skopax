import logging
import threading
from collections.abc import Mapping

from skopax.messages import msg

log = logging.getLogger('skopax.kit.env')
WARNED: set[tuple[str, tuple[str, ...]]] = set()
GUARD = threading.Lock()


def env_set(env: Mapping[str, str], plugin: str, needed: tuple[str, ...], optional: tuple[str, ...] = ()) -> dict[str, str] | None:
    values = {name: str(env.get(name) or '').strip() for name in needed + optional}
    missing = tuple(name for name in needed if not values[name])
    if not missing:
        return values
    if any(values[name] for name in needed):
        with GUARD:
            fresh = (plugin, missing) not in WARNED
            WARNED.add((plugin, missing))
        if fresh:
            log.warning('%s', msg('settings.env.partial', plugin=plugin, missing=', '.join(missing)))
    return None
