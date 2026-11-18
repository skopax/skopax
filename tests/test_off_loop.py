import asyncio
import dataclasses
import threading
import time
from types import SimpleNamespace as NS

import chainlit as cl
from chainlit.context import init_ws_context
from chainlit.session import WebsocketSession
from chainlit.user import PersistedUser
from chainlit.user_session import user_sessions
from fastapi.testclient import TestClient

from tests.conftest import AUTH, settings
from tests.sso_kit import FakeIdp, idp_settings
from tests.test_auth import Clock
from skopax import app as app_module, chatui
from skopax.app import create_app
from skopax.auth import SESSION_COOKIE
from skopax.chatui import handlers
from skopax.chatui.layer import ChatDataLayer
from skopax.instances import Instances, PluginState
from skopax import migrations
from skopax.kube import Kube
from skopax.registry import Registry
from skopax.contract import SourceData
from skopax.features import Features
from skopax.runtime import Runtime
from skopax.settingsources import SourceSet
from skopax.sessions import Sessions, session_secret
from skopax.slack import Slack
from skopax.slacklink import SlackLink
from skopax.sso import SsoHttp
from skopax.store import Store
from skopax.tunables import Live, View
from skopax import messages
from skopax.messages import msg
from tests.test_plugin_hooks import DEMO, served
from tests.test_deploys import deploy
from tests.test_enrich import fake_gitlab
from tests.fake_http import FakeHttp
from tests.plugin_kit import JIRA_VALUES
from skopax.plugins.jira.client import JIRA_GATEWAY
from tests.fake_anthropic import FakeAnthropic
from tests.fake_slack import slack_instance
from tests.test_slack_listener import Factory, wait_for

NINA = 'nina.white@example.com'
THREAD = 'aaaabbbb-cccc-4ddd-8eee-ffff00001111'
KEY = 'log|checkout-service|ERROR|boom'


def on_loop() -> bool:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return False
    return True


async def emit(*args, **kwargs):
    return None


def test_a_request_reads_no_live_setting_on_the_event_loop(store: Store, tmp_path, monkeypatch):
    # given
    seen = []
    current = Live.current
    monkeypatch.setattr(Live, 'current', lambda self: seen.append(on_loop()) or current(self))

    # when
    with TestClient(create_app(settings(), store, kube=Kube(root=tmp_path))) as c:
        seen.clear()
        res = c.get('/api/rev')

    # then
    assert res.status_code == 200 and True not in seen, 'a settings read can query Mongo, which blocks every request while it waits'


def test_the_mcp_switch_and_the_mcp_endpoint_read_no_live_setting_on_the_event_loop(store: Store, tmp_path, monkeypatch):
    # given
    switched, reads = [], []
    on, current = Features.on, Live.current
    monkeypatch.setattr(Features, 'on', lambda self, feature_id: switched.append((feature_id, on_loop())) or on(self, feature_id))
    monkeypatch.setattr(Live, 'current', lambda self: reads.append(on_loop()) or current(self))
    call = {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/list'}
    idp = FakeIdp()
    mcp = idp_settings(idp, mcp_issuer='https://login.example.com')

    # when
    with TestClient(create_app(mcp, store, kube=Kube(root=tmp_path), sso_http=SsoHttp(fetch=idp.fetch, exchange=idp.exchange))) as c:
        c.put('/api/admin/features/mcp', json={'on': False}, headers=AUTH)
        switched.clear()
        reads.clear()
        off = c.post('/mcp', json=call)
        off_switched, off_reads = list(switched), list(reads)
        c.put('/api/admin/features/mcp', json={'on': True}, headers=AUTH)
        switched.clear()
        reads.clear()
        no_token = c.post('/mcp', json=call)
        bad_token = c.post('/mcp', json=call, headers={'Authorization': 'Bearer not-a-jwt'})
        on_switched, on_reads = list(switched), list(reads)
    with TestClient(create_app(settings(), store, kube=Kube(root=tmp_path))) as c:
        reads.clear()
        not_set_up = c.post('/mcp', json=call)
        unset_reads = list(reads)

    # then
    assert off.status_code == 409 and off_switched == [('mcp', False)]
    assert (no_token.status_code, bad_token.status_code, not_set_up.status_code) == (401, 401, 404)
    assert no_token.headers['www-authenticate'].startswith('Bearer ') and 'invalid_token' in bad_token.headers['www-authenticate']
    assert on_switched == [('mcp', False), ('mcp', False)]
    for seen in (off_reads, on_reads, unset_reads):
        assert seen and True not in seen, 'the switch, the endpoint and its challenge read the settings, which can query Mongo, so they run in a thread'


def test_renewing_a_session_cookie_reads_no_live_setting_on_the_event_loop(store: Store, tmp_path, monkeypatch):
    # given
    clock = Clock()
    s = settings()
    cookie = Sessions(session_secret(store.db, s), 3600, 7200, clock=clock).issue('admin@example.com', 'admin', 'local')
    seen = []
    current = Live.current
    monkeypatch.setattr(Live, 'current', lambda self: seen.append(on_loop()) or current(self))
    client = TestClient(create_app(s, store, kube=Kube(root=tmp_path), clock=clock), base_url='https://testserver')
    client.cookies.set(SESSION_COOKIE, cookie)
    clock.advance(6 * 60)

    # when
    seen.clear()
    res = client.get('/api/rev')

    # then
    assert res.status_code == 200 and any(h.startswith(f'{SESSION_COOKIE}=') for h in res.headers.get_list('set-cookie')), 'the cookie was renewed'
    assert True not in seen, 'the Secure flag is decided without a settings read on the loop'


def test_the_chat_page_reads_the_stored_chats_off_the_event_loop(monkeypatch):
    # given
    calls = []

    def seen(name, value):
        return lambda *args: calls.append((name, on_loop())) or value

    chat = {'id': THREAD, 'title': 'Question', 'messages': [{'id': 'm1', 'text': 'Question', 'at': '2026-11-10T12:00:00Z', 'status': 'done', 'answer': 'Answer',
                                                          'cardAction': {'key': KEY, 'action': 'investigate'}}]}
    bridge = chatui.Bridge(user=seen('user', {'login': NINA, 'operator': True}), post=seen('post', chat), chat=seen('chat', chat), chats=seen('chats', [chat]), owner=seen('owner', NINA),
                           shared=seen('shared', False), share=seen('share', True), hide=seen('hide', None), apply=seen('apply', ('queued', '')), card=seen('card', None))
    layer = ChatDataLayer(bridge)
    monkeypatch.setattr(chatui, 'BRIDGE', bridge)
    sent = []

    async def send(self):
        sent.append(self.content)
        return self

    monkeypatch.setattr(cl.Message, 'send', send)

    async def removed():
        return None

    async def scenario():
        user = PersistedUser(id=NINA, createdAt='2026-11-10T00:00:00Z', identifier=NINA, metadata={})
        session = WebsocketSession(id='tab-1', socket_id='tab-1', emit=emit, emit_call=emit, user_env={}, client_type='webapp', user=user, thread_id=THREAD,
                                   environ={'HTTP_COOKIE': f'{chatui.CARD_COOKIE}=14'})
        init_ws_context(session)
        try:
            await layer.list_threads(NS(first=20), NS(userId=NINA, search=None))
            await layer.get_thread(THREAD)
            await layer.get_thread_author(THREAD)
            await layer.update_thread(thread_id=THREAD, metadata={'is_shared': True})
            await layer.delete_thread(THREAD)
            await handlers.shared_view({'id': THREAD}, user)
            await handlers.start()
            await handlers.answer(NS(content='Question'))
            await handlers.apply(NS(payload={'key': KEY, 'action': 'investigate'}, remove=removed))
        finally:
            user_sessions.pop('tab-1', None)
            await session.delete()

    # when
    asyncio.run(scenario())

    # then
    assert {name for name, _ in calls} == {'chats', 'shared', 'owner', 'chat', 'share', 'hide', 'card', 'post', 'user', 'apply'}
    assert [name for name, _ in calls].count('user') == 2, 'answer and apply each look the user up again'
    assert [name for name, loop in calls if loop] == [], 'every bridge call reads or writes Mongo, so none may run on the event loop'
    assert sent == ['Answer', 'Investigation queued; its findings will be on the card.']


def test_the_chat_header_auth_callback_looks_the_user_up_off_the_event_loop(monkeypatch):
    # given
    calls = []

    def seen(cookie):
        calls.append(on_loop())
        return {'login': NINA, 'operator': True}

    monkeypatch.setattr(chatui, 'BRIDGE', chatui.Bridge(user=seen, post=lambda *a: None, chat=lambda *a: None, chats=lambda *a: [], owner=lambda *a: NINA, shared=lambda *a: False,
                                                        share=lambda *a: True, hide=lambda *a: None, apply=lambda *a: ('done', ''), card=lambda *a: None))

    async def scenario():
        return await handlers.login({'cookie': f'{SESSION_COOKIE}=abc'})

    # when
    who = asyncio.run(scenario())

    # then
    assert who.identifier == NINA and who.metadata == {'operator': True}
    assert calls == [False], 'the lookup can wait on a lock or query Mongo, so it must not run on the event loop'


def test_the_first_message_of_a_new_chat_is_announced_again_once_the_chat_is_saved_so_the_sidebar_lists_it(monkeypatch):
    # given
    emitted = []

    async def record(event, data):
        emitted.append((event, data))

    def bridge_with(messages: int):
        chat = {'id': THREAD, 'title': 'Question', 'messages': [{'id': f'm{i}', 'text': 'Question', 'at': '2026-11-10T12:00:00Z', 'status': 'done', 'answer': 'Answer'} for i in range(messages)]}
        return chatui.Bridge(user=lambda cookie: {'login': NINA, 'operator': True}, post=lambda *a: chat, chat=lambda *a: chat, chats=lambda *a: [chat], owner=lambda *a: NINA,
                             shared=lambda *a: False, share=lambda *a: True, hide=lambda *a: None, apply=lambda *a: ('done', ''), card=lambda *a: None)

    async def send(self):
        return self

    monkeypatch.setattr(cl.Message, 'send', send)

    async def ask(messages: int):
        monkeypatch.setattr(chatui, 'BRIDGE', bridge_with(messages))
        user = PersistedUser(id=NINA, createdAt='2026-11-10T00:00:00Z', identifier=NINA, metadata={})
        session = WebsocketSession(id='tab-2', socket_id='tab-2', emit=record, emit_call=record, user_env={}, client_type='webapp', user=user, thread_id=THREAD, environ={})
        init_ws_context(session)
        try:
            await handlers.answer(NS(content='Question'))
        finally:
            user_sessions.pop('tab-2', None)
            await session.delete()

    # when
    asyncio.run(ask(1))
    first = [d for e, d in emitted if e == 'first_interaction']
    emitted.clear()
    asyncio.run(ask(2))
    later = [d for e, d in emitted if e == 'first_interaction']

    # then
    assert first == [{'interaction': 'Question' + handlers.LIST_AGAIN, 'thread_id': THREAD}], 'a changed interaction makes Chainlit reload its thread list, now with this chat in it'
    assert later == [], 'later messages of the chat announce nothing'


def test_reopening_a_chat_whose_answer_is_still_pending_posts_the_answer_after_the_thread_is_shown_and_a_finished_chat_posts_nothing(monkeypatch):
    # given
    async def record(event, data):
        return None

    def chat_with(statuses: list[str]):
        reads = iter(statuses)

        def read(*a):
            status = next(reads, statuses[-1])
            return {'id': THREAD, 'title': 'Question', 'messages': [{'id': 'm0', 'text': 'Why did this card open?', 'at': '2026-11-11T12:00:00Z', 'status': status,
                                                                  'answer': 'Answer' if status == 'done' else None}]}
        return read

    question = {'id': 'm0-q', 'type': 'user_message', 'output': 'Why did this card open?'}
    answered = {'id': 'm0-a', 'type': 'assistant_message', 'output': 'Answer'}
    sent = []

    async def send(self):
        sent.append(self.content)
        return self

    monkeypatch.setattr(cl.Message, 'send', send)
    monkeypatch.setattr(handlers, 'POLL_S', 0)
    monkeypatch.setattr(handlers, 'RESUME_SETTLE_S', 0)

    async def reopen(read, steps):
        monkeypatch.setattr(chatui, 'BRIDGE', chatui.Bridge(user=lambda cookie: {'login': NINA, 'operator': True}, post=lambda *a: None, chat=read, chats=lambda *a: [], owner=lambda *a: NINA,
                                                            shared=lambda *a: False, share=lambda *a: True, hide=lambda *a: None, apply=lambda *a: ('done', ''),
                                                            card=lambda *a: None))
        user = PersistedUser(id=NINA, createdAt='2026-11-10T00:00:00Z', identifier=NINA, metadata={})
        session = WebsocketSession(id='tab-3', socket_id='tab-3', emit=record, emit_call=record, user_env={}, client_type='webapp', user=user, thread_id=THREAD, environ={})
        init_ws_context(session)
        cl.user_session.set('user', user)
        try:
            task = await handlers.resume({'id': THREAD, 'steps': steps})
            before = list(sent)
            if task:
                await task
            return before
        finally:
            user_sessions.pop('tab-3', None)
            await session.delete()

    # when
    while_resuming = asyncio.run(reopen(chat_with(['processing', 'processing', 'done']), [question]))
    pending = list(sent)
    sent.clear()
    asyncio.run(reopen(chat_with(['processing', 'done']), [question, answered]))
    finished = list(sent)
    sent.clear()
    asyncio.run(reopen(chat_with(['done']), [question]))

    # then
    assert while_resuming == [], 'nothing is posted inside the resume hook: Chainlit sends the stored thread after it and would replace what was posted'
    assert pending == ['Answer'], 'the answer that landed after the chat was reopened is posted once, after the stored thread'
    assert finished == [], 'a chat whose stored thread already holds the answer posts nothing'
    assert sent == ['Answer'], 'an answer that landed between the thread read and the check is still posted'


def test_the_instance_and_plugin_caches_refresh_off_the_event_loop(store: Store, tmp_path, monkeypatch):
    # given
    seen = []

    def watch(cls, name):
        original = getattr(cls, name)
        monkeypatch.setattr(cls, name, lambda self, *a, **k: seen.append((cls.__name__ + '.' + name, on_loop())) or original(self, *a, **k))

    for cls, name in ((Instances, 'refresh'), (Instances, 'ensure_indexes'), (PluginState, 'refresh'), (Registry, 'refresh')):
        watch(cls, name)
    run = migrations.wait_and_run
    monkeypatch.setattr(migrations, 'wait_and_run', lambda *a, **k: seen.append(('migrations.run', on_loop())) or run(*a, **k))

    # when
    with TestClient(create_app(settings(), store, kube=Kube(root=tmp_path), registry=Registry.of())) as c:
        res = c.get('/api/rev')
        runtime = c.app.state.runtime

    # then
    assert res.status_code == 200 and isinstance(runtime, Runtime)
    assert {name for name, _ in seen} >= {'Instances.refresh', 'Instances.ensure_indexes', 'PluginState.refresh', 'Registry.refresh', 'migrations.run'}
    assert [name for name, loop in seen if loop] == [], 'these read or write Mongo, or run a plug-in hook, so none may run on the event loop'


def test_the_runtime_refresh_tick_runs_in_a_thread(store: Store):
    # given
    seen = []
    runtime = Runtime(Registry.of(), PluginState(store.db), Instances(store.db, Registry.of(), lambda: None))
    runtime.refresh = lambda: seen.append(on_loop())

    async def scenario():
        task = asyncio.create_task(runtime.loop(every=0.01))
        await asyncio.sleep(0.15)
        task.cancel()

    # when
    asyncio.run(scenario())

    # then
    assert len(seen) >= 2 and True not in seen


def test_an_outbound_message_reads_its_language_from_the_refresh_snapshot_not_the_live_settings(store: Store, monkeypatch):
    # given
    seen = []
    current = Live.current
    monkeypatch.setattr(Live, 'current', lambda self: seen.append(threading.get_ident()) or current(self))

    async def outbound() -> tuple[str, str]:
        return messages.language(), msg('slack.open')

    # when
    with served(store, DEMO, language='de'):
        seen.clear()
        said = asyncio.run(outbound())
        mine = [ident for ident in seen if ident == threading.get_ident()]

    # then
    assert said == ('de', 'In Skopax oeffnen'), 'the refresh thread set the language snapshot'
    assert mine == [], 'an outbound text on the event loop reads the snapshot, never the settings, which can query Mongo'


def test_the_startup_probe_reads_its_switch_and_runs_off_the_event_loop(store: Store, tmp_path, monkeypatch):
    # given
    seen, reads = [], []
    read = View.__getattr__
    monkeypatch.setattr(View, '__getattr__', lambda self, name: reads.append((name, on_loop())) or read(self, name))

    class Recording:
        def run(self, now):
            seen.append(on_loop())
            return {'at': '2026-10-19T19:04:00Z', 'targets': []}

    on = dataclasses.replace(settings(), probe=True)

    # when
    with TestClient(create_app(on, store, kube=Kube(root=tmp_path), probe=Recording())):
        deadline = time.monotonic() + 3
        while not seen and time.monotonic() < deadline:
            time.sleep(0.02)

    # then
    assert seen == [False], 'the probe opens sockets and writes Mongo, so it runs in a worker thread'
    assert ('probe', False) in reads and ('probe', True) not in reads, 'the probe switch is a live setting, read in the worker thread'


def test_the_settings_source_refresh_loop_runs_in_a_thread(store: Store, tmp_path):
    # given
    seen = []
    data = SourceData('feed', {'investigate.max-cards': '4'})

    def load():
        seen.append(on_loop())
        return data

    sources = SourceSet([('feed', load)], {'feed': data}, every=0.01)

    # when
    with TestClient(create_app(settings(), store, kube=Kube(root=tmp_path), registry=Registry.of(), sources=sources)):
        deadline = time.monotonic() + 5
        while len(seen) < 3 and time.monotonic() < deadline:
            time.sleep(0.02)

    # then
    assert len(seen) >= 3 and True not in seen, 'a source loader can call a config service, which blocks every request while it waits'


def test_the_stored_ai_key_is_read_in_a_thread_at_boot_and_on_every_refresh(store: Store, tmp_path):
    # given
    seen = []
    app = create_app(settings(), store, kube=Kube(root=tmp_path), registry=Registry.of())
    aikey = app.state.aikey
    refresh = aikey.refresh
    aikey.refresh = lambda: seen.append(on_loop()) or refresh()
    aikey.every = 0.01

    # when
    with TestClient(app):
        deadline = time.monotonic() + 5
        while len(seen) < 3 and time.monotonic() < deadline:
            time.sleep(0.02)

    # then
    assert len(seen) >= 3 and True not in seen, 'the stored key is read from Mongo, which blocks every request while it waits'


def test_the_stored_sso_secret_is_read_in_a_thread_at_boot_and_on_every_refresh(store: Store, tmp_path):
    # given
    seen = []
    app = create_app(settings(), store, kube=Kube(root=tmp_path), registry=Registry.of())
    secret = app.state.sso_secret
    refresh = secret.refresh
    secret.refresh = lambda: seen.append(on_loop()) or refresh()
    secret.every = 0.01

    # when
    with TestClient(app):
        deadline = time.monotonic() + 5
        while len(seen) < 3 and time.monotonic() < deadline:
            time.sleep(0.02)

    # then
    assert len(seen) >= 3 and True not in seen, 'the stored client secret is read from Mongo, which blocks every request while it waits'


def test_proxy_sighting_is_written_off_the_loop(store: Store, tmp_path, monkeypatch):
    # given
    seen = []
    app = create_app(settings(), store, kube=Kube(root=tmp_path), registry=Registry.of())
    note = store.note_proxy
    monkeypatch.setattr(store, 'note_proxy', lambda peer, at: seen.append((threading.current_thread().name, on_loop())) or note(peer, at))

    # when
    with TestClient(app) as c:
        res = c.get('/actuator/health/liveness', headers={'X-Forwarded-For': '203.0.113.9'})
        app.state.proxy_write.result(timeout=2)

    # then
    assert res.status_code == 200 and len(seen) == 1
    name, loop = seen[0]
    assert name.startswith(app_module.PROXY_THREAD) and not loop, 'the sighting is a Mongo write, which blocks every request while it waits'


def test_the_gitlab_client_is_read_off_the_event_loop(store: Store, tmp_path, monkeypatch):
    # given
    seen = []
    client = Runtime.client

    def watched(self, plugin_id, name=None):
        if plugin_id == 'gitlab':
            seen.append(on_loop())
        return client(self, plugin_id, name)

    monkeypatch.setattr(Runtime, 'client', watched)
    store.add_deploys([deploy()])

    # when
    with TestClient(create_app(settings(), store, kube=Kube(root=tmp_path), transport=fake_gitlab())) as c:
        c.app.state.instances.create('gitlab', 'main', {'url': 'https://gitlab.com', 'token': 'glpat', 'group': 'acme'}, True, NINA)
        info = c.get('/api/server', headers=AUTH)
        me = c.get('/api/me', headers=AUTH)
        posted = c.post('/api/deploys', json=[deploy('orders-service', to='3c3c3c3c')], headers=AUTH)
        ran = c.app.state.scheduler.run_due()

    # then
    assert info.status_code == 200 and info.json()['enrich']['gitlab'] is True and posted.status_code == 200 and 'enrich' in ran
    assert me.json()['gitlab'] is True, 'the page learns that GitLab serves, so Releases can say whether a read is still coming'
    assert seen and True not in seen, 'building the GitLab client can take up to 10 s, which would stall every request'


def test_the_jira_client_is_read_off_the_event_loop(store: Store, tmp_path, monkeypatch):
    # given
    seen = []
    client = Runtime.client
    search = f'{JIRA_GATEWAY}/cid/rest/api/3/search/jql'
    people = f'{JIRA_GATEWAY}/cid/rest/api/3/user/search'

    def watched(self, plugin_id, name=None):
        if plugin_id == 'jira':
            seen.append(on_loop())
        return client(self, plugin_id, name)

    monkeypatch.setattr(Runtime, 'client', watched)
    http = FakeHttp({('POST', search): {'issues': [{'key': 'OPS-1', 'fields': {'summary': 'cart'}}]}, ('GET', people): []})

    # when
    with TestClient(create_app(settings(), store, kube=Kube(root=tmp_path), transport=http)) as c:
        c.app.state.instances.create('jira', 'main', JIRA_VALUES, True, NINA)
        me = c.get('/api/me', headers=AUTH)
        info = c.get('/api/server', headers=AUTH)
        found = c.get('/api/jira/search', params={'q': 'cart'}, headers=AUTH)
        named = c.get('/api/people', params={'q': 'nina'}, headers=AUTH)
        ran = c.app.state.scheduler.run_due()

    # then
    assert me.json()['jira'] is True and info.json()['enrich']['jira'] is True and found.json()[0]['key'] == 'OPS-1' and named.status_code == 200 and 'jira-links' in ran
    assert seen and True not in seen, 'building the Jira client can take up to 10 s, which would stall every request'


def test_a_tag_change_delivers_to_the_routes_off_the_event_loop(store: Store, tmp_path, monkeypatch):
    # given
    from skopax.routes import Routes
    seen = []
    store.save({**{'anomalies': [{'key': KEY, 'sev': 'warn', 'status': 'open', 'svc': 'checkout-service', 'label': 'ERROR lines'}]}, 'initialized': True}, None)
    monkeypatch.setattr(Routes, 'deliver', lambda self, changed, now, pushed=(), by='server': seen.append((on_loop(), [a['key'] for a in changed], by)) or 0)

    # when
    with TestClient(create_app(settings(), store, kube=Kube(root=tmp_path))) as c:
        tagged = c.post('/api/tags', json={'key': KEY, 'tag': 'developer', 'on': True}, headers=AUTH)
        assert c.app.state.routes.drain(5)
        said = c.put('/api/business', json={'key': KEY, 'text': 'We recommend checking the checkout page.'}, headers=AUTH)
        assert c.app.state.routes.drain(5)

    # then
    assert (tagged.status_code, said.status_code) == (200, 200)
    assert seen == [(False, [KEY], 'token:bootstrap'), (False, [KEY], 'token:bootstrap')], 'a hung webhook must never hold the event loop'


def test_the_slack_reads_at_startup_run_off_the_event_loop(store: Store, tmp_path, monkeypatch):
    # given
    enabled, tokens = [], []
    factory = Factory()
    app_token, slack_on = SlackLink.app_token, Slack.enabled
    monkeypatch.setattr(SlackLink, 'app_token', lambda self: tokens.append(on_loop()) or app_token(self))
    monkeypatch.setattr(Slack, 'enabled', property(lambda self: enabled.append(on_loop()) or slack_on.fget(self)))
    s = dataclasses.replace(settings(), anthropic_api_key='sk-test', investigate_mode='shadow', slack_listen='shadow')
    app = create_app(s, store, kube=Kube(root=tmp_path), anthropic_client=FakeAnthropic([]), operators_lookup=lambda email: False, slack_socket=factory)
    slack_instance(app, app_token='xapp-test')

    # when
    with TestClient(app):
        wait_for(lambda: bool(factory.made) and factory.made[0].connected.is_set())
        read_enabled, read_tokens = list(enabled), list(tokens)

    # then
    assert factory.tokens == ['xapp-test']
    assert read_enabled and True not in read_enabled, 'Slack.enabled reads the channel setting, which can query Mongo, so the boot backfills and the socket decision read it in a thread'
    assert read_tokens and True not in read_tokens, 'the socket decision reads the app token in a thread'


def test_the_mcp_tools_routes_are_sync_and_read_the_catalog_off_the_event_loop(store: Store, monkeypatch):
    # given
    import inspect
    from pathlib import Path
    from fastapi.routing import APIRoute
    from mcp import types
    from skopax.mcptools.catalog import Catalog
    from skopax.plugins import mcp_tools
    from tests.mcp_fake import FakeServer, tool

    seen = []
    get = Catalog.get
    monkeypatch.setattr(Catalog, 'get', lambda self, name: seen.append(on_loop()) or get(self, name))
    fake = FakeServer(tools=[{**tool('search'), 'annotations': types.ToolAnnotations(readOnlyHint=True).model_dump(exclude_none=True)}],
                      output='{"files": [{"id": "f1", "title": "Runbooks"}]}')
    registry = Registry.of(mcp_tools.PLUGIN, directory=Path(mcp_tools.__file__).parent)
    with TestClient(create_app(settings(), store, registry=registry)) as c:
        c.app.state.mcptools.http = lambda ca: fake.client
        c.post('/api/admin/plugins/mcp-tools/instances', json={'name': 'main', 'values': {'url': 'https://mcp.example.com/mcp'}}, headers=AUTH)
        c.post('/api/admin/plugins/mcp-tools/instances', json={'name': 'docs', 'values': {'url': 'https://drivemcp.googleapis.com/mcp/v1', 'preset': 'google-drive',
                                                                                        'knowledge': 'true'}}, headers=AUTH)
        seen.clear()

        # when
        answers = [c.get('/api/admin/mcp-tools/presets', headers=AUTH), c.post('/api/admin/mcp-tools/instances/main/tools/refresh', headers=AUTH),
                   c.get('/api/admin/mcp-tools/instances/main', headers=AUTH), c.put('/api/admin/mcp-tools/instances/main/tools', json={'on': ['search']}, headers=AUTH),
                   c.post('/api/admin/mcp-tools/instances/main/confirm', json={'understood': True, 'version': 1}, headers=AUTH),
                   c.post('/api/admin/plugins/mcp-tools/test', json={'name': 'main', 'values': {}}, headers=AUTH),
                   c.get('/api/admin/mcp-tools/instances/docs/folders', params={'q': 'run'}, headers=AUTH),
                   c.put('/api/admin/mcp-tools/instances/docs/scope', json={'id': 'f1'}, headers=AUTH)]
        endpoints = [r.endpoint for r in c.app.routes if isinstance(r, APIRoute) and r.path.startswith('/api/admin/mcp-tools/')]

    # then
    assert [a.status_code for a in answers] == [200, 200, 200, 200, 200, 200, 200, 200]
    assert len(endpoints) == 10 and not [e for e in endpoints if inspect.iscoroutinefunction(e)]
    assert seen and True not in seen, 'the catalog lives in Mongo, so every route that reads it runs in a thread'


def test_a_run_reads_its_mcp_connections_and_switches_off_the_event_loop(store: Store, monkeypatch):
    # given
    from pathlib import Path
    from skopax.engine import iso
    from skopax.mcptools.catalog import Catalog
    from skopax.plugins import mcp_tools
    from tests.conftest import anomaly
    from tests.fake_anthropic import FakeAnthropic, reply, text
    from tests.mcp_fake import FakeServer, tool
    from tests.test_investigator import NOW, seed

    seen = []
    for owner, name in ((Catalog, 'get'), (Catalog, 'current'), (Features, 'on')):
        original = getattr(owner, name)
        monkeypatch.setattr(owner, name, lambda self, *a, _o=original, _n=name: (seen.append((_n, a[0], on_loop())) if _n != 'on' or a[0] in ('investigator', 'chat') else None) or _o(self, *a))
    fake = FakeServer(tools=[{**tool('search'), 'annotations': {'readOnlyHint': True}}])
    model = FakeAnthropic([reply(text('nothing to add'), stop='end_turn')])
    seed(store, anomaly(KEY, firstSeen=iso(NOW - 600)))
    s = dataclasses.replace(settings(), anthropic_api_key='sk-test', investigate_mode='shadow')
    with TestClient(create_app(s, store, registry=Registry.of(mcp_tools.PLUGIN, directory=Path(mcp_tools.__file__).parent), anthropic_client=model)) as c:
        c.app.state.mcptools.http = lambda ca: fake.client
        c.post('/api/admin/plugins/mcp-tools/instances', json={'name': 'main', 'values': {'url': 'https://mcp.example.com/mcp'}}, headers=AUTH)
        c.post('/api/admin/mcp-tools/instances/main/tools/refresh', headers=AUTH)
        c.post('/api/admin/mcp-tools/instances/main/confirm', json={'understood': True, 'version': 1}, headers=AUTH)
        c.patch('/api/admin/plugins/mcp-tools/instances/main', json={'enabled': True}, headers=AUTH)
        inv = c.app.state.investigator
        seen.clear()

        async def kicked():
            inv.kick(NOW)

        # when
        asyncio.run(kicked())
        inv._thread.join(10)
        notices = c.get('/api/admin/notices', headers=AUTH)

    # then
    assert notices.status_code == 200 and 'mcp_main_search' in [t['name'] for t in model.requests[0]['tools']]
    assert {n for n, _, _ in seen} == {'get', 'current', 'on'}
    assert [x for x in seen if x[2]] == [], 'the bridge reads Mongo, the live settings and the servers, so it runs in the run thread, never on the event loop'


def test_the_mcp_oauth_routes_are_sync_and_read_the_sign_in_off_the_event_loop(store: Store, monkeypatch):
    # given
    import inspect
    from pathlib import Path
    from fastapi.routing import APIRoute
    from skopax.mcptools.oauth import McpOAuth
    from skopax.plugins import mcp_tools
    from tests.mcp_fake import FakeAuth

    seen, indexed = [], []
    doc = McpOAuth._doc
    monkeypatch.setattr(McpOAuth, '_doc', lambda self, name: seen.append(on_loop()) or doc(self, name))
    ensure = McpOAuth.ensure_indexes
    monkeypatch.setattr(McpOAuth, 'ensure_indexes', lambda self: indexed.append(on_loop()) or ensure(self))
    fake = FakeAuth()
    registry = Registry.of(mcp_tools.PLUGIN, directory=Path(mcp_tools.__file__).parent)
    with TestClient(create_app(settings(), store, registry=registry)) as c:
        c.app.state.mcptools.http = lambda ca: fake.client
        c.post('/api/admin/plugins/mcp-tools/instances', json={'name': 'main', 'values': {'url': 'https://mcp.example.com/mcp', 'auth': 'oauth'}}, headers=AUTH)
        seen.clear()

        # when
        url = c.post('/api/admin/mcp-tools/instances/main/oauth/start', headers=AUTH).json()['url']
        code, state = fake.authorize(url)
        answers = [c.get('/api/admin/mcp-tools/oauth/callback', params={'code': code, 'state': state}, headers=AUTH, follow_redirects=False),
                   c.get('/api/admin/mcp-tools/instances/main', headers=AUTH), c.get('/api/admin/notices', headers=AUTH),
                   c.delete('/api/admin/mcp-tools/instances/main/oauth', headers=AUTH)]
        endpoints = [r.endpoint for r in c.app.routes if isinstance(r, APIRoute) and '/oauth' in r.path and r.path.startswith('/api/admin/mcp-tools/')]

    # then
    assert [a.status_code for a in answers] == [303, 200, 200, 204] and fake.exchanges == 1
    assert len(endpoints) == 3 and not [e for e in endpoints if inspect.iscoroutinefunction(e)]
    assert seen and True not in seen, 'the sign-in lives in Mongo, so every route that reads it runs in a thread'
    assert indexed == [False], 'the OAuth indexes are built at startup in a thread'
