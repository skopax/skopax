import re
from urllib.parse import quote, unquote, urlsplit


def card_url(base_url: str, key: str, num: int | None = None) -> str:
    base = base_url.rstrip('/')
    return f'{base}/c/{num}' if num else f"{base}/#a={quote(key, safe='')}"


def chat_url(base: str, key: str, num: int | None = None) -> str:
    return f"{base.rstrip('/')}/#t=chat/new/{num if num else quote(key, safe='')}"


def card_link_pattern(base_url: str) -> re.Pattern:
    netloc = urlsplit(base_url if '://' in base_url else '//' + base_url).netloc
    head = r'(?<![\w.-])(?:https?://)?' + re.escape(netloc) if netloc else ''
    return re.compile(head + r'/(?:#a=(?P<key>[^\s"\\<>)\]]+)|c/(?P<num>\d+))')


def linked(text: str, base_url: str = '') -> tuple[set[str], set[int]]:
    keys, nums = set(), set()
    for m in card_link_pattern(base_url).finditer(text):
        if m.group('key'):
            keys.add(unquote(m.group('key')))
        else:
            nums.add(int(m.group('num')))
    return keys, nums
