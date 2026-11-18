import ast
import base64
import datetime as dt
import hashlib
import hmac
import ipaddress
import json
import logging
import http.server
import socket
import ssl
import threading
import time
import types
import urllib.error
from pathlib import Path

import jwt
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.x509.oid import NameOID

from tests.sso_kit import KEY, OTHER_KEY, FakeIdp, Listener, jwk, scripted_server, server_url, stop_server
from skopax import auth, oidc
from skopax.clock import stamp
from skopax.oidc import (CLOCK_SKEW_S, DISCOVERY_TTL_S, KEYS_TTL_S, MAX_BODY, RETRY_S, STALE_MAX_S, Discovery, Endpoints, OidcError, Provider, SigningKeys, get_json,
                         url_problem, verify)

ISSUER = 'https://idp.example.com'


def reason(call) -> str:
    with pytest.raises(OidcError) as caught:
        call()
    return caught.value.reason


def wall_clock(monkeypatch, start: float = 1_800_000_000.0) -> list[float]:
    wall = [start]
    monkeypatch.setattr(oidc, 'time', types.SimpleNamespace(time=lambda: wall[0], monotonic=time.monotonic))
    return wall


def oidc_warnings(caplog) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.name == 'skopax.oidc' and r.levelno == logging.WARNING]


class Reply:
    def __init__(self, body: bytes, asked: list[int]):
        self.body = body
        self.asked = asked
        self.at = 0

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.asked.append(self.at)
        return False

    def read(self, size: int = -1) -> bytes:
        end = len(self.body) if size < 0 else self.at + size
        part, self.at = self.body[self.at:end], min(end, len(self.body))
        return part


class Drip:
    def __init__(self, now: list[float], step: float):
        self.now = now
        self.step = step
        self.reads = 0

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read1(self, size: int = -1) -> bytes:
        self.reads += 1
        self.now[0] += self.step
        if self.step <= 0:
            raise TimeoutError('timed out')
        return b' '


def auth_imports(source: str) -> list[str]:
    found = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.ImportFrom):
            names = {node.module or ''} | {f'{node.module}.{a.name}' for a in node.names}
            if node.level or any(n == 'skopax.auth' or n.startswith('skopax.auth.') for n in names):
                found.append(ast.unparse(node))
        elif isinstance(node, ast.Import):
            found += [f'import {a.name}' for a in node.names if a.name == 'skopax.auth' or a.name.startswith('skopax.auth.')]
    return found


def hs256(body: dict, secret: bytes, kid: str) -> str:
    def part(value: dict) -> bytes:
        return base64.urlsafe_b64encode(json.dumps(value, separators=(',', ':')).encode()).rstrip(b'=')
    signing = part({'alg': 'HS256', 'typ': 'JWT', 'kid': kid}) + b'.' + part(body)
    return (signing + b'.' + base64.urlsafe_b64encode(hmac.new(secret, signing, hashlib.sha256).digest()).rstrip(b'=')).decode()


def test_discovery_reads_endpoints_and_overrides_win():
    # given
    idp = FakeIdp()
    plain = Discovery(ISSUER, fetch=idp.fetch)
    split = Discovery(ISSUER, {'token': 'https://auth.example.com/oauth2/token'}, fetch=FakeIdp().fetch)

    # when
    found = plain.endpoints()
    again = plain.endpoints()
    overridden = split.endpoints()

    # then
    assert idp.discovery_url == ISSUER + oidc.DISCOVERY_PATH == 'https://idp.example.com/.well-known/openid-configuration'
    assert found == again == Endpoints(issuer=ISSUER, authorize='https://idp.example.com/authorize', token='https://idp.example.com/token', jwks='https://idp.example.com/jwks',
                                       auth_methods=('client_secret_post', 'client_secret_basic'), pkce=('S256',), discovered=True)
    assert idp.calls == {idp.discovery_url: 1}
    assert overridden == Endpoints(issuer=ISSUER, authorize='https://idp.example.com/authorize', token='https://auth.example.com/oauth2/token', jwks='https://idp.example.com/jwks',
                                   auth_methods=('client_secret_post', 'client_secret_basic'), pkce=('S256',), discovered=True)
    assert plain.status()['ok'] is True and plain.status()['error'] is None


def test_all_three_overrides_skip_discovery():
    # given
    idp = FakeIdp()
    explicit = {'authorize': 'https://login.example.com/authorize', 'token': 'https://auth.example.com/oauth2/token', 'jwks': 'https://auth.example.com/oauth2/jwks'}
    two = {k: v for k, v in explicit.items() if k != 'jwks'}

    # when
    found = Discovery(ISSUER, explicit, fetch=idp.fetch).endpoints()
    calls_after_three = dict(idp.calls)
    partly = Discovery(ISSUER, two, fetch=idp.fetch).endpoints()

    # then
    assert found == Endpoints(issuer=ISSUER, authorize='https://login.example.com/authorize', token='https://auth.example.com/oauth2/token', jwks='https://auth.example.com/oauth2/jwks',
                              discovered=False)
    assert calls_after_three == {}
    assert idp.calls == {idp.discovery_url: 1}
    assert partly.jwks == 'https://idp.example.com/jwks' and partly.discovered is True


def test_discovery_issuer_must_match_exactly():
    # given
    look_alike = FakeIdp()
    look_alike.discovery['issuer'] = 'https://idp.example.com/other'
    doubled = FakeIdp()
    doubled.discovery['issuer'] = 'https://idp.example.com//'
    slash_configured = FakeIdp()
    slash_in_document = FakeIdp(issuer='https://idp.example.com/')

    # when
    refused = Discovery(ISSUER, fetch=look_alike.fetch)
    why = reason(refused.endpoints)
    configured_with_slash = Discovery('https://idp.example.com/', fetch=slash_configured.fetch).endpoints()
    document_with_slash = Discovery(ISSUER, fetch=slash_in_document.fetch).endpoints()

    # then
    assert why == 'issuer'
    assert refused.status()['ok'] is False and refused.status()['error'] == 'issuer'
    assert reason(Discovery(ISSUER, fetch=doubled.fetch).endpoints) == 'issuer'
    assert slash_configured.calls == {'https://idp.example.com/.well-known/openid-configuration': 1}
    assert configured_with_slash.issuer == 'https://idp.example.com'
    assert document_with_slash.issuer == 'https://idp.example.com/'


def test_endpoint_urls_must_be_https_or_loopback():
    # given
    idp = FakeIdp()
    plain_http = FakeIdp()
    plain_http.discovery['token_endpoint'] = 'http://idp.example.com/token'
    local = FakeIdp()
    local.discovery['token_endpoint'] = 'http://localhost:8081/default/token'

    # when
    problems = {url: url_problem(url) for url in ('http://idp.example.com/token', 'https://u:p@idp.example.com', 'https://idp.example.com/token#frag', 'ftp://idp.example.com/token',
                                                   'idp.example.com/token', 'http://localhost@idp.example.com/token', 'http://localhost:8081/default/token',
                                                   'http://127.0.0.1:8081/token', 'http://[::1]:8081/token', 'https://idp.example.com/token')}

    # then
    assert problems == {
        'http://idp.example.com/token': 'sso.url_scheme',
        'https://u:p@idp.example.com': 'sso.url_userinfo',
        'https://idp.example.com/token#frag': 'sso.url_fragment',
        'ftp://idp.example.com/token': 'sso.url_scheme',
        'idp.example.com/token': 'sso.url_scheme',
        'http://localhost@idp.example.com/token': 'sso.url_scheme',
        'http://localhost:8081/default/token': None,
        'http://127.0.0.1:8081/token': None,
        'http://[::1]:8081/token': None,
        'https://idp.example.com/token': None,
    }
    assert reason(Discovery(ISSUER, {'token': 'http://idp.example.com/token'}, fetch=idp.fetch).endpoints) == 'endpoint'
    assert reason(Discovery(ISSUER, {'jwks': 'https://u:p@idp.example.com'}, fetch=idp.fetch).endpoints) == 'endpoint'
    assert reason(Discovery('http://idp.example.com', fetch=idp.fetch).endpoints) == 'endpoint'
    assert reason(Discovery('https://u:p@idp.example.com', fetch=idp.fetch).endpoints) == 'endpoint'
    assert idp.calls == {}
    assert Discovery('http://localhost:8081', fetch=FakeIdp(issuer='http://localhost:8081').fetch).endpoints().issuer == 'http://localhost:8081'
    assert reason(Discovery(ISSUER, fetch=plain_http.fetch).endpoints) == 'endpoint'
    assert Discovery(ISSUER, fetch=local.fetch).endpoints().token == 'http://localhost:8081/default/token'
    assert Discovery(ISSUER, {'token': 'http://localhost:8081/default/token'}, fetch=idp.fetch).endpoints().token == 'http://localhost:8081/default/token'


def test_discovery_keeps_the_last_good_document_for_a_day(monkeypatch, caplog):
    # given
    wall = wall_clock(monkeypatch)
    now = [1000.0]
    idp = FakeIdp()
    discovery = Discovery(ISSUER, fetch=idp.fetch, clock=lambda: now[0])
    good = discovery.endpoints()
    healthy = discovery.status()
    idp.fail = True
    caplog.set_level(logging.WARNING, logger='skopax.oidc')

    # when
    now[0] += DISCOVERY_TTL_S + 1
    wall[0] += 3600
    stale = discovery.endpoints()
    failing = discovery.status()
    now[0] += RETRY_S - 1
    wall[0] += 60
    within_retry = discovery.endpoints()
    calls_within_retry = idp.calls[idp.discovery_url]
    now[0] += 1
    after_retry = discovery.endpoints()
    calls_after_retry = idp.calls[idp.discovery_url]
    still_failing = discovery.status()
    now[0] = 1000.0 + STALE_MAX_S + 1
    expired = reason(discovery.endpoints)
    expired_again = reason(discovery.endpoints)

    # then
    assert healthy == {'ok': True, 'since': stamp(1_800_000_000.0), 'error': None}
    assert stale == within_retry == after_retry == good
    assert failing == {'ok': False, 'since': stamp(1_800_003_600.0), 'error': 'discovery'}
    assert still_failing == failing
    assert calls_within_retry == 2
    assert calls_after_retry == 3
    assert expired == expired_again == 'discovery'
    assert idp.calls[idp.discovery_url] == 4
    assert len(oidc_warnings(caplog)) == 1


def test_discovery_recovers_and_logs_each_failure_spell(monkeypatch, caplog):
    # given
    wall = wall_clock(monkeypatch)
    now = [1000.0]
    idp = FakeIdp()
    discovery = Discovery(ISSUER, fetch=idp.fetch, clock=lambda: now[0])
    discovery.endpoints()
    caplog.set_level(logging.WARNING, logger='skopax.oidc')

    # when
    idp.fail = True
    now[0] += DISCOVERY_TTL_S + 1
    discovery.endpoints()
    idp.fail = False
    now[0] += RETRY_S
    wall[0] += 120
    discovery.endpoints()
    recovered = discovery.status()
    idp.fail = True
    now[0] += DISCOVERY_TTL_S + 1
    discovery.endpoints()

    # then
    assert recovered == {'ok': True, 'since': stamp(1_800_000_120.0), 'error': None}
    assert len(oidc_warnings(caplog)) == 2


def test_jwks_rotation_refetches_once_and_keeps_last_good(caplog):
    # given
    now = [1000.0]
    idp = FakeIdp()
    keys = SigningKeys(idp.jwks_url, idp.fetch, clock=lambda: now[0])

    def check(token: str) -> dict:
        return verify(token, keys, issuers=[ISSUER], audiences=['skopax'])

    first = check(idp.token())
    kidless_single = check(idp.token(kid=None))
    two_keys = SigningKeys(idp.jwks_url, lambda url: {'keys': [jwk(KEY, 'k1'), jwk(OTHER_KEY, 'k2')]})
    kidless_two = reason(lambda: verify(idp.token(kid=None), two_keys, issuers=[ISSUER], audiences=['skopax']))
    old_token = idp.token()
    caplog.set_level(logging.WARNING, logger='skopax.oidc')

    # when
    now[0] += RETRY_S + 1
    new_kid = idp.rotate()
    rotated = check(idp.token())
    rotated_again = check(idp.token())
    calls_after_rotation = idp.calls[idp.jwks_url]
    old_kid = reason(lambda: check(old_token))
    now[0] += RETRY_S + 1
    flood = [reason(lambda i=i: check(idp.token(kid=f'unknown-{i}'))) for i in range(10)]
    calls_after_flood = idp.calls[idp.jwks_url]
    last_good_at = now[0]
    idp.fail = True
    now[0] += KEYS_TTL_S + 1
    kept = check(idp.token())
    calls_while_failing = idp.calls[idp.jwks_url]
    now[0] = last_good_at + STALE_MAX_S + 1
    too_old = reason(lambda: check(idp.token()))

    # then
    assert first['sub'] == kidless_single['sub'] == 'oscar.green@example.com'
    assert kidless_two == 'keys'
    assert new_kid != 'k1'
    assert rotated['sub'] == rotated_again['sub'] == 'oscar.green@example.com'
    assert calls_after_rotation == 2
    assert old_kid == 'keys'
    assert flood == ['keys'] * 10
    assert calls_after_flood == 3
    assert kept['sub'] == 'oscar.green@example.com'
    assert calls_while_failing == 4
    assert too_old == 'unreachable'
    assert len(oidc_warnings(caplog)) == 1


def test_a_token_without_kid_needs_exactly_one_key():
    # given
    idp = FakeIdp()
    two = {'keys': [jwk(KEY, 'k1'), jwk(OTHER_KEY, 'k2')]}
    single = SigningKeys(idp.jwks_url, idp.fetch)
    double = SigningKeys(idp.jwks_url, lambda url: two)
    unnamed = SigningKeys(idp.jwks_url, lambda url: {'keys': [jwk(KEY, None)]})
    mixed = SigningKeys(idp.jwks_url, lambda url: {'keys': [jwk(KEY, 'k1'), jwk(OTHER_KEY, None)]})

    # when
    kidless = idp.token(kid=None)

    # then
    assert verify(kidless, single, issuers=[ISSUER], audiences=['skopax'])['sub'] == 'oscar.green@example.com'
    assert reason(lambda: verify(kidless, double, issuers=[ISSUER], audiences=['skopax'])) == 'keys'
    assert verify(idp.token(), double, issuers=[ISSUER], audiences=['skopax'])['sub'] == 'oscar.green@example.com'
    assert verify(idp.token(key=OTHER_KEY, kid='k2'), double, issuers=[ISSUER], audiences=['skopax'])['sub'] == 'oscar.green@example.com'
    assert verify(kidless, unnamed, issuers=[ISSUER], audiences=['skopax'])['sub'] == 'oscar.green@example.com'
    assert reason(lambda: verify(idp.token(), unnamed, issuers=[ISSUER], audiences=['skopax'])) == 'keys'
    assert reason(lambda: verify(idp.token(key=OTHER_KEY, kid='k9'), mixed, issuers=[ISSUER], audiences=['skopax'])) == 'keys'
    assert verify(idp.token(), mixed, issuers=[ISSUER], audiences=['skopax'])['sub'] == 'oscar.green@example.com'


def test_only_rsa_signing_keys_are_kept():
    # given
    idp = FakeIdp()
    offered = {'keys': [{**jwk(KEY, 'enc'), 'use': 'enc'}, {**jwk(KEY, 'ps'), 'alg': 'PS256'}, {'kty': 'oct', 'k': 'c2VjcmV0', 'kid': 'oct'}, {'kty': 'RSA', 'kid': 'broken'},
                       {k: v for k, v in jwk(KEY, 'bare').items() if k not in ('use', 'alg')}]}
    keys = SigningKeys(idp.jwks_url, lambda url: offered)

    # when
    refused = {kid: reason(lambda kid=kid: verify(idp.token(kid=kid), keys, issuers=[ISSUER], audiences=['skopax'])) for kid in ('enc', 'ps', 'oct', 'broken')}
    bare = verify(idp.token(kid='bare'), keys, issuers=[ISSUER], audiences=['skopax'])

    # then
    assert refused == {'enc': 'keys', 'ps': 'keys', 'oct': 'keys', 'broken': 'keys'}
    assert bare['sub'] == 'oscar.green@example.com'


def test_algorithm_is_pinned_not_taken_from_the_header():
    # given
    idp = FakeIdp()
    keys = SigningKeys(idp.jwks_url, idp.fetch)
    now = int(time.time())
    body = {'iss': ISSUER, 'aud': 'skopax', 'sub': 'oscar.green@example.com', 'iat': now, 'exp': now + 300}
    public_pem = KEY.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)

    def check(token: str) -> dict:
        return verify(token, keys, issuers=[ISSUER], audiences=['skopax'])

    # when
    confused = reason(lambda: check(hs256(body, public_pem, 'k1')))
    unsigned = reason(lambda: check(jwt.encode(body, None, algorithm='none', headers={'kid': 'k1'})))
    calls_before_signed = dict(idp.calls)
    forged = reason(lambda: check(jwt.encode(body, OTHER_KEY, algorithm='RS256', headers={'kid': 'k1'})))
    genuine = check(jwt.encode(body, KEY, algorithm='RS256', headers={'kid': 'k1'}))

    # then
    assert oidc.ALGORITHMS == ('RS256',)
    assert confused == 'algorithm'
    assert unsigned == 'algorithm'
    assert calls_before_signed == {}
    assert forged == 'signature'
    assert genuine == body


def test_clock_skew_is_thirty_seconds_each_way():
    # given
    idp = FakeIdp()
    keys = SigningKeys(idp.jwks_url, idp.fetch)
    now = int(time.time())

    def check(**claims) -> dict:
        return verify(idp.token(**claims), keys, issuers=[ISSUER], audiences=['skopax'])

    # when
    late_but_inside = check(iat=now - 300, exp=now - 20)
    late_outside = reason(lambda: check(iat=now - 300, exp=now - 40))
    early_iat_inside = check(iat=now + 20)
    early_iat_outside = reason(lambda: check(iat=now + 40))
    early_nbf_inside = check(nbf=now + 20)
    early_nbf_outside = reason(lambda: check(nbf=now + 40))

    # then
    assert CLOCK_SKEW_S == 30
    assert late_but_inside['exp'] == now - 20
    assert late_outside == 'expired'
    assert early_iat_inside['iat'] == now + 20
    assert early_iat_outside == 'expired'
    assert early_nbf_inside['nbf'] == now + 20
    assert early_nbf_outside == 'expired'


def test_required_claims_are_enforced():
    # given
    idp = FakeIdp()
    keys = SigningKeys(idp.jwks_url, idp.fetch)

    # when
    missing = {claim: reason(lambda claim=claim: verify(idp.token(**{claim: None}), keys, issuers=[ISSUER], audiences=['skopax'])) for claim in ('exp', 'iat', 'sub')}
    complete = verify(idp.token(), keys, issuers=[ISSUER], audiences=['skopax'])

    # then
    assert missing == {'exp': 'claims', 'iat': 'claims', 'sub': 'claims'}
    assert complete['sub'] == 'oscar.green@example.com'


def test_audience_issuer_nonce_and_client_rules():
    # given
    idp = FakeIdp()
    keys = SigningKeys(idp.jwks_url, idp.fetch)

    def check(token: str, audiences=('skopax',), **rules) -> dict:
        return verify(token, keys, issuers=[ISSUER], audiences=audiences, **rules)

    # when
    wrong_audience = reason(lambda: check(idp.token(aud='other-app')))
    wrong_issuer = reason(lambda: check(idp.token(iss='https://idp.example.com/other')))
    no_nonce = reason(lambda: check(idp.token(), nonce='n-1'))
    other_nonce = reason(lambda: check(idp.token(nonce='n-2'), nonce='n-1'))
    same_nonce = check(idp.token(nonce='n-1'), nonce='n-1')
    several_without_azp = reason(lambda: check(idp.token(aud=['skopax', 'other-app']), client_id='skopax'))
    several_with_azp = check(idp.token(aud=['skopax', 'other-app'], azp='skopax'), client_id='skopax')
    other_azp = reason(lambda: check(idp.token(azp='other-app'), client_id='skopax'))
    single_without_azp = check(idp.token(), client_id='skopax')
    mcp = {'audiences': ('skopax-mcp',), 'allowed_clients': ('skopax-mcp',)}
    allowed_cid = check(idp.token(aud='skopax-mcp', cid='skopax-mcp'), **mcp)
    allowed_client_id = check(idp.token(aud='skopax-mcp', client_id='skopax-mcp'), **mcp)
    no_client = reason(lambda: check(idp.token(aud='skopax-mcp'), **mcp))
    other_client = reason(lambda: check(idp.token(aud='skopax-mcp', client_id='other-app'), **mcp))
    azp_first = reason(lambda: check(idp.token(aud='skopax-mcp', azp='other-app', cid='skopax-mcp'), **mcp))
    any_client = check(idp.token(aud='skopax-mcp'), audiences=('skopax-mcp',))

    # then
    assert check(idp.token())['aud'] == 'skopax'
    assert wrong_audience == 'audience'
    assert wrong_issuer == 'issuer'
    assert no_nonce == other_nonce == 'nonce'
    assert same_nonce['nonce'] == 'n-1'
    assert several_without_azp == 'client'
    assert several_with_azp['azp'] == 'skopax'
    assert other_azp == 'client'
    assert single_without_azp['aud'] == 'skopax'
    assert allowed_cid['cid'] == 'skopax-mcp'
    assert allowed_client_id['client_id'] == 'skopax-mcp'
    assert no_client == other_client == azp_first == 'client'
    assert any_client['aud'] == 'skopax-mcp'


def test_a_huge_or_non_json_document_is_refused(monkeypatch):
    # given
    seen = []
    asked = []

    def opener(request, timeout, watch):
        seen.append((request.full_url, request.get_header('Accept'), timeout))
        return Reply(bodies.pop(0), asked)

    filler = b'{"pad": "' + b'x' * (MAX_BODY - 11) + b'"}'
    one_over = b'{"pad": "' + b'x' * (MAX_BODY - 10) + b'"}'
    bodies = [b'{"pad": "' + b'x' * (2 << 20) + b'"}', one_over, b'[1, 2]', b'not json', filler, b'{"issuer": "https://idp.example.com"}']
    monkeypatch.setattr(oidc, 'open_url', opener)

    # when
    huge = reason(lambda: get_json('https://idp.example.com/.well-known/openid-configuration'))
    over = reason(lambda: get_json('https://idp.example.com/.well-known/openid-configuration'))
    listed = reason(lambda: get_json('https://idp.example.com/.well-known/openid-configuration'))
    garbage = reason(lambda: get_json('https://idp.example.com/.well-known/openid-configuration'))
    at_cap = get_json('https://idp.example.com/.well-known/openid-configuration')
    good = get_json('https://idp.example.com/.well-known/openid-configuration')

    # then
    assert len(filler) == MAX_BODY == 1 << 20 and len(one_over) == MAX_BODY + 1
    assert huge == over == listed == garbage == 'discovery'
    assert len(at_cap['pad']) == MAX_BODY - 11
    assert good == {'issuer': 'https://idp.example.com'}
    assert seen == [('https://idp.example.com/.well-known/openid-configuration', 'application/json', oidc.FETCH_TIMEOUT_S)] * 6
    assert asked == [MAX_BODY + 1, MAX_BODY + 1, 6, 8, MAX_BODY, 37]
    assert oidc.FETCH_TIMEOUT_S == 10


def test_provider_keeps_one_key_set_per_jwks_url():
    # given
    now = [1000.0]
    idp = FakeIdp()
    provider = Provider(ISSUER, fetch=idp.fetch, clock=lambda: now[0])

    # when
    first = provider.keys()
    same = provider.keys()
    idp.discovery['jwks_uri'] = 'https://idp.example.com/jwks-next'
    now[0] += DISCOVERY_TTL_S + 1
    moved = provider.keys()

    # then
    assert first is same
    assert first.url == 'https://idp.example.com/jwks'
    assert moved is not first and moved.url == 'https://idp.example.com/jwks-next'
    assert provider.endpoints().jwks == 'https://idp.example.com/jwks-next'
    assert provider.status() == provider.discovery.status()


def test_oidc_stands_alone_and_auth_reexports_it():
    # given
    source = Path(oidc.__file__).read_text(encoding='utf-8')
    reaching = ('from skopax import auth', 'from skopax import clock, auth', 'from skopax.auth import AuthError', 'import skopax.auth', 'from . import auth',
                'from .auth import AuthError', 'from .clock import stamp')

    # when
    own = auth_imports(source)
    caught = [sample for sample in reaching if auth_imports(sample)]

    # then
    assert own == []
    assert caught == list(reaching)
    assert auth_imports('from skopax.clock import stamp\nimport jwt\n') == []
    assert issubclass(OidcError, oidc.AuthError)
    assert auth.AuthError is oidc.AuthError
    assert not hasattr(auth, 'SigningKeys') and not hasattr(auth, 'get_json')
    assert 'SigningKeys' not in auth.__all__ and 'get_json' not in auth.__all__


def test_cache_windows_have_the_planned_values():
    # given
    windows = (oidc.DISCOVERY_TTL_S, oidc.KEYS_TTL_S, oidc.RETRY_S, oidc.STALE_MAX_S)

    # when
    planned = (3600, 600, 30, 24 * 3600)

    # then
    assert windows == planned


def test_an_empty_or_unusable_key_set_keeps_the_last_good_keys(caplog):
    # given
    now = [1000.0]
    idp = FakeIdp()
    calls = []
    replies = [idp.jwks(), {'keys': []}, {'keys': [{**jwk(KEY, 'enc'), 'use': 'enc'}]}]

    def fetch(url: str) -> dict:
        calls.append(url)
        return replies[min(len(calls), len(replies)) - 1]

    keys = SigningKeys(idp.jwks_url, fetch, clock=lambda: now[0])

    def check(token: str, with_keys: SigningKeys = keys) -> dict:
        return verify(token, with_keys, issuers=[ISSUER], audiences=['skopax'])

    check(idp.token())
    caplog.set_level(logging.WARNING, logger='skopax.oidc')
    down = FakeIdp()
    down.fail = True

    # when
    now[0] += KEYS_TTL_S + 1
    after_empty = check(idp.token())
    now[0] += RETRY_S - 1
    unknown_within_retry = reason(lambda: check(idp.token(kid='k9')))
    calls_within_retry = len(calls)
    now[0] += 1
    unknown_after_retry = reason(lambda: check(idp.token(kid='k9')))
    after_unusable = check(idp.token())
    one_spell = len(oidc_warnings(caplog))
    never_empty = reason(lambda: check(idp.token(), SigningKeys(idp.jwks_url, lambda url: {'keys': []})))
    never_unusable = reason(lambda: check(idp.token(), SigningKeys(idp.jwks_url, lambda url: {'keys': [{**jwk(KEY, 'k1'), 'use': 'enc'}]})))
    never_reached = reason(lambda: check(idp.token(), SigningKeys(down.jwks_url, down.fetch)))

    # then
    assert after_empty['sub'] == after_unusable['sub'] == 'oscar.green@example.com'
    assert unknown_within_retry == unknown_after_retry == 'keys'
    assert calls_within_retry == 2
    assert len(calls) == 3
    assert never_empty == never_unusable == never_reached == 'unreachable'
    assert one_spell == 1


def test_verify_refuses_a_plain_string_where_a_list_is_meant():
    # given
    idp = FakeIdp()
    keys = SigningKeys(idp.jwks_url, idp.fetch)
    token = idp.token(aud='skopax-mcp', cid='skopax-mcp')
    misuses = ({'issuers': ISSUER, 'audiences': ['skopax-mcp']}, {'issuers': [ISSUER], 'audiences': 'skopax-mcp'},
               {'issuers': [ISSUER], 'audiences': ['skopax-mcp'], 'allowed_clients': 'skopax-mcp-admin'})

    # when
    refused = []
    for rules in misuses:
        with pytest.raises(TypeError) as caught:
            verify(token, keys, **rules)
        refused.append(caught.type)
    calls_before_control = dict(idp.calls)
    accepted = verify(token, keys, issuers=(ISSUER,), audiences=('skopax-mcp',), allowed_clients=('skopax-mcp',))

    # then
    assert refused == [TypeError] * 3
    assert calls_before_control == {}
    assert accepted['cid'] == 'skopax-mcp'


def test_deeply_nested_json_is_refused_and_the_last_good_document_stays(monkeypatch):
    # given
    now = [1000.0]
    asked = []
    deep = b'{"a":' + b'[' * 500000 + b']' * 500000 + b'}'
    bodies = [json.dumps(FakeIdp().discovery).encode(), deep, deep]
    monkeypatch.setattr(oidc, 'open_url', lambda request, timeout, watch: Reply(bodies.pop(0), asked))
    discovery = Discovery(ISSUER, clock=lambda: now[0])
    good = discovery.endpoints()

    # when
    now[0] += DISCOVERY_TTL_S + 1
    stale = discovery.endpoints()
    failing = discovery.status()
    direct = reason(lambda: get_json('https://idp.example.com/.well-known/openid-configuration'))

    # then
    assert len(deep) <= MAX_BODY
    assert stale == good
    assert failing['ok'] is False and failing['error'] == 'discovery'
    assert direct == 'discovery'
    assert bodies == []


def test_status_does_not_wait_for_a_fetch_in_flight():
    # given
    entered, release = threading.Event(), threading.Event()
    idp = FakeIdp()

    def slow(url: str) -> dict:
        entered.set()
        release.wait(5)
        return idp.fetch(url)

    provider = Provider(ISSUER, fetch=slow)
    loader = threading.Thread(target=provider.endpoints, daemon=True)
    answers = []
    reader = threading.Thread(target=lambda: answers.append((provider.discovery.status(), provider.status())), daemon=True)

    # when
    loader.start()
    fetching = entered.wait(5)
    reader.start()
    reader.join(2)
    answered_while_fetching = not reader.is_alive()
    release.set()
    loader.join(5)
    reader.join(5)

    # then
    assert fetching
    assert answered_while_fetching
    assert len(answers) == 1 and answers[0][0] == answers[0][1]
    assert (answers[0][0]['ok'], answers[0][0]['error']) == (True, None)
    assert provider.status()['ok'] is True and provider.status()['since'] is not None


def test_status_is_one_snapshot_even_while_the_health_flips(monkeypatch):
    # given
    wall = wall_clock(monkeypatch)
    now = [1000.0]
    idp = FakeIdp()
    discovery = Discovery(ISSUER, fetch=idp.fetch, clock=lambda: now[0])
    seen = []
    real_stamp = oidc.stamp

    def watching_stamp(at: float) -> str:
        seen.append(discovery.status())
        return real_stamp(at)

    monkeypatch.setattr(oidc, 'stamp', watching_stamp)

    # when
    discovery.endpoints()
    idp.fail = True
    now[0] += DISCOVERY_TTL_S + 1
    discovery.endpoints()
    failing = discovery.status()
    idp.fail = False
    now[0] += RETRY_S
    wall[0] += 60
    discovery.endpoints()
    recovered = discovery.status()

    # then
    assert len(seen) == 3
    assert all((answer['ok'] is False) == (answer['error'] is not None) for answer in seen + [failing, recovered])
    assert (failing['ok'], failing['error']) == (False, 'discovery') and failing['since'] is not None
    assert (recovered['ok'], recovered['error']) == (True, None) and recovered['since'] != failing['since']


def test_a_dripping_response_stops_at_the_total_deadline(monkeypatch):
    # given
    now = [500.0]
    drip = Drip(now, 1.0)
    monkeypatch.setattr(oidc, 'time', types.SimpleNamespace(time=time.time, monotonic=lambda: now[0]))
    monkeypatch.setattr(oidc, 'open_url', lambda request, timeout, watch: drip)

    # when
    caught = reason(lambda: get_json('https://idp.example.com/.well-known/openid-configuration'))
    stalled = Drip(now, 0)
    monkeypatch.setattr(oidc, 'open_url', lambda request, timeout, watch: stalled)
    timed_out = reason(lambda: get_json('https://idp.example.com/.well-known/openid-configuration'))

    # then
    assert caught == timed_out == 'unreachable' and stalled.reads == 1
    assert oidc.FETCH_TOTAL_S == 10
    assert drip.reads <= oidc.FETCH_TOTAL_S + 1


def header_dripper(stop: threading.Event, most: int) -> tuple[socket.socket, threading.Thread]:
    server = socket.create_server(('127.0.0.1', 0))
    server.settimeout(5)

    def serve() -> None:
        try:
            conn, _ = server.accept()
        except OSError:
            return
        with conn:
            try:
                conn.recv(4096)
                conn.sendall(b'HTTP/1.1 200 OK\r\nX-Slow: ')
                for _ in range(most):
                    if stop.wait(1):
                        return
                    conn.sendall(b'a')
            except OSError:
                return

    worker = threading.Thread(target=serve, daemon=True)
    worker.start()
    return server, worker


def test_a_server_dripping_headers_stops_at_the_deadline_and_frees_the_lock(monkeypatch):
    # given
    monkeypatch.setattr(oidc, 'FETCH_TOTAL_S', 1.5)
    stop = threading.Event()
    server, worker = header_dripper(stop, 8)
    issuer = f'http://127.0.0.1:{server.getsockname()[1]}'
    discovery = Discovery(issuer)
    took: dict[str, float] = {}
    reasons: dict[str, str] = {}
    started = time.monotonic()

    def call(name: str) -> None:
        try:
            discovery.endpoints()
            reasons[name] = 'ok'
        except OidcError as e:
            reasons[name] = e.reason
        took[name] = time.monotonic() - started

    first = threading.Thread(target=call, args=('first',), daemon=True)
    second = threading.Thread(target=call, args=('second',), daemon=True)

    # when
    first.start()
    time.sleep(0.2)
    second.start()
    first.join(10)
    second.join(10)
    stop.set()
    worker.join(5)
    server.close()

    # then
    assert reasons == {'first': 'unreachable', 'second': 'unreachable'}
    assert took['first'] <= oidc.FETCH_TOTAL_S + 1 and took['second'] <= oidc.FETCH_TOTAL_S + 1
    assert discovery.status()['error'] == 'unreachable'


def self_signed(tmp_path) -> tuple[str, str]:
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, '127.0.0.1')])
    now = dt.datetime.now(dt.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(KEY.public_key()).serial_number(1)
            .not_valid_before(now - dt.timedelta(minutes=5)).not_valid_after(now + dt.timedelta(hours=1))
            .add_extension(x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address('127.0.0.1'))]), critical=False)
            .sign(KEY, hashes.SHA256()))
    cert_path, key_path = tmp_path / 'cert.pem', tmp_path / 'key.pem'
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(KEY.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    return str(cert_path), str(key_path)


def one_shot_server(handle) -> tuple[socket.socket, threading.Thread]:
    server = socket.create_server(('127.0.0.1', 0))
    server.settimeout(5)

    def serve() -> None:
        try:
            conn, _ = server.accept()
        except OSError:
            return
        with conn:
            try:
                handle(conn)
            except OSError:
                return

    worker = threading.Thread(target=serve, daemon=True)
    worker.start()
    return server, worker


def drip(conn: socket.socket, stop: threading.Event, head: bytes, most: int, every: float = 0.4) -> None:
    conn.sendall(head)
    for _ in range(most):
        if stop.wait(every):
            return
        conn.sendall(b'a')


def late_connect(monkeypatch, port: int, delay: float) -> None:
    real = socket.create_connection

    def late(address, *args, **kwargs):
        made = real(address, *args, **kwargs)
        if address[1] == port:
            time.sleep(delay)
        return made

    monkeypatch.setattr(socket, 'create_connection', late)


def timed_reason(url: str) -> tuple[str, float]:
    started = time.monotonic()
    try:
        get_json(url)
        return 'ok', time.monotonic() - started
    except OidcError as e:
        return e.reason, time.monotonic() - started


def test_https_works_and_a_dripping_tls_handshake_stops_at_the_deadline(monkeypatch, tmp_path):
    # given
    cert, key = self_signed(tmp_path)
    monkeypatch.setattr(ssl, '_create_default_https_context', lambda: ssl.create_default_context(cafile=cert))
    serving = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    serving.load_cert_chain(cert, key)

    def answer(conn: socket.socket) -> None:
        with serving.wrap_socket(conn, server_side=True) as tls:
            tls.recv(4096)
            body = b'{"issuer": "https://127.0.0.1"}'
            tls.sendall(b'HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: %d\r\nConnection: close\r\n\r\n%s' % (len(body), body))

    good_server, good_worker = one_shot_server(answer)
    good = get_json(f'https://127.0.0.1:{good_server.getsockname()[1]}/.well-known/openid-configuration')
    good_worker.join(5)
    good_server.close()
    monkeypatch.setattr(oidc, 'FETCH_TOTAL_S', 1.5)
    monkeypatch.setattr(oidc, 'FETCH_TIMEOUT_S', 20)
    stop = threading.Event()
    server, worker = one_shot_server(lambda conn: drip(conn, stop, b'\x16\x03\x03\x40\x00', 20))

    # when
    caught, took = timed_reason(f'https://127.0.0.1:{server.getsockname()[1]}/.well-known/openid-configuration')
    stop.set()
    worker.join(5)
    server.close()

    # then
    assert good == {'issuer': 'https://127.0.0.1'}
    assert caught == 'unreachable' and took <= 1.5 + 1


def test_a_connect_that_ends_after_the_deadline_stops_at_once(monkeypatch):
    # given
    monkeypatch.setattr(oidc, 'FETCH_TOTAL_S', 1.0)
    monkeypatch.setattr(oidc, 'FETCH_TIMEOUT_S', 20)
    stop = threading.Event()
    server, worker = one_shot_server(lambda conn: (conn.recv(4096), drip(conn, stop, b'HTTP/1.1 200 OK\r\nX-Slow: ', 20)))
    port = server.getsockname()[1]
    late_connect(monkeypatch, port, 1.3)

    # when
    caught, took = timed_reason(f'http://127.0.0.1:{port}/.well-known/openid-configuration')
    stop.set()
    worker.join(5)
    server.close()

    # then
    assert caught == 'unreachable' and took <= 1.3 + 1


def test_a_slow_connect_leaves_the_tls_handshake_only_the_time_left(monkeypatch):
    # given
    monkeypatch.setattr(oidc, 'FETCH_TOTAL_S', 3.0)
    monkeypatch.setattr(oidc, 'FETCH_TIMEOUT_S', 20)
    stop = threading.Event()
    server, worker = one_shot_server(lambda conn: drip(conn, stop, b'\x16\x03\x03\x40\x00', 20))
    port = server.getsockname()[1]
    late_connect(monkeypatch, port, 2.5)

    # when
    caught, took = timed_reason(f'https://127.0.0.1:{port}/.well-known/openid-configuration')
    stop.set()
    worker.join(5)
    server.close()

    # then
    assert caught == 'unreachable' and took <= 3.0 + 1


class Statuses(http.server.BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def log_message(self, *args) -> None:
        pass

    def do_GET(self) -> None:
        status, body = self.server.answers.pop(0) if self.server.answers else (500, b'{}')
        self.server.hits += 1
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Connection', 'close')
        self.end_headers()
        self.wfile.write(body)


def status_server(answers: list[tuple[int, bytes]]) -> http.server.ThreadingHTTPServer:
    server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Statuses)
    server.answers, server.hits = answers, 0
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def test_an_http_error_status_is_a_discovery_failure_with_backoff_and_the_last_good_document():
    # given
    now = [1000.0]
    good = {**FakeIdp().discovery}
    server = status_server([])
    issuer = f'http://127.0.0.1:{server.server_address[1]}'
    good |= {'issuer': issuer, 'authorization_endpoint': issuer + '/authorize', 'token_endpoint': issuer + '/token', 'jwks_uri': issuer + '/jwks'}
    server.answers += [(200, json.dumps(good).encode()), (503, b'{"error": "down"}')]
    discovery = Discovery(issuer, clock=lambda: now[0])
    first = discovery.endpoints()

    # when
    now[0] += DISCOVERY_TTL_S + 1
    stale = discovery.endpoints()
    failing = discovery.status()
    now[0] += RETRY_S - 1
    within_retry = discovery.endpoints()
    hits = server.hits
    server.answers += [(404, b'{}'), (404, b'{}')]
    fresh = reason(Discovery(issuer + '/missing').endpoints)
    keys = reason(lambda: SigningKeys(issuer + '/jwks').key_for('k1'))
    server.shutdown()
    server.server_close()

    # then
    assert first.token == issuer + '/token' and stale == first == within_retry
    assert (failing['ok'], failing['error']) == (False, 'discovery')
    assert hits == 2
    assert fresh == 'discovery' and keys == 'unreachable'


def test_a_value_error_after_the_deadline_fired_is_unreachable(monkeypatch):
    # given
    class Unwrapped(Reply):
        def read(self, size: int = -1) -> bytes:
            self.watch.expire()
            raise ValueError('Read on closed or unwrapped SSL socket.')

    def opener(request, timeout, watch):
        reply = Unwrapped(b'', [])
        reply.watch = watch
        return reply

    monkeypatch.setattr(oidc, 'open_url', opener)

    # when
    caught = reason(lambda: get_json('https://idp.example.com/.well-known/openid-configuration'))

    # then
    assert caught == 'unreachable'


def test_a_value_error_before_the_deadline_stays_a_value_error(monkeypatch):
    # given
    class Broken(Reply):
        def read(self, size: int = -1) -> bytes:
            raise ValueError('bad chunk')

    def opener(request, timeout, watch):
        return Broken(b'', [])

    monkeypatch.setattr(oidc, 'open_url', opener)

    # when
    with pytest.raises(ValueError) as caught:
        get_json('https://idp.example.com/.well-known/openid-configuration')

    # then
    assert str(caught.value) == 'bad chunk'


def connect_proxy(stop: threading.Event, drip_reply: bool, seen: list[bytes]) -> tuple[socket.socket, threading.Thread]:
    def handle(client: socket.socket) -> None:
        head = b''
        while b'\r\n\r\n' not in head:
            part = client.recv(4096)
            if not part:
                return
            head += part
        seen.append(head.split(b'\r\n', 1)[0])
        target = head.split(b' ', 2)[1].decode()
        if drip_reply:
            drip(client, stop, b'HTTP/1.1 200 ', 20)
            return
        host, port = target.rsplit(':', 1)
        with socket.create_connection((host, int(port)), 5) as upstream:
            client.sendall(b'HTTP/1.1 200 Connection established\r\n\r\n')

            def pump(source: socket.socket, sink: socket.socket) -> None:
                try:
                    while part := source.recv(65536):
                        sink.sendall(part)
                except OSError:
                    pass
                try:
                    sink.shutdown(socket.SHUT_WR)
                except OSError:
                    pass

            back = threading.Thread(target=pump, args=(upstream, client), daemon=True)
            back.start()
            pump(client, upstream)
            back.join(5)

    return one_shot_server(handle)


def test_https_through_a_connect_proxy_works_and_a_dripping_proxy_reply_stops_at_the_deadline(monkeypatch, tmp_path):
    # given
    cert, key = self_signed(tmp_path)
    monkeypatch.setattr(ssl, '_create_default_https_context', lambda: ssl.create_default_context(cafile=cert))
    for name in ('no_proxy', 'NO_PROXY', 'http_proxy', 'HTTP_PROXY', 'all_proxy', 'ALL_PROXY'):
        monkeypatch.delenv(name, raising=False)
    serving = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    serving.load_cert_chain(cert, key)

    def answer(conn: socket.socket) -> None:
        with serving.wrap_socket(conn, server_side=True) as tls:
            tls.recv(4096)
            body = b'{"issuer": "https://127.0.0.1", "via": "tunnel"}'
            tls.sendall(b'HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: %d\r\nConnection: close\r\n\r\n%s' % (len(body), body))

    target, target_worker = one_shot_server(answer)
    stop = threading.Event()
    seen: list[bytes] = []
    proxy, proxy_worker = connect_proxy(stop, drip_reply=False, seen=seen)
    url = f'https://127.0.0.1:{target.getsockname()[1]}/.well-known/openid-configuration'
    monkeypatch.setenv('https_proxy', f'http://127.0.0.1:{proxy.getsockname()[1]}')
    monkeypatch.setenv('HTTPS_PROXY', f'http://127.0.0.1:{proxy.getsockname()[1]}')

    # when
    tunnelled = get_json(url)
    proxy_worker.join(5)
    target_worker.join(5)
    proxy.close()
    target.close()
    monkeypatch.setattr(oidc, 'FETCH_TOTAL_S', 1.5)
    monkeypatch.setattr(oidc, 'FETCH_TIMEOUT_S', 20)
    slow_proxy, slow_worker = connect_proxy(stop, drip_reply=True, seen=seen)
    monkeypatch.setenv('https_proxy', f'http://127.0.0.1:{slow_proxy.getsockname()[1]}')
    monkeypatch.setenv('HTTPS_PROXY', f'http://127.0.0.1:{slow_proxy.getsockname()[1]}')
    caught, took = timed_reason('https://127.0.0.1:9/.well-known/openid-configuration')
    stop.set()
    slow_worker.join(5)
    slow_proxy.close()

    # then
    assert tunnelled == {'issuer': 'https://127.0.0.1', 'via': 'tunnel'}
    assert seen == [b'CONNECT 127.0.0.1:%d HTTP/1.1' % int(url.split(':')[2].split('/')[0]), b'CONNECT 127.0.0.1:9 HTTP/1.1']
    assert caught == 'unreachable' and took <= 1.5 + 1


def test_a_redirect_on_discovery_or_keys_is_a_failure_that_is_never_followed():
    # given
    now = [1000.0]
    elsewhere = Listener()
    moved = (302, {'Location': elsewhere.url + '/elsewhere'}, b'{}')
    server = scripted_server([])
    issuer = server_url(server)
    good = {**FakeIdp().discovery, 'issuer': issuer, 'authorization_endpoint': issuer + '/authorize', 'token_endpoint': issuer + '/token', 'jwks_uri': issuer + '/jwks'}
    server.answers += [(200, {}, json.dumps(good).encode()), moved]
    discovery = Discovery(issuer, clock=lambda: now[0])
    first = discovery.endpoints()

    # when
    now[0] += DISCOVERY_TTL_S + 1
    stale = discovery.endpoints()
    failing = discovery.status()
    server.answers += [moved, moved, (301, {'Location': elsewhere.url}, b'{}'), (307, {'Location': elsewhere.url}, b'{}'), (308, {'Location': elsewhere.url}, b'{}')]
    fresh = reason(Discovery(issuer + '/moved').endpoints)
    keys = reason(lambda: SigningKeys(issuer + '/jwks').key_for('k1'))
    codes = []
    for _ in range(3):
        with pytest.raises(urllib.error.HTTPError) as caught:
            get_json(issuer + '/jwks')
        codes.append(caught.value.code)
    asked = len(server.seen)
    stop_server(server)
    elsewhere.close()

    # then
    assert stale == first and (failing['ok'], failing['error']) == (False, 'discovery')
    assert fresh == 'discovery' and keys == 'unreachable'
    assert codes == [301, 307, 308]
    assert asked == 7, 'each fetch reached the first server once'
    assert elsewhere.accepted == 0, 'no redirect is followed'
