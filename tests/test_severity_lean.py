import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tests.conftest import settings
from tests.test_engine import NOW, ago, item, stored
from tests.test_prometheus import HARD, NOW as PROM_NOW, prom_data
from skopax import mail, rescore, severity, slack
from skopax.app import create_app
from skopax.cardposts import drawn_sig
from skopax.engine import PassResult, apply, merge
from skopax.plugins.elasticsearch import checks
from skopax.plugins.prometheus import engine as pm
from skopax.plugins.prometheus.engine import evaluate_prom
from skopax.store import SEVERITY_RESCORE_MARK, Store
from skopax.tunables import TunableStore


def warn_row(now: int, d1: int, d7: int) -> dict:
    return {'app': 'auth-service', 'level': 'WARN', 'msg': 'retrying role lookup', 'now': now, 'd1': d1, 'd7': d7}


@pytest.mark.parametrize('now, d1, d7, sev', [(640, 0, 0, 'high'), (100, 0, 0, 'high'), (99, 0, 0, 'warn'), (60, 0, 0, 'warn'), (72, 4, 6, 'high'), (50, 5, 5, 'high'),
                                              (50, 5, 6, 'high'), (50, 6, 6, 'warn'), (49, 1, 1, 'info'), (95, 9, 14, 'high'), (120, 12, 1, 'high'), (119, 12, 13, 'warn'), (50, 0, 5, 'high'), (60, 7, 0, 'warn'),
                                              (500, 10, 120, 'high'), (60, 5, 50, 'high')])
def test_a_warn_log_pattern_is_high_when_new_and_loud_or_ten_times_either_baseline(now, d1, d7, sev):
    # when
    found, _ = checks.evaluate_logs([warn_row(now, d1, d7)])

    # then
    assert [x['sev'] for x in found] == [sev]
    assert found[0]['short'].startswith(sev.upper() + ' ')


def test_ten_times_one_baseline_is_high_and_one_short_baseline_with_the_other_missing_stays_warning():
    # when
    either, _ = checks.evaluate_logs([warn_row(95, 9, 14)])
    missing, _ = checks.evaluate_logs([{'app': 'auth-service', 'level': 'WARN', 'msg': 'retrying role lookup', 'now': 60, 'd1': 7}])
    stored_missing = rescore.current(card('log|auth-service|WARN|x', 'logs', sig='log', now=60, d1=7, d7=None))

    # then
    assert [x['sev'] for x in either] == ['high']
    assert [x['sev'] for x in missing] == ['warn'] and stored_missing == 'warn'


def test_the_log_query_lets_a_warn_row_through_at_ten_times_either_baseline():
    # when
    query = checks.logs_query({})

    # then
    assert '(level == "WARN" AND now >= 50 AND ((d1 > 0 AND now >= 10 * d1) OR (d7 > 0 AND now >= 10 * d7)))' in query


def test_three_times_the_baseline_is_high_exactly_at_the_boundary():
    # when
    small = severity.latency(0.027 * 1000, 0.009 * 1000, 500)
    edge = severity.latency(1.005 * 1000, 0.335 * 1000, 500)

    # then
    assert (small, edge) == ('high', 'high')


def test_a_fractional_request_count_is_rounded_once_so_the_live_rule_and_the_rescore_agree():
    # when
    found = [a for a in evaluate_prom(PROM_NOW, latency_data('avg-latency', 0.9, 0.3, 499.6), {k: [] for k in HARD}, [], [])[0] if a['sig'] == 'avg-latency']
    card_only_text = {k: v for k, v in found[0].items() if k != 'requests'}

    # then
    assert (found[0]['sev'], found[0]['requests'], rescore.current(found[0]), rescore.current(card_only_text)) == ('high', 500, 'high', 'high')
    assert 'over 500 requests' in found[0]['what']


def test_the_lean_thresholds_live_in_one_place():
    # then
    assert (severity.WARN_LOG_NEW_MIN, severity.WARN_LOG_GROWTH_TIMES, severity.WARN_LOG_GROWTH_MIN, severity.LATENCY_TIMES, severity.LATENCY_MIN_REQUESTS) == (100, 10, 50, 3, 500)


def latency_data(sig: str, now_s: float, base_s: float, n: int) -> dict:
    data = prom_data()
    for s in pm.LATENCY:
        for i in range(3):
            data[(s['sig'], i)], data[(s['sig'] + '#n', i)] = {}, {}
    key = ('billing', 'payments-api', '') if sig == 'avg-latency' else ('edge', 'api-gateway', '/v1/pay') if sig == 'p95-route' else ('storefront', 'ingress/shop-web', '')
    for i, v in enumerate((now_s, base_s, base_s)):
        data[(sig, i)][key] = v
        data[(sig + '#n', i)][key] = n
    return data


@pytest.mark.parametrize('sig', ['avg-latency', 'p95-route', 'p95-ingress'])
@pytest.mark.parametrize('now_s, base_s, n, sev', [(0.9, 0.3, 2400, 'high'), (1.005, 0.335, 500, 'high'), (0.6, 0.2, 500, 'high'), (0.6, 0.2, 499, 'warn'), (0.59, 0.2, 5000, 'warn'), (0.9, 0.1, 300, 'warn')])
def test_a_latency_card_is_high_at_three_times_the_baseline_over_five_hundred_requests(sig, now_s, base_s, n, sev):
    # when
    found = [a for a in evaluate_prom(PROM_NOW, latency_data(sig, now_s, base_s, n), {k: [] for k in HARD}, [], [])[0] if a['sig'] == sig]

    # then
    assert [(a['sev'], a['requests']) for a in found] == [(sev, n)]


def card(key: str, src: str, **extra) -> dict:
    return {**stored(key, src=src, sev='warn'), **extra}


STOPPED = {'status': 'cleared', 'clearedAt': ago(600)}
LIFTED = [
    card('err|shop/checkout-service', 'prom', now=24, d1=0, d7=0, **STOPPED),
    card('alert|ExamplePodKilled|namespace=shop', 'alert', sig='alert', alertName='ExamplePodKilled', alertLabel='warning', **STOPPED),
    card('alert|ExampleTrafficAnomaly|cluster=prod', 'grafana', sig='alert', **STOPPED),
    card('lf-traffic|ai/assistant', 'langfuse', now=20, d1=150, d7=140, **STOPPED),
    card('log|auth-service|WARN|retrying role lookup', 'logs', sig='log', now=640, d1=0, d7=0),
    card('log|auth-service|WARN|token refresh', 'logs', sig='log', now=72, d1=4, d7=6),
    card('log|auth-service|WARN|one baseline', 'logs', sig='log', now=95, d1=9, d7=14),
    card('avg-latency|billing/payments-api', 'prom', unit='ms', now=210, d1=60, d7=55, what='average response time 210 ms over 2400 requests in the last 60 min, the baseline is 60 ms'),
    card('p95-route|edge/api-gateway|/v1/pay', 'prom', unit='ms', now=900, d1=200, d7=150, requests=800),
    card('traffic-drop|shop/checkout-service', 'prom', now=10, d1=5000, d7=4800, pushedSev='warn'),
    card('page-typos|-/https://example.com/gone', 'pagescan', gone={'status': 404, 'since': ago(600)}),
]
KEPT = [
    card('log|auth-service|WARN|growing', 'logs', sig='log', now=60, d1=7, d7=None),
    card('avg-latency|billing/orders-api', 'prom', unit='ms', now=210, d1=60, d7=55, what='average response time 210 ms over 450 requests in the last 60 min, the baseline is 60 ms'),
    card('out-err|shop/checkout-service', 'prom', now=40),
    card('alert|DiskForecastFull|cluster=prod', 'alert', sig='alert', alertName='DiskForecastFull', alertLabel='warning', keptWarning=True),
    card('alert|CephPoolGrowthForecast|cluster=prod', 'alert', sig='alert', alertName='CephPoolGrowthForecast', alertLabel='warning'),
    card('alert|DiskSlow|cluster=prod', 'alert', sig='alert', alertName='DiskSlow', alertLabel='minor'),
    card('err|shop/resolved', 'prom', triage='handled', triagedAt=ago(600), **STOPPED),
    card('err|shop/auto-resolved', 'prom', autoResolvedAt=ago(600), **STOPPED),
    card('err|shop/in-progress', 'prom', triage='progress', owner='alice@example.com', **STOPPED),
    card('err|shop/muted-stopped', 'prom', muted='noise', **STOPPED),
    card('err|shop/archived', 'prom', muted='noise'),
    card('err|shop/hand-set', 'prom', sevSet={'sev': 'warn', 'by': 'alice@example.com', 'at': ago(600), 'auto': 'warn'}),
    card('page-typos|-/https://example.com/live', 'pagescan'),
]


def seed(store: Store, *anomalies: dict) -> Store:
    store.save({'initialized': True, 'muted': [], 'history': [], 'anomalies': list(anomalies)}, None)
    store.meta.update_one({'_id': 'current'}, {'$set': {'muted': [{'pattern': 'err|shop/archived', 'until': None, 'reason': 'noise', 'kind': 'mute', 'at': ago(72 * 3600)},
                                                                  {'pattern': 'err|shop/muted-stopped', 'until': None, 'reason': 'noise', 'kind': 'mute', 'at': ago(600)}]}})
    return store


def test_the_rescore_lifts_open_and_waiting_cards_the_current_rules_make_high_and_leaves_the_rest(store: Store):
    # given
    seed(store, *LIFTED, *KEPT)

    # when
    lifted = store.rescore_warnings(ago(48 * 3600), ('*Forecast',))
    again = store.rescore_warnings(ago(48 * 3600), ('*Forecast',))
    by = {a['key']: a for a in store.load()['anomalies']}

    # then
    assert (lifted, again) == (len(LIFTED), 0)
    assert {a['key'] for a in LIFTED} == {k for k, a in by.items() if a['sev'] == 'high'}
    assert all(by[a['key']]['sev'] == 'warn' and 'sevBefore' not in by[a['key']] for a in KEPT)
    assert all((by[a['key']]['sevBefore'], by[a['key']]['sevUp']) == ('warn', 'high') for a in LIFTED)
    assert by['traffic-drop|shop/checkout-service']['pushedSev'] == 'high'
    assert by['err|shop/checkout-service']['status'] == 'cleared'
    assert by['err|shop/checkout-service']['short'] == 'HIGH err|shop/checkout-service' and by['out-err|shop/checkout-service']['short'] == 'WARN out-err|shop/checkout-service'


def test_the_rescore_never_lowers_a_card_and_keeps_an_earlier_sev_before(store: Store):
    # given
    seed(store, card('alert|Note|cluster=prod', 'alert', sig='alert', alertLabel='info'), {**stored('err|shop/x', src='prom', sev='crit')},
         card('err|shop/y', 'prom', sevBefore='info'))

    # when
    lifted = store.rescore_warnings(ago(48 * 3600))
    by = {a['key']: a for a in store.load()['anomalies']}

    # then
    assert lifted == 1
    assert (by['alert|Note|cluster=prod']['sev'], by['err|shop/x']['sev'], by['err|shop/y']['sev'], by['err|shop/y']['sevBefore']) == ('warn', 'crit', 'high', 'info')


def test_a_rescored_card_that_fires_on_pushes_nothing_and_its_slack_post_is_redrawn(store: Store):
    # given
    seed(store, card('log|auth-service|WARN|retrying role lookup', 'logs', sig='log', now=640, d1=0, d7=0, peak=640, pushedPeak=640))
    before = drawn_sig(store.load()['anomalies'][0], None)
    store.rescore_warnings(ago(48 * 3600))
    state = store.load()
    after = drawn_sig(state['anomalies'][0], None)

    # when
    events, push, _ = merge(state, [item('log|auth-service|WARN|retrying role lookup', src='logs', sev='high', sig='log', now=920, d1=0, d7=0)], {'logs': True}, NOW)

    # then
    assert (state['anomalies'][0]['sev'], events, push) == ('high', [], [])
    assert before != after


def test_the_rescore_runs_once_at_start_writes_one_activity_line_and_sends_nothing(store: Store):
    # given
    store.counters.delete_one({'_id': SEVERITY_RESCORE_MARK})
    seed(store, *LIFTED[:2], KEPT[0])

    # when
    with TestClient(create_app(settings(), store)):
        pass
    store.anomalies.update_one({'_id': LIFTED[0]['key']}, {'$set': {'sev': 'warn'}, '$unset': {'sevBefore': ''}})
    with TestClient(create_app(settings(), store)):
        pass
    lines = [d['text'] for d in store.activity.find({'text': {'$regex': '^Severity High rules'}})]

    # then
    assert lines == ['Severity High rules: 2 warning cards became High']
    assert store.anomaly(LIFTED[0]['key'])['sev'] == 'warn', 'the second start does not run it again'
    assert store.notices.count_documents({}) == 0 and store.anomaly(LIFTED[1]['key'])['sev'] == 'high'


def test_a_start_with_nothing_to_rescore_writes_no_activity_line(store: Store):
    # given
    store.counters.delete_one({'_id': SEVERITY_RESCORE_MARK})
    seed(store, KEPT[0])

    # when
    with TestClient(create_app(settings(), store)):
        pass

    # then
    assert store.activity.count_documents({'text': {'$regex': '^Severity High rules'}}) == 0
    assert store.counters.find_one({'_id': SEVERITY_RESCORE_MARK})['done'] is True


def test_two_pods_racing_for_the_rescore_mark_let_only_one_run(store: Store):
    # given
    store.counters.delete_one({'_id': SEVERITY_RESCORE_MARK})
    other = Store(store.db)

    # when
    first = store.begin_once(SEVERITY_RESCORE_MARK, ago(0))
    second = other.begin_once(SEVERITY_RESCORE_MARK, ago(0))

    # then
    assert (first, second) == (True, False)


def test_an_alert_without_a_stored_label_reads_as_a_warning_alert_and_honours_the_kept_list():
    # given
    old = card('alert|KubeHpaMaxedOut|cluster=prod', 'alert', sig='alert', sub='KubeHpaMaxedOut')

    # when
    plain = rescore.current(old)
    kept = rescore.current(old, ('KubeHpa*',))
    by_key = rescore.current({**old, 'sub': ''}, ('KubeHpa*',))

    # then
    assert (plain, kept, by_key) == ('high', 'warn', 'warn')


def luminance(hex_colour: str) -> float:
    c = [int(hex_colour[i:i + 2], 16) / 255 for i in (1, 3, 5)]
    c = [v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4 for v in c]
    return 0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2]


def ratio(a: str, b: str) -> float:
    x, y = luminance(a), luminance(b)
    return (max(x, y) + 0.05) / (min(x, y) + 0.05)


def test_the_card_page_mail_slack_and_favicon_share_the_new_orange():
    # given
    root = Path(__file__).resolve().parent.parent / 'skopax'
    card_page, page = (root / 'web' / 'card.html').read_text(), (root / 'web' / 'template.html').read_text()
    overview = (root / 'web' / 'static' / 'overview.js').read_text()

    # when
    tokens = re.findall(r'--high: (#[0-9a-f]{6}); --high-bg: (#[0-9a-f]{6});', card_page)
    page_tokens = re.findall(r'--high: (#[0-9a-f]{6}); --high-bg: (#[0-9a-f]{6});', page)

    # then
    assert tokens == page_tokens[:3] == [('#b54400', '#fff0e3'), ('#ff7a2e', '#3d2210'), ('#ff7a2e', '#3d2210')]
    assert mail.SEV_COLOURS['high'] == '#b54400' and ratio('#ffffff', mail.SEV_COLOURS['high']) >= 4.5
    assert slack.COLOR['high'] == '#f97316' and "high: '#f97316'" in overview
    assert ratio(mail.SEV_COLOURS['high'], '#fff0e3') >= 4.5 and ratio('#ff7a2e', '#3d2210') >= 4.5


def test_cards_that_lived_through_passes_and_stopped_are_rescored_and_lifted(store: Store):
    # given
    seed(store, card('err|shop/checkout-service', 'prom', now=24, d1=0, d7=0, peak=24, pushedPeak=24))
    found = [{**item('err|shop/checkout-service', src='prom', sev='warn', now=24), 'sig': 'err'}]
    apply(store, [PassResult('prom', True, 'ok', found, {}, ('prom',))], NOW)
    apply(store, [PassResult('prom', True, 'ok', found, {}, ('prom',))], NOW + 60)
    apply(store, [PassResult('prom', True, 'ok', [], {}, ('prom',))], NOW + 120)
    lived = store.anomalies.find_one({'_id': 'err|shop/checkout-service'})
    store.anomalies.update_one({'_id': 'err|shop/checkout-service'}, {'$set': {'sev': 'warn'}})
    seed_open = {**stored('5xx|shop/orders', src='prom', sev='warn'), 'sevSet': None, 'sevBefore': None}
    store.anomalies.insert_one({**seed_open, '_id': seed_open['key']})

    # when
    lifted = store.rescore_warnings(ago(48 * 3600))
    upgraded = store.lift_warnings(ago(48 * 3600))

    # then
    assert lived['status'] == 'cleared'
    assert lifted == 2 and store.anomaly('err|shop/checkout-service')['sev'] == 'high' and store.anomaly('5xx|shop/orders')['sevBefore'] == 'warn'
    assert upgraded == 0


def test_the_0_5_8_lift_also_takes_cards_whose_hand_set_and_sev_before_fields_are_empty(store: Store):
    # given
    seed_open = {**stored('5xx|shop/orders', src='prom', sev='warn'), 'sevSet': None, 'sevBefore': None}
    store.anomalies.insert_one({**seed_open, '_id': seed_open['key']})

    # when
    lifted = store.lift_warnings(ago(48 * 3600))

    # then
    assert lifted == 1 and store.anomaly('5xx|shop/orders')['sevBefore'] == 'warn'


def test_the_start_rescore_honours_the_kept_at_warning_list_saved_in_admin(store: Store):
    # given
    store.counters.delete_one({'_id': SEVERITY_RESCORE_MARK})
    TunableStore(store.db).write('alerts.warn', 'DiskForecast*', 'admin@example.com', None)
    seed(store, card('alert|DiskForecastFull|cluster=prod', 'alert', sig='alert', alertName='DiskForecastFull', alertLabel='warning', **STOPPED),
         card('alert|KubePodNotReady|cluster=prod', 'alert', sig='alert', alertName='KubePodNotReady', alertLabel='warning', **STOPPED))

    # when
    with TestClient(create_app(settings(), store)):
        pass

    # then
    assert (store.anomaly('alert|DiskForecastFull|cluster=prod')['sev'], store.anomaly('alert|KubePodNotReady|cluster=prod')['sev']) == ('warn', 'high')
