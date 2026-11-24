import logging
from collections.abc import Callable, Iterable

from skopax import information, severity
from skopax.engine import ALREADY_ACTIVE, SEV_RANK, parse_iso

log = logging.getLogger('skopax.investigator.select')

ANNOUNCE_WINDOW_S = 6 * 3600
EVENT_WINDOW_S = 6 * 3600
OWN_FINDINGS = ('pagescan', 'nodes', 'slack-posts', 'browser')
ACCESS_KEY = ':access:'
REFIRE_COOLDOWN_S = 72 * 3600
EVENTS = ('new', 'escalated', 'reopened')
MAX_FAILURES = 2
NOT_FAILURES = ('done', 'stopped', 'skipped', 'abandoned')
SEVERITIES = severity.CARD_LEVELS
NO_SERVICE = (None, '', '-')
PLUGGED: list = [()]


def use_own_findings(extra: Iterable[str] | Callable[[], Iterable[str]]) -> None:
    PLUGGED[0] = extra


def own_findings() -> frozenset[str]:
    plugged = PLUGGED[0]
    try:
        extra = frozenset(plugged() if callable(plugged) else plugged)
    except Exception as e:
        log.warning('plug-in own findings not read, using the core ones (%s)', type(e).__name__)
        extra = frozenset()
    return frozenset(OWN_FINDINGS) | extra


def stamps(history: list[dict]) -> dict[str, str]:
    out = {}
    for e in history:
        if e.get('ev') in EVENTS and e.get('key') not in out:
            out[e['key']] = e['t']
    return out


def kinds(history: list[dict]) -> dict[str, str]:
    out = {}
    for e in history:
        if e.get('ev') in EVENTS and e.get('key') not in out:
            out[e['key']] = e['ev']
    return out


def run_window(cooldown_s: float = REFIRE_COOLDOWN_S) -> float:
    return max(EVENT_WINDOW_S, cooldown_s)


def cooling(a: dict, kind: str | None, runs: list[dict], now: float, cooldown_s: float = REFIRE_COOLDOWN_S) -> bool:
    if kind != 'reopened' or a.get('sev') == 'crit' or a.get('backAfterResolve'):
        return False
    done = [r['startedAt'] for r in runs if a['key'] in (r.get('keys') or []) and r.get('outcome') == 'done' and r.get('trigger') != 'slack' and r.get('startedAt')]
    return bool(done) and now - parse_iso(max(done)) < cooldown_s


def merged(a: dict, by_key: dict) -> bool:
    host = by_key.get(a.get('parent'))
    return bool(host and host is not a and information.is_card(host) and host.get('status') == 'open')


def underway(x: dict) -> bool:
    auto, draft = x.get('autoSend') or {}, x.get('slackDraft') or {}
    return bool(draft.get('text')) and not draft.get('notAutoSent') and bool(auto.get('at') or auto.get('claimedAt')) and not (auto.get('failedAt') and auto.get('claimedAt'))


def announced_twin(card: dict, cards: list[dict], now: float) -> dict | None:
    if card.get('svc') in NO_SERVICE:
        return None
    for x in cards:
        if x['key'] == card['key'] or x.get('status') != 'open' or x.get('svc') != card.get('svc'):
            continue
        at = (x.get('slackPost') or {}).get('at') or (x.get('slackDraft') or {}).get('at') if (x.get('slackPost') or {}).get('at') or underway(x) else None
        if at and now - parse_iso(at) < ANNOUNCE_WINDOW_S and SEV_RANK.get(x.get('sev'), 0) >= SEV_RANK.get(card.get('sev'), 0):
            return x
    return None


def announced(card: dict, cards: list[dict], now: float) -> dict | None:
    if (card.get('slackPost') or {}).get('at') or card.get('slackDraft'):
        return None
    twin = announced_twin(card, cards, now)
    return {'key': twin['key'], 'svc': card.get('svc'), 'posted': bool((twin.get('slackPost') or {}).get('ts'))} if twin else None


def eligible(state: dict, mode: str, runs: list[dict], now: float, limit: int, cooldown_s: float = REFIRE_COOLDOWN_S) -> list[dict]:
    by_key = {a['key']: a for a in state.get('anomalies') or []}
    picked = []
    kind = kinds(state.get('history') or [])
    own = own_findings()
    for key, t in stamps(state.get('history') or []).items():
        a = by_key.get(key)
        if not a or a.get('status') != 'open' or a.get('muted') or a.get('sev') not in SEVERITIES or merged(a, by_key) or a.get('src') in own or ACCESS_KEY in str(a.get('ranKey') or ''):
            continue
        if a.get(ALREADY_ACTIVE) or now - parse_iso(t) > EVENT_WINDOW_S:
            continue
        if mode == 'live' and a.get('triage'):
            continue
        tried = [r for r in runs if key in (r.get('keys') or []) and (r.get('startedAt') or '') >= t and r.get('trigger') != 'slack']
        if any(r.get('outcome') == 'done' for r in tried):
            continue
        if sum(1 for r in tried if r.get('outcome') not in NOT_FAILURES) >= MAX_FAILURES:
            continue
        if cooling(a, kind.get(key), runs, now, cooldown_s):
            continue
        picked.append(a)
    picked.sort(key=lambda a: (-SEV_RANK[a['sev']], a.get('firstSeen') or ''))
    return picked[:limit]
