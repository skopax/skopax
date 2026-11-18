from fastapi.testclient import TestClient

from skopax.app import create_app
from skopax.engine import PassResult
from skopax.kube import Kube
from skopax.messages import msg
from skopax.registry import Registry
from skopax.remote import HttpError
from skopax.store import Store
from tests.conftest import AUTH, settings, state
from tests.plugin_kit import add_instance
from tests.plugins import two_checks_plugin
from tests.teams_kit import SEO, Transport, add_route, open_card, tag
from tests.test_admin_plugins import HERE
from tests.test_jira_plugin import NOW, SEARCH, USER, VALUES, jira_links_app

SAID = {'text': 'We recommend adding a redirect for the old spring sale page.', 'by': 'pod', 'at': '2026-11-09T16:00:00Z'}


def both(c: TestClient, plugin: str) -> tuple[dict, dict]:
    admin = next(p for p in c.get('/api/admin/plugins', headers=AUTH).json()['plugins'] if p['id'] == plugin)
    tiles = {t['id']: t for t in c.get('/api/server', headers=AUTH).json()['plugins']}
    shown = {i['name']: {'state': i['state'], 'text': i['text']} for i in admin['health']['instances']}
    board = {i['name']: {'state': i.get('state'), 'text': i.get('text')} for i in (tiles.get(plugin) or {}).get('instances', [])}
    return shown, board


def test_a_jira_link_refusal_reads_no_access_on_the_board_as_on_admin(store: Store):
    # given
    with jira_links_app(store, {'search': HttpError(403, SEARCH)}) as c:
        c.app.state.instances.create('jira', 'main', VALUES | {'cloud-id': 'cid'}, True, USER)
        next(j for j in c.app.state.scheduler.jobs if j.name == 'jira-links').run(NOW)

        # when
        shown, board = both(c, 'jira')

    # then
    assert shown == board == {'main': {'state': 'warn', 'text': msg('integrations.health.no_access')}}


def test_a_failed_route_is_an_error_on_the_board_as_on_admin(store: Store, tmp_path):
    # given
    found = open_card('log|a|ERROR|one', tags=tag('seo'), business=SAID) | {'src': 'log', 'sig': 'err', 'ns': 'shop', 'sub': '', 'now': 5, 'd1': None, 'd7': None, 'unit': '',
                                                                           'short': 'x', 'slack': 1, 'ranKey': 'log'}
    store.save({**state(found), 'initialized': True}, None)
    with TestClient(create_app(settings(), store, kube=Kube(root=tmp_path), transport=Transport(fail='sig=SEO'))) as c:
        add_route(c, 'seo', SEO, tags='seo')
        c.app.state.server_apply([PassResult('log', True, 'ok', [found], {}, ('log',))], NOW, None)
        assert c.app.state.routes.drain(5)

        # when
        shown, board = both(c, 'teams')

    # then
    assert shown == board == {'seo': {'state': 'error', 'text': 'error: seo: HTTP 400'}}


def test_an_instance_with_a_config_problem_stays_on_the_board_as_an_error(store: Store):
    # given
    for i, name in enumerate(('main', 'east')):
        add_instance(store, two_checks_plugin.PLUGIN, name, primary=i == 0, url='https://demo.example.com')
    store.db['instances'].update_one({'_id': 'demo/east'}, {'$set': {'values.url': 'ftp://demo.example.com'}})

    # when
    with TestClient(create_app(settings(), store, registry=Registry.of(two_checks_plugin.PLUGIN, directory=HERE))) as c:
        shown, board = both(c, 'demo')
        store.db['instances'].update_one({'_id': 'demo/main'}, {'$set': {'values.url': 'ftp://demo.example.com'}})
        c.app.state.instances.refresh()
        alone, alone_board = both(c, 'demo')

    # then
    assert board == shown and board['east']['state'] == 'error' and board['east']['text'] == shown['east']['text']
    assert board['main'] == {'state': 'pending', 'text': msg('integrations.health.pending')}
    assert alone_board == alone and {r['state'] for r in alone_board.values()} == {'error'}, 'a plug-in whose every instance has a problem keeps its tile'


def test_a_nudged_check_reads_queued_on_its_tile_until_it_starts(store: Store):
    # given
    add_instance(store, two_checks_plugin.PLUGIN, 'main', primary=True, url='https://demo.example.com')
    with TestClient(create_app(settings(), store, registry=Registry.of(two_checks_plugin.PLUGIN, directory=HERE))) as c:
        before = {t['id']: t for t in c.get('/api/server', headers=AUTH).json()['plugins']}['demo']
        c.app.state.scheduler.nudge('demo.pulse')

        # when
        nudged = {t['id']: t for t in c.get('/api/server', headers=AUTH).json()['plugins']}['demo']

    # then
    assert before['queued'] is False and nudged['queued'] is True


def test_a_gitlab_instance_without_a_check_reads_no_checks_on_both_surfaces(store: Store):
    # given
    unreachable = {'url': 'https://gitlab.unreachable.example.com', 'token': 'glpat-test', 'group': 'acme'}
    with TestClient(create_app(settings(), store)) as c:
        c.app.state.instances.create('gitlab', 'main', unreachable, True, 'alice@example.com')

        # when
        shown, board = both(c, 'gitlab')
        top = next(p for p in c.get('/api/admin/plugins', headers=AUTH).json()['plugins'] if p['id'] == 'gitlab')['health']

    # then
    neutral = {'state': 'none', 'text': msg('integrations.health.no_checks')}
    assert shown == board == {'main': neutral}
    assert {'state': top['state'], 'text': top['text'], 'at': top['at']} == {**neutral, 'at': None}
    assert msg('integrations.health.connected') not in {r['text'] for r in top['instances']}
