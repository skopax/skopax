import dataclasses

import pytest
from fastapi.testclient import TestClient

from tests.conftest import settings
from tests.test_engine import NOW, ago, item, stored
from tests.test_prometheus import HARD, NOW as PROM_NOW, prom_data
from skopax import mail, severity, slack
from skopax.app import create_app
from skopax.config import SLACK_POST_SEV, load_settings
from skopax.contract import SEVERITIES, alert_sev
from skopax.engine import PassResult, apply, kept_at_warning, merge
from skopax.plugins.grafana.grafana import alert_card
from skopax.plugins.prometheus.engine import evaluate_prom
from skopax.routes import DEFAULTS
from skopax.store import SEVERITY_HIGH_MARK, Store
from skopax.tunables import Live, TunableStore, apply as apply_overrides


def test_the_order_runs_critical_high_warning_info_and_doubling_moves_one_level_up():
    # given
    shuffled = ['info', 'warn', 'crit', 'high']

    # when
    ordered = sorted(shuffled, key=lambda s: -severity.rank(s))

    # then
    assert ordered == ['crit', 'high', 'warn', 'info'] == list(severity.LEVELS) == list(SEVERITIES)
    assert severity.worst(['warn', 'high', 'info']) == 'high' and severity.worst([]) is None
    assert [severity.up(s) for s in ('warn', 'high', 'crit', 'info')] == ['high', 'crit', 'crit', 'info']
    assert severity.NAMES['high'] == 'High'


@pytest.mark.parametrize('label, sev', [('critical', 'crit'), ('warning', 'high'), ('high', 'high'), ('major', 'high'), ('error', 'high'), ('Error', 'high'), ('low', 'warn'), ('minor', 'warn'), ('info', 'info'), ('none', 'info'), (None, 'info')])
def test_an_alert_label_maps_to_a_severity(label, sev):
    # when
    got = alert_sev(label)

    # then
    assert got == sev


def test_prometheus_and_grafana_alerts_with_a_high_label_open_high_cards():
    # given
    alerts = [{'state': 'firing', 'labels': {'alertname': name, 'severity': label, 'namespace': 'shop', 'job': 'checkout-service'}, 'annotations': {}}
              for name, label in (('Down', 'critical'), ('Errors', 'error'), ('Slow', 'major'), ('Disk', 'warning'), ('Note', 'info'))]

    # when
    found = evaluate_prom(PROM_NOW, prom_data(), {k: [] for k in HARD}, alerts, [])[0]
    by_name = {a['sub']: a['sev'] for a in found if a['src'] == 'alert'}
    graf = alert_card('prod', {'alertname': 'Errors', 'severity': 'high', 'job': 'checkout-service'}, None)

    # then
    assert by_name == {'Down': 'crit', 'Errors': 'high', 'Slow': 'high', 'Disk': 'high', 'Note': 'info'}
    assert graf['sev'] == 'high' and graf['short'].startswith('HIGH alert Errors')


def state_of(*anomalies: dict) -> dict:
    return {'initialized': True, 'muted': [], 'history': [], 'anomalies': list(anomalies)}


def test_a_doubling_warning_becomes_high_then_critical_and_keeps_its_level_while_it_lives():
    # given
    state = state_of(stored('log|checkout-service|ERROR|x', src='logs', sev='warn', now=10, pushedAt=ago(600)))
    ran = {'logs': True}

    # when
    first_events, first_push, _ = merge(state, [item('log|checkout-service|ERROR|x', src='logs', sev='warn', now=25)], ran, NOW)
    after_first = dict(state['anomalies'][0])
    quiet_events, quiet_push, _ = merge(state, [item('log|checkout-service|ERROR|x', src='logs', sev='warn', now=30)], ran, NOW + 60)
    after_quiet = dict(state['anomalies'][0])
    second_events, _, _ = merge(state, [item('log|checkout-service|ERROR|x', src='logs', sev='warn', now=60)], ran, NOW + 120)
    after_second = state['anomalies'][0]

    # then
    assert [(e['ev'], e['sev']) for e in first_events] == [('escalated', 'high')] and [a['key'] for a in first_push] == ['log|checkout-service|ERROR|x']
    assert (after_first['sev'], after_first['sevUp'], after_first['pushedSev']) == ('high', 'high', 'high')
    assert (after_quiet['sev'], quiet_events, quiet_push) == ('high', [], []), 'the signal still says warn, the card stays high and says nothing'
    assert [(e['ev'], e['sev']) for e in second_events] == [('escalated', 'crit')] and after_second['sev'] == 'crit'


def test_a_hand_set_severity_is_never_moved_by_doubling():
    # given
    manual = {'sev': 'warn', 'by': 'alice@example.com', 'at': ago(600), 'auto': 'warn'}
    state = state_of(stored('log|checkout-service|ERROR|x', src='logs', sev='warn', now=10, sevSet=manual, pushedAt=ago(600)))

    # when
    events, _, _ = merge(state, [item('log|checkout-service|ERROR|x', src='logs', sev='warn', now=25)], {'logs': True}, NOW)
    card = state['anomalies'][0]

    # then
    assert (card['sev'], card.get('sevUp'), [e['ev'] for e in events]) == ('warn', None, ['escalated'])


def test_a_resolved_card_that_fires_again_starts_from_its_checks_level():
    # given
    state = state_of(stored('log|checkout-service|ERROR|x', src='logs', status='cleared', clearedAt=ago(3 * 3600), sev='high', sevUp='high', triage='handled', triagedBy='alice@example.com',
                            triagedAt=ago(3 * 3600), pushedAt=ago(4 * 3600), pushedSev='high'))

    # when
    merge(state, [item('log|checkout-service|ERROR|x', src='logs', sev='warn', now=1)], {'logs': True}, NOW)
    card = state['anomalies'][0]

    # then
    assert (card['sev'], card.get('sevUp')) == ('warn', None)


def seed(store: Store, *anomalies: dict) -> Store:
    store.save(state_of(*anomalies), None)
    return store


def test_the_upgrade_lifts_live_warning_cards_once_and_skips_hand_set_info_and_critical(store: Store):
    # given
    manual = {'sev': 'warn', 'by': 'alice@example.com', 'at': ago(600), 'auto': 'crit'}
    seed(store, stored('a|shop', sev='warn', pushedSev='warn'), stored('c|shop', sev='warn', muted='noise'),
         stored('d|shop', sev='warn', sevSet=manual), stored('e|shop', sev='info'), stored('f|shop', sev='crit'), stored('g|shop', sev='warn', triage='progress', owner='alice@example.com'))
    store.meta.update_one({'_id': 'current'}, {'$set': {'muted': [{'pattern': 'c|shop', 'until': None, 'reason': 'noise', 'kind': 'mute', 'at': ago(600)}]}})

    # when
    lifted = store.lift_warnings(ago(48 * 3600))
    again = store.lift_warnings(ago(48 * 3600))
    by = {a['key']: a for a in store.load()['anomalies']}

    # then
    assert (lifted, again) == (3, 0)
    assert {k: by[k]['sev'] for k in by} == {'a|shop': 'high', 'c|shop': 'high', 'd|shop': 'warn', 'e|shop': 'info', 'f|shop': 'crit', 'g|shop': 'high'}
    assert {k for k in by if by[k].get('sevBefore') == 'warn'} == {'a|shop', 'c|shop', 'g|shop'}
    assert by['a|shop']['pushedSev'] == 'high' and 'sevBefore' not in by['d|shop']


def test_the_upgrade_leaves_stopped_resolved_and_archived_warning_cards_alone(store: Store):
    # given
    seed(store, stored('b|shop', sev='warn', status='cleared', clearedAt=ago(600)), stored('r|shop', sev='warn', status='cleared', clearedAt=ago(600), triage='handled', triagedAt=ago(600)),
         stored('o|shop', sev='warn', triage='handled', triagedAt=ago(600)), stored('m|shop', sev='warn', muted='noise'))
    store.meta.update_one({'_id': 'current'}, {'$set': {'muted': [{'pattern': 'm|shop', 'until': None, 'reason': 'noise', 'kind': 'mute', 'at': ago(72 * 3600)}]}})

    # when
    lifted = store.lift_warnings(ago(48 * 3600))
    by = {a['key']: a['sev'] for a in store.load()['anomalies']}

    # then
    assert (lifted, by) == (0, {'b|shop': 'warn', 'r|shop': 'warn', 'o|shop': 'warn', 'm|shop': 'warn'})


def test_a_lifted_card_that_still_fires_as_warning_stays_high_and_pushes_nothing(store: Store):
    # given
    seed(store, stored('a|shop', src='cf', sev='warn', now=5))
    store.lift_warnings(ago(48 * 3600))
    state = store.load()

    # when
    events, push, _ = merge(state, [item('a|shop', src='cf', sev='warn', now=6)], {'cf': True}, NOW)

    # then
    assert (state['anomalies'][0]['sev'], events, push) == ('high', [], [])


def test_the_upgrade_runs_once_across_two_app_instances_writes_one_activity_line_and_sends_nothing(store: Store):
    # given
    store.counters.delete_one({'_id': SEVERITY_HIGH_MARK})
    seed(store, stored('a|shop', sev='warn'), stored('b|shop', sev='warn'))

    # when
    with TestClient(create_app(settings(), store)):
        pass
    store.anomalies.update_one({'_id': 'a|shop'}, {'$set': {'sev': 'warn'}, '$unset': {'sevBefore': ''}})
    with TestClient(create_app(settings(), store)):
        pass
    lines = [d['text'] for d in store.activity.find({'text': {'$regex': '^Severity High added'}})]

    # then
    assert lines == ['Severity High added: 2 warning cards became High']
    assert store.anomaly('a|shop')['sev'] == 'warn', 'the second start does not run the upgrade again'
    assert store.notices.count_documents({}) == 0


def test_two_pods_racing_for_the_upgrade_mark_let_only_one_run(store: Store):
    # given
    store.counters.delete_one({'_id': SEVERITY_HIGH_MARK})
    other = Store(store.db)

    # when
    first = store.begin_once(SEVERITY_HIGH_MARK, ago(0))
    second = other.begin_once(SEVERITY_HIGH_MARK, ago(0))

    # then
    assert (first, second) == (True, False)


def test_post_at_once_defaults_to_high_and_an_explicit_warning_is_kept():
    # when
    default = load_settings(lambda: {})
    explicit = load_settings(lambda: {'slack.post-sev': 'warn'})
    chosen = load_settings(lambda: {'slack.post-sev': 'high'})

    # then
    assert SLACK_POST_SEV == 'high' and (default.slack_post_sev, default.slack_post_sev_set) == ('high', False)
    assert (explicit.slack_post_sev, explicit.slack_post_sev_set) == ('warn', True)
    assert chosen.slack_post_sev == 'high'


def test_a_warning_saved_in_admin_before_the_upgrade_stays_warning(store: Store):
    # given
    tunables = TunableStore(store.db)
    tunables.write('slack.post-sev', 'warn', 'admin@example.com', None)

    # when
    live = Live(load_settings(lambda: {}), tunables)

    # then
    assert live.current().slack_post_sev == 'warn'


def test_the_high_window_follows_the_warning_window_until_it_has_its_own():
    # when
    default = load_settings(lambda: {})
    warned = load_settings(lambda: {'core.auto-resolve-hours-warn': '12'})
    legacy = load_settings(lambda: {'core.auto-resolve-hours': '24'})
    own = load_settings(lambda: {'core.auto-resolve-hours-warn': '12', 'core.auto-resolve-hours-high': '3'})
    admin = apply_overrides(load_settings(lambda: {}), {'core.auto-resolve-hours-warn': '9'})

    # then
    assert (default.auto_resolve_hours_crit, default.auto_resolve_hours_high, default.auto_resolve_hours_warn) == (6, 6, 6)
    assert (warned.auto_resolve_hours_high, warned.auto_resolve_hours_warn) == (12, 12)
    assert (legacy.auto_resolve_hours_crit, legacy.auto_resolve_hours_high, legacy.auto_resolve_hours_warn) == (24, 24, 24)
    assert (own.auto_resolve_hours_high, own.auto_resolve_hours_warn, own.auto_resolve_sev_set) == (3, 12, ('warn', 'high'))
    assert (admin.auto_resolve_hours_high, admin.auto_resolve_hours_warn) == (9, 9)


def test_routes_default_to_high_and_the_route_plugins_offer_it():
    # given
    from skopax.plugins.email_route import PLUGIN as EMAIL
    from skopax.plugins.teams import PLUGIN as TEAMS

    # when
    offered = {p.id: ({s.key: s for s in p.settings}['min-sev'], {f.key: f for f in p.fields}['min-sev']) for p in (EMAIL, TEAMS)}

    # then
    assert DEFAULTS['min-sev'] == 'high'
    for setting, field in offered.values():
        assert setting.default == 'high' and 'high' in setting.choices and field.choice_labels['high'] == 'High'


def test_every_outbound_mark_has_a_colour_per_level():
    # when
    html = {sev: mail.card_html('t', 'h', sev, sev, 's', [], None, 'f', None) for sev in ('crit', 'high', 'warn', 'info')}

    # then
    assert (slack.EMOJI['crit'], slack.EMOJI['high'], slack.EMOJI['warn'], slack.EMOJI['info']) == (':red_circle:', ':large_orange_circle:', ':large_yellow_circle:', ':large_blue_circle:')
    assert mail.SEV_COLOURS['high'] in html['high'] and mail.SEV_COLOURS['warn'] in html['warn'] and 'color:#0f1720' in html['warn']
    assert len(set(mail.SEV_COLOURS.values())) == 4


def test_a_card_whose_value_is_its_age_is_pushed_on_doubling_but_keeps_its_level():
    # given
    state = state_of(stored('argo|shop/checkout-service', src='argo', sev='warn', now=600, unit='dur', pushedAt=ago(600)))

    # when
    events, _, _ = merge(state, [item('argo|shop/checkout-service', src='argo', sev='warn', now=1800, unit='dur')], {'argo': True}, NOW)
    card = state['anomalies'][0]

    # then
    assert [e['ev'] for e in events] == ['escalated'] and (card['sev'], card.get('sevUp')) == ('warn', None)


def alert(name: str, label: str) -> dict:
    sev = alert_sev(label)
    return {**item(f'alert|{name}|cluster=prod', src='alert', sev=sev), 'sig': 'alert', 'sub': name, 'short': f'{sev.upper()} alert {name}', 'alertName': name, 'alertLabel': label}


def test_a_listed_warning_alert_stays_warning_and_says_why_while_critical_and_unlisted_ones_do_not():
    # given
    found = [alert('CephPoolGrowthForecast', 'warning'), alert('DiskForecastFull', 'warning'), alert('KubePodNotReady', 'warning'), alert('NodeDown', 'critical'), alert('ErrorBurst', 'error')]

    # when
    kept = {x['alertName']: x for x in kept_at_warning(found, ('CephPoolGrowthForecast', '*Forecast*', 'NodeDown', 'ErrorBurst'))}
    none = kept_at_warning([alert('DiskForecastFull', 'warning')], ())

    # then
    assert {k: v['sev'] for k, v in kept.items()} == {'CephPoolGrowthForecast': 'warn', 'DiskForecastFull': 'warn', 'KubePodNotReady': 'high', 'NodeDown': 'crit', 'ErrorBurst': 'high'}
    assert kept['DiskForecastFull']['what'].endswith('Warning: listed in Alerts kept at Warning.') and kept['DiskForecastFull']['short'] == 'WARN alert DiskForecastFull'
    assert 'keptWarning' not in kept['KubePodNotReady'] and none[0]['sev'] == 'high'


def test_the_kept_at_warning_list_is_read_validated_and_capped():
    # when
    loaded = load_settings(lambda: {'alerts.warn': '*Forecast*, CephPoolGrowthForecast, not an alert'})
    many = load_settings(lambda: {'alerts.warn': ','.join(f'A{n}' for n in range(150))})

    # then
    assert loaded.alerts_warn == ('*Forecast*', 'CephPoolGrowthForecast')
    assert len(many.alerts_warn) == 100 and load_settings(lambda: {}).alerts_warn == ()


def test_admin_refuses_a_bad_alert_name_and_more_than_a_hundred(store: Store):
    # given
    live = Live(load_settings(lambda: {}), TunableStore(store.db))

    # when
    saved, _ = live.set('alerts.warn', '*Forecast*, CephPoolGrowthForecast', 'admin@example.com')
    with pytest.raises(Exception) as bad:
        live.set('alerts.warn', 'Disk Full', 'admin@example.com')
    with pytest.raises(Exception) as many:
        live.set('alerts.warn', ','.join(f'A{n}' for n in range(101)), 'admin@example.com')

    # then
    assert live.current().alerts_warn == ('*Forecast*', 'CephPoolGrowthForecast')
    assert 'Alerts kept at Warning' in str(bad.value) and 'at most 100' in str(many.value)


def test_the_server_pass_applies_the_kept_at_warning_list(store: Store):
    # given
    store.save(state_of(), None)
    found = [alert('DiskForecastFull', 'warning'), alert('KubePodNotReady', 'warning')]

    # when
    apply(store, [PassResult('alert', True, 'ok', found, {}, ('alert',))], NOW, alerts_warn=('*Forecast*',))
    by = {a['key']: a['sev'] for a in store.load()['anomalies']}

    # then
    assert by == {'alert|DiskForecastFull|cluster=prod': 'warn', 'alert|KubePodNotReady|cluster=prod': 'high'}


def test_a_pass_keeps_open_cards_worst_first_with_high_between_critical_and_warning():
    # given
    state = state_of(stored('a|shop', sev='warn'), stored('b|shop', sev='high'), stored('c|shop', sev='crit'))

    # when
    merge(state, [item('a|shop', sev='warn'), item('b|shop', sev='high'), item('c|shop', sev='crit')], {'cf': True}, NOW)

    # then
    assert [a['key'] for a in state['anomalies']] == ['c|shop', 'b|shop', 'a|shop']


def test_a_migrated_alert_listed_as_kept_at_warning_reads_warning(store: Store):
    # given
    seed(store, {**stored('alert|DiskForecastFull|cluster=prod', src='alert', sev='warn'), 'sig': 'alert'})
    store.lift_warnings(ago(48 * 3600))

    # when
    applied = apply(store, [PassResult('alert', True, 'ok', [alert('DiskForecastFull', 'warning')], {}, ('alert',))], NOW, alerts_warn=('*Forecast*',))
    card = store.anomaly('alert|DiskForecastFull|cluster=prod')

    # then
    assert (card['sev'], card['keptWarning'], [e['ev'] for e in applied.events]) == ('warn', True, [])


def test_the_upgrade_leaves_history_rows_alone(store: Store):
    # given
    store.card_history.insert_one({'_id': 'h1', 'key': 'a|shop', 'sev': 'warn'})
    seed(store, stored('a|shop', sev='warn'))

    # when
    store.lift_warnings(ago(48 * 3600))

    # then
    assert store.card_history.find_one({'_id': 'h1'})['sev'] == 'warn' and store.anomaly('a|shop')['sev'] == 'high'


def test_real_alert_cards_feed_the_kept_at_warning_list():
    # given
    labels = {'alertname': 'CephPoolGrowthForecast', 'severity': 'warning', 'namespace': 'shop', 'job': 'ceph'}
    prom = [a for a in evaluate_prom(PROM_NOW, prom_data(), {k: [] for k in HARD}, [{'state': 'firing', 'labels': labels, 'annotations': {}}], [])[0] if a['src'] == 'alert']
    graf = [alert_card('prod', dict(labels), None)]

    # when
    kept = kept_at_warning(prom + graf, ('*Forecast',))

    # then
    assert [(x['src'], x['sev'], x['short'].split()[0]) for x in kept] == [('alert', 'warn', 'WARN'), ('grafana', 'warn', 'WARN')]


def test_the_user_impact_findings_of_prometheus_are_high_and_the_rest_keep_their_level():
    # given
    from skopax.plugins.prometheus import engine as pm
    data = prom_data()
    data[('req', 0)], data[('req', 1)], data[('req', 2)] = {('shop', 'checkout-service'): 100}, {('shop', 'checkout-service'): 5000}, {('shop', 'checkout-service'): 5000}

    # when
    levels = {s['sig']: s['sev'] for s in pm.RATES}
    found = evaluate_prom(PROM_NOW, data, {k: [] for k in HARD}, [], [])[0]

    # then
    assert levels == {'err': 'high', 'warn': 'info', '5xx': 'high', 'out-err': 'warn', 'gw-err': 'high', 'ing-5xx': 'high', 'hikari-tmo': 'warn'}
    assert ('traffic-drop', 'high') in {(a['sig'], a['sev']) for a in found if a['svc'] == 'checkout-service'}


def test_a_high_sibling_stays_out_of_a_handled_host_like_a_critical_one():
    # given
    from skopax.engine import group_siblings
    es = 'pvc-disk|cluster-logging/elasticsearch-data-elasticsearch-logging-es-elasticsearch-logging-{}|disk'
    pvc = 'elasticsearch-data-elasticsearch-logging-es-elasticsearch-logging-{}'
    st = {'anomalies': [
        {'key': es.format(0), 'svc': pvc.format(0), 'src': 'capacity', 'ns': 'cluster-logging', 'sev': 'crit', 'status': 'open', 'firstSeen': '2026-11-07T17:00:00Z', 'triage': 'handled'},
        {'key': es.format(1), 'svc': pvc.format(1), 'src': 'capacity', 'ns': 'cluster-logging', 'sev': 'high', 'status': 'open', 'firstSeen': '2026-11-07T18:00:00Z'},
        {'key': es.format(2), 'svc': pvc.format(2), 'src': 'capacity', 'ns': 'cluster-logging', 'sev': 'warn', 'status': 'open', 'firstSeen': '2026-11-07T19:00:00Z'},
    ]}

    # when
    grouped = group_siblings(st)

    # then
    assert grouped == [(es.format(2), es.format(0))]


def test_a_kept_at_warning_alert_stays_warning_when_its_value_doubles():
    # given
    state = state_of({**stored('alert|DiskForecastFull|cluster=prod', src='alert', sev='warn', now=10, pushedAt=ago(600)), 'sig': 'alert'})
    found = [{**alert('DiskForecastFull', 'warning'), 'now': 25}]

    # when
    merge(state, kept_at_warning(found, ('*Forecast*',)), {'alert': True}, NOW)
    card = state['anomalies'][0]

    # then
    assert (card['sev'], card.get('sevUp')) == ('warn', None)
