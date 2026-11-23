from skopax import clock
from skopax.contract import anomaly
from skopax.kit.releases import attach_publish
from skopax.kit.umami import PERMISSION, WEBSITE, WHO, Umami, numbers, pairs, span, standing
from skopax.monitor import below_both, pct

CHECK = 'umami'
ACCESS_PART = 'website'
ACCESS_RAN = f'{CHECK}:access:{ACCESS_PART}'
PAGEVIEWS_RAN = f'{CHECK}:pageviews'
EVENTS_RAN = f'{CHECK}:events'
NO_DATA_RAN = f'{CHECK}:no-data'
HOUR = 3600
DAY = 86400
TYPES_LIMIT = 50
BY_LIMIT = 6
BUDGET_S = 200
BOTH = 'the same window 1 day and 7 days earlier'
RULES = {'redirect_warn': 0.5, 'redirect_crit': 0.2, 'redirect_floor': 50, 'redirect_crit_floor': 100, 'type_warn': 0.4, 'type_floor': 30,
         'pageview_warn': 0.5, 'pageview_floor': 500}
RULE_HELP = {
    'redirect_warn': f'High when the events named in the event field fall below this share of {BOTH} (0.5 = under half of both).',
    'redirect_crit': 'Critical when those events fall below this share of both earlier windows (0.2 = under a fifth).',
    'redirect_floor': 'The warning is checked only when both earlier windows had at least this many of those events, so quiet hours raise nothing.',
    'redirect_crit_floor': 'The critical level is checked only when both earlier windows had at least this many of those events.',
    'type_warn': 'High when the events with one value of the first listed property (one plan, one product type) fall below this share of both earlier windows.',
    'type_floor': 'One value is checked only when both earlier windows had at least this many events with it.',
    'pageview_warn': 'High when page views fall below this share of both earlier windows.',
    'pageview_floor': 'Page views are checked only when both earlier windows had at least this many.',
}
COUNTED = 'Umami counts only the visitors its tracker records'
GTM_WINDOW_S = 48 * HOUR


def hour_counts(um: Umami, end: float, event: str, prop: str) -> tuple[int, dict[str, int]]:
    window = span(end - HOUR, end)
    pageviews = numbers(um.get('stats', window))['pageviews']
    if not (event and prop):
        return pageviews, {}
    return pageviews, dict(pairs(um.get('event-data/values', window | {'eventName': event, 'propertyName': prop}), 'value', 'total', TYPES_LIMIT, 'event data'))


def card(sig: str, sub: str, sev: str, count: float, d1: float, d7: float, label: str, what: str, short: str, site: str, website: str, part: str) -> dict:
    x = anomaly(CHECK, sig, '-', site, sub, sev, count, d1, d7, '', label, what, max(d1, d7), short)
    x['ranKey'] = part
    x['siteId'] = website
    return x


def evaluate(counts: dict[str, tuple[int, dict[str, int]]], now: float, rules: dict, site: str, website: str, event: str, prop: str) -> list[dict]:
    (pv, types), (pv1, types1), (pv7, types7) = counts['now'], counts['1d'], counts['7d']
    until = f'{clock.date(now)} {clock.label(now)}'
    found = []
    total, total1, total7 = sum(types.values()), sum(types1.values()), sum(types7.values())
    sev = ('crit' if below_both(total, total1, total7, rules['redirect_crit'], rules['redirect_crit_floor'])
           else 'high' if below_both(total, total1, total7, rules['redirect_warn'], rules['redirect_floor']) else None)
    if sev:
        by_value = ', '.join(f'{t} {types.get(t, 0)} (1d {n1} / 7d {types7.get(t, 0)})' for t, n1 in sorted(types1.items(), key=lambda kv: -kv[1])[:BY_LIMIT])
        found.append(card('umami-redirect', event, sev, total, total1, total7, f'{event} events down',
                          f'Umami counted {total} {event} events in the hour to {until}, {pct(total, total1)} of the same hour yesterday ({total1}) '
                          f'and {pct(total, total7)} of a week ago ({total7}). By {prop}: {by_value}. {COUNTED}; pageviews in the same hour: {pv} (1d {pv1} / 7d {pv7}).',
                          f'{sev.upper()} {site} {event} events {total} in the last hour (1d {total1} / 7d {total7})', site, website, EVENTS_RAN))
    for t in sorted(set(types1) | set(types7)):
        n, n1, n7 = types.get(t, 0), types1.get(t, 0), types7.get(t, 0)
        if below_both(n, n1, n7, rules['type_warn'], rules['type_floor']):
            found.append(card('umami-redirect', f'{event}:{t}', 'high', n, n1, n7, f'{t} {event} events down',
                              f'Umami counted {n} {event} events with {prop} {t} in the hour to {until}, {pct(n, n1)} of the same hour yesterday ({n1}) and {pct(n, n7)} '
                              f'of a week ago ({n7}); all {event} events in the hour: {total} (1d {total1} / 7d {total7}). {COUNTED}.',
                              f'HIGH {site} {event} {prop} {t} {n} in the last hour (1d {n1} / 7d {n7})', site, website, EVENTS_RAN))
    if below_both(pv, pv1, pv7, rules['pageview_warn'], rules['pageview_floor']):
        found.append(card('umami-pageviews', 'pageviews', 'high', pv, pv1, pv7, 'pageviews down',
                          f'Umami counted {pv} pageviews in the hour to {until}, {pct(pv, pv1)} of the same hour yesterday ({pv1}) and {pct(pv, pv7)} of a week ago ({pv7}). '
                          'A drop this size with the site up usually means the tracker stopped loading (its script route, a consent banner or a site release); '
                          f'{COUNTED}.',
                          f'HIGH {site} Umami pageviews {pv} in the last hour (1d {pv1} / 7d {pv7})', site, website, PAGEVIEWS_RAN))
    return found


def run_pass(um: Umami, now: float, rules: dict, site: str, event: str, prop: str) -> tuple[list[dict], tuple[str, ...], list[str]]:
    on = bool(event and prop)
    counts = {'now': hour_counts(um, now, event, prop), '1d': hour_counts(um, now - DAY, event, prop), '7d': hour_counts(um, now - 7 * DAY, event, prop)}
    parts = {'pageviews': (PAGEVIEWS_RAN, any(pv for pv, _ in counts.values()))}
    if on:
        parts[f'{event} events'] = (EVENTS_RAN, any(types for _, types in counts.values()))
    empty = [name for name, (_, read) in parts.items() if not read]
    off = () if on else (EVENTS_RAN,)
    if len(empty) == len(parts):
        return [], (NO_DATA_RAN,) + off + (ACCESS_RAN,), empty
    ran = ((CHECK,) if not empty else ()) + tuple(key for key, read in parts.values() if read) + off + (ACCESS_RAN,)
    return evaluate(counts, now, rules, site, um.website_id, event, prop), ran, empty


def access_card(site: str, website: str, code: int, credential: str, both: bool = False) -> dict:
    lacks = f'the {credential} {standing(401 if both else code)} {PERMISSION}'
    x = anomaly(CHECK, 'umami-access', '-', site, ACCESS_PART, 'warn', 1, None, None, '', f'Umami cannot read the {ACCESS_PART}: {lacks}',
                f'Umami refused the reads of website {website} (HTTP {code}): {lacks}; {WHO}. '
                'Its pageview and event cards stay open until the website can be read again, and this card closes at the first pass that reads it.',
                1, f'WARN {site} Umami cannot read the {ACCESS_PART} (HTTP {code})')
    x['ranKey'] = ACCESS_RAN
    x['siteId'] = website
    return x


def links_for(a: dict, ui: str, website: str) -> list[dict] | None:
    if a.get('src') != CHECK:
        return None
    if not ui:
        return []
    given = str(a.get('siteId') or '')
    site = given if WEBSITE.fullmatch(given) else website
    return [{'label': 'Umami website', 'url': f'{ui}/websites/{site}' if WEBSITE.fullmatch(site) else ui}]
