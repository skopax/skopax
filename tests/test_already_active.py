from skopax import engine
from skopax.app import card_view
from skopax.autosend import Fallback
from skopax.engine import ALREADY_ACTIVE, CONNECT_GRACE_S, iso, merge, read_any, started_before
from skopax.investigator import select
from skopax.contract import Check, PassResult, Sweep
from skopax.routes import Route
from skopax.sweeps import Sweeper
from tests.teams_kit import AT, open_card as route_card, tag

NOW = 1_794_240_000.0
OLD = iso(NOW - 40 * 86400)
MARK = {'since': OLD, 'source': 'Zabbix'}


def found(key: str, sev: str = 'high', **extra) -> dict:
    card = engine.anomaly('zabbix', 'zabbix', '-', 'web-01', key, sev, 1, None, None, '', key, f'Zabbix problem {key}.')
    return card | {'key': f'zabbix|web-01|{key}', 'ranKey': 'zabbix'} | extra


def first_state() -> dict:
    return {'anomalies': [], 'history': [], 'initialized': True}


def test_a_new_card_marked_already_active_opens_without_a_push_and_keeps_its_mark_on_later_passes():
    # given
    state = first_state()

    # when
    events, push, _ = merge(state, [found('cpu', **{ALREADY_ACTIVE: MARK}), found('disk')], {'zabbix': True}, NOW)
    again_events, again_push, _ = merge(state, [found('cpu'), found('disk')], {'zabbix': True}, NOW + 900)
    cards = {a['key']: a for a in state['anomalies']}

    # then
    assert [e['ev'] for e in events] == ['new', 'new']
    assert [a['key'] for a in push] == ['zabbix|web-01|disk']
    assert again_events == [] and again_push == []
    assert cards['zabbix|web-01|cpu'][ALREADY_ACTIVE] == MARK and cards['zabbix|web-01|cpu']['pushedSev'] == 'high' and cards['zabbix|web-01|cpu']['pushedAt'] is None
    assert ALREADY_ACTIVE not in cards['zabbix|web-01|disk']


def test_the_mark_is_honoured_only_on_a_new_card():
    # given
    state = first_state()
    merge(state, [found('cpu')], {'zabbix': True}, NOW)

    # when
    _, push, _ = merge(state, [found('cpu', **{ALREADY_ACTIVE: MARK})], {'zabbix': True}, NOW + 900)

    # then
    assert push == [] and ALREADY_ACTIVE not in state['anomalies'][0]


def test_an_escalation_is_pushed_and_drops_the_mark():
    # given
    state = first_state()
    merge(state, [found('cpu', 'warn', **{ALREADY_ACTIVE: MARK})], {'zabbix': True}, NOW)

    # when
    events, push, _ = merge(state, [found('cpu', 'crit')], {'zabbix': True}, NOW + 900)

    # then
    assert [e['ev'] for e in events] == ['escalated']
    assert [a['key'] for a in push] == ['zabbix|web-01|cpu']
    assert ALREADY_ACTIVE not in state['anomalies'][0]


def test_a_reopen_is_pushed_and_carries_no_mark_even_if_one_is_given():
    # given
    state = first_state()
    merge(state, [found('cpu', **{ALREADY_ACTIVE: MARK})], {'zabbix': True}, NOW)
    merge(state, [], {'zabbix': True}, NOW + 900)

    # when
    events, push, _ = merge(state, [found('cpu', **{ALREADY_ACTIVE: MARK})], {'zabbix': True}, NOW + 4 * 3600)

    # then
    assert [e['ev'] for e in events] == ['reopened']
    assert [a['key'] for a in push] == ['zabbix|web-01|cpu']
    assert ALREADY_ACTIVE not in state['anomalies'][0]


def test_a_start_counts_as_before_only_past_the_grace_and_a_bad_one_never_does():
    # given
    stamps = {'old': iso(NOW - CONNECT_GRACE_S - 1), 'grace': iso(NOW - CONNECT_GRACE_S + 60), 'after': iso(NOW + 60), 'bad': 'yesterday', 'none': None, 'number': NOW - 86400}

    # when
    got = {name: started_before({'startedAt': at}, NOW) for name, at in stamps.items()}

    # then
    assert got == {'old': stamps['old'], 'grace': None, 'after': None, 'bad': None, 'none': None, 'number': None}


def test_a_pass_connects_once_it_read_anything_and_a_refused_or_failed_one_does_not():
    # given
    passes = {'read': PassResult('zabbix', True, 'ok', [], {}, ('zabbix', 'zabbix:access:problems')), 'capped': PassResult('zabbix', True, 'ok', [], {}, ('zabbix:no-data', 'zabbix:access:problems')),
              'refused': PassResult('zabbix', True, 'ok', [], {}, ('zabbix:access:problems',)), 'failed': PassResult('zabbix', False, 'error: x'), 'core': PassResult('prom', True, 'ok')}

    # when
    got = {name: read_any(r) for name, r in passes.items()}

    # then
    assert got == {'read': True, 'capped': True, 'refused': False, 'failed': False, 'core': True}


def test_a_quiet_card_is_no_push_baseline_so_a_new_problem_on_its_service_is_pushed():
    # given
    state = first_state()
    merge(state, [found('cpu', 'crit', **{ALREADY_ACTIVE: MARK})], {'zabbix': True}, NOW)

    # when
    events, push, _ = merge(state, [found('cpu', 'crit'), found('disk', 'crit')], {'zabbix': True}, NOW + 900)

    # then
    assert [e['ev'] for e in events] == ['new'] and [a['key'] for a in push] == ['zabbix|web-01|disk']


def test_a_quiet_card_that_clears_and_comes_back_with_a_new_start_reopens_loud():
    # given
    state = first_state()
    merge(state, [found('cpu', startedAt=OLD, **{ALREADY_ACTIVE: MARK})], {'zabbix': True}, NOW)
    merge(state, [], {'zabbix': True}, NOW + 900)

    # when
    events, push, _ = merge(state, [found('cpu', startedAt=iso(NOW + 1700))], {'zabbix': True}, NOW + 1800)

    # then
    assert [e['ev'] for e in events] == ['reopened'] and [a['key'] for a in push] == ['zabbix|web-01|cpu']
    assert ALREADY_ACTIVE not in state['anomalies'][0]
    assert [a['key'] for a in select.eligible(state, 'live', [], NOW + 1860, 5)] == ['zabbix|web-01|cpu']


def test_a_quiet_card_that_clears_and_comes_back_with_the_same_start_stays_quiet():
    # given
    state = first_state()
    merge(state, [found('cpu', startedAt=OLD, **{ALREADY_ACTIVE: MARK})], {'zabbix': True}, NOW)
    merge(state, [], {'zabbix': True}, NOW + 900)

    # when
    events, push, _ = merge(state, [found('cpu', startedAt=OLD)], {'zabbix': True}, NOW + 1800)

    # then
    assert [e['ev'] for e in events] == ['reopened'] and push == []
    assert state['anomalies'][0][ALREADY_ACTIVE] == MARK
    assert select.eligible(state, 'live', [], NOW + 1860, 5) == []


def test_a_quiet_card_closed_by_its_check_going_away_comes_back_quiet():
    # given
    state = first_state()
    merge(state, [found('cpu', startedAt=OLD, **{ALREADY_ACTIVE: MARK})], {'zabbix': True}, NOW)
    engine.retire(state, {'zabbix'}, 'switched off', NOW + 60)

    # when
    events, push, _ = merge(state, [found('cpu', startedAt=OLD, **{ALREADY_ACTIVE: MARK})], {'zabbix': True}, NOW + 900)

    # then
    assert [e['ev'] for e in events] == ['reopened'] and push == []
    assert state['anomalies'][0][ALREADY_ACTIVE] == MARK


def test_a_muted_quiet_card_that_escalates_stays_quiet_until_the_mute_ends_and_then_escalates_loud():
    # given
    state = first_state() | {'muted': [{'pattern': 'zabbix|web-01|cpu', 'until': iso(NOW + 3600), 'reason': 'maintenance', 'by': 'op'}]}
    merge(state, [found('cpu', 'warn', **{ALREADY_ACTIVE: MARK})], {'zabbix': True}, NOW)

    # when
    muted_events, muted_push, _ = merge(state, [found('cpu', 'crit')], {'zabbix': True}, NOW + 900)
    marked = dict(state['anomalies'][0])
    events, push, _ = merge(state, [found('cpu', 'crit')], {'zabbix': True}, NOW + 7200)

    # then
    assert muted_events == [] and muted_push == [] and marked[ALREADY_ACTIVE] == MARK
    assert [e['ev'] for e in events] == ['escalated'] and [a['key'] for a in push] == ['zabbix|web-01|cpu']
    assert ALREADY_ACTIVE not in state['anomalies'][0]


def history_of(*keys: str) -> list[dict]:
    return [{'t': iso(NOW - 60), 'key': k, 'ev': 'new', 'sev': 'crit', 'text': k, 'muted': False} for k in keys]


def open_card(key: str, **extra) -> dict:
    return {'key': key, 'src': 'zabbix', 'svc': 'web-01', 'sev': 'crit', 'status': 'open', 'firstSeen': iso(NOW - 60), 'muted': False} | extra


def test_the_investigator_and_the_slack_fallback_skip_an_already_active_card():
    # given
    state = {'anomalies': [open_card('quiet', **{ALREADY_ACTIVE: MARK}), open_card('loud')], 'history': history_of('quiet', 'loud')}

    # when
    picked = [a['key'] for a in select.eligible(state, 'live', [], NOW, 5)]
    fallback = [a['key'] for a, _ in Fallback(None, 0).due(state, [], NOW + 3600)]

    # then
    assert picked == ['loud']
    assert fallback == ['loud']


def test_an_automatic_route_post_skips_an_already_active_card():
    # given
    route = Route('teams/seo', 'seo', 'teams', 'seo', frozenset({'seo'}), (), None)
    said = {'text': 'We recommend checking the product page.', 'by': 'pod', 'at': AT}
    quiet = route_card('log|a|ERROR|quiet', 'crit', tags=tag('seo'), business=said, **{ALREADY_ACTIVE: MARK})
    loud = route_card('log|a|ERROR|loud', 'crit', tags=tag('seo'), business=said)
    fixed = route_card('log|a|ERROR|fixed', 'crit', tags=tag('seo'), business={**said, 'kind': 'info'}, status='cleared', clearedAt='2026-11-08T00:00:00Z')

    # when
    due = (route.due(quiet), route.due(loud))
    fixed_due = (route.fixed_due(fixed | {ALREADY_ACTIVE: MARK}, NOW), route.fixed_due(fixed, NOW))

    # then
    assert due == (False, True)
    assert fixed_due == (False, True)


def test_a_check_cannot_mark_its_own_cards_already_active():
    # given
    check = Check('zabbix', 'Active Zabbix problems', 900, 0, ('zabbix',), lambda ctx: None)
    raised = Sweep(PassResult('zabbix', True, 'ok', [found('cpu', **{ALREADY_ACTIVE: MARK})], {}, ('zabbix',)))

    # when
    kept = Sweeper(None, None, None)._validated(check, raised)

    # then
    assert [ALREADY_ACTIVE in c for c in kept.result.found] == [False]


def test_the_card_page_gets_the_mark():
    # given
    card = open_card('quiet', num=61, **{ALREADY_ACTIVE: MARK})

    # when
    view = card_view(card, True)

    # then
    assert view[ALREADY_ACTIVE] == MARK


def test_a_quiet_card_that_comes_back_with_the_same_start_at_a_higher_severity_is_pushed():
    # given
    state = first_state()
    merge(state, [found('cpu', 'warn', startedAt=OLD, **{ALREADY_ACTIVE: MARK})], {'zabbix': True}, NOW)
    merge(state, [], {'zabbix': True}, NOW + 900)

    # when
    events, push, _ = merge(state, [found('cpu', 'crit', startedAt=OLD)], {'zabbix': True}, NOW + 1800)
    again_events, again_push, _ = merge(state, [found('cpu', 'crit', startedAt=OLD)], {'zabbix': True}, NOW + 2700)

    # then
    assert [e['ev'] for e in events] == ['reopened'] and [a['key'] for a in push] == ['zabbix|web-01|cpu']
    assert ALREADY_ACTIVE not in state['anomalies'][0] and state['anomalies'][0]['pushedSev'] == 'crit'
    assert again_events == [] and again_push == []


def test_a_quiet_card_resolved_as_fixed_that_comes_back_with_the_same_start_is_pushed():
    # given
    state = first_state()
    merge(state, [found('cpu', startedAt=OLD, **{ALREADY_ACTIVE: MARK})], {'zabbix': True}, NOW)
    state['anomalies'][0].update(triage='handled', triagedBy='alice@example.com', triagedAt=iso(NOW + 300), resolution={'kind': 'fixed', 'by': 'alice@example.com', 'at': iso(NOW + 300)})
    merge(state, [], {'zabbix': True}, NOW + 900)

    # when
    events, push, _ = merge(state, [found('cpu', startedAt=OLD)], {'zabbix': True}, NOW + 1800)

    # then
    assert [e['ev'] for e in events] == ['reopened'] and [a['key'] for a in push] == ['zabbix|web-01|cpu']
    assert ALREADY_ACTIVE not in state['anomalies'][0] and state['anomalies'][0]['cameBack']
