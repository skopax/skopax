import base64
import hashlib
import hmac
import logging
import re
import secrets
import time
from collections.abc import Callable

from pymongo import ASCENDING, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError

from skopax import clock
from skopax.errkind import Authored, kind as error_kind
from skopax.messages import msg

log = logging.getLogger('skopax.users')

ROLES = ('viewer', 'operator', 'admin')
SOURCES = ('local', 'sso')
ROLE_SOURCES = ('resolver', 'claim', 'admin', 'default')
IDP_SOURCES = ('resolver', 'claim')
ROLE_FIELDS = ('role', 'roleFrom', 'adminRole')
NAME_SOURCES = ('default', 'idp', 'admin', 'self')
IDP_NAMES = ('default', 'idp')
RESOLVED_AT = 'resolvedAtTs'
LOCAL_AFTER = 'localAfterTs'
SSO_AFTER = 'ssoAfterTs'

SCRYPT_N = 2 ** 14
SCRYPT_R = 8
SCRYPT_P = 1
SCRYPT_LEN = 32
SALT_LEN = 16
PASSWORD_MIN = 12
PASSWORD_MAX = 256
NAME_MAX = 120
LINK_PENDING_S = 7 * 24 * 3600
EMAIL = re.compile(r'^[^@\s]{1,64}@[^@\s]{1,189}$')


class UserExists(Exception):
    pass


class InvalidUser(Authored, ValueError):
    pass


class LastAdmin(Exception):
    pass


class IdpManaged(Exception):
    pass


class NoPasswordAdmin(Exception):
    pass


class ChangeReverted(Exception):
    pass


class ChangeUnsure(Exception):
    pass


class Unopened(Exception):
    pass


def admin_set_of(doc: dict | None) -> str | None:
    doc = doc or {}
    if doc.get('adminRole') in ROLES:
        return doc['adminRole']
    if doc.get('roleFrom') in (None, 'admin') and doc.get('role') in ROLES:
        return doc['role']
    return None


def name_from(doc: dict | None) -> str:
    doc = doc or {}
    if doc.get('nameFrom') in NAME_SOURCES:
        return doc['nameFrom']
    name = str(doc.get('name') or '').strip()
    if name.lower() == str(doc.get('email') or '').strip().lower():
        return 'default'
    if doc.get('source') == 'sso':
        return 'idp'
    return 'default' if name == msg('setup.admin_name') else 'admin'


def idp_source(name: str, email: str) -> str:
    return 'default' if name.strip().lower() == str(email or '').strip().lower() else 'idp'


def leaves_admin(fields: dict) -> bool:
    return ('role' in fields and fields['role'] != 'admin') or fields.get('disabled') is True


def b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip('=')


def unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + '=' * (-len(text) % 4))


def derive(password: str, salt: bytes, n: int = SCRYPT_N, r: int = SCRYPT_R, p: int = SCRYPT_P) -> bytes:
    return hashlib.scrypt(password.encode(), salt=salt, n=n, r=r, p=p, dklen=SCRYPT_LEN, maxmem=128 * n * r * 2 + 2 ** 20)


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(SALT_LEN)
    return f'scrypt${SCRYPT_N}${SCRYPT_R}${SCRYPT_P}${b64(salt)}${b64(derive(password, salt))}'


def scrypt_parts(stored: str) -> tuple[bytes, bytes] | None:
    try:
        kind, n, r, p, salt, digest = stored.split('$')
        if kind != 'scrypt' or (int(n), int(r), int(p)) != (SCRYPT_N, SCRYPT_R, SCRYPT_P):
            return None
        return unb64(salt), unb64(digest)
    except (ValueError, TypeError, AttributeError):
        return None


def check_password(password: str, stored: str) -> bool:
    parts = scrypt_parts(stored)
    salt, expected = parts or scrypt_parts(DUMMY_HASH)
    try:
        actual = derive(password, salt)
    except (ValueError, TypeError, AttributeError):
        return False
    return parts is not None and hmac.compare_digest(actual, expected)


DUMMY_HASH = hash_password(secrets.token_urlsafe(24))


MCP_USE_EVERY_S = 60


def stamp() -> str:
    return clock.stamp(time.time())


def clean_email(email: str) -> str:
    text = str(email or '').strip()
    if not EMAIL.match(text):
        raise InvalidUser(msg('user.email_invalid'))
    return text.lower()


def unseen(ch: str) -> bool:
    code = ord(ch)
    return code < 32 or 127 <= code <= 159 or 0x202A <= code <= 0x202E or 0x2066 <= code <= 0x2069


def clean_name(name: str) -> str:
    text = ''.join(ch for ch in str(name or '') if not unseen(ch)).strip()
    if not 1 <= len(text) <= NAME_MAX:
        raise InvalidUser(msg('user.name_length', max=NAME_MAX))
    return text


def clean_role(role: str) -> str:
    if role not in ROLES:
        raise InvalidUser(msg('user.role_invalid'))
    return role


def clean_password(password: str) -> str:
    if not isinstance(password, str) or not PASSWORD_MIN <= len(password) <= PASSWORD_MAX:
        raise InvalidUser(msg('user.password_length', min=PASSWORD_MIN, max=PASSWORD_MAX))
    try:
        password.encode()
    except UnicodeEncodeError:
        raise InvalidUser(msg('user.password_text')) from None
    return password


def public(doc: dict | None) -> dict | None:
    if doc is None:
        return None
    return {k: v for k, v in doc.items() if k != 'pw'}


def link_state(held: dict, now: float) -> str | None:
    state = held.get('state')
    since = held.get('atTs')
    if state == 'pending' and (not isinstance(since, (int, float)) or isinstance(since, bool) or since < now - LINK_PENDING_S):
        return 'expired'
    return state


def mcp_mark(doc: dict, used: dict | None) -> dict | None:
    if not used or not used.get('at'):
        return None
    login = doc.get('lastLogin')
    unseen = not login or login == used.get('seed')
    return {'at': used['at'], 'only': (None if unseen else False) if used.get('undecided') else unseen}


def admin_row(doc: dict | None, now: float) -> dict | None:
    if doc is None:
        return None
    held = doc.get('ssoLink')
    state = link_state(held, now) if isinstance(held, dict) else None
    waiting = state in ('pending', 'expired')
    allowed = held.get('at') if waiting and isinstance(held.get('at'), str) else None
    since = held.get('atTs') if waiting else None
    expires = clock.stamp(since + LINK_PENDING_S) if isinstance(since, (int, float)) and not isinstance(since, bool) else None
    shown = {'state': state, 'boundAt': held.get('boundAt'), 'allowedAt': allowed, 'expiresAt': expires} if isinstance(held, dict) else None
    return {k: v for k, v in doc.items() if k not in ('pw', 'ssoLink')} | {'ssoLink': shown, 'nameFrom': name_from(doc)}


def password_admin(doc: dict | None) -> bool:
    doc = doc or {}
    return doc.get('role') == 'admin' and not doc.get('disabled') and doc.get('source') != 'sso' and bool(doc.get('pw'))


def number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def cut_admits(doc: dict, src, login) -> bool:
    if src != 'sso' and doc.get('source') == 'sso':
        return False
    if src == 'sso':
        cut = doc.get(SSO_AFTER)
        return not number(cut) or (number(login) and login >= cut)
    cut = doc.get(LOCAL_AFTER)
    return not number(cut) or (number(login) and login > cut)


def link_admits(doc: dict, login) -> bool:
    held = doc.get('ssoLink')
    if not isinstance(held, dict) or held.get('state') != 'bound':
        return False
    since = held.get('boundAtTs')
    numbers = all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in (since, login))
    return numbers and login >= since


class Users:
    def __init__(self, db, now: Callable[[], float] = time.time):
        self.col = db['users']
        self.mcp_use = db['mcp_use']
        self.activity = db['activity']
        self.actions = db['actions']
        self.now = now

    def ensure_indexes(self) -> None:
        self.col.create_index([('role', ASCENDING), ('disabled', ASCENDING)])

    def count(self, active_only: bool = False) -> int:
        return self.col.count_documents({'disabled': False} if active_only else {})

    def get(self, email: str) -> dict | None:
        return public(self.col.find_one({'_id': str(email or '').strip().lower()}))

    def pending_link(self, by: str) -> dict:
        at = self.now()
        return {'state': 'pending', 'by': by, 'at': clock.stamp(at), 'atTs': at}

    def create(self, email: str, name: str, role: str, password: str | None = None, source: str = 'local', link_by: str | None = None, name_from: str = 'admin') -> dict:
        key = clean_email(email)
        if source not in SOURCES:
            raise InvalidUser(msg('user.source_invalid'))
        if name_from not in NAME_SOURCES:
            raise ValueError(f'unknown name source {name_from!r}')
        doc = {'_id': key, 'email': key, 'name': clean_name(name), 'nameFrom': name_from, 'role': clean_role(role), 'source': source,
               'disabled': False, 'createdAt': stamp(), 'lastLogin': None}
        if password is not None:
            doc['pw'] = hash_password(clean_password(password))
        if link_by is not None:
            if source != 'local':
                raise InvalidUser(msg('access.sso_link_local_only'))
            doc['ssoLink'] = self.pending_link(link_by)
        return self.insert(doc)

    def add_sso(self, email: str, name: str, role: str, name_from: str = 'admin') -> dict:
        key = clean_email(email)
        chosen = clean_role(role)
        if name_from not in NAME_SOURCES:
            raise ValueError(f'unknown name source {name_from!r}')
        return self.insert({'_id': key, 'email': key, 'name': clean_name(name), 'nameFrom': name_from, 'role': chosen, 'adminRole': chosen, 'roleFrom': 'admin', 'source': 'sso',
                            'disabled': False, 'createdAt': stamp(), 'lastLogin': None, 'opened': False})

    def make_local(self, email: str, password: str, by: str, fields: dict | None = None, link: bool = True) -> dict | None:
        key = str(email or '').strip().lower()
        held = self.col.find_one({'_id': key, 'source': 'sso'}, dict.fromkeys(ROLE_FIELDS, 1))
        if held is None:
            return None
        owned = {'roleFrom': 'admin', 'adminRole': held['role']} if held.get('roleFrom') in IDP_SOURCES and held.get('role') in ROLES else {}
        change = {'source': 'local', 'pw': hash_password(clean_password(password))} | owned | ({'ssoLink': self.pending_link(by)} if link else {}) | (fields or {})
        unchanged = {'source': 'sso'} | {name: held[name] if name in held else {'$exists': False} for name in ROLE_FIELDS}
        before = self.guarded_update(key, change, ('opened',), only=unchanged)
        return self.get(key) if before is not None else None

    def make_sso_only(self, email: str) -> dict | None:
        key = str(email or '').strip().lower()
        held = self.col.find_one({'_id': key, 'source': 'local'})
        if held is None:
            return None
        if password_admin(held) and not self.other_password_admin(key):
            raise NoPasswordAdmin(key)
        now = self.now()
        link = held.get('ssoLink')
        bound = isinstance(link, dict) and link.get('state') == 'bound' and number(link.get('boundAtTs'))
        fields = {'source': 'sso', 'opened': False, LOCAL_AFTER: now, SSO_AFTER: link['boundAtTs'] if bound else now}
        if held.get('role') in ROLES:
            fields |= {'roleFrom': 'admin', 'adminRole': held['role']}
        unset = ('pw', 'ssoLink')
        same = {name: held[name] if name in held else {'$exists': False} for name in ('ssoLink', 'role', 'disabled')}
        before = self.col.find_one_and_update({'_id': key, 'source': 'local', **same}, {'$set': fields, '$unset': {name: '' for name in unset}})
        if before is None:
            return None
        touched = set(fields) | set(unset)
        try:
            lost = password_admin(before) and not self.has_password_admin()
            made = None if lost else self.get(key)
        except Exception as e:
            self.put_back(key, before, touched, now)
            raise ChangeReverted(key) from e
        if lost:
            self.put_back(key, before, touched, now)
            raise NoPasswordAdmin(key)
        return made

    def is_last_password_admin(self, email: str) -> bool:
        key = str(email or '').strip().lower()
        return password_admin(self.col.find_one({'_id': key})) and self.active_admins() > 1 and not self.other_password_admin(key)

    def other_password_admin(self, email: str) -> bool:
        return self.col.find_one({'_id': {'$ne': email}, 'role': 'admin', 'disabled': False, 'source': {'$ne': 'sso'}, 'pw': {'$exists': True, '$nin': [None, '']}}, {'_id': 1}) is not None

    def put_back(self, key: str, before: dict, touched: set, now: float) -> None:
        try:
            self.restore(key, before, touched, now)
        except Exception as e:
            log.error('%s may now sign in with SSO only: the change could not be put back after a failed check: %s', key, error_kind(e))
            raise ChangeUnsure(key) from e

    def restore(self, key: str, before: dict, touched: set, now: float) -> None:
        back = {name: before[name] for name in touched if name in before}
        gone = {name: '' for name in touched if name not in before}
        res = self.col.update_one({'_id': key, 'source': 'sso', LOCAL_AFTER: now}, ({'$set': back} if back else {}) | ({'$unset': gone} if gone else {}))
        if not res.matched_count:
            raise ChangeUnsure(key)

    def insert(self, doc: dict) -> dict:
        key = doc['_id']
        try:
            self.col.insert_one(doc)
        except DuplicateKeyError as e:
            raise UserExists(key) from e
        return public(doc)

    def authenticate(self, email: str, password: str) -> dict | None:
        doc = self.col.find_one({'_id': str(email or '').strip().lower()})
        stored = (doc or {}).get('pw')
        usable = bool(doc) and bool(stored) and not doc.get('disabled') and doc.get('source') != 'sso'
        ok = check_password(password if isinstance(password, str) else '', stored if usable else DUMMY_HASH)
        return public(doc) if usable and ok else None

    def update(self, email: str, fields: dict, unset: tuple[str, ...] = ()) -> None:
        change = {'$set': fields} | ({'$unset': {name: '' for name in unset}} if unset else {})
        res = self.col.update_one({'_id': str(email or '').strip().lower()}, change)
        if not res.matched_count:
            raise InvalidUser(msg('access.unknown_user'))

    def guarded_update(self, email: str, fields: dict, unset: tuple[str, ...] = (), only: dict | None = None) -> dict | None:
        key = str(email or '').strip().lower()
        change = {'$set': fields} | ({'$unset': {name: '' for name in unset}} if unset else {})
        before = self.col.find_one_and_update({'_id': key, **(only or {})}, change)
        if before is None or not leaves_admin(fields) or before.get('role') != 'admin' or before.get('disabled'):
            return before
        empty = self.active_admins() == 0
        if empty or (password_admin(before) and not self.has_password_admin()):
            touched = set(fields) | set(unset)
            back = {name: before[name] for name in touched if name in before}
            gone = {name: '' for name in touched if name not in before}
            written = dict(fields) | {name: {'$exists': False} for name in unset}
            try:
                self.col.update_one({'_id': key, **written}, ({'$set': back} if back else {}) | ({'$unset': gone} if gone else {}))
            except PyMongoError as e:
                log.error('the last-admin check failed and the change to %s could not be restored, so no active admin may remain: %s', key, error_kind(e))
            raise LastAdmin(key) if empty else NoPasswordAdmin(key)
        return before

    def prepare_change(self, role: str | None, disabled: bool | None, password: str | None, stored: dict) -> tuple[dict, tuple[str, ...]]:
        fields = {}
        if role is not None:
            chosen = clean_role(role)
            if stored.get('roleFrom') in IDP_SOURCES:
                raise IdpManaged(stored.get('email'))
            fields |= {'role': chosen, 'adminRole': chosen, 'roleFrom': 'admin'}
        if disabled is not None:
            fields['disabled'] = bool(disabled)
        if password is not None:
            fields['pw'] = hash_password(clean_password(password))
        return fields, ()

    def set_role(self, email: str, role: str) -> None:
        chosen = clean_role(role)
        self.update(email, {'role': chosen, 'adminRole': chosen, 'roleFrom': 'admin'})

    def set_disabled(self, email: str, disabled: bool) -> None:
        self.update(email, {'disabled': bool(disabled)})

    def set_password(self, email: str, password: str) -> None:
        self.update(email, {'pw': hash_password(clean_password(password))})

    def list(self) -> list[dict]:
        return [public(d) for d in self.col.find().sort('_id', ASCENDING)]

    def upsert_sso(self, email: str, name: str, default_role: str, verified: bool = False) -> dict:
        key = clean_email(email)
        label = clean_name(name)
        role = clean_role(default_role)
        now = stamp()
        only = {'_id': key, 'source': 'sso', 'disabled': {'$ne': True}} | ({} if verified else {'opened': {'$ne': False}})
        seen = {'$set': {'lastLogin': now}, '$unset': {'opened': ''}}
        made = {'email': key, 'name': label, 'nameFrom': idp_source(label, key), 'role': role, 'roleFrom': 'default', 'disabled': False, 'createdAt': now}
        try:
            self.col.update_one(only, seen | {'$setOnInsert': made}, upsert=True)
        except DuplicateKeyError as e:
            found = self.get(key)
            if found is None or found.get('source') != 'sso':
                raise UserExists(key) from e
            if found.get('opened') is False and not verified:
                raise Unopened(key) from e
            if not found.get('disabled'):
                self.col.update_one(only, seen)
                self.idp_name(key, label)
                return self.get(key)
            return found
        self.idp_name(key, label)
        return self.get(key)

    def insert_sso(self, email: str, name: str, default_role: str) -> dict | None:
        key = clean_email(email)
        label = clean_name(name)
        now = stamp()
        self.col.update_one({'_id': key}, {'$setOnInsert': {'email': key, 'name': label, 'nameFrom': idp_source(label, key), 'role': clean_role(default_role), 'roleFrom': 'default', 'source': 'sso',
                                                             'disabled': False, 'createdAt': now, 'lastLogin': now}}, upsert=True)
        return self.get(key)

    def idp_name(self, email: str, name: str) -> bool:
        key = str(email or '').strip().lower()
        label = clean_name(name)
        held = self.col.find_one({'_id': key}, {'name': 1, 'nameFrom': 1, 'email': 1, 'disabled': 1, 'source': 1})
        if held is None or held.get('disabled') or name_from(held) not in IDP_NAMES:
            return False
        kept = str(held.get('name') or '').strip()
        if idp_source(label, key) == 'default' and kept != msg('setup.admin_name') and idp_source(kept, key) != 'default':
            return False
        unchanged = {'name': held.get('name'), 'nameFrom': held['nameFrom'] if 'nameFrom' in held else {'$exists': False}}
        res = self.col.update_one({'_id': key, 'disabled': {'$ne': True}, **unchanged}, {'$set': {'name': label, 'nameFrom': idp_source(label, key)}})
        return bool(res.modified_count)

    def rename(self, email: str, name: str, source: str) -> dict:
        if source not in ('admin', 'self'):
            raise ValueError(f'unknown name source {source!r}')
        key = str(email or '').strip().lower()
        after = self.col.find_one_and_update({'_id': key}, {'$set': {'name': clean_name(name), 'nameFrom': source}}, return_document=ReturnDocument.AFTER)
        if after is None:
            raise InvalidUser(msg('access.unknown_user'))
        return public(after)

    def open_sso(self, email: str) -> dict | None:
        key = str(email or '').strip().lower()
        return public(self.col.find_one_and_update({'_id': key, 'source': 'sso', 'disabled': {'$ne': True}, 'opened': False},
                                                   {'$set': {'lastLogin': stamp()}, '$unset': {'opened': ''}}, return_document=ReturnDocument.AFTER))

    def stamp_resolved(self, email: str, role: str, at: float) -> None:
        key = str(email or '').strip().lower()
        self.col.update_one({'_id': key, 'source': 'sso', 'disabled': {'$ne': True}, 'role': role, 'roleFrom': 'resolver'}, {'$set': {RESOLVED_AT: int(at)}})

    def set_sso_link(self, email: str, allow: bool, by: str) -> dict:
        key = str(email or '').strip().lower()
        if allow:
            only = {'ssoLink.state': {'$ne': 'bound'}}
            change = {'$set': {'ssoLink': self.pending_link(by)}}
        else:
            only, change = {}, {'$unset': {'ssoLink': ''}}
        after = self.col.find_one_and_update({'_id': key, 'source': 'local', **only}, change, return_document=ReturnDocument.AFTER)
        if after is not None:
            return public(after)
        found = self.get(key)
        if found is None:
            raise InvalidUser(msg('access.unknown_user'))
        if found.get('source') != 'local':
            raise InvalidUser(msg('access.sso_link_local_only'))
        return found

    def bind_sso(self, email: str, iss: str, sub: str, now: float) -> bool:
        key = str(email or '').strip().lower()
        bound = self.col.find_one_and_update(
            {'_id': key, 'source': 'local', 'disabled': {'$ne': True}, 'ssoLink.state': 'pending', 'ssoLink.atTs': {'$gte': now - LINK_PENDING_S}},
            {'$set': {'ssoLink.state': 'bound', 'ssoLink.iss': iss, 'ssoLink.sub': sub, 'ssoLink.boundAt': clock.stamp(now), 'ssoLink.boundAtTs': int(now)}})
        return bound is not None

    def linked_login(self, email: str, iss: str, sub: str) -> dict | None:
        key = str(email or '').strip().lower()
        return public(self.col.find_one_and_update(
            {'_id': key, 'source': 'local', 'disabled': {'$ne': True}, 'ssoLink.state': 'bound', 'ssoLink.iss': iss, 'ssoLink.sub': sub},
            {'$set': {'lastLogin': clock.stamp(self.now())}}, return_document=ReturnDocument.AFTER))

    def apply_sign_in_role(self, email: str, role: str, role_from: str, seen: dict | None = None, at: float | None = None) -> dict:
        key = clean_email(email)
        if role_from not in ROLE_SOURCES:
            raise ValueError(f'unknown role source {role_from!r}')
        fields = {'role': clean_role(role), 'roleFrom': role_from} | ({RESOLVED_AT: int(at)} if role_from == 'resolver' and at is not None else {})
        held = seen if seen is not None else self.col.find_one({'_id': key}, dict.fromkeys(ROLE_FIELDS, 1)) or {}
        if 'adminRole' not in held and admin_set_of(held) is not None:
            fields['adminRole'] = admin_set_of(held)
        unchanged = {'source': 'sso', 'disabled': {'$ne': True}} | {name: held[name] if name in held else {'$exists': False} for name in ROLE_FIELDS}
        try:
            if self.guarded_update(key, fields, only=unchanged) is None:
                log.info('the role of %s changed during the sign-in, so the newer role stands', key)
        except LastAdmin:
            log.warning(msg('access.last_admin_kept', email=key))
        return self.get(key)

    def touch_login(self, email: str) -> None:
        self.col.update_one({'_id': str(email or '').strip().lower()}, {'$set': {'lastLogin': stamp()}})

    def stamp_first_login(self, email: str) -> bool:
        done = self.col.update_one({'_id': str(email or '').strip().lower(), '$or': [{'lastLogin': None}, {'lastLogin': {'$exists': False}}]}, {'$set': {'lastLogin': clock.stamp(self.now())}})
        return done.modified_count > 0

    def page_evidence(self, email: str, name: str | None) -> bool:
        who = [w for w in (email, name) if w]
        page = {'requestedBy': {'$in': who}, 'via': {'$ne': 'mcp'}, 'source': {'$nin': ['mcp', 'slack']}}
        if self.actions.find_one(page, {'_id': 1}) is not None:
            return True
        refused = msg('access.sso_not_added', email=email)
        return self.activity.find_one({'kind': {'$in': ['access', 'admin']}, 'who': {'$in': who}, 'text': {'$ne': refused}}, {'_id': 1}) is not None

    def mcp_evidence(self, email: str, name: str | None) -> bool:
        who = [w for w in (email, name) if w]
        return self.actions.find_one({'requestedBy': {'$in': who}, '$or': [{'via': 'mcp'}, {'source': 'mcp'}]}, {'_id': 1}) is not None

    def first_seed(self, key: str, doc: dict, seeded: bool) -> dict:
        login = doc.get('lastLogin')
        if seeded:
            return {'seed': login, 'undecided': False}
        if not login or self.page_evidence(key, doc.get('name')):
            return {'seed': None, 'undecided': False}
        return {'seed': login, 'undecided': not self.mcp_evidence(key, doc.get('name'))}

    def record_mcp(self, email: str, seeded: bool = False) -> None:
        key = str(email or '').strip().lower()
        at = self.now()
        doc = self.col.find_one({'_id': key}, {'lastLogin': 1, 'name': 1}) or {}
        first = self.mcp_use.find_one({'_id': key}, {'_id': 1}) is None
        seed = self.first_seed(key, doc, seeded) if first or seeded else {}
        stale = {'$or': [{'at': {'$lt': clock.stamp(at - MCP_USE_EVERY_S)}}, {'at': {'$exists': False}}]}
        try:
            self.mcp_use.find_one_and_update({'_id': key, **stale}, {'$set': {'at': clock.stamp(at), **seed}}, upsert=True)
        except DuplicateKeyError:
            if seed:
                self.mcp_use.update_one({'_id': key, 'seed': None}, {'$set': seed})

    def mcp_uses(self) -> dict[str, dict]:
        return {d['_id']: d for d in self.mcp_use.find({'at': {'$exists': True}})}

    def mcp_summary(self, days: int) -> dict:
        since = clock.stamp(self.now() - days * 86400)
        recent = list(self.mcp_use.find({'at': {'$gte': since}}, sort=[('at', -1)]))
        who = None
        if recent:
            doc = self.col.find_one({'_id': recent[0]['_id']}, {'name': 1}) or {}
            who = doc.get('name') or recent[0]['_id']
        return {'people': len(recent), 'last': {'at': recent[0]['at'], 'by': who} if recent else None}

    def active_admins(self) -> int:
        return self.col.count_documents({'role': 'admin', 'disabled': False})

    def is_last_admin(self, email: str) -> bool:
        doc = self.col.find_one({'_id': str(email or '').strip().lower()}) or {}
        return doc.get('role') == 'admin' and not doc.get('disabled') and self.active_admins() <= 1

    def has_password_admin(self) -> bool:
        return self.col.find_one({'role': 'admin', 'disabled': False, 'source': {'$ne': 'sso'}, 'pw': {'$exists': True, '$nin': [None, '']}}, {'_id': 1}) is not None
