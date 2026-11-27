import logging
from collections.abc import Mapping

from skopax.errkind import kind as error_kind

log = logging.getLogger('skopax.semantic')

FIELD = 'index-pages'


def index_on(values: Mapping[str, str]) -> bool:
    return str(values.get(FIELD) or '').strip().lower() == 'true'


def spaces_of(plugin, config: Mapping[str, str]) -> tuple[str, ...] | None:
    corpus = getattr(plugin, 'corpus', None)
    if corpus is None:
        return ()
    if corpus.spaces is None:
        return None
    try:
        return tuple(sorted({str(s).strip().upper() for s in corpus.spaces(config) if str(s).strip()}))
    except Exception as e:
        log.warning('docs spaces unreadable for %s: %s', plugin.id, error_kind(e))
        return ()


def allowed(spaces: tuple[str, ...] | None, space: object) -> bool:
    shown = str(space or '').strip()
    if spaces is None:
        return not shown
    return shown.upper() in spaces
