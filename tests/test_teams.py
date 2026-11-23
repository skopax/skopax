import json
import logging
import time

import pytest
from fastapi.testclient import TestClient

from skopax import tags
from skopax.app import create_app
from skopax.engine import PassResult, iso as iso_at
from skopax.kube import Kube
from skopax.messages import msg
from skopax.plugins import teams
from skopax.store import Store
from tests.conftest import AUTH, settings, state
from tests.teams_kit import (ALICE, AT, BIZDEV, BOB, DATA, MARKETING, SEO, TECH, Transport, add_route, open_card, operator_me, route, routes_of, runtime_of,
                             tag, texts)
from tests.test_investigator_tools import box
from tests.test_tags import typo_card

NOW = 1_794_240_000.0
TYPO = {'wrong': 'Gift Crad', 'fix': 'Gift Card', 'where': 'FAQ'}


def card(key: str, sev: str = 'warn', **extra) -> dict:
    return {'key': key, 'sev': sev, 'svc': key.split('|')[1], 'label': 'Typos', 'what': 'x' * 400, 'status': 'open', **extra}


def said(text: str, **extra) -> dict:
    return {'text': text, 'by': 'pod', 'at': AT, **extra}


def test_routes_come_from_instances_and_an_incomplete_one_stays_off(store: Store):
    # given
    specs = (route('marketing', MARKETING, keys='page-typos|*, page-layout|*'), route('data', DATA, keys='*namespace=reporting*', sev='crit'),
             route('finance', '', keys='*|payments-service|*'))

    # when
    found = routes_of(store, Transport(), *specs, old=False).routes()

    # then
    assert [(r.id, r.keys, r.min_sev, r.tags, r.mentions) for r in found] == [('teams/data', ('*namespace=reporting*',), 'crit', frozenset(), ()),
                                                                               ('teams/marketing', ('page-typos|*', 'page-layout|*'), 'high', frozenset(), ())], 'a key route with no lowest severity starts at high'


def test_each_team_gets_one_post_with_its_own_cards_and_never_a_muted_or_too_mild_one(store: Store):
    # given
    t = Transport()
    routes = routes_of(store, t, route('marketing', MARKETING, keys='page-typos|*'), route('data', DATA, keys='*namespace=reporting*', sev='crit'))
    items = [card('page-typos|shop.example.com/products'), card('page-typos|shop.example.com/cart', muted=True), card('alert|JobFailed|namespace=reporting,job=x'),
             card('log|checkout-service|ERROR|boom', 'crit')]

    # when
    sent = routes.push(items)

    # then
    assert sent == {'teams/marketing': ['page-typos|shop.example.com/products']}
    [(url, body)] = t.calls
    assert url == MARKETING and body['type'] == 'message'
    adaptive = body['attachments'][0]
    assert adaptive['contentType'] == 'application/vnd.microsoft.card.adaptive' and adaptive['content']['type'] == 'AdaptiveCard'
    lines = texts(body)
    assert lines[0] == 'Skopax \u00b7 1 card'
    assert lines[1] == '\U0001f7e1 Warning \u00b7 **shop.example.com/products / Typos**'
    assert lines[2] == 'x' * 299 + '\u2026'
    assert lines[3] == '[Open in Skopax](https://skopax.example.com/#a=page-typos%7Cshop.example.com%2Fproducts) \u00b7 [Chat](https://skopax.example.com/#t=chat/new/page-typos%7Cshop.example.com%2Fproducts)'
    assert any(r['kind'] == 'routes' and 'marketing' in r['text'] for r in store.list_activity(None, 5))


def test_a_failed_post_is_logged_without_the_secret_url_and_the_other_teams_still_get_theirs(store: Store, caplog):
    # given
    t = Transport(fail='hooks.example.com')
    routes = routes_of(store, t, route('marketing', MARKETING, keys='page-typos|*'), route('data', DATA, keys='page-typos|*'))

    # when
    with caplog.at_level(logging.WARNING, logger='skopax.routes'):
        sent = routes.push([card('page-typos|shop.example.com/products')])

    # then
    assert sent == {'teams/data': ['page-typos|shop.example.com/products']}
    assert 'SECRET' not in caplog.text and 'route teams/marketing post failed: HTTP 400' in caplog.text


def test_the_app_posts_pushed_cards_to_teams_and_a_test_post_checks_a_route(store: Store, tmp_path):
    # given
    t = Transport()
    store.save({**state(), 'initialized': True}, None)
    found = typo_card('https://shop.example.com/products', [TYPO])

    # when
    with TestClient(create_app(settings(), store, kube=Kube(root=tmp_path), transport=t)) as c:
        add_route(c, 'marketing', MARKETING, keys='page-typos|*', min_sev='warn')
        c.app.state.server_apply([PassResult('pagescan', True, 'ok', [found], {}, (found['ranKey'],))], NOW, None)
        assert c.app.state.routes.drain(5)
        tested = c.post('/api/admin/plugins/teams/test', json={'name': 'marketing'}, headers=AUTH)
        unknown = c.post('/api/admin/plugins/teams/test', json={'name': 'finance'}, headers=AUTH)
        anonymous = c.post('/api/admin/plugins/teams/test', json={'name': 'marketing'})
        info = operator_me(c, store)['routes']

    # then
    assert texts(t.calls[0][1])[1].endswith('**shop.example.com/products / Typos** \u00b7 #1'), 'the pass gave the new card its number'
    assert (tested.json()['ok'], tested.json()['summary']) == (True, 'Teams took the test post')
    assert texts(t.calls[-1][1])[0] == 'Skopax \u00b7 connection test'
    assert (unknown.status_code, anonymous.status_code) == (404, 401)
    assert info == [{'id': 'teams/marketing', 'name': 'marketing', 'tags': [], 'tagged': False, 'business': False, 'plugin': 'Microsoft Teams'}]


def test_one_card_can_be_sent_by_hand_to_its_teams_or_a_named_one(store: Store, tmp_path):
    # given
    t = Transport()
    store.save({**state(card('log|web-shop|WARN|boom', 'crit'), card('page-typos|shop.example.com/products')), 'initialized': True}, None)

    # when
    with TestClient(create_app(settings(), store, kube=Kube(root=tmp_path), transport=t)) as c:
        add_route(c, 'tech', MARKETING, keys='*', min_sev='crit')
        add_route(c, 'marketing', DATA, keys='page-typos|*')
        crit = c.post('/api/routes/send', json={'key': 'log|web-shop|WARN|boom'}, headers=AUTH)
        named = c.post('/api/routes/send', json={'key': 'page-typos|shop.example.com/products', 'route': 'teams/tech'}, headers=AUTH)
        nobody = c.post('/api/routes/send', json={'key': 'log|x|ERROR|y'}, headers=AUTH)
        anonymous = c.post('/api/routes/send', json={'key': 'log|web-shop|WARN|boom'})

    # then
    assert (crit.json(), named.json()) == ({'ok': True, 'sent': ['teams/tech']}, {'ok': True, 'sent': ['teams/tech']})
    assert [u for u, _ in t.calls] == [MARKETING, MARKETING]
    assert (nobody.status_code, anonymous.status_code) == (404, 401)
    posts = store.anomaly('log|web-shop|WARN|boom')['teamsPosts']
    assert [(p['route'], p['plugin'], p['by']) for p in posts] == [('tech', 'teams', 'token:bootstrap')] and posts[0]['at'].endswith('Z')


def test_a_card_keeps_where_it_went_in_teams_through_a_state_save_and_the_page_learns_the_routes(store: Store, tmp_path):
    # given
    t = Transport()
    store.save({**state(card('log|web-shop|WARN|boom', 'crit')), 'initialized': True}, None)
    old = store.load()

    # when
    with TestClient(create_app(settings(), store, kube=Kube(root=tmp_path), transport=t)) as c:
        add_route(c, 'tech', MARKETING, keys='*', min_sev='crit')
        c.post('/api/routes/send', json={'key': 'log|web-shop|WARN|boom'}, headers=AUTH)
        c.put('/api/state', json=old, headers={**AUTH, 'X-Base-Rev': str(store.rev()['rev'])})
        me = operator_me(c, store)

    # then
    assert [p['route'] for p in store.anomaly('log|web-shop|WARN|boom')['teamsPosts']] == ['tech']
    assert me['routes'] == [{'id': 'teams/tech', 'name': 'tech', 'tags': [], 'tagged': False, 'business': False, 'plugin': 'Microsoft Teams'}]


def test_routes_take_tags_or_card_keys_and_a_business_route_takes_every_severity(store: Store):
    # when
    found = routes_of(store, Transport(), route('bizdev', BIZDEV, tags='bizdev, marketing'), route('seo', SEO, tags='seo'), route('tech', TECH, keys='*', sev='crit'), old=False).routes()

    # then
    assert [(r.instance, r.tags, r.keys, r.min_sev) for r in found] == [('bizdev', frozenset({'bizdev', 'marketing'}), (), 'warn'), ('seo', frozenset({'seo'}), (), 'warn'),
                                                                       ('tech', frozenset(), ('*',), 'crit')]
    assert [(r.business, r.label) for r in found] == [(True, 'bizdev'), (True, 'SEO'), (False, 'tech')]
    assert found[0].takes(open_card('log|a|ERROR|x', 'warn', tags=tag('marketing'))) and not found[2].takes(open_card('log|a|ERROR|x', 'warn'))
    assert not found[0].takes(open_card('log|a|ERROR|x', 'info', tags=tag('marketing'))), 'information never reaches a channel'


def test_a_business_channel_gets_a_tagged_open_card_once_with_only_the_sentence_and_links(store: Store):
    # given
    t = Transport()
    routes = routes_of(store, t, route('bizdev', BIZDEV, tags='bizdev'), route('tech', TECH, keys='*', sev='crit'))
    typo = typo_card('https://shop.example.com/products', [TYPO]) | {'status': 'open', 'firstSeen': '2026-11-09T10:00:00Z', 'sev': 'warn'}
    tags.apply_rules([typo], tags.RULES, AT)
    typo['business'] = {'text': 'We recommend changing "Gift Crad" to "Gift Card" on shop.example.com/products.', 'by': 'rule', 'at': AT}
    crit = open_card('log|web-shop|ERROR|boom', 'crit', tags=tag('bizdev'), business=said('We recommend checking why the product page does not open.'))
    mute = open_card('log|a|ERROR|m', muted='noise', tags=tag('bizdev'), business=said('m'))
    silent = open_card('log|a|ERROR|s', tags=tag('bizdev'))
    handled = open_card('log|a|ERROR|h', triage='handled', tags=tag('bizdev'), business=said('h'))
    store.save(state(typo, crit, mute, silent, handled), None)
    cards = store.load()['anomalies']

    # when
    first = routes.tagged(cards)
    again = routes.tagged(store.load()['anomalies'])

    # then
    assert first == {'teams/bizdev': ['page-typos|shop.example.com/products', 'log|web-shop|ERROR|boom']} and again == {}
    [(url, body), (url2, body2)] = t.calls
    assert url == url2 == BIZDEV, 'every card is its own post'
    assert texts(body) == ['Skopax \u00b7 Business Development', '**Recommended:** ' + typo['business']['text'],
                           '[Open page](https://shop.example.com/products) \u00b7 [Open in Skopax](https://skopax.example.com/#a=page-typos%7Cshop.example.com%2Fproducts) \u00b7 '
                           '[Chat](https://skopax.example.com/#t=chat/new/page-typos%7Cshop.example.com%2Fproducts)']
    assert texts(body2) == ['Skopax \u00b7 Business Development', '\U0001f534 Urgent \u00b7 **Recommended:** We recommend checking why the product page does not open.',
                            '[Open in Skopax](https://skopax.example.com/#a=log%7Cweb-shop%7CERROR%7Cboom) \u00b7 [Chat](https://skopax.example.com/#t=chat/new/log%7Cweb-shop%7CERROR%7Cboom)']
    assert 'checkout-service' not in json.dumps([body, body2]) and 'NullPointer' not in json.dumps([body, body2])
    assert [(p['route'], p['by']) for p in store.anomaly('log|web-shop|ERROR|boom')['teamsPosts']] == [('bizdev', 'server')]


def test_a_failed_business_post_is_taken_back_and_tried_again_and_a_reopened_card_goes_again(store: Store):
    # given
    t = Transport(fail='sig=SEO')
    routes = routes_of(store, t, route('seo', SEO, tags='seo'))
    store.save(state(open_card('log|web-shop|ERROR|boom', tags=tag('seo'), business=said('We recommend checking the product page.'))), None)

    # when
    failed = routes.tagged(store.load()['anomalies'])
    t.fail = None
    sent = routes.tagged(store.load()['anomalies'])
    reopened = store.load()
    reopened['anomalies'][0]['firstSeen'] = '2099-01-01T00:00:00Z'
    store.save(reopened, None)
    later = routes.tagged(store.load()['anomalies'], now=4_070_908_800.0 + 60)

    # then
    assert failed == {} and sent == {'teams/seo': ['log|web-shop|ERROR|boom']} and later == {'teams/seo': ['log|web-shop|ERROR|boom']}
    assert len(t.calls) == 3


def test_the_push_flow_never_sends_to_a_business_channel_and_the_pass_tags_and_posts_page_cards(store: Store, tmp_path):
    # given
    t = Transport()
    store.save({**state(), 'initialized': True}, None)
    found = typo_card('https://shop.example.com/products', [TYPO])

    # when
    with TestClient(create_app(settings(), store, kube=Kube(root=tmp_path), transport=t)) as c:
        add_route(c, 'bizdev', BIZDEV, tags='bizdev')
        add_route(c, 'tech', TECH, keys='*', min_sev='warn')
        c.app.state.server_apply([PassResult('pagescan', True, 'ok', [found], {}, (found['ranKey'],))], NOW, None)
        c.app.state.server_apply([PassResult('pagescan', True, 'ok', [found], {}, (found['ranKey'],))], NOW + 600, None)
        assert c.app.state.routes.drain(5)
        early = [u for u, _ in t.calls]
        c.put('/api/business', json={'key': found['key'], 'text': 'We recommend fixing the typo on the products page.', 'why': 'Shoppers see a misspelt product name.'}, headers=AUTH)
        assert c.app.state.routes.drain(5)

    # then
    card_now = store.anomaly(found['key'])
    assert tags.ids(card_now) == ['bizdev'] and early == [TECH], 'the rule tags the card, and the business channel waits for its sentence'
    assert sorted(u for u, _ in t.calls) == sorted([TECH, BIZDEV])
    assert [p['route'] for p in card_now['teamsPosts']] == ['tech', 'bizdev']


def test_a_business_post_leads_with_the_reason_then_the_recommendation_and_mentions_the_route_s_people(store: Store):
    # given
    t = Transport()
    routes = routes_of(store, t, route('bizdev', BIZDEV, tags='bizdev', mentions=f'{ALICE}, {BOB}'))
    store.save(state(open_card('log|checkout-service|WARN|slug', tags=tag('bizdev'), business=said('We recommend setting the product slug back to chairs in the shop admin.',
                                                                                                     why='Old product links open an empty page.'))), None)

    # when
    routes.tagged(store.load()['anomalies'])

    # then
    [(_, body)] = t.calls
    content = body['attachments'][0]['content']
    assert texts(body)[:4] == ['Skopax \u00b7 Business Development', 'cc <at>Alice Archer</at>, <at>Bob Stone</at>', 'Why: Old product links open an empty page.',
                               '**Recommended:** We recommend setting the product slug back to chairs in the shop admin.']
    assert content['body'][2]['weight'] == 'Bolder', 'the reason is what the team reads first'
    assert content['msteams']['entities'] == [{'type': 'mention', 'text': '<at>Alice Archer</at>', 'mentioned': {'id': ALICE, 'name': 'Alice Archer'}},
                                              {'type': 'mention', 'text': '<at>Bob Stone</at>', 'mentioned': {'id': BOB, 'name': 'Bob Stone'}}]


def test_an_old_business_sentence_with_no_why_still_posts(store: Store):
    # given
    t = Transport()
    routes = routes_of(store, t, route('bizdev', BIZDEV, tags='bizdev'))
    store.save(state(open_card('log|a|ERROR|old', tags=tag('bizdev'), business={'text': 'Update the price in the admin tool.', 'by': 'nina.white@example.com', 'at': AT})), None)

    # when
    sent = routes.tagged(store.load()['anomalies'])

    # then
    assert sent == {'teams/bizdev': ['log|a|ERROR|old']}
    assert texts(t.calls[0][1])[1] == '**Recommended:** Update the price in the admin tool.'


@pytest.fixture
def tool_store(store: Store) -> Store:
    store.save({**state(open_card('log|checkout-service|ERROR|boom'), open_card('log|a|ERROR|said', business=said('A person wrote it.', by='nina.white@example.com')),
                        open_card('log|a|ERROR|off', tagsOff=['seo'])), 'initialized': True}, None)
    return store


def test_a_card_the_team_only_needs_to_know_about_posts_as_info_not_as_an_action(tool_store: Store, tmp_path):
    # given
    t = Transport()
    routes = routes_of(tool_store, t, route('bizdev', BIZDEV, tags='bizdev'))
    key = 'log|checkout-service|ERROR|boom'
    b = box(tool_store, keys=(key,))

    # when
    saved, error = b.call('tag', {'key': key, 'tags': ['bizdev'], 'kind': 'info', 'text': 'Orders cannot be finished right now; engineering is fixing it.',
                                  'why': 'Shoppers who check out see an error.'})
    _, bad = b.call('tag', {'key': key, 'tags': ['bizdev'], 'kind': 'fyi', 'text': 'x', 'why': 'y'})
    routes.tagged(tool_store.load()['anomalies'])
    with TestClient(create_app(settings(), tool_store, kube=Kube(root=tmp_path))) as c:
        by_hand = c.put('/api/business', json={'key': key, 'text': 'Orders work again.', 'kind': 'info'}, headers=AUTH).json()
        as_action = c.put('/api/business', json={'key': key, 'text': 'We recommend publishing the garden chair again.'}, headers=AUTH).json()
        wrong = c.put('/api/business', json={'key': key, 'text': 'x', 'kind': 'fyi'}, headers=AUTH)

    # then
    assert not error and bad
    [(_, body)] = t.calls
    assert texts(body)[1:3] == ['**Info:** Orders cannot be finished right now; engineering is fixing it.', 'Why: Shoppers who check out see an error.']
    assert by_hand['business']['kind'] == 'info' and 'kind' not in as_action['business'] and wrong.status_code == 422


def test_an_info_card_that_closes_gets_one_fixed_post_and_an_action_card_gets_none(store: Store):
    # given
    t = Transport()
    routes = routes_of(store, t, route('bizdev', BIZDEV, tags='bizdev', mentions=ALICE))
    info = said('Orders cannot be finished right now; engineering is fixing it.', kind='info')
    store.save(state(open_card('log|a|ERROR|cart', tags=tag('bizdev'), business=info), open_card('log|a|ERROR|other', tags=tag('bizdev'), business=info | {'text': 'Orders are slow.'}),
                     open_card('log|a|ERROR|act', tags=tag('bizdev'), business=said('Update the price in the admin tool.')),
                     open_card('log|a|ERROR|never', tags=tag('bizdev'), business=info | {'text': 'Never sent.'}, muted='noise')), None)
    now = time.time()
    routes.tagged(store.load()['anomalies'], now=now)
    now += 60
    store.anomalies.update_many({'_id': {'$in': ['log|a|ERROR|cart', 'log|a|ERROR|act', 'log|a|ERROR|never']}}, {'$set': {'status': 'cleared', 'clearedAt': iso_at(now)}})
    store.anomalies.update_one({'_id': 'log|a|ERROR|other'}, {'$set': {'triage': 'handled'}})

    # when
    soon = routes.tagged(store.load()['anomalies'], now=now + 300)
    later = routes.tagged(store.load()['anomalies'], now=now + 660)
    again = routes.tagged(store.load()['anomalies'], now=now + 900)

    # then
    assert soon == {}, 'a cleared card waits ten minutes, and a click on Resolve while the problem still fires says nothing'
    assert later == {'teams/bizdev:fixed': ['log|a|ERROR|cart']} and again == {}
    assert [texts(b)[0] for _, b in t.calls] == ['Skopax \u00b7 Business Development'] * 3 + ['Skopax \u00b7 Business Development \u00b7 stopped firing']
    assert texts(t.calls[3][1])[1:] == ['\U0001F7E1 **Stopped firing:** Orders cannot be finished right now; engineering is fixing it.',
                                        '[Open in Skopax](https://skopax.example.com/#a=log%7Ca%7CERROR%7Ccart)']
    assert 'entities' not in t.calls[3][1]['attachments'][0]['content'].get('msteams', {}), 'the all-clear pings nobody'
    assert [p.get('fixed') for p in store.anomaly('log|a|ERROR|cart')['teamsPosts']] == [None, True] and len(store.anomaly('log|a|ERROR|act')['teamsPosts']) == 1


def test_no_fixed_post_for_a_card_that_had_already_closed_when_the_info_went_out_and_none_at_all_while_the_switch_is_off(store: Store):
    # given
    t = Transport()
    routes = routes_of(store, t, route('seo', SEO, tags='seo'))
    off = routes_of(store, t, route('seo', SEO, tags='seo'), fixed_posts=False)
    info = said('Numbered gallery addresses are not real pages.', kind='info')
    now = time.time()
    store.save(state(open_card('log|a|ERROR|late', tags=tag('seo'), business=info), open_card('log|a|ERROR|told', tags=tag('seo'), business=info)), None)
    store.anomalies.update_one({'_id': 'log|a|ERROR|late'}, {'$set': {'status': 'cleared', 'clearedAt': iso_at(now - 3600)}})

    # when
    by_hand = routes.push([store.anomaly('log|a|ERROR|late')], routes.routes(), 'token', now=now)
    told = routes.tagged(store.load()['anomalies'], now=now)
    store.anomalies.update_one({'_id': 'log|a|ERROR|told'}, {'$set': {'status': 'cleared', 'clearedAt': iso_at(now + 60)}})
    quiet = off.tagged(store.load()['anomalies'], now=now + 760)
    late = routes.tagged(store.load()['anomalies'], now=now + 7200)

    # then
    assert by_hand == {'teams/seo': ['log|a|ERROR|late']} and told == {'teams/seo': ['log|a|ERROR|told']}
    assert quiet == {}, 'with fixed posts off nothing follows up'
    assert late == {'teams/seo:fixed': ['log|a|ERROR|told']}, 'the card that closed before its info went out by hand gets no fixed post'
    assert {s.key: s.default for s in teams.PLUGIN.settings}['fixed-posts'] == 'false'


def test_a_failed_fixed_post_is_tried_again_on_the_next_pass(store: Store):
    # given
    t = Transport()
    routes = routes_of(store, t, route('bizdev', BIZDEV, tags='bizdev'))
    store.save(state(open_card('log|a|ERROR|cart', tags=tag('bizdev'), business=said('Orders cannot be finished.', kind='info'))), None)
    now = time.time()
    routes.tagged(store.load()['anomalies'], now=now)
    store.anomalies.update_one({'_id': 'log|a|ERROR|cart'}, {'$set': {'triage': 'handled', 'status': 'cleared', 'clearedAt': iso_at(now + 60)}})

    # when
    t.fail = 'bizdev'
    failed = routes.tagged(store.load()['anomalies'], now=now + 760)
    t.fail = None
    sent = routes.tagged(store.load()['anomalies'], now=now + 860)

    # then
    assert failed == {} and sent == {'teams/bizdev:fixed': ['log|a|ERROR|cart']}


def test_a_channel_shared_by_two_teams_gets_one_post_per_card_titled_with_its_teams(store: Store):
    # given
    t = Transport()
    routes = routes_of(store, t, route('growth', SEO, tags='seo, marketing'))
    store.save(state(open_card('log|a|ERROR|seo', tags=tag('seo'), business=said('Add a redirect.')),
                     open_card('log|a|ERROR|ads', tags=tag('marketing', 'bizdev'), business=said('Change the ad link.')),
                     open_card('log|a|ERROR|both', tags=tag('marketing', 'seo'), business=said('The page was taken down.', kind='info'))), None)

    # when
    routes.tagged(store.load()['anomalies'])

    # then
    posts = [texts(b)[:2] for _, b in t.calls]
    assert posts == [['Skopax \u00b7 SEO', '**Recommended:** Add a redirect.'], ['Skopax \u00b7 Marketing', '**Recommended:** Change the ad link.'],
                     ['Skopax \u00b7 Marketing, SEO', '**Info:** The page was taken down.']], 'one post per card, titled with the teams this channel takes that the card is for'


def test_two_routes_that_share_one_webhook_post_a_card_tagged_for_both_once(store: Store):
    # given
    t = Transport()
    routes = routes_of(store, t, route('marketing', SEO, tags='marketing', mentions=ALICE), route('seo', SEO, tags='seo', mentions=BOB), route('bizdev', BIZDEV, tags='bizdev'))
    both = tag('marketing', 'seo')
    store.save(state(open_card('log|a|ERROR|both', tags=both, business=said('Add 301 redirects for the old product addresses.')),
                     open_card('log|a|ERROR|ads', tags=both[:1], business=said('Test the tag manager tag.')),
                     open_card('log|a|ERROR|seo', tags=both[1:], business=said('Add a redirect.', kind='info')),
                     open_card('log|a|ERROR|biz', tags=tag('bizdev'), business=said('Update the price in the admin tool.'))), None)
    now = time.time()

    # when
    sent = routes.tagged(store.load()['anomalies'], now=now)
    again = routes.tagged(store.load()['anomalies'], now=now + 60)
    store.anomalies.update_one({'_id': 'log|a|ERROR|seo'}, {'$set': {'status': 'cleared', 'clearedAt': iso_at(now + 60)}})
    cleared = routes.tagged(store.load()['anomalies'], now=now + 760)

    # then
    assert sent == {'teams/marketing': ['log|a|ERROR|both', 'log|a|ERROR|ads'], 'teams/seo': ['log|a|ERROR|both', 'log|a|ERROR|seo'], 'teams/bizdev': ['log|a|ERROR|biz']} and again == {}
    assert [u for u, _ in t.calls[:4]] == [BIZDEV, SEO, SEO, SEO], 'a card tagged for both teams goes to the shared link once'
    assert [texts(b)[:3] for _, b in t.calls[1:4]] == [['Skopax \u00b7 Marketing, SEO', 'cc <at>Alice Archer</at>, <at>Bob Stone</at>', '**Recommended:** Add 301 redirects for the old product addresses.'],
                                                       ['Skopax \u00b7 Marketing', 'cc <at>Alice Archer</at>, <at>Bob Stone</at>', '**Recommended:** Test the tag manager tag.'],
                                                       ['Skopax \u00b7 SEO', 'cc <at>Alice Archer</at>, <at>Bob Stone</at>', '**Info:** Add a redirect.']]
    assert [p['route'] for p in store.anomaly('log|a|ERROR|both')['teamsPosts']] == ['marketing', 'seo'], 'both routes count the card as posted'
    assert cleared == {'teams/seo:fixed': ['log|a|ERROR|seo']} and texts(t.calls[4][1])[:2] == ['Skopax \u00b7 SEO \u00b7 stopped firing', '\U0001F7E1 **Stopped firing:** Add a redirect.']


def test_a_failed_post_to_a_shared_link_takes_back_every_claim_so_the_next_pass_tries_again(store: Store):
    # given
    t = Transport(fail='seo')
    routes = routes_of(store, t, route('marketing', SEO, tags='marketing'), route('seo', SEO, tags='seo'))
    store.save(state(open_card('log|a|ERROR|both', tags=tag('marketing', 'seo'), business=said('x'))), None)

    # when
    failed = routes.tagged(store.load()['anomalies'])
    t.fail = None
    sent = routes.tagged(store.load()['anomalies'])

    # then
    assert failed == {} and sent == {'teams/marketing': ['log|a|ERROR|both'], 'teams/seo': ['log|a|ERROR|both']} and len(t.calls) == 2


def test_a_route_that_takes_the_tech_tag_posts_the_technical_card_once_and_needs_no_sentence(store: Store):
    # given
    t = Transport()
    routes = routes_of(store, t, route('eng', TECH, tags='developer'))
    store.save(state(open_card('log|a|ERROR|fix', tags=tag('developer')), open_card('log|a|ERROR|plain', 'crit'), open_card('log|a|ERROR|mute', tags=tag('developer'), muted='noise')), None)

    # when
    pushed = routes.push(store.load()['anomalies'])
    first = routes.tagged(store.load()['anomalies'])
    again = routes.tagged(store.load()['anomalies'])

    # then
    assert pushed == {}, 'a tag route waits for the tag; the push flow leaves it alone'
    assert first == {'teams/eng': ['log|a|ERROR|fix']} and again == {}
    [(url, body)] = t.calls
    assert url == TECH and texts(body) == ['Skopax \u00b7 Developer', '\U0001f7e1 Warning \u00b7 **checkout-service / ERROR lines**', 'NullPointerException in OrderService',
                                          '[Open in Skopax](https://skopax.example.com/#a=log%7Ca%7CERROR%7Cfix) \u00b7 [Chat](https://skopax.example.com/#t=chat/new/log%7Ca%7CERROR%7Cfix)']


def test_a_link_shared_by_a_business_and_a_tech_route_posts_the_sentence_once_and_a_tech_only_card_in_full(store: Store):
    # given
    t = Transport()
    routes = routes_of(store, t, route('seo', SEO, tags='seo'), route('eng', SEO, tags='developer'))
    store.save(state(open_card('log|a|ERROR|both', tags=tag('seo', 'developer'), business=said('Add a redirect.')),
                     open_card('log|a|ERROR|code', tags=tag('developer')),
                     open_card('log|a|ERROR|unsaid', tags=tag('seo', 'developer'))), None)

    # when
    sent = routes.tagged(store.load()['anomalies'])

    # then
    assert sent == {'teams/seo': ['log|a|ERROR|both'], 'teams/eng': ['log|a|ERROR|both', 'log|a|ERROR|code', 'log|a|ERROR|unsaid']}
    assert [texts(b)[:2] for _, b in t.calls] == [['Skopax \u00b7 SEO, Developer', '**Recommended:** Add a redirect.'],
                                                  ['Skopax \u00b7 Developer', '\U0001f7e1 Warning \u00b7 **checkout-service / ERROR lines**'],
                                                  ['Skopax \u00b7 Developer', '\U0001f7e1 Warning \u00b7 **checkout-service / ERROR lines**']]


def test_a_card_that_fires_again_or_gets_a_second_team_tag_is_not_posted_twice_to_one_channel_within_the_repeat_days(store: Store):
    # given
    t = Transport()
    routes = routes_of(store, t, route('marketing', SEO, tags='marketing'), route('seo', SEO, tags='seo'), route('bizdev', BIZDEV, tags='bizdev'), route('eng', SEO, tags='developer'),
                       repeat_days=7)
    key = 'log|a|ERROR|again'
    store.save(state(open_card(key, tags=tag('seo'), business=said('Add a redirect.'))), None)
    now = time.time()

    # when
    first = routes.tagged(store.load()['anomalies'], now=now)
    refired = store.load()
    refired['anomalies'][0].update(firstSeen=iso_at(now + 3600), tags=tag('seo', 'marketing', 'bizdev', 'developer'))
    store.save(refired, None)
    same_day = routes.tagged(store.load()['anomalies'], now=now + 3700)
    week_later = store.load()
    week_later['anomalies'][0]['firstSeen'] = iso_at(now + 8 * 86400)
    store.save(week_later, None)
    later = routes.tagged(store.load()['anomalies'], now=now + 8 * 86400 + 60)

    # then
    assert first == {'teams/seo': [key]}
    assert same_day == {'teams/bizdev': [key], 'teams/eng': [key]}, 'the shared business channel has it already; another channel and the technical form still get it once'
    assert sorted(later) == ['teams/bizdev', 'teams/eng', 'teams/marketing', 'teams/seo'], 'after the repeat days a card that fires again is news again'
    assert sorted(u for u, _ in t.calls) == sorted([SEO, BIZDEV, SEO, SEO, BIZDEV]), 'five posts in all: one, then two, then one per channel a week later'


def test_an_info_card_that_was_declared_fixed_and_comes_back_is_posted_again_inside_the_repeat_days(store: Store):
    # given
    t = Transport()
    routes = routes_of(store, t, route('seo', SEO, tags='seo'), repeat_days=7)
    key = 'log|a|ERROR|back'
    store.save(state(open_card(key, tags=tag('seo'), business=said('The product page does not open.', kind='info'))), None)
    now = time.time()

    # when
    first = routes.tagged(store.load()['anomalies'], now=now)
    store.anomalies.update_one({'_id': key}, {'$set': {'status': 'cleared', 'clearedAt': iso_at(now + 600)}})
    fixed = routes.tagged(store.load()['anomalies'], now=now + 1300)
    store.anomalies.update_one({'_id': key}, {'$set': {'status': 'open', 'firstSeen': iso_at(now + 2 * 86400)}, '$unset': {'clearedAt': ''}})
    back = routes.tagged(store.load()['anomalies'], now=now + 2 * 86400 + 60)
    again = routes.tagged(store.load()['anomalies'], now=now + 2 * 86400 + 120)

    # then
    assert first == {'teams/seo': [key]} and fixed == {'teams/seo:fixed': [key]}
    assert back == {'teams/seo': [key]}, 'the channel ended on the fixed post, so the returning problem is said again'
    assert again == {} and len(t.calls) == 3


def test_admins_add_test_send_and_remove_routes_in_integrations_and_never_see_a_webhook(store: Store, tmp_path):
    # given
    t = Transport()
    store.save({**state(open_card('log|a|ERROR|x', 'warn')), 'initialized': True}, None)

    # when
    with TestClient(create_app(settings(), store, kube=Kube(root=tmp_path), transport=t)) as c:
        add_route(c, 'tech', TECH, keys='*', min_sev='crit')
        no_hook = c.post('/api/admin/plugins/teams/instances', json={'name': 'seo', 'values': {'tags': 'seo'}, 'enabled': True}, headers=AUTH)
        bad_name = c.post('/api/admin/plugins/teams/instances', json={'name': 'SEO team', 'values': {'webhook': SEO, 'tags': 'seo'}, 'enabled': True}, headers=AUTH)
        added = add_route(c, 'seo', SEO, tags='seo')
        moved = c.patch('/api/admin/plugins/teams/instances/tech', json={'values': {'min-sev': 'warn'}}, headers=AUTH)
        tested = c.post('/api/admin/plugins/teams/test', json={'name': 'seo'}, headers=AUTH)
        sent = c.post('/api/routes/send', json={'key': 'log|a|ERROR|x', 'route': 'teams/tech'}, headers=AUTH)
        listed = c.get('/api/admin/plugins', headers=AUTH)
        removed = c.delete('/api/admin/plugins/teams/instances/tech', headers=AUTH)
        gone = operator_me(c, store)['routes']

    # then
    assert (no_hook.status_code, bad_name.status_code) == (422, 422)
    assert added['values'] == {'tags': 'seo'} and added['secrets'] == {'webhook': {'set': True, 'from': None}}
    assert moved.json()['instance']['values']['min-sev'] == 'warn'
    assert tested.json()['ok'] is True and t.calls[0][0] == SEO
    assert sent.json() == {'ok': True, 'sent': ['teams/tech']} and t.calls[1][0] == TECH
    assert 'sig=' not in listed.text and sorted(i['name'] for i in {p['id']: p for p in listed.json()['plugins']}['teams']['instances']) == ['seo', 'tech']
    assert removed.status_code == 204 and [r['name'] for r in gone] == ['seo']
    assert all('sig=' not in a['text'] for a in store.list_activity(None, 50))


def test_a_route_mentions_people_by_email_and_drops_what_is_not_one(store: Store, caplog):
    # when
    with caplog.at_level(logging.WARNING, logger='skopax.runtime'):
        found = runtime_of(store, Transport(), route('seo', SEO, tags='seo', mentions=f' Alice.Archer@example.com , {BOB}, alice'), route('tech', TECH, keys='*')).routes()

    # then
    assert [r.mentions for r in found] == [(ALICE, BOB), ()]
    assert 'route seo mentions 1 entry that is not an email address' in caplog.text


def test_a_route_of_tag_rules_takes_no_card_and_its_health_says_what_to_write(store: Store, caplog):
    # given
    runtime = runtime_of(store, Transport(), route('seo', SEO, tags='gone, type:crux, type:safebrowsing'), route('good', SEO, tags='seo'))

    # when
    with caplog.at_level(logging.WARNING, logger='skopax.runtime'):
        found = runtime.routes()

    # then
    assert [r.id for r in found] == ['teams/good']
    assert runtime.route_problem(teams.PLUGIN, runtime.instances.get('teams', 'seo')) == msg('integrations.route_empty')
    assert 'route seo takes gone, type:crux, type:safebrowsing, which no card can carry' in caplog.text


def test_routes_that_share_a_webhook_share_its_hash_and_never_show_it(store: Store):
    # when
    found = {r.instance: r for r in runtime_of(store, Transport(), route('tech', TECH, keys='*', sev='crit'), route('seo', SEO, tags='seo'), route('marketing', SEO, tags='marketing')).routes()}

    # then
    assert found['seo'].link == found['marketing'].link != found['tech'].link and len(found['seo'].link) == 16
    assert 'sig=' not in str(found)


@pytest.mark.parametrize('url', [
    SEO.replace('sig=SEO', 'sig=SE CRET'),
    SEO.replace('/workflows/', '/work\tflows/'),
    SEO.replace('https://', 'https://user:pw@'),
    SEO.replace('example.com/', 'example.com:abc/'),
    SEO.replace('example.com/', 'example.com:99999/'),
    SEO.replace('invoke', 'invoke\u2013x'),
    'http://hooks.example.com/workflows/seo/invoke?sig=CRET',
])
def test_a_webhook_the_http_client_cannot_send_is_refused_without_echoing_it(store: Store, url: str, caplog):
    # given
    t = Transport()
    runtime = runtime_of(store, t, route('seo', url, tags='seo'))

    # when
    with caplog.at_level(logging.WARNING):
        found = runtime.routes()
        problem = runtime.route_problem(teams.PLUGIN, runtime.instances.get('teams', 'seo'))
        tested = runtime.connection_test(teams.PLUGIN, 'seo', {}, 5, NOW)

    # then
    assert found == [] and problem == msg('integrations.route_link') and t.calls == []
    assert (tested['ok'], tested['summary']) == (False, msg('integrations.route_link'))
    assert 'CRET' not in problem + caplog.text + tested['summary']


def test_a_clean_webhook_with_a_port_or_spaces_around_it_is_still_taken(store: Store):
    # when
    found = runtime_of(store, Transport(), route('seo', f'  {SEO}  ', tags='seo'), route('port', SEO.replace('example.com/', 'example.com:443/'), tags='seo')).routes()

    # then
    assert [r.instance for r in found] == ['port', 'seo']


def test_a_route_takes_a_tag_in_any_case_and_drops_one_no_card_can_carry(store: Store, caplog):
    # given
    t = Transport()
    store.save(state(open_card('log|a|ERROR|x', tags=tag('seo'), business=said('Add a redirect.'))), None)

    # when
    with caplog.at_level(logging.WARNING, logger='skopax.runtime'):
        routes = routes_of(store, t, route('growth', SEO, tags='tag:SEO,  Tag: Marketing , unknown'), route('odd', SEO, tags='tag:finance'))
        found = routes.routes()
        sent = routes.tagged(store.load()['anomalies'])

    # then
    assert [(r.instance, r.tags, r.business) for r in found] == [('growth', frozenset({'seo', 'marketing'}), True)]
    assert sent == {'teams/growth': ['log|a|ERROR|x']}
    assert 'route growth takes unknown, which no card can carry' in caplog.text and 'route odd is skipped' in caplog.text


def test_a_state_save_with_a_new_push_goes_to_the_key_routes_once_without_a_push_user(store: Store, tmp_path):
    # given
    t = Transport()
    at = iso_at(time.time() - 60)
    fresh = open_card('log|checkout-service|ERROR|boom', 'crit', firstSeen=at, lastSeen=at, pushedAt=at, pushedSev='crit', pushedPeak=1)

    # when
    with TestClient(create_app(settings(), store, kube=Kube(root=tmp_path), transport=t)) as c:
        add_route(c, 'tech', TECH, keys='log|*')
        saved = [c.put('/api/state', json={'initialized': initialized, 'anomalies': cards, 'sweptIso': iso_at(time.time())}, headers=AUTH).status_code
                 for initialized, cards in ((False, []), (True, []), (True, [fresh]), (True, [fresh]))]
        assert c.app.state.routes.drain(5)

    # then
    assert saved == [200, 200, 200, 200]
    assert [u for u, _ in t.calls] == [TECH], 'the new push goes to the route once, with no push user set'
    assert [p['route'] for p in store.anomaly('log|checkout-service|ERROR|boom')['teamsPosts']] == ['tech']


def test_an_empty_route_severity_uses_the_teams_setting_and_a_key_route_own_severity_replaces_it_even_below(store: Store):
    # given
    t = Transport()
    routes = routes_of(store, t, route('quiet', MARKETING, keys='log|quiet|*'), route('loud', DATA, keys='log|loud|*', sev='info'), old=False, min_sev='crit')
    store.save(state(card('log|quiet|ERROR|a', 'warn'), card('log|quiet|ERROR|b', 'crit'), card('log|loud|ERROR|c', 'info')), None)

    # when
    sent = routes.deliver([], NOW, pushed=store.load()['anomalies'])

    # then
    assert sent == 1 and [u for u, _ in t.calls] == [MARKETING]
    assert [p['route'] for p in store.anomaly('log|quiet|ERROR|b').get('teamsPosts') or []] == ['quiet'] and not store.anomaly('log|quiet|ERROR|a').get('teamsPosts')
    assert not store.anomaly('log|loud|ERROR|c').get('teamsPosts'), 'even a key route set to info never posts information'


def test_the_severity_help_says_what_empty_means_and_that_a_key_route_own_value_replaces_the_setting():
    # given
    field = next(f for f in teams.PLUGIN.fields if f.key == 'min-sev')
    setting = next(s for s in teams.PLUGIN.settings if s.key == 'min-sev')

    # then
    assert 'empty uses the Teams lowest severity setting' in field.help
    assert 'can only raise it' not in setting.help and "a key route's own lowest severity replaces it" in setting.help
