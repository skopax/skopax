import http.client
import logging
import time

from fastapi.testclient import TestClient

from skopax.app import create_app
from skopax.engine import iso as iso_at, merge
from skopax.kube import Kube
from skopax.plugins import teams
from skopax.store import Store
from tests.conftest import AUTH, settings, state
from tests.teams_kit import AT, BIZDEV, SEO, Transport, add_route, open_card, route, routes_of, tag, texts

KEY = 'log|a|ERROR|orders'
SEO_TAG = tag('seo')
INFO = {'text': 'Orders cannot be finished right now.', 'kind': 'info', 'by': 'pod', 'at': AT}
ACTION = {'text': 'We recommend adding a redirect for shop.example.com/products.', 'by': 'pod', 'at': AT}


def found(sev: str = 'warn') -> dict:
    return {'key': KEY, 'src': 'log', 'sig': 'err', 'ns': 'a', 'svc': 'a', 'sub': '', 'sev': sev, 'now': 5, 'd1': None, 'd7': None, 'unit': '', 'label': 'ERROR lines',
            'what': 'x', 'short': 'x', 'slack': 1, 'ranKey': 'log'}


def passed(store: Store, cards: list[dict], at: float, ran: bool = True, said: dict | None = None) -> None:
    st = store.load()
    merge(st, [dict(c) for c in cards], {'log': ran}, at)
    for a in st['anomalies']:
        if said and a['key'] == KEY and a['status'] == 'open' and not a.get('business'):
            a.update(tags=SEO_TAG, business=said)
    store.save(st, None)


def seo(store: Store, transport: Transport, repeat_days: float = 7, **values):
    return routes_of(store, transport, route('seo', SEO, tags='seo'), repeat_days=repeat_days, **values)


def test_an_info_card_that_fires_again_before_its_fixed_post_still_gets_one_when_it_stops(store: Store):
    # given
    t = Transport()
    routes = seo(store, t)
    store.save({**state(), 'initialized': True}, None)
    now = time.time()
    passed(store, [found()], now - 60, said=INFO)
    first = routes.tagged(store.load()['anomalies'], now=now)
    passed(store, [], now + 1200)
    held = routes.tagged(store.load()['anomalies'], now=now + 1300)
    passed(store, [found()], now + 1500)
    refired = routes.tagged(store.load()['anomalies'], now=now + 1560)
    passed(store, [], now + 3600)

    # when
    fixed = routes.tagged(store.load()['anomalies'], now=now + 3600 + 700)
    again = routes.tagged(store.load()['anomalies'], now=now + 3600 + 900)

    # then
    assert first == {'teams/seo': [KEY]} and held == {} and refired == {}, 'the channel already has it, so the quick return is not said twice'
    assert fixed == {'teams/seo:fixed': [KEY]} and again == {}, 'the channel was told the problem is on, so it hears once that it stopped'
    assert [texts(b)[0] for _, b in t.calls] == ['Skopax \u00b7 SEO', 'Skopax \u00b7 SEO \u00b7 fixed']


def test_an_info_card_whose_check_stopped_reporting_gets_no_fixed_post(store: Store):
    # given
    t = Transport()
    routes = seo(store, t)
    store.save({**state(), 'initialized': True}, None)
    now = time.time()
    passed(store, [found()], now - 60, said=INFO)
    routes.tagged(store.load()['anomalies'], now=now)
    passed(store, [], now + 25 * 3600, ran=False)
    card = store.anomaly(KEY)

    # when
    sent = routes.tagged(store.load()['anomalies'], now=now + 25 * 3600 + 700)

    # then
    assert (card['status'], card['expired']) == ('cleared', True)
    assert sent == {} and len(t.calls) == 1, 'nothing confirmed the problem stopped, so the channel is not told it is fixed'


def test_a_card_that_left_the_page_and_fires_again_inside_the_repeat_days_is_not_posted_to_the_same_channel_again(store: Store):
    # given
    t = Transport()
    routes = seo(store, t)
    store.save({**state(), 'initialized': True}, None)
    now = time.time()
    passed(store, [found()], now - 60, said=ACTION)
    first = routes.tagged(store.load()['anomalies'], now=now)
    store.triage(KEY, 'handled', at=iso_at(now + 60))
    passed(store, [], now + 3600)
    passed(store, [], now + 26 * 3600)
    gone = KEY not in [a['key'] for a in store.load()['anomalies']]

    # when
    passed(store, [found()], now + 3 * 86400, said=ACTION)
    back = routes.tagged(store.load()['anomalies'], now=now + 3 * 86400 + 60)
    store.triage(KEY, 'progress', owner='nina.white@example.com')
    taken = routes.tagged(store.load()['anomalies'], now=now + 3 * 86400 + 360)

    # then
    assert first == {'teams/seo': [KEY]} and gone
    assert back == {} and taken == {} and len(t.calls) == 1, 'the channel got this card three days ago and never heard it was fixed, also after a Take wrote its new firing to the history'


def test_a_card_that_was_resolved_left_the_page_and_came_back_gets_its_fixed_post_when_it_really_stops(store: Store):
    # given
    t = Transport()
    routes = seo(store, t)
    store.save({**state(), 'initialized': True}, None)
    now = time.time()
    passed(store, [found()], now - 60, said=INFO)
    routes.tagged(store.load()['anomalies'], now=now)
    store.triage(KEY, 'handled', at=iso_at(now + 60))
    passed(store, [], now + 25 * 3600)
    passed(store, [], now + 50 * 3600)
    passed(store, [found()], now + 3 * 86400, said=INFO)
    held = routes.tagged(store.load()['anomalies'], now=now + 3 * 86400 + 60)
    store.triage(KEY, 'progress', owner='nina.white@example.com')
    passed(store, [], now + 3 * 86400 + 3600)

    # when
    fixed = routes.tagged(store.load()['anomalies'], now=now + 3 * 86400 + 3600 + 700)

    # then
    assert held == {} and fixed == {'teams/seo:fixed': [KEY]}, 'the channel still believed the problem was on, so it hears once that it stopped'
    assert [texts(b)[0] for _, b in t.calls] == ['Skopax \u00b7 SEO', 'Skopax \u00b7 SEO \u00b7 fixed']


def test_a_business_post_sent_by_hand_carries_the_merged_cards_and_the_team_title_like_the_automatic_one(store: Store, tmp_path):
    # given
    t = Transport()
    parent = open_card('log|a|ERROR|host', tags=SEO_TAG, business=ACTION)
    kid = open_card('log|a|ERROR|kid', parent=parent['key'], business={'text': 'We recommend adding a redirect for shop.example.com/cards too.', 'by': 'pod', 'at': AT})
    quiet = open_card('log|a|ERROR|quiet', parent=parent['key'])
    store.save({**state(parent, kid, quiet), 'initialized': True}, None)

    # when
    with TestClient(create_app(settings(), store, kube=Kube(root=tmp_path), transport=t)) as c:
        add_route(c, 'growth', SEO, tags='seo, marketing')
        sent = c.post('/api/routes/send', json={'key': parent['key'], 'route': 'teams/growth'}, headers=AUTH)

    # then
    assert sent.json() == {'ok': True, 'sent': ['teams/growth']}
    lines = texts(t.calls[0][1])
    assert lines[:3] == ['Skopax \u00b7 SEO', '**Recommended:** ' + ACTION['text'], '\u2022 We recommend adding a redirect for shop.example.com/cards too.']
    assert [p['route'] for p in store.anomaly(parent['key'])['teamsPosts']] == ['growth']


def test_a_tagged_post_after_an_operator_tags_a_card_carries_its_merged_cards(store: Store):
    # given
    t = Transport()
    routes = routes_of(store, t, route('bizdev', BIZDEV, tags='bizdev'))
    host = open_card('log|a|ERROR|host', tags=tag('bizdev', by='nina.white@example.com'), business=ACTION)
    kid = open_card('log|a|ERROR|kid', parent=host['key'], business={'text': 'We recommend updating the campaign page too.', 'by': 'pod', 'at': AT})
    store.save(state(host, kid), None)

    # when
    sent = routes.tagged([store.anomaly(host['key'])], 'nina.white@example.com')

    # then
    assert sent == {'teams/bizdev': [host['key']]}
    assert texts(t.calls[0][1])[2] == '\u2022 We recommend updating the campaign page too.'


def test_a_markdown_link_in_a_stored_sentence_reaches_the_business_channel_as_plain_text_with_its_address(store: Store):
    # given
    t = Transport()
    routes = routes_of(store, t, route('bizdev', BIZDEV, tags='bizdev'))
    told = {'text': 'For the campaign [click here](https://evil.example/login) and update the page.', 'kind': 'info',
            'why': 'The old [campaign [2025]](https://evil.example/old) link opens an empty page.', 'by': 'nina.white@example.com', 'at': AT}
    kid = open_card('log|a|ERROR|kid', parent=KEY, business={'text': 'Add a redirect for the [products page](https://www.example.com/products), instead of [old [[x]] link](https://evil.example/x).',
                                                             'by': 'pod', 'at': AT})
    store.save(state(open_card(KEY, tags=tag('bizdev', by='nina.white@example.com'), business=told), kid), None)
    now = time.time()
    routes.tagged(store.load()['anomalies'], now=now)
    store.anomalies.update_one({'_id': KEY}, {'$set': {'status': 'cleared', 'clearedAt': iso_at(now + 60)}})

    # when
    routes.tagged(store.load()['anomalies'], now=now + 760)

    # then
    posted, fixed = texts(t.calls[0][1]), texts(t.calls[1][1])
    assert posted[1:4] == ['**Info:** For the campaign click here (https://evil.example/login) and update the page.',
                           'Why: The old campaign [2025] (https://evil.example/old) link opens an empty page.',
                           '\u2022 Add a redirect for the products page (https://www.example.com/products), instead of [old [[x]] link] (https://evil.example/x).']
    assert fixed[1] == '\u2705 **Fixed:** For the campaign click here (https://evil.example/login) and update the page.'


class Refusing:
    def __init__(self):
        self.calls = 0

    def __call__(self, method, url, headers, data, timeout):
        self.calls += 1
        if 'sig=SEO' in url:
            raise http.client.InvalidURL(f"URL can't contain control characters. {url.split('/', 3)[3]!r} (found at least ' ')")
        return Transport()(method, url, headers, data, timeout)


def test_a_link_the_http_client_refuses_fails_that_post_only_and_keeps_its_secret_out_of_the_log(store: Store, caplog):
    # given
    t = Refusing()
    routes = routes_of(store, t, route('seo', SEO, tags='seo'), route('bizdev', BIZDEV, tags='bizdev'), route('tech', BIZDEV, keys='*', sev='warn'))
    store.save(state(open_card(KEY, tags=SEO_TAG, business=ACTION), open_card('log|a|ERROR|biz', tags=tag('bizdev'), business=ACTION)), None)

    # when
    with caplog.at_level(logging.WARNING, logger='skopax.routes'):
        tagged = routes.tagged(store.load()['anomalies'])
        live = {r.instance: r for r in routes.routes()}
        pushed = routes.push([open_card('log|b|ERROR|x')], [live['seo'], live['tech']])

    # then
    assert tagged == {'teams/bizdev': ['log|a|ERROR|biz']} and pushed == {'teams/tech': ['log|b|ERROR|x']}
    assert not [p for p in store.anomaly(KEY).get('teamsPosts') or [] if p['route'] == 'seo'], 'the claim is taken back, so the next pass tries again'
    assert 'route teams/seo post failed: InvalidURL' in caplog.text and 'sig=' not in caplog.text


def test_a_business_channel_only_gets_cards_at_the_teams_lowest_severity_or_above(store: Store):
    # given
    t = Transport()
    routes = seo(store, t, min_sev='warn')
    store.save({**state(), 'initialized': True}, None)
    now = time.time()
    passed(store, [found('info')], now - 60, said=ACTION)

    # when
    quiet = routes.tagged(store.load()['anomalies'], now=now)
    passed(store, [found('warn')], now + 60)
    loud = routes.tagged(store.load()['anomalies'], now=now + 120)

    # then
    assert quiet == {} and loud == {'teams/seo': [KEY]}, 'the route itself takes info, the Teams floor holds the info card back until it is a warning'
    assert {s.key: s.default for s in teams.PLUGIN.settings}['min-sev'] == 'warn', 'warn and up is the default'
