import datetime as dt
import logging

from skopax import clock
from skopax.store import Store, iso, utc_now

log = logging.getLogger('skopax.settle')

SETTLE_MINUTES = 35
WINDOW_HOURS = 3
RELEASE_GAP_HOURS = 2
REPORT_LOOKBACK_HOURS = 24
STUCK_MINUTES = 60
SETTLED_BY = 'auto · release settled'


def when(value: str | None) -> dt.datetime | None:
    try:
        return dt.datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    except (TypeError, ValueError):
        return None


def newest_release(deploys: list[dict]) -> list[dict]:
    wave: list[dict] = []
    for d in deploys:
        t = when(d.get('at'))
        if t is None:
            continue
        if wave and t - when(wave[-1]['at']) > dt.timedelta(hours=RELEASE_GAP_HOURS):
            wave = []
        wave.append(d)
    return wave


def covered(last: dict, reports: list[dict], actions: list[dict]) -> str | None:
    at = when(last['at'])
    for r in reports:
        rows = r.get('rows') or []
        made = when(r.get('createdAt'))
        if any(x.get('svc') == last['svc'] and x.get('toImage') == last['toImage'] for x in rows):
            return f"deploy report {r['id']}"
        if not rows and made and made >= at:
            return f"deploy report {r['id']}"
    for a in actions:
        asked = when(a.get('requestedAt'))
        if a.get('status') in ('requested', 'processing'):
            return f"postdeploy action {a['id']} ({a['status']})"
        if asked and asked >= at and a.get('status') != 'failed':
            return f"postdeploy action {a['id']} ({a.get('status')})"
    return None


def ignore_doc(store: Store) -> dict:
    return {'services': list(store.release_ignore), 'updatedAt': None, 'updatedBy': None, **(store.postdeploy_ignore() or {})}


def ignored(store: Store) -> frozenset[str]:
    return frozenset(ignore_doc(store)['services'])


class Settle:
    def __init__(self, store: Store, enabled: bool):
        self.store = store
        self.enabled = enabled

    def run_once(self, now: dt.datetime | None = None) -> dict | None:
        now = now or utc_now()
        stuck = self.store.fail_stale_actions(iso(now - dt.timedelta(minutes=STUCK_MINUTES)), ('postdeploy',))
        if stuck:
            log.warning('%d post-deploy checks failed after %d min in processing: their pod died mid-check', stuck, STUCK_MINUTES)
        deploys = self.store.deploys_since(iso(now - dt.timedelta(hours=WINDOW_HOURS + RELEASE_GAP_HOURS)))
        skip = ignored(self.store)
        wave = newest_release([d for d in deploys if d.get('svc') not in skip])
        if not wave:
            return None
        last = wave[-1]
        age = now - when(last['at'])
        if age < dt.timedelta(minutes=SETTLE_MINUTES) or age > dt.timedelta(hours=WINDOW_HOURS):
            return None
        since = iso(now - dt.timedelta(hours=REPORT_LOOKBACK_HOURS))
        why = covered(last, self.store.reports_since(since), self.store.postdeploy_actions(since))
        if why:
            return None
        if not self.store.claim_run(f"settle:{last['id']}"):
            return None
        first = when(wave[0]['at'])
        svcs = sorted({d['svc'] for d in wave})
        start, end = clock.hm(first), clock.hm(when(last['at']))
        label = f"Post-deploy check: release settled, {svcs[0] if len(svcs) == 1 else f'{len(svcs)} services'} {start if start == end else f'{start}–{end}'} {clock.label(first)}"
        action = self.store.add_action({'type': 'postdeploy', 'key': 'postdeploy', 'target': 'postdeploy', 'label': label[:300], 'requestedBy': SETTLED_BY,
                                        'release': {'firstAt': wave[0]['at'], 'lastAt': last['at'], 'lastId': last['id'], 'deploys': len(wave), 'services': svcs[:50]}})
        log.info('release settled at %s with %d deploys, queued postdeploy action %s', last['at'], len(wave), action['id'])
        return action
