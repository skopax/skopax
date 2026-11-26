import asyncio
import dataclasses
import json
import os
import pathlib
import subprocess
import sys
import tomllib

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tests.conftest import AUTH, anomaly, ready_app, settings, state
from skopax import brand, chatui
from skopax.app import create_app
from skopax.cardlink import chat_url
from skopax.slack import anomaly_message
from skopax.chatui.layer import ChatDataLayer
from skopax.chatui.text import TEXTS, configure, linkify, about_line, action_label, answer_of, cookie_of, done_line, lang_of, markdown_of, pending_line, refusal, said as told
from skopax.kube import Kube
from skopax.messages import msg
from skopax.store import Store

KEY = 'log|checkout-service|ERROR|boom'
NINA = 'nina.white@example.com'
PAUL = 'paul.stone@example.com'
THREAD = 'aaaabbbb-cccc-4ddd-8eee-ffff00001111'
SPILL = 'hunter2-spill-' * 4


class Filters:
    def __init__(self, user: str, search: str | None = None):
        self.userId = user
        self.search = search


class Page:
    first = 20


def bridge_of(store: Store, tmp_path, **over) -> chatui.Bridge:
    store.save({**state(anomaly(KEY, num=14, svc='checkout-service', label='ERROR lines')), 'initialized': True}, None)
    store.card_ids.insert_one({'_id': KEY, 'n': 14})
    app = ready_app(dataclasses.replace(settings(), **over), store, kube=Kube(root=tmp_path))
    return app.state.chat_bridge


@pytest.fixture(autouse=True)
def jira_site():
    configure('https://jira.example.com', ('OPS', 'WEB', 'INFRA'))
    yield
    configure('', ())


def test_a_slack_style_answer_becomes_markdown_with_card_links_and_the_status_line_follows_the_run():
    # when
    text = markdown_of('*checkout-service*: one client, 40 lines.\n• card #14 open, #7 closed\nDetails: <https://kibana.example.com/x|Kibana> and <https://shop.example.com/a>')

    # then
    assert text == '**checkout-service**: one client, 40 lines.\n- card [#14](/c/14) open, [#7](/c/7) closed\nDetails: [Kibana](https://kibana.example.com/x) and https://shop.example.com/a'
    assert markdown_of('2 * 3 * 4 and **bold** stays, [#3](/c/3) too') == '2 * 3 * 4 and **bold** stays, [#3](/c/3) too'
    assert pending_line({'status': 'requested'}) == 'Waiting for a free room…'
    assert pending_line({'status': 'processing', 'progress': {'toolCalls': 2, 'lastTool': 'esql'}}) == 'Working · 2 tool calls, last esql…'
    assert pending_line({'status': 'requested'}, 'stopped') == 'Waiting: the investigator is stopped.'
    assert answer_of({'status': 'failed', 'error': 'over the tool cap'}) == 'No answer: over the tool cap.'
    assert action_label({'key': KEY, 'action': 'mute', 'hours': 48}, {'num': 14, 'svc': 'checkout-service', 'label': 'ERROR lines'}) == 'Mute 48 h: #14 checkout-service / ERROR lines'
    assert about_line([{'key': KEY, 'num': 14, 'title': 'checkout-service / ERROR lines'}]) == '📌 **[#14](/c/14) · checkout-service / ERROR lines**'
    full = {'key': KEY, 'num': 14, 'title': 'checkout-service / ERROR lines', 'sev': 'warn', 'status': 'open', 'what': '40 ERROR lines in the last hour,\n 2 a day ago', 'next': 'Check the 14:05 deploy.',
            'owner': 'nina.white@example.com', 'ticket': 'OPS-1'}
    assert about_line([full]) == ('📌 🟡 **[#14](/c/14) · checkout-service / ERROR lines**\n\n> 40 ERROR lines in the last hour, 2 a day ago\n\n→ Check the 14:05 deploy.\n\n👤 nina.white · [OPS-1](https://jira.example.com/browse/OPS-1)')
    assert about_line([full | {'status': 'cleared', 'what': 'x' * 900}]).startswith('📌 ✅ **[#14]') and len(about_line([full | {'what': 'x' * 900}])) < 560
    assert cookie_of('a=1; skopax_chat_card=log%7Ca; b=2', 'skopax_chat_card') == 'log|a' and cookie_of('a=1', 'skopax_chat_card') is None


def test_the_chat_page_writes_into_the_same_chats_under_its_own_thread_id_and_never_into_someone_else_s(store: Store, tmp_path):
    # given
    bridge = bridge_of(store, tmp_path, chat_daily_cap=2)

    # when
    first = bridge.post(NINA, 'Why is this card open?', THREAD, [KEY])
    with pytest.raises(chatui.Refused) as busy:
        bridge.post(NINA, 'second', THREAD, None)
    with pytest.raises(chatui.Refused) as foreign:
        bridge.post(PAUL, 'I have a question too', THREAD, None)
    with pytest.raises(chatui.Refused) as bad_id:
        bridge.post(NINA, 'x', '../etc', None)
    store.update_action(store.list_actions('requested', 10)[0]['id'], 'done', 'answer', 'requested')
    bridge.post(NINA, 'And then?', THREAD, None)
    store.update_action(store.list_actions('requested', 10)[0]['id'], 'done', 'answer 2', 'requested')
    with pytest.raises(chatui.Refused) as capped:
        bridge.post(NINA, 'third', THREAD, None)

    # then
    assert first['id'] == THREAD and first['pending'] and first['cards'][0]['num'] == 14
    assert str(busy.value) == 'Wait for the answer to your last message.' and str(foreign.value) == 'No such chat of yours.' and str(bad_id.value) == 'No such chat of yours.'
    assert str(capped.value) == '2 chat messages a day is the cap; it starts again at midnight.'
    assert [m['answer'] for m in bridge.chat(NINA, THREAD)['messages']] == ['answer', 'answer 2'] and bridge.chat(PAUL, THREAD) is None
    assert bridge.owner(THREAD) == NINA and bridge.card('14')['key'] == KEY and bridge.card(KEY)['title'] == 'checkout-service / ERROR lines' and bridge.card('999') is None


def test_a_proposed_card_action_is_applied_only_by_an_operator_and_queues_the_usual_action(store: Store, tmp_path):
    # given
    bridge = bridge_of(store, tmp_path)

    # when
    with pytest.raises(chatui.Refused) as reader:
        bridge.apply(NINA, False, {'key': KEY, 'action': 'investigate'})
    with pytest.raises(chatui.Refused) as odd:
        bridge.apply(NINA, True, {'key': KEY, 'action': 'delete'})
    said = bridge.apply(NINA, True, {'key': KEY, 'action': 'investigate'})

    # then
    assert str(reader.value) == msg('access.need_operator') and reader.value.code == 'role' and str(odd.value) == 'That proposal cannot be applied.'
    assert said == ('queued', '')
    [queued] = store.list_actions('requested', 10)
    assert (queued['type'], queued['key'], queued['requestedBy']) == ('dive', KEY, NINA)


def test_a_chat_resolve_takes_the_proposed_kind_or_no_action_needed_and_is_stored_as_via_chat(store: Store, tmp_path):
    # given
    bridge = bridge_of(store, tmp_path)
    card = {'num': 14, 'svc': 'checkout-service', 'label': 'ERROR lines'}

    # when
    with pytest.raises(chatui.Refused) as bare:
        bridge.apply(NINA, True, {'key': KEY, 'action': 'resolve', 'kind': 'duplicate'})
    said = bridge.apply(NINA, True, {'key': KEY, 'action': 'resolve', 'kind': 'fixed', 'ref': 'OPS-140'})
    how = store.anomaly(KEY)['resolution']

    # then
    assert str(bare.value) == 'That proposal cannot be applied.' and bare.value.code == 'bad'
    assert said == ('done', 'Fixed (OPS-140)')
    assert {k: how[k] for k in ('kind', 'ref', 'by', 'via')} == {'kind': 'fixed', 'ref': 'OPS-140', 'by': NINA, 'via': 'chat'}
    assert action_label({'key': KEY, 'action': 'resolve', 'kind': 'fixed'}, card) == 'Resolve · Fixed: #14 checkout-service / ERROR lines'
    assert action_label({'key': KEY, 'action': 'resolve', 'kind': 'duplicate', 'ref': '#12'}, card) == 'Resolve · Duplicate of #12: #14 checkout-service / ERROR lines'
    assert action_label({'key': KEY, 'action': 'resolve'}, card) == 'Resolve: #14 checkout-service / ERROR lines'


def test_a_chat_action_that_fails_names_the_core_reason_in_the_refusal(store: Store, tmp_path):
    # given
    bridge = bridge_of(store, tmp_path)

    # when
    with pytest.raises(chatui.Refused) as failed:
        bridge.apply(NINA, True, {'key': KEY, 'action': 'resolve', 'kind': 'duplicate', 'ref': '#999'})
    [stored] = store.list_actions('failed', 10)

    # then
    assert stored['result'] == 'no card #999 to be a duplicate of'
    assert (failed.value.code, failed.value.fields['reason']) == ('failed', stored['result'])
    assert refusal(failed.value, 'en') == r'Not applied: no card \#999 to be a duplicate of.'


def test_the_history_sidebar_reads_the_stored_chats_and_share_and_delete_go_through_the_same_rules(store: Store, tmp_path):
    # given
    bridge = bridge_of(store, tmp_path)
    layer = ChatDataLayer(bridge)
    bridge.post(NINA, 'Why is this card open?', THREAD, [KEY])
    store.update_action(store.list_actions('requested', 10)[0]['id'], 'done', '*checkout-service*: card #14 open.', 'requested')

    async def scenario():
        listed = await layer.list_threads(Page(), Filters(NINA))
        other = await layer.list_threads(Page(), Filters(PAUL))
        thread = await layer.get_thread(THREAD)
        author = await layer.get_thread_author(THREAD)
        await layer.update_thread(THREAD, name='renamed', metadata={'chat_profile': 'x'})
        untouched = bridge.shared(THREAD)
        await layer.update_thread(THREAD, metadata={'is_shared': True})
        shared = (await layer.get_thread(THREAD))['metadata']['is_shared']
        await layer.delete_thread(THREAD)
        gone = await layer.get_thread(THREAD)
        return listed, other, thread, author, untouched, shared, gone

    # when
    listed, other, thread, author, untouched, shared, gone = asyncio.run(scenario())

    # then
    assert [(t['id'], t['name'], t['userIdentifier']) for t in listed.data] == [(THREAD, 'Why is this card open?', NINA)] and other.data == []
    assert [(s['type'], s['output']) for s in thread['steps']] == [('assistant_message', '📌 🟡 **[#14](/c/14) · checkout-service / ERROR lines**'),
                                                                 ('user_message', 'Why is this card open?'), ('assistant_message', '**checkout-service**: card [#14](/c/14) open.')]
    assert author == NINA and untouched is False and shared is True
    assert gone is None and store.chats(NINA, 10) == [] and not store.chat_shared(THREAD), 'a deleted chat leaves the list and stops being shared'


def test_the_chat_speaks_english_whatever_the_reader_picked_while_no_pack_brings_chat_texts():
    # then
    assert [lang_of('a=1; skopax_lang=de', 'en-US'), lang_of('', 'de-DE,de;q=0.9'), lang_of(None, 'en-US'), lang_of('skopax_lang=xx', None)] == ['en', 'en', 'en', 'en']
    assert set(TEXTS) == {'en'}
    assert pending_line({'status': 'requested'}, None, 'en') == 'Waiting for a free room\u2026'
    assert pending_line({'status': 'processing', 'progress': {'toolCalls': 2, 'lastTool': 'esql'}}, None, 'en') == 'Working \u00b7 2 tool calls, last esql\u2026'
    assert done_line({'answer': 'x', 'usd': 0.12}, 'en') == 'Answered \u00b7 $0.12' and answer_of({'status': 'failed'}, 'en') == 'No answer.'
    assert action_label({'key': KEY, 'action': 'mute', 'hours': 48}, {'num': 14, 'svc': 'checkout-service', 'label': 'ERROR lines'}, 'en') == 'Mute 48 h: #14 checkout-service / ERROR lines'
    capped = chatui.Refused('held text', 'cap', cap=30)
    assert refusal(capped, 'en') == '30 chat messages a day is the cap; it starts again at midnight.' and refusal(chatui.Refused('Odd one.'), 'en') == 'That could not be done.'
    assert told('en', 'done', what='x') == 'Done: x.'


def test_the_mounted_chat_page_lets_in_only_a_service_login_and_lists_only_that_person_s_chats():
    # given
    chats = [{'id': THREAD, 'title': 'Question', 'startedAt': '2026-11-10T12:00:00Z', 'messages': 1}]
    bridge = chatui.Bridge(user=lambda cookie: {'login': NINA, 'operator': False} if cookie == 'sealed-session' else None, post=lambda *a: {}, chat=lambda by, chat_id: None,
                           chats=lambda by: chats if by == NINA else [], owner=lambda chat_id: NINA, shared=lambda chat_id: False, share=lambda *a: True, hide=lambda *a: None,
                           apply=lambda *a: '', card=lambda ref: None)
    app = FastAPI()
    chatui.mount(app, bridge, b'seed', 'http://testserver')
    listing = {'pagination': {'first': 20}, 'filter': {}}

    # when
    with TestClient(app) as c:
        page = c.get('/chat/')
        refused = c.post('/chat/auth/header')
        anonymous = c.post('/chat/project/threads', json=listing)
        c.cookies.set('skopax_session', 'sealed-session')
        allowed = c.post('/chat/auth/header')
        threads = c.post('/chat/project/threads', json=listing)
        outside = c.get('/api/state')
        english = c.get('/chat/project/translations', params={'language': 'en-US'}).json()['translation']['navigation']['newChat']['button']
        logo = c.get('/chat/logo', params={'theme': 'dark'})
        logo_light = c.get('/chat/logo', params={'theme': 'light'})
        avatar = c.get('/chat/avatars/Skopax')
        default_avatar = c.get('/chat/avatars/default')

    # then
    assert page.status_code == 200 and '<title>Skopax</title>' in page.text
    assert (refused.status_code, anonymous.status_code, allowed.status_code, outside.status_code) == (401, 401, 200, 404)
    assert [t['id'] for t in threads.json()['data']] == [THREAD]
    assert english == 'New Chat'
    assert logo.status_code == 200 and logo.headers['content-type'].startswith('image/svg+xml') and 'aria-label="Skopax"' in logo.text, 'the chat carries the wordmark'
    assert logo.text == brand.asset('wordmark-on-dark.svg') and logo_light.text == brand.asset('wordmark-on-light.svg') and logo.text != logo_light.text
    assert avatar.status_code == 200 and avatar.text == brand.mark_svg() and default_avatar.text == avatar.text


def test_a_refused_chat_body_never_comes_back_in_the_422():
    # given
    bridge = chatui.Bridge(user=lambda cookie: {'login': NINA, 'operator': False} if cookie == 'sealed-session' else None, post=lambda *a: {}, chat=lambda by, chat_id: None,
                           chats=lambda by: [], owner=lambda chat_id: NINA, shared=lambda chat_id: False, share=lambda *a: True, hide=lambda *a: None,
                           apply=lambda *a: '', card=lambda ref: None)
    app = FastAPI()
    chatui.mount(app, bridge, b'seed', 'http://testserver')
    bodies = {'paging value': ('POST', '/chat/project/threads', {'pagination': {'first': SPILL}}),
              'thread name': ('PUT', '/chat/project/thread', {'name': SPILL}),
              'mcp header': ('POST', '/chat/mcp', {'clientType': 'sse', 'name': 'x', 'headers': {'Authorization': f'Bearer {SPILL}'}})}

    # when
    with TestClient(app) as c:
        c.cookies.set('skopax_session', 'sealed-session')
        signed_in = c.post('/chat/auth/header')
        seen = {name: c.request(method, path, json=body) for name, (method, path, body) in bodies.items()}

    # then
    assert signed_in.status_code == 200
    assert {name: r.status_code for name, r in seen.items()} == dict.fromkeys(bodies, 422)
    assert [name for name, r in seen.items() if SPILL in r.text] == [], 'the chat 422 never sends back what was submitted'
    assert {name: [sorted(item) for item in r.json()['detail']] for name, r in seen.items()} == {name: [['loc', 'msg', 'type']] * len(seen[name].json()['detail']) for name in bodies}


def test_the_page_learns_whether_the_chat_is_mounted_and_a_test_app_never_mounts_it(store: Store, tmp_path):
    # when
    with TestClient(create_app(settings(), store, kube=Kube(root=tmp_path))) as c:
        me = c.get('/api/me', headers=AUTH).json()
        page = c.get('/chat/')

    # then
    assert me['chat'] is False and page.status_code == 404


def test_the_chat_api_needs_a_service_session_on_every_call_and_logout_drops_the_chat_s_own_cookie(store: Store, tmp_path):
    # when
    with TestClient(create_app(settings(), store, kube=Kube(root=tmp_path)), follow_redirects=False) as c:
        api = c.post('/chat/project/threads', json={})
        login = c.post('/chat/auth/header')
        page = c.get('/chat/')
        out = c.post('/logout', follow_redirects=False)

    # then
    assert (api.status_code, login.status_code) == (401, 401) and api.json() == {'detail': msg('chat.login_first')} == {'detail': 'log in to Skopax first'}
    assert page.status_code == 404, 'the page shell itself is not gated; here the chat is simply not mounted'
    cleared = [h for h in out.headers.get_list('set-cookie') if h.startswith('access_token')]
    assert len(cleared) == 7 and all('Max-Age=0' in h for h in cleared)
    assert [chatui.needs_session(p) for p in ('/chat', '/chat/', '/chat/assets/index.js', '/chat/public/theme.json', '/chat/favicon', '/chat/project/threads', '/chat/user', '/chat/ws/socket.io/',
                                              '/chatter', '/api/state')] == [False, False, False, False, False, True, True, True, False, False]


def test_a_slack_card_post_an_assignee_message_and_a_push_carry_a_chat_about_this_link():
    # given
    card = {'key': KEY, 'num': 14, 'sev': 'warn', 'svc': 'checkout-service', 'label': 'ERROR lines', 'status': 'open'}

    # when
    post = anomaly_message(card, 'https://skopax.example.com/c/14', 'pod', None, None, chat_url('https://skopax.example.com/', KEY, 14))
    plain = anomaly_message(card, 'https://skopax.example.com/c/14', 'pod')

    # then
    options = [(o['text']['text'], o['url']) for o in post['blocks'][0]['accessory']['options']]
    assert post['blocks'][1]['text']['text'].startswith('*<https://skopax.example.com/c/14|')
    assert options == [('Ask in chat', 'https://skopax.example.com/#t=chat/new/14')]
    assert 'accessory' not in plain['blocks'][0]
    assert chat_url('https://skopax.example.com', 'page-typos|shop.example.com/products') == 'https://skopax.example.com/#t=chat/new/page-typos%7Cshop.example.com%2Fproducts'


def test_tickets_become_jira_links_and_site_addresses_code_and_existing_links_are_left_alone():
    # when
    text = linkify('WEB-204: add a redirect from /shop/a to /shop/b. See example.com/help and https://example.com/shop, '
                   '`/shop/x` in code, [#1](/c/1), skopax.example.com/c/1, the API /orders/detailed and src/main/kotlin stay.')

    # then
    assert text == ('[WEB-204](https://jira.example.com/browse/WEB-204): add a redirect from /shop/a to /shop/b. See example.com/help and https://example.com/shop, '
                    '`/shop/x` in code, [#1](/c/1), skopax.example.com/c/1, the API /orders/detailed and src/main/kotlin stay.')
    assert linkify('INFRA-401 and OPS-4321-2') == '[INFRA-401](https://jira.example.com/browse/INFRA-401) and [OPS-4321](https://jira.example.com/browse/OPS-4321)-2'
    configure('https://jira.example.com', ('OPS',))
    assert linkify('OPS-1 and WEB-2') == '[OPS-1](https://jira.example.com/browse/OPS-1) and WEB-2'
    configure('', ())
    assert linkify('OPS-1') == 'OPS-1'


def test_the_chat_header_links_back_to_the_dashboard_and_hides_the_link_inside_the_dashboard_frame():
    # given
    import tomllib
    from skopax import chatui
    public = pathlib.Path(chatui.__file__).parent / 'public'

    # when
    links = tomllib.loads(chatui.config_text('https://skopax.example.com', session=1))['UI']['header_links']
    js, css = (public / 'skopax-chat.js').read_text(), (public / 'skopax-chat.css').read_text()

    # then
    assert links == [{'name': 'Skopax', 'display_name': 'Skopax', 'icon_url': '/chat/public/skopax-back.svg', 'url': '/', 'target': '_top'}]
    assert (public / 'skopax-back.svg').exists()
    assert 'Unshare' not in js and 'skopax_lang' not in js
    assert "classList.add('in-frame')" in js and 'html.in-frame a[href="/"], html.in-frame button:has(a[href="/"]) { display: none !important; }' in css


def test_the_chat_root_has_an_empty_readme_per_language_so_chainlit_logs_no_missing_translation_warning(tmp_path, monkeypatch):
    # given
    monkeypatch.delenv(chatui.ROOT_ENV, raising=False)

    # when
    root = pathlib.Path(chatui.prepare('https://skopax.example.com'))

    # then
    assert {p.name for p in root.glob('chainlit*.md')} == {'chainlit.md', 'chainlit_en.md', 'chainlit_en-US.md', 'chainlit_en-GB.md'}
    assert all(p.read_text() == '' for p in root.glob('chainlit*.md'))


def test_the_chainlit_config_keeps_mcp_file_upload_audio_and_html_off():
    # when
    config = tomllib.loads(chatui.config_text('https://skopax.example.com', session=3600))

    # then
    assert config['features']['mcp']['enabled'] is False, 'chainlit MCP stays off: CVE-2026-45018 and CVE-2026-45019 need it on, and 2.12.0 does not run on Python 3.15'
    assert config['features']['spontaneous_file_upload']['enabled'] is False
    assert config['features']['audio']['enabled'] is False
    assert config['features']['unsafe_allow_html'] is False


def test_the_chat_logo_is_the_wordmark_or_the_escaped_display_name_and_the_avatar_is_always_the_mark():
    # given
    brand.use('Skopax')
    on_light, on_dark = chatui.brand_answer('/logo', b'theme=light'), chatui.brand_answer('/logo', b'theme=dark')
    brand.use("O'Brien Ops")
    named = chatui.brand_answer('/logo', b'theme=dark')
    unthemed = chatui.brand_answer('/logo', b'')
    avatars = [chatui.brand_answer(f'/avatars/{who}', b'') for who in ('default', 'Skopax', "o_brien_ops", 'someone.else', "O'Brien Ops", 'Caf\u00e9 Ops', 'Ops (EU)', 'Ops\u00b7EU')]
    nested = [chatui.brand_answer(path, b'') for path in ('/avatars/../x', '/avatars/a/b', '/avatars/Ops / SRE')]
    empty = chatui.brand_answer('/avatars/', b'')

    # then
    assert on_light.body != on_dark.body and b'aria-label="Skopax"' in on_light.body and on_light.media_type == 'image/svg+xml'
    assert b'<text' not in on_light.body and b'#0f1720' in on_light.body and b'#e8eef5' in on_dark.body
    assert b'O&#x27;Brien Ops' in named.body and b"O'Brien" not in named.body and b'#e8eef5' in named.body
    assert b'#0f1720' in unthemed.body
    assert {a.body for a in avatars} == {brand.mark_svg().encode()} and {a.body for a in nested} == {brand.mark_svg().encode()} and empty is None
    assert chatui.brand_answer('/project/settings', b'') is None


def test_the_chat_front_answers_the_logo_and_avatars_before_the_chat_and_leaves_every_other_path_to_it():
    # given
    app = FastAPI()
    chatui.mount(app, chatui.Bridge(user=lambda c: None, post=lambda *a: {}, chat=lambda *a: None, chats=lambda by: [], owner=lambda i: None, shared=lambda i: False,
                                    share=lambda *a: True, hide=lambda *a: None, apply=lambda *a: '', card=lambda r: None), b'seed', 'http://testserver')

    # when
    with TestClient(app) as c:
        anonymous_logo = c.get('/chat/logo', params={'theme': 'light'})
        head = c.head('/chat/logo')
        post = c.post('/chat/logo')
        threads = c.post('/chat/project/threads', json={})

    # then
    assert anonymous_logo.status_code == 200 and 'aria-label="Skopax"' in anonymous_logo.text and head.status_code == 200
    assert post.status_code != 200 and threads.status_code == 401


def test_the_chat_front_strips_a_root_path_only_at_a_path_boundary():
    # then
    assert chatui.route_path({'path': '/chat/logo', 'root_path': '/chat'}) == '/logo'
    assert chatui.route_path({'path': '/chat', 'root_path': '/chat'}) == '/'
    assert chatui.route_path({'path': '/chatter/logo', 'root_path': '/chat'}) == '/chatter/logo'
    assert chatui.route_path({'path': '/logo'}) == '/logo'


def test_the_chat_logo_has_one_source_in_the_brand_folder():
    # then
    assert not (chatui.HERE / 'public' / 'logo_dark.svg').exists() and not (chatui.HERE / 'public' / 'logo_light.svg').exists()


def test_the_front_adds_the_csp_to_html_only():
    # given
    async def inner(scope, receive, send):
        kind = 'application/json' if scope['path'].endswith('.json') else 'text/html; charset=utf-8'
        await send({'type': 'http.response.start', 'status': 200, 'headers': [(b'content-type', kind.encode())]})
        await send({'type': 'http.response.body', 'body': b'x'})

    front = chatui.BrandFront(inner)
    app = FastAPI()
    app.mount('/', front)

    # when
    with TestClient(app) as c:
        page, head, data, post = c.get('/x'), c.head('/x'), c.get('/x.json'), c.post('/x')

    # then
    assert chatui.CSP == "style-src 'self' 'unsafe-inline'; font-src 'self' data:"
    assert page.headers['content-security-policy'] == chatui.CSP and head.headers['content-security-policy'] == chatui.CSP and 'content-security-policy' not in data.headers and 'content-security-policy' not in post.headers


def test_the_chat_html_carries_the_csp_and_no_font_link(tmp_path):
    # given
    from tests.test_brand import CHAT_SCRIPT
    root = str(pathlib.Path(__file__).resolve().parent.parent)
    env = {k: v for k, v in os.environ.items() if k != 'CHAINLIT_APP_ROOT'} | {'PYTHONPATH': root}

    # when
    run = subprocess.run([sys.executable, '-c', CHAT_SCRIPT, 'Ops'], cwd=tmp_path, env=env, capture_output=True, text=True, timeout=120)

    # then
    assert run.returncode == 0, run.stderr[-800:]
    served = json.loads(run.stdout)
    headers = {k.lower(): v for k, v in served['headers'].items()}
    assert served['status'] == 200 and headers['content-security-policy'] == "style-src 'self' 'unsafe-inline'; font-src 'self' data:"
    assert 'fonts.googleapis.com/css2' not in served['text'] and 'rel="stylesheet"' not in served['text'].split('FONT START')[1].split('FONT END')[0]


def bare_bridge() -> chatui.Bridge:
    return chatui.Bridge(user=lambda c: None, post=lambda *a: {}, chat=lambda *a: None, chats=lambda by: [], owner=lambda i: None, shared=lambda i: False,
                         share=lambda *a: True, hide=lambda *a: None, apply=lambda *a: '', card=lambda r: None)


def on_loop() -> bool:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return False
    return True


def test_the_chat_brand_json_follows_the_attribution_rules(monkeypatch):
    # given
    calls = []
    made = brand.chat_brand
    monkeypatch.setattr(brand, 'chat_brand', lambda: calls.append(on_loop()) or made())
    app = FastAPI()
    chatui.mount(app, bare_bridge(), b'seed', 'http://testserver')
    brand.use('Skopax')
    plain = chatui.brand_answer('/public/skopax-brand.json', b'')
    brand.use('Acme Ops')
    named = chatui.brand_answer('/public/skopax-brand.json', b'')
    calls.clear()

    # when
    with TestClient(app) as c:
        served = c.get('/chat/public/skopax-brand.json')

    # then
    assert json.loads(plain.body) == {'attribution': {'text': 'Skopax 0.6.3 \u00b7 skopax.io', 'href': 'https://skopax.io'}}
    assert json.loads(named.body) == {'attribution': {'text': 'Acme Ops \u00b7 powered by Skopax 0.6.3', 'href': 'https://skopax.io'}}
    assert plain.media_type == 'application/json' and plain.headers['cache-control'] == 'no-cache'
    assert served.status_code == 200 and served.json() == json.loads(named.body)
    assert served.headers['content-type'].startswith('application/json') and served.headers['cache-control'] == 'no-cache' and 'content-security-policy' not in served.headers
    assert calls == [False], 'the brand JSON may read the settings store, so it is built off the event loop'


def test_the_served_chat_html_loads_the_brand_script(tmp_path):
    # given
    from tests.test_brand import CHAT_SCRIPT
    root = str(pathlib.Path(__file__).resolve().parent.parent)
    env = {k: v for k, v in os.environ.items() if k != 'CHAINLIT_APP_ROOT'} | {'PYTHONPATH': root}

    # when
    run = subprocess.run([sys.executable, '-c', CHAT_SCRIPT, 'Ops'], cwd=tmp_path, env=env, capture_output=True, text=True, timeout=120)

    # then
    assert run.returncode == 0, run.stderr[-800:]
    served = json.loads(run.stdout)
    assert served['status'] == 200 and '<script src="/chat/public/skopax-chat.js"' in served['text'], 'the chat attribution rides on custom_js; a Chainlit upgrade that drops it would hide the line silently'


def test_inside_skopax_the_chat_hides_its_own_theme_toggle_and_avatar_menu_but_the_standalone_tab_keeps_them():
    # given
    css = (pathlib.Path(__file__).parent.parent / 'skopax' / 'chatui' / 'public' / 'skopax-chat.css').read_text()

    # then
    assert 'html.in-frame #theme-toggle, html.in-frame #user-nav-button { display: none !important; }' in css
    assert not [line for line in css.splitlines() if '#theme-toggle' in line and 'in-frame' not in line]
