import http.client
import http.server
import io
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

import pytest

from skopax import remote
from skopax.remote import HttpError, Unreachable, call
from tests.test_pinned_ca import made, recorded, served as pinned_site
from tests.fake_http import FakeHttp


class Answer(io.BytesIO):
    headers = {'X-Next-Page': '2'}

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_send_parses_json_and_lowers_the_header_names(monkeypatch):
    # given
    seen = {}

    def urlopen(req, timeout, context=None):
        seen.update(url=req.full_url, auth=req.get_header('Authorization'), method=req.get_method())
        return Answer(b'{"ok": true}')

    monkeypatch.setattr(remote, 'open_url', urlopen)

    # when
    reply = remote.call(remote.send, 'POST', 'https://api.example.test/x?page=1', {'Authorization': 'Bearer t'}, {'a': 1})

    # then
    assert reply.body == {'ok': True}
    assert reply.headers == {'x-next-page': '2'}
    assert seen == {'url': 'https://api.example.test/x?page=1', 'auth': 'Bearer t', 'method': 'POST'}


@pytest.mark.parametrize('raised, error, text', [
    (urllib.error.HTTPError('https://api.example.test/x', 403, 'Forbidden', {}, None), HttpError, 'HTTP 403 on https://api.example.test/x'),
    (urllib.error.URLError('timed out'), Unreachable, 'https://api.example.test/x unreachable (timed out)'),
])
def test_send_turns_failures_into_short_errors_without_the_query(monkeypatch, raised, error, text):
    # given
    def urlopen(req, timeout, context=None):
        raise raised

    monkeypatch.setattr(remote, 'open_url', urlopen)

    # then
    with pytest.raises(error, match=f'^{text}$'.replace('(', r'\(').replace(')', r'\)')):
        remote.send('GET', 'https://api.example.test/x?private=1', {}, None, 5)


def test_an_answer_without_json_is_a_source_error(monkeypatch):
    # given
    monkeypatch.setattr(remote, 'open_url', lambda req, timeout, context=None: Answer(b'<html>login</html>'))

    # then
    with pytest.raises(Unreachable, match='answered without JSON'):
        remote.send('GET', 'https://api.example.test/x', {}, None, 5)


class Site(http.server.BaseHTTPRequestHandler):
    seen: list = []
    names: list = []

    def do_GET(self):
        type(self).seen.append((self.path, self.headers.get(remote.PAGE_READ), self.headers.get('User-Agent')))
        type(self).names.append({k.lower() for k in self.headers.keys()})
        if self.path == '/old':
            self.send_response(302)
            self.send_header('Location', '/api/token')
            self.send_header('Content-Length', '0')
            self.end_headers()
            return
        body = b'<title>Not found</title>' + b'x' * 5000 if self.path == '/gone' else b'<title>Home</title>' + b'y' * 5000
        self.send_response(404 if self.path == '/gone' else 200)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.send_header('CF-Cache-Status', 'HIT')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        return None


@pytest.fixture
def site():
    Site.seen = []
    Site.names = []
    server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Site)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f'http://127.0.0.1:{server.server_address[1]}'
    server.shutdown()
    server.server_close()


def test_a_page_read_through_the_real_sender_never_follows_a_redirect_and_keeps_status_headers_and_a_capped_body(site):
    # when
    moved = remote.call_page(remote.send, f'{site}/old', {'User-Agent': 'page-check'}, 5, 100)
    gone = remote.call_page(remote.send, f'{site}/gone', None, 5, 100)
    home = remote.call_page(remote.send, f'{site}/', None, 5, 100)

    # then
    assert (moved.status, moved.headers.get('location'), moved.body) == (302, '/api/token', b'')
    assert (gone.status, gone.headers['content-type'], gone.body[:24], len(gone.body)) == (404, 'text/html; charset=utf-8', b'<title>Not found</title>', 101)
    assert (home.status, home.headers['cf-cache-status'], len(home.body)) == (200, 'HIT', 101)
    assert [(path, marker) for path, marker, _ in Site.seen] == [('/old', None), ('/gone', None), ('/', None)], 'one request each, the marker never leaves the server'
    assert Site.seen[0][2] == 'page-check'


def test_a_page_read_keeps_the_sender_errors_without_the_query(monkeypatch):
    # given
    def dial(address, timeout=None, source_address=None):
        raise TimeoutError('timed out')

    monkeypatch.setattr(remote.socket, 'create_connection', dial)
    monkeypatch.setattr(remote, 'addresses', lambda host, timeout=5.0: ['203.0.113.9'])
    monkeypatch.setattr(remote, 'open_plain', lambda *a, **k: pytest.fail('a page read goes out on its own connection'))
    monkeypatch.setattr(remote, 'open_url', lambda *a, **k: pytest.fail('a page read never follows redirects'))

    # when
    with pytest.raises(Unreachable) as e:
        remote.call_page(remote.send, 'https://shop.example.com/x?token=secret', None, 5)

    # then
    assert str(e.value) == 'https://shop.example.com/x unreachable (TimeoutError)'


def test_no_marker_header_ever_reaches_the_network(site, monkeypatch):
    # given
    sent = []

    def urlopen(req, timeout, context=None):
        sent.append({k.lower() for k, _ in req.header_items()})
        return Answer(b'{"ok": true}')

    monkeypatch.setattr(remote, 'open_url', urlopen)

    # when
    remote.call(remote.send, 'GET', 'https://api.example.test/x', {'X-Skopax-Trace': '1', 'x-skopax-other': '2', 'X-Ok': 'y'})
    remote.call_page(remote.send, f'{site}/', {'X-Skopax-Trace': '1', 'X-Ok': 'y'}, 5, 100)

    # then
    assert sent == [{'accept', 'x-ok'}]
    assert not any(n.startswith('x-skopax-') for n in Site.names[0]) and 'x-ok' in Site.names[0]


def test_a_hook_or_admin_cannot_set_a_marker_header(store):
    # given
    from tests.test_plugin_runtime_guards import hook_runtime
    runtime = hook_runtime(store, lambda request: {'headers': {'X-Skopax-Page-Read': '1', 'x-skopax-anything': '2', 'X-Ok': 'fine'}})

    # when
    out = runtime.token_request({})

    # then
    assert out['headers'] == {'X-Ok': 'fine'}


def test_a_page_read_without_a_ca_verifies_the_site_certificate(tmp_path):
    # given
    import datetime as dt
    import ipaddress
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'shop.example.com')])
    now = dt.datetime.now(dt.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - dt.timedelta(days=1)).not_valid_after(now + dt.timedelta(days=1))
            .add_extension(x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address('127.0.0.1'))]), critical=False).sign(key, hashes.SHA256()))
    (tmp_path / 'c.pem').write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    (tmp_path / 'k.pem').write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    served = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    served.load_cert_chain(tmp_path / 'c.pem', tmp_path / 'k.pem')
    Site.seen = []
    server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Site)
    server.socket = served.wrap_socket(server.socket, server_side=True)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f'https://127.0.0.1:{server.server_address[1]}/'
    trusted = remote.sender(ssl.create_default_context(cadata=cert.public_bytes(serialization.Encoding.PEM).decode()))

    # when
    try:
        with pytest.raises(Unreachable):
            remote.call_page(remote.send, url, None, 5, 100)
        answered = remote.call_page(trusted, url, None, 5, 100)
    finally:
        server.shutdown()
        server.server_close()

    # then
    assert answered.status == 200 and [p for p, _, _ in Site.seen] == ['/']


def test_a_page_read_refuses_a_transport_that_answers_parsed_json():
    # given
    def parsed(method, url, headers, data, timeout):
        return remote.Reply({'ok': True}, {})

    # when
    with pytest.raises(Unreachable) as e:
        remote.call_page(parsed, 'https://shop.example.com/x?token=secret')

    # then
    assert str(e.value) == 'https://shop.example.com/x answered without a page'


def test_a_sender_hands_its_tls_context_to_every_request(monkeypatch):
    # given
    ctx = object()
    seen = []
    monkeypatch.setattr(remote, 'open_url', lambda req, timeout, context=None: seen.append(context) or Answer(b'{}'))

    # when
    remote.sender(ctx)('GET', 'https://pmm.example.test/x', {}, None, 5)
    remote.send('GET', 'https://api.example.test/x', {}, None, 5)

    # then
    assert seen == [ctx, None]


@pytest.mark.parametrize('old, new, kept', [
    ('https://grafana.example.test/api/x', 'https://grafana.example.test/api/x/', True),
    ('https://grafana.example.test:443/api/x', 'https://GRAFANA.example.test/login', True),
    ('http://grafana.example.test/api/x', 'https://grafana.example.test/api/x', True),
    ('https://grafana.example.test/api/x', 'https://sso.example.test/login', False),
    ('https://grafana.example.test/api/x', 'http://grafana.example.test/api/x', False),
    ('https://grafana.example.test/api/x', 'https://grafana.example.test:8443/api/x', False),
    ('http://grafana.example.test:8080/api/x', 'https://grafana.example.test/api/x', False),
])
def test_a_redirect_keeps_the_callers_headers_only_on_the_same_host_and_port(old, new, kept):
    # given
    req = urllib.request.Request(old, headers={'Authorization': 'Bearer t', 'X-Api-Key': 'k', 'Accept': 'application/json'})

    # when
    moved = remote.HostRedirect().redirect_request(req, None, 302, 'Found', {}, new)

    # then
    assert moved.full_url == new
    assert (moved.get_header('Authorization'), moved.get_header('X-api-key')) == (('Bearer t', 'k') if kept else (None, None))


def test_every_sender_follows_redirects_with_that_rule_and_sends_the_headers_on_the_first_hop(monkeypatch):
    # given
    sent = []
    monkeypatch.setattr(remote, 'open_url', lambda req, timeout, context=None: sent.append(req) or Answer(b'{}'))

    # when
    remote.send('GET', 'https://grafana.example.test/api/x', {'Authorization': 'Bearer t'}, None, 5)

    # then
    assert sent[0].get_header('Authorization') == 'Bearer t'
    for context in (None, ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)):
        assert [type(h) for h in remote.opener(context).handlers if isinstance(h, urllib.request.HTTPRedirectHandler)] == [remote.HostRedirect]


@pytest.mark.parametrize('url, broken', [
    ('https://prod-1.example.test/workflows/x?sig=SECRET', http.client.InvalidURL("URL can't contain control characters. '/workflows/x?sig=SECRET' (found at least ' ')")),
    ('https://prod-1.example.test/workflows/x?sig=SECRET', http.client.IncompleteRead(b'{"ok"')),
    ('prod-1.example.test/workflows/x?sig=SECRET', None),
])
def test_a_malformed_link_or_a_cut_answer_is_unreachable_without_the_query(monkeypatch, url, broken):
    # given
    def fail(req, timeout, context=None):
        raise broken or AssertionError('no request should go out')

    monkeypatch.setattr(remote, 'open_url', fail)

    # when
    with pytest.raises(Unreachable) as e:
        remote.send('POST', url, {}, b'{}', 5)

    # then
    assert str(e.value) == f"{url.split('?')[0]} unreachable ({type(broken).__name__ if broken else 'ValueError'})" and 'SECRET' not in str(e.value)


def test_an_unreachable_reason_that_is_an_error_is_named_by_its_type(monkeypatch):
    # given
    def urlopen(req, timeout, context=None):
        raise urllib.error.URLError(ConnectionRefusedError(61, 'refused by 10.0.0.7'))

    monkeypatch.setattr(remote, 'open_url', urlopen)

    # when
    with pytest.raises(Unreachable) as e:
        remote.send('GET', 'https://api.example.test/x', {}, None, 5)

    # then
    assert str(e.value) == 'https://api.example.test/x unreachable (ConnectionRefusedError)'


def test_an_opener_is_built_once_per_context_and_redirect_choice():
    # given
    context = remote.tls_context(made().pem)

    # when
    plain, pinned = remote.opener(), remote.opener(context)

    # then
    assert plain is remote.opener() and pinned is remote.opener(context)
    assert remote.opener(context, False) is remote.opener(context, False) is not pinned


def test_a_text_read_hands_back_the_answer_unparsed_through_the_same_sender(monkeypatch):
    # given
    seen = []

    def urlopen(req, timeout, context=None):
        seen.append((req.full_url, req.get_method(), req.get_header('Accept'), req.get_header('User-agent'), timeout))
        return Answer(b'{"ok": true}' if 'json' in req.get_header('Accept') else b")]}'\n[[\"sb.ssr\",1]]")

    monkeypatch.setattr(remote, 'open_url', urlopen)

    # when
    text = remote.call_text(remote.send, 'https://status.example.test/s?site=a', {'User-Agent': 'site-checks'}, 7)
    parsed = remote.call(remote.send, 'GET', 'https://status.example.test/j')

    # then
    assert text == ")]}'\n[[\"sb.ssr\",1]]"
    assert seen[0] == ('https://status.example.test/s?site=a', 'GET', remote.TEXT_ACCEPT, 'site-checks', 7) and 'json' not in remote.TEXT_ACCEPT
    assert parsed.body == {'ok': True} and seen[1][2] == 'application/json'


def test_a_text_read_keeps_the_senders_errors_without_the_query(monkeypatch):
    # given
    def urlopen(req, timeout, context=None):
        raise urllib.error.HTTPError('https://status.example.test/s', 429, 'Too Many', {}, None)

    monkeypatch.setattr(remote, 'open_url', urlopen)

    # when
    with pytest.raises(HttpError) as e:
        remote.call_text(remote.send, 'https://status.example.test/s?site=secret', None, 5)

    # then
    assert e.value.code == 429 and 'secret' not in str(e.value)


def test_a_text_read_takes_a_fake_transports_text_and_refuses_a_parsed_answer():
    # given
    http = FakeHttp({('GET', 'https://status.example.test/s'): lambda q, body: f"page for {q['site'][0]}", ('GET', 'https://status.example.test/j'): {'ok': True}})

    # when
    text = remote.call_text(http, 'https://status.example.test/s?site=a')
    with pytest.raises(Unreachable) as e:
        remote.call_text(http, 'https://status.example.test/j?x=secret')

    # then
    assert text == 'page for a' and http.calls[0][2]['Accept'] == remote.TEXT_ACCEPT
    assert str(e.value) == 'https://status.example.test/j answered without text'


def test_the_text_mode_is_chosen_by_its_marker_and_never_by_the_accept_header(monkeypatch):
    # given
    sent = []

    def urlopen(req, timeout, context=None):
        sent.append({k.lower(): v for k, v in req.header_items()})
        return Answer(b'<html>a page</html>')

    monkeypatch.setattr(remote, 'open_url', urlopen)

    # when
    with pytest.raises(Unreachable) as e:
        remote.call(remote.send, 'GET', 'https://status.example.test/s?site=a', {'Accept': 'text/html'})
    text = remote.call_text(remote.send, 'https://status.example.test/s?site=a')

    # then
    assert str(e.value) == 'https://status.example.test/s answered without JSON'
    assert text == '<html>a page</html>'
    assert not any(k.startswith('x-skopax-') for row in sent for k in row)


def test_a_text_read_refuses_an_answer_past_its_limit(monkeypatch):
    # given
    monkeypatch.setattr(remote, 'open_url', lambda req, timeout, context=None: Answer(b'x' * 101))

    # when
    with pytest.raises(Unreachable) as e:
        remote.call_text(remote.send, 'https://status.example.test/s?site=secret', None, 5, 100)
    kept = remote.call_text(remote.send, 'https://status.example.test/s?site=a', None, 5, 101)

    # then
    assert str(e.value) == 'https://status.example.test/s answered more than 100 bytes'
    assert len(kept) == 101 and remote.TEXT_LIMIT == 2 * 1024 * 1024


class Drip(http.server.BaseHTTPRequestHandler):
    stop = threading.Event()

    def do_GET(self):
        path = self.path.split('?')[0]
        try:
            if path == '/slow-head':
                self.wfile.write(b'HTTP/1.1 200 OK\r\n')
                self.wfile.flush()
                for _ in range(100):
                    if type(self).stop.wait(0.2):
                        return
                    self.wfile.write(b'X-Drip: 1\r\n')
                    self.wfile.flush()
                return
            if path == '/slow-json':
                self.send_response(200)
                self.send_header('Content-Length', '12')
                self.end_headers()
                for part in [b'"'] + [b'x'] * 10 + [b'"']:
                    if type(self).stop.wait(0.2):
                        return
                    self.wfile.write(part)
                    self.wfile.flush()
                return
            self.send_response(200)
            self.send_header('Content-Length', '1000')
            self.end_headers()
            for _ in range(1000):
                if type(self).stop.wait(0.2):
                    return
                self.wfile.write(b'x')
                self.wfile.flush()
        except OSError:
            return

    def log_message(self, *args):
        return None


@pytest.fixture
def drip():
    Drip.stop = threading.Event()
    server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Drip)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f'http://127.0.0.1:{server.server_address[1]}'
    Drip.stop.set()
    server.shutdown()
    server.server_close()


@pytest.mark.parametrize('path', ['/slow-body', '/slow-head'])
def test_a_page_read_that_trickles_stops_at_its_wall_clock_deadline(drip, path):
    # given
    started = time.monotonic()

    # when
    with pytest.raises(Unreachable) as e:
        remote.call_page(remote.send, f'{drip}{path}?token=secret', None, 0.5, 10_000)

    # then
    took = time.monotonic() - started
    assert str(e.value) == f'{drip}{path} gave no full answer within 1 s'
    assert took < 4, took


def tls_site(tmp_path, host: str):
    import datetime as dt
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, host)])
    now = dt.datetime.now(dt.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - dt.timedelta(days=1)).not_valid_after(now + dt.timedelta(days=1))
            .add_extension(x509.SubjectAlternativeName([x509.DNSName(host)]), critical=False).sign(key, hashes.SHA256()))
    (tmp_path / 'c.pem').write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    (tmp_path / 'k.pem').write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    served = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    served.load_cert_chain(tmp_path / 'c.pem', tmp_path / 'k.pem')
    return served, ssl.create_default_context(cadata=cert.public_bytes(serialization.Encoding.PEM).decode())


class Hosted(http.server.BaseHTTPRequestHandler):
    seen: list = []

    def do_GET(self):
        type(self).seen.append((self.path, self.headers.get('Host'), {k.lower() for k in self.headers.keys()}))
        body = b'<title>Pinned</title>'
        self.send_response(200)
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        return None


def test_a_pinned_page_read_connects_to_the_checked_address_and_keeps_the_name_for_tls_and_host(tmp_path, monkeypatch):
    # given
    import socket
    served, trusted = tls_site(tmp_path, 'shop.example.com')
    names = []
    served.sni_callback = lambda sock, name, ctx: names.append(name)
    Hosted.seen = []
    server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Hosted)
    server.socket = served.wrap_socket(server.socket, server_side=True)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    port = server.server_address[1]
    looked = []
    real = socket.getaddrinfo

    def lookup(host, *args, **kwargs):
        looked.append(host)
        if host == 'shop.example.com':
            raise socket.gaierror(8, 'no such name')
        return real(host, *args, **kwargs)

    monkeypatch.setattr(socket, 'getaddrinfo', lookup)

    # when
    try:
        with pytest.raises(Unreachable):
            remote.call_page(remote.sender(trusted), f'https://shop.example.com:{port}/p?x=1', None, 5, 100)
        answered = remote.call_page(remote.sender(trusted), f'https://shop.example.com:{port}/p?x=1', None, 5, 100, pin='127.0.0.1')
    finally:
        server.shutdown()
        server.server_close()

    # then
    assert answered.status == 200 and answered.body == b'<title>Pinned</title>'
    assert names == ['shop.example.com']
    assert [(p, h) for p, h, _ in Hosted.seen] == [('/p?x=1', f'shop.example.com:{port}')]
    assert not any(n.startswith('x-skopax-') for n in Hosted.seen[0][2])
    assert 'shop.example.com' in looked and looked.count('shop.example.com') == 1


def test_a_pinned_page_read_refuses_a_certificate_for_another_name(tmp_path):
    # given
    served, trusted = tls_site(tmp_path, 'other.example.com')
    server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Hosted)
    server.socket = served.wrap_socket(server.socket, server_side=True)
    threading.Thread(target=server.serve_forever, daemon=True).start()

    # when
    try:
        with pytest.raises(Unreachable) as e:
            remote.call_page(remote.sender(trusted), f'https://shop.example.com:{server.server_address[1]}/', None, 5, 100, pin='127.0.0.1')
    finally:
        server.shutdown()
        server.server_close()

    # then
    assert 'SSL' in str(e.value)


def test_a_pin_without_a_page_read_is_refused_before_any_request(monkeypatch):
    # given
    monkeypatch.setattr(remote, 'open_url', lambda *a, **k: pytest.fail('no request may go out'))

    # when
    with pytest.raises(Unreachable) as e:
        remote.send('GET', 'https://api.example.test/x?k=secret', {remote.PIN: '127.0.0.1'}, None, 5)

    # then
    assert str(e.value) == 'https://api.example.test/x: a pinned address is only for a page read'


def test_addresses_lists_each_address_once_and_gives_up_after_its_time_limit(monkeypatch):
    # given
    import socket
    release = threading.Event()

    def lookup(host, port, *args, **kwargs):
        if host == 'slow.example.com':
            release.wait(5)
            return []
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('203.0.113.7', port)), (socket.AF_INET, socket.SOCK_DGRAM, 17, '', ('203.0.113.7', port)),
                (socket.AF_INET6, socket.SOCK_STREAM, 6, '', ('2001:db8::7', port, 0, 0))]

    monkeypatch.setattr(socket, 'getaddrinfo', lookup)
    started = time.monotonic()

    # when
    found = remote.addresses('shop.example.com')
    with pytest.raises(TimeoutError):
        remote.addresses('slow.example.com', 0.2)
    took = time.monotonic() - started
    release.set()

    # then
    assert found == ['203.0.113.7', '2001:db8::7']
    assert took < 2 and remote.LOOKUP_S == 5.0


class Cut(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == '/chunked':
            self.wfile.write(b'HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\nContent-Type: text/html\r\n\r\n5\r\nhello\r\n')
        else:
            self.wfile.write(b'HTTP/1.1 200 OK\r\nContent-Length: 1000\r\nContent-Type: text/html\r\n\r\n' + b'x' * 10)
        self.wfile.flush()
        self.close_connection = True

    def log_message(self, *args):
        return None


@pytest.mark.parametrize('path', ['/short', '/chunked'])
def test_a_page_whose_body_ends_before_its_length_or_its_last_chunk_is_refused_not_returned_cut(path):
    # given
    server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Cut)
    threading.Thread(target=server.serve_forever, daemon=True).start()

    # when
    try:
        with pytest.raises(Unreachable) as e:
            remote.call_page(remote.send, f'http://127.0.0.1:{server.server_address[1]}{path}?k=secret', None, 5, 10_000)
    finally:
        server.shutdown()
        server.server_close()

    # then
    assert 'secret' not in str(e.value) and str(e.value).startswith(f'http://127.0.0.1:{server.server_address[1]}{path} ')


def test_a_socket_opened_after_the_deadline_fired_is_shut_at_once(drip, monkeypatch):
    # given
    import socket
    real = socket.create_connection

    def late(address, timeout=None, source_address=None):
        time.sleep(1.3)
        return real(address, timeout, source_address)

    monkeypatch.setattr(remote.socket, 'create_connection', late)
    started = time.monotonic()

    # when
    with pytest.raises(Unreachable) as e:
        remote.call_page(remote.send, f'{drip}/slow-body', None, 0.5, 10_000)

    # then
    assert str(e.value) == f'{drip}/slow-body gave no full answer within 1 s' and time.monotonic() - started < 3


class Proxy:
    def __init__(self, mode: str = 'ok'):
        import socket
        self.mode = mode
        self.asked: list = []
        self.release = threading.Event()
        self.listener = socket.create_server(('127.0.0.1', 0))
        self.port = self.listener.getsockname()[1]
        threading.Thread(target=self.serve, daemon=True).start()

    def serve(self):
        import socket
        while True:
            try:
                client, _ = self.listener.accept()
            except OSError:
                return
            threading.Thread(target=self.handle, args=(client, socket), daemon=True).start()

    def handle(self, client, socket):
        head = b''
        while b'\r\n\r\n' not in head:
            got = client.recv(1024)
            if not got:
                return
            head += got
        line, *rest = head.split(b'\r\n\r\n', 1)[0].decode().split('\r\n')
        self.asked.append((line, {k.lower(): v.strip() for k, v in (h.split(':', 1) for h in rest if ':' in h)}))
        if self.mode == 'stall':
            try:
                while not self.release.wait(0.2):
                    client.sendall(b'H')
            except OSError:
                pass
            client.close()
            return
        if self.mode == 'refuse':
            client.sendall(b'HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\n\r\n')
            client.close()
            return
        target = line.split()[1]
        host, _, port = target.rpartition(':')
        upstream = socket.create_connection((host.strip('[]'), int(port)))
        client.sendall(b'HTTP/1.1 200 Connection established\r\n\r\n')

        def pump(a, b):
            try:
                while (data := a.recv(65536)):
                    b.sendall(data)
            except OSError:
                pass
            finally:
                for s in (a, b):
                    try:
                        s.close()
                    except OSError:
                        pass

        threading.Thread(target=pump, args=(upstream, client), daemon=True).start()
        pump(client, upstream)

    def close(self):
        self.release.set()
        self.listener.close()


@pytest.fixture
def tls_shop(tmp_path):
    served, trusted = tls_site(tmp_path, 'shop.example.com')
    names = []
    served.sni_callback = lambda sock, name, ctx: names.append(name)
    Hosted.seen = []
    server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Hosted)
    server.socket = served.wrap_socket(server.socket, server_side=True)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield server.server_address[1], trusted, names
    server.shutdown()
    server.server_close()


PROXY_NAMES = ('HTTP_PROXY', 'HTTPS_PROXY', 'NO_PROXY', 'http_proxy', 'https_proxy', 'no_proxy')


def clear_proxies(monkeypatch):
    for name in PROXY_NAMES:
        monkeypatch.delenv(name, raising=False)


def no_name(monkeypatch):
    import socket
    real = socket.getaddrinfo

    def lookup(host, *args, **kwargs):
        if host == 'shop.example.com':
            raise socket.gaierror(8, 'no such name')
        return real(host, *args, **kwargs)

    monkeypatch.setattr(socket, 'getaddrinfo', lookup)


def test_a_pinned_page_read_through_a_proxy_tunnels_to_the_checked_address_and_keeps_the_name(tls_shop, monkeypatch):
    # given
    port, trusted, names = tls_shop
    proxy = Proxy()
    no_name(monkeypatch)
    clear_proxies(monkeypatch)
    monkeypatch.setenv('https_proxy', f'http://127.0.0.1:{proxy.port}')

    # when
    try:
        answered = remote.call_page(remote.sender(trusted), f'https://shop.example.com:{port}/p', None, 5, 100, pin='127.0.0.1')
    finally:
        proxy.close()

    # then
    assert answered.status == 200 and answered.body == b'<title>Pinned</title>'
    assert [line for line, _ in proxy.asked] == [f'CONNECT 127.0.0.1:{port} HTTP/1.1']
    assert names == ['shop.example.com'] and [(p, h) for p, h, _ in Hosted.seen] == [('/p', f'shop.example.com:{port}')]


def test_no_proxy_sends_the_read_direct(tls_shop, monkeypatch):
    # given
    port, trusted, _ = tls_shop
    proxy = Proxy()
    clear_proxies(monkeypatch)
    monkeypatch.setenv('HTTPS_PROXY', f'http://127.0.0.1:{proxy.port}')
    monkeypatch.setenv('NO_PROXY', 'internal.example.net, .example.com')

    # when
    try:
        answered = remote.call_page(remote.sender(trusted), f'https://shop.example.com:{port}/p', None, 5, 100, pin='127.0.0.1')
    finally:
        proxy.close()

    # then
    assert answered.status == 200 and proxy.asked == []


def test_a_proxy_that_refuses_the_tunnel_is_named_and_a_pinned_read_never_falls_back_to_the_name(tls_shop, monkeypatch):
    # given
    port, trusted, _ = tls_shop
    proxy = Proxy('refuse')
    clear_proxies(monkeypatch)
    monkeypatch.setenv('HTTPS_PROXY', f'http://127.0.0.1:{proxy.port}')

    # when
    try:
        with pytest.raises(Unreachable) as e:
            remote.call_page(remote.sender(trusted), f'https://shop.example.com:{port}/p?k=secret', None, 5, 100, pin='127.0.0.1')
    finally:
        proxy.close()

    # then
    assert str(e.value) == f'https://shop.example.com:{port}/p unreachable: the proxy refused the tunnel (HTTP 403)'
    assert [line for line, _ in proxy.asked] == [f'CONNECT 127.0.0.1:{port} HTTP/1.1']


def test_a_proxy_that_stalls_ends_at_the_page_deadline(monkeypatch):
    # given
    proxy = Proxy('stall')
    clear_proxies(monkeypatch)
    monkeypatch.setenv('HTTPS_PROXY', f'http://127.0.0.1:{proxy.port}')
    started = time.monotonic()

    # when
    try:
        with pytest.raises(Unreachable) as e:
            remote.call_page(remote.send, 'https://shop.example.com/p', None, 0.5, 100, pin='203.0.113.9')
    finally:
        proxy.close()

    # then
    assert str(e.value) == 'https://shop.example.com/p gave no full answer within 1 s' and time.monotonic() - started < 3


def test_a_proxy_whose_name_does_not_resolve_in_time_ends_within_the_page_deadline(monkeypatch):
    # given
    import socket
    clear_proxies(monkeypatch)
    monkeypatch.setenv('HTTPS_PROXY', 'http://proxy.example.net:3128')
    release = threading.Event()
    real = socket.getaddrinfo

    def stalled(host, *args, **kwargs):
        if host == 'proxy.example.net':
            release.wait(10)
            raise socket.gaierror(8, 'no such name')
        return real(host, *args, **kwargs)

    monkeypatch.setattr(socket, 'getaddrinfo', stalled)
    started = time.monotonic()

    # when
    with pytest.raises(Unreachable) as e:
        remote.call_page(remote.send, 'https://shop.example.com/p', None, 0.5, 100, pin='203.0.113.9')
    took = time.monotonic() - started
    release.set()

    # then
    assert str(e.value).startswith('https://shop.example.com/p ') and took < 1.6, took


class SlowTunnel:
    def __init__(self, pace: float):
        import socket
        self.pace = pace
        self.release = threading.Event()
        self.listener = socket.create_server(('127.0.0.1', 0))
        self.port = self.listener.getsockname()[1]
        threading.Thread(target=self.serve, daemon=True).start()

    def serve(self):
        try:
            client, _ = self.listener.accept()
        except OSError:
            return
        try:
            client.recv(4096)
            for part in (b'HTTP/1.1 200 ', b'Connection ', b'established\r\n\r\n'):
                if self.release.wait(self.pace):
                    return
                client.sendall(part)
            self.release.wait(10)
        except OSError:
            pass
        finally:
            client.close()

    def close(self):
        self.release.set()
        self.listener.close()


def test_a_tls_handshake_that_never_answers_after_a_slow_tunnel_ends_at_the_page_deadline(monkeypatch):
    # given
    clear_proxies(monkeypatch)
    proxy = SlowTunnel(pace=1.2)
    monkeypatch.setenv('HTTPS_PROXY', f'http://127.0.0.1:{proxy.port}')
    started = time.monotonic()

    # when
    try:
        with pytest.raises(Unreachable) as e:
            remote.call_page(remote.send, 'https://shop.example.com/p', None, 2.0, 100, pin='203.0.113.9')
    finally:
        proxy.close()

    # then
    took = time.monotonic() - started
    assert str(e.value) == 'https://shop.example.com/p gave no full answer within 4 s'
    assert took < 4.8, f'the handshake ran {took:.1f} s; it must end at the 4 s deadline, not one 2 s read timeout after it'


def test_each_lookup_and_connect_waits_no_longer_than_the_time_left(monkeypatch):
    # given
    import socket
    asked = []

    def dial(address, timeout=None, source_address=None):
        asked.append(('connect', timeout))
        raise ConnectionRefusedError(61, 'refused')

    monkeypatch.setattr(remote.socket, 'create_connection', dial)
    monkeypatch.setattr(remote, 'addresses', lambda host, timeout=5.0: asked.append(('lookup', timeout)) or ['203.0.113.9', '203.0.113.10'])
    wire = remote.Wire('shop.example.com', 443, 5.0, None, [], threading.Event(), deadline=time.monotonic() + 0.3)

    # when
    with pytest.raises(OSError):
        wire.connect()

    # then
    assert [what for what, _ in asked] == ['lookup', 'connect', 'connect']
    assert all(0 < waited <= 0.3 for _, waited in asked), asked


PIN_PATHS = ('direct', 'address pin', 'proxy tunnel', 'proxy tunnel by name')
TUNNELED = {'proxy tunnel': '127.0.0.1', 'proxy tunnel by name': 'localhost'}
PIN_SECRET = 'pin-merge-token'


def pinned_read(path: str, port: int, context: ssl.SSLContext, monkeypatch) -> tuple[object, Proxy | None]:
    clear_proxies(monkeypatch)
    no_name(monkeypatch)
    proxy = Proxy() if path in TUNNELED else None
    if proxy is not None:
        monkeypatch.setenv('HTTPS_PROXY', f'http://127.0.0.1:{proxy.port}')
    url, pin = {'direct': (f'https://127.0.0.1:{port}/page', None), 'proxy tunnel by name': (f'https://localhost:{port}/page', None)}.get(path, (f'https://shop.example.com:{port}/page', '127.0.0.1'))
    try:
        return remote.call_page(remote.sender(context), url, {'Authorization': f'Bearer {PIN_SECRET}'}, 5, 100, pin=pin), proxy
    except Unreachable as e:
        return e, proxy
    finally:
        if proxy is not None:
            proxy.close()


@pytest.mark.parametrize('path', PIN_PATHS)
def test_ca_pinned_lets_a_page_read_reach_the_exact_pasted_certificate_on_every_path(tmp_path, monkeypatch, path):
    # given
    leaf = made('other.example.com')

    # when
    with pinned_site(leaf, tmp_path) as url:
        port = int(url.rstrip('/').rsplit(':', 1)[1])
        unpinned, _ = pinned_read(path, port, remote.tls_context(leaf.pem), monkeypatch)
        pinned, proxy = pinned_read(path, port, remote.tls_context(leaf.pem, True), monkeypatch)

    # then
    assert isinstance(unpinned, Unreachable) and 'SSL' in str(unpinned), 'the usual checks refuse this certificate'
    assert not isinstance(pinned, Unreachable) and (pinned.status, pinned.body) == (200, b'{"ok": true}'), pinned
    if proxy is not None:
        assert [line for line, _ in proxy.asked] == [f'CONNECT {TUNNELED[path]}:{port} HTTP/1.1']


@pytest.mark.parametrize('path', PIN_PATHS)
@pytest.mark.parametrize('pasted', ['another self-signed certificate', 'the CA that issued the server certificate'])
def test_ca_pinned_refuses_any_other_certificate_before_a_page_read_sends_a_byte_on_every_path(tmp_path, monkeypatch, path, pasted):
    # given
    authority = made('Example Private CA', ca=True)
    leaf = made('127.0.0.1', issuer=authority, ca=False)
    pem = made('127.0.0.1', ca=True).pem if pasted.startswith('another') else authority.pem

    # when
    with recorded(leaf, tmp_path) as (port, heard):
        refused, proxy = pinned_read(path, port, remote.tls_context(pem, True), monkeypatch)

    # then
    assert isinstance(refused, Unreachable) and 'SSLCertVerificationError' in str(refused), refused
    assert not heard.plain and b'/page' not in heard.raw and PIN_SECRET.encode() not in heard.raw, 'no request byte reached the server'
    if proxy is not None:
        assert [line for line, _ in proxy.asked] == [f'CONNECT {TUNNELED[path]}:{port} HTTP/1.1'], 'the proxy saw only the tunnel request'


@pytest.mark.parametrize('path', ['/slow-json', '/slow-head'])
def test_a_json_read_that_trickles_stops_at_the_same_wall_clock_deadline(drip, path):
    # given
    started = time.monotonic()

    # when
    with pytest.raises(Unreachable) as e:
        remote.send('GET', f'{drip}{path}?token=secret', {}, None, 0.5)

    # then
    took = time.monotonic() - started
    assert str(e.value) == f'{drip}{path} gave no full answer within 1 s'
    assert took < 4, took


class Sized(http.server.BaseHTTPRequestHandler):
    size = 0

    def do_GET(self):
        body = b'"' + b'x' * (type(self).size - 2) + b'"'
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        return None


@pytest.fixture
def sized():
    server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Sized)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f'http://127.0.0.1:{server.server_address[1]}/j'
    server.shutdown()
    server.server_close()


def test_a_json_read_has_a_default_byte_cap_that_a_caller_may_raise(sized, monkeypatch):
    # given
    monkeypatch.setattr(remote, 'JSON_LIMIT', 1000)

    # when
    Sized.size = 1000
    whole = remote.send('GET', sized, {}, None, 5)
    Sized.size = 1001
    with pytest.raises(Unreachable) as e:
        call(remote.send, 'GET', f'{sized}?k=secret')
    raised = call(remote.send, 'GET', sized, limit=2000)

    # then
    assert len(whole.body) == 998
    assert str(e.value) == f'{sized} answered more than 1000 bytes'
    assert len(raised.body) == 999


def test_the_default_json_cap_is_sixty_four_megabytes_and_the_refusal_names_it(sized, monkeypatch):
    # given
    default = remote.JSON_LIMIT
    monkeypatch.setattr(remote, 'JSON_LIMIT', 1024 * 1024)
    Sized.size = 1024 * 1024 + 1

    # when
    with pytest.raises(Unreachable) as e:
        remote.send('GET', sized, {}, None, 5)

    # then
    assert default == 64 * 1024 * 1024
    assert str(e.value) == f'{sized} answered more than 1 MB'


class Short(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header('Content-Length', '100')
        self.end_headers()
        self.wfile.write(b'x' * 10)

    def log_message(self, *args):
        return None


def test_a_proxy_refusal_or_a_cut_answer_keeps_its_own_text_inside_the_slack_before_the_deadline(monkeypatch):
    # given
    proxy = Proxy('refuse')
    server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Short)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    clear_proxies(monkeypatch)
    monkeypatch.setattr(remote, 'DEADLINE_SLACK', 100)

    # when
    try:
        with pytest.raises(Unreachable) as cut:
            remote.call_page(remote.send, f'http://127.0.0.1:{server.server_address[1]}/p', None, 5, 1000)
        monkeypatch.setenv('HTTPS_PROXY', f'http://127.0.0.1:{proxy.port}')
        with pytest.raises(Unreachable) as refused:
            remote.call_page(remote.send, 'https://shop.example.com/p', None, 5, 100, pin='203.0.113.9')
    finally:
        proxy.close()
        server.shutdown()
        server.server_close()

    # then
    assert str(cut.value) == f'http://127.0.0.1:{server.server_address[1]}/p gave a cut-off answer'
    assert str(refused.value) == 'https://shop.example.com/p unreachable: the proxy refused the tunnel (HTTP 403)'


class Recorded:
    def __init__(self, replies: list[bytes]):
        self.replies = list(replies)
        self.timeouts: list[float] = []
        self.sent = b''

    def settimeout(self, value):
        self.timeouts.append(value)

    def sendall(self, data):
        self.sent += data

    def recv(self, n):
        return self.replies.pop(0) if self.replies else b''

    def do_handshake(self):
        self.timeouts.append('handshake')

    def close(self):
        return None


def test_the_connect_read_waits_no_longer_than_the_time_left(monkeypatch):
    # given
    sock = Recorded([b'HTTP/1.1 200 Connection established\r\n', b'\r\n'])
    proxy = urllib.parse.urlsplit('http://proxy.example.net:3128')
    wire = remote.Wire('shop.example.com', 443, 5.0, '203.0.113.9', [], threading.Event(), proxy, deadline=time.monotonic() + 0.3)
    monkeypatch.setattr(wire, 'reach', lambda host, port: sock)

    # when
    got = wire.tunnel()

    # then
    assert got is sock and len(sock.timeouts) == 2
    assert all(0 < waited <= 0.3 for waited in sock.timeouts), sock.timeouts


def test_the_tls_handshake_waits_no_longer_than_the_time_left(monkeypatch):
    # given
    wrapped = Recorded([])

    class Context:
        def wrap_socket(self, sock, server_hostname=None, do_handshake_on_connect=True):
            return wrapped

    wire = remote.TlsWire('shop.example.com', 443, 5.0, '203.0.113.9', [], threading.Event(), None, Context(), deadline=time.monotonic() + 0.3)
    monkeypatch.setattr(wire, 'dial', lambda: object())

    # when
    wire.connect()

    # then
    first, step, last = wrapped.timeouts
    assert 0 < first <= 0.3 and step == 'handshake' and last == 5.0
