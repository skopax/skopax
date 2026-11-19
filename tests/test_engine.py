import dataclasses
import datetime as dt

import pytest
from fastapi.testclient import TestClient

from tests.conftest import settings
from tests.test_jiralinks import FakeJira, issue
from skopax.app import create_app
from skopax import clock
from skopax.engine import PassResult, apply, iso, merge, new_pushes, push_line
from skopax.jiralinks import JiraLinks
from skopax.store import Conflict, Store

NOW = 1_792_224_000.0
def ago(seconds: float) -> str:
    return iso(NOW - seconds)


def item(key: str, src: str = 'cf', sev: str = 'warn', now: float = 1, **extra) -> dict:
    return {'key': key, 'src': src, 'sig': key.split('|')[0], 'ns': 'cloudflare', 'svc': key.split('|')[1], 'sub': '', 'sev': sev, 'now': now, 'd1': None, 'd7': None,
            'unit': '', 'label': 'x', 'what': f'{key} happened', 'short': f'{sev.upper()} {key}', 'slack': 1, **extra}


def stored(key: str, status: str = 'open', src: str = 'cf', sev: str = 'warn', now: float = 1, **extra) -> dict:
    return {**item(key, src=src, sev=sev, now=now), 'firstSeen': ago(7200), 'lastSeen': ago(1800), 'peak': now, 'status': status,
            'pushedAt': ago(7200), 'pushedSev': sev, 'pushedPeak': now, 'note': None, **extra}


def scenario() -> tuple[dict, list[dict], dict]:
    state = {
        'initialized': True,
        'muted': [
            {'pattern': 'cf-status|cloudflare/WAF*', 'until': None, 'reason': 'known noise', 'kind': 'known', 'at': ago(86400)},
            {'pattern': 'cf-health|*', 'until': ago(60), 'reason': 'expired', 'kind': 'mute', 'at': ago(86400)},
        ],
        'anomalies': [
            stored('cf-cert|universal', now=10, sev='warn', suspects=['renewal stuck'], triage='dived', slackDraft={'text': 'x', 'at': ago(600), 'by': 'Claude'}, slackFallback={'at': ago(600), 'firedAt': ago(7200)}),
            stored('cf-status|cdn', status='cleared', clearedAt=ago(3600), note={'text': 'checked', 'at': ago(3500)}),
            stored('cf-status|old-info', status='cleared', sev='info', clearedAt=ago(30 * 3600)),
            stored('cf-health|origin', sev='crit'),
            stored('edge|shop.example.com|edge-conn', ranKey='edge'),
            stored('log|checkout-service|ERROR|boom', src='logs', sev='crit'),
            stored('cf-incident|parent', absorb=['cf-status|cloudflare/*']),
            stored('cf-status|handled-cleared', status='cleared', clearedAt=ago(30 * 3600), triage='handled'),
        ],
        'history': [{'t': ago(7200), 'key': 'x', 'ev': 'new', 'sev': 'warn', 'text': 'old', 'muted': False}],
    }
    found = [
        item('cf-cert|universal', now=25, sev='warn'),
        item('cf-status|cdn', now=1, sev='warn'),
        item('cf-new|zone', sev='crit', now=3),
        item('cf-status|cloudflare/WAF', sev='warn'),
        item('cf-status|cloudflare/Turnstile', sev='warn'),
        item('cf-cert|universal', now=99),
    ]
    return state, found, {'cf': True}


def test_clock_date_reads_day_and_english_month_in_the_zone():
    # given
    feb = dt.datetime(2026, 2, 5, 6, 7, tzinfo=dt.timezone.utc)
    firsts = [dt.datetime(2026, m, 1, 12, tzinfo=dt.timezone.utc) for m in range(1, 13)]

    # then
    assert clock.date(feb) == '5 Feb 06:07'
    assert clock.date(feb, hm=False) == '5 Feb'
    assert clock.date(NOW) == '17 Oct 08:00', 'a timestamp works too'
    assert clock.date(dt.datetime(2026, 10, 31, 22, 30, tzinfo=dt.timezone.utc)) == '31 Oct 22:30'
    assert [clock.date(d, hm=False) for d in firsts] == ['1 Jan', '1 Feb', '1 Mar', '1 Apr', '1 May', '1 Jun', '1 Jul', '1 Aug', '1 Sep', '1 Oct', '1 Nov', '1 Dec']
    clock.set_zone('Europe/Berlin')
    assert clock.date(dt.datetime(2026, 10, 31, 22, 30, tzinfo=dt.timezone.utc)) == '31 Oct 23:30'
    assert clock.date(dt.datetime(2026, 11, 1, 23, 30, tzinfo=dt.timezone.utc)) == '2 Nov 00:30'


def test_merge_keeps_assessments_escalates_reopens_and_clears_only_what_ran():
    # given
    state, found, ran = scenario()

    # when
    events, push, first = merge(state, found, ran, NOW)
    by = {a['key']: a for a in state['anomalies']}

    # then
    assert first is False
    assert {e['key']: e['ev'] for e in events} == {
        'cf-cert|universal': 'escalated', 'cf-status|cdn': 'reopened', 'cf-new|zone': 'new', 'cf-status|cloudflare/WAF': 'new',
        'cf-status|cloudflare/Turnstile': 'new', 'cf-health|origin': 'cleared', 'cf-incident|parent': 'cleared'}
    assert [a['key'] for a in push] == ['cf-cert|universal', 'cf-new|zone']
    assert by['cf-cert|universal']['suspects'] == ['renewal stuck']
    assert by['cf-cert|universal']['slackDraft']['text'] == 'x'
    assert by['cf-cert|universal']['slackFallback'] == {'at': ago(600), 'firedAt': ago(7200)}
    assert by['cf-cert|universal']['pushedPeak'] == 25
    assert by['cf-status|cdn']['note'] == {'text': 'checked', 'at': ago(3500)}
    assert by['cf-status|cloudflare/WAF']['muted'] == 'known noise'
    assert by['cf-status|cloudflare/Turnstile']['parent'] == 'cf-incident|parent'
    assert by['cf-health|origin']['status'] == 'cleared'
    assert by['edge|shop.example.com|edge-conn']['status'] == 'open'
    assert by['log|checkout-service|ERROR|boom']['status'] == 'open'
    assert 'cf-status|old-info' not in by
    assert 'cf-status|handled-cleared' not in by
    assert [m['pattern'] for m in state['muted']] == ['cf-status|cloudflare/WAF*']
    assert state['history'][-1]['text'] == 'old'


def test_a_take_offer_mark_stays_on_a_card_that_stays_open_and_drops_when_it_reopens():
    # given
    offer = {'ts': '1792224000.000200', 'by': 'adam.baker@example.com', 'at': ago(600)}
    state = {'initialized': True, 'muted': [], 'history': [],
             'anomalies': [stored('cf-cert|universal', takeOffered=offer), stored('cf-status|cdn', status='cleared', clearedAt=ago(3600), takeOffered=offer)]}

    # when
    merge(state, [item('cf-cert|universal'), item('cf-status|cdn')], {'cf': True}, NOW)
    by = {a['key']: a for a in state['anomalies']}

    # then
    assert by['cf-cert|universal']['takeOffered'] == offer
    assert by['cf-status|cdn']['status'] == 'open' and 'takeOffered' not in by['cf-status|cdn']


def test_a_card_a_person_resolved_that_fires_again_is_marked_back_after_resolve_until_it_reopens_without_one_and_the_service_s_own_resolves_never_mark():
    # given
    keys = ('cf-status|cdn', 'cf-status|waf', 'cf-status|dns', 'cf-status|api')
    state = {'initialized': True, 'muted': [], 'history': [],
             'anomalies': [stored(keys[0], status='cleared', clearedAt=ago(3600), triage='handled', triagedBy='nina.white@example.com'),
                           stored(keys[1], status='cleared', clearedAt=ago(3600), triage='handled', triagedBy='auto'),
                           stored(keys[2], status='cleared', clearedAt=ago(3600), triage='handled', triagedBy='pod'),
                           stored(keys[3], status='cleared', clearedAt=ago(3600))]}

    # when
    merge(state, [item(k) for k in keys], {'cf': True}, NOW)
    back = {a['key']: a.get('backAfterResolve') for a in state['anomalies']}
    merge(state, [item(k) for k in keys], {'cf': True}, NOW + 900)
    still = {a['key']: a.get('backAfterResolve') for a in state['anomalies']}
    merge(state, [], {'cf': True}, NOW + 1800)
    merge(state, [item(keys[0])], {'cf': True}, NOW + 2700)
    again = next(a for a in state['anomalies'] if a['key'] == keys[0])

    # then
    assert back == {keys[0]: iso(NOW), keys[1]: None, keys[2]: None, keys[3]: None}
    assert still == back, 'the mark stays while the card keeps firing'
    assert again['status'] == 'open' and 'backAfterResolve' not in again and again.get('triage') is None, 'a later return nobody resolved carries no mark'


def test_a_severity_set_by_hand_survives_every_pass_and_keeps_the_signal_s_own_beside_it():
    # given
    manual = {'sev': 'info', 'by': 'nina.white@example.com', 'at': ago(600), 'auto': 'crit'}
    state = {'initialized': True, 'muted': [], 'history': [],
             'anomalies': [stored('cf-cert|universal', sev='info', sevSet=manual, pushedSev='crit'), stored('cf-status|cdn', status='cleared', clearedAt=ago(3600), sev='crit', sevSet=dict(manual, sev='crit'))]}

    # when
    _, pushes, _ = merge(state, [item('cf-cert|universal', sev='crit', now=50), item('cf-status|cdn', sev='info')], {'cf': True}, NOW)
    by = {a['key']: a for a in state['anomalies']}

    # then
    assert (by['cf-cert|universal']['sev'], by['cf-cert|universal']['sevAuto'], by['cf-cert|universal']['sevSet']) == ('info', 'crit', manual), 'a still firing card keeps the hand-set severity'
    assert (by['cf-status|cdn']['sev'], by['cf-status|cdn']['sevAuto']) == ('crit', 'info'), 'a reopened card too'
    assert 'cf-cert|universal' not in [p['key'] for p in pushes], 'an info card set by hand never pushes, even when the signal says crit'


def test_push_line_is_one_line_worst_first_and_cut():
    # given
    items = [item('a|x', sev='warn', short='WARN a'), item('b|x', sev='crit', short='CRIT b')]

    # when
    line = push_line(items)
    long = push_line([item(f'k|{i}', short='W' * 50) for i in range(6)])

    # then
    assert line == 'Skopax: CRIT b; WARN a'
    assert len(long) == 197 and long.endswith('…')
    assert push_line([]) == ''


def test_new_pushes_are_the_fresh_pushed_at_changes_only():
    # given
    t = ago(60)
    before = {'same': t, 'old': ago(7200)}
    items = [
        {'key': 'same', 'sev': 'warn', 'pushedAt': t},
        {'key': 'old', 'sev': 'crit', 'pushedAt': t},
        {'key': 'new', 'sev': 'warn', 'pushedAt': t},
        {'key': 'info', 'sev': 'info', 'pushedAt': t},
        {'key': 'muted', 'sev': 'crit', 'pushedAt': t, 'muted': 'noise'},
        {'key': 'child', 'sev': 'crit', 'pushedAt': t, 'parent': 'p', 'firstSeen': t},
        {'key': 'child-up', 'sev': 'crit', 'pushedAt': t, 'parent': 'p', 'firstSeen': ago(9000)},
        {'key': 'stale', 'sev': 'crit', 'pushedAt': ago(4 * 3600)},
        {'key': 'never', 'sev': 'crit', 'pushedAt': None},
    ]

    # when
    got = new_pushes(before, items, NOW)

    # then
    assert [a['key'] for a in got] == ['old', 'new', 'child-up']


def seeded(store: Store, *anomalies: dict, initialized: bool = True) -> Store:
    store.save({'initialized': initialized, 'anomalies': list(anomalies), 'sources': {'prom': 'ok', 'cf': 'ok'}, 'sweptIso': ago(300), 'muted': []}, None)
    return store


def test_a_pass_clears_every_key_it_ran_and_the_after_hook_sees_the_merged_state(store: Store):
    # given
    seeded(store, stored('restarts|billing/payments-api', src='kube'), stored('alert|DiskFull|x', src='alert'), stored('err|billing/payments-api', src='prom'),
           stored('log|checkout-service|ERROR|boom', src='logs'))
    seen = []

    def after(state):
        seen.append(sorted(a['key'] for a in state['anomalies'] if a['status'] == 'open'))
        state['services'] = [{'svc': 'payments-api', 'status': 'ok'}]

    # when
    apply(store, [PassResult('prom', True, 'ok', [item('err|billing/payments-api', src='prom')], {}, ('prom', 'kube', 'alert'))], NOW, after=after)
    state = store.load()
    by = {a['key']: a['status'] for a in state['anomalies']}

    # then
    assert by == {'restarts|billing/payments-api': 'cleared', 'alert|DiskFull|x': 'cleared', 'err|billing/payments-api': 'open', 'log|checkout-service|ERROR|boom': 'open'}
    assert seen == [['err|billing/payments-api', 'log|checkout-service|ERROR|boom']]
    assert state['services'] == [{'svc': 'payments-api', 'status': 'ok'}]
    assert state['sources']['prom'] == 'ok' and 'kube' not in state['sources']


def test_apply_merges_the_server_passes_and_writes_sources_and_run_times(store: Store):
    # given
    seeded(store, stored('cf-health|origin', sev='crit'), stored('log|checkout-service|ERROR|boom', src='logs'))
    results = [PassResult('cf', True, 'ok', [item('cf-new|zone', sev='crit')], {'cloudflare': {'checks': [{'name': 'origin', 'status': 'healthy'}]}}),
               PassResult('edge', False, 'error: Cloudflare GraphQL HTTP 403')]

    # when
    applied = apply(store, results, NOW, lambda a: [{'label': 'Cloudflare status page', 'url': 'https://www.cloudflarestatus.com/'}] if a.get('src') == 'cf' else None, dm=True)
    state = store.load()
    by = {a['key']: a for a in state['anomalies']}

    # then
    assert applied.rev == state['rev'] == 2
    assert [a['key'] for a in applied.push] == ['cf-new|zone']
    assert by['cf-health|origin']['status'] == 'cleared'
    assert by['log|checkout-service|ERROR|boom']['status'] == 'open'
    assert 'links' not in by['log|checkout-service|ERROR|boom']
    assert by['cf-new|zone']['links'][0]['label'] == 'Cloudflare status page'
    assert state['sources'] == {'prom': 'ok', 'cf': 'ok', 'edge': 'error: Cloudflare GraphQL HTTP 403'}
    assert state['serverPasses'] == {'cf': {'at': iso(NOW), 'status': 'ok'}, 'edge': {'at': iso(NOW), 'status': 'error: Cloudflare GraphQL HTTP 403'}}
    assert state['cloudflare'] == {'checks': [{'name': 'origin', 'status': 'healthy'}]}
    assert 'edge' not in state
    assert state['sweptIso'] == ago(300)
    assert state['serverPushes'] == [{'at': iso(NOW), 'line': 'Skopax: CRIT cf-new|zone', 'keys': ['cf-new|zone'], 'dm': True}]
    assert state['summary']['level'] == 'crit' and state['summary']['at'] == iso(NOW)


class Racing(Store):
    def __init__(self, db, writes: int):
        super().__init__(db)
        self.writes = writes

    def load(self) -> dict:
        state = super().load()
        if self.writes:
            self.writes -= 1
            other = super().load()
            other['anomalies'].append(stored(f'log|other-{self.writes}|ERROR|x', src='logs'))
            super().save(other, None)
        return state


def test_apply_rereads_and_merges_again_when_another_write_landed_in_between(store: Store):
    # given
    racing = Racing(store.db, writes=2)
    seeded(racing, stored('cf-health|origin', sev='crit'))

    # when
    applied = apply(racing, [PassResult('cf', True, 'ok', [])], NOW)
    keys = [a['key'] for a in racing.load()['anomalies']]

    # then
    assert applied.rev == 4
    assert set(keys) == {'cf-health|origin', 'log|other-1|ERROR|x', 'log|other-0|ERROR|x'}


def test_apply_gives_up_with_a_conflict_after_the_attempts(store: Store):
    # given
    racing = Racing(store.db, writes=10)
    seeded(racing)

    # then
    with pytest.raises(Conflict):
        apply(racing, [PassResult('cf', True, 'ok', [])], NOW, attempts=3)


def pod_scenario() -> tuple[dict, list[dict]]:
    state = {'initialized': True, 'muted': [], 'history': [], 'anomalies': [
        stored('cf-a|x', now=10, triage='handled', triagedBy='pod', investigator={'runId': 'r1', 'at': ago(60)}),
        stored('cf-b|y', now=10, triage='handled'),
        stored('cf-c|z', now=10, triage='progress', triagedBy='pod', ticket='OPS-1'),
    ]}
    found = [item('cf-a|x', now=30), item('cf-b|y', now=30), item('cf-c|z', now=11)]
    return state, found


def test_a_pod_triaged_card_that_grows_goes_back_to_open_and_a_human_one_stays_handled():
    # given
    state, found = pod_scenario()

    # when
    events, _, _ = merge(state, found, {'cf': True}, NOW)
    by = {a['key']: a for a in state['anomalies']}

    # then
    assert (by['cf-a|x']['triage'], by['cf-a|x']['triagedBy']) == (None, None)
    assert by['cf-a|x']['investigator'] == {'runId': 'r1', 'at': ago(60)}
    assert by['cf-b|y']['triage'] == 'handled'
    assert (by['cf-c|z']['triage'], by['cf-c|z']['ticket'], by['cf-c|z']['triagedBy']) == ('progress', 'OPS-1', 'pod')
    assert {e['key'] for e in events if e['ev'] == 'escalated'} == {'cf-a|x', 'cf-b|y'}


def pod_noted_scenario() -> tuple[dict, list[dict]]:
    state = {'initialized': True, 'muted': [], 'history': [], 'anomalies': [
        stored('cf-d|w', now=10, triage='dived', triagedBy='pod', note={'text': 'pod looked', 'at': ago(600)}),
        stored('cf-e|v', now=10, triage='dived', note={'text': 'human looked', 'at': ago(600)}),
        stored('cf-f|u', now=10, triage='dived', triagedBy='pod'),
    ]}
    found = [item('cf-d|w', now=30), item('cf-e|v', now=30), item('cf-f|u', now=11)]
    return state, found


def test_a_pod_noted_card_that_grows_goes_back_to_open_and_a_human_noted_one_stays_investigated():
    # given
    state, found = pod_noted_scenario()

    # when
    events, _, _ = merge(state, found, {'cf': True}, NOW)
    by = {a['key']: a for a in state['anomalies']}

    # then
    assert (by['cf-d|w']['triage'], by['cf-d|w']['triagedBy'], by['cf-d|w']['note']['text']) == (None, None, 'pod looked')
    assert by['cf-e|v']['triage'] == 'dived'
    assert (by['cf-f|u']['triage'], by['cf-f|u']['triagedBy']) == ('dived', 'pod')
    assert {e['key'] for e in events if e['ev'] == 'escalated'} == {'cf-d|w', 'cf-e|v'}


def test_a_service_that_pushed_in_the_last_30_minutes_does_not_dm_again_unless_severity_rises(store: Store):
    # given
    from skopax.engine import merge
    st = {'initialized': True, 'muted': [], 'history': [], 'anomalies': [stored('a|svc1', pushedAt=ago(600), pushedSev='warn', pushedPeak=1)]}
    found = [item('a|svc1', now=1), item('b|svc1', sev='warn', now=5), item('c|svc1', sev='crit', now=5), item('d|svc2', sev='warn')]

    # when
    events, push, first = merge(st, found, {'cf': True}, NOW)
    by = {a['key']: a for a in st['anomalies']}

    # then
    assert not first
    assert [a['key'] for a in push] == ['c|svc1', 'd|svc2'], 'the warn twin of a service that DMed 10 min ago is skipped, the crit one and another service go out'
    assert by['b|svc1']['pushedAt'] == iso(NOW) and by['b|svc1']['pushedSev'] == 'warn', 'the skipped card still counts as pushed, so it only DMs again when it doubles or rises'
    assert {e['key']: e['ev'] for e in events} == {'b|svc1': 'new', 'c|svc1': 'new', 'd|svc2': 'new'}


DAY = 86400
OLD = 3 * DAY + 60
YOUNG = 2 * DAY + 23 * 3600
CART = 'log|checkout-service|ERROR|boom'


def waiting(key: str, cleared: float = OLD, **extra) -> dict:
    return stored(key, status='cleared', src='logs', clearedAt=ago(cleared), label='ERROR pattern', **extra)


def auto_lines(store: Store) -> list[dict]:
    return [r for r in store.list_activity(None, 50) if r['kind'] == 'auto']


def test_a_waiting_card_nobody_acted_on_for_three_days_is_auto_resolved_with_a_note_and_one_activity_line(store: Store):
    # given
    seeded(store, waiting(CART), waiting('log|payments-api|ERROR|x', YOUNG))

    # when
    apply(store, [], NOW, auto_resolve_hours=72)
    by = {a['key']: a for a in store.load()['anomalies']}
    lines = auto_lines(store)

    # then
    old, young = by[CART], by['log|payments-api|ERROR|x']
    assert (old['triage'], old['triagedBy'], old['autoResolvedAt']) == ('handled', 'auto', iso(NOW))
    assert old['note'] == {'text': 'Auto-resolved: cleared 14 Oct 07:59, nobody acted in 3 days', 'at': iso(NOW)}
    assert young.get('triage') is None and 'triagedBy' not in young and 'autoResolvedAt' not in young
    assert [(r['text'], r['who'], r['key']) for r in lines] == [('auto-resolved checkout-service ERROR pattern · cleared 14 Oct 07:59, nobody acted in 3 days', 'system', CART)]
    assert store.load()['summary']['headline'] == '1 waiting to be addressed'


def test_the_auto_note_is_written_only_on_a_card_without_a_note(store: Store):
    # given
    seeded(store, waiting(CART), waiting('log|payments-api|ERROR|x', triage='dived', triagedBy='pod', note={'text': 'one client retried 40 times', 'at': ago(OLD)}))

    # when
    apply(store, [], NOW, auto_resolve_hours=72)
    bare, noted = store.anomaly(CART), store.anomaly('log|payments-api|ERROR|x')

    # then
    assert bare['note'] == {'text': 'Auto-resolved: cleared 14 Oct 07:59, nobody acted in 3 days', 'at': iso(NOW)}
    assert noted['note'] == {'text': 'one client retried 40 times', 'at': ago(OLD)}
    assert (noted['triage'], noted['triagedBy'], noted['autoResolvedAt']) == ('handled', 'auto', iso(NOW))
    assert len(auto_lines(store)) == 2


def test_the_note_names_the_configured_window_in_days_when_it_divides_by_24(store: Store):
    # given
    seeded(store, waiting(CART, cleared=DAY + 60))

    # when
    apply(store, [], NOW, auto_resolve_hours=24)

    # then
    assert store.anomaly(CART)['note']['text'] == 'Auto-resolved: cleared 16 Oct 07:59, nobody acted in 1 day'


def test_auto_resolve_leaves_info_muted_addressed_investigating_and_merged_cards_alone():
    # given
    from skopax.engine import auto_resolve
    state = {'anomalies': [
        waiting('log|a|ERROR|plain'),
        waiting('log|b|ERROR|dived', triage='dived', triagedBy='pod', note={'text': 'pod looked', 'at': ago(OLD)}),
        waiting('log|c|ERROR|info', sev='info'),
        waiting('log|d|ERROR|muted', muted='muted on the page for 24 h'),
        waiting('log|e|ERROR|handled', triage='handled'),
        waiting('log|f|ERROR|progress', triage='progress', ticket='OPS-1'),
        waiting('log|g|ERROR|investigating', triage='investigating', investigator={'runId': 'r1', 'at': ago(60)}),
        waiting('log|h|ERROR|settled-parent', triage='handled'),
        waiting('log|i|ERROR|kid', parent='log|h|ERROR|settled-parent'),
        stored('log|j|ERROR|open-parent', src='logs'),
        waiting('log|k|ERROR|kid', parent='log|j|ERROR|open-parent'),
        waiting('log|l|ERROR|undone', triage='dived', autoResolvedAt=ago(DAY), note={'text': 'a human looked again', 'at': ago(600)}),
        stored('log|m|ERROR|no-clear-time', status='cleared', src='logs'),
    ]}

    # when
    done = auto_resolve(state, NOW, 72)
    by = {a['key']: a for a in state['anomalies']}

    # then
    assert [d['key'] for d in done] == ['log|a|ERROR|plain', 'log|b|ERROR|dived']
    assert {k for k, a in by.items() if a.get('triagedBy') == 'auto'} == {'log|a|ERROR|plain', 'log|b|ERROR|dived'}
    assert (by['log|b|ERROR|dived']['triage'], by['log|b|ERROR|dived']['note']['text']) == ('handled', 'pod looked')
    assert by['log|g|ERROR|investigating']['triage'] == 'investigating'
    assert by['log|l|ERROR|undone']['note']['text'] == 'a human looked again'
    assert by['log|i|ERROR|kid'].get('triage') is None and by['log|k|ERROR|kid'].get('triage') is None


def test_zero_hours_turns_auto_resolve_off(store: Store):
    # given
    seeded(store, waiting(CART, cleared=30 * DAY))

    # when
    apply(store, [], NOW, auto_resolve_hours=0)

    # then
    assert store.anomaly(CART).get('triage') is None
    assert auto_lines(store) == []


def test_an_auto_resolved_card_and_its_merged_cards_stay_24_hours_after_the_auto_resolve_then_drop():
    # given
    state = {'initialized': True, 'muted': [], 'history': [], 'anomalies': [
        waiting('log|a|ERROR|recent', 4 * DAY, triage='handled', triagedBy='auto', autoResolvedAt=ago(23 * 3600)),
        waiting('log|b|ERROR|kid', 4 * DAY, parent='log|a|ERROR|recent'),
        waiting('log|c|ERROR|old', 5 * DAY, triage='handled', triagedBy='auto', autoResolvedAt=ago(25 * 3600)),
        waiting('log|d|ERROR|human', 4 * DAY, triage='handled'),
    ]}

    # when
    merge(state, [], {}, NOW)

    # then
    assert [a['key'] for a in state['anomalies']] == ['log|a|ERROR|recent', 'log|b|ERROR|kid']


TRACKED = 'log|catalog-service|ERROR|npe'
FREE = 'Android nonce fix, no Jira task yet'


def kept_keys(store: Store) -> list[str]:
    return [a['key'] for a in store.load()['anomalies']]


def test_a_card_in_progress_with_a_jira_ticket_and_its_merged_cards_stay_however_long_ago_they_cleared():
    # given
    state = {'initialized': True, 'muted': [], 'history': [], 'anomalies': [
        waiting(TRACKED, 5 * DAY, triage='progress', ticket='OPS-134', owner='Oscar Green'),
        waiting('log|b|ERROR|kid', 5 * DAY, parent=TRACKED),
        waiting('log|c|ERROR|lowercase', 5 * DAY, triage='progress', ticket='ops-134'),
    ]}

    # when
    merge(state, [], {}, NOW)

    # then
    assert [a['key'] for a in state['anomalies']] == [TRACKED, 'log|b|ERROR|kid']


def test_a_card_in_progress_with_free_text_or_no_ticket_still_drops_24_hours_after_it_cleared():
    # given
    state = {'initialized': True, 'muted': [], 'history': [], 'anomalies': [
        waiting('log|a|ERROR|free-young', 23 * 3600, triage='progress', ticket=FREE),
        waiting('log|b|ERROR|free-old', 25 * 3600, triage='progress', ticket=FREE, triagedAt=ago(3600)),
        waiting('log|c|ERROR|bare-old', 25 * 3600, triage='progress'),
    ]}

    # when
    merge(state, [], {}, NOW)

    # then
    assert [a['key'] for a in state['anomalies']] == ['log|a|ERROR|free-young']


def test_a_resolved_card_and_its_merged_cards_stay_24_hours_after_the_resolve_then_drop():
    # given
    state = {'initialized': True, 'muted': [], 'history': [], 'anomalies': [
        waiting('log|a|ERROR|recent', 5 * DAY, triage='handled', ticket='OPS-135', triagedAt=ago(23 * 3600)),
        waiting('log|b|ERROR|kid', 5 * DAY, parent='log|a|ERROR|recent'),
        waiting('log|c|ERROR|old', 5 * DAY, triage='handled', ticket='OPS-136', triagedAt=ago(25 * 3600)),
        waiting('log|d|ERROR|old-kid', 5 * DAY, parent='log|c|ERROR|old'),
        waiting('log|e|ERROR|unstamped', 5 * DAY, triage='handled', ticket='OPS-137'),
        waiting('log|f|ERROR|re-resolved', 5 * DAY, triage='handled', triagedAt=ago(3600), autoResolvedAt=ago(30 * 3600)),
    ]}

    # when
    merge(state, [], {}, NOW)

    # then
    assert [a['key'] for a in state['anomalies']] == ['log|a|ERROR|recent', 'log|b|ERROR|kid', 'log|f|ERROR|re-resolved']


def test_open_waiting_info_and_known_noise_cards_keep_their_rules_next_to_tracked_ones():
    # given
    state = {'initialized': True, 'history': [],
             'muted': [{'pattern': 'log|e|*', 'until': None, 'reason': 'known noise', 'kind': 'known', 'at': ago(DAY)}],
             'anomalies': [
                 stored('log|a|ERROR|open', src='logs'),
                 stored('log|b|ERROR|open-tracked', src='logs', triage='progress', ticket='OPS-134'),
                 waiting('log|c|ERROR|waiting', 5 * DAY),
                 waiting('log|d|ERROR|info', 25 * 3600, sev='info'),
                 waiting('log|e|ERROR|known', 25 * 3600),
             ]}

    # when
    merge(state, [], {}, NOW)

    # then
    assert [a['key'] for a in state['anomalies']] == ['log|a|ERROR|open', 'log|b|ERROR|open-tracked', 'log|c|ERROR|waiting']


def test_a_tracked_card_stays_until_its_ticket_closes_then_24_hours_under_action_taken(store: Store):
    # given
    seeded(store, waiting(TRACKED, 5 * DAY, triage='progress', ticket='OPS-134'), waiting('log|f|ERROR|free', 5 * DAY, triage='progress', ticket=FREE))
    jira = FakeJira([issue('OPS-134', 'Live', 'x')])

    # when
    apply(store, [], NOW)
    tracked = kept_keys(store)
    resolved = JiraLinks(store, jira).closed_tickets(NOW + DAY)
    card = store.anomaly(TRACKED)
    apply(store, [], NOW + DAY + 23 * 3600)
    day_after = kept_keys(store)
    apply(store, [], NOW + DAY + 25 * 3600)

    # then
    assert tracked == [TRACKED]
    assert resolved == [{'card': TRACKED, 'issue': 'OPS-134', 'closed': 'Live'}]
    assert (card['triage'], card['ticket'], card['triagedAt']) == ('handled', 'OPS-134', iso(NOW + DAY))
    assert day_after == [TRACKED]
    assert kept_keys(store) == []


def test_a_second_pass_does_not_resolve_or_log_again(store: Store):
    # given
    seeded(store, waiting(CART))
    apply(store, [], NOW, auto_resolve_hours=72)

    # when
    apply(store, [], NOW + 600, auto_resolve_hours=72)
    card = store.anomaly(CART)

    # then
    assert (card['triage'], card['autoResolvedAt'], card['note']['at']) == ('handled', iso(NOW), iso(NOW))
    assert len(auto_lines(store)) == 1


def test_the_summary_stops_counting_an_auto_resolved_card_as_waiting(store: Store):
    # given
    seeded(store, waiting(CART))

    # when
    apply(store, [], NOW, auto_resolve_hours=72)

    # then
    assert store.load()['summary'] == {'headline': 'All clear', 'points': [], 'level': 'ok', 'at': iso(NOW)}


def test_an_auto_resolved_card_that_fires_again_comes_back_as_a_normal_open_card_and_so_does_a_human_handled_one():
    # given
    state = {'initialized': True, 'muted': [], 'history': [], 'anomalies': [
        waiting(CART, 4 * DAY, triage='handled', triagedBy='auto', autoResolvedAt=ago(3600), note={'text': 'Auto-resolved: x', 'at': ago(3600)}, pushedAt=ago(5 * DAY)),
        waiting('log|payments-api|ERROR|x', 4 * DAY, triage='handled', pushedAt=ago(5 * DAY))]}
    found = [item(CART, src='logs', label='ERROR pattern'), item('log|payments-api|ERROR|x', src='logs', label='ERROR pattern')]

    # when
    events, push, _ = merge(state, found, {'logs': True}, NOW)
    by = {a['key']: a for a in state['anomalies']}

    # then
    auto, human = by[CART], by['log|payments-api|ERROR|x']
    assert [e['ev'] for e in events] == ['reopened', 'reopened']
    assert (auto['status'], auto['triage'], auto['triagedBy']) == ('open', None, None)
    assert 'autoResolvedAt' not in auto
    assert auto['note']['text'] == 'Auto-resolved: x'
    assert (human['status'], human['triage'], human['triagedBy']) == ('open', None, None)
    assert [a['key'] for a in push] == [CART, 'log|payments-api|ERROR|x']


HAND = 'log|payments-api|ERROR|x'
POD = 'log|pod-service|ERROR|x'


def handled_reopen(pushed: float) -> tuple[dict, list[dict]]:
    state = {'initialized': True, 'muted': [], 'history': [], 'anomalies': [
        waiting(HAND, 3600, triage='handled', ticket='OPS-1', note={'text': 'one-off', 'at': ago(3600)}, pushedAt=ago(pushed)),
        waiting(POD, 3600, triage='handled', triagedBy='pod', pushedAt=ago(pushed))]}
    return state, [item(HAND, src='logs'), item(POD, src='logs')]


def test_a_hand_resolved_card_that_fires_again_inside_the_quiet_window_comes_back_untriaged_without_a_push():
    # given
    state, found = handled_reopen(600)

    # when
    events, push, _ = merge(state, found, {'logs': True}, NOW)
    by = {a['key']: a for a in state['anomalies']}

    # then
    assert [e['ev'] for e in events] == ['reopened', 'reopened']
    assert {k: (a['status'], a['triage'], a['triagedBy']) for k, a in by.items()} == {HAND: ('open', None, None), POD: ('open', None, None)}
    assert (by[HAND]['note']['text'], by[HAND]['ticket']) == ('one-off', 'OPS-1')
    assert push == []


def test_a_hand_resolved_card_that_fires_again_after_the_quiet_window_pushes_at_the_same_level():
    # given
    state, found = handled_reopen(3 * 3600)

    # when
    _, push, _ = merge(state, found, {'logs': True}, NOW)

    # then
    assert [a['key'] for a in push] == [HAND, POD]
    assert [a['triage'] for a in state['anomalies']] == [None, None]


def test_a_card_in_progress_that_fires_again_keeps_its_ticket_and_pushes_only_when_it_doubles():
    # given
    state = {'initialized': True, 'muted': [], 'history': [], 'anomalies': [
        waiting('log|a-service|ERROR|x', 3600, triage='progress', ticket='OPS-1', owner='Oscar Green', now=10, pushedAt=ago(3 * 3600)),
        waiting('log|b-service|ERROR|x', 3600, triage='progress', ticket='OPS-2', now=10, pushedAt=ago(3 * 3600))]}
    found = [item('log|a-service|ERROR|x', src='logs', now=10), item('log|b-service|ERROR|x', src='logs', now=30)]

    # when
    events, push, _ = merge(state, found, {'logs': True}, NOW)
    by = {a['key']: a for a in state['anomalies']}

    # then
    assert [e['ev'] for e in events] == ['reopened', 'reopened']
    assert (by['log|a-service|ERROR|x']['triage'], by['log|a-service|ERROR|x']['ticket'], by['log|a-service|ERROR|x']['owner']) == ('progress', 'OPS-1', 'Oscar Green')
    assert (by['log|b-service|ERROR|x']['triage'], by['log|b-service|ERROR|x']['ticket']) == ('progress', 'OPS-2')
    assert [a['key'] for a in push] == ['log|b-service|ERROR|x']


def test_a_pod_triaged_card_that_fires_again_goes_back_to_open_only_when_it_pushes():
    # given
    state = {'initialized': True, 'muted': [], 'history': [], 'anomalies': [
        waiting('log|a-service|ERROR|x', 3600, triage='progress', triagedBy='pod', ticket='OPS-1', now=10, pushedAt=ago(3 * 3600)),
        waiting('log|b-service|ERROR|x', 3600, triage='progress', triagedBy='pod', ticket='OPS-2', now=10, pushedAt=ago(3 * 3600)),
        waiting('log|c-service|ERROR|x', 3600, triage='dived', triagedBy='pod', now=10, pushedAt=ago(3 * 3600)),
        waiting('log|d-service|ERROR|x', 3600, triage='dived', triagedBy='pod', now=10, pushedAt=ago(600))]}
    found = [item('log|a-service|ERROR|x', src='logs', now=10), item('log|b-service|ERROR|x', src='logs', now=30),
             item('log|c-service|ERROR|x', src='logs', now=10), item('log|d-service|ERROR|x', src='logs', now=10)]

    # when
    _, push, _ = merge(state, found, {'logs': True}, NOW)
    by = {a['key']: a for a in state['anomalies']}

    # then
    assert {k.split('|')[1]: (a['triage'], a['triagedBy'], a.get('ticket')) for k, a in by.items()} == {
        'a-service': ('progress', 'pod', 'OPS-1'), 'b-service': (None, None, 'OPS-2'), 'c-service': (None, None, None), 'd-service': ('dived', 'pod', None)}
    assert [a['key'] for a in push] == ['log|b-service|ERROR|x', 'log|c-service|ERROR|x']


def test_a_hand_resolved_card_under_a_mute_or_known_noise_that_fires_again_stays_muted_and_silent():
    # given
    state = {'initialized': True, 'history': [], 'muted': [
        {'pattern': 'log|a-service|*', 'until': iso(NOW + 3600), 'reason': 'muted for 24 h', 'kind': 'mute', 'at': ago(600)},
        {'pattern': 'log|b-service|*', 'until': None, 'reason': 'known noise', 'kind': 'known', 'at': ago(600)}], 'anomalies': [
        waiting('log|a-service|ERROR|x', 3600, triage='handled', muted='muted for 24 h', pushedAt=ago(3 * 3600)),
        waiting('log|b-service|ERROR|x', 3600, triage='handled', muted='known noise', pushedAt=ago(3 * 3600))]}
    found = [item('log|a-service|ERROR|x', src='logs'), item('log|b-service|ERROR|x', src='logs')]

    # when
    events, push, _ = merge(state, found, {'logs': True}, NOW)
    by = {a['key']: a for a in state['anomalies']}

    # then
    assert {k: (a['status'], a['muted'], a['triage']) for k, a in by.items()} == {
        'log|a-service|ERROR|x': ('open', 'muted for 24 h', None), 'log|b-service|ERROR|x': ('open', 'known noise', None)}
    assert [m['pattern'] for m in state['muted']] == ['log|a-service|*', 'log|b-service|*']
    assert [(e['ev'], e['muted']) for e in events] == [('reopened', True), ('reopened', True)]
    assert push == []


def test_the_server_passes_auto_resolve_with_the_configured_hours(store: Store):
    # given
    seeded(store, waiting(CART))

    # when
    with TestClient(create_app(dataclasses.replace(settings(), auto_resolve_hours=0), store)) as c:
        c.app.state.server_apply([], NOW, None)
    off = store.anomaly(CART).get('triage')
    with TestClient(create_app(settings(), store)) as c:
        c.app.state.server_apply([], NOW, None)
    on = store.anomaly(CART)

    # then
    assert off is None
    assert (on['triage'], on['triagedBy']) == ('handled', 'auto')


def test_numbered_siblings_of_one_check_join_the_oldest_open_one_once():
    # given
    from skopax.engine import group_siblings
    es = 'pvc-disk|cluster-logging/elasticsearch-data-elasticsearch-logging-es-elasticsearch-logging-{}|disk'
    pvc = 'elasticsearch-data-elasticsearch-logging-es-elasticsearch-logging-{}'
    old_post = {'ts': '1', 'at': '2026-11-06T10:52:36Z'}
    st = {'anomalies': [
        {'key': es.format(2), 'svc': pvc.format(2), 'src': 'capacity', 'ns': 'cluster-logging', 'sev': 'warn', 'status': 'open', 'firstSeen': '2026-11-07T17:45:38Z', 'triage': 'dived', 'slackPost': old_post},
        {'key': es.format(0), 'svc': pvc.format(0), 'src': 'capacity', 'ns': 'cluster-logging', 'sev': 'warn', 'status': 'open', 'firstSeen': '2026-11-07T23:15:48Z', 'triage': 'dived', 'slackPost': old_post},
        {'key': es.format(1), 'svc': pvc.format(1), 'src': 'capacity', 'ns': 'cluster-logging', 'sev': 'warn', 'status': 'open', 'firstSeen': '2026-11-08T01:00:00Z', 'slackPost': {'ts': '2', 'at': '2026-11-08T01:30:00Z'}},
        {'key': 'pvc-disk|other-ns/data-kafka-0|disk', 'svc': 'data-kafka-0', 'src': 'capacity', 'ns': 'other-ns', 'sev': 'warn', 'status': 'open', 'firstSeen': '2026-11-08T01:00:00Z'},
        {'key': 'pvc-disk|kafka/data-kafka-0|disk', 'svc': 'data-kafka-0', 'src': 'capacity', 'ns': 'kafka', 'sev': 'warn', 'status': 'open', 'firstSeen': '2026-11-08T01:00:00Z'},
        {'key': 'pvc-disk|kafka/data-kafka-1|disk', 'svc': 'data-kafka-1', 'src': 'capacity', 'ns': 'kafka', 'sev': 'warn', 'status': 'open', 'firstSeen': '2026-11-08T02:00:00Z', 'triage': 'progress'},
        {'key': 'alert|KubePodNotReady|namespace=analytics', 'svc': '-', 'src': 'grafana', 'ns': 'analytics', 'sev': 'warn', 'status': 'open'},
    ]}

    # when
    first = group_siblings(st)
    again = group_siblings(st)

    # then
    assert first == [(es.format(0), es.format(2))]
    assert again == []
    assert st['anomalies'][1]['parent'] == es.format(2)
    assert [a['key'] for a in st['anomalies'] if a.get('parent')] == [es.format(0)], 'posted this firing, another namespace, in progress and a service-less alert stay alone'


def test_a_grafana_error_alert_joins_the_worst_native_card_of_its_namespace_once():
    # given
    from skopax.engine import group_alert_twins
    twins = ('AppHighErrorLogCount', 'AppHighHttp5xxRatio')
    st = {'anomalies': [
        {'key': 'err|members/email-service', 'src': 'prom', 'ns': 'members', 'sev': 'warn', 'status': 'open', 'firstSeen': '2026-11-07T20:00:00Z'},
        {'key': 'kafka-consume-err|members/email-service|in', 'src': 'kafka', 'ns': 'members', 'sev': 'crit', 'status': 'open', 'firstSeen': '2026-11-07T20:14:00Z'},
        {'key': 'alert|AppHighErrorLogCount|namespace=members', 'src': 'alert', 'ns': 'members', 'sev': 'warn', 'status': 'open'},
        {'key': 'alert|AppHighHttp5xxRatio|namespace=identity', 'src': 'alert', 'ns': 'identity', 'sev': 'warn', 'status': 'open'},
        {'key': 'alert|KubePodNotReady|namespace=members', 'src': 'alert', 'ns': 'members', 'sev': 'warn', 'status': 'open'},
        {'key': 'alert|AppHighHttp5xxRatio|namespace=members', 'src': 'alert', 'ns': 'members', 'sev': 'warn', 'status': 'open', 'slackPost': {'ts': '1'}},
    ]}

    # when
    first = group_alert_twins(st, twins)
    again = group_alert_twins(st, twins)

    # then
    assert first == [('alert|AppHighErrorLogCount|namespace=members', 'kafka-consume-err|members/email-service|in')]
    assert again == []
    assert st['anomalies'][2]['parent'] == 'kafka-consume-err|members/email-service|in'
    assert all('parent' not in a for a in st['anomalies'][3:])


DUP = 'log|shop-web|WARN|getOrderTotalV3'
ORIG = 'log|shop-web|WARN|calculateShippingCost'


def duplicate(**extra) -> dict:
    return waiting(DUP, 3600, num=182, triage='handled', resolution={'kind': 'duplicate', 'ref': '#61', 'by': 'pod', 'via': 'system'}, pushedAt=ago(3 * 3600), **extra)


def test_a_duplicate_firing_with_its_known_noise_original_gets_a_24_hour_mute_and_one_firing_alone_reopens():
    # given
    known = [{'pattern': ORIG, 'until': None, 'reason': 'known noise', 'kind': 'known', 'at': ago(600)}]
    together = {'initialized': True, 'history': [], 'muted': list(known), 'anomalies': [waiting(ORIG, 3600, num=61, muted='known noise'), duplicate()]}
    alone = {'initialized': True, 'history': [], 'muted': list(known), 'anomalies': [waiting(ORIG, 3600, num=61, muted='known noise'), duplicate()]}

    # when
    events, push, _ = merge(together, [item(ORIG, src='logs'), item(DUP, src='logs')], {'logs': True}, NOW)
    merge(alone, [item(DUP, src='logs')], {'logs': True}, NOW)
    dup = next(a for a in together['anomalies'] if a['key'] == DUP)
    solo = next(a for a in alone['anomalies'] if a['key'] == DUP)

    # then
    assert (dup['status'], dup['triage'], dup['resolution']['kind'], dup['muted']) == ('open', 'handled', 'duplicate', 'follows #61: known noise')
    assert [m for m in together['muted'] if m['pattern'] == DUP] == [{'pattern': DUP, 'exact': True, 'until': iso(NOW + 24 * 3600), 'reason': 'follows #61: known noise', 'kind': 'mute', 'at': iso(NOW)}], 'never muted for good, whatever the original has'
    assert DUP not in [a['key'] for a in push]
    assert (solo['triage'], solo['muted'], [m['pattern'] for m in alone['muted']]) == (None, None, [ORIG]), 'firing alone it may be a real outage, so it reopens on its own'


def test_a_duplicate_folds_back_under_its_original_while_the_original_fires_and_takes_its_ticket_when_it_is_in_progress():
    # given
    firing = {'initialized': True, 'history': [], 'muted': [], 'anomalies': [stored(ORIG, num=61, src='logs'), duplicate()]}
    taken = {'initialized': True, 'history': [], 'muted': [], 'anomalies': [waiting(ORIG, 3600, num=61, triage='progress', ticket='OPS-146', owner='Dora Lane'), duplicate()]}

    # when
    _, push, _ = merge(firing, [item(ORIG, src='logs'), item(DUP, src='logs')], {'logs': True}, NOW)
    _, push_taken, _ = merge(taken, [item(DUP, src='logs')], {'logs': True}, NOW)
    merged = next(a for a in firing['anomalies'] if a['key'] == DUP)
    progress = next(a for a in taken['anomalies'] if a['key'] == DUP)

    # then
    assert (merged['parent'], merged['triage']) == (ORIG, None), 'merged under the firing original, like the investigator grouped it'
    assert DUP not in [a['key'] for a in push]
    assert (progress['triage'], progress['ticket'], progress['owner'], progress.get('parent')) == ('progress', 'OPS-146', 'Dora Lane', None)
    assert push_taken == []


def test_a_duplicate_of_an_original_that_is_resolved_and_quiet_reopens_on_its_own_as_before():
    # given
    state = {'initialized': True, 'history': [], 'muted': [], 'anomalies': [waiting(ORIG, 3600, num=61, triage='handled'), duplicate()]}

    # when
    merge(state, [item(DUP, src='logs')], {'logs': True}, NOW)
    dup = next(a for a in state['anomalies'] if a['key'] == DUP)

    # then
    assert (dup['status'], dup['triage'], dup.get('parent'), dup['muted']) == ('open', None, None, None)


def test_applied_says_whether_it_was_the_first_pass(store: Store):
    # given
    result = PassResult('cf', True, 'ok', [item('a|svc1', sev='info'), item('b|svc1', sev='crit')])

    # when
    first = apply(store, [result], NOW)
    later = apply(store, [PassResult('cf', True, 'ok', [item('a|svc1', sev='info'), item('b|svc1', sev='crit'), item('c|svc2', sev='info')])], NOW + 60)

    # then
    assert first.first is True and first.push == []
    assert later.first is False and [e['key'] for e in later.events if e['ev'] == 'new'] == ['c|svc2']
