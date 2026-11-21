import dataclasses
import json
import logging
import urllib.request

import pytest
from fastapi.testclient import TestClient
from pymongo.errors import PyMongoError

from skopax import registry as registry_module
from skopax.app import create_app
from skopax.autosend import armed_for
from skopax.config import load_settings
from skopax.kube import Kube
from skopax.messages import msg
from skopax.plugins import slack as slack_plugin
from skopax.registry import BUILTIN, Registry, describe
from skopax.remote import HttpError, Reply, Unreachable
from skopax.settingsources import boot, core_setting_ids
from skopax.slack import CREATE_TASK, Slack, SlackError
from skopax.slacklink import SlackLink
from skopax.store import Store
from tests.conftest import AUTH, TOKEN, anomaly, settings, state
from tests.fake_anthropic import FakeAnthropic
from tests.fake_http import FakeHttp
from tests.fake_slack import FakeSlackApi, SlackReply, SlackWeb, slack_instance
from tests.test_features import items
from tests.test_slack import FakeSlack
from tests.test_slack_listener import Factory, wait_for

AUTH_URL = 'https://slack.com/api/auth.test'
BOT = 'xoxb-1111-aaaa'
NEW_BOT = 'xoxb-2222-bbbb'
APP = 'xapp-1-cccc'
KEY = 'log|checkout-service|ERROR|boom'
OTHER = 'log|payments|ERROR|slow'
DRAFT = 'checkout-service showed 116 ERROR lines, 13:05 UTC.\nhttps://skopax.example.com/#a=x'
JOBS = ('slack-posts', 'slack-fallback', 'slack-reminders', 'slack-clear-notes')
NOW = 1792224000.0


def add_slack(c: TestClient, values: dict, enabled: bool = True):
    return c.post('/api/admin/plugins/slack/instances', json={'name': 'main', 'values': values, 'enabled': enabled}, headers=AUTH)


def ready_for_socket(**over):
    return dataclasses.replace(settings(), anthropic_api_key='sk-test', investigate_mode='shadow', slack_listen='shadow', **over)


@pytest.fixture
def web(monkeypatch) -> SlackWeb:
    fake = SlackWeb()
    monkeypatch.setattr(urllib.request, 'urlopen', fake)
    return fake


def test_slack_identity_and_manifest():
    # given
    plugin = slack_plugin.PLUGIN
    loaded = Registry.load({}, entry_points=False, core_settings=core_setting_ids())

    # when
    manifest = describe(plugin, icon=True)

    # then
    assert 'skopax.plugins.slack' in BUILTIN and loaded.get('slack').state == 'ok' and loaded.get('slack').plugin is plugin
    assert json.loads(json.dumps(manifest, allow_nan=False)) == manifest
    assert (manifest['id'], manifest['name'], manifest['version'], manifest['category'], manifest['author'], manifest['licence'], manifest['core'], manifest['edition']) == (
        'slack', 'Slack', '0.1.0', 'chat', 'Skopax', 'Apache-2.0', '>=0.1,<0.5', 'community')
    assert manifest['capabilities'] == {'hosts': ['slack.com'], 'secrets': ['bot-token', 'app-token'], 'agentReads': False}
    assert (manifest['uses'], manifest['maxInstances'], manifest['checks'], manifest['tools'], manifest['settings'], manifest['panels']) == ([], 1, [], [], [], [])
    assert [(f['key'], f['kind'], f['required']) for f in manifest['fields']] == [('bot-token', 'secret', True), ('app-token', 'secret', False)]
    assert plugin.client is None and plugin.test is not None and plugin.env_instances is not None
    assert loaded.icon('slack').startswith('<svg')
    assert 'slack' not in registry_module.CORE_PREFIXES and 'slack-posts' in registry_module.CORE_RAN


def test_slack_connection_test_reports_ok_and_failures(store: Store):
    # given
    answers = {'ok': {'ok': True, 'team': 'Example', 'user': 'skopax-bot', 'url': 'https://example.slack.com/'}, 'refused': {'ok': False, 'error': 'invalid_auth'},
               'rejected': HttpError(401, AUTH_URL), 'down': Unreachable('slack.com unreachable (timed out)')}
    fakes = {name: FakeHttp({('GET', AUTH_URL): answer, ('POST', 'https://slack.com/api/apps.connections.open'): {'ok': True}}) for name, answer in answers.items()}

    def tried(name: str, values: dict) -> tuple[int, dict]:
        with TestClient(create_app(settings(), store, transport=fakes[name])) as c:
            res = c.post('/api/admin/plugins/slack/test', json={'values': values}, headers=AUTH)
        return res.status_code, res.json()

    # when
    ok = tried('ok', {'bot-token': BOT, 'app-token': APP})
    refused = tried('refused', {'bot-token': BOT})
    rejected = tried('rejected', {'bot-token': BOT})
    down = tried('down', {'bot-token': BOT})
    bad_bot = tried('ok', {'bot-token': 'not-a-bot-token'})
    bad_app = tried('ok', {'bot-token': BOT, 'app-token': BOT})

    # then
    assert ok[0] == 200 and (ok[1]['ok'], ok[1]['summary']) == (True, msg('integrations.slack_ok', team='Example', user='skopax-bot'))
    assert refused[1] | {'ms': 0} == {'ok': False, 'summary': msg('integrations.slack_refused', error='invalid_auth'), 'ms': 0}
    assert (rejected[1]['ok'], rejected[1]['summary']) == (False, msg('connection.rejected', status=401))
    assert (down[1]['ok'], down[1]['summary']) == (False, msg('connection.unreachable'))
    assert (bad_bot[1]['ok'], bad_bot[1]['summary']) == (False, msg('integrations.slack_bot_shape'))
    assert (bad_app[1]['ok'], bad_app[1]['summary']) == (False, msg('integrations.slack_app_shape'))
    assert {code for code, _ in (ok, refused, rejected, down, bad_bot, bad_app)} == {200}
    assert [c[2]['Authorization'] for c in fakes['ok'].calls] == [f'Bearer {BOT}', f'Bearer {APP}'], 'a token of the wrong shape is refused before Slack is asked'
    assert all(BOT not in json.dumps(body) and APP not in json.dumps(body) for _, body in (ok, refused, rejected, down, bad_bot, bad_app))


def test_slack_defaults_are_neutral():
    # given
    plugin = slack_plugin.PLUGIN

    # then
    assert {f.key: f.default for f in plugin.fields} == {'bot-token': '', 'app-token': ''}
    assert {s.key: s.default for s in plugin.settings} == {}
    assert settings().slack_channel == '#skopax'


def test_every_slack_key_the_config_reads_stays_a_core_setting():
    # given
    class Asked(dict):
        def __init__(self):
            super().__init__()
            self.keys_asked: list[str] = []

        def __contains__(self, key) -> bool:
            self.keys_asked.append(key)
            return False

    asked = Asked()

    # when
    load_settings(lambda: asked, env={})
    read = {k for k in asked.keys_asked if k.startswith('slack.')}

    # then
    assert {'slack.channel', 'slack.push-user', 'slack.operator-cache-seconds', 'slack.listen'} <= read
    assert not read & {'slack.bot-token', 'slack.app-token'}
    assert sorted(read - core_setting_ids()) == []


def test_an_env_app_token_opens_the_socket_at_boot(store: Store, web: SlackWeb, tmp_path):
    # given
    factory = Factory()
    b = boot({'SLACK_BOT_TOKEN': BOT, 'SLACK_APP_TOKEN': APP})
    s = dataclasses.replace(b.settings, token=TOKEN, anthropic_api_key='sk-test', investigate_mode='shadow', slack_listen='shadow')

    # when
    with TestClient(create_app(s, store, registry=b.registry, sources=b.source_set(), kube=Kube(root=tmp_path), anthropic_client=FakeAnthropic([]),
                               operators_lookup=lambda email: False, slack_socket=factory)) as c:
        wait_for(lambda: bool(factory.made) and factory.made[0].connected.is_set())
        info = c.get('/api/server', headers=AUTH).json()['slack']

    # then
    assert factory.tokens == [APP] and info['socket'] is True and info['link']['connected'] is True


def test_env_defines_the_main_instance(store: Store, tmp_path):
    # given
    env = {'SLACK_BOT_TOKEN': BOT, 'SLACK_APP_TOKEN': APP, 'SLACK_CHANNEL': 'C0123ABCD'}

    # when
    b = boot(env)
    bare = boot({'SLACK_BOT_TOKEN': f' {BOT} '})
    none = boot({'SLACK_APP_TOKEN': APP, 'SLACK_CHANNEL': 'C0123ABCD'})
    with TestClient(create_app(dataclasses.replace(b.settings, token=TOKEN), store, registry=b.registry, sources=b.source_set(), kube=Kube(root=tmp_path))) as c:
        main = c.app.state.instances.get('slack', 'main')
        link = c.app.state.slack_link
        tokens = (link.token(), link.app_token())
        me = c.get('/api/me', headers=AUTH).json()

    # then
    assert b.sources[0].instances == ({'plugin': 'slack', 'name': 'main', 'primary': 'true', 'bot-token': BOT, 'app-token': APP},)
    assert bare.sources[0].instances == ({'plugin': 'slack', 'name': 'main', 'primary': 'true', 'bot-token': BOT, 'app-token': ''},)
    assert none.sources[0].instances == ()
    assert (main.primary, main.enabled, main.problem, main.source, sorted(main.owned)) == (True, True, None, 'environment', ['app-token', 'bot-token'])
    assert tokens == (BOT, APP)
    assert (me['slack'], me['slackChannel']) == (True, 'C0123ABCD')
    assert not hasattr(b.settings, 'slack_bot_token') and not hasattr(b.settings, 'slack_app_token') and b.settings.slack_channel == 'C0123ABCD'


def test_an_admin_set_token_is_used_without_restart(store: Store, web: SlackWeb, tmp_path):
    # given
    store.save(state(anomaly(KEY), anomaly(OTHER, svc='payments')), None)

    # when
    with TestClient(create_app(settings(), store, kube=Kube(root=tmp_path))) as c:
        made = add_slack(c, {'bot-token': BOT})
        first = c.post('/api/slack-posts', json={'key': KEY}, headers=AUTH)
        changed = c.patch('/api/admin/plugins/slack/instances/main', json={'values': {'bot-token': NEW_BOT}}, headers=AUTH)
        c.app.state.runtime.refresh()
        second = c.post('/api/slack-posts', json={'key': OTHER}, headers=AUTH)

    # then
    assert (made.status_code, first.status_code, changed.status_code, second.status_code) == (201, 201, 200, 201)
    assert [(auth, body['channel']) for auth, body in web.posts()] == [(f'Bearer {BOT}', '#skopax'), (f'Bearer {NEW_BOT}', '#skopax')]


def test_a_rotated_token_drops_what_slack_said_for_the_old_one():
    # given
    current = ['xoxb-old']
    asked = []

    def api(token: str, method: str, payload: dict) -> dict:
        asked.append((token, method))
        return {'ok': True, 'user_id': 'U' + token[5:].upper(), 'url': f'https://{token[5:]}.slack.com/', '_scopes': 'chat:write,reactions:write'}

    slack = Slack(lambda: current[0], '#ops', FakeSlack(), api)

    # when
    before = (slack.bot_user(), slack.workspace(), slack.channel_name('C1'))
    current[0] = 'xoxb-new'
    after = (slack.bot_user(), slack.workspace(), slack.token, slack.enabled)
    current[0] = ''
    off = (slack.token, slack.enabled)

    # then
    assert before == ('UOLD', 'https://old.slack.com/', None) and after == ('UNEW', 'https://new.slack.com/', 'xoxb-new', True)
    assert asked == [('xoxb-old', 'auth.test'), ('xoxb-old', 'conversations.info'), ('xoxb-new', 'auth.test')]
    assert off == ('', False)


def test_slack_added_in_admin_starts_its_jobs_without_restart(store: Store, web: SlackWeb, tmp_path):
    # given
    with TestClient(create_app(settings(), store, kube=Kube(root=tmp_path))) as c:
        before = c.app.state.scheduler.run_due()

        # when
        add_slack(c, {'bot-token': BOT})
        after = c.app.state.scheduler.run_due()

    # then
    assert [j for j in JOBS if j in before] == []
    assert [j for j in JOBS if j in after] == list(JOBS)


def test_env_instance_jobs_run_after_the_first_refresh(store: Store, web: SlackWeb, tmp_path):
    # given
    b = boot({'SLACK_BOT_TOKEN': BOT})
    app = create_app(b.settings, store, registry=b.registry, sources=b.source_set(), kube=Kube(root=tmp_path))

    # when
    before = app.state.scheduler.run_due()
    app.state.runtime.refresh()
    after = app.state.scheduler.run_due()

    # then
    assert [j for j in JOBS if j in before] == []
    assert [j for j in JOBS if j in after] == list(JOBS)


def test_a_channel_change_is_live(store: Store, web: SlackWeb, tmp_path):
    # given
    store.save(state(anomaly(KEY), anomaly(OTHER, svc='payments')), None)

    # when
    with TestClient(create_app(settings(), store, kube=Kube(root=tmp_path))) as c:
        add_slack(c, {'bot-token': BOT})
        before = c.get('/api/me', headers=AUTH).json()['slackChannel']
        c.post('/api/slack-posts', json={'key': KEY}, headers=AUTH)
        changed = c.put('/api/admin/settings', json={'key': 'slack.channel', 'value': 'C0NEWCHAN1'}, headers=AUTH)
        after = c.get('/api/me', headers=AUTH).json()['slackChannel']
        c.post('/api/slack-posts', json={'key': OTHER}, headers=AUTH)

    # then
    assert changed.status_code == 200 and (before, after) == ('#skopax', 'C0NEWCHAN1')
    assert [body['channel'] for _, body in web.posts()] == ['#skopax', 'C0NEWCHAN1']


def test_drafts_wait_while_slack_is_off(store: Store, web: SlackWeb, tmp_path):
    # given
    store.save(state(anomaly(KEY, sev='crit')), None)
    with TestClient(create_app(settings(), store, kube=Kube(root=tmp_path))) as c:
        add_slack(c, {'bot-token': BOT})
        c.put('/api/slack-drafts', json={'key': KEY, 'text': DRAFT}, headers=AUTH)
        c.put('/api/admin/plugins/slack', json={'enabled': False}, headers=AUTH)

        # when
        while_off = c.app.state.auto_send.run_once()
        held = store.anomaly(KEY)
        posted_while_off = list(web.posts())
        c.put('/api/admin/plugins/slack', json={'enabled': True}, headers=AUTH)
        while_on = c.app.state.auto_send.run_once()

    # then
    assert (while_off, posted_while_off) == (0, [])
    assert armed_for(held) is not None and 'claimedAt' not in held['autoSend'] and 'failedAt' not in held['autoSend'] and not held.get('slackPost')
    assert while_on == 1 and [body['channel'] for _, body in web.posts()] == ['#skopax']
    assert store.anomaly(KEY)['slackPost']['ts']


def test_socket_mode_needs_a_restart_for_a_token_set_later(store: Store, web: SlackWeb, tmp_path):
    # given
    deaf, heard = Factory(), Factory()

    def app_with(factory: Factory):
        return create_app(ready_for_socket(), store, kube=Kube(root=tmp_path), anthropic_client=FakeAnthropic([]), operators_lookup=lambda email: False, slack_socket=factory)

    # when
    with TestClient(app_with(deaf)) as c:
        add_slack(c, {'bot-token': BOT})
        before = items(c)['slack-listener']
        c.patch('/api/admin/plugins/slack/instances/main', json={'values': {'app-token': APP}}, headers=AUTH)
        promised = items(c)['slack-listener']
        deaf_info = c.get('/api/server').json()['slack']
        deaf_listener = c.app.state.listener
    with TestClient(app_with(heard)) as c:
        wait_for(lambda: bool(heard.made) and heard.made[0].connected.is_set())
        heard_info = c.get('/api/server').json()['slack']
        card = items(c)['slack-listener']

    # then
    assert (before['needs'], before['restart']) == ([msg('features.needs.slack_app_token')], None)
    assert (promised['needs'], promised['restart']) == ([], msg('features.restart'))
    assert deaf.tokens == [] and deaf_info == {'listen': 'shadow', 'socket': False} and deaf_listener is None
    assert heard.tokens == [APP] and heard_info['socket'] is True and (card['needs'], card['restart']) == ([], None)


def test_slack_needs_point_at_admin_and_follow_the_instance(store: Store, web: SlackWeb, tmp_path):
    # given
    s = dataclasses.replace(ready_for_socket(), slack_push_user='U0123ABCD')

    # when
    with TestClient(create_app(s, store, kube=Kube(root=tmp_path))) as c:
        bare = items(c)
        add_slack(c, {'bot-token': BOT, 'app-token': APP})
        set_up = items(c)
        c.put('/api/admin/plugins/slack', json={'enabled': False}, headers=AUTH)
        off = items(c)

    # then
    assert bare['push']['needs'] == [msg('features.needs.bot_token')] and msg('features.needs.slack_app_token') in bare['slack-listener']['needs']
    assert bare['slack-autosend']['needs'] == [msg('features.needs.slack_token')]
    assert set_up['push']['needs'] == [] and set_up['slack-autosend']['needs'] == [] and msg('features.needs.slack_app_token') not in set_up['slack-listener']['needs']
    assert off['push']['needs'] == [msg('features.needs.bot_token')] and off['slack-autosend']['needs'] == [msg('features.needs.slack_token')]
    for key in ('features.needs.slack_token', 'features.needs.bot_token', 'features.needs.slack_app_token'):
        assert 'Connect \u203a Integrations \u203a Slack' in msg(key) and 'SLACK_' not in msg(key)
    assert 'Connect \u203a Integrations \u203a Slack' in msg('cards.slack_posts_failed', count=1, plural='', since='12:00', errors='x', names='y')


def test_slack_link_reads_the_serving_primary_instance_only(store: Store, tmp_path):
    # given
    app = create_app(settings(), store, kube=Kube(root=tmp_path))
    link = SlackLink(app.state.runtime)
    empty = (link.token(), link.app_token())
    app.state.instances.create('slack', 'main', {'bot-token': BOT, 'app-token': APP}, True, 'test')

    # when
    served = (link.token(), link.app_token())
    app.state.instances.set_enabled('slack', 'main', False, 'test')
    instance_off = (link.token(), link.app_token())
    app.state.instances.set_enabled('slack', 'main', True, 'test')
    app.state.plugin_state.set('slack', False, 'test')
    plugin_off = (link.token(), link.app_token())

    # then
    assert empty == instance_off == plugin_off == ('', '')
    assert served == (BOT, APP)


def test_slack_tokens_never_leave_the_server(store: Store, web: SlackWeb, tmp_path, caplog):
    # given
    good = FakeHttp({('GET', AUTH_URL): {'ok': True, 'team': 'Example', 'user': 'skopax-bot'}})
    store.save(state(anomaly(KEY)), None)

    # when
    with caplog.at_level(logging.DEBUG):
        with TestClient(create_app(ready_for_socket(), store, kube=Kube(root=tmp_path), transport=good, anthropic_client=FakeAnthropic([]))) as c:
            add_slack(c, {'bot-token': BOT, 'app-token': APP})
            tested = c.post('/api/admin/plugins/slack/test', json={'name': 'main', 'values': {}}, headers=AUTH).text
            changed = c.patch('/api/admin/plugins/slack/instances/main', json={'values': {'bot-token': NEW_BOT}}, headers=AUTH).text
            c.post('/api/slack-posts', json={'key': KEY}, headers=AUTH)
            seen = [tested, changed, c.get('/api/admin/plugins', headers=AUTH).text, c.get('/api/me', headers=AUTH).text, c.get('/api/server', headers=AUTH).text,
                    c.get('/api/activity?limit=500', headers=AUTH).text, c.get('/api/admin/history', headers=AUTH).text, c.get('/api/admin/features', headers=AUTH).text,
                    json.dumps(list(store.db['tunable_changes'].find({}, {'_id': 0})), default=str), json.dumps(list(store.db['activity'].find({}, {'_id': 0})), default=str)]

    # then
    shown = '\n'.join(seen + [caplog.text])
    assert web.tokens() and set(web.tokens()) == {f'Bearer {NEW_BOT}'}
    assert [t for t in (BOT, NEW_BOT, APP) if t in shown] == []


def test_an_env_listener_opens_once_a_failed_first_load_recovers(store: Store, web: SlackWeb, tmp_path, monkeypatch, caplog):
    # given
    factory = Factory()
    b = boot({'SLACK_BOT_TOKEN': BOT, 'SLACK_APP_TOKEN': APP})
    s = dataclasses.replace(b.settings, token=TOKEN, anthropic_api_key='sk-test', investigate_mode='shadow', slack_listen='shadow')
    indexes = store.ensure_indexes
    failures = [PyMongoError('the primary is stepping down')]

    def blip():
        if failures:
            raise failures.pop()
        return indexes()

    monkeypatch.setattr(store, 'ensure_indexes', blip)

    # when
    with caplog.at_level(logging.INFO, logger='skopax'):
        with TestClient(create_app(s, store, registry=b.registry, sources=b.source_set(), kube=Kube(root=tmp_path), anthropic_client=FakeAnthropic([]),
                                   operators_lookup=lambda email: False, slack_socket=factory)) as c:
            c.app.state.runtime.refresh()
            wait_for(lambda: bool(factory.made) and factory.made[0].connected.is_set(), seconds=5)
            seen = []
            wait_for(lambda: seen.append(c.get('/api/server', headers=AUTH).json()['slack']) or seen[-1]['socket'] is True, seconds=5)
            info = seen[-1]

    # then
    assert failures == [] and factory.tokens == [APP] and info['socket'] is True
    assert 'the Slack listener stays off' not in caplog.text


def test_slack_switched_off_ignores_listener_events_and_clicks(store: Store, web: SlackWeb, tmp_path):
    # given
    app = create_app(ready_for_socket(), store, kube=Kube(root=tmp_path), anthropic_client=FakeAnthropic([]), operators_lookup=lambda email: True, slack_socket=Factory())
    app.state.instances.create('slack', 'main', {'bot-token': BOT, 'app-token': APP}, True, 'test')
    app.state.plugin_state.set('slack', False, 'test')
    event = {'type': 'app_mention', 'channel': 'C1', 'user': 'U1', 'text': '<@UBOT> why is checkout-service slow?', 'ts': '1792224000.000100'}
    click = {'type': 'block_actions', 'user': {'id': 'U1'}, 'channel': {'id': 'C1'}, 'message': {'ts': '1792224000.000100'}, 'actions': [{'action_id': CREATE_TASK, 'value': 'gone'}]}

    # when
    heard, clicked = app.state.intake.handle(event), app.state.clicks.handle(click)

    # then
    assert (heard, clicked) == (None, None)
    assert web.seen == [] and store.list_intake(10) == [] and store.list_actions(None, 10) == []


def test_after_a_rotation_the_first_post_goes_to_the_channel_name():
    # given
    current = ['xoxb-old']
    sent = []

    def transport(token: str, payload: dict) -> dict:
        sent.append((token, payload['channel']))
        if payload['channel'] == {'xoxb-old': '#ops', 'xoxb-new': 'COLDWS01'}[token]:
            raise SlackError('channel_not_found', code='channel_not_found')
        return {'ok': True, 'channel': 'COLDWS01' if token == 'xoxb-old' else 'CNEWWS01', 'ts': f'1792224000.{len(sent):06d}'}

    slack = Slack(lambda: current[0], '#ops', transport, FakeSlackApi(), known_channel=lambda: 'COLDWS01')

    # when
    slack.post({'text': 'one'})
    slack.post({'text': 'two'})
    current[0] = 'xoxb-new'
    third = slack.post({'text': 'three'})

    # then
    assert sent == [('xoxb-old', '#ops'), ('xoxb-old', 'COLDWS01'), ('xoxb-old', 'COLDWS01'), ('xoxb-new', '#ops')]
    assert third['channel'] == 'CNEWWS01'


def test_an_auth_answer_for_a_rotated_token_is_not_kept():
    # given
    current = ['xoxb-old']
    asked, rotated = [], []

    def api(token: str, method: str, payload: dict) -> dict:
        asked.append((token, method))
        if token == 'xoxb-old':
            current[0] = 'xoxb-new'
            rotated.append(slack.token)
        return {'ok': True, 'user_id': 'U' + token[5:].upper(), 'url': f'https://{token[5:]}.slack.com/', '_scopes': 'chat:write'}

    slack = Slack(lambda: current[0], '#ops', FakeSlack(), api)

    # when
    raced = slack.bot_user()
    after = (slack.bot_user(), slack.workspace(), slack.scopes())

    # then
    assert (raced, rotated) == ('UOLD', ['xoxb-new'])
    assert after == ('UNEW', 'https://new.slack.com/', frozenset({'chat:write'}))
    assert asked == [('xoxb-old', 'auth.test'), ('xoxb-new', 'auth.test')]


def test_the_push_and_operator_rows_reach_slack_after_a_restart(store: Store, tmp_path):
    # given
    with TestClient(create_app(settings(), store, kube=Kube(root=tmp_path))) as c:
        pushed = c.put('/api/admin/settings', json={'key': 'slack.push-user', 'value': 'U0AAA1111, U0BBB2222'}, headers=AUTH)
        cached = c.put('/api/admin/settings', json={'key': 'slack.operator-cache-seconds', 'value': '120'}, headers=AUTH)

    # when
    again = create_app(settings(), store, kube=Kube(root=tmp_path))

    # then
    assert (pushed.status_code, cached.status_code, pushed.json()['item']['live'], cached.json()['item']['live']) == (200, 200, False, False)
    assert again.state.pusher.users == ['U0AAA1111', 'U0BBB2222'] and again.state.operators.cache_s == 120


def test_the_connection_test_fails_naming_the_read_scopes_the_token_lacks(store: Store):
    # given
    body = {'ok': True, 'team': 'Example', 'user': 'skopax-bot'}
    full = 'chat:write,files:write,files:read,channels:read,channels:history,users:read,users:read.email'

    def tried(scopes: str | None, values: dict) -> dict:
        fake = FakeHttp({('GET', AUTH_URL): Reply(body, {} if scopes is None else {'x-oauth-scopes': scopes}), ('POST', 'https://slack.com/api/apps.connections.open'): {'ok': True}})
        with TestClient(create_app(settings(), store, transport=fake)) as c:
            return c.post('/api/admin/plugins/slack/test', json={'values': values}, headers=AUTH).json()

    # when
    every = tried(full, {'bot-token': BOT, 'app-token': APP})
    no_email = tried('chat:write,files:write,channels:read,channels:history,users:read', {'bot-token': BOT})
    no_history = tried('chat:write,files:write,files:read,channels:read,users:read,users:read.email', {'bot-token': BOT, 'app-token': APP})
    bot_only = tried('chat:write,files:write,channels:read,users:read,users:read.email', {'bot-token': BOT})
    unknown = tried(None, {'bot-token': BOT})
    private_only = tried('chat:write,files:write,files:read,groups:read,groups:history,users:read,users:read.email', {'bot-token': BOT, 'app-token': APP})
    no_read = tried('chat:write,files:write,users:read,users:read.email', {'bot-token': BOT})

    # then
    assert every['ok'] and bot_only['ok'] and unknown['ok'], 'history is needed only with the app token the listener uses; no scope header, nothing to judge'
    assert private_only['ok'], 'an app made for a private channel needs no channels scope'
    assert (no_email['ok'], no_email['summary']) == (False, msg('integrations.slack_scopes', scopes='users:read.email'))
    assert (no_history['ok'], no_history['summary']) == (False, msg('integrations.slack_scopes', scopes='channels:history or groups:history'))
    assert (no_read['ok'], no_read['summary']) == (False, msg('integrations.slack_scopes', scopes='channels:read or groups:read'))


def test_a_read_refused_for_a_scope_names_the_scope_and_never_reads_as_none(monkeypatch):
    # given
    answers = iter([{'ok': False, 'error': 'missing_scope', 'needed': 'users:read.email', 'provided': 'chat:write'},
                    {'ok': False, 'error': 'missing_scope', 'needed': 'channels:read', 'provided': 'chat:write'},
                    {'ok': False, 'error': 'not_allowed_token_type'}])
    monkeypatch.setattr(urllib.request, 'urlopen', lambda req, timeout=None: SlackReply(next(answers)))
    slack = Slack(BOT, 'C0123ABCD')

    # when
    with pytest.raises(SlackError) as lookup:
        slack.user_by_email('alice@example.com')
    channel = slack.check_channel('C0123ABCD')
    with pytest.raises(SlackError) as token_type:
        slack.history('C0123ABCD', '1792224000.000100')

    # then
    assert (lookup.value.code, lookup.value.text) == ('missing_scope', 'Slack refused: the bot token lacks the scope users:read.email')
    assert channel == 'Slack refused: the bot token lacks the scope channels:read'
    assert (token_type.value.code, token_type.value.text) == ('not_allowed_token_type', 'Slack refused: this call needs a bot token with more scopes (not_allowed_token_type)')


class ChannelApi(FakeSlackApi):
    def __init__(self, scopes: str, private: bool = False, refused: str | None = None):
        super().__init__(scopes=scopes)
        self.private = private
        self.refused = refused

    def __call__(self, token: str, method: str, payload: dict) -> dict:
        if method == 'conversations.info':
            self.calls.append((method, payload))
            if self.refused:
                raise SlackError(f'Slack refused: the bot token lacks the scope {self.refused}', code='missing_scope', needed=self.refused)
            return {'ok': True, 'channel': {'id': payload['channel'], 'name': 'ops', 'is_private': self.private, 'is_member': True}}
        return super().__call__(token, method, payload)


def test_core_names_the_channel_scopes_a_public_or_private_main_channel_and_dm_chats_need():
    # given
    def missing(scopes: str, private: bool = False, listening: bool = True, dm: bool = False, refused: str | None = None) -> list[str]:
        return Slack(BOT, 'C0123ABCD', FakeSlack(), ChannelApi(scopes, private, refused)).missing_scopes(listening, dm)

    # when
    public = missing('chat:write,channels:read,channels:history')
    private = missing('chat:write,groups:read,groups:history', private=True)
    private_no_history = missing('chat:write,channels:read,channels:history,groups:read', private=True)
    dm_without_im = missing('chat:write,channels:read,channels:history', dm=True)
    no_listener = missing('chat:write,channels:read', listening=False, dm=True)
    unreadable = missing('chat:write', refused='channels:read,groups:read')
    unknown = missing('')

    # then
    assert (public, private, no_listener, unknown) == ([], [], [], [])
    assert private_no_history == ['groups:history']
    assert dm_without_im == ['im:history']
    assert unreadable == ['channels:read,groups:read']


def test_a_missing_channel_scope_is_an_admin_notice_after_the_slack_check(store: Store, tmp_path):
    # given
    api = ChannelApi('chat:write,channels:read,channels:history,groups:read,users:read,users:read.email', private=True)
    s = dataclasses.replace(settings(), slack_listen='live')

    # when
    with TestClient(create_app(s, store, slack=Slack(BOT, 'C0123ABCD', FakeSlack(), api))) as c:
        slack_instance(c.app, app_token=APP, bot=BOT)
        c.app.state.runtime.refresh()
        before = [i['id'] for i in c.get('/api/admin/notices', headers=AUTH).json()['items']]
        job = next(j for j in c.app.state.scheduler.jobs if j.name == 'slack-posts')
        job.run(1_792_224_000.0)
        notices = {i['id']: i['text'] for i in c.get('/api/admin/notices', headers=AUTH).json()['items']}

    # then
    assert 'slack-scopes' not in before
    assert notices['slack-scopes'] == msg('integrations.slack_scopes', scopes='groups:history, im:history')


CONNECT_URL = 'https://slack.com/api/apps.connections.open'
FULL_SCOPES = 'chat:write,files:write,files:read,channels:read,channels:history,users:read,users:read.email'


def test_the_connection_test_opens_a_socket_mode_url_with_the_app_token_and_names_a_refused_one(store: Store):
    # given
    auth = Reply({'ok': True, 'team': 'Example', 'user': 'skopax-bot'}, {'x-oauth-scopes': FULL_SCOPES})

    def tried(answer) -> tuple[dict, FakeHttp]:
        fake = FakeHttp({('GET', AUTH_URL): auth, ('POST', CONNECT_URL): answer})
        with TestClient(create_app(settings(), store, transport=fake)) as c:
            return c.post('/api/admin/plugins/slack/test', json={'values': {'bot-token': BOT, 'app-token': APP}}, headers=AUTH).json(), fake

    # when
    good, good_http = tried({'ok': True, 'url': 'wss://wss.slack.com/link/?ticket=t'})
    revoked, _ = tried({'ok': False, 'error': 'invalid_auth'})

    # then
    assert good['ok'] is True and [c[2]['Authorization'] for c in good_http.calls] == [f'Bearer {BOT}', f'Bearer {APP}']
    assert (revoked['ok'], revoked['summary']) == (False, msg('integrations.slack_app_refused', error='invalid_auth'))
    assert 'wss://' not in json.dumps(good)


def test_the_connection_test_names_chat_write_and_the_files_scopes_core_uses(store: Store):
    # given
    def tried(scopes: str, values: dict) -> dict:
        fake = FakeHttp({('GET', AUTH_URL): Reply({'ok': True, 'team': 'Example', 'user': 'skopax-bot'}, {'x-oauth-scopes': scopes}), ('POST', CONNECT_URL): {'ok': True}})
        with TestClient(create_app(settings(), store, transport=fake)) as c:
            return c.post('/api/admin/plugins/slack/test', json={'values': values}, headers=AUTH).json()

    # when
    no_post = tried(FULL_SCOPES.replace('chat:write,', ''), {'bot-token': BOT})
    no_upload = tried(FULL_SCOPES.replace('files:write,', ''), {'bot-token': BOT})
    no_read = tried(FULL_SCOPES.replace('files:read,', ''), {'bot-token': BOT, 'app-token': APP})
    bot_only = tried(FULL_SCOPES.replace('files:read,', ''), {'bot-token': BOT})

    # then
    assert (no_post['ok'], no_post['summary']) == (False, msg('integrations.slack_scopes', scopes='chat:write'))
    assert (no_upload['ok'], no_upload['summary']) == (False, msg('integrations.slack_scopes', scopes='files:write'))
    assert (no_read['ok'], no_read['summary']) == (False, msg('integrations.slack_scopes', scopes='files:read'))
    assert bot_only['ok'], 'files:read reads the pictures people attach to questions, so only the listener needs it'


def test_a_slack_outage_never_clears_the_scope_check():
    # given
    from skopax.slack import SlackDown

    class Down(ChannelApi):
        def __call__(self, token, method, payload):
            if method == 'conversations.info':
                raise SlackDown('Slack did not answer (TimeoutError)')
            return super().__call__(token, method, payload)

    # when
    with pytest.raises(SlackDown):
        Slack(BOT, 'C0123ABCD', FakeSlack(), Down('chat:write')).missing_scopes(True, False)


def test_a_fixed_scope_clears_on_the_next_check_since_the_scopes_are_read_after_the_channel_answer():
    # given
    class Fresh(ChannelApi):
        def __call__(self, token, method, payload):
            body = super().__call__(token, method, payload)
            return body | {'_scopes': self.scopes} if method == 'conversations.info' else body

    api = Fresh('chat:write,groups:read', private=True)
    slack = Slack(BOT, 'C0123ABCD', FakeSlack(), api)

    # when
    before = slack.missing_scopes(True, False)
    api.scopes = 'chat:write,groups:read,groups:history'
    after = slack.missing_scopes(True, False)

    # then
    assert (before, after) == (['groups:history'], [])


def test_a_scope_refusal_without_a_named_scope_reads_as_words_not_a_code():
    # given
    class TokenType(ChannelApi):
        def __call__(self, token, method, payload):
            if method == 'conversations.info':
                raise SlackError('x', code='not_allowed_token_type')
            return super().__call__(token, method, payload)

    # when
    missing = Slack(BOT, 'C0123ABCD', FakeSlack(), TokenType('chat:write')).missing_scopes(False, False)

    # then
    assert missing == ['channels:read or groups:read']


def test_the_scope_notice_is_stored_so_a_restarted_server_shows_it_before_its_first_check(store: Store, tmp_path):
    # given
    api = ChannelApi('chat:write,channels:read,channels:history,groups:read,users:read,users:read.email', private=True)
    s = dataclasses.replace(settings(), slack_listen='live')
    with TestClient(create_app(s, store, slack=Slack(BOT, 'C0123ABCD', FakeSlack(), api))) as c:
        slack_instance(c.app, app_token=APP, bot=BOT)
        c.app.state.runtime.refresh()
        next(j for j in c.app.state.scheduler.jobs if j.name == 'slack-posts').run(1_792_224_000.0)

    # when
    with TestClient(create_app(s, store, slack=Slack(BOT, 'C0123ABCD', FakeSlack(), api))) as c:
        notices = {i['id']: i['text'] for i in c.get('/api/admin/notices', headers=AUTH).json()['items']}

    # then
    assert notices['slack-scopes'] == msg('integrations.slack_scopes', scopes='groups:history, im:history')
