import fnmatch
import re

from skopax import severity

HIGH_CHECKS = frozenset({('prom', 'err'), ('prom', '5xx'), ('prom', 'gw-err'), ('prom', 'ing-5xx'), ('prom', 'traffic-drop'), ('apm', 'apm-fail'), ('ghdeploy', 'github-deploy'),
                         ('langfuse', 'lf-traffic'), ('langfuse', 'lf-errors'), ('umami', 'umami-redirect'), ('umami', 'umami-pageviews'), ('crashlytics', 'crash-rate'),
                         ('cf', 'edge-conn'), ('cf', 'edge-5xx'), ('gads', 'ad-link')})
LATENCY_CHECKS = frozenset({('prom', 'avg-latency'), ('prom', 'p95-route'), ('prom', 'p95-ingress')})
ALERT_SOURCES = ('alert', 'grafana')
LOG_SOURCES = ('logs', 'loki')
REQUESTS = re.compile(r'over (\d+) requests')


def number(value: object) -> float:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else 0.0


def alert_name(card: dict) -> str:
    name = card.get('alertName') or card.get('sub')
    if isinstance(name, str) and name:
        return name
    parts = str(card.get('key') or '').split('|')
    return parts[1] if len(parts) > 1 else ''


def alert_level(card: dict, kept: tuple[str, ...]) -> str:
    label = card.get('alertLabel')
    label = label if isinstance(label, str) and label else severity.ALERT_WARNING[0]
    sev = severity.from_label(label)
    name = alert_name(card)
    if sev == severity.HIGH and label in severity.ALERT_WARNING and (card.get('keptWarning') or any(fnmatch.fnmatchcase(name, p) for p in kept)):
        return severity.WARN
    return sev


def requests_of(card: dict) -> float | None:
    if isinstance(card.get('requests'), (int, float)):
        return float(card['requests'])
    found = REQUESTS.search(str(card.get('what') or ''))
    return float(found.group(1)) if found else None


def current(card: dict, kept: tuple[str, ...] = ()) -> str:
    src, sig = card.get('src'), card.get('sig')
    if sig == 'alert' and src in ALERT_SOURCES:
        return alert_level(card, kept)
    if (src, sig) in HIGH_CHECKS or (src == 'pagescan' and card.get('gone')):
        return severity.HIGH
    if src in LOG_SOURCES and sig == 'log':
        level = (str(card.get('key') or '').split('|') + ['', '', ''])[2]
        if level == 'ERROR':
            return severity.HIGH
        if level == 'WARN':
            return severity.warn_log(number(card.get('now')), number(card.get('d1')), number(card.get('d7')))
    if (src, sig) in LATENCY_CHECKS and card.get('unit') == 'ms':
        bases = [number(card.get(d)) for d in ('d1', 'd7') if card.get(d) is not None]
        return severity.latency(number(card.get('now')), max(bases) if bases else None, requests_of(card))
    return severity.WARN
