import asyncio
import logging
import ssl
import time
from types import SimpleNamespace

import anyio
import httpcore
import httpx
import pytest
from mcp import types
from mcp.shared.exceptions import McpError

from skopax.messages import msg
from skopax.mcptools import CONNECT_TIMEOUT_S, READ_TIMEOUT_S, RESPONSE_BYTES_MAX
from skopax.mcptools.client import PLAIN_HTTP, Capped, GuardedBackend, McpClient, McpFailure, Refused, TooLarge, client_for, failure_text, guard_url
from tests.mcp_fake import SERVER_SECRET, FakeResolver, FakeServer, tool

URL = 'https://mcp.example.com/mcp'
LEAKS = ('s3cr3t', 'u:p', 'x.example.com')
MB = 1024 * 1024


def tools(count: int) -> list[dict]:
    return [tool(f'tool_{i}') for i in range(count)]


def connect(fake: FakeServer, headers: dict | None = None) -> McpClient:
    return McpClient(URL, headers or {}, False, http=fake.client)


def failed(run) -> str:
    with pytest.raises(McpFailure) as caught:
        run()
    assert (caught.value.__cause__, caught.value.__context__) == (None, None)
    return caught.value.text


def status_error(code: int) -> httpx.HTTPStatusError:
    request = httpx.Request('POST', 'https://u:p@x.example.com/mcp?token=s3cr3t')
    return httpx.HTTPStatusError(SERVER_SECRET, request=request, response=httpx.Response(code, request=request))


def tls_error() -> httpx.ConnectError:
    error = httpx.ConnectError(SERVER_SECRET)
    error.__cause__ = ssl.SSLError(1, SERVER_SECRET)
    return error


def caused(outer: Exception, cause: BaseException) -> Exception:
    outer.__cause__ = cause
    return outer


@pytest.fixture
def connects(monkeypatch) -> SimpleNamespace:
    seen = SimpleNamespace(hosts=[], down=set(), dark=set())

    async def spy(self, host, port, timeout=None, local_address=None, socket_options=None):
        seen.hosts.append(host)
        if host in seen.dark:
            await anyio.sleep(5)
        if host in seen.down:
            raise httpcore.ConnectError()
        return object()

    monkeypatch.setattr(httpcore.AnyIOBackend, 'connect_tcp', spy)
    return seen


def test_listing_reads_every_page_and_the_server_name():
    # given
    fake = FakeServer(tools=tools(6), pages=3)

    # when
    listing = connect(fake).listing()

    # then
    assert [t.name for t in listing.tools] == [f'tool_{i}' for i in range(6)]
    assert (listing.server, listing.version, listing.truncated) == ('fake', '1.0.0', False)
    assert fake.methods().count('tools/list') == 3 and listing.ms >= 0


def test_the_server_name_is_kept_printable_and_cut():
    # given
    fake = FakeServer(tools=tools(1), server_name='Docs\x1b[31m\nserver ' + 'x' * 100)

    # when
    listing = connect(fake).listing()

    # then
    assert listing.server.isprintable() and listing.server.startswith('Docs') and len(listing.server) == 60


def test_a_listing_stops_at_500_tools_and_10_pages():
    # given
    wide, exact, endless = FakeServer(tools=tools(600), pages=6), FakeServer(tools=tools(500), pages=5), FakeServer(tools=tools(2), pages=1000)

    # when
    many, full, deep = connect(wide).listing(), connect(exact).listing(), connect(endless).listing()

    # then
    assert (len(many.tools), many.truncated, many.tools[-1].name) == (500, True, 'tool_499')
    assert (len(full.tools), full.truncated) == (500, False)
    assert (len(deep.tools), deep.truncated) == (2, True)
    assert endless.methods().count('tools/list') == 10


def test_a_call_opens_one_session_and_lists_nothing():
    # given
    fake = FakeServer(tools=tools(1), output='done')

    # when
    result = connect(fake, {'X-Api-Key': 'k-123'}).call('tool_0', {'query': 'disk'})

    # then
    assert [c.text for c in result.content] == ['done'] and result.isError is False
    assert fake.methods() == ['initialize', 'notifications/initialized', 'tools/call']
    assert fake.seen[-1]['params']['arguments'] == {'query': 'disk'}
    assert {s['headers'].get('x-api-key') for s in fake.seen} == {'k-123'}


@pytest.mark.parametrize('session', [None, 'session-1'])
def test_a_hanging_server_is_bounded(session):
    # given
    fake = FakeServer(tools=tools(1), hang_s=5, session=session)
    started = time.monotonic()

    # when
    text = failed(lambda: connect(fake).call('tool_0', {}, deadline=0.3))

    # then
    assert text == msg('connection.timeout')
    assert time.monotonic() - started < 0.3 + 3, 'bounded by the deadline with room for load, far below the 5 s hang'


def test_a_completed_call_returns_its_answer_when_the_teardown_stalls(caplog):
    # given
    fake = FakeServer(tools=tools(1), output='done', session='session-1', hang_s=5, hang_on=('DELETE',))
    started = time.monotonic()

    # when
    with caplog.at_level(logging.WARNING, logger='skopax.mcptools'):
        result = connect(fake).call('tool_0', {}, deadline=0.5)

    # then
    assert [c.text for c in result.content] == ['done']
    assert 'DELETE' in fake.methods()
    assert 1.5 < time.monotonic() - started < 4.5, 'the teardown is cut after its own 2 s, well before the 5 s hang'
    assert [r.getMessage() for r in caplog.records if r.name == 'skopax.mcptools'] == ['MCP session teardown cut after the answer: timeout']


def test_a_stalled_answer_still_times_out_at_the_deadline():
    # given
    fake = FakeServer(tools=tools(1), session='session-1', hang_s=5, hang_on=('tools/call',))
    started = time.monotonic()

    # when
    text = failed(lambda: connect(fake).call('tool_0', {}, deadline=0.3))

    # then
    assert text == msg('connection.timeout')
    assert time.monotonic() - started < 0.3 + 3, 'bounded by the deadline with room for load, far below the 5 s hang'


@pytest.mark.parametrize('sse', [False, True])
def test_a_huge_answer_stops_at_two_megabytes(sse):
    # given
    fake = FakeServer(tools=tools(1), output_bytes=3 * MB, sse=sse)
    started = time.monotonic()

    # when
    text = failed(lambda: connect(fake).call('tool_0', {}, deadline=5))

    # then
    assert text == msg('mcptools.too_large', limit='2 MB')
    assert time.monotonic() - started < 4


@pytest.mark.parametrize('sse', [False, True])
def test_an_answer_under_the_cap_is_read_whole(sse):
    # given
    fake = FakeServer(tools=tools(1), output_bytes=RESPONSE_BYTES_MAX - 4096, sse=sse)

    # when
    result = connect(fake).call('tool_0', {})

    # then
    assert len(result.content[0].text) == RESPONSE_BYTES_MAX - 4096


def test_a_broken_answer_fails_at_once_instead_of_waiting_for_the_deadline():
    # given
    fake = FakeServer(tools=tools(1), raw=b'{"jsonrpc": "2.0", "id": ' + SERVER_SECRET.encode())
    started = time.monotonic()

    # when
    text = failed(lambda: connect(fake).call('tool_0', {}, deadline=5))

    # then
    assert text == msg('connection.failed', error='ValidationError')
    assert time.monotonic() - started < 4


def test_a_compressed_answer_is_refused_and_the_default_client_asks_for_none():
    # given
    fake = FakeServer(tools=tools(1), gzip=True)

    # when
    text = failed(lambda: connect(fake).listing())
    headers = client_for().headers

    # then
    assert text == msg('mcptools.encoded')
    assert headers['accept-encoding'] == 'identity'


@pytest.mark.parametrize('error, key, values', [
    (lambda: status_error(401), 'connection.rejected', {'status': 401}),
    (lambda: status_error(403), 'mcptools.no_permission', {}),
    (lambda: status_error(500), 'connection.status', {'status': 500}),
    (lambda: status_error(307), 'connection.status', {'status': 307}),
    (tls_error, 'connection.tls', {}),
    (lambda: httpx.ReadTimeout(SERVER_SECRET), 'connection.timeout', {}),
    (lambda: TimeoutError(SERVER_SECRET), 'connection.timeout', {}),
    (lambda: httpx.ConnectError(SERVER_SECRET), 'connection.unreachable', {}),
    (lambda: OSError(SERVER_SECRET), 'connection.unreachable', {}),
    (lambda: McpError(types.ErrorData(code=-32601, message=SERVER_SECRET)), 'mcptools.server_error', {'code': -32601}),
    (lambda: McpError(types.ErrorData(code=32600, message=SERVER_SECRET)), 'connection.status', {'status': 404}),
    (lambda: McpError(types.ErrorData(code=-32600, message=SERVER_SECRET)), 'mcptools.server_error', {'code': -32600}),
    (lambda: ExceptionGroup(SERVER_SECRET, [status_error(401)]), 'connection.rejected', {'status': 401}),
    (lambda: ExceptionGroup(SERVER_SECRET, [ExceptionGroup(SERVER_SECRET, [httpx.ReadTimeout(SERVER_SECRET)]), ValueError()]), 'connection.timeout', {}),
    (lambda: caused(RuntimeError(SERVER_SECRET), caused(ValueError(SERVER_SECRET), status_error(403))), 'mcptools.no_permission', {}),
    (lambda: caused(McpError(types.ErrorData(code=-32000, message=SERVER_SECRET)), TimeoutError()), 'connection.timeout', {}),
    (lambda: TooLarge(), 'mcptools.too_large', {'limit': '2 MB'}),
    (lambda: ValueError('token=s3cr3t https://u:p@x.example.com'), 'connection.failed', {'error': 'ValueError'}),
], ids=['401', '403', '500', '307', 'tls', 'read-timeout', 'timeout', 'connect', 'os', 'mcp', 'wrong-path', 'mcp-invalid', 'group', 'nested-group',
        'cause', 'context-wins-by-rule', 'too-large', 'other'])
def test_failures_map_to_fixed_texts(error, key, values):
    # when
    text = failure_text(error())

    # then
    assert text == msg(key, **values)
    assert not [leak for leak in LEAKS if leak in text]


def test_a_refusal_keeps_its_own_text():
    # given
    refusal = Refused(msg('mcptools.url.blocked'))

    # when
    text = failure_text(ExceptionGroup('x', [caused(RuntimeError(SERVER_SECRET), refusal)]))

    # then
    assert text == msg('mcptools.url.blocked')


def test_a_cause_further_than_three_steps_is_not_read():
    # given
    error = caused(ValueError(), caused(KeyError(), caused(LookupError(), caused(RuntimeError(), status_error(401)))))

    # when
    text = failure_text(error)

    # then
    assert text == msg('connection.failed', error='ValueError')


@pytest.mark.parametrize('url, local, discovered, key', [
    ('https://mcp.example.com/mcp', False, False, None),
    ('http://mcp.example.com/mcp', False, False, 'mcptools.url.https'),
    ('http://mcp.example.com/mcp', True, False, 'mcptools.url.https'),
    ('ftp://mcp.example.com/mcp', False, False, 'mcptools.url.https'),
    ('http://localhost:8000/mcp', False, False, 'mcptools.url.https'),
    ('http://localhost:8000/mcp', True, False, None),
    ('http://127.0.0.1:8000/mcp', True, False, None),
    ('http://[::1]:8000/mcp', True, False, None),
    ('https://localhost:8443/mcp', False, False, None),
    ('https://169.254.169.254/', False, False, 'mcptools.url.blocked'),
    ('https://[fd00:ec2::254]/', False, False, 'mcptools.url.blocked'),
    ('https://[::ffff:169.254.169.254]/', False, False, 'mcptools.url.blocked'),
    ('https://[fe80::1]/', False, False, 'mcptools.url.blocked'),
    ('https://0.0.0.0:8080/', False, False, 'mcptools.url.blocked'),
    ('https://[::]/', False, False, 'mcptools.url.blocked'),
    ('https://100.100.100.200/', False, False, 'mcptools.url.blocked'),
    ('https://u:p@mcp.example.com/', False, False, 'mcptools.url.shape'),
    ('https://mcp.example.com/mcp#part', False, False, 'mcptools.url.shape'),
    ('https:///mcp', False, False, 'mcptools.url.shape'),
    ('https://auth.example.com/token', False, True, None),
    ('http://auth.example.com/token', True, True, 'mcptools.url.https'),
    ('http://localhost/token', True, True, 'mcptools.url.https'),
    ('https://127.0.0.1/token', False, True, 'mcptools.url.blocked'),
    ('https://localhost/token', False, True, 'mcptools.url.blocked'),
    ('https://127.0.0.1/token', True, True, None),
    ('https://169.254.169.254/token', True, True, 'mcptools.url.blocked'),
    ('http://mcp.tools.svc.cluster.local/mcp', False, False, None),
    ('http://mcp.tools.svc.cluster.local:8080/mcp', False, False, None),
    ('http://mcp.tools.svc/mcp', False, False, None),
    ('http://MCP.Tools.SVC/mcp', False, False, None),
    ('http://pod-0.mcp.tools.svc.cluster.local/mcp', False, False, None),
    ('http://mcp.svc/mcp', False, False, 'mcptools.url.https'),
    ('http://svc/mcp', False, False, 'mcptools.url.https'),
    ('http://mcp.svc.cluster.local/mcp', False, False, 'mcptools.url.https'),
    ('http://mcp.tools.svc.cluster.local./mcp', False, False, 'mcptools.url.https'),
    ('http://mcp.tools.svc.example.com/mcp', False, False, 'mcptools.url.https'),
    ('http://mcp.toolssvc/mcp', False, False, 'mcptools.url.https'),
    ('http://mcp.tools.svc.corp.local/mcp', False, False, 'mcptools.url.https'),
    ('http://-bad.tools.svc/mcp', False, False, 'mcptools.url.https'),
    ('http://10.0.0.5/mcp', False, False, 'mcptools.url.https'),
    ('http://192.168.1.5:8080/mcp', True, False, 'mcptools.url.https'),
    ('http://[fd00::5]/mcp', False, False, 'mcptools.url.https'),
    ('http://u:p' + '@mcp.tools.svc.cluster.local/mcp', False, False, 'mcptools.url.shape'),
    ('http://mcp.tools.svc.cluster.local/mcp#part', False, False, 'mcptools.url.shape'),
    ('http://mcp.tools.svc.cluster.local/token', False, True, 'mcptools.url.https'),
    ('https://mcp.tools.svc.cluster.local/mcp', False, False, None),
])
def test_url_guard_rules(url, local, discovered, key):
    # when
    try:
        guard_url(url, local, discovered)
        text = None
    except Refused as refusal:
        text = refusal.text

    # then
    assert text == (msg(key) if key else None)


def test_a_name_that_resolves_to_a_refused_address_is_never_connected(connects):
    # given
    names = {'mcp.example.com': '169.254.169.254', 'rebind.example.com': '127.0.0.1', 'zero.example.com': '0.0.0.0', 'any.example.com': '::',
             'cloud.example.com': '100.100.100.200', 'mapped.example.com': '::ffff:169.254.169.254', 'local6.example.com': 'fe80::1',
             'mixed.example.com': ('203.0.113.10', '169.254.169.254'), 'localhost': '127.0.0.1', 'good.example.com': '203.0.113.10'}

    def connect_to(host: str):
        return asyncio.run(GuardedBackend().connect_tcp(host, 443, timeout=5))

    # when
    with FakeResolver(names):
        refused = []
        for host in ('mcp.example.com', 'rebind.example.com', 'zero.example.com', 'any.example.com', 'cloud.example.com', 'mapped.example.com',
                     'local6.example.com', 'mixed.example.com'):
            with pytest.raises(Refused) as caught:
                connect_to(host)
            refused.append(caught.value.text)
        before = list(connects.hosts)
        connect_to('localhost')
        connect_to('good.example.com')

    # then
    assert refused == [msg('mcptools.url.resolved')] * 8
    assert before == []
    assert connects.hosts == ['127.0.0.1', '203.0.113.10']


def test_a_cluster_name_is_dialled_only_at_a_private_address(connects):
    # given
    names = {'mcp.tools.svc.cluster.local.': '10.96.0.12', 'six.tools.svc.cluster.local.': 'fd00:10::12', 'shared.tools.svc.cluster.local.': '100.64.3.4',
             'lan.tools.svc.cluster.local.': '192.168.4.4', 'pods.tools.svc.cluster.local.': '172.20.0.9', 'outside.tools.svc.cluster.local.': '203.0.113.10',
             'mixed.tools.svc.cluster.local.': ('10.96.0.13', '203.0.113.11'), 'mapped.tools.svc.cluster.local.': '::ffff:203.0.113.12',
             'loop.tools.svc.cluster.local.': '127.0.0.1', 'public.example.com': '203.0.113.20'}

    async def over_http(host: str, port: int):
        PLAIN_HTTP.set(True)
        return await GuardedBackend().connect_tcp(host, port, timeout=5)

    def connect_to(host: str, port: int):
        return asyncio.run(over_http(host, port))

    # when
    with FakeResolver(names):
        for host in ('mcp.tools.svc.cluster.local', 'six.tools.svc', 'shared.tools.svc', 'lan.tools.svc', 'pods.tools.svc'):
            connect_to(host, 80)
        refused = []
        for host in ('outside.tools.svc.cluster.local', 'mixed.tools.svc', 'mapped.tools.svc', 'loop.tools.svc'):
            with pytest.raises(Refused) as caught:
                connect_to(host, 80)
            refused.append(caught.value.text)
        connect_to('public.example.com', 443)

    # then
    assert refused == [msg('mcptools.url.resolved')] * 4
    assert connects.hosts == ['10.96.0.12', 'fd00:10::12', '100.64.3.4', '192.168.4.4', '172.20.0.9', '203.0.113.20']


def test_only_plain_http_to_a_cluster_name_needs_a_private_address(connects):
    # given
    names = {'outside.tools.svc.cluster.local.': '203.0.113.10', 'outside.tools.svc.cluster.local': '203.0.113.10', 'inside.tools.svc.cluster.local.': '10.96.0.12'}

    async def get(url: str) -> BaseException | None:
        async with client_for() as client:
            try:
                await client.get(url)
            except BaseException as e:
                return e
        return None

    def sent(url: str) -> BaseException | None:
        return asyncio.run(get(url))

    # when
    with FakeResolver(names):
        public_http = sent('http://outside.tools.svc.cluster.local/mcp')
        dialled_before = list(connects.hosts)
        public_https = sent('https://outside.tools.svc.cluster.local/mcp')
        private_http = sent('http://inside.tools.svc.cluster.local/mcp')

    # then
    assert isinstance(public_http, Refused) and public_http.text == msg('mcptools.url.resolved')
    assert dialled_before == []
    assert not isinstance(public_https, Refused) and not isinstance(private_http, Refused)
    assert connects.hosts == ['203.0.113.10', '10.96.0.12']


def test_a_backend_used_outside_the_capped_client_checks_a_cluster_name_as_plain_http(connects):
    # given
    names = {'outside.tools.svc.cluster.local.': '203.0.113.10', 'inside.tools.svc.cluster.local.': '10.96.0.12'}

    # when
    with FakeResolver(names):
        with pytest.raises(Refused) as caught:
            asyncio.run(GuardedBackend().connect_tcp('outside.tools.svc.cluster.local', 80, timeout=5))
        asyncio.run(GuardedBackend().connect_tcp('inside.tools.svc.cluster.local', 80, timeout=5))

    # then
    assert caught.value.text == msg('mcptools.url.resolved')
    assert connects.hosts == ['10.96.0.12']


def test_a_cluster_name_is_resolved_as_an_absolute_cluster_name_so_search_domains_never_apply(connects):
    # given
    resolver = FakeResolver({'mcp.tools.svc.cluster.local.': '10.96.0.12', 'short.tools.svc.cluster.local.': '10.96.0.13', 'mcp.tools.svc.example.com': '203.0.113.10',
                             'public.example.com': '203.0.113.20'})

    # when
    with resolver:
        for host in ('mcp.tools.svc.cluster.local', 'short.tools.svc', 'public.example.com'):
            asyncio.run(GuardedBackend().connect_tcp(host, 80, timeout=5))

    # then
    assert resolver.asked == ['mcp.tools.svc.cluster.local.', 'short.tools.svc.cluster.local.', 'public.example.com']
    assert connects.hosts == ['10.96.0.12', '10.96.0.13', '203.0.113.20']


def test_an_https_cluster_name_is_resolved_as_written_through_the_search_list(connects):
    # given
    resolver = FakeResolver({'short.tools.svc': '10.96.0.13', 'mcp.tools.svc.cluster.local': '10.96.0.12'})

    async def over_https(host: str):
        PLAIN_HTTP.set(False)
        return await GuardedBackend().connect_tcp(host, 443, timeout=5)

    async def get(url: str) -> BaseException | None:
        async with client_for() as client:
            try:
                await client.get(url)
            except BaseException as e:
                return e
        return None

    # when
    with resolver:
        asyncio.run(over_https('mcp.tools.svc.cluster.local'))
        sent = asyncio.run(get('https://short.tools.svc/mcp'))

    # then
    assert resolver.asked == ['mcp.tools.svc.cluster.local', 'short.tools.svc']
    assert not isinstance(sent, Refused)
    assert connects.hosts == ['10.96.0.12', '10.96.0.13']


def test_a_plain_http_cluster_server_is_listed_and_its_redirects_are_not_followed():
    # given
    url = 'http://mcp.tools.svc.cluster.local/mcp'
    fake = FakeServer(tools=tools(2))
    moved = FakeServer(tools=tools(1), redirect='http://mcp.example.com/mcp')

    # when
    listing = McpClient(url, {'Authorization': 'Bearer t'}, False, http=fake.client).listing()
    text = failed(McpClient(url, {}, False, http=moved.client).listing)
    public = failed(McpClient('http://mcp.example.com/mcp', {}, False, http=fake.client).listing)

    # then
    assert [t.name for t in listing.tools] == ['tool_0', 'tool_1']
    assert {s['host'] for s in fake.seen} == {'mcp.tools.svc.cluster.local'}
    assert text == msg('connection.status', status=307)
    assert [s for s in moved.seen if s['host'] != 'mcp.tools.svc.cluster.local'] == []
    assert public == msg('mcptools.url.https')


def test_each_checked_address_is_tried_in_order(connects):
    # given
    connects.down |= {'::1', '203.0.113.10', '203.0.113.20', '203.0.113.21'}
    names = {'localhost': ('::1', '127.0.0.1'), 'multi.example.com': ('203.0.113.10', '203.0.113.11'), 'dark.example.com': ('203.0.113.20', '203.0.113.21')}

    def connect_to(host: str):
        return asyncio.run(GuardedBackend().connect_tcp(host, 443, timeout=5))

    # when
    with FakeResolver(names):
        connect_to('localhost')
        connect_to('multi.example.com')
        with pytest.raises(httpcore.ConnectError):
            connect_to('dark.example.com')

    # then
    assert connects.hosts == ['::1', '127.0.0.1', '203.0.113.10', '203.0.113.11', '203.0.113.20', '203.0.113.21']


def test_a_dark_first_address_is_bounded_by_the_deadline(connects):
    # given
    connects.dark.add('203.0.113.10')
    started = time.monotonic()

    # when
    with FakeResolver({'mcp.example.com': ('203.0.113.10', '203.0.113.11')}):
        text = failed(lambda: McpClient(URL, {}, False).listing(deadline=0.3))

    # then
    assert text == msg('connection.timeout')
    assert time.monotonic() - started < 0.3 + 3, 'bounded by the deadline with room for load, far below the 5 s hang'
    assert connects.hosts == ['203.0.113.10']


def test_a_hanging_name_lookup_is_bounded_by_the_deadline(connects):
    # given
    started = time.monotonic()

    # when
    with FakeResolver({'mcp.example.com': '203.0.113.10'}, delay_s=2):
        text = failed(lambda: McpClient(URL, {}, False).listing(deadline=0.3))

    # then
    assert text == msg('connection.timeout')
    assert time.monotonic() - started < 0.3 + 3, 'bounded by the deadline with room for load, far below the 5 s hang'
    assert connects.hosts == []


def test_the_default_client_refuses_a_resolved_metadata_address(connects):
    # given
    client = McpClient(URL, {}, False)

    # when
    with FakeResolver({'mcp.example.com': '169.254.169.254'}):
        text = failed(client.listing)

    # then
    assert text == msg('mcptools.url.resolved')
    assert connects.hosts == []


def test_redirects_are_not_followed_across_origins():
    # given
    fake = FakeServer(tools=tools(1), redirect='https://169.254.169.254/latest/')

    # when
    text = failed(connect(fake).listing)

    # then
    assert text == msg('connection.status', status=307)
    assert [s for s in fake.seen if s['host'] != 'mcp.example.com' or s['path'] != '/mcp'] == []
    assert '169.254' not in text


def test_a_wrong_path_reads_as_http_404():
    # given
    fake = FakeServer(tools=tools(1), status=404)

    # when
    text = failed(connect(fake).listing)

    # then
    assert text == msg('connection.status', status=404)


def test_a_server_error_names_only_its_code():
    # given
    fake = FakeServer(tools=tools(1), error_code=-32602)

    # when
    text = failed(lambda: connect(fake).call('tool_0', {}))

    # then
    assert text == msg('mcptools.server_error', code=-32602)


@pytest.mark.parametrize('headers', [{'Host': 'evil.example.com'}, {'Mcp-Session-Id': 'abc'}, {'X-Api-Key': 'abc\r\nX-Other: 1'}, {'X-Api-Key': 'abc\x00'},
                                     {'X Api Key': 'abc'}, {'Content-Type': 'text/plain'}, {'MCP-Protocol-Version': '2020-01-01'}, {'Accept-Encoding': 'gzip'}],
                         ids=['host', 'session', 'crlf', 'nul', 'name', 'content-type', 'protocol', 'encoding'])
def test_protected_headers_are_refused(headers):
    # given
    fake = FakeServer(tools=tools(1))

    # when
    text = failed(connect(fake, headers).listing)

    # then
    assert text == msg('mcptools.header_refused')
    assert fake.seen == []


def test_the_client_never_runs_on_the_event_loop():
    # given
    fake = FakeServer(tools=tools(1))

    async def inside():
        connect(fake).listing()

    # when
    with pytest.raises(RuntimeError):
        asyncio.run(inside())

    # then
    assert fake.seen == []


def test_httpx_pool_attribute_is_where_we_replace_it():
    # when
    transport = httpx.AsyncHTTPTransport()

    # then
    assert isinstance(transport._pool, httpcore.AsyncConnectionPool)


def test_the_default_client_is_capped_guarded_and_bounded(monkeypatch):
    # given
    made, real = [], ssl.create_default_context

    def default_context(*args, **kwargs):
        made.append((args, kwargs, real(*args, **kwargs)))
        return made[-1][2]

    monkeypatch.setattr(ssl, 'create_default_context', default_context)

    # when
    client = client_for()
    pool = client._transport.transport._pool

    # then
    assert isinstance(client._transport, Capped) and isinstance(pool._network_backend, GuardedBackend)
    assert [context for args, kwargs, context in made if not args and not kwargs] == [pool._ssl_context]
    assert (pool._max_connections, pool._keepalive_expiry) == (10, 5)
    assert (client.timeout.connect, client.timeout.read, client.follow_redirects) == (CONNECT_TIMEOUT_S, READ_TIMEOUT_S, False)


def test_metadata_addresses_written_as_nat64_or_6to4_are_refused_and_a_dns64_address_is_dialled_as_resolved(connects):
    # given
    names = {'nat64.example.com': '64:ff9b::a9fe:a9fe', 'sixto4.example.com': '2002:a9fe:a9fe::1', 'loop64.example.com': '64:ff9b::7f00:1',
             'good64.example.com': '64:ff9b::cb00:710a'}

    def connect_to(host: str):
        return asyncio.run(GuardedBackend().connect_tcp(host, 443, timeout=5))

    # when
    with FakeResolver(names):
        refused = []
        for host in ('nat64.example.com', 'sixto4.example.com', 'loop64.example.com'):
            with pytest.raises(Refused) as caught:
                connect_to(host)
            refused.append(caught.value.text)
        connect_to('good64.example.com')
    with pytest.raises(Refused) as literal:
        guard_url('https://[64:ff9b::a9fe:a9fe]/mcp', False)

    # then
    assert refused == [msg('mcptools.url.resolved')] * 3 and literal.value.text == msg('mcptools.url.blocked')
    assert connects.hosts == ['64:ff9b::cb00:710a']
