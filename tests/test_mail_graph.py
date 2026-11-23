import base64
import datetime as dt
import email
import email.policy
import hashlib
import http.server
import json
import logging
import threading
import urllib.parse

import jwt
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from skopax import graphmail, mail, remote
from skopax.contract import InvalidInstance
from skopax.instances import moved_addresses, validate
from skopax.messages import msg
from skopax.plugins import mail_server
from skopax.registry import describe
from tests.mail_kit import CONFIG as SMTP_CONFIG
from tests.mail_kit import faked
from tests.test_mail_server_plugin import runtime_of

TENANT = '11111111-2222-3333-4444-555555555555'
CLIENT = '66666666-7777-8888-9999-000000000000'
SECRET = 'client-secret-word-58'
TOKEN_URL = f'https://login.microsoftonline.com/{TENANT}/oauth2/v2.0/token'
SEND_URL = 'https://graph.microsoft.com/v1.0/users/alerts@example.com/sendMail'
NOW = 1_794_240_000.0
BODY_SECRET = 'planted-body-detail-91'


def made_credential(name: str = 'skopax-test') -> tuple[str, x509.Certificate]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name)])
    start = dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(subject).issuer_name(subject).public_key(key.public_key()).serial_number(1)
            .not_valid_before(start).not_valid_after(start + dt.timedelta(days=3650)).sign(key, hashes.SHA256()))
    pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode()
    return pem + cert.public_bytes(serialization.Encoding.PEM).decode(), cert


PEM, CERT = made_credential()
OTHER_PEM, _ = made_credential('other')
GRAPH = {'method': 'graph', 'tenant-id': TENANT, 'client-id': CLIENT, 'client-secret': SECRET, 'sender': 'Alerts <alerts@example.com>'}
CERT_CONFIG = {k: v for k, v in GRAPH.items() if k != 'client-secret'} | {'client-cert': PEM}


def token_of(roles: list[str] | None = None, n: int = 1) -> str:
    return jwt.encode({'n': n, **({'roles': roles} if roles is not None else {})}, 'k' * 32, algorithm='HS256')


class FakeGraph:
    def __init__(self, sends=(), tokens=None, life: int = 3599):
        self.calls: list[tuple[str, str, dict, bytes | None, float]] = []
        self.sends = list(sends)
        self.tokens = tokens
        self.life = life
        self.issued = 0

    def __call__(self, method, url, headers, data, timeout):
        self.calls.append((method, url, dict(headers), data, timeout))
        if url.endswith('/oauth2/v2.0/token'):
            if self.tokens is not None:
                raise self.tokens
            self.issued += 1
            return remote.Reply({'token_type': 'Bearer', 'expires_in': self.life, 'access_token': token_of(n=self.issued)}, {})
        if self.sends:
            step = self.sends.pop(0)
            if isinstance(step, BaseException):
                raise step
            return step
        return remote.Reply('', {})

    def token_calls(self):
        return [c for c in self.calls if c[1].endswith('/oauth2/v2.0/token')]

    def send_calls(self):
        return [c for c in self.calls if c[1].endswith('/sendMail')]


def form_of(call) -> dict[str, str]:
    return dict(urllib.parse.parse_qsl(call[3].decode()))


def http_error(code: int, body: object = None, headers: dict | None = None) -> remote.HttpError:
    return remote.HttpError(code, 'https://graph.microsoft.com/v1.0', json.dumps(body) if body is not None else '', headers)


@pytest.fixture(autouse=True)
def fresh_tokens(monkeypatch):
    graphmail.forget()
    monkeypatch.setattr(graphmail, 'clock', lambda: NOW)
    slept: list[float] = []
    monkeypatch.setattr(graphmail, 'sleep', slept.append)
    yield slept
    graphmail.forget()


def test_a_client_secret_buys_a_token_with_the_graph_scope():
    # given
    fake = FakeGraph()

    # when
    mail.Mailer(GRAPH, transport=fake).send(['alice@example.com'], 'Hi', 'plain')

    # then
    method, url, headers, _, _ = fake.token_calls()[0]
    form = form_of(fake.token_calls()[0])
    assert (method, url, headers['Content-Type']) == ('POST', TOKEN_URL, 'application/x-www-form-urlencoded')
    assert form == {'client_id': CLIENT, 'scope': 'https://graph.microsoft.com/.default', 'grant_type': 'client_credentials', 'client_secret': SECRET}


def test_a_certificate_signs_a_client_assertion_with_its_sha256_thumbprint():
    # given
    fake = FakeGraph()

    # when
    mail.Mailer(CERT_CONFIG, transport=fake).send(['alice@example.com'], 'Hi', 'plain')

    # then
    form = form_of(fake.token_calls()[0])
    signed = form['client_assertion']
    head = jwt.get_unverified_header(signed)
    der = CERT.public_bytes(serialization.Encoding.DER)
    claims = jwt.decode(signed, CERT.public_key(), algorithms=['PS256'], audience=TOKEN_URL, options={'verify_nbf': False, 'verify_exp': False})
    assert 'client_secret' not in form and form['client_assertion_type'] == 'urn:ietf:params:oauth:client-assertion-type:jwt-bearer'
    assert (head['alg'], head['typ']) == ('PS256', 'JWT') and head['x5t#S256'] == base64.urlsafe_b64encode(hashlib.sha256(der).digest()).rstrip(b'=').decode()
    assert (claims['iss'], claims['sub'], claims['aud']) == (CLIENT, CLIENT, TOKEN_URL) and claims['jti']
    assert (claims['nbf'], claims['iat'], claims['exp']) == (NOW - 60, NOW, NOW + 300)


def test_a_certificate_pasted_without_line_breaks_still_signs():
    # given
    flat = PEM.replace('\n', '')

    # when
    found = graphmail.credential(flat)

    # then
    assert found is not None and found[1] == CERT and graphmail.credential_problem(flat) is None


@pytest.mark.parametrize('value', ['', 'not a pem', PEM.split('-----BEGIN CERTIFICATE-----')[0], OTHER_PEM.split('-----BEGIN CERTIFICATE-----')[0] + PEM.split('-----END PRIVATE KEY-----')[1]])
def test_a_certificate_without_its_own_key_is_refused_on_save(value):
    # then
    assert graphmail.credential_problem(value) == msg('mailserver.graph_cert')


def test_the_token_is_kept_until_shortly_before_it_runs_out(monkeypatch):
    # given
    fake = FakeGraph(life=3600)
    send = lambda: mail.Mailer(dict(GRAPH), transport=fake).send(['alice@example.com'], 'Hi', 'plain')

    # when
    send()
    send()
    monkeypatch.setattr(graphmail, 'clock', lambda: NOW + 3600 - graphmail.TOKEN_MARGIN_S + 1)
    send()

    # then
    tokens = [c[2]['Authorization'] for c in fake.send_calls()]
    assert len(fake.token_calls()) == 2 and tokens[0] == tokens[1] != tokens[2]


def test_each_credential_keeps_its_own_token():
    # given
    fake = FakeGraph()
    other = GRAPH | {'client-secret': 'another-secret'}

    # when
    for config in (GRAPH, other, GRAPH, other):
        mail.Mailer(config, transport=fake).send(['alice@example.com'], 'Hi', 'plain')

    # then
    used = [c[2]['Authorization'] for c in fake.send_calls()]
    secrets = [form_of(c)['client_secret'] for c in fake.token_calls()]
    assert secrets == [SECRET, 'another-secret'] and used[0] == used[2] != used[1] == used[3]


@pytest.mark.parametrize('change', [{'authority': 'https://login.microsoftonline.us'}, {'graph-url': 'https://graph.microsoft.us'},
                                    {'tenant-id': 'example.onmicrosoft.com'}, {'client-id': '66666666-7777-8888-9999-000000000001'}])
def test_the_same_secret_somewhere_else_gets_a_new_token(change):
    # given
    fake = FakeGraph()

    # when
    mail.Mailer(GRAPH, transport=fake).send(['alice@example.com'], 'Hi', 'plain')
    mail.Mailer(GRAPH | change, transport=fake).send(['alice@example.com'], 'Hi', 'plain')

    # then
    used = [c[2]['Authorization'] for c in fake.send_calls()]
    assert len(fake.token_calls()) == 2 and used[0] != used[1]


def test_a_refused_token_drops_only_its_own_credential():
    # given
    fake = FakeGraph()
    other = GRAPH | {'client-secret': 'another-secret'}
    mail.Mailer(GRAPH, transport=fake).send(['alice@example.com'], 'Hi', 'plain')
    mail.Mailer(other, transport=fake).send(['alice@example.com'], 'Hi', 'plain')
    fake.sends = [http_error(401)]

    # when
    with pytest.raises(mail.MailDown):
        mail.Mailer(GRAPH, transport=fake).send(['alice@example.com'], 'Hi', 'plain')
    mail.Mailer(other, transport=fake).send(['alice@example.com'], 'Hi', 'plain')

    # then
    used = [c[2]['Authorization'] for c in fake.send_calls()]
    assert len(fake.token_calls()) == 2 and used[3] == used[1]


def test_a_new_token_sweeps_out_the_tokens_that_ran_out(monkeypatch):
    # given
    fake = FakeGraph(life=3600)
    mail.Mailer(GRAPH | {'client-secret': 'typed-wrong-once'}, transport=fake).send(['alice@example.com'], 'Hi', 'plain')
    monkeypatch.setattr(graphmail, 'clock', lambda: NOW + 7200)

    # when
    mail.Mailer(GRAPH, transport=fake).send(['alice@example.com'], 'Hi', 'plain')

    # then
    assert len(graphmail.TOKENS) == 1 and all(until > NOW + 7200 for _, until in graphmail.TOKENS.values())


def test_send_mail_posts_the_mime_message_in_base64_as_the_sender():
    # given
    fake = FakeGraph()

    # when
    mail.Mailer(GRAPH | {'reply-to': 'ops@example.com'}, transport=fake).send(['alice@example.com', 'bob@example.com'], 'Card fired', 'plain body', '<p>html</p>', kind='route')

    # then
    method, url, headers, data, _ = fake.send_calls()[0]
    sent = email.message_from_bytes(base64.b64decode(data), policy=email.policy.default)
    assert (method, url, headers['Content-Type']) == ('POST', SEND_URL, 'text/plain') and headers['Authorization'].startswith('Bearer ')
    assert (sent['Auto-Submitted'], sent['X-Skopax-Kind'], sent['Subject']) == ('auto-generated', 'route', 'Card fired')
    assert str(sent['From']) == 'Alerts <alerts@example.com>' and str(sent['Reply-To']) == 'ops@example.com'
    assert [a.addr_spec for a in sent['To'].addresses] == ['alice@example.com', 'bob@example.com'] and sent['Bcc'] is None and sent['Cc'] is None
    assert sent.get_body(('plain',)).get_content().strip() == 'plain body'


def test_the_recipients_match_what_smtp_sends(monkeypatch):
    # given
    fake, smtp = FakeGraph(), faked(monkeypatch)
    to = ['alice@example.com', 'bob@example.com']

    # when
    mail.Mailer(GRAPH, transport=fake).send(to, 'Hi', 'plain')
    mail.Mailer(SMTP_CONFIG).send(to, 'Hi', 'plain')

    # then
    sent = email.message_from_bytes(base64.b64decode(fake.send_calls()[0][3]), policy=email.policy.default)
    message, _, to_addrs = smtp.made[-1].sent[0]
    assert [a.addr_spec for a in sent['To'].addresses] == to_addrs == to and str(message['To']) == str(sent['To'])


def test_a_mailbox_name_is_quoted_into_the_send_url():
    # given
    fake = FakeGraph()

    # when
    mail.Mailer(GRAPH | {'sender': 'ops+alerts@example.com'}, transport=fake).send(['alice@example.com'], 'Hi', 'plain')

    # then
    assert fake.send_calls()[0][1] == 'https://graph.microsoft.com/v1.0/users/ops%2Balerts@example.com/sendMail'


def test_a_national_cloud_uses_its_own_addresses():
    # given
    fake = FakeGraph()
    config = GRAPH | {'authority': 'https://login.microsoftonline.us/', 'graph-url': 'https://graph.microsoft.us'}

    # when
    mail.Mailer(config, transport=fake).send(['alice@example.com'], 'Hi', 'plain')

    # then
    assert fake.token_calls()[0][1] == f'https://login.microsoftonline.us/{TENANT}/oauth2/v2.0/token'
    assert form_of(fake.token_calls()[0])['scope'] == 'https://graph.microsoft.us/.default'
    assert fake.send_calls()[0][1] == 'https://graph.microsoft.us/v1.0/users/alerts@example.com/sendMail'


def test_send_answered_says_graph_accepted_the_mail():
    # given
    fake = FakeGraph()
    plain = FakeGraph(sends=[remote.Reply('', {}, 200)])

    # when
    said = mail.Mailer(GRAPH, transport=fake).send_answered(['alice@example.com'], 'Hi', 'plain')
    graphmail.forget()
    other = mail.Mailer(GRAPH, transport=plain).send_answered(['alice@example.com'], 'Hi', 'plain')

    # then
    assert (said, other) == ('202 Accepted', '200 OK')


@pytest.mark.parametrize(('error', 'kind', 'recipient', 'key'), [
    (http_error(401, {'error': {'code': 'InvalidAuthenticationToken'}}), mail.MailDown, False, 'mailserver.graph_credentials'),
    (http_error(403, {'error': {'code': 'ErrorAccessDenied'}}), mail.MailRefused, False, 'mailserver.graph_scope'),
    (http_error(404, {'error': {'code': 'ErrorInvalidUser'}}), mail.MailRefused, False, 'mailserver.graph_mailbox'),
    (http_error(400, {'error': {'code': 'ErrorInvalidRecipients'}}), mail.MailRefused, True, 'mailserver.graph_recipient'),
    (http_error(400, {'error': {'code': 'ErrorRecipientNotFound'}}), mail.MailRefused, True, 'mailserver.graph_recipient'),
    (http_error(400, {'error': {'code': 'ErrorMissingRecipients'}}), mail.MailRefused, False, 'mailserver.graph_refused'),
    (http_error(400, {'error': {'code': 'ErrorRecipientTypeLooksSimilar'}}), mail.MailRefused, False, 'mailserver.graph_refused'),
    (http_error(408), mail.MailDown, False, 'mailserver.graph_down'),
    (http_error(429), mail.MailDown, False, 'mailserver.graph_down'),
    (http_error(400, {'error': {'code': 'ErrorMimeContentInvalidBase64String'}}), mail.MailRefused, False, 'mailserver.graph_refused'),
    (http_error(400, 'not an object'), mail.MailRefused, False, 'mailserver.graph_refused'),
    (http_error(500), mail.MailDown, False, 'mailserver.graph_down'),
    (http_error(503), mail.MailDown, False, 'mailserver.graph_down'),
    (remote.TimedOut('https://graph.microsoft.com gave no full answer'), mail.MailDown, False, 'mailserver.graph_down'),
    (remote.Unreachable('https://graph.microsoft.com unreachable'), mail.MailDown, False, 'mailserver.graph_down'),
])
def test_each_graph_answer_maps_to_down_or_refused(error, kind, recipient, key):
    # given
    fake = FakeGraph(sends=[error])
    heads = {'mailserver.graph_credentials': 'Microsoft Graph refused the credentials', 'mailserver.graph_scope': 'Microsoft 365 does not let the app send as alerts@example.com',
             'mailserver.graph_mailbox': 'Microsoft 365 has no mailbox', 'mailserver.graph_recipient': 'Microsoft Graph refused a recipient',
             'mailserver.graph_refused': 'Microsoft Graph refused the mail', 'mailserver.graph_down': 'Microsoft Graph at graph.microsoft.com did not take the mail'}

    # when
    with pytest.raises((mail.MailDown, mail.MailRefused)) as failed:
        mail.Mailer(GRAPH, transport=fake).send(['alice@example.com'], 'Hi', 'plain')

    # then
    assert type(failed.value) is kind and getattr(failed.value, 'recipient', False) is recipient
    assert failed.value.text.startswith(heads[key]) and 'alice' not in failed.value.text


def test_a_refused_token_is_dropped_so_the_next_mail_gets_a_new_one():
    # given
    fake = FakeGraph(sends=[http_error(401)])

    # when
    with pytest.raises(mail.MailDown):
        mail.Mailer(GRAPH, transport=fake).send(['alice@example.com'], 'Hi', 'plain')
    mail.Mailer(GRAPH, transport=fake).send(['alice@example.com'], 'Hi', 'plain')

    # then
    assert len(fake.token_calls()) == 2


def test_a_short_retry_after_waits_and_tries_once_more(fresh_tokens):
    # given
    fake = FakeGraph(sends=[http_error(429, headers={'Retry-After': '2'})])

    # when
    said = mail.Mailer(GRAPH, transport=fake).send_answered(['alice@example.com'], 'Hi', 'plain')

    # then
    assert said == '202 Accepted' and fresh_tokens == [2] and len(fake.send_calls()) == 2


def test_a_short_retry_after_on_503_waits_and_tries_once_more(fresh_tokens):
    # given
    fake = FakeGraph(sends=[http_error(503, headers={'Retry-After': '1'})])

    # when
    said = mail.Mailer(GRAPH, transport=fake).send_answered(['alice@example.com'], 'Hi', 'plain')

    # then
    assert said == '202 Accepted' and fresh_tokens == [1] and len(fake.send_calls()) == 2


def test_a_retry_after_date_counts_as_a_long_wait(fresh_tokens):
    # given
    fake = FakeGraph(sends=[http_error(429, headers={'Retry-After': 'Wed, 21 Oct 2026 07:28:00 GMT'})])

    # when
    with pytest.raises(mail.MailDown) as failed:
        mail.Mailer(GRAPH, transport=fake).send(['alice@example.com'], 'Hi', 'plain')

    # then
    assert failed.value.text == msg('mailserver.graph_busy', wait=graphmail.RETRY_DEFAULT_S) and fresh_tokens == [] and len(fake.send_calls()) == 1


def test_a_long_retry_after_is_mail_down_with_the_wait(fresh_tokens):
    # given
    fake = FakeGraph(sends=[http_error(429, headers={'Retry-After': '120'})])

    # when
    with pytest.raises(mail.MailDown) as failed:
        mail.Mailer(GRAPH, transport=fake).send(['alice@example.com'], 'Hi', 'plain')

    # then
    assert failed.value.text == msg('mailserver.graph_busy', wait=120) and fresh_tokens == [] and len(fake.send_calls()) == 1


def test_a_second_throttle_after_the_wait_is_mail_down(fresh_tokens):
    # given
    fake = FakeGraph(sends=[http_error(429, headers={'Retry-After': '1'}), http_error(429, headers={'Retry-After': '1'})])

    # when
    with pytest.raises(mail.MailDown) as failed:
        mail.Mailer(GRAPH, transport=fake).send(['alice@example.com'], 'Hi', 'plain')

    # then
    assert failed.value.text == msg('mailserver.graph_busy', wait=1) and fresh_tokens == [1] and len(fake.send_calls()) == 2


@pytest.mark.parametrize(('error', 'kind', 'shown'), [
    (http_error(401, {'error': 'invalid_client', 'error_codes': [7000215], 'error_description': BODY_SECRET}), mail.MailRefused, 'invalid_client AADSTS7000215'),
    (http_error(400, {'error': 'unauthorized_client', 'error_codes': [700016]}), mail.MailRefused, 'unauthorized_client AADSTS700016'),
    (http_error(500, {'error': 'temporarily_unavailable'}), mail.MailDown, 'temporarily_unavailable'),
    (http_error(429), mail.MailDown, 'HTTP 429'),
    (remote.TimedOut('login gave no answer'), mail.MailDown, 'no answer in 20 s'),
])
def test_a_refused_or_failed_token_says_only_the_error_kind(error, kind, shown):
    # given
    fake = FakeGraph(tokens=error)

    # when
    with pytest.raises((mail.MailDown, mail.MailRefused)) as failed:
        mail.Mailer(GRAPH, transport=fake).send(['alice@example.com'], 'Hi', 'plain')

    # then
    assert type(failed.value) is kind and shown in failed.value.text and BODY_SECRET not in failed.value.text and fake.send_calls() == []


def test_no_secret_token_or_answer_body_reaches_the_logs(caplog):
    # given
    caplog.set_level(logging.DEBUG)
    fake = FakeGraph(sends=[http_error(403, {'error': {'code': 'ErrorAccessDenied', 'message': BODY_SECRET}})])
    refused = FakeGraph(tokens=http_error(401, {'error': 'invalid_client', 'error_description': BODY_SECRET}))

    # when
    with pytest.raises(mail.MailRefused) as first:
        mail.Mailer(CERT_CONFIG, transport=fake).send(['alice@example.com'], 'Hi', 'plain')
    with pytest.raises(mail.MailRefused) as second:
        mail.Mailer(GRAPH, transport=refused).send(['alice@example.com'], 'Hi', 'plain')

    # then
    token = fake.send_calls()[0][2]['Authorization'].split()[1]
    signed = form_of(fake.token_calls()[0])['client_assertion']
    logged = ' '.join(r.getMessage() for r in caplog.records) + first.value.text + second.value.text
    key_line = PEM.splitlines()[1]
    assert logged and all(part not in logged for part in (SECRET, token, signed, BODY_SECRET, key_line, 'alice@example.com'))
    assert repr(mail.Mailer(CERT_CONFIG)) == 'Mailer(graph:alerts@example.com)'


def test_test_connection_gets_a_new_token_and_names_what_it_does_not_prove():
    # given
    fake = FakeGraph()
    mailer = mail.Mailer(GRAPH, transport=fake)
    mailer.send(['alice@example.com'], 'Hi', 'plain')

    # when
    said = mailer.probe()

    # then
    assert said == msg('mailserver.graph_ok', client=CLIENT, sender='alerts@example.com')
    assert len(fake.token_calls()) == 2 and len(fake.send_calls()) == 1


def test_test_connection_warns_about_an_entra_wide_mail_send():
    # given
    wide = token_of(['Mail.Send'])
    fake = lambda method, url, headers, data, timeout: remote.Reply({'access_token': wide, 'expires_in': 3599}, {})

    # when
    said = mail.Mailer(GRAPH, transport=fake).probe()

    # then
    assert said == msg('mailserver.graph_ok_wide', client=CLIENT, sender='alerts@example.com')


def test_test_connection_of_the_plugin_runs_the_graph_probe(store, monkeypatch):
    # given
    fake = FakeGraph()
    monkeypatch.setattr(graphmail, 'TRANSPORT', fake)

    # when
    answer = runtime_of(store).connection_test(mail_server.PLUGIN, None, GRAPH, 30, NOW)
    refused = runtime_of(store).connection_test(mail_server.PLUGIN, None, GRAPH | {'client-secret': ''}, 30, NOW)

    # then
    assert (answer['ok'], answer['summary']) == (True, msg('mailserver.graph_ok', client=CLIENT, sender='alerts@example.com'))
    assert (refused['ok'], refused['summary']) == (False, msg('mailserver.graph_not_ready')) and len(fake.token_calls()) == 1


def test_a_graph_mail_server_is_the_mailer_of_the_runtime(store, monkeypatch):
    # given
    fake = FakeGraph()
    monkeypatch.setattr(graphmail, 'TRANSPORT', fake)
    runtime = runtime_of(store, ('main', CERT_CONFIG))

    # when
    found = mail.mailer(runtime)
    found.send(['alice@example.com'], 'Hi', 'plain')

    # then
    assert found.ready() and fake.send_calls()[0][1] == SEND_URL


@pytest.mark.parametrize('change', [{'tenant-id': ''}, {'client-id': 'not-a-guid'}, {'sender': 'alerts'}, {'client-secret': ''}, {'authority': 'http://login.example.com'},
                                    {'graph-url': 'https://graph.example.com?x=1'}, {'client-secret': '', 'client-cert': OTHER_PEM.split('-----BEGIN CERTIFICATE-----')[0]},
                                    {'reply-to': 'ops@'}, {'method': 'fax'}])
def test_a_graph_mail_server_that_is_not_ready_sends_nothing(change):
    # given
    fake = FakeGraph()

    # when
    with pytest.raises(mail.MailRefused):
        mail.Mailer(GRAPH | change, transport=fake).send(['alice@example.com'], 'Hi', 'plain')

    # then
    assert fake.calls == []


def test_a_stored_smtp_instance_without_a_method_keeps_sending_over_smtp(store, monkeypatch):
    # given
    smtp, fake = faked(monkeypatch), FakeGraph()
    monkeypatch.setattr(graphmail, 'TRANSPORT', fake)
    runtime = runtime_of(store, ('main', {'host': 'smtp.example.com', 'from': 'alerts@example.com'}))

    # when
    mail.mailer(runtime).send(['alice@example.com'], 'Hi', 'plain')

    # then
    assert smtp.made[-1].steps == ['connect', 'ehlo', 'starttls', 'ehlo', 'send', 'quit'] and fake.calls == []


def test_smtp_fields_show_only_for_smtp_and_graph_fields_only_for_graph():
    # given
    manifest = {f['key']: f for f in describe(mail_server.PLUGIN)['fields']}
    smtp_only = ('host', 'port', 'security', 'username', 'password', 'from')
    graph_only = ('tenant-id', 'client-id', 'client-cert', 'client-secret', 'sender', 'authority', 'graph-url')

    # then
    assert all(manifest[k]['showIf'] == ['method', 'smtp'] for k in smtp_only)
    assert all(manifest[k]['showIf'] == ['method', 'graph'] for k in graph_only)
    assert manifest['method']['showIf'] is None and manifest['reply-to']['showIf'] is None
    assert (manifest['authority']['default'], manifest['graph-url']['default']) == ('https://login.microsoftonline.com', 'https://graph.microsoft.com')
    assert (manifest['client-cert']['kind'], manifest['client-secret']['kind'], manifest['authority']['kind']) == ('secret', 'secret', 'url')


def test_a_graph_instance_needs_no_smtp_field_and_an_smtp_instance_no_graph_field():
    # when
    graph = validate(mail_server.PLUGIN, CERT_CONFIG)
    smtp = validate(mail_server.PLUGIN, {'host': 'smtp.example.com', 'from': 'alerts@example.com'})

    # then
    assert graph['method'] == 'graph' and 'host' not in graph and smtp == {'host': 'smtp.example.com', 'from': 'alerts@example.com'}
    with pytest.raises(InvalidInstance) as missing:
        validate(mail_server.PLUGIN, {'method': 'graph', 'client-id': CLIENT, 'sender': 'alerts@example.com'})
    assert missing.value.field == 'tenant-id'
    with pytest.raises(InvalidInstance) as smtp_missing:
        validate(mail_server.PLUGIN, {'from': 'alerts@example.com'})
    assert smtp_missing.value.field == 'host'


def test_the_graph_fields_are_checked_on_save():
    # given
    fields = {f.key: f for f in mail_server.PLUGIN.fields}

    # then
    assert fields['tenant-id'].check(TENANT) is None and fields['tenant-id'].check('example.onmicrosoft.com') is None and fields['tenant-id'].check('a/b') == msg('mailserver.graph_tenant')
    assert fields['client-id'].check(CLIENT) is None and fields['client-id'].check('app') == msg('mailserver.graph_client')
    assert fields['client-cert'].check(PEM) is None and fields['client-cert'].check('x') == msg('mailserver.graph_cert')
    assert fields['authority'].check('https://login.microsoftonline.com') is None and fields['authority'].check('http://login.microsoftonline.com') == msg('mailserver.graph_https')
    assert fields['authority'].check('https://login.microsoftonline.com@evil.example') == msg('mailserver.graph_https')
    assert fields['graph-url'].check('https://user:word' + '@' + 'graph.microsoft.com') == msg('mailserver.graph_https')
    assert fields['sender'].check('Alerts <alerts@example.com>') is None and fields['sender'].check('alerts') == msg('mailserver.from')


def test_a_2048_bit_certificate_and_key_fit_in_a_secret_field():
    # then
    assert len(PEM.replace('\n', '\r\n')) < 3200 and graphmail.credential_problem(PEM.replace('\n', '\r\n')) is None


def test_a_moved_authority_or_graph_url_needs_the_secrets_again():
    # given
    fields = {f.key: f for f in mail_server.PLUGIN.fields}
    before = {k: v for k, v in GRAPH.items() if k != 'client-secret'}

    # when
    moved = [moved_addresses(mail_server.PLUGIN, fields, {}, before, {key: 'https://graph.example.com'}) for key in ('authority', 'graph-url')]
    kept = moved_addresses(mail_server.PLUGIN, fields, {}, before, {'sender': 'ops@example.com'})

    # then
    assert moved == [{'mail-server'}, {'mail-server'}] and kept == set()


def test_a_certificate_pasted_with_spaces_or_as_a_traditional_rsa_key_still_signs():
    # given
    key = serialization.load_pem_private_key(PEM.encode(), password=None)
    traditional = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL, serialization.NoEncryption()).decode()
    cert = CERT.public_bytes(serialization.Encoding.PEM).decode()

    # when
    spaced = graphmail.credential(PEM.replace('\n', ' '))
    old = graphmail.credential(traditional + cert)

    # then
    assert spaced is not None and old is not None and old[1] == CERT and '-----BEGIN RSA PRIVATE KEY-----' in traditional


def test_a_graph_mail_server_saved_through_admin_sends_with_its_certificate(store, monkeypatch):
    # given
    fake = FakeGraph()
    monkeypatch.setattr(graphmail, 'TRANSPORT', fake)
    runtime = runtime_of(store)
    key_only = PEM.split('-----BEGIN CERTIFICATE-----')[0]

    # when
    with pytest.raises(InvalidInstance) as refused:
        runtime.instances.create('mail-server', 'main', CERT_CONFIG | {'client-cert': key_only}, True, 'alice@example.com')
    runtime.instances.create('mail-server', 'main', CERT_CONFIG, True, 'alice@example.com')
    runtime.refresh()
    mail.mailer(runtime).send(['bob@example.com'], 'Hi', 'plain')

    # then
    stored = store.db['instances'].find_one({'_id': 'mail-server/main'})
    assert (refused.value.field, refused.value.text) == ('client-cert', msg('mailserver.graph_cert'))
    assert 'client-cert' not in stored['values'] and PEM not in json.dumps(stored['secrets']) and fake.send_calls()[0][1] == SEND_URL
    assert 'client_assertion' in form_of(fake.token_calls()[0])


@pytest.mark.parametrize(('error', 'kind'), [(http_error(401, {'error': 'invalid_client'}), mail.MailRefused), (http_error(500), mail.MailDown), (remote.Unreachable('login unreachable'), mail.MailDown)])
def test_test_connection_says_when_the_token_endpoint_fails(store, monkeypatch, error, kind):
    # given
    fake = FakeGraph(tokens=error)
    monkeypatch.setattr(graphmail, 'TRANSPORT', fake)

    # when
    with pytest.raises(kind) as failed:
        mail.Mailer(GRAPH).probe()
    answer = runtime_of(store).connection_test(mail_server.PLUGIN, None, GRAPH, 30, NOW)

    # then
    assert (answer['ok'], answer['summary']) == (False, failed.value.text) and fake.send_calls() == []


def test_the_graph_transport_never_follows_a_redirect_with_the_credentials(monkeypatch):
    # given
    for name in ('HTTPS_PROXY', 'HTTP_PROXY', 'https_proxy', 'http_proxy', 'ALL_PROXY', 'all_proxy'):
        monkeypatch.delenv(name, raising=False)
    seen: list[tuple[str, str | None]] = []

    class Moving(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            seen.append((self.path, self.headers.get('Authorization')))
            self.send_response(302)
            self.send_header('Location', '/stolen')
            self.send_header('Content-Length', '0')
            self.end_headers()

        do_GET = do_POST

        def log_message(self, *args):
            pass

    server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Moving)
    threading.Thread(target=server.serve_forever, daemon=True).start()

    # when
    try:
        with pytest.raises(remote.HttpError) as moved:
            graphmail.TRANSPORT('POST', f'http://127.0.0.1:{server.server_port}/token', {'Authorization': 'Bearer kept-here'}, b'grant_type=client_credentials', 5)
    finally:
        server.shutdown()
        server.server_close()

    # then
    assert moved.value.code == 302 and seen == [('/token', 'Bearer kept-here')]
