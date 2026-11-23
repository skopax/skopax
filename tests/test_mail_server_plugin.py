import json
import smtplib
import socket
import ssl

import pytest

from skopax import mail
from skopax.contract import InvalidInstance
from skopax.instances import Instances, PluginState
from skopax.messages import msg
from skopax.plugins import mail_server
from skopax.registry import BUILTIN, Registry, describe
from skopax.runtime import Runtime
from skopax.sealer import Sealer
from tests.mail_kit import CONFIG, PASSWORD, faked
from tests.plugin_kit import KIT_KEY, add_instance

NOW = 1_794_240_000.0


def runtime_of(store, *specs) -> Runtime:
    registry = Registry.of(mail_server.PLUGIN)
    runtime = Runtime(registry, PluginState(store.db), Instances(store.db, registry, lambda: Sealer(KIT_KEY)), None)
    for index, (name, values) in enumerate(specs):
        add_instance(store, mail_server.PLUGIN, name, sealer_key=KIT_KEY, primary=index == 0, **values)
    runtime.refresh()
    return runtime


def probed(store, **values) -> dict:
    return runtime_of(store).connection_test(mail_server.PLUGIN, None, CONFIG | values, 30, NOW)


def test_the_mail_server_plugin_describes_itself():
    # when
    manifest = describe(mail_server.PLUGIN, icon=True)
    fields = {f.key: f for f in mail_server.PLUGIN.fields}

    # then
    assert json.loads(json.dumps(manifest, allow_nan=False)) == manifest
    assert (manifest['id'], manifest['name'], manifest['category'], manifest['maxInstances']) == ('mail-server', 'Mail server', 'chat', 1)
    assert manifest['capabilities']['secrets'] == ['password', 'client-cert', 'client-secret'] and mail.PLUGIN_ID == mail_server.ID
    assert manifest['capabilities']['hosts'] == ['@authority', '@graph-url']
    assert list(fields) == ['method', 'host', 'port', 'security', 'tenant-id', 'username', 'password', 'client-id', 'client-cert', 'client-secret', 'from', 'sender', 'reply-to', 'authority', 'graph-url']
    assert (fields['method'].choices, fields['method'].default, fields['method'].choice_labels['graph']) == (('smtp', 'graph'), 'smtp', 'Microsoft 365 (Graph)')
    assert fields['host'].required and fields['from'].required and fields['password'].kind == 'secret'
    assert (fields['port'].kind, fields['port'].default, fields['port'].low, fields['port'].high) == ('int', '587', 1, 65535)
    assert (fields['security'].choices, fields['security'].default) == (('starttls', 'tls', 'none'), 'starttls')
    assert 'skopax.plugins.mail_server' in BUILTIN and Registry.of(mail_server.PLUGIN).get('mail-server').state == 'ok'


def test_the_address_fields_are_checked_on_save():
    # given
    fields = {f.key: f for f in mail_server.PLUGIN.fields}

    # then
    assert fields['from'].check('Skopax <alerts@example.com>') is None and fields['from'].check('alerts') == msg('mailserver.from')
    assert fields['reply-to'].check('ops@example.com') is None and fields['reply-to'].check('ops@') == msg('mailserver.from')
    assert fields['host'].check('smtp.example.com') is None and fields['host'].check('smtp://smtp.example.com') == msg('mailserver.host')


def test_test_connection_signs_in_and_says_so(store, monkeypatch):
    # given
    fake = faked(monkeypatch)

    # when
    answer = probed(store)

    # then
    assert (answer['ok'], answer['summary']) == (True, msg('mailserver.ok_signed', host='smtp.example.com', port=587, security='STARTTLS', user='alerts@example.com'))
    assert fake.made[-1].steps == ['connect', 'ehlo', 'starttls', 'ehlo', 'login', 'noop', 'quit']


def test_test_connection_without_username_does_not_sign_in(store, monkeypatch):
    # given
    fake = faked(monkeypatch)

    # when
    answer = probed(store, security='tls', port='465', username='', password='')

    # then
    assert (answer['ok'], answer['summary']) == (True, msg('mailserver.ok', host='smtp.example.com', port=465, security='TLS'))
    assert fake.made[-1].ssl and fake.made[-1].steps == ['connect', 'ehlo', 'noop', 'quit']


@pytest.mark.parametrize(('fail', 'key'), [
    ({'connect': ConnectionRefusedError()}, 'mailserver.connect'),
    ({'connect': socket.timeout('timed out')}, 'mailserver.connect'),
    ({'starttls': ssl.SSLCertVerificationError('certificate verify failed')}, 'mailserver.tls'),
    ({'starttls': smtplib.SMTPNotSupportedError('no')}, 'mailserver.tls'),
    ({'login': smtplib.SMTPAuthenticationError(535, f'nope {PASSWORD}'.encode())}, 'mailserver.login'),
    ({'noop': smtplib.SMTPResponseException(554, b'go away')}, 'mailserver.refused'),
])
def test_test_connection_says_in_plain_words_what_failed(store, monkeypatch, fail, key):
    # given
    faked(monkeypatch, **fail)
    heads = {'mailserver.connect': 'Could not connect', 'mailserver.tls': 'TLS failed', 'mailserver.login': 'The mail server refused the sign-in', 'mailserver.refused': 'The mail server refused'}

    # when
    answer = probed(store)

    # then
    assert answer['ok'] is False and answer['summary'].startswith(heads[key]) and PASSWORD not in answer['summary']


def test_test_connection_refuses_plain_to_a_public_host_without_connecting(store, monkeypatch):
    # given
    fake = faked(monkeypatch)
    monkeypatch.setattr(socket, 'getaddrinfo', lambda host, *a, **kw: [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('93.184.215.14', 0))])

    # when
    answer = probed(store, security='none')

    # then
    assert (answer['ok'], answer['summary']) == (False, msg('mailserver.plain_public', host='smtp.example.com'))
    assert fake.made == []


def test_a_second_mail_server_is_refused(store):
    # given
    runtime = runtime_of(store, ('main', CONFIG))

    # when
    with pytest.raises(InvalidInstance) as refused:
        runtime.instances.create('mail-server', 'backup', CONFIG | {'host': 'smtp2.example.com'}, True, 'alice@example.com')

    # then
    assert refused.value.text == msg('instances.max_one')


def test_mailer_is_the_enabled_mail_server_or_none(store, monkeypatch):
    # given
    fake = faked(monkeypatch)
    configured = runtime_of(store, ('main', CONFIG))

    # when
    found = mail.mailer(configured)
    found.send(['alice@example.com'], 'Hi', 'plain')

    # then
    assert found.ready() and fake.made[-1].login_as == ('alerts@example.com', PASSWORD) and fake.made[-1].port == 587
    store.db['instances'].update_one({'_id': 'mail-server/main'}, {'$set': {'enabled': False}})
    configured.refresh()
    assert mail.mailer(configured) is None


def test_mailer_fills_the_defaults_of_a_stored_instance(store, monkeypatch):
    # given
    fake = faked(monkeypatch)
    runtime = runtime_of(store, ('main', {'host': 'smtp.example.com', 'from': 'alerts@example.com'}))

    # when
    mail.mailer(runtime).send(['alice@example.com'], 'Hi', 'plain')

    # then
    assert fake.made[-1].port == 587 and fake.made[-1].steps == ['connect', 'ehlo', 'starttls', 'ehlo', 'send', 'quit']


def test_a_mail_server_that_is_not_ready_gives_no_mailer(store):
    # given
    runtime = runtime_of(store, ('main', CONFIG | {'password': 'p\u00e4ss'}))

    # then
    assert runtime.instances_of('mail-server') and mail.mailer(runtime) is None


def test_test_connection_refuses_a_non_ascii_password_in_plain_words(store, monkeypatch):
    # given
    fake = faked(monkeypatch)

    # when
    answer = probed(store, password='p\u00e4ss')

    # then
    assert (answer['ok'], answer['summary']) == (False, msg('mailserver.sign_in_ascii')) and fake.made == []


def test_no_mail_server_means_no_mailer(store):
    # then
    assert mail.mailer(runtime_of(store)) is None


def test_the_password_is_never_shown_in_an_instance_read(store):
    # given
    runtime = runtime_of(store, ('main', CONFIG))

    # when
    shown = json.dumps(runtime.instances.get('mail-server', 'main').view())

    # then
    assert PASSWORD not in shown and json.loads(shown)['secrets']['password'] == {'set': True, 'from': None}
    assert not json.loads(shown)['secrets']['client-cert']['set'] and not json.loads(shown)['secrets']['client-secret']['set']
