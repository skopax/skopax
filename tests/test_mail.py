import email
import email.policy
import logging
import smtplib
import socket
import ssl

import pytest

from skopax import brand, mail
from skopax.mail import MailDown, Mailer, MailRefused, card_mail
from skopax.messages import msg, speaking
from tests.mail_kit import CONFIG, PASSWORD, faked

CARD = {'key': 'log|checkout-service|ERROR', 'num': 7, 'svc': 'checkout-service', 'label': 'ERROR lines', 'sev': 'crit', 'status': 'open'}
LINK = 'https://skopax.example.com/c/7'


def parsed(fake) -> email.message.EmailMessage:
    message = fake.made[-1].sent[-1][0]
    return email.message_from_bytes(message.as_bytes(policy=email.policy.SMTP), policy=email.policy.default)


def test_starttls_send_signs_in_and_builds_a_plain_utf8_message(monkeypatch):
    # given
    fake = faked(monkeypatch)

    # when
    sent_id = Mailer(CONFIG).send(['alice@example.com'], 'Checkout \u00e9rrors', 'Caf\u00e9 is down', kind='route')

    # then
    session = fake.made[-1]
    assert (session.host, session.port, session.timeout, session.ssl) == ('smtp.example.com', 587, mail.TIMEOUT_S, False) and mail.TIMEOUT_S == 20
    assert session.steps == ['connect', 'ehlo', 'starttls', 'ehlo', 'login', 'send', 'quit']
    assert session.login_as == ('alerts@example.com', PASSWORD)
    assert isinstance(session.tls_context, ssl.SSLContext) and session.tls_context.verify_mode == ssl.CERT_REQUIRED and session.tls_context.check_hostname
    got = parsed(fake)
    assert got['Subject'] == 'Checkout \u00e9rrors' and got['To'] == 'alice@example.com' and got['From'] == 'Skopax <alerts@example.com>'
    assert got['Message-ID'] == sent_id and sent_id.endswith('@example.com>') and got['Date']
    assert got['Auto-Submitted'] == 'auto-generated' and got['X-Skopax-Kind'] == 'route'
    assert got.get_content_type() == 'text/plain' and got.get_content_charset() == 'utf-8' and got.get_content().strip() == 'Caf\u00e9 is down'
    assert session.sent[-1][2] == ['alice@example.com']


def test_html_makes_multipart_alternative_and_reply_to_is_set(monkeypatch):
    # given
    fake = faked(monkeypatch)

    # when
    Mailer(CONFIG | {'reply-to': 'ops@example.com'}).send(['alice@example.com', 'bob@example.com'], 'Hi', 'plain', '<p>rich</p>')

    # then
    got = parsed(fake)
    assert got.get_content_type() == 'multipart/alternative'
    assert [p.get_content_type() for p in got.iter_parts()] == ['text/plain', 'text/html']
    assert got['Reply-To'] == 'ops@example.com' and got['To'] == 'alice@example.com, bob@example.com' and 'X-Skopax-Kind' not in got


def test_tls_uses_smtp_ssl_and_no_username_means_no_login(monkeypatch):
    # given
    fake = faked(monkeypatch)

    # when
    Mailer(CONFIG | {'security': 'tls', 'port': '465', 'username': '', 'password': ''}).send(['alice@example.com'], 'Hi', 'plain')

    # then
    session = fake.made[-1]
    assert session.ssl and session.port == 465 and isinstance(session.context, ssl.SSLContext) and session.context.verify_mode == ssl.CERT_REQUIRED
    assert session.steps == ['connect', 'ehlo', 'send', 'quit']


def test_plain_none_goes_to_a_private_host_and_is_refused_for_a_public_one(monkeypatch):
    # given
    fake = faked(monkeypatch)
    monkeypatch.setattr(socket, 'getaddrinfo', lambda host, *a, **kw: [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('93.184.215.14', 0))])

    # when
    Mailer(CONFIG | {'security': 'none', 'host': '10.0.0.5', 'port': '25'}).send(['alice@example.com'], 'Hi', 'plain')
    Mailer(CONFIG | {'security': 'none', 'host': 'localhost', 'port': '25'}).send(['alice@example.com'], 'Hi', 'plain')
    with pytest.raises(MailRefused) as refused:
        Mailer(CONFIG | {'security': 'none', 'port': '25'}).send(['alice@example.com'], 'Hi', 'plain')

    # then
    assert [s.steps for s in fake.made] == [['connect', 'ehlo', 'login', 'send', 'quit']] * 2
    assert str(refused.value) == msg('mailserver.plain_public', host='smtp.example.com')


@pytest.mark.parametrize(('fail', 'kind'), [
    ({'connect': ConnectionRefusedError()}, MailDown),
    ({'connect': socket.timeout('timed out')}, MailDown),
    ({'connect': smtplib.SMTPConnectError(421, b'busy')}, MailDown),
    ({'send': smtplib.SMTPServerDisconnected('gone')}, MailDown),
    ({'send': smtplib.SMTPDataError(451, b'try later')}, MailDown),
    ({'send': smtplib.SMTPRecipientsRefused({'alice@example.com': (450, b'mailbox busy')})}, MailDown),
    ({'starttls': ssl.SSLError('handshake')}, MailDown),
    ({'login': smtplib.SMTPAuthenticationError(535, b'bad credentials')}, MailRefused),
    ({'send': smtplib.SMTPDataError(554, b'rejected')}, MailRefused),
    ({'send': smtplib.SMTPSenderRefused(553, b'not yours', 'alerts@example.com')}, MailRefused),
    ({'send': smtplib.SMTPRecipientsRefused({'alice@example.com': (550, b'no such user')})}, MailRefused),
    ({'starttls': ssl.SSLCertVerificationError('certificate verify failed')}, MailRefused),
    ({'starttls': smtplib.SMTPNotSupportedError('STARTTLS extension not supported by server.')}, MailRefused),
])
def test_failures_map_to_down_or_refused(monkeypatch, fail, kind):
    # given
    fake = faked(monkeypatch, **fail)

    # when
    with pytest.raises((MailDown, MailRefused)) as raised:
        Mailer(CONFIG).send(['alice@example.com'], 'Hi', 'plain')

    # then
    assert type(raised.value) is kind
    assert PASSWORD not in str(raised.value)
    assert all(s.steps[-1] in ('quit', 'close') for s in fake.made if len(s.steps) > 1)


@pytest.mark.parametrize(('fail', 'recipient'), [
    (smtplib.SMTPRecipientsRefused({'alice@example.com': (550, b'no such user')}), True),
    (smtplib.SMTPSenderRefused(553, b'not yours', 'alerts@example.com'), False),
    (smtplib.SMTPDataError(554, b'rejected'), False),
    (smtplib.SMTPAuthenticationError(535, b'bad credentials'), False),
])
def test_a_refusal_says_whether_the_server_refused_that_recipient_or_every_mail(monkeypatch, fail, recipient):
    # given
    faked(monkeypatch, **{'login' if isinstance(fail, smtplib.SMTPAuthenticationError) else 'send': fail})

    # when
    with pytest.raises(MailRefused) as raised:
        Mailer(CONFIG).send(['alice@example.com'], 'Hi', 'plain')

    # then
    assert raised.value.recipient is recipient
    assert MailRefused('plain words').recipient is False


def test_an_address_that_is_not_one_is_refused_for_that_recipient(monkeypatch):
    # given
    fake = faked(monkeypatch)

    # when
    with pytest.raises(MailRefused) as raised:
        Mailer(CONFIG).send(['ops@localhost'], 'Hi', 'plain')

    # then
    assert raised.value.recipient is True and fake.made == []


@pytest.mark.parametrize(('to', 'subject'), [
    (['alice@example.com'], 'Hi\r\nBcc: eve@example.com'),
    (['alice@example.com'], 'Hi\x00there'),
    (['alice@example.com\x00'], 'Hi'),
    (['alice@example.com'], 'Hi\nthere'),
    (['alice@example.com\r\nBcc: eve@example.com'], 'Hi'),
    (['not-an-address'], 'Hi'),
    (['alice@example'], 'Hi'),
    (['Alice <alice@example.com>'], 'Hi'),
    ([], 'Hi'),
    ([f'user{i}@example.com' for i in range(51)], 'Hi'),
])
def test_bad_subjects_and_addresses_are_refused_before_connecting(monkeypatch, to, subject):
    # given
    fake = faked(monkeypatch)

    # when
    with pytest.raises(MailRefused):
        Mailer(CONFIG).send(to, subject, 'plain')

    # then
    assert fake.made == []


def test_fifty_recipients_are_allowed(monkeypatch):
    # given
    fake = faked(monkeypatch)
    to = [f'user{i}@example.com' for i in range(mail.RECIPIENTS_MAX)]

    # when
    Mailer(CONFIG).send(to, 'Hi', 'plain')

    # then
    assert mail.RECIPIENTS_MAX == 50 and fake.made[-1].sent[-1][2] == to


@pytest.mark.parametrize('config', [
    CONFIG | {'from': 'Skopax\r\n <alerts@example.com>'},
    CONFIG | {'from': 'nobody'},
    CONFIG | {'reply-to': 'ops@example.com\nBcc: eve@example.com'},
    CONFIG | {'host': ''},
    CONFIG | {'security': 'carrier-pigeon'},
    CONFIG | {'port': '70000'},
])
def test_a_bad_server_config_is_not_ready_and_never_connects(monkeypatch, config):
    # given
    fake = faked(monkeypatch)

    # when
    ready = Mailer(config).ready()
    with pytest.raises(MailRefused):
        Mailer(config).send(['alice@example.com'], 'Hi', 'plain')

    # then
    assert ready is False and fake.made == []
    assert Mailer(CONFIG).ready() is True


def test_a_bad_kind_is_refused(monkeypatch):
    # given
    faked(monkeypatch)

    # when
    with pytest.raises(MailRefused):
        Mailer(CONFIG).send(['alice@example.com'], 'Hi', 'plain', kind='a\r\nBcc: eve@example.com')


def test_the_password_never_reaches_the_logs(monkeypatch, caplog):
    # given
    spaced = 'pa ss\t word  61'
    caplog.set_level(logging.DEBUG)

    # when
    faked(monkeypatch, send=smtplib.SMTPDataError(451, f'later, {PASSWORD}'.encode()))
    with pytest.raises(MailDown) as first:
        Mailer(CONFIG).send(['alice@example.com'], 'Hi', 'plain')
    faked(monkeypatch, send=smtplib.SMTPDataError(451, f'later, {spaced}'.encode()))
    with pytest.raises(MailDown) as second:
        Mailer(CONFIG | {'password': spaced}).send(['alice@example.com'], 'Hi', 'plain')

    # then
    logged = [r.getMessage() for r in caplog.records if r.name == 'skopax.mail']
    assert logged == [first.value.text, second.value.text] and all('451 later, ***' in line for line in logged)
    assert PASSWORD not in caplog.text and spaced not in caplog.text and PASSWORD not in repr(Mailer(CONFIG))
    assert spaced not in second.value.text and '***' in second.value.text


@pytest.mark.parametrize('over', [{'password': 'p\u00e4ss-61'}, {'username': 'j\u00fcrgen@example.com'}])
def test_a_non_ascii_sign_in_is_refused_before_connecting(monkeypatch, over):
    # given
    fake = faked(monkeypatch)

    # when
    with pytest.raises(MailRefused) as sent:
        Mailer(CONFIG | over).send(['alice@example.com'], 'Hi', 'plain')
    with pytest.raises(MailRefused) as probed:
        Mailer(CONFIG | over).probe()

    # then
    assert fake.made == [] and Mailer(CONFIG | over).ready() is False
    for raised in (sent.value, probed.value):
        assert raised.text == msg('mailserver.sign_in_ascii') and raised.__context__ is None and raised.__cause__ is None
        assert 'p\u00e4ss' not in repr(raised.args)


def test_plain_mode_connects_to_the_address_it_checked(monkeypatch):
    # given
    fake = faked(monkeypatch)
    asked = []

    def lookup(host, *a, **kw):
        asked.append(host)
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('10.0.0.9', 0))]

    monkeypatch.setattr(socket, 'getaddrinfo', lookup)

    # when
    Mailer(CONFIG | {'security': 'none', 'host': 'relay.example.com', 'port': '25'}).send(['alice@example.com'], 'Hi', 'plain')

    # then
    assert asked == ['relay.example.com'] and fake.made[-1].host == '10.0.0.9'


def test_a_slow_plain_mode_lookup_gives_up_in_time(monkeypatch):
    # given
    fake = faked(monkeypatch)
    import threading
    hold = threading.Event()
    monkeypatch.setattr(socket, 'getaddrinfo', lambda host, *a, **kw: hold.wait(5) and [])
    monkeypatch.setattr(mail, 'LOOKUP_TIMEOUT_S', 0.05)

    # when
    try:
        with pytest.raises(MailDown) as raised:
            Mailer(CONFIG | {'security': 'none', 'host': 'relay.example.com', 'port': '25'}).send(['alice@example.com'], 'Hi', 'plain')
    finally:
        hold.set()

    # then
    assert raised.value.text == msg('mailserver.connect', host='relay.example.com', port=25, reason='the host lookup took too long') and fake.made == []


@pytest.mark.parametrize(('given', 'problem'), [
    ('alerts@example.com', None),
    ('Skopax <alerts@example.com>', None),
    ('"Ops, Shop" <ops@shop.example.com>', None),
    ('alerts@', 'bad'),
    ('Skopax <alerts>', 'bad'),
    ('a@example.com, b@example.com', 'bad'),
])
def test_the_sender_check_takes_an_address_with_an_optional_name(given, problem):
    # when
    found = mail.sender_problem(given)

    # then
    assert (found is None) == (problem is None)


def test_card_mail_lays_out_title_severity_status_lines_and_link():
    # when
    subject, text, html = card_mail(CARD, LINK, head='New critical card', body_lines=['Error rate is 12%', 'Recommended: roll back'])

    # then
    assert subject == '[Skopax] New critical card: #7 checkout-service / ERROR lines'
    for part in ('New critical card', '#7 checkout-service / ERROR lines', 'Critical', 'Firing', 'Error rate is 12%', 'Recommended: roll back', LINK, 'Skopax'):
        assert part in text and part in html
    assert f'href="{LINK}"' in html and '<img' not in html and '<link' not in html and '<style' not in html and 'src=' not in html
    assert brand.attribution_line()['text'] in text


def test_card_mail_escapes_html_and_drops_a_link_that_is_not_web():
    # given
    card = CARD | {'svc': '<script>alert(1)</script>', 'label': 'a & "b"'}

    # when
    subject, text, html = card_mail(card, 'javascript:alert(1)', head='<b>head</b>', body_lines=['<img src=x onerror=alert(1)>'])

    # then
    assert '<script>' not in html and '<img' not in html and '<b>head' not in html
    assert '&lt;script&gt;' in html and '&lt;img src=x' in html and 'a &amp; &quot;b&quot;' in html
    assert 'javascript:' not in html and 'javascript:' not in text
    assert '<script>alert(1)</script>' in text and '\n' not in subject


def test_card_mail_follows_the_status_of_the_card():
    # when
    states = [card_mail(CARD | over, LINK, head='h', body_lines=[])[1] for over in ({'status': 'cleared'}, {'triage': 'progress'}, {'muted': True})]

    # then
    assert msg('mail.state.stopped') in states[0] and msg('mail.state.progress') in states[1] and msg('mail.state.muted') in states[2]


def test_card_mail_speaks_the_language_it_is_given(monkeypatch):
    # given
    from skopax import messages
    messages.set_catalogs([('de', {'mail.state.firing': 'Feuert', 'mail.sev.crit': 'Kritisch'})])

    # when
    try:
        _, german, _ = card_mail(CARD, LINK, head='h', body_lines=[], lang='de')
        _, english, _ = card_mail(CARD, LINK, head='h', body_lines=[])
    finally:
        messages.reset()

    # then
    assert 'Feuert' in german and 'Kritisch' in german and 'Feuert' not in english and 'Firing' in english
    assert messages.outbound('mail.state.firing')


def test_card_mail_keeps_the_footer_without_attribution():
    # given
    brand.use_hidden(lambda: True)

    # when
    _, text, html = card_mail(CARD, LINK, head='h', body_lines=[])

    # then
    assert 'skopax.io' not in text and 'skopax.io' not in html and msg('mail.footer') in text


def test_speaking_does_not_leak_out_of_card_mail():
    # when
    with speaking(None):
        card_mail(CARD, LINK, head='h', body_lines=[], lang='de')

    # then
    from skopax.messages import SPEAKING
    assert SPEAKING.get() is None
