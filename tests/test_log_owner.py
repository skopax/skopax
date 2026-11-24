import pytest

from skopax.engine import merge
from tests.test_engine import NOW, item, stored

KEY = 'log|checkout|ERROR|connection refused to ledger'


def log_item(src: str, now: float, sev: str = 'high') -> dict:
    return item(KEY, src=src, sev=sev, now=now, sig='log', ranKey=src, d1=1, d7=1)


def log_card(src: str, now: float, status: str = 'open', **extra) -> dict:
    return stored(KEY, status=status, src=src, sev='high', now=now, sig='log', ranKey=src, **extra)


def card_of(state: dict) -> dict:
    return next(a for a in state['anomalies'] if a['key'] == KEY)


def test_the_other_log_source_does_not_take_over_escalate_or_refresh_an_open_card():
    # given
    state = {'initialized': True, 'anomalies': [log_card('logs', 20)]}

    # when
    events, push, _ = merge(state, [log_item('loki', 60)], {'loki': True}, NOW)

    # then
    card = card_of(state)
    assert events == [] and push == []
    assert (card['src'], card['ranKey'], card['now'], card['sev'], card['status']) == ('logs', 'logs', 20, 'high', 'open')
    assert card['lastSeen'] == stored(KEY)['lastSeen']


def test_a_quiet_pass_of_the_other_log_source_never_clears_the_card():
    # given
    state = {'initialized': True, 'anomalies': [log_card('logs', 20)]}

    # when
    events, _, _ = merge(state, [], {'loki': True}, NOW)

    # then
    assert events == [] and card_of(state)['status'] == 'open'


def test_a_card_its_owner_cleared_is_reopened_by_the_other_log_source_as_its_new_owner():
    # given
    state = {'initialized': True, 'anomalies': [log_card('logs', 20)]}
    merge(state, [], {'logs': True}, NOW)

    # when
    events, push, _ = merge(state, [log_item('loki', 60)], {'loki': True}, NOW + 420)

    # then
    card = card_of(state)
    assert [e['ev'] for e in events] == ['reopened'] and (card['status'], card['src'], card['ranKey'], card['now']) == ('open', 'loki', 'loki', 60)


@pytest.mark.parametrize('later', [3600, 30 * 86400])
def test_a_critical_burst_of_the_other_log_source_after_the_owner_cleared_is_raised_and_pushed(later):
    # given
    state = {'initialized': True, 'anomalies': [log_card('logs', 20)]}
    merge(state, [], {'logs': True}, NOW)

    # when
    events, push, _ = merge(state, [log_item('loki', 900, sev='crit')], {'loki': True}, NOW + later)

    # then
    card = card_of(state)
    assert [e['ev'] for e in events] in (['reopened'], ['new']) and [p['key'] for p in push] == [KEY]
    assert (card['status'], card['src'], card['sev'], card['now']) == ('open', 'loki', 'crit', 900)


def test_both_log_sources_firing_neither_flips_nor_flaps_the_card():
    # given
    state = {'initialized': True, 'anomalies': [log_card('logs', 20)]}

    # when
    seen = []
    for step in range(6):
        src, now = ('loki', 60) if step % 2 else ('logs', 20)
        events, push, _ = merge(state, [log_item(src, now)], {src: True}, NOW + step * 420)
        seen += [e['ev'] for e in events] + ['push'] * len(push)

    # then
    card = card_of(state)
    assert seen == [] and (card['src'], card['now'], card['sev'], card['status']) == ('logs', 20, 'high', 'open')


def test_once_resolved_either_log_source_opens_the_key_afresh():
    # given
    state = {'initialized': True, 'anomalies': [log_card('logs', 20, status='cleared', clearedAt=stored(KEY)['lastSeen'], triage='handled', triagedBy='someone')]}

    # when
    events, _, _ = merge(state, [log_item('loki', 60)], {'loki': True}, NOW)

    # then
    card = card_of(state)
    assert [e['ev'] for e in events] == ['reopened'] and (card['src'], card['now'], card['status']) == ('loki', 60, 'open')


def test_an_elasticsearch_only_install_escalates_and_clears_as_before():
    # given
    state = {'initialized': True, 'anomalies': [log_card('logs', 20)]}

    # when
    escalated, push, _ = merge(state, [log_item('logs', 60)], {'logs': True}, NOW)
    cleared, _, _ = merge(state, [], {'logs': True}, NOW + 900)

    # then
    assert [e['ev'] for e in escalated] == ['escalated'] and len(push) == 1 and [e['ev'] for e in cleared] == ['cleared']


def test_other_sources_sharing_a_key_still_take_over_as_before():
    # given
    state = {'initialized': True, 'anomalies': [stored('cf-a|x', src='cf', now=1)]}

    # when
    merge(state, [item('cf-a|x', src='prom', now=3)], {'prom': True}, NOW)

    # then
    card = next(a for a in state['anomalies'] if a['key'] == 'cf-a|x')
    assert (card['src'], card['now']) == ('prom', 3)
