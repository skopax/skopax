import hashlib
import secrets

from skopax.oidc import AuthError
from skopax.sessions import b64
from skopax.users import EMAIL

__all__ = ['AuthError', 'SESSION_COOKIE', 'LOGIN_COOKIE', 'LOGIN_SECONDS', 'bearer_token', 'login_name', 'sso_email', 'pkce_pair', 'safe_next']

SESSION_COOKIE = 'skopax_session'
LOGIN_COOKIE = 'skopax_login'
LOGIN_SECONDS = 600


def bearer_token(authorization: str | None) -> str | None:
    parts = (authorization or '').split(None, 1)
    return parts[1].strip() if len(parts) == 2 and parts[0].lower() == 'bearer' and parts[1].strip() else None


def login_name(claims: dict) -> str:
    return next((str(claims[k]) for k in ('name', 'preferred_username', 'email', 'sub') if claims.get(k)), 'unknown')


def sso_email(claims: dict) -> str | None:
    for key in ('email', 'preferred_username', 'sub'):
        value = str(claims.get(key) or '').strip()
        if EMAIL.match(value):
            return value.lower()
    return None


def pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)
    return verifier, b64(hashlib.sha256(verifier.encode()).digest())


def safe_next(target: str | None) -> str:
    if not target or not target.startswith('/') or target.startswith('//') or '\\' in target:
        return '/'
    return target
