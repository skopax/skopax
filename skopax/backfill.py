import datetime as dt
import logging
from collections import Counter

from skopax.engine import parse_iso
from skopax.errkind import kind as error_kind
from skopax import clock
from skopax.investigator import SOURCE_OF
from skopax.investigator.pricing import PARTS, POD_BY, PRICES, split, usd
from skopax.store import NotFound, Store, iso

log = logging.getLogger('skopax.backfill')


def at_of(record: dict) -> float:
    return parse_iso(record.get('endedAt') or record['startedAt'])


def served_model(record: dict) -> str:
    if record.get('model'):
        return record['model']
    return Counter(record['models']).most_common(1)[0][0]


def requested_by(store: Store, record: dict) -> str:
    asked = (record.get('ask') or {}).get('by')
    if asked:
        return asked
    if record.get('actionId'):
        try:
            return store.get_action(record['actionId']).get('requestedBy') or POD_BY
        except NotFound:
            pass
    return POD_BY


def priced_split(model: str, usage: dict, total: float) -> tuple[str, dict]:
    priced = model if model in PRICES else max(PRICES, key=lambda m: usd(m, usage))
    parts = split(priced, usage)
    base = parts['usd']['total']
    if base > 0:
        scale = total / base
        parts['usd'] = {p: v * scale for p, v in parts['usd'].items()}
    else:
        parts['usd'] = dict.fromkeys(PARTS, 0.0) | {'input': total}
    parts['usd']['total'] = total
    return priced, parts


def row_of(store: Store, record: dict) -> dict | None:
    source = SOURCE_OF.get(record.get('trigger'))
    if source is None:
        return None
    model = served_model(record)
    total = float(record['usd'])
    priced, parts = priced_split(model, record.get('usage') or {}, total)
    at = dt.datetime.fromtimestamp(at_of(record), dt.timezone.utc)
    row = {'_id': 'backfill-' + record['_id'], 'backfill': True, 'at': iso(at), 'atDt': at, 'day': clock.day(parse_iso(record['startedAt'])), 'source': source,
           'model': model, 'effort': record.get('effort'), **parts, 'calls': record.get('turns') or len(record.get('models') or []) or 1,
           'runId': record['_id'], 'actionId': record.get('actionId'), 'keys': record.get('keys') or [], 'by': requested_by(store, record)}
    if priced != model:
        row['pricedAs'] = priced
    return row


def safe_row(store: Store, record: dict) -> dict | None:
    try:
        return row_of(store, record)
    except (KeyError, TypeError, ValueError, IndexError) as e:
        log.warning('run %s not backfilled: %s', record.get('_id'), error_kind(e))
        return None


def run(store: Store) -> int:
    try:
        rows = [r for r in (safe_row(store, d) for d in store.backfill_candidates()) if r is not None]
        written = store.add_ledger_backfill(rows)
    except Exception as e:
        log.warning('spend ledger backfill skipped: %s', error_kind(e))
        return 0
    if written:
        log.info('spend ledger backfilled %d runs', written)
    return written
