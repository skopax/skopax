import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from skopax import locales
from skopax.chattext import TEXTS
from skopax.messages import MESSAGES, outbound as sent_out
from skopax.registry import DAYS, MONTHS

REFERENCE = 'tr'


def outbound() -> list[str]:
    return [key for key in MESSAGES if sent_out(key)]


def source() -> dict:
    reference = locales.read(REFERENCE)
    return {
        'page': sorted(reference.page),
        'rules': [pattern for pattern, _ in reference.rules],
        'tails': [end for end, _ in reference.tails],
        'headers': sorted(reference.headers),
        'months': list(MONTHS),
        'days': list(DAYS),
        'messages': {key: MESSAGES[key] for key in sorted(outbound())},
        'chat': {key: [list(pair) for pair in value] if isinstance(value, tuple) else value for key, value in TEXTS['en'].items()},
    }


def text() -> str:
    return json.dumps(source(), indent=1)


if __name__ == '__main__':
    print(text())
