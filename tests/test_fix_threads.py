import pytest

from tests.conftest import AUTH, anomaly, state
from tests.test_slack import FakeSlack, client_with
from skopax.slack import SlackError
from skopax.store import Store, keep_slack_state

KEY = 'log|checkout-service|WARN|store-by-slug NOT_FOUND'
FIX = {'label': 'messages.properties:18', 'repo': 'checkout-service', 'path': 'src/main/resources/messages/messages.properties', 'lines': '18',
       'note': 'An unescaped apostrophe makes MessageFormat drop the {0} slug.', 'url': 'https://gitlab.com/acme/shop/checkout-service/-/blob/5a697887/x#L18', 'sha': '5a697887',
       'current': "error.get.store-by-slug=No store has the slug's value. storeSlug: {0}",
       'suggestion': "error.get.store-by-slug=No store has the slug''s value. storeSlug: {0}"}
LINK = {'label': 'Kibana', 'url': 'https://kibana.prod.example.com/x'}


class ThreadFails(FakeSlack):
    def __init__(self):
        super().__init__()
        self.fail = True

    def __call__(self, token: str, payload: dict) -> dict:
        self.calls.append((token, payload))
        if self.fail and payload.get('thread_ts'):
            raise SlackError('channel_not_found')
        return {'ok': True, 'channel': 'C123', 'ts': '1791900000.000100'}


@pytest.fixture
def card(store: Store) -> Store:
    store.save(state(anomaly(KEY, sev='warn', svc='checkout-service', label='WARN pattern', now=459, d1=198, d7=208, unit='', firstSeen='2026-11-09T16:32:00Z', links=[LINK],
                             code=[FIX, {'label': 'StoreService.kt:20-24', 'repo': 'checkout-service', 'path': 'StoreService.kt', 'lines': '20-24', 'url': 'https://gitlab.com/x', 'note': 'throws'}])), None)
    return store


def test_a_posted_card_gets_its_suggested_fix_in_the_thread_once(card: Store):
    # given
    transport = FakeSlack()

    # when
    with client_with(card, transport) as client:
        client.post('/api/slack-posts', json={'key': KEY}, headers=AUTH)
        client.post('/api/slack-posts', json={'key': KEY}, headers=AUTH)

    # then
    assert len(transport.calls) == 3, 'the card twice, the fix once'
    _, reply = transport.calls[1]
    assert (reply['channel'], reply['thread_ts']) == ('C123', '1791900000.000100')
    assert reply['text'] == 'Suggested fix (untested): src/main/resources/messages/messages.properties:18'
    texts = [b.get('text', {}).get('text') or b.get('elements', [{}])[0].get('text') for b in reply['blocks']]
    assert texts[0] == (':bulb: *Suggested fix (untested)* · <https://gitlab.com/acme/shop/checkout-service/-/blob/5a697887/x#L18|checkout-service · '
                        'src/main/resources/messages/messages.properties:18> @ `5a697887`')
    assert texts[1] == 'An unescaped apostrophe makes MessageFormat drop the {0} slug.'
    assert texts[2] == "```- error.get.store-by-slug=No store has the slug's value. storeSlug: {0}\n+ error.get.store-by-slug=No store has the slug''s value. storeSlug: {0}```"
    assert reply['blocks'][-1]['elements'][0]['text']['text'] == 'Open in Skopax'
    assert 'StoreService.kt' not in str(reply), 'a code link without a suggestion stays on the card'
    assert len(card.anomaly(KEY)['fixPosts']) == 1
    assert [a['text'] for a in card.list_activity(None, 20) if a['who'] == 'server' and a['kind'] == 'slack'] == ['suggested fix posted in the #ops thread · src/main/resources/messages/messages.properties']


def test_a_failed_thread_post_is_taken_back_and_goes_out_on_the_next_try(card: Store):
    # given
    transport = ThreadFails()

    # when
    with client_with(card, transport) as client:
        client.post('/api/slack-posts', json={'key': KEY}, headers=AUTH)
        after_failure = card.anomaly(KEY).get('fixPosts') or []
        transport.fail = False
        again = client.app.state.post_fixes(KEY)
        twice = client.app.state.post_fixes(KEY)

    # then
    assert after_failure == []
    assert (again, twice) == (1, 0)
    assert sum(1 for _, p in transport.calls if p.get('thread_ts')) == 2


def test_nothing_goes_to_a_thread_before_the_card_is_posted_and_the_marks_survive_a_state_save(card: Store):
    # given
    transport = FakeSlack()

    # when
    with client_with(card, transport) as client:
        before = client.app.state.post_fixes(KEY)

    stored = {'fixPosts': ['a', 'b'], 'slackPost': {'ts': '1', 'at': '2026-11-09T17:00:00Z'}}
    kept = keep_slack_state({'key': KEY, 'fixPosts': ['a'], 'slackPost': {'ts': '1', 'at': '2026-11-09T17:00:00Z'}}, stored)

    # then
    assert before == 0 and transport.calls == []
    assert kept['fixPosts'] == ['a', 'b']
