import json

from fastapi.testclient import TestClient

from tests.conftest import anomaly, settings, state
from tests.fake_slack import FakeSlackApi
from tests.test_engine import NOW as ENGINE_NOW, OLD, YOUNG, CART, DAY, auto_lines, item, seeded as engine_seeded, waiting, ago
from tests.test_investigator_tools import KEY, box, seeded
from tests.test_state_notes import POST_TS, Transport, card, seed
from tests.test_auto_resolve_hours import VWO_WINDOW, verdict
from skopax import causes
from skopax.app import create_app
from skopax.clearnotes import ClearNotes
from skopax.engine import PassResult, apply, auto_resolve, auto_resolve_kinds, parse_iso
from skopax.investigator import prompt
from skopax.kube import Kube
from skopax.messages import msg
from skopax.reminders import state_text
from skopax.slack import Slack, anomaly_message
from skopax.statenotes import StateNotes
from skopax.store import Store

CLEARED = '2026-10-26T09:30:00Z'
FIRST = '2026-10-26T07:00:00Z'
T = parse_iso(CLEARED)
M = 60
WHAT = 'the 10:00 push campaign send'
EVENT = {'kind': 'event', 'what': WHAT, 'at': '2026-10-26T07:00:00Z', 'firstSeen': FIRST}


def notes(store: Store, transport: Transport) -> ClearNotes:
    return ClearNotes(store, Slack('xoxb-test', '#ops', transport, FakeSlackApi()), 15)


def test_note_takes_an_event_cause_and_keys_it_to_the_episode(store: Store):
    # given
    seeded(store, firstSeen='2026-10-27T08:00:00Z')
    b = box(store)

    # when
    out, err = b.call('note', {'key': KEY, 'text': 'errors followed the push send', 'cause': {'kind': 'event', 'what': WHAT, 'at': '2026-10-27T07:58:00Z'}})

    # then
    assert not err, out
    assert store.anomaly(KEY)['cause'] == {'kind': 'event', 'what': WHAT, 'at': '2026-10-27T07:58:00Z', 'firstSeen': '2026-10-27T08:00:00Z'}
    assert store.anomaly(KEY)['note']['text'] == 'errors followed the push send'


def test_note_refuses_a_long_what_an_unknown_kind_and_a_bad_time(store: Store):
    # given
    seeded(store, firstSeen='2026-10-27T08:00:00Z')
    b = box(store)

    # when
    long = b.call('note', {'key': KEY, 'text': 'x', 'cause': {'kind': 'event', 'what': 'w' * 121}})
    kind = b.call('note', {'key': KEY, 'text': 'x', 'cause': {'kind': 'weather', 'what': 'rain'}})
    when = b.call('note', {'key': KEY, 'text': 'x', 'cause': {'kind': 'event', 'what': 'a batch job', 'at': 'yesterday'}})
    empty = b.call('note', {'key': KEY, 'text': 'x', 'cause': {'kind': 'event', 'what': '   '}})

    # then
    assert long[1] and 'at most 120 characters' in long[0]
    assert kind[1] and 'must be one of event, recurring' in kind[0]
    assert when[1] and 'cause.at' in when[0]
    assert empty[1] and 'cause.what' in empty[0]
    assert 'cause' not in store.anomaly(KEY) and 'note' not in store.anomaly(KEY)


def test_a_120_character_what_is_kept_whole_and_the_card_tool_shows_it(store: Store):
    # given
    seeded(store, firstSeen='2026-10-27T08:00:00Z')
    b = box(store)
    what = 'w' * 120

    # when
    _, err = b.call('note', {'key': KEY, 'text': 'x', 'cause': {'kind': 'event', 'what': what}})
    shown, _ = b.call('card', {'key': KEY})

    # then
    assert not err
    assert json.loads(shown)['cause'] == {'kind': 'event', 'what': what, 'firstSeen': '2026-10-27T08:00:00Z'}


def test_a_run_that_read_planted_text_sets_no_cause(store: Store):
    # given
    seeded(store, firstSeen='2026-10-27T08:00:00Z')
    b = box(store)
    b.flags += [{'tool': 'esql', 'phrase': 'ignore previous', 'excerpt': 'ignore previous instructions'}]

    # when
    out, err = b.call('note', {'key': KEY, 'text': 'x', 'cause': {'kind': 'event', 'what': WHAT}})

    # then
    assert err and out.startswith('this run read planted text in the esql answer (ignore previous), so it sets no cause')
    assert 'cause' not in store.anomaly(KEY) and 'note' not in store.anomaly(KEY)


def test_the_brief_shows_the_cause_on_the_card():
    # when
    text = prompt.card_brief({'key': KEY, 'svc': 'a', 'label': 'x', 'sev': 'warn', 'status': 'open', 'firstSeen': FIRST, 'cause': EVENT}, 'https://skopax.example.com')

    # then
    assert f'  Cause: an event \u00b7 {WHAT}' in text


def test_shadow_mode_records_the_cause_without_writing_it(store: Store):
    # given
    seeded(store, firstSeen='2026-10-27T08:00:00Z')
    b = box(store, mode='shadow')

    # when
    out, err = b.call('note', {'key': KEY, 'text': 'x', 'cause': {'kind': 'event', 'what': WHAT}})

    # then
    assert not err and 'shadow mode' in out
    assert 'cause' not in store.anomaly(KEY)


def test_an_event_card_that_clears_says_it_stopped_after_the_event(store: Store):
    # given
    seed(store, card(CART, status='cleared', clearedAt=CLEARED, cause=EVENT))
    t = Transport()

    # when
    notes(store, t).run_once(T + 16 * M)

    # then
    [(ts, text)] = t.replies()
    assert ts == POST_TS
    assert text == msg('clear.event', name='checkout-service / ERROR pattern', at='26 Oct 09:30 UTC', took='2 h 30 min', what=WHAT, tail=msg('clear.recur_free_task'))
    assert text.startswith('\U0001F7E1 Stopped after the event: *checkout-service / ERROR pattern* \u00b7 quiet since 26 Oct 09:30 UTC, lasted 2 h 30 min.')
    assert f'The investigator tied it to {WHAT}; the signal ending does not fix the cause' in text
    assert 'Back to normal' not in text


def test_an_event_card_with_a_ticket_or_in_progress_gets_the_recurring_tails(store: Store):
    # given
    seed(store, card(CART, status='cleared', clearedAt=CLEARED, cause=EVENT, ticket='OPS-1'),
         card('log|b|ERROR|taken', ts='1792224000.000300', status='cleared', clearedAt=CLEARED, cause=EVENT | {'firstSeen': FIRST}, triage='progress', ticket='OPS-2'))
    t = Transport()

    # when
    notes(store, t).run_once(T + 16 * M)

    # then
    tails = sorted(text.rsplit('. ', 1)[1] for _, text in t.replies())
    assert tails == sorted([msg('clear.recur_free'), msg('clear.recur_taken')])


def test_a_card_without_a_cause_still_gets_back_to_normal_and_the_recurring_path_is_unchanged(store: Store):
    # given
    seed(store, card(CART, status='cleared', clearedAt=CLEARED),
         card('kube|shop/web|OOMKilled', ts='1792224000.000300', status='cleared', clearedAt=CLEARED, label='OOMKilled'))
    t = Transport()

    # when
    notes(store, t).run_once(T + 16 * M)

    # then
    texts = sorted(text for _, text in t.replies())
    assert texts[1].startswith('\U0001F7E1 Alarm went quiet: *checkout-service / OOMKilled*')
    assert texts[0].startswith('\U0001F440 Stopped firing, still open: *checkout-service / ERROR pattern*')


def test_a_recurring_cause_gets_the_alarm_went_quiet_wording(store: Store):
    # given
    seed(store, card(CART, status='cleared', clearedAt=CLEARED, cause={'kind': 'recurring', 'what': 'a leak in the image cache', 'firstSeen': FIRST}))
    t = Transport()

    # when
    notes(store, t).run_once(T + 16 * M)

    # then
    assert t.replies()[0][1].startswith('\U0001F7E1 Alarm went quiet: *checkout-service / ERROR pattern*')


def test_a_cause_from_an_earlier_episode_is_not_used(store: Store):
    # given
    seed(store, card(CART, status='cleared', clearedAt=CLEARED, cause=EVENT | {'firstSeen': '2026-10-20T07:00:00Z'}))
    t = Transport()

    # when
    notes(store, t).run_once(T + 16 * M)

    # then
    assert t.replies()[0][1].startswith('\U0001F440 Stopped firing, still open:')


def test_slack_marks_in_what_stay_inert(store: Store):
    # given
    planted = '<!channel> *bold* _it_ ~gone~ `code` <http://x|y> &amp;'
    seed(store, card(CART, status='cleared', clearedAt=CLEARED, cause=EVENT | {'what': planted}))
    t = Transport()

    # when
    notes(store, t).run_once(T + 16 * M)

    # then
    text = t.replies()[0][1]
    tied = text.split('tied it to ', 1)[1].split('; the signal', 1)[0]
    assert '<' not in tied and '>' not in tied and '*' not in tied and '_' not in tied and '~' not in tied and '`' not in tied
    assert '&lt;!channel&gt;' in tied and '&lt;http://x|y&gt;' in tied and '&amp;amp;' in tied
    assert 'bold' in tied and 'gone' in tied and 'code' in tied


def test_a_merged_child_takes_its_parents_cause():
    # given
    parent = {'key': 'p', 'firstSeen': FIRST, 'status': 'open', 'cause': EVENT}
    child = {'key': 'c', 'firstSeen': '2026-10-26T07:05:00Z', 'parent': 'p', 'status': 'cleared'}
    own = {'key': 'o', 'firstSeen': FIRST, 'parent': 'p', 'cause': {'kind': 'recurring', 'what': 'leak', 'firstSeen': FIRST}}
    stale = {'key': 's', 'firstSeen': FIRST, 'parent': 'gone'}

    # when
    by = {c['key']: c for c in (parent, child, own, stale)}

    # then
    assert causes.cause_of(child, by) == EVENT and causes.event(child, by)
    assert causes.cause_of(own, by)['kind'] == 'recurring' and not causes.event(own, by)
    assert causes.cause_of(stale, by) is None
    assert causes.cause_of({'key': 'x', 'firstSeen': FIRST}, by) is None


def test_the_cause_stays_on_the_card_through_passes_and_a_late_refire_starts_without_it(store: Store):
    # given
    key = 'cf-status|cdn'
    first = ago(7200)
    engine_seeded(store, {**item(key), 'firstSeen': first, 'lastSeen': ago(1800), 'peak': 1, 'status': 'open', 'note': None,
                          'cause': {'kind': 'event', 'what': WHAT, 'firstSeen': first}})

    # when
    apply(store, [PassResult('cf', True, 'ok', [item(key)], {}, ('cf',))], ENGINE_NOW)
    kept = store.anomaly(key)
    apply(store, [PassResult('cf', True, 'ok', [], {}, ('cf',))], ENGINE_NOW + 60)
    cleared = store.anomaly(key)
    apply(store, [PassResult('cf', True, 'ok', [item(key)], {}, ('cf',))], ENGINE_NOW + 60 + 3 * 3600)
    again = store.anomaly(key)

    # then
    assert kept['cause']['what'] == WHAT and causes.cause_of(kept, {key: kept})
    assert cleared['status'] == 'cleared' and causes.event(cleared, {key: cleared})
    assert again['status'] == 'open' and again['firstSeen'] != first
    assert causes.cause_of(again, {key: again}) is None


def test_auto_resolve_gives_an_event_card_without_a_ticket_six_more_hours(store: Store):
    # given
    noted = {'text': 'errors followed the push send', 'at': ago(OLD)}
    engine_seeded(store, waiting(CART, cause={'kind': 'event', 'what': WHAT, 'firstSeen': ago(7200)}, triage='dived', triagedBy='pod', note=noted),
                  waiting('log|b|ERROR|ticketed', ticket='OPS-1', cause={'kind': 'event', 'what': WHAT, 'firstSeen': ago(7200)}),
                  waiting('log|c|ERROR|plain'))

    # when
    apply(store, [], ENGINE_NOW, auto_resolve_hours=72)
    early = {a['key']: a for a in store.load()['anomalies']}
    lines = auto_lines(store)

    # then
    assert early[CART].get('triage') == 'dived' and 'autoResolvedAt' not in early[CART]
    assert early['log|b|ERROR|ticketed']['triage'] == 'handled' and early['log|c|ERROR|plain']['triage'] == 'handled'
    assert {r['key'] for r in lines} == {'log|b|ERROR|ticketed', 'log|c|ERROR|plain'}


def test_an_event_card_is_auto_resolved_six_hours_later_with_the_event_note(store: Store):
    # given
    cause = {'kind': 'event', 'what': WHAT, 'firstSeen': ago(7200)}
    engine_seeded(store, waiting(CART, cleared=OLD + 6 * 3600, cause=cause, triage='dived', triagedBy='pod', note={'text': 'errors followed the push send', 'at': ago(OLD)}),
                  waiting('log|b|ERROR|bare', cleared=OLD + 6 * 3600, cause=cause))

    # when
    applied = apply(store, [], ENGINE_NOW, auto_resolve_hours=72)
    by = {a['key']: a for a in store.load()['anomalies']}
    lines = {r['key']: r['text'] for r in auto_lines(store)}

    # then
    assert by[CART]['triage'] == 'handled' and by[CART]['autoResolvedAt']
    assert by[CART]['note']['text'] == 'errors followed the push send'
    assert by['log|b|ERROR|bare']['note']['text'] == 'Stopped after the event; nobody opened a task'
    assert lines[CART] == 'auto-resolved checkout-service ERROR pattern \u00b7 Stopped after the event; nobody opened a task'
    assert {r['key']: r.get('event') for r in applied.resolved} == {CART: True, 'log|b|ERROR|bare': True}
    assert {r['key']: r['hours'] for r in applied.resolved} == {CART: 78, 'log|b|ERROR|bare': 78}


def test_auto_resolve_on_its_own_waits_the_extra_hours_only_for_an_episode_cause():
    # given
    state = {'anomalies': [waiting('log|a|ERROR|old-cause', cause={'kind': 'event', 'what': WHAT, 'firstSeen': ago(99 * DAY)}),
                           waiting('log|b|ERROR|recurring', cause={'kind': 'recurring', 'what': 'leak', 'firstSeen': ago(7200)}),
                           waiting('log|c|ERROR|young', YOUNG)]}

    # when
    done = auto_resolve(state, ENGINE_NOW, 72)

    # then
    assert [d['key'] for d in done] == ['log|a|ERROR|old-cause', 'log|b|ERROR|recurring']


def test_the_auto_resolve_thread_note_of_an_event_card_says_nobody_opened_a_task(store: Store):
    # given
    seed(store, card(CART, cause=EVENT, triage='handled'), card('log|b|ERROR|plain', ts='1792224000.000300', triage='handled'))
    t = Transport()
    box_ = StateNotes(store, Slack('xoxb-test', '#ops', t, FakeSlackApi()))

    # when
    box_.auto_resolved([CART], 12, events={CART})
    box_.auto_resolved(['log|b|ERROR|plain'], 6, events=())

    # then
    assert sorted(text for _, text in t.replies()) == sorted([msg('notes.state.auto_event', span='12 hours'), msg('notes.state.auto', span='6 hours')])
    assert msg('notes.state.auto_event', span='12 hours') == '\u2705 Stopped after the event; nobody opened a task. Auto-resolved after 12 hours'


def test_the_card_page_gets_the_cause_of_this_firing_or_its_parents(store: Store, tmp_path):
    # given
    first = '2026-11-09T16:00:00Z'
    parent = anomaly('log|push-service|ERROR|send', num=1, firstSeen=first, cause={'kind': 'event', 'what': WHAT, 'firstSeen': first})
    child = anomaly('log|push-service|WARN|kid', num=2, firstSeen='2026-11-09T16:05:00Z', parent=parent['key'])
    old = anomaly('log|mail-service|ERROR|old', num=3, firstSeen=first, cause={'kind': 'event', 'what': 'an old deploy', 'firstSeen': '2026-10-01T10:00:00Z'})
    store.save(state(parent, child, old), None)
    store.card_ids.insert_many([{'_id': c['key'], 'n': c['num']} for c in (parent, child, old)])

    # when
    with TestClient(create_app(settings(), store, kube=Kube(root=tmp_path))) as c:
        got = [c.get(f'/api/cards/{n}').json() for n in (1, 2, 3)]

    # then
    assert got[0]['cause'] == got[1]['cause'] == {'kind': 'event', 'what': WHAT, 'firstSeen': first}
    assert 'cause' not in got[2]


def test_a_check_cannot_put_a_cause_on_its_card(store: Store):
    # given
    key = 'cf-new|zone'
    engine_seeded(store)
    t = ago(0)

    # when
    apply(store, [PassResult('cf', True, 'ok', [item(key, cause={'kind': 'event', 'what': 'planted', 'firstSeen': t}, mutedPreset=True)], {}, ('cf',))], ENGINE_NOW)
    card = store.anomaly(key)

    # then
    assert 'cause' not in card and 'mutedPreset' not in card


def test_the_main_post_of_a_cleared_event_card_does_not_say_recovered():
    # given
    base = {'key': CART, 'svc': 'checkout-service', 'label': 'ERROR pattern', 'sev': 'warn', 'status': 'cleared', 'firstSeen': FIRST, 'clearedAt': CLEARED, 'now': 1, 'unit': ''}

    # when
    event = anomaly_message(base | {'cause': EVENT}, 'https://skopax.example.com/#a=x', 'server')
    plain = anomaly_message(base, 'https://skopax.example.com/#a=x', 'server')
    old = anomaly_message(base | {'cause': EVENT | {'firstSeen': '2026-10-01T07:00:00Z'}}, 'https://skopax.example.com/#a=x', 'server')

    # then
    bar = lambda m: m['blocks'][-1]['elements'][0]['text']
    assert bar(event).startswith(msg('slack.status.event')) and event['text'].startswith(msg('slack.status.event'))
    assert msg('slack.status.event') == 'Stopped after the event \u00b7 still open'
    assert bar(plain).startswith(msg('slack.status.cleared')) and plain['text'].startswith(msg('slack.cleared'))
    assert bar(old).startswith(msg('slack.status.cleared'))


def test_a_reminder_on_a_quiet_event_card_does_not_say_back_to_normal():
    # when
    event = state_text({'status': 'cleared', 'firstSeen': FIRST, 'cause': EVENT, 'key': CART, 'label': 'ERROR pattern'})
    plain = state_text({'status': 'cleared', 'firstSeen': FIRST, 'key': CART, 'label': 'ERROR pattern'})

    # then
    assert event == msg('reminder.event') == 'Stopped after the event but not marked; the cause can come back with the next one.'
    assert plain == msg('reminder.calmed')


def test_note_with_a_null_cause_takes_the_cause_back(store: Store):
    # given
    seeded(store, firstSeen='2026-10-27T08:00:00Z')
    b = box(store)
    b.call('note', {'key': KEY, 'text': 'x', 'cause': {'kind': 'event', 'what': WHAT}})

    # when
    out, err = b.call('note', {'key': KEY, 'text': 'not the push after all', 'cause': None})
    kept, _ = b.call('note', {'key': KEY, 'text': 'still not'})

    # then
    assert not err, out
    assert 'cause' not in store.anomaly(KEY) and store.anomaly(KEY)['note']['text'] == 'still not'


def test_cause_at_must_be_a_full_time_with_a_zone(store: Store):
    # given
    seeded(store, firstSeen='2026-10-27T08:00:00Z')
    b = box(store)

    # when
    day = b.call('note', {'key': KEY, 'text': 'x', 'cause': {'kind': 'event', 'what': WHAT, 'at': '2026-10-26'}})
    naive = b.call('note', {'key': KEY, 'text': 'x', 'cause': {'kind': 'event', 'what': WHAT, 'at': '2026-10-26T07:00:00'}})
    zoned = b.call('note', {'key': KEY, 'text': 'x', 'cause': {'kind': 'event', 'what': WHAT, 'at': '2026-10-26T09:00:00+02:00'}})

    # then
    assert day[1] and naive[1] and 'cause.at' in day[0]
    assert not zoned[1] and store.anomaly(KEY)['cause']['at'] == '2026-10-26T09:00:00+02:00'


def test_what_follows_the_draft_rules_for_links_and_pings(store: Store):
    # given
    seeded(store, firstSeen='2026-10-27T08:00:00Z')
    b = box(store)

    # when
    link = b.call('note', {'key': KEY, 'text': 'x', 'cause': {'kind': 'event', 'what': 'the send from https://evil.example.net/x'}})
    ping = b.call('note', {'key': KEY, 'text': 'x', 'cause': {'kind': 'event', 'what': 'the send, tell @channel'}})
    raw = b.call('note', {'key': KEY, 'text': 'x', 'cause': {'kind': 'event', 'what': 'the send <!here>'}})
    fence = b.call('note', {'key': KEY, 'text': 'x', 'cause': {'kind': 'event', 'what': 'the ```job```'}})
    ok = b.call('note', {'key': KEY, 'text': 'x', 'cause': {'kind': 'event', 'what': 'the send, see https://skopax.example.com/#a=x'}})

    # then
    assert link[1] and 'evil.example.net' in link[0]
    assert ping[1] and raw[1] and 'mention' in ping[0] and 'mention' in raw[0]
    assert fence[1] and 'code' in fence[0]
    assert not ok[1], ok[0]


def test_the_posted_clear_note_section_is_verbatim(store: Store):
    # given
    seed(store, card(CART, status='cleared', clearedAt=CLEARED, cause=EVENT))
    t = Transport()

    # when
    notes(store, t).run_once(T + 16 * M)

    # then
    section = t.calls[0]['blocks'][0]
    assert section['type'] == 'section' and section['text']['verbatim'] is True


def test_the_clear_note_waits_while_an_investigation_runs(store: Store):
    # given
    seed(store, card(CART, status='cleared', clearedAt=CLEARED, triage='investigating'))
    t = Transport()
    box_ = notes(store, t)

    # when
    waiting_ = box_.run_once(T + 16 * M)
    store.anomalies.update_one({'_id': CART}, {'$set': {'triage': 'dived', 'cause': EVENT}})
    sent = box_.run_once(T + 30 * M)

    # then
    assert waiting_ == [] and sent == [CART]
    assert t.replies()[0][1].startswith('\U0001F7E1 Stopped after the event:')


def test_a_merged_child_gets_no_clear_note_and_its_parent_says_stopped_after_the_event(store: Store):
    # given
    seed(store, card(CART, status='cleared', clearedAt=CLEARED, cause=EVENT), card('log|checkout-service|ERROR|kid', ts='1792224000.000300', status='cleared', clearedAt=CLEARED, parent=CART))
    t = Transport()

    # when
    sent = notes(store, t).run_once(T + 16 * M)

    # then
    assert sent == [CART]
    assert t.replies()[0][1].startswith('\U0001F7E1 Stopped after the event:')


def test_the_card_tool_shows_only_the_cause_of_this_firing(store: Store):
    # given
    seeded(store, firstSeen='2026-10-27T08:00:00Z', cause={'kind': 'event', 'what': WHAT, 'firstSeen': '2026-10-01T08:00:00Z'})

    # when
    shown, _ = box(store).call('card', {'key': KEY})

    # then
    assert 'cause' not in json.loads(shown)


def test_the_main_post_and_reminder_of_a_merged_child_take_the_parent_cause():
    # given
    parent = {'key': 'p', 'firstSeen': FIRST, 'status': 'open', 'cause': EVENT}
    child = {'key': CART, 'svc': 'checkout-service', 'label': 'ERROR pattern', 'sev': 'warn', 'status': 'cleared', 'firstSeen': '2026-10-26T07:05:00Z', 'parent': 'p', 'now': 1, 'unit': ''}

    # when
    message = anomaly_message(child, 'https://skopax.example.com/#a=x', 'server', parent=parent)
    line = state_text(child, {'p': parent, CART: child})

    # then
    assert message['blocks'][-1]['elements'][0]['text'].startswith(msg('slack.status.event'))
    assert line == msg('reminder.event')


def flapping(store: Store, back_after: float) -> tuple[dict, list]:
    key = 'cf-status|cdn'
    first = ago(7200)
    post = {'channel': 'C9', 'ts': POST_TS, 'at': ago(7100), 'by': 'server'}
    engine_seeded(store, {**item(key), 'firstSeen': first, 'lastSeen': ago(1800), 'peak': 1, 'status': 'open', 'note': None, 'slackPost': post,
                          'cause': {'kind': 'event', 'what': WHAT, 'firstSeen': first}})
    apply(store, [PassResult('cf', True, 'ok', [], {}, ('cf',))], ENGINE_NOW)
    apply(store, [PassResult('cf', True, 'ok', [item(key)], {}, ('cf',))], ENGINE_NOW + back_after)
    apply(store, [PassResult('cf', True, 'ok', [], {}, ('cf',))], ENGINE_NOW + back_after + 60)
    again = store.anomaly(key)
    store.anomalies.update_one({'_id': key}, {'$set': {'slackPost.update': {'at': again['firstSeen']}}})
    t = Transport()
    notes(store, t).run_once(ENGINE_NOW + back_after + 60 + 16 * M)
    return store.anomaly(key), t.replies()


def test_a_flap_inside_the_reopen_window_keeps_the_event_cause(store: Store):
    # when
    card_, replies = flapping(store, 600)

    # then
    assert card_['cause']['firstSeen'] == card_['firstSeen'] and card_['cause']['what'] == WHAT
    assert replies[0][1].startswith('\U0001F7E1 Stopped after the event:')


def test_a_refire_after_the_reopen_window_starts_without_a_cause(store: Store):
    # when
    card_, replies = flapping(store, 3 * 3600)

    # then
    assert causes.own(card_) is None
    assert replies[0][1].startswith('\U0001F440 Stopped firing, still open:')


def test_an_event_card_with_a_plugin_note_says_stopped_after_the_event_and_carries_the_note(store: Store):
    # given
    said = {'text': 'Rolled back to 1.4.2', 'by': 'Fixer', 'plugin': 'fixer', 'at': CLEARED}
    seed(store, card(CART, status='cleared', clearedAt=CLEARED, cause=EVENT, clearNote=said))
    t = Transport()

    # when
    sent = notes(store, t).run_once(T + 16 * M)

    # then
    [(_, text)] = t.replies()
    assert sent == [CART]
    assert text == msg('clear.plugin_event', name='checkout-service / ERROR pattern', plugin='Fixer', note='Rolled back to 1.4.2', what=WHAT, tail=msg('clear.recur_free_task'))
    assert text.startswith('\U0001F7E1 Stopped after the event: *checkout-service / ERROR pattern* \u00b7 Fixer: Rolled back to 1.4.2 \u00b7 The investigator tied it to ' + WHAT)
    assert 'Cleared' not in text and 'Back to normal' not in text


def test_an_event_card_a_plugin_resolved_keeps_the_plugin_s_fixed_line(store: Store):
    # given
    said = {'text': 'Rolled back to 1.4.2', 'by': 'Fixer', 'plugin': 'fixer', 'at': CLEARED, 'outcome': 'fixed'}
    seed(store, card(CART, status='cleared', clearedAt=CLEARED, cause=EVENT, triage='handled', clearNote=said))
    t = Transport()

    # when
    sent = notes(store, t).run_once(T + 16 * M)

    # then
    [(_, text)] = t.replies()
    assert sent == [CART]
    assert text == msg('clear.plugin_fixed', plugin='Fixer', name='checkout-service / ERROR pattern') + ' \u00b7 Rolled back to 1.4.2'
    assert 'Cleared' not in text


def test_a_plugin_note_on_an_event_card_waits_while_an_investigation_runs(store: Store):
    # given
    said = {'text': 'Rolled back to 1.4.2', 'by': 'Fixer', 'plugin': 'fixer', 'at': CLEARED}
    seed(store, card(CART, status='cleared', clearedAt=CLEARED, cause=EVENT, triage='investigating', clearNote=said))
    t = Transport()

    # when
    sent = notes(store, t).run_once(T + 16 * M)

    # then
    assert sent == [] and t.replies() == []


def test_with_the_default_six_hours_an_event_card_closes_twelve_hours_after_it_stopped():
    # given
    cause = {'kind': 'event', 'what': WHAT, 'firstSeen': ago(7200)}
    state = {'anomalies': [waiting('log|a|ERROR|event-old', cleared=12 * 3600 + 60, cause=cause),
                           waiting('log|b|ERROR|event-young', cleared=12 * 3600 - 60, cause=cause),
                           waiting('log|c|ERROR|plain', cleared=6 * 3600 + 60)]}

    # when
    done = auto_resolve(state, ENGINE_NOW, 6)

    # then
    assert [(d['key'], d['hours'], d.get('event')) for d in done] == [('log|a|ERROR|event-old', 12, True), ('log|c|ERROR|plain', 6, None)]


def test_a_card_kind_with_its_own_window_gets_no_extra_time_for_an_event_cause():
    # given
    state = {'anomalies': [verdict('event', 24.1, cause={'kind': 'event', 'what': WHAT, 'firstSeen': ago(24.1 * 3600)}),
                           verdict('cleared', 23, status='cleared', clearedAt=ago(13 * 3600), cause={'kind': 'event', 'what': WHAT, 'firstSeen': ago(23 * 3600)})]}

    # when
    kinds = auto_resolve_kinds(state, ENGINE_NOW, (VWO_WINDOW,))
    general = auto_resolve(state, ENGINE_NOW, 6, (VWO_WINDOW,))

    # then
    assert [(d['key'], d['hours'], d.get('event')) for d in kinds] == [('ab-verdict|event', 24, None)]
    assert general == []
