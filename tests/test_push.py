import dataclasses
import logging
import time

from fastapi.testclient import TestClient

from tests.conftest import AUTH, settings
from tests.fake_slack import FakeSlackApi
from tests.test_engine import item
from tests.test_slack import FakeSlack
from skopax.app import create_app
from skopax.engine import iso
from skopax.push import Pusher
from skopax.slack import Slack
from skopax.store import Store

USER = 'U0123ABCD'
PAGE = 'https://skopax.example.com'


def app_with(store: Store, transport: FakeSlack, user: str = USER, **extra) -> TestClient:
    return TestClient(create_app(dataclasses.replace(settings(), slack_push_user=user, **extra), store, slack=Slack('xoxb-test', '#ops', transport, FakeSlackApi())))


def pushed(key: str, sev: str = 'crit', at: str | None = None, **extra) -> dict:
    at = at or iso(time.time() - 60)
    return {**item(key, sev=sev), 'status': 'open', 'firstSeen': at, 'lastSeen': at, 'pushedAt': at, 'pushedSev': sev, 'pushedPeak': 1, **extra}


def put(c: TestClient, *anomalies: dict, initialized: bool = True):
    return c.put('/api/state', json={'initialized': initialized, 'anomalies': list(anomalies), 'sweptIso': iso(time.time())}, headers=AUTH)


def test_a_new_push_from_the_local_check_is_sent_as_one_dm(store: Store):
    # given
    transport = FakeSlack()
    old = pushed('cf-health|origin', at=iso(time.time() - 7200))
    fresh = [pushed('log|checkout-service|ERROR|boom', short='CRIT checkout-service ERROR boom'), pushed('kafka-lag|x/y', sev='warn', short='WARN x lag <5>')]

    # when
    with app_with(store, transport) as c:
        put(c, old, initialized=False)
        put(c, old)
        first = len(transport.calls)
        put(c, old, *fresh)
        put(c, old, *fresh)

    # then
    assert first == 0
    assert len(transport.calls) == 1
    token, payload = transport.calls[0]
    assert token == 'xoxb-test'
    assert payload['channel'] == USER
    assert payload['text'] == f'Skopax: CRIT checkout-service ERROR boom; WARN x lag &lt;5&gt;\n<{PAGE}/#t=anoms|Open in Skopax>'
    assert 'attachments' not in payload


def test_no_dm_without_a_push_user_or_for_a_merged_child(store: Store):
    # given
    transport = FakeSlack()
    parent = pushed('log|a|ERROR|x')

    # when
    with app_with(store, transport, user='') as c:
        put(c)
        put(c, parent)
    with app_with(store, transport) as c:
        put(c, parent, pushed('log|child|ERROR|y', parent='log|a|ERROR|x'))

    # then
    assert transport.calls == []


def test_a_slack_failure_never_fails_the_state_write(store: Store):
    # given
    transport = FakeSlack('channel_not_found')

    # when
    with app_with(store, transport) as c:
        put(c)
        res = put(c, pushed('log|a|ERROR|x'))

    # then
    assert res.status_code == 200
    assert len(transport.calls) == 1


class FailsFor(FakeSlack):
    def __init__(self, *channels: str):
        super().__init__()
        self.channels = channels

    def __call__(self, token: str, payload: dict) -> dict:
        self.error = 'channel_not_found' if payload['channel'] in self.channels else None
        return super().__call__(token, payload)


def pusher(transport: FakeSlack, users: str) -> Pusher:
    return Pusher(Slack('xoxb-test', '#ops', transport), users, PAGE)


def test_a_single_push_user_gets_one_dm():
    # given
    transport = FakeSlack()

    # when
    line = pusher(transport, 'U1').send([item('log|checkout-service|ERROR|boom', sev='crit', short='CRIT checkout-service ERROR boom')])

    # then
    assert line == 'Skopax: CRIT checkout-service ERROR boom'
    assert [p['channel'] for _, p in transport.calls] == ['U1']
    assert transport.calls[0][1]['text'].startswith('Skopax: CRIT checkout-service ERROR boom\n')


def test_a_comma_separated_push_user_list_dms_every_member_the_same_text():
    # given
    transport = FakeSlack()
    p = pusher(transport, ' U1 , U2,, ')

    # when
    line = p.send([item('log|a|ERROR|x', sev='crit', short='CRIT a ERROR x')])

    # then
    assert p.enabled
    assert line == 'Skopax: CRIT a ERROR x'
    assert [c['channel'] for _, c in transport.calls] == ['U1', 'U2']
    assert transport.calls[0][1]['text'] == transport.calls[1][1]['text']


def test_a_failing_member_is_logged_and_the_others_still_get_the_dm(caplog):
    # given
    caplog.set_level(logging.INFO, logger='skopax.push')
    transport = FailsFor('U1')

    # when
    line = pusher(transport, 'U1,U2').send([item('log|a|ERROR|x', sev='crit', short='CRIT a ERROR x')])

    # then
    assert line == 'Skopax: CRIT a ERROR x'
    assert [c['channel'] for _, c in transport.calls] == ['U1', 'U2']
    assert [(r.levelname, r.getMessage()) for r in caplog.records] == [
        ('WARNING', 'Slack DM to U1 failed: SlackError'),
        ('INFO', 'Slack DM sent to U2 for log|a|ERROR|x'),
    ]


def test_no_line_is_returned_when_every_member_fails():
    # given
    transport = FailsFor('U1', 'U2')

    # when
    line = pusher(transport, 'U1,U2').send([item('log|a|ERROR|x', sev='crit')])

    # then
    assert line is None
    assert len(transport.calls) == 2


def test_an_empty_push_user_list_turns_the_push_off():
    # given
    transport = FakeSlack()

    # when
    pushers = [pusher(transport, users) for users in ('', ' ', ' , ,')]
    lines = [p.send([item('log|a|ERROR|x', sev='crit')]) for p in pushers]

    # then
    assert [p.enabled for p in pushers] == [False, False, False]
    assert lines == [None, None, None]
    assert transport.calls == []


def test_one_pushed_item_links_its_card():
    # given
    transport = FakeSlack()

    # when
    pusher(transport, 'U1').send([item('log|web-bff|WARN|Define logo for <BRAND>', short='WARN bff logo <BRAND>')])

    # then
    assert transport.calls[0][1]['text'] == f'Skopax: WARN bff logo &lt;BRAND&gt;\n<{PAGE}/#a=log%7Cweb-bff%7CWARN%7CDefine%20logo%20for%20%3CBRAND%3E|Open in Skopax> · <{PAGE}/#t=chat/new/log%7Cweb-bff%7CWARN%7CDefine%20logo%20for%20%3CBRAND%3E|Chat about this>'


def test_several_pushed_items_link_the_anomalies_tab():
    # given
    transport = FakeSlack()

    # when
    pusher(transport, 'U1').send([item('log|a|ERROR|x', sev='crit', short='CRIT a'), item('kafka-lag|x/y', short='WARN lag')])

    # then
    assert transport.calls[0][1]['text'] == f'Skopax: CRIT a; WARN lag\n<{PAGE}/#t=anoms|Open in Skopax>'


def test_the_service_dms_every_member_with_a_link_to_the_host_it_runs_on(store: Store):
    # given
    transport = FakeSlack()

    # when
    with app_with(store, transport, user='U1, U2', base_url='', sso_redirect_uri='https://skopax.dev.example.com/authorized') as c:
        put(c)
        put(c, pushed('log|a|ERROR|x', short='CRIT a ERROR x'))

    # then
    assert [p['channel'] for _, p in transport.calls] == ['U1', 'U2']
    assert transport.calls[0][1]['text'] == 'Skopax: CRIT a ERROR x\n<https://skopax.dev.example.com/#a=log%7Ca%7CERROR%7Cx|Open in Skopax> · <https://skopax.dev.example.com/#t=chat/new/log%7Ca%7CERROR%7Cx|Chat about this>'
