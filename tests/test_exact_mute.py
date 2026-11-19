import copy

from skopax import actions
from skopax.engine import PassResult, anomaly, apply
from skopax.store import Store

NOW = 1_792_224_000.0


def log_card(cron: str) -> dict:
    return anomaly('logs', 'log', 'batch', 'batch-service', f'ERROR|Job failed for cron {cron} in group DEFAULT', 'warn', 12, None, None, '', 'ERROR pattern', 'x')


EVERY_5 = log_card('0 0/5 * * * ?')
OFFICE = log_card('0 0/5 9-18 ? * MON-FRI ?')
NIGHT = log_card('0 0 2 * * ?')


def passed(store: Store, *found: dict, at: float = NOW) -> dict:
    apply(store, [PassResult('logs', True, 'ok', [copy.deepcopy(f) for f in found])], at)
    return {a['key']: a for a in store.load()['anomalies']}


def test_known_noise_on_a_card_whose_key_holds_a_star_mutes_that_card_only_now_and_later(store: Store):
    # given
    store.save({'initialized': True, 'muted': [], 'anomalies': []}, None)
    passed(store, EVERY_5, OFFICE, at=NOW - 600)

    # when
    actions.apply(store, {'type': 'known', 'key': EVERY_5['key'], 'requestedBy': 'nina.white@example.com'})
    right_after = {a['key']: a for a in store.load()['anomalies']}
    later = passed(store, EVERY_5, OFFICE, NIGHT)

    # then
    assert right_after[EVERY_5['key']]['muted'] and not right_after[OFFICE['key']]['muted']
    assert later[EVERY_5['key']]['muted'] and not later[OFFICE['key']]['muted'] and not later[NIGHT['key']]['muted']
    assert store.load()['muted'][0] | {'at': None} == {'pattern': EVERY_5['key'], 'until': None, 'reason': 'marked as known noise on the page', 'kind': 'known', 'at': None, 'exact': True, 'preset': True}


def test_a_star_pattern_that_names_no_card_still_mutes_every_card_it_fits_and_unmute_lifts_each_kind(store: Store):
    # given
    store.save({'initialized': True, 'muted': [], 'anomalies': []}, None)
    passed(store, EVERY_5, OFFICE, at=NOW - 600)
    actions.apply(store, {'type': 'known', 'key': EVERY_5['key']})
    actions.apply(store, {'type': 'mute', 'key': 'log|batch/batch-service|ERROR|*', 'hours': 2, 'requestedBy': 'paul.stone@example.com'})

    # when
    both = passed(store, EVERY_5, OFFICE, NIGHT, at=NOW + 60)
    actions.apply(store, {'type': 'unmute', 'key': EVERY_5['key']})
    pattern_left = passed(store, EVERY_5, OFFICE, at=NOW + 120)
    kept = [m['pattern'] for m in store.load()['muted']]
    actions.apply(store, {'type': 'unmute', 'key': 'log|batch/batch-service|ERROR|*'})
    none_left = passed(store, EVERY_5, OFFICE, at=NOW + 180)

    # then
    assert all(both[k]['muted'] for k in (EVERY_5['key'], OFFICE['key'], NIGHT['key']))
    assert kept == ['log|batch/batch-service|ERROR|*']
    assert all(pattern_left[k]['muted'] for k in (EVERY_5['key'], OFFICE['key'])), 'the pattern mute still holds both'
    assert not any(none_left[k]['muted'] for k in (EVERY_5['key'], OFFICE['key'])) and store.load()['muted'] == []
