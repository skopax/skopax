import re
from urllib.parse import urlsplit

LINK_MAX = 2000
UNSENDABLE = re.compile(r'[^\x21-\x7e]')


def sendable(url: object) -> bool:
    text = str(url or '').strip()
    if not text or len(text) > LINK_MAX or UNSENDABLE.search(text):
        return False
    try:
        parts = urlsplit(text)
        parts.port
    except ValueError:
        return False
    return parts.scheme == 'https' and bool(parts.hostname) and '@' not in parts.netloc
