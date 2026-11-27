import datetime as dt
import logging
import threading
import time
from dataclasses import dataclass

import numpy as np

from skopax.store import EMBED_KINDS

log = logging.getLogger('skopax.semantic.index')

BLOCK = 16384
PAGE = 2000
CANDIDATES = 4
OVERLAP = 64
REFRESH_SECONDS = 30
RELOAD_SECONDS = 6 * 3600
MAX_VECTORS = 300000
FILTER_FIELDS = ('svc', 'plugin', 'instance', 'space', 'slack', 'level', 'sev', 'trigger', 'label')
KIND_CODES = {k: n for n, k in enumerate(EMBED_KINDS)}
WIRE = np.dtype('<f2')


class NotReady(Exception):
    pass


def pack_vector(values, dims: int) -> bytes:
    arr = np.asarray(values, dtype=np.float32)
    if arr.ndim != 1 or arr.shape[0] != dims:
        raise ValueError('vector has the wrong length')
    if not np.isfinite(arr).all():
        raise ValueError('vector has a value that is not finite')
    with np.errstate(over='ignore'):
        packed = arr.astype(WIRE)
    if not np.isfinite(packed).all():
        raise ValueError('vector does not fit in float16')
    return packed.tobytes()


def _code(table: dict, value) -> int:
    if value is None:
        return 0
    if isinstance(value, bool):
        value = bool(value)
    elif isinstance(value, (list, tuple, dict, set)):
        return 0
    return table.setdefault(value, len(table) + 1)


@dataclass(frozen=True)
class _Snap:
    fp: str
    vecs: np.ndarray
    ids: tuple
    seqs: np.ndarray
    ats: np.ndarray
    kinds: np.ndarray
    filters: dict
    tables: dict
    evicted: dict
    last_seq: int
    tomb_seq: int
    capped: bool
    loaded_at: float
    refreshed_at: float


def _empty(fp: str, dims: int, now: float) -> _Snap:
    return _Snap(fp, np.empty((0, dims), dtype=WIRE), (), np.empty(0, dtype=np.int64), np.empty(0, dtype=np.float64), np.empty(0, dtype=np.uint8),
                 {f: np.empty(0, dtype=np.int32) for f in FILTER_FIELDS}, {f: {} for f in FILTER_FIELDS}, {}, 0, 0, False, now, now)


def _stamp(value) -> float:
    if not isinstance(value, dt.datetime):
        return 0.0
    try:
        moment = value if value.tzinfo else value.replace(tzinfo=dt.timezone.utc)
        return moment.timestamp()
    except (OverflowError, OSError, ValueError):
        return 0.0


def _merge(base: _Snap, dims: int, fresh: dict, dead: set, last_seq: int, tomb_seq: int, max_vectors: int, loaded_at: float, now: float) -> _Snap:
    drop = dead | set(fresh)
    keep = np.fromiter((i not in drop for i in base.ids), dtype=bool, count=len(base.ids))
    rows = sorted(fresh.values(), key=lambda r: r['seq'])
    tables = {f: dict(t) for f, t in base.tables.items()}
    add_vecs = np.empty((len(rows), dims), dtype=WIRE)
    add_filters = {f: np.zeros(len(rows), dtype=np.int32) for f in FILTER_FIELDS}
    for n, row in enumerate(rows):
        add_vecs[n] = np.frombuffer(row['vec'], dtype=WIRE)
        for f in FILTER_FIELDS:
            add_filters[f][n] = _code(tables[f], row['f'].get(f))
    vecs = np.concatenate((base.vecs[keep], add_vecs))
    ids = tuple(i for i, k in zip(base.ids, keep) if k) + tuple(r['id'] for r in rows)
    seqs = np.concatenate((base.seqs[keep], np.array([r['seq'] for r in rows], dtype=np.int64)))
    ats = np.concatenate((base.ats[keep], np.array([r['at'] for r in rows], dtype=np.float64)))
    kinds = np.concatenate((base.kinds[keep], np.array([KIND_CODES[r['kind']] for r in rows], dtype=np.uint8)))
    filters = {f: np.concatenate((base.filters[f][keep], add_filters[f])) for f in FILTER_FIELDS}
    horizon = last_seq - OVERLAP
    evicted = {i: q for i, q in base.evicted.items() if q > horizon and i not in drop}
    chosen = _capped(kinds, seqs, ats, max_vectors)
    capped = chosen is not None or base.capped
    if chosen is not None:
        gone = np.setdiff1d(np.arange(len(ids)), chosen)
        evicted.update({ids[int(i)]: int(seqs[i]) for i in gone if int(seqs[i]) > horizon})
        vecs, seqs, ats, kinds = vecs[chosen], seqs[chosen], ats[chosen], kinds[chosen]
        ids = tuple(ids[int(i)] for i in chosen)
        filters = {f: v[chosen] for f, v in filters.items()}
    return _Snap(base.fp, vecs, ids, seqs, ats, kinds, filters, tables, evicted, last_seq, tomb_seq, capped, loaded_at, now)


def _capped(kinds: np.ndarray, seqs: np.ndarray, ats: np.ndarray, max_vectors: int):
    if len(kinds) <= max_vectors:
        return None
    groups = {code: np.flatnonzero(kinds == code) for code in np.unique(kinds)}
    quota = {}
    left = max_vectors
    pending = sorted(groups, key=lambda c: len(groups[c]))
    while pending:
        share = left // len(pending)
        code = pending.pop(0)
        quota[code] = min(len(groups[code]), share)
        left -= quota[code]
    kept = []
    for code, rows in groups.items():
        newest = rows[np.lexsort((seqs[rows], ats[rows]))]
        kept.append(newest[len(newest) - quota[code]:])
    return np.sort(np.concatenate(kept))


class VectorIndex:
    def __init__(self, dims: int, max_vectors: int = MAX_VECTORS, refresh_seconds: float = REFRESH_SECONDS, reload_seconds: float = RELOAD_SECONDS, clock=time.monotonic):
        if dims <= 0:
            raise ValueError('dims must be positive')
        self.dims = dims
        self.max_vectors = max(1, int(max_vectors))
        self.refresh_seconds = refresh_seconds
        self.reload_seconds = reload_seconds
        self._clock = clock
        self._snap: _Snap | None = None
        self._lock = threading.Lock()

    @property
    def ready(self) -> bool:
        return self._snap is not None

    @property
    def capped(self) -> bool:
        snap = self._snap
        return bool(snap and snap.capped)

    @property
    def size(self) -> int:
        snap = self._snap
        return len(snap.ids) if snap else 0

    @property
    def fp(self) -> str | None:
        snap = self._snap
        return snap.fp if snap else None

    def counts(self) -> dict:
        snap = self._snap
        if not snap:
            return {}
        out = {}
        for kind, code in KIND_CODES.items():
            n = int((snap.kinds == code).sum())
            if n:
                out[kind] = n
        return out

    def _row(self, doc: dict) -> dict | None:
        raw = doc.get('vec')
        kind = doc.get('kind')
        if raw is None or kind not in KIND_CODES or len(bytes(raw)) != self.dims * 2 or not isinstance(doc.get('f') or {}, dict):
            log.warning('embedding %s skipped: wrong shape', doc.get('_id'))
            return None
        return {'id': doc['_id'], 'kind': kind, 'seq': int(doc['seq']), 'at': _stamp(doc.get('at')), 'vec': bytes(raw), 'f': doc.get('f') or {}}

    def _pull(self, store, fp: str, since: int, rows: dict) -> int:
        last = since
        while True:
            page = store.embeddings_since(fp, last, PAGE)
            for doc in page:
                row = self._row(doc)
                if row:
                    rows[row['id']] = row
                last = max(last, int(doc['seq']))
            if len(page) < PAGE:
                return last

    def load(self, store, fp: str) -> None:
        with self._lock:
            self._load(store, fp)

    def _load(self, store, fp: str) -> None:
        now = self._clock()
        rows: dict = {}
        last = self._pull(store, fp, 0, rows)
        tombs = store.tombstones_since(0)
        tomb_seq = max((int(t['seq']) for t in tombs), default=0)
        for t in tombs:
            row = rows.get(t['_id'])
            if row and int(t['seq']) > row['seq']:
                del rows[t['_id']]
        self._snap = _merge(_empty(fp, self.dims, now), self.dims, rows, set(), last, tomb_seq, self.max_vectors, now, now)

    def refresh(self, store) -> bool:
        with self._lock:
            return self._refresh(store)

    def _refresh(self, store) -> bool:
        snap = self._snap
        if snap is None:
            raise NotReady('index is not loaded')
        now = self._clock()
        rows = {}
        last = self._pull(store, snap.fp, max(0, snap.last_seq - OVERLAP), rows)
        tombs = store.tombstones_since(max(0, snap.tomb_seq - OVERLAP))
        known = dict(zip(snap.ids, snap.seqs.tolist()))
        fresh = {i: r for i, r in rows.items() if known.get(i) != r['seq'] and snap.evicted.get(i) != r['seq']}
        dead = set()
        tomb_seq = snap.tomb_seq
        for t in tombs:
            tomb_seq = max(tomb_seq, int(t['seq']))
            current = fresh[t['_id']]['seq'] if t['_id'] in fresh else known.get(t['_id'])
            if current is not None and int(t['seq']) > current:
                dead.add(t['_id'])
                fresh.pop(t['_id'], None)
        last = max(last, snap.last_seq)
        if not fresh and not dead:
            self._snap = _with_clock(snap, last, tomb_seq, now)
            return False
        self._snap = _merge(snap, self.dims, fresh, dead, last, tomb_seq, self.max_vectors, snap.loaded_at, now)
        return True

    def _due(self, fp: str) -> str | None:
        snap = self._snap
        now = self._clock()
        if snap is None or snap.fp != fp or now - snap.loaded_at >= self.reload_seconds:
            return 'load'
        if now - snap.refreshed_at >= self.refresh_seconds:
            return 'refresh'
        return None

    def ensure(self, store, fp: str) -> None:
        if self._due(fp) is None:
            return
        with self._lock:
            due = self._due(fp)
            if due == 'load':
                self._load(store, fp)
            elif due == 'refresh':
                self._refresh(store)

    def search(self, query_vec, k: int, kinds=None, **filters) -> list[tuple[str, float]]:
        snap = self._snap
        if snap is None:
            raise NotReady('index is not loaded')
        query = np.asarray(query_vec, dtype=np.float32)
        if query.ndim != 1 or query.shape[0] != self.dims or not np.isfinite(query).all():
            raise ValueError('query vector has the wrong length')
        norm = float(np.linalg.norm(query))
        if norm == 0.0:
            raise ValueError('query vector is zero')
        query = query / norm
        want = max(1, int(k)) * CANDIDATES
        mask = self._mask(snap, kinds, filters)
        if mask is not None and not mask.any():
            return []
        best_scores = np.empty(0, dtype=np.float32)
        best_rows = np.empty(0, dtype=np.int64)
        for start in range(0, len(snap.ids), BLOCK):
            end = min(start + BLOCK, len(snap.ids))
            if mask is None:
                rows = np.arange(start, end)
            else:
                rows = start + np.flatnonzero(mask[start:end])
            if rows.size == 0:
                continue
            block = snap.vecs[start:end] if mask is None else snap.vecs[rows]
            scores = block.astype(np.float32) @ query
            if scores.size > want:
                top = np.argpartition(scores, -want)[-want:]
                scores, rows = scores[top], rows[top]
            best_scores = np.concatenate((best_scores, scores))
            best_rows = np.concatenate((best_rows, rows))
            if best_scores.size > want:
                top = np.argpartition(best_scores, -want)[-want:]
                best_scores, best_rows = best_scores[top], best_rows[top]
        order = np.argsort(-best_scores, kind='stable')
        return [(snap.ids[int(best_rows[i])], float(best_scores[i])) for i in order]

    def _mask(self, snap: _Snap, kinds, filters: dict):
        mask = None
        if kinds is not None:
            wanted = list(kinds)
            if any(k not in KIND_CODES for k in wanted):
                raise ValueError('unknown kind')
            mask = np.isin(snap.kinds, [KIND_CODES[k] for k in wanted])
        for field, allowed in filters.items():
            if field not in FILTER_FIELDS:
                raise ValueError(f'unknown filter {field!r}')
            values = allowed if isinstance(allowed, (set, frozenset, list, tuple)) else [allowed]
            table = snap.tables[field]
            codes = [0 if v is None else table[v] for v in values if v is None or (not isinstance(v, (list, dict, set)) and v in table)]
            hit = np.isin(snap.filters[field], codes)
            mask = hit if mask is None else mask & hit
        return mask


def _with_clock(snap: _Snap, last: int, tomb_seq: int, now: float) -> _Snap:
    return _Snap(snap.fp, snap.vecs, snap.ids, snap.seqs, snap.ats, snap.kinds, snap.filters, snap.tables, snap.evicted, last, tomb_seq, snap.capped, snap.loaded_at, now)
