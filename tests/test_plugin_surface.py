import ast
import importlib
import inspect
import email.message
import json
import os
import ssl
import subprocess
import sys
import urllib.error
from pathlib import Path

import pytest

import skopax
from skopax import contract, errkind, redact, remote
from skopax.messages import msg
from skopax.remote import HttpError, Unreachable

ROOT = Path(skopax.__file__).parent
PLUGIN_MAY_IMPORT = ('skopax.contract', 'skopax.kit', 'skopax.clock', 'skopax.remote', 'skopax.redact', 'skopax.monitor', 'skopax.messages', 'skopax.kube', 'skopax.agent', 'skopax.errkind', 'skopax.mail')
MESSAGES_NAMES = {'msg'}
MOVED = ('Prometheus', 'PrometheusError', 'scoped', 'ranged', 'client_for', 'auth_headers', 'with_headers', 'signal_queries', 'out_err')
CLOSURE = ('import importlib, json, sys; sys.path.insert(0, sys.argv[1]); importlib.import_module(sys.argv[2]); '
           'print(json.dumps(sorted(m for m in sys.modules if m == "skopax.plugins" or m.startswith("skopax.plugins."))))')
URL = 'https://prometheus.example.com/api/v1/status/buildinfo'
SECRET = 's3cr3t'
LEAK = 'token=s3cr3t'
NETWORK = ('urllib.request', 'http.client', 'http.server', 'socket', 'socketserver', 'ssl', 'asyncio', 'importlib', 'httpx', 'requests', 'aiohttp', 'urllib3', 'websockets',
           'websocket', 'pycurl', 'ftplib', 'smtplib', 'poplib', 'imaplib', 'telnetlib', 'xmlrpc', 'subprocess', 'multiprocessing')
RAW = ('skopax.remote.Wire', 'skopax.remote.TlsWire', 'skopax.remote.open_url', 'skopax.remote.open_plain', 'skopax.remote.opener', 'skopax.remote.read_page',
       'skopax.remote.relay_send', 'skopax.remote.PinnedSocket', 'skopax.remote.PinnedContext', 'skopax.remote.pinned_context', 'skopax.remote.tls_context',
       'skopax.kit.browser.connect', 'skopax.kit.browser.Connection', 'skopax.remote.direct', 'skopax.remote.unproxied', 'skopax.remote.open_direct')
ESCAPES = ('ctypes', 'pty', 'builtins', 'os.system', 'os.popen')
SPAWNS = ('os.exec', 'os.spawn')
CODE_BUILTINS = ('exec', 'eval', 'compile')
OPENERS = ('create_connection', 'open_connection', 'create_server', 'start_server', 'wrap_socket', 'wrap_bio', 'getaddrinfo', 'gethostbyname', 'urlopen', 'import_module')


def within(name: str, entry: str) -> bool:
    return name == entry or name.startswith(entry + '.')


def module_of(path: Path, base: Path = ROOT.parent) -> str:
    parts = path.relative_to(base).with_suffix('').parts
    return '.'.join(parts[:-1] if parts[-1] == '__init__' else parts)


def imported(path: Path, base: Path = ROOT.parent) -> list[tuple[str, tuple[str, ...] | None]]:
    module = module_of(path, base)
    package = module if path.name == '__init__.py' else module.rpartition('.')[0]
    out = []
    for node in ast.walk(ast.parse(path.read_text(encoding='utf-8'))):
        if isinstance(node, ast.Import):
            out += [(a.name, None) for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            head = node.module or ''
            if node.level:
                parts = package.split('.')
                head = '.'.join(parts[:len(parts) - node.level + 1] + [node.module] * bool(node.module))
            if head == 'skopax' or not node.module:
                out += [(f'{head}.{a.name}', None) for a in node.names]
            else:
                out.append((head, tuple(a.name for a in node.names)))
    return [(name, names) for name, names in out if within(name, 'skopax')]


def allowed(path: Path, name: str, names: tuple[str, ...] | None, base: Path = ROOT.parent) -> bool:
    parts = module_of(path, base).split('.')
    own = ('.'.join(parts[:3]),) if len(parts) > 2 else ()
    if name == 'skopax.messages':
        return names is not None and set(names) <= MESSAGES_NAMES
    return any(within(name, entry) for entry in PLUGIN_MAY_IMPORT + own)


def refused(files: list[Path], base: Path = ROOT.parent) -> list[str]:
    return [f'{path.relative_to(base).as_posix()}: {name}' for path in files for name, names in imported(path, base) if not allowed(path, name, names, base)]


def failing(error: BaseException, cause: BaseException | None = None):
    def run() -> str:
        if cause is None:
            raise error
        raise error from cause

    return run


def chain_of(node: ast.AST, aliases: dict[str, str]) -> str | None:
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if not isinstance(node, ast.Name):
        return None
    return '.'.join([aliases.get(node.id, node.id), *reversed(parts)])


def resolved(dotted: str) -> str | None:
    if not dotted.startswith('skopax.'):
        return None
    head, _, name = dotted.rpartition('.')
    try:
        obj = getattr(importlib.import_module(head), name)
    except (ImportError, AttributeError, ValueError):
        return None
    if inspect.ismodule(obj):
        return None if obj.__name__.startswith('skopax') else f'module {obj.__name__}'
    owner = getattr(obj, '__module__', None)
    return f'{owner}.{name}' if isinstance(owner, str) and not owner.startswith('skopax') else None


def network_uses(path: Path, modules: tuple[str, ...] = NETWORK) -> list[str]:
    modules = modules + RAW + ESCAPES
    tree = ast.parse(path.read_text(encoding='utf-8'))
    aliases: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                aliases[a.asname or a.name.split('.')[0]] = a.name if a.asname else a.name.split('.')[0]
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            for a in node.names:
                aliases[a.asname or a.name] = f'{node.module}.{a.name}'
    found = []
    for node in ast.walk(tree):
        names: list[str] = []
        if isinstance(node, ast.Import):
            names = [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            names = [node.module, *(f'{node.module}.{a.name}' for a in node.names)]
        elif isinstance(node, ast.Attribute):
            if isinstance(node.value, ast.Name):
                names = [f'{aliases.get(node.value.id, node.value.id)}.{node.attr}']
            full = chain_of(node, aliases)
            if node.attr in OPENERS and (full is None or not any(within(full, m) for m in modules)):
                names.append(f'.{node.attr}')
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in ('__import__', 'getattr') and len(node.args) >= 2 - (node.func.id == '__import__'):
            if node.func.id == '__import__':
                names = ['__import__']
            elif isinstance(node.args[1], ast.Constant) and isinstance(node.args[1].value, str):
                base = aliases.get(node.args[0].id, '') if isinstance(node.args[0], ast.Name) else ''
                wanted = node.args[1].value
                inner = resolved(f'{base}.{wanted}') or f'{base}.{wanted}'
                if wanted in OPENERS or inner.startswith('module ') or any(within(inner, m) for m in modules):
                    names = [f'getattr {wanted}']
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in CODE_BUILTINS:
            names = [f'{node.func.id}()']
        if isinstance(node, ast.Attribute):
            full = chain_of(node, aliases) or ''
            if full.startswith(SPAWNS):
                names.append(full)
        labels: list[str] = []
        for n in names:
            via = resolved(n)
            if n.startswith(('.', '__import__', 'getattr ')) or n.startswith(SPAWNS):
                labels.append(n)
            elif n.endswith('()'):
                labels.append(n[:-2])
            elif any(within(n, m) for m in modules) or (via is not None and (via.startswith('module ') or any(within(via, m) for m in modules))):
                labels.append(n)
        many = isinstance(node, ast.ImportFrom) and node.module.startswith('skopax')
        found += [n for n in labels if n != node.module] if many else labels[:1]
    return found


def test_plugins_never_open_their_own_network_connections():
    # given
    files = sorted((ROOT / 'plugins').rglob('*.py'))

    # when
    found = {path.relative_to(ROOT.parent).as_posix(): network_uses(path) for path in files}

    # then
    assert files and {where: hits for where, hits in found.items() if hits} == {}


def test_the_network_scan_catches_each_form_and_leaves_url_parsing_alone(tmp_path):
    # given
    path = tmp_path / 'fake.py'
    path.write_text(
        'import socket\n'
        'import urllib.request\n'
        'import http.client as wire\n'
        'from urllib import request\n'
        'from urllib.request import urlopen\n'
        'from http import client\n'
        'import urllib\n'
        'import urllib.parse\n'
        'from urllib.parse import quote\n'
        'from urllib import parse\n'
        'opened = urllib.request.urlopen\n', encoding='utf-8')

    # when
    found = network_uses(path)

    # then
    assert found == ['socket', 'urllib.request', 'http.client', 'urllib.request', 'urllib.request', 'http.client', 'urllib.request']


def test_the_network_scan_catches_aliased_parents_dynamic_imports_and_other_clients(tmp_path):
    # given
    path = tmp_path / 'fake.py'
    path.write_text(
        'import urllib as u\n'
        'import http as h\n'
        'import ssl\n'
        'import asyncio\n'
        'import importlib\n'
        'import httpx\n'
        'import requests\n'
        'import aiohttp\n'
        'import urllib3\n'
        'from websockets.sync import client\n'
        'opened = u.request.urlopen\n'
        'wire = h.client\n'
        'reader = asyncio.open_connection\n'
        'mod = __import__("so" + "cket")\n'
        'other = importlib.import_module("http.client")\n'
        'found = getattr(u, "request")\n'
        'tls = ssl.create_connection\n'
        'raw = object().wrap_socket\n', encoding='utf-8')

    # when
    found = set(network_uses(path))

    # then
    for hit in ('urllib.request', 'http.client', 'ssl', 'asyncio', 'importlib', 'httpx', 'requests', 'aiohttp', 'urllib3', 'websockets.sync', 'asyncio.open_connection',
                '__import__', 'importlib.import_module', 'getattr request', 'ssl.create_connection', '.wrap_socket'):
        assert hit in found, hit


def test_the_network_scan_leaves_plain_parsing_and_data_modules_alone(tmp_path):
    # given
    path = tmp_path / 'fake.py'
    path.write_text('import json\nimport urllib.parse\nfrom urllib import parse\nimport email.utils\nvalue = getattr(object(), "status", None)\n', encoding='utf-8')

    # then
    assert network_uses(path) == []


def test_plugins_import_only_the_public_surface():
    # given
    files = sorted((ROOT / 'plugins').rglob('*.py'))

    # when
    bad = refused(files)

    # then
    assert files and bad == []


def test_the_surface_scan_resolves_each_import_form(tmp_path):
    # given
    home = tmp_path / 'skopax' / 'plugins' / 'fake'
    home.mkdir(parents=True)
    (home / '__init__.py').write_text(
        'from skopax import kube as cluster\n'
        'from skopax import store\n'
        'import skopax.app\n'
        'import skopax.kit.promql\n'
        'from skopax.plugins.prometheus import engine\n'
        'from skopax.plugins.fake import part\n'
        'from . import part\n'
        'from .part import thing\n'
        'from .. import prometheus\n'
        'from skopax.messages import msg\n'
        'from skopax.messages import MESSAGES\n'
        'from skopax import messages\n'
        'from skopax.kit.promql import Prometheus\n'
        'from skopax.contract import Check\n'
        'from skopax.contractual import thing\n'
        'from skopax.errkind import kind\n', encoding='utf-8')

    # when
    bad = refused([home / '__init__.py'], tmp_path)

    # then
    where = 'skopax/plugins/fake/__init__.py'
    assert bad == [f'{where}: skopax.store', f'{where}: skopax.app', f'{where}: skopax.plugins.prometheus', f'{where}: skopax.plugins.prometheus', f'{where}: skopax.messages',
                   f'{where}: skopax.messages', f'{where}: skopax.contractual']


def test_the_public_surface_never_imports_a_plugin():
    # given
    roots = sorted({*PLUGIN_MAY_IMPORT, *(module_of(p) for p in (ROOT / 'kit').rglob('*.py'))})

    # when
    plain = {**os.environ, 'NO_COLOR': '1'}
    runs = {}
    try:
        for m in roots:
            runs[m] = subprocess.Popen([sys.executable, '-I', '-c', CLOSURE, str(ROOT.parent), m], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=plain)
        done = {m: (*p.communicate(timeout=120), p.returncode) for m, p in runs.items()}
    finally:
        for p in runs.values():
            if p.poll() is None:
                p.kill()
                p.communicate()
    problems = {m: [f'exit {code}', *err.strip().splitlines()[-1:]] if code else json.loads(out) for m, (out, err, code) in done.items()}

    # then
    assert {m: found for m, found in problems.items() if found} == {}
    assert any(m.startswith('skopax.kit.') for m in roots)


def test_promql_client_lives_in_the_kit():
    # given
    from skopax.kit import promql
    from skopax.plugins import prometheus

    home = ROOT / 'plugins' / 'prometheus'

    # when
    defined = {n.name for path in home.glob('*.py') for n in ast.walk(ast.parse(path.read_text(encoding='utf-8'))) if isinstance(n, (ast.ClassDef, ast.FunctionDef))}
    homes = {name: getattr(getattr(promql, name, None), '__module__', None) for name in MOVED}

    # then
    assert defined & set(MOVED) == set()
    assert homes == {name: 'skopax.kit.promql' for name in MOVED}
    assert (prometheus.client_for, prometheus.ranged, prometheus.auth_headers) == (promql.client_for, promql.ranged, promql.auth_headers)
    assert [name for name, _ in imported(ROOT / 'postdeploy.py') if within(name, 'skopax.plugins')] == []


def test_the_app_imports_no_plugin_and_takes_the_prometheus_names_from_the_kit():
    # given
    from skopax.kit import promql
    from skopax.plugins import prometheus

    # when
    found = [name for name, _ in imported(ROOT / 'app.py') if within(name, 'skopax.plugins')]

    # then
    assert found == []
    assert (promql.ID, promql.CHECK) == ('prometheus', 'prom')
    assert (prometheus.PLUGIN.id, prometheus.PLUGIN.checks[0].id) == (promql.ID, promql.CHECK)


MAPPED = [
    pytest.param(lambda: HttpError(401, 'u'), None, lambda: msg('connection.rejected', status=401), id='refused-401'),
    pytest.param(lambda: HttpError(403, 'u'), None, lambda: msg('connection.forbidden', status=403), id='forbidden-403'),
    pytest.param(lambda: HttpError(503, 'u'), None, lambda: msg('connection.status', status=503), id='status-503'),
    pytest.param(Unreachable, ssl.SSLError, lambda: msg('connection.tls'), id='unreachable-from-tls'),
    pytest.param(ssl.SSLError, None, lambda: msg('connection.tls'), id='tls'),
    pytest.param(TimeoutError, None, lambda: msg('connection.timeout'), id='timeout'),
    pytest.param(Unreachable, None, lambda: msg('connection.unreachable'), id='unreachable'),
    pytest.param(lambda: contract.SourceDown('x'), None, lambda: 'x', id='source-down'),
    pytest.param(lambda: contract.ReadError(503, 'instance east is not set up'), None, lambda: 'instance east is not set up', id='read-error'),
    pytest.param(lambda: ValueError('https://u:p@x.example.com'), None, lambda: msg('connection.failed', error='ValueError'), id='failed'),
]


@pytest.mark.parametrize('error, cause, shown', MAPPED)
def test_connection_results_map_errors_to_fixed_texts(error, cause, shown):
    # given
    from skopax.kit import connection

    # when
    found = connection.result(failing(error(), cause() if cause else None))

    # then
    assert (found.ok, found.summary) == (False, shown())
    assert isinstance(found.ms, int) and found.ms >= 0


def test_an_ok_connection_run_keeps_its_summary_and_times_it():
    # given
    from skopax.kit import connection

    ticks = iter([100.0, 100.25])

    # when
    found = connection.result(lambda: 'Prometheus 2.53.0', clock=lambda: next(ticks))

    # then
    assert found == contract.TestResult(True, 'Prometheus 2.53.0', 250)


SENT = [
    pytest.param(lambda: urllib.error.URLError(ssl.SSLError()), lambda: msg('connection.tls'), id='url-tls'),
    pytest.param(lambda: urllib.error.URLError(ssl.SSLCertVerificationError()), lambda: msg('connection.tls'), id='url-certificate'),
    pytest.param(lambda: urllib.error.URLError(TimeoutError()), lambda: msg('connection.timeout'), id='url-timeout'),
    pytest.param(TimeoutError, lambda: msg('connection.timeout'), id='timeout'),
    pytest.param(lambda: urllib.error.URLError(ConnectionRefusedError()), lambda: msg('connection.unreachable'), id='url-refused'),
    pytest.param(lambda: urllib.error.HTTPError(URL, 401, 'Unauthorized', email.message.Message(), None), lambda: msg('connection.rejected', status=401), id='http-401'),
]


@pytest.mark.parametrize('raised, shown', SENT)
def test_real_transport_errors_are_classified_through_their_context(raised, shown, monkeypatch):
    # given
    from skopax.kit import connection

    error = raised()

    class Refusing:
        def open(self, request, timeout):
            raise error

    monkeypatch.setattr(remote, 'opener', lambda context=None, follow=True: Refusing())
    send = remote.sender()

    # when
    found = connection.result(lambda: remote.call(send, 'GET', URL).body)

    # then
    assert (found.ok, found.summary) == (False, shown())


LEAKY = [
    pytest.param(lambda: HttpError(401, LEAK, LEAK), None, id='refused'),
    pytest.param(lambda: HttpError(503, LEAK, LEAK), None, id='status'),
    pytest.param(lambda: Unreachable(LEAK), lambda: ssl.SSLError(LEAK), id='unreachable-from-tls'),
    pytest.param(lambda: ssl.SSLError(LEAK), None, id='tls'),
    pytest.param(lambda: TimeoutError(LEAK), None, id='timeout'),
    pytest.param(lambda: Unreachable(LEAK), None, id='unreachable'),
    pytest.param(lambda: ValueError(LEAK), None, id='failed'),
    pytest.param(lambda: RuntimeError(LEAK), lambda: KeyError(LEAK), id='failed-chained'),
]


@pytest.mark.parametrize('error, cause', LEAKY)
def test_connection_results_never_carry_the_exception_text(error, cause):
    # given
    from skopax.kit import connection

    # when
    found = connection.result(failing(error(), cause() if cause else None))

    # then
    assert found.ok is False and SECRET not in found.summary and SECRET not in repr(found)


@pytest.mark.parametrize('handled', [ssl.SSLError, TimeoutError, lambda: urllib.error.URLError(ssl.SSLError())], ids=['tls', 'timeout', 'url-tls'])
def test_a_source_down_raised_while_a_transport_error_is_handled_keeps_its_own_text(handled):
    # given
    from skopax.kit import connection

    def run() -> str:
        try:
            raise handled()
        except OSError:
            raise contract.SourceDown('Jira could not be reached') from None

    # when
    found = connection.result(run)

    # then
    assert (found.ok, found.summary) == (False, 'Jira could not be reached')


def test_a_read_error_raised_while_a_tls_error_is_handled_keeps_its_message():
    # given
    from skopax.kit import connection

    def run() -> str:
        try:
            raise ssl.SSLError('handshake')
        except ssl.SSLError:
            raise contract.ReadError(503, 'instance east is not set up') from None

    # when
    found = connection.result(run)

    # then
    assert (found.ok, found.summary) == (False, 'instance east is not set up')


@pytest.mark.parametrize('said, shown', [('upstream answered: Bearer abcdefgh12345678', f'upstream answered: {redact.REDACTED}'),
                                         ('instance east is not set up', 'instance east is not set up')], ids=['masked', 'plain'])
def test_a_read_error_message_is_redacted_before_it_is_shown(said, shown):
    # given
    from skopax.kit import connection

    # when
    found = connection.result(failing(contract.ReadError(502, said)))

    # then
    assert (found.ok, found.summary) == (False, shown)


@pytest.mark.parametrize('raised, shown', [(lambda: HttpError(401, URL), lambda: msg('connection.rejected', status=401)), (lambda: HttpError(503, URL), lambda: msg('connection.status', status=503)),
                                           (lambda: Unreachable(URL), lambda: msg('connection.unreachable'))], ids=['refused-401', 'status-503', 'unreachable'])
def test_a_wrapping_client_keeps_the_status_of_the_error_it_wrapped(raised, shown):
    # given
    from skopax.kit import connection
    from skopax.kit.promql import Prometheus

    def transport(method, url, headers, data, timeout):
        raise raised()

    client = Prometheus('https://prometheus.example.com', transport, sleep=lambda s: None)

    # when
    found = connection.result(lambda: client.call('query', query='up'))

    # then
    assert (found.ok, found.summary) == (False, shown())


def test_source_down_is_one_class():
    # given
    from skopax import enrich

    down = contract.SourceDown('Jira HTTP 503')

    # then
    assert enrich.SourceDown is contract.SourceDown and 'SourceDown' in contract.__all__
    assert isinstance(down, errkind.Authored) and errkind.text_of(down) == 'Jira HTTP 503'


OLD_TEXT = [
    ('duration', (0,), '0 s'),
    ('duration', (None,), '0 s'),
    ('duration', (45,), '45 s'),
    ('duration', (119.4,), '119 s'),
    ('duration', (150,), '2 min'),
    ('duration', (7199,), '119 min'),
    ('duration', (7200,), '2 h 0 min'),
    ('duration', (3 * 3600 + 600,), '3 h 10 min'),
    ('duration', (2 * 86400 + 3660,), '49 h 1 min'),
    ('cut', ('  Release\n\tnote  alice@example.com ', 120), 'Release note [redacted]'),
    ('cut', ('x' * 500, 10), 'x' * 10),
    ('cut', (None, 120), ''),
    ('cut', ('checkout-service failed', 12), 'checkout-ser'),
    ('cut', ('checkout \n\t service failed', 9), 'checkout '),
]


@pytest.mark.parametrize('helper, args, shown', OLD_TEXT)
def test_text_helpers_match_the_old_ones(helper, args, shown):
    # given
    from skopax.kit import text

    # when
    found = getattr(text, helper)(*args)

    # then
    assert found == shown


KIT_NETWORK = tuple(m for m in NETWORK if m != 'ssl')
KIT_OWN_CONNECTIONS = ('skopax.kit.browser',)


def kit_network_uses(path: Path) -> list[str]:
    return network_uses(path, KIT_NETWORK)


def test_kit_modules_read_through_the_transport_and_only_the_browser_opens_its_own_connection():
    # given
    files = sorted((ROOT / 'kit').rglob('*.py'))

    # when
    found = {module_of(path): kit_network_uses(path) for path in files}

    # then
    assert KIT_OWN_CONNECTIONS == ('skopax.kit.browser',)
    assert {m for m, hits in found.items() if hits} == set(KIT_OWN_CONNECTIONS), found
    assert {'websockets.sync.client'} <= set(found['skopax.kit.browser'])
    assert 'skopax.kit.web' in found and found['skopax.kit.web'] == []


def test_the_kit_scan_catches_a_websocket_client_too(tmp_path):
    # given
    path = tmp_path / 'fake.py'
    path.write_text('from websockets.sync.client import connect\nimport socket\nfrom urllib.parse import quote\n', encoding='utf-8')

    # when
    found = kit_network_uses(path)

    # then
    assert found == ['websockets.sync.client', 'socket']


def test_an_answer_that_is_not_json_says_to_check_the_api_address_not_the_network(monkeypatch):
    # given
    class Html:
        headers = {}

        def __init__(self):
            self.body = b'<html>dashboard</html>'

        def read(self, *args):
            return self.body

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(remote, 'open_url', lambda req, timeout, context=None: Html())

    def ask() -> str:
        remote.call(remote.send, 'GET', 'https://stats.example.com/api/websites/x?key=secret')
        return 'ok'

    # when
    from skopax.kit import connection
    got = connection.result(ask)

    # then
    assert (got.ok, got.summary) == (False, msg('connection.not_api'))
    assert 'API address' in msg('connection.not_api') and 'secret' not in got.summary


def test_the_network_scan_catches_transport_names_re_exported_by_core_and_kit_and_process_escapes(tmp_path):
    # given
    path = tmp_path / 'fake.py'
    path.write_text(
        'import os\n'
        'import ctypes\n'
        'import pty\n'
        'import builtins\n'
        'from skopax import remote\n'
        'from skopax.remote import socket\n'
        'from skopax.remote import http\n'
        'from skopax.kit.browser import connect\n'
        'from skopax.kit.browser import Connection\n'
        'from skopax.remote import Wire, read_page\n'
        'from skopax.remote import PinnedSocket, PinnedContext, pinned_context, tls_context\n'
        'raw = getattr(remote, "socket")\n'
        'sent = remote.relay_send\n'
        'os.system("curl https://example.org")\n'
        'os.popen("curl https://example.org")\n'
        'os.execv("/bin/sh", ["sh"])\n'
        'os.spawnl(os.P_WAIT, "/bin/sh", "sh")\n'
        'exec("import socket")\n'
        'eval("1")\n'
        'compile("1", "x", "eval")\n', encoding='utf-8')

    # when
    found = set(network_uses(path))

    # then
    for hit in ('ctypes', 'pty', 'builtins', 'skopax.remote.socket', 'skopax.remote.http', 'skopax.kit.browser.connect', 'skopax.kit.browser.Connection', 'skopax.remote.Wire',
                'skopax.remote.read_page', 'skopax.remote.PinnedSocket', 'skopax.remote.PinnedContext', 'skopax.remote.pinned_context', 'skopax.remote.tls_context',
                'getattr socket', 'skopax.remote.relay_send', 'os.system', 'os.popen', 'os.execv', 'os.spawnl', 'exec', 'eval', 'compile'):
        assert hit in found, hit


def test_the_network_scan_still_lets_plugins_take_the_transport_helpers_and_errors(tmp_path):
    # given
    path = tmp_path / 'fake.py'
    path.write_text('import re\nfrom skopax import remote\nfrom skopax.remote import addresses, call_text, HttpError, MARKER, send, sender\nfrom skopax.kit.browser import Browser, BrowserDown\n'
                    'pattern = re.compile("x")\nfound = os.path.join("a", "b") if False else None\nsent = remote.send\nmade = remote.sender()\n', encoding='utf-8')

    # then
    assert network_uses(path) == []


def test_the_plugin_docs_say_the_plugin_guard_catches_mistakes_and_is_not_a_sandbox():
    # given
    path = ROOT.parent / 'docs' / 'extending.md'

    # then
    assert path.is_file() and 'not a sandbox' in path.read_text(encoding='utf-8')
