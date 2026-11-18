import hashlib
import http.client
import json
import logging
import threading
import time
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from skopax.app import create_app
from skopax.contract import Outbound
from skopax.engine import PassResult
from skopax.kube import Kube
from skopax.messages import msg
from skopax.plugins import teams
from skopax.plugins.teams import cards as teams_cards
from skopax.registry import Registry
from skopax.remote import HttpError, Reply, Unreachable
from skopax.routes import Route
from skopax.store import Store
from tests.conftest import AUTH, settings, state
from tests.plugins.hook_plugin import Hook, hook_plugin
from tests.teams_kit import AT, BIZDEV, MARKETING, SEO, TECH, Transport, add_route, open_card, operator_me, route, routes_of, runtime_of, tag, texts

KEY = 'log|checkout-service|ERROR|boom'
HOOK_URL = 'https://hooks.example.com/web'
NOW = 1_794_240_000.0
SAID = {'text': 'We recommend adding a redirect for the old spring sale page.', 'why': 'Visitors from the old campaign land on an empty page.', 'by': 'pod', 'at': AT}


def outbound_threads() -> None:
    for t in threading.enumerate():
        if t.name == 'outbound':
            t.join(5)


def test_any_outbound_plugin_gets_the_same_routing(store: Store, monkeypatch):
    # given
    hook = Hook()
    plugin = hook_plugin(hook)
    given: list[dict] = []
    render = teams_cards.render
    monkeypatch.setattr(teams_cards, 'render', lambda message: given.append(message) or render(message))
    told = open_card('log|a|ERROR|told', tags=tag('seo'), business={'text': 'Newsletter sign-ups fail right now; engineering is fixing it.', 'kind': 'info', 'by': 'pod', 'at': AT})
    store.save(state(open_card(KEY, tags=tag('seo'), business=SAID), told, open_card('log|a|ERROR|plain', 'crit')), None)
    routes = routes_of(store, Transport(), route('seo', SEO, tags='seo'), (plugin, 'web', {'url': HOOK_URL, 'tags': 'seo'}), plugins=(teams.PLUGIN, plugin))

    # when
    sent = routes.tagged(store.load()['anomalies'], now=NOW)

    # then
    assert sent == {'teams/seo': [KEY, 'log|a|ERROR|told'], 'hook/web': [KEY, 'log|a|ERROR|told']}
    assert [m for _, m in hook.posts] == given and [m['kind'] for m in given] == ['action', 'info']
    assert [[(line['role'], line['label'], line['text']) for line in m['lines']] for m in given] == [
        [('why', 'Why', SAID['why']), ('recommended', 'Recommended', SAID['text'])], [('info', 'Info', told['business']['text'])]], 'core sets the order and the words: the why leads'
    assert [p['plugin'] for p in store.anomaly(KEY)['teamsPosts']] == ['hook', 'teams']


def test_routes_of_two_plugins_with_one_name_both_post_and_an_old_post_with_no_plugin_counts_for_either(store: Store):
    # given
    hook = Hook()
    plugin = hook_plugin(hook)
    t = Transport()
    old = open_card('log|a|ERROR|old', tags=tag('seo'), business=SAID, teamsPosts=[{'route': 'seo', 'at': '2026-11-09T11:00:00Z', 'by': 'server'}])
    store.save(state(open_card(KEY, tags=tag('seo'), business=SAID), old), None)
    routes = routes_of(store, t, route('seo', SEO, tags='seo'), (plugin, 'seo', {'url': HOOK_URL, 'tags': 'seo'}), plugins=(teams.PLUGIN, plugin))

    # when
    first = routes.tagged(store.load()['anomalies'], now=NOW)
    again = routes.tagged(store.load()['anomalies'], now=NOW + 60)

    # then
    assert first == {'hook/seo': [KEY], 'teams/seo': [KEY]} and again == {}
    assert len(t.calls) == 1 and [name for name, _ in hook.posts] == ['seo'], 'a route of another plug-in with the same name does not take the post'
    assert sorted(p['plugin'] for p in store.anomaly(KEY)['teamsPosts']) == ['hook', 'teams']
    assert len(store.anomaly('log|a|ERROR|old')['teamsPosts']) == 1, 'a stored post from before plug-ins counts for every plug-in'


def held_push(routes, card: dict, hook: Hook) -> threading.Thread:
    worker = threading.Thread(target=routes.push, args=([card],))
    worker.start()
    end = time.monotonic() + 5
    while not hook.posts and time.monotonic() < end:
        time.sleep(0.01)
    return worker


def test_a_push_refused_while_an_earlier_post_runs_is_posted_at_the_next_delivery_once(store: Store, caplog):
    # given
    gate = threading.Event()
    hook = Hook(hold=gate)
    plugin = hook_plugin(hook)
    first, second = open_card(KEY, 'crit'), open_card('log|a|ERROR|next', 'crit')
    store.save(state(first, second), None)
    routes = routes_of(store, Transport(), (plugin, 'ops', {'url': HOOK_URL, 'keys': 'log|*'}), plugins=(plugin,))

    # when
    with caplog.at_level(logging.WARNING, logger='skopax.routes'):
        worker = held_push(routes, first, hook)
        refused = routes.push([second], now=NOW)
        gate.set()
        worker.join(5)
        delivered = routes.deliver([], NOW)
        again = routes.deliver([], NOW + 60)

    # then
    assert refused == {} and (delivered, again) == (1, 0)
    assert [m['card']['key'] for _, m in hook.posts] == [KEY, 'log|a|ERROR|next']
    assert [p['route'] for p in store.anomaly('log|a|ERROR|next')['teamsPosts']] == ['ops']
    assert 'route hook/ops post failed: busy' in caplog.text and 'TimedOut' not in caplog.text


def test_a_failed_push_is_tried_again_with_backoff_and_then_dropped_with_one_line(store: Store, caplog):
    # given
    hook = Hook(answer={'ok': False, 'status': 500})
    plugin = hook_plugin(hook)
    store.save(state(open_card(KEY, 'crit')), None)
    routes = routes_of(store, Transport(), (plugin, 'ops', {'url': HOOK_URL, 'keys': 'log|*'}), plugins=(plugin,))

    # when
    with caplog.at_level(logging.WARNING, logger='skopax.routes'):
        pushed = routes.push([store.anomaly(KEY)], now=NOW)
        retried = sum(routes.deliver([], NOW + offset) for offset in range(30, 7200, 30))

    # then
    assert (pushed, retried) == ({}, 0) and len(hook.posts) == 6
    assert caplog.text.count(f'route hook/ops gave up on {KEY} after 6 tries: HTTP 500') == 1


def test_a_settings_read_that_fails_uses_the_default_and_says_so_while_a_missing_setting_stays_quiet(store: Store, caplog):
    # given
    plugin = hook_plugin(Hook())
    routes = routes_of(store, Transport(), route('seo', SEO, tags='seo'), (plugin, 'web', {'url': HOOK_URL, 'tags': 'seo'}), plugins=(teams.PLUGIN, plugin), min_sev='crit')

    # when
    with caplog.at_level(logging.WARNING, logger='skopax.routes'):
        quiet = (routes.setting('hook', 'min-sev'), routes.setting('teams', 'min-sev'))
        routes.runtime.live = SimpleNamespace(plugin_value=lambda key: 1 / 0)
        broken = routes.setting('teams', 'min-sev')

    # then
    assert quiet == ('warn', 'crit') and broken == 'warn'
    assert 'route setting min-sev of plug-in teams not read, using its default: ZeroDivisionError' in caplog.text and 'plug-in hook' not in caplog.text


def test_send_by_hand_needs_the_tag_and_the_sentence(store: Store, tmp_path):
    # given
    t = Transport()
    bare = open_card('log|a|ERROR|bare', 'crit')
    quiet = open_card('log|a|ERROR|quiet', 'crit', tags=tag('seo'))
    ready = open_card('log|a|ERROR|ready', 'crit', tags=tag('seo'), business={'text': 'We recommend checking the product page.', 'by': 'pod', 'at': AT})
    store.save({**state(bare, quiet, ready), 'initialized': True}, None)

    # when
    with TestClient(create_app(settings(), store, kube=Kube(root=tmp_path), transport=t)) as c:
        add_route(c, 'seo', SEO, tags='seo')
        add_route(c, 'tech', TECH, keys='*', min_sev='crit')
        untagged = c.post('/api/routes/send', json={'key': 'log|a|ERROR|bare', 'route': 'teams/seo'}, headers=AUTH)
        unsaid = c.post('/api/routes/send', json={'key': 'log|a|ERROR|quiet', 'route': 'teams/seo'}, headers=AUTH)
        auto = c.post('/api/routes/send', json={'key': 'log|a|ERROR|quiet'}, headers=AUTH)
        sent = c.post('/api/routes/send', json={'key': 'log|a|ERROR|ready', 'route': 'teams/seo'}, headers=AUTH)
        unknown = c.post('/api/routes/send', json={'key': 'log|a|ERROR|ready', 'route': 'teams/finance'}, headers=AUTH)
        gone = c.post('/api/routes/send', json={'key': 'log|x|ERROR|y', 'route': 'teams/seo'}, headers=AUTH)
        anonymous = c.post('/api/routes/send', json={'key': 'log|a|ERROR|ready'})

    # then
    assert (untagged.status_code, unsaid.status_code) == (422, 422)
    assert 'no SEO tag' in untagged.json()['detail'] and 'business sentence' in unsaid.json()['detail']
    assert auto.json() == {'ok': True, 'sent': ['teams/tech']} and sent.json() == {'ok': True, 'sent': ['teams/seo']}
    assert (unknown.status_code, gone.status_code, anonymous.status_code) == (404, 404, 401)
    assert unknown.json()['detail'] == msg('integrations.route_unknown', route='teams/finance')
    assert [u for u, _ in t.calls] == [TECH, SEO] and texts(t.calls[1][1])[:2] == ['Skopax \u00b7 SEO', '\U0001f534 Urgent \u00b7 **Recommended:** We recommend checking the product page.']


def test_a_by_hand_post_the_chat_tool_refuses_answers_502(store: Store, tmp_path):
    # given
    store.save({**state(open_card(KEY, 'crit')), 'initialized': True}, None)

    # when
    with TestClient(create_app(settings(), store, kube=Kube(root=tmp_path), transport=Transport(fail='sig=TECH'))) as c:
        add_route(c, 'tech', TECH, keys='*')
        refused = c.post('/api/routes/send', json={'key': KEY, 'route': 'teams/tech'}, headers=AUTH)

    # then
    assert refused.status_code == 502 and refused.json()['detail'] == msg('integrations.route_failed', plugin='Microsoft Teams', reason='HTTP 400')
    assert 'server log' not in refused.json()['detail'] and 'sig=' not in refused.text
    assert not store.anomaly(KEY).get('teamsPosts')


class Leaky:
    def __init__(self):
        self.calls: list[str] = []

    def __call__(self, method, url, headers, data, timeout):
        self.calls.append(url)
        if 'sig=SECRET&' in url or url.endswith('sig=SECRET'):
            raise HttpError(400, url, 'refused ' + url)
        if 'SECRET2' in url:
            raise Unreachable(f'{url} unreachable')
        if 'SECRET3' in url:
            raise http.client.InvalidURL(f"URL can't contain control characters. {url!r}")
        return Reply(None, {})


def test_a_webhook_never_leaves_the_server(store: Store, tmp_path, caplog):
    # given
    hooks = {'first': 'https://hooks.example.com/workflows/a/invoke?sig=SECRET', 'second': 'https://hooks.example.com/workflows/b/invoke?sig=SECRET2',
             'third': 'https://hooks.example.com/workflows/c/invoke?sig=SECRET3'}
    t = Leaky()
    store.save({**state(open_card(KEY, 'crit', tags=tag('seo'), business=SAID)), 'initialized': True}, None)

    # when
    with caplog.at_level(logging.DEBUG):
        with TestClient(create_app(settings(), store, kube=Kube(root=tmp_path), transport=t)) as c:
            made = [add_route(c, name, hook, tags='seo') for name, hook in hooks.items()]
            admin = c.get('/api/admin/plugins', headers=AUTH)
            me = json.dumps(operator_me(c, store))
            sent = [c.post('/api/routes/send', json={'key': KEY, 'route': f'teams/{name}'}, headers=AUTH) for name in hooks]
            tested = [c.post('/api/admin/plugins/teams/test', json={'name': name}, headers=AUTH) for name in hooks]
            delivered = c.app.state.routes.deliver([store.anomaly(KEY)], NOW)
            runtime = c.app.state.runtime
            answer = teams.PLUGIN.outbound.post(runtime.context(teams.PLUGIN, runtime.instances.get('teams', 'first'), NOW), c.app.state.routes.business_message(store.anomaly(KEY), 'SEO', ()))
            activity = store.list_activity(None, 100)

    # then
    assert [r.status_code for r in sent] == [502, 502, 502] and delivered == 0
    assert answer == {'ok': False, 'status': 400}
    assert len(t.calls) == 10
    seen = [json.dumps(made), admin.text, me, *[r.text for r in sent], *[r.text for r in tested], json.dumps(activity, default=str), caplog.text, json.dumps(answer)]
    assert [s for s in seen if 'sig=SECRET' in s or 'example.com/workflows' in s] == []


def test_a_post_that_hangs_is_a_failed_post_released_and_tried_again_at_the_next_pass(store: Store, caplog):
    # given
    gate = threading.Event()
    hook = Hook(hold=gate)
    plugin = hook_plugin(hook)
    store.save(state(open_card(KEY, tags=tag('seo'), business=SAID)), None)
    routes = routes_of(store, Transport(), (plugin, 'web', {'url': HOOK_URL, 'tags': 'seo'}), plugins=(plugin,), timeout=0.2)

    # when
    with caplog.at_level(logging.WARNING, logger='skopax.routes'):
        hung = routes.tagged(store.load()['anomalies'], now=NOW)
        busy = routes.tagged(store.load()['anomalies'], now=NOW + 60)
        claimed = store.anomaly(KEY).get('teamsPosts')
        gate.set()
        outbound_threads()
        later = routes.tagged(store.load()['anomalies'], now=NOW + 120)

    # then
    assert hung == {} and busy == {} and not claimed, 'a slow post is a failed post: its claim is taken back'
    assert later == {'hook/web': [KEY]} and len(hook.posts) == 2, 'the call made while the first one still ran never reached the plug-in'
    assert caplog.text.count('route hook/web post failed: TimedOut') == 1 and caplog.text.count('route hook/web post failed: busy') == 1, 'the second call names the real reason'
    assert [p['route'] for p in store.anomaly(KEY)['teamsPosts']] == ['web']


@pytest.mark.parametrize('answer', [RuntimeError('boom at https://hooks.example.com/web?sig=S'), 'yes', {'ok': 'yes'}, {'ok': False, 'status': 500}])
def test_a_post_that_raises_or_gives_no_usable_answer_is_a_failed_post(store: Store, caplog, answer):
    # given
    plugin = hook_plugin(Hook(answer=answer))
    store.save(state(open_card(KEY, tags=tag('seo'), business=SAID)), None)
    routes = routes_of(store, Transport(), (plugin, 'web', {'url': HOOK_URL, 'tags': 'seo'}), plugins=(plugin,))

    # when
    with caplog.at_level(logging.WARNING, logger='skopax.routes'):
        sent = routes.tagged(store.load()['anomalies'], now=NOW)

    # then
    assert sent == {} and not store.anomaly(KEY).get('teamsPosts')
    assert 'route hook/web post failed' in caplog.text and 'sig=' not in caplog.text and 'boom' not in caplog.text


def test_delivery_without_routes_does_nothing_and_a_failing_store_never_reaches_the_pass(store: Store, monkeypatch, caplog):
    # given
    routes = routes_of(store, Transport())
    with_route = routes_of(store, Transport(), route('seo', SEO, tags='seo'))
    monkeypatch.setattr(type(with_route), 'tagged', lambda self, *a, **k: 1 / 0)

    # when
    with caplog.at_level(logging.WARNING, logger='skopax.routes'):
        none = routes.deliver([open_card(KEY, tags=tag('seo'), business=SAID)], NOW)
        failed = with_route.deliver([open_card(KEY, tags=tag('seo'), business=SAID)], NOW)

    # then
    assert (none, failed) == (0, 0) and routes.enabled is False
    assert 'route delivery skipped: ZeroDivisionError' in caplog.text


def outbound_of(**over) -> Outbound:
    hook = Hook()
    return Outbound(**({'post': hook.post, 'tags_field': 'tags', 'mentions_field': 'mentions', 'link_field': 'url', 'keys_field': 'keys'} | over))


@pytest.mark.parametrize('outbound', [outbound_of(tags_field='nope'), outbound_of(tags_field='url'), outbound_of(mentions_field='url'), outbound_of(link_field='nope'),
                                      outbound_of(keys_field='url'), outbound_of(sev_field='tags'), outbound_of(post='post')])
def test_outbound_names_fields_of_the_right_kind(outbound: Outbound):
    # when
    found = Registry.of(hook_plugin(Hook(), outbound=outbound)).get('hook')

    # then
    assert found.state == 'broken' and found.reason.endswith(': ' + msg('plugins.why.outbound'))


def test_a_good_outbound_loads_and_the_route_link_is_a_hash_of_the_link_field(store: Store):
    # given
    plugin = hook_plugin(Hook())

    # when
    runtime = runtime_of(store, Transport(), (plugin, 'web', {'url': HOOK_URL, 'tags': 'SEO, tag:Developer, finance', 'mentions': 'Alice.Archer@example.com, bob'}), plugins=(plugin,))

    # then
    assert Registry.of(plugin).get('hook').state == 'ok'
    assert runtime.routes() == [Route('hook/web', 'web', 'hook', 'web', frozenset({'seo', 'developer'}), ('alice.archer@example.com',), hashlib.sha256(HOOK_URL.encode()).hexdigest()[:16])]


def test_a_route_with_a_bad_link_or_no_known_tag_is_skipped_and_its_health_says_why(store: Store, tmp_path, caplog):
    # given
    t = Transport()

    # when
    with caplog.at_level(logging.WARNING, logger='skopax.runtime'):
        with TestClient(create_app(settings(), store, kube=Kube(root=tmp_path), transport=t)) as c:
            add_route(c, 'plain', 'http://hooks.example.com/workflows/x?sig=PLAIN', tags='seo')
            add_route(c, 'lost', SEO, tags='finance, type:crux')
            add_route(c, 'good', BIZDEV, tags='bizdev')
            row = {p['id']: p for p in c.get('/api/admin/plugins', headers=AUTH).json()['plugins']}['teams']
            found = [r.id for r in c.app.state.routes.routes()]
            tested = c.post('/api/admin/plugins/teams/test', json={'name': 'plain'}, headers=AUTH).json()

    # then
    health = {r['name']: r for r in row['health']['instances']}
    assert health['plain'] == {'name': 'plain', 'state': 'error', 'text': msg('integrations.route_link'), 'at': None}
    assert health['lost'] == {'name': 'lost', 'state': 'error', 'text': msg('integrations.route_empty'), 'at': None}
    assert health['good']['state'] != 'error' and found == ['teams/good']
    assert (tested['ok'], tested['summary']) == (False, msg('integrations.route_link')) and t.calls == [], 'the test refuses the link before it posts'
    assert caplog.text.count('route lost takes finance, type:crux, which no card can carry') == 1 and 'PLAIN' not in caplog.text


def test_the_page_learns_the_routes_for_operators_only(store: Store, tmp_path):
    # given
    store.save({**state(), 'initialized': True}, None)

    # when
    with TestClient(create_app(settings(), store, kube=Kube(root=tmp_path), transport=Transport())) as c:
        add_route(c, 'growth', SEO, tags='seo, bizdev')
        add_route(c, 'mixed', MARKETING, tags='seo', keys='page-*')
        add_route(c, 'tech', TECH, keys='log|*')
        add_route(c, 'eng', BIZDEV, tags='developer')
        operator = operator_me(c, store)['routes']
        guest = c.get('/api/me').json()['routes']

    # then
    assert operator == [{'id': 'teams/eng', 'name': 'eng', 'tags': ['developer'], 'tagged': True, 'business': False, 'plugin': 'Microsoft Teams'},
                        {'id': 'teams/growth', 'name': 'growth', 'tags': ['bizdev', 'seo'], 'tagged': True, 'business': True, 'plugin': 'Microsoft Teams'},
                        {'id': 'teams/mixed', 'name': 'mixed', 'tags': ['seo'], 'tagged': False, 'business': False, 'plugin': 'Microsoft Teams'},
                        {'id': 'teams/tech', 'name': 'tech', 'tags': [], 'tagged': False, 'business': False, 'plugin': 'Microsoft Teams'}]
    assert 'link' not in json.dumps(operator)
    assert guest == []


def test_a_pass_delivers_its_pushed_and_tagged_cards(store: Store, tmp_path):
    # given
    t = Transport()
    found = open_card(KEY, 'crit') | {'src': 'log', 'sig': 'err', 'ns': 'shop', 'sub': '', 'now': 5, 'd1': None, 'd7': None, 'unit': '', 'short': 'x', 'slack': 1, 'ranKey': 'log'}
    store.save({**state(), 'initialized': True}, None)

    # when
    with TestClient(create_app(settings(), store, kube=Kube(root=tmp_path), transport=t)) as c:
        add_route(c, 'tech', TECH, keys='log|*')
        add_route(c, 'eng', MARKETING, tags='developer')
        c.app.state.server_apply([PassResult('log', True, 'ok', [found], {}, ('log',))], NOW, None)
        assert c.app.state.routes.drain(5)
        c.post('/api/tags', json={'key': KEY, 'tag': 'developer', 'on': True}, headers=AUTH)
        assert c.app.state.routes.drain(5)

    # then
    assert [u for u, _ in t.calls] == [TECH, MARKETING]
    assert [(p['route'], p['plugin']) for p in store.anomaly(KEY)['teamsPosts']] == [('tech', 'teams'), ('eng', 'teams')]
