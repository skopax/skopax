import json
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass

from pymongo.errors import PyMongoError

from skopax.auth import login_name, sso_email
from skopax.messages import msg
from skopax.roles import ResolverError, Resolvers, choose, claim_values, mapped_role, role_map
from skopax.sessions import role_at_least
from skopax.tunables import Invalid
from skopax.users import NAME_MAX, UserExists, Users, admin_set_of

log = logging.getLogger('skopax.signin')

UNAVAILABLE = ('database', 'resolver')


class Refused(Exception):
    def __init__(self, reason: str):
        self.reason = reason
        self.status = 503 if reason in UNAVAILABLE else 401
        super().__init__(reason)


@dataclass(frozen=True)
class McpUser:
    name: str
    sub: str
    email: str
    role: str

    @property
    def operator(self) -> bool:
        return role_at_least(self.role, 'operator')

    @property
    def admin(self) -> bool:
        return self.role == 'admin'


def email_verified(value, required: bool) -> bool:
    if value is None:
        return not required
    return value is True or isinstance(value, str) and value.lower() == 'true'


def display_name(claims: dict, email: str) -> str:
    label = login_name(claims).strip()
    return (label if label and label != 'unknown' else email)[:NAME_MAX].strip()


def json_safe(claims: dict) -> dict:
    return json.loads(json.dumps(claims, default=str))


def text_claim(claims: dict, name: str) -> str:
    value = claims.get(name)
    return value if isinstance(value, str) else ''


class SignIn:
    def __init__(self, users: Users, settings, resolvers: Resolvers, clock: Callable[[], float] = time.time, on_bound: Callable[[str], None] = lambda email: None):
        self.users = users
        self.settings = settings
        self.resolvers = resolvers
        self.clock = clock
        self.on_bound = on_bound

    def page(self, claims: dict, issuer: str) -> dict:
        email = sso_email(claims)
        if email is None:
            raise Refused('no-email')
        if not email_verified(claims.get('email_verified'), bool(self.settings.sso_require_verified_email)):
            raise Refused('unverified')
        try:
            existing = self.users.get(email)
            if existing is not None and existing.get('source') != 'sso':
                return self.linked(existing, claims, email)
            if existing is not None and existing.get('disabled'):
                raise Refused('disabled')
            name = display_name(claims, email)
            user = {'email': email, 'name': name, 'source': 'sso', 'role': (existing or {}).get('role'), 'claims': json_safe(claims)}
            try:
                resolved = self.resolvers.resolve(user, 'page')
                mapped = mapped_role(claim_values(claims, self.settings.sso_roles_claim), role_map(self.settings))
            except (ResolverError, Invalid) as e:
                raise Refused('resolver') from e
            try:
                stored = self.users.upsert_sso(email, name, self.settings.sso_default_role)
            except UserExists:
                raise Refused('local-account') from None
            if stored is None or stored.get('disabled'):
                raise Refused('disabled')
            role, role_from = choose(resolved=resolved, mapped=mapped, admin_set=admin_set_of(stored), default=self.settings.sso_default_role)
            stored = self.users.apply_sign_in_role(email, role, role_from, seen=stored)
        except PyMongoError as e:
            raise Refused('database') from e
        if stored is None or stored.get('disabled'):
            raise Refused('disabled')
        return stored

    def linked(self, existing: dict, claims: dict, email: str) -> dict:
        if existing.get('disabled'):
            raise Refused('disabled')
        held = existing.get('ssoLink') if existing.get('source') == 'local' else None
        state = held.get('state') if isinstance(held, dict) else None
        iss, sub = text_claim(claims, 'iss'), text_claim(claims, 'sub')
        if state not in ('pending', 'bound') or not iss or not sub:
            raise Refused('local-account')
        if state == 'pending':
            own = text_claim(claims, 'email').strip().lower()
            if own != email or not email_verified(claims.get('email_verified'), True):
                raise Refused('local-account')
            if not self.users.bind_sso(email, iss, sub, self.clock()):
                raise Refused('local-account')
            self.on_bound(email)
        elif held.get('iss') != iss or held.get('sub') != sub:
            log.warning('%s', msg('access.sso_other_subject', email=email))
            raise Refused('subject')
        stored = self.users.linked_login(email, iss, sub)
        if stored is None:
            raise Refused('local-account')
        return stored

    def mcp(self, claims: dict, issuer: str) -> McpUser:
        email = sso_email(claims)
        if email is None:
            raise Refused('no-email')
        if not email_verified(claims.get('email_verified'), bool(self.settings.sso_require_verified_email)):
            raise Refused('unverified')
        sub = text_claim(claims, 'sub')
        try:
            existing = self.users.get(email)
            if existing is not None and existing.get('source') != 'sso':
                return McpUser(name=email, sub=sub, email=email, role=self.mcp_linked(existing, text_claim(claims, 'iss'), sub, email))
            if existing is not None and existing.get('disabled'):
                raise Refused('disabled')
            name = display_name(claims, email)
            user = {'email': email, 'name': (existing or {}).get('name') or name, 'source': 'sso', 'role': (existing or {}).get('role'), 'claims': json_safe(claims)}
            try:
                resolved = self.resolvers.resolve(user, 'mcp')
                mapped = mapped_role(claim_values(claims, self.settings.sso_roles_claim), role_map(self.settings))
            except (ResolverError, Invalid) as e:
                raise Refused('resolver') from e
            stored = existing if existing is not None else self.users.insert_sso(email, name, self.settings.sso_default_role)
            if stored is not None and stored.get('source') != 'sso':
                raise Refused('local-account')
            if stored is None or stored.get('disabled'):
                raise Refused('disabled')
        except PyMongoError as e:
            raise Refused('database') from e
        role, _ = choose(resolved=resolved, mapped=mapped, admin_set=admin_set_of(stored), default=self.settings.sso_default_role)
        return McpUser(name=email, sub=sub, email=email, role=role)

    def mcp_linked(self, existing: dict, iss: str, sub: str, email: str) -> str:
        if existing.get('disabled'):
            raise Refused('disabled')
        held = existing.get('ssoLink') if existing.get('source') == 'local' else None
        if not isinstance(held, dict) or held.get('state') != 'bound' or not iss or not sub:
            raise Refused('local-account')
        if held.get('iss') != iss or held.get('sub') != sub:
            log.warning('%s', msg('access.sso_other_subject', email=email))
            raise Refused('local-account')
        return existing.get('role')

    def slack_role(self, email: str) -> str | None:
        key = str(email or '').strip().lower()
        doc = self.users.get(key)
        if doc is not None and doc.get('disabled'):
            return None
        if doc is not None and doc.get('source') != 'sso':
            return doc.get('role')
        user = {'email': key, 'name': (doc or {}).get('name') or '', 'source': (doc or {}).get('source'), 'role': (doc or {}).get('role'), 'claims': {}}
        resolved = self.resolvers.resolve(user, 'slack')
        if resolved is not None:
            return resolved
        return doc.get('role') if doc is not None else None
