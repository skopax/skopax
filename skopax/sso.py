import base64
import dataclasses
import hashlib
import hmac
import http.client
import json
import logging
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Callable, Mapping

from skopax import oidc
from skopax.adminsecret import REFRESH_S, AdminSecret
from skopax.auth import pkce_pair, safe_next
from skopax.errkind import kind as error_kind
from skopax.messages import msg
from skopax.oidc import Endpoints, OidcError, Provider, url_problem, verify
from skopax.sealer import Sealer

log = logging.getLogger('skopax.sso')

Exchange = Callable[[str, dict[str, str], dict[str, str]], dict]

REDIRECT_PATH = '/authorized'
HIDDEN_PARAMS = ('client_secret', 'code', 'code_verifier')
RESERVED_PARAMS = ('grant_type', 'code', 'redirect_uri', 'client_id', 'client_secret', 'code_verifier')
RESERVED_HEADERS = ('authorization',)
SECRET_ID = 'sso.client-secret'
SECRET_MAX = 2000
FIELDS: tuple[str, ...] = ('issuer', 'client-id', 'client-secret', 'scope', 'claims', 'client-auth', 'require-verified-email', 'authorize-url', 'token-url', 'jwks-url',
                           'roles-claim', 'role-map', 'default-role')
SECRET_FIELD = 'client-secret'
VALUE_MAX = 4000
TEST_TIMEOUT_S = 15
CHECKS = ('discovery', 'issuer', 'pkce', 'keys', 'client-auth', 'redirect', 'scope')


class SourceOwned(Exception):
    def __init__(self, source: str):
        super().__init__(msg('sso.secret_source_owned', source=source))
        self.source = source


class TestBusy(Exception):
    pass


class InvalidSecret(ValueError):
    def __init__(self):
        super().__init__(msg('sso.secret_invalid'))


def post_form(url: str, data: dict[str, str], headers: dict[str, str]) -> dict:
    request = urllib.request.Request(url, data=urllib.parse.urlencode(data).encode(), method='POST',
                                     headers={'Accept': 'application/json', 'Content-Type': 'application/x-www-form-urlencoded', **headers})
    try:
        body = oidc.fetch_body(request)
    except OidcError:
        raise
    except urllib.error.HTTPError as e:
        raise OidcError('exchange', f'status {e.code}') from None
    except (OSError, http.client.HTTPException) as e:
        raise OidcError('unreachable', error_kind(e)) from None
    if len(body) > oidc.MAX_BODY:
        raise OidcError('exchange', 'the response is larger than 1 MiB')
    try:
        doc = json.loads(body)
    except (ValueError, RecursionError):
        raise OidcError('exchange', 'the response is not JSON') from None
    if not isinstance(doc, dict):
        raise OidcError('exchange', 'the response is not a JSON object')
    return doc


@dataclass(frozen=True)
class SsoHttp:
    fetch: Callable[[str], dict] = field(default_factory=lambda: oidc.get_json)
    exchange: Exchange = field(default_factory=lambda: post_form)


def redirect_problem(uri: str) -> bool:
    if not uri or url_problem(uri) or '?' in uri:
        return True
    return urllib.parse.urlsplit(uri).path != REDIRECT_PATH


@dataclass(frozen=True)
class SsoConfig:
    on: bool
    issuer: str
    client_id: str
    secret: str = field(repr=False)
    scope: str
    claims: str
    client_auth: str
    require_verified: bool
    overrides: Mapping[str, str]
    redirect_uri: str
    token_headers: Mapping[str, str]
    secret_unreadable: bool = False

    @classmethod
    def of(cls, settings, secret: str, unreadable: bool = False) -> 'SsoConfig':
        base = (settings.base_url or '').strip().rstrip('/')
        redirect = (settings.sso_redirect_uri or '').strip() or (base + REDIRECT_PATH if base else '')
        given = {'authorize': settings.sso_authorize_url, 'token': settings.sso_token_url, 'jwks': settings.sso_jwks_url}
        return cls(on=bool(settings.sso_on), issuer=(settings.sso_issuer or '').strip(), client_id=(settings.sso_client_id or '').strip(), secret=secret or '',
                   scope=(settings.sso_scope or '').strip(), claims=settings.sso_claims, client_auth=settings.sso_client_auth,
                   require_verified=bool(settings.sso_require_verified_email), overrides={k: v.strip() for k, v in given.items() if v and v.strip()},
                   redirect_uri=redirect, token_headers=dict(settings.token_headers), secret_unreadable=bool(unreadable and not secret))

    def problem(self) -> str | None:
        if not self.issuer or url_problem(self.issuer):
            return msg('sso.problem.issuer', field='sso.issuer')
        if not self.client_id:
            return msg('sso.problem.client_id', field='sso.client-id')
        if not self.secret:
            return msg('sso.problem.secret_unreadable') if self.secret_unreadable else msg('sso.problem.secret', field='sso.client-secret')
        if redirect_problem(self.redirect_uri):
            return msg('sso.problem.redirect', field='sso.redirect-uri')
        if self.claims != 'access-token' and 'openid' not in self.scope.split():
            return msg('sso.problem.scope', field='sso.scope')
        for name, url in self.overrides.items():
            if url_problem(url):
                return msg('sso.problem.endpoint', field=f'sso.{name}-url')
        return None

    @property
    def active(self) -> bool:
        return self.on and self.problem() is None

    def fingerprint(self, key: bytes) -> str:
        values = {f.name: getattr(self, f.name) for f in dataclasses.fields(self)}
        values |= {'secret': digest(self.secret), 'overrides': sorted(self.overrides.items()),
                   'token_headers': sorted((name, digest(value)) for name, value in self.token_headers.items())}
        return hmac.new(key, json.dumps(values, sort_keys=True).encode(), hashlib.sha256).hexdigest()[:16]


def digest(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def basic_auth(client_id: str, secret: str) -> str:
    pair = urllib.parse.quote_plus(client_id) + ':' + urllib.parse.quote_plus(secret)
    return 'Basic ' + base64.b64encode(pair.encode()).decode()


def auth_method(config: SsoConfig, ends: Endpoints) -> str:
    if config.client_auth in ('post', 'basic'):
        return config.client_auth
    if 'client_secret_post' in ends.auth_methods or 'client_secret_basic' not in ends.auth_methods:
        return 'post'
    return 'basic'


class SignOn:
    def __init__(self, config: Callable[[], SsoConfig], http: SsoHttp | None = None, token_request: Callable[[dict], dict] = lambda r: {},
                 *, fp_key: Callable[[], bytes], clock: Callable[[], float] = time.monotonic):
        self.config = config
        self.fp_key = fp_key
        self.http = http or SsoHttp()
        self.token_request = token_request
        self.clock = clock
        self.lock = threading.Lock()
        self.cached: tuple[tuple, Provider] | None = None
        self.warned = False
        self.testing = threading.Lock()

    def active(self) -> bool:
        return self.config().active

    def provider(self, config: SsoConfig | None = None) -> Provider:
        config = config or self.config()
        key = (config.on, config.issuer, tuple(sorted(config.overrides.items())))
        with self.lock:
            if self.cached is None or self.cached[0] != key:
                self.cached = (key, Provider(config.issuer, config.overrides, self.http.fetch, self.clock))
            return self.cached[1]

    def status(self) -> dict:
        return self.provider().status()

    def test(self, config: SsoConfig) -> dict:
        if not self.testing.acquire(blocking=False):
            raise TestBusy()
        started = time.monotonic()
        done: list[dict] = []
        guard = threading.Lock()

        def note(check_id: str, ok: bool, key: str, **values) -> None:
            with guard:
                done.append({'id': check_id, 'ok': ok, 'text': msg(key, **values)})

        def run() -> None:
            try:
                self._checks(config, note)
            except Exception as e:
                log.warning('the connection test stopped: %s', error_kind(e))
            finally:
                self.testing.release()

        cap = TEST_TIMEOUT_S
        worker = threading.Thread(target=run, name='skopax-sso-test', daemon=True)
        try:
            worker.start()
        except BaseException:
            self.testing.release()
            raise
        worker.join(cap)
        late = worker.is_alive()
        with guard:
            found = {c['id']: c for c in done}
        checks, missed = [], False
        for check_id in CHECKS:
            if check_id in found:
                checks.append(found[check_id])
                continue
            text = msg('sso.test.skipped') if missed else msg('sso.test.timeout', seconds=f'{cap:g}') if late else msg('sso.test.stopped')
            checks.append({'id': check_id, 'ok': False, 'text': text})
            missed = True
        failed = sum(not c['ok'] for c in checks)
        return {'ok': failed == 0, 'summary': msg('sso.test.ok') if failed == 0 else msg('sso.test.failed', count=failed), 'ms': int((time.monotonic() - started) * 1000),
                'checks': checks}

    def _checks(self, config: SsoConfig, note: Callable[..., None]) -> None:
        if redirect_problem(config.redirect_uri):
            note('redirect', False, 'sso.problem.redirect', field='sso.redirect-uri')
        else:
            note('redirect', True, 'sso.test.redirect_ok', uri=config.redirect_uri)
        if config.claims != 'access-token' and 'openid' not in config.scope.split():
            note('scope', False, 'sso.problem.scope', field='sso.scope')
        else:
            note('scope', True, 'sso.test.scope_ok')
        ends = self._discover(config, note)
        if ends is None:
            for check_id in ('pkce', 'keys', 'client-auth'):
                note(check_id, False, 'sso.test.skipped')
        else:
            if not ends.discovered or not ends.pkce:
                note('pkce', True, 'sso.test.pkce_unlisted')
            elif 'S256' in ends.pkce:
                note('pkce', True, 'sso.test.pkce_ok')
            else:
                note('pkce', False, 'sso.test.pkce_missing')
            self._check_keys(ends, note)
            self._check_client_auth(config, ends, note)

    def _discover(self, config: SsoConfig, note: Callable[..., None]) -> Endpoints | None:
        discovery = oidc.Discovery(config.issuer, config.overrides)
        if not config.issuer or url_problem(config.issuer):
            note('discovery', False, 'sso.problem.issuer', field='sso.issuer')
            note('issuer', False, 'sso.test.skipped')
            return None
        if len(discovery.overrides) == len(oidc.FIELDS):
            note('discovery', True, 'sso.test.discovery_manual')
            note('issuer', True, 'sso.test.issuer_manual')
            return Endpoints(issuer=config.issuer, discovered=False, **discovery.overrides)
        try:
            doc = self.http.fetch(discovery.url)
        except OidcError as e:
            note('discovery', False, 'sso.test.discovery_unreachable' if e.reason == 'unreachable' else 'sso.test.discovery_invalid')
            note('issuer', False, 'sso.test.skipped')
            return None
        except oidc.FETCH_ERRORS:
            note('discovery', False, 'sso.test.discovery_unreachable')
            note('issuer', False, 'sso.test.skipped')
            return None
        try:
            ends = discovery.read(doc)
        except OidcError as e:
            if e.reason == 'issuer':
                note('discovery', True, 'sso.test.discovery_ok')
                note('issuer', False, 'sso.test.issuer_mismatch')
            else:
                note('discovery', False, 'sso.test.discovery_invalid')
                note('issuer', False, 'sso.test.skipped')
            return None
        note('discovery', True, 'sso.test.discovery_ok')
        note('issuer', True, 'sso.test.issuer_ok')
        return ends

    def _check_keys(self, ends: Endpoints, note: Callable[..., None]) -> None:
        try:
            doc = self.http.fetch(ends.jwks)
        except (OidcError, *oidc.FETCH_ERRORS):
            note('keys', False, 'sso.test.keys_unreachable')
            return
        entries = doc.get('keys') if isinstance(doc, dict) else None
        found = oidc.signing_keys(entries) if isinstance(entries, list) else []
        if found:
            note('keys', True, 'sso.test.keys_ok', count=len(found))
        else:
            note('keys', False, 'sso.test.keys_none')

    def _check_client_auth(self, config: SsoConfig, ends: Endpoints, note: Callable[..., None]) -> None:
        if not config.secret:
            note('client-auth', False, 'sso.test.client_auth_no_secret')
            return
        method = 'client_secret_' + auth_method(config, ends)
        if ends.auth_methods and method not in ends.auth_methods:
            note('client-auth', False, 'sso.test.client_auth_unlisted', method=method)
        else:
            note('client-auth', True, 'sso.test.client_auth_ok', method=method)

    def _key(self) -> bytes:
        key = self.fp_key()
        if not key:
            raise OidcError('unreachable', 'the session key is not available')
        return key

    def start(self, next_page: str) -> tuple[str, dict]:
        config = self.config()
        ends = self.provider(config).endpoints()
        if ends.pkce and 'S256' not in ends.pkce:
            raise OidcError('pkce', 'the identity provider does not offer the S256 code challenge')
        state, nonce = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        verifier, challenge = pkce_pair()
        params = {'response_type': 'code', 'client_id': config.client_id, 'redirect_uri': config.redirect_uri, 'scope': config.scope, 'state': state, 'nonce': nonce,
                  'code_challenge': challenge, 'code_challenge_method': 'S256'}
        sep = '&' if '?' in ends.authorize else '?'
        pending = {'state': state, 'nonce': nonce, 'verifier': verifier, 'next': safe_next(next_page), 'redirect': config.redirect_uri, 'issuer': ends.issuer,
                   'fp': config.fingerprint(self._key())}
        return f'{ends.authorize}{sep}{urllib.parse.urlencode(params)}', pending

    def finish(self, pending: dict, code: str | None, state: str | None, error: str | None) -> dict:
        if error or not code:
            raise OidcError('cancelled')
        expected = pending.get('state')
        if not state or not isinstance(expected, str) or not expected or not oidc.same(state, expected):
            raise OidcError('state', 'the state does not match this login')
        config = self.config()
        if not config.active:
            raise OidcError('changed', 'single sign-on is off')
        if not isinstance(pending.get('fp'), str) or not hmac.compare_digest(pending['fp'], config.fingerprint(self._key())):
            raise OidcError('changed', 'the sign-in settings changed during the login')
        try:
            provider = self.provider(config)
            ends, keys = provider.endpoints(), provider.keys()
        except OidcError as e:
            raise OidcError('unreachable', e.reason) from None
        if pending.get('issuer') != ends.issuer:
            raise OidcError('changed', 'the identity provider issuer changed during the login')
        data, headers = self._request(config, ends, code, pending)
        return self._claims(config, ends, keys, self._exchange(ends.token, data, headers), pending)

    def _request(self, config: SsoConfig, ends: Endpoints, code: str, pending: dict) -> tuple[dict[str, str], dict[str, str]]:
        data = {'grant_type': 'authorization_code', 'code': code, 'redirect_uri': str(pending.get('redirect') or ''), 'client_id': config.client_id,
                'code_verifier': str(pending.get('verifier') or '')}
        shown = {k: v for k, v in config.token_headers.items() if k.lower() not in RESERVED_HEADERS}
        params, extra = self._hook(ends.token, shown, {k: v for k, v in data.items() if k not in HIDDEN_PARAMS})
        method = auth_method(config, ends)
        if method == 'post':
            data['client_secret'] = config.secret
        headers = {**config.token_headers, **extra}
        if method == 'basic':
            headers = {k: v for k, v in headers.items() if k.lower() not in RESERVED_HEADERS} | {'Authorization': basic_auth(config.client_id, config.secret)}
        return {**params, **data}, headers

    def _hook(self, url: str, headers: dict[str, str], params: dict[str, str]) -> tuple[dict[str, str], dict[str, str]]:
        try:
            given = self.token_request({'url': url, 'headers': dict(headers), 'params': dict(params)})
        except Exception as e:
            raise OidcError('hook', type(e).__name__) from None
        given = given if isinstance(given, dict) else {}
        added = {k: str(v) for k, v in (given.get('params') or {}).items() if isinstance(k, str)} if isinstance(given.get('params'), dict) else {}
        extra = {k: str(v) for k, v in (given.get('headers') or {}).items() if isinstance(k, str)} if isinstance(given.get('headers'), dict) else {}
        kept_params = {k: v for k, v in added.items() if k not in RESERVED_PARAMS}
        kept_headers = {k: v for k, v in extra.items() if k.lower() not in RESERVED_HEADERS}
        if (len(kept_params) < len(added) or len(kept_headers) < len(extra)) and not self.warned:
            self.warned = True
            dropped = sorted({*(set(added) - set(kept_params)), *(set(extra) - set(kept_headers))})
            log.warning('the token request hook tried to set %s; those were dropped', ', '.join(dropped))
        return kept_params, kept_headers

    def _exchange(self, url: str, data: dict[str, str], headers: dict[str, str]) -> dict:
        try:
            body = self.http.exchange(url, data, headers)
        except OidcError:
            raise
        except urllib.error.HTTPError as e:
            raise OidcError('exchange', f'status {e.code}') from None
        except (OSError, http.client.HTTPException) as e:
            raise OidcError('unreachable', error_kind(e)) from None
        except (ValueError, TypeError) as e:
            raise OidcError('exchange', error_kind(e)) from None
        if not isinstance(body, dict):
            raise OidcError('exchange', 'the token response is not a JSON object')
        return body

    def _claims(self, config: SsoConfig, ends: Endpoints, keys: oidc.SigningKeys, body: dict, pending: dict) -> dict:
        name = 'access_token' if config.claims == 'access-token' else 'id_token'
        token = body.get(name)
        if not isinstance(token, str) or not token:
            raise OidcError('exchange', f'the token response has no {name}')
        if name == 'access_token':
            return verify(token, keys, issuers=(ends.issuer,), audiences=(config.client_id,))
        nonce = pending.get('nonce')
        if not isinstance(nonce, str) or not nonce:
            raise OidcError('nonce', 'this login has no nonce')
        return verify(token, keys, issuers=(ends.issuer,), audiences=(config.client_id,), nonce=nonce, client_id=config.client_id)


class SsoSecret(AdminSecret):
    def __init__(self, db, sealer: Callable[[], Sealer], source: Callable[[], tuple[str, str | None]], stored_key: Callable[[], str | None], tunables,
                 every: float = REFRESH_S):
        super().__init__(db, SECRET_ID, sealer, stored_key, tunables, 'client secret', lambda: None if self._sourced() else msg('sso.problem.secret_unreadable'), log, every)
        self.source = source

    def _sourced(self) -> tuple[str, str] | None:
        value, label = self.source()
        return (value, label or 'environment') if value else None

    def current(self) -> str:
        sourced = self._sourced()
        if sourced is not None:
            return sourced[0]
        return self.held.value or ''

    def status(self) -> dict:
        held, sourced = self.held, self._sourced()
        if sourced is not None:
            return {'set': True, 'from': sourced[1], 'unreadable': held.unreadable, 'updatedBy': None, 'updatedAt': None}
        return {'set': held.value is not None, 'from': None, 'unreadable': held.unreadable, 'updatedBy': held.by, 'updatedAt': held.at}

    def _owned(self) -> None:
        sourced = self._sourced()
        if sourced is not None:
            raise SourceOwned(sourced[1])

    def set(self, value: str, by: str) -> dict:
        self._owned()
        text = value.strip() if isinstance(value, str) else ''
        if not text or len(text) > SECRET_MAX:
            raise InvalidSecret()
        return super().set(text, by)

    def clear(self, by: str) -> bool:
        self._owned()
        return super().clear(by)
