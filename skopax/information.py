import datetime as dt

from skopax import severity

CARD_SEVS = severity.CARD_LEVELS
IN_FLIGHT = ('progress', 'handled')
SYSTEM_TRIAGE = ('pod', 'auto')
ENDED_KEEP_S = 24 * 3600
TEXT_MAX = 200
ROW_FIELDS = ('key', 'svc', 'label', 'src', 'firstSeen', 'lastSeen')


def stamp(text) -> float:
    try:
        return dt.datetime.fromisoformat(str(text).replace('Z', '+00:00')).timestamp()
    except (TypeError, ValueError):
        return 0.0


def in_flight(a: dict) -> bool:
    by_person = a.get('triage') in IN_FLIGHT and a.get('triagedBy') not in SYSTEM_TRIAGE
    return bool(a.get('owner') or a.get('assigned') or a.get('ticket') or by_person)


def is_card(a: dict | None) -> bool:
    a = a or {}
    return a.get('sev') in CARD_SEVS or in_flight(a)


def calm(a: dict | None) -> bool:
    return (a or {}).get('sev') in ('warn', 'info')


def is_item(a: dict | None) -> bool:
    return isinstance(a, dict) and not is_card(a)


def cards_only(items) -> list[dict]:
    return [a for a in items or [] if isinstance(a, dict) and is_card(a)]


def moved_by_hand(a: dict) -> dict | None:
    hand = a.get('sevSet')
    if isinstance(hand, dict) and hand.get('sev') == 'info' and (a.get('sevAuto') or hand.get('auto')) in CARD_SEVS:
        return {'by': hand.get('by'), 'at': hand.get('at')}
    return None


def short_text(a: dict) -> str:
    text = ' '.join(str(a.get('what') or a.get('short') or '').split())
    return text if len(text) <= TEXT_MAX else text[:TEXT_MAX - 1].rstrip() + '\u2026'


def row(a: dict) -> dict:
    firing = a.get('status') == 'open'
    out = {k: a[k] for k in ROW_FIELDS if a.get(k) not in (None, '')} | {'text': short_text(a), 'firing': firing}
    if a.get('muted'):
        out['muted'] = True
    if not firing:
        out['endedAt'] = a.get('clearedAt')
    moved = moved_by_hand(a)
    if moved:
        out['moved'] = moved
    return out


def rows(items, now: float) -> list[dict]:
    out = []
    for a in items or []:
        if not is_item(a):
            continue
        if a.get('status') != 'open' and (not a.get('clearedAt') or now - stamp(a['clearedAt']) >= ENDED_KEEP_S):
            continue
        out.append(row(a))
    out.sort(key=lambda r: (not r['firing'], -stamp(r.get('firstSeen') if r['firing'] else r.get('endedAt')), r['key']))
    return out


def host(a: dict, by_key: dict) -> dict | None:
    p = by_key.get(a.get('parent')) if a.get('parent') and a.get('parent') != a.get('key') else None
    return p if p is not None and is_card(p) else None


def under_information(a: dict, by_key: dict) -> bool:
    p = by_key.get(a.get('parent')) if a.get('parent') and a.get('parent') != a.get('key') else None
    return p is not None and is_item(p)


def page_state(state: dict, now: float) -> dict:
    items = state.get('anomalies') or []
    by_key = {a.get('key'): a for a in items if isinstance(a, dict)}
    cards = [{k: v for k, v in a.items() if k != 'parent'} if under_information(a, by_key) else a for a in cards_only(items)]
    card_keys = {a.get('key') for a in cards}
    info_keys = {a.get('key') for a in items if is_item(a)}
    history = [e for e in state.get('history') or [] if e.get('key') in card_keys or (e.get('key') not in info_keys and e.get('sev') in CARD_SEVS)]
    return {**state, 'anomalies': cards, 'history': history, 'information': rows(items, now)}


def regrouped(a, by_key: dict):
    old = by_key.get(a.get('key')) if isinstance(a, dict) else None
    if old is None or 'parent' in a or not under_information(old, by_key):
        return a
    return {**a, 'parent': old['parent']}


def written_back(incoming: dict, stored: dict) -> dict:
    out = {k: v for k, v in incoming.items() if k != 'information'}
    by_key = {a.get('key'): a for a in stored.get('anomalies') or [] if isinstance(a, dict)}
    out['anomalies'] = [regrouped(a, by_key) for a in out.get('anomalies') or []]
    sent = {a.get('key') for a in out['anomalies'] if isinstance(a, dict)}
    kept = [a for a in stored.get('anomalies') or [] if is_item(a) and a.get('key') not in sent]
    if kept:
        out['anomalies'] = list(out.get('anomalies') or []) + kept
    shown = page_state(stored, 0)['history']
    hidden = [e for e in stored.get('history') or [] if e not in shown]
    if hidden:
        given = list(out.get('history') or [])
        out['history'] = sorted(given + [e for e in hidden if e not in given], key=lambda e: str(e.get('t') or ''), reverse=True)
    return out
