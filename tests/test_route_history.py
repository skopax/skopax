from fastapi.testclient import TestClient

from skopax.app import create_app
from skopax.kube import Kube
from skopax.store import Store
from tests.conftest import AUTH, settings, state
from tests.teams_kit import SEO, Transport, add_route, open_card, tag

ONE = 'log|checkout-service|ERROR|one'
TWO = 'log|checkout-service|ERROR|two'
SAID = {'text': 'We recommend adding a redirect for the old spring sale page.', 'by': 'pod', 'at': '2026-11-09T16:00:00Z'}


def routes_lines(c: TestClient, key: str) -> list[str]:
    answer = c.get('/api/card-history', params={'key': key}, headers=AUTH)
    assert answer.status_code == 200, answer.text
    return [r['text'] for r in answer.json()['activity'] if r['kind'] == 'routes']


def test_a_route_send_shows_in_the_history_of_every_card_it_covered(store: Store, tmp_path):
    # given
    store.save({**state(open_card(ONE, tags=tag('seo'), business=SAID), open_card(TWO, tags=tag('seo'), business=SAID), open_card('log|a|ERROR|other')), 'initialized': True}, None)

    # when
    with TestClient(create_app(settings(), store, kube=Kube(root=tmp_path), transport=Transport())) as c:
        add_route(c, 'seo', SEO, tags='seo')
        sent = [c.post('/api/routes/send', json={'key': key, 'route': 'teams/seo'}, headers=AUTH) for key in (ONE, TWO)]
        store.add_activity('routes', 'server', 'Microsoft Teams post · seo · 2 card(s)', keys=[ONE, TWO])
        lines = {key: routes_lines(c, key) for key in (ONE, TWO, 'log|a|ERROR|other')}

    # then
    assert [r.status_code for r in sent] == [200, 200]
    assert lines[ONE] == ['Microsoft Teams post · seo · 2 card(s)', 'Microsoft Teams post · seo · 1 card(s)']
    assert lines[TWO] == ['Microsoft Teams post · seo · 2 card(s)', 'Microsoft Teams post · seo · 1 card(s)']
    assert lines['log|a|ERROR|other'] == []


def test_the_activity_has_an_index_for_the_cards_a_row_covers(store: Store):
    # when
    store.ensure_indexes()

    # then
    assert any(i['key'][0][0] == 'keys' for i in store.activity.index_information().values())
