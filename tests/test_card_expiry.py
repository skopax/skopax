from skopax.engine import AUTO_RESOLVED_BY, anomaly, follow_parents, iso, merge

NOW = 1_793_790_000.0
H = 3600


def card(key: str, sev: str, src: str, seen_h: float, first_h: float | None = None, **extra) -> dict:
    sig = key.split('|')[0]
    a = anomaly(src, sig, 'ns', key.split('|')[1], '', sev, 5, 1, 1, '', 'label', 'what')
    a.update(key=key, status='open', firstSeen=iso(NOW - (first_h if first_h is not None else seen_h) * H), lastSeen=iso(NOW - seen_h * H),
             pushedAt=None, pushedSev=None, pushedPeak=None, note=None, peak=5, muted=None, **extra)
    return a


def test_an_open_card_its_check_stopped_reporting_closes_after_a_day_a_daily_one_after_three_and_a_manual_one_after_seven():
    # given
    state = {'initialized': True, 'anomalies': [
        card('log|tracker', 'info', 'logs', 25 * 24, ranKey='manual'),
        card('qan|x', 'warn', 'qan', 25),
        card('crux|shop.example.com', 'warn', 'crux', 50),
        card('err|y', 'warn', 'prom', 2),
        card('manual|z', 'info', 'logs', 100, ranKey='manual')]}

    # when
    events, _, _ = merge(state, [], {}, NOW)

    # then
    by = {a['key']: a for a in state['anomalies']}
    assert by['log|tracker']['status'] == 'cleared' and by['log|tracker']['expired'] and by['log|tracker']['note']['text'].startswith('Closed: not seen since ')
    assert by['qan|x']['status'] == 'cleared', 'a 30-min source silent for 25 h'
    assert by['crux|shop.example.com']['status'] == 'open', 'a daily source gets 3 days'
    assert by['err|y']['status'] == 'open' and by['manual|z']['status'] == 'open'
    assert sorted(e['key'] for e in events if e['ev'] == 'cleared') == ['log|tracker', 'qan|x']


def test_an_auto_closed_info_card_that_escalates_opens_again_for_the_investigator():
    # given
    a = card('edge|shop.example.com', 'info', 'edge', 0.5, 80, triage='handled', triagedBy=AUTO_RESOLVED_BY)
    state = {'initialized': True, 'anomalies': [a]}
    up = dict(card('edge|shop.example.com', 'warn', 'edge', 0), now=50)

    # when
    _, push, _ = merge(state, [up], {}, NOW)

    # then
    assert state['anomalies'][0]['triage'] is None and push


def test_cleared_children_follow_a_handled_parent_and_a_parent_that_left_the_page():
    # given
    def cleared(key, **extra):
        a = card(key, 'warn', 'prom', 30, **extra)
        a.update(status='cleared', clearedAt=iso(NOW - 29 * H))
        return a
    state = {'anomalies': [
        cleared('5xx|ns/a', triage='handled', triagedBy='oscar'),
        cleared('err|ns/a', parent='5xx|ns/a'),
        cleared('out-err|ns/gone', parent='5xx|ns/gone'),
        cleared('5xx|ns/b', triage='progress', ticket='OPS-1'),
        cleared('err|ns/b', parent='5xx|ns/b'),
        cleared('5xx|ns/c'),
        cleared('err|ns/c', parent='5xx|ns/c'),
        dict(card('err|ns/d', 'warn', 'prom', 0.1, parent='5xx|ns/gone')),
        cleared('err|ns/e', parent='5xx|ns/gone', triage='dived'),
        cleared('err|ns/f', parent='5xx|ns/gone', triage='investigating'),
        cleared('err|ns/g', parent='5xx|ns/a', triage='handled', triagedBy='oscar')]}

    # when
    out = follow_parents(state, NOW)

    # then
    by = {a['key']: a for a in state['anomalies']}
    assert sorted(x['key'] for x in out) == ['err|ns/a', 'err|ns/b', 'err|ns/e', 'out-err|ns/gone']
    assert by['err|ns/a']['triage'] == 'handled' and by['err|ns/a']['triagedBy'] == AUTO_RESOLVED_BY and by['err|ns/a']['autoResolvedAt'] == iso(NOW)
    assert by['out-err|ns/gone']['triage'] == 'handled' and by['out-err|ns/gone']['note']['text'] == 'Auto-resolved: its parent 5xx|ns/gone was handled and has left the page'
    assert by['err|ns/b']['triage'] == 'progress' and by['err|ns/b']['ticket'] == 'OPS-1' and 'autoResolvedAt' not in by['err|ns/b']
    assert by['err|ns/b']['note']['text'] == 'In progress with its parent: 5xx|ns/b (OPS-1)' and [x['text'] for x in out if x['key'] == 'err|ns/b'] == ['in progress with its parent ns/b label · 5xx|ns/b (OPS-1)']
    assert by['err|ns/c'].get('triage') is None, 'a parent still waiting keeps its children waiting with it'
    assert by['err|ns/e']['triage'] == 'handled', 'a child the investigator only looked at (dived) follows its parent too'
    assert by['err|ns/d'].get('triage') is None and by['err|ns/f']['triage'] == 'investigating' and by['err|ns/g']['triagedBy'] == 'oscar', 'open children, a run in flight and a human outcome are left alone'
