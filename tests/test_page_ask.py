import dataclasses

from fastapi.testclient import TestClient

from tests.conftest import AUTH, anomaly, settings, state
from tests.fake_anthropic import FakeAnthropic, verdict
from skopax import brand
from skopax.app import create_app
from skopax.investigator import prompt
from skopax.store import Store


def app_with_gate(store: Store, gate_replies: list) -> tuple[TestClient, FakeAnthropic]:
    fake = FakeAnthropic([], gate_replies)
    return TestClient(create_app(dataclasses.replace(settings(), anthropic_api_key='sk-test'), store, anthropic_client=fake)), fake


def ask(client: TestClient, text: str, cards=None):
    return client.post('/api/actions', json={'type': 'ask', 'key': 'ask', 'target': 'ask', 'text': text, 'cards': cards or []}, headers=AUTH)


def test_an_off_topic_page_ask_is_refused_with_the_reason(store: Store):
    # given
    client, _ = app_with_gate(store, [verdict(on_topic=False, reason='asks for a poem')])

    # when
    resp = ask(client, 'Write me a poem')

    # then
    assert resp.status_code == 422
    assert resp.json()['detail'] == 'Only questions about Skopax and the production systems Skopax monitors: asks for a poem'
    assert store.list_actions(None, 5) == []


def test_an_on_topic_page_ask_is_stored_as_a_page_ask_and_the_gate_sees_the_card(store: Store):
    # given
    store.save(state(anomaly('k1', firstSeen='2026-10-24T09:00:00Z')), None)
    client, fake = app_with_gate(store, [verdict()])

    # when
    resp = ask(client, 'Why did checkout-service spike?', ['k1'])

    # then
    assert resp.status_code == 201 and resp.json()['source'] == 'api', 'sent with the API token'
    sent = fake.gate_requests[0]['messages'][0]['content']
    assert sent.startswith('Card: checkout-service / ERROR pattern, warn') and 'Why did checkout-service spike?' in sent
    assert brand.fill(prompt.TAGGED) not in sent and sent.endswith('<message>\nWhy did checkout-service spike?\n</message>')


def test_a_page_ask_goes_through_when_the_gate_is_down(store: Store):
    # given
    client, fake = app_with_gate(store, [])

    # when
    resp = ask(client, 'Why did checkout-service spike?')

    # then
    assert resp.status_code == 201
    assert len(fake.gate_requests) == 1


def test_page_and_slack_asks_have_their_own_caps(store: Store):
    # given
    client, fake = app_with_gate(store, [verdict()])
    for i in range(6):
        store.add_action({'type': 'ask', 'source': 'slack', 'key': 'ask', 'target': f'slack:C1:{i}', 'text': 'q'})

    # when
    open_page = ask(client, 'Why did checkout-service spike?')
    for i in range(5):
        store.add_action({'type': 'ask', 'source': 'page', 'key': 'ask', 'target': 'ask', 'text': 'q'})
    capped = ask(client, 'And now?')

    # then
    assert open_page.status_code == 201
    assert capped.status_code == 429
    assert len(fake.gate_requests) == 1
