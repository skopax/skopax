from skopax.kit.flags import ESCAPE, EXCERPT, MAX_FLAGS, PHRASES, PLANTED, REQUESTS, SIMPLE, SOFT, asked, scan, unescaped
from skopax.redact import redact_secrets

__all__ = ['ESCAPE', 'EXCERPT', 'MAX_FLAGS', 'PHRASES', 'PLANTED', 'REQUESTS', 'SIMPLE', 'SOFT', 'asked', 'masked_asked', 'masked_scan', 'safe_excerpt', 'scan', 'unescaped']

EXCERPT_MARGIN = 200


def safe_excerpt(flag: dict, text: str) -> str:
    plain = unescaped(text)
    pattern = dict(PHRASES).get(flag.get('phrase'))
    found = pattern.search(plain) if pattern is not None else None
    if found is None:
        return redact_secrets(flag.get('excerpt') or '')
    window = redact_secrets(plain[max(0, found.start() - EXCERPT_MARGIN):found.end() + EXCERPT_MARGIN])
    again = pattern.search(window)
    at = max(0, (again.start() if again else 0) - 40)
    return window[at:at + EXCERPT]


def masked_scan(tool: str, text: str, skip: tuple[str, ...] = ()) -> list[dict]:
    return [f | {'excerpt': safe_excerpt(f, text)} for f in scan(tool, text, skip)]


def masked_asked(tool: str, text: str) -> list[dict]:
    return masked_scan(tool, text, PLANTED)
