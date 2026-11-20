import hashlib
import re

from fastapi.testclient import TestClient
from pymongo.errors import ServerSelectionTimeoutError

from tests.conftest import AUTH, anomaly, settings, state
from skopax import locales
from skopax.app import WEB, create_app
from skopax.messages import msg


def test_action_lifecycle_from_queue_to_done(client: TestClient):
    # given
    created = client.post('/api/actions', json={'type': 'dive', 'key': 'log|a|ERROR|x', 'label': 'Investigate a'}, headers=AUTH)
    action_id = created.json()['id']

    # when
    queued = client.get('/api/actions', params={'status': 'requested'}).json()
    picked = client.patch(f'/api/actions/{action_id}', json={'status': 'processing', 'expect': 'requested'}, headers=AUTH)
    done = client.patch(f'/api/actions/{action_id}', json={'status': 'done', 'result': 'note added'}, headers=AUTH)

    # then
    assert created.status_code == 201
    assert created.json()['status'] == 'requested'
    assert created.json()['target'] == 'log|a|ERROR|x'
    assert [a['id'] for a in queued] == [action_id]
    assert picked.json()['status'] == 'processing'
    assert done.json()['status'] == 'done'
    assert done.json()['result'] == 'note added'
    assert client.get('/api/actions', params={'status': 'requested'}).json() == []


def test_cancel_after_pickup_conflicts(client: TestClient):
    # given
    action_id = client.post('/api/actions', json={'type': 'dive', 'key': 'k'}, headers=AUTH).json()['id']
    client.patch(f'/api/actions/{action_id}', json={'status': 'processing', 'expect': 'requested'}, headers=AUTH)

    # when
    res = client.patch(f'/api/actions/{action_id}', json={'status': 'cancelled', 'expect': 'requested'}, headers=AUTH)

    # then
    assert res.status_code == 409
    assert res.json()['status'] == 'processing'


def test_finished_actions_do_not_move_again(client: TestClient):
    # given
    done_id = client.post('/api/actions', json={'type': 'dive', 'key': 'a'}, headers=AUTH).json()['id']
    cancelled_id = client.post('/api/actions', json={'type': 'dive', 'key': 'b'}, headers=AUTH).json()['id']
    client.patch(f'/api/actions/{done_id}', json={'status': 'done'}, headers=AUTH)
    client.patch(f'/api/actions/{cancelled_id}', json={'status': 'cancelled'}, headers=AUTH)

    # when
    reopened = client.patch(f'/api/actions/{done_id}', json={'status': 'processing'}, headers=AUTH)
    finished = client.patch(f'/api/actions/{cancelled_id}', json={'status': 'done'}, headers=AUTH)
    requeued = client.patch(f'/api/actions/{done_id}', json={'status': 'requested'}, headers=AUTH)

    # then
    assert reopened.status_code == 409
    assert reopened.json()['status'] == 'done'
    assert finished.status_code == 409
    assert finished.json()['status'] == 'cancelled'
    assert requeued.status_code == 409


def test_unknown_action_is_not_found(client: TestClient):
    # when
    res = client.patch('/api/actions/nope', json={'status': 'done'}, headers=AUTH)

    # then
    assert res.status_code == 404


def test_queueing_an_action_needs_the_token(client: TestClient):
    # when
    res = client.post('/api/actions', json={'type': 'dive', 'key': 'k'})

    # then
    assert res.status_code == 401


def test_queueing_rejects_an_unknown_action_type(client: TestClient):
    # when
    res = client.post('/api/actions', json={'type': 'delete-prod', 'key': 'k'}, headers=AUTH)

    # then
    assert res.status_code == 422


def test_page_embeds_the_state_and_escapes_closing_tags(client: TestClient):
    # given
    client.put('/api/state', json=state(anomaly('log|web|ERROR|</script><b>x'), swept='2026-10-10T09:04:00Z'), headers=AUTH)

    # when
    res = client.get('/')

    # then
    assert res.status_code == 200
    assert res.headers['content-type'].startswith('text/html')
    assert '/*__SKOPAX_DATA__*/' not in res.text
    assert '\\u003c/script>\\u003cb>x' in res.text
    assert '</script><b>x' not in res.text
    assert '"rev": 1' in res.text


def test_page_renders_before_any_state_exists(client: TestClient):
    # when
    res = client.get('/')

    # then
    assert res.status_code == 200
    assert '"anomalies": []' in res.text


def test_page_loads_every_script_file_with_its_content_hash(client: TestClient):
    # when
    res = client.get('/')

    # then
    core = locales.page_files()
    linked = re.findall(r'<script src="/static/([\w.-]+)\?v=(\w+)"></script>', res.text)
    names = [name for name, _ in linked if name not in core]
    assert core and set(core) <= {name for name, _ in linked}
    assert names[:4] == ['actors.js', 'icons.js', 'i18n.js', 'core.js'] and names[-1] == 'main.js'
    card = re.findall(r'<script src="/static/([\w.-]+)\?v=(\w+)"></script>', client.get('/c/1').text)
    assert [name for name, _ in card if name not in core] == ['actors.js', 'i18n.js', 'suggest.js', 'runshow.js', 'card.js']
    assert sorted(set(names) | {'card.js'}) == sorted(f.name for f in (WEB / 'static').glob('*.js'))
    assert '<script src="/static/' not in re.sub(r'<script src="/static/[\w.-]+\?v=\w+"></script>', '', res.text)
    for name, digest in linked:
        js = client.get(f'/static/{name}?v={digest}')
        assert js.status_code == 200
        assert js.headers['content-type'] == 'text/javascript; charset=utf-8'
        assert js.headers['cache-control'] == 'no-cache'
        assert js.content == (core[name] if name in core else (WEB / 'static' / name).read_bytes())
        assert hashlib.sha256(js.content).hexdigest()[:12] == digest


def test_static_file_revalidates_with_its_etag_and_hides_everything_else(client: TestClient):
    # given
    etag = client.get('/static/core.js').headers['etag']

    # when
    same = client.get('/static/core.js', headers={'If-None-Match': etag})
    stale = client.get('/static/core.js', headers={'If-None-Match': '"old"'})
    template = client.get('/static/template.html')
    outside = client.get('/static/..%2Fapp.py')

    # then
    assert same.status_code == 304
    assert same.content == b''
    assert stale.status_code == 200
    assert template.status_code == 404
    assert outside.status_code == 404


def test_health_reports_mongo(client: TestClient, store, monkeypatch):
    # given
    monkeypatch.setattr(store, 'ping', lambda: True)

    # when
    res = client.get('/actuator/health')

    # then
    assert res.status_code == 200
    assert res.json()['status'] == 'UP'
    assert res.json()['components']['mongo']['status'] == 'UP'


def test_health_is_down_without_mongo_while_liveness_stays_up(client: TestClient, store, monkeypatch):
    # given
    monkeypatch.setattr(store, 'ping', lambda: False)

    # when
    health = client.get('/actuator/health')
    liveness = client.get('/actuator/health/liveness')

    # then
    assert health.status_code == 503
    assert health.json()['status'] == 'DOWN'
    assert health.json()['components']['mongo']['status'] == 'DOWN'
    assert liveness.status_code == 200
    assert liveness.json() == {'status': 'UP'}


def test_readiness_answers_like_health_and_follows_mongo(client: TestClient, store, monkeypatch):
    # given
    monkeypatch.setattr(store, 'ping', lambda: True)

    # when
    up = client.get('/actuator/health/readiness')
    monkeypatch.setattr(store, 'ping', lambda: False)
    down = client.get('/actuator/health/readiness')
    health = client.get('/actuator/health')

    # then
    assert up.status_code == 200 and up.json()['status'] == 'UP' and up.json()['components']['mongo']['status'] == 'UP'
    assert down.status_code == 503 and down.json() == health.json()
    assert down.json()['components']['mongo']['status'] == 'DOWN'


def test_a_database_outage_answers_503(store, monkeypatch):
    # given
    def down(*_args, **_kwargs):
        raise ServerSelectionTimeoutError('no servers')

    monkeypatch.setattr(store, 'load', down)
    with TestClient(create_app(settings(), store)) as c:
        # when
        res = c.get('/api/state')

    # then
    assert res.status_code == 503
    assert res.json() == {'detail': msg('db.unavailable')}


def test_post_deploy_check_finishes_with_a_report(client: TestClient):
    # given
    action_id = client.post('/api/actions', json={'type': 'postdeploy', 'key': 'postdeploy', 'label': 'Post-deploy check'}, headers=AUTH).json()['id']
    client.patch(f'/api/actions/{action_id}', json={'status': 'processing', 'expect': 'requested'}, headers=AUTH)

    # when
    done = client.patch(f'/api/actions/{action_id}', json={'status': 'done', 'result': '3 deploys, all clean', 'report': 'checkout-service  clean\npayments-api  clean'}, headers=AUTH)

    # then
    assert done.status_code == 200
    assert done.json()['type'] == 'postdeploy'
    assert done.json()['result'] == '3 deploys, all clean'
    assert done.json()['report'] == 'checkout-service  clean\npayments-api  clean'


def test_actions_in_the_same_second_list_newest_first(client: TestClient):
    # given
    ids = [client.post('/api/actions', json={'type': 'mute', 'key': f'k{i}', 'hours': 1}, headers=AUTH).json()['id'] for i in range(5)]

    # when
    listed = client.get('/api/actions').json()

    # then
    assert [a['id'] for a in listed] == ids[::-1]


def test_an_ask_needs_a_question_is_capped_per_hour_and_is_operator_only(client: TestClient):
    # given
    good = {'type': 'ask', 'key': 'ask', 'text': '  Why did orders-service 5xx rise after 14:00?  ', 'cards': ['log|a|ERROR|x', 'log|a|ERROR|x']}

    # when
    empty = client.post('/api/actions', json={'type': 'ask', 'key': 'ask', 'text': '  '}, headers=AUTH)
    first = client.post('/api/actions', json=good, headers=AUTH)
    rest = [client.post('/api/actions', json=good, headers=AUTH).status_code for _ in range(6)]
    guest = client.post('/api/actions', json=good)
    queued = client.get('/api/actions?status=requested').json()

    # then
    assert empty.status_code == 422
    assert first.status_code == 201
    assert (first.json()['label'], first.json()['text'], first.json()['cards'], first.json()['target']) == ('Why did orders-service 5xx rise after 14:00?',) * 2 + (['log|a|ERROR|x'], 'ask')
    assert rest == [201] * 5 + [429]
    assert guest.status_code == 401
    assert len(queued) == 6 and all(q['type'] == 'ask' for q in queued)


def test_the_page_ask_cap_is_an_admin_setting_that_applies_at_once(client: TestClient):
    # given
    good = {'type': 'ask', 'key': 'ask', 'text': 'Why did orders-service 5xx rise?'}
    saved = client.put('/api/admin/settings', json={'key': 'page.ask-cap', 'value': '2'}, headers=AUTH)

    # when
    codes = [client.post('/api/actions', json=good, headers=AUTH).status_code for _ in range(3)]
    over = client.post('/api/actions', json=good, headers=AUTH)
    client.put('/api/admin/settings', json={'key': 'page.ask-cap', 'value': '3'}, headers=AUTH)
    raised = client.post('/api/actions', json=good, headers=AUTH)
    shown = client.get('/api/admin/settings', headers=AUTH).text

    # then
    assert saved.status_code == 200 and codes == [201, 201, 429]
    assert over.json()['detail'] == '2 asks an hour is the cap (Admin \u203a Investigator, Page asks per hour); try again later'
    assert raised.status_code == 201, 'a higher cap lets the next ask through without a restart'
    assert 'Page asks per hour' in shown


def test_an_operator_can_queue_a_reassessment_of_a_deploy_report(client: TestClient):
    # given
    from tests.test_deploy_reports import report
    saved = client.post('/api/deploy-reports', json=report('2 deploys since 05:06 UTC'), headers=AUTH).json()
    body = {'type': 'assess', 'key': f"report:{saved['id']}", 'target': f"report:{saved['id']}", 'reportId': saved['id']}

    # when
    created = client.post('/api/actions', json=body, headers=AUTH)
    missing_id = client.post('/api/actions', json={'type': 'assess', 'key': 'report:x'}, headers=AUTH)
    unknown = client.post('/api/actions', json={'type': 'assess', 'key': 'report:nope', 'reportId': 'nope'}, headers=AUTH)
    again = client.post('/api/actions', json=body, headers=AUTH)
    guest = client.post('/api/actions', json=body)

    # then
    assert created.status_code == 201
    assert (created.json()['status'], created.json()['reportId'], created.json()['source'], created.json()['label']) == ('requested', saved['id'], 'page', 'Re-assess deploy report: 2 deploys since 05:06 UTC')
    assert (created.json()['key'], created.json()['target']) == (f"report:{saved['id']}", f"report:{saved['id']}")
    assert missing_id.status_code == 422 and 'reportId' in missing_id.json()['detail']
    assert unknown.status_code == 404
    assert again.status_code == 409 and 'already queued or running' in again.json()['detail']
    assert guest.status_code == 401


def test_a_reassessment_is_capped_per_hour_and_the_servers_own_assessments_do_not_count(client: TestClient, store):
    # given
    from tests.test_deploy_reports import report
    ids = [client.post('/api/deploy-reports', json=report(f'r{i}'), headers=AUTH).json()['id'] for i in range(8)]
    for i in ids:
        store.add_action({'type': 'assess', 'key': f'report:{i}', 'target': f'report:{i}', 'reportId': i, 'requestedBy': 'server'}, 'done')

    # when
    codes = [client.post('/api/actions', json={'type': 'assess', 'key': f'report:{i}', 'reportId': i}, headers=AUTH).status_code for i in ids]

    # then
    assert codes == [201] * 6 + [429] * 2


def test_a_report_id_is_stored_only_on_an_assess(client: TestClient):
    # when
    dive = client.post('/api/actions', json={'type': 'dive', 'key': 'k', 'reportId': 'r1'}, headers=AUTH)

    # then
    assert dive.status_code == 201 and 'reportId' not in dive.json()


def png_size(body: bytes) -> tuple[int, int]:
    assert body[:8] == b'\x89PNG\r\n\x1a\n'
    return int.from_bytes(body[16:20], 'big'), int.from_bytes(body[20:24], 'big')


def test_the_app_manifest_makes_the_page_installable(client: TestClient):
    # when
    res = client.get('/manifest.webmanifest')
    manifest = res.json()

    # then
    assert res.status_code == 200 and res.headers['content-type'].startswith('application/manifest+json')
    assert {k: manifest[k] for k in ('name', 'short_name', 'id', 'start_url', 'scope', 'display')} == {
        'name': 'Skopax', 'short_name': 'Skopax', 'id': '/', 'start_url': '/', 'scope': '/', 'display': 'standalone'}
    assert manifest['theme_color'].startswith('#') and manifest['background_color'].startswith('#')
    sizes = {(i['sizes'], i.get('purpose', 'any')) for i in manifest['icons']}
    assert {('192x192', 'any'), ('512x512', 'any'), ('512x512', 'maskable')} <= sizes
    for icon in manifest['icons']:
        got = client.get(icon['src'])
        w, h = icon['sizes'].split('x')
        assert got.status_code == 200 and got.headers['content-type'] == 'image/png' and icon['type'] == 'image/png'
        assert png_size(got.content) == (int(w), int(h))


def test_the_page_links_the_manifest_the_apple_icon_and_the_theme_colour(client: TestClient):
    # when
    page = client.get('/').text
    apple = client.get('/icons/apple-touch-icon.png')

    # then
    assert '<link rel="manifest" href="/manifest.webmanifest">' in page
    assert '<link rel="apple-touch-icon" href="/icons/apple-touch-icon.png">' in page
    assert '<meta name="apple-mobile-web-app-title" content="Skopax">' in page
    assert re.search(r'<meta name="theme-color" media="\(prefers-color-scheme: dark\)" content="#[0-9a-f]{6}">', page)
    assert apple.status_code == 200 and png_size(apple.content) == (180, 180)


def test_icons_revalidate_with_their_etag_and_hide_everything_else(client: TestClient):
    # given
    etag = client.get('/icons/icon-192.png').headers['etag']

    # when
    same = client.get('/icons/icon-192.png', headers={'If-None-Match': etag})
    unknown = client.get('/icons/nope.png')
    outside = client.get('/icons/..%2Fapp.py')

    # then
    assert same.status_code == 304
    assert unknown.status_code == 404 and outside.status_code == 404


def test_the_chat_message_length_is_an_admin_setting_for_the_chat_the_page_ask_and_the_page_inputs(client: TestClient):
    # given
    client.put('/api/admin/settings', json={'key': 'chat.max-chars', 'value': '120'}, headers=AUTH)
    long_text = 'x' * 121

    # when
    ask = client.post('/api/actions', json={'type': 'ask', 'key': 'ask', 'text': long_text}, headers=AUTH)
    fits = client.post('/api/actions', json={'type': 'ask', 'key': 'ask', 'text': 'y' * 120}, headers=AUTH)
    client.put('/api/admin/settings', json={'key': 'chat.max-chars', 'value': '2000'}, headers=AUTH)
    longer = client.post('/api/actions', json={'type': 'ask', 'key': 'ask', 'text': 'z' * 1500}, headers=AUTH)
    me = client.get('/api/me').json()

    # then
    assert ask.status_code == 413 and ask.json()['detail'] == 'a question is at most 120 characters (Admin \u203a Investigator, Chat message length)'
    assert fits.status_code == 201
    assert longer.status_code == 201, 'raised past the old 1000 without a restart'
    assert me['chatMax'] == 2000, 'the page sets its ask inputs from it'


def test_the_archive_hours_are_an_admin_setting_the_page_gets_with_the_state(client: TestClient):
    # given
    before = client.get('/api/state').json()['archiveHours']

    # when
    saved = client.put('/api/admin/settings', json={'key': 'cards.archive-hours', 'value': '72'}, headers=AUTH)
    after = client.get('/api/state').json()['archiveHours']
    page = client.get('/').text

    # then
    assert (before, saved.status_code, after) == (48, 200, 72)
    assert '"archiveHours": 72' in page, 'the first render already hides archived cards'


def test_an_ask_sent_with_the_api_token_is_marked_as_coming_from_the_api_and_shares_the_page_ask_cap(client: TestClient, store):
    # given
    client.put('/api/admin/settings', json={'key': 'page.ask-cap', 'value': '2'}, headers=AUTH)
    store.add_action({'type': 'ask', 'key': 'ask', 'target': 'ask', 'text': 'from the page', 'source': 'page', 'requestedBy': 'nina.white@example.com'})
    good = {'type': 'ask', 'key': 'ask', 'text': 'Why did orders-service 5xx rise?'}

    # when
    first = client.post('/api/actions', json=good, headers=AUTH)
    second = client.post('/api/actions', json=good, headers=AUTH)

    # then
    assert first.status_code == 201 and first.json()['source'] == 'api', 'the Requests tab and the queue say API, not Skopax page'
    assert second.status_code == 429, 'page and API asks count against the same hourly cap'
