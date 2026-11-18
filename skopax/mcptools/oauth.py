import asyncio
import base64
import datetime as dt
import email.utils
import hashlib
import json
import logging
import re
import secrets
import threading
import time
from collections.abc import Callable, Coroutine, Mapping
from dataclasses import dataclass
from functools import partial
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit

import anyio
import httpx
from mcp.client.auth import OAuthFlowError, OAuthRegistrationError
from mcp.client.auth.utils import (build_oauth_authorization_server_metadata_discovery_urls, build_protected_resource_metadata_discovery_urls,
                                   create_client_registration_request, extract_resource_metadata_from_www_auth, handle_registration_response,
                                   validate_metadata_issuer)
from mcp.client.session import DEFAULT_CLIENT_INFO
from mcp.client.streamable_http import MCP_PROTOCOL_VERSION
from mcp.shared.auth import OAuthClientInformationFull, OAuthClientMetadata, OAuthMetadata, OAuthToken, ProtectedResourceMetadata
from mcp.shared.auth_utils import check_resource_allowed, resource_url_from_server_url
from mcp.types import LATEST_PROTOCOL_VERSION
from pydantic import ValidationError
from pymongo import ASCENDING, DESCENDING, ReturnDocument

from skopax.actors import SYSTEM
from skopax.auth import pkce_pair
from skopax.engine import iso, parse_iso
from skopax.errkind import Authored, kind
from skopax.messages import msg
from skopax.sealer import Sealer, stored_key
from skopax.mcptools import (OAUTH_DEADLINE_S, PARK_REASONS, REFRESH_EARLY_S, RETRY_AFTER_MAX_S, RETRY_AFTER_S, START_DEADLINE_S, STATE_TTL_S, STATES_PER_INSTANCE,
                             Moved, Reconnect, State, left, secrets_of, within)
from skopax.mcptools import presets
from skopax.mcptools.client import McpFailure, client_for, failure_text, guard_url, lacks, on_loop, refused_scope

log = logging.getLogger('skopax.mcptools')

AUTH = 'mcp_auth'
STATES = 'mcp_oauth_states'
CALLBACK_PATH = '/api/admin/mcp-tools/oauth/callback'
LANDING = '/#t=integrations'
EXPIRED_LANDING = '/#t=integrations/signin-expired'
PRM_TRIES = 3
ASM_TRIES = 4
S256 = 'S256'
GRANTS = ['authorization_code', 'refresh_token']
LOCAL_BASES = ('localhost', '127.0.0.1')
CONNECTED, DENIED, EXPIRED, FAILED, RECONNECT = 'connected', 'denied', 'expired', 'failed', 'reconnect'
SHOWN_STATES = (CONNECTED, RECONNECT)
RESULT_TEXTS = {CONNECTED: 'mcptools.oauth.result.connected', DENIED: 'mcptools.oauth.result.denied', EXPIRED: 'mcptools.oauth.result.expired',
                FAILED: 'mcptools.oauth.result.failed'}
DYNAMIC, REGISTERED = 'dynamic', 'registered'
BASIC, POST, NONE = 'client_secret_basic', 'client_secret_post', 'none'
OK = 200
CONFLICT = 409
BAD_GATEWAY = 502
REFUSED_FROM, REFUSED_TO = 400, 500
TRANSIENT = (408, 425, 429)
SECONDS = re.compile(r'[0-9]+')
PEER_POLL_S = 0.1
EPOCH_BYTES = 8
LIVE = ('issuer', 'authorizationEndpoint', 'tokenEndpoint', 'resource', 'audience', 'redirectUri', 'client', 'clientKind', 'ca')
JSON_ACCEPT = {'accept': 'application/json'}
PROBE_HEADERS = {'accept': 'application/json, text/event-stream', MCP_PROTOCOL_VERSION: LATEST_PROTOCOL_VERSION}
INITIALIZE = {'jsonrpc': '2.0', 'id': 0, 'method': 'initialize',
              'params': {'protocolVersion': LATEST_PROTOCOL_VERSION, 'capabilities': {}, 'clientInfo': DEFAULT_CLIENT_INFO.model_dump(exclude_none=True)}}


class NotOAuth(Authored, Exception):
    pass


class FlowError(Authored, Exception):
    def __init__(self, status: int, text: str):
        super().__init__(text)
        self.status = status


class NoScope(Authored, Reconnect):
    pass


@dataclass(frozen=True)
class Found:
    issuer: str
    authorization: str
    token: str
    audience: str
    client: OAuthClientInformationFull
    kind: str


@dataclass(frozen=True)
class Held:
    access: str
    refresh: str
    expires: float | None
    rev: int
    resource: str
    epoch: str | None

    @property
    def key(self) -> dict:
        return {'rev': self.rev, 'resource': self.resource, 'epoch': self.epoch}

    def due(self, now: float) -> bool:
        return self.expires is not None and self.expires - REFRESH_EARLY_S <= now

    def expired(self, now: float) -> bool:
        return self.expires is not None and self.expires <= now


def key_of(doc: dict) -> dict:
    return {'rev': int(doc.get('rev') or 0), 'resource': str(doc.get('resource') or ''), 'epoch': doc.get('epoch')}


def digest(state: str) -> str:
    return hashlib.sha256(state.encode()).hexdigest()


def stamp(t: float) -> dt.datetime:
    return dt.datetime.fromtimestamp(t, dt.timezone.utc)


def seconds(found: object) -> float:
    if not isinstance(found, dt.datetime):
        return 0.0
    return found.replace(tzinfo=found.tzinfo or dt.timezone.utc).timestamp()


def usable_base(base: str) -> bool:
    try:
        parts = urlsplit(base)
        port = parts.port
    except ValueError:
        return False
    host = parts.hostname or ''
    if not host or parts.query or parts.fragment or '@' in parts.netloc:
        return False
    return parts.scheme == 'https' or (parts.scheme == 'http' and host in LOCAL_BASES and port is not None)


def with_query(url: str, params: list[tuple[str, str]]) -> str:
    parts = urlsplit(url)
    return urlunsplit(parts._replace(query=urlencode(parse_qsl(parts.query, keep_blank_values=True) + params)))


def method_for(metadata: OAuthMetadata, secret: str) -> str:
    if not secret:
        return NONE
    offered = metadata.token_endpoint_auth_methods_supported
    return POST if offered is not None and BASIC not in offered and POST in offered else BASIC


def client_auth(info: OAuthClientInformationFull, form: dict[str, str]) -> dict[str, str]:
    form['client_id'] = info.client_id or ''
    secret = info.client_secret or ''
    method = info.token_endpoint_auth_method or (BASIC if secret else NONE)
    if secret and method == BASIC:
        pair = f"{quote(info.client_id or '', safe='')}:{quote(secret, safe='')}"
        return {'authorization': 'Basic ' + base64.b64encode(pair.encode()).decode()}
    if secret and method == POST:
        form['client_secret'] = secret
    return {}


def issued(response: httpx.Response | None) -> OAuthToken | None:
    if response is None or response.status_code != OK:
        return None
    try:
        found = OAuthToken.model_validate_json(response.content)
    except ValidationError:
        return None
    return found if found.access_token else None


def refused(response: httpx.Response | None) -> bool:
    return response is not None and REFUSED_FROM <= response.status_code < REFUSED_TO


def retry_after(response: httpx.Response, now: float) -> float:
    given = response.headers.get('retry-after', '').strip()
    try:
        wait = float(given) if SECONDS.fullmatch(given) else seconds(email.utils.parsedate_to_datetime(given)) - now
    except (TypeError, ValueError, IndexError, OverflowError):
        wait = 0.0
    return min(max(float(RETRY_AFTER_S), wait), float(RETRY_AFTER_MAX_S))


class McpOAuth:
    def __init__(self, db, sealer: Callable[[], Sealer], base_url: Callable[[], str], catalog, note: Callable[[str, str], None] | None = None,
                 http: Callable[[str], Callable[[], httpx.AsyncClient]] | None = None, local: Callable[[], bool] | None = None, clock: Callable[[], float] = time.time):
        self.db = db
        self.col = db[AUTH]
        self.states = db[STATES]
        self.sealer = sealer
        self.base_url = base_url
        self.catalog = catalog
        self.note = note or (lambda by, text: None)
        self.http = http or (lambda ca: partial(client_for, ca))
        self.local = local or (lambda: False)
        self.clock = clock
        self._locks: dict[str, threading.Lock] = {}
        self._guard = threading.Lock()

    def ensure_indexes(self) -> None:
        self.states.create_index('expires', expireAfterSeconds=0)
        self.states.create_index([('instance', ASCENDING)])

    def redirect_uri(self) -> str:
        base = (self.base_url() or '').rstrip('/')
        if not usable_base(base):
            raise FlowError(CONFLICT, msg('mcptools.oauth.base_url'))
        return base + CALLBACK_PATH

    def landing(self, name: str | None) -> str:
        base = (self.base_url() or '').rstrip('/')
        return base + LANDING + f"/mcp/{quote(name, safe='')}" if name else base + EXPIRED_LANDING

    def state(self, name: str) -> State:
        found = (self._doc(name) or {}).get('state')
        return found if found in SHOWN_STATES else 'none'

    def view(self, name: str) -> dict:
        doc = self._doc(name) or {}
        state = doc.get('state') if doc.get('state') in SHOWN_STATES else 'none'
        return {'mode': 'oauth', 'state': state, 'connectedBy': doc.get('connectedBy'), 'connectedAt': doc.get('connectedAt'), 'expiresAt': doc.get('expiresAt'),
                'last': doc.get('last')}

    def secrets(self, name: str) -> tuple[str, ...]:
        doc = self._doc(name)
        if doc is None:
            return ()
        found = self._tokens(doc)
        clients = (self._client(doc.get('client')), self._client((doc.get('registered') or {}).get('client')))
        values = (found.get('access_token'), found.get('refresh_token'), *(c.client_secret for c in clients if c is not None))
        return tuple(dict.fromkeys(v for v in values if isinstance(v, str) and v))

    def park(self, name: str, reason: str) -> None:
        if reason not in PARK_REASONS:
            raise ValueError('a park reason is one of the fixed reasons')
        doc = self._doc(name)
        if doc is not None and doc.get('state') == CONNECTED:
            self._park(name, key_of(doc), reason)

    def moved(self, name: str, url: str) -> bool:
        doc = self._doc(name)
        return doc is not None and doc.get('state') == CONNECTED and resource_url_from_server_url(url) != doc.get('resource')

    def start(self, name: str, instance: Mapping[str, str], allow_local_http: bool, by: str) -> str:
        if (instance.get('auth') or 'none') != 'oauth':
            raise NotOAuth(msg('mcptools.oauth.not_oauth'))
        redirect = self.redirect_uri()
        url = instance.get('url') or ''
        registered = (self._doc(name) or {}).get('registered') or {}
        kept = self._client(registered.get('client')) if registered.get('redirectUri') == redirect else None
        try:
            guard_url(url, allow_local_http)
            with within(START_DEADLINE_S):
                found = self._run(self._discover(url, instance, redirect, allow_local_http, kept, registered.get('issuer')))
        except FlowError:
            raise
        except Exception as e:
            text = failure_text(e)
            raise FlowError(BAD_GATEWAY, text) from None
        sealer = self.sealer()
        client = sealer.seal(found.client.model_dump_json(exclude_none=True))
        keep = {'registered': {'client': client, 'issuer': found.issuer, 'redirectUri': redirect}} if found.kind == DYNAMIC else {}
        doc = self.col.find_one_and_update({'_id': name}, {'$set': keep, '$setOnInsert': {'rev': 0, 'epoch': secrets.token_hex(EPOCH_BYTES)}} if keep else
                                           {'$setOnInsert': {'rev': 0, 'epoch': secrets.token_hex(EPOCH_BYTES)}}, upsert=True, return_document=ReturnDocument.AFTER)
        flow = {'issuer': found.issuer, 'authorizationEndpoint': found.authorization, 'tokenEndpoint': found.token, 'resource': resource_url_from_server_url(url),
                'audience': found.audience, 'redirectUri': redirect, 'client': client, 'clientKind': found.kind, 'ca': instance.get('ca') or ''}
        state = secrets.token_urlsafe(32)
        verifier, challenge = pkce_pair()
        self.states.insert_one({'_id': digest(state), 'instance': name, 'verifier': sealer.seal(verifier), 'by': by, 'expires': stamp(self.clock() + STATE_TTL_S),
                                'epoch': doc.get('epoch'), **flow})
        self._prune(name)
        self.note(by, msg('mcptools.oauth.started', by=by, instance=name))
        params = [('response_type', 'code'), ('client_id', found.client.client_id or ''), ('redirect_uri', redirect), ('state', state), ('code_challenge', challenge),
                  ('code_challenge_method', S256), ('resource', found.audience)]
        scope = instance.get('oauth-scopes') or ''
        preset = presets.get(instance.get('preset'))
        return with_query(found.authorization, params + ([('scope', scope)] if scope else []) + list(preset.auth_params if preset is not None else ()))

    def finish(self, code: str | None, state: str | None, error: str | None, by: str) -> tuple[str | None, str]:
        found = self.states.find_one_and_delete({'_id': digest(state)}) if state else None
        if found is None:
            return None, EXPIRED
        name = str(found.get('instance') or '')
        if seconds(found.get('expires')) <= self.clock():
            return name, self._ended(name, EXPIRED, found.get('epoch'))
        if found.get('by') != by:
            return name, self._ended(name, FAILED, found.get('epoch'))
        if error is not None:
            return name, self._ended(name, DENIED, found.get('epoch'))
        verifier = self._open(found.get('verifier'))
        info = self._client(found.get('client'))
        if not code or verifier is None or info is None or not found.get('tokenEndpoint') or not found.get('epoch'):
            return name, self._ended(name, FAILED, found.get('epoch'))
        try:
            token = issued(self._run(self._exchange(found, info, code, verifier, self.local())))
        except Exception as e:
            log.warning('MCP sign-in code exchange failed: %s', kind(e))
            token = None
        if token is None:
            return name, self._ended(name, FAILED, found.get('epoch'), msg('mcptools.oauth.exchange'))
        now = self.clock()
        expires = now + token.expires_in if token.expires_in else None
        stored = self.col.update_one({'_id': name, 'epoch': found['epoch']},
                                     {'$set': {**{k: found.get(k) for k in LIVE}, 'tokens': self._sealed(token, token.refresh_token or '', expires),
                                               'expiresAt': iso(expires) if expires else None, 'state': CONNECTED, 'connectedBy': by, 'connectedAt': iso(now),
                                               'last': self._result(CONNECTED, now)},
                                      '$unset': {'retryAt': '', 'refreshing': ''}, '$inc': {'rev': 1}})
        if not stored.modified_count:
            return name, FAILED
        self.catalog.bump_account(name)
        self.note(by, msg('mcptools.oauth.connected', by=by, instance=name))
        return name, CONNECTED

    def token(self, name: str, instance: Mapping[str, str]) -> str:
        doc = self._doc(name)
        held = self._held(name, doc, instance)
        now = self.clock()
        if not held.due(now):
            return held.access
        if not held.refresh:
            if held.expired(now):
                self._park(name, held.key, 'expired')
                raise Reconnect()
            return held.access
        if self._waiting(doc, now):
            return self._wait_out(held, now, False)
        return self._refresh(name, instance, held.key, False)

    def after_unauthorized(self, name: str, instance: Mapping[str, str]) -> str:
        doc = self._doc(name)
        held = self._held(name, doc, instance)
        if self._waiting(doc, self.clock()):
            return self._wait_out(held, self.clock(), True)
        return self._refresh(name, instance, held.key, True)

    def disconnect(self, name: str, by: str) -> None:
        self.forget(name)
        self.catalog.bump_account(name)
        self.note(by, msg('mcptools.oauth.disconnected', by=by, instance=name))

    def forget(self, name: str) -> None:
        self.col.delete_one({'_id': name})
        self.states.delete_many({'instance': name})

    def _doc(self, name: str) -> dict | None:
        return self.col.find_one({'_id': name})

    def _lock(self, name: str) -> threading.Lock:
        with self._guard:
            return self._locks.setdefault(name, threading.Lock())

    def _open(self, sealed: object) -> str | None:
        plain = self.sealer().open(sealed)
        if plain is not None or not isinstance(sealed, str):
            return plain
        key = stored_key(self.db)
        return Sealer(key).open(sealed) if key else None

    def _client(self, sealed: object) -> OAuthClientInformationFull | None:
        plain = self._open(sealed)
        if plain is None:
            return None
        try:
            return OAuthClientInformationFull.model_validate_json(plain)
        except ValidationError:
            return None

    def _tokens(self, doc: dict) -> dict:
        plain = self._open(doc.get('tokens'))
        try:
            found = json.loads(plain) if plain else {}
        except ValueError:
            return {}
        return found if isinstance(found, dict) else {}

    def _held(self, name: str, doc: dict | None, instance: Mapping[str, str]) -> Held:
        if doc is None or doc.get('state') != CONNECTED:
            raise Reconnect()
        if resource_url_from_server_url(instance.get('url') or '') != doc.get('resource'):
            raise Moved()
        key = key_of(doc)
        found = self._tokens(doc)
        if not isinstance(found.get('access_token'), str) or not found['access_token']:
            self._park(name, key, 'unreadable')
            raise Reconnect()
        expires = parse_iso(doc['expiresAt']) if doc.get('expiresAt') else None
        return Held(found['access_token'], found.get('refresh_token') or '', expires, key['rev'], key['resource'], key['epoch'])

    def _waiting(self, doc: dict | None, now: float) -> bool:
        retry = (doc or {}).get('retryAt')
        return bool(retry) and now < parse_iso(retry)

    @staticmethod
    def _wait_out(held: Held, now: float, force: bool) -> str:
        if force or held.expired(now):
            raise McpFailure(msg('mcptools.oauth.refresh_failed'))
        return held.access

    def _refresh(self, name: str, instance: Mapping[str, str], seen: dict, force: bool) -> str:
        lock = self._lock(name)
        if not lock.acquire(timeout=left(OAUTH_DEADLINE_S)):
            raise McpFailure(msg('mcptools.oauth.refresh_failed'))
        try:
            doc = self._doc(name)
            held = self._held(name, doc, instance)
            now = self.clock()
            if held.key != seen and (force or not held.due(now)):
                return held.access
            if self._waiting(doc, now):
                return self._wait_out(held, now, force)
            if not held.refresh:
                self._park(name, held.key, 'expired')
                raise Reconnect()
            return self._renew(name, doc, held, instance, force)
        finally:
            lock.release()

    def _renewing_client(self, name: str, doc: dict, held: Held, instance: Mapping[str, str]) -> OAuthClientInformationFull:
        info = self._client(doc.get('client'))
        reason = 'unreadable' if info is None else ''
        if info is not None and doc.get('clientKind') == REGISTERED and (instance.get('oauth-client-id') or '') != info.client_id:
            reason = 'client_changed'
        if reason:
            self._park(name, held.key, reason)
            raise Reconnect()
        if doc.get('clientKind') != REGISTERED:
            return info
        secret = instance.get('oauth-client-secret') or ''
        method = NONE if not secret else info.token_endpoint_auth_method if info.token_endpoint_auth_method in (BASIC, POST) else BASIC
        return info.model_copy(update={'client_secret': secret or None, 'token_endpoint_auth_method': method})

    def _renew(self, name: str, doc: dict, held: Held, instance: Mapping[str, str], force: bool = False) -> str:
        info = self._renewing_client(name, doc, held, instance)
        endpoint = doc.get('tokenEndpoint') or ''
        form = {'grant_type': 'refresh_token', 'refresh_token': held.refresh, 'resource': doc.get('audience') or doc.get('resource') or ''}
        now = self.clock()
        claimed = self.col.update_one({'_id': name, **held.key, '$or': [{'refreshing': {'$exists': False}}, {'refreshing': {'$lte': now}}]},
                                      {'$set': {'refreshing': now + OAUTH_DEADLINE_S}})
        if not claimed.modified_count:
            return self._await_peer(name, held, instance, force)
        try:
            guard_url(endpoint, self.local(), discovered=True)
            response = self._run(self._post(instance.get('ca') or '', endpoint, form, info, left(OAUTH_DEADLINE_S)))
        except Exception as e:
            log.warning('MCP sign-in refresh failed: %s', kind(e))
            response = None
        now = self.clock()
        if response is not None and response.status_code in TRANSIENT:
            return self._later(name, held, instance, now + retry_after(response, now), force)
        if refused(response):
            return self._refused(name, held, instance, response)
        token = issued(response)
        if token is None:
            return self._later(name, held, instance, now + RETRY_AFTER_S, force)
        expires = now + token.expires_in if token.expires_in else None
        stored = self.col.update_one({'_id': name, **held.key}, {'$set': {'tokens': self._sealed(token, token.refresh_token or held.refresh, expires),
                                                                          'expiresAt': iso(expires) if expires else None},
                                                                 '$unset': {'retryAt': '', 'refreshing': ''}, '$inc': {'rev': 1}})
        if stored.modified_count:
            return token.access_token
        return self._held(name, self._doc(name), instance).access

    def _await_peer(self, name: str, held: Held, instance: Mapping[str, str], force: bool) -> str:
        end = time.monotonic() + left(OAUTH_DEADLINE_S)
        while time.monotonic() < end:
            time.sleep(PEER_POLL_S)
            doc = self._doc(name)
            if doc is None or key_of(doc) != held.key:
                return self._held(name, doc, instance).access
            if not isinstance(doc.get('refreshing'), (int, float)) or doc['refreshing'] <= self.clock():
                break
        if not force and not held.expired(self.clock()):
            return held.access
        raise McpFailure(msg('mcptools.oauth.refresh_failed'))

    def _later(self, name: str, held: Held, instance: Mapping[str, str], at: float, force: bool = False) -> str:
        marked = self.col.update_one({'_id': name, **held.key}, {'$set': {'retryAt': iso(at)}, '$unset': {'refreshing': ''}})
        if not marked.matched_count:
            return self._held(name, self._doc(name), instance).access
        if not force and not held.expired(self.clock()):
            return held.access
        raise McpFailure(msg('mcptools.oauth.refresh_failed'))

    def _refused(self, name: str, held: Held, instance: Mapping[str, str], response: httpx.Response) -> str:
        again = self._doc(name)
        if again is not None and key_of(again) != held.key and again.get('state') == CONNECTED:
            return self._held(name, again, instance).access
        hidden = secrets_of(instance, (held.access, held.refresh, *self.secrets(name)))
        self._park(name, held.key, 'lost')
        missing = refused_scope(response, secrets=hidden)
        if missing is not None:
            raise NoScope(lacks(missing, msg('mcptools.credential.oauth')))
        raise Reconnect()

    def _park(self, name: str, key: dict, reason: str) -> None:
        now = self.clock()
        parked = self.col.update_one({'_id': name, **key, 'state': CONNECTED},
                                     {'$set': {'state': RECONNECT, 'last': self._result(RECONNECT, now, msg('mcptools.oauth.reconnect', instance=name))},
                                      '$unset': {'tokens': '', 'expiresAt': '', 'retryAt': '', 'refreshing': ''}, '$inc': {'rev': 1}})
        if parked.modified_count:
            self.note(SYSTEM, msg(f'mcptools.oauth.{reason}', instance=name))

    def _ended(self, name: str, result: str, epoch: object, text: str | None = None) -> str:
        self.col.update_one({'_id': name, 'epoch': epoch}, {'$set': {'last': self._result(result, self.clock(), text)}})
        return result

    def _prune(self, name: str) -> None:
        waiting = [d['_id'] for d in self.states.find({'instance': name}, {'_id': 1}).sort('expires', DESCENDING)]
        if len(waiting) > STATES_PER_INSTANCE:
            self.states.delete_many({'_id': {'$in': waiting[STATES_PER_INSTANCE:]}})

    def _sealed(self, token: OAuthToken, refresh: str, expires: float | None) -> str:
        return self.sealer().seal(json.dumps({'access_token': token.access_token, 'refresh_token': refresh or None, 'expires_at': expires, 'scope': token.scope,
                                              'token_type': token.token_type}))

    @staticmethod
    def _result(result: str, now: float, text: str | None = None) -> dict:
        return {'result': result, 'text': text or msg(RESULT_TEXTS[result]), 'at': iso(now)}

    @staticmethod
    def _run(work: Coroutine):
        if on_loop():
            work.close()
            raise RuntimeError('the MCP sign-in blocks its thread, so it never runs on an event loop')
        return asyncio.run(work)

    @staticmethod
    def _guard_candidate(candidate: str, url: str, local: bool) -> None:
        guard_url(candidate, local, discovered=not presets.same_origin(candidate, url))

    async def _discover(self, url: str, instance: Mapping[str, str], redirect: str, local: bool, kept: OAuthClientInformationFull | None, kept_issuer: str | None) -> Found:
        async with self.http(instance.get('ca') or '')() as client:
            resource = await self._resource_metadata(client, url, local)
            if not check_resource_allowed(resource_url_from_server_url(url), str(resource.resource)):
                raise FlowError(BAD_GATEWAY, msg('mcptools.oauth.resource'))
            issuer = str(resource.authorization_servers[0])
            metadata = await self._server_metadata(client, url, issuer, local)
            for endpoint in (metadata.authorization_endpoint, metadata.token_endpoint, metadata.registration_endpoint):
                if endpoint is not None:
                    guard_url(str(endpoint), local, discovered=True)
            info, made = await self._registered(client, instance, metadata, redirect, kept, kept_issuer)
        return Found(str(metadata.issuer), str(metadata.authorization_endpoint), str(metadata.token_endpoint), str(resource.resource), info, made)

    async def _get(self, client: httpx.AsyncClient, target: str) -> httpx.Response:
        with anyio.fail_after(left(OAUTH_DEADLINE_S)):
            return await client.get(target, headers={MCP_PROTOCOL_VERSION: LATEST_PROTOCOL_VERSION})

    async def _resource_metadata(self, client: httpx.AsyncClient, url: str, local: bool) -> ProtectedResourceMetadata:
        with anyio.fail_after(left(OAUTH_DEADLINE_S)):
            async with client.stream('POST', url, json=INITIALIZE, headers=PROBE_HEADERS) as probe:
                named = extract_resource_metadata_from_www_auth(probe)
        for candidate in build_protected_resource_metadata_discovery_urls(named, url)[:PRM_TRIES]:
            self._guard_candidate(candidate, url, local)
            response = await self._get(client, candidate)
            if response.status_code != OK:
                continue
            try:
                return ProtectedResourceMetadata.model_validate_json(response.content)
            except ValidationError:
                continue
        raise FlowError(BAD_GATEWAY, msg('mcptools.oauth.discovery'))

    async def _server_metadata(self, client: httpx.AsyncClient, url: str, issuer: str, local: bool) -> OAuthMetadata:
        found = None
        for candidate in build_oauth_authorization_server_metadata_discovery_urls(issuer, url)[:ASM_TRIES]:
            self._guard_candidate(candidate, url, local)
            response = await self._get(client, candidate)
            if response.status_code != OK:
                continue
            try:
                found = OAuthMetadata.model_validate_json(response.content)
            except ValidationError:
                continue
            break
        if found is None:
            raise FlowError(BAD_GATEWAY, msg('mcptools.oauth.discovery'))
        try:
            validate_metadata_issuer(found, issuer)
        except OAuthFlowError:
            raise FlowError(BAD_GATEWAY, msg('mcptools.oauth.issuer')) from None
        if S256 not in (found.code_challenge_methods_supported or []):
            raise FlowError(BAD_GATEWAY, msg('mcptools.oauth.no_pkce'))
        return found

    async def _registered(self, client: httpx.AsyncClient, instance: Mapping[str, str], metadata: OAuthMetadata, redirect: str,
                          kept: OAuthClientInformationFull | None, kept_issuer: str | None) -> tuple[OAuthClientInformationFull, str]:
        client_id = instance.get('oauth-client-id') or ''
        if client_id:
            secret = instance.get('oauth-client-secret') or ''
            return OAuthClientInformationFull(client_id=client_id, client_secret=secret or None, token_endpoint_auth_method=method_for(metadata, secret),
                                              redirect_uris=[redirect]), REGISTERED
        if metadata.registration_endpoint is None:
            raise FlowError(CONFLICT, msg('mcptools.oauth.needs_client'))
        if kept is not None and kept_issuer == str(metadata.issuer):
            return kept, DYNAMIC
        wanted = OAuthClientMetadata(redirect_uris=[redirect], grant_types=GRANTS, response_types=['code'], token_endpoint_auth_method=NONE,
                                     scope=instance.get('oauth-scopes') or None, client_name=msg('mcptools.oauth.client_name', host=urlsplit(redirect).hostname or ''))
        request = create_client_registration_request(metadata, wanted, str(metadata.issuer))
        request.headers['accept-encoding'] = 'identity'
        with anyio.fail_after(left(OAUTH_DEADLINE_S)):
            response = await client.send(request)
        try:
            info = await handle_registration_response(response)
        except OAuthRegistrationError:
            raise FlowError(BAD_GATEWAY, msg('mcptools.oauth.registration')) from None
        if not info.client_id:
            raise FlowError(BAD_GATEWAY, msg('mcptools.oauth.registration'))
        return info, DYNAMIC

    async def _exchange(self, flow: dict, info: OAuthClientInformationFull, code: str, verifier: str, local: bool) -> httpx.Response:
        endpoint = flow['tokenEndpoint']
        guard_url(endpoint, local, discovered=True)
        form = {'grant_type': 'authorization_code', 'code': code, 'redirect_uri': flow.get('redirectUri') or '', 'code_verifier': verifier,
                'resource': flow.get('audience') or flow.get('resource') or ''}
        return await self._post(flow.get('ca') or '', endpoint, form, info, OAUTH_DEADLINE_S)

    async def _post(self, ca: str, endpoint: str, form: dict[str, str], info: OAuthClientInformationFull, deadline: float) -> httpx.Response:
        headers = JSON_ACCEPT | client_auth(info, form)
        async with self.http(ca)() as client:
            with anyio.fail_after(deadline):
                return await client.post(endpoint, data=form, headers=headers)
