import datetime as dt
import time
from typing import Callable

from bson import ObjectId
from pymongo import ASCENDING, ReadPreference, WriteConcern
from pymongo.errors import PyMongoError

KEY_MAX = 300


class Throttle:
    def __init__(self, db, clock: Callable[[], float] = time.time, limit: int = 5, window_seconds: int = 900, ip_limit: int = 20):
        self.col = db['login_attempts'].with_options(write_concern=WriteConcern(w=1), read_preference=ReadPreference.PRIMARY)
        self.clock = clock
        self.limit = limit
        self.ip_limit = ip_limit
        self.window = window_seconds

    def ensure_indexes(self) -> None:
        self.col.create_index([('at', ASCENDING)], expireAfterSeconds=self.window)
        self.col.create_index([('key', ASCENDING), ('at', ASCENDING)])

    def when(self, offset: float = 0.0) -> dt.datetime:
        return dt.datetime.fromtimestamp(self.clock() + offset, dt.timezone.utc)

    def reserve(self, *keys: str) -> list | None:
        now = self.when()
        rows = [{'_id': ObjectId(), 'key': key[:KEY_MAX], 'at': now} for key in keys]
        if rows:
            self.col.insert_many(rows)
        since = self.when(-self.window)
        try:
            over = any(self.col.count_documents({'key': row['key'], 'at': {'$gt': since}}) > self.cap(row['key']) for row in rows)
        except PyMongoError:
            self.give_up(rows)
            raise
        if over:
            self.release(rows)
            return None
        return rows

    def cap(self, key: str) -> int:
        return self.ip_limit if key.startswith('ip:') else self.limit

    def give_up(self, rows: list) -> None:
        try:
            self.release(rows)
        except PyMongoError:
            pass

    def release(self, rows: list) -> None:
        if rows:
            self.col.delete_many({'_id': {'$in': [row['_id'] for row in rows]}})

    def clear(self, *keys: str) -> None:
        if keys:
            self.col.delete_many({'key': {'$in': [key[:KEY_MAX] for key in keys]}})

    def forgive(self, rows: list, *keys: str) -> None:
        self.release(rows)
        self.clear(*keys)
