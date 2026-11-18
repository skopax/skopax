import dataclasses

from tests.conftest import anomaly, state
from tests.test_auth import TokenEndpoint, access_token, app_client, log_in, sso_settings
from skopax.store import Store

KEY = 'log|checkout-service|ERROR|boom'


def test_a_login_without_any_role_assigns_and_acts_on_a_card(store: Store):
    # given
    store.save({**state(anomaly(KEY, num=14)), 'initialized': True}, None)
    client = app_client(store, TokenEndpoint(access_token([])))
    log_in(client)

    # when
    assigned = client.put('/api/assignee', json={'key': KEY, 'me': True})
    acted = client.post('/api/actions', json={'type': 'resolve', 'key': KEY, 'target': KEY, 'label': 'Resolve'})
    anonymous = app_client(store).put('/api/assignee', json={'key': KEY, 'me': True})
    costs = client.get('/api/costs', params={'days': 7})
    guest_costs = app_client(store).get('/api/costs', params={'days': 7})

    # then
    assert assigned.status_code == 200, assigned.text
    assert store.anomaly(KEY)['owner']
    assert acted.status_code == 201 and acted.json()['status'] == 'done'
    assert anonymous.status_code == 401
    assert costs.status_code == 200 and guest_costs.status_code == 401, 'costs are open to every login, not only operators'


def test_a_login_continues_a_shared_chat_through_the_link_and_lands_in_its_new_private_chat(store: Store):
    # given
    owner = 'nina.white@example.com'
    store.add_action({'type': 'ask', 'source': 'chat', 'key': 'ask', 'target': 'chat:shared-chat-01', 'text': 'Why is this card open?', 'cards': [], 'requestedBy': owner, 'chat': {'id': 'shared-chat-01'}}, 'done', 'answer')
    store.share_chat(owner, 'shared-chat-01', True)
    client = app_client(store, TokenEndpoint(access_token([])), s=dataclasses.replace(sso_settings(), chat_ui=True))
    log_in(client)

    # when
    r = client.get('/chat-continue/shared-chat-01', follow_redirects=False)
    fresh = r.headers['location'].rsplit('/', 1)[-1]

    # then
    assert r.status_code == 303 and r.headers['location'].startswith('/chat/thread/')
    assert store.chat_owner(fresh) not in (None, owner)


def test_stopping_or_resuming_the_investigator_is_for_admins_only(store: Store):
    # given
    member = app_client(store, TokenEndpoint(access_token([])))
    log_in(member)

    # when
    stopped = member.put('/api/investigate', json={'stopped': True})

    # then
    assert stopped.status_code == 403 and stopped.json()['detail'] == 'This needs the admin role.'
    assert store.investigator_stop() is None, 'the investigator keeps running'
