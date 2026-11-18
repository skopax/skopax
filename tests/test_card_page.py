import json

from fastapi.testclient import TestClient

from skopax.plugins.teams import cards as teams_cards
from tests.conftest import anomaly, jira_links, settings, state
from tests.teams_kit import BIZDEV, Transport, route, routes_of, texts
from tests.test_tags import typo_card
from skopax.app import create_app
from skopax.engine import PassResult, number_cards
from skopax.kube import Kube
from skopax.slack import anomaly_message
from skopax.store import Store

NOW = 1_794_240_000.0
AT = '2026-11-09T16:00:00Z'


def test_every_card_key_gets_one_number_for_good_and_the_next_key_the_next_number(store: Store):
    # given
    cards = [{'key': 'a'}, {'key': 'b'}, {'key': 'a-again', 'num': 7}]

    # when
    number_cards(store, cards)
    again = [{'key': 'b'}, {'key': 'c'}]
    number_cards(store, again)

    # then
    assert [c['num'] for c in cards] == [1, 2, 7]
    assert [c['num'] for c in again] == [2, 3], 'a card that comes back keeps its number'
    assert store.key_of_number(3) == 'c' and store.key_of_number(99) is None


def test_a_pass_numbers_its_cards_and_the_number_survives_the_next_pass(store: Store, tmp_path):
    # given
    store.save({**state(), 'initialized': True}, None)
    found = typo_card('https://shop.example.com/products', [{'wrong': 'Crad', 'fix': 'Card', 'where': ''}])

    # when
    with TestClient(create_app(settings(), store, kube=Kube(root=tmp_path))) as c:
        c.app.state.server_apply([PassResult('pagescan', True, 'ok', [found], {}, (found['ranKey'],))], NOW, None)
        first = store.anomaly(found['key'])['num']
        c.app.state.server_apply([PassResult('pagescan', True, 'ok', [found], {}, (found['ranKey'],))], NOW + 600, None)

    # then
    assert first == 1 and store.anomaly(found['key'])['num'] == 1


def test_the_card_api_gives_one_card_by_number_live_or_from_its_history_and_the_page_loads_it(store: Store, tmp_path):
    # given
    live = anomaly('page-typos|shop.example.com/products', sev='warn', svc='shop.example.com/products', label='Typos', num=4, url='https://shop.example.com/products', what='1 typo',
                   firstSeen=AT, tags=[{'id': 'seo', 'by': 'rule', 'at': AT}], business={'text': 'Fix it.', 'by': 'rule', 'at': AT},
                   slackPost={'ts': '1', 'screenshots': [{'id': 's1', 'viewport': 'phone'}]}, slackDraft={'text': 'secret draft'}, ticket='OPS-1', recommend='fix it')
    store.save(state(live), None)
    store.card_ids.insert_many([{'_id': live['key'], 'n': 4}, {'_id': 'log|old|ERROR|x', 'n': 5}, {'_id': 'log|gone|ERROR|y', 'n': 6}])
    store.card_history.insert_one({'key': 'log|old|ERROR|x', 'svc': 'old', 'label': 'ERROR lines', 'sev': 'crit', 'firstSeen': '2026-11-01T10:00:00Z', 'lastSeen': '2026-11-01T12:00:00Z', 'outcome': 'cleared'})

    # when
    with TestClient(create_app(settings(), store, kube=Kube(root=tmp_path))) as c:
        got = c.get('/api/cards/4').json()
        past = c.get('/api/cards/5').json()
        bare = c.get('/api/cards/6').json()
        missing = c.get('/api/cards/77')
        page = c.get('/c/4')

    # then
    assert got['live'] is True and got['num'] == 4 and got['business']['text'] == 'Fix it.' and got['screenshots'] == [{'id': 's1', 'viewport': 'phone'}]
    assert 'slackDraft' not in got and 'slackPost' not in got, 'only what the card page shows'
    assert (past['live'], past['num'], past['svc'], past['outcome']) == (False, 5, 'old', 'cleared')
    assert bare == {'num': 6, 'key': 'log|gone|ERROR|y', 'live': False}
    assert missing.status_code == 404
    assert page.status_code == 200 and '/static/card.js?v=' in page.text and page.headers['cache-control'] == 'no-cache'


def test_business_teams_posts_link_the_card_page_and_slack_titles_carry_the_number(store: Store):
    # given
    card = anomaly('log|cart|ERROR|x', sev='warn', svc='checkout-service', label='ERROR lines', num=482, business={'text': 'Fix the slug in the back office.', 'by': 'pod', 'at': AT})

    # when
    teams = teams_cards.render(routes_of(store, Transport(), route('bizdev', BIZDEV, tags='bizdev')).business_message(card, 'Business Development', ()))
    slack = anomaly_message(card, 'https://skopax.example.com/#a=x', 'token')

    # then
    assert texts(teams)[-1] == '#482 \u00b7 [Open card](https://skopax.example.com/c/482) \u00b7 [Chat](https://skopax.example.com/#t=chat/new/482)'
    assert slack['blocks'][1]['text']['text'] == '*<https://skopax.example.com/#a=x|#482 checkout-service / ERROR lines>*'
    assert json.dumps(teams).count('#a=') == 0


def test_every_outgoing_card_link_opens_the_card_page_when_the_card_has_a_number():
    # given
    from skopax.cardlink import card_url, linked
    from skopax.push import page_link

    numbered = anomaly('log|cart|ERROR|x', num=482)
    old = anomaly('log|cart|ERROR|y')

    # when
    slack = anomaly_message(numbered, card_url('https://skopax.example.com/', numbered['key'], numbered['num']), 'token')

    # then
    assert card_url('https://skopax.example.com/', 'log|cart|ERROR|x', 482) == 'https://skopax.example.com/c/482'
    assert card_url('https://skopax.example.com', 'log|cart|ERROR|y') == 'https://skopax.example.com/#a=log%7Ccart%7CERROR%7Cy'
    assert slack['blocks'][1]['text']['text'].startswith('*<https://skopax.example.com/c/482|')
    assert page_link('https://skopax.example.com', [numbered]) == 'https://skopax.example.com/c/482' and page_link('https://skopax.example.com', [old]).endswith('#a=log%7Ccart%7CERROR%7Cy')
    assert linked('see https://skopax.example.com/c/482 and https://skopax.example.com/#a=log%7Cb%7CERROR%7Cz', 'https://skopax.example.com') == ({'log|b|ERROR|z'}, {482})


def test_a_jira_ticket_with_a_card_page_link_moves_that_card_to_in_progress(store: Store):
    # given
    from types import SimpleNamespace

    store.save(state(anomaly('log|cart|ERROR|x', num=482)), None)
    store.card_ids.insert_one({'_id': 'log|cart|ERROR|x', 'n': 482})
    issue = {'key': 'OPS-9', 'fields': {'status': {'name': 'In Progress'}, 'description': 'Card: https://skopax.example.com/c/482', 'customfield_4': {'displayName': 'Nina'}}}
    jira = SimpleNamespace(enabled=True, search=lambda jql, fields, limit: [issue] if 'skopax.example.com' in jql else [])

    # when
    linked = jira_links(store, jira).run_once(NOW)

    # then
    assert {'card': 'log|cart|ERROR|x', 'issue': 'OPS-9'} in linked
    card = store.anomaly('log|cart|ERROR|x')
    assert (card['triage'], card['ticket'], card['owner']) == ('progress', 'OPS-9', 'Nina')


def test_a_merged_card_brings_its_merged_cards_to_the_card_page_and_the_business_post(store: Store, tmp_path):
    # given
    tag = [{'id': 'bizdev', 'by': 'rule', 'at': AT}]
    parent = anomaly('page-typos|shop.example.com/a', num=2, svc='shop.example.com/a', label='Typos', url='https://shop.example.com/a', tags=tag, firstSeen=AT,
                     business={'text': 'On shop.example.com/a change "x" to "y".', 'by': 'rule', 'at': AT})
    kid = anomaly('page-typos|shop.example.com/b', num=3, svc='shop.example.com/b', label='Typos', url='https://shop.example.com/b', tags=tag, firstSeen=AT, parent=parent['key'],
                  what='1 typo on https://shop.example.com/b', business={'text': 'On shop.example.com/b change "k" to "l".', 'by': 'rule', 'at': AT})
    quiet = anomaly('page-typos|shop.example.com/c', num=4, svc='shop.example.com/c', label='Typos', firstSeen=AT, parent=parent['key'])
    store.save(state(parent, kid, quiet), None)
    store.card_ids.insert_one({'_id': parent['key'], 'n': 2})

    t = Transport()

    # when
    with TestClient(create_app(settings(), store, kube=Kube(root=tmp_path))) as c:
        got = c.get('/api/cards/2').json()
    sent = routes_of(store, t, route('bizdev', BIZDEV, tags='bizdev')).tagged(store.load()['anomalies'])

    # then
    assert [m['num'] for m in got['merged']] == [3, 4]
    assert got['merged'][0] == {'num': 3, 'key': kid['key'], 'svc': 'shop.example.com/b', 'label': 'Typos', 'sev': 'warn', 'status': 'open', 'url': 'https://shop.example.com/b',
                                'what': '1 typo on https://shop.example.com/b', 'business': kid['business']}
    assert sent == {'teams/bizdev': [parent['key']]}, 'merged cards ride in the parent post, never alone'
    lines = texts(t.calls[0][1])
    assert lines[1] == '**Recommended:** ' + parent['business']['text']
    assert lines[2] == '\u2022 ' + kid['business']['text'] + ' \u00b7 [Open page](https://shop.example.com/b) \u00b7 [#3](https://skopax.example.com/c/3)'
    assert not any('#4' in line for line in lines), 'a merged card without an action adds nothing'


def test_the_card_api_carries_the_card_s_last_action_and_actions_need_an_operator(store: Store, tmp_path):
    # given
    from tests.conftest import AUTH

    store.save({**state(anomaly('log|cart|ERROR|x', num=9)), 'initialized': True}, None)
    store.card_ids.insert_one({'_id': 'log|cart|ERROR|x', 'n': 9})

    # when
    with TestClient(create_app(settings(), store, kube=Kube(root=tmp_path))) as c:
        before = c.get('/api/cards/9').json()
        guest = c.post('/api/actions', json={'type': 'dive', 'key': 'log|cart|ERROR|x'})
        queued = c.post('/api/actions', json={'type': 'dive', 'key': 'log|cart|ERROR|x', 'label': 'Investigate'}, headers=AUTH)
        after = c.get('/api/cards/9').json()

    # then
    assert before['last'] is None
    assert guest.status_code == 401 and queued.status_code == 201
    assert (after['last']['type'], after['last']['status'], after['last']['requestedBy']) == ('dive', 'requested', 'token:bootstrap')


def test_the_take_field_searches_jira_by_key_or_task_name_for_operators_only(store: Store, tmp_path):
    # given
    import dataclasses
    from types import SimpleNamespace

    from tests.conftest import AUTH
    from skopax.app import ticket_jql

    asked = []
    jira = SimpleNamespace(enabled=True, search=lambda jql, fields, limit: asked.append((jql, limit)) or [
        {'key': 'OPS-144', 'fields': {'summary': 'SEO metadata fetch cache', 'status': {'name': 'In UAT'}, 'customfield_4': {'displayName': 'Oscar Green'}}}])
    store.save({**state(), 'initialized': True}, None)

    # when
    with TestClient(create_app(settings(), store, kube=Kube(root=tmp_path), jira=jira)) as c:
        found = c.get('/api/jira/search', params={'q': 'seo cache'}, headers=AUTH).json()
        short = c.get('/api/jira/search', params={'q': 'x'}, headers=AUTH).json()
        c.get('/api/jira/search', params={'q': 'audit trail', 'type': 'epic', 'project': 'web'}, headers=AUTH)
        bad_project = c.get('/api/jira/search', params={'q': 'audit', 'type': 'epic', 'project': 'WEB" OR 1=1'}, headers=AUTH)
        guest = c.get('/api/jira/search', params={'q': 'seo'})

    # then
    assert found == [{'key': 'OPS-144', 'summary': 'SEO metadata fetch cache', 'status': 'In UAT', 'developer': 'Oscar Green'}]
    assert asked == [('summary ~ "seo*" AND summary ~ "cache*" AND statusCategory != Done ORDER BY updated DESC', 8),
                     ('summary ~ "audit*" AND summary ~ "trail*" AND issuetype = Epic AND project = "WEB" AND statusCategory != Done ORDER BY updated DESC', 8)]
    assert bad_project.status_code == 422
    assert short == [] and guest.status_code == 401
    assert ticket_jql('ops-144') == 'key = "OPS-144"'
    assert ticket_jql('a"; delete') == 'summary ~ "delete*" AND statusCategory != Done ORDER BY updated DESC', 'quotes and short words never reach the JQL'
