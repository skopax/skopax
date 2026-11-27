import datetime as dt
import logging

import numpy as np
import pytest
from bson.binary import Binary

from skopax.semantic.index import pack_vector
from skopax.store import EMBED_KINDS, utc_now

DIMS = 8


def vec(seed: int) -> bytes:
    rng = np.random.default_rng(seed)
    v = rng.normal(size=DIMS)
    return pack_vector(v / np.linalg.norm(v), DIMS)


def doc(kind='run', ref='r1', part=0, fp='fp1', digest='h1', seed=1, **extra) -> dict:
    return {'kind': kind, 'ref': ref, 'part': part, 'fp': fp, 'hash': digest, 'vec': vec(seed), 'f': {'svc': 'shop'}, **extra}


def test_upsert_assigns_increasing_seq_and_stores_binary(store):
    # given
    docs = [doc(ref='a', seed=1), doc(ref='b', seed=2)]

    # when
    written = store.upsert_embeddings(docs, dims=DIMS)
    store.upsert_embeddings([doc(ref='c', seed=3)], dims=DIMS)

    # then
    assert written == 2
    rows = list(store.embeddings.find().sort('seq', 1))
    assert [r['_id'] for r in rows] == ['run:a:0', 'run:b:0', 'run:c:0']
    assert [r['seq'] for r in rows] == [1, 2, 3]
    assert isinstance(rows[0]['vec'], bytes) or isinstance(rows[0]['vec'], Binary)
    assert len(bytes(rows[0]['vec'])) == DIMS * 2
    assert rows[0]['f'] == {'svc': 'shop'}
    assert 'text' not in rows[0]


def test_upsert_same_id_replaces_and_bumps_seq(store):
    # given
    store.upsert_embeddings([doc(seed=1)])

    # when
    store.upsert_embeddings([doc(seed=2, digest='h2')])

    # then
    rows = list(store.embeddings.find())
    assert len(rows) == 1
    assert rows[0]['seq'] == 2
    assert rows[0]['hash'] == 'h2'


def test_wrong_length_unknown_kind_and_stray_fields_are_refused(store):
    # given
    short = doc()
    short['vec'] = short['vec'][:-2]

    # when
    calls = [lambda: store.upsert_embeddings([short], dims=DIMS)] + [lambda row=row: store.upsert_embeddings([row]) for row in [doc(kind='chat'), {**doc(), 'text': 'secret words'}, {**doc(), 'vec': b''}, {**doc(), 'vec': b'abc'}, {**doc(), 'vec': 'not bytes'}]]

    # then
    for call in calls:
        with pytest.raises(ValueError):
            call()
    assert store.embeddings.count_documents({}) == 0


def test_only_docs_carry_title_url_and_a_short_excerpt(store):
    # given
    page = doc(kind='doc', ref='p1', title='Runbook', url='https://example.com/r', excerpt='x' * 300)

    # when
    store.upsert_embeddings([page])

    # then
    saved = store.embeddings.find_one({'_id': 'doc:p1:0'})
    assert saved['title'] == 'Runbook'
    assert len(saved['excerpt']) == 300
    with pytest.raises(ValueError):
        store.upsert_embeddings([doc(kind='doc', ref='p2', excerpt='x' * 301)])
    with pytest.raises(ValueError):
        store.upsert_embeddings([doc(kind='run', ref='r9', title='Runbook')])


def test_every_kind_is_accepted(store):
    # given
    docs = [doc(kind=k, ref='x', seed=n) for n, k in enumerate(EMBED_KINDS)]

    # when
    store.upsert_embeddings(docs)

    # then
    assert store.embeddings.count_documents({}) == len(EMBED_KINDS)


def test_delete_writes_tombstones_with_seq(store):
    # given
    store.upsert_embeddings([doc(ref='a', part=0, seed=1), doc(ref='a', part=1, seed=2), doc(ref='b', seed=3)])

    # when
    removed = store.delete_embeddings('run', 'a')

    # then
    assert removed == 2
    assert [d['_id'] for d in store.embeddings.find()] == ['run:b:0']
    tombs = store.tombstones_since(0)
    assert sorted(t['_id'] for t in tombs) == ['run:a:0', 'run:a:1']
    assert all(t['seq'] > 3 for t in tombs)
    assert store.tombstones_since(max(t['seq'] for t in tombs)) == []
    assert store.delete_embeddings('run', 'missing') == 0
    with pytest.raises(ValueError):
        store.delete_embeddings('chat', 'a')


def test_embeddings_since_filters_by_fp_and_seq_and_limit(store):
    # given
    store.upsert_embeddings([doc(ref='a', seed=1), doc(ref='b', seed=2), doc(ref='c', fp='other', seed=3), doc(ref='d', seed=4)])

    # when
    first = store.embeddings_since('fp1', 0, 2)
    rest = store.embeddings_since('fp1', first[-1]['seq'], 10)

    # then
    assert [d['ref'] for d in first] == ['a', 'b']
    assert [d['ref'] for d in rest] == ['d']


def test_embedding_by_hash_reuses_only_the_active_fp(store):
    # given
    store.upsert_embeddings([doc(ref='a', digest='same', seed=1), doc(ref='b', fp='old', digest='gone', seed=2)])

    # when
    found = store.embedding_by_hash('fp1', 'same')

    # then
    assert found == vec(1)
    assert store.embedding_by_hash('fp1', 'gone') is None
    assert store.embedding_by_hash('fp1', 'none') is None


def plain(ops):
    return [{k: v for k, v in op.items() if k != 'ids'} for op in ops]


def test_queue_drains_in_order_and_dedupes_to_the_last_op(store):
    # given
    assert store.enqueue_embed('run', 'r1', 'upsert')
    assert store.enqueue_embed('card', 'k1', 'upsert')
    assert store.enqueue_embed('run', 'r1', 'delete')

    # when
    first = store.drain_embed_queue(2)
    rest = store.drain_embed_queue(10)

    # then
    assert plain(first) == [{'kind': 'run', 'ref': 'r1', 'op': 'upsert'}, {'kind': 'card', 'ref': 'k1', 'op': 'upsert'}]
    assert plain(rest) == [{'kind': 'run', 'ref': 'r1', 'op': 'delete'}]
    assert store.drain_embed_queue(10) == []


def test_drained_ops_stay_in_the_queue_until_acked(store):
    # given
    store.enqueue_embed('run', 'r1', 'upsert')
    store.enqueue_embed('run', 'r1', 'delete')
    store.enqueue_embed('card', 'k1', 'upsert')

    # when
    taken = store.drain_embed_queue(10)
    again = store.drain_embed_queue(10)
    left = store.embed_queue.count_documents({})
    removed = store.ack_embed_queue(taken)

    # then
    assert len(taken) == 2 and again == [] and left == 3
    assert removed == 3 and store.embed_queue.count_documents({}) == 0
    assert store.ack_embed_queue(taken) == 0 and store.ack_embed_queue([]) == 0


def test_an_unacked_claim_is_reclaimed_after_ten_minutes(store):
    # given
    store.enqueue_embed('run', 'r1', 'upsert')
    store.drain_embed_queue(10)
    store.embed_queue.update_many({}, {'$set': {'claimed': utc_now() - dt.timedelta(minutes=9)}})

    # when
    recent = store.drain_embed_queue(10)
    store.embed_queue.update_many({}, {'$set': {'claimed': utc_now() - dt.timedelta(minutes=11)}})
    stale = store.drain_embed_queue(10)

    # then
    assert recent == [] and plain(stale) == [{'kind': 'run', 'ref': 'r1', 'op': 'upsert'}]
    assert store.ack_embed_queue(stale) == 1


def test_enqueue_never_raises_when_the_insert_fails(store, monkeypatch, caplog):
    # given
    def boom(*a, **k):
        raise RuntimeError('mongo is gone')

    monkeypatch.setattr(store.embed_queue, 'insert_one', boom)

    # when
    with caplog.at_level(logging.WARNING, logger='skopax.store'):
        result = store.enqueue_embed('run', 'r1', 'upsert')

    # then
    assert result is False
    assert 'embed queue skipped' in caplog.text


def test_enqueue_refuses_bad_input_without_raising(store):
    # given
    calls = [('chat', 'r1', 'upsert'), ('run', '', 'upsert'), ('run', 'r1', 'nuke')]

    # when
    results = [store.enqueue_embed(*call) for call in calls]

    # then
    assert results == [False, False, False]
    assert store.embed_queue.count_documents({}) == 0


def test_state_docs_round_trip_and_unknown_names_are_refused(store):
    # given
    store.save_embed_state('embed-marks', {'run': {'cursor': 'abc'}})
    store.save_embed_state('embed-status', {'up': True, 'counts': {'run': 3}})

    # when
    marks = store.embed_state('embed-marks')

    # then
    assert marks == {'run': {'cursor': 'abc'}}
    assert store.embed_state('embed-status')['up'] is True
    assert store.embed_state('embed-marks') == marks
    with pytest.raises(ValueError):
        store.embed_state('investigator-stop')
    with pytest.raises(ValueError):
        store.save_embed_state('state', {})


def test_collections_and_indexes_exist_with_ttls(store):
    # given
    store.ensure_indexes()
    store.upsert_embeddings([doc(), doc(ref='r3', seed=3)])
    store.delete_embeddings('run', 'r1')
    store.enqueue_embed('run', 'r2', 'upsert')

    # when
    embeddings = store.embeddings.index_information()
    tombs = store.embed_tombstones.index_information()
    queue = store.embed_queue.index_information()

    # then
    keys = {tuple(v['key']) for v in embeddings.values()}
    assert (('fp', 1), ('seq', 1)) in keys
    assert (('kind', 1), ('ref', 1)) in keys
    ttl = [v for v in embeddings.values() if v['key'] == [('expireAt', 1)]]
    assert ttl and ttl[0]['expireAfterSeconds'] == 0
    assert any(v['key'] == [('seq', 1)] for v in tombs.values())
    assert [v for v in tombs.values() if v['key'] == [('at', 1)]][0]['expireAfterSeconds'] == 7 * 86400
    assert [v for v in queue.values() if v['key'] == [('at', 1)]][0]['expireAfterSeconds'] == 7 * 86400
    assert {'embeddings', 'embed_tombstones', 'embed_queue'} <= set(store.db.list_collection_names())


def test_expire_at_must_be_a_datetime(store):
    # given
    later = utc_now() + dt.timedelta(days=3)

    # when
    store.upsert_embeddings([doc(expireAt=later)])

    # then
    assert store.embeddings.find_one({})['expireAt'] is not None
    with pytest.raises(ValueError):
        store.upsert_embeddings([doc(ref='z', expireAt='tomorrow')])


def test_filter_fields_are_allow_listed_scalars_or_short_lists(store):
    # given
    good = {'svc': 'shop', 'slack': True, 'started': '2026-11-01T10:00:00Z', 'keys': ['a', 'b', 'c', 'd'], 'sev': 3, 'level': 1.5, 'ns': 'x', 'plugin': 'p', 'instance': 'i', 'space': 's',
            'trigger': 't', 'label': 'l'}

    # when
    store.upsert_embeddings([doc(f=good)])

    # then
    assert store.embeddings.find_one({})['f'] == good


@pytest.mark.parametrize('bad', [{'secret': 'x'}, {'svc': 'x' * 121}, {'svc': None}, {'svc': {'a': 1}}, {'keys': ['a'] * 5}, {'keys': ['x' * 121]}, {'keys': [1]}, {'keys': [['a']]},
                                 {'level': float('nan')}, {'svc': b'x'}, 'text', ['svc']])
def test_bad_filter_fields_are_refused(store, bad):
    # given
    row = doc(f=bad)

    # when
    with pytest.raises(ValueError):
        store.upsert_embeddings([row])

    # then
    assert store.embeddings.count_documents({}) == 0


def test_title_and_url_are_capped(store):
    # given
    ok = doc(kind='doc', ref='p1', title='t' * 200, url='https://example.com/' + 'u' * 480)

    # when
    store.upsert_embeddings([ok])

    # then
    with pytest.raises(ValueError):
        store.upsert_embeddings([doc(kind='doc', ref='p2', title='t' * 201)])
    with pytest.raises(ValueError):
        store.upsert_embeddings([doc(kind='doc', ref='p3', url='u' * 501)])
    assert store.embeddings.count_documents({}) == 1


def test_the_source_time_is_stored_and_must_be_a_datetime(store):
    # given
    when = dt.datetime(2026, 11, 1, 10, 0, tzinfo=dt.timezone.utc)

    # when
    store.upsert_embeddings([doc(at=when)])

    # then
    assert store.embeddings.find_one({})['at'] == when
    with pytest.raises(ValueError):
        store.upsert_embeddings([doc(ref='z', at='yesterday')])
