import dataclasses
import datetime as dt
import json

import pytest

from skopax import embedclient
from skopax.contract import Knowledge
from skopax.scheduler import Job, Scheduler
from skopax.semantic import indexer as ix
from skopax.semantic.indexer import Indexer
from skopax.store import Store
from tests.conftest import settings
from tests.fake_embedder import FakeEmbedder

NOW = 1_792_224_000.0
KEY = 'cpu|shop/checkout-service'
KEY_API = 'mem|shop/api'
LONG = ' '.join(f'Paragraph {n} describes how the checkout pool saturates under load and why retries pile up.' for n in range(60))


@pytest.fixture(autouse=True)
def clean_learned():
    embedclient.LEARNED.clear()
    yield
    embedclient.LEARNED.clear()


def conf(**over):
    return dataclasses.replace(settings(), **({'embed_url': 'http://skopax-embed', 'semantic_search_on': True} | over))


class Box:
    def __init__(self, store: Store, **over):
        self.store = store
        self.s = conf(**over)
        self.fake = FakeEmbedder()
        self.on = True
        self.items: list = []
        self.indexer = self.make()

    def make(self) -> Indexer:
        return Indexer(self.store, lambda: self.s, self.fake, lambda: self.on, lambda: list(self.items))

    def set(self, **over) -> None:
        self.s = dataclasses.replace(self.s, **over)

    def run(self, at: float = NOW) -> dict:
        return self.indexer.run_once(at)

    def rows(self, kind: str | None = None) -> list[dict]:
        return list(self.store.embeddings.find({'kind': kind} if kind else {}))

    def refs(self, kind: str) -> set[str]:
        return {r['ref'] for r in self.rows(kind)}


@pytest.fixture
def box(store) -> Box:
    return Box(store)


def add_run(store: Store, run_id='r1', outcome='done', trigger='review', keys=(KEY,), recap='The pool was exhausted by a retry storm.', started='2026-11-20T10:00:00Z', **extra) -> str:
    store.add_investigation({'id': run_id, 'outcome': outcome, 'trigger': trigger, 'keys': list(keys), 'ask': {'text': 'why is checkout slow'}, 'recap': recap, 'startedAt': started, **extra})
    return run_id


def add_card(store: Store, key=KEY, first='2026-11-20T09:00:00Z', what='CPU above 90 percent for 10 minutes', **extra) -> str:
    card = {'key': key, 'svc': 'checkout-service', 'label': 'High CPU', 'sub': 'pod-a', 'sev': 'warn', 'firstSeen': first, 'lastSeen': first, 'what': what, **extra}
    store.anomalies.replace_one({'_id': key}, {'_id': key, 'status': 'open', **card}, upsert=True)
    return store.record_card(card, 'cleared')


def drain(box: Box, passes: int = 6) -> dict:
    out = {}
    for n in range(passes):
        out = box.run(NOW + n * 60)
    return out


def test_done_run_is_indexed_through_the_outbox(box, store):
    # given
    add_run(store, outcome='running')
    store.update_investigation('r1', {'outcome': 'done'})

    # when
    status = box.run()

    # then
    rows = box.rows('run')
    assert [r['_id'] for r in rows] == ['run:r1:0']
    assert rows[0]['f']['svc'] == 'checkout-service' and rows[0]['f']['trigger'] == 'review'
    assert rows[0]['at'] is not None and rows[0]['fp'] == status['fp']
    assert any('retry storm' in t for t in box.fake.texts)
    assert store.embed_queue.count_documents({}) == 0


@pytest.mark.parametrize('outcome, trigger', [('held', 'review'), ('releasing', 'review'), ('dropped', 'review'), ('discarded', 'review'), ('done', 'chat')])
def test_runs_that_must_not_be_embedded_are_skipped(box, store, outcome, trigger):
    # given
    add_run(store, outcome='running', trigger=trigger, held={'until': '2999-01-01T00:00:00Z', 'text': 'held answer text'}, toolCalls=[{'input': 'tool secret input'}])
    store.update_investigation('r1', {'outcome': outcome})

    # when
    drain(box)

    # then
    assert box.rows('run') == []
    assert not any('held answer text' in t or 'tool secret input' in t for t in box.fake.texts)


def test_backfill_also_skips_those_runs(box, store):
    # given
    for n, (outcome, trigger) in enumerate([('held', 'review'), ('dropped', 'review'), ('discarded', 'review'), ('done', 'chat'), ('releasing', 'review')]):
        add_run(store, run_id=f'x{n}', outcome=outcome, trigger=trigger)
    add_run(store, run_id='ok1')

    # when
    drain(box)

    # then
    assert box.refs('run') == {'ok1'}


def test_a_held_run_released_later_is_indexed(box, store):
    # given
    add_run(store, outcome='held', held={'until': '2999-01-01T00:00:00Z', 'text': 'held answer text'})
    drain(box, 2)
    assert box.rows('run') == []

    # when
    claimed = store.claim_held('r1', '2026-11-21T00:00:00Z', 'tok')
    assert claimed is not None
    assert store.finish_release('r1', 'tok', {'recap': 'Released recap about the retry storm.'})
    drain(box, 2)

    # then
    assert box.refs('run') == {'r1'}
    assert not any('held answer text' in t for t in box.fake.texts)


def test_a_run_discarded_later_is_deleted(box, store):
    # given
    add_run(store)
    drain(box, 2)
    assert box.refs('run') == {'r1'}

    # when
    store.update_investigation('r1', {'outcome': 'discarded'})
    drain(box, 2)

    # then
    assert box.rows('run') == []
    assert store.tombstones_since(0)


def test_card_history_occurrence_is_indexed_with_its_what(box, store):
    # given
    did = add_card(store)

    # when
    drain(box, 2)

    # then
    assert box.refs('card') == {did}
    text = next(t for t in box.fake.texts if 'CPU above 90 percent' in t)
    assert 'what: CPU above 90 percent for 10 minutes' in text and 'outcome: ' in text
    row = box.rows('card')[0]
    assert row['f'] == {'svc': 'checkout-service', 'label': 'High CPU', 'sev': 'warn'}


def test_open_card_is_re_embedded_only_when_its_text_changes_and_deleted_on_clear(box, store):
    # given
    store.anomalies.insert_one({'_id': KEY, 'key': KEY, 'status': 'open', 'svc': 'checkout-service', 'label': 'High CPU', 'what': 'CPU high', 'sev': 'warn', 'firstSeen': '2026-11-20T09:00:00Z'})
    box.run()
    first = len(box.fake.texts)

    # when
    box.run(NOW + 60)
    same = len(box.fake.texts)
    store.anomalies.update_one({'_id': KEY}, {'$set': {'what': 'CPU very high now'}})
    box.run(NOW + 120)
    changed = len(box.fake.texts)
    store.anomalies.update_one({'_id': KEY}, {'$set': {'status': 'cleared'}})
    box.run(NOW + 180)

    # then
    assert (first, same, changed) == (1, 1, 2)
    assert box.rows('open') == []


def test_a_muted_open_card_is_deleted(box, store):
    # given
    store.anomalies.insert_one({'_id': KEY, 'key': KEY, 'status': 'open', 'svc': 'checkout-service', 'label': 'High CPU', 'what': 'CPU high', 'firstSeen': '2026-11-20T09:00:00Z'})
    box.run()
    assert box.refs('open') == {KEY}

    # when
    store.anomalies.update_one({'_id': KEY}, {'$set': {'muted': 'noise'}})
    box.run(NOW + 60)

    # then
    assert box.rows('open') == []


def test_deploy_and_deploy_report_are_indexed(box, store):
    # given
    store.add_deploys([{'ns': 'shop', 'svc': 'checkout-service', 'toImage': 'reg/app:2', 'fromImage': 'reg/app:1', 'at': '2026-11-20T08:00:00Z', 'imageBy': 'Fix pool leak', 'tickets': ['OPS-123']}])
    report = store.add_deploy_report({'summary': '1 deploy: 1 flagged', 'rows': [], 'brief': {'headline': 'Errors up', 'points': ['checkout-service: errors']}})

    # when
    drain(box, 2)

    # then
    refs = box.refs('change')
    assert any(r.startswith('deploy:') for r in refs) and f"report:{report['id']}" in refs
    assert any('Fix pool leak' in t for t in box.fake.texts) and any('Errors up' in t for t in box.fake.texts)
    for row in box.rows('change'):
        assert 'expireAt' not in row


def test_deleted_deploy_and_report_are_removed(box, store):
    # given
    store.add_deploys([{'ns': 'shop', 'svc': 'checkout-service', 'toImage': 'reg/app:2', 'fromImage': 'reg/app:1', 'at': '2026-11-20T08:00:00Z'}])
    report = store.add_deploy_report({'summary': '1 deploy', 'rows': []})
    drain(box, 2)
    assert len(box.refs('change')) == 2

    # when
    store.delete_deploy(store.deploys.find_one()['_id'])
    store.delete_deploy_report(report['id'])
    drain(box, 2)

    # then
    assert box.rows('change') == []


def test_knowledge_is_indexed_and_re_embedded_on_a_text_change(box, store):
    # given
    box.items = [('shop', '', Knowledge('runbook', 'Runbook', 'Restart the pool, then check the queue depth.'))]
    box.run()
    first = len(box.fake.texts)

    # when
    box.run(NOW + 120)
    same = len(box.fake.texts)
    box.items = [('shop', '', Knowledge('runbook', 'Runbook', 'Scale the pool first, then check the queue depth.'))]
    box.run(NOW + 180)

    # then
    assert (first, same, len(box.fake.texts)) == (1, 1, 2)
    assert box.refs('knowledge') == {'shop:runbook'}
    assert box.rows('knowledge')[0]['f'] == {'plugin': 'shop', 'instance': '', 'slack': False}


def test_knowledge_of_an_unusable_plugin_is_removed_at_once_while_others_remain(box, store):
    # given
    box.items = [('shop', '', Knowledge('runbook', 'Runbook', 'Restart the pool.')), ('held', '', Knowledge('guide', 'Guide', 'Held plugin guide text.'))]
    box.run()
    assert box.refs('knowledge') == {'shop:runbook', 'held:guide'}

    # when
    box.items = box.items[:1]
    box.run(NOW + 60)

    # then
    assert box.refs('knowledge') == {'shop:runbook'}


def test_an_empty_runtime_list_does_not_wipe_knowledge_until_it_stays_empty(box, store):
    # given
    box.items = [('shop', '', Knowledge('runbook', 'Runbook', 'Restart the pool.'))]
    box.run()
    box.items = []

    # when
    for n in range(ix.EMPTY_PASSES - 1):
        box.run(NOW + 60 * (n + 1))
    kept = box.refs('knowledge')
    flag = store.embed_state('embed-marks')['knowledge']
    box.items = [('shop', '', Knowledge('runbook', 'Runbook', 'Restart the pool.'))]
    box.run(NOW + 600)
    back = box.refs('knowledge')
    box.items = []
    for n in range(ix.EMPTY_PASSES):
        box.run(NOW + 700 + 60 * n)

    # then
    assert kept == {'shop:runbook'} and flag is False and back == {'shop:runbook'}
    assert box.rows('knowledge') == []
    assert len(box.fake.texts) == 1


def test_a_known_vector_is_reused_by_hash_without_a_pod_call(box, store):
    # given
    add_run(store, run_id='a')
    add_run(store, run_id='b')
    drain(box, 2)
    calls = len(box.fake.texts)
    assert box.refs('run') == {'a', 'b'}

    # when
    box.store.embeddings.delete_many({'ref': 'b'})
    box.store.enqueue_embed('run', 'b', 'upsert')
    box.run(NOW + 600)

    # then
    assert calls == 1
    assert len(box.fake.texts) == 1
    assert box.refs('run') == {'a', 'b'}


def test_embed_down_keeps_the_queue_and_nothing_raises(box, store):
    # given
    box.fake.down = 'down'
    add_run(store, outcome='running')
    store.update_investigation('r1', {'outcome': 'done'})

    # when
    status = box.run()

    # then
    assert status['up'] is False and status['kind'] == 'down'
    assert store.embed_queue.count_documents({}) == 1
    assert box.rows('run') == []
    assert store.embed_state('embed-status')['kind'] == 'down'


@pytest.mark.parametrize('kind', ['down', 'refused', 'auth', 'mismatch'])
def test_every_pod_failure_kind_stops_the_pass(box, store, kind):
    # given
    box.fake.down = kind
    add_run(store)

    # when
    status = box.run()

    # then
    assert status['kind'] == kind and status['up'] is False


def test_unacked_items_are_reclaimed_after_the_pod_returns(box, store):
    # given
    box.fake.down = 'down'
    add_run(store, outcome='running')
    store.update_investigation('r1', {'outcome': 'done'})
    box.run()
    store.embed_queue.update_many({}, {'$set': {'claimed': dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=1)}})

    # when
    box.fake.down = None
    box.run(NOW + 60)

    # then
    assert box.refs('run') == {'r1'}
    assert store.embed_queue.count_documents({}) == 0


def test_save_paths_never_raise_when_the_queue_is_broken(store, monkeypatch):
    # given
    def broken(*a, **k):
        raise RuntimeError('queue gone')
    monkeypatch.setattr(store.embed_queue, 'insert_one', broken)
    add_run(store, outcome='running')

    # when
    store.update_investigation('r1', {'outcome': 'done'})
    add_card(store)
    store.add_deploys([{'ns': 'shop', 'svc': 'checkout-service', 'toImage': 'reg/app:2', 'fromImage': 'reg/app:1', 'at': '2026-11-20T08:00:00Z'}])
    store.add_deploy_report({'summary': '1 deploy', 'rows': []})

    # then
    assert store.investigations.find_one({'_id': 'r1'})['outcome'] == 'done'


def test_the_job_never_raises_on_an_unexpected_error(box, store, monkeypatch):
    # given
    add_run(store)
    monkeypatch.setattr(box.indexer, 'drain', lambda ctx: (_ for _ in ()).throw(RuntimeError('boom')))

    # when
    status = box.run()

    # then
    assert status['kind'] == 'error' and status['up'] is None


def test_orphan_parts_are_deleted_when_a_document_shrinks(box, store):
    # given
    add_run(store, recap=LONG)
    drain(box, 2)
    long_parts = len(box.rows('run'))
    assert long_parts == 3

    # when
    store.update_investigation('r1', {'recap': 'Short recap.', 'outcome': 'done'})
    drain(box, 2)

    # then
    assert [r['part'] for r in box.rows('run')] == [0]


def test_backfill_respects_the_limit_and_resumes_after_a_restart(store):
    # given
    box = Box(store, embed_backfill_per_pass=5)
    for n in range(12):
        add_run(store, run_id=f'r{n:02d}', recap=f'Recap number {n} about a different failure.', started=f'2026-11-{10 + n:02d}T10:00:00Z')

    # when
    first = box.run()
    after_first = box.refs('run')
    restarted = Box(store, embed_backfill_per_pass=5)
    restarted.run(NOW + 60)
    second = len(restarted.refs('run'))
    restarted.run(NOW + 120)

    # then
    assert first['embedded'] == 5
    assert after_first == {f'r{n:02d}' for n in range(7, 12)}
    assert second == 10
    assert len(restarted.refs('run')) == 12
    assert [len(c) for c in restarted.fake.calls] == [5, 2]
    assert store.embed_state('embed-marks')['cursors']['run']['phase'] in ('orphan', 'done')


def test_the_backfill_limit_is_shared_across_kinds(store):
    # given
    box = Box(store, embed_backfill_per_pass=4)
    for n in range(4):
        add_run(store, run_id=f'r{n}', recap=f'Recap {n} unique text here.')
    for n in range(4):
        add_card(store, key=f'cpu|shop/svc{n}', what=f'what number {n}')
    store.anomalies.delete_many({})
    store.embed_queue.delete_many({})

    # when
    status = box.run()

    # then
    assert status['embedded'] == 4
    assert sum(len(c) for c in box.fake.calls) == 4
    assert len(box.rows('run')) + len(box.rows('card')) == 4


def test_backfill_with_a_zero_limit_embeds_nothing_but_the_outbox_still_runs(store):
    # given
    box = Box(store, embed_backfill_per_pass=0)
    add_run(store, run_id='old')
    store.embed_queue.delete_many({})
    add_run(store, run_id='new', outcome='running')
    store.update_investigation('new', {'outcome': 'done'})

    # when
    box.run()

    # then
    assert box.refs('run') == {'new'}


def test_backfill_fixes_a_stale_vector_and_removes_an_orphan(box, store):
    # given
    add_run(store, run_id='r1')
    drain(box, 3)
    store.investigations.update_one({'_id': 'r1'}, {'$set': {'recap': 'A changed recap nobody was told about.'}})
    ghost = {'kind': 'run', 'ref': 'ghost', 'part': 0, 'fp': box.rows('run')[0]['fp'], 'hash': 'h', 'vec': box.rows('run')[0]['vec'], 'f': {}}
    store.upsert_embeddings([ghost])
    marks = store.embed_state('embed-marks')
    marks['cursors'] = {}
    store.save_embed_state('embed-marks', marks)

    # when
    drain(box, 6)

    # then
    assert box.refs('run') == {'r1'}
    assert any('changed recap' in t for t in box.fake.texts)


def test_a_new_cycle_starts_after_the_reconcile_interval(box, store):
    # given
    add_run(store)
    drain(box, 4)
    assert store.embed_state('embed-marks')['cursors']['run']['phase'] == 'done'
    store.investigations.update_one({'_id': 'r1'}, {'$set': {'recap': 'Updated quietly while the pod was down.'}})

    # when
    box.run(NOW + 600)
    still = any('Updated quietly' in t for t in box.fake.texts)
    drain_at = NOW + ix.RECONCILE_S + 600
    for n in range(4):
        box.run(drain_at + n * 60)

    # then
    assert not still
    assert any('Updated quietly' in t for t in box.fake.texts)


def test_a_fingerprint_change_rebuilds_and_then_deletes_old_vectors(store):
    # given
    box = Box(store, embed_backfill_per_pass=3)
    for n in range(7):
        add_run(store, run_id=f'r{n}', recap=f'Recap {n} for the rebuild.')
    for n in range(4):
        box.run(NOW + n * 60)
    old_fp = box.rows('run')[0]['fp']
    assert len(box.rows('run')) == 7

    # when
    box.set(embed_passage_prefix='doc: ')
    mid = box.run(NOW + 1000)
    partial = {r['fp'] for r in box.rows('run')}
    for n in range(6):
        box.run(NOW + 1100 + n * 60)
    final = box.run(NOW + 2000)

    # then
    assert mid['fp'] != old_fp and mid['rebuilding']['of'] == 7 and old_fp in partial and len(partial) == 2
    assert {r['fp'] for r in box.rows('run')} == {final['fp']}
    assert len(box.rows('run')) == 7 and 'rebuilding' not in final
    assert store.embed_state('embed-status')['fp'] == final['fp']


def test_expire_at_mirrors_the_source_retention(box, store):
    # given
    add_run(store)
    did = add_card(store)
    started = store.investigations.find_one({'_id': 'r1'})['startedAtDt']
    at = store.card_history.find_one({'_id': did})['atDt']

    # when
    drain(box, 4)

    # then
    run = box.rows('run')[0]['expireAt']
    card = box.rows('card')[0]['expireAt']
    assert abs((run - started).total_seconds() - store.investigate_days * 86400) < 1
    assert abs((card - at).total_seconds() - store.history_days * 86400) < 1
    store.embeddings.delete_many({})
    store.embed_queue.delete_many({})
    store.save_embed_state('embed-marks', {})
    box.indexer = box.make()
    drain(box, 4)
    assert abs((box.rows('run')[0]['expireAt'] - started).total_seconds() - store.investigate_days * 86400) < 1


def test_unknown_dims_make_one_probe_to_learn_them(store):
    # given
    box = Box(store, embed_model='custom/model')
    box.fake.model = 'custom/model'
    add_run(store)

    # when
    status = box.run()

    # then
    assert box.fake.calls[0] == ['ok'] and status['dims'] == 384 and status['fp']
    assert box.refs('run') == {'r1'}


def test_unknown_dims_that_stay_unknown_skip_the_pass(store):
    # given
    box = Box(store, embed_model='custom/model')
    add_run(store)

    # when
    status = box.run()

    # then
    assert status['fp'] is None and status['up'] is False and box.rows() == []
    assert box.fake.calls == [['ok']]


def test_status_has_counts_progress_and_no_secrets(store):
    # given
    box = Box(store, embed_api_key='k-secret-key-123', embed_url='http://skopax-embed/v1/private-path')
    add_run(store)
    add_card(store)

    # when
    drain(box, 4)
    status = store.embed_state('embed-status')

    # then
    assert status['up'] is True and status['counts']['run'] == 1 and status['counts']['card'] == 1
    assert status['backfill'] == {'run': 'done', 'card': 'done', 'deploy': 'done', 'report': 'done', 'errtpl': 'done'}
    assert status['capped'] is False and status['at'] and status['fp']
    blob = json.dumps(status, default=str)
    assert 'k-secret-key-123' not in blob and 'private-path' not in blob and 'skopax-embed' not in blob


def test_status_flags_a_capped_index(store):
    # given
    box = Box(store, embed_max_vectors=1)
    add_run(store, run_id='a', recap='First recap text.')
    add_run(store, run_id='b', recap='Second recap text.')

    # when
    status = drain(box, 3)

    # then
    assert status['capped'] is True


def test_feature_off_or_empty_url_means_no_pass(box, store):
    # given
    add_run(store)

    # when
    box.on = False
    off = box.run()
    box.on = True
    box.set(embed_url='')
    blank = box.run()
    box.set(embed_url='http://skopax-embed', embed_sources=())
    none = box.run()

    # then
    assert (off, blank, none) == (None, None, None)
    assert box.fake.calls == []
    assert store.embed_state('embed-status')['kind'] == 'off' and store.embed_state('embed-status')['up'] is None
    assert box.indexer.enabled() is False


def test_a_disabled_source_is_not_indexed(store):
    # given
    box = Box(store, embed_sources=('cards',))
    add_run(store)
    add_card(store)

    # when
    drain(box, 4)

    # then
    assert box.rows('run') == [] and len(box.rows('card')) == 1


def test_the_job_runs_once_across_two_schedulers(store):
    # given
    first, second = Box(store), Box(Store(store.db))
    add_run(store)
    clock = lambda: NOW + ix.OFFSET + 5
    pods = [Scheduler(store, [Job(ix.NAME, ix.PERIOD, ix.OFFSET, first.indexer.run_once, first.indexer.enabled)], clock=clock),
            Scheduler(Store(store.db), [Job(ix.NAME, ix.PERIOD, ix.OFFSET, second.indexer.run_once, second.indexer.enabled)], clock=clock)]

    # when
    started = [pod.run_due() for pod in pods]

    # then
    assert started == [[ix.NAME], []]
    assert first.fake.calls and not second.fake.calls


def test_nothing_forbidden_ever_reaches_the_pod(box, store):
    # given
    add_run(store, held={'text': 'held proposal text', 'writes': ['write text'], 'proposed': 'proposed text'}, toolCalls=[{'name': 'x', 'input': 'tool input text', 'output': 'tool output text'}],
            guard={'verdict': 'guard verdict text'})
    add_card(store, guard='guard card text')

    # when
    drain(box, 4)

    # then
    blob = ' '.join(box.fake.texts) + json.dumps([r['f'] for r in box.rows()])
    for text in ('held proposal', 'write text', 'proposed text', 'tool input', 'tool output', 'guard verdict', 'guard card'):
        assert text not in blob
    assert 'vec' in box.rows()[0] and 'text' not in box.rows()[0]


def test_a_slow_pass_stops_between_batches_and_releases_unprocessed_claims(store, monkeypatch):
    # given
    box = Box(store, embed_batch=1)
    for n in range(4):
        add_run(store, run_id=f'q{n}', outcome='running', recap=f'Distinct recap {n} for the deadline test.')
        store.update_investigation(f'q{n}', {'outcome': 'done'})
    store.save_embed_state('embed-marks', {'fp': 'x'})
    real = box.fake.embed_passages
    ticks = {'n': 0}

    def slow(texts):
        ticks['n'] += 1
        if ticks['n'] >= 2:
            monkeypatch.setattr(ix.Ctx, 'late', lambda self: True)
        return real(texts)
    box.fake.embed_passages = slow

    # when
    box.run()

    # then
    assert len(box.fake.calls) == 2
    assert len(box.rows('run')) == 2
    left = list(store.embed_queue.find())
    assert len(left) == 2 and all(not d.get('claimed') for d in left)


def test_commit_embeds_in_batches_of_the_setting(store):
    # given
    box = Box(store, embed_batch=2)
    for n in range(5):
        add_run(store, run_id=f'b{n}', recap=f'Batch recap {n} unique words.')

    # when
    drain(box, 2)

    # then
    assert [len(c) for c in box.fake.calls] == [2, 2, 1]


def test_a_slow_pod_cannot_move_the_marks_backwards(box, store):
    # given
    add_run(store)
    box.run()
    stale = store.embed_state('embed-marks')

    # when
    box.run(NOW + 60)
    newer = store.embed_state('embed-marks')
    kept = store.save_embed_marks({**stale, 'cursors': {}}, stale['ver'])

    # then
    assert kept is False
    assert store.embed_state('embed-marks')['ver'] == newer['ver'] and store.embed_state('embed-marks')['cursors'] == newer['cursors']


def test_a_fingerprint_rebuild_flag_survives_a_stale_save(store):
    # given
    box = Box(store)
    add_run(store)
    drain(box, 3)
    box.set(embed_passage_prefix='doc: ')
    box.run(NOW + 1000)
    stale = {'fp': 'old', 'cursors': {}}
    marks = store.embed_state('embed-marks')

    # when
    ok = store.save_embed_marks(stale, 1)

    # then
    assert ok is False and store.embed_state('embed-marks')['fp'] == marks['fp']


def test_open_cards_are_not_rebuilt_while_their_signature_is_unchanged(box, store, monkeypatch):
    # given
    store.anomalies.insert_one({'_id': KEY, 'key': KEY, 'status': 'open', 'svc': 'checkout-service', 'label': 'High CPU', 'what': 'CPU high', 'firstSeen': '2026-11-20T09:00:00Z'})
    box.run()
    built = []
    real = ix.chunks.open_passages
    monkeypatch.setattr(ix.chunks, 'open_passages', lambda doc, tally=None: built.append(doc) or real(doc, tally))

    # when
    box.run(NOW + 60)
    store.anomalies.update_one({'_id': KEY}, {'$set': {'what': 'CPU changed'}})
    box.run(NOW + 120)

    # then
    assert len(built) == 1


def test_ops_of_a_disabled_source_stay_queued_until_it_is_enabled(store):
    # given
    box = Box(store, embed_sources=('cards',))
    add_run(store, outcome='running')
    store.update_investigation('r1', {'outcome': 'done'})
    drain(box, 2)
    assert store.embed_queue.count_documents({'kind': 'run'}) == 1

    # when
    box.set(embed_sources=('runs', 'cards'))
    box.run(NOW + 600)

    # then
    assert box.refs('run') == {'r1'}
    assert store.embed_queue.count_documents({'kind': 'run'}) == 0


def test_the_gate_skips_upserts_when_off_but_deletes_are_always_queued(store):
    # given
    box = Box(store)
    store.embed_gate = box.indexer.wants
    box.on = False

    # when
    upsert = store.enqueue_embed('run', 'r1', 'upsert')
    delete = store.enqueue_embed('run', 'r1', 'delete')
    box.on = True
    box.set(embed_sources=('cards',))
    off_source = store.enqueue_embed('run', 'r2', 'upsert')
    on_source = store.enqueue_embed('card', 'c1', 'upsert')
    box.set(embed_url='')
    no_url = store.enqueue_embed('card', 'c2', 'upsert')

    # then
    assert (upsert, delete, off_source, on_source, no_url) == (False, True, False, True, False)


def test_a_mongo_error_does_not_mark_the_pod_down(box, store, monkeypatch):
    # given
    add_run(store)
    box.run()
    monkeypatch.setattr(store.anomalies, 'find', lambda *a, **k: (_ for _ in ()).throw(RuntimeError('mongo gone')))

    # when
    status = box.run(NOW + 60)

    # then
    assert status['kind'] == 'error' and status['up'] is True


def test_runs_without_a_sort_key_are_included(box, store):
    # given
    add_run(store, run_id='nokey')
    store.investigations.update_one({'_id': 'nokey'}, {'$unset': {'startedAt': ''}})

    # when
    drain(box, 6)

    # then
    assert box.refs('run') == {'nokey'}


def test_the_orphan_phase_has_a_per_pass_budget(store, monkeypatch):
    # given
    box = Box(store)
    monkeypatch.setattr(ix, 'ORPHAN_PAGES', 1)
    monkeypatch.setattr(ix, 'PAGE', 2)
    for n in range(6):
        store.upsert_embeddings([{'kind': 'run', 'ref': f'ghost{n}', 'part': 0, 'fp': 'old', 'hash': f'h{n}', 'vec': bytes(768), 'f': {}}])

    # when
    box.run()

    # then
    assert len(box.rows('run')) == 4


def test_card_history_backfill_inserts_are_queued(box, store):
    # given
    doc = {'_id': 'cpu|shop/x|2026-11-20T09:00:00Z', 'key': 'cpu|shop/x', 'firstSeen': '2026-11-20T09:00:00Z', 'at': '2026-11-20T09:00:00Z', 'backfill': True}

    # when
    store.add_card_history_backfill([doc])

    # then
    assert store.embed_queue.count_documents({'kind': 'card', 'ref': doc['_id']}) == 1


def test_the_job_stays_listed_until_the_off_status_is_written(box, store):
    # given
    box.run()
    box.on = False

    # when
    listed = box.indexer.active()
    box.run(NOW + 60)
    after_off = box.indexer.active()

    # then
    assert listed is True and after_off is False
    assert store.embed_state('embed-status')['kind'] == 'off'


def test_guard_refused_runs_are_never_indexed(box, store):
    # given
    add_run(store, 'ok')
    add_run(store, 'refused', error='Refused by the guard', recap='refused text')

    # when
    drain(box)

    # then
    assert box.refs('run') == {'ok'}
    assert ix.Indexer.eligible(box.indexer, ix.BY_NAME['run'], {'outcome': 'done', 'trigger': 'review', 'error': 'Refused by the guard'}) is False
    assert ix.chunks.run_passages({'_id': 'x', 'outcome': 'done', 'trigger': 'review', 'ask': {'text': 'q'}, 'recap': 'a', 'error': 'Refused by the guard'}) == []
