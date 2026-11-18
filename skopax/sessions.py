import base64
import hashlib
import hmac
import json
import logging
import time
from dataclasses import dataclass
from typing import Callable

from skopax.sealer import META_ID, master_key
from skopax.users import ROLES

log = logging.getLogger('skopax.sessions')

SESSION_KIND = 'session'
LOGIN_KIND = 'login'
LEGACY_SESSION_SECONDS = 8 * 3600
SESSION_RENEW_SECONDS = 300
SECRET_ID = META_ID


def b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b'=').decode()


def unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + '=' * (-len(text) % 4))


def role_at_least(role: str | None, need: str) -> bool:
    return role in ROLES and need in ROLES and ROLES.index(role) >= ROLES.index(need)


@dataclass(frozen=True)
class User:
    name: str
    role: str
    source: str = 'local'

    @property
    def operator(self) -> bool:
        return role_at_least(self.role, 'operator')

    @property
    def admin(self) -> bool:
        return self.role == 'admin'


def derive_secret(key: str | bytes) -> bytes:
    raw = key.encode() if isinstance(key, str) else key
    return hashlib.sha256(b'skopax-session:' + raw).digest()


def session_secret(db, settings) -> bytes:
    key = master_key(db, settings)
    if not settings.secret_key:
        log.warning('SKOPAX_SECRET_KEY is not set; using a key generated once and kept in the database. Set SKOPAX_SECRET_KEY to manage it yourself')
    return derive_secret(key)


class Sessions:
    def __init__(self, secret: bytes, idle_seconds: int, max_seconds: int, clock: Callable[[], float] = time.time):
        self.secret = secret
        self.idle_seconds = idle_seconds
        self.max_seconds = max_seconds
        self.clock = clock

    def seal(self, payload: dict, seconds: int, kind: str) -> str:
        body = b64(json.dumps({**payload, 'kind': kind, 'exp': int(self.clock()) + seconds}, separators=(',', ':')).encode())
        return body + '.' + b64(hmac.new(self.secret, body.encode(), hashlib.sha256).digest())

    def unseal(self, sealed: str | None, kind: str) -> dict | None:
        if not sealed or '.' not in sealed:
            return None
        body, sig = sealed.rsplit('.', 1)
        if not sig.isascii() or not hmac.compare_digest(sig, b64(hmac.new(self.secret, body.encode(), hashlib.sha256).digest())):
            return None
        try:
            payload = json.loads(unb64(body))
        except ValueError:
            return None
        if not isinstance(payload, dict) or payload.get('exp', 0) < self.clock():
            return None
        if (payload.get('kind') or (LOGIN_KIND if 'verifier' in payload else SESSION_KIND)) != kind:
            return None
        return payload

    def issue(self, name: str, role: str, source: str) -> str:
        now = int(self.clock())
        return self.seal({'name': name, 'role': role, 'src': source, 'login': now, 'iat': now}, self.idle_seconds, SESSION_KIND)

    def renew(self, cookie: str | None) -> tuple[str, int] | None:
        payload = self.unseal(cookie, SESSION_KIND)
        if payload is None:
            return None
        now = int(self.clock())
        exp = int(payload['exp'])
        login = int(payload.get('login', exp - LEGACY_SESSION_SECONDS))
        issued = int(payload.get('iat', exp - LEGACY_SESSION_SECONDS))
        if now - issued < SESSION_RENEW_SECONDS:
            return None
        seconds = min(self.idle_seconds, login + self.max_seconds - now)
        if seconds <= 0 or now + seconds <= exp:
            return None
        return self.seal({**payload, 'login': login, 'iat': now}, seconds, SESSION_KIND), seconds
