import datetime as dt

from fastapi.testclient import TestClient

from tests.conftest import AUTH


def ago(days: float) -> str:
    return (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days)).strftime('%Y-%m-%dT%H:%M:%SZ')


def deploy(svc: str = 'checkout-service', to: str = '2b2b2b2b', at: str | None = None, **extra) -> dict:
    return {'ns': 'shop', 'svc': svc, 'fromImage': '1a1a1a1a', 'toImage': to, 'at': at or ago(1), **extra}


def test_posted_deploys_list_newest_first(client: TestClient):
    # given
    body = [
        deploy('checkout-service', at=ago(2), tickets=['OPS-115', 'OPS-106'], imageBy={'names': ['Nina'], 'title': 'OPS-115 bundle calc', 'sha': '2b2b2b2b', 'url': 'https://gitlab.com/x/-/commit/2b2b2b2b'}),
        deploy('search-service', to='7a7a7a7a', at=ago(1), ticketsNote='no local clone'),
    ]

    # when
    res = client.post('/api/deploys', json=body, headers=AUTH)
    listed = client.get('/api/deploys').json()

    # then
    assert res.status_code == 200
    assert res.json() == {'received': 2, 'inserted': 2, 'existing': 0}
    assert [d['svc'] for d in listed] == ['search-service', 'checkout-service']
    assert listed[1]['tickets'] == ['OPS-115', 'OPS-106']
    assert listed[1]['imageBy']['names'] == ['Nina']
    assert listed[1]['fromImage'] == '1a1a1a1a'
    assert listed[0]['ticketsNote'] == 'no local clone'
    assert all(d['id'] and d['createdAt'] for d in listed)


def test_the_services_own_image_changes_are_ignored(client: TestClient):
    # when
    res = client.post('/api/deploys', json=[deploy(), dict(deploy('skopax', to='1a2b3c4d'), ns='platform')], headers=AUTH)
    listed = client.get('/api/deploys').json()

    # then
    assert res.json() == {'received': 2, 'inserted': 1, 'existing': 0, 'ignored': 1}
    assert [d['svc'] for d in listed] == ['checkout-service']


def test_reposting_the_same_image_change_adds_nothing(client: TestClient):
    # given
    first_at = ago(1)
    client.post('/api/deploys', json=[deploy(at=first_at)], headers=AUTH)

    # when
    again = client.post('/api/deploys', json=[deploy(at=ago(0.9)), deploy(to='aaaa1111')], headers=AUTH)
    listed = client.get('/api/deploys').json()

    # then
    assert again.json() == {'received': 2, 'inserted': 1, 'existing': 1}
    assert sorted(d['toImage'] for d in listed) == ['2b2b2b2b', 'aaaa1111']
    assert [d['at'] for d in listed if d['toImage'] == '2b2b2b2b'] == [first_at]


def test_a_repost_keeps_the_earliest_time_and_fills_what_was_missing(client: TestClient):
    # given
    later, earlier = ago(1), ago(1.01)
    client.post('/api/deploys', json=[deploy(at=later, ticketsNote='image commits not in the clone')], headers=AUTH)

    # when
    client.post('/api/deploys', json=[deploy(at=earlier, fromImage='ffff0000', tickets=['OPS-115'], releases=['OPS 13.10.2026 Release'])], headers=AUTH)
    client.post('/api/deploys', json=[deploy(at=later)], headers=AUTH)
    saved = client.get('/api/deploys').json()

    # then
    assert len(saved) == 1
    assert saved[0]['at'] == earlier
    assert saved[0]['fromImage'] == '1a1a1a1a'
    assert saved[0]['tickets'] == ['OPS-115']
    assert saved[0]['releases'] == ['OPS 13.10.2026 Release']
    assert 'ticketsNote' not in saved[0]


def test_a_repost_replaces_the_merge_author_with_every_changer_of_the_range(client: TestClient):
    # given
    merge_only = {'names': ['Oscar'], 'title': 'Merge OPS-115', 'sha': '2b2b2b2b', 'url': 'https://gitlab.com/x/-/commit/2b2b2b2b'}
    client.post('/api/deploys', json=[deploy(imageBy=merge_only)], headers=AUTH)
    changers = dict(merge_only, names=['Gina', 'Ben', 'Oscar'], commits=7, compare='https://gitlab.com/x/-/compare/1a1a1a1a...2b2b2b2b')

    # when
    res = client.post('/api/deploys', json=[deploy(imageBy=changers)], headers=AUTH)
    saved = client.get('/api/deploys').json()

    # then
    assert res.json() == {'received': 1, 'inserted': 0, 'existing': 1}
    assert saved[0]['imageBy']['names'] == ['Gina', 'Ben', 'Oscar']
    assert saved[0]['imageBy']['commits'] == 7
    assert saved[0]['imageBy']['compare'] == 'https://gitlab.com/x/-/compare/1a1a1a1a...2b2b2b2b'


def test_the_same_tag_in_another_service_is_its_own_deploy(client: TestClient):
    # when
    res = client.post('/api/deploys', json=[deploy('batch-service', to='8b8b8b8b', ns='identity'), deploy('batch-service', to='8b8b8b8b', ns='members')], headers=AUTH)

    # then
    assert res.json()['inserted'] == 2


def test_days_limits_the_list(client: TestClient):
    # given
    client.post('/api/deploys', json=[deploy(to='old00001', at=ago(40)), deploy(to='new00001', at=ago(3))], headers=AUTH)

    # when
    default = client.get('/api/deploys').json()
    wide = client.get('/api/deploys', params={'days': 60}).json()
    narrow = client.get('/api/deploys', params={'days': 2}).json()

    # then
    assert [d['toImage'] for d in default] == ['new00001']
    assert [d['toImage'] for d in wide] == ['new00001', 'old00001']
    assert narrow == []
    assert client.get('/api/deploys', params={'days': 0}).status_code == 422


def test_times_are_stored_as_utc(client: TestClient):
    # when
    client.post('/api/deploys', json=[deploy(at='2026-10-13T06:05:00+01:00')], headers=AUTH)

    # then
    assert client.get('/api/deploys', params={'days': 3650}).json()[0]['at'] == '2026-10-13T05:05:00Z'


def test_posting_deploys_needs_the_token(client: TestClient):
    # when
    res = client.post('/api/deploys', json=[deploy()])

    # then
    assert res.status_code == 401
    assert client.get('/api/deploys').json() == []


def test_bad_deploys_are_rejected_whole(client: TestClient):
    # when
    bad_key = client.post('/api/deploys', json=[deploy(), deploy(to='x2', tickets=['ops 115'])], headers=AUTH)
    no_image = client.post('/api/deploys', json=[{k: v for k, v in deploy().items() if k != 'toImage'}], headers=AUTH)
    no_zone = client.post('/api/deploys', json=[deploy(at='2026-10-13T05:05:00')], headers=AUTH)
    not_a_list = client.post('/api/deploys', json=deploy(), headers=AUTH)

    # then
    assert [r.status_code for r in (bad_key, no_image, no_zone, not_a_list)] == [422, 422, 422, 422]
    assert client.get('/api/deploys').json() == []


def test_a_deploy_is_deleted_only_with_the_token(client: TestClient):
    # given
    client.post('/api/deploys', json=[deploy(), deploy('search-service', to='7a7a7a7a')], headers=AUTH)
    wrong = next(d for d in client.get('/api/deploys').json() if d['svc'] == 'checkout-service')

    # when
    anonymous = client.delete(f'/api/deploys/{wrong["id"]}')
    res = client.delete(f'/api/deploys/{wrong["id"]}', headers=AUTH)
    again = client.delete(f'/api/deploys/{wrong["id"]}', headers=AUTH)
    left = client.get('/api/deploys').json()
    reposted = client.post('/api/deploys', json=[deploy()], headers=AUTH)

    # then
    assert anonymous.status_code == 401
    assert res.status_code == 204
    assert again.status_code == 404
    assert [d['svc'] for d in left] == ['search-service']
    assert reposted.json() == {'received': 1, 'inserted': 1, 'existing': 0}


def test_deploys_of_services_on_the_check_ignore_list_come_back_marked(client: TestClient):
    # given
    client.put('/api/postdeploy-ignore', json={'services': ['release-board']}, headers=AUTH)
    client.post('/api/deploys', json=[deploy(), dict(deploy('release-board', to='69788796'), ns='platform')], headers=AUTH)

    # when
    listed = {d['svc']: d for d in client.get('/api/deploys').json()}

    # then
    assert listed['release-board']['ignored'] is True and 'ignored' not in listed['checkout-service'], 'the Releases tab still lists it; the page skips it for the pending-report banner'
