import asyncio
import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass, field

from skopax.errkind import kind
from skopax.sealer import Sealer
from skopax.store import iso, utc_now

COLLECTION = 'admin_secrets'
REFRESH_S = 15


@dataclass(frozen=True)
class Held:
    value: str | None = field(default=None, repr=False)
    unreadable: bool = False
    by: str | None = None
    at: str | None = None


def _text(value) -> str | None:
    return value if isinstance(value, str) else None


class AdminSecret:
    def __init__(self, db, key_id: str, sealer: Callable[[], Sealer], stored_key: Callable[[], str | None], tunables, what: str, unreadable: Callable[[], str | None],
                 log: logging.Logger, every: float = REFRESH_S):
        self.col = db[COLLECTION]
        self.key_id = key_id
        self.sealer = sealer
        self.stored_key = stored_key
        self.tunables = tunables
        self.what = what
        self.unreadable_text = unreadable
        self.log = log
        self.every = every
        self._held = Held()
        self._write = threading.Lock()

    @property
    def held(self) -> Held:
        return self._held

    def refresh(self) -> None:
        with self._write:
            self._load()

    async def loop(self) -> None:
        while True:
            await asyncio.sleep(self.every)
            try:
                await asyncio.to_thread(self.refresh)
            except Exception as e:
                self.log.warning('the stored %s was not read again, keeping the last state: %s', self.what, kind(e))

    def _load(self) -> None:
        doc = self.col.find_one({'_id': self.key_id})
        if doc is None:
            self._held = Held()
            return
        sealed, by, at = doc.get('value'), _text(doc.get('updatedBy')), _text(doc.get('updatedAt'))
        current = self.sealer()
        plain = current.open(sealed)
        if plain is None:
            previous = self.stored_key()
            plain = Sealer(previous).open(sealed) if previous else None
            if plain is not None:
                self._reseal(sealed, plain, current)
        if plain is None:
            text = self.unreadable_text() if not self._held.unreadable else None
            if text:
                self.log.warning('%s', text)
            self._held = Held(None, True, by, at)
            return
        self._held = Held(plain, False, by, at)

    def _reseal(self, sealed: str, plain: str, current: Sealer) -> None:
        if current.enabled and self.col.update_one({'_id': self.key_id, 'value': sealed}, {'$set': {'value': current.seal(plain)}}).modified_count:
            self.log.info('the stored %s was sealed again with the current secret key', self.what)

    def status(self) -> dict:
        held = self._held
        return {'set': held.value is not None, 'unreadable': held.unreadable, 'updatedBy': held.by, 'updatedAt': held.at}

    def set(self, value: str, by: str) -> dict:
        with self._write:
            doc = {'_id': self.key_id, 'value': self.sealer().seal(value), 'updatedBy': by, 'updatedAt': iso(utc_now())}
            before = self.col.find_one_and_replace({'_id': self.key_id}, doc, upsert=True)
            self.tunables.write_change(self.key_id, 'set' if before is not None else None, 'set', by, secret=True)
            self._load()
        return self.status()

    def clear(self, by: str) -> bool:
        with self._write:
            gone = bool(self.col.delete_one({'_id': self.key_id}).deleted_count)
            if gone:
                self.tunables.write_change(self.key_id, 'set', None, by, secret=True)
            self._load()
        return gone
