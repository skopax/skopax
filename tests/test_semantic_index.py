import datetime as dt
import threading

import numpy as np
import pytest

from skopax.semantic import index as index_mod
from skopax.semantic.index import NotReady, VectorIndex, pack_vector

DIMS = 16


def unit(rng) -> np.ndarray:
    v = rng.normal(size=DIMS).astype(np.float32)
    return v / np.linalg.norm(v)


def stored(v: np.ndarray) -> np.ndarray:
    return np.frombuffer(pack_vector(v, DIMS), dtype='<f2').astype(np.float32)


def put(store, kind, ref, v, part=0, fp='fp1', at=None, **f) -> None:
    doc = {'kind': kind, 'ref': ref, 'part': part, 'fp': fp, 'hash': f'{kind}{ref}{part}', 'vec': pack_vector(v, DIMS), 'f': f}
    if at is not None:
        doc['at'] = at
    store.upsert_embeddings([doc], dims=DIMS)


def fill(store, count, seed=0, kind='run', fp='fp1', start=0, **f):
    rng = np.random.default_rng(seed)
    vectors = {}
    for n in range(start, start + count):
        v = unit(rng)
        vectors[f'{kind}:{n}:0'] = v
        put(store, kind, str(n), v, fp=fp, **f)
    return vectors


def brute(vectors: dict, query: np.ndarray, allowed=None) -> list:
    q = query / np.linalg.norm(query)
    scored = [(i, float(stored(v) @ q)) for i, v in vectors.items() if allowed is None or i in allowed]
    return sorted(scored, key=lambda t: -t[1])


def test_search_matches_a_brute_force_loop(store):
    # given
    vectors = fill(store, 300, seed=1)
    query = unit(np.random.default_rng(99))
    idx = VectorIndex(DIMS)
    idx.load(store, 'fp1')

    # when
    got = idx.search(query, 5)

    # then
    want = brute(vectors, query)[:20]
    assert [i for i, _ in got] == [i for i, _ in want]
    assert [s for _, s in got] == pytest.approx([s for _, s in want], abs=1e-5)
    assert len(got) == 20


def test_scores_are_cosine_even_for_an_unnormalised_query(store):
    # given
    vectors = fill(store, 20, seed=2)
    query = unit(np.random.default_rng(5))
    idx = VectorIndex(DIMS)
    idx.load(store, 'fp1')

    # when
    got = idx.search(query * 37.0, 1)

    # then
    assert got[0][0] == brute(vectors, query)[0][0]
    assert got[0][1] == pytest.approx(brute(vectors, query)[0][1], abs=1e-5)


def test_blocks_merge_into_the_global_top(store, monkeypatch):
    # given
    monkeypatch.setattr(index_mod, 'BLOCK', 7)
    vectors = fill(store, 100, seed=3)
    query = unit(np.random.default_rng(11))
    idx = VectorIndex(DIMS)
    idx.load(store, 'fp1')

    # when
    got = idx.search(query, 3)

    # then
    assert [i for i, _ in got] == [i for i, _ in brute(vectors, query)[:12]]


def test_masks_by_kind_and_filter_fields(store):
    # given
    runs = fill(store, 40, seed=4, kind='run', svc='shop')
    cards = fill(store, 40, seed=5, kind='card', svc='shop')
    other = fill(store, 40, seed=6, kind='run', svc='billing', start=100)
    docs = fill(store, 10, seed=7, kind='doc', plugin='confluence', instance='main', space='ENG', slack=True)
    query = unit(np.random.default_rng(12))
    idx = VectorIndex(DIMS)
    idx.load(store, 'fp1')

    # when
    only_cards = idx.search(query, 100, kinds=['card'])
    shop_runs = idx.search(query, 100, kinds=['run'], svc='shop')
    two = idx.search(query, 100, svc={'shop', 'billing'}, kinds=('run',))
    plugin = idx.search(query, 100, plugin='confluence', instance='main', slack=True)
    nothing = idx.search(query, 100, svc='nope')

    # then
    assert {i for i, _ in only_cards} == set(cards)
    assert {i for i, _ in shop_runs} == set(runs)
    assert {i for i, _ in two} == set(runs) | set(other)
    assert {i for i, _ in plugin} == set(docs)
    assert nothing == []
    assert idx.search(query, 100, kinds=['doc'], plugin='other') == []
    assert idx.search(query, 100, kinds=['doc'], slack=False) == []


def test_rows_missing_a_filter_field_never_match_it(store):
    # given
    fill(store, 5, seed=8, kind='run')
    idx = VectorIndex(DIMS)
    idx.load(store, 'fp1')
    query = unit(np.random.default_rng(1))

    # when
    got = idx.search(query, 5, svc='shop')

    # then
    assert got == []


def test_unknown_kind_or_filter_is_refused(store):
    # given
    fill(store, 3, seed=9)
    idx = VectorIndex(DIMS)
    idx.load(store, 'fp1')
    query = unit(np.random.default_rng(1))

    # when
    calls = [lambda: idx.search(query, 3, kinds=['chat']), lambda: idx.search(query, 3, password='x'), lambda: idx.search(query[:-1], 3), lambda: idx.search(np.zeros(DIMS), 3)]

    # then
    for call in calls:
        with pytest.raises(ValueError):
            call()


def test_nothing_is_loaded_until_the_first_search_asks(store):
    # given
    fill(store, 3, seed=10)
    idx = VectorIndex(DIMS)

    # when
    before = (idx.ready, idx.size)
    with pytest.raises(NotReady):
        idx.search(unit(np.random.default_rng(1)), 3)
    idx.ensure(store, 'fp1')

    # then
    assert before == (False, 0)
    assert idx.ready and idx.size == 3 and idx.fp == 'fp1'
    assert idx.counts() == {'run': 3}


def test_refresh_picks_up_inserts_and_replacements(store):
    # given
    vectors = fill(store, 10, seed=11)
    idx = VectorIndex(DIMS)
    idx.load(store, 'fp1')
    rng = np.random.default_rng(123)
    newcomer = unit(rng)
    replaced = unit(rng)

    # when
    quiet = idx.refresh(store)
    put(store, 'run', 'new', newcomer)
    put(store, 'run', '3', replaced)
    changed = idx.refresh(store)

    # then
    assert quiet is False and changed is True
    assert idx.size == 11
    assert idx.search(newcomer, 1)[0][0] == 'run:new:0'
    assert idx.search(replaced, 1)[0][0] == 'run:3:0'
    vectors['run:3:0'] = replaced
    vectors['run:new:0'] = newcomer
    query = unit(rng)
    assert [i for i, _ in idx.search(query, 11)] == [i for i, _ in brute(vectors, query)]


def test_refresh_drops_tombstoned_rows(store):
    # given
    vectors = fill(store, 10, seed=12)
    idx = VectorIndex(DIMS)
    idx.load(store, 'fp1')
    victim = vectors['run:4:0']

    # when
    store.delete_embeddings('run', '4')
    idx.refresh(store)

    # then
    assert idx.size == 9
    assert 'run:4:0' not in [i for i, _ in idx.search(victim, 9)]


def test_a_row_deleted_and_written_again_comes_back(store):
    # given
    fill(store, 3, seed=13)
    idx = VectorIndex(DIMS)
    idx.load(store, 'fp1')
    again = unit(np.random.default_rng(77))

    # when
    store.delete_embeddings('run', '1')
    put(store, 'run', '1', again)
    idx.refresh(store)

    # then
    assert idx.size == 3
    assert idx.search(again, 1)[0][0] == 'run:1:0'


def test_load_skips_rows_deleted_before_it_started(store):
    # given
    fill(store, 5, seed=14)
    store.delete_embeddings('run', '2')

    # when
    idx = VectorIndex(DIMS)
    idx.load(store, 'fp1')

    # then
    assert idx.size == 4


def test_fp_change_reloads_everything(store):
    # given
    fill(store, 5, seed=15, fp='fp1')
    fill(store, 3, seed=16, fp='fp2', start=50)
    idx = VectorIndex(DIMS)
    idx.ensure(store, 'fp1')

    # when
    idx.ensure(store, 'fp2')

    # then
    assert idx.fp == 'fp2'
    assert idx.size == 3
    assert {i for i, _ in idx.search(unit(np.random.default_rng(1)), 3)} == {'run:50:0', 'run:51:0', 'run:52:0'}


def test_full_reload_after_the_reload_interval_and_refresh_after_the_short_one(store):
    # given
    now = [0.0]
    fill(store, 3, seed=17)
    idx = VectorIndex(DIMS, refresh_seconds=30, reload_seconds=100, clock=lambda: now[0])
    idx.ensure(store, 'fp1')
    put(store, 'run', 'late', unit(np.random.default_rng(3)))

    # when
    now[0] = 10
    idx.ensure(store, 'fp1')
    early = idx.size
    now[0] = 40
    idx.ensure(store, 'fp1')
    refreshed = idx.size
    store.embeddings.delete_one({'_id': 'run:0:0'})
    now[0] = 80
    idx.ensure(store, 'fp1')
    still = idx.size
    now[0] = 140
    idx.ensure(store, 'fp1')

    # then
    assert (early, refreshed, still) == (3, 4, 4)
    assert idx.size == 3


def test_cap_keeps_the_newest_per_kind_and_reports_capped(store):
    # given
    fill(store, 30, seed=18, kind='run')
    fill(store, 30, seed=19, kind='card')
    fill(store, 2, seed=20, kind='change')
    idx = VectorIndex(DIMS, max_vectors=20)

    # when
    idx.load(store, 'fp1')

    # then
    assert idx.capped is True
    assert idx.size == 20
    counts = idx.counts()
    assert counts['change'] == 2 and counts['run'] == 9 and counts['card'] == 9
    ids = {i for i, _ in idx.search(unit(np.random.default_rng(1)), 20)}
    assert {f'run:{n}:0' for n in range(21, 30)} <= ids
    assert 'run:0:0' not in ids


def test_not_capped_when_it_fits(store):
    # given
    fill(store, 5, seed=21)
    idx = VectorIndex(DIMS, max_vectors=10)

    # when
    idx.load(store, 'fp1')

    # then
    assert idx.capped is False


def test_float16_round_trip_is_within_tolerance():
    # given
    v = unit(np.random.default_rng(31))

    # when
    packed = pack_vector(v, DIMS)
    back = np.frombuffer(packed, dtype='<f2').astype(np.float32)

    # then
    assert len(packed) == DIMS * 2
    assert np.abs(back - v).max() < 1e-3
    assert float(back @ v) == pytest.approx(1.0, abs=1e-3)


def test_pack_refuses_wrong_length_and_non_finite_values():
    # given
    bad = [[0.1] * (DIMS - 1), [float('nan')] + [0.1] * (DIMS - 1), [1e9] + [0.1] * (DIMS - 1)]

    # when
    calls = [lambda v=v: pack_vector(v, DIMS) for v in bad]

    # then
    for call in calls:
        with pytest.raises(ValueError):
            call()


def test_rows_of_the_wrong_length_in_mongo_are_skipped(store):
    # given
    fill(store, 3, seed=22)
    store.upsert_embeddings([{'kind': 'run', 'ref': 'odd', 'part': 0, 'fp': 'fp1', 'hash': 'x', 'vec': b'\x00\x00' * (DIMS + 1)}])
    idx = VectorIndex(DIMS)

    # when
    idx.load(store, 'fp1')

    # then
    assert idx.size == 3


def test_concurrent_searches_during_refresh_stay_consistent(store):
    # given
    vectors = fill(store, 200, seed=23)
    idx = VectorIndex(DIMS)
    idx.load(store, 'fp1')
    query = unit(np.random.default_rng(8))
    base = [i for i, _ in brute(vectors, query)[:20]]
    stop = threading.Event()
    problems = []

    def searcher():
        while not stop.is_set():
            got = [i for i, _ in idx.search(query, 5)]
            if len(got) != len(set(got)) or any(not i.startswith('run:') for i in got):
                problems.append(got)
            if not set(base[:5]) <= set(got) and not any(i.startswith('run:x') for i in got):
                problems.append(got)

    threads = [threading.Thread(target=searcher) for _ in range(4)]
    for t in threads:
        t.start()

    # when
    for n in range(15):
        put(store, 'run', f'x{n}', unit(np.random.default_rng(1000 + n)))
        idx.refresh(store)
    stop.set()
    for t in threads:
        t.join()

    # then
    assert problems == []
    assert idx.size == 215


def test_the_cap_keeps_the_newest_by_source_time_not_insertion_order(store):
    # given
    rng = np.random.default_rng(31)
    base = dt.datetime(2026, 11, 1, tzinfo=dt.timezone.utc)
    for n in range(12):
        put(store, 'run', str(n), unit(rng), at=base - dt.timedelta(days=n))
    idx = VectorIndex(DIMS, max_vectors=5)

    # when
    idx.load(store, 'fp1')

    # then
    ids = {i for i, _ in idx.search(unit(rng), 5)}
    assert ids == {f'run:{n}:0' for n in range(5)}
    assert idx.capped is True


def test_rows_without_a_source_time_are_evicted_first(store):
    # given
    rng = np.random.default_rng(32)
    base = dt.datetime(2026, 11, 1, tzinfo=dt.timezone.utc)
    put(store, 'run', 'old', unit(rng), at=base - dt.timedelta(days=400))
    for n in range(3):
        put(store, 'run', f'undated{n}', unit(rng))
    put(store, 'run', 'new', unit(rng), at=base)
    idx = VectorIndex(DIMS, max_vectors=2)

    # when
    idx.load(store, 'fp1')

    # then
    assert {i for i, _ in idx.search(unit(rng), 2)} == {'run:old:0', 'run:new:0'}


def test_refresh_does_not_re_add_rows_the_cap_evicted(store):
    # given
    rng = np.random.default_rng(33)
    base = dt.datetime(2026, 11, 1, tzinfo=dt.timezone.utc)
    for n in range(8):
        put(store, 'run', str(n), unit(rng), at=base - dt.timedelta(days=n))
    put(store, 'run', 'ancient', unit(rng), at=base - dt.timedelta(days=900))
    idx = VectorIndex(DIMS, max_vectors=8)
    idx.load(store, 'fp1')

    # when
    first = idx.refresh(store)
    second = idx.refresh(store)

    # then
    assert first is False and second is False
    assert idx.size == 8
    assert 'run:ancient:0' not in {i for i, _ in idx.search(unit(rng), 8)}


def test_an_evicted_row_that_is_replaced_by_a_newer_one_comes_back(store):
    # given
    rng = np.random.default_rng(34)
    base = dt.datetime(2026, 11, 1, tzinfo=dt.timezone.utc)
    for n in range(4):
        put(store, 'run', str(n), unit(rng), at=base - dt.timedelta(days=n))
    put(store, 'run', 'late', unit(rng), at=base - dt.timedelta(days=900))
    idx = VectorIndex(DIMS, max_vectors=4)
    idx.load(store, 'fp1')

    # when
    put(store, 'run', 'late', unit(rng), at=base + dt.timedelta(days=1))
    changed = idx.refresh(store)

    # then
    assert changed is True
    assert 'run:late:0' in {i for i, _ in idx.search(unit(rng), 4)}


def test_concurrent_ensure_calls_load_only_once(store):
    # given
    fill(store, 5, seed=35)
    idx = VectorIndex(DIMS)
    calls = []
    real = store.embeddings_since
    gate = threading.Barrier(4)

    def counting(fp, seq, limit):
        calls.append(seq)
        return real(fp, seq, limit)

    store.embeddings_since = counting

    def work():
        gate.wait()
        idx.ensure(store, 'fp1')

    # when
    threads = [threading.Thread(target=work) for _ in range(4)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()

    # then
    assert idx.size == 5 and len(calls) == 1
