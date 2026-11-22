import datetime as dt

import pytest

from tests.conftest import AUTH, anomaly, state
from tests.fake_slack import FakeSlackApi
from tests.test_slack import client_with
from tests.test_slack_links import Transport, link, post_of
from skopax.store import Store, iso, utc_now

PARENT = 'kafka-consume-err|members/email-service|in'
CHILD = 'log|email-service|ERROR|Mailer email api exception occurred'
ALERT = 'alert|AppHighErrorLogCount|namespace=members'
ELSEWHERE = 'err|identity/auth-service'


def seed(store: Store, **parent) -> Store:
    store.save(state(anomaly(PARENT, ns='members', svc='email-service', **parent),
                     anomaly(CHILD, sev='crit', ns='-', svc='email-service', parent=PARENT),
                     anomaly(ALERT, ns='members', svc='-', src='alert'),
                     anomaly(ELSEWHERE, ns='identity', svc='auth-service')), None)
    return store


def send(client, key: str) -> dict:
    return client.post('/api/slack-posts', json={'key': key}, headers=AUTH).json()


def test_a_child_and_an_alert_of_the_same_namespace_go_into_the_first_posts_thread_and_another_service_gets_its_own_post(store: Store):
    # given
    seed(store)
    t = Transport()
    with client_with(store, t, api=FakeSlackApi()) as client:

        # when
        send(client, PARENT)
        child = send(client, CHILD)
        alert = send(client, ALERT)
        send(client, ELSEWHERE)
        posts = {k: post_of(client, k) for k in (PARENT, CHILD, ALERT, ELSEWHERE)}

    # then
    root = '1791900000.000001'
    assert [c.get('thread_ts') for c in t.calls] == [None, root, root, None]
    assert {k: posts[k]['ts'] for k in posts} == {PARENT: root, CHILD: root, ALERT: root, ELSEWHERE: '1791900000.000004'}
    assert (posts[CHILD]['replyTs'], posts[CHILD]['threadOf'], posts[ALERT]['replyTs'], posts[ALERT]['threadOf']) == ('1791900000.000002', PARENT, '1791900000.000003', PARENT)
    assert 'threadOf' not in posts[PARENT] and 'threadOf' not in posts[ELSEWHERE]
    assert posts[CHILD]['permalink'] == link('1791900000.000002')
    assert child['result'].startswith(f'sent to the thread of {PARENT} in ') and alert['result'].startswith(f'sent to the thread of {PARENT} in ')
    assert store.anomaly_by_post('C123', root)['key'] == PARENT


@pytest.mark.parametrize('setup', ['handled', 'old', 'off'])
def test_no_thread_under_a_handled_card_a_post_older_than_the_window_or_with_threading_off(store: Store, setup):
    # given
    seed(store, **({'triage': 'handled'} if setup == 'handled' else {}))
    t = Transport()
    with client_with(store, t, api=FakeSlackApi(), **({'slack_thread_hours': 0} if setup == 'off' else {})) as client:
        send(client, PARENT)
        if setup == 'old':
            post = store.anomaly(PARENT)['slackPost']
            store.slack_posted(PARENT, post | {'at': iso(utc_now() - dt.timedelta(hours=7))})

        # when
        send(client, CHILD)

    # then
    assert [c.get('thread_ts') for c in t.calls] == [None, None]
    assert 'threadOf' not in store.anomaly(CHILD)['slackPost']


def test_sending_a_posted_card_again_stays_where_it_was_first_sent(store: Store):
    # given
    seed(store)
    t = Transport()
    with client_with(store, t, api=FakeSlackApi()) as client:
        send(client, ELSEWHERE)
        send(client, PARENT)

        # when
        send(client, PARENT)

    # then
    assert [c.get('thread_ts') for c in t.calls] == [None, None, None]


def placed_lines(message: dict) -> list[str]:
    return [e['text'] for b in message.get('blocks') or [] if b.get('type') == 'context' for e in b.get('elements') or []
            if e.get('text', '').startswith(('Grouped under ', 'Same service as ', 'Same namespace as '))]


def test_a_card_posted_into_another_cards_thread_says_why_it_is_there_and_keeps_it_when_redrawn(store: Store):
    # given
    seed(store)
    t, api = Transport(), FakeSlackApi()
    with client_with(store, t, api=api) as client:
        send(client, PARENT)

        # when
        send(client, CHILD)
        send(client, ALERT)
        send(client, ELSEWHERE)
        client.app.state.card_posts.redraw()

    # then
    host = store.anomaly(PARENT)
    name = (f"#{host['num']} " if host.get('num') else '') + f"{host['svc']} / {host['label']}"
    assert [placed_lines(c) for c in t.calls] == [[], [f'Grouped under {name}'], [f'Same namespace as {name}'], []]
    redrawn = {p['ts']: placed_lines(p) for m, p in api.calls if m == 'chat.update'}
    assert redrawn['1791900000.000002'] == [f'Grouped under {name}'] and redrawn['1791900000.000003'] == [f'Same namespace as {name}']
    assert redrawn['1791900000.000001'] == [] and redrawn['1791900000.000004'] == []
    assert store.anomaly(ALERT)['slackPost']['threadWhy'] == 'ns'
