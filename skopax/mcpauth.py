import hashlib
import math
import threading
import time
from collections import OrderedDict
from typing import TYPE_CHECKING, Callable

from skopax import brand
from skopax.auth import sso_email
from skopax.config import MCP_CLIENT_ID, MCP_SCOPE, origin_of
from skopax.messages import msg
from skopax.oidc import OidcError, verify
from skopax.roles import claim_values
from skopax.signin import McpUser, Refused

if TYPE_CHECKING:
    from skopax.signin import SignIn
    from skopax.sso import SignOn

__all__ = ['CACHE_MAX', 'CACHE_S', 'Forbidden', 'McpAuth', 'McpUser', 'Unavailable']

CACHE_S = 30
CACHE_MAX = 1000
PAUSE_S = 5
PROVIDER_DOWN = ('unreachable', 'discovery', 'endpoint')
REFUSALS = {'no-email': ('mcp.no_email', 'mcp.no_email_header'), 'unverified': ('mcp.unverified', 'mcp.unverified_header'),
            'local-account': ('mcp.local_account', 'mcp.local_account_header'), 'disabled': ('mcp.disabled', 'mcp.disabled_header'),
            'not-added': ('mcp.not_allowed', 'mcp.not_allowed_header'), 'domain': ('mcp.not_allowed', 'mcp.not_allowed_header'),
            'unconfirmed': ('mcp.not_allowed', 'mcp.not_allowed_header')}


class Forbidden(Exception):
    def __init__(self, reason: str, description: str | None = None):
        self.detail = reason
        super().__init__(self.detail)
        self.description = description or msg('mcp.no_role_header')


class Unavailable(Exception):
    def __init__(self, reason: str = 'database'):
        self.reason = reason
        super().__init__(reason)

    @property
    def detail(self) -> str:
        return msg({'resolver': 'mcp.roles_unavailable', 'provider': 'mcp.provider_unavailable'}.get(self.reason, 'db.unavailable'))


class McpAuth:
    def __init__(self, settings, signon: 'SignOn', signin: 'SignIn', base_url: str, clock: Callable[[], float] = time.monotonic,
                 sessions_after: Callable[[], float | None] = lambda: None):
        self.settings = settings
        self.signon = signon
        self.signin = signin
        self.base_url = base_url
        self.clock = clock
        self.sessions_after = sessions_after
        self.cache: OrderedDict[str, tuple[float, McpUser, object]] = OrderedDict()
        self.guard = threading.Lock()
        self.paused_until = 0.0
        self.epoch = 0
        self.generations: dict[str, int] = {}

    @property
    def enabled(self) -> bool:
        return self.signon.active()

    def client_id(self) -> str:
        return (self.settings.mcp_client_id or '').strip() or MCP_CLIENT_ID

    def scope(self) -> str:
        return (self.settings.mcp_scope or '').strip() or MCP_SCOPE

    def issuer(self) -> str:
        s = self.settings
        return ((s.mcp_issuer or '').strip() or origin_of(s.sso_authorize_url or '') or (s.sso_issuer or '').strip()).rstrip('/')

    def base(self) -> str:
        return ((self.settings.mcp_resource_url or '').strip() or self.base_url).rstrip('/')

    def resource(self) -> str:
        return f'{self.base()}/mcp'

    def metadata_url(self) -> str:
        return f'{self.base()}/.well-known/oauth-protected-resource'

    def metadata(self) -> dict:
        return {'resource': self.resource(), 'authorization_servers': [self.issuer()], 'scopes_supported': [self.scope()],
                'bearer_methods_supported': ['header'], 'resource_name': brand.product()}

    def challenge(self, error: str | None = None, description: str | None = None) -> str:
        quoted = (description or '').encode('ascii', 'replace').decode('ascii').replace('\\', '\\\\').replace('"', '\\"')
        parts = ([f'error="{error}"'] if error else []) + ([f'error_description="{quoted}"'] if description else [])
        return 'Bearer ' + ', '.join(parts + [f'resource_metadata="{self.metadata_url()}"', f'scope="{self.scope()}"'])

    def audiences(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(((self.settings.mcp_audience or '').strip() or self.client_id(), self.resource())))

    def issuers(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(i for i in (self.issuer(), self.signon.provider().endpoints().issuer) if i))

    def user(self, token: str) -> McpUser:
        key = hashlib.sha256(token.encode()).hexdigest()
        hit = self._cached(key)
        if hit is not None:
            self._after_stop(hit[1])
            return hit[0]
        epoch = self._epoch()
        try:
            provider = self.signon.provider()
            claims = verify(token, provider.keys(), issuers=self.issuers(), audiences=self.audiences(), allowed_clients=tuple(self.settings.mcp_allowed_clients or ()))
            issuer = provider.endpoints().issuer
        except OidcError as e:
            if e.reason in PROVIDER_DOWN:
                raise Unavailable('provider') from None
            raise
        self._after_stop(claims.get('iat'))
        required = (self.settings.mcp_required_role or '').strip()
        if required and required not in claim_values(claims, self.settings.sso_roles_claim):
            raise Forbidden(msg('mcp.role_missing', role=required))
        seen = (epoch, self._generation(sso_email(claims)))
        user = self._signed_in(claims, issuer)
        ttl = min(CACHE_S, float(claims['exp']) - time.time())
        if ttl > 0:
            self._keep(key, self.clock() + ttl, user, claims.get('iat'), seen)
        return user

    def _after_stop(self, iat) -> None:
        after = self.sessions_after()
        if after is None:
            return
        if not isinstance(iat, (int, float)) or isinstance(iat, bool) or math.floor(iat) < after:
            raise OidcError('stopped', 'the token was issued before single sign-on was last switched off')

    def _epoch(self) -> int:
        with self.guard:
            return self.epoch

    def _generation(self, email: str | None) -> int:
        with self.guard:
            return self.generations.get(email or '', 0)

    def _signed_in(self, claims: dict, issuer: str) -> McpUser:
        if self.clock() < self.paused_until:
            raise Unavailable('database')
        try:
            return self.signin.mcp(claims, issuer)
        except Refused as e:
            if e.reason == 'database':
                self.paused_until = self.clock() + PAUSE_S
                raise Unavailable('database') from None
            if e.reason in REFUSALS:
                body, header = REFUSALS[e.reason]
                raise Forbidden(msg(body), msg(header)) from None
            raise Unavailable('resolver') from None

    def _cached(self, key: str) -> tuple[McpUser, object] | None:
        with self.guard:
            hit = self.cache.get(key)
            if hit is None:
                return None
            if hit[0] <= self.clock():
                del self.cache[key]
                return None
            self.cache.move_to_end(key)
            return hit[1], hit[2]

    def _keep(self, key: str, until: float, user: McpUser, iat, seen: tuple[int, int]) -> None:
        with self.guard:
            if seen != (self.epoch, self.generations.get(user.email, 0)):
                return
            self.cache[key] = (until, user, iat)
            self.cache.move_to_end(key)
            while len(self.cache) > CACHE_MAX:
                self.cache.popitem(last=False)

    def forget(self, email: str) -> None:
        key = str(email or '').strip().lower()
        with self.guard:
            self.generations[key] = self.generations.get(key, 0) + 1
            for token in [t for t, (_, user, _) in self.cache.items() if user.email == key]:
                del self.cache[token]

    def clear(self) -> None:
        with self.guard:
            self.epoch += 1
            self.cache.clear()
