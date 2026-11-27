import json
import re

from skopax.redact import redact

PATTERN_LIMIT = 90
TIMESTAMP = re.compile(r'^\s*\[?\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:[.,]\d+)?(?:Z|[+-]\d{2}:?\d{2})?\]?\s*')
STAMP_ANY = re.compile(r'\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:[.,]\d+)?(?:Z|[+-]\d{2}:?\d{2})?')
UUID = re.compile(r'\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b')
HEX = re.compile(r'\b(?:0x)?(?=[0-9a-fA-F]*\d)(?=[0-9a-fA-F]*[a-fA-F])[0-9a-fA-F]{8,}\b')
IP = re.compile(r'\b\d{1,3}(?:\.\d{1,3}){3}(?::\d+)?\b')
LONG_NUMBER = re.compile(r'\d{4,}')
LOGFMT_MSG = re.compile(r'(?:^|\s)(?:msg|message)=(?:"((?:[^"\\]|\\.)*)"|(\S+))')
JSON_KEYS = ('msg', 'message', 'error', 'err', 'log')


def duration(seconds: float) -> str:
    s = round(seconds or 0)
    return f'{s} s' if s < 120 else f'{s // 60} min' if s < 7200 else f'{s // 3600} h {s % 3600 // 60} min'


def cut(text: str, limit: int) -> str:
    return redact(' '.join(str(text or '').split()))[:limit]


def flat(text: object) -> str:
    return ' '.join(str(text if text is not None else '').split())


def message_of(line: str) -> str:
    text = line.strip()
    if text.startswith('{'):
        try:
            found = json.loads(text)
        except ValueError:
            found = None
        if isinstance(found, dict):
            for key in JSON_KEYS:
                value = found.get(key)
                if isinstance(value, str) and value.strip():
                    return value
    m = LOGFMT_MSG.search(text)
    if m:
        return (m.group(1) or m.group(2) or '').replace('\\"', '"')
    return text


def normalised(line: str, limit: int = PATTERN_LIMIT) -> str:
    text = TIMESTAMP.sub('', message_of(line))
    text = STAMP_ANY.sub('<time>', text)
    text = UUID.sub('<id>', text)
    text = HEX.sub('<id>', text)
    text = IP.sub('<ip>', text)
    text = LONG_NUMBER.sub('<n>', text)
    return flat(text)[:limit]
