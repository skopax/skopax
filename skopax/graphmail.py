import base64
import hashlib
import http
import json
import logging
import re
import threading
import time
import urllib.parse
import uuid
from collections.abc import Callable, Mapping
from email.message import EmailMessage

import jwt
from cryptography import x509
from cryptography.exceptions import UnsupportedAlgorithm
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from skopax import mail, remote
from skopax.messages import msg

log = logging.getLogger('skopax.mail')

METHOD = 'graph'
ASSERTION_TYPE = 'urn:ietf:params:oauth:client-assertion-type:jwt-bearer'
ASSERTION_S = 300
SKEW_S = 60
TOKEN_MARGIN_S = 300
TOKEN_MIN_S = 60
RETRY_SHORT_S = 3
RETRY_DEFAULT_S = 30
RETRY_MAX_S = 3600
ANSWER_READ = 65536
ACCEPTED = 202
KEY_LABELS = ('PRIVATE KEY', 'RSA PRIVATE KEY')
PEM_BLOCK = re.compile(r'-----BEGIN ([A-Z0-9 ]{1,40})-----(.*?)-----END \1-----', re.S)
GUID = re.compile(r'[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}')
LABEL = r'[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?'
TENANT = re.compile(rf'{GUID.pattern}|{LABEL}(?:\.{LABEL})+')
CODE = re.compile(r'[A-Za-z0-9_.-]{1,60}')
RECIPIENT_CODES = frozenset({'ErrorInvalidRecipients', 'ErrorRecipientNotFound', 'ErrorMailRecipientNotFound'})
REFUSING = ('invalid_client', 'unauthorized_client', 'invalid_request', 'invalid_grant', 'invalid_scope', 'unsupported_grant_type')

TRANSPORT = remote.sender(follow=False)
TOKENS: dict[str, tuple[str, float]] = {}
TOKENS_GUARD = threading.Lock()
sleep: Callable[[float], None] = time.sleep
clock: Callable[[], float] = time.time


def forget() -> None:
    with TOKENS_GUARD:
        TOKENS.clear()


def tenant_problem(value: str) -> str | None:
    text = str(value or '').strip()
    return None if len(text) <= 253 and TENANT.fullmatch(text) else msg('mailserver.graph_tenant')


def client_problem(value: str) -> str | None:
    return None if GUID.fullmatch(str(value or '').strip()) else msg('mailserver.graph_client')


def https_problem(value: str) -> str | None:
    try:
        parts = urllib.parse.urlsplit(str(value or '').strip())
        parts.port
    except ValueError:
        return msg('mailserver.graph_https')
    clean = parts.scheme == 'https' and bool(parts.hostname) and '@' not in parts.netloc and not parts.query and not parts.fragment
    return None if clean else msg('mailserver.graph_https')


def blocks(text: str) -> list[tuple[str, bytes]]:
    out = []
    for label, body in PEM_BLOCK.findall(str(text or '')):
        try:
            out.append((label, base64.b64decode(''.join(body.split()), validate=True)))
        except ValueError:
            return []
    return out


def credential(text: str) -> tuple[rsa.RSAPrivateKey, x509.Certificate] | None:
    found = blocks(text)
    keys = [der for label, der in found if label in KEY_LABELS]
    certs = [der for label, der in found if label == 'CERTIFICATE']
    if len(keys) != 1 or not certs:
        return None
    try:
        key = serialization.load_der_private_key(keys[0], password=None)
        cert = x509.load_der_x509_certificate(certs[0])
        public = cert.public_key()
    except (ValueError, TypeError, UnsupportedAlgorithm):
        return None
    if not isinstance(key, rsa.RSAPrivateKey) or not isinstance(public, rsa.RSAPublicKey) or public.public_numbers() != key.public_key().public_numbers():
        return None
    return key, cert


def credential_problem(value: str) -> str | None:
    return None if credential(value) is not None else msg('mailserver.graph_cert')


def thumbprint(cert: x509.Certificate, algorithm: hashes.HashAlgorithm) -> str:
    return base64.urlsafe_b64encode(cert.fingerprint(algorithm)).rstrip(b'=').decode('ascii')


def assertion(client: str, token_url: str, pem: str, now: float) -> str:
    found = credential(pem)
    if found is None:
        raise mail.MailRefused(msg('mailserver.graph_cert'))
    key, cert = found
    claims = {'aud': token_url, 'iss': client, 'sub': client, 'jti': str(uuid.uuid4()), 'nbf': int(now) - SKEW_S, 'iat': int(now), 'exp': int(now) + ASSERTION_S}
    return jwt.encode(claims, key, algorithm='PS256', headers={'typ': 'JWT', 'x5t#S256': thumbprint(cert, hashes.SHA256())})


def code_of(error: remote.HttpError, *path: str) -> str:
    try:
        found = json.loads(error.body or 'null')
        for step in path:
            found = found.get(step) if isinstance(found, dict) else None
    except ValueError:
        return ''
    return found if isinstance(found, str) and CODE.fullmatch(found) else ''


def aadsts_of(error: remote.HttpError) -> str:
    try:
        codes = json.loads(error.body or 'null').get('error_codes')
    except (ValueError, AttributeError):
        return ''
    first = codes[0] if isinstance(codes, list) and codes else None
    return f'AADSTS{first}' if isinstance(first, int) and not isinstance(first, bool) and 0 < first < 10**9 else ''


def waited(headers: Mapping[str, str]) -> int:
    try:
        wait = int(str(headers.get('retry-after', '')).strip())
    except ValueError:
        return RETRY_DEFAULT_S
    return min(max(wait, 0), RETRY_MAX_S)


def reason_of(error: BaseException, timeout: float) -> str:
    if isinstance(error, remote.TimedOut):
        return f'no answer in {timeout:g} s'
    if isinstance(error, remote.TooLarge):
        return 'the answer was too large'
    if isinstance(error, remote.NotJson):
        return 'the answer was not JSON'
    return 'unreachable'


def answer_of(status: int | None) -> tuple[int, bytes]:
    code = status if isinstance(status, int) and not isinstance(status, bool) and 200 <= status < 300 else ACCEPTED
    try:
        phrase = http.HTTPStatus(code).phrase
    except ValueError:
        phrase = ''
    return code, phrase.encode('ascii')


def roles_of(token: str) -> list[str]:
    try:
        claims = jwt.decode(token, options={'verify_signature': False})
    except jwt.PyJWTError:
        return []
    roles = claims.get('roles') if isinstance(claims, dict) else None
    return [r for r in roles if isinstance(r, str)] if isinstance(roles, list) else []


class Graph:
    def __init__(self, config: Mapping[str, str], timeout: float, transport: remote.Transport | None = None):
        self._config = config
        self.timeout = timeout
        self._transport = transport

    def __repr__(self) -> str:
        return f'Graph({self.sender})'

    def _get(self, key: str) -> str:
        return str(self._config.get(key) or '').strip()

    @property
    def tenant(self) -> str:
        return self._get('tenant-id')

    @property
    def client(self) -> str:
        return self._get('client-id')

    @property
    def sender(self) -> str:
        return (mail.named(self._config.get('sender', '')) or ('', ''))[1]

    @property
    def authority(self) -> str:
        return (self._get('authority') or mail.GRAPH_AUTHORITY).rstrip('/')

    @property
    def graph_url(self) -> str:
        return (self._get('graph-url') or mail.GRAPH_URL).rstrip('/')

    @property
    def token_url(self) -> str:
        return f"{self.authority}/{urllib.parse.quote(self.tenant, safe='')}/oauth2/v2.0/token"

    @property
    def send_url(self) -> str:
        return f"{self.graph_url}/v1.0/users/{urllib.parse.quote(self.sender, safe='@')}/sendMail"

    def _secret(self) -> tuple[str, str]:
        cert = str(self._config.get('client-cert') or '')
        if cert.strip():
            return 'cert', cert
        return 'secret', str(self._config.get('client-secret') or '')

    def problem(self) -> str | None:
        if tenant_problem(self.tenant) is not None or client_problem(self.client) is not None or not self.sender:
            return msg('mailserver.graph_not_ready')
        if https_problem(self.authority) is not None or https_problem(self.graph_url) is not None:
            return msg('mailserver.graph_not_ready')
        how, secret = self._secret()
        if not secret.strip() or (how == 'cert' and credential(secret) is None):
            return msg('mailserver.graph_not_ready')
        return None

    def _key(self) -> str:
        how, secret = self._secret()
        joined = '\0'.join((self.authority, self.tenant, self.client, self.graph_url, how, secret))
        return hashlib.sha256(joined.encode('utf-8', 'replace')).hexdigest()

    def _send(self, method: str, url: str, headers: dict[str, str], data: bytes) -> remote.Reply:
        transport = self._transport or TRANSPORT
        return transport(method, url, headers, data, self.timeout)

    def _fresh(self) -> tuple[str, float]:
        how, secret = self._secret()
        now = clock()
        form = {'client_id': self.client, 'scope': f'{self.graph_url}/.default', 'grant_type': 'client_credentials'}
        if how == 'cert':
            form |= {'client_assertion_type': ASSERTION_TYPE, 'client_assertion': assertion(self.client, self.token_url, secret, now)}
        else:
            form['client_secret'] = secret
        host = urllib.parse.urlsplit(self.authority).hostname or self.authority
        headers = {'Accept': 'application/json', 'Content-Type': 'application/x-www-form-urlencoded'}
        try:
            reply = self._send('POST', self.token_url, headers, urllib.parse.urlencode(form).encode('ascii'))
        except remote.HttpError as e:
            error = code_of(e, 'error')
            if e.code < 500 and e.code != 429 and (error in REFUSING or not error):
                reason = ' '.join(part for part in (error or f'HTTP {e.code}', aadsts_of(e)) if part)
                raise self._logged(mail.MailRefused(msg('mailserver.graph_token', reason=reason))) from None
            raise self._logged(mail.MailDown(msg('mailserver.graph_token_down', host=host, reason=error or f'HTTP {e.code}'))) from None
        except remote.Unreachable as e:
            raise self._logged(mail.MailDown(msg('mailserver.graph_token_down', host=host, reason=reason_of(e, self.timeout)))) from None
        body = reply.body if isinstance(reply.body, dict) else {}
        token, life = body.get('access_token'), body.get('expires_in')
        try:
            life = float(life)
        except (TypeError, ValueError):
            life = 0.0
        if not isinstance(token, str) or not token or life <= 0:
            raise self._logged(mail.MailDown(msg('mailserver.graph_token_down', host=host, reason='the answer held no token')))
        return token, now + max(life - TOKEN_MARGIN_S, TOKEN_MIN_S)

    def token(self, fresh: bool = False) -> str:
        key = self._key()
        with TOKENS_GUARD:
            held = TOKENS.get(key)
        if held is not None and not fresh and held[1] > clock():
            return held[0]
        token, until = self._fresh()
        with TOKENS_GUARD:
            now = clock()
            for old in [k for k, (_, ends) in TOKENS.items() if ends <= now]:
                del TOKENS[old]
            TOKENS[key] = (token, until)
        return token

    def _drop(self) -> None:
        with TOKENS_GUARD:
            TOKENS.pop(self._key(), None)

    def _logged(self, error: Exception) -> Exception:
        log.info('%s', error.text)
        return error

    def probe(self) -> str:
        problem = self.problem()
        if problem is not None:
            raise mail.MailRefused(problem)
        token = self.token(fresh=True)
        key = 'mailserver.graph_ok_wide' if any(role.lower() in ('mail.send', 'mail.send.shared') for role in roles_of(token)) else 'mailserver.graph_ok'
        return msg(key, client=self.client, sender=self.sender)

    def _mapped(self, e: remote.HttpError) -> Exception:
        host = urllib.parse.urlsplit(self.graph_url).hostname or self.graph_url
        code = code_of(e, 'error', 'code')
        shown = ' '.join(part for part in (f'HTTP {e.code}', code) if part)
        if e.code == 401:
            self._drop()
            return mail.MailDown(msg('mailserver.graph_credentials'))
        if e.code == 403:
            return mail.MailRefused(msg('mailserver.graph_scope', sender=self.sender))
        if e.code == 404:
            return mail.MailRefused(msg('mailserver.graph_mailbox', sender=self.sender, code=shown))
        if e.code == 400 and code in RECIPIENT_CODES:
            return mail.MailRefused(msg('mailserver.graph_recipient', code=shown), recipient=True)
        if e.code in (429, 503) and 'retry-after' in e.headers:
            return mail.MailDown(msg('mailserver.graph_busy', wait=waited(e.headers)))
        if e.code >= 500 or e.code in (408, 429):
            return mail.MailDown(msg('mailserver.graph_down', host=host, reason=shown))
        return mail.MailRefused(msg('mailserver.graph_refused', code=shown))

    def send(self, message: EmailMessage) -> tuple[int, bytes]:
        problem = self.problem()
        if problem is not None:
            raise mail.MailRefused(problem)
        data = base64.b64encode(message.as_bytes())
        host = urllib.parse.urlsplit(self.graph_url).hostname or self.graph_url
        for attempt in (1, 2):
            headers = {'Accept': 'application/json', 'Content-Type': 'text/plain', 'Authorization': f'Bearer {self.token()}', remote.TEXT_READ: str(ANSWER_READ)}
            try:
                reply = self._send('POST', self.send_url, headers, data)
                return answer_of(reply.status)
            except remote.HttpError as e:
                wait = waited(e.headers)
                if attempt == 1 and e.code in (429, 503) and 'retry-after' in e.headers and wait <= min(RETRY_SHORT_S, self.timeout / 2):
                    sleep(wait)
                    continue
                raise self._logged(self._mapped(e)) from None
            except remote.Unreachable as e:
                raise self._logged(mail.MailDown(msg('mailserver.graph_down', host=host, reason=reason_of(e, self.timeout)))) from None
