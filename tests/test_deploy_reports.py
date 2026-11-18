from fastapi.testclient import TestClient
import pytest
from urllib.parse import parse_qs, urlsplit

from tests.conftest import AUTH, PROM_UI, settings
from tests.plugin_kit import add_instance
from skopax.app import create_app
from skopax.plugins import prometheus
from skopax.store import Store

SECONDARY_UI = 'https://prometheus.secondary.example.com'


@pytest.fixture
def linked(store: Store) -> TestClient:
    add_instance(store, prometheus.PLUGIN, 'main', primary=True, url='http://prometheus.monitoring:9090', **{'ui-url': PROM_UI})
    add_instance(store, prometheus.PLUGIN, 'secondary', url='http://prometheus.secondary:9090', **{'ui-url': SECONDARY_UI})
    with TestClient(create_app(settings(), store)) as c:
        yield c


def report(summary: str = '2 deploys since 05:06 UTC: 1 clean, orders-service restarts', **extra) -> dict:
    return {
        'summary': summary,
        'windowFrom': '2026-10-13T05:06:00Z',
        'windowTo': '2026-10-13T05:35:00Z',
        'rows': [
            {'ns': 'shop', 'svc': 'checkout-service', 'fromImage': '1a1a1a1a', 'toImage': '2b2b2b2b', 'at': '2026-10-13T05:08:00Z'},
            {'ns': 'payments', 'svc': 'orders-service', 'fromImage': '3c3c3c3c', 'toImage': '4d4d4d4d', 'at': '2026-10-13T05:12:00Z', 'flags': ['restarts=3'], 'metrics': 'req 1013 (1047/983)', 'note': 'config binding failed until 05:14'},
        ],
        'text': 'checkout-service  clean\norders-service  restarts=3',
        **extra,
    }


def test_saved_reports_list_newest_first(client: TestClient):
    # given
    first = client.post('/api/deploy-reports', json=report('first'), headers=AUTH)
    second = client.post('/api/deploy-reports', json=report('second'), headers=AUTH)

    # when
    listed = client.get('/api/deploy-reports').json()

    # then
    assert first.status_code == 201
    assert first.json()['id']
    assert first.json()['createdAt']
    assert [r['summary'] for r in listed] == ['second', 'first']
    assert listed[0]['id'] == second.json()['id']
    assert listed[0]['rows'][1]['flags'] == ['restarts=3']
    assert listed[0]['rows'][1]['metrics'] == 'req 1013 (1047/983)'
    assert listed[0]['rows'][0]['flags'] == []


def test_list_honours_the_limit(client: TestClient):
    # given
    for i in range(3):
        client.post('/api/deploy-reports', json=report(f'r{i}'), headers=AUTH)

    # when
    listed = client.get('/api/deploy-reports', params={'limit': 2}).json()

    # then
    assert len(listed) == 2


def test_saving_a_report_needs_the_token(client: TestClient):
    # when
    res = client.post('/api/deploy-reports', json=report())

    # then
    assert res.status_code == 401
    assert client.get('/api/deploy-reports').json() == []


def test_report_from_the_page_button_links_both_ways(client: TestClient):
    # given
    action = client.post('/api/actions', json={'type': 'postdeploy', 'key': 'postdeploy', 'label': 'Post-deploy check'}, headers=AUTH).json()

    # when
    saved = client.post('/api/deploy-reports', json=report(actionId=action['id']), headers=AUTH).json()

    # then
    assert saved['actionId'] == action['id']
    assert saved['requestedBy'] == 'token:bootstrap'
    linked = client.get('/api/actions').json()[0]
    assert linked['reportId'] == saved['id']


def test_report_for_an_unknown_action_is_not_found(client: TestClient):
    # when
    res = client.post('/api/deploy-reports', json=report(actionId='nope'), headers=AUTH)

    # then
    assert res.status_code == 404
    assert client.get('/api/deploy-reports').json() == []


def test_report_without_a_summary_is_rejected(client: TestClient):
    # when
    res = client.post('/api/deploy-reports', json=report(summary=''), headers=AUTH)

    # then
    assert res.status_code == 422


def test_report_rows_need_a_service(client: TestClient):
    # given
    body = report()
    body['rows'][0].pop('svc')

    # when
    res = client.post('/api/deploy-reports', json=body, headers=AUTH)

    # then
    assert res.status_code == 422


def test_released_issues_round_trip(client: TestClient):
    # given
    body = report(releases=['OPS 13.10.2026 Release', 'WEB 13.10.2026 Release'], issues=[
        {'key': 'OPS-115', 'summary': 'cart bundle total calculation returns 500', 'status': 'Live', 'inRelease': True},
        {'key': 'OPS-106', 'status': 'Ready For Live', 'inRelease': False},
        {'key': 'OPS-114', 'summary': 'reports-service limit log', 'status': 'Live', 'inRelease': True},
    ])
    body['rows'][0]['issues'] = ['OPS-115', 'OPS-106']
    body['rows'][1]['issuesNote'] = 'no local clone'

    # when
    client.post('/api/deploy-reports', json=body, headers=AUTH)
    saved = client.get('/api/deploy-reports').json()[0]

    # then
    assert saved['releases'] == ['OPS 13.10.2026 Release', 'WEB 13.10.2026 Release']
    assert saved['rows'][0]['issues'] == ['OPS-115', 'OPS-106']
    assert saved['rows'][1]['issues'] == []
    assert saved['rows'][1]['issuesNote'] == 'no local clone'
    assert [i['key'] for i in saved['issues']] == ['OPS-115', 'OPS-106', 'OPS-114']
    assert saved['issues'][1]['inRelease'] is False


def test_issue_keys_must_look_like_jira_keys(client: TestClient):
    # given
    body = report()
    body['rows'][0]['issues'] = ['ops 2793']

    # when
    res = client.post('/api/deploy-reports', json=body, headers=AUTH)
    bad_issue = client.post('/api/deploy-reports', json=report(issues=[{'key': 'OPS-'}]), headers=AUTH)

    # then
    assert res.status_code == 422
    assert bad_issue.status_code == 422
    assert client.get('/api/deploy-reports').json() == []


def test_a_report_replaces_older_ones_and_takes_the_button_link(client: TestClient):
    # given
    action = client.post('/api/actions', json={'type': 'postdeploy', 'key': 'postdeploy'}, headers=AUTH).json()
    first = client.post('/api/deploy-reports', json=report('first', actionId=action['id']), headers=AUTH).json()
    second = client.post('/api/deploy-reports', json=report('second'), headers=AUTH).json()

    # when
    res = client.post('/api/deploy-reports', json=report('third', replaces=[first['id'], second['id']]), headers=AUTH)

    # then
    assert res.status_code == 201
    listed = client.get('/api/deploy-reports').json()
    assert [r['summary'] for r in listed] == ['third']
    assert listed[0]['actionId'] == action['id']
    assert listed[0]['requestedBy'] == 'token:bootstrap'
    assert 'replaces' not in listed[0]
    assert client.get('/api/actions').json()[0]['reportId'] == res.json()['id']


def test_replacing_an_unknown_report_saves_nothing(client: TestClient):
    # given
    kept = client.post('/api/deploy-reports', json=report('kept'), headers=AUTH).json()

    # when
    res = client.post('/api/deploy-reports', json=report('new', replaces=[kept['id'], 'nope']), headers=AUTH)

    # then
    assert res.status_code == 404
    assert [r['summary'] for r in client.get('/api/deploy-reports').json()] == ['kept']


def test_deleting_a_report_drops_the_button_link(client: TestClient):
    # given
    action = client.post('/api/actions', json={'type': 'postdeploy', 'key': 'postdeploy'}, headers=AUTH).json()
    old = client.post('/api/deploy-reports', json=report(actionId=action['id']), headers=AUTH).json()

    # when
    anonymous = client.delete(f'/api/deploy-reports/{old["id"]}')
    res = client.delete(f'/api/deploy-reports/{old["id"]}', headers=AUTH)
    again = client.delete(f'/api/deploy-reports/{old["id"]}', headers=AUTH)

    # then
    assert anonymous.status_code == 401
    assert res.status_code == 204
    assert again.status_code == 404
    assert client.get('/api/deploy-reports').json() == []
    assert 'reportId' not in client.get('/api/actions').json()[0]


def test_a_saved_report_gets_its_summary_later(client: TestClient):
    # given
    flagged = client.post('/api/deploy-reports', json=report('flagged'), headers=AUTH).json()
    clean = client.post('/api/deploy-reports', json=report('clean', rows=[{'svc': 'checkout-service', 'flags': []}]), headers=AUTH).json()

    # when
    anonymous = client.put(f'/api/deploy-reports/{flagged["id"]}/brief', json={'headline': 'h'})
    res = client.put(f'/api/deploy-reports/{flagged["id"]}/brief', json={'headline': 'Release 05:05–05:35: 2 services; orders-service restarted 3 times', 'points': ['healthy since 05:14']}, headers=AUTH)
    plain = client.put(f'/api/deploy-reports/{clean["id"]}/brief', json={'headline': 'all clean'}, headers=AUTH)
    told = client.put(f'/api/deploy-reports/{clean["id"]}/brief', json={'headline': 'all clean', 'level': 'info'}, headers=AUTH)
    missing = client.put('/api/deploy-reports/nope/brief', json={'headline': 'h'}, headers=AUTH)
    blank = client.put(f'/api/deploy-reports/{clean["id"]}/brief', json={'headline': ''}, headers=AUTH)
    too_many = client.put(f'/api/deploy-reports/{clean["id"]}/brief', json={'headline': 'h', 'points': ['p'] * 11}, headers=AUTH)

    # then
    assert anonymous.status_code == 401
    assert res.status_code == 200
    assert res.json()['brief']['level'] == 'warn'
    assert res.json()['brief']['at']
    assert plain.json()['brief']['level'] == 'ok'
    assert told.json()['brief']['level'] == 'info'
    assert missing.status_code == 404
    assert blank.status_code == 422
    assert too_many.status_code == 422
    listed = {r['summary']: r for r in client.get('/api/deploy-reports').json()}
    assert listed['flagged']['brief'] == res.json()['brief']
    assert listed['flagged']['brief']['points'] == ['healthy since 05:14']
    assert listed['clean']['brief']['level'] == 'info'


def test_report_rows_keep_their_signal_links_and_cluster(client: TestClient):
    # given
    prom = 'https://prometheus.secondary.example.com/query?g0.expr=sum%28x%29&g0.tab=graph'
    kibana = "https://logging.prod.example.com/app/discover#/?_g=(time:(from:'2026-10-13T05:03:00Z',to:now))"
    row = {'ns': 'reports', 'svc': 'reports-service', 'cluster': 'secondary', 'links': {'req': prom, 'err': kibana}}

    # when
    res = client.post('/api/deploy-reports', json=report(rows=[row]), headers=AUTH)
    plain_http = client.post('/api/deploy-reports', json=report(rows=[{'svc': 's', 'links': {'req': 'http://x'}}]), headers=AUTH)
    bad_name = client.post('/api/deploy-reports', json=report(rows=[{'svc': 's', 'links': {'Req Rate': prom}}]), headers=AUTH)
    long_cluster = client.post('/api/deploy-reports', json=report(rows=[{'svc': 's', 'cluster': 'c' * 101}]), headers=AUTH)

    # then
    assert res.status_code == 201
    saved = client.get('/api/deploy-reports').json()[0]['rows'][0]
    assert saved['links'] == {'req': prom, 'err': kibana}
    assert saved['cluster'] == 'secondary'
    assert [plain_http.status_code, bad_name.status_code, long_cluster.status_code] == [422, 422, 422]


def test_new_reports_save_missing_metric_links(linked: TestClient, store: Store):
    row = {'ns': 'identity', 'svc': 'auth-service', 'metrics': 'warn 99 (45/51),req 8614 (6030/8589),kafka-lag 3 (2/1)'}

    response = linked.post('/api/deploy-reports', json=report(rows=[row]), headers=AUTH)

    assert response.status_code == 201
    saved = store.deploy_reports.find_one({'_id': response.json()['id']})['rows'][0]
    links = saved['links']
    req = parse_qs(urlsplit(links['req']).query)
    lag = parse_qs(urlsplit(links['kafka-lag']).query)
    assert urlsplit(links['req']).netloc == 'prometheus.example.com'
    assert req['g0.expr'] == ['sum(increase(http_server_requests_seconds_count{namespace="identity",job="auth-service"}[1740s]))']
    assert lag['g0.expr'] == ['max(max_over_time(kafka_consumer_fetch_manager_records_lag_max{namespace="identity",job="auth-service"}[1740s]))']
    assert req['g0.end_input'] == ['2026-10-13 05:50:00']
    assert req['g0.range_input'] == ['74m']
    assert req['g0.tab'] == ['graph']
    assert saved['metrics'] == row['metrics']


@pytest.mark.parametrize('namespace,cluster', [('custom-prod', 'secondary')])
def test_old_reports_gain_links_without_replacing_history(linked: TestClient, store: Store, namespace: str, cluster: str | None):
    stored_req = 'https://prometheus.example.com/query?g0.expr=original'
    row = {'ns': namespace, 'svc': 'reports-service', 'at': '2026-10-13T05:00:00Z',
           'metrics': 'req 10 (9/8), kafka-lag 3 (2/1)', 'flags': ['restarts=1'], 'links': {'req': stored_req}}
    if cluster:
        row['cluster'] = cluster
    original = {'_id': 'old-report', 'createdAt': '2026-10-13T05:40:00Z', **report(rows=[row]),
                'slackPost': {'ts': '123.456', 'channel': 'ops'}, 'brief': {'headline': 'Original assessment'}, 'actionId': 'original-action'}
    store.deploy_reports.insert_one(original)

    listed = linked.get('/api/deploy-reports').json()[0]
    single = store.deploy_report('old-report')

    assert listed == single
    assert listed['id'] == 'old-report'
    for key in ('createdAt', 'slackPost', 'brief', 'actionId'):
        assert listed[key] == original[key]
    filled = listed['rows'][0]
    assert filled['links']['req'] == stored_req
    assert filled['cluster'] == cluster
    for name in ('kafka-lag', 'restarts'):
        url = urlsplit(filled['links'][name])
        assert url.netloc == 'prometheus.secondary.example.com'
        assert parse_qs(url.query)['g0.range_input'] == ['80m']
        assert f'namespace="{namespace}"' in parse_qs(url.query)['g0.expr'][0]
    assert store.deploy_reports.find_one({'_id': 'old-report'}) == original, 'reading historical reports does not rewrite them'


@pytest.mark.parametrize('changes', [
    {'windowFrom': None}, {'windowTo': None}, {'windowFrom': 'invalid'},
    {'windowTo': '2026-10-13T04:00:00Z'}, {'windowFrom': '2026-10-13T05:06:00'},
    {'rows': [{'svc': 'unknown', 'metrics': 'req 10 (9/8)'}]},
])
def test_missing_report_context_does_not_invent_metric_links(linked: TestClient, store: Store, changes: dict):
    store.deploy_reports.insert_one({'_id': 'incomplete', **report(**changes)})

    response = linked.get('/api/deploy-reports')

    assert response.status_code == 200
    assert all(not row.get('links') for row in response.json()[0]['rows'])
