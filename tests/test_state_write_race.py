import copy
import datetime as dt
import logging
import sys
import threading
import time

from types import SimpleNamespace

import mongomock
import pytest
from pymongo.errors import PyMongoError

from skopax import engine
from skopax.engine import PassResult, anomaly
from skopax.store import STATE_ID, Conflict, Store, iso, utc_now
from tests.conftest import anomaly as stored_card, state

SEED, NEW = 'err|shop/cart-service', 'err|shop/checkout-service'


def card(src: str, svc: str) -> dict:
    found = anomaly(src, 'err', 'shop', svc, '', 'warn', 10, 1, 1, '', 'Error rate', 'demo')
    found['ranKey'] = src
    return found


class Serial:
    def __init__(self, coll, lock: threading.RLock):
        self.coll, self.lock = coll, lock

    def __getattr__(self, name):
        found = getattr(self.coll, name)
        if not callable(found):
            return found

        def call(*args, **kwargs):
            with self.lock:
                out = found(*args, **kwargs)
                return list(out) if isinstance(out, mongomock.collection.Cursor) else out
        return call


class SerialDb:
    def __init__(self, db):
        self.db, self.lock = db, threading.RLock()

    def __getitem__(self, name):
        return Serial(self.db[name], self.lock)

    def __getattr__(self, name):
        return getattr(self.db, name)


def fresh_store(serial: bool = False) -> Store:
    db = mongomock.MongoClient(tz_aware=True)['skopax']
    return Store(SerialDb(db) if serial else db)


def passed(name: str, *found: dict) -> PassResult:
    return PassResult(name, True, 'ok', list(found), {}, (name,))


def gate_after_claim(store: Store, names: tuple[str, ...]) -> dict[str, tuple[threading.Event, threading.Event]]:
    gates = {name: (threading.Event(), threading.Event()) for name in names}
    real = store._claim

    def claim(*args, **kwargs):
        out = real(*args, **kwargs)
        gate = gates.get(threading.current_thread().name)
        if gate is not None and args and args[0] is not None:
            gate[0].set()
            gate[1].wait(5)
        return out

    store._claim = claim
    return gates


def test_a_pass_that_overlaps_another_keeps_both_pass_records_and_the_new_card():
    # given
    store = fresh_store(serial=True)
    now = time.time()
    engine.apply(store, [passed('seed', card('seed', 'cart-service'))], now)
    gates = gate_after_claim(store, ('alpha', 'beta'))
    alpha = threading.Thread(name='alpha', target=lambda: engine.apply(store, [passed('alpha', card('alpha', 'checkout-service'))], now + 1))
    beta = threading.Thread(name='beta', target=lambda: engine.apply(store, [passed('beta')], now + 1))

    # when
    alpha.start()
    assert gates['alpha'][0].wait(5)
    beta.start()
    gates['beta'][0].wait(0.5)
    gates['alpha'][1].set()
    alpha.join(5)
    gates['beta'][1].set()
    beta.join(5)
    final = store.load()

    # then
    assert not alpha.is_alive() and not beta.is_alive()
    assert set(final['sources']) == {'seed', 'alpha', 'beta'}
    assert set(final['serverPasses']) == {'seed', 'alpha', 'beta'}
    assert {a['key'] for a in final['anomalies']} == {SEED, NEW}
    assert (NEW, 'new') in {(e['key'], e['ev']) for e in final['history']}
    assert not [d for d in store.card_history.find({'key': NEW}) if any(e['ev'] == 'left' for e in d.get('events') or [])]


def test_many_overlapping_writers_lose_no_pass_record_history_event_or_card(request):
    # given
    store = fresh_store(serial=True)
    now = time.time()
    engine.apply(store, [passed('seed', card('seed', 'cart-service'))], now)
    applied, failed = [], []
    request.addfinalizer(lambda old=sys.getswitchinterval(): sys.setswitchinterval(old))
    sys.setswitchinterval(1e-5)

    def writer(i: int) -> None:
        for r in range(12):
            name = f'w{i}r{r}'
            try:
                engine.apply(store, [passed(name, card(name, f'svc-{i}-{r}'))], now + 1 + r, attempts=40)
                applied.append(name)
            except engine.Conflict:
                failed.append(name)

    threads = [threading.Thread(target=writer, args=(i,)) for i in range(4)]

    # when
    for t in threads:
        t.start()
    for t in threads:
        t.join(60)
    final = store.load()
    keys = {a['key'] for a in final['anomalies']}
    events = {(e['key'], e['ev']) for e in final['history']}
    left = [d['key'] for d in store.card_history.find() if any(e['ev'] == 'left' for e in d.get('events') or [])]

    # then
    assert not failed and len(applied) == 48
    assert [n for n in applied if n not in final['sources'] or n not in final['serverPasses']] == []
    assert [n for n in applied if f'err|shop/svc-{n[1]}-{n[3:]}' not in keys] == []
    assert [n for n in applied if (f'err|shop/svc-{n[1]}-{n[3:]}', 'new') not in events] == []
    assert left == []


def test_a_save_keeps_pass_records_history_events_and_push_lines_written_beside_it():
    # given
    store = fresh_store()
    store.save({**state(stored_card(SEED)), 'initialized': True, 'serverPasses': {'prom': {'at': 'a', 'status': 'ok'}}, 'serverPushes': [{'line': 'old'}],
                'history': [{'key': SEED, 'ev': 'new'}]}, None)
    loaded = store.load()
    mine = copy.deepcopy(loaded)
    mine['sources']['mine'] = 'ok'
    mine['serverPasses']['mine'] = {'at': 'b', 'status': 'ok'}
    mine['history'] = [{'key': 'mine', 'ev': 'new'}] + mine['history']
    mine['serverPushes'] = [{'line': 'mine'}] + mine['serverPushes']
    store.meta.update_one({'_id': STATE_ID}, {'$set': {'sources.other': 'ok', 'serverPasses.other': {'at': 'c', 'status': 'ok'}},
                                              '$push': {'history': {'$each': [{'key': 'other', 'ev': 'new'}], '$position': 0},
                                                        'serverPushes': {'$each': [{'line': 'other'}], '$position': 0}}})

    # when
    store.save_state(mine, loaded['rev'], loaded)
    final = store.load()

    # then
    assert final['sources'] == {'prom': 'ok', 'mine': 'ok', 'other': 'ok'}
    assert set(final['serverPasses']) == {'prom', 'mine', 'other'}
    assert [e['key'] for e in final['history']] == ['mine', 'other', SEED]
    assert [p['line'] for p in final['serverPushes']] == ['mine', 'other', 'old']


def test_a_save_drops_a_pass_record_it_removed_and_keeps_the_list_cap():
    # given
    store = fresh_store()
    store.save({**state(), 'sources': {'prom': 'ok', 'gone': 'ok'}, 'serverPushes': [{'line': 'one'}, {'line': 'two'}]}, None)
    loaded = store.load()
    mine = copy.deepcopy(loaded)
    del mine['sources']['gone']
    mine['serverPushes'] = ([{'line': 'three'}] + mine['serverPushes'])[:2]

    # when
    store.save_state(mine, loaded['rev'], loaded)
    final = store.load()

    # then
    assert final['sources'] == {'prom': 'ok'}
    assert [p['line'] for p in final['serverPushes']] == ['three', 'one']


def test_a_save_deletes_only_the_cards_it_loaded_and_dropped():
    # given
    store = fresh_store()
    store.save({**state(stored_card(SEED)), 'initialized': True}, None)
    loaded = store.load()
    store.anomalies.insert_one({**stored_card(NEW, firstSeen='2026-10-10T09:00:00Z'), '_id': NEW, 'pos': 1})
    mine = {**copy.deepcopy(loaded), 'anomalies': []}

    # when
    store.save_state(mine, loaded['rev'], loaded)

    # then
    assert [a['key'] for a in store.load()['anomalies']] == [NEW]
    assert store.card_history.count_documents({'key': NEW}) == 0


def test_a_reader_waits_while_a_state_write_is_in_flight():
    # given
    store = fresh_store(serial=True)
    store.save(state(stored_card(SEED)), None)
    store.meta.update_one({'_id': STATE_ID}, {'$set': {'writing': {'id': 'live', 'at': iso(utc_now())}}})
    done = threading.Timer(0.3, lambda: store.meta.update_one({'_id': STATE_ID}, {'$unset': {'writing': ''}}))

    # when
    started = time.monotonic()
    done.start()
    loaded = store.load()
    waited = time.monotonic() - started

    # then
    assert waited >= 0.25 and 'writing' not in loaded


def test_a_writer_that_died_mid_write_holds_the_state_only_until_its_lease_ends():
    # given
    store = fresh_store()
    store.save(state(stored_card(SEED)), None)
    dead = {'id': 'dead', 'at': iso(utc_now() - dt.timedelta(minutes=5))}
    store.meta.update_one({'_id': STATE_ID}, {'$set': {'writing': dead}})

    # when
    started = time.monotonic()
    loaded = store.load()
    waited = time.monotonic() - started
    rev, _ = store.save_state(copy.deepcopy(loaded), loaded['rev'], loaded)

    # then
    assert waited < 1 and 'writing' not in loaded
    assert rev == loaded['rev'] + 1 and 'writing' not in store.meta.find_one({'_id': STATE_ID})


def test_a_save_without_a_base_rev_waits_for_a_write_in_flight_instead_of_failing():
    # given
    store = fresh_store(serial=True)
    store.save(state(stored_card(SEED)), None)
    rev = store.rev()['rev']
    store.meta.update_one({'_id': STATE_ID}, {'$set': {'writing': {'id': 'live', 'at': iso(utc_now())}}})
    done = threading.Timer(0.3, lambda: store.meta.update_one({'_id': STATE_ID}, {'$unset': {'writing': ''}}))

    # when
    done.start()
    saved = store.save(state(stored_card(SEED), stored_card(NEW)), None)

    # then
    assert saved == rev + 1
    assert [a['key'] for a in store.load()['anomalies']] == [SEED, NEW]


def test_a_pass_name_that_cannot_be_a_field_path_is_still_saved_whole():
    # given
    store = fresh_store()
    store.save({**state(), 'sources': {'prom': 'ok'}, 'serverPasses': {'prom': {'at': 'a', 'status': 'ok'}}}, None)
    loaded = store.load()
    mine = copy.deepcopy(loaded)
    mine['sources']['apm.eu'] = 'ok'
    mine['serverPasses']['$odd'] = {'at': 'b', 'status': 'ok'}

    # when
    store.save_state(mine, loaded['rev'], loaded)
    final = store.load()

    # then
    assert final['sources'] == {'prom': 'ok', 'apm.eu': 'ok'}
    assert set(final['serverPasses']) == {'prom', '$odd'}


class Broken:
    def __init__(self, coll):
        self.coll = coll

    def __getattr__(self, name):
        if name in ('update_one', 'replace_one'):
            def fail(*args, **kwargs):
                raise PyMongoError('down')
            return fail
        return getattr(self.coll, name)


def test_a_state_write_that_fails_half_way_does_not_hold_the_readers():
    # given
    store = fresh_store()
    store.save(state(stored_card(SEED)), None)
    loaded = store.load()
    store.anomalies = Broken(store.anomalies)

    # when
    try:
        store.save_state(copy.deepcopy(loaded), loaded['rev'], loaded)
    except PyMongoError:
        pass

    # then
    assert 'writing' not in store.meta.find_one({'_id': STATE_ID})


@pytest.mark.parametrize('merged', [True, False])
def test_a_writer_whose_lease_was_taken_over_writes_no_meta_and_gets_a_conflict(merged):
    # given
    store = fresh_store()
    store.save({**state(stored_card(SEED)), 'sources': {'prom': 'ok'}}, None)
    loaded = store.load()
    mine = copy.deepcopy(loaded)
    mine['sources']['mine'] = 'ok'
    real = store._claim

    def claim(base_rev):
        out = real(base_rev)
        store.meta.update_one({'_id': STATE_ID}, {'$set': {'writing': {'id': 'other', 'at': iso(utc_now())}}})
        return out

    store._claim = claim

    # when
    with pytest.raises(Conflict):
        store.save_state(mine, loaded['rev'], loaded if merged else None)

    # then
    meta = store.meta.find_one({'_id': STATE_ID})
    assert meta['sources'] == {'prom': 'ok'} and meta['writing']['id'] == 'other'


def test_a_writer_that_waited_for_another_stamps_its_lease_when_it_takes_it(monkeypatch):
    # given
    store = fresh_store()
    store.save(state(stored_card(SEED)), None)
    start = utc_now()
    clock = {'now': start}
    store.meta.update_one({'_id': STATE_ID}, {'$set': {'writing': {'id': 'live', 'at': iso(start)}}})

    def sleep(seconds: float) -> None:
        clock['now'] += dt.timedelta(seconds=20)
        store.meta.update_one({'_id': STATE_ID, 'writing.id': 'live'}, {'$unset': {'writing': ''}})

    monkeypatch.setattr('skopax.store.utc_now', lambda: clock['now'])
    monkeypatch.setattr('skopax.store.time', SimpleNamespace(sleep=sleep, monotonic=time.monotonic))
    leases = []
    real = store._claim

    def claim(base_rev):
        leases.append(store.meta.find_one({'_id': STATE_ID})['writing']['at'])
        return real(base_rev)

    store._claim = claim

    # when
    store.save(state(stored_card(SEED), stored_card(NEW)), None)

    # then
    assert leases == [iso(start + dt.timedelta(seconds=20))]


def paused_in_claim(store: Store, name: str) -> tuple[threading.Event, threading.Event]:
    reached, go = threading.Event(), threading.Event()
    real = store._claim

    def claim(base_rev):
        out = real(base_rev)
        if threading.current_thread().name == name:
            reached.set()
            go.wait(5)
        return out

    store._claim = claim
    return reached, go


CARD_WRITES = {
    'assess': (lambda s: s.assess(SEED, ['a bad deploy'], None, None), lambda c: c.get('suspects') == ['a bad deploy']),
    'note': (lambda s: s.note(SEED, 'looked at it'), lambda c: (c.get('note') or {}).get('text') == 'looked at it'),
    'triage': (lambda s: s.triage(SEED, 'progress'), lambda c: c.get('triage') == 'progress'),
    'severity': (lambda s: s.set_severity(SEED, 'crit', 'alice@example.com'), lambda c: c.get('sev') == 'crit'),
    'draft': (lambda s: s.slack_draft(SEED, 'Draft text', 'alice@example.com'), lambda c: (c.get('slackDraft') or {}).get('text') == 'Draft text'),
    'investigate': (lambda s: s.claim_card(SEED, 'run-1'), lambda c: c.get('triage') == 'investigating'),
}


@pytest.mark.parametrize('name', sorted(CARD_WRITES))
def test_a_card_write_that_lands_during_a_state_write_waits_for_it_and_survives(name):
    # given
    store = fresh_store(serial=True)
    store.save({**state(stored_card(SEED)), 'initialized': True}, None)
    board = store.load()
    write, landed = CARD_WRITES[name]
    reached, go = paused_in_claim(store, 'board')
    saver = threading.Thread(name='board', target=lambda: store.save(copy.deepcopy(board), None))
    clicker = threading.Thread(target=lambda: write(store))

    # when
    saver.start()
    assert reached.wait(5)
    clicker.start()
    clicker.join(0.3)
    go.set()
    saver.join(5)
    clicker.join(5)

    # then
    assert not saver.is_alive() and not clicker.is_alive()
    assert landed(store.anomaly(SEED))


def muting(key: str) -> dict:
    return {'pattern': key, 'until': None, 'reason': 'noisy', 'kind': 'mute', 'at': '2026-11-16T10:00:00Z'}


@pytest.mark.parametrize('action, first, after', [('mute', [], {SEED, NEW}), ('unmute', [SEED], {NEW})])
def test_a_mute_change_that_lands_during_a_state_write_keeps_the_mute_that_write_adds(action, first, after):
    # given
    store = fresh_store(serial=True)
    store.save({**state(stored_card(SEED), stored_card(NEW)), 'initialized': True, 'muted': [muting(k) for k in first]}, None)
    loaded = store.load()
    mine = {**copy.deepcopy(loaded), 'muted': copy.deepcopy(loaded['muted']) + [muting(NEW)]}
    reached, go = paused_in_claim(store, 'mute')
    change = (lambda: store.mute(SEED, 'known noise', None, 'known')) if action == 'mute' else (lambda: store.unmute(SEED))
    muter = threading.Thread(name='mute', target=change)
    saver = threading.Thread(target=lambda: store.save_state(mine, None, loaded))

    # when
    muter.start()
    assert reached.wait(5)
    saver.start()
    saver.join(0.3)
    go.set()
    muter.join(5)
    saver.join(5)

    # then
    assert not saver.is_alive() and not muter.is_alive()
    assert {m['pattern'] for m in store.load()['muted']} == after


def test_a_board_save_without_a_base_rev_never_sets_the_rev_back():
    # given
    store = fresh_store(serial=True)
    store.save({**state(stored_card(SEED)), 'initialized': True}, None)
    board = store.load()
    reached, go = paused_in_claim(store, 'board')
    seen = []
    saver = threading.Thread(name='board', target=lambda: seen.append(store.save(copy.deepcopy(board), None)))
    noter = threading.Thread(target=lambda: seen.append(store.note(SEED, 'looked at it')))

    # when
    saver.start()
    assert reached.wait(5)
    noter.start()
    noter.join(0.3)
    go.set()
    saver.join(5)
    noter.join(5)

    # then
    assert len(seen) == 2 and store.rev()['rev'] == max(seen)


def busy_for(store: Store, attempts: int) -> list:
    tries = []
    store.meta.update_one({'_id': STATE_ID}, {'$set': {'writing': {'id': 'busy', 'at': iso(utc_now())}}})

    def settled():
        tries.append(1)
        if len(tries) == attempts:
            store.meta.update_one({'_id': STATE_ID, 'writing.id': 'busy'}, {'$unset': {'writing': ''}})
        return {k: v for k, v in store._meta().items() if k != 'writing'}

    store._settled = settled
    return tries


LEASED_WRITES = {
    'investigate': ({}, lambda s: s.claim_card(SEED, 'run-1'), lambda c: c.get('triage') == 'investigating'),
    'release': ({'triage': 'investigating', 'investigator': {'runId': 'run-1', 'at': '2026-11-16T10:00:00Z'}}, lambda s: s.release_card(SEED, 'run-1'),
                lambda c: 'triage' not in c and 'investigator' not in c),
    'stop auto-send': ({'autoSend': {'at': '2026-11-16T10:00:00Z', 'draftAt': '2026-11-16T09:59:00Z', 'draftHash': 'h'}}, lambda s: s.stop_anomaly_auto_send(SEED),
                       lambda c: 'autoSend' not in c),
    'slack head': ({'slackPost': {'ts': '1.5', 'at': '2026-11-16T10:00:00Z'}}, lambda s: s.slack_post_head(SEED, '1.5', {'at': '2026-11-16T10:01:00Z'}),
                   lambda c: c['slackPost']['head'] == {'at': '2026-11-16T10:01:00Z'}),
}


@pytest.mark.parametrize('name', sorted(LEASED_WRITES))
def test_a_card_write_waits_out_a_lease_that_stays_busy_for_several_attempts(name):
    # given
    extra, write, landed = LEASED_WRITES[name]
    store = fresh_store()
    store.save({**state(stored_card(SEED, **extra)), 'initialized': True}, None)
    tries = busy_for(store, 6)

    # when
    write(store)

    # then
    assert len(tries) == 6 and landed(store.anomaly(SEED))
    assert 'writing' not in store.meta.find_one({'_id': STATE_ID})


def test_a_card_write_whose_lease_never_frees_still_lands_and_says_so_loudly(monkeypatch, caplog):
    # given
    monkeypatch.setattr('skopax.store.HOLD_WAIT_S', 0)
    store = fresh_store()
    store.save({**state(stored_card(SEED)), 'initialized': True}, None)
    busy_for(store, 10 ** 6)

    # when
    with caplog.at_level(logging.ERROR, logger='skopax.store'):
        claimed = store.claim_card(SEED, 'run-1')

    # then
    assert claimed and store.anomaly(SEED)['triage'] == 'investigating'
    assert [r.getMessage() for r in caplog.records if r.levelno == logging.ERROR] == [
        'a card write went ahead without the state lease: the lease was not free for 0 s']

