import dataclasses
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from tests.conftest import AUTH, settings
from tests.test_engine import CART, NOW, ago, auto_lines, item, seeded, stored, waiting
from skopax.app import create_app
from skopax.contract import AutoResolve
from skopax.engine import PassResult, follow_parents, apply, auto_resolve, auto_resolve_kinds, window_text
from skopax.plugins import google_ads, vwo
from skopax.runtime import Runtime
from skopax.statenotes import auto_text
from skopax.store import Store
from skopax.sealer import master_key
from tests.plugin_kit import add_instance

H = 3600
VERDICT = 'ab-verdict'
DISAPPROVED = 'ad-disapproved'
VWO_WINDOW = (frozenset({'wingify-verdict'}), VERDICT, 24)


def verdict(key: str, first_h: float, status: str = 'open', **extra) -> dict:
    return stored(f'{VERDICT}|{key}', status=status, src='wingify', ranKey='wingify-verdict', firstSeen=ago(first_h * H), **extra)


def by_key(store: Store) -> dict:
    return {a['key']: a for a in store.load()['anomalies']}


def test_the_window_reads_whole_days_when_the_hours_divide_by_24():
    # given

    # when
    got = [window_text(h) for h in (1, 6, 23, 24, 36, 72, 720)]

    # then
    assert got == ['1 hour', '6 hours', '23 hours', '1 day', '36 hours', '3 days', '30 days']


def test_the_slack_note_says_the_unit_of_the_window():
    # given

    # when
    got = [auto_text(h) for h in (6, 24, 72)]

    # then
    assert got == ['\u2705 Did not recur for 6 hours, auto-resolved', '\u2705 Did not recur for 1 day, auto-resolved', '\u2705 Did not recur for 3 days, auto-resolved']


def test_a_card_cleared_longer_ago_than_the_hours_resolves_with_an_hours_note(store: Store):
    # given
    seeded(store, waiting(CART, cleared=6 * H + 60), waiting('log|payments-api|ERROR|x', cleared=5 * H + 3000))

    # when
    apply(store, [], NOW, auto_resolve_hours=6)
    cards = by_key(store)

    # then
    old, young = cards[CART], cards['log|payments-api|ERROR|x']
    assert (old['triage'], old['triagedBy'], old['autoResolvedAt']) == ('handled', 'auto', ago(0))
    assert old['note']['text'].endswith('nobody acted in 6 hours')
    assert young.get('triage') is None
    assert [r['text'].endswith('nobody acted in 6 hours') for r in auto_lines(store)] == [True]


def test_a_card_that_cleared_and_fired_again_inside_the_window_is_not_resolved(store: Store):
    # given
    seeded(store, stored(CART, src='logs', label='ERROR pattern', firstSeen=ago(H), lastSeen=ago(60)))

    # when
    apply(store, [], NOW, auto_resolve_hours=6)

    # then
    assert store.anomaly(CART).get('triage') is None


def test_the_hours_rule_skips_investigating_muted_and_ticketed_cards():
    # given
    state = {'anomalies': [
        waiting('log|a|ERROR|plain', 7 * H),
        waiting('log|b|ERROR|investigating', 7 * H, triage='investigating'),
        waiting('log|c|ERROR|muted', 7 * H, muted='muted on the page for 24 h'),
        waiting('log|d|ERROR|ticket', 7 * H, triage='progress', ticket='OPS-1'),
    ]}

    # when
    done = auto_resolve(state, NOW, 6)

    # then
    assert [d['key'] for d in done] == ['log|a|ERROR|plain']


def test_zero_hours_turns_the_global_rule_off():
    # given
    state = {'anomalies': [waiting('log|a|ERROR|plain', 400 * H)]}

    # when
    done = auto_resolve(state, NOW, 0)

    # then
    assert done == []


def test_a_verdict_still_firing_after_25_hours_resolves_and_one_at_23_hours_does_not():
    # given
    state = {'anomalies': [verdict('old', 25), verdict('young', 23)]}

    # when
    done = auto_resolve_kinds(state, NOW, (VWO_WINDOW,))
    cards = {a['key']: a for a in state['anomalies']}

    # then
    assert [d['key'] for d in done] == [f'{VERDICT}|old']
    old = cards[f'{VERDICT}|old']
    assert (old['status'], old['triage'], old['triagedBy'], old['autoResolvedAt']) == ('open', 'handled', 'auto', ago(0))
    assert old['resolution']['by'] == 'auto'
    assert old['note']['text'].startswith('Auto-resolved: first seen ') and old['note']['text'].endswith('nobody acted in 1 day')
    assert cards[f'{VERDICT}|young'].get('triage') is None
    assert done[0]['hours'] == 24 and done[0]['since'] == 'first'


def test_a_verdict_someone_acted_on_is_not_auto_resolved():
    # given
    state = {'anomalies': [
        verdict('taken', 30, triage='progress', owner='alice@example.com'),
        verdict('ticket', 30, ticket='OPS-9'),
        verdict('muted', 30, muted='muted on the page for 24 h'),
        verdict('investigating', 30, triage='investigating'),
        verdict('handled', 30, triage='handled', triagedBy='alice@example.com'),
        verdict('plain', 30),
    ]}

    # when
    done = auto_resolve_kinds(state, NOW, (VWO_WINDOW,))

    # then
    assert [d['key'] for d in done] == [f'{VERDICT}|plain']


def test_another_kind_and_another_plugins_cards_are_untouched():
    # given
    other_sig = stored('wingify-change|x', src='wingify', ranKey='wingify', firstSeen=ago(80 * H))
    other_plugin = stored(f'{VERDICT}|elsewhere', src='gads-policy', ranKey='gads-policy', firstSeen=ago(80 * H))
    state = {'anomalies': [other_sig, other_plugin, verdict('mine', 80)]}

    # when
    done = auto_resolve_kinds(state, NOW, (VWO_WINDOW,))

    # then
    assert [d['key'] for d in done] == [f'{VERDICT}|mine']
    assert other_sig.get('triage') is None and other_plugin.get('triage') is None


def test_a_covered_card_follows_its_own_window_not_the_global_rule():
    # given
    state = {'anomalies': [verdict('cleared-early', 10, status='cleared', clearedAt=ago(8 * H)), waiting('log|a|ERROR|plain', 8 * H)]}

    # when
    done = auto_resolve(state, NOW, 6, (VWO_WINDOW,))

    # then
    assert [d['key'] for d in done] == ['log|a|ERROR|plain']


def test_a_resolved_verdict_that_still_fires_stays_resolved_through_the_next_pass(store: Store):
    # given
    seeded(store, verdict('old', 30))
    result = PassResult('wingify-verdict', True, 'ok', [item(f'{VERDICT}|old', src='wingify', ranKey='wingify-verdict')], {}, ('wingify-verdict',))

    # when
    apply(store, [result], NOW, kind_windows=(VWO_WINDOW,))
    first = store.anomaly(f'{VERDICT}|old')
    apply(store, [result], NOW + 600, kind_windows=(VWO_WINDOW,))
    second = store.anomaly(f'{VERDICT}|old')

    # then
    assert (first['status'], first['triage']) == ('open', 'handled')
    assert (second['status'], second['triage'], second['triagedBy']) == ('open', 'handled', 'auto')
    assert len(auto_lines(store)) == 1


def test_a_kind_window_of_zero_is_dropped_and_the_global_rule_applies(store: Store):
    # given
    ctx = SimpleNamespace(_warn=lambda *a: None, _plugins=lambda: [vwo.PLUGIN, google_ads.PLUGIN], setting=lambda plugin_id, key: 0 if plugin_id == 'vwo' else 24)
    seeded(store, verdict('cleared', 20, status='cleared', clearedAt=ago(7 * H)), verdict('firing', 20))

    # when
    windows = Runtime.auto_windows(ctx)
    apply(store, [], NOW, auto_resolve_hours=6, kind_windows=windows)
    cards = by_key(store)

    # then
    assert [w[1] for w in windows] == [DISAPPROVED]
    assert cards[f'{VERDICT}|cleared']['triage'] == 'handled'
    assert cards[f'{VERDICT}|firing'].get('triage') is None


def test_the_builtin_plugins_declare_24_hours_with_an_admin_setting():
    # given

    # when
    declared = {p.id: (p.auto_resolve, [s for s in p.settings if s.key == 'auto-resolve-hours']) for p in (vwo.PLUGIN, google_ads.PLUGIN)}

    # then
    assert declared['vwo'][0] == (AutoResolve('wingify-verdict', VERDICT, 24, 'auto-resolve-hours'),)
    assert declared['google-ads'][0] == (AutoResolve('gads-policy', DISAPPROVED, 24, 'auto-resolve-hours'),)
    for _, [setting] in declared.values():
        assert (setting.kind, setting.default, setting.low, setting.high) == ('int', '24', 0, 720)


def test_runtime_windows_use_the_setting_and_skip_a_misbehaving_declaration():
    # given
    broken = SimpleNamespace(id='broken', auto_resolve=('junk', AutoResolve('x', 'y', True, '')), checks=())
    plain = dataclasses.replace(vwo.PLUGIN, id='vwo')
    ctx = SimpleNamespace(_warn=lambda *a: None, _plugins=lambda: [broken, plain, google_ads.PLUGIN], setting=lambda plugin_id, key: 48 if plugin_id == 'vwo' else 'x')

    # when
    windows = Runtime.auto_windows(ctx)

    # then
    assert [(sorted(names), sig, hours) for names, sig, hours in windows] == [(['wingify-verdict'], VERDICT, 48), (['gads-policy'], DISAPPROVED, 24)]


def test_the_server_passes_the_hours_and_the_slack_note_says_hours(store: Store):
    # given
    seeded(store, waiting(CART, cleared=7 * H))

    # when
    with TestClient(create_app(dataclasses.replace(settings(), auto_resolve_hours_crit=6, auto_resolve_hours_warn=6), store)) as c:
        c.app.state.server_apply([], NOW, None)
    on = store.anomaly(CART)
    seeded(store, waiting(CART, cleared=7 * H))
    with TestClient(create_app(dataclasses.replace(settings(), auto_resolve_hours_crit=0, auto_resolve_hours_high=0, auto_resolve_hours_warn=0), store)) as c:
        c.app.state.server_apply([], NOW, None)

    # then
    assert on['triage'] == 'handled'
    assert store.anomaly(CART).get('triage') is None


def test_a_plugin_that_raises_in_its_setting_does_not_stop_the_others():
    # given
    def setting(plugin_id: str, key: str) -> int:
        if plugin_id == 'vwo':
            raise RuntimeError('boom')
        return 12

    ctx = SimpleNamespace(_warn=lambda *a: None, _plugins=lambda: [vwo.PLUGIN, google_ads.PLUGIN], setting=setting)

    # when
    windows = Runtime.auto_windows(ctx)

    # then
    assert [(w[1], w[2]) for w in windows] == [(VERDICT, 24), (DISAPPROVED, 12)]


def test_the_parent_pass_names_its_own_window_and_note():
    # given
    child = stored('log|kid|ERROR|x', status='cleared', src='logs', clearedAt=ago(H), parent='log|dad|ERROR|x')
    dad = stored('log|dad|ERROR|x', status='cleared', src='logs', clearedAt=ago(H), triage='handled')
    state = {'anomalies': [child, dad]}

    # when
    followed = follow_parents(state, NOW)

    # then
    assert [(r['key'], r['since']) for r in followed] == [('log|kid|ERROR|x', 'parent')]
    assert auto_text(0, 'parent') == '\u2705 Auto-resolved with its parent'


def test_a_stale_hidden_days_override_can_be_deleted_but_never_set(store: Store):
    # given
    from skopax.config import load_settings
    from skopax.tunables import Live, TunableStore
    tunables = TunableStore(store.db)
    tunables.write('core.auto-resolve-days', '2', 'alice@example.com', None)
    live = Live(load_settings(lambda: {}), tunables)
    live.refresh(force=True)
    before = live.current().auto_resolve_hours

    # when
    live.set('core.auto-resolve-days', None, 'alice@example.com')
    after = live.current().auto_resolve_hours

    # then
    assert (before, after) == (48, 6)
    with pytest.raises(KeyError):
        live.set('core.auto-resolve-days', '3', 'alice@example.com')


def test_deleting_the_hidden_days_override_is_allowed(store: Store):
    # given
    from skopax.tunables import TunableStore
    TunableStore(store.db).write('core.auto-resolve-days', '2', 'alice@example.com', None)
    seeded(store)

    # when
    with TestClient(create_app(settings(), store)) as c:
        res = c.delete('/api/admin/settings', params={'key': 'core.auto-resolve-days'}, headers=AUTH)
        hours = c.app.state.runtime.live.current().auto_resolve_hours

    # then
    assert res.status_code == 200
    assert hours == 6


def test_a_vwo_verdict_resolves_at_25_hours_through_the_real_runtime(store: Store):
    # given
    add_instance(store, vwo.PLUGIN, 'main', sealer_key=master_key(store.db, settings()), primary=True, **{'api-token': 'tok'})
    seeded(store, verdict('old', 25), verdict('young', 23))

    # when
    with TestClient(create_app(settings(), store)) as c:
        c.app.state.server_apply([], NOW, None)
    cards = by_key(store)

    # then
    assert cards[f'{VERDICT}|old']['triage'] == 'handled' and cards[f'{VERDICT}|old']['triagedBy'] == 'auto'
    assert cards[f'{VERDICT}|young'].get('triage') is None
