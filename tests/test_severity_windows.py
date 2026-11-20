import dataclasses

import pytest
from fastapi.testclient import TestClient

from tests.conftest import AUTH, settings
from tests.test_engine import CART, NOW, ago, auto_lines, seeded, stored, waiting
from skopax.app import create_app
from skopax.config import load_settings
from skopax.contract import SourceData
from skopax.kube import Kube
from skopax.registry import Registry
from skopax.settingsources import Follower, SourceSet
from skopax.engine import apply, auto_close_info, auto_resolve
from skopax.statenotes import auto_text
from skopax.store import Store
from skopax.tunables import Live, TunableStore, apply as apply_overrides

H = 3600
SEVS = ('crit', 'warn')
HOURS_KEYS = {sev: f'core.auto-resolve-hours-{sev}' for sev in SEVS}
HOURS_FIELDS = {sev: f'auto_resolve_hours_{sev}' for sev in SEVS}


def windows(config: dict, monkeypatch, env: dict | None = None) -> tuple:
    for name, value in (env or {}).items():
        monkeypatch.setenv(name, value)
    loaded = load_settings(lambda: config)
    return tuple(getattr(loaded, HOURS_FIELDS[sev]) for sev in SEVS)


def by_key(store: Store) -> dict:
    return {a['key']: a for a in store.load()['anomalies']}


def firing_info(key: str, days: float, **extra) -> dict:
    return stored(key, src='edge', sev='info', firstSeen=ago(days * 24 * H), lastSeen=ago(60), **extra)


def test_each_severity_window_defaults_to_six_hours_and_the_info_close_days_to_three(monkeypatch):
    # given

    # when
    loaded = load_settings(lambda: {})

    # then
    assert windows({}, monkeypatch) == (6, 6)
    assert loaded.info_auto_close_days == 3


def test_a_severity_window_is_read_from_the_config_file_and_from_the_environment(monkeypatch):
    # given

    # when
    from_config = windows({'core.auto-resolve-hours-crit': '48', 'core.auto-resolve-hours-warn': '12'}, monkeypatch)
    from_env = windows({}, monkeypatch, {'SKOPAX_AUTO_RESOLVE_HOURS_CRIT': '72', 'SKOPAX_AUTO_RESOLVE_HOURS_WARN': '2'})

    # then
    assert from_config == (48, 12)
    assert from_env == (72, 2)


def test_the_old_hours_key_seeds_the_severities_that_have_no_key_of_their_own(monkeypatch):
    # given

    # when
    config = windows({'core.auto-resolve-hours': '12', 'core.auto-resolve-hours-warn': '3'}, monkeypatch)
    days = windows({'core.auto-resolve-days': '2'}, monkeypatch)
    off = windows({'core.auto-resolve-hours': '0'}, monkeypatch)
    env = windows({}, monkeypatch, {'SKOPAX_AUTO_RESOLVE_HOURS': '8'})

    # then
    assert config == (12, 3)
    assert days == (48, 48)
    assert off == (0, 0)
    assert env == (8, 8)


def test_a_severity_window_out_of_range_or_not_a_number_falls_back(monkeypatch):
    # given

    # when
    too_big = windows({'core.auto-resolve-hours-crit': '721'}, monkeypatch)
    text = windows({'core.auto-resolve-hours-warn': 'soon'}, monkeypatch)
    seeded_back = windows({'core.auto-resolve-hours': '9', 'core.auto-resolve-hours-warn': '-1'}, monkeypatch)

    # then
    assert too_big == (6, 6)
    assert text == (6, 6)
    assert seeded_back == (9, 9)


def test_the_info_close_days_come_from_config_or_env_and_stay_between_zero_and_ninety(monkeypatch):
    # given

    # when
    config = load_settings(lambda: {'core.info-auto-close-days': ' 7 '}).info_auto_close_days
    monkeypatch.setenv('SKOPAX_INFO_AUTO_CLOSE_DAYS', '10')
    env = load_settings(lambda: {}).info_auto_close_days
    monkeypatch.setenv('SKOPAX_INFO_AUTO_CLOSE_DAYS', '0')
    off = load_settings(lambda: {}).info_auto_close_days
    monkeypatch.setenv('SKOPAX_INFO_AUTO_CLOSE_DAYS', '91')
    too_big = load_settings(lambda: {}).info_auto_close_days
    monkeypatch.setenv('SKOPAX_INFO_AUTO_CLOSE_DAYS', 'many')
    text = load_settings(lambda: {}).info_auto_close_days

    # then
    assert (config, env, off, too_big, text) == (7, 10, 0, 3, 3)


def follow(store, tmp_path, stored_old: dict | None = None):
    start = SourceData('feed', {'investigate.max-cards': '4'})
    box = {'data': start}
    sources = SourceSet([('feed', lambda: box['data'])], {'feed': start})
    client = TestClient(create_app(settings(), store, sources=sources, registry=Registry.of(), kube=Kube(root=tmp_path)))
    client.__enter__()
    live = client.app.state.runtime.live
    for key, value in (stored_old or {}).items():
        live.tunables.write(key, value, 'alice@example.com', None)
    follower = Follower(sources, live, client.app.state.runtime.instances, {})

    def move(**values):
        box['data'] = SourceData('feed', {'investigate.max-cards': '4', **{k.replace('_', '-').replace('core-', 'core.'): v for k, v in values.items()}})
        follower.tick()
        live.refresh(force=True)
        now = live.current()
        return now.auto_resolve_hours_crit, now.auto_resolve_hours_warn, now.info_auto_close_days

    live.refresh(force=True)
    return client, live, move


def test_a_source_changing_the_old_hours_key_moves_both_severity_windows_live(store, tmp_path):
    # given
    client, live, move = follow(store, tmp_path)

    # when
    added = move(core_auto_resolve_hours='12')
    changed = move(core_auto_resolve_hours='20')
    removed = move()
    client.__exit__(None, None, None)

    # then
    assert added == (12, 12, 3)
    assert changed == (20, 20, 3)
    assert removed == (6, 6, 3)


def test_a_source_adding_a_severity_window_beats_a_stored_admin_old_value_and_removing_it_restores_seeding(store, tmp_path):
    # given
    client, live, move = follow(store, tmp_path, {'core.auto-resolve-hours': '9'})

    # when
    before = (live.current().auto_resolve_hours_crit, live.current().auto_resolve_hours_warn)
    added = move(core_auto_resolve_hours_crit='30')
    marks = live.current().auto_resolve_sev_set
    removed = move()
    client.__exit__(None, None, None)

    # then
    assert before == (9, 9)
    assert added[:2] == (30, 9) and marks == ('crit',)
    assert removed[:2] == (9, 9) and live.current().auto_resolve_sev_set == ()


def test_a_source_setting_the_old_hours_to_zero_turns_the_info_days_off_until_they_are_set(store, tmp_path):
    # given
    client, live, move = follow(store, tmp_path)

    # when
    off = move(core_auto_resolve_hours='0')
    own = move(core_auto_resolve_hours='0', core_info_auto_close_days='5')
    back = move(core_auto_resolve_hours='0')
    removed = move()
    client.__exit__(None, None, None)

    # then
    assert off == (0, 0, 0)
    assert own == (0, 0, 5)
    assert back == (0, 0, 0)
    assert removed == (6, 6, 3)


def test_an_admin_old_hours_value_seeds_only_severities_without_their_own_key():
    # given

    # when
    seeded_all = apply_overrides(load_settings(lambda: {}), {'core.auto-resolve-hours': '9'})
    explicit_admin = apply_overrides(load_settings(lambda: {}), {'core.auto-resolve-hours': '9', HOURS_KEYS['crit']: '30'})
    explicit_config = apply_overrides(load_settings(lambda: {'core.auto-resolve-hours-warn': '4'}), {'core.auto-resolve-hours': '9'})
    days_chain = apply_overrides(load_settings(lambda: {}), {'core.auto-resolve-days': '2'})

    # then
    assert [getattr(seeded_all, HOURS_FIELDS[s]) for s in SEVS] == [9, 9]
    assert [getattr(explicit_admin, HOURS_FIELDS[s]) for s in SEVS] == [30, 9]
    assert [getattr(explicit_config, HOURS_FIELDS[s]) for s in SEVS] == [9, 4]
    assert [getattr(days_chain, HOURS_FIELDS[s]) for s in SEVS] == [48, 48]


def test_admin_lists_two_severity_rows_and_the_info_days_and_hides_the_old_hours_key(store):
    # given
    live = Live(load_settings(lambda: {}), TunableStore(store.db))

    # when
    listed = {e['key']: e for e in live.entries()}
    change = live.set(HOURS_KEYS['crit'], '48', 'alice@example.com')[1]
    live.set('core.info-auto-close-days', '5', 'alice@example.com')
    after = live.current()

    # then
    assert 'core.auto-resolve-hours' not in listed
    crit, warn = (listed[HOURS_KEYS[s]] for s in SEVS)
    assert [e['label'] for e in (crit, warn)] == ['Auto-resolve cleared critical cards after (hours)', 'Auto-resolve cleared warning cards after (hours)']
    assert [(e['group'], e['kind'], e['low'], e['high'], e['default'], e['live']) for e in (crit, warn)] == [('Cards', 'int', 0, 720, '6', True)] * 2
    assert 'core.auto-resolve-hours-info' not in listed
    days = listed['core.info-auto-close-days']
    assert (days['group'], days['label'], days['kind'], days['low'], days['high'], days['default'], days['live']) == (
        'Cards', 'Auto-resolve info cards still firing after (days)', 'int', 0, 90, '3', True)
    assert 'Closes an info card that is still firing and that nobody acted on after N days; 0 turns it off.' in days['help']
    assert 'Log cards and info cards from warning or error signals are kept' in days['help']
    assert (change['label'], change['before'], change['after']) == ('Auto-resolve cleared critical cards after (hours)', '6', '48')
    assert (after.auto_resolve_hours_crit, after.auto_resolve_hours_warn, after.info_auto_close_days) == (48, 6, 5)
    with pytest.raises(KeyError):
        live.set('core.auto-resolve-hours', '10', 'alice@example.com')


def test_a_stored_old_hours_value_still_seeds_the_severities_through_live(store):
    # given
    tunables = TunableStore(store.db)
    tunables.write('core.auto-resolve-hours', '9', 'alice@example.com', None)
    live = Live(load_settings(lambda: {}), tunables)

    # when
    live.refresh(force=True)
    seen = live.current()
    live.set(HOURS_KEYS['warn'], '2', 'alice@example.com')
    after = live.current()

    # then
    assert (seen.auto_resolve_hours_crit, seen.auto_resolve_hours_warn) == (9, 9)
    assert (after.auto_resolve_hours_crit, after.auto_resolve_hours_warn) == (9, 2)


def test_the_admin_api_saves_a_severity_window_and_the_history_names_it(store: Store):
    # given
    seeded(store)

    # when
    with TestClient(create_app(settings(), store)) as c:
        saved = c.put('/api/admin/settings', json={'key': HOURS_KEYS['warn'], 'value': '2'}, headers=AUTH)
        bad = c.put('/api/admin/settings', json={'key': 'core.info-auto-close-days', 'value': '91'}, headers=AUTH)
        c.put('/api/admin/settings', json={'key': 'core.info-auto-close-days', 'value': '5'}, headers=AUTH)
        history = c.get('/api/admin/history', headers=AUTH).json()
        live = c.app.state.runtime.live.current()

    # then
    assert saved.status_code == 200 and bad.status_code == 422
    assert (live.auto_resolve_hours_warn, live.info_auto_close_days) == (2, 5)
    assert {(h['key'], h['new']) for h in history} >= {(HOURS_KEYS['warn'], '2'), ('core.info-auto-close-days', '5')}


def test_each_severity_resolves_on_its_own_window(store: Store):
    # given
    seeded(store, waiting('log|a|ERROR|crit-old', cleared=25 * H, sev='crit'), waiting('log|b|ERROR|crit-young', cleared=23 * H, sev='crit'),
           waiting('log|c|ERROR|warn-old', cleared=7 * H, sev='warn'), waiting('log|d|ERROR|warn-young', cleared=5 * H, sev='warn'),
           waiting('log|e|ERROR|info-old', cleared=30 * H, sev='info'))

    # when
    applied = apply(store, [], NOW, severity_hours={'crit': 24, 'warn': 6})
    cards = by_key(store)

    # then
    assert {k for k, a in cards.items() if a.get('triage') == 'handled'} == {'log|a|ERROR|crit-old', 'log|c|ERROR|warn-old'}
    assert {r['key']: r['hours'] for r in applied.resolved} == {'log|a|ERROR|crit-old': 24, 'log|c|ERROR|warn-old': 6}
    assert cards['log|a|ERROR|crit-old']['note']['text'].endswith('nobody acted in 1 day')
    assert cards['log|c|ERROR|warn-old']['note']['text'].endswith('nobody acted in 6 hours')
    assert 'log|e|ERROR|info-old' not in cards, 'a cleared info card is never auto-resolved; it leaves the page after a day'
    assert {r['key'] for r in auto_lines(store)} == {'log|a|ERROR|crit-old', 'log|c|ERROR|warn-old'}


def test_zero_for_one_severity_leaves_the_others_running(store: Store):
    # given
    seeded(store, waiting('log|a|ERROR|crit', cleared=20 * H, sev='crit'), waiting('log|b|ERROR|warn', cleared=20 * H, sev='warn'))

    # when
    apply(store, [], NOW, severity_hours={'crit': 0, 'warn': 6})
    cards = by_key(store)

    # then
    assert cards['log|a|ERROR|crit'].get('triage') is None
    assert cards['log|b|ERROR|warn']['triage'] == 'handled'


def test_the_event_extra_hours_are_added_to_the_severity_window(store: Store):
    # given
    cause = {'kind': 'event', 'what': 'a push', 'firstSeen': ago(7200)}
    seeded(store, waiting('log|a|ERROR|crit', cleared=31 * H, cause=cause, sev='crit'), waiting('log|b|ERROR|warn', cleared=31 * H, cause=cause, sev='warn'),
           waiting('log|c|ERROR|crit-early', cleared=29 * H, cause=cause, sev='crit'))

    # when
    applied = apply(store, [], NOW, severity_hours={'crit': 24, 'warn': 72})

    # then
    assert {r['key']: r['hours'] for r in applied.resolved} == {'log|a|ERROR|crit': 30}
    assert by_key(store)['log|b|ERROR|warn'].get('triage') is None


def test_the_plain_hours_argument_still_applies_to_every_waiting_severity(store: Store):
    # given
    seeded(store, waiting('log|a|ERROR|crit', cleared=7 * H, sev='crit'), waiting('log|b|ERROR|warn', cleared=7 * H, sev='warn'))

    # when
    applied = apply(store, [], NOW, auto_resolve_hours=6)

    # then
    assert {r['key'] for r in applied.resolved} == {'log|a|ERROR|crit', 'log|b|ERROR|warn'}


def test_a_severity_hours_map_with_every_window_zero_turns_the_cleared_rule_off(store: Store):
    # given
    seeded(store, waiting(CART, cleared=90 * H, sev='crit'))

    # when
    apply(store, [], NOW, severity_hours={'crit': 0, 'warn': 0})

    # then
    assert store.anomaly(CART).get('triage') is None
    assert auto_lines(store) == []


def test_auto_resolve_takes_a_per_severity_map_directly():
    # given
    state = {'anomalies': [waiting('log|a|ERROR|crit', cleared=7 * H, sev='crit'), waiting('log|b|ERROR|warn', cleared=7 * H, sev='warn')]}

    # when
    done = auto_resolve(state, NOW, 6, (), {'crit': 12, 'warn': 6})

    # then
    assert [d['key'] for d in done] == ['log|b|ERROR|warn']


def test_an_info_card_closes_after_the_configured_days_and_not_before():
    # given
    state = {'anomalies': [firing_info('edge|a.example.com', 6.5), firing_info('edge|b.example.com', 5.5), firing_info('edge|c.example.com', 3.5)]}

    # when
    out = auto_close_info(state, NOW, 6)

    # then
    assert [(r['key'], r['hours'], r['since']) for r in out] == [('edge|a.example.com', 144, 'info')]
    assert out[0]['text'].endswith('nobody acted in 6 days')
    assert auto_text(144, 'info') == '\u2705 Auto-resolved: info card firing for 6 days with nobody acting'


def test_zero_info_close_days_turns_only_the_info_rule_off(store: Store):
    # given
    seeded(store, firing_info('edge|a.example.com', 80), waiting('log|b|ERROR|plain', cleared=90 * H, sev='warn'))

    # when
    applied = apply(store, [], NOW, info_close_days=0)

    # then
    assert {r['key'] for r in applied.resolved} == {'log|b|ERROR|plain'}
    assert by_key(store)['edge|a.example.com'].get('triage') is None


def test_the_info_still_firing_rule_runs_with_the_configured_days_through_apply(store: Store):
    # given
    seeded(store, firing_info('edge|a.example.com', 6.5), firing_info('edge|b.example.com', 5.5))

    # when
    applied = apply(store, [], NOW, info_close_days=6)

    # then
    assert [(r['key'], r['hours']) for r in applied.resolved] == [('edge|a.example.com', 144)]


def test_the_info_still_firing_rule_ignores_the_hours_windows(store: Store):
    # given
    seeded(store, firing_info('edge|a.example.com', 80))

    # when
    applied = apply(store, [], NOW, severity_hours={'crit': 0, 'warn': 0}, info_close_days=3)

    # then
    assert [r['key'] for r in applied.resolved] == ['edge|a.example.com']


def test_parents_are_followed_while_any_severity_window_is_positive(store: Store):
    # given
    child = waiting('log|kid|ERROR|x', cleared=H, sev='warn', parent='log|dad|ERROR|x')
    dad = waiting('log|dad|ERROR|x', cleared=H, sev='crit', triage='handled', triagedBy='oscar')
    seeded(store, child, dad)

    # when
    off = apply(store, [], NOW, severity_hours={'crit': 0, 'warn': 0})
    kept = by_key(store)['log|kid|ERROR|x'].get('triage')
    on = apply(store, [], NOW + 60, severity_hours={'crit': 0, 'warn': 6})

    # then
    assert off.resolved == [] and kept is None
    assert [r['key'] for r in on.resolved] == ['log|kid|ERROR|x']


def test_the_server_pass_uses_the_severity_windows_and_the_info_days_with_matching_notes(store: Store):
    # given
    seeded(store, waiting('log|a|ERROR|crit', cleared=7 * H, sev='crit'), waiting('log|b|ERROR|warn', cleared=7 * H, sev='warn'), firing_info('edge|a.example.com', 6.5))
    config = dataclasses.replace(settings(), auto_resolve_hours_crit=24, auto_resolve_hours_warn=6, info_auto_close_days=6)

    # when
    with TestClient(create_app(config, store)) as c:
        c.app.state.server_apply([], NOW, None)
    cards = by_key(store)

    # then
    assert cards['log|a|ERROR|crit'].get('triage') is None
    assert cards['log|b|ERROR|warn']['triage'] == 'handled'
    assert cards['edge|a.example.com']['triage'] == 'handled'
    assert cards['edge|a.example.com']['note']['text'].endswith('nobody acted in 6 days')


def test_an_old_explicit_off_turns_the_info_days_off_unless_they_are_set(monkeypatch):
    # given
    zero_hours = {'core.auto-resolve-hours': '0'}

    # when
    from_hours = load_settings(lambda: zero_hours)
    from_days = load_settings(lambda: {'core.auto-resolve-days': '0'})
    own = load_settings(lambda: {**zero_hours, 'core.info-auto-close-days': '5'})
    on = load_settings(lambda: {'core.auto-resolve-hours': '12'})
    stored = apply_overrides(load_settings(lambda: {}), zero_hours)
    stored_own = apply_overrides(load_settings(lambda: {'core.info-auto-close-days': '4'}), zero_hours)
    stored_days = apply_overrides(load_settings(lambda: {}), {'core.auto-resolve-days': '0'})

    # then
    assert (from_hours.info_auto_close_days, from_days.info_auto_close_days, own.info_auto_close_days, on.info_auto_close_days) == (0, 0, 5, 3)
    assert (from_hours.info_auto_close_days_set, own.info_auto_close_days_set, on.info_auto_close_days_set) == (False, True, False)
    assert (stored.info_auto_close_days, stored_own.info_auto_close_days, stored_days.info_auto_close_days) == (0, 4, 0)


def test_a_severity_value_counts_as_set_only_when_it_parsed_in_range(monkeypatch):
    # given

    # when
    bad = load_settings(lambda: {'core.auto-resolve-hours': '9', 'core.auto-resolve-hours-crit': '721', 'core.auto-resolve-hours-warn': '5', 'core.info-auto-close-days': '91'})

    # then
    assert (bad.auto_resolve_hours_crit, bad.auto_resolve_hours_warn, bad.auto_resolve_sev_set, bad.info_auto_close_days_set) == (9, 5, ('warn',), False)


def test_a_card_whose_severity_changed_takes_the_window_of_its_current_severity(store: Store):
    # given
    seeded(store, waiting('log|a|ERROR|down', cleared=7 * H, sev='warn', pushedSev='crit', sevSet={'sev': 'warn', 'by': 'alice@example.com', 'at': ago(H), 'auto': 'crit'}),
           waiting('log|b|ERROR|up', cleared=7 * H, sev='crit', pushedSev='warn', sevSet={'sev': 'crit', 'by': 'alice@example.com', 'at': ago(H), 'auto': 'warn'}))

    # when
    applied = apply(store, [], NOW, severity_hours={'crit': 24, 'warn': 6})

    # then
    assert [r['key'] for r in applied.resolved] == ['log|a|ERROR|down']


def old_hours_live(store, **base):
    tunables = TunableStore(store.db)
    tunables.write('core.auto-resolve-hours', '9', 'bob@example.com', None)
    live = Live(load_settings(lambda: base), tunables)
    live.refresh(force=True)
    return live


def test_admin_shows_a_value_seeded_by_the_stored_old_hours_as_an_old_setting_with_its_author(store):
    # given
    live = old_hours_live(store)

    # when
    listed = {e['key']: e for e in live.entries()}

    # then
    for key in HOURS_KEYS.values():
        assert (listed[key]['source'], listed[key]['by'], listed[key]['value'], listed[key]['oldSwitch']) == ('admin', 'bob@example.com', '9', 'core.auto-resolve-hours')
    assert 'oldSwitch' not in listed['core.info-auto-close-days']


def test_a_severity_with_its_own_value_or_config_is_not_marked_as_the_old_setting(store):
    # given
    own = old_hours_live(store, **{'core.auto-resolve-hours-warn': '4'})

    # when
    marked = {e['key']: e.get('oldSwitch') for e in own.entries()}
    own.set(HOURS_KEYS['crit'], '30', 'alice@example.com')
    after = {e['key']: e.get('oldSwitch') for e in own.entries()}

    # then
    assert (marked[HOURS_KEYS['crit']], marked[HOURS_KEYS['warn']]) == ('core.auto-resolve-hours', None)
    assert after[HOURS_KEYS['crit']] is None and after[HOURS_KEYS['warn']] is None


def test_saving_one_severity_writes_the_other_explicitly_and_clears_the_old_key(store):
    # given
    live = old_hours_live(store)

    # when
    entry, change = live.set(HOURS_KEYS['crit'], '30', 'alice@example.com')
    now = live.current()
    rows = live.tunables.history(10)
    stored = live.tunables.all()

    # then
    assert (now.auto_resolve_hours_crit, now.auto_resolve_hours_warn) == (30, 9)
    assert 'core.auto-resolve-hours' not in stored and stored[HOURS_KEYS['warn']]['value'] == '9' and stored[HOURS_KEYS['crit']]['value'] == '30'
    assert (change['before'], change['after']) == ('9', '30')
    legacy = {r['key']: r for r in rows if r.get('legacy')}
    assert set(legacy) == {'core.auto-resolve-hours', HOURS_KEYS['warn']}
    assert (legacy['core.auto-resolve-hours']['old'], legacy['core.auto-resolve-hours']['new']) == ('9', None)
    assert not [r for r in rows if r['key'] == HOURS_KEYS['crit'] and r.get('legacy')]


def test_reset_on_a_seeded_severity_clears_the_old_key_keeps_the_other_and_returns_the_deletion_row(store):
    # given
    live = old_hours_live(store)

    # when
    entry, change = live.set(HOURS_KEYS['warn'], None, 'alice@example.com')
    now = live.current()
    stored = live.tunables.all()

    # then
    assert change['key'] == 'core.auto-resolve-hours' and change['legacy'] is True
    assert (now.auto_resolve_hours_crit, now.auto_resolve_hours_warn) == (9, 6)
    assert 'core.auto-resolve-hours' not in stored and stored[HOURS_KEYS['crit']]['value'] == '9' and HOURS_KEYS['warn'] not in stored
    assert entry['source'] == 'default' and 'oldSwitch' not in entry


def test_clearing_a_stored_old_off_keeps_the_info_days_off(store):
    # given
    tunables = TunableStore(store.db)
    tunables.write('core.auto-resolve-hours', '0', 'bob@example.com', None)
    live = Live(load_settings(lambda: {}), tunables)
    live.refresh(force=True)

    # when
    live.set(HOURS_KEYS['crit'], '12', 'alice@example.com')
    now = live.current()

    # then
    assert (now.auto_resolve_hours_crit, now.auto_resolve_hours_warn, now.info_auto_close_days) == (12, 0, 0)


def test_a_config_seeded_severity_names_the_old_keys_source(store):
    # given
    base = load_settings(lambda: {'core.auto-resolve-hours': '12'})
    live = Live(base, TunableStore(store.db), values={'core.auto-resolve-hours': '12'}, labels={'core.auto-resolve-hours': 'config file'})

    # when
    listed = {e['key']: e for e in live.entries()}

    # then
    assert (listed[HOURS_KEYS['crit']]['source'], listed[HOURS_KEYS['crit']]['from'], listed[HOURS_KEYS['crit']]['value']) == ('config', 'config file', '12')


def test_a_stored_old_value_that_turns_hours_back_on_restores_the_info_days_over_a_config_off():
    # given
    off = load_settings(lambda: {'core.auto-resolve-hours': '0'})
    days_off = load_settings(lambda: {'core.auto-resolve-days': '0'})

    # when
    on = apply_overrides(off, {'core.auto-resolve-hours': '6'})
    on_days = apply_overrides(days_off, {'core.auto-resolve-days': '2'})
    still_off = apply_overrides(off, {'core.auto-resolve-hours': '0'})
    own = apply_overrides(load_settings(lambda: {'core.auto-resolve-hours': '0', 'core.info-auto-close-days': '5'}), {'core.auto-resolve-hours': '6'})

    # then
    assert (on.info_auto_close_days, on_days.info_auto_close_days, still_off.info_auto_close_days, own.info_auto_close_days) == (3, 3, 0, 5)


def test_saving_a_severity_pins_the_info_days_a_stored_old_value_restored_over_a_config_off(store):
    # given
    tunables = TunableStore(store.db)
    tunables.write('core.auto-resolve-hours', '6', 'bob@example.com', None)
    live = Live(load_settings(lambda: {'core.auto-resolve-hours': '0'}), tunables)
    live.refresh(force=True)

    # when
    before = live.current().info_auto_close_days
    live.set(HOURS_KEYS['crit'], '12', 'alice@example.com')
    now = live.current()
    pin = [r for r in tunables.history(10) if r['key'] == 'core.info-auto-close-days']

    # then
    assert before == 3
    assert (now.auto_resolve_hours_crit, now.auto_resolve_hours_warn, now.info_auto_close_days) == (12, 6, 3)
    assert [(r['old'], r['new'], r.get('legacy')) for r in pin] == [(None, '3', True)]


def test_clearing_a_stored_old_off_pins_the_info_days_as_a_legacy_row(store):
    # given
    tunables = TunableStore(store.db)
    tunables.write('core.auto-resolve-hours', '0', 'bob@example.com', None)
    live = Live(load_settings(lambda: {}), tunables)
    live.refresh(force=True)

    # when
    live.set(HOURS_KEYS['warn'], '12', 'alice@example.com')
    pin = [r for r in tunables.history(10) if r['key'] == 'core.info-auto-close-days']

    # then
    assert [(r['old'], r['new'], r.get('legacy')) for r in pin] == [(None, '0', True)]


def test_the_info_days_row_names_the_old_key_that_turned_it_off_without_claiming_admin(store):
    # given
    base = load_settings(lambda: {'core.auto-resolve-hours': '0'})
    live = Live(base, TunableStore(store.db), values={'core.auto-resolve-hours': '0'}, labels={'core.auto-resolve-hours': 'config file'})

    # when
    row = {e['key']: e for e in live.entries()}['core.info-auto-close-days']
    on = old_hours_live(store)
    quiet = {e['key']: e for e in on.entries()}['core.info-auto-close-days']

    # then
    assert (row['oldSwitch'], row['source'], row['from'], row['value']) == ('core.auto-resolve-hours', 'config', 'config file', '0')
    assert 'oldSwitch' not in quiet


def test_a_stored_days_row_beside_a_stored_hours_row_is_ignored_for_the_tag_but_cleared_with_it(store):
    # given
    tunables = TunableStore(store.db)
    tunables.write('core.auto-resolve-hours', '9', 'bob@example.com', None)
    tunables.write('core.auto-resolve-days', '2', 'bob@example.com', None)
    live = Live(load_settings(lambda: {}), tunables)
    live.refresh(force=True)

    # when
    row = {e['key']: e for e in live.entries()}[HOURS_KEYS['crit']]
    live.set(HOURS_KEYS['crit'], '30', 'alice@example.com')
    stored = tunables.all()
    now = live.current()

    # then
    assert row['oldSwitch'] == 'core.auto-resolve-hours'
    assert 'core.auto-resolve-hours' not in stored and 'core.auto-resolve-days' not in stored
    assert (now.auto_resolve_hours_crit, now.auto_resolve_hours_warn) == (30, 9)


def test_a_card_cleared_seven_hours_ago_with_info_severity_stays_untriaged(store: Store):
    # given
    seeded(store, waiting('log|a|ERROR|quiet', cleared=7 * H, sev='info'))

    # when
    applied = apply(store, [], NOW, severity_hours={'crit': 6, 'warn': 6})

    # then
    assert applied.resolved == []
    assert by_key(store)['log|a|ERROR|quiet'].get('triage') is None


def test_saving_a_severity_does_not_pin_the_other_when_it_equals_the_base(store):
    # given
    tunables = TunableStore(store.db)
    tunables.write('core.auto-resolve-hours', '6', 'bob@example.com', None)
    live = Live(load_settings(lambda: {}), tunables)
    live.refresh(force=True)

    # when
    live.set(HOURS_KEYS['crit'], '12', 'alice@example.com')
    stored = tunables.all()
    rows = tunables.history(10)

    # then
    assert HOURS_KEYS['warn'] not in stored and 'core.auto-resolve-hours' not in stored
    assert not [r for r in rows if r['key'] == HOURS_KEYS['warn']]
    assert (live.current().auto_resolve_hours_crit, live.current().auto_resolve_hours_warn) == (12, 6)


def test_saving_a_severity_at_its_seeded_value_writes_no_history_row_but_still_pins_it(store):
    # given
    live = old_hours_live(store)
    before = len(live.tunables.history(50))

    # when
    entry, change = live.set(HOURS_KEYS['crit'], '9', 'alice@example.com')
    rows = live.tunables.history(50)
    stored = live.tunables.all()

    # then
    assert not [r for r in rows if r['key'] == HOURS_KEYS['crit']], 'no 9 -> 9 row'
    assert not [r for r in rows if r.get('old') == r.get('new')]
    assert stored[HOURS_KEYS['crit']]['value'] == '9' and stored[HOURS_KEYS['crit']]['by'] == 'alice@example.com'
    assert stored[HOURS_KEYS['warn']]['value'] == '9' and 'core.auto-resolve-hours' not in stored
    assert (change['before'], change['after']) == ('9', '9')
    assert len(rows) == before + 2
    assert (entry['source'], entry['value']) == ('admin', '9')
    assert live.tunables.rev() > 0


def test_a_stored_old_off_names_itself_on_the_info_days_row_while_the_row_stays_a_default(store):
    # given
    tunables = TunableStore(store.db)
    tunables.write('core.auto-resolve-hours', '0', 'bob@example.com', None)
    live = Live(load_settings(lambda: {}), tunables)
    live.refresh(force=True)

    # when
    row = {e['key']: e for e in live.entries()}['core.info-auto-close-days']

    # then
    assert (row['oldSwitch'], row['source'], row['value']) == ('core.auto-resolve-hours', 'default', '0')
    assert row['by'] is None


def test_a_stored_old_days_row_is_ignored_when_the_config_sets_the_old_hours(store):
    # given
    tunables = TunableStore(store.db)
    tunables.write('core.auto-resolve-days', '2', 'bob@example.com', None)
    live = Live(load_settings(lambda: {'core.auto-resolve-hours': '12'}), tunables, values={'core.auto-resolve-hours': '12'})
    live.refresh(force=True)

    # when
    seen = live.stored_old_hours()
    every = live.stored_old_hours(True)
    row = {e['key']: e for e in live.entries()}[HOURS_KEYS['crit']]

    # then
    assert seen == []
    assert [k for k, _ in every] == ['core.auto-resolve-days']
    assert (row['source'], row['value'], row['from']) == ('config', '12', 'environment')
    assert 'oldSwitch' not in row


def test_an_old_off_set_outside_the_files_names_the_environment_on_the_info_days_row(store):
    # given
    base = load_settings(lambda: {'core.auto-resolve-hours': '0'})
    live = Live(base, TunableStore(store.db), values={'core.auto-resolve-hours': '0'})

    # when
    row = {e['key']: e for e in live.entries()}['core.info-auto-close-days']

    # then
    assert (row['oldSwitch'], row['source'], row['from'], row['value']) == ('core.auto-resolve-hours', 'config', 'environment', '0')
