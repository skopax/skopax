import calendar
import hashlib
import json
import os
import re
import threading
import time
import xml.etree.ElementTree as ElementTree
from collections.abc import Callable, Mapping
from pathlib import Path
from urllib.parse import urlencode, urlsplit

from skopax.contract import HttpError, Transport, Unreachable, send
from skopax.kit import env as env_kit
from skopax.redact import redact_secrets
from skopax.remote import JSON_ACCEPT, PAGE_READ, Reply
from skopax.plugins.cloudwatch.sigv4 import Credentials, signed_headers

NAME = 'CloudWatch'
TIMEOUT = 20
ANSWER_LIMIT = 8 * 1024 * 1024
STS_LIMIT = 64 * 1024
TOKEN_FILE_LIMIT = 16 * 1024
REASON_LIMIT = 200
SESSION_NAME = 'skopax'
SESSION_SECONDS = 3600
REFRESH_BEFORE = 300
CACHE_MAX = 32
STS_VERSION = '2011-06-15'
REGION = re.compile(r'[a-z]{2}(?:-[a-z]+)+-\d{1,2}')
ROLE_ARN = re.compile(r'arn:aws[a-z-]{0,20}:iam::\d{12}:role/[\w+=,.@/-]{1,512}')
ROLE_ENV = 'AWS_ROLE_ARN'
TOKEN_ENV = 'AWS_WEB_IDENTITY_TOKEN_FILE'
SOURCE_KEYS = 'access keys'
SOURCE_WEB = 'web identity (IRSA)'
SOURCE_ROLE = 'assumed role'
CW_TARGET = 'GraniteServiceVersion20100801'
LOGS_TARGET = 'Logs_20140328'
CW_TYPE = 'application/x-amz-json-1.0'
LOGS_TYPE = 'application/x-amz-json-1.1'
FORM_TYPE = 'application/x-www-form-urlencoded; charset=utf-8'
READ_ACTIONS = frozenset({'cloudwatch:DescribeAlarms', 'cloudwatch:DescribeAlarmHistory', 'cloudwatch:GetMetricData', 'logs:StartQuery', 'logs:GetQueryResults',
                          'logs:StopQuery', 'logs:DescribeLogGroups', 'sts:AssumeRole', 'sts:AssumeRoleWithWebIdentity'})
SERVICES = {'cloudwatch': 'monitoring', 'logs': 'logs', 'sts': 'sts'}
DENIED_CODES = ('AccessDenied', 'AccessDeniedException', 'UnauthorizedOperation', 'UnauthorizedAccess', 'NotAuthorized')
UNKNOWN_KEY_CODES = ('InvalidClientTokenId', 'UnrecognizedClientException', 'InvalidAccessKeyId', 'IncompleteSignature')
SIGNATURE_CODES = ('SignatureDoesNotMatch', 'InvalidSignatureException')
EXPIRED_CODES = ('ExpiredToken', 'ExpiredTokenException', 'TokenRefreshRequired')
SKEW_CODES = ('RequestTimeTooSkewed', 'RequestExpired', 'RequestInTheFuture')
SKEW_WORDS = ('signature expired', 'not yet current', 'earlier than', 'later than', 'skew')
CODE = re.compile(r'[A-Za-z][A-Za-z0-9.]{0,80}')
SKEW_TEXT = ("AWS refused the request time: this server's clock is more than 5 minutes off; sync it (NTP)")
EXPIRED_TEXT = 'AWS says the session token or web identity token has expired; give fresh credentials'
UNKNOWN_KEY_TEXT = 'AWS does not know the access key ID or session token; check them, and that the region is enabled for the account'
SIGNATURE_TEXT = 'AWS refused the signature: the secret access key does not match the access key ID'
NO_CREDENTIALS = ('no AWS credentials: give Access key ID and Secret access key, or run on EKS with IRSA so that '
                  f'{ROLE_ENV} and {TOKEN_ENV} are set')


class AwsError(Exception):
    def __init__(self, reason: str, code: int | None = None, kind: str = '', action: str = '', answered: bool = False, name: str = ''):
        super().__init__(reason)
        self.name = name
        self.reason = reason
        self.code = code
        self.kind = kind
        self.action = action
        self.answered = answered


def flat(text: object) -> str:
    return ' '.join(str(text if text is not None else '').split())


def region_problem(value: str) -> str | None:
    return None if REGION.fullmatch(value or '') else 'give an AWS region name such as eu-west-1'


def role_problem(value: str) -> str | None:
    return None if not value or ROLE_ARN.fullmatch(value) else 'give an IAM role ARN such as arn:aws:iam::123456789012:role/skopax-read'


def domain_of(region: str) -> str:
    return 'amazonaws.com.cn' if region.startswith('cn-') else 'amazonaws.com'


def console_of(region: str) -> str:
    host = 'console.amazonaws.cn' if region.startswith('cn-') else 'console.aws.amazon.com'
    return f'https://{region}.{host}/cloudwatch/home?region={region}'


def endpoint(service: str, region: str) -> str:
    return f'https://{SERVICES[service]}.{region}.{domain_of(region)}/'


def seconds(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str) and value:
        try:
            return float(value)
        except ValueError:
            pass
        try:
            return float(calendar.timegm(time.strptime(value.split('.')[0].rstrip('Z'), '%Y-%m-%dT%H:%M:%S')))
        except ValueError:
            return None
    return None


def error_parts(reply_body: object, headers: Mapping[str, str]) -> tuple[str, str]:
    code, said = '', ''
    head = {str(k).lower(): str(v) for k, v in (headers or {}).items()}
    raw = reply_body.decode('utf-8', 'replace') if isinstance(reply_body, bytes) else reply_body if isinstance(reply_body, str) else ''
    text = raw.strip()
    if text.startswith('{'):
        try:
            found = json.loads(text)
        except ValueError:
            found = None
        if isinstance(found, dict):
            code = str(found.get('__type') or found.get('code') or found.get('Code') or '')
            said = str(found.get('message') or found.get('Message') or '')
    elif text.startswith('<'):
        root = xml_root(text.encode('utf-8'))
        if root is not None:
            code = child_text(root, 'Error', 'Code') or child_text(root, 'Code')
            said = child_text(root, 'Error', 'Message') or child_text(root, 'Message')
    query = head.get('x-amzn-query-error', '').split(';')[0]
    code = query or code or head.get('x-amzn-errortype', '')
    code = code.split('#')[-1].split(':')[0].strip()
    return (code if CODE.fullmatch(code) else ''), flat(said)


def xml_root(raw: bytes) -> ElementTree.Element | None:
    if len(raw) > STS_LIMIT or b'<!DOCTYPE' in raw.upper() or b'<!ENTITY' in raw.upper():
        return None
    try:
        root = ElementTree.fromstring(raw)
    except ElementTree.ParseError:
        return None
    for node in root.iter():
        if isinstance(node.tag, str) and '}' in node.tag:
            node.tag = node.tag.split('}', 1)[1]
    return root


def child_text(root: ElementTree.Element, *path: str) -> str:
    node = root.find('.//' + '/'.join(path))
    return flat(node.text) if node is not None and node.text else ''


class Aws:
    def __init__(self, region: str, provider: 'Provider', transport: Transport = send, timeout: float = TIMEOUT, clock: Callable[[], float] = time.time):
        self.region = region
        self.provider = provider
        self.transport = transport
        self.timeout = timeout
        self.clock = clock

    def scrub(self, text: str) -> str:
        shown = text
        for secret in self.provider.hidden():
            if secret:
                shown = shown.replace(secret, '[redacted]')
        return redact_secrets(flat(shown))[:REASON_LIMIT]

    def cloudwatch(self, op: str, body: dict) -> dict:
        return self.json('cloudwatch', op, body, f'{CW_TARGET}.{op}', CW_TYPE)

    def logs(self, op: str, body: dict) -> dict:
        return self.json('logs', op, body, f'{LOGS_TARGET}.{op}', LOGS_TYPE)

    def json(self, service: str, op: str, body: dict, target: str, kind: str) -> dict:
        action = f'{service}:{op}'
        if action not in READ_ACTIONS:
            raise AwsError(f'{action} is not a read action; the CloudWatch plug-in only reads', action=action)
        creds = self.provider.credentials(self)
        url = endpoint(service, self.region)
        data = json.dumps(body).encode()
        headers = signed_headers('POST', url, {'Content-Type': kind, 'X-Amz-Target': target}, data, creds, self.region, SERVICES[service], self.clock())
        reply = self.send(url, headers | {'Accept': JSON_ACCEPT}, data, action, ANSWER_LIMIT)
        try:
            found = json.loads(reply.body or b'{}')
        except ValueError:
            raise AwsError(f'AWS {action}: the answer is not JSON', action=action, answered=True) from None
        if not isinstance(found, dict):
            raise AwsError(f'AWS {action}: the answer is not a JSON object', action=action, answered=True)
        return found

    def sts(self, op: str, params: dict, creds: Credentials | None) -> ElementTree.Element:
        action = f'sts:{op}'
        if action not in READ_ACTIONS:
            raise AwsError(f'{action} is not allowed', action=action)
        url = endpoint('sts', self.region)
        data = urlencode({'Action': op, 'Version': STS_VERSION} | params).encode()
        headers = {'Content-Type': FORM_TYPE}
        if creds is not None:
            headers = signed_headers('POST', url, headers, data, creds, self.region, 'sts', self.clock())
        reply = self.send(url, headers, data, action, STS_LIMIT)
        root = xml_root(reply.body if isinstance(reply.body, bytes) else str(reply.body or '').encode())
        if root is None:
            raise AwsError(f'AWS {action}: the answer is not STS XML', action=action, answered=True)
        return root

    def send(self, url: str, headers: dict[str, str], data: bytes, action: str, limit: int) -> Reply:
        host = urlsplit(url).hostname or ''
        try:
            reply = self.transport('POST', url, headers | {PAGE_READ: str(limit)}, data, self.timeout)
        except HttpError as e:
            raise self.failure(action, e.code, e.body, e.headers) from None
        except Unreachable:
            raise AwsError(f'AWS {action}: no answer from {host}; check Region and the network', action=action, kind='unreachable') from None
        status = reply.status if isinstance(reply.status, int) else 200
        if status != 200:
            raise self.failure(action, status, reply.body, reply.headers)
        return reply

    def failure(self, action: str, status: int | None, body: object, headers: Mapping[str, str] | None) -> AwsError:
        code, said = error_parts(body, headers or {})
        lowered = said.lower()
        if status in (301, 302, 303, 307, 308):
            return AwsError(f'AWS {action}: HTTP {status}, a redirect that is not followed', status, action=action, answered=True)
        if code in DENIED_CODES or (status == 403 and not code and not said):
            return AwsError(f'AWS refused {action}: the credentials lack the {action} permission; add it to the IAM policy', 403, 'denied', action, True)
        if code in SKEW_CODES or (code in SIGNATURE_CODES and any(w in lowered for w in SKEW_WORDS)):
            return AwsError(f'AWS {action}: {SKEW_TEXT}', 401, 'skew', action, True)
        if code in EXPIRED_CODES:
            return AwsError(f'AWS {action}: {EXPIRED_TEXT} ({code})', 401, 'expired', action, True)
        if code in UNKNOWN_KEY_CODES:
            return AwsError(f'AWS {action}: {UNKNOWN_KEY_TEXT} ({code})', 401, 'key', action, True)
        if code in SIGNATURE_CODES:
            return AwsError(f'AWS {action}: {SIGNATURE_TEXT}', 401, 'signature', action, True)
        shown = self.scrub(said)
        named = f' {code}' if code else ''
        return AwsError(f'AWS {action}: HTTP {status}{named}' + (f': {shown}' if shown else ''), status, action=action, answered=True, name=code)


class Cache:
    def __init__(self) -> None:
        self.guard = threading.Lock()
        self.held: dict[str, Credentials] = {}

    def get(self, key: str, now: float) -> Credentials | None:
        with self.guard:
            found = self.held.get(key)
            if found is not None and found.expires is not None and now < found.expires - REFRESH_BEFORE:
                return found
            self.held.pop(key, None)
            return None

    def put(self, key: str, creds: Credentials) -> None:
        with self.guard:
            self.held[key] = creds
            while len(self.held) > CACHE_MAX:
                del self.held[next(iter(self.held))]

    def clear(self) -> None:
        with self.guard:
            self.held.clear()


CACHE = Cache()


def digest(*parts: str) -> str:
    return hashlib.sha256('\x00'.join(parts).encode('utf-8')).hexdigest()


def sts_credentials(root: ElementTree.Element, source: str, action: str) -> Credentials:
    key_id, secret = child_text(root, 'Credentials', 'AccessKeyId'), child_text(root, 'Credentials', 'SecretAccessKey')
    token = ''.join(child_text(root, 'Credentials', 'SessionToken').split())
    expires = seconds(child_text(root, 'Credentials', 'Expiration'))
    if not key_id or not secret or not token or expires is None:
        raise AwsError(f'AWS {action}: the answer carries no credentials', action=action, answered=True)
    return Credentials(key_id, secret, token, expires, source)


class Provider:
    def __init__(self, config: Mapping[str, str], env: Mapping[str, str] | None = None, cache: Cache = CACHE,
                 read_token: Callable[[str], str] | None = None):
        self.config = config
        self.env = os.environ if env is None else env
        self.cache = cache
        self.read_token = read_token or token_from_file
        self.seen: list[str] = []
        self._source = ''

    def hidden(self) -> list[str]:
        own = [self.config.get('secret-access-key') or '', self.config.get('session-token') or '', self.config.get('access-key-id') or '']
        return [s for s in own + self.seen if s]

    def _keep(self, creds: Credentials) -> Credentials:
        self.seen += [s for s in creds.secrets() if s not in self.seen]
        return creds

    def source(self) -> str:
        if self._source:
            return self._source
        base = SOURCE_KEYS if self.keys() is not None else SOURCE_WEB if self.web() is not None else ''
        if base and (self.config.get('role-arn') or '').strip():
            return f'{base}, then {SOURCE_ROLE}'
        return base

    def keys(self) -> tuple[str, str, str] | None:
        key_id, secret = (self.config.get('access-key-id') or '').strip(), (self.config.get('secret-access-key') or '').strip()
        if not key_id and not secret:
            return None
        if not key_id or not secret:
            raise AwsError('give both Access key ID and Secret access key, or neither to use IRSA')
        return key_id, secret, (self.config.get('session-token') or '').strip()

    def web(self) -> dict[str, str] | None:
        return env_kit.env_set(self.env, NAME, (ROLE_ENV, TOKEN_ENV))

    def credentials(self, aws: Aws) -> Credentials:
        base = self.base(aws)
        role = (self.config.get('role-arn') or '').strip()
        if not role:
            return base
        if role_problem(role):
            raise AwsError(f'Role ARN: {role_problem(role)}')
        key = digest('role', aws.region, role, base.key_id, digest(base.secret, base.token))
        found = self.cache.get(key, aws.clock())
        if found is None:
            root = aws.sts('AssumeRole', {'RoleArn': role, 'RoleSessionName': SESSION_NAME, 'DurationSeconds': str(SESSION_SECONDS)}, base)
            found = sts_credentials(root, f'{base.source}, then {SOURCE_ROLE}', 'sts:AssumeRole')
            self.cache.put(key, found)
        self._source = found.source
        return self._keep(found)

    def base(self, aws: Aws) -> Credentials:
        keys = self.keys()
        if keys is not None:
            self._source = SOURCE_KEYS
            return self._keep(Credentials(*keys, source=SOURCE_KEYS))
        web = self.web()
        if web is None:
            raise AwsError(NO_CREDENTIALS)
        role, path = web[ROLE_ENV], web[TOKEN_ENV]
        key = digest('web', aws.region, role, path)
        found = self.cache.get(key, aws.clock())
        if found is None:
            token = self.read_token(path)
            if not token:
                raise AwsError(f'the web identity token file named by {TOKEN_ENV} cannot be read or is empty')
            self.seen.append(token)
            root = aws.sts('AssumeRoleWithWebIdentity', {'RoleArn': role, 'RoleSessionName': SESSION_NAME, 'WebIdentityToken': token,
                                                         'DurationSeconds': str(SESSION_SECONDS)}, None)
            found = sts_credentials(root, SOURCE_WEB, 'sts:AssumeRoleWithWebIdentity')
            self.cache.put(key, found)
        self._source = SOURCE_WEB
        return self._keep(found)


def token_from_file(path: str) -> str:
    try:
        file = Path(path)
        if not file.is_file() or file.stat().st_size > TOKEN_FILE_LIMIT:
            return ''
        return file.read_text(encoding='utf-8').strip()
    except (OSError, UnicodeDecodeError, ValueError):
        return ''


def client_for(config: Mapping[str, str], transport: Transport, timeout: float = TIMEOUT, env: Mapping[str, str] | None = None,
               clock: Callable[[], float] = time.time) -> Aws:
    region = (config.get('region') or '').strip()
    if region_problem(region):
        raise AwsError(f'Region: {region_problem(region)}')
    return Aws(region, Provider(config, env), transport, timeout, clock)
