import base64
import hashlib
import logging
import secrets

from cryptography.fernet import Fernet, InvalidToken

SEALED = 'enc:v1:'
META_ID = 'secret-key'

log = logging.getLogger('skopax.sealer')


class Sealer:
    def __init__(self, key: str, generated: bool = False):
        self.generated = generated
        self._fernet = Fernet(base64.urlsafe_b64encode(hashlib.sha256(key.encode()).digest())) if key else None

    @property
    def enabled(self) -> bool:
        return self._fernet is not None

    def seal(self, text: str) -> str:
        if self._fernet is None:
            raise RuntimeError('no secret key is available to seal with')
        return SEALED + self._fernet.encrypt(text.encode()).decode()

    def open(self, value: object) -> str | None:
        if self._fernet is None or not isinstance(value, str) or not value.startswith(SEALED):
            return None
        try:
            return self._fernet.decrypt(value[len(SEALED):].encode()).decode()
        except (InvalidToken, ValueError):
            return None


def master_key(db, settings) -> str:
    if settings.secret_key:
        return settings.secret_key
    db['meta'].update_one({'_id': META_ID}, {'$setOnInsert': {'key': base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b'=').decode()}}, upsert=True)
    return db['meta'].find_one({'_id': META_ID})['key']


def stored_key(db) -> str | None:
    found = db['meta'].find_one({'_id': META_ID})
    return found.get('key') if found else None
