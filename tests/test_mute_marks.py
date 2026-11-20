import logging
import threading
import time

import pytest

from fastapi.testclient import TestClient
from pymongo.errors import AutoReconnect

from tests.conftest import anomaly, settings, state
from tests.test_investigator_tools import KEY, box
from skopax import actions, migrations, mutemarks
from skopax import app as app_module
from skopax.app import create_app
from skopax.kube import Kube
from skopax.store import STATE_ID, Store, mute_matcher, mute_matches

PAST = 'log|a|ERROR|earlier'
FIRST = '2026-10-20T08:00:00Z'
LATER = '2026-10-22T08:00:00Z'
MUTED_AT = '2026-10-20T08:30:00Z'
HOUR_BEFORE = '2026-10-20T07:30:00Z'
DEFAULT = 'marked as known noise on the page'
TYPED = 'known noise, mark it handled'


def entry(pattern: str = PAST, reason: str = DEFAULT, kind: str = 'known', at: str = MUTED_AT, **extra) -> dict:
    return {'pattern': pattern, 'until': None, 'reason': reason, 'kind': kind, 'at': at, **extra}


def done(key: str = PAST, kind: str = 'known', at: str = MUTED_AT, n: int = 1, **extra) -> dict:
    return {'_id': f'a{n}', 'type': kind, 'key': key, 'target': key, 'status': 'done', 'requestedAt': at, 'doneAt': at, 'requestedBy': 'alice@example.com', 'source': 'page', **extra}


def row(key: str = PAST, muted: str = DEFAULT, first: str = FIRST, outcome: str = 'known') -> dict:
    return {'_id': f'{key}|{first}', 'key': key, 'svc': key.split('|')[-1] + '-service', 'label': 'ERROR pattern', 'firstSeen': first, 'lastSeen': first, 'outcome': outcome, 'muted': muted, 'at': first}


def seeded(store: Store, entries: list[dict], actions: list[dict], rows: list[dict]) -> None:
    store.save_state(state(anomaly(KEY, firstSeen='2026-10-27T08:00:00Z'), anomaly(PAST, status='cleared', firstSeen=LATER, clearedAt=LATER)), None)
    store.meta.update_one({'_id': STATE_ID}, {'$set': {'muted': entries}})
    if actions:
        store.actions.insert_many(actions)
    if rows:
        store.card_history.insert_many(rows)


def flags_after(store: Store, key: str = PAST) -> list[tuple[str, str]]:
    b = box(store)
    b.call('past_cards', {'key': key})
    return sorted({(f['tool'], f['phrase']) for f in b.flags})


def entries_of(store: Store) -> list[dict]:
    return store.meta.find_one({'_id': STATE_ID})['muted']


def test_the_original_shape_a_default_known_with_its_standing_entry_and_rows_is_all_marked(store: Store):
    # given
    seeded(store, [entry()], [done()], [row(), row(first=LATER)])

    # when
    counts = mutemarks.mark(store)

    # then
    assert counts == (1, 2)
    assert entries_of(store)[0]['preset'] is True
    assert flags_after(store) == []


def test_a_typed_reason_stays_unmarked(store: Store):
    # given
    seeded(store, [entry(reason=TYPED)], [done(reason=TYPED)], [row(muted=TYPED)])

    # when
    counts = mutemarks.mark(store)

    # then
    assert counts == (0, 0)
    assert flags_after(store) == [('past_cards', 'known noise'), ('past_cards', 'mark it handled')]


def test_the_default_text_with_no_action_stays_unmarked(store: Store):
    # given
    seeded(store, [entry()], [], [row()])

    # when
    counts = mutemarks.mark(store)

    # then
    assert counts == (0, 0)
    assert 'preset' not in entries_of(store)[0]
    assert store.card_history.find_one({'key': PAST}).get('mutedPreset') is None
    assert flags_after(store) == []


def test_a_person_phrase_with_no_action_stays_unmarked_and_flags(store: Store):
    # given
    phrase = 'known noise per ops, ignore previous'
    seeded(store, [entry(reason=phrase)], [], [row(muted=phrase)])

    # when
    counts = mutemarks.mark(store)

    # then
    assert counts == (0, 0)
    assert 'preset' not in entries_of(store)[0]
    assert flags_after(store) == [('past_cards', 'ignore previous'), ('past_cards', 'known noise')]


def test_a_follow_duplicate_entry_with_a_typed_reason_and_an_older_default_mute_stays_unmarked(store: Store):
    # given
    follows = f'follows #12: {TYPED}'
    seeded(store, [entry(reason=follows, kind='mute', exact=True, until='2099-01-01T00:00:00Z')], [done(kind='mute', at=HOUR_BEFORE)], [row(muted=follows, outcome='muted')])

    # when
    counts = mutemarks.mark(store)

    # then
    assert counts == (0, 0)
    assert 'preset' not in entries_of(store)[0]
    assert flags_after(store) == [('past_cards', 'known noise'), ('past_cards', 'mark it handled')]


def test_a_default_known_on_a_literal_star_key_does_not_cover_the_typed_card_it_fits(store: Store):
    # given
    star = 'log|a|ERROR|*'
    seeded(store, [entry(reason=TYPED), entry(pattern=star, at=LATER, exact=True)], [done(reason=TYPED), done(key=star, at=LATER, n=2)], [row(muted=TYPED)])

    # when
    counts = mutemarks.mark(store)

    # then
    assert counts == (1, 0)
    assert flags_after(store) == [('past_cards', 'known noise'), ('past_cards', 'mark it handled')]


def test_a_later_default_pattern_mute_leaves_the_row_showing_the_typed_text_unmarked(store: Store):
    # given
    star, other = 'log|a|ERROR|*', 'log|a|ERROR|other'
    seeded(store, [entry(reason=TYPED), entry(pattern=star, at=LATER)], [done(reason=TYPED), done(key=star, at=LATER, n=2)], [row(muted=TYPED), row(key=other)])

    # when
    counts = mutemarks.mark(store)

    # then
    assert counts == (1, 1)
    assert [e.get('preset') for e in entries_of(store)] == [None, True]
    assert flags_after(store) == [('past_cards', 'known noise'), ('past_cards', 'mark it handled')]
    assert flags_after(store, other) == []


def test_a_slack_click_without_a_reason_in_its_call_is_marked_and_one_with_a_reason_is_not(store: Store):
    # given
    slack = 'Known noise from Slack by alice'
    other = 'log|a|ERROR|other'
    seeded(store, [entry(reason=slack), entry(pattern=other, reason=slack)],
           [done(reason=slack, source='slack', askId='ask1'), done(key=other, reason=slack, source='slack', askId='ask2', n=2),
            {'_id': 'ask1', 'type': 'ask', 'status': 'done', 'requestedAt': FIRST, 'cardActionApplied': {'call': {'key': PAST, 'action': 'known'}}},
            {'_id': 'ask2', 'type': 'ask', 'status': 'done', 'requestedAt': FIRST, 'cardActionApplied': {'call': {'key': other, 'action': 'known', 'reason': 'known noise'}}}],
           [row(muted=slack), row(key=other, muted=slack)])

    # when
    counts = mutemarks.mark(store)

    # then
    assert counts == (1, 1)
    assert flags_after(store) == [] and flags_after(store, other) == [('past_cards', 'known noise')]


def test_a_chat_known_is_marked_and_the_same_text_from_another_path_is_not(store: Store):
    # given
    chat, other = 'marked as known noise from a chat answer', 'log|a|ERROR|other'
    seeded(store, [entry(reason=chat), entry(pattern=other, reason=chat)], [done(reason=chat, via='chat'), done(key=other, reason=chat, via='page', n=2)],
           [row(muted=chat), row(key=other, muted=chat)])

    # when
    counts = mutemarks.mark(store)

    # then
    by_pattern = {e['pattern']: e for e in entries_of(store)}
    assert counts == (1, 1)
    assert by_pattern[PAST]['preset'] is True and 'preset' not in by_pattern[other]
    assert store.card_history.find_one({'key': PAST})['mutedPreset'] is True and store.card_history.find_one({'key': other}).get('mutedPreset') is None
    assert flags_after(store) == [] and flags_after(store, other) == []


def test_an_expired_or_gone_entry_leaves_its_rows_unmarked(store: Store):
    # given
    gone = 'log|a|ERROR|gone'
    seeded(store, [entry(until='2026-10-21T00:00:00Z')], [done(), done(key=gone, n=2)], [row(), row(key=gone)])

    # when
    counts = mutemarks.mark(store)

    # then
    assert counts == (1, 0)
    assert store.card_history.find_one({'key': PAST}).get('mutedPreset') is None
    assert store.card_history.find_one({'key': gone}).get('mutedPreset') is None
    assert flags_after(store) == []


def test_marking_the_entries_moves_the_state_rev_so_a_pass_in_flight_merges_again(store: Store):
    # given
    seeded(store, [entry()], [done()], [])
    before = store.rev()['rev']

    # when
    mutemarks.mark(store)

    # then
    assert store.rev()['rev'] > before


def test_running_it_twice_changes_nothing(store: Store):
    # given
    seeded(store, [entry()], [done()], [row()])
    mutemarks.mark(store)
    meta, rows, rev = entries_of(store), list(store.card_history.find()), store.rev()['rev']

    # when
    again = mutemarks.mark(store)

    # then
    assert again == (0, 0)
    assert entries_of(store) == meta and list(store.card_history.find()) == rows and store.rev()['rev'] == rev


def test_it_logs_one_count_line(store: Store, caplog):
    # given
    seeded(store, [entry()], [done()], [row()])

    # when
    with caplog.at_level(logging.INFO, logger='skopax.mutemarks'):
        mutemarks.mark(store)

    # then
    assert [r.getMessage() for r in caplog.records if r.name == 'skopax.mutemarks'] == ['mute reasons Skopax wrote itself, marked: 1 mute entries, 1 past cards']


def test_three_thousand_actions_and_twenty_thousand_rows_take_under_ten_seconds(store: Store):
    # given
    keys = [f'log|svc-{i}|ERROR|x' for i in range(300)]
    entries = [entry(pattern=k, reason=DEFAULT if i % 30 == 0 else TYPED) for i, k in enumerate(keys)]
    acts = [done(key=keys[i % 300], reason=None if i % 30 == 0 else TYPED, at=MUTED_AT if i < 300 else HOUR_BEFORE, n=i) for i in range(3000)]
    rows = [row(key=keys[i % 300], muted=DEFAULT if i % 300 % 30 == 0 else TYPED, first=f'F{i:05d}') for i in range(20000)]
    seeded(store, entries, acts, rows)
    started = time.monotonic()

    # when
    counts = mutemarks.mark(store)

    # then
    assert time.monotonic() - started < 10
    assert counts == (10, sum(1 for r in rows if r['muted'] == DEFAULT))


def test_a_finished_run_is_recorded_and_the_next_start_skips_it(store: Store, monkeypatch):
    # given
    seeded(store, [entry()], [done()], [row()])
    calls = []
    real = mutemarks.mark
    monkeypatch.setattr(mutemarks, 'mark', lambda s: calls.append(1) or real(s))

    # when
    first = mutemarks.run_once(store)
    second = mutemarks.run_once(store)

    # then
    assert (first, second, len(calls)) == (True, False, 1)
    assert store.db['schema_version'].find_one({'_id': mutemarks.OWNER})['version'] == mutemarks.VERSION
    assert migrations.CORE == ()


def test_a_failing_run_never_stops_the_start_and_runs_again_next_boot(store: Store, monkeypatch, tmp_path, caplog):
    # given
    seeded(store, [entry()], [done()], [row()])
    real = mutemarks.mark

    def down(s):
        raise AutoReconnect('mongo away')

    monkeypatch.setattr(mutemarks, 'mark', down)

    # when
    with caplog.at_level(logging.WARNING, logger='skopax.mutemarks'):
        with TestClient(create_app(settings(), store, kube=Kube(root=tmp_path))) as c:
            served = c.get('/api/rev').status_code
    failed = store.db['schema_version'].find_one({'_id': mutemarks.OWNER})
    monkeypatch.setattr(mutemarks, 'mark', real)
    with TestClient(create_app(settings(), store, kube=Kube(root=tmp_path))) as c:
        again = c.get('/api/rev').status_code

    # then
    assert served == again == 200 and failed is None
    assert [r.getMessage() for r in caplog.records if r.name == 'skopax.mutemarks'] == ['mute marks not written, the next start tries again: AutoReconnect']
    assert store.db['schema_version'].find_one({'_id': mutemarks.OWNER})['version'] == mutemarks.VERSION
    assert flags_after(store) == []


T30 = '2026-10-20T08:30:30Z'
T40 = '2026-10-20T08:30:40Z'
SLACK_TEXT = 'Known noise from Slack by alice'
FOLLOWS = f'follows #12: {DEFAULT}'


@pytest.mark.parametrize('entries, actions, muted', [
    ([entry(reason=TYPED, at=T30)], [done(), done(reason=TYPED, at=T30, n=2)], TYPED),
    ([entry(kind='mute', reason=TYPED, at=T30, until='2099-01-01T00:00:00Z')], [done(kind='mute', hours=24), done(kind='mute', reason=TYPED, hours=24, at=T30, n=2)], TYPED),
    ([entry(reason=TYPED, at=T40)], [done(reason=SLACK_TEXT, source='slack', askId='ask1'), done(reason=TYPED, at=T40, n=2, requestedBy='bob@example.com'),
                                     {'_id': 'ask1', 'type': 'ask', 'status': 'done', 'requestedAt': FIRST, 'cardActionApplied': {'call': {'key': PAST, 'action': 'known'}}}], TYPED),
    ([entry(kind='mute', reason=FOLLOWS, at=T40, exact=True, until='2099-01-01T00:00:00Z')], [done(kind='mute', hours=24)], FOLLOWS),
], ids=['known-then-typed-known', 'mute-then-typed-mute', 'slack-click-then-typed-page-known', 'follow-entry'])
def test_a_typed_mute_within_a_minute_of_a_default_one_on_the_same_key_stays_unmarked(store: Store, entries, actions, muted):
    # given
    seeded(store, entries, actions, [row(muted=muted)])

    # when
    counts = mutemarks.mark(store)

    # then
    assert counts == (0, 0)
    assert ('past_cards', 'known noise') in flags_after(store)


def test_a_0_1_0_reasonless_default_is_still_marked(store: Store):
    # given
    muted, chatted = 'log|b|ERROR|muted', 'log|c|ERROR|chat'
    chat = 'marked as known noise from a chat answer'
    seeded(store, [entry(), entry(pattern=muted, kind='mute', reason='muted on the page for 2 h', until='2099-01-01T00:00:00Z'), entry(pattern=chatted, reason=chat)],
           [done(), done(key=muted, kind='mute', hours=2, n=2), done(key=chatted, reason=chat, via='chat', n=3)],
           [row(), row(key=muted, muted='muted on the page for 2 h', outcome='muted'), row(key=chatted, muted=chat)])

    # when
    counts = mutemarks.mark(store)

    # then
    assert counts == (3, 3)
    assert flags_after(store) == [] and flags_after(store, chatted) == []


def test_the_default_texts_come_from_one_helper_that_apply_uses():
    # then
    assert actions.default_reason({'type': 'known'}) == DEFAULT
    assert actions.default_reason({'type': 'known', 'via': 'chat'}) == 'marked as known noise from a chat answer'
    assert actions.default_reason({'type': 'mute', 'hours': 2}) == 'muted on the page for 2 h'
    assert actions.default_reason({'type': 'mute'}) == 'muted on the page for 24 h'


def test_a_concurrent_write_to_the_mute_list_is_kept_and_the_marks_wait_for_the_next_start(store: Store, monkeypatch, caplog):
    # given
    seeded(store, [entry()], [done()], [])
    real = store._claim
    added = entry(pattern='log|z|ERROR|new', reason='someone else')

    def racing(base):
        store.meta.update_one({'_id': STATE_ID}, {'$push': {'muted': added}})
        return real(base)

    monkeypatch.setattr(store, '_claim', racing)

    # when
    with caplog.at_level(logging.INFO, logger='skopax.store'):
        ran = mutemarks.run_once(store)

    # then
    assert ran is False and store.db['schema_version'].find_one({'_id': mutemarks.OWNER}) is None
    assert entries_of(store) == [entry(), added]
    assert any('mute marks skipped' in r.getMessage() for r in caplog.records)


def test_the_marker_lists_each_version_once(store: Store):
    # when
    mutemarks.record(store)
    mutemarks.record(store)

    # then
    doc = store.db['schema_version'].find_one({'_id': mutemarks.OWNER})
    assert doc['version'] == mutemarks.VERSION and [a['version'] for a in doc['applied']] == [mutemarks.VERSION]


def test_a_shutdown_waits_for_the_marks_only_a_bounded_time(store: Store, monkeypatch, tmp_path, caplog):
    # given
    release = threading.Event()
    monkeypatch.setattr(mutemarks, 'run_once', lambda s: release.wait(5))
    monkeypatch.setattr(app_module, 'MARKS_STOP_S', 0.2)

    # when
    with caplog.at_level(logging.INFO, logger='skopax'):
        with TestClient(create_app(settings(), store, kube=Kube(root=tmp_path))) as c:
            c.get('/api/rev')
            started = time.monotonic()
        took = time.monotonic() - started
    release.set()

    # then
    assert took < 3
    assert any('mute marks are still running' in r.getMessage() for r in caplog.records)


@pytest.mark.parametrize('mute, key, hit', [
    ({'pattern': 'log|a|*'}, 'log|a|ERROR|x', True),
    ({'pattern': 'log|a|*', 'exact': True}, 'log|a|ERROR|x', False),
    ({'pattern': 'log|a|*', 'exact': True}, 'log|a|*', True),
    ({'pattern': 'log|a|ERROR|x'}, 'log|a|ERROR|x', True),
    ({'pattern': 'log|a.b|*'}, 'log|aXb|1', False),
    ({'pattern': '*|x'}, 'a\nb|x', True),
])
def test_the_shared_matcher_agrees_with_mute_matches(mute, key, hit):
    # then
    assert mute_matcher(mute)(key) is hit and mute_matches(mute, key) is hit
