import logging

import pytest
from fastapi.testclient import TestClient

from tests.conftest import AUTH, anomaly, settings, state
from skopax import information, summary, tagreport
from skopax.app import create_app
from skopax.autosend import PostNow, fresh
from skopax.config import load_settings
from skopax.engine import Applied, PassResult, anomaly as found_item, apply, iso, parse_iso
from skopax.messages import msg
from skopax.routes import Route
from skopax.store import Store

NOW = parse_iso('2026-11-20T10:00:00Z')
HOUR = 3600
INFO = 'log|shop|INFO|x'
WARN = 'log|checkout-service|WARN|x'


def info_item(key: str = INFO, **extra) -> dict:
    return anomaly(key, **({'sev': 'info', 'svc': key.split('|')[1], 'firstSeen': '2026-11-20T08:00:00Z', 'lastSeen': '2026-11-20T09:55:00Z', 'src': 'log', 'what': 'INFO lines went up'} | extra))


def seen(store: Store, *cards: dict) -> None:
    store.save({**state(*cards), 'initialized': True}, None)


def signal(svc: str, sev: str, now: float) -> dict:
    return found_item('prom', 'lat', 'shop', svc, '', sev, now, 1.0, 1.0, 'ms', 'Latency', f'{svc} latency is up')


def passed(*found: dict) -> list[PassResult]:
    return [PassResult('prom', True, 'ok', list(found), {}, ('prom',))]


@pytest.mark.parametrize('extra, card', [
    ({}, False),
    ({'owner': 'alice@example.com'}, True),
    ({'assigned': {'name': 'Alice', 'email': 'alice@example.com'}}, True),
    ({'ticket': 'OPS-123'}, True),
    ({'triage': 'progress'}, True),
    ({'triage': 'handled'}, True),
    ({'muted': 'noise'}, False),
])
def test_an_info_record_is_a_card_only_while_someone_works_on_it(extra, card):
    # when
    got = information.is_card(info_item(**extra))

    # then
    assert got is card
    assert information.is_card(anomaly(WARN, sev='warn')) and information.is_card(anomaly(WARN, sev='crit'))


def test_the_page_state_carries_cards_only_and_lists_information_apart(store: Store):
    # given
    owned = info_item('log|cart|INFO|owned', owner='alice@example.com')
    store.save({**state(anomaly(WARN, sev='warn'), info_item(), owned), 'initialized': True,
                'history': [{'t': '2026-11-20T08:00:00Z', 'key': INFO, 'ev': 'new', 'sev': 'info', 'text': 'x'},
                            {'t': '2026-11-20T08:00:00Z', 'key': WARN, 'ev': 'new', 'sev': 'warn', 'text': 'y'},
                            {'t': '2026-11-20T08:00:00Z', 'key': 'log|cart|INFO|owned', 'ev': 'new', 'sev': 'info', 'text': 'z'}]}, None)

    # when
    with TestClient(create_app(settings(), store)) as c:
        got = c.get('/api/state').json()
        page = c.get('/').text

    # then
    assert [a['key'] for a in got['anomalies']] == [WARN, 'log|cart|INFO|owned'], 'an owned info record stays a card'
    assert [r['key'] for r in got['information']] == [INFO]
    assert [e['key'] for e in got['history']] == [WARN, 'log|cart|INFO|owned']
    assert '"information": [{' in page and '"key": "log|shop|INFO|x", "sev"' not in page


def test_information_rows_put_still_firing_first_newest_first_and_drop_ended_items_after_a_day():
    # given
    items = [info_item('log|a|INFO|old', firstSeen='2026-11-19T08:00:00Z'),
             info_item('log|b|INFO|new', firstSeen='2026-11-20T09:00:00Z'),
             info_item('log|c|INFO|ended', status='cleared', clearedAt=iso(NOW - 2 * HOUR)),
             info_item('log|d|INFO|ended-later', status='cleared', clearedAt=iso(NOW - HOUR)),
             info_item('log|e|INFO|gone', status='cleared', clearedAt=iso(NOW - 25 * HOUR)),
             anomaly(WARN, sev='warn')]

    # when
    rows = information.rows(items, NOW)

    # then
    assert [r['key'] for r in rows] == ['log|b|INFO|new', 'log|a|INFO|old', 'log|d|INFO|ended-later', 'log|c|INFO|ended']
    first = rows[0]
    assert (first['svc'], first['label'], first['text'], first['src'], first['firing']) == ('b', 'ERROR pattern', 'INFO lines went up', 'log', True)
    assert rows[2]['firing'] is False and rows[2]['endedAt'] == iso(NOW - HOUR)
    assert all('num' not in r for r in rows), 'information has no numbers'


def test_a_rise_to_warning_makes_a_card_with_the_next_number_and_a_drop_to_info_leaves_the_board(store: Store):
    # given
    seen(store)

    # when
    apply(store, passed(signal('cart', 'warn', 5), signal('shop', 'info', 5)), NOW)
    after_first = {a['key']: a.get('num') for a in store.load()['anomalies']}
    apply(store, passed(signal('cart', 'warn', 5), signal('shop', 'warn', 5)), NOW + 600)
    after_rise = {a['key']: a.get('num') for a in store.load()['anomalies']}
    apply(store, passed(signal('cart', 'info', 5), signal('shop', 'warn', 5)), NOW + 1200)
    with TestClient(create_app(settings(), store)) as c:
        page = c.get('/api/state').json()
        card_api = c.get('/api/cards/1')

    # then
    assert after_first == {'lat|shop/cart': 1, 'lat|shop/shop': None}, 'an info record takes no number'
    assert after_rise == {'lat|shop/cart': 1, 'lat|shop/shop': 2}, 'the rise takes the next number, no gap'
    assert store.anomaly('lat|shop/cart')['num'] == 1, 'a card that drops to info keeps its number'
    assert [a['key'] for a in page['anomalies']] == ['lat|shop/shop']
    assert [r['key'] for r in page['information']] == ['lat|shop/cart'] and 'num' not in page['information'][0]
    assert card_api.status_code == 404, 'the card page of a record that is information now is not a card'


def test_information_is_never_auto_closed_and_the_old_setting_logs_its_deprecation(store: Store, caplog):
    # given
    from skopax import config
    old = info_item(firstSeen='2026-10-01T08:00:00Z', lastSeen=iso(NOW - 60))
    seen(store, old)
    config.TOLD.discard(config.INFO_DAYS_TOLD)
    caplog.set_level(logging.WARNING, 'skopax')

    # when
    apply(store, [], NOW)
    loaded = load_settings(lambda: {'core.info-auto-close-days': '5'})
    quiet = load_settings(lambda: {})

    # then
    assert store.anomaly(INFO).get('triage') is None
    assert loaded.info_auto_close_days == 5, 'still readable'
    assert caplog.text.count('core.info-auto-close-days is deprecated') == 1
    assert quiet.info_auto_close_days == 3


@pytest.mark.parametrize('body', [
    {'type': 'take'}, {'type': 'resolve'}, {'type': 'mute', 'hours': 1}, {'type': 'known'}, {'type': 'dive'},
    {'type': 'link', 'ticket': 'OPS-123'}, {'type': 'ticket', 'title': 't'}, {'type': 'reopen'},
])
def test_card_actions_refuse_an_information_item(store: Store, body):
    # given
    seen(store, info_item())

    # when
    with TestClient(create_app(settings(), store)) as c:
        res = c.post('/api/actions', json=body | {'key': INFO}, headers=AUTH)

    # then
    assert res.status_code == 409
    assert res.json()['detail'] == msg('cards.information_item', key=INFO)
    assert 'Unmute' in res.json()['detail']
    assert store.anomaly(INFO).get('triage') is None


def test_information_rows_carry_the_muted_flag_only_when_muted():
    # given
    items = [info_item('log|a|INFO|loud'), info_item('log|b|INFO|quiet', muted='noise')]

    # when
    rows = {r['key']: r for r in information.rows(items, NOW)}

    # then
    assert 'muted' not in rows['log|a|INFO|loud']
    assert rows['log|b|INFO|quiet']['muted'] is True


def test_a_muted_information_item_can_be_unmuted_and_nothing_else_opens_up(store: Store):
    # given
    seen(store, info_item())
    store.mute(INFO, 'noise', None, 'known')

    # when
    with TestClient(create_app(settings(), store)) as c:
        before = c.get('/api/state').json()['information'][0]
        res = c.post('/api/actions', json={'type': 'unmute', 'key': INFO}, headers=AUTH)
        refused = c.post('/api/actions', json={'type': 'known', 'key': INFO}, headers=AUTH)
        after = c.get('/api/state').json()['information'][0]

    # then
    assert before['muted'] is True
    assert res.status_code == 201
    assert not store.anomaly(INFO).get('muted')
    assert 'muted' not in after
    assert refused.status_code == 409


def test_a_card_that_became_information_keeps_no_closed_row_in_the_history_list(store: Store):
    # given
    was = info_item(firstSeen='2026-11-19T08:00:00Z')
    seen(store, was)
    store.card_history.insert_one({'_id': 'old-card', 'key': INFO, 'firstSeen': '2026-11-10T08:00:00Z', 'lastSeen': '2026-11-10T09:00:00Z', 'svc': 'shop', 'label': 'INFO lines', 'outcome': 'cleared'})
    store.card_history.insert_one({'_id': 'other-card', 'key': WARN, 'firstSeen': '2026-11-10T08:00:00Z', 'lastSeen': '2026-11-10T09:00:00Z', 'svc': 'checkout-service', 'label': 'WARN lines', 'outcome': 'cleared'})

    # when
    rows, total = store.closed_cards('2026-11-01T00:00:00Z', '2026-11-20T00:00:00Z')

    # then
    assert [r['key'] for r in rows] == [WARN]
    assert total == 1


def test_assign_tags_business_and_posts_refuse_an_information_item(store: Store):
    # given
    seen(store, info_item())

    # when
    with TestClient(create_app(settings(), store)) as c:
        got = [c.put('/api/assignee', json={'key': INFO, 'me': True}, headers=AUTH),
               c.post('/api/tags', json={'key': INFO, 'tag': 'developer', 'on': True}, headers=AUTH),
               c.put('/api/business', json={'key': INFO, 'text': 'Fix it'}, headers=AUTH),
               c.post('/api/slack-posts', json={'key': INFO}, headers=AUTH),
               c.put('/api/slack-drafts', json={'key': INFO, 'text': 'hi'}, headers=AUTH),
               c.post('/api/routes/send', json={'key': INFO}, headers=AUTH)]

    # then
    assert [r.status_code for r in got] == [409] * 6
    assert store.anomaly(INFO).get('assigned') is None and not store.anomaly(INFO).get('tags')


def test_move_to_information_takes_a_card_off_the_board_and_back_to_the_board_returns_it(store: Store):
    # given
    seen(store, anomaly(WARN, sev='warn', firstSeen='2026-11-20T08:00:00Z'))

    # when
    with TestClient(create_app(settings(), store)) as c:
        moved = c.post('/api/actions', json={'type': 'severity', 'key': WARN, 'sev': 'info'}, headers=AUTH)
        off = c.get('/api/state').json()
        back = c.post('/api/actions', json={'type': 'severity', 'key': WARN, 'sev': 'auto'}, headers=AUTH)
        on = c.get('/api/state').json()

    # then
    assert moved.json()['status'] == 'done' and back.json()['status'] == 'done'
    assert off['anomalies'] == []
    row = off['information'][0]
    assert row['key'] == WARN and row['moved']['by'] and row['moved']['at']
    assert [a['key'] for a in on['anomalies']] == [WARN] and on['information'] == []


def test_a_signal_driven_item_shows_no_hand_move():
    # when
    rows = information.rows([info_item(), info_item('log|b|INFO|moved', sevSet={'sev': 'info', 'by': 'alice@example.com', 'at': '2026-11-20T09:00:00Z', 'auto': 'warn'})], NOW)

    # then
    by = {r['key']: r for r in rows}
    assert 'moved' not in by[INFO]
    assert by['log|b|INFO|moved']['moved'] == {'by': 'alice@example.com', 'at': '2026-11-20T09:00:00Z'}


def test_the_tag_report_names_critical_and_warning_cards_only():
    # given
    tagged = {'tags': [{'id': 'developer', 'by': 'rule', 'at': '2026-11-20T09:00:00Z'}]}
    data = {'tags': [{'id': 'data', 'by': 'rule', 'at': '2026-11-20T09:00:00Z'}]}
    cards = [anomaly('a', sev='warn', num=1, firstSeen='2026-11-20T09:00:00Z', **tagged),
             anomaly('b', sev='info', num=2, firstSeen='2026-11-20T09:00:00Z', owner='bob@example.com', **tagged),
             anomaly('c', sev='info', firstSeen='2026-11-20T09:00:00Z', **data)]

    # when
    out = tagreport.build(cards, {'developer': ['alice@example.com'], 'data': ['bob@example.com']}, {}, lambda e: None, 'https://skopax.example.com', NOW,
                          {'developer': 'Developer', 'data': 'Data'})

    # then
    assert out is not None and 'Data' not in out['text']
    body = out['blocks'][1]['text']['text']
    assert '#1' in body and '#2' not in body and 'Info' not in body


def test_slack_never_posts_information_at_once_or_as_a_fresh_card(store: Store):
    # given
    pushed = anomaly(WARN, sev='warn', svc='checkout-service', pushedAt='2026-11-20T09:59:00Z')
    item = info_item(pushedAt='2026-11-20T09:59:00Z')
    seen(store, pushed, item)
    cards = store.load()['anomalies']
    sent = []

    # when
    news = fresh(Applied(rev=2, events=[{'key': INFO, 'ev': 'new'}], push=[pushed], cards=cards), NOW)
    posted = PostNow(store, sent.append, lambda: 'info').run(cards, cards, NOW)

    # then
    assert [a['key'] for a in news] == [WARN]
    assert posted == [WARN] and sent == [WARN]


def test_a_teams_route_never_takes_information():
    # given
    every = Route('teams/all', 'all', 'teams', 'all', frozenset(), (), None, keys=('*',), min_sev='info')
    business = Route('teams/seo', 'seo', 'teams', 'seo', frozenset({'seo'}), (), None)
    tagged = {'tags': [{'id': 'seo', 'by': 'rule', 'at': '2026-11-20T09:00:00Z'}]}

    # when
    took = [every.takes(info_item()), business.takes(info_item(**tagged)), every.takes(anomaly(WARN, sev='warn'))]

    # then
    assert took == [False, False, True]


def test_the_summary_line_leaves_information_out(store: Store):
    # when
    built = summary.build({'anomalies': [info_item()]}, store, NOW)

    # then
    assert built['headline'].startswith('All clear') and built['level'] == 'ok'


def test_mcp_lists_and_reads_cards_only(store: Store):
    # given
    from tests.test_mcp import board, call, mcp_client, token
    board(store, anomaly(WARN, sev='warn', num=1), info_item(num=2))

    # when
    with mcp_client(store) as c:
        listed, _ = call(c, token(), 'list_cards', {'filter': 'all'})
        by_number, number_failed = call(c, token(), 'get_card', {'card': 2})
        by_key, key_failed = call(c, token(), 'get_card', {'card': INFO})
        taken, take_failed = call(c, token(), 'take_card', {'card': INFO})

    # then
    assert listed.startswith('1 all card:') and '#1' in listed and INFO not in listed
    assert number_failed and key_failed and take_failed
    assert msg('cards.information_item', key=INFO) in by_number and msg('cards.information_item', key=INFO) in by_key
    assert store.anomaly(INFO).get('triage') is None


def test_a_posted_card_that_dropped_to_information_gets_no_slack_reminder():
    # given
    from skopax import reminders
    post = {'slackPost': {'at': '2026-11-20T06:00:00Z', 'ts': '1.0', 'channel': 'C1'}}
    dropped, card = info_item(**post), anomaly(WARN, sev='warn', **post)
    by_key = {a['key']: a for a in (dropped, card)}

    # when
    due = [reminders.waiting(a, by_key, {'muted': []}, NOW) for a in (dropped, card)]

    # then
    assert due == [False, True]


def test_a_slack_button_on_an_old_post_refuses_an_information_item(store: Store):
    # given
    from skopax import actions
    seen(store, info_item())

    # when
    with pytest.raises(actions.Refused) as refused:
        actions.apply(store, {'type': 'resolve', 'key': INFO, 'requestedBy': 'alice@example.com', 'source': 'slack'})
    moved = actions.apply(store, {'type': 'severity', 'key': INFO, 'sev': 'warn', 'requestedBy': 'alice@example.com'})

    # then
    assert refused.value.text == msg('cards.information_item', key=INFO)
    assert moved.startswith('severity set to Warning by hand') and store.anomaly(INFO)['sev'] == 'warn'


def test_an_ended_information_item_that_leaves_keeps_no_closed_card_row(store: Store):
    # given
    long_ago = iso(NOW - 30 * HOUR)
    seen(store, info_item(status='cleared', clearedAt=long_ago),
         anomaly(WARN, sev='warn', status='cleared', clearedAt=long_ago, firstSeen='2026-11-19T01:00:00Z', lastSeen=long_ago, triage='handled', triagedAt=long_ago, num=1))

    # when
    apply(store, [], NOW)
    rows, total = store.closed_cards('2026-11-01T00:00:00Z', '2026-12-01T00:00:00Z')

    # then
    assert store.load()['anomalies'] == []
    assert [r['key'] for r in rows] == [WARN] and total == 1
    assert store.card_history.count_documents({'key': INFO}) == 0


def test_a_state_written_back_does_not_store_the_information_list(store: Store):
    # given
    seen(store, anomaly(WARN, sev='warn'))

    # when
    with TestClient(create_app(settings(), store)) as c:
        read = c.get('/api/state').json()
        saved = c.put('/api/state', json=read | {'information': [{'key': INFO}]}, headers={**AUTH, 'X-Base-Rev': str(read['rev'])})

    # then
    assert saved.status_code == 200
    assert 'information' not in store.load()


def test_the_per_kind_and_parent_rules_never_close_information_into_a_card():
    # given
    from skopax.engine import auto_resolve_kinds, follow_parents
    windows = (({'wingify-verdict'}, 'ab-verdict', 24),)
    verdict = info_item('ab-verdict|x', src='wingify-verdict', sig='ab-verdict', firstSeen=iso(NOW - 48 * HOUR))
    parent = anomaly('log|dad|ERROR|x', sev='warn', status='cleared', triage='handled')
    kid = info_item('log|kid|INFO|x', status='cleared', parent='log|dad|ERROR|x')
    state = {'anomalies': [verdict, parent, kid]}

    # when
    closed = auto_resolve_kinds(state, NOW, windows) + follow_parents(state, NOW)

    # then
    assert closed == []
    assert information.is_item(verdict) and information.is_item(kid)


def test_a_state_read_and_written_back_loses_no_information(store: Store):
    # given
    first = '2026-11-20T08:00:00Z'
    seen(store, anomaly(WARN, sev='warn', firstSeen=first, lastSeen=iso(NOW - 60), src='log', pushedAt=first, pushedSev='warn'), info_item(lastSeen=iso(NOW - 60)))
    with TestClient(create_app(settings(), store)) as c:
        c.post('/api/actions', json={'type': 'severity', 'key': WARN, 'sev': 'info'}, headers=AUTH)
        store.save({**store.load(), 'history': [{'t': first, 'key': INFO, 'ev': 'new', 'sev': 'info', 'text': 'x'}]}, store.rev()['rev'])
        read = c.get('/api/state').json()

        # when
        saved = c.put('/api/state', json=read, headers={**AUTH, 'X-Base-Rev': str(read['rev'])})
        applied = apply(store, [], NOW)

    # then
    kept = {a['key']: a for a in store.load()['anomalies']}
    assert saved.status_code == 200 and set(kept) == {WARN, INFO}
    assert kept[WARN]['sevSet']['sev'] == 'info' and kept[WARN]['firstSeen'] == first and kept[INFO]['firstSeen'] == '2026-11-20T08:00:00Z'
    assert [e['key'] for e in store.load()['history']] == [INFO]
    assert store.card_history.count_documents({}) == 0 and applied.push == []


def test_an_info_card_the_old_info_rule_auto_closed_is_information_after_the_upgrade():
    # given
    closed = info_item(triage='handled', triagedBy='auto', autoResolvedAt='2026-11-19T08:00:00Z', resolution={'kind': 'handled', 'by': 'auto', 'via': 'skopax', 'at': '2026-11-19T08:00:00Z'})
    by_pod = info_item(triage='handled', triagedBy='pod')
    by_hand = info_item(triage='handled', triagedBy='alice@example.com')

    # then
    assert information.is_item(closed) and information.is_item(by_pod) and information.is_card(by_hand)


def test_a_card_whose_parent_is_information_stands_alone_everywhere(store: Store):
    # given
    from skopax.engine import group_alert_twins
    from skopax.investigator import select
    host = info_item('log|ns1|INFO|x', ns='ns1', src='log', firstSeen=iso(NOW - HOUR))
    twin = anomaly('alert|KubePodCrash|ns1', sev='crit', src='alert', ns='ns1', firstSeen=iso(NOW - 600), parent='log|ns1|INFO|x')
    fresh_twin = anomaly('alert|KubePodCrash|ns1-b', sev='crit', src='alert', ns='ns1', firstSeen=iso(NOW - 600))
    seen(store, host, twin)

    # when
    grouped = group_alert_twins({'anomalies': [host, fresh_twin]}, ('KubePodCrash',))
    picked = select.eligible({'anomalies': [host, twin], 'history': [{'t': iso(NOW - 600), 'key': twin['key'], 'ev': 'new'}]}, 'live', [], NOW, 5)
    with TestClient(create_app(settings(), store)) as c:
        page = c.get('/api/state').json()
    pushed = apply(store, passed(signal('shop', 'info', 5), signal('cart', 'crit', 5) | {'parent': 'lat|shop/shop'}), NOW).push

    # then
    assert grouped == [] and 'parent' not in fresh_twin
    assert [a['key'] for a in picked] == [twin['key']]
    assert [a['key'] for a in page['anomalies']] == [twin['key']] and 'parent' not in page['anomalies'][0]
    assert 'lat|shop/cart' in [a['key'] for a in pushed]
    tagged = {'tags': [{'id': 'seo', 'by': 'rule', 'at': iso(NOW)}], 'business': {'text': 'Fix it.', 'kind': 'info', 'by': 'rule', 'at': iso(NOW)}}
    route = Route('teams/seo', 'seo', 'teams', 'seo', frozenset({'seo'}), (), None)
    kid, ended = twin | tagged, twin | tagged | {'status': 'cleared', 'clearedAt': iso(NOW - 600)}
    under_card = kid | {'parent': WARN}
    by_key = {a['key']: a for a in (host, kid, anomaly(WARN, sev='warn'))}
    assert route.due(kid, by_key) and not route.due(under_card, by_key)
    assert route.fixed_due(ended, NOW, by_key) == route.fixed_due(ended | {'parent': None}, NOW, by_key)


def test_the_investigator_drops_a_queued_investigate_on_information(store: Store, caplog):
    # given
    from tests.conftest import ready_app
    seen(store, info_item(), anomaly(WARN, sev='warn'))
    with TestClient(ready_app(settings(), store)) as c:
        store.add_action({'type': 'dive', 'key': INFO, 'target': INFO, 'requestedBy': 'alice@example.com'})
        caplog.set_level(logging.INFO, 'skopax')

        # when
        work = c.app.state.investigator.pick_action()

    # then
    assert work is None
    [act] = store.list_actions(None, 10)
    assert act['status'] == 'failed' and act['result'] == msg('cards.information_item', key=INFO)
    assert 'is information' in caplog.text


def test_holds_thread_updates_and_asks_refuse_an_information_item(store: Store):
    # given
    seen(store, info_item())

    # when
    with TestClient(create_app(settings(), store)) as c:
        got = [c.post('/api/slack-holds', json={'key': INFO}, headers=AUTH),
               c.post('/api/slack-updates', json={'key': INFO, 'text': 'hi'}, headers=AUTH),
               c.post('/api/actions', json={'type': 'ask', 'key': INFO, 'text': 'why?'}, headers=AUTH),
               c.post('/api/actions', json={'type': 'ask', 'key': 'q', 'text': 'why?', 'cards': [INFO]}, headers=AUTH)]

    # then
    assert [r.status_code for r in got] == [410, 409, 409, 409], 'holds are gone for every card'


def test_an_information_item_with_an_old_number_never_reaches_closed(store: Store):
    # given
    long_ago = iso(NOW - 30 * HOUR)
    seen(store, info_item(status='cleared', clearedAt=long_ago, lastSeen=long_ago, num=4))

    # when
    apply(store, [], NOW)

    # then
    assert store.load()['anomalies'] == [] and store.card_history.count_documents({}) == 0


def test_back_to_the_board_is_offered_only_when_the_hand_move_makes_it_information():
    # given
    hand = {'sev': 'info', 'by': 'alice@example.com', 'at': '2026-11-20T09:00:00Z'}
    warn_signal = info_item('log|a|INFO|x', sevSet=hand | {'auto': 'warn'}, sevAuto='warn')
    info_signal = info_item('log|b|INFO|x', sevSet=hand | {'auto': 'warn'}, sevAuto='info')

    # when
    rows = {r['key']: r for r in information.rows([warn_signal, info_signal], NOW)}

    # then
    assert 'moved' in rows['log|a|INFO|x'] and 'moved' not in rows['log|b|INFO|x']


def test_raising_an_item_to_warning_by_hand_numbers_it_at_once(store: Store):
    # given
    store.card_ids.insert_one({'_id': 'other', 'n': 6})
    store.counters.insert_one({'_id': 'card', 'n': 6})
    seen(store, info_item())

    # when
    with TestClient(create_app(settings(), store)) as c:
        c.post('/api/actions', json={'type': 'severity', 'key': INFO, 'sev': 'warn'}, headers=AUTH)
        page = c.get('/api/state').json()

    # then
    assert store.anomaly(INFO)['num'] == 7 and page['anomalies'][0]['num'] == 7


def test_deploy_report_flags_do_not_link_information_as_a_card():
    # given
    import json
    from skopax.slack import report_message
    row = {'svc': 'shop', 'ns': 'shop', 'flags': ['err=3'], 'at': iso(NOW - 600)}
    report = {'id': 'r1', 'summary': 'release', 'createdAt': iso(NOW), 'windowTo': iso(NOW), 'rows': [row]}
    item = info_item('log|shop|ERROR|x', firstSeen=iso(NOW - 300), lastSeen=iso(NOW - 60))
    card = anomaly('log|shop|ERROR|y', sev='warn', svc='shop', firstSeen=iso(NOW - 300), lastSeen=iso(NOW - 60), num=9)

    # when
    told = json.dumps(report_message(report, 'https://skopax.example.com/#r=r1', 'server', None, [item]))
    linked = json.dumps(report_message(report, 'https://skopax.example.com/#r=r1', 'server', None, [item, card]))

    # then
    assert 'ERROR%7Cx' not in told and '/c/' not in told
    assert '/c/9' in linked


def test_the_deprecation_line_is_logged_once_per_process(caplog):
    # given
    from skopax import config
    config.TOLD.discard(config.INFO_DAYS_TOLD)
    caplog.set_level(logging.WARNING, 'skopax')

    # when
    load_settings(lambda: {'core.info-auto-close-days': '5'})
    load_settings(lambda: {'core.info-auto-close-days': '5'})

    # then
    assert caplog.text.count('core.info-auto-close-days is deprecated') == 1


@pytest.mark.parametrize('lang', ['de', 'es', 'fr', 'tr'])
def test_every_pack_words_the_move_label_and_the_severity_tooltip(lang):
    # given
    import re
    from skopax import locales
    rules = locales.read(lang).rules
    tail = '. A hand-set severity stays through every check until you set it back; Critical pushes, Move to Information takes it off the board onto the Information page'
    said = ['Severity now: Warning, from the signal' + tail, 'Severity now: Info, set by hand by alice; the signal says Warning' + tail, 'Moved to Information: shop / Latency']

    # when
    hits = [[r for r, _ in rules if re.search(r, text)] for text in said]

    # then
    assert all(len(h) == 1 for h in hits), hits


def test_teams_offers_no_info_and_an_old_info_route_or_setting_reads_as_warning(store: Store, caplog, monkeypatch):
    # given
    from skopax import config
    from skopax.plugins import teams
    from tests.teams_kit import DATA, Transport, route, routes_of
    monkeypatch.setattr(config, 'TOLD', set())
    caplog.set_level(logging.WARNING, 'skopax')
    field = next(f for f in teams.PLUGIN.fields if f.key == 'min-sev')
    setting = next(s for s in teams.PLUGIN.settings if s.key == 'min-sev')

    # when
    found = routes_of(store, Transport(), route('loud', DATA, keys='log|loud|*', sev='info'), old=False, min_sev='info')
    sevs = [r.min_sev for r in found.routes()] + [found.setting('teams', 'min-sev')]

    # then
    assert 'info' not in field.choices and 'info' not in setting.choices
    assert sevs == ['warn', 'warn']
    assert caplog.text.count('teams min-sev info is retired') == 1 and caplog.text.count('teams.min-sev info is retired') == 1


def test_an_owned_info_card_is_not_held_as_critical_under_a_handled_parent(store: Store):
    # given
    from skopax import actions
    host = anomaly(WARN, sev='warn', firstSeen=iso(NOW - HOUR), triage='handled')
    owned = info_item(owner='alice@example.com', triage='handled', triagedBy='alice@example.com')
    seen(store, host, owned)

    # when
    grouped = actions.apply(store, {'type': 'group', 'key': INFO, 'parent': WARN, 'requestedBy': 'alice@example.com'})

    # then
    shown = store.anomaly(WARN)
    assert grouped == 'grouped under ' + (f"#{shown['num']} " if shown.get('num') else '') + f"{shown['svc']} / {shown['label']}", 'an info card someone owns is not critical'


def test_the_investigator_triage_and_group_ignore_information_children(store: Store):
    # given
    from tests.test_investigator_tools import box
    kid = 'log|kid|WARN|x'
    seen(store, anomaly(WARN, sev='warn', firstSeen=iso(NOW - HOUR)), info_item(parent=WARN), anomaly(kid, sev='warn', firstSeen=iso(NOW - HOUR)),
         info_item('log|kid|INFO|y', parent=kid))
    b = box(store, keys=(WARN, kid))
    b.call('note', {'key': WARN, 'text': 'one client'})

    # when
    out, err = b.call('triage', {'key': WARN, 'state': 'handled'})
    grouped, group_err = b.call('group', {'parent': WARN, 'children': [kid]})

    # then
    assert not err, out
    assert not group_err, grouped
    assert store.anomaly(WARN)['triage'] == 'handled' and store.anomaly(kid)['parent'] == WARN


def test_an_ask_on_information_is_refused_when_the_investigator_picks_it(store: Store):
    # given
    from tests.conftest import ready_app
    seen(store, info_item())
    with TestClient(ready_app(settings(), store)) as c:
        store.add_action({'type': 'ask', 'key': 'q1', 'target': 'q1', 'text': 'why?', 'cards': [INFO], 'requestedBy': 'alice@example.com', 'source': 'page'})

        # when
        work = c.app.state.investigator.pick_action()

    # then
    assert work is None
    [act] = store.list_actions(None, 10)
    assert act['status'] == 'failed' and act['result'] == msg('cards.information_item', key=INFO)


@pytest.mark.parametrize('lang', ['de', 'es', 'fr', 'tr'])
def test_every_pack_words_the_business_days_hint(lang):
    # given
    from skopax import locales
    from skopax.tunables import registry
    hint = next(t.help for t in registry() if t.key == 'core.business-days')

    # when
    page = locales.read(lang).page

    # then
    assert hint in page and not any('info close' in k for k in page)
