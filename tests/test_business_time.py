import dataclasses
import datetime as dt
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from tests.conftest import AUTH, settings
from tests.test_engine import seeded, stored
from skopax import locales, messages, statenotes
from skopax.app import create_app
from skopax.businesstime import business_elapsed, elapsed
from skopax.config import load_settings
from skopax.engine import apply, auto_close_info, auto_resolve, auto_resolve_kinds, window_text
from skopax.settingcards import CARDS
from skopax.statenotes import auto_event_text, auto_text
from skopax.store import Store
from skopax.tunables import Live, TunableStore

H = 3600
DAY = 86400
UTC = ZoneInfo('UTC')
KEY = 'core.business-days'
HELP = 'Count only Monday to Friday (install time zone) for auto-resolve and info close timers'


def at(text: str, zone: str = 'UTC') -> float:
    return dt.datetime.strptime(text, '%Y-%m-%d %H:%M').replace(tzinfo=ZoneInfo(zone)).timestamp()


def iso(t: float) -> str:
    return dt.datetime.fromtimestamp(t, dt.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def cleared_card(key: str, cleared: float, sev: str = 'warn') -> dict:
    return stored(key, status='cleared', src='logs', sev=sev, label='ERROR pattern', clearedAt=iso(cleared), firstSeen=iso(cleared - 2 * H), lastSeen=iso(cleared))


def firing_info(key: str, first: float, now: float) -> dict:
    return stored(key, src='edge', sev='info', firstSeen=iso(first), lastSeen=iso(now - 60))


def kind_card(key: str, first: float) -> dict:
    return stored(key, src='wingify-verdict', sig='ab-verdict', firstSeen=iso(first), lastSeen=iso(first))


@pytest.mark.parametrize(('start', 'end', 'hours'), [
    ('2026-11-18 09:00', '2026-11-18 15:00', 6),
    ('2026-11-20 20:00', '2026-11-23 04:00', 8),
    ('2026-11-21 10:00', '2026-11-23 03:00', 3),
    ('2026-11-20 22:00', '2026-11-22 10:00', 2),
    ('2026-11-21 00:00', '2026-11-23 00:00', 0),
    ('2026-11-16 00:00', '2026-11-23 00:00', 120),
    ('2026-11-18 12:00', '2026-12-09 12:00', 15 * 24),
])
def test_business_time_counts_only_monday_to_friday(start, end, hours):
    # when
    got = business_elapsed(at(start), at(end), UTC)

    # then
    assert got == hours * H


def test_business_time_over_many_weeks_is_five_days_a_week():
    # given
    start = at('2026-11-16 00:00')

    # when
    got = business_elapsed(start, start + 520 * 7 * DAY, UTC)

    # then
    assert got == 520 * 5 * DAY


def test_business_time_is_zero_when_the_end_is_not_after_the_start():
    # given
    start = at('2026-11-18 09:00')

    # when
    backwards = business_elapsed(start, start - 5 * H, UTC)
    same = business_elapsed(start, start, UTC)

    # then
    assert (backwards, same) == (0, 0)


def test_business_time_follows_the_install_time_zone():
    # given
    start, end = at('2026-11-20 20:00'), at('2026-11-22 16:00')

    # when
    utc = business_elapsed(start, end, UTC)
    tokyo = business_elapsed(start, end, ZoneInfo('Asia/Tokyo'))

    # then
    assert (utc, tokyo) == (4 * H, 1 * H)


def test_a_weekend_daylight_saving_change_does_not_shift_the_business_hours():
    # given
    berlin = ZoneInfo('Europe/Berlin')
    start, end = at('2027-03-26 00:00', 'Europe/Berlin'), at('2027-03-29 12:00', 'Europe/Berlin')

    # when
    got = business_elapsed(start, end, berlin)

    # then
    assert end - start == 83 * H
    assert got == 36 * H


def test_a_weekday_daylight_saving_change_counts_the_real_hours_of_that_day():
    # given
    cairo = ZoneInfo('Africa/Cairo')
    start, end = at('2027-04-26 00:00', 'Africa/Cairo'), at('2027-05-03 00:00', 'Africa/Cairo')

    # when
    week = business_elapsed(start, end, cairo)
    friday = business_elapsed(at('2027-04-30 00:00', 'Africa/Cairo'), end, cairo)

    # then
    assert week == 5 * DAY - H
    assert friday == 23 * H


def hour_by_hour(start: float, end: float, zone: ZoneInfo) -> float:
    counted = 0
    t = start
    while t < end:
        if dt.datetime.fromtimestamp(t, zone).weekday() < 5:
            counted += H
        t += H
    return counted


def test_a_weekday_daylight_saving_change_inside_whole_weeks_still_counts():
    # given
    cairo = ZoneInfo('Africa/Cairo')
    start, end = at('2027-04-12 00:00', 'Africa/Cairo'), at('2027-05-24 00:00', 'Africa/Cairo')

    # when
    weeks = business_elapsed(start, end, cairo)

    # then
    assert weeks == 6 * 5 * DAY - H


def test_a_short_weekday_shift_that_returns_on_a_weekend_counts_its_extra_hour():
    # given
    tucuman = ZoneInfo('America/Argentina/Tucuman')
    start, end = at('2004-05-28 12:00', 'America/Argentina/Tucuman'), at('2004-06-14 12:00', 'America/Argentina/Tucuman')

    # when
    got = business_elapsed(start, end, tucuman)

    # then
    assert got == 265 * H
    assert got == hour_by_hour(start, end, tucuman)


@pytest.mark.parametrize(('zone', 'start', 'end'), [
    ('Africa/Cairo', '2027-01-04 00:00', '2037-01-05 00:00'),
    ('America/Argentina/Tucuman', '2003-12-31 05:00', '2005-01-03 13:00'),
    ('Europe/Berlin', '2025-03-01 07:00', '2028-11-02 19:00'),
    ('America/New_York', '2024-11-01 03:00', '2027-03-20 22:00'),
])
def test_long_spans_match_an_hour_by_hour_count(zone, start, end):
    # given
    where = ZoneInfo(zone)
    begin, finish = at(start, zone), at(end, zone)

    # when
    got = business_elapsed(begin, finish, where)

    # then
    assert got == hour_by_hour(begin, finish, where)


def test_elapsed_is_wall_clock_without_a_zone_and_business_time_with_one():
    # given
    start, end = at('2026-11-20 22:00'), at('2026-11-23 03:00')

    # when
    wall = elapsed(start, end, None)
    business = elapsed(start, end, UTC)

    # then
    assert (wall, business) == (53 * H, 5 * H)


def test_a_cleared_card_from_friday_night_waits_for_monday_hours_when_business_days_are_on():
    # given
    cleared = at('2026-11-20 22:00')
    early, later = at('2026-11-23 03:00'), at('2026-11-23 05:00')

    # when
    off = auto_resolve({'anomalies': [cleared_card('log|a|ERROR|x', cleared)]}, early, 6)
    on_early = auto_resolve({'anomalies': [cleared_card('log|a|ERROR|x', cleared)]}, early, 6, business=UTC)
    on_later = auto_resolve({'anomalies': [cleared_card('log|a|ERROR|x', cleared)]}, later, 6, business=UTC)

    # then
    assert [r['key'] for r in off] == ['log|a|ERROR|x'] and 'businessTime' not in off[0]
    assert 'nobody acted in 6 hours' in off[0]['text']
    assert on_early == []
    assert [r['key'] for r in on_later] == ['log|a|ERROR|x'] and on_later[0]['businessTime'] is True
    assert on_later[0]['text'].endswith('nobody acted in 6 business hours')


def test_the_severity_windows_and_the_event_extra_hours_count_business_time():
    # given
    cleared = at('2026-11-20 22:00')
    cause = {'kind': 'event', 'what': 'a push', 'firstSeen': iso(cleared - 2 * H)}
    cards = lambda: {'anomalies': [cleared_card('log|a|ERROR|crit', cleared, sev='crit'), cleared_card('log|b|ERROR|warn', cleared, sev='warn'),
                                   cleared_card('log|c|ERROR|event', cleared, sev='warn') | {'cause': cause}]}

    # when
    early = auto_resolve(cards(), at('2026-11-23 09:00'), 6, by_severity={'crit': 12, 'warn': 6}, business=UTC)
    later = auto_resolve(cards(), at('2026-11-23 11:00'), 6, by_severity={'crit': 12, 'warn': 6}, business=UTC)

    # then
    assert [r['key'] for r in early] == ['log|b|ERROR|warn']
    assert {r['key']: r['hours'] for r in later} == {'log|a|ERROR|crit': 12, 'log|b|ERROR|warn': 6, 'log|c|ERROR|event': 12}
    assert [r.get('event') for r in later] == [None, None, True]


def test_a_kind_window_counts_business_time_from_when_the_card_first_appeared():
    # given
    first = at('2026-11-20 20:00')
    now = at('2026-11-23 10:00')
    windows = (({'wingify-verdict'}, 'ab-verdict', 24),)

    # when
    off = auto_resolve_kinds({'anomalies': [kind_card('vwo|x', first)]}, now, windows)
    on = auto_resolve_kinds({'anomalies': [kind_card('vwo|x', first)]}, now, windows, business=UTC)
    on_later = auto_resolve_kinds({'anomalies': [kind_card('vwo|x', first)]}, at('2026-11-23 21:00'), windows, business=UTC)

    # then
    assert [r['key'] for r in off] == ['vwo|x']
    assert on == []
    assert [r['key'] for r in on_later] == ['vwo|x'] and on_later[0]['businessTime'] is True
    assert on_later[0]['text'].endswith('nobody acted in 1 business day')


def test_an_info_card_counts_its_close_days_as_business_days():
    # given
    first = at('2026-11-20 12:00')
    now = at('2026-11-23 06:00')

    # when
    off = auto_close_info({'anomalies': [firing_info('edge|a.example.com', first, now)]}, now, 1)
    on = auto_close_info({'anomalies': [firing_info('edge|a.example.com', first, now)]}, now, 1, business=UTC)
    later = at('2026-11-23 13:00')
    on_later = auto_close_info({'anomalies': [firing_info('edge|a.example.com', first, later)]}, later, 1, business=UTC)

    # then
    assert [r['key'] for r in off] == ['edge|a.example.com'] and off[0]['text'].endswith('nobody acted in 1 days')
    assert on == []
    assert [r['key'] for r in on_later] == ['edge|a.example.com'] and on_later[0]['businessTime'] is True
    assert on_later[0]['text'].endswith('nobody acted in 1 business day')


def test_apply_passes_business_time_to_every_rule(store: Store):
    # given
    cleared = at('2026-11-20 22:00')
    now = at('2026-11-23 03:00')
    seeded(store, cleared_card('log|a|ERROR|x', cleared), firing_info('edge|a.example.com', at('2026-11-20 12:00'), now), kind_card('vwo|x', at('2026-11-20 20:00')))
    windows = (({'wingify-verdict'}, 'ab-verdict', 24),)

    # when
    on = apply(store, [], now, severity_hours={'crit': 6, 'warn': 6}, info_close_days=1, kind_windows=windows, business=UTC)
    off = apply(store, [], now, severity_hours={'crit': 6, 'warn': 6}, info_close_days=1, kind_windows=windows)

    # then
    assert on.resolved == []
    assert sorted(r['key'] for r in off.resolved) == ['edge|a.example.com', 'log|a|ERROR|x', 'vwo|x']


def test_the_window_words_name_business_hours_and_days():
    # when
    words = [window_text(n, business=True) for n in (1, 6, 24, 72)]

    # then
    assert words == ['1 business hour', '6 business hours', '1 business day', '3 business days']
    assert window_text(6) == '6 hours'


def test_the_auto_resolved_notes_name_business_time_when_it_was_counted():
    # when
    cleared = auto_text(6, business=True)
    info = auto_text(72, 'info', business=True)
    first = auto_text(24, 'first', business=True)
    event = auto_event_text(12, business=True)

    # then
    assert cleared == '\u2705 Did not recur for 6 business hours, auto-resolved'
    assert info == '\u2705 Auto-resolved: info card firing for 3 business days with nobody acting'
    assert first == '\u2705 Auto-resolved 1 business day after it first appeared'
    assert event == '\u2705 Stopped after the event; nobody opened a task. Auto-resolved after 12 business hours'
    assert auto_text(6) == '\u2705 Did not recur for 6 hours, auto-resolved'


def test_business_days_default_on_and_read_from_config_and_environment(monkeypatch):
    # when
    default = load_settings(lambda: {}).business_days
    config_off = load_settings(lambda: {KEY: 'false'}).business_days
    monkeypatch.setenv('SKOPAX_BUSINESS_DAYS', 'false')
    env_off = load_settings(lambda: {}).business_days

    # then
    assert (default, config_off, env_off) == (True, False, False)


def test_admin_lists_the_business_days_switch_in_cards_and_saves_it(store):
    # given
    live = Live(load_settings(lambda: {}), TunableStore(store.db))

    # when
    entry = {e['key']: e for e in live.entries()}[KEY]
    change = live.set(KEY, 'false', 'alice@example.com')[1]
    after = live.current()

    # then
    assert (entry['group'], entry['kind'], entry['default'], entry['live'], entry['help']) == ('Cards', 'bool', 'true', True, HELP)
    assert (change['before'], change['after']) == ('true', 'false')
    assert after.business_days is False
    assert KEY in dict(CARDS['Cards'])['Archive and resolve']


def test_the_admin_hour_and_day_help_texts_say_which_time_counts(store):
    # given
    live = Live(load_settings(lambda: {}), TunableStore(store.db))

    # when
    listed = {e['key']: e for e in live.entries()}

    # then
    for key in ('core.auto-resolve-hours-crit', 'core.auto-resolve-hours-warn', 'core.info-auto-close-days'):
        assert listed[key]['help'].endswith('With Business days only on, only Monday to Friday counts')


def test_the_admin_api_round_trips_the_business_days_switch(store: Store):
    # given
    seeded(store)

    # when
    with TestClient(create_app(settings(), store)) as c:
        saved = c.put('/api/admin/settings', json={'key': KEY, 'value': 'false'}, headers=AUTH)
        off = c.app.state.runtime.live.current().business_days
        c.put('/api/admin/settings', json={'key': KEY, 'value': 'true'}, headers=AUTH)
        on = c.app.state.runtime.live.current().business_days

    # then
    assert saved.status_code == 200
    assert (off, on) == (False, True)


@pytest.mark.parametrize(('business', 'now', 'resolved', 'span'), [
    (True, '2026-11-23 03:00', False, None),
    (True, '2026-11-23 05:00', True, '6 business hours'),
    (False, '2026-11-23 03:00', True, '6 hours'),
])
def test_the_server_pass_counts_business_time_only_while_the_switch_is_on(store: Store, monkeypatch, business, now, resolved, span):
    # given
    seeded(store, cleared_card('log|a|ERROR|x', at('2026-11-20 22:00')))
    config = dataclasses.replace(settings(), auto_resolve_hours_crit=6, auto_resolve_hours_warn=6, business_days=business)
    noted = []
    monkeypatch.setattr(statenotes.StateNotes, 'auto_resolved', lambda self, keys, hours, since=None, events=(), business=False: noted.append((keys, hours, business)))

    # when
    with TestClient(create_app(config, store)) as c:
        c.app.state.server_apply([], at(now), None)
    card = {a['key']: a for a in store.load()['anomalies']}['log|a|ERROR|x']

    # then
    assert (card.get('triage') == 'handled') is resolved
    if resolved:
        assert card['note']['text'].endswith(f'nobody acted in {span}')
        assert noted == [(['log|a|ERROR|x'], 6, business)]
    else:
        assert noted == []


def test_switching_business_days_off_in_admin_applies_on_the_next_server_pass(store: Store):
    # given
    seeded(store, cleared_card('log|a|ERROR|x', at('2026-11-20 22:00')))
    config = dataclasses.replace(settings(), auto_resolve_hours_crit=6, auto_resolve_hours_warn=6, business_days=True)

    # when
    with TestClient(create_app(config, store)) as c:
        c.app.state.server_apply([], at('2026-11-23 03:00'), None)
        before = {a['key']: a for a in store.load()['anomalies']}['log|a|ERROR|x'].get('triage')
        c.put('/api/admin/settings', json={'key': KEY, 'value': 'false'}, headers=AUTH)
        c.app.state.server_apply([], at('2026-11-23 03:00'), None)
    after = {a['key']: a for a in store.load()['anomalies']}['log|a|ERROR|x']

    # then
    assert before is None
    assert after['triage'] == 'handled' and after['note']['text'].endswith('nobody acted in 6 hours')


def test_stored_notes_and_activity_lines_stay_english_while_the_thread_line_follows_the_pack(store: Store):
    # given
    pack = next(locale for locale in locales.packs() if locale.lang == 'de')
    messages.set_catalogs([('de', pack.messages)])
    seeded(store, cleared_card('log|a|ERROR|x', at('2026-11-20 22:00')))

    # when
    with messages.speaking('de'):
        applied = apply(store, [], at('2026-11-23 05:00'), severity_hours={'crit': 6, 'warn': 6}, business=UTC)
        thread = auto_text(6, business=True)
    card = {a['key']: a for a in store.load()['anomalies']}['log|a|ERROR|x']
    lines = [r['text'] for r in store.list_activity(None, 50) if r['kind'] == 'auto']

    # then
    assert applied.resolved[0]['text'].endswith('nobody acted in 6 business hours')
    assert card['note']['text'].endswith('nobody acted in 6 business hours')
    assert len(lines) == 1 and lines[0].endswith('nobody acted in 6 business hours')
    assert thread == pack.messages['notes.state.auto'].format(span=pack.messages['notes.window.business_hours'].format(n=6))
    assert 'business' not in thread
