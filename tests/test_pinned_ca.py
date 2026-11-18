import datetime as dt
import http.server
import ipaddress
import json
import select
import socket
import ssl
import threading
import urllib.error
import urllib.request
from contextlib import contextmanager
from dataclasses import dataclass

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from skopax import probe, remote, runtime
from skopax.instances import Instances, PluginState, visible
from skopax.messages import msg
from skopax.plugins import airbyte, argocd, elasticsearch, gitlab, grafana, langfuse, pmm, prometheus, umami
from skopax.registry import Registry
from skopax.runtime import Runtime
from skopax.sealer import Sealer
from tests.plugin_kit import KIT_KEY, add_instance

NOW = dt.datetime.now(dt.timezone.utc)
HOST = '127.0.0.1'
BY = 'admin@example.com'
MARK = b'GET /pinned-request HTTP/1.0\r\n\r\n'
WEBSITE_ID = '6f1c2a90-0b8e-4d4e-9a52-6a2b8f4d1c11'
ANSWERS = {'/api/v4/version': {'version': '17.5.0'}, '/api/v4/groups/shop/projects': [], '/api/version': {'Version': 'v2.13.0'},
           '/api/v1/applications': {'items': [{'metadata': {'name': 'checkout-service'}}]}, f'/api/websites/{WEBSITE_ID}': {'domain': 'shop.example.com'}}
SECRET = 'pinned-test-token'
PINNED_TESTS = {'gitlab': (gitlab.PLUGIN, {'token': SECRET, 'group': 'shop'}), 'argocd': (argocd.PLUGIN, {'token': SECRET}),
                'umami': (umami.PLUGIN, {'token': SECRET, 'website-id': WEBSITE_ID})}


@dataclass(frozen=True)
class Made:
    cert: x509.Certificate | None
    key: ec.EllipticCurvePrivateKey

    @property
    def pem(self) -> str:
        return self.cert.public_bytes(serialization.Encoding.PEM).decode()

    @property
    def secret(self) -> str:
        return self.key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode()


def made(name: str = HOST, days: tuple[int, int] = (-1, 30), issuer: Made | None = None, ca: bool | None = None, san: bool = True, aki: bool = True) -> Made:
    key = ec.generate_private_key(ec.SECP256R1())
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name)])
    builder = (x509.CertificateBuilder().subject_name(subject).issuer_name(issuer.cert.subject if issuer else subject).public_key(key.public_key())
               .serial_number(x509.random_serial_number()).not_valid_before(NOW + dt.timedelta(days=days[0])).not_valid_after(NOW + dt.timedelta(days=days[1])))
    if san:
        try:
            alt = x509.IPAddress(ipaddress.ip_address(name))
        except ValueError:
            alt = x509.DNSName(name)
        builder = builder.add_extension(x509.SubjectAlternativeName([alt]), critical=False)
    if ca is not None:
        builder = builder.add_extension(x509.BasicConstraints(ca=ca, path_length=None), critical=True)
    if ca:
        builder = builder.add_extension(x509.KeyUsage(False, False, False, False, False, True, True, False, False), critical=True)
        builder = builder.add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
    if issuer is not None and aki:
        builder = builder.add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(issuer.key.public_key()), critical=False)
    return Made(builder.sign((issuer or Made(None, key)).key, hashes.SHA256()), key)


class Answer(http.server.BaseHTTPRequestHandler):
    def answer(self) -> object:
        return {'ok': True}

    def do_GET(self):
        body = json.dumps(self.answer()).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


class ByPath(Answer):
    def answer(self) -> object:
        return ANSWERS.get(self.path.split('?')[0])


def serving(leaf: Made, tmp_path) -> ssl.SSLContext:
    (tmp_path / 'cert.pem').write_text(leaf.pem)
    (tmp_path / 'key.pem').write_text(leaf.secret)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(tmp_path / 'cert.pem', tmp_path / 'key.pem')
    return context


@dataclass(frozen=True)
class Heard:
    raw: bytearray
    plain: bytearray

    def got(self, data: bytes) -> bool:
        return data in self.raw or data in self.plain


def listen(context: ssl.SSLContext, listener: socket.socket, heard: Heard, stop: threading.Event) -> None:
    while True:
        try:
            conn, _ = listener.accept()
        except TimeoutError:
            if stop.is_set():
                return
            continue
        except OSError:
            return
        incoming, outgoing = ssl.MemoryBIO(), ssl.MemoryBIO()
        tls = context.wrap_bio(incoming, outgoing, server_side=True)
        with conn:
            conn.settimeout(5)
            while True:
                try:
                    data = conn.recv(65536)
                except OSError:
                    break
                if not data:
                    break
                heard.raw.extend(data)
                incoming.write(data)
                try:
                    tls.do_handshake()
                    while chunk := tls.read(65536):
                        heard.plain.extend(chunk)
                except ssl.SSLError:
                    pass
                try:
                    conn.sendall(outgoing.read())
                except OSError:
                    pass


@contextmanager
def recorded(leaf: Made, tmp_path):
    heard, stop = Heard(bytearray(), bytearray()), threading.Event()
    listener = socket.create_server((HOST, 0))
    listener.settimeout(0.1)
    thread = threading.Thread(target=listen, args=(serving(leaf, tmp_path), listener, heard, stop), daemon=True)
    thread.start()
    try:
        yield listener.getsockname()[1], heard
    finally:
        stop.set()
        thread.join(10)
        listener.close()


@contextmanager
def served(leaf: Made, tmp_path, handler: type[Answer] = Answer):
    context = serving(leaf, tmp_path)
    server = http.server.HTTPServer((HOST, 0), handler)
    server.socket = context.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f'https://{HOST}:{server.server_address[1]}/'
    finally:
        server.shutdown()
        server.server_close()


def reads(url: str, pem: str, pinned: bool) -> bool:
    try:
        runtime.pem_transport(pem, pinned)('GET', url, {}, None, 5)
    except (remote.HttpError, remote.Unreachable):
        return False
    return True


def refusal(url: str, pem: str, pinned: bool, follow: bool = True) -> str | None:
    try:
        with (remote.open_url if follow else remote.open_plain)(urllib.request.Request(url), 5, remote.tls_context(pem, pinned)):
            return None
    except urllib.error.URLError as e:
        assert isinstance(e.reason, ssl.SSLError), e.reason
        return str(e.reason)


def probed(url: str, pem: str) -> dict:
    return probe.check({'name': 'pmm-main', 'plugin': 'pmm', 'instance': 'main', 'url': url, 'pem': pem, 'pinned': True}, timeout=5)


def prometheus_runtime(store, plugin=prometheus.PLUGIN) -> Runtime:
    registry = Registry.of(plugin)
    rt = Runtime(registry, PluginState(store.db), Instances(store.db, registry, lambda: Sealer(KIT_KEY)))
    rt.refresh()
    return rt


@pytest.mark.parametrize('leaf', [
    {'name': 'other.example.com'},
    {'days': (-30, -1)},
    {'san': False},
    {'name': 'other.example.com', 'ca': False},
    {'ca': True, 'days': (-30, -1)},
], ids=['wrong-name', 'expired', 'no-san', 'wrong-name-not-a-ca', 'expired-ca-true'])
def test_the_pasted_server_certificate_passes_as_it_is_whatever_its_name_age_or_shape(tmp_path, leaf):
    # given
    server = made(**leaf)

    # when
    with served(server, tmp_path) as url:
        plain, pinned = reads(url, server.pem, False), reads(url, server.pem, True)
        status = probe.http_status(url, probe.pinned(server.pem, True), 5)
        checked = probed(url, server.pem)

    # then
    assert (plain, pinned) == (False, True)
    assert status == 200
    assert (checked['ok'], checked['error']) == (True, None)


def test_a_ca_issued_leaf_without_an_authority_key_id_passes_when_it_is_the_pasted_one(tmp_path):
    # given
    authority = made('Example Private CA', ca=True)
    server = made('pmm.example.com', issuer=authority, ca=False, aki=False)

    # when
    with served(server, tmp_path) as url:
        pinned = reads(url, server.pem, True)

    # then
    assert pinned is True


def test_with_a_ca_pasted_and_the_pin_on_no_certificate_from_that_ca_passes(tmp_path):
    # given
    authority = made('Example Private CA', ca=True)
    own = made(HOST, issuer=authority, ca=False)
    sibling = made('evil.example.org', issuer=authority, ca=False)
    stale = made(HOST, days=(-30, -1), issuer=authority, ca=False)

    # when
    with served(own, tmp_path) as url:
        anchor = refusal(url, authority.pem, False)
        own_pinned = refusal(url, authority.pem, True)
    with served(sibling, tmp_path) as url:
        sibling_pinned = refusal(url, authority.pem, True, follow=False)
        sibling_checked = probed(url, authority.pem)
    with served(stale, tmp_path) as url:
        stale_pinned = reads(url, authority.pem, True)

    # then
    assert anchor is None, 'with the pin off the CA is a trust anchor with full checks, as before'
    assert own_pinned == sibling_pinned == remote.PIN_MISMATCH
    assert (sibling_checked['ok'], sibling_checked['error']) == (False, probe.UNTRUSTED)
    assert stale_pinned is False


def test_a_different_self_signed_certificate_is_refused_on_every_path(tmp_path, store):
    # given
    server = made(HOST, ca=True)
    other = made(HOST, ca=True)
    rt = prometheus_runtime(store)

    # when
    with served(server, tmp_path) as url:
        sent = reads(url, other.pem, True)
        direct = refusal(url, other.pem, True), refusal(url, other.pem, True, follow=False)
        checked = probed(url, other.pem)
        tested = rt.connection_test(prometheus.PLUGIN, None, {'url': url, 'ca': other.pem, 'ca-pinned': 'true'}, 5, NOW.timestamp())
        control = rt.connection_test(prometheus.PLUGIN, None, {'url': url, 'ca': server.pem, 'ca-pinned': 'true'}, 5, NOW.timestamp())

    # then
    assert sent is False
    assert direct == (remote.PIN_MISMATCH, remote.PIN_MISMATCH)
    assert (checked['ok'], checked['error']) == (False, probe.UNTRUSTED)
    assert (tested['ok'], tested['summary']) == (False, msg('connection.tls'))
    assert control['ok'] is True, control


def test_a_pasted_bundle_lets_only_its_own_certificates_through(tmp_path):
    # given
    authority = made('Example Private CA', ca=True)
    first, second = made('one.example.com', ca=False), made('two.example.com', ca=False)
    issued = made(HOST, issuer=authority, ca=False)
    bundle = authority.pem + first.pem + second.pem

    # when
    with served(first, tmp_path) as url:
        one = refusal(url, bundle, True)
    with served(second, tmp_path) as url:
        two = refusal(url, bundle, True)
    with served(issued, tmp_path) as url:
        child = refusal(url, bundle, True)

    # then
    assert (one, two) == (None, None)
    assert child == remote.PIN_MISMATCH


def sent(tls: ssl.SSLSocket) -> None:
    for _ in range(50):
        try:
            tls.sendall(MARK)
            return
        except ssl.SSLWantReadError:
            select.select([tls], [], [], 5)
        except ssl.SSLWantWriteError:
            select.select([], [tls], [], 5)


def blocking(context: ssl.SSLContext, port: int) -> None:
    with socket.create_connection((HOST, port), timeout=5) as sock:
        with context.wrap_socket(sock, server_hostname=HOST) as tls:
            tls.sendall(MARK)


def memory_bio_write_first(context: ssl.SSLContext, port: int) -> None:
    with socket.create_connection((HOST, port), timeout=5) as conn:
        incoming, outgoing = ssl.MemoryBIO(), ssl.MemoryBIO()
        tls = context.wrap_bio(incoming, outgoing, server_hostname=HOST)
        try:
            for _ in range(20):
                try:
                    tls.write(MARK)
                    return
                except ssl.SSLWantReadError:
                    conn.sendall(outgoing.read())
                    incoming.write(conn.recv(65536))
        finally:
            conn.sendall(outgoing.read())


def non_blocking_connect_ex(context: ssl.SSLContext, port: int) -> None:
    with socket.socket() as sock:
        sock.setblocking(False)
        with context.wrap_socket(sock, server_hostname=HOST) as tls:
            tls.connect_ex((HOST, port))
            select.select([], [tls], [], 5)
            sent(tls)


def refused_connect_then_send(context: ssl.SSLContext, port: int) -> None:
    with socket.socket() as sock:
        sock.settimeout(5)
        with context.wrap_socket(sock, server_hostname=HOST) as tls:
            try:
                tls.connect((HOST, port))
            except ssl.SSLError:
                pass
            tls.sendall(MARK)


def connected_non_blocking(context: ssl.SSLContext, port: int) -> None:
    with socket.create_connection((HOST, port), timeout=5) as sock:
        sock.setblocking(False)
        with context.wrap_socket(sock, server_hostname=HOST, do_handshake_on_connect=False) as tls:
            sent(tls)


PATHS = {'memory-bio-write-first': memory_bio_write_first, 'non-blocking-connect-ex': non_blocking_connect_ex,
         'refused-connect-then-send': refused_connect_then_send, 'connected-non-blocking': connected_non_blocking}


def attempt(path, context: ssl.SSLContext, port: int) -> BaseException | None:
    try:
        path(context, port)
    except (OSError, ValueError) as e:
        return e
    return None


def test_the_recorder_hears_a_request_that_the_matching_pin_lets_through_and_nothing_else(tmp_path):
    # given
    server, other = made(HOST, ca=True), made(HOST, ca=True)

    # when
    with recorded(server, tmp_path) as (port, matching):
        passed = attempt(blocking, remote.tls_context(server.pem, True), port)
    with recorded(server, tmp_path) as (port, refused):
        failed = attempt(blocking, remote.tls_context(other.pem, True), port)

    # then
    assert passed is None and MARK in matching.plain
    assert str(failed) == remote.PIN_MISMATCH and not refused.got(MARK)


@pytest.mark.parametrize('path', PATHS)
def test_a_pinned_context_lets_no_request_byte_out_on_any_way_around_its_check(tmp_path, path):
    # given
    server, other = made(HOST, ca=True), made(HOST, ca=True)
    context = remote.tls_context(other.pem, True)

    # when
    with recorded(server, tmp_path) as (port, heard):
        error = attempt(PATHS[path], context, port)

    # then
    assert not heard.got(MARK), bytes(heard.plain or heard.raw[-200:])
    assert str(error) == remote.PIN_SOCKET, repr(error)


def test_a_pinned_context_checks_a_handshake_that_was_put_off(tmp_path):
    # given
    server = made(HOST, ca=True)
    other = made(HOST, ca=True)
    context = remote.tls_context(other.pem, True)

    # when
    with served(server, tmp_path) as url:
        port = int(url.rstrip('/').rsplit(':', 1)[1])
        with socket.create_connection((HOST, port), timeout=5) as raw:
            with pytest.raises(ssl.SSLCertVerificationError) as late:
                with context.wrap_socket(raw, server_hostname=HOST, do_handshake_on_connect=False) as tls:
                    tls.sendall(b'GET / HTTP/1.0\r\n\r\n')

    # then
    assert str(late.value) == remote.PIN_MISMATCH


def test_an_empty_ca_with_the_pin_stored_gives_the_default_context(store):
    # given
    add_instance(store, prometheus.PLUGIN, 'main', sealer_key=KIT_KEY, url='https://prometheus.example.com', **{'ca-pinned': 'true'})
    rt = prometheus_runtime(store)

    # when
    found = runtime.ca_of(prometheus.PLUGIN, {'ca': '', 'ca-pinned': 'true'})
    transport = rt.transport_for(prometheus.PLUGIN, rt.instances.get('prometheus', 'main').info())

    # then
    assert found == ('', False)
    assert transport is remote.send


def test_saving_with_an_empty_ca_drops_a_stored_pin(store):
    # given
    server = made(HOST, ca=True)
    instances = prometheus_runtime(store).instances
    instances.create('prometheus', 'main', {'url': 'https://prometheus.example.com', 'ca': server.pem, 'ca-pinned': 'true'}, True, BY)
    instances.create('prometheus', 'east', {'url': 'https://east.example.com', 'ca': server.pem, 'ca-pinned': 'true'}, True, BY)

    def stored(name: str) -> dict:
        return store.db['instances'].find_one({'_id': f'prometheus/{name}'})['values']

    # when
    cleared = instances.update('prometheus', 'main', {'ca': ''}, BY).values, stored('main')
    both = instances.update('prometheus', 'east', {'ca': '', 'ca-pinned': 'true'}, BY).values, stored('east')
    bare = instances.create('prometheus', 'west', {'url': 'https://west.example.com', 'ca-pinned': 'true'}, True, BY).values, stored('west')
    kept = instances.update('prometheus', 'main', {'ca': server.pem, 'ca-pinned': 'true'}, BY).values, stored('main')

    # then
    assert all((shown['ca-pinned'], 'ca-pinned' in held) == ('false', False) for shown, held in (cleared, both, bare)), (cleared, both, bare)
    assert (kept[0]['ca-pinned'], kept[1]['ca-pinned']) == ('true', 'true')


def test_every_plugin_with_a_ca_offers_the_pin_only_when_a_ca_is_set():
    # given
    plugins = (pmm, elasticsearch, prometheus, grafana, airbyte, langfuse, gitlab, argocd, umami)

    # when
    found = {p.PLUGIN.id: next((f for f in p.PLUGIN.fields if f.key == 'ca-pinned'), None) for p in plugins}

    # then
    assert all(f is not None and f.kind == 'bool' and f.default == 'false' and f.show_if == ('ca', '*') for f in found.values()), found
    assert all("paste the server's own certificate; with the switch on, only that exact certificate is accepted, even if it is expired, self-signed or names another host" in f.help
               and 'A CA certificate does not work with the switch on' in f.help for f in found.values()), found
    field = found['pmm']
    fields = {f.key: f for f in pmm.PLUGIN.fields}
    assert not visible(field, {}, fields) and visible(field, {'ca': '-----BEGIN CERTIFICATE-----'}, fields)


def test_a_saved_form_with_the_pin_on_reaches_a_server_whose_certificate_has_the_wrong_name(store, tmp_path):
    # given
    server = made('other.example.com', ca=True)
    rt = prometheus_runtime(store)

    # when
    with served(server, tmp_path) as url:
        off = rt.connection_test(prometheus.PLUGIN, None, {'url': url, 'ca': server.pem}, 5, NOW.timestamp())
        on = rt.connection_test(prometheus.PLUGIN, None, {'url': url, 'ca': server.pem, 'ca-pinned': 'true'}, 5, NOW.timestamp())

    # then
    assert (off['ok'], off['summary']) == (False, msg('connection.tls'))
    assert on['ok'] is True, on


def test_a_pin_beside_a_ca_that_a_settings_source_supplies_survives_later_saves(store):
    # given
    from skopax.contract import SourceData
    server = made(HOST, ca=True)
    registry = Registry.of(prometheus.PLUGIN)
    sources = [SourceData('file', {}, ({'plugin': 'prometheus', 'name': 'main', 'url': 'https://prometheus.example.com', 'ca': server.pem},))]
    instances = Instances(store.db, registry, lambda: Sealer(KIT_KEY), sources=lambda: sources)
    instances.refresh()

    # when
    pinned = instances.update('prometheus', 'main', {'ca-pinned': 'true'}, BY).values
    later = instances.update('prometheus', 'main', {'namespaces': 'shop'}, BY).values
    stored = store.db['instances'].find_one({'_id': 'prometheus/main'})['values']

    # then
    assert (pinned['ca-pinned'], later['ca-pinned'], stored.get('ca-pinned')) == ('true', 'true', 'true')
    assert runtime.ca_of(prometheus.PLUGIN, later) == (server.pem, True)


def test_clearing_a_saved_ca_keeps_the_pin_when_a_settings_source_still_supplies_one(store):
    # given
    from skopax.contract import SourceData
    supplied, saved = made(HOST, ca=True), made(HOST, ca=True)
    registry = Registry.of(prometheus.PLUGIN)
    sources = [SourceData('file', {}, ({'plugin': 'prometheus', 'name': 'main', 'url': 'https://prometheus.example.com', 'ca': supplied.pem},))]
    instances = Instances(store.db, registry, lambda: Sealer(KIT_KEY), sources=lambda: sources)
    instances.refresh()
    instances.update('prometheus', 'main', {'ca': saved.pem, 'ca-pinned': 'true'}, BY)

    # when
    after = instances.update('prometheus', 'main', {'ca': ''}, BY).values
    stored = store.db['instances'].find_one({'_id': 'prometheus/main'})['values']

    # then
    assert (after['ca'], after['ca-pinned']) == (supplied.pem, 'true')
    assert (stored.get('ca'), stored.get('ca-pinned')) == (None, 'true')
    assert runtime.ca_of(prometheus.PLUGIN, after) == (supplied.pem, True)


def test_a_pinned_socket_that_the_wrap_did_not_check_is_never_handed_out(tmp_path, monkeypatch):
    # given
    server, other = made(HOST, ca=True), made(HOST, ca=True)
    context = remote.tls_context(other.pem, True)
    monkeypatch.setattr(remote, 'connected', lambda sock: True)

    # when
    with recorded(server, tmp_path) as (port, heard):
        error = attempt(refused_connect_then_send, context, port)

    # then
    assert not heard.got(MARK), bytes(heard.plain or heard.raw[-200:])
    assert str(error) == remote.PIN_SOCKET, repr(error)


@pytest.mark.parametrize('name', sorted(PINNED_TESTS))
def test_gitlab_argo_cd_and_umami_with_the_pin_on_reach_a_server_whose_pasted_certificate_has_the_wrong_name(store, tmp_path, name):
    # given
    plugin, values = PINNED_TESTS[name]
    server = made('other.example.com', ca=True)
    rt = prometheus_runtime(store, plugin)

    # when
    with served(server, tmp_path, ByPath) as url:
        given = {**values, 'url': url.rstrip('/'), 'ca': server.pem}
        off = rt.connection_test(plugin, None, given, 5, NOW.timestamp())
        on = rt.connection_test(plugin, None, {**given, 'ca-pinned': 'true'}, 5, NOW.timestamp())

    # then
    assert (off['ok'], off['summary']) == (False, msg('connection.tls'))
    assert on['ok'] is True, on


@pytest.mark.parametrize('name', sorted(PINNED_TESTS))
def test_gitlab_argo_cd_and_umami_with_the_pin_on_send_no_request_byte_to_any_other_certificate(store, tmp_path, name):
    # given
    plugin, values = PINNED_TESTS[name]
    authority = made('Example Private CA', ca=True)
    issued, other = made(HOST, issuer=authority, ca=False), made(HOST, ca=True)
    rt = prometheus_runtime(store, plugin)

    def tested(url: str, pem: str, pinned: str) -> dict:
        return rt.connection_test(plugin, None, {**values, 'url': url, 'ca': pem, 'ca-pinned': pinned}, 5, NOW.timestamp())

    # when
    with served(issued, tmp_path, ByPath) as url:
        anchored = tested(url.rstrip('/'), authority.pem, 'false')
    refused = {}
    for label, server, pem in (('issued by the pasted CA', issued, authority.pem), ('another self-signed', other, made(HOST, ca=True).pem)):
        with recorded(server, tmp_path) as (port, heard):
            refused[label] = tested(f'https://{HOST}:{port}', pem, 'true'), heard

    # then
    assert anchored['ok'] is True, anchored
    for label, (result, heard) in refused.items():
        assert (result['ok'], result['summary']) == (False, msg('connection.tls')), label
        assert not heard.plain and SECRET.encode() not in heard.raw, label
