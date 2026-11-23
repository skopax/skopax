import pytest

from tests.fake_slack import FakeSlackApi
from tests.test_state_notes import KEY, KID_TS, POST_TS, QUIET, Transport, card, seed
from skopax.engine import iso, parse_iso
from skopax.messages import msg
from skopax.reminders import Reminders
from skopax.slack import CARD_ACTION, Slack, SlackDown
from skopax.store import Store, keep_slack_state

POSTED = parse_iso('2026-10-26T08:00:00Z')
H = 3600
BASE = 'https://skopax.example.com'
LINK = f'{BASE}/#a=log%7Ccheckout-service%7CERROR%7Cboom'


def reminders(store: Store, transport: Transport, hours: float = 24, **kw) -> Reminders:
    return Reminders(store, Slack('xoxb-test', '#ops', transport, FakeSlackApi()), hours, BASE + '/', **kw)


def line(age: str, state: str) -> str:
    return f'⏰ Reminder: this card has been waiting {age}, nobody has taken it yet. {state} <{LINK}|Open card>'


def kept(store: Store) -> list[dict]:
    return [a for a in store.list_actions(None, 20) if a['type'] == 'reminder']


def test_a_posted_card_nobody_took_gets_one_reminder_a_day_in_its_thread_with_a_take_button(store: Store):
    # given
    seed(store, card(KEY))
    t = Transport()
    box = reminders(store, t)

    # when
    early = box.run_once(POSTED + 23 * H)
    first = box.run_once(POSTED + 24.5 * H)
    same_day = box.run_once(POSTED + 30 * H)
    second = box.run_once(POSTED + 48.5 * H)

    # then
    assert (early, first, same_day, second) == ([], [KEY], [], [KEY])
    assert t.replies() == [(POST_TS, line('for 24 hours', msg('reminder.still'))), (POST_TS, line('for 2 days', msg('reminder.still')))]
    assert t.calls[0]['blocks'][1]['elements'][0] | {'value': None} == {'type': 'button', 'action_id': CARD_ACTION, 'value': None, 'style': 'primary',
                                                                      'text': {'type': 'plain_text', 'text': 'Take'}}
    offer = kept(store)[-1]
    assert (offer['status'], offer['source'], offer['cardAction'], t.calls[0]['blocks'][1]['elements'][0]['value']) == ('done', 'slack', {'key': KEY, 'action': 'take'}, offer['id'])
    assert store.anomaly(KEY)['slackPost']['reminder'] == {'at': iso(POSTED + 48.5 * H), 'ts': '1792224001.000002', 'n': 2}


def test_cards_sharing_one_thread_get_one_reminder_that_names_each_card_with_its_own_button(store: Store):
    # given
    chairs, tables = 'page-typos|shop.example.com/products/chairs', 'page-typos|shop.example.com/products/tables'
    seed(store, card(KEY), card(chairs, svc='shop.example.com/products/chairs', label='Typos'), card(tables, svc='shop.example.com/products/tables', label='Typos', status='cleared', sev='crit'),
         card(QUIET, ts=KID_TS))
    t = Transport()
    box = reminders(store, t)

    # when
    sent = box.run_once(POSTED + 24.5 * H)
    again = box.run_once(POSTED + 30 * H)

    # then
    assert sorted(sent) == sorted([KEY, chairs, tables, QUIET]) and again == []
    [shared] = [c for c in t.calls if c.get('thread_ts') == POST_TS]
    [alone] = [c for c in t.calls if c.get('thread_ts') == KID_TS]
    assert shared['text'].split('\n') == [
        '⏰ Reminder: the 3 cards on this topic have been waiting for 24 hours, nobody has taken them yet.',
        f'• *checkout-service / ERROR pattern* · {msg('reminder.still')} <{LINK}|Open card>',
        f"• *shop.example.com/products/chairs / Typos* · {msg('reminder.still')} <{BASE}/#a=page-typos%7Cshop.example.com%2Fproducts%2Fchairs|Open card>",
        f"• *shop.example.com/products/tables / Typos* · {msg('reminder.calmed')} <{BASE}/#a=page-typos%7Cshop.example.com%2Fproducts%2Ftables|Open card>"]
    offers = {a['id']: a['cardAction'] for a in kept(store)}
    buttons = [b['elements'][0] for b in shared['blocks'] if b['type'] == 'actions']
    assert [offers[b['value']] for b in buttons] == [{'key': KEY, 'action': 'take'}, {'key': chairs, 'action': 'take'}, {'key': tables, 'action': 'resolve'}]
    assert [b['text']['text'] for b in buttons] == ['Take', 'Take', 'Resolve']
    assert alone['text'].startswith('⏰ Reminder: this card has been waiting for 24 hours, nobody has taken it yet.')
    assert {store.anomaly(k)['slackPost']['reminder']['ts'] for k in (KEY, chairs, tables)} == {'1792224001.000001'}


def test_a_waiting_card_that_stopped_firing_gets_a_resolve_button_and_an_info_one_gets_none(store: Store):
    # given
    seed(store, card(KEY, status='cleared'), card('info|x', ts='1792224000.000300', status='cleared', sev='info'))
    t = Transport()

    # when
    sent = reminders(store, t).run_once(POSTED + 25 * H)

    # then
    assert sent == [KEY]
    assert t.replies() == [(POST_TS, line('for 25 hours', msg('reminder.calmed')))]
    assert kept(store)[0]['cardAction'] == {'key': KEY, 'action': 'resolve'}
    assert t.calls[0]['blocks'][1]['elements'][0]['text']['text'] == 'Resolve'


@pytest.mark.parametrize('setup', ['progress', 'handled', 'muted', 'pattern-mute', 'not-posted', 'merged', 'reopened'])
def test_no_reminder_for_a_taken_handled_muted_unposted_merged_or_just_reopened_card(store: Store, setup):
    # given
    extra = {'progress': {'triage': 'progress'}, 'handled': {'triage': 'handled'}, 'muted': {'muted': {'kind': 'mute'}},
             'merged': {'parent': 'log|checkout-service|ERROR|host'}, 'reopened': {'firstSeen': iso(POSTED + 20 * H)}}.get(setup, {})
    cards = [card(KEY, ts=None if setup == 'not-posted' else POST_TS, **extra)]
    if setup == 'merged':
        cards.append(card('log|checkout-service|ERROR|host', ts=None))
    seed(store, *cards)
    if setup == 'pattern-mute':
        store.save(store.load() | {'muted': [{'pattern': 'log|checkout-service|*', 'until': None, 'kind': 'known'}]}, None)
    t = Transport()

    # when
    sent = reminders(store, t).run_once(POSTED + 25 * H)

    # then
    assert sent == [] and t.calls == [] and kept(store) == []


def test_a_failed_post_fails_the_reminder_and_the_next_run_tries_again(store: Store):
    # given
    seed(store, card(KEY))
    down = Transport(fail=SlackDown('channel_not_found'))

    # when
    failed = reminders(store, down).run_once(POSTED + 25 * H)
    retried = reminders(store, Transport()).run_once(POSTED + 25.25 * H)

    # then
    assert (failed, retried) == ([], [KEY])
    assert [a['status'] for a in kept(store)] == ['done', 'failed']
    assert store.anomaly(KEY)['slackPost']['reminder']['n'] == 1


def test_zero_hours_turns_reminders_off_and_a_save_with_an_older_copy_keeps_the_reminder():
    # given
    stored = card(KEY)
    stored['slackPost'] = stored['slackPost'] | {'reminder': {'at': '2026-10-27T08:30:00Z', 'ts': '1', 'n': 1}}

    # when
    out = keep_slack_state(card(KEY), stored)

    # then
    assert not reminders(None, Transport(), hours=0).enabled
    assert out['slackPost']['reminder'] == {'at': '2026-10-27T08:30:00Z', 'ts': '1', 'n': 1}


def test_a_quiet_oom_card_s_reminder_says_it_can_come_back_instead_of_back_to_normal(store: Store):
    # given
    oom = 'alert|KubePodCrashLooping|cluster=east-data-prometheus,namespace=warehouse'
    seed(store, card(oom, status='cleared', src='grafana', svc='kube-state-metrics', label='KubePodCrashLooping'))
    t = Transport()

    # when
    reminders(store, t).run_once(POSTED + 25 * H)

    # then
    text = t.replies()[0][1]
    assert 'The alarm went quiet but the card is not marked; if the cause is not removed it can come back.' in text and msg('reminder.calmed') not in text


def test_an_assigned_card_that_nobody_started_says_who_has_it_not_that_nobody_took_it(store: Store):
    # given
    seed(store, card(KEY, owner='Dora Lane'))
    t = Transport()
    box = reminders(store, t)

    # when
    box.run_once(POSTED + 24.5 * H)

    # then
    assert t.replies() == [(POST_TS, f'⏰ Reminder: this card has been waiting for 24 hours, :bust_in_silhouette: Dora Lane was assigned but has not started yet. {msg('reminder.still')} <{LINK}|Open card>')]
    assert t.calls[0]['blocks'][1]['elements'][0]['text']['text'] == 'Take', 'the Take button stays: taking the card starts the work'


def test_a_shared_thread_reminder_names_the_assignee_of_each_card(store: Store):
    # given
    chairs = 'page-typos|shop.example.com/products/chairs'
    seed(store, card(KEY, owner='Dora Lane'), card(chairs, svc='shop.example.com/products/chairs', label='Typos'))
    t = Transport()

    # when
    reminders(store, t).run_once(POSTED + 24.5 * H)

    # then
    [shared] = [c for c in t.calls if c.get('thread_ts') == POST_TS]
    assert shared['text'].split('\n') == [
        '⏰ Reminder: the 2 cards on this topic have been waiting for 24 hours, work has not started yet.',
        f'• *checkout-service / ERROR pattern* · assigned to Dora Lane. {msg('reminder.still')} <{LINK}|Open card>',
        f"• *shop.example.com/products/chairs / Typos* · {msg('reminder.still')} <{BASE}/#a=page-typos%7Cshop.example.com%2Fproducts%2Fchairs|Open card>"]


def test_an_assigned_card_with_a_slack_user_pings_that_person_once_with_the_person_mark(store: Store):
    # given
    seed(store, card(KEY, owner='dora.lane@example.com'))
    t = Transport()
    asked = []
    box = reminders(store, t, mention=lambda email: asked.append(email) or '<@U123>', name_of=lambda email: 'Dora Lane')

    # when
    box.run_once(POSTED + 24.5 * H)

    # then
    text = t.replies()[0][1]
    assert asked == ['dora.lane@example.com']
    assert text == f'\u23f0 Reminder: this card has been waiting for 24 hours, :bust_in_silhouette: <@U123> was assigned but has not started yet. {msg("reminder.still")} <{LINK}|Open card>'
    assert text.count('<@') == 1 and 'dora.lane@example.com' not in text


def test_a_failed_lookup_falls_back_to_the_display_name_and_never_the_email(store: Store):
    # given
    seed(store, card(KEY, owner='dora.lane@example.com'))
    t = Transport()
    named = reminders(store, t, mention=lambda email: None, name_of=lambda email: 'Dora Lane')

    # when
    named.run_once(POSTED + 24.5 * H)
    store.anomalies.update_one({'_id': KEY}, {'$unset': {'slackPost.reminder': ''}})
    other = Transport()
    reminders(store, other, mention=lambda email: None, name_of=lambda email: '').run_once(POSTED + 24.5 * H)

    # then
    assert ':bust_in_silhouette: Dora Lane was assigned' in t.replies()[0][1]
    assert ':bust_in_silhouette: dora.lane was assigned' in other.replies()[0][1] and '@example.com' not in other.replies()[0][1]


def test_a_card_without_an_owner_keeps_its_plain_text_and_asks_no_one(store: Store):
    # given
    seed(store, card(KEY))
    t = Transport()
    asked = []

    # when
    reminders(store, t, mention=lambda email: asked.append(email), name_of=lambda email: 'x').run_once(POSTED + 24.5 * H)

    # then
    assert asked == [] and t.replies() == [(POST_TS, line('for 24 hours', msg('reminder.still')))]


def test_the_reminder_packs_carry_the_person_mark_in_every_language():
    # given
    import json
    from pathlib import Path
    root = Path(__file__).parent.parent / 'skopax' / 'locale'

    # then
    assert msg('reminder.owned', owner='x') == ':bust_in_silhouette: x was assigned but has not started yet.'
    for lang in ('tr', 'de', 'es', 'fr'):
        text = json.loads((root / lang / 'messages.json').read_text(encoding='utf-8'))['reminder.owned']
        assert ':bust_in_silhouette: {owner}' in text


def test_a_card_assigned_by_name_pings_the_person_through_the_assigned_email(store: Store):
    # given
    seed(store, card(KEY, owner='Dora Lane', assigned={'name': 'Dora Lane', 'email': 'dora.lane@example.com'}))
    t = Transport()
    asked = []
    box = reminders(store, t, mention=lambda email: asked.append(email) or '<@U123>', name_of=lambda email: 'Dora Lane')

    # when
    box.run_once(POSTED + 24.5 * H)

    # then
    text = t.replies()[0][1]
    assert asked == ['dora.lane@example.com'], 'the owner field holds a name, so the lookup goes through the assigned email'
    assert ':bust_in_silhouette: <@U123> was assigned' in text and 'dora.lane@example.com' not in text


def test_a_name_only_assignee_with_no_slack_match_keeps_the_name(store: Store):
    # given
    seed(store, card(KEY, owner='Dora Lane', assigned={'name': 'Dora Lane', 'email': 'dora.lane@example.com'}))
    t = Transport()

    # when
    reminders(store, t, mention=lambda email: None, name_of=lambda email: 'Someone Else').run_once(POSTED + 24.5 * H)

    # then
    assert ':bust_in_silhouette: Dora Lane was assigned' in t.replies()[0][1]
