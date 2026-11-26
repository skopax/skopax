import difflib
from collections.abc import Callable, Iterable

NEAR = 10


def closest(wanted: str | Iterable[str], names: Iterable[str], limit: int = NEAR, key: Callable[[str], str] = lambda name: name) -> list[str]:
    asked = [wanted.lower()] if isinstance(wanted, str) else [w.lower() for w in wanted]
    scored = sorted(((max((difflib.SequenceMatcher(None, w, key(n).lower()).ratio() for w in asked), default=0.0), n) for n in dict.fromkeys(names)), key=lambda x: (-x[0], x[1]))
    return [n for _, n in scored[:limit]]


def entries(wanted: str, names: Iterable[str], limit: int = NEAR) -> list[str]:
    listed = list(dict.fromkeys(names))
    folders = [n for n in listed if n.endswith('/')]
    files = [n for n in listed if not n.endswith('/')]
    keep = set(closest(wanted, files, limit, last) + closest(wanted, folders, limit, last))
    return [n for n in closest(wanted, listed, len(listed), last) if n in keep]


def last(name: str) -> str:
    return name.rstrip('/').rpartition('/')[2]
