import pytest
from fastapi.testclient import TestClient

from tests.conftest import AUTH, anomaly, settings, state
from tests.fake_slack import FakeSlackApi
from tests.test_autosend import DRAFT, REPORT_DRAFT, due_now, due_report, job
from tests.test_deploy_reports import report
from tests.test_slack import KEY, client_with
from tests.test_tickets import FakeJira
from skopax.app import create_app
from skopax.slack import Slack, built_link
from skopax.store import Store

OTHER = 'log|checkout-service|ERROR|x'
WORKSPACE = 'https://example.slack.com/'


def link(ts: str, thread_ts: str | None = None) -> str:
    return f"https://example.slack.com/archives/C123/p{ts.replace('.', '')}" + (f'?thread_ts={thread_ts}&cid=C123' if thread_ts else '')


class Transport:
    def __init__(self):
        self.calls: list[dict] = []

    def __call__(self, token: str, payload: dict) -> dict:
        self.calls.append(payload)
        return {'ok': True, 'channel': 'C123', 'ts': f'1791900000.{len(self.calls):06d}'}


class BrokenApi(FakeSlackApi):
    def __call__(self, token: str, method: str, payload: dict) -> dict:
        self.calls.append((method, payload))
        raise RuntimeError('socket closed')


def seed(store: Store) -> Store:
    store.save(state(anomaly(KEY, sev='crit'), anomaly(OTHER, sev='crit', svc='payments-api')), None)
    return store


def post_of(client: TestClient, key: str = KEY) -> dict:
    return next(a for a in client.get('/api/state').json()['anomalies'] if a['key'] == key).get('slackPost') or {}


def test_a_sent_card_stores_slacks_permalink_on_the_post_and_the_action(store: Store):
    # given
    seed(store)
    api = FakeSlackApi()

    # when
    with client_with(store, Transport(), api=api) as client:
        res = client.post('/api/slack-posts', json={'key': KEY}, headers=AUTH)
        post = post_of(client)

    # then
    assert res.status_code == 201
    assert res.json()['slack'] == {'channel': 'C123', 'ts': '1791900000.000001', 'permalink': link('1791900000.000001')}
    assert post['permalink'] == link('1791900000.000001')
    assert api.methods() == ['chat.getPermalink']


def test_without_getpermalink_the_link_is_built_from_the_workspace_url_read_once(store: Store):
    # given
    seed(store)
    api = FakeSlackApi(fail={'chat.getPermalink'})

    # when
    with client_with(store, Transport(), api=api) as client:
        first = client.post('/api/slack-posts', json={'key': KEY}, headers=AUTH).json()
        second = client.post('/api/slack-posts', json={'key': OTHER}, headers=AUTH).json()
        posts = post_of(client), post_of(client, OTHER)

    # then
    assert (first['slack']['permalink'], second['slack']['permalink']) == (link('1791900000.000001'), link('1791900000.000002'))
    assert [p['permalink'] for p in posts] == [link('1791900000.000001'), link('1791900000.000002')]
    assert api.methods().count('auth.test') == 1


@pytest.mark.parametrize('api', [FakeSlackApi(fail={'chat.getPermalink', 'auth.test'}), FakeSlackApi(fail={'chat.getPermalink'}, url=None), BrokenApi()])
def test_a_link_that_cannot_be_had_never_fails_the_post(store: Store, api: FakeSlackApi):
    # given
    seed(store)

    # when
    with client_with(store, Transport(), api=api) as client:
        res = client.post('/api/slack-posts', json={'key': KEY}, headers=AUTH)
        post = post_of(client)

    # then
    assert res.status_code == 201
    assert res.json()['slack'] == {'channel': 'C123', 'ts': '1791900000.000001'}
    assert {k: post[k] for k in ('channel', 'ts')} == {'channel': 'C123', 'ts': '1791900000.000001'} and 'permalink' not in post


def test_a_sent_deploy_report_stores_the_permalink(store: Store):
    # given
    with client_with(store, Transport()) as client:
        saved = client.post('/api/deploy-reports', json=report(), headers=AUTH).json()

        # when
        res = client.post(f"/api/deploy-reports/{saved['id']}/slack-posts", headers=AUTH)
        listed = client.get('/api/deploy-reports').json()

    # then
    assert res.status_code == 201
    assert res.json()['slack']['permalink'] == link('1791900000.000001')
    assert listed[0]['slackPost']['permalink'] == link('1791900000.000001')


def test_the_auto_send_and_the_pods_send_store_the_permalink(store: Store):
    # given
    seed(store)
    with client_with(store, Transport()) as client:
        client.put('/api/slack-drafts', json={'key': KEY, 'text': DRAFT}, headers=AUTH)
        saved = client.post('/api/deploy-reports', json=report(), headers=AUTH).json()
        client.put(f"/api/deploy-reports/{saved['id']}/slack-draft", json={'text': REPORT_DRAFT}, headers=AUTH)
        due_now(store)
        due_report(store, saved['id'])

        # when
        sent = job(client).run_once()
        client.app.state.investigator.effects.send_slack(OTHER, 'Claude (pod)')
        auto, pod = post_of(client), post_of(client, OTHER)
        rep = client.get('/api/deploy-reports').json()[0]['slackPost']

    # then
    assert sent == 2
    assert {auto['permalink'], rep['permalink']} == {link('1791900000.000001'), link('1791900000.000002')}
    assert pod['permalink'] == link('1791900000.000003')


def test_a_thread_update_stores_the_replys_permalink(store: Store):
    # given
    seed(store)
    api = FakeSlackApi()
    with client_with(store, Transport(), api=api) as client:
        client.post('/api/slack-posts', json={'key': KEY}, headers=AUTH)
        client.post('/api/slack-posts', json={'key': OTHER}, headers=AUTH)

        # when
        found = client.post('/api/slack-updates', json={'key': KEY, 'text': 'fixed now'}, headers=AUTH).json()
        api.fail.add('chat.getPermalink')
        built = client.post('/api/slack-updates', json={'key': OTHER, 'text': 'fixed now'}, headers=AUTH).json()

    # then
    assert found['slack'] == {'channel': 'C123', 'ts': '1791900000.000003', 'permalink': link('1791900000.000003')}
    assert built['slack']['permalink'] == link('1791900000.000004', '1791900000.000002')


def test_built_links_need_an_https_workspace():
    # then
    assert built_link(WORKSPACE, 'C1', '1792224000.000100') == 'https://example.slack.com/archives/C1/p1792224000000100'
    assert built_link('https://example.slack.com', 'C1', '1792224000.000200', '1792224000.000100') == 'https://example.slack.com/archives/C1/p1792224000000200?thread_ts=1792224000.000100&cid=C1'
    assert built_link(WORKSPACE, 'C1', '1792224000.000100', '1792224000.000100') == 'https://example.slack.com/archives/C1/p1792224000000100'
    assert built_link(None, 'C1', '1792224000.000100') is None
    assert built_link('http://example.slack.com/', 'C1', '1792224000.000100') is None
    assert built_link(WORKSPACE, '', '1792224000.000100') is None


def seed_old_posts(store: Store) -> str:
    store.save(state(anomaly(KEY), anomaly(OTHER)), None)
    store.slack_posted(KEY, {'channel': 'C123', 'ts': '1791800000.000100', 'at': '2026-10-25T08:00:00Z', 'by': 'server', 'draft': False})
    store.slack_posted(OTHER, {'channel': 'C123', 'ts': '1791800000.000200', 'at': '2026-10-25T08:00:00Z', 'by': 'server', 'draft': False, 'permalink': 'https://example.slack.com/archives/C123/pkeep'})
    saved = store.add_deploy_report(report())
    store.report_slack_posted(saved['id'], {'channel': 'C123', 'ts': '1791800000.000300', 'at': '2026-10-25T08:00:00Z', 'by': 'server', 'draft': False})
    return saved['id']


def test_old_posts_get_the_built_link_once_at_startup(store: Store):
    # given
    seed_old_posts(store)
    before = store.rev()['rev']
    api = FakeSlackApi()

    # when
    with client_with(store, Transport(), api=api) as client:
        cards = {a['key']: a['slackPost'] for a in client.get('/api/state').json()['anomalies']}
        rep = client.get('/api/deploy-reports').json()[0]['slackPost']
    with client_with(store, Transport(), api=api):
        pass

    # then
    assert cards[KEY]['permalink'] == link('1791800000.000100')
    assert cards[OTHER]['permalink'] == 'https://example.slack.com/archives/C123/pkeep'
    assert rep['permalink'] == link('1791800000.000300')
    assert store.rev()['rev'] > before
    assert api.methods() == ['auth.test'], 'one workspace lookup, no getPermalink per old post, nothing on the second start'


@pytest.mark.parametrize('api', [FakeSlackApi(fail={'auth.test'}), FakeSlackApi(url=None), BrokenApi()])
def test_old_posts_stay_plain_when_the_workspace_url_is_not_known(store: Store, api: FakeSlackApi):
    # given
    seed_old_posts(store)

    # when
    with client_with(store, Transport(), api=api) as client:
        post = post_of(client)
        rep = client.get('/api/deploy-reports').json()[0]['slackPost']

    # then
    assert 'permalink' not in post and 'permalink' not in rep


def test_old_posts_are_left_alone_without_a_slack_token(store: Store):
    # given
    seed_old_posts(store)
    api = FakeSlackApi()

    # when
    with TestClient(create_app(settings(), store, slack=Slack('', '#ops', Transport(), api))) as client:
        post = post_of(client)

    # then
    assert 'permalink' not in post
    assert api.calls == []


def test_the_create_task_notice_in_the_thread_records_its_link(store: Store):
    # given
    seed(store)
    with TestClient(create_app(settings(), store, slack=Slack('xoxb-test', '#ops', Transport(), FakeSlackApi()), jira=FakeJira())) as client:
        client.post('/api/slack-posts', json={'key': KEY}, headers=AUTH)

        # when
        client.post('/api/actions', json={'type': 'ticket', 'key': KEY, 'target': KEY, 'title': 'OPS: checkout-service fix'}, headers=AUTH)
        notice = next(a for a in client.get('/api/actions').json() if a['type'] == 'slack-update')

    # then
    assert notice['slack'] == {'channel': 'C123', 'ts': '1791900000.000002', 'permalink': link('1791900000.000002')}


def test_the_create_task_notice_names_a_token_caller_claude_not_token(store: Store):
    # given
    seed(store)
    transport = Transport()
    with TestClient(create_app(settings(), store, slack=Slack('xoxb-test', '#ops', transport, FakeSlackApi()), jira=FakeJira())) as client:
        client.post('/api/slack-posts', json={'key': KEY}, headers=AUTH)

        # when
        client.post('/api/actions', json={'type': 'ticket', 'key': KEY, 'target': KEY, 'title': 'OPS: checkout-service fix'}, headers=AUTH)

    # then
    notice = transport.calls[-1]['text']
    assert notice.endswith('opened, card In progress · bootstrap') and 'token' not in notice
