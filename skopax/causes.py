import datetime as dt

EVENT = 'event'
RECURRING = 'recurring'
KINDS = (EVENT, RECURRING)
WHAT_LIMIT = 120
EVENT_EXTRA_HOURS = 6


def problem(cause) -> str | None:
    if not isinstance(cause, dict):
        return 'cause must be an object'
    if cause.get('kind') not in KINDS:
        return 'cause.kind must be one of ' + ', '.join(KINDS)
    what = cause.get('what')
    if not isinstance(what, str) or not what.strip():
        return 'cause.what is required: the trigger in a few words'
    if len(what) > WHAT_LIMIT:
        return f'cause.what must be at most {WHAT_LIMIT} characters, got {len(what)}'
    at = cause.get('at')
    if at is not None and not full_time(at):
        return 'cause.at must be a full ISO time with its zone, such as 2026-10-26T07:00:00Z'
    return None


def full_time(at) -> bool:
    text = str(at)
    try:
        parsed = dt.datetime.fromisoformat(text.replace('Z', '+00:00'))
    except ValueError:
        return False
    return 'T' in text and parsed.tzinfo is not None


def recorded(cause: dict, first_seen) -> dict:
    out = {'kind': cause['kind'], 'what': ' '.join(cause['what'].split())}
    if cause.get('at') is not None:
        out['at'] = str(cause['at'])
    return out | {'firstSeen': first_seen}


def own(card: dict) -> dict | None:
    cause = card.get('cause')
    if not isinstance(cause, dict) or cause.get('kind') not in KINDS or not cause.get('what'):
        return None
    return cause if card.get('firstSeen') and cause.get('firstSeen') == card.get('firstSeen') else None


def cause_of(card: dict, by_key: dict) -> dict | None:
    found = own(card)
    if found is not None:
        return found
    parent = by_key.get(card.get('parent'))
    return own(parent) if parent is not None and parent is not card else None


def event(card: dict, by_key: dict) -> bool:
    return (cause_of(card, by_key) or {}).get('kind') == EVENT
