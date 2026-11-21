import json
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass

from pymongo.errors import PyMongoError

from skopax.auth import login_name, sso_email
from skopax.messages import msg
from skopax.models import email_allowed
from skopax.roles import ResolverError, Resolvers, choose, claim_values, last_known, mapped_role, role_map, said_down
from skopax.sessions import role_at_least
from skopax.tunables import Invalid
from skopax.users import EMAIL, NAME_MAX, Unopened, UserExists, Users, admin_set_of

log = logging.getLogger('skopax.signin')

UNAVAILABLE = ('database', 'resolver')


class Refused(Exception):
    def __init__(self, reason: str, email: str | None = None):
        self.reason = reason
        self.email = email
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


def domain_refused(email: str, settings) -> bool:
    return not email_allowed(email, tuple(settings.sso_allowed_domains or ()))


def new_refused(existing: dict | None, settings) -> bool:
    return existing is None and settings.sso_new_accounts == 'added-only'


def restricted(settings) -> bool:
    return bool(tuple(settings.sso_allowed_domains or ())) or settings.sso_new_accounts == 'added-only'


def owns(claims: dict, email: str) -> bool:
    return text_claim(claims, 'email').strip().lower() == email and email_verified(claims.get('email_verified'), True)


def unopened(doc: dict | None) -> bool:
    return doc is not None and doc.get('source') == 'sso' and doc.get('opened') is False


def address_of(claims: dict, settings) -> str:
    if restricted(settings):
        own = text_claim(claims, 'email').strip()
        if not EMAIL.match(own) or not owns(claims, own.lower()):
            raise Refused('unconfirmed')
        return own.lower()
    email = sso_email(claims)
    if email is None:
        raise Refused('no-email')
    if not email_verified(claims.get('email_verified'), bool(settings.sso_require_verified_email)):
        raise Refused('unverified')
    return email


class SignIn:
    def __init__(self, users: Users, settings, resolvers: Resolvers, clock: Callable[[], float] = time.time, on_bound: Callable[[str], None] = lambda email: None):
        self.users = users
        self.settings = settings
        self.resolvers = resolvers
        self.clock = clock
        self.on_bound = on_bound

    def page(self, claims: dict, issuer: str) -> dict:
        email = address_of(claims, self.settings)
        if domain_refused(email, self.settings):
            raise Refused('domain', email)
        try:
            existing = self.users.get(email)
            if existing is not None and existing.get('source') != 'sso':
                return self.linked(existing, claims, email)
            if existing is not None and existing.get('disabled'):
                raise Refused('disabled')
            if unopened(existing) and not owns(claims, email):
                raise Refused('unverified')
            if new_refused(existing, self.settings):
                raise Refused('not-added', email)
            name = display_name(claims, email)
            user = {'email': email, 'name': name, 'source': 'sso', 'role': (existing or {}).get('role'), 'claims': json_safe(claims)}
            kept = None
            try:
                resolved = self.resolvers.resolve(user, 'page')
                mapped = mapped_role(claim_values(claims, self.settings.sso_roles_claim), role_map(self.settings))
            except ResolverError as e:
                kept = self.last_role(existing, e)
            except Invalid as e:
                raise Refused('resolver') from e
            try:
                stored = self.users.upsert_sso(email, name, self.settings.sso_default_role, verified=owns(claims, email))
            except UserExists:
                raise Refused('local-account') from None
            except Unopened:
                raise Refused('unverified') from None
            if stored is None or stored.get('disabled'):
                raise Refused('disabled')
            if kept is None:
                role, role_from = choose(resolved=resolved, mapped=mapped, admin_set=admin_set_of(stored), default=self.settings.sso_default_role)
                stored = self.users.apply_sign_in_role(email, role, role_from, seen=stored, at=self.clock())
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
        if self.users.idp_name(email, display_name(claims, email)):
            stored = self.users.get(email) or stored
        return stored

    def mcp(self, claims: dict, issuer: str) -> McpUser:
        email = address_of(claims, self.settings)
        if domain_refused(email, self.settings):
            raise Refused('domain', email)
        sub = text_claim(claims, 'sub')
        try:
            existing = self.users.get(email)
            if existing is not None and existing.get('source') != 'sso':
                linked = self.mcp_linked(existing, text_claim(claims, 'iss'), sub, email)
                self.mcp_used(email)
                return McpUser(name=email, sub=sub, email=email, role=linked)
            if existing is not None and existing.get('disabled'):
                raise Refused('disabled')
            if unopened(existing) and not owns(claims, email):
                raise Refused('unverified')
            if new_refused(existing, self.settings):
                raise Refused('not-added', email)
            name = display_name(claims, email)
            user = {'email': email, 'name': (existing or {}).get('name') or name, 'source': 'sso', 'role': (existing or {}).get('role'), 'claims': json_safe(claims)}
            kept = None
            try:
                resolved = self.resolvers.resolve(user, 'mcp')
                mapped = mapped_role(claim_values(claims, self.settings.sso_roles_claim), role_map(self.settings))
            except ResolverError as e:
                kept = self.last_role(existing, e)
            except Invalid as e:
                raise Refused('resolver') from e
            fresh = existing is None
            stored = existing if existing is not None else self.users.insert_sso(email, name, self.settings.sso_default_role)
            if stored is not None and stored.get('source') != 'sso':
                raise Refused('local-account')
            if stored is None or stored.get('disabled'):
                raise Refused('disabled')
            if unopened(stored):
                stored = self.mcp_opened(claims, email)
                fresh = True
            if kept is None and resolved is not None:
                self.users.stamp_resolved(email, resolved, self.clock())
            wrote = self.users.stamp_first_login(email)
            self.mcp_used(email, fresh or wrote)
        except PyMongoError as e:
            raise Refused('database') from e
        role = kept if kept is not None else choose(resolved=resolved, mapped=mapped, admin_set=admin_set_of(stored), default=self.settings.sso_default_role)[0]
        return McpUser(name=email, sub=sub, email=email, role=role)

    def mcp_used(self, email: str, seeded: bool = False) -> None:
        try:
            self.users.record_mcp(email, seeded)
        except PyMongoError as e:
            log.warning('%s', f'The MCP use record could not be written ({type(e).__name__}); MCP access goes on')

    def mcp_opened(self, claims: dict, email: str) -> dict:
        if not owns(claims, email):
            raise Refused('unverified')
        opened = self.users.open_sso(email)
        if opened is None:
            opened = self.users.get(email)
        if opened is None or opened.get('source') != 'sso':
            raise Refused('local-account')
        if opened.get('disabled'):
            raise Refused('disabled')
        if unopened(opened):
            raise Refused('unverified')
        return opened

    def last_role(self, existing: dict | None, error: ResolverError) -> str:
        found = last_known(existing, self.clock(), int(getattr(self.settings, 'roles_stale_max_minutes', 0) or 0))
        said_down(error, found[1] if found else None)
        if found is None:
            raise Refused('resolver') from error
        return found[0]

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
