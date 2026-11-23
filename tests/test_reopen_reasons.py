from skopax import notices as notices_module
from skopax.engine import merge
from skopax.messages import msg
from skopax.slack import head_text
from tests.test_engine import NOW, ago, item, stored

KEY = 'log|checkout-service|ERROR|boom'
H = 3600


def closed(kind: str, by: str = 'alice@example.com', ref: str | None = None, sev: str = 'warn', **extra) -> dict:
    resolution = {'kind': kind, 'by': by, 'at': ago(5 * H)} | ({'ref': ref} if ref else {})
    return stored(KEY, status='cleared', src='logs', sev=sev, clearedAt=ago(5 * H), triage='handled', triagedBy=by, triagedAt=ago(5 * H), resolution=resolution,
                  pushedAt=ago(6 * H), pushedSev=sev, **extra)


def fire(card: dict, sev: str = 'warn', now: float = 1) -> tuple[list[dict], list[dict], dict]:
    state = {'initialized': True, 'muted': [], 'history': [], 'anomalies': [card]}
    events, push, _ = merge(state, [item(KEY, src='logs', sev=sev, now=now)], {'logs': True}, NOW)
    return events, push, state['anomalies'][0]


def test_a_fixed_card_that_fires_again_is_a_regression_pushed_as_high_and_assigned_back():
    # given
    card = closed('fixed', ref='OPS-123')

    # when
    events, push, after = fire(card, sev='warn')

    # then
    assert [(e['ev'], e['sev'], e.get('cameBack')) for e in events] == [('reopened', 'high', 'OPS-123')]
    assert [a['key'] for a in push] == [KEY], 'a regression is pushed even without doubling'
    assert (after['sev'], after['triage'], after['resolution']) == ('high', None, None)
    assert after['previousResolution'] == {'kind': 'fixed', 'ref': 'OPS-123', 'by': 'alice@example.com', 'at': ago(5 * H)}
    assert after['cameBack']['ref'] == 'OPS-123' and after['assignBack'] == 'alice@example.com' and not after.get('assigned'), 'the pass asks for the assign; assign.py does it'


def test_a_regression_keeps_critical_and_a_ticket_sweep_fix_is_not_assigned_to_a_person():
    # when
    _, _, crit = fire(closed('fixed', sev='crit'), sev='crit')
    _, push, swept = fire(closed('fixed', by='jira-links', ref='OPS-9'), sev='info')

    # then
    assert crit['sev'] == 'crit'
    assert (swept['sev'], swept.get('assignBack'), [a['key'] for a in push]) == ('high', None, [KEY])


def test_a_no_action_card_that_fires_again_at_the_same_level_reopens_quietly_and_a_higher_one_is_pushed():
    # when
    events, same_push, same = fire(closed('handled'), sev='warn')
    _, higher_push, higher = fire(closed('handled'), sev='crit')

    # then
    assert [e['ev'] for e in events] == ['reopened'] and 'cameBack' not in events[0]
    assert (same_push, same['sev'], same['previousResolution']['kind'], bool(same.get('cameBack'))) == ([], 'warn', 'handled', False)
    assert [a['key'] for a in higher_push] == [KEY] and higher['sev'] == 'crit'


def test_duplicates_known_noise_and_auto_resolved_cards_keep_their_reopen_rules():
    # given
    auto = closed('handled', by='auto')
    muted = closed('fixed')

    # when
    _, auto_push, auto_after = fire(auto, sev='warn')
    state = {'initialized': True, 'history': [], 'anomalies': [muted], 'muted': [{'pattern': KEY, 'until': None, 'reason': 'known noise', 'kind': 'known', 'at': ago(H)}]}
    _, muted_push, _ = merge(state, [item(KEY, src='logs', sev='warn')], {'logs': True}, NOW)
    _, _, dup = fire(closed('duplicate', ref='#99'), sev='warn')

    # then
    assert [a['key'] for a in auto_push] == [KEY] and not auto_after.get('previousResolution'), 'the server close reopens by the normal rules'
    assert muted_push == [] and not state['anomalies'][0].get('previousResolution')
    assert not dup.get('previousResolution')


def test_the_regression_wording_reaches_the_slack_head_and_the_notice():
    # given
    _, _, after = fire(closed('fixed', ref='OPS-123'))
    card = {**after, 'num': 14}

    # when
    head = head_text(card, {'kind': 'reopened', 'by': 'server'})
    plain = head_text({**card, 'cameBack': {'ref': None}}, {'kind': 'reopened', 'by': 'server'})

    # then
    assert msg('slack.head.came_back', card='#14', ref='OPS-123') in head
    assert msg('slack.head.came_back_plain', card='#14') in plain
    assert notices_module.came_back_text('OPS-123') == 'Came back after fix OPS-123' and notices_module.came_back_text('') == 'Came back after its fix'


def test_a_card_the_investigator_closed_reopens_by_the_normal_rules():
    # when
    _, push, after = fire(closed('fixed', by='pod'), sev='warn')

    # then
    assert [a['key'] for a in push] == [KEY] and not after.get('previousResolution') and not after.get('cameBack') and after['sev'] == 'warn'


def test_a_fresh_at_once_slack_post_of_a_regression_says_it_came_back():
    # given
    _, _, after = fire(closed('fixed', ref='OPS-123'))
    card = {**after, 'num': 14}

    # when
    fresh = head_text(card, {'kind': 'opened', 'by': 'server'})

    # then
    assert msg('slack.head.came_back', card='#14', ref='OPS-123') in fresh


def test_a_teams_post_of_a_regression_says_it_came_back_after_its_fix(store):
    # given
    from skopax.plugins.teams.cards import card_rows
    from skopax.routes import Routes
    from tests.conftest import settings
    from tests.teams_kit import Transport, runtime_of
    _, _, after = fire(closed('fixed', ref='OPS-123'))
    routes = Routes(store, runtime_of(store, Transport()), settings(), attribution=lambda: None)

    # when
    message = routes.cards_message([{**after, 'num': 14}], (), None)
    texts = [b.get('text') for b in card_rows(message)]

    # then
    assert message['cards'][0]['cameBack'] == 'Came back after fix OPS-123'
    assert any('Came back after fix OPS-123' in str(t) for t in texts)


def test_the_next_resolve_drops_the_came_back_marks(store):
    # given
    _, _, after = fire(closed('fixed', ref='OPS-123'))
    store.save({'initialized': True, 'muted': [], 'history': [], 'anomalies': [after]}, None)

    # when
    store.triage(KEY, 'handled', by='alice@example.com', resolution={'kind': 'handled'})
    card = store.anomaly(KEY)

    # then
    assert 'cameBack' not in card and 'previousResolution' not in card


def test_an_auto_resolved_regression_that_fires_again_no_longer_says_it_came_back():
    # given
    _, _, after = fire(closed('fixed', ref='OPS-123'))
    auto = {**after, 'status': 'cleared', 'clearedAt': ago(5 * H), 'triage': 'handled', 'triagedBy': 'auto', 'resolution': {'kind': 'handled', 'by': 'auto', 'at': ago(5 * H)}}

    # when
    _, _, again = fire(auto)

    # then
    assert not again.get('cameBack') and not again.get('previousResolution')


def test_the_server_pass_assigns_a_regression_back_through_the_assigner(store):
    # given
    from fastapi.testclient import TestClient
    from skopax.app import create_app
    from skopax.contract import PassResult
    from tests.conftest import settings
    store.save({'initialized': True, 'muted': [], 'history': [], 'anomalies': [closed('fixed', ref='OPS-123', assigned={'by': 'bob@example.com', 'at': ago(9 * H), 'email': 'bob@example.com'}, owner='Bob')]}, None)

    # when
    with TestClient(create_app(settings(), store)) as c:
        c.app.state.server_apply([PassResult('logs', True, 'ok', [item(KEY, src='logs', sev='warn')], {}, ('logs',))], NOW, None)
    card = store.anomaly(KEY)
    lines = [d['text'] for d in store.activity.find({'kind': 'assign'})]

    # then
    assert card['assigned']['email'] == 'alice@example.com' and 'assignBack' not in card
    assert lines == [f'assigned to {card["owner"]} \u00b7 {KEY}']


def test_a_regression_is_pushed_even_when_its_service_was_pushed_minutes_ago():
    # given
    other = stored('err|checkout-service', src='logs', sev='crit', pushedAt=ago(300), pushedSev='crit')
    card = closed('fixed', ref='OPS-123')
    state = {'initialized': True, 'muted': [], 'history': [], 'anomalies': [card, other]}

    # when
    _, push, _ = merge(state, [item(KEY, src='logs', sev='warn'), item('err|checkout-service', src='logs', sev='crit')], {'logs': True}, NOW)

    # then
    assert KEY in [a['key'] for a in push]
