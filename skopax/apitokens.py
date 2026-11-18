import hashlib
import hmac
import logging
import re
import secrets
import time
from dataclasses import dataclass
from typing import Callable

from pymongo import ASCENDING
from pymongo.errors import DuplicateKeyError, PyMongoError

from skopax import clock
from skopax.errkind import kind as error_kind
from skopax.users import clean_role

log = logging.getLogger('skopax.apitokens')

PREFIX = 'skopax_'
NAME = re.compile(r'^[a-z0-9][a-z0-9._-]{0,79}$')
RESERVED = ('bootstrap',)
USED_EVERY_SECONDS = 60
ID_TRIES = 5
TOKEN = re.compile(r'^skopax_([0-9a-f]{8})_([A-Za-z0-9_-]{16,64})$')
HIDDEN = ('hash', 'usedAt')


class UnknownToken(LookupError):
    pass


@dataclass(frozen=True)
class Actor:
    name: str
    role: str
    creator: str
    via: str | None = None


def digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class TokenNameError(ValueError):
    def __init__(self, key: str):
        super().__init__(key)
        self.key = key


def clean_token_name(name: str) -> str:
    text = str(name or '').strip().lower()
    if not NAME.match(text):
        raise TokenNameError('access.token_name_invalid')
    if text in RESERVED:
        raise TokenNameError('access.token_name_taken')
    return text


def public(doc: dict) -> dict:
    return {('id' if k == '_id' else k): v for k, v in doc.items() if k not in HIDDEN}


DUMMY = digest(secrets.token_urlsafe(24))


class ApiTokens:
    def __init__(self, db, clock: Callable[[], float] = time.time):
        self.col = db['api_tokens']
        self.clock = clock

    def ensure_indexes(self) -> None:
        self.col.create_index([('createdAt', ASCENDING)])
        self.col.create_index([('name', ASCENDING)], unique=True)

    def stamp(self) -> str:
        return clock.stamp(self.clock())

    def create(self, name: str, role: str, by: str, via: str | None = None) -> tuple[str, dict]:
        label = clean_token_name(name)
        kind = clean_role(role)
        if self.col.find_one({'name': label}):
            raise TokenNameError('access.token_name_taken')
        for attempt in range(ID_TRIES):
            token_id = secrets.token_hex(4)
            token = f'{PREFIX}{token_id}_{secrets.token_urlsafe(24)}'
            doc = {'_id': token_id, 'hash': digest(token), 'name': label, 'role': kind, 'createdBy': str(by), 'createdAt': self.stamp(), 'lastUsed': None, 'usedAt': None, 'revoked': False} | ({'via': via} if via else {})
            try:
                self.col.insert_one(doc)
            except DuplicateKeyError as e:
                if self.col.find_one({'name': label}):
                    raise TokenNameError('access.token_name_taken') from e
                if attempt == ID_TRIES - 1:
                    raise
                continue
            return token, public(doc)

    def resolve(self, token: str, refused: Callable[[dict], bool] = lambda doc: False) -> dict | None:
        found = TOKEN.match(token or '')
        doc = self.col.find_one({'_id': found.group(1)}) if found else None
        expected = (doc or {}).get('hash') or DUMMY
        same = hmac.compare_digest(digest(token or '').encode(), expected.encode())
        if not found or not doc or not same or doc.get('revoked') or refused(doc):
            return None
        now = self.clock()
        used = doc.get('usedAt')
        if used is None or used <= now - USED_EVERY_SECONDS:
            try:
                self.col.update_one({'_id': doc['_id'], '$or': [{'usedAt': None}, {'usedAt': {'$lte': now - USED_EVERY_SECONDS}}]}, {'$set': {'usedAt': now, 'lastUsed': self.stamp()}})
            except PyMongoError as e:
                log.warning('API token %s: last use not recorded: %s', doc['_id'], error_kind(e))
        return public(doc)

    def list(self) -> list[dict]:
        return [public(d) for d in self.col.find().sort('createdAt', ASCENDING)]

    def revoke(self, token_id: str, by: str) -> dict | None:
        key = str(token_id or '')
        doc = self.col.find_one_and_update({'_id': key, 'revoked': {'$ne': True}}, {'$set': {'revoked': True, 'revokedBy': str(by), 'revokedAt': self.stamp()}})
        if doc is not None:
            return public(doc)
        if self.col.find_one({'_id': key}) is None:
            raise UnknownToken(token_id)
        return None
