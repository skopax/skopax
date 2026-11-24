import dataclasses
import email
import logging
import smtplib

import pytest
from fastapi.testclient import TestClient

from skopax.app import create_app
from skopax.instances import Instances
from skopax.plugins import mail_server
from skopax.messages import msg
from skopax.sessions import Sessions, session_secret
from skopax.auth import SESSION_COOKIE
from skopax.users import Users
from tests.conftest import ADMIN_EMAIL, AUTH, settings
from tests.mail_kit import CONFIG, PASSWORD, FakeSMTP, faked
from tests.test_auth import sso_settings
from tests.test_login import post_login, with_password

PATH = '/api/admin/plugins/mail-server/instances/mail/test-mail'
MASKED = 'a***@example.com'
NOW = 1_800_000_000.0


class Clock:
    def __init__(self, now: float = NOW):
        self.now = now

    def __call__(self) -> float:
        return self.now


def served(store, clock: Clock | None = None) -> TestClient:
    return TestClient(create_app(settings(), store, clock=clock), base_url='https://testserver', follow_redirects=False)


def ready(client, enabled: bool = True, values: dict | None = None):
    made = client.post('/api/admin/plugins/mail-server/instances', json={'name': 'mail', 'values': dict(CONFIG) if values is None else values, 'enabled': enabled}, headers=AUTH)
    assert made.status_code == 201, made.text
    return made


def signed(client, store) -> None:
    with_password(store)
    assert post_login(client).status_code == 303


def sent(smtp) -> list[email.message.EmailMessage]:
    return [m for box in smtp.made for m, _, _ in box.sent]


def body(message, subtype: str) -> str:
    return next(p for p in message.walk() if p.get_content_type() == f'text/{subtype}').get_content()


def texts(store) -> list[str]:
    return [d['text'] for d in store.activity.find()]


def test_an_admin_sends_one_test_mail_to_their_own_address_and_hears_the_servers_answer(store, monkeypatch):
    # given
    smtp = faked(monkeypatch)
    with served(store) as client:
        ready(client)
        signed(client, store)

        # when
        answer = client.post(PATH)

    # then
    assert answer.status_code == 200, answer.text
    out = answer.json()
    [message] = sent(smtp)
    [(_, _, to)] = [s for box in smtp.made for s in box.sent]
    assert to == [ADMIN_EMAIL] and message['To'] == ADMIN_EMAIL
    assert message['Subject'] == '[Skopax] Test mail' and message['X-Skopax-Kind'] == 'test'
    assert out == {'ok': True, 'text': msg('mailserver.test_sent', address=MASKED, reply='250 2.0.0 OK queued as 4ABC')}
    assert MASKED in out['text'] and '250 2.0.0 OK queued as 4ABC' in out['text'] and 'junk' in out['text']
    plain, html = body(message, 'plain'), body(message, 'html')
    assert msg('mail.test.line') in plain
    assert msg('mail.test.instance', instance='mail') in plain and msg('mail.test.instance', instance='mail') in html
    assert msg('mail.footer') in plain and 'max-width:600px' in html
    assert msg('mail.status') not in plain and msg('mail.notice.why_nolink') not in plain and 'Preferences' not in html


def test_the_test_mail_says_when_it_was_sent(store, monkeypatch):
    # given
    smtp = faked(monkeypatch)
    with served(store, Clock(1_800_000_000.0)) as client:
        ready(client)
        signed(client, store)

        # when
        client.post(PATH)

    # then
    [message] = sent(smtp)
    assert msg('mail.test.at', at='2027-01-15 08:00 UTC') in body(message, 'plain')


def test_a_refused_sender_answers_not_ok_with_the_servers_reason(store, monkeypatch):
    # given
    faked(monkeypatch, send=smtplib.SMTPSenderRefused(550, b'5.7.60 Client does not have permissions to send as this sender', 'alerts@example.com'))
    with served(store) as client:
        ready(client)
        signed(client, store)

        # when
        answer = client.post(PATH)

    # then
    assert answer.status_code == 200
    assert answer.json() == {'ok': False, 'text': msg('mailserver.refused', reply='550 5.7.60 Client does not have permissions to send as this sender')}
    assert msg('integrations.test_mail_failed', instance='mail') in texts(store)


def test_a_mail_server_that_is_down_answers_not_ok(store, monkeypatch):
    # given
    faked(monkeypatch, connect=ConnectionRefusedError())
    with served(store) as client:
        ready(client)
        signed(client, store)

        # when
        answer = client.post(PATH)

    # then
    assert answer.status_code == 200
    assert answer.json() == {'ok': False, 'text': msg('mailserver.connect', host='smtp.example.com', port=587, reason='connection refused')}


@pytest.mark.parametrize('role', ['viewer', 'operator'])
def test_an_operator_or_viewer_is_refused(store, monkeypatch, role):
    # given
    smtp = faked(monkeypatch)
    with served(store) as client:
        ready(client)
        Users(store.db).create('bob@example.com', 'Bob', role)
        sealer = Sessions(session_secret(store.db, sso_settings()), 3600, 7200)
        client.cookies.set(SESSION_COOKIE, sealer.issue('bob@example.com', role, 'local'))

        # when
        answer = client.post(PATH)

    # then
    assert answer.status_code == 403 and sent(smtp) == []


def test_a_body_naming_another_recipient_is_refused(store, monkeypatch):
    # given
    smtp = faked(monkeypatch)
    with served(store) as client:
        ready(client)
        signed(client, store)

        # when
        named = client.post(PATH, json={'to': 'mallory@example.com'})
        empty = client.post(PATH, json={})

    # then
    assert named.status_code == 422
    assert empty.status_code == 200 and [m['To'] for m in sent(smtp)] == [ADMIN_EMAIL]


def test_an_unknown_instance_is_not_found_and_a_switched_off_one_is_a_conflict(store, monkeypatch):
    # given
    smtp = faked(monkeypatch)
    with served(store) as client:
        ready(client, enabled=False)
        signed(client, store)

        # when
        unknown = client.post('/api/admin/plugins/mail-server/instances/other/test-mail')
        off = client.post(PATH)

    # then
    assert unknown.status_code == 404 and unknown.json()['detail'] == msg('instances.unknown', name='other')
    assert off.status_code == 409 and off.json()['detail'] == msg('mailserver.test_off', instance='mail')
    assert sent(smtp) == []


def test_the_bootstrap_token_has_no_person_and_is_refused_plainly(store, monkeypatch):
    # given
    smtp = faked(monkeypatch)
    with served(store) as client:
        ready(client)

        # when
        answer = client.post(PATH, headers=AUTH)

    # then
    assert answer.status_code == 409 and answer.json()['detail'] == msg('mailserver.test_no_person')
    assert sent(smtp) == []


def test_a_personal_admin_token_mails_its_owner(store, monkeypatch):
    # given
    smtp = faked(monkeypatch)
    with served(store) as client:
        ready(client)
        signed(client, store)
        made = client.post('/api/admin/tokens', json={'name': 'ci', 'role': 'admin'})
        client.cookies.clear()

        # when
        answer = client.post(PATH, headers={'Authorization': f"Bearer {made.json()['token']}"})

    # then
    assert made.status_code == 201 and answer.status_code == 200 and answer.json()['ok'] is True
    assert [m['To'] for m in sent(smtp)] == [ADMIN_EMAIL]


def test_the_sixth_test_mail_in_an_hour_is_refused_and_not_sent(store, monkeypatch):
    # given
    smtp = faked(monkeypatch)
    clock = Clock()
    with served(store, clock) as client:
        ready(client)
        signed(client, store)
        first = [client.post(PATH).status_code for _ in range(5)]

        # when
        clock.now += 3000
        over = client.post(PATH)
        clock.now += 700
        later = client.post(PATH)

    # then
    assert first == [200] * 5
    assert over.status_code == 429 and over.json()['detail'] == msg('mailserver.test_limit', max=5)
    assert later.status_code == 200
    assert len(sent(smtp)) == 6


def test_a_failed_test_mail_counts_toward_the_hour(store, monkeypatch):
    # given
    faked(monkeypatch, connect=ConnectionRefusedError())
    with served(store) as client:
        ready(client)
        signed(client, store)

        # when
        codes = [client.post(PATH).status_code for _ in range(6)]

    # then
    assert codes == [200] * 5 + [429]


def test_the_activity_line_names_the_instance_and_never_the_address(store, monkeypatch):
    # given
    faked(monkeypatch)
    with served(store) as client:
        ready(client)
        signed(client, store)

        # when
        client.post(PATH)

    # then
    rows = list(store.activity.find())
    line = next(r for r in rows if r['text'] == msg('integrations.test_mail_sent', instance='mail'))
    assert line['who'] == ADMIN_EMAIL
    assert [r['text'] for r in rows if 'Test mail' in r['text'] and '@' in r['text']] == []


def test_a_refusal_that_echoes_the_address_is_masked_in_the_answer(store, monkeypatch, caplog):
    # given
    faked(monkeypatch, send=smtplib.SMTPRecipientsRefused({ADMIN_EMAIL: (550, f'5.1.1 {ADMIN_EMAIL.upper()} unknown'.encode())}))
    with served(store) as client:
        ready(client)
        signed(client, store)

        # when
        with caplog.at_level(logging.DEBUG):
            answer = client.post(PATH).json()

    # then
    lines = [r.getMessage() for r in caplog.records]
    assert answer['ok'] is False and ADMIN_EMAIL not in answer['text'].lower() and MASKED in answer['text']
    assert any(MASKED in line for line in lines)
    assert not any(ADMIN_EMAIL in line.lower() for line in lines)


def test_a_success_reply_is_cleaned_of_the_password_and_control_characters(store, monkeypatch):
    # given
    faked(monkeypatch)
    monkeypatch.setattr(FakeSMTP, 'reply', (250, f'2.0.0 OK {PASSWORD}\x07 queued'.encode()))
    with served(store) as client:
        ready(client)
        signed(client, store)

        # when
        text = client.post(PATH).json()['text']

    # then
    assert PASSWORD not in text and '\x07' not in text
    assert '(250 2.0.0 OK *** queued)' in text


class Quiet(FakeSMTP):
    def send_message(self, message, from_addr=None, to_addrs=None):
        self.sent.append((message, from_addr, to_addrs))
        self.step('send')
        return {}


def test_without_a_data_reply_the_answer_claims_no_code(store, monkeypatch):
    # given
    faked(monkeypatch)
    monkeypatch.setattr(smtplib, 'SMTP', Quiet)
    with served(store) as client:
        ready(client)
        signed(client, store)

        # when
        answer = client.post(PATH).json()

    # then
    assert answer == {'ok': True, 'text': msg('mailserver.test_sent_plain', address=MASKED)}
    assert '250' not in answer['text'] and 'the mail server accepted it' in answer['text']


def test_an_instance_with_a_problem_says_its_problem(store, monkeypatch):
    # given
    smtp = faked(monkeypatch)
    real = Instances.fresh
    monkeypatch.setattr(Instances, 'fresh', lambda self, plugin, name: (lambda got: got and dataclasses.replace(got, problem='The saved password could not be read'))(real(self, plugin, name)))
    with served(store) as client:
        ready(client)
        signed(client, store)

        # when
        answer = client.post(PATH)

    # then
    assert answer.status_code == 409 and answer.json()['detail'] == 'The saved password could not be read'
    assert sent(smtp) == []


def test_a_switched_off_mail_server_plugin_is_a_conflict(store, monkeypatch):
    # given
    smtp = faked(monkeypatch)
    with served(store) as client:
        ready(client)
        off = client.put('/api/admin/plugins/mail-server', json={'enabled': False}, headers=AUTH)
        signed(client, store)

        # when
        answer = client.post(PATH)

    # then
    assert off.status_code == 200
    assert answer.status_code == 409 and answer.json()['detail'] == msg('mailserver.test_off', instance='mail')
    assert sent(smtp) == []


def test_the_setup_guide_ends_with_send_test_mail():
    # when
    steps = mail_server.PLUGIN.setup.steps

    # then
    assert 'Send test mail' in steps[-1] and 'Test connection' in steps[-2]


def test_the_test_mail_sees_a_plug_in_switched_off_on_another_pod_at_once(store, monkeypatch):
    # given
    faked(monkeypatch)
    with served(store) as first, served(store) as second:
        ready(first)
        signed(second, store)
        assert second.post(PATH).status_code == 200

        # when
        first.put('/api/admin/plugins/mail-server', json={'enabled': False}, headers=AUTH)
        answer = second.post(PATH)

        # then
        assert answer.status_code == 409
