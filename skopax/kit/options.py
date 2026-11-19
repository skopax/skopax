import math

import regex

PATTERN_MAX = 200
MATCH_S = 0.05
MATCH_CHARS = 400
SAMPLES = tuple(unit * 60 + '!' for unit in ('a', 'ab', '1', ' ', 'x', 'a ', '.', '/'))


def entries(raw: object) -> list[str]:
    return [p for p in (part.strip() for part in str(raw or '').split(',')) if p]


def pairs(raw: object) -> tuple[dict[str, str], list[str]]:
    found: dict[str, str] = {}
    bad: list[str] = []
    for entry in entries(raw):
        key, sep, value = entry.partition('=')
        key, value = key.strip(), value.strip()
        if sep and key and value and key not in found:
            found[key] = value
        else:
            bad.append(entry)
    return found, bad


def number(text: object) -> float | None:
    try:
        value = float(str(text).strip())
    except (TypeError, ValueError, OverflowError):
        return None
    return value if math.isfinite(value) and value > 0 else None


def compiled(raw: object) -> 'regex.Pattern | None':
    text = str(raw or '').strip()
    if not text or len(text) > PATTERN_MAX:
        return None
    try:
        return regex.compile(text)
    except (regex.error, RecursionError, OverflowError, ValueError):
        return None


class Guard:
    def __init__(self, pattern: 'regex.Pattern'):
        self.pattern = pattern
        self.timed_out = False

    @property
    def groupindex(self):
        return self.pattern.groupindex

    def search(self, text: object) -> 'regex.Match | None':
        if self.timed_out:
            return None
        try:
            return self.pattern.search(str(text or '')[:MATCH_CHARS], timeout=MATCH_S)
        except TimeoutError:
            self.timed_out = True
            return None


def guarded(raw: object) -> 'Guard | None':
    found = compiled(raw)
    return None if found is None else Guard(found)


def search(pattern: 'regex.Pattern | Guard', text: object) -> 'regex.Match | None':
    if isinstance(pattern, Guard):
        return pattern.search(text)
    try:
        return pattern.search(str(text or '')[:MATCH_CHARS], timeout=MATCH_S)
    except TimeoutError:
        return None


def slow(pattern: 'regex.Pattern') -> bool:
    for sample in SAMPLES:
        try:
            pattern.search(sample, timeout=MATCH_S)
        except TimeoutError:
            return True
    return False
