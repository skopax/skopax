import dataclasses
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from skopax import brand
from skopax.app import create_app
from skopax.backlink import Backlinks
from skopax.contract import Denied
from skopax.plugins.jira.client import JIRA_GATEWAY, Jira
from skopax.remote import HttpError
from skopax.store import Store
from tests.conftest import AUTH, anomaly, settings, state
from tests.fake_http import FakeHttp

NOW = 1_792_224_000.0
USER = 'alice@example.com'
SITE = 'https://example.atlassian.net'
GATEWAY = f'{JIRA_GATEWAY}/cid'
SEARCH = f'{GATEWAY}/rest/api/3/search/jql'
CREATE = f'{GATEWAY}/rest/api/3/issue'
USERS = f'{GATEWAY}/rest/api/3/user/search'
VALUES = {'url': SITE, 'email': 'bot@example.com', 'api-token': 'jt', 'cloud-id': 'cid'}
BASE = 'https://skopax.example.com'
KEY = 'log|checkout-service|ERROR|retry'
OTHER = 'log|checkout-service|ERROR|other'


def remote(issue: str) -> str:
    return f'{GATEWAY}/rest/api/3/issue/{issue}/remotelink'


def comment(issue: str) -> str:
    return f'{GATEWAY}/rest/api/3/issue/{issue}/comment'


def answering(*issues: str, error: Exception | None = None) -> FakeHttp:
    routes = {}
    for issue in issues:
        routes[('POST', remote(issue))] = error or {'id': 10000, 'self': remote(issue) + '/10000'}
        routes[('POST', comment(issue))] = error or {'id': '20000'}
    return FakeHttp(routes)


def client(http: FakeHttp) -> Jira:
    return Jira('bot@example.com', 'jt', http, cloud_id='cid', site=SITE)


def saved(store: Store, *cards: dict) -> None:
    store.save({**state(*cards), 'initialized': True}, None)


def jira_lines(store: Store) -> list[str]:
    return [a['text'] for a in reversed(store.list_activity(None, 50)) if a['kind'] == 'jira']


def backlinks(store: Store, http: FakeHttp, mode: str = 'link', base: str = BASE) -> Backlinks:
    return Backlinks(store, client(http), base, mode=lambda: mode)


def app_with(store: Store, http: FakeHttp, mode: str = 'link') -> TestClient:
    return TestClient(create_app(dataclasses.replace(settings(), jira_backlink=mode), store, transport=http))


def drained(c: TestClient) -> None:
    pool = c.app.state.backlinks.pool
    if pool is not None:
        pool.submit(lambda: None).result(timeout=30)


def with_jira(c: TestClient) -> None:
    c.app.state.instances.create('jira', 'main', VALUES, True, USER)


def test_the_client_posts_a_remote_link_with_the_global_id_and_the_card_link():
    # given
    http = answering('OPS-40')

    # when
    client(http).remote_link('OPS-40', f'{BASE}/c/12', 'Skopax card #12 · checkout-service / ERROR pattern', 'skopax:skopax.example.com:card:12', f'{BASE}/favicon.ico')

    # then
    [(method, url, _, body)] = http.calls
    assert (method, url) == ('POST', remote('OPS-40'))
    assert body == {'globalId': 'skopax:skopax.example.com:card:12', 'application': {'type': 'skopax', 'name': 'Skopax'},
                    'object': {'url': f'{BASE}/c/12', 'title': 'Skopax card #12 · checkout-service / ERROR pattern', 'icon': {'url16x16': f'{BASE}/favicon.ico', 'title': 'Skopax'}}}


def test_the_client_posts_a_comment_body_as_given():
    # given
    http = answering('OPS-40')
    doc = {'type': 'doc', 'version': 1, 'content': []}

    # when
    client(http).add_comment('OPS-40', doc)

    # then
    assert [(c[0], c[1], c[3]) for c in http.calls] == [('POST', comment('OPS-40'), {'body': doc})]


@pytest.mark.parametrize('status', [401, 403])
def test_a_refused_remote_link_names_the_link_issues_permission(status):
    # given
    http = answering('OPS-40', error=HttpError(status, remote('OPS-40')))

    # when
    with pytest.raises(Denied) as caught:
        client(http).remote_link('OPS-40', f'{BASE}/c/12', 'title', 'skopax:x:card:12')

    # then
    assert f'HTTP {status}' in caught.value.text and 'Link issues' in caught.value.text


def test_a_refused_comment_names_the_add_comments_permission():
    # given
    http = answering('OPS-40', error=HttpError(403, comment('OPS-40')))

    # when
    with pytest.raises(Denied) as caught:
        client(http).add_comment('OPS-40', {'type': 'doc', 'version': 1, 'content': []})

    # then
    assert 'HTTP 403' in caught.value.text and 'Add comments' in caught.value.text


def test_a_take_with_a_ticket_on_the_page_links_the_jira_issue_back_to_the_card(store: Store):
    # given
    saved(store, anomaly(KEY, num=12))
    http = answering('OPS-40')

    # when
    with app_with(store, http) as c:
        with_jira(c)
        done = c.post('/api/actions', json={'type': 'take', 'key': KEY, 'ticket': 'OPS-40'}, headers=AUTH)
        drained(c)

    # then
    assert done.json()['status'] == 'done'
    [(_, _, _, body)] = http.hits(remote('OPS-40'))
    assert body['globalId'] == 'skopax:skopax.example.com:card:12'
    assert body['object']['url'] == f'{BASE}/c/12'
    assert body['object']['title'] == f'{brand.product()} card #12 · checkout-service / ERROR pattern'
    assert body['object']['icon']['url16x16'] == f'{BASE}/favicon.ico'
    assert jira_lines(store) == ['Linked back from OPS-40']


def test_a_card_without_a_number_links_back_with_the_main_page_link(store: Store):
    # given
    saved(store, anomaly(KEY))
    http = answering('OPS-40')

    # when
    backlinks(store, http).ticket_set(KEY, 'OPS-40', USER)

    # then
    [(_, _, _, body)] = http.hits(remote('OPS-40'))
    assert body['object']['url'] == f'{BASE}/#a=log%7Ccheckout-service%7CERROR%7Cretry'
    assert body['globalId'] == f'skopax:skopax.example.com:card:{KEY}'
    assert body['object']['title'] == f'{brand.product()} card · checkout-service / ERROR pattern'


def test_a_link_action_and_a_resolve_with_a_ticket_also_link_back(store: Store):
    # given
    saved(store, anomaly(KEY, num=12), anomaly(OTHER, num=13))
    http = answering('OPS-40', 'OPS-41')

    # when
    with app_with(store, http) as c:
        with_jira(c)
        c.post('/api/actions', json={'type': 'link', 'key': KEY, 'ticket': 'ops-40'}, headers=AUTH)
        c.post('/api/actions', json={'type': 'resolve', 'key': OTHER, 'ticket': 'OPS-41', 'kind': 'fixed', 'ref': 'OPS-41'}, headers=AUTH)
        drained(c)

    # then
    assert [h[3]['globalId'] for h in http.hits(remote('OPS-40')) + http.hits(remote('OPS-41'))] == ['skopax:skopax.example.com:card:12', 'skopax:skopax.example.com:card:13']


def test_the_same_ticket_again_makes_no_second_call(store: Store):
    # given
    saved(store, anomaly(KEY, num=12))
    http = answering('OPS-40')

    # when
    with app_with(store, http) as c:
        with_jira(c)
        c.post('/api/actions', json={'type': 'take', 'key': KEY, 'ticket': 'OPS-40'}, headers=AUTH)
        c.post('/api/actions', json={'type': 'link', 'key': KEY, 'ticket': 'OPS-40'}, headers=AUTH)
        drained(c)

    # then
    assert len(http.hits(remote('OPS-40'))) == 1


def test_a_ticket_already_linked_back_once_is_not_linked_again_when_it_comes_back(store: Store):
    # given
    saved(store, anomaly(KEY, num=12))
    http = answering('OPS-40', 'OPS-41')
    links = backlinks(store, http)
    links.ticket_set(KEY, 'OPS-40', USER)
    links.ticket_set(KEY, 'OPS-41', USER)

    # when
    links.ticket_set(KEY, 'OPS-40', USER)

    # then
    assert (len(http.hits(remote('OPS-40'))), len(http.hits(remote('OPS-41')))) == (1, 1)


def test_a_changed_ticket_links_the_new_issue_back(store: Store):
    # given
    saved(store, anomaly(KEY, num=12, triage='progress', ticket='OPS-40'))
    http = answering('OPS-40', 'OPS-41')

    # when
    with app_with(store, http) as c:
        with_jira(c)
        c.post('/api/actions', json={'type': 'link', 'key': KEY, 'ticket': 'OPS-41'}, headers=AUTH)
        drained(c)

    # then
    assert (len(http.hits(remote('OPS-40'))), len(http.hits(remote('OPS-41')))) == (0, 1)
    assert http.hits(remote('OPS-41'))[0][3]['globalId'] == 'skopax:skopax.example.com:card:12'


def test_create_task_links_the_new_issue_back(store: Store):
    # given
    saved(store, anomaly(KEY, num=12))
    http = answering('OPS-140')
    http.routes[('POST', CREATE)] = {'key': 'OPS-140'}
    http.routes[('GET', USERS)] = []

    # when
    with app_with(store, http) as c:
        with_jira(c)
        done = c.post('/api/actions', json={'type': 'ticket', 'key': KEY, 'title': 'checkout-service retry storm'}, headers=AUTH)
        drained(c)

    # then
    assert done.json()['status'] == 'done'
    assert len(http.hits(remote('OPS-140'))) == 1


def test_comment_mode_comments_once_per_card_and_issue_and_skips_a_created_task(store: Store):
    # given
    saved(store, anomaly(KEY, num=12), anomaly(OTHER, num=13))
    http = answering('OPS-40', 'OPS-140')
    links = backlinks(store, http, mode='comment')

    # when
    links.ticket_set(KEY, 'OPS-40', USER)
    links.ticket_set(KEY, 'OPS-40', USER)
    links.ticket_set(OTHER, 'OPS-140', USER, created=True)

    # then
    [(_, _, _, body)] = http.hits(comment('OPS-40'))
    texts = [part for para in body['body']['content'] for part in para['content']]
    assert any(p.get('marks') == [{'type': 'link', 'attrs': {'href': f'{BASE}/c/12'}}] and p['text'] == f'{brand.product()} card #12 · checkout-service / ERROR pattern' for p in texts)
    assert http.hits(remote('OPS-40')) == [] and http.hits(comment('OPS-140')) == []
    assert jira_lines(store) == ['Commented the card link on OPS-40']


def test_off_mode_makes_no_call(store: Store):
    # given
    saved(store, anomaly(KEY, num=12))
    http = answering('OPS-40')

    # when
    with app_with(store, http, mode='off') as c:
        with_jira(c)
        c.post('/api/actions', json={'type': 'take', 'key': KEY, 'ticket': 'OPS-40'}, headers=AUTH)
        drained(c)

    # then
    assert http.calls == [] and store.anomaly(KEY)['ticket'] == 'OPS-40'


@pytest.mark.parametrize('base, jira_on', [('', True), (BASE, False)])
def test_no_public_address_or_no_jira_skips_silently(store: Store, base, jira_on):
    # given
    saved(store, anomaly(KEY, num=12))
    http = answering('OPS-40')
    jira = client(http) if jira_on else Jira('', '', http)

    # when
    Backlinks(store, jira, base, mode=lambda: 'link').ticket_set(KEY, 'OPS-40', USER)

    # then
    assert http.calls == [] and jira_lines(store) == []


def test_a_jira_refusal_adds_an_activity_line_and_the_ticket_stays_on_the_card(store: Store):
    # given
    saved(store, anomaly(KEY, num=12))
    http = answering('OPS-40', error=HttpError(403, remote('OPS-40')))

    # when
    with app_with(store, http) as c:
        with_jira(c)
        done = c.post('/api/actions', json={'type': 'take', 'key': KEY, 'ticket': 'OPS-40'}, headers=AUTH)
        drained(c)

    # then
    assert done.json()['status'] == 'done'
    card = store.anomaly(KEY)
    assert (card['triage'], card['ticket']) == ('progress', 'OPS-40')
    [line] = jira_lines(store)
    assert line.startswith('Jira back-link not added to OPS-40: ') and 'HTTP 403' in line and 'Link issues' in line


def test_a_jira_outage_adds_an_activity_line_and_a_later_set_tries_again(store: Store):
    # given
    saved(store, anomaly(KEY, num=12))
    http = answering('OPS-40', error=HttpError(503, remote('OPS-40')))
    links = backlinks(store, http)

    # when
    links.ticket_set(KEY, 'OPS-40', USER)
    http.routes[('POST', remote('OPS-40'))] = {'id': 1}
    links.ticket_set(KEY, 'OPS-40', USER)

    # then
    assert jira_lines(store) == ['Jira back-link not added to OPS-40: Jira HTTP 503', 'Linked back from OPS-40']


def test_the_investigator_triage_links_the_ticket_back(store: Store):
    # given
    saved(store, anomaly(KEY, num=12))
    http = answering('OPS-40', 'OPS-41')

    # when
    with app_with(store, http) as c:
        with_jira(c)
        before = store.anomaly(KEY)
        store.triage(KEY, 'progress', 'OPS-40 OPS-41', by='pod')
        c.app.state.investigator.effects.state_note(KEY, before)
        drained(c)

    # then
    assert (len(http.hits(remote('OPS-40'))), len(http.hits(remote('OPS-41')))) == (1, 1)


def test_the_jira_link_sweep_links_the_issue_back(store: Store):
    # given
    saved(store, anomaly(KEY, num=12))
    found = {'issues': [{'key': 'OPS-40', 'fields': {'summary': 's', 'status': {'name': 'In Development'}, 'description': f'see {BASE}/#a=log%7Ccheckout-service%7CERROR%7Cretry', 'comment': {'comments': []}}}]}
    http = answering('OPS-40')
    http.routes[('POST', SEARCH)] = lambda query, body: found if body['jql'].startswith('text ~') else {'issues': []}

    # when
    with app_with(store, http) as c:
        with_jira(c)
        next(j for j in c.app.state.scheduler.jobs if j.name == 'jira-links').run(NOW)
        drained(c)

    # then
    assert store.anomaly(KEY)['ticket'] == 'OPS-40'
    assert [h[3]['globalId'] for h in http.hits(remote('OPS-40'))] == ['skopax:skopax.example.com:card:12']


def test_the_same_ticket_set_again_after_a_refusal_links_back_once(store: Store):
    # given
    saved(store, anomaly(KEY, num=12))
    http = answering('OPS-40', error=HttpError(403, remote('OPS-40')))

    # when
    with app_with(store, http) as c:
        with_jira(c)
        c.post('/api/actions', json={'type': 'take', 'key': KEY, 'ticket': 'OPS-40'}, headers=AUTH)
        drained(c)
        http.routes[('POST', remote('OPS-40'))] = {'id': 1}
        c.post('/api/actions', json={'type': 'take', 'key': KEY, 'ticket': 'OPS-40'}, headers=AUTH)
        c.post('/api/actions', json={'type': 'link', 'key': KEY, 'ticket': 'OPS-40'}, headers=AUTH)
        drained(c)

    # then
    assert len(http.hits(remote('OPS-40'))) == 2
    assert jira_lines(store)[-1] == 'Linked back from OPS-40' and sum(line == 'Linked back from OPS-40' for line in jira_lines(store)) == 1


def test_a_free_text_resolve_back_links_only_keys_of_the_configured_projects_as_written(store: Store):
    # given
    saved(store, anomaly(KEY, num=12), anomaly(OTHER, num=13))
    http = answering('OPS-40', 'UTF-8', 'PR-12', 'OPS-41')

    # when
    with app_with(store, http) as c:
        with_jira(c)
        c.post('/api/actions', json={'type': 'resolve', 'key': KEY, 'ticket': 'fixed by utf-8 patch, vendor case PR-12'}, headers=AUTH)
        c.post('/api/actions', json={'type': 'resolve', 'key': OTHER, 'ticket': 'fixed in OPS-40, see ops-41 and PR-12'}, headers=AUTH)
        drained(c)

    # then
    assert [c[1] for c in http.calls] == [remote('OPS-40')]


def test_shutdown_lets_a_running_back_link_finish_and_drops_the_queued_ones(store: Store):
    # given
    saved(store, anomaly(KEY, num=12), anomaly(OTHER, num=13))
    started, go = threading.Event(), threading.Event()

    class Slow:
        enabled = True
        sent: list[str] = []

        def remote_link(self, issue, *rest):
            started.set()
            go.wait(5)
            self.sent.append(issue)

    jira = Slow()
    links = Backlinks(store, jira, BASE, pool=lambda: ThreadPoolExecutor(max_workers=1))
    links.ticket_set(KEY, 'OPS-40', USER)
    links.ticket_set(OTHER, 'OPS-41', USER)
    started.wait(5)
    threading.Timer(0.2, go.set).start()

    # when
    links.stop(timeout=5)
    links.ticket_set(KEY, 'OPS-42', USER)

    # then
    assert jira.sent == ['OPS-40'] and jira_lines(store) == ['Linked back from OPS-40']


def test_a_closing_store_cannot_raise_out_of_a_failed_back_link(store: Store, monkeypatch):
    # given
    saved(store, anomaly(KEY, num=12))
    http = answering('OPS-40', error=HttpError(503, remote('OPS-40')))

    def closed(*args, **kwargs):
        raise RuntimeError('cannot use MongoClient after close')

    monkeypatch.setattr(store, 'add_activity', closed)

    # when
    backlinks(store, http).ticket_set(KEY, 'OPS-40', USER)

    # then
    assert len(http.hits(remote('OPS-40'))) == 1


def test_a_comment_that_went_out_but_was_not_recorded_is_not_reported_as_not_added(store: Store, monkeypatch):
    # given
    saved(store, anomaly(KEY, num=12))
    http = answering('OPS-40')

    def refused(*args, **kwargs):
        raise RuntimeError('write refused')

    monkeypatch.setattr(store, 'set_tagging', refused)

    # when
    backlinks(store, http, mode='comment').ticket_set(KEY, 'OPS-40', USER)

    # then
    assert len(http.hits(comment('OPS-40'))) == 1 and jira_lines(store) == ['Commented the card link on OPS-40']


def test_the_global_id_carries_the_base_path_and_stays_under_255_characters(store: Store):
    # given
    long_key = 'log|checkout-service|ERROR|' + 'x' * 400
    saved(store, anomaly(KEY, num=12), anomaly(long_key))
    http = answering('OPS-40', 'OPS-41')
    links = backlinks(store, http, base='https://example.com/ops/')

    # when
    links.ticket_set(KEY, 'OPS-40', USER)
    links.ticket_set(long_key, 'OPS-41', USER)

    # then
    short, = [h[3]['globalId'] for h in http.hits(remote('OPS-40'))]
    long_id, = [h[3]['globalId'] for h in http.hits(remote('OPS-41'))]
    assert short == 'skopax:example.com/ops:card:12'
    assert len(long_id) <= 255 and long_id.startswith('skopax:example.com/ops:card:') and len(long_id.rsplit(':', 1)[1]) == 24
