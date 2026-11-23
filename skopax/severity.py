CRIT = 'crit'
HIGH = 'high'
WARN = 'warn'
INFO = 'info'
LEVELS = (CRIT, HIGH, WARN, INFO)
CARD_LEVELS = (CRIT, HIGH, WARN)
RANK = {INFO: 0, WARN: 1, HIGH: 2, CRIT: 3}
NAMES = {CRIT: 'Critical', HIGH: 'High', WARN: 'Warning', INFO: 'Info'}
WORDS = {CRIT: 'critical', HIGH: 'high', WARN: 'warning'}
ALERT_LABELS = {'critical': CRIT, 'crit': CRIT, 'warning': HIGH, 'warn': HIGH, 'high': HIGH, 'major': HIGH, 'error': HIGH, 'low': WARN, 'minor': WARN}
ALERT_WARNING = ('warning', 'warn')


def rank(sev: object) -> int:
    return RANK.get(sev, -1) if isinstance(sev, str) else -1


def worst(levels) -> str | None:
    return max((s for s in levels if s in RANK), key=RANK.__getitem__, default=None)


def higher(a: object, b: object) -> bool:
    return rank(a) > rank(b)


def up(sev: str) -> str:
    return {WARN: HIGH, HIGH: CRIT}.get(sev, sev)


def from_label(label: object, default: str = INFO) -> str:
    return ALERT_LABELS.get(str(label or '').strip().lower(), default)


WARN_LOG_NEW_MIN = 100
WARN_LOG_GROWTH_TIMES = 10
WARN_LOG_GROWTH_MIN = 50
LATENCY_TIMES = 3
LATENCY_MIN_REQUESTS = 500
LATENCY_EPSILON = 1e-9


def warn_log(now: float, d1: float, d7: float) -> str:
    bases = [b for b in (d1, d7) if b]
    new = not bases and now >= WARN_LOG_NEW_MIN
    grown = now >= WARN_LOG_GROWTH_MIN and any(now >= WARN_LOG_GROWTH_TIMES * b for b in bases)
    return HIGH if new or grown else WARN


def latency(now: float, base: float | None, requests: float | None) -> str:
    return HIGH if base and requests is not None and requests >= LATENCY_MIN_REQUESTS and now >= LATENCY_TIMES * base * (1 - LATENCY_EPSILON) else WARN
