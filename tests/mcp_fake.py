import asyncio
import base64
import gzip as gzip_codec
import hashlib
import ipaddress
import json
import math
import socket
import time
from urllib.parse import parse_qsl, urlsplit

import httpx

from skopax.mcptools import Reconnect
from skopax.mcptools.client import Capped

SERVER_SECRET = 'token=s3cr3t https://u:p@x.example.com'


def tool(name: str, description: str = '', schema: dict | None = None) -> dict:
    return {'name': name, 'description': description, 'inputSchema': schema or {'type': 'object', 'properties': {}}}


class FakeServer:
    def __init__(self, tools=(), server_name: str = 'fake', pages: int = 1, hang_s: float = 0.0, output: str | None = None, output_bytes: int = 0,
                 status: int = 200, redirect: str | None = None, error_code: int | None = None, sse: bool = False, session: str | None = None,
                 raw: bytes | None = None, gzip: bool = False, hang_on: tuple[str, ...] | None = None):
        self.tools = list(tools)
        self.server_name = server_name
        self.version = '1.0.0'
        self.pages = pages
        self.hang_s = hang_s
        self.output = output
        self.output_bytes = output_bytes
        self.status = status
        self.redirect = redirect
        self.error_code = error_code
        self.sse = sse
        self.session = session
        self.raw = raw
        self.gzip = gzip
        self.hang_on = hang_on
        self.seen: list[dict] = []

    def asgi(self):
        return self.app

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=Capped(httpx.ASGITransport(app=self.asgi())))

    def methods(self) -> list[str]:
        return [s['method'] for s in self.seen]

    async def app(self, scope, receive, send) -> None:
        body = b''
        while True:
            message = await receive()
            body += message.get('body', b'')
            if not message.get('more_body'):
                break
        headers = {k.decode('latin-1'): v.decode('latin-1') for k, v in scope['headers']}
        call = json.loads(body) if body else {}
        self.seen.append({'method': call.get('method', scope['method']), 'http': scope['method'], 'host': headers.get('host', ''), 'path': scope['path'],
                          'headers': headers, 'params': call.get('params', {})})
        if self.redirect:
            return await self.answer(send, 307, b'', {'location': self.redirect})
        if self.status != 200:
            return await self.answer(send, self.status, b'{}', {'content-type': 'application/json'})
        if scope['method'] == 'GET':
            return await self.answer(send, 405, b'', {})
        if scope['method'] == 'DELETE':
            await self.hang('DELETE')
            return await self.answer(send, 200, b'', {})
        if 'id' not in call:
            return await self.answer(send, 202, b'', {})
        if call['method'] != 'initialize':
            await self.hang(call['method'])
        if call['method'] == 'tools/call' and self.raw is not None:
            return await self.answer(send, 200, self.raw, {'content-type': 'application/json'})
        reply = self.reply(call)
        extra = {'mcp-session-id': self.session} if self.session and call['method'] == 'initialize' else {}
        if self.sse:
            return await self.answer(send, 200, b'event: message\ndata: ' + json.dumps(reply).encode() + b'\n\n', {'content-type': 'text/event-stream', **extra})
        return await self.answer(send, 200, json.dumps(reply).encode(), {'content-type': 'application/json', **extra})

    async def hang(self, method: str) -> None:
        if self.hang_s and (self.hang_on is None or method in self.hang_on):
            await asyncio.sleep(self.hang_s)

    def reply(self, call: dict) -> dict:
        method, params = call['method'], call.get('params') or {}
        if method == 'initialize':
            return self.result(call, {'protocolVersion': params['protocolVersion'], 'capabilities': {'tools': {'listChanged': False}},
                                      'serverInfo': {'name': self.server_name, 'version': self.version}})
        if method == 'tools/list':
            return self.result(call, self.page(int(params.get('cursor') or 0)))
        if method == 'tools/call' and self.error_code is not None:
            return {'jsonrpc': '2.0', 'id': call['id'], 'error': {'code': self.error_code, 'message': SERVER_SECRET}}
        if method == 'tools/call':
            text = 'x' * self.output_bytes if self.output_bytes else self.output if self.output is not None else 'ok'
            return self.result(call, {'content': [{'type': 'text', 'text': text}], 'isError': False})
        return {'jsonrpc': '2.0', 'id': call['id'], 'error': {'code': -32601, 'message': 'Method not found'}}

    def page(self, index: int) -> dict:
        size = max(1, math.ceil(len(self.tools) / self.pages))
        found = {'tools': self.tools[index * size:(index + 1) * size]}
        return found | ({'nextCursor': str(index + 1)} if index + 1 < self.pages else {})

    @staticmethod
    def result(call: dict, result: dict) -> dict:
        return {'jsonrpc': '2.0', 'id': call['id'], 'result': result}

    async def answer(self, send, status: int, body: bytes, headers: dict) -> None:
        if self.gzip and body:
            body, headers = gzip_codec.compress(body), headers | {'content-encoding': 'gzip'}
        pairs = [(k.encode('latin-1'), v.encode('latin-1')) for k, v in (headers | {'content-length': str(len(body))}).items()]
        await send({'type': 'http.response.start', 'status': status, 'headers': pairs})
        await send({'type': 'http.response.body', 'body': body})


class FakeResolver:
    def __init__(self, mapping: dict, delay_s: float = 0.0):
        self.mapping = {name: (found,) if isinstance(found, str) else tuple(found) for name, found in mapping.items()}
        self.delay_s = delay_s
        self.asked: list[str] = []
        self.saved = None

    def __call__(self, host, port, family=0, type=0, proto=0, flags=0):
        self.asked.append(host)
        time.sleep(self.delay_s)
        found = self.mapping.get(host) or self.literal(host)
        if not found:
            raise socket.gaierror(socket.EAI_NONAME, 'unknown name')
        return [(socket.AF_INET6, socket.SOCK_STREAM, 6, '', (a, port, 0, 0)) if ':' in a else (socket.AF_INET, socket.SOCK_STREAM, 6, '', (a, port)) for a in found]

    @staticmethod
    def literal(host: str) -> tuple:
        try:
            return (str(ipaddress.ip_address(host)),)
        except ValueError:
            return ()

    def __enter__(self) -> 'FakeResolver':
        self.saved = socket.getaddrinfo
        socket.getaddrinfo = self
        return self

    def __exit__(self, *exc) -> None:
        socket.getaddrinfo = self.saved


AUTH_ORIGIN = 'https://auth.example.com'
PRM_PATH = '/.well-known/oauth-protected-resource'
ASM_PATHS = ('/.well-known/oauth-authorization-server', '/.well-known/openid-configuration')


def challenge_of(verifier: str) -> str:
    return base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b'=').decode()


class FakeAuth:
    def __init__(self, server: FakeServer | None = None, registration: bool = True, s256: bool = True, metadata: dict | None = None, prm: bool = True,
                 resource: str | None = None, register_status: int = 201, refresh_error: str | None = None, token_status: int = 200, token_body: bytes | None = None,
                 hang_s: float = 0.0, expires_in: int | None = 3600, refresh: bool = True, servers: list[str] | None = None, challenge: str | None = None,
                 token_headers: dict | None = None, slow_s: float = 0.0, missing_scope: str | None = None):
        self.server = server or FakeServer(tools=[{**tool('search', 'Search the docs'), 'annotations': {'readOnlyHint': True}}])
        self.registration = registration
        self.s256 = s256
        self.metadata = metadata or {}
        self.prm = prm
        self.resource = resource
        self.register_status = register_status
        self.refresh_error = refresh_error
        self.token_status = token_status
        self.token_body = token_body
        self.hang_s = hang_s
        self.expires_in = expires_in
        self.refresh = refresh
        self.servers = servers or [AUTH_ORIGIN]
        self.challenge = challenge
        self.token_headers = token_headers or {}
        self.slow_s = slow_s
        self.missing_scope = missing_scope
        self.access = ''
        self.refresh_token = ''
        self.issued = 0
        self.refreshes = 0
        self.exchanges = 0
        self.rejected = 0
        self.revoked: set[str] = set()
        self.codes: dict[str, dict] = {}
        self.registered: list[dict] = []
        self.token_calls: list[dict] = []
        self.seen: list[tuple[str, str, str]] = []

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=Capped(httpx.ASGITransport(app=self.app)))

    def authorize(self, url: str) -> tuple[str, str]:
        query = dict(parse_qsl(urlsplit(url).query))
        code = f'code-{len(self.codes) + self.exchanges + 1}'
        self.codes[code] = {'challenge': query['code_challenge'], 'redirect_uri': query['redirect_uri'], 'resource': query.get('resource'), 'client_id': query['client_id']}
        return code, query['state']

    def revoke(self) -> None:
        self.revoked.add(self.access)

    async def app(self, scope, receive, send) -> None:
        body = b''
        while True:
            message = await receive()
            body += message.get('body', b'')
            if not message.get('more_body'):
                break
        headers = {k.decode('latin-1'): v.decode('latin-1') for k, v in scope['headers']}
        host, path = headers.get('host', ''), scope['path']
        self.seen.append((scope['method'], host, path))
        if self.slow_s and path.startswith('/.well-known/'):
            await asyncio.sleep(self.slow_s)
        if path.startswith(PRM_PATH):
            if not self.prm:
                return await self.answer(send, 404, {})
            return await self.answer(send, 200, {'resource': self.resource or f'https://{host}{path[len(PRM_PATH):] or "/"}', 'authorization_servers': self.servers})
        if path in ASM_PATHS:
            return await self.answer(send, 200, self.server_metadata())
        if path == '/register':
            return await self.register(send, json.loads(body or b'{}'))
        if path == '/token':
            return await self.token(send, dict(parse_qsl(body.decode())), headers, scope['query_string'].decode('latin-1'))
        if headers.get('authorization') != f'Bearer {self.access}' or not self.access or self.access in self.revoked:
            self.rejected += 1
            target = self.challenge or f'https://{host}{PRM_PATH}{path}'
            challenge = f'Bearer resource_metadata="{target}"'
            return await self.answer(send, 401, {}, {'www-authenticate': challenge})
        if self.missing_scope is not None:
            named = f', scope="{self.missing_scope}"' if self.missing_scope else ''
            return await self.answer(send, 403, {}, {'www-authenticate': f'Bearer error="insufficient_scope"{named}'})

        async def replay():
            return {'type': 'http.request', 'body': body, 'more_body': False}

        await self.server.app(scope, replay, send)

    def server_metadata(self) -> dict:
        found = {'issuer': AUTH_ORIGIN, 'authorization_endpoint': f'{AUTH_ORIGIN}/authorize', 'token_endpoint': f'{AUTH_ORIGIN}/token', 'response_types_supported': ['code'],
                 'code_challenge_methods_supported': ['S256'] if self.s256 else ['plain'], 'token_endpoint_auth_methods_supported': ['none', 'client_secret_basic', 'client_secret_post']}
        if self.registration:
            found['registration_endpoint'] = f'{AUTH_ORIGIN}/register'
        return found | self.metadata

    async def register(self, send, body: dict) -> None:
        self.registered.append(body)
        if self.register_status != 201:
            return await self.answer(send, self.register_status, {'error': 'invalid_client_metadata', 'error_description': SERVER_SECRET})
        return await self.answer(send, 201, {**body, 'client_id': f'client-{len(self.registered)}', 'client_id_issued_at': 1})

    async def token(self, send, form: dict, headers: dict, query: str = '') -> None:
        self.token_calls.append({'form': form, 'headers': headers, 'query': query})
        if form.get('grant_type') == 'refresh_token':
            self.refreshes += 1
        else:
            self.exchanges += 1
        if self.hang_s:
            await asyncio.sleep(self.hang_s)
        if self.token_body is not None:
            return await self.raw(send, self.token_status, self.token_body, self.token_headers)
        if self.token_status != 200:
            return await self.answer(send, self.token_status, {}, self.token_headers)
        if form.get('grant_type') == 'authorization_code':
            code = self.codes.pop(form.get('code', ''), None)
            if code is None or challenge_of(form.get('code_verifier', '')) != code['challenge'] or form.get('redirect_uri') != code['redirect_uri'] \
                    or form.get('resource') != code['resource'] or form.get('client_id') != code['client_id']:
                return await self.answer(send, 400, {'error': 'invalid_grant', 'error_description': f"bad code {form.get('code')}"})
            return await self.answer(send, 200, self.issue())
        if self.refresh_error:
            return await self.answer(send, 400, {'error': self.refresh_error, 'error_description': SERVER_SECRET})
        if not self.refresh_token or form.get('refresh_token') != self.refresh_token:
            return await self.answer(send, 400, {'error': 'invalid_grant'})
        return await self.answer(send, 200, self.issue())

    def issue(self) -> dict:
        self.issued += 1
        self.access = f'access-{self.issued}-s3cr3t'
        self.refresh_token = f'refresh-{self.issued}-s3cr3t' if self.refresh else ''
        found = {'access_token': self.access, 'token_type': 'bearer', 'scope': 'read'}
        if self.expires_in is not None:
            found['expires_in'] = self.expires_in
        return found | ({'refresh_token': self.refresh_token} if self.refresh else {})

    async def answer(self, send, status: int, doc: dict, headers: dict | None = None) -> None:
        await self.raw(send, status, json.dumps(doc).encode(), {'content-type': 'application/json', **(headers or {})})

    @staticmethod
    async def raw(send, status: int, body: bytes, headers: dict | None = None) -> None:
        pairs = [(k.encode('latin-1'), v.encode('latin-1')) for k, v in ({'content-type': 'text/html'} | (headers or {}) | {'content-length': str(len(body))}).items()]
        await send({'type': 'http.response.start', 'status': status, 'headers': pairs})
        await send({'type': 'http.response.body', 'body': body})


class FakeTokens:
    def __init__(self, state: str = 'connected', access: str = 'fake-access-token'):
        self.current = state
        self.access = access
        self.parked: list[tuple[str, str]] = []
        self.asked: list[tuple[str, str]] = []

    def state(self, name: str) -> str:
        return self.current

    def token(self, name: str, instance) -> str:
        self.asked.append(('token', name))
        if self.current != 'connected':
            raise Reconnect()
        return self.access

    def after_unauthorized(self, name: str, instance) -> str:
        self.asked.append(('after_unauthorized', name))
        if self.current != 'connected':
            raise Reconnect()
        return self.access

    def forget(self, name: str) -> None:
        self.asked.append(('forget', name))

    def secrets(self, name: str) -> tuple[str, ...]:
        return (self.access,)

    def park(self, name: str, reason: str) -> None:
        self.parked.append((name, reason))
        self.current = 'reconnect'

    def moved(self, name: str, url: str) -> bool:
        return False
