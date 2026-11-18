import datetime as dt

from fastapi.testclient import TestClient

from tests.conftest import AUTH
from tests.test_deploys import ago, deploy
from skopax.settle import Settle
from skopax.store import DEPLOY_IMAGE, Store, utc_now


def test_a_rollback_to_an_image_deployed_before_is_a_new_deploy_that_settles_and_the_first_keeps_its_time(client: TestClient, store: Store):
    # given
    first_at, back_at = ago(3), ago(40 / 1440)
    client.post('/api/deploys', json=[deploy(to='1111bbbb', fromImage='0000aaaa', at=first_at)], headers=AUTH)
    client.post('/api/deploys', json=[deploy(to='2222cccc', fromImage='1111bbbb', at=ago(1))], headers=AUTH)

    # when
    rolled = client.post('/api/deploys', json=[deploy(to='1111bbbb', fromImage='2222cccc', at=back_at)], headers=AUTH).json()
    seen_again = client.post('/api/deploys', json=[deploy(to='1111bbbb', fromImage='2222cccc', at=back_at)], headers=AUTH).json()
    listed = client.get('/api/deploys?days=7').json()
    queued = Settle(store, True).run_once(utc_now())

    # then
    assert (rolled['inserted'], seen_again['existing']) == (1, 1)
    assert [(d['fromImage'], d['toImage']) for d in listed] == [('2222cccc', '1111bbbb'), ('1111bbbb', '2222cccc'), ('0000aaaa', '1111bbbb')]
    assert [d['at'] for d in listed if d['fromImage'] == '0000aaaa'] == [first_at]
    assert queued['release']['lastAt'] == back_at and queued['release']['services'] == ['checkout-service']


def test_the_unique_deploy_index_takes_the_rollback_number_and_the_old_one_is_dropped(store: Store):
    # given
    store.deploys.create_index(DEPLOY_IMAGE, unique=True)

    # when
    store.ensure_indexes()
    store.ensure_indexes()

    # then
    keys = [list(spec['key']) for spec in store.deploys.index_information().values() if spec.get('unique')]
    assert keys == [DEPLOY_IMAGE + [('seq', 1)]]


def test_the_same_change_seen_hours_later_or_without_its_old_image_is_still_one_deploy(store: Store):
    # given
    store.ensure_indexes()
    first = (utc_now() - dt.timedelta(days=1)).strftime('%Y-%m-%dT%H:%M:%SZ')
    store.add_deploys([deploy(to='1111bbbb', fromImage='0000aaaa', at=first)])

    # when
    later = store.add_deploys([deploy(to='1111bbbb', fromImage='0000aaaa', at=ago(0.5))])
    blind = store.add_deploys([deploy(to='1111bbbb', fromImage=None, at=ago(0.2))])

    # then
    assert (later['existing'], blind['existing']) == (1, 1)
    assert [(d['at'], d.get('seq')) for d in store.list_deploys(7)] == [(first, None)]
