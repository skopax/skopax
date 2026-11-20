import dataclasses
import threading
import time

import anyio
import jwt
import pytest
from fastapi.testclient import TestClient
from pymongo.errors import AutoReconnect

from tests.conftest import AUTH, anomaly, ready_app, settings, state
from tests.fake_anthropic import reply, text, tool
from tests.test_auth import sso_login_as
from tests.test_chat import chat_investigator
from tests.test_investigator import NOW, seed
from tests.sso_kit import KEY, KEY_FP, OTHER_KEY, FakeIdp, idp_settings
from skopax import clock, costs, mcpserver
from skopax.app import create_app
from skopax.config import load_settings
from skopax.features import off_text
from skopax.kube import Kube
from skopax.mcpauth import McpAuth, McpUser
from skopax.sso import SignOn, SsoConfig, SsoHttp
from skopax.messages import msg
from skopax.store import Store
from skopax.users import Users

ISSUER = 'https://login.prod.example.com'
IDP = FakeIdp()
NINA = 'nina.white@example.com'
PAUL = 'paul.stone@example.com'
CARD = 'log|checkout-service|ERROR|x'
OTHER_CARD = 'prom|payments-api|5xx|y'
HEADERS = {'Accept': 'application/json, text/event-stream', 'Content-Type': 'application/json', 'MCP-Protocol-Version': '2025-06-18'}
METADATA = 'https://skopax.example.com/.well-known/oauth-protected-resource'


def mcp_settings(idp: FakeIdp = IDP, **over):
    return idp_settings(idp, **({'mcp_issuer': ISSUER} | over))


def token(sub: str = NINA, roles=('SKOPAX_MCP', 'SKOPAX_OPERATOR'), aud: str = 'skopax-mcp', iss: str = ISSUER, key=KEY, ttl: int = 300, **claims) -> str:
    now = int(time.time())
    return jwt.encode({'sub': sub, 'aud': aud, 'iss': iss, 'iat': now, 'exp': now + ttl, 'roles': list(roles), **claims}, key, algorithm='RS256', headers={'kid': IDP.kid})


def mcp_client(store: Store, s=None, tmp_path=None, ready: bool = False, idp: FakeIdp = IDP) -> TestClient:
    s = s or mcp_settings(idp)
    return TestClient((ready_app if ready else create_app)(s, store, sso_http=SsoHttp(fetch=idp.fetch, exchange=idp.exchange), kube=Kube(root=tmp_path) if tmp_path else None))


def rpc(client: TestClient, method: str, params: dict | None = None, bearer: str | None = None):
    headers = HEADERS | ({'Authorization': f'Bearer {bearer}'} if bearer else {})
    return client.post('/mcp', json={'jsonrpc': '2.0', 'id': 1, 'method': method, 'params': params or {}}, headers=headers)


def call(client: TestClient, bearer: str, name: str, args: dict | None = None) -> tuple[str, bool]:
    res = rpc(client, 'tools/call', {'name': name, 'arguments': args or {}}, bearer)
    assert res.status_code == 200, res.text
    result = res.json()['result']
    return result['content'][0]['text'], bool(result.get('isError'))


def numbered(store: Store, *cards: dict) -> None:
    for a in cards:
        if a.get('num') is not None:
            store.card_ids.insert_one({'_id': a['key'], 'n': a['num']})


def board(store: Store, *cards: dict) -> None:
    store.save({**state(*cards), 'initialized': True}, None)
    numbered(store, *cards)


def cards() -> tuple[dict, ...]:
    return (anomaly(CARD, sev='crit', num=14, what='checkout-service ERROR lines went up 40 times'),
            anomaly(OTHER_CARD, sev='warn', num=15, svc='payments-api', label='5xx rate', what='5xx rate is high'),
            anomaly('log|old|x|z', sev='info', status='cleared', num=16, svc='old-service', owner='paul.stone@example.com'))


def test_without_a_token_mcp_answers_401_with_the_resource_metadata_and_the_metadata_points_at_the_login_host(store: Store):
    # given
    client = mcp_client(store)

    # when
    with client as c:
        bare = rpc(c, 'initialize', {'protocolVersion': '2025-06-18', 'capabilities': {}, 'clientInfo': {'name': 'claude-code', 'version': '2'}})
        meta = c.get('/.well-known/oauth-protected-resource').json()
        suffixed = c.get('/.well-known/oauth-protected-resource/mcp').json()

    # then
    assert bare.status_code == 401
    assert bare.headers['www-authenticate'] == f'Bearer resource_metadata="{METADATA}", scope="mcp"'
    assert meta == suffixed == {'resource': 'https://skopax.example.com/mcp', 'authorization_servers': [ISSUER], 'scopes_supported': ['mcp'],
                                'bearer_methods_supported': ['header'], 'resource_name': 'Skopax'}


def test_a_bad_token_is_401_invalid_token_and_a_token_without_the_role_is_403(store: Store):
    # given
    client = mcp_client(store)
    bad = {'other key': token(key=OTHER_KEY), 'page client': token(aud='skopax'), 'other issuer': token(iss='https://evil.example'), 'expired': token(ttl=-120), 'garbage': 'not-a-jwt'}

    # when
    with client as c:
        refused = {name: rpc(c, 'tools/list', bearer=t) for name, t in bad.items()}
        no_role = rpc(c, 'tools/list', bearer=token(roles=('PUBLIC',)))

    # then
    for name, res in refused.items():
        assert res.status_code == 401, name
        assert res.headers['www-authenticate'].startswith('Bearer error="invalid_token"') and f'resource_metadata="{METADATA}"' in res.headers['www-authenticate'], name
    assert no_role.status_code == 403 and 'error="insufficient_scope"' in no_role.headers['www-authenticate']


def test_a_user_disabled_in_the_service_is_refused_while_an_unknown_or_active_one_passes(store: Store):
    # given
    users = Users(store.db)
    users.create(NINA, 'Nina', 'operator', source='sso')
    users.create(PAUL, 'Paul', 'operator', source='sso')
    users.update(NINA, {'disabled': True})

    # when
    with mcp_client(store) as c:
        disabled = rpc(c, 'tools/list', bearer=token(NINA))
        active = rpc(c, 'tools/list', bearer=token(PAUL))
        unknown = rpc(c, 'tools/list', bearer=token('dana.reed@example.com'))

    # then
    assert disabled.status_code == 403 and disabled.json() == {'detail': msg('mcp.disabled')}
    assert disabled.headers['www-authenticate'].startswith('Bearer error="insufficient_scope", error_description="This account is disabled"')
    assert (active.status_code, unknown.status_code) == (200, 200)
    assert (users.get('dana.reed@example.com') or {}).get('source') == 'sso', 'an unknown user passes and is provisioned'


def test_mcp_stays_off_until_sso_is_active(store: Store):
    # given
    unset = mcp_client(store, dataclasses.replace(settings(), sso_jwks_url='http://auth/jwks', mcp_issuer=ISSUER))
    no_secret = mcp_client(store, mcp_settings(sso_client_secret=''))
    active = mcp_client(store)

    # when
    with unset as c:
        endpoint = rpc(c, 'tools/list', bearer=token())
        meta = c.get('/.well-known/oauth-protected-resource')
    with no_secret as c:
        half = rpc(c, 'tools/list', bearer=token())
    with active as c:
        on = rpc(c, 'tools/list', bearer=token())

    # then
    assert (endpoint.status_code, meta.status_code, half.status_code) == (404, 404, 404)
    assert endpoint.json() == {'detail': msg('mcp.not_set_up')}
    assert on.status_code == 200


def test_a_logged_in_client_initializes_and_lists_every_tool_with_all_four_hints(store: Store):
    # given
    client = mcp_client(store)

    # when
    with client as c:
        init = rpc(c, 'initialize', {'protocolVersion': '2025-06-18', 'capabilities': {}, 'clientInfo': {'name': 'claude-code', 'version': '2'}}, token())
        listed = rpc(c, 'tools/list', bearer=token())
        discover = rpc(c, 'server/discover', bearer=token())
        stream = c.get('/mcp', headers={'Accept': 'text/event-stream', 'Authorization': f'Bearer {token()}'})
        anonymous_stream = c.get('/mcp', headers={'Accept': 'text/event-stream'})

    # then
    assert init.status_code == 200 and init.json()['result']['serverInfo']['name'] == 'skopax'
    tools = {t['name']: t for t in listed.json()['result']['tools']}
    assert set(tools) == {'list_cards', 'get_card', 'get_investigation', 'investigator_status', 'list_deploy_reports', 'list_my_asks', 'get_ask', 'list_notifications', 'investigate_card', 'ask',
                          'take_card', 'resolve_card', 'mute_card', 'reopen_card', 'assign_card', 'link_ticket'}
    for name, t in tools.items():
        hints = t['annotations']
        assert all(isinstance(hints.get(h), bool) for h in ('readOnlyHint', 'destructiveHint', 'idempotentHint', 'openWorldHint')), name
    assert [n for n, t in tools.items() if t['annotations']['readOnlyHint']] == ['list_cards', 'get_card', 'get_investigation', 'investigator_status', 'list_deploy_reports', 'list_my_asks', 'get_ask',
                                                                                    'list_notifications']
    assert discover.status_code == 200 and discover.json() == {'jsonrpc': '2.0', 'id': 1, 'error': {'code': -32601, 'message': 'Method not found'}}
    assert (stream.status_code, stream.headers['allow'], anonymous_stream.status_code) == (405, 'POST', 401)


def test_the_mcp_texts_name_the_board_and_notifications_in_the_sidebar():
    # given
    described = {t.name: t.description for t in mcpserver.TOOLS}
    texts = [mcpserver.instructions('')] + list(described.values())

    # then
    assert [t for t in texts if 'Anomalies tab' in t or 'the bell' in t] == []
    assert 'like the Board' in described['list_cards']
    assert 'Notifications in the sidebar' in described['list_notifications'] and 'Notifications in the sidebar' in mcpserver.instructions('')


def test_the_token_maps_to_the_same_login_name_as_the_page_session(store: Store):
    # given
    board(store, *cards())
    s = mcp_settings()
    page, _ = sso_login_as(store, {'sub': NINA, 'name': 'Nina White', 'roles': []})

    # when
    with mcp_client(store, s) as c:
        said, failed = call(c, token(name='Nina White'), 'take_card', {'card': 14, 'ticket': 'OPS-146'})
    done = store.list_actions(None, 10)[0]

    # then
    assert not failed and '#14' in said and 'In progress' in said
    assert done['requestedBy'] == page.get('/api/me').json()['user'] == NINA
    assert (done['type'], done['via'], store.anomaly(CARD)['owner'], store.anomaly(CARD)['ticket']) == ('take', 'mcp', NINA, 'OPS-146')


def test_list_and_get_cards_read_like_the_page(store: Store):
    # given
    board(store, *cards())
    store.note(CARD, 'One client, a retry loop.')

    # when
    with mcp_client(store) as c:
        open_cards, _ = call(c, token(), 'list_cards')
        crit_only, _ = call(c, token(), 'list_cards', {'sev': 'crit'})
        by_service, _ = call(c, token(), 'list_cards', {'filter': 'all', 'service': 'old'})
        one, _ = call(c, token(), 'get_card', {'card': '#14'})
        by_key, _ = call(c, token(), 'get_card', {'card': OTHER_CARD})
        missing, failed = call(c, token(), 'get_card', {'card': 99})

    # then
    assert open_cards.startswith('2 open cards:') and open_cards.index('#14') < open_cards.index('#15') and '#16' not in open_cards
    assert '#14' in crit_only and '#15' not in crit_only
    assert '#16' in by_service and 'cleared' in by_service
    assert one.startswith('Card #14 · crit · checkout-service / ERROR pattern') and 'investigator note: One client, a retry loop.' in one and f'key: {CARD}' in one
    assert by_key.startswith('Card #15 · warn · payments-api / 5xx rate')
    assert failed and 'no card #99' in missing


def test_investigate_card_queues_one_dive_per_card_marked_mcp_within_the_hourly_cap(store: Store):
    # given
    board(store, *cards())

    # when
    with mcp_client(store, mcp_settings(mcp_dive_cap=1)) as c:
        first, _ = call(c, token(), 'investigate_card', {'card': 14})
        again, _ = call(c, token(sub=PAUL), 'investigate_card', {'card': '14'})
        capped, capped_failed = call(c, token(), 'investigate_card', {'card': 15})
        unknown, unknown_failed = call(c, token(), 'investigate_card', {'card': 'no|such'})
        activity = c.get('/api/activity').json()
    with mcp_client(store, mcp_settings(mcp_dive_cap=0)) as c:
        off, off_failed = call(c, token(), 'investigate_card', {'card': 15})
    [dive] = store.list_actions('requested', 10)

    # then
    assert (dive['type'], dive['key'], dive['source'], dive['via'], dive['requestedBy']) == ('dive', CARD, 'mcp', 'mcp', NINA)
    assert f"request {dive['id']} queued, now requested" in first and 'get_card' in first
    assert f"already queued by {NINA}" in again
    assert capped_failed and '1 investigations over MCP an hour' in capped
    assert unknown_failed and 'no card with key no|such' in unknown
    assert off_failed and 'switched off' in off
    assert any(a['kind'] == 'mcp' and a['text'] == f'investigate queued · {CARD} · via MCP' and a['who'] == NINA for a in activity)


def test_investigate_card_is_refused_while_the_ai_investigator_is_off_like_the_page(store: Store):
    # given
    board(store, *cards())

    # when
    with mcp_client(store, mcp_settings(investigator_on=False)) as c:
        text, failed = call(c, token(), 'investigate_card', {'card': 14})
        page = c.post('/api/actions', json={'type': 'dive', 'key': CARD}, headers=AUTH)

    # then
    assert (text, failed) == (off_text('investigator'), True)
    assert (page.status_code, page.json()['detail']) == (409, off_text('investigator'))
    assert store.list_actions(None, 10) == []


def tools_of(store: Store, **over) -> tuple[mcpserver.Tools, list]:
    kicked = []
    bridge = mcpserver.Bridge(store=store, settings=mcp_settings(**over), chat=lambda *a: {}, chat_view=lambda chat: chat, act=lambda *a: {}, assign=lambda *a: {},
                              card_view=lambda *a: {}, board=lambda now: {}, kick=lambda: kicked.append(True), chat_used=lambda by: 0)
    return mcpserver.Tools(bridge, poll_s=0.01), kicked


def test_a_card_action_the_model_refuses_gets_a_fixed_text_never_the_validation_errors(store: Store):
    # given
    board(store, *cards())
    tools, _ = tools_of(store)

    # when
    with pytest.raises(mcpserver.ToolError) as refused:
        tools.card_action(McpUser(name=NINA, sub=NINA, email=NINA, role='operator'), 'mute', 14, hours=10 ** 9, reason='x' * 400)

    # then
    assert refused.value.text == msg('mcp.action_invalid', verb='mute')


def test_investigate_card_with_a_wait_returns_the_finished_run_and_without_one_says_how_to_follow_it(store: Store):
    # given
    board(store, *cards())
    tools, kicked = tools_of(store)
    user = McpUser(name=NINA, sub=NINA, email=NINA, role='operator')

    def finish() -> None:
        [dive] = store.list_actions('requested', 10)
        store.add_investigation({'trigger': 'action', 'actionId': dive['id'], 'keys': [CARD], 'startedAt': '2026-11-11T09:00:00Z', 'outcome': 'done', 'recap': 'One client, a retry loop.',
                                 'model': 'claude-opus-5-5', 'usd': 1.2, 'turns': 4, 'toolCalls': [{'name': 'esql'}, {'name': 'esql'}, {'name': 'kube', 'error': 'x'}], 'source': 'mcp'})
        store.update_action(dive['id'], 'done', 'One client', 'requested')

    tools.b = dataclasses.replace(tools.b, kick=finish)
    waited = anyio.run(tools.run, 'investigate_card', {'card': 14, 'wait_seconds': 5}, user)
    tools.b = dataclasses.replace(tools.b, kick=lambda: kicked.append(True))

    # when
    queued = anyio.run(tools.run, 'investigate_card', {'card': 15}, user)

    # then
    assert 'now done · One client' in waited and 'recap:\nOne client, a retry loop.' in waited and 'tools: esql×2, kube · 1 failed' in waited
    assert 'now requested' in queued and 'get_card' in queued and kicked == [True]


def test_ask_waits_for_the_answer_and_hands_back_the_chat_when_it_is_late(store: Store):
    # given
    board(store, *cards())
    tools, _ = tools_of(store, mcp_wait_seconds=1)
    user = McpUser(name=NINA, sub=NINA, email=NINA, role='operator')

    def post(by: str, question: str, chat_id, keys, answer: str | None = None) -> dict:
        act = store.add_action({'type': 'ask', 'source': 'chat', 'key': 'ask', 'target': 'chat:c1', 'text': question, 'cards': keys, 'requestedBy': by, 'chat': {'id': 'c1'}, 'via': 'mcp'})
        if answer:
            threading.Timer(0.05, lambda: store.update_action(act['id'], 'done', answer, 'requested')).start()
        return store.chat_for(by, 'c1')

    tools.b = dataclasses.replace(tools.b, chat=lambda by, q, chat_id, keys, role: post(by, q, chat_id, keys, 'One client.'))
    answered = anyio.run(tools.run, 'ask', {'question': 'Why?', 'cards': [14]}, user)
    tools.b = dataclasses.replace(tools.b, chat=lambda by, q, chat_id, keys, role: post(by, q, chat_id, keys))

    # when
    late = anyio.run(tools.run, 'ask', {'question': 'And now?', 'chat_id': 'c1', 'wait_seconds': 0}, user)

    # then
    assert '[1] question: Why?' in answered and 'answer:\nOne client.' in answered
    assert '[2] question: And now?' in late and "not ready yet (requested); read it later with get_ask(chat_id='c1')" in late


def test_ask_is_a_private_chat_of_the_asker_marked_mcp_and_another_person_cannot_read_or_continue_it(store: Store, tmp_path):
    # given
    board(store, *cards())

    # when
    with mcp_client(store, tmp_path=tmp_path, ready=True) as c:
        asked, failed = call(c, token(), 'ask', {'question': 'Secret question: why did client 42 get an error?', 'cards': [14], 'wait_seconds': 0})
        [act] = store.list_actions('requested', 10)
        chat_id = act['chat']['id']
        store.update_action(act['id'], 'done', 'Secret answer: client 42.', 'requested')
        mine, _ = call(c, token(), 'get_ask', {'chat_id': chat_id})
        my_list, _ = call(c, token(), 'list_my_asks')
        theirs, theirs_failed = call(c, token(sub=PAUL), 'get_ask', {'chat_id': chat_id})
        their_list, _ = call(c, token(sub=PAUL), 'list_my_asks')
        hijack, hijack_failed = call(c, token(sub=PAUL), 'ask', {'question': 'go on', 'chat_id': chat_id, 'wait_seconds': 0})
        pages = [c.get('/api/actions?limit=500').text, c.get('/api/requests').text, c.get('/api/activity').text, c.get('/api/investigator/status').text,
                 c.get('/api/investigations').text, c.get('/api/costs', headers=AUTH).text]

    # then
    assert not failed and f'Chat {chat_id}' in asked and 'not ready yet' in asked and f"get_ask(chat_id='{chat_id}')" in asked
    assert (act['source'], act['via'], act['requestedBy'], act['cards'], act['target']) == ('chat', 'mcp', NINA, [CARD], f'chat:{chat_id}')
    assert 'Secret answer: client 42.' in mine and '(private to you)' in mine and chat_id in my_list
    assert theirs_failed and 'no such chat' in theirs
    assert 'no chats yet' in their_list
    assert hijack_failed and 'no such chat of yours' in hijack
    for page in pages:
        assert 'Secret' not in page and 'client 42' not in page
    for page in pages[:5]:
        assert NINA not in page and chat_id not in page, 'the open pages do not even say who asked'
    assert store.chats(NINA, 10)[0]['id'] == chat_id, 'the MCP ask shows on the asker\'s own Chat page'


def test_an_ask_or_investigate_says_why_it_waits_when_the_investigator_is_stopped_or_off(store: Store):
    # given
    board(store, *cards())
    store.set_investigator_stop(True, 'admin@example.com')

    # when
    with mcp_client(store, ready=True) as c:
        asked, _ = call(c, token(), 'ask', {'question': 'Why is this card open?', 'wait_seconds': 0})
        dive, _ = call(c, token(), 'investigate_card', {'card': 14})

    # then
    assert 'not ready yet (requested, the investigator is stopped (Stop switch on the page))' in asked
    assert 'now requested (the investigator is stopped)' in dive


def test_ask_is_refused_with_the_reason_while_the_investigator_is_not_ready(store: Store):
    # given
    board(store, *cards())

    # when
    with mcp_client(store) as c:
        asked, failed = call(c, token(), 'ask', {'question': 'Why is this card open?', 'wait_seconds': 0})

    # then
    assert (asked, failed) == (' '.join([msg('chat.not_ready'), msg('features.needs.anthropic_key'), msg('features.needs.mode')]), True)
    assert store.list_actions(None, 10) == []


def test_ask_counts_against_the_chat_daily_cap_and_waits_for_one_answer_at_a_time(store: Store):
    # given
    board(store, *cards())

    # when
    with mcp_client(store, mcp_settings(chat_daily_cap=1), ready=True) as c:
        first, _ = call(c, token(), 'ask', {'question': 'Why is this card open?', 'cards': ['14'], 'wait_seconds': 0})
        chat_id = store.list_actions('requested', 10)[0]['chat']['id']
        busy, busy_failed = call(c, token(), 'ask', {'question': 'and then?', 'chat_id': chat_id, 'wait_seconds': 0})
        capped, capped_failed = call(c, token(), 'ask', {'question': 'new question', 'wait_seconds': 0})
        too_many, schema_failed = call(c, token(), 'ask', {'question': 'x', 'cards': [14, 15, 16, 14]})
        status, _ = call(c, token(), 'investigator_status')

    # then
    assert busy_failed and 'wait for the answer' in busy
    assert capped_failed and '1 chat messages a day is the cap' in capped
    assert schema_failed and 'Input validation error' in too_many
    assert 'your chat messages today 1/1' in status and 'Chat message (private)' in status


def test_card_actions_apply_like_the_page_buttons_and_refuse_what_the_page_refuses(store: Store):
    # given
    board(store, *cards())

    # when
    with mcp_client(store) as c:
        took, _ = call(c, token(), 'take_card', {'card': 14, 'reason': 'looking into it'})
        reopened, _ = call(c, token(), 'reopen_card', {'card': 14})
        resolved, _ = call(c, token(), 'resolve_card', {'card': 15, 'reason': 'one-off'})
        muted, _ = call(c, token(), 'mute_card', {'card': 14, 'hours': 2})
        known, known_failed = call(c, token(), 'mark_known_noise', {'card': 16})
        linked, _ = call(c, token(), 'link_ticket', {'card': 15, 'ticket': 'ops-147'})
        bad_link, bad_link_failed = call(c, token(), 'link_ticket', {'card': 15, 'ticket': 'not a key'})
        long_mute, long_failed = call(c, token(), 'mute_card', {'card': 14, 'hours': 1000})
        assigned, _ = call(c, token(), 'assign_card', {'card': 15, 'assignee': 'me'})
        unassigned, _ = call(c, token(), 'assign_card', {'card': 15, 'assignee': ''})
        bad_email, bad_email_failed = call(c, token(), 'assign_card', {'card': 15, 'assignee': 'Paul', 'email': 'nope'})

    # then
    assert 'In progress' in took and 'reopened' in reopened and 'No action needed · one-off' in resolved
    assert 'muted until' in muted and 'linked to OPS-147' in linked
    assert known == msg('mcp.no_tool', name='mark_known_noise')
    assert known_failed, 'Known noise silences a card for good, so it stays a page and Slack button a person confirms; MCP has no such tool'
    assert store.anomaly(CARD)['muted'] and store.anomaly(OTHER_CARD)['ticket'] == 'OPS-147'
    assert bad_link_failed and 'link needs a Jira key' in bad_link
    assert long_failed and 'Input validation error' in long_mute
    assert f'assigned to {NINA}' in assigned and 'assignee removed' in unassigned and store.anomaly(OTHER_CARD).get('owner') is None
    assert bad_email_failed and 'not an email address' in bad_email
    applied = [a for a in store.list_actions(None, 50) if a['type'] in ('take', 'reopen', 'resolve', 'mute', 'known', 'link')]
    assert applied and all(a['via'] == 'mcp' and a['requestedBy'] == NINA for a in applied)
    assert [a['status'] for a in applied if a['type'] == 'link'] == ['failed', 'done']


def test_deploy_reports_and_investigations_read_through_mcp_and_a_private_chat_run_stays_redacted(store: Store):
    # given
    board(store, *cards())
    store.add_deploy_report({'summary': '2 deploys: 1 clean, 1 flagged', 'brief': {'level': 'warn', 'headline': 'checkout-service errors went up'},
                             'rows': [{'svc': 'checkout-service', 'flags': ['err up']}, {'svc': 'payments-api', 'flags': []}]})
    public = store.add_investigation({'trigger': 'action', 'keys': [CARD], 'startedAt': '2026-11-11T09:00:00Z', 'outcome': 'done', 'recap': 'A retry loop.', 'usd': 0.5, 'toolCalls': [{'name': 'esql'}]})
    private = store.add_investigation({'trigger': 'chat', 'keys': [CARD], 'startedAt': '2026-11-11T09:05:00Z', 'outcome': 'done', 'recap': 'Secret answer', 'ask': {'text': 'Secret question', 'by': NINA}})

    # when
    with mcp_client(store) as c:
        reports, _ = call(c, token(), 'list_deploy_reports')
        run, _ = call(c, token(sub=PAUL), 'get_investigation', {'run_id': public['id']})
        chat_run, _ = call(c, token(sub=PAUL), 'get_investigation', {'run_id': private['id']})

    # then
    assert 'warn · 2 deploys: 1 clean, 1 flagged' in reports and 'flagged: checkout-service' in reports
    assert 'recap:\nA retry loop.' in run and 'tools: esql' in run
    assert 'private chat run' in chat_run and 'Secret' not in chat_run and CARD not in chat_run


def test_mcp_runs_carry_source_mcp_on_the_run_the_ledger_and_activity_and_a_private_ask_keeps_its_cards_out_of_costs(store: Store, tmp_path):
    # given
    seed(store, anomaly(CARD, num=14), history=[])
    numbered(store, anomaly(CARD, num=14))
    with mcp_client(store, tmp_path=tmp_path, ready=True) as c:
        call(c, token(), 'ask', {'question': 'Why did it go up?', 'cards': [14], 'wait_seconds': 0})
    inv = chat_investigator(store, [reply(tool('card', {'key': CARD})), reply(text('One client.'), stop='end_turn')])

    # when
    record = inv.run_once(NOW)
    rows = store.ledger_since('2000-01-01')
    summary = costs.summary(rows, store.run_costs_meta({record['id']}), clock.day(NOW), 1, 0.0, 25.0)
    lines = [a['text'] for a in store.list_activity(None, 50)]

    # then
    assert record['trigger'] == 'chat' and record['source'] == 'mcp'
    assert rows and all(r['source'] == 'mcp' and r['keys'] == [] for r in rows if r.get('runId') == record['id'])
    [row] = [r for r in summary['runs'] if r['id'] == record['id']]
    assert (row['source'], row['what'], row['keys']) == ('mcp', costs.CHAT_WHAT, [])
    assert summary['topCards'] == []
    assert 'chat message · private · via MCP' in lines and 'chat answer · private · via MCP' in lines
    assert [s['source'] for s in summary['sources']][-1] == 'mcp'


def test_the_connect_claude_page_reads_the_address_the_client_id_and_every_tool_from_the_server(store: Store):
    # given
    board(store, *cards())

    # when
    with mcp_client(store) as c:
        info = c.get('/api/mcp/info').json()

    # then
    assert info['enabled'] is True and info['url'] == 'https://skopax.example.com/mcp' and info['clientId'] == 'skopax-mcp'
    assert [t['name'] for t in info['tools']][:3] == ['list_cards', 'get_card', 'get_investigation']
    assert 'mark_known_noise' not in [t['name'] for t in info['tools']]
    assert all(t['what'] and len(t['what']) <= 180 for t in info['tools']), 'one short sentence per tool'
    assert {t['name']: t['readOnly'] for t in info['tools']}['list_cards'] is True and {t['name']: t['readOnly'] for t in info['tools']}['take_card'] is False


def test_the_mcp_info_reports_the_switch_and_the_oauth_metadata_follows_the_switch(store: Store):
    # given
    board(store, *cards())

    # when
    with mcp_client(store) as c:
        on = c.get('/api/mcp/info').json()
        on_meta = c.get('/.well-known/oauth-protected-resource').status_code
    with mcp_client(store, mcp_settings(mcp_on=False)) as c:
        off = c.get('/api/mcp/info').json()
        off_meta = c.get('/.well-known/oauth-protected-resource')
        off_suffixed = c.get('/.well-known/oauth-protected-resource/mcp')
        refused = c.post('/mcp', json={'jsonrpc': '2.0', 'id': 1, 'method': 'tools/list'}, headers=HEADERS | {'Authorization': f'Bearer {token()}'})

    # then
    assert (on['on'], on['enabled'], off['on'], off['enabled']) == (True, True, False, True)
    assert (on_meta, refused.status_code) == (200, 409)
    for res in (off_meta, off_suffixed):
        assert (res.status_code, res.json()) == (409, {'detail': off_text('mcp')}), 'a switched-off MCP feature sends no client into an OAuth login'


def mcp_auth_of(values: dict, idp: FakeIdp) -> McpAuth:
    s = load_settings(lambda: values | {'sso.client-secret': 'client-secret', 'sso.redirect-uri': 'https://skopax.example.com/authorized'}, env={})
    signon = SignOn(lambda: SsoConfig.of(s, s.sso_client_secret), SsoHttp(fetch=idp.fetch), fp_key=lambda: KEY_FP)
    return McpAuth(s, signon, None, 'https://skopax.example.com')


def test_mcp_points_claude_at_the_login_host_the_browser_reaches_when_mcp_issuer_is_not_set():
    # given
    global_name, prod_name = FakeIdp('https://login.example.com'), FakeIdp('https://login.prod.example.com')
    prod = {'sso.issuer': 'https://login.example.com', 'sso.authorize-url': 'https://login.prod.example.com/oauth2/authorize', 'sso.jwks-url': 'https://login.example.com/oauth2/jwks'}

    # when
    page_login = mcp_auth_of(prod, global_name)
    issuer_only = mcp_auth_of({'sso.issuer': 'https://login.prod.example.com/', 'sso.jwks-url': 'https://login.example.com/oauth2/jwks'}, prod_name)
    explicit = mcp_auth_of(prod | {'mcp.issuer': 'https://other.example'}, global_name)
    neither = mcp_auth_of({'sso.jwks-url': 'https://login.example.com/oauth2/jwks'}, global_name)

    # then
    assert page_login.issuer() == 'https://login.prod.example.com' and page_login.enabled, 'the page login authorize host, not its token issuer name login.example.com, which does not resolve'
    assert page_login.metadata()['authorization_servers'] == ['https://login.prod.example.com']
    assert page_login.issuers() == ('https://login.prod.example.com', 'https://login.example.com'), 'tokens from the same auth-service are accepted under either name'
    assert issuer_only.issuer() == 'https://login.prod.example.com' and issuer_only.issuers() == ('https://login.prod.example.com',)
    assert explicit.issuer() == 'https://other.example', 'an explicit mcp.issuer wins'
    assert not neither.enabled


def test_the_metadata_names_the_prod_login_host_and_a_token_under_either_login_name_is_accepted(store: Store):
    # given
    idp = FakeIdp('https://login.example.com')
    client = mcp_client(store, mcp_settings(idp), idp=idp)

    # when
    with client as c:
        meta = c.get('/.well-known/oauth-protected-resource').json()
        prod_name = rpc(c, 'tools/list', bearer=token(iss='https://login.prod.example.com'))
        global_name = rpc(c, 'tools/list', bearer=token(iss='https://login.example.com'))
        other = rpc(c, 'tools/list', bearer=token(iss='https://evil.example'))

    # then
    assert meta['authorization_servers'] == ['https://login.prod.example.com']
    assert (prod_name.status_code, global_name.status_code, other.status_code) == (200, 200, 401)


def test_cards_chats_runs_and_reports_come_with_their_links(store: Store, tmp_path):
    # given
    board(store, *cards())
    report = store.add_deploy_report({'summary': '1 deploy', 'brief': {'level': 'ok'}, 'rows': [{'svc': 'checkout-service', 'flags': []}]})
    run = store.add_investigation({'trigger': 'action', 'keys': [CARD], 'startedAt': '2026-11-11T09:00:00Z', 'outcome': 'done', 'recap': 'A retry loop.', 'usd': 0.5})
    site = 'https://skopax.example.com'

    # when
    with mcp_client(store, tmp_path=tmp_path, ready=True) as c:
        listed, _ = call(c, token(), 'list_cards')
        one, _ = call(c, token(), 'get_card', {'card': 14})
        taken, _ = call(c, token(), 'take_card', {'card': 15})
        asked, _ = call(c, token(), 'ask', {'question': 'why did checkout-service go up?', 'wait_seconds': 0})
        chat_id = store.chats(NINA, 1)[0]['id']
        mine, _ = call(c, token(), 'list_my_asks')
        read, _ = call(c, token(), 'get_investigation', {'run_id': run['id']})
        reports, _ = call(c, token(), 'list_deploy_reports')

    # then
    assert f'link: {site}/c/14' in listed and f'link: {site}/c/15' in listed, 'every listed card has its page'
    assert f'link: {site}/c/14' in one and f'chat about it: {site}/#t=chat/new/14' in one
    assert taken.endswith(f'{site}/c/15'), 'a card action says where to look'
    assert f'link: {site}/#t=chat/thread/{chat_id}' in asked and f'{site}/#t=chat/thread/{chat_id}' in mine, 'the asker gets the chat on the page'
    assert f'{CARD} ({site}/c/14)' in read and f"record: {site}/api/investigations/{run['id']}" in read
    assert f"link: {site}/#r={report['id']}" in reports


def test_a_card_nobody_addressed_is_open_and_investigation_and_stopping_are_properties():
    # given
    cards = {
        'fresh': {'status': 'open', 'sev': 'warn'},
        'looking': {'status': 'open', 'sev': 'warn', 'triage': 'investigating'},
        'dived': {'status': 'open', 'sev': 'crit', 'triage': 'dived'},
        'stopped': {'status': 'cleared', 'sev': 'warn'},
        'stopped dived': {'status': 'cleared', 'sev': 'crit', 'triage': 'dived'},
        'info stopped': {'status': 'cleared', 'sev': 'info'},
        'taken': {'status': 'cleared', 'sev': 'warn', 'triage': 'progress'},
    }

    # when
    states = {name: mcpserver.state_of(a) for name, a in cards.items()}

    # then
    assert states == {
        'fresh': 'open, firing',
        'looking': 'open, firing, being investigated',
        'dived': 'open, firing, investigated, waiting to be addressed',
        'stopped': 'open, stopped firing, waiting to be addressed',
        'stopped dived': 'open, stopped firing, investigated, waiting to be addressed',
        'info stopped': 'cleared',
        'taken': 'in progress',
    }


def test_mcp_refuses_while_the_user_lookup_fails_and_lets_the_answer_decide_after(store: Store, monkeypatch):
    # given
    users = Users(store.db)
    users.create(NINA, 'Nina', 'operator', source='sso')
    users.update(NINA, {'disabled': True})
    real = Users.get
    calls = []

    def down(self, email):
        calls.append(email)
        raise AutoReconnect('mongo is down')

    clock = [time.time()]
    s = mcp_settings()
    client = TestClient(create_app(s, store, sso_http=SsoHttp(fetch=IDP.fetch, exchange=IDP.exchange), clock=lambda: clock[0]))

    # when
    with client as c:
        monkeypatch.setattr(Users, 'get', down)
        failing = rpc(c, 'tools/list', bearer=token(NINA))
        paused = rpc(c, 'tools/list', bearer=token(PAUL))
        looked_up = len(calls)
        monkeypatch.setattr(Users, 'get', real)
        clock[0] += 6
        back = rpc(c, 'tools/list', bearer=token(NINA))

    # then
    assert (failing.status_code, failing.json()) == (503, {'detail': msg('db.unavailable')}), 'a failed lookup refuses instead of letting a maybe-disabled user in'
    assert (paused.status_code, paused.json()) == (503, {'detail': msg('db.unavailable')}) and looked_up == 1, 'inside the pause nobody passes and Mongo is not asked again'
    assert back.status_code == 403, 'once the lookup works the stored doc decides again'
