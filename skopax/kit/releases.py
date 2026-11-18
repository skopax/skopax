import datetime as dt

from skopax import clock
from skopax.contract import iso

GTM_KIND = 'gtm'


def seconds(text: object) -> float | None:
    try:
        return dt.datetime.fromisoformat(str(text)).timestamp()
    except (ValueError, OverflowError, OSError):
        return None


def publish_near(rows: list[dict], start: float, end: float) -> tuple[float, dict] | None:
    best = None
    for r in rows:
        if not isinstance(r, dict) or r.get('kind') != GTM_KIND:
            continue
        t = seconds(r.get('at'))
        if t is not None and start <= t <= end and (best is None or t > best[0]):
            best = (t, r)
    return best


def attach_publish(cards: list[dict], rows: list[dict], start: float, end: float) -> None:
    hit = publish_near(rows, start, end)
    if not hit or not cards:
        return
    t, r = hit
    told = r.get('gtm') if isinstance(r.get('gtm'), dict) else {}
    container, version = r.get('svc'), str(r.get('toImage') or '').removeprefix('v')
    named = f' "{told["name"]}"' if told.get('name') else ''
    line = f' GTM {container} v{version}{named} went live around {clock.date(t)} {clock.label(t)}' + (f': {told["changes"]}' if told.get('changes') else '') + '.'
    for x in cards:
        x['gtmPublish'] = {'container': container, 'version': version, 'name': told.get('name'), 'at': iso(t), 'changes': told.get('changes')}
        x['what'] = x['what'].rstrip() + line
