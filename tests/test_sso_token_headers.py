import dataclasses
import json
import logging

import pytest
from fastapi.testclient import TestClient

from skopax import oidc
from skopax.app import create_app
from skopax.config import ConfigurationError, TOKEN_HEADERS_MAX, extra_headers_of, load_settings
from skopax.messages import msg
from skopax.sso import FIELDS, SsoConfig, SsoHttp
from skopax.store import Store
from skopax.tunables import apply
from tests.conftest import AUTH
from tests.sso_kit import SECRET, FakeIdp, app_client, idp_settings, log_in, sign_on

SSO = '/api/admin/sso'
FIELD = 'extra-token-headers'
ENV = {'MONGO_URI': 'mongodb://unused', 'SKOPAX_TOKEN': 't' * 32}


def save(client, value):
    return client.put(SSO, json={'values': {FIELD: value}}, headers=AUTH)


@pytest.mark.parametrize('value, reason', [
    ('X-Channel', 'pair'),
    ('X-Channel:', 'pair'),
    ('X Channel: web', 'name'),
    ('X-Channel\u00e9: web', 'name'),
    ('X-Channel: we\r\nb', 'value'),
    ('X-Channel: we\nX-Evil: 1', 'value'),
    ('X-Channel: we\x00b', 'value'),
    ('X-Channel: caf\u20ac', 'value'),
    ('Authorization: Bearer x', 'reserved'),
    ('authorization: Bearer x', 'reserved'),
    ('Host: idp.example.com', 'reserved'),
    ('Content-Length: 3', 'reserved'),
    ('Cookie: a=b', 'reserved'),
    ('Proxy-Authorization: x', 'reserved'),
    ('Content-Type: text/plain', 'reserved'),
    ('Accept: text/html', 'reserved'),
    ('Transfer-Encoding: chunked', 'reserved'),
    ('Connection: close', 'reserved'),
    ('Accept-Encoding: gzip', 'reserved'),
    ('Expect: 100-continue', 'reserved'),
    ('TE: trailers', 'reserved'),
    ('Upgrade: h2c', 'reserved'),
    ('Keep-Alive: timeout=5', 'reserved'),
    ('Trailer: X-A', 'reserved'),
    ('Forwarded: for=192.0.2.1', 'reserved'),
    ('Origin: https://example.com', 'reserved'),
    ('X-Forwarded-For: 192.0.2.1', 'reserved'),
    ('x-forwarded-host: example.com', 'reserved'),
    ('X-Forwarded-Anything: 1', 'reserved'),
    ('X-Channel: we\x85b', 'value'),
    ('X-Channel: we\x9fb', 'value'),
    ('X-A: 1; x-a: 2', 'repeat'),
    ('; '.join(f'X-H{i}: {i}' for i in range(TOKEN_HEADERS_MAX + 1)), 'count'),
])
def test_a_wrong_extra_header_is_refused_on_save_and_stops_the_boot(store: Store, value, reason):
    # given
    idp = FakeIdp()
    client = app_client(store, idp)

    # when
    res = save(client, value)
    with pytest.raises(ConfigurationError) as boot:
        load_settings(env={**ENV, 'SSO_EXTRA_TOKEN_HEADERS': value})

    # then
    wanted = msg(f'sso.token_headers.{reason}', limit=TOKEN_HEADERS_MAX)
    assert (res.status_code, res.json()) == (422, {'detail': msg('sso.token_headers_invalid', reason=wanted), 'field': FIELD})
    assert str(boot.value) == msg('config.sso_token_headers_invalid', reason=wanted)
    assert store.db['tunable_changes'].count_documents({}) == 0


def test_good_extra_headers_are_saved_shown_and_parsed():
    # given
    raw = ' X-Channel: web ; X-Api-Version:2024-01  ;; X-Trace:a=b,c '
    ten = '; '.join(f'X-H{i}: {i}' for i in range(TOKEN_HEADERS_MAX))

    # when
    parsed = extra_headers_of(raw)
    full = extra_headers_of(ten)
    loaded = load_settings(env={**ENV, 'SSO_EXTRA_TOKEN_HEADERS': raw})

    # then
    assert parsed == {'X-Channel': 'web', 'X-Api-Version': '2024-01', 'X-Trace': 'a=b,c'}
    assert len(full) == TOKEN_HEADERS_MAX and extra_headers_of('') == {}
    assert loaded.extra_token_headers == parsed


def test_the_extra_headers_go_only_to_the_token_endpoint_and_replace_a_same_named_environment_header(store: Store, monkeypatch):
    # given
    idp = FakeIdp()
    fetched = []

    def fetch_body(request):
        fetched.append((request.full_url, {k.lower() for k in request.headers}))
        return json.dumps(idp.fetch(request.full_url)).encode()

    monkeypatch.setattr(oidc, 'fetch_body', fetch_body)
    s = idp_settings(idp, sso_token_headers='X-Gateway: env-gateway; X-Channel: env')
    client = TestClient(create_app(s, store, sso_http=SsoHttp(exchange=idp.exchange)), base_url='https://testserver')

    # when
    saved = save(client, 'X-Channel: web; X-Api-Version: 2')
    shown = client.get(SSO, headers=AUTH).json()
    signed = log_in(client, idp)

    # then
    assert saved.status_code == 200 and FIELD in FIELDS
    assert shown['values'][FIELD]['value'] == 'X-Channel: web; X-Api-Version: 2'
    assert signed.status_code == 303
    sent = idp.exchanges[-1]['headers']
    assert sent == {'X-Gateway': 'env-gateway', 'X-Channel': 'web', 'X-Api-Version': '2'}
    urls = {url for url, _ in fetched}
    assert idp.discovery['jwks_uri'] in urls and any(url.endswith('/.well-known/openid-configuration') for url in urls)
    assert all(names == {'accept'} for _, names in fetched)


def test_the_token_request_hook_sees_the_extra_headers_and_the_fingerprint_follows_them():
    # given
    seen = []
    idp = FakeIdp()
    s = idp_settings(idp, sso_extra_token_headers='X-Channel: web')
    signon = sign_on(s, lambda request: seen.append(request) or {}, secret=SECRET)
    key = b'k' * 32

    # when
    config = SsoConfig.of(s, SECRET)
    other = SsoConfig.of(dataclasses.replace(s, sso_extra_token_headers='X-Channel: app'), SECRET)
    _, headers = signon._request(config, signon.provider(config).endpoints(), 'code', {'redirect': 'https://skopax.example.com/authorized', 'verifier': 'v'})

    # then
    assert seen[0]['headers'] == {'X-Channel': 'web'}
    assert headers == {'X-Channel': 'web'}
    assert config.fingerprint(key) != other.fingerprint(key)


def test_a_stored_value_that_no_longer_passes_is_dropped_with_a_warning_that_names_the_reason_only(caplog):
    # given
    base = dataclasses.replace(idp_settings(FakeIdp()), sso_extra_token_headers='X-Channel: env')
    stored = 'X-Channel: web; Expect: planted-value-9f3'

    # when
    with caplog.at_level(logging.WARNING):
        applied = apply(base, {'sso.extra-token-headers': stored})
        kept = apply(base, {'sso.extra-token-headers': 'X-Channel: web'})

    # then
    assert applied.sso_extra_token_headers == 'X-Channel: env' and applied.extra_token_headers == {'X-Channel': 'env'}
    assert kept.extra_token_headers == {'X-Channel': 'web'}
    said = [r.getMessage() for r in caplog.records if 'sso.extra-token-headers' in r.getMessage()]
    assert any(msg('sso.token_headers.reserved') in line for line in said)
    assert 'planted-value-9f3' not in caplog.text
