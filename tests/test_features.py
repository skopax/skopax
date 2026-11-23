import dataclasses
import logging
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import pytest
from fastapi.testclient import TestClient

from tests.conftest import AUTH, TOKEN, anomaly, ready_app, settings, state
from tests.fake_anthropic import FakeAnthropic, reply, text, verdict
from tests.fake_http import FakeHttp
from tests.fake_slack import FakeSlackApi, fake_slack, slack_instance
from tests.plugin_kit import add_instance, registry_with
from tests.plugins import entitled_plugin
from tests.plugins.base import minimal
from tests.test_auth import app_client, sso_settings
from tests.test_investigator import KEY as RUN_KEY, NOW, investigator as card_investigator, note_and_recap, seed
from tests.test_slack import FakeSlack
from tests.test_slack_dm_chats import DM, dm_event
from tests.test_slack_listener import Factory
from tests.test_slackevents import NOW as SLACK_NOW, FakeInvestigator, card_with_post, intake, reply_event, top_event
from skopax import chatui, mcpserver
from skopax.chatui.text import pending_line, refusal
from skopax.contract import Hooks
from skopax.app import create_app
from skopax.config import load_settings
from skopax.engine import PassResult, anomaly as found_card, iso
from skopax.features import FEATURES, KEYS, Features, off_text
from skopax.messages import msg
from skopax.plugins import prometheus
from skopax.slack import CREATE_TASK, Slack
from skopax.store import Store
from skopax.tunables import STATIC, Live, TunableStore

IDS = ('investigator', 'chat', 'slack-autosend', 'slack-listener', 'mcp', 'public-read', 'push', 'postdeploy', 'self-review', 'update-check', 'tag-report', 'warn-digest', 'settle', 'langfuse-tracing', 'hide-attribution')
ICONS = Path(__file__).resolve().parent.parent / 'skopax' / 'web' / 'static' / 'icons.js'
KEY = 'log|checkout-service|ERROR|boom'
DRAFT = 'checkout-service showed 116 ERROR lines, 13:05 UTC.\nhttps://skopax.example.com/#a=x'
USER = 'U0123ABCD'
PROD = 'https://prom.example.com'
HIDE_LABEL = 'Hide "powered by Skopax"'


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@dataclass
class Scene:
    client: TestClient
    probe: Callable[[int], object]
    off: object
    on: object
    ready: Callable[[], object] = lambda: None
    turn_on: Callable[[], object] | None = None


def switch(c: TestClient, feature_id: str, on: bool):
    return c.put(f'/api/admin/features/{feature_id}', json={'on': on}, headers=AUTH)


def items(c: TestClient) -> dict[str, dict]:
    return {i['id']: i for i in c.get('/api/admin/features', headers=AUTH).json()['items']}


def jobs(c: TestClient) -> list[str]:
    return [j.name for j in c.app.state.scheduler.jobs]


def fresh_card(store: Store) -> None:
    now = time.time()
    store.save_state(state(anomaly(KEY, firstSeen=iso(now - 600))) | {'initialized': True, 'history': [{'t': iso(now - 300), 'key': KEY, 'ev': 'new', 'sev': 'warn', 'text': KEY, 'muted': False}]}, None)


def status_of(store: Store, action_id: str) -> str:
    return next(a['status'] for a in store.list_actions(None, 20) if a['id'] == action_id)


def ask_scene(store: Store, **over) -> Scene:
    c = TestClient(create_app(dataclasses.replace(settings(), anthropic_api_key='sk-test', **over), store, anthropic_client=FakeAnthropic([], [verdict(), verdict()])))

    def probe(n: int):
        res = c.post('/api/actions', json={'type': 'ask', 'key': 'ask', 'target': 'ask', 'text': f'Why did checkout-service spike at 13:0{n}?'}, headers=AUTH)
        return res.status_code, res.json().get('detail'), c.app.state.investigator.why_halted(time.time())

    return Scene(c, probe, (409, off_text('investigator'), 'off'), (201, None, None))


def chat_scene(store: Store, **over) -> Scene:
    c = TestClient(ready_app(dataclasses.replace(settings(), **over), store, anthropic_client=FakeAnthropic([], [verdict(), verdict()])))

    def probe(n: int):
        res = c.post('/api/chats', json={'text': f'What needs attention now, take {n}?'}, headers=AUTH)
        return res.status_code, res.json().get('detail')

    return Scene(c, probe, (409, off_text('chat')), (201, None))


def autosend_scene(store: Store, **over) -> Scene:
    store.save(state(anomaly(KEY, sev='crit')), None)
    transport = FakeSlack()
    c = TestClient(create_app(dataclasses.replace(settings(), **over), store, slack=Slack('xoxb-test', '#ops', transport, FakeSlackApi())))

    def probe(n: int):
        store.slack_draft(KEY, f'{DRAFT} {n}', 'token')
        return c.app.state.auto_send.run_once(), len(transport.calls), bool(store.anomaly(KEY).get('slackPost'))

    return Scene(c, probe, (0, 0, False), (1, 1, True), ready=lambda: None)


def listener_scene(store: Store, **over) -> Scene:
    slack, post, _ = fake_slack(emails={'U1': 'adam.baker@example.com'})
    fake = FakeAnthropic([], [verdict(on_topic=False, reason='asks for a poem')])
    s = dataclasses.replace(settings(), anthropic_api_key='sk-test', investigate_mode='shadow', slack_listen='shadow', **over)
    app = create_app(s, store, slack=slack, anthropic_client=fake, operators_lookup=lambda email: False, slack_socket=Factory())
    slack_instance(app, app_token='xapp-test')
    c = TestClient(app)
    click = {'type': 'block_actions', 'user': {'id': 'U1'}, 'channel': {'id': 'C1'}, 'message': {'ts': '1792224000.000100'}, 'actions': [{'action_id': CREATE_TASK, 'value': 'gone'}]}

    def probe(n: int):
        heard = c.app.state.intake.handle({'type': 'app_mention', 'channel': 'C1', 'user': 'U1', 'text': '<@UBOT> write me a poem', 'ts': f'1792224000.00{n}000'})
        clicked = c.app.state.clicks.handle(click)
        return heard, clicked, len(store.list_intake(10)), len(fake.gate_requests), len(store.list_actions(None, 10)), len(post.calls)

    return Scene(c, probe, (None, None, 0, 0, 0, 0), ('off-topic', 'not-operator', 1, 1, 0, 0))


def mcp_scene(store: Store, **over) -> Scene:
    c = app_client(store, s=dataclasses.replace(sso_settings(), mcp_issuer='https://login.dev.example.com', **over))

    def probe(n: int):
        res = c.post('/mcp', json={'jsonrpc': '2.0', 'id': n, 'method': 'tools/list'})
        return res.status_code, res.json().get('detail')

    return Scene(c, probe, (409, off_text('mcp')), (401, 'log in first'))


def public_scene(store: Store, **over) -> Scene:
    c = TestClient(create_app(dataclasses.replace(settings(), **over), store))
    return Scene(c, lambda n: c.get('/api/state').status_code, 401, 200)


def push_scene(store: Store, **over) -> Scene:
    store.save_state({'anomalies': [], 'initialized': True}, None)
    transport = FakeSlack()
    c = TestClient(create_app(dataclasses.replace(settings(), slack_push_user=USER, **over), store, slack=Slack('xoxb-test', '#ops', transport, FakeSlackApi())))

    def probe(n: int):
        card = found_card('prom', 'err', 'shop', f'checkout-service-{n}', '', 'crit', 50, 0, 0, '', 'ERROR logs', 'errors')
        c.app.state.server_apply([PassResult('prom', True, 'ok', [card], {}, ('prom',))], time.time(), None)
        return len([p for _, p in transport.calls if p.get('channel') == USER])

    return Scene(c, probe, 0, 1)


def postdeploy_scene(store: Store, **over) -> Scene:
    add_instance(store, prometheus.PLUGIN, 'main', url=PROD)
    c = TestClient(create_app(dataclasses.replace(settings(), **over), store, transport=FakeHttp()))
    settled = store.add_action({'type': 'postdeploy', 'key': 'postdeploy', 'target': 'postdeploy', 'label': 'Post-deploy check after a release', 'requestedBy': 'server'})

    def probe(n: int):
        listed = 'postdeploy' in jobs(c)
        ran = 'postdeploy' in c.app.state.scheduler.run_due()
        manual = c.post('/api/actions', json={'type': 'postdeploy', 'key': 'postdeploy', 'target': 'postdeploy', 'label': 'Post-deploy check'}, headers=AUTH)
        return listed, ran, status_of(store, settled['id']), manual.status_code, manual.json().get('detail')

    return Scene(c, probe, (False, False, 'requested', 409, off_text('postdeploy')), (True, True, 'done', 201, None))


def card_run_scene(store: Store, **over) -> Scene:
    fresh_card(store)
    fake = FakeAnthropic([reply(text('Checked checkout-service: one client.'), stop='end_turn')])
    c = TestClient(create_app(dataclasses.replace(settings(), anthropic_api_key='sk-test', investigate_mode='live', **over), store, anthropic_client=fake))

    def probe(n: int):
        c.app.state.investigator.run_once(time.time())
        return 'investigate' in jobs(c), [r['keys'] for r in store.list_investigations(None, 5)]

    return Scene(c, probe, (True, []), (True, [[KEY]]))


def mode_scene(store: Store) -> Scene:
    fresh_card(store)
    fake = FakeAnthropic([reply(text('Checked checkout-service: one client.'), stop='end_turn')])
    c = TestClient(create_app(dataclasses.replace(settings(), anthropic_api_key='sk-test'), store, anthropic_client=fake))

    def probe(n: int):
        run = c.app.state.investigator.run_once(time.time())
        return 'investigate' in jobs(c), run and run['mode'], items(c)['investigator']['needs']

    return Scene(c, probe, (False, None, [msg('features.needs.mode')]), (True, 'shadow', []),
                 turn_on=lambda: c.put('/api/admin/settings', json={'key': 'investigate.mode', 'value': 'shadow'}, headers=AUTH))


def self_review_scene(store: Store) -> Scene:
    c = TestClient(create_app(dataclasses.replace(settings(), anthropic_api_key='sk-test', self_project=''), store))

    def probe(n: int):
        item = items(c)['self-review']
        return 'self-review' in jobs(c), item['on'], item['needs']

    return Scene(c, probe, (False, False, [msg('features.needs.self_project')]), (False, True, [msg('features.needs.self_project')]))


def hide_scene(store: Store) -> Scene:
    c = TestClient(create_app(settings(), store, registry=registry_with(entitled_plugin)))

    def probe(n: int):
        return c.app.state.features.on('hide-attribution'), c.get('/api/me', headers=AUTH).json()['features']['hide-attribution']

    return Scene(c, probe, (False, False), (True, True))


def update_scene(store: Store, **over) -> Scene:
    store.note_version_check({'latest': '9.9.9', 'url': 'https://github.com/skopax/skopax/releases/tag/v9.9.9', 'securityIn': [], 'at': '2026-11-19T00:00:00Z'})
    c = TestClient(create_app(dataclasses.replace(settings(), **over), store, transport=FakeHttp()))

    def probe(n: int):
        shown = [i['id'] for i in c.get('/api/admin/notices', headers=AUTH).json()['items']]
        return 'version-check' in jobs(c), 'update' in shown

    return Scene(c, probe, (False, False), (True, True))


LIVE = {'investigator': ask_scene, 'chat': chat_scene, 'slack-autosend': autosend_scene, 'slack-listener': listener_scene, 'mcp': mcp_scene, 'public-read': public_scene,
        'push': push_scene, 'postdeploy': postdeploy_scene, 'update-check': update_scene}
BOOTED = {'investigator': lambda store: card_run_scene(store, investigator_on=False), 'investigate.mode': mode_scene,
          'slack-autosend': lambda store: autosend_scene(store, slack_autosend=False), 'mcp': lambda store: mcp_scene(store, mcp_on=False),
          'public-read': lambda store: public_scene(store, public_read=False), 'push': lambda store: push_scene(store, push_on=False),
          'postdeploy': lambda store: postdeploy_scene(store, postdeploy=False), 'self-review': self_review_scene, 'update-check': lambda store: update_scene(store, update_check=False), 'hide-attribution': hide_scene}


def test_features_list_every_core_feature_in_order():
    # given
    icons = set(re.findall(r"^  (\w+): '", ICONS.read_text(encoding='utf-8'), re.M))
    tunables = {t.key: t for t in STATIC}

    # then
    assert tuple(f.id for f in FEATURES) == IDS
    assert [f.id for f in FEATURES if f.icon not in icons] == []
    assert KEYS == frozenset(f.key for f in FEATURES) and len(KEYS) == len(IDS)
    assert [f.key for f in FEATURES if f.key not in tunables or tunables[f.key].kind != 'bool' or not tunables[f.key].live] == []
    assert [f.id for f in FEATURES if f.edition != 'community'] == ['hide-attribution']
    assert {f.id: f.started for f in FEATURES if f.started} == {'chat': 'chat-ui', 'slack-listener': 'slack-socket'}
    assert tunables['investigate.mode'].live is True and tunables['core.postdeploy'].live is True


def test_defaults_keep_every_feature_on_except_four(store: Store):
    # given
    base = dataclasses.replace(load_settings(env={}), token=TOKEN)

    # when
    with TestClient(create_app(base, store)) as c:
        listed = {i['id']: i['on'] for i in c.get('/api/admin/features', headers=AUTH).json()['items']}
        mine = c.get('/api/me', headers=AUTH).json()['features']

    # then
    assert tuple(listed) == IDS
    assert [k for k, on in listed.items() if not on] == ['public-read', 'self-review', 'langfuse-tracing', 'hide-attribution']
    assert mine == listed


def test_me_carries_the_server_off_text_of_each_switched_off_feature(store: Store):
    # given
    base = dataclasses.replace(load_settings(env={}), token=TOKEN)

    # when
    with TestClient(create_app(base, store)) as c:
        before = c.get('/api/me', headers=AUTH).json()['featureOff']
        c.put('/api/admin/features/investigator', json={'on': False}, headers=AUTH)
        after = c.get('/api/me', headers=AUTH).json()['featureOff']

    # then
    assert before == {f: off_text(f) for f in ('public-read', 'self-review', 'langfuse-tracing', 'hide-attribution')}
    assert after['investigator'] == off_text('investigator') == msg('features.off', feature='AI investigator')
    assert set(after) == set(before) | {'investigator'}


@pytest.mark.parametrize('feature_id', list(LIVE))
def test_every_feature_switch_is_live_off_without_restart(store: Store, feature_id: str):
    # given
    scene = LIVE[feature_id](store)
    with scene.client as c:
        scene.ready()

        # when
        turned_off = switch(c, feature_id, False)
        while_off = scene.probe(1)
        turned_on = switch(c, feature_id, True)
        while_on = scene.probe(2)

    # then
    assert (turned_off.status_code, turned_off.json()['on'], turned_on.status_code, turned_on.json()['on']) == (200, False, 200, True)
    assert while_off == scene.off
    assert while_on == scene.on


def test_another_pod_sees_a_switch_within_one_refresh(store: Store):
    # given
    clock = Clock()
    here = Features(Live(settings(), TunableStore(store.db), every=15, clock=clock), lambda: 'community')
    there = Features(Live(settings(), TunableStore(store.db), every=15, clock=clock), lambda: 'community')
    before = there.on('investigator')

    # when
    here.set('investigator', False, 'alice@example.com')
    clock.now += 10
    soon = there.on('investigator')
    clock.now += 6
    later = there.on('investigator')

    # then
    assert (before, soon, later) == (True, True, False)


@pytest.mark.parametrize('case', list(BOOTED))
def test_features_booted_off_turn_on_without_restart(store: Store, case: str):
    # given
    scene = BOOTED[case](store)
    with scene.client as c:
        scene.ready()
        before = scene.probe(1)

        # when
        turned = scene.turn_on() if scene.turn_on else switch(c, case, True)
        after = scene.probe(2)

    # then
    assert turned.status_code == 200
    assert before == scene.off
    assert after == scene.on


def test_a_run_already_started_finishes_when_the_switch_goes_off(store: Store):
    # given
    seed(store, anomaly(RUN_KEY, firstSeen=iso(NOW - 600)))
    inv = card_investigator(store, note_and_recap())
    inv.on = lambda: not inv.client.requests

    # when
    record = inv.run_once(NOW)

    # then
    assert record['outcome'] == 'done' and len(inv.client.requests) == 2
    assert inv.why_halted(NOW) == 'off' and inv.run_once(NOW) is None


def test_off_keeps_queued_work(store: Store):
    # given
    fresh_card(store)
    fake = FakeAnthropic([reply(text('Checked checkout-service: one client.'), stop='end_turn')])
    with TestClient(create_app(dataclasses.replace(settings(), anthropic_api_key='sk-test', investigate_mode='live'), store, anthropic_client=fake)) as c:
        queued = c.post('/api/actions', json={'type': 'dive', 'key': KEY, 'target': KEY, 'label': 'Investigate'}, headers=AUTH).json()
        switch(c, 'investigator', False)

        # when
        off_tick = c.app.state.investigator.run_once(time.time())
        while_off = status_of(store, queued['id'])
        refused = c.post('/api/actions', json={'type': 'dive', 'key': KEY, 'target': KEY, 'label': 'Investigate'}, headers=AUTH)
        switch(c, 'investigator', True)
        on_tick = c.app.state.investigator.run_once(time.time())

    # then
    assert queued['status'] == 'requested'
    assert off_tick is None and while_off == 'requested'
    assert (refused.status_code, refused.json()['detail']) == (409, off_text('investigator'))
    assert on_tick['actionId'] == queued['id'] and status_of(store, queued['id']) == 'done'


def test_needs_are_computed_on_the_server(client: TestClient):
    # when
    listed = items(client)

    # then
    assert listed['slack-autosend']['needs'] == [msg('features.needs.slack_token')]
    assert listed['mcp']['needs'] == [msg('features.needs.sso')]
    assert listed['postdeploy']['needs'] == [msg('features.needs.prometheus')]
    assert listed['investigator']['needs'] == [msg('features.needs.anthropic_key'), msg('features.needs.mode')]
    assert listed['push']['needs'] == [msg('features.needs.bot_token'), msg('features.needs.push_users')]
    assert listed['public-read']['needs'] == [] and listed['hide-attribution']['needs'] == []
    assert {i['source'] for i in listed.values()} == {'default', 'config'}


def test_the_postdeploy_need_and_serving_ask_for_an_instance_that_runs_the_prom_check(store: Store):
    # given
    add_instance(store, prometheus.PLUGIN, 'main', url=PROD, primary=True)
    add_instance(store, prometheus.PLUGIN, 'west', url=PROD)
    c = TestClient(create_app(settings(), store, transport=FakeHttp()))

    def seen():
        return items(c)['postdeploy']['needs'], c.get('/api/server', headers=AUTH).json()['postdeploy']

    # when
    with c:
        main_off = c.patch('/api/admin/plugins/prometheus/instances/main', json={'offChecks': ['prom']}, headers=AUTH).status_code
        one_on = seen()
        west_off = c.patch('/api/admin/plugins/prometheus/instances/west', json={'offChecks': ['prom']}, headers=AUTH).status_code
        none_on = seen()

    # then
    assert (main_off, west_off) == (200, 200)
    assert one_on == ([], True), 'one instance still runs the prom check'
    assert none_on == ([msg('features.needs.prometheus')], False), 'every instance is on but none runs the prom check'


def test_the_page_holds_a_toggle_answer_exactly_as_long_as_the_settings_take_to_reach_every_pod(client: TestClient):
    # given
    held = re.findall(r'\bFEAT_HOLD_MS = (\d+)\b', (ICONS.parent / 'admin.js').read_text(encoding='utf-8'))

    # when
    refresh = client.get('/api/admin/settings', headers=AUTH).json()['refreshSeconds']

    # then
    assert len(held) == 1 and int(held[0]) == refresh * 1000


def test_after_restart_shows_only_when_a_restart_would_start_it(store: Store):
    # given
    ready = dataclasses.replace(sso_settings(), anthropic_api_key='sk-test', investigate_mode='live')
    rules = Features(Live(settings(), TunableStore(store.db)), lambda: 'community', needs=lambda feature_id: [], started=lambda what: False)
    slack, _, _ = fake_slack()
    socketless = dataclasses.replace(settings(), anthropic_api_key='sk-test', slack_listen='shadow')

    # when
    unstarted = {i['id']: i['restart'] for i in rules.view()}
    with TestClient(create_app(dataclasses.replace(settings(), chat_ui=True), store)) as c:
        without_sso = items(c)['chat']
    with app_client(store, s=dataclasses.replace(ready, chat_ui=True)) as c:
        mounted = items(c)['chat']
    socketless_app = create_app(socketless, store, slack=slack, anthropic_client=FakeAnthropic([]), operators_lookup=lambda email: False, slack_socket=Factory())
    slack_instance(socketless_app, app_token='xapp-test')
    with TestClient(socketless_app) as c:
        before_mode = items(c)['slack-listener']
        c.put('/api/admin/settings', json={'key': 'investigate.mode', 'value': 'shadow'}, headers=AUTH)
        after_mode = items(c)['slack-listener']

    # then
    assert unstarted['chat'] == unstarted['slack-listener'] == msg('features.restart')
    assert [k for k, v in unstarted.items() if v] == ['chat', 'slack-listener']
    assert msg('features.needs.chat_ui') in without_sso['needs'] and without_sso['restart'] is None
    assert (mounted['needs'], mounted['restart']) == ([], None)
    assert (before_mode['needs'], before_mode['restart']) == ([msg('features.needs.investigator')], None)
    assert (after_mode['needs'], after_mode['restart']) == ([], msg('features.restart'))


def test_switching_on_a_locked_feature_is_409_on_every_write_path(store: Store):
    # given
    refused = msg('features.needs_edition', feature=HIDE_LABEL, edition='Enterprise')

    # when
    with TestClient(create_app(settings(), store)) as c:
        by_route = c.put('/api/admin/features/hide-attribution', json={'on': True}, headers=AUTH)
        by_setting = c.put('/api/admin/settings', json={'key': 'core.hide-attribution', 'value': ' true '}, headers=AUTH)
        stored = store.db['tunables'].find_one({'_id': 'core.hide-attribution'})
        off = c.put('/api/admin/features/hide-attribution', json={'on': False}, headers=AUTH)
        locked = items(c)['hide-attribution']
        unknown = c.put('/api/admin/features/teleport', json={'on': True}, headers=AUTH)
    with TestClient(create_app(settings(), store, registry=registry_with(entitled_plugin))) as c:
        entitled = c.put('/api/admin/features/hide-attribution', json={'on': True}, headers=AUTH)
        listed = c.get('/api/admin/features', headers=AUTH).json()

    # then
    assert (by_route.status_code, by_route.json()['detail']) == (409, refused)
    assert (by_setting.status_code, by_setting.json()['detail']) == (409, refused)
    assert stored is None
    assert off.status_code == 200 and off.json()['on'] is False
    assert (locked['label'], locked['icon'], locked['locked'], locked['edition'], locked['on'], locked['stored']) == (HIDE_LABEL, 'badge', True, 'enterprise', False, False)
    assert unknown.status_code == 404
    assert entitled.status_code == 200 and (entitled.json()['on'], entitled.json()['locked']) == (True, False)
    assert listed['edition'] == 'enterprise'


def test_feature_switches_keep_history_and_activity(client: TestClient, store: Store):
    # when
    turned = switch(client, 'investigator', False)
    history = client.get('/api/admin/history', headers=AUTH).json()
    lines = [a['text'] for a in store.list_activity(None, 10) if a['kind'] == 'admin']
    reverted = client.delete('/api/admin/settings', params={'key': history[0]['key']}, headers=AUTH)
    after = items(client)['investigator']

    # then
    assert turned.status_code == 200 and (turned.json()['on'], turned.json()['stored'], turned.json()['source']) == (False, False, 'admin')
    assert [(h['key'], h['old'], h['new']) for h in history] == [('core.investigator', None, 'false')]
    assert lines == [msg('features.switched', feature='AI investigator', state='off')]
    assert reverted.status_code == 200 and (after['on'], after['source']) == (True, 'default')


def crit_card(c: TestClient, n: int) -> None:
    card = found_card('prom', 'err', 'shop', f'orders-service-{n}', '', 'crit', 50, 0, 0, '', 'ERROR logs', 'errors')
    c.app.state.server_apply([PassResult('prom', True, 'ok', [card], {}, ('prom',))], time.time(), None)


def test_critical_cards_are_not_posted_at_once_while_auto_send_is_off(store: Store):
    # given
    store.save_state({'anomalies': [], 'initialized': True}, None)
    transport = FakeSlack()
    with TestClient(create_app(settings(), store, slack=Slack('xoxb-test', '#ops', transport, FakeSlackApi()))) as c:
        switch(c, 'slack-autosend', False)

        # when
        crit_card(c, 1)
        while_off = len(transport.calls)
        switch(c, 'slack-autosend', True)
        crit_card(c, 2)
        turned_on = len(transport.calls)

    # then
    assert (while_off, turned_on) == (0, 1)


def test_slack_dms_and_asks_while_chat_or_the_investigator_is_off_get_one_reply_and_no_ask(store: Store):
    # given
    chat_off, f = intake(store, gate_replies=[verdict()])
    chat_off.chat = lambda: False
    investigator_off, g = intake(store, gate_replies=[verdict()], investigator=FakeInvestigator(halted='off'))

    # when
    dm = chat_off.handle(dm_event())
    asked = investigator_off.handle(top_event())

    # then
    assert dm == 'chat-off' and [(c['channel'], c['text']) for c in f.post.calls] == [(DM, off_text('chat') + '\n' + msg('slackevents.dm.site', url='https://skopax.example.com/chat/'))]
    assert asked == 'investigator-off' and [c['text'] for c in g.post.calls] == [off_text('investigator')]
    assert store.list_actions(None, 5) == [] and f.gate.gate_requests == [] and g.gate.gate_requests == []


def test_a_slack_dm_or_tagged_ask_while_the_investigator_is_not_ready_gets_one_short_reply_an_hour(store: Store):
    # given
    not_ready = FakeInvestigator(enabled=False)
    box, f = intake(store, gate_replies=[verdict()], investigator=not_ready)
    quiet, q = intake(store, listen='off', investigator=not_ready)
    later, g = intake(store, investigator=not_ready)
    later.clock = lambda: SLACK_NOW + 3601
    card_with_post(store)

    # when
    dm = box.handle(dm_event())
    tagged = box.handle(top_event('<@UBOT> why is checkout-service slow?', ts='1792224000.000700'))
    untagged = box.handle(reply_event('why did it go up?', ts='1792224000.000800'))
    off = quiet.handle(top_event(ts='1792224000.000900'))
    next_hour = later.handle(top_event(ts='1792224000.001000'))

    # then
    assert (dm, tagged, off, next_hour) == ('not-ready', 'not-ready-held', 'off', 'not-ready')
    assert untagged == 'off'
    assert [(c['channel'], c['text']) for c in f.post.calls] == [(DM, msg('chat.not_ready'))]
    assert q.post.calls == [] and [c['text'] for c in g.post.calls] == [msg('chat.not_ready')]
    assert store.list_actions(None, 5) == [] and f.gate.gate_requests == []


def test_every_reader_of_the_halted_state_knows_off(store: Store):
    # given
    seed(store, anomaly(RUN_KEY, firstSeen=iso(NOW - 600)))
    inv = card_investigator(store, [])
    inv.on = lambda: False
    store.add_action({'type': 'dive', 'key': RUN_KEY, 'target': RUN_KEY, 'label': 'Investigate', 'requestedBy': 'alice@example.com'})

    # when
    board = inv.board(NOW)

    # then
    assert board['halted'] == 'off' and [w['why'] for w in board['waiting']] == [off_text('investigator')]
    assert mcpserver.HALTED['off'] == off_text('investigator')
    assert pending_line({'status': 'requested'}, 'off') == off_text('investigator') and pending_line({'status': 'requested'}, 'stopped') == 'Waiting: the investigator is stopped.'
    assert inv.available() is False and inv.run_once(NOW) is None


def test_the_chat_ui_gets_the_off_text_as_it_is(client: TestClient, store: Store):
    # given
    store.save(state(anomaly(KEY)), None)
    bridge = client.app.state.chat_bridge
    switch(client, 'chat', False)
    switch(client, 'investigator', False)

    # when
    with pytest.raises(chatui.Refused) as posted:
        bridge.post('alice@example.com', 'What needs attention now?', 'aaaabbbb-cccc-4ddd-8eee-ffff00001111', None)
    with pytest.raises(chatui.Refused) as applied:
        bridge.apply('alice@example.com', True, {'action': 'investigate', 'key': KEY})

    # then
    assert (str(posted.value), posted.value.code) == (off_text('chat'), 'feature-off')
    assert (str(applied.value), applied.value.code) == (off_text('investigator'), 'feature-off')
    assert (posted.value.fields['feature'], applied.value.fields['feature']) == ('chat', 'investigator')
    assert (refusal(posted.value, 'en'), refusal(applied.value, 'en')) == (off_text('chat'), off_text('investigator')), 'the chat table keeps the English off texts'
    assert store.list_actions(None, 5) == []


def test_the_slack_listener_ignores_events_and_logs_once_per_off_spell(store: Store, caplog):
    # given
    box, f = intake(store, gate_replies=[verdict(on_topic=False)])
    switched = {'on': False}
    box.on = lambda: switched['on']

    # when
    with caplog.at_level(logging.INFO, logger='skopax.slackevents'):
        first_spell = [box.handle(top_event(ts=f'1792224000.00{n}500')) for n in range(3)]
        switched['on'] = True
        heard = box.handle(top_event(ts='1792224000.004500'))
        switched['on'] = False
        second_spell = box.handle(top_event(ts='1792224000.005500'))
    logged = [r.getMessage() for r in caplog.records if off_text('slack-listener') in r.getMessage()]

    # then
    assert first_spell == [None, None, None] and second_spell is None and heard == 'off-topic'
    assert len(logged) == 2
    assert len(store.list_intake(10)) == 1 and len(f.gate.gate_requests) == 1


def hide_state(c: TestClient) -> tuple:
    item = items(c)['hide-attribution']
    return (item['stored'], item['on'], item['locked']), c.get('/api/me', headers=AUTH).json()['features']['hide-attribution'], c.app.state.features.on('hide-attribution')


def test_a_configured_or_stored_hide_switch_stays_off_while_the_edition_is_community(store: Store):
    # given
    entitlement = {'edition': 'enterprise'}
    switchable = minimal('switch-demo', 'Switch demo', hooks=Hooks(entitlement=lambda: entitlement['edition']))

    # when
    with TestClient(create_app(dataclasses.replace(settings(), hide_attribution=True), store)) as c:
        from_config = hide_state(c)
    store.db['tunables'].insert_one({'_id': 'core.hide-attribution', 'value': 'true', 'by': 'alice@example.com', 'at': '2026-11-15T00:00:00Z'})
    with TestClient(create_app(settings(), store)) as c:
        from_store = hide_state(c)
    with TestClient(create_app(settings(), store, registry=registry_with(switchable))) as c:
        entitled = hide_state(c)
        entitlement['edition'] = 'community'
        c.app.state.registry.refresh()
        dropped = hide_state(c)

    # then
    assert from_config == from_store == ((True, False, True), False, False)
    assert entitled == ((True, True, False), True, True)
    assert dropped == ((True, False, True), False, False)


def test_a_community_feature_never_asks_for_the_edition(store: Store):
    # given
    asked = []
    features = Features(Live(settings(), TunableStore(store.db)), lambda: asked.append('edition') or 'community')

    # when
    community = {f.id: (features.on(f.id), features.locked(f.id)) for f in FEATURES if f.edition == 'community'}
    before = list(asked)
    locked = features.locked('hide-attribution')

    # then
    assert len(community) == 14 and before == []
    assert locked is True and asked == ['edition']


def test_small_talk_while_the_investigator_halts_off_is_refused_with_the_off_text(store: Store):
    # given
    fake = FakeAnthropic([], [verdict(on_topic=False, small_talk=True, reason='says hello')])
    with TestClient(create_app(dataclasses.replace(settings(), anthropic_api_key='sk-test'), store, anthropic_client=fake)) as c:
        c.app.state.investigator.on = lambda: False

        # when
        res = c.post('/api/actions', json={'type': 'ask', 'key': 'ask', 'target': 'ask', 'text': 'Hello there!'}, headers=AUTH)

    # then
    assert (res.status_code, res.json()['detail']) == (409, off_text('investigator'))
    assert len(fake.gate_requests) == 1 and store.list_actions(None, 5) == []


def test_a_slack_dm_through_the_app_while_chat_is_off_gets_one_reply_and_no_ask(store: Store):
    # given
    slack, post, _ = fake_slack(emails={'U1': 'adam.baker@example.com'})
    fake = FakeAnthropic([], [verdict()])
    s = dataclasses.replace(settings(), anthropic_api_key='sk-test', investigate_mode='live', slack_listen='live')
    with TestClient(create_app(s, store, slack=slack, anthropic_client=fake, operators_lookup=lambda email: True)) as c:
        switch(c, 'chat', False)

        # when
        decision = c.app.state.intake.handle(dm_event())

    # then
    assert decision == 'chat-off'
    assert [(m['channel'], m['text']) for m in post.calls] == [(DM, off_text('chat') + '\n' + msg('slackevents.dm.site', url='https://skopax.example.com/chat/'))]
    assert store.list_actions(None, 5) == [] and fake.gate_requests == []


def test_phone_push_needs_only_the_bot_token_and_channel_posts_need_the_channel_too(store: Store):
    # given
    s = dataclasses.replace(settings(), slack_channel='', slack_push_user=USER)
    app = create_app(s, store)
    slack_instance(app)

    # when
    with TestClient(app) as c:
        listed = items(c)

    # then
    assert listed['push']['needs'] == [] and listed['push']['on'] is True
    assert listed['slack-autosend']['needs'] == [msg('features.needs.slack_token')]
    assert msg('features.needs.slack_token') in listed['slack-listener']['needs']


def test_the_self_review_run_is_409_while_switched_off_and_503_while_not_set_up(store: Store):
    # given
    s = dataclasses.replace(settings(), anthropic_api_key='sk-test', self_review_enabled=True, self_project='')

    # when
    with TestClient(create_app(s, store)) as c:
        not_set_up = c.post('/api/self-review/run', headers=AUTH)
        switch(c, 'self-review', False)
        switched_off = c.post('/api/self-review/run', headers=AUTH)

    # then
    assert (switched_off.status_code, switched_off.json()['detail']) == (409, off_text('self-review'))
    assert not_set_up.status_code == 503 and 'switched off' not in not_set_up.json()['detail']


def test_a_locked_feature_carries_the_reason_its_switch_shows(store: Store):
    # when
    with TestClient(create_app(settings(), store)) as c:
        community = items(c)
    with TestClient(create_app(settings(), store, registry=registry_with(entitled_plugin))) as c:
        enterprise = items(c)

    # then
    assert community['hide-attribution']['reason'] == msg('features.needs_edition', feature=HIDE_LABEL, edition='Enterprise')
    assert [i for i, item in community.items() if item['reason'] is not None] == ['hide-attribution']
    assert [i for i, item in enterprise.items() if item['reason'] is not None] == []
