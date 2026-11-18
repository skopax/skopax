import datetime as dt

from skopax import clock
from skopax.settle import RELEASE_GAP_HOURS, REPORT_LOOKBACK_HOURS, SETTLE_MINUTES, WINDOW_HOURS, covered, ignored, newest_release, when
from skopax.store import Store, iso

RANK = {'crit': 3, 'warn': 2, 'info': 1}
SHOWN = 4
ADDRESSED = ('progress', 'handled')
NO_HEALTH = 'noHealth'


def health_card(a: dict) -> bool:
    return not a.get(NO_HEALTH)


def change(a: dict) -> str:
    now = a.get('now')
    base = max(a.get('d1') or 0, a.get('d7') or 0)
    if now is None:
        return ''
    if a.get('unit') == 'ms':
        return f'{now - base:+.0f} ms'
    if not base:
        return 'new' if now else ''
    r = now / base
    return f'×{r:.0f}' if r >= 10 else f'×{r:.1f}'


def open_cards(anomalies: list[dict]) -> list[dict]:
    opened = {a['key'] for a in anomalies if a.get('status') == 'open'}
    live = [a for a in anomalies if a.get('status') == 'open' and not a.get('muted') and a.get('triage') not in ADDRESSED and a.get('parent') not in opened]
    return sorted(live, key=lambda a: (-RANK.get(a.get('sev'), 0), a.get('firstSeen') or ''))


def waiting_cards(anomalies: list[dict]) -> list[dict]:
    keys = {a['key'] for a in anomalies}
    live = [a for a in anomalies if a.get('status') == 'cleared' and not a.get('muted') and a.get('triage') not in ADDRESSED and a.get('sev') in ('warn', 'crit') and not (a.get('parent') in keys and a.get('parent') != a['key'])]
    return sorted(live, key=lambda a: (-RANK.get(a.get('sev'), 0), a.get('firstSeen') or ''))


def release_line(store: Store, now: dt.datetime) -> str | None:
    skip = ignored(store)
    wave = newest_release([d for d in store.deploys_since(iso(now - dt.timedelta(hours=WINDOW_HOURS + RELEASE_GAP_HOURS))) if d.get('svc') not in skip])
    if not wave:
        return None
    first, last = when(wave[0]['at']), when(wave[-1]['at'])
    age = now - last
    if age > dt.timedelta(hours=WINDOW_HOURS):
        return None
    svcs = len({d['svc'] for d in wave})
    n = f"{svcs} {'service' if svcs == 1 else 'services'}"
    if age < dt.timedelta(minutes=SETTLE_MINUTES):
        return f'Release running since {clock.hm(first)} {clock.label()}, {n}'
    since = iso(now - dt.timedelta(hours=REPORT_LOOKBACK_HOURS))
    why = covered(wave[-1], store.reports_since(since), store.postdeploy_actions(since))
    if why and why.startswith('deploy report'):
        return f'Release {clock.hm(first)}–{clock.hm(last)} {clock.label()} ({n}): deploy report ready'
    return f'Release {clock.hm(first)}–{clock.hm(last)} {clock.label()} ({n}) finished, post-deploy check pending'


def build(state: dict, store: Store, now: float) -> dict:
    anomalies = state.get('anomalies') or []
    cards = [a for a in open_cards(anomalies) if health_card(a)]
    waiting = [a for a in waiting_cards(anomalies) if health_card(a)]
    level = max((a.get('sev') for a in cards), key=lambda s: RANK.get(s, 0), default='warn' if waiting else 'ok')
    if cards:
        parts = [f"{a.get('svc')} {a.get('label')} {change(a)}".strip() for a in cards[:SHOWN]]
        if len(cards) > SHOWN:
            parts.append(f'+{len(cards) - SHOWN} more')
        if waiting:
            parts.append(f'{len(waiting)} waiting to be addressed')
    elif waiting:
        parts = [f'{len(waiting)} waiting to be addressed']
    else:
        parts = ['All clear']
    at = dt.datetime.fromtimestamp(now, dt.timezone.utc)
    line = release_line(store, at)
    if line:
        parts.append(line)
    return {'headline': '; '.join(parts), 'points': [], 'level': level, 'at': iso(at)}
