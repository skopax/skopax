import logging
import threading
from typing import Callable

from skopax.engine import HANDLED, SEV_RANK
from skopax.summary import health_card


LEGACY_PLUGIN = 'prometheus'
NOT_LISTS: set[str] = set()
NOT_LISTS_GUARD = threading.Lock()

log = logging.getLogger(__name__)


def plugin_of(row: dict) -> str:
    return row.get('plugin') or LEGACY_PLUGIN


def _mine(row: dict, plugin: str, segment: str | None) -> bool:
    return plugin_of(row) == plugin and (row.get('cluster') or None) == segment


def stored_rows(state: dict, section: str) -> list[dict]:
    value = state.get(section, [])
    if isinstance(value, list):
        return value
    with NOT_LISTS_GUARD:
        first = section not in NOT_LISTS
        NOT_LISTS.add(section)
    if first:
        log.warning('stored section %s is not a list of rows; reading it as no rows', section)
    return []


def merge_rows(state: dict, section: str, rows: list[dict], plugin: str, segment: str | None) -> None:
    kept = [r for r in stored_rows(state, section) if not _mine(r, plugin, segment)]
    fresh = [{k: v for k, v in r.items() if k != 'cluster'} | {'plugin': plugin} if segment is None else dict(r, plugin=plugin, cluster=segment) for r in rows]
    state[section] = kept + fresh


def drop_rows(state: dict, section: str, plugin: str, segment: str | None) -> None:
    if section in state:
        state[section] = [r for r in stored_rows(state, section) if not _mine(r, plugin, segment)]


def merged_into(state: dict, a: dict) -> bool:
    p = a.get('parent')
    return bool(p) and any(x['key'] == p and x['status'] == 'open' for x in state['anomalies'])


def status_of(state: dict, svc_rows: dict) -> list[dict]:
    worst = {}
    row_ns = {r['ns'] for r in svc_rows.values()}
    for a in state['anomalies']:
        if a['status'] != 'open' or a.get('muted') or a.get('triage') in HANDLED or not health_card(a) or merged_into(state, a):
            continue
        k = (a.get('ns') if a.get('ns') in row_ns else '*', a['svc'])
        worst[k] = max(worst.get(k, a['sev']), a['sev'], key=SEV_RANK.get)
    rows = []
    for r in svc_rows.values():
        found = [worst[k] for k in ((r['ns'], r['svc']), ('*', r['svc'])) if k in worst]
        r['status'] = max(found, key=SEV_RANK.get) if found else 'ok'
        rows.append(r)
    rows.sort(key=lambda r: (-SEV_RANK.get(r['status'], -1), r['kind'] != 'app', -(r.get('req') or r.get('gw-req') or r.get('ing-req') or 0)))
    return rows


def finish(state: dict, image_by: Callable[[str, str, str], str | None] | None = None) -> None:
    rows = {(plugin_of(r), r.get('cluster'), r['ns'], r['svc']): dict(r) for r in state.get('services') or []}
    if image_by is not None:
        for r in rows.values():
            found = image_by(r['ns'], r['svc'], r['image']) if r.get('image') else None
            if found:
                r['imageBy'] = found
    state['services'] = status_of(state, rows)
    if 'deploys' in state:
        state['deploys'] = sorted(state['deploys'], key=lambda d: d['at'])
