import asyncio
import copy
import dataclasses
import http.server
import io
import socket
import threading
import time
import urllib.error
from email.message import Message
from urllib.parse import parse_qs, urlsplit

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from httpx import Response
from jwt.algorithms import RSAAlgorithm

from skopax.app import create_app
from skopax.config import Settings
from skopax.oidc import OidcError
from skopax.sso import SignOn, SsoConfig, SsoHttp
from tests.conftest import settings

DISCOVERY_PATH = '/.well-known/openid-configuration'
SECRET = 'client-secret'
KEY_FP = b'sso-kit-session-secret-0123456789'
REDIRECT = 'https://skopax.example.com/authorized'
OSCAR = 'oscar.green@example.com'


def rsa_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


KEY = rsa_key()
OTHER_KEY = rsa_key()


def jwk(key, kid: str | None) -> dict:
    out = {**RSAAlgorithm.to_jwk(key.public_key(), as_dict=True), 'use': 'sig', 'alg': 'RS256'}
    return {**out, 'kid': kid} if kid is not None else out


class FakeIdp:
    def __init__(self, issuer: str = 'https://idp.example.com', kid: str = 'k1'):
        self.issuer = issuer
        self.kid = kid
        self.key = KEY
        self.fail = False
        self.calls: dict[str, int] = {}
        self.rotations = 0
        self.claims: dict = {'sub': OSCAR, 'email': OSCAR, 'roles': []}
        self.overrides: dict = {}
        self.access_claims: dict = {}
        self.signer = None
        self.status: int | None = None
        self.error_body = b'{"error": "invalid_client"}'
        self.response: dict | None = None
        self.nonce: str | None = None
        self.authorizations: list[dict] = []
        self.exchanges: list[dict] = []
        self.minted: list[str] = []
        self.on_loop: list[str] = []
        base = issuer.removesuffix('/')
        self.discovery: dict = {
            'issuer': issuer,
            'authorization_endpoint': f'{base}/authorize',
            'token_endpoint': f'{base}/token',
            'jwks_uri': f'{base}/jwks',
            'response_types_supported': ['code'],
            'id_token_signing_alg_values_supported': ['RS256'],
            'token_endpoint_auth_methods_supported': ['client_secret_post', 'client_secret_basic'],
            'code_challenge_methods_supported': ['S256'],
        }

    @property
    def discovery_url(self) -> str:
        return self.issuer.removesuffix('/') + DISCOVERY_PATH

    @property
    def jwks_url(self) -> str:
        return self.discovery['jwks_uri']

    def jwks(self) -> dict:
        return {'keys': [jwk(self.key, self.kid)]}

    def fetch(self, url: str) -> dict:
        self.calls[url] = self.calls.get(url, 0) + 1
        self._note_thread()
        if self.fail:
            raise OSError('the identity provider is unreachable')
        if url == self.discovery_url:
            return copy.deepcopy(self.discovery)
        if url == self.jwks_url:
            return self.jwks()
        raise OSError(f'no such document: {url}')

    def token(self, aud: str = 'skopax', key=None, kid: str | None = ..., ttl: int = 300, **claims) -> str:
        now = int(time.time())
        body = {'iss': self.issuer, 'aud': aud, 'sub': 'oscar.green@example.com', 'iat': now, 'exp': now + ttl, **claims}
        kid = self.kid if kid is ... else kid
        headers = {'kid': kid} if kid is not None else None
        return jwt.encode({k: v for k, v in body.items() if v is not None}, key or self.key, algorithm='RS256', headers=headers)

    def rotate(self) -> str:
        self.rotations += 1
        self.key = rsa_key()
        self.kid = f'rotated-{self.rotations}'
        return self.kid

    def authorize(self, url: str) -> dict:
        params = {k: v[0] for k, v in parse_qs(urlsplit(url).query).items()}
        self.authorizations.append(params)
        self.nonce = params.get('nonce')
        return params

    def exchange(self, url: str, data: dict[str, str], headers: dict[str, str]) -> dict:
        self.exchanges.append({'url': url, 'data': dict(data), 'headers': dict(headers)})
        self._note_thread()
        if self.status:
            raise urllib.error.HTTPError(url, self.status, 'refused', Message(), io.BytesIO(self.error_body))
        if self.response is not None:
            return copy.deepcopy(self.response)
        claims = {**self.claims, **self.overrides}
        minted = self.token(key=self.signer, **{'nonce': self.nonce, **claims})
        access = self.token(key=self.signer, **{**claims, 'nonce': None, **self.access_claims})
        self.minted.append(minted)
        return {'access_token': access, 'id_token': minted, 'token_type': 'Bearer'}

    def _note_thread(self) -> None:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return
        self.on_loop.append(threading.current_thread().name)


def idp_settings(idp: FakeIdp, **over) -> Settings:
    return dataclasses.replace(settings(), **{'sso_issuer': idp.issuer, 'sso_client_secret': SECRET, 'sso_redirect_uri': REDIRECT, **over})


def app_client(store, idp: FakeIdp, s: Settings | None = None, clock=None) -> TestClient:
    client = TestClient(create_app(s or idp_settings(idp), store, sso_http=SsoHttp(fetch=idp.fetch, exchange=idp.exchange), clock=clock), base_url='https://testserver')
    client.idp = idp
    return client


def log_in(client: TestClient, idp: FakeIdp, **claims) -> Response:
    idp.overrides = dict(claims)
    start = client.get('/login/sso', follow_redirects=False)
    params = idp.authorize(start.headers['location'])
    return client.get('/authorized', params={'code': 'auth-code', 'state': params['state']}, follow_redirects=False)


def sign_on(s: Settings, token_request=lambda request: {}, secret: str = SECRET) -> SignOn:
    idp = FakeIdp()
    s = dataclasses.replace(s, sso_issuer=idp.issuer, sso_redirect_uri=REDIRECT)
    return SignOn(lambda: SsoConfig.of(s, secret), SsoHttp(fetch=idp.fetch), token_request=token_request, fp_key=lambda: KEY_FP)


def redeem(signon: SignOn, code: str = 'auth-code') -> tuple[dict, OidcError]:
    _, pending = signon.start('/')
    try:
        signon.finish(pending, code, pending['state'], None)
    except OidcError as e:
        return pending, e
    raise AssertionError('the login went through')


class Listener:
    def __init__(self):
        self.sock = socket.create_server(('127.0.0.1', 0))
        self.sock.settimeout(0.1)
        self.accepted = 0
        self.stop = threading.Event()
        self.worker = threading.Thread(target=self._serve, daemon=True)
        self.worker.start()

    @property
    def url(self) -> str:
        return f'http://127.0.0.1:{self.sock.getsockname()[1]}'

    def _serve(self) -> None:
        while not self.stop.is_set():
            try:
                conn, _ = self.sock.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            self.accepted += 1
            conn.close()

    def close(self) -> None:
        self.stop.set()
        self.worker.join(5)
        self.sock.close()


class Scripted(http.server.BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def log_message(self, *args) -> None:
        pass

    def answer(self) -> None:
        length = int(self.headers.get('Content-Length') or 0)
        body_in = self.rfile.read(length) if length else b''
        self.server.seen.append({'method': self.command, 'path': self.path, 'headers': dict(self.headers), 'body': body_in})
        status, headers, body = self.server.answers.pop(0) if self.server.answers else (500, {}, b'{}')
        self.send_response(status)
        for name, value in {'Content-Type': 'application/json', **headers}.items():
            self.send_header(name, value)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Connection', 'close')
        self.end_headers()
        self.wfile.write(body)

    do_GET = answer
    do_POST = answer


def scripted_server(answers: list[tuple[int, dict, bytes]]) -> http.server.ThreadingHTTPServer:
    server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Scripted)
    server.answers, server.seen = list(answers), []
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def server_url(server: http.server.ThreadingHTTPServer) -> str:
    return f'http://127.0.0.1:{server.server_address[1]}'


def stop_server(server: http.server.ThreadingHTTPServer) -> None:
    server.shutdown()
    server.server_close()
