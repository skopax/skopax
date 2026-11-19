import datetime as dt
import logging
import time

from skopax import history
from skopax.actions import default_reason
from skopax.engine import mute_live
from skopax.errkind import kind as error_kind
from skopax.store import MUTED_PRESET, Store, mute_matcher

log = logging.getLogger('skopax.mutemarks')

OWNER = 'core-mute-marks'
VERSION = 1
NAME = 'mute-preset-marks'
KINDS = ('known', 'mute')
SLACK = 'slack'
CHAT = 'chat'
LAG_S = 60
BATCH = 500
ACTION_FIELDS = {'type': 1, 'key': 1, 'reason': 1, 'source': 1, 'askId': 1, 'via': 1, 'hours': 1, 'requestedAt': 1, 'doneAt': 1}
ROW_FIELDS = {'key': 1, 'muted': 1}


def stamp(text) -> float | None:
    try:
        return dt.datetime.fromisoformat(str(text).replace('Z', '+00:00')).timestamp()
    except ValueError:
        return None


class Retry(Exception):
    pass


def written_text(action: dict, asks: dict) -> str | None:
    if not action.get('reason') or action.get('via') == CHAT:
        return default_reason(action)
    if action.get('source') != SLACK or not action.get('askId'):
        return None
    call = ((asks.get(action['askId']) or {}).get('cardActionApplied') or {}).get('call')
    return action['reason'] if isinstance(call, dict) and not call.get('reason') else None


def made_by_default(entry: dict, actions: dict, asks: dict) -> bool:
    at = stamp(entry.get('at'))
    if at is None:
        return False
    for a in actions.get((entry.get('kind'), entry.get('pattern')), ()):
        t = stamp(a.get('doneAt') or a.get('requestedAt'))
        if t is not None and abs(t - at) <= LAG_S and entry.get('reason') == written_text(a, asks):
            return True
    return False


def mark_entries(store: Store) -> int:
    open_ = [m for m in store.mute_entries() if isinstance(m, dict) and not m.get('preset') and m.get('kind') in KINDS and isinstance(m.get('pattern'), str)]
    if not open_:
        return 0
    wanted = {(m['kind'], m['pattern']) for m in open_}
    actions: dict = {}
    for a in store.actions.find({'type': {'$in': list(KINDS)}, 'status': 'done', 'key': {'$in': sorted({p for _, p in wanted})}}, ACTION_FIELDS):
        if (a.get('type'), a.get('key')) in wanted:
            actions.setdefault((a['type'], a['key']), []).append(a)
    ids = sorted({a['askId'] for found in actions.values() for a in found if a.get('source') == SLACK and a.get('askId')})
    asks = {a['_id']: a for a in store.actions.find({'_id': {'$in': ids}}, {'cardActionApplied.call.reason': 1})} if ids else {}
    marks = [(m['kind'], m['pattern'], m.get('at')) for m in open_ if made_by_default(m, actions, asks)]
    if not marks:
        return 0
    done = store.mark_preset_mutes(marks)
    if done is None:
        raise Retry()
    return done


def mark_rows(store: Store, now: float) -> int:
    entries = [(m, mute_matcher(m)) for m in store.mute_entries() if isinstance(m, dict) and isinstance(m.get('pattern'), str) and mute_live(m, now)]
    if not any(m.get('preset') for m, _ in entries):
        return 0
    marked, batch = 0, []
    for doc in store.card_history.find({'muted': {'$nin': [None, '']}, MUTED_PRESET: {'$ne': True}}, ROW_FIELDS):
        key = doc.get('key')
        m = next((m for m, fits in entries if fits(key)), None) if isinstance(key, str) else None
        if m is None or not m.get('preset') or doc.get('muted') not in (m.get('reason'), history.cut(m.get('reason'), history.MUTED_LIMIT)):
            continue
        batch.append(doc['_id'])
        if len(batch) == BATCH:
            marked += store.mark_preset_rows(batch)
            batch = []
    return marked + (store.mark_preset_rows(batch) if batch else 0)


def mark(store: Store) -> tuple[int, int]:
    entries = mark_entries(store)
    rows = mark_rows(store, time.time())
    log.info('mute reasons Skopax wrote itself, marked: %d mute entries, %d past cards', entries, rows)
    return entries, rows


def record(store: Store) -> None:
    col = store.db['schema_version']
    col.update_one({'_id': OWNER}, {'$max': {'version': VERSION}}, upsert=True)
    col.update_one({'_id': OWNER, 'applied.version': {'$ne': VERSION}}, {'$push': {'applied': {'version': VERSION, 'name': NAME, 'at': history.iso(time.time())}}})


def run_once(store: Store) -> bool:
    try:
        if int((store.db['schema_version'].find_one({'_id': OWNER}) or {}).get('version', 0)) >= VERSION:
            return False
        mark(store)
        record(store)
        return True
    except Retry:
        return False
    except Exception as e:
        log.warning('mute marks not written, the next start tries again: %s', error_kind(e))
        return False
