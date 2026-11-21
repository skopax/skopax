import dataclasses
import json
import re
import time
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient

from tests.conftest import anomaly, settings
from tests.test_auto_resolve_hours import VERDICT, VWO_WINDOW, verdict
from tests.test_business_time import at as at_utc
from tests.test_engine import CART, NOW, ago, seeded, stored, waiting
from tests.test_notifications import NINA, board, inbox
from tests.test_reminders import reminders
from tests.test_state_notes import KEY, POST_TS, Transport, card, seed
from skopax import actions
from skopax.app import create_app
from skopax.engine import apply, auto_resolve, auto_resolve_kinds, follow_parents, iso, parse_iso
from skopax.messages import msg
from skopax.notices import NOTE_LIMIT, Notices
from skopax.store import Store

H = 3600
ASSIGNED = {'by': 'paul.stone@example.com', 'at': ago(30 * H), 'email': NINA}
AUTO = {'by': 'system', 'at': ago(30 * H), 'email': NINA}
DAD = 'log|dad|ERROR|x'
KID = 'log|kid|ERROR|x'
TR_RULES = json.loads((Path(__file__).parent.parent / 'skopax' / 'locale' / 'tr' / 'page.json').read_text(encoding='utf-8'))['rules']


def by_key(store: Store) -> dict:
    return {a['key']: a for a in store.load()['anomalies']}


def tr_rule(text: str) -> list[str]:
    return [r[0] for r in TR_RULES if re.search(r[0], text)]


def test_an_assigned_card_that_stopped_firing_waits_three_windows_and_an_unassigned_one_waits_one(store: Store):
    # given
    seeded(store, waiting(CART, cleared=7 * H, owner='Nina White', assigned=ASSIGNED, handAssigned=True), waiting('log|payments-api|ERROR|x', cleared=7 * H),
           waiting('log|old|ERROR|x', cleared=19 * H, owner='Nina White', assigned=ASSIGNED, handAssigned=True))

    # when
    applied = apply(store, [], NOW, auto_resolve_hours=6)
    cards = by_key(store)

    # then
    assert sorted(r['key'] for r in applied.resolved) == ['log|old|ERROR|x', 'log|payments-api|ERROR|x']
    held = cards[CART]
    assert held.get('triage') is None and 'autoResolvedAt' not in held and held['status'] == 'cleared', 'at 7 hours it still waits for Nina'
    old = {r['key']: r for r in applied.resolved}['log|old|ERROR|x']
    assert (old['hours'], old['why']) == (18, 'cleared 16 Oct 13:00, nobody acted in 18 hours')
    assert cards['log|old|ERROR|x']['note']['text'] == 'Auto-resolved: cleared 16 Oct 13:00, nobody acted in 18 hours'
    assert cards['log|payments-api|ERROR|x']['triage'] == 'handled', 'an unassigned card still closes after one window'


def test_an_auto_assigned_an_owner_only_and_an_assigned_only_card_all_wait_three_windows():
    # given
    def cards(cleared: float) -> dict:
        return {'anomalies': [
            waiting('log|a|ERROR|auto', cleared, owner='Nina White', assigned=AUTO, autoAssigned={'tag': 'payments', 'at': ago(30 * H)}),
            waiting('log|b|ERROR|owner-only', cleared, owner='Nina White'),
            waiting('log|c|ERROR|assigned-only', cleared, assigned=ASSIGNED),
            waiting('log|d|ERROR|plain', cleared),
        ]}

    # when
    early = auto_resolve(cards(17 * H), NOW, 6)
    late = auto_resolve(cards(19 * H), NOW, 6)

    # then
    assert [d['key'] for d in early] == ['log|d|ERROR|plain']
    assert {d['key']: d['hours'] for d in late} == {'log|a|ERROR|auto': 18, 'log|b|ERROR|owner-only': 18, 'log|c|ERROR|assigned-only': 18, 'log|d|ERROR|plain': 6}


def test_the_three_windows_of_an_assigned_card_count_business_hours_only():
    # given
    friday = at_utc('2026-11-20 22:00')

    def owned() -> dict:
        return {'anomalies': [stored('log|a|ERROR|x', status='cleared', src='logs', clearedAt=iso(friday), owner='Nina White', assigned=ASSIGNED)]}

    # when
    before = auto_resolve(owned(), at_utc('2026-11-23 15:59'), 6, business=ZoneInfo('UTC'))
    after = auto_resolve(owned(), at_utc('2026-11-23 16:01'), 6, business=ZoneInfo('UTC'))

    # then
    assert before == []
    assert [(d['hours'], d['why'], d['businessTime']) for d in after] == [(18, 'cleared 20 Nov 22:00, nobody acted in 18 business hours', True)]


def test_the_per_kind_window_still_skips_an_owned_card_and_closes_a_free_one():
    # given
    state = {'anomalies': [verdict('owned', 300, owner='Nina White', assigned=ASSIGNED), verdict('free', 30)]}

    # when
    done = auto_resolve_kinds(state, NOW, (VWO_WINDOW,))

    # then
    assert [d['key'] for d in done] == [f'{VERDICT}|free']


def test_an_assigned_grouped_child_follows_its_handled_parent_and_the_reason_names_the_parent_by_number():
    # given
    kid = stored(KID, status='cleared', src='logs', clearedAt=ago(H), parent=DAD, owner='Nina White', assigned=ASSIGNED)
    dad = stored(DAD, status='cleared', src='logs', clearedAt=ago(H), triage='handled', num=16, svc='checkout-service', label='ERROR pattern')
    gone = stored('log|orphan|ERROR|x', status='cleared', src='logs', clearedAt=ago(H), parent='log|left|ERROR|x', owner='Nina White')
    state = {'anomalies': [kid, dad, gone]}

    # when
    followed = {r['key']: r for r in follow_parents(state, NOW)}

    # then
    assert followed[KID]['why'] == 'its parent #16 checkout-service / ERROR pattern was marked no action needed'
    assert followed['log|orphan|ERROR|x']['why'] == 'its parent was handled and has left the page'
    assert kid['triage'] == 'handled' and kid['triagedBy'] == 'auto'
    assert DAD not in followed[KID]['why']


def test_a_long_parent_name_is_clipped_in_its_free_part_so_the_reason_fits_and_still_matches_its_rule():
    # given
    kid = stored(KID, status='cleared', src='logs', clearedAt=ago(H), parent=DAD)
    dad = stored(DAD, status='cleared', src='logs', clearedAt=ago(H), triage='handled', num=16, svc='checkout-service', label='ERROR ' + 'x' * 400)
    state = {'anomalies': [kid, dad]}

    # when
    [row] = follow_parents(state, NOW)
    note = Notices.reason(row['why'])

    # then
    assert len(note) <= NOTE_LIMIT
    assert note.startswith('Its parent #16 checkout-service / ERROR x') and note.endswith('\u2026 was marked no action needed')
    assert tr_rule(note) == ['^Its parent (.+) was marked no action needed$']


def test_a_child_following_its_parent_into_progress_carries_no_auto_reason():
    # given
    kid = stored(KID, status='cleared', src='logs', clearedAt=ago(H), parent=DAD, owner='Nina White')
    dad = stored(DAD, status='cleared', src='logs', clearedAt=ago(H), triage='progress', ticket='OPS-1', num=16)
    state = {'anomalies': [kid, dad]}

    # when
    [row] = follow_parents(state, NOW)

    # then
    assert row.get('progress') is True and 'why' not in row
    assert kid['triage'] == 'progress'


def test_the_engine_rows_carry_the_reason_it_wrote_for_the_window_the_kind_and_the_event():
    # given
    window = {'anomalies': [waiting(CART, 7 * H)]}
    kind = {'anomalies': [verdict('free', 30)]}
    event = {'anomalies': [waiting('log|ev|ERROR|x', 30 * H, cause={'kind': 'event', 'what': 'the 10:00 push campaign send', 'firstSeen': ago(7200)})]}

    # when
    [w] = auto_resolve(window, NOW, 6)
    [k] = auto_resolve_kinds(kind, NOW, (VWO_WINDOW,))
    [e] = auto_resolve(event, NOW, 6)

    # then
    assert w['why'] == 'cleared 17 Oct 01:00, nobody acted in 6 hours'
    assert window['anomalies'][0]['note']['text'] == 'Auto-resolved: ' + w['why']
    assert k['why'].startswith('first seen ') and k['why'].endswith(', nobody acted in 1 day')
    assert kind['anomalies'][0]['note']['text'] == 'Auto-resolved: ' + k['why']
    assert (e['event'], e['why']) == (True, 'Stopped after the event; nobody opened a task')
    assert all(r['text'].endswith(' \u00b7 ' + r['why']) for r in (w, k, e))


def test_the_auto_notice_carries_the_reason_as_a_sentence_for_the_window_the_kind_the_event_and_the_parent(store: Store):
    # given
    board(store, anomaly(CART, num=14, owner=NINA), anomaly('log|kind|ERROR|x', num=15, owner=NINA), anomaly('log|ev|ERROR|x', num=16, owner=NINA),
          anomaly(KID, num=17, owner=NINA), anomaly('log|bare|ERROR|x', num=18, owner=NINA))
    reasons = {CART: 'cleared 16 Oct 18:00, nobody acted in 6 hours', 'log|kind|ERROR|x': 'first seen 15 Oct 18:00, nobody acted in 1 day',
               'log|ev|ERROR|x': msg('engine.auto.event'), KID: 'its parent #16 checkout-service / ERROR pattern was marked no action needed'}

    # when
    Notices(store).auto_resolved([CART, 'log|kind|ERROR|x', 'log|ev|ERROR|x', KID, 'log|bare|ERROR|x'], reasons)
    rows = {n['key']: n for n in inbox(store, 'nina.white')}

    # then
    assert {k: (n['kind'], n['note']) for k, n in rows.items()} == {
        CART: ('auto', 'Cleared 16 Oct 18:00, nobody acted in 6 hours'), 'log|kind|ERROR|x': ('auto', 'First seen 15 Oct 18:00, nobody acted in 1 day'),
        'log|ev|ERROR|x': ('auto', 'Stopped after the event; nobody opened a task'), KID: ('auto', 'Its parent #16 checkout-service / ERROR pattern was marked no action needed'),
        'log|bare|ERROR|x': ('auto', '')}
    assert rows[CART]['text'] == 'Auto-resolved \u00b7 Cleared 16 Oct 18:00, nobody acted in 6 hours \u00b7 by Skopax'
    assert all(tr_rule(n['note'])[0].startswith(('^Cleared ', '^First seen (.+), nobody', '^Its parent ')) for k, n in rows.items() if k not in ('log|ev|ERROR|x', 'log|bare|ERROR|x'))


def test_the_server_pass_tells_the_assignee_why_after_three_windows(store: Store):
    # given
    seeded(store, waiting(CART, cleared=19 * H, owner='Nina White', assigned=ASSIGNED, num=14))

    # when
    with TestClient(create_app(dataclasses.replace(settings(), auto_resolve_hours_crit=6, auto_resolve_hours_warn=6, business_days=False), store)) as c:
        c.app.state.server_apply([], NOW, None)

    # then
    assert [(n['kind'], n['key'], n['note']) for n in inbox(store, 'nina.white')] == [('auto', CART, 'Cleared 16 Oct 13:00, nobody acted in 18 hours')]


def test_the_server_pass_sends_the_parent_reason_to_the_assigned_child_s_person(store: Store):
    # given
    seeded(store, stored(KID, status='cleared', src='logs', clearedAt=ago(H), parent=DAD, owner='Nina White', assigned=ASSIGNED, num=17),
           stored(DAD, status='cleared', src='logs', clearedAt=ago(H), triage='handled', triagedAt=ago(H), num=16, svc='checkout-service', label='ERROR pattern'))

    # when
    with TestClient(create_app(dataclasses.replace(settings(), auto_resolve_hours_crit=6, auto_resolve_hours_warn=6), store)) as c:
        c.app.state.server_apply([], NOW, None)

    # then
    assert [(n['kind'], n['key'], n['note']) for n in inbox(store, 'nina.white')] == [('auto', KID, 'Its parent #16 checkout-service / ERROR pattern was marked no action needed')]


def test_a_child_following_its_parent_into_progress_gets_a_progress_notice_not_an_auto_one(store: Store):
    # given
    seeded(store, stored(KID, status='cleared', src='logs', clearedAt=ago(H), parent=DAD, owner='Nina White', assigned=ASSIGNED, num=17),
           stored(DAD, status='cleared', src='logs', clearedAt=ago(H), triage='progress', ticket='OPS-1', num=16))

    # when
    with TestClient(create_app(dataclasses.replace(settings(), auto_resolve_hours_crit=6, auto_resolve_hours_warn=6), store)) as c:
        c.app.state.server_apply([], NOW, None)

    # then
    assert [(n['kind'], n['key'], n['note']) for n in inbox(store, 'nina.white')] == [('progress', KID, 'OPS-1')]


def test_an_assigned_stopped_card_keeps_its_reminder_inside_its_three_windows(store: Store):
    # given
    at = parse_iso('2026-10-26T08:00:00Z') + 24.5 * H
    seed(store, card(KEY, status='cleared', clearedAt='2026-10-26T09:00:00Z', owner='Dora Lane', assigned={'by': 'paul.stone@example.com', 'at': '2026-10-26T08:30:00Z'}))
    t = Transport()

    # when
    apply(store, [], at, auto_resolve_hours=12)
    sent = reminders(store, t).run_once(at)

    # then
    assert store.anomaly(KEY).get('triage') is None, 'one window of 12 hours has passed, three have not'
    assert sent == [KEY]
    [(ts, text)] = t.replies()
    assert ts == POST_TS and 'Dora Lane' in text
    assert iso(at) == store.anomaly(KEY)['slackPost']['reminder']['at']


def test_a_reassignment_late_in_the_wait_gives_the_new_person_three_full_windows():
    # given
    cleared = NOW - 40 * H
    moved = cleared + 17.9 * H

    def owned() -> dict:
        return {'anomalies': [stored('log|a|ERROR|x', status='cleared', src='logs', clearedAt=iso(cleared), owner='Sara Bell', assigned={'by': NINA, 'at': iso(moved)})]}

    # when
    old_wait = auto_resolve(owned(), cleared + 18.1 * H, 6)
    almost = auto_resolve(owned(), moved + 17.9 * H, 6)
    full = auto_resolve(owned(), moved + 18.1 * H, 6)

    # then
    assert (old_wait, almost) == ([], [])
    assert [d['key'] for d in full] == ['log|a|ERROR|x']


def test_a_card_reopened_a_day_after_it_cleared_is_not_auto_resolved_on_the_next_pass(store: Store):
    # given
    now = time.time()
    seeded(store, stored('log|a|ERROR|x', status='cleared', src='logs', firstSeen=iso(now - 30 * H), lastSeen=iso(now - 25 * H), clearedAt=iso(now - 25 * H),
                         triage='handled', triagedBy=NINA, triagedAt=iso(now - 2 * H)))

    # when
    actions.apply(store, {'type': 'reopen', 'key': 'log|a|ERROR|x', 'requestedBy': NINA})
    reopened = parse_iso(store.anomaly('log|a|ERROR|x')['reopenedAt'])
    soon = apply(store, [], reopened + 60, auto_resolve_hours=6)
    later = apply(store, [], reopened + 6 * H + 60, auto_resolve_hours=6)

    # then
    assert abs(reopened - now) < 60
    assert soon.resolved == [], 'the window starts again at the reopen'
    assert [r['key'] for r in later.resolved] == ['log|a|ERROR|x']
