import hashlib
import hmac
import time
from dataclasses import dataclass, field
from urllib.parse import parse_qsl, quote, urlsplit

ALGORITHM = 'AWS4-HMAC-SHA256'
TERMINATOR = 'aws4_request'
UNRESERVED = '-_.~'
DATE_HEADER = 'x-amz-date'
TOKEN_HEADER = 'x-amz-security-token'


@dataclass(frozen=True)
class Credentials:
    key_id: str = field(repr=False)
    secret: str = field(repr=False)
    token: str = field(default='', repr=False)
    expires: float | None = None
    source: str = ''

    def secrets(self) -> tuple[str, ...]:
        return tuple(s for s in (self.secret, self.token, self.key_id) if s)


def amz_date(at: float) -> str:
    return time.strftime('%Y%m%dT%H%M%SZ', time.gmtime(at))


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _hmac(key: bytes, text: str) -> bytes:
    return hmac.new(key, text.encode('utf-8'), hashlib.sha256).digest()


def signing_key(secret: str, day: str, region: str, service: str) -> bytes:
    key = _hmac(('AWS4' + secret).encode('utf-8'), day)
    key = _hmac(key, region)
    key = _hmac(key, service)
    return _hmac(key, TERMINATOR)


def canonical_path(path: str) -> str:
    return quote(path or '/', safe='/' + UNRESERVED)


def canonical_query(query: str) -> str:
    pairs = [(quote(k, safe=UNRESERVED), quote(v, safe=UNRESERVED)) for k, v in parse_qsl(query, keep_blank_values=True)]
    return '&'.join(f'{k}={v}' for k, v in sorted(pairs))


def header_value(value: str) -> str:
    return ' '.join(str(value).split())


def canonical_headers(headers: dict[str, str]) -> tuple[str, str]:
    lowered: dict[str, list[str]] = {}
    for name, value in headers.items():
        lowered.setdefault(name.strip().lower(), []).append(header_value(value))
    names = sorted(lowered)
    return ''.join(f"{n}:{','.join(lowered[n])}\n" for n in names), ';'.join(names)


def canonical_request(method: str, url: str, headers: dict[str, str], body: bytes) -> tuple[str, str]:
    parts = urlsplit(url)
    block, signed = canonical_headers(headers)
    text = '\n'.join((method.upper(), canonical_path(parts.path), canonical_query(parts.query), block, signed, sha256_hex(body or b'')))
    return text, signed


def scope_of(day: str, region: str, service: str) -> str:
    return f'{day}/{region}/{service}/{TERMINATOR}'


def string_to_sign(stamp: str, scope: str, request: str) -> str:
    return '\n'.join((ALGORITHM, stamp, scope, sha256_hex(request.encode('utf-8'))))


def signed_headers(method: str, url: str, headers: dict[str, str], body: bytes, creds: Credentials, region: str, service: str, at: float) -> dict[str, str]:
    stamp = amz_date(at)
    host = urlsplit(url).netloc
    signing = {k: v for k, v in headers.items() if k.lower() not in ('host', DATE_HEADER, TOKEN_HEADER, 'authorization')}
    signing |= {'host': host, DATE_HEADER: stamp}
    if creds.token:
        signing[TOKEN_HEADER] = creds.token
    request, signed = canonical_request(method, url, signing, body)
    scope = scope_of(stamp[:8], region, service)
    signature = hmac.new(signing_key(creds.secret, stamp[:8], region, service), string_to_sign(stamp, scope, request).encode('utf-8'), hashlib.sha256).hexdigest()
    out = {k: v for k, v in signing.items() if k != 'host'}
    out['Authorization'] = f'{ALGORITHM} Credential={creds.key_id}/{scope}, SignedHeaders={signed}, Signature={signature}'
    return out
