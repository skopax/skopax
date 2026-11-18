import pytest
from fastapi.testclient import TestClient

from tests.conftest import AUTH
from tests.fake_anthropic import verdict
from tests.test_auth import ROLE, TokenEndpoint, access_token, app_client, log_in
from tests.test_gate import gate
from skopax.investigator import prompt
from skopax.investigator.gate import EXAMPLES_DEFAULT
from skopax.store import Store, iso, utc_now

EXAMPLES = 'addressed, smallTalk (a word to a teammate, not an opinion about them): "skopax what do you say to Alice", "what do you say to alice skopax"'


def test_with_no_examples_saved_the_gate_sends_its_own_prompt_only(store: Store):
    # given
    g, fake = gate(store, [verdict()])

    # when
    g.check('hey skopax')

    # then
    assert fake.gate_requests[0]['system'] == prompt.gate_system()
    assert g.examples() == EXAMPLES_DEFAULT


def test_saved_examples_follow_the_gate_prompt_and_its_rules_win_over_them(store: Store):
    # given
    store.set_gate_examples({'examples': EXAMPLES, 'updatedAt': '2026-10-28T08:00:00Z', 'updatedBy': 'token'})
    g, fake = gate(store, [verdict()])

    # when
    g.check('what do you say to alice skopax')

    # then
    assert fake.gate_requests[0]['system'] == prompt.gate_system() + prompt.GATE_EXTRA + EXAMPLES == prompt.gate_system(examples=EXAMPLES)
    assert 'one verdict per line' in prompt.GATE_EXTRA and 'the rules win' in prompt.GATE_EXTRA


def test_only_the_service_token_can_change_the_gate_examples(store: Store):
    # given
    operator = app_client(store, TokenEndpoint(access_token([ROLE])))
    log_in(operator)

    # when
    as_operator = operator.put('/api/gate-examples', json={'examples': EXAMPLES})
    as_token = operator.put('/api/gate-examples', json={'examples': f'  {EXAMPLES}\n'}, headers=AUTH)
    read = operator.get('/api/gate-examples').json()

    # then
    assert as_operator.status_code == 401
    assert as_token.status_code == 200
    saved = as_token.json()
    assert saved['examples'] == EXAMPLES and saved['updatedBy'] == 'token:bootstrap' and saved['updatedAt'].startswith(iso(utc_now())[:10])
    assert read == saved
    line = store.list_activity(None, 5)[0]
    assert (line['kind'], line['who'], line['text']) == ('switch', 'token:bootstrap', f'Gate examples · {len(EXAMPLES)} characters')


def test_an_empty_text_clears_the_examples_and_the_gate_prompt_is_its_own_again(store: Store, client: TestClient):
    # given
    client.put('/api/gate-examples', json={'examples': EXAMPLES}, headers=AUTH)
    g, fake = gate(store, [verdict()])

    # when
    cleared = client.put('/api/gate-examples', json={'examples': '   '}, headers=AUTH)
    g.check('hey skopax')

    # then
    assert cleared.status_code == 200 and cleared.json()['examples'] == ''
    assert fake.gate_requests[0]['system'] == prompt.gate_system()


@pytest.mark.parametrize('body', [{'examples': 'x' * 2001}, {}, {'examples': 5}, {'examples': 'ok', 'enabled': True}])
def test_the_gate_examples_body_is_validated(store: Store, client: TestClient, body):
    # when
    resp = client.put('/api/gate-examples', json=body, headers=AUTH)

    # then
    assert resp.status_code == 422
    assert store.gate_examples() is None
